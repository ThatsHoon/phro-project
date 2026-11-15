# doc-reader phase 3: the engine reads wpd, mobi, azw3, pages, numbers and key

Approved by the user, 2026-11-15. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec), whose phase table names these six formats and the shared parts `palmdb`, `snappy` and `pbwire`; the engine spec edits listed at the end are made in the same commit. It builds on phase 2d (`2026-11-14-doc-reader-pdf-design.md`): the counts below start from 2d's 41 extensions. Notes behind it, outside this repository: `research/ai-document-reader-plugins/notes/mobi-azw3-survey.md`, `notes/wpd-survey.md`, `notes/iwork-survey.md` (the surveys) and `notes/rea-decisions.md` (the rulings).

## Goal

The gate converts WordPerfect documents (`wpd` and the variant extensions `wp`, `wp5`, `wp6`, `wp7`, `wpt`), Kindle books (`mobi`, `azw3`) and Apple iWork files (`pages`, `numbers`, `key`), both the IWA format (2013 and later) and the iWork '09 XML format. Five of the bench's nine iWork files are '09 XML, so both are needed.

Every reader is standard library only. No library adds capability here: the formats are compression, protobuf wire bytes, HTML and byte codes, and the surveys found no permissive library that reads more than one of them.

Success, as in the engine spec, with these baselines from `reports/written/routing.tsv`:

| Format | Route (pick) | Note |
|---|---|---|
| wpd | `xberg` | one scored file; bar 0.99 fidelity and 79.9 calibrated tokens (76.1 × 1.05) |
| mobi, azw3 | `calibre>epub>docling` | the route opens 1 of the 4 Tika files and gives no images |
| pages | `lo>docx>dochan` | LibreOffice cannot open one of the three files |
| numbers | `numbers-parser` | |
| key | `docling` | |

- Fidelity is at least the route's minus 0.01, and calibrated tokens are at most the route's plus 5%.
- Linked images count as viewed, as the bench counts them. Text tokens are listed beside. A token MISS that comes from images the route does not give is recorded with that reason and does not block the phase, as for epub in phase 1.

## Decisions (2026-11-14)

Decisions 1 and 2 are the user's answers (`rea-decisions.md`, "사용자 결정"). The rest are rulings from the surveys (`rea-decisions.md`).

**User decisions**

1. The downloads are allowed: the two Gutenberg books (KF8 and old Kindle) join the corpus, and the Corel SDK WordPerfect sample is read only, never indexed. Both landed in bench commit f455ede.
2. MPL-2.0 ports are allowed. This phase uses none: decision 12 keeps libwpd as format knowledge.

**Rulings shared by the three formats**

3. MIT, BSD and Apache-2.0 sources are ported and credited in `THIRD_PARTY_NOTICES.md`, and their names join the notices test. GPL and AGPL code is read for understanding only. Each port source is pinned to its release tag, else to its main commit at survey time.
4. Extension variants read by the same reader are added: `wp`, `wp5`, `wp6`, `wp7`, `wpt`. A compound file with a `PerfectOffice_MAIN` stream goes to wpd through `cfb_route`, and a `.doc` that starts with `FF 'WPC'` goes to wpd.
5. A value outside the format's specification is `unsupported`. Only broken structure is `damaged`.
6. Deleted text, hidden rows and columns and skipped slides are left out by default and counted; `--hidden` shows them.
7. Footnotes and comments take the doc, docx and rtf form: `[^n]`, and "Comment: …" or "Comment by Author: …". A mobi footnote is linked only by a heuristic (a `<sup>` link to a target near the end); when unsure it stays text at the end.
8. Pictures Claude cannot view (WPG, WMF, EMF) are kept as files and listed as not viewable. No WPG decoder is built.
9. Tables use the xlsx caps `SHEET_ROWS` and `SHEET_CELLS`.
10. Number display covers decimal, currency, percent, date, duration and bool. Any other format shows the raw value.
11. Engine test files already on disk under MIT or Apache-2.0 join the corpus; the bias toward their own engine is noted in the report. A format whose token bar misses only because of linked images keeps its images, and the text tokens are recorded with it.

**mobi and azw3**

12. Port sources: Apache Tika `parser/mobi` (Apache-2.0) for PalmDB, PalmDOC, HUFF/CDIC and its caps, and foliate-js `mobi.js` (MIT) for KF8 indexes, skeleton and fragment assembly and link resolution.
13. A DRM book is denied as `drm`. No metadata-only output.
14. Headings are recovered from the NCX.
15. A combo file is read KF8 first, then MOBI 6, then as stripped text.
16. The four Tika test files not yet in the corpus (encrypted, HUFF/CDIC, broken HUFF/CDIC, stored) join it.

**wpd**

17. libwpd is format knowledge only. A new text-first reader is written. Character sets come from Tika (Apache-2.0); sets Tika lacks are filled from WordPerfect's published tables.
18. LibreOffice is used only for files the native reader refuses (Mac WordPerfect), and only when an environment variable turns it on.
19. Versions read: 5.x, 6 and later, and 4.2. Mac WordPerfect 1 to 3.5 is `unsupported`.
20. Headings are not recovered from style names.
21. Hidden text: the Corel SDK sample was read (decision 1). What it settles is under "Hidden text in WordPerfect". The risk that remains goes into the README's limits.
22. Korean and CJK WordPerfect text gets the character-set mapping only.
23. The Tika WordPerfect 4.2 sample on disk joins the corpus.

**iWork**

24. One IWA reader reads all three apps, on the shared parts `snappy` and `pbwire`. The iWork '09 XML reader is in this phase.
25. macOS bundle folders are not taken by the gate. Read keeps failing on a folder, and the README says so.
26. LZVN (`bvxn`) members are counted and skipped. No decoder yet.
27. An equation is counted as `object`.
28. `character_index` is read in code points. Text outside the BMP gives a warning.
29. `preview.jpg` is output, as one image with the LOST line, only when the body could not be read.
30. python-pages is read on the web (not downloaded) to check the Pages field numbers; this is a plan task.
31. Samples with hidden rows, a skipped slide and an emoji paragraph come from the user if they make them. Without them, the addendum says those three are untested on real files.
32. The docling and numbers-parser test files on disk join the corpus.

## Hidden text in WordPerfect

