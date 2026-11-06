"""Unit tests for heats_core. Run: python -m pytest -q tests (needs git; no Hermes needed)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heats_core import gitops  # noqa: E402
from heats_core.checks import Guard, detect_check, is_test_path, parse_counts, run_check  # noqa: E402
from heats_core.isolation import IsolationMap, Lane, map_path, note_terminal_result, rewrite  # noqa: E402
from heats_core.runner import RunConfig, Runner, apply_heat, list_runs, load_report  # noqa: E402

PY = sys.executable


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_calc.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    sh("git init -q && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -qm init", r)
    return str(r.resolve())


CHECK = f"{PY} -m pytest -q -p no:cacheprovider tests"


# ----------------------------------------------------------------- gitops
def test_snapshot_base_includes_uncommitted(repo):
    head, dirty = gitops.snapshot_base(repo)
    assert not dirty
    Path(repo, "calc.py").write_text("def add(a, b):\n    return a + b  # wip\n")
    sha, dirty = gitops.snapshot_base(repo)
    assert dirty and sha != head
    wt = os.path.join(repo, ".heats", "x", "h1")
    gitops.add_worktree(repo, wt, "heats/x/h1", sha)
    assert "# wip" in Path(wt, "calc.py").read_text()
    # the user's tree and index are untouched
    assert "# wip" in Path(repo, "calc.py").read_text()
    gitops.remove_worktree(repo, wt, "heats/x/h1")
    assert not Path(wt).exists()
    br = subprocess.run(["git", "branch", "--list", "heats/*"], cwd=repo, capture_output=True, text=True).stdout
    assert br.strip() == ""


def test_no_commits_is_a_clear_error(tmp_path):
    sh("git init -q", tmp_path)
    with pytest.raises(gitops.HeatsGitError, match="no commits"):
        gitops.snapshot_base(str(tmp_path))


def test_patch_roundtrip_and_apply(repo):
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "y", "h1")
    gitops.add_worktree(repo, wt, "heats/y/h1", base)
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n")
    Path(wt, "new.txt").write_text("hello\n")
    p = gitops.collect_patch(wt, base)
    assert sorted(p.files) == ["calc.py", "new.txt"]
    assert p.insertions == 2 and p.deletions == 1
    gitops.remove_worktree(repo, wt, "heats/y/h1")
    ok, msg = gitops.apply_patch(repo, p.data)
    assert ok, msg
    assert "a + b" in Path(repo, "calc.py").read_text()
    assert Path(repo, "new.txt").exists()


def test_exclude_entry_added_once(repo):
    gitops.ensure_excluded(repo)
    gitops.ensure_excluded(repo)
    text = Path(repo, ".git", "info", "exclude").read_text()
    assert text.count("/.heats/") == 1


def test_untracked_files_are_in_the_base_and_not_in_patches(repo):
    Path(repo, "notes.txt").write_text("untracked\n")
    Path(repo, "tests", "test_local.py").write_text("def test_local():\n    assert True\n")
    base, dirty = gitops.snapshot_base(repo)
    assert dirty
    wt = os.path.join(repo, ".heats", "z", "h1")
    gitops.add_worktree(repo, wt, "heats/z/h1", base)
    assert Path(wt, "notes.txt").read_text() == "untracked\n"
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n")
    p = gitops.collect_patch(wt, base)
    assert p.files == ["calc.py"], p.files  # the user's untracked files are NOT part of the heat's change
    gitops.remove_worktree(repo, wt, "heats/z/h1")
    ok, msg = gitops.apply_patch(repo, p.data)
    assert ok, msg
    st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert "?? notes.txt" in st and "?? tests/test_local.py" in st  # still untracked, untouched


def test_snapshot_never_touches_index_stash_or_refs(repo):
    Path(repo, "calc.py").write_text("def add(a, b):\n    return a + b  # staged\n")
    subprocess.run(["git", "add", "calc.py"], cwd=repo, check=True)
    Path(repo, "calc.py").write_text("def add(a, b):\n    return a + b  # unstaged\n")
    before = [subprocess.run(c, cwd=repo, capture_output=True, text=True).stdout for c in
              (["git", "diff", "--cached"], ["git", "stash", "list"], ["git", "for-each-ref"])]
    gitops.snapshot_base(repo)
    after = [subprocess.run(c, cwd=repo, capture_output=True, text=True).stdout for c in
             (["git", "diff", "--cached"], ["git", "stash", "list"], ["git", "for-each-ref"])]
    assert before == after


def test_failed_apply_never_leaves_markers_in_dirty_files(repo):
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "c", "h1")
    gitops.add_worktree(repo, wt, "heats/c/h1", base)
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n")
    p = gitops.collect_patch(wt, base)
    gitops.remove_worktree(repo, wt, "heats/c/h1")
    Path(repo, "calc.py").write_text("def add(x, y):\n    return x - y  # user's edit\n")
    ok, msg = gitops.apply_patch(repo, p.data)
    assert not ok and "uncommitted edits" in msg
    assert "user's edit" in Path(repo, "calc.py").read_text() and "<<<<<<<" not in Path(repo, "calc.py").read_text()


def test_patch_bytes_roundtrip_non_utf8(repo):
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "b", "h1")
    gitops.add_worktree(repo, wt, "heats/b/h1", base)
    Path(wt, "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    p = gitops.collect_patch(wt, base)
    gitops.remove_worktree(repo, wt, "heats/b/h1")
    ok, msg = gitops.apply_patch(repo, p.data)
    assert ok, msg
    assert Path(repo, "latin1.txt").read_bytes() == "caf\xe9\n".encode("latin-1")


def test_fingerprint_sees_stash_branch_and_config_changes(repo):
    f0 = gitops.tree_fingerprint(repo)
    subprocess.run(["git", "branch", "side"], cwd=repo, check=True)
    f1 = gitops.tree_fingerprint(repo)
    subprocess.run(["git", "config", "heats.test", "1"], cwd=repo, check=True)
    f2 = gitops.tree_fingerprint(repo)
    assert len({f0, f1, f2}) == 3


# ----------------------------------------------------------------- checks
@pytest.mark.parametrize("out,expected", [
    ("..F\n1 failed, 2 passed in 0.1s", (2, 1)),
    ("3 passed in 0.02s", (3, 0)),
    ("1 failed, 4 passed, 1 error in 2s", (4, 2)),
    ("Tests:       1 failed, 5 passed, 6 total", (5, 1)),
    ("Tests:       7 passed, 7 total", (7, 0)),
    ("test result: ok. 4 passed; 0 failed; 0 ignored\ntest result: FAILED. 1 passed; 2 failed;", (5, 2)),
    ("--- PASS: TestA\n--- FAIL: TestB\n--- PASS: TestC", (2, 1)),
    ("nothing useful here", (None, None)),
])
def test_parse_counts(out, expected):
    assert parse_counts(out) == expected


def test_detect_check(tmp_path):
    assert detect_check(str(tmp_path)) is None
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert detect_check(str(tmp_path)) == "npm test"
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_check(str(tmp_path)) == "pnpm test"
    other = tmp_path / "py"
    (other / "tests").mkdir(parents=True)
    assert detect_check(str(other)) == "python -m pytest -q"


def test_run_check_timeout_kills_process_group(tmp_path):
    r = run_check(f"{PY} -c 'import time; time.sleep(30)'", str(tmp_path), timeout_s=1)
    assert r.timed_out and not r.ok and r.exit_code is None
    assert r.duration_s < 10


def test_run_check_reads_counts(repo):
    r = run_check(CHECK, repo, timeout_s=60)
    assert not r.ok and r.failed == 1 and r.passed == 1


@pytest.mark.parametrize("path,is_test", [
    ("tests/test_calc.py", True), ("test_x.py", True), ("pkg/foo_test.go", True), ("src/a.test.ts", True),
    ("__tests__/x.js", True), ("conftest.py", True), ("calc.py", False), ("src/testing_utils.py", False),
    ("latest/notes.md", False),
])
def test_is_test_path(path, is_test):
    assert is_test_path(path) is is_test


def test_guard_extra_paths():
    g = Guard(protect_tests=False, extra=["ci/", "Makefile"])
    assert g.violations(["ci/run.sh", "Makefile", "src/a.py", "tests/test_a.py"]) == ["ci/run.sh", "Makefile"]


# -------------------------------------------------------------- isolation
def make_lane(tmp_path):
    root = str(tmp_path / "repo")
    wt = os.path.join(root, ".heats", "r1", "h2")
    os.makedirs(wt)
    return Lane(root=root, worktree=wt, heat=2)


def test_relative_paths_land_in_worktree(tmp_path):
    lane = make_lane(tmp_path)
    new = rewrite("write_file", {"path": "src/a.py", "content": "x"}, lane)
    assert new["path"] == os.path.join(lane.worktree, "src", "a.py")
    assert lane.redirects == 0  # normal relative use is not an escape


def test_absolute_main_checkout_path_is_mapped(tmp_path):
    lane = make_lane(tmp_path)
    new = rewrite("patch", {"path": os.path.join(lane.root, "src", "a.py"), "old_string": "a", "new_string": "b"}, lane)
    assert new["path"] == os.path.join(lane.worktree, "src", "a.py")
    assert lane.redirects == 1


def test_paths_already_in_worktree_untouched(tmp_path):
    lane = make_lane(tmp_path)
    p = os.path.join(lane.worktree, "x.py")
    assert rewrite("read_file", {"path": p}, lane) is None


def test_outside_paths_untouched(tmp_path):
    lane = make_lane(tmp_path)
    assert map_path("/usr/include/stdio.h", lane) == "/usr/include/stdio.h"


def test_terminal_gets_worktree_and_command_rewritten(tmp_path):
    lane = make_lane(tmp_path)
    new = rewrite("terminal", {"command": f"cd {lane.root} && pytest"}, lane)
    assert new["workdir"] == lane.worktree
    assert new["command"] == f"cd {lane.worktree} && pytest"
    assert lane.redirects == 1
    # a command that already names the worktree is left alone
    new2 = rewrite("terminal", {"command": f"ls {lane.worktree}/src", "workdir": lane.worktree}, lane)
    assert new2 is None


def test_terminal_workdir_outside_is_pulled_back(tmp_path):
    lane = make_lane(tmp_path)
    new = rewrite("terminal", {"command": "ls", "workdir": "/tmp"}, lane)
    assert new["workdir"] == lane.worktree


def test_terminal_remembers_cwd(tmp_path):
    lane = make_lane(tmp_path)
    sub = os.path.join(lane.worktree, "pkg")
    note_terminal_result('{"output": "", "cwd": "%s"}' % sub, lane)
    assert rewrite("terminal", {"command": "ls"}, lane)["workdir"] == sub
    note_terminal_result('{"cwd": "/etc"}', lane)  # outside: ignored
    assert lane.cwd == sub
    note_terminal_result("not json", lane)


def test_search_files_default_path(tmp_path):
    lane = make_lane(tmp_path)
    assert rewrite("search_files", {"pattern": "foo"}, lane)["path"] == lane.worktree


def test_other_tools_untouched(tmp_path):
    lane = make_lane(tmp_path)
    assert rewrite("web_search", {"query": "x"}, lane) is None


def test_isolation_map():
    m = IsolationMap()
    lane = Lane("/r", "/r/.heats/a/h1", 1)
    m.add("child-1", lane)
    assert m.find(None, "", "child-1") is lane
    assert m.find("nope") is None
    assert m.remove("child-1") is lane and len(m) == 0


# ----------------------------------------------------------------- runner
class FakeLauncher:
    """Plays one scripted 'agent' per heat in a thread, writing through the isolation rewrite
    exactly as a real child's tool calls would be rewritten."""

    def __init__(self, isolation, behaviours, delay=0.05):
        self.isolation = isolation
        self.behaviours = behaviours
        self.delay = delay
        self.handles = {}
        self.cancelled = []
        self.goals = {}

    def launch(self, *, goal, context, model, key):
        heat = int(re.search(r"-h(\d+)(?:-lap\d+)?$", key).group(1))
        h = {"key": key, "heat": heat, "state": "RUNNING", "model": model, "goal": goal}
        self.goals.setdefault(heat, []).append(goal)
        self.handles[key] = h

        def work():
            time.sleep(self.delay)
            lane = None
            for _ in range(100):
                lane = self.isolation.find(key)
                if lane:
                    break
                time.sleep(0.01)
            fn = self.behaviours[heat - 1]
            try:
                fn(lane, self)
                h["state"] = "SUCCEEDED"
            except Exception as exc:  # noqa: BLE001
                h["state"], h["error"] = "FAILED", str(exc)

        threading.Thread(target=work, daemon=True).start()
        return h

    def task_keys(self, handle):
        return [handle["key"]]

    def state(self, handle, wait_s):
        time.sleep(min(wait_s, 0.02))
        return handle["state"]

    def cancel(self, handle, reason):
        self.cancelled.append(handle["key"])
        handle["state"] = "CANCELLED"

    def result(self, handle):
        return {"state": handle["state"], "summary": f"heat {handle['heat']} done", "error": handle.get("error")}


