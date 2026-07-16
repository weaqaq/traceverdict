"""Load harness run config YAML (e.g. configs/dev.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from traceverdict.core.simple_yaml import load_path


def resolve_config_path(config_spec: str | Path) -> Path:
    """Resolve --config path (or path with optional .yaml/.yml suffix) to a YAML file."""
    spec = Path(config_spec)
    candidates = [spec]
    if not spec.suffix:
        candidates.extend([Path(f"{spec}.yaml"), Path(f"{spec}.yml")])
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        f"config not found for {config_spec!r}; tried: "
        + ", ".join(str(c) for c in candidates)
    )


def load_config(config_spec: str | Path) -> dict[str, Any]:
    path = resolve_config_path(config_spec)
    raw = load_path(path)
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a mapping: {path}")

    required = (
        "config_id",
        "agent_name",
        "agent_version",
        "model_name",
        "model_params",
        "prompt_version",
        "harness_version",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"config missing fields {missing}: {path}")

    registry_spec = raw.get("litellm_model_registry")
    registry_path = None
    if registry_spec:
        registry_path = Path(registry_spec)
        if not registry_path.is_absolute():
            registry_path = (path.parent / registry_path).resolve()
        if not registry_path.is_file():
            raise FileNotFoundError(
                f"LiteLLM model registry not found: {registry_path}"
            )

    return {
        "config_id": raw["config_id"],
        "agent_name": raw["agent_name"],
        "agent_version": str(raw["agent_version"]),
        "model_name": raw["model_name"],
        "model_params": raw["model_params"] or {},
        "prompt_version": raw["prompt_version"],
        "harness_version": str(raw["harness_version"]),
        "notes": raw.get("notes"),
        "store_prompt_full": bool(raw.get("store_prompt_full", False)),
        "litellm_model_registry": registry_path,
        "container_cwd": raw.get("container_cwd", "/testbed"),
        "base_image": raw.get("base_image", "python:3.12-slim"),
        "local_image_tag": raw.get("local_image_tag", "traceverdict/self-base:py3.12-v1"),
        "suite_dockerfile": raw.get("suite_dockerfile", "Dockerfile"),
        "config_path": path,
    }
