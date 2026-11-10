import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Board, Ticket } from '../types'

// The panel only reads queue/board.json, which the project's queue.py writes on every change.
const PANE = 'queue-panel'
const TITLE = 'Work queue'
const FILE = 'queue/board.json'
const EVERY_MS = 15000
const board = atom({ plugin: 'queue-panel', key: 'board' } as const, null, { shape: 'board-1' })

const age = (s: number) => (s >= 86400 ? `${Math.floor(s / 86400)}d` : s >= 3600 ? `${Math.floor(s / 3600)}h` : `${Math.max(1, Math.floor(s / 60))}m`)

let mtime = -1
let seen: Set<string> | null = null // waiting ids at the last read; null before the first, so a reload toasts nothing

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
  const waiting = next.tickets.filter(t => t.status === 'needs-owner')
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
    const waiting = b.tickets.filter((t: Ticket) => t.status === 'needs-owner')
    const running = b.tickets.filter((t: Ticket) => t.status === 'doing' || t.status === 'verify')
    const blocked = b.counts.blocked ?? 0
    const thrice = b.tickets.filter((t: Ticket) => t.returns >= 3 && t.status !== 'done').length
    return (
      <Box flexDirection="column">
        <Text bold>Waiting on you ({waiting.length})</Text>
        {waiting.length === 0 && <Text dimColor>Nothing waiting.</Text>}
        {waiting.map((t: Ticket) => (
          <Box flexDirection="row">
            <Text>P{t.priority} {t.title} <Text dimColor>{t.session} · {age(t.age_s)}</Text></Text>
            <Button key={`answer-${t.id}`} onPress={() => void $.prompt.fill({ text: `/inbox ${t.id}` })}>answer</Button>
          </Box>
        ))}
        <Text bold>Running ({running.length})</Text>
        {running.length === 0 && <Text dimColor>Nothing running.</Text>}
        {running.map((t: Ticket) => (
          <Text>{t.title} <Text dimColor>{t.status} · {t.session} · {t.last_line || 'no record line'}</Text></Text>
        ))}
        <Text bold>Attention</Text>
        <Text>blocked {blocked} · returned 3 times {thrice}</Text>
        <Text bold>Done in 7 days</Text>
        <Text>{b.closed_7d}</Text>
      </Box>
    )
  })
}
