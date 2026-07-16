#!/usr/bin/env python3
"""Validate the sealed M3 corpus and generate its zero-cost handoff reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from traceverdict.compare import compare_configs, load_task_set
from traceverdict.m3 import (
    ALPHA_CONFIG_ID,
    BETA_CONFIG_ID,
    EXP_C_CONFIG_ID,
    I3Q_CONFIG_ID,
    MIMO_REUSED_RUNS,
    TASK_SET_SHA256,
    cumulative_project_cost,
)
from traceverdict.report import generate_report
from traceverdict.tracer import db as dbmod

SELF_TASKS = tuple(f"S{index}" for index in range(1, 9))


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")


def _rule_pass(conn, run_id: str) -> bool:
    rows = conn.execute(
        "SELECT passed FROM verdict WHERE run_id=? AND track='rule'", (run_id,)
    ).fetchall()
    if not rows:
        raise RuntimeError(f"run has no rule verdict: {run_id}")
    return all(row["passed"] == 1 for row in rows)


def _config_runs(
    conn, *, config_id: str, task_ids: tuple[str, ...], repetitions: int
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"SELECT * FROM run WHERE config_id=? AND task_id IN ({placeholders}) "
        "ORDER BY task_id,repetition_idx,run_id",
        (config_id, *task_ids),
    ).fetchall()
    expected = {(task_id, rep) for task_id in task_ids for rep in range(repetitions)}
    actual = {(row["task_id"], row["repetition_idx"]) for row in rows}
    if len(rows) != len(expected) or actual != expected:
        raise RuntimeError(
            f"sealed run matrix differs for {config_id}: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    result = []
    for row in rows:
        item = dict(row)
        for field in ("tokens_in", "tokens_out", "cost_usd", "wall_time_s"):
            if item[field] is None:
                raise RuntimeError(f"run {item['run_id']} missing {field}")
        item["rule_passed"] = _rule_pass(conn, item["run_id"])
        result.append(item)
    return result


def _completion_trace_complete(completion_root: Path, run: dict[str, Any]) -> bool:
    path = (
        completion_root
        / "runs"
        / run["config_id"]
        / f"r{run['repetition_idx']}"
        / f"{run['task_id']}.json"
    )
    if not path.is_file():
        raise RuntimeError(f"sealed completion missing: {path}")
    data = json.loads(path.read_text("utf-8"))
    if data.get("run_id") != run["run_id"]:
        raise RuntimeError(f"completion/run mismatch: {path}")
    return data.get("trace_complete") is True


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run[key]
        for key in (
            "run_id",
            "task_id",
            "config_id",
            "repetition_idx",
            "status",
            "exit_reason",
            "tokens_in",
            "tokens_out",
            "cost_usd",
            "wall_time_s",
            "env_fingerprint",
            "rule_passed",
        )
    }


def _repeat_stability(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> dict[str, Any]:
    repetitions = sorted({run["repetition_idx"] for run in baseline})
    rows = []
    for repetition_idx in repetitions:
        left = [run for run in baseline if run["repetition_idx"] == repetition_idx]
        right = [run for run in candidate if run["repetition_idx"] == repetition_idx]
        if len(left) != len(right):
            raise RuntimeError(f"repeat {repetition_idx} run counts differ")
        baseline_rate = sum(run["rule_passed"] for run in left) / len(left)
        candidate_rate = sum(run["rule_passed"] for run in right) / len(right)
        rows.append(
            {
                "repetition_idx": repetition_idx,
                "baseline_pass_rate": baseline_rate,
                "candidate_pass_rate": candidate_rate,
                "delta_pass": candidate_rate - baseline_rate,
            }
        )
    signs = {0 if row["delta_pass"] == 0 else (1 if row["delta_pass"] > 0 else -1) for row in rows}
    return {"repetitions": rows, "direction_stable": len(signs) == 1}


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    task_ids_list, task_set_sha = load_task_set(args.task_set)
    if task_set_sha != TASK_SET_SHA256:
        raise RuntimeError(f"task-set SHA drift: {task_set_sha}")
    task_ids = tuple(task_ids_list)
    conn = dbmod._connect(args.db)
    try:
        alpha = _config_runs(
            conn, config_id=ALPHA_CONFIG_ID, task_ids=task_ids, repetitions=2
        )
        beta = _config_runs(
            conn, config_id=BETA_CONFIG_ID, task_ids=task_ids, repetitions=2
        )
        i3q = _config_runs(
            conn, config_id=I3Q_CONFIG_ID, task_ids=task_ids, repetitions=2
        )
        exp_c = _config_runs(
            conn, config_id=EXP_C_CONFIG_ID, task_ids=SELF_TASKS, repetitions=1
        )

        # Same-task public-benchmark arms must use the same frozen image/base identity.
        for task_id in task_ids:
            fingerprints = {
                run["env_fingerprint"]
                for run in (*alpha, *beta, *i3q)
                if run["task_id"] == task_id
            }
            if len(fingerprints) != 1:
                raise RuntimeError(
                    f"environment fingerprint drift for {task_id}: {sorted(fingerprints)}"
                )

        isolation_count = conn.execute(
            "SELECT COUNT(*) FROM artifact a JOIN run r ON r.run_id=a.run_id "
            "WHERE r.config_id=? AND a.kind='i3q_isolation'",
            (I3Q_CONFIG_ID,),
        ).fetchone()[0]
        if isolation_count != 32:
            raise RuntimeError(f"I3Q isolation evidence count is {isolation_count}, expected 32")

        artifact_rows = conn.execute(
            "SELECT a.run_id,a.kind,a.path,a.sha256 FROM artifact a "
            "JOIN run r ON r.run_id=a.run_id WHERE r.config_id IN (?,?,?,?) "
            "ORDER BY a.run_id,a.kind,a.artifact_id",
            (ALPHA_CONFIG_ID, BETA_CONFIG_ID, I3Q_CONFIG_ID, EXP_C_CONFIG_ID),
        ).fetchall()
        artifacts = [dict(row) for row in artifact_rows]
    finally:
        conn.close()

    if not all(_completion_trace_complete(args.completion_root, run) for run in exp_c):
        raise RuntimeError("Exp-C contains an incomplete trace")

    exp_a = compare_configs(
        ALPHA_CONFIG_ID,
        BETA_CONFIG_ID,
        args.task_set,
        db_path=args.db,
        taxonomy_overrides_path=args.taxonomy_overrides,
    )
    exp_b = compare_configs(
        ALPHA_CONFIG_ID,
        I3Q_CONFIG_ID,
        args.task_set,
        db_path=args.db,
        taxonomy_overrides_path=args.taxonomy_overrides,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    exp_a_report = args.output / "exp_a_model_comparison.md"
    exp_b_report = args.output / "exp_b_i3q_regression.md"
    generate_report(
        exp_a["comparison_id"], db_path=args.db, output_path=exp_a_report
    )
    generate_report(
        exp_b["comparison_id"], db_path=args.db, output_path=exp_b_report
    )

    shared_alpha = [run["run_id"] for run in alpha]
    exp_a_stability = _repeat_stability(alpha, beta)
    exp_b_stability = _repeat_stability(alpha, i3q)
    m3_new_runs = [
        *[run for run in alpha if run["repetition_idx"] == 1],
        *[run for run in beta if run["run_id"] not in MIMO_REUSED_RUNS],
        *i3q,
        *exp_c,
    ]
    if len(m3_new_runs) != 85:
        raise RuntimeError(f"M3 new-run accounting is {len(m3_new_runs)}, expected 85")
    m3_new_cost = sum(float(run["cost_usd"]) for run in m3_new_runs)
    manifest = {
        "schema_version": 1,
        "task_set_sha256": task_set_sha,
        "run_accounting": {
            "m3_new_paid_runs": 85,
            "exp_a_alpha_new": 16,
            "exp_a_beta_new": 29,
            "exp_b_i3q_new": 32,
            "exp_c_new": 8,
            "exp_a_alpha_total_with_m2_reuse": 32,
            "exp_a_beta_total_with_probe_reuse": 32,
        },
        "shared_samples": {
            "m2_alpha_first_repetition": [
                run["run_id"] for run in alpha if run["repetition_idx"] == 0
            ],
            "exp_b_baseline_is_exp_a_alpha": shared_alpha,
            "mimo_probe_reuse": list(MIMO_REUSED_RUNS),
        },
        "comparisons": {
            "exp_a": {**exp_a, "repeat_stability": exp_a_stability},
            "exp_b": {**exp_b, "repeat_stability": exp_b_stability},
        },
        "cost_ledger": {
            "m3_new_paid_runs_usd": m3_new_cost,
            "project_cumulative_usd": str(cumulative_project_cost(args.db)),
            "tripwire_usd": 28,
        },
        "runs": {
            "alpha": [_run_view(run) for run in alpha],
            "beta": [_run_view(run) for run in beta],
            "i3q": [_run_view(run) for run in i3q],
            "exp_c": [_run_view(run) for run in exp_c],
        },
        "exp_c": {
            "trace_complete": f"{sum(_completion_trace_complete(args.completion_root, run) for run in exp_c)}/8",
            "status_counts": dict(Counter(run["status"] for run in exp_c)),
            "rule_pass_count": sum(run["rule_passed"] for run in exp_c),
            "scope": "tracer/verifier/report genericity only; not a model comparison",
        },
        "i3q_isolation_evidence_count": isolation_count,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "limitations": [
            "k=2 gives only a low-resolution estimate of run-to-run variance",
            "Exp-C uses the self suite and cannot be compared with Exp-A model results",
            "SWE-agent 1.1.0 is maintenance-only and is used as a pinned compatibility target",
        ],
    }
    manifest_path = args.output / "m3_summary.json"
    _dump(manifest_path, manifest)
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    a = exp_a["stats"]
    b = exp_b["stats"]
    c_rows = "\n".join(
        f"| {run['task_id']} | `{run['run_id']}` | {run['status']} | "
        f"{run['exit_reason'] or ''} | {str(run['rule_passed']).lower()} | "
        f"{run['tokens_in'] + run['tokens_out']} | {run['cost_usd']:.10f} |"
        for run in exp_c
    )
    markdown = f"""# M3 final evidence

