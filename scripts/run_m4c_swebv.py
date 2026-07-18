"""Run the M4-C SWE-bench agent phase locally in bounded disk batches.

The ChatGPT subscription credential never leaves the local machine.  Each task
is prepared from the frozen set, run once, reduced to a sanitized patch package,
and then its local agent/task image tags are removed before the next task.  The
remote host receives only the resulting package in a later phase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.core.runner import run_task
from traceverdict.m4c import (
    MAX_SUBSCRIPTION_WINDOWS,
    append_subscription_window,
    build_patch_manifest,
    write_patch_package,
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _task_ids(path: Path) -> list[str]:
    values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if len(values) != 16 or any(not value for value in values) or len(set(values)) != 16:
        raise ValueError("M4-C task set must contain 16 unique non-empty IDs")
    return values


def _load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _completed_run(
    conn: sqlite3.Connection, task_id: str, config_id: str
) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT * FROM run WHERE task_id=? AND config_id=? AND "
        "finished_at IS NOT NULL AND tokens_in IS NOT NULL ORDER BY started_at",
        (task_id, config_id),
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError(
            f"duplicate subscription completion for {task_id}: "
            f"{[row['run_id'] for row in rows]}"
        )
    return rows[0] if rows else None


def _guard_unreconciled_attempts(
    conn: sqlite3.Connection, task_id: str, config_id: str
) -> None:
    rows = conn.execute(
        "SELECT run_id,status,exit_reason,tokens_in FROM run "
        "WHERE task_id=? AND config_id=? ORDER BY started_at",
        (task_id, config_id),
    ).fetchall()
    unsafe = [
        dict(row)
        for row in rows
        if row["tokens_in"] is None
        and row["exit_reason"] != "SubscriptionLimitExceeded"
    ]
    if unsafe:
        raise RuntimeError(
            f"unreconciled prior attempt for {task_id}; refusing another model call: "
            f"{unsafe}"
        )


def _image_record(records_path: Path, task_id: str) -> dict[str, Any]:
    records = _load_json(records_path, [])
    matches = [record for record in records if record.get("instance_id") == task_id]
    if len(matches) != 1:
        raise RuntimeError(f"expected one image record for {task_id}, got {len(matches)}")
    return matches[0]


def _prepare_instance(
    *, repo: Path, private_root: Path, task_set: Path, task_id: str, prep_image: str
) -> None:
    task_yaml = private_root / "tasks" / task_id / "task.yaml"
    records_path = private_root / "image_records.json"
    if task_yaml.is_file() and records_path.is_file():
        try:
            record = _image_record(records_path, task_id)
        except RuntimeError:
            record = None
        if record is not None:
            inspected = subprocess.run(
                ["docker", "image", "inspect", str(record["image_ref"])],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspected.returncode == 0:
                return
    hf_cache = repo.parent / ".cache" / "huggingface"
    hf_cache.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--rm",
        "-w",
        "/private",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{repo.resolve()}:/workspace:ro",
        "-v",
        f"{private_root.resolve()}:/private",
        "-v",
        f"{hf_cache.resolve()}:/cache/huggingface",
        prep_image,
        "python",
        "/workspace/scripts/run_swebv_pilot.py",
        "--task-set",
        "/workspace/benchmarks/swebv_subset_v1.txt",
        "--output",
        "/private",
        "prepare",
        "--instance-id",
        task_id,
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=6 * 60 * 60)
    if proc.returncode != 0 or not task_yaml.is_file():
        raise RuntimeError(
            f"image/task preparation failed for {task_id}: "
            f"{(proc.stderr or proc.stdout)[-4000:]}"
        )


def _patch_artifact(conn: sqlite3.Connection, run_id: str) -> tuple[bytes, str]:
    row = conn.execute(
        "SELECT path,sha256 FROM artifact WHERE run_id=? AND kind='patch'", (run_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"completed run {run_id} has no patch artifact")
    path = Path(row["path"])
    if not path.is_file():
        raise RuntimeError(f"patch artifact is missing for {run_id}: {path}")
    patch = path.read_bytes()
    digest = hashlib.sha256(patch).hexdigest()
    if digest != row["sha256"]:
        raise RuntimeError(f"patch artifact SHA mismatch for {run_id}")
    return patch, digest


def _write_handoff(
    *,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    record: dict[str, Any],
    config: dict[str, Any],
    packages: Path,
) -> dict[str, Any]:
    patch, _ = _patch_artifact(conn, row["run_id"])
    manifest = build_patch_manifest(
        task_id=row["task_id"],
        run_id=row["run_id"],
        patch=patch,
        base_commit=conn.execute(
            "SELECT base_commit FROM task WHERE task_id=?", (row["task_id"],)
        ).fetchone()[0],
        original_image_digest=record["image_digest"],
        agent_env_fingerprint=row["env_fingerprint"],
        config_id=config["config_id"],
        codex_binary_sha256=config["model_params"]["codex_binary_sha256"],
    )
    return write_patch_package(
        packages / f"{row['task_id']}__{row['run_id']}.zip",
        manifest=manifest,
        patch=patch,
    )


def _remove_image_tag(tag: str | None) -> dict[str, Any]:
    if not tag:
        return {"tag": tag, "removed": False, "reason": "missing"}
    proc = subprocess.run(
        ["docker", "image", "rm", tag], capture_output=True, text=True, check=False
    )
    return {
        "tag": tag,
        "removed": proc.returncode == 0,
        "detail": (proc.stderr or proc.stdout)[-1000:],
    }


def _cleanup_task_images(
    *, record: dict[str, Any], agent_tag: str | None
) -> list[dict[str, Any]]:
    # Remove the derivative first so the original task tag is no longer a parent
    # of a tagged image. Shared base/env layers remain available to later tasks.
    return [
        _remove_image_tag(agent_tag),
        _remove_image_tag(str(record["image_ref"])),
    ]


def run_arm(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    private_root = args.private_root.resolve()
    config_path = args.config.resolve()
    task_set = args.task_set.resolve()
    config = load_config(config_path)
    ids = _task_ids(task_set)
    ledger = _load_json(args.ledger, {"windows": []})
    if len(ledger.get("windows", [])) >= MAX_SUBSCRIPTION_WINDOWS:
        raise RuntimeError("three subscription windows exhausted; owner ruling required")

    summary = _load_json(
        args.output,
        {
            "phase": "m4c_swebv_local_agent",
            "config_id": config["config_id"],
            "started_at": _now(),
            "rows": [],
        },
    )
    rows_by_task = {row["task_id"]: row for row in summary.get("rows", [])}
    new_run_ids: list[str] = []
    window_started = _now()
    conn = _connect(args.db)
    try:
        for task_id in ids:
            completed = _completed_run(conn, task_id, config["config_id"])
            reused = completed is not None
            result: dict[str, Any] = {}
            if completed is None:
                _prepare_instance(
                    repo=repo,
                    private_root=private_root,
                    task_set=task_set,
                    task_id=task_id,
                    prep_image=args.prep_image,
                )
                _guard_unreconciled_attempts(conn, task_id, config["config_id"])
                result = run_task(
                    private_root / "tasks" / task_id,
                    config_path,
                    db_path=args.db,
                    artifacts_dir=args.artifacts,
                )
                if result.get("status") == "harness_error":
                    if result.get("error") == "SubscriptionLimitExceeded":
                        append_subscription_window(
                            args.ledger,
                            window_started_at=window_started,
                            window_finished_at=_now(),
                            completed_run_ids=new_run_ids,
                            quota_error="SubscriptionLimitExceeded",
                            pause_reason="window_exhausted",
                        )
                    raise RuntimeError(f"{task_id} stopped: {result.get('error')}")
                if not result.get("trace_complete"):
                    raise RuntimeError(f"{task_id} produced an incomplete trace")
                new_run_ids.append(str(result["run_id"]))
                completed = _completed_run(conn, task_id, config["config_id"])
                if completed is None:
                    raise RuntimeError(f"{task_id} completion was not committed")

            record = _image_record(private_root / "image_records.json", task_id)
            package = _write_handoff(
                conn=conn,
                row=completed,
                record=record,
                config=config,
                packages=args.packages,
            )
            agent_tag = ((result.get("agent_layer") or {}).get("tag") if result else None)
            cleanup = _cleanup_task_images(record=record, agent_tag=agent_tag)
            rows_by_task[task_id] = {
                "task_id": task_id,
                "run_id": completed["run_id"],
                "reused": reused,
                "status": completed["status"],
                "tokens_in": completed["tokens_in"],
                "tokens_out": completed["tokens_out"],
                "env_fingerprint": completed["env_fingerprint"],
                "patch_package": package,
                "image_cleanup": cleanup,
                "finished_at": _now(),
            }
            summary["rows"] = [rows_by_task[item] for item in ids if item in rows_by_task]
            summary["completed"] = len(summary["rows"])
            summary["finished_at"] = None
            _write_json(args.output, summary)
            print(json.dumps(summary["rows"][-1], ensure_ascii=False), flush=True)

        append_subscription_window(
            args.ledger,
            window_started_at=window_started,
            window_finished_at=_now(),
            completed_run_ids=[rows_by_task[item]["run_id"] for item in ids],
            quota_error=None,
            pause_reason=None,
        )
        summary["finished_at"] = _now()
        summary["complete"] = True
        _write_json(args.output, summary)
        return summary
    finally:
        conn.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repo", type=Path, required=True)
    result.add_argument("--private-root", type=Path, required=True)
    result.add_argument("--task-set", type=Path, required=True)
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--db", type=Path, required=True)
    result.add_argument("--artifacts", type=Path, required=True)
    result.add_argument("--packages", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--ledger", type=Path, required=True)
    result.add_argument(
        "--prep-image", default="traceverdict/m4c-prep:swebench-4.1.0"
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run_arm(parser().parse_args()), ensure_ascii=False, indent=2))
