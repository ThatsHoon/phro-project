# phro-project

Keeps a long project on course. It does three jobs:

- Adds capabilities to the project when told to: finds what already exists, places it in that project, adds the rule that makes later sessions use it, and measures whether it fires. It also audits the project's plugins and proposes what to merge, delete or repair.
- Holds the project's core documents in `docs/core`: goals and architecture that every session and fork starts from, so local detail does not bury the original goal.
- Restructures the project's folders on request: maps every file and dependency, moves files, fixes references, adds tests for changed code, and writes the folder rules later sessions follow.

It has no universal structure, because plugins differ too much between projects.

Always-on cost, in every session of every project where the plugin is enabled:

- One skill description, `design`, about 80 tokens. The other four skills are not listed, so they cost nothing until you type them.
- One `SessionStart` hook, at start, clear and compaction. It reads `docs/core/README.md` and `docs/core/core.json` and prints the index, cut at 2,500 characters, plus a few lines. What it prints enters the session's context, although `claude plugin details` counts a hook as free. It makes no model call and never walks the project. Without both files it prints nothing. An index holding hidden text prints one warning instead. After a compaction it adds about 150 tokens telling the session to update the documents from its summary, which can take that session a few turns.

## 새 프로젝트에서 쓰기

이 플러그인은 원본입니다. 프로젝트에서 처음 쓰면 `~/phro-project_<프로젝트>`에 복제본이 생기고, 그 프로젝트에서는 복제본만 켜집니다. 프로젝트가 만든 것은 복제본과 프로젝트 안에 남고 원본은 그대로라, 원본은 언제든 그대로 푸시할 수 있습니다.

1. 프로젝트 폴더에서 새 세션을 열고 `/phro-project:compass`를 입력합니다. 복제본이 생기고 작업이 이어집니다. 다른 폴더에서 작업하던 세션은 맥락이 섞이지 않게 멈춥니다.
2. 끝나면 `/reload-plugins`를 입력합니다. 이후 이 프로젝트에서는 `/phro-project_<프로젝트>:compass`처럼 부릅니다.
3. 영역 제안을 확인합니다. 최종 목표는 사용자의 말로만 적으니, 없으면 물을 때 한두 문장으로 답합니다.
4. 매일 갱신 예약을 물으면 답합니다. 예약 작업은 프로젝트마다 하나입니다.
5. 평소에는 그대로 일합니다. `/compact`나 자동 압축이 일어나면 그 세션이 압축 요약을 핵심 문서와 대조해 바뀐 결정을 반영합니다. 프로젝트 폴더의 세션과 포크 모두 같습니다. `/compact` 뒤에는 다음 메시지를 보낼 때 진행됩니다.
6. 원본이 새 판이 되면 세션 시작 때 알려 줍니다. `clone.py sync`를 돌리면 원본에서 바뀐 파일만 가져오고, 프로젝트가 더하거나 고친 파일은 그대로 둡니다.

| 언제 | 무엇을 |
|---|---|
| 목표가 바뀌었을 때 | 그렇게 말하면 해당 문서를 같은 단계에서 고칩니다 |
| 폴더가 지저분할 때 | `/phro-project:restructure` |
| 이 프로젝트에 능력을 더할 때 | "이 방식을 스킬로 만들고 앞으로 설계 때마다 호출해", "hwp 읽는 플러그인 찾아서 내장해", 또는 `/phro-project:design` |
| 스킬이 안 불리는 것 같을 때 | `/phro-project:route-test`, 비용을 먼저 알려 줍니다 |
| 플러그인이 커졌을 때 | `/phro-project:audit` |

제대로 돌면 이렇게 보입니다.

