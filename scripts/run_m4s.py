#!/usr/bin/env python3
"""Run the paid M4-S arm without modifying the sealed M3 finalizer."""

from __future__ import annotations

import argparse
import json
import shutil
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import run_swebv_pilot as pilot

from traceverdict.compare import compare_configs
from traceverdict.core.config_loader import load_config
from traceverdict.m3 import (
    assert_below_tripwire,
    assert_no_existing_repetition,
    cumulative_project_cost,
)
from traceverdict.m4s import (
    BASELINE_CONFIG_ID,
    CONFIG_ID,
    POSITIONING,
    PROBE_TASKS,
    PROJECTED_CEILING_USD,
    assert_probe_reuse_identity,
    choose_formal_count,
    load_frozen_task_set,
)
from traceverdict.report import generate_report
from traceverdict.tracer import db as dbmod


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")


def _completion(output: Path, task_id: str) -> Path:
    return output / "runs" / CONFIG_ID / "r0" / f"{task_id}.json"


def seed(args) -> None:
    if args.db.exists() or args.output.exists():
        raise RuntimeError("M4-S target DB/output already exists; refusing to overwrite")
    args.output.mkdir(parents=True)
    shutil.copy2(args.m3_db, args.db)
    shutil.copy2(args.m3_output / "image_records.json", args.output / "image_records.json")
    shutil.copytree(args.m3_output / "tasks", args.output / "tasks")
    _dump(
        args.output / "seed_manifest.json",
        {
            "m3_db": str(args.m3_db),
            "m3_output": str(args.m3_output),
            "historical_actual_usd": str(cumulative_project_cost(args.db)),
            "config_id": CONFIG_ID,
        },
    )


def _run_one(args, task_id: str) -> dict:
    cfg = load_config(args.config)
    if cfg["config_id"] != CONFIG_ID:
        raise RuntimeError(f"M4-S config identity drift: {cfg['config_id']!r}")
    tasks = load_frozen_task_set(args.task_set, expected_count=16)
    if task_id not in tasks:
        raise RuntimeError(f"task is outside frozen M4-S set: {task_id}")
    before = assert_below_tripwire(args.db)
    assert_no_existing_repetition(
        args.db, task_id=task_id, config_id=CONFIG_ID, repetition_idx=0
    )
    destination = _completion(args.output, task_id)
    if destination.exists():
        raise RuntimeError(f"completion exists without matching DB guard: {destination}")
    pilot.run_one(
        Namespace(
            task_set=args.task_set,
            config=args.config,
            db=args.db,
            output=args.output,
            instance_id=task_id,
            repetition_idx=0,
            completion_path=destination,
        )
    )
    after = cumulative_project_cost(args.db)
    if after >= Decimal("28"):
        raise RuntimeError(f"$28 tripwire reached after paid run: {after}")
    result = json.loads(destination.read_text("utf-8"))
    result["project_cost_before_usd"] = str(before)
    result["project_cost_after_usd"] = str(after)
    _dump(destination, result)
    return result


def probe(args) -> None:
    for task_id in PROBE_TASKS:
        destination = _completion(args.output, task_id)
        if destination.exists():
            continue
        _run_one(args, task_id)
    reuse = assert_probe_reuse_identity(
        db_path=args.db, output=args.output, task_set=args.task_set
    )
    conn = dbmod._connect(args.db)
    try:
        costs = [
            Decimal(
                str(
                    conn.execute(
                        "SELECT cost_usd FROM run WHERE task_id=? AND config_id=? "
                        "AND repetition_idx=0",
                        (task_id, CONFIG_ID),
                    ).fetchone()["cost_usd"]
                )
            )
            for task_id in PROBE_TASKS
        ]
    finally:
        conn.close()
    current = cumulative_project_cost(args.db)
    historical = current - sum(costs, Decimal("0"))
    count, projection = choose_formal_count(
        historical_actual_usd=historical, probe_costs_usd=costs
    )
    projection["probe_reuse"] = reuse
    projection["projected_ceiling_usd"] = str(PROJECTED_CEILING_USD)
    _dump(args.output / "cost_gate.json", projection)
    if count == 0:
        raise RuntimeError("M4-S cost gate failed at 16 and 12 tasks; arm abandoned")
    print(json.dumps(projection, indent=2))


def formal(args) -> None:
    gate = json.loads((args.output / "cost_gate.json").read_text("utf-8"))
    decision = gate["decision"]
    if decision == "full-16":
        count = 16
    elif decision == "reduced-first-12":
        count = 12
    else:
        raise RuntimeError(f"M4-S gate does not authorize formal work: {decision}")
    tasks = load_frozen_task_set(args.task_set, expected_count=16)[:count]
    for task_id in tasks:
        destination = _completion(args.output, task_id)
        if destination.exists():
            continue
        _run_one(args, task_id)
    _dump(
        args.output / "formal_manifest.json",
        {
            "config_id": CONFIG_ID,
            "formal_count": count,
            "tasks": tasks,
            "run_ids": [json.loads(_completion(args.output, t).read_text("utf-8"))["run_id"] for t in tasks],
            "positioning": POSITIONING,
            "cumulative_project_cost_usd": str(cumulative_project_cost(args.db)),
        },
    )


def finalize(args) -> None:
    manifest = json.loads((args.output / "formal_manifest.json").read_text("utf-8"))
    count = int(manifest["formal_count"])
    comparison_task_set = args.task_set if count == 16 else args.first12_task_set
    result = compare_configs(
        BASELINE_CONFIG_ID,
        CONFIG_ID,
        comparison_task_set,
        db_path=args.db,
        allow_asymmetric_repetitions=True,
    )
    report = generate_report(
        result["comparison_id"],
        db_path=args.db,
        output_path=args.output / "m4s_flash_vs_pro.md",
    )
    summary = {
        "positioning": POSITIONING,
        "config_id": CONFIG_ID,
        "baseline_config_id": BASELINE_CONFIG_ID,
        "formal": manifest,
        "cost_gate": json.loads((args.output / "cost_gate.json").read_text("utf-8")),
        "comparison_id": result["comparison_id"],
        "comparison": result,
        "report": report["output"],
    }
    _dump(args.output / "m4s_summary.json", summary)
    print(json.dumps(summary, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--task-set", type=Path, default=Path("benchmarks/swebv_subset_v1.txt"))
    root.add_argument(
        "--first12-task-set",
        type=Path,
        default=Path("benchmarks/swebv_subset_v1_first12.txt"),
    )
    root.add_argument("--config", type=Path, default=Path("configs/m4s_deepseek_v4_pro_v1.yaml"))
    root.add_argument("--db", type=Path, default=Path("reports/m4s/traceverdict.db"))
    root.add_argument("--output", type=Path, default=Path("reports/m4s"))
    sub = root.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--m3-db", type=Path, required=True)
    s.add_argument("--m3-output", type=Path, required=True)
    s.set_defaults(func=seed)
    p = sub.add_parser("probe")
    p.set_defaults(func=probe)
    f = sub.add_parser("formal")
    f.set_defaults(func=formal)
    z = sub.add_parser("finalize")
    z.set_defaults(func=finalize)
    return root


if __name__ == "__main__":
    args = parser().parse_args()
    args.func(args)
