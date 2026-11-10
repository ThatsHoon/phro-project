# lean and grill: built-in working style and interview skills

## Goal

Replace three outside plugins with two skills that live in phro-project. The three plugins are ponytail (minimal code), i-have-adhd (answers shaped for action) and mattpocock grilling (design interviews). The user removes the originals afterwards.

- `phro-project:lean` fuses ponytail and i-have-adhd into one ruleset. It is on in every session by default. `phro-lean off` turns it off for the current session and its subagents. `phro-lean on` turns it back on.
- `phro-project:grill` is a fork of grilling. It fixes the weak points found when the three were compared.

Success:

- With the originals disabled, sessions behave as they do now. This covers code built minimally, answers that lead with the action, and subagents that get the build rules.
- Each known weak point is fixed by a stated rule.
- Each session start injects at most 6000 bytes instead of about 14 KB.

## Decisions (user, 2026-11-10)

1. Names: `lean` and `grill`, inside phro-project.
2. The ponytail core is forked, and ponytail-review is forked as `phro-project:lean-review`, an on-demand over-engineering review. ponytail-review was added at the user's word on 2026-11-10, after it showed 89 calls in 30 days. ponytail-audit, -debt, -gain and -help are dropped. The debt harvest becomes one grep line in the rules.
3. There is one intensity. lite, full, ultra and the mode-tracking hook are gone.
4. Subagents get the build rules plus output-saving report rules. They do not get the answer-shaping rules.
5. `phro-lean on|off` switches the mode. The default is on, and off applies to the current session only. A global off is out of scope, because the user runs several forks at once.
6. The new hook stays silent while either original plugin is enabled. It takes over the moment the user disables them.
7. grill changes:
   - a one-line rationale for each recommended answer
   - the design tree kept in a file
   - at most 5 questions a round
   - a one-line question when the open decisions are few and cheap to reverse

## Precedence

Stated in the ruleset, highest first:

1. The user's own words and CLAUDE.md, including language rules.
2. The harness and system prompt.
3. Process skills in use, such as superpowers TDD, brainstorming and plans. Where a test-scope or test-first rule applies, it beats lean's "one runnable check" rule.
4. lean.

## Files

| File | Holds |
|---|---|
| `skills/lean/SKILL.md` | The ruleset, the only copy. Sections `## Build`, `## Answer`, `## Report`, `## Switch`. |
| `skills/grill/SKILL.md` | The grill interview. |
| `scripts/lean.py` | The one hook entry for three events. Standard library only. |
| `hooks/hooks.json` | Gains three entries. The compass SessionStart entry stays as it is. |
| `tests/test_lean.py` | Assert-based checks, run as `python -I -X utf8 tests/test_lean.py`, printing `pass`. |
| `THIRD_PARTY_NOTICES.md` | MIT notices: ponytail (c) 2026 DietrichGebert; i-have-adhd (c) 2026 Ayoub Ghriss; grilling (c) 2026 Matt Pocock. |
| `README.md`, `.claude-plugin/plugin.json` | A README section for both skills. Version 0.5.0. |

## lean ruleset content

Written in English, as plain rules without persona or role-play.

### Build (main session and subagents)

- Read the task and every file the change touches, and trace the real flow, before choosing a solution. Minimal applies to the solution, never to the reading.
- Take the first rung that holds:
  1. Is the change needed at all?
  2. Does it already exist in this codebase?
  3. Does the standard library do it?
  4. Does a native platform feature cover it?
  5. Does an installed dependency solve it?
  6. Can it be one line?
  7. Only then, write the minimum code that works.
- For a bug, fix the root cause. Grep every caller of the function before editing it, and fix the shared place once.
- No unrequested abstractions, scaffolding or configuration. Prefer deletion over addition and boring code over clever code. Use the fewest files.
- When a request is complex, ship the minimal version and name the fuller one in one line in the same response.
- When two options are the same size, take the one that is correct on edge cases.
- A deliberate corner with a known ceiling gets a `lean:` comment naming the ceiling and the upgrade path. Existing `ponytail:` comments mean the same thing. `grep -rn "lean:\|ponytail:"` lists them.
- Never simplify away:
  - validation at trust boundaries
  - error handling that prevents data loss
  - security
  - accessibility basics
  - anything the user asked for
  - a calibration knob for physical hardware
