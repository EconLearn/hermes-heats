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


def _root_spellings(lane: Lane) -> list[tuple[str, str]]:
    """(spelling of the main checkout as it may appear in a shell command, same spelling of the worktree).

    Covers the realpath, the macOS ``/tmp`` -> ``/private/tmp`` style aliases, a ``~/`` form and the
    backslash-escaped form of each (``/Users/j/algo\\ tester``)."""
    root, wt = lane.root.rstrip(os.sep) or os.sep, lane.worktree
    under = wt.startswith(root + os.sep)
    plain: list[tuple[str, str]] = [(root, wt)]
    for alias in ("/private/tmp", "/private/var", "/private/etc"):
        if root.startswith(alias + os.sep) or root == alias:
            short = root[len("/private"):]
            plain.append((short, short + wt[len(root):] if under else wt))
    for home in {os.path.expanduser("~"), os.path.realpath(os.path.expanduser("~"))}:
        home = home.rstrip(os.sep)
        if home and home != os.sep:
            for spell, target in list(plain):
                if spell.startswith(home + os.sep):
                    t = "~" + target[len(home):] if target.startswith(home + os.sep) else target
                    plain.append(("~" + spell[len(home):], t))
    out: list[tuple[str, str]] = []
    for spell, target in plain:
        out.append((spell, target))
        esc = re.sub(r"([ \t'\"()&;|$`])", r"\\\1", spell)
        if esc != spell:
            out.append((esc, re.sub(r"([ \t'\"()&;|$`])", r"\\\1", target)))
    seen, uniq = set(), []
    for spell, target in sorted(out, key=lambda x: -len(x[0])):
        if spell not in seen:
            seen.add(spell)
            uniq.append((spell, target))
    return uniq


def _rewrite_command(cmd: str, lane: Lane) -> str:
    """Point references to the main checkout at the worktree instead (absolute, ``~/``, ``/tmp``
    alias and backslash-escaped spellings)."""
    if not isinstance(cmd, str) or not cmd:
        return cmd
    spellings = [(s, t) for s, t in _root_spellings(lane) if s in cmd]
    if not spellings:
        return cmd
    targets = dict(spellings)
    # Replace only at a path boundary, and not where the text already names the worktree (the
    # worktree lives under <root>/.heats/, so a plain replace would corrupt it).
    pattern = re.compile(r"(?<![\w.~/\\-])(" + "|".join(re.escape(s) for s, _ in spellings)
                         + r")(?=$|[/\s'\";|&)<>`])")

    def sub(m: re.Match) -> str:
        spell = m.group(1)
        target = targets[spell]
        suffix = target[len(spell):] if target.startswith(spell) else ""
        if suffix and cmd[m.end():].startswith(suffix):
            return spell
        return target

    return pattern.sub(sub, cmd)


# V4A patch headers, as Hermes's patch tool reads them (tools/file_tools.py): group 1 = prefix, 3 = path.
_V4A_SINGLE = re.compile(r"^(\*\*\*\s*(Update|Add|Delete)\s+File:\s*)(.+)$", re.M)
_V4A_MOVE = re.compile(r"^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+)$", re.M)


def v4a_paths(patch: Any) -> list[str]:
    """Every file path named in a V4A patch's headers."""
    if not isinstance(patch, str):
        return []
    out = [m.group(3).strip() for m in _V4A_SINGLE.finditer(patch)]
    for m in _V4A_MOVE.finditer(patch):
        out += [m.group(2).strip(), m.group(3).strip()]
    return [p for p in out if p]


