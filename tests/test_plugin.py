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
