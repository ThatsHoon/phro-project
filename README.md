# phro-project

Keeps a long project on course. It does three jobs:

- Adds capabilities to the project when told to: finds what already exists, places it in that project, adds the rule that makes later sessions use it, and measures whether it fires. It also audits the project's plugins and proposes what to merge, delete or repair.
- Holds the project's core documents in `docs/core`: goals and architecture that every session and fork starts from, so local detail does not bury the original goal.
- Restructures the project's folders on request: maps every file and dependency, moves files, fixes references, adds tests for changed code, and writes the folder rules later sessions follow.

It has no universal structure, because plugins differ too much between projects.

Always-on cost, in every session of every project where the plugin is enabled:

- One skill description, `design`, about 80 tokens. The other four skills are not listed, so they cost nothing until you type them.
- One `SessionStart` hook, at start, resume, clear and compaction. Resume matters for forks and sessions reopened after a restart, which otherwise never see a rule added since they began. It reads `docs/core/README.md` and `docs/core/core.json` and prints the index, cut at 2,500 characters, plus a few lines. What it prints enters the session's context, although `claude plugin details` counts a hook as free. It makes no model call and never walks the project. Without both files it prints nothing. An index holding hidden text prints one warning instead. After a compaction it adds about 150 tokens telling the session to update the documents from its summary, which can take that session a few turns.

## 새 프로젝트에서 쓰기

이 플러그인은 원본입니다. 프로젝트에서 처음 쓰면 `~/phro-project_<프로젝트>`에 복제본이 생기고, 그 프로젝트에서는 복제본만 켜집니다. 프로젝트가 만든 것은 복제본과 프로젝트 안에 남고 원본은 그대로라, 원본은 언제든 그대로 푸시할 수 있습니다.

1. 프로젝트 폴더에서 새 세션을 열고 `/phro-project:compass`를 입력합니다. 복제본이 생기고 작업이 이어집니다. 다른 폴더에서 작업하던 세션은 맥락이 섞이지 않게 멈춥니다.
2. 끝나면 `/reload-plugins`를 입력합니다. 이후 이 프로젝트에서는 `/phro-project_<프로젝트>:compass`처럼 부릅니다.
3. 영역 제안을 확인합니다. 최종 목표는 사용자의 말로만 적으니, 없으면 물을 때 한두 문장으로 답합니다.
4. 매일 갱신 예약을 물으면 답합니다. 예약 작업은 프로젝트마다 하나입니다.
5. 평소에는 그대로 일합니다. `/compact`나 자동 압축이 일어나면 그 세션이 압축 요약을 핵심 문서와 대조해 바뀐 결정을 반영합니다. 프로젝트 폴더의 세션과 포크 모두 같습니다. `/compact` 뒤에는 다음 메시지를 보낼 때 진행됩니다.
6. 원본에 새 커밋이나 새 판이 생기면 세션 시작 때 알려 줍니다. `phro-project_<프로젝트> update`라고 하면 바뀔 목록을 먼저 보여 주고, 원본에서 바뀐 파일만 가져옵니다. 프로젝트가 더하거나 고친 파일은 그대로 둡니다. 반영은 원본에서 사본 쪽으로만 갑니다. 사본에 더한 프로젝트 기능은 원본으로 올라가지 않습니다.

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
    update/SKILL.md            take a newer original into this project's copy, one way
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

6. A project's skill goes in that project's copy of this plugin, never in the original, so no other project receives it and a sync cannot overwrite it. Updates flow one way, from the original into copies; nothing in a copy goes back.
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
5. Tally (`tally`): hits, other skill, unresolved, no skill, wrong order, false fires. A run that ended abnormally with no skill called, by turn limit, timeout or API error, is unresolved, not a miss; `tally` names its kind and prints one rerun instruction per kind. `--save` keeps an accepted run's outcome per case with the model, Claude Code version and skill descriptions it ran on, and `--accepted` reports later runs against it, with a `stale` line when the model or a description changed. A case may name an MCP tool (`"tool"`) instead of a skill. A skill meant to fire after a step is graded by `tool_order`, so a call at the wrong moment is not a hit. An eval reads no project configuration, so its standing rule arrives through `append_system_prompt`.
6. Act on the result:

   | Result | Next |
   |---|---|
   | Another skill took it | Read both descriptions, fix the overlap or merge |
   | No skill fired | Try the description, then `paths`, then a hook |
   | Unresolved | Follow the `rerun` line for its kind: a higher `--max-turns`, one more try, a rerun as is, or read the error first |
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

