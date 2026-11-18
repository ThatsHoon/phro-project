---
name: compass
description: Builds and refreshes a project's core documents in docs/core, the fixed point every session and fork starts from. Runs only when you type it.
disable-model-invocation: true
---

# compass

Output: short documents in `docs/core`, then at most 5 lines to the user. Long work buries the original goal under local detail. These documents hold what must survive that, so they stay short.

First run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <project root>` and do what it prints: a project works from its own copy of this plugin, never from the original.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/compass.py <command> <project root>`.

A plugin hook prints `docs/core/README.md` at every session start and after every compaction, in every session and fork of the project. It is silent in a project without that file and `core.json`. After a compaction, manual or automatic, it also tells that session to compare its summary with these documents and update what the summary shows is out of date, so the documents keep up with no one asking.

These documents belong to one project. Use only the project at the given root: its files, its own memory folder and its own sessions. If this session has been working in another folder, stop and ask the user to open a session in the target project, so this conversation's context stays out of the documents. A scheduled run starts clean, so it goes ahead.

## The documents

| File | Holds |
|---|---|
| `README.md` | The index sessions receive, at most 40 lines and 2,500 characters: final goal in a line or two, current mid-term goals, one line per area saying when to read its document, the names of what was ruled out, work rules, folder rules |
| `goals.md` | First section: the final goal in the user's own words, as `>` quotes. Then mid-term and short-term goals, then what was ruled out |
| `architecture.md` | The whole system: its parts, how data moves between them, where each runs, then the design principles every part follows. A principle only one area follows goes in that area's document |
| `features/<area>.md` | One per largest functional area: purpose, inputs and outputs, entry points, what is adopted now, the open problem |
| `<name>.md` | A document the user asked for. Its `brief` in `core.json` records what they want it to hold |
| `structure.md` | Folder rules, written by the `restructure` skill |
| `core.json` | The list of documents and the sources each depends on |

## Writing rules

- State what is true now. Replace old text, never append to it.
- No change history, dates, version chains or experiment logs. Those stay in ledgers, MLflow and git. A line that does not orient a new developer or change a decision is cut.
- At most 80 lines per document. Details stay in the documents these point to.
- Use the user's language. Write a path only if it exists here; a path on another machine starts with `~/` or names the host.
- No AI marks or AI writing tells. `lint` checks for them.
- Write what cannot be found by looking: the reason behind a choice, an unwritten convention, a trap. Leave what one file or one command shows to that file or command, where it cannot go stale.
- A fact has one owner. If a registry, ledger, spec or status file already holds it, link that, and never rewrite a document generated from a registry.
- A decision earns a line only when it is hard to reverse, surprising without its reason, and the result of a real trade-off.
- What was ruled out gets one line each in its own section of `goals.md`, in the user's language: what, why, what replaces it, and the condition for looking again. Start the section with its first item. Name a removed path without backticks, since `lint` reads those as live paths.
- An index line for an area says when to read the document, one clause per distinct case: `When changing payment retries or refunds: features/billing.md`.
- The index holds no number or setting the code holds. In a test, sessions repeated such a value from the index without opening the code.
- Work rules in the index are the user's standing instructions, and later sessions act on them. A refresh keeps them, and only the user drops one. A rule that something enforces names it in parentheses, such as `(hook: guard.py)` or `(test: test_paths.py)`, so a rule nothing enforces stands out when it is broken again. When the enforcer makes the mistake impossible, the rule goes; ask the user first, since it is theirs.
- A claim nobody has confirmed carries `확인 필요`, or the same words in the user's language, until the code or the user settles it.
- These documents keep no glossary. If the project needs one, call the Skill tool with `mattpocock-skills:domain-modeling` when that skill is listed, and name its file in the index.
- Never read session transcripts, which can hold private data. Sources are memory, documents, code and, right after a compaction, the summary the session already holds. Keep out of the documents anything in that summary a project forbids copying.

