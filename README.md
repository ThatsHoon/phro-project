# phro-project

Keeps a long project on course. It does three jobs:

- Designs and audits the project's plugins: re-reads a plugin's structure from its files, decides where a new capability belongs, measures whether skills really get called, and proposes what to merge, delete or repair.
- Holds the project's core documents in `docs/core`: goals and architecture that every session and fork starts from, so local detail does not bury the original goal.
- Restructures the project's folders on request: maps every file and dependency, moves files, fixes references, adds tests for changed code, and writes the folder rules later sessions follow.

It has no universal structure, because plugins differ too much between projects.

Always-on cost, in every session of every project where the plugin is enabled:

- Two skill descriptions, `compass` and `design`, about 160 tokens by `/skill-doctor`. The other three skills are not listed, so they cost nothing until you type them.
- One `SessionStart` hook, at start, clear and compaction. It reads `docs/core/README.md` and `docs/core/core.json` and prints the index, cut at 2,500 characters, plus a few lines. What it prints enters the session's context, although `claude plugin details` counts a hook as free. It makes no model call and never walks the project. Without both files it prints nothing. An index holding hidden text prints one warning instead.

## 새 프로젝트에서 쓰기

한 번 설치하면 모든 폴더에서 켜져 있고, 문서와 기록은 프로젝트마다 따로 둡니다.

1. 일부 프로젝트에서만 쓰려면 사용자 범위 설치를 지우고, 그 프로젝트 폴더에서 설치합니다. 안 쓰는 폴더에서 목록 비용 약 160토큰을 아낍니다.

   ```bash
   claude plugin install phro-project@phro-project --scope local
   ```

2. 그 폴더에서 새 세션을 열고 "핵심 문서 만들어줘"라고 합니다. 다른 폴더에서 작업하던 세션은 맥락이 섞이지 않게 멈춥니다.
3. 영역 제안을 확인합니다. 최종 목표는 사용자의 말로만 적으니, 없으면 물을 때 한두 문장으로 답합니다.
4. 매일 갱신 예약을 물으면 답합니다. 예약 작업은 프로젝트마다 하나입니다.
5. 평소에는 그대로 일합니다. 필요할 때만 부릅니다.

| 언제 | 무엇을 |
|---|---|
| 목표가 바뀌었을 때 | 그렇게 말하면 해당 문서를 같은 단계에서 고칩니다 |
| 폴더가 지저분할 때 | `/phro-project:restructure` |
| 이 프로젝트의 플러그인에 기능을 더할 때 | "이 기능 어디에 넣을까" 또는 `/phro-project:design` |
| 스킬이 안 불리는 것 같을 때 | `/phro-project:route-test`, 비용을 먼저 알려 줍니다 |
| 플러그인이 커졌을 때 | `/phro-project:audit` |

제대로 돌면 이렇게 보입니다.

- 새 세션에서 "이 프로젝트 최종 목표가 뭐야?"라고 물으면 파일을 열지 않고 답합니다.
- 갱신이 멈추면 세션 시작 때 경고가 나옵니다.
- 다른 폴더의 세션은 이 문서를 모릅니다.

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
    compass.py                 the hook; status, lint, stamp and usage of docs/core. Standard library only
    restructure.py             map, plan, apply, verify and undo of a move. Standard library only
  fixtures/
    midwork.jsonl              synthetic mid-task conversation that route-test resumes
    make_midwork.py            regenerates it
  tests/
    test_project.py            seeded-defect test of project.py
    test_compass.py            seeded-defect test of compass.py
    test_restructure.py        a small real project moved end to end, then compass and restructure together
    route_cases.json           this plugin's own routing cases, written by its author. `check` counts them as evals
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

`compass` and `design` fire on their own or by name, such as `/phro-project:design`. `route-test`, `audit` and `restructure` run only when you type them, because they spend money or move files. When `design` or `audit` reaches its routing step, it reads the `route-test` instructions directly. Sections 1 to 3 follow a plugin's life. Sections 4 and 5 serve the project itself.

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
   | Skill only you invoke | It needs judgment, and it moves files, spends money or is always typed |
   | Skill | It needs judgment, and a user request can name it |
   | Skill with `paths` | It matters only when certain files are touched |
   | Hook | It must react to an event the model does not act on, and a miss was measured |
   | MCP | It needs a connection or shared state that running a script cannot give |

