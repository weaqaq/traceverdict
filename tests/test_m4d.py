from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from traceverdict.m4d import (
    ARM_SPECS,
    build_diagnostic,
    dataset_profile,
    exit_audit,
    find_test_runtimes,
    matrix,
)

_SCRIPT = Path(__file__).parents[1] / "scripts" / "analyze_m4d.py"
_SPEC = importlib.util.spec_from_file_location("traceverdict_analyze_m4d", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _rows(tasks: list[str]) -> dict[str, list[dict]]:
    result = {}
    for arm, spec in ARM_SPECS.items():
        result[arm] = [
            {
                "run_id": f"{arm}-{task}-{rep}",
                "task_id": task,
                "config_id": spec["config_id"],
                "repetition_idx": rep,
                "status": "ok",
                "exit_reason": "Submitted",
                "wall_time_s": None if arm == "pro" else 100.0 + rep,
                "passed": task == "pass" or (task == "tie" and arm == "flash" and rep == 0),
            }
            for task in tasks
            for rep in range(spec["repetitions"])
        ]
    return result


def _dataset(tasks: list[str]) -> list[dict]:
    return [
        {
            "instance_id": task,
            "patch": f"diff --git a/{task}.py b/{task}.py\n+line\n",
            "difficulty": "<15 min fix",
        }
        for task in tasks
    ]


def test_matrix_separates_zero_success_from_k2_tie():
    tasks = ["fail", "tie", "pass"]
    result = matrix(_rows(tasks), tasks)
    assert result["strict_all_fail"] == ["fail"]
    assert result["tie_sensitive"] == ["tie"]


def test_build_diagnostic_requires_exact_96_shape_for_sixteen_tasks():
    tasks = [f"t{i}" for i in range(16)]
    arms = _rows(tasks)
    arms["codex"].pop()
    with pytest.raises(RuntimeError, match="95 runs, expected 96"):
        build_diagnostic(
            arms=arms,
            task_ids=tasks,
            task_set_sha="a" * 64,
            verified_rows=_dataset(tasks),
            evidence={},
        )


def test_profiles_and_exit_wall_distance_are_descriptive():
    rows = [
        {"instance_id": "a", "patch": "diff --git a/a b/a\n123", "difficulty": "easy"},
        {
            "instance_id": "b",
            "patch": "diff --git a/a b/a\n1\ndiff --git a/b b/b\n2",
            "difficulty": "hard",
        },
    ]
    profile = dataset_profile(rows)
    assert profile["files"]["multi_file_fraction"] == 0.5
    arms = _rows(["fail"])
    arms["codex"][0]["wall_time_s"] = 3590.0
    audit = exit_audit(arms)
    assert audit["arms"]["codex"]["wall"]["within_60s"] == 1
    assert audit["arms"]["pro"]["wall"]["reason"] == "source has no wall_time_s"


def test_missing_official_runtime_is_fail_closed(tmp_path: Path):
    assert find_test_runtimes([tmp_path])["status"] == "unavailable"
    log = tmp_path / "run_instance.log"
    log.write_text("Test runtime: 1_234.50 seconds\n", "utf-8")
    found = find_test_runtimes([tmp_path])
    assert found["status"] == "available"
    assert found["values_found"] == 1


def test_report_discloses_missing_pro_wall_and_runtime():
    tasks = [f"t{i}" for i in range(16)]
    data = build_diagnostic(
        arms=_rows(tasks),
        task_ids=tasks,
        task_set_sha="a" * 64,
        verified_rows=_dataset(tasks),
        evidence={
            "main_db_sha256": "b" * 64,
            "codex_db_sha256": "c" * 64,
            "pro": {"kind": "public_summary_fallback", "sha256": "d" * 64},
            "verified_arrow_sha256": "e" * 64,
        },
    )
    report = _MODULE.render_markdown(data)
    assert "Pro wall-time is unavailable" in report
    assert "Official test runtime: **unavailable**" in report
    assert "no model call" in report
