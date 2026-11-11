---
name: route-test
description: Measures whether a plugin's skills get called and which skill takes each miss, on real prompts. Spends money, so it runs only when you type it.
disable-model-invocation: true
---

# route-test

Report per skill: hits, false fires, and who took each miss. A pass rate with no confusion table cannot say what to fix.

First run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <project root>` and do what it prints: a project works from its own copy of this plugin, never from the original.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`. `<plugin>` is a plugin folder, or a project root, which means the skills in its `.claude`. Every model run costs money, about $0.15 per case at 55 skills on Opus. State the estimate (cases x runs x cost) and get a yes before any `claude plugin eval`.

## 1. Cases

Write `cases.json`: `[{"id": "c01", "skill": "name", "prompt": "..."}, {"id": "n01", "skill": null, "prompt": "..."}]`.

Use these sources, best first:
1. Typed commands: `project.py mine <transcripts_dir> cases.json --only a,b`, with this project's own transcripts folder only. Keep `cases.json` with this project too. The user's own command is the label. The script drops anything pasted. Read the file and drop bare topics, which were arguments and not requests. Do this by a stated rule, and sample with a fixed seed.
2. Prompts written for the feature in the `design` skill, kept in the plugin as `tests/route_cases.json`.
3. Your own prompts, only when neither exists. Write them before reading the description, and mark them as written by the author in the report.

Include negatives: close misses that share words with the skill and should fire nothing. Aim for about 8 positive and 8 negative per skill under test. Leave out a skill with `disable-model-invocation: true`, since the model cannot select it. Never put a project's restricted data in a case; if the project forbids reading some transcripts' content, stop and ask.

## 2. Build

`project.py build <plugin> cases.json <out_dir> [--stub] [--also catalog.json] [--history fixtures/midwork.jsonl]`

- Keep `out_dir` outside the plugin. The plugin stays untouched.
- Give each run a new folder, such as `<runs>/<UTC time>/copy`, with its `result.json` beside it. `build` refuses a folder that holds files, so an earlier run's evidence is never wiped.
- `--stub` replaces bodies, which is right when measuring selection only. Leave it off to measure what happens after the skill loads.
- `--also` adds competing skills. Real misses come from what else is installed: `project.py catalog catalog.json <dirs> --only a,b`.
- `--history` resumes a mid-task conversation. Run both with and without it, since a fresh session flatters the result.

## 3. Run and read

```
claude plugin eval "<out_dir>" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --model <model> -j 4 --json <run>/result.json
project.py tally <run>/result.json
```

Always pass `--no-publish`. Pin the model, since firing rates differ widely between models. The cost ceiling is only checked before each run starts, so with `-j 4` spend can pass it by several runs: set it below what the user approved.

`unresolved` runs ended abnormally with no skill called yet. They are not misses. Each such `miss` line names the kind (`turn_limit`, `timeout`, `api_error`, `other`), and one `rerun` line per kind says what to do: a higher `--max-turns`, one more try, a rerun as is, or reading the error first. Rerun them before drawing a conclusion.

One run per case moves a rate by a whole case, so a difference of one or two cases is noise. Rerun the disputed cases with `--runs 3` before acting on them.

| Result | Next step |
|---|---|
| miss, another skill took it | Read both descriptions. Fix the overlap or merge the skills |
| miss, no skill | Sharpen the description first. Then `paths` for a skill tied to certain files. A hook only after those fail on a rerun |
| false fire | Narrow the description |
| all pass | Stop. Add nothing |

A label can be wrong: when the other skill also fits the request, say so and fix the case, not the skill.

## 4. Before and after

For any change, run the same cases on both versions and report what was gained and what broke, not only the net. A change that gains one case and breaks another needs the user's call.

When the user accepts a result, `project.py tally <run>/result.json --save <plugin>/tests/route_accepted.json` keeps each case's outcome. Later runs pass `--accepted` with that file and print `gained`, `broken` and `changed` per case; an unresolved run on either side is `not compared`, not a change.

A loaded skill can still be ignored. When following its procedure matters, drop `--stub`, raise `--max-turns`, and add a grader for a step of the procedure.

## 5. A skill that fires from a step

Some skills should fire partway through the work, such as after a draft and before the report. To a case, add `"after": "Write"`, the tool that marks the step, and `"rule": "<the standing rule's text>"`. `build` adds a `tool_order` grader and prints the `--allow-tools` the run needs, and `tally` counts a call at the wrong moment as `wrong_order`. Write the prompt so the step really happens, and add the same prompt without `rule` as a control.

An eval reads no project configuration, so `rule` reaches the run through `append_system_prompt`, standing in for the core index. A pass says the wording works, not that the hook delivered it.

Report in the user's language with the model, cases, runs and cost, and say which prompts were real, which parts were stand-ins (`--stub` bodies, a `rule` passed by `append_system_prompt`, a `--history` fixture), and what was not run.
