"""Drive mini-swe-agent via subprocess with DockerEnvironment (D1-a/d/f)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from traceverdict.core.simple_yaml import dump_to_path
from traceverdict.swebench_budget import (
    ADAPTER_WALL_TIMEOUT_GRACE_S,
    AGENT_TOOL_TIMEOUT_S,
)
from traceverdict.tracer.trajectory import TrajectoryFormatError, assert_trajectory_format

PINNED_MINI_SWE_AGENT = "2.4.5"

# mini-swe-agent 2.4.5 creates prompt_toolkit PromptSession objects at import
# time, even for yolo/non-interactive runs.  On Windows a subprocess whose
# stdout is captured has no console screen buffer, so that import otherwise
# raises NoConsoleScreenBufferError before the CLI can parse ``-y``.  Running
# the module inside a DummyOutput app session keeps the process headless while
# preserving stdout/stderr capture for harness diagnostics.
_WINDOWS_HEADLESS_WRAPPER = (
    "from prompt_toolkit.application.current import create_app_session;"
    "from prompt_toolkit.output import DummyOutput;"
    "import runpy;"
    "ctx=create_app_session(output=DummyOutput());"
    "ctx.__enter__();"
    "runpy.run_module('minisweagent.run.mini',run_name='__main__')"
)


class AdapterHarnessError(RuntimeError):
    """Harness-level failure before/around agent execution."""

    def __init__(self, message: str, *, exit_reason: str = "harness_error"):
        super().__init__(message)
        self.exit_reason = exit_reason


def installed_mini_swe_agent_version() -> str | None:
    """Return installed mini-swe-agent version or None if not installed."""
    try:
        return metadata.version("mini-swe-agent")
    except metadata.PackageNotFoundError:
        return None


def assert_agent_version(expected: str) -> str:
    """D1-a: installed version must match config.agent_version."""
    got = installed_mini_swe_agent_version()
    if got is None:
        raise AdapterHarnessError(
            f"mini-swe-agent not installed; require =={expected} (D1-a)",
            exit_reason="harness_error",
        )
    if got != expected:
        raise AdapterHarnessError(
            f"mini-swe-agent version mismatch: installed={got!r} "
            f"config.agent_version={expected!r} (D1-a)",
            exit_reason="harness_error",
        )
    return got


def find_mini_cli() -> str:
    for name in ("mini-swe-agent", "mini"):
        path = shutil.which(name)
        if path:
            return path
    # Fallback: python -m minisweagent
    return ""


def build_mini_command(cli: str, args: list[str]) -> list[str]:
    """Build a non-interactive mini-swe-agent command for this platform."""
    if os.name == "nt":
        return [sys.executable, "-c", _WINDOWS_HEADLESS_WRAPPER, *args]
    if cli:
        return [cli, *args]
    return [sys.executable, "-m", "minisweagent.run.mini", *args]


@dataclass
class AdapterResult:
    traj: dict[str, Any]
    traj_path: Path
    returncode: int
    stdout: str
    stderr: str
    source_artifacts: dict[str, Path] = field(default_factory=dict)


def _build_mini_config(
    *,
    image: str,
    docker_executable: str,
    host_work_path: Path,
    container_cwd: str,
    model_name: str,
    model_params: dict[str, Any],
    litellm_model_registry: Path,
    output_path: Path,
    cost_limit: float,
    step_limit: int,
    wall_time_s: int,
) -> dict[str, Any]:
    """YAML config for mini-swe-agent: DockerEnvironment + litellm model + default agent."""
    # Windows paths for docker -v need absolute path; docker desktop accepts forward slashes.
    host = str(host_work_path.resolve()).replace("\\", "/")
    run_args = [
        "--rm",
        "-v",
        f"{host}:{container_cwd}",
    ]
    raw_model_params = dict(model_params or {})
    injection = dict(raw_model_params.pop("_traceverdict_injection", {}) or {})
    prompt_override = raw_model_params.pop("_traceverdict_system_prompt", None)
    injection_id = str(injection.get("id", "")).upper()
    if injection_id == "I3Q":
        run_args[-1] = f"{host}:{container_cwd}:ro"
    system_template = (
        "You are a helpful assistant that can interact with a computer shell "
        "to solve programming tasks."
    )
    if prompt_override is not None:
        if not isinstance(prompt_override, dict):
            raise AdapterHarnessError("daily system prompt identity must be a mapping")
        content = prompt_override.get("content")
        expected_sha = prompt_override.get("sha256")
        if not isinstance(content, str) or not content.strip():
            raise AdapterHarnessError("daily system prompt content is empty")
        actual_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if expected_sha != actual_sha:
            raise AdapterHarnessError("daily system prompt SHA256 mismatch")
        system_template = content
    tool_instruction = "You can execute bash commands and edit files.\n"
    instance_template = (
        "Please solve this issue in {{cwd}}:\n{{task}}\n\n"
        + tool_instruction
        + "When done, submit with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
        "Optionally print a patch after that line."
    )
    if injection_id == "I1":
        instance_template = instance_template.replace(tool_instruction, "")
    elif injection_id == "I1P":
        instance_template = (
            "Please work in {{cwd}}.\n\n"
            + tool_instruction
            + "When done, submit with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
            "Optionally print a patch after that line."
        )

    agent: dict[str, Any] = {
        "agent_class": "default",
        "output_path": str(output_path),
        "cost_limit": cost_limit,
        "step_limit": step_limit if step_limit > 0 else 0,
        "system_template": system_template,
        "instance_template": instance_template,
    }
    if wall_time_s and wall_time_s > 0:
        agent["wall_time_limit_seconds"] = wall_time_s

    model_kwargs = raw_model_params
    # The OpenAI SDK only accepts DeepSeek's vendor-specific thinking switch
    # through extra_body. This transport normalization is deliberately outside
    # immutable experiment config identity (D4-a).
    if "thinking" in model_kwargs:
        thinking = model_kwargs.pop("thinking")
        extra_body = dict(model_kwargs.get("extra_body") or {})
        extra_body["thinking"] = thinking
        model_kwargs["extra_body"] = extra_body

    observation_template = (
        "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
        "<returncode>{{output.returncode}}</returncode>\n<output>\n"
        + (
            "{{ output.output[:500] }}"
            if injection_id == "I2"
            else "" if injection_id == "I2P" else "{{ output.output }}"
        )
        + "</output>"
    )
    interpreter = ["bash", "-c"]
    if injection_id == "I3":
        guard = (
            "case \"$1\" in *pytest*) echo 'pytest disabled by TraceVerdict I3' >&2; exit 126;; "
            "*) exec bash -c \"$1\";; esac"
        )
        interpreter = ["bash", "-c", guard, "traceverdict-i3"]

    model_classes = {
        "I1Q": "traceverdict.injections.BrokenToolNameLitellmModel",
        "I5": "traceverdict.injections.HistoryWindowLitellmModel",
        "I5P": "traceverdict.injections.HistoryResetLitellmModel",
    }
    model: dict[str, Any] = {
        "model_class": model_classes.get(injection_id, "litellm"),
        "model_name": model_name,
        "model_kwargs": model_kwargs,
        "litellm_model_registry": str(litellm_model_registry.resolve()),
        "cost_tracking": "default",
        "observation_template": observation_template,
    }

    return {
        "agent": agent,
        "environment": {
            "environment_class": "docker",
            # D1-d: use the exact CLI already validated by require_docker().
            # This is essential for per-user Docker Desktop installs on
            # Windows, where a nested Python process may see a stale PATH.
            "executable": docker_executable,
            "image": image,
            "cwd": container_cwd,
            "timeout": AGENT_TOOL_TIMEOUT_S,
            "interpreter": interpreter,
            "run_args": run_args,
            "env": {
                "PAGER": "cat",
                "PIP_PROGRESS_BAR": "off",
                # Test execution must not pollute the authoritative patch
                # with transient __pycache__ files from the disposable repo.
                "PYTHONDONTWRITEBYTECODE": "enabled",
            },
        },
        "model": model,
    }


def run_mini_swe_agent(
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
    """Subprocess-run mini-swe-agent against DockerEnvironment mounting host_work_path.

    Strict Docker only (D1-d). Does not import agent for execution; uses CLI.
    """
    assert_agent_version(agent_version)

    work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="traceverdict-mini-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    traj_path = work_dir / "run.traj.json"
    cfg_path = work_dir / "mini_config.yaml"
    cfg = _build_mini_config(
        image=image,
        docker_executable=docker_executable,
        host_work_path=host_work_path,
        container_cwd=container_cwd,
        model_name=model_name,
        model_params=model_params,
        litellm_model_registry=litellm_model_registry,
        output_path=traj_path,
        cost_limit=cost_limit,
        step_limit=step_limit,
        wall_time_s=wall_time_s,
    )
    dump_to_path(cfg_path, cfg)
    injection_id = str(
        ((model_params or {}).get("_traceverdict_injection") or {}).get("id", "")
    ).upper()
    model_class_arg = {
        "I1Q": "traceverdict.injections.BrokenToolNameLitellmModel",
        "I5": "traceverdict.injections.HistoryWindowLitellmModel",
        "I5P": "traceverdict.injections.HistoryResetLitellmModel",
    }.get(injection_id, "litellm")

    args = [
        "-c",
        str(cfg_path),
        "-t",
        instruction,
        "-o",
        str(traj_path),
        "-y",
        "--exit-immediately",
        "--agent-class",
        "default",
        "--environment-class",
        "docker",
        "--model-class",
        model_class_arg,
        "-m",
        model_name,
    ]
    cmd = build_mini_command(find_mini_cli(), args)

    env = os.environ.copy()
    env["MSWEA_SILENT_STARTUP"] = "1"
    # The adapter supplies model/task/config explicitly and must never enter
    # mini-swe-agent's first-run API-key wizard in a harness subprocess.
    env["MSWEA_CONFIGURED"] = "1"

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=(
            max(wall_time_s + ADAPTER_WALL_TIMEOUT_GRACE_S, 300)
            if wall_time_s
            else 3600
        ),
        check=False,
    )

    # Preserve the complete child-process diagnostics before parsing its
    # trajectory.  Rich tracebacks put the root exception at the end, so a
    # prefix-only error message can hide the actionable failure entirely.
    stdout_path = work_dir / "mini.stdout.log"
    stderr_path = work_dir / "mini.stderr.log"
    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")

    if not traj_path.is_file():
        raise AdapterHarnessError(
            "mini-swe-agent produced no trajectory file. "
            f"returncode={proc.returncode} "
            f"stderr_tail={(proc.stderr or '')[-4000:]!r} "
            f"stdout_tail={(proc.stdout or '')[-2000:]!r}; "
            f"full logs: {stderr_path}, {stdout_path}",
            exit_reason="harness_error",
        )

    try:
        traj = json.loads(traj_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AdapterHarnessError(
            f"invalid trajectory JSON: {e}", exit_reason="harness_error"
        ) from e

    try:
        assert_trajectory_format(traj)
    except TrajectoryFormatError as e:
        raise AdapterHarnessError(str(e), exit_reason="harness_error") from e

    return AdapterResult(
        traj=traj,
        traj_path=traj_path,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
