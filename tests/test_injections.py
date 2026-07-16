from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jinja2 import Template

from traceverdict.adapters.mini_swe_agent import _build_mini_config
from traceverdict.adapters import mini_swe_agent as mini_adapter
from traceverdict.core.simple_yaml import dump_to_path, load_path
from minisweagent.exceptions import FormatError

from traceverdict.injections import (
    BrokenToolNameLitellmModel,
    HistoryResetLitellmModel,
    generate_injected_config,
    truncate_history,
)
from traceverdict.verifier import _docker_pytest

ROOT = Path(__file__).resolve().parents[1]


def _mini(tmp_path: Path, injection_id: str):
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    return _build_mini_config(
        image="image",
        docker_executable="docker",
        host_work_path=tmp_path,
        container_cwd="/testbed",
        model_name="openai/model",
        model_params={
            "thinking": {"type": "enabled"},
            "_traceverdict_injection": {"id": injection_id},
        },
        litellm_model_registry=registry,
        output_path=tmp_path / "traj.json",
        cost_limit=1.0,
        step_limit=3,
        wall_time_s=30,
    )


def test_i1_i1p_i2_i2p_i3_i5_map_to_public_mini_interfaces(tmp_path: Path):
    i1 = _mini(tmp_path, "I1")
    assert "execute bash commands" not in i1["agent"]["instance_template"]
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in i1["agent"]["instance_template"]

    i1p = _mini(tmp_path, "I1P")
    template = i1p["agent"]["instance_template"]
    rendered = Template(template).render(cwd="/testbed", task="TRACEVERDICT_TASK_SENTINEL")
    assert "TRACEVERDICT_TASK_SENTINEL" not in rendered
    assert "execute bash commands and edit files" in rendered
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in rendered
    assert "{{task}}" not in template and "{{ task }}" not in template

    i2 = _mini(tmp_path, "I2")
    assert "output.output[:500]" in i2["model"]["observation_template"]

    i2p = _mini(tmp_path, "I2P")
    observation = Template(i2p["model"]["observation_template"]).render(
        output={"output": "TRACEVERDICT_STDOUT_SENTINEL", "returncode": 23, "exception_info": "boom"}
    )
    assert "TRACEVERDICT_STDOUT_SENTINEL" not in observation
    assert "<returncode>23</returncode>" in observation
    assert "<exception>boom</exception>" in observation
    assert "<output>\n</output>" in observation

    i3 = _mini(tmp_path, "I3")
    assert "pytest disabled by TraceVerdict I3" in " ".join(i3["environment"]["interpreter"])
    assert "_traceverdict_injection" not in i3["model"]["model_kwargs"]

    i5 = _mini(tmp_path, "I5")
    assert i5["model"]["model_class"] == "traceverdict.injections.HistoryWindowLitellmModel"

    i1q = _mini(tmp_path, "I1Q")
    assert i1q["model"]["model_class"] == "traceverdict.injections.BrokenToolNameLitellmModel"

    i5p = _mini(tmp_path, "I5P")
    assert i5p["model"]["model_class"] == "traceverdict.injections.HistoryResetLitellmModel"


def test_i3_never_changes_verifier_command(tmp_path: Path):
    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    with patch("traceverdict.verifier.subprocess.run", return_value=Result()) as run:
        passed, _ = _docker_pytest(tmp_path, "image", ["tests/test_x.py"], "docker")
    assert passed
    command = run.call_args.args[0]
    assert command[-4:] == ["-m", "pytest", "-q", "tests/test_x.py"]
    assert "traceverdict-i3" not in " ".join(command)
    assert "pytest disabled by TraceVerdict I3" not in " ".join(command)


def test_i3q_agent_ro_verifier_rw(tmp_path: Path):
    i3q = _mini(tmp_path, "I3Q")
    agent_mount = i3q["environment"]["run_args"][-1]
    assert agent_mount.endswith(":/testbed:ro")

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    with patch("traceverdict.verifier.subprocess.run", return_value=Result()) as run:
        passed, _ = _docker_pytest(tmp_path, "image", ["tests/test_x.py"], "docker")
    assert passed
    verifier_command = run.call_args.args[0]
    verifier_mount = verifier_command[verifier_command.index("-v") + 1]
    assert verifier_mount.endswith(":/testbed")
    assert not verifier_mount.endswith(":ro")


def test_history_window_keeps_initial_prompt_and_two_complete_turns():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "o1"},
        {"role": "assistant", "content": "a2"},
        {"role": "tool", "content": "o2"},
        {"role": "assistant", "content": "a3"},
        {"role": "tool", "content": "o3"},
    ]
    kept = truncate_history(messages)
    assert [m["content"] for m in kept] == ["s", "task", "a2", "o2", "a3", "o3"]


def test_i1q_corrupts_only_deep_parsing_copy():
    class Response:
        def __init__(self, tool_calls):
            self.choices = [
                SimpleNamespace(
                    message=SimpleNamespace(tool_calls=tool_calls), finish_reason="tool_calls"
                )
            ]

        def model_copy(self, *, deep=False):
            return deepcopy(self) if deep else self

    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="bash", arguments='{"command":"echo ok"}'),
    )
    response = Response([call])
    model = object.__new__(BrokenToolNameLitellmModel)
    model.config = SimpleNamespace(format_error_template="{{ error }}")
    with pytest.raises(FormatError) as exc:
        model._parse_actions(response)
    assert "Unknown tool 'traceverdict_broken_bash'" in exc.value.messages[0]["content"]
    assert response.choices[0].message.tool_calls[0].function.name == "bash"

    with pytest.raises(FormatError) as exc:
        model._parse_actions(Response([]))
    assert "No tool calls found" in exc.value.messages[0]["content"]


