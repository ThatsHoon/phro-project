import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'queue-panel',
  component: 'Pane',
  requestId: 'queue-panel',
  props: {
    title: 'Work queue',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const ticket = (id: string, status: string, priority: number, extra = {}) => ({
  id, title: `title ${id}`, status, priority, session: 's1', age_s: 3600, returns: 0, last_line: '', ...extra,
})

const BOARD = {
  generated: 0,
  counts: { 'needs-owner': 2, doing: 1, blocked: 1 },
  closed_7d: 4,
  tickets: [
    ticket('a', 'needs-owner', 1),
    ticket('b', 'needs-owner', 2),
    ticket('c', 'doing', 2, { last_line: 'step 3 done' }),
    ticket('d', 'blocked', 3),
  ],
}

function world(on: any) {
  const disk = { text: JSON.stringify(BOARD), mtime: 1 }
  const seen = { status: [] as (string | undefined)[], toasts: [] as string[], fills: [] as string[], submits: 0 }
  on('fs.stat', () => ({ value: { kind: 'file', size: disk.text.length, mtimeMs: disk.mtime } }))
  on('fs.read', () => ({ value: disk.text }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('command.register', () => ({ value: { command: 'queue-panel' } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('ui.status', (_: any, e: any) => { seen.status.push(e.text); return { value: undefined } })
  on('ui.toast', (_: any, e: any) => { seen.toasts.push(e.text); return { value: undefined } })
  on('prompt.fill', (_: any, e: any) => { seen.fills.push(e.text); return { isFilled: true } })
  on('prompt.submit', () => { seen.submits++; return {} })
  const clock = mock.clock(on)
  const write = (board: unknown) => { disk.text = typeof board === 'string' ? board : JSON.stringify(board); disk.mtime++ }
  return { seen, clock, write }
}

test('four sections in order, the answer button fills /inbox, the status counts the waiting', async ($, on) => {
  const { seen } = world(on)
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const text = (await ui.findAll({ type: 'Text' })).map(t => t.text).join('\n')
  const at = (s: string) => text.indexOf(s)
  expect(at('Waiting on you')).toBeGreaterThan(-1)
  expect(at('Waiting on you') < at('Running') && at('Running') < at('Attention') && at('Attention') < at('Done in 7 days')).toBe(true)
  expect(at('title a') < at('title b')).toBe(true)
  expect(text).toContain('step 3 done')
  expect(text).toContain('blocked 1')
  expect(text).toContain('4')
  await ui.press({ key: 'answer-a' })
  expect(seen.fills).toEqual(['/inbox a'])
  expect(seen.submits).toBe(0)
  expect(seen.status.at(-1)).toBe('waiting on you: 2')
  expect(seen.toasts).toEqual([])
  await ui.unmount()
})

test('a new priority-1 ticket toasts once, a priority-2 one does not', async ($, on) => {
  const { seen, clock, write } = world(on)
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  write({ ...BOARD, tickets: [...BOARD.tickets, ticket('e', 'needs-owner', 2)] })
  await clock.advance(15000)
  expect(seen.toasts).toEqual([])
  write({ ...BOARD, tickets: [...BOARD.tickets, ticket('e', 'needs-owner', 2), ticket('f', 'needs-owner', 1)] })
  await clock.advance(15000)
  await clock.advance(15000)
  expect(seen.toasts.length).toBe(1)
  expect(seen.toasts[0]).toContain('title f')
  expect(seen.status.at(-1)).toBe('waiting on you: 4')
})

test('a half-written board keeps the last good one drawn', async ($, on) => {
  const { clock, write } = world(on)
  await $.session.start({ cwd: '/p', surface: 'terminal', isInteractive: true })
  write('{"tickets": [')
  await clock.advance(15000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /title a/ }))).toBeDefined()
  await ui.unmount()
})

test('a queue.py run refreshes at once', async ($, on) => {
  const { seen, write } = world(on)
  on('tool.call', () => ({ result: {} }))
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  write({ ...BOARD, tickets: [ticket('a', 'needs-owner', 1)] })
  await $.tool.call({ tool: 'Bash', command: 'python -I -X utf8 scripts/queue.py decide . b --text go' })
  expect(seen.status.at(-1)).toBe('waiting on you: 1')
})
