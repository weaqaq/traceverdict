# TraceVerdict v0.2 — Public Frozen Requirements

This is the sanitized public form of the frozen product requirements. The complete original task cards, raw run data, and per-commit audit history remain in the private audit repository. The SQLite schema in `src/traceverdict/tracer/schema.sql` is the authoritative v0.1 schema and is byte-identical to the accepted private snapshot.

## 1. Purpose

TraceVerdict makes coding-agent regressions auditable. A run binds an immutable agent/model configuration, frozen task repository, container image digest, native trajectory, authoritative patch, official test verdicts, token usage, cost, and artifact hashes. A comparison operates only on an explicit frozen task set and fails closed on incomplete pairing.

## 2. Scope and dependencies

The v0.2 harness supports mini-swe-agent 2.4.5 and SWE-agent 1.1.0. GitPython is the frozen Git library. Docker provides disposable workspaces. SWE-bench 4.1.0 supplies official benchmark tests and ground-truth verdicts. LiteLLM is used for provider adaptation and cost reconciliation.

The harness owns trace capture, state snapshots, rule verification, paired statistics, reports, and injection isolation. It does not own provider models, agent algorithms, Docker, or the official SWE-bench judge.

## 3. Frozen data model

The schema contains immutable configuration identity, task identity, run lifecycle and resource metrics, ordered events, content-addressed artifacts, deterministic rule verdicts, injections, comparisons, and report statistics. Public rename work does not add, remove, or rename a schema column.

## 4. CLI contract

The public v0.2 CLI has ten top-level commands: `run`, `suite`, `compare`, `report`, `inject`, `replay`, `selftest`, `quick`, `baseline`, and `ingest`; `tv` and `traceverdict` are equivalent entry points. Unsupported or gated behavior exits with code 2. `compare` requires an explicit task-set file; deriving a convenient database intersection is forbidden. The v0.1 SQLite core schema remains frozen.

Daily Mode is self-suite-only. `quick` runs S1/S4/S6, `quick --full` runs S1-S8 while reusing completed smoke tasks, and the 16-task SWE-bench set is prohibited. Configs are content-addressed immutable derivatives of a canonical parent, sorted overrides, prompt identity, and strict price-registry identity. Cached baselines live only in ignored local state; promotion never starts a model and requires explicit acceptance of a correctness/forbidden regression.

Passive `ingest` starts no agent, Docker container, or verifier. It incrementally processes stable Codex exec JSONL and a separately versioned observed desktop-rollout format, persists metrics only, detects truncation/prefix rewrites, and fails closed on required usage drift.

## 5. Reproducibility and honesty rules

- Each run uses a disposable checkout; frozen fixtures are never mounted writable.
- The authoritative patch includes untracked files and excludes harness artifacts.
- Missing native exit data is recorded as incomplete, never repaired into a fictional final event.
- Prompt hashes, tool observations, usage, and artifact hashes remain auditable.
- Provider usage/cost mismatches become harness errors under strict reconciliation.
- The official SWE-bench verdict is ground truth for benchmark tasks.
- Rule results and optional human/judge interpretation remain separate tracks.
- Paid runs require written authorization and a cumulative budget check.

## 6. Milestones represented in this release

- M1: tracing, reconciliation, disposable environments, self suite, and a faithful regression battery.
- M2: 16 frozen SWE-bench Verified tasks; five-task three-way pilot agreement; 16-task baseline.
- M3: paired cross-provider experiment, known read-only-workspace regression, and second-agent compatibility arm.

## 7. Frozen statistics

Comparisons use 10,000 task-paired bootstrap resamples, 95% confidence intervals, seed `20260710`, and an exact two-sided McNemar test. A `k=2` one-pass/one-fail task has no majority and is disclosed as an excluded tie for McNemar only; bootstrap uses its task mean. Hard and warning thresholds live in `src/traceverdict/compare/constants.py`.

## 8. Replay boundaries

Three boundaries are distinct: deterministic environment replay, deterministic tool-observation replay, and scenario re-run with a live model. Matching a prompt hash detects divergence; it does not imply identical stochastic output. Full replay work remains beyond the v0.2 public release.

## 9. Public positioning (frozen)

1. TraceVerdict is not an agent-rewind generic recorder. It freezes repository/environment state and evaluates file behavior and tests.
2. TraceVerdict is a coding-agent state-regression layer below general trace products such as LangSmith and Langfuse; it can export traces to them.
3. TraceVerdict sits on top of the SWE-bench official harness. The official harness is ground truth and is not replaced.

## 10. Public revision log excerpt

| Date | Area | Revision | Reason |
|---|---|---|---|
| 2026-07-11 | M1 injection I1 | Retired deletion of tool instructions; replaced during instrument qualification | No degradation was observed at `n=8, k=1`; kept as robustness evidence F-1 |
| 2026-07-11 | M1 injection I2 | Retired 500-character observation truncation | Adaptive recovery and small fixtures made the instrument ineffective; F-2 |
| 2026-07-11 | M1 injection I1P | Retired task-template loss from the M1 gate | Cost-only detection was unstable at `k=1`; F-3 |
| 2026-07-11 | M1 battery | Froze native-tool/readonly/sampling/history instruments with explicit deterministic/probabilistic admission claims | Ended serial threshold-tuning and preserved fail-closed stop rules |
| 2026-07-12 | Verifier portability | Normalize frozen-text line endings before hashing | Removed the Windows CRLF false positive documented in F-4 |
| 2026-07-14 | Provider cost | Versioned MiMo registry/config after reasoning-token partition mismatch | Strict reconciliation caught the issue before expanding the experiment; F-5 |

## 11. Publication boundary

The public mirror contains product code, tests, self fixtures, frozen task identifiers, sanitized aggregate evidence, and readable decision/finding summaries. Databases, full trajectories, provider logs, machine paths, server configuration, credentials, and the private Git history are deliberately excluded.