A structure-only probe of the Corel SDK sample (no text quoted or kept) found:

- The sample is the WordPerfect 5.x format reference: its version fields describe 5.0 and 5.1.
- Its attribute tables run 0 to 15 (extra large to small caps). No 5.x function stores hidden text, so 5.x has none to leak.
- It documents the 5.x comment, function 0xD9 subfunction 2, whose text starts at group offset 7 after a u16 screen column and a u8 line count. libwpd does not read it; this reader does.
- Function 0xD9 subfunction 7 ("hide function") holds conversion data, not text. It is skipped.
- It says nothing about 6 and later. libwpd and Tika have no hidden-text code there either, and the 6+ attribute table they know ends at 16 (blink).

So hidden text in 6+ files is not detected and can reach the body. The README states this, and an attribute number above 16 is counted (Defaults).

## Defaults (proposed with this spec, approved with it)

`rea-decisions.md` has no ruling on these; this spec decides them.

- **mobi footnote heuristic.** An internal link is a note reference when all hold:
  - its visible text is 1 to 4 characters, and it is inside a `<sup>` or holds one, or it has `epub:type`/`role` `noteref` or `doc-noteref`;
  - its target lies in the last chapter, or in the last fifth of the book's text.
  The note is the nearest block element (`p`, `div`, `li`, `aside`, `blockquote`) holding the target, never a whole chapter. It is marked `role="doc-footnote"` in the tree, so `html.blocks` collects it as for epub.
- **Link targets by inserted anchors.** `filepos` and `kindle:pos` targets are both resolved by inserting `<a id="…"></a>` at the target byte before parsing; a target inside a tag moves before that tag's `<`. foliate-js looks up `id`/`aid` instead; one mechanism serves both kinds here.
- **NCX headings by marker.** At each NCX position the reader inserts `\x1e` plus the depth digit (0 to 5); inside a tag it moves after the next `>`. After `html.blocks`, a `Para` that starts with a marker becomes `Heading(depth + 1)`. Every marker is then stripped, also inside headings and cells. A book whose text already holds `\x1e` skips the recovery.
- **mobi images.** Only images the body links are output; a cover outside the body is not. In a combo file the KF8 first-image index is tried as absolute, then as relative to the KF8 boundary, keeping the one that sniffs as an image.
- **mobi containers.** KFX (`CONT` head) and Topaz (`TPZ` head) are `unsupported`. A PalmDOC file (`TEXtREAd`) is `unsupported`. A MOBI text encoding other than 1252 or 65001 is `unsupported`.
- **Chapters.** MOBI 6 text is split into chapters at `<mbp:pagebreak`. A KF8 book with no skeleton index is split at each `<html` in flow 0.
- **wpd deleted text.** An undo span (F1 type 0 to 1) is `hidden="deleted"`. An undo span with no end runs to the end of its stream.
- **wpd details.**
  - Comments become footnotes through `word_fields.comment_note`, with no author.
  - Endnotes are footnotes.
  - Watermark text is a header line.
  - Text boxes are paragraphs in place.
  - Equation boxes are `object`.
  - Box hyperlinks (packet 0x07) are counted `link`.
  - Paragraph numbers stay inline text.
  - Character set 12 and any unmapped character are counted `glyphs`.
  - An attribute on or off above 16 is counted under the internal kind `attribute`, and the first one adds a warning to `meta.json`.
- **LibreOffice switch.** `PHRO_READER_SOFFICE=1` looks for `soffice` on PATH and in the standard install folders; any other value is the path of `soffice`. It applies to Mac WordPerfect (file type 0x2C) and to a headerless file the 4.2 check refuses. Without it, the denial names the switch.
- **iWork merges.** An object's payload is its archive's first message. When `should_merge` is set, the later messages' bytes are appended to it, which gives protobuf's merge (a single field takes the last value; a repeated one grows). `fields_to_remove` is ignored and counted under the internal kind `merge`. Other messages after the first are ignored. A duplicate object id keeps the first, counted under the internal kind `duplicate`.
- **iWork chunks.** A chunk of type 1 is read as stored bytes, as xberg does. Any other type fails its member (`document`).
- **iWork '09 passwords.** An `index.xml` or `index.apxl` stored with a zip method other than 0 or 8 is `encrypted`: the Tika protected Pages file uses methods 25451 and 25452.
- **Pages styles.** "Title" and "Heading" (bare or "Heading 1") are level 1, "Subtitle" and "Subheading" level 2, and "Heading N" level N up to 6, as in docling. Floating drawables follow the body, in their container's order.
- **Numbers.**
  - Any nonzero `hidingState` hides its row or column.
  - A formula error cell shows `#ERROR`.
  - Formula text is never shown: every IWA formula cell has its value.
  - A sheet's sticky notes go into `Section.comments`.
  - '09 hidden rows are not read.
  - '09 charts (`sf:chart-info`) are counted `chart`, not read (amended 2026-11-15, Task 16).
- **Picture kinds.** `images.sniff` learns `wpg` (`FF 'WPC'` with file type 0x16), `heic` (`ftypheic`, `ftypheix`, `ftypmif1`) and `pdf` (`%PDF-`). All three are kept as files and listed as not viewable.
- **App by extension.** The iWork app is taken from the extension. When its root is missing, the other two apps' roots are tried, so a renamed file still reads.
- **obriensp's iWorkFileFormat** (MIT, documentation) is format knowledge. No code is ported, so it gets no notices section.

## Design

### Modules

```
scripts/reader/
  containers/palmdb.py     PalmDB records, trailing entries, PalmDOC LZ77, HUFF/CDIC (Tika port)
  containers/snappy.py     raw Snappy blocks and the IWA chunk stream (docling port)
  containers/pbwire.py     protobuf wire reader (docling port)
  formats/mobi.py          MOBI 6, KF8, combo, links, NCX headings, fallbacks
  formats/wpd.py           WordPerfect 4.2, 5.x and 6+: header, prefix packets, body walk, LibreOffice switch
  formats/wpd_charsets.py  WordPerfect character sets 0-14 (Tika tables)
  formats/iwork.py         container shapes, encryption, dispatch, preview fallback
  formats/iwork_iwa.py     object store, merges, data references, Pages, Numbers and Keynote walks
  formats/iwork_text.py    TSWP storage to blocks
  formats/iwork_tables.py  TST tables, cells v4 and v5, number formats, charts
  formats/iwork_xml.py     iWork '09 XML (Pages, Numbers, Keynote)
```

