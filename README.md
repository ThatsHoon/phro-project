# phro-project

Designer for project-specific plugins. While you build a plugin for a project, it re-reads the plugin's current structure from its files, decides where a new capability belongs, measures whether skills really get called, and proposes what to merge, delete or repair.

It has no universal structure, because plugins differ too much between projects. It has no always-on parts either: no hook, no server, no routing layer. The only cost in every session is three skill descriptions, about 272 tokens.

## Skeleton

```
phro-project/
  .claude-plugin/plugin.json   manifest
  skills/
    design/SKILL.md            before adding a capability: where it belongs, trigger prompts first
    route-test/SKILL.md        whether skills get called on real prompts, and who takes each miss
    audit/SKILL.md             after growth: merge, delete and repair proposals
  scripts/project.py           all deterministic work, standard library only
  fixtures/
    midwork.jsonl              synthetic mid-task conversation that route-test resumes
    make_midwork.py            regenerates it
  tests/
    test_project.py            seeded-defect test of project.py
    route_cases.json           this plugin's own routing cases, written by its author
```

| Part | Does | Does not |
|---|---|---|
| `scripts/project.py` | Reads structure, lists candidates, builds tests, tallies results | Judge |
| The three skills | Procedure, judgment, checking candidates, proposals | Edit the plugin unasked |
| You | Pick proposals, approve spending | |

### Structure graph

Each `project.py` call reads the plugin's files into an in-memory SQLite graph and discards it on exit. The files are the source, so the graph cannot go stale.

- Nodes (`node`): skills, agents, commands, scripts, docs, other assets, config, MCP servers, eval cases.
- Edges (`edge`): `path` for a mentioned file, `import` for a Python import, `name` for a `plugin:name` reference or an eval grading a skill.

The graph answers structure questions such as what breaks if a part is removed. It does not route prompts: published results showed little gain from a graph for picking one skill, a single hop. No source measured the graph for structure questions, so this placement is a design judgment.

## Workflow

Three flows follow a plugin's life. Each skill fires on its own or by name, such as `/phro-project:design`.

```
design -> build -> route-test -> use in real work -> audit -> apply on a copy -> route-test
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

On real plugins, `broken-ref` was often an example path in a skill about writing skills, and `duplicate-file` was often deliberate. This repo's own `broken-ref` on `hooks/hooks.json` is a literal in `project.py`. That is why `audit` opens each file.

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

## Check and install

```bash
python -I -X utf8 tests/test_project.py
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

## Not done yet

- Use of the three skills in real plugin work.
- Routing test of this plugin's own skills with `tests/route_cases.json`, about $3.
- Signal hook, local search and compliance grading.

## When to delete it

Delete this plugin if two audits of real plugins produce no adopted proposal and no routing problem.
