/**
 * AI Subscriptions: plans, live quota and token usage across this PC.
 *
 * Page (/ai-subscriptions) with three tabs (Overview, Usage, Plans) plus a
 * status-bar chip whose popover shows the tightest quota and today's tokens.
 *
 * Backend (plugins/ai-subscriptions/dashboard): GET /subscriptions, GET
 * /usage?days=N, POST /refresh. Token usage covers Hermes, Claude Code and the
 * Grok CLI. Everything is read-only and theme-variable styled.
 */
import * as sdk from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'ai-subscriptions'
const ROUTE = '/ai-subscriptions'
const REFRESH_MS = 5 * 60 * 1000
const LIVE_MS = 45 * 1000
const USAGE_MS = 2 * 60 * 1000

const host = sdk.host
const { useQuery, queryClient, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, STATUSBAR_AREAS, Popover, PopoverContent, PopoverTrigger } = sdk

let apiCtx = null

const SOURCES = [
  { id: 'hermes', label: 'Hermes', color: 'var(--ui-blue)' },
  { id: 'claude', label: 'Claude Code', color: 'var(--ui-orange)' },
  { id: 'grok', label: 'Grok CLI', color: 'var(--ui-purple)' }
]
const SRC = Object.fromEntries(SOURCES.map(s => [s.id, s]))
const ORDER = ['anthropic', 'xai-oauth', 'nous', 'copilot']
const NAMES = {
  nous: 'Nous Portal',
  anthropic: 'Claude',
  'xai-oauth': 'Grok (xAI)',
  copilot: 'GitHub Copilot',
  'openai-codex': 'OpenAI Codex'
}

// h(type, props, ...children): tiny wrapper so the file reads like markup.
function h(type, props, ...kids) {
  if (kids.length === 0) return jsx(type, props || {})
  if (kids.length === 1) return jsx(type, { ...(props || {}), children: kids[0] })
  return jsxs(type, { ...(props || {}), children: kids })
}

// ── Formatters ──────────────────────────────────────────────────────────────

const trim1 = x => (x.toFixed(1).endsWith('.0') ? x.toFixed(0) : x.toFixed(1))

function fmtNum(n) {
  n = Number(n)
  if (!Number.isFinite(n)) return '-'
  if (n >= 1e9) return trim1(n / 1e9) + 'B'
  if (n >= 1e6) return trim1(n / 1e6) + 'M'
  if (n >= 1e3) return trim1(n / 1e3) + 'K'
  return String(Math.round(n))
}

function fmtMoney(n) {
  n = Number(n)
  if (!Number.isFinite(n)) return '-'
  if (n === 0) return '$0'
  if (n < 0.01) return '<$0.01'
  return '$' + (n >= 100 ? n.toFixed(0) : n.toFixed(2))
}

function fmtReset(iso) {
  const t = Date.parse(iso || '')
  if (Number.isNaN(t)) return null
  const m = Math.round((t - Date.now()) / 60000)
  if (m < 0) return 'reset ' + new Date(t).toLocaleDateString()
  if (m < 1) return 'resets now'
  if (m < 60) return 'resets in ' + m + 'm'
  const hrs = Math.round(m / 60)
  if (hrs < 48) return 'resets in ' + hrs + 'h'
  return 'resets in ' + Math.round(hrs / 24) + 'd'
}

function fmtClock(value) {
  const t = value instanceof Date ? value.getTime() : Date.parse(value || '')
  return Number.isNaN(t) ? null : new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function age(stamp, stale) {
  const t = Date.parse(stamp || '')
  if (Number.isNaN(t)) return { text: 'age unknown', live: false }
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000))
  if (stale) return { text: 'last good reading, ' + (mins < 90 ? mins + 'm' : mins < 2160 ? Math.round(mins / 60) + 'h' : Math.round(mins / 1440) + 'd') + ' ago', live: false }
  if (mins < 2) return { text: 'live, just now', live: true }
  if (mins < 90) return { text: 'live, ' + mins + 'm ago', live: true }
  if (mins < 2160) return { text: 'cached, ' + Math.round(mins / 60) + 'h old', live: false }
  return { text: 'cached, ' + Math.round(mins / 1440) + 'd old', live: false }
}

