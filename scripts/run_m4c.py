"""Run the local-only M4-C Codex compatibility arm without duplicate completions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from traceverdict.core.config_loader import load_config
from traceverdict.core.runner import run_task
from traceverdict.m4c import append_subscription_window
from traceverdict.verifier import rule_run_passed, verify_run


SELF_TASKS = tuple(f"S{i}" for i in range(1, 9))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _completed_run(conn: sqlite3.Connection, task_id: str, config_id: str):
    rows = conn.execute(
        """
        SELECT * FROM run
        WHERE task_id=? AND config_id=? AND finished_at IS NOT NULL
        ORDER BY started_at
        """,
        (task_id, config_id),
    ).fetchall()
    completed = [row for row in rows if row["tokens_in"] is not None]
    if len(completed) > 1:
        raise RuntimeError(f"duplicate paid completion for {task_id}: {[r['run_id'] for r in completed]}")
    return completed[0] if completed else None


def _rebase_artifact_paths(conn: sqlite3.Connection, artifacts: Path) -> int:
    """Repair artifact locations after the private evidence root is relocated.

    The database remains the audit record, so a path is changed only when the
    expected run-relative file exists under the configured private root and its
    bytes still match the stored SHA256.
    """
    changed = 0
    for row in conn.execute("SELECT artifact_id, run_id, path, sha256 FROM artifact"):
        current = Path(row["path"])
        if current.is_file():
            continue
        candidate = artifacts / row["run_id"] / current.name
        if current.parent.name == "adapter":
            candidate = artifacts / row["run_id"] / "adapter" / current.name
        if not candidate.is_file():
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != row["sha256"]:
            raise RuntimeError(
                f"refusing artifact path repair for {row['artifact_id']}: "
                f"sha256 {actual} != {row['sha256']}"
            )
        conn.execute(
            "UPDATE artifact SET path=? WHERE artifact_id=?",
            (str(candidate.resolve()), row["artifact_id"]),
        )
        changed += 1
    conn.commit()
    return changed


def run_self_gate(
    config: Path, db_path: Path, artifacts: Path, output: Path, ledger_path: Path
) -> dict:
    cfg = load_config(config)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    started = _now()
    rows: list[dict] = []
    new_run_ids: list[str] = []
    subscription_window_recorded = False
    try:
        repaired_artifact_paths = _rebase_artifact_paths(conn, artifacts.resolve())
        if repaired_artifact_paths:
            print(
                json.dumps({"repaired_artifact_paths": repaired_artifact_paths}),
                flush=True,
            )
        for task_id in SELF_TASKS:
            task_path = Path("tasks/self") / task_id
            prior = _completed_run(conn, task_id, cfg["config_id"])
            reused = prior is not None
            if prior is None:
                result = run_task(task_path, config, db_path=db_path, artifacts_dir=artifacts)
                print(json.dumps({"task_id": task_id, "run": result}), flush=True)
                if result.get("status") == "harness_error":
                    if result.get("error") == "SubscriptionLimitExceeded":
                        append_subscription_window(
                            ledger_path,
                            window_started_at=started,
                            window_finished_at=_now(),
                            completed_run_ids=new_run_ids,
                            quota_error="SubscriptionLimitExceeded",
                            pause_reason="window_exhausted",
                        )
                    raise RuntimeError(f"{task_id} stopped: {result.get('error')}")
                run_id = str(result["run_id"])
                new_run_ids.append(run_id)
                if not result.get("trace_complete"):
                    raise RuntimeError(f"{task_id} trace is incomplete")
            else:
                run_id = str(prior["run_id"])
            verdicts = verify_run(conn, run_id, task_path, docker_executable="docker")
            conn.commit()
            rows.append(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "reused": reused,
                    "status": conn.execute(
                        "SELECT status FROM run WHERE run_id=?", (run_id,)
                    ).fetchone()[0],
                    "rule_passed": rule_run_passed(conn, run_id),
                    "verdicts": {row["name"]: bool(row["passed"]) for row in verdicts},
                }
            )
            print(json.dumps(rows[-1]), flush=True)
        if new_run_ids:
            append_subscription_window(
                ledger_path,
                window_started_at=started,
                window_finished_at=_now(),
                completed_run_ids=new_run_ids,
                quota_error=None,
                pause_reason=None,
            )
            subscription_window_recorded = True
    finally:
        conn.close()
    summary = {
        "phase": "self",
        "config_id": cfg["config_id"],
        "started_at": started,
        "finished_at": _now(),
        "repaired_artifact_paths": repaired_artifact_paths,
        "subscription_window_recorded": subscription_window_recorded,
        "adapter_complete": len(rows) == 8 and all(row["status"] != "harness_error" for row in rows),
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run_self_gate(args.config, args.db, args.artifacts, args.output, args.ledger),
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
