"""Daily Mode orchestration: immutable configs, cached baselines, and smoke runs."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

from traceverdict.core.config_loader import load_config
from traceverdict.core.simple_yaml import dump_to_path, load_path
from traceverdict.tracer import db as dbmod
from traceverdict.verifier import rule_run_passed, verify_run

QUICK_TASKS = ("S1", "S4", "S6")
FULL_TASKS = tuple(f"S{i}" for i in range(1, 9))
STATE_RELATIVE = Path(".traceverdict/daily")


class DailyError(RuntimeError):
    """User/configuration/integrity error (CLI exit 2)."""


class DailyFailure(RuntimeError):
    """A candidate completed but failed the Daily gate (CLI exit 1)."""


@dataclass(frozen=True)
class DailyPaths:
    root: Path
    db: Path
    configs: Path
    artifacts: Path
    baselines: Path
    ingest_state: Path
    ingest_metrics: Path

    @classmethod
    def at(cls, root: str | Path = STATE_RELATIVE) -> "DailyPaths":
        root = Path(root)
        return cls(
            root=root,
            db=root / "traceverdict.db",
            configs=root / "configs",
            artifacts=root / "artifacts",
            baselines=root / "baselines.json",
            ingest_state=root / "ingest-state.json",
            ingest_metrics=root / "ingest-metrics.json",
        )

    def ensure(self) -> None:
        self.configs.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_scalar(text: str) -> Any:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = text
    if isinstance(value, (dict, list)):
        raise DailyError("--set values must be JSON scalars")
    return value


def parse_overrides(items: Iterable[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key or key in parsed:
            raise DailyError(f"invalid or duplicate --set: {item!r}")
        if key != "model" and not key.startswith("model_params."):
            raise DailyError("only model and model_params.* may be overridden")
        parsed[key] = _parse_scalar(raw)
    return parsed


def find_registry(model: str, configs_dir: str | Path = "configs") -> tuple[Path, str]:
    hits: list[Path] = []
    for path in sorted(Path(configs_dir).glob("litellm_models*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entry = data.get(model) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            continue
        required = ("input_cost_per_token", "output_cost_per_token", "litellm_provider", "mode")
        if all(k in entry for k in required):
            hits.append(path.resolve())
    if len(hits) != 1:
        raise DailyError(f"model {model!r} must match exactly one priced registry; got {len(hits)}")
    return hits[0], _sha(hits[0].read_bytes())


def derive_config(
    base_config: str | Path,
    *,
    set_values: Iterable[str] = (),
    prompt_file: str | Path | None = None,
    output_dir: str | Path,
    registries_dir: str | Path = "configs",
) -> tuple[Path, dict[str, Any]]:
    base_path = Path(base_config).resolve()
    raw = load_path(base_path)
    if not isinstance(raw, dict) or raw.get("agent_name") != "mini-swe-agent":
        raise DailyError("Daily Mode v0.2 supports mini-swe-agent configs only")
    overrides = parse_overrides(set_values)
    model = str(overrides.pop("model", raw.get("model_name", "")))
    registry, registry_sha = find_registry(model, registries_dir)
    model_params = json.loads(json.dumps(raw.get("model_params") or {}))
    for key, value in sorted(overrides.items()):
        cursor = model_params
        parts = key.split(".")[1:]
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise DailyError(f"override crosses non-mapping value: {key}")
            cursor = child
        cursor[parts[-1]] = value

    prompt = None
    if prompt_file is not None:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
        if not prompt.strip():
            raise DailyError("system prompt file is empty")
        model_params["_traceverdict_system_prompt"] = {
            "content": prompt,
            "sha256": _sha(prompt.encode("utf-8")),
        }

    parent_identity = {k: v for k, v in raw.items() if k not in {"config_id", "notes", "litellm_model_registry"}}
    identity = {
        "parent": parent_identity,
        "model_name": model,
        "model_params": model_params,
        "registry_sha256": registry_sha,
        "system_prompt_sha256": _sha(prompt.encode()) if prompt is not None else None,
    }
    digest = _sha(_canonical(identity))
    derived = dict(raw)
    derived.update(
        config_id=f"daily-{digest[:20]}",
        model_name=model,
        model_params=model_params,
        litellm_model_registry=str(registry),
        harness_version="0.2.1",
        notes=f"daily parent={raw.get('config_id')} identity_sha256={digest}",
    )
    output = Path(output_dir) / f"{derived['config_id']}.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = _canonical(derived)
    if output.exists():
        existing = load_path(output)
        if _canonical(existing) != rendered:
            raise DailyError(f"immutable derived config collision: {derived['config_id']}")
    else:
        dump_to_path(output, derived)
    return output, derived


def _connect(path: Path) -> sqlite3.Connection:
    if path.exists():
        return dbmod._connect(path)
    return dbmod.init_db(path)


def _task_ids(full: bool) -> tuple[str, ...]:
    return FULL_TASKS if full else QUICK_TASKS


def _load_baselines(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise DailyError("unsupported baselines.json format")
    return data


def _save_baselines(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _runs_for(conn: sqlite3.Connection, config_id: str, task_ids: Iterable[str]) -> dict[str, sqlite3.Row]:
    wanted = tuple(task_ids)
    if not wanted:
        return {}
    qs = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT * FROM run WHERE config_id=? AND repetition_idx=0 AND task_id IN ({qs}) ORDER BY finished_at DESC",
        (config_id, *wanted),
    ).fetchall()
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        result.setdefault(row["task_id"], row)
    return result


def _valid_completion(row: sqlite3.Row) -> bool:
    return (
        row["status"] in {"ok", "agent_error", "budget", "timeout"}
        and row["tokens_in"] is not None and row["tokens_out"] is not None
        and row["wall_time_s"] is not None and row["cost_usd"] is not None
    )


def execute_scope(
    config_path: str | Path,
    *,
    full: bool,
    paths: DailyPaths,
    runner: Callable[..., dict[str, Any]] | None = None,
    verifier: Callable[..., Any] | None = None,
    tasks_root: str | Path = "tasks/self",
) -> dict[str, Any]:
    from traceverdict.core.runner import run_task

    runner = runner or run_task
    verifier = verifier or verify_run
    paths.ensure()
    cfg = load_config(config_path)
    task_ids = _task_ids(full)
    conn = _connect(paths.db)
    reused: list[str] = []
    created: list[str] = []
    try:
        existing = _runs_for(conn, cfg["config_id"], task_ids)
        for task_id in task_ids:
            row = existing.get(task_id)
            if row is not None:
                if not _valid_completion(row):
                    raise DailyFailure(f"incomplete or unauditable existing run for {task_id}")
                verdict_count = conn.execute(
                    "SELECT COUNT(*) FROM verdict WHERE run_id=? AND track='rule'",
                    (row["run_id"],),
                ).fetchone()[0]
                if verdict_count == 0:
                    verifier(conn, row["run_id"], Path(tasks_root) / task_id)
                reused.append(task_id)
                continue
            result = runner(
                Path(tasks_root) / task_id,
                config_path,
                db_path=paths.db,
                artifacts_dir=paths.artifacts,
                repetition_idx=0,
            )
            if result.get("status") == "harness_error":
                raise DailyFailure(f"harness error on {task_id}: {result.get('error') or result.get('exit_reason')}")
            conn.close()
            conn = _connect(paths.db)
            verifier(conn, result["run_id"], Path(tasks_root) / task_id)
            created.append(task_id)
        rows = _runs_for(conn, cfg["config_id"], task_ids)
        if set(rows) != set(task_ids) or not all(_valid_completion(r) for r in rows.values()):
            raise DailyFailure("scope did not produce complete auditable runs")
        missing_verdicts = [
            task_id for task_id, row in rows.items()
            if conn.execute(
                "SELECT COUNT(*) FROM verdict WHERE run_id=? AND track='rule'",
                (row["run_id"],),
            ).fetchone()[0] == 0
        ]
        if missing_verdicts:
            raise DailyFailure(f"scope has runs without rule verdicts: {missing_verdicts}")
        return {"config_id": cfg["config_id"], "task_ids": list(task_ids), "runs": {k: v["run_id"] for k, v in rows.items()}, "reused": reused, "new": created}
    finally:
        conn.close()


def _percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _rule_snapshot(conn: sqlite3.Connection, rows: dict[str, sqlite3.Row]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for task_id, row in rows.items():
        verdicts = conn.execute("SELECT name, passed FROM verdict WHERE run_id=? AND track='rule'", (row["run_id"],)).fetchall()
        if not verdicts:
            raise DailyError(f"missing rule verdicts for {task_id}")
        forbidden = next((v["passed"] == 0 for v in verdicts if v["name"] == "forbidden"), False)
        out[task_id] = {
            "run_id": row["run_id"], "passed": rule_run_passed(conn, row["run_id"]),
            "forbidden": forbidden, "tokens": row["tokens_in"] + row["tokens_out"],
            "wall": float(row["wall_time_s"]), "cost": row["cost_usd"], "status": row["status"],
        }
    return out


def compare_daily(conn: sqlite3.Connection, baseline_id: str, candidate_id: str, *, full: bool) -> dict[str, Any]:
    tasks = _task_ids(full)
    b_rows, c_rows = _runs_for(conn, baseline_id, tasks), _runs_for(conn, candidate_id, tasks)
    if set(b_rows) != set(tasks) or set(c_rows) != set(tasks):
        raise DailyError("baseline and candidate must completely cover the Daily task set")
    if not all(_valid_completion(row) for row in (*b_rows.values(), *c_rows.values())):
        raise DailyError("baseline or candidate contains an incomplete/unauditable run")
    b, c = _rule_snapshot(conn, b_rows), _rule_snapshot(conn, c_rows)
    regressions = [t for t in tasks if b[t]["passed"] and not c[t]["passed"]]
    forbidden = [t for t in tasks if not b[t]["forbidden"] and c[t]["forbidden"]]
    b_tokens, c_tokens = [b[t]["tokens"] for t in tasks], [c[t]["tokens"] for t in tasks]
    b_wall, c_wall = [b[t]["wall"] for t in tasks], [c[t]["wall"] for t in tasks]
    bm, cm = median(b_tokens), median(c_tokens)
    bp, cp = _percentile95(b_wall), _percentile95(c_wall)
    token_ratio = cm / bm if bm else None
    wall_ratio = cp / bp if bp else None
    conclusion = "FAIL" if regressions or forbidden else "WARN" if (token_ratio is not None and token_ratio > 1.30) or (wall_ratio is not None and wall_ratio > 1.50) else "PASS"
    return {
        "conclusion": conclusion,
        "baseline_config_id": baseline_id,
        "candidate_config_id": candidate_id,
        "scope": "full" if full else "quick",
        "task_count": len(tasks),
        "delta_pass": sum(c[t]["passed"] for t in tasks) / len(tasks) - sum(b[t]["passed"] for t in tasks) / len(tasks),
        "delta_tokens_median": cm - bm,
        "token_ratio": token_ratio,
        "delta_wall_p95": cp - bp,
        "wall_ratio": wall_ratio,
        "actual_cost_usd": sum(float(c[t]["cost"] or 0) for t in tasks),
        "correctness_regressions": regressions,
        "new_forbidden": forbidden,
        "failed_tasks": [t for t in tasks if not c[t]["passed"]],
        "candidate_run_ids": {t: c[t]["run_id"] for t in tasks},
    }


def set_baseline(config_path: str | Path, *, full: bool, name: str, paths: DailyPaths, runner=None, verifier=None, tasks_root="tasks/self") -> dict[str, Any]:
    result = execute_scope(config_path, full=full, paths=paths, runner=runner, verifier=verifier, tasks_root=tasks_root)
    data = _load_baselines(paths.baselines)
    def make_entry(runs: dict[str, str]) -> dict[str, Any]:
        entry = {"config_id": result["config_id"], "run_ids": runs}
        entry["identity_sha256"] = _sha(_canonical(entry))
        return entry
    entry = make_entry(result["runs"])
    data["entries"][f"{name}:full" if full else f"{name}:quick"] = entry
    if full:
        data["entries"][f"{name}:quick"] = make_entry({k: v for k, v in result["runs"].items() if k in QUICK_TASKS})
    _save_baselines(paths.baselines, data)
    return {**result, "baseline": entry}


def update_baseline(candidate_id: str, *, full: bool, name: str, accept_regression: bool, paths: DailyPaths) -> dict[str, Any]:
    data = _load_baselines(paths.baselines)
    key = f"{name}:full" if full else f"{name}:quick"
    current = data["entries"].get(key)
    if current is None:
        raise DailyError(f"baseline {key!r} does not exist")
    conn = _connect(paths.db)
    try:
        comparison = compare_daily(conn, current["config_id"], candidate_id, full=full)
        if comparison["conclusion"] == "FAIL" and not accept_regression:
            raise DailyError("candidate regresses correctness/forbidden; use --accept-regression to promote")
        rows = _runs_for(conn, candidate_id, _task_ids(full))
        def make_entry(runs: dict[str, str]) -> dict[str, Any]:
            item = {"config_id": candidate_id, "run_ids": runs}
            item["identity_sha256"] = _sha(_canonical(item))
            return item
        entry = make_entry({k: v["run_id"] for k, v in rows.items()})
        data["entries"][key] = entry
        if full:
            data["entries"][f"{name}:quick"] = make_entry({k: v for k, v in entry["run_ids"].items() if k in QUICK_TASKS})
        _save_baselines(paths.baselines, data)
        return {"baseline": entry, "comparison": comparison, "model_runs_started": 0}
    finally:
        conn.close()


def run_quick(config_path: str | Path, *, full: bool, name: str, paths: DailyPaths, runner=None, verifier=None, tasks_root="tasks/self") -> dict[str, Any]:
    data = _load_baselines(paths.baselines)
    key = f"{name}:full" if full else f"{name}:quick"
    baseline = data["entries"].get(key)
    if baseline is None:
        raise DailyError(f"missing cached baseline {key!r}; run 'tv baseline set' first")
    execution = execute_scope(config_path, full=full, paths=paths, runner=runner, verifier=verifier, tasks_root=tasks_root)
    conn = _connect(paths.db)
    try:
        result = compare_daily(conn, baseline["config_id"], execution["config_id"], full=full)
    finally:
        conn.close()
    result.update(reused_task_count=len(execution["reused"]), new_task_count=len(execution["new"]))
    return result
