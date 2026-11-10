import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Board, Ticket } from '../types'

// The panel only reads queue/board.json, which the project's queue.py writes on every change.
const PANE = 'queue-panel'
const TITLE = 'Work queue'
const FILE = 'queue/board.json'
const EVERY_MS = 15000
const WORKING_S = 600
const board = atom({ plugin: 'queue-panel', key: 'board' } as const, null, { shape: 'board-2' })
const canWaitOpen = atom({ plugin: 'queue-panel', key: 'canWaitOpen' } as const, false)
const confirmDrop = atom({ plugin: 'queue-panel', key: 'confirmDrop' } as const, null as string | null)
const TICKET_ID = /^\d{8}-[a-z0-9-]+$/

// The first press arms the button, the second closes the ticket through queue.py, the queue's only writer.
// board.json names the script; only a queue.py path and a ticket-shaped id are ever run.
async function drop($: any, b: Board, id: string, isArmed: boolean) {
  if (!isArmed) return void (await update($, confirmDrop, () => id)) // the drawn label said "drop", not "confirm drop"
  await update($, confirmDrop, () => null)
  if (!/\/scripts\/queue\.py$/.test(b.script ?? '') || !TICKET_ID.test(id)) return void $.ui.toast('Cannot drop: the board names no queue script')
  try {
    const r = await $.process.run(['python', '-I', '-X', 'utf8', b.script, 'drop', b.root, id])
    if (r.exitCode !== 0 || !String(r.stdout).startsWith('OK')) $.ui.toast(`Drop failed: ${String(r.stdout || r.stderr).split('\n')[0]}`)
  } catch {
    $.ui.toast('Drop failed: python did not start')
  }
  await refresh($, true)
}

const age = (s: number) => (s >= 86400 ? `${Math.floor(s / 86400)}d` : s >= 3600 ? `${Math.floor(s / 3600)}h` : `${Math.max(1, Math.floor(s / 60))}m`)
const cut = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
// a ticket blocks its session unless it carries a default the session goes on with
const blocking = (b: Board) => b.tickets.filter(t => t.status === 'needs-owner' && !t.can_wait)

let mtime = -1
let seen: Set<string> | null = null // blocking ids at the last read; null before the first, so a reload toasts nothing

async function refresh($: any, force = false) {
  let stat
  try {
    stat = await $.fs.stat(FILE)
  } catch {
    return
  }
  if (!force && stat.mtimeMs === mtime) return
  let next: Board
  try {
    next = JSON.parse((await $.fs.read(FILE)) as string)
  } catch {
    return // half-written or broken: keep the last good board
  }
  if (!next || !Array.isArray(next.tickets)) return
  mtime = stat.mtimeMs
  const waiting = blocking(next)
  if (seen) {
    for (const t of waiting) if (t.priority === 1 && !seen.has(t.id)) $.ui.toast(`Decision needed: ${t.title}`)
  }
  seen = new Set(waiting.map(t => t.id))
  $.ui.status(waiting.length ? `waiting on you: ${waiting.length}` : undefined)
  await update($, board, () => next)
}

let opened = false // per module load: a reload starts over, so the first prompt after /reload-plugins opens it

