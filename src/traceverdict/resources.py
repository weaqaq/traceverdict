"""Resolve immutable runtime assets from a checkout or an installed wheel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DailyAssets:
    root: Path
    configs: Path
    tasks_self: Path
    source: str


def _candidate(root: Path, source: str) -> DailyAssets | None:
    configs = root / "configs"
    tasks_self = root / "tasks" / "self"
    required = (
        configs / "dev.yaml",
        configs / "litellm_models.json",
        tasks_self / "task_set.txt",
        tasks_self / "S1" / "repo.bundle",
        tasks_self / "S8" / "repo.bundle",
        tasks_self / "_image" / "Dockerfile",
    )
    if all(path.is_file() for path in required):
        return DailyAssets(root=root, configs=configs, tasks_self=tasks_self, source=source)
    return None


def resolve_daily_assets() -> DailyAssets:
    """Prefer canonical checkout assets and fall back to packaged copies."""
    candidates = (
        (Path.cwd(), "working-tree"),
        (Path(__file__).resolve().parents[2], "source-checkout"),
        (Path(__file__).resolve().parent / "resources" / "daily", "package-resources"),
    )
    seen: set[Path] = set()
    for root, source in candidates:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        assets = _candidate(root, source)
        if assets is not None:
            return assets
    raise RuntimeError("Daily default assets are unavailable or incomplete")
