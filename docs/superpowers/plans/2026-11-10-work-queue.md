# Work Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A git-backed queue of owner decisions for a project's sessions and forks, with an inbox page, a session rule injected by the compass hook, `init` and `inbox` skills, and a panel mod beside the chat.

**Architecture:** `scripts/queue.py` is the only writer: tickets are Markdown files in `<root>/queue/` (its own git repo), and every write regenerates `board.json` and `inbox.html` and commits only its own paths. Skills and the hook call the script; the panel mod only reads `board.json`.

**Tech Stack:** Python 3 stdlib; Claude Code function-hooks module (TSX); git.

**Spec:** `docs/superpowers/specs/2026-11-10-work-queue-design.md`

## Global Constraints

- Work only in `C:\Users\hunje\phro-project\.worktrees\work-queue`, branch `work-queue`. The main checkout has someone else's uncommitted `scripts/clone.py` and `tests/test_clone.py`; never touch them.
- Commits use the repo's local author (do not pass `--author`). No AI attribution lines. No push.
- Python: stdlib only, run as `python -I -X utf8 <script>`. Tests are scripts: `python -I -X utf8 tests/test_<x>.py` prints `pass` and exits 0.
- Scripts print paths and counts only, never ticket bodies; refusals print `REFUSED\t<reason>` and exit 1.
- Files are UTF-8 without BOM, LF line endings. Write with the Write tool or Python, never PowerShell `Set-Content`/`Out-File`.
- Skills and code comments are English. User-invoked skills carry `disable-model-invocation: true`.
- No project content (names, paths, models of any real project) in skills, tests or examples: the plugin stays project-neutral.
- Ticket states: `new`, `researching`, `ready`, `doing`, `verify`, `done`, `needs-owner`, `blocked`. Priorities 1, 2, 3.
- The rule text and the "close or shrink" question are copied verbatim from the spec (sections 7 and 6).

## Review Focus

1. A hand-edited ticket with broken frontmatter: `board` reports `SKIPPED\t<file>\t<reason>` and builds the board from the rest; never a traceback. Test in Task 1.
2. A title with no ASCII letters (Korean) or two tickets with the same title on one day: ids stay ASCII and unique (`<yyyymmdd>-t<n>` fallback, `-2` suffix). Test in Task 1.
3. A project that is itself a git repo: `init` adds `queue/` to the outer repo's `.git/info/exclude`, so the outer `git status` never shows the queue. Test in Task 1.
4. `board.json` read while being written: writes go to a temp file and `os.replace`, and the panel keeps its last good board when a read fails to parse. Tests in Task 1 (atomic write) and Task 4 (panel).
5. Links written with Windows backslashes or to a missing file: stored as forward-slash paths; a missing link gives no last line, not an error. Test in Task 1.

---

### Task 1: `scripts/queue.py` (tickets, commands, board.json)

