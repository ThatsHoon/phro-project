---
name: init
description: Brings this plugin into a project for the first time: the project's own copy of the plugin, a queue of the owner's decisions, the first inbox page and the queue panel. Run it once per project, when you type it.
disable-model-invocation: true
---

# init

Output: the project works from its own copy of this plugin, has a decision queue at `<root>/queue/`, a private inbox page, and the queue panel beside the chat.

`<root>` is the folder the session was opened in. `Q` below is `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/queue.py`.

1. Run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <root>` and do what it prints. If it switched the project to a new copy, `CLAUDE_PLUGIN_ROOT` below is still this plugin; the copy holds the same scripts.
2. Run `Q init <root>`. It prints `OK\tinit\t<path>`, with `exists` when the queue was already there. Skip step 3 when `<root>/queue/artifact.txt` already exists.
3. Publish `<root>/queue/inbox.html` with the Artifact tool as a private page (load the artifact-design skill first; the page is already self-contained, so publish the file as it is). Write the URL it returns to `<root>/queue/artifact.txt` as one line, then run `git -C <root>/queue add artifact.txt` and `git -C <root>/queue commit -q -m "inbox page" -- artifact.txt`.
4. Turn on the queue panel for this project only. `<copy>` is the folder step 1 printed (the project's own copy), or `${CLAUDE_PLUGIN_ROOT}` when step 1 printed `THIS` or `ORIGINAL`. From `<root>` run:
   - `claude plugin marketplace add <copy>/mods/queue-panel --scope local`
   - `claude plugin install queue-panel@queue-panel --scope local`
   A line saying it is already added or installed is fine. Then tell the owner to type `/reload-plugins`; the panel opens in every new session of this project.
5. Tell the owner, in their language: the queue path, the page URL, that sessions now file decisions there, and that `/inbox` walks them one at a time.

Never put anything from the project into this plugin's own files. The queue lives in the project.
