# Publication provenance

TraceVerdict is a clean public mirror of an accepted private audit snapshot.

- Private snapshot commit: `c4f80bba4ea1886b4901f4dceca3ff81e6f526f1`.
- Public release: the commit pointed to by annotated tag `v0.1.0`.
- Version: `0.1.0`.
- History policy: one genuine public root release commit; no reconstructed or replayed development history.
- Commit identity: GitHub handle `weaqaq` with a GitHub noreply address.

A commit cannot contain its own hash without changing that hash. Therefore the exact public root is resolved mechanically with `git rev-parse v0.1.0^{commit}` and is also reported in the release handoff. The annotated tag and CI bind the published tree.

## Included

Product source, public tests, self-suite fixture bundles, frozen benchmark IDs and sampling metadata, required model/config examples, the sanitized PRD and decisions, F-1–F-5, and aggregate M3 evidence.

## Deliberately excluded

Raw T5 evidence, server initialization notes/scripts, databases, full trajectories and prompts, provider logs, local/remote absolute paths, infrastructure addresses, credential-file references, private build artifacts, and private Git objects.

The private audit repository remains private and unchanged. Its complete per-commit history and raw artifacts can be inspected in a controlled interview setting.
