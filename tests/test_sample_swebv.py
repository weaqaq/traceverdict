from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "sample_swebv.py"
SPEC = spec_from_file_location("sample_swebv", SCRIPT)
sample = module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sample
SPEC.loader.exec_module(sample)


def _patch(files: int, size: int) -> str:
    headers = "".join(
        f"diff --git a/f{index}.py b/f{index}.py\n--- a/f{index}.py\n+++ b/f{index}.py\n"
        for index in range(files)
    )
    return headers + "+" + ("x" * size) + "\n"


def _rows():
    rows = []
    # Plenty of candidates in every emergent tercile/file cell and repo.
    for index in range(90):
        rows.append(
            {
                "instance_id": f"repo{index % 12}__pkg-{index:03d}",
                "repo": f"org/repo{index % 12}",
                "patch": _patch(1 if index % 2 == 0 else 2, 100 + index * 100),
            }
        )
    return rows


def test_patch_metrics_are_frozen():
    patch = "diff --git a/a.py b/a.py\n" + "diff --git a/a.py b/a.py\n" + "é"
    assert sample.patch_file_count(patch) == 1
    assert len(patch.encode("utf-8")) == len(patch) + 1


def test_membership_is_deterministic_and_ordering_only_permutes():
    candidates, _ = sample.make_candidates(_rows())
    members1 = sample.select_members(candidates)
    members2 = sample.select_members(candidates)
    assert members1 == members2
    ordered = sample.order_for_pilot(members1)
    assert set(ordered) == set(members1)
    assert len(ordered) == 16
    assert max(__import__("collections").Counter(c.repo for c in ordered).values()) <= 2
    assert {(c.size_band, c.file_band) for c in ordered}
    assert {c.size_band for c in ordered[:5]} == {"low", "mid", "high"}
    assert {c.file_band for c in ordered[:5]} == {"single", "multi"}
    assert len({c.repo for c in ordered[:5]}) == 5
    probes = sample.cost_probe_members(ordered)
    assert len(probes) == 3
    assert {c.size_band for c in probes} == {"low", "mid", "high"}
    assert {c.file_band for c in probes} == {"single", "multi"}


def test_exact_cell_quotas():
    candidates, _ = sample.make_candidates(_rows())
    members = sample.select_members(candidates)
    counts = __import__("collections").Counter((c.size_band, c.file_band) for c in members)
    assert dict(counts) == sample.QUOTAS


def test_freeze_records_unenforced_token_budget_semantics(tmp_path):
    metadata = sample.freeze(
        _rows(), tmp_path / "tasks.txt", tmp_path / "tasks.meta.json"
    )
    assert metadata["task_budget"] == {
        "max_steps": 100,
        "max_tokens": 250000,
        "max_wall_s": 3600,
        "max_cost_usd": 1.0,
    }
    assert metadata["task_budget_semantics"]["max_tokens"]["enforcement"] == "none"
    assert metadata["task_budget_semantics"]["max_tokens"]["status"] == "recorded-inert"
    assert (
        metadata["task_budget_block_sha256"]
        == "3a1051a3fa1b75d9dc5f1231820abac17775c8c9e164637104ca1ecdf64edde9"
    )


def test_load_rejects_swebench_version_drift(monkeypatch):
    monkeypatch.setattr(sample.importlib.metadata, "version", lambda _name: "4.1.1")
    try:
        sample.load_rows()
    except RuntimeError as exc:
        assert "expected 4.1.0" in str(exc)
    else:
        raise AssertionError("version drift was accepted")
