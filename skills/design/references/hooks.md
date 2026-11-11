# Building a hook (design row "Hook")

Facts from code.claude.com/docs/en/hooks, read 2026-11-11. When a run disagrees with one, trust the run and fix this file.

## Contract

- Input is one JSON object on stdin. Every event has `session_id`, `transcript_path`, `cwd` and `hook_event_name`. UserPromptSubmit adds `prompt`. SessionStart adds `source` (`startup`, `resume`, `clear`, `compact`, `fork`), which is what its matcher matches.
- On SessionStart and UserPromptSubmit, plain stdout at exit 0 becomes context. The JSON form is `hookSpecificOutput` with `hookEventName` and `additionalContext`, cut at 10,000 characters. Use one form per hook.
- Exit 0 applies the output. Exit 2 stops the action on a blocking event (PreToolUse, UserPromptSubmit, Stop, SubagentStop, PreCompact and a few more), with stderr as the reason. Any other code is a non-blocking error the user sees.
- Hooks on one event run in parallel, in no stated order. One hook never relies on another's output.
- Every command hook gets a `timeout` in seconds; without one it can hold the session for the default (`check` flags it). Slow work that decides nothing runs with `"async": true`.
- A Stop or SubagentStop hook that blocks (`{"decision": "block", "reason": "..."}`) does nothing when `stop_hook_active` is true, or it loops.
- PreToolUse decides through `hookSpecificOutput.permissionDecision`: `allow`, `deny`, `ask` or `defer`.
- A matcher on a plugin's MCP tool uses the full `mcp__plugin_<plugin>_<server>__<tool>` name; the short form never fires.
- A `type: "prompt"` hook has a model judge the event, such as "is the draft done" on Stop. It costs a model call every time, so use it only after a standing rule has failed route-test twice.

## Failure

- A hook that adds context fails open: any exception exits 0 and prints nothing, so a broken hook never blocks a session.
- A hook that blocks says in its docstring whether it fails open or closed, and why.
- stdout is the hook's output. Logs go to stderr or a file.

## Test before calling it built

- Run the exact command from `hooks.json` as a subprocess with a recorded event, with empty stdin and with broken stdin. When it reads or writes the config folder, point `CLAUDE_CONFIG_DIR` at a temp folder. Assert exit 0, the output form, and a run time under the timeout. `tests/test_lean.py` does all of this.
- Measure what it adds per event, in characters, beside the other hooks on that event: every session pays for all of them.
- These tests are a stand-in. Verified means a real session showed the effect.
