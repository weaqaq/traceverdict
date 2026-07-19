from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


def _module():
    path = Path("scripts/check_public_safety.py").resolve()
    spec = importlib.util.spec_from_file_location("public_safety_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worktree_scan_uses_git_publication_boundary(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "candidate.txt").write_text("safe", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "local-secret.txt").write_text("not a publication candidate", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "tracked.txt"], check=True)

    module = _module()
    module.ROOT = tmp_path
    module.SELF = tmp_path / "scanner.py"
    relative = {path.relative_to(tmp_path).as_posix() for path in module.worktree_files()}

    assert relative == {".gitignore", "candidate.txt", "tracked.txt"}