def _rewrite_v4a(patch: str, lane: Lane) -> tuple[str, bool]:
    """Map every V4A header path into the worktree (relative ones resolve in the heat's directory,
    not in the session cwd Hermes inherited from the parent). Returns (patch, aimed_at_root)."""
    hit = [False]

    def one(raw: str) -> str:
        raw = raw.strip()
        if _aims_at_root(raw, lane):
            hit[0] = True
        return map_path(raw, lane)

    patch = _V4A_SINGLE.sub(lambda m: m.group(1) + one(m.group(3)), patch)
    patch = _V4A_MOVE.sub(lambda m: f"{m.group(1)}{one(m.group(2))} -> {one(m.group(3))}", patch)
    return patch, hit[0]


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
        if tool_name == "patch" and isinstance(new.get("patch"), str) and v4a_paths(new["patch"]):
            new["patch"], v4a_escaped = _rewrite_v4a(new["patch"], lane)
            escaped = escaped or v4a_escaped
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


# ------------------------------------------------------------------ shell parsing
# A heat's terminal command is parsed into shell segments (split at newlines, ; & && || | ( ) and
# backticks), nested scripts (`bash -c '...'`, `eval`, `$(...)`) are parsed too, and every git
# invocation is judged by its real subcommand, whatever comes before it (`/usr/bin/git`, `env git`,
# `xargs git`, `git --no-pager -C x -c k=v ...`). When a command cannot be parsed it is judged
# conservatively.
import shlex

_PUNCT = ";&|()<>`\n"
_SEP_CHARS = set(";&|()`\n")


class _WordChars(str):
    """shlex word characters: everything that is not whitespace, punctuation, a quote or a backslash
    (so non-ASCII names, ``$VAR`` and ``a=b`` stay one word)."""

    def __contains__(self, c: object) -> bool:  # type: ignore[override]
        return isinstance(c, str) and len(c) == 1 and c not in _PUNCT and c not in " \t\r'\"\\"


def _tokens(cmd: str) -> Optional[list[str]]:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.commenters = ""
        lex.wordchars = _WordChars()
        return list(lex)
    except ValueError:  # unbalanced quotes and the like
        return None


def _is_sep(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok) and any(c in _SEP_CHARS for c in tok) \
        and not tok.startswith((">", "<"))


def _segments(tokens: list[str]) -> list[list[str]]:
    segs, cur = [], []
    for t in tokens:
        if _is_sep(t):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KEYWORDS = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time", "case", "esac"}
_WRAPPERS = {"env", "command", "exec", "sudo", "doas", "nohup", "time", "xargs", "nice", "timeout", "gtimeout",
             "stdbuf", "ionice", "caffeinate", "watch", "parallel", "find", "unbuffer", "chronic", "flock", "setsid",
             "builtin", "noglob", "arch", "script", "strace", "ltrace", "dtruss", "taskset", "chrt", "su", "runuser"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "ash", "busybox"}
_INTERPRETERS = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|perl|ruby|php|osascript|tclsh)$")
_GIT_ENV = re.compile(r"(?<![A-Za-z0-9_])GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|NAMESPACE|OBJECT_DIRECTORY|CONFIG[A-Z_]*)\s*=")


def _cmd_index(seg: list[str]) -> int:
    """Index of the command word in a segment (after VAR=x assignments and shell keywords)."""
    i = 0
    while i < len(seg) and (seg[i] in _KEYWORDS or _ASSIGN.match(seg[i])):
        i += 1
    return i


def _base(tok: str) -> str:
    return os.path.basename(tok.rstrip("/")) if tok else ""


