#!/bin/bash
# v7: replication of v6 on the ship version (v0.3.1), reps 3-5.
#   single  = plain Hermes (referee off), referee = plain Hermes + Heats referee (pre_verify), heats = Heats n=1.
cd "$(dirname "$0")"
export HERMES_HOME=$HOME/hm_src/home HERMES_API_TIMEOUT=1800 BENCH_MODEL_LABEL=qwen3-4b-instruct-2507-q4_K_M
PY=$HOME/hm_src/testvenv/bin/python
FROZEN=${FROZEN:?set FROZEN to the frozen plugin dir}
ln -sfn "$FROZEN" $HOME/hm_src/home/plugins/heats
grep -q 'pre_verify' $HOME/hm_src/home/plugins/heats/plugin.yaml || { echo "frozen plugin has no referee"; exit 1; }
$HOME/hm_src/venv/bin/hermes config set plugins.entries.heats.settings.check "$PY -m pytest -q -p no:cacheprovider"
for rep in 3 4 5; do
  for t in t01_wordcount t02_roman t03_median t04_slug t05_dates t06_lru t07_parse_duration t08_flatten t09_csvsum t10_intervals; do
    $PY run_bench.py --mode single --seeds 1 --timeout 1200 --tasks $t --out results_v7_single_r$rep.jsonl
    $PY run_bench.py --mode referee --seeds 1 --timeout 1200 --tasks $t --out results_v7_referee_r$rep.jsonl
    $PY run_bench.py --mode heats --n 1 --seeds 1 --timeout 1800 --tasks $t --out results_v7_heats1_r$rep.jsonl
  done
done
echo FULL_BENCH_V7_DONE