- Frozen task set SHA256: `{task_set_sha}`
- New paid runs: **85/85**
- Summary JSON SHA256: `{manifest_sha}`
- Executor: Codex; terminal reviewer: Claude; owner and final veto: project owner.

## Exp-A: model-family comparison

- Baseline: `{ALPHA_CONFIG_ID}`; candidate: `{BETA_CONFIG_ID}`; 16 tasks, k=2.
- Delta pass: `{a['delta_pass']}`; bootstrap 95% CI: `{a['bootstrap']['ci95']}`.
- Exact McNemar p: `{a['mcnemar']['p_value']}`; excluded k=2 ties: `{a['mcnemar']['excluded_ties']}`.
- Alarm: **{exp_a['alarm']}**; median token ratio: `{a['tokens']['median_ratio']}`; P95 wall ratio: `{a['wall_time']['p95_ratio']}`.
- Independent repeats: `{exp_a_stability['repetitions']}`; direction stable: `{str(exp_a_stability['direction_stable']).lower()}`.
- Full report: [exp_a_model_comparison.md](exp_a_model_comparison.md).

## Exp-B: known I3Q regression

- Baseline reuses all 32 Exp-A DeepSeek runs; candidate: `{I3Q_CONFIG_ID}`.
- Delta pass: `{b['delta_pass']}`; bootstrap 95% CI: `{b['bootstrap']['ci95']}`.
- Exact McNemar p: `{b['mcnemar']['p_value']}`; alarm: **{exp_b['alarm']}**.
- Independent repeats: `{exp_b_stability['repetitions']}`; direction stable: `{str(exp_b_stability['direction_stable']).lower()}`.
- Agent-ro/verifier-rw evidence: **{isolation_count}/32**.
- Full report: [exp_b_i3q_regression.md](exp_b_i3q_regression.md).