const fmtTps = v => (v == null || !isFinite(v) ? null : (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + ' t/s')
const tone = used => (used == null ? 'var(--ui-stroke-secondary)' : used >= 85 ? 'var(--ui-red)' : used >= 60 ? 'var(--ui-yellow)' : 'var(--ui-green)')

// ── Data merge ──────────────────────────────────────────────────────────────

function mergePlans(subs, live) {
  const map = new Map()
  const slot = key => {
    if (!map.has(key)) {
      map.set(key, { key, name: NAMES[key] || key, account: null, plan: null, windows: [], details: [], sources: [], pool: [], live: false, stamp: null })
    }
    return map.get(key)
  }
  for (const [key, p] of Object.entries((subs && subs.providers) || {})) {
    const e = slot(key)
    e.name = p.name || e.name
    e.account = p.account
    e.sources = p.sources || []
    e.pool = p.pool || []
    if (p.quota) {
      e.plan = p.quota.plan
      e.windows = (p.quota.windows || []).map(w => ({ ...w }))
      e.details = [...(p.quota.details || [])]
      e.stamp = subs.quota_cached_at
    }
  }
  for (const [key, snap] of Object.entries((subs && subs.last_good) || {})) {
    const e = slot(key)
    if (e.windows.length || !snap) continue
    e.plan = snap.plan || e.plan
    e.windows = (snap.windows || []).map(w => ({ ...w }))
    e.details = [...(snap.details || [])]
    e.stamp = snap.fetched_at
    e.stale = true
  }
  const failed = new Set(((live && live.errors) || []).map(x => x.provider))
  for (const snap of (live && live.snapshots) || []) {
    const e = slot(snap.provider)
    e.live = true
    e.stamp = snap.fetched_at
    e.plan = snap.plan || e.plan
    e.windows = (snap.windows || []).map(w => ({ ...w }))
    e.details = [...(snap.details || [])]
    e.stale = !!snap.stale
  }
  const list = [...map.values()]
  for (const e of list) e.failed = failed.has(e.key) && !e.live
  list.sort((a, b) => {
    const ia = ORDER.indexOf(a.key)
    const ib = ORDER.indexOf(b.key)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.name.localeCompare(b.name)
  })
  return list
}

function tightest(plans) {
  let best = null
  for (const p of plans) {
    for (const w of p.windows) {
      if (w.used_percent == null) continue
      if (!best || w.used_percent > best.used) best = { used: w.used_percent, provider: p.name, label: w.label, reset: w.reset_at, live: p.live }
    }
  }
  return best
}

// ── Styles ──────────────────────────────────────────────────────────────────

const line = '1px solid var(--ui-stroke-secondary)'
const S = {
  page: { padding: 20, display: 'flex', flexDirection: 'column', gap: 14, height: '100%', overflowY: 'auto', color: 'var(--ui-text-primary)' },
  muted: { fontSize: 12, color: 'var(--ui-text-secondary)' },
  faint: { fontSize: 11, color: 'var(--ui-text-quaternary)' },
  card: { border: line, borderRadius: 8, padding: 12, display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(290px, 1fr))', gap: 12 },
  tiles: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10 },
  title: { fontSize: 12, fontWeight: 600, letterSpacing: 0.5, textTransform: 'uppercase', color: 'var(--ui-text-secondary)' }
}

function Btn({ children, onClick, active, disabled, title, style }) {
  return h('button', {
    type: 'button', onClick, disabled, title,
    style: {
      fontSize: 12, padding: '4px 11px', borderRadius: 6, cursor: disabled ? 'default' : 'pointer',
      border: line, color: active ? 'var(--ui-text-primary)' : 'var(--ui-text-secondary)',
      background: active ? 'var(--ui-stroke-secondary)' : 'transparent', opacity: disabled ? 0.6 : 1, ...(style || {})
    }
  }, children)
}

function Bar({ used }) {
  const p = used == null ? 0 : Math.max(0, Math.min(100, used))
  return h('div', { style: { flex: 1, height: 6, borderRadius: 3, background: 'var(--ui-stroke-secondary)', overflow: 'hidden', minWidth: 40 } },
    h('div', { style: { width: p + '%', height: '100%', background: tone(used), borderRadius: 3, transition: 'width .3s' } }))
}

function Tile({ label, value, sub }) {
  return h('div', { style: { ...S.card, padding: '10px 12px', gap: 2 } },
    h('div', { style: S.faint }, label),
    h('div', { style: { fontSize: 18, fontWeight: 600 } }, value),
    sub ? h('div', { style: { fontSize: 11, color: 'var(--ui-text-secondary)' } }, sub) : null)
}

function WindowRow({ w }) {
  return h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
    h('span', { style: { width: 112, fontSize: 12, color: 'var(--ui-text-secondary)', flexShrink: 0 } }, w.label || 'Window'),
    h(Bar, { used: w.used_percent }),
    h('span', { style: { width: 40, textAlign: 'right', fontSize: 12, flexShrink: 0 } }, w.used_percent == null ? '-' : Math.round(w.used_percent) + '%'),
    h('span', { style: { width: 92, textAlign: 'right', fontSize: 11, color: 'var(--ui-text-quaternary)', flexShrink: 0 } }, fmtReset(w.reset_at) || (w.detail || '')))
}

// ── Plans ───────────────────────────────────────────────────────────────────

function PlanCard({ plan, compact }) {
  const a = age(plan.stamp, plan.stale)
  const hasNumbers = plan.windows.length > 0
  return h('div', { style: S.card },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
      h('span', { style: { width: 8, height: 8, borderRadius: '50%', background: plan.live && !plan.stale ? 'var(--ui-green)' : 'var(--ui-stroke-secondary)', flexShrink: 0 } }),
      h('span', { style: { fontWeight: 600, fontSize: 13, flex: 1 } }, plan.name),
      plan.plan ? h('span', { style: { ...S.faint, border: line, borderRadius: 4, padding: '1px 6px' } }, plan.plan) : null),
    plan.account && !compact ? h('div', { style: S.faint }, plan.account) : null,
    hasNumbers ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } }, plan.windows.map((w, i) => h(WindowRow, { key: i, w }))) : null,
    plan.windows.filter(w => w.detail).length && !compact
      ? h('div', { style: S.faint }, plan.windows.filter(w => w.detail).map(w => w.label + ': ' + w.detail).join('  ·  '))
      : null,
    plan.details.length ? h('div', { style: { ...S.muted, display: 'flex', flexDirection: 'column', gap: 2 } }, plan.details.slice(0, compact ? 2 : 6).map((d, i) => h('div', { key: i }, d))) : null,
    !hasNumbers && !plan.details.length ? h('div', { style: S.faint }, plan.failed ? 'live check failed, no earlier reading' : 'waiting for the first live reading') : null,
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 6 } },
      h('span', { style: { ...S.faint, color: a.live ? 'var(--ui-text-secondary)' : 'var(--ui-text-quaternary)' } }, hasNumbers || plan.live ? a.text : ''),
      !compact && plan.sources.length ? h('span', { style: S.faint }, plan.sources.join(' · ')) : null))
}

