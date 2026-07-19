from __future__ import annotations

import json
from pathlib import Path

from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.workspace import cleanup_work_copy, materialize_work_copy

ROOT = Path(__file__).resolve().parents[1]


def _work(task_id: str, tmp_path: Path) -> Path:
    task = load_task(ROOT / "tasks" / "self" / task_id)
    return materialize_work_copy(task["repo_ref_path"], task["base_commit"], dest_parent=tmp_path / task_id)


def test_s9_s10_are_explicit_ingest_rollbacks(tmp_path: Path) -> None:
    for task_id, missing in (("S9", "null_usage_heartbeats"), ("S10", "open_turn_underflow")):
        work = _work(task_id, tmp_path)
        try:
            text = (work / "src/traceverdict/ingest.py").read_text("utf-8")
            if task_id == "S9":
                assert 'current = _desktop_usage(payload.get("info"))' in text
            else:
                assert missing not in text
            readme = (ROOT / "tasks" / "self" / task_id / "verify" / "README.md").read_text("utf-8")
            assert "rollback" in readme.lower()
        finally:
            cleanup_work_copy(work)


def test_s11_keeps_registry_writable_and_freezes_evidence(tmp_path: Path) -> None:
    task = load_task(ROOT / "tasks" / "self" / "S11")
    assert "configs/litellm_models_mimo_v2.json" not in task["forbidden_paths"]
    assert task["forbidden_paths"] == ["tests/test_case.py", "tests/fixtures/mimo_usage.json"]
    work = _work("S11", tmp_path)
    try:
        registry = json.loads((work / "configs/litellm_models_mimo_v2.json").read_text("utf-8"))
        assert registry["xiaomi_mimo/mimo-v2.5"]["output_cost_per_reasoning_token"] == 0
    finally:
        cleanup_work_copy(work)