### 6. Portable modules: `modularize`

Fires when you ask. Where `restructure` makes a layout a newcomer can read, this one makes a layout another team can lift piece by piece: one folder per feature with its contract, examples, weights manifest, `requirements.txt` and tests, grouped into one bundle per consumer plus `shared/`.

1. Facts first: features, consumers, the wires between them, outside needs and state, found from the code.
2. Decide the rest with you in rounds, then a spec and a step plan, each waiting for your yes.
3. Move in a worktree with `restructure.py`, one phase per commit. After each phase: `modularize.py imports` (0 forbidden), `modularize.py copytest <feature> --venv` (the feature copied alone, with only its own requirements, passes its tests) and the whole suite.
4. Last, `contracts/README.md` for outside readers, checked with `modularize.py contracts`.

The import rules live in `contracts/modules.json`: `{"features": {"control/hazard": ["shared", "control/geo"]}, "consumers": ["training", "examples", "tools"]}`. A feature imports itself and what its row lists; consumers may import features, never the reverse.

### 7. Working style: `lean`, `lean-review` and `grill`

`lean` is always on. A hook injects its rules from `skills/lean/SKILL.md`:

- Main session, at start, resume, clear and compact: **Build** (minimal code, root-cause fixes, one runnable check) and **Answer** (the action or the answer first, one open question at a time, short visible lists).
- Each subagent: **Build** and **Report** (status first, long output to a file, at most 20 lines).

Type `phro-lean off` as the whole prompt to stop the rules for the current session and its subagents, and `phro-lean on` to bring them back. Other sessions are not affected. lean injects nothing while the original ponytail or i-have-adhd plugin is enabled, so the two never stack; disable those and lean takes over.

`lean-review` fires when you ask for an over-engineering review. It lists what a diff could delete or shrink, one line per finding, and ends with the lines it could save.

Technical debt lives in the code: each shortcut and each skipped test carries one English comment, `lean: <ceiling> | when: <trigger> | fix: <upgrade path>`. `compass.py debt` counts them against the last refresh, the daily refresh adds `debt N (+K)` to the drift line every session receives, and only debt whose `when` is `owner` or already met becomes a work-queue ticket. `lean-review` reports markers whose `when` holds as `debt:` findings.

`grill` interviews you about a plan in rounds of at most 5 numbered questions. Each question has a recommended answer and one line of reason. The design tree is kept in a file, so a compaction does not lose it.

These three are reworded forks of MIT-licensed skills; see `THIRD_PARTY_NOTICES.md`.

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
| `summary <plugin> [--usage DIR]` | Node counts, tokens per skill, uses, eval cases, calls in transcripts, and Reads of each skill's reference files |
| `check <plugin> [--usage DIR]` | Deterministic findings, one per line |
| `impact <plugin> <node>` | What breaks if it is removed, and what only it uses |
| `sql <plugin> "<select>"` | Read-only query on `node` and `edge` |
| `mine <transcripts_dir> <cases.json> [--alias a=b] [--only a,b]` | Labeled prompts from typed commands |
| `catalog <out.json> <skills_dir>... [--only a,b]` | Competitor skill descriptions |
| `build <plugin> <cases.json> <out_dir> [--stub] [--also out.json] [--history f.jsonl] [--max-turns N]` | A scratch copy with eval cases, in a new folder; it refuses one that holds files. A case may add `after` and `rule` for a skill that fires from a step |
| `tally <result.json>... [--cases f] [--save f] [--accepted f]` | Hits, misses by cause, wrong order, false fires, a rerun instruction per kind of abnormal end; outcomes saved or compared per case; with `--cases`, cases marked `"split": "test"` scored apart from the ones the description was written from |

