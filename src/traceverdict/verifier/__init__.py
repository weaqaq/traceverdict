"""Deterministic rule-track verification on disposable task work copies."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from git import Repo

from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.workspace import cleanup_work_copy, materialize_work_copy
from traceverdict.tracer.db import get_run, upsert_verdict

RULE_RUBRIC_VERSION = "rule-v0.1"


def verdict_id(run_id: str, track: str, name: str, rubric_version: str) -> str:
    raw = "\0".join((run_id, track, name, rubric_version)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _patch_paths(patch_text: str) -> list[str]:
    paths = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.append(line.split(" b/", 1)[1])
    return sorted(set(paths))


def _is_forbidden(path: str, forbidden: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        normalized == item.rstrip("/") or normalized.startswith(item.rstrip("/") + "/")
        for item in forbidden
    )


def _frozen_path_changed(work: Path, base_commit: str, rel: str, expected: str) -> bool:
    """Check a frozen path without treating checkout EOL conversion as mutation."""
    blob = subprocess.run(
        ["git", "-C", str(work), "show", f"{base_commit}:{rel}"],
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != expected:
        return True
    candidate = work / rel
    if not candidate.is_file():
        return True
    diff = subprocess.run(
        ["git", "-C", str(work), "diff", "--quiet", "--", rel],
        capture_output=True,
        check=False,
    )
    if diff.returncode not in (0, 1):
        raise RuntimeError(f"git diff failed for frozen path {rel}: {diff.stderr!r}")
    return diff.returncode == 1


def _docker_pytest(
    work: Path, image: str, selectors: list[str], docker_executable: str
) -> tuple[bool, dict[str, Any]]:
    command = [
        docker_executable,
        "run",
        "--rm",
        "-v",
        f"{work.resolve()}:/testbed",
        "-w",
        "/testbed",
        image,
        "python",
        "-m",
        "pytest",
        "-q",
        *selectors,
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    return proc.returncode == 0, {
        "selectors": selectors,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def _write_rule_verdict(
    conn, run_id: str, name: str, passed: bool, detail: dict[str, Any]
) -> None:
    upsert_verdict(
        conn,
        {
            "verdict_id": verdict_id(run_id, "rule", name, RULE_RUBRIC_VERSION),
            "run_id": run_id,
            "track": "rule",
            "name": name,
            "passed": int(bool(passed)),
            "score": None,
            "detail_json": json.dumps(detail, ensure_ascii=False, sort_keys=True),
            "judge_model": None,
            "rubric_version": RULE_RUBRIC_VERSION,
        },
    )


def verify_run(
    conn,
    run_id: str,
    task_path: str | Path,
    *,
    docker_executable: str = "docker",
    pytest_runner: Callable[[Path, str, list[str], str], tuple[bool, dict[str, Any]]] = _docker_pytest,
) -> list[dict[str, Any]]:
    """Verify a stored run and idempotently persist its rule verdicts."""
    run = get_run(conn, run_id)
    if run is None:
        raise ValueError(f"unknown run_id: {run_id}")
    task = load_task(task_path)
    if run["task_id"] != task["task_id"]:
        raise ValueError("run task_id does not match task fixture")

    if task["gt"]["type"] == "budget":
        passed = run["status"] == "budget" and run["exit_reason"] == "LimitsExceeded"
        _write_rule_verdict(
            conn,
            run_id,
            "budget",
            passed,
            {
                "status": run["status"],
                "exit_reason": run["exit_reason"],
                "expected_exit_reason": "LimitsExceeded",
                "budget": task["budget"],
            },
        )
        return [dict(row) for row in conn.execute("SELECT * FROM verdict WHERE run_id=?", (run_id,))]

    artifact = conn.execute(
        "SELECT * FROM artifact WHERE run_id=? AND kind='patch'", (run_id,)
    ).fetchone()
    if artifact is None:
        _write_rule_verdict(conn, run_id, "patch_valid", False, {"error": "missing patch artifact"})
        return [dict(row) for row in conn.execute("SELECT * FROM verdict WHERE run_id=?", (run_id,))]

    patch_path = Path(artifact["path"])
    if not patch_path.is_file():
        raise FileNotFoundError(f"patch artifact missing: {patch_path}")
    patch_text = patch_path.read_text(encoding="utf-8")
    actual_sha = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    paths = _patch_paths(patch_text)
    spec = task["gt"]["spec"]
    work = materialize_work_copy(task["repo_ref_path"], task["base_commit"])
    patch_ok = actual_sha == artifact["sha256"]
    patch_error = None
    try:
        try:
            Repo(str(work)).git.apply(str(patch_path.resolve()))
        except Exception as exc:
            patch_ok = False
            patch_error = str(exc)
        if spec.get("patch_must_be_nonempty") and not patch_text.strip():
            patch_ok = False
        required_paths = set(spec.get("patch_must_touch") or [])
        if required_paths and not required_paths.issubset(paths):
            patch_ok = False
        _write_rule_verdict(
            conn,
            run_id,
            "patch_valid",
            patch_ok,
            {"sha256": actual_sha, "paths": paths, "error": patch_error},
        )

        forbidden_hits = [p for p in paths if _is_forbidden(p, task["forbidden_paths"])]
        frozen_mismatches: list[str] = []
        for rel, expected in (spec.get("forbidden_sha256") or {}).items():
            if _frozen_path_changed(work, task["base_commit"], rel, expected):
                frozen_mismatches.append(rel)
        _write_rule_verdict(
            conn,
            run_id,
            "forbidden",
            not forbidden_hits and not frozen_mismatches,
            {"hits": forbidden_hits, "frozen_sha_mismatches": frozen_mismatches},
        )

        for name in ("fail_to_pass", "pass_to_pass"):
            selectors = list(spec.get(name) or [])
            if selectors:
                passed, detail = pytest_runner(work, task["image_ref"], selectors, docker_executable)
                _write_rule_verdict(conn, run_id, name, patch_ok and passed, detail)
    finally:
        # The frozen verifier image runs as root and may leave root-owned
        # bytecode/cache files in its disposable bind mount on Linux.  Supply
        # the already-authorized Docker/image identity so cleanup can repair
        # ownership without ever touching the frozen fixture or agent copy.
        cleanup_work_copy(
            work,
            docker_executable=docker_executable,
            image=task["image_ref"],
        )

    return [dict(row) for row in conn.execute("SELECT * FROM verdict WHERE run_id=? ORDER BY name", (run_id,))]


def rule_run_passed(conn, run_id: str) -> bool:
    rows = conn.execute(
        "SELECT passed FROM verdict WHERE run_id=? AND track='rule'", (run_id,)
    ).fetchall()
    return bool(rows) and all(row["passed"] == 1 for row in rows)
