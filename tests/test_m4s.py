from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from traceverdict.core.config_loader import load_config
from traceverdict.m4s import (
    CONFIG_ID,
    POSITIONING,
    PROJECTED_CEILING_USD,
    assert_kimi3_stable_identity,
    choose_formal_count,
    cost_projection,
    load_frozen_task_set,
)

ROOT = Path(__file__).resolve().parents[1]


def test_m4s_config_and_official_price_identity():
    cfg = load_config(ROOT / "configs" / "m4s_deepseek_v4_pro_v1.yaml")
    assert cfg["config_id"] == CONFIG_ID
    assert cfg["model_name"] == "openai/deepseek-v4-pro"
    assert cfg["model_params"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort intentionally omitted" in cfg["notes"]
    registry = json.loads(cfg["litellm_model_registry"].read_text("utf-8"))
    price = registry[cfg["model_name"]]
    assert price["cache_read_input_token_cost"] == 3.625e-9
    assert price["input_cost_per_token"] == 4.35e-7
    assert price["output_cost_per_token"] == 8.7e-7
    meta = json.loads((ROOT / "configs" / "litellm_models.meta.json").read_text("utf-8"))
    assert meta["verified_at"] == "2026-07-16"
    assert hashlib.sha256(cfg["litellm_model_registry"].read_bytes()).hexdigest() == meta[
        "registry_sha256"
    ]


def test_m4s_19_run_envelope_and_reuse_projection():
    value = cost_projection(
        historical_actual_usd=Decimal("1"),
        probe_costs_usd=[Decimal("0.1"), Decimal("0.2"), Decimal("0.3")],
        formal_count=16,
    )
    assert value["authorization_run_envelope"] == 19
    assert value["actual_unique_runs_with_reuse"] == 16
    assert Decimal(value["conservative_projected_total_usd"]) == Decimal("6.4")
    assert Decimal(value["reuse_projected_total_usd"]) == Decimal("5.5")
    assert value["approved"] is True


def test_m4s_cut_order_is_16_then_first12_then_abandon():
    count, value = choose_formal_count(
        historical_actual_usd=Decimal("20"),
        probe_costs_usd=[Decimal("0.4"), Decimal("0.4"), Decimal("0.4")],
    )
    assert count == 0
    assert value["decision"] == "abandon"
    assert PROJECTED_CEILING_USD == Decimal("25")

    count, value = choose_formal_count(
        historical_actual_usd=Decimal("20"),
        probe_costs_usd=[Decimal("0.2"), Decimal("0.2"), Decimal("0.28")],
    )
    assert count == 12
    assert value["decision"] == "reduced-first-12"


def test_first12_is_literal_prefix_and_contains_all_probes():
    full = load_frozen_task_set(
        ROOT / "benchmarks" / "swebv_subset_v1.txt", expected_count=16
    )
    reduced = load_frozen_task_set(
        ROOT / "benchmarks" / "swebv_subset_v1_first12.txt", expected_count=12
    )
    assert reduced == full[:12]
    assert {"pytest-dev__pytest-7982", "sympy__sympy-20438", "pydata__xarray-7229"} <= set(
        reduced
    )


def test_kimi3_dormant_identity_rejects_preview_and_unofficial_ids():
    with pytest.raises(RuntimeError, match="preview/rolling"):
        assert_kimi3_stable_identity("kimi-3-preview", official_ids={"kimi-3-preview"})
    with pytest.raises(RuntimeError, match="official model list"):
        assert_kimi3_stable_identity("kimi-3", official_ids=set())
    assert_kimi3_stable_identity("kimi-3", official_ids={"kimi-3"}) is None
    assert POSITIONING == "同厂跨档对比，回答 harness 结论是否随被试能力档位稳健。"
