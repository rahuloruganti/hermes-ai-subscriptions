"""AI Subscriptions — desktop dashboard for AI subscriptions + local usage stats.

The UI half lives in ``desktop-plugins/ai-subscriptions/plugin.js``; this package
hosts the read-only API half (``dashboard/plugin_api.py``) which the page reaches
via ``ctx.rest``, plus one observer hook, ``post_api_request``, that records a
timed line per completed model call so the dashboard can show measured
tokens/second for Hermes' own models. Numbers only: no prompt, response or
credential is read or stored. Timings start from the first call after Hermes
restarts; older sessions are not backfilled.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_WRITES = {"n": 0}
MAX_BYTES = 5 * 1024 * 1024


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _log_path() -> Path:
    folder = _home() / "plugin-data" / "ai-subscriptions"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "api_speed.jsonl"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rotate(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_suffix(".jsonl.1"))
    except OSError:
        pass


def record_api_call(**payload: Any) -> None:
    """Hook callback. Numbers only; never raises into the agent."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    out = _num(usage.get("output_tokens")) or _num(usage.get("completion_tokens"))
    seconds = _num(payload.get("api_duration"))
    if out <= 0 or seconds <= 0:
        return
    started = _num(payload.get("started_at"))
    first = _num(payload.get("first_chunk_at"))
    ended = _num(payload.get("ended_at")) or (started + seconds) or time.time()
    ttft = first - started if started and first >= started else None
    row = {
        "ts": round(ended, 3),
        "model": str(payload.get("model") or ""),
        "provider": str(payload.get("provider") or ""),
        "out": out,
        "sec": seconds,
        "ttft": round(ttft, 3) if ttft else None,
        "session": str(payload.get("session_id") or "")[:64],
    }
    line = json.dumps(row, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            path = _log_path()
            _WRITES["n"] += 1
            if _WRITES["n"] % 200 == 0:
                _rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


def register(ctx):
    """Observer hook only; the desktop page talks to dashboard/plugin_api.py."""
    ctx.register_hook("post_api_request", record_api_call)

