---
name: design
description: Adds a capability to this project or its plugin, or stops a repeated mistake for good: finds what already exists, decides where it belongs, builds it when told to, and tests that it fires when it should. 스킬로 만들어서 내장, 앞으로 ~할 때 자동으로, 이런 파일 읽는 플러그인 찾아서 설치, 플러그인 기능 추가 설계, 어디에 넣을지, 같은 실수 다시 못 하게 막아.
---

# design

Output: the capability in place and firing at the right moment, with what was measured. If the user only asked where something belongs, a decision of a few lines. If nothing should be built, a short reason. Build only what the user asked for, never on your own idea.

First run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <project root>` and do what it prints: a project works from its own copy of this plugin, never from the original.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`. `<plugin>` is a plugin folder, or a project root, which means the skills in its `.claude`.

1. Run `summary <plugin>` and `check <plugin>` on this project's copy, and on the project root if it has its own `.claude/skills`. Read the parts nearest the new capability.
2. Name the real problem: what failed or repeats in actual work, or what the user says they need, and how a working run would show the fix. If nobody can name one, recommend not building it, and stop.
3. Look for what exists before writing anything, in this order: the project's own standing rules (`docs/core/README.md`, `.claude/rules`), `CLAUDE.md` and the memory index, where a rule may already say it or say the opposite; skills and plugins already installed (`/skill-doctor`, `project.py catalog`), the plugin catalog (the `SearchPlugins` tool if present, `claude plugin marketplace list`), tools on this machine, then packages. A skill only the user can invoke (`disable-model-invocation: true`) cannot be called by the model, and a skill that waits for the user's answers stops an automatic flow. To use its method inside a flow, write a project skill that holds the method: answer what the code and documents can answer, act on it, and leave only the user's decisions for the report.
4. For anything from outside, check before installing: who publishes it, its license, what it runs on this machine, and whether project files leave the machine. A service that receives files needs the user's explicit yes, and data the project restricts never goes there. Name the exact item and version to the user and wait for a yes; pin that version, never `latest`. Install it for this project only: `claude plugin install <plugin>@<marketplace> --scope local`, or `--scope project` to share it through git.
5. Place it at the first row that holds:

| Row | When | Why here |
|---|---|---|
| Don't build | The platform or an installed skill already does it and fires when it should | No upkeep |
| Install | Someone built it, and it passes step 4 | Their upkeep, not yours |
| Extend a part | An existing skill's job, reached by its existing triggers | No new listing cost, no new overlap |
| Standing rule | "When X, do Y", where the model can see X: a draft finished, a file type met, a kind of task | One line, delivered to every session of this project |
| Script | Deterministic steps, with the same output every time | The model calls it instead of reasoning through it |
| Skill with `disable-model-invocation: true` | Judgement is needed, and it moves files, spends money, or the user always types it | No listing cost, and it never fires by accident |
| Skill | Judgement is needed, and a user request or a standing rule can name it | Listed by description, read on demand |
| `paths` on a skill | Only relevant when certain files are in play | Gated without a hook |
| Hook | Must react to an event the model will not act on by itself, such as a tool error. Measured misses come first (`route-test`). A hook cannot see a step like "the draft is done": that is a standing rule. Build it by `references/hooks.md` | Runs every time, so it costs every time |
| MCP | A tool or a live connection that a script run cannot provide, or state shared across calls. Build it by `references/mcp.md` | Costs a server |

5b. A repeated mistake is placed by what enforces the fix, not by cost, since nothing fails when an agent skips prose. It counts once it has happened twice; a written rule broken again is that second time. Read the commits, reverts and corrections that show it, then take the first rung that holds:

| Rung | Fix |
|---|---|
| Architecture | One owner for the state, one supported way to do the task, the wrong path removed or hidden so it fails |
| Permission or hook | A `permissions.deny` entry, or a PreToolUse hook whose message names what to do instead |
| Type, lint or test | A check that fails on the mistake; prove it by running it on a real past instance |
| Written rule | Only for a judgment call no check can make, as one standing rule naming what it guards |

Name the rung in the rule that stays, as compass's writing rules say, and report why each higher rung did not work. When a structural fix lands, the written rule it replaces is deleted on the user's yes: the rule was the symptom.

