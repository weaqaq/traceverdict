"""Smoke tests: CLI --help lists all seven subcommands."""

from __future__ import annotations

import json
from unittest.mock import patch

from click import unstyle
from typer.testing import CliRunner

from traceverdict.cli import app

runner = CliRunner()

EXPECTED_COMMANDS = (
    "run",
    "suite",
    "compare",
    "report",
    "inject",
    "replay",
    "selftest",
)


def test_help_lists_seven_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_COMMANDS:
        assert name in result.stdout


def test_stub_commands_exit_2() -> None:
    # T4 implements inject/selftest; replay remains the only gated stub.
    stubs = [c for c in EXPECTED_COMMANDS if c not in {"run", "suite", "compare", "report", "inject", "selftest"}]
    for name in stubs:
        result = runner.invoke(app, [name])
        assert result.exit_code == 2, f"{name} should exit 2"
        assert "Not implemented in v0.1" in result.stdout


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