# git subcommands that only read, or only change this heat's own worktree, index and heats/* branch.
_GIT_SAFE = {
    "status", "diff", "log", "show", "add", "rm", "mv", "restore", "commit", "grep", "blame", "annotate",
    "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file", "describe", "shortlog", "apply", "am",
    "format-patch", "range-diff", "merge-base", "merge-file", "merge-tree", "diff-files", "diff-index", "diff-tree",
    "show-ref", "for-each-ref", "name-rev", "check-ignore", "check-attr", "check-ref-format", "count-objects", "fsck",
    "hash-object", "mktree", "write-tree", "read-tree", "commit-tree", "checkout-index", "clean", "reset", "revert",
    "cherry-pick", "merge", "rebase", "bisect", "cherry", "whatchanged", "stripspace", "interpret-trailers", "var",
    "version", "help", "archive", "clone", "difftool", "verify-commit", "verify-tag", "show-branch", "patch-id",
    "ls-remote", "request-pull", "fmt-merge-msg", "mailinfo", "mailsplit", "column", "unpack-file",
}
_GIT_VALUE_OPTS = {"-C", "-c", "--exec-path", "--super-prefix", "--list-cmds", "--attr-source"}
_GIT_FORBIDDEN_OPTS = ("--git-dir", "--work-tree", "--namespace", "--config-env")


def _positionals(args: list[str], value_opts: frozenset = frozenset()) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            continue
        if a.startswith("-"):
            skip = a in value_opts
            continue
        out.append(a)
    return out


