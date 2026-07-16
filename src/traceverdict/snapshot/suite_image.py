"""Discover and build suite-owned Docker images (D2-b/D3-a)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from traceverdict.snapshot.image import image_digest


def find_suite_image_dir(task_dir: Path) -> Path | None:
    """Find the nearest parent containing an ``_image/Dockerfile``."""
    current = Path(task_dir).resolve()
    for parent in (current, *current.parents):
        candidate = parent / "_image"
        if (candidate / "Dockerfile").is_file():
            return candidate
        if (parent / ".git").exists():
            break
    return None


def ensure_suite_image(
    *,
    task_dir: Path,
    image_ref: str,
    docker_exe: str,
    dockerfile_name: str = "Dockerfile",
) -> str | None:
    """Build a suite image only when its declared local tag is absent."""
    image_dir = find_suite_image_dir(task_dir)
    if image_dir is None:
        return None
    inspect = subprocess.run(
        [docker_exe, "image", "inspect", image_ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        build = subprocess.run(
            [
                docker_exe,
                "build",
                "--pull=false",
                "--file",
                str(image_dir / dockerfile_name),
                "--tag",
                image_ref,
                str(image_dir),
            ],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if build.returncode != 0:
            raise RuntimeError(
                f"suite image build failed for {image_ref}: {build.stderr or build.stdout}"
            )
    return image_digest(image_ref, docker_exe=docker_exe)
