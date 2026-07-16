"""Paired task statistics, exact McNemar, and frozen regression alarms."""

from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from traceverdict.compare.constants import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    HARD_DELTA_PASS,
    MCNEMAR_ALPHA,
    WARN_DELTA_PASS,
    WARN_MEDIAN_TOKENS_RATIO,
    WARN_P95_WALL_RATIO,
)
from traceverdict.tracer import db as dbmod
from traceverdict.report.taxonomy import load_overrides, summarize_failures

DEFAULT_SELF_TASK_SET = Path("tasks/self/task_set.txt")


def load_task_set(path: str | Path) -> tuple[list[str], str]:
    data = Path(path).read_bytes()
    task_ids = [line.strip() for line in data.decode("utf-8").splitlines()]
    if not task_ids or any(not task_id for task_id in task_ids):
        raise ValueError("task-set must contain one non-empty task_id per line")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task-set contains duplicate task_id")
    return task_ids, hashlib.sha256(data).hexdigest()


def _quantile(values: list[float], probability: float) -> float:
    """R-7 linear quantile, implemented locally to avoid a new dependency."""
    if not values:
        raise ValueError("quantile requires non-empty values")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def paired_bootstrap_ci(
    deltas: list[float], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    if not deltas:
        raise ValueError("paired bootstrap requires at least one task")
    rng = random.Random(seed)
    n = len(deltas)
    draws = [sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)]
    tail = (1.0 - BOOTSTRAP_CONFIDENCE) / 2.0
    return _quantile(draws, tail), _quantile(draws, 1.0 - tail)


def exact_mcnemar(baseline: dict[str, float], candidate: dict[str, float]) -> dict[str, Any]:
    cells = {"both_pass": 0, "baseline_only": 0, "candidate_only": 0, "both_fail": 0}
    ties: list[str] = []
    for task_id in baseline:
        b, c = baseline[task_id], candidate[task_id]
        if b == 0.5 or c == 0.5:
            ties.append(task_id)
            continue
        bp, cp = b > 0.5, c > 0.5
        if bp and cp:
            cells["both_pass"] += 1
        elif bp:
            cells["baseline_only"] += 1
        elif cp:
            cells["candidate_only"] += 1
        else:
            cells["both_fail"] += 1
    discordant = cells["baseline_only"] + cells["candidate_only"]
    if discordant == 0:
        p_value = 1.0
    else:
        smaller = min(cells["baseline_only"], cells["candidate_only"])
        tail = sum(math.comb(discordant, i) for i in range(smaller + 1)) / (2**discordant)
        p_value = min(1.0, 2.0 * tail)
    return {**cells, "discordant": discordant, "p_value": p_value, "excluded_ties": sorted(ties)}


def _load_config_runs(conn, config_id: str, task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"SELECT * FROM run WHERE config_id=? AND task_id IN ({placeholders}) "
        "ORDER BY task_id,repetition_idx,run_id",
        (config_id, *task_ids),
    ).fetchall()
    for raw in rows:
        run = dict(raw)
        task = conn.execute(
            "SELECT gt_type,gt_spec_json FROM task WHERE task_id=?", (run["task_id"],)
        ).fetchone()
        if task is None:
            raise ValueError(f"run {run['run_id']} references missing task")
        verdicts = conn.execute(
            "SELECT name,passed FROM verdict WHERE run_id=? AND track='rule'", (run["run_id"],)
        ).fetchall()
        if not verdicts:
            raise ValueError(f"run {run['run_id']} has no rule verdicts")
        spec = json.loads(task["gt_spec_json"])
        if task["gt_type"] == "budget":
            required = {"budget"}
        elif task["gt_type"] == "swebench":
            # The official harness is the public-benchmark ground truth and is
            # stored as one aggregate rule verdict.  Self-suite pytest tasks
            # retain their more granular patch/F2P/P2P/forbidden verdicts.
            required = {"swebench"}
        else:
            required = {"patch_valid", "forbidden"}
            required.update(
                name for name in ("fail_to_pass", "pass_to_pass") if spec.get(name)
            )
        present = {verdict["name"] for verdict in verdicts}
        missing_verdicts = sorted(required - present)
        if missing_verdicts:
            raise ValueError(f"run {run['run_id']} missing rule verdicts: {missing_verdicts}")
        if any(run[field] is None for field in ("tokens_in", "tokens_out", "cost_usd", "wall_time_s")):
            raise ValueError(f"run {run['run_id']} has missing metric")
        run["passed"] = int(all(v["passed"] == 1 for v in verdicts))
        run["forbidden_failed"] = any(v["name"] == "forbidden" and v["passed"] != 1 for v in verdicts)
        grouped[run["task_id"]].append(run)
    missing = [task_id for task_id in task_ids if not grouped.get(task_id)]
    if missing:
        raise ValueError(f"config {config_id!r} missing task runs: {missing}")
    for task_id, task_runs in grouped.items():
        reps = [r["repetition_idx"] for r in task_runs]
        if len(reps) != len(set(reps)):
            raise ValueError(f"config {config_id!r} task {task_id} has duplicate repetition_idx")
    return grouped


