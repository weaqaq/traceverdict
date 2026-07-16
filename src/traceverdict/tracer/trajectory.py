"""Map mini-swe-agent trajectory messages to TraceVerdict events (D1-b, D1-f)."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

EXPECTED_TRAJECTORY_FORMAT = "mini-swe-agent-1.1"


class TrajectoryFormatError(ValueError):
    """trajectory_format assertion failed (D1-f)."""


def assert_trajectory_format(traj: dict[str, Any]) -> None:
    fmt = traj.get("trajectory_format")
    if fmt != EXPECTED_TRAJECTORY_FORMAT:
        raise TrajectoryFormatError(
            f"trajectory_format must be {EXPECTED_TRAJECTORY_FORMAT!r}, got {fmt!r}"
        )


def _iso_ts(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return str(value)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prompt_sha256(messages_for_model: list[dict[str, Any]]) -> str:
    """Hash the message sequence actually sent into the model for this turn (D1-b)."""
    # Strip non-API fields like nested huge blobs only if marked; hash full sequence as presented.
    prepared = []
    for m in messages_for_model:
        # Mirror litellm path: exclude only nothing for sha; use role/content/tool_calls/tool_call_id.
        entry = {k: m[k] for k in m if k != "extra"}
        prepared.append(entry)
    return hashlib.sha256(_canonical_json(prepared).encode("utf-8")).hexdigest()


def _is_observation(msg: dict[str, Any]) -> bool:
    role = msg.get("role")
    if role == "tool":
        return True
    extra = msg.get("extra") or {}
    if "returncode" in extra or "raw_output" in extra:
        return True
    return False


def _observation_body(msg: dict[str, Any]) -> dict[str, Any]:
    """Full observation content for embedding into tool_call.payload_json (D1-b)."""
    return {
        "role": msg.get("role"),
        "content": msg.get("content"),
        "tool_call_id": msg.get("tool_call_id"),
        "extra": msg.get("extra") or {},
    }


def _usage_from_extra(extra: dict[str, Any]) -> dict[str, Any]:
    response = extra.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        raise TrajectoryFormatError("assistant response is missing native usage (D2-a/D3-c)")
    usage = response["usage"]
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TrajectoryFormatError(f"usage.{key} must be a non-negative integer")
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise TrajectoryFormatError(
            "usage.total_tokens must equal prompt_tokens + completion_tokens"
        )
    return usage


def _is_llm_attempt(msg: dict[str, Any]) -> bool:
    """True for normal replies and mini's persisted FormatError responses.

    mini-swe-agent 2.4.5 stores a response that failed action parsing on the
    generated user retry message.  The response remains a real, billable LLM
    attempt and must not disappear from the trace merely because its retry
    envelope has role=user.
    """
    if msg.get("role") == "assistant":
        return True
    extra = msg.get("extra") or {}
    response = extra.get("response")
    return (
        extra.get("interrupt_type") == "FormatError"
        and isinstance(response, dict)
        and isinstance(response.get("usage"), dict)
    )


def _has_native_usage(msg: dict[str, Any]) -> bool:
    response = (msg.get("extra") or {}).get("response")
    return isinstance(response, dict) and isinstance(response.get("usage"), dict)


def usage_cost_decimal(usage: dict[str, Any], model_price: dict[str, Any]) -> Decimal:
    """Recompute cached-input/output cost from native usage using Decimal."""
    prompt = int(usage["prompt_tokens"])
    completion = int(usage["completion_tokens"])
    details = usage.get("prompt_tokens_details") or {}
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        # OpenAI-compatible providers may serialize a known zero cache hit as
        # JSON null on the first request.  Missing/null means zero; non-null
        # values remain strictly type checked below.
        cached = details.get("cached_tokens")
    if cached is None:
        cached = 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = prompt - int(cached or 0)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (cached, miss)):
        raise TrajectoryFormatError("usage cache hit/miss tokens must be non-negative integers")
    if int(cached) + int(miss) != prompt:
        raise TrajectoryFormatError("usage cache hit + miss tokens must equal prompt_tokens")

    def rate(name: str) -> Decimal:
        value = model_price.get(name)
        if value is None or Decimal(str(value)) <= 0:
            raise TrajectoryFormatError(f"missing or non-positive model price: {name}")
        return Decimal(str(value))

    return (
        Decimal(int(miss)) * rate("input_cost_per_token")
        + Decimal(int(cached)) * rate("cache_read_input_token_cost")
        + Decimal(completion) * rate("output_cost_per_token")
    )


def summarize_llm_metrics(
    events: list[dict[str, Any]], *, instance_cost: Any, model_price: dict[str, Any]
) -> tuple[int, int, float]:
    """Strictly reconcile event usage/cost with registry and mini model_stats."""
    tokens_in = 0
    tokens_out = 0
    cost_sum = 0.0
    mini_cost_sum = 0.0
    registry_fallback_sum = 0.0
    for event in events:
        if event["etype"] != "llm_call":
            continue
        payload = json.loads(event["payload_json"])
        usage = payload["usage"]
        tokens_in += event["tokens_in"]
        tokens_out += event["tokens_out"]
        expected = float(usage_cost_decimal(usage, model_price))
        actual_cost = payload.get("cost")
        if actual_cost is None and (
            payload.get("source_role") != "assistant"
            and payload.get("source_interrupt_type") == "FormatError"
        ):
            # mini 2.4.5 persists FormatError responses without cost, although
            # their native usage is complete.  Preserve strict accounting by
            # recomputing from the frozen registry, never by treating them as
            # free calls.
            actual_cost = expected
            registry_fallback_sum += expected
        elif (
            isinstance(actual_cost, bool)
            or not isinstance(actual_cost, (int, float))
            or actual_cost <= 0
        ):
            raise TrajectoryFormatError("llm_call cost must be a positive number")
        if not math.isclose(float(actual_cost), expected, abs_tol=1e-9, rel_tol=1e-6):
            raise TrajectoryFormatError(
                f"llm_call cost mismatch: mini={actual_cost!r} recomputed={expected!r}"
            )
        cost_sum += float(actual_cost)
        if payload.get("cost") is not None:
            mini_cost_sum += float(actual_cost)
    if tokens_in <= 0 or tokens_out <= 0:
        raise TrajectoryFormatError("run token totals must both be positive")
    format_error_only = all(
        json.loads(event["payload_json"]).get("source_role") != "assistant"
        for event in events
        if event["etype"] == "llm_call"
    )
    if (
        isinstance(instance_cost, bool)
        or not isinstance(instance_cost, (int, float))
        or instance_cost < 0
        or (instance_cost == 0 and not format_error_only)
    ):
        raise TrajectoryFormatError("info.model_stats.instance_cost must be positive")
    if instance_cost > 0 and not math.isclose(
        mini_cost_sum, float(instance_cost), abs_tol=1e-9, rel_tol=1e-6
    ):
        raise TrajectoryFormatError(
            "instance cost mismatch: "
            f"mini_events={mini_cost_sum!r} model_stats={instance_cost!r}"
        )
    normalized_instance_cost = float(instance_cost) + registry_fallback_sum
    if not math.isclose(
        cost_sum, normalized_instance_cost, abs_tol=1e-9, rel_tol=1e-6
    ):
        raise TrajectoryFormatError(
            "normalized instance cost mismatch: "
            f"events={cost_sum!r} model_stats={instance_cost!r} "
            f"registry_fallback={registry_fallback_sum!r}"
        )
    return tokens_in, tokens_out, cost_sum


def map_trajectory_to_events(
    traj: dict[str, Any],
    *,
    store_prompt_full: bool = False,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Convert trajectory to event row dicts (without run_id).

    Returns (events, trace_complete, exit_status).
    Counting (D1-b): each assistant -> llm_call; each actions[] item -> tool_call;
    role=exit -> final. Missing exit -> error event, trace_complete=False, no synthetic final.
    Observations are not separate events; full body goes into matching tool_call.payload_json.
    """
    assert_trajectory_format(traj)
    messages: list[dict[str, Any]] = list(traj.get("messages") or [])
    events: list[dict[str, Any]] = []
    step_idx = 0
    has_exit = False
    exit_status: str | None = None

    # Index observations by tool_call_id and sequential fallback queue.
    obs_by_id: dict[str, dict[str, Any]] = {}
    obs_queue: list[dict[str, Any]] = []
    for m in messages:
        if _is_observation(m):
            body = _observation_body(m)
            tid = m.get("tool_call_id")
            if tid:
                obs_by_id[str(tid)] = body
            else:
                obs_queue.append(body)

    for i, msg in enumerate(messages):
        role = msg.get("role")
        extra = msg.get("extra") or {}
        ts = _iso_ts(extra.get("timestamp"))

        if role != "assistant" and _has_native_usage(msg) and not _is_llm_attempt(msg):
            raise TrajectoryFormatError(
                "usage-bearing non-assistant message must be a persisted FormatError retry"
            )

        if _is_llm_attempt(msg):
            # Prompt = messages actually sent before this assistant reply.
            prompt_msgs = messages[:i]
            sha = extra.get("prompt_sha256") or prompt_sha256(prompt_msgs)
            if not isinstance(sha, str) or len(sha) != 64:
                raise TrajectoryFormatError("prompt_sha256 must be a 64-character hex digest")
            try:
                int(sha, 16)
            except ValueError as e:
                raise TrajectoryFormatError("prompt_sha256 must be hexadecimal") from e
            usage = _usage_from_extra(extra)
            cost = extra.get("cost")
            if role == "assistant" and (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or cost <= 0
            ):
                raise TrajectoryFormatError("assistant cost must be a positive number (D2-a/D3-c)")
            if role != "assistant":
                cost = None
            response = extra.get("response") or {}
            choices = response.get("choices") or []
            response_message = {}
            if choices and isinstance(choices[0], dict):
                response_message = choices[0].get("message") or {}
            llm_payload: dict[str, Any] = {
                "prompt_sha256": sha,
                "role": "assistant",
                "source_role": role,
                "source_interrupt_type": extra.get("interrupt_type"),
                "content": response_message.get("content", msg.get("content")),
                "tool_calls": response_message.get("tool_calls", msg.get("tool_calls")),
                "cost": cost,
                "cost_source": "mini" if cost is not None else "registry_recomputed",
                "usage": usage,
            }
            if store_prompt_full:
                llm_payload["prompt_messages"] = [
                    {k: m[k] for k in m if k != "extra"} for m in prompt_msgs
                ]
                # D4-a: reasoning text is full model output and follows the
                # existing full-text switch. Token details remain in usage
                # regardless of this switch.
                reasoning_content = msg.get("reasoning_content")
                if reasoning_content is None:
                    if choices and isinstance(choices[0], dict):
                        reasoning_content = (choices[0].get("message") or {}).get(
                            "reasoning_content"
                        )
                if reasoning_content is not None:
                    llm_payload["reasoning_content"] = reasoning_content
            events.append(
                {
                    "step_idx": step_idx,
                    "ts": ts,
                    "etype": "llm_call",
                    "payload_json": json.dumps(llm_payload, ensure_ascii=False),
                    "tokens_in": usage["prompt_tokens"],
                    "tokens_out": usage["completion_tokens"],
                    "latency_ms": None,
                }
            )
            step_idx += 1

            actions = list(extra.get("actions") or []) if role == "assistant" else []
            for action in actions:
                tid = action.get("tool_call_id")
                obs = None
                if tid and str(tid) in obs_by_id:
                    obs = obs_by_id[str(tid)]
                elif obs_queue:
                    obs = obs_queue.pop(0)
                tool_payload = {
                    "action": action,
                    "observation": obs,  # full observation verbatim when present (D1-b)
                }
                events.append(
                    {
                        "step_idx": step_idx,
                        "ts": ts,
                        "etype": "tool_call",
                        "payload_json": json.dumps(tool_payload, ensure_ascii=False),
                        "tokens_in": None,
                        "tokens_out": None,
                        "latency_ms": None,
                    }
                )
                step_idx += 1

        elif role == "exit":
            has_exit = True
            exit_status = extra.get("exit_status")
            final_payload = {
                "exit_status": exit_status,
                "submission": extra.get("submission", ""),
                "content": msg.get("content"),
            }
            events.append(
                {
                    "step_idx": step_idx,
                    "ts": ts,
                    "etype": "final",
                    "payload_json": json.dumps(final_payload, ensure_ascii=False),
                    "tokens_in": None,
                    "tokens_out": None,
                    "latency_ms": None,
                }
            )
            step_idx += 1

    if not has_exit:
        events.append(
            {
                "step_idx": step_idx,
                "ts": datetime.now(timezone.utc).isoformat(),
                "etype": "error",
                "payload_json": json.dumps(
                    {
                        "error": "missing_exit",
                        "detail": "trajectory has no role=exit message; final not synthesized (D1-b)",
                    },
                    ensure_ascii=False,
                ),
                "tokens_in": None,
                "tokens_out": None,
                "latency_ms": None,
            }
        )
        return events, False, exit_status

    # Count check: llm_call + tool_call + final
    n_llm = sum(1 for e in events if e["etype"] == "llm_call")
    n_tool = sum(1 for e in events if e["etype"] == "tool_call")
    n_final = sum(1 for e in events if e["etype"] == "final")
    n_assistant = sum(1 for m in messages if _is_llm_attempt(m))
    n_actions = sum(
        len((m.get("extra") or {}).get("actions") or [])
        for m in messages
        if _is_llm_attempt(m)
    )
    count_ok = n_llm == n_assistant and n_tool == n_actions and n_final == 1
    return events, count_ok, exit_status


def reconcile_trace(
    *,
    event_count_ok: bool,
    has_exit: bool,
    patch_sha256: str,
    native_submission: str | None,
    submission_sha256: str | None,
) -> bool:
    """trace_complete when event counts reconcile and patch sha is present.

    Patch authority is snapshot diff (D1-c); native submission mismatch does not fail
    reconciliation (recorded separately as note).
    """
    if not has_exit or not event_count_ok:
        return False
    if not patch_sha256:
        return False
    # submission_sha256 unused for hard fail; kept for call-site clarity
    _ = (native_submission, submission_sha256)
    return True