// ── Usage ───────────────────────────────────────────────────────────────────

function useUsageView(data, enabled, sort) {
  return useMemo(() => {
    if (!data) return null
    const on = id => enabled.has(id)
    const days = (data.by_day || []).map(d => {
      let tokens = 0, cost = 0, requests = 0
      const parts = []
      for (const s of SOURCES) {
        const v = d.s[s.id]
        if (!v || !on(s.id)) continue
        tokens += v.tokens; cost += v.cost; requests += v.requests
        parts.push({ id: s.id, tokens: v.tokens })
      }
      return { date: d.date, tokens, cost, requests, parts }
    })
    const total = days.reduce((a, d) => ({ tokens: a.tokens + d.tokens, cost: a.cost + d.cost, requests: a.requests + d.requests }), { tokens: 0, cost: 0, requests: 0 })
    const models = (data.by_model || []).filter(m => on(m.src))
      .map(m => ({ ...m, tps: m.speed && m.speed.tps != null ? m.speed.tps : null }))
    const dir = sort.dir === 'asc' ? 1 : -1
    models.sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key]
      if (typeof av === 'string' || typeof bv === 'string') return String(av).localeCompare(String(bv)) * dir
      // untimed rows sort last in both directions
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      return (av - bv) * dir
    })
    return { days, total, models, speed: data.speed || null }
  }, [data, enabled, sort])
}

function StackChart({ days, selected, onPick }) {
  const max = Math.max(1, ...days.map(d => d.tokens))
  return h('div', { style: { display: 'flex', alignItems: 'flex-end', gap: 2, height: 110 } }, days.map(d =>
    h('div', {
      key: d.date, role: 'button', onClick: () => onPick(selected === d.date ? null : d.date),
      title: d.date + ': ' + fmtNum(d.tokens) + ' tokens, ' + fmtMoney(d.cost) + ' est.',
      style: { flex: 1, minWidth: 3, height: '100%', display: 'flex', flexDirection: 'column-reverse', cursor: 'pointer', outline: selected === d.date ? '1px solid var(--ui-text-secondary)' : 'none', borderRadius: 2 }
    },
      d.tokens === 0
        ? h('div', { style: { height: 3, background: 'var(--ui-stroke-secondary)', borderRadius: 2, opacity: 0.6 } })
        : d.parts.filter(p => p.tokens > 0).map(p => h('div', { key: p.id, style: { height: (p.tokens / max * 100).toFixed(2) + '%', minHeight: 2, background: SRC[p.id].color, opacity: selected && selected !== d.date ? 0.45 : 1 } })))))
}

