# Building an MCP server (design row "MCP")

Facts from code.claude.com/docs/en/mcp and the plugin reference, read 2026-11-11. When a run disagrees with one, trust the run and fix this file.

## Place and run

- Declare it in the copy's `.mcp.json`, or `mcpServers` in `plugin.json`. Reach files through `${CLAUDE_PLUGIN_ROOT}` and keep state under `${CLAUDE_PLUGIN_DATA}`.
- A project's server runs over stdio on this machine, since project files must not leave it (step 4).
- stdout carries the protocol only. Logs go to stderr or a file.
- A server fetched at start (`npx`, `uvx`, `bunx`, `pipx`) is pinned to the exact version checked in step 4; `check` flags `mcp-unpinned`.

## Tools

- A tool is named `mcp__plugin_<plugin>_<server>__<tool>`. A skill, a standing rule or a hook matcher names it in full.
- Tool definitions load on demand through tool search by default, so listing costs little. The description is what search and the model read: say what the tool returns and what it does not do, and when two tools are alike, have each name the other.
- Return the least the model needs, since a result stays in context for the rest of the session: filter to what can be acted on, cap the size, and hand back a reference or a file path with a narrower follow-up tool instead of a whole document. A page read as raw HTML costs tens of thousands of tokens; its interactive elements, a few hundred.
- Tighten inputs with enums and bounds. Return an error as an `isError` result that says what to do next. Mark tools `readOnlyHint` or `destructiveHint`.
- Past about 15 tools, look for tools to merge.

## Test before calling it built

- A standard-library test starts the server, sends `initialize`, `tools/list` and one `tools/call` over stdio, and asserts the tool names, schemas and result.
- That test is a stand-in. Verified means `/mcp` showed the server connected in a real session and a real call did its job.
- Whether the model picks the tool is measured by route-test cases with `"tool"`. Without that run, say it is not verified.
