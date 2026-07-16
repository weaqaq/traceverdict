"""Map Codex exec JSONL without overstating its turn-level observability."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from traceverdict.adapters.codex import CODEX_TRAJECTORY_FORMAT
from traceverdict.tracer.trajectory import TrajectoryFormatError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _item(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("item")
    return value if isinstance(value, dict) else {}


def _failure_reason(failures: list[dict[str, Any]]) -> str:
    text = _canonical(failures).lower()
    if "usage limit" in text or "quota" in text:
        return "SubscriptionLimitExceeded"
    return "CodexTurnFailed"


def _usage(record: dict[str, Any]) -> dict[str, int]:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        raise TrajectoryFormatError("Codex turn.completed is missing usage")
    result: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = usage.get(key, 0 if key in {"cached_input_tokens", "reasoning_output_tokens"} else None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TrajectoryFormatError(f"Codex usage {key} must be a non-negative integer")
        result[key] = value
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise TrajectoryFormatError("Codex cached_input_tokens exceeds input_tokens")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise TrajectoryFormatError("Codex reasoning_output_tokens exceeds output_tokens")
    for optional in ("cache_write_input_tokens",):
        if optional in usage:
            value = usage[optional]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TrajectoryFormatError(f"Codex usage {optional} must be non-negative")
            result[optional] = value
    return result


def map_codex_trajectory_to_events(
    traj: dict[str, Any], *, store_prompt_full: bool = False
) -> tuple[list[dict[str, Any]], bool, str | None]:
    if traj.get("trajectory_format") != CODEX_TRAJECTORY_FORMAT:
        raise TrajectoryFormatError("unexpected Codex trajectory format")
    records = traj.get("records")
    if not isinstance(records, list):
        raise TrajectoryFormatError("Codex trajectory records must be a list")
    events: list[dict[str, Any]] = []
    completed_commands = 0
    completed_turns: list[dict[str, Any]] = []
    completed_messages: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    returncode = (traj.get("info") or {}).get("returncode")
    if returncode != 0:
        failures.append({"type": "codex_nonzero_exit", "returncode": returncode})
    known_types = {
        "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
        "item.started", "item.updated", "item.completed",
    }
    for record in records:
        if not isinstance(record, dict):
            raise TrajectoryFormatError("Codex JSONL record must be an object")
        record_type = record.get("type")
        item = _item(record)
        if record_type == "item.completed" and item.get("type") == "command_execution":
            completed_commands += 1
            payload = {
                "source_id": item.get("id"),
                "source_sha256": _source_sha(record),
                "command": item.get("command"),
                "output": item.get("aggregated_output", item.get("output")),
                "exit_code": item.get("exit_code"),
                "status": item.get("status"),
            }
            events.append({
                "step_idx": len(events), "ts": str(record.get("timestamp") or _now()),
                "etype": "tool_call", "payload_json": json.dumps(payload, ensure_ascii=False),
                "tokens_in": None, "tokens_out": None, "latency_ms": None,
            })
        elif record_type == "item.completed" and item.get("type") == "agent_message":
            completed_messages.append(record)
        elif record_type == "turn.completed":
            completed_turns.append(record)
        elif record_type in {"turn.failed", "error"}:
            failures.append(record)
        elif record_type not in known_types:
            unknown.append(record)

    if len(completed_turns) == 1 and not failures:
        turn = completed_turns[0]
        usage = _usage(turn)
        llm_payload: dict[str, Any] = {
            "granularity": "codex_turn_aggregate",
            "prompt_sha256": traj.get("instruction_sha256"),
            "prompt_hash_scope": "cli_input",
            "jsonl_sha256": traj.get("jsonl_sha256"),
            "source_sha256": _source_sha(turn),
            "usage": usage,
            "actual_cost_mode": "subscription_unallocatable",
            "actual_cost_usd": None,
        }
        if store_prompt_full:
            llm_payload["cli_instruction_included"] = True
        events.append({
            "step_idx": len(events), "ts": str(turn.get("timestamp") or _now()),
            "etype": "llm_call", "payload_json": json.dumps(llm_payload, ensure_ascii=False),
            "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
            "latency_ms": None,
        })
        if completed_messages:
            final_record = completed_messages[-1]
            final_item = _item(final_record)
            events.append({
                "step_idx": len(events), "ts": str(final_record.get("timestamp") or _now()),
                "etype": "final",
                "payload_json": json.dumps({
                    "exit_status": "Submitted",
                    "source_id": final_item.get("id"),
                    "source_sha256": _source_sha(final_record),
                    "content": final_item.get("text", final_item.get("content")),
                }, ensure_ascii=False),
                "tokens_in": None, "tokens_out": None, "latency_ms": None,
            })
        else:
            failures.append({"type": "missing_final_agent_message"})
    elif len(completed_turns) > 1:
        failures.append({"type": "multiple_completed_turns", "count": len(completed_turns)})
    elif not failures:
        failures.append({"type": "missing_turn_completed"})

    for record in unknown:
        events.append({
            "step_idx": len(events), "ts": _now(), "etype": "note",
            "payload_json": json.dumps({
                "kind": "unknown_codex_jsonl_item",
                "source_type": record.get("type"),
                "source_sha256": _source_sha(record),
            }, ensure_ascii=False),
            "tokens_in": None, "tokens_out": None, "latency_ms": None,
        })
    if failures:
        events.append({
            "step_idx": len(events), "ts": _now(), "etype": "error",
            "payload_json": json.dumps({
                "error": "incomplete_codex_turn",
                "failures": failures,
            }, ensure_ascii=False),
            "tokens_in": None, "tokens_out": None, "latency_ms": None,
        })
        return events, False, _failure_reason(failures)

    tool_events = sum(event["etype"] == "tool_call" for event in events)
    final_events = sum(event["etype"] == "final" for event in events)
    llm_events = sum(event["etype"] == "llm_call" for event in events)
    count_ok = tool_events == completed_commands and final_events == 1 and llm_events == 1
    return events, count_ok, "Submitted"


def summarize_codex_metrics(
    events: list[dict[str, Any]], *, model_price: dict[str, Any]
) -> tuple[int, int, None, dict[str, Any]]:
    llm_events = [event for event in events if event["etype"] == "llm_call"]
    if len(llm_events) != 1:
        raise TrajectoryFormatError("Codex trace must contain one turn-aggregate llm_call")
    event = llm_events[0]
    payload = json.loads(event["payload_json"])
    usage = payload["usage"]
    input_tokens = int(usage["input_tokens"])
    cached_tokens = int(usage.get("cached_input_tokens", 0))
    output_tokens = int(usage["output_tokens"])
    uncached_tokens = input_tokens - cached_tokens
    required = (
        "uncached_input_cost_per_token", "cached_input_cost_per_token",
        "output_cost_per_token", "long_context_threshold_input_tokens",
        "long_context_input_multiplier", "long_context_output_multiplier",
        "cache_write_multiplier",
    )
    missing_price = [key for key in required if key not in model_price]
    if missing_price:
        raise TrajectoryFormatError(f"Codex shadow registry missing fields: {missing_price}")
    long_context = input_tokens > int(model_price["long_context_threshold_input_tokens"])
    input_multiplier = Decimal(str(model_price["long_context_input_multiplier"])) if long_context else Decimal(1)
    output_multiplier = Decimal(str(model_price["long_context_output_multiplier"])) if long_context else Decimal(1)
    cost = (
        Decimal(uncached_tokens) * Decimal(str(model_price["uncached_input_cost_per_token"])) * input_multiplier
        + Decimal(cached_tokens) * Decimal(str(model_price["cached_input_cost_per_token"])) * input_multiplier
        + Decimal(output_tokens) * Decimal(str(model_price["output_cost_per_token"])) * output_multiplier
    )
    missing_components: list[str] = []
    cache_write = usage.get("cache_write_input_tokens")
    if cache_write is None:
        missing_components.append("cache_write_input_tokens")
    else:
        cost += (
            Decimal(int(cache_write))
            * Decimal(str(model_price["uncached_input_cost_per_token"]))
            * Decimal(str(model_price["cache_write_multiplier"]))
            * input_multiplier
        )
    shadow = {
        "amount_usd": float(cost),
        "classification": "lower_bound" if missing_components else "complete",
        "missing_components": missing_components,
        "long_context_multiplier_applied": long_context,
        "actual_cost_mode": "subscription_unallocatable",
        "enters_real_spend_tripwire": False,
    }
    payload["api_equivalent_shadow_cost"] = shadow
    event["payload_json"] = json.dumps(payload, ensure_ascii=False)
    return input_tokens, output_tokens, None, shadow
