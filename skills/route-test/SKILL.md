---
name: route-test
description: Measures whether a plugin's skills get called and which skill takes each miss, on real prompts. 스킬이 호출되는지 측정, 왜 안 불렸는지, 설명문 수정 전후 비교.
disable-model-invocation: true
---

# route-test

Report per skill: hits, false fires, and who took each miss. A pass rate with no confusion table cannot say what to fix.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`. Every model run costs money, about $0.15 per case at 55 skills on Opus. State the estimate (cases x runs x cost) and get a yes before any `claude plugin eval`.

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
- `--stub` replaces bodies, which is right when measuring selection only. Leave it off to measure what happens after the skill loads.
- `--also` adds competing skills. Real misses come from what else is installed: `project.py catalog catalog.json <dirs> --only a,b`.
- `--history` resumes a mid-task conversation. Run both with and without it, since a fresh session flatters the result.

## 3. Run and read

```
claude plugin eval "<out_dir>" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --model <model> -j 4 --json result.json
project.py tally result.json
```

Always pass `--no-publish`. Pin the model, since firing rates differ widely between models. The cost ceiling is only checked before each run starts, so with `-j 4` spend can pass it by several runs: set it below what the user approved.

`cut_off` runs hit the turn limit with no skill called yet. They are unresolved, not misses. Rerun them with a higher `--max-turns` before drawing a conclusion.

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

A loaded skill can still be ignored. When following its procedure matters, drop `--stub`, raise `--max-turns`, and add a grader for a step of the procedure.

Report in the user's language with the model, cases, runs and cost, and say which prompts were real.