Shared code they reuse, unchanged unless the edit is named:

- `html.tree`, `html.blocks`, `html.decode`, `css.Css` (mobi).
- `epub._referenced` and `epub._settle` move to `html.py` as `referenced` and `settle`; epub imports them. Behaviour does not change.
- `zipx.Package` (iWork; `Index.zip` opens as `Package(bytes)`), `zipx.parse_xml_ns` and `zipx.iterparse` ('09 XML). `Package.method(name) -> int` is added: the member's compression method, read without inflating it.
- `flate.gunzip(data, budget, limit)` is added: `_run` with wbits 31, for `index.xml.gz` and `index.apxl.gz`.
- `cfb.Compound` and `cfb_route.route` (wpd's OLE wrapper).
- `sheet_grid.Grid` and `sheet_grid.Budget` (every TST table), `charts.series_table` (iWork charts), `numfmt.display` (iWork number display).
- `word_fields.comment_note` (wpd and iWork comments).
- `images.Assets` and `images.sniff`, extended as in Defaults.

### containers/palmdb.py

- `Palm(data)`: the 78-byte head, then the record table: count u16, offsets u32. Offsets must not decrease and must lie inside the file; else `damaged`. `type_creator` gives the eight bytes at 60. `record(i)` returns that record's bytes.
- `trailing(rec, flags)`: strips the trailing entries before unpacking. For each set bit 15 to 1, a backward varint size (itself included); then, with bit 0, `(last byte & 3) + 1` bytes. A varint is at most 4 bytes and no larger than the record.
- `palmdoc(rec, limit)`: LZ77. 0x00 and 0x09-0x7F literal; 0x01-0x08 copy the next n bytes; 0x80-0xBF a two-byte copy, distance `(v >> 3) & 0x7FF`, length `(v & 7) + 3`, overlapping forward; 0xC0-0xFF a space plus `b ^ 0x80`. A distance of 0 or past the output fails the record.
- `HuffCdic(huff, cdics)` and `.unpack(rec, limit)`: the HUFF table (256 × u32: code length in the low 5 bits, 0x80 terminal, max code above) and the 32 (min, max) pairs; CDIC phrases with u16 offsets, bit 0x8000 marking an unpacked phrase. Unpacked phrases are cached; a phrase that refers to itself fails.

### containers/snappy.py

- `raw(block, limit)`: a varint length (at most `limit`), then literal, copy-1, copy-2 and copy-4 tags. An overlapping copy repeats forward with `(out[s:] * k)[:n]`. Output that does not equal the declared length, or a copy before the start, raises `ValueError`.
- `iwa(data, budget)`: the chunk stream: one type byte, three length bytes (little-endian), then the block. Type 0 is Snappy and type 1 stored. Data that starts with `bvx` raises `Lzvn`, a `ValueError` the caller counts. `budget` is a `flate.Budget` of `IWA_BYTES`, shared by every member.

### containers/pbwire.py

- `varint(buf, pos) -> (value, pos)`: at most 10 bytes.
- `fields(buf) -> dict[int, list]`: wire 0 as int, 1 and 5 as raw bytes, 2 as a memoryview. Wire 3 and 4, field 0 and a length past the buffer raise `ValueError`.
- `packed(b)`, `f32(b)`, `f64(b)`, `ref(b)` (field 1 of a `TSP.Reference`) and `one(f, n, default)`.
- Parsing is lazy: a caller parses a nested message only when it walks into it, so the caller owns the depth.

### formats/mobi.py

1. **Head.** `Palm`; type and creator must be `BOOKMOBI`. Record 0 holds the PalmDOC head (compression 1, 2 or 17480; text length; record count; record size; encryption) and the MOBI head (length, encoding, version, first image, HUFF record and count, EXTH flag, FDST/SKEL/FRAG/NCX indexes, extra-data flags).
2. **Refusals.** Encryption 1 or 2, or a DRM offset that is not 0xFFFFFFFF with a count over 0, is `drm`. Another compression (3 in a Tika file) is `unsupported`.
3. **EXTH.** 100 author, 503 title, 121 KF8 boundary, 201 cover, 129 KF8 cover URI. Only the title is used; it is the record-0 full title when 503 is absent.
4. **Text.** Text records 1 to n, each trimmed by `trailing` and unpacked to its cap, joined.
5. **KF8** (version 8, or EXTH 121 in a combo; KF8 indexes are relative to the boundary).
   - FDST gives the flows. Flow 0 is XHTML; `kindle:flow:000N?mime=text/css` links read flow N as a stylesheet.
   - INDX, TAGX and CNCX read SKEL (tag 1 fragment count, tag 6 start and length; a doubled value is read once) and FRAG (label = insert position, tag 3 file number, tag 6 length). Each skeleton is cut from flow 0, and its fragments are inserted in turn at `insert position − skeleton start`.
   - With SKEL and FRAG at 0xFFFFFFFF, flow 0 is the XHTML (Defaults: chapters at `<html`).
   - Any structural failure raises an internal fallback: a combo goes to MOBI 6, and a KF8-only book to stripped text.
6. **MOBI 6.** Chapters at `<mbp:pagebreak`. `filepos` anchors are inserted (Defaults), and `<img recindex="N">` becomes `src="recindex:N"` in the tree, so `html.py` reads it unchanged.
7. **Stripped text.** Tags are removed, entities decoded with the stdlib `html.unescape`, and paragraphs split at block tags. The named item `plain_text` is set.
8. **Blocks.** Each part goes through `html.tree` and `html.blocks` with a `Css` of its stylesheets (at most 32, as epub) and a `load` that resolves `recindex:N` and `kindle:embed:XXXX` (base 32, 0-9A-V) to record `first image + n − 1`. A record that does not sniff as an image gives None, counted `image`. Notes are settled with `html.referenced` and `html.settle`. Each part is `Section("chapter", number=n)` under `ctx.guard("chapter")`.
9. **Headings** from the NCX (tag 1 position, tag 4 depth) by marker (Defaults).

### formats/wpd.py

**Head.**
- A compound file reads its `PerfectOffice_MAIN` stream. A compound file without it goes to `cfb_route.route(cf, "wpd", "no PerfectOffice_MAIN stream")`.
- `FF 'WPC'`: u32 document pointer, product type, file type, major and minor version, u16 encryption word.
  - Encryption word not 0: `encrypted`.
  - File type 0x16 (WPG): `unsupported`. File type 0x2C (Mac): the LibreOffice switch, else `unsupported`.
  - Major 0: WP5. Major 2: WP6 and later. Any other version, or product type not 1: `unsupported`.
- No head: the 4.2 check walks the first `WP42_PROBE` bytes. Every byte 0xC0-0xFE must close by its size table or by its closing byte, and at least one group must occur. `FE FF 61 61` is a 4.2 password: `encrypted`. A refused file goes to the LibreOffice switch, else `unsupported`.
- Offset 20 (the 6.1 document size) is never trusted.

**WP6+.**
- The prefix index: entry count at `ih + 2`, 14-byte entries from `ih + 14` (flags, type, use count, hide count, size, offset). Packets read:
  - 0x08: sub-document text.
  - 0x1B: comment to text packet.
  - 0x40 and 0x6F: graphics bytes.
  - 0x41: box content kind.
  - 0x12: summary; field 17 is the title.
- The body runs from the document pointer to the end of the file.
  - Text and code bytes: ASCII runs are taken whole with `re.finditer(rb"[\x21-\x7f]+")`. 0x01-0x20 are the extended international characters. 0x80 is a space, 0x81 a no-break space, 0x84 a hyphen.
  - Breaks: hard EOLs (0xCC; D0 subfunctions 4-6 and 0x17-0x19) end a paragraph. Soft EOLs are spaces. Page and column breaks end a paragraph.
  - Variable groups 0xD0-0xEF and fixed groups 0xF0-0xFE are checked by size and closing byte. A failed check consumes one byte (libwpd's resync); one `record` is counted per run of failures.
- Content:
  - Bold: attribute 12 on F2, off F3.
  - Extended characters: F0 `[char][set]` through `wpd_charsets`.
  - Tables: D4 0x2A/0x2B/0x2C, cells from D0 0x0A-0x13 and bytes 0xBD-0xC6, spans from EOL subfunction 0x85 (a column span of 128 or more joins the cell above), header rows from 0x80 bit 0x04.
  - Footnotes and endnotes: D7 → 0x08. Comments: D4 0x1D → 0x1B → 0x08. Headers, footers and watermarks: D6 → 0x08.
  - Boxes: DF with box content 1 (text) or 3 (graphics → 0x40 → 0x6F).
  - Undo: F1 type 0 to 1 (Defaults).

**WP5.**
- The prefix runs from offset 16: 0xFFFB blocks of five 10-byte indexes, chained by a u32 next.
  - Packet 8 holds the graphics: a count, u32 sizes, then the bytes. A picture with no WPG head gets the 16-byte head prepended, so it sniffs as WPG.
- The body:
  - Single bytes: 0x0A hard return; 0x0B, 0x0D and 0x93-0x95 spaces; 0x0C a page break; 0xA0 a no-break space.
  - Fixed groups 0xC0-0xCF use the size table. Variable groups 0xD0 and up are `[id][sub][u16 len]…[u16 len][sub][id]`.
- Groups:
  - C0 is an extended character. C3/C4 attribute 12 is bold.
  - D5 header or footer: text after 7 bytes, the occurrence byte and 10 more bytes. D6 footnote or endnote: text after the skip by kind (a footnote `2 × (n + 1) + 9` bytes past its page count, an endnote 4).
  - D9 sub 2 comment: text at +7 (the Corel SDK). D9 sub 7 is skipped.
  - DA figure: picture n of packet 8. DC and DD are table cells, rows and end.

**4.2.** Text, tab, hard and soft new lines, and footnotes (0xE2), per the 4.2 size table; any other group is skipped by its size.

**Stories and limits.** Sub-documents are walked with a set of visited packet ids and `MAX_DEPTH`; a cycle or a deeper nest is counted `depth`. The walk keeps one paragraph list per story (body, note, header), the way `rtf.py` does.

**LibreOffice switch.**
- `soffice --headless --norestore -env:UserInstallation=file:///<tmp>/profile --convert-to docx --outdir <tmp> <file>` runs with `subprocess.run(timeout=CONVERT_SECONDS − elapsed − 3)`.
- The result is read by `ooxml.parse`, and the temporary folder is removed.
- A timeout kills the process tree and raises `timeout`. No docx in the folder is `damaged`.

### formats/iwork.py and its parts

**Container.**
- The outer file opens with `zipx.Package`.
- A member named `.iwpv2` or `.iwph`, at any depth, is `encrypted`. This is checked first, since an encrypted `.iwa` may start with 0x00 by chance.
- '09: a root `index.xml`, `index.xml.gz`, `index.apxl` or `index.apxl.gz` goes to `iwork_xml`. A method other than 0 or 8 is `encrypted`, and `.gz` inflates through `flate.gunzip` to `IWORK_XML`.
- IWA: root `Index/*.iwa`, or an `Index.zip` at the root or one folder down, which opens as `Package(bytes)`. An `Index.zip` inside it is refused (`document`).
- Nothing readable: `preview.jpg`, else `QuickLook/Thumbnail.jpg`, becomes the only image, counted `document`. With neither, `damaged`.

**Object store (`iwork_iwa`).**
- Every `.iwa` member is unpacked by `snappy.iwa` under one budget. An LZVN or failed member is counted `document` and skipped.
- The stream is read as `[varint n][ArchiveInfo][payloads…]` repeated, from the joined chunks, since an archive may cross a chunk boundary.
- ArchiveInfo gives 1 identifier, 2 message infos (1 type, 3 length, 6 data references) and 3 `should_merge` (Defaults).
- All members fill one map `id → (type, payload)` before any walk, because references cross members.
- Data: `Metadata.iwa` `TSP.PackageMetadata` (11006) field 4 → `TSP.DataInfo` {1 id, 3 preferred file name, 4 file name} → member `Data/<file name>`. A missing member is counted `image`.

**Walks.**
- Each app has its own type table, because the same numbers mean different things in each app.
- Pages: `TP.DocumentArchive` (10000) field 4 is the body storage, and field 20 holds the floating drawables. Section 17 → `TP.PageMasterArchive` (10011) → `HeadersAndFooters` (10143) gives `Doc.headers` and `Doc.footers`.
- Numbers: `TN.DocumentArchive` (1) field 1 lists the sheets. `TN.SheetArchive` (2) gives 1 name and 2 drawables (6000 tables, 3005 images, 5021 charts, 2011 text boxes). Each sheet is `Section("sheet", name)` under `ctx.guard("sheet")`.
- Keynote: `KN.DocumentArchive` (1) → show (2) field 3 → slide tree → `SlideNode` (4) {2 slide, 4 isSkipped} → `Slide` (5).
  - The title placeholder (5) gives the section title; the body (6) and then the owned drawables (7) follow in `drawables_z_order` (42).
  - The note (27) → 15 → storage gives `Section.notes`, and comments (2014 → 3056) give `Section.comments`.
  - An isSkipped slide is `hidden="hidden"`. Templates and masters are never walked.
- Every reference walk keeps a visited set and stops at `MAX_DEPTH`.

**Text (`iwork_text`).** `TSWP.StorageArchive` (2001):
- Text: field 3 strings are joined and split at `\n`. U+FFFC places the attachment from field 9 (2003 → drawable) after its paragraph.
- Styles: field 5 paragraph styles (2022; name through `super` and `parent`, `STYLE_CHAIN` steps) give headings by name (Defaults). Field 6 gives list depth and field 7 (2023) the list label type, for `ListItem` with `-` or `1.`. Field 8 character styles give bold (property 1) through the same chain.
- Links and notes: field 11 `HyperlinkField` (2032) url is a `Link` for http, https and mailto, else text. Field 16 footnotes (2008 → storage) give `NoteRef` and note blocks. Field 23 highlights (2013 → 3056 {1 text, 3 author → 212 name, 4 replies}) give a comment note.
- Changes: field 22 deletions (2060 kind 2) are `Hidden` with kind `deleted`. Insertions are plain text.
- Every run table is `{1 entries {1 character_index, 2 ref}}`, and an entry with no ref ends the run. Indexes are code points (decision 28).
- An equation (2015) is counted `object`.

**Tables (`iwork_tables`).**
- Path: `TST.TableInfoArchive` (6000, or 6007 in Pages) → model (6001: 6 rows, 7 columns, 8 name, 9-11 header rows and columns) → data store (6002 tiles; 4 strings, 17 rich text and 19 comments as `TableDataList` 6005; 13 merges 6144; 1 and 2 header buckets 6006 for hiding).
- Tile rows: a tile entry {1 tileid, 2 ref}, and tile size from field 2 (256 when absent). The table row is `tileid × tile size + tile_row_index`; docling's "first row" reading is wrong past 256 rows.
- Cells: a row's cell buffer is field 6 with offsets in field 7 (int16; −1 empty; ×4 when field 8 is set). A v5 cell (`version 5`, type, u32 flags at 8) is read in numbers-parser's field order: decimal128, double, seconds, string id, rich id, styles, formula, control, formula error, suggest, then the five format ids and the comment id. When only the v4 buffer (fields 3 and 4) exists, it is read as docling's `cells.py` reads it, with its checks.
- Cell display:

| Type | Shown as |
|---|---|
| 2 number, 10 currency | the value through its format |
| 3 text | the string table entry |
| 9 rich text | the storage's inlines, links kept |
| 5 date | seconds from 2001-01-01, as the serial `36892 + s / 86400` through its format |
| 6 bool | TRUE or FALSE |
| 7 duration | seconds, as `[h]:mm:ss` |
| 8 formula error | `#ERROR` |

- Formats: `TSK.FormatStructArchive` {1 type, 2 decimals, 3 currency code, 5 thousands, 14 ICU date pattern} maps to an Excel code for `numfmt.display`:
  - 256 decimal: `0` with the decimals, `#,##0` with thousands; 253 decimals means General.
  - 257 currency: the symbol for USD, EUR, GBP, JPY and KRW, else the code and a space.
  - 258 percent.
  - 261 date: the ICU tokens `yyyy yy MMMM MMM MM M dd d EEEE EEE HH H hh h mm ss a`. A pattern with any other token shows ISO `YYYY-MM-DD`, plus the time when it is not midnight.
  - 1 bool and 263 checkbox: TRUE or FALSE.
  - Any other type shows the raw value (decision 10).
- Grid: cells go to `sheet_grid.Grid` with merges and hidden rows and columns, so a table renders as xlsx does. A cell comment is a footnote on its cell.
- A table that fails is skipped and counted `table`.

**Charts.** `TSCH.ChartDrawableArchive` (5021) extension 10000 → `ChartArchive` {1 type, 7 grid {1 row names, 2 column names, 3 rows of values}, 10 → 5023 title}. It goes to `charts.series_table` with the caption `Chart: <type> — <title>`.

**'09 XML (`iwork_xml`).**
- The `sf` vocabulary is shared: `sf:p`, `sf:span`, `sf:paragraphstyle` and `sf:characterstyle` (`sf:parent-ident`, `STYLE_CHAIN` steps), `sf:liststyle`, `sf:link`, `sf:footnote`, `sf:media` and `sf:image` (`sf:data path`).
- `sf:ghost-text` and `sf:ghost-text-ref` are template placeholders and are dropped uncounted.
- Pages `sl:document`: the body is everything outside `sf:header`, `sf:footer`, `sf:footnotes` and `sf:annotations`. `sf:annotation` comments become comment notes.
- Keynote `key:presentation`: `key:slide-list/key:slide`. The title and body placeholders come first, then the drawables on `key:page`, then `key:notes`.
- Numbers `ls:document`: each `ls:workspace` is a sheet named by `ls:workspace-name`. A `sf:tabular-model` `sf:datasource` gives the cells:
  - `sf:t`, `sf:n` (`sf:v`), `sf:d` (`sf:cell-date`), `sf:b`, `sf:cb` and `sf:pm`;
  - a formula `sf:f` shows its result `sf:r` (`sf:rn`, `sf:rt`, `sf:rd`, `sf:rb`);
  - `sf:s` is a spanned cell, with spans from `sf:col-span`.
- XML goes through `zipx.parse_xml_ns`, so a DOCTYPE or entity is refused.

### Output

| Content | out.md |
|---|---|
| mobi chapter | `Section("chapter")` blocks as epub's; headings from tags and the NCX |
| mobi footnote | `[^n]` when the heuristic holds, else the note's text where it stands |
| mobi image | `![](assets/img-NNN.jpg)` where the body links it |
| wpd paragraph, bold, table | paragraphs, `**bold**`, pipe or HTML tables by the renderer's rules |
| wpd note, comment | `[^n]`; a comment reads `Comment: …` |
| wpd header, footer, watermark | one `Header:`/`Footer:` line at the top, deduplicated |
| wpd picture | the WPG file in assets/, listed under "Not viewable here" |
| Pages | body blocks, headings by style name, lists, links, `[^n]` for footnotes and comments, headers and footers |
| Numbers | `## Sheet: name`, then each table as a pipe or HTML table (caption = table name), charts, images, text boxes; sticky notes as `> Comment: …` |
| Keynote | `## Slide N: Title`, the body, `> Notes: …`, `> Comment by Author: …` |
| iWork image | `![](assets/img-NNN.png)`; heic, tiff and pdf data listed as not viewable |
| Hidden | wpd undo, Pages (IWA) deletions, Numbers hidden rows and columns, KF8 `display:none` and `hidden`, skipped slides: left out and counted; `--hidden` marks them `(hidden)` |

### Limits

Local caps in each module, unless `limits.py` is named:

| Cap | Value | Past it |
|---|---|---|
| `MOBI_RECORD` (palmdb) | the record-size field (0 means 4,096) + 8 bytes unpacked per text record | the record is cut, counted `record` |
| `MOBI_TEXT` (palmdb) | 64 MiB, and at most 32 × the file size | the text is cut, counted `truncated` |
| HUFF/CDIC (palmdb, Tika's) | recursion 32; dictionary 16 MiB (64 bytes charged per phrase); one phrase 64 KiB; code lengths 1-32; index bits at most 16 | the record fails, counted `record`; a bad table fails the book (`damaged`) |
| `INDX_ENTRIES` | 100,000 entries an index; TAGX at most 64 tags and 8 control bytes; varints at most 5 bytes; index records no more than the records left | the KF8 fallback |
| `FDST_FLOWS` | 1,000 flows, each inside the text with start ≤ end | the KF8 fallback |
| `MOBI_ANCHORS` | 100,000 inserted link anchors | the rest keep their link text |
| `NCX_ITEMS` | 10,000 entries; labels cut at 300 characters | the rest unused |
| HTML (`html.py`) | `MAX_DOC` 16 MiB a part, `MAX_NODES` 500,000, depth `MAX_DEPTH` | as for epub |
| `WP42_PROBE` | 65,536 bytes scanned to recognize 4.2 | |
| `WP5_BLOCKS` | 1,024 index blocks, and a visited-offset set | counted `record` |
| `WPD_SUBDOCS` | 10,000 sub-documents read per document | the rest counted `note` |
| `MAX_TITLE` | 500 characters of the wpd title | cut |
| `SNAPPY_BLOCK` | 16 MiB declared per block | the member fails (`document`) |
| `IWA_BYTES` | 256 MiB unpacked per document, all members (`flate.Budget`) | `too_large` (`total`) |
| `IWA_OBJECTS` | 1,000,000 objects | the rest unread, counted `record` |
| `STYLE_CHAIN` | 8 style parents (docling) | the chain stops |
| `TABLE_SEGMENTS` | 100,000 segments of one value table, with a visited set | the rest unread, counted `cells` |
| tiles | tile size 1 to 65,536; rows to `SHEET_ROWS`, columns to `SHEET_COLS`; cells `SHEET_CELLS`, characters `SHEET_CHARS`, unpacked cells `SHEET_SCAN` (`limits.py`) | the sheet is cut, counted `sheet_cut` |
| `IWORK_XML` | 100 MiB inflated '09 index (docling) | `too_large` |
| charts | `CHART_CELLS`, `CHART_TOTAL` (`limits.py`) | counted `chart_cut` |
| `IMAGE_BYTES` (`limits.py`) | 256 MiB a document; mobi, wpd and iWork join its comment | counted `image` |

The plan's measure task runs `snappy.iwa` over 256 MiB and the wpd body walk over 50 MB on this machine. `IWA_BYTES` is lowered if it takes over 20 seconds; the survey measured about 22 MB/s, which is 12 seconds.

### Hostile input

- **Length fields.** Every PalmDB offset, INDX position, FDST range, WordPerfect group size and packet range, protobuf length and cell offset is checked against its buffer before use.
- **Bombs.** Snappy declares at most 21 times its input. The declared length is checked against `SNAPPY_BLOCK` and the budget before any output, and output past the declaration fails. PalmDOC and HUFF output stop at the record cap; HUFF recursion and the dictionary are capped. '09 gzip stops at `IWORK_XML`. An `Index.zip` keeps `zipx`'s own ratio and total checks.
- **Cycles.** CDIC self-reference, WordPerfect note → packet → note, WP5 index chains, iWork references, style parents and value-table segments are cut by visited sets and depth caps.
- **Huge tables.** A false tileid or span is stopped by the row, column and slot caps. Rows are never allocated from a declared count.
- **Planted text.** KF8 `display:none` and `hidden`, wpd undo and Pages (IWA) deletions are hidden by default. Pages '09 tracked changes are not read, so their deleted text shows as body text (amended 2026-11-15, Task 15: no public source names the '09 change elements). WP6+ hidden text is not detected (Hidden text in WordPerfect). Heading text in the context is cleaned by `gate._heading`, as now.
- **External programs.** Nothing is run, except `soffice` under the user's own `PHRO_READER_SOFFICE`.

### Errors and LOST counts

| Kind | When |
|---|---|
| `drm` | mobi encryption 1 or 2, or a valid DRM offset with a count |
| `encrypted` | a WordPerfect encryption word, a 4.2 password head, an iWork `.iwpv2` or `.iwph`, a '09 index with a non-standard zip method |
| `unsupported` | mobi compression or encoding outside the specification, KFX, Topaz, PalmDOC; WPG files, Mac WordPerfect without the switch, an unknown WordPerfect version or product |
| `damaged` | a PalmDB table out of order, no readable chapter, no WordPerfect body, an iWork file with no readable part and no preview |
| `too_large` | `IWA_BYTES`, `IWORK_XML`, the zip checks |
| `timeout` | the LibreOffice child |

New `LOST` entries:
- `table`: "table" / "tables" (an iWork table that failed).

New `NAMED` items:
- `kf8_fallback`, "KF8 layout (the MOBI 6 copy was read)".
- `plain_text`, "book markup (read as plain text)".

New `INTERNAL` kinds: `attribute`, `merge`, `duplicate`.

Kinds that already exist, used as they are: `chapter`, `sheet`, `slide`, `document`, `image`, `chart`, `chart_cut`, `object`, `note`, `link`, `glyphs`, `record`, `depth`, `cells`, `sheet_cut`, `truncated`.

Warnings: `utf16` (iWork text outside the BMP: "text positions may be off after characters outside the BMP") and `attribute` (the first unknown WordPerfect attribute number).

### Gate and format table

`scripts/reader/__init__.py`:
- `FORMATS` gains `wp`, `wp5`, `wp6`, `wp7`, `wpt` → `"wpd"`. The count goes from 41 to 46.
- `BUILT` gains `wpd`, `wp`, `wp5`, `wp6`, `wp7`, `wpt`, `mobi`, `azw3`, `pages`, `numbers` and `key`.

`gate.py`:
- `_deny` keeps "this format is not supported yet" for an unbuilt format (no detail). An `unsupported` with a detail reads `this file is not supported (<detail>)`. For Mac WordPerfect the detail ends `; set PHRO_READER_SOFFICE=1 to read it through LibreOffice`.
- No other change. A bundle folder fails `scope`'s `is_file` and goes to Read as now.

Routing:
- `cfb_route._STREAMS` gains `("PerfectOffice_MAIN", "wpd")`, after the Office streams.
- `doc.parse` sends a `FF 'WPC'` head to `wpd.parse`. A head that is not compound, zip, RTF or `WPC` is offered to the 4.2 check before "not a Word document".
- `html.py`'s docstring says epub and mobi use it.

### README

In "## Document reader":
- The eleven extensions join the built list, and the "Every other in-scope extension" list becomes djvu, mdb, accdb, pub, vsd and mpp.
- "41 in-scope extensions" becomes 46.
- A limits paragraph:
  - DRM Kindle books, KFX and Topaz are denied.
  - macOS bundle folders are not read; zip-saved iWork files are.
  - LZVN members are skipped.
  - Numbers number formats beyond the six kinds show raw values.
  - Text positions may be off after emoji in iWork.
  - WordPerfect 6+ hidden text may show as body text.
  - Pages '09 tracked changes are not read: deleted text shows as body text.
  - iWork '09 charts are counted, not read.
  - Mac WordPerfect needs `PHRO_READER_SOFFICE`.
  - WPG pictures are files only.
- The phase 3 bench lines are added after the phase.

In "## Not done yet": phase 3 is built; phase 4 remains.

### THIRD_PARTY_NOTICES.md

- New `## tika`: Apache-2.0 text and Tika's `NOTICE` lines. `Ported:` names `scripts/reader/containers/palmdb.py` and `scripts/reader/formats/wpd_charsets.py`, with the pinned tag.
- New `## foliate-js`: MIT text. `Ported:` names `scripts/reader/formats/mobi.py`, with the pinned commit.
- New `## numbers-parser` (MIT, v4.22.2, commit f783064231a796df8ab5ef66daf7078c705df508): `scripts/reader/formats/iwork_tables.py` (cell v5 order, decimal128, formats).
- New `## keynote-parser` (MIT): `scripts/reader/formats/iwork_iwa.py`, Keynote field numbers taken as facts.
- `## docling`'s `Ported:` line gains `scripts/reader/containers/snappy.py`, `scripts/reader/containers/pbwire.py` and `scripts/reader/formats/iwork*.py`, pinned to v2.150.0 (1ae470a1f4580881ed73853f64e0bf178f623c5e).
- Apple's `.proto` files are named as format knowledge, never copied.
- `test_notices_credit_port_sources` adds `tika`, `foliate-js`, `numbers-parser` and `keynote-parser` to its list.

The plan's first task records the Tika tag and the foliate-js and keynote-parser commits read for the port.

### Bench

The bench lives in `research/ai-document-reader-plugins/bench`, a local git repository.

- **Adapter.** `adapters/phro_read.py` gets wpd, mobi, azw3, pages, numbers and key.
- **Routing.** The rows stay: `wpd xberg`, `mobi`/`azw3 calibre>epub>docling`, `pages lo>docx>dochan`, `numbers numbers-parser`, `key docling`. The `wp*` variants have no corpus file and no row.
- **Corpus.**
  - From disk, chosen by feature:
    - Tika's four other mobi files and its WordPerfect 4.2 file (indexed as `doc`, as Tika names it; it tests the `doc.py` hand-off).
    - Tika's iWork files (IWA Pages among them, which the corpus lacks).
    - docling's keynote, pages and numbers files.
    - numbers-parser files with more than 256 rows, charts, merges, the six formats, wide offsets and passwords. No bundle folders.
  - The bias toward each file's own engine is noted.
- **Bar.** `bar --engine phro-read` must PASS for wpd, pages, numbers and key. For mobi and azw3 a token MISS that comes from linked images is recorded with the text tokens. Files the route cannot open score against the peers that can.
- **Addendum.** `reports/written/phro-read-phase3.md`, with the untested items of decision 31.

## Testing

All fixtures are built inside the tests.

**palmdb and mobi** (`tests/test_reader_mobi.py`). Helpers `_pdb(records, type)`, `_rec0(version, compression, …, exth)` and `_indx(tagx, entries, cncx)`.
- PalmDOC: each of the four code paths, an overlapping copy, and a bad distance.
- Trailing entries with flags 3.
- HUFF/CDIC: 8-bit terminal codes, a recursive phrase, a self-reference that fails, and a depth-33 chain.
- Books: MOBI 6 only; KF8 with two skeletons and three fragments; a combo (EXTH 121 + `BOUN`); KF8 with no skeleton (two `<html>`); KF8 failure falling to MOBI 6; stripped text.
- Content: `recindex`, `kindle:embed`, `kindle:flow` CSS with `display:none`, `filepos` inside a tag, `kindle:pos`, NCX headings (a marker inside a tag), and the footnote heuristic both ways.
- Refusals: compression 3 (`unsupported`), encryption 2 (`drm`), `TPZ` and `CONT` (`unsupported`), offsets backwards (`damaged`), CNCX zero padding, and each cap.

**wpd** (`tests/test_reader_wpd.py`). A WP6 builder (head, 14-byte index entries, packets, body; variable groups sized by the helper) and a WP5 builder (0xFFFB blocks).
- Text: extended characters (a composite and an unmapped one, `glyphs`), bold, and an undo span hidden and shown with `--hidden`.
- Notes and comments: a footnote packet, a comment chain, a note-in-note cycle (`depth`), and a WP5 comment (D9 2).
- Tables: a span joined to the cell above, and a header row.
- Resync: a broken group (one `record`).
- WP5: notes and headers inside groups, a picture given its WPG head.
- Wrappers: an OLE `PerfectOffice_MAIN` (the CFB builder of the cfb tests), `FF 'WPC'` as `.doc`, and 4.2 recognized and refused.
- Refusals: an encryption word (WP5 byte order too), the 4.2 password, a WPG file, and Mac with and without the switch (`soffice` swapped for a stub that writes a docx).
- An attribute above 16 is counted and warned.

**iWork** (`tests/test_reader_iwork.py`). A pbwire encoder, a literal-only Snappy writer with hand-made copy tags, chunks, archives, and zips with `Index/`, `Index.zip` and `index.xml(.gz)`.
- Containers: `.iwpv2`, an encrypted `.iwa` starting with 0x00, a '09 index with method 25452, a `bvxn` member counted, a nested `Index.zip`, and preview fallback.
- Object store: `should_merge` patches, a duplicate id, a self-reference and a style cycle.
- Text: headings by style name, lists, bold, links, footnotes, comments, a tracked deletion, an attachment at U+FFFC, and text outside the BMP (warning).
- Tables: two tiles (a row past 256), each v5 cell type, a v4 row, wide offsets, merges, a hidden row, the six formats and an unknown one, and a cell comment.
- Charts and Keynote: a chart; a skipped slide with notes and a comment; Pages headers and footers.
- '09: Pages body, header and footnote, ghost text dropped; Keynote slides and notes; Numbers cells, spans and formula results.
- Caps: `SNAPPY_BLOCK`, `IWA_BYTES` (a small budget injected), `IWA_OBJECTS`, a false tileid against `SHEET_ROWS`, and `IWORK_XML`.

**Gate and the rest.**
- Each new extension converts. `x.vsd` stays the unsupported example.
- An unsupported detail and the Mac switch hint fit in the reason.
- A bundle folder passes through. `tls.key` as plain text still passes through.
- `test_reader_hwpx.py:64` and `test_reader_zipx.py:35`: 46. The README and notices checks.
- `test_reader_mutation.py` gets mobi, wpd and iwork fixtures.
- A test that expects the bare "not supported yet" for a detailed `unsupported` (Excel 95, PowerPoint 95) changes.
- epub tests run unchanged after `referenced` and `settle` move.

## Review focus

The five inputs most likely to bite a user:

1. **A store-bought Kindle book.** Most are DRM or KFX under an `.azw3` name. They must deny as `drm` or `unsupported` with a clear reason, never as `damaged` and never with partial text.
2. **A WordPerfect 6+ legal or government document.**
   - Hidden text is not detected and reads as body text.
   - Undo spans are left out; Tika keeps them, so a reviewer comparing outputs sees a difference.
   - Redline and strikeout are read as plain text.
3. **A large Numbers workbook.** Tiles past 256 rows, wide offsets, v4 and v5 buffers side by side, styled empty cells against `SHEET_SCAN`, and formats outside the six kinds shown raw. A wrong tile row puts values on the wrong rows silently.
4. **An iWork file from an unusual source.**
   - A '09 file is told from IWA by its index member, and an `Index.zip` inside a folder must be found.
   - A password is detected two ways ('09 zip method, IWA `.iwpv2`).
   - An LZVN member must not fail the file.
   - A bundle folder fails in Read, as before.
5. **The mobi heuristics on a real book** (the Gutenberg files).
   - An NCX marker that lands inside a tag or a table can promote the wrong paragraph.
   - The footnote rule can turn a short internal link near the end (an index entry) into a note.
   - A `filepos` that points inside a tag must move to a tag boundary.

## Engine spec edits made with this amendment

| Line | Edit |
|---|---|
| 5 | Add the WordPerfect variants; the format count becomes 46. |
| 44 | wpd row: base "text-first reader from libwpd and Tika knowledge"; taken "Tika character sets, the Corel SDK 5.x reference"; gaps "6+ hidden text, Mac WordPerfect (LibreOffice opt-in)". |
| 45 | mobi/azw3 row: base "Tika `parser/mobi` (PalmDB, PalmDOC, HUFF/CDIC)"; taken "foliate-js (KF8 assembly, links)"; gaps "KFX, Topaz, skeleton-less KF8 read from flow 0". |
| 46 | pages/numbers/key row: add keynote-parser field numbers and the '09 XML reader; gaps "LZVN members, bundle folders". |
| 48 | Add Apache Tika (Apache-2.0), foliate-js (MIT) and keynote-parser (MIT) to the ported licences. libwpd and Apple's `.proto` files stay knowledge only. |
| 64-67 | Add `formats/wpd_charsets.py` and `formats/iwork_iwa.py iwork_text.py iwork_tables.py iwork_xml.py`. |
| 133 | Add `IWA_BYTES` and `MOBI_TEXT`. |
| 150 | Phase 3 row: specified in `2026-11-15-doc-reader-phase3-design.md`. |
| 158 | Add the KF8 fallback, the tile rows over 256 and the WordPerfect resync tests. |

## Out of scope

- DRM removal, user passwords, KFX, Topaz and Print Replica books.
- LZVN and LZFSE decoders, and macOS bundle folders.
- A WPG decoder, and rendering any vector picture.
- Mac WordPerfect without LibreOffice, WordPerfect headings from style names, macros and merge codes.
- Numbers formula text, pivot tables, and formats beyond the six kinds.
- iWork '08 and earlier and Keynote 1 to 3: untested, read only if they share the '09 vocabulary.
- Fonts, audio and video in Kindle books, and the NCX as a printed table of contents.
- Converting heic or tiff to a viewable format.
