#!/usr/bin/env python3
"""Bench: single attempt vs Heats on bench/tasks/*, inside a real Hermes install.

Each task is a tiny repo with failing tests. For every (task, mode, seed) we copy it to a
scratch dir, git-init it, run `hermes chat --oneshot`, then judge the MAIN checkout with
the hidden-from-nothing test command (tests are visible; editing them counts as a fail).

  single : "fix it" prompt, plain Hermes, one agent.
  heats  : the agent is told to call heats_run(n=N) then heats_apply on the winner.

Usage: run_bench.py --mode single|heats [--n 3] [--tasks t01,t02] [--seeds 1] [--out results.jsonl]
Env: HERMES_HOME (test home), HERMES_BIN, PYTEST_PY (python with pytest).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks"
HERMES = os.environ.get("HERMES_BIN", str(Path.home() / "hm_src/venv/bin/hermes"))
PYTEST_PY = os.environ.get("PYTEST_PY", str(Path.home() / "hm_src/testvenv/bin/python"))
CHECK = f"{PYTEST_PY} -m pytest -q -p no:cacheprovider"
SCRATCH = Path(os.environ.get("BENCH_SCRATCH", str(Path.home() / "hm_src/bench_scratch")))


def sh(cmd, cwd, timeout=None):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)


def bench_gateways() -> list[int]:
    """PIDs of the on-demand gateway `hermes chat` starts for the bench home (it runs the agent)."""
    r = subprocess.run(["pgrep", "-f", "gateway run --quiet --idle-exit"], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def stop_gateway() -> None:
    """`hermes chat --oneshot` runs its agent inside the gateway, so killing the client on a timeout
    leaves the agent running (measured 6 Nov: one leaked for 3.4 h and shared the GPU with every later
    run). Stop the gateway after every run, and refuse to continue if it will not die."""
    subprocess.run([HERMES, "gateway", "stop"], capture_output=True, text=True, timeout=120)
    for _ in range(30):
        if not bench_gateways():
            return
        time.sleep(1)
    for pid in bench_gateways():
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    time.sleep(2)
    if bench_gateways():
        raise SystemExit(f"bench gateway will not stop: {bench_gateways()}")


KEEP_LOG = Path.home() / "hm_src/ollama_keep.log"
MODEL_URL = os.environ.get("BENCH_MODEL_URL", "http://localhost:11434/api/tags")


def tunnel_drops() -> int:
    try:
        return KEEP_LOG.read_text(errors="replace").count("tunnel exited")
    except OSError:
        return 0


def wait_for_model(max_s: float = 6 * 3600) -> None:
    """Block until the model server answers (the tunnel to the GPU PC can be down for hours)."""
    import urllib.request
    end = time.time() + max_s
    while time.time() < end:
        try:
            urllib.request.urlopen(MODEL_URL, timeout=10).read()
            return
        except Exception:
            time.sleep(30)
    raise SystemExit("model server unreachable for too long")


def tests_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted((d / "tests").rglob("*.py")):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


def prepare(task: str, tag: str) -> Path:
    dst = SCRATCH / f"{task}-{tag}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(TASKS / task, dst, ignore=shutil.ignore_patterns("TASK.txt", "__pycache__"))
    sh("git init -q && git -c user.email=b@b -c user.name=bench add -A && "
       "git -c user.email=b@b -c user.name=bench commit -qm task", dst)
    return dst


def judge(d: Path, before_hash: str) -> dict:
    r = sh(CHECK, d, timeout=300)
    tampered = tests_hash(d) != before_hash
    return {"check_exit": r.returncode, "tests_tampered": tampered,
            "solved": r.returncode == 0 and not tampered, "tail": r.stdout[-300:]}


def run_one(task: str, mode: str, n: int, seed: int, timeout_s: int) -> dict:
    text = (TASKS / task / "TASK.txt").read_text().strip()
    d = prepare(task, f"{mode}{n if mode == 'heats' else ''}-s{seed}")
    h0 = tests_hash(d)
    if mode in ("single", "referee"):
        # the plugin's referee (pre_verify hook) is ON only in the "referee" arm
        subprocess.run([HERMES, "config", "set", "plugins.entries.heats.settings.referee",
                        "true" if mode == "referee" else "false"], capture_output=True, text=True, timeout=60)
        prompt = (f"{text}\nThe check is `{CHECK}`; it must exit 0. Do not edit the tests. "
                  "Fix the code in this repository now, verify with the check, then stop.")
    else:
        prompt = (f"Call the heats_run tool with n={n}, check='{CHECK}' and this task: \"{text} Do not edit the tests.\" "
                  "If it returns status running, call heats_wait with its run_id until the status is done. "
                  "Then, if there is a winner, call heats_apply with the run_id. Then stop.")
    if bench_gateways():
        stop_gateway()
    wait_for_model()
    drops0 = tunnel_drops()
    t = time.time()
    env = dict(os.environ)
    try:
        p = subprocess.run([HERMES, "chat", "--oneshot", "-Q", "-q", prompt], cwd=d, env=env,
                           capture_output=True, text=True, timeout=timeout_s)
        out, rc = (p.stdout + p.stderr), p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = f"TIMEOUT after {timeout_s}s", -9
    dur = round(time.time() - t, 1)
    stop_gateway()
    infra = "message was not sent" in out or "couldn't start the model connection" in out
    res = {"tunnel_drop": tunnel_drops() != drops0 or infra, "infra_error": infra, "task": task, "mode": mode, "n": n if mode == "heats" else 1, "seed": seed, "hermes_rc": rc,
           "seconds": dur, **judge(d, h0), "hermes_tail": out[-600:]}
    if mode == "referee":
        log = Path(os.environ["HERMES_HOME"]) / "plugin-data/heats/referee.jsonl"
        recs = []
        if log.exists():
            for line in log.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= t - 5 and any(os.path.realpath(x).startswith(os.path.realpath(d)) for x in r.get("paths", [])):
                    recs.append(r)
        res["referee_calls"] = len(recs)
        res["referee_nudges"] = sum(bool(r.get("nudged")) for r in recs)
    if mode == "heats":
        runs = sorted((Path(os.environ["HERMES_HOME"]) / "plugin-data/heats/runs").glob("*/report.json"),
                      key=lambda p: p.stat().st_mtime)
        mine = [json.loads(p.read_text()) for p in runs]
        mine = [r for r in mine if os.path.realpath(r["root"]) == os.path.realpath(d)
                and r.get("started_at", 0) >= t - 5]   # only a race started by THIS run
        if mine:
            rep = mine[-1]
            res["run_id"] = rep["run_id"]
            res["winner"] = rep["winner"]
            res["heats"] = [{"i": h["index"], "pass": bool(h["check"] and h["check"]["ok"]),
                             "qualifies": h["qualifies"], "guarded": h["guarded"], "state": h["agent_state"],
                             "escaped": h["escaped"], "s": h["duration_s"]} for h in rep["heats"]]
        else:
            res["run_id"] = None
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["single", "referee", "heats"], required=True)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out", default=str(HERE / "results.jsonl"))
    a = ap.parse_args()
    tasks = a.tasks.split(",") if a.tasks else sorted(p.name for p in TASKS.iterdir() if p.is_dir())
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for seed in range(1, a.seeds + 1):
        for t in tasks:
            for attempt in range(3):   # a run the tunnel dropped under is not a measurement: redo it
                r = run_one(t, a.mode, a.n, seed, a.timeout)
                if not r["tunnel_drop"]:
                    break
                print(f"{t} {a.mode} seed{seed}: tunnel drop or infra error; retrying", flush=True)
                time.sleep(30)
            r["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            r["model"] = os.environ.get("BENCH_MODEL_LABEL", "")
            with open(a.out, "a") as fh:
                fh.write(json.dumps(r) + "\n")
            print(f"{t} {a.mode} seed{seed}: solved={r['solved']} tampered={r['tests_tampered']} "
                  f"{r['seconds']}s winner={r.get('winner')} heats={r.get('heats')}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
