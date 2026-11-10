# Ticket routing across sessions: design

## Goal

The owner can answer any waiting ticket from any session of the project, and the answer reaches the session that asked, by its exact id, without that work leaking into the session that took the answer. Routing is done by hooks and `queue.py`, with no model in the middle: the only model step is reading a free-form answer, done by the session that already holds it.

Owner decisions (2026-11-10): exact ids, answer from any fork, built thoroughly; no resident MCP server or broker model (a broker would re-read what the answering session already holds and cannot wake a session, since session messaging is a harness tool).

## Facts this rests on

- Every hook event carries `session_id`, the session's transcript id (a UUID). The desktop app addresses the same session as `local_<UUID>`; the four linked forks checked on 2026-11-10 all follow this.
- A ticket's life: `ask` sets `needs-owner`; `decide` sets `ready` with `waiting_on: agent`; the owner session moves it to `doing`, `verify`, `done`.
- Today's faults: ticket `session` fields hold three forms (`local_<UUID>`, bare UUID, an 8-character prefix); `inbox` delivers only to an exact listed id, so about half never arrive; and only `inbox` delivers at all, so an answer typed into another fork's chat is not recorded or routed.

## Identity

- The canonical session value is the bare UUID, lowercase, `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`.
- `queue.py ask --session` accepts the UUID or `local_<UUID>` and stores the UUID; anything else is refused with a message naming the expected form.
- The queue rule a session receives carries its own id, filled in by the hook: `--session <UUID>`. A session never types or guesses its id.
- `queue.py sessions <root>` repairs existing tickets: strips `local_`; resolves a shorter prefix to the one transcript in `~/.claude/projects/<project folder>/` whose name starts with it; leaves and reports a value that matches none or several. It commits only the tickets it changed.

## Hook: `queue.py hook`

One standard-library hook registered for `SessionStart` (matcher `startup|resume|clear|compact`) and `UserPromptSubmit`. It reads `<root>/queue/board.json` only, never ticket files, prints nothing when there is no queue or the board does not parse, and never fails the event. `<root>` is the event's `cwd`, or the project root above it that holds `queue/`.

- `SessionStart`: prints the queue rule with this session's id. The rule moves here from `compass.py`, which stops printing it, so all queue text has one owner.
- Both events, for this session (`session` equals this `session_id`): each ticket in `ready` gets one line, `Ticket <id> was decided for you: read <absolute ticket path>, then run <queue> move <root> <id> doing.` This is the pull: an answer reaches its session on the next prompt or reopen even when no message arrived.
- `UserPromptSubmit`, for other sessions: each ticket id named in the prompt (`\b\d{8}-[a-z0-9-]+\b`, kept only when the id is on the board) and owned by another session gets one line: `Ticket <id> belongs to session <UUID> ("<title>"), not this one. Record the owner's answer with <queue> decide <root> <id> --text "<answer>", then deliver: <queue> deliver <root> <id>. Do not do that ticket's work here.`
- A prompt that answers without naming an id is not guessed at. The rule says: in a session that does not own the ticket, ask the owner for the ticket id in one line.
- Output is capped at 10 lines; titles are cut to 80 characters and stripped of newlines and backticks.

## Delivery: `queue.py deliver <root> <id>`

Prints the exact message and target for the session to send, since only the session holds the messaging tool:

```
SEND	local_<UUID>	ticket <id> decided, read the ticket file <absolute path>
```

The session sends it with its messaging tool to that target. If the tool refuses the target (a CLI session, or a session no longer running), nothing else is needed: the pull delivers on the owner's next prompt. `inbox` step 5 is replaced by the same command, so `inbox` and an answer typed anywhere take one path.

## Blocking or not

"Waiting on you" must mean a session is stopped until the owner answers. Owner report 2026-11-10: a ticket whose session had already moved on (`NPU 속도 측정 대상`) sat there with the real blockers.

