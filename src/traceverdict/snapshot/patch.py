"""Authoritative patch from work copy relative to base_commit (D1-c)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from git import Repo


def collect_patch(work_path: Path, base_commit: str) -> tuple[str, str]:
    """Stage all changes (incl. untracked) and return (unified_diff, sha256).

    Uses ``git add -A`` then ``git diff --cached`` against base_commit so
    untracked new files are included (D1-c).
    """
    work_path = Path(work_path)
    repo = Repo(str(work_path))
    # Ensure base is known.
    repo.git.rev_parse(base_commit)
    repo.git.add("-A")
    # Diff of the index (after add -A) vs base tree — covers mods + new files.
    diff_text = repo.git.diff(base_commit, "--cached")
    if not diff_text.endswith("\n") and diff_text:
        diff_text = diff_text + "\n"
    sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    return diff_text, sha


def write_artifact_file(path: Path, content: str) -> str:
    """Write content to path; return sha256 of content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
