"""Keep each heat inside its own worktree.

Hermes children start in the parent's working directory, and the public subagent API does
not take a working directory yet. So Heats registers a ``tool_request`` middleware: for a
tool call made by a heat's child agent, it rewrites the arguments so the call lands in that
heat's worktree (relative paths are resolved there; absolute paths into the main checkout
are mapped onto the same file in the worktree; terminal commands get the worktree as their
working directory). Calls from any other agent pass through untouched.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

PATH_TOOLS = {"read_file": "path", "write_file": "path", "patch": "path", "search_files": "path"}
TERMINAL_TOOLS = {"terminal"}


@dataclass
class Lane:
    root: str          # main checkout (realpath)
    worktree: str      # this heat's worktree (realpath)
    heat: int
    redirects: int = 0      # calls that aimed at the main checkout and were moved into the worktree
    cwd: str = ""           # last directory the heat's terminal reported (inside the worktree)
    blocked: bool = False   # the run ended while this child was still alive: refuse all its tool calls

    def default_dir(self) -> str:
        return self.cwd if self.cwd and _inside(self.cwd, self.worktree) else self.worktree


class IsolationMap:
    def __init__(self) -> None:
        self._lanes: dict[str, Lane] = {}
        self._lock = threading.Lock()

    def add(self, key: str, lane: Lane) -> None:
        with self._lock:
            self._lanes[key] = lane

    def remove(self, key: str) -> Optional[Lane]:
        with self._lock:
            return self._lanes.pop(key, None)

    def block(self, key: str) -> None:
        with self._lock:
            lane = self._lanes.get(key)
            if lane is not None:
                lane.blocked = True

    def find(self, *keys: Any) -> Optional[Lane]:
        with self._lock:
            for k in keys:
                if k and str(k) in self._lanes:
                    return self._lanes[str(k)]
        return None

    def __len__(self) -> int:
        return len(self._lanes)


def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def map_path(p: str, lane: Lane, base: Optional[str] = None) -> str:
    """Where *p* should point for this heat (relative paths resolve against *base*)."""
    if not isinstance(p, str) or not p:
        return p
    expanded = os.path.expanduser(p)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base or lane.default_dir(), expanded)
        # falls through: a relative path that climbs out ("../../src/x.py") is mapped like an absolute one
    norm = os.path.normpath(expanded)
    real = os.path.realpath(norm)
    for cand in (norm, real):
        if _inside(cand, lane.worktree):
            return cand
    for cand in (norm, real):
        if _inside(cand, lane.root):
            rel = os.path.relpath(cand, lane.root)
            if rel == ".heats" or rel.startswith(".heats" + os.sep):  # another heat's worktree
                return lane.worktree
            return os.path.normpath(os.path.join(lane.worktree, rel))
    return norm


def _rewrite_command(cmd: str, lane: Lane) -> str:
    """Point absolute references to the main checkout at the worktree instead."""
    if not isinstance(cmd, str) or lane.root not in cmd:
        return cmd
    wt = lane.worktree
    # Replace the root only at a path boundary and only where it is not already the worktree
    # (the worktree lives under <root>/.heats/, so a plain replace would corrupt it).
    pattern = re.compile(re.escape(lane.root) + r"(?=$|[/\s'\";|&)])")

    def sub(m: re.Match) -> str:
        rest = cmd[m.end():]
        wt_suffix = wt[len(lane.root):]
        if rest.startswith(wt_suffix):
            return m.group(0)
        return wt

    return pattern.sub(sub, cmd)


def rewrite(tool_name: str, args: dict, lane: Lane) -> Optional[dict]:
    """New args for this heat's tool call, or None when nothing changes."""
    if not isinstance(args, dict):
        return None
    new = dict(args)
    escaped = False
    if tool_name in PATH_TOOLS:
        key = PATH_TOOLS[tool_name]
        if tool_name == "search_files" and not new.get(key):
            new[key] = "."
        if key in new:
            before = new[key]
            new[key] = map_path(before, lane)
            escaped = _aims_at_root(before, lane)
    elif tool_name in TERMINAL_TOOLS:
        wd = new.get("workdir")
        if wd:
            escaped = _aims_at_root(wd, lane)
            new["workdir"] = map_path(wd, lane)
        else:
            new["workdir"] = lane.default_dir()
        if not _inside(new["workdir"], lane.worktree):
            new["workdir"] = lane.worktree
        cmd = new.get("command", "")
        new["command"] = _rewrite_command(cmd, lane)
        escaped = escaped or new["command"] != cmd
    else:
        return None
    if new == args:
        return None
    if escaped:
        lane.redirects += 1
    return new


def _aims_at_root(p: Any, lane: Lane) -> bool:
    """True when *p* is an absolute path into the main checkout but outside the worktree."""
    if not isinstance(p, str) or not p:
        return False
    e = os.path.expanduser(p)
    if not os.path.isabs(e):
        return False
    n = os.path.normpath(e)
    return _inside(n, lane.root) and not _inside(n, lane.worktree)


def note_terminal_result(result: Any, lane: Lane) -> None:
    """Remember the directory a heat's terminal ended in, so the next call starts there."""
    import json
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if isinstance(cwd, str) and _inside(os.path.normpath(cwd), lane.worktree):
        lane.cwd = os.path.normpath(cwd)


# git state that every worktree shares with the user's checkout: a heat must not change it.
_SHARED_GIT = re.compile(
    r"(?:^|[;&|(]\s*|\bthen\s+|\bdo\s+)git(?:\s+-[cC]\s+\S+)*\s+"
    r"(stash|config|worktree|gc|prune|push|remote|update-ref|reflog|filter-branch|filter-repo|notes|"
    r"submodule|lfs|branch\s+(?:.*\s)?(?:-[dDmMcC]\b|--delete|--move|--copy|--force|-f\b)|tag\s+(?:.*\s)?(?:-d\b|--delete)|"
    r"checkout\s+(?:.*\s)?-B\b|switch\s+(?:.*\s)?-C\b)",
    re.I,
)


def shared_git_command(command: Any) -> Optional[str]:
    """The git subcommand in *command* that would change state shared with the user's checkout, or None."""
    if not isinstance(command, str) or "git" not in command:
        return None
    m = _SHARED_GIT.search(command)
    return m.group(1).split()[0] if m else None
