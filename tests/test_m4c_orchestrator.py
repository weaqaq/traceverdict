from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.run_m4c import _completed_run, _rebase_artifact_paths
from scripts.run_m4c_swebv import (
    _guard_unreconciled_attempts,
    _task_ids,
    _write_handoff,
)


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


def test_m4c_swebv_task_set_is_exactly_sixteen(tmp_path: Path):
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("\n".join(f"T{i}" for i in range(16)) + "\n", encoding="utf-8")
    assert _task_ids(task_set) == [f"T{i}" for i in range(16)]
    task_set.write_text("T0\nT0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="16 unique"):
        _task_ids(task_set)


def test_m4c_swebv_retry_guard_allows_only_zero_usage_quota_attempt():
    db = _db()
    db.execute("ALTER TABLE run ADD COLUMN status TEXT")
    db.execute("ALTER TABLE run ADD COLUMN exit_reason TEXT")
    db.execute(
        "INSERT INTO run VALUES ('quota','T1','cfg','t','1',NULL,'harness_error',"
        "'SubscriptionLimitExceeded')"
    )
    _guard_unreconciled_attempts(db, "T1", "cfg")
    db.execute(
        "INSERT INTO run VALUES ('unknown','T2','cfg','t','2',NULL,'harness_error','boom')"
    )
    with pytest.raises(RuntimeError, match="unreconciled prior attempt"):
        _guard_unreconciled_attempts(db, "T2", "cfg")


def test_m4c_swebv_handoff_rechecks_patch_sha(tmp_path: Path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE task (task_id TEXT, base_commit TEXT)")
    db.execute("CREATE TABLE artifact (run_id TEXT, kind TEXT, path TEXT, sha256 TEXT)")
    patch = tmp_path / "patch.diff"
    patch.write_bytes(b"diff --git a/a b/a\n")
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    db.execute("INSERT INTO task VALUES (?,?)", ("org__repo-1", "a" * 40))
    db.execute("INSERT INTO artifact VALUES (?,?,?,?)", ("run-1", "patch", str(patch), digest))
    row = {"task_id": "org__repo-1", "run_id": "run-1", "env_fingerprint": "sha256:agent+base"}
    result = _write_handoff(
        conn=db,
        row=row,
        record={"image_digest": "sha256:official"},
        config={"config_id": "cfg", "model_params": {"codex_binary_sha256": "b" * 64}},
        packages=tmp_path / "packages",
    )
    assert Path(result["path"]).is_file()
    assert result["manifest"]["patch_sha256"] == digest
