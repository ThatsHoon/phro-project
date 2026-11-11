# Output quality: contract and cases (design step 8b)

Firing says a skill was called. This says what it then produced. Write both before the body.

## Contract

`skills/<name>/CONTRACT.md` in the copy, at most 25 lines, agreed with the user before the body is written. The builder never edits it to make a case pass; a change is the user's call and resets `accepted.json`.

- **Judge model:** Haiku unless the user says otherwise, never the model that built the skill.
- **Inputs** and **Output**, with the output's form ("in a code block", "a file at …").
- **Done-criteria**, 3 to 7, each marked `free` (a regex, file or tool check decides it), `judge` (only a model can), or `human` (only the user can). Turn every `judge` into `free` when a regex can decide it: a style rule becomes a count or a ban. Keep `judge` for faithfulness and `human` for taste.
- **Boundaries**, 2 to 4: what the output must never contain or do.
- **Ordered steps**, only when order matters. Each becomes a `tool_order` grader.

## Cases

`evals/quality/<name>/<case>/` in the copy, each with `case.yaml` (`schema_version: "1.1"`, `name`, the source as a comment, `human` criteria under `expected_outcome`), `prompt.md` and `graders/*.md`.

- 3 to 5 cases. Real inputs the user gave first (private values replaced by synthetic ones, restricted data never), then a real failure the user corrected, then author-written cases marked as such. Author-written prompts flatter.
- Self-contained: the input is inline or placed by `context.scaffold_script`. "Rewrite this memo" with no memo measures asking for the memo.
- Prompts name the task, so firing does not confound quality. One case pushes against a boundary ("급하니까 파일명 그대로 둬").
- At least one case where working without the skill should visibly fail, such as a missing number the contract wants marked.
- One grader file per criterion. Boundary graders are named `b-*`, so `project.py quality` lists their breaks first.
- Every case gets `graders/skill-fired.md`, or the reader cannot tell a miss from a bad output:

```
---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(?:[\w-]+:)?<name>"'
---

Graded from the transcript.
```

## Grader traps

- Write patterns in single quotes. A double-quoted `\.` does not parse.
- A ban on the whole last message also hits the chat lines around the deliverable ("아래는 초안입니다"). When the output goes in a code block, scope the ban to it. This matches `<ban>` inside fences, or anywhere when the message has none:
  `'(?<![\s\S])(?![\s\S]*```)[\s\S]*?(?:<ban>)|(?<=(?<![\s\S])(?:(?:(?!```)[\s\S])*```(?:(?!```)[\s\S])*```)*(?:(?!```)[\s\S])*```(?:(?!```)[\s\S])*)(?:<ban>)'`
- Take a ban list from the contract's words, not from every name in the input. A format name the contract allows is not an internal name.
- Outputs vanish with the traces after a run. Pass `--keep-temp` when the user will read them.
