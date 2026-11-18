# A run nobody watches (design step 5)

A scheduled task, a `/loop`, or a pass repeated until a condition holds. Left alone, an agent drifts from the goal, counts time spent as progress, keeps what might help, and reinterprets the goal to call it met. The prompt below is the contract that stops each of those; write it before the first pass.

## Before turning it on

All four hold, or the run goes on only while someone watches it:

- The pass was done once by hand, so good output is known.
- The run has the checks and tools a person would use for the same judgment.
- Every step checks its own output and can stop the run when it misses the bar.
- A few watched passes were read, and each failure seen twice became a check or a script.

## The prompt

- **Goal** in one line, and the **finish condition** as a check that passes or fails: a command, a file state, a count. A duration is not a finish condition. A run with no end, such as a daily refresh, names the check each pass ends on.
- **Nothing to do**: what it looks at first, and the one line it ends with when nothing changed. Most passes should end there.
- **Without asking**: reversible work inside named paths, servers and commands. **Stops and asks** through a queue ticket with `--default`: an irreversible step, new data, an account or terms, a change of direction, a stop rule firing.
- **Claim before work**: a step another pass or session could also take is claimed first, where every run looks: `queue.py move <id> doing --session <id>`, or a dated marker on the plan line. A claim older than one pass's budget is free again.
- **Budget** per pass, in minutes or steps. At the budget, the pass stops at a safe point and records where it stopped.
- **One change, one check, one trail row.** A change that did not move the check is reverted, not left to ride. A plateau means a new approach, never a looser check.
- **Evidence between steps**: each step hands the next the file, the number with its key, or the command. A step that cannot show its output stops the run instead of passing it on.
- **Dead end**: the run stops with what was tried and why it cannot go on, in a ticket, rather than spinning or redefining done.
- **Report**: at most 5 lines, what changed and what waits on the owner.

## A run that improves a number

Freeze the measurement first: one command, shown to tell a known-good case from a known-bad one, with the baseline recorded before any change. The finish condition pairs the target with a minimum number of attempts, so one lucky result cannot end the run. Each attempt names the mechanism it tests, and a result that looks too good is checked for what else it measured.

## The trail

Each pass records forks, reverts, finished steps and stops with `queue.py log`. The board and the morning report read it, so the owner audits decisions rather than rereading sessions. A wrong row is corrected by a new row with `--supersedes`, never by an edit. A pass that must stop mid-step leaves `queue.py pause` with its note, and the next pass picks it up from there.
