"""SWE-bench Verified materialization and official-harness result mapping.

The official ``swebench==4.1.0`` package remains the ground-truth judge.  This
module only prepares TraceVerdict's disposable workspace and maps the two official
report surfaces into the frozen verdict schema.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from traceverdict.core.simple_yaml import dump_to_path
from traceverdict.swebench_budget import SWEBV_TASK_BUDGET
from traceverdict.tracer.db import upsert_artifact, upsert_verdict
from traceverdict.verifier import RULE_RUBRIC_VERSION, verdict_id

SWEBENCH_VERSION = "4.1.0"
DATASET = "princeton-nlp/SWE-bench_Verified"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
MAX_WORKERS = 2
PULL_TIMEOUT_S = 120


@dataclass(frozen=True)
class ImageAcquisition:
    image_ref: str
    path: str
    digest: str
    elapsed_s: float


@dataclass(frozen=True)
class OfficialOutcome:
    instance_id: str
    raw_resolved: bool | None
    aggregate_resolved: bool
    aggregate_classification: str

    @property
    def agreed(self) -> bool:
        if self.raw_resolved is None:
            return (
                self.aggregate_classification == "empty_patch"
                and not self.aggregate_resolved
            )
        return self.raw_resolved == self.aggregate_resolved


def assert_swebench_version() -> None:
    actual = importlib.metadata.version("swebench")
    if actual != SWEBENCH_VERSION:
        raise RuntimeError(
            f"swebench version drift: expected {SWEBENCH_VERSION}, got {actual}"
        )


def load_verified_instances(
    instance_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    assert_swebench_version()
    from datasets import load_dataset

    rows = [
        dict(row)
        for row in load_dataset(DATASET, split="test", revision=DATASET_REVISION)
    ]
    if len(rows) != 500 or len({row["instance_id"] for row in rows}) != 500:
        raise RuntimeError("Verified dataset revision drift: expected 500 unique rows")
    if instance_ids is None:
        return rows
    by_id = {row["instance_id"]: row for row in rows}
    missing = sorted(set(instance_ids) - set(by_id))
    if missing:
        raise ValueError(f"instances absent from frozen dataset revision: {missing}")
    return [by_id[instance_id] for instance_id in instance_ids]


def test_spec(instance: dict[str, Any], *, namespace: str | None):
    assert_swebench_version()
    from swebench.harness.test_spec.test_spec import make_test_spec

    return make_test_spec(
        instance,
        namespace=namespace,
        base_image_tag="latest",
        env_image_tag="latest",
        instance_image_tag="latest",
        arch="x86_64",
    )


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def image_digest(image_ref: str, *, docker_executable: str = "docker") -> str:
    proc = _run(
        [docker_executable, "image", "inspect", image_ref, "--format", "{{json .}}"]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cannot inspect image {image_ref}: {proc.stderr[-1000:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid docker inspect JSON for {image_ref}") from exc
    repo_digests = data.get("RepoDigests") or []
    if repo_digests and "@sha256:" in repo_digests[0]:
        return repo_digests[0].split("@", 1)[1]
    image_id = data.get("Id") or ""
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"image {image_ref} has no usable digest")
    return image_id


def acquire_official_image(
    instance: dict[str, Any],
    *,
    python_executable: str,
    docker_executable: str = "docker",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ImageAcquisition:
    """Pull the official image, falling back to official ``prepare_images``."""
    started = time.perf_counter()
    remote_ref = test_spec(instance, namespace="swebench").instance_image_key
    local_ref = test_spec(instance, namespace=None).instance_image_key
    # A previous official prepare_images build is reusable and already proves
    # the pull->build fallback path.  Inspect it before retrying a known-bad
    # Docker Hub route on resumed probes.
    try:
        local_digest = image_digest(local_ref, docker_executable=docker_executable)
    except RuntimeError:
        local_digest = None
    if local_digest:
        return ImageAcquisition(
            local_ref, "build", local_digest, time.perf_counter() - started
        )
    try:
        pull = _run(
            [docker_executable, "pull", remote_ref],
            runner=runner,
            timeout=PULL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        pull = subprocess.CompletedProcess(
            exc.cmd, 124, stdout=exc.stdout or "", stderr="official pull timed out"
        )
    if pull.returncode == 0:
        return ImageAcquisition(
            remote_ref,
            "pull",
            image_digest(remote_ref, docker_executable=docker_executable),
            time.perf_counter() - started,
        )

    prepare_command = [
        python_executable,
        "-m",
        "swebench.harness.prepare_images",
        "--dataset_name",
        DATASET,
        "--split",
        "test",
        "--instance_ids",
        instance["instance_id"],
        "--max_workers",
        "1",
        "--tag",
        "latest",
        "--env_image_tag",
        "latest",
    ]
    failures = []
    # SWE-bench 4.1.0 prints per-instance build failures but its CLI main does
    # not propagate them as a non-zero process exit.  The image itself is the
    # authority.  Retry one silent failure to cover transient package/network
    # errors, then fail closed rather than entering a paid run without an image.
    for attempt in range(2):
        prepare = _run(
            prepare_command,
            runner=runner,
            timeout=4 * 60 * 60,
        )
        if prepare.returncode != 0:
            failures.append(
                f"attempt {attempt + 1} exit={prepare.returncode} "
                f"stderr={prepare.stderr[-3000:]!r}"
            )
            continue
        try:
            digest = image_digest(local_ref, docker_executable=docker_executable)
        except RuntimeError as exc:
            failures.append(f"attempt {attempt + 1} silent failure: {exc}")
            continue
        return ImageAcquisition(
            local_ref, "build", digest, time.perf_counter() - started
        )
    raise RuntimeError(
        "official image pull and build both failed; "
        f"pull={pull.stderr[-1500:]!r} builds={failures!r}"
    )


def materialize_task(
    instance: dict[str, Any],
    *,
    image_ref: str,
    output_dir: Path,
    docker_executable: str = "docker",
) -> Path:
    """Extract the official image's frozen checkout and create a TraceVerdict task."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="traceverdict-sweb-task-"))
    container_id = ""
    try:
        created = _run([docker_executable, "create", image_ref, "bash", "-lc", "true"])
        if created.returncode != 0:
            raise RuntimeError(f"docker create failed: {created.stderr[-2000:]}")
        container_id = created.stdout.strip()
        checkout = temp_root / "checkout"
        checkout.mkdir()
        copied = _run(
            [docker_executable, "cp", f"{container_id}:/testbed/.", str(checkout)]
        )
        if copied.returncode != 0:
            raise RuntimeError(f"docker cp /testbed failed: {copied.stderr[-2000:]}")
        _reset_checkout_to_base(checkout, instance["base_commit"])
        head = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
        if head.returncode != 0 or head.stdout.strip() != instance["base_commit"]:
            raise RuntimeError(
                f"official image checkout mismatch: expected {instance['base_commit']}, got {head.stdout.strip()}"
            )
        bundle = output_dir / "repo.bundle"
        bundled = _run(
            ["git", "-C", str(checkout), "bundle", "create", str(bundle), "HEAD"]
        )
        if bundled.returncode != 0:
            raise RuntimeError(f"git bundle failed: {bundled.stderr[-2000:]}")
    finally:
        if container_id:
            _run([docker_executable, "rm", "-f", container_id])
        shutil.rmtree(temp_root, ignore_errors=True)

    task = {
        "id": instance["instance_id"],
        "suite": "swebv_subset_v1",
        "source": "swebench_verified",
        "repo_ref": "repo.bundle",
        "base_commit": instance["base_commit"],
        "image_ref": image_ref,
        "instruction": instance["problem_statement"],
        "budget": dict(SWEBV_TASK_BUDGET),
        "forbidden_paths": [],
        "gt": {
            "type": "swebench",
            "spec": {
                "dataset": DATASET,
                "dataset_revision": DATASET_REVISION,
                "instance_id": instance["instance_id"],
                "fail_to_pass": (
                    json.loads(instance["FAIL_TO_PASS"])
                    if isinstance(instance["FAIL_TO_PASS"], str)
                    else instance["FAIL_TO_PASS"]
                ),
                "pass_to_pass": (
                    json.loads(instance["PASS_TO_PASS"])
                    if isinstance(instance["PASS_TO_PASS"], str)
                    else instance["PASS_TO_PASS"]
                ),
            },
        },
        "tags": ["swebench_verified"],
    }
    dump_to_path(output_dir / "task.yaml", task)
    return output_dir


