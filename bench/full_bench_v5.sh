#!/bin/bash
# v5: compute-matched test. Single vs Heats n=1 (one isolated attempt + up to 3 laps of real test feedback),
# interleaved task by task so GPU drift hits both arms equally. Plugin v0.2.2 frozen.
cd "$(dirname "$0")"
export HERMES_HOME=$HOME/hm_src/home HERMES_API_TIMEOUT=1800 BENCH_MODEL_LABEL=qwen3-4b-instruct-2507-q4_K_M
PY=$HOME/hm_src/testvenv/bin/python
grep -q '0.2.2' $HOME/hm_src/home/plugins/heats/plugin.yaml || { echo "frozen plugin is not v0.2.2"; exit 1; }
for rep in 1 2; do
  for t in t01_wordcount t02_roman t03_median t04_slug t05_dates t06_lru t07_parse_duration t08_flatten t09_csvsum t10_intervals; do
    $PY run_bench.py --mode single --seeds 1 --timeout 1200 --tasks $t --out results_v5_single_r$rep.jsonl
    $PY run_bench.py --mode heats --n 1 --seeds 1 --timeout 1800 --tasks $t --out results_v5_heats1_r$rep.jsonl
  done
done
echo FULL_BENCH_V5_DONE
