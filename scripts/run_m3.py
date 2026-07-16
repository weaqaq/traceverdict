#!/usr/bin/env python3
"""M3 orchestration with paid-run idempotency and explicit reuse evidence."""

from __future__ import annotations

import argparse
import json
from argparse import Namespace
from pathlib import Path

import run_swebv_pilot as pilot

from traceverdict.core.config_loader import load_config
from traceverdict.m3 import (
    I3Q_CONFIG_ID,
    assert_below_tripwire,
    assert_no_existing_repetition,
    assert_reuse_identity,
    alpha_run_ids_from_completions,
    completion_path,
    corrected_projection,
    cumulative_project_cost,
    materialize_m3_inputs,
    record_i3q_isolation,
    seed_m3_database,
)


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")


def seed(args) -> None:
    identity = assert_reuse_identity(
        task_set=args.task_set,
        m2_output=args.m2_output,
        mimo_output=args.mimo_output,
        beta_config=args.beta_config,
    )
    alpha_run_ids = alpha_run_ids_from_completions(
        task_set=args.task_set, m2_output=args.m2_output, m2_db=args.m2_db
    )
    seed_m3_database(
        m2_db=args.m2_db,
        mimo_db=args.mimo_db,
        target_db=args.db,
        alpha_run_ids=alpha_run_ids,
    )
    identity["alpha_run_ids"] = list(alpha_run_ids)
    copied = materialize_m3_inputs(m2_output=args.m2_output, output=args.output)
    identity["materialized_inputs"] = copied
    _dump(args.output / "reuse_manifest.json", identity)
    _dump(args.output / "cost_projection.json", corrected_projection())
    print(json.dumps(identity, indent=2))


def run_one(args) -> None:
    cfg = load_config(args.config)
    task_ids = [line.strip() for line in args.task_set.read_text("utf-8").splitlines()]
    if args.instance_id not in task_ids:
        raise RuntimeError(f"task not in frozen M3 set: {args.instance_id}")
    before = assert_below_tripwire(args.db)
    assert_no_existing_repetition(
        args.db,
        task_id=args.instance_id,
        config_id=cfg["config_id"],
        repetition_idx=args.repetition_idx,
    )
    dest = completion_path(
        args.output,
        config_id=cfg["config_id"],
        repetition_idx=args.repetition_idx,
        task_id=args.instance_id,
    )
    if dest.exists():
        raise RuntimeError(f"completion exists without DB repetition: {dest}")
    pilot.run_one(
        Namespace(
            task_set=args.task_set,
            config=args.config,
            db=args.db,
            output=args.output,
            instance_id=args.instance_id,
            repetition_idx=args.repetition_idx,
            completion_path=dest,
        )
    )
    result = json.loads(dest.read_text("utf-8"))
    if cfg["config_id"] == I3Q_CONFIG_ID:
        result["i3q_isolation_path"] = str(
            record_i3q_isolation(
                output=args.output,
                db_path=args.db,
                run_id=result["run_id"],
                task_id=args.instance_id,
            )
        )
        _dump(dest, result)
    after = cumulative_project_cost(args.db)
    if after >= 28:
        raise RuntimeError(f"$28 cumulative tripwire reached after run: {after}")
    print(json.dumps({"before_usd": str(before), "after_usd": str(after), **result}, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--task-set", type=Path, default=Path("benchmarks/swebv_subset_v1.txt"))
    root.add_argument("--db", type=Path, default=Path("reports/m3/traceverdict.db"))
    root.add_argument("--output", type=Path, default=Path("reports/m3"))
    sub = root.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--m2-db", type=Path, required=True)
    s.add_argument("--mimo-db", type=Path, required=True)
    s.add_argument("--m2-output", type=Path, required=True)
    s.add_argument("--mimo-output", type=Path, required=True)
    s.add_argument("--beta-config", type=Path, default=Path("configs/mimo_v2_5_probe_v3.yaml"))
    s.set_defaults(func=seed)
    r = sub.add_parser("run-one")
    r.add_argument("instance_id")
    r.add_argument("--config", type=Path, required=True)
    r.add_argument("--repetition-idx", type=int, choices=(0, 1), required=True)
    r.set_defaults(func=run_one)
    return root


if __name__ == "__main__":
    args = parser().parse_args()
    args.func(args)
