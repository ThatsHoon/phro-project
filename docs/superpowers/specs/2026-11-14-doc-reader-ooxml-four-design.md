# doc-reader phase 2c: the engine reads docx, pptx, xlsx and xlsm

Approved by the user on 2026-11-14. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec), whose decision 1 left these four with `anthropic-skills:*`; the engine spec edits listed at the end are made in the same commit. Survey behind it: `research/ai-document-reader-plugins/notes/ooxml-four-survey.md` (outside this repository).

## Goal

The gate converts the OOXML files that Anthropic's skills used to read: docx, pptx, xlsx and xlsm, plus the template and macro variants dotx, dotm, potx, potm, ppsm, xltx and xltm. This phase moves reading only. Creating and editing these files stays with `anthropic-skills:docx`, `:pptx` and `:xlsx`. pdf follows in its own phase.

Success, as in the engine spec, with these baselines:

- The route for each format is the skill's own read command run on the original file, without LibreOffice in between: `builtin>docx` (pandoc), `builtin>pptx` (`slide_text.py`) and `builtin>xlsx` (markitdown).
- Fidelity is at least the route's minus 0.01, and calibrated tokens are at most the route's plus 5%.
- The skill's own load (its SKILL.md, read once per session) is reported in its own column. It is not added to any document's tokens.
- A Read or Edit flow that a skill runs is not changed. The skills read through Bash and edit unzipped XML, and the hook sees neither.

## Decisions (user, 2026-11-14)

1. Absorb reading of docx, pptx, xlsx and xlsm now, and pdf after this phase. The evidence is the bench: docm and pptm run the same code, and they score as well as or better than the builtin routes, at about the same text tokens. The skill load the engine saves is about 2,085 o200k tokens for docx, 7,368 for pptx and 2,134 for xlsx.
2. Edit protection takes two parts:
   - Each converted Read of an OOXML-family file gets one sentence (about 30 tokens) in `additionalContext`: the view is read-only, and editing goes through the skill.
   - An environment switch turns the engine off per extension.
3. xlsx output has no cell addresses, as with xls, xlsb and ods.
4. A formula cell shows its cached value. Only when there is no cached value does it show the formula.
5. In scope: OMML equations as TeX, SmartArt text, slide hyperlinks and comments, and the template and macro extensions.

## Defaults (proposed with this spec, approved with it)

- **Failure.** A failed conversion denies, as now, and the reason also names the skill route: "… or read it with the xlsx skill".
- **xlsx hyperlinks and comments.**
  - An external hyperlink becomes an inline link in its cell.
  - A cell comment becomes a footnote `[^n]` that reads "Comment by Author: …", as in docx and ods.
  - `xl/tables` is not read, because a pipe table already has a header row.
- **Slide comments** go after the slide's notes, one line each: `> Comment by Author: …`. A slide comment has no place in the text, so a footnote at the end of the document would be far from its slide.
- **Macros.** A file with a `vbaProject` part lists "VBA macros" among the parts not read. The gate already shows that list ("Some parts could not be read: …"), so this costs no new code path.
- **A zip saved as .xls or .xlsb** is read by the xlsx reader. It is no longer denied as "xlsx workbook saved as …".
- **Bench routes and image tokens.**
  - The builtin routes run each skill's own command: pandoc without `--extract-media`, as in `docx/SKILL.md`.
  - Those routes give no images, so for image-bearing documents the token bar compares text tokens and lists image tokens beside them. The image item in the fidelity score is where images count. This is the same reason the pptm token MISS was ruled in phase 2b.
- **Corpus.**
  - Public files are chosen by feature, not as the three smallest. The features are tables, images, charts, comments, equations, SmartArt and hidden content.
  - Tika's test documents are added.
  - The xlsm seed comes from LibreOffice's macro-enabled Excel export.

## Design

### Gate and format table

`scripts/reader/__init__.py`:

- `FORMATS` maps all eleven new extensions to `"ooxml"`:
  - Word: docx, dotx, dotm
  - PowerPoint: pptx, potx, potm, ppsm
  - Excel: xlsx, xlsm, xltx, xltm
- `BUILT` gets the same eleven.
- `SKILL_EXTS` becomes `{"pdf"}`.
- The format count goes from 29 to 40.

**Edit hint.** `gate._context` appends one sentence for the OOXML family. The family is docx, docm, dotx, dotm, pptx, pptm, ppsx, potx, potm, ppsm, xlsx, xlsm, xltx and xltm.
- The sentence is `Read-only view (no XML, cell addresses or formulas); to edit the file, use the <docx|pptx|xlsx> skill.`, with the skill named by family.
- Its room is taken before the unread list and the heading index are fitted, so the whole stays under `CONTEXT_MAX`.

