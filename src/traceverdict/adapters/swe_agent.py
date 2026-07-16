"""Official SWE-agent 1.1.0 adapter for TraceVerdict Exp-C."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

from traceverdict.adapters.mini_swe_agent import AdapterHarnessError, AdapterResult
from traceverdict.core.simple_yaml import dump_to_path
from traceverdict.swebench_budget import ADAPTER_WALL_TIMEOUT_GRACE_S, AGENT_TOOL_TIMEOUT_S
from traceverdict.tracer.trajectory import assert_trajectory_format

PINNED_SWE_AGENT = "1.1.0"
PINNED_SWE_AGENT_COMMIT = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
SWE_TRAJECTORY_FORMAT = "swe-agent-1.1.0"
PROBLEM_ID = "traceverdict-task"


def _container_ids(docker_executable: str) -> set[str]:
    proc = subprocess.run(
        [docker_executable, "ps", "-aq"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AdapterHarnessError(
            f"cannot snapshot Docker containers: {(proc.stderr or '')[-1000:]!r}"
        )
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def _cleanup_new_containers(docker_executable: str, before: set[str]) -> list[str]:
    new_ids = sorted(_container_ids(docker_executable) - before)
    for container_id in new_ids:
        subprocess.run(
            [docker_executable, "rm", "-f", container_id],
            capture_output=True,
            text=True,
            check=False,
        )
    return new_ids


def installed_swe_agent_version() -> str | None:
    try:
        return metadata.version("sweagent")
    except metadata.PackageNotFoundError:
        return None


def installed_swe_agent_root() -> Path | None:
    spec = importlib.util.find_spec("sweagent")
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).resolve().parent.parent


def assert_swe_agent_identity(expected_version: str) -> tuple[Path, str]:
    got = installed_swe_agent_version()
    if got != expected_version:
        raise AdapterHarnessError(
            f"SWE-agent version mismatch: installed={got!r} expected={expected_version!r}"
        )
    root = installed_swe_agent_root()
    if root is None:
        raise AdapterHarnessError("SWE-agent source root cannot be located")
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = (proc.stdout or "").strip()
    if proc.returncode != 0 or commit != PINNED_SWE_AGENT_COMMIT:
        raise AdapterHarnessError(
            "SWE-agent source commit mismatch: "
            f"installed={commit or 'unavailable'!r} expected={PINNED_SWE_AGENT_COMMIT!r}"
        )
    default_config = root / "config" / "default.yaml"
    if not default_config.is_file():
        raise AdapterHarnessError(f"SWE-agent default config missing: {default_config}")
    return default_config, commit


def _build_overlay(
    *,
    instruction: str,
    image: str,
    host_work_path: Path,
    model_name: str,
    model_params: dict[str, Any],
    output_dir: Path,
    cost_limit: float,
    step_limit: int,
) -> dict[str, Any]:
    params = copy.deepcopy(model_params or {})
    params.pop("_traceverdict_injection", None)
    thinking = params.pop("thinking", None)
    retry = params.pop("retry", None)
    completion_kwargs = dict(params.pop("completion_kwargs", {}) or {})
    if thinking is not None:
        extra_body = dict(completion_kwargs.get("extra_body", {}) or {})
        extra_body["thinking"] = thinking
        completion_kwargs["extra_body"] = extra_body
    temperature = float(params.pop("temperature", 0.0))
    top_p = params.pop("top_p", 1.0)
    completion_kwargs.update(params)
    repo_mount = f"{host_work_path.resolve()}:/testbed"
    model = {
        "name": model_name,
        "per_instance_cost_limit": cost_limit,
        "per_instance_call_limit": step_limit,
        "temperature": temperature,
        "top_p": top_p,
        "completion_kwargs": completion_kwargs,
    }
    if retry is not None:
        model["retry"] = retry
    return {
        "env": {
            "deployment": {
                "type": "docker",
                "image": image,
                "pull": "missing",
                "remove_images": False,
                "docker_args": ["--volume", repo_mount],
            },
            "repo": {
                # SWE-agent 1.1.0 LocalRepoConfig uploads to /<repo_name> but
                # then chowns the same name as a relative path.  Mount the
                # already-isolated TraceVerdict work copy and use the official
                # preexisting-repo mechanism instead.  This preserves the
                # pinned upstream source while avoiding that cwd-sensitive bug.
                "type": "preexisting",
                "repo_name": "testbed",
                "base_commit": "HEAD",
            },
        },
        "agent": {
            "model": model,
            "tools": {"execution_timeout": AGENT_TOOL_TIMEOUT_S},
        },
        "problem_statement": {"type": "text", "id": PROBLEM_ID, "text": instruction},
        "output_dir": str(output_dir.resolve()),
        "actions": {"apply_patch_locally": True, "open_pr": False},
    }


def _response_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise AdapterHarnessError("SWE-agent captured response has no first choice")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise AdapterHarnessError("SWE-agent captured response message is invalid")
    return message


def _action_from_call(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    arguments = function.get("arguments") or "{}"
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        parsed = {"raw_arguments": arguments}
    return {
        "tool_call_id": call.get("id"),
        "name": function.get("name"),
        "arguments": parsed,
        "command": parsed.get("command") if isinstance(parsed, dict) else None,
    }


def _exit_status(raw: Any) -> str:
    value = str(raw or "")
    lowered = value.lower()
    if lowered == "submitted":
        return "Submitted"
    if "cost" in lowered or "call_limit" in lowered or "limit" in lowered:
        return "LimitsExceeded"
    if "time" in lowered or "timeout" in lowered:
        return "TimeExceeded"
    return value or "AgentError"


def convert_swe_trajectory(
    raw: dict[str, Any], captures: list[dict[str, Any]]
) -> dict[str, Any]:
    """Convert official raw trajectory into TraceVerdict's strict canonical envelope."""
    steps = list(raw.get("trajectory") or [])
    if len(steps) < len(captures):
        raise AdapterHarnessError(
            f"SWE-agent trajectory/capture mismatch: calls={len(captures)} "
            f"steps={len(steps)}"
        )
    messages: list[dict[str, Any]] = []
    for index, capture in enumerate(captures):
        response = capture.get("response") or {}
        message = _response_message(response)
        tool_calls = list(message.get("tool_calls") or [])
        proposed_actions = [_action_from_call(call) for call in tool_calls]
        source_step = steps[index]
        source_action = str(source_step.get("action") or "")
        observation = source_step.get("observation", "")
        executed_actions: list[dict[str, Any]] = []
        if source_action:
            if not proposed_actions:
                raise AdapterHarnessError(
                    f"SWE-agent call {index} executed an action absent from native response"
                )
            # SWE-agent 1.1.0 executes at most the first native tool call in a
            # model turn. Parallel/malformed calls are retained in the raw
            # response but are not executions and must not receive synthetic
            # observations or tool_call events.
            executed = dict(proposed_actions[0])
            executed["source_action"] = source_action
            executed_actions.append(executed)
        elif observation:
            raise AdapterHarnessError(
                f"SWE-agent call {index} has observation without executed action"
            )
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise AdapterHarnessError(f"SWE-agent call {index} missing native usage")
        prompt_hash = capture.get("prompt_sha256")
        if not isinstance(prompt_hash, str) or len(prompt_hash) != 64:
            raise AdapterHarnessError(f"SWE-agent call {index} missing prompt hash")
        assistant = {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
            "reasoning_content": message.get("reasoning_content"),
            "extra": {
                "timestamp": capture.get("timestamp"),
                "prompt_sha256": prompt_hash,
                "actions": executed_actions,
                "proposed_actions": proposed_actions,
                "source_step": {
                    "action": source_action,
                    "execution_time": source_step.get("execution_time"),
                    "rejected_or_format_error": bool(proposed_actions)
                    and not bool(executed_actions),
                },
                "cost": capture.get("cost"),
                "response": response,
            },
        }
        messages.append(assistant)
        for action in executed_actions:
            extra_info = source_step.get("extra_info") or {}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": action.get("tool_call_id"),
                    "content": observation,
                    "extra": {
                        "raw_output": observation,
                        "returncode": extra_info.get("exit_code"),
                        "execution_time": source_step.get("execution_time"),
                    },
                }
            )
    trailing_steps = steps[len(captures) :]
    if any(
        str(step.get("action") or "") or str(step.get("observation") or "")
        for step in trailing_steps
    ):
        raise AdapterHarnessError(
            "SWE-agent trajectory/capture mismatch: trailing executed step has no response"
        )
    info = copy.deepcopy(raw.get("info") or {})
    status = _exit_status(info.get("exit_status"))
    messages.append(
        {
            "role": "exit",
            "content": str(info.get("exit_status") or ""),
            "extra": {
                "exit_status": status,
                "submission": info.get("submission") or "",
            },
        }
    )
    info["exit_status"] = status
    info["source_terminal_steps"] = len(trailing_steps)
    canonical = {
        "trajectory_format": "mini-swe-agent-1.1",
        "source_trajectory_format": SWE_TRAJECTORY_FORMAT,
        "source_identity": {
            "version": PINNED_SWE_AGENT,
            "commit": PINNED_SWE_AGENT_COMMIT,
        },
        "messages": messages,
        "info": info,
    }
    assert_trajectory_format(canonical)
    return canonical