function MiniStat({ label, value, sub }) {
  return h('div', { style: { minWidth: 92 } },
    h('div', { style: { fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--ui-text-quaternary)' } }, label),
    h('div', { style: { fontSize: 14, fontWeight: 600, fontVariantNumeric: 'tabular-nums' } }, value),
    sub ? h('div', { style: S.faint }, sub) : null)
}

function ShareBar({ value, max, color }) {
  const pct = max ? Math.max(1, value / max * 100) : 0
  return h('div', { style: { height: 3, background: 'var(--ui-stroke-secondary)', borderRadius: 2, overflow: 'hidden' } },
    h('div', { style: { width: pct.toFixed(1) + '%', height: '100%', background: color, borderRadius: 2 } }))
}

function HourStrip({ hours, peak }) {
  const max = Math.max(1, ...hours)
  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
    h('div', { style: { display: 'flex', alignItems: 'flex-end', gap: 2, height: 34 } },
      ...hours.map((v, i) => h('div', {
        key: i,
        title: String(i).padStart(2, '0') + ':00 to ' + String(i + 1).padStart(2, '0') + ':00, ' + fmtNum(v) + ' tokens',
        style: {
          flex: 1, height: Math.max(2, (v / max * 100)).toFixed(1) + '%', borderRadius: 1,
          background: v ? (i === peak ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)') : 'var(--ui-stroke-secondary)'
        } }))),
    h('div', { style: { ...S.faint, display: 'flex', justifyContent: 'space-between' } },
      h('span', null, '00'), h('span', null, '06'), h('span', null, '12'), h('span', null, '18'), h('span', null, '23')))
}

function DayDetail({ date, detail, enabled, rangeTokens, onClear }) {
  const weekday = new Date(date + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'long' })
  const sources = ((detail && detail.sources) || []).filter(x => enabled.has(x.id))
  const models = ((detail && detail.models) || []).filter(m => enabled.has(m.src))
  const total = sources.reduce((a, x) => a + x.tokens, 0)
  const calls = sources.reduce((a, x) => a + x.requests, 0)
  const cost = sources.reduce((a, x) => a + x.cost, 0)
  const maxSrc = Math.max(1, ...sources.map(x => x.tokens))
  const maxModel = Math.max(1, ...models.slice(0, 12).map(m => m.tokens))
  const peak = detail && detail.peak_hour != null ? detail.peak_hour : null
  return h('div', { style: { marginTop: 12, paddingTop: 10, borderTop: line, display: 'flex', flexDirection: 'column', gap: 12 } },
    h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' } },
      h('strong', { style: { fontSize: 13 } }, weekday + ', ' + date),
      total ? null : h('span', { style: S.faint }, 'nothing logged'),
      h('span', { style: { flex: 1 } }),
      onClear ? h('button', { type: 'button', onClick: onClear, title: 'Back to the latest day',
        style: { background: 'transparent', border: 'none', color: 'var(--ui-text-quaternary)', cursor: 'pointer', fontSize: 11 } }, 'close') : null),
    h('div', { style: { display: 'flex', gap: 18, flexWrap: 'wrap' } },
      h(MiniStat, { label: 'Tokens', value: fmtNum(total), sub: rangeTokens ? Math.round(total / rangeTokens * 100) + '% of range' : null }),
      h(MiniStat, { label: 'Calls', value: fmtNum(calls) }),
      h(MiniStat, { label: 'Est. value', value: fmtMoney(cost), sub: 'list prices' }),
      h(MiniStat, { label: 'Avg speed', value: fmtTps(detail && detail.speed && detail.speed.tps) || 'n/a',
        sub: detail && detail.speed && detail.speed.calls ? fmtNum(detail.speed.calls) + ' timed calls' : 'untimed' }),
      h(MiniStat, { label: 'Busiest hour', value: peak != null ? String(peak).padStart(2, '0') + ':00' : 'none',
        sub: peak != null ? fmtNum((detail.hours || [])[peak]) + ' tokens' : null })),
    h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 16 } },
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: 7 } },
        h('div', { style: S.faint }, 'By source'),
        ...sources.map(x => h('div', { key: x.id, style: { display: 'flex', flexDirection: 'column', gap: 3 } },
          h('div', { style: { display: 'flex', gap: 6, fontSize: 12 } },
            h('span', { style: { width: 7, height: 7, borderRadius: 2, background: SRC[x.id].color, marginTop: 4 } }),
            h('span', { style: { flex: 1 } }, x.label),
            h('span', { style: { color: 'var(--ui-text-secondary)', fontVariantNumeric: 'tabular-nums' } }, fmtNum(x.tokens)),
            h('span', { style: { width: 58, textAlign: 'right', ...S.faint } }, x.requests + ' calls')),
          h(ShareBar, { value: x.tokens, max: maxSrc, color: SRC[x.id].color }),
          h('div', { style: S.faint }, 'in ' + fmtNum(x.input) + ' | out ' + fmtNum(x.output) +
            ' | cache read ' + fmtNum(x.cache_read) + ' | cache write ' + fmtNum(x.cache_write)))),
        sources.length ? null : h('div', { style: S.faint }, 'No tracked source on this day.')),
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 } },
        h('div', { style: S.faint }, 'By model'),
        models.length === 0 ? h('div', { style: S.faint }, 'No models on this day.') : models.slice(0, 12).map((m, i) =>
          h('div', { key: m.src + m.model + i, style: { display: 'flex', flexDirection: 'column', gap: 2 } },
            h('div', { style: { display: 'flex', gap: 6, fontSize: 12 } },
              h('span', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: m.model }, m.model),
              h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, fmtNum(m.tokens)),
              h('span', { title: m.speed && m.speed.calls ? 'measured over ' + m.speed.calls + ' calls' : 'no timing logged',
                style: { width: 52, textAlign: 'right', color: m.speed && m.speed.tps != null ? 'var(--ui-text-secondary)' : 'var(--ui-text-quaternary)', fontVariantNumeric: 'tabular-nums' } }, fmtTps(m.speed && m.speed.tps) || '-'),
              h('span', { style: { width: 54, textAlign: 'right', ...S.faint } }, m.requests + ' calls')),
            h(ShareBar, { value: m.tokens, max: maxModel, color: SRC[m.src].color }))))),
    detail && detail.hours ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
      h('div', { style: S.faint }, 'Through the day'),
      h(HourStrip, { hours: detail.hours, peak })) : null)
}

function SortHead({ label, k, sort, setSort, width, align }) {
  const on = sort.key === k
  return h('div', {
    role: 'button', onClick: () => setSort(on ? { key: k, dir: sort.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: 'desc' }),
    style: { width, flex: width ? undefined : 2, textAlign: align || 'left', fontSize: 11, fontWeight: 600, cursor: 'pointer', color: on ? 'var(--ui-text-primary)' : 'var(--ui-text-quaternary)', userSelect: 'none' }
  }, label + (on ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''))
}

