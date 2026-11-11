"""Run N attempts of one task in parallel git worktrees and judge them with the repo's check.

The runner does not import Hermes. It talks to the agent through a small ``Launcher``
protocol (launch / poll / cancel / result), so tests drive it with a fake and the plugin
adapts Hermes's public subagent lifecycle API to it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import gitops
from .checks import cannot_run, check_defining_edits, CheckResult, Guard, detect_check, run_check
from .isolation import IsolationMap, Lane
from .scoring import HeatResult, rank, scoreboard, winner

MAX_HEATS = 8
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED", "UNKNOWN"}

DEFAULT_STRATEGIES = [
    ("standard", "Fix the real cause of the failure the check shows; rewrite the faulty code if that is cleaner than patching around it."),
    ("root-cause", "Read the relevant code first and find the root cause, then fix that cause properly."),
    ("minimal", "Make the smallest change that fixes the problem. Do not refactor."),
    ("fresh-eyes", "Do not assume the obvious culprit; list two possible causes, check both, then fix the real one."),
]
CANNOT_RUN_EXIT_CODES = (126, 127)  # shell: not executable / command not found



def _live_seconds(h) -> float:
    """Elapsed seconds for the live view: the final duration once known, else time since the heat (re)started."""
    t = getattr(h, "_t0", None)
    if getattr(h, "_status", "") == "running" and t is not None:
        return round(time.monotonic() - t, 1)
    return h.duration_s

class Launcher(Protocol):
    def launch(self, *, goal: str, context: str, model: Optional[str], key: str) -> Any: ...
    def task_keys(self, handle: Any) -> list[str]: ...
    def state(self, handle: Any, wait_s: float) -> str: ...
    def cancel(self, handle: Any, reason: str) -> None: ...
    def result(self, handle: Any) -> dict: ...


@dataclass
class RunConfig:
    task: str
    n: int = 3
    check: Optional[str] = None
    models: list[str] = field(default_factory=list)
    timeout_s: float = 1200
    check_timeout_s: float = 600
    protect_tests: bool = True
    protected_paths: list[str] = field(default_factory=list)
    run_baseline: bool = True
    keep_worktrees: bool = False
    laps: int = 1                 # child launches per heat: a failing heat is relaunched with the check's output
    finish_early: bool = False    # the first heat that qualifies ends the race (others are stopped and judged)
    min_lap_s: float = 90         # do not start a new lap with less time than this left
    run_id: Optional[str] = None  # preset by the caller (the plugin returns it before the race ends)
    attempt_context: str = ""     # extra brief for every attempt (the plugin passes Hermes's coding guidance)
    active_run_ids: set = field(default_factory=set)  # other live races (stale-run cleanup leaves them alone)


@dataclass
class RunReport:
    run_id: str
    root: str
    base: str
    base_dirty: bool
    check: str
    baseline: Optional[CheckResult]
    heats: list[HeatResult]
    started_at: float
    finished_at: float
    run_dir: str
    errors: list[str] = field(default_factory=list)

    @property
    def winner(self) -> Optional[HeatResult]:
        return winner(self.heats)

    def markdown(self) -> str:
        label = self.base[:8] + (" + your uncommitted changes" if self.base_dirty else "")
        md = scoreboard(self.run_id, self.heats, self.baseline, self.check, label)
        if self.errors:
            md += "\n\nWarnings: " + "; ".join(self.errors)
        return md

    def to_dict(self) -> dict:
        w = self.winner
        return {
            "run_id": self.run_id, "root": self.root, "base": self.base, "base_dirty": self.base_dirty,
            "check": self.check, "baseline": self.baseline.to_dict() if self.baseline else None,
            "heats": [h.to_dict() for h in rank(self.heats)], "winner": w.index if w else None,
            "started_at": self.started_at, "finished_at": self.finished_at, "run_dir": self.run_dir,
            "errors": self.errors,
        }


def new_run_id() -> str:
    return time.strftime("%m%d-%H%M%S") + "-" + secrets.token_hex(2)


_RUN_ID = re.compile(r"\d{4}-\d{6}-[0-9a-f]{4}")


def valid_run_id(run_id: object) -> bool:
    """Only the shape new_run_id() makes: no '/', no '..', no absolute path can reach the runs folder."""
    return isinstance(run_id, str) and bool(_RUN_ID.fullmatch(run_id))


def _run_dir(data_dir: str | Path, run_id: str) -> Path:
    if not valid_run_id(run_id):
        raise FileNotFoundError(f"no Heats run '{str(run_id)[:40]}' (run ids look like 1110-083617-ab12)")
    return Path(data_dir) / "runs" / run_id


_EDIT_RULES = ("Edit with the patch tool (replace the exact faulty lines); use write_file only for new files, and "
               "never paste read_file's line-number prefixes into a file. Describing a change does nothing: only "
               "tool calls change files.")


def build_goal(task: str, heat: int, n: int, strategy: str, check: str, worktree: str) -> str:
    who = f"You are attempt {heat} of {n} running in parallel on this task" if n > 1 else "You are working on this task"
    return (
        f"{task.strip()}\n\n"
        f"---\n{who} (a Heats run). Your own copy of the repository is at: {worktree}\n"
        "Work only inside that directory; your file and terminal tools already start there.\n"
        f"Approach: {strategy}\n"
        f"Steps: (1) run the check with the terminal tool to see what fails: `{check}`; (2) read the code it points to; "
        f"(3) fix it. {_EDIT_RULES} (4) run the check again; repeat until it exits 0, then stop.\n"
        "Heats runs the check itself after you stop and judges only by its exit code, so do not spend steps on a "
        "summary. Do not edit, delete or skip existing tests, and do not commit."
    )


def build_followup_goal(task: str, heat: int, lap: int, laps: int, check: str, worktree: str,
                        h: HeatResult, tried: str = "", reverted: str = "") -> str:
    """Goal for the next lap of a heat whose last lap ended without passing. Everything stated here
    was measured by Heats in the worktree, not taken from the previous lap's own claims."""
    c = h.check
    if reverted:
        changed = reverted
    elif h.files:
        changed = f"files changed so far (kept in your copy): {', '.join(h.files[:8])} (+{h.insertions} -{h.deletions})"
    else:
        changed = ("NOTHING has been changed in your copy so far. Describing or planning a change does nothing: "
                   "you must call write_file or patch")
    if c is None:
        result = "the check could not be run"
    elif c.exit_code is None and c.error.startswith("not run"):
        result = f"the check was {c.error}; restore those files (e.g. `git checkout -- <file>`)"
    elif c.timed_out:
        result = "the check timed out"
    else:
        counts = f", {c.passed or 0} passed / {c.failed or 0} failed" if (c.passed is not None or c.failed is not None) else ""
        result = f"`{check}` exited {c.exit_code}{counts}"
    guard = (f"\nYou edited protected test files ({', '.join(h.guarded[:5])}); an attempt that edits tests cannot win. "
             "Restore them exactly (e.g. `git checkout -- <file>`) and fix the code instead.") if h.guarded else ""
    tail = (c.tail if c else "")[-1500:]
    tried_txt = (f"What the previous lap changed (diff against the starting code):\n```diff\n{tried[-1500:]}\n```\n"
                 "That did not work; do not simply repeat it.\n") if tried.strip() else ""
    return (
        f"{task.strip()}\n\n"
        f"---\nYou are continuing attempt {heat} (lap {lap} of {laps}) of a Heats run. "
        f"Your own copy of the repository is at: {worktree}\n"
        "Work only inside that directory; your file and terminal tools already start there.\n"
        "The previous lap ended but the task is NOT done. Heats measured, just now:\n"
        f"- {changed}\n- {result}{guard}\n"
        f"Last lines of the check output:\n```\n{tail}\n```\n{tried_txt}"
        f"Steps: (1) read the failing code and the output above; (2) fix it. {_EDIT_RULES} "
        f"(3) run `{check}` with the terminal tool; (4) repeat until it exits 0, then stop. "
        "Heats judges only by the check's exit code. Do not edit, delete or skip existing tests, and do not commit."
    )


