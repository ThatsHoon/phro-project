# What pstack knows about agents, and what phro-project takes from it

Source: every file of `cursor/plugins` pstack 0.15.43 (commit 93b916d): the README, the harness notes, the ten guide chapters, poteto-mode with `worker.md` and its 22 playbooks, the 24 principles, the other skills and their references, both agents, the benny automation pack, and the code of statechart and watch-pr with their tests. The 2.5 MB statechart test corpus was read for its structure only. An earlier version of this spec claimed a full read after about 40 files; this one replaces it.

This is not an import of pstack skills. It takes apart why pstack's agents do better work and changes phro-project's always-on rules, skills and scripts where they fall short of that.

## Owner directives (2026-11-18)

- "주석을 통해 상태를 기록하고 추후 작업에 참고하는 식의 플로우 … ai의 회피 편향을 기록하고 추후 그것을 정당화해가는 맥락으로 구축하여 프로젝트 퀄리티의 한계를 만드니 주석의 99%는 쓰지 않도록 해라."
- "pstack에서 유용한 스킬 몇 개를 phro-project 내에 장착하는 지시가 아님. pstack에서 agentic 개발 방식에서 agent를 보다 똑똑하게 작업시키기 위한 개발 원칙이나 철학을 해체하여 이해한 뒤에 phro-project에 반영하는 것임."

## Part 1. The model of an agent behind pstack

pstack's whole design follows from one picture of a coding agent, stated in its architect skill: it "sees only the files it opened, copies the nearest example, and takes the shortest path that compiles." The rest of the corpus adds four more traits. It defends what it wrote. It grades itself generously, on proxies. It drifts from the goal over a long run and keeps adding what might help. It acts differently when it knows it is watched.

Seven laws answer those traits. Each law names the failure it prevents.

| # | Law | Failure it prevents | Where pstack holds it |
|---|---|---|---|
| 1 | Context is instruction. Whatever an agent reads, it copies or obeys, so only true, current text may stay in reach | A stale comment, an old code path or a long rationale becomes the template for the next change | architect (delete the old way), comment-sicko, authoring-a-skill ("when in doubt, delete"), subtract-before-you-add, every error message naming the fix |
| 2 | A claim is worth its evidence. Proof is an artifact someone other than the author can check, and a check counts only after it has been seen failing | Success reported off a green build, a self-report, or a test that could never fail | prove-it-works, explain-the-number, Shipping (the judge is never the author), eval (blind candidates), statechart's differential tests and "not checked" lines |
| 3 | The finish line is fixed before the work. It is a predicate that can pass or fail, and it is never loosened | Goal drift, a duration taken as done, a plateau taken as a stop | autonomous-run, hillclimb, the overnight contract, "a duration is not a finish condition" |
| 4 | One change, one measurement. What did not help is reverted, and what a refuted idea motivated goes with it | Belt-and-suspenders changes that accumulate; fixes stacked on a wrong premise | hillclimb, attack-the-premise, fix-root-causes, the night loop |
| 5 | Autonomy ends where reversibility ends. Reversible calls are made and named; a fact is settled by running something; only a preference or an irreversible step goes to a human | Blocking on questions an experiment answers, or acting on what cannot be undone | never-block-on-the-human, worker.md, prototype-instead-of-asking, read-only and draft-only defaults |
| 6 | A rule is ranked by what enforces it. Structure beats prose; once structure holds, the prose goes | The same correction typed every week; written rules nobody enforces | correct, encode-lessons-in-structure ("the instruction is the symptom"), ratchets, rule tables with enforcers |
| 7 | Memory is the record, and the record is corrected, not narrated. Inherited claims are verified on the artifact | A summary or a previous run's word reused as proof; lessons from one session turned into rules | session-pickup, show-me-your-work ("correct the log, not the story"), recall, reflect's thresholds |

Delegation applies all seven to subagents: a brief carries the goal, the done check, the proof wanted, the known facts and the real constraints, by path and without the how; the worker holds the brief's bar and decides reversible calls; the parent owns and checks what comes back.

The code follows the same laws: every error names what is wrong, where and the next step; unknown input fails closed before any work; output is all or nothing; coverage is reported honestly, including what was not checked; every loop and wait is bounded.

## Part 2. phro-project against the laws

