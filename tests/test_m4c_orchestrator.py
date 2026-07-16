from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.run_m4c import _completed_run, _rebase_artifact_paths


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


def test_rebase_artifact_paths_requires_matching_bytes(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE artifact (artifact_id TEXT, run_id TEXT, path TEXT, sha256 TEXT)"
    )
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    patch = run_dir / "patch.diff"
    patch.write_text("diff bytes\n", encoding="utf-8")
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    db.execute(
        "INSERT INTO artifact VALUES (?,?,?,?)",
        ("a", "run-1", "reports\\old\\run-1\\patch.diff", digest),
    )
    assert _rebase_artifact_paths(db, tmp_path) == 1
    assert Path(db.execute("SELECT path FROM artifact").fetchone()[0]) == patch.resolve()


def test_rebase_artifact_paths_rejects_hash_mismatch(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE artifact (artifact_id TEXT, run_id TEXT, path TEXT, sha256 TEXT)"
    )
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "patch.diff").write_text("changed\n", encoding="utf-8")
    db.execute(
        "INSERT INTO artifact VALUES (?,?,?,?)",
        ("a", "run-1", "reports\\old\\run-1\\patch.diff", "0" * 64),
    )
    with pytest.raises(RuntimeError, match="refusing artifact path repair"):
        _rebase_artifact_paths(db, tmp_path)
