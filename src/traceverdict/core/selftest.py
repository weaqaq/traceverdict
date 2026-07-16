"""M1 four-gate selftest orchestrator (T4)."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from git import Repo

from traceverdict.compare import compare_configs, load_task_set
from traceverdict.compare.constants import CUMULATIVE_BASELINE_API_TRIPWIRE_USD
from traceverdict.core.runner import run_task
from traceverdict.core.simple_yaml import dump_to_path, load_path
from traceverdict.core.task_loader import load_task
from traceverdict.injections import (
    DETERMINISTIC_INJECTION_IDS,
    INJECTION_DESCRIPTIONS,
    INJECTION_DISPLAY_NAMES,
    M1_INJECTION_IDS,
    PROBABILISTIC_INJECTION_IDS,
    generate_injected_config,
    injection_patch,
)
from traceverdict.report import generate_report
from traceverdict.snapshot.image import make_env_fingerprint, require_docker
from traceverdict.snapshot.suite_image import ensure_suite_image
from traceverdict.snapshot.workspace import cleanup_work_copy, materialize_work_copy
from traceverdict.tracer import db as dbmod
from traceverdict.tracer.db import upsert_injection
from traceverdict.verifier import verify_run


def cumulative_cost_usd(db_path: str | Path) -> float:
    if not Path(db_path).is_file():
        return 0.0
    conn = dbmod._connect(db_path)
    try:
        return float(conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM run").fetchone()[0])
    finally:
        conn.close()


def _assert_cost_gate(db_path: str | Path) -> float:
    total = cumulative_cost_usd(db_path)
    if total >= CUMULATIVE_BASELINE_API_TRIPWIRE_USD:
        raise RuntimeError(
            f"cumulative API cost tripwire reached: ${total:.8f} >= "
            f"${CUMULATIVE_BASELINE_API_TRIPWIRE_USD:.2f}"
        )
    return total


def classify_no_alarm(injection_id: str) -> dict[str, str]:
    """D11/D12 stop routing for a finalized Battery v2 instrument."""
    if injection_id in PROBABILISTIC_INJECTION_IDS:
        return {
            "kind": "probabilistic_instrument_none",
            "injection_id": injection_id,
            "finding": "F-4" if injection_id == "I2P" else "F-5",
        }
    if injection_id in DETERMINISTIC_INJECTION_IDS:
        return {
            "kind": "deterministic_instrument_pipeline_bug",
            "injection_id": injection_id,
        }
    raise ValueError(f"injection is not in Battery v2: {injection_id}")


def _mini_container_ids(docker_executable: str) -> set[str]:
    proc = subprocess.run(
        [docker_executable, "ps", "-q", "--filter", "name=minisweagent-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to list mini containers: {proc.stderr}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _cleanup_new_mini_containers(docker_executable: str, before: set[str]) -> None:
    created = sorted(_mini_container_ids(docker_executable) - before)
    if not created:
        return
    proc = subprocess.run(
        [docker_executable, "rm", "-f", *created], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to clean mini containers {created}: {proc.stderr}")


def _session_config(
    raw: dict[str, Any], path: Path, *, parent: str, session_id: str, baseline: str
) -> dict[str, Any]:
    result = json.loads(json.dumps(raw))
    result["config_id"] = f"{parent}-selftest-{session_id}-{baseline}-base"
    result["notes"] = (
        f"parent_config_id={parent}; selftest_session={session_id}; "
        f"baseline={baseline}; injection_id=none"
    )
    dump_to_path(path, result)
    return result


def build_session_configs(
    base_config: str | Path, output_dir: str | Path, session_id: str
) -> dict[str, Path]:
    base_config = Path(base_config).resolve()
    raw = load_path(base_config)
    registry = raw.get("litellm_model_registry")
    if registry and not Path(registry).is_absolute():
        raw["litellm_model_registry"] = str((base_config.parent / registry).resolve())
    parent = str(raw["config_id"])
    config_dir = Path(output_dir) / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    thinking_path = config_dir / "thinking-base.yaml"
    _session_config(raw, thinking_path, parent=parent, session_id=session_id, baseline="thinking")

    result = {"thinking": thinking_path}
    for injection_id in ("I1Q", "I2P", "I3Q", "I5P"):
        path = config_dir / f"{injection_id.lower()}.yaml"
        generate_injected_config(
            injection_id,
            thinking_path,
            path,
            session_id=session_id,
            parent_config_id=parent,
        )
        result[injection_id] = path

    nonthinking = json.loads(json.dumps(raw))
    nonthinking["model_params"] = {"thinking": {"type": "disabled"}}
    nonthinking_path = config_dir / "nonthinking-base.yaml"
    _session_config(
        nonthinking,
        nonthinking_path,
        parent=parent,
        session_id=session_id,
        baseline="nonthinking",
    )
    i4_path = config_dir / "i4q.yaml"
    generate_injected_config(
        "I4Q",
        nonthinking_path,
        i4_path,
        session_id=session_id,
        parent_config_id=parent,
    )
    result["nonthinking"] = nonthinking_path
    result["I4Q"] = i4_path
    return result


def check_environment_reproduction(
    task_ids: list[str], suite_dir: Path, *, docker_executable: str
) -> dict[str, Any]:
    results = {}
    for task_id in task_ids:
        task = load_task(suite_dir / task_id)
        digest = ensure_suite_image(
            task_dir=task["task_dir"], image_ref=task["image_ref"], docker_exe=docker_executable
        )
        fingerprints = []
        cleaned = []
        for _ in range(2):
            work = materialize_work_copy(task["repo_ref_path"], task["base_commit"])
            repo = Repo(str(work))
            try:
                assert repo.head.commit.hexsha == task["base_commit"]
                fingerprints.append(make_env_fingerprint(digest, task["base_commit"]))
            finally:
                repo.close()
                cleanup_work_copy(work)
                cleaned.append(not work.exists())
        results[task_id] = {
            "passed": len(set(fingerprints)) == 1 and all(cleaned),
            "fingerprints": fingerprints,
            "copies_deleted": cleaned,
        }
    return {
        "passed": all(item["passed"] for item in results.values()),
        "passed_count": sum(item["passed"] for item in results.values()),
        "total": len(results),
        "tasks": results,
    }


def evaluate_gates(
    *, trace_complete: list[bool], environment: dict[str, Any], alarms: dict[str, str], reports: list[str]
) -> dict[str, Any]:
    trace_rate = sum(trace_complete) / len(trace_complete) if trace_complete else 0.0
    injection_ok = all(alarms.get(i) in {"hard", "warn"} for i in M1_INJECTION_IDS)
    report_ok = len(reports) == 5 and all(Path(path).is_file() for path in reports)
    checks = {
        "trace_complete": {"passed": trace_rate >= 0.99, "rate": trace_rate},
        "environment_reproduction": environment,
        "injection_detection": {"passed": injection_ok, "alarms": alarms},
        "report_generation": {"passed": report_ok, "paths": reports},
    }
    return {"passed": all(check["passed"] for check in checks.values()), "checks": checks}


def _write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "selftest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# TraceVerdict selftest `{summary['session_id']}`",
        "",
        f"- Status: **{summary['status']}**",
        f"- Finalized battery: `{summary.get('battery', list(M1_INJECTION_IDS))}`",
        f"- I4Q baseline: `{summary.get('i4_baseline', 'nonthinking')}` (different from I1Q/I2P/I3Q/I5P)",
        f"- Cost USD: `{summary.get('cost_usd', 0)}` / tripwire `28.0`",
        "",
        "## Checks",
        "",
        "```json",
        json.dumps(summary.get("checks", {}), ensure_ascii=False, indent=2),
        "```",
    ]
    (output_dir / "selftest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_selftest(
    base_config: str | Path,
    task_set_path: str | Path,
    *,
    db_path: str | Path = "reports/traceverdict.db",
    output_dir: str | Path = "reports/selftest",
    artifacts_dir: str | Path = "reports/artifacts",
    session_id: str | None = None,
    run_fn: Callable[..., dict[str, Any]] = run_task,
    verify_fn: Callable[..., list[dict[str, Any]]] = verify_run,
) -> dict[str, Any]:
    session_id = session_id or uuid.uuid4().hex[:8]
    output_dir = Path(output_dir)
    output_dir = output_dir / session_id if output_dir.name != session_id else output_dir
    task_ids, _ = load_task_set(task_set_path)
    suite_dir = Path(task_set_path).resolve().parent
    configs = build_session_configs(base_config, output_dir, session_id)
    summary: dict[str, Any] = {
        "session_id": session_id,
        "status": "running",
        "executor": "Codex",
        "k": 1,
        "battery": list(M1_INJECTION_IDS),
        "display_names": INJECTION_DISPLAY_NAMES,
        "evidence_scope": "single clean session; no cross-session stitching",
        "deterministic_injections": list(DETERMINISTIC_INJECTION_IDS),
        "probabilistic_exceptions": list(PROBABILISTIC_INJECTION_IDS),
        "i4_baseline": "nonthinking (I1Q/I2P/I3Q/I5P use thinking)",
        "runs": {},
        "comparisons": {},
        "cost_before_usd": _assert_cost_gate(db_path),
    }
    _write_summary(output_dir, summary)
    try:
        conn = dbmod._connect(db_path) if Path(db_path).is_file() else dbmod.init_db(db_path)
        try:
            for injection_id, description in INJECTION_DESCRIPTIONS.items():
                upsert_injection(
                    conn,
                    {
                        "injection_id": injection_id,
                        "description": description,
                        "config_patch_json": json.dumps(injection_patch(injection_id), sort_keys=True),
                    },
                )
        finally:
            conn.close()

        docker_executable = require_docker()
        environment = check_environment_reproduction(
            task_ids, suite_dir, docker_executable=docker_executable
        )
        trace_flags: list[bool] = []
        config_ids = {
            key: str(load_path(path)["config_id"]) for key, path in configs.items()
        }
        reports: list[str] = []
        alarms: dict[str, str] = {}

        def execute_config(config_key: str) -> None:
            summary["runs"][config_key] = []
            for task_id in task_ids:
                _assert_cost_gate(db_path)
                before_containers = _mini_container_ids(docker_executable)
                try:
                    result = run_fn(
                        suite_dir / task_id,
                        configs[config_key],
                        db_path=db_path,
                        artifacts_dir=artifacts_dir,
                    )
                finally:
                    _cleanup_new_mini_containers(docker_executable, before_containers)
                summary["runs"][config_key].append(result)
                trace_flags.append(bool(result.get("trace_complete")))
                if result.get("status") == "harness_error":
                    raise RuntimeError(
                        f"selftest run failed: {config_key}/{task_id}: {result.get('error')}"
                    )
                conn = dbmod._connect(db_path)
                try:
                    verify_fn(conn, result["run_id"], suite_dir / task_id)
                finally:
                    conn.close()
                summary["cost_usd"] = _assert_cost_gate(db_path)
                _write_summary(output_dir, summary)

        def compare_injection(injection_id: str) -> None:
            baseline_key = "nonthinking" if injection_id == "I4Q" else "thinking"
            comparison = compare_configs(
                config_ids[baseline_key],
                config_ids[injection_id],
                task_set_path,
                db_path=db_path,
            )
            summary["comparisons"][injection_id] = {
                **comparison,
                "baseline_kind": baseline_key,
            }
            alarms[injection_id] = comparison["alarm"]
            report_path = output_dir / f"{injection_id.lower()}-comparison.md"
            generate_report(
                comparison["comparison_id"], db_path=db_path, output_path=report_path
            )
            reports.append(str(report_path))
            if comparison["alarm"] not in {"hard", "warn"}:
                classification = classify_no_alarm(injection_id)
                summary["stop_classification"] = classification
                if classification["kind"] == "probabilistic_instrument_none":
                    raise RuntimeError(
                        f"STOP: {injection_id} triggered no alarm; register "
                        f"{classification['finding']} and seek approval"
                    )
                raise RuntimeError(
                    f"STOP: deterministic {injection_id} triggered no alarm; investigate pipeline bug"
                )

        execute_config("thinking")
        for injection_id in ("I1Q", "I2P", "I3Q", "I5P"):
            execute_config(injection_id)
            if injection_id == "I3Q":
                conn = dbmod._connect(db_path)
                try:
                    passing_tasks = []
                    for result in summary["runs"]["I3Q"]:
                        rows = conn.execute(
                            "SELECT passed FROM verdict WHERE run_id=? AND track='rule'",
                            (result["run_id"],),
                        ).fetchall()
                        if rows and all(row["passed"] == 1 for row in rows):
                            passing_tasks.append(
                                conn.execute(
                                    "SELECT task_id FROM run WHERE run_id=?", (result["run_id"],)
                                ).fetchone()[0]
                            )
                finally:
                    conn.close()
                summary["i3_verifier_isolation"] = {
                    "unit_test": "tests/test_injections.py::test_i3q_agent_ro_verifier_rw",
                    "agent_mount": "/testbed:ro",
                    "verifier_mount": "/testbed (rw; no :ro suffix)",
                    "passing_tasks": passing_tasks,
                }
            compare_injection(injection_id)
            _write_summary(output_dir, summary)
        execute_config("nonthinking")
        execute_config("I4Q")
        compare_injection("I4Q")

        gates = evaluate_gates(
            trace_complete=trace_flags,
            environment=environment,
            alarms=alarms,
            reports=reports,
        )
        summary.update(gates)
        summary["status"] = "passed" if gates["passed"] else "failed"
        summary["cost_usd"] = cumulative_cost_usd(db_path)
    except Exception as exc:
        summary["status"] = "stopped"
        summary["error"] = str(exc)
        summary["cost_usd"] = cumulative_cost_usd(db_path)
    _write_summary(output_dir, summary)
    return summary
