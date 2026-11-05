---
name: compass
description: Builds and refreshes a project's core documents in docs/core, the fixed point every session and fork starts from: final and mid-term goals, overall and per-feature architecture, documents the user names. 핵심 문서, 전체 구조 문서, 목표 정리, 문서 최신화, 아키텍처 요약, 방향 잃지 않게.
---

# compass

Output: short documents in `docs/core`, then at most 5 lines to the user. Long work buries the original goal under local detail. These documents hold what must survive that, so they stay short.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/compass.py <command> <project root>`.

A plugin hook prints `docs/core/README.md` at every session start and after every compaction, in every session and fork of the project. It is silent in a project without that file.

These documents belong to one project. Use only the project at the given root: its files, its own memory folder and its own sessions. If this session has been working in another folder, stop and ask the user to open a session in the target project, so this conversation's context stays out of the documents. A scheduled run starts clean, so it goes ahead.

## The documents

| File | Holds |
|---|---|
| `README.md` | The index sessions receive, at most 40 lines and 2,500 characters: final goal in a line or two, current mid-term goals, one line per area with its document, folder rules |
| `goals.md` | First section: the final goal in the user's own words, as `>` quotes. Then mid-term and short-term goals |
| `architecture.md` | The whole system: its parts, how data moves between them, where each runs |
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
- Link a document generated from a registry. Never rewrite it.
- Never read session transcripts. Sources are memory, documents and code: a session's durable decisions are already there, and transcripts can hold private data.

## Init, when `docs/core` does not exist

1. Harvest: the memory index and its goal entries, READMEs, existing indexes, the top folders (`restructure.py map` for a large project), and the titles of this project's sessions if a session list tool exists. Such a list can span every project, so keep only rows whose folder is this root.
2. Propose the areas (at most 8) and the document list in one message, and wait for a yes. Take the final goal only from the user's words. If none exist, ask for it. Never infer one.
3. Write the documents and `core.json`. Keep `sources` narrow, such as `src/*.py` and `~memory`, and put data folders in `skip`.
4. Run `lint`, fix what it flags, then `stamp`.
5. Offer a daily refresh. A scheduled task is standing configuration, so ask first. Create one task per project, named after it, with the scheduled task tool and a self-contained prompt: `Run /phro-project:compass refresh for <absolute project root>. End with at most 5 lines.`

## Refresh, the default

1. Run `status`. If `STALE 0`, say so in one line and stop.
2. For each stale document, read the changed files it lists, and the memory entries when `~memory` changed. Change only what is no longer true. A stale document that is still right stays as it is.
3. Never edit the first section of `goals.md` on your own. A result or a fork's work cannot move a final goal: report the conflict instead. Only after the user says so, edit it and use `stamp --accept-goal`.
4. Run `lint`, edit only what it flags, then `stamp`.
5. Report the documents changed and the drift: areas with recent changes that serve no listed goal, and goals with no recent work.

## Add a document

Add an entry with `file`, `brief` and `sources` to `core.json`, write the document, name it in `README.md`, then `lint` and `stamp <file>`.

## In a fork

Forks read the same files. One scheduled session refreshes them, so a fork does not run a refresh on its own. When the user settles a direction in a fork, that fork updates the matching document in the same step.
