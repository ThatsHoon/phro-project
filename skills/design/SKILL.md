---
name: design
description: Before adding a plugin feature, decides where it belongs (skill, script, hook, MCP, existing part, or nothing) and writes trigger prompts first. 플러그인 기능 추가 설계, 어디에 넣을지, 스킬로 만들지 스크립트로 만들지.
---

# design

Output: a decision of a few lines, then trigger prompts. No universal template: each project's plugin has its own shape, so read this one first.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`.

1. Run `summary <plugin>` and `check <plugin>`. Read the parts nearest the new capability.
2. Name the real problem: what failed or repeats in actual work, and how a working run would show the fix. If nobody can name one, recommend not building it, and stop.
3. Place it at the first row that holds:

| Row | When | Why here |
|---|---|---|
| Don't build | The platform, an existing skill or a plain instruction already covers it | No upkeep |
| Extend a part | An existing skill's job, reached by its existing triggers | No new listing cost, no new overlap |
| Script | Deterministic steps, with the same output every time | The model calls it instead of reasoning through it |
| Skill with `disable-model-invocation: true` | Judgement is needed, and it moves files, spends money, or the user always types it | No listing cost, and it never fires by accident |
| Skill | Judgement is needed, and a user request can name it | Listed by description, read on demand |
| `paths` on a skill | Only relevant when certain files are in play | Gated without a hook |
| Hook | Must react to an event the model will not act on by itself, such as a tool error. Measured misses come first (`route-test`) | Runs every time, so it costs every time |
| MCP | A tool or a live connection that a script run cannot provide, or state shared across calls | Costs a server |

4. Check the cost. A new skill costs listing tokens on every session and adds a competitor to every other skill, which can break tasks that already worked. Name the overlapping description it will sit beside, and say what could be merged instead.
5. Write trigger prompts before the code: about 8 that should fire it and 8 close misses that should not, in the language users really type. Save them in the plugin as `tests/route_cases.json`, in the format `route-test` uses. `check` counts a skill named there as tested.
6. Write the description last, from those prompts: what it does, then the words users really type, in their language. In one measurement, adding the user's own verbs to a description moved a skill from 1 of 7 to 6 of 7. Keep the combined description and when_to_use under 1,536 characters; shorter is better, since a description is read every session.
7. After building, read `${CLAUDE_PLUGIN_ROOT}/skills/route-test/SKILL.md` and follow it with the existing skills as competitors, and report any existing case that broke.

Report in the user's language. Mark as inference anything you did not measure.
