"""Local-only Codex exec adapter for the M4-C compatibility arm (D24)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from traceverdict.adapters.mini_swe_agent import AdapterHarnessError, AdapterResult
from traceverdict.swebench_budget import ADAPTER_WALL_TIMEOUT_GRACE_S

PINNED_CODEX_VERSION = "0.144.4"
CODEX_TRAJECTORY_FORMAT = "codex-exec-jsonl-1"
CODEX_BINARY_IN_IMAGE = "/opt/traceverdict/codex"


def _config_args(model_params: dict[str, Any]) -> list[str]:
    """Return the complete behavior-affecting Codex config overrides."""
    expected = {
        "auth_mode": "chatgpt_subscription",
        "billing_mode": "subscription_unallocatable",
        "model_reasoning_effort": "high",
        "approval_policy": "never",
        "sandbox_mode": "workspace-write",
        "web_search": "disabled",
        "ephemeral": True,
        "ignore_user_config": True,
        "ignore_rules": True,
        "concurrency": 1,
        "network_mode": "bridge",
        "container_cap_add": "SYS_ADMIN",
        "container_seccomp": "unconfined",
        "inner_sandbox": "workspace-write",
        "auth_isolation": "per_tool_mount_namespace_hide_codex_home_v2",
    }
    mismatches = {
        key: {"expected": value, "actual": model_params.get(key)}
        for key, value in expected.items()
        if model_params.get(key) != value
    }
    if mismatches:
        raise AdapterHarnessError(
            "immutable Codex M4-C identity mismatch: "
            + json.dumps(mismatches, sort_keys=True)
        )
    nested_expected = {
        "sandbox_workspace_write": {"network_access": True},
        "history": {"persistence": "none"},
        "features": {
            "shell_tool": True,
            "shell_snapshot": False,
            "unified_exec": False,
        },
        "hide_agent_reasoning": False,
    }
    nested_mismatches = {
        key: {"expected": value, "actual": model_params.get(key)}
        for key, value in nested_expected.items()
        if model_params.get(key) != value
    }
    if nested_mismatches:
        raise AdapterHarnessError(
            "immutable Codex nested identity mismatch: "
            + json.dumps(nested_mismatches, sort_keys=True)
        )
    if model_params.get("service_tier") != "omitted":
        raise AdapterHarnessError("service_tier must be explicitly omitted")
    if model_params.get("temperature") != "omitted" or model_params.get("top_p") != "omitted":
        raise AdapterHarnessError("temperature and top_p must be explicitly omitted")
    return [
        "-c", "model_reasoning_effort=\"high\"",
        "-c", "approval_policy=\"never\"",
        "-c", "web_search=\"disabled\"",
        "-c", "history.persistence=\"none\"",
        "-c", "hide_agent_reasoning=false",
        "-c", "features.shell_tool=true",
        "-c", "features.shell_snapshot=false",
        "-c", "features.unified_exec=false",
        "-c", "sandbox_workspace_write.network_access=true",
    ]


def build_codex_command(
    *, model_name: str, model_params: dict[str, Any], container_cwd: str
) -> list[str]:
    return [
        CODEX_BINARY_IN_IMAGE,
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--model",
        model_name,
        "--sandbox",
        "workspace-write",
        "-C",
        container_cwd,
        *_config_args(model_params),
        "-",
    ]


def parse_codex_jsonl(data: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(data.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterHarnessError(
                f"Codex JSONL line {line_number} is invalid: {exc}"
            ) from exc
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise AdapterHarnessError(
                f"Codex JSONL line {line_number} lacks an event type"
            )
        records.append(item)
    if not records:
        raise AdapterHarnessError("Codex produced no JSONL records")
    return records


def run_codex(
    *,
    instruction: str,
    image: str,
    docker_executable: str,
    host_work_path: Path,
    container_cwd: str,
    model_name: str,
    model_params: dict[str, Any],
    litellm_model_registry: Path | None,
    agent_version: str,
    cost_limit: float,
    step_limit: int,
    wall_time_s: int,
    work_dir: Path,
) -> AdapterResult:
    """Run Codex inside the local agent-layer image; never use a remote host."""
    del litellm_model_registry, cost_limit, step_limit
    if agent_version != PINNED_CODEX_VERSION:
        raise AdapterHarnessError(
            f"Codex version mismatch: expected {PINNED_CODEX_VERSION}, got {agent_version}"
        )
    auth_spec = os.environ.get("TRACEVERDICT_CODEX_AUTH_FILE")
    if not auth_spec:
        raise AdapterHarnessError("TRACEVERDICT_CODEX_AUTH_FILE is required locally")
    auth_file = Path(auth_spec).resolve()
    if not auth_file.is_file():
        raise AdapterHarnessError("local Codex auth file does not exist")
    work_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = work_dir / "codex.events.jsonl"
    stderr_path = work_dir / "codex.stderr.log"
    command = build_codex_command(
        model_name=model_name,
        model_params=model_params,
        container_cwd=container_cwd,
    )
    host_work = str(host_work_path.resolve()).replace("\\", "/")
    host_auth = str(auth_file).replace("\\", "/")
    docker_command = [
        docker_executable,
        "run",
        "--rm",
        "-i",
        "--network",
        "bridge",
        "--cap-add",
        "SYS_ADMIN",
        "--security-opt",
        "seccomp=unconfined",
        "-e",
        "CODEX_HOME=/run/traceverdict-codex",
        "-v",
        f"{host_work}:{container_cwd}",
        "-v",
        f"{host_auth}:/run/traceverdict-codex/auth.json",
        "-w",
        container_cwd,
        image,
        *command,
    ]
    timeout = max(1, int(wall_time_s)) + ADAPTER_WALL_TIMEOUT_GRACE_S
    try:
        proc = subprocess.run(
            docker_command,
            input=instruction,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterHarnessError(
            f"Codex exceeded adapter timeout {timeout}s", exit_reason="TimeExceeded"
        ) from exc
    jsonl_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")
    records = parse_codex_jsonl(proc.stdout or "")
    trajectory = {
        "trajectory_format": CODEX_TRAJECTORY_FORMAT,
        "records": records,
        "jsonl_sha256": hashlib.sha256((proc.stdout or "").encode("utf-8")).hexdigest(),
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "info": {
            "submission": "",
            "model_stats": {"instance_cost": None},
            "returncode": proc.returncode,
        },
    }
    trajectory_path = work_dir / "codex.trajectory.json"
    trajectory_path.write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return AdapterResult(
        traj=trajectory,
        traj_path=trajectory_path,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        source_artifacts={"codex_jsonl": jsonl_path},
    )
