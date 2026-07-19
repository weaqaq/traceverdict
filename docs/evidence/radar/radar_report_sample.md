# Radar report sample (zero-model synthetic fixture)

This screen is produced from the deterministic multi-tick test fixture; it is not presented as a paid monitoring run.

```text
TraceVerdict Radar - last 7 days
+---------------+--------------------+-----+-------+------------+----------+--------+------------+
| tick          | config             | rep | pass  | tokens med | wall p95 | level  | cost USD   |
+---------------+--------------------+-----+-------+------------+----------+--------+------------+
| tick-baseline | daily-radar-config | 0   | 1.000 | 100        | 10.00    | clean  | 0.03000000 |
| tick-signal   | daily-radar-config | 1   | 1.000 | 200        | 10.00    | signal | 0.03000000 |
+---------------+--------------------+-----+-------+------------+----------+--------+------------+
signals=1 month=$0.060000/$3.00 project=$1.060000/$28
```

The matching confirmation fixture adds two runs only for each signalled task. A reproduced token warning becomes `confirmed` and exits 1; a non-reproduction becomes `withdrawn` and exits 0.