function UsageTab({ data, range, setRange, enabled, setEnabled, loading }) {
  const [sort, setSort] = useState({ key: 'tokens', dir: 'desc' })
  const [pick, setPick] = useState(null)
  const view = useUsageView(data, enabled, sort)
  const detail = (data && data.day_detail) || {}
  const latest = view ? [...view.days].reverse().find(d => d.tokens > 0) : null
  const shown = pick || (latest && latest.date)
  if (!view) return h('div', { style: S.muted }, loading ? 'Reading usage from this PC...' : 'No usage data yet.')
  const toggle = id => {
    const next = new Set(enabled)
    if (next.has(id)) { if (next.size > 1) next.delete(id) } else next.add(id)
    setEnabled(next)
  }

  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14 } },
    h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' } },
      ...[7, 30, 90].map(n => h(Btn, { key: n, active: range === n, onClick: () => setRange(n) }, n + ' days')),
      h('span', { style: { width: 10 } }),
      ...SOURCES.map(s => h(Btn, { key: s.id, active: enabled.has(s.id), onClick: () => toggle(s.id), title: 'Include or exclude ' + s.label },
        h('span', { style: { display: 'inline-block', width: 8, height: 8, borderRadius: 2, background: s.color, marginRight: 6, opacity: enabled.has(s.id) ? 1 : 0.3 } }), s.label))),
    h('div', { style: S.tiles },
      h(Tile, { label: 'Tokens, last ' + range + 'd', value: fmtNum(view.total.tokens), sub: 'input, output and cache' }),
      h(Tile, { label: 'API calls', value: fmtNum(view.total.requests), sub: 'in this range' }),
      h(Tile, { label: 'Est. value', value: fmtMoney(view.total.cost), sub: 'at list API prices' }),
      h(Tile, { label: 'All time', value: fmtNum(data.periods.all.tokens), sub: fmtMoney(data.periods.all.cost) + ' est.' }),
      h(Tile, { label: 'Avg speed', value: fmtTps(view.speed && view.speed.tps) || 'n/a',
        sub: view.speed && view.speed.calls ? 'over ' + fmtNum(view.speed.calls) + ' timed calls' : 'no timed calls yet' })),
    h('div', { style: S.card },
      h('div', { style: S.faint }, "Tokens per day (click a bar for that day's breakdown)"),
      h(StackChart, { days: view.days, selected: shown, onPick: d => setPick(d === shown && pick ? null : d) }),
      shown ? h(DayDetail, {
        date: shown, detail: detail[shown], enabled, rangeTokens: view.total.tokens,
        onClear: pick ? () => setPick(null) : null
      }) : null),
    h('div', { style: S.card },
      h('div', { style: S.faint }, 'By model (click a header to sort)'),
      h('div', { style: { display: 'flex', gap: 8, padding: '2px 0', borderBottom: line } },
        h(SortHead, { label: 'Model', k: 'model', sort, setSort }),
        h('div', { style: { width: 86, fontSize: 11, fontWeight: 600, color: 'var(--ui-text-quaternary)' } }, 'Source'),
        h(SortHead, { label: 'Calls', k: 'requests', sort, setSort, width: 58, align: 'right' }),
        h(SortHead, { label: 'Tokens', k: 'tokens', sort, setSort, width: 70, align: 'right' }),
        h(SortHead, { label: 't/s', k: 'tps', sort, setSort, width: 56, align: 'right' }),
        h(SortHead, { label: 'Est. $', k: 'cost', sort, setSort, width: 64, align: 'right' })),
      view.models.length === 0 ? h('div', { style: S.faint }, 'Nothing in this range.') : view.models.slice(0, 25).map((m, i) =>
        h('div', { key: m.src + m.model + i, style: { display: 'flex', gap: 8, alignItems: 'center', padding: '2px 0', fontSize: 12 } },
          h('div', { style: { flex: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: m.model }, m.model),
          h('div', { style: { width: 86, fontSize: 11, color: 'var(--ui-text-secondary)' } }, h('span', { style: { display: 'inline-block', width: 7, height: 7, borderRadius: 2, background: SRC[m.src].color, marginRight: 5 } }), SRC[m.src].label),
          h('div', { style: { width: 58, textAlign: 'right' } }, fmtNum(m.requests)),
          h('div', { style: { width: 70, textAlign: 'right' } }, fmtNum(m.tokens)),
          h('div', { title: m.speed && m.speed.calls ? 'measured over ' + m.speed.calls + ' calls (' + fmtNum(m.speed.tokens) + ' output tokens in ' + fmtNum(m.speed.seconds) + 's)' : 'no timing logged for this model',
            style: { width: 56, textAlign: 'right', color: m.tps == null ? 'var(--ui-text-quaternary)' : 'var(--ui-text-secondary)', fontVariantNumeric: 'tabular-nums' } }, fmtTps(m.tps) || '-'),
          h('div', { style: { width: 64, textAlign: 'right', color: 'var(--ui-text-secondary)' } }, fmtMoney(m.cost))))),
    h('div', { style: S.card },
      h('div', { style: S.faint }, 'Token mix by source, last ' + range + ' days'),
      ...(data.by_source || []).filter(s => enabled.has(s.id)).map(s => {
        const mix = [['input', s.input], ['output', s.output], ['cache read', s.cache_read], ['cache write', s.cache_write]]
        return h('div', { key: s.id, style: { display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, alignItems: 'center' } },
          h('strong', { style: { width: 96 } }, s.label),
          ...mix.map(([k, v]) => h('span', { key: k, style: { color: 'var(--ui-text-secondary)' } }, k + ' ' + fmtNum(v))),
          s.reasoning ? h('span', { style: S.faint }, '(' + fmtNum(s.reasoning) + ' reasoning, included in output)') : null)
      })),
    h('div', { style: S.faint },
      'Speed is output tokens over the model time each tool records: Grok CLI logs every call, Claude Code logs a turn summary, and Hermes calls are timed by this plugin\'s own hook, which starts recording from the first call after a restart. Counts come from local logs: ' + SOURCES.map(s => {
        const c = data.coverage[s.id]
        return s.label + ' ' + (c && c.first ? c.first.slice(0, 10) + ' to ' + c.last.slice(0, 10) : 'none')
      }).join('; ') + '. Not tracked: ' + (data.not_tracked || []).map(n => n.name + ' (' + n.why + ')').join('; ') + '. Claude and Grok dollar figures are list-price estimates; subscriptions are not billed per token.'))
}

