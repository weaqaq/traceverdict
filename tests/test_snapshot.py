"""Snapshot unit tests: work copy + patch includes untracked files (D1-c/d)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from git import Repo

from traceverdict.snapshot.patch import collect_patch
from traceverdict.snapshot import workspace as workspace_mod
from traceverdict.snapshot.workspace import (
    cleanup_work_copy,
    materialize_work_copy,
    repair_work_copy_ownership,
)
from traceverdict.core.runner import _detach_swe_agent_origin


def test_swe_agent_disposable_copy_detaches_host_only_origin(tmp_path: Path):
    repo = Repo.init(tmp_path)
    repo.create_remote("origin", "/host-only/repo.bundle")

    _detach_swe_agent_origin(tmp_path)

    assert list(repo.remotes) == []


def _make_bundle(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hello\n", encoding="utf-8")
    repo = Repo.init(str(src))
    repo.git.config("user.email", "t@t")
    repo.git.config("user.name", "t")
    repo.git.add("-A")
    repo.index.commit("init")
    sha = repo.head.commit.hexsha
    bundle = tmp_path / "repo.bundle"
    repo.git.bundle("create", str(bundle), "HEAD")
    repo.close()
    return bundle, sha


def test_materialize_and_patch_includes_untracked(tmp_path: Path):
    bundle, sha = _make_bundle(tmp_path)
    work = materialize_work_copy(bundle, sha, dest_parent=tmp_path / "works")
    try:
        # Modify tracked + add untracked new file
        (work / "hello.txt").write_text("hello world\n", encoding="utf-8")
        (work / "new_file.py").write_text("print('new')\n", encoding="utf-8")
        diff, sha256 = collect_patch(work, sha)
        assert sha256
        assert "hello.txt" in diff
        assert "new_file.py" in diff
        assert "print('new')" in diff
    finally:
        cleanup_work_copy(work)
    assert not work.exists()


def test_env_fingerprint_format():
    from traceverdict.snapshot.image import make_env_fingerprint

    fp = make_env_fingerprint("sha256:deadbeef", "abc123")
    assert fp == "sha256:deadbeef+abc123"


def test_cleanup_repairs_container_root_ownership(tmp_path: Path):
    work = tmp_path / "traceverdict-work-root-owned"
    work.mkdir()
    (work / "root-owned.txt").write_text("x", encoding="utf-8")
    real_rmtree = shutil.rmtree
    attempts = 0

    def flaky_rmtree(path, *, onexc):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("root owned")
        real_rmtree(path)

    with (
        patch.object(workspace_mod.shutil, "rmtree", side_effect=flaky_rmtree),
        patch.object(workspace_mod.os, "getuid", return_value=1000, create=True),
        patch.object(workspace_mod.os, "getgid", return_value=1000, create=True),
        patch.object(workspace_mod.subprocess, "run") as docker_run,
    ):
        cleanup_work_copy(work, docker_executable="docker", image="task:image")

    assert not work.exists()
    assert attempts == 2
    cmd = docker_run.call_args.args[0]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert f"{work.resolve()}:/cleanup:rw" in cmd
    assert cmd[-3:] == ["task:image", "-c", "chown -R 1000:1000 /cleanup"]


def test_repair_ownership_before_host_git_collection(tmp_path: Path):
    work = tmp_path / "traceverdict-work-before-patch"
    work.mkdir()
    with (
        patch.object(workspace_mod.os, "getuid", return_value=1000, create=True),
        patch.object(workspace_mod.os, "getgid", return_value=1001, create=True),
        patch.object(workspace_mod.subprocess, "run") as docker_run,
    ):
        repair_work_copy_ownership(
            work, docker_executable="docker", image="task:image"
        )

    cmd = docker_run.call_args.args[0]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert f"{work.resolve()}:/cleanup:rw" in cmd
    assert cmd[-3:] == ["task:image", "-c", "chown -R 1000:1001 /cleanup"]


def test_existing_local_image_skips_network_pull():
    from traceverdict.snapshot.image import ensure_local_image

    calls: list[list[str]] = []

    class Proc:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-2:] == ["--format", "{{.Id}}"]:
            return Proc("sha256:local\n")
        return Proc(json.dumps({"RepoDigests": ["traceverdict/self-base@sha256:pinned"]}))

    with patch("traceverdict.snapshot.image.subprocess.run", side_effect=fake_run):
        digest = ensure_local_image(
            base_image="python:3.12-slim",
            local_tag="traceverdict/self-base:py3.12-v1",
            docker_exe="docker",
        )

    assert digest == "sha256:pinned"
    assert not any(len(cmd) > 1 and cmd[1] == "pull" for cmd in calls)


def test_missing_suite_image_builds_from_nearest_image_dir(tmp_path: Path):
    from traceverdict.snapshot.suite_image import ensure_suite_image, find_suite_image_dir

    image_dir = tmp_path / "tasks" / "self" / "_image"
    image_dir.mkdir(parents=True)
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    task_dir = tmp_path / "tasks" / "self" / "S2"
    task_dir.mkdir()

    class Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["image", "inspect"]:
            return Proc(returncode=1, stderr="missing")
        return Proc(returncode=0, stdout="built")

    assert find_suite_image_dir(task_dir) == image_dir
    with (
        patch("traceverdict.snapshot.suite_image.subprocess.run", side_effect=fake_run),
        patch("traceverdict.snapshot.suite_image.image_digest", return_value="sha256:built"),
    ):
        digest = ensure_suite_image(
            task_dir=task_dir,
            image_ref="traceverdict/self-base:py3.12-v1",
            docker_exe="docker",
        )

    assert digest == "sha256:built"
    build = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "build"]
    assert len(build) == 1
    assert "--pull=false" in build[0]


def test_suite_image_can_use_swe_agent_derivative(tmp_path: Path):
    from traceverdict.snapshot.suite_image import ensure_suite_image

    image_dir = tmp_path / "tasks" / "self" / "_image"
    image_dir.mkdir(parents=True)
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    derivative = image_dir / "SWEAgent.Dockerfile"
    derivative.write_text("FROM base\n", encoding="utf-8")
    task_dir = tmp_path / "tasks" / "self" / "S1"
    task_dir.mkdir()

    class Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["image", "inspect"]:
            return Proc(returncode=1, stderr="missing")
        return Proc(returncode=0, stdout="built")

    with (
        patch("traceverdict.snapshot.suite_image.subprocess.run", side_effect=fake_run),
        patch("traceverdict.snapshot.suite_image.image_digest", return_value="sha256:derived"),
    ):
        digest = ensure_suite_image(
            task_dir=task_dir,
            image_ref="traceverdict/self-base:swe-agent-1.1.0-v1",
            docker_exe="docker",
            dockerfile_name="SWEAgent.Dockerfile",
        )

    assert digest == "sha256:derived"
    build = [cmd for cmd in calls if len(cmd) > 1 and cmd[1] == "build"]
    assert len(build) == 1
    assert str(derivative) in build[0]
