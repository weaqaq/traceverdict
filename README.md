# TraceVerdict

TraceVerdict is an auditable regression-evaluation harness for coding agents. It freezes repository and container state, captures native agent trajectories, and compares the resulting file changes and official test verdicts with paired statistics.

TraceVerdict is not a general-purpose call recorder or agent-rewind system: its unit of evidence is a reproducible coding task with a frozen workspace, environment fingerprint, authoritative patch, and test outcome. It sits below general observability products such as LangSmith and Langfuse as a vertical state-regression layer for coding agents; traces can be exported to those systems. It runs on top of the official SWE-bench harness, whose verdict remains ground truth—TraceVerdict records, reconciles, and compares that verdict rather than replacing the harness.

> This repository is a clean public mirror. Full per-commit audit history and raw run data remain in a private audit repository and can be shown in an interview. Public documents preserve the decision and finding narrative while removing machine paths, credentials, personal information, and infrastructure details.

## What it provides

- adapters for mini-swe-agent 2.4.5 and pinned SWE-agent 1.1.0;
- immutable config identities and strict provider usage/cost reconciliation;
- disposable Docker workspaces with patch, artifact, and image-digest evidence;
- SWE-bench 4.1.0 official-harness verdict integration;
- paired 10,000-resample bootstrap, exact McNemar, regression alarms, and failure taxonomy;
- deterministic comparison/report generation with explicit shared-sample disclosure;
- faithful fault injection that isolates the agent environment from the verifier.

## Evidence, with limits

M3 froze 16 SWE-bench Verified tasks and added 85 paid evaluations. The cross-provider experiment used 16 tasks at `k=2`; its pass delta was `-0.09375` with bootstrap 95% CI `[-0.21875, 0.03125]`. This is engineering evidence for the regression pipeline, not a broad model leaderboard claim. A read-only-workspace regression produced a hard alarm (delta `-0.3125`, CI `[-0.5, -0.125]`) with 32/32 agent-read-only/verifier-read-write isolation records. Total audited API spend was `$1.8627937123` against a `$28` tripwire.

See the [M3 evidence summary](docs/evidence/m3/m3_summary.md) and its [machine-readable companion](docs/evidence/m3/m3_summary.json).

The later [M4-C compatibility arm](docs/evidence/m4c/m4c_codex_arm_20260718.md) ran a pinned Codex CLI locally while keeping subscription credentials off the verifier host. It completed 16/16 official judgments with 6/16 resolved; versus the DeepSeek Flash `k=2` baseline, the authorized asymmetric comparison yielded delta pass `+0.0625`, 95% CI `[-0.15625, 0.3125]`, exact McNemar `p=0.25`, and no regression alarm. This is cross-scaffolding compatibility evidence, not a model leaderboard claim.

## Install

Python 3.12 and Docker are required for real task execution.

```bash
python -m venv .venv
python -m pip install -e .
python -m pip install "mini-swe-agent==2.4.5"
traceverdict --help
```

TraceVerdict v0.2 exposes exactly ten top-level commands through either `traceverdict` or the equivalent short entry point `tv`:

```text
traceverdict run TASK --config CONFIG
traceverdict suite tasks/self --config configs/dev.yaml --dry-run
traceverdict compare --baseline BASE --candidate CANDIDATE --task-set tasks/self/task_set.txt
traceverdict report COMPARISON_ID
traceverdict inject I3Q --base configs/dev.yaml --output injected.yaml
traceverdict replay
traceverdict selftest --config configs/dev.yaml
tv quick --set model=openai/deepseek-v4-flash
tv baseline set --config configs/dev.yaml
tv ingest
```

`replay` remains an intentionally visible zero-model CI boundary and currently exits with code 2. Paid/provider runs require credentials supplied only through the process environment; no credential file belongs in this repository.

## Daily Mode

Daily Mode keeps its local, ignored state under `.traceverdict/daily/`. First establish an explicit cached baseline (this is the only step that may execute its missing baseline tasks):

```bash
tv baseline set --config configs/dev.yaml
tv quick --set model=openai/deepseek-v4-flash
tv quick --set model=openai/deepseek-v4-flash --set model_params.thinking.type=enabled --full
```

The default smoke set is frozen to S1/S4/S6; `--full` is frozen to S1-S8 and reuses already completed smoke runs. SWE-bench is deliberately unavailable through `quick`: the 16-task public benchmark is for release evidence, not daily iteration. Derived configs are content-addressed, exact-price-registry checked, and immutable. `baseline update` promotes an already complete candidate without running a model, and refuses correctness or forbidden-path regressions unless `--accept-regression` is explicit.

The one-screen result reports pass delta, token-median delta/ratio, wall-P95 delta/ratio, strict actual cost, cache reuse, and failed tasks. PASS/WARN exit 0, a behavioral FAIL exits 1, and a missing baseline or invalid identity exits 2. The five-minute/$0.005 and full-suite timing figures are experience estimates only; each invocation reports measured time and cost.

`tv ingest [PATH ...]` is passive and starts no model, Docker container, or verifier. It incrementally summarizes stable `codex exec --json` logs and the explicitly versioned July 2026 desktop-rollout compatibility format. Only dates, model, token classes, turns, tool counts, failure classes, and aggregate event counters are persisted; prompts, answers, commands, output, paths, and credentials are not. Desktop transcript format is not a stable public interface, so required-field drift fails closed instead of being guessed. Format `codex-rollout-jsonl-observed-2026-07-v2` differs from v1 only by recognizing `token_count` records whose entire `info` value is null as zero-token initialization heartbeats; a non-null `info` without required usage remains an error.

This package is published by the project owner. Trusted Publishing through GitHub Actions OIDC is planned as a v0.3 supply-chain improvement.

## Reproduce the frozen report

```bash
python scripts/finalize_m3.py \
  --db reports/m3/traceverdict.db \
  --completion-root reports/m3 \
  --output reports/m3/final
```

The command fails closed on an incomplete 16-task/`k=2` matrix, missing metrics or verdicts, environment-fingerprint drift, missing I3Q isolation evidence, or an incomplete SWE-agent trace. Raw databases and trajectories are intentionally absent from the public mirror.

## Findings index

- [F-1](docs/findings/F1.md): deleting tool instructions did not degrade the tested agent at `n=8, k=1`.
- [F-2](docs/findings/F2.md): truncating observations to 500 characters invited adaptive recovery.
- [F-3](docs/findings/F3.md): a task-template-loss alarm was unstable when it depended only on cost at `k=1`.
- [F-4](docs/findings/F4.md): CRLF conversion caused a cross-platform forbidden-path false positive.
- [F-5](docs/findings/F5.md): strict reconciliation caught a provider reasoning-token pricing partition bug.
- [F-6](docs/findings/F6.md): a 96-run, four-arm audit separated shared failures into sampling difficulty, runtime constraints, and submitted task-level convergence.

## Build/reuse boundary

Self-built components are the tracer, agent adapters, verifier wiring, snapshot discipline, comparison statistics, taxonomy, injection isolation, and evidence/replay boundaries. Reused components are mini-swe-agent, SWE-agent, the SWE-bench official harness and images, LiteLLM, Docker, and provider APIs. See the [public PRD](docs/PRD.md), [decision log](docs/decisions.md), and [publication provenance](docs/publication_provenance.md).

## Security and provenance

CI runs a fail-closed public-safety scan over text, binary bundles, nested Git authors/content, and repository history. The initial public history is one genuine release commit; it does not imitate the private development history. See [name audit](docs/name_audit.md).

## License

[MIT](LICENSE)
