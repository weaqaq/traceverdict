"""Docker image pull/tag and digest fingerprint (D1-d). Strict Docker only."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class DockerUnavailableError(RuntimeError):
    """Docker CLI/daemon not available."""


def require_docker() -> str:
    """Return path to docker executable or raise DockerUnavailableError."""
    exe = shutil.which("docker")
    if not exe:
        raise DockerUnavailableError(
            "Docker CLI not found on PATH. D1-d requires DockerEnvironment; "
            "no local Python fallback is permitted."
        )
    try:
        r = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as e:
        raise DockerUnavailableError(f"Docker CLI not executable: {e}") from e
    if r.returncode != 0:
        raise DockerUnavailableError(
            f"Docker daemon not available (docker info failed): {r.stderr.strip() or r.stdout.strip()}"
        )
    return exe


def ensure_local_image(
    *,
    base_image: str,
    local_tag: str,
    docker_exe: str | None = None,
) -> str:
    """Use the pinned local tag, pulling/tagging only when it is absent."""
    exe = docker_exe or require_docker()
    local = subprocess.run(
        [exe, "image", "inspect", local_tag, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if local.returncode == 0 and local.stdout.strip():
        return image_digest(local_tag, docker_exe=exe)

    pull = subprocess.run(
        [exe, "pull", base_image],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if pull.returncode != 0:
        raise RuntimeError(f"docker pull {base_image} failed: {pull.stderr}")

    tag = subprocess.run(
        [exe, "tag", base_image, local_tag],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if tag.returncode != 0:
        raise RuntimeError(f"docker tag {base_image} -> {local_tag} failed: {tag.stderr}")

    return image_digest(local_tag, docker_exe=exe)


def image_digest(image_ref: str, *, docker_exe: str | None = None) -> str:
    """Return RepoDigest or Image ID for fingerprinting."""
    exe = docker_exe or require_docker()
    # Prefer RepoDigests when present.
    insp = subprocess.run(
        [exe, "image", "inspect", image_ref, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if insp.returncode != 0:
        raise RuntimeError(f"docker image inspect failed for {image_ref}: {insp.stderr}")
    data: dict[str, Any] = json.loads(insp.stdout)
    digests = data.get("RepoDigests") or []
    if digests:
        # e.g. "python@sha256:..."
        d = digests[0]
        if "@" in d:
            return d.split("@", 1)[1]
        return d
    image_id = data.get("Id") or ""
    if not image_id:
        raise RuntimeError(f"no digest/Id for image {image_ref}")
    return image_id


def make_env_fingerprint(image_digest_str: str, base_commit: str) -> str:
    """env_fingerprint = image digest + base_commit (D1-d)."""
    return f"{image_digest_str}+{base_commit}"
