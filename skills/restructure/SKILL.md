---
name: restructure
description: Reorganizes a whole project's files and folders: maps every file and dependency, proposes the best structure without compromising for the current one, moves files, fixes references, adds tests for changed code, and writes the folder rules later sessions follow. 폴더 구조 정리, 디렉토리 리팩토링, 재구조화, 파일이 지저분해, 모듈화, directory structure refactoring.
disable-model-invocation: true
---

# restructure

Output: a target structure the user approves, then that structure in place with every reference working and its rules written down.

Script: `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/restructure.py <command>`. It prints paths and counts, never file contents. It works on any project, including one without git.

Be bold about the target and strict about the proof. The current layout never limits the proposal, and nothing counts as done until `verify` and the tests say so.

## 1. Read the project

1. Run `map <root>`, adding `--also <folder>` for anything outside the project that names its paths: this project's own memory folder, and the folder of each scheduled task that works on this project. Never the whole scheduled tasks folder or another project's folder, since a move here would rewrite their text. The script refuses those. Also read `.claude/settings*.json`, launch configs, cron lines and server paths that use these files.
2. Read the READMEs, and `docs/core` if it exists, to learn what each part is for. Read code where a cluster's purpose is unclear.
3. Use the clusters, not the folders, as the map of real modules. Hubs are candidates for shared code. Folder cycles, code in docs, duplicates and backups show what the layout hides.

## 2. Propose

Design the target as if the repository were new, then compare. Migration cost sets the order of the work, never the shape of the target. If a compromise seems necessary, propose the clean structure anyway and name the cost beside it.

1. Group by what the code does for the product: one folder per module, not per file type.
2. One home per concern: shared logic once, runtime apart from training, code never under docs, data and weights outside the source tree with a pointer.
3. Dependencies point one way, so no folder cycles. Each module has a clear entry point and can be tested alone.
4. Everything superseded goes to one `legacy/` with a short note on what replaced it. Deleting needs a separate yes for each item.
5. A newcomer can read the top level in a minute: about 8 folders, no loose files beyond the standard ones, names that say what is inside.

Show the tree with one line per folder, a folder-level mapping from old to new, what changes in the code beyond moves (such as replacing `sys.path` hacks or splitting a module), the deletions you want, and the phases. Wait for the user's yes. Do not move anything before it.

## 3. Prepare

1. Stop the world: no other session or fork may work in the project during `apply`. List sessions if a tool exists, and get the user's yes that they are idle. Running jobs that read these paths count too, and so do this project's scheduled tasks: pause them, and resume them after section 6.
2. Checkpoint: in a git repository use a new branch with a clean tree. Without git, `undo` covers moves and rewrites. Name in the proposal what it does not cover.
3. Baseline: run every existing test and save the results. A failure that exists now is recorded, not blamed on the move.
4. Pin first: before changing a file's logic, write a test at its current location that pins what it does now on real inputs. Run it, and break the code once to see the test fail.

## 4. Move, in phases

For each phase, usually one module at a time with shared code first:

1. Write `moves.json` and run `plan <root> moves.json`. Fix every error.
2. Run `apply <root> moves.json`. It moves files and rewrites relative imports, Python imports, links, path strings and the source patterns in `docs/core/core.json`, and it reports the rest.
3. Fix every `manual` line. Then read every `fragile` line: code that computes paths breaks silently when a file changes depth. Replace `sys.path` hacks and `parents[N]` chains with real imports or one configured root. Change the code freely here.
4. Run `verify <root> <stamp>`. `LOST`, `MISSING`, `SYNTAX` and `NO-TEST` must reach 0. Read every `OLD-MENTION` as well, and fix it or say why it stays.
5. Run the whole test suite and compare with the baseline. A new failure means fix it or `undo <root> <stamp>`, never move on.

## 5. Tests for changed code

`verify` lists code whose logic changed and new code. Each needs a test that imports it and would fail if it broke. Mechanical import and path edits are covered by `verify` and the existing suite. A split or rewrite gets a real test against the pinned behaviour.

## 6. Write the rules

1. Write `docs/core/structure.md`: the tree with one line per folder, where a new file of each kind goes, how to run things, and the rules (what must not import what, what is banned). State what is true now, with no history. If `docs/core/README.md` exists, name it there, with the folder rules in a line or two. Sessions receive that file through the plugin hook. If it does not exist, create it with those lines and a `core.json`, as the `compass` skill describes.
2. Write the same rules as data in `docs/core/structure.json`, and copy `${CLAUDE_PLUGIN_ROOT}/skills/restructure/references/test_structure.py` to `tests/test_structure.py`. Run it. From then on, a file in the wrong place fails the suite.
3. Add `structure.md` to the `docs` list in `core.json` with empty `sources`, since the test holds it to the code. Run `compass.py lint <root>` from the same scripts folder, fix what it flags, then `compass.py stamp <root>`. Without the list entry `lint` fails and every later refresh stops at `stamp`.
4. Fix what still names old paths: memory, scheduled tasks, settings, server scripts.
5. Report: before and after top-level folders and file counts, references rewritten, tests before and after, what is left, and the state of each `verify` count.