| Law | phro-project before | Gap | Change |
|---|---|---|---|
| 1 | No-comment rule (0.20.0); compass keeps documents short and current | lint and check findings say what is wrong but not what to do, so the skill text explains fixes the tool could state; skill bodies may repeat their description | Every lint and check finding carries its fix as a fourth column; the prose that explained those fixes goes. `check` flags a body that repeats its description |
| 1 | Comments in the plugin's own scripts | 0.20.0 deferred them | Comments outside the exceptions are removed from every script outside `scripts/reader/` |
| 2 | Build: "one runnable check"; Answer: "what now works and how to try it" | A check never seen failing counts; the user is handed the check | The check must be seen failing once. A finished reply carries the evidence that was run, says how each claim is known, and never hands the user a check the agent could run |
| 2 | lean-review run by the session that wrote the diff | An author defends its code | When the session wrote the diff, the review goes to a fresh subagent and the parent checks each finding |
| 3 | Nothing in Answer about done | Multi-step work starts without a predicate | Multi-step work opens with the goal and the check that will show it done; the check is never loosened |
| 4 | Build: root cause, deletion over addition; Answer: after three failed turns, ask the user one diagnostic question | Guards and retries pass as fixes; "might help" stays; the diagnostic goes to the user | A guard, retry, fallback or swallowed error is not a fix. What was added on a "might help" stays only when a run shows it helped. After two failed fixes on one premise, the premise is written down and tested; the give-way that asked the user goes |
| 4 | Build: minimal version first | A design change can leave the old path and a shim behind | A design change moves every caller and deletes the old path in the same change |
| 5 | Queue tickets with `--default`; grill asks decisions | Reversible choices still come back as questions; a fact a run could settle is asked | Answer: a choice cheap to undo is made and named; a question a run can settle is settled by running it. Grill treats such a question as a fact |
| 5 | Nothing about candor | Agreement by default | Answer: when the user's plan or claim looks wrong, say so with the reason |
| 6 | design 5b ladder, enforcer in parentheses (0.20.0) | The written rule stays after the structure lands | 5b and compass: when a check makes the mistake impossible, the written rule is deleted |
| 6 | design step 10 rules for scripts with side effects | Error lines need not say the next step; unknown input may be guessed | Step 10: error and refusal lines name what is wrong, where and the next step; a missing or unknown input stops before any side effect |
| 7 | Compaction instruction moves the summary into docs/core | The summary's own word about results enters the documents, and the next step builds on it | Documents take only what the user said or a file shows. A claim in the summary that something passes is checked on the artifact before the next step relies on it |
| 7 | Build has nothing on inherited claims | A subagent's or an earlier session's "done" is repeated as fact | Build: a claim from an earlier session, a summary or a subagent is checked on the artifact before it is built on or reported |
| Delegation | Report rules for the subagent side only | The parent's brief has no shape | Build: a brief holds the goal, the done check, the proof to return, the known facts and the real constraints, by path, without the how. Report: a problem the base already had gets one line; a check that could not run is inconclusive, not a pass; each claim carries its evidence |

## Part 3. What 0.20.0 already holds

1. No state in comments: lean Build's comment rule, lean-review's `comment:` tag, compass's debt flow removed.
2. Resume dedupe: `scripts/once.py` keeps one SHA-256 per session and part; an unchanged part is not injected again on resume.
3. Role-scoped subagents: the read-only built-ins get Report only; Report holds "the brief's bar is the whole bar".
4. A repeated mistake goes to structure: the Answer enforcement proposal, design 5b, the enforcer in parentheses in compass work rules.
5. Evals the candidate cannot recognize: `check` flags `leaky-case`.
6. Whether lean holds: `lean.py stats`.

## Not taken, with the reason

- pstack's skills themselves, its model panels and multi-vendor reviewers, and Cursor-only features.
- Its autonomy defaults that act outward without asking (posting, PRs opened ready): this user's standing rules keep pushes and outward actions on the user's word.
- A decision-log skill for overnight runs: the queue holds decisions, git holds the trail, and no autonomous run has needed more yet.
- Naming every applied principle in each reply: it costs words in every answer, and `lean.py stats` and `compass usage` already measure rule use without it.
- A ratchet baseline for `check`: four findings exist, all in the reader under work on other branches; a baseline file would make them look accepted.
- Lint suppressions as a comment exception: a suppression on the line is accepted state in a comment, which the owner's directive rules out.
- daily's sources: it reads commits and tickets with the session tails; it is a status note the owner reads, not a proof.

## Tests

- `test_lean.py`: the new Build, Answer and Report lines reach the right roles; the old give-way and "how to try it" are gone.
- `test_compass.py`: every lint finding prints a fix; the compaction text holds the evidence sentence.
- `test_project.py`: every check finding prints a fix; `echo-description` fires on a body that repeats its description and not on one that does not.
- `test_restructure.py`: the lint line it compares carries the fix column.
- The whole suite after the comment removal, with no change in behavior. Each deleted comment that guarded a constraint is either covered by a test that fails when the constraint breaks (the manifest read before the move, private session titles, both shown failing on a broken copy) or gets one: `test_read_cli.py` keeps document text out of an internal error, and `test_project.py` keeps the catch-all error kind last.
- Every new test above was run once on a broken copy of the code and failed there.
