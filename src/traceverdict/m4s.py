"""M4-S experiment identity, cost gate, and reuse helpers."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from traceverdict.swebench_budget import assert_frozen_budget_bytes
from traceverdict.tracer import db as dbmod

CONFIG_ID = "m4s-mini-2-4-5-deepseek-v4-pro-thinking-v1"
BASELINE_CONFIG_ID = "dev-deepseek-v4-flash-v2"
PROBE_TASKS = (
    "pytest-dev__pytest-7982",
    "sympy__sympy-20438",
    "pydata__xarray-7229",
)
TRIPWIRE_USD = Decimal("28")
REQUIRED_RESERVE_USD = Decimal("3")
PROJECTED_CEILING_USD = TRIPWIRE_USD - REQUIRED_RESERVE_USD
FULL_COUNT = 16
REDUCED_COUNT = 12
POSITIONING = "同厂跨档对比，回答 harness 结论是否随被试能力档位稳健。"


def load_frozen_task_set(path: Path, *, expected_count: int | None = None) -> list[str]:
    values = [line.strip() for line in path.read_text("utf-8").splitlines()]
    if not values or any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError("task set must contain unique non-empty IDs")
    if expected_count is not None and len(values) != expected_count:
        raise ValueError(f"expected {expected_count} frozen tasks, got {len(values)}")
    return values


def cost_projection(
    *, historical_actual_usd: Decimal, probe_costs_usd: list[Decimal], formal_count: int
) -> dict[str, Any]:
    if len(probe_costs_usd) != len(PROBE_TASKS) or any(v <= 0 for v in probe_costs_usd):
        raise ValueError("exactly three positive probe costs are required")
    if formal_count not in (FULL_COUNT, REDUCED_COUNT):
        raise ValueError("formal_count must be 16 or 12")
    probe_actual = sum(probe_costs_usd, Decimal("0"))
    maximum = max(probe_costs_usd)
    conservative = historical_actual_usd + probe_actual + maximum * formal_count
    reuse = historical_actual_usd + probe_actual + maximum * (
        formal_count - len(PROBE_TASKS)
    )
    return {
        "historical_actual_usd": str(historical_actual_usd),
        "probe_actual_usd": str(probe_actual),
        "probe_costs_usd": [str(v) for v in probe_costs_usd],
        "maximum_probe_run_usd": str(maximum),
        "formal_count": formal_count,
        "authorization_run_envelope": formal_count + len(PROBE_TASKS),
        "actual_unique_runs_with_reuse": formal_count,
        "conservative_projected_total_usd": str(conservative),
        "reuse_projected_total_usd": str(reuse),
        "projected_ceiling_usd": str(PROJECTED_CEILING_USD),
        "reserve_at_conservative_projection_usd": str(TRIPWIRE_USD - conservative),
        "approved": conservative <= PROJECTED_CEILING_USD,
    }


def choose_formal_count(
    *, historical_actual_usd: Decimal, probe_costs_usd: list[Decimal]
) -> tuple[int, dict[str, Any]]:
    full = cost_projection(
        historical_actual_usd=historical_actual_usd,
        probe_costs_usd=probe_costs_usd,
        formal_count=FULL_COUNT,
    )
    if full["approved"]:
        return FULL_COUNT, {"full": full, "decision": "full-16"}
    reduced = cost_projection(
        historical_actual_usd=historical_actual_usd,
        probe_costs_usd=probe_costs_usd,
        formal_count=REDUCED_COUNT,
    )
    if reduced["approved"]:
        return REDUCED_COUNT, {
            "full": full,
            "reduced": reduced,
            "decision": "reduced-first-12",
        }
    return 0, {"full": full, "reduced": reduced, "decision": "abandon"}


def assert_probe_reuse_identity(
    *, db_path: Path, output: Path, task_set: Path
) -> dict[str, Any]:
    tasks = load_frozen_task_set(task_set, expected_count=FULL_COUNT)
    if not set(PROBE_TASKS).issubset(tasks):
        raise RuntimeError("frozen probe tasks are absent from the formal set")
    image_records = json.loads((output / "image_records.json").read_text("utf-8"))
    images = {item["instance_id"]: item for item in image_records}
    conn = dbmod._connect(db_path)
    try:
        records = []
        for task_id in PROBE_TASKS:
            completion = output / "runs" / CONFIG_ID / "r0" / f"{task_id}.json"
            value = json.loads(completion.read_text("utf-8"))
            if len(
                {
                    bool(value["traceverdict_verdict"]),
                    bool(value["official_raw_resolved"]),
                    bool(value["official_aggregate_resolved"]),
                }
            ) != 1:
                raise RuntimeError(f"probe three-way verdict mismatch: {task_id}")
            row = conn.execute(
                "SELECT run_id,task_id,config_id,repetition_idx,cost_usd,env_fingerprint "
                "FROM run WHERE run_id=?",
                (value["run_id"],),
            ).fetchone()
            if row is None or (
                row["task_id"], row["config_id"], row["repetition_idx"]
            ) != (task_id, CONFIG_ID, 0):
                raise RuntimeError(f"probe DB identity mismatch: {task_id}")
            budget_sha = assert_frozen_budget_bytes(
                (output / "tasks" / task_id / "task.yaml").read_bytes()
            )
            image = images[task_id]
            records.append(
                {
                    "task_id": task_id,
                    "run_id": row["run_id"],
                    "cost_usd": str(row["cost_usd"]),
                    "env_fingerprint": row["env_fingerprint"],
                    "budget_block_sha256": budget_sha,
                    "image_ref": image["image_ref"],
                    "image_digest": image["image_digest"],
                    "completion_sha256": hashlib.sha256(completion.read_bytes()).hexdigest(),
                }
            )
    finally:
        conn.close()
    return {
        "status": "eligible-for-formal-reuse",
        "config_id": CONFIG_ID,
        "repetition_idx": 0,
        "formal_task_set_sha256": hashlib.sha256(task_set.read_bytes()).hexdigest(),
        "records": records,
    }


def assert_kimi3_stable_identity(model_id: str, *, official_ids: set[str]) -> None:
    lowered = model_id.lower()
    if "preview" in lowered or "latest" in lowered:
        raise RuntimeError("Kimi-3 preview/rolling aliases cannot enter M4-S")
    if model_id not in official_ids:
        raise RuntimeError("Kimi-3 exact model ID is not in the official model list")
