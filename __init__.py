"""Heats: race N attempts at a coding task in parallel git worktrees; the repo's own tests pick the winner.

Hermes surfaces used (all public): ``ctx.subagent_lifecycle`` to launch the attempts,
``tool_request`` middleware to keep each attempt inside its worktree, ``pre_tool_call`` to
refuse writes that would still land outside it, ``post_tool_call`` to follow each attempt's
terminal directory, plus tools, a slash command, a CLI command and a bundled skill.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .heats_core import gitops
from .heats_core import referee as _referee
from .heats_core.isolation import IsolationMap, note_terminal_result, rewrite, shared_git_command
from .heats_core.runner import MAX_HEATS, RunConfig, Runner, apply_heat, list_runs, load_report, new_run_id

logger = logging.getLogger(__name__)

PLUGIN = "heats"
_ISOLATION = IsolationMap()
_CTX: Any = None
_WRITE_TOOLS = {"write_file", "patch"}
# Hermes stops waiting for any one tool call after 420 s (tools.sequential_call) and plugins cannot raise
# that per tool, so a race runs in a background thread and the tools wait for it in slices below that.
DEFAULT_WAIT_S = 360
_RUNS: dict[str, dict] = {}
_RUNS_LOCK = threading.Lock()


# ------------------------------------------------------------------ helpers
def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN))
    except Exception:  # running outside Hermes (tests)
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        d = home / "plugin-data" / PLUGIN
        d.mkdir(parents=True, exist_ok=True)
        return d


def _cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:
        return os.getcwd()


# Heats' own brief for attempt subagents (they do not get Hermes's interactive coding brief). Measured on the
# bench: without a brief like this a 4B attempt rewrote whole files and claimed passes it did not have far more often.
CODING_BRIEF = (
    "You are fixing code in a git checkout. Read the relevant files and find the code with search before you "
    "change anything; never guess a file's contents or invent an API. Change files only through your file tools "
    "(patch for edits, write_file for new files): describing a change in the chat does nothing. Touch only what "
    "the task needs. If a patch fails to apply, read the file again before retrying. Run the check yourself and "
    "read its output before you say the work is done; fix the cause, not the symptom. Do not commit, push or "
    "change git branches, and leave secrets and .env files alone."
)


def _coding_brief() -> str:
    return CODING_BRIEF


def _setting(key: str, default: Any) -> Any:
    try:
        v = _CTX.get_config(key, default=default) if _CTX is not None else default
        return default if v is None else v
    except Exception:
        return default


def _trusted_repos() -> list[str]:
    """Repos whose test command the owner approved (plugin setting ``trusted_repos``, empty by default).
    Only the owner adds to it: /heats trust, `hermes heats trust`, or the config file."""
    raw = _setting("trusted_repos", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return [os.path.realpath(os.path.expanduser(p.strip())) for p in raw if isinstance(p, str) and p.strip()]


def _is_trusted(root: Optional[str]) -> bool:
    return bool(root) and os.path.realpath(root) in _trusted_repos()


def _trust(path: str, add: bool = True) -> str:
    cwd = os.path.realpath(os.path.expanduser(path or _cwd()))
    root = gitops.repo_root(cwd) if os.path.isdir(cwd) else None
    if not root:
        return f"{cwd} is not inside a git repository."
    root = os.path.realpath(root)
    cur = _trusted_repos()
    new = (cur + [root]) if add and root not in cur else [p for p in cur if p != root] if not add else cur
    if new != cur:
        _CTX.set_config("trusted_repos", new)
    from .heats_core.checks import detect_check
    cmd = _setting("check", "") or detect_check(root) or "(no check found)"
    if add:
        return (f"Heats will now run `{cmd}` in {root} when the agent edits code there (the referee) and when you "
                f"race attempts. Undo with /heats untrust {root}")
    return f"Heats will no longer run the check in {root} without asking."


def _publish(snapshot: dict) -> None:
    """Live view: write the latest snapshot for the Desktop pane's REST read, and push it as
    the event plugin.heats.run.update to any connected Desktop window. Never raises."""
    try:
        path = _data_dir() / "live.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN, "run.update", snapshot)
    except Exception:
        pass


def _err(msg: str, **extra) -> str:
    return json.dumps({"ok": False, "error": msg, **extra})


class LifecycleLauncher:
    """Adapts Hermes's public subagent lifecycle API to the runner's Launcher protocol."""

    def __init__(self, ctx: Any, toolsets: Optional[tuple[str, ...]]):
        self.service = ctx.subagent_lifecycle
        self.toolsets = toolsets

    def launch(self, *, goal: str, context: str, model: Optional[str], key: str) -> Any:
        from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleError
        req = dict(goal=goal, context=context, role="leaf", model=model or None, correlation_id=key,
                   metadata={"plugin": PLUGIN, "heat": key})
        if self.toolsets:
            try:
                return self.service.launch(SubagentLaunchRequest(allowed_toolsets=self.toolsets, **req))
            except SubagentLifecycleError as exc:
                if "toolset" not in str(exc).lower():
                    raise
                logger.info("heats: toolsets %s refused (%s); inheriting the parent's", self.toolsets, exc)
        return self.service.launch(SubagentLaunchRequest(**req))

    def task_keys(self, handle: Any) -> list[str]:
        return [handle.subagent_id]

    def state(self, handle: Any, wait_s: float) -> str:
        t = self.service.wait(handle, timeout_seconds=max(0.05, wait_s))
        return t.state.value if t.completed or not t.timed_out else self.service.status(handle).state.value

    def cancel(self, handle: Any, reason: str) -> None:
        try:
            self.service.cancel(handle, reason=reason)
        except Exception as exc:
            logger.warning("heats: cancel failed: %s", exc)

    def result(self, handle: Any) -> dict:
        r = self.service.result(handle)
        return {"state": r.terminal_state.value, "summary": r.summary or "", "error": r.error_message or "",
                "tokens": None, "api_calls": (r.usage_metadata or {}).get("api_calls")}


# -------------------------------------------------------------------- tools
HEATS_RUN = {
    "name": "heats_run",
    "description": (
        "Race several independent attempts at ONE coding task in parallel, each in its own git worktree, "
        "then run the repository's check command (its tests) in every worktree and rank the attempts. "
        "Nothing in the user's checkout changes; the winner is applied afterwards with heats_apply. "
        "Use it when the user asks for heats / best-of-N / several attempts, or for a bug fix or small "
        "feature that has a test command which decides success. Takes minutes: it waits up to ~6 minutes and "
        "returns the scoreboard, or a run_id to pass to heats_wait if the race is still going. "
        "Do not use it for tasks without an objective check."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The full, self-contained task for every attempt (they see nothing else from this chat): what to change and why, file names, error text."},
            "n": {"type": "integer", "minimum": 1, "maximum": MAX_HEATS, "description": "Number of parallel attempts (default from settings, usually 3)."},
            "check": {"type": "string", "description": "Shell command that exits 0 when the task is solved, e.g. 'python -m pytest -q tests/test_parser.py'. Omit to auto-detect (pytest, npm/pnpm/yarn test, cargo test, go test, make test)."},
            "models": {"type": "array", "items": {"type": "string"}, "description": "Optional model names to race against each other; attempts cycle through them. Omit to use the delegation model."},
            "timeout_minutes": {"type": "number", "description": "Wall-clock limit for the attempts (default from settings, 20)."},
            "laps": {"type": "integer", "description": "Max launches per attempt; a failing attempt is restarted with the check output (default from settings, 3)."},
            "repo_path": {"type": "string", "description": "Repository to work on (default: the session's working directory)."},
        },
        "required": ["task"],
    },
}

HEATS_WAIT = {
    "name": "heats_wait",
    "description": (
        "Wait for a Heats race that heats_run reported as still running, then return its scoreboard. "
        "Waits up to ~6 minutes per call; if it is still running, call heats_wait again with the same run_id. "
        "Do not edit the repository yourself while a race is running."
    ),
    "parameters": {
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "run_id from heats_run."}},
        "required": ["run_id"],
    },
}

HEATS_APPLY = {
    "name": "heats_apply",
    "description": (
        "Apply one heat's saved patch from a finished heats_run to the user's working tree (unstaged, like a normal edit). "
        "Default is the winner. Only call it after the user agreed, or when they asked for the winner to be applied."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "run_id from heats_run."},
            "heat": {"type": "integer", "description": "Heat number to apply (default: the winner)."},
            "force": {"type": "boolean", "description": "Apply a heat that did not pass cleanly. Only if the user explicitly asks."},
        },
        "required": ["run_id"],
    },
}


def _handle_run(args: dict, **kwargs) -> str:
    if _CTX is None:
        return _err("heats is not initialised")
    task = (args.get("task") or "").strip()
    if not task:
        return _err("task is required")
    try:
        n = int(args.get("n") or _setting("default_heats", 3))
    except (TypeError, ValueError):
        return _err("n must be an integer")
    if not 1 <= n <= MAX_HEATS:
        return _err(f"n must be between 1 and {MAX_HEATS}")
    raw_models = args.get("models") or _setting("models", []) or []
    if isinstance(raw_models, str):
        raw_models = [m.strip() for m in raw_models.split(",")]
    models = [m.strip() for m in raw_models if isinstance(m, str) and m.strip()]
    try:
        timeout_min = float(args.get("timeout_minutes") or _setting("timeout_minutes", 20))
    except (TypeError, ValueError):
        return _err("timeout_minutes must be a number")
    try:
        laps = int(args.get("laps") or _setting("laps", 3))
    except (TypeError, ValueError):
        return _err("laps must be an integer")
    if not 1 <= laps <= 10:
        return _err("laps must be between 1 and 10")
    toolsets = tuple(_setting("toolsets", ["terminal", "file"]) or ()) or None
    cfg = RunConfig(
        task=task, n=n, check=args.get("check") or _setting("check", "") or None, models=models,
        timeout_s=min(max(30.0, timeout_min * 60), 4 * 3600.0),  # at most 4 h: a forgotten race must end
        check_timeout_s=float(_setting("check_timeout_seconds", 600)),
        protect_tests=bool(_setting("protect_tests", True)),
        protected_paths=list(_setting("protected_paths", []) or []),
        run_baseline=bool(_setting("run_baseline", True)),
        laps=laps, finish_early=bool(_setting("finish_early", True)),
        attempt_context=_coding_brief(),
        active_run_ids=set(_RUNS),  # races still live in this process: never cleaned up as stale
    )
    cwd = os.path.expanduser(args.get("repo_path") or _cwd())
    if not os.path.isdir(cwd):
        return _err(f"{cwd} is not a directory")
    if not gitops.repo_root(cwd):
        return _err(f"{cwd} is not inside a git repository; Heats needs git worktrees (run `git init` and commit once)")
    if getattr(_CTX, "subagent_lifecycle", None) is None:
        return _err("this Hermes has no subagent lifecycle API; update Hermes to use Heats")
    _load_check_env()
    run_id = new_run_id()
    cfg.run_id = run_id
    runner = Runner(LifecycleLauncher(_CTX, toolsets), _ISOLATION, _data_dir(),
                    progress=lambda m: logger.info(m), on_update=_publish)
    entry = {"runner": runner, "report": None, "error": None, "done": threading.Event()}

    def race() -> None:
        try:
            entry["report"] = runner.run(cfg, cwd=cwd)
        except gitops.HeatsGitError as exc:
            entry["error"] = str(exc)
        except Exception as exc:  # never crash the agent turn
            logger.exception("heats_run failed")
            entry["error"] = f"heats_run failed: {type(exc).__name__}: {exc}"
        finally:
            entry["done"].set()

    with _RUNS_LOCK:
        _RUNS[run_id] = entry
    # copy the context: the subagent lifecycle finds the parent session through context variables
    threading.Thread(target=contextvars.copy_context().run, args=(race,), name=f"heats-{run_id}",
                     daemon=True).start()
    return _wait_for(run_id, _wait_budget())


def _wait_budget() -> float:
    try:
        return max(5.0, min(3600.0, float(_setting("wait_seconds", DEFAULT_WAIT_S))))
    except (TypeError, ValueError):
        return float(DEFAULT_WAIT_S)


def _final_payload(rep: dict) -> str:
    w = rep.get("winner")
    md = ""
    try:
        md = (Path(rep["run_dir"]) / "scoreboard.md").read_text(encoding="utf-8")
    except (OSError, KeyError):
        pass
    return json.dumps({
        "ok": True, "status": "done", "run_id": rep["run_id"], "winner": w, "scoreboard": md,
        "heats": [{"heat": h["index"], "check_passed": bool(h.get("check") and h["check"].get("ok")),
                   "qualifies": h.get("qualifies"), "files": h.get("files"),
                   "agent_summary": (h.get("agent_summary") or "")[:300]} for h in rep.get("heats", [])],
        "next": ("Show the user the scoreboard, then offer heats_apply for the winner." if w else
                 "Show the user the scoreboard; no attempt passed cleanly."),
    })


def _wait_for(run_id: str, budget_s: float) -> str:
    with _RUNS_LOCK:
        entry = _RUNS.get(run_id)
    if entry is None:
        try:
            return _final_payload(load_report(_data_dir(), run_id))
        except FileNotFoundError:
            return _err(f"no Heats run '{run_id}'")
    entry["done"].wait(timeout=budget_s)
    if entry["done"].is_set():
        if entry["error"]:
            return _err(entry["error"])
        return _final_payload(entry["report"].to_dict())
    live = dict(getattr(entry["runner"], "_live", {}) or {})
    rows = live.get("heats") or []
    judged = sum(1 for r in rows if r.get("status") in ("pass", "fail", "flagged", "error"))
    passing = sum(1 for r in rows if r.get("status") == "pass")
    laps = max([r.get("lap") or 1 for r in rows] or [1])
    elapsed = int(time.time() - (live.get("started_at") or time.time()))
    return json.dumps({
        "ok": True, "status": "running", "run_id": run_id, "elapsed_s": elapsed,
        "progress": f"{len(rows)} attempts, {judged} judged, {passing} passing, up to lap {laps}",
        "next": (f"The race is still running. Call heats_wait with run_id '{run_id}' now to keep waiting. "
                 "Do not edit the repository yourself meanwhile."),
    })


def _handle_wait(args: dict, **kwargs) -> str:
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return _err("run_id is required")
    return _wait_for(run_id, _wait_budget())


def _handle_apply(args: dict, **kwargs) -> str:
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return _err("run_id is required")
    try:
        ok, msg = apply_heat(_data_dir(), run_id, args.get("heat"), bool(args.get("force")))
    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"heats_apply failed: {exc}")
    if ok:
        _mark_live_applied(run_id, args.get("heat"))
    return json.dumps({"ok": ok, "message": msg})


def _mark_live_applied(run_id: str, heat) -> None:
    try:
        path = _data_dir() / "live.json"
        live = json.loads(path.read_text(encoding="utf-8"))
        if live.get("run_id") == run_id:
            live["applied"] = int(heat) if heat else live.get("winner")
            _publish(live)
    except Exception:
        pass


# ------------------------------------------------------- isolation seams
def _lane_for(kwargs: dict):
    return _ISOLATION.find(kwargs.get("task_id"), kwargs.get("session_id")) if len(_ISOLATION) else None


def _mw_tool_request(tool_name: str = "", args: Optional[dict] = None, **kwargs):
    lane = _lane_for(kwargs)
    if lane is not None and lane.blocked:
        return None  # pre_tool_call refuses the call
    if os.environ.get("HEATS_DEBUG") and len(_ISOLATION):
        logger.warning("heats debug: %s task_id=%s session_id=%s lane=%s", tool_name, kwargs.get("task_id"),
                       kwargs.get("session_id"), getattr(lane, "heat", None))
    if lane is None:
        return None
    new = rewrite(tool_name, args or {}, lane)
    if new is None:
        return None
    return {"args": new, "source": PLUGIN, "reason": f"keep heat {lane.heat} inside its worktree"}


_OUR_TOOLS = ("heats_run", "heats_wait", "heats_apply")
_CODE_ARG_KEYS = ("code", "command", "script", "source")
_CALL_IN_CODE = re.compile(r"\b(heats_run|heats_wait|heats_apply)\s*\(")
_CALL_IN_SHELL = re.compile(r"(?:^|[;&|(]\s*|\n\s*)(heats_run|heats_wait|heats_apply)\b")


def _misrouted_call(tool_name: str, args: Optional[dict]) -> Optional[str]:
    """Small models that met heats_run through tool_search sometimes try to "call" it from
    execute_code or the shell, which Hermes blocks (measured: 7 of 20 bench runs, 4B model).
    Name the mistake and the right call instead of letting the turn end in a refusal."""
    if tool_name not in ("execute_code", "terminal") or not isinstance(args, dict):
        return None
    text = " ".join(str(args.get(k) or "") for k in _CODE_ARG_KEYS)
    # only an attempted call: `heats_run(...)` in code, or `heats_run ...` as a shell command;
    # `grep heats_run` and the like stay allowed
    pat = _CALL_IN_CODE if tool_name == "execute_code" else _CALL_IN_SHELL
    m = pat.search(text)
    if not m:
        return None
    hit = m.group(1)
    return (f"Heats: `{hit}` is a Hermes tool, not a Python function or shell command, so it cannot run "
            f"inside {tool_name}. Call it as a tool: if it was found with tool_search, use "
            f'tool_call with calls=[{{"name": "{hit}", "arguments": {{...}}}}] (the arguments tool_describe '
            f"listed); otherwise call {hit} directly.")


def _heats_run_approval(args: Optional[dict], in_attempt: bool = False) -> Optional[dict]:
    """heats_run runs a shell check in the repo and in every attempt worktree, outside Hermes's terminal
    approval. Ask the human first whenever the model chose the check, or the repo is not on the owner's
    trusted list (an auto-detected check still runs repo code: test scripts, conftest.py, Makefiles)."""
    import hashlib
    from .heats_core.checks import detect_check
    a = args if isinstance(args, dict) else {}
    model_check = a.get("check").strip() if isinstance(a.get("check"), str) else ""
    cwd = os.path.expanduser(a.get("repo_path") or _cwd()) if isinstance(a.get("repo_path") or "", str) else ""
    root = gitops.repo_root(cwd) if cwd and os.path.isdir(cwd) else None
    if not root:
        return None  # the tool itself refuses: not a git repo
    root = os.path.realpath(root)
    if not model_check and not in_attempt and _is_trusted(root):
        return None
    check = model_check or _setting("check", "") or detect_check(root)
    if not check:
        return None  # nothing to run; the tool reports that no check was found
    who = ("the model chose this check" if model_check else
           "requested from inside a Heats attempt" if in_attempt else "auto-detected; this repo is not on the trusted list")
    return {"action": "approve",
            "message": f"heats_run will run `{check}` in {root} and in each attempt's git worktree ({who})",
            "rule_key": "heats_run:" + hashlib.sha256(f"{root}\0{check}".encode()).hexdigest()[:16]}


def _on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs):
    lane = _lane_for(kwargs)
    if tool_name == "heats_run":  # also inside an attempt: adding heats to `toolsets` must not skip the prompt
        if lane is not None and lane.blocked:
            return {"action": "block", "message": f"Heats: the run for attempt {lane.heat} has ended; stop now."}
        try:
            return _heats_run_approval(args, in_attempt=lane is not None)
        except Exception as exc:  # hooks fail open: an error here must refuse, not allow
            logger.warning("heats: heats_run approval check failed: %s", exc)
            return {"action": "block", "message": f"Heats could not check this heats_run call safely ({type(exc).__name__})."}
    if lane is None:
        msg = _misrouted_call(tool_name, args)
        return {"action": "block", "message": msg} if msg else None
    if lane.blocked:
        return {"action": "block", "message": f"Heats: the run for attempt {lane.heat} has ended; stop now."}
    try:
        msg = _lane_refusal(tool_name, args if isinstance(args, dict) else {}, lane)
    except Exception as exc:  # Hermes runs hooks fail-open: an error here must refuse, not allow
        logger.warning("heats: pre_tool_call check failed: %s", exc)
        msg = f"Heats could not check this {tool_name} call safely ({type(exc).__name__}); rephrase it more simply."
    return {"action": "block", "message": msg} if msg else None


def _lane_refusal(tool_name: str, args: dict, lane) -> Optional[str]:
    """Why a heat's tool call must not run, or None."""
    from .heats_core import isolation as _iso
    wt = os.path.realpath(lane.worktree)
    if tool_name == "terminal":
        cmd = args.get("command")
        workdir = args.get("workdir") or lane.default_dir()
        hit = _iso.git_guard(cmd, workdir)
        if hit:
            sub, why = hit
            return (f"Heats: refused `{sub}` in attempt {lane.heat}: {why}. An attempt may only edit files and use "
                    "local git commands (status, diff, log, add, commit, reset, restore, ...). Edit files and run the "
                    "check instead.")
        esc = _iso.escaping_path(cmd, workdir, lane)
        if esc:
            return (f"Heats: `{esc}` leads out of attempt {lane.heat}'s worktree into the user's own checkout (or "
                    f"another attempt). Work inside {lane.worktree}; use paths relative to it.")
        return None
    if tool_name not in _WRITE_TOOLS:
        return None
    paths = []
    p = args.get("path")
    if isinstance(p, str) and p:
        paths.append(p)
    v4a = _iso.v4a_paths(args.get("patch"))
    for vp in v4a:
        if not os.path.isabs(os.path.expanduser(vp)):
            # Hermes would resolve it against the session cwd (the user's checkout); the middleware makes
            # every header absolute, so a relative one here means it did not run.
            return f"Heats: attempt {lane.heat}: use absolute paths under {lane.worktree} in patch headers."
    for path in paths + v4a:
        real = os.path.realpath(os.path.expanduser(path))
        if not (real == wt or real.startswith(wt + os.sep)):
            return f"Heats: attempt {lane.heat} may only write inside {lane.worktree} (refused: {path})"
    return None


