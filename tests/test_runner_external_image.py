from pathlib import Path
from unittest.mock import patch

from traceverdict.core.runner import run_task

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_external_image_is_never_retagged_from_config(tmp_path: Path):
    task_dir = tmp_path / "external"
    task_dir.mkdir()
    (task_dir / "repo.bundle").write_bytes(b"not reached")
    (task_dir / "task.yaml").write_text(
        """id: external-1
suite: external
source: swebench_verified
repo_ref: repo.bundle
base_commit: 0000000000000000000000000000000000000000
image_ref: swebench/example:latest
instruction: test
budget:
  max_steps: 1
  max_tokens: 1
  max_wall_s: 1
  max_cost_usd: 1
forbidden_paths: []
gt:
  type: swebench
  spec: {}
tags: []
""",
        encoding="utf-8",
    )
    with (
        patch("traceverdict.core.runner.require_docker", return_value="docker"),
        patch("traceverdict.core.runner.ensure_suite_image", return_value=None),
        patch("traceverdict.core.runner.image_digest", side_effect=RuntimeError("missing external")),
        patch("traceverdict.core.runner.ensure_local_image") as fallback,
        patch("traceverdict.core.runner.subprocess.run") as subprocess_run,
    ):
        result = run_task(
            task_dir,
            ROOT / "configs" / "dev.yaml",
            db_path=tmp_path / "db.sqlite",
            artifacts_dir=tmp_path / "artifacts",
        )
    assert result["status"] == "harness_error"
    assert "missing external" in result["error"]
    fallback.assert_not_called()
    subprocess_run.assert_not_called()
