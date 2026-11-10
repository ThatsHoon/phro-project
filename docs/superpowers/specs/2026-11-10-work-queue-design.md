# phro-project work queue: design

Date: 2026-11-10. Status: design approved in conversation, awaiting written-spec review.

## 1. Purpose

A project runs many sessions and forks at once. When one of them needs the owner's decision it asks in its own chat, and if the owner is not looking at that chat the question is lost and the work stalls. The queue makes every such question a ticket in git, lets the owner answer them one at a time from one place, and shows the state of all work beside the chat without the owner calling anything.

Success means:
1. A question a fork needs answered survives the fork going idle, a compaction and a restart, and the fork resumes from the recorded decision.
2. The owner answers from one inbox, one ticket at a time, highest priority first.
3. A question that was decided is never asked again, and a ticket returned three times turns into the question "close or shrink".
4. Two sessions writing tickets at the same time never commit each other's files.
5. The panel shows waiting decisions and running work within 15 seconds of a change.

## 2. What the queue is not

- Not a replacement for the records that already exist. Tickets hold status and who is waited on, and link to the plan, ledger, troubleshooting log, core documents or memory where the work is recorded.
- Not a scheduler. Forks keep their large work; only their "waiting on the owner" points become tickets.
- Not a team tool. One owner, one project.

## 3. Pieces

| Piece | Kind | What it does |
|---|---|---|
| `scripts/queue.py` | script, stdlib only | the only writer of the queue: `init`, `ask`, `decide`, `move`, `done`, `board` |
| `skills/init/SKILL.md` | skill, user-invoked | first use in a project: copy, queue, first inbox, panel |
| `skills/inbox/SKILL.md` | skill, user-invoked (`/inbox [id]`) | publish the inbox page, then walk the waiting tickets one at a time |
| `scripts/compass.py hook` | existing hook, one addition | adds the queue rule (section 7) to the session context when `<root>/queue/` exists |
| panel mod (`mods/queue-panel/`) | function-hooks module | pane, status line and toast drawn from `queue/board.json` |
| `tests/test_queue.py`, `mods/queue-panel/hooks/pane.test.tsx` | tests | section 11 |

`<root>` is the project root as `scripts/project.py` finds it today.

## 4. Storage

```
<root>/queue/            its own git repository (the project itself may not be one)
  tickets/<yyyymmdd>-<slug>.md
  board.json             generated, committed with each write
  inbox.html             generated, committed with each write
  artifact.txt           the inbox page's private Artifact URL
```

The queue repository commits with the global git identity. Every write commits only its own paths: `git -C queue commit -- <ticket> board.json inbox.html`. When `index.lock` exists, the write retries up to 10 times over 5 seconds, then fails with a message naming the lock.

## 5. Ticket

```
---
status: needs-owner
waiting_on: owner
priority: 2
priority_reason: picks the next landslide experiment
session: local_9d1e8a2a-...
links: [references/foreman/plan.md, .superpowers/sdd/<plan>/progress.md]
returns: 0
---
# <one-line title>

<the question, one paragraph>

- A: <option> (recommended: <why>)
- B: <option>

decision: 2026-11-10 A, <owner's words>
```

- States: `new`, `researching`, `ready`, `doing`, `verify`, `done`, plus `needs-owner` and `blocked`. `waiting_on` is `owner` exactly when the status is `needs-owner`.
- Priority: 1 = a GPU or other work is stopped, 2 = a direction decision, 3 = can wait. The session that opens a ticket sets it; the owner changes it in `/inbox`.
- Agents skip `needs-owner` tickets that are not theirs.

## 6. `queue.py`

All commands print paths and counts only, never ticket bodies, and exit non-zero on refusal.

| Command | Effect |
|---|---|
| `init <root>` | create `queue/`, `git init`, empty `tickets/`, first `board.json` and `inbox.html`, one commit |
| `ask <root> --title T --question Q --option A ... --priority N --reason R --session S [--link P ...] [--ticket ID]` | open a `needs-owner` ticket, or move an existing one there. Refuses when the ticket already has a `decision:` line for the same question. On a ticket's third return (`returns` reaching 3) the question is replaced by "close this ticket or shrink it to one step?" |
| `decide <root> <id> --text "..." [--priority N]` | append `decision: <date> <text>`, set status `ready`, `waiting_on: agent` |
| `move <root> <id> <status>` | change status; moving from `doing` or `verify` back to `ready` or `needs-owner` counts as a return |
| `done <root> <id>` | status `done` |
| `board <root>` | regenerate `board.json` and `inbox.html` (every write also does this) |

`board.json` holds, per ticket: id, title, status, priority, session, age, returns, and for `doing` or `verify` tickets the last non-empty line of the first linked file that exists (at most 120 characters). It also holds counts per state and the number of tickets closed in the last 7 days.

