"""First-pass failure taxonomy with optional human overrides (D6-a)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

CATEGORIES = {
    "tool_misuse",
    "context_loss",
    "hallucinated_api",
    "loop",
    "budget",
    "other",
}


def load_overrides(path: str | Path | None) -> dict[str, str]:
    if path is None or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("taxonomy overrides must be a run_id -> category mapping")
    invalid = {run_id: value for run_id, value in data.items() if value not in CATEGORIES}
    if invalid:
        raise ValueError(f"invalid taxonomy overrides: {invalid}")
    return {str(key): str(value) for key, value in data.items()}


def _has_repeated_tool_call(conn, run_id: str) -> bool:
    rows = conn.execute(
        "SELECT payload_json FROM event WHERE run_id=? AND etype='tool_call' ORDER BY step_idx,event_id",
        (run_id,),
    ).fetchall()
    previous = None
    for row in rows:
        payload = json.loads(row["payload_json"])
        action = json.dumps(payload.get("action"), sort_keys=True, separators=(",", ":"))
        if previous is not None and action == previous:
            return True
        previous = action
    return False


def classify_failure(conn, run_id: str, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    run = conn.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run_id: {run_id}")
    forbidden = conn.execute(
        "SELECT 1 FROM verdict WHERE run_id=? AND track='rule' "
        "AND name='forbidden' AND passed!=1 LIMIT 1",
        (run_id,),
    ).fetchone()
    if forbidden is not None:
        rule = "tool_misuse"
    elif run["status"] == "budget":
        rule = "budget"
    elif _has_repeated_tool_call(conn, run_id):
        rule = "loop"
    else:
        rule = "other"
    manual = (overrides or {}).get(run_id)
    return {
        "run_id": run_id,
        "rule_category": rule,
        "manual_category": manual,
        "category": manual or rule,
        "source": "human" if manual else "rule",
    }


def summarize_failures(conn, run_ids: list[str], overrides: dict[str, str] | None = None) -> dict[str, Any]:
    items = [classify_failure(conn, run_id, overrides) for run_id in run_ids]
    return {"counts": dict(Counter(item["category"] for item in items)), "runs": items}