**Files:**
- Create: `scripts/queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Produces (CLI, all take `<root>` first after the command):
  - `init <root>`; `ask <root> --title T --question Q --option O [--option O ...] --priority {1,2,3} --reason R --session S [--link P ...] [--ticket ID]`; `decide <root> <id> --text T [--priority N]`; `move <root> <id> <status> [--priority N]` (a move to the same status only changes the priority and is not a return); `done <root> <id>`; `board <root>`.
  - Each successful write prints `OK\t<command>\t<ticket path>` (`init`: `OK\tinit\t<queue path>`).
- Produces (Python, for Tasks 2 and 3): `QUEUE = "queue"`; `board(root: Path) -> dict` (writes `board.json` and calls `render_inbox` if Task 2 defined it, else skips); `read_ticket(path: Path) -> dict` (keys: `id, title, status, waiting_on, priority, priority_reason, session, links, returns, question, options, decisions`); `CLOSE_OR_SHRINK = "Close this ticket or shrink it to one step?"`.
- `board.json` shape: `{"generated": <unix s>, "counts": {<status>: n}, "closed_7d": n, "tickets": [{"id","title","status","priority","session","age_s","returns","last_line"}]}`, tickets sorted by priority then oldest first.

- [ ] **Step 1: Write the failing tests** in `tests/test_queue.py`, each in a fresh temp dir, calling the CLI through `subprocess` with `python -I -X utf8 scripts/queue.py`:
  - `test_init`: `queue/.git`, `queue/tickets/`, `queue/board.json` exist; one commit; with the temp root `git init`-ed first, `queue/` is in the outer `.git/info/exclude` and outer `git status --porcelain` is empty of `queue`.
  - `test_ask_decide`: `ask` makes `tickets/<today>-<slug>.md` with `status: needs-owner`, `waiting_on: owner`; `decide` appends `decision: <today> <text>` and sets `ready`/`agent`.
  - `test_no_reask`: `ask --ticket <id>` with the same `--question` after `decide` prints `REFUSED` and exits 1.
  - `test_third_return`: `move` doing→ready three times (`ask`, `decide`, `move doing`, `move ready` x3): `returns: 3` and the question equals `CLOSE_OR_SHRINK`.
  - `test_parallel`: two `ask` processes started together (`subprocess.Popen` x2): both succeed; `git -C queue show --name-only` for each commit lists its own ticket plus `board.json` (and `inbox.html` if present) and never the other ticket.
  - `test_board`: a `doing` ticket linking `notes/ledger.md` (written as `notes\\ledger.md`) whose last non-empty line is `step 3 done` has `last_line == "step 3 done"`; a link to a missing file gives `last_line == ""`; a 200-char last line is cut to 120.
  - `test_bad_ticket`: a file `tickets/broken.md` with no frontmatter: `board` prints `SKIPPED\t...broken.md\t...`, exits 0, and the other tickets are in `board.json`.
  - `test_ids`: title `산사태` gives an id matching `^\d{8}-t\d+$`; two `ask` with title `Same title` on one day give `<date>-same-title` and `<date>-same-title-2`.
  - `test_atomic`: after `board`, no `*.tmp` remains in `queue/`.
- [ ] **Step 2: Run** `python -I -X utf8 tests/test_queue.py`. Expected: FAIL (no `scripts/queue.py`).
- [ ] **Step 3: Implement `scripts/queue.py`.** Frontmatter is the flat `key: value` block of spec section 5 (`links` as a JSON list); parse it by hand, no YAML. Commits: `git -C <queue> add -- <paths>` then `git -C <queue> commit -q -m "<command> <id>" -- <paths>`; on `index.lock` retry 10 times with 0.5 s sleeps, then `REFUSED\tgit index locked: <lock path>`. Generated files are written to `<name>.tmp` then `os.replace`. A return is a move from `doing` or `verify` to `ready` or `needs-owner`. Age comes from the ticket file's first commit time, falling back to its mtime.
- [ ] **Step 4: Run** `python -I -X utf8 tests/test_queue.py`. Expected: `pass`.
- [ ] **Step 5: Run the existing suite:** `python -I -X utf8 tests/test_compass.py`, `tests/test_project.py`, `tests/test_restructure.py`. Expected: each `pass` (record and leave alone any failure that also fails on `main`).
- [ ] **Step 6: Commit** `git add scripts/queue.py tests/test_queue.py` and `git commit -m "Add the work queue script"`.

### Task 2: `inbox.html`

**Files:**
- Modify: `scripts/queue.py` (add `render_inbox(root: Path, data: dict) -> Path`, called by `board`)
- Test: `tests/test_queue.py` (add `test_inbox`)

**Interfaces:**
- Consumes: `board(root)` data and `read_ticket(path)` from Task 1.
- Produces: `queue/inbox.html`, a single self-contained page (inline CSS, no scripts, no external URLs), which the skills publish as a private Artifact.

- [ ] **Step 1: Write `test_inbox`:** two `needs-owner` tickets (priorities 2 and 1) and one `doing`: `inbox.html` exists, the priority-1 title appears before the priority-2 title, each waiting ticket shows its question and options, the `doing` ticket appears under a running section with its `last_line`, a title containing `<script>` appears escaped (`&lt;script&gt;`), and the file has no `http://` or `https://`.
- [ ] **Step 2: Run** the test file. Expected: FAIL in `test_inbox`.
- [ ] **Step 3: Implement `render_inbox`.** Sections in the panel's order (spec section 10): waiting on the owner, running, attention (blocked, 3 returns), done in 7 days. `<title>Work queue</title>`; colours as `:root` tokens with a `prefers-color-scheme: dark` block guarded by `:root:not([data-theme="light"])` and a `:root[data-theme="dark"]` block; explicit `body` background; readable at 375 px with 16 px side gutters. Escape every ticket field with `html.escape`.
- [ ] **Step 4: Run** the test file. Expected: `pass`.
- [ ] **Step 5: Commit** `git commit -m "Render the inbox page" -- scripts/queue.py tests/test_queue.py`.

