#!/usr/bin/env python3
"""Summarise bench results: per task, single attempt vs Heats; writes RESULTS.md next to this file.
Usage: summarize.py results_full_s1.jsonl [results_full_s2.jsonl ...]"""
import json
import sys
from collections import defaultdict
from pathlib import Path

rows = []
for f in sys.argv[1:]:
    for line in open(f):
        r = json.loads(line)
        r["_file"] = Path(f).name
        rows.append(r)
dropped = [r for r in rows if r.get("tunnel_drop")]
rows = [r for r in rows if not r.get("tunnel_drop")]

tasks = sorted({r["task"] for r in rows})
by = defaultdict(list)
for r in rows:
    by[(r["task"], r["mode"])].append(r)

def rate(rs):
    return sum(r["solved"] for r in rs), len(rs)

out = ["# Bench results: single attempt vs Heats-3", "",
       "MEASURED. Model: qwen3-4b-instruct-2507 (Q4_K_M, Ollama, 64K context) on one RTX 4060. Hermes main (90d6b98).",
       "Each task: a tiny repo with failing tests; solved = the repo's tests pass in the user's checkout after the run",
       "AND the test files are unchanged. Single = plain Hermes told to fix it. Heats = Hermes told to call heats_run(n=3)",
       "then heats_apply. Plugin v0.2.2 (frozen copy): 3 attempts, up to 3 laps each, first clean pass ends the race.",
       "Time limits: single 1200 s per run; Heats 2400 s per run (attempts get 15 min). The GPU serves one request",
       "at a time, so Heats attempts queue behind each other. The bench gateway is stopped after every run and runs",
       f"during which the model tunnel dropped are re-run ({len(dropped)} such rows excluded).", "",
       "| Task | Single solved | Heats solved | Heats: attempts passing | Single s (mean) | Heats s (mean) |",
       "|---|---|---|---|---|---|"]
tot = defaultdict(lambda: [0, 0])
att = [0, 0]
for t in tasks:
    s, h = by[(t, "single")], by[(t, "heats")]
    ss, sn = rate(s)
    hs, hn = rate(h)
    tot["single"][0] += ss; tot["single"][1] += sn
    tot["heats"][0] += hs; tot["heats"][1] += hn
    ap = [sum(x["pass"] for x in (r.get("heats") or [])) for r in h]
    an = [len(r.get("heats") or []) for r in h]
    att[0] += sum(ap); att[1] += sum(an)
    ms = lambda rs: f"{sum(r['seconds'] for r in rs) / len(rs):.0f}" if rs else "-"
    out.append(f"| {t} | {ss}/{sn} | {hs}/{hn} | {', '.join(f'{a}/{b}' for a, b in zip(ap, an)) or '-'} | {ms(s)} | {ms(h)} |")
out += ["", f"**Total: single {tot['single'][0]}/{tot['single'][1]} runs solved; Heats-3 {tot['heats'][0]}/{tot['heats'][1]}.**",
        f"Individual Heats attempts that passed the check: {att[0]}/{att[1]} "
        f"({100 * att[0] / max(1, att[1]):.0f}%, a per-attempt rate comparable to a single try).",
        f"Heats runs in which a race actually started (the agent called heats_run): "
        f"{sum(bool(r.get('heats')) for r in rows if r['mode'] == 'heats')}/{tot['heats'][1]}.",
        f"Tests tampered in any solved run: {sum(r['tests_tampered'] for r in rows if r['solved'])}.", "",
        "Caveats: 10 small Python tasks, 2 seeds each; one local 4B model; NUM_PARALLEL=1 so Heats attempts share the GPU",
        "(wall time is not optimised). This shows the mechanism on a weak model, not a general benchmark."]
Path(__file__).with_name("RESULTS.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