def _on_post_tool_call(tool_name: str = "", result: Any = None, **kwargs):
    if tool_name != "terminal":
        return None
    lane = _lane_for(kwargs)
    if lane is not None:
        note_terminal_result(result, lane)
    return None


# ------------------------------------------------------------ slash + CLI
USAGE = ("Usage: /heats [list] | /heats show <run_id> [heat] | /heats apply <run_id> [heat] [--force] | "
         "/heats trust [repo] | /heats untrust [repo]\n"
         "Start a run by asking the agent, e.g. \"use heats (3) to fix the failing test in tests/test_dates.py\".")


def _cmd(raw: str = "", **_) -> str:
    parts = (raw or "").split()
    sub = parts[0].lower() if parts else "list"
    data = _data_dir()
    try:
        if sub in ("list", "ls"):
            runs = list_runs(data)
            if not runs:
                return "No Heats runs yet.\n" + USAGE
            rows = ["| run | repo | heats | winner | check |", "|---|---|---|---|---|"]
            for r in runs:
                rows.append(f"| {r['run_id']} | {Path(r['root']).name} | {len(r['heats'])} | "
                            f"{r['winner'] or '-'} | `{r['check']}` |")
            return "\n".join(rows)
        if sub == "show" and len(parts) >= 2:
            rep = load_report(data, parts[1])
            if len(parts) >= 3:
                h = next((x for x in rep["heats"] if str(x["index"]) == parts[2]), None)
                if not h:
                    return f"No heat {parts[2]} in {parts[1]}"
                patch = Path(h["patch_file"]).read_text(encoding="utf-8", errors="replace") if h.get("patch_file") else ""
                tail = (h.get("check") or {}).get("tail", "")
                return (f"Heat {h['index']} ({h['strategy']}, {h.get('model') or 'default'}): "
                        f"{h.get('agent_summary') or ''}\n\n```diff\n{patch[:6000]}\n```\n\nCheck output (tail):\n```\n{tail[-1500:]}\n```")
            return (Path(rep["run_dir"]) / "scoreboard.md").read_text(encoding="utf-8")
        if sub in ("trust", "untrust"):
            return _trust(" ".join((raw or "").split()[1:]).strip(), add=sub == "trust")
        if sub == "apply" and len(parts) >= 2:
            heat = next((int(p) for p in parts[2:] if p.isdigit()), None)
            ok, msg = apply_heat(data, parts[1], heat, "--force" in parts)
            if ok:
                _mark_live_applied(parts[1], heat)
            return msg
    except FileNotFoundError as exc:
        return str(exc)
    return USAGE