def tool_write(lane, path, content):
    args = rewrite("write_file", {"path": path, "content": content}, lane) or {"path": path}
    Path(args["path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(args["path"]).write_text(content)


def good_fix(lane, _):
    # aims at the MAIN checkout with an absolute path: isolation must redirect it
    tool_write(lane, os.path.join(lane.root, "calc.py"), "def add(a, b):\n    return a + b\n")


def wrong_fix(lane, _):
    tool_write(lane, "calc.py", "def add(a, b):\n    return a * b\n")


def cheat(lane, _):
    tool_write(lane, "calc.py", "def add(a, b):\n    return 5\n")
    tool_write(lane, "tests/test_calc.py", "def test_add():\n    assert True\n")


def test_end_to_end_picks_honest_winner(repo, tmp_path):
    iso = IsolationMap()
    fl = FakeLauncher(iso, [wrong_fix, cheat, good_fix])
    runner = Runner(fl, iso, tmp_path / "data")
    rep = runner.run(RunConfig(task="make add() add", n=3, check=CHECK, timeout_s=60), cwd=repo)
    assert rep.winner is not None and rep.winner.index == 3
    by = {h.index: h for h in rep.heats}
    assert by[2].check.ok and by[2].guarded == ["tests/test_calc.py"] and not by[2].qualifies
    assert not by[1].check.ok
    assert by[3].escaped == 1
    assert rep.baseline is not None and not rep.baseline.ok
    # main checkout untouched, worktrees and branches gone
    assert "a - b" in Path(repo, "calc.py").read_text()
    assert not Path(repo, ".heats").exists()
    br = subprocess.run(["git", "branch", "--list", "heats/*"], cwd=repo, capture_output=True, text=True).stdout
    assert br.strip() == ""
    st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert st.strip() == ""
    md = rep.markdown()
    assert "WIN" in md and "edited protected files" in md
    # saved, listable, appliable
    assert load_report(tmp_path / "data", rep.run_id)["winner"] == 3
    assert list_runs(tmp_path / "data")[0]["run_id"] == rep.run_id
    ok, msg = apply_heat(tmp_path / "data", rep.run_id)
    assert ok, msg
    assert "a + b" in Path(repo, "calc.py").read_text()
    ok, msg = apply_heat(tmp_path / "data", rep.run_id, heat=2)
    assert not ok and "force" in msg


def test_no_winner_when_all_fail(repo, tmp_path):
    iso = IsolationMap()
    fl = FakeLauncher(iso, [wrong_fix, wrong_fix])
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=2, check=CHECK, timeout_s=60), cwd=repo)
    assert rep.winner is None
    assert "no winner" in rep.markdown()
    ok, msg = apply_heat(tmp_path / "d", rep.run_id)
    assert not ok and "no winner" in msg


