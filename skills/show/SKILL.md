---
name: show
description: Opens the project's board page beside the chat, with the decisions waiting on the owner, running work and a rough map of the core documents (final goal, drift, each document's headings and first sentence). Use when the user types "phro-project show" or asks to see the project board, the work queue page or how the core documents are laid out ("프로젝트 보드 보여줘", "핵심 문서 구조 보여줘", "phro-project show").
---

# show

Output: the board page republished at its usual URL and open in front of the user.

`<root>` is the folder the session was opened in. `Q` below is `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/queue.py`. If `<root>/queue/` does not exist, say so and offer the `init` skill; stop.

1. Run `Q board <root>`. It rebuilds `<root>/queue/inbox.html`, including the core documents section when `<root>/docs/core/core.json` exists.
2. Republish `<root>/queue/inbox.html` with the Artifact tool to the URL in `<root>/queue/artifact.txt` (load the artifact-design skill first; the page is already self-contained, so publish the file as it is). If `artifact.txt` is missing, follow step 3 of the `init` skill instead.
3. Open that URL with the Artifact tool's `open` action.
4. Reply in one line: how many decisions are waiting and how many core documents the page shows.
