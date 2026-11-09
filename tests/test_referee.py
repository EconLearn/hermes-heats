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


def test_timeout_is_reported(tmp_path):
    r = _repo(tmp_path)
    out = referee.review([str(r / "m.py")], 0, 3, check="pytest -q", timeout_s=5, runner=_fake(False, [], timed_out=True))
    assert "timed out after 5 s" in out["message"]


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
