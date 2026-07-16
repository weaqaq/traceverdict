"""Docker gate: require_docker fails clearly when CLI missing (D1-d)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from traceverdict.snapshot.image import DockerUnavailableError, require_docker


def test_require_docker_missing_cli():
    with patch("traceverdict.snapshot.image.shutil.which", return_value=None):
        with pytest.raises(DockerUnavailableError) as ei:
            require_docker()
        assert "Docker CLI not found" in str(ei.value)
