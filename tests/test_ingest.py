from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceverdict.ingest import IngestError, ROLLOUT_FORMAT, ROLLOUT_FORMAT_V1, ingest


def _line(value) -> bytes:
    return (json.dumps(value) + "\n").encode()


def test_exec_ingest_is_incremental_and_content_free(tmp_path: Path) -> None:
    source = tmp_path / "exec.jsonl"
    source.write_bytes(b"".join([
        _line({"type": "turn.started"}),
        _line({"type": "item.completed", "item": {"type": "command_execution", "command": "SECRET", "aggregated_output": "PRIVATE"}}),
        _line({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 4, "reasoning_output_tokens": 2}}),
    ]))
    state, metrics = tmp_path / "state.json", tmp_path / "metrics.json"
    first = ingest([source], state_path=state, metrics_path=metrics)
    second = ingest([source], state_path=state, metrics_path=metrics)
    assert first["added"]["input_tokens"] == 10
    assert first["added"]["tool_calls"] == 1
    assert second["sources_updated"] == 0
    stored = metrics.read_text(encoding="utf-8")
    assert "SECRET" not in stored and "PRIVATE" not in stored and str(source) not in stored


def test_half_line_waits_and_rewrite_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "x.jsonl"
    complete = _line({"type": "turn.started"})
    source.write_bytes(complete + b'{"type":"turn.completed"')
    state, metrics = tmp_path / "s.json", tmp_path / "m.json"
    ingest([source], state_path=state, metrics_path=metrics)
    offset = json.loads(state.read_text())["sources"]
    assert next(iter(offset.values()))["offset"] == len(complete)
    source.write_bytes(_line({"type": "error", "message": "changed"}))
    with pytest.raises(IngestError, match="prefix changed|shortened"):
        ingest([source], state_path=state, metrics_path=metrics)


def test_rollout_usage_and_open_turn(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    records = [
        {"timestamp": "2026-07-18T00:00:00Z", "type": "turn_context", "payload": {"model": "gpt-x"}},
        {"timestamp": "2026-07-18T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"timestamp": "2026-07-18T00:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 7, "reasoning_output_tokens": 2}}}},
        {"timestamp": "2026-07-18T00:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "private"}},
    ]
    source.write_bytes(b"".join(_line(x) for x in records))
    state, metrics = tmp_path / "s.json", tmp_path / "m.json"
    result = ingest([source], state_path=state, metrics_path=metrics)
    assert result["added"]["input_tokens"] == 20
    assert result["added"]["open_turns"] == 1
    assert result["added"]["tool_calls"] == 1
    assert result["token_count_events"] == 1
    assert result["null_usage_heartbeats"] == 0
    source_state = next(iter(json.loads(state.read_text())["sources"].values()))
    assert source_state["format"] == ROLLOUT_FORMAT
    assert "private" not in metrics.read_text()


def test_rollout_usage_shape_drift_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"
    source.write_bytes(_line({"timestamp": "2026-07-18", "type": "event_msg", "payload": {"type": "token_count", "info": {}}}))
    with pytest.raises(IngestError, match="required total_token_usage"):
        ingest([source], state_path=tmp_path / "s", metrics_path=tmp_path / "m")


def test_null_usage_heartbeat_is_zero_counted_but_non_null_drift_still_fails(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(b"".join([
        _line({"timestamp": "2026-07-18", "type": "event_msg", "payload": {"type": "token_count", "info": None}}),
        _line({"timestamp": "2026-07-18", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 4, "output_tokens": 2}}}}),
    ]))
    state, metrics = tmp_path / "s.json", tmp_path / "m.json"
    result = ingest([source], state_path=state, metrics_path=metrics)
    assert result["token_count_events"] == 2
    assert result["null_usage_heartbeats"] == 1
    assert result["added"]["input_tokens"] == 4
    stored = json.loads(metrics.read_text(encoding="utf-8"))
    day = next(iter(stored["days"].values()))
    assert day["token_count_events"] == 2
    assert day["null_usage_heartbeats"] == 1
    unchanged = ingest([source], state_path=state, metrics_path=metrics)
    assert unchanged["sources_updated"] == 0
    assert unchanged["token_count_events"] == 0
    assert unchanged["null_usage_heartbeats"] == 0
    day = next(iter(json.loads(metrics.read_text(encoding="utf-8"))["days"].values()))
    assert day["token_count_events"] == 2
    assert day["null_usage_heartbeats"] == 1

    source.write_bytes(source.read_bytes() + _line({
        "timestamp": "2026-07-18", "type": "event_msg",
        "payload": {"type": "token_count", "info": {}},
    }))
    state_before, metrics_before = state.read_bytes(), metrics.read_bytes()
    with pytest.raises(IngestError, match="required total_token_usage"):
        ingest([source], state_path=state, metrics_path=metrics)
    assert state.read_bytes() == state_before
    assert metrics.read_bytes() == metrics_before


@pytest.mark.parametrize("bad", [
    {"total_token_usage": {"input_tokens": -1, "output_tokens": 0}},
    {"total_token_usage": {"input_tokens": True, "output_tokens": 0}},
    {"total_token_usage": {"input_tokens": 1}},
])
def test_non_null_usage_value_drift_is_rejected(tmp_path: Path, bad: dict) -> None:
    source = tmp_path / "bad-values.jsonl"
    source.write_bytes(_line({
        "timestamp": "2026-07-18", "type": "event_msg",
        "payload": {"type": "token_count", "info": bad},
    }))
    with pytest.raises(IngestError, match="desktop usage"):
        ingest([source], state_path=tmp_path / "s", metrics_path=tmp_path / "m")


def test_v1_rollout_state_migrates_without_replay(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    first = _line({
        "timestamp": "2026-07-18", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}}},
    })
    source.write_bytes(first)
    state, metrics = tmp_path / "s.json", tmp_path / "m.json"
    ingest([source], state_path=state, metrics_path=metrics)
    state_value = json.loads(state.read_text(encoding="utf-8"))
    source_state = next(iter(state_value["sources"].values()))
    source_state["format"] = ROLLOUT_FORMAT_V1
    state.write_text(json.dumps(state_value), encoding="utf-8")

    source.write_bytes(first + _line({
        "timestamp": "2026-07-18", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 13, "output_tokens": 3}}},
    }))
    result = ingest([source], state_path=state, metrics_path=metrics)
    assert result["added"]["input_tokens"] == 3
    assert result["added"]["output_tokens"] == 1
    assert result["token_count_events"] == 1
    migrated = next(iter(json.loads(state.read_text(encoding="utf-8"))["sources"].values()))
    assert migrated["format"] == ROLLOUT_FORMAT
    day = next(iter(json.loads(metrics.read_text(encoding="utf-8"))["days"].values()))
    assert day["input_tokens"] == 13
    assert day["output_tokens"] == 3
    assert day["token_count_events"] == 2


def test_ingest_batches_large_logs_instead_of_loading_whole_file(tmp_path: Path, monkeypatch) -> None:
    import traceverdict.ingest as module
    source = tmp_path / "large.jsonl"
    source.write_bytes(b"".join(_line({"type": "item.completed", "item": {"type": "reasoning"}}) for _ in range(1200)))
    original = module._parse_exec
    sizes: list[int] = []

    def observed(records, previous_open=0):
        sizes.append(len(records))
        return original(records, previous_open)

    monkeypatch.setattr(module, "_parse_exec", observed)
    ingest([source], state_path=tmp_path / "s", metrics_path=tmp_path / "m")
    assert max(sizes) <= 512
    assert len(sizes) == 3