// ── Overview ────────────────────────────────────────────────────────────────

function OverviewTab({ plans, data, setTab }) {
  const p = (data && data.periods) || null
  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14 } },
    p ? h('div', { style: S.tiles },
      h(Tile, { label: 'Today', value: fmtNum(p.today.tokens), sub: p.today.requests + ' calls, ' + fmtMoney(p.today.cost) + ' est.' }),
      h(Tile, { label: 'Last 7 days', value: fmtNum(p.d7.tokens), sub: fmtMoney(p.d7.cost) + ' est.' }),
      h(Tile, { label: 'Last 30 days', value: fmtNum(p.d30.tokens), sub: fmtMoney(p.d30.cost) + ' est.' }),
      h(Tile, { label: 'All time', value: fmtNum(p.all.tokens), sub: fmtMoney(p.all.cost) + ' est.' })) : null,
    h('div', { style: S.title }, 'Quota right now'),
    plans.length ? h('div', { style: S.grid }, plans.map(pl => h(PlanCard, { key: pl.key, plan: pl, compact: true })))
      : h('div', { style: S.muted }, 'No signed-in subscriptions found.'),
    data ? h('div', { style: S.card },
      h('div', { style: S.faint }, 'Share of tokens, last 30 days'),
      ...(data.by_source || []).map(s => {
        const total = data.by_source.reduce((a, x) => a + x.tokens, 0) || 1
        return h('div', { key: s.id, style: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 } },
          h('span', { style: { width: 96 } }, s.label),
          h('div', { style: { flex: 1, height: 6, borderRadius: 3, background: 'var(--ui-stroke-secondary)', overflow: 'hidden' } },
            h('div', { style: { width: (s.tokens / total * 100).toFixed(1) + '%', height: '100%', background: SRC[s.id].color } })),
          h('span', { style: { width: 130, textAlign: 'right', color: 'var(--ui-text-secondary)' } }, fmtNum(s.tokens) + ' · ' + fmtMoney(s.cost)))
      }),
      h(Btn, { onClick: () => setTab('usage'), style: { alignSelf: 'flex-start' } }, 'Open usage details')) : null)
}

// ── Page ────────────────────────────────────────────────────────────────────

function useSubData(range) {
  const subsQ = useQuery({ queryKey: [PLUGIN_ID, 'subs'], queryFn: () => apiCtx.rest('/subscriptions'), refetchInterval: REFRESH_MS, retry: 1 })
  const liveQ = useQuery({ queryKey: [PLUGIN_ID, 'live'], queryFn: () => apiCtx.rest('/refresh', { method: 'POST', timeoutMs: 45000 }), refetchInterval: LIVE_MS, retry: 1 })
  const usageQ = useQuery({ queryKey: [PLUGIN_ID, 'usage', range], queryFn: () => apiCtx.rest('/usage?days=' + range), refetchInterval: USAGE_MS, retry: 1 })
  const lastUsage = useRef(null)
  if (usageQ.data) lastUsage.current = usageQ.data
  const plans = useMemo(() => mergePlans(subsQ.data, liveQ.data), [subsQ.data, liveQ.data])
  return { subsQ, liveQ, usageQ, usage: usageQ.data || lastUsage.current, plans }
}