### Task 3: Session rule, `init` and `inbox` skills, version

**Files:**
- Modify: `scripts/compass.py` (`hook()` appends the rule)
- Modify: `tests/test_compass.py` (add the rule cases)
- Create: `skills/init/SKILL.md`, `skills/inbox/SKILL.md`
- Modify: `.claude-plugin/plugin.json` (version `0.3.0`, description adds "and keeps a queue of the owner's decisions"), `README.md` (one section: the queue, `init`, `/inbox`, the panel, removal condition)

**Interfaces:**
- Consumes: the `queue.py` CLI from Task 1.
- Produces: `QUEUE_RULE` constant in `compass.py`, the exact text of spec section 7 with `queue.py` written as `python -I -X utf8 <plugin root>/scripts/queue.py <root>`.

- [ ] **Step 1: Add tests to `tests/test_compass.py`:** run `compass.py hook` with `CLAUDE_PROJECT_DIR` at a temp root (a) with neither `docs/core` nor `queue/`: empty output; (b) with only `queue/board.json`: output contains `open a ticket with` and the absolute queue.py path; (c) with `docs/core` set up as the existing tests do and `queue/`: index text then the rule, in that order.
- [ ] **Step 2: Run** `python -I -X utf8 tests/test_compass.py`. Expected: FAIL on (b).
- [ ] **Step 3: Implement** in `hook()`: after `session_text`, if `<root>/queue/board.json` exists, append `QUEUE_RULE` (joined with a blank line; alone when the index text is empty). Failures stay silent, as now.
- [ ] **Step 4: Run** `python -I -X utf8 tests/test_compass.py`. Expected: `pass`.
- [ ] **Step 5: Write `skills/init/SKILL.md`** (`name: init`, user-invoked): spec section 8 steps 1 to 5, with the exact commands; step 3 says to publish `queue/inbox.html` with the Artifact tool as a private page (load the artifact-design skill first), and write the returned URL to `queue/artifact.txt` and commit it with `git -C queue commit -- artifact.txt`; step 4 points to Task 5's install instructions in README. Under 60 lines.
- [ ] **Step 6: Write `skills/inbox/SKILL.md`** (`name: inbox`, user-invoked, argument `[id]`): spec section 9 steps 1 to 4, with the exact commands; one ticket per message; the owner's answer is passed verbatim to `decide --text`; the session message is sent only to a session that the session tools list as existing, and its text is exactly `ticket <id> decided, read the ticket file <path>`; a priority change without a decision ("make this 1") is `move <root> <id> needs-owner --priority 1`. Under 60 lines.
- [ ] **Step 7: Bump** `plugin.json` to `0.3.0` and add the README section.
- [ ] **Step 8: Run** every `tests/test_*.py`. Expected: each `pass` except failures recorded in Task 1 Step 5.
- [ ] **Step 9: Commit** `git commit -m "Inject the queue rule and add the init and inbox skills" -- scripts/compass.py tests/test_compass.py skills/init skills/inbox .claude-plugin/plugin.json README.md` (add the new files first).

### Task 4: Panel mod (built in the session mods folder)

