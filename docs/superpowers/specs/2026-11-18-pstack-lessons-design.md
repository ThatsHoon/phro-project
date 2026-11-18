# What pstack knows about agents, and what phro-project takes from it

Source: every file of `cursor/plugins` pstack 0.15.43 (commit 93b916d): the README, the harness notes, the ten guide chapters, poteto-mode with `worker.md` and its 22 playbooks, the 24 principles, the other skills and their references, both agents, the benny automation pack, and the code of statechart and watch-pr with their tests. The 2.5 MB statechart test corpus was read for its structure only. An earlier version of this spec claimed a full read after about 40 files; this one replaces it.

This is not an import of pstack skills. It takes apart why pstack's agents do better work and changes phro-project's always-on rules, skills and scripts where they fall short of that.

## Owner directives (2026-11-18)

- "주석을 통해 상태를 기록하고 추후 작업에 참고하는 식의 플로우 … ai의 회피 편향을 기록하고 추후 그것을 정당화해가는 맥락으로 구축하여 프로젝트 퀄리티의 한계를 만드니 주석의 99%는 쓰지 않도록 해라."
- "pstack에서 유용한 스킬 몇 개를 phro-project 내에 장착하는 지시가 아님. pstack에서 agentic 개발 방식에서 agent를 보다 똑똑하게 작업시키기 위한 개발 원칙이나 철학을 해체하여 이해한 뒤에 phro-project에 반영하는 것임."
- "다시 한 번 프로젝트를 장기적으로 끌고 나가는 agentic 활용및 방침 고도화 측면에서 patack에서 놓친 부분은 없는지 전체적으로 점검하고 개선 진행해" (0.22.0, Part 4)

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

## Part 4. Carrying a project over months (0.22.0)

Parts 2 and 3 cover one session. A project that runs for months also needs what pstack builds for runs nobody watches and for work that crosses sessions: the overnight contract, autonomous-run, show-me-your-work, pause-safely, session-pickup, recall, correct, hillclimb and benny. The gaps below were checked against the one project that uses this plugin daily.

| pstack idea | phro-project before | Gap seen in real use | Change |
|---|---|---|---|
| The overnight contract, autonomous-run, earn trust before the loop, hillclimb, benny (laws 3, 4, 5) | design places skills, hooks, scripts and MCP; nothing for a run with nobody typing | The project's hourly unattended loop was built by hand: its plan has steps but no run-level finish condition, no budget per pass and no record format | design row "Scheduled run or loop" and `references/runs.md`: four trust conditions before turning it on; a prompt with the goal and a checkable finish condition (a duration is not one), a "nothing to do" exit, what it does without asking and what becomes a ticket with a default, a claim before work, a budget per pass, one change, one check and one trail row per step, a plateau that changes the approach and never loosens the check, evidence handed between steps and a stop when a step cannot show its output, and dead ends written to a ticket. A run that improves a number freezes its measurement, takes a baseline first, and sets a target with a minimum number of attempts |
| show-me-your-work (law 7) | 0.21.0 made reversible calls the agent's own | Autonomy with no record: what a session decided alone is only in transcripts, which no tool may read as text | `queue.py log`: append-only `queue/trail.tsv` (ts, session, phase, decision, why, evidence, result), committed. A wrong row is corrected by a new row that supersedes it by ts. One line per cell, at most 300 characters, detail in a file named by evidence; a cell a spreadsheet would run is quoted; ts strictly increases. The board shows decision, result, phase and session for 7 days; daily reads decision and result |
| pause-safely, session-pickup (law 7) | Nothing; handoffs written by hand | Two hand-written handoff documents, several status files and a memory pointing at a handoff three weeks old; nothing told a new session they existed | `queue.py pause` with a note that must hold Goal, Verified, Next and Files; the ticket becomes `paused`. A new session, not a compaction or resume, sees other sessions' paused tickets; it takes one only on the owner's word, checks what the note calls verified, and does not redo the rest. The board counts pauses older than 3 days |
| correct, encode-lessons-in-structure (law 6) | Answer proposes an enforcement when the user corrects a mistake a rule covers | The proposal waits for the user to notice; a rule broken in a run nobody watched is never raised | compass refresh: each work rule with no enforcer is checked against the problem log, reverts and feedback memories; a repeat opens a can-wait ticket proposing an enforcement, default "keep the rule as written", never a second ticket for the same rule |
| recall (law 7) | Build: root cause | A fix starts from nothing although the problem log or git holds the last failed attempt | Build: before the first fix, search the git log and the problem log and start where the last attempt failed |
| explain-the-number, figure-it-out (law 2) | Build: a check seen failing | A check that passes too easily, or a number too good, is accepted | Build: it is checked for what else it measured |
| session-pickup, "the trail is authoritative" (law 7) | Build: inherited claims checked | Checking turns into redoing | Build: the rest of that work is taken as given, not redone |
| sequence-verifiable-units, build-the-lever, guard-the-context-window (laws 3, 4) | Nothing | Long sessions make the same edit by hand in many files and keep state only in the chat, which a compaction cuts | Build: long work in units that each end in a check; a repeated edit through a rerunnable script proven on one unit by hand; what must outlive the context in a file |

