"""Regression tests for the bug bash (bugbash/FINDINGS.md): an existing .heats breaking later runs,
stale-run cleanup, check artifacts counted as changes, renamed test files, auto-detected python,
editable installs in a linked venv. Needs git; no Hermes."""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heats_core import gitops  # noqa: E402
from heats_core.checks import Guard, detect_check, editable_pythonpath, run_check  # noqa: E402

GIT_ID = "-c user.email=t@t -c user.name=t"
RUN = "1109-130000-abcd"


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (r / ".gitignore").write_text("node_modules/\n")
    sh(f"git init -q && git {GIT_ID} add -A && git {GIT_ID} commit -qm init", r)
    return str(r.resolve())


def _wt(repo, base, h=1):
    wt = os.path.join(repo, ".heats", RUN, f"h{h}")
    gitops.add_worktree(repo, wt, f"heats/{RUN}/h{h}", base)
    return wt


def test_existing_heats_dir_does_not_break_snapshot(repo):
    """(A) after a killed run, .heats/<id> (ignored via info/exclude) made every later snapshot fail."""
    gitops.ensure_excluded(repo)
    base, _ = gitops.snapshot_base(repo)
    _wt(repo, base)  # a live (or crashed) run's worktree
    Path(repo, "calc.py").write_text("def add(a, b):\n    return a + b  # wip\n")
    sha, dirty = gitops.snapshot_base(repo)  # used to raise "git add (snapshot) failed"
    assert dirty
    names = sh(f"git ls-tree -r --name-only {sha}", repo).split()
    assert not [n for n in names if n.startswith(".heats")]
    gitops.prune_stale_runs(repo)


def test_heats_dir_not_ignored_is_still_left_out(repo):
    base, _ = gitops.snapshot_base(repo)
    _wt(repo, base)
    Path(repo, "new.txt").write_text("x\n")
    sha, _ = gitops.snapshot_base(repo)
    names = sh(f"git ls-tree -r --name-only {sha}", repo).split()
    assert "new.txt" in names and not [n for n in names if n.startswith(".heats")]
    gitops.prune_stale_runs(repo)


def test_cleanup_stale_signature(repo):
    base, _ = gitops.snapshot_base(repo)
    _wt(repo, base)
    assert gitops.cleanup_stale(repo, {RUN}) == []
    assert gitops.cleanup_stale(repo, set()) == [RUN]
    assert not Path(repo, ".heats", RUN).exists()
    assert "heats/" not in sh("git branch", repo)


def test_linked_ignored_dir_exclude_does_not_crash(repo):
    """collect_patch's exclude pathspecs for linked dependency dirs must not trip advice.addIgnoredFile."""
    os.makedirs(f"{repo}/node_modules/x")
    base, _ = gitops.snapshot_base(repo)
    wt = _wt(repo, base)
    linked = gitops.link_dependency_dirs(repo, wt)
    assert linked == ["node_modules"]
    Path(wt, "calc.py").write_text("def add(a, b):\n    return b + a\n")
    p = gitops.collect_patch(wt, base, ignore=tuple(linked) + ("does_not_exist",))
    assert p.files == ["calc.py"]
    gitops.prune_stale_runs(repo)


def test_check_artifacts_are_not_a_change(repo):
    """(C) a heat that only ran the check (report.xml, __pycache__, .coverage) changed nothing."""
    base, _ = gitops.snapshot_base(repo)
    wt = _wt(repo, base)
    Path(wt, "report.xml").write_text("<testsuite/>")
    Path(wt, ".coverage").write_text("x")
    os.makedirs(f"{wt}/__pycache__")
    Path(wt, "__pycache__", "calc.cpython-312.pyc").write_bytes(b"\0")
    os.makedirs(f"{wt}/.pytest_cache/v")
    Path(wt, ".pytest_cache", "v", "x").write_text("x")
    p = gitops.collect_patch(wt, base)
    assert p.empty and p.files == []
    Path(wt, "calc.py").write_text("def add(a, b):\n    return b + a\n")
    assert gitops.collect_patch(wt, base).files == ["calc.py"]
    gitops.prune_stale_runs(repo)


def test_renamed_root_test_file_is_guarded(repo):
    """(D) `mv test_calc.py disabled.bak` showed up as 'test_calc.py => disabled.bak' and was not guarded."""
    base, _ = gitops.snapshot_base(repo)
    wt = _wt(repo, base)
    os.rename(f"{wt}/test_calc.py", f"{wt}/disabled_calc.py.bak")
    p = gitops.collect_patch(wt, base)
    assert "test_calc.py" in p.files
    assert "test_calc.py" in Guard().violations(p.files)
    gitops.prune_stale_runs(repo)


def test_detect_check_python_exists(tmp_path, monkeypatch):
    """(B) macOS has no `python`: auto-detect must name an interpreter that exists."""
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr("shutil.which", lambda name: None if name == "python" else "/usr/bin/" + name)
    assert detect_check(str(tmp_path)) == "python3 -m pytest -q"
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert detect_check(str(tmp_path)) == ".venv/bin/python -m pytest -q"


def test_editable_install_in_linked_venv_judges_the_worktree(tmp_path):
    """(E) a src-layout package installed editable in a linked .venv imported the user's checkout."""
    r = tmp_path / "ed"
    (r / "src" / "mypkg").mkdir(parents=True)
    (r / "src" / "mypkg" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_add.py").write_text("from mypkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (r / ".gitignore").write_text(".venv/\n")
    sh(f"git init -q && git {GIT_ID} add -A && git {GIT_ID} commit -qm init", r)
    root = str(r.resolve())
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", ".venv"], cwd=root, check=True)
    sp = glob.glob(os.path.join(root, ".venv/lib/python*/site-packages"))[0]
    Path(sp, "__editable__.mypkg.pth").write_text(os.path.join(root, "src") + "\n")
    base, _ = gitops.snapshot_base(root)
    wt = os.path.join(root, ".heats", RUN, "h1")
    gitops.add_worktree(root, wt, f"heats/{RUN}/h1", base)
    gitops.link_dependency_dirs(root, wt)
    Path(wt, "src", "mypkg", "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    assert editable_pythonpath(root, wt) == [os.path.join(os.path.realpath(wt), "src")]
    check = ".venv/bin/python -c 'import mypkg,sys; sys.exit(0 if mypkg.add(2, 3) == 5 else 1)'"
    assert run_check(check, wt, timeout_s=60).ok  # the heat's fixed code
    assert not run_check(check, root, timeout_s=60).ok  # the user's checkout is still the old code
    gitops.prune_stale_runs(root)
