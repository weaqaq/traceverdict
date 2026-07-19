# S1 verify materials

Ground-truth track (rule): FAIL_TO_PASS on `tests/test_calc.py`.

After the agent run, apply the authoritative patch from the work copy and run:

```bash
pytest tests/test_calc.py -q
```

Expected after a correct fix: both tests pass.
