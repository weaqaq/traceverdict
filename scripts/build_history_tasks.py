"""Build deterministic S9-S11 bundles from the accepted public HEAD.

The task bases are explicit rollback constructions, not claims that the sick
trees were historical public commits.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "self"
COPY = (
    "src/traceverdict/__init__.py",
    "src/traceverdict/ingest.py",
    "src/traceverdict/tracer/__init__.py",
    "src/traceverdict/tracer/codex_jsonl.py",
    "src/traceverdict/tracer/trajectory.py",
    "src/traceverdict/adapters/__init__.py",
    "src/traceverdict/adapters/codex.py",
    "src/traceverdict/adapters/mini_swe_agent.py",
    "src/traceverdict/core/__init__.py",
    "src/traceverdict/core/simple_yaml.py",
    "src/traceverdict/swebench_budget.py",
)


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def common_repo(work: Path) -> None:
    for rel in COPY:
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    write(work / "pytest.ini", "[pytest]\npythonpath = src\n")


def commit_bundle(work: Path, task_id: str) -> str:
    run("git", "init", "-b", "main", cwd=work)
    run("git", "config", "user.name", "TraceVerdict Fixture", cwd=work)
    run("git", "config", "user.email", "weaqaq@users.noreply.github.com", cwd=work)
    run("git", "add", ".", cwd=work)
    run("git", "commit", "-m", f"{task_id} rollback-constructed sick base", cwd=work)
    base = run("git", "rev-parse", "HEAD", cwd=work)
    target = TASKS / task_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "BASE_COMMIT.txt").write_text(base + "\n", "utf-8")
    subprocess.run(["git", "bundle", "create", str(target / "repo.bundle"), "HEAD"], cwd=work, check=True)
    return base


def task_yaml(task_id: str, base: str, instruction: str, f2p: str, p2p: str, forbidden: list[str], tags: list[str]) -> str:
    forbidden_block = (
        "forbidden_paths: []"
        if not forbidden
        else "forbidden_paths:\n" + "\n".join(f"  - {x}" for x in forbidden)
    )
    return f'''id: {task_id}
suite: self
source: public_head_rollback
repo_ref: repo.bundle
base_commit: {base}
image_ref: "traceverdict/self-base:py3.12-v1"
instruction: {instruction}
budget:
  max_steps: 30
  max_tokens: 100000
  max_wall_s: 600
  max_cost_usd: 1.0
{forbidden_block}
gt:
  type: pytest
  spec:
    fail_to_pass:
      - "{f2p}"
    pass_to_pass:
      - "{p2p}"
tags:
''' + "\n".join(f"  - {tag}" for tag in tags) + "\n"


def build_s9(root: Path) -> None:
    common_repo(root)
    path = root / "src/traceverdict/ingest.py"
    text = path.read_text("utf-8")
    good = '''            out["token_count_events"] += 1
            info = payload.get("info")
            if info is None:
                out["null_usage_heartbeats"] += 1
                continue
            current = _desktop_usage(info)'''
    text = text.replace(good, '''            current = _desktop_usage(payload.get("info"))''')
    write(path, text)
    write(root / "tests/test_case.py", '''
import json
from pathlib import Path
import pytest
from traceverdict.ingest import IngestError, ingest

def line(x): return (json.dumps(x) + "\\n").encode()
def test_null_heartbeat_is_accepted(tmp_path: Path):
    p=tmp_path/"r.jsonl"; p.write_bytes(line({"type":"event_msg","payload":{"type":"token_count","info":None}}))
    ingest([p], state_path=tmp_path/"s", metrics_path=tmp_path/"m")
def test_non_null_missing_usage_stays_fail_closed(tmp_path: Path):
    p=tmp_path/"r.jsonl"; p.write_bytes(line({"type":"event_msg","payload":{"type":"token_count","info":{}}}))
    with pytest.raises(IngestError): ingest([p], state_path=tmp_path/"s", metrics_path=tmp_path/"m")
''')
    base = commit_bundle(root, "S9")
    target = TASKS / "S9"
    write(target / "task.yaml", task_yaml("S9", base, "Accept a null token-count heartbeat without weakening non-null usage validation.", "tests/test_case.py::test_null_heartbeat_is_accepted", "tests/test_case.py::test_non_null_missing_usage_stays_fail_closed", ["tests/"], ["uf1", "rollback_constructed", "fail_closed"]))
    write(target / "verify/README.md", "# S9 — UF-1 case history\n\nDerived from public HEAD by reversing only the null-heartbeat compatibility fix. This is a rollback construction, not an original historical commit. Tests are frozen and forbidden.\n")


def build_s10(root: Path) -> None:
    common_repo(root)
    path = root / "src/traceverdict/ingest.py"
    text = path.read_text("utf-8")
    text = text.replace('    state.setdefault("diagnostics", [])\n', '')
    block = '''            if int(day.get("open_turns", 0)) + int(delta.get("open_turns", 0)) < 0:
                state["diagnostics"].append({
                    "kind": "open_turn_underflow",
                    "source_sha256": source_id,
                    "offset": consumed,
                })
                delta["open_turns"] = -int(day.get("open_turns", 0))
'''
    text = text.replace(block, '')
    write(path, text)
    write(root / "tests/test_case.py", '''
import json
from pathlib import Path
from traceverdict.ingest import ingest
def line(x): return (json.dumps(x) + "\\n").encode()
def test_unmatched_close_is_clamped_and_diagnosed(tmp_path: Path):
    p=tmp_path/"r.jsonl"; opened=line({"timestamp":"2026-07-18","type":"event_msg","payload":{"type":"task_started","turn_id":"one"}}); p.write_bytes(opened)
    ingest([p],state_path=tmp_path/"s",metrics_path=tmp_path/"m")
    p.write_bytes(opened+line({"timestamp":"2026-07-19","type":"event_msg","payload":{"type":"task_complete","turn_id":"one"}}))
    result=ingest([p],state_path=tmp_path/"s",metrics_path=tmp_path/"m")
    assert result["added"]["open_turns"] == 0
    state=json.loads((tmp_path/"s").read_text()); assert state["diagnostics"][0]["kind"] == "open_turn_underflow"
def test_balanced_open_close_remains_zero(tmp_path: Path):
    p=tmp_path/"r.jsonl"; p.write_bytes(b"".join([line({"type":"event_msg","payload":{"type":"task_started","turn_id":"1"}}),line({"type":"event_msg","payload":{"type":"task_complete","turn_id":"1"}})]))
    assert ingest([p],state_path=tmp_path/"s",metrics_path=tmp_path/"m")["added"]["open_turns"] == 0
''')
    base = commit_bundle(root, "S10")
    target = TASKS / "S10"
    write(target / "task.yaml", task_yaml("S10", base, "Prevent open-turn counts from becoming negative and retain a content-free diagnostic.", "tests/test_case.py::test_unmatched_close_is_clamped_and_diagnosed", "tests/test_case.py::test_balanced_open_close_remains_zero", ["tests/"], ["uf2", "rollback_constructed", "counter_invariant"]))
    write(target / "verify/README.md", "# S10 — UF-2 case history\n\nDerived from the public UF-2 fix by reversing its clamp/diagnostic hunk. This rollback-constructed base is the direct pre-fix implementation artifact, not a claimed historical release.\n")


def build_s11(root: Path) -> None:
    registry = json.loads((ROOT / "configs/litellm_models_mimo_v2.json").read_text("utf-8"))
    registry["xiaomi_mimo/mimo-v2.5"]["output_cost_per_reasoning_token"] = 0.0
    write(root / "configs/litellm_models_mimo_v2.json", json.dumps(registry, indent=2) + "\n")
    fixture = {
        "synthetic": {"expected_cny": "0.00061908", "calls": [{"uncached":335,"cached":192,"completion":58},{"uncached":86,"cached":512,"completion":34}]},
        "paid": {"registry_total_usd":"0.001043790321955755298128704816","without_reasoning_usd":"0.000953452094068420471","reasoning_tokens":306},
    }
    write(root / "tests/fixtures/mimo_usage.json", json.dumps(fixture, indent=2) + "\n")
    write(root / "tests/test_case.py", '''
import json
from decimal import Decimal
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_two_real_datasets_reconstruct_with_one_partition_rule():
    price=json.loads((ROOT/"configs/litellm_models_mimo_v2.json").read_text())["xiaomi_mimo/mimo-v2.5"]
    data=json.loads((ROOT/"tests/fixtures/mimo_usage.json").read_text())
    cny=sum(Decimal(x["uncached"])*Decimal("0.000001")+Decimal(x["cached"])*Decimal("0.00000002")+Decimal(x["completion"])*Decimal("0.000002") for x in data["synthetic"]["calls"])
    assert cny == Decimal(data["synthetic"]["expected_cny"])
    paid=data["paid"]; rate=Decimal(str(price["output_cost_per_reasoning_token"]))
    reconstructed=Decimal(paid["without_reasoning_usd"])+Decimal(paid["reasoning_tokens"])*rate
    assert abs(reconstructed-Decimal(paid["registry_total_usd"])) <= Decimal("1e-18")
def test_reasoning_and_text_share_official_output_rate():
    p=json.loads((ROOT/"configs/litellm_models_mimo_v2.json").read_text())["xiaomi_mimo/mimo-v2.5"]
    assert p["output_cost_per_reasoning_token"] == p["output_cost_per_token"]
''')
    write(root / "pytest.ini", "[pytest]\n")
    base = commit_bundle(root, "S11")
    target = TASKS / "S11"
    forbidden = ["tests/test_case.py", "tests/fixtures/mimo_usage.json"]
    write(target / "task.yaml", task_yaml("S11", base, "Restore strict MiMo reasoning/text partition pricing so both frozen real datasets reconstruct exactly.", "tests/test_case.py::test_two_real_datasets_reconstruct_with_one_partition_rule", "tests/test_case.py::test_reasoning_and_text_share_official_output_rate", forbidden, ["f5", "rollback_constructed", "strict_cost"]))
    write(target / "verify/README.md", "# S11 — F-5 case history\n\nThe price registry is intentionally writable because it is the legitimate repair surface. The dual-dataset test and frozen usage fixture are forbidden so the agent cannot pass by changing evidence. The sick base is a public-HEAD rollback construction, not a historical commit original.\n")


def main() -> int:
    for task in ("S9", "S10", "S11"):
        shutil.rmtree(TASKS / task, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="traceverdict-history-") as temp:
        base = Path(temp)
        for task, builder in (("S9", build_s9), ("S10", build_s10), ("S11", build_s11)):
            work = base / task; work.mkdir(); builder(work)
    print("built S9-S11 rollback-constructed bundles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