**Off switch.** `PHRO_READER_OFF` holds a comma-separated list of extensions. Matching ignores case, and a leading dot is allowed.
- For a listed extension, `gate.scope` returns None: the hook prints nothing, and Claude's own Read handles the file as it did before this phase.
- The switch works for any extension. Turning off a binary format is the user's choice.
- `read.py convert` ignores the switch.

### Word and PowerPoint

These use the existing `ooxml_word` and `ooxml_slides`, plus four additions.

**OMML to TeX** goes in a new `formats/omml.py` with `tex(el) -> str`. It is ported from anydoc `shared/math/omml.rs` (MIT, credited in `THIRD_PARTY_NOTICES.md`).
- `m:oMathPara` becomes a display `Equation`.
- An `m:oMath` inside a paragraph becomes an inline `Math`.
- It covers r/t, f (bar, skw, lin, noBar), sSup, sSub, sSubSup, sPre, rad (deg, degHide), nary (chr, limLoc, subHide, supHide), d (begChr, endChr, sepChr), m, eqArr, acc, bar, groupChr, limLow, limUpp, func, box, borderBox and phant.
- An unknown element gives its text.
- PowerPoint stores math as `a14:m` inside `mc:AlternateContent`. The slide reader accepts that Choice for this element, and the fallback picture is not emitted.

**SmartArt text** is read from the data part, which `dgm:relIds@r:dm` names (in docx a `w:drawing` graphic, in pptx a `p:graphicFrame`).
- Nodes are the `dgm:pt` points with no type or type `node`. The `doc` point is the root.
- Parents come from `dgm:cxn` with no type or type `parOf`, ordered by `srcOrd`.
- A node's text is its `dgm:t` paragraphs.
- Output is `ListItem`s, with each item's level set by its depth in the tree.
- A node with no text is skipped, and its children move up a level.
- A node cap, `SMARTART_NODES` (5,000), joins `limits.py`.

**Slide hyperlinks.**
- An `a:hlinkClick` on a run with an external relationship becomes a `Link`.
- An internal jump (`ppaction://…`) keeps the text only.
- Shape-level links are not read.

**Slide comments.** There are two forms:
- Legacy: the `…/comments` relationship, with authors in `ppt/commentAuthors.xml` by `authorId`.
- Modern: the `…/2018/10/relationships/comments` relationship, with authors in `ppt/authors.xml` by id, and replies in `replyLst`.

Comments, replies included, go after the notes in file order. A hidden slide hides its comments with it.

### Excel

A new `formats/ooxml_sheets.py` is reached from `ooxml.parse` when the main part's root is `workbook`.

**Workbook.**
- Sheets follow the `workbook.xml` order.
- `state` hidden or veryHidden gives `Section("sheet", name, hidden="hidden")`.
- A chartsheet gives its chart tables.
- `workbookPr@date1904` is honoured.

**Strings.**
- `sharedStrings.xml` is streamed.
- The text of an `si` is its direct `t` plus its `r/t`. `rPh` and `phoneticPr` are dropped.
- `_xHHHH_` escapes are decoded.
- The total length counts toward `SHEET_CHARS`.

**Styles.** `numFmts` and `cellXfs` feed `numfmt.code_for`, and display goes through `numfmt.display`.

**Cells.** Each sheet part is streamed with `iterparse`, and every closed `row` is cleared.

| Cell type | Shown as |
|---|---|
| `n` | the displayed number |
| `s` | the shared string |
| `str` | the formula's text result |
| `inlineStr` | the inline string, by the same rules as shared strings |
| `b` | TRUE or FALSE |
| `e` | the error text |
| `d` | the ISO date, through the number format |

For formulas (decision 4):
- A cached `v` wins.
- A formula with no `v` shows `=` plus its `f` text.
- A shared-formula cell whose `f` has no text stays empty.

**Rows, columns and merges.** Hidden rows and columns and merged cells go through `sheet_grid`, exactly as for xls and xlsb.

