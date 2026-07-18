from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

from traceverdict.core.simple_yaml import dumps
from traceverdict.swebench_budget import (
    SWEBV_BUDGET_SEMANTICS,
    assert_frozen_budget_bytes,
    frozen_budget_block_bytes,
    frozen_budget_block_sha256,
)


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_swebv_pilot.py"
SPEC = spec_from_file_location("run_swebv_pilot", SCRIPT)
pilot = module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pilot
SPEC.loader.exec_module(pilot)


def test_104_and_152_cost_arithmetic_uses_conservative_probe_max():
    result = pilot.cost_projection(
        probe_costs=[0.1, 0.2, 0.3], pilot_actual=0.8, historical_actual=0.2
    )
    assert result["unique_runs"] == 104
    assert result["gross_runs"] == 152
    assert result["unique_projected_cumulative_usd"] == 1.0 + 99 * 0.3
    assert result["gross_projected_cumulative_usd"] == 1.0 + 147 * 0.3
    assert result["unique_within_tripwire"] is False


def test_docker_desktop_storage_fallback_parses_machine_readable_sizes(
    monkeypatch, tmp_path: Path
):
    rows = "\n".join(
        [
            '{"Type":"Images","Size":"1.114GB"}',
            '{"Type":"Containers","Size":"0B"}',
            '{"Type":"Local Volumes","Size":"12.5MB"}',
            '{"Type":"Build Cache","Size":"2.773GB"}',
        ]
    )
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=rows, stderr=""
        ),
    )
    missing = tmp_path / "linux-docker-root-not-mounted-on-windows"
    assert pilot._docker_storage_used(missing) == 3_899_500_000


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0B", 0), ("1.5KB", 1500), ("243.9MB", 243_900_000), ("2GB", 2_000_000_000)],
)
def test_parse_docker_size(value: str, expected: int):
    assert pilot._parse_docker_size(value) == expected


def test_script_never_reads_dotenv_or_prints_secret_values():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "dotenv" not in source.casefold()
    assert "OPENAI_API_KEY" in source
    assert "XIAOMI_MIMO_API_KEY" in source
    assert "os.environ.get" in source
    assert "os.environ[" not in source


def test_provider_credentials_are_explicit_and_mimo_needs_no_custom_base(
    monkeypatch,
):
    assert pilot._required_model_env(
        {"model_name": "xiaomi_mimo/mimo-v2.5"}
    ) == ("XIAOMI_MIMO_API_KEY",)
    assert pilot._required_model_env(
        {"model_name": "openai/deepseek-v4-flash"}
    ) == ("OPENAI_API_KEY", "OPENAI_API_BASE")
    monkeypatch.setenv("XIAOMI_MIMO_API_KEY", "redacted-test-value")
    pilot._require_credentials({"model_name": "xiaomi_mimo/mimo-v2.5"})


def test_agent_container_guard_removes_only_containers_created_by_run(monkeypatch):
    monkeypatch.setattr(pilot, "_container_ids", lambda: {"old", "new-agent"})
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    assert pilot._cleanup_new_containers({"old"}) == ["new-agent"]
    assert calls == [["docker", "rm", "-f", "new-agent"]]


def test_selected_ids_keep_pilot_prefix_and_expand_to_frozen_16(tmp_path):
    task_set = tmp_path / "task_set.txt"
    ids = [f"repo__task-{index:02d}" for index in range(16)]
    task_set.write_text("\n".join(ids) + "\n", encoding="utf-8")

    assert pilot._selected_ids(SimpleNamespace(task_set=task_set, all=False)) == ids[:5]
    assert pilot._selected_ids(SimpleNamespace(task_set=task_set, all=True)) == ids
    assert pilot._selected_ids(
        SimpleNamespace(task_set=task_set, all=False, instance_id=ids[9])
    ) == [ids[9]]
    with pytest.raises(ValueError, match="not in frozen task set"):
        pilot._selected_ids(
            SimpleNamespace(task_set=task_set, all=False, instance_id="missing")
        )


