from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceverdict.ingest import IngestError, ROLLOUT_FORMAT, ingest


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
    source_state = next(iter(json.loads(state.read_text())["sources"].values()))
    assert source_state["format"] == ROLLOUT_FORMAT
    assert "private" not in metrics.read_text()


def test_rollout_usage_shape_drift_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"
    source.write_bytes(_line({"timestamp": "2026-07-18", "type": "event_msg", "payload": {"type": "token_count", "info": {}}}))
    with pytest.raises(IngestError, match="required total_token_usage"):
        ingest([source], state_path=tmp_path / "s", metrics_path=tmp_path / "m")


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
