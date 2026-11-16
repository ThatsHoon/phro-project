# doc-reader engine: one parser for every document format, behind a Read gate

## Goal

phro-project gets a built-in document reader. When Claude is about to `Read` a document that Claude Code cannot read well by itself, a hook turns it into compact Markdown (plus viewable images) and hands Claude that file instead. One engine does the conversion for all formats in scope: the 28 of decision 1, and since decision 7 also docx, pptx, xlsx and xlsm with their template and macro variants, since decision 8 pdf, with phase 3 the WordPerfect variants wp, wp5, wp6, wp7 and wpt, and with phase 4 the variants mde, accde, accdr, djv, vst, vsdm, vstx and mpt (54 extensions). The standard library is not a goal (the user, 2026-11-16): parsers and libraries are chosen for token efficiency, compute efficiency and the AI understanding every content type; a third-party library is used wherever it measurably beats our own code, installed only on the user's yes. The parsers built so far are Python with no third-party imports; libraries in use today (decision 8: pypdfium2 for pdf, in a plugin-managed venv created only with the user's yes, with a stdlib parser beside it; decision 9: MPXJ for mpp, which reads only through its opt-in venv). It is rebuilt from what reverse engineering the bench's engines taught us, not wrapped around them.

This is stage 4 of the user's request. Stages 1 to 3 (clone, test, report) are done: `C:\Users\hunje\research\ai-document-reader-plugins\bench` and its reports.

Success:

- Every in-scope extension converts through the gate with no software outside the plugin-managed venvs. pdf converts with none at all through its stdlib parser; the optional pdf venv holds only pypdfium2 and is created only with the user's yes (phase 2d). mpp has no stdlib path: it converts only through the mpp venv (MPXJ and JPype), created only with the user's yes, using a Java already on PATH. The plugin never installs Java (phase 4).
- On the bench, for every format with scored documents, the engine's fidelity is at least the current recommended route's minus 0.01, and its calibrated tokens are at most that route's plus 5%. Where it misses, the format's report says why and what it would take. For docx, pptx, xlsx and xlsm the route is the skill's own read command run on the original file, and the skill's own load is reported in its own column (decision 7).
- A document never reaches Claude as raw binary: the gate either swaps in the Markdown or blocks the Read with a reason.
- Nothing is printed into a session that does not read a document. The plugin's always-on token cost does not change.
- No real document's name or content enters this repository.

## Decisions (user, 2026-11-11)

1. Scope: the 28 extensions of the bench that are not read by Anthropic's built-in skills. pdf, docx, pptx, xlsx and xlsm stay with `anthropic-skills:*`; the gate leaves them alone. (Narrowed by decision 7.)
2. One engine for all of them, rebuilt from reverse engineering, not a router over the bench's engines. The bench stays as the yardstick.
3. The gate is a `PreToolUse` hook on `Read` that converts and swaps the path (`updatedInput`). If it cannot convert, it blocks the Read and says why.
4. Permission deny rules may be ignored: the hook does not re-check them against the original path. Personal use.
5. REA (morluto/rea) was installed and run on the engines' binaries. Every engine on the routing table is open source with its source cloned, so the reverse engineering was done on source. Notes: `research\ai-document-reader-plugins\reverse\*.md`.
6. Build order: the gate and the zip+XML formats first, then the rest in phases (see Phases).
7. (User, 2026-11-14.) The engine also reads docx, pptx, xlsx and xlsm, and dotx, dotm, potx, potm, ppsm, xltx and xltm: phase 2c, specified in `2026-11-14-doc-reader-ooxml-four-design.md`. This moves reading only; creating and editing these files stays with `anthropic-skills:*`. pdf stays with its skill until phase 2d.
8. (User, 2026-11-14.) pdf is read by the engine: phase 2d, specified in `2026-11-14-doc-reader-pdf-design.md`. Editing and creation stay with `anthropic-skills:pdf`. The stdlib-only rule is dropped (the user, 2026-11-14, restated 2026-11-16: the bar is token and compute efficiency, never the standard library); for pdf that library is pypdfium2 in a plugin-managed venv, installed only on the user's yes, with a stdlib port of dochan's parser when there is no venv.
9. (User, 2026-11-14.) Phase 4 as specified in `2026-11-15-doc-reader-phase4-design.md`: djvu reads its text layer only; MPL-2.0 ports (libmspub, libvisio) are allowed; mpp is unsupported by default and read only through an opt-in MPXJ venv.

