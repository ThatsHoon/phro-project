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
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const A = 'aaaaaaaa-1111-4111-8111-111111111111'
const B = 'bbbbbbbb-2222-4222-8222-222222222222'
const NOW = 1_800_000_000

const ticket = (id: string, status: string, priority: number, extra = {}) => ({
  id, title: `title ${id}`, status, priority, session: A, age_s: 3600, changed_s: 600, returns: 0, can_wait: false,
  default: '', question: status === 'needs-owner' ? `question ${id}?` : '', still_working: false, last_line: '', ...extra,
})

const BOARD = {
  generated: NOW,
  counts: { 'needs-owner': 3, doing: 1, ready: 1, blocked: 0 },
  closed_7d: 4,
  inbox: '/phro-project_e-call:inbox',
  attention: { gone: 1, returned: 0, stale_ready: 1 },
  sessions: {
    [A]: { title: 'Fire model work', last_active: NOW - 60, gone: false },
    [B]: { title: B.slice(0, 8), last_active: null, gone: true },
  },
  tickets: [
    ticket('a', 'needs-owner', 1),
    ticket('b', 'needs-owner', 2, { session: B, still_working: false }),
    ticket('w', 'needs-owner', 2, { can_wait: true, default: 'keep A' }),
    ticket('r', 'ready', 2, { changed_s: 2 * 86400 }),
    ticket('c', 'doing', 2, { last_line: 'step 3 done' }),
  ],
}

function world(on: any, initial: unknown = BOARD) {
  const disk = { text: JSON.stringify(initial), mtime: 1 }
  const seen = { status: [] as (string | undefined)[], toasts: [] as string[], fills: [] as string[], submits: 0, opens: 0 }
  on('fs.stat', () => ({ value: { kind: 'file', size: disk.text.length, mtimeMs: disk.mtime } }))
  on('fs.read', () => ({ value: disk.text }))
  on('ui.open', () => { seen.opens++; return { value: { isPlaced: true } } })
  on('command.register', () => ({ value: { command: 'queue-panel' } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('ui.status', (_: any, e: any) => { seen.status.push(e.text); return { value: undefined } })
  on('ui.toast', (_: any, e: any) => { seen.toasts.push(e.text); return { value: undefined } })
  on('prompt.fill', (_: any, e: any) => { seen.fills.push(e.text); return { isFilled: true } })
  on('prompt.submit', (_: any, e: any) => { seen.submits++; return { text: e.text } })
  const clock = mock.clock(on)
  const write = (board: unknown) => { disk.text = typeof board === 'string' ? board : JSON.stringify(board); disk.mtime++ }
  return { seen, clock, write }
}

const textOf = async (ui: any) => (await ui.findAll({ type: 'Text' })).map((t: any) => t.text).join('\n')

test('sections in order, blocking only in Needs your answer, titles not ids, answer fills the copy inbox', async ($, on) => {
  const { seen } = world(on)
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const text = await textOf(ui)
  const at = (s: string) => text.indexOf(s)
  for (const s of ['Needs your answer (2)', 'Can wait (1)', 'Decided, not picked up (1)', 'In progress (1)', 'Sessions (2)']) expect(at(s)).toBeGreaterThan(-1)
  expect(at('Needs your answer') < at('Can wait') && at('Can wait') < at('Decided, not picked up')).toBe(true)
  expect(at('Decided, not picked up') < at('In progress') && at('In progress') < at('Sessions')).toBe(true)
  expect(at('title a') < at('title b') && at('title b') < at('Can wait')).toBe(true)
  expect(text).toContain('question a?')
  expect(text).toContain('Fire model work')
  expect(text).toContain('stopped')
  expect(text).toContain('decided 2d ago')
  expect(text).toContain('step 3 done')
  expect(text).toContain('1 with the asking session gone')
  expect(text).toContain('Done in 7 days: 4')
  expect(text).not.toContain('title w') // collapsed
  expect(text).not.toContain(A)
  expect(text).not.toContain(B)
  await ui.press({ key: 'answer-a' })
  expect(seen.fills).toEqual(['/phro-project_e-call:inbox a'])
  expect(seen.submits).toBe(0)
  expect(seen.status.at(-1)).toBe('waiting on you: 2')
  expect(seen.toasts).toEqual([])
  await ui.unmount()
})

test('drop needs a second press, then runs queue.py drop and redraws without the ticket', async ($, on) => {
  const ID = '20261110-pick-a'
  const tickets = [ticket(ID, 'needs-owner', 1), ...BOARD.tickets.slice(1)]
  const { write, seen } = world(on, { ...BOARD, tickets, script: 'C:/x/phro-project_e-call/scripts/queue.py', root: 'C:/p' })
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push([...e.argv])
    write({ ...BOARD, tickets: tickets.slice(1) })
    return { value: { exitCode: 0, stdout: `OK\tdrop\tC:/p/queue/tickets/${ID}.md\n`, stderr: '' } }
  })
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: `drop-${ID}` })
  expect(runs).toEqual([])
  expect(await textOf(ui)).toContain(`title ${ID}`)
  await ui.press({ key: `drop-${ID}` })
  expect(seen.toasts).toEqual([])
  expect(runs).toEqual([['python', '-I', '-X', 'utf8', 'C:/x/phro-project_e-call/scripts/queue.py', 'drop', 'C:/p', ID]])
  const text = await textOf(ui)
  expect(text).not.toContain(`title ${ID}`)
  expect(text).toContain('Needs your answer (1)')
  await ui.unmount()
})