def test_timeout_cancels_and_judges(repo, tmp_path):
    def slow(lane, fl):
        tool_write(lane, "calc.py", "def add(a, b):\n    return a + b\n")
        time.sleep(30)

    iso = IsolationMap()
    fl = FakeLauncher(iso, [slow])
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=1, check=CHECK, timeout_s=2), cwd=repo)
    assert fl.cancelled
    assert rep.heats[0].agent_state == "TIMEOUT"
    assert rep.heats[0].check is not None and rep.heats[0].check.ok
    assert any("time limit" in e for e in rep.errors)
    assert not Path(repo, ".heats").exists()


def test_launch_failure_is_recorded(repo, tmp_path):
    class Refusing(FakeLauncher):
        def launch(self, **kw):
            if kw["key"].endswith("h1"):
                raise RuntimeError("no parent session")
            return super().launch(**kw)

    iso = IsolationMap()
    fl = Refusing(iso, [good_fix, good_fix])
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=2, check=CHECK, timeout_s=60), cwd=repo)
    h1 = next(h for h in rep.heats if h.index == 1)
    assert "launch failed" in h1.agent_error
    assert rep.winner and rep.winner.index == 2


def test_not_a_repo(tmp_path):
    iso = IsolationMap()
    with pytest.raises(gitops.HeatsGitError, match="not inside a git repository"):
        Runner(FakeLauncher(iso, []), iso, tmp_path / "d").run(RunConfig(task="t"), cwd=str(tmp_path))


