---
name: design
description: Adds a capability to this project or its plugin: finds what already exists, decides where it belongs, builds it when told to, and tests that it fires when it should. 스킬로 만들어서 내장, 앞으로 ~할 때 자동으로, 이런 파일 읽는 플러그인 찾아서 설치, 플러그인 기능 추가 설계, 어디에 넣을지.
---

# design

Output: the capability in place and firing at the right moment, with what was measured. If the user only asked where something belongs, a decision of a few lines. If nothing should be built, a short reason. Build only what the user asked for, never on your own idea.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`. `<plugin>` is a plugin folder, or a project root, which means the skills in its `.claude`.

1. Run `summary <plugin>` and `check <plugin>`. Read the parts nearest the new capability.
2. Name the real problem: what failed or repeats in actual work, or what the user says they need, and how a working run would show the fix. If nobody can name one, recommend not building it, and stop.
3. Look for what exists before writing anything, in this order: skills and plugins already installed (`/skill-doctor`, `project.py catalog`), the plugin catalog (the `SearchPlugins` tool if present, `claude plugin marketplace list`), tools on this machine, then packages. A skill only the user can invoke (`disable-model-invocation: true`) cannot be called by the model, and a skill that waits for the user's answers stops an automatic flow. To use its method inside a flow, write a project skill that holds the method: answer what the code and documents can answer, act on it, and leave only the user's decisions for the report.
4. For anything from outside, check before installing: who publishes it, its license, what it runs on this machine, and whether project files leave the machine. A service that receives files needs the user's explicit yes, and data the project restricts never goes there. Name the exact item to the user and wait for a yes. Install it for this project only: `claude plugin install <plugin>@<marketplace> --scope local`, or `--scope project` to share it through git.
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
| Hook | Must react to an event the model will not act on by itself, such as a tool error. Measured misses come first (`route-test`). A hook cannot see a step like "the draft is done": that is a standing rule | Runs every time, so it costs every time |
| MCP | A tool or a live connection that a script run cannot provide, or state shared across calls | Costs a server |

6. Put it in this project, not in this plugin:
   - A skill goes in `<project>/.claude/skills/<name>/SKILL.md`, its cases in `<project>/.claude/tests/route_cases.json`. Never in this plugin's folder or cache: every project would receive it, and an update would overwrite it. A project with its own plugin uses that plugin's folder.
   - A standing rule is one line under the work rules in `docs/core/README.md`, naming the skill exactly: `After writing a design draft, before reporting, call the Skill tool with design-review.` The hook delivers it at every session start and after every compaction, in this project only. Without `docs/core`, use `.claude/rules/<name>.md` with no `paths`. Use `CLAUDE.md` only if the user asks.
   - A third-party skill keeps its own description. Add a standing rule that names it, since a crowded listing can leave it with only a name.
7. Check the cost. A new skill costs listing tokens on every session and adds a competitor to every other skill, which can break tasks that already worked. The listing holds about 1% of the context window, and when it overflows, the least used skills lose their descriptions first. Name the overlapping description it will sit beside, and say what could be merged instead.
8. Write how it fires before the code, in the language users really type:
   - From a prompt: about 8 cases that should fire it and 8 close misses that should not, in `tests/route_cases.json`, in the format `route-test` uses. `check` counts a skill named there as tested.
   - From a step in the work: cases with `after` and `rule`, as `route-test` describes.
9. Write the description last, from those prompts: what it does, then the words users really type, in their language. In one measurement, adding the user's own verbs to a description moved a skill from 1 of 7 to 6 of 7. Keep the combined description and when_to_use under 1,536 characters; shorter is better, since a description is read every session.
10. Build it if the user told you to, and write its standing rule in the same step.
11. Read `${CLAUDE_PLUGIN_ROOT}/skills/route-test/SKILL.md`. It spends money, so state the estimate and get a yes, then follow it with the existing skills as competitors, and report any existing case that broke. Without that yes, report the capability as built and not measured.

Report in the user's language: what was built, where, what makes it fire, and what was measured. Mark as inference anything you did not measure.
