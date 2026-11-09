#!/usr/bin/env python3
"""Bench v6 summary: single vs single+referee vs Heats n=1, interleaved per task, 2 reps. Writes RESULTS_v6.md.
Paired comparison per (task, rep): wins/losses and an exact two-sided sign test on the discordant pairs."""
import json
from collections import defaultdict
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = [("single", "Plain Hermes"), ("referee", "Hermes + Heats referee"), ("heats1", "Heats race, n=1")]


def load(arm):
    rows = {}
    dropped = 0
    for rep in (1, 2):
        f = HERE / "results" / f"results_v6_{arm}_r{rep}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get("tunnel_drop"):
                dropped += 1
                continue
            rows[(r["task"], rep)] = r
    return rows, dropped


def sign_p(a, b):
    n = a + b
    if n == 0:
        return 1.0
    k = min(a, b)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


data = {a: load(a) for a, _ in ARMS}
keys = sorted(set().union(*[set(d[0]) for d in data.values()]))
tasks = sorted({k[0] for k in keys})
out = ["# Bench v6: plain Hermes vs Hermes + referee vs Heats race (n=1)", "",
       "MEASURED. qwen3-4b-instruct-2507 Q4_K_M via Ollama on CPU (Azure E64as_v7, 64K context), Hermes main 90d6b98,",
       "Heats v0.3.0 frozen (1e819e0). 10 small Python bug-fix tasks x 2 reps; the three arms run back to back for each",
       "task (interleaved), so model speed drift hits all arms alike. Solved = the repo's tests pass in the user's",
       "checkout after the run and the test files are unchanged. Single and referee arms get the same prompt; the",
       "referee arm only switches the plugin's `referee` setting on. Time limits: 1200 s (single, referee), 1800 s (Heats).", ""]
out += ["| Arm | Solved | Mean s | Runs excluded (infra) |", "|---|---|---|---|"]
for a, name in ARMS:
    rows, dropped = data[a]
    rs = list(rows.values())
    s = sum(r["solved"] for r in rs)
    ms = sum(r["seconds"] for r in rs) / max(1, len(rs))
    out.append(f"| {name} | {s}/{len(rs)} | {ms:.0f} | {dropped} |")
out += ["", "| Task | " + " | ".join(n for _, n in ARMS) + " |", "|---|" + "---|" * len(ARMS)]
for t in tasks:
    cells = []
    for a, _ in ARMS:
        rows = data[a][0]
        v = [rows[(t, rep)]["solved"] for rep in (1, 2) if (t, rep) in rows]
        cells.append(f"{sum(v)}/{len(v)}")
    out.append(f"| {t} | " + " | ".join(cells) + " |")
out += ["", "Paired (same task, same rep):", ""]
for a, b in (("referee", "single"), ("heats1", "single"), ("referee", "heats1")):
    ra, rb = data[a][0], data[b][0]
    both = [k for k in ra if k in rb]
    wa = sum(ra[k]["solved"] and not rb[k]["solved"] for k in both)
    wb = sum(rb[k]["solved"] and not ra[k]["solved"] for k in both)
    out.append(f"- {dict(ARMS)[a]} vs {dict(ARMS)[b]}: {len(both)} pairs; only {a} solved {wa}, only {b} solved {wb} "
               f"(exact sign test p = {sign_p(wa, wb):.2f})")
ref = list(data["referee"][0].values())
calls = sum(r.get("referee_calls", 0) for r in ref)
nudges = sum(r.get("referee_nudges", 0) for r in ref)
nudged_runs = [r for r in ref if r.get("referee_nudges", 0)]
out += ["", f"Referee: {calls} checks run, {nudges} times the agent was sent back, in {len(nudged_runs)} of {len(ref)} runs; "
        f"{sum(r['solved'] for r in nudged_runs)} of those runs ended solved.",
        f"Tests tampered in any solved run: {sum(r['solved'] and r['tests_tampered'] for a in data for r in data[a][0].values())}.",
        "", "Caveats: 20 runs per arm, one 4B model, small Python tasks; a difference of a few runs is within noise "
        "(see the sign-test p-values). This measures the mechanism on a weak local model, not a general benchmark."]
(HERE / "RESULTS_v6.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
