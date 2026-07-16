"""Rich terminal and Markdown comparison reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from traceverdict.tracer import db as dbmod

FAILURE_TAXONOMY = (
    "tool_misuse",
    "context_loss",
    "hallucinated_api",
    "loop",
    "budget",
    "other",
)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def generate_report(
    comparison_id: str,
    *,
    db_path: str | Path = "reports/traceverdict.db",
    output_path: str | Path | None = None,
    console: Console | None = None,
) -> dict[str, Any]:
    conn = dbmod._connect(db_path)
    try:
        row = dbmod.get_comparison(conn, comparison_id)
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"unknown comparison_id: {comparison_id}")
    stats = json.loads(row["stats_json"])
    m = stats["mcnemar"]
    ci = stats["bootstrap"]["ci95"]
    baseline_repetitions = stats.get("baseline_repetitions", stats["repetitions"])
    candidate_repetitions = stats.get("candidate_repetitions", stats["repetitions"])
    mode = stats.get("comparison_mode", "symmetric")

    table = Table(title=f"TraceVerdict comparison {comparison_id}")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Baseline", row["baseline_config"])
    table.add_row("Candidate", row["candidate_config"])
    table.add_row("Task set SHA256", row["task_set_sha"])
    table.add_row("Tasks", str(stats["task_count"]))
    table.add_row("Comparison mode", mode)
    table.add_row(
        "Baseline repetitions", json.dumps(baseline_repetitions, sort_keys=True)
    )
    table.add_row(
        "Candidate repetitions", json.dumps(candidate_repetitions, sort_keys=True)
    )
    table.add_row("Delta pass", _fmt(stats["delta_pass"]))
    table.add_row("Bootstrap 95% CI", f"[{_fmt(ci[0])}, {_fmt(ci[1])}]")
    table.add_row("McNemar p", _fmt(m["p_value"]))
    table.add_row("McNemar excluded ties", ", ".join(m["excluded_ties"]) or "none")
    table.add_row("Median token ratio", _fmt(stats["tokens"]["median_ratio"]))
    table.add_row("P95 wall ratio", _fmt(stats["wall_time"]["p95_ratio"]))
    table.add_row("Actual cost", _fmt(stats["cost"]))
    table.add_row("New forbidden", ", ".join(stats["new_forbidden"]) or "none")
    table.add_row("Alarm", row["alarm"])
    (console or Console()).print(table)

    output = Path(output_path) if output_path else Path("reports") / f"{comparison_id}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    asymmetry_note = (
        "Per-task metrics average each side independently. The paired bootstrap "
        "therefore compares candidate k=1 values with baseline k=2 means. "
        "Baseline 1/2 no-majority tasks are excluded from McNemar and listed "
        "below. This low-resolution asymmetric design is not a leaderboard or "
        "a general model-ranking claim."
        if mode == "asymmetric"
        else "Both sides use equal per-task repetition counts."
    )
    markdown = f"""# TraceVerdict comparison `{comparison_id}`

- Baseline: `{row['baseline_config']}`
- Candidate: `{row['candidate_config']}`
- Task set SHA256: `{row['task_set_sha']}`
- Tasks: {stats['task_count']}
- Comparison mode: `{mode}`
- Baseline repetitions: `{json.dumps(baseline_repetitions, sort_keys=True)}`
- Candidate repetitions: `{json.dumps(candidate_repetitions, sort_keys=True)}`
- Alarm: **{row['alarm']}**

> {asymmetry_note}

## Paired pass statistics

- Delta pass: {_fmt(stats['delta_pass'])}
- Bootstrap: {stats['bootstrap']['resamples']} resamples, seed {stats['bootstrap']['seed']}
- 95% CI: [{_fmt(ci[0])}, {_fmt(ci[1])}]
- McNemar cells: both_pass={m['both_pass']}, baseline_only={m['baseline_only']}, candidate_only={m['candidate_only']}, both_fail={m['both_fail']}
- Exact two-sided McNemar p: {_fmt(m['p_value'])}
- Excluded no-majority ties ({len(m['excluded_ties'])}): {', '.join(m['excluded_ties']) or 'none'}

## Cost and performance

- Tokens: `{json.dumps(stats['tokens'], sort_keys=True)}`
- Cost USD: `{json.dumps(stats['cost'], sort_keys=True)}`
- Subscription shadow cost, when present, is report-only and never substitutes for actual cost or enters alarms.
- Wall time: `{json.dumps(stats['wall_time'], sort_keys=True)}`
- New forbidden violations: {', '.join(stats['new_forbidden']) or 'none'}
- Warn reasons: {', '.join(stats['warn_reasons']) or 'none'}

## Failure taxonomy

""" + "\n".join(
        f"- {name}: {stats.get('failure_taxonomy', {}).get('counts', {}).get(name, 0)}"
        for name in FAILURE_TAXONOMY
    ) + "\n\n" + "\n".join(
        f"- `{item['run_id']}`: rule={item['rule_category']}, manual={item['manual_category'] or 'none'}, final={item['category']} ({item['source']})"
        for item in stats.get("failure_taxonomy", {}).get("runs", [])
    ) + "\n"
    output.write_text(markdown, encoding="utf-8")
    return {"comparison_id": comparison_id, "alarm": row["alarm"], "output": str(output), "stats": stats}
