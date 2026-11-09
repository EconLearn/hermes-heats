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
        skip = [".heats"]
        for rel in untracked_files(root):
            if rel == ".heats" or rel.startswith(".heats/"):
                continue
            try:
                if (Path(root) / rel).stat().st_size > max_untracked_file_bytes:
                    skip.append(rel)
            except OSError:
                skip.append(rel)
        excludes = exclude_pathspecs(root, skip, env_extra=env)
        _must(git(["add", "-A", "--", ".", *excludes], cwd=root, env_extra=env, timeout=300), "git add (snapshot)")
        # belt and braces: whatever git did with .heats, it is never part of the base
        git(["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", ".heats"], cwd=root, env_extra=env, timeout=120)
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


def exclude_pathspecs(cwd: str, rels, env_extra: Optional[dict] = None) -> list[str]:
    """``:(exclude,literal)`` pathspecs for *rels*, leaving out paths git already ignores: git 2.39
    refuses ``git add`` with "paths are ignored" (advice.addIgnoredFile) when an exclude pathspec names an
    ignored path, which broke every run once ``.heats`` existed. Ignored paths are skipped by ``add -A``
    anyway."""
    rels = [r for r in dict.fromkeys(rels) if r]
    if not rels:
        return []
    cp = git(["check-ignore", "-z", "--stdin"], cwd=cwd, input_text="\0".join(rels) + "\0", env_extra=env_extra)
    ignored = set(cp.stdout.split("\0")) if cp.returncode == 0 else set()
    return [f":(exclude,literal){r}" for r in rels if r not in ignored and r.rstrip("/") not in ignored]


# Files a test run itself creates. Only NEW untracked ones are left out of a heat's patch: a heat that
# merely ran the check must not count as a change (or win), and these must not reach the user's tree.
_ARTIFACT = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|htmlcov|\.nyc_output|\.tox|\.nox|"
    r"coverage|test-results|junit|\.eggs|[^/]+\.egg-info)(/|$)"
    r"|\.py[co]$|(^|/)\.coverage(\.[^/]*)?$|(^|/)(coverage|cobertura|lcov|clover)\.(xml|info|json)$"
    r"|(^|/)(report|results|test-results|junit[^/]*|TEST-[^/]*|pytest[^/]*)\.xml$|(^|/)lcov\.info$")


def check_artifacts(wt: str) -> list[str]:
    """New untracked files in a worktree that look like test-run output (see ``_ARTIFACT``)."""
    return [f for f in untracked_files(wt) if _ARTIFACT.search(f)]


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


class PathList(list):
    """A list of changed paths that also carries the patch text, so a Guard can look inside it
    (for changes to the test command's definition)."""
    patch_data: bytes = b""


def _parse_numstat_z(out: bytes | str) -> list[tuple[int, int, list[str]]]:
    """Rows of ``git ... --numstat -z``: (insertions, deletions, [path] or [old, new] for a rename)."""
    raw = out.decode("utf-8", "surrogateescape") if isinstance(out, bytes) else out
    parts = raw.split("\0")
    rows, i = [], 0
    while i < len(parts):
        head = parts[i]
        if not head.strip():
            i += 1
            continue
        fields = head.split("\t", 2)
        if len(fields) < 3:
            i += 1
            continue
        ins = int(fields[0]) if fields[0].isdigit() else 0
        dels = int(fields[1]) if fields[1].isdigit() else 0
        if fields[2]:
            rows.append((ins, dels, [fields[2]]))
            i += 1
        else:  # rename/copy: the old and new paths follow as their own NUL-terminated fields
            rows.append((ins, dels, [p for p in parts[i + 1:i + 3] if p]))
            i += 3
    return rows


def collect_patch(wt: str, base: str, ignore: tuple[str, ...] = ()) -> Patch:
    """Everything the heat changed relative to *base* (tracked, new and deleted files)."""
    skip = list(ignore) + check_artifacts(wt)
    excludes = exclude_pathspecs(wt, skip)
    _must(git(["add", "-A", "--", ".", *excludes], cwd=wt, timeout=120), "git add in heat")
    d = git(["diff", "--cached", "--binary", base], cwd=wt, timeout=120, binary=True)
    if d.returncode != 0:
        raise HeatsGitError(f"git diff in heat failed: {d.stderr.strip()[-200:]}")
    data = d.stdout
    # -z and quotePath=false: non-ASCII names come back as they are (not C-quoted), renames as two fields
    stat = git(["-c", "core.quotePath=false", "diff", "--cached", "--numstat", "-z", base], cwd=wt, timeout=120)
    files, ins, dels = PathList(), 0, 0
    for i, dl, paths in _parse_numstat_z(stat.stdout):
        for p in paths:
            if p not in files:
                files.append(p)
        ins += i
        dels += dl
    files.patch_data = data
    return Patch(data=data, files=files, insertions=ins, deletions=dels)