def test_models_cycle_and_strategies_differ(repo, tmp_path):
    iso = IsolationMap()
    fl = FakeLauncher(iso, [good_fix, good_fix, good_fix])
    rep = Runner(fl, iso, tmp_path / "d").run(
        RunConfig(task="t", n=3, check=CHECK, models=["m1", "m2"], timeout_s=60, run_baseline=False), cwd=repo)
    assert [h.model for h in sorted(rep.heats, key=lambda h: h.index)] == ["m1", "m2", "m1"]
    assert len({h.strategy for h in rep.heats}) == 3
    goals = [fl.handles[k]["goal"] for k in sorted(fl.handles)]
    assert all("attempt" in g and CHECK in g for g in goals)


def test_escape_into_main_checkout_is_reported(repo, tmp_path):
    def escaper(lane, _):
        # bypasses the rewrite entirely, like a shell command with a computed path would
        Path(lane.root, "calc.py").write_text("def add(a, b):\n    return a + b\n")

    iso = IsolationMap()
    rep = Runner(FakeLauncher(iso, [escaper]), iso, tmp_path / "d").run(
        RunConfig(task="t", n=1, check=CHECK, timeout_s=60, run_baseline=False), cwd=repo)
    assert any("main checkout changed" in e for e in rep.errors)
    assert rep.winner is None  # the heat itself changed nothing in its worktree


