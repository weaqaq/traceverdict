#!/usr/bin/env python3
"""Prepare and run the frozen five-instance T5 pilot on the Linux server.

This is a repository-external operations entry point, not an eighth TraceVerdict
CLI command.  API credentials must already be present in the process
environment; this script never reads a external process environment file or prints environment values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.core.runner import run_task
from traceverdict.core.task_loader import load_task
from traceverdict.adapters.mini_swe_agent import _build_mini_config
from traceverdict.swebench_budget import (
    AGENT_TOOL_TIMEOUT_S,
    SWEBV_BUDGET_SEMANTICS,
    SWEBV_TASK_BUDGET,
    assert_frozen_budget_bytes,
    expected_mini_agent_limits,
    frozen_budget_block_sha256,
)
from traceverdict.swebench_adapter import (
    DATASET_REVISION,
    MAX_WORKERS,
    SWEBENCH_VERSION,
    acquire_official_image,
    assert_three_way_agreement,
    load_verified_instances,
    materialize_task,
    record_official_verdict,
    run_official_evaluation,
)
from traceverdict.tracer.db import _connect

MODEL_ENV_BY_PROVIDER = {
    "openai": ("OPENAI_API_KEY", "OPENAI_API_BASE"),
    "xiaomi_mimo": ("XIAOMI_MIMO_API_KEY",),
}
MAX_DISK_BYTES = 20 * 1024**3
UNIQUE_RUNS = 104
GROSS_RUNS = 152
CUMULATIVE_TRIPWIRE_USD = 28.0


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _task_set(path: Path) -> list[str]:
    raw = path.read_bytes()
    values = [line.strip() for line in raw.decode("utf-8").splitlines()]
    if (
        any(not value for value in values)
        or len(values) != 16
        or len(set(values)) != 16
    ):
        raise ValueError("frozen task set must contain 16 unique non-empty IDs")
    return values


def _required_model_env(config: dict[str, Any]) -> tuple[str, ...]:
    provider = str(config["model_name"]).split("/", 1)[0]
    try:
        return MODEL_ENV_BY_PROVIDER[provider]
    except KeyError as exc:
        raise RuntimeError(f"unsupported paid-run provider: {provider}") from exc


def _require_credentials(config: dict[str, Any]) -> None:
    required = _required_model_env(config)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing process-only model environment names: {missing}")


def _docker_root() -> Path:
    proc = subprocess.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(proc.stdout.strip())


class MemorySampler:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.is_set():
            values = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0]) * 1024
            self.peak_bytes = max(
                self.peak_bytes, values["MemTotal"] - values["MemAvailable"]
            )
            self._stop.wait(2)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _selected_ids(args) -> list[str]:
    ids = _task_set(args.task_set)
    return ids if getattr(args, "all", False) else ids[:5]


def _assert_budget_identity(
    *, ids: list[str], config: dict[str, Any], output: Path
) -> dict[str, Any]:
    """Fail before paid work if task bytes or generated mini limits drift."""
    expected_limits = expected_mini_agent_limits()
    records = []
    for instance_id in ids:
        task_dir = output / "tasks" / instance_id
        task_yaml_path = task_dir / "task.yaml"
        block_sha = assert_frozen_budget_bytes(task_yaml_path.read_bytes())
        task = load_task(task_dir)
        if task["budget"] != SWEBV_TASK_BUDGET:
            raise RuntimeError(f"SWE-bench parsed budget drift: {instance_id}")
        generated = _build_mini_config(
            image=task["image_ref"],
            docker_executable="docker",
            host_work_path=task_dir,
            container_cwd=config["container_cwd"],
            model_name=config["model_name"],
            model_params=config["model_params"],
            litellm_model_registry=config["litellm_model_registry"],
            output_path=output / "budget-guard.traj.json",
            cost_limit=float(task["budget"]["max_cost_usd"]),
            step_limit=int(task["budget"]["max_steps"]),
            wall_time_s=int(task["budget"]["max_wall_s"]),
        )
        actual_limits = {
            key: generated["agent"].get(key) for key in expected_limits
        }
        if actual_limits != expected_limits:
            raise RuntimeError(
                f"SWE-bench generated mini limit drift for {instance_id}: "
                f"{actual_limits!r} != {expected_limits!r}"
            )
        if "max_tokens" in generated["agent"]:
            raise RuntimeError("D1-i drift: max_tokens unexpectedly became enforced")
        tool_timeout = generated["environment"].get("timeout")
        if tool_timeout != AGENT_TOOL_TIMEOUT_S:
            raise RuntimeError(
                f"SWE-bench tool timeout drift for {instance_id}: {tool_timeout}"
            )
        records.append(
            {
                "instance_id": instance_id,
                "budget_block_sha256": block_sha,
                "mini_agent_limits": actual_limits,
                "agent_tool_timeout_s": tool_timeout,
                "max_tokens_enforcement": "none",
            }
        )
    evidence = {
        "config_id": config["config_id"],
        "budget": dict(SWEBV_TASK_BUDGET),
        "budget_block_sha256": frozen_budget_block_sha256(),
        "semantics": SWEBV_BUDGET_SEMANTICS,
        "records": records,
    }
    _json_dump(output / "budget_identity.json", evidence)
    return evidence


def prepare(args) -> None:
    ids = _selected_ids(args)
    rows = load_verified_instances(ids)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    records_path = output / "image_records.json"
    existing = (
        json.loads(records_path.read_text(encoding="utf-8"))
        if records_path.is_file()
        else []
    )
    records_by_id = {item["instance_id"]: item for item in existing}
    docker_root = _docker_root()
    for row in rows:
        instance_id = row["instance_id"]
        task_dir = output / "tasks" / instance_id
        if instance_id in records_by_id and (task_dir / "task.yaml").is_file():
            continue
        before = shutil_disk_used(docker_root)
        with MemorySampler() as memory:
            image = acquire_official_image(
                row, python_executable=sys.executable, docker_executable="docker"
            )
            materialize_task(row, image_ref=image.image_ref, output_dir=task_dir)
        disk_delta = max(0, shutil_disk_used(docker_root) - before)
        if disk_delta > MAX_DISK_BYTES:
            raise RuntimeError(f"stop: {instance_id} image delta exceeds 20 GiB")
        records_by_id[instance_id] = {
            "instance_id": instance_id,
            "image_ref": image.image_ref,
            "image_digest": image.digest,
            "image_path": image.path,
            "elapsed_s": image.elapsed_s,
            "disk_delta_bytes": disk_delta,
            "network_path": (
                "official_prebuilt"
                if image.path == "pull"
                else "official_prepare_images"
            ),
            "peak_host_memory_bytes": memory.peak_bytes,
        }
        ordered = [
            records_by_id[item]
            for item in _task_set(args.task_set)
            if item in records_by_id
        ]
        _json_dump(records_path, ordered)


def shutil_disk_used(path: Path) -> int:
    import shutil

    usage = shutil.disk_usage(path)
    return usage.total - usage.free


def _artifact_patch(conn: sqlite3.Connection, run_id: str) -> Path:
    row = conn.execute(
        "SELECT path FROM artifact WHERE run_id=? AND kind='patch'", (run_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"run {run_id} has no authoritative patch artifact")
    return Path(row["path"])


def run_one(args) -> None:
    config = load_config(args.config)
    _assert_budget_identity(
        ids=[args.instance_id], config=config, output=args.output
    )
    _require_credentials(config)
    records = json.loads(
        (args.output / "image_records.json").read_text(encoding="utf-8")
    )
    record = next(item for item in records if item["instance_id"] == args.instance_id)
    task_dir = args.output / "tasks" / args.instance_id
    result = run_task(
        task_dir,
        args.config,
        db_path=args.db,
        artifacts_dir=args.output / "artifacts",
        repetition_idx=int(getattr(args, "repetition_idx", 0)),
    )
    if result.get("status") == "harness_error":
        raise RuntimeError(f"agent harness error: {result.get('error')}")
    conn = _connect(args.db)
    try:
        patch_path = _artifact_patch(conn, result["run_id"])
        official_dir = args.output / "official" / result["run_id"]
        raw, aggregate = run_official_evaluation(
            python_executable=sys.executable,
            instance_id=args.instance_id,
            patch_text=patch_path.read_text(encoding="utf-8"),
            output_dir=official_dir,
            official_run_id=f"t5-{result['run_id']}",
            model_name_or_path=f"traceverdict__{config['config_id']}",
            image_path=record["image_path"],
        )
        outcome = record_official_verdict(
            conn,
            run_id=result["run_id"],
            instance_id=args.instance_id,
            raw_report_path=raw,
            aggregate_report_path=aggregate,
        )
        verdict = conn.execute(
            "SELECT passed FROM verdict WHERE run_id=? AND name='swebench'",
            (result["run_id"],),
        ).fetchone()
        assert_three_way_agreement(
            traceverdict_passed=bool(verdict["passed"]),
            raw_resolved=outcome.raw_resolved,
            aggregate_resolved=outcome.aggregate_resolved,
        )
    finally:
        conn.close()
    result["official_raw_resolved"] = outcome.raw_resolved
    result["official_aggregate_resolved"] = outcome.aggregate_resolved
    result["traceverdict_verdict"] = bool(verdict["passed"])
    result["image_path"] = record["image_path"]
    completion_path = getattr(args, "completion_path", None)
    if completion_path is None:
        completion_path = args.output / "runs" / f"{args.instance_id}.json"
    _json_dump(Path(completion_path), result)
    print(json.dumps(result, indent=2))


def _existing_config_runs(
    db_path: Path, *, instance_id: str, config_id: str
) -> list[sqlite3.Row]:
    if not db_path.is_file():
        return []
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT run_id, status, cost_usd FROM run "
            "WHERE task_id=? AND config_id=? ORDER BY started_at DESC",
            (instance_id, config_id),
        ).fetchall()
    finally:
        conn.close()


def _cumulative_cost(db_path: Path) -> float:
    if not db_path.is_file():
        return 0.0
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM run"
        ).fetchone()
        return float(row["total"])
    finally:
        conn.close()


def run_all(args) -> None:
    """Run the frozen M2 set sequentially, resuming only completed instances.

    A database row without the per-instance completion record is deliberately
    not retried: it may represent a paid call whose judging step was interrupted.
    An operator must reconcile that evidence before another paid attempt.
    """

    config = load_config(args.config)
    ids = _task_set(args.task_set)
    _assert_budget_identity(ids=ids, config=config, output=args.output)
    _require_credentials(config)
    image_records = json.loads(
        (args.output / "image_records.json").read_text(encoding="utf-8")
    )
    prepared = {item["instance_id"] for item in image_records}
    missing_images = [instance_id for instance_id in ids if instance_id not in prepared]
    if missing_images:
        raise RuntimeError(f"M2 images incomplete: {missing_images}")

    for instance_id in ids:
        completion = args.output / "runs" / f"{instance_id}.json"
        if completion.is_file():
            continue
        prior = _existing_config_runs(
            args.db, instance_id=instance_id, config_id=config["config_id"]
        )
        if prior:
            run_ids = [row["run_id"] for row in prior]
            raise RuntimeError(
                f"paid-run guard: {instance_id} has DB runs {run_ids} but no "
                "completion record; reconcile before retry"
            )
        if _cumulative_cost(args.db) >= CUMULATIVE_TRIPWIRE_USD:
            raise RuntimeError("$28 cumulative tripwire reached before paid run")
        run_one(
            Namespace(
                task_set=args.task_set,
                config=args.config,
                db=args.db,
                output=args.output,
                instance_id=instance_id,
            )
        )
        if _cumulative_cost(args.db) >= CUMULATIVE_TRIPWIRE_USD:
            raise RuntimeError("$28 cumulative tripwire reached after paid run")

    verify_runs(Namespace(task_set=args.task_set, output=args.output, all=True))


def verify_runs(args) -> None:
    ids = _selected_ids(args)
    results = []
    for instance_id in ids:
        path = args.output / "runs" / f"{instance_id}.json"
        if not path.is_file():
            raise RuntimeError(f"M2 incomplete: missing {instance_id}")
        item = json.loads(path.read_text(encoding="utf-8"))
        if (
            len(
                {
                    bool(item["traceverdict_verdict"]),
                    bool(item["official_raw_resolved"]),
                    bool(item["official_aggregate_resolved"]),
                }
            )
            != 1
        ):
            raise RuntimeError(f"M2 stop: three-way mismatch for {instance_id}")
        results.append(item)
    summary_name = (
        "m2_summary.json" if getattr(args, "all", False) else "pilot_summary.json"
    )
    _json_dump(
        args.output / summary_name,
        {
            "dataset_revision": DATASET_REVISION,
            "swebench_version": SWEBENCH_VERSION,
            "task_set_sha256": hashlib.sha256(args.task_set.read_bytes()).hexdigest(),
            "count": len(results),
            "agreement": 1.0,
            "max_workers": MAX_WORKERS,
            "runs": results,
        },
    )


def cost_projection(
    *, probe_costs: list[float], pilot_actual: float, historical_actual: float
) -> dict[str, float | int]:
    if len(probe_costs) != 3 or any(cost <= 0 for cost in probe_costs):
        raise ValueError("exactly three positive probe costs are required")
    conservative_per_run = max(probe_costs)
    unique_total = (
        historical_actual + pilot_actual + conservative_per_run * (UNIQUE_RUNS - 5)
    )
    gross_total = (
        historical_actual + pilot_actual + conservative_per_run * (GROSS_RUNS - 5)
    )
    return {
        "historical_actual_usd": historical_actual,
        "pilot_actual_usd": pilot_actual,
        "conservative_per_run_usd": conservative_per_run,
        "unique_runs": UNIQUE_RUNS,
        "unique_projected_cumulative_usd": unique_total,
        "gross_runs": GROSS_RUNS,
        "gross_projected_cumulative_usd": gross_total,
        "tripwire_usd": CUMULATIVE_TRIPWIRE_USD,
        "unique_within_tripwire": unique_total < CUMULATIVE_TRIPWIRE_USD,
        "gross_within_tripwire": gross_total < CUMULATIVE_TRIPWIRE_USD,
    }


def project_cost(args) -> None:
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    probe_ids = metadata["cost_probe_ids"]
    pilot_ids = _task_set(args.task_set)[:5]
    conn = _connect(args.db)
    try:
        costs = {}
        for instance_id in pilot_ids:
            rows = conn.execute(
                "SELECT r.cost_usd FROM run r WHERE r.task_id=? AND r.cost_usd IS NOT NULL "
                "ORDER BY r.started_at DESC",
                (instance_id,),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    f"expected exactly one paid pilot run for {instance_id}, got {len(rows)}"
                )
            costs[instance_id] = float(rows[0]["cost_usd"])
    finally:
        conn.close()
    result = cost_projection(
        probe_costs=[costs[instance_id] for instance_id in probe_ids],
        pilot_actual=sum(costs.values()),
        historical_actual=args.historical_actual,
    )
    result["probe_ids"] = probe_ids
    result["pilot_costs_usd"] = costs
    _json_dump(args.output / "cost_projection.json", result)
    if not result["unique_within_tripwire"]:
        raise RuntimeError("$28 tripwire reached by 104-run projection")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument(
        "--task-set", type=Path, default=Path("benchmarks/swebv_subset_v1.txt")
    )
    root.add_argument("--config", type=Path, default=Path("configs/dev.yaml"))
    root.add_argument("--db", type=Path, default=Path("reports/t5/traceverdict.db"))
    root.add_argument("--output", type=Path, default=Path("reports/t5"))
    sub = root.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--all", action="store_true")
    prepare_parser.set_defaults(func=prepare)
    one = sub.add_parser("run-one")
    one.add_argument("instance_id")
    one.add_argument("--repetition-idx", type=int, default=0)
    one.set_defaults(func=run_one)
    sub.add_parser("run-all").set_defaults(func=run_all)
    sub.add_parser("verify-pilot").set_defaults(func=verify_runs, all=False)
    sub.add_parser("verify-all").set_defaults(func=verify_runs, all=True)
    cost = sub.add_parser("project-cost")
    cost.add_argument(
        "--metadata",
        type=Path,
        default=Path("benchmarks/swebv_subset_v1.meta.json"),
    )
    cost.add_argument("--historical-actual", type=float, required=True)
    cost.set_defaults(func=project_cost)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.func(arguments)