- Non-trivial logic leaves one runnable check behind, the smallest one that fails if the logic breaks. Where a test-first process or the plan names tests, follow that instead.
- After code, write at most three lines on what was skipped and when to add it. When the user asked for explanation, give it in full.

### Answer (main session only)

- The first line is the next action. When the user asked a question, the first line is the answer.
- Write multi-step work as a numbered list of single actions, with the fewest steps that work.
- When something is left open, end with one concrete next action. Ask a question at most once. Do not re-append a question the user has not answered; if it still blocks work, name it once in the state line.
- Keep to one topic. Offer a second issue once, at the end, as a separate question.
- Restate the state on multi-step work, for example "step 3 of 5 done". Use the task tool when the harness has one.
- Give time estimates in concrete units.
- Show completed work concretely: what now works and how to try it.
- State errors as cause and fix, without alarm words.
- Show at most 5 items per group, ranked. This limits presentation only, never analysis, search or retained items.
- No preamble, no recap after a finished task, no closing pleasantries, no hedges that carry no real uncertainty, no idioms.
- These rules give way in six cases:
  - The user asks to explain or walk through something: answer fully, with headers.
  - Before a destructive action: confirm first.
  - After three failed turns: name the assumption that may be wrong and ask one diagnostic question.
  - On real ambiguity: ask one short question.
  - When a rule would delete the answer itself, such as a list of options: the task wins.
  - The harness or system prompt outranks these rules.
- Before sending, check that a reader who sees only the first and last lines knows what happened and what to do next.

### Report (subagents only)

- The first line is the status.
- Long output goes to a file, and the reply gives the path.
- Do not restate the brief.
- Quote only the log lines needed as evidence.
- No progress narration.
- Keep the reply within 20 lines, unless the brief asks for more.
- Once you return, you are finished: no background waits and no re-sending the report.

### Switch

One line stating that `phro-lean off` stops these rules for the session and `phro-lean on` brings them back. This line goes in the main injection only.

## lean.py behaviour

`python -I -X utf8 lean.py hook` reads the hook JSON from stdin. It uses the fields `hook_event_name`, `session_id`, `cwd` and `prompt`.

| Event | Output |
|---|---|
| SessionStart (startup, resume, clear, compact) | Build, Answer and Switch as plain stdout, behind a one-line header saying lean is on. When this session is off, it prints nothing. On startup it also removes state files older than 7 days. |
| SubagentStart | `{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": <Build + Report>}}`. When the parent session is off, it prints nothing. |
| UserPromptSubmit | The prompt is matched, case-insensitively, against `^\s*/?(phro-lean\|phro-project:lean)\s+(on\|off)\s*$`. On `off`, it creates the state file and prints one line telling the model to stop applying the lean rules in this session. On `on`, it removes the file and prints the full main injection, because a compact while off left no rules in context. Any other prompt exits at once with no output. |

- **State.** The file `<config>/phro-lean/off/<session_id>` marks a session as off. `<config>` is `CLAUDE_CONFIG_DIR` or `~/.claude`. Any `session_id` that is not a plain token (`[A-Za-z0-9_-]+`) is treated as on, and no file is written for it.
- **Guard.** The hook reads `enabledPlugins` from `<config>/settings.json`, then from `<cwd>/.claude/settings.json` and `<cwd>/.claude/settings.local.json`; later files override earlier ones. If a key starting with `ponytail@` or `i-have-adhd@` resolves to true, SessionStart and SubagentStart print nothing. The toggle still works and adds that the original plugin is still enabled.
- **Section extraction.**
  - The frontmatter is stripped.
  - A section runs from its `## ` heading to the next `## ` heading.
  - A missing section yields nothing rather than an error.
  - A section is never reworded at injection time.
