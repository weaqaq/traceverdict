"""Zero-model, streaming ingestion of Codex exec and desktop rollout JSONL."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from traceverdict.tracer.codex_jsonl import _usage as exec_usage
from traceverdict.tracer.trajectory import TrajectoryFormatError

ROLLOUT_FORMAT = "codex-rollout-jsonl-observed-2026-07-v1"
STATE_VERSION = 1
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class IngestError(RuntimeError):
    pass


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _prefix_sha(path: Path, length: int) -> str:
    h = hashlib.sha256()
    remaining = length
    with path.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    if remaining:
        raise IngestError("source file shortened since last ingest")
    return h.hexdigest()


def _date(value: Any) -> str:
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _failure(text: str) -> str:
    lower = text.lower()
    if any(x in lower for x in ("rate limit", "quota", "usage limit", "budget")):
        return "budget/rate_limit"
    if "abort" in lower or "cancel" in lower or "interrupt" in lower:
        return "aborted"
    if "loop" in lower or "repeated" in lower:
        return "loop"
    if "incomplete" in lower or "truncated" in lower:
        return "incomplete"
    if "error" in lower or "failed" in lower:
        return "agent_error"
    return "other"


def _record_key(date: str, model: str) -> str:
    return f"{date}\0{model or 'unknown'}"


def _blank(date: str, model: str) -> dict[str, Any]:
    return {
        "date_utc": date, "model": model or "unknown",
        "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
        "reasoning_output_tokens": 0, "turns": 0, "tool_calls": 0,
        "failures": {k: 0 for k in ("budget/rate_limit", "aborted", "agent_error", "loop", "incomplete", "other")},
        "unknown_events": {}, "open_turns": 0,
    }


def _merge(target: dict[str, Any], delta: dict[str, Any]) -> None:
    for key in (*TOKEN_KEYS, "turns", "tool_calls", "open_turns"):
        target[key] = int(target.get(key, 0)) + int(delta.get(key, 0))
    for key, value in delta.get("failures", {}).items():
        target["failures"][key] = target["failures"].get(key, 0) + int(value)
    for key, value in delta.get("unknown_events", {}).items():
        target["unknown_events"][key] = target["unknown_events"].get(key, 0) + int(value)


def _parse_exec(records: list[dict[str, Any]], previous_open: int = 0) -> tuple[dict[str, Any], int]:
    date = _date(next((r.get("timestamp") for r in records if r.get("timestamp")), None))
    model = str(next((r.get("model") for r in records if r.get("model")), "unknown"))
    out = _blank(date, model)
    known = {"thread.started", "turn.started", "turn.completed", "turn.failed", "error", "item.started", "item.updated", "item.completed"}
    started = sum(r.get("type") == "turn.started" for r in records)
    completed = 0
    for record in records:
        typ = record.get("type")
        if typ == "turn.completed":
            try:
                usage = exec_usage(record)
            except TrajectoryFormatError as exc:
                raise IngestError(str(exc)) from exc
            for key in TOKEN_KEYS:
                out[key] += usage[key]
            out["turns"] += 1
            completed += 1
        elif typ == "item.completed" and isinstance(record.get("item"), dict) and record["item"].get("type") == "command_execution":
            out["tool_calls"] += 1
        elif typ == "item.completed" and isinstance(record.get("item"), dict) and record["item"].get("type") not in {"agent_message", "reasoning"}:
            label = f"item.completed:{record['item'].get('type')}"
            out["unknown_events"][label] = out["unknown_events"].get(label, 0) + 1
        elif typ in {"turn.failed", "error"}:
            out["failures"][_failure(json.dumps(record, ensure_ascii=False))] += 1
        elif typ not in known:
            out["unknown_events"][str(typ)] = out["unknown_events"].get(str(typ), 0) + 1
    open_now = max(0, previous_open + started - completed - sum(out["failures"].values()))
    out["open_turns"] = open_now - previous_open
    return out, open_now


def _desktop_usage(info: Any) -> dict[str, int]:
    if not isinstance(info, dict) or not isinstance(info.get("total_token_usage"), dict):
        raise IngestError("desktop token_count required total_token_usage is missing")
    usage = info["total_token_usage"]
    result: dict[str, int] = {}
    for key in TOKEN_KEYS:
        value = usage.get(key, 0 if key in {"cached_input_tokens", "reasoning_output_tokens"} else None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IngestError(f"desktop usage {key} must be a non-negative integer")
        result[key] = value
    return result


def _parse_rollout(
    records: list[dict[str, Any]], previous_total: dict[str, int] | None,
    previous_open: set[str] | None = None, previous_model: str = "unknown",
) -> tuple[dict[str, Any], dict[str, int], set[str], str]:
    date = _date(next((r.get("timestamp") for r in records if r.get("timestamp")), None))
    model = previous_model
    out = _blank(date, model)
    latest = dict(previous_total or {k: 0 for k in TOKEN_KEYS})
    open_ids = set(previous_open or set())
    prior_open_count = len(open_ids)
    known_payload = {"task_started", "task_complete", "turn_aborted", "token_count", "custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output", "message", "user_message", "agent_message"}
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        typ = payload.get("type") or (payload.get("info") and "token_count")
        if record.get("type") == "turn_context" and payload.get("model"):
            model = str(payload["model"])
            out["model"] = model
        if typ == "task_started":
            open_ids.add(str(payload.get("turn_id")))
        elif typ == "task_complete":
            open_ids.discard(str(payload.get("turn_id")))
            out["turns"] += 1
        elif typ == "turn_aborted":
            open_ids.discard(str(payload.get("turn_id")))
            out["failures"]["aborted"] += 1
        elif typ == "token_count":
            current = _desktop_usage(payload.get("info"))
            for key in TOKEN_KEYS:
                if current[key] < latest.get(key, 0):
                    raise IngestError("desktop cumulative usage moved backwards")
                out[key] += current[key] - latest.get(key, 0)
            latest = current
        elif typ in {"custom_tool_call", "function_call"}:
            out["tool_calls"] += 1
        elif typ not in known_payload:
            label = f"{record.get('type')}:{typ}"
            out["unknown_events"][label] = out["unknown_events"].get(label, 0) + 1
    out["open_turns"] = len(open_ids) - prior_open_count
    return out, latest, open_ids, model


def _source_files(paths: Iterable[str | Path] | None) -> list[Path]:
    if paths:
        files: list[Path] = []
        for value in paths:
            path = Path(value)
            files.extend(sorted(path.rglob("*.jsonl")) if path.is_dir() else [path])
        return files
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return sorted((home / "sessions").rglob("*.jsonl"))


def ingest(paths: Iterable[str | Path] | None, *, state_path: str | Path, metrics_path: str | Path) -> dict[str, Any]:
    state_file, metrics_file = Path(state_path), Path(metrics_path)
    state = _read_json(state_file, {"version": STATE_VERSION, "sources": {}})
    metrics = _read_json(metrics_file, {"version": STATE_VERSION, "days": {}})
    if state.get("version") != STATE_VERSION or metrics.get("version") != STATE_VERSION:
        raise IngestError("unsupported ingest state version")
    added = _blank(datetime.now(timezone.utc).date().isoformat(), "mixed")
    processed = 0
    for path in _source_files(paths):
        if not path.is_file():
            raise IngestError(f"JSONL source not found: {path}")
        source_id = hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()
        prior = state["sources"].get(source_id, {})
        offset = int(prior.get("offset", 0))
        if path.stat().st_size < offset:
            raise IngestError("source file shortened since last ingest")
        if offset and _prefix_sha(path, offset) != prior.get("prefix_sha256"):
            raise IngestError("source prefix changed since last ingest")
        consumed = offset
        record_count = 0
        batch: list[dict[str, Any]] = []
        fmt = str(prior.get("format") or "")
        latest = prior.get("desktop_total_usage")
        open_ids = set(prior.get("open_turn_ids") or [])
        open_count = int(prior.get("open_turn_count", 0))
        source_model = str(prior.get("model") or "unknown")

        def consume(items: list[dict[str, Any]]) -> None:
            nonlocal latest, open_ids, open_count, source_model
            if fmt == ROLLOUT_FORMAT:
                delta, latest, open_ids, source_model = _parse_rollout(
                    items, latest, open_ids, source_model,
                )
            else:
                delta, open_count = _parse_exec(items, open_count)
                source_model = delta["model"]
            key = _record_key(delta["date_utc"], delta["model"])
            day = metrics["days"].setdefault(key, _blank(delta["date_utc"], delta["model"]))
            _merge(day, delta)
            _merge(added, delta)

        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                consumed = stream.tell()
                try:
                    item = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise IngestError(f"invalid complete JSONL line at byte {before}") from exc
                if not isinstance(item, dict):
                    raise IngestError("JSONL records must be objects")
                if not fmt:
                    fmt = ROLLOUT_FORMAT if item.get("type") in {"session_meta", "event_msg", "response_item", "turn_context", "turn_context_compacted"} and "payload" in item else "codex-exec-jsonl"
                batch.append(item)
                record_count += 1
                if len(batch) >= 512:
                    consume(batch)
                    batch.clear()
        if not record_count:
            continue
        if batch:
            consume(batch)
        state["sources"][source_id] = {
            "offset": consumed, "prefix_sha256": _prefix_sha(path, consumed),
            "format": fmt, "desktop_total_usage": latest,
            "open_turn_ids": sorted(open_ids) if fmt == ROLLOUT_FORMAT else None,
            "open_turn_count": len(open_ids) if fmt == ROLLOUT_FORMAT else open_count,
            "model": source_model,
        }
        processed += 1
    _write_json(state_file, state)
    _write_json(metrics_file, metrics)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    recent = [v for v in metrics["days"].values() if v["date_utc"] >= cutoff]
    return {"sources_updated": processed, "added": added, "last_7_days": recent, "state_version": STATE_VERSION}
