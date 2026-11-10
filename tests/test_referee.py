from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from heats_core import referee  # noqa: E402
from heats_core.checks import CheckResult  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "my repo"
    (r / "tests").mkdir(parents=True)
    (r / "m.py").write_text("def f():\n    return 1\n")
    (r / "tests" / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=r, check=True)
    return r


def _fake(ok: bool, calls: list, timed_out: bool = False):
    def run(cmd, cwd, timeout_s=0):
        calls.append((cmd, cwd))
        if timed_out:
            return CheckResult(cmd, None, 1.0, timed_out=True, tail="...", error="timed out")
        return CheckResult(cmd, 0 if ok else 1, 0.1, passed=1 if ok else 0, failed=0 if ok else 1,
                           tail="" if ok else "E   assert 1 == 2\n1 failed")
    return run


def test_failing_check_sends_agent_back_with_output(tmp_path):
    r = _repo(tmp_path)
    calls: list = []
    out = referee.review([str(r / "m.py")], 0, 3, check="pytest -q", runner=_fake(False, calls))
    assert out and out["action"] == "continue"
    assert "exited 1, 0 passed / 1 failed" in out["message"] and "assert 1 == 2" in out["message"]
    assert "m.py" in out["message"] and "last reminder" not in out["message"]
    assert calls == [("pytest -q", os.path.realpath(r))]


def test_passing_check_lets_agent_finish(tmp_path):
    r = _repo(tmp_path)
    assert referee.review([str(r / "m.py")], 0, 3, check="pytest -q", runner=_fake(True, [])) is None


def test_last_nudge_suggests_a_race_and_flags_test_edits(tmp_path):
    r = _repo(tmp_path)
    out = referee.review([str(r / "m.py"), str(r / "tests" / "test_m.py")], 2, 3, check="pytest -q",
                         runner=_fake(False, []))
    assert "last reminder" in out["message"] and "heats_run" in out["message"]
    assert "tests/test_m.py" in out["message"]


def test_timeout_is_skipped_not_blamed_on_the_agent(tmp_path):
    r = _repo(tmp_path)
    info: dict = {}
    out = referee.review([str(r / "m.py")], 0, 3, check="pytest -q", timeout_s=5,
                         runner=_fake(False, [], timed_out=True), info=info)
    assert out is None and "longer than 5 s" in info["skipped"]


def test_check_that_cannot_run_is_skipped(tmp_path):
    r = _repo(tmp_path)
    for code, cmd in ((127, "nosuchtool test"), (126, "./notexec"), (5, "python -m pytest -q"), (4, "pytest -q tests/x.py")):
        def run(c, cwd, timeout_s=0, code=code):
            return CheckResult(c, code, 0.1, tail="boom")
        info: dict = {}
        assert referee.review([str(r / "m.py")], 0, 3, check=cmd, runner=run, info=info) is None, code
        assert "could not run" in info["skipped"]
    # exit 4/5 from a non-pytest command is a real failure
    def run1(c, cwd, timeout_s=0):
        return CheckResult(c, 5, 0.1, tail="5 failed")
    assert referee.review([str(r / "m.py")], 0, 3, check="make test", runner=run1) is not None


def test_skips_outside_repo_attempt_worktrees_and_mixed_repos(tmp_path):
    calls: list = []
    loose = tmp_path / "loose.py"
    loose.write_text("x")
    assert referee.review([str(loose)], 0, 3, check="pytest", runner=_fake(False, calls)) is None
    r = _repo(tmp_path)
    wt = r / ".heats" / "run1" / "h1" / "m.py"
    assert referee.review([str(wt)], 0, 3, check="pytest", runner=_fake(False, calls)) is None
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    (other / "a.py").write_text("x")
    assert referee.review([str(r / "m.py"), str(other / "a.py")], 0, 3, check="pytest", runner=_fake(False, calls)) is None
    assert referee.review([], 0, 3, check="pytest", runner=_fake(False, calls)) is None
    assert calls == []


