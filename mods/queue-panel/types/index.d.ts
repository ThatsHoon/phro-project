export type Ticket = {
  id: string
  title: string
  status: string
  priority: number
  session: string
  age_s: number
  changed_s: number
  returns: number
  can_wait: boolean
  default: string
  question: string
  still_working: boolean
  last_line: string
}

export type Session = {
  title: string
  last_active: number | null
  gone: boolean
}

export type Board = {
  generated: number
  counts: Record<string, number>
  closed_7d: number
  inbox: string
  attention: { gone: number; returned: number; stale_ready: number }
  sessions: Record<string, Session>
  tickets: Ticket[]
}

declare module 'claude-code' {
  interface PluginState {
    'queue-panel': { board: Shaped<Board | null>; canWaitOpen: boolean }
  }
}