Findings from `check` are candidates, not verdicts:

| Finding | Meaning |
|---|---|
| `broken-ref` | A running file points at a missing file |
| `duplicate-file`, `shared-lines` | A whole file repeated, or lines copied between instructions |
| `orphan` | A script or doc nothing points at |
| `no-eval`, `never-called`, `unmeasured` | No eval case or `tests/route_cases.json` entry grades it, or no call in the transcripts, or no transcripts to count in. A skill only the user invokes needs no eval |
| `never-read` | A skill's reference file no Read in the transcripts opened, subagents included: a candidate to fold in or drop |
| `no-description`, `long-description`, `long-body` | No description, over 1,536 characters, over about 5,000 tokens, where a compaction cuts a skill |
| `overlap-candidate` | Descriptions share many words. `audit` judges the meaning |
| `unknown-frontmatter` | A skill key Claude Code does not read, such as `disable_model_invocation`. It is ignored without an error |
| `hook-config` | An event name `hooks.json` does not have, or a command hook with no `timeout`, which can hold a session for the default |
| `mcp-unpinned` | An MCP server fetched at start by `npx`, `uvx`, `bunx` or `pipx` with no exact version, so it runs whatever is newest |

On real plugins, `broken-ref` was often an example path in a skill about writing skills, and `duplicate-file` was often deliberate. That is why `audit` opens each file.

## `compass.py` commands

Run as `python -I -X utf8 scripts/compass.py <command>`.

| Command | Does |
|---|---|
| `hook` | Reads the event from standard input and prints the index. Called by `hooks/hooks.json` |
| `status <root>` | Per document: stale or current, sources changed, age. Lists up to 100 new paths per document, each path once, and says how many it left out. Then folders with changes outside every source since the oldest review, up to 30 changed documents among them by name, `ELSEWHERE n` and `STALE n`. Records when it looked |
| `lint <root>` | One finding per line, then `OK` or `FINDINGS n` |
| `stamp <root> [files] [--accept-goal] [--drift line]` | Records the review time, the goal's fingerprint, the drift line and the debt baseline |
| `debt <root>` | `DEBT n NEW n GONE n INCOMPLETE n SKIP n`, then up to 50 lines: new markers, skipped tests with no marker, markers with no `when`, removed markers. Reads code files only and writes nothing |
| `usage <root>` | Reads of each document in this project's transcripts, leaving out sessions that ran `stamp`. Counts only |

## `clone.py` commands

Run as `python -I -X utf8 scripts/clone.py <command>`.

| Command | Does |
|---|---|
| `ensure <root> [--dest dir] [--no-register]` | Prints `CREATED`, `EXISTS`, `THIS`, `ORIGINAL`, `WRONG-PROJECT` or `NO-PROJECT`, and what to do. Every skill runs it first |
| `sync [--from dir] [--dry-run]` | Run from a copy. Takes a newer original: files the project did not touch are updated, new ones added, dropped ones removed. Files the project added stay, and files it changed stay and print as `kept`. An original in a git checkout is taken from its published ref (`origin/main`, else `main`) by `git archive`, never from a worktree; a session hears of a new commit or a changed folder, not only a new version. `--dry-run` lists the changes and writes nothing. `.git` files, `.superpowers`, `.worktrees` and Claude Code's `.in_use` marks never reach a copy |

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
claude plugin eval "<out_dir>" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --model <model> -j 1 --json <run>/result.json
python -I -X utf8 scripts/project.py tally <run>/result.json
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

