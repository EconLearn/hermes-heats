#!/bin/bash
# v0.2.2 (misrouted-call guard): Heats arm, both seeds, all 10 tasks. Single rows reused from v2/v3.
cd "$(dirname "$0")"
export HERMES_HOME=$HOME/hm_src/home HERMES_API_TIMEOUT=1800 BENCH_MODEL_LABEL=qwen3-4b-instruct-2507-q4_K_M
PY=$HOME/hm_src/testvenv/bin/python
ln -sfn $HOME/hm_src/heats_frozen_v022 $HOME/hm_src/home/plugins/heats
grep -q '0.2.2' $HOME/hm_src/home/plugins/heats/plugin.yaml || { echo "frozen plugin is not v0.2.2"; exit 1; }
$PY run_bench.py --mode heats --n 3 --seeds 2 --timeout 2400 --out results_v4_heats.jsonl
echo FULL_BENCH_V4_DONE
