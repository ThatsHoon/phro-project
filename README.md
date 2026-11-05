# phro-project

Keeps a long project on course. It does three jobs:

- Designs and audits the project's plugins: re-reads a plugin's structure from its files, decides where a new capability belongs, measures whether skills really get called, and proposes what to merge, delete or repair.
- Holds the project's core documents in `docs/core`: goals and architecture that every session and fork starts from, so local detail does not bury the original goal.
- Restructures the project's folders on request: maps every file and dependency, moves files, fixes references, adds tests for changed code, and writes the folder rules later sessions follow.

It has no universal structure, because plugins differ too much between projects.

Always-on cost, in every session of every project where the plugin is enabled:

- Five skill descriptions, about 329 tokens.
- One `SessionStart` hook, at start, clear and compaction. It reads `docs/core/README.md` and `docs/core/core.json` and prints the index, at most 2,500 characters. It makes no model call and never walks the project. Without `docs/core/README.md` it prints nothing.

## Skeleton

```
phro-project/
  .claude-plugin/plugin.json   manifest
  hooks/hooks.json             SessionStart: prints the core index
  skills/
    design/SKILL.md            before adding a capability: where it belongs, trigger prompts first
    route-test/SKILL.md        whether skills get called on real prompts, and who takes each miss
    audit/SKILL.md             after growth: merge, delete and repair proposals
    compass/SKILL.md           build and refresh docs/core
    restructure/SKILL.md       reorganize a whole project
      references/test_structure.py   copied into the project: enforces its folder rules
  scripts/
    project.py                 plugin structure, routing tests. Standard library only
    compass.py                 the hook; status, lint and stamp of docs/core. Standard library only
    restructure.py             map, plan, apply, verify and undo of a move. Standard library only
  fixtures/
    midwork.jsonl              synthetic mid-task conversation that route-test resumes
    make_midwork.py            regenerates it
  tests/
    test_project.py            seeded-defect test of project.py
    test_compass.py            seeded-defect test of compass.py
    test_restructure.py        a small real project moved end to end
    route_cases.json           this plugin's own routing cases, written by its author
```

| Part | Does | Does not |
|---|---|---|
| The three scripts | Read structure, list candidates, build tests, tally, check documents, move files, rewrite references | Judge |
| The five skills | Procedure, judgment, checking candidates, proposals, writing documents | Edit a plugin or move a project unasked |
| You | Pick proposals, give final goals, approve moves and spending | |

### Structure graph

Each `project.py` call reads the plugin's files into an in-memory SQLite graph and discards it on exit. The files are the source, so the graph cannot go stale.

- Nodes (`node`): skills, agents, commands, scripts, docs, other assets, config, MCP servers, eval cases.
- Edges (`edge`): `path` for a mentioned file, `import` for a Python import, `name` for a `plugin:name` reference or an eval grading a skill.

The graph answers structure questions such as what breaks if a part is removed. It does not route prompts: published results showed little gain from a graph for picking one skill, a single hop. No source measured the graph for structure questions, so this placement is a design judgment.

## Workflow

Each skill fires on its own or by name, such as `/phro-project:design`. Sections 1 to 3 follow a plugin's life. Sections 4 and 5 serve the project itself.

```
design -> build -> route-test -> use in real work -> audit -> apply on a copy -> route-test
compass init -> sessions start from docs/core -> scheduled refresh -> report drift
restructure -> map -> proposal, your yes -> phased moves -> tests -> folder rules in docs/core
```

### 1. Before adding a capability: `design`

1. Run `summary` and `check` to read the current structure.
2. Name the recurring problem and how a fix would look. If neither can be named, recommend not building.
3. Place the capability on the first row that fits:

   | Place | When |
   |---|---|
   | Don't build | The platform, an existing skill or one instruction line covers it |
   | Extend a part | It is an existing skill's job, reached by its existing trigger |
   | Script | The step is deterministic |
   | Skill | It needs judgment, and a user request can name it |
   | Skill with `paths` | It matters only when certain files are touched |
   | Hook | It must react to an event the model does not act on, and a miss was measured |
   | MCP | It needs a connection or shared state that running a script cannot give |

