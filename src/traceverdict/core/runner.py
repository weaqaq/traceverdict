"""Orchestrate a single task run: snapshot → Docker mini-swe-agent → tracer."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traceverdict.adapters.mini_swe_agent import AdapterHarnessError, run_mini_swe_agent
from traceverdict.adapters.codex import run_codex
from traceverdict.adapters.swe_agent import run_swe_agent
from traceverdict.core.config_loader import load_config
from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.image import (
    DockerUnavailableError,
    ensure_local_image,
    image_digest,
    make_env_fingerprint,
    require_docker,
)
from traceverdict.snapshot.patch import collect_patch, write_artifact_file
from traceverdict.snapshot.suite_image import ensure_suite_image
from traceverdict.snapshot.workspace import (
    cleanup_work_copy,
    materialize_work_copy,
    repair_work_copy_ownership,
)
from traceverdict.snapshot.codex_image import ensure_codex_agent_image
from traceverdict.tracer import db as dbmod
from traceverdict.tracer.trajectory import (
    map_trajectory_to_events,
    reconcile_trace,
    summarize_llm_metrics,
)
from traceverdict.tracer.codex_jsonl import (
    map_codex_trajectory_to_events,
    summarize_codex_metrics,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_db(db_path: Path):
    if db_path.is_file():
        conn = dbmod._connect(db_path)
        try:
            conn.execute("SELECT 1 FROM task LIMIT 1")
            return conn
        except Exception:
            conn.close()
    return dbmod.init_db(db_path)


def _detach_swe_agent_origin(work_copy: Path) -> None:
    """Remove the bundle origin that is not visible inside the agent container."""
    proc = subprocess.run(
        ["git", "-C", str(work_copy), "remote", "remove", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "failed to detach disposable SWE-agent origin: "
            f"{(proc.stderr or proc.stdout)[-1000:]}"
        )


def _upsert_task(conn, task: dict[str, Any]) -> None:
    if dbmod.get_task(conn, task["task_id"]) is not None:
        return
    dbmod.insert_task(
        conn,
        {
            "task_id": task["task_id"],
            "suite": task["suite"],
            "source": task["source"],
            "repo_ref": str(task["repo_ref_path"]),
            "base_commit": task["base_commit"],
            "image_ref": task["image_ref"],
            "instruction": task["instruction"],
            "budget_json": json.dumps(task["budget"], ensure_ascii=False),
            "forbidden_json": json.dumps(task["forbidden_paths"], ensure_ascii=False),
            "gt_type": task["gt"]["type"],
            "gt_spec_json": json.dumps(task["gt"]["spec"], ensure_ascii=False),
            "tags_json": json.dumps(task["tags"], ensure_ascii=False),
            "created_at": _now(),
        },
    )


def _upsert_config(conn, cfg: dict[str, Any]) -> None:
    desired = {
        "config_id": cfg["config_id"],
        "agent_name": cfg["agent_name"],
        "agent_version": cfg["agent_version"],
        "model_name": cfg["model_name"],
        "model_params_json": json.dumps(
            cfg["model_params"], ensure_ascii=False, sort_keys=True
        ),
        "prompt_version": cfg["prompt_version"],
        "harness_version": cfg["harness_version"],
        "notes": cfg.get("notes"),
    }
    existing = dbmod.get_config(conn, cfg["config_id"])
    if existing is None:
        dbmod.insert_config(conn, desired)
        return
    mismatches = {
        key: {"stored": existing.get(key), "requested": value}
        for key, value in desired.items()
        if existing.get(key) != value
    }
    if mismatches:
        raise ValueError(
            "immutable config_id collision for "
            f"{cfg['config_id']!r}: {json.dumps(mismatches, ensure_ascii=False)}"
        )


def _update_run(conn, run_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE run SET {cols} WHERE run_id=?", (*fields.values(), run_id))
    conn.commit()


def _status_from_exit(*, has_exit: bool, exit_status: str | None) -> str:
    if not has_exit:
        return "harness_error"
    if exit_status == "Submitted":
        return "ok"
    if exit_status == "LimitsExceeded":
        return "budget"
    if exit_status == "TimeExceeded":
        return "timeout"
    return "agent_error"


def run_task(
    task_path: str | Path,
    config_spec: str | Path,
    *,
    db_path: str | Path = "reports/traceverdict.db",
    artifacts_dir: str | Path = "reports/artifacts",
    repetition_idx: int = 0,
) -> dict[str, Any]:
    """Execute one Scenario Re-run with strict DockerEnvironment (D1-d)."""
    if repetition_idx < 0:
        raise ValueError("repetition_idx must be non-negative")
    task = load_task(task_path)
    cfg = load_config(config_spec)
    budget = task["budget"]
    monotonic_started = time.perf_counter()

    def elapsed() -> float:
        return max(0.0, time.perf_counter() - monotonic_started)

    db_path = Path(db_path)
    artifacts_root = Path(artifacts_dir)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    conn = _connect_db(db_path)
    _upsert_task(conn, task)
    _upsert_config(conn, cfg)

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    dbmod.insert_run(
        conn,
        {
            "run_id": run_id,
            "task_id": task["task_id"],
            "config_id": cfg["config_id"],
            "repetition_idx": repetition_idx,
            "mode": "scenario",
            "status": "harness_error",
            "exit_reason": None,
            "started_at": _now(),
            "finished_at": None,
            "wall_time_s": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "seed": None,
            "env_fingerprint": None,
        },
    )

    work_copy: Path | None = None
    docker_exe: str | None = None
    image_for_run: str | None = None
    run_out: dict[str, Any] = {"run_id": run_id, "db_path": str(db_path)}

    try:
        docker_exe = require_docker()
        task_image = task["image_ref"] or cfg["local_image_tag"]
        suite_digest = ensure_suite_image(
            task_dir=task["task_dir"], image_ref=task_image, docker_exe=docker_exe
        )
        if suite_digest is None and not task["image_ref"]:
            # Only tasks without an explicit image may use the config fallback.
            ensure_local_image(
                base_image=cfg["base_image"],
                local_tag=cfg["local_image_tag"],
                docker_exe=docker_exe,
            )
        # An explicit external image (for example an official SWE-bench
        # TestSpec image) is authoritative.  Never retag the self-suite image
        # over it when no nearest _image build descriptor exists.
        image_for_run = task_image
        digest = suite_digest or image_digest(task_image, docker_exe=docker_exe)
        if cfg["agent_name"] == "codex":
            expected_sha = cfg["model_params"].get("codex_binary_sha256")
            expected_bwrap_sha = cfg["model_params"].get("codex_bwrap_binary_sha256")
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                raise AdapterHarnessError(
                    "Codex config must freeze codex_binary_sha256"
                )
            if not isinstance(expected_bwrap_sha, str) or len(expected_bwrap_sha) != 64:
                raise AdapterHarnessError(
                    "Codex config must freeze codex_bwrap_binary_sha256"
                )
            image_for_run, digest, layer_evidence = ensure_codex_agent_image(
                base_image=task_image,
                docker_executable=docker_exe,
                expected_binary_sha256=expected_sha,
                expected_bwrap_sha256=expected_bwrap_sha,
                expected_version=cfg["agent_version"],
            )
            run_out["agent_layer"] = layer_evidence
        if (
            cfg["agent_name"] == "swe-agent"
            and task["suite"] == "self"
            and cfg["local_image_tag"] != task_image
        ):
            # SWE-agent resets the repository with the git CLI before its first
            # model query. Keep the T2 task/verifier image immutable and run
            # the agent in a suite-owned derivative that adds only pinned git.
            image_for_run = cfg["local_image_tag"]
            derived_digest = ensure_suite_image(
                task_dir=task["task_dir"],
                image_ref=image_for_run,
                docker_exe=docker_exe,
                dockerfile_name=cfg["suite_dockerfile"],
            )
            if derived_digest is None:
                raise RuntimeError("self SWE-agent image descriptor not found")
            digest = derived_digest
        env_fp = make_env_fingerprint(digest, task["base_commit"])
        _update_run(conn, run_id, env_fingerprint=env_fp)
        run_out["env_fingerprint"] = env_fp

        # One-shot work copy from frozen bundle — never mount tasks/self/S1 raw (D1-d).
        work_copy = materialize_work_copy(task["repo_ref_path"], task["base_commit"])
        run_out["work_copy"] = str(work_copy)
        if cfg["agent_name"] == "swe-agent":
            # GitPython clones record the host-only bundle path as origin.
            # SWE-agent 1.1.0 unconditionally runs `git fetch` during reset;
            # with that origin mounted into /testbed, SWE-ReX can stall until
            # the adapter wall timeout. The disposable checkout is already
            # frozen at base_commit, so detach only this unreachable remote.
            _detach_swe_agent_origin(work_copy)

        adapter_dir = artifacts_root / run_id / "adapter"
        adapter_dir.mkdir(parents=True, exist_ok=True)
        try:
            adapter = (
                run_mini_swe_agent
                if cfg["agent_name"] == "mini-swe-agent"
                else run_swe_agent
                if cfg["agent_name"] == "swe-agent"
                else run_codex
                if cfg["agent_name"] == "codex"
                else None
            )
            if adapter is None:
                raise AdapterHarnessError(
                    f"unsupported agent_name: {cfg['agent_name']!r}"
                )
            result = adapter(
                instruction=task["instruction"],
                image=image_for_run,
                docker_executable=docker_exe,
                host_work_path=work_copy,
                container_cwd=cfg["container_cwd"],
                model_name=cfg["model_name"],
                model_params=cfg["model_params"],
                litellm_model_registry=cfg["litellm_model_registry"],
                agent_version=cfg["agent_version"],
                cost_limit=float(budget.get("max_cost_usd") or 0) or 3.0,
                step_limit=int(budget.get("max_steps") or 0),
                wall_time_s=int(budget.get("max_wall_s") or 0),
                work_dir=adapter_dir,
            )
        except AdapterHarnessError as e:
            _update_run(
                conn,
                run_id,
                status="harness_error",
                exit_reason=str(e)[:500],
                finished_at=_now(),
                wall_time_s=elapsed(),
            )
            run_out["status"] = "harness_error"
            run_out["error"] = str(e)
            return run_out

        traj = result.traj
        info = traj.get("info") or {}
        native_submission = info.get("submission") or ""
        instance_cost = (info.get("model_stats") or {}).get("instance_cost")

        # The agent container runs as root and can create root-owned Git
        # objects in the bind mount.  Restore ownership before host-side
        # ``git add -A`` so paid trajectories cannot be lost after model exit.
        repair_work_copy_ownership(
            work_copy, docker_executable=docker_exe, image=image_for_run
        )
        patch_text, patch_sha = collect_patch(work_copy, task["base_commit"])
        art_dir = artifacts_root / run_id
        art_dir.mkdir(parents=True, exist_ok=True)
        patch_path = art_dir / "patch.diff"
        write_artifact_file(patch_path, patch_text)
        dbmod.insert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-patch",
                "run_id": run_id,
                "kind": "patch",
                "path": str(patch_path),
                "sha256": patch_sha,
            },
        )
        source_artifact_kinds: list[str] = []
        for source_kind, source_path in result.source_artifacts.items():
            source_path = Path(source_path)
            if not source_path.is_file():
                raise ValueError(f"adapter source artifact missing: {source_path}")
            source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
            dbmod.insert_artifact(
                conn,
                {
                    "artifact_id": f"{run_id}-{source_kind}",
                    "run_id": run_id,
                    "kind": source_kind,
                    "path": str(source_path),
                    "sha256": source_sha,
                },
            )
            source_artifact_kinds.append(source_kind)
        fs_path = art_dir / "fs_diff.diff"
        write_artifact_file(fs_path, patch_text)
        dbmod.insert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-fs_diff",
                "run_id": run_id,
                "kind": "fs_diff",
                "path": str(fs_path),
                "sha256": patch_sha,
            },
        )
        log_path = art_dir / "trajectory.traj.json"
        shutil.copy2(result.traj_path, log_path)
        log_sha = hashlib.sha256(log_path.read_bytes()).hexdigest()
        dbmod.insert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-log",
                "run_id": run_id,
                "kind": "log",
                "path": str(log_path),
                "sha256": log_sha,
            },
        )

        if cfg["agent_name"] == "codex":
            events, count_ok, exit_status = map_codex_trajectory_to_events(
                traj, store_prompt_full=cfg["store_prompt_full"]
            )
        else:
            events, count_ok, exit_status = map_trajectory_to_events(
                traj, store_prompt_full=cfg["store_prompt_full"]
            )
        registry = json.loads(
            Path(cfg["litellm_model_registry"]).read_text(encoding="utf-8")
        )
        model_price = registry.get(cfg["model_name"])
        if not isinstance(model_price, dict):
            raise ValueError(
                f"model {cfg['model_name']!r} missing from LiteLLM registry"
            )
        shadow_cost = None
        if cfg["agent_name"] == "codex":
            tokens_in, tokens_out, cost, shadow_cost = summarize_codex_metrics(
                events, model_price=model_price
            )
        else:
            tokens_in, tokens_out, cost = summarize_llm_metrics(
                events,
                instance_cost=instance_cost,
                model_price=model_price,
            )
        has_exit = any(e["etype"] == "final" for e in events)
        for ev in events:
            dbmod.insert_event(
                conn,
                {
                    "run_id": run_id,
                    "step_idx": ev["step_idx"],
                    "ts": ev["ts"],
                    "etype": ev["etype"],
                    "payload_json": ev["payload_json"],
                    "tokens_in": ev.get("tokens_in"),
                    "tokens_out": ev.get("tokens_out"),
                    "latency_ms": ev.get("latency_ms"),
                },
            )

        if native_submission and native_submission.strip() != patch_text.strip():
            note_payload = {
                "kind": "submission_patch_divergence",
                "patch_sha256": patch_sha,
                "submission_len": len(native_submission),
                "patch_len": len(patch_text),
                "detail": (
                    "info.submission differs from authoritative work-copy diff (D1-c); not an error"
                ),
            }
            dbmod.insert_event(
                conn,
                {
                    "run_id": run_id,
                    "step_idx": len(events),
                    "ts": _now(),
                    "etype": "note",
                    "payload_json": json.dumps(note_payload, ensure_ascii=False),
                    "tokens_in": None,
                    "tokens_out": None,
                    "latency_ms": None,
                },
            )

        sub_sha = (
            hashlib.sha256(native_submission.encode("utf-8")).hexdigest()
            if native_submission
            else None
        )
        trace_complete = reconcile_trace(
            event_count_ok=count_ok,
            has_exit=has_exit,
            patch_sha256=patch_sha,
            native_submission=native_submission,
            submission_sha256=sub_sha,
        )

        status = _status_from_exit(has_exit=has_exit, exit_status=exit_status)
        finished = _now()
        _update_run(
            conn,
            run_id,
            status=status,
            exit_reason=exit_status or ("missing_exit" if not has_exit else None),
            finished_at=finished,
            wall_time_s=elapsed(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            env_fingerprint=env_fp,
        )

        run_out.update(
            {
                "status": status,
                "trace_complete": trace_complete,
                "patch_sha256": patch_sha,
                "exit_status": exit_status,
                "event_count": len(dbmod.get_events_for_run(conn, run_id)),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost,
                "api_equivalent_shadow_cost": shadow_cost,
                "artifacts": ["patch", "fs_diff", "log", *source_artifact_kinds],
            }
        )
        return run_out

    except DockerUnavailableError as e:
        _update_run(
            conn,
            run_id,
            status="harness_error",
            exit_reason=f"docker_unavailable: {e}"[:500],
            finished_at=_now(),
            wall_time_s=elapsed(),
        )
        run_out["status"] = "harness_error"
        run_out["error"] = str(e)
        return run_out
    except Exception as e:
        _update_run(
            conn,
            run_id,
            status="harness_error",
            exit_reason=str(e)[:500],
            finished_at=_now(),
            wall_time_s=elapsed(),
        )
        run_out["status"] = "harness_error"
        run_out["error"] = str(e)
        return run_out
    finally:
        if work_copy is not None:
            cleanup_work_copy(
                work_copy,
                docker_executable=docker_exe,
                image=image_for_run,
            )
        conn.close()
