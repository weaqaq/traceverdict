from pathlib import Path

import pytest

from traceverdict.core.selftest import build_session_configs, classify_no_alarm, evaluate_gates
from traceverdict.injections import M1_INJECTION_IDS
from traceverdict.core.simple_yaml import load_path
from traceverdict.core.config_loader import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_session_configs_record_parent_and_split_i4_baseline(tmp_path):
    configs = build_session_configs(ROOT / "configs" / "dev.yaml", tmp_path, "session1")
    thinking = load_path(configs["thinking"])
    nonthinking = load_path(configs["nonthinking"])
    i4 = load_path(configs["I4Q"])
    assert "parent_config_id=dev-deepseek-v4-flash-v2" in thinking["notes"]
    assert "selftest_session=session1" in thinking["notes"]
    assert nonthinking["model_params"]["thinking"]["type"] == "disabled"
    assert i4["model_params"]["thinking"]["type"] == "disabled"
    assert i4["model_params"]["tool_choice"] == "none"
    assert load_config(configs["thinking"])["litellm_model_registry"].is_file()
    assert set(configs) == {"thinking", "I1Q", "I2P", "I3Q", "I5P", "nonthinking", "I4Q"}
    assert M1_INJECTION_IDS == ("I1Q", "I2P", "I3Q", "I4Q", "I5P")


def test_four_gate_evaluation_and_zero_alarm_stop_signal(tmp_path):
    reports = []
    for index in range(5):
        path = tmp_path / f"r{index}.md"
        path.write_text("ok", encoding="utf-8")
        reports.append(str(path))
    environment = {"passed": True, "passed_count": 8, "total": 8}
    alarms = {injection_id: "warn" for injection_id in M1_INJECTION_IDS}
    result = evaluate_gates(
        trace_complete=[True] * 100,
        environment=environment,
        alarms=alarms,
        reports=reports,
    )
    assert result["passed"]
    assert result["checks"]["injection_detection"]["alarms"]["I4Q"] == "warn"
    alarms["I3Q"] = "none"
    failed = evaluate_gates(
        trace_complete=[True] * 100,
        environment=environment,
        alarms=alarms,
        reports=reports,
    )
    assert not failed["passed"]


def test_battery_v2_none_alarm_routing():
    assert classify_no_alarm("I2P") == {
        "kind": "probabilistic_instrument_none",
        "injection_id": "I2P",
        "finding": "F-4",
    }
    assert classify_no_alarm("I5P")["finding"] == "F-5"
    for injection_id in ("I1Q", "I3Q", "I4Q"):
        assert classify_no_alarm(injection_id)["kind"] == "deterministic_instrument_pipeline_bug"
    with pytest.raises(ValueError, match="not in Battery v2"):
        classify_no_alarm("I1P")
