"""Run N attempts of one task in parallel git worktrees and judge them with the repo's check.

The runner does not import Hermes. It talks to the agent through a small ``Launcher``
protocol (launch / poll / cancel / result), so tests drive it with a fake and the plugin
adapts Hermes's public subagent lifecycle API to it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import gitops
from .checks import CheckResult, Guard, detect_check, run_check
from .isolation import IsolationMap, Lane
from .scoring import HeatResult, rank, scoreboard, winner

MAX_HEATS = 8
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED", "UNKNOWN"}

DEFAULT_STRATEGIES = [
    ("minimal", "Make the smallest change that fixes the problem. Do not refactor."),
    ("root-cause", "Read the relevant code first and find the root cause, then fix that cause properly."),
    ("test-first", "Run the check first to see exactly what fails, then iterate: change, re-run, until it passes."),
    ("fresh-eyes", "Do not assume the obvious culprit; list two possible causes, check both, then fix the real one."),
]


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
    timeout_s: float = 900
    check_timeout_s: float = 600
    protect_tests: bool = True
    protected_paths: list[str] = field(default_factory=list)
    run_baseline: bool = True
    keep_worktrees: bool = False


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


def build_goal(task: str, heat: int, n: int, strategy: str, check: str, worktree: str) -> str:
    return (
        f"{task.strip()}\n\n"
        f"---\nYou are attempt {heat} of {n} running in parallel on this task (a Heats run). "
        f"Your own copy of the repository is at: {worktree}\n"
        "Work only inside that directory; your file and terminal tools already start there.\n"
        f"Approach for this attempt: {strategy}\n"
        "Steps: (1) read the relevant files; (2) make the change with the write_file or patch tool "
        "(describing a change does nothing, only tool calls change files); "
        f"(3) run the check with the terminal tool: `{check}`; (4) if it fails, fix and run it again.\n"
        "Success is judged only by that command exiting 0 in your copy, checked independently after you finish. "
        "Do not edit, delete or skip existing tests, and do not commit. "
        "Finish with one sentence: what you changed and the check's actual result."
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
        n = max(1, min(int(cfg.n), MAX_HEATS))
        check = (cfg.check or "").strip() or detect_check(root)
        if not check:
            raise gitops.HeatsGitError(
                "could not find a test command for this repo; pass check='<command that exits 0 on success>'")
        base, dirty = gitops.snapshot_base(root)
        untracked = gitops.untracked_files(root)
        fingerprint = gitops.tree_fingerprint(root)
        run_id = new_run_id()
        run_dir = self.data_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        gitops.ensure_excluded(root)
        wt_root = Path(root) / ".heats" / run_id
        guard = Guard(protect_tests=cfg.protect_tests, extra=list(cfg.protected_paths))
        started = time.time()
        errors: list[str] = []

        heats: list[HeatResult] = []
        self._heats_ref = heats
        self._linked: dict[str, tuple] = {}
        lanes: dict[int, tuple[str, str]] = {}
        for i in range(1, n + 1):
            name, text = DEFAULT_STRATEGIES[(i - 1) % len(DEFAULT_STRATEGIES)]
            model = cfg.models[(i - 1) % len(cfg.models)] if cfg.models else None
            heats.append(HeatResult(index=i, strategy=name, model=model))
            path = str(wt_root / f"h{i}")
            branch = f"heats/{run_id}/h{i}"
            gitops.add_worktree(root, path, branch, base)
            if untracked:
                gitops.copy_untracked(root, path, untracked)
            self._linked[os.path.realpath(path)] = tuple(gitops.link_dependency_dirs(root, path))
            lanes[i] = (os.path.realpath(path), branch)
        self.progress(f"heats {run_id}: {n} worktrees ready from {base[:8]}{' (+uncommitted)' if dirty else ''}")
        self._live = {"run_id": run_id, "repo": os.path.basename(root), "root": root, "check": check,
                      "n": n, "task": cfg.task[:300], "started_at": started, "winner": None, "baseline": None}
        self._emit("starting", heats)

        baseline: Optional[CheckResult] = None
        pool = ThreadPoolExecutor(max_workers=min(4, n + 1), thread_name_prefix="heats-check")
        base_future = pool.submit(self._baseline, root, base, run_id, untracked, check, cfg) if cfg.run_baseline else None

        handles: dict[int, Any] = {}
        keys: dict[int, list[str]] = {}
        t0: dict[int, float] = {}
        try:
            for h in heats:
                wt, _ = lanes[h.index]
                strategy_text = dict(DEFAULT_STRATEGIES)[h.strategy]
                goal = build_goal(cfg.task, h.index, n, strategy_text, check, wt)
                key = f"heats-{run_id}-h{h.index}"
                try:
                    handle = self.launcher.launch(goal=goal, context=f"Repository copy: {wt}", model=h.model, key=key)
                except Exception as exc:  # launch refused: record and carry on with the others
                    h.agent_state, h.agent_error = "FAILED", f"launch failed: {exc}"
                    continue
                handles[h.index] = handle
                t0[h.index] = time.monotonic()
                h._status = "running"  # type: ignore[attr-defined]
                lane = Lane(root=root, worktree=wt, heat=h.index)
                keys[h.index] = [k for k in self.launcher.task_keys(handle) if k]
                for k in keys[h.index]:
                    self.isolation.add(k, lane)
                h._lane = lane  # type: ignore[attr-defined]
            for h in heats:
                if h.agent_error and not hasattr(h, "_status"):
                    h._status = "error"  # type: ignore[attr-defined]
            self.progress(f"heats {run_id}: {len(handles)} attempts running")
            self._emit("running", heats)

            futures = {}
            deadline = time.monotonic() + cfg.timeout_s
            pending = set(handles)
            while pending:
                if self.should_stop():
                    errors.append("stopped by the user")
                    for i in pending:
                        self.launcher.cancel(handles[i], "Heats run stopped")
                    deadline = min(deadline, time.monotonic() + 20)
                if time.monotonic() > deadline:
                    for i in list(pending):
                        self.launcher.cancel(handles[i], "Heats time limit reached")
                        heats[i - 1].agent_state = "TIMEOUT"
                        heats[i - 1]._status = "judging"  # type: ignore[attr-defined]
                        heats[i - 1].duration_s = round(time.monotonic() - t0[i], 1)
                        futures[i] = pool.submit(self._judge, heats[i - 1], root, lanes[i][0], base, check, cfg, guard, run_dir)
                    errors.append(f"time limit {cfg.timeout_s:.0f}s reached; unfinished heats were judged as they stood")
                    pending.clear()
                    break
                for i in list(pending):
                    state = self.launcher.state(handles[i], wait_s=1.0 / max(1, len(pending)))
                    if state in TERMINAL_STATES:
                        pending.discard(i)
                        h = heats[i - 1]
                        h.duration_s = round(time.monotonic() - t0[i], 1)
                        res = self.launcher.result(handles[i]) or {}
                        h.agent_state = res.get("state") or state
                        h.agent_summary = (res.get("summary") or "")[:600]
                        h.agent_error = (res.get("error") or "")[:300]
                        h.tokens = res.get("tokens")
                        self.progress(f"heats {run_id}: heat {i} finished ({h.agent_state.lower()}), judging")
                        h._status = "judging"  # type: ignore[attr-defined]
                        self._emit("running", heats)
                        futures[i] = pool.submit(self._judge, h, root, lanes[i][0], base, check, cfg, guard, run_dir)
            for i, fut in futures.items():
                try:
                    fut.result()
                except Exception as exc:
                    errors.append(f"heat {i}: judging failed: {exc}")
            if base_future is not None:
                try:
                    baseline = base_future.result()
                    self._live["baseline"] = {"ok": baseline.ok, "passed": baseline.passed, "failed": baseline.failed}
                except Exception as exc:
                    errors.append(f"baseline check failed to run: {exc}")
        finally:
            pool.shutdown(wait=True)
            for h in heats:
                lane = getattr(h, "_lane", None)
                if lane is not None:
                    h.escaped = lane.redirects
            for ks in keys.values():
                for k in ks:
                    self.isolation.remove(k)
            if not cfg.keep_worktrees:
                for i, (path, branch) in lanes.items():
                    gitops.remove_worktree(root, path, branch)
                shutil.rmtree(wt_root, ignore_errors=True)
                try:
                    (Path(root) / ".heats").rmdir()
                except OSError:
                    pass

        if gitops.tree_fingerprint(root) != fingerprint:
            errors.append("your main checkout changed while the heats ran (by you, or by an attempt that escaped "
                          "its worktree); check `git status` before applying a winner")
        report = RunReport(run_id, root, base, dirty, check, baseline, heats, started, time.time(), str(run_dir), errors)
        (run_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        (run_dir / "scoreboard.md").write_text(report.markdown(), encoding="utf-8")
        self.progress(f"heats {run_id}: done")
        self._emit("done", heats, errors=errors, finished_at=report.finished_at)
        return report

    # ------------------------------------------------------------ helpers
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
                        "status": getattr(h, "_status", "queued"), "seconds": h.duration_s,
                        "passed": c.passed if c else None, "failed": c.failed if c else None,
                        "check_ok": bool(c and c.ok), "insertions": h.insertions, "deletions": h.deletions,
                        "files": len(h.files), "guarded": bool(h.guarded), "qualifies": h.qualifies,
                        "claimed_unchanged": bool(c and not h.changed and h.agent_summary),
                    })
                self._live["heats"] = rows
                if phase == "done":
                    w = winner(heats)
                    self._live["winner"] = w.index if w else None
                snapshot = json.loads(json.dumps(self._live, default=str))
            self.on_update(snapshot)
        except Exception:
            pass

    def _baseline(self, root, base, run_id, untracked, check, cfg) -> CheckResult:
        path = str(Path(root) / ".heats" / run_id / "h0")
        branch = f"heats/{run_id}/h0"
        gitops.add_worktree(root, path, branch, base)
        try:
            if untracked:
                gitops.copy_untracked(root, path, untracked)
            gitops.link_dependency_dirs(root, path)
            return run_check(check, path, timeout_s=cfg.check_timeout_s)
        finally:
            gitops.remove_worktree(root, path, branch)

    def _judge(self, h: HeatResult, root: str, wt: str, base: str, check: str, cfg: RunConfig,
               guard: Guard, run_dir: Path) -> None:
        patch = gitops.collect_patch(wt, base, ignore=self._linked.get(os.path.realpath(wt), ()))
        h.files, h.insertions, h.deletions = patch.files, patch.insertions, patch.deletions
        h.guarded = guard.violations(patch.files)
        pf = run_dir / f"heat{h.index}.patch"
        pf.write_text(patch.text, encoding="utf-8")
        h.patch_file = str(pf)
        h.check = run_check(check, wt, timeout_s=cfg.check_timeout_s)
        h._status = "pass" if h.qualifies else ("flagged" if h.check.ok else "fail")  # type: ignore[attr-defined]
        self._emit("running", self._heats_ref)


# ---------------------------------------------------------------- saved runs
def load_report(data_dir: str | Path, run_id: str) -> dict:
    p = Path(data_dir) / "runs" / run_id / "report.json"
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
    ok, msg = gitops.apply_patch(rep["root"], patch_path.read_text(encoding="utf-8"))
    if ok:
        files = ", ".join(entry.get("files", [])[:6])
        return True, f"applied heat {idx} of {run_id} to {rep['root']} ({msg}). Changed: {files}. Review with `git diff`."
    return False, f"could not apply heat {idx}: {msg}"
