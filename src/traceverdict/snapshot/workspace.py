"""One-shot work copies from frozen git bundle (D1-d). Never mount task fixture raw."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from git import Repo


def materialize_work_copy(
    bundle_path: Path,
    base_commit: str,
    *,
    dest_parent: Path | None = None,
) -> Path:
    """Checkout bundle@base_commit into a new temporary directory; return work copy path.

    Caller must discard the directory after the run (use cleanup_work_copy).
    """
    bundle_path = Path(bundle_path).resolve()
    if not bundle_path.is_file():
        raise FileNotFoundError(f"git bundle not found: {bundle_path}")

    parent = Path(dest_parent) if dest_parent else Path(tempfile.mkdtemp(prefix="traceverdict-work-"))
    parent.mkdir(parents=True, exist_ok=True)
    work = parent if dest_parent is None else Path(tempfile.mkdtemp(prefix="traceverdict-work-", dir=str(parent)))

    # Clone from bundle into work.
    repo = Repo.clone_from(str(bundle_path), str(work))
    repo.git.checkout(base_commit)
    # Ensure clean state at base.
    repo.git.reset("--hard", base_commit)
    repo.git.clean("-fdx")
    return work


def cleanup_work_copy(
    work_path: Path,
    *,
    docker_executable: str | None = None,
    image: str | None = None,
) -> None:
    """Delete a one-shot work copy tree."""
    path = Path(work_path)
    if not path.exists():
        return

    def clear_readonly_and_retry(func, failed_path, exc) -> None:
        """Git bundle objects are commonly read-only on Windows."""
        os.chmod(failed_path, stat.S_IREAD | stat.S_IWRITE)
        func(failed_path)

    try:
        shutil.rmtree(path, onexc=clear_readonly_and_retry)
    except PermissionError:
        # Agent containers run as root so editable installs can leave root-owned
        # metadata in the host-mounted disposable checkout.  Repair ownership
        # with the already-local task image, then retry on the host.  Mount only
        # the one-shot directory and never the frozen fixture/bundle.
        repair_work_copy_ownership(
            path, docker_executable=docker_executable, image=image
        )
        shutil.rmtree(path, onexc=clear_readonly_and_retry)
    if path.exists():
        raise RuntimeError(f"failed to remove one-shot work copy: {path}")


def repair_work_copy_ownership(
    work_path: Path,
    *,
    docker_executable: str | None,
    image: str | None,
) -> None:
    """Return a disposable container-mounted checkout to the host user.

    Agent containers commonly run as root.  Git may therefore create root-owned
    objects while the agent edits the bind mount, and host-side ``git add`` must
    not run until ownership is repaired.  The repair mounts only the validated
    one-shot checkout; the frozen task bundle is never exposed.
    """
    path = Path(work_path)
    if not path.exists():
        return
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return
    if not docker_executable or not image:
        raise ValueError("Docker executable and task image are required for ownership repair")
    if not path.name.startswith("traceverdict-work-"):
        raise ValueError(f"refusing ownership repair outside a one-shot checkout: {path}")
    subprocess.run(
        [
            docker_executable,
            "run",
            "--rm",
            "-v",
            f"{path.resolve()}:/cleanup:rw",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            f"chown -R {getuid()}:{getgid()} /cleanup",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
