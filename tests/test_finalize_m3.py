from __future__ import annotations

import json
import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from traceverdict.m3 import (
    ALPHA_CONFIG_ID,
    BETA_CONFIG_ID,
    EXP_C_CONFIG_ID,
    I3Q_CONFIG_ID,
    MIMO_REUSED_RUNS,
    MIMO_REUSED_TASKS,
)
from traceverdict.tracer import db as dbmod

_SCRIPT = Path(__file__).parents[1] / "scripts" / "finalize_m3.py"
_SPEC = importlib.util.spec_from_file_location("traceverdict_finalize_m3_script", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SELF_TASKS = _MODULE.SELF_TASKS
finalize = _MODULE.finalize


def _config(conn, config_id: str) -> None:
    dbmod.insert_config(
        conn,
        {
            "config_id": config_id,
            "agent_name": "agent",
            "agent_version": "1",
            "model_name": "model",
            "model_params_json": "{}",
            "prompt_version": "p",
            "harness_version": "h",
            "notes": "test",
        },
    )


def _task(conn, task_id: str, *, suite: str) -> None:
    dbmod.insert_task(
        conn,
        {
            "task_id": task_id,
            "suite": suite,
            "source": suite,
            "repo_ref": "bundle",
            "base_commit": "a" * 40,
            "image_ref": "image",
            "instruction": "fix",
            "budget_json": "{}",
            "forbidden_json": "[]",
            "gt_type": "pytest" if suite == "self" else "swebench",
            "gt_spec_json": "{}",
            "tags_json": "[]",
            "created_at": "now",
        },
    )


def _run(
    conn,
    *,
    run_id: str,
    task_id: str,
    config_id: str,
    rep: int,
    passed: bool,
    swebench: bool = False,
) -> None:
    dbmod.insert_run(
        conn,
        {
            "run_id": run_id,
            "task_id": task_id,
            "config_id": config_id,
            "repetition_idx": rep,
            "mode": "scenario",
            "status": "ok",
            "exit_reason": "Submitted",
            "wall_time_s": 10.0,
            "tokens_in": 100,
            "tokens_out": 10,
            "cost_usd": 0.001,
            "env_fingerprint": f"fingerprint-{task_id}",
        },
    )
    verdicts = (("swebench", passed),) if swebench else (
        ("patch_valid", passed),
        ("forbidden", True),
    )
    for name, value in verdicts:
        dbmod.insert_verdict(
            conn,
            {
                "verdict_id": f"{run_id}-{name}",
                "run_id": run_id,
                "track": "rule",
                "name": name,
                "passed": int(value),
                "detail_json": "{}",
                "rubric_version": "v1",
            },
        )


def test_finalize_m3_validates_sealed_matrix_and_writes_reports(tmp_path: Path):
    repo = Path(__file__).parents[1]
    task_set = repo / "benchmarks" / "swebv_subset_v1.txt"
    public_tasks = tuple(task_set.read_text("utf-8").splitlines())
    db = tmp_path / "m3.sqlite"
    conn = dbmod.init_db(db)
    for config_id in (ALPHA_CONFIG_ID, BETA_CONFIG_ID, I3Q_CONFIG_ID, EXP_C_CONFIG_ID):
        _config(conn, config_id)
    for task_id in public_tasks:
        _task(conn, task_id, suite="swebv")
    for task_id in SELF_TASKS:
        _task(conn, task_id, suite="self")

    for config_id in (ALPHA_CONFIG_ID, BETA_CONFIG_ID, I3Q_CONFIG_ID):
        for task_id in public_tasks:
            for rep in (0, 1):
                if (
                    config_id == BETA_CONFIG_ID
                    and rep == 0
                    and task_id in MIMO_REUSED_TASKS
                ):
                    run_id = MIMO_REUSED_RUNS[MIMO_REUSED_TASKS.index(task_id)]
                else:
                    run_id = f"{config_id}-{task_id}-{rep}"
                _run(
                    conn,
                    run_id=run_id,
                    task_id=task_id,
                        config_id=config_id,
                        rep=rep,
                        passed=config_id != I3Q_CONFIG_ID,
                        swebench=True,
                )
                if config_id == I3Q_CONFIG_ID:
                    dbmod.upsert_artifact(
                        conn,
                        {
                            "artifact_id": f"{run_id}-isolation",
                            "run_id": run_id,
                            "kind": "i3q_isolation",
                            "path": "evidence.json",
                            "sha256": "b" * 64,
                        },
                    )
    completion_root = tmp_path / "m3"
    for task_id in SELF_TASKS:
        run_id = f"exp-c-{task_id}"
        _run(
            conn,
            run_id=run_id,
            task_id=task_id,
            config_id=EXP_C_CONFIG_ID,
            rep=0,
            passed=task_id == "S7",
        )
        path = completion_root / "runs" / EXP_C_CONFIG_ID / "r0" / f"{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"run_id": run_id, "trace_complete": True}), "utf-8"
        )
    conn.close()

    output = tmp_path / "final"
    result = finalize(
        Namespace(
            db=db,
            task_set=task_set,
            completion_root=completion_root,
            output=output,
            taxonomy_overrides=None,
        )
    )

    assert result["run_accounting"]["m3_new_paid_runs"] == 85
    assert result["comparisons"]["exp_b"]["alarm"] == "hard"
    assert result["comparisons"]["exp_a"]["repeat_stability"]["direction_stable"] is True
    assert result["cost_ledger"]["m3_new_paid_runs_usd"] == pytest.approx(0.085)
    assert result["i3q_isolation_evidence_count"] == 32
    assert (output / "m3_summary.json").is_file()
    assert (output / "m3_summary.md").is_file()
    assert (output / "exp_a_model_comparison.md").is_file()
    assert (output / "exp_b_i3q_regression.md").is_file()
