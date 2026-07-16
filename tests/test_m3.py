from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from traceverdict.m3 import (
    ALPHA_CONFIG_ID,
    BETA_CONFIG_ID,
    MIMO_REUSED_RUNS,
    MIMO_REUSED_TASKS,
    MAX_SEEN_RUN_USD,
    PROJECT_ACTUAL_BEFORE_M3,
    alpha_run_ids_from_completions,
    assert_no_existing_repetition,
    completion_path,
    compare_budget_identities,
    corrected_projection,
    materialize_m3_inputs,
    record_i3q_isolation,
    seed_m3_database,
)
from traceverdict.tracer import db as dbmod


def _task(conn, task_id: str = "S1") -> None:
    dbmod.insert_task(
        conn,
        {
            "task_id": task_id,
            "suite": "self",
            "source": "self",
            "repo_ref": "repo.bundle",
            "base_commit": "a" * 40,
            "image_ref": "image",
            "instruction": "x",
            "budget_json": "{}",
            "forbidden_json": "[]",
            "gt_type": "pytest",
            "gt_spec_json": "{}",
            "tags_json": "[]",
            "created_at": "now",
        },
    )


def _config(conn, config_id: str = "cfg") -> None:
    dbmod.insert_config(
        conn,
        {
            "config_id": config_id,
            "agent_name": "mini-swe-agent",
            "agent_version": "2.4.5",
            "model_name": "model",
            "model_params_json": "{}",
            "prompt_version": "p",
            "harness_version": "0.1.0",
            "notes": "",
        },
    )


def test_corrected_projection_uses_true_global_max():
    value = corrected_projection()
    assert value["basis_run_id"] == "run-a370ff1f7fb9"
    assert Decimal(value["remaining_usd"]) == MAX_SEEN_RUN_USD * 85
    assert Decimal(value["unique_104_total_usd"]) == PROJECT_ACTUAL_BEFORE_M3 + MAX_SEEN_RUN_USD * 85
    assert Decimal(value["gross_152_total_usd"]) == PROJECT_ACTUAL_BEFORE_M3 + MAX_SEEN_RUN_USD * 133


def test_completion_path_is_config_and_repetition_scoped(tmp_path: Path):
    assert completion_path(tmp_path, config_id="cfg", repetition_idx=1, task_id="S1") == (
        tmp_path / "runs" / "cfg" / "r1" / "S1.json"
    )