def test_run_all_stops_before_rebuying_incomplete_paid_run(tmp_path, monkeypatch):
    task_set = tmp_path / "task_set.txt"
    ids = [f"repo__task-{index:02d}" for index in range(16)]
    task_set.write_text("\n".join(ids) + "\n", encoding="utf-8")
    output = tmp_path / "reports"
    output.mkdir()
    (output / "image_records.json").write_text(
        json.dumps([{"instance_id": instance_id} for instance_id in ids]),
        encoding="utf-8",
    )
    monkeypatch.setattr(pilot, "_require_credentials", lambda _config: None)
    monkeypatch.setattr(pilot, "_assert_budget_identity", lambda **_kwargs: {})
    monkeypatch.setattr(
        pilot, "load_config", lambda _path: {"config_id": "deepseek-v4-flash-v2"}
    )
    monkeypatch.setattr(
        pilot,
        "_existing_config_runs",
        lambda *_args, **_kwargs: [{"run_id": "paid-but-unjudged"}],
    )
    monkeypatch.setattr(
        pilot,
        "run_one",
        lambda _args: pytest.fail("paid run must not be repeated"),
    )

    with pytest.raises(RuntimeError, match="paid-run guard"):
        pilot.run_all(
            SimpleNamespace(
                task_set=task_set,
                config=tmp_path / "config.yaml",
                db=tmp_path / "traceverdict.db",
                output=output,
            )
        )


def _write_sweb_task(path: Path, *, max_tokens: int = 250000) -> None:
    path.mkdir(parents=True)
    (path / "task.yaml").write_bytes(
        dumps(
            {
                "id": path.name,
                "suite": "swebv_subset_v1",
                "source": "swebench_verified",
                "repo_ref": "repo.bundle",
                "base_commit": "a" * 40,
                "image_ref": "sweb.eval:latest",
                "instruction": "fix it",
                "budget": {
                    "max_steps": 100,
                    "max_tokens": max_tokens,
                    "max_wall_s": 3600,
                    "max_cost_usd": 1.0,
                },
                "forbidden_paths": [],
                "gt": {"type": "swebench", "spec": {}},
                "tags": ["swebench_verified"],
            }
        ).encode("utf-8")
    )


def _guard_config(tmp_path: Path) -> dict:
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    return {
        "config_id": "dev-deepseek-v4-flash-v2",
        "container_cwd": "/testbed",
        "model_name": "openai/deepseek-v4-flash",
        "model_params": {"thinking": {"type": "enabled"}},
        "litellm_model_registry": registry,
    }


def test_budget_identity_guard_checks_bytes_and_generated_limits(tmp_path):
    output = tmp_path / "reports"
    task_id = "repo__task-1"
    _write_sweb_task(output / "tasks" / task_id)
    evidence = pilot._assert_budget_identity(
        ids=[task_id], config=_guard_config(tmp_path), output=output
    )
    assert evidence["budget_block_sha256"] == frozen_budget_block_sha256()
    assert evidence["records"][0]["mini_agent_limits"] == {
        "step_limit": 100,
        "cost_limit": 1.0,
        "wall_time_limit_seconds": 3600,
    }
    assert evidence["records"][0]["max_tokens_enforcement"] == "none"
    assert SWEBV_BUDGET_SEMANTICS["max_tokens"]["status"] == "recorded-inert"
    assert all(
        item["status"] == "enforced"
        for name, item in SWEBV_BUDGET_SEMANTICS.items()
        if name != "max_tokens"
    )


def test_budget_identity_guard_rejects_declared_token_drift(tmp_path):
    output = tmp_path / "reports"
    task_id = "repo__task-1"
    _write_sweb_task(output / "tasks" / task_id, max_tokens=250001)
    with pytest.raises(RuntimeError, match="budget identity drift"):
        pilot._assert_budget_identity(
            ids=[task_id], config=_guard_config(tmp_path), output=output
        )


def test_budget_block_is_exact_bytes_not_line_ending_normalized():
    assert frozen_budget_block_sha256() == (
        "3a1051a3fa1b75d9dc5f1231820abac17775c8c9e164637104ca1ecdf64edde9"
    )
    assert_frozen_budget_bytes(frozen_budget_block_bytes())
    with pytest.raises(RuntimeError, match="budget identity drift"):
        assert_frozen_budget_bytes(frozen_budget_block_bytes().replace(b"\n", b"\r\n"))