def test_auto_detects_check_and_real_run(tmp_path):
    r = _repo(tmp_path)
    (r / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    out = referee.review([str(r / "m.py")], 0, 3, check=f"{sys.executable} -m pytest -q -p no:cacheprovider")
    assert out and "1 failed" in out["message"]
    (r / "m.py").write_text("def f():\n    return 2\n")
    assert referee.review([str(r / "m.py")], 0, 3, check=f"{sys.executable} -m pytest -q -p no:cacheprovider") is None


def test_deleted_file_still_maps_to_repo(tmp_path):
    r = _repo(tmp_path)
    assert referee.repo_for([str(r / "gone" / "x.py")]) == os.path.realpath(r)


def test_info_reports_verdict_for_the_pane(tmp_path):
    r = _repo(tmp_path)
    info: dict = {}
    referee.review([str(r / "m.py")], 2, 3, check="pytest -q", runner=_fake(False, []), info=info)
    assert info["ran"] and info["ok"] is False and info["failed"] == 1 and info["last_nudge"] and info["repo"] == "my repo"
    info2: dict = {}
    referee.review([str(tmp_path / "nowhere.py")], 0, 3, check="pytest", runner=_fake(False, []), info=info2)
    assert info2 == {"ran": False}


def test_does_not_run_a_check_the_agent_may_have_just_redefined(tmp_path):
    r = _repo(tmp_path)
    calls: list = []
    for f in ("package.json", "Makefile", "conftest.py", "pyproject.toml", "pytest.ini", "tests/conftest.py"):
        info: dict = {}
        assert referee.review([str(r / "m.py"), str(r / f)], 0, 3, check="make test", runner=_fake(False, calls),
                              info=info) is None
        assert info["ran"] and "can change what the check runs" in info["skipped"], f
    assert calls == []


def test_missing_test_runner_is_not_blamed_on_the_agent(tmp_path):
    r = _repo(tmp_path)
    def run(c, cwd, timeout_s=0):
        return CheckResult(c, 1, 0.1, tail="/usr/bin/python3: No module named pytest\n")
    info: dict = {}
    assert referee.review([str(r / "m.py")], 0, 3, check="python3 -m pytest -q", runner=run, info=info) is None
    assert "could not run" in info["skipped"]


def test_edited_script_named_in_the_check_is_not_run(tmp_path):
    r = _repo(tmp_path)
    calls: list = []
    info: dict = {}
    assert referee.review([str(r / "run_tests.sh")], 0, 3, check="./run_tests.sh -q", runner=_fake(False, calls), info=info) is None
    assert calls == [] and "run_tests.sh" in info["skipped"]
    # an unrelated short file name inside a word of the command does not count
    assert referee.review([str(r / "q")], 0, 3, check="pytest -q", runner=_fake(False, calls)) is not None


def test_green_after_editing_tests_is_sent_back(tmp_path):
    r = _repo(tmp_path)
    info: dict = {}
    out = referee.review([str(r / "m.py"), str(r / "tests" / "test_m.py")], 1, 3, check="pytest -q",
                         runner=_fake(True, []), info=info)
    assert out and out["action"] == "continue"
    assert "passes now" in out["message"] and "tests/test_m.py" in out["message"]
    assert "restore them" in out["message"] and "last reminder" not in out["message"]
    assert info["ok"] is True and info["tests_edited"] == ["tests/test_m.py"]
    # a new test file at the repo root counts too, and the last nudge says to tell the user
    out = referee.review([str(r / "m.py"), str(r / "test_new.py")], 2, 3, check="pytest -q", runner=_fake(True, []))
    assert out and "test_new.py" in out["message"] and "last reminder" in out["message"]


def test_green_without_test_edits_still_finishes(tmp_path):
    r = _repo(tmp_path)
    info: dict = {}
    assert referee.review([str(r / "m.py")], 1, 3, check="pytest -q", runner=_fake(True, []), info=info) is None
    assert "tests_edited" not in info