def test_paid_repetition_guard(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    conn = dbmod.init_db(db)
    _task(conn)
    _config(conn)
    dbmod.insert_run(
        conn,
        {
            "run_id": "run-1",
            "task_id": "S1",
            "config_id": "cfg",
            "repetition_idx": 0,
            "mode": "scenario",
            "status": "ok",
            "cost_usd": 0.1,
        },
    )
    conn.close()
    with pytest.raises(RuntimeError, match="repetition already exists"):
        assert_no_existing_repetition(db, task_id="S1", config_id="cfg", repetition_idx=0)
    assert_no_existing_repetition(db, task_id="S1", config_id="cfg", repetition_idx=1)


def test_i3q_isolation_accepts_empty_patch_aggregate(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    conn = dbmod.init_db(db)
    _task(conn)
    _config(conn)
    _run(conn, run_id="run-empty", task_id="S1", config_id="cfg")
    aggregate = tmp_path / "aggregate.json"
    aggregate.write_text('{"empty_patch_ids":["S1"]}\n', "utf-8")
    dbmod.insert_artifact(
        conn,
        {
            "artifact_id": "run-empty-swebench_aggregate",
            "run_id": "run-empty",
            "kind": "swebench_aggregate",
            "path": str(aggregate),
            "sha256": "a" * 64,
        },
    )
    conn.close()
    mini = tmp_path / "artifacts" / "run-empty" / "adapter" / "mini_config.yaml"
    mini.parent.mkdir(parents=True)
    mini.write_text(
        "environment:\n  run_args:\n    - --volume\n    - /tmp/work:/testbed:ro\n",
        "utf-8",
    )

    evidence_path = record_i3q_isolation(
        output=tmp_path, db_path=db, run_id="run-empty", task_id="S1"
    )

    import json

    evidence = json.loads(evidence_path.read_text("utf-8"))
    assert evidence["agent_read_only"] is True
    assert evidence["verifier_artifact_kind"] == "swebench_aggregate"
    conn = dbmod._connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM artifact WHERE run_id='run-empty' AND kind='i3q_isolation'"
    ).fetchone()[0] == 1
    conn.close()


def _run(conn, *, run_id: str, task_id: str, config_id: str, cost: float = 0.01):
    dbmod.insert_run(
        conn,
        {
            "run_id": run_id,
            "task_id": task_id,
            "config_id": config_id,
            "repetition_idx": 0,
            "mode": "scenario",
            "status": "ok",
            "cost_usd": cost,
        },
    )


def test_seed_database_imports_only_approved_beta_runs_and_rekeys_events(tmp_path: Path):
    m2 = tmp_path / "m2.sqlite"
    mimo = tmp_path / "mimo.sqlite"
    target = tmp_path / "m3.sqlite"
    left = dbmod.init_db(m2)
    _config(left, ALPHA_CONFIG_ID)
    task_ids = [*MIMO_REUSED_TASKS, *(f"T{index:02d}" for index in range(3, 16))]
    for index, task_id in enumerate(task_ids):
        _task(left, task_id)
        _run(left, run_id=f"alpha-{index}", task_id=task_id, config_id=ALPHA_CONFIG_ID)
        dbmod.insert_event(
            left,
            {
                "run_id": f"alpha-{index}",
                "step_idx": 0,
                "ts": "now",
                "etype": "final",
                "payload_json": "{}",
            },
        )
    left.close()

    right = dbmod.init_db(mimo)
    _config(right, BETA_CONFIG_ID)
    for task_id, run_id in zip(MIMO_REUSED_TASKS, MIMO_REUSED_RUNS, strict=True):
        _task(right, task_id)
        _run(right, run_id=run_id, task_id=task_id, config_id=BETA_CONFIG_ID)
        dbmod.insert_event(
            right,
            {
                "run_id": run_id,
                "step_idx": 0,
                "ts": "now",
                "etype": "final",
                "payload_json": "{}",
            },
        )
        dbmod.insert_artifact(
            right,
            {
                "artifact_id": f"{run_id}-log",
                "run_id": run_id,
                "kind": "log",
                "path": "x",
                "sha256": "a" * 64,
            },
        )
        dbmod.insert_verdict(
            right,
            {
                "verdict_id": f"{run_id}-verdict",
                "run_id": run_id,
                "track": "rule",
                "name": "patch_valid",
                "passed": 1,
                "detail_json": "{}",
                "rubric_version": "v1",
            },
        )
    right.close()

    # Retain two historical harness failures in the M2 source DB; the seed is
    # expected to select only the 16 sealed baseline completions.
    left = dbmod._connect(m2)
    _run(
        left,
        run_id="diagnostic-1",
        task_id=MIMO_REUSED_TASKS[0],
        config_id=ALPHA_CONFIG_ID,
    )
    left.execute(
        "UPDATE run SET status='harness_error', cost_usd=NULL "
        "WHERE run_id='diagnostic-1'"
    )
    left.commit()
    left.close()

    alpha_run_ids = tuple(f"alpha-{index}" for index in range(16))
    seed_m3_database(
        m2_db=m2,
        mimo_db=mimo,
        target_db=target,
        alpha_run_ids=alpha_run_ids,
    )
    conn = dbmod._connect(target)
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 19
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 19
    assert conn.execute(
        "SELECT COUNT(*) FROM run WHERE config_id=?", (BETA_CONFIG_ID,)
    ).fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 3
    conn.close()


def test_alpha_run_ids_are_selected_from_sealed_completions(tmp_path: Path):
    import json

    db = tmp_path / "m2.sqlite"
    conn = dbmod.init_db(db)
    _config(conn, ALPHA_CONFIG_ID)
    task_ids = [f"T{index:02d}" for index in range(16)]
    output = tmp_path / "m2"
    (output / "runs").mkdir(parents=True)
    for index, task_id in enumerate(task_ids):
        _task(conn, task_id)
        run_id = f"approved-{index}"
        _run(conn, run_id=run_id, task_id=task_id, config_id=ALPHA_CONFIG_ID)
        (output / "runs" / f"{task_id}.json").write_text(
            json.dumps({"run_id": run_id, "status": "ok"}), "utf-8"
        )
    _run(conn, run_id="diagnostic", task_id=task_ids[0], config_id=ALPHA_CONFIG_ID)
    conn.execute("UPDATE run SET status='harness_error' WHERE run_id='diagnostic'")
    conn.commit()
    conn.close()
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("\n".join(task_ids) + "\n", "utf-8")

    selected = alpha_run_ids_from_completions(
        task_set=task_set, m2_output=output, m2_db=db
    )
    assert selected == tuple(f"approved-{index}" for index in range(16))
    assert "diagnostic" not in selected


def test_materialize_m3_inputs_is_replayable_and_refuses_overwrite(tmp_path: Path):
    m2 = tmp_path / "m2"
    (m2 / "tasks" / "S1").mkdir(parents=True)
    (m2 / "tasks" / "S1" / "task.yaml").write_text("id: S1\n", "utf-8")
    original_task_bytes = (m2 / "tasks" / "S1" / "task.yaml").read_bytes()
    (m2 / "image_records.json").write_text("[]\n", "utf-8")
    (m2 / "budget_identity.json").write_text("{}\n", "utf-8")
    output = tmp_path / "m3"
    first = materialize_m3_inputs(m2_output=m2, output=output)
    assert set(first) == {"image_records.json", "budget_identity.json", "tasks_tree"}
    assert (output / "tasks" / "S1" / "task.yaml").read_bytes() == original_task_bytes
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize_m3_inputs(m2_output=m2, output=output)


def _budget_identity(*, config_id: str, instance_id: str, with_status: bool) -> dict:
    semantics = {
        "max_steps": {
            "counter": "DefaultAgent.n_calls",
            "definition": "query boundary",
            "enforcement": "mini_agent_query_boundary",
        },
        "max_tokens": {
            "counter": None,
            "definition": "declared only",
            "enforcement": "none",
        },
    }
    if with_status:
        semantics["max_steps"]["status"] = "enforced"
        semantics["max_tokens"]["status"] = "recorded-inert"
    return {
        "budget": {
            "max_cost_usd": 1.0,
            "max_steps": 100,
            "max_tokens": 250000,
            "max_wall_s": 3600,
        },
        "budget_block_sha256": "a" * 64,
        "config_id": config_id,
        "records": [
            {
                "agent_tool_timeout_s": 60,
                "budget_block_sha256": "a" * 64,
                "instance_id": instance_id,
                "max_tokens_enforcement": "none",
                "mini_agent_limits": {
                    "cost_limit": 1.0,
                    "step_limit": 100,
                    "wall_time_limit_seconds": 3600,
                },
            }
        ],
        "semantics": semantics,
    }


def test_budget_identity_comparison_allows_only_evidence_wrapper_differences(
    tmp_path: Path,
):
    m2 = tmp_path / "m2.json"
    mimo = tmp_path / "mimo.json"
    import json

    m2.write_text(
        json.dumps(
            _budget_identity(
                config_id=ALPHA_CONFIG_ID, instance_id="old-sample", with_status=False
            )
        ),
        "utf-8",
    )
    mimo.write_text(
        json.dumps(
            _budget_identity(
                config_id=BETA_CONFIG_ID, instance_id="new-sample", with_status=True
            )
        ),
        "utf-8",
    )
    result = compare_budget_identities(m2_path=m2, mimo_path=mimo)
    assert result["status"] == "normalized-identical"
    assert result["m2"]["source_sha256"] != result["mimo"]["source_sha256"]
    assert result["m2"]["sample_instance_ids"] == ["old-sample"]
    assert result["mimo"]["semantic_statuses"]["max_tokens"] == "recorded-inert"


def test_budget_identity_comparison_rejects_semantic_drift(tmp_path: Path):
    m2 = tmp_path / "m2.json"
    mimo = tmp_path / "mimo.json"
    import json

    left = _budget_identity(
        config_id=ALPHA_CONFIG_ID, instance_id="old-sample", with_status=False
    )
    right = _budget_identity(
        config_id=BETA_CONFIG_ID, instance_id="new-sample", with_status=True
    )
    right["records"][0]["mini_agent_limits"]["step_limit"] = 99
    m2.write_text(json.dumps(left), "utf-8")
    mimo.write_text(json.dumps(right), "utf-8")
    with pytest.raises(RuntimeError, match="normalized budget identity differs"):
        compare_budget_identities(m2_path=m2, mimo_path=mimo)
