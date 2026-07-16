#!/usr/bin/env python3
"""Freeze the TraceVerdict v0.1 stratified SWE-bench Verified subset.

Membership is selected before pilot ordering.  The ordering search is only
allowed to permute already-selected members (D15-d).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from traceverdict.swebench_budget import (
    SWEBV_BUDGET_SEMANTICS,
    SWEBV_TASK_BUDGET,
    frozen_budget_block_sha256,
)

DATASET = "princeton-nlp/SWE-bench_Verified"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
SWEBENCH_VERSION = "4.1.0"
SEED = 20260710
QUOTAS = {
    ("low", "single"): 3,
    ("low", "multi"): 2,
    ("mid", "single"): 2,
    ("mid", "multi"): 3,
    ("high", "single"): 3,
    ("high", "multi"): 3,
}
DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    repo: str
    patch_bytes: int
    file_count: int
    size_band: str

    @property
    def file_band(self) -> str:
        return "single" if self.file_count == 1 else "multi"


def patch_file_count(patch: str) -> int:
    paths = {right for _left, right in DIFF_PATH_RE.findall(patch)}
    if not paths:
        raise ValueError("gold patch has no 'diff --git' path")
    return len(paths)


def tercile_thresholds(sizes: Sequence[int]) -> tuple[int, int]:
    sizes = sorted(sizes)
    if len(sizes) < 3:
        raise ValueError("at least three rows are required for terciles")
    # Nearest-rank cut points at 1/3 and 2/3, frozen as integer indices.
    return sizes[(len(sizes) - 1) // 3], sizes[(2 * len(sizes) - 1) // 3]


def size_band(size: int, low_cut: int, high_cut: int) -> str:
    if size <= low_cut:
        return "low"
    if size <= high_cut:
        return "mid"
    return "high"


def make_candidates(
    rows: Sequence[dict],
) -> tuple[list[Candidate], dict[str, tuple[int, int]]]:
    measured = []
    for row in rows:
        patch = row["patch"]
        byte_count = len(patch.encode("utf-8"))
        file_count = patch_file_count(patch)
        measured.append((row, byte_count, file_count))
    # Size is stratified *within* the single/multi-file dimension.  Global
    # cuts yield zero low/multi candidates in Verified revision c104f84 and
    # therefore cannot realize the frozen six-cell quotas.
    thresholds = {
        file_band: tercile_thresholds(
            [size for _row, size, files in measured if ("single" if files == 1 else "multi") == file_band]
        )
        for file_band in ("single", "multi")
    }
    candidates = []
    for row, byte_count, file_count in measured:
        file_band = "single" if file_count == 1 else "multi"
        low_cut, high_cut = thresholds[file_band]
        candidates.append(
            Candidate(
                instance_id=row["instance_id"],
                repo=row["repo"],
                patch_bytes=byte_count,
                file_count=file_count,
                size_band=size_band(byte_count, low_cut, high_cut),
            )
        )
    return candidates, thresholds


def select_members(candidates: Sequence[Candidate], seed: int = SEED) -> list[Candidate]:
    """Select exact cell quotas with a global per-repository cap of two."""
    rng = random.Random(seed)
    pools: dict[tuple[str, str], list[Candidate]] = {}
    for cell, quota in QUOTAS.items():
        pool = [c for c in candidates if (c.size_band, c.file_band) == cell]
        pool.sort(key=lambda c: c.instance_id)
        rng.shuffle(pool)
        if len(pool) < quota:
            raise ValueError(f"insufficient candidates for {cell}: {len(pool)} < {quota}")
        pools[cell] = pool

    cells = [cell for cell, count in QUOTAS.items() for _ in range(count)]
    # Most constrained slots first; seeded pool order remains the tie-breaker.
    cells.sort(key=lambda cell: (len(pools[cell]), cell))
    selected: list[Candidate] = []
    repo_counts: Counter[str] = Counter()

    def search(index: int) -> bool:
        if index == len(cells):
            return True
        cell = cells[index]
        for candidate in pools[cell]:
            if candidate in selected or repo_counts[candidate.repo] >= 2:
                continue
            selected.append(candidate)
            repo_counts[candidate.repo] += 1
            if search(index + 1):
                return True
            repo_counts[candidate.repo] -= 1
            selected.pop()
        return False

    if not search(0):
        raise ValueError("no quota-conforming member set satisfies repo<=2")
    return sorted(selected, key=lambda c: c.instance_id)


def order_for_pilot(members: Sequence[Candidate], seed: int = SEED) -> list[Candidate]:
    """Reorder frozen members so the first five cover all pilot dimensions."""
    rng = random.Random(seed ^ 0x5A17)
    choices = list(members)
    choices.sort(key=lambda c: c.instance_id)
    rng.shuffle(choices)

    prefix: list[Candidate] = []

    def search(start: int) -> bool:
        if len(prefix) == 5:
            return (
                len({c.repo for c in prefix}) == 5
                and {c.size_band for c in prefix} == {"low", "mid", "high"}
                and {c.file_band for c in prefix} == {"single", "multi"}
            )
        for idx in range(start, len(choices)):
            candidate = choices[idx]
            if any(candidate.repo == item.repo for item in prefix):
                continue
            prefix.append(candidate)
            choices[start], choices[idx] = choices[idx], choices[start]
            if search(start + 1):
                return True
            choices[start], choices[idx] = choices[idx], choices[start]
            prefix.pop()
        return False

    if not search(0):
        raise ValueError("frozen members cannot satisfy the first-five pilot coverage")
    rest = sorted((c for c in members if c not in prefix), key=lambda c: c.instance_id)
    return [*prefix, *rest]


def cost_probe_members(pilot: Sequence[Candidate]) -> list[Candidate]:
    """Pick three preregistered pilot members covering size and file bands."""
    from itertools import combinations

    for group in combinations(pilot[:5], 3):
        if (
            {candidate.size_band for candidate in group} == {"low", "mid", "high"}
            and {candidate.file_band for candidate in group} == {"single", "multi"}
        ):
            return list(group)
    raise ValueError("pilot prefix has no valid three-instance cost probe")


def load_rows() -> list[dict]:
    from datasets import load_dataset

    actual_version = importlib.metadata.version("swebench")
    if actual_version != SWEBENCH_VERSION:
        raise RuntimeError(
            f"swebench revision drift: expected {SWEBENCH_VERSION}, got {actual_version}"
        )
    dataset = load_dataset(DATASET, split="test", revision=DATASET_REVISION)
    rows = [dict(row) for row in dataset]
    instance_ids = [row["instance_id"] for row in rows]
    if len(rows) != 500 or len(set(instance_ids)) != 500:
        raise RuntimeError("dataset revision drift: expected 500 unique test instances")
    return rows


def freeze(rows: Sequence[dict], output: Path, metadata_path: Path) -> dict:
    candidates, thresholds = make_candidates(rows)
    members = select_members(candidates)
    ordered = order_for_pilot(members)
    content = "".join(f"{candidate.instance_id}\n" for candidate in ordered).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    metadata = {
        "dataset": DATASET,
        "dataset_revision": DATASET_REVISION,
        "split": "test",
        "swebench_version": SWEBENCH_VERSION,
        "seed": SEED,
        "patch_size_unit": "UTF-8 bytes",
        "patch_file_count": "unique b/ paths in diff --git headers",
        "tercile_index_rule": "within each file band: sorted[(n-1)//3], sorted[(2*n-1)//3]",
        "tercile_thresholds_bytes": {
            band: {"low_max": cuts[0], "mid_max": cuts[1]}
            for band, cuts in thresholds.items()
        },
        "quotas": {f"{size}_{files}": count for (size, files), count in QUOTAS.items()},
        "repo_cap": 2,
        "member_selection_precedes_pilot_ordering": True,
        "pilot_coverage": "first five: all size bands, both file bands, five repos",
        "cost_probe_ids": [candidate.instance_id for candidate in cost_probe_members(ordered)],
        "task_set_sha256": hashlib.sha256(content).hexdigest(),
        "task_budget": dict(SWEBV_TASK_BUDGET),
        "task_budget_block_sha256": frozen_budget_block_sha256(),
        "task_budget_semantics": SWEBV_BUDGET_SEMANTICS,
        "instances": [candidate.__dict__ | {"file_band": candidate.file_band} for candidate in ordered],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("benchmarks/swebv_subset_v1.txt"))
    parser.add_argument(
        "--metadata", type=Path, default=Path("benchmarks/swebv_subset_v1.meta.json")
    )
    args = parser.parse_args()
    metadata = freeze(load_rows(), args.output, args.metadata)
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
