"""Tests for the Hermes-facing wiring in heats/__init__.py, with a fake PluginContext."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("heats_plugin_under_test", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


class FakeCtx:
    def __init__(self):
        self.tools, self.hooks, self.mw, self.cmds, self.cli, self.skills = {}, {}, {}, {}, {}, {}
        self.config = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_middleware(self, kind, cb):
        self.mw[kind] = cb

    def register_command(self, name, handler, **kw):
        self.cmds[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kw):
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path):
        self.skills[name] = path

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_matches_manifest(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text()
    for t in ctx.tools:
        assert f"- {t}" in manifest
    for h in ctx.hooks:
        assert f"- {h}" in manifest
    assert set(ctx.mw) == {"tool_request"}
    assert "heats" in ctx.cmds and "heats" in ctx.cli and "heats" in ctx.skills
    assert Path(ctx.skills["heats"]).is_file()


def test_middleware_only_touches_heat_children(plugin, tmp_path):
    root = str(tmp_path / "r")
    wt = os.path.join(root, ".heats", "run", "h1")
    os.makedirs(wt)
    lane = plugin.Lane(root=root, worktree=wt, heat=1) if hasattr(plugin, "Lane") else None
    from importlib import import_module
    iso_mod = sys.modules["heats_plugin_under_test.heats_core.isolation"]
    lane = iso_mod.Lane(root=root, worktree=wt, heat=1)
    plugin._ISOLATION.add("child-1", lane)
    try:
        out = plugin._mw_tool_request(tool_name="write_file", args={"path": "a.py", "content": ""},
                                      task_id="child-1", session_id="s")
        assert out["args"]["path"] == os.path.join(wt, "a.py") and out["source"] == "heats"
        assert plugin._mw_tool_request(tool_name="write_file", args={"path": "a.py"}, task_id="parent") is None
        # write guard: a path outside the worktree is blocked for the child only
        blocked = plugin._on_pre_tool_call(tool_name="write_file", args={"path": os.path.join(root, "a.py")}, task_id="child-1")
        assert blocked["action"] == "block"
        assert plugin._on_pre_tool_call(tool_name="write_file", args={"path": os.path.join(wt, "a.py")}, task_id="child-1") is None
        assert plugin._on_pre_tool_call(tool_name="write_file", args={"path": "/x"}, task_id="parent") is None
        assert plugin._on_pre_tool_call(tool_name="read_file", args={"path": "/x"}, task_id="child-1") is None
        plugin._on_post_tool_call(tool_name="terminal", result=json.dumps({"cwd": os.path.join(wt, "src")}), task_id="child-1")
        assert lane.cwd == os.path.join(wt, "src")
    finally:
        plugin._ISOLATION.remove("child-1")


def test_tools_validate_arguments(plugin):
    plugin.register(FakeCtx())
    assert "task is required" in plugin._handle_run({})
    assert "between 1 and" in plugin._handle_run({"task": "x", "n": 99})
    assert "run_id is required" in plugin._handle_apply({})
    assert json.loads(plugin._handle_apply({"run_id": "nope"}))["ok"] is False


def test_run_outside_git_returns_error_not_exception(plugin, tmp_path):
    plugin.register(FakeCtx())
    out = json.loads(plugin._handle_run({"task": "x", "repo_path": str(tmp_path)}))
    assert out["ok"] is False and "git" in out["error"]


def test_slash_command_with_no_runs_and_unknown(plugin):
    assert "No Heats runs yet" in plugin._cmd("")
    assert "Usage" in plugin._cmd("bogus")
    assert "no Heats run" in plugin._cmd("show 0000")


def test_slash_command_lists_and_shows_saved_run(plugin):
    d = plugin._data_dir() / "runs" / "r1"
    d.mkdir(parents=True)
    patch = d / "heat1.patch"
    patch.write_text("diff --git a/x b/x\n")
    rep = {"run_id": "r1", "root": "/tmp/proj", "check": "pytest", "winner": 1, "run_dir": str(d),
           "heats": [{"index": 1, "strategy": "minimal", "model": None, "agent_summary": "fixed",
                      "patch_file": str(patch), "check": {"tail": "1 passed"}, "qualifies": True, "files": ["x"]}]}
    (d / "report.json").write_text(json.dumps(rep))
    (d / "scoreboard.md").write_text("SCOREBOARD")
    assert "| r1 | proj | 1 | 1 |" in plugin._cmd("list")
    assert plugin._cmd("show r1") == "SCOREBOARD"
    shown = plugin._cmd("show r1 1")
    assert "diff --git" in shown and "1 passed" in shown


def _git_repo(path):
    import subprocess
    path.mkdir()
    subprocess.run("git init -q && echo x > a.txt && git add -A && "
                   "git -c user.name=t -c user.email=t@t commit -qm init", shell=True, cwd=path, check=True)
    return path


def test_long_race_returns_run_id_then_heats_wait_finishes(plugin, tmp_path, monkeypatch):
    import contextvars
    import threading
    import time as _t
    marker = contextvars.ContextVar("parent_session", default=None)
    seen = {}

    class SlowRunner:
        def __init__(self, *a, **kw):
            self._live = {"heats": [{"status": "running", "lap": 2}], "started_at": _t.time()}

        def run(self, cfg, cwd):
            seen["ctx"] = marker.get()          # the race thread must see the caller's context
            seen["run_id"] = cfg.run_id
            _t.sleep(1.5)
            run_dir = tmp_path / "rd"
            run_dir.mkdir()
            (run_dir / "scoreboard.md").write_text("SCOREBOARD")

            class Rep:
                def to_dict(self):
                    return {"run_id": cfg.run_id, "winner": 1, "run_dir": str(run_dir),
                            "heats": [{"index": 1, "check": {"ok": True}, "qualifies": True, "files": ["a.txt"]}]}
            return Rep()

    ctx = FakeCtx()
    ctx.subagent_lifecycle = object()
    ctx.config["wait_seconds"] = 5
    plugin.register(ctx)
    monkeypatch.setattr(plugin, "Runner", SlowRunner)
    monkeypatch.setattr(plugin, "_wait_budget", lambda: 0.2)
    repo = _git_repo(tmp_path / "repo")
    marker.set("parent-1")
    first = json.loads(plugin._handle_run({"task": "x", "repo_path": str(repo), "check": "true"}))
    assert first["status"] == "running" and first["run_id"] == seen["run_id"]
    assert "heats_wait" in first["next"] and "lap 2" in first["progress"]
    assert seen["ctx"] == "parent-1"
    monkeypatch.setattr(plugin, "_wait_budget", lambda: 10)
    done = json.loads(plugin._handle_wait({"run_id": first["run_id"]}))
    assert done["status"] == "done" and done["winner"] == 1 and done["scoreboard"] == "SCOREBOARD"
    assert json.loads(plugin._handle_wait({"run_id": "nope"}))["ok"] is False
    assert "run_id is required" in plugin._handle_wait({})


def test_heats_wait_reads_a_finished_run_from_disk(plugin, tmp_path):
    plugin.register(FakeCtx())
    d = plugin._data_dir() / "runs" / "0101-000000-abcd"
    d.mkdir(parents=True)
    (d / "scoreboard.md").write_text("OLD")
    (d / "report.json").write_text(json.dumps({"run_id": "0101-000000-abcd", "winner": None, "run_dir": str(d),
                                               "heats": []}))
    out = json.loads(plugin._handle_wait({"run_id": "0101-000000-abcd"}))
    assert out["status"] == "done" and out["scoreboard"] == "OLD" and out["winner"] is None


def test_misrouted_heats_call_gets_a_how_to_message(plugin):
    r = plugin._on_pre_tool_call(tool_name="execute_code", args={"code": "res = heats_run(task='fix', n=3)\nprint(res)"}, task_id="p")
    assert r and r["action"] == "block" and "tool_call" in r["message"] and "heats_run" in r["message"]
    r = plugin._on_pre_tool_call(tool_name="terminal", args={"command": "cd /x && heats_wait --run_id 1"}, task_id="p")
    assert r and "heats_wait" in r["message"]
    # mentions that are not calls stay allowed
    assert plugin._on_pre_tool_call(tool_name="terminal", args={"command": "grep -rn heats_run ."}, task_id="p") is None
    assert plugin._on_pre_tool_call(tool_name="execute_code", args={"code": "print('heats_run')"}, task_id="p") is None
    assert plugin._on_pre_tool_call(tool_name="read_file", args={"path": "heats_run("}, task_id="p") is None
