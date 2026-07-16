"""M4-C local-agent/remote-verifier handoff primitives (D24)."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PATCH_PACKAGE_FORMAT = "traceverdict-m4c-patch-v1"
MAX_SUBSCRIPTION_WINDOWS = 3
CODEX_DISCLOSURES = (
    "The executor and subject share a vendor; ground truth remains the official tests.",
    "Scaffolding differences from mini are subject properties and are not normalized away.",
    "The agent layer is additive; the task test environment is unchanged.",
)

_FORBIDDEN_TEXT = (
    ("windows_user_path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I)),
    ("linux_home_path", re.compile(r"/home/[^/\s]+/")),
    ("codex_auth", re.compile(r"auth\.json|CODEX_HOME|TRACEVERDICT_CODEX_AUTH_FILE", re.I)),
    ("credential", re.compile(r"(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S+", re.I)),
    ("rented_host", re.compile(r"workspace\.featurize\.cn|featurize@", re.I)),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scan_handoff_text(text: str) -> list[str]:
    return [name for name, pattern in _FORBIDDEN_TEXT if pattern.search(text)]


def build_patch_manifest(
    *,
    task_id: str,
    run_id: str,
    patch: bytes,
    base_commit: str,
    original_image_digest: str,
    agent_env_fingerprint: str,
    config_id: str,
    codex_binary_sha256: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ValueError("base_commit must be a 40-character lowercase SHA1")
    if not re.fullmatch(r"[0-9a-f]{64}", codex_binary_sha256):
        raise ValueError("codex_binary_sha256 must be lowercase SHA256")
    return {
        "format": PATCH_PACKAGE_FORMAT,
        "task_id": task_id,
        "run_id": run_id,
        "patch_file": "patch.diff",
        "patch_sha256": sha256_bytes(patch),
        "base_commit": base_commit,
        "original_image_digest": original_image_digest,
        "agent_env_fingerprint": agent_env_fingerprint,
        "config_id": config_id,
        "codex_binary_sha256": codex_binary_sha256,
        "contains_agent_jsonl": False,
        "contains_credentials": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def write_patch_package(output: str | Path, *, manifest: dict[str, Any], patch: bytes) -> dict[str, Any]:
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    findings = scan_handoff_text(manifest_bytes.decode("utf-8"))
    findings.extend(scan_handoff_text(patch.decode("utf-8", errors="replace")))
    if findings:
        raise ValueError(f"patch handoff failed sensitive scan: {sorted(set(findings))}")
    if manifest.get("patch_sha256") != sha256_bytes(patch):
        raise ValueError("manifest patch SHA does not match patch bytes")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("patch.diff", patch)
    return {
        "path": str(output),
        "sha256": sha256_bytes(output.read_bytes()),
        "manifest": manifest,
    }


def validate_patch_package(path: str | Path) -> tuple[dict[str, Any], bytes]:
    with zipfile.ZipFile(path, "r") as archive:
        names = sorted(archive.namelist())
        if names != ["manifest.json", "patch.diff"]:
            raise ValueError(f"unexpected patch package entries: {names}")
        manifest = json.loads(archive.read("manifest.json"))
        patch = archive.read("patch.diff")
    if manifest.get("format") != PATCH_PACKAGE_FORMAT:
        raise ValueError("unsupported patch package format")
    if manifest.get("patch_sha256") != sha256_bytes(patch):
        raise ValueError("patch package SHA verification failed")
    findings = scan_handoff_text(json.dumps(manifest, ensure_ascii=False))
    findings.extend(scan_handoff_text(patch.decode("utf-8", errors="replace")))
    if findings:
        raise ValueError(f"patch package contains forbidden data: {sorted(set(findings))}")
    return manifest, patch


def append_subscription_window(
    ledger_path: str | Path,
    *,
    window_started_at: str,
    window_finished_at: str,
    completed_run_ids: list[str],
    quota_error: str | None,
    pause_reason: str | None,
) -> dict[str, Any]:
    path = Path(ledger_path)
    ledger = json.loads(path.read_text("utf-8")) if path.is_file() else {"windows": []}
    windows = ledger.setdefault("windows", [])
    if len(windows) >= MAX_SUBSCRIPTION_WINDOWS:
        raise RuntimeError("three subscription windows exhausted; owner ruling required")
    windows.append({
        "window_index": len(windows) + 1,
        "started_at": window_started_at,
        "finished_at": window_finished_at,
        "completed_run_ids": list(completed_run_ids),
        "quota_error": quota_error,
        "pause_reason": pause_reason,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return ledger
