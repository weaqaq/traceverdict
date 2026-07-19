from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceverdict.core.simple_yaml import dump_to_path, load_path
from traceverdict.adapters.mini_swe_agent import _build_mini_config
from traceverdict.daily import (
    DailyError,
    DailyPaths,
    FULL_TASKS,
    QUICK_TASKS,
    compare_daily,
    derive_config,
    execute_scope,
    parse_overrides,
    update_baseline,
)
from traceverdict.tracer import db as dbmod


def _base(tmp_path: Path) -> tuple[Path, Path]:
    configs = tmp_path / "configs"
    configs.mkdir()
    registry = configs / "litellm_models.json"
    registry.write_text(json.dumps({"vendor/model": {
        "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
        "litellm_provider": "openai", "mode": "chat",
    }}), encoding="utf-8")
    base = configs / "dev.yaml"
    dump_to_path(base, {
        "config_id": "parent", "agent_name": "mini-swe-agent", "agent_version": "2.4.5",
        "model_name": "vendor/model", "model_params": {"thinking": {"type": "enabled"}},
        "prompt_version": "v0", "harness_version": "0.1.0",
        "litellm_model_registry": "litellm_models.json",
    })
    return base, configs


def test_derived_config_is_order_independent_and_prompt_is_identity(tmp_path: Path) -> None:
    base, configs = _base(tmp_path)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Be exact.", encoding="utf-8")
    a, cfg_a = derive_config(base, set_values=["model_params.z=1", "model=vendor/model"], prompt_file=prompt, output_dir=tmp_path / "out", registries_dir=configs)
    b, cfg_b = derive_config(base, set_values=["model=vendor/model", "model_params.z=1"], prompt_file=prompt, output_dir=tmp_path / "out", registries_dir=configs)
    assert a == b
    assert cfg_a["config_id"] == cfg_b["config_id"]
    reserved = cfg_a["model_params"]["_traceverdict_system_prompt"]
    assert reserved["content"] == "Be exact."
    assert len(reserved["sha256"]) == 64


def test_registry_must_be_unique_and_overrides_are_restricted(tmp_path: Path) -> None:
    base, configs = _base(tmp_path)
    (configs / "litellm_models_copy.json").write_text((configs / "litellm_models.json").read_text(), encoding="utf-8")
    with pytest.raises(DailyError, match="exactly one"):
        derive_config(base, output_dir=tmp_path / "out", registries_dir=configs)
    with pytest.raises(DailyError, match="only model"):
        parse_overrides(["agent_name=other"])
    with pytest.raises(DailyError, match="JSON scalars"):
        parse_overrides(["model_params.x={\"a\":1}"])


def _insert_config(conn, cid: str) -> None:
    conn.execute("INSERT INTO config VALUES (?,?,?,?,?,?,?,?)", (cid, "mini-swe-agent", "2.4.5", "m", "{}", "p", "0.2.2", None))


def _insert_task(conn, task: str) -> None:
    conn.execute("INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        task, "self", "self", "repo.bundle", "a" * 40, "image", "instruction",
        "{}", "[]", "pytest", "{}", "[]", "2026-07-18T00:00:00Z",
    ))


def _insert_run(conn, cid: str, task: str, passed: bool, forbidden: bool, tokens: int, wall: float) -> None:
    rid = f"{cid}-{task}"
    conn.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rid, task, cid, 0, "scenario", "ok", "Submitted", None, None, wall, tokens - 1, 1, 0.01, None, "fp"))
    for name, value in (("patch_valid", passed), ("forbidden", not forbidden)):
        conn.execute("INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)", (f"{rid}-{name}", rid, "rule", name, int(value), None, "{}", None, "r"))


def _comparison_db(tmp_path: Path):
    db = tmp_path / "daily.db"
    conn = dbmod.init_db(db)
    _insert_config(conn, "base")
    _insert_config(conn, "candidate")
    for task in QUICK_TASKS:
        _insert_task(conn, task)
        _insert_run(conn, "base", task, True, False, 100, 10)
        _insert_run(conn, "candidate", task, task != "S4", task == "S6", 140, 16)
    conn.commit()
    return db, conn


