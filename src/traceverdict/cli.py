"""TraceVerdict public CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="traceverdict",
    help="TraceVerdict: stateful regression evaluation for coding agents.",
    no_args_is_help=True,
)
baseline_app = typer.Typer(help="Manage cached Daily Mode baselines.", no_args_is_help=True)
app.add_typer(baseline_app, name="baseline")

_UNIMPLEMENTED = "Not implemented in v0.2"


def _not_implemented() -> None:
    typer.echo(_UNIMPLEMENTED)
    raise typer.Exit(code=2)


@app.command()
def run(
    task: Path = typer.Argument(..., help="Task directory, e.g. tasks/self/S1"),
    config: Path = typer.Option(
        ...,
        "--config",
        help="Harness config YAML path, e.g. configs/dev.yaml",
    ),
    db: Path = typer.Option(
        Path("reports/traceverdict.db"),
        "--db",
        help="SQLite database path",
    ),
    artifacts: Path = typer.Option(
        Path("reports/artifacts"),
        "--artifacts",
        help="Artifact output directory",
    ),
) -> None:
    """Run a single task via DockerEnvironment + mini-swe-agent (strict D1-d)."""
    from traceverdict.core.runner import run_task

    result = run_task(task, config, db_path=db, artifacts_dir=artifacts)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "harness_error":
        raise typer.Exit(code=1)


@app.command()
def suite(
    suite_path: Path = typer.Argument(..., help="Suite directory, e.g. tasks/self"),
    config: Path = typer.Option(..., "--config", help="Harness config YAML path"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate/list without running tasks"),
) -> None:
    """Validate and list a task suite; execution remains explicitly gated."""
    if not dry_run:
        typer.echo("suite execution requires --dry-run in T2")
        raise typer.Exit(code=2)
    from traceverdict.core.suite import validate_suite

    result = validate_suite(suite_path, config)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def compare(
    baseline: str = typer.Option(..., "--baseline"),
    candidate: str = typer.Option(..., "--candidate"),
    task_set: Path = typer.Option(Path("tasks/self/task_set.txt"), "--task-set"),
    db: Path = typer.Option(Path("reports/traceverdict.db"), "--db"),
    taxonomy_overrides: Optional[Path] = typer.Option(None, "--taxonomy-overrides"),
    allow_asymmetric_repetitions: bool = typer.Option(
        False,
        "--allow-asymmetric-repetitions",
        help="Explicitly permit unequal per-task k; both sides are disclosed",
    ),
    allow_unpriced_candidate: bool = typer.Option(
        False,
        "--allow-unpriced-candidate",
        help="Permit NULL actual cost only for subscription_unallocatable candidates",
    ),
) -> None:
    """Compare two configs on an explicit frozen task set."""
    from traceverdict.compare import compare_configs

    result = compare_configs(
        baseline,
        candidate,
        task_set,
        db_path=db,
        taxonomy_overrides_path=taxonomy_overrides,
        allow_asymmetric_repetitions=allow_asymmetric_repetitions,
        allow_unpriced_candidate=allow_unpriced_candidate,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def report(
    comparison_id: str = typer.Argument(...),
    db: Path = typer.Option(Path("reports/traceverdict.db"), "--db"),
    output: Optional[Path] = typer.Option(None, "--output"),
) -> None:
    """Render a stored comparison as Rich terminal output and Markdown."""
    from traceverdict.report import generate_report

    result = generate_report(comparison_id, db_path=db, output_path=output)
    typer.echo(json.dumps({"comparison_id": result["comparison_id"], "alarm": result["alarm"], "output": result["output"]}, ensure_ascii=False, indent=2))


@app.command()
def inject(
    injection_id: str = typer.Argument(
        ..., help="Injection ID I1/I1P/I1Q/I2/I2P/I3/I3Q/I4/I4Q/I5/I5P"
    ),
    base: Path = typer.Option(..., "--base", help="Canonical base config YAML"),
    output: Path = typer.Option(..., "--output", help="Generated config YAML"),
    session_id: str = typer.Option("manual", "--session-id"),
) -> None:
    """Generate a config containing one faithful mini injection."""
    from traceverdict.injections import generate_injected_config

    result = generate_injected_config(injection_id, base, output, session_id=session_id)
    typer.echo(json.dumps({"output": str(output), "config_id": result["config_id"], "notes": result["notes"]}, ensure_ascii=False, indent=2))


@app.command()
def replay() -> None:
    """Deterministic replay of a recorded fixture (stub)."""
    _not_implemented()


@app.command()
def selftest(
    config: Path = typer.Option(..., "--config"),
    task_set: Path = typer.Option(Path("tasks/self/task_set.txt"), "--task-set"),
    db: Path = typer.Option(Path("reports/traceverdict.db"), "--db"),
    output: Path = typer.Option(Path("reports/selftest"), "--output"),
    artifacts: Path = typer.Option(Path("reports/artifacts"), "--artifacts"),
    session_id: Optional[str] = typer.Option(None, "--session-id"),
) -> None:
    """Run all four M1 technical gates with Battery v2."""
    from traceverdict.core.selftest import run_selftest

    result = run_selftest(
        config,
        task_set,
        db_path=db,
        output_dir=output,
        artifacts_dir=artifacts,
        session_id=session_id,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("passed"):
        raise typer.Exit(code=1)


def _daily_echo(result: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"Daily Mode — {result.get('conclusion', 'BASELINE')}")
    table.add_column("Metric")
    table.add_column("Value")
    for key in (
        "candidate_config_id", "baseline_config_id", "scope", "delta_pass",
        "delta_tokens_median", "token_ratio", "delta_wall_p95", "wall_ratio",
        "actual_cost_usd", "reused_task_count", "new_task_count", "failed_tasks",
    ):
        if key in result:
            table.add_row(key, json.dumps(result[key], ensure_ascii=False))
    Console().print(table)


def _ingest_echo(result: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"Passive ingest — {result['sources_updated']} source(s) updated")
    for name in ("UTC date", "model", "tokens in/cached/out/reasoning", "turns", "tools", "failures", "open"):
        table.add_column(name)
    for row in sorted(result["last_7_days"], key=lambda item: (item["date_utc"], item["model"])):
        failures = sum(int(value) for value in row["failures"].values())
        table.add_row(
            row["date_utc"], row["model"],
            f"{row['input_tokens']}/{row['cached_input_tokens']}/{row['output_tokens']}/{row['reasoning_output_tokens']}",
            str(row["turns"]), str(row["tool_calls"]), str(failures), str(row["open_turns"]),
        )
    Console().print(table)


@app.command()
def quick(
    set_values: list[str] = typer.Option([], "--set", help="model=<id> or model_params.x=<JSON scalar>"),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file"),
    full: bool = typer.Option(False, "--full", help="Run frozen S1-S8 instead of S1/S4/S6"),
    base_config: Path = typer.Option(Path("configs/dev.yaml"), "--base-config"),
    name: str = typer.Option("default", "--name"),
    json_output: bool = typer.Option(False, "--json"),
    state_dir: Path = typer.Option(Path(".traceverdict/daily"), "--state-dir", hidden=True),
) -> None:
    """Derive an immutable config and compare the candidate with a cached baseline."""
    from traceverdict.daily import DailyError, DailyFailure, DailyPaths, derive_config, run_quick
    from traceverdict.resources import resolve_daily_assets

    paths = DailyPaths.at(state_dir)
    try:
        assets = resolve_daily_assets()
        if base_config == Path("configs/dev.yaml") and not base_config.is_file():
            base_config = assets.configs / "dev.yaml"
        config_path, _ = derive_config(
            base_config, set_values=set_values, prompt_file=prompt_file,
            output_dir=paths.configs, registries_dir=assets.configs,
        )
        result = run_quick(
            config_path, full=full, name=name, paths=paths,
            tasks_root=assets.tasks_self,
        )
    except DailyFailure as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except DailyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _daily_echo(result, json_output)
    if result["conclusion"] == "FAIL":
        raise typer.Exit(code=1)


@baseline_app.command("set")
def baseline_set(
    config: Path = typer.Option(..., "--config"),
    full: bool = typer.Option(False, "--full"),
    name: str = typer.Option("default", "--name"),
    state_dir: Path = typer.Option(Path(".traceverdict/daily"), "--state-dir", hidden=True),
) -> None:
    """Run a missing baseline scope once and cache its immutable pointers."""
    from traceverdict.daily import DailyError, DailyFailure, DailyPaths, set_baseline
    from traceverdict.resources import resolve_daily_assets

    try:
        assets = resolve_daily_assets()
        if config == Path("configs/dev.yaml") and not config.is_file():
            config = assets.configs / "dev.yaml"
        result = set_baseline(
            config, full=full, name=name, paths=DailyPaths.at(state_dir),
            tasks_root=assets.tasks_self,
        )
    except DailyFailure as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except DailyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@baseline_app.command("update")
def baseline_update(
    candidate: str = typer.Option(..., "--candidate"),
    full: bool = typer.Option(False, "--full"),
    name: str = typer.Option("default", "--name"),
    accept_regression: bool = typer.Option(False, "--accept-regression"),
    state_dir: Path = typer.Option(Path(".traceverdict/daily"), "--state-dir", hidden=True),
) -> None:
    """Promote an existing complete candidate without starting any run."""
    from traceverdict.daily import DailyError, DailyPaths, update_baseline

    try:
        result = update_baseline(candidate, full=full, name=name, accept_regression=accept_regression, paths=DailyPaths.at(state_dir))
    except DailyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def ingest(
    paths: list[Path] = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json"),
    state_dir: Path = typer.Option(Path(".traceverdict/daily"), "--state-dir", hidden=True),
) -> None:
    """Incrementally summarize Codex JSONL without storing session content."""
    from traceverdict.daily import DailyPaths
    from traceverdict.ingest import IngestError, ingest as ingest_logs

    daily = DailyPaths.at(state_dir)
    try:
        result = ingest_logs(paths or None, state_path=daily.ingest_state, metrics_path=daily.ingest_metrics)
    except IngestError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _ingest_echo(result, json_output)


if __name__ == "__main__":
    app()
