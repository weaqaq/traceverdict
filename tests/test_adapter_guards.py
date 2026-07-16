"""Adapter hard-fail guards: version and trajectory_format (D1-a, D1-f)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from traceverdict.adapters.mini_swe_agent import (
    AdapterHarnessError,
    assert_agent_version,
    build_mini_command,
    run_mini_swe_agent,
)
from traceverdict.tracer.trajectory import TrajectoryFormatError, assert_trajectory_format


def test_version_mismatch_raises():
    with patch(
        "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
        return_value="0.0.0",
    ):
        with pytest.raises(AdapterHarnessError) as ei:
            assert_agent_version("2.4.5")
        assert "version mismatch" in str(ei.value)


def test_version_match_ok():
    with patch(
        "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
        return_value="2.4.5",
    ):
        assert assert_agent_version("2.4.5") == "2.4.5"


@pytest.mark.skipif(os.name != "nt", reason="Windows console regression")
def test_windows_headless_command_survives_captured_output():
    """The Windows wrapper must import mini-swe-agent without a console buffer."""
    cmd = build_mini_command("mini-swe-agent", ["--help"])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout


def test_run_marks_mini_as_configured(tmp_path: Path):
    """Harness runs must never block in mini-swe-agent's first-run wizard."""

    def fake_run(*args, **kwargs):
        assert kwargs["env"]["MSWEA_CONFIGURED"] == "1"
        config_text = (tmp_path / "adapter" / "mini_config.yaml").read_text(
            encoding="utf-8"
        )
        assert "executable: /usr/bin/docker" in config_text
        assert 'PIP_PROGRESS_BAR: "off"' in config_text
        assert "PYTHONDONTWRITEBYTECODE: enabled" in config_text
        assert "litellm_model_registry:" in config_text
        assert "cost_tracking: default" in config_text
        assert "thinking:" in config_text
        assert "type: enabled" in config_text
        assert "extra_body:" in config_text
        return _write_bad_traj_and_return(tmp_path)

    with (
        patch(
            "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
            return_value="2.4.5",
        ),
        patch("traceverdict.adapters.mini_swe_agent.find_mini_cli", return_value="mini"),
        patch("traceverdict.adapters.mini_swe_agent.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(AdapterHarnessError, match="trajectory_format"):
            run_mini_swe_agent(
                instruction="x",
                image="traceverdict/self-base:py3.12-v1",
                docker_executable="/usr/bin/docker",
                host_work_path=tmp_path,
                container_cwd="/testbed",
                model_name="test",
                model_params={"thinking": {"type": "enabled"}},
                litellm_model_registry=tmp_path / "registry.json",
                agent_version="2.4.5",
                cost_limit=1.0,
                step_limit=5,
                wall_time_s=60,
                work_dir=tmp_path / "adapter",
            )


def test_bad_trajectory_format_on_run(tmp_path: Path):
    traj_path = tmp_path / "run.traj.json"
    bad = {"trajectory_format": "nope", "messages": []}
    traj_path.write_text(json.dumps(bad), encoding="utf-8")

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    with (
        patch(
            "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
            return_value="2.4.5",
        ),
        patch("traceverdict.adapters.mini_swe_agent.find_mini_cli", return_value="mini"),
        patch("traceverdict.adapters.mini_swe_agent.subprocess.run", return_value=FakeProc()),
        patch(
            "traceverdict.adapters.mini_swe_agent.Path.is_file",
            # traj_path.is_file True; allow config write
            side_effect=lambda self=None: True,
        ),
    ):
        # More reliable: write real files and only mock subprocess + version
        pass

    with (
        patch(
            "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
            return_value="2.4.5",
        ),
        patch("traceverdict.adapters.mini_swe_agent.find_mini_cli", return_value="mini"),
        patch(
            "traceverdict.adapters.mini_swe_agent.subprocess.run",
            side_effect=lambda *a, **k: _write_bad_traj_and_return(tmp_path),
        ),
    ):
        with pytest.raises(AdapterHarnessError) as ei:
            run_mini_swe_agent(
                instruction="x",
                image="traceverdict/self-base:py3.12-v1",
                docker_executable="/usr/bin/docker",
                host_work_path=tmp_path,
                container_cwd="/testbed",
                model_name="test",
                model_params={},
                litellm_model_registry=tmp_path / "registry.json",
                agent_version="2.4.5",
                cost_limit=1.0,
                step_limit=5,
                wall_time_s=60,
                work_dir=tmp_path / "adapter",
            )
        assert "trajectory_format" in str(ei.value)


def test_no_trajectory_persists_complete_logs_and_reports_stderr_tail(tmp_path: Path):
    class FakeProc:
        returncode = 1
        stdout = "stdout-prefix\n" + ("o" * 3000) + "\nstdout-root"
        stderr = "stderr-prefix\n" + ("e" * 5000) + "\nROOT-CAUSE"

    adapter_dir = tmp_path / "adapter"
    with (
        patch(
            "traceverdict.adapters.mini_swe_agent.installed_mini_swe_agent_version",
            return_value="2.4.5",
        ),
        patch("traceverdict.adapters.mini_swe_agent.find_mini_cli", return_value="mini"),
        patch(
            "traceverdict.adapters.mini_swe_agent.subprocess.run",
            return_value=FakeProc(),
        ),
    ):
        with pytest.raises(AdapterHarnessError) as ei:
            run_mini_swe_agent(
                instruction="x",
                image="traceverdict/self-base:py3.12-v1",
                docker_executable="/usr/bin/docker",
                host_work_path=tmp_path,
                container_cwd="/testbed",
                model_name="test",
                model_params={},
                litellm_model_registry=tmp_path / "registry.json",
                agent_version="2.4.5",
                cost_limit=1.0,
                step_limit=5,
                wall_time_s=60,
                work_dir=adapter_dir,
            )

    assert (adapter_dir / "mini.stdout.log").read_text(encoding="utf-8") == FakeProc.stdout
    assert (adapter_dir / "mini.stderr.log").read_text(encoding="utf-8") == FakeProc.stderr
    assert "ROOT-CAUSE" in str(ei.value)
    assert "stderr-prefix" not in str(ei.value)


def _write_bad_traj_and_return(tmp_path: Path):
    # run_mini_swe_agent writes traj to work_dir/run.traj.json
    # Find newest adapter dir
    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    # The work_dir is tmp_path/adapter
    p = tmp_path / "adapter" / "run.traj.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"trajectory_format": "bad-format", "messages": []}),
        encoding="utf-8",
    )
    return P()


def test_assert_format_direct():
    with pytest.raises(TrajectoryFormatError):
        assert_trajectory_format({"trajectory_format": "x"})
