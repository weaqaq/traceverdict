import json
import sqlite3
from pathlib import Path

import pytest

from scripts.finalize_m4c import _assert_task_identity, _task_ids


def test_m4c_finalizer_requires_exact_frozen_sixteen(tmp_path: Path):
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("\n".join(f"T{i}" for i in range(16)) + "\n", encoding="utf-8")
    assert _task_ids(task_set) == [f"T{i}" for i in range(16)]
    task_set.write_text("T0\nT0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly 16 unique"):
        _task_ids(task_set)


def test_m4c_finalizer_rejects_budget_identity_drift():
    baseline = sqlite3.connect(":memory:")
    candidate = sqlite3.connect(":memory:")
    baseline.row_factory = sqlite3.Row
    candidate.row_factory = sqlite3.Row
    schema = (
        "CREATE TABLE task(task_id TEXT PRIMARY KEY,base_commit TEXT,instruction TEXT,"
        "budget_json TEXT,gt_type TEXT,gt_spec_json TEXT)"
    )
    for conn, budget in ((baseline, {"max_steps": 100}), (candidate, {"max_steps": 99})):
        conn.execute(schema)
        conn.execute(
            "INSERT INTO task VALUES (?,?,?,?,?,?)",
            ("T", "a" * 40, "task", json.dumps(budget), "swebench", "{}"),
        )
    with pytest.raises(ValueError, match="budget_json"):
        _assert_task_identity(baseline, candidate, ["T"])