4. Write down the session token cost of a new skill and the descriptions it would overlap.
5. Write the trigger prompts before the code: about 8 that should fire and about 8 close misses, in `tests/route_cases.json`.
6. Write the description last, in the words users really type.
7. After building, run `route-test` and report which existing cases broke.

### 2. Measuring calls: `route-test`

1. Collect cases. Prompts typed together with a skill command are best (`mine`), since the skill used is the label.
2. Collect competitors (`catalog`). Real misses come from the other installed skills.
3. Build the test on a scratch copy (`build`). The plugin itself is not touched.
4. State the estimate, get approval, then run `claude plugin eval`. A case costs about $0.15 at 55 skills on Opus.
5. Tally (`tally`): hits, other skill, unresolved, no skill, false fires. A run that ended abnormally with no skill called, by turn limit, timeout or API error, is unresolved, not a miss.
6. Act on the result:

   | Result | Next |
   |---|---|
   | Another skill took it | Read both descriptions, fix the overlap or merge |
   | No skill fired | Try the description, then `paths`, then a hook |
   | Unresolved | Rerun, with a higher `--max-turns` if the turn limit ended it |
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
| `README.md` | The index sessions receive: final goal, mid-term goals, one line per area saying when to read it, the names of what was ruled out, folder rules. At most 40 lines |
| `goals.md` | The final goal in the user's own words, then mid-term and short-term goals, then what was ruled out: what, why, what replaces it, and the condition for looking again |
| `architecture.md` | The whole system |
| `features/<area>.md` | One per largest functional area |
| `<name>.md` | A document the user asked for, with its brief in `core.json` |
| `structure.md`, `structure.json` | Folder rules, written by `restructure` |
| `core.json` | The documents and the sources each depends on |

1. Init: list what already owns each kind of fact in memory, READMEs, registries and ledgers, propose at most 8 areas, wait for a yes. The final goal comes only from the user's words. Documents link to an existing owner instead of copying it, and hold what cannot be found by looking.
2. Every session and fork receives the index from the hook, with how long ago the documents were reviewed and how long ago changes were checked. The hook also says that the user outranks the documents, and that on a fact about the code the code wins and the mismatch gets reported. They read the same files, so a fork's work reaches later sessions through them.
3. Refresh runs once a day as one scheduled session, set up only after you agree. `status` finds documents whose sources changed, and lists by folder what changed outside every source; with neither it stops without further work. It records when it looked, and `stamp` uses that time, so a file saved during a refresh stays stale for the next one. The hook warns when nothing has looked for longer than `refresh_days` plus one day, not merely when a quiet project had nothing to change.
4. A refresh changes only what is no longer true. It cannot edit the final goal: `lint` flags that as `goal-changed`, and only `stamp --accept-goal` after you say so clears it.
5. `lint` must pass before `stamp`. It flags AI marks and tells, change history, dates, overlong documents, dead paths, unlisted documents, source patterns that match no file and hidden text in the index. A `>` quote is the user's own words, so it is never flagged for wording.
6. A refresh ends with `stamp --drift "<line>"`: work that serves no listed goal, and goals with no recent work, in at most 200 characters. The folder list from `status` is what it judges this from. Every session receives this line after the index, about 50 tokens.

Session transcripts are never a source for these documents. Durable decisions are already in memory, documents and code, and transcripts can hold private data. `usage` is the one script that opens them: it counts reads of each document and prints no text.

### 5. Restructuring: `restructure`

Fires when you ask. Nothing moves without your yes on the proposal.

1. Read: `map` lists folders, smells, fragile path code, clusters that import each other, and hubs.
2. Propose the structure you would choose for a new repository, not a patch of the current one. Wait for a yes.
3. Prepare: stop running jobs, pause this project's scheduled tasks, take a checkpoint, record the test baseline, pin untested behaviour with tests.
4. Move in phases: `plan`, `apply`, fix the listed manual and fragile lines, `verify`, then the full test suite. `apply` also moves the source patterns in `docs/core/core.json`, including a file that leaves a pattern's folder, so later edits still mark documents stale.
5. Add tests for any code whose logic changed. `verify` blocks on `no-test` until they exist.
6. Write the rules: `docs/core/structure.md`, `docs/core/structure.json` and a copy of `references/test_structure.py`, so a later break fails a test. Plant one violation of each rule to prove it can fail. A misspelled rule name fails the test. Add `structure.md` to `core.json`, then run compass `lint` and `stamp`. Fix memory, schedules and settings that name old paths.

