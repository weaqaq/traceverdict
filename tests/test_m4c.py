from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from traceverdict.m4c import (
    MAX_SUBSCRIPTION_WINDOWS,
    append_subscription_window,
    build_patch_manifest,
    validate_patch_package,
    write_patch_package,
)


def _manifest(patch: bytes):
    return build_patch_manifest(
        task_id="org__repo-1",
        run_id="run-1",
        patch=patch,
        base_commit="a" * 40,
        original_image_digest="sha256:base",
        agent_env_fingerprint="sha256:layer+base",
        config_id="m4c",
        codex_binary_sha256="b" * 64,
    )


def test_patch_package_contains_only_manifest_and_patch(tmp_path: Path):
    patch = b"diff --git a/x b/x\n"
    output = tmp_path / "handoff.zip"
    result = write_patch_package(output, manifest=_manifest(patch), patch=patch)
    assert len(result["sha256"]) == 64
    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == ["manifest.json", "patch.diff"]
    manifest, restored = validate_patch_package(output)
    assert restored == patch
    assert manifest["contains_credentials"] is False


def test_patch_package_rejects_credentials_paths_and_tampering(tmp_path: Path):
    with pytest.raises(ValueError, match="sensitive scan"):
        write_patch_package(
            tmp_path / "bad.zip",
            manifest=_manifest(b"password=secret\n"),
            patch=b"password=secret\n",
        )
    patch = b"diff --git a/x b/x\n"
    output = tmp_path / "tampered.zip"
    manifest = _manifest(patch)
    manifest["patch_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        write_patch_package(output, manifest=manifest, patch=patch)


def test_subscription_windows_stop_after_three(tmp_path: Path):
    ledger = tmp_path / "windows.json"
    for index in range(MAX_SUBSCRIPTION_WINDOWS):
        result = append_subscription_window(
            ledger,
            window_started_at=f"2026-07-1{index}T00:00:00Z",
            window_finished_at=f"2026-07-1{index}T01:00:00Z",
            completed_run_ids=[f"run-{index}"],
            quota_error="quota" if index < 2 else None,
            pause_reason="window_exhausted" if index < 2 else None,
        )
    assert len(result["windows"]) == 3
    with pytest.raises(RuntimeError, match="owner ruling"):
        append_subscription_window(
            ledger,
            window_started_at="x",
            window_finished_at="y",
            completed_run_ids=[],
            quota_error="quota",
            pause_reason="window_exhausted",
        )
