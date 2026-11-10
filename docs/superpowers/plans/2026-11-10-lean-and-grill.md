# lean and grill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `phro-project:lean`, an always-on ruleset that fuses ponytail and i-have-adhd and can be switched per session, and `phro-project:grill`, a fork of grilling.

**Architecture:**
- The ruleset lives once, in `skills/lean/SKILL.md`, split into four `## ` sections.
- One script, `scripts/lean.py hook`, handles SessionStart, SubagentStart and UserPromptSubmit. It slices sections out of SKILL.md, keeps one off file per session, and stays silent while the original plugins are enabled.
- grill is a SKILL.md and nothing more.

**Tech Stack:** Python 3 standard library, Claude Code command hooks, Markdown skills.

**Spec:** `docs/superpowers/specs/2026-11-10-lean-and-grill-design.md`

## Global Constraints

- Standard library only. Scripts run as `python -I -X utf8 ...`.
- Skills, scripts and docs are written in English. No AI attribution in commits or files. Use the local git author and no Co-Authored-By trailer.
- The rules come from the spec's "lean ruleset content" section. Write them as plain rules, with no persona and no role-play.
- Byte limits:
  - The SessionStart injection, including its header, is at most 6000 bytes.
  - The SubagentStart `additionalContext` is at most 4000 bytes.
- A hook never blocks: on any exception it exits 0 with no output.
- The state file is `<config>/phro-lean/off/<session_id>`. `<config>` is `CLAUDE_CONFIG_DIR` or `~/.claude`. A session id is a token only if it matches `[A-Za-z0-9_-]+`.
- The toggle pattern is `^\s*/?(phro-lean|phro-project:lean)\s+(on|off)\s*$`, case-insensitive.
- The guard keys are any `enabledPlugins` key starting with `ponytail@` or `i-have-adhd@` that resolves to true. The hook reads `<config>/settings.json`, then `<cwd>/.claude/settings.json`, then `<cwd>/.claude/settings.local.json`; later files override earlier ones.
- Do not touch `scripts/clone.py` or `tests/test_clone.py` in the main checkout. Their changes belong to another session.

## Review Focus

1. A SKILL.md checked out with CRLF line endings must still slice into the same sections. Pinned in Task 1, test `crlf`.
2. A settings file with a UTF-8 BOM, or with broken JSON, must not stop the injection. A BOM file is read; a broken file counts as no plugins enabled. Pinned in Task 1, test `bad_settings`.
3. SubagentStart input with no `session_id` field must inject, since a missing id means on. Pinned in Task 1, test `subagent_no_id`.
4. A prompt where the toggle words sit inside a longer or multi-line text must change nothing. Pinned in Task 2, test `mention_only`.
5. Pruning must never delete an off file younger than 7 days, and must ignore other files in the folder. Pinned in Task 2, test `prune`.

---

### Task 1: lean ruleset and injection

**Files:**
- Create: `skills/lean/SKILL.md`
- Create: `scripts/lean.py`
- Create: `tests/test_lean.py`

**Interfaces:**
- Produces, in `scripts/lean.py`:
  - `PLUGIN = Path(__file__).resolve().parents[1]`
  - `SKILL = PLUGIN / "skills/lean/SKILL.md"`
  - `MAIN = ["Build", "Answer", "Switch"]`
  - `SUB = ["Build", "Report"]`
  - `HEADER = "PHRO-LEAN ON. The rules below apply to every response in this session."`
  - `config_dir() -> Path`
  - `sections(text: str, names: list[str]) -> str`. Strips the frontmatter and normalises `\r\n` to `\n`. Returns the named sections in the order given, each starting with its own `## ` heading and joined by one blank line. A missing name is skipped.
  - `originals_enabled(cwd: str) -> bool`
  - `is_off(session_id: str) -> bool`. Returns False for a non-token id.
  - `main_text() -> str`. Returns `HEADER + "\n\n" + sections(SKILL text, MAIN)`.
  - `handle(event: dict) -> str`. Returns the stdout text; an empty string means no output.
  - `main(argv) -> int`. Its only subcommand is `hook`. It reads JSON from stdin, prints `handle()`, and returns 0 on any exception.
- SKILL.md frontmatter:
  - `name: lean`
  - The description says it is for when the user says `phro-lean`, asks what lean mode does, or asks to turn it on or off. It names the rules as minimal code and answers shaped for action, injected by a hook in every session.