- **Failure.** Any exception exits 0 with no output, so a session start or prompt is never blocked. Input that does not parse counts as on, with an empty session id.
- **Hooks.** Each new hooks.json entry calls `python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/lean.py" hook` with a timeout of 5 seconds. The SessionStart matcher is `startup|resume|clear|compact`. SubagentStart and UserPromptSubmit have no matcher.

## lean SKILL.md frontmatter

The description covers explicit use only: the user says `phro-lean`, asks what lean mode does, or asks to switch it. The rules arrive through the hook, so the skill does not need to trigger on coding tasks.

## grill content

The grilling mechanics carry over unchanged:

- design tree, rounds and frontier
- numbered questions, each with a recommended answer, worded so "yes" accepts it
- facts go to sub-agents, decisions go to the user
- the session is done when the frontier is empty and the user confirms

Changes:

1. Each recommended answer carries one line of rationale, a reason drawn from facts found, not a restatement.
2. The tree goes in a file: the session scratchpad when the system prompt names one, else the OS temp dir. The file is named `grill-<topic>.md`. It holds settled decisions with their answers, open questions and running fact lookups, and it is rewritten after every round. The path is named in chat once. After a compaction, read it before asking the next round.
3. A round holds at most 5 questions, most blocking first. The rest wait for the next round.
4. When at most 2 decisions remain and both are cheap to reverse, ask them in one line each, without the round format.
5. When the user accepts a whole round with one word and a decision in it is costly to reverse, restate that decision's cost in one line before acting on it.

The frontmatter keeps the trigger phrases of grilling and grill-me, such as "grill me" and "stress-test this plan", so the user can call it the same way.

## Tests (`tests/test_lean.py`)

Each test runs `lean.py` as a subprocess with `CLAUDE_CONFIG_DIR` set to a temp directory and a temp `cwd`.

1. SessionStart prints the Build, Answer and Switch text and none of Report. The output is at most 6000 bytes and starts after the frontmatter.
2. SubagentStart prints valid JSON with the event name and with Build and Report, without Answer. The context is at most 4000 bytes.
3. The sequence works: `phro-lean off`, then SessionStart prints nothing, SubagentStart prints nothing, then `/phro-lean on` reinjects. Another session id is unaffected throughout.
4. A prompt that merely mentions phro-lean in a sentence changes nothing and prints nothing.
5. With `{"enabledPlugins": {"ponytail@ponytail": true}}` in the temp config, SessionStart prints nothing. With a project-local override to false, it prints.
6. A non-token session id writes no file. Broken stdin exits 0.
7. The fixed rules are present verbatim in SKILL.md:
   - the do-not-re-append-questions line
   - the answer-first line for questions
   - the test-first precedence line
   - the Report "Once you return" line

Every existing test in `tests/` still passes.

## Rollout

1. Build on branch `lean` in `.worktrees/lean`, then merge to main locally.
2. Sync the e-call copy with `clone.py sync`, so the copy gains the hook. Then `/reload-plugins`. A new session start shows the effect.
   - Check once, in a live session, that SubagentStart carries the parent's `session_id`. Run `phro-lean off`, spawn one subagent, and confirm it got no Build text. If the id is the subagent's own, record that in the troubleshooting doc. Off then cannot reach subagents until a parent id is found in the input.
3. Push only after the user says yes.
4. The originals are removed by the user. This spec does not remove them. mattpocock-skills also carries tdd, research and other skills, and grill-me cannot be turned off alone. Either keep that plugin enabled, since the guard does not look at it, or disable it knowing those other skills go too.

## Out of scope

- A global off switch.
- Intensity levels.
- Porting ponytail-audit, -debt, -gain or -help.
- A status line indicator.
- Changes to karpathy-guidelines or other skills that overlap.
- Detecting two enabled copies of phro-project in one project.
