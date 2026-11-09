"""Regression tests for the reliability review (panels/reliability.md): git-guard bypasses, apply
destroying staged work, V4A patches, checks that cd into the main checkout, terminal path escapes,
stale worktrees after a crash, and test-guard gaps. Needs git; no Hermes."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from heats_core import gitops  # noqa: E402
from heats_core.checks import Guard, run_check, harness_changes  # noqa: E402
from heats_core.isolation import Lane, escaping_path, rewrite, shared_git_command  # noqa: E402

GIT_ID = "-c user.email=t@t -c user.name=t"


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("heats_plugin_rel_test", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "a.py").write_text("".join(f"line{i}\n" for i in range(30)))
    (r / "b.py").write_text("".join(f"b{i}\n" for i in range(30)))
    sh(f"git init -q && git {GIT_ID} add -A && git {GIT_ID} commit -qm init", r)
    return str(r.resolve())


def _lane(plugin, root, wt, key="child-1"):
    iso = sys.modules["heats_plugin_rel_test.heats_core.isolation"]
    lane = iso.Lane(root=root, worktree=wt, heat=1)
    plugin._ISOLATION.add(key, lane)
    return lane


# ------------------------------------------------------------ 1. git guard
BYPASSES = [
    "echo hi\ngit stash clear", "/usr/bin/git stash clear", "git --no-pager stash clear", "env git stash clear",
    "command git stash drop", "git -c x=y stash clear", "bash -c 'git stash clear'", "sh -lc \"git push\"",
    "ls | xargs -n1 git stash", "find . -exec git stash ;", "sudo -u me git push", "x=1; git  stash pop",
    "echo \"$(git stash)\"", "echo `git stash`", "eval 'git stash clear'", "git tag v9", "git branch newb",
    "git symbolic-ref HEAD refs/heads/main", "git update-index --assume-unchanged a", "git switch -c x",
    "git checkout -b x", "git fetch", "git -c alias.st=stash st clear", "GIT_DIR=/x/.git git status",
    "git --git-dir=/x/.git status", "python3 -c \"import os; os.system('git stash clear')\"",
    "git config user.email a@b", "git worktree add ../x", "git remote add o url", "git reflog expire --all",
    "git 'stash' clear", "true && git stash", "true || git stash", "(git stash)", "{ git stash; }",
]
ALLOWED = [
    "git status", "git diff HEAD", "git add -A && git commit -m 'fix git stash bug'", "git log --oneline | head",
    "git branch", "git branch --show-current", "git tag", "git tag -l", "git stash list", "echo git stash",
    "git checkout -- a.py", "git restore a.py", "git reset --hard HEAD", "git config --get user.name",
    "git -C . status", "pytest -q", "grep -rn stash .", "git rev-parse HEAD",
]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_git_guard_blocks_bypasses(plugin, tmp_path, cmd):
    wt = str(tmp_path / "r" / ".heats" / "run" / "h1")
    os.makedirs(wt)
    _lane(plugin, str(tmp_path / "r"), wt)
    try:
        r = plugin._on_pre_tool_call(tool_name="terminal", args={"command": cmd, "workdir": wt}, task_id="child-1")
        assert r and r["action"] == "block", cmd
    finally:
        plugin._ISOLATION.remove("child-1")


@pytest.mark.parametrize("cmd", ALLOWED)
def test_git_guard_allows_local_work(plugin, tmp_path, cmd):
    wt = str(tmp_path / "r" / ".heats" / "run" / "h1")
    os.makedirs(wt)
    Path(wt, "a.py").write_text("x")
    _lane(plugin, str(tmp_path / "r"), wt)
    try:
        assert plugin._on_pre_tool_call(tool_name="terminal", args={"command": cmd, "workdir": wt},
                                        task_id="child-1") is None, cmd
    finally:
        plugin._ISOLATION.remove("child-1")


def test_newline_chained_stash_clear_cannot_wipe_user_stash(repo):
    """The confirmed exploit: `echo hi\\ngit stash clear` passed the old regex."""
    assert shared_git_command("echo hi\ngit stash clear") == "stash"


def test_checkout_of_user_branch_blocked_but_file_checkout_allowed(repo):
    sh(f"git {GIT_ID} branch feature", repo)
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "0101-000000-abcd", "h1")
    gitops.add_worktree(repo, wt, "heats/0101-000000-abcd/h1", base)
    try:
        assert shared_git_command("git checkout feature", wt) == "checkout"
        assert shared_git_command("git checkout a.py", wt) is None
        assert shared_git_command("git checkout HEAD~0", wt) is None
    finally:
        gitops.remove_worktree(repo, wt, "heats/0101-000000-abcd/h1")


def test_pre_tool_call_fails_closed(plugin, tmp_path, monkeypatch):
    wt = str(tmp_path / "r" / ".heats" / "run" / "h1")
    os.makedirs(wt)
    _lane(plugin, str(tmp_path / "r"), wt)
    iso = sys.modules["heats_plugin_rel_test.heats_core.isolation"]
    monkeypatch.setattr(iso, "git_guard", lambda *a, **k: 1 / 0)
    try:
        r = plugin._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, task_id="child-1")
        assert r["action"] == "block"
    finally:
        plugin._ISOLATION.remove("child-1")


# ------------------------------------------------------------ 2. apply
def _heat_patch(repo, edit):
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "0101-000000-abcd", "h1")
    gitops.add_worktree(repo, wt, "heats/0101-000000-abcd/h1", base)
    edit(wt)
    p = gitops.collect_patch(wt, base)
    gitops.remove_worktree(repo, wt, "heats/0101-000000-abcd/h1")
    return p


def _state(repo):
    return (sh("git status --porcelain", repo), sh("git diff --cached", repo), sh("git diff", repo),
            Path(repo, "a.py").read_text())


def _replace(path, old, new):
    Path(path).write_text(Path(path).read_text().replace(old, new))


def test_apply_refuses_when_user_has_staged_edits(repo):
    _replace(f"{repo}/a.py", "line1\n", "line1 USER-STAGED\n")
    sh("git add a.py", repo)
    p = _heat_patch(repo, lambda wt: (_replace(f"{wt}/a.py", "line15\n", "line15 HEAT\n"),
                                      _replace(f"{wt}/b.py", "b3\n", "b3 HEAT\n")))
    _replace(f"{repo}/a.py", "line15\n", "line15 USER2\n")
    sh("git add a.py", repo)  # staged: invisible to the old `git diff --name-only` dirty test
    before = _state(repo)
    ok, msg = gitops.apply_patch(repo, p.data)
    assert not ok and "a.py" in msg and "Nothing was changed" in msg
    assert _state(repo) == before  # nothing staged, no conflict markers, b.py untouched
    assert "restore --staged --worktree" not in msg


def test_apply_refuses_untracked_file_in_patch_path(repo):
    p = _heat_patch(repo, lambda wt: (Path(wt, "new.py").write_text("heat\n"),
                                      _replace(f"{wt}/a.py", "line5\n", "line5 HEAT\n")))
    Path(repo, "new.py").write_text("mine\n")
    ok, msg = gitops.apply_patch(repo, p.data)
    assert not ok
    assert Path(repo, "new.py").read_text() == "mine\n"
    assert "HEAT" not in Path(repo, "a.py").read_text()


def test_apply_conflict_is_all_or_nothing(repo):
    p = _heat_patch(repo, lambda wt: (_replace(f"{wt}/a.py", "line15\n", "line15 HEAT\n"),
                                      _replace(f"{wt}/b.py", "b3\n", "b3 HEAT\n")))
    _replace(f"{repo}/a.py", "line15\n", "line15 COMMITTED\n")  # the user commits a conflicting change
    sh(f"git {GIT_ID} commit -qam c2", repo)
    ok, msg = gitops.apply_patch(repo, p.data)
    assert not ok and "a.py" in msg
    assert sh("git status --porcelain", repo) == ""  # b.py not left applied or staged, no markers
    assert "<<<<<<<" not in Path(repo, "a.py").read_text()


def test_apply_3way_success_leaves_nothing_staged(repo):
    p = _heat_patch(repo, lambda wt: _replace(f"{wt}/a.py", "line15\n", "line15 HEAT\n"))
    _replace(f"{repo}/a.py", "line12\n", "line12 COMMITTED\n")  # in the patch's context: clean apply fails
    sh(f"git {GIT_ID} commit -qam c2", repo)
    ok, msg = gitops.apply_patch(repo, p.data)
    assert ok, msg
    a = Path(repo, "a.py").read_text()
    assert "line15 HEAT" in a and "line12 COMMITTED" in a
    assert sh("git diff --cached", repo) == ""


# ------------------------------------------------------------ 3. V4A patches
def test_v4a_headers_rewritten_into_worktree_and_outside_blocked(plugin, tmp_path):
    root = str(tmp_path / "r")
    wt = os.path.join(root, ".heats", "run", "h1")
    os.makedirs(wt)
    _lane(plugin, root, wt)
    try:
        patch = (f"*** Begin Patch\n*** Update File: {root}/src/x.py\n@@\n-a\n+b\n*** Add File: src/new.py\n+x\n"
                 f"*** Move File: old.py -> {root}/moved.py\n*** End Patch")
        out = plugin._mw_tool_request(tool_name="patch", args={"mode": "patch", "patch": patch}, task_id="child-1")
        new = out["args"]["patch"]
        assert f"*** Update File: {wt}/src/x.py" in new
        assert f"*** Add File: {wt}/src/new.py" in new
        assert f"*** Move File: {wt}/old.py -> {wt}/moved.py" in new
        assert plugin._on_pre_tool_call(tool_name="patch", args=out["args"], task_id="child-1") is None
        bad = {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: /etc/evil\n+x\n*** End Patch"}
        assert plugin._on_pre_tool_call(tool_name="patch", args=bad, task_id="child-1")["action"] == "block"
        rel = {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: src/n.py\n+x\n*** End Patch"}
        assert plugin._on_pre_tool_call(tool_name="patch", args=rel, task_id="child-1")["action"] == "block"
    finally:
        plugin._ISOLATION.remove("child-1")


# ------------------------------------------------------------ 4. check that cds into the main checkout
def test_check_that_cds_into_root_is_judged_on_the_worktree(repo):
    Path(repo, "ok.txt").write_text("yes\n")
    sh(f"git {GIT_ID} add -A && git {GIT_ID} commit -qm ok", repo)
    base, _ = gitops.snapshot_base(repo)
    wt = os.path.join(repo, ".heats", "0101-000000-abcd", "h1")
    gitops.add_worktree(repo, wt, "heats/0101-000000-abcd/h1", base)
    try:
        Path(wt, "ok.txt").write_text("BROKEN\n")
        res = run_check(f"cd '{repo}' && grep -q yes ok.txt", wt, timeout_s=30)
        assert not res.ok  # judged on the heat's broken file, not the user's good one
        assert run_check(f"cd '{repo}' && grep -q yes ok.txt", repo, timeout_s=30).ok  # main checkout: unchanged
    finally:
        gitops.remove_worktree(repo, wt, "heats/0101-000000-abcd/h1")


# ------------------------------------------------------------ 5. terminal path escapes
def test_cd_climbing_out_of_worktree_is_blocked(plugin, tmp_path):
    root = str(tmp_path / "r")
    wt = os.path.join(root, ".heats", "run", "h1")
    os.makedirs(os.path.join(wt, "src"))
    _lane(plugin, root, wt)
    try:
        for cmd in ("cd ../../.. && echo x > src/x.py", "echo x > ../../../a.py", "cd src && cd ../../.. && ls",
                    "ls ../h2", "cd .."):
            r = plugin._on_pre_tool_call(tool_name="terminal", args={"command": cmd, "workdir": wt}, task_id="child-1")
            assert r and r["action"] == "block", cmd
        for cmd in ("cd src && ls ..", "ls ./src", "cat /etc/hosts", "cd /tmp && ls"):
            assert plugin._on_pre_tool_call(tool_name="terminal", args={"command": cmd, "workdir": wt},
                                            task_id="child-1") is None, cmd
    finally:
        plugin._ISOLATION.remove("child-1")


def test_backslash_tilde_and_tmp_alias_spellings_rewritten():
    home = os.path.expanduser("~")
    root = os.path.join(home, "heats fake repo")
    lane = Lane(root=root, worktree=os.path.join(root, ".heats", "r", "h1"), heat=1)
    esc_root = root.replace(" ", "\\ ")
    esc_wt = lane.worktree.replace(" ", "\\ ")
    assert rewrite("terminal", {"command": f"cd {esc_root} && ls"}, lane)["command"] == f"cd {esc_wt} && ls"
    tilde = "~/heats\\ fake\\ repo/src"
    assert rewrite("terminal", {"command": f"cd {tilde}"}, lane)["command"] == "cd ~/heats\\ fake\\ repo/.heats/r/h1/src"
    assert rewrite("terminal", {"command": f"cd {esc_wt}"}, lane)["command"] == f"cd {esc_wt}"  # already inside
    lane2 = Lane(root="/private/tmp/repo", worktree="/private/tmp/repo/.heats/r/h1", heat=1)
    assert rewrite("terminal", {"command": "cd /tmp/repo && ls"}, lane2)["command"] == "cd /tmp/repo/.heats/r/h1 && ls"


def test_escaping_path_sees_through_backslash_spaces(tmp_path):
    root = str(tmp_path / "my repo")
    wt = os.path.join(root, ".heats", "r", "h1")
    os.makedirs(wt)
    lane = Lane(root=root, worktree=wt, heat=1)
    assert escaping_path("cat ../../../x.py", wt, lane) == "../../../x.py"
    assert escaping_path("cat " + os.path.join(root, "x.py").replace(" ", "\\ "), wt, lane)


# ------------------------------------------------------------ 6. stale worktrees after a crash
def test_prune_stale_runs_removes_crash_leftovers(repo):
    base, _ = gitops.snapshot_base(repo)
    dead, live = "1109-120000-dead", "1109-120001-beef"
    for run in (dead, live):
        for h in (0, 1):
            gitops.add_worktree(repo, os.path.join(repo, ".heats", run, f"h{h}"), f"heats/{run}/h{h}", base)
    sh(f"git worktree lock .heats/{dead}/h0", repo)
    sh(f"git {GIT_ID} branch mine", repo)
    cleaned = gitops.prune_stale_runs(repo, active={live})
    assert cleaned == [dead]
    wl = sh("git worktree list", repo)
    assert dead not in wl and live in wl
    branches = sh("git branch --format='%(refname:short)'", repo).split()
    assert not [b for b in branches if dead in b] and f"heats/{live}/h1" in branches and "mine" in branches
    assert not Path(repo, ".heats", dead).exists() and Path(repo, ".heats", live).exists()
    assert gitops.prune_stale_runs(repo, active=(), min_age_s=3600) == []  # too young: kept
    for h in (0, 1):
        gitops.remove_worktree(repo, os.path.join(repo, ".heats", live, f"h{h}"), f"heats/{live}/h{h}")


# ------------------------------------------------------------ 7. test guard gaps
def test_non_ascii_test_file_is_guarded(repo):
    os.makedirs(f"{repo}/tests")
    Path(repo, "tests", "tést_x.py").write_text("def test(): assert 1\n")
    sh(f"git {GIT_ID} add -A && git {GIT_ID} commit -qm t", repo)
    p = _heat_patch(repo, lambda wt: Path(wt, "tests", "tést_x.py").write_text("def test(): pass\n"))
    assert p.files == ["tests/tést_x.py"]
    assert Guard().violations(p.files) == ["tests/tést_x.py"]


def test_test_command_definition_changes_are_guarded(repo):
    Path(repo, "package.json").write_text('{\n  "name": "x",\n  "scripts": {\n    "test": "jest"\n  },\n'
                                          '  "dependencies": {\n    "a": "1"\n  }\n}\n')
    Path(repo, "pyproject.toml").write_text('[project]\nname = "x"\n\n[tool.pytest.ini_options]\nminversion = "6"\n'
                                            'testpaths = ["tests"]\n\n\n\n\n[tool.black]\nline-length = 88\n')
    Path(repo, "Makefile").write_text("build:\n\techo build\n\n\n\n\ntest:\n\tpytest -q\n")
    sh(f"git {GIT_ID} add -A && git {GIT_ID} commit -qm cfg", repo)

    def cheat(wt):
        _replace(f"{wt}/package.json", '"test": "jest"', '"test": "exit 0"')
        _replace(f"{wt}/pyproject.toml", 'testpaths = ["tests"]', 'testpaths = ["tests"]\naddopts = "--deselect t"')
        _replace(f"{wt}/Makefile", "\tpytest -q", "\ttrue")
        Path(wt, "pytest.ini").write_text("[pytest]\naddopts = -k nothing\n")
    p = _heat_patch(repo, cheat)
    v = Guard().violations(p.files)
    assert {"package.json", "pyproject.toml", "Makefile", "pytest.ini"} <= set(v)

    def honest(wt):
        _replace(f"{wt}/package.json", '"a": "1"', '"a": "2"')
        _replace(f"{wt}/pyproject.toml", "line-length = 88", "line-length = 100")
        _replace(f"{wt}/Makefile", "\techo build", "\techo built")
    p2 = _heat_patch(repo, honest)
    assert Guard().violations(p2.files) == []
    assert harness_changes(p2.data) == []