4. Write down the session token cost of a new skill and the descriptions it would overlap.
5. Write the trigger prompts before the code: about 8 that should fire and about 8 close misses.
6. Write the description last, in the words users really type.
7. After building, run `route-test` and report which existing cases broke.

### 2. Measuring calls: `route-test`

1. Collect cases. Prompts typed together with a skill command are best (`mine`), since the skill used is the label.
2. Collect competitors (`catalog`). Real misses come from the other installed skills.
3. Build the test on a scratch copy (`build`). The plugin itself is not touched.
4. State the estimate, get approval, then run `claude plugin eval`. A case costs about $0.15 at 55 skills on Opus.
5. Tally (`tally`): hits, other skill, cut off by the turn limit, no skill, false fires.
6. Act on the result:

   | Result | Next |
   |---|---|
   | Another skill took it | Read both descriptions, fix the overlap or merge |
   | No skill fired | Try the description, then `paths`, then a hook |
   | Cut off | Raise `--max-turns` and rerun |
   | False fire | Narrow the description |
   | All pass | Stop. Add nothing |

7. Compare before and after on the same cases, and report gains and breaks together.

### 3. After growth: `audit`

1. Run `summary` and `check`, and read earlier refusals in `<plugin>/.phro-project/rejected.md`.
2. Verify each scripted finding by opening the file.
3. Look for what the script cannot see: skills doing the same job in other words, rules that no longer match the code, steps that could be scripts.
4. Write at most 5 proposals, deletions and merges first, each with evidence, what breaks, the gain and the test. "Change nothing" is a valid result.
5. Apply only what you pick, on a copy or branch, and compare with `route-test`.
6. Record refusals with their reason in `rejected.md`, so they are not proposed again.

### Raising detection

Detection machinery is not stacked in advance. Measure per skill, and move a skill to the next step only after the current one failed.

1. Description: fix overlaps from the confusion table and add users' words. No extra cost per turn.
2. `paths`: tie a file-specific skill to those files.
3. Signal hook: only for skills that steps 1 and 2 could not fix. Not built.
4. Local search: only if descriptions overflow the listing budget. Not built.
5. Compliance after loading: grade whether the procedure was followed, using real skill bodies. Not measured.

### 4. Core documents: `compass`

| File in `docs/core` | Holds |
|---|---|
| `README.md` | The index sessions receive: final goal, mid-term goals, one line per area, folder rules. At most 40 lines |
| `goals.md` | The final goal in the user's own words, then mid-term and short-term goals |
| `architecture.md` | The whole system |
| `features/<area>.md` | One per largest functional area |
| `<name>.md` | A document the user asked for, with its brief in `core.json` |
| `structure.md`, `structure.json` | Folder rules, written by `restructure` |
| `core.json` | The documents and the sources each depends on |

1. Init: harvest memory, READMEs and top folders, propose at most 8 areas, wait for a yes. The final goal comes only from the user's words.
2. Every session and fork receives the index from the hook. They read the same files, so a fork's work reaches later sessions through them.
3. Refresh runs once a day as one scheduled session, set up only after you agree. `status` finds documents whose sources changed; with none stale it stops without further work.
4. A refresh changes only what is no longer true. It cannot edit the final goal: `lint` flags that as `goal-changed`, and only `stamp --accept-goal` after you say so clears it.
5. `lint` must pass before `stamp`. It flags AI marks and tells, change history, dates, overlong documents, dead paths and unlisted documents.

Session transcripts are never a source. Durable decisions are already in memory, documents and code, and transcripts can hold private data.

### 5. Restructuring: `restructure`

Fires when you ask. Nothing moves without your yes on the proposal.

