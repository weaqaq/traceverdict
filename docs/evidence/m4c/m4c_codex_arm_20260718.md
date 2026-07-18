# M4-C Codex compatibility arm

## Scope and identity

- Candidate: `m4c-codex-0-144-4-gpt-5-6-luna-high-subscription-v2`
- Baseline: `dev-deepseek-v4-flash-v2`
- Frozen tasks: 16 SWE-bench Verified instances
- Candidate repetitions: `k=1`; baseline repetitions: `k=2`
- Codex CLI: `0.144.4`, model string `gpt-5.6-luna`, reasoning effort `high`
- Billing: `subscription_unallocatable`; actual incremental API spend `$0`

The executor and subject share a vendor; ground truth remains the official tests. Scaffolding differences from mini are subject properties and are not normalized away. The agent layer is additive; the task test environment is unchanged.

The agent phase ran locally with the subscription credential hidden from agent shell commands. Each handoff contained only a manifest and authoritative patch. The verifier host received no Codex JSONL, workspace, environment value, user path, or credential. Official tests ran in the original task image rather than the additive agent image.

## Gate and verdict results

- Self-suite adapter gate: 8/8 complete before the public benchmark arm.
- Public agent runs: 16/16 complete, no selected harness errors.
- Sanitized patch packages: 16/16 SHA-verified.
- Official raw/aggregate agreement: 16/16.
- Resolved: 6/16.
- Task identity check: base commit, instruction, budget bytes and official test specification matched the frozen baseline for all 16 tasks.

| Task | Run | Official verdict |
|---|---|---:|
| pytest-dev__pytest-7982 | `run-943e46d3ff31` | pass |
| sympy__sympy-20438 | `run-c9996628da1a` | fail |
| django__django-16315 | `run-10bf2cc49eff` | pass |
| pydata__xarray-7229 | `run-1439de003501` | fail |
| scikit-learn__scikit-learn-12682 | `run-d5ac625c5fd7` | fail |
| astropy__astropy-14369 | `run-d6506659ff22` | pass |
| django__django-14376 | `run-1461c2ea242a` | fail |
| matplotlib__matplotlib-22871 | `run-741d3671dac6` | fail |
| matplotlib__matplotlib-25960 | `run-119072354eef` | fail |
| psf__requests-1142 | `run-cd3ab8d4e748` | pass |
| pydata__xarray-3677 | `run-d7921b5a7b2f` | fail |
| pytest-dev__pytest-7490 | `run-02f34dca8608` | fail |
| scikit-learn__scikit-learn-25102 | `run-8343826458d3` | pass |
| sphinx-doc__sphinx-8593 | `run-914c840769cf` | fail |
| sphinx-doc__sphinx-8721 | `run-023b4815b09a` | fail |
| sympy__sympy-17318 | `run-cb8ed7f39f6e` | pass |

## Authorized asymmetric comparison

- Delta pass: `+0.0625`.
- Paired bootstrap: 10,000 resamples, seed `20260710`, 95% CI `[-0.15625, 0.3125]`.
- Exact two-sided McNemar: `p=0.25`; cells both-pass `3`, baseline-only `0`, candidate-only `3`, both-fail `6`.
- Baseline 1/2 ties excluded from McNemar: `django__django-14376`, `matplotlib__matplotlib-25960`, `pydata__xarray-3677`, `pytest-dev__pytest-7490`.
- Median token ratio: `1.137549`.
- P95 wall-time ratio: `1.314402`.
- Alarm: `none`.

The asymmetric `k=2` versus `k=1` design has lower variance resolution than a balanced experiment. It is compatibility evidence, not a leaderboard or a single-factor model comparison.

## Cost and trace limits

Per-call subscription spend cannot be allocated. `run.cost_usd` is therefore NULL and actual cost alarms are unavailable by design. The API-equivalent shadow estimate totals `$9.9315542`, is classified as a lower bound because cache-write input is unobserved, and does not enter the real-spend tripwire. Trace events are Codex turn aggregates; their CLI-input hash is not represented as a hash of the hosted model's internal prompt.

Raw databases, trajectories, official logs, patch packages, subscription-window records and machine-specific image evidence remain in the private audit store.
