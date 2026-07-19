"""One-shot Radar scheduling, ledgers, and two-level alert confirmation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.daily import (
    FULL_TASKS,
    QUICK_TASKS,
    DailyFailure,
    DailyPaths,
    _connect,
    _percentile95,
    _rule_snapshot,
    _runs_for,
    execute_scope,
)

MONTHLY_DEFAULT_USD = 3.0
PROJECT_TRIPWIRE_USD = 28.0


class RadarError(RuntimeError):
    """Configuration/integrity error (exit 2)."""


class RadarBudgetPause(RuntimeError):
    """A soft or project budget boundary paused a tick (exit 2)."""


@dataclass(frozen=True)
class RadarPaths:
    root: Path
    db: Path
    artifacts: Path
    registry: Path
    ticks: Path
    signals: Path
    ledger: Path

    @classmethod
    def at(cls, root: str | Path = ".traceverdict/radar") -> "RadarPaths":
        root = Path(root)
        return cls(
            root=root,
            db=root / "traceverdict.db",
            artifacts=root / "artifacts",
            registry=root / "registry.json",
            ticks=root / "ticks.json",
            signals=root / "signals.json",
            ledger=root / "ledger.json",
        )

    @property
    def daily(self) -> DailyPaths:
        return DailyPaths(
            root=self.root,
            db=self.db,
            configs=self.root / "configs",
            artifacts=self.artifacts,
            baselines=self.root / "unused-daily-baselines.json",
            ingest_state=self.root / "unused-ingest-state.json",
            ingest_metrics=self.root / "unused-ingest-metrics.json",
        )


def _read(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RadarError(f"unsupported Radar state: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")
    temp.replace(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_watch(config: str | Path, *, set_name: str, paths: RadarPaths) -> dict[str, Any]:
    if set_name not in {"quick", "full"}:
        raise RadarError("--set-name must be quick or full")
    config_path = Path(config).resolve()
    cfg = load_config(config_path)
    if not str(cfg["config_id"]).startswith("daily-") or "identity_sha256=" not in str(cfg.get("notes") or ""):
        raise RadarError("radar add requires a content-addressed Daily derived config")
    state = _read(paths.registry, {"version": 1, "watches": {}})
    item = {
        "name": cfg["config_id"],
        "config_id": cfg["config_id"],
        "config_path": str(config_path),
        "config_sha256": _sha(config_path),
        "set_name": set_name,
        "billing_mode": cfg.get("billing_mode", "api_metered"),
    }
    old = state["watches"].get(item["name"])
    if old is not None and old != item:
        raise RadarError("immutable Radar watch identity changed")
    state["watches"][item["name"]] = item
    _write(paths.registry, state)
    return item


def set_budget(*, project_actual_usd: float, monthly_limit_usd: float, paths: RadarPaths) -> dict[str, Any]:
    if project_actual_usd < 0 or monthly_limit_usd <= 0:
        raise RadarError("budget values must be non-negative and monthly limit positive")
    ledger = _read(paths.ledger, {"version": 1, "project_actual_seed_usd": 0.0, "monthly_limit_usd": MONTHLY_DEFAULT_USD, "entries": []})
    ledger["project_actual_seed_usd"] = project_actual_usd
    ledger["monthly_limit_usd"] = monthly_limit_usd
    ledger["seeded_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write(paths.ledger, ledger)
    return ledger


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ledger_totals(paths: RadarPaths) -> tuple[dict[str, Any], float, float]:
    ledger = _read(paths.ledger, {"version": 1, "project_actual_seed_usd": None, "monthly_limit_usd": MONTHLY_DEFAULT_USD, "entries": []})
    if ledger.get("project_actual_seed_usd") is None:
        raise RadarError("run 'tv radar budget set' before the first tick")
    actual = [e for e in ledger["entries"] if e.get("billing_mode") != "subscription_unallocatable"]
    month_total = sum(float(e["cost_usd"]) for e in actual if e["month_utc"] == _month())
    project_total = float(ledger["project_actual_seed_usd"]) + sum(float(e["cost_usd"]) for e in actual)
    return ledger, month_total, project_total


def _budget_check(paths: RadarPaths) -> None:
    ledger, month_total, project_total = _ledger_totals(paths)
    if month_total >= float(ledger["monthly_limit_usd"]):
        raise RadarBudgetPause("Radar monthly API budget is exhausted")
    if project_total >= PROJECT_TRIPWIRE_USD:
        raise RadarBudgetPause("project $28 API-spend tripwire is reached")


def _tasks(set_name: str) -> tuple[str, ...]:
    return FULL_TASKS if set_name == "full" else QUICK_TASKS


def _comparison(
    conn: sqlite3.Connection,
    config_id: str,
    tasks: tuple[str, ...],
    baseline_rep: int,
    candidate_rep: int,
) -> dict[str, Any]:
    b = _rule_snapshot(conn, _runs_for(conn, config_id, tasks, repetition_idx=baseline_rep))
    c = _rule_snapshot(conn, _runs_for(conn, config_id, tasks, repetition_idx=candidate_rep))
    b_tokens, c_tokens = [b[t]["tokens"] for t in tasks], [c[t]["tokens"] for t in tasks]
    b_wall, c_wall = [b[t]["wall"] for t in tasks], [c[t]["wall"] for t in tasks]
    token_alarm = bool(median(b_tokens)) and median(c_tokens) / median(b_tokens) > 1.30
    wall_alarm = bool(_percentile95(b_wall)) and _percentile95(c_wall) / _percentile95(b_wall) > 1.50
    reasons: dict[str, list[str]] = {}
    for task in tasks:
        why: list[str] = []
        if b[task]["passed"] and not c[task]["passed"]:
            why.append("correctness")
        if not b[task]["forbidden"] and c[task]["forbidden"]:
            why.append("forbidden")
        if token_alarm and b[task]["tokens"] and c[task]["tokens"] / b[task]["tokens"] > 1.30:
            why.append("tokens")
        if wall_alarm and b[task]["wall"] and c[task]["wall"] / b[task]["wall"] > 1.50:
            why.append("wall")
        if why:
            reasons[task] = why
    severity = "FAIL" if any(set(v) & {"correctness", "forbidden"} for v in reasons.values()) else "WARN" if reasons else "PASS"
    return {"severity": severity, "signal_tasks": reasons, "baseline": b, "candidate": c}


def _next_rep(conn: sqlite3.Connection, config_id: str) -> int:
    row = conn.execute("SELECT MAX(repetition_idx) FROM run WHERE config_id=?", (config_id,)).fetchone()
    return 0 if row[0] is None else int(row[0]) + 1


def tick(*, only: str | None, paths: RadarPaths, runner=None, verifier=None, tasks_root="tasks/self") -> dict[str, Any]:
    _budget_check(paths)
    registry = _read(paths.registry, {"version": 1, "watches": {}})
    watches = registry["watches"]
    if only:
        watches = {only: watches[only]} if only in watches else {}
    if not watches:
        raise RadarError("no matching Radar watch")
    tick_state = _read(paths.ticks, {"version": 1, "ticks": [], "baselines": {}})
    signal_state = _read(paths.signals, {"version": 1, "signals": {}})
    ledger, _, _ = _ledger_totals(paths)
    results = []
    for name, watch in sorted(watches.items()):
        config = Path(watch["config_path"])
        if not config.is_file() or _sha(config) != watch["config_sha256"]:
            raise RadarError(f"watch config drift: {name}")
        conn = _connect(paths.db)
        try:
            rep = _next_rep(conn, watch["config_id"])
        finally:
            conn.close()
        task_ids = _tasks(watch["set_name"])
        execution = execute_scope(config, full=watch["set_name"] == "full", paths=paths.daily, runner=runner, verifier=verifier, tasks_root=tasks_root, task_ids=task_ids, repetition_idx=rep)
        conn = _connect(paths.db)
        try:
            rows = _runs_for(conn, watch["config_id"], task_ids, repetition_idx=rep)
            snapshot = _rule_snapshot(conn, rows)
            cost = sum(float(row["cost_usd"] or 0) for row in rows.values())
            pass_rate = sum(bool(snapshot[t]["passed"]) for t in task_ids) / len(task_ids)
            median_tokens = median(snapshot[t]["tokens"] for t in task_ids)
            p95_wall = _percentile95([snapshot[t]["wall"] for t in task_ids])
            baseline_rep = tick_state["baselines"].setdefault(name, rep)
            comparison = None if rep == baseline_rep else _comparison(conn, watch["config_id"], task_ids, baseline_rep, rep)
        finally:
            conn.close()
        tick_id = "tick-" + hashlib.sha256(f"{name}\0{rep}\0{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12]
        level = "clean"
        signal_id = None
        if comparison and comparison["severity"] != "PASS":
            level = "signal"
            signal_id = "signal-" + hashlib.sha256(f"{tick_id}\0{name}".encode()).hexdigest()[:12]
            signal_state["signals"][signal_id] = {
                "signal_id": signal_id, "tick_id": tick_id, "name": name,
                "config_id": watch["config_id"], "config_path": str(config),
                "set_name": watch["set_name"], "baseline_rep": baseline_rep,
                "initial_rep": rep, "severity": comparison["severity"],
                "tasks": comparison["signal_tasks"], "level": "signal",
                "billing_mode": watch["billing_mode"],
            }
        item = {
            "tick_id": tick_id, "name": name, "config_id": watch["config_id"],
            "repetition_idx": rep, "baseline_rep": baseline_rep, "level": level,
            "signal_id": signal_id, "cost_usd": cost,
            "pass_rate": pass_rate, "median_tokens": median_tokens,
            "p95_wall_s": p95_wall,
            "run_ids": execution["runs"], "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        tick_state["ticks"].append(item)
        ledger["entries"].append({
            "tick_id": tick_id, "month_utc": _month(), "cost_usd": cost,
            "billing_mode": watch["billing_mode"],
        })
        results.append(item)
        _write(paths.ticks, tick_state); _write(paths.signals, signal_state); _write(paths.ledger, ledger)
        _budget_check(paths)
    return {"ticks": results, "level": "signal" if any(x["level"] == "signal" for x in results) else "clean"}


def set_baseline(*, name: str, tick_id: str | None, paths: RadarPaths) -> dict[str, Any]:
    state = _read(paths.ticks, {"version": 1, "ticks": [], "baselines": {}})
    choices = [x for x in state["ticks"] if x["name"] == name and (tick_id is None or x["tick_id"] == tick_id)]
    if not choices:
        raise RadarError("no matching tick for baseline")
    chosen = choices[-1]
    state["baselines"][name] = chosen["repetition_idx"]
    _write(paths.ticks, state)
    return {"name": name, "tick_id": chosen["tick_id"], "baseline_rep": chosen["repetition_idx"]}


def confirm(signal_id: str, *, paths: RadarPaths, runner=None, verifier=None, tasks_root="tasks/self") -> dict[str, Any]:
    signals = _read(paths.signals, {"version": 1, "signals": {}})
    signal = signals["signals"].get(signal_id)
    if signal is None:
        raise RadarError("unknown signal_id")
    if signal["level"] != "signal":
        return signal
    _budget_check(paths)
    tasks = tuple(sorted(signal["tasks"]))
    config = Path(signal["config_path"])
    conn = _connect(paths.db)
    try:
        start = _next_rep(conn, signal["config_id"])
    finally:
        conn.close()
    for rep in (start, start + 1):
        execute_scope(config, full=False, paths=paths.daily, runner=runner, verifier=verifier, tasks_root=tasks_root, task_ids=tasks, repetition_idx=rep)
    conn = _connect(paths.db)
    try:
        base = _rule_snapshot(conn, _runs_for(conn, signal["config_id"], tasks, repetition_idx=signal["baseline_rep"]))
        evidence = {}
        confirmed_any = False
        for task in tasks:
            reps = [signal["initial_rep"], start, start + 1]
            rows = [_rule_snapshot(conn, _runs_for(conn, signal["config_id"], (task,), repetition_idx=r))[task] for r in reps]
            confirmed_reasons = []
            reasons = signal["tasks"][task]
            if "correctness" in reasons and base[task]["passed"] and sum(not x["passed"] for x in rows) >= 2: confirmed_reasons.append("correctness")
            if "forbidden" in reasons and sum(bool(x["forbidden"]) for x in rows) >= 2: confirmed_reasons.append("forbidden")
            if "tokens" in reasons and base[task]["tokens"] and median(x["tokens"] for x in rows) / base[task]["tokens"] > 1.30: confirmed_reasons.append("tokens")
            if "wall" in reasons and base[task]["wall"] and median(x["wall"] for x in rows) / base[task]["wall"] > 1.50: confirmed_reasons.append("wall")
            confirmed_any = confirmed_any or bool(confirmed_reasons)
            evidence[task] = {"repetitions": reps, "run_ids": [x["run_id"] for x in rows], "confirmed_reasons": confirmed_reasons}
        confirmation_cost = 0.0
        for rep in (start, start + 1):
            confirmation_cost += sum(
                float(row["cost_usd"] or 0)
                for row in _runs_for(conn, signal["config_id"], tasks, repetition_idx=rep).values()
            )
    finally:
        conn.close()
    signal["level"] = "confirmed" if confirmed_any else "withdrawn"
    signal["confirmation"] = evidence
    signal["confirmation_cost_usd"] = confirmation_cost
    signals["signals"][signal_id] = signal
    _write(paths.signals, signals)
    ledger, _, _ = _ledger_totals(paths)
    ledger["entries"].append({
        "tick_id": f"confirm:{signal_id}", "month_utc": _month(),
        "cost_usd": confirmation_cost, "billing_mode": signal["billing_mode"],
    })
    _write(paths.ledger, ledger)
    _budget_check(paths)
    return signal


def report(*, days: int, paths: RadarPaths) -> dict[str, Any]:
    if days <= 0:
        raise RadarError("--days must be positive")
    ticks = _read(paths.ticks, {"version": 1, "ticks": [], "baselines": {}})
    signals = _read(paths.signals, {"version": 1, "signals": {}})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [x for x in ticks["ticks"] if datetime.fromisoformat(x["created_at_utc"]) >= cutoff]
    ledger, month_total, project_total = _ledger_totals(paths)
    return {
        "days": days, "ticks": recent,
        "signals": list(signals["signals"].values()),
        "monthly_actual_usd": month_total,
        "monthly_limit_usd": ledger["monthly_limit_usd"],
        "project_actual_usd": project_total,
        "project_tripwire_usd": PROJECT_TRIPWIRE_USD,
    }