function Page() {
  const stored = (k, d) => { try { const v = apiCtx && apiCtx.storage ? apiCtx.storage.get(k, d) : d; return v == null ? d : v } catch (e) { return d } }
  const [tab, setTabState] = useState(() => stored('tab', 'overview'))
  const [range, setRangeState] = useState(() => stored('range', 30))
  const [enabled, setEnabled] = useState(() => new Set(SOURCES.map(s => s.id)))
  const [busy, setBusy] = useState(false)
  const [checked, setChecked] = useState(null)
  const setTab = t => { setTabState(t); try { apiCtx.storage.set('tab', t) } catch (e) {} }
  const setRange = n => { setRangeState(n); try { apiCtx.storage.set('range', n) } catch (e) {} }
  const { subsQ, liveQ, usageQ, usage: rawUsage, plans } = useSubData(range)
  // An older backend (loaded before core.py existed) returns a different shape.
  const staleBackend = !!rawUsage && !rawUsage.periods
  const usage = staleBackend ? null : rawUsage

  const refresh = useCallback(async () => {
    setBusy(true)
    try {
      const live = await apiCtx.rest('/refresh?force=true', { method: 'POST', timeoutMs: 45000 })
      queryClient.setQueryData([PLUGIN_ID, 'live'], live)
      await Promise.allSettled([subsQ.refetch(), usageQ.refetch()])
    } catch (e) {
      /* the banner below reports a failed poll */
    }
    setChecked(new Date())
    setBusy(false)
  }, [subsQ, usageQ])

  const live = plans.filter(p => p.live).length
  const failed = liveQ.data && liveQ.data.errors && liveQ.data.errors.length ? liveQ.data.errors : []
  const tabs = [['overview', 'Overview'], ['usage', 'Usage'], ['plans', 'Plans']]
  return h('div', { style: S.page },
    h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' } },
      h('h1', { style: { fontSize: 18, fontWeight: 600, margin: 0 } }, 'AI Subscriptions'),
      h('span', { style: S.muted }, 'plans, live quota and token usage across this PC')),
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', borderBottom: line, paddingBottom: 8 } },
      ...tabs.map(([id, label]) => h('button', {
        key: id, type: 'button', role: 'tab', 'aria-selected': tab === id, onClick: () => setTab(id),
        style: { fontSize: 13, padding: '4px 12px', cursor: 'pointer', background: 'transparent', border: 'none', borderBottom: tab === id ? '2px solid var(--ui-accent)' : '2px solid transparent', color: tab === id ? 'var(--ui-text-primary)' : 'var(--ui-text-secondary)', fontWeight: tab === id ? 600 : 400 }
      }, label)),
      h('span', { style: { flex: 1 } }),
      h('span', { style: S.faint }, checked ? 'checked ' + fmtClock(checked) + ', ' + live + ' live' : (live ? live + ' live' : 'checking...')),
      h(Btn, { onClick: refresh, disabled: busy }, busy ? 'Refreshing...' : 'Refresh')),
    staleBackend ? h('div', { style: { ...S.muted, ...S.card } }, 'The Hermes backend is still running the previous plugin code, so usage across Claude Code and Grok is not available yet. Quit and reopen Hermes once; after that, backend changes load without a restart.') : null,
    liveQ.isError ? h('div', { style: { ...S.muted, ...S.card } }, 'Live poll failed: ' + String((liveQ.error && liveQ.error.message) || 'unknown') + '. Showing cached values.') : null,
    failed.length ? h('div', { style: S.faint }, 'Not live: ' + failed.map(f => (NAMES[f.provider] || f.provider) + ' (' + f.error + ')').join('; ')) : null,
    tab === 'overview' ? h(OverviewTab, { plans, data: usage, setTab }) : null,
    tab === 'usage' ? h(UsageTab, { data: usage, range, setRange, enabled, setEnabled, loading: usageQ.isLoading }) : null,
    tab === 'plans' ? h('div', { style: S.grid }, plans.map(p => h(PlanCard, { key: p.key, plan: p, compact: false }))) : null)
}

// ── Status bar chip ─────────────────────────────────────────────────────────

const SHORT = { anthropic: 'Claude', 'xai-oauth': 'Grok', nous: 'Nous', copilot: 'Copilot', 'openai-codex': 'Codex' }

function worstOf(plan) {
  let w = null
  for (const x of plan.windows) if (x.used_percent != null && (!w || x.used_percent > w.used_percent)) w = x
  return w
}

function useChipPrefs() {
  const read = (k, d) => { try { const v = apiCtx && apiCtx.storage ? apiCtx.storage.get(k, d) : d; return v == null ? d : v } catch (e) { return d } }
  const [subs, setSubsState] = useState(() => { const v = read('chip_subs', null); return Array.isArray(v) && v.length ? v : null })
  const [tokens, setTokensState] = useState(() => read('chip_tokens', true) !== false)
  const save = (k, v) => { try { apiCtx.storage.set(k, v) } catch (e) {} }
  return {
    subs, tokens,
    setSubs: v => { const next = v && v.length ? v : null; setSubsState(next); save('chip_subs', next) },
    setTokens: v => { setTokensState(v); save('chip_tokens', v) }
  }
}

function Check({ checked, onChange, label, sub, disabled }) {
  return h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1, fontSize: 12 } },
    h('input', { type: 'checkbox', checked, disabled, onChange: e => onChange(e.target.checked), style: { accentColor: 'var(--ui-accent)', margin: 0 } }),
    h('span', { style: { flex: 1, fontWeight: 600 } }, label),
    sub ? h('span', { style: S.faint }, sub) : null)
}

