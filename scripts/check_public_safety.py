"""Fail-closed safety scan for the clean public mirror."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

RULES = {
    "private-key": re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
    "token": re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})"),
    "windows-absolute-path": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!\\\\)"),
    "user-home-path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "private-ip": re.compile(r"(?<![0-9])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?![0-9])"),
    "credential-file-reference": re.compile(r"\.env(?:\d+)?\b", re.IGNORECASE),
    "private-identity": re.compile(r"(?:evaluation-user|local-user|workspace\.featurize\.cn|api\.ikuncode\.cc|劉立|鍔夌珛)", re.IGNORECASE),
    "proxy-setting": re.compile(r"(?:http_proxy|https_proxy|registry-mirrors)\s*[:=]", re.IGNORECASE),
}
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
SAFE_EMAIL_SUFFIXES = (
    "@users.noreply.github.com",
    "@example.com",
    "@example.invalid",
    "@app.get",  # lexical overlap in a generated fixture string, not an address
)


def run(*args: str, cwd: Path | None = None) -> bytes:
    return subprocess.check_output(args, cwd=cwd, stderr=subprocess.STDOUT)


def findings(label: str, data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="ignore")
    errors: list[str] = []
    for name, pattern in RULES.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{label}:{line}: {name}: {match.group(0)!r}")
    for match in EMAIL.finditer(text):
        value = match.group(0).lower()
        if not value.endswith(SAFE_EMAIL_SUFFIXES):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{label}:{line}: email: {match.group(0)!r}")
    return errors


def worktree_files() -> list[Path]:
    # Scan exactly the publication candidate: tracked files plus untracked
    # files that are not excluded by .gitignore.  Test caches and local Daily
    # state are intentionally outside the public artifact and must not make a
    # scan non-deterministic merely because they exist on a developer machine.
    raw = run(
        "git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
        cwd=ROOT,
    )
    files: list[Path] = []
    for rel in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        if path.is_file() and path.resolve() != SELF:
            files.append(path)
    return sorted(files)


def scan_worktree() -> list[str]:
    errors: list[str] = []
    for path in worktree_files():
        if path.suffix == ".bundle":
            continue
        errors.extend(findings(path.relative_to(ROOT).as_posix(), path.read_bytes()))
    return errors


def scan_git_repository(repo: Path, label: str) -> list[str]:
    errors: list[str] = []
    identities = run("git", "log", "--all", "--format=%an%x00%ae", cwd=repo)
    errors.extend(findings(f"{label}:authors", identities))
    for commit in run("git", "rev-list", "--all", cwd=repo).decode().split():
        paths = run("git", "ls-tree", "-r", "--name-only", commit, cwd=repo).decode("utf-8", "replace").splitlines()
        for rel in paths:
            if rel == "scripts/check_public_safety.py":
                continue
            try:
                data = run("git", "show", f"{commit}:{rel}", cwd=repo)
            except subprocess.CalledProcessError as exc:
                errors.append(f"{label}:{commit}:{rel}: unreadable object: {exc.output!r}")
                continue
            errors.extend(findings(f"{label}:{commit[:12]}:{rel}", data))
    return errors


def scan_bundles() -> list[str]:
    errors: list[str] = []
    for bundle in (path for path in worktree_files() if path.suffix == ".bundle"):
        with tempfile.TemporaryDirectory(prefix="traceverdict-bundle-") as tmp:
            repo = Path(tmp) / "repo"
            subprocess.check_call(
                ["git", "clone", "--quiet", str(bundle), str(repo)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            errors.extend(scan_git_repository(repo, bundle.relative_to(ROOT).as_posix()))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-objects", action="store_true")
    args = parser.parse_args()
    errors = scan_worktree() + scan_bundles()
    if args.git_objects:
        errors += scan_git_repository(ROOT, "public-git")
    if errors:
        print("PUBLIC SAFETY SCAN FAILED")
        print("\n".join(sorted(set(errors))))
        return 1
    files = worktree_files()
    print(f"PUBLIC SAFETY SCAN PASSED: {len(files)} files, {sum(path.suffix == '.bundle' for path in files)} bundles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
