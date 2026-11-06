"""Heats: race N attempts at a coding task in parallel git worktrees; the repo's own tests pick the winner.

Hermes surfaces used (all public): ``ctx.subagent_lifecycle`` to launch the attempts,
``tool_request`` middleware to keep each attempt inside its worktree, ``pre_tool_call`` to
refuse writes that would still land outside it, ``post_tool_call`` to follow each attempt's
terminal directory, plus tools, a slash command, a CLI command and a bundled skill.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .heats_core import gitops
from .heats_core.isolation import IsolationMap, note_terminal_result, rewrite
from .heats_core.runner import MAX_HEATS, RunConfig, Runner, apply_heat, list_runs, load_report

logger = logging.getLogger(__name__)

PLUGIN = "heats"
_ISOLATION = IsolationMap()
_CTX: Any = None
_WRITE_TOOLS = {"write_file", "patch"}


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


def _setting(key: str, default: Any) -> Any:
    try:
        v = _CTX.get_config(key, default=default) if _CTX is not None else default
        return default if v is None else v
    except Exception:
        return default


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
        "feature that has a test command which decides success. Takes minutes; blocks until done. "
        "Do not use it for tasks without an objective check."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The full, self-contained task for every attempt (they see nothing else from this chat): what to change and why, file names, error text."},
            "n": {"type": "integer", "minimum": 1, "maximum": MAX_HEATS, "description": "Number of parallel attempts (default from settings, usually 3)."},
            "check": {"type": "string", "description": "Shell command that exits 0 when the task is solved, e.g. 'python -m pytest -q tests/test_parser.py'. Omit to auto-detect (pytest, npm/pnpm/yarn test, cargo test, go test, make test)."},
            "models": {"type": "array", "items": {"type": "string"}, "description": "Optional model names to race against each other; attempts cycle through them. Omit to use the delegation model."},
            "timeout_minutes": {"type": "number", "description": "Wall-clock limit for the attempts (default from settings, 15)."},
            "repo_path": {"type": "string", "description": "Repository to work on (default: the session's working directory)."},
        },
        "required": ["task"],
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
    models = [m for m in (args.get("models") or _setting("models", []) or []) if isinstance(m, str) and m.strip()]
    timeout_min = float(args.get("timeout_minutes") or _setting("timeout_minutes", 15))
    toolsets = tuple(_setting("toolsets", ["terminal", "file"]) or ()) or None
    cfg = RunConfig(
        task=task, n=n, check=args.get("check") or _setting("check", "") or None, models=models,
        timeout_s=max(30.0, timeout_min * 60), check_timeout_s=float(_setting("check_timeout_seconds", 600)),
        protect_tests=bool(_setting("protect_tests", True)),
        protected_paths=list(_setting("protected_paths", []) or []),
        run_baseline=bool(_setting("run_baseline", True)),
    )
    cwd = os.path.expanduser(args.get("repo_path") or _cwd())
    if not os.path.isdir(cwd):
        return _err(f"{cwd} is not a directory")
    if not gitops.repo_root(cwd):
        return _err(f"{cwd} is not inside a git repository; Heats needs git worktrees (run `git init` and commit once)")
    if getattr(_CTX, "subagent_lifecycle", None) is None:
        return _err("this Hermes has no subagent lifecycle API; update Hermes to use Heats")
    try:
        runner = Runner(LifecycleLauncher(_CTX, toolsets), _ISOLATION, _data_dir(),
                        progress=lambda m: logger.info(m), on_update=_publish)
        report = runner.run(cfg, cwd=cwd)
    except gitops.HeatsGitError as exc:
        return _err(str(exc))
    except Exception as exc:  # never crash the agent turn
        logger.exception("heats_run failed")
        return _err(f"heats_run failed: {type(exc).__name__}: {exc}")
    w = report.winner
    return json.dumps({
        "ok": True, "run_id": report.run_id, "winner": w.index if w else None,
        "scoreboard": report.markdown(),
        "heats": [{"heat": h.index, "check_passed": bool(h.check and h.check.ok), "qualifies": h.qualifies,
                   "files": h.files, "agent_summary": h.agent_summary[:300]} for h in report.heats],
        "next": ("Show the user the scoreboard, then offer heats_apply for the winner." if w else
                 "Show the user the scoreboard; no attempt passed cleanly."),
    })


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
    return json.dumps({"ok": ok, "message": msg})


# ------------------------------------------------------- isolation seams
def _lane_for(kwargs: dict):
    return _ISOLATION.find(kwargs.get("task_id"), kwargs.get("session_id")) if len(_ISOLATION) else None


def _mw_tool_request(tool_name: str = "", args: Optional[dict] = None, **kwargs):
    lane = _lane_for(kwargs)
    if os.environ.get("HEATS_DEBUG") and len(_ISOLATION):
        logger.warning("heats debug: %s task_id=%s session_id=%s lane=%s", tool_name, kwargs.get("task_id"),
                       kwargs.get("session_id"), getattr(lane, "heat", None))
    if lane is None:
        return None
    new = rewrite(tool_name, args or {}, lane)
    if new is None:
        return None
    return {"args": new, "source": PLUGIN, "reason": f"keep heat {lane.heat} inside its worktree"}


def _on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs):
    lane = _lane_for(kwargs)
    if lane is None or tool_name not in _WRITE_TOOLS:
        return None
    p = (args or {}).get("path")
    if isinstance(p, str) and p:
        real = os.path.realpath(os.path.expanduser(p))
        wt = os.path.realpath(lane.worktree)
        if not (real == wt or real.startswith(wt + os.sep)):
            return {"action": "block", "message": f"Heats: attempt {lane.heat} may only write inside {lane.worktree}"}
    return None


def _on_post_tool_call(tool_name: str = "", result: Any = None, **kwargs):
    if tool_name != "terminal":
        return None
    lane = _lane_for(kwargs)
    if lane is not None:
        note_terminal_result(result, lane)
    return None


# ------------------------------------------------------------ slash + CLI
USAGE = ("Usage: /heats [list] | /heats show <run_id> [heat] | /heats apply <run_id> [heat] [--force]\n"
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
                patch = Path(h["patch_file"]).read_text(encoding="utf-8") if h.get("patch_file") else ""
                tail = (h.get("check") or {}).get("tail", "")
                return (f"Heat {h['index']} ({h['strategy']}, {h.get('model') or 'default'}): "
                        f"{h.get('agent_summary') or ''}\n\n```diff\n{patch[:6000]}\n```\n\nCheck output (tail):\n```\n{tail[-1500:]}\n```")
            return (Path(rep["run_dir"]) / "scoreboard.md").read_text(encoding="utf-8")
        if sub == "apply" and len(parts) >= 2:
            heat = next((int(p) for p in parts[2:] if p.isdigit()), None)
            ok, msg = apply_heat(data, parts[1], heat, "--force" in parts)
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
    sub.set_defaults(func=_cli)


def _cli(args) -> None:
    c = getattr(args, "heats_command", None) or "list"
    raw = " ".join(str(x) for x in [c, getattr(args, "run_id", None), getattr(args, "heat", None)] if x)
    if getattr(args, "force", False):
        raw += " --force"
    print(_cmd(raw))


# --------------------------------------------------------------- register
def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_tool(name="heats_run", toolset="heats", schema=HEATS_RUN, handler=_handle_run, emoji="🏁")
    ctx.register_tool(name="heats_apply", toolset="heats", schema=HEATS_APPLY, handler=_handle_apply, emoji="🏁")
    ctx.register_middleware("tool_request", _mw_tool_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_command("heats", _cmd, description="List, show and apply Heats runs", args_hint="[list|show|apply] [run_id] [heat]")
    ctx.register_cli_command(name="heats", help="List, show and apply Heats runs", setup_fn=_cli_setup, handler_fn=_cli)
    skill = Path(__file__).parent / "skills" / "heats" / "SKILL.md"
    if skill.is_file():
        try:
            ctx.register_skill("heats", skill)
        except Exception as exc:
            logger.debug("heats: skill registration skipped: %s", exc)
