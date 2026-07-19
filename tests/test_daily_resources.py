from __future__ import annotations

import importlib.util
from pathlib import Path

from traceverdict.resources import resolve_daily_assets


def _sync_module():
    path = Path("scripts/sync_daily_resources.py").resolve()
    spec = importlib.util.spec_from_file_location("daily_resource_sync_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_daily_resources_are_byte_identical() -> None:
    assert _sync_module().sync(check=True) == []


def test_daily_assets_prefer_checkout() -> None:
    assets = resolve_daily_assets()
    assert assets.source in {"working-tree", "source-checkout"}
    assert assets.configs.joinpath("dev.yaml").is_file()
    assert assets.tasks_self.joinpath("S1", "repo.bundle").is_file()
