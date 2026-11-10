---
name: show
description: Opens the project's board page beside the chat, with the decisions waiting on the owner, running work and a rough map of the core documents (final goal, drift, each document's headings and first sentence). Use when the user types "phro-project show" or asks to see the project board, the work queue page or how the core documents are laid out ("프로젝트 보드 보여줘", "핵심 문서 구조 보여줘", "phro-project show").
---

# show

Output: the board page served from this machine and open in the browser pane.

`<root>` is the folder the session was opened in. `<script>` is `${CLAUDE_PLUGIN_ROOT}/scripts/queue.py` with forward slashes. If `<root>/queue/` does not exist, say so and offer the `init` skill; stop.

The page is `${CLAUDE_PLUGIN_ROOT}/ui/board.html`. `queue.py serve` hands it out on 127.0.0.1 and fills it on every refresh from `<root>/queue/` and `<root>/docs/core/`; answers and drops on the page go through the same `decide` and `drop` commands as the command line. Nothing is published.

1. Make sure `<root>/.claude/launch.json` has a configuration named `work-queue`. Add it when missing, keeping any other configurations as they are:
   `{"name": "work-queue", "runtimeExecutable": "python", "runtimeArgs": ["-I", "-X", "utf8", "<script>", "serve", "<root>", "--port", "8765"], "port": 8765}`.
   If the entry exists but names another `<script>` (an older copy), change only that path.
2. Start it with `preview_start {name: "work-queue"}`.
3. Read the first line with `preview_logs`. `SERVING<TAB><url>` or `RUNNING<TAB><url>` with another port than 8765 means 8765 was taken: open that url with `navigate`. Without the browser pane (a terminal session), give the owner the url to open.
4. Reply in one line: how many decisions are waiting and how many core documents the page shows (read `<root>/queue/board.json` and `<root>/docs/core/core.json`).
