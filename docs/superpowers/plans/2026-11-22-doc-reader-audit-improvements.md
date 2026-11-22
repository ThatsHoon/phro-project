# doc-reader audit improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Every task is test-first (superpowers:test-driven-development).

**Goal:** Close the gaps the 2026-11-22 audit found between the reader and a frontier-grade reading engine: content an AI model never receives, tokens that buy no understanding, time that buys nothing, and a delivery path that leaves the model without a next step.

**Architecture:** No new architecture. Each task fixes one cause in the place every format shares when there is one (`markdown.py`, `charts.py`, `sheet_grid.py`, `gate.py`, `cache.py`, `diagram.py`), and in the format parser only when the cause is the format's. Parsers still emit `reader/model.py` blocks and never write Markdown.

**Tech Stack:** Python 3.12, the reader's own code. No new dependency, no download, no install.

**Spec:** the audit reports are the spec. They sit in `.superpowers/sdd/2026-11-22-doc-reader-audit/audit/` of this worktree: `output.md`, `word.md`, `sheet.md`, `slides.md`, `pages.md`, `perf.md`, with `common.md` (the audit brief, which states the owner's goal). A task names its findings as `<report> §N`. A finding gives the evidence (doc_id, measured numbers), the cause (file:line) and a fix design. Where this plan's "Decisions" section and a finding's fix design disagree, the Decisions section wins.

## Global Constraints

- The bars: real AI understanding, token efficiency, compute efficiency. Fidelity is never traded for lightness. Code volume, effort and crash risk are not costs.
- Standard library only is NOT a goal, but nothing is installed or downloaded in this plan.
- Tests are plain scripts, one per file: `python -I -X utf8 tests/test_reader_<name>.py` (exit code 0 is a pass; pytest is not installed). The whole suite is every `tests/test_*.py` run that way from the worktree root. A task ends with the whole suite green, and names any test it had to change and why.
- Corpus checks use only `public` and `generated` rows of `C:/Users/hunje/research/ai-document-reader-plugins/bench/corpus/index.tsv`; documents are named by doc_id only; a `real-*` document is never opened. Python that reads a corpus file runs with `-I`. The bench repository is read-only here.
- Never run: `read.py setup`, `claude -p`, bench.py `calibrate`, `qa` or `--help`, `cargo`. Never push, amend, rebase or stash. Never stage `docs/` or `.superpowers/`. Leave `scripts/reader/formats/rtf_lex.py` alone unless the task names it.
- Code carries no comments (project rule), except a spec link for behaviour an outside format forces.
- Budgets live in `reader/limits.py`; every new loop over untrusted input is bounded by a budget there and by the conversion deadline.
- Hidden content keeps the existing rule: it is left out unless `--hidden`, and what was left out is counted in the status line.
- A change that alters output shape for every format (Decisions 1-3) lands in the shared renderer once; no format writes its own variant.

## Decisions this plan makes (the owner may veto them at review)

1. **Pipe tables lose their outer pipes.** A row is `a|b|c`, the delimiter row `-|-|-`. A row whose first or last cell is empty, and every row of a one-column table, keeps both outer pipes. GFM allows this; it saves 13.6 % of sheet tokens (`output.md` F5).
2. **A table with merged cells takes the cheaper of two lossless forms.**
   - *Sparse merges* (merged regions ≤ max(3, rows // 20)): a pipe table, each merged region's text in its top-left cell and the covered cells empty, followed by one line `Merged: A1:C1, B4:B6` in A1-style addresses of the table grid (row 1 is the first row written, header included; a sheet table uses the sheet's own addresses).
   - *Dense merges:* an HTML table in compact form: no end tags for `tr`, `th`, `td`, unquoted `rowspan=2 colspan=3`, no whitespace between tags.
3. **A picture link says what opening it costs and is written once when it repeats.** Alt text ends with the pixel size (`![chart 640x480](assets/img-003.png)`). A picture placed in three or more sections (pages, slides, sheets, chapters) is linked at its first placement only, with `on N pages` (or slides, sheets) in the alt text. File names stay `img-NNN`.
4. **A conversion the deadline cut is still one stable result.** It is stored under its key and returned as is on the next read; the context names what is missing and the exact `pages="A-B"` read that gets it. Nothing reconverts the whole file on a re-read.
5. **Formulas are content.** Each sheet's formulas follow its table as one block, grouped by R1C1 pattern (`Formulas: D2:D2001 =B2*C2; E1 =SUM(D2:D2001)`). A formula cell with no cached value shows its formula in the cell.
6. **A sheet's tables carry their addresses.** A sheet is split into its rectangular islands; each table's caption gives its range (`B3:F20`), and hidden or cut rows are named in it, so line arithmetic gives a cell address.
7. **VBA source is read and kept out of the main text.** `out.md` lists the modules (name, kind, line count); the source is written to `assets/vba/<module>.bas` for the model to open on demand, like a picture.
8. **A scanned page that sits under a text layer is not exported as a picture.** Its text is already given; the page can be viewed through the existing `pages="A-B"` hint. A picture of one flat colour is dropped.
9. **Embedded objects go through one dispatcher** (`reader/embedded.py`) that sniffs the bytes and hands them to the parser the reader already has (workbook, document, ODF chart, equation). A caller never keeps a preview picture of an object whose content was read.

## Review Focus

1. A table whose first column is empty in some rows (Decision 1): the empty cell must survive a round trip through a GFM parser.
2. A merged region that crosses the 100,000-row cut or a hidden row (Decisions 2, 6): the `Merged:` line and the caption must still give true addresses.
3. A partial result (Decision 4) read again after the reader's code changed: the code digest must still retire it.
4. A workbook where every cell is a formula (Decision 5): the formulas block must stay under the sheet budgets and group, not list.
5. A VBA project with a module named like a path (`..\x`), or thousands of modules (Decision 7): names are sanitized and counted against a budget.

---

## Streams

Tasks are grouped into streams that own disjoint files. A stream runs in its own worktree and branch (`audit/<stream>` cut from `doc-reader-audit`), its tasks in order, one commit per task. Wave 2 streams start after wave 1 is merged.

| Stream | Wave | Owns |
|---|---|---|
| S1 delivery | 1 | `gate.py`, `cache.py`, `scripts/read.py`, `hooks/hooks.json` |
| S2 charts | 1 | `charts.py`, `formats/biff_chart.py`, `formats/hwp_chart.py`, chart reading in `formats/odf_base.py` |
| S3 sheets | 1 | `formats/sheet_grid.py`, `ooxml_sheets.py`, `odf_sheets.py`, `xls.py`, `xls_alike.py`, `xlsb.py`, `iwork_tables.py`, `containers/crypto.py` |
| S4 hancom | 1 | `formats/hwpx.py`, `hwp.py`, `hwp_records.py`, `hancom.py` |
| S5 word | 1 | `formats/rtf.py`, `rtf_table.py`, `ooxml_word.py`, `word_fields.py`, `omml.py`, `odf.py`, `doc.py` |
| S6 slides | 1 | `formats/ooxml_slides.py`, `odf_slides.py`, `ppt_text.py`, `ppt_shapes.py`, Keynote part of `iwork.py` |
| S7 diagram | 1 | `diagram.py`, `formats/vsd.py`, `vsd_stream.py`, `vsdx.py` |
| S8 pdf | 1 | `formats/pdf.py`, `pdf_layout.py`, `pdf_tables.py`, `pdf_pdfium.py`, `pdfnative/*` |
| S9 ocr-ebook | 1 | `ocr.py`, `formats/epub.py`, `mobi.py`, `html.py`, `css.py` |
| S10 renderer | 2 | `markdown.py`, `images.py`, `text.py`, `formats/jet.py` |
| S12 runtime | 2 | `reader/__init__.py`, import structure of every module, `containers/zipx.py`, `containers/palmdb.py`, `text.py` (`garble_ratio`), the deadline checks in every format's main loop |
| S11 embedded | 2 | new `embedded.py`, new `containers/vba.py`, call sites in `doc_objects.py`, `ooxml_word.py`, `rtf.py`, `ooxml_slides.py`, `ppt_objects.py`, `vsd.py`, `ooxml_sheets.py`, `xls.py` |

---

### Task 1 (S1): a cut conversion is stored and stable

**Findings:** `output.md` F1, §5 last two bullets. **Files:** `scripts/reader/cache.py`, `scripts/reader/gate.py`; tests `tests/test_reader_cache.py`, `tests/test_reader_gate.py`.

- `cache.store` keeps a result that carries `limits.FIGURES_LATE` or `limits.OCR_LATE` under its key, with `partial: true` in its meta. `cache.lookup` returns it. The code digest and the backend ids retire it as they retire any entry.
- The gate's context for a partial entry names what is missing (pages, pictures) and the read that gets it: `Read the pdf with pages="A-B"` for pages past the cut, in runs.
- Tests: a conversion with a stand-in deadline returns the same `out.md` path and bytes on a second `decide`, starts no child the second time, and its context names the missing page runs.

### Task 2 (S1): the context tells the model where things are

**Findings:** `output.md` F2, F3, F4, §5. **Files:** `gate.py`, `cache.py`; tests `tests/test_reader_gate.py`.

- When the result has pictures, the context gives the absolute `assets` directory once.
- One index rule for every format: `line title` entries without quotes, titles cut at 30 characters; when the index passes the room it has, it keeps the top heading level only, and past that an even sample (the pdf page index's way). A truncated index is also written in full to `index.md` beside `out.md`, and the context names that path.
- A read with no `offset`/`limit` of an output over 20,000 estimated tokens (or 240 KB) gets a `limit` that fits, and the context says `lines 1-N of M; continue with offset=N+1`. The estimate is per line, kept in meta at store time: 1 token per 3 UTF-8 bytes.
- Tests: assets path present only with pictures; a 64-heading document's index names every top-level heading or names `index.md`; a 700 KB output gets a limit and the continue line; an explicit `offset` is passed through untouched.

### Task 3 (S1): deny texts name the next step

**Findings:** `output.md` F9. **Files:** `gate.py`; tests `tests/test_reader_gate.py`.

- The retry command is attached to `timeout` only. `damaged`, `unsupported`, `encrypted`, `drm`, `too_large` end with one sentence saying what the model can do (ask the user for an unprotected copy, for the file saved in a named format, or name the limit hit). No nested parentheses.
- Tests: one per kind, asserting the sentence and the absence of the retry command.

### Task 4 (S1): a Read of a non-document pays almost nothing

**Findings:** `output.md` F8, `perf.md` (fixed cost, cache sections). **Files:** `scripts/read.py`, `gate.py`, `cache.py`; tests `tests/test_reader_gate.py`, `tests/test_reader_cache.py`.

- `read.py hook` decides "not a document" from the event's `file_path` extension before importing `argparse`, `pathlib` or `reader`.
- The code digest is not recomputed per read: it is kept in a file keyed by each source file's (path, size, mtime) and recomputed only when that list changes.
- A document whose (path, size, mtime) matches a remembered key skips the content hash; the key itself stays the content hash.
- Tests: the hook on a `.py` path imports none of `argparse`, `pathlib`, `reader` (asserted in a child process through `sys.modules`); touching a reader source file changes the digest; changing a document's bytes with the same size and a new mtime gives a new key.

### Task 5 (S2): charts keep their units, kind and true values

**Findings:** `output.md` F7, `slides.md` §3, `sheet.md` §5. **Files:** `charts.py`, `formats/biff_chart.py`, `formats/hwp_chart.py`, `formats/odf_base.py` (chart part); tests `tests/test_reader_charts.py`, `tests/test_reader_biff_chart.py`, `tests/test_reader_ods_odp.py`.

- Caption: kind with direction and grouping (`column`, `bar`, `stacked column`, `100% stacked bar`), title, axis titles `(x: Year; y: Units)`.
- Category values that are date serials under a date format are written as dates; General numbers are written with at most 15 significant digits (`4.4`, not `4.4000000000000004`).
- Scatter and bubble series each keep their own x values; multi-level categories keep every level.
- A chart whose series all come from one range of the same sheet names that range in its caption (`Data: Sheet1!A1:C9`). The chart table is always kept here; dropping rows the sheet output already holds is Task 27.
- An ods chart is one table with its title, not cells joined by ` / ` and ` ; `.
- Tests: one per bullet, on in-memory chart parts.

### Task 6 (S3): sheet tables carry addresses; cuts and hidden rows are said

**Findings:** `sheet.md` §2, §4, §8, §9, §15. **Files:** `formats/sheet_grid.py`, `ooxml_sheets.py`, `odf_sheets.py`, `xls.py`, `xlsb.py`, `iwork_tables.py`; tests `tests/test_reader_sheet_grid.py` and the format tests.

- Decision 6. Islands are split on fully empty rows and columns; an island of one cell stays a paragraph with its address.
- A sheet cut at `SHEET_ROWS`, or left unread because `SHEET_CELLS` ran out, says so where the cut is, with the row numbers and counts.
- Hidden rows, columns and sheets are named by address in the caption or status line; ods hidden columns follow the hidden rule.
- Tests: three islands on one sheet give three captioned tables; a 100,001-row sheet names the cut row; a sheet after the cell budget says it was not read; hidden row runs appear in the caption; ods hidden column left out and counted.

### Task 7 (S3): formulas reach the model (xlsx, xlsm, ods)

**Findings:** `sheet.md` §1. **Files:** `ooxml_sheets.py`, `odf_sheets.py`, `sheet_grid.py`; tests `tests/test_reader_sheet_grid.py`, format tests.

- Decision 5. Shared formulas are expanded to their R1C1 pattern for grouping, never cell by cell in the output. A cell that breaks its column's pattern is listed on its own (that is how a hard-coded cell shows).
- Budgets: formula text is charged to `SHEET_CHARS`; groups past a new `limits.FORMULA_GROUPS` are counted, not listed.
- Tests: fill-down grouped to one entry; a pattern break listed separately; `<f>` with empty `<v>` shows the formula in the cell; ods `table:formula` read; the budget cut is said.

### Task 8 (S3): formulas in xls and xlsb

**Findings:** `sheet.md` §1. **Files:** new `formats/ptg.py`, `xls.py`, `xlsb.py`, `biff.py`, `containers/biff12.py`; tests new `tests/test_reader_ptg.py`.

- One decompiler for the parsed-expression tokens both formats share (MS-XLS 2.5.198, MS-XLSB 2.5.97): operands, operators, function calls by the built-in function table, 3-D references through the sheet tables, shared and array formulas. An unknown token gives `=?` for that formula and a count, never a wrong formula.
- The output joins Task 7's block.
- Tests: each token class from hand-built token bytes; shared formula; unknown token counted.

### Task 9 (S3): workbook-level content, links and notes

**Findings:** `sheet.md` §6, §7, §10 (the "macros present" line only), §14. **Files:** `ooxml_sheets.py`, `xls.py`, `xlsb.py`, `odf_sheets.py`, `containers/crypto.py`; tests in the format tests.

- xlsx/xlsm/xlsb/xls: defined names, data-validation lists, tables (ListObjects: name, range, totals row), autofilter range, pivot tables (source, row/column/data fields; the cached result stays as the cells show it), conditional-format rules as one line each, sheet protection as a flag, external link targets. Each is a short block after the sheet's tables; none repeats cell values.
- xls `HLINK` and `NOTE` records, xlsb `BrtHLink` and comment parts: links on their cells, notes with cell address and author.
- The slicer fallback sentence is not content.
- `containers/crypto.py` builds its tables on first use, not at import, and builds a key's round keys once per key, not once per block (`perf.md` F5: 25,858 key schedules, 71 % of one workbook's time).
- Tests: one per construct on in-memory parts or records.

### Task 10 (S4): hwpx fields, captions and outline numbers; hwp outline numbers

**Findings:** `word.md` §2, §3, §4, §11. **Files:** `formats/hwpx.py`, `hwp.py`, `hancom.py`; tests `tests/test_reader_hwpx.py`, `tests/test_reader_hwp.py`, `tests/test_reader_hancom.py`.

- A filled click-here field's value is text; an empty one's guide text stays out.
- hwpx table and picture captions are read as hwp's are.
- Outline and paragraph numbers are computed and written for hwpx; hwp writes the number it consumes on a demoted outline paragraph.
- The application's automatic object description is not alt text.
- Tests: one per bullet.

### Task 11 (S4): Hancom inline objects and the equation script

**Findings:** `word.md` §7, §10. **Files:** `hwpx.py`, `hwp.py`, `hwp_records.py`, `hancom.py`; tests as Task 10.

- Ruby keeps base and ruby text; memos become comments; text art gives its text; form objects give their value or state; an OLE object is counted when not read.
- The equation script translator is correct for subscripts before `over`, `LEFT`/`RIGHT`, `matrix`, `cases`, `root`, `from`/`to`, and writes what it cannot translate as the raw script in a code span, never as wrong TeX.
- Tests: each construct; the `x_i over n` case.

### Task 12 (S5): RTF notes; tracked changes mean what they are

**Findings:** `word.md` §6, §9. **Files:** `formats/rtf.py`, `ooxml_word.py`, `odf.py`, `doc.py`, `model.py`; tests `tests/test_reader_rtf.py`, `tests/test_reader_odt.py`, docx tests.

- RTF `\footnote`, `\annotation` and `\deleted` are a note, a comment and deleted text.
- Insertions and deletions are distinct from hidden text in the model (`hidden="deleted"` exists; add `"inserted"` as a mark that is shown), carry the author where the format has one, and sit at their place in the text (odt).
- Tests: per format, per kind.

### Task 13 (S5): Word numbering, forms and special runs; properties; math noise

**Findings:** `word.md` §12, §13, §14, §15, §16, §17, §18. **Files:** `ooxml_word.py`, `word_fields.py`, `omml.py`, `doc.py`, `doc_fib.py`, `mtef.py`; tests in the docx and doc tests.

- Numbering through `numStyleLink` and every `numFmt`; content-control and legacy form state; ruby; `w:sym`; `altChunk`; VML text art.
- Document properties (title, author, dates, custom ones that are set) as one compact line block at the top, for every word format that has them.
- A `.doc` with no table stream reads what its main stream holds; upright OMML runs are not wrapped in `\mathrm{}`; a doc OLE preview is written once.
- Tests: one per bullet.

### Task 14 (S6): slides keep their structure

**Findings:** `slides.md` §4, §6, §7, §8, §9, §12. **Files:** `formats/ooxml_slides.py`, `odf_slides.py`, `ppt_text.py`, `ppt_shapes.py`, `iwork.py`; tests `tests/test_reader_slides.py`, `tests/test_reader_ppt.py`, `tests/test_reader_iwork.py`.

- Bullet levels inherited from layout and master.
- Notes in any shape of the notes page.
- Master and layout content that is not a placeholder (a table on a Keynote master, footers, an odp master's text) is written once, labelled, not per slide and not lost.
- Reading order by a shared XY-cut over shape boxes (title first), with a tolerance for rows off by a fraction of a line.
- Shape and picture click links, slide jumps, action buttons and media are named.
- Tests: one per bullet.

### Task 15 (S7): diagrams connect named shapes

**Findings:** `slides.md` §1, §2, §10, §11, `output.md` U2. **Files:** `diagram.py`, `formats/vsd.py`, `vsd_stream.py`, `vsdx.py`; tests `tests/test_reader_diagram.py`, `tests/test_reader_vsd.py`, `tests/test_reader_vsdx.py`.

- Shapes have parents: a connector end on a port or a textless child resolves to the nearest ancestor with text; glue to another connector is followed.
- Tree mode never drops an edge silently.
- A connector's label is on its edge; shape kinds that carry meaning (decision, callout, chevron) are written; Shape Data, hyperlinks, master names and background pages are read.
- Tests: grouped shapes with a connector to a child; connector-to-connector glue; tree mode with an empty child; a labelled edge; a decision shape.

### Task 16 (S8): pdf text is words and paragraphs

**Findings:** `pages.md` §4, §5, §7, §11, §12, §13, `output.md` F11. **Files:** `formats/pdf_layout.py`; tests `tests/test_reader_pdf_layout.py`.

- The word gap and the paragraph gap are measured per page (or per font run) from the page's own distribution, not fixed at 0.2 em and 1.5 lines.
- Running lines are found with page numbers normalized anywhere in the line and in the side margins.
- Vertical CJK columns set with horizontal fonts are read as columns; a heading that wraps is one heading; mathematical alphanumerics are folded to plain letters.
- Tests: a tight-set line keeps its spaces; 160 % leading gives one paragraph; a header with a page number in the middle is removed from the body; a vertical column keeps its punctuation in place.

### Task 17 (S8): pdf pictures, figures, forms and navigation

**Findings:** `pages.md` §1, §2, §8, §9, §15, §16, §17. **Files:** `formats/pdf.py`, `pdf_pdfium.py`, `pdfnative/*`; tests `tests/test_reader_pdf.py`, `tests/test_reader_pdfium.py`, `tests/test_reader_pdf_native.py`.

- Decision 8.
- A page picture is encoded to PNG only after it is chosen for the output, and at zlib level 1 (`perf.md` F4: 64 % of one document's time went to encoding pictures before the choice; under load the page timed out and the same input gave no pictures).
- A vector chart is a figure: the figure test counts what the paths draw, not only how many there are; a figure's own text stays with the figure and the figure sits between paragraphs of its column.
- AcroForm fields at their place, with empty fields and choices; the outline and page labels feed the heading index; attachments are listed; an IRM wrapper is `drm`.
- Tests: one per bullet.

### Task 18 (S8): tagged pdf structure is used

**Findings:** `pages.md` §6. **Files:** new `pdfnative/structure.py`, `formats/pdf.py`, `pdf_layout.py`; tests new `tests/test_reader_pdf_structure.py`.

- When a pdf is marked and has a StructTreeRoot, headings, lists, table cells, figure Alt and ActualText come from the tree through MCIDs, and Artifact content is left out. Layout stays the fallback for content the tree does not cover and for unmarked files.
- Tests: a hand-built tagged pdf with H1, L/LI, Table, Figure Alt and an Artifact header.

### Task 19 (S9): OCR does not hand over junk

**Findings:** `pages.md` §3, §18, `output.md` F12, `perf.md` F1. **Files:** `ocr.py`, `limits.py`; tests `tests/test_reader_ocr.py`.

- Picture text passes the same doubt signals as a page; doubtful picture text is not written.
- A picture is sent to OCR only when a cheap look at its pixels says it can hold text (the ink and rule measures `ocr_pixels` already has), and a Latin page that Windows read with no doubt is not read again by Tesseract (`perf.md` F1: OCR is 57 % of wall time and 76 % of CPU over the 36 documents that start it; one e-book takes 8.0 s with it and 0.73 s without, for 480 bytes of text).
- A junk page is read again at another scale and by the other engine before it is called doubtful, and the best read is kept by the doubt signals, not by `_score` alone.
- Tests: with stand-in engines.

### Task 20 (S9): e-books keep headings, math and hidden rules

**Findings:** `pages.md` §14, `output.md` U4. **Files:** `formats/epub.py`, `mobi.py`, `html.py`, `css.py`; tests `tests/test_reader_epub.py`, `tests/test_reader_mobi.py`.

- Headings are headings; no `# Unknown`; MathML becomes TeX through the existing math path; `visibility:hidden` follows the hidden rule; a drop-cap picture's letter is kept.
- Tests: one per bullet.

### Task 21 (S10): tables and pictures in the cheapest lossless form

**Findings:** `output.md` F5, F6, F10, `sheet.md` §3, §12, `word.md` §1, `pages.md` §1. **Files:** `markdown.py`, `images.py`, `text.py`, `formats/jet.py`; tests `tests/test_reader_markdown.py`, `tests/test_reader_images.py`, `tests/test_reader_jet.py`, and every test whose expected table text changes.

- Decisions 1, 2, 3.
- Access: the column types move into the header cells and the `Columns:` line goes; a one-row table wider than 12 columns is written as `name: value` lines.
- A paragraph over 2,000 characters is broken at sentence ends.
- Tests: outer pipes kept only where Decision 1 says; sparse and dense merge forms; compact HTML parses back to the same grid (with `html.parser`); a running picture linked once with its count; size in every alt.

### Task 22 (S11): embedded objects are read

**Findings:** `word.md` §5, `slides.md` §5, §13.1. **Files:** new `embedded.py`, call sites; tests new `tests/test_reader_embedded.py` and the callers' tests.

- Decision 9.
- Tests: an xlsx inside a docx, a LibreOffice chart package inside a doc and a ppt, an Equation.3 object, an `p:oleObj` workbook, a vsd 0x1F object.

### Task 23 (S11): VBA source

**Findings:** `word.md` §8, `sheet.md` §10. **Files:** new `containers/vba.py`, call sites in the OOXML and CFB formats, `markdown.py`; tests new `tests/test_reader_vba.py`.

- Decision 7. MS-OVBA decompression and the `dir` stream; budgets in `limits.py` for modules and bytes.
- Tests: a compressed container round trip from a hand-built chunk; a module list; a hostile module name.

### Task 25 (S1): time limits hold on every path

**Findings:** `perf.md` F7 (gate and CLI parts), F8, F11, F12. **Files:** `gate.py`, `cache.py`, `scripts/read.py`; tests `tests/test_reader_gate.py`, `tests/test_reader_cache.py`.

- A document whose conversion timed out is remembered (by its key and the code digest), so the next Read denies at once with the timeout text instead of paying 45 s again.
- `read.py convert` run by hand has the same deadline as the hook's child, and the venv re-exec has a timeout.
- A `pages="A-B"` read of a pdf with no whole-document entry converts that range only.
- The cache prunes itself at most once a day, off the allow path's clock.
- Tests: second Read of a timed-out document starts no child; a ranged read starts one child for the range only; prune runs once for two reads on the same day.

### Task 26 (S12): no format runs past the deadline, and nothing unused is loaded

**Findings:** `perf.md` F6, F7 (format part), F9, F10, F13. **Files:** see the Streams table; tests in each format's test file and a new `tests/test_reader_deadline.py`.

- One deadline clock, set by `reader.convert`, checked in every format's record, row, page and part loops (19 formats have count caps only). Past it the format stops, keeps what it read and says what it did not read.
- A read imports only what its format uses: OCR and raster load when a picture needs them; xlsx does not load the xls modules.
- The per-unit Python loops the profiles name are replaced with C-speed calls: `zipx.local` per member, PalmDOC decompression, `garble_ratio`.
- A mobi's picture records are not copied while the model holds them.
- Tests: a stand-in clock stops each format family mid-document with a partial result and a status line; `sys.modules` after a tiny xlsx read holds no xls, ocr or raster module.

### Task 27 (S10, after Task 21): a workbook chart does not repeat cells the sheet already shows

**Findings:** `sheet.md` §5, §11, review `s2-charts-review.md` Important 1. **Files:** `formats/sheet_grid.py`, `ooxml_sheets.py`, `xlsb.py`, `xls.py`, `iwork_tables.py`, `charts.py`; tests in the sheet and chart tests.

- After a workbook's sheets are read, a chart whose caption names a range keeps only its caption when every cell of that range was written to the output: no hidden row or column inside it, no cut, the sheet shown. Otherwise the chart keeps its table.
- Tests: all cells shown gives caption only; a hidden row inside the range with plotVisOnly=0 keeps the table; a sheet cut by the cell budget keeps the table.

### Task 24: measure

**Files:** none in this repository.

- Run the reader over every public and generated corpus document before (main) and after (this branch): tokens by family, failures, seconds. The after run must lose no content the before run had (a per-document check that every non-markup token of the before text is still present, moved or not), and the report states each family's token change and each new construct's cost.
