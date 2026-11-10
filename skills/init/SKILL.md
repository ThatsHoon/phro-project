---
name: init
description: "Brings this plugin into a project for the first time: the project's own copy of the plugin, a queue of the owner's decisions and the queue panel. Run it once per project, when you type it."
disable-model-invocation: true
---

# init

Output: the project works from its own copy of this plugin, has a decision queue at `<root>/queue/` and the queue panel beside the chat.

`<root>` is the folder the session was opened in. `Q` below is `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/queue.py`.

1. Run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <root>` and do what it prints. If it switched the project to a new copy, `CLAUDE_PLUGIN_ROOT` below is still this plugin; the copy holds the same scripts.
2. Run `Q init <root>`. It prints `OK\tinit\t<path>`, with `exists` when the queue was already there.
3. Turn on the queue panel for this project only. `<copy>` is the folder step 1 printed (the project's own copy), or `${CLAUDE_PLUGIN_ROOT}` when step 1 printed `THIS` or `ORIGINAL`. From `<root>` run:
   - `claude plugin marketplace add <copy>/mods/queue-panel --scope local`
   - `claude plugin install queue-panel@queue-panel --scope local`
   A line saying it is already added or installed is fine. Then tell the owner to type `/reload-plugins`; the panel opens in every new session of this project.
4. Tell the owner, in their language: the queue path, that sessions now file decisions there, that `/inbox` walks them one at a time, and that the `show` skill opens the board page.

Never put anything from the project into this plugin's own files. The queue lives in the project.
