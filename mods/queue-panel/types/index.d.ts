export type Ticket = {
  id: string
  title: string
  status: string
  priority: number
  session: string
  age_s: number
  returns: number
  last_line: string
}

export type Board = {
  generated: number
  counts: Record<string, number>
  closed_7d: number
  tickets: Ticket[]
}

declare module 'claude-code' {
  interface PluginState {
    'queue-panel': { board: Shaped<Board | null> }
  }
}
