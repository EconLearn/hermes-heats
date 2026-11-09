"""Regression tests for reliability review round 2 (panels/reliability_r2.md): stale cleanup must not
touch a live race run from another checkout of the same repo, collect_patch must leave the heat's
index alone, detect_check on JS repos, and git-guard spellings. Needs git; no Hermes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heats_core import gitops  # noqa: E402
from heats_core.checks import detect_check  # noqa: E402
from heats_core.isolation import shared_git_command  # noqa: E402

GIT_ID = "-c user.email=t@t -c user.name=t"


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_calc.py").write_text("def test():\n    assert True\n")
    sh(f"git init -q && git {GIT_ID} add -A && git {GIT_ID} commit -qm init", r)
    return str(r.resolve())


def test_cleanup_keeps_live_race_of_another_checkout(repo, tmp_path):
    """r2 #11: a race running from a linked worktree had its heats/* branches deleted."""
    other = str(tmp_path / "feature")
    sh(f"git worktree add -q -b feature '{other}'", repo)
    other = os.path.realpath(other)
    live = "1109-140000-aaaa"
    base, _ = gitops.snapshot_base(other)
    wt = os.path.join(other, ".heats", live, "h1")
    gitops.add_worktree(other, wt, f"heats/{live}/h1", base)
    dead = "1109-140001-bbbb"  # a crash leftover of the main checkout: branch only, no worktree
    sh(f"git branch heats/{dead}/h1", repo)
    cleaned = gitops.cleanup_stale(repo, set())
    assert cleaned == [dead]
    assert f"heats/{live}/h1" in sh("git branch", repo)
    assert "No commits yet" not in sh("git status", wt)
    gitops.remove_worktree(other, wt, f"heats/{live}/h1")


def test_collect_patch_leaves_heat_index_alone(repo):
    """r2 #12: judging staged the heat's edits, so `git checkout -- f` restored the weakened test."""
    base, _ = gitops.snapshot_base(repo)
    run = "1109-140002-cccc"
    wt = os.path.join(repo, ".heats", run, "h1")
    gitops.add_worktree(repo, wt, f"heats/{run}/h1", base)
    try:
        Path(wt, "tests", "test_calc.py").write_text("def test():\n    pass  # weakened\n")
        Path(wt, "new.py").write_text("x = 1\n")
        p = gitops.collect_patch(wt, base)
        assert set(p.files) == {"tests/test_calc.py", "new.py"}
        assert sh("git diff --cached --name-only", wt) == ""  # nothing left staged
        sh("git checkout -- tests/test_calc.py", wt)  # the advice now really restores it
        assert "weakened" not in Path(wt, "tests", "test_calc.py").read_text()
        assert gitops.collect_patch(wt, base).files == ["new.py"]  # and collecting again still works
    finally:
        gitops.remove_worktree(repo, wt, f"heats/{run}/h1")


def test_detect_check_npm_init_with_test_folder_is_not_pytest(tmp_path):
    """r2 #4: `npm init` repo (no test script) with a test/ folder was given `python3 -m pytest`."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": {
        "test": "echo \"Error: no test specified\" && exit 1"}}))
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "a.test.js").write_text("")
    assert detect_check(str(tmp_path)) is None
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": "node --test"}}))
    assert detect_check(str(tmp_path)) == "npm test"
    py = tmp_path / "mixed"
    (py / "tests").mkdir(parents=True)
    (py / "package.json").write_text("{}")
    (py / "tests" / "test_x.py").write_text("")
    assert detect_check(str(py)).endswith("-m pytest -q")  # Python evidence: still pytest


@pytest.mark.parametrize("cmd", ["g''it stash clear", "gi\\t stash clear", "\"git\" stash", "'git' push",
                                 "G=git; $G stash clear", "${G} stash clear", "$G push origin main",
                                 "cd \"$(git rev-parse --git-common-dir)/..\" && git commit -am x"])
def test_git_guard_quoting_and_variable_bypasses(cmd):
    assert shared_git_command(cmd) is not None


@pytest.mark.parametrize("cmd", ["echo $HOME", "$PY -m pytest -q", "\"$VENV/bin/python\" -m pytest", "git status"])
def test_git_guard_variable_commands_still_allowed(cmd):
    assert shared_git_command(cmd) is None