1. Read: `map` lists folders, smells, fragile path code, clusters that import each other, and hubs.
2. Propose the structure you would choose for a new repository, not a patch of the current one. Wait for a yes.
3. Prepare: stop running jobs, take a checkpoint, record the test baseline, pin untested behaviour with tests.
4. Move in phases: `plan`, `apply`, fix the listed manual and fragile lines, `verify`, then the full test suite.
5. Add tests for any code whose logic changed. `verify` blocks on `no-test` until they exist.
6. Write the rules: `docs/core/structure.md`, `docs/core/structure.json` and a copy of `references/test_structure.py`, so a later break fails a test. Fix memory, schedules and settings that name old paths.

`verify` blocks on references lost, files missing, syntax errors and changed code without a test. It also lists old mentions, but passing is not enough: on one real move it passed while the suite had 41 failures, because tests built the path from the folder name in ways no scanner resolves. The suite decides.

`undo` restores the moved files and the original text of every edited file.

## One project at a time

The code is installed once for the user. What it reads and writes about a project stays with that project.

| What | Where |
|---|---|
| Core documents and their list | `<root>/docs/core` |
| The `~memory` source | that root's own folder, `~/.claude/projects/<slug>/memory` |
| Restructure plans and undo data | `<root>/.phro-project/restructure` |
| Refused audit proposals | `<plugin>/.phro-project/rejected.md` |
| Daily refresh | one scheduled task per project, naming its root |

- The hook reads `docs/core` in the session's own project folder and never walks up. A project without it gets nothing, even inside another project.
- `restructure` rewrites the text under `--also`, so it refuses the whole scheduled tasks folder, the projects folder and another project's folder.
- `compass` stops when the session has been working in another folder, so that conversation's context stays out of the documents. Open a session in the target project instead.
- `route-test` cases come from this project's own transcripts and stay with this project.
- Nothing from a project ships in this plugin. Skills and tests use made-up examples.

## `project.py` commands

Run as `python -I -X utf8 scripts/project.py <command>`.

| Command | Does |
|---|---|
| `summary <plugin> [--usage DIR]` | Node counts, tokens per skill, uses, eval cases, calls in transcripts |
| `check <plugin> [--usage DIR]` | Deterministic findings, one per line |
| `impact <plugin> <node>` | What breaks if it is removed, and what only it uses |
| `sql <plugin> "<select>"` | Read-only query on `node` and `edge` |
| `mine <transcripts_dir> <cases.json> [--alias a=b] [--only a,b]` | Labeled prompts from typed commands |
| `catalog <out.json> <skills_dir>... [--only a,b]` | Competitor skill descriptions |
| `build <plugin> <cases.json> <out_dir> [--stub] [--also out.json] [--history f.jsonl] [--max-turns N]` | A scratch copy with eval cases |
| `tally <result.json>...` | Hits, misses by cause, false fires |

Findings from `check` are candidates, not verdicts:

| Finding | Meaning |
|---|---|
| `broken-ref` | A running file points at a missing file |
| `duplicate-file`, `shared-lines` | A whole file repeated, or lines copied between instructions |
| `orphan` | A script or doc nothing points at |
| `no-eval`, `never-called` | No eval case grades it, or no call in the transcripts |
| `no-description`, `long-description`, `long-body` | No description, over 1,536 characters, over 500 lines |
| `overlap-candidate` | Descriptions share many words. `audit` judges the meaning |

On real plugins, `broken-ref` was often an example path in a skill about writing skills, and `duplicate-file` was often deliberate. That is why `audit` opens each file.

## `compass.py` commands

Run as `python -I -X utf8 scripts/compass.py <command>`.

| Command | Does |
|---|---|
| `hook` | Reads the event from standard input and prints the index. Called by `hooks/hooks.json` |
| `status <root>` | Per document: stale or current, sources changed, age, then `STALE n` |
| `lint <root>` | One finding per line, then `OK` or `FINDINGS n` |
| `stamp <root> [files] [--accept-goal]` | Records the review time and the goal's fingerprint |

## `restructure.py` commands

Run as `python -I -X utf8 scripts/restructure.py <command>`. State stays in `<root>/.phro-project/restructure/`.

