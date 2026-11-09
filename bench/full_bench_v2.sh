#!/bin/bash
# Clean bench for v0.2.0: single then heats(n=3), all 10 tasks, 2 seeds. Waits for the model tunnel before
# every run; stops the bench gateway after every run; re-runs any run the tunnel dropped under.
cd "$(dirname "$0")"
export HERMES_HOME=$HOME/hm_src/home HERMES_API_TIMEOUT=1800 BENCH_MODEL_LABEL=qwen3-4b-instruct-2507-q4_K_M
PY=$HOME/hm_src/testvenv/bin/python
for seed in 1 2; do
  $PY run_bench.py --mode single --seeds 1 --timeout 1200 --out results_v2_s$seed.jsonl
  $PY run_bench.py --mode heats --n 3 --seeds 1 --timeout 2400 --out results_v2_s$seed.jsonl
done
echo FULL_BENCH_V2_DONE
