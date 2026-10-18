"""AI Subscriptions dashboard API, mounted at /api/plugins/ai-subscriptions/.

Read-only. Deliberately thin: all logic lives in core.py, which is re-imported whenever its
mtime changes. Edit core.py and the next request uses it; only changes to the
route list in this file need a Hermes restart.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter()
_CORE = Path(__file__).resolve().parent / "core.py"
_state = {"mtime": 0.0, "mod": None}


def core():
    mtime = _CORE.stat().st_mtime
    if _state["mod"] is None or mtime != _state["mtime"]:
        name = "hermes_ai_subscriptions_core"
        spec = importlib.util.spec_from_file_location(name, _CORE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _state["mod"], _state["mtime"] = module, mtime
    return _state["mod"]


@router.get("/usage")
async def usage(days: int = Query(30, ge=1, le=365)) -> dict:
    return await asyncio.to_thread(lambda: core().usage(days))