def _cli_setup(sub):
    sp = sub.add_subparsers(dest="heats_command")
    sp.add_parser("list", help="List recent runs")
    s = sp.add_parser("show", help="Show a run's scoreboard, or one heat's patch")
    s.add_argument("run_id")
    s.add_argument("heat", nargs="?")
    a = sp.add_parser("apply", help="Apply a heat's patch (default: the winner) to the repo's working tree")
    a.add_argument("run_id")
    a.add_argument("heat", nargs="?", type=int)
    a.add_argument("--force", action="store_true")
    for name, text in (("trust", "Let Heats run the check in this repo without asking"),
                       ("untrust", "Remove a repo from the trusted list")):
        t = sp.add_parser(name, help=text)
        t.add_argument("repo", nargs="?", default=".")
    sub.set_defaults(func=_cli)


def _cli(args) -> None:
    c = getattr(args, "heats_command", None) or "list"
    if c in ("trust", "untrust"):
        print(_trust(os.path.abspath(getattr(args, "repo", ".") or "."), add=c == "trust"))
        return
    raw = " ".join(str(x) for x in [c, getattr(args, "run_id", None), getattr(args, "heat", None)] if x)
    if getattr(args, "force", False):
        raw += " --force"
    print(_cmd(raw))


# --------------------------------------------------------------- register

