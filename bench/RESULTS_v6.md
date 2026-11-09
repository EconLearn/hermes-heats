# Bench v6: plain Hermes vs Hermes + referee vs Heats race (n=1)

MEASURED. qwen3-4b-instruct-2507 Q4_K_M via Ollama on CPU (Azure E64as_v7, 64K context), Hermes main 90d6b98,
Heats v0.3.0 frozen (1e819e0). 10 small Python bug-fix tasks x 2 reps; the three arms run back to back for each
task (interleaved), so model speed drift hits all arms alike. Solved = the repo's tests pass in the user's
checkout after the run and the test files are unchanged. Single and referee arms get the same prompt; the
referee arm only switches the plugin's `referee` setting on. Time limits: 1200 s (single, referee), 1800 s (Heats).

| Arm | Solved | Mean s | Runs excluded (infra) |
|---|---|---|---|
| Plain Hermes | 8/20 | 217 | 0 |
| Hermes + Heats referee | 12/20 | 295 | 0 |
| Heats race, n=1 | 16/20 | 247 | 0 |

| Task | Plain Hermes | Hermes + Heats referee | Heats race, n=1 |
|---|---|---|---|
| t01_wordcount | 2/2 | 2/2 | 2/2 |
| t02_roman | 0/2 | 1/2 | 2/2 |
| t03_median | 2/2 | 2/2 | 2/2 |
| t04_slug | 1/2 | 0/2 | 2/2 |
| t05_dates | 0/2 | 1/2 | 0/2 |
| t06_lru | 1/2 | 1/2 | 2/2 |
| t07_parse_duration | 0/2 | 1/2 | 1/2 |
| t08_flatten | 2/2 | 2/2 | 2/2 |
| t09_csvsum | 0/2 | 1/2 | 1/2 |
| t10_intervals | 0/2 | 1/2 | 2/2 |

Paired (same task, same rep):

- Hermes + Heats referee vs Plain Hermes: 20 pairs; only referee solved 6, only single solved 2 (exact sign test p = 0.29)
- Heats race, n=1 vs Plain Hermes: 20 pairs; only heats1 solved 8, only single solved 0 (exact sign test p = 0.01)
- Hermes + Heats referee vs Heats race, n=1: 20 pairs; only referee solved 3, only heats1 solved 7 (exact sign test p = 0.34)

Referee: 34 checks run, 19 times the agent was sent back, in 8 of 20 runs; 2 of those runs ended solved.
Tests tampered in any solved run: 0.

Caveats: 20 runs per arm, one 4B model, small Python tasks; a difference of a few runs is within noise (see the sign-test p-values). This measures the mechanism on a weak local model, not a general benchmark.
