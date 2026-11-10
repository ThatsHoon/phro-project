---
name: inbox
description: Walks the project's queue of decisions waiting on the owner, one ticket at a time, highest priority first, records each answer in git and tells the waiting session. Runs when you type it, optionally with a ticket id.
disable-model-invocation: true
---

# inbox

Output: each waiting ticket decided or left as it was, every decision committed in `<root>/queue/`, the inbox page current.

`<root>` is the folder the session was opened in. `Q` below is `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/queue.py`. If `<root>/queue/` does not exist, say so and offer the `init` skill; stop.

1. Run `Q board <root>`. Republish `<root>/queue/inbox.html` with the Artifact tool to the URL in `<root>/queue/artifact.txt` (load the artifact-design skill first).
2. Read `<root>/queue/board.json`. Take the tickets with status `needs-owner` in the order listed there (priority, then oldest), or only the one named in the argument. If there are none, say so in one line and stop.
3. For one ticket, read its file in `<root>/queue/tickets/` and show, in the owner's language: the title, the question, the options with the recommended one first, the session that is waiting and how long. One ticket per message, nothing else in it.
4. On the owner's answer:
   - A decision: run `Q decide <root> <id> --text "<the owner's answer, verbatim>"`.
   - A priority change only ("make this 1"): run `Q move <root> <id> needs-owner --priority <n>`, then show the next ticket.
   - "Delete", "drop" or "no longer needed" ("삭제", "필요 없어"): run `Q drop <root> <id>`, then deliver as in step 5.
   - "Skip" or "later": leave the ticket and show the next one.
   - "Stop": stop.
5. After a decision or a drop, run `Q deliver <root> <id>`. It prints `SEND<TAB><target><TAB><message>`: send that message, unchanged, to that target with the session messaging tool. If the tool refuses the target or `deliver` is refused, send nothing; the owner session's hook pulls the decision on its next prompt. Send nothing else.
6. Go back to step 3 with the next waiting ticket. When none are left, rerun step 1 and report how many were decided, skipped and left.

Never decide for the owner, never edit a ticket by hand, and never paste a ticket's body into a message to another session.