def test_claimed_but_unchanged_is_called_out(repo, tmp_path):
    def liar(lane, fl):
        pass  # says it fixed things, touches nothing

    class Liar(FakeLauncher):
        def result(self, handle):
            return {"state": "SUCCEEDED", "summary": "I updated calc.py and the tests pass."}

    iso = IsolationMap()
    rep = Runner(Liar(iso, [liar]), iso, tmp_path / "d").run(
        RunConfig(task="t", n=1, check=CHECK, timeout_s=60, run_baseline=False), cwd=repo)
    assert rep.winner is None
    assert "claimed a fix but changed nothing" in rep.markdown()


def test_live_updates_stream_every_stage(repo, tmp_path):
    snaps = []
    iso = IsolationMap()
    fl = FakeLauncher(iso, [wrong_fix, good_fix])
    rep = Runner(fl, iso, tmp_path / "d", on_update=snaps.append).run(
        RunConfig(task="t", n=2, check=CHECK, timeout_s=60), cwd=repo)
    phases = [s["phase"] for s in snaps]
    assert phases[0] == "starting" and phases[-1] == "done"
    assert "running" in phases
    statuses = {st for s in snaps for st in (h["status"] for h in s["heats"])}
    assert {"queued", "running", "judging"} <= statuses
    final = snaps[-1]
    assert final["winner"] == 2 == rep.winner.index
    assert {h["index"]: h["status"] for h in final["heats"]} == {1: "fail", 2: "pass"}
    assert final["baseline"] == {"ok": False, "passed": 1, "failed": 1}
    json.dumps(final)  # serialisable