def _reset_checkout_to_base(checkout: Path, base_commit: str) -> None:
    """Match official eval semantics instead of assuming image HEAD is base."""
    exists = _run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{base_commit}^{{commit}}"]
    )
    if exists.returncode != 0:
        raise RuntimeError(f"official image lacks frozen base commit {base_commit}")
    reset = _run(["git", "-C", str(checkout), "reset", "--hard", base_commit])
    if reset.returncode != 0:
        raise RuntimeError(f"cannot reset official checkout: {reset.stderr[-2000:]}")
    clean = _run(["git", "-C", str(checkout), "clean", "-fdx"])
    if clean.returncode != 0:
        raise RuntimeError(f"cannot clean official checkout: {clean.stderr[-2000:]}")


def read_official_outcome(
    instance_id: str, raw_report_path: Path | None, aggregate_report_path: Path
) -> OfficialOutcome:
    aggregate = json.loads(aggregate_report_path.read_text(encoding="utf-8"))
    sets = {
        "resolved": set(aggregate.get("resolved_ids") or []),
        "unresolved": set(aggregate.get("unresolved_ids") or []),
        "error": set(aggregate.get("error_ids") or []),
        "empty_patch": set(aggregate.get("empty_patch_ids") or []),
    }
    memberships = [name for name, values in sets.items() if instance_id in values]
    if len(memberships) != 1:
        raise ValueError(
            f"aggregate report classifies {instance_id} {len(memberships)} times"
        )
    classification = memberships[0]
    aggregate_resolved = classification == "resolved"
    if raw_report_path is None:
        if classification != "empty_patch":
            raise ValueError(
                f"official raw report missing for non-empty classification {classification}"
            )
        raw_resolved = None
    else:
        raw = json.loads(raw_report_path.read_text(encoding="utf-8"))
        try:
            raw_resolved = raw[instance_id]["resolved"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"official raw report lacks {instance_id}.resolved"
            ) from exc
        if not isinstance(raw_resolved, bool):
            raise ValueError("official raw resolved must be boolean")
    return OfficialOutcome(
        instance_id, raw_resolved, aggregate_resolved, classification
    )