**Files:**
- Create in `C:\Users\hunje\.claude\dev-mods\<session id>\queue-panel\`: `.claude-plugin/plugin.json` (`name: queue-panel`, `version: 0.1.0`, `types: ./types/index.d.ts`), `hooks/hooks.json` (`{"modules": ["./register.tsx"]}`), `hooks/register.tsx`, `types/index.d.ts`, `hooks/pane.test.tsx`
- Copy afterwards to the worktree as `mods/queue-panel/` (same files)

**Interfaces:**
- Consumes: `queue/board.json` shape from Task 1, read with `$.fs.read("queue/board.json")` relative to the working directory.
- Produces: pane id `queue-panel`; command `/queue-panel`; state `queuePanel.board` (last good board or `null`) and `queuePanel.mtime`.

- [ ] **Step 1: Read** the plugin-authoring skill's `examples/pane.tsx`, `pane-state.d.ts`, `pane.test.tsx`, `band.tsx` and `tool-call.ts`, and grep the types file for `Pane: {`, `fs: {`, `clock: {`, `fill:`, `status(`, `toast(`.
- [ ] **Step 2: Write `pane.test.tsx`** seeding `queue/board.json` with two waiting tickets (priorities 1 and 2), one `doing` with `last_line`, one `blocked`, `closed_7d: 4`: the tree shows the four sections in order; the [answer] button's handler calls `$.prompt.fill` with `/inbox <id>` and no `submit`; the status line reads `waiting on you: 2`; replacing the file with one new priority-1 ticket gives exactly one toast, a new priority-2 ticket none; replacing it with invalid JSON keeps the previous board drawn.
- [ ] **Step 3: Run** `claude plugin test <mod folder>`. Expected: FAIL (no module).
- [ ] **Step 4: Implement `register.tsx`.** `session.start`: if `queue/board.json` exists, load it, `$.ui.open({ id: "queue-panel", title: "Work queue" })`, register `/queue-panel`, start `$.clock.every` 15 000 ms checking `$.fs.stat` mtime and rereading on change. `tool.call` on `Bash` and `PowerShell`: after `next(e)`, reread when the command contains `queue.py`. `ui.render` on `{ component: 'Pane', requestId: 'queue-panel' }` draws the four sections with elements from `$.ui.resolve(e)`. Layout per spec section 10.
- [ ] **Step 5: Run** `claude plugin validate <mod folder>`, `tsc -p <mod folder>` (once types are laid) and `claude plugin test <mod folder>`. Expected: no refusals, no type errors, tests pass.
- [ ] **Step 6: Live check (needs the owner):** with a test queue in a scratch project, the owner turns the mods on when asked; the pane appears, a `queue.py ask` run updates it within 15 s, [answer] fills the prompt. Record what was seen.
- [ ] **Step 7: Copy** the mod files to `mods/queue-panel/` in the worktree (without `.claude-plugin/types/`) and commit `git commit -m "Add the queue panel mod" -- mods/queue-panel` (add first).

### Task 5: Ship the panel with the plugin

**Files:**
- Modify: `hooks/hooks.json` (try `"modules": ["../mods/queue-panel/hooks/register.tsx"]` beside `"hooks"`), or keep `mods/queue-panel/` as a sibling plugin
- Modify: `README.md` (how the panel is installed), `skills/init/SKILL.md` step 4

- [ ] **Step 1: Try one plugin:** add the module entry to the plugin's `hooks/hooks.json` and run `claude plugin validate <worktree>`. Expected either: accepted (then also run `claude plugin test` on the plugin) or refused.
- [ ] **Step 2: If refused,** revert the hooks.json change and keep `mods/queue-panel/` as a sibling plugin. Find the supported way to install a local plugin folder for one project (`claude plugin --help`, the plugin-authoring reference on `--plugin-dir` and `CLAUDE_CODE_PLUGIN_DIRS`), try it on a scratch project, and write that exact command into `skills/init/SKILL.md` step 4 and the README.
- [ ] **Step 3: Run** every `tests/test_*.py`. Expected: as in Task 3 Step 8.
- [ ] **Step 4: Commit** `git commit -m "Ship the queue panel with the plugin" -- <changed files>`.

### Task 6: Roll-out (each step needs the owner's yes; not for subagents)

- [ ] Merge `work-queue` into `main` (owner's yes).
- [ ] In the project's copy: `python -I -X utf8 <copy>/scripts/clone.py sync`; check that the copy's own additions are still there.
- [ ] Run the `init` skill on the project.
- [ ] In the project's foreman, where it stops for new data or a direction, call `queue.py ask` with the plan line as `--link`.
- [ ] Push (owner's yes).
- [ ] Two weeks later: count `decision:` lines dated in the window; fewer than 5 means removal (spec section 14).
