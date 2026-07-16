from pathlib import Path
from unittest.mock import patch

from traceverdict.core.runner import run_task
from traceverdict.snapshot.image import DockerUnavailableError
from traceverdict.tracer.db import _connect

ROOT = Path(__file__).resolve().parents[1]


def test_docker_failure_records_wall_time(tmp_path: Path):
    db = tmp_path / "traceverdict.db"
    with patch("traceverdict.core.runner.require_docker", side_effect=DockerUnavailableError("no docker")):
        result = run_task(
            ROOT / "tasks" / "self" / "S1",
            ROOT / "configs" / "dev.yaml",
            db_path=db,
            artifacts_dir=tmp_path / "artifacts",
        )
    assert result["status"] == "harness_error"
    conn = _connect(db)
    try:
        row = conn.execute("SELECT wall_time_s FROM run WHERE run_id=?", (result["run_id"],)).fetchone()
        assert row["wall_time_s"] is not None
        assert row["wall_time_s"] >= 0
    finally:
        conn.close()
