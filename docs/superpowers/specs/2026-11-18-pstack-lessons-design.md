# Lessons from pstack: comments, injection cost, mistakes, measurement

Source: a full read of `cursor/plugins` pstack 0.15.43 (58 skills, 2 agents, no hooks), set against every phro-project node. pstack keeps rules opt-in, ranks a rule by what enforces it, raises a lesson only after it repeats, keeps no memory store and rebuilds context from the record, and treats a skill edit as an experiment.

## Owner directives (2026-11-18)

- "주석을 통해 상태를 기록하고 추후 작업에 참고하는 식의 플로우 … ai의 회피 편향을 기록하고 추후 그것을 정당화해가는 맥락으로 구축하여 프로젝트 퀄리티의 한계를 만드니 주석의 99%는 쓰지 않도록 해라."
- Build the whole improvement set and apply it to phro-project.

## 1. No state in comments

A `lean:` marker records a shortcut as accepted state. Later sessions read it, treat the ceiling as settled, and the shortcut becomes the reason not to do the work. The debt scan then reports the count every session, which normalizes it further.

Changes:
- `lean` Build: the marker bullet is replaced by a comment rule. Write no comments. The exceptions are a license header, behavior forced by an outside dependency, platform or protocol that the code cannot show (with its issue or spec link), and a public API's documented contract. A shortcut, a known limit or a skipped test is never written into the code: finish it, or name it once in the reply.
- `lean-review`: the `debt:` tag goes. A `comment:` tag flags every comment outside those exceptions, and any `lean:`, `ponytail:`, `TODO`, `FIXME` or `HACK` marker, with the replacement (delete, or rename or extract so the code says it).
- `compass`: the `debt` command, the debt baseline in `stamp`, the `debt N (+K)` part of the drift line and the debt tickets go.
- The plugin's own `lean:` markers outside `scripts/reader/` go. The reader is under active development on other branches, so its one marker and its comments wait for that work to merge.

Existing markers in projects are not touched by this change. A project removes them through `lean-review` on the owner's word.

## 2. Injection that repeats on resume

SessionStart fires on `resume` (`--resume`, `--continue`, `/resume`). The restored conversation already holds what the hook printed before, so the same lean rules, queue rule and core index are added a second time, about 2,400 tokens each. A fork arrives as its own source, `fork`, which the matcher does not list, and it inherits the parent's context.

Change: `scripts/once.py` keeps one SHA-256 per session and part under `<config>/phro-project/seen/`. Every injection records its hash. On `resume`, a part whose hash matches the last one for that session prints nothing. A part that changed since, such as an index a refresh rewrote, prints in full, which is why resume stays in the matcher. Compass hashes the index only, so the short status lines still print. Files older than 14 days are pruned at startup.

## 3. Subagents get the rules their role needs

`agent_type` arrives with SubagentStart (`Explore`, `Plan`, `general-purpose`, or `plugin:name`). The read-only built-ins (`Explore`, `Plan`, `claude-code-guide`, `web-fetch`) write no code, so they get Report only, about 450 tokens fewer each. Report gains pstack's worker rule: the brief's bar is the whole bar, with no check or review round added above it.

## 4. A repeated mistake goes to structure, not more prose

pstack ranks a fix by what enforces it: architecture, then types, lint or CI, then a test, with a written rule last, since nothing fails when an agent skips prose. A mistake counts once it has happened twice. A written rule that is broken again is that second time.

Changes:
- `lean` Answer: when the user corrects a mistake that a written rule already covers, propose in the same reply the strongest enforcement that fits (a permission deny, a hook, a test or a script), and build it only on the user's yes.
- `design`: a mistake-class section with that ladder, used instead of the capability table when the request is to stop a repeated mistake. The description gains the words users type for it.
- `compass` writing rules: a work rule that something enforces names it in parentheses, so a rule nothing enforces is visible.
- The compaction instruction keeps only what changes later work, and a preference seen once becomes a rule only when the user stated it as one.

## 5. Evals the candidate cannot recognize

pstack never lets a candidate see that it is evaluated, since an agent that knows behaves differently. `project.py check` gains `leaky-case`: a routing prompt in `tests/route_cases.json` or a quality `prompt.md` that contains eval, test, judge, grader, rubric, benchmark or candidate words.

## 6. Whether lean works

pstack makes rule use visible by having the agent name the rule and the decision it changed. Nothing shows whether lean's answer rules hold. `lean.py stats <transcripts_dir> [--days N]` counts, per arm (sessions where lean was injected and sessions where it was not), the replies and the share with a list group over 5 items and with more than one question in the last paragraph. It prints counts only, never text.

## Already aligned, no change

- Owner decisions: queue tickets with `--default` already proceed on reversible work.
- Measured reports: `design` already splits Verified, Stand-in and Not verified.
- One owner per fact and no numbers in the index: compass already points at ledgers instead of copying them; dates stay out of core documents, so a decision's source is its owner file and git, not an inline date.
- Daily reports already read the day's commits.

## Deferred, with the reason

- Shorter queue rule and skill descriptions: a description change moves routing, so it waits for a route-test run.
- Grading whether a candidate followed a skill from the files it read: needs a paid quality run to check.
- Stripping comments across existing plugin code: a separate cleanup through `lean-review`, after the reader branches merge.

## Not taken from pstack

Its autonomy defaults (external actions without asking, no question tools, PRs opened ready), its per-run read cost (router and adapter re-read by every delegate), and multi-vendor panels.

## Tests

- `test_lean.py`: resume skip and change, subagent role scoping, the comment rule and the absence of the marker format, stats on synthetic transcripts.
- `test_compass.py`: debt gone, resume prints the status lines without the index when unchanged, compaction text.
- `test_queue.py`: resume keeps the open-ticket line and drops an unchanged rule.
- `test_project.py`: `leaky-case`.
