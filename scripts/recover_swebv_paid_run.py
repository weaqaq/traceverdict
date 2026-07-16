#!/usr/bin/env python3
"""Recover a paid SWE-bench run whose model trajectory outlived patch capture.

This repository-external operations entry point performs no model calls.  It
replays the trajectory's recorded bash actions in one fresh disposable
checkout, requires every recorded return code to match, then finalizes the
original run row and executes the official judge exactly once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.core.runner import _now, _status_from_exit, _update_run
from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.patch import collect_patch, write_artifact_file
from traceverdict.snapshot.workspace import (
    cleanup_work_copy,
    materialize_work_copy,
    repair_work_copy_ownership,
)
from traceverdict.swebench_adapter import (
    assert_three_way_agreement,
    record_official_verdict,
    run_official_evaluation,
)
from traceverdict.tracer import db as dbmod
from traceverdict.tracer.trajectory import (
    map_trajectory_to_events,
    reconcile_trace,
    summarize_llm_metrics,
)


def _observations_by_id(traj: dict[str, Any]) -> dict[str, int]:
    values: dict[str, int] = {}
    for message in traj.get("messages") or []:
        if message.get("role") not in {"tool", "observation"}:
            continue
        tool_call_id = message.get("tool_call_id")
        returncode = (message.get("extra") or {}).get("returncode")
        if tool_call_id is None or isinstance(returncode, bool) or not isinstance(
            returncode, int
        ):
            raise ValueError("recorded observation lacks an integer returncode")
        values[str(tool_call_id)] = returncode
    return values


def _actions(traj: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        action
        for message in traj.get("messages") or []
        if message.get("role") == "assistant"
        for action in ((message.get("extra") or {}).get("actions") or [])
    ]
    if not actions or any(
        not isinstance(action.get("command"), str)
        or not action.get("tool_call_id")
        for action in actions
    ):
        raise ValueError("trajectory actions are not replayable bash commands")
    return actions


def replay_actions(
    *,
    traj: dict[str, Any],
    work_copy: Path,
    image: str,
    docker_executable: str = "docker",
) -> dict[str, Any]:
    """Replay all recorded actions without a model and compare return codes."""
    actions = _actions(traj)
    observations = _observations_by_id(traj)
    container = f"traceverdict-recover-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        [
            docker_executable,
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-v",
            f"{work_copy.resolve()}:/testbed:rw",
            "-w",
            "/testbed",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "while :; do sleep 3600; done",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    matched = 0
    unobserved = 0
    try:
        for index, action in enumerate(actions):
            proc = subprocess.run(
                [
                    docker_executable,
                    "exec",
                    "-w",
                    "/testbed",
                    "-e",
                    "PAGER=cat",
                    "-e",
                    "PIP_PROGRESS_BAR=off",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=enabled",
                    container,
                    "bash",
                    "-c",
                    action["command"],
                ],
                capture_output=True,
                text=True,
            )
            tool_call_id = str(action["tool_call_id"])
            expected = observations.get(tool_call_id)
            if expected is None:
                unobserved += 1
                continue
            if proc.returncode != expected:
                raise RuntimeError(
                    "zero-model replay diverged at action "
                    f"{index}: expected returncode {expected}, got {proc.returncode}"
                )
            matched += 1
    finally:
        subprocess.run(
            [docker_executable, "rm", "-f", container],
            check=False,
            capture_output=True,
            text=True,
        )
    if matched != len(observations) or unobserved != len(actions) - len(observations):
        raise RuntimeError("zero-model replay did not reconcile action counts")
    return {
        "actions": len(actions),
        "observations": len(observations),
        "matched_returncodes": matched,
        "unobserved_exit_actions": unobserved,
    }


def backup_frozen_git_metadata(work_copy: Path) -> Path:
    """Copy the clean checkout's Git metadata outside the agent-mounted tree."""
    source = work_copy / ".git"
    if not source.is_dir():
        raise RuntimeError("fresh recovery checkout has no .git directory")
    parent = Path(tempfile.mkdtemp(prefix="traceverdict-git-backup-"))
    backup = parent / ".git"
    shutil.copytree(source, backup)
    return backup


def restore_frozen_git_metadata(work_copy: Path, backup: Path) -> None:
    """Restore only Git metadata; retain the replayed working-tree contents."""
    destination = work_copy / ".git"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(backup, destination)