- [ ] **Step 1: Write the failing tests in `tests/test_lean.py`**

  Follow the shape of `tests/test_compass.py`: a `main()` that runs every check and prints `pass`. A helper `run(event: dict | str, config: Path, cwd: Path) -> str` runs `lean.py hook` as a subprocess. It passes `CLAUDE_CONFIG_DIR=config` and sends the event as JSON on stdin; a str event is sent raw. Checks:
  - `session_start`: `{"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": cwd}`.
    - The output starts with `HEADER`.
    - It contains `## Build`, `## Answer` and `## Switch`, but not `## Report`.
    - It does not contain `name: lean`.
    - `len(out.encode()) <= 6000`.
  - `subagent`: `{"hook_event_name": "SubagentStart", "session_id": "s1", "cwd": cwd}`.
    - `json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SubagentStart"`.
    - The context has `## Build` and `## Report`, but not `## Answer`.
    - `len(ctx.encode()) <= 4000`.
  - `subagent_no_id`: the same event with no `session_id` key still injects.
  - `guard`:
    - With `config/settings.json` = `{"enabledPlugins": {"ponytail@ponytail": true}}`, SessionStart and SubagentStart give `""`.
    - Then with `cwd/.claude/settings.local.json` = `{"enabledPlugins": {"ponytail@ponytail": false}}`, SessionStart injects again.
  - `bad_settings`:
    - A `config/settings.json` written with a BOM and `{"enabledPlugins": {"i-have-adhd@i-have-adhd": true}}` blocks injection.
    - A `config/settings.json` of `{not json` does not block it.
  - `crlf`: `sections(text.replace("\n", "\r\n"), MAIN) == sections(text, MAIN)`, where `text` is the real SKILL.md. Import `lean` directly for this.
  - `broken_stdin`: the raw input `"{"` gives exit code 0 and empty output.
  - `fixed_rules`: SKILL.md contains each of these strings verbatim:
    - `Do not re-append a question the user has not answered`
    - `When the user asked a question, the first line is the answer`
    - `Where a test-first process or the plan names tests, follow that instead`
    - `Once you return, you are finished`
    - `grep -rn "lean:\|ponytail:"`

- [ ] **Step 2: Run the tests and watch them fail**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: FAIL. The test errors because `scripts/lean.py` does not exist.

- [ ] **Step 3: Write `skills/lean/SKILL.md`**

  Write the four sections `## Build`, `## Answer`, `## Report` and `## Switch` from the spec's "lean ruleset content". Include the precedence list from the spec's "Precedence" section as the first lines of `## Build`. This puts the precedence in both injections. The `## Switch` line reads exactly:
  ```
  `phro-lean off` stops these rules for this session and its subagents; `phro-lean on` brings them back.
  ```

- [ ] **Step 4: Implement `scripts/lean.py`**

  - `handle` dispatches on `event.get("hook_event_name")`.
  - SessionStart returns `main_text()` unless the guard is on or `is_off`.
  - SubagentStart returns `json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": sections(text, SUB)}})` under the same conditions.
  - Any other event returns `""`. Task 2 adds UserPromptSubmit.
  - Read the settings files with `encoding="utf-8-sig"`. A file that fails to parse counts as empty.

- [ ] **Step 5: Run the tests and see them pass**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: `pass`. Then measure the two sizes and note them for the report:
  ```
  python -I -X utf8 -c "import sys; sys.path.insert(0, 'scripts'); import lean; print(len(lean.main_text().encode()), len(lean.sections(lean.SKILL.read_text(encoding='utf-8'), lean.SUB).encode()))"
  ```

- [ ] **Step 6: Commit**
  ```bash
  git add skills/lean/SKILL.md scripts/lean.py tests/test_lean.py
  git commit -m "lean: ruleset and session and subagent injection"
  ```

### Task 2: per-session switch and hook wiring

**Files:**
- Modify: `scripts/lean.py`
- Modify: `hooks/hooks.json`
- Modify: `tests/test_lean.py`

**Interfaces:**
- Consumes: Task 1's `handle`, `is_off`, `main_text` and `originals_enabled`.
- Produces:
  - `set_off(session_id: str, off: bool) -> bool`. It creates or removes the state file and returns False for a non-token id.
  - `prune(days: float = 7) -> None`. It removes the files in `<config>/phro-lean/off/` whose mtime is older than `days`.
  - `OFF_TEXT = 'PHRO-LEAN OFF for this session: stop applying the lean rules from here on. "phro-lean on" brings them back.'`
  - `GUARD_NOTE = " (ponytail or i-have-adhd is still enabled, so lean injects nothing until they are disabled.)"`

