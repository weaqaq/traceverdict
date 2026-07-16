from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from rich.console import Console

from traceverdict.compare import compare_configs, exact_mcnemar, load_task_set, paired_bootstrap_ci
from traceverdict.report import generate_report
from traceverdict.tracer.db import init_db


def _seed_comparison_db(path: Path) -> None:
    conn = init_db(path)
    try:
        for config_id in ("base", "candidate"):
            conn.execute(
                "INSERT INTO config VALUES (?,?,?,?,?,?,?,?)",
                (config_id, "mini", "2.4.5", "model", "{}", "v0", "0.1.0", None),
            )
        for index in range(1, 9):
            task_id = f"S{index}"
            conn.execute(
                "INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, "self", "self", "bundle", "a" * 40, "image", "x", "{}", "[]", "pytest", "{}", "[]", "now"),
            )
            for config_id, passed in (("base", 1), ("candidate", 0)):
                run_id = f"{config_id}-{task_id}"
                conn.execute(
                    "INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, task_id, config_id, 0, "scenario", "ok", "Submitted", "a", "b", 10.0 if config_id == "base" else 20.0, 100, 20, 0.01, 1, "fp"),
                )
                conn.execute(
                    "INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"v-{run_id}", run_id, "rule", "patch_valid", passed, None, "{}", None, "rule-v0.1"),
                )
                conn.execute(
                    "INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"v-forbidden-{run_id}", run_id, "rule", "forbidden", 1, None, "{}", None, "rule-v0.1"),
                )
        conn.commit()
    finally:
        conn.close()