def test_broken_update_callback_does_not_break_run(repo, tmp_path):
    def boom(_):
        raise RuntimeError("ui crashed")
    iso = IsolationMap()
    rep = Runner(FakeLauncher(iso, [good_fix]), iso, tmp_path / "d", on_update=boom).run(
        RunConfig(task="t", n=1, check=CHECK, timeout_s=60, run_baseline=False), cwd=repo)
    assert rep.winner is not None


def test_dependency_dirs_are_linked_not_copied(repo):
    Path(repo, ".gitignore").write_text("node_modules/\n.venv/\n")
    subprocess.run("git -c user.email=t@t -c user.name=t add .gitignore && git -c user.email=t@t -c user.name=t commit -qm gi",
                   cwd=repo, shell=True, check=True)
    (Path(repo) / "node_modules" / "left-pad").mkdir(parents=True)
    (Path(repo) / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1")
    (Path(repo) / "web" / "node_modules").mkdir(parents=True)
    (Path(repo) / ".venv" / "bin").mkdir(parents=True)
    (Path(repo) / "build").mkdir()  # ignored? no: untracked, not a dependency dir
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "d", "h1")
    gitops.add_worktree(repo, wt, "heats/d/h1", base)
    linked = gitops.link_dependency_dirs(repo, wt)
    assert sorted(linked) == [".venv", "node_modules", "web/node_modules"]  # nested (monorepo) ones too
    assert os.path.islink(os.path.join(wt, "node_modules"))
    assert Path(wt, "node_modules", "left-pad", "index.js").exists()
    # the symlinks must never end up in the heat's patch
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n")
    p = gitops.collect_patch(wt, base, ignore=tuple(linked))
    assert p.files == ["calc.py"], p.files
    gitops.remove_worktree(repo, wt, "heats/d/h1")
    assert Path(repo, "node_modules", "left-pad", "index.js").exists()  # removing the heat kept the real folder



def test_relative_climb_out_is_mapped_back(tmp_path):
    lane = make_lane(tmp_path)
    p = map_path("../../../src/x.py", lane)
    assert p == os.path.join(lane.worktree, "src", "x.py")
    # another heat's worktree is not reachable either
    other = os.path.join(lane.root, ".heats", "r1", "h1", "a.py")
    assert map_path(other, lane) == lane.worktree


def test_root_prefix_lookalike_is_not_rewritten(tmp_path):
    lane = make_lane(tmp_path)
    sibling = lane.root + "2/x.py"  # /repo2 is not /repo
    assert map_path(sibling, lane) == sibling
    new = rewrite("terminal", {"command": f"cat {sibling}"}, lane)
    assert new["command"] == f"cat {sibling}"


@pytest.mark.parametrize("cmd,blocked", [
    ("git stash pop", "stash"), ("cd x && git stash", "stash"), ("git -C . config user.name x", "config"),
    ("git branch -D main", "branch"), ("git worktree prune", "worktree"), ("git push origin main", "push"),
    ("git tag -d v1", "tag"), ("git status", None), ("git diff HEAD", None), ("git add -A && git commit -m x", None),
    ("git branch", None), ("echo git stash", None), ("pytest -q", None),
])
def test_shared_git_commands_detected(cmd, blocked):
    from heats_core.isolation import shared_git_command
    got = shared_git_command(cmd)
    assert (got.lower() if got else None) == blocked


def test_worktree_failure_mid_setup_cleans_up(repo, tmp_path, monkeypatch):
    real_add = gitops.add_worktree
    calls = {"n": 0}

    def flaky(root, path, branch, base):
        calls["n"] += 1
        if calls["n"] == 3:
            raise gitops.HeatsGitError("disk full")
        return real_add(root, path, branch, base)

    monkeypatch.setattr(gitops, "add_worktree", flaky)
    iso = IsolationMap()
    with pytest.raises(gitops.HeatsGitError):
        Runner(FakeLauncher(iso, [good_fix] * 3), iso, tmp_path / "d").run(
            RunConfig(task="t", n=3, check=CHECK, run_baseline=False), cwd=repo)
    assert not Path(repo, ".heats").exists()
    br = subprocess.run(["git", "branch", "--list", "heats/*"], cwd=repo, capture_output=True, text=True).stdout
    assert br.strip() == ""