| Command | Does |
|---|---|
| `map <root> [--out f] [--also dir]` | Whole-project map. Read-only apart from its JSON file |
| `plan <root> moves.json [--limit n]` | Lists every rewrite and manual line. Writes nothing |
| `apply <root> moves.json [--limit n]` | Moves files, rewrites references, saves what undo needs |
| `verify <root> <stamp> [--limit n]` | Compares dependencies, syntax and logic with the state before the move |
| `undo <root> <stamp> [--force]` | Reverses the move. Refuses over later edits without `--force` |

`moves.json` is `{"moves": {"old": "new"}, "also": ["dir"]}`.

## A routing test

```bash
python -I -X utf8 scripts/project.py mine <transcripts_dir> cases.json --only search,review
python -I -X utf8 scripts/project.py catalog catalog.json <skills_dir>
python -I -X utf8 scripts/project.py build <plugin> cases.json <out_dir> --stub --also catalog.json --history fixtures/midwork.jsonl
claude plugin eval "<out_dir>" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --model <model> -j 1 --json result.json
python -I -X utf8 scripts/project.py tally result.json
```

Always pass `--no-publish`. The cost ceiling is checked only before each run starts, so spend can pass it by as many runs as `-j` allows.

## Measurements so far

2026-11-05, Opus 5.5, 55 competing skills with stub bodies, one run per case.

| Test | Result |
|---|---|
| Prompts written by the author | 14/16 |
| Typed prompts, fresh session, 2 turns | 1/16 (7/16 if every cut-off run had hit) |
| Typed prompts, mid-task session, 2 turns | 1/16 (5/16 if every cut-off run had hit) |
| `search` description with users' verbs added, mid-task, 4 turns | 1/7 to 6/7 |
| Held-out prompt on that edit | 1/1 |
| False fires on close misses | 0/3 |

Author-written prompts inflated the rate, so they are not trusted. The description edit used prompts whose failures had been seen and only one held-out case, so it may be overfit. Stub bodies mean nothing here measures compliance after loading.

`restructure.py` on scratch copies of two real projects, 2026-11-05:

| Project | Move | Rewritten | Tests before | After the script alone | After fixing flagged lines | Undo |
|---|---|---|---|---|---|---|
| 1,382 files, Python and JS | 2 folders | 22 references in 18 files | 18/18 suites | 18/18 | Nothing flagged as manual | Same bytes |
| 98 files, a plugin | 1 folder holding all scripts | 27 references in 12 files | 404 tests, 1 failure | 41 failures, 1 error | 1 failure, the same one | Same bytes |

One pytest group was skipped in the first project because its environment variable was unset. Flagged lines in the second were fixed by a replacement script, not by a person. Both moves were small and chosen by the author, so neither is a whole-project restructure.

## Check and install

```bash
python -I -X utf8 tests/test_project.py
```

```bash
python -I -X utf8 tests/test_compass.py
```

```bash
python -I -X utf8 tests/test_restructure.py
```

```bash
claude plugin validate .
```

The installed copy comes from GitHub `main`. After a change, bump the version in `plugin.json`, push, then run:

```bash
claude plugin marketplace update phro-project
```

```bash
claude plugin update phro-project@phro-project
```

Then type `/reload-plugins` in a running session.

## Not done yet

- Use of the design, route-test and audit skills in real plugin work.
- `compass` on a real project, and the hook firing in a real session.
- `restructure` start to finish with a person choosing the structure. Only its script has met real projects, on scratch copies.
- Routing test of this plugin's own skills with `tests/route_cases.json`, 52 cases, about $8.
- Signal hook, local search and compliance grading.

## When to delete it

- The plugin: if two audits of real plugins produce no adopted proposal and no routing problem.
- `compass` and its hook: if after a month sessions still lose the goal with `docs/core` in place, or nobody opens the documents and the refresh changes nothing.
- `restructure`: if a real restructure takes more of your time with it than by hand, or its misses reach the test suite more than its manual list catches them.