- [ ] **Step 1: Add failing checks to `tests/test_lean.py`**

  - `toggle`:
    1. A UserPromptSubmit with `prompt: "phro-lean off"`, session `s1`, gives `OFF_TEXT`.
    2. SessionStart and SubagentStart for `s1` give `""`.
    3. SessionStart for `s2` still injects.
    4. `prompt: "/phro-lean on"` gives `main_text()`.
    5. SessionStart for `s1` injects again.
    6. `prompt: "/phro-project:lean OFF "` also switches `s1` off.
  - `mention_only`: none of these prompts changes state or prints anything:
    - `"phro-lean off 하면 어떻게 돼?"`
    - `"please run\nphro-lean off"`
    - `"what is phro-lean"`
  - `bad_id`: session id `"../x"` with `phro-lean off` writes no file anywhere under `config`. A later SessionStart with that id injects.
  - `guard_toggle`: with the ponytail guard on, `phro-lean off` returns `OFF_TEXT + GUARD_NOTE`.
  - `prune`:
    1. Create an off file for `old` and set its mtime 8 days back with `os.utime`.
    2. Create an off file for `new`, and a non-session file `notes.txt` in the same folder.
    3. A SessionStart with `source: "startup"` removes `old` and keeps `new`.
    4. `notes.txt` is kept as well.
  - `hooks_json`: `hooks/hooks.json` has:
    - the compass SessionStart entry, unchanged;
    - a SessionStart entry with matcher `startup|resume|clear|compact` running `python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/lean.py" hook` with timeout 5;
    - SubagentStart and UserPromptSubmit entries with the same command and no matcher.

- [ ] **Step 2: Run the tests and watch them fail**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: FAIL at `toggle`. The UserPromptSubmit event returns `""`.

- [ ] **Step 3: Implement the switch**

  - UserPromptSubmit matches the Global Constraints pattern against `prompt`. A miss returns `""` before any file access.
  - `prune` runs only on SessionStart with `source == "startup"`.
  - Treat a file as a session file only if its name is a token. This leaves `notes.txt` alone.

- [ ] **Step 4: Add the three hook entries to `hooks/hooks.json`**

- [ ] **Step 5: Run the tests and see them pass**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: `pass`.

- [ ] **Step 6: Commit**
  ```bash
  git add scripts/lean.py hooks/hooks.json tests/test_lean.py
  git commit -m "lean: per-session phro-lean on/off and hook wiring"
  ```

### Task 3: grill skill

**Files:**
- Create: `skills/grill/SKILL.md`
- Modify: `tests/test_lean.py` (one more check)
- Modify: `tests/route_cases.json`

**Interfaces:**
- SKILL.md frontmatter:
  - `name: grill`
  - The description keeps the triggers of grilling and grill-me: "grill me", "grill this", "stress-test this plan", "interview me about this design", "그릴해", "검수해줘 질문으로". It says the skill interviews the user one design-tree round at a time until shared understanding.

- [ ] **Step 1: Add a failing check `grill_rules` to `tests/test_lean.py`**

  `skills/grill/SKILL.md` exists and contains each of these verbatim:
  - `one line of rationale`
  - `grill-<topic>.md`
  - `at most 5 questions`
  - `When at most 2 decisions remain and both are cheap to reverse`
  - `restate that decision's cost in one line`
  - `Word each question so "yes" accepts`
  - `Finding facts is your job`

- [ ] **Step 2: Run the tests and watch them fail**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: FAIL at `grill_rules`. The file is missing.

- [ ] **Step 3: Write `skills/grill/SKILL.md`**

  Carry over the grilling mechanics from the spec's "grill content", including the round format, and add the five changes it lists. Grilling's own text is at `C:\Users\hunje\.claude\plugins\cache\mattpocock\mattpocock-skills\1.3.1\skills\productivity\grilling\SKILL.md`. Read it and reword it; do not paste it.

- [ ] **Step 4: Add routing cases to `tests/route_cases.json`**

  Use the file's existing object shape. Add:
  - `g01`, skill `grill`: "이 설계 grill 해줘, 빠진 결정 없는지 질문으로 검수해"
  - `g02`, skill `grill`: "stress-test this migration plan by interviewing me"
  - `l01`, skill `lean`: "phro-lean 모드가 지금 뭘 하는 거야?"
  - `n_l1`, `"skill": null` (JSON null): "이 함수 최소한으로 짧게 고쳐줘". The rules arrive through the hook, so the skill must not fire for plain coding.

- [ ] **Step 5: Run the tests and the plugin check**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: `pass`.

  Run: `python -I -X utf8 scripts/project.py check .`
  Expected: no finding that names `lean` or `grill`. Findings that were already there on main are not this task's work. To tell them apart, run the same command in the main checkout `C:/Users/hunje/phro-project` and compare.

- [ ] **Step 6: Commit**
  ```bash
  git add skills/grill/SKILL.md tests/test_lean.py tests/route_cases.json
  git commit -m "grill: design-tree interview with rationale, tree file and round cap"
  ```

