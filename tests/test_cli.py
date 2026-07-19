"""Smoke tests: CLI --help lists all ten v0.2 subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click import unstyle
from typer.testing import CliRunner

from traceverdict.cli import app
from traceverdict import __version__

runner = CliRunner()

EXPECTED_COMMANDS = (
    "run",
    "suite",
    "compare",
    "report",
    "inject",
    "replay",
    "selftest",
    "quick",
    "baseline",
    "ingest",
)


def test_help_lists_ten_subcommands() -> None:
    from typer.main import get_command
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_COMMANDS:
        assert name in result.stdout
    assert len(EXPECTED_COMMANDS) == 10
    assert set(get_command(app).commands) == set(EXPECTED_COMMANDS)


def test_v02_version_and_tv_alias() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    assert __version__ == "0.2.2"
    assert 'version = "0.2.2"' in text
    assert '"litellm==1.91.1"' in text
    assert 'tv = "traceverdict.cli:app"' in text


def test_stub_commands_exit_2() -> None:
    # T4 implements inject/selftest; replay remains the only gated stub.
    stubs = ["replay"]
    for name in stubs:
        result = runner.invoke(app, [name])
        assert result.exit_code == 2, f"{name} should exit 2"
        assert "Not implemented in v0.2" in result.stdout


def test_daily_help_contract() -> None:
    for command in ("quick", "baseline", "ingest"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
    result = runner.invoke(app, ["baseline", "--help"])
    assert "set" in result.stdout and "update" in result.stdout


def test_ingest_json_and_rich_disclose_heartbeat_counts(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_text(json.dumps({
        "timestamp": "2026-07-19T00:00:00Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": None},
    }) + "\n", encoding="utf-8")
    json_state = tmp_path / "json-state"
    result = runner.invoke(app, ["ingest", str(source), "--json", "--state-dir", str(json_state)])
    assert result.exit_code == 0
    value = json.loads(result.stdout)
    assert value["token_count_events"] == 1
    assert value["null_usage_heartbeats"] == 1

    rich_state = tmp_path / "rich-state"
    result = runner.invoke(app, ["ingest", str(source), "--state-dir", str(rich_state)])
    assert result.exit_code == 0
    assert "token events=1" in result.stdout
    assert "null heartbeats=1" in result.stdout


def test_baseline_set_uses_packaged_default_when_checkout_path_is_absent(
    tmp_path, monkeypatch
) -> None:
    from traceverdict.resources import resolve_daily_assets

    monkeypatch.chdir(tmp_path)
    with patch("traceverdict.daily.set_baseline", return_value={"ok": True}) as mocked:
        result = runner.invoke(
            app, ["baseline", "set", "--config", "configs/dev.yaml"]
        )
    assert result.exit_code == 0
    assert mocked.call_args.args[0] == resolve_daily_assets().configs / "dev.yaml"


def test_run_help_lists_config_option() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    # Rich may insert ANSI style sequences inside the option name in CI.
    assert "--config" in unstyle(result.stdout)


def test_suite_requires_dry_run() -> None:
    result = runner.invoke(
        app, ["suite", "tasks/self", "--config", "configs/dev.yaml"]
    )
    assert result.exit_code == 2
    assert "requires --dry-run" in result.stdout


def test_suite_dry_run_prints_json() -> None:
    payload = {
        "suite": "self",
        "config_id": "dev-deepseek-v4-flash-v2",
        "dry_run": True,
        "count": 8,
        "tasks": [],
    }
    with patch("traceverdict.core.suite.validate_suite", return_value=payload):
        result = runner.invoke(
            app,
            ["suite", "tasks/self", "--config", "configs/dev.yaml", "--dry-run"],
        )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["count"] == 8