## Init, when `docs/core` does not exist

1. Inventory before writing: the memory index and its goal entries, READMEs, `CLAUDE.md`, existing indexes, registries, ledgers, decision records, the top folders (`restructure.py map` for a large project), and the titles of this project's sessions if a session list tool exists. Such a list can span every project, so keep only rows whose folder is this root. Note what already owns each kind of fact.
2. Propose the areas (at most 8) and the document list in one message, and wait for a yes. Take the final goal only from the user's words. If none exist, ask for it. Never infer one.
3. Write the documents and `core.json`. Keep `sources` narrow, such as `src/*.py` and `~memory`, and put data folders in `skip`. Work outside `sources` still shows in `status`, by folder.
4. Run `lint`, fix what it flags, then `stamp`.
5. Offer a daily refresh. A scheduled task is standing configuration, so ask first. Create one task per project, named after it, with the scheduled task tool and a self-contained prompt: `Read <copy>/skills/compass/SKILL.md and follow its Refresh section for <absolute project root>, reading ${CLAUDE_PLUGIN_ROOT} as <copy>. End with at most 5 lines.` `<copy>` is the folder `ensure` printed. The prompt reads the file because this skill runs only when typed.

## Refresh, the default

1. Run `status`. If `STALE 0` and `ELSEWHERE 0`, say so in one line and stop. It records when it looked, and `stamp` uses that time, so a file saved during the refresh stays stale for the next one.
2. For each stale document, read the changed files it lists, and the memory entries when `~memory` changed. A path prints once, under the first document that depends on it: "listed above" means those paths apply here too, and "not listed" means read the folder or narrow the source. Change only what is no longer true, and add what memory or the documents show was ruled out. A stale document that is still right stays as it is, except that a document written before one of the rules above is brought up to it now.
3. Read the `document` lines: writing that changed outside every source, newest first. Open at most 10 whose names suggest a goal, a principle, a design decision or a new functional area, and move what must survive into the core documents. A lasting area no document covers gets `features/<area>.md`, a `core.json` entry with narrow `sources` and an index line, without asking, up to 8 areas in all, so merge before going past that. Say so in the report.
4. Never edit the first section of `goals.md` on your own. A result, a document or a fork's work cannot move a final goal: report the conflict instead. Only after the user says so, edit it and use `stamp --accept-goal`.
5. Run `lint` and do what each finding's last column says, editing nothing else.
6. For each work rule in the index with no enforcer in parentheses, look for the mistake it guards in what changed: new entries in the project's problem log, reverts in git, feedback memories. When it happened again, `<root>/queue` exists and no open ticket in `queue/board.json` already proposes it, open a ticket proposing the enforcement by the design skill's step 5b, with `--default "keep the rule as written"`, and name it in the report.
7. Write the drift in one line, at most 200 characters in the user's language: areas with recent changes that serve no listed goal, and goals with no recent work. The `elsewhere` lines show where work happened outside every document's sources, by folder and count. Judge from those, and open a file only when a folder name does not say what it holds. A folder of generated or vendor files goes in `skip`, and a folder a document should follow goes in its `sources`. Run `stamp --drift "<line>"`, or `--drift ""` when there is none, also when nothing was stale. Every session receives this line.
8. Report the documents changed and added, the tickets opened, and the drift.

## Add a document

Add an entry with `file`, `brief` and `sources` to `core.json`, write the document, name it in `README.md`, then `lint` and `stamp <file>`.

## Whether sessions read them

`usage <root>` counts reads of each document in this project's own transcripts, leaving out sessions that ran `stamp`. It prints counts, never text. The index arrives through the hook, so a low count on `README.md` means nothing.

## In a fork

Forks read the same files, and a fork's compaction updates them the way any session's does. A fork does not run a refresh on its own. When the user settles a direction in a fork, that fork updates the matching document in the same step.
