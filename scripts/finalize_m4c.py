"""Bind remote official reports to local M4-C runs and compare with M3.

The candidate database never leaves the local machine.  This finalizer imports
only the selected completed run IDs into a private copy of the baseline DB.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from traceverdict.compare import compare_configs
from traceverdict.report import generate_report
from traceverdict.swebench_adapter import record_official_verdict
from traceverdict.tracer import db as dbmod


CANDIDATE_CONFIG = "m4c-codex-0-144-4-gpt-5-6-luna-high-subscription-v2"
BASELINE_CONFIG = "dev-deepseek-v4-flash-v2"


def _task_ids(path: Path) -> list[str]:
    ids = [line.strip() for line in path.read_text("utf-8").splitlines() if line.strip()]
    if len(ids) != 16 or len(set(ids)) != 16:
        raise ValueError("M4-C task set must contain exactly 16 unique IDs")
    return ids


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _bind_official_reports(candidate_db: Path, official_root: Path) -> list[dict[str, Any]]:
    summary = json.loads(
        (official_root / "m4c_remote_judge_summary.json").read_text("utf-8")
    )
    rows = summary.get("rows", [])
    if len(rows) != 16:
        raise ValueError(f"expected 16 official rows, got {len(rows)}")
    conn = _connect(candidate_db)
    try:
        for row in rows:
            raw = official_root / row["raw_report"] if row["raw_report"] else None
            aggregate = official_root / row["aggregate_report"]
            outcome = record_official_verdict(
                conn,
                run_id=row["run_id"],
                instance_id=row["task_id"],
                raw_report_path=raw,
                aggregate_report_path=aggregate,
            )
            if not outcome.agreed or outcome.aggregate_resolved != row["aggregate_resolved"]:
                raise RuntimeError(f"official outcome drift for {row['task_id']}")
    finally:
        conn.close()
    return rows


def _assert_task_identity(
    baseline: sqlite3.Connection,
    candidate: sqlite3.Connection,
    task_ids: list[str],
) -> None:
    identity = ("base_commit", "instruction", "budget_json", "gt_type", "gt_spec_json")
    for task_id in task_ids:
        left = baseline.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        right = candidate.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        if left is None or right is None:
            raise ValueError(f"missing task identity for {task_id}")
        mismatches = [field for field in identity if left[field] != right[field]]
        if mismatches:
            raise ValueError(f"task identity mismatch for {task_id}: {mismatches}")


def _merge_candidate(
    *, baseline_db: Path, candidate_db: Path, output_db: Path, rows: list[dict[str, Any]], task_ids: list[str]
) -> list[str]:
    shutil.copy2(baseline_db, output_db)
    output = _connect(output_db)
    candidate = _connect(candidate_db)
    run_ids = [row["run_id"] for row in rows]
    try:
        _assert_task_identity(output, candidate, task_ids)
        config = candidate.execute(
            "SELECT * FROM config WHERE config_id=?", (CANDIDATE_CONFIG,)
        ).fetchone()
        if config is None:
            raise ValueError("candidate config is missing")
        output.execute(
            "INSERT INTO config VALUES (?,?,?,?,?,?,?,?)",
            tuple(config),
        )
        for run_id in run_ids:
            run = candidate.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] != "ok" or run["config_id"] != CANDIDATE_CONFIG:
                raise ValueError(f"invalid selected candidate run {run_id}")
            output.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(run))
            for event in candidate.execute(
                "SELECT run_id,step_idx,ts,etype,payload_json,tokens_in,tokens_out,latency_ms "
                "FROM event WHERE run_id=? ORDER BY event_id",
                (run_id,),
            ):
                output.execute(
                    "INSERT INTO event(run_id,step_idx,ts,etype,payload_json,tokens_in,tokens_out,latency_ms) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    tuple(event),
                )
            for table in ("artifact", "verdict"):
                source_rows = candidate.execute(
                    f"SELECT * FROM {table} WHERE run_id=?", (run_id,)
                ).fetchall()
                placeholders = ",".join("?" for _ in source_rows[0]) if source_rows else ""
                for source in source_rows:
                    output.execute(f"INSERT INTO {table} VALUES ({placeholders})", tuple(source))
        output.commit()
    finally:
        candidate.close()
        output.close()
    return run_ids


def _shadow_costs(candidate_db: Path, run_ids: list[str]) -> dict[str, Any]:
    conn = _connect(candidate_db)
    rows = []
    try:
        for run_id in run_ids:
            payloads = conn.execute(
                "SELECT payload_json FROM event WHERE run_id=? AND etype='llm_call'",
                (run_id,),
            ).fetchall()
            amount = 0.0
            classifications = set()
            missing = set()
            for payload_row in payloads:
                payload = json.loads(payload_row["payload_json"])
                shadow = payload.get("api_equivalent_shadow_cost") or {}
                amount += float(shadow.get("amount_usd") or 0.0)
                if shadow.get("classification"):
                    classifications.add(shadow["classification"])
                missing.update(shadow.get("missing_components") or [])
            rows.append(
                {
                    "run_id": run_id,
                    "amount_usd": amount,
                    "classification": sorted(classifications),
                    "missing_components": sorted(missing),
                }
            )
    finally:
        conn.close()
    return {
        "actual_subscription_increment_usd": 0.0,
        "enters_real_spend_tripwire": False,
        "api_equivalent_shadow_total_usd": sum(row["amount_usd"] for row in rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--baseline-db", type=Path, required=True)
    parser.add_argument("--official-root", type=Path, required=True)
    parser.add_argument("--task-set", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ids = _task_ids(args.task_set)
    official_rows = _bind_official_reports(args.candidate_db, args.official_root)
    if {row["task_id"] for row in official_rows} != set(ids):
        raise ValueError("official report task set differs from frozen task set")
    run_ids = _merge_candidate(
        baseline_db=args.baseline_db,
        candidate_db=args.candidate_db,
        output_db=args.output_db,
        rows=official_rows,
        task_ids=ids,
    )
    comparison = compare_configs(
        BASELINE_CONFIG,
        CANDIDATE_CONFIG,
        args.task_set,
        db_path=args.output_db,
        allow_asymmetric_repetitions=True,
        allow_unpriced_candidate=True,
    )
    report = generate_report(
        comparison["comparison_id"],
        db_path=args.output_db,
        output_path=args.output_dir / "m4c_comparison.md",
    )
    final = {
        "candidate_config": CANDIDATE_CONFIG,
        "baseline_config": BASELINE_CONFIG,
        "candidate_run_ids": run_ids,
        "official_rows": official_rows,
        "resolved": sum(bool(row["aggregate_resolved"]) for row in official_rows),
        "raw_aggregate_disagreements": sum(
            row["raw_resolved"] != row["aggregate_resolved"] for row in official_rows
        ),
        "comparison": comparison,
        "report": report["output"],
        "shadow_cost": _shadow_costs(args.candidate_db, run_ids),
    }
    (args.output_dir / "m4c_final_summary.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
