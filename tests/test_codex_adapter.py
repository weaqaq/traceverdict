from __future__ import annotations

import copy
import hashlib
import json

import pytest

from traceverdict.adapters.codex import build_codex_command, parse_codex_jsonl
from traceverdict.adapters.mini_swe_agent import AdapterHarnessError
from traceverdict.snapshot.codex_image import AUTH_SHELL_WRAPPER
from traceverdict.tracer.codex_jsonl import (
    map_codex_trajectory_to_events,
    summarize_codex_metrics,
)
from traceverdict.tracer.trajectory import TrajectoryFormatError


def _params():
    return {
        "auth_mode": "chatgpt_subscription",
        "billing_mode": "subscription_unallocatable",
        "model_reasoning_effort": "high",
        "approval_policy": "never",
        "sandbox_mode": "workspace-write",
        "web_search": "disabled",
        "ephemeral": True,
        "ignore_user_config": True,
        "ignore_rules": True,
        "concurrency": 1,
        "network_mode": "bridge",
        "container_cap_add": "SYS_ADMIN",
        "container_seccomp": "unconfined",
        "inner_sandbox": "workspace-write",
        "sandbox_workspace_write": {"network_access": True},
        "history": {"persistence": "none"},
        "features": {
            "shell_tool": True,
            "shell_snapshot": False,
            "unified_exec": False,
        },
        "hide_agent_reasoning": False,
        "auth_isolation": "per_tool_mount_namespace_hide_codex_home_v2",
        "service_tier": "omitted",
        "temperature": "omitted",
        "top_p": "omitted",
    }


def _trajectory(*, include_final=True, returncode=0):
    records = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "pytest -q",
                "aggregated_output": "1 passed\n",
                "exit_code": 0,
                "status": "completed",
            },
        },
    ]
    if include_final:
        records.append(
            {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "done"}}
        )
    records.extend(
        [
            {"type": "future.event", "value": 1},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 300000,
                    "cached_input_tokens": 200000,
                    "output_tokens": 1000,
                    "reasoning_output_tokens": 200,
                },
            },
        ]
    )
    raw = "\n".join(json.dumps(r) for r in records) + "\n"
    return {
        "trajectory_format": "codex-exec-jsonl-1",
        "records": records,
        "jsonl_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "instruction_sha256": "a" * 64,
        "info": {"returncode": returncode},
    }


def test_command_freezes_behavior_and_rejects_identity_drift():
    command = build_codex_command(
        model_name="gpt-5.6-luna", model_params=_params(), container_cwd="/testbed"
    )
    assert command[:4] == ["/opt/traceverdict/codex", "exec", "--json", "--ephemeral"]
    assert "workspace-write" in command
    assert "--strict-config" in command
    assert "--dangerously-bypass-hook-trust" not in command
    assert "--profile" not in command
    assert "features.unified_exec=false" in command
    assert "sandbox_workspace_write.network_access=true" in command
    drift = _params()
    drift["network_mode"] = "host"
    with pytest.raises(AdapterHarnessError, match="identity mismatch"):
        build_codex_command(model_name="gpt-5.6-luna", model_params=drift, container_cwd="/testbed")


def test_jsonl_parser_fails_closed_on_bad_line():
    assert parse_codex_jsonl('{"type":"turn.started"}\n')[0]["type"] == "turn.started"
    assert parse_codex_jsonl(
        '{"type":"item.completed","item":{"type":"agent_message","text":"修复完成"}}\n'
    )[0]["item"]["text"] == "修复完成"
    with pytest.raises(AdapterHarnessError, match="invalid"):
        parse_codex_jsonl("not-json\n")


def test_codex_mapping_is_turn_aggregate_and_preserves_command():
    events, complete, status = map_codex_trajectory_to_events(_trajectory())
    assert complete is True
    assert status == "Submitted"
    assert [event["etype"] for event in events] == ["tool_call", "llm_call", "final", "note"]
    tool = json.loads(events[0]["payload_json"])
    assert tool["command"] == "pytest -q"
    assert tool["output"] == "1 passed\n"
    llm = json.loads(events[1]["payload_json"])
    assert llm["granularity"] == "codex_turn_aggregate"
    assert llm["prompt_hash_scope"] == "cli_input"
    assert llm["actual_cost_usd"] is None


def test_codex_missing_final_and_nonzero_exit_are_incomplete():
    events, complete, status = map_codex_trajectory_to_events(_trajectory(include_final=False))
    assert complete is False and status == "CodexTurnFailed"
    assert events[-1]["etype"] == "error"
    events, complete, _ = map_codex_trajectory_to_events(_trajectory(returncode=1))
    assert complete is False


def test_subscription_limit_is_an_incomplete_error_without_false_llm_or_final():
    traj = _trajectory(include_final=False)
    traj["records"] = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "error", "message": "quota"},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
    ]
    events, complete, status = map_codex_trajectory_to_events(traj)
    assert complete is False
    assert status == "SubscriptionLimitExceeded"
    assert [event["etype"] for event in events] == ["error"]


def test_shadow_cost_is_lower_bound_and_never_actual_spend():
    events, _, _ = map_codex_trajectory_to_events(_trajectory())
    price = {
        "uncached_input_cost_per_token": 0.000001,
        "cached_input_cost_per_token": 0.0000001,
        "output_cost_per_token": 0.000006,
        "long_context_threshold_input_tokens": 272000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
        "cache_write_multiplier": 1.25,
    }
    tokens_in, tokens_out, actual, shadow = summarize_codex_metrics(events, model_price=price)
    assert (tokens_in, tokens_out, actual) == (300000, 1000, None)
    assert shadow["amount_usd"] == pytest.approx(0.249)
    assert shadow["classification"] == "lower_bound"
    assert shadow["enters_real_spend_tripwire"] is False
    payload = json.loads(next(e for e in events if e["etype"] == "llm_call")["payload_json"])
    assert payload["api_equivalent_shadow_cost"] == shadow


def test_missing_usage_is_harness_level_format_error():
    traj = _trajectory()
    traj["records"][-1].pop("usage")
    with pytest.raises(TrajectoryFormatError, match="missing usage"):
        map_codex_trajectory_to_events(traj)


def test_auth_shell_wrapper_hides_codex_home_before_original_bash():
    mount_index = AUTH_SHELL_WRAPPER.index("mount -t tmpfs")
    exec_index = AUTH_SHELL_WRAPPER.index('exec /opt/traceverdict/bash-real "$@"')
    assert "umount /run/traceverdict-codex/auth.json" in AUTH_SHELL_WRAPPER
    assert mount_index < exec_index
    assert "exit 126" in AUTH_SHELL_WRAPPER