`verify` blocks on references lost, files missing, syntax errors and changed code without a test. It also lists old mentions, but passing is not enough: on one real move it passed while the suite had 41 failures, because tests built the path from the folder name in ways no scanner resolves. The suite decides.

`undo` restores the moved files and the original text of every edited file. It refuses without `--force` if any of them changed after `apply`, including a file edited under `--also`.

## One project at a time

The code is installed once for the user. What it reads and writes about a project stays with that project.

| What | Where |
|---|---|
| Core documents and their list | `<root>/docs/core` |
| The `~memory` source | that root's own folder, `~/.claude/projects/<slug>/memory` |
| Restructure undo data | `<root>/.phro-project/restructure`, ignored by git through its own `.gitignore` |
| Refused audit proposals | `<plugin>/.phro-project/rejected.md` |
| Daily refresh | one scheduled task per project, naming its root |

- The hook reads `docs/core` in the session's own project folder, which is the folder you opened, and never walks up. A project without `README.md` and `core.json` there gets nothing, even inside another project.
- `restructure` rewrites the text under `--also`, so it refuses the whole scheduled tasks folder, the projects folder and another project's folder.
- `compass` stops when the session has been working in another folder, so that conversation's context stays out of the documents. Open a session in the target project instead.
- A cloned repository's `docs/core` enters the session the way its `CLAUDE.md` does. The hook refuses an index with an HTML comment, an invisible character or a hidden element. Plain instructions in the text are not detected.
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
| `no-eval`, `never-called`, `unmeasured` | No eval case or `tests/route_cases.json` entry grades it, or no call in the transcripts, or no transcripts to count in. A skill only the user invokes needs no eval |
| `no-description`, `long-description`, `long-body` | No description, over 1,536 characters, over about 5,000 tokens, where a compaction cuts a skill |
| `overlap-candidate` | Descriptions share many words. `audit` judges the meaning |

On real plugins, `broken-ref` was often an example path in a skill about writing skills, and `duplicate-file` was often deliberate. That is why `audit` opens each file.

## `compass.py` commands

Run as `python -I -X utf8 scripts/compass.py <command>`.

| Command | Does |
|---|---|
| `hook` | Reads the event from standard input and prints the index. Called by `hooks/hooks.json` |
| `status <root>` | Per document: stale or current, sources changed, age. Lists up to 100 new paths per document, each path once, and says how many it left out. Then folders with changes outside every source since the oldest review, `ELSEWHERE n` and `STALE n`. Records when it looked |
| `lint <root>` | One finding per line, then `OK` or `FINDINGS n` |
| `stamp <root> [files] [--accept-goal] [--drift line]` | Records the review time, the goal's fingerprint and the drift line |
| `usage <root>` | Reads of each document in this project's transcripts, leaving out sessions that ran `stamp`. Counts only |

## `restructure.py` commands

Run as `python -I -X utf8 scripts/restructure.py <command>`. State stays in `<root>/.phro-project/restructure/`.

| Command | Does |
|---|---|
| `map <root> [--out f] [--also dir]` | Whole-project map. Writes nothing unless `--out` names a file for the full JSON |
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
- A scheduled `compass` refresh, and a session in a second project. One project has its documents, and the hook has fired there.
- `restructure` start to finish with a person choosing the structure. Only its script has met real projects, on scratch copies.
- Routing test of this plugin's own skills with `tests/route_cases.json`, 36 cases, about $5. The cases are written by the author, which inflates the rate, so it waits for prompts really typed.
- A hook at write time that enforces folder rules, a search over the documents, signal hook, local search and compliance grading. Each waits for a measured failure.

## When to delete it

- The plugin: if two audits of real plugins produce no adopted proposal and no routing problem.
- `compass` and its hook: if after a month sessions still lose the goal with `docs/core` in place, or `usage` shows nobody opens the documents and the refresh changes nothing.
- `restructure`: if a real restructure takes more of your time with it than by hand, or its misses reach the test suite more than its manual list catches them.