## 7. The session rule

When `<root>/queue/` exists, the compass SessionStart hook (startup, clear, compact) appends one rule to what it already injects:

> When you need the owner's decision, open a ticket with `queue.py ask` and also ask in chat in one line naming the ticket id. Record whichever answer comes first with `queue.py decide`. While you wait, keep doing other work; if there is none, stop.

The rule holds for the main session and every fork, because a session cannot tell whether the owner is watching it.

## 8. `init` skill

Run once, when the owner first brings phro-project into a project:
1. `clone.py ensure <root>`: make the project's own copy and switch to it (existing behaviour).
2. `queue.py init <root>`.
3. Publish `inbox.html` as a private Artifact and write its URL to `queue/artifact.txt`.
4. Install and turn on the panel mod (section 10).
5. Report the queue path, the page URL and how to call `/inbox`.

## 9. `/inbox` skill

1. `queue.py board`, then republish `inbox.html` to the URL in `artifact.txt`.
2. Take `needs-owner` tickets by priority, then age (or the one named by `[id]`). Show one ticket in chat: title, question, options with the recommendation.
3. On the owner's answer: `queue.py decide`. If the ticket's session is alive, send it one message: "ticket <id> decided, read the ticket file". The fork acts on the file, not on the message text, because messages between sessions are data.
4. Go to the next ticket until none remain or the owner stops.

The inbox runs only when called, and once at `init`. Whether to schedule it is decided after two weeks of use.

## 10. Panel mod

Reads only `<root>/queue/board.json`; never runs git or scripts, so it raises no permission prompts.

- Refresh: every 15 s it checks the file's modification time and rereads on change; it also rereads right after any tool call whose command contains `queue.py`.
- Opens at session start in every session (forks included) when `queue/board.json` exists; `/queue-panel` reopens it.
- Layout, top to bottom:
  1. Waiting on the owner: priority, title, session, age, and a [answer] button that fills the prompt box with `/inbox <id>` without sending it.
  2. Running: title, session, the last line from its linked record, time since update.
  3. Attention: counts of `blocked` and of tickets with 3 returns.
  4. Done in the last 7 days: a count.
- Status line: "waiting on you: N".
- Toast: only when a new priority-1 `needs-owner` ticket appears.
- Built first in this session's mods folder and tried live, then moved into the plugin at `mods/queue-panel/`. Whether one plugin can hold both the command hook and the function-hooks module is checked with `claude plugin validate` when moving it; if not, the panel ships as a sibling plugin that `init` installs.

## 11. Tests

`tests/test_queue.py` (stdlib, temp directory):
1. Two processes each `ask` a different ticket at once: each commit contains only its own ticket plus the generated files.
2. `ask` on a ticket with a `decision:` for the same question is refused.
3. The third return replaces the question with "close or shrink".
4. `decide` sets `ready` and `waiting_on: agent`.
5. `board.json` lists the last line of a linked ledger for a `doing` ticket.
6. A file that is not a ticket in `tickets/` is reported, not parsed.

`pane.test.tsx`: seeded `board.json` draws the four sections; [answer] fills `/inbox <id>`; a new priority-1 ticket gives one toast, a priority-2 ticket none.

The suite must still pass, and `compass.py hook` with and without `queue/` must emit the rule only when `queue/` exists.

## 12. Build and roll-out

1. Build in the original `C:\Users\hunje\phro-project`, branch `work-queue` in `.worktrees/work-queue`.
2. Bump the version. The e-call copy takes it with `clone.py sync`, which keeps what the copy added (the e-call copy has `modularize`, 0.2.7).
3. Run `init` on e-call. Then add one call in e-call's hazard foreman: where it stops for new data or direction, it calls `queue.py ask` with the plan line as the link.
4. Merge, push and the e-call roll-out each need the owner's yes.

## 13. Out of scope

- A scheduled inbox; a board across projects; multiple people or accounts.
- Editing tickets from the panel or the page. Decisions go through chat and `queue.py`.

## 14. Removal

If fewer than 5 decisions are closed through the queue in the first two weeks, remove it: the skills, `queue.py`, the panel and the hook rule. The count is `decision:` lines in `queue/tickets/` dated in that window.

## 15. Risks

- Index lock contention between sessions: retry, then a clear failure; never a commit without paths.
- The panel's file-time check on slow disks: a 15 s delay is accepted.
- A session ignores the rule and asks only in chat: the question is still answered live, it just leaves no ticket. The two-week count shows whether this happens.
- The original and the e-call copy have diverged (`modularize` exists only in the copy); `sync` keeps the copy's additions, checked during roll-out.
