# design: measure what a built capability produces, not only whether it fires

## Problem

`design` ends at `route-test`, which measures one thing: whether the Skill tool is called. Nothing checks what the capability then produces. In the last build on a project copy, a writing-style skill was built, its firing was measured twice (72 runs, about $9.40), and its output was never looked at once. Firing at 5 of 8 says nothing about whether the text it writes is any good. Measuring that is all this spec covers. It does not cover authoring the skill body, MCP design, or invocation mode.

A working run looks like this: after `design` builds a capability, a short output report states, per case, what it produced with and without the capability (Δ). It names which checks failed and what each failure was, and the user has looked at the outputs that need human judgment.

## Sources read, and what each gives

| Source | Taken | Left out |
|---|---|---|
| Anthropic `skill-creator` (SKILL.md, agents/grader.md, analyzer.md, comparator.md, references/schemas.md) | With-skill vs without-skill runs in the same batch; case folders in the `claude plugin eval` layout; one verifiable statement per grader file; the grader also critiques the evals; the analyzer flags assertions that pass in both arms (non-discriminating) or vary between runs (flaky); assertions only where output is objectively checkable, with human review for the subjective rest; snapshot the old skill as baseline when improving | Its own workspace layout, viewer and `benchmark.json`. `claude plugin eval` already runs both arms, computes Δ and writes an HTML report |
| `claude plugin eval` docs (code.claude.com/docs/en/plugin-evals.md) | Six grader types. Four are free: `regex` (with `match: not_contains` or `count:N`), `tool_used`, `tool_order`, `file_exists`. Two are paid: `llm` (with `focus`: `last_message`, `trace`, `{source: file, path}`) and `baseline`. Also: `--ablation with-without` gives a WITH score, a W/OUT score and Δ; `tool_used: Skill` graders are with-only indicators, not score; `context.scaffold_script` (with `--scaffold`) places input files; `--judge-model` | Custom-code graders (none exist) |
| ECC `skill-comply` | The skill text becomes 3 to 7 observable steps, each with order constraints. The same task runs at three prompt levels (supportive, neutral, competing). A step followed in under 60% of runs is a candidate to become a hook | Its own runner. `tool_order` and `llm focus: trace` graders cover the same checks |
| ECC `eval-harness`, `agent-eval` | Define the evals before building. Capability evals (pass@3) are kept separate from regression evals (pass^3). Prefer deterministic graders. At least 3 runs per case. Track cost and time alongside pass rate | Its capsule and replay machinery |
| ECC `loop-design-check`, `gan-style-harness` | Agree on the acceptance conditions before the build (the "sprint contract"). Every done-criterion has a boundary, a statement of what the output must not do. The judge is never the builder, and the builder may not edit the acceptance conditions to pass. Retry cap. The human flips the last switch, and the more self-rewriting a loop is, the stricter that review must be | The autonomous generator-evaluator loop. See Not doing |
| ECC `taste-distillation`, `brand-voice` | "Prompting cannot deliver a grade. Measurement can." Style rules become measured features and bans enforced in code, not in prompt text. A voice profile lists preferred and banned moves, each observable in the source | Video and LUT specifics |
| ECC `santa-method` | Two independent reviewers on the same rubric, both must pass, fresh reviewers each round | Running it on every output. It is used here only for the subjective criterion, and only when the user asks |
| ECC `ai-regression-testing`, `benchmark-optimization-loop` | Add a case where a failure was found, not for coverage. Compare against the last accepted result, not the previous run. A difference within noise is not a change. A promotion gate | |
| Own record (memory: plugin lessons, measured 11-03 to 11-05) | Prompts written by the author fired 14 of 16; real prompts fired 1 of 16. So author-written cases flatter, and real inputs come first. A self-improvement loop that edited its own files landed 1 of 8 cycles at about $11 of overhead, so no automatic rewriting | |

## Design

### 1. Output contract, written before the skill body (new step 8b)

Step 8 already makes firing cases before the build. Step 8b adds the output contract in `skills/<name>/CONTRACT.md` of the copy, at most 25 lines. It is agreed with the user before the body is written, and the builder does not change it afterwards. A change to it is the user's call and resets the accepted result.

- **Inputs:** what the capability receives.
- **Output:** what it returns, and its form.
- **Done-criteria:** 3 to 7 statements about the output. Each is marked `free` if a regex, file or tool check can decide it, `judge` if only a model can, or `human` if only the user can.
- **Boundaries:** 2 to 4 statements about what the output must never contain or do. These are the anti-Goodhart half.
- **Ordered steps:** only when order matters. Each becomes a `tool_order` grader.

Rule: every `judge` criterion first tries to become a `free` one. A style rule ("formal endings", "no internal file names") becomes a regex with a count or a `not_contains` ban. `judge` is kept for faithfulness and coherence. `human` is kept for taste.

### 2. Cases (`evals/quality/<skill>/<case>/` in the copy)

- **Count:** 3 to 5 per skill.
- **Sources, best first:**
  1. A real input the user already gave in a session. Use its text, never restricted data, and replace private values with synthetic ones.
  2. A real failure that the user corrected.
  3. Author-written cases, marked as such in `case.yaml`.