def _hook_timeout() -> float:
    """Hermes's per-callback hook limit (plugins.hook_callback_timeout, default 30 s)."""
    try:
        from hermes_cli.config import read_raw_config
        v = ((read_raw_config() or {}).get("plugins") or {}).get("hook_callback_timeout")
        return float(v) if v else 30.0
    except Exception:
        return 30.0


def _max_verify_nudges() -> int:
    """Hermes's ``agent.max_verify_nudges`` from the public config reader (core default 3)."""
    try:
        from hermes_cli.config import read_raw_config
        v = ((read_raw_config() or {}).get("agent") or {}).get("max_verify_nudges", 3)
        return max(0, int(3 if v is None else v))
    except Exception:
        return 3


def _publish_referee(verdict: dict) -> None:
    """Live view of the referee (its message to the agent is not shown in the chat): keep the last few
    verdicts in plugin-data for the pane's REST read, and push plugin.heats.referee. Never raises."""
    try:
        path = _data_dir() / "referee_live.json"
        try:
            items = json.loads(path.read_text(encoding="utf-8")).get("verdicts", [])
        except (OSError, ValueError):
            items = []
        items = (items + [verdict])[-6:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"verdicts": items}, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN, "referee", verdict)
    except Exception:
        pass


_NOTED: set = set()  # (session, repo) pairs already told that the repo is not approved


