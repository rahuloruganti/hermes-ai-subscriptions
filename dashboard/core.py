"""AI Subscriptions core: everything the dashboard routes return.

plugin_api.py reloads this file whenever it changes on disk, so edits here take
effect on the next request with no Hermes restart.

Read-only. Tokens are used for outbound usage calls and never returned.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

USER = Path.home()

SRC_LABELS = {"hermes": "Hermes", "claude": "Claude Code", "grok": "Grok CLI"}
ALIASES = {"grok": "xai-oauth", "xai": "xai-oauth", "claude": "anthropic"}
NAMES = {
    "nous": "Nous Portal",
    "anthropic": "Claude",
    "xai-oauth": "Grok (xAI)",
    "copilot": "GitHub Copilot",
    "openai-codex": "OpenAI Codex",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
}


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


def _claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── Pricing (list prices, only to put a dollar value on token counts) ───────

_PRICES: dict = {"mtime": 0.0, "data": {}}


def _pricing() -> dict:
    path = USER / ".tokentracker" / "cache" / "pricing.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _PRICES["mtime"]:
        data = _json(path)
        _PRICES["data"] = data if isinstance(data, dict) else {}
        _PRICES["mtime"] = mtime
    return _PRICES["data"]


def _cost(model: str, i: float, o: float, cr: float, cw: float) -> float:
    prices = _pricing()
    entry = None
    for key in (model, "anthropic/" + model, "xai/" + model, model.split("/")[-1]):
        candidate = prices.get(key)
        if isinstance(candidate, dict):
            entry = candidate
            break
    if not entry:
        return 0.0
    in_c = _num(entry.get("input_cost_per_token"))
    out_c = _num(entry.get("output_cost_per_token"))
    cr_c = _num(entry.get("cache_read_input_token_cost")) or in_c
    cw_c = _num(entry.get("cache_creation_input_token_cost")) or in_c
    return i * in_c + o * out_c + cr * cr_c + cw * cw_c


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


def _canon(key: str) -> str:
    k = str(key or "").strip().lower()
    return ALIASES.get(k, k)


def subscriptions() -> dict:
    h = home()
    auth = _json(h / "auth.json") or {}
    if not isinstance(auth, dict):
        auth = {}
    providers: dict = {}

    def slot(key: str) -> dict:
        return providers.setdefault(_canon(key), {
            "name": NAMES.get(_canon(key), _canon(key).replace("-", " ").title()),
            "account": None, "auth": None, "pool": [], "quota": None, "sources": []})

    for prov, data in (auth.get("providers") or {}).items():
        if isinstance(data, dict):
            s = slot(prov)
            s["auth"] = {"mode": data.get("auth_mode"),
                         "expires_at": data.get("expires_at") or data.get("agent_key_expires_at"),
                         "last_refresh": data.get("last_refresh") or data.get("obtained_at")}
            if "hermes login" not in s["sources"]:
                s["sources"].append("hermes login")
    for prov, entries in (auth.get("credential_pool") or {}).items():
        if not isinstance(entries, list) or not entries:
            continue
        s = slot(prov)
        for e in entries:
            if not isinstance(e, dict):
                continue
            s["pool"].append({"label": e.get("label") or e.get("source"), "status": e.get("last_status"),
                              "failure": e.get("failure_reason"), "requests": e.get("request_count")})
            if "@" in str(e.get("label") or "") and not s["account"]:
                s["account"] = e.get("label")
        if "hermes login" not in s["sources"]:
            s["sources"].append("hermes login")

    claude_cfg = _json(USER / ".claude.json") or {}
    oauth = claude_cfg.get("oauthAccount") if isinstance(claude_cfg, dict) else None
    if (USER / ".claude" / ".credentials.json").is_file() or oauth:
        s = slot("anthropic")
        s["sources"].append("Claude Code login")
        if isinstance(oauth, dict):
            s["account"] = oauth.get("emailAddress") or s["account"]
    grok_auth = _json(USER / ".grok" / "auth.json")
    if isinstance(grok_auth, dict) and grok_auth:
        s = slot("xai-oauth")
        s["sources"].append("Grok CLI login")
        first = next(iter(grok_auth.values()), None)
        if isinstance(first, dict):
            s["account"] = first.get("email") or s["account"]

    quota = _json(h / "quota_cache.json") or {}
    cached_at = quota.get("fetched_at") if isinstance(quota, dict) else None
    for prov, data in ((quota.get("providers") or {}) if isinstance(quota, dict) else {}).items():
        key = _canon(prov)
        # A cached plan for a provider that is no longer signed in must not show as a subscription.
        if key not in providers or not isinstance(data, dict):
            continue
        cached_ts = _ts(cached_at)
        fresh_enough = cached_ts is not None and time.time() - cached_ts < 6 * 3600
        if fresh_enough and (data.get("windows") or data.get("plan") or data.get("details")):
            providers[key]["quota"] = {"plan": data.get("plan"), "windows": data.get("windows") or [],
                                       "details": data.get("details") or []}
    return {"generated_at": _now_iso(), "quota_cached_at": cached_at, "providers": providers,
            "last_good": _load_state().get("snaps") or {}}


# ── Live provider polls ─────────────────────────────────────────────────────


def _get_json(url: str, headers: dict, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _snap(provider: str, plan: Optional[str], windows: list, details: list) -> dict:
    return {"provider": provider, "source": "live", "fetched_at": _now_iso(), "plan": plan,
            "windows": windows, "details": details}


def _money(value: Any) -> str:
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


# ── Persistent state: last good readings and the Anthropic cooldown ─────────
# Kept on disk so a restart or a core.py reload neither forgets a rate-limit
# lockout nor falls back to the days-old quota_cache.json.


def _state_file() -> Path:
    d = home() / "plugin-data" / "ai-subscriptions"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _load_state() -> dict:
    data = _json(_state_file())
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    try:
        path = _state_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _remember(provider: str, snap: dict) -> None:
    state = _load_state()
    state.setdefault("snaps", {})[provider] = snap
    _save_state(state)


def _last_good(provider: str, note: str) -> Optional[dict]:
    snap = (_load_state().get("snaps") or {}).get(provider)
    if not isinstance(snap, dict):
        return None
    out = dict(snap)
    out["details"] = [d for d in (out.get("details") or []) if not str(d).startswith("last check failed")]
    out["details"].append(note)
    out["stale"] = True
    return out


def _claude_token() -> str:
    """Claude Code's own OAuth token, read-only.

    Hermes' resolve_anthropic_token() reads secrets through a profile scope that
    does not exist inside plugin routes on the multiplexed gateway (it raises
    UnscopedSecretError), and it may also refresh the token. This reads the file
    Claude Code already maintains and never writes to it.
    """
    creds = _json(USER / ".claude" / ".credentials.json")
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = str((oauth or {}).get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("no Claude Code login found; run `claude` once to sign in")
    expires = _num((oauth or {}).get("expiresAt"))
    if expires and expires / 1000.0 < time.time():
        raise RuntimeError("Claude Code login has expired; open Claude Code once and it refreshes itself")
    return token


# Anthropic's usage endpoint answers 429 (Retry-After 300) after about five calls
# in under a minute, so Claude is called at most once a minute and a lockout is
# honoured across restarts.
CLAUDE_MIN_GAP = 60.0


def _fetch_anthropic(auth: dict) -> dict:
    from agent.account_usage import _usage_windows

    state = _load_state()
    gate = state.get("claude_gate") or {}
    now = time.time()
    last = (state.get("snaps") or {}).get("anthropic")
    if now < _num(gate.get("until")):
        note = f"last check failed: Anthropic rate limit, retrying in {int(_num(gate.get('until')) - now) + 1}s"
        cached = _last_good("anthropic", note)
        if cached:
            return cached
        raise RuntimeError(note.replace("last check failed: ", ""))
    if last and now - _num(gate.get("last_call")) < CLAUDE_MIN_GAP:
        return dict(last)

    token = _claude_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
               "User-Agent": "claude-code/2.1.0"}
    state["claude_gate"] = {"until": _num(gate.get("until")), "last_call": now}
    _save_state(state)
    try:
        payload = _get_json("https://api.anthropic.com/api/oauth/usage", headers, timeout=15.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                retry = int(exc.headers.get("Retry-After") or 300)
            except ValueError:
                retry = 300
            state = _load_state()
            state["claude_gate"] = {"until": time.time() + retry, "last_call": now}
            _save_state(state)
            note = f"last check failed: Anthropic rate limit, retrying in {retry}s"
            cached = _last_good("anthropic", note)
            if cached:
                return cached
            raise RuntimeError(f"Anthropic rate limit, retrying in {retry}s")
        raise
    wins = _usage_windows(
        payload, (("five_hour", "5-hour session"), ("seven_day", "Weekly"), ("seven_day_opus", "Opus weekly"),
                  ("seven_day_sonnet", "Sonnet weekly")), "utilization", "resets_at", fraction=True,
        model_scoped={"seven_day_opus", "seven_day_sonnet"})
    windows = [{
        "label": w.label, "used_percent": w.used_percent,
        "reset_at": w.reset_at.isoformat() if hasattr(w.reset_at, "isoformat") else None,
        "detail": w.detail} for w in wins]
    if not windows:
        raise RuntimeError("Claude usage API returned no windows")
    details: list = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("monthly_limit"):
        details.append(f"extra usage {_money(extra.get('used_credits'))} of {_money(extra.get('monthly_limit'))} {extra.get('currency') or 'USD'}")
    cfg = _json(USER / ".claude.json") or {}
    acct = cfg.get("oauthAccount") if isinstance(cfg, dict) else None
    plan = None
    if isinstance(acct, dict):
        kind = str(acct.get("organizationType") or "")
        plan = {"claude_pro": "Pro", "claude_max": "Max", "claude_team": "Team"}.get(kind) or (
            kind.replace("claude_", "").title() or None)
    snap = _snap("anthropic", plan, windows, details)
    _remember("anthropic", snap)
    return snap


def _fetch_xai(auth: dict) -> dict:
    tokens = ((auth.get("providers") or {}).get("xai-oauth") or {}).get("tokens") or {}
    token = str(tokens.get("access_token") or "").strip()
    if not token:
        for e in (auth.get("credential_pool") or {}).get("xai-oauth") or []:
            token = str((e or {}).get("access_token") or "").strip()
            if token:
                break
    if not token:
        raise RuntimeError("no xAI token")
    claims = _claims(token)
    user_id = str(claims.get("principal_id") or claims.get("sub") or "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "User-Agent": "hermes-ai-subscriptions", "X-XAI-Token-Auth": "xai-grok-cli",
               "x-userid": user_id, "x-grok-client-mode": "cli"}
    try:
        payload = _get_json("https://cli-chat-proxy.grok.com/v1/billing?format=credits", headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("xAI token expired; Hermes refreshes it the next time it uses Grok")
        raise
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload

    def cents(v: Any) -> Optional[float]:
        v = v.get("val") if isinstance(v, dict) else v
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    reset = period.get("end") or cfg.get("billingPeriodEnd")
    kind = str(period.get("type") or "").upper()
    label = "Monthly" if "MONTH" in kind else "Daily" if "DAY" in kind else "Weekly"
    windows: list = []
    pct = cfg.get("creditUsagePercent")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        windows.append({"label": f"{label} credits", "used_percent": float(pct), "reset_at": _iso_from_ts(reset)})
    products = cfg.get("productUsage") if isinstance(cfg.get("productUsage"), list) else []
    pretty = {"GrokBuild": "Grok Build", "GrokChat": "Grok Chat"}
    for item in products:
        if not isinstance(item, dict) or not isinstance(item.get("usagePercent"), (int, float)):
            continue
        if len(products) == 1 and windows and item["usagePercent"] == pct:
            continue  # same number as the overall row
        name = str(item.get("product") or "Usage")
        windows.append({"label": pretty.get(name, name), "used_percent": float(item["usagePercent"]),
                        "reset_at": _iso_from_ts(reset)})
    details: list = []
    prepaid = cents(cfg.get("prepaidBalance"))
    if prepaid and prepaid > 0:
        details.append(f"prepaid balance ${prepaid / 100:.2f}")
    cap, used = cents(cfg.get("onDemandCap")), cents(cfg.get("onDemandUsed"))
    if cap and cap > 0:
        windows.append({"label": "On demand", "used_percent": (used or 0) / cap * 100.0,
                        "reset_at": _iso_from_ts(reset), "detail": f"${(used or 0) / 100:.2f} of ${cap / 100:.2f}"})
    if not windows:
        details.append("xAI returned no usage figure for this period")
    return _snap("xai-oauth", None, windows, details)


def _fetch_copilot(auth: dict) -> dict:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("GitHub CLI (gh) not found")
    flags = 0x08000000 if os.name == "nt" else 0
    token = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                           creationflags=flags).stdout.strip()
    if not token:
        raise RuntimeError("gh is not signed in")
    data = _get_json("https://api.github.com/copilot_internal/user", {
        "Authorization": f"token {token}", "Accept": "application/json",
        "Editor-Version": "vscode/1.99.0", "User-Agent": "GitHubCopilotChat/0.26.0"})
    windows: list = []
    unlimited: list = []
    reset = data.get("quota_reset_date_utc") or data.get("quota_reset_date")
    for name, q in (data.get("quota_snapshots") or {}).items():
        if not isinstance(q, dict):
            continue
        if q.get("unlimited"):
            unlimited.append(name.replace("_", " "))
            continue
        ent, rem = _num(q.get("entitlement")), _num(q.get("remaining"))
        used = 100.0 - _num(q.get("percent_remaining")) if q.get("percent_remaining") is not None else (
            (ent - rem) / ent * 100.0 if ent else None)
        windows.append({"label": name.replace("_", " ").capitalize(), "used_percent": used,
                        "reset_at": _iso_from_ts(reset),
                        "detail": f"{int(rem)} of {int(ent)} left" if ent else None})
    details = []
    sku = str(data.get("access_type_sku") or "")
    if "educational" in sku:
        details.append("free educational plan")
    if unlimited:
        details.append("unlimited: " + ", ".join(unlimited))
    plan = str(data.get("copilot_plan") or "").title() or None
    return _snap("copilot", plan, windows, details)


def _fetch_nous(auth: dict) -> dict:
    prov = (auth.get("providers") or {}).get("nous") or {}
    token = str(prov.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("not signed in to Nous")
    base = str(prov.get("portal_base_url") or "").rstrip("/") or "https://portal.nousresearch.com"
    data = _get_json(f"{base}/api/billing/subscription",
                     {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    cur = data.get("current") or {}
    price = cur.get("dollarsPerMonthDisplay")
    if price is None:
        for tier in data.get("tiers") or []:
            if isinstance(tier, dict) and tier.get("tierId") == cur.get("tierId"):
                price = tier.get("dollarsPerMonthDisplay")
    cycle = cur.get("cycleEndsAt")
    monthly, left = cur.get("monthlyCredits"), cur.get("creditsRemaining")
    windows, details = [], []
    if price is not None:
        details.append(f"${price}/month")
    try:
        if monthly and float(monthly) > 0 and left is not None:
            windows.append({"label": "Credits", "used_percent": max(0.0, (1 - float(left) / float(monthly)) * 100.0),
                            "reset_at": cycle, "detail": f"{_money(left)} of {_money(monthly)} left"})
    except (TypeError, ValueError):
        pass
    if cycle:
        details.append(f"renews {str(cycle)[:10]}")
    if cur.get("pendingDowngradeTierName"):
        details.append(f"downgrades to {cur['pendingDowngradeTierName']} on {str(cur.get('pendingDowngradeAt') or '')[:10]}")
    elif cur.get("cancelAtPeriodEnd"):
        details.append("cancels at period end")
    return _snap("nous", cur.get("tierName"), windows, details)


_REFRESH: dict = {"t": 0.0, "v": None}


def refresh(force: bool = False) -> dict:
    """Poll every signed-in provider that has a usage API. Cached 30s unless forced."""
    if not force and _REFRESH["v"] and time.time() - _REFRESH["t"] < 30:
        return _REFRESH["v"]
    auth = _json(home() / "auth.json") or {}
    inventory = subscriptions()["providers"]
    jobs = {"anthropic": _fetch_anthropic, "xai-oauth": _fetch_xai,
            "copilot": _fetch_copilot, "nous": _fetch_nous}
    snapshots: list = []
    errors: list = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {p: pool.submit(fn, auth) for p, fn in jobs.items() if p in inventory}
        for provider, future in futures.items():
            try:
                snap = future.result(timeout=30)
                snapshots.append(snap)
                if provider != "anthropic" and not snap.get("stale"):
                    _remember(provider, snap)
            except Exception as exc:
                import traceback
                msg = (str(exc) or type(exc).__name__)[:160]
                st = _load_state()
                st.setdefault("errors", {})[provider] = {
                    "at": _now_iso(), "type": type(exc).__name__, "message": msg,
                    "trace": traceback.format_exc()[-1200:]}
                _save_state(st)
                cached = _last_good(provider, "last check failed: " + msg)
                if cached:
                    snapshots.append(cached)
                else:
                    errors.append({"provider": provider, "error": msg})
    result = {"generated_at": _now_iso(), "snapshots": snapshots, "errors": errors}
    _REFRESH["t"], _REFRESH["v"] = time.time(), result
    return result
