#!/bin/bash
# v0.2.1 (heats_wait) rerun: Heats arm for both seeds, plus the 6 seed-2 single runs that hit
# "Gateway starting: deadline" in v2. Valid v2 single rows are reused by summarize.
cd "$(dirname "$0")"
export HERMES_HOME=$HOME/hm_src/home HERMES_API_TIMEOUT=1800 BENCH_MODEL_LABEL=qwen3-4b-instruct-2507-q4_K_M
PY=$HOME/hm_src/testvenv/bin/python
$PY run_bench.py --mode single --seeds 1 --timeout 1200 --tasks t01_wordcount,t02_roman,t03_median,t04_slug,t09_csvsum,t10_intervals --out results_v3_single_s2.jsonl
for seed in 1 2; do
  $PY run_bench.py --mode heats --n 3 --seeds 1 --timeout 2400 --out results_v3_heats_s$seed.jsonl
done
echo FULL_BENCH_V3_DONE
