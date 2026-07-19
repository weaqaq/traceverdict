# Private Daily Tasks from Real Bug History

This is a documentation recipe, not an EVC-specific product feature. Private source code, bundles, tests, and task text must remain outside the public TraceVerdict mirror.

1. Select two or three already-fixed historical bugs whose fixes you understand and are allowed to evaluate privately.
2. For each fix commit, freeze its parent as the task base and create a Git bundle containing that base. Keep the bundle in a private suite directory.
3. Write FAIL_TO_PASS selectors that reproduce the bug and PASS_TO_PASS selectors that protect nearby behavior. Prefer tests that existed independently of the benchmark author; document any test you add solely for the task.
4. Freeze forbidden paths for migrations, fixtures, generated files, or other tempting shortcuts. Record base commit, image digest, budget bytes, and test selectors before any agent run.
5. Run the private suite through the same immutable-config, disposable-copy, authoritative-diff, verifier, and cached-baseline discipline. Do not copy private task artifacts into this public repository.

The point is to make a daily smoke test representative of your own engineering failure modes without weakening provenance or publishing proprietary history.
