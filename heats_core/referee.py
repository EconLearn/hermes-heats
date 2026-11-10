"""Referee: when the main agent edited code and is about to say "done", run the repo's check in the
user's checkout. If it fails, send the agent back once more with what was measured (exit code,
counts, test output tail) instead of letting it finish on a claim. Bounded by Hermes's
``agent.max_verify_nudges``; on the last nudge, point at heats_run for a race.

Pure logic here; the ``pre_verify`` hook in the plugin's __init__ wires it up."""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from . import gitops
from .checks import _TEST_CONFIG_FILE, CheckResult, cannot_run, detect_check, is_test_path, run_check

TAIL_CHARS = 1_500
CANNOT_RUN = (126, 127)  # shell: not executable / command not found


def _is_pytest(cmd: str) -> bool:
    return "pytest" in cmd  # pytest exit 4 = usage error (e.g. path not found), 5 = no tests collected
# Files that can change WHAT the check command runs. If the agent edited one this turn, the referee does not
# run the check: that would execute a command the agent just wrote, without the user's approval.
_DEFINES_CHECK = {"package.json", "Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "pyproject.toml",
                  "setup.cfg", "setup.py", "conftest.py", "tox.ini", "noxfile.py", "Cargo.toml", "go.mod",
                  "Rakefile", "build.gradle", "build.gradle.kts", "pom.xml", "build.rs", "config.toml"}


def _defines_check(rel: str) -> bool:
    return os.path.basename(rel) in _DEFINES_CHECK or bool(_TEST_CONFIG_FILE.search(rel)) or rel.startswith(".git/")


def repo_for(changed_paths: list[str]) -> Optional[str]:
    """The git repo the edits belong to, or None (no edits, edits outside any repo, edits spread
    over several repos, or edits inside a Heats attempt worktree, which has its own laps)."""
    roots = set()
    for p in changed_paths or []:
        if not isinstance(p, str) or not p:
            continue
        real = os.path.realpath(os.path.expanduser(p))
        if f"{os.sep}.heats{os.sep}" in real:
            return None
        d = real if os.path.isdir(real) else os.path.dirname(real)
        while d and not os.path.isdir(d):  # a deleted file: walk up to an existing folder
            d = os.path.dirname(d)
        root = gitops.repo_root(d) if d else None
        if root:
            roots.add(os.path.realpath(root))
    return roots.pop() if len(roots) == 1 else None


def review(changed_paths: list[str], attempt: int, max_nudges: int, check: Optional[str] = None,
           timeout_s: float = 120, runner: Callable[..., CheckResult] = run_check,
           info: Optional[dict] = None) -> Optional[dict]:
    """Return ``{"action": "continue", "message": ...}`` when the check fails, else None. If *info* is
    given it is filled with what was measured (for the Desktop pane): ran, repo, check, ok, exit_code,
    passed, failed, timed_out, last_nudge, and tests_edited when a green check came after test edits."""
    info = {} if info is None else info
    info["ran"] = False
    root = repo_for(changed_paths)
    if not root:
        return None
    cmd = check or detect_check(root)
    if not cmd:
        return None
    rel = sorted({os.path.relpath(os.path.realpath(p), root) for p in changed_paths if isinstance(p, str)})
    named = {os.path.basename(w) for w in re.split(r"[\s;&|()<>'\"]+", cmd) if w}  # scripts the check runs
    risky = [p for p in rel if _defines_check(p) or os.path.basename(p) in named]
    if risky:
        info.update(ran=True, repo=os.path.basename(root), check=cmd, ok=None, exit_code=None,
                    skipped=f"{', '.join(risky[:3])} edited (it can change what the check runs)")
        return None
    res = runner(cmd, root, timeout_s=timeout_s)
    info.update(ran=True, repo=os.path.basename(root), check=cmd, ok=res.ok, exit_code=res.exit_code,
                passed=res.passed, failed=res.failed, timed_out=res.timed_out,
                last_nudge=attempt + 1 >= max_nudges, attempt=attempt + 1, max_nudges=max_nudges)
    touched_tests = [p for p in rel if is_test_path(p)]
    if res.ok:
        if not touched_tests:
            return None
        # Green, but only after the agent changed the tests: the same rule as a race (editing tests cannot win).
        info["tests_edited"] = touched_tests[:6]
        msg = [f"Heats referee: the check `{cmd}` passes now ({res.passed if res.passed is not None else '?'} passed), "
               f"but this turn you changed test files: {', '.join(touched_tests[:4])}. A green check after editing the "
               "tests does not show that the code is fixed.",
               "Unless the user asked you to change these tests, restore them (git checkout -- <file> for tracked "
               "files; delete test files you created), fix the code instead, and run the check again. If the user did "
               "ask for these test changes, say which ones and why, then finish."]
        if attempt + 1 >= max_nudges:
            msg.append("This is the referee's last reminder. If you keep the test changes, tell the user plainly that "
                       "you changed the tests to make the check pass.")
        return {"action": "continue", "message": "\n".join(msg)}
    # Not the agent's fault, or not knowable: never tell it "your work is not done" for these.
    if res.timed_out:
        info["skipped"] = f"check took longer than {timeout_s:.0f} s"
        return None
    if cannot_run(res, cmd):
        info["skipped"] = f"check could not run (exit {res.exit_code})"
        return None
    counts = (f", {res.passed} passed / {res.failed} failed" if res.passed is not None or res.failed is not None else "")
    status = f"timed out after {timeout_s:.0f} s" if res.timed_out else f"exited {res.exit_code}{counts}"
    lines = [
        f"Heats referee: you edited {', '.join(rel[:6]) or 'files'} and are about to finish, but the "
        f"check `{cmd}` (run in {root}) {status}. Your work is not done yet.",
        "Read the failing output below, fix the code, then run the check yourself before you finish."
        " Do not edit the tests to make them pass. If these failures were already there before your change and"
        " are unrelated to the task, say so plainly and finish."
        + (f" (You changed test files: {', '.join(touched_tests[:4])}.)" if touched_tests else ""),
    ]
    if attempt + 1 >= max_nudges:
        lines.append("This is the referee's last reminder. If you cannot get it passing, tell the user plainly "
                     "that the check still fails and offer to race several attempts with heats_run.")
    lines.append("Check output (last lines):\n" + (res.tail or res.error or "")[-TAIL_CHARS:])
    return {"action": "continue", "message": "\n".join(lines)}
