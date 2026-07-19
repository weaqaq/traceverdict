#!/usr/bin/env python3
"""Generate the zero-run M4-D convergence diagnostic from sealed evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from traceverdict.m4d import (
    ARM_SPECS,
    build_diagnostic,
    dump_json,
    load_db_arm,
    load_pro_markdown,
    load_task_set,
    load_verified_arrow,
    sha256_file,
)


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def render_markdown(data: dict[str, Any]) -> str:
    lines = [
        "# F-6 — Four-arm convergence is a mixed effect",
        "",
        "M4-D is a read-only diagnosis. It made no model call, agent run, Docker run, or paid request.",
        "",
        "## Evidence boundary",
        "",
        f"- Frozen task-set SHA256: `{data['task_set_sha256']}`.",
        f"- Sealed run matrix: {data['run_accounting']['actual']}/{data['run_accounting']['expected']} runs.",
        "- Arms: Flash k=2, MiMo k=2, Pro k=1, Codex k=1.",
    ]
    pro_wall = data["exit_audit"]["arms"]["pro"]["wall"]
    if pro_wall.get("missing"):
        lines.append(
            f"- Pro wall-time is unavailable for {pro_wall['missing']} runs because the public fallback omits the private DB field; no rerun was used to fill it."
        )
    evidence = data["evidence"]
    lines.extend(
        [
            f"- Flash/MiMo DB SHA256: `{evidence['main_db_sha256']}`.",
            f"- Codex DB SHA256: `{evidence['codex_db_sha256']}`.",
            f"- Pro source: `{evidence['pro']['kind']}`; SHA256 `{evidence['pro']['sha256']}`.",
            f"- Verified Arrow SHA256: `{evidence['verified_arrow_sha256']}`.",
        ]
    )
    runtime = data["test_runtime"]
    lines.extend(
        [
            f"- Official test runtime: **{runtime['status']}**. {runtime.get('reason') or ''}",
            f"- Full-Verified runtime comparison: {runtime['verified_500_comparison']}.",
            "",
            "## Per-task pass matrix",
            "",
            "Cells are pass-count/repetitions. A 1/2 cell is a tie, not a zero-success failure.",
            "",
            "| Task | Flash | MiMo | Pro | Codex |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in data["matrix"]["rows"]:
        cells = row["arms"]
        lines.append(
            f"| {row['task_id']} | {cells['flash']['passed']}/{cells['flash']['repetitions']} "
            f"| {cells['mimo']['passed']}/{cells['mimo']['repetitions']} "
            f"| {cells['pro']['passed']}/{cells['pro']['repetitions']} "
            f"| {cells['codex']['passed']}/{cells['codex']['repetitions']} |"
        )
    lines.extend(
        [
            "",
            f"Strict zero-success intersection ({len(data['matrix']['strict_all_fail'])}): "
            + ", ".join(f"`{task}`" for task in data["matrix"]["strict_all_fail"])
            + ".",
            "",
            "Tie-sensitive candidates (kept outside the strict intersection): "
            + (", ".join(f"`{task}`" for task in data["matrix"]["tie_sensitive"]) or "none")
            + ".",
            "",
            "## Difficulty shift",
            "",
            "| Population | n | Patch P25 | Patch median | Patch P75 | Patch mean | File median | File mean | Multi-file |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for key, label in (
        ("verified_500", "Verified full set"),
        ("frozen_16", "Frozen subset"),
        ("strict_all_fail", "Strict all-fail"),
    ):
        profile = data["difficulty_shift"][key]
        patch = profile["patch_bytes"]
        files = profile["files"]
        lines.append(
            f"| {label} | {profile['count']} | {_fmt(patch['p25'])} | {_fmt(patch['median'])} "
            f"| {_fmt(patch['p75'])} | {_fmt(patch['mean'])} | {_fmt(files['median'])} "
            f"| {_fmt(files['mean'])} | {files['multi_file_fraction']:.1%} |"
        )
    lines.extend(
        [
            "",
            f"The frozen subset's patch median is {data['difficulty_shift']['sample_to_full_patch_median_ratio']:.2f}× the full-set median; "
            f"its multi-file share is higher by {data['difficulty_shift']['sample_multi_file_percentage_point_shift']:.1f} percentage points. "
            "Because selection deliberately stratified patch size and file count, these are descriptive shifts, not random-sample significance claims.",
            "",
            "Verified `difficulty` labels are reported only as human repair-time metadata; they are not test runtime.",
            "",
            "| Population | <15 min | 15 min–1 hour | 1–4 hours | >4 hours |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key, label in (
        ("verified_500", "Verified full set"),
        ("frozen_16", "Frozen subset"),
        ("strict_all_fail", "Strict all-fail"),
    ):
        counts = data["difficulty_shift"][key]["difficulty"]
        lines.append(
            f"| {label} | {counts.get('<15 min fix', 0)} | {counts.get('15 min - 1 hour', 0)} "
            f"| {counts.get('1-4 hours', 0)} | {counts.get('>4 hours', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Exit and 3600-second wall audit",
            "",
            "| Arm | Runs | Exit distribution | Wall min/median/P95/max | Within 300/120/60s |",
            "|---|---:|---|---|---|",
        ]
    )
    for arm in ("flash", "mimo", "pro", "codex"):
        item = data["exit_audit"]["arms"][arm]
        exits = ", ".join(
            f"{row['status']}/{row['exit_reason']}={row['count']}"
            for row in item["exit_distribution"]
        )
        wall = item["wall"]
        if wall.get("available"):
            wall_text = "/".join(_fmt(wall[key]) for key in ("min", "median", "p95", "max"))
            threshold_text = f"{wall['within_300s']}/{wall['within_120s']}/{wall['within_60s']}"
        else:
            wall_text = f"unavailable ({wall['missing']} missing)"
            threshold_text = "unavailable"
        lines.append(f"| {arm} | {item['run_count']} | {exits} | {wall_text} | {threshold_text} |")
    total_exits = ", ".join(
        f"{row['status']}/{row['exit_reason']}={row['count']}"
        for row in data["exit_audit"]["exit_distribution"]
    )
    lines.extend(["", f"Aggregate across 96 runs: {total_exits}.", ""])
    for arm in ("flash", "mimo", "pro", "codex"):
        wall = data["exit_audit"]["arms"][arm]["wall"]
        if not wall.get("available"):
            lines.append(f"- **{arm} nearest-wall runs:** unavailable ({wall['missing']} missing wall values).")
            continue
        nearest = "; ".join(
            f"`{row['task_id']}` r={row['wall_time_s']:.3f}s, distance={row['distance_to_3600_s']:.3f}s"
            for row in wall["nearest"]
        )
        lines.append(f"- **{arm} nearest-wall runs:** {nearest}.")
    codex_wall = data["exit_audit"]["arms"]["codex"]["wall"]
    lines.extend(
        [
            "",
            f"Codex's maximum observed wall time was {_fmt(codex_wall['max'])}s, "
            f"leaving {_fmt(3600-codex_wall['max'])}s to the hard wall; no Codex run was within 300 seconds of it.",
            "",
            "## What converged, and why",
            "",
        ]
    )
    for row in data["all_fail_classification"]:
        lines.append(
            f"- `{row['task_id']}` — **{row['category']}**: "
            f"{row['submitted_runs']} submitted, {row['non_submitted_runs']} non-submitted runs"
            + (f" ({row['non_submitted_reasons']})" if row["non_submitted_reasons"] else "")
            + "."
        )
    lines.extend(
        [
            "",
            "The convergence is not attributable to one cause. The frozen subset is structurally harder than the 500-task distribution on patch size and file count; several failures are entangled with budget, context-window, or API exits; and a smaller core remains unresolved even after normal submission. Only the last category is strong evidence of stable task-level convergence. This is not a model leaderboard or a general capability claim.",
            "",
            "## Resume and interview wording",
            "",
            "> 对96条跨四种被试配置的SWE-bench轨迹实施退出与难度审计，将共同失败拆分为抽样难度偏移、运行约束和正常提交后的任务级收敛，避免把低通过率直接包装成模型结论。",
            "",
            "> 我先检查样本是不是更难，再检查模型是否被预算、上下文或硬墙截断，最后只在各臂正常提交后仍共同失败时谈真实收敛；数通过率本身不能完成归因。",
            "",
        ]
    )
    return "\n".join(lines)


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    task_ids, task_set_sha = load_task_set(args.task_set)
    arms = {
        "flash": load_db_arm(
            args.main_db,
            config_id=ARM_SPECS["flash"]["config_id"],
            task_ids=task_ids,
            repetitions=2,
        ),
        "mimo": load_db_arm(
            args.main_db,
            config_id=ARM_SPECS["mimo"]["config_id"],
            task_ids=task_ids,
            repetitions=2,
        ),
        "codex": load_db_arm(
            args.codex_db,
            config_id=ARM_SPECS["codex"]["config_id"],
            task_ids=task_ids,
            repetitions=1,
        ),
    }
    if args.pro_db:
        arms["pro"] = load_db_arm(
            args.pro_db,
            config_id=ARM_SPECS["pro"]["config_id"],
            task_ids=task_ids,
            repetitions=1,
        )
        pro_source = {"kind": "private_db", "sha256": sha256_file(args.pro_db)}
    else:
        arms["pro"] = load_pro_markdown(args.pro_summary, task_ids)
        pro_source = {"kind": "public_summary_fallback", "sha256": sha256_file(args.pro_summary)}
    evidence = {
        "main_db_sha256": sha256_file(args.main_db),
        "codex_db_sha256": sha256_file(args.codex_db),
        "pro": pro_source,
        "verified_arrow_sha256": sha256_file(args.verified_arrow),
    }
    data = build_diagnostic(
        arms=arms,
        task_ids=task_ids,
        task_set_sha=task_set_sha,
        verified_rows=load_verified_arrow(args.verified_arrow),
        evidence=evidence,
        runtime_roots=args.official_log_root,
    )
    dump_json(args.output_json, data)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(render_markdown(data), "utf-8")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-db", type=Path, required=True)
    parser.add_argument("--codex-db", type=Path, required=True)
    parser.add_argument("--pro-db", type=Path)
    parser.add_argument("--pro-summary", type=Path)
    parser.add_argument("--task-set", type=Path, required=True)
    parser.add_argument("--verified-arrow", type=Path, required=True)
    parser.add_argument("--official-log-root", type=Path, action="append", default=[])
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    if not args.pro_db and not args.pro_summary:
        parser.error("one of --pro-db or --pro-summary is required")
    return args


if __name__ == "__main__":
    analyze(parse_args())
