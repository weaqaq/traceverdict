"""Build a no-package Codex layer on top of an exact task image."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from traceverdict.adapters.mini_swe_agent import AdapterHarnessError
from traceverdict.snapshot.image import image_digest

AUTH_SHELL_WRAPPER = """#!/bin/sh
set -eu
umount /run/traceverdict-codex/auth.json 2>/dev/null || true
if ! mount -t tmpfs -o mode=0700,nosuid,nodev,noexec tmpfs /run/traceverdict-codex; then
    echo "traceverdict: failed to isolate Codex credentials from agent shell" >&2
    exit 126
fi
exec /opt/traceverdict/bash-real "$@"
"""
AUTH_SHELL_WRAPPER_SHA256 = hashlib.sha256(AUTH_SHELL_WRAPPER.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_codex_agent_image(
    *,
    base_image: str,
    docker_executable: str,
    expected_binary_sha256: str,
    expected_bwrap_sha256: str,
    expected_version: str,
) -> tuple[str, str, dict[str, str]]:
    """Return (tag, digest, evidence) for an immutable local Codex layer."""
    binary_spec = os.environ.get("TRACEVERDICT_CODEX_LINUX_BINARY")
    if not binary_spec:
        raise AdapterHarnessError("TRACEVERDICT_CODEX_LINUX_BINARY is required")
    binary = Path(binary_spec).resolve()
    if not binary.is_file():
        raise AdapterHarnessError("pinned Codex Linux binary does not exist")
    actual_sha = _sha256(binary)
    if actual_sha != expected_binary_sha256:
        raise AdapterHarnessError(
            f"Codex binary SHA256 mismatch: {actual_sha} != {expected_binary_sha256}"
        )
    bwrap_spec = os.environ.get("TRACEVERDICT_CODEX_BWRAP_BINARY")
    if not bwrap_spec:
        raise AdapterHarnessError("TRACEVERDICT_CODEX_BWRAP_BINARY is required")
    bwrap = Path(bwrap_spec).resolve()
    if not bwrap.is_file():
        raise AdapterHarnessError("pinned Codex bwrap binary does not exist")
    actual_bwrap_sha = _sha256(bwrap)
    if actual_bwrap_sha != expected_bwrap_sha256:
        raise AdapterHarnessError(
            f"Codex bwrap SHA256 mismatch: {actual_bwrap_sha} != {expected_bwrap_sha256}"
        )
    base_digest = image_digest(base_image, docker_exe=docker_executable)
    identity = hashlib.sha256(
        (
            f"{base_digest}\0{expected_version}\0{actual_sha}\0{actual_bwrap_sha}"
            f"\0{AUTH_SHELL_WRAPPER_SHA256}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    tag = f"traceverdict/codex-agent:{identity}"
    inspect = subprocess.run(
        [docker_executable, "image", "inspect", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        cache_root = Path(
            os.environ.get("TRACEVERDICT_RUNTIME_CACHE", str(binary.parent))
        ).resolve()
        context = cache_root / f"layer-{identity}"
        context.mkdir(parents=True, exist_ok=True)
        context_binary = context / "codex"
        if not context_binary.exists() or _sha256(context_binary) != actual_sha:
            if context_binary.exists():
                context_binary.unlink()
            try:
                os.link(binary, context_binary)
            except OSError:
                shutil.copy2(binary, context_binary)
        context_bwrap = context / "bwrap"
        if not context_bwrap.exists() or _sha256(context_bwrap) != actual_bwrap_sha:
            if context_bwrap.exists():
                context_bwrap.unlink()
            try:
                os.link(bwrap, context_bwrap)
            except OSError:
                shutil.copy2(bwrap, context_bwrap)
        auth_shell_wrapper = context / "bash"
        auth_shell_wrapper.write_text(AUTH_SHELL_WRAPPER, encoding="utf-8", newline="\n")
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(
            f"FROM {base_image}\n"
            "COPY --chmod=0755 codex /opt/traceverdict/codex\n"
            "COPY --chmod=0755 bwrap /opt/traceverdict/codex-resources/bwrap\n"
            "RUN cp /bin/bash /opt/traceverdict/bash-real\n"
            "COPY --chmod=0755 bash /bin/bash\n"
            "RUN mkdir -p /run/traceverdict-codex && : > /run/traceverdict-codex/auth.json\n"
            f'LABEL org.traceverdict.codex.version="{expected_version}" '
            f'org.traceverdict.codex.sha256="{actual_sha}" '
            f'org.traceverdict.codex.bwrap.sha256="{actual_bwrap_sha}" '
            f'org.traceverdict.codex.auth_shell_wrapper.sha256="{AUTH_SHELL_WRAPPER_SHA256}" '
            f'org.traceverdict.base.digest="{base_digest}"\n',
            encoding="utf-8",
        )
        built = subprocess.run(
            [docker_executable, "build", "--network=none", "--pull=false", "-t", tag, str(context)],
            capture_output=True,
            text=True,
            check=False,
        )
        if built.returncode != 0:
            raise AdapterHarnessError(
                "Codex static binary layer failed without modifying the base image: "
                + (built.stderr or built.stdout)[-2000:]
            )
    digest = image_digest(tag, docker_exe=docker_executable)
    evidence = {
        "tag": tag,
        "digest": digest,
        "base_image": base_image,
        "base_digest": base_digest,
        "codex_version": expected_version,
        "codex_binary_sha256": actual_sha,
        "codex_bwrap_binary_sha256": actual_bwrap_sha,
        "auth_shell_wrapper_sha256": AUTH_SHELL_WRAPPER_SHA256,
        "network_mode": "bridge",
        "layer_policy": "official_static_runtime_plus_per_tool_auth_namespace_no_packages",
    }
    return tag, digest, evidence
