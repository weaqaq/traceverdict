"""Self-suite discovery and dry-run validation (T2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from traceverdict.core.config_loader import load_config
from traceverdict.core.task_loader import load_task
from traceverdict.snapshot.image import require_docker
from traceverdict.snapshot.suite_image import ensure_suite_image, find_suite_image_dir

EXPECTED_SELF_IDS = tuple(f"S{i}" for i in range(1, 9))
SELF_IMAGE_REF = "traceverdict/self-base:py3.12-v1"


def validate_suite(
    suite_path: str | Path,
    config_spec: str | Path,
    *,
    ensure_image: bool = True,
) -> dict[str, Any]:
    suite_dir = Path(suite_path).resolve()
    if not suite_dir.is_dir():
        raise FileNotFoundError(f"suite directory not found: {suite_dir}")
    cfg = load_config(config_spec)
    task_dirs = sorted(
        (p for p in suite_dir.iterdir() if p.is_dir() and (p / "task.yaml").is_file()),
        key=lambda p: p.name,
    )
    ids = tuple(p.name for p in task_dirs)
    if ids != EXPECTED_SELF_IDS:
        raise ValueError(f"self suite must contain exactly {EXPECTED_SELF_IDS}, got {ids}")
    image_dir = find_suite_image_dir(task_dirs[0])
    if image_dir != suite_dir / "_image":
        raise ValueError(f"shared suite image description not found under {suite_dir / '_image'}")

    tasks = []
    for task_dir in task_dirs:
        task = load_task(task_dir)
        if task["task_id"] != task_dir.name:
            raise ValueError(f"task id/path mismatch: {task['task_id']} vs {task_dir.name}")
        if task["suite"] != "self":
            raise ValueError(f"{task['task_id']} suite must be 'self'")
        if task["image_ref"] != SELF_IMAGE_REF:
            raise ValueError(f"{task['task_id']} must use shared image {SELF_IMAGE_REF}")
        if not re.fullmatch(r"[0-9a-f]{40}", str(task["base_commit"])):
            raise ValueError(f"{task['task_id']} base_commit must be 40 lowercase hex")
        if not task["repo_ref_path"].is_file():
            raise FileNotFoundError(f"{task['task_id']} bundle missing")
        if not (task_dir / "verify" / "README.md").is_file():
            raise FileNotFoundError(f"{task['task_id']} verify/README.md missing")
        tasks.append(
            {
                "id": task["task_id"],
                "tags": task["tags"],
                "image_ref": task["image_ref"],
                "valid": True,
            }
        )

    image_digest = None
    if ensure_image:
        docker_exe = require_docker()
        image_digest = ensure_suite_image(
            task_dir=task_dirs[0], image_ref=SELF_IMAGE_REF, docker_exe=docker_exe
        )
    return {
        "suite": "self",
        "config_id": cfg["config_id"],
        "dry_run": True,
        "count": len(tasks),
        "image_ref": SELF_IMAGE_REF,
        "image_digest": image_digest,
        "tasks": tasks,
    }
