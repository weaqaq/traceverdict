"""Unit tests for trajectory mapping (D1-b, D1-f)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from traceverdict.core.runner import _status_from_exit

from traceverdict.tracer.trajectory import (
    TrajectoryFormatError,
    assert_trajectory_format,
    map_trajectory_to_events,
    prompt_sha256,
    reconcile_trace,
    summarize_llm_metrics,
    usage_cost_decimal,
)


def _sample_traj(*, with_exit: bool = True, fmt: str = "mini-swe-agent-1.1") -> dict:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "fixing",
            "extra": {
                "actions": [{"command": "ls", "tool_call_id": "call_1"}],
                "cost": 0.104,
                "response": {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                        "prompt_cache_hit_tokens": 4,
                        "prompt_cache_miss_tokens": 6,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    }
                },
                "timestamp": 1700000000.0,
            },
            "reasoning_content": "private reasoning",
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "<returncode>0</returncode>\n<output>\nok\n</output>",
            "extra": {
                "raw_output": "ok\n",
                "returncode": 0,
                "timestamp": 1700000001.0,
            },
        },
    ]
    if with_exit:
        messages.append(
            {
                "role": "exit",
                "content": "done",
                "extra": {
                    "exit_status": "Submitted",
                    "submission": "diff --git a/x b/x\n",
                    "timestamp": 1700000002.0,
                },
            }
        )
    return {
        "trajectory_format": fmt,
        "messages": messages,
        "info": {
            "exit_status": "Submitted" if with_exit else "",
            "submission": "diff --git a/x b/x\n" if with_exit else "",
            "model_stats": {"instance_cost": 0.104, "api_calls": 1},
        },
    }


def test_assert_trajectory_format_ok():
    assert_trajectory_format({"trajectory_format": "mini-swe-agent-1.1"})


def test_assert_trajectory_format_hard_fail():
    with pytest.raises(TrajectoryFormatError):
        assert_trajectory_format({"trajectory_format": "other"})


def test_map_events_counts_and_observation_embedded():
    events, ok, status = map_trajectory_to_events(_sample_traj())
    assert ok is True
    assert status == "Submitted"
    types = [e["etype"] for e in events]
    assert types == ["llm_call", "tool_call", "final"]

    llm = json.loads(events[0]["payload_json"])
    assert "prompt_sha256" in llm
    assert len(llm["prompt_sha256"]) == 64
    assert llm["usage"]["prompt_tokens"] == 10
    assert events[0]["tokens_in"] == 10
    assert events[0]["tokens_out"] == 2
    # prompt is system+user before assistant
    expected = prompt_sha256(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
    )
    assert llm["prompt_sha256"] == expected

    tool = json.loads(events[1]["payload_json"])
    assert tool["action"]["command"] == "ls"
    assert tool["observation"] is not None
    assert tool["observation"]["content"] == (
        "<returncode>0</returncode>\n<output>\nok\n</output>"
    )
    assert tool["observation"]["extra"]["raw_output"] == "ok\n"


def test_missing_exit_writes_error_not_final():
    events, ok, status = map_trajectory_to_events(_sample_traj(with_exit=False))
    assert ok is False
    assert status is None
    types = [e["etype"] for e in events]
    assert "final" not in types
    assert "error" in types
    err = json.loads([e for e in events if e["etype"] == "error"][0]["payload_json"])
    assert err["error"] == "missing_exit"


def test_store_prompt_full_switch():
    hidden, _, _ = map_trajectory_to_events(_sample_traj(), store_prompt_full=False)
    hidden_payload = json.loads(hidden[0]["payload_json"])
    assert "reasoning_content" not in hidden_payload
    assert hidden_payload["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1

    events, _, _ = map_trajectory_to_events(_sample_traj(), store_prompt_full=True)
    llm = json.loads(events[0]["payload_json"])
    assert "prompt_messages" in llm
    assert llm["prompt_messages"][0]["role"] == "system"
    assert llm["reasoning_content"] == "private reasoning"


def test_reconcile_requires_exit_and_patch():
    assert (
        reconcile_trace(
            event_count_ok=True,
            has_exit=True,
            patch_sha256="abc",
            native_submission="",
            submission_sha256=None,
        )
        is True
    )


def test_usage_cost_and_run_summary():
    events, _, _ = map_trajectory_to_events(_sample_traj())
    price = {
        "input_cost_per_token": 0.01,
        "cache_read_input_token_cost": 0.001,
        "output_cost_per_token": 0.02,
    }
    payload = json.loads(events[0]["payload_json"])
    assert str(usage_cost_decimal(payload["usage"], price)) == "0.104"
    tokens_in, tokens_out, cost = summarize_llm_metrics(
        events, instance_cost=0.104, model_price=price
    )
    assert (tokens_in, tokens_out) == (10, 2)
    assert cost == pytest.approx(0.104)


def test_null_cached_tokens_is_a_strict_zero_not_a_type_error():
    usage = {
        "prompt_tokens": 448,
        "completion_tokens": 96,
        "total_tokens": 544,
        "prompt_tokens_details": {"cached_tokens": None},
        "completion_tokens_details": {"reasoning_tokens": 15},
    }
    price = {
        "input_cost_per_token": 1.4761148347603735e-7,
        "cache_read_input_token_cost": 2.952229669520747e-9,
        "output_cost_per_token": 2.952229669520747e-7,
    }
    assert usage_cost_decimal(usage, price) == Decimal(
        "0.00009447134942466390400"
    )


def test_mimo_reasoning_partition_shortfall_matches_frozen_paid_run():
    output_rate = Decimal("2.952229669520747e-7")
    reasoning_tokens = Decimal(306)
    registry_total = Decimal("0.001043790321955755298128704816")
    mini_total = Decimal("0.000953452094068420471")
    assert (registry_total - mini_total).quantize(Decimal("1e-18")) == (
        reasoning_tokens * output_rate
    ).quantize(Decimal("1e-18"))


def test_missing_usage_hard_fails():
    traj = _sample_traj()
    del traj["messages"][2]["extra"]["response"]
    with pytest.raises(TrajectoryFormatError, match="missing native usage"):
        map_trajectory_to_events(traj)


def test_format_error_responses_are_billable_llm_attempts():
    traj = {
        "trajectory_format": "mini-swe-agent-1.1",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {
                "role": "user",
                "content": "unknown tool; retry",
                "extra": {
                    "interrupt_type": "FormatError",
                    "response": {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {"function": {"name": "bash", "arguments": "{}"}}
                                    ],
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 2,
                            "total_tokens": 12,
                            "prompt_cache_hit_tokens": 4,
                            "prompt_cache_miss_tokens": 6,
                        },
                    },
                },
            },
            {
                "role": "exit",
                "content": "RepeatedFormatError",
                "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
            },
        ],
        "info": {
            "exit_status": "RepeatedFormatError",
            "model_stats": {"instance_cost": 0.0, "api_calls": 1},
        },
    }
    events, count_ok, status = map_trajectory_to_events(traj)
    assert count_ok is True
    assert status == "RepeatedFormatError"
    assert [event["etype"] for event in events] == ["llm_call", "final"]
    payload = json.loads(events[0]["payload_json"])
    assert payload["source_role"] == "user"
    assert payload["role"] == "assistant"
    assert payload["cost"] is None
    assert payload["cost_source"] == "registry_recomputed"
    assert payload["tool_calls"][0]["function"]["name"] == "bash"

    price = {
        "input_cost_per_token": 0.01,
        "cache_read_input_token_cost": 0.001,
        "output_cost_per_token": 0.02,
    }
    tokens_in, tokens_out, cost = summarize_llm_metrics(
        events, instance_cost=0.0, model_price=price
    )
    assert (tokens_in, tokens_out) == (10, 2)
    assert cost == pytest.approx(0.104)


def test_mixed_success_and_format_error_reconciles_mini_subset_but_bills_all():
    traj = _sample_traj()
    failed = {
        "role": "user",
        "content": "format retry",
        "extra": {
            "interrupt_type": "FormatError",
            "response": {
                "choices": [{"message": {"role": "assistant", "content": None}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_cache_miss_tokens": 6,
                },
            }
        },
    }
    traj["messages"].insert(-1, failed)
    events, ok, _ = map_trajectory_to_events(traj)
    assert ok is True
    price = {
        "input_cost_per_token": 0.01,
        "cache_read_input_token_cost": 0.001,
        "output_cost_per_token": 0.02,
    }
    tokens_in, tokens_out, cost = summarize_llm_metrics(
        events, instance_cost=0.104, model_price=price
    )
    assert (tokens_in, tokens_out) == (20, 4)
    assert cost == pytest.approx(0.208)


def test_kimi_role_boundary_reconciles_native_and_registry_fallback_costs():
    price = {
        "input_cost_per_token": 6e-7,
        "cache_read_input_token_cost": 1e-7,
        "output_cost_per_token": 3e-6,
    }

    def event(
        *,
        prompt: int,
        cached: int,
        completion: int,
        cost: float | None,
        source_role: str,
        interrupt_type: str | None,
    ) -> dict:
        payload = {
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
            "cost": cost,
            "source_role": source_role,
            "source_interrupt_type": interrupt_type,
        }
        return {
            "etype": "llm_call",
            "tokens_in": prompt,
            "tokens_out": completion,
            "payload_json": json.dumps(payload),
        }

    # Calls 1-39 and 41 from the archived Kimi probe, aggregated into the
    # native-cost surface reported by mini-swe-agent.
    native = event(
        prompt=381974,
        cached=359424,
        completion=5202,
        cost=0.0650784,
        source_role="assistant",
        interrupt_type=None,
    )
    # Call 40: mini persisted the billable FormatError response on role=user,
    # so model_stats omitted exactly this registry-recomputed $0.0025404.
    format_error = event(
        prompt=17499,
        cached=16896,
        completion=163,
        cost=None,
        source_role="user",
        interrupt_type="FormatError",
    )

    tokens_in, tokens_out, cost = summarize_llm_metrics(
        [native, format_error], instance_cost=0.0650784, model_price=price
    )
    assert (tokens_in, tokens_out) == (399473, 5365)
    assert cost == pytest.approx(0.0676188, abs=1e-12)


def test_usage_bearing_user_message_without_format_error_hard_fails():
    traj = _sample_traj()
    traj["messages"].insert(
        -1,
        {
            "role": "user",
            "content": "unexpected envelope",
            "extra": {
                "response": {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    }
                }
            },
        },
    )
    with pytest.raises(
        TrajectoryFormatError,
        match="usage-bearing non-assistant message must be a persisted FormatError retry",
    ):
        map_trajectory_to_events(traj)


def test_budget_and_timeout_exit_status_mapping():
    assert _status_from_exit(has_exit=True, exit_status="LimitsExceeded") == "budget"
    assert _status_from_exit(has_exit=True, exit_status="TimeExceeded") == "timeout"
    assert _status_from_exit(has_exit=True, exit_status="Submitted") == "ok"
    assert _status_from_exit(has_exit=False, exit_status=None) == "harness_error"
    assert (
        reconcile_trace(
            event_count_ok=True,
            has_exit=False,
            patch_sha256="abc",
            native_submission="",
            submission_sha256=None,
        )
        is False
    )
