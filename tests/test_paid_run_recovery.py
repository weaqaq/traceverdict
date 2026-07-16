from __future__ import annotations

from unittest.mock import patch

import pytest
from git import Repo

from scripts.recover_swebv_paid_run import (
    backup_frozen_git_metadata,
    replay_actions,
    restore_frozen_git_metadata,
)
from traceverdict.snapshot.patch import collect_patch


def _trajectory() -> dict:
    return {
        "messages": [
            {
                "role": "assistant",
                "extra": {
                    "actions": [
                        {"command": "true", "tool_call_id": "call-1"},
                        {"command": "echo submit", "tool_call_id": "call-2"},
                    ]
                },
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "extra": {"returncode": 0},
            },
            {"role": "exit", "extra": {"exit_status": "Submitted"}},
        ]
    }


def test_zero_model_replay_matches_recorded_returncodes(tmp_path):
    work = tmp_path / "traceverdict-work-recover"
    work.mkdir()
    returncodes = iter([0, 0, 0, 0])

    calls = []

    def fake_run(*args, **_kwargs):
        calls.append(args[0])
        class Proc:
            returncode = next(returncodes)
            stdout = ""
            stderr = ""

        return Proc()

    with patch("scripts.recover_swebv_paid_run.subprocess.run", side_effect=fake_run):
        result = replay_actions(traj=_trajectory(), work_copy=work, image="img")

    assert result == {
        "actions": 2,
        "observations": 1,
        "matched_returncodes": 1,
        "unobserved_exit_actions": 1,
    }
    exec_calls = [call for call in calls if len(call) > 1 and call[1] == "exec"]
    assert exec_calls[0][-3:-1] == ["bash", "-c"]
    assert "-lc" not in exec_calls[0]


def test_zero_model_replay_stops_on_returncode_divergence(tmp_path):
    work = tmp_path / "traceverdict-work-recover"
    work.mkdir()
    returncodes = iter([0, 3, 0])

    def fake_run(*_args, **_kwargs):
        class Proc:
            returncode = next(returncodes)
            stdout = ""
            stderr = ""

        return Proc()

    with patch("scripts.recover_swebv_paid_run.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="diverged at action 0"):
            replay_actions(traj=_trajectory(), work_copy=work, image="img")


def test_restore_frozen_git_metadata_survives_agent_reinit(tmp_path):
    work = tmp_path / "traceverdict-work-recover"
    repo = Repo.init(work)
    (work / "source.py").write_text("old\n", "utf-8")
    repo.index.add(["source.py"])
    repo.index.commit("base")
    base_commit = repo.head.commit.hexsha
    backup = backup_frozen_git_metadata(work)

    # Model the recorded destructive action while retaining its source edit.
    (work / "source.py").write_text("new\n", "utf-8")
    import shutil

    shutil.rmtree(work / ".git")
    Repo.init(work).index.add(["source.py"])

    restore_frozen_git_metadata(work, backup)
    patch_text, _ = collect_patch(work, base_commit)
    assert "-old" in patch_text
    assert "+new" in patch_text
