#!/usr/bin/env python3
"""Bench v6 + v7 summary. v6 = Heats v0.3.0 reps 1-2; v7 = Heats v0.3.1 reps 3-5 (replication, same tasks, model, VM).
Counts only (task, rep) cells where all three arms finished. Writes RESULTS_v7.md."""
import json, re
from math import comb
from pathlib import Path
HERE = Path(__file__).resolve().parent
ARMS = [("single", "Plain Hermes"), ("referee", "Hermes + Heats referee"), ("heats1", "Heats race, n=1")]

def load(ver):
    d = {a: {} for a, _ in ARMS}
    for f in HERE.glob(f"results_{ver}_*_r*.jsonl"):
        arm, rep = re.match(rf"results_{ver}_(\w+?)_r(\d+)", f.name).groups()
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if not r.get("tunnel_drop"):
                d[arm][(r["task"], int(rep))] = r
    keys = set.intersection(*[set(v) for v in d.values()])
    return {a: {k: d[a][k] for k in keys} for a in d}, keys

def sign_p(a, b):
    n = a + b
    return 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, i) for i in range(min(a, b) + 1)) / 2 ** n)

v6, k6 = load("v6"); v7, k7 = load("v7")
pool = {a: {**{("v6",) + k: r for k, r in v6[a].items()}, **{("v7",) + k: r for k, r in v7[a].items()}} for a, _ in ARMS}
out = ["# Bench v6 + v7 (replication)", "",
       "MEASURED. qwen3-4b-instruct-2507 Q4_K_M via Ollama on CPU (Azure E64as_v7), Hermes main 90d6b98, 10 small Python",
       "bug-fix tasks; the three arms run back to back per task. v6 = Heats v0.3.0 (reps 1-2), v7 = Heats v0.3.1 (reps 3-4; rep 5 cut for time;",
       "same tasks, model and machine). Only cells where all three arms finished are counted.", "",
       "| Arm | v6 | v7 | pooled |", "|---|---|---|---|"]
for a, name in ARMS:
    c = lambda d: f"{sum(r['solved'] for r in d.values())}/{len(d)}"
    out.append(f"| {name} | {c(v6[a])} | {c(v7[a])} | {c(pool[a])} |")
out += ["", "Paired, same task and rep (only A solved / only B solved, exact two-sided sign test):", ""]
for a, b in (("referee", "single"), ("heats1", "single")):
    for lab, d in (("v6", v6), ("v7", v7), ("pooled", pool)):
        wa = sum(d[a][k]["solved"] and not d[b][k]["solved"] for k in d[a])
        wb = sum(d[b][k]["solved"] and not d[a][k]["solved"] for k in d[a])
        out.append(f"- {dict(ARMS)[a]} vs {dict(ARMS)[b]}, {lab}: {wa} vs {wb} (p = {sign_p(wa, wb):.2f})")
tasks = sorted({k[0] for k in k6 | k7})
out += ["", "| Task | " + " | ".join(f"{n} v6 / v7" for _, n in ARMS) + " |", "|---|" + "---|" * len(ARMS)]
for t in tasks:
    cells = []
    for a, _ in ARMS:
        f = lambda d: f"{sum(r['solved'] for k, r in d[a].items() if k[0] == t)}/{sum(1 for k in d[a] if k[0] == t)}"
        cells.append(f"{f(v6)} / {f(v7)}")
    out.append(f"| {t} | " + " | ".join(cells) + " |")
ref = list(v7["referee"].values())
out += ["", f"v7 referee: {sum(r.get('referee_calls', 0) for r in ref)} checks run, "
        f"{sum(r.get('referee_nudges', 0) for r in ref)} send-backs in {sum(1 for r in ref if r.get('referee_nudges', 0))} of {len(ref)} runs.",
        f"Solved runs with edited tests, all arms, v6+v7: {sum(r['solved'] and r.get('tests_tampered') for a in pool for r in pool[a].values())}."]
(HERE / "RESULTS_v7.md").write_text("\n".join(out) + "\n"); print("\n".join(out))
