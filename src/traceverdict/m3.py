"""M3 experiment identity, reuse, budget, and isolation helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.core.simple_yaml import load_path
from traceverdict.tracer import db as dbmod

TASK_SET_SHA256 = "eeb30802a00dd865cfc7214dc6ab123f267f1f92b9e5d3a4d21869bf4fb152be"
ALPHA_CONFIG_ID = "dev-deepseek-v4-flash-v2"
BETA_CONFIG_ID = "probe-mimo-v2-5-thinking-v3"
I3Q_CONFIG_ID = "m3-mini-2-4-5-deepseek-v4-flash-thinking-i3q-v1"
EXP_C_CONFIG_ID = "m3-swe-agent-1-1-0-deepseek-v4-flash-thinking-v7"
MIMO_REUSED_RUNS = (
    "run-a8ed5ed858d5",
    "run-2f777984344f",
    "run-a370ff1f7fb9",
)
MIMO_REUSED_TASKS = (
    "pytest-dev__pytest-7982",
    "sympy__sympy-20438",
    "pydata__xarray-7229",
)
PROJECT_ACTUAL_BEFORE_M3 = Decimal("0.8214003558768276576478361938")
SEEDED_DB_COST = Decimal("0.2298029363926078028747433265")
LEDGER_OFFSET_USD = PROJECT_ACTUAL_BEFORE_M3 - SEEDED_DB_COST
TRIPWIRE_USD = Decimal("28")
MAX_SEEN_RUN_USD = Decimal("0.02605477895470923249777226764")


def corrected_projection() -> dict[str, str | int]:
    remaining = MAX_SEEN_RUN_USD * 85
    unique = PROJECT_ACTUAL_BEFORE_M3 + remaining
    gross = unique + MAX_SEEN_RUN_USD * 48
    return {
        "basis_run_id": "run-a370ff1f7fb9",
        "basis_usd": str(MAX_SEEN_RUN_USD),
        "remaining_unique_runs": 85,
        "remaining_usd": str(remaining),
        "unique_104_total_usd": str(unique),
        "unique_104_reserve_usd": str(TRIPWIRE_USD - unique),
        "gross_152_total_usd": str(gross),
        "gross_152_reserve_usd": str(TRIPWIRE_USD - gross),
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_budget_identity(
    path: Path, *, expected_config_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the experiment identity while retaining wrapper provenance.

    T5 wrote this evidence after each run, so ``config_id`` and the single
    sample ``instance_id`` identify the writer rather than the shared budget.
    Later records also added an explicit enforcement ``status``.  Those are
    the only permitted representation differences; budget values, generated
    mini limits, timeout, and operational semantics remain exact.
    """
    raw = json.loads(path.read_text("utf-8"))
    required = {
        "budget",
        "budget_block_sha256",
        "config_id",
        "records",
        "semantics",
    }
    if set(raw) != required:
        raise RuntimeError(f"budget identity fields drift: {path}: {sorted(raw)}")
    if raw["config_id"] != expected_config_id:
        raise RuntimeError(
            f"budget identity config drift: {path}: {raw['config_id']!r} "
            f"!= {expected_config_id!r}"
        )

    semantics: dict[str, Any] = {}
    statuses: dict[str, str | None] = {}
    for name, value in raw["semantics"].items():
        item = dict(value)
        status = item.pop("status", None)
        expected_status = (
            "recorded-inert" if item.get("enforcement") == "none" else "enforced"
        )
        if status is not None and status != expected_status:
            raise RuntimeError(
                f"budget enforcement status drift: {path}: {name}={status!r}"
            )
        statuses[name] = status
        semantics[name] = item

    records = raw["records"]
    if not isinstance(records, list) or not records:
        raise RuntimeError(f"budget identity has no sample records: {path}")
    record_identities = []
    sample_ids = []
    for record in records:
        item = dict(record)
        sample_ids.append(item.pop("instance_id"))
        record_identities.append(item)
    if any(item != record_identities[0] for item in record_identities[1:]):
        raise RuntimeError(f"budget identity records disagree internally: {path}")

    normalized = {
        "budget": raw["budget"],
        "budget_block_sha256": raw["budget_block_sha256"],
        "record_identity": record_identities[0],
        "semantics": semantics,
    }
    wrapper = {
        "config_id": raw["config_id"],
        "sample_instance_ids": sample_ids,
        "semantic_statuses": statuses,
        "source_sha256": _sha(path),
    }
    return normalized, wrapper