## Reverse-engineering findings that shape the design

No engine wins a format on every axis; each wins different items. The engine takes the best item from each:

| Format | Base | Taken from others | Gaps nobody filled |
|---|---|---|---|
| hwpx | dochan (container, `hp:switch`, image map, charts, footnotes, fields, span grid) | kordoc: numbering, alt-text stripping, layout-table unwrapping, final-state track changes | body text in heading styles (demote long sentence paragraphs) |
| hwp | dochan (CFB reader, distribution docs, OLE charts, budgets) | kordoc: numbering, equation→LaTeX, password; rhwp: control-char semantics, per-item BinData compression, ViewText track changes, DRM sniffing | Hancom-native chart streams, grouped-shape text, BMP images |
| odt/ods/odp | anydoc (style chain, span grid, row templates, display text) | docling: hidden sections and slides, comments, notes fields, chart local table; xberg: group recursion, master pages, hidden sheet marker | hidden text/rows, hidden sheets via table style, odp custom shapes |
| docx/docm/dotx/dotm, pptx/pptm/ppsx/potx/potm/ppsm | anydoc (relationship discovery, style cascade, numbering, `Requires`-aware AlternateContent, grid) | dochan: charts, group geometry; docling: `w:vanish`, hidden slides, outlineLvl first; xberg: SDT placeholder drop; anydoc: OMML → TeX | none of the engines gates correctly on content types: accept by root element; SmartArt text and slide comments, written from the OOXML spec |
| epub | docling (spine merge, link rewrite) | xberg: DRM skip, fallback chains, nav/TOC removal; anydoc: CSS `display:none` cascade, span grid | `epub:type` footnotes, ruby, `linear="no"` |
| xps | none (only AGPL MuPDF) | written from the XPS/OXPS spec | reading order, columns, tables |
| doc/xls/ppt | dochan (FIB walk, piece table, sprms, BIFF8, persist chain, charts, ppt tables) | anydoc: lid code pages, lists, hidden rows/cols | `sprmCFVanish`, hidden slides |
| xlsb | anydoc (record framing, cells) | | both engines abort on one bad record |
| xlsx/xlsm/xltx/xltm | anydoc (phonetics dropped, hidden rows and columns, merges) | dochan: streamed sheets, comments and threaded comments, hyperlinks | formulas with no cached value; sheets of styled empty cells (a scan cap) |
| rtf | anydoc (byte lexer, `\u` with surrogates, edge grid, merges) | xberg: `\v` hidden, `\uc` stack | `\*` mid-group dropping whole sentences |
| wpd | text-first reader from libwpd and Tika knowledge | Tika character sets, the Corel SDK 5.x reference | 6+ hidden text, Mac WordPerfect (LibreOffice opt-in) |
| mobi/azw3 | Tika `parser/mobi` (PalmDB, PalmDOC, HUFF/CDIC) | foliate-js (KF8 assembly, links) | KFX, Topaz, skeleton-less KF8 read from flow 0; fall back KF8 → MOBI6 → stripped text |
| pages/numbers/key | docling (own Snappy, protobuf wire reader, IWA objects) | numbers-parser: field numbers, tile row = tileid × tile size, cell v5; keynote-parser field numbers; an iWork '09 XML reader | trailing empties, `None` in merges, LZVN members, bundle folders |

Licenses: dochan, kordoc, rhwp, anydoc, docling, xberg, markitdown, numbers-parser, foliate-js and keynote-parser are MIT, and Apache Tika is Apache-2.0. Code ported from them is credited in `THIRD_PARTY_NOTICES.md`. djvu-rs is MIT, Jackcess and Apache POI are Apache-2.0, and libmspub and libvisio are MPL-2.0; each file ported from libmspub or libvisio carries the MPL-2.0 header. MuPDF (AGPL), Calibre (GPL), libwpd/librevenge (MPL/LGPL), Apple's `.proto` files (numbers-parser's and keynote-parser's), mdbtools (LGPL), MPXJ (LGPL) and DjVuLibre (GPL) are used as format knowledge only, never copied; MPXJ is installed into the mpp venv and called.

