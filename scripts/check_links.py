"""Validate relative Markdown links without fetching the network."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")


def main() -> int:
    missing: list[str] = []
    checked = 0
    for document in ROOT.rglob("*.md"):
        for raw in LINK.findall(document.read_text("utf-8")):
            target = raw.split("#", 1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            checked += 1
            if not (document.parent / target).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)} -> {raw}")
    if missing:
        print("BROKEN LOCAL LINKS")
        print("\n".join(missing))
        return 1
    print(f"LOCAL LINK CHECK PASSED: {checked} links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
