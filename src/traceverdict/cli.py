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

_UNIMPLEMENTED = "Not implemented in v0.1"


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


if __name__ == "__main__":
    app()