def test_still_running_child_is_blocked_not_released(repo, tmp_path):
    class Stubborn(FakeLauncher):
        def cancel(self, handle, reason):
            self.cancelled.append(handle["key"])  # ignores the cancel and keeps "running"

    def slow(lane, fl):
        time.sleep(60)

    iso = IsolationMap()
    fl = Stubborn(iso, [slow])
    rep = Runner(fl, iso, tmp_path / "d").run(
        RunConfig(task="t", n=1, check=CHECK, timeout_s=1, run_baseline=False), cwd=repo)
    lane = iso.find(rep.run_id and f"heats-{rep.run_id}-h1")
    assert lane is not None and lane.blocked


def test_apply_twice_is_refused(repo, tmp_path):
    iso = IsolationMap()
    rep = Runner(FakeLauncher(iso, [good_fix]), iso, tmp_path / "d").run(
        RunConfig(task="t", n=1, check=CHECK, timeout_s=60, run_baseline=False), cwd=repo)
    assert apply_heat(tmp_path / "d", rep.run_id)[0]
    ok, msg = apply_heat(tmp_path / "d", rep.run_id)
    assert not ok and "already applied" in msg


def test_check_output_is_bounded_and_group_killed(tmp_path):
    marker = tmp_path / "bg_alive"
    cmd = (f"{PY} -c \"print('x'*5_000_000)\"; "
           f"(sleep 3; touch {marker}) & exit 0")
    r = run_check(cmd, str(tmp_path), timeout_s=30, max_output_bytes=100_000)
    assert r.ok and len(r.tail) <= 2000
    time.sleep(4)
    assert not marker.exists()  # the background child was killed with the group


# ----------------------------------------------------------------- laps / finish early
def test_failing_heat_gets_another_lap_with_real_check_output(repo, tmp_path):
    def learns(lane, fl):
        if len(fl.goals[1]) == 1:
            wrong_fix(lane, fl)
        else:
            good_fix(lane, fl)

    iso = IsolationMap()
    fl = FakeLauncher(iso, [learns])
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=1, check=CHECK, timeout_s=60, laps=3,
                                                        min_lap_s=0), cwd=repo)
    assert len(fl.goals[1]) == 2                      # stopped relaunching once it passed
    second = fl.goals[1][1]
    assert "lap 2 of 3" in second and "exited 1" in second and "calc.py" in second
    assert rep.winner is not None and rep.winner.index == 1 and rep.heats[0].laps == 2
    assert "2 laps" in rep.markdown()
    assert len(iso) == 0                              # every lap's keys released
    assert "a - b" in Path(repo, "calc.py").read_text()


def test_lap_after_no_change_says_nothing_changed(repo, tmp_path):
    iso = IsolationMap()
    fl = FakeLauncher(iso, [lambda lane, fl: None])
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=1, check=CHECK, timeout_s=60, laps=2,
                                                        min_lap_s=0), cwd=repo)
    assert len(fl.goals[1]) == 2 and "NOTHING has been changed" in fl.goals[1][1]
    assert rep.winner is None and rep.heats[0].laps == 2


def test_no_new_lap_without_enough_time(repo, tmp_path):
    iso = IsolationMap()
    fl = FakeLauncher(iso, [wrong_fix])
    Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=1, check=CHECK, timeout_s=60, laps=3,
                                                  min_lap_s=120), cwd=repo)
    assert len(fl.goals[1]) == 1


def test_finish_early_stops_the_others(repo, tmp_path):
    def slow(lane, fl):
        time.sleep(30)

    iso = IsolationMap()
    fl = FakeLauncher(iso, [slow, good_fix])
    t = time.monotonic()
    rep = Runner(fl, iso, tmp_path / "d").run(RunConfig(task="t", n=2, check=CHECK, timeout_s=120,
                                                        finish_early=True, laps=3, min_lap_s=0), cwd=repo)
    assert time.monotonic() - t < 25
    assert rep.winner is not None and rep.winner.index == 2
    by = {h.index: h for h in rep.heats}
    assert by[1].agent_state == "STOPPED" and any(k.endswith("h1") for k in fl.cancelled)
    assert len(fl.goals[1]) == 1                      # a stopped heat gets no new lap
    assert not Path(repo, ".heats").exists()