def compare_budget_identities(
    *, m2_path: Path, mimo_path: Path
) -> dict[str, Any]:
    """Prove shared budget semantics without conflating evidence wrappers."""
    m2, m2_wrapper = _normalized_budget_identity(
        m2_path, expected_config_id=ALPHA_CONFIG_ID
    )
    mimo, mimo_wrapper = _normalized_budget_identity(
        mimo_path, expected_config_id=BETA_CONFIG_ID
    )
    if m2 != mimo:
        raise RuntimeError("MiMo reuse normalized budget identity differs")
    normalized_bytes = json.dumps(
        m2, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "status": "normalized-identical",
        "normalized_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
        "m2": m2_wrapper,
        "mimo": mimo_wrapper,
        "permitted_wrapper_differences": [
            "config_id",
            "sample_instance_ids",
            "additive_semantic_status_labels",
        ],
    }


def completion_path(
    output: Path, *, config_id: str, repetition_idx: int, task_id: str
) -> Path:
    return output / "runs" / config_id / f"r{repetition_idx}" / f"{task_id}.json"


def cumulative_project_cost(db_path: Path) -> Decimal:
    conn = dbmod._connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM run"
        ).fetchone()
        return LEDGER_OFFSET_USD + Decimal(str(row["total"]))
    finally:
        conn.close()


def assert_below_tripwire(db_path: Path) -> Decimal:
    total = cumulative_project_cost(db_path)
    if total >= TRIPWIRE_USD:
        raise RuntimeError(f"$28 cumulative tripwire reached: {total}")
    return total


def assert_reuse_identity(
    *,
    task_set: Path,
    m2_output: Path,
    mimo_output: Path,
    beta_config: Path,
) -> dict[str, Any]:
    task_bytes = task_set.read_bytes()
    if hashlib.sha256(task_bytes).hexdigest() != TASK_SET_SHA256:
        raise RuntimeError("M3 task-set SHA drift")
    ids = [line.strip() for line in task_bytes.decode("utf-8").splitlines()]
    if len(ids) != 16 or len(set(ids)) != 16:
        raise RuntimeError("M3 task set must contain exactly 16 unique IDs")

    beta = load_config(beta_config)
    if beta["config_id"] != BETA_CONFIG_ID:
        raise RuntimeError("MiMo beta config ID drift")

    m2_records = {
        row["instance_id"]: row
        for row in json.loads((m2_output / "image_records.json").read_text("utf-8"))
    }
    mimo_records = {
        row["instance_id"]: row
        for row in json.loads((mimo_output / "image_records.json").read_text("utf-8"))
    }
    tasks: list[dict[str, Any]] = []
    for task_id in MIMO_REUSED_TASKS:
        m2_task = m2_output / "tasks" / task_id / "task.yaml"
        mimo_task = mimo_output / "tasks" / task_id / "task.yaml"
        if m2_task.read_bytes() != mimo_task.read_bytes():
            raise RuntimeError(f"MiMo reuse task bytes differ: {task_id}")
        left = m2_records[task_id]
        right = mimo_records[task_id]
        for key in ("image_ref", "image_digest", "image_path"):
            if left[key] != right[key]:
                raise RuntimeError(f"MiMo reuse {key} differs: {task_id}")
        tasks.append(
            {
                "task_id": task_id,
                "task_yaml_sha256": _sha(m2_task),
                "image_ref": left["image_ref"],
                "image_digest": left["image_digest"],
                "image_path": left["image_path"],
            }
        )
    budget_identity = compare_budget_identities(
        m2_path=m2_output / "budget_identity.json",
        mimo_path=mimo_output / "budget_identity.json",
    )
    return {
        "task_set_sha256": TASK_SET_SHA256,
        "beta_config_id": BETA_CONFIG_ID,
        "beta_config_sha256": _sha(beta_config),
        "budget_identity": budget_identity,
        "tasks": tasks,
        "reuse_run_ids": list(MIMO_REUSED_RUNS),
        "status": "mechanically-identical",
    }


