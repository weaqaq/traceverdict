"""Load frozen task.yaml into a structured dict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from traceverdict.core.simple_yaml import load_path


def load_task(task_path: str | Path) -> dict[str, Any]:
    """Load task directory or task.yaml path. Returns normalized task dict + paths."""
    path = Path(task_path).resolve()
    if path.is_dir():
        task_dir = path
        yaml_path = path / "task.yaml"
    else:
        yaml_path = path
        task_dir = path.parent
    if not yaml_path.is_file():
        raise FileNotFoundError(f"task.yaml not found: {yaml_path}")

    raw = load_path(yaml_path)
    if not isinstance(raw, dict):
        raise ValueError(f"task.yaml must be a mapping: {yaml_path}")

    required = (
        "id",
        "suite",
        "source",
        "repo_ref",
        "base_commit",
        "image_ref",
        "instruction",
        "budget",
        "forbidden_paths",
        "gt",
        "tags",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"task.yaml missing fields {missing}: {yaml_path}")

    budget = raw["budget"]
    for bk in ("max_steps", "max_tokens", "max_wall_s", "max_cost_usd"):
        if bk not in budget:
            raise ValueError(f"budget missing {bk}")

    gt = raw["gt"]
    if "type" not in gt or "spec" not in gt:
        raise ValueError("gt must have type and spec")

    return {
        "task_id": raw["id"],
        "suite": raw["suite"],
        "source": raw["source"],
        "repo_ref": raw["repo_ref"],
        "base_commit": raw["base_commit"],
        "image_ref": raw["image_ref"],
        "instruction": raw["instruction"],
        "budget": budget,
        "forbidden_paths": list(raw["forbidden_paths"] or []),
        "gt": gt,
        "tags": list(raw["tags"] or []),
        "task_dir": task_dir,
        "repo_ref_path": (task_dir / raw["repo_ref"]).resolve()
        if not Path(raw["repo_ref"]).is_absolute()
        else Path(raw["repo_ref"]),
    }
