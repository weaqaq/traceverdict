"""Synchronize the wheel's Daily assets from canonical repository sources."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src" / "traceverdict" / "resources" / "daily"

FILES = (
    "configs/dev.yaml",
    "configs/litellm_models.json",
    "configs/litellm_models.meta.json",
    "tasks/self/task_set.txt",
    "tasks/self/_image/Dockerfile",
    "tasks/self/_image/metadata.json",
    "tasks/self/_image/requirements.txt",
    "tasks/self/_image/SWEAgent.Dockerfile",
    "tasks/self/_image/SWEAgentV2.Dockerfile",
    "tasks/self/_image/SWEAgentV3.Dockerfile",
    *(
        f"tasks/self/S{index}/{name}"
        for index in range(1, 12)
        for name in ("BASE_COMMIT.txt", "repo.bundle", "task.yaml", "verify/README.md")
    ),
)


def sync(*, check: bool) -> list[str]:
    errors: list[str] = []
    expected = {Path(relative) for relative in FILES}
    for relative in expected:
        source = ROOT / relative
        target = DEST / relative
        if not source.is_file():
            errors.append(f"missing canonical asset: {relative.as_posix()}")
            continue
        if check:
            if not target.is_file() or not filecmp.cmp(source, target, shallow=False):
                errors.append(f"resource drift: {relative.as_posix()}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if check and DEST.is_dir():
        actual = {
            path.relative_to(DEST)
            for path in DEST.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        for extra in sorted(actual - expected):
            errors.append(f"unexpected packaged asset: {extra.as_posix()}")
    return sorted(errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    errors = sync(check=args.check)
    if errors:
        print("DAILY RESOURCE SYNC FAILED")
        print("\n".join(errors))
        return 1
    print(f"DAILY RESOURCE SYNC {'CHECKED' if args.check else 'UPDATED'}: {len(FILES)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
