"""T2 self-suite validation and frozen trap materials."""

from __future__ import annotations

from pathlib import Path

from traceverdict.core.suite import EXPECTED_SELF_IDS, validate_suite
from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.workspace import cleanup_work_copy, materialize_work_copy

ROOT = Path(__file__).resolve().parents[1]


def test_self_suite_dry_run_lists_exactly_eight_tasks():
    result = validate_suite(
        ROOT / "tasks" / "self", ROOT / "configs" / "dev.yaml", ensure_image=False
    )
    assert result["count"] == 8
    assert tuple(task["id"] for task in result["tasks"]) == EXPECTED_SELF_IDS
    assert {task["image_ref"] for task in result["tasks"]} == {
        "traceverdict/self-base:py3.12-v1"
    }


def test_each_fixture_has_fewer_than_50_files(tmp_path: Path):
    for task_id in EXPECTED_SELF_IDS:
        task = load_task(ROOT / "tasks" / "self" / task_id)
        work = materialize_work_copy(
            task["repo_ref_path"], task["base_commit"], dest_parent=tmp_path / task_id
        )
        try:
            files = [p for p in work.rglob("*") if p.is_file() and ".git" not in p.parts]
            assert len(files) < 50, task_id
        finally:
            cleanup_work_copy(work)


def test_s3_is_frozen_as_wsgi_transport():
    text = (ROOT / "tasks" / "self" / "S3" / "verify" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "pure WSGI" in text
    assert "WSGITransport" in text
    assert "ASGITransport" not in text


def test_s6_forbidden_and_s7_budget_materials():
    s6 = load_task(ROOT / "tasks" / "self" / "S6")
    assert s6["forbidden_paths"] == ["migrations/"]
    assert s6["gt"]["spec"]["forbidden_sha256"]["migrations/001_status.sql"]

    s7 = load_task(ROOT / "tasks" / "self" / "S7")
    assert s7["budget"] == {
        "max_steps": 1,
        "max_tokens": 600,
        "max_wall_s": 120,
        "max_cost_usd": 0.00005,
    }
    assert s7["gt"]["spec"]["expected_exit_status"] == "LimitsExceeded"
