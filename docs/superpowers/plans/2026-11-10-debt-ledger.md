# Debt ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Count technical debt from English `lean:` markers and unmarked skipped tests, report its trend in the compass drift line, and send only owner debt to the work queue.

**Architecture:** One read-only `debt` subcommand in `scripts/compass.py` reuses `walk()` and `core.json`; `stamp` saves a fingerprint snapshot that `debt` compares against. Skill text (lean, lean-review, compass Refresh) tells sessions to write and use the markers. No new files besides tests.

**Tech Stack:** Python 3 standard library; Markdown skills; tests are plain scripts run with `python -I -X utf8`.

**Spec:** `docs/superpowers/specs/2026-11-10-debt-ledger-design.md`

## Global Constraints

- Standard library only; scripts run as `python -I -X utf8`.
- Marker form, verbatim: `lean: <ceiling> | when: <trigger> | fix: <upgrade path>`; English; `when: owner` means the owner decides.
- `debt` never writes; only `stamp` saves the snapshot, under `core.json` key `debt`.
- Output lists at most 50 item lines; the counts line is always complete.
- Code suffixes scanned: `.py .js .cjs .mjs .ts .tsx .jsx .sh .ps1 .go .rs .java .kt .swift .c .h .cpp .cs .rb .html .css .yml .yaml .toml`. Documentation is never scanned.
- `grep -rn "lean:\|ponytail:"` stays in the lean Build rule (pinned by `tests/test_lean.py`).
- Work in a worktree `.worktrees/debt-ledger` on branch `debt-ledger`; main holds another session's uncommitted `scripts/clone.py` and `tests/test_clone.py`, never touch them.
- Commits as `thatshoon <gagea45@gmail.com>`, no AI attribution lines.

## Review Focus

- A project with no `docs/core`: `debt` still runs, prints `no baseline`, exits 0; `stamp` is not reachable there, so nothing is written.
- Moving a marker to another line is not NEW; two identical markers in one file count twice and vanish one at a time (multiset compare).
- A `lean:` in a string literal without a comment token, or in a `.md` file, is not counted.
- A huge or binary file with a code suffix does not crash the scan (read with `errors="ignore"`, files over 1 MB skipped).
- A `core.json` written before this change (no `debt` key) is a project with no baseline, not an error.

---

### Task 1: `compass.py debt` scan

**Files:**
- Modify: `scripts/compass.py` (constants near `DRIFT_CHARS`, new functions after `walk()`, a `debt` subparser and branch in `main()`)
- Test: `tests/test_compass.py` (new `debt_scan()` called from `demo()` before `queue_rule()`)

**Interfaces:**
- Produces: `debt_items(root) -> list[tuple[str, int, str, str, bool]]` as `(rel_posix_path, line_no, kind, text, complete)`, `kind` in `{"marker", "skip"}`; `fingerprint(item) -> str` = `f"{path}\t{kind}\t{' '.join(text.split())}"`; `debt_lines(root) -> list[str]` (counts line first); CLI `compass.py debt <root>` prints `debt_lines` and exits 0.

- [ ] **Step 1: Write the failing test `debt_scan()`**

Fixture in a temp dir (no `docs/core`):
- `src/a.py`: `x = 1  # lean: one pass only | when: input > 1e6 rows | fix: stream it` (complete marker), `# lean: rough guess` (incomplete), `# ponytail: old style` (incomplete), `s = "lean: not a comment"` (ignored).
- `notes.md`: `# lean: in docs | when: never | fix: none` (ignored).
- `tests/test_a.py`: `# lean: needs GPU | when: CI has a GPU | fix: unskip` on the line above `@pytest.mark.skip` (marked, not a skip item), and a bare `@pytest.mark.xfail` (unmarked skip).
- `web/a.test.cjs`: `it.skip("x", () => {})` (unmarked skip).
- `runs/big.py` with `# lean: ignored | when: x | fix: y`, and `docs/core` absent so `skip` is empty: assert it IS counted; then write `docs/core/core.json` with `{"skip": ["runs"], "docs": []}` and assert it is NOT.