def remove_worktree(root: str, path: str, branch: Optional[str] = None) -> None:
    git(["worktree", "remove", "--force", "--force", path], cwd=root, timeout=120)  # twice: also when locked
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    if branch:
        git(["branch", "-D", branch], cwd=root)


_PATCH_FILES = re.compile(rb"^diff --git a/(.+?) b/(.+)$", re.M)


def patch_paths(data: bytes, root: Optional[str] = None) -> list[str]:
    """Every path a patch touches (both sides of a rename). Asks git when *root* is given, so quoted
    non-ASCII names are read correctly; falls back to the diff headers."""
    if root:
        cp = git(["-c", "core.quotePath=false", "apply", "--numstat", "-z", "-"], cwd=root, input_text=data, binary=True)
        if cp.returncode == 0:
            out: set[str] = set()
            for _i, _d, paths in _parse_numstat_z(cp.stdout):
                out.update(paths)
            if out:
                return sorted(out)
    return sorted({m.group(2).decode("utf-8", "replace") for m in _PATCH_FILES.finditer(data)})


def _local_state(root: str, paths: list[str]) -> tuple[list[str], set[str]]:
    """(paths with ANY local change: staged, unstaged, untracked or ignored-but-present, the paths HEAD has)."""
    spec = [f":(literal){p}" for p in paths]
    st = git(["-c", "core.quotePath=false", "status", "--porcelain", "-z", "--untracked-files=all", "--ignored",
              "--", *spec], cwd=root, binary=True)
    dirty: set[str] = set()
    if st.returncode != 0:
        dirty.update(paths)  # cannot tell: treat all as changed
    else:
        entries = st.stdout.decode("utf-8", "surrogateescape").split("\0")
        k = 0
        while k < len(entries):
            e = entries[k]
            if len(e) > 3:
                dirty.add(e[3:])
                if e[0] in "RC":  # a rename's source follows as its own field
                    k += 1
                    if k < len(entries) and entries[k]:
                        dirty.add(entries[k])
            k += 1
    lt = git(["-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *spec], cwd=root,
             binary=True)
    in_head = {x for x in lt.stdout.decode("utf-8", "surrogateescape").split("\0") if x} if lt.returncode == 0 else set()
    for p in paths:
        if p not in in_head and os.path.lexists(os.path.join(root, p)):
            dirty.add(p)
    hit = sorted(p for p in paths if p in dirty or any(d.startswith(p.rstrip("/") + "/") for d in dirty))
    return hit, in_head


def _restore_paths(root: str, paths: list[str], in_head: set[str]) -> None:
    """Put *paths* back exactly as HEAD has them (index and file), removing ones HEAD does not have.
    Only called for paths that had no local change before, so nothing of the user's is lost."""
    tracked = [p for p in paths if p in in_head]
    for i in range(0, len(tracked), 100):
        git(["checkout", "HEAD", "--", *[f":(literal){p}" for p in tracked[i:i + 100]]], cwd=root)
    for p in paths:
        if p in in_head:
            continue
        git(["rm", "--cached", "-q", "--ignore-unmatch", "--", f":(literal){p}"], cwd=root)
        fp = os.path.join(root, p)
        try:
            if os.path.islink(fp) or os.path.isfile(fp):
                os.unlink(fp)
        except OSError:
            pass


def apply_patch(root: str, data: bytes) -> tuple[bool, str]:
    """Apply a heat's patch to the user's working tree, unstaged, all or nothing.

    A clean ``git apply`` first (atomic: it changes every file or none). If that fails, a 3-way merge
    is tried only when every file the patch touches has no local change at all (nothing staged,
    unstaged or untracked), so the merge can never mix into the user's uncommitted work; and if the
    merge does not go through cleanly, every touched file is put back as it was."""
    if not data.strip():
        return False, "the patch is empty"
    check = git(["apply", "--check", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
    if check.returncode == 0:
        cp = git(["apply", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
        return (cp.returncode == 0, "applied" if cp.returncode == 0 else (cp.stderr or "git apply failed").strip())
    paths = patch_paths(data, root)
    if not paths:
        return False, "nothing was changed: " + (check.stderr or "git apply failed").strip()[-300:]
    dirty, in_head = _local_state(root, paths)
    if dirty:
        return False, ("the patch no longer applies cleanly and these files have uncommitted edits (staged, "
                       "unstaged or untracked): " + ", ".join(dirty[:6]) + ". Nothing was changed. Commit or stash "
                       "them, then apply again.")
    cp = git(["apply", "--3way", "--whitespace=nowarn", "-"], cwd=root, input_text=data, binary=True)
    conflicts = sorted(set(git(["-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=U"],
                               cwd=root).stdout.splitlines()))
    if cp.returncode == 0 and not conflicts:
        # --3way stages what it merged; unstage it so the result matches a clean apply (index = HEAD again)
        git(["reset", "-q", "--", *[f":(literal){p}" for p in paths]], cwd=root)
        return True, "applied with a 3-way merge"
    _restore_paths(root, paths, in_head)
    detail = ("the 3-way merge conflicts in " + ", ".join(conflicts[:6])) if conflicts else         (cp.stderr or check.stderr or "git apply failed").strip()[-300:]
    return False, (f"{detail}. Nothing was changed (the touched files had no local edits and are back as they were). "
                   "The code moved on since the run started: run heats again from the current code.")


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


_RUN_ID = re.compile(r"^\d{4}-\d{6}-[0-9a-f]{4}$")


def prune_stale_runs(root: str, active: "set[str] | list[str] | tuple[str, ...]" = (), min_age_s: float = 0.0) -> list[str]:
    """Remove what a crashed or killed Hermes left behind in *root*: the ``.heats/<run_id>`` worktrees
    (locked ones too), their ``heats/<run_id>/h*`` branches and the run folders, for every run not in
    *active* whose folder is older than *min_age_s*. Only names Heats itself creates are touched.
    Returns the run ids cleaned."""
    import time as _time
    root = os.path.realpath(root)
    active = set(active or ())
    heats_dir = os.path.join(root, ".heats")
    now = _time.time()

    verdicts: dict[str, bool] = {}

    def stale(run_id: str) -> bool:  # decided once per run, before anything is removed
        if run_id not in verdicts:
            d = os.path.join(heats_dir, run_id)
            ok = run_id not in active and bool(_RUN_ID.match(run_id))
            if ok and min_age_s > 0:
                try:
                    ok = now - os.stat(d).st_mtime >= min_age_s
                except OSError:
                    ok = True
            verdicts[run_id] = ok
        return verdicts[run_id]

    cleaned: set[str] = set()
    wl = git(["worktree", "list", "--porcelain"], cwd=root, timeout=120)
    for line in wl.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):]
        for cand in {path, os.path.realpath(path)}:
            if cand.startswith(heats_dir + os.sep):
                run_id = cand[len(heats_dir) + 1:].split(os.sep, 1)[0]
                if stale(run_id):
                    git(["worktree", "remove", "--force", "--force", path], cwd=root, timeout=120)
                    cleaned.add(run_id)
                break
    br = git(["for-each-ref", "--format=%(refname)", "refs/heads/heats/"], cwd=root)
    for ref in br.stdout.splitlines():
        parts = ref.split("/")
        if len(parts) >= 5 and stale(parts[3]):
            git(["update-ref", "-d", ref], cwd=root)
            cleaned.add(parts[3])
    if os.path.isdir(heats_dir):
        for name in os.listdir(heats_dir):
            full = os.path.join(heats_dir, name)
            if stale(name) and os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)  # symlinked dependency folders are unlinked, not followed
                cleaned.add(name)
    return sorted(cleaned)


def cleanup_stale(root: str, active_run_ids=(), min_age_s: float = 0.0) -> list[str]:
    """Same as ``prune_stale_runs``: call at run start with the ids of runs still live in this process."""
    return prune_stale_runs(root, active=active_run_ids, min_age_s=min_age_s)