- `/phro-project:init`: once per project. Makes the project's copy and the queue, and turns on the panel.
- Routing by exact id: `queue.py hook` runs at session start and on every prompt. At start it gives the session the rule with its own id (`ask ... --session <UUID>`) and lists its open tickets. On every prompt it pulls this session's decided tickets ("decided for you: read <path>"), and when the prompt names another session's ticket id it says whose it is and how to record and deliver the answer without doing that work here. Tickets store the bare transcript UUID; `queue.py sessions` repairs older values.
- Blocking or not: `ask --default "<what I do without an answer>"` makes a ticket can-wait, so only tickets that stop a session show as needing an answer. A session that went past its own ticket closes it with `done` or `decide --text "self: ..."`.
- `/phro-project:inbox [id]`: walks the waiting tickets one at a time, highest priority first, records each answer and sends what `queue.py deliver` prints to the asking session.
- A decided question is not asked again. A ticket returned three times becomes the question "close it or shrink it to one step".

- The board page: `ui/board.html`, one self-contained file. `queue.py serve <project> [--port 8765]` hands it out on 127.0.0.1 and fills it on each refresh (every 10 s) from `queue/` and `docs/core/`: waiting questions with their options, a free answer box and a two-press drop, can-wait, decided, in progress, sessions and the core documents map. Answers and drops go through `decide` and `drop`; writes need the token the page was served with and a local Host and Origin. Nothing is published. The `show` skill starts it through `<project>/.claude/launch.json` and opens it in the browser pane.

`queue.py` commands: `init`, `ask`, `decide`, `move`, `done`, `drop`, `board`, `deliver`, `sessions`, `serve`, `hook`; each prints `OK`, `SEND` or `REFUSED` with a path, never a ticket body.

### The queue panel