Assertions (markers: `src/a.py` 3 + `tests/test_a.py` 1 + `runs/big.py` 1; incomplete: the two in `src/a.py`; unmarked skips: 2):
```python
assert compass.debt_lines(d)[0] == "DEBT 7 NEW 0 GONE 0 INCOMPLETE 2 SKIP 2 no baseline"
# after writing docs/core/core.json = {"skip": ["runs"], "docs": []}
assert compass.debt_lines(d)[0] == "DEBT 6 NEW 0 GONE 0 INCOMPLETE 2 SKIP 2 no baseline"
```
Also assert: an `incomplete\tsrc/a.py:<n>\tmarker\trough guess` line exists; `notes.md` and the string literal appear nowhere; CLI `python -I -X utf8 scripts/compass.py debt <d>` exits 0 and prints the same first line; with 60 extra incomplete markers in one file, item lines are 50 plus one `and N more` line while the counts line still shows the full total; a 2 MB `.py` file and a `.py` file of random bytes do not raise.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -I -X utf8 tests/test_compass.py`
Expected: FAIL with `AttributeError: module 'compass' has no attribute 'debt_lines'`

- [ ] **Step 3: Implement**

- Constants: `CODE_SUFFIXES` (the set above), `DEBT_SHOWN = 50`, `DEBT_MAX_BYTES = 1_000_000`.
- `MARKER = re.compile(r"(?:#|//|/\*|\*|<!--|--)\s*(?:lean|ponytail):\s*(.*?)\s*(?:\*/|-->)?\s*$")`.
- `SKIPPED = re.compile(r"pytest\.mark\.(?:skip|xfail)|pytest\.skip\(|unittest\.skip|\b(?:it|test|describe)\.skip\(|\bx(?:it|describe)\(")`.
- `complete` = `re.search(r"\|\s*when:", text, re.I)` is truthy.
- A `SKIPPED` line is an item only when neither it nor the previous line matches `MARKER`; its text is the stripped line.
- `skip` set: `core.json` `skip` when `docs/core/core.json` exists and parses, else empty.
- `debt_lines`: baseline = `manifest.get("debt")` (None when no core.json or no key). Compare `collections.Counter` of fingerprints. Item order: new, unmarked skip, incomplete, gone; `new` lines only when a baseline exists. Line form: `<state>\t<path>:<line>\t<kind>\t<text>`; `gone` lines have no `:<line>`.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -I -X utf8 tests/test_compass.py`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add scripts/compass.py tests/test_compass.py
git commit -m "compass: debt scan of lean markers and unmarked skipped tests"
```

### Task 2: Baseline in `stamp`

**Files:**
- Modify: `scripts/compass.py` `stamp()`
- Test: `tests/test_compass.py` `debt_scan()` (extend)

**Interfaces:**
- Consumes: `debt_items`, `fingerprint` from Task 1.
- Produces: `stamp()` writes `manifest["debt"] = sorted(fingerprint(i) for i in debt_items(root))` on success.

- [ ] **Step 1: Extend the test** on a `clean(d)` project (it has `docs/core`): add `src/b.py` with one complete marker; run `compass.stamp(d, [])`; assert `debt_lines(d)[0]` starts `DEBT 1 NEW 0 GONE 0` with no `no baseline`; insert two blank lines above the marker and assert still `NEW 0`; add a second identical marker line in the same file and assert `NEW 1`; restamp, remove one copy, assert `GONE 1` and one `gone\tsrc/b.py\tmarker\t...` line; assert `compass.lint(d)` is still `[]` after stamping (the `debt` key must not trip lint).
- [ ] **Step 2: Run** `python -I -X utf8 tests/test_compass.py`. Expected: FAIL on the `no baseline` assertion.
- [ ] **Step 3: Implement** the one assignment in `stamp()` before `save()`.
- [ ] **Step 4: Run** the same command. Expected: `ok`.
- [ ] **Step 5: Commit** `git commit -am "compass: stamp saves the debt baseline"`

### Task 3: Skills, existing markers, release text

**Files:**
- Modify: `skills/lean/SKILL.md` (Build rule line 20), `skills/lean-review/SKILL.md` (Scope paragraph, tag table, examples), `skills/compass/SKILL.md` (Refresh step 6), `scripts/project.py:31,47`, `scripts/compass.py:39`, `scripts/queue.py:139,157,289`, `README.md` (section on lean), `.claude-plugin/plugin.json` (version `0.6.0`)
- Test: `tests/test_lean.py` (new `debt_rules()` registered in the check list), `tests/test_compass.py` (`debt_scan()` end)

- [ ] **Step 1: Write the failing tests**
  - `debt_rules()` in `tests/test_lean.py`: the injected main and subagent Build text contains `lean: <ceiling> | when: <trigger> | fix: <upgrade path>` and `in English`; `skills/lean-review/SKILL.md` contains `` `debt:` `` and `compass.py debt`; `skills/compass/SKILL.md` contains `compass.py debt` or `` `debt` `` inside step 6 and `when: owner`.
  - End of `debt_scan()` in `tests/test_compass.py`: every `debt_items(<repo root>)` item whose path starts with `scripts/` is complete (tests hold fixture markers inside strings, so only `scripts/` is checked).
- [ ] **Step 2: Run** both test files. Expected: FAIL on the marker form string and on six incomplete items under `scripts/`.
- [ ] **Step 3: Edit the text**
  - lean Build rule becomes: ``- A deliberate corner with a known ceiling gets one English comment: `lean: <ceiling> | when: <trigger> | fix: <upgrade path>`, where `when` is a checkable condition or `owner`. A skipped or flaky test gets one on the line above. Existing `ponytail:` comments mean the same. `grep -rn "lean:\|ponytail:"` lists them.`` Keep the injection growth under 150 bytes.
  - lean-review: Scope gains ``Also run `python -I -X utf8 ${CLAUDE_PLUGIN_ROOT}/scripts/compass.py debt <project root>` and check the markers under the reviewed paths.``; table row ``| `debt:` | a `lean:` marker whose `when` already holds | its `fix` |``; example ``- `queue.py:L139: debt: one git call per ticket, queue now 412 tickets. Batch with one git log.` ``.
  - compass Refresh step 6, appended: ``Before `stamp`, run `debt <root>` and add `debt N (+K)` to the drift line. For a new marker whose `when` is `owner`, or one the scan output already shows as met, open a ticket with `queue.py ask`; leave every other marker in code.``
  - The six comments, verbatim:
    - `project.py:31` `# lean: word Jaccard only finds candidates | when: audit misses an overlap the user spots | fix: let the model judge overlap in audit`
    - `project.py:47` `# lean: rough byte estimate | when: a listing-cost decision rests on it | fix: read claude plugin details`
    - `compass.py:39` `# lean: blocks hidden text only, plain doc text passes like CLAUDE.md | when: a plain-text injection is seen | fix: owner reviews index changes`
    - `queue.py:139` `# lean: one git call per ticket | when: queue > 300 tickets | fix: batch with one git log`
    - `queue.py:157` `# lean: done time = file mtime | when: edits after done skew the weekly count | fix: add a done_at field`
    - `queue.py:289` `# lean: dedup checks the current question only | when: an old question is asked again | fix: compare every past question`
  - README: one short paragraph under the lean section on the marker and `compass.py debt` (Korean, like the rest of the README's user section). `plugin.json` version `0.6.0`.
- [ ] **Step 4: Run every test file**: `for t in tests/test_*.py; do python -I -X utf8 $t || echo FAIL $t; done`. Expected: each prints its pass line, no `FAIL`.
- [ ] **Step 5: Commit** `git commit -am "lean, lean-review, compass: English debt markers and the debt scan in refresh (0.6.0)"`