def _task_metrics(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float | bool]]:
    result = {}
    for task_id, runs in grouped.items():
        result[task_id] = {
            "pass": sum(r["passed"] for r in runs) / len(runs),
            "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in runs) / len(runs),
            "cost": sum(r["cost_usd"] for r in runs) / len(runs),
            "wall": sum(r["wall_time_s"] for r in runs) / len(runs),
            "forbidden_failed": any(r["forbidden_failed"] for r in runs),
        }
    return result


def compare_configs(
    baseline_config: str,
    candidate_config: str,
    task_set_path: str | Path = DEFAULT_SELF_TASK_SET,
    *,
    db_path: str | Path = "reports/traceverdict.db",
    taxonomy_overrides_path: str | Path | None = None,
) -> dict[str, Any]:
    task_ids, task_set_sha = load_task_set(task_set_path)
    conn = dbmod._connect(db_path)
    try:
        baseline_runs = _load_config_runs(conn, baseline_config, task_ids)
        candidate_runs = _load_config_runs(conn, candidate_config, task_ids)
        for task_id in task_ids:
            if len(baseline_runs[task_id]) != len(candidate_runs[task_id]):
                raise ValueError(f"repetition count mismatch for task {task_id}")
        baseline = _task_metrics(baseline_runs)
        candidate = _task_metrics(candidate_runs)
        bpass = {t: float(baseline[t]["pass"]) for t in task_ids}
        cpass = {t: float(candidate[t]["pass"]) for t in task_ids}
        deltas = [cpass[t] - bpass[t] for t in task_ids]
        delta_pass = sum(deltas) / len(deltas)
        ci_low, ci_high = paired_bootstrap_ci(deltas)
        mcnemar = exact_mcnemar(bpass, cpass)

        def distribution(field: str) -> dict[str, float]:
            bvals = [float(baseline[t][field]) for t in task_ids]
            cvals = [float(candidate[t][field]) for t in task_ids]
            return {
                "baseline_median": median(bvals),
                "candidate_median": median(cvals),
                "baseline_p95": _quantile(bvals, 0.95),
                "candidate_p95": _quantile(cvals, 0.95),
            }

        tokens = distribution("tokens")
        cost = distribution("cost")
        wall = distribution("wall")
        token_ratio = math.inf if tokens["baseline_median"] == 0 and tokens["candidate_median"] > 0 else (
            tokens["candidate_median"] / tokens["baseline_median"] if tokens["baseline_median"] else 1.0
        )
        wall_ratio = math.inf if wall["baseline_p95"] == 0 and wall["candidate_p95"] > 0 else (
            wall["candidate_p95"] / wall["baseline_p95"] if wall["baseline_p95"] else 1.0
        )
        new_forbidden = sorted(
            t for t in task_ids if not baseline[t]["forbidden_failed"] and candidate[t]["forbidden_failed"]
        )
        hard = delta_pass <= HARD_DELTA_PASS and (
            ci_high < 0 or mcnemar["p_value"] < MCNEMAR_ALPHA
        )
        warn_reasons = []
        if delta_pass <= WARN_DELTA_PASS:
            warn_reasons.append("delta_pass")
        if token_ratio >= WARN_MEDIAN_TOKENS_RATIO:
            warn_reasons.append("median_tokens")
        if wall_ratio >= WARN_P95_WALL_RATIO:
            warn_reasons.append("p95_wall")
        if new_forbidden:
            warn_reasons.append("new_forbidden")
        alarm = "hard" if hard else ("warn" if warn_reasons else "none")
        failed_candidate_runs = [
            run["run_id"]
            for task_id in task_ids
            for run in candidate_runs[task_id]
            if run["passed"] == 0
        ]
        taxonomy = summarize_failures(
            conn, failed_candidate_runs, load_overrides(taxonomy_overrides_path)
        )
        stats = {
            "task_ids": task_ids,
            "task_count": len(task_ids),
            "repetitions": {t: len(baseline_runs[t]) for t in task_ids},
            "delta_pass": delta_pass,
            "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED, "ci95": [ci_low, ci_high]},
            "mcnemar": mcnemar,
            "tokens": {**tokens, "median_ratio": token_ratio},
            "cost": cost,
            "wall_time": {**wall, "p95_ratio": wall_ratio},
            "new_forbidden": new_forbidden,
            "warn_reasons": warn_reasons,
            "failure_taxonomy": taxonomy,
        }
        comparison_id = f"cmp-{uuid.uuid4().hex[:12]}"
        dbmod.insert_comparison(
            conn,
            {
                "comparison_id": comparison_id,
                "baseline_config": baseline_config,
                "candidate_config": candidate_config,
                "task_set_sha": task_set_sha,
                "stats_json": json.dumps(stats, ensure_ascii=False, sort_keys=True),
                "alarm": alarm,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {"comparison_id": comparison_id, "task_set_sha": task_set_sha, "alarm": alarm, "stats": stats}
    finally:
        conn.close()
