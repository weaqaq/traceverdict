from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import litellm

from traceverdict.adapters.mini_swe_agent import AdapterHarnessError
from traceverdict.adapters.swe_agent import _build_overlay, convert_swe_trajectory, run_swe_agent
from traceverdict.adapters.swe_agent_entrypoint import install_capture
from traceverdict.tracer.trajectory import map_trajectory_to_events, summarize_llm_metrics


def _capture(*, prompt: int = 10, completion: int = 2, cost: float = 0.104):
    return {
        "timestamp": 1700000000.0,
        "prompt_sha256": "a" * 64,
        "cost": cost,
        "response": {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "reason",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps({"command": "echo ok"}),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_cache_hit_tokens": 4,
                "prompt_cache_miss_tokens": prompt - 4,
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        },
    }


def _raw():
    return {
        "trajectory": [
            {
                "action": "echo ok",
                "observation": "ok\n",
                "execution_time": 0.1,
                "extra_info": {"exit_code": 0},
            }
        ],
        "info": {
            "submission": "diff --git a/a b/a\n",
            "exit_status": "submitted",
            "model_stats": {"instance_cost": 0.104, "api_calls": 1},
        },
    }


def test_convert_preserves_native_usage_prompt_hash_and_observation():
    canonical = convert_swe_trajectory(_raw(), [_capture()])
    assert canonical["source_trajectory_format"] == "swe-agent-1.1.0"
    events, complete, status = map_trajectory_to_events(canonical)
    assert complete is True
    assert status == "Submitted"
    assert [event["etype"] for event in events] == ["llm_call", "tool_call", "final"]
    llm = json.loads(events[0]["payload_json"])
    assert llm["prompt_sha256"] == "a" * 64
    assert llm["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1
    tool = json.loads(events[1]["payload_json"])
    assert tool["observation"]["extra"]["raw_output"] == "ok\n"
    assert tool["observation"]["extra"]["returncode"] == 0
    assert summarize_llm_metrics(
        events,
        instance_cost=0.104,
        model_price={
            "input_cost_per_token": 0.01,
            "cache_read_input_token_cost": 0.001,
            "output_cost_per_token": 0.02,
        },
    ) == (10, 2, 0.104)


def test_convert_does_not_mutate_raw_and_requires_usage():
    raw = _raw()
    original = copy.deepcopy(raw)
    capture = _capture()
    del capture["response"]["usage"]
    with pytest.raises(AdapterHarnessError, match="missing native usage"):
        convert_swe_trajectory(raw, [capture])
    assert raw == original


def test_convert_rejects_unreconciled_tool_steps():
    with pytest.raises(AdapterHarnessError, match="mismatch"):
        convert_swe_trajectory({"trajectory": _raw()["trajectory"] * 2, "info": {}}, [_capture()])


def test_convert_retains_parallel_proposals_without_faking_executions():
    first = _capture()
    parallel = _capture(prompt=20, completion=4, cost=0.208)
    parallel_calls = parallel["response"]["choices"][0]["message"]["tool_calls"]
    parallel_calls.append(
        {
            "id": "call-2",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": "echo ignored"}),
            },
        }
    )
    raw = {
        "trajectory": [
            _raw()["trajectory"][0],
            {
                "action": "",
                "observation": "",
                "execution_time": 0.0,
                "extra_info": {},
            },
            {
                "action": "",
                "observation": "",
                "thought": "Exit due to repeated format errors",
                "execution_time": 0.0,
                "extra_info": {},
            },
        ],
        "info": {
            "submission": None,
            "exit_status": "exit_format",
            "model_stats": {"instance_cost": 0.312, "api_calls": 2},
        },
    }
    canonical = convert_swe_trajectory(raw, [first, parallel])
    assert canonical["info"]["source_terminal_steps"] == 1
    assistants = [m for m in canonical["messages"] if m["role"] == "assistant"]
    observations = [m for m in canonical["messages"] if m["role"] == "tool"]
    assert len(assistants) == 2
    assert len(assistants[0]["extra"]["actions"]) == 1
    assert len(assistants[1]["extra"]["actions"]) == 0
    assert len(assistants[1]["extra"]["proposed_actions"]) == 2
    assert assistants[1]["extra"]["source_step"]["rejected_or_format_error"] is True
    assert len(observations) == 1
    assert observations[0]["tool_call_id"] == "call-1"
    events, complete, status = map_trajectory_to_events(canonical)
    assert complete is True
    assert status == "exit_format"
    assert [event["etype"] for event in events] == [
        "llm_call",
        "tool_call",
        "llm_call",
        "final",
    ]