class Runner:
    def __init__(self, launcher: Launcher, isolation: IsolationMap, data_dir: str | Path,
                 progress: Optional[Callable[[str], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None,
                 on_update: Optional[Callable[[dict], None]] = None) -> None:
        self.on_update = on_update
        self._emit_lock = threading.Lock()
        self._live: dict = {}
        self.launcher = launcher
        self.isolation = isolation
        self.data_dir = Path(data_dir)
        self.progress = progress or (lambda msg: None)
        self.should_stop = should_stop or (lambda: False)

    # ------------------------------------------------------------------ run
    def run(self, cfg: RunConfig, cwd: str) -> RunReport:
        root = gitops.repo_root(cwd)
        if not root:
            raise gitops.HeatsGitError(f"{cwd} is not inside a git repository; Heats needs git worktrees")
        try:  # worktrees/branches left by a run that was killed (e.g. Hermes restarted mid-race)
            gitops.cleanup_stale(root, set(cfg.active_run_ids) | ({cfg.run_id} if cfg.run_id else set()),
                                 min_age_s=6 * 3600)
        except Exception:
            pass
        n = max(1, min(int(cfg.n), MAX_HEATS))
        check = (cfg.check or "").strip() or detect_check(root)
        if not check:
            raise gitops.HeatsGitError(
                "could not find a test command for this repo; pass check='<command that exits 0 on success>'")
        base, dirty = gitops.snapshot_base(root)
        fingerprint = gitops.tree_fingerprint(root)
        run_id = cfg.run_id or new_run_id()
        run_dir = self.data_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        gitops.ensure_excluded(root)
        wt_root = Path(root) / ".heats" / run_id
        guard = Guard(protect_tests=cfg.protect_tests, extra=list(cfg.protected_paths))
        started = time.time()
        errors: list[str] = []

        heats: list[HeatResult] = []
        for i in range(1, n + 1):
            name, _ = DEFAULT_STRATEGIES[(i - 1) % len(DEFAULT_STRATEGIES)]
            model = cfg.models[(i - 1) % len(cfg.models)] if cfg.models else None
            heats.append(HeatResult(index=i, strategy=name, model=model))
        self._heats_ref = heats
        self._linked: dict[str, tuple] = {}
        self._live = {"run_id": run_id, "repo": os.path.basename(root), "root": root, "check": check,
                      "n": n, "task": cfg.task[:300], "started_at": started, "winner": None, "baseline": None}
        lanes: dict[int, tuple[str, str]] = {}
        handles: dict[int, Any] = {}
        keys: dict[int, list[str]] = {}
        t0: dict[int, float] = {}
        finished_children: set[int] = set()
        baseline: Optional[CheckResult] = None
        pool = ThreadPoolExecutor(max_workers=min(4, n + 1), thread_name_prefix="heats-check")
        completed = False
        try:
            for h in heats:
                path = str(wt_root / f"h{h.index}")
                branch = f"heats/{run_id}/h{h.index}"
                lanes[h.index] = (path, branch)  # recorded first, so cleanup covers a half-made worktree
                gitops.add_worktree(root, path, branch, base)
                real = os.path.realpath(path)
                lanes[h.index] = (real, branch)
                self._linked[real] = tuple(gitops.link_dependency_dirs(root, real))
            self.progress(f"heats {run_id}: {n} worktrees ready from {base[:8]}{' (your working tree)' if dirty else ''}")
            self._emit("starting", heats)
            base_future = None
            if cfg.run_baseline:  # measured before any attempt starts: a check that cannot run stops the race here
                baseline = self._baseline(root, base, run_id, check, cfg)
                self._live["baseline"] = {"ok": baseline.ok, "passed": baseline.passed, "failed": baseline.failed}
                if cannot_run(baseline, check):
                    raise RuntimeError(f"the check command could not run (exit {baseline.exit_code}): `{check}`. "
                                       f"Output: {(baseline.error or baseline.tail)[-300:].strip()} -- fix the command and try again")
                if baseline.ok:
                    raise RuntimeError(f"the check `{check}` already passes on your current code"
                                       f"{f' ({baseline.passed} passed)' if baseline.passed is not None else ''}, so there is "
                                       "nothing for Heats to judge. Name a check that fails now and should pass after the fix "
                                       "(write the failing test first if there is none).")

            for h in heats:
                wt, _ = lanes[h.index]
                strategy_text = dict(DEFAULT_STRATEGIES)[h.strategy]
                goal = build_goal(cfg.task, h.index, n, strategy_text, check, wt)
                key = f"heats-{run_id}-h{h.index}"
                try:
                    handle = self.launcher.launch(goal=goal, context=self._context(wt, cfg), model=h.model, key=key)
                except Exception as exc:  # launch refused: record and carry on with the others
                    h.agent_state, h.agent_error = "FAILED", f"launch failed: {exc}"
                    h._status = "error"  # type: ignore[attr-defined]
                    continue
                lane = Lane(root=root, worktree=wt, heat=h.index)
                keys[h.index] = [k for k in self.launcher.task_keys(handle) if k]
                for k in keys[h.index]:
                    self.isolation.add(k, lane)
                handles[h.index] = handle
                t0[h.index] = time.monotonic()
                h._t0 = t0[h.index]  # type: ignore[attr-defined]  # live elapsed time for the pane
                h._status = "running"  # type: ignore[attr-defined]
                h._lane = lane  # type: ignore[attr-defined]
            self.progress(f"heats {run_id}: {len(handles)} attempts running")
            self._emit("running", heats)

            futures: dict[int, Any] = {}      # latest judge per heat (final once the heat is done)
            judging: dict[int, Any] = {}      # judges whose outcome may still start another lap
            laps_used = {i: 1 for i in handles}
            deadline = time.monotonic() + cfg.timeout_s
            pending = set(handles)
            unknown_since: dict[int, float] = {}
            stop_noted = False
            race_won = False
            last_tick = time.monotonic()

            def stop_pending(reason: str, state: str) -> None:
                for i in sorted(pending):
                    # freeze the worktree first: every further tool call of this child is refused,
                    # so the judge below sees a tree that is no longer changing
                    for k in keys.get(i, []):
                        self.isolation.block(k)
                    self.launcher.cancel(handles[i], reason)
                    hh = heats[i - 1]
                    hh.agent_state = state
                    hh._status = "judging"  # type: ignore[attr-defined]
                    hh.duration_s = round(time.monotonic() - t0[i], 1)
                    futures[i] = pool.submit(self._judge, hh, root, lanes[i][0], base, check, cfg, guard, run_dir)
                pending.clear()

            while pending or judging:
                if not stop_noted and self.should_stop():
                    stop_noted = True
                    errors.append("stopped by the user")
                    for i in pending:
                        self.launcher.cancel(handles[i], "Heats run stopped")
                    deadline = min(deadline, time.monotonic() + 20)
                if time.monotonic() > deadline:
                    stop_pending("Heats time limit reached", "TIMEOUT")
                    if not stop_noted:
                        errors.append(f"time limit {cfg.timeout_s:.0f}s reached; unfinished heats were judged as they stood")
                    judging.clear()   # their judges are in `futures`; no further laps
                    break
                for i in list(pending):
                    state = self.launcher.state(handles[i], wait_s=1.0 / max(1, len(pending)))
                    if state == "UNKNOWN":
                        # Hermes can report UNKNOWN for a child that is still alive (e.g. its session id rotated
                        # during context compression). Only believe it after 60 s, and even then do not count
                        # the child as finished: its tool calls stay blocked rather than un-isolated.
                        first = unknown_since.setdefault(i, time.monotonic())
                        if time.monotonic() - first < 60:
                            continue
                    else:
                        unknown_since.pop(i, None)
                    if state in TERMINAL_STATES:
                        pending.discard(i)
                        if state != "UNKNOWN":
                            finished_children.add(i)
                        h = heats[i - 1]
                        h.duration_s = round(time.monotonic() - t0[i], 1)
                        res = self.launcher.result(handles[i]) or {}
                        h.agent_state = res.get("state") or state
                        h.agent_summary = (res.get("summary") or "")[:600]
                        h.agent_error = (res.get("error") or "")[:300]
                        tok = res.get("tokens")
                        if tok is not None:
                            h.tokens = (h.tokens or 0) + tok
                        self.progress(f"heats {run_id}: heat {i} lap {laps_used[i]} finished ({h.agent_state.lower()}), judging")
                        h._status = "judging"  # type: ignore[attr-defined]
                        self._emit("running", heats)
                        futures[i] = judging[i] = pool.submit(
                            self._judge, h, root, lanes[i][0], base, check, cfg, guard, run_dir)
                if pending and time.monotonic() - last_tick >= 5:  # keep the pane's elapsed times moving
                    last_tick = time.monotonic()
                    self._emit("running", heats)
                if not pending and judging:
                    time.sleep(0.05)
                done_now = [i for i, f in judging.items() if f.done()]
                for i in done_now:
                    judging.pop(i)
                # decide on the whole batch at once: a pass anywhere in it ends the race before
                # any failing heat in the same batch is sent on another lap
                ok_now = [i for i in done_now if futures[i].exception() is None and heats[i - 1].qualifies]
                if ok_now and cfg.finish_early and not race_won:
                    race_won = True
                    self.progress(f"heats {run_id}: heat {ok_now[0]} passed; stopping the others")
                    stop_pending(f"heat {ok_now[0]} already passed", "STOPPED")
                for i in done_now:
                    if futures[i].exception() is not None or heats[i - 1].qualifies:
                        continue  # exceptions are reported below from `futures`
                    left = deadline - time.monotonic()
                    if (laps_used[i] < max(1, cfg.laps) and not race_won and not stop_noted
                            and left > cfg.min_lap_s):
                        self._next_lap(heats[i - 1], i, laps_used, cfg, check, lanes, handles, keys, t0,
                                       finished_children, pending, errors, run_id, baseline, base)
            for i, fut in futures.items():
                try:
                    fut.result()
                except Exception as exc:
                    errors.append(f"heat {i}: judging failed: {exc}")
            completed = True
        finally:
            pool.shutdown(wait=True)
            # Children that were cancelled may still be finishing their last step. Give them a moment;
            # any that are still alive stay BLOCKED (every tool call refused), never un-isolated.
            grace = time.monotonic() + 20
            for i in sorted(set(handles) - finished_children):
                while time.monotonic() < grace:
                    try:
                        st = self.launcher.state(handles[i], wait_s=0.5)
                        if st in TERMINAL_STATES:
                            if st != "UNKNOWN":  # unknown = maybe still alive: keep its calls blocked
                                finished_children.add(i)
                            break
                    except Exception:
                        break
            for i, ks in keys.items():
                for k in ks:
                    if i in finished_children:
                        self.isolation.remove(k)
                    else:
                        self.isolation.block(k)
            for h in heats:
                lane = getattr(h, "_lane", None)
                if lane is not None:
                    h.escaped = lane.redirects
            if not cfg.keep_worktrees:
                for i, (path, branch) in lanes.items():
                    gitops.remove_worktree(root, path, branch)
                shutil.rmtree(wt_root, ignore_errors=True)
                try:
                    (Path(root) / ".heats").rmdir()
                except OSError:
                    pass
            if not completed:
                self._emit("error", heats, errors=errors + ["the run stopped early because of an error"])

        if gitops.tree_fingerprint(root) != fingerprint:
            errors.append("your main checkout changed while the heats ran (files, branches, stash or git config; by you, "
                          "or by an attempt that escaped its worktree); check `git status` before applying a winner")
        report = RunReport(run_id, root, base, dirty, check, baseline, heats, started, time.time(), str(run_dir), errors)
        (run_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        (run_dir / "scoreboard.md").write_text(report.markdown(), encoding="utf-8")
        self.progress(f"heats {run_id}: done")
        self._emit("done", heats, errors=errors, finished_at=report.finished_at)
        return report

    # ------------------------------------------------------------ helpers
    def _next_lap(self, h: HeatResult, i: int, laps_used: dict, cfg: RunConfig, check: str, lanes: dict,
                  handles: dict, keys: dict, t0: dict, finished_children: set, pending: set,
                  errors: list, run_id: str, baseline: Optional[CheckResult] = None, base: str = "") -> None:
        """Relaunch heat *i* in its own worktree with what the last lap left behind. A lap that left fewer
        tests passing than the starting code is undone first, and the next lap is told what was tried."""
        lap = laps_used[i] + 1
        wt = lanes[i][0]
        lane = getattr(h, "_lane", None)
        raw = b""
        try:
            raw = Path(h.patch_file).read_bytes() if h.patch_file else b""
        except OSError:
            pass
        tried = raw.decode("utf-8", "replace")
        reverted = ""
        c = h.check
        if (tried.strip() and baseline is not None and c is not None and baseline.passed is not None
                and c.passed is not None and c.passed < baseline.passed):
            cp = gitops.git(["apply", "-R", "--whitespace=nowarn", "-"], cwd=wt, input_text=raw, binary=True)
            if cp.returncode == 0:
                reverted = (f"the previous lap left FEWER tests passing ({c.passed}) than the starting code "
                            f"({baseline.passed}), so Heats reset your copy to the starting code")
            else:
                errors.append(f"heat {i}: could not undo lap {lap - 1} ({str(cp.stderr or '')[:200]}); continuing from it")
        goal = build_followup_goal(cfg.task, i, lap, cfg.laps, check, wt, h, tried=tried, reverted=reverted)
        try:
            handle = self.launcher.launch(goal=goal, context=self._context(wt, cfg), model=h.model,
                                          key=f"heats-{run_id}-h{i}-lap{lap}")
        except Exception as exc:
            errors.append(f"heat {i}: lap {lap} could not start: {exc}")
            return
        old_keys = keys.get(i, [])
        # the new child is live from here on: track it before anything else can fail
        handles[i] = handle
        finished_children.discard(i)
        keys[i] = []
        try:
            keys[i] = [k for k in self.launcher.task_keys(handle) if k]
            if lane is None:
                raise RuntimeError("no isolation lane for this heat")
            for k in keys[i]:
                self.isolation.add(k, lane)
        except Exception as exc:
            errors.append(f"heat {i}: lap {lap} could not be isolated, stopped it: {exc}")
            for k in keys[i]:
                self.isolation.block(k)
            try:
                self.launcher.cancel(handle, "Heats could not isolate this lap")
            except Exception:
                pass
            keys[i] = keys[i] + old_keys
            return
        for k in old_keys:            # the previous lap's child is finished: release its keys
            self.isolation.remove(k)
        h.agent_summary = h.agent_error = ""
        laps_used[i] = lap
        h.laps = lap
        t0[i] = time.monotonic() - h.duration_s   # duration keeps counting across laps
        h._t0 = t0[i]  # type: ignore[attr-defined]
        h._status = "running"  # type: ignore[attr-defined]
        pending.add(i)
        self.progress(f"heats {run_id}: heat {i} did not pass; lap {lap} of {cfg.laps} started")
        self._emit("running", self._heats_ref)

    @staticmethod
    def _context(wt: str, cfg: RunConfig) -> str:
        brief = (cfg.attempt_context or "").strip()
        return f"Repository copy: {wt}" + (f"\n\n{brief}" if brief else "")

    def _emit(self, phase: str, heats: list[HeatResult], **extra) -> None:
        """Push a snapshot of the run to the live view (Desktop pane). Never raises."""
        if self.on_update is None:
            return
        try:
            with self._emit_lock:
                self._live.update(extra)
                self._live["phase"] = phase
                rows = []
                for h in sorted(heats, key=lambda x: x.index):
                    c = h.check
                    rows.append({
                        "index": h.index, "strategy": h.strategy, "model": h.model,
                        "status": getattr(h, "_status", "queued"), "seconds": _live_seconds(h),
                        "passed": c.passed if c else None, "failed": c.failed if c else None,
                        "check_ok": bool(c and c.ok), "insertions": h.insertions, "deletions": h.deletions,
                        "files": len(h.files), "guarded": bool(h.guarded), "qualifies": h.qualifies,
                        "claimed_unchanged": bool(c and not h.changed and h.agent_summary),
                        "lap": h.laps,
                    })
                self._live["heats"] = rows
                if phase == "done":
                    w = winner(heats)
                    self._live["winner"] = w.index if w else None
                snapshot = json.loads(json.dumps(self._live, default=str))
            self.on_update(snapshot)
        except Exception:
            pass

    def _baseline(self, root, base, run_id, check, cfg) -> CheckResult:
        path = str(Path(root) / ".heats" / run_id / "h0")
        branch = f"heats/{run_id}/h0"
        try:
            gitops.add_worktree(root, path, branch, base)
            gitops.link_dependency_dirs(root, path)
            return run_check(check, path, timeout_s=cfg.check_timeout_s)
        finally:
            gitops.remove_worktree(root, path, branch)

    def _judge(self, h: HeatResult, root: str, wt: str, base: str, check: str, cfg: RunConfig,
               guard: Guard, run_dir: Path) -> None:
        patch = gitops.collect_patch(wt, base, ignore=self._linked.get(os.path.realpath(wt), ()))
        h.files, h.insertions, h.deletions = patch.files, patch.insertions, patch.deletions
        h.guarded = guard.violations(patch.files, patch.data)
        pf = run_dir / f"heat{h.index}.patch"
        pf.write_bytes(patch.data)
        h.patch_file = str(pf)
        risky = check_defining_edits(patch.files, check)
        if risky:
            # The attempt changed what the check runs (conftest.py, package.json, a Makefile, a script the check
            # names...). Running it would execute model-written code on the host with no prompt, so it is not run;
            # the heat cannot win (as the referee refuses the same case).
            h.guarded = h.guarded + [f for f in risky if f not in h.guarded]
            h.check = CheckResult(check, None, 0.0, error=f"not run: the attempt edited {', '.join(risky[:4])}, "
                                  "which can change what the check runs")
            h._status = "flagged"  # type: ignore[attr-defined]
            self._emit("running", self._heats_ref)
            return
        h.check = run_check(check, wt, timeout_s=cfg.check_timeout_s)
        h._status = "pass" if h.qualifies else ("flagged" if h.check.ok else "fail")  # type: ignore[attr-defined]
        self._emit("running", self._heats_ref)


# ---------------------------------------------------------------- saved runs
def load_report(data_dir: str | Path, run_id: str) -> dict:
    p = _run_dir(data_dir, run_id) / "report.json"
    if not p.is_file():
        raise FileNotFoundError(f"no Heats run '{run_id}'")
    return json.loads(p.read_text(encoding="utf-8"))


def list_runs(data_dir: str | Path, limit: int = 10) -> list[dict]:
    runs_dir = Path(data_dir) / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for d in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads((d / "report.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def apply_heat(data_dir: str | Path, run_id: str, heat: Optional[int] = None, force: bool = False) -> tuple[bool, str]:
    """Apply a saved heat's patch (default: the winner) to the run's repository working tree."""
    rep = load_report(data_dir, run_id)
    idx = heat or rep.get("winner")
    if not idx:
        return False, f"run {run_id} has no winner; name a heat explicitly to apply it anyway"
    entry = next((h for h in rep["heats"] if h["index"] == int(idx)), None)
    if entry is None:
        return False, f"run {run_id} has no heat {idx}"
    if not entry.get("qualifies") and not force:
        return False, f"heat {idx} did not pass the check cleanly; pass force=true to apply it anyway"
    patch_path = Path(entry.get("patch_file") or "")
    if not patch_path.is_file():
        return False, f"the patch for heat {idx} is missing ({patch_path})"
    if rep.get("applied") and not force:
        return False, (f"heat {rep['applied'].get('heat')} of {run_id} was already applied at {rep['applied'].get('at')}; "
                       "pass force=true to apply again")
    if not Path(rep["root"]).is_dir() or gitops.repo_root(rep["root"]) != os.path.realpath(rep["root"]):
        return False, f"the run's repository {rep['root']} is no longer a git checkout"
    ok, msg = gitops.apply_patch(rep["root"], patch_path.read_bytes())
    if ok:
        rep["applied"] = {"heat": int(idx), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            (Path(data_dir) / "runs" / run_id / "report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        except OSError:
            pass
        files = ", ".join(entry.get("files", [])[:6])
        return True, f"applied heat {idx} of {run_id} to {rep['root']} ({msg}). Changed: {files}. Review with `git diff`."
    return False, f"could not apply heat {idx}: {msg}"
