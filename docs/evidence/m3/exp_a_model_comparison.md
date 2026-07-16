# TraceVerdict comparison `cmp-71b0f6a50b2d`

- Baseline: `dev-deepseek-v4-flash-v2`
- Candidate: `probe-mimo-v2-5-thinking-v3`
- Task set SHA256: `eeb30802a00dd865cfc7214dc6ab123f267f1f92b9e5d3a4d21869bf4fb152be`
- Tasks: 16; repetitions: `{"astropy__astropy-14369": 2, "django__django-14376": 2, "django__django-16315": 2, "matplotlib__matplotlib-22871": 2, "matplotlib__matplotlib-25960": 2, "psf__requests-1142": 2, "pydata__xarray-3677": 2, "pydata__xarray-7229": 2, "pytest-dev__pytest-7490": 2, "pytest-dev__pytest-7982": 2, "scikit-learn__scikit-learn-12682": 2, "scikit-learn__scikit-learn-25102": 2, "sphinx-doc__sphinx-8593": 2, "sphinx-doc__sphinx-8721": 2, "sympy__sympy-17318": 2, "sympy__sympy-20438": 2}`
- Alarm: **warn**

## Paired pass statistics

- Delta pass: -0.09375
- Bootstrap: 10000 resamples, seed 20260710
- 95% CI: [-0.21875, 0.03125]
- McNemar cells: both_pass=3, baseline_only=0, candidate_only=0, both_fail=8
- Exact two-sided McNemar p: 1
- Excluded no-majority ties (5): django__django-14376, matplotlib__matplotlib-25960, pydata__xarray-3677, pytest-dev__pytest-7490, scikit-learn__scikit-learn-25102

## Cost and performance

- Tokens: `{"baseline_median": 1130157.75, "baseline_p95": 2723514.875, "candidate_median": 925219.0, "candidate_p95": 3681043.0, "median_ratio": 0.8186635892201775}`
- Cost USD: `{"baseline_median": 0.010189011, "baseline_p95": 0.021701092000000005, "candidate_median": 0.010546089151911976, "candidate_p95": 0.03121061410832591}`
- Wall time: `{"baseline_median": 356.8078735142567, "baseline_p95": 596.5893335869984, "candidate_median": 306.29364343874886, "candidate_p95": 937.2906746121257, "p95_ratio": 1.5710818511900937}`
- New forbidden violations: none
- Warn reasons: delta_pass, p95_wall

## Failure taxonomy

- tool_misuse: 0
- context_loss: 0
- hallucinated_api: 0
- loop: 0
- budget: 1
- other: 24

- `run-2f777984344f`: rule=other, manual=none, final=other (rule)
- `run-3cc0193d15fa`: rule=other, manual=none, final=other (rule)
- `run-a370ff1f7fb9`: rule=other, manual=none, final=other (rule)
- `run-ff638921c93b`: rule=other, manual=none, final=other (rule)
- `run-e65acf4995b7`: rule=other, manual=none, final=other (rule)
- `run-c054002b8a85`: rule=other, manual=none, final=other (rule)
- `run-036340dd98e5`: rule=other, manual=none, final=other (rule)
- `run-d561a0ca719f`: rule=other, manual=none, final=other (rule)
- `run-b3e5854cba38`: rule=other, manual=none, final=other (rule)
- `run-18ce71f4ee34`: rule=other, manual=none, final=other (rule)
- `run-f2e2ca2eef51`: rule=other, manual=none, final=other (rule)
- `run-ba449ecef1e1`: rule=other, manual=none, final=other (rule)
- `run-29266802043b`: rule=other, manual=none, final=other (rule)
- `run-ba949c93d0e4`: rule=other, manual=none, final=other (rule)
- `run-3be6de2cac67`: rule=other, manual=none, final=other (rule)
- `run-3034450243ca`: rule=other, manual=none, final=other (rule)
- `run-edd1dc8da610`: rule=other, manual=none, final=other (rule)
- `run-0d74513451b4`: rule=other, manual=none, final=other (rule)
- `run-f8ece7e738c9`: rule=budget, manual=none, final=budget (rule)
- `run-4331547d00b5`: rule=other, manual=none, final=other (rule)
- `run-8b9e2d8f9747`: rule=other, manual=none, final=other (rule)
- `run-8ad41de54184`: rule=other, manual=none, final=other (rule)
- `run-463af591826d`: rule=other, manual=none, final=other (rule)
- `run-7d93661e6a25`: rule=other, manual=none, final=other (rule)
- `run-b8077f5c7c7c`: rule=other, manual=none, final=other (rule)
