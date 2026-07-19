from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from traceverdict.cli import app
from traceverdict.core.simple_yaml import dump_to_path, load_path
from traceverdict.radar import (
    RadarBudgetPause,
    RadarError,
    RadarPaths,
    add_watch,
    confirm,
    report,
    set_baseline,
    set_budget,
    tick,
)
from traceverdict.tracer import db as dbmod


def _config(tmp_path: Path) -> Path:
    (tmp_path / "registry.json").write_text(json.dumps({"vendor/model": {
        "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
        "litellm_provider": "openai", "mode": "chat",
    }}), "utf-8")
    path = tmp_path / "config.yaml"
    dump_to_path(path, {
        "config_id": "daily-radar-config", "agent_name": "mini-swe-agent",
        "agent_version": "2.4.5", "model_name": "vendor/model",
        "model_params": {}, "prompt_version": "v0", "harness_version": "0.3.0",
        "litellm_model_registry": str(tmp_path / "registry.json"),
        "notes": "identity_sha256=" + "a" * 64,
    })
    return path


def _runner(task_path, config_path, *, db_path, artifacts_dir, repetition_idx):
    task = Path(task_path).name
    cfg = load_path(config_path)
    conn = dbmod._connect(db_path) if Path(db_path).exists() else dbmod.init_db(db_path)
    if not conn.execute("SELECT 1 FROM config WHERE config_id=?", (cfg["config_id"],)).fetchone():
        conn.execute("INSERT INTO config VALUES (?,?,?,?,?,?,?,?)", (cfg["config_id"], "mini-swe-agent", "2.4.5", "vendor/model", "{}", "v0", "0.3.0", None))
    if not conn.execute("SELECT 1 FROM task WHERE task_id=?", (task,)).fetchone():
        conn.execute("INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (task,"self","self","repo.bundle","a"*40,"image","fix","{}","[]","pytest","{}","[]","now"))
    run_id = f"run-{task}-{repetition_idx}"
    tokens = 100 if repetition_idx == 0 else 200
    conn.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id,task,cfg["config_id"],repetition_idx,"scenario","ok","Submitted",None,None,10.0,tokens-1,1,0.01,None,"fp"))
    for name, passed in (("patch_valid",1),("forbidden",1)):
        conn.execute("INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)", (f"{run_id}-{name}",run_id,"rule",name,passed,None,"{}",None,"rule"))
    conn.commit(); conn.close()
    return {"run_id": run_id, "status": "ok"}


def test_radar_signal_confirm_and_budget_ledger(tmp_path: Path) -> None:
    paths = RadarPaths.at(tmp_path / "radar")
    config = _config(tmp_path)
    add_watch(config, set_name="quick", paths=paths)
    set_budget(project_actual_usd=1.0, monthly_limit_usd=3.0, paths=paths)
    first = tick(only=None, paths=paths, runner=_runner, verifier=lambda *a: [], tasks_root=tmp_path)
    assert first["level"] == "clean"
    second = tick(only=None, paths=paths, runner=_runner, verifier=lambda *a: [], tasks_root=tmp_path)
    assert second["level"] == "signal"
    signal_id = second["ticks"][0]["signal_id"]
    result = confirm(signal_id, paths=paths, runner=_runner, verifier=lambda *a: [], tasks_root=tmp_path)
    assert result["level"] == "confirmed"
    assert all(v["confirmed_reasons"] == ["tokens"] for v in result["confirmation"].values())
    summary = report(days=7, paths=paths)
    assert summary["monthly_actual_usd"] == pytest.approx(0.12)


def test_monthly_soft_limit_pauses_before_tick(tmp_path: Path) -> None:
    paths = RadarPaths.at(tmp_path / "radar")
    set_budget(project_actual_usd=0, monthly_limit_usd=0.01, paths=paths)
    paths.ledger.write_text(json.dumps({
        "version": 1, "project_actual_seed_usd": 0,
        "monthly_limit_usd": 0.01,
        "entries": [{"tick_id":"x","month_utc":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m"),"cost_usd":0.01,"billing_mode":"api_metered"}],
    }), "utf-8")
    with pytest.raises(RadarBudgetPause):
        tick(only=None, paths=paths)


def test_signal_is_withdrawn_when_two_targeted_repeats_do_not_reproduce(tmp_path: Path) -> None:
    paths = RadarPaths.at(tmp_path / "radar")
    config = _config(tmp_path)
    add_watch(config, set_name="quick", paths=paths)
    set_budget(project_actual_usd=0, monthly_limit_usd=3, paths=paths)

    def noisy_once_runner(task_path, config_path, *, db_path, artifacts_dir, repetition_idx):
        result = _runner(
            task_path,
            config_path,
            db_path=db_path,
            artifacts_dir=artifacts_dir,
            repetition_idx=repetition_idx,
        )
        if repetition_idx >= 2:
            conn = dbmod._connect(db_path)
            conn.execute(
                "UPDATE run SET tokens_in=99, tokens_out=1 WHERE run_id=?",
                (result["run_id"],),
            )
            conn.commit()
            conn.close()
        return result

    tick(only=None, paths=paths, runner=noisy_once_runner, verifier=lambda *a: [], tasks_root=tmp_path)
    signalled = tick(only=None, paths=paths, runner=noisy_once_runner, verifier=lambda *a: [], tasks_root=tmp_path)
    result = confirm(
        signalled["ticks"][0]["signal_id"],
        paths=paths,
        runner=noisy_once_runner,
        verifier=lambda *a: [],
        tasks_root=tmp_path,
    )
    assert result["level"] == "withdrawn"
    assert all(not row["confirmed_reasons"] for row in result["confirmation"].values())


def test_missing_baseline_window_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RadarError, match="no matching tick"):
        set_baseline(name="missing", tick_id=None, paths=RadarPaths.at(tmp_path / "radar"))


def test_cli_exit_matrix_is_explicit(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    monkeypatch.setattr("traceverdict.radar.tick", lambda **k: {"level":"signal","ticks":[]})
    result = runner.invoke(app, ["radar","tick","--state-dir",str(tmp_path)])
    assert result.exit_code == 3
    monkeypatch.setattr("traceverdict.radar.confirm", lambda *a, **k: {"level":"confirmed"})
    result = runner.invoke(app, ["radar","confirm","signal-x","--state-dir",str(tmp_path)])
    assert result.exit_code == 1
    monkeypatch.setattr("traceverdict.radar.confirm", lambda *a, **k: {"level":"withdrawn"})
    result = runner.invoke(app, ["radar","confirm","signal-x","--state-dir",str(tmp_path)])
    assert result.exit_code == 0
