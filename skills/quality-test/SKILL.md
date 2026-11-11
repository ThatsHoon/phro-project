---
name: quality-test
description: Measures what a skill produces, with and without it, against its CONTRACT.md and graded cases. Spends money, so it runs only when you type it.
disable-model-invocation: true
---

# quality-test

Report per case and grader: the pass rate with and without the skill, the difference, boundary breaks first, whether it fired, and the cost. A firing rate says nothing about the output; this does.

First run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <project root>` and do what it prints: a project works from its own copy of this plugin, never from the original.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py <command>`.

## 1. Contract and cases

`skills/<name>/CONTRACT.md` and `evals/quality/<name>/` must exist in the copy, as `${CLAUDE_PLUGIN_ROOT}/skills/design/references/quality.md` describes. If they do not, write them by that file first, and get the user's yes on the contract before any run.

## 2. Run

Every run costs money, about $0.13 per run with the skill and $0.08 without on Opus. State the estimate (cases x 3 runs x both arms, plus judge calls) and get a yes.

```
claude plugin eval "<copy>" --eval-dir evals/quality --case "<case glob>" --ablation with-without --runs 3 --no-publish --trust-plugin --keep-temp --model <model> --judge-model <contract's judge> --max-cost-usd <usd> -j 4 --json <out>/quality.json --report <out>/quality.html
project.py quality <out>/quality.json [--accepted evals/quality/<name>/accepted.json]
```

`--case` matches case folder names. Keep `<out>` outside the plugin. The ceiling is checked before each run starts, so set it below what the user approved. A difference of one run in three is noise. A run that ended abnormally is `unresolved`: it is left out of every rate, and its `rerun` line says what to do.

## 3. Decide with the user

| Result | Next |
|---|---|
| Boundary broken | Read that run's transcript in the kept temp folder first. Fix the skill body. Fix the grader only when it bans what the contract allows or reads outside the deliverable, and say so |
| `free` criterion fails with the skill | Fix the body, or move the rule into a script the skill calls |
| Non-discriminating grader | Sharpen it, or drop it and say so. It proves nothing |
| No gain on any case | Recommend not keeping the skill |
| An ordered step followed in under 2 of 3 runs, twice | Propose a script or hook. Do not build it |
| 2 of 3 runs with the skill write the same helper code | Propose it as a script the skill calls. Do not build it |
| `human` criteria | Show the user the outputs with and without the skill side by side, from the report or the kept temp folders. Their comments become cases or contract edits, by them |

## 4. Prove the graders

A boundary that never failed proves nothing. Once per skill, and whenever a boundary passes in both arms: copy the plugin to a scratch folder, delete from the copy's `SKILL.md` the rule that boundary checks, and rerun that case with `--ablation none`. The boundary must break. If it holds, the case or the grader is too weak to keep.

## 5. Accept

When the user accepts, `project.py quality <out>/quality.json --save evals/quality/<name>/accepted.json`. Later edits to the skill rerun its cases and report gained and broken against that file before they count as done.

Report in the user's language: the model, cases, runs and cost, the difference per grader, the breaks, and where the outputs that need human reading are.
