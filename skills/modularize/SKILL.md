---
name: modularize
description: Rebuilds a repository into portable feature modules that teammates can drop into the app or back end they build, each with a machine-checkable contract, then writes the contract document for those teams. Moves files, so it runs only when you type it.
disable-model-invocation: true
---

# modularize

Output: an approved spec, then the repository rebuilt as portable feature folders that pass the copy test, and a contract document an outside team can code against.

First run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/clone.py ensure <project root>` and do what it prints.

Scripts: `${CLAUDE_PLUGIN_ROOT}/scripts/modularize.py` (`imports`, `copytest`, `contracts`) for the portability checks, and `${CLAUDE_PLUGIN_ROOT}/scripts/restructure.py` (`map`, `plan`, `apply`, `verify`, `undo`) for moving files. Both print paths and counts, never file contents.

The difference from `restructure`: that skill makes a layout a newcomer can read. This one makes a layout another team can lift piece by piece, so the unit is "what a consumer takes", and nothing is done until a copied piece runs alone.

## 1. Facts before questions

Find these yourself, from code, docs and git, never by asking:
1. Every capability and where its code, model code, weights, rules and tests live today. Run `restructure.py map <root>` and count cross-imports between candidate features.
2. Every consumer: what runs where (device, browser, back end, batch). Demos and dashboards are consumers, not deliverables, unless the user says otherwise.
3. Every wire between consumers: messages, HTTP ingest routes, files. A wire hidden inside one feature is a shared format to pull out.
4. What each feature needs from outside: data it fetches, keys, model files, base models, device formats that do not exist yet.
5. State each feature keeps (windows, sessions, tracks, stores), version couplings (dictionary or schema versions, trained-on versions), uncommitted work and other sessions editing the repo.

## 2. Decide with the user, in rounds

Ask in rounds (the grilling form: numbered questions, each with a recommended answer, worded so "yes" accepts it), only what the facts cannot settle. A question that depends on an open one waits for the next round. The decision tree, in order:
1. Consumers and bundles: one top-level bundle per consumer (for example device app, control back end) plus `shared/` for the wires. Bundles never import each other.
2. Hand-off form per bundle: a library with a thin HTTP router where the consumer is a back end; model files, rule data, a reference implementation and golden examples where the consumer is on a device or in another language.
3. The unit: one feature = one folder with inference code, contract, examples, weights manifest, `requirements.txt`, tests and README. Training code stays outside and imports features, never the reverse.
4. The contract source: machine-readable files beside the code (JSON Schema, golden examples the tests replay, OpenAPI generated from the router). The document only indexes them.
5. Feature list and common layers: merge features that import each other both ways; pull helpers that several features share into one common layer per bundle.
6. Outside needs: fetch built in with keys from the environment, plus an entry that takes the data from the caller. Missing device formats are written as "not decided", not built.
7. State goes in and out of calls; stores sit behind a replaceable interface. Auth is an overridable dependency.
8. Versions: one table of what each feature supports; mismatches fail loudly.
9. What is dropped (to `legacy/`) and what is out of scope (experiments, conversions, packaging). The move changes no behaviour except what the spec names.
10. Order, done criteria, and what to update after the merge (deployments, docs and memory that name old paths, pushes, which need a fresh yes).

When the tree is empty, show the whole design in one message and wait for the user's yes.

## 3. Spec

Write the spec in English to `docs/superpowers/specs/<date>-portable-modules-design.md` on a new branch in a local worktree (`git worktree add .worktrees/<branch> -b <branch>`, `.worktrees/` in `.git/info/exclude`), so sessions on the main checkout are not disturbed. Commit it with the repository's own author. Sections: purpose and success, bundles, target tree, one feature folder, rules, version table, per-feature decisions, migration, done criteria, after the merge, out of scope, risks. Ask the user to review it. Then write the step plan with `superpowers:writing-plans` if installed, otherwise as a numbered plan in the spec's folder, and wait for its approval.

## 4. Move

Follow sections 3 to 5 of `${CLAUDE_PLUGIN_ROOT}/skills/restructure/SKILL.md` (stop the world, baseline, phases with `plan`, `apply`, `verify`, tests per phase) inside the worktree. Order: shared formats, common layers, then features from the most depended on, the feature another session is editing last. One commit per phase.

Write `contracts/modules.json` as the first phase, and keep it current:
```
{"features": {"control/hazard": ["shared", "control/geo"], "app/stt": ["shared"]},
 "consumers": ["training", "examples", "tools"]}
```

After each phase run:
1. `modularize.py imports <worktree>`: 0 forbidden.
2. `modularize.py copytest <worktree> <feature> --venv` for each feature moved: OK. A feature without tests fails here, so it gets its contract replay test in the same phase.
3. The whole suite against the baseline.

## 5. Contract document

After the last phase, write `contracts/README.md` in the user's language for outside readers: no internal version names, run history, server paths or account names. For each wire in `shared/` and each feature: what it does, how to call it (function and HTTP), inputs and outputs by link to its `contract/` files with one example inline, errors, state it expects the caller to keep, auth, versions, weights and keys it needs. Then run `modularize.py contracts <worktree>` until every feature reads OK, and check that each inline example matches its file.

## 6. Finish

1. Done means: suite matches the baseline, `imports` 0, `copytest` OK for every feature, generated OpenAPI equals the committed one, every consumer example starts without errors, no old-path mentions outside `legacy/` and training.
2. Write the folder rules the way `restructure` section 6 does, adding the import rule and the copy test to the suite.
3. Report in the user's language: the tree, each feature's `copytest` line, `contracts` counts, what changed beyond moves, what was left out of scope, and the after-merge steps still waiting for the user's yes. Merging the branch and pushing are the user's call.
