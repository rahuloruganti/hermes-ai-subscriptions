"""AI Subscriptions (catalog edition): local token usage across this PC.

Reads, read-only: Hermes state.db and models_dev_cache.json (price list), plus the
usage counters other CLIs already log (Claude Code projects/*.jsonl, Grok CLI
logs/unified.jsonl). It never reads a credential or calls a provider API.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

USER = Path.home()

SRC_LABELS = {"hermes": "Hermes", "claude": "Claude Code", "grok": "Grok CLI"}


# ── Paths and small helpers ─────────────────────────────────────────────────


def home() -> Path:
    candidates: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        candidates.append(Path(get_hermes_home()))
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        candidates.append(Path(env))
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "hermes"
    return USER / ".hermes"


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _ts(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    text = _FRACTION.sub(r"\1", str(value).strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso_from_ts(value: Any) -> Optional[str]:
    t = _ts(value)
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat() if t else None


# ── Pricing (list prices, only to put a dollar value on token counts) ───────

_PRICES: dict = {"mtime": 0.0, "data": {}}


def _pricing() -> dict:
    """model id -> {input, output, cache_read, cache_write} in USD per 1M tokens."""
    path = home() / "models_dev_cache.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _PRICES["mtime"]:
        raw = _json(path)
        table: dict = {}
        for prov_id in ("anthropic", "xai", "openai", "google"):
            models = ((raw or {}).get(prov_id) or {}).get("models") or {}
            for mid, info in models.items():
                cost = info.get("cost") if isinstance(info, dict) else None
                if isinstance(cost, dict) and mid not in table:
                    table[mid] = cost
        for prov in (raw or {}).values():
            models = (prov.get("models") if isinstance(prov, dict) else None) or {}
            for mid, info in models.items():
                cost = info.get("cost") if isinstance(info, dict) else None
                if isinstance(cost, dict) and mid not in table:
                    table[mid] = cost
        _PRICES["data"], _PRICES["mtime"] = table, mtime
    return _PRICES["data"]


def _cost(model: str, i: float, o: float, cr: float, cw: float) -> float:
    prices = _pricing()
    entry = prices.get(model) or prices.get(model.split("/")[-1])
    if not entry:
        return 0.0
    inp = _num(entry.get("input"))
    return (i * inp + o * _num(entry.get("output"))
            + cr * (_num(entry.get("cache_read")) or inp)
            + cw * (_num(entry.get("cache_write")) or inp)) / 1_000_000.0


# ── Usage scan: Hermes + Claude Code + Grok CLI ─────────────────────────────


def _rec(src, ts, model, provider, req, i, o, cr, cw, rs, cost, est):
    return {
        "src": src, "ts": ts, "model": model or "unknown", "provider": provider or src,
        "req": req, "i": i, "o": o, "cr": cr, "cw": cw, "rs": rs, "cost": cost, "est": est,
    }


def _scan_hermes() -> list:
    db = home() / "state.db"
    if not db.is_file():
        return []
    out: list = []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        have = set()
        for r in con.execute(
            """SELECT session_id, model, billing_provider, api_call_count, input_tokens,
                      output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
                      estimated_cost_usd, actual_cost_usd, last_seen FROM session_model_usage"""
        ):
            have.add(r[0])
            ts = _ts(r[11])
            if ts is None:
                continue
            cost = _num(r[10]) or _num(r[9])
            out.append(_rec("hermes", ts, r[1], r[2], int(_num(r[3])), _num(r[4]), _num(r[5]),
                            _num(r[6]), _num(r[7]), _num(r[8]), cost, False))
        for r in con.execute(
            """SELECT id, model, billing_provider, api_call_count, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, reasoning_tokens,
                      estimated_cost_usd, actual_cost_usd, last_activity_at FROM sessions"""
        ):
            if r[0] in have:
                continue
            ts = _ts(r[11])
            if ts is None or not (_num(r[4]) or _num(r[5])):
                continue
            cost = _num(r[10]) or _num(r[9])
            out.append(_rec("hermes", ts, r[1], r[2], int(_num(r[3])), _num(r[4]), _num(r[5]),
                            _num(r[6]), _num(r[7]), _num(r[8]), cost, False))
    finally:
        con.close()
    return out


def _scan_claude() -> list:
    root = USER / ".claude" / "projects"
    if not root.is_dir():
        return []
    best: dict = {}
    for path in root.rglob("*.jsonl"):
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                model = str(msg.get("model") or "")
                if not model or model.startswith("<"):
                    continue
                ts = _ts(obj.get("timestamp"))
                if ts is None:
                    continue
                u = msg["usage"]
                i, o = _num(u.get("input_tokens")), _num(u.get("output_tokens"))
                cr = _num(u.get("cache_read_input_tokens"))
                cw = _num(u.get("cache_creation_input_tokens"))
                details = u.get("output_tokens_details")
                rs = _num(details.get("thinking_tokens")) if isinstance(details, dict) else 0.0
                key = (msg.get("id") or obj.get("uuid"), obj.get("requestId"))
                prev = best.get(key)
                # One API call is logged once per content block; keep the final (largest) one.
                if prev is None or o > prev["o"]:
                    best[key] = _rec("claude", ts, model, "anthropic", 1, i, o, cr, cw, rs,
                                     _cost(model, i, o, cr, cw), True)
    return list(best.values())


def _scan_grok() -> list:
    log = USER / ".grok" / "logs" / "unified.jsonl"
    if not log.is_file():
        return []
    models: dict = {}
    for summary in (USER / ".grok" / "sessions").glob("*/*/summary.json"):
        data = _json(summary)
        if isinstance(data, dict):
            info = data.get("info") or {}
            if info.get("id") and data.get("current_model_id"):
                models[info["id"]] = data["current_model_id"]
    out: list = []
    with open(log, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "inference_done" not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("msg") != "shell.turn.inference_done":
                continue
            ts = _ts(obj.get("ts"))
            ctx = obj.get("ctx") or {}
            if ts is None:
                continue
            prompt, cached = _num(ctx.get("prompt_tokens")), _num(ctx.get("cached_prompt_tokens"))
            comp, rs = _num(ctx.get("completion_tokens")), _num(ctx.get("reasoning_tokens"))
            model = models.get(obj.get("sid")) or "grok-4.7"
            fresh_in = max(prompt - cached, 0.0)
            out.append(_rec("grok", ts, model, "xai-oauth", 1, fresh_in, comp, cached, 0.0, rs,
                            _cost(model, fresh_in, comp, cached, 0.0), True))
    return out


_RECS: dict = {"t": 0.0, "v": []}


def _records() -> list:
    if time.time() - _RECS["t"] < 30 and _RECS["v"]:
        return _RECS["v"]
    records: list = []
    for scan in (_scan_hermes, _scan_claude, _scan_grok):
        try:
            records.extend(scan())
        except Exception:
            continue
    _RECS["t"], _RECS["v"] = time.time(), records
    return records


def _tokens(r: dict) -> float:
    # Reasoning tokens are a subset of output tokens for every source, so they are not added.
    return r["i"] + r["o"] + r["cr"] + r["cw"]


def _local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _period(records: list, since: float) -> dict:
    sel = [r for r in records if r["ts"] >= since]
    return {
        "tokens": sum(_tokens(r) for r in sel),
        "cost": sum(r["cost"] for r in sel),
        "requests": sum(r["req"] for r in sel),
    }


def usage(days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    records = _records()
    now = time.time()
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    cutoff = midnight - (days - 1) * 86400
    window = [r for r in records if r["ts"] >= cutoff]

    by_day: dict = {}
    for k in range(days):
        day = _local_day(midnight - (days - 1 - k) * 86400 + 3600)
        by_day[day] = {"date": day, "s": {s: {"tokens": 0.0, "cost": 0.0, "requests": 0} for s in SRC_LABELS}}
    models: dict = {}
    by_source = {
        s: {"id": s, "label": lbl, "requests": 0, "input": 0.0, "output": 0.0, "cache_read": 0.0,
            "cache_write": 0.0, "reasoning": 0.0, "tokens": 0.0, "cost": 0.0, "estimated": s != "hermes"}
        for s, lbl in SRC_LABELS.items()
    }
    for r in window:
        cell = by_day.get(_local_day(r["ts"]))
        total = _tokens(r)
        if cell is not None:
            c = cell["s"][r["src"]]
            c["tokens"] += total
            c["cost"] += r["cost"]
            c["requests"] += r["req"]
        m = models.setdefault((r["src"], r["model"]), {
            "model": r["model"], "src": r["src"], "provider": r["provider"],
            "requests": 0, "tokens": 0.0, "cost": 0.0})
        m["requests"] += r["req"]
        m["tokens"] += total
        m["cost"] += r["cost"]
        b = by_source[r["src"]]
        b["requests"] += r["req"]
        b["input"] += r["i"]
        b["output"] += r["o"]
        b["cache_read"] += r["cr"]
        b["cache_write"] += r["cw"]
        b["reasoning"] += r["rs"]
        b["tokens"] += total
        b["cost"] += r["cost"]

    coverage: dict = {}
    for s in SRC_LABELS:
        mine = [r["ts"] for r in records if r["src"] == s]
        coverage[s] = {
            "records": len(mine),
            "first": datetime.fromtimestamp(min(mine), tz=timezone.utc).isoformat() if mine else None,
            "last": datetime.fromtimestamp(max(mine), tz=timezone.utc).isoformat() if mine else None,
        }
    return {
        "days": days,
        "generated_at": _now_iso(),
        "periods": {
            "today": _period(records, midnight),
            "d7": _period(records, midnight - 6 * 86400),
            "d30": _period(records, midnight - 29 * 86400),
            "all": _period(records, 0),
        },
        "by_day": list(by_day.values()),
        "by_model": sorted(models.values(), key=lambda x: x["tokens"], reverse=True)[:40],
        "by_source": list(by_source.values()),
        "coverage": coverage,
        "not_tracked": [
            {"name": "Gemini / Antigravity", "why": "writes no token counts to disk"},
            {"name": "OpenAI Codex", "why": "signed out on this PC (removed)"},
        ],
    }


# ── Subscription inventory (who is signed in) ───────────────────────────────
