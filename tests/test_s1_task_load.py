"""S1 task/config load smoke (no Docker)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from traceverdict.core.config_loader import load_config
from traceverdict.core.runner import _upsert_config
from traceverdict.core.task_loader import load_task
from traceverdict.tracer.db import init_db

ROOT = Path(__file__).resolve().parents[1]


def test_load_s1_task_and_dev_config():
    task = load_task(ROOT / "tasks" / "self" / "S1")
    assert task["task_id"] == "S1"
    assert task["repo_ref_path"].name == "repo.bundle"
    assert task["repo_ref_path"].is_file()
    assert task["base_commit"]
    assert task["image_ref"] == "traceverdict/self-base:py3.12-v1"
    assert task["gt"]["spec"]["fail_to_pass"] == [
        "tests/test_calc.py::test_add",
        "tests/test_calc.py::test_add_zero",
    ]

    cfg = load_config(ROOT / "configs" / "dev.yaml")
    assert cfg["config_id"] == "dev-deepseek-v4-flash-v2"
    assert cfg["agent_version"] == "2.4.5"
    assert cfg["model_name"] == "openai/deepseek-v4-flash"
    assert cfg["model_params"] == {"thinking": {"type": "enabled"}}
    assert cfg["litellm_model_registry"].is_file()
    assert cfg["local_image_tag"] == "traceverdict/self-base:py3.12-v1"
    assert cfg["suite_dockerfile"] == "Dockerfile"


def test_exp_c_v4_freezes_derived_image_descriptor():
    cfg = load_config(ROOT / "configs" / "m3_swe_agent_deepseek_v4.yaml")
    assert cfg["config_id"].endswith("thinking-v4")
    assert cfg["local_image_tag"] == "traceverdict/self-base:swe-agent-1.1.0-v2"
    assert cfg["suite_dockerfile"] == "SWEAgentV2.Dockerfile"


def test_exp_c_v5_keeps_the_derived_image_identity():
    cfg = load_config(ROOT / "configs" / "m3_swe_agent_deepseek_v5.yaml")
    assert cfg["config_id"].endswith("thinking-v5")
    assert cfg["local_image_tag"] == "traceverdict/self-base:swe-agent-1.1.0-v2"
    assert cfg["suite_dockerfile"] == "SWEAgentV2.Dockerfile"


def test_exp_c_v6_bounds_request_and_retry():
    cfg = load_config(ROOT / "configs" / "m3_swe_agent_deepseek_v6.yaml")
    assert cfg["config_id"].endswith("thinking-v6")
    assert cfg["model_params"]["completion_kwargs"]["timeout"] == 180
    assert cfg["model_params"]["retry"] == {
        "retries": 1,
        "min_wait": 1,
        "max_wait": 1,
    }
    assert cfg["local_image_tag"] == "traceverdict/self-base:swe-agent-1.1.0-v2"


def test_exp_c_v7_bakes_pinned_swe_rex_runtime():
    cfg = load_config(ROOT / "configs" / "m3_swe_agent_deepseek_v7.yaml")
    assert cfg["config_id"].endswith("thinking-v7")
    assert cfg["model_params"]["completion_kwargs"]["timeout"] == 180
    assert cfg["model_params"]["retry"]["retries"] == 1
    assert cfg["local_image_tag"] == "traceverdict/self-base:swe-agent-1.1.0-v3"
    assert cfg["suite_dockerfile"] == "SWEAgentV3.Dockerfile"
    dockerfile = (
        ROOT / "tasks" / "self" / "_image" / cfg["suite_dockerfile"]
    ).read_text("utf-8")
    assert "FROM traceverdict/self-base:swe-agent-1.1.0-v2" in dockerfile
    assert "swe-rex==1.2.1" in dockerfile


def test_config_id_is_immutable(tmp_path):
    cfg = load_config(ROOT / "configs" / "dev.yaml")
    conn = init_db(tmp_path / "traceverdict.db")
    try:
        _upsert_config(conn, cfg)
        _upsert_config(conn, cfg)
        changed = dict(cfg)
        changed["model_name"] = "openai/not-the-same-model"
        with pytest.raises(ValueError, match="immutable config_id collision"):
            _upsert_config(conn, changed)
    finally:
        conn.close()


def test_kimi_probe_config_has_frozen_pricing_identity():
    cfg = load_config(ROOT / "configs" / "kimi_k2_5_probe.yaml")
    assert cfg["config_id"] == "probe-kimi-k2-5-thinking-v1"
    assert cfg["model_name"] == "openai/kimi-k2.5"
    assert cfg["model_params"] == {"thinking": {"type": "enabled"}}

    registry_path = cfg["litellm_model_registry"]
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    price = registry[cfg["model_name"]]
    assert price["max_tokens"] == 262144
    assert price["cache_read_input_token_cost"] == 1e-7
    assert price["input_cost_per_token"] == 6e-7
    assert price["output_cost_per_token"] == 3e-6

    meta = json.loads(
        (ROOT / "configs" / "litellm_models_kimi.meta.json").read_text(
            encoding="utf-8"
        )
    )
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == meta[
        "registry_sha256"
    ]


def test_qwen_probe_config_has_frozen_pricing_identity():
    cfg = load_config(ROOT / "configs" / "qwen3_6_flash_probe.yaml")
    assert cfg["config_id"] == "probe-qwen3-6-flash-thinking-v1"
    assert cfg["model_name"] == "openai/qwen3.6-flash"
    assert cfg["model_params"] == {"enable_thinking": True}

    registry_path = cfg["litellm_model_registry"]
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    price = registry[cfg["model_name"]]
    assert price["max_tokens"] == 262144
    assert price["cache_read_input_token_cost"] == pytest.approx(
        4.2512107241098756e-8
    )
    assert price["input_cost_per_token"] == pytest.approx(
        2.1256053620549378e-7
    )
    assert price["output_cost_per_token"] == pytest.approx(
        1.2753632172329627e-6
    )

    meta = json.loads(
        (ROOT / "configs" / "litellm_models_qwen.meta.json").read_text(
            encoding="utf-8"
        )
    )
    assert meta["registry_pricing_tier_max_input_tokens"] == 262144
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == meta[
        "registry_sha256"
    ]


def test_qwen_v2_prevents_reasoning_subset_double_charge():
    cfg = load_config(ROOT / "configs" / "qwen3_6_flash_probe_v2.yaml")
    assert cfg["config_id"] == "probe-qwen3-6-flash-thinking-v2"
    assert cfg["model_name"] == "openai/qwen3.6-flash"
    assert cfg["model_params"] == {"enable_thinking": True}

    registry_path = cfg["litellm_model_registry"]
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    price = registry[cfg["model_name"]]
    assert price["output_cost_per_reasoning_token"] == 0.0
    assert price["output_cost_per_token"] == pytest.approx(
        1.2753632172329627e-6
    )

    meta = json.loads(
        (ROOT / "configs" / "litellm_models_qwen_v2.meta.json").read_text(
            encoding="utf-8"
        )
    )
    assert "already includes" in meta["usage_semantics"]
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == meta[
        "registry_sha256"
    ]

    import litellm
    from litellm.types.utils import (
        Choices,
        Message,
        ModelResponse,
        Usage,
    )

    litellm.utils.register_model(registry)
    response = ModelResponse(
        model="qwen3.6-flash",
        choices=[
            Choices(index=0, message=Message(role="assistant", content="ok"))
        ],
        usage=Usage(
            prompt_tokens=477,
            completion_tokens=150,
            total_tokens=627,
            completion_tokens_details={
                "reasoning_tokens": 104,
                "text_tokens": 150,
            },
        ),
    )
    actual = litellm.cost_calculator.completion_cost(
        response, model=cfg["model_name"]
    )
    expected = (
        477 * price["input_cost_per_token"]
        + 150 * price["output_cost_per_token"]
    )
    assert actual == pytest.approx(expected, abs=1e-12)
