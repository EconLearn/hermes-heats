/**
 * Heats — Desktop half. A live pane that shows each attempt of the current Heats run as a lane
 * (queued → running → judging → PASS / FAIL / flagged), the before/after test counts, and an
 * "Apply winner" button; plus a status-bar chip while a run is in progress.
 *
 * Data: the agent half pushes `plugin.heats.run.update` events (broadcast_plugin_event) and writes
 * the same snapshot to plugin-data, served by dashboard/plugin_api.py at GET /live. The pane
 * listens to the event and polls /live every few seconds as a fallback.
 *
 * Loaded uncompiled: no JSX, only @hermes/plugin-sdk, react and react/jsx-runtime imports.
 */

import { Button, STATUSBAR_AREAS, haptic, host } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 3000

// ---- tiny store shared by the pane and the chip ----------------------------------------
let snapshot = { run_id: null }
const listeners = new Set()
function setSnapshot(next) {
  if (!next || typeof next !== 'object') return
  if (snapshot.run_id && next.run_id && next.run_id !== snapshot.run_id && next.phase === 'done' && snapshot.phase !== 'done') {
    // an older finished run arriving late must not replace a live one
    return
  }
  snapshot = next
  listeners.forEach(fn => fn(snapshot))
}
// referee verdicts (its message to the agent is not shown in the chat, so the pane shows them)
let refs = []
const refListeners = new Set()
function setRefs(list) {
  if (!Array.isArray(list)) return
  refs = list.slice(-6)
  refListeners.forEach(fn => fn(refs))
}
function addRef(v) {
  if (v && typeof v === 'object' && !refs.some(r => r.ts === v.ts)) setRefs(refs.concat([v]))
}
function useRefs() {
  const [r, setR] = useState(refs)
  useEffect(() => {
    refListeners.add(setR)
    return () => refListeners.delete(setR)
  }, [])
  return r
}

function useSnapshot() {
  const [s, setS] = useState(snapshot)
  useEffect(() => {
    listeners.add(setS)
    return () => listeners.delete(setS)
  }, [])
  return s
}

// ---- formatting --------------------------------------------------------------------------
const STATUS = {
  queued: { label: 'queued', color: 'var(--ui-text-quaternary)' },
  running: { label: 'running', color: 'var(--ui-accent)' },
  judging: { label: 'running tests', color: 'var(--ui-accent)' },
  pass: { label: 'PASS', color: 'var(--ui-success, #3fb950)' },
  fail: { label: 'FAIL', color: 'var(--ui-danger, #f85149)' },
  flagged: { label: 'passed, but edited tests', color: 'var(--ui-warning, #d29922)' },
  error: { label: 'error', color: 'var(--ui-danger, #f85149)' }
}

function fmtSecs(s) {
  if (!s && s !== 0) return ''
  const n = Math.round(s)
  return n >= 60 ? `${Math.floor(n / 60)}m${String(n % 60).padStart(2, '0')}s` : `${n}s`
}

function tests(h) {
  if (h.passed == null && h.failed == null) return ''
  const total = (h.passed || 0) + (h.failed || 0)
  return `${h.passed || 0}/${total} tests`
}