def test_overlay_freezes_agent_identity_without_credentials(tmp_path: Path):
    overlay = _build_overlay(
        instruction="task",
        image="frozen@sha256:abc",
        host_work_path=tmp_path,
        model_name="openai/deepseek-v4-flash",
        model_params={
            "thinking": {"type": "enabled"},
            "completion_kwargs": {"timeout": 180},
            "retry": {"retries": 1, "min_wait": 1, "max_wait": 1},
        },
        output_dir=tmp_path / "out",
        cost_limit=1.0,
        step_limit=100,
    )
    model = overlay["agent"]["model"]
    assert model["completion_kwargs"]["extra_body"]["thinking"] == {"type": "enabled"}
    assert model["per_instance_call_limit"] == 100
    assert model["completion_kwargs"]["timeout"] == 180
    assert model["retry"] == {"retries": 1, "min_wait": 1, "max_wait": 1}
    assert overlay["agent"]["tools"]["execution_timeout"] == 60
    assert overlay["actions"]["apply_patch_locally"] is True
    assert overlay["env"]["repo"] == {
        "type": "preexisting",
        "repo_name": "testbed",
        "base_commit": "HEAD",
    }
    assert overlay["env"]["deployment"]["docker_args"] == [
        "--volume",
        f"{tmp_path.resolve()}:/testbed",
    ]
    assert "api_key" not in json.dumps(overlay)


def test_entrypoint_strips_sampling_controls_for_deepseek_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    seen = {}

    class Response:
        def model_dump(self):
            return {"choices": [], "usage": {}}

    def fake_completion(*args, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setattr(litellm.utils, "register_model", lambda value: None)
    monkeypatch.setattr(litellm.cost_calculator, "completion_cost", lambda value: 0.1)
    capture = tmp_path / "capture.jsonl"
    requests = tmp_path / "requests.jsonl"
    install_capture(
        registry_path=registry, capture_path=capture, request_path=requests
    )
    litellm.completion(
        model="openai/deepseek-v4-flash",
        messages=[{"role": "user", "content": "x"}],
        temperature=0.0,
        top_p=1.0,
        extra_body={"thinking": {"type": "enabled"}},
    )
    assert "temperature" not in seen
    assert "top_p" not in seen
    record = json.loads(capture.read_text("utf-8"))
    assert record["temperature"] is None
    assert record["top_p"] is None
    request = json.loads(requests.read_text("utf-8"))
    assert request["prompt_sha256"] == record["prompt_sha256"]
    assert request["model"] == "openai/deepseek-v4-flash"
    assert "messages" not in request
    assert "api_key" not in json.dumps(request)


def test_timeout_preserves_logs_and_cleans_only_new_containers(tmp_path: Path):
    default = tmp_path / "default.yaml"
    default.write_text("{}", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    adapter = tmp_path / "adapter"

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stderr = kwargs["stderr"]
            self.stderr.write("provider call started\n")
            self.stderr.flush()
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("swe", timeout)
            return -15

        def terminate(self):
            pass

        def kill(self):
            raise AssertionError("graceful terminate should be sufficient")

    cleaned = []
    with (
        patch(
            "traceverdict.adapters.swe_agent.assert_swe_agent_identity",
            return_value=(default, "commit"),
        ),
        patch("traceverdict.adapters.swe_agent._container_ids", return_value={"old"}),
        patch(
            "traceverdict.adapters.swe_agent._cleanup_new_containers",
            side_effect=lambda docker, before: cleaned.append((docker, before)) or ["new"],
        ),
        patch("traceverdict.adapters.swe_agent.subprocess.Popen", side_effect=FakeProcess),
    ):
        with pytest.raises(AdapterHarnessError, match=r"request_count=0.*new"):
            run_swe_agent(
                instruction="task",
                image="image",
                docker_executable="docker",
                host_work_path=work,
                container_cwd="/testbed",
                model_name="openai/deepseek-v4-flash",
                model_params={"thinking": {"type": "enabled"}},
                litellm_model_registry=registry,
                agent_version="1.1.0",
                cost_limit=1.0,
                step_limit=100,
                wall_time_s=1,
                work_dir=adapter,
            )
    assert cleaned == [("docker", {"old"})]
    assert (adapter / "swe.stderr.log").read_text("utf-8") == "provider call started\n"