def run_swe_agent(
    *,
    instruction: str,
    image: str,
    docker_executable: str,
    host_work_path: Path,
    container_cwd: str,
    model_name: str,
    model_params: dict[str, Any],
    litellm_model_registry: Path,
    agent_version: str,
    cost_limit: float,
    step_limit: int,
    wall_time_s: int,
    work_dir: Path | None = None,
) -> AdapterResult:
    """Run official SWE-agent without altering its source or verifier environment."""
    del container_cwd  # Deployment is owned by official SWE-ReX.
    default_config, _commit = assert_swe_agent_identity(agent_version)
    work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="traceverdict-swe-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir = work_dir / "swe_output"
    capture_path = work_dir / "native_responses.jsonl"
    request_path = work_dir / "native_requests.jsonl"
    overlay_path = work_dir / "swe_config.yaml"
    canonical_path = work_dir / "run.traj.json"
    dump_to_path(
        overlay_path,
        _build_overlay(
            instruction=instruction,
            image=image,
            host_work_path=host_work_path,
            model_name=model_name,
            model_params=model_params,
            output_dir=output_dir,
            cost_limit=cost_limit,
            step_limit=step_limit,
        ),
    )
    cmd = [
        sys.executable,
        "-m",
        "traceverdict.adapters.swe_agent_entrypoint",
        "--registry",
        str(litellm_model_registry.resolve()),
        "--capture",
        str(capture_path.resolve()),
        "--request-log",
        str(request_path.resolve()),
        "--",
        "--config",
        str(default_config),
        "--config",
        str(overlay_path.resolve()),
    ]
    env = os.environ.copy()
    stdout_path = work_dir / "swe.stdout.log"
    stderr_path = work_dir / "swe.stderr.log"
    before_containers = _container_ids(docker_executable)
    timeout_s = max(wall_time_s + ADAPTER_WALL_TIMEOUT_GRACE_S, 300)
    # Stream directly to files so a timeout retains every completed line.
    # capture_output=True previously discarded the only useful diagnostics on
    # TimeoutExpired and left the child-owned Docker container behind.
    with stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_handle, \
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_handle:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env=env,
        )
        try:
            returncode = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            cleaned = _cleanup_new_containers(docker_executable, before_containers)
            stderr_tail = stderr_path.read_text("utf-8", errors="replace")[-4000:]
            request_count = (
                len(request_path.read_text("utf-8").splitlines())
                if request_path.is_file()
                else 0
            )
            raise AdapterHarnessError(
                "SWE-agent adapter timeout. "
                f"timeout_s={timeout_s} request_count={request_count} "
                f"cleaned_containers={cleaned!r} stderr_tail={stderr_tail!r}"
            )
    stdout = stdout_path.read_text("utf-8", errors="replace")
    stderr = stderr_path.read_text("utf-8", errors="replace")
    raw_path = output_dir / PROBLEM_ID / f"{PROBLEM_ID}.traj"
    if not raw_path.is_file():
        raise AdapterHarnessError(
            "SWE-agent produced no trajectory file. "
            f"returncode={returncode} stderr_tail={stderr[-4000:]!r}"
        )
    if not capture_path.is_file():
        raise AdapterHarnessError("SWE-agent produced no native response capture")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    captures = [json.loads(line) for line in capture_path.read_text("utf-8").splitlines() if line]
    canonical = convert_swe_trajectory(raw, captures)
    canonical_path.write_text(json.dumps(canonical, indent=2, ensure_ascii=False), "utf-8")
    # Preserve raw SWE-agent data next to the canonical trace. Runner archives
    # the canonical trace; raw path/hash are embedded as immutable provenance.
    canonical["source_artifacts"] = {
        "raw_trajectory": str(raw_path),
        "raw_trajectory_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "native_responses": str(capture_path),
        "native_responses_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
    }
    canonical_path.write_text(json.dumps(canonical, indent=2, ensure_ascii=False), "utf-8")
    return AdapterResult(
        traj=canonical,
        traj_path=canonical_path,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        source_artifacts={
            "swe_agent_raw_trajectory": raw_path,
            "swe_agent_native_responses": capture_path,
            "swe_agent_native_requests": request_path,
            "swe_agent_config": overlay_path,
        },
    )
