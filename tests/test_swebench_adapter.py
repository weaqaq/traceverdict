import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from traceverdict.swebench_adapter import (
    MAX_WORKERS,
    assert_three_way_agreement,
    read_official_outcome,
)
import traceverdict.swebench_adapter as adapter
from traceverdict.tracer import db as dbmod


def test_concurrency_is_hard_capped_at_two():
    assert MAX_WORKERS == 2


def test_official_raw_and_aggregate_mapping(tmp_path: Path):
    raw = tmp_path / "report.json"
    aggregate = tmp_path / "aggregate.json"
    raw.write_text(json.dumps({"x-1": {"resolved": True}}), encoding="utf-8")
    aggregate.write_text(
        json.dumps({"resolved_ids": ["x-1"], "unresolved_ids": [], "error_ids": []}),
        encoding="utf-8",
    )
    outcome = read_official_outcome("x-1", raw, aggregate)
    assert outcome.raw_resolved is True
    assert outcome.aggregate_resolved is True
    assert outcome.aggregate_classification == "resolved"
    assert outcome.agreed


def test_empty_patch_without_raw_report_is_explicit_unresolved(tmp_path: Path):
    aggregate = tmp_path / "aggregate.json"
    aggregate.write_text(
        json.dumps(
            {
                "resolved_ids": [],
                "unresolved_ids": [],
                "error_ids": [],
                "empty_patch_ids": ["x-1"],
            }
        ),
        encoding="utf-8",
    )
    outcome = read_official_outcome("x-1", None, aggregate)
    assert outcome.raw_resolved is None
    assert outcome.aggregate_resolved is False
    assert outcome.aggregate_classification == "empty_patch"
    assert outcome.agreed
    assert_three_way_agreement(
        traceverdict_passed=False, raw_resolved=None, aggregate_resolved=False
    )


