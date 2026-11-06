"""Heats backend routes for the Desktop pane, mounted at /api/plugins/heats/.

GET  /live          latest run snapshot (the same payload as the plugin.heats.run.update event)
GET  /runs          recent runs (summaries)
POST /apply         {"run_id": str, "heat": int | null}: apply that heat's patch (default: the winner)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
_PKG = "heats_dashboard_core"


def _core():
    """Load heats_core from the package folder next to this file (works however the gateway imported us)."""
    if _PKG in sys.modules:
        return sys.modules[_PKG]
    root = Path(__file__).resolve().parent.parent / "heats_core"
    spec = importlib.util.spec_from_file_location(_PKG, root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


def _runner():
    _core()
    return importlib.import_module(f"{_PKG}.runner")


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return Path(plugin_data_dir("heats"))


@router.get("/live")
async def live():
    p = _data_dir() / "live.json"
    if not p.is_file():
        return {"run_id": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"run_id": None}


@router.get("/runs")
async def runs(limit: int = 10):
    out = []
    for r in _runner().list_runs(_data_dir(), limit=max(1, min(limit, 50))):
        out.append({"run_id": r["run_id"], "repo": Path(r["root"]).name, "n": len(r["heats"]),
                    "winner": r["winner"], "check": r["check"], "finished_at": r.get("finished_at")})
    return {"runs": out}


@router.post("/apply")
async def apply(body: dict):
    run_id = str((body or {}).get("run_id") or "").strip()
    if not run_id or "/" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="run_id is required")
    heat = (body or {}).get("heat")
    try:
        ok, msg = _runner().apply_heat(_data_dir(), run_id, int(heat) if heat else None, False)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if ok:
        live_path = _data_dir() / "live.json"
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
            if live.get("run_id") == run_id:
                live["applied"] = int(heat) if heat else live.get("winner")
                live_path.write_text(json.dumps(live), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return {"ok": ok, "message": msg}
