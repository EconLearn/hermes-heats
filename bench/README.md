# Bench

Ten tiny Python repos with failing tests (`tasks/`). `run_bench.py` copies one into a scratch folder, runs
`hermes chat --oneshot` on it with a fixed prompt, and judges the result with the repo's own tests:
solved = tests pass in the checkout afterwards AND the test files are unchanged. The bench gateway is stopped
after every run. Arms: `single` (plain Hermes), `referee` (same prompt, Heats referee on), `heats` (the agent is
told to call `heats_run`).

| Files | Plugin | Status |
|---|---|---|
| `results/results_v6_*` | v0.3.0 | Current: three arms interleaved per task, 2 runs; `RESULTS_v6.md` (`python summarize_v6.py`) |
| `results/results_v5_*` | v0.2.2 | Valid: single vs Heats n=1 interleaved (11/20 vs 7/20); led to the v0.3.0 fixes |
| `results/results_v4_*`, `results/results_v3_heats_*` | v0.2.2 / v0.2.1 | Valid Heats n=3 runs; in v3, 7 of 20 runs never started a race (model called the tool from Python; fixed in v0.2.2) |
| `results/results_v2_s1.jsonl`, `results_v2_s2.jsonl` | v0.2.0 | Single rows valid for seed 1 (all) and seed 2 t05-t08; the Heats rows are INVALID (Hermes's 420 s tool limit cut the race off and the agent fixed the code itself; fixed in v0.2.1 with `heats_wait`) |
| `results/results_v3_single_s2.jsonl` | - | Valid single re-runs for seed 2 (t01-t04, t09, t10) |

Rows with `tunnel_drop: true` lost the model connection and are excluded by the summaries. Model: qwen3-4b-instruct-2507
Q4_K_M via Ollama, 64K context; v2-v5 on one RTX 4060, v6 on CPU (Azure E64as_v7). Paths in the result files were
shortened to `~`.