def record_official_verdict(
    conn,
    *,
    run_id: str,
    instance_id: str,
    raw_report_path: Path | None,
    aggregate_report_path: Path,
) -> OfficialOutcome:
    """Persist official reports and map their agreed result to one rule verdict."""
    outcome = read_official_outcome(instance_id, raw_report_path, aggregate_report_path)
    if not outcome.agreed:
        raise RuntimeError(
            f"official raw/aggregate disagreement for {instance_id}: "
            f"{outcome.raw_resolved} != {outcome.aggregate_resolved}"
        )
    artifacts = [("swebench_aggregate", aggregate_report_path)]
    if raw_report_path is not None:
        artifacts.insert(0, ("swebench_report", raw_report_path))
    for kind, path in artifacts:
        payload = path.read_bytes()
        upsert_artifact(
            conn,
            {
                "artifact_id": f"{run_id}-{kind}",
                "run_id": run_id,
                "kind": kind,
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
    upsert_verdict(
        conn,
        {
            "verdict_id": verdict_id(run_id, "rule", "swebench", RULE_RUBRIC_VERSION),
            "run_id": run_id,
            "track": "rule",
            "name": "swebench",
            "passed": int(outcome.aggregate_resolved),
            "score": None,
            "detail_json": json.dumps(
                {
                    "instance_id": instance_id,
                    "raw_resolved": outcome.raw_resolved,
                    "raw_report_present": raw_report_path is not None,
                    "aggregate_resolved": outcome.aggregate_resolved,
                    "aggregate_classification": outcome.aggregate_classification,
                    "dataset_revision": DATASET_REVISION,
                    "swebench_version": SWEBENCH_VERSION,
                },
                sort_keys=True,
            ),
            "judge_model": None,
            "rubric_version": RULE_RUBRIC_VERSION,
        },
    )
    return outcome


def assert_three_way_agreement(
    *, traceverdict_passed: bool, raw_resolved: bool | None, aggregate_resolved: bool
) -> None:
    if raw_resolved is None:
        if traceverdict_passed or aggregate_resolved:
            raise RuntimeError(
                "pilot stop: empty-patch official aggregate disagrees with TraceVerdict"
            )
        return
    if len({traceverdict_passed, raw_resolved, aggregate_resolved}) != 1:
        raise RuntimeError(
            "pilot stop: TraceVerdict verdict, official raw report, and official aggregate disagree"
        )


def run_official_evaluation(
    *,
    python_executable: str,
    instance_id: str,
    patch_text: str,
    output_dir: Path,
    official_run_id: str,
    model_name_or_path: str,
    image_path: str,
    timeout_s: int = 1800,
) -> tuple[Path | None, Path]:
    """Execute the official 4.1.0 harness for exactly one stored patch."""
    if image_path not in {"pull", "build"}:
        raise ValueError("image_path must be pull or build")
    output_dir.mkdir(parents=True, exist_ok=True)
    model_name = model_name_or_path.strip()
    if not model_name:
        raise ValueError("model_name_or_path must be non-empty")
    predictions = output_dir / "predictions.json"
    predictions.write_text(
        json.dumps(
            [
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_name,
                    "model_patch": patch_text,
                }
            ]
        ),
        encoding="utf-8",
    )
    namespace = "swebench" if image_path == "pull" else "none"
    proc = _run(
        [
            python_executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            DATASET,
            "--split",
            "test",
            "--instance_ids",
            instance_id,
            "--predictions_path",
            str(predictions.resolve()),
            "--max_workers",
            "1",
            "--run_id",
            official_run_id,
            "--namespace",
            namespace,
            "--timeout",
            str(timeout_s),
            "--cache_level",
            "instance",
            "--clean",
            "false",
        ],
        cwd=output_dir,
        timeout=timeout_s + 900,
    )
    (output_dir / "official.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (output_dir / "official.stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"official harness failed: {proc.stderr[-4000:]}")
    raw = (
        output_dir
        / "logs"
        / "run_evaluation"
        / official_run_id
        / model_name.replace("/", "__")
        / instance_id
        / "report.json"
    )
    aggregate = output_dir / f"{model_name.replace('/', '__')}.{official_run_id}.json"
    if not aggregate.is_file():
        raise FileNotFoundError(f"official aggregate report missing: {aggregate}")
    if raw.is_file():
        return raw, aggregate
    aggregate_payload = json.loads(aggregate.read_text(encoding="utf-8"))
    if instance_id in set(aggregate_payload.get("empty_patch_ids") or []):
        return None, aggregate
    raise FileNotFoundError(f"official raw report missing: {raw}")