def test_daily_fail_precedes_token_and_wall_warn(tmp_path: Path) -> None:
    _, conn = _comparison_db(tmp_path)
    result = compare_daily(conn, "base", "candidate", full=False)
    assert result["conclusion"] == "FAIL"
    assert result["correctness_regressions"] == ["S4", "S6"]
    assert result["new_forbidden"] == ["S6"]
    assert result["token_ratio"] == 1.4
    conn.close()


def test_baseline_update_never_runs_and_protects_regression(tmp_path: Path) -> None:
    db, conn = _comparison_db(tmp_path)
    conn.close()
    paths = DailyPaths.at(tmp_path / "state")
    paths.root.mkdir()
    paths.db.write_bytes(db.read_bytes())
    paths.baselines.write_text(json.dumps({"version": 1, "entries": {"default:quick": {"config_id": "base", "run_ids": {}}}}), encoding="utf-8")
    with pytest.raises(DailyError, match="accept-regression"):
        update_baseline("candidate", full=False, name="default", accept_regression=False, paths=paths)
    result = update_baseline("candidate", full=False, name="default", accept_regression=True, paths=paths)
    assert result["model_runs_started"] == 0
    assert json.loads(paths.baselines.read_text())["entries"]["default:quick"]["config_id"] == "candidate"


def test_daily_task_scopes_are_frozen() -> None:
    assert QUICK_TASKS == ("S1", "S4", "S6")
    assert FULL_TASKS == tuple(f"S{i}" for i in range(1, 9))


def test_prompt_reserved_identity_is_stripped_before_provider(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    prompt = "Custom system prompt"
    import hashlib
    mini = _build_mini_config(
        image="image", docker_executable="docker", host_work_path=tmp_path,
        container_cwd="/testbed", model_name="vendor/model",
        model_params={"_traceverdict_system_prompt": {"content": prompt, "sha256": hashlib.sha256(prompt.encode()).hexdigest()}},
        litellm_model_registry=registry, output_path=tmp_path / "traj.json",
        cost_limit=1.0, step_limit=10, wall_time_s=60,
    )
    assert mini["agent"]["system_template"] == prompt
    assert "_traceverdict_system_prompt" not in mini["model"]["model_kwargs"]


def test_quick_to_full_reuses_three_completions_and_runs_only_five(tmp_path: Path) -> None:
    base, _ = _base(tmp_path)
    paths = DailyPaths.at(tmp_path / "state")
    calls: list[str] = []

    def runner(task_path, config_path, *, db_path, artifacts_dir, repetition_idx):
        task_id = Path(task_path).name
        calls.append(task_id)
        conn = dbmod._connect(db_path) if Path(db_path).exists() else dbmod.init_db(db_path)
        cfg = load_path(config_path)
        if conn.execute("SELECT COUNT(*) FROM config WHERE config_id=?", (cfg["config_id"],)).fetchone()[0] == 0:
            _insert_config(conn, cfg["config_id"])
        if conn.execute("SELECT COUNT(*) FROM task WHERE task_id=?", (task_id,)).fetchone()[0] == 0:
            _insert_task(conn, task_id)
        _insert_run(conn, cfg["config_id"], task_id, True, False, 100, 10)
        conn.commit()
        conn.close()
        return {"run_id": f"{cfg['config_id']}-{task_id}", "status": "ok"}

    def verifier(conn, run_id, task_path):
        # The mocked runner already persisted deterministic rule verdicts.
        return []

    first = execute_scope(base, full=False, paths=paths, runner=runner, verifier=verifier, tasks_root=tmp_path / "tasks")
    second = execute_scope(base, full=True, paths=paths, runner=runner, verifier=verifier, tasks_root=tmp_path / "tasks")
    assert first["new"] == list(QUICK_TASKS)
    assert second["reused"] == list(QUICK_TASKS)
    assert second["new"] == ["S2", "S3", "S5", "S7", "S8"]
    assert len(calls) == 8