def _untrusted_note(session_id: str, info: dict) -> Optional[dict]:
    """Tell the agent once per session and repo why the tests were not run, so it can tell the user how to
    approve. Never a second time: the agent must not loop on this."""
    key = (session_id or "", info.get("root") or "")
    if key in _NOTED:
        return None
    _NOTED.add(key)
    return {"action": "continue", "message": (
        f"Heats referee (note, not an error): the tests in {info.get('repo')} were not run, because the owner has "
        f"not approved running this repository's check (`{info.get('check')}`) yet. You cannot approve it. Finish as "
        f"you would, and end your answer with one line telling the user: to let Heats run `{info.get('check')}` "
        f"after each code edit in this repository, type /heats trust {info.get('root')}")}


def _load_check_env() -> None:
    from .heats_core import checks as _checks
    raw = _setting("check_env", []) or []
    _checks.EXTRA_ENV = [n for n in ([raw] if isinstance(raw, str) else raw) if isinstance(n, str) and n]


def _on_pre_verify(changed_paths: Optional[list] = None, attempt: int = 0, session_id: str = "", **kwargs):
    """Referee: the main agent edited code and wants to finish; run the check, send it back on red."""
    if not _setting("referee", True):
        return None
    _load_check_env()
    try:
        # Hermes abandons a pre_verify callback after plugins.hook_callback_timeout (default 30 s); the check
        # must finish well inside that, or its verdict never reaches the agent.
        timeout = min(float(_setting("referee_timeout_seconds", 25)), max(5.0, _hook_timeout() - 5.0))
        info: dict = {}
        out = _referee.review(list(changed_paths or []), int(attempt or 0), _max_verify_nudges(),
                              check=_setting("check", "") or None, timeout_s=timeout, info=info,
                              allowed=_is_trusted)
        if info.get("untrusted"):
            out = _untrusted_note(session_id, info)
        rec = {"ts": time.time(), "session_id": session_id, "attempt": attempt, "nudged": bool(out),
               "paths": list(changed_paths or [])[:20], **{k: v for k, v in info.items() if k != "check"}}
        try:  # logging must never cost the agent its nudge
            with open(_data_dir() / "referee.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass
        if info.get("ran"):
            _publish_referee({"ts": rec["ts"], "nudged": bool(out), **info})
        return out
    except Exception as exc:  # never break the user's turn
        logger.warning("heats referee failed: %s", exc)
        return None

def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_tool(name="heats_run", toolset="heats", schema=HEATS_RUN, handler=_handle_run, emoji="🏁")
    ctx.register_tool(name="heats_wait", toolset="heats", schema=HEATS_WAIT, handler=_handle_wait, emoji="🏁")
    ctx.register_tool(name="heats_apply", toolset="heats", schema=HEATS_APPLY, handler=_handle_apply, emoji="🏁")
    ctx.register_middleware("tool_request", _mw_tool_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_verify", _on_pre_verify)
    ctx.register_command("heats", _cmd, description="List, show and apply Heats runs", args_hint="[list|show|apply] [run_id] [heat]")
    ctx.register_cli_command(name="heats", help="List, show and apply Heats runs", setup_fn=_cli_setup, handler_fn=_cli)
    skill = Path(__file__).parent / "skills" / "heats" / "SKILL.md"
    if skill.is_file():
        try:
            ctx.register_skill("heats", skill)
        except Exception as exc:
            logger.debug("heats: skill registration skipped: %s", exc)
