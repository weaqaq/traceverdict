import json

import pytest

from traceverdict.report.taxonomy import classify_failure, load_overrides
from traceverdict.tracer.db import init_db


def _db(tmp_path, status="agent_error"):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("S1","self","self","b","a","i","x","{}","[]","pytest","{}","[]","now"))
    conn.execute("INSERT INTO config VALUES (?,?,?,?,?,?,?,?)", ("c","a","1","m","{}","v","h",None))
    conn.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("r","S1","c",0,"scenario",status,"x","a","b",1,1,1,0.1,1,"fp"))
    conn.commit()
    return conn


def test_budget_and_loop_rule_with_human_override(tmp_path):
    conn = _db(tmp_path, status="budget")
    try:
        assert classify_failure(conn, "r")["category"] == "budget"
        item = classify_failure(conn, "r", {"r": "context_loss"})
        assert item["rule_category"] == "budget"
        assert item["category"] == "context_loss"
        assert item["source"] == "human"
    finally:
        conn.close()


def test_consecutive_equal_tool_calls_map_loop(tmp_path):
    conn = _db(tmp_path)
    try:
        payload = json.dumps({"action": {"command": "pytest"}})
        for step in (1, 2):
            conn.execute("INSERT INTO event(run_id,step_idx,ts,etype,payload_json) VALUES (?,?,?,?,?)", ("r",step,"now","tool_call",payload))
        conn.commit()
        assert classify_failure(conn, "r")["category"] == "loop"
    finally:
        conn.close()


def test_forbidden_has_priority_over_budget_and_loop(tmp_path):
    conn = _db(tmp_path, status="budget")
    try:
        payload = json.dumps({"action": {"command": "pytest"}})
        for step in (1, 2):
            conn.execute(
                "INSERT INTO event(run_id,step_idx,ts,etype,payload_json) VALUES (?,?,?,?,?)",
                ("r", step, "now", "tool_call", payload),
            )
        conn.execute(
            "INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)",
            ("v", "r", "rule", "forbidden", 0, None, "{}", None, "v0.1"),
        )
        conn.commit()
        assert classify_failure(conn, "r")["category"] == "tool_misuse"
    finally:
        conn.close()


def test_override_validation(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text('{"r":"not-real"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid taxonomy"):
        load_overrides(path)