- 새 세션에서 "이 프로젝트 최종 목표가 뭐야?"라고 물으면 파일을 열지 않고 답합니다.
- 갱신이 멈추면 세션 시작 때 경고가 나옵니다.
- 다른 폴더의 세션은 이 문서를 모릅니다.
- 한 번 시킨 능력은 이 프로젝트의 복제본과 색인의 작업 규칙에 남고, 다른 프로젝트에는 나타나지 않습니다.
- 원본 저장소의 `git status`는 어떤 프로젝트에서 무엇을 해도 깨끗합니다.

## Skeleton

```
phro-project/
  .claude-plugin/plugin.json   manifest
  hooks/hooks.json             SessionStart: prints the core index
  skills/
    design/SKILL.md            adding a capability: find it, place it, build it, make it fire
    route-test/SKILL.md        whether skills get called on real prompts, and who takes each miss
    audit/SKILL.md             after growth: merge, delete and repair proposals
    compass/SKILL.md           build and refresh docs/core
    restructure/SKILL.md       reorganize a whole project
      references/test_structure.py   copied into the project: enforces its folder rules
  scripts/
    project.py                 plugin structure, routing tests. Standard library only
    clone.py                   a project's own copy of this plugin: ensure and sync. Standard library only
    compass.py                 the hook; status, lint, stamp and usage of docs/core. Standard library only
    restructure.py             map, plan, apply, verify and undo of a move. Standard library only
  fixtures/
    midwork.jsonl              synthetic mid-task conversation that route-test resumes
    make_midwork.py            regenerates it
  tests/
    test_project.py            seeded-defect test of project.py
    test_clone.py              a project's copy: the original stays unchanged, projects stay apart, sync keeps the project's files
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

`design` fires on its own or by name. `compass`, `route-test`, `audit` and `restructure` run only when you type them. The core documents also update after every compaction, without a skill call. When `design` or `audit` reaches its routing step, it reads the `route-test` instructions directly. Sections 1 to 3 follow a plugin's life. Sections 4 and 5 serve the project itself.

```
design -> build -> route-test -> use in real work -> audit -> apply on a copy -> route-test
compass init -> sessions start from docs/core -> scheduled refresh -> report drift
restructure -> map -> proposal, your yes -> phased moves -> tests -> folder rules in docs/core
```

### 1. Adding a capability: `design`

Say what the project should be able to do, and from when: "make this method a skill and call it after every design draft", "find a plugin that reads this file type and add it to this project".

1. Run `summary` and `check` to read the current structure. A project root means the skills in its `.claude`.
2. Name the problem and how a fix would look. If neither can be named, recommend not building.
3. Look for what exists: installed skills and plugins, the plugin catalog, tools on the machine, packages. A skill only you can invoke cannot be called by the model, and a skill that waits for your answers stops an automatic flow, so its method goes into a project skill instead.
4. For anything from outside, check the publisher, license, what it runs and whether project files leave the machine. You approve the exact item, and it installs for this project only with `--scope local` or `--scope project`.
5. Place the capability on the first row that fits:

   | Place | When |
   |---|---|
   | Don't build | The platform or an installed skill does it and fires when it should |
   | Install | Someone built it, and it passed step 4 |
   | Extend a part | It is an existing skill's job, reached by its existing trigger |
   | Standing rule | "When X, do Y", where the model can see X. One line in the core index's work rules, or `.claude/rules` without `docs/core` |
   | Script | The step is deterministic |
   | Skill only you invoke | It needs judgment, and it moves files, spends money or is always typed |
   | Skill | It needs judgment, and a user request can name it |
   | Skill with `paths` | It matters only when certain files are touched |
   | Hook | It must react to an event the model does not act on, and a miss was measured |
   | MCP | It needs a connection or shared state that running a script cannot give |

6. A project's skill goes in that project's copy of this plugin, never in the original, so no other project receives it and a sync cannot overwrite it.
7. Write down the session token cost of a new skill and the descriptions it would overlap.
8. Write how it fires before the code. From a prompt: about 8 that should fire and about 8 close misses, in the copy's `tests/route_cases.json`. From a step in the work: cases with `after` and `rule`.
9. Write the description last, in the words users really type.
10. Build it if you asked for that, with its standing rule in the same step.
11. Run `route-test` once you approve the cost, and report which existing cases broke. Until then it is reported as built and not measured.

### 2. Measuring calls: `route-test`

1. Collect cases. Prompts typed together with a skill command are best (`mine`), since the skill used is the label.
2. Collect competitors (`catalog`). Real misses come from the other installed skills.
3. Build the test on a scratch copy (`build`). The plugin itself is not touched.
4. State the estimate, get approval, then run `claude plugin eval`. A case costs about $0.15 at 55 skills on Opus.
5. Tally (`tally`): hits, other skill, unresolved, no skill, wrong order, false fires. A run that ended abnormally with no skill called, by turn limit, timeout or API error, is unresolved, not a miss. A skill meant to fire after a step is graded by `tool_order`, so a call at the wrong moment is not a hit. An eval reads no project configuration, so its standing rule arrives through `append_system_prompt`.
6. Act on the result:

   | Result | Next |
   |---|---|
   | Another skill took it | Read both descriptions, fix the overlap or merge |
   | No skill fired | Try the description, then `paths`, then a hook |
   | Unresolved | Rerun, with a higher `--max-turns` if the turn limit ended it |
   | Wrong order | Sharpen the wording of the standing rule |
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
| `README.md` | The index sessions receive: final goal, mid-term goals, one line per area saying when to read it, the names of what was ruled out, work rules, folder rules. At most 40 lines |
| `goals.md` | The final goal in the user's own words, then mid-term and short-term goals, then what was ruled out: what, why, what replaces it, and the condition for looking again |
| `architecture.md` | The whole system, then the design principles every part follows |
| `features/<area>.md` | One per largest functional area |
| `<name>.md` | A document the user asked for, with its brief in `core.json` |
| `structure.md`, `structure.json` | Folder rules, written by `restructure` |
| `core.json` | The documents and the sources each depends on |

1. Init: list what already owns each kind of fact in memory, READMEs, registries and ledgers, propose at most 8 areas, wait for a yes. The final goal comes only from the user's words. Documents link to an existing owner instead of copying it, and hold what cannot be found by looking.
2. Every session and fork receives the index from the hook, with how long ago the documents were reviewed and how long ago changes were checked. The hook also says that the user outranks the documents, and that on a fact about the code the code wins and the mismatch gets reported. They read the same files, so a fork's work reaches later sessions through them.
3. Refresh runs once a day as one scheduled session, set up only after you agree. `status` finds documents whose sources changed, and lists what changed outside every source: a count per folder, and the documents by name. With neither it stops without further work. It opens up to 10 of those documents, moves what must survive into the core documents, and may add a new area document on its own, up to 8 areas, saying so in its report. It records when it looked, and `stamp` uses that time, so a file saved during a refresh stays stale for the next one. The hook warns when nothing has looked for longer than `refresh_days` plus one day, not merely when a quiet project had nothing to change.
4. A refresh changes only what is no longer true. It cannot edit the final goal: `lint` flags that as `goal-changed`, and only `stamp --accept-goal` after you say so clears it.
5. `lint` must pass before `stamp`. It flags AI marks and tells, change history, dates, overlong documents, dead paths, unlisted documents, source patterns that match no file and hidden text in the index. A `>` quote is the user's own words, so it is never flagged for wording.
6. After `/compact` or an automatic compaction, in any session or fork of the project, the hook tells that session to compare its summary with the documents and update what is out of date, then run `lint`. A summary reaches a hook only on `PostCompact`, which cannot call a model or reach the session, so the session that already holds the summary does the work and no extra run is paid for. After a manual `/compact` this happens with your next message. A session that ends first updates nothing, and whether sessions follow the instruction is not measured.
7. A refresh ends with `stamp --drift "<line>"`: work that serves no listed goal, and goals with no recent work, in at most 200 characters. The folder list from `status` is what it judges this from. Every session receives this line after the index, about 50 tokens.

Session transcripts are never a source for these documents, since they can hold private data. Sources are memory, documents, code and, right after a compaction, the summary the session already holds. `usage` is the one script that opens them: it counts reads of each document and prints no text.

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

The original is installed once for the user and holds no project. A project's first use of any skill runs `clone.py ensure`, which copies the plugin to `~/phro-project_<project>`, registers that copy as its own marketplace and turns it on with `--scope local`, and turns the original off with `--scope local`. The copy loads in place, so an edit needs only `/reload-plugins`. Two projects with one folder name get separate copies, and a copy refuses to run for any project but its own. Inside the plugin's own folder, no copy is made.

| What | Where |
|---|---|
| The project's copy of this plugin | `~/phro-project_<project>`, with `origin.json` recording its project and the original's files |
| Core documents and their list | `<root>/docs/core` |
| The `~memory` source | that root's own folder, `~/.claude/projects/<slug>/memory` |
| Restructure undo data | `<root>/.phro-project/restructure`, ignored by git through its own `.gitignore` |
| Refused audit proposals | `<plugin>/.phro-project/rejected.md` |
| Daily refresh | one scheduled task per project, naming its root |
| Skills built on request | the project's copy: `skills/<name>`, with cases in `tests/route_cases.json` |
| Standing rules | the work rules in `<root>/docs/core/README.md`, or `<root>/.claude/rules` |

- The hook reads `docs/core` in the session's own project folder, which is the folder you opened, and never walks up. A project without `README.md` and `core.json` there gets nothing, even inside another project.
- `restructure` rewrites the text under `--also`, so it refuses the whole scheduled tasks folder, the projects folder and another project's folder.
- `compass` stops when the session has been working in another folder, so that conversation's context stays out of the documents. Open a session in the target project instead.
- A cloned repository's `docs/core` enters the session the way its `CLAUDE.md` does. The hook refuses an index with an HTML comment, an invisible character or a hidden element. Plain instructions in the text are not detected.
- `route-test` cases come from this project's own transcripts and stay with this project.
- Nothing from a project ships in this plugin. Skills and tests use made-up examples.

## `project.py` commands

Run as `python -I -X utf8 scripts/project.py <command>`. `<plugin>` is a plugin folder, or a project root, which means the skills in its `.claude`.

| Command | Does |
|---|---|
| `summary <plugin> [--usage DIR]` | Node counts, tokens per skill, uses, eval cases, calls in transcripts |
| `check <plugin> [--usage DIR]` | Deterministic findings, one per line |
| `impact <plugin> <node>` | What breaks if it is removed, and what only it uses |
| `sql <plugin> "<select>"` | Read-only query on `node` and `edge` |
| `mine <transcripts_dir> <cases.json> [--alias a=b] [--only a,b]` | Labeled prompts from typed commands |
| `catalog <out.json> <skills_dir>... [--only a,b]` | Competitor skill descriptions |
| `build <plugin> <cases.json> <out_dir> [--stub] [--also out.json] [--history f.jsonl] [--max-turns N]` | A scratch copy with eval cases. A case may add `after` and `rule` for a skill that fires from a step |
| `tally <result.json>...` | Hits, misses by cause, wrong order, false fires |

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
| `status <root>` | Per document: stale or current, sources changed, age. Lists up to 100 new paths per document, each path once, and says how many it left out. Then folders with changes outside every source since the oldest review, up to 30 changed documents among them by name, `ELSEWHERE n` and `STALE n`. Records when it looked |
| `lint <root>` | One finding per line, then `OK` or `FINDINGS n` |
| `stamp <root> [files] [--accept-goal] [--drift line]` | Records the review time, the goal's fingerprint and the drift line |
| `usage <root>` | Reads of each document in this project's transcripts, leaving out sessions that ran `stamp`. Counts only |

## `clone.py` commands

Run as `python -I -X utf8 scripts/clone.py <command>`.

| Command | Does |
|---|---|
| `ensure <root> [--dest dir] [--no-register]` | Prints `CREATED`, `EXISTS`, `THIS`, `ORIGINAL`, `WRONG-PROJECT` or `NO-PROJECT`, and what to do. Every skill runs it first |
| `sync [--from dir]` | Run from a copy. Takes a newer original: files the project did not touch are updated, new ones added, dropped ones removed. Files the project added stay, and files it changed stay and print as `kept` |

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

The core index on a made-up project, 2026-11-05, Opus 5.5, with the hook and without it. Both had the documents on disk. 32 runs, new sessions only:

| Test | With | Without |
|---|---|---|
| Flags work the project ruled out, including on 290 files | 6/6 | 6/6 |
| Goal, priority, code over document, area document, no false alarm | 5/5 | 5/5 |
| Open question that does not look like it needs the repository | 4/4 | 0/4 |
| Repeats a wrong value from the index without opening the code | 5/16 | 0/16 |
| Turns on the same tasks | 49 | 85 |
| Cost on the same tasks | $1.01 | $1.10 |

Flagging ruled-out work came from `goals.md`, which both sides read, not from the hook. The hook saved turns and changed one open question. The project and the rubrics were written by the author, and long mid-task sessions were not measured, so this does not settle whether sessions keep the goal.

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
python -I -X utf8 tests/test_clone.py
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

## Work queue

Sessions and forks that need the owner's decision file a ticket instead of waiting in a chat nobody reads. Tickets are Markdown files in `<project>/queue/`, its own git repository; `scripts/queue.py` is the only writer and commits only the paths it wrote, so sessions writing at once never commit each other's files.

- `/phro-project:init`: once per project. Makes the project's copy, the queue, the first inbox page (a private Artifact) and turns on the panel.
- The session rule: when `queue/board.json` exists, the compass hook tells every session to open a ticket with `queue.py ask`, ask in chat too, and record the first answer with `queue.py decide`.
- `/phro-project:inbox [id]`: walks the waiting tickets one at a time, highest priority first, records each answer and tells the waiting session to read its ticket.
- A decided question is not asked again. A ticket returned three times becomes the question "close it or shrink it to one step".

`queue.py` commands: `init`, `ask`, `decide`, `move`, `done`, `board`; each prints `OK` or `REFUSED` with a path, never a ticket body.

### The queue panel

The panel (`mods/queue-panel/`) reads `queue/board.json` only: waiting decisions with an answer button that fills `/inbox <id>`, running work with the last line of its record, blocked and thrice-returned counts, and what closed in 7 days. How to turn it on is written here when the panel ships.

## Not done yet

- Use of the design, route-test and audit skills in real work. A capability built on request and a step-triggered skill have only passed scripted checks, not a run with a model.
- A scheduled `compass` refresh, and a session in a second project. One project has its documents, and the hook has fired there.
- `restructure` start to finish with a person choosing the structure. Only its script has met real projects, on scratch copies.
- Routing test of this plugin's own skills with `tests/route_cases.json`, 28 cases, about $4. The cases are written by the author, which inflates the rate, so it waits for prompts really typed.
- A hook at write time that enforces folder rules, a search over the documents, signal hook, local search and compliance grading. Each waits for a measured failure.

## When to delete it

- The work queue: if fewer than 5 decisions close through it in its first two weeks; remove `queue.py`, the `init` and `inbox` skills, the panel and the hook rule.
- The plugin: if two audits of real plugins produce no adopted proposal and no routing problem.
- `compass` and its hook: if after a month sessions still lose the goal with `docs/core` in place, or `usage` shows nobody opens the documents and the refresh changes nothing.
- `restructure`: if a real restructure takes more of your time with it than by hand, or its misses reach the test suite more than its manual list catches them.