The panel (`mods/queue-panel/`) reads `queue/board.json` only. Sections, empty ones hidden except the first: Needs your answer (blocking tickets, with the question, the session's title, stopped or still working, an answer button that fills `/<copy>:inbox <id>`, and a drop button that on a second press runs `queue.py drop`, closing the ticket with a record line), Can wait (collapsed), Decided not picked up (over a day highlighted), In progress, Sessions (title, working or idle, open count), attention counts and what closed in 7 days. Sessions appear by title, never by full id. It is a plugin of its own, because a plugin's panel state belongs to that plugin's name and each project's copy has another name. `init` turns it on for one project, from the project folder:

```bash
claude plugin marketplace add <the project's copy>/mods/queue-panel --scope local
claude plugin install queue-panel@queue-panel --scope local
```

Then `/reload-plugins`. Every project registers the marketplace under the one name `queue-panel`; the panel code is the same in every copy, so the last one registered serves all of them.

## Daily report

A project's copy writes `docs/daily/<YYYY-MM-DD>(<요일>).md` on each weekday: what was finished since the previous weekday and what comes today, in short Korean list items (style in `skills/daily/SKILL.md`). No scheduler: `daily.py hook` runs at every session start and prompt, and the first one after 08:00 KST that finds today's file missing starts `daily.py run` in the background. `run` reads the tails of every session transcript of the project for the period (previous weekday 00:00 to today 00:00 KST, so Monday covers Friday to Sunday), the project's commits, the queue's closed and open tickets and the current goals in `docs/core/README.md`, asks `claude -p --model sonnet` with no tools, checks the form and writes the file. A failure is kept in `docs/daily/.state/<date>.error` and retried after 30 minutes. The original plugin never writes one.

```bash
python -I -X utf8 <copy>/scripts/daily.py run --force [--date YYYY-MM-DD]
```

## Document reader

A `PreToolUse` hook on `Read` (`scripts/read.py hook`) catches a read of a document the model cannot open as text, converts it to Markdown with images extracted to files, and hands Claude the Markdown path to read instead. Word, PowerPoint, Excel and pdf files are read here, while editing or creating them stays with the `anthropic-skills:docx`, `:pptx`, `:xlsx` and `:pdf` skills, as each converted Read says in one sentence. `PHRO_READER_OFF` (comma-separated extensions, case and a leading dot ignored) hands those extensions back to Claude's own Read, and `read.py convert` ignores it.

Built formats: `hwpx`, `hwp`, `doc`, `xls`, `odt`, `ods`, `odp`, `docm`, `pptm`, `ppsx`, `epub`, `xps`, `oxps`, `vsdx`, `xlsb`, `ppt`, `rtf`, `docx`, `dotx`, `dotm`, `pptx`, `potx`, `potm`, `ppsm`, `xlsx`, `xlsm`, `xltx`, `xltm`, `pdf`. In a workbook a cell shows its cached value (a formula only when it has none); slide and cell comments, cell hyperlinks, OMML equations (as TeX) and SmartArt text are read. Every other in-scope extension (wpd, mobi, azw3, pages, numbers, key, djvu, mdb, accdb, pub, vsd, mpp) is denied as "unsupported" until its phase is built; a plain-text file that only carries one of these extensions passes through unchanged. Standard library only, except the optional pdf engine in its own venv; budgets: input 512 MiB, output 20 MB, 45 seconds per conversion.

PDF: a pdf is read by the built-in parser unless the optional pypdfium2 engine is set up; the engine reads tables, scans and forms better. `read.py setup` makes its venv and installs only on the user's yes; the converted Read offers it once a session, and a pdf is never held up by the offer. The install is pinned and hash-checked (pypdfium2 5.15.2, platform wheels only, no dependencies), about 4.2 MB downloaded and 8.4 MB on disk, under `${CLAUDE_PLUGIN_DATA}/pdf-venv`. pypdfium2 is licensed Apache-2.0 or BSD-3-Clause and is installed, not copied.

```bash
python -I -X utf8 scripts/read.py setup [--venv DIR] [--check] [--decline] [--remove]
```

`setup` alone installs, `--check` reports the venv's state, `--decline` records that the user never wants it (the offer stops) and `--remove` deletes the venv. `--venv DIR` chooses the folder, and `setup` refuses a folder that is not a pdf venv. The hook looks in this order: `PHRO_READER_PYENV`, then `CLAUDE_PLUGIN_DATA/pdf-venv`, then the folder `setup --venv` recorded, then a default beside the cache. Inside the plugin `CLAUDE_PLUGIN_DATA` is set, so it takes priority over a folder chosen with `setup --venv` in a shell. `PHRO_READER_PYENV=off` forces the built-in parser. A venv that fails its file check is not used: the pdf is read by the built-in parser and the Read offers the repair.

Each page starts at a `## Page N` line. A scanned page is linked as an image, with no OCR, so Claude looks at it; form fields and comments are read, and hidden layers and hidden text are left out and marked. Read's `pages` selects those pages in the Markdown (a page shown nowhere as an image goes to Claude's own Read, and one past the end is reported). pypdfium2's pdfium has no V8 or XFA, so no JavaScript runs; an XFA form and attached files are reported by name, not read. A pdf that cannot be converted or times out is read by Claude's own Read; an encrypted or DRM-protected one, or one over the size budget, is denied with the `pdf` skill named. To edit or create a pdf, use `anthropic-skills:pdf`.

HWP protection: a distribution document is decrypted with the key the file itself carries and read like any other `.hwp`. One locked with a password or a certificate is denied with "the file is encrypted and needs a password", and one under DRM (the FileHeader DRM flags, or a Fasoo or SoftCamp container) with "the file is DRM-protected"; no password is tried and DRM is not removed. An encrypted or obfuscated `.doc` is denied as encrypted too.

Office protection: an `.xls` or `.ppt` that is encrypted with its format's default password (the Excel `VelvetSweatshop` password, the PowerPoint default) is decrypted and read; one locked with any other password is denied as encrypted. Every encrypted `.xlsb` is denied as encrypted. Excel charts and worksheets embedded in `.hwp`, `.doc` and `.ppt` files are read and placed where the object sits.

Always-on cost: for any file outside the 41 in-scope extensions the hook prints nothing, and each Read costs one Python start. Measured with `Measure-Command` over 20 runs of `read.py hook` on a `.py` event, the median is 84 ms.

