# TraceVerdict comparison `cmp-6d73922dc667`

- Baseline: `dev-deepseek-v4-flash-v2`
- Candidate: `m3-mini-2-4-5-deepseek-v4-flash-thinking-i3q-v1`
- Task set SHA256: `eeb30802a00dd865cfc7214dc6ab123f267f1f92b9e5d3a4d21869bf4fb152be`
- Tasks: 16; repetitions: `{"astropy__astropy-14369": 2, "django__django-14376": 2, "django__django-16315": 2, "matplotlib__matplotlib-22871": 2, "matplotlib__matplotlib-25960": 2, "psf__requests-1142": 2, "pydata__xarray-3677": 2, "pydata__xarray-7229": 2, "pytest-dev__pytest-7490": 2, "pytest-dev__pytest-7982": 2, "scikit-learn__scikit-learn-12682": 2, "scikit-learn__scikit-learn-25102": 2, "sphinx-doc__sphinx-8593": 2, "sphinx-doc__sphinx-8721": 2, "sympy__sympy-17318": 2, "sympy__sympy-20438": 2}`
- Alarm: **hard**

## Paired pass statistics

- Delta pass: -0.3125
- Bootstrap: 10000 resamples, seed 20260710
- 95% CI: [-0.5, -0.125]
- McNemar cells: both_pass=0, baseline_only=3, candidate_only=0, both_fail=9
- Exact two-sided McNemar p: 0.25
- Excluded no-majority ties (4): django__django-14376, matplotlib__matplotlib-25960, pydata__xarray-3677, pytest-dev__pytest-7490

## Cost and performance

- Tokens: `{"baseline_median": 1130157.75, "baseline_p95": 2723514.875, "candidate_median": 1907089.5, "candidate_p95": 3484180.125, "median_ratio": 1.6874542514087083}`
- Cost USD: `{"baseline_median": 0.010189011, "baseline_p95": 0.021701092000000005, "candidate_median": 0.014252510999999999, "candidate_p95": 0.0256821397}`
- Wall time: `{"baseline_median": 356.8078735142567, "baseline_p95": 596.5893335869984, "candidate_median": 310.67522996574917, "candidate_p95": 615.4866581763763, "p95_ratio": 1.031675599152532}`
- New forbidden violations: none
- Warn reasons: delta_pass, median_tokens

## Failure taxonomy

- tool_misuse: 0
- context_loss: 0
- hallucinated_api: 0
- loop: 0
- budget: 11
- other: 21

- `run-52b9df00610a`: rule=other, manual=none, final=other (rule)
- `run-b46c7c75e484`: rule=other, manual=none, final=other (rule)
- `run-70f6c824c8ce`: rule=budget, manual=none, final=budget (rule)
- `run-a11fbd016f74`: rule=budget, manual=none, final=budget (rule)
- `run-791cda440333`: rule=budget, manual=none, final=budget (rule)
- `run-52687906ad01`: rule=other, manual=none, final=other (rule)
- `run-57d5e337a659`: rule=other, manual=none, final=other (rule)
- `run-dc6cc419d0e3`: rule=budget, manual=none, final=budget (rule)
- `run-441a46872dea`: rule=other, manual=none, final=other (rule)
- `run-e3ff272ea236`: rule=other, manual=none, final=other (rule)
- `run-3a783a6243ea`: rule=other, manual=none, final=other (rule)
- `run-dd7e916dcfdc`: rule=budget, manual=none, final=budget (rule)
- `run-4b3daee91085`: rule=other, manual=none, final=other (rule)
- `run-fdbf7772495c`: rule=other, manual=none, final=other (rule)
- `run-585ab82d01b2`: rule=other, manual=none, final=other (rule)
- `run-430d542bc639`: rule=other, manual=none, final=other (rule)
- `run-a39fe8c48891`: rule=other, manual=none, final=other (rule)
- `run-e339c1e8a5d7`: rule=other, manual=none, final=other (rule)
- `run-119c42f9dc21`: rule=other, manual=none, final=other (rule)
- `run-450e41b4fc6c`: rule=other, manual=none, final=other (rule)
- `run-4c86d6d3a9dc`: rule=other, manual=none, final=other (rule)
- `run-47e830473e0e`: rule=budget, manual=none, final=budget (rule)
- `run-155ca83392f4`: rule=other, manual=none, final=other (rule)
- `run-bcc26acf6db7`: rule=other, manual=none, final=other (rule)
- `run-631e9b6d8cf8`: rule=budget, manual=none, final=budget (rule)
- `run-ca0c5fd67a42`: rule=other, manual=none, final=other (rule)
- `run-e75ccab78941`: rule=budget, manual=none, final=budget (rule)
- `run-aa8c9c4b238d`: rule=budget, manual=none, final=budget (rule)
- `run-8d4a76fc2880`: rule=other, manual=none, final=other (rule)
- `run-a2013edfa477`: rule=other, manual=none, final=other (rule)
- `run-ecb0da7ff03e`: rule=budget, manual=none, final=budget (rule)
- `run-1dab8854c929`: rule=budget, manual=none, final=budget (rule)