function Chip() {
  const [open, setOpen] = useState(false)
  const { plans, usage } = useSubData(7)
  const prefs = useChipPrefs()
  const today = usage && usage.periods ? usage.periods.today : null
  const withNumbers = plans.filter(p => worstOf(p))
  const chosen = prefs.subs ? withNumbers.filter(p => prefs.subs.includes(p.key)) : []
  const shown = chosen.length ? chosen : null
  const worst = tightest(plans)
  const parts = shown ? shown.map(p => ({ name: SHORT[p.key] || p.name, used: worstOf(p).used_percent })) : (worst ? [{ name: null, used: worst.used }] : [])
  const topUsed = parts.length ? Math.max(...parts.map(x => x.used)) : null
  const label = (parts.length ? parts.map(x => (x.name ? x.name + ' ' : '') + Math.round(x.used) + '%').join(' · ') : 'usage') + (prefs.tokens && today ? ' · ' + fmtNum(today.tokens) : '')
  const trigger = h('button', {
    type: 'button', 'aria-label': 'AI subscription usage',
    title: shown ? shown.map(p => p.name + ' ' + Math.round(worstOf(p).used_percent) + '% (' + worstOf(p).label + ')').join('; ') : (worst ? worst.provider + ' ' + worst.label + ' ' + Math.round(worst.used) + '% used (tightest)' : 'AI subscription usage'),
    style: { display: 'inline-flex', alignItems: 'center', gap: 5, padding: '0 6px', height: '100%', background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '0.6875rem', color: 'var(--ui-text-tertiary)', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }
  }, h('span', { style: { width: 7, height: 7, borderRadius: '50%', background: tone(topUsed) } }), label)
  if (!Popover || !PopoverTrigger || !PopoverContent) return trigger

  const toggle = (key, on) => {
    const cur = prefs.subs ? prefs.subs.filter(k => withNumbers.some(p => p.key === k)) : []
    prefs.setSubs(on ? [...new Set([...cur, key])] : cur.filter(k => k !== key))
  }
  return h(Popover, { open, onOpenChange: setOpen },
    h(PopoverTrigger, { asChild: true }, trigger),
    h(PopoverContent, { side: 'top', align: 'end', sideOffset: 8, style: { width: 'min(380px, calc(100vw - 24px))', padding: 14, color: 'var(--ui-text-primary)' } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 } },
        h('strong', { style: { flex: 1, fontSize: 14 } }, 'AI usage'),
        h(Btn, { onClick: () => { setOpen(false); host.navigate(ROUTE) } }, 'Open page')),
      h('div', { style: { ...S.faint, marginBottom: 6 } }, 'Tick the subscriptions to keep in the status bar'),
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 0 8px', borderBottom: line } },
        h(Check, { checked: !shown, onChange: on => { if (on) prefs.setSubs(null) }, label: 'Auto', sub: 'tightest quota' }),
        ...plans.map(p => {
          const w = worstOf(p)
          return h(Check, {
            key: p.key, checked: !!shown && shown.some(x => x.key === p.key), disabled: !w,
            onChange: on => toggle(p.key, on), label: p.name,
            sub: w ? Math.round(w.used_percent) + '% ' + String(w.label).toLowerCase() : 'no quota figure'
          })
        }),
        h(Check, { checked: prefs.tokens, onChange: prefs.setTokens, label: 'Tokens today', sub: today ? fmtNum(today.tokens) : '' })),
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: 9, maxHeight: 'min(360px, 50vh)', overflowY: 'auto', paddingTop: 10 } },
        ...withNumbers.map(p => h('div', { key: p.key, style: { display: 'flex', flexDirection: 'column', gap: 4 } },
          h('div', { style: { fontSize: 12, fontWeight: 600 } }, p.name + (p.plan ? ' · ' + p.plan : '')),
          ...p.windows.filter(w => w.used_percent != null).map((w, i) => h(WindowRow, { key: i, w })))),
        withNumbers.length === 0 ? h('div', { style: S.muted }, 'No quota numbers yet.') : null,
        today ? h('div', { style: { borderTop: line, paddingTop: 8, display: 'flex', flexDirection: 'column', gap: 3 } },
          h('div', { style: { fontSize: 12, fontWeight: 600 } }, 'Tokens'),
          h('div', { style: S.muted }, 'Today ' + fmtNum(today.tokens) + ' · 7 days ' + fmtNum(usage.periods.d7.tokens) + ' · ' + fmtMoney(usage.periods.d7.cost) + ' est.'),
          ...(usage.by_source || []).map(x => h('div', { key: x.id, style: S.faint }, x.label + ' ' + fmtNum(x.tokens) + ' in 7 days'))) : null)))
}

// ── Registration ────────────────────────────────────────────────────────────

export default {
  id: PLUGIN_ID,
  name: 'AI Subscriptions',
  description: 'Plans, live quota and token usage across this PC.',
  defaultEnabled: true,
  register(ctx) {
    apiCtx = ctx
    const items = [
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 70, data: { path: ROUTE, label: 'AI Subscriptions', codicon: 'credit-card' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'ai-subscriptions.open', label: 'AI Subscriptions: Open', keywords: ['subscription', 'usage', 'quota', 'tokens', 'claude', 'grok'], run: () => host.navigate(ROUTE) } }
    ]
    const bar = (STATUSBAR_AREAS && STATUSBAR_AREAS.right) || 'statusBar.right'
    items.push({ id: 'chip', area: bar, order: 90, render: () => jsx(Chip, {}) })
    ctx.registerMany(items)
    if (typeof ctx.onDispose === 'function') ctx.onDispose(() => { apiCtx = null })
  }
}