## Architecture

```
scripts/read.py                 CLI and hook entry
scripts/reader/
  __init__.py                   VERSION, FORMATS (ext -> module)
  limits.py                     byte, record, depth, time and output budgets
  model.py                      the document model
  markdown.py                   the one renderer
  images.py                     sniff, size, BMP->PNG, asset naming
  cache.py                      user-level cache
  gate.py                       PreToolUse decision
  text.py                       code pages, garble check, whitespace rules
  html.py                       XHTML walker with a small CSS cascade (epub, mobi)
  containers/  zipx.py cfb.py palmdb.py snappy.py pbwire.py bzz.py crypto.py
  formats/     hwpx.py hwp.py odf.py ooxml.py vsdx.py epub.py xps.py
               doc.py xls.py ppt.py xlsb.py rtf.py wpd.py wpd_charsets.py mobi.py
               iwork.py iwork_iwa.py iwork_text.py iwork_tables.py iwork_xml.py
               djvu.py jet.py jet_pages.py jet_sql.py pub.py pub_2k.py pub_contents.py pub_quill.py
               vsd.py vsd_stream.py mpp.py mpp_mpxj.py
               pdf.py pdf_pdfium.py pdf_layout.py pdf_tables.py
  pdfnative/                    stdlib pdf parser (dochan port)
  pyenv.py pysetup.py           the pdf and mpp venvs: location, ready marker, setup
```

Each format module exposes one function, `parse(path: Path, ctx: Context) -> Doc`. It reads through a container module and emits model blocks; it never writes Markdown. `markdown.render(doc, assets_dir) -> str` is the only place output format and token rules live, so every format gets the same rules.

`read.py` commands:

| Command | Does |
|---|---|
| `hook` | reads the PreToolUse JSON on stdin, prints the hook JSON |
| `convert <file> [--out DIR] [--hidden]` | converts one file, prints the out.md path |
| `formats` | lists extensions and their module |
| `prune [--days 30]` | deletes cache entries not used for N days |
| `setup [--for pdf\|mpp] [--venv DIR] [--check] [--decline] [--remove]` | creates, checks or removes the pdf (default) or mpp venv; run only on the user's yes |

### Document model

```
Doc(title, meta, blocks, notes, warnings, counts)
Block = Heading(level, inlines) | Para(inlines) | ListItem(level, marker, inlines)
      | Table(rows: list[list[Cell(inlines, rowspan, colspan, header)]], caption, hidden_rows, hidden_cols)
      | Image(asset, alt, viewable) | Equation(tex, display) | Quote(blocks)
      | Section(kind: slide|sheet|chapter|page, title, hidden, blocks, notes)
Inline = Text | Bold | Link(url, inlines) | NoteRef(id) | Math(tex) | Break
```

A chart becomes a `Table` with caption `Chart: <type> — <title>`, built from cached values. Formats keep hidden content, comments and tracked deletions in the model, flagged, so the renderer decides.

### Markdown rules

From what cost tokens or lost content in the bench outputs:

- LF line endings, one blank line between blocks, no trailing spaces.
- Inline formatting: bold only. Escape Markdown characters only where they would change the structure.
- Tables: GFM pipes with no padding. Only when a table has merged cells, a compact HTML table with `rowspan`/`colspan`. A merged cell's text is written once. Empty trailing rows and columns are trimmed. A 1×1 table becomes a paragraph; a one-column table becomes lines; an empty nested table is dropped.
- Spreadsheet cells show the displayed value (`₩1,385.50`, dates as formatted), not raw numbers. Formulas are off, except that an xlsx formula cell with no cached value shows its formula (`=SUM(A1:A3)`).
- Slides: `## Slide N: Title`, speaker notes as `> Notes: …`, then slide comments as `> Comment by Author: …`. Sheets: `## Sheet: name`. Master, date, footer and slide-number placeholders are skipped. Access tables and saved queries: `## Table: name (N rows)` and `## Query: name`, the query's SQL in a fenced `Code` block; Publisher master pages: `## Master page: name`.
- Footnotes and comments: `[^n]` in place, definitions at the end of the document. A spreadsheet cell's comment is a footnote on its cell, and an external hyperlink stays a link in the cell.
- Headers and footers: deduplicated, one `Header:`/`Footer:` line at the top; page-number-only ones dropped.
- Images: written to `assets/img-NNN.<ext>` and linked as `![alt](assets/img-NNN.png)`. Never base64. BMP is converted to PNG. WMF, EMF and other formats Claude cannot view are kept as files and listed once in a note.
- Hidden content (hidden text, rows, sheets, slides, sections) and tracked deletions are left out. The first line says how many places were left out; `convert --hidden` includes them, marked `(hidden)`.
- Metadata: title only, and only when the body does not start with it.