def _finalize(
    *,
    conn,
    run_id: str,
    task: dict[str, Any],
    cfg: dict[str, Any],
    traj: dict[str, Any],
    traj_path: Path,
    work_copy: Path,
    artifacts_root: Path,
) -> dict[str, Any]:
    patch_text, patch_sha = collect_patch(work_copy, task["base_commit"])
    art_dir = artifacts_root / run_id
    art_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "patch": art_dir / "patch.diff",
        "fs_diff": art_dir / "fs_diff.diff",
        "log": art_dir / "trajectory.traj.json",
    }
    write_artifact_file(paths["patch"], patch_text)
    write_artifact_file(paths["fs_diff"], patch_text)
    shutil.copy2(traj_path, paths["log"])
    for kind, path in paths.items():
        dbmod.upsert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-{kind}",
                "run_id": run_id,
                "kind": kind,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )

    events, count_ok, exit_status = map_trajectory_to_events(
        traj, store_prompt_full=cfg["store_prompt_full"]
    )
    registry = json.loads(
        Path(cfg["litellm_model_registry"]).read_text(encoding="utf-8")
    )
    model_price = registry.get(cfg["model_name"])
    if not isinstance(model_price, dict):
        raise ValueError(f"model {cfg['model_name']!r} missing from registry")
    info = traj.get("info") or {}
    instance_cost = (info.get("model_stats") or {}).get("instance_cost")
    tokens_in, tokens_out, cost = summarize_llm_metrics(
        events, instance_cost=instance_cost, model_price=model_price
    )
    conn.execute("DELETE FROM event WHERE run_id=?", (run_id,))
    conn.commit()
    for event in events:
        dbmod.insert_event(conn, {"run_id": run_id, **event})
    has_exit = any(event["etype"] == "final" for event in events)
    native_submission = info.get("submission") or ""
    trace_complete = reconcile_trace(
        event_count_ok=count_ok,
        has_exit=has_exit,
        patch_sha256=patch_sha,
        native_submission=native_submission,
        submission_sha256=(
            hashlib.sha256(native_submission.encode()).hexdigest()
            if native_submission
            else None
        ),
    )
    status = _status_from_exit(has_exit=has_exit, exit_status=exit_status)
    _update_run(
        conn,
        run_id,
        status=status,
        exit_reason=exit_status or ("missing_exit" if not has_exit else None),
        finished_at=_now(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
    )
    return {
        "run_id": run_id,
        "status": status,
        "trace_complete": trace_complete,
        "patch_sha256": patch_sha,
        "exit_status": exit_status,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "patch_path": str(paths["patch"]),
    }


def recover(args: argparse.Namespace) -> dict[str, Any]:
    task_dir = args.output / "tasks" / args.instance_id
    task = load_task(task_dir)
    cfg = load_config(args.config)
    traj_path = (
        args.output / "artifacts" / args.run_id / "adapter" / "run.traj.json"
    )
    traj = json.loads(traj_path.read_text(encoding="utf-8"))
    conn = dbmod._connect(args.db)
    work_copy: Path | None = None
    git_backup: Path | None = None
    try:
        run = dbmod.get_run(conn, args.run_id)
        if run is None or run["task_id"] != args.instance_id:
            raise ValueError("run/task identity mismatch")
        if run["status"] != "harness_error" or dbmod.get_events_for_run(
            conn, args.run_id
        ):
            raise ValueError("recovery requires an unfinalized harness_error run")
        work_copy = materialize_work_copy(
            task["repo_ref_path"], task["base_commit"]
        )
        git_backup = backup_frozen_git_metadata(work_copy)
        try:
            replay = replay_actions(
                traj=traj, work_copy=work_copy, image=task["image_ref"]
            )
        finally:
            repair_work_copy_ownership(
                work_copy, docker_executable="docker", image=task["image_ref"]
            )
            restore_frozen_git_metadata(work_copy, git_backup)
        result = _finalize(
            conn=conn,
            run_id=args.run_id,
            task=task,
            cfg=cfg,
            traj=traj,
            traj_path=traj_path,
            work_copy=work_copy,
            artifacts_root=args.output / "artifacts",
        )
        records = json.loads(
            (args.output / "image_records.json").read_text(encoding="utf-8")
        )
        image_record = next(
            item for item in records if item["instance_id"] == args.instance_id
        )
        official_dir = args.output / "official" / args.run_id
        patch_text = Path(result["patch_path"]).read_text(encoding="utf-8")
        raw, aggregate = run_official_evaluation(
            python_executable=args.python_executable,
            instance_id=args.instance_id,
            patch_text=patch_text,
            output_dir=official_dir,
            official_run_id=f"t5-{args.run_id}",
            model_name_or_path=f"traceverdict__{cfg['config_id']}",
            image_path=image_record["image_path"],
        )
        outcome = record_official_verdict(
            conn,
            run_id=args.run_id,
            instance_id=args.instance_id,
            raw_report_path=raw,
            aggregate_report_path=aggregate,
        )
        verdict = conn.execute(
            "SELECT passed FROM verdict WHERE run_id=? AND name='swebench'",
            (args.run_id,),
        ).fetchone()
        assert_three_way_agreement(
            traceverdict_passed=bool(verdict["passed"]),
            raw_resolved=outcome.raw_resolved,
            aggregate_resolved=outcome.aggregate_resolved,
        )
        result.update(
            {
                "recovery": replay,
                "traceverdict_verdict": bool(verdict["passed"]),
                "official_raw_resolved": outcome.raw_resolved,
                "official_aggregate_resolved": outcome.aggregate_resolved,
                "image_path": image_record["image_path"],
            }
        )
        completion = args.output / "runs" / f"{args.instance_id}.json"
        completion.parent.mkdir(parents=True, exist_ok=True)
        completion.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    finally:
        if work_copy is not None:
            cleanup_work_copy(
                work_copy, docker_executable="docker", image=task["image_ref"]
            )
        if git_backup is not None:
            shutil.rmtree(git_backup.parent, ignore_errors=True)
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instance_id")
    parser.add_argument("run_id")
    parser.add_argument("--output", type=Path, default=Path("reports/t5"))
    parser.add_argument("--db", type=Path, default=Path("reports/t5/traceverdict.db"))
    parser.add_argument("--config", type=Path, default=Path("configs/dev.yaml"))
    parser.add_argument("--python-executable", default="python")
    args = parser.parse_args()
    print(json.dumps(recover(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