**Hyperlinks.**
- An external link (through the sheet's relationships) becomes a `Link` in the cell.
- An internal `location` keeps the text only.

**Comments.**
- A legacy comments part gives a footnote on its cell.
- Threaded comments (`threadedComments` with `persons`) win where present. The legacy placeholder for the same cell is then dropped.

**Drawings.** Images and charts go through a module shared with xlsb. The code in `xlsb.py:316-410` moves into a new `formats/sheet_drawing.py`, so both formats use it.

**Streaming and budgets.**
- `zipx.Package.open(name)` gives a stream after the same checks as `read`: the encryption bit, `ZIP_RATIO`, and `ZIP_TOTAL` summed over the parts.
- The DOCTYPE and ENTITY refusal checks the stream's prefix, up to the root element.
- A new `SHEET_SCAN` (2,000,000) caps the cells scanned per workbook, styled empty cells included. Past it, the sheet is cut with the existing `sheet_cut` warning. The plan's first task measures it and lowers it if 2,000,000 styled cells take more than 20 seconds.
- Each sheet is isolated: a failed sheet is skipped and counted. A workbook with no readable sheet is `damaged`.

**Renamed files.**
- `xls.parse`, `xlsb.parse` and `xls_alike.kind` hand a zip workbook to this reader.
- The three tests that expect the denial change.

### Model

`sheet_grid.Grid.put` accepts inlines (`Link`, `NoteRef`) as well as plain text. `Equation` and `Math` already exist, so no renderer change is planned.

### Bench

The bench lives in `research/ai-document-reader-plugins/bench`, a local git repository.

- **Harness.** `common.BUILTIN` becomes `{"pdf"}`, and the bench's tests change with it. `adapters/phro_read.py` gets docx, pptx, xlsx and xlsm.
- **Builtin routes.** `adapters/baseline.py` skips LibreOffice when the input already is docx, pptx, xlsx or xlsm, and runs the skill's own command. `report.py`'s route text follows.
- **Peer engines.** anydoc, xberg, docling, dochan and markitdown get the four extensions where they read them natively.
- **Skill load.** A new report column holds SKILL.md tokens (o200k) times the `text_en` calibration factor, once per format.
- **Corpus.** It takes the generated docx, pptx and xlsx seeds, a new xlsm seed, public files chosen by feature, and Tika.
- **Addendum.** The phase ends with `reports/written/phro-read-phase2c.md`.

## Testing

All fixtures are built inside the tests, as now.

**Gate.**
- docx, pptx, xlsx and xlsm are converted, and pdf passes through.
- The edit sentence is present, and the context stays under 600 characters.
- `PHRO_READER_OFF` passes a listed extension through and ignores unlisted ones.
- `a.docx` leaves the pass-through list in `test_reader_gate.py`.

**OMML.**
- One test per construct in the list above.
- Nesting: a fraction inside a radical inside a superscript.
- Display versus inline.
- pptx `a14:m` replaces its fallback.

**SmartArt.**
- Tree order by `srcOrd`.
- A textless node promotes its children.
- The node cap.
- A missing data part is counted as lost, not fatal.

**Slides.**
- External and internal hyperlinks.
- Legacy and modern comments with authors and replies.
- Comments on a hidden slide.

**xlsx.**
- Every cell type, and 1904 dates.
- Shared strings with `rPh` and `_x000D_`.
- A formula with a cache, without one, and a shared formula without one.
- Hidden sheets, rows and columns, and merges.
- Comments, both legacy and threaded.
- Hyperlinks.
- A chartsheet, and drawing images and charts.
- `SHEET_SCAN` on a sheet of styled empty cells built in memory.
- A DOCTYPE in a sheet part.
- A failed sheet among good ones.
- A zip named .xls or .xlsb.

**Templates.** One file each for dotx, potx and xltx through the right reader.

## Engine spec edits made with this amendment

| Engine spec line | Edit |
|---|---|
| 5 | 28 formats → 39 formats in scope (the count of extensions, oxps included, is 40) |
| 12 | Name the route baselines above, and say that skill load is its own column |
| 19 | Add decision 7 (2026-11-14), which points to this spec. pdf alone stays with the skills, until its phase |
| 35 | Row docm/pptm/ppsx → OOXML (docx, docm, dotx, dotm, pptx, pptm, ppsx, potx, potm, ppsm), plus OMML and SmartArt in the taken column |
| 39 | New row xlsx/xlsm/xltx/xltm. Base: anydoc xlsx (phonetics, hidden, merges). Taken from dochan: streaming, comments, links. Gaps: no formula cache, scan cap |
| 98 | "Formulas are off" → the cached value, else the formula (xlsx) |
| 100 | Add the slide comment line and the cell hyperlink rule |
| 112 | "the 28" and "the built-in five" → "pdf"; add the edit sentence and `PHRO_READER_OFF` |
| 116 | The skill route is named in the denial for the OOXML family |
| 126 | Add `SHEET_SCAN` and `SMARTART_NODES` |
| 136-143 | Add phase 2c (this spec), then 2d (pdf, starting with rea), before 3 and 4 |
| 147-151 | Add the gate's OOXML tests and the xlsx streaming test |
| 155 | "the five built-in formats" → "pdf until phase 2d" |

The bench spec (`2026-11-11-doc-reader-bench-design.md:34-39, 51-53, 130-131`) gets the same scope change.

## Out of scope

- Editing and writing these files.
- Cell addresses.
- Formula text beside a cached value.
- Translating a shared formula that has no cache.
- `xl/tables`, pivot caches, slicers, sparklines and form controls. A pivot table still shows as the cell values its sheet holds.
- VBA.
- Shape-level slide links.
