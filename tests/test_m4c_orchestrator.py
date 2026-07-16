from __future__ import annotations

import sqlite3

import pytest

from scripts.run_m4c import _completed_run


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE run (
        run_id TEXT, task_id TEXT, config_id TEXT, finished_at TEXT,
        started_at TEXT, tokens_in INTEGER
        )"""
    )
    return db


def test_completed_run_ignores_zero_api_harness_error():
    db = _db()
    db.execute("INSERT INTO run VALUES ('bad','S1','cfg','t','1',NULL)")
    db.execute("INSERT INTO run VALUES ('good','S1','cfg','t','2',10)")
    assert _completed_run(db, "S1", "cfg")["run_id"] == "good"


def test_completed_run_rejects_duplicate_paid_completion():
    db = _db()
    db.execute("INSERT INTO run VALUES ('a','S1','cfg','t','1',10)")
    db.execute("INSERT INTO run VALUES ('b','S1','cfg','t','2',11)")
    with pytest.raises(RuntimeError, match="duplicate paid completion"):
        _completed_run(db, "S1", "cfg")
