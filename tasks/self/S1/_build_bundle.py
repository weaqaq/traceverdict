"""One-shot helper to freeze S1 repo into repo.bundle. Run from repo root."""

from __future__ import annotations

import shutil
from pathlib import Path

from git import Repo

ROOT = Path(__file__).resolve().parent
REPO_SRC = ROOT / "_repo_src"
BUNDLE = ROOT / "repo.bundle"


def main() -> None:
    if REPO_SRC.exists():
        shutil.rmtree(REPO_SRC)
    REPO_SRC.mkdir(parents=True)

    (REPO_SRC / "calc.py").write_text(
        'def add(a: int, b: int) -> int:\n'
        '    """Return the sum of a and b."""\n'
        "    return a - b  # intentional bug for S1\n",
        encoding="utf-8",
    )
    (REPO_SRC / "tests").mkdir()
    (REPO_SRC / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n\n"
        "def test_add_zero():\n"
        "    assert add(0, 0) == 0\n",
        encoding="utf-8",
    )
    (REPO_SRC / "README.md").write_text("S1 fixture: broken add()\n", encoding="utf-8")

    repo = Repo.init(str(REPO_SRC))
    repo.git.config("user.email", "traceverdict@local")
    repo.git.config("user.name", "traceverdict")
    repo.git.add("-A")
    repo.index.commit("initial broken add")
    sha = repo.head.commit.hexsha

    if BUNDLE.exists():
        BUNDLE.unlink()
    repo.git.bundle("create", str(BUNDLE), "HEAD")
    (ROOT / "BASE_COMMIT.txt").write_text(sha + "\n", encoding="utf-8")
    # Close git handles before delete (Windows).
    repo.close()
    del repo
    import gc
    import stat
    import time

    gc.collect()
    time.sleep(0.2)

    def _onexc(func, path, exc_info):
        Path(path).chmod(stat.S_IWRITE)
        func(path)

    shutil.rmtree(REPO_SRC, onexc=_onexc)
    print(sha)


if __name__ == "__main__":
    main()