Converted output is cached by a hash of the document's bytes and the reader's own source at `%LOCALAPPDATA%\phro-project\reader\` on Windows and `~/.cache/phro-project/reader/` elsewhere, as `<sha1[:2]>/<sha1>/` holding `out.md`, `assets/` and `meta.json`. `python -I -X utf8 scripts/read.py prune [--days 30]` deletes entries not used for N days.

Hidden content (hidden sheets, slides, rows and text) is left out and marked. To get it in full:

```bash
python -I -X utf8 scripts/read.py convert <file> [--out DIR] --hidden
```

Result against the routing pick on the bench corpus, one line per extension:

- `docm`: PASS.
- `odt`: PASS.
- `xps`: PASS, though its two public documents are scored only against engines that include this reader.
- `docx`: PASS (fidelity 0.832 against 0.625). For the native four the bar compares text-only tokens, since linked images are opened only when needed; the pick also loads the skill's SKILL.md once a session, which is not counted.
- `pptx`: PASS (fidelity 0.861 against 0.680); text-only tokens are 2% over the pick, inside the bar.
- `xlsx`: PASS (fidelity 0.714 against 0.705) after one fix: SmartArt anchored on a sheet was dropped without a count and is now read.
- `xlsm`: PASS (fidelity 0.841 against 0.687). markitdown, the xlsx skill's command, read all three files; none of them holds VBA.
- `epub`: MISS on tokens only; the bench counts linked images as viewed, text-only tokens are below the pick.
- `hwpx`: MISS on tokens only; linked images are counted, text-only tokens are within 1% of the pick.
- `odp`: MISS on tokens only; linked images are counted, text-only tokens are below the pick.
- `ppsx`: MISS on tokens only; linked images are counted, text-only tokens are within 1% of the pick.
- `pptm`: MISS on tokens only; linked images are counted, text-only tokens are within 1% of the pick.
- `ods`: MISS on fidelity (0.868 against 1.000), mostly a peer-agreement artifact on one public document.
- `hwp`: MISS on fidelity (0.695 against 0.706) and tokens. The fidelity gap is peer agreement: the reader writes list paragraphs in table cells as marked items, where dochan and hwp-mcp both join them into one line without bullets, so their shared lines set the reference. Tokens: bitmaps become viewable PNGs and are counted, and tables with merged cells are HTML, which also puts text-only tokens above the pick.
- `doc`: MISS on two failures the pick does not share: a Word 6/95 file, denied as "unsupported", and a hand-made test file without the table stream MS-DOC requires. On the generated document fidelity is above the pick (0.903 against 0.869) and tokens are 13% over, mostly an HTML table with merged cells.
- `oxps`: NODATA, no corpus files; it goes through the `xps` module.
- `vsdx`: NODATA, no routing pick to compare with; 3 of 3 corpus files converted without failure.

## Not done yet

- Use of the design, route-test and audit skills in real work. A capability built on request and a step-triggered skill have only passed scripted checks, not a run with a model.
- A scheduled `compass` refresh, and a session in a second project. One project has its documents, and the hook has fired there.
- `restructure` start to finish with a person choosing the structure. Only its script has met real projects, on scratch copies.
- Routing test of this plugin's own skills with `tests/route_cases.json`, 28 cases, about $4. The cases are written by the author, which inflates the rate, so it waits for prompts really typed.
- A hook at write time that enforces folder rules, a search over the documents, signal hook, local search and compliance grading. Each waits for a measured failure.
- Phases 3-4 of the document reader (wpd, ebooks, iWork, djvu, Access, Publisher, Visio binary, Project). Phases 1, 2a, 2b, 2c and 2d (hwpx, hwp, doc, xls, ppt, xlsb, rtf, the zip formats, docx, pptx, xlsx and their template and macro variants, pdf) are built.

## When to delete it

- The work queue: if fewer than 5 decisions close through it in its first two weeks; remove `queue.py`, the `init` and `inbox` skills, the panel and the hook rule.
- The plugin: if two audits of real plugins produce no adopted proposal and no routing problem.
- `compass` and its hook: if after a month sessions still lose the goal with `docs/core` in place, or `usage` shows nobody opens the documents and the refresh changes nothing.
- `restructure`: if a real restructure takes more of your time with it than by hand, or its misses reach the test suite more than its manual list catches them.
