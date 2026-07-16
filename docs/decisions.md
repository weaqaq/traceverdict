# Public Decisions Log

> **Audit-history note:** abbreviated commit hashes mentioned here identify commits in the private audit repository. They are provenance references, not dangling claims about this public mirror. The private history and raw run data can be shown in an interview. This document is a sanitized, append-only public summary; it does not rewrite the private original.

| ID | Date | Public decision summary |
|---|---|---|
| D0 | 2026-07-10 | Freeze the v0.1 schema, GitPython, seven-command CLI, small commits, CI, and explicit stop conditions before business logic. |
| D1 | 2026-07-10 | Pin mini-swe-agent 2.4.5; preserve native trajectories and observations; use authoritative Git diffs; bind disposable Docker copies and image digests. |
| D2 | 2026-07-10 | Require non-null usage/cost data, a pinned self-suite image, and execution/review provenance. |
| D3 | 2026-07-10 | Keep image identity outside the frozen config schema; propagate provider price registries into the child process; fail closed on missing usage or cost mismatch. |
| D4 | 2026-07-11 | Make thinking mode an immutable config field; fix the cumulative API anomaly tripwire at $28. |
| D5 | 2026-07-11 | Require explicit task-set files; freeze exact McNemar/tie handling; make verdict IDs deterministic and idempotent. |
| D6 | 2026-07-11 | Keep failure taxonomy rule-first and human-overridable; map abstract injections faithfully onto the native single-tool agent. |
| D7 | 2026-07-11 | Keep agent fault injection isolated from the verifier; disclose differing baselines and session lineage. |
| D8–D12 | 2026-07-11 | Qualify the M1 battery by mechanism, retire ineffective cognitive degradations into robustness findings, and freeze native tool-call, observation, read-only, sampling, and history implementations. |
| D13 | 2026-07-12 | Accept a 6-core/27.4-GiB evaluation host with concurrency capped at 2 because functional gates—not a sizing recommendation—define readiness. Public evidence omits host identity and operations. |
| D14 | 2026-07-12 | Correct CRLF-sensitive forbidden-path hashing, reverify all affected S6 runs, and append F-4 rather than rewriting prior findings. |
| D15 | 2026-07-12 | Close M1 after four technical gates; freeze the 16-task sampling method and explicit baseline-reuse disclosure. |
| D16–D18 | 2026-07-13/14 | Complete the 16-task DeepSeek baseline, audit candidate-model approval chains, freeze effective budget semantics, and reject unverifiable preview/gateway identities. |
| D19 | 2026-07-14 | Select MiMo V2.5 as the cross-provider candidate; correct currency conversion provenance; version the provider cost registry after F-5; authorize M3 only after strict reconciliation. |
| D20 | 2026-07-15 | Run M3 in three bounded arms: paired provider comparison, known I3Q regression, and second-agent compatibility; prohibit model-ranking claims from the compatibility arm. |
| D21 | 2026-07-16 | Publish TraceVerdict as a clean MIT-licensed mirror with a single genuine root release commit; keep the complete development history and raw evidence private. |
| D22 | 2026-07-16 | Authorize M4-S as a same-provider cross-tier robustness arm: DeepSeek V4 Pro uses explicit thinking mode, mirrors the frozen Flash request by intentionally omitting reasoning-effort and sampling parameters, gates a three-task probe against a conservative 19-run projection, then runs 16 tasks at k=1 (or the pre-registered first 12 before abandonment). Flash k=2 versus Pro k=1 is permitted only through an explicit asymmetric-comparison mode with baseline ties listed and excluded from McNemar. Kimi-3 remains dormant until an exact stable official model identity and auditable price exist. |
| D23 | 2026-07-16 | Record the completed M4-S arm without a model-ranking claim: Pro resolved 4/16 at k=1; versus Flash k=2, delta pass was -0.0625 with bootstrap 95% CI [-0.21875, 0.125] and exact McNemar p=1.0 after excluding four baseline ties. The comparison emitted warn, not hard, for delta pass and P95 wall time. All 16 Retrace/raw/aggregate verdicts agreed and all costs reconciled to the frozen registry, including explicitly itemized FormatError retries omitted by mini's native instance aggregate. |
| D24 | 2026-07-16 | Authorize M4-C as a Codex compatibility arm with `codex 0.144.4`, `gpt-5.6-luna`, high reasoning, and ChatGPT-subscription authentication. Subscription credentials are first-class secrets: the agent phase runs only in local WSL2/Docker Desktop and no credential byte may reach the rented verifier host. The host receives only a sanitized patch manifest and runs the unchanged official verifier image. Subscription spend is unallocatable per call, so `run.cost_usd` remains NULL; API-equivalent shadow cost is report-only and never enters the $28 real-API-spend tripwire. Codex JSONL is recorded honestly at turn-aggregate granularity, the CLI input hash is not represented as the hosted internal prompt hash, and unsupported mini budget dimensions are labelled inert rather than emulated. The Codex agent container uses the same Docker default bridge semantics as mini, while the agent layer and original verifier image remain distinct and fingerprinted. |

## Frozen governance rules

- Evidence corrections are appended; original private evidence is not overwritten.
- A configuration ID is immutable. Any behavior-affecting change creates a new ID.
- The task set precedes execution and is hashed from raw bytes.
- Paid execution needs written authorization and pre/post budget checks.
- A missing or contradictory fact stops execution; it is never guessed into compliance.
- Public release uses a machine-readable allowlist and a fail-closed safety scan.
