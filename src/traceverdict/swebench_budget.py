"""Frozen SWE-bench task-budget identity and operational semantics (D1-i)."""

from __future__ import annotations

import hashlib
from typing import Any

from traceverdict.core.simple_yaml import dumps


SWEBV_TASK_BUDGET: dict[str, int | float] = {
    "max_steps": 100,
    "max_tokens": 250000,
    "max_wall_s": 3600,
    "max_cost_usd": 1.0,
}

# These limits were already active for the paid T5 pilot and are therefore
# part of the experiment identity even though they are adapter mechanics rather
# than columns in task.budget_json.
AGENT_TOOL_TIMEOUT_S = 60
ADAPTER_WALL_TIMEOUT_GRACE_S = 120

SWEBV_BUDGET_SEMANTICS: dict[str, dict[str, Any]] = {
    "max_steps": {
        "status": "enforced",
        "enforcement": "mini_agent_query_boundary",
        "counter": "DefaultAgent.n_calls",
        "definition": (
            "Checked before each model query and incremented immediately before "
            "model.query; at most 100 model queries are admitted."
        ),
    },
    "max_tokens": {
        "status": "recorded-inert",
        "enforcement": "none",
        "counter": None,
        "definition": (
            "Declared in task.budget_json but not passed to mini-swe-agent and "
            "not enforced by TraceVerdict v0.1; it is not a cumulative-token limit."
        ),
    },
    "max_wall_s": {
        "status": "enforced",
        "enforcement": "mini_agent_query_boundary_plus_adapter_hard_timeout",
        "counter": "integer wall-clock seconds since DefaultAgent construction",
        "definition": (
            "mini checks 3600 seconds before model queries; the adapter subprocess "
            "hard timeout is 3600+120=3720 seconds."
        ),
    },
    "max_cost_usd": {
        "status": "enforced",
        "enforcement": "mini_agent_query_boundary",
        "counter": "sum of model message extra.cost",
        "definition": (
            "Checked before each model query; the final admitted call may overshoot "
            "the threshold by one call."
        ),
    },
    "agent_tool_timeout_s": {
        "status": "enforced",
        "enforcement": "docker_environment_per_command",
        "counter": None,
        "definition": "Each agent shell command has a 60-second timeout.",
        "value": AGENT_TOOL_TIMEOUT_S,
    },
}


def frozen_budget_block_bytes() -> bytes:
    """Exact YAML bytes required in every materialized M2/M3 task."""
    return dumps({"budget": SWEBV_TASK_BUDGET}).encode("utf-8")


def frozen_budget_block_sha256() -> str:
    return hashlib.sha256(frozen_budget_block_bytes()).hexdigest()


def extract_budget_block_bytes(task_yaml: bytes) -> bytes:
    """Extract the top-level ``budget`` block without normalizing its bytes."""
    lines = task_yaml.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line == b"budget:\n"), None
    )
    if start is None:
        raise RuntimeError(
            "SWE-bench budget identity drift: task YAML has no exact LF budget block"
        )
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith((b" ", b"\t")):
            break
        end += 1
    return b"".join(lines[start:end])


def assert_frozen_budget_bytes(task_yaml: bytes) -> str:
    actual = extract_budget_block_bytes(task_yaml)
    expected = frozen_budget_block_bytes()
    if actual != expected:
        raise RuntimeError(
            "SWE-bench budget identity drift: task budget bytes differ from D1-i"
        )
    return hashlib.sha256(actual).hexdigest()


def expected_mini_agent_limits() -> dict[str, int | float]:
    return {
        "step_limit": int(SWEBV_TASK_BUDGET["max_steps"]),
        "cost_limit": float(SWEBV_TASK_BUDGET["max_cost_usd"]),
        "wall_time_limit_seconds": int(SWEBV_TASK_BUDGET["max_wall_s"]),
    }
