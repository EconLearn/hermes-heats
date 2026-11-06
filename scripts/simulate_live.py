#!/usr/bin/env python3
"""Dev aid: write a sequence of fake run snapshots to <HERMES_HOME>/plugin-data/heats/live.json so the
Desktop pane can be checked without running agents. Never used by the plugin itself.
Usage: HERMES_HOME=... python scripts/simulate_live.py [--delay 1.5]"""
import argparse, json, os, time
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--delay", type=float, default=1.5); a = ap.parse_args()
live = Path(os.environ["HERMES_HOME"]) / "plugin-data" / "heats" / "live.json"
live.parent.mkdir(parents=True, exist_ok=True)
base = {"run_id": "sim-0000", "repo": "demo", "check": "python -m pytest -q", "n": 3,
        "baseline": {"ok": False, "passed": 2, "failed": 2}, "winner": None}
H = lambda i, st, **k: {"index": i, "strategy": ["minimal", "root-cause", "test-first"][i - 1], "model": None,
                        "status": st, "seconds": k.get("s", 0), "passed": k.get("p"), "failed": k.get("f"),
                        "check_ok": st in ("pass", "flagged"), "insertions": k.get("ins", 0), "deletions": k.get("dels", 0),
                        "files": k.get("files", 0), "guarded": st == "flagged", "qualifies": st == "pass",
                        "claimed_unchanged": k.get("claim", False)}
steps = [
    ("starting", [H(1, "queued"), H(2, "queued"), H(3, "queued")]),
    ("running", [H(1, "running"), H(2, "running"), H(3, "running")]),
    ("running", [H(1, "judging", s=48), H(2, "running"), H(3, "running")]),
    ("running", [H(1, "fail", s=48, p=3, f=1, files=1, ins=1, dels=1), H(2, "judging", s=71), H(3, "running")]),
    ("running", [H(1, "fail", s=48, p=3, f=1, files=1, ins=1, dels=1), H(2, "pass", s=71, p=4, f=0, files=1, ins=4, dels=2), H(3, "judging", s=90)]),
    ("done", [H(1, "fail", s=48, p=3, f=1, files=1, ins=1, dels=1), H(2, "pass", s=71, p=4, f=0, files=1, ins=4, dels=2), H(3, "fail", s=90, p=2, f=2, claim=True)]),
]
for phase, heats in steps:
    snap = dict(base, phase=phase, heats=heats)
    if phase == "done":
        snap["winner"] = 2
    live.write_text(json.dumps(snap)); print(phase); time.sleep(a.delay)
