# doc-reader engine: one stdlib parser for every document format, behind a Read gate

## Goal

phro-project gets a built-in document reader. When Claude is about to `Read` a document that Claude Code cannot read well by itself, a hook turns it into compact Markdown (plus viewable images) and hands Claude that file instead. One engine does the conversion for all 28 formats in scope. It is written in the Python standard library only, like the rest of `scripts/`, and is rebuilt from what reverse engineering the bench's engines taught us, not wrapped around them.

This is stage 4 of the user's request. Stages 1 to 3 (clone, test, report) are done: `C:\Users\hunje\research\ai-document-reader-plugins\bench` and its reports.

Success:

- Every in-scope extension converts through the gate with no other software installed.
- On the bench, for every format with scored documents, the engine's fidelity is at least the current recommended route's minus 0.01, and its calibrated tokens are at most that route's plus 5%. Where it misses, the format's report says why and what it would take.
- A document never reaches Claude as raw binary: the gate either swaps in the Markdown or blocks the Read with a reason.
- Nothing is printed into a session that does not read a document. The plugin's always-on token cost does not change.
- No real document's name or content enters this repository.

## Decisions (user, 2026-11-11)

1. Scope: the 28 extensions of the bench that are not read by Anthropic's built-in skills. pdf, docx, pptx, xlsx and xlsm stay with `anthropic-skills:*`; the gate leaves them alone.
2. One engine for all of them, rebuilt from reverse engineering, not a router over the bench's engines. The bench stays as the yardstick.
3. The gate is a `PreToolUse` hook on `Read` that converts and swaps the path (`updatedInput`). If it cannot convert, it blocks the Read and says why.
4. Permission deny rules may be ignored: the hook does not re-check them against the original path. Personal use.
5. REA (morluto/rea) was installed and run on the engines' binaries. Every engine on the routing table is open source with its source cloned, so the reverse engineering was done on source. Notes: `research\ai-document-reader-plugins\reverse\*.md`.
6. Build order: the gate and the zip+XML formats first, then the rest in phases (see Phases).

## Reverse-engineering findings that shape the design

No engine wins a format on every axis; each wins different items. The engine takes the best item from each:

| Format | Base | Taken from others | Gaps nobody filled |
|---|---|---|---|
| hwpx | dochan (container, `hp:switch`, image map, charts, footnotes, fields, span grid) | kordoc: numbering, alt-text stripping, layout-table unwrapping, final-state track changes | body text in heading styles (demote long sentence paragraphs) |
| hwp | dochan (CFB reader, distribution docs, OLE charts, budgets) | kordoc: numbering, equation→LaTeX, password; rhwp: control-char semantics, per-item BinData compression, ViewText track changes, DRM sniffing | Hancom-native chart streams, grouped-shape text, BMP images |
| odt/ods/odp | anydoc (style chain, span grid, row templates, display text) | docling: hidden sections and slides, comments, notes fields, chart local table; xberg: group recursion, master pages, hidden sheet marker | hidden text/rows, hidden sheets via table style, odp custom shapes |
| docm/pptm/ppsx | anydoc (relationship discovery, style cascade, numbering, `Requires`-aware AlternateContent, grid) | dochan: charts, group geometry; docling: `w:vanish`, hidden slides, outlineLvl first; xberg: SDT placeholder drop | none of the engines gates correctly on content types: accept by root element |
| epub | docling (spine merge, link rewrite) | xberg: DRM skip, fallback chains, nav/TOC removal; anydoc: CSS `display:none` cascade, span grid | `epub:type` footnotes, ruby, `linear="no"` |
| xps | none (only AGPL MuPDF) | written from the XPS/OXPS spec | reading order, columns, tables |
| doc/xls/ppt | dochan (FIB walk, piece table, sprms, BIFF8, persist chain, charts, ppt tables) | anydoc: lid code pages, lists, hidden rows/cols | `sprmCFVanish`, hidden slides |
| xlsb | anydoc (record framing, cells) | | both engines abort on one bad record |
| rtf | anydoc (byte lexer, `\u` with surrogates, edge grid, merges) | xberg: `\v` hidden, `\uc` stack | `\*` mid-group dropping whole sentences |
| wpd | libwpd behaviour (via xberg), reimplemented | | tables, images, footnotes untested |
| mobi/azw3 | format knowledge (PalmDOC, HUFF/CDIC, KF8 skeleton/fragments) | | Calibre failed on 2 of 4 files: fall back KF8 → MOBI6 → stripped text |
| pages/numbers/key | docling (own Snappy, protobuf wire reader, IWA objects) | numbers-parser: field numbers, tile row = tileid × tile size, cell v5 | trailing empties, `None` in merges |

Licenses: dochan, kordoc, rhwp, anydoc, docling, xberg, markitdown and numbers-parser are MIT. Code ported from them is credited in `THIRD_PARTY_NOTICES.md`. MuPDF (AGPL), Calibre (GPL), libwpd/librevenge (MPL/LGPL) and numbers-parser's Apple-derived `.proto` files are used as format knowledge only, never copied.

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
  containers/  zipx.py cfb.py palmdb.py snappy.py pbwire.py iff.py crypto.py
  formats/     hwpx.py hwp.py odf.py ooxml.py vsdx.py epub.py xps.py
               doc.py xls.py ppt.py xlsb.py rtf.py wpd.py mobi.py iwork.py
               djvu.py jet.py pub.py vsd.py mpp.py