def test_task_set_hash_and_duplicate_rejection(tmp_path: Path):
    task_set = tmp_path / "tasks.txt"
    task_set.write_bytes(b"S1\nS2\n")
    ids, sha = load_task_set(task_set)
    assert ids == ["S1", "S2"]
    assert sha == hashlib.sha256(task_set.read_bytes()).hexdigest()
    task_set.write_text("S1\nS1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_task_set(task_set)


def test_known_bootstrap_and_exact_mcnemar_with_tie():
    assert paired_bootstrap_ci([-1.0] * 8) == (-1.0, -1.0)
    result = exact_mcnemar(
        {"S1": 1.0, "S2": 1.0, "S3": 0.5},
        {"S1": 0.0, "S2": 1.0, "S3": 1.0},
    )
    assert result["baseline_only"] == 1
    assert result["both_pass"] == 1
    assert result["p_value"] == 1.0
    assert result["excluded_ties"] == ["S3"]


def test_compare_hard_alarm_and_markdown_report(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    _seed_comparison_db(db)
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("".join(f"S{i}\n" for i in range(1, 9)), encoding="utf-8")
    result = compare_configs("base", "candidate", task_set, db_path=db)
    assert result["alarm"] == "hard"
    assert result["stats"]["delta_pass"] == -1.0
    assert result["stats"]["bootstrap"]["ci95"] == [-1.0, -1.0]
    output = tmp_path / "report.md"
    rendered = generate_report(
        result["comparison_id"],
        db_path=db,
        output_path=output,
        console=Console(file=None, force_terminal=False, quiet=True),
    )
    assert rendered["alarm"] == "hard"
    text = output.read_text(encoding="utf-8")
    assert "Failure taxonomy" in text
    assert "tool_misuse" in text


def test_compare_rejects_missing_explicit_task(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    _seed_comparison_db(db)
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("S1\nS9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing task runs"):
        compare_configs("base", "candidate", task_set, db_path=db)


def test_compare_rejects_incomplete_rule_verdicts(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    _seed_comparison_db(db)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM verdict WHERE verdict_id='v-forbidden-candidate-S1'")
    conn.commit()
    conn.close()
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("".join(f"S{i}\n" for i in range(1, 9)), encoding="utf-8")
    with pytest.raises(ValueError, match="missing rule verdicts"):
        compare_configs("base", "candidate", task_set, db_path=db)


def test_compare_accepts_official_swebench_aggregate_verdict(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    conn = init_db(db)
    try:
        for config_id in ("base", "candidate"):
            conn.execute(
                "INSERT INTO config VALUES (?,?,?,?,?,?,?,?)",
                (config_id, "mini", "2.4.5", "model", "{}", "v0", "0.1.0", None),
            )
        conn.execute(
            "INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "org__repo-1",
                "swebv",
                "swebench_verified",
                "bundle",
                "a" * 40,
                "image",
                "fix",
                "{}",
                "[]",
                "swebench",
                "{}",
                "[]",
                "now",
            ),
        )
        for config_id, passed in (("base", 1), ("candidate", 0)):
            run_id = f"{config_id}-run"
            conn.execute(
                "INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    "org__repo-1",
                    config_id,
                    0,
                    "scenario",
                    "ok",
                    "Submitted",
                    "a",
                    "b",
                    10.0,
                    100,
                    20,
                    0.01,
                    1,
                    "fp",
                ),
            )
            conn.execute(
                "INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"v-{run_id}",
                    run_id,
                    "rule",
                    "swebench",
                    passed,
                    None,
                    "{}",
                    None,
                    "rule-v0.1",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("org__repo-1\n", "utf-8")
    result = compare_configs("base", "candidate", task_set, db_path=db)
    assert result["stats"]["delta_pass"] == -1.0


def test_asymmetric_repetitions_require_explicit_flag_and_disclose_both_sides(
    tmp_path: Path,
):
    db = tmp_path / "traceverdict.db"
    _seed_comparison_db(db)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO run SELECT 'base-S1-r1',task_id,config_id,1,mode,status,"
        "exit_reason,started_at,finished_at,wall_time_s,tokens_in,tokens_out,"
        "cost_usd,seed,env_fingerprint FROM run WHERE run_id='base-S1'"
    )
    conn.execute(
        "INSERT INTO verdict SELECT 'v-base-S1-r1','base-S1-r1',track,name,0,"
        "score,detail_json,judge_model,rubric_version FROM verdict "
        "WHERE verdict_id='v-base-S1'"
    )
    conn.execute(
        "INSERT INTO verdict SELECT 'v-forbidden-base-S1-r1','base-S1-r1',track,"
        "name,passed,score,detail_json,judge_model,rubric_version FROM verdict "
        "WHERE verdict_id='v-forbidden-base-S1'"
    )
    conn.commit()
    conn.close()
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("".join(f"S{i}\n" for i in range(1, 9)), "utf-8")

    with pytest.raises(ValueError, match="repetition count mismatch"):
        compare_configs("base", "candidate", task_set, db_path=db)

    result = compare_configs(
        "base",
        "candidate",
        task_set,
        db_path=db,
        allow_asymmetric_repetitions=True,
    )
    stats = result["stats"]
    assert stats["comparison_mode"] == "asymmetric"
    assert stats["baseline_repetitions"]["S1"] == 2
    assert stats["candidate_repetitions"]["S1"] == 1
    assert stats["mcnemar"]["excluded_ties"] == ["S1"]
    report = tmp_path / "asymmetric.md"
    generate_report(
        result["comparison_id"],
        db_path=db,
        output_path=report,
        console=Console(file=None, force_terminal=False, quiet=True),
    )
    text = report.read_text("utf-8")
    assert "Comparison mode: `asymmetric`" in text
    assert "Baseline 1/2 no-majority tasks" in text


def test_unpriced_candidate_requires_explicit_subscription_identity(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    _seed_comparison_db(db)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE run SET cost_usd=NULL WHERE config_id='candidate'")
    conn.commit()
    conn.close()
    task_set = tmp_path / "tasks.txt"
    task_set.write_text("".join(f"S{i}\n" for i in range(1, 9)), encoding="utf-8")
    with pytest.raises(ValueError, match="missing metric"):
        compare_configs("base", "candidate", task_set, db_path=db)
    with pytest.raises(ValueError, match="billing_mode"):
        compare_configs(
            "base", "candidate", task_set, db_path=db, allow_unpriced_candidate=True
        )
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE config SET model_params_json=? WHERE config_id='candidate'",
        (json.dumps({"billing_mode": "subscription_unallocatable"}),),
    )
    conn.commit()
    conn.close()
    result = compare_configs(
        "base", "candidate", task_set, db_path=db, allow_unpriced_candidate=True
    )
    assert result["stats"]["cost"]["status"] == "unavailable"
    assert result["stats"]["cost"]["shadow_cost_is_report_only"] is True
