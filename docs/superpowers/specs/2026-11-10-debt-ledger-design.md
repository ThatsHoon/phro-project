# Debt ledger: design

## Goal

Technical debt grows with every shortcut and every skipped test. Keep it visible and paid on time without a second system to maintain. The ledger is the code itself: each shortcut carries one English comment that says what it costs, when to pay it and how. A read-only scan counts the comments, the daily compass refresh reports the trend, and only debt that needs the owner reaches the work queue.

Owner decisions (2026-11-10): debt lives in code comments, not a separate document or one ticket per item; comments are written in English to keep them short.

## Marker

One line, in the file's own comment syntax, on or just above the shortcut:

```
# lean: <ceiling> | when: <trigger> | fix: <upgrade path>
```

- `ceiling`: what breaks or degrades, in a few words ("one git call per ticket").
- `when`: a condition someone can check, ideally a number or an event ("queue > 300 tickets", "second caller appears", "before release"). `owner` means paying it needs the owner's decision.
- `fix`: the upgrade, in a few words ("batch with one git log").
- English only, at most about 120 characters. Fields after `ceiling` may be omitted, but the scan reports a marker without `when` as incomplete.
- A skipped, xfail or known-flaky test carries the same marker on the line above it, with the reason as the ceiling.
- Existing `ponytail:` comments and free-form `lean:` comments count as markers; free-form ones are incomplete until rewritten.

## Scan: `compass.py debt <root>`

Standard library only, read-only, works with or without `docs/core`.

- Walks the project with compass's existing `walk()`, honouring `core.json` `skip` when it exists.
- Reads files with code extensions only (`.py .js .cjs .mjs .ts .tsx .jsx .sh .ps1 .go .rs .java .kt .swift .c .h .cpp .cs .rb .html .css .yml .yaml .toml`). Documentation is not scanned.
- Finds two kinds:
  - `marker`: `lean:` or `ponytail:` after a comment token (`#`, `//`, `/*`, `*`, `<!--`, `--`).
  - `skip`: a skipped or expected-failure test with no marker on that line or the line above: `pytest.mark.skip`, `pytest.mark.xfail`, `pytest.skip(`, `unittest.skip`, `it.skip(`, `test.skip(`, `describe.skip(`, `xit(`, `xdescribe(`.
- Fingerprint of an item: its path plus its normalised comment or matched line, without the line number, so moving code does not make it new.
- Output, counts first, then one tab-separated line per item, at most 50 item lines (the counts stay complete):

```
DEBT 14 NEW 2 GONE 1 INCOMPLETE 3 SKIP 1
new	scripts/queue.py:139	marker	one git call per ticket | when: queue > 300 tickets | fix: batch with one git log
incomplete	scripts/project.py:47	marker	rough byte estimate
skip	tests/test_x.py:20	skip	@pytest.mark.skip
gone	scripts/old.py	marker	...
```

- `NEW` and `GONE` compare against the snapshot saved in `docs/core/core.json` under `debt` (a sorted list of fingerprints). With no snapshot, `NEW` is 0 and the line says `no baseline`.
- `stamp` saves the current snapshot, so a refresh that ends with `stamp` moves the baseline. `debt` itself never writes.

## Compass refresh

- Refresh step 6 runs `debt` before writing the drift line, and appends `debt N (+K)` to it, in the user's language, inside the existing 200-character limit. Every session already receives the drift line, so nothing new is injected.
- Refresh never pays debt. For a new marker whose `when` is `owner`, or one whose `when` the refresh can see is already true from the scan output alone, it opens a ticket with `queue.py ask` (the queue's normal dedup applies). Every other marker stays in code.

## lean rules

- Build section: the marker line replaces the current `lean:` rule. The rule keeps `grep -rn "lean:\|ponytail:"` and adds that the comment is in English and in the three-field form, and that a skipped test gets one too.
- Injection size grows by at most about 150 bytes.

## lean-review

When it reviews a folder, it also runs `compass.py debt` on the project, takes the items under that folder, and reports each marker whose `when` already holds as a `debt:` finding with its `fix`. It still edits nothing.

## Existing comments

The six `ponytail:` comments in this plugin (`project.py` 31 and 47, `compass.py` 39, `queue.py` 139, 157 and 289) are rewritten in the new form in the same change. Comments in other projects are left alone; the scan reports them as incomplete, and they are rewritten when someone touches the code.

## Tests (`tests/test_compass.py`)

- A fixture project with: a complete marker, a free-form `lean:`, a `ponytail:`, a marker in a `.md` file (ignored), a `lean:` inside a string literal with no comment token (ignored), a marked skip, an unmarked skip, a file under a `skip` folder (ignored).
- Counts and kinds match; output never exceeds 50 item lines while counts stay full.
- Fingerprints survive a line shift; `stamp` saves the snapshot; a new marker then shows as `NEW 1`, a removed one as `GONE 1`.
- No `docs/core`: the scan still runs and prints `no baseline`.
- lean rule text: the injected Build section holds the marker form and the English rule.

## Out of scope

- Files without tests, coverage, and flaky detection from test history: noisy or costly to compute; a flaky test is recorded by its marker instead.
- Paying debt automatically, or ranking it beyond counts.
- A separate debt document or dashboard.
