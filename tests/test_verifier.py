from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.patch import collect_patch
from traceverdict.snapshot.workspace import cleanup_work_copy, materialize_work_copy
from traceverdict.tracer.db import init_db
from traceverdict.verifier import RULE_RUBRIC_VERSION, verdict_id, verify_run

ROOT = Path(__file__).resolve().parents[1]


def _insert_task_config_run(conn, task_path: Path, run_id: str, *, status: str, exit_reason: str):
    task = load_task(task_path)
    conn.execute(
        "INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task["task_id"], task["suite"], task["source"], str(task["repo_ref_path"]),
            task["base_commit"], task["image_ref"], task["instruction"],
            json.dumps(task["budget"]), json.dumps(task["forbidden_paths"]),
            task["gt"]["type"], json.dumps(task["gt"]["spec"]), json.dumps(task["tags"]), "now",
        ),
    )
    conn.execute("INSERT INTO config VALUES (?,?,?,?,?,?,?,?)", ("cfg", "mini", "2.4.5", "model", "{}", "v0", "0.1.0", None))
    conn.execute(
        "INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, task["task_id"], "cfg", 0, "scenario", status, exit_reason, "a", "b", 1.0, 1, 1, 0.1, 1, "fp"),
    )
    conn.commit()


def test_verdict_id_is_deterministic():
    first = verdict_id("run", "rule", "budget", RULE_RUBRIC_VERSION)
    assert first == verdict_id("run", "rule", "budget", RULE_RUBRIC_VERSION)
    assert len(first) == 64


def test_budget_verdict_is_idempotent(tmp_path: Path):
    conn = init_db(tmp_path / "db.sqlite")
    try:
        task_path = ROOT / "tasks" / "self" / "S7"
        _insert_task_config_run(conn, task_path, "run-s7", status="budget", exit_reason="LimitsExceeded")
        verify_run(conn, "run-s7", task_path)
        verify_run(conn, "run-s7", task_path)
        rows = conn.execute("SELECT * FROM verdict WHERE run_id='run-s7'").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "budget"
        assert rows[0]["passed"] == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["budget"]["max_cost_usd"] == 0.00005
    finally:
        conn.close()


def _patch_artifact(conn, task_path: Path, run_id: str, tmp_path: Path, mutate):
    task = load_task(task_path)
    work = materialize_work_copy(task["repo_ref_path"], task["base_commit"])
    try:
        mutate(work)
        patch_text, patch_sha = collect_patch(work, task["base_commit"])
    finally:
        cleanup_work_copy(work)
    patch_path = tmp_path / f"{run_id}.diff"
    patch_path.write_text(patch_text, encoding="utf-8")
    conn.execute(
        "INSERT INTO artifact VALUES (?,?,?,?,?)",
        (f"{run_id}-patch", run_id, "patch", str(patch_path), patch_sha),
    )
    conn.commit()


def test_pytest_patch_verification_uses_disposable_copy(tmp_path: Path):
    conn = init_db(tmp_path / "db.sqlite")
    try:
        task_path = ROOT / "tasks" / "self" / "S1"
        _insert_task_config_run(conn, task_path, "run-s1", status="ok", exit_reason="Submitted")
        _patch_artifact(
            conn,
            task_path,
            "run-s1",
            tmp_path,
            lambda work: (work / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            ),
        )
        seen = []

        def fake_pytest(work, image, selectors, docker):
            seen.append((work, image, selectors))
            assert (work / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")
            return True, {"selectors": selectors, "returncode": 0}

        rows = verify_run(conn, "run-s1", task_path, pytest_runner=fake_pytest)
        assert {row["name"]: row["passed"] for row in rows} == {
            "fail_to_pass": 1,
            "forbidden": 1,
            "patch_valid": 1,
        }
        assert seen and not seen[0][0].exists()
    finally:
        conn.close()


def test_s6_forbidden_migration_is_detected(tmp_path: Path):
    conn = init_db(tmp_path / "db.sqlite")
    try:
        task_path = ROOT / "tasks" / "self" / "S6"
        _insert_task_config_run(conn, task_path, "run-s6", status="ok", exit_reason="Submitted")
        _patch_artifact(
            conn,
            task_path,
            "run-s6",
            tmp_path,
            lambda work: (work / "migrations" / "001_status.sql").write_text(
                "-- cheating mutation\n", encoding="utf-8"
            ),
        )
        rows = verify_run(
            conn,
            "run-s6",
            task_path,
            pytest_runner=lambda *args: (True, {"returncode": 0}),
        )
        verdicts = {row["name"]: row["passed"] for row in rows}
        assert verdicts["forbidden"] == 0
    finally:
        conn.close()


def test_s6_clean_frozen_file_ignores_checkout_line_endings(tmp_path: Path):
    conn = init_db(tmp_path / "db.sqlite")
    try:
        task_path = ROOT / "tasks" / "self" / "S6"
        _insert_task_config_run(conn, task_path, "run-s6-clean", status="agent_error", exit_reason="RepeatedFormatError")
        _patch_artifact(conn, task_path, "run-s6-clean", tmp_path, lambda work: None)
        rows = verify_run(
            conn,
            "run-s6-clean",
            task_path,
            pytest_runner=lambda *args: (False, {"returncode": 1}),
        )
        verdicts = {row["name"]: row["passed"] for row in rows}
        assert verdicts["patch_valid"] == 0
        assert verdicts["forbidden"] == 1
    finally:
        conn.close()


def test_verifier_cleanup_has_docker_ownership_repair_identity(tmp_path: Path):
    conn = init_db(tmp_path / "db.sqlite")
    try:
        task_path = ROOT / "tasks" / "self" / "S1"
        _insert_task_config_run(
            conn, task_path, "run-cleanup", status="agent_error", exit_reason="exit_format"
        )
        _patch_artifact(conn, task_path, "run-cleanup", tmp_path, lambda work: None)
        with patch("traceverdict.verifier.cleanup_work_copy") as cleanup:
            verify_run(
                conn,
                "run-cleanup",
                task_path,
                docker_executable="docker",
                pytest_runner=lambda *args: (False, {"returncode": 1}),
            )
        work = cleanup.call_args.args[0]
        assert work.name.startswith("traceverdict-work-")
        assert cleanup.call_args.kwargs == {
            "docker_executable": "docker",
            "image": "traceverdict/self-base:py3.12-v1",
        }
        # The mock deliberately skipped cleanup.
        cleanup_work_copy(work)
    finally:
        conn.close()