### Task 3b: lean-review skill (user addition, 2026-11-10)

**Files:**
- Create: `skills/lean-review/SKILL.md`
- Modify: `tests/test_lean.py`, `tests/route_cases.json`

**Interfaces:**
- SKILL.md frontmatter:
  - `name: lean-review`
  - The description is an over-engineering review of a diff or a named path: it finds what to delete. It keeps ponytail-review's triggers: "review for over-engineering", "what can we delete", "is this over-engineered", "simplify review", "과설계 리뷰", "뭘 지울 수 있어".

- [ ] **Step 1: Add a failing check `lean_review_rules` to `tests/test_lean.py`**

  `skills/lean-review/SKILL.md` contains each of these verbatim:
  - `delete:`
  - `stdlib:`
  - `native:`
  - `yagni:`
  - `shrink:`
  - `net: -<N> lines possible.`
  - `Lean already. Ship.`
  - `Correctness bugs, security holes and performance are out of scope`
  - `Does not apply the fixes`

- [ ] **Step 2: Run the tests and watch them fail**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: FAIL at `lean_review_rules`.

- [ ] **Step 3: Write `skills/lean-review/SKILL.md`**

  Fork ponytail-review (`C:\Users\hunje\.claude\plugins\cache\ponytail\ponytail\4.10.0\skills\ponytail-review\SKILL.md`): the line format `<file>:L<line>: <tag> <what>. <replacement>.`, the five tags, the net score, the empty result, and the boundaries.
  - Reword the text; do not paste it.
  - Keep one example per tag.
  - Drop the ❌ example.
  - Read the whole diff and every caller of a function before suggesting its deletion.
  - When no diff is named, review `git diff` of the working tree, and say so in the first line.
  - A `lean:` or `ponytail:` comment marks a deliberate corner. Flag it only if its stated ceiling has been reached.
  - The one runnable check that lean's Build rules leave behind is never flagged.

- [ ] **Step 4: Add routing cases**

  - `r01`, skill `lean-review`: "이 diff 과설계 리뷰해줘, 지울 수 있는 것만 뽑아"
  - `r02`, skill `lean-review`: "review this PR for over-engineering, what can we delete"

- [ ] **Step 5: Run the tests**

  Run: `python -I -X utf8 tests/test_lean.py`
  Expected: `pass`.

- [ ] **Step 6: Commit**
  ```bash
  git add skills/lean-review/SKILL.md tests/test_lean.py tests/route_cases.json
  git commit -m "lean-review: over-engineering review forked from ponytail-review"
  ```

### Task 4: notices, README, version

**Files:**
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `README.md`
- Modify: `.claude-plugin/plugin.json`

- [ ] **Step 1: Write `THIRD_PARTY_NOTICES.md`**

  It names three sources and what was forked from each:
  - ponytail: MIT, (c) 2026 DietrichGebert
  - i-have-adhd: MIT, (c) 2026 Ayoub Ghriss
  - grilling from mattpocock-skills: MIT, (c) 2026 Matt Pocock

  Each entry carries the full MIT permission text once. Copy the text from the source LICENSE files under `C:\Users\hunje\.claude\plugins\cache\{ponytail\ponytail\4.10.0,i-have-adhd\i-have-adhd\0.4.1,mattpocock\mattpocock-skills\1.3.1}\LICENSE`.

- [ ] **Step 2: Update README and plugin.json**

  - In `README.md`, add a section `### 7. Working style: \`lean\` and \`grill\`` after the modularize section. Cover:
    - what lean injects, and where (session versus subagent);
    - `phro-lean on|off` and that it is per session;
    - that lean stays silent while ponytail or i-have-adhd is enabled;
    - what grill and lean-review do;
    - a pointer to THIRD_PARTY_NOTICES.md.
  - In `plugin.json`, set `"version": "0.5.0"` and append ", and an always-on working style with a design interview" to the description.

- [ ] **Step 3: Run every test**

  Run each `tests/test_*.py` with `python -I -X utf8`.
  Expected: each prints `pass`. `test_modularize` prints `pass` with or without pytest present.

- [ ] **Step 4: Commit**
  ```bash
  git add THIRD_PARTY_NOTICES.md README.md .claude-plugin/plugin.json
  git commit -m "lean, grill: notices, README section, 0.5.0"
  ```

## After the tasks (controller, not a task)

1. Final whole-branch review.
2. Merge `lean` into main locally.
3. With the user's yes, run `clone.py sync` for the e-call copy and `/reload-plugins`.
4. Run the live subagent session-id check from the spec's Rollout section.
5. Ask before pushing.