```

Each format module exposes one function, `parse(path: Path, ctx: Context) -> Doc`. It reads through a container module and emits model blocks; it never writes Markdown. `markdown.render(doc, assets_dir) -> str` is the only place output format and token rules live, so every format gets the same rules.

`read.py` commands:

| Command | Does |
|---|---|
| `hook` | reads the PreToolUse JSON on stdin, prints the hook JSON |
| `convert <file> [--out DIR] [--hidden]` | converts one file, prints the out.md path |
| `formats` | lists extensions and their module |
| `prune [--days 30]` | deletes cache entries not used for N days |

### Document model

```
Doc(title, meta, blocks, notes, warnings, counts)
Block = Heading(level, inlines) | Para(inlines) | ListItem(level, marker, inlines)
      | Table(rows: list[list[Cell(inlines, rowspan, colspan, header)]], caption)
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
- Spreadsheet cells show the displayed value (`₩1,385.50`, dates as formatted), not raw numbers. Formulas are off.
- Slides: `## Slide N: Title`, speaker notes as `> Notes: …`. Sheets: `## Sheet: name`. Master, date, footer and slide-number placeholders are skipped.
- Footnotes and comments: `[^n]` in place, definitions at the end of the document.
- Headers and footers: deduplicated, one `Header:`/`Footer:` line at the top; page-number-only ones dropped.
- Images: written to `assets/img-NNN.<ext>` and linked as `![alt](assets/img-NNN.png)`. Never base64. BMP is converted to PNG. WMF, EMF and other formats Claude cannot view are kept as files and listed once in a note.
- Hidden content (hidden text, rows, sheets, slides, sections) and tracked deletions are left out. The first line says how many places were left out; `convert --hidden` includes them, marked `(hidden)`.
- Metadata: title only, and only when the body does not start with it.

### Gate

`hooks/hooks.json` gains one entry: `PreToolUse`, matcher `^Read$`, `python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/read.py" hook`, timeout 60.

`gate.decide(event) -> dict | None`:

1. Not a `Read`, or the extension is not one of the 28: print nothing, exit 0. The built-in five pass through.
2. Look the file up in the cache (key = SHA-1 of the bytes + `reader.VERSION`). A hit skips conversion.
3. Otherwise convert in a child process with a 45-second budget, so a hang cannot reach the hook timeout (on a hook timeout Claude Code would read the original binary).
4. Success: `permissionDecision: "allow"`, `updatedInput` = the original input with `file_path` set to `out.md` (offset and limit kept), and `additionalContext`, under 600 characters: `<name> was converted to Markdown by phro-project (N lines, M images in assets/; Read them to view). k hidden places left out.` For outputs over 2,000 lines it adds the line numbers of the first-level headings so Claude can read by range.
5. Failure: `permissionDecision: "deny"` with a one-line reason: unsupported (phase not built yet), encrypted (needs a password), DRM, damaged, too large, or timed out, plus the `convert` command to retry by hand.

The hook must also run correctly when Claude reads the converted file or an asset: those are `.md` and image paths, so rule 1 passes them through.

### Cache

`%LOCALAPPDATA%\phro-project\reader\<sha1[:2]>\<sha1>\` on Windows, `~/.cache/phro-project/reader/...` elsewhere: `out.md`, `assets/`, `meta.json` (source path, size, version, seconds, warnings). Shared by every project and every clone of the plugin. A different version re-converts. `prune` is manual.

### Safety and errors

- Budgets in `limits.py`: input 512 MiB; zip total uncompressed 1 GiB and ratio 200; CFB limits as in dochan (sectors, entries, depth 128, chain steps); records per stream; recursion depth 64; output 20 MB.
- Errors are isolated per record, part, sheet and slide: a bad record is skipped and counted, never the whole file. Only a container that cannot be opened fails the file.
- Encryption: detected for every format and reported. Default passwords are tried where the format has one (xls `VelvetSweatshop`, ppt default, HWP distribution documents). User passwords and DRM are out of scope.
- Garble check: if more than 5% of output characters are replacement or control characters, the warning goes into `additionalContext`.
- Crypto in `containers/crypto.py`: RC4, AES-128 (ECB, CFB-1) and the MSVC `rand()` stream, pure Python (AES ported from dochan, MIT).

## Phases

The gate ships in phase 1. A format whose phase is not built yet is denied with "unsupported". Each phase gets its own plan.

| Phase | Formats | Containers and shared parts |
|---|---|---|
| 1 | hwpx, odt, ods, odp, docm, pptm, ppsx, epub, xps, vsdx | model, renderer, images, cache, gate, `zipx`, `html` |
| 2 | hwp, doc, xls, ppt, xlsb, rtf | `cfb`, `crypto`, `text` code pages |
| 3 | wpd, mobi, azw3, pages, numbers, key | `palmdb`, `snappy`, `pbwire` |
| 4 | djvu (text layer only), mdb, accdb, pub, vsd, mpp | `iff`; each starts with a reverse-engineering note like phase 1-3's |

Phase 4's formats were not reverse engineered yet. Their plans start with that step, and a format that cannot reach the success bar in pure Python stays "unsupported" with the reason recorded.

## Testing

- Unit tests in `tests/test_reader_*.py`, in the style of the existing tests. Fixtures are built inside the tests from bytes and XML strings (tiny zips, CFBs, PalmDBs); no real documents.
- Tests per format cover the gaps in the findings table: hidden content, merged cells, numbering, footnotes, charts from caches, content-type-agnostic OOXML, `\*` mid-group RTF, xlsb bad-record recovery, KF8 fallback, tile rows over 256.
- The gate: allow with a swapped path, deny on failure, pass-through for other files, cache hit, timeout.
- Bench: a `phro-read` adapter in the bench harness, run with `python -I -X utf8 harness/bench.py run`. The success bar is checked per format against `reports/written/routing.tsv`, and each phase ends with a short addendum to the bench reports.

## Out of scope

OCR of scanned pages or images; user passwords and DRM removal; editing or writing documents; the five built-in formats; rendering WMF/EMF.