### Gate

`hooks/hooks.json` gains one entry: `PreToolUse`, matcher `^Read$`, `python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/read.py" hook`, timeout 60.

`gate.decide(event) -> dict | None`:

1. Not a `Read`, the extension is not in `FORMATS`, or it is listed in `PHRO_READER_OFF` (comma-separated extensions): print nothing, exit 0.
2. Look the file up in the cache (key = SHA-1 of the bytes + `reader.VERSION`). A hit skips conversion.
3. Otherwise convert in a child process with a 45-second budget, so a hang cannot reach the hook timeout (on a hook timeout Claude Code would read the original binary).
4. Success: `permissionDecision: "allow"`, `updatedInput` = the original input with `file_path` set to `out.md` (offset and limit kept), and `additionalContext`, under 600 characters: `<name> was converted to Markdown by phro-project (N lines, M images in assets/; Read them to view). k hidden places left out.` For outputs over 2,000 lines it adds the line numbers of the first-level headings so Claude can read by range. For the OOXML family (docx, docm, dotx, dotm, pptx, pptm, ppsx, potx, potm, ppsm, xlsx, xlsm, xltx, xltm) it adds `Read-only view (no XML, cell addresses or formulas); to edit the file, use the <docx|pptx|xlsx> skill.` For pdf it adds the page-line hint, the setup offer when the stdlib parser was used, the unrendered pages and the page index (pdf spec).
5. Failure: `permissionDecision: "deny"` with a one-line reason: unsupported (phase not built yet, or the reader's detail: `this file is not supported (<detail>)`), encrypted (needs a password), DRM, damaged, too large, or timed out, plus the `convert` command to retry by hand. For the OOXML family the reason also names the skill as the other way to read the file. A pdf that fails as damaged or timed out passes through to Claude's own Read instead; a pdf denial names the pdf skill. An mpp denial carries the MPXJ install offer until the user declines it (phase 4).

The hook must also run correctly when Claude reads the converted file or an asset: those are `.md` and image paths, so rule 1 passes them through.

### Cache

`%LOCALAPPDATA%\phro-project\reader\<sha1[:2]>\<sha1>\` on Windows, `~/.cache/phro-project/reader/...` elsewhere: `out.md`, `assets/`, `meta.json` (source path, size, version, seconds, warnings). Shared by every project and every clone of the plugin. A different version re-converts. For pdf the key also holds the engine id (pypdfium2 and pdfium versions, or `native`). `prune` is manual.

### Safety and errors

- Budgets in `limits.py`: input 512 MiB; zip total uncompressed 1 GiB and ratio 200; CFB limits as in dochan (sectors, entries, depth 128, chain steps); records per stream; recursion depth 64; output 20 MB; cells scanned per xlsx workbook `SHEET_SCAN` (2,000,000, styled empty cells included); SmartArt nodes `SMARTART_NODES` (5,000); pdf pages, page-loop deadline and renders `PDF_PAGES` (5,000), `PDF_DEADLINE` (36 s), `PDF_RENDERS` (100); iWork unpacked bytes per document `IWA_BYTES` (256 MiB) and mobi text `MOBI_TEXT` (64 MiB, at most 32 × the file size); djvu BZZ output per document `BZZ_TOTAL` (64 MiB) and page-loop deadline `DJVU_DEADLINE` (36 s); Access `JET_CATALOG` (100,000 catalog rows), `JET_COLS` (1,024), `JET_CELL_CHARS` (32,768), `JET_LVAL_BYTES` (1 MiB), `JET_QUERY_ROWS` (10,000), `JET_SQL` (65,536 characters) and `JET_PROPS` (1 MiB). The phase specs' Local caps tables hold the rest.
- Errors are isolated per record, part, sheet and slide: a bad record is skipped and counted, never the whole file. Only a container that cannot be opened fails the file.
- Encryption: detected for every format and reported. Default passwords are tried where the format has one (xls `VelvetSweatshop`, ppt default, HWP distribution documents). User passwords and DRM are out of scope.
- Garble check: if more than 5% of output characters are replacement or control characters, the warning goes into `additionalContext`.
- Crypto in `containers/crypto.py`: RC4, AES-128 (ECB) and the MSVC `rand()` stream, pure Python (AES written from FIPS-197). AES CFB-1 serves only user-password HWP, which is out of scope (decision 1), so it is not built. AES-256 and CBC are added for the pdf standard security handler (phase 2d).

## Phases

The gate ships in phase 1. A format whose phase is not built yet is denied with "unsupported". Each phase gets its own plan.

| Phase | Formats | Containers and shared parts |
|---|---|---|
| 1 | hwpx, odt, ods, odp, docm, pptm, ppsx, epub, xps, vsdx | model, renderer, images, cache, gate, `zipx`, `html` |
| 2a | hwp, doc | cfb, crypto (AES-128, MSVC rand), text code pages, officeart, mtef |
| 2b | xls, ppt, xlsb, rtf | RC4, number formats, BIFF charts |
| 2c | docx, pptx, xlsx, xlsm, dotx, dotm, potx, potm, ppsm, xltx, xltm | `ooxml_sheets`, `sheet_drawing` (moved out of xlsb), `omml`, streamed zip parts |
| 2d | pdf | `pdfenv` (now `pyenv`) and `read.py setup` (the pypdfium2 venv), `pdfnative/` (dochan's parser), `pdf_layout`, `pdf_tables` (dochan's table assembly), AES-256 and CBC in `crypto`; specified in `2026-11-14-doc-reader-pdf-design.md` |
| 3 | wpd (with wp, wp5, wp6, wp7, wpt), mobi, azw3, pages, numbers, key | `palmdb`, `snappy`, `pbwire`; specified in `2026-11-15-doc-reader-phase3-design.md` |
| 4 | djvu (text layer only), mdb, accdb, pub, vsd, mpp (opt-in) | `bzz`, `jet_pages`, `vsd_stream`, `pub_contents`, `pub_quill`, `pyenv` (mpp); specified in `2026-11-15-doc-reader-phase4-design.md` |

Phases 3 and 4 were reverse engineered on 2026-11-14 (`research/ai-document-reader-plugins/notes/*-survey.md`, rulings in `rea-decisions.md`). A format that cannot reach the success bar stays "unsupported" with the reason recorded; mpp is one: unsupported by default, read only through the opt-in `read.py setup --for mpp` path when Java and the user's install are present.

## Testing

- Unit tests in `tests/test_reader_*.py`, in the style of the existing tests. Fixtures are built inside the tests from bytes and XML strings (tiny zips, CFBs, PalmDBs); no real documents.
- Tests per format cover the gaps in the findings table: hidden content, merged cells, numbering, footnotes, charts from caches, content-type-agnostic OOXML, `\*` mid-group RTF, xlsb bad-record recovery, the KF8 fallback (KF8 → MOBI 6 → stripped text), tile rows over 256, and the WordPerfect resync after a broken group.
- The gate: allow with a swapped path, deny on failure, pass-through for other files, cache hit, timeout; for the OOXML family, the edit sentence and `PHRO_READER_OFF`.
- xlsx: streamed sheets against `SHEET_SCAN`, with styled empty cells built in memory.
- pdf: a stdlib fixture builder (`tests/pdf_fix.py`) writes tiny pdf bytes; tests that need pypdfium2 run under the venv and skip, saying so, without it.
- Bench: a `phro-read` adapter in the bench harness, run with `python -I -X utf8 harness/bench.py run`. The success bar is checked per format against `reports/written/routing.tsv`, and each phase ends with a short addendum to the bench reports.

## Out of scope

OCR of scanned pages or images (a scanned pdf page goes out as a page image); user passwords and DRM removal; editing or writing documents (pdf editing and creation stay with `anthropic-skills:pdf`); rendering WMF/EMF.
