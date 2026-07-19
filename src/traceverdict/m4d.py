"""Read-only convergence diagnostics for the sealed M4 benchmark arms."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

FLASH_CONFIG = "dev-deepseek-v4-flash-v2"
MIMO_CONFIG = "probe-mimo-v2-5-thinking-v3"
PRO_CONFIG = "m4s-mini-2-4-5-deepseek-v4-pro-thinking-v1"
CODEX_CONFIG = "m4c-codex-0-144-4-gpt-5-6-luna-high-subscription-v2"
WALL_LIMIT_S = 3600.0
DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
TEST_RUNTIME_RE = re.compile(r"Test runtime:\s*([0-9_,.]+)\s+seconds")

ARM_SPECS = {
    "flash": {"config_id": FLASH_CONFIG, "repetitions": 2},
    "mimo": {"config_id": MIMO_CONFIG, "repetitions": 2},
    "pro": {"config_id": PRO_CONFIG, "repetitions": 1},
    "codex": {"config_id": CODEX_CONFIG, "repetitions": 1},
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_task_set(path: Path) -> tuple[list[str], str]:
    raw = path.read_bytes()
    tasks = raw.decode("utf-8").splitlines()
    if not tasks or any(not task.strip() for task in tasks):
        raise RuntimeError("task set contains an empty task id")
    if len(tasks) != len(set(tasks)):
        raise RuntimeError("task set contains duplicate task ids")
    return tasks, hashlib.sha256(raw).hexdigest()


def _rule_pass(conn: sqlite3.Connection, run_id: str) -> bool:
    rows = conn.execute(
        "SELECT passed FROM verdict WHERE run_id=? AND track='rule' AND name='swebench'",
        (run_id,),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"run {run_id} has {len(rows)} SWE-bench verdicts, expected 1")
    return bool(rows[0][0])


def load_db_arm(
    path: Path, *, config_id: str, task_ids: Sequence[str], repetitions: int
) -> list[dict[str, Any]]:
    """Load a sealed arm without changing the database."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"SELECT * FROM run WHERE config_id=? AND task_id IN ({marks}) "
            "ORDER BY task_id,repetition_idx,run_id",
            (config_id, *task_ids),
        ).fetchall()
        expected = {(task, rep) for task in task_ids for rep in range(repetitions)}
        actual = {(row["task_id"], row["repetition_idx"]) for row in rows}
        if len(rows) != len(expected) or actual != expected:
            raise RuntimeError(
                f"sealed run matrix differs for {config_id}: "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        result = []
        for row in rows:
            item = dict(row)
            item["passed"] = _rule_pass(conn, item["run_id"])
            result.append(item)
        return result
    finally:
        conn.close()


def load_pro_markdown(path: Path, task_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Load the public Pro fallback. It intentionally has no wall-time field."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.startswith("|") or "`run-" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6:
            continue
        task_id, run_id, terminal, resolved, token_text, cost_text = cells
        if task_id not in task_ids:
            continue
        tokens_in, tokens_out = [int(value.replace(",", "")) for value in token_text.split("/")]
        rows.append(
            {
                "run_id": run_id.strip("`"),
                "task_id": task_id,
                "config_id": PRO_CONFIG,
                "repetition_idx": 0,
                "status": "ok" if terminal == "Submitted" else "agent_error",
                "exit_reason": terminal,
                "wall_time_s": None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": float(cost_text),
                "passed": resolved == "yes",
            }
        )
    expected = set(task_ids)
    actual = {row["task_id"] for row in rows}
    if len(rows) != len(expected) or actual != expected:
        raise RuntimeError(
            f"Pro public summary differs from frozen task set: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return sorted(rows, key=lambda row: row["task_id"])


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def patch_file_count(patch: str) -> int:
    paths = {right for _left, right in DIFF_PATH_RE.findall(patch)}
    if not paths:
        raise ValueError("gold patch has no diff --git path")
    return len(paths)


def dataset_profile(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    measured = []
    for row in rows:
        patch = row["patch"]
        measured.append(
            {
                "task_id": row["instance_id"],
                "patch_bytes": len(patch.encode("utf-8")),
                "file_count": patch_file_count(patch),
                "difficulty": row.get("difficulty") or "unavailable",
            }
        )
    patch_sizes = [row["patch_bytes"] for row in measured]
    files = [row["file_count"] for row in measured]
    return {
        "count": len(measured),
        "patch_bytes": {
            "p25": percentile(patch_sizes, 0.25),
            "median": percentile(patch_sizes, 0.5),
            "p75": percentile(patch_sizes, 0.75),
            "mean": sum(patch_sizes) / len(patch_sizes),
        },
        "files": {
            "median": percentile(files, 0.5),
            "mean": sum(files) / len(files),
            "multi_file_fraction": sum(value > 1 for value in files) / len(files),
        },
        "difficulty": dict(sorted(Counter(row["difficulty"] for row in measured).items())),
        "tasks": measured,
    }


def matrix(arms: dict[str, list[dict[str, Any]]], task_ids: Sequence[str]) -> dict[str, Any]:
    rows = []
    strict_all_fail = []
    tie_sensitive = []
    for task_id in task_ids:
        arm_cells = {}
        for arm_name, spec in ARM_SPECS.items():
            selected = [row for row in arms[arm_name] if row["task_id"] == task_id]
            if len(selected) != spec["repetitions"]:
                raise RuntimeError(f"{arm_name}/{task_id} repetition mismatch")
            passed = sum(bool(row["passed"]) for row in selected)
            arm_cells[arm_name] = {
                "passed": passed,
                "repetitions": len(selected),
                "pass_rate": passed / len(selected),
                "exits": [
                    {
                        "status": row["status"],
                        "exit_reason": row.get("exit_reason"),
                    }
                    for row in selected
                ],
            }
        if all(cell["passed"] == 0 for cell in arm_cells.values()):
            strict_all_fail.append(task_id)
        if (
            any(cell["passed"] * 2 == cell["repetitions"] for cell in arm_cells.values())
            and all(cell["pass_rate"] <= 0.5 for cell in arm_cells.values())
        ):
            tie_sensitive.append(task_id)
        rows.append({"task_id": task_id, "arms": arm_cells})
    return {
        "rows": rows,
        "strict_all_fail": strict_all_fail,
        "tie_sensitive": tie_sensitive,
    }


def exit_audit(arms: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {"arms": {}}
    total_exits: Counter[tuple[str, str]] = Counter()
    total_runs = 0
    for arm_name, rows in arms.items():
        exits = Counter((row["status"], row.get("exit_reason") or "NULL") for row in rows)
        total_exits.update(exits)
        total_runs += len(rows)
        wall_rows = [row for row in rows if row.get("wall_time_s") is not None]
        wall_values = [float(row["wall_time_s"]) for row in wall_rows]
        nearest = sorted(wall_rows, key=lambda row: row["wall_time_s"], reverse=True)[:5]
        result["arms"][arm_name] = {
            "run_count": len(rows),
            "exit_distribution": [
                {"status": status, "exit_reason": reason, "count": count}
                for (status, reason), count in sorted(exits.items())
            ],
            "wall": (
                {
                    "available": len(wall_rows),
                    "missing": len(rows) - len(wall_rows),
                    "min": min(wall_values),
                    "median": percentile(wall_values, 0.5),
                    "p95": percentile(wall_values, 0.95),
                    "max": max(wall_values),
                    "within_300s": sum(WALL_LIMIT_S - value < 300 for value in wall_values),
                    "within_120s": sum(WALL_LIMIT_S - value < 120 for value in wall_values),
                    "within_60s": sum(WALL_LIMIT_S - value < 60 for value in wall_values),
                    "nearest": [
                        {
                            "run_id": row["run_id"],
                            "task_id": row["task_id"],
                            "wall_time_s": row["wall_time_s"],
                            "distance_to_3600_s": WALL_LIMIT_S - row["wall_time_s"],
                            "wall_ratio": row["wall_time_s"] / WALL_LIMIT_S,
                        }
                        for row in nearest
                    ],
                }
                if wall_values
                else {"available": 0, "missing": len(rows), "reason": "source has no wall_time_s"}
            ),
        }
    result["total_runs"] = total_runs
    result["exit_distribution"] = [
        {"status": status, "exit_reason": reason, "count": count}
        for (status, reason), count in sorted(total_exits.items())
    ]
    return result


def classify_all_fail(
    matrix_result: dict[str, Any], arms: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    classifications = []
    for task_id in matrix_result["strict_all_fail"]:
        task_runs = [row for rows in arms.values() for row in rows if row["task_id"] == task_id]
        non_submitted = [row for row in task_runs if row.get("exit_reason") != "Submitted"]
        if not non_submitted:
            category = "submitted_convergence"
        elif len(non_submitted) == len(task_runs):
            category = "harness_dominated"
        else:
            category = "mixed_constraints_and_task_failure"
        classifications.append(
            {
                "task_id": task_id,
                "category": category,
                "submitted_runs": len(task_runs) - len(non_submitted),
                "non_submitted_runs": len(non_submitted),
                "non_submitted_reasons": dict(
                    sorted(Counter(row.get("exit_reason") or "NULL" for row in non_submitted).items())
                ),
            }
        )
    return classifications


def find_test_runtimes(roots: Iterable[Path]) -> dict[str, Any]:
    values = []
    inspected = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("run_instance.log"):
            inspected += 1
            match = TEST_RUNTIME_RE.search(path.read_text("utf-8", errors="replace"))
            if match:
                values.append(float(match.group(1).replace(",", "")))
    return {
        "definition": "SWE-bench harness 'Test runtime' from run_instance.log",
        "logs_inspected": inspected,
        "values_found": len(values),
        "status": "available" if values else "unavailable",
        "reason": None if values else "archived official reports do not retain run_instance.log",
        "verified_500_comparison": "unavailable: the frozen dataset has no test-runtime field",
    }


def build_diagnostic(
    *,
    arms: dict[str, list[dict[str, Any]]],
    task_ids: Sequence[str],
    task_set_sha: str,
    verified_rows: Sequence[dict[str, Any]],
    evidence: dict[str, Any],
    runtime_roots: Iterable[Path] = (),
) -> dict[str, Any]:
    expected = sum(len(task_ids) * spec["repetitions"] for spec in ARM_SPECS.values())
    actual = sum(len(rows) for rows in arms.values())
    if actual != expected:
        raise RuntimeError(f"four-arm corpus has {actual} runs, expected {expected}")
    matrix_result = matrix(arms, task_ids)
    by_id = {row["instance_id"]: row for row in verified_rows}
    missing = sorted(set(task_ids) - set(by_id))
    if missing:
        raise RuntimeError(f"Verified dataset missing frozen tasks: {missing}")
    full_profile = dataset_profile(verified_rows)
    subset_profile = dataset_profile([by_id[task] for task in task_ids])
    all_fail_profile = dataset_profile(
        [by_id[task] for task in matrix_result["strict_all_fail"]]
    )
    return {
        "schema_version": "m4d-1",
        "task_set_sha256": task_set_sha,
        "config_ids": {name: spec["config_id"] for name, spec in ARM_SPECS.items()},
        "run_accounting": {"expected": expected, "actual": actual},
        "evidence": evidence,
        "matrix": matrix_result,
        "difficulty_shift": {
            "verified_500": full_profile,
            "frozen_16": subset_profile,
            "strict_all_fail": all_fail_profile,
            "sample_to_full_patch_median_ratio": (
                subset_profile["patch_bytes"]["median"] / full_profile["patch_bytes"]["median"]
            ),
            "all_fail_to_full_patch_median_ratio": (
                all_fail_profile["patch_bytes"]["median"] / full_profile["patch_bytes"]["median"]
            ),
            "sample_multi_file_percentage_point_shift": 100
            * (
                subset_profile["files"]["multi_file_fraction"]
                - full_profile["files"]["multi_file_fraction"]
            ),
            "selection_note": "The 16 tasks were deliberately stratified by patch-size band and single/multi-file status; these are descriptive effects, not random-sample p-values.",
        },
        "exit_audit": exit_audit(arms),
        "all_fail_classification": classify_all_fail(matrix_result, arms),
        "test_runtime": find_test_runtimes(runtime_roots),
        "conclusion": "mixed: subset difficulty shift, harness/provider constraints, and submitted-task convergence all contribute",
    }


def load_verified_arrow(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow as pa
        import pyarrow.ipc as ipc
    except ImportError as exc:  # pragma: no cover - exercised only in audit environment
        raise RuntimeError("reading the frozen Arrow dataset requires pyarrow") from exc
    with pa.memory_map(str(path), "r") as source:
        return ipc.open_stream(source).read_all().to_pylist()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")