- `ask` takes `--default "<what I do if no answer>"`. With it, the ticket is `can-wait`: the session goes on with the default, and an answer only changes course. Without it, the ticket is blocking and the session stops after asking. The rule says to give a default whenever one is reasonable.
- When a session proceeds past its own ticket (it chose, or the question went away), it runs `queue.py done <root> <id>` or `decide <root> <id> --text "self: <what it did>"`. The rule says so, and the session's own hook lists its open tickets once at `SessionStart` so stale ones get closed.
- The board reads each owner session's last activity from its transcript's modification time. A blocking ticket whose session wrote after asking is shown as "session still working", a hint that it may no longer block.

## Board and pane

Owner report 2026-11-10: the pane is hard to read (raw UUIDs, one undifferentiated list). The board data and the pane follow one layout:

- Each session is named by its title: the last `custom-title` entry in its transcript, read from the file's tail (last 64 KB) so it stays fast. Without one, the first 8 characters of the id. Full ids never appear in the pane.
- Sections, in order, empty ones hidden except the first:
  1. **Needs your answer**: blocking tickets, priority then age. Card: priority chip, title, the question cut to two lines, the session title with "stopped" or "still working", age, and an `answer` button that sends `/phro-project:inbox <id>` (the copy's name in a project).
  2. **Can wait**: can-wait tickets, collapsed to a count line until expanded; card shows the default it will take.
  3. **Decided, not picked up**: `ready` tickets, with how long since the decision; more than a day is highlighted.
  4. **In progress**: `doing` and `verify`, one line each with the session title and the ticket's last record line.
  5. **Sessions**: one line per session with open tickets: title, stopped or working, last activity, counts.
  6. **Done in 7 days**: a count.
- Attention lines: owner gone (no transcript with that UUID in the project folder; shown, not reassigned; `queue.py move ... --session <UUID>` is a new option to reassign) and returned 3 times.
- The HTML page (`inbox.html`, the `show` skill) uses the same sections and adds the core documents map at the end.

## Tests (`tests/test_queue.py`)

Two simulated sessions A and B, fed through `queue.py hook` with JSON on stdin:

- A's `SessionStart` rule contains `--session <A>`; `ask --session local_<A>` stores `<A>`; `ask --session abc` and a prefix are refused.
- B's prompt naming A's ticket gets the "belongs to session <A>" line and the decide and deliver commands; B's prompt naming nothing gets no output; a made-up id gets no output.
- After `decide`, A's next prompt gets "decided for you"; after `move ... doing`, it gets nothing. B never gets A's "decided for you" line.
- `deliver` prints `SEND\tlocal_<A>\tticket ...` with the absolute path.
- `sessions` repairs a `local_` value and a unique prefix, reports an ambiguous prefix and leaves it.
- No queue, a broken `board.json`, and broken stdin: silent, exit 0.
- Eleven waiting tickets for one session: output stops at 10 lines.
- `compass.py hook` no longer prints the queue rule; `queue.py hook` does.
- `ask --default` lands in Can wait, not Needs your answer; `decide --text "self: ..."` and `done` clear it.
- The board names a session by its last `custom-title`, falls back to the short id, and never prints a full UUID in the pane data; a session whose transcript changed after the ask is marked still working; a UUID with no transcript is counted as owner gone.
- The pane test (`mods/queue-panel/hooks/pane.test.tsx`) renders each section from a seeded board and checks the answer button's prompt and that empty sections other than the first are hidden.

## Rollout

After merge and sync to the e-call copy: run `queue.py sessions` on e-call once, then `/reload-plugins`. Open forks receive the new rule on their next resume, or by one message from the main session.

## Out of scope

- A resident broker, an MCP server, or model calls inside hooks.
- Reading free-form answers that name no ticket id in a session that does not own the ticket.
- Ranking or summarising what a session is doing; the pane shows only its title, state and last activity.