6. Put it in this project's copy, never in the original:
   - A skill goes in `skills/<name>/SKILL.md` of the copy `ensure` printed, its cases in that copy's `tests/route_cases.json`. The copy loads in place, so `/reload-plugins` makes it live, and `clone.py sync` never touches what the project added. The original stays free of any project, so it can be pushed as it is.
   - A standing rule is one line under the work rules in `docs/core/README.md`, naming the skill exactly, with this copy's name: `After writing a design draft, before reporting, call the Skill tool with <copy>:design-review.` The hook delivers it at every session start and after every compaction, in this project only. Without `docs/core`, use `.claude/rules/<name>.md` with no `paths`. Use `CLAUDE.md` only if the user asks. When a rule with the same condition exists, change that line instead of adding one.
   - A third-party skill keeps its own description. Add a standing rule that names it, since a crowded listing can leave it with only a name.
7. Check the cost. A new skill costs listing tokens on every session and adds a competitor to every other skill, which can break tasks that already worked. The listing holds about 1% of the context window, and when it overflows, the least used skills lose their descriptions first. Name the overlapping description it will sit beside, and say what could be merged instead.
8. Write how it fires before the code, in the language users really type:
   - From a prompt: about 8 cases that should fire it and 8 close misses that should not, in `tests/route_cases.json`, in the format `route-test` uses. `check` counts a skill named there as tested. A case that should fire asks for work of more than one step; a one-step request ("read this file") gets done without any skill.
   - From a step in the work: cases with `after` and `rule`, as `route-test` describes.
   - Mark 3 cases that should fire and 3 that should not with `"split": "test"`, and do not read them while writing the description. `tally --cases` scores them apart; a clearly lower score there means the description was fitted to the other cases.
8b. For a skill that produces something (text, a file, a decision), write what it must produce before the body: `skills/<name>/CONTRACT.md` and 3 to 5 cases in `evals/quality/<name>/`, by `${CLAUDE_PLUGIN_ROOT}/skills/design/references/quality.md`. Agree the contract with the user; after that it changes only on their word. Write the body by the kind of failure the cases show: the output's parts in order when the shape goes wrong, a required slot when something is left out, a condition on something visible when it depends on the case, and a plain prohibition only for a rule skipped under pressure. Boundaries are checked by graders, not repeated as bans in the body.
9. Every sentence of a body becomes an instruction some agent follows: keep only those that change a decision, and give a reason only where judgment needs it, with an example of the failure. Write the description last, from those prompts: what it does in one clause, then the words users really type, in their language. Never list its steps in order, since a model can follow a description in place of the body. In one measurement, adding the user's own verbs to a description moved a skill from 1 of 7 to 6 of 7. Keep the combined description and when_to_use under 1,536 characters; shorter is better, since a description is read every session. The body never repeats it (`check` flags `echo-description`).
10. Build it if the user told you to, and write its standing rule in the same step. A script or hook that runs several steps, or has a side effect such as a push, a deploy or a message, also gets:
   - one JSON line per step start and end, under one run id, with the elapsed time and the error kind; no secrets or private paths in it;
   - a log write that fails never stops the work;
   - an error or refusal line names what is wrong, where, and the next step;
   - a missing, ambiguous or unknown input stops the run before any side effect, never a guessed default;
   - a step with a side effect runs once and is never retried by itself; retries follow a fixed list of error kinds;
   - results go to a new folder each run, written as each step ends, never over an earlier run;
   - in a hook or a stdio MCP server, logs go to stderr or a file, since stdout is what it returns.
   A hook also follows `references/hooks.md` and an MCP server `references/mcp.md`, including their tests.
11. Read `${CLAUDE_PLUGIN_ROOT}/skills/route-test/SKILL.md`, and `${CLAUDE_PLUGIN_ROOT}/skills/quality-test/SKILL.md` when 8b wrote cases. Both spend money, so state each estimate and get a yes, then follow route-test with the existing skills as competitors, and report any existing case that broke. Without that yes, report the capability as built and not measured.

Report in the user's language: what was built, where and what makes it fire, what it produced against its contract, then what was measured in three parts. Verified: run on the real thing, with the command. Stand-in: measured through a substitute, such as stub bodies, a standing rule passed by `append_system_prompt` instead of the hook, or a fixture instead of real data; a stand-in pass never reads as the real thing working. Not verified: each item with the reason.