def alpha_run_ids_from_completions(
    *, task_set: Path, m2_output: Path, m2_db: Path
) -> tuple[str, ...]:
    """Select the 16 paid baseline runs from their sealed completion records."""
    task_ids = tuple(
        line.strip() for line in task_set.read_text("utf-8").splitlines() if line.strip()
    )
    completion_dir = m2_output / "runs"
    completion_paths = {path.stem: path for path in completion_dir.glob("*.json")}
    if set(completion_paths) != set(task_ids):
        raise RuntimeError(
            "M2 completion/task-set mismatch: "
            f"missing={sorted(set(task_ids) - set(completion_paths))}, "
            f"extra={sorted(set(completion_paths) - set(task_ids))}"
        )
    conn = dbmod._connect(m2_db)
    try:
        run_ids: list[str] = []
        for task_id in task_ids:
            completion = json.loads(completion_paths[task_id].read_text("utf-8"))
            run_id = completion.get("run_id")
            row = conn.execute(
                "SELECT task_id,config_id,repetition_idx,status FROM run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"M2 completion run missing from DB: {task_id}: {run_id}"
                )
            expected = (task_id, ALPHA_CONFIG_ID, 0, completion.get("status"))
            actual = (
                row["task_id"],
                row["config_id"],
                row["repetition_idx"],
                row["status"],
            )
            if actual != expected:
                raise RuntimeError(
                    f"M2 completion identity drift: {task_id}: {actual!r} != {expected!r}"
                )
            run_ids.append(run_id)
    finally:
        conn.close()
    if len(run_ids) != 16 or len(set(run_ids)) != 16:
        raise RuntimeError("M2 completion evidence must select 16 unique baseline runs")
    return tuple(run_ids)


def seed_m3_database(
    *,
    m2_db: Path,
    mimo_db: Path,
    target_db: Path,
    alpha_run_ids: tuple[str, ...],
) -> None:
    if target_db.exists():
        raise FileExistsError(f"refusing to overwrite M3 DB: {target_db}")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(m2_db, target_db)
    conn = dbmod._connect(target_db)
    try:
        if len(alpha_run_ids) != 16 or len(set(alpha_run_ids)) != 16:
            raise RuntimeError("M2 seed requires 16 unique approved alpha run IDs")
        placeholders = ",".join("?" for _ in alpha_run_ids)
        base_rows = conn.execute(
            f"SELECT run_id,task_id,config_id,repetition_idx FROM run "
            f"WHERE run_id IN ({placeholders})",
            alpha_run_ids,
        ).fetchall()
        if (
            len(base_rows) != 16
            or len({row["task_id"] for row in base_rows}) != 16
            or any(
                row["config_id"] != ALPHA_CONFIG_ID or row["repetition_idx"] != 0
                for row in base_rows
            )
        ):
            raise RuntimeError("approved M2 baseline run identities are invalid")

        # The M2 database also retains two paid-call-free harness diagnostics.
        # Seed only the 16 sealed baseline completions, not historical attempts.
        for table in ("event", "artifact", "verdict"):
            conn.execute(
                f"DELETE FROM {table} WHERE run_id NOT IN ({placeholders})",
                alpha_run_ids,
            )
        conn.execute(
            f"DELETE FROM run WHERE run_id NOT IN ({placeholders})", alpha_run_ids
        )
        conn.execute("DELETE FROM comparison")
        conn.execute("DELETE FROM config WHERE config_id!=?", (ALPHA_CONFIG_ID,))
        task_ids = tuple(row["task_id"] for row in base_rows)
        task_placeholders = ",".join("?" for _ in task_ids)
        conn.execute(
            f"DELETE FROM task WHERE task_id NOT IN ({task_placeholders})", task_ids
        )

        conn.execute("ATTACH DATABASE ? AS mimo", (str(mimo_db),))
        config = conn.execute(
            "SELECT * FROM mimo.config WHERE config_id=?", (BETA_CONFIG_ID,)
        ).fetchone()
        if config is None:
            raise RuntimeError("MiMo v3 config missing from source DB")
        conn.execute(
            "INSERT INTO config SELECT * FROM mimo.config WHERE config_id=?",
            (BETA_CONFIG_ID,),
        )
        placeholders = ",".join("?" for _ in MIMO_REUSED_RUNS)
        selected = conn.execute(
            f"SELECT run_id,task_id,config_id,status,repetition_idx FROM mimo.run "
            f"WHERE run_id IN ({placeholders}) ORDER BY run_id",
            MIMO_REUSED_RUNS,
        ).fetchall()
        if len(selected) != 3:
            raise RuntimeError("MiMo source DB missing approved reuse runs")
        if any(
            row["status"] != "ok"
            or row["repetition_idx"] != 0
            or row["config_id"] != BETA_CONFIG_ID
            for row in selected
        ):
            raise RuntimeError("MiMo reuse run status/repetition mismatch")
        if {row["task_id"] for row in selected} != set(MIMO_REUSED_TASKS):
            raise RuntimeError("MiMo reuse run/task mapping mismatch")
        for table in ("run", "artifact", "verdict"):
            conn.execute(
                f"INSERT INTO {table} SELECT * FROM mimo.{table} "
                f"WHERE run_id IN ({placeholders})",
                MIMO_REUSED_RUNS,
            )
        # event_id is an auto-increment surrogate and may overlap between the
        # two source databases. Preserve event payload/order while allocating
        # fresh target IDs.
        conn.execute(
            f"INSERT INTO event(run_id,step_idx,ts,etype,payload_json,tokens_in,"
            f"tokens_out,latency_ms) "
            f"SELECT run_id,step_idx,ts,etype,payload_json,tokens_in,tokens_out,"
            f"latency_ms FROM mimo.event WHERE run_id IN ({placeholders})",
            MIMO_REUSED_RUNS,
        )
        conn.commit()
        conn.execute("DETACH DATABASE mimo")
    except Exception:
        conn.close()
        target_db.unlink(missing_ok=True)
        raise
    else:
        conn.close()


