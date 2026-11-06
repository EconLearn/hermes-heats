"""Git plumbing for Heats: worktrees, base snapshots, patches. Standard library only.

Every git call is non-interactive, never runs repository hooks and never raises on a
non-zero exit: callers read ``returncode`` and decide.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
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


def git(args: list[str], cwd: str | Path, timeout: int = GIT_TIMEOUT, input_text: Optional[str | bytes] = None,
        env_extra: Optional[dict] = None, binary: bool = False) -> subprocess.CompletedProcess:
    """Run git without hooks, prompts or pagers. Never raises for a non-zero exit.
    ``binary=True`` keeps stdout as bytes (patches must round-trip byte for byte)."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    env = _env()
    if env_extra:
        env.update(env_extra)
    text_kw = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    try:
        cp = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=timeout, env=env, input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL, check=False, **text_kw,
        )
        if binary and isinstance(cp.stderr, bytes):
            cp.stderr = cp.stderr.decode("utf-8", "replace")
        return cp
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, b"" if binary else "", f"git {' '.join(args[:2])} timed out after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, b"" if binary else "", "git is not installed or not on PATH")


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


def snapshot_base(root: str, max_untracked_file_bytes: int = 10_000_000) -> tuple[str, bool]:
    """Commit to start every heat from: the user's working tree exactly as it is (tracked edits AND
    untracked, not-ignored files), written through a temporary index so the user's index, stash
    and branches are never touched. The commit is dangling (no ref points at it).

    Returns (sha, differs_from_HEAD)."""
    head = git(["rev-parse", "--verify", "HEAD"], cwd=root)
    if head.returncode != 0:
        raise HeatsGitError("this repository has no commits yet; commit once before running heats")
    head_sha = head.stdout.strip()
    gitdir = _must(git(["rev-parse", "--absolute-git-dir"], cwd=root), "git rev-parse")
    tmp_index = os.path.join(gitdir.strip(), f"heats-index-{os.getpid()}-{threading.get_ident()}")
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        _must(git(["read-tree", head_sha], cwd=root, env_extra=env), "git read-tree")
        excludes = [":(exclude).heats"]
        for rel in untracked_files(root):
            try:
                if (Path(root) / rel).stat().st_size > max_untracked_file_bytes:
                    excludes.append(f":(exclude,literal){rel}")
            except OSError:
                excludes.append(f":(exclude,literal){rel}")
        _must(git(["add", "-A", "--", ".", *excludes], cwd=root, env_extra=env, timeout=300), "git add (snapshot)")
        tree = _must(git(["write-tree"], cwd=root, env_extra=env), "git write-tree").strip()
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass
    head_tree = git(["rev-parse", head_sha + "^{tree}"], cwd=root).stdout.strip()
    if tree == head_tree:
        return head_sha, False
    sha = _must(git(["commit-tree", tree, "-p", head_sha, "-m", "heats base (your working tree)"], cwd=root,
                    env_extra={"GIT_AUTHOR_NAME": "heats", "GIT_AUTHOR_EMAIL": "heats@localhost",
                               "GIT_COMMITTER_NAME": "heats", "GIT_COMMITTER_EMAIL": "heats@localhost"}),
                "git commit-tree").strip()
    return sha, True


def untracked_files(root: str) -> list[str]:
    cp = git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
    return [f for f in cp.stdout.split("\0") if f] if cp.returncode == 0 else []


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


@dataclass
class Patch:
    data: bytes
    files: list[str]
    insertions: int
    deletions: int

    @property
    def empty(self) -> bool:
        return not self.data.strip()


_NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


def collect_patch(wt: str, base: str, ignore: tuple[str, ...] = ()) -> Patch:
    """Everything the heat changed relative to *base* (tracked, new and deleted files)."""
    excludes = [f":(exclude,literal){rel}" for rel in ignore]
    _must(git(["add", "-A", "--", ".", *excludes], cwd=wt, timeout=120), "git add in heat")
    d = git(["diff", "--cached", "--binary", base], cwd=wt, timeout=120, binary=True)
    if d.returncode != 0:
        raise HeatsGitError(f"git diff in heat failed: {d.stderr.strip()[-200:]}")
    data = d.stdout
    stat = git(["diff", "--cached", "--numstat", base], cwd=wt, timeout=120)
    files, ins, dels = [], 0, 0
    for line in stat.stdout.splitlines():
        m = _NUMSTAT.match(line)
        if not m:
            continue
        files.append(m.group(3))
        ins += int(m.group(1)) if m.group(1) != "-" else 0
        dels += int(m.group(2)) if m.group(2) != "-" else 0
    return Patch(data=data, files=files, insertions=ins, deletions=dels)


def remove_worktree(root: str, path: str, branch: Optional[str] = None) -> None:
    git(["worktree", "remove", "--force", path], cwd=root, timeout=120)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    if branch:
        git(["branch", "-D", branch], cwd=root)


_PATCH_FILES = re.compile(rb"^diff --git a/(.+?) b/(.+)$", re.M)


def patch_paths(data: bytes) -> list[str]:
    return sorted({m.group(2).decode("utf-8", "replace") for m in _PATCH_FILES.finditer(data)})


def apply_patch(root: str, data: bytes) -> tuple[bool, str]:
    """Apply a heat's patch to the user's working tree, unstaged. Clean apply first; a 3-way merge
    only when every touched file is unmodified in the working tree (so nothing of the user's can be
    mixed with conflict markers silently); conflicts are reported by name."""
    if not data.strip():
        return False, "the patch is empty"
    check = git(["apply", "--check", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
    if check.returncode == 0:
        cp = git(["apply", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
        return (cp.returncode == 0, "applied" if cp.returncode == 0 else (cp.stderr or "git apply failed").strip())
    paths = patch_paths(data)
    dirty = git(["diff", "--name-only", "--", *paths], cwd=root).stdout.split() if paths else []
    if dirty:
        return False, ("the patch no longer applies cleanly and these files have uncommitted edits: "
                       + ", ".join(dirty[:6]) + ". Nothing was changed. Commit or stash them, then apply again.")
    cp = git(["apply", "--3way", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
    if cp.returncode == 0:
        return True, "applied with a 3-way merge (the merged files are staged)"
    conflicts = sorted(set(git(["diff", "--name-only", "--diff-filter=U"], cwd=root).stdout.split()))
    if conflicts:
        return False, ("3-way merge left conflict markers in: " + ", ".join(conflicts[:6])
                       + ". Resolve them, or undo with `git checkout -m -- <file>` / `git restore --staged --worktree <file>`.")
    return False, "nothing was changed: " + (cp.stderr or check.stderr or "git apply failed").strip()[-300:]


def tree_fingerprint(root: str) -> str:
    """Cheap fingerprint of the user's working tree (status + diff of tracked files), excluding .heats."""
    import hashlib
    st = git(["status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).heats"], cwd=root, timeout=120)
    df = git(["diff", "--binary", "HEAD", "--", ".", ":(exclude).heats"], cwd=root, timeout=120)
    refs = git(["for-each-ref", "--format=%(refname) %(objectname)"], cwd=root, timeout=120)
    ref_lines = "\n".join(l for l in refs.stdout.splitlines() if not l.startswith("refs/heads/heats/"))
    stash = git(["stash", "list", "--format=%H"], cwd=root)
    cfg = git(["config", "--local", "--list"], cwd=root)
    parts = [st.stdout, df.stdout, ref_lines, stash.stdout, cfg.stdout]
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()


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