- **Self-contained:** every prompt carries its input inline or in `files/` with `place-files.sh`. A prompt that points at missing content ("rewrite this memo") is not a case. It measures the model asking for the memo.
- **Prompt level:** level 1 (supportive), meaning the prompt names the task the skill does, so that firing does not confound quality. Firing stays `route-test`'s job. One extra case per skill runs at level 3 (competing): the prompt pushes against a boundary, for example "keep the run IDs in". It checks whether the boundary holds.
- **At least one discriminating case:** a case where doing it without the skill should visibly fail. For example, the input lacks a number and the contract requires a marker in its place.
- **Graders:**
  - one file per criterion: `regex`/`file_exists`/`tool_order` for `free`, `llm` for `judge`;
  - `human` criteria are listed in `case.yaml` under `expected_outcome` and are not graded;
  - plus the automatic with-only `tool_used: Skill` indicator.

### 3. Run

```
claude plugin eval <copy> --eval-dir evals/quality --case "<skill>/*" --ablation with-without --runs 3 --no-publish --trust-plugin --model <session model> --judge-model <model> --max-cost-usd <usd> -j 4 --json <out>/quality.json
```

- **Cost:** stated first and approved, as in `route-test`. The estimate is cases × 3 runs × 2 arms × run cost, plus judge calls.
- **Judge model:** set per skill in `CONTRACT.md`. Haiku is the default. The model of the session that built the skill is never the judge, to keep the judge independent.

### 4. Read (`project.py quality <quality.json> [--accepted <file>]`, new, deterministic)

Per case and per grader, report:

- **Δ:** with-arm minus without-arm pass rate.
- **Non-discriminating:** passes in both arms in 3 of 3 runs.
- **Flaky:** passes in 1 or 2 of 3 runs in the with arm.
- **Boundary breaks:** any boundary grader that failed, listed first.
- **Fired:** the with-only Skill indicator. A case where the skill did not fire is shown, not scored.
- **Cost and time:** per arm.

With `--accepted`, it also reports gained and broken against the last accepted result. A change of one run in three is noise.

### 5. Decide, with the user

| Result | Next |
|---|---|
| Boundary broken | Fix the skill body. Fix the grader only when it bans what the contract allows or reads outside the deliverable, and say so |
| `free` criterion fails in the with arm | Fix the body, or move the rule into a script the skill calls (measurement over prompting) |
| Non-discriminating grader | Make it sharper, or drop it and say so. It proves nothing |
| Δ ≤ 0 on every case | Recommend not keeping the skill (row "Don't build" of the placement table) |
| An ordered step followed in under 2 of 3 runs, on two separate runs | Candidate for a script or hook (the table's "measured misses first"). Proposed, not built |
| `human` criteria | Show the user the with-arm outputs next to the without-arm outputs from the eval report. Their comments become new cases or contract edits, by them |

When the user accepts, `evals/quality/<skill>/accepted.json` stores the result. Later edits to that skill rerun its cases and report against that file before they count as done.

### 6. Where it lives

- **`design` SKILL.md:** step 8b (contract and cases), with step 11 now covering both measurements. The detail lives in `skills/design/references/quality.md`, read only at that step, so the body grows by about 6 lines.
- **New user-only skill `quality-test`** (`disable-model-invocation: true`, so it costs nothing in the listing): runs sections 3 to 5 on an existing skill. `route-test` stays as it is.
- **`project.py quality`:** with tests in `tests/test_project.py` on a fixed `quality.json` fixture.
- **No new hook, MCP or loop.**

## Not doing

- **No automatic generator–evaluator loop that rewrites skills.** The measured record above says it does not pay here. Failures become proposals, and the user approves each edit.
- **No own viewer or benchmark format.** The `claude plugin eval` report and `quality.json` are enough.
- **No dual-reviewer gate by default.** Use it only on the user's request, for a `judge` criterion that decides adoption.
- **No coverage goal.** Cases grow where failures were found.

## Decisions (user, 2026-11-11)

1. A new user-only skill `quality-test`; `route-test` is unchanged.
2. The judge model is Haiku.
3. Pilot first: the writing-style skill in one project copy, 4 cases, about $4 to $5, before the method goes into `design`.

## Success check for this change

- **The pilot report shows at least one grader with Δ > 0** and lists any non-discriminating grader.
- **A deliberate defect is caught.** Removing the ban section from the pilot skill and rerunning must break at least one boundary grader. If nothing breaks, the graders prove nothing.
- **The `design` body grows by no more than 10 lines.**
- **`check` passes.**

## Pilot result (2026-11-11)

Writing-style skill, 4 cases (2 from real session inputs, 1 boundary push, 1 author-written), Opus runs, Haiku judge, $4.18 in all.

- With and without, 3 runs each: 8 of 19 graders gained beyond noise (+0.67 to +1.00), mean +0.40 over all 19. Five passed in both arms and prove nothing yet.
- Two grader faults, both from reading the whole last message: the polite-ending ban hit the chat lines around the code block in every run, and one name ban listed format names the contract allows. Both were scoped to the report body and the contract's words. Rerun with the skill: 19 of 19 graders 3 of 3, fired 12 of 12.
- Deliberate defect: with the ban rules deleted from the skill, the boundary case kept the script and split names in 3 of 3 runs (1.00 before, 0.00 after). Caught.
- Every case needs the Skill indicator grader, or the reader cannot tell a miss from a bad output.