_BRANCH_WRITE = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                 "--unset-upstream", "--edit-description", "-t", "--track", "--no-track", "--create-reflog"}
_BRANCH_LIST = {"-l", "--list", "-a", "--all", "-r", "--remotes", "--show-current", "-v", "-vv", "--verbose"}
_BRANCH_VALUE = frozenset({"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                           "--format", "--abbrev", "--color", "--column"})


def _is_local_branch(name: str, cwd: Optional[str]) -> bool:
    if not cwd or not os.path.isdir(cwd):
        return True  # cannot tell: assume it is one
    import subprocess
    try:
        cp = subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/" + name], cwd=cwd,
                            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        return cp.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def _git_verdict(args: list[str], cwd: Optional[str]) -> Optional[tuple[str, str]]:
    """(subcommand, why) when this git invocation would change state shared with the user's checkout."""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith(_GIT_FORBIDDEN_OPTS):
            name = a.split("=", 1)[0]
            return name, f"`git {name}` can point git at the user's own checkout"
        if a in _GIT_VALUE_OPTS:
            val = args[i + 1] if i + 1 < len(args) else ""
            if a == "-c" and val.lower().startswith(("alias.", "include.", "includeif.")):
                return "config", "`git -c alias.*` can run any command"
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return None
    sub, rest = args[i].lower(), args[i + 1:]
    first = rest[0] if rest else ""
    pos = _positionals(rest)
    if sub == "rev-parse" and {"--git-common-dir", "--show-superproject-working-tree"} & set(rest):
        return "rev-parse", "it reveals the user's own checkout, which an attempt must not reach"
    if sub in _GIT_SAFE:
        return None
    if sub == "stash" and first in ("list", "show"):
        return None
    if sub == "worktree" and first == "list":
        return None
    if sub == "remote" and (not rest or first in ("-v", "--verbose", "show", "get-url")):
        return None
    if sub == "reflog" and (not rest or first in ("show", "exists") or first.startswith("-")):
        return None
    if sub == "notes" and first in ("list", "show"):
        return None
    if sub == "symbolic-ref" and not ({"-d", "--delete"} & set(rest)) and len(pos) <= 1:
        return None
    if sub == "config":
        writes = {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section", "-e",
                  "--edit", "set", "unset", "rename-section", "remove-section", "edit"}
        reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list"}
        if not (writes & set(rest)) and ((reads & set(rest)) or len(pos) <= 1):
            return None
        return "config", "`git config` writes the configuration every worktree shares"
    if sub == "branch":
        if _BRANCH_WRITE & set(rest) or any(a.startswith("--set-upstream-to") for a in rest):
            return "branch", "renaming, deleting or moving branches changes the user's branches"
        if _BRANCH_LIST & set(rest) or not _positionals(rest, _BRANCH_VALUE):
            return None
        return "branch", "a new branch would land in the user's repository"
    if sub == "tag":
        tag_writes = {"-d", "--delete", "-f", "--force", "-a", "--annotate", "-s", "--sign", "-u", "-m", "-F"}
        tag_value = frozenset({"--contains", "--no-contains", "--points-at", "--merged", "--no-merged", "--sort",
                               "--format", "--color", "--column"})
        if not (tag_writes & set(rest)) and ({"-l", "--list", "-v", "--verify"} & set(rest)
                                             or not _positionals(rest, tag_value)):
            return None
        return "tag", "tags are shared with the user's repository"
    if sub == "checkout":
        if {"-b", "-B", "--orphan", "-t", "--track"} & set(rest):
            return "checkout", "a new branch would land in the user's repository"
        if "--" in rest or {"-p", "--patch"} & set(rest):
            return None
        targets = [p for p in pos if not (cwd and os.path.lexists(os.path.join(cwd, p)))]
        if not targets:
            return None
        name = targets[0]
        if name.startswith("heats/") or name in ("HEAD", ".") or not _is_local_branch(name, cwd):
            return None
        return "checkout", (f"checking out the branch `{name}` would let commits land on the user's branch; "
                            "use `git checkout -- <file>` or `git restore <file>` for files")
    if sub == "switch":
        if {"--detach", "-d"} & set(rest) and not ({"-c", "-C", "--create", "--force-create", "--orphan"} & set(rest)):
            return None
        return "switch", "switching branches would let commits land on the user's branch"
    known = {"stash", "worktree", "remote", "reflog", "notes", "symbolic-ref", "gc", "prune", "push", "fetch",
             "pull", "update-ref", "update-index", "filter-branch", "filter-repo", "submodule", "lfs", "replace",
             "pack-refs", "repack", "maintenance", "init", "sparse-checkout", "send-email", "credential", "hook"}
    if sub in known:
        return sub, f"`git {sub}` changes state shared with the user's checkout"
    return sub, (f"`git {sub}` is not on Heats' list of git commands that are safe in an attempt "
                 "(status, diff, log, show, add, rm, mv, restore, commit, reset, grep, ...)")


_GIT_WORDS = ("stash", "config", "worktree", "gc", "prune", "push", "fetch", "pull", "remote", "update-ref",
              "reflog", "filter-branch", "filter-repo", "notes", "submodule", "lfs", "branch", "tag", "checkout",
              "switch", "symbolic-ref", "update-index", "replace", "init", "maintenance", "pack-refs", "repack")
_LOOSE_GIT = re.compile(r"(?<![\w.-])git(?![\w.-])[\s'\",\[\]\\]+(?:-\S+[\s'\",\]]+)*(" + "|".join(map(re.escape, _GIT_WORDS)) + r")\b",
                        re.I)


def _loose(text: str) -> Optional[tuple[str, str]]:
    m = _LOOSE_GIT.search(text)
    return (m.group(1).lower(), f"`git {m.group(1).lower()}` changes state shared with the user's checkout") if m else None


def git_guard(command: Any, cwd: Optional[str] = None, _depth: int = 0) -> Optional[tuple[str, str]]:
    """(subcommand, reason) when *command* runs git in a way that would change state shared with the
    user's checkout (stash, branches, tags, config, other worktrees, remotes, ...), else None."""
    if not isinstance(command, str) or not command:
        return None
    m = _GIT_ENV.search(command)
    if m:
        return "GIT_" + m.group(1), f"setting GIT_{m.group(1)} can point git at the user's own checkout"
    toks = _tokens(command)
    if toks is None:
        return _loose(command)
    # judged on the shell words (quotes and backslashes removed: g''it, gi\\t, "git"), not the raw text
    joined = " ".join(toks).lower()
    if "git" not in joined and "$" not in joined:
        return None
    if _depth > 4:
        return _loose(command)
    for tok in toks:  # command substitutions inside a quoted word run too
        if ("$(" in tok or "`" in tok) and "git" in tok.lower() and len(toks) > 1:
            hit = git_guard(tok, cwd, _depth + 1)
            if hit:
                return hit
    for seg in _segments(toks):
        j = _cmd_index(seg)
        if j < len(seg) and "$" in seg[j]:
            # the command word is a variable ($G, ${G}, $(...)): it may be git; judge its words conservatively
            words = {w.lower() for w in seg[j + 1:]}
            hit_word = next((w for w in _GIT_WORDS if w in words), None)
            if hit_word:
                return hit_word, (f"the command word `{seg[j]}` is a variable and `{hit_word}` would change state "
                                  "shared with the user's checkout if it is git; spell `git` out")
        while j < len(seg):
            b = _base(seg[j]).lower()
            if b in ("git", "git.exe"):
                hit = _git_verdict(seg[j + 1:], cwd)
                if hit:
                    return hit
                break
            if b in _SHELLS:
                for k in range(j + 1, len(seg) - 1):
                    t = seg[k]
                    if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
                        hit = git_guard(seg[k + 1], cwd, _depth + 1)
                        if hit:
                            return hit
                        break
                break
            if b == "eval" or b == "source" or b == ".":
                hit = git_guard(" ".join(seg[j + 1:]), cwd, _depth + 1) if b == "eval" else None
                if hit:
                    return hit
                break
            if _INTERPRETERS.match(b):
                hit = _loose(" ".join(seg[j + 1:]))
                if hit:
                    return hit
                break
            if b in _WRAPPERS:
                # the wrapped command is somewhere after the wrapper's own options: find it
                nxt = next((k for k in range(j + 1, len(seg))
                            if _base(seg[k]).lower() in ("git", "git.exe", "eval") or _base(seg[k]).lower() in _SHELLS
                            or _INTERPRETERS.match(_base(seg[k]).lower())), None)
                if nxt is None:
                    break
                j = nxt
                continue
            break
    return None


def shared_git_command(command: Any, cwd: Optional[str] = None) -> Optional[str]:
    """The git subcommand in *command* that would change state shared with the user's checkout, or None."""
    hit = git_guard(command, cwd)
    return hit[0] if hit else None


def _escapes(p: str, cur: str, lane: Lane) -> bool:
    e = os.path.expanduser(p)
    n = os.path.normpath(e if os.path.isabs(e) else os.path.join(cur, e))
    if _inside(n, lane.worktree):
        return False
    for cand in (n, os.path.realpath(n)):
        if _inside(cand, lane.worktree):
            return False
        if _inside(cand, lane.root):
            return True
    return False


def escaping_path(command: Any, workdir: Optional[str], lane: Lane) -> Optional[str]:
    """The first path in a terminal command that lands in the main checkout (or another heat) rather
    than this heat's worktree: ``cd ../../..``, a ``~`` or symlinked spelling the rewrite missed, ...
    ``cd`` is followed from segment to segment. None when every literal path stays inside."""
    if not isinstance(command, str) or not command:
        return None
    toks = _tokens(command)
    if toks is None:
        return None
    cur = workdir if workdir and _inside(os.path.normpath(workdir), lane.worktree) else lane.default_dir()
    for seg in _segments(toks):
        j = _cmd_index(seg)
        words = seg[j:]
        for w in seg:
            for cand in [w] + ([w.split("=", 1)[1]] if "=" in w else []):
                if not cand or "$" in cand or "://" in cand or "`" in cand:
                    continue
                if not ("/" in cand or cand in ("..", "~") or cand.startswith("~")):
                    continue
                if cand.startswith("-") and "=" not in w:
                    continue
                if _escapes(cand, cur, lane):
                    return w
        if words and words[0] in ("cd", "pushd"):
            args = [a for a in words[1:] if not a.startswith("-")]
            if args and "$" not in args[0]:
                e = os.path.expanduser(args[0])
                cur = os.path.normpath(e if os.path.isabs(e) else os.path.join(cur, e))
    return None