The always-on text kept its byte caps (main session 6000, subagent 4000). Two main-only rules moved from Build to Answer to make room: the minimal version with the fuller one named, and the shape of a subagent brief. A subagent spawns no subagents and takes its scope from its brief, so neither applies to it.

Two defects came out of this round. daily put the titles of tickets from private sessions into its prompt; tickets and trail rows now pass the same private filter as transcripts. The board counted a paused ticket as "asking session gone", although a session that pauses is meant to end; paused tickets are left out of that count.

## Not taken, with the reason

- pstack's skills themselves, its model panels and multi-vendor reviewers, and Cursor-only features.
- Its autonomy defaults that act outward without asking (posting, PRs opened ready): this user's standing rules keep pushes and outward actions on the user's word.
- A decision log was not taken in 0.21.0 ("the queue holds decisions, git holds the trail"). Reversed in 0.22.0: 0.21.0 made reversible calls the agent's own, the project already runs an unattended loop, and git holds what changed, not why. It is a queue command, not a skill.
- Cross-model review and show-me-your-work's Attention section: one model family is in use. The board shows the trail without why or evidence, and the owner opens `trail.tsv` for those.
- A wip commit on pause: commits wait for the owner's word, and the project that uses this most is not a git repository. The note's Files section names what changed.
- Progress kept in memory: memory holds lasting facts; progress goes to the trail and pause notes, which carry a date and an owner.
- Evidence and risk fields on tickets: a ticket stays a question; evidence goes in trail rows.
- worktree-audit and maintain-verification: worktrees belong to the harness and the branch-finishing skill, and no stale verification has been seen.
- A rule to retry a failed subagent with a fresh one: one case, not measured.
- Paused work and the trail in the queue panel: the board page shows them, and the panel waits until someone asks.
- Naming every applied principle in each reply: it costs words in every answer, and `lean.py stats` and `compass usage` already measure rule use without it.
- A ratchet baseline for `check`: four findings exist, all in the reader under work on other branches; a baseline file would make them look accepted.
- Lint suppressions as a comment exception: a suppression on the line is accepted state in a comment, which the owner's directive rules out.
- daily as proof: it reads commits, tickets and trail decisions with the session tails; it is a status note the owner reads, not a proof.

## Tests

- `test_lean.py`: the new Build, Answer and Report lines reach the right roles; the old give-way and "how to try it" are gone.
- `test_compass.py`: every lint finding prints a fix; the compaction text holds the evidence sentence.
- `test_project.py`: every check finding prints a fix; `echo-description` fires on a body that repeats its description and not on one that does not.
- `test_restructure.py`: the lint line it compares carries the fix column.
- The whole suite after the comment removal, with no change in behavior. Each deleted comment that guarded a constraint is either covered by a test that fails when the constraint breaks (the manifest read before the move, private session titles, both shown failing on a broken copy) or gets one: `test_read_cli.py` keeps document text out of an internal error, and `test_project.py` keeps the catch-all error kind last.
- `test_queue.py` (0.22.0): `log` quotes formula cells, appends, supersedes one exact ts, refuses empty and overlong cells, keeps ts increasing; `pause` checks the note's headings, refuses done work, shows pickup to new sessions of others only, and the board counts stale pauses without counting them as gone.
- `test_daily.py` (0.22.0): trail decisions in the period reach the digest; private sessions and stretches hide their decisions and ticket titles.
- `test_lean.py` (0.22.0): the long-run Build rules reach both roles, the moved rules reach the main session only, the byte caps hold, and the runs reference, design row and compass sweep keep their key lines.
- Every new test above was run once on a broken copy of the code and failed there: 12 mutants of `queue.py`, 6 of `daily.py` and 6 of the skill texts in 0.22.0.