test('drop never runs a script the board names unless it is queue.py', async ($, on) => {
  const { seen } = world(on, { ...BOARD, script: 'C:/evil/run.exe', root: 'C:/p' })
  const runs: unknown[] = []
  on('process.run', (_: any, e: any) => { runs.push(e.argv); return { exitCode: 0, stdout: '', stderr: '' } })
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'drop-a' })
  await ui.press({ key: 'drop-a' })
  expect(runs).toEqual([])
  expect(seen.toasts.at(-1)).toContain('Cannot drop')
  await ui.unmount()
})

test('the can-wait section opens on show and lists the default it will take', async ($, on) => {
  world(on)
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'can-wait-toggle' })
  const text = await textOf(ui)
  expect(text).toContain('title w')
  expect(text).toContain('otherwise: keep A')
  await ui.unmount()
})

test('empty sections other than the first are hidden', async ($, on) => {
  world(on, { ...BOARD, closed_7d: 0, attention: { gone: 0, returned: 0, stale_ready: 0 }, sessions: {}, tickets: [] })
  await $.session.start({ cwd: '/p', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const text = await textOf(ui)
  expect(text).toContain('Needs your answer (0)')
  expect(text).toContain('Nothing is waiting on you.')
  for (const s of ['Can wait', 'Decided', 'In progress', 'Sessions', 'Done in 7 days', 'gone']) expect(text).not.toContain(s)
  await ui.unmount()
})

test('a new blocking priority-1 ticket toasts once; a can-wait one does not', async ($, on) => {
  const { seen, clock, write } = world(on)
  await $.session.start({ cwd: '/p', surface: 'desktop', isInteractive: true })
  write({ ...BOARD, tickets: [...BOARD.tickets, ticket('e', 'needs-owner', 1, { can_wait: true, default: 'go' })] })
  await clock.advance(15000)
  expect(seen.toasts).toEqual([])
  write({ ...BOARD, tickets: [...BOARD.tickets, ticket('f', 'needs-owner', 1)] })
  await clock.advance(15000)
  await clock.advance(15000)
  expect(seen.toasts.length).toBe(1)
  expect(seen.toasts[0]).toContain('title f')
  expect(seen.status.at(-1)).toBe('waiting on you: 3')
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

test('installed mid-session (no session.start): the first prompt opens the panel, once', async ($, on) => {
  const { seen } = world(on)
  await $.prompt.submit({ text: 'hi' })
  expect(seen.opens).toBe(1)
  expect(seen.status.at(-1)).toBe('waiting on you: 2')
  await $.prompt.submit({ text: 'again' })
  expect(seen.opens).toBe(1)
})