def test_missing_raw_report_is_rejected_for_non_empty_classification(tmp_path: Path):
    aggregate = tmp_path / "aggregate.json"
    aggregate.write_text(
        json.dumps(
            {
                "resolved_ids": [],
                "unresolved_ids": ["x-1"],
                "error_ids": [],
                "empty_patch_ids": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing for non-empty classification"):
        read_official_outcome("x-1", None, aggregate)


def test_aggregate_must_classify_exactly_once(tmp_path: Path):
    raw = tmp_path / "report.json"
    aggregate = tmp_path / "aggregate.json"
    raw.write_text(json.dumps({"x-1": {"resolved": False}}), encoding="utf-8")
    aggregate.write_text(
        json.dumps(
            {"resolved_ids": [], "unresolved_ids": ["x-1"], "error_ids": ["x-1"]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="classifies x-1 2 times"):
        read_official_outcome("x-1", raw, aggregate)


def test_three_way_disagreement_is_a_stop_condition():
    with pytest.raises(RuntimeError, match="pilot stop"):
        assert_three_way_agreement(
            traceverdict_passed=True, raw_resolved=False, aggregate_resolved=False
        )


def test_official_evaluation_uses_explicit_config_identity(monkeypatch, tmp_path):
    model_identity = "traceverdict__probe-qwen3-6-flash-thinking-v2"
    run_id = "t5-run-1"
    instance_id = "x-1"

    def fake_run(_command, *, cwd, **_kwargs):
        raw = (
            Path(cwd)
            / "logs"
            / "run_evaluation"
            / run_id
            / model_identity
            / instance_id
            / "report.json"
        )
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({instance_id: {"resolved": True}}), encoding="utf-8")
        aggregate = Path(cwd) / f"{model_identity}.{run_id}.json"
        aggregate.write_text(
            json.dumps(
                {
                    "resolved_ids": [instance_id],
                    "unresolved_ids": [],
                    "error_ids": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(adapter, "_run", fake_run)
    raw, aggregate = adapter.run_official_evaluation(
        python_executable="python",
        instance_id=instance_id,
        patch_text="diff --git a/x b/x\n",
        output_dir=tmp_path,
        official_run_id=run_id,
        model_name_or_path=model_identity,
        image_path="build",
    )
    prediction = json.loads((tmp_path / "predictions.json").read_text())[0]
    assert prediction["model_name_or_path"] == model_identity
    assert raw is not None and model_identity in str(raw)
    assert aggregate.name == f"{model_identity}.{run_id}.json"


def test_image_pull_falls_back_to_official_builder(monkeypatch):
    calls = []

    def fake_runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1 if command[:2] == ["docker", "pull"] else 0,
            stdout="",
            stderr="pull failed",
        )

    monkeypatch.setattr(
        adapter,
        "test_spec",
        lambda _instance, namespace: SimpleNamespace(
            instance_image_key=(
                "swebench/remote:latest" if namespace else "local:latest"
            )
        ),
    )
    # Force the initial local-cache inspection to miss, then let the completed
    # official build inspection succeed.
    digests = iter([RuntimeError("missing"), "sha256:abc"])

    def fake_digest(*_args, **_kwargs):
        value = next(digests)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(adapter, "image_digest", fake_digest)
    result = adapter.acquire_official_image(
        {"instance_id": "x-1"}, python_executable="python", runner=fake_runner
    )
    assert result.path == "build"
    assert result.image_ref == "local:latest"
    assert any("swebench.harness.prepare_images" in command for command in calls)


def test_official_pull_timeout_falls_back_to_builder(monkeypatch):
    calls = []

    def fake_runner(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            raise adapter.subprocess.TimeoutExpired(command, adapter.PULL_TIMEOUT_S)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        adapter,
        "test_spec",
        lambda _instance, namespace: SimpleNamespace(
            instance_image_key=(
                "swebench/remote:latest" if namespace else "local:latest"
            )
        ),
    )
    digests = iter([RuntimeError("missing"), "sha256:built"])

    def fake_digest(*_args, **_kwargs):
        value = next(digests)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(adapter, "image_digest", fake_digest)
    result = adapter.acquire_official_image(
        {"instance_id": "x-1"}, python_executable="python", runner=fake_runner
    )
    assert result.path == "build"
    assert result.digest == "sha256:built"
    assert any("swebench.harness.prepare_images" in command for command in calls)


def test_official_builder_retries_cli_zero_when_image_is_missing(monkeypatch):
    calls = []

    def fake_runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1 if command[:2] == ["docker", "pull"] else 0,
            stdout="",
            stderr="pull failed",
        )

    monkeypatch.setattr(
        adapter,
        "test_spec",
        lambda _instance, namespace: SimpleNamespace(
            instance_image_key=(
                "swebench/remote:latest" if namespace else "local:latest"
            )
        ),
    )
    digests = iter(
        [
            RuntimeError("initial cache miss"),
            RuntimeError("CLI returned zero but image is absent"),
            "sha256:retry-built",
        ]
    )

    def fake_digest(*_args, **_kwargs):
        value = next(digests)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(adapter, "image_digest", fake_digest)
    result = adapter.acquire_official_image(
        {"instance_id": "x-1"}, python_executable="python", runner=fake_runner
    )

    prepare_calls = [
        command for command in calls if "swebench.harness.prepare_images" in command
    ]
    assert len(prepare_calls) == 2
    assert result.digest == "sha256:retry-built"


def test_local_built_image_digest_falls_back_to_image_id(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda _command: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"RepoDigests": [], "Id": "sha256:local-image"}),
            stderr="",
        ),
    )
    assert adapter.image_digest("local:latest") == "sha256:local-image"


def test_official_checkout_is_reset_to_frozen_base(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    (repo / "value.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (repo / "value.txt").write_text("image build", encoding="utf-8")
    (repo / "generated.tmp").write_text("remove", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "image"], check=True)
    adapter._reset_checkout_to_base(repo, base)
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        == base
    )
    assert (repo / "value.txt").read_text(encoding="utf-8") == "base"
    assert not (repo / "generated.tmp").exists()


def test_materialized_bundle_target_is_absolute(monkeypatch, tmp_path: Path):
    commands = []
    base = "a" * 40

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["docker", "create"]:
            return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=base + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter, "_run", fake_run)
    monkeypatch.setattr(adapter, "_reset_checkout_to_base", lambda *_args: None)
    monkeypatch.setattr(adapter.shutil, "rmtree", lambda *_args, **_kwargs: None)
    instance = {
        "instance_id": "x-1",
        "base_commit": base,
        "problem_statement": "fix x",
        "FAIL_TO_PASS": "[]",
        "PASS_TO_PASS": "[]",
    }
    relative = Path("relative-task")
    monkeypatch.chdir(tmp_path)
    result = adapter.materialize_task(instance, image_ref="image", output_dir=relative)
    bundle_command = next(command for command in commands if "bundle" in command)
    assert Path(bundle_command[-2]).is_absolute()
    assert result == (tmp_path / relative).resolve()


def test_official_artifacts_and_verdict_are_idempotent(tmp_path: Path):
    conn = dbmod.init_db(tmp_path / "db.sqlite")
    dbmod.insert_task(
        conn,
        {
            "task_id": "x-1",
            "suite": "x",
            "source": "swebench_verified",
            "repo_ref": "bundle",
            "base_commit": "0" * 40,
            "image_ref": "image",
            "instruction": "x",
            "budget_json": "{}",
            "forbidden_json": "[]",
            "gt_type": "swebench",
            "gt_spec_json": "{}",
            "tags_json": "[]",
            "created_at": "now",
        },
    )
    dbmod.insert_config(
        conn,
        {
            "config_id": "c",
            "agent_name": "mini-swe-agent",
            "agent_version": "2.4.5",
            "model_name": "m",
            "model_params_json": "{}",
            "prompt_version": "v0",
            "harness_version": "0.1.0",
            "notes": None,
        },
    )
    dbmod.insert_run(
        conn,
        {
            "run_id": "r",
            "task_id": "x-1",
            "config_id": "c",
            "repetition_idx": 0,
            "mode": "scenario",
            "status": "ok",
            "exit_reason": "Submitted",
            "started_at": "now",
            "finished_at": "now",
            "wall_time_s": 1.0,
            "tokens_in": 1,
            "tokens_out": 1,
            "cost_usd": 0.1,
            "seed": None,
            "env_fingerprint": "f",
        },
    )
    raw = tmp_path / "report.json"
    aggregate = tmp_path / "aggregate.json"
    raw.write_text(json.dumps({"x-1": {"resolved": True}}), encoding="utf-8")
    aggregate.write_text(
        json.dumps({"resolved_ids": ["x-1"], "unresolved_ids": [], "error_ids": []}),
        encoding="utf-8",
    )
    for _ in range(2):
        adapter.record_official_verdict(
            conn,
            run_id="r",
            instance_id="x-1",
            raw_report_path=raw,
            aggregate_report_path=aggregate,
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM artifact WHERE run_id='r'").fetchone()[0]
        == 2
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM verdict WHERE run_id='r'").fetchone()[0] == 1
    )
    conn.close()
