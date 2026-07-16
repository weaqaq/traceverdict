"""Minimal SQLite helpers for the TraceVerdict v0.1 schema (no business logic)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Column tuples mirror PRD §3 for insert helpers.
_TASK_COLS = (
    "task_id",
    "suite",
    "source",
    "repo_ref",
    "base_commit",
    "image_ref",
    "instruction",
    "budget_json",
    "forbidden_json",
    "gt_type",
    "gt_spec_json",
    "tags_json",
    "created_at",
)
_CONFIG_COLS = (
    "config_id",
    "agent_name",
    "agent_version",
    "model_name",
    "model_params_json",
    "prompt_version",
    "harness_version",
    "notes",
)
_RUN_COLS = (
    "run_id",
    "task_id",
    "config_id",
    "repetition_idx",
    "mode",
    "status",
    "exit_reason",
    "started_at",
    "finished_at",
    "wall_time_s",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "seed",
    "env_fingerprint",
)
_EVENT_COLS = (
    "run_id",
    "step_idx",
    "ts",
    "etype",
    "payload_json",
    "tokens_in",
    "tokens_out",
    "latency_ms",
)
_ARTIFACT_COLS = ("artifact_id", "run_id", "kind", "path", "sha256")
_VERDICT_COLS = (
    "verdict_id",
    "run_id",
    "track",
    "name",
    "passed",
    "score",
    "detail_json",
    "judge_model",
    "rubric_version",
)
_COMPARISON_COLS = (
    "comparison_id",
    "baseline_config",
    "candidate_config",
    "task_set_sha",
    "stats_json",
    "alarm",
    "created_at",
)
_INJECTION_COLS = ("injection_id", "description", "config_patch_json")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create all v0.1 tables from schema.sql and return an open connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect(path)
    conn.executescript(schema)
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, table: str, cols: tuple[str, ...], row: dict[str, Any]) -> None:
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    values = [row.get(c) for c in cols]
    conn.execute(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", values)
    conn.commit()


def _get_by_pk(
    conn: sqlite3.Connection, table: str, pk_col: str, pk_val: Any
) -> dict[str, Any] | None:
    cur = conn.execute(f"SELECT * FROM {table} WHERE {pk_col} = ?", (pk_val,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def insert_task(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert one task row. Caller supplies all NOT NULL fields."""
    _insert(conn, "task", _TASK_COLS, row)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "task", "task_id", task_id)


def insert_config(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "config", _CONFIG_COLS, row)


def get_config(conn: sqlite3.Connection, config_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "config", "config_id", config_id)


def insert_run(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "run", _RUN_COLS, row)


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "run", "run_id", run_id)


def insert_event(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    """Insert one event; returns the auto-assigned event_id."""
    placeholders = ", ".join("?" for _ in _EVENT_COLS)
    col_list = ", ".join(_EVENT_COLS)
    values = [row.get(c) for c in _EVENT_COLS]
    cur = conn.execute(
        f"INSERT INTO event ({col_list}) VALUES ({placeholders})", values
    )
    conn.commit()
    return int(cur.lastrowid)


def get_events_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM event WHERE run_id = ? ORDER BY step_idx, event_id",
        (run_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def insert_artifact(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "artifact", _ARTIFACT_COLS, row)


def upsert_artifact(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Idempotently replace a deterministic artifact record."""
    placeholders = ", ".join("?" for _ in _ARTIFACT_COLS)
    columns = ", ".join(_ARTIFACT_COLS)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in _ARTIFACT_COLS
        if column != "artifact_id"
    )
    conn.execute(
        f"INSERT INTO artifact ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(artifact_id) DO UPDATE SET {updates}",
        [row.get(column) for column in _ARTIFACT_COLS],
    )
    conn.commit()


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "artifact", "artifact_id", artifact_id)


def insert_verdict(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "verdict", _VERDICT_COLS, row)


def upsert_verdict(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Idempotently replace a verdict identified by its deterministic PK (D5-d)."""
    placeholders = ", ".join("?" for _ in _VERDICT_COLS)
    columns = ", ".join(_VERDICT_COLS)
    updates = ", ".join(
        f"{column}=excluded.{column}" for column in _VERDICT_COLS if column != "verdict_id"
    )
    conn.execute(
        f"INSERT INTO verdict ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(verdict_id) DO UPDATE SET {updates}",
        [row.get(column) for column in _VERDICT_COLS],
    )
    conn.commit()


def get_verdict(conn: sqlite3.Connection, verdict_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "verdict", "verdict_id", verdict_id)


def insert_comparison(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "comparison", _COMPARISON_COLS, row)


def get_comparison(conn: sqlite3.Connection, comparison_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "comparison", "comparison_id", comparison_id)


def insert_injection(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _insert(conn, "injection", _INJECTION_COLS, row)


def upsert_injection(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO injection(injection_id,description,config_patch_json) VALUES (?,?,?) "
        "ON CONFLICT(injection_id) DO UPDATE SET "
        "description=excluded.description,config_patch_json=excluded.config_patch_json",
        tuple(row.get(column) for column in _INJECTION_COLS),
    )
    conn.commit()


def get_injection(conn: sqlite3.Connection, injection_id: str) -> dict[str, Any] | None:
    return _get_by_pk(conn, "injection", "injection_id", injection_id)