async function start($: any) {
  if (opened) return
  try {
    await $.fs.stat(FILE)
  } catch {
    return // no queue in this project: stay out of the way
  }
  opened = true
  await $.command.register({ name: 'queue-panel', description: 'Show the work queue beside the chat' })
  await refresh($, true)
  void $.ui.open({ id: PANE, title: TITLE })
  $.clock.every(EVERY_MS, () => void refresh($))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await start($)
    return started
  })

  // session.start fires once per session, so a panel installed or reloaded mid-session opens at the next prompt
  on('prompt.submit', async ($, e, next) => {
    await start($)
    return next(e)
  }).catch(($, e, next) => next(e)) // a failed start never changes the prompt

  on('command.run', { command: 'queue-panel' }, async $ => {
    await refresh($, true)
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'Work queue opened.' }
  })

  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    if ((e.tool === 'Bash' || e.tool === 'PowerShell') && String((e as any).command ?? '').includes('queue.py')) await refresh($)
    return ran
  }).catch(($, e, next) => next(e)) // a failed refresh never changes the tool call's result

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const b = await read($, board)
    if (!b) return <Text dimColor>No queue/board.json in this project.</Text>
    const open = await read($, canWaitOpen)
    const sessions = b.sessions ?? {}
    // full session ids never reach the pane: the session's title, else the first 8 characters
    const who = (t: Ticket) => cut(sessions[t.session]?.title || t.session.slice(0, 8), 40)
    const now = b.generated
    const needs = blocking(b)
    const later = b.tickets.filter(t => t.status === 'needs-owner' && t.can_wait)
    const decided = b.tickets.filter(t => t.status === 'ready')
    const running = b.tickets.filter(t => t.status === 'doing' || t.status === 'verify')
    const people = Object.entries(sessions)
    const at = b.attention ?? { gone: 0, returned: 0, stale_ready: 0 }
    const notes = [
      at.gone ? `${at.gone} with the asking session gone` : '',
      at.returned ? `${at.returned} returned 3 times` : '',
      b.counts?.blocked ? `${b.counts.blocked} blocked` : '',
    ].filter(Boolean)
    const armed = await read($, confirmDrop)
    const answer = (t: Ticket) => (
      <Box flexDirection="row" gap={1}>
        <Button key={`answer-${t.id}`} onPress={() => void $.prompt.fill({ text: `${b.inbox || '/inbox'} ${t.id}` })}>answer</Button>
        <Button key={`drop-${t.id}`} onPress={() => void drop($, b, t.id, armed === t.id)}>{armed === t.id ? 'confirm drop' : 'drop'}</Button>
      </Box>
    )
    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="column">
          <Text bold>Needs your answer ({needs.length})</Text>
          {needs.length === 0 && <Text dimColor>Nothing is waiting on you.</Text>}
          {needs.map(t => (
            <Box key={`need-${t.id}`} flexDirection="column">
              <Text>
                <Text bold color={t.priority === 1 ? 'warning' : undefined}>P{t.priority}</Text> {t.title}
              </Text>
              {t.question ? <Text wrap="wrap">{cut(t.question.replace(/\s+/g, ' '), 160)}</Text> : null}
              <Box flexDirection="row" gap={1}>
                <Text dimColor>
                  {who(t)} · {t.still_working ? 'still working' : 'stopped'} · {age(t.age_s)}
                </Text>
                {answer(t)}
              </Box>
            </Box>
          ))}
        </Box>
        {later.length > 0 && (
          <Box flexDirection="column">
            <Box flexDirection="row" gap={1}>
              <Text bold>Can wait ({later.length})</Text>
              <Button key="can-wait-toggle" onPress={() => void update($, canWaitOpen, v => !v)}>{open ? 'hide' : 'show'}</Button>
            </Box>
            {open &&
              later.map(t => (
                <Box key={`wait-${t.id}`} flexDirection="row" gap={1}>
                  <Text>
                    {t.title} <Text dimColor>· {who(t)} · otherwise: {cut(t.default, 80)}</Text>
                  </Text>
                  {answer(t)}
                </Box>
              ))}
          </Box>
        )}
        {decided.length > 0 && (
          <Box flexDirection="column">
            <Text bold>Decided, not picked up ({decided.length})</Text>
            {decided.map(t => (
              <Text key={`ready-${t.id}`} color={t.changed_s > 86400 ? 'warning' : undefined}>
                {t.title} <Text dimColor>· {who(t)} · {t.dropped ? 'dropped' : 'decided'} {age(t.changed_s)} ago</Text>
              </Text>
            ))}
          </Box>
        )}
        {running.length > 0 && (
          <Box flexDirection="column">
            <Text bold>In progress ({running.length})</Text>
            {running.map(t => (
              <Text key={`run-${t.id}`}>
                {t.title} <Text dimColor>· {t.status} · {who(t)} · {t.last_line || 'no record line'}</Text>
              </Text>
            ))}
          </Box>
        )}
        {people.length > 0 && (
          <Box flexDirection="column">
            <Text bold>Sessions ({people.length})</Text>
            {people.map(([sid, s]) => {
              const open = b.tickets.filter(t => t.session === sid && t.status !== 'done').length
              const state = s.gone ? 'gone' : now - (s.last_active ?? 0) < WORKING_S ? 'working' : `idle ${age(now - (s.last_active ?? 0))}`
              return (
                <Text key={`s-${sid}`}>
                  {cut(s.title || sid.slice(0, 8), 40)} <Text dimColor>· {state} · {open} open</Text>
                </Text>
              )
            })}
          </Box>
        )}
        {notes.length > 0 && <Text color="warning">{notes.join(' · ')}</Text>}
        {b.closed_7d > 0 && <Text dimColor>Done in 7 days: {b.closed_7d}</Text>}
      </Box>
    )
  })
}
