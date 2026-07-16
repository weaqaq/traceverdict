"""Smoke tests: schema creates all tables; minimal insert/get works."""

from __future__ import annotations

import sqlite3

import pytest

from traceverdict.tracer.db import (
    get_config,
    get_run,
    get_task,
    init_db,
    insert_config,
    insert_event,
    insert_run,
    insert_task,
    get_events_for_run,
)

EXPECTED_TABLES = {
    "task",
    "config",
    "run",
    "event",
    "artifact",
    "verdict",
    "comparison",
    "injection",
}


def test_init_db_creates_all_tables(tmp_path) -> None:
    db_path = tmp_path / "traceverdict.db"
    conn = init_db(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert EXPECTED_TABLES.issubset(names)
    finally:
        conn.close()


def test_minimal_task_config_run_event_roundtrip(tmp_path) -> None:
    conn = init_db(tmp_path / "traceverdict.db")
    try:
        insert_task(
            conn,
            {
                "task_id": "S1",
                "suite": "self",
                "source": "self",
                "repo_ref": "fixtures/S1.bundle",
                "base_commit": "abc123",
                "image_ref": "traceverdict/self:v0.1",
                "instruction": "fix the bug",
                "budget_json": '{"max_steps":10}',
                "forbidden_json": "[]",
                "gt_type": "pytest",
                "gt_spec_json": "{}",
                "tags_json": "[]",
                "created_at": "2026-07-10T00:00:00Z",
            },
        )
        insert_config(
            conn,
            {
                "config_id": "cfg-dev",
                "agent_name": "mini-swe-agent",
                "agent_version": "0.0.0",
                "model_name": "stub",
                "model_params_json": "{}",
                "prompt_version": "v0",
                "harness_version": "0.1.0",
                "notes": None,
            },
        )
        insert_run(
            conn,
            {
                "run_id": "run-1",
                "task_id": "S1",
                "config_id": "cfg-dev",
                "repetition_idx": 0,
                "mode": "scenario",
                "status": "ok",
                "exit_reason": None,
                "started_at": None,
                "finished_at": None,
                "wall_time_s": None,
                "tokens_in": None,
                "tokens_out": None,
                "cost_usd": None,
                "seed": None,
                "env_fingerprint": None,
            },
        )
        event_id = insert_event(
            conn,
            {
                "run_id": "run-1",
                "step_idx": 0,
                "ts": "2026-07-10T00:00:01Z",
                "etype": "note",
                "payload_json": "{}",
                "tokens_in": None,
                "tokens_out": None,
                "latency_ms": None,
            },
        )
        assert event_id >= 1
        assert get_task(conn, "S1")["task_id"] == "S1"
        assert get_config(conn, "cfg-dev")["config_id"] == "cfg-dev"
        assert get_run(conn, "run-1")["run_id"] == "run-1"
        events = get_events_for_run(conn, "run-1")
        assert len(events) == 1
        assert events[0]["etype"] == "note"
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path) -> None:
    conn = init_db(tmp_path / "traceverdict.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            insert_run(
                conn,
                {
                    "run_id": "orphan",
                    "task_id": "missing",
                    "config_id": "missing",
                    "repetition_idx": 0,
                    "mode": "scenario",
                    "status": "ok",
                    "exit_reason": None,
                    "started_at": None,
                    "finished_at": None,
                    "wall_time_s": None,
                    "tokens_in": None,
                    "tokens_out": None,
                    "cost_usd": None,
                    "seed": None,
                    "env_fingerprint": None,
                },
            )
    finally:
        conn.close()