// ---- components --------------------------------------------------------------------------
function RefereeCard() {
  const list = useRefs()
  if (!list.length) return null
  const ago = ts => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
    return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`
  }
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md px-2.5 py-2',
    style: { border: '1px solid var(--ui-stroke-secondary)' },
    children: [
      jsx('div', { className: 'text-[0.75rem] font-medium text-(--ui-text-secondary)', children: 'Referee: tests run when the agent says it is done' }),
      ...list.slice().reverse().slice(0, 4).map(v => {
        const red = !v.ok
        const counts = v.passed != null || v.failed != null ? ` · ${v.passed || 0} passed, ${v.failed || 0} failed` : ''
        const verdict = v.timed_out ? 'tests timed out' : red ? `tests red (exit ${v.exit_code})` : 'tests green'
        const action = red ? (v.last_nudge ? ` · sent back (last of ${v.max_nudges})` : ` · sent back (${v.attempt}/${v.max_nudges})`) : ' · done'
        return jsxs('div', {
          className: 'flex items-center gap-2 text-[0.75rem]', key: String(v.ts),
          children: [
            jsx('span', { style: { width: 8, height: 8, borderRadius: 9999, flex: 'none', background: red ? 'var(--ui-danger, #f85149)' : 'var(--ui-success, #3fb950)' } }),
            jsx('span', { children: `${v.repo || ''}: ${verdict}${counts}${action}` }),
            jsx('span', { className: 'ml-auto text-(--ui-text-quaternary)', children: ago(v.ts) })
          ]
        })
      })
    ]
  })
}


function Lane({ h, isWinner }) {
  const st = STATUS[h.status] || STATUS.queued
  const live = h.status === 'running' || h.status === 'judging'
  const diff = h.files ? `+${h.insertions} −${h.deletions} · ${h.files} file${h.files === 1 ? '' : 's'}` : ''
  let note = ''
  if (h.claimed_unchanged && !h.files) note = 'claimed a fix but changed nothing'
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md px-2.5 py-2',
    style: {
      border: `1px solid ${isWinner ? st.color : 'var(--ui-stroke-secondary)'}`,
      boxShadow: isWinner ? `0 0 0 1px ${st.color}` : 'none'
    },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.8125rem]',
        children: [
          jsx('span', {
            style: {
              width: 8, height: 8, borderRadius: 9999, background: st.color, flex: 'none',
              animation: live ? 'pulse 1.2s ease-in-out infinite' : 'none'
            }
          }),
          jsx('span', { className: 'font-medium', children: `Heat ${h.index}` }),
          jsx('span', { className: 'text-(--ui-text-tertiary)', children: h.strategy }),
          h.lap > 1 ? jsx('span', { className: 'text-(--ui-text-quaternary)', title: 'restarted with the real test output', children: `lap ${h.lap}` }) : null,
          h.model ? jsx('span', { className: 'text-(--ui-text-quaternary) truncate', children: h.model }) : null,
          jsx('span', { className: 'ml-auto font-medium', style: { color: st.color }, children: isWinner ? `WINNER · ${st.label}` : st.label })
        ]
      }),
      jsxs('div', {
        className: 'flex gap-3 text-[0.75rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: tests(h) }),
          jsx('span', { children: diff }),
          jsx('span', { className: 'ml-auto', children: fmtSecs(h.seconds) })
        ]
      }),
      note ? jsx('div', { className: 'text-[0.75rem]', style: { color: 'var(--ui-warning, #d29922)' }, children: note }) : null
    ]
  })
}

function HeatsPane({ ctx }) {
  const s = useSnapshot()
  const [busy, setBusy] = useState(false)
  const [applied, setApplied] = useState(null)

  useEffect(() => setApplied(s.applied ? 'applied' : null), [s.run_id, s.applied])

  if (!s.run_id) {
    return jsxs('div', {
      className: 'flex h-full flex-col gap-2 p-3 text-sm text-(--ui-text-tertiary)',
      children: [
        jsx('div', { className: 'font-medium text-(--ui-text-secondary)', children: 'Heats' }),
        jsx(RefereeCard, {}),
        jsx('div', { children: 'No race yet. Ask the agent: "use heats (3) to fix the failing test".' })
      ]
    })
  }

  const heats = s.heats || []
  const done = s.phase === 'done'
  const failed = s.phase === 'error'
  const b = s.baseline
  const apply = async () => {
    setBusy(true)
    haptic && haptic('tap')
    try {
      const r = await ctx.rest('/apply', { method: 'POST', body: { run_id: s.run_id, heat: s.winner } })
      setApplied(r && r.ok ? 'applied' : 'failed')
      host.notify({ kind: r && r.ok ? 'success' : 'error', message: (r && r.message) || 'apply failed' })
    } catch (e) {
      setApplied('failed')
      host.notify({ kind: 'error', message: `Heats: ${e && e.message ? e.message : e}` })
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-auto p-3 text-sm',
    children: [
      jsx('style', { children: '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}' }),
      jsxs('div', {
        className: 'flex items-baseline gap-2',
        children: [
          jsx('span', { className: 'font-medium', children: `🏁 Heats · ${s.repo || ''}` }),
          jsx('span', { className: 'text-[0.75rem] text-(--ui-text-quaternary)', children: s.run_id })
        ]
      }),
      jsx(RefereeCard, {}),
      jsx('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary) truncate', title: s.check, children: `check: ${s.check || ''}` }),
      b ? jsx('div', {
        className: 'text-[0.75rem] text-(--ui-text-tertiary)',
        children: `before: ${b.ok ? 'passing' : 'failing'}${b.passed != null ? ` · ${b.passed}/${(b.passed || 0) + (b.failed || 0)} tests` : ''}`
      }) : null,
      ...heats.map(h => jsx(Lane, { h, isWinner: done && s.winner === h.index }, h.index)),
      failed ? jsx('div', {
        className: 'mt-1 text-[0.8125rem]', style: { color: 'var(--ui-danger, #f85149)' },
        children: 'The run stopped because of an error. Nothing was applied; see the chat for details.'
      }) : null,
      done ? jsx('div', {
        className: 'mt-1 text-[0.8125rem]',
        children: s.winner
          ? jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                jsx('span', { children: applied === 'applied' ? `Heat ${s.winner} applied. Review with git diff.` : `Heat ${s.winner} wins. Your repo is unchanged until you apply it.` }),
                applied === 'applied' ? null : jsx(Button, { size: 'sm', disabled: busy, onClick: apply, children: busy ? 'Applying…' : 'Apply winner' })
              ]
            })
          : jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'No attempt passed cleanly, so nothing will be applied.' })
      }) : null
    ]
  })
}

function Chip() {
  const s = useSnapshot()
  if (!s.run_id || s.phase === 'done' || s.phase === 'error') return null
  const heats = s.heats || []
  const finished = heats.filter(h => ['pass', 'fail', 'flagged', 'error'].includes(h.status)).length
  const passing = heats.filter(h => h.status === 'pass').length
  return jsx('span', {
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: `Heats ${s.run_id}: ${finished} of ${heats.length} judged, ${passing} passing`,
    children: `🏁 heats ${finished}/${heats.length}${passing ? ` · ${passing} ✓` : ''}`
  })
}

export default {
  id: 'heats',
  name: 'Heats',
  register(ctx) {
    const refresh = () => ctx.rest('/live').then(setSnapshot).catch(() => {})
    refresh()
    ctx.onEvent('plugin.heats.run.update', ev => setSnapshot(ev && ev.payload))
    const refreshRefs = () => ctx.rest('/referee').then(r => setRefs(r && r.verdicts)).catch(() => {})
    refreshRefs()
    ctx.onEvent('plugin.heats.referee', ev => addRef(ev && ev.payload))
    ctx.setInterval(refreshRefs, POLL_MS * 2)
    ctx.setInterval(() => {
      if (!snapshot.run_id || (snapshot.phase !== 'done' && snapshot.phase !== 'error')) refresh()
    }, POLL_MS)
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Heats',
      data: { placement: 'right', width: '340px' },
      render: () => jsx(HeatsPane, { ctx })
    })
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(Chip, {}) })
  }
}