def materialize_m3_inputs(*, m2_output: Path, output: Path) -> dict[str, str]:
    """Copy the frozen, non-model M2 inputs needed by every M3 run."""
    tasks_src = m2_output / "tasks"
    if not tasks_src.is_dir():
        raise FileNotFoundError(f"M2 task materialization missing: {tasks_src}")
    tasks_dst = output / "tasks"
    if tasks_dst.exists():
        raise FileExistsError(f"refusing to overwrite M3 tasks: {tasks_dst}")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tasks_src, tasks_dst)
    copied: dict[str, str] = {}
    for name in ("image_records.json", "budget_identity.json"):
        source = m2_output / name
        if not source.is_file():
            raise FileNotFoundError(f"M2 identity file missing: {source}")
        destination = output / name
        shutil.copy2(source, destination)
        copied[name] = _sha(destination)
    copied["tasks_tree"] = hashlib.sha256(
        "".join(
            f"{path.relative_to(tasks_dst).as_posix()}:{_sha(path)}\n"
            for path in sorted(tasks_dst.rglob("*"))
            if path.is_file()
        ).encode("utf-8")
    ).hexdigest()
    return copied


def assert_no_existing_repetition(
    db_path: Path, *, task_id: str, config_id: str, repetition_idx: int
) -> None:
    conn = dbmod._connect(db_path)
    try:
        rows = conn.execute(
            "SELECT run_id,status,cost_usd FROM run WHERE task_id=? AND config_id=? "
            "AND repetition_idx=?",
            (task_id, config_id, repetition_idx),
        ).fetchall()
        if rows:
            raise RuntimeError(
                "paid-run guard: repetition already exists: "
                + json.dumps([dict(row) for row in rows], sort_keys=True)
            )
    finally:
        conn.close()


def record_i3q_isolation(
    *, output: Path, db_path: Path, run_id: str, task_id: str
) -> Path:
    mini_config_path = output / "artifacts" / run_id / "adapter" / "mini_config.yaml"
    mini = load_path(mini_config_path)
    run_args = mini["environment"]["run_args"]
    mounts = [value for value in run_args if isinstance(value, str) and ":/testbed" in value]
    if len(mounts) != 1 or not mounts[0].endswith(":/testbed:ro"):
        raise RuntimeError("I3Q agent mount is not read-only")
    conn = dbmod._connect(db_path)
    try:
        official = conn.execute(
            "SELECT kind,path,sha256 FROM artifact WHERE run_id=? "
            "AND kind IN ('swebench_report','swebench_aggregate') "
            "ORDER BY CASE kind WHEN 'swebench_report' THEN 0 ELSE 1 END LIMIT 1",
            (run_id,),
        ).fetchone()
        if official is None:
            raise RuntimeError("I3Q official verifier artifact missing")
        evidence = {
            "decision_lineage": "D7-d -> D19-i",
            "run_id": run_id,
            "task_id": task_id,
            "agent_mount": mounts[0],
            "agent_read_only": True,
            "verifier_artifact_kind": official["kind"],
            "verifier_path": official["path"],
            "verifier_report_sha256": official["sha256"],
            "verifier_receives_injection_config": False,
            "verifier_workspace": "official harness independent container and clean image",
        }
        path = output / "isolation" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", "utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        dbmod.upsert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-i3q-isolation",
                "run_id": run_id,
                "kind": "i3q_isolation",
                "path": str(path),
                "sha256": sha,
            },
        )
        return path
    finally:
        conn.close()