## Exp-C: second-agent compatibility

This arm tests tracer/verifier/report compatibility only. It is not a model-quality comparison.

| Task | Run | Status | Exit reason | Rule pass | Tokens | Cost USD |
|---|---|---|---|---:|---:|---:|
{c_rows}

- Complete traces: 8/8; rule passes: {sum(run['rule_passed'] for run in exp_c)}/8.
- SWE-agent 1.1.0 proposed native parallel tool calls that its own parser rejected on seven tasks; TraceVerdict preserves proposed calls but records only actually executed actions.
- S7 exercised the native `LimitsExceeded -> budget` path and passed its budget verdict.

## Reuse and interpretation boundaries

- M2 DeepSeek repetition 0 run IDs and all shared Exp-B baseline IDs are listed in `m3_summary.json`.
- MiMo reused probe runs: {', '.join(f'`{run_id}`' for run_id in MIMO_REUSED_RUNS)}.
- k=2 is sufficient for the frozen paired pipeline but gives a low-resolution variance estimate; results must not be presented as a broad leaderboard claim.
- M3 new-run spend: `${m3_new_cost:.10f}`; cumulative audited project spend: `${cumulative_project_cost(args.db)}` against the `$28` tripwire.
- Self-built components: TraceVerdict tracer, adapters, verifier wiring, comparison statistics, taxonomy, evidence and replay boundaries.
- Reused external components: mini-swe-agent 2.4.5, SWE-agent 1.1.0, SWE-bench 4.1.0 official harness/images, LiteLLM and provider APIs.

## Resume bullet draft

Built an auditable coding-agent regression harness and ran 16 frozen SWE-bench Verified tasks with paired bootstrap/McNemar analysis, strict token-cost reconciliation, official-verdict cross-checks, deterministic environment fingerprints, and an injected read-only-workspace regression; completed 85 new M3 runs while preserving explicit sample reuse and provenance.
"""
    (args.output / "m3_summary.md").write_text(markdown, "utf-8")
    return manifest


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--db", type=Path, default=Path("reports/m3/traceverdict.db"))
    root.add_argument(
        "--task-set", type=Path, default=Path("benchmarks/swebv_subset_v1.txt")
    )
    root.add_argument("--completion-root", type=Path, default=Path("reports/m3"))
    root.add_argument("--output", type=Path, default=Path("reports/m3/final"))
    root.add_argument("--taxonomy-overrides", type=Path)
    return root


if __name__ == "__main__":
    finalize(parser().parse_args())
