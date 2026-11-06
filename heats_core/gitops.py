"""Git plumbing for Heats: worktrees, base snapshots, patches. Standard library only.

Every git call is non-interactive, never runs repository hooks and never raises on a
non-zero exit: callers read ``returncode`` and decide.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GIT_TIMEOUT = 60


class HeatsGitError(RuntimeError):
    """A git step Heats depends on failed; the message is safe to show the user."""


def _env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_EDITOR": "true",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    return env


def git(args: list[str], cwd: str | Path, timeout: int = GIT_TIMEOUT, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run git without hooks, prompts or pagers. Never raises for a non-zero exit."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    try:
        return subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_env(), input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git {' '.join(args[:2])} timed out after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", "git is not installed or not on PATH")


def _must(cp: subprocess.CompletedProcess, what: str) -> str:
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip().splitlines()
        raise HeatsGitError(f"{what} failed: {detail[-1] if detail else 'exit ' + str(cp.returncode)}")
    return cp.stdout


def repo_root(path: str | Path) -> Optional[str]:
    """Top level of the git work tree containing *path*, or None."""
    cp = git(["rev-parse", "--show-toplevel"], cwd=path)
    if cp.returncode != 0:
        return None
    return os.path.realpath(cp.stdout.strip())


def snapshot_base(root: str) -> tuple[str, bool]:
    """Commit to start every heat from: HEAD, or a stash-style commit holding the
    uncommitted changes to tracked files (``git stash create`` does not touch the tree).

    Returns (sha, includes_uncommitted_changes)."""
    head = git(["rev-parse", "--verify", "HEAD"], cwd=root)
    if head.returncode != 0:
        raise HeatsGitError("this repository has no commits yet; commit once before running heats")
    stash = git(["stash", "create", "heats base"], cwd=root)
    sha = stash.stdout.strip() if stash.returncode == 0 else ""
    if sha:
        return sha, True
    return head.stdout.strip(), False


def untracked_files(root: str) -> list[str]:
    cp = git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    return [line for line in cp.stdout.splitlines() if line.strip()] if cp.returncode == 0 else []


def ensure_excluded(root: str, entry: str = "/.heats/") -> None:
    """Keep the heats directory out of ``git status`` without touching the user's .gitignore."""
    cp = git(["rev-parse", "--git-path", "info/exclude"], cwd=root)
    if cp.returncode != 0:
        return
    path = Path(cp.stdout.strip())
    if not path.is_absolute():
        path = Path(root) / path
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if entry not in existing.splitlines():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(entry + "\n")
    except OSError:
        pass


def add_worktree(root: str, path: str, branch: str, base: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _must(git(["worktree", "add", "--quiet", "-b", branch, path, base], cwd=root, timeout=180), "git worktree add")


def copy_untracked(root: str, wt: str, files: list[str], limit_bytes: int = 20_000_000) -> int:
    """Copy untracked (not ignored) files into a heat so it sees the same tree as the user."""
    copied, total = 0, 0
    for rel in files:
        src = Path(root) / rel
        try:
            if not src.is_file() or src.is_symlink():
                continue
            total += src.stat().st_size
            if total > limit_bytes:
                break
            dst = Path(wt) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except OSError:
            continue
    return copied


@dataclass
class Patch:
    text: str
    files: list[str]
    insertions: int
    deletions: int

    @property
    def empty(self) -> bool:
        return not self.text.strip()


_NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


def collect_patch(wt: str, base: str, ignore: tuple[str, ...] = ()) -> Patch:
    """Everything the heat changed relative to *base* (tracked, new and deleted files)."""
    excludes = [f":(exclude,literal){rel}" for rel in ignore]
    _must(git(["add", "-A", "--", ".", *excludes], cwd=wt, timeout=120), "git add in heat")
    text = _must(git(["diff", "--cached", "--binary", base], cwd=wt, timeout=120), "git diff in heat")
    stat = git(["diff", "--cached", "--numstat", base], cwd=wt, timeout=120)
    files, ins, dels = [], 0, 0
    for line in stat.stdout.splitlines():
        m = _NUMSTAT.match(line)
        if not m:
            continue
        files.append(m.group(3))
        ins += int(m.group(1)) if m.group(1) != "-" else 0
        dels += int(m.group(2)) if m.group(2) != "-" else 0
    return Patch(text=text, files=files, insertions=ins, deletions=dels)


def remove_worktree(root: str, path: str, branch: Optional[str] = None) -> None:
    git(["worktree", "remove", "--force", path], cwd=root, timeout=120)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    git(["worktree", "prune"], cwd=root)
    if branch:
        git(["branch", "-D", branch], cwd=root)


def apply_patch(root: str, patch_text: str) -> tuple[bool, str]:
    """Apply a heat's patch to the user's working tree (unstaged), 3-way when needed."""
    if not patch_text.strip():
        return False, "the patch is empty"
    check = git(["apply", "--check", "--whitespace=nowarn", "-"], cwd=root, input_text=patch_text)
    if check.returncode == 0:
        cp = git(["apply", "--whitespace=nowarn", "-"], cwd=root, input_text=patch_text)
        return (cp.returncode == 0, (cp.stderr or "applied").strip())
    cp = git(["apply", "--3way", "--whitespace=nowarn", "-"], cwd=root, input_text=patch_text)
    if cp.returncode == 0:
        # --3way records the result in the index; we leave it there rather than resetting,
        # because a reset would also unstage anything the user had staged.
        return True, "applied with a 3-way merge (changes are staged)"
    return False, (cp.stderr or check.stderr or "git apply failed").strip()


def tree_fingerprint(root: str) -> str:
    """Cheap fingerprint of the user's working tree (status + diff of tracked files), excluding .heats."""
    import hashlib
    st = git(["status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).heats"], cwd=root, timeout=120)
    df = git(["diff", "--binary", "HEAD", "--", ".", ":(exclude).heats"], cwd=root, timeout=120)
    return hashlib.sha256((st.stdout + "\0" + df.stdout).encode("utf-8", "replace")).hexdigest()


DEPENDENCY_DIRS = ("node_modules", ".venv", "venv", ".tox", "vendor/bundle", ".yarn/cache", "bower_components")


def link_dependency_dirs(root: str, wt: str, limit: int = 200) -> list[str]:
    """Symlink ignored dependency folders (node_modules, .venv, ...) from the main checkout into a
    heat, so the check command finds the installed dependencies. Returns the linked paths."""
    cp = git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], cwd=root, timeout=120)
    if cp.returncode != 0:
        return []
    linked = []
    for line in cp.stdout.splitlines():
        rel = line.strip().rstrip("/")
        if not rel or rel.startswith(".heats"):
            continue
        if not any(rel == d or rel.endswith("/" + d) for d in DEPENDENCY_DIRS):
            continue
        src, dst = Path(root) / rel, Path(wt) / rel
        if not src.is_dir() or dst.exists() or dst.is_symlink():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(src, dst, target_is_directory=True)
            linked.append(rel)
        except OSError:
            continue
        if len(linked) >= limit:
            break
    return linked
