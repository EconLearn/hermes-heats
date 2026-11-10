# Bench v6 + v7 (replication)

MEASURED. qwen3-4b-instruct-2507 Q4_K_M via Ollama on CPU (Azure E64as_v7), Hermes main 90d6b98, 10 small Python
bug-fix tasks; the three arms run back to back per task. v6 = Heats v0.3.0 (reps 1-2), v7 = Heats v0.3.1 (reps 3-4; rep 5 cut for time;
same tasks, model and machine). Only cells where all three arms finished are counted.

| Arm | v6 | v7 | pooled |
|---|---|---|---|
| Plain Hermes | 8/20 | 14/20 | 22/40 |
| Hermes + Heats referee | 12/20 | 12/20 | 24/40 |
| Heats race, n=1 | 16/20 | 12/20 | 28/40 |

Paired, same task and rep (only A solved / only B solved, exact two-sided sign test):

- Hermes + Heats referee vs Plain Hermes, v6: 6 vs 2 (p = 0.29)
- Hermes + Heats referee vs Plain Hermes, v7: 2 vs 4 (p = 0.69)
- Hermes + Heats referee vs Plain Hermes, pooled: 8 vs 6 (p = 0.79)
- Heats race, n=1 vs Plain Hermes, v6: 8 vs 0 (p = 0.01)
- Heats race, n=1 vs Plain Hermes, v7: 1 vs 3 (p = 0.62)
- Heats race, n=1 vs Plain Hermes, pooled: 9 vs 3 (p = 0.15)

| Task | Plain Hermes v6 / v7 | Hermes + Heats referee v6 / v7 | Heats race, n=1 v6 / v7 |
|---|---|---|---|
| t01_wordcount | 2/2 / 2/2 | 2/2 / 1/2 | 2/2 / 2/2 |
| t02_roman | 0/2 / 2/2 | 1/2 / 2/2 | 2/2 / 2/2 |
| t03_median | 2/2 / 2/2 | 2/2 / 2/2 | 2/2 / 1/2 |
| t04_slug | 1/2 / 1/2 | 0/2 / 1/2 | 2/2 / 1/2 |
| t05_dates | 0/2 / 1/2 | 1/2 / 0/2 | 0/2 / 0/2 |
| t06_lru | 1/2 / 2/2 | 1/2 / 2/2 | 2/2 / 2/2 |
| t07_parse_duration | 0/2 / 1/2 | 1/2 / 0/2 | 1/2 / 0/2 |
| t08_flatten | 2/2 / 2/2 | 2/2 / 2/2 | 2/2 / 2/2 |
| t09_csvsum | 0/2 / 0/2 | 1/2 / 0/2 | 1/2 / 1/2 |
| t10_intervals | 0/2 / 1/2 | 1/2 / 2/2 | 2/2 / 1/2 |

v7 referee: 32 checks run, 20 send-backs in 8 of 20 runs.
Solved runs with edited tests, all arms, v6+v7: 0.
