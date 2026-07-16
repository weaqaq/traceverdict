# M3 public evidence summary

- Frozen task-set SHA256: `eeb30802a00dd865cfc7214dc6ab123f267f1f92b9e5d3a4d21869bf4fb152be`
- New paid runs: **85/85**
- Execution: Codex; terminal review: Claude; final veto: project owner.
- Raw databases, trajectories, prompts, provider logs, host paths, and per-run artifact manifests are intentionally retained only in the private audit repository.

## Exp-A — model-family comparison

- Baseline: mini-swe-agent 2.4.5 with DeepSeek V4 Flash; candidate: mini-swe-agent 2.4.5 with MiMo V2.5.
- 16 frozen SWE-bench Verified tasks, `k=2`.
- Delta pass: `-0.09375`; paired bootstrap 95% CI: `[-0.21875, 0.03125]`.
- Exact McNemar `p=1.0`; five `k=2` ties were excluded from McNemar and disclosed.
- Alarm: **warn**, caused by the P95 wall-time channel; repeat-direction stability: `false`.
- Interpretation: pipeline evidence only, not a broad model-ranking claim.

See [the full aggregate report](exp_a_model_comparison.md).

## Exp-B — known read-only-workspace regression

- Baseline reuses the 32 Exp-A DeepSeek runs; candidate applies I3Q to the agent workspace only.
- Delta pass: `-0.3125`; bootstrap 95% CI: `[-0.5, -0.125]`.
- Exact McNemar `p=0.25`; alarm: **hard**; repeat-direction stability: `true`.
- Agent-read-only/verifier-read-write isolation evidence: **32/32**.

See [the full aggregate report](exp_b_i3q_regression.md).

## Exp-C — second-agent compatibility

Eight self-suite tasks exercised the tracer/verifier boundary with SWE-agent 1.1.0 and the same model family used by the baseline. Complete traces were captured for 8/8 tasks; one rule verdict passed. Seven tasks proposed native parallel tool calls that the pinned agent parser rejected; TraceVerdict preserved the proposals but recorded only actions that actually executed. The budget task exercised the native `LimitsExceeded -> budget` path and passed its budget verdict.

This arm tests adapter and evidence compatibility only. It is not a model-quality comparison.

## Cost and reuse boundaries

- M3 new-run spend: `$1.0413933564`.
- Cumulative audited project spend: `$1.862793712251148854773092867` against a `$28` tripwire.
- Three accepted MiMo probe runs were reused only after byte-level config/task/image/budget identity checks.
- The M2 baseline supplied Exp-A repetition 0; Exp-B explicitly shared the Exp-A DeepSeek baseline.
- `k=2` provides low-resolution variance evidence and must not be presented as leaderboard-quality inference.

## Build/reuse boundary

TraceVerdict built the tracer, adapters, verifier wiring, paired statistics, taxonomy, injection isolation, and evidence boundaries. It reused mini-swe-agent 2.4.5, SWE-agent 1.1.0, SWE-bench 4.1.0 official harness/images, LiteLLM, Docker, and provider APIs.