def test_i5p_api_history_resets_but_input_trajectory_is_untouched():
    messages = [
        {"role": "system", "content": "s", "extra": {"kept": True}},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "o1"},
    ]
    original = deepcopy(messages)
    model = object.__new__(HistoryResetLitellmModel)
    model.config = SimpleNamespace(set_cache_control=None)
    prepared = model._prepare_messages_for_api(messages)
    assert prepared == [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    assert messages == original


@pytest.mark.parametrize(
    ("injection_id", "expected"),
    [
        ("I1Q", "traceverdict.injections.BrokenToolNameLitellmModel"),
        ("I5P", "traceverdict.injections.HistoryResetLitellmModel"),
    ],
)
def test_wrapper_model_class_reaches_mini_cli(tmp_path: Path, injection_id: str, expected: str):
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "expected stop"

    with (
        patch.object(mini_adapter, "assert_agent_version"),
        patch.object(mini_adapter, "find_mini_cli", return_value="mini"),
        patch.object(mini_adapter, "build_mini_command", side_effect=lambda _, args: args),
        patch.object(mini_adapter.subprocess, "run", return_value=Result()) as run,
    ):
        with pytest.raises(mini_adapter.AdapterHarnessError):
            mini_adapter.run_mini_swe_agent(
                instruction="task",
                image="image",
                docker_executable="docker",
                host_work_path=tmp_path,
                container_cwd="/testbed",
                model_name="openai/model",
                model_params={"_traceverdict_injection": {"id": injection_id}},
                litellm_model_registry=registry,
                agent_version="2.4.5",
                cost_limit=1.0,
                step_limit=3,
                wall_time_s=30,
                work_dir=tmp_path / "adapter",
            )
    command = run.call_args.args[0]
    index = command.index("--model-class")
    assert command[index + 1] == expected


def test_generated_config_has_lineage_and_i4_requires_nonthinking(tmp_path: Path):
    base = load_path(ROOT / "configs" / "dev.yaml")
    base_path = tmp_path / "base.yaml"
    dump_to_path(base_path, base)
    output = tmp_path / "i3.yaml"
    result = generate_injected_config("I3", base_path, output, session_id="abc")
    assert "parent_config_id=dev-deepseek-v4-flash-v2" in result["notes"]
    assert "selftest_session=abc" in result["notes"]
    assert load_path(output)["model_params"]["_traceverdict_injection"]["id"] == "I3"

    with pytest.raises(ValueError, match="thinking.type=disabled"):
        generate_injected_config("I4", base_path, tmp_path / "i4.yaml")
    base["config_id"] = "nonthinking"
    base["model_params"] = {"thinking": {"type": "disabled"}}
    dump_to_path(base_path, base)
    i4 = generate_injected_config("I4", base_path, tmp_path / "i4.yaml", session_id="abc")
    assert i4["model_params"]["temperature"] == 1.2
    assert i4["model_params"]["thinking"]["type"] == "disabled"

    i4q = generate_injected_config("I4Q", base_path, tmp_path / "i4q.yaml", session_id="abc")
    assert i4q["model_params"]["tool_choice"] == "none"
    mini_i4q = _build_mini_config(
        image="image",
        docker_executable="docker",
        host_work_path=tmp_path,
        container_cwd="/testbed",
        model_name="openai/model",
        model_params=i4q["model_params"],
        litellm_model_registry=tmp_path / "registry.json",
        output_path=tmp_path / "traj.json",
        cost_limit=1.0,
        step_limit=3,
        wall_time_s=30,
    )
    assert mini_i4q["model"]["model_kwargs"]["tool_choice"] == "none"


def test_i1_and_i1p_keep_distinct_lineage(tmp_path: Path):
    base = ROOT / "configs" / "dev.yaml"
    old = generate_injected_config("I1", base, tmp_path / "i1.yaml", session_id="s")
    replacement = generate_injected_config("I1P", base, tmp_path / "i1p.yaml", session_id="s")
    assert old["config_id"].endswith("-i1")
    assert replacement["config_id"].endswith("-i1p")
    assert old["model_params"]["_traceverdict_injection"]["id"] == "I1"
    assert replacement["model_params"]["_traceverdict_injection"]["id"] == "I1P"


def test_i2_and_i2p_keep_distinct_lineage(tmp_path: Path):
    base = ROOT / "configs" / "dev.yaml"
    old = generate_injected_config("I2", base, tmp_path / "i2.yaml", session_id="s")
    replacement = generate_injected_config("I2P", base, tmp_path / "i2p.yaml", session_id="s")
    assert old["config_id"].endswith("-i2")
    assert replacement["config_id"].endswith("-i2p")
    assert old["model_params"]["_traceverdict_injection"]["id"] == "I2"
    assert replacement["model_params"]["_traceverdict_injection"]["id"] == "I2P"
