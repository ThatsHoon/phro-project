# doc-reader phase 4: djvu, Access, Publisher, Visio binary and Project

Approved 2026-11-16, with its Defaults, on the user's go-ahead to run the remaining work ("남은 작업 계속 진행해라"). It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec); the engine spec edits listed at the end are made in the same commit. It builds on phase 2d (`2026-11-14-doc-reader-pdf-design.md`) and comes after phase 3. Notes behind it, outside this repository: `research/ai-document-reader-plugins/notes/djvu-access-survey.md` and `notes/pub-vsd-mpp-survey.md` (the surveys), `notes/rea-decisions.md` (the rulings) and `notes/sample-collection-report.md` (the downloads).

## Goal

The gate converts djvu (its text layer only), Access databases (mdb, accdb, mde, accde, accdr), Publisher (pub) and Visio binary (vsd). The vsdx reader gains master text and a connection list, so vsd and vsdx give the same output. Project (mpp) stays "unsupported", with one opt-in path: MPXJ in a plugin-managed venv, installed only on the user's yes, using Java that is already on PATH.

Success, as in the engine spec, with these changes:

- Every phase 4 extension but mpp converts with no install, through the engine's own parsers (the standard library is not a goal: a library replaces a parser wherever it measurably wins on tokens or compute, installed on the user's yes; the user, 2026-11-16). mpp converts only through the opt-in path.
- No bench document of these formats has a truth file. The bar per format is in "Bench" below. Where there is no truth, it says what the bar becomes.
- No real document's name or content enters this repository. Bench documents are named by doc_id only.

## Decisions (2026-11-14)

Decisions 1-3 are the user's answers (`rea-decisions.md`, "사용자 결정"). Decision 4 is the user's standing rule from 2d. The rest are rulings from the surveys (`rea-decisions.md`, sections "공통 원칙", "djvu·mdb·accdb" and "pub·vsd·mpp").

1. **Downloads.** The djvu-rs public-domain sample, Jackcess test data and POI publisher and diagram test data were allowed, and are indexed in bench commit f455ede. jackcess-encrypt could not be fetched (the survey's URL is a 404), so no encrypted Access file is in the corpus.
2. **MPL-2.0 ports are allowed.** Code ported from libmspub and libvisio carries the MPL-2.0 notice at the top of each ported file, and `THIRD_PARTY_NOTICES.md` gains a section for each.
3. **mpp is unsupported by default.** It is read only when Java is on PATH and the user has approved the MPXJ install.
4. Every install needs the user's yes. Setup never runs from a hook.
5. `containers/bzz.py` replaces `containers/iff.py`. djvu is the only IFF format in phase 4, so its chunk walk stays in `formats/djvu.py`.
6. djvu reads the text layer only. A page with no text layer is counted. When no page has one, the file is `unsupported`. Rendering through djvu-rs is left for later.
7. An indirect DjVu (pages in separate files) is `unsupported`.
8. A Jet database password makes the file `encrypted`, though its pages are not encrypted. The db_key RC4 encoding, which holds no user secret, is decoded.
9. Access values: ISO dates and plain numbers by default. Of the Format property, only Percent and Yes/No are applied.
10. Saved queries are rebuilt as SQL, ported from Jackcess (about 220 lines).
11. ACE attachment images are extracted. OLE objects are counted only.
12. accdb page type 8 is skipped and counted.
13. pub and vsd are written in pure Python, porting libmspub and libvisio (decision 2) and following POI where it is enough.
14. vsd and vsdx both get a "Connections:" list.
15. vsd and vsdx both read shape text inherited from masters.
16. A pub master page is its own Section. Shapes in the scratch area are hidden.
17. Visio 5 joins after it is checked on a POI v5 sample. The corpus now holds one (`poi__v5_Connection_Types.vsd`), so Visio 5 is in scope; if that sample does not read, Visio 5 is `unsupported` and the reason is recorded.
18. An MPP9 read password (an XOR mask) is `encrypted`.
19. Extension variants read by the same reader join: mde, accde, accdr. `cfb_route` learns VisioDocument, Quill and the MSProject CompObj.
20. Shared rules: sources are pinned to a tag or the survey-day commit; table rows use `SHEET_ROWS` and `SHEET_CELLS`; WMF and EMF stay as asset files, counted as not viewable; hidden content is left out, counted, and shown with `--hidden`; a value outside the format's spec is `unsupported`, a broken structure `damaged`; library tests skip without the venv and say so.

## Defaults (proposed with this spec, for approval with it)

These answer survey questions that `rea-decisions.md` does not rule.

- **No pure-Python mpp.** The user's answer covers the opt-in path only. A reader rewritten from MPXJ (LGPL) is not built, and no mpp structure is parsed outside MPXJ.
- **More extension variants**, by rule 19's principle: `djv` (djvu), `vst` (vsd template), `vsdm` and `vstx` (vsdx macro and template), `mpt` (mpp template). Stencils (`vss`, `vssx`) are not added: they hold masters and no pages.
- **Unsupported details reach Claude.** An `unsupported` with a detail is denied as "this file is not supported (<detail>)". Phase 3 adds this (`2026-11-15-doc-reader-phase3-design.md`); phase 4's details use it.
- **djvu text.** The text layer's separators give the blocks; the zone tree is not parsed. NAVM bookmarks become headings. ANT links and comments go at the end of their page. The ANT `(metadata (title …))` is the document title. DjVu's own "hidden text" is the OCR layer, not hidden content.
- **Access layout.**
  - Rows follow the primary key, sorted in memory, when the table fits the caps; otherwise they keep their physical order.
  - System tables (MSys*) are the file's catalog and are never shown or counted. A table with the hidden flag is a hidden Section.
  - A memo cell shows at most 32,768 characters.
  - Linked tables are listed with their target file and table. `PWD=` and `UID=` are removed from the connect string.
  - Forms, reports, macros and modules are listed by name. A module turns on the existing named item "VBA macros".
- **Visio.**
  - A background page is read as a page, as vsdx does today.
  - Text a shape hides (`m_hideText`) is hidden.
  - A connector is listed only when both ends are glued. Its text then goes in the list, not in the shape flow.
- **pub.**
  - Hyperlink URLs have no position (POI cannot place them either), so they go in one "Links:" line at the end of the document.
  - Bold comes from the character runs. Master pages go after the normal pages.
- **mpp offer.** Every mpp deny carries the install offer until the user declines, since the deny is all Claude gets.
- **Bench.** POI's four `Sample*.txt` files become `expect` strings for their pub files. The corpus takes 12 MPXJ test files, three each of MPP8, 9, 12 and 14.

## Shared parts

### `containers/bzz.py`

`decode(data, limit) -> bytes`, DjVu's BZZ codec: the ZP-coder, the MTF stage and the inverse BWT. It is ported from djvu-rs `djvu-bzz` and `djvu-zp` 0.45.0 (MIT). The ZP tables are checked against the DjVu 3 spec's appendix.

- Each block: a 24-bit size (0 ends the stream), the FSHIFT bits, then MTF indexes read through 262 contexts that are never reset between blocks.
- A block over `BZZ_BLOCK` (4 MiB, the spec's maximum) is `damaged`.
- Output stops at `limit`. The caller passes `BZZ_CHUNK` (4 MiB) per chunk, and the document's remaining `BZZ_TOTAL` (64 MiB).
- A chunk over 1 MiB of output is also held to `ZIP_RATIO` (200) against its input, as zip members are.
- Reading more than 16 bytes past the input's end is a truncation. A missing EOB marker, or `MARKERPOS` outside `0 <= pos < blocksize`, is `damaged`. djvu-bzz does not check the latter.

### Jet page reader (`formats/jet_pages.py`)

The Jet and ACE page layer. Format knowledge comes from mdbtools `HACKING.md` (LGPL repository, used as knowledge only). Code-level detail comes from Jackcess 5.0.4 (Apache-2.0).

- `open(data) -> Jet`: page 0, version and page size (Jet3 2,048; Jet4 and ACE 4,096).
- The header's bytes from 0x18 (126 in Jet3, 128 later) are RC4 with the fixed key `C7 DA 39 6B`, through `containers/crypto.rc4`.
- `Jet.page(n) -> bytes` checks `n` against `size // page_size`. With a db_key at 0x3E (Jet3 and Jet4), each page is RC4 with `db_key XOR n`.
- `Jet.usage(map_bytes) -> list[int]`: usage maps of type 0 (start page plus bitmap) and type 1 (through type 5 pages). Every page id is range-checked.
- `Jet.chain(first)`: a page chain (TDEF `next_pg`, LVAL chunks, type 1 maps) with a seen-set, so a cycle ends it.
- `Jet.text(raw, compressed)`: Jet3 text in the header's code page (0x3C, through `text.codepage`), and Jet4 UCS-2 or the `FF FE` compressed form whose 0x00 bytes toggle the mode.

### Visio and Publisher chunk readers

**`formats/vsd_stream.py`** (MPL-2.0, from libvisio `VSDInternalStream`, `VSDParser`, `VSD6Parser` and `VSD5Parser`; POI HDGF for the pointer cap):

- `header(data)`: `Visio (TM) Drawing\r\n`, the version at 0x1A (5, 6 or 11) and the trailer pointer at 0x24. Version 1-4 is `unsupported` ("Visio 4 or older").
- `inflate(data, limit)`: the LZ77 variant with a 4,096-byte window and a flag byte per eight items. About 40 lines.
- `pointers(stream, version)`: 18-byte pointers (v6, v11) or 16-byte ones (v5), and pointer lists.
- `chunks(stream, version)`: 19-byte headers with libvisio's trailer rules (v6, v11), or 12-byte headers (v5).

**`formats/pub_contents.py`** (MPL-2.0, from libmspub `MSPUBParser`, `MSPUBBlockType.h`, `MSPUBBlockID.h` and `MSPUBContentChunkType.h`):

- `blocks(data, start, end)`: block = id u8, type u8, data. The fixed length comes from the type; variable blocks start with a u32 length that counts itself.
- `directory(contents)`: the trailer at u32 0x1A, its `0x90` block, and each `0x88` chunk reference (type, offset, parent seqnum). A chunk ends where the next begins.

**`formats/pub_quill.py`** (MPL-2.0, from libmspub `parseQuill`; POI `QuillContents` for the bit layout):

- `bits(data)`: the `CHNKINK ` head and the bit directory at 0x18. libmspub's linked list is followed, with a seen-set.
- `stories(bits)`: `TEXT` (UTF-16LE) cut by `STRS` lengths and keyed by `SYID` text ids. `\r` ends a paragraph and `\v` breaks a line.
- Only TEXT, STRS and SYID are required. Without FDPC, FDPP or STSH the text stays and only bold is lost. libmspub drops the text instead.
- `cells(bits, story)`: `TCD ` offsets that cut a table story into cells.
- `links(bits)`: the URL list of the `PLC ` bit, type 12.

### Python environment (`pyenv.py`, `pysetup.py`)

2d's `pdfenv` and `pdfsetup` move to `pyenv.py` and `pysetup.py` and take `kind`: `"pdf"` or `"mpp"`. pdf behaves exactly as in 2d. For mpp:

| Item | Value |
|---|---|
| Folder | `PHRO_READER_MPPENV`, else `$CLAUDE_PLUGIN_DATA/mpp-venv`, else the pointer file `<cache root>/mpp-venv.json` |
| Pins | `scripts/reader/requirements-mpp.txt`: `mpxj==16.10.0` (one `py3-none-any` wheel), `jpype1==1.7.1` with one hash per platform wheel, and its dependencies pinned the same way. `--no-deps` stays. About 32 MB downloaded. |
| Ready marker | 2d's `ready.json` fields, plus `java`: the path and major version that setup found |
| Cheap check, every mpp Read | 2d's checks, plus `shutil.which("java")` still giving the recorded path |
| Engine id | `mpxj-16.10.0-java<major>` |
| Decline marker | `<cache root>/mpp-declined` |

**Setup** is `read.py setup --for mpp [--venv DIR] [--check] [--decline] [--remove]`; `--for` defaults to `pdf`. The steps are 2d's, with two changes:
- Before the venv is made, `java -version` must report a major version at or above MPXJ 16.10.0's documented floor. The plan reads that floor from MPXJ's documentation, a web read; Java 11 is assumed here. Otherwise setup prints `FAIL\tjava\t<detail>` and makes nothing.
- The smoke test starts the JVM through JPype and loads `org.mpxj.mpp.MPPReader`. This catches a 32/64-bit mismatch between Python and Java.

Java is never installed or downloaded by the plugin.

## Formats

### djvu (`formats/djvu.py`)

**Container.**
- `AT&T` then one `FORM`. `DJVU` is one page. `DJVM` is many: the first chunk is `DIRM`, and an optional `NAVM` follows.
- A chunk must fit inside its parent FORM. One that does not is dropped and counted `record`. An odd-length chunk is followed by one pad byte, as in IFF85; the plan checks this on the djvu-rs sample.
- Only one level of nesting exists: DJVM, then component FORMs.

**DIRM.**
- bit 7 of byte 0 is 0 (indirect): `unsupported`, "indirect DjVu (its pages are separate files; bundle it first)". DjVu 2's `NDIR` is `unsupported` too.
- Bundled offsets must lie in the file. Overlapping or duplicate offsets are dropped.
- The BZZ part gives the sizes, the flags (page, DJVI, THUM), the ids and the optional titles.

**Page text.**
- `TXTa`, or `TXTz` through `bzz.decode`: a BE24 length, then UTF-8 text, decoded with `errors="replace"`. The zone tree after it is not read.
- GS (0x1D), VT (0x0B) and US (0x1F) end a paragraph. LF ends a line.
- A line break becomes a space, except between Han or Kana, where it is dropped. A line-end `-` after a letter, followed by a lowercase letter, is dropped and the word joined.
- `INCL` brings in the chunks of a shared `DJVI` component, one level deep.
- A page with no TXTa or TXTz chunk is counted `textless`. A page whose text layer is empty is read, and empty.
- When every page is textless: `unsupported`, "no text layer: N image-only pages (OCR is out of scope)". That keeps a scan from being called damaged by `reader.convert`'s "nothing readable" check.

**Annotations** (`ANTa`, or `ANTz` through BZZ), read as one S-expression text per page, shared annotations included:
- `(maparea "url" "comment" …)` with an `http`, `https`, `mailto` or `ftp` URL: `> Link: <comment or url>` as a `Link`, at the end of the page.
- A maparea with no URL and a comment: `> Comment: <comment>`.
- An internal link (`#12`, `#+1`) keeps only its comment.
- `(metadata (title "…"))` gives `Doc.title`.

**Bookmarks** (`NAVM`, BZZ): each bookmark whose URL names a page becomes a `Heading` at the start of that page. The level is depth + 2, at most 6, and the text is cut at 120 characters.

**Output.**
- Each page is `Section("page", number=N, title=<DIRM title when it differs from N>)`, so 2d's renderer writes `## Page N` or `## Page N: title`.
- When any page holds image chunks (`BG44`, `FG44`, `Sjbz`, `BGjp`, `FGjp`, `Smmr`), the named item `page_images` is set.

### Access (`formats/jet.py`, `formats/jet_sql.py`)

**Versions.**
- Code 0 is Jet3 (Access 97), 1 is Jet4 (2000-2003), 2/3/5/6 are ACE12/14/16/17.
- The engine name `MSISAM Database` (Money) is `unsupported`.
- A file starting `01 00 00 00` (Access 1.x/2.0) is `unsupported`.
- Any other version code is `unsupported`.

**Encryption.**
- A Jet3 or Jet4 database password: `encrypted`, "database password set". In Jet4 the password bytes at 0x42 are masked with the creation date (decision 8).
- ACE with a db_key, or the 0x299 encryption structure: `encrypted`.
- Jet3 or Jet4 with a db_key and no password: decoded by `jet_pages` (the encoding has no user secret).

**Catalog.** MSysObjects is the TDEF at page 2.
- `Type` 1 is a table, 4 and 6 are linked tables, and 5 is a query.
- -32768 is a form, -32764 a report, -32766 a macro and -32761 a module.
- `Flags` 0x80000000 or 0x2 mark a system object, and 0x08 a hidden one.

**Properties.** `LvProp` blobs (`KKD\0` in Jet3, `MR2\0` later) give Format, Description, TextFormat, Expression and Required.

**Tables.**
- The TDEF chain gives the columns, the real and logical indexes (the primary key) and the usage maps.
- Each data page that the table's usage map owns is read. In the row offset table, 0x8000 is a deleted row (skipped, not counted) and 0x4000 an overflow pointer, followed once.
- Rows hold fixed and variable columns, the Jet3 jump table and the null mask. A Yes/No value is its null-mask bit.
- Old rows written before a column was deleted are read through the column's `offset_F` and `offset_V`.
- Long values: inline (0x80), one LVAL row (0x40) or an LVAL chain (0x00), through `Jet.chain`.

**Values.**

| Type | Shown as |
|---|---|
| Yes/No | `Yes` / `No` |
| Byte, Integer, Long, BigInt | the integer |
| Currency | up to 4 decimals, trailing zeros dropped, no symbol |
| Single, Double | the shortest round-trip `repr`; `Percent` format: × 100 with `%` |
| Date | `YYYY-MM-DD`, with ` HH:MM:SS` when the time is not 0. The fraction is a positive day part, whatever the sign. |
| Extended date (type 20) | ISO with trailing zero fraction digits dropped |
| Decimal (NUMERIC) | the exact decimal |
| GUID | `{XXXXXXXX-…}` |
| Text, Memo | the text. Rich text (TextFormat 1) goes through `html.text_of(html.tree(data))`. A hyperlink column's `display#address#` becomes a `Link` for the four schemes, else `display (address)`. |
| Binary, big binary | `<binary N bytes>` |
| OLE | `<OLE N bytes>`, counted `object` |
| Attachment (ACE complex) | the file names, joined with `; `. A jpg, png or gif is also an `Image` after the table, alt `<table> <key>: <file name>`. A deflated file goes through `flate.inflate` under a `flate.Budget`. |
| Other complex columns | their values joined with `; ` |
| Calculated | its stored value; the schema line shows `= <Expression>` |

**Queries** (`jet_sql.py`, a Jackcess `impl/query` port). The MSysQueries rows of each query (Attribute 1 type, 2 parameters, 3 TOP and DISTINCT, 5 sources, 6 columns, 7 joins, 8 WHERE, 9 GROUP BY, 10 HAVING, 11 ORDER BY) are rebuilt as SQL text:
- SELECT, INSERT, UPDATE, DELETE, make-table, crosstab (`TRANSFORM … PIVOT`), UNION, DDL and pass-through. Pass-through SQL is shown as stored.
- A query that cannot be rebuilt keeps its heading with no SQL, counted `query`.
- No query is ever run.

**Relationships.** MSysRelationships gives each foreign key's target, shown in the schema line.

**Pages.** accdb page type 8 is skipped and counted `jet_page8` (internal, not reported). Index pages are never read.

**Output.** First, one paragraph:

```
Access 2000-2003 database (Jet 4): 3 tables, 2 queries, 1 linked table.
Forms: Orders, Customers. Reports: Sales. Macros: AutoExec. Modules: Utils (names only).
Linked tables: Prices → prices.accdb (Prices)
```

Then, for each table:
- `Section("table", title="Orders (1,204 rows)")`, rendered as `## Table: Orders (1,204 rows)`. The count is the TDEF's row count.
- A schema line: `Columns: OrderID (Long, PK, autonumber) · CustomerID (Long → Customers.ID) · OrderDate (Date) · Total (Currency, required) · Notes (Memo, rich text)`. Columns Access hides (flag 0x10, replication `s_` and `Gen_`) are `Hidden` inlines here and hidden columns in the table.
- The table, through `sheet_grid.Grid` with one `sheet_grid.Budget` shared by the document. The header row holds the column names. Past a cap, the existing `sheet_cut` warning applies.

Then each query: `Section("query", title=name)`, rendered `## Query: name`, holding one `Code(sql, "sql")`.

A hidden table (flag 0x08) is a hidden Section. A table whose TDEF fails is skipped and counted `db_table`. When every table fails and nothing else was read, the file is `damaged`.

### Publisher (`formats/pub.py`, `formats/pub_2k.py`)

**Version.** The `Contents` stream's head `E8 AC xx 00`:
- `0x2C`: 2002 and later, read by `pub.py`.
- `0x22` with a Quill stream: Publisher 2000. `0x22` without one: 97/98. Both are read by `pub_2k.py`.
- Any other byte is `unsupported`.
- A compound file with no `Contents` goes to `cfb_route.route(cf, "pub", "no Contents stream")`.

**2002 and later.**
- The DOCUMENT chunk's page list gives the page order.
- Each PAGE chunk's shape list (block `0x02`) gives its shapes. A shape's text id (block `0x27`) names a Quill story.
- Shapes are ordered top, then left, by the box libmspub's `parseShape` reads. A shape with no box keeps list order, after the placed ones.
- TABLE chunks: rows (`0x66`), columns (`0x67`), and the CELLS chunk's start and end rows and columns, giving rowspan and colspan. The cell text is the table story cut by `pub_quill.cells`.
- Pictures: `Escher/EscherStm`, through `officeart.records`, `officeart.bstore` and `officeart.Shapes`. The ClientData shape id joins an Escher shape to its Contents shape, and `pib` names the BStore entry. `officeart.fbse_blip` reads the BLIP from `Escher/EscherDelayStm`. PNG, JPEG and DIB (`officeart.dib_to_bmp`, then `images.bmp_to_png`) are viewable; WMF and EMF are assets only.
- Master pages: a PAGE chunk with a non-empty `0x0E` (its own master name). Pages naming it through `0x0D` do not repeat its shapes.
- The DUMMY page seqnums `0x10D`, `0x110`, `0x113` and `0x117` hold the scratch area.

**2000** (`pub_2k.py`, from libmspub `MSPUBParser2k`):
- The trailer is at u32 0x16, and the chunk type is the chunk's first u16.
- A text box's text id is the u16 at +0x58. Quill text works as above.
- Every picture is a WMF (`0x21`): an asset, not viewable. There are no tables.
- The master seqnum is `0x109`. The DUMMY seqnums are `0x108`, `0x10B`, `0x10D`, `0x116` and `0x119`.

**97/98** (`pub_2k.py`, from libmspub `MSPUBParser97`):
- 8-bit text in `Contents`, through `text.decode_8bit(data, 0x0409)`. The garble check catches a wrong code page.
- CR LF ends a paragraph, `0x0B` breaks a line and `0x0C` ends a text box.
- Pictures are not read; each is counted `image`.

**Output.**
- Each page is `Section("page", number=N)`.
- Text boxes are `Para`s, with bold from FDPC runs. Tables are `Table`s and pictures `Image`s.
- Each master page is `Section("master", title=name)`, rendered `## Master page: name`, after the normal pages.
- The scratch area is one hidden `Section("page", title="Scratch area")`.
- The link list is `Para("Links: …")` at the end of the document.

### Visio binary (`formats/vsd.py`)

The tree walk starts at the trailer pointer. Pointers of format 0x5 hold pointer lists; 0x8, 0xC and 0xD are chunk streams. A pointer with format bit 2 is inflated, with a shift of 4.

| Content | Source |
|---|---|
| Pages | streams `0x27` and `0x15`, with names from `0x2D`, `0x33`, `0x2C` and `0xC9` |
| Shapes | chunk streams `0x48` (shape), `0x47` (group) and `0x4E` (foreign) |
| Text | chunk `0x0E`: 8 bytes skipped, then UTF-16LE (v11) or 8-bit (v5, v6, through `text.decode_8bit`). Field values (`0xA1`) are shown. |
| Position | `0x9B` (PinX, PinY, LocPin). Group offsets add up as in vsdx. |
| Pictures | `0x98` and `0x0C`: BMP, PNG and JPEG are viewable; EMF and WMF are assets only |
| Masters | stencils `0x1D` and `0x1E`. A shape that names a master and has no text of its own takes the stencil shape's text, as libvisio's `readShape` does. |
| Connections | the begin and end shape ids that libvisio's `readMisc` reads. The plan checks the byte pattern on `v5_Connection_Types.vsd` and a v11 sample. |
| Hidden text | `readMisc`'s `m_hideText`: the shape's text is a hidden `Para` |

**Output, the same as vsdx.**
- `Section("page", title=name)`.
- Shape text in `(-PinY, PinX)` order, through `vsdx._inlines`. Then the pictures.
- Then the connection list (below).

A VBA storage (`_VBA_PROJECT_CUR`) sets the named item "VBA macros".

### vsdx changes (`formats/vsdx.py`)

**Masters.**
- `visio/masters/masters.xml` maps a `Master` id to its part, through its relationships. Each master part is loaded once, through `ooxml_base.load`, up to `MASTERS` (1,000) parts.
- A top-level shape with `Master="M"` and no `Text` element takes the text of master M's shape: the one with its `MasterShape` id, else the master's first top-level shape.
- A sub-shape with `MasterShape="K"` takes the text of shape K in its group's master.
- The test `test_shared_media_and_master_ignored` changes: its `MASTERTEXT` is now read.

**Connections.**
- Each page's `Connects/Connect` gives connector `FromSheet`, the end `FromCell` (`BeginX` or `EndX`) and the target `ToSheet`.
- `vsdx.connections(edges, names) -> list` builds the blocks that vsd shares. That is `Para("Connections:")` and one `ListItem(0, "-", …)` per connector: `A → B`, or `A → B (label)` when the connector has text.
- A shape's name is its text's first line, cut at 80 characters. Failing that, its `NameU` or `Name`. Failing that, `shape <ID>`.
- Only connectors glued at both ends are listed. Those connectors' text leaves the shape flow.
- `CONNECTIONS` (10,000) caps the list per document. Past it the list is cut and counted `connections_cut`.

**Macros.** A `vbaProject` part (vsdm) sets the named item "VBA macros".

### Project (`formats/mpp.py`, `formats/mpp_mpxj.py`)

`mpp.parse` with no ready mpp venv raises `unsupported`, "Microsoft Project files are read only through MPXJ". The gate normally stops this sooner (see "Gate").

`mpp_mpxj.py` runs only in the child under the mpp venv interpreter (`read.py convert <path> --engine mpxj`).
- It starts the JVM through JPype with `-Xmx1024m` and `-Djava.awt.headless=true`.
- It reads with `org.mpxj.mpp.MPPReader`. `UniversalProjectReader` is not used, so a renamed file of another format is not read through MPXJ.
- MPXJ's password exception is `encrypted`. The plan checks how MPXJ 16.10.0 treats an MPP9 read-password file. If MPXJ reads one silently, the child reads the password flag through MPXJ's own props and returns `encrypted` (decision 18).
- Any other MPXJ exception is `damaged`. A JVM crash kills only the child.

**Output.**
- `Doc.title` is the project title. One line follows: `Start 2026-01-05 · Finish 2026-03-27 · Status date 2026-02-01`.
- `## Tasks`: `ID | Outline | Name | Duration | Start | Finish | % Complete | Predecessors | Resources`, through `sheet_grid.Grid`.
  - Outline is the WBS, else the outline level.
  - A summary task's name is bold, and a milestone gets ` (milestone)`.
  - Predecessors are written `3FS+2d`, resources `Name[50%]`. A duration is its own unit with a trailing `.0` dropped (`5d`, `2.5h`).
  - Null tasks are skipped. Inactive tasks are hidden rows.
- `## Resources`: `ID | Name | Type | Max units`.
- `## Notes`: one `ListItem` per task note, `ID Name: note`, cut at `MPP_NOTE` (20,000) characters.
- Calendars, views, filters, baselines and time-phased data are not read.

## Model and renderer

- `Section.kind` gains `table` (`## Table: <title>`), `query` (`## Query: <title>`) and `master` (`## Master page: <title>`), in `markdown._Render._section`.
- A new block `Code(text, lang="")` renders as a fenced block. The fence is one backtick longer than the longest backtick run inside, and at least three.
- `Section.comments` carries djvu's link and comment lines, as it does for pdf.

## Local caps

| Cap | Value | Past it |
|---|---|---|
| `BZZ_BLOCK`, `BZZ_CHUNK`, `BZZ_TOTAL` (`bzz.py`) | 4 MiB block, 4 MiB per chunk, 64 MiB per document | `damaged` for a block; the chunk counted `record`; the remaining pages counted `textless` |
| `DJVU_PAGES` | 5,000 pages per conversion | counted `page_cut` |
| `DJVU_DEADLINE` | 36 s for the page loop, as `PDF_DEADLINE` | the rest counted `page_cut`; the output is kept |
| `DJVU_ANT` | 1 MiB of S-expression per page, depth 64, strings 64 KiB, 256 mapareas | the rest ignored, counted `link` |
| `DJVU_BOOKMARKS` | 1,000 bookmarks, depth 32 | the rest unused |
| `JET_CATALOG` | 100,000 MSysObjects rows; names cut at 64 characters (Access's own limit) | the rest counted `record` |
| `JET_COLS` | 1,024 column numbers per TDEF | the table counted `db_table` |
| `JET_CELL_CHARS` | 32,768 characters shown per cell | cut with `…`, counted `text` |
| `JET_LVAL_BYTES` | 1 MiB read per long value; OLE and binary read only their length | as above |
| `JET_QUERY_ROWS`, `JET_SQL` | 10,000 MSysQueries rows per query; 65,536 characters of SQL | the query counted `query` |
| `JET_PROPS` | 1 MiB per LvProp blob, 10,000 chunks | properties ignored, counted `record` |
| `QUILL_BITS` | 1,000 bit directory entries | the rest ignored |
| `PUB_CHUNKS` | 100,000 chunk references | the rest counted `record` |
| `VSD_POINTERS` | 100,000 pointers per document (POI), and at most the remaining bytes ÷ 16 per list | the rest counted `record` |
| `VSD_INFLATE` | 256 MiB inflated per document (`CFB_STREAM`) | the stream counted `record` |
| `MASTERS` (`vsdx.py`) | 1,000 master parts per vsdx | inherited text from the rest left out, counted `stencil` |
| `CONNECTIONS` (`vsdx.py`) | 10,000 connectors per document, vsd and vsdx | counted `connections_cut` |
| `MPP_NOTE` | 20,000 characters per task note | cut, counted `text` |
| shared | `SHEET_ROWS`, `SHEET_CELLS`, `SHEET_CHARS` (Access, mpp); `GRID_SLOTS`, `TABLE_NODES` (pub tables); `IMAGE_BYTES` (all, renders none); `vsdx.MAX_TEXT` (20,000 per shape, vsd too); `officeart.MAX_RECORDS`; `BIN_RECORDS` (vsd chunks); `MAX_DEPTH` | as today |

The plan's measure task times pure-Python BZZ on the djvu-rs sample and on a 4 MiB synthetic stream. The survey guesses 0.3-0.5 MB/s. If a 500-page book does not finish inside `DJVU_DEADLINE`, the README's limits say so; the deadline keeps the output either way.

## Hostile input

- **djvu.** Chunk lengths up to 4 GB are checked against the parent. Component offsets that overlap or repeat are dropped. Only one nesting level is followed, and INCL goes one level deep. BZZ bombs stop at the caps above. The zone tree is never parsed, so its declared child counts are never trusted.
- **Jet.** Every page pointer is range-checked. TDEF, LVAL and usage-map chains use seen-sets, so a chain is at most the page count. An overflow row is followed once. Every offset and length is clipped to its row. A long value reads only up to its caps. Index pages are not read. Attachment deflate runs under `flate.Budget` and `ZIP_RATIO`.
- **pub.** The Quill list uses a seen-set and `QUILL_BITS`. Every block advances at least two bytes. A variable block's length must be at least 4 and fit in its parent. Parent seqnum cycles are cut by a seen-set. Block nesting stops at `MAX_DEPTH`; libmspub allows 100. A table's rows × columns must fit `GRID_SLOTS`. BLIPs go through `officeart` with `flate.Budget`.
- **vsd.** Visited `(offset, length)` pairs are tracked for every pointer type, where libvisio tracks only 0x5. The tree stops at `MAX_DEPTH`. Each chunk advances at least its header size. A `dataLength` must fit in what is left; a negative or short one ends the stream (POI's `NegativeChunkLength` and `ShortChunk` samples). LZ77 expands at most about 8.5×, and `VSD_INFLATE` caps the total.
- **mpp.** MPXJ runs in the child under `CONVERT_SECONDS`. A hang is a `timeout` and a crash is `damaged`. No macro or script in the file is run.
- **Prompt injection.** Hidden tables, columns, scratch-area shapes and hidden Visio text are left out by default. White or tiny text in pub and vsd is not detected.

## Errors, LOST and NAMED

| Kind | When |
|---|---|
| `unsupported` | djvu: indirect, NDIR, no text layer anywhere. Jet: MSISAM, Access 1.x/2.0, unknown version. pub: unknown version byte. vsd: Visio 4 or older (and Visio 5, if decision 17's check fails). mpp: no ready venv. |
| `encrypted` | Jet database password; ACE encryption; an `EncryptedPackage` (through `cfb_route`); an mpp password |
| `damaged` | no container, a broken structure, or nothing readable |
| `too_large`, `timeout` | as today |

New `LOST` entries:
- `textless`: "page with no text layer" / "pages with no text layer".
- `db_table`: "database table" / "database tables".
- `query`: "query not rebuilt as SQL" / "queries not rebuilt as SQL".
- `master_page`: "master page" / "master pages".
- `stencil`: "master shape" / "master shapes".
- `connections_cut`: "connection list cut short" / "connection lists cut short".

Kinds that already exist, used as they are: `page`, `page_cut` (2d), `shape`, `image`, `object`, `text`, `link`, `record`, `depth`, `cells`, `sheet_cut`.

New `NAMED` items:
- `page_images`, "page images (text layer only)".
- `linked_data`, "linked tables' data (in other files)".

`vba` ("VBA macros") is used by Access modules, vsd, vsdm and pub. New `INTERNAL` kind: `jet_page8`.

## Gate, format table and routing

`scripts/reader/__init__.py`:
- `FORMATS` gains `mde`, `accde`, `accdr` → `jet`; `djv` → `djvu`; `vst` → `vsd`; `vsdm`, `vstx` → `vsdx`; `mpt` → `mpp`. That is eight more than phase 3 leaves.
- `BUILT` gains djvu, djv, mdb, accdb, mde, accde, accdr, pub, vsd, vst, vsdm, vstx, mpp and mpt.
- `VERSION` is bumped, so cached vsdx outputs re-convert with masters and connections.

`formats/cfb_route.py`:
- `_STREAMS` gains `("VisioDocument", "vsd")` and `("Quill/QuillSub/CONTENTS", "pub")`.
- A new `progid(cf)` reads at most 4,096 bytes of `\x01CompObj`, as `doc_objects` does. After the stream list, a ProgID starting `MSProject.` routes to `"mpp"`.
- `_OWN` gains `vsd` → `VisioDocument` and `pub` → `Contents`.
- vsd, pub and mpp call `route` when their own stream is missing, as xls, ppt, doc and hwp do.

`gate.py`:
- **Unsupported detail.** Built in phase 3: `_deny` writes `this file is not supported (<detail>)` when an `unsupported` has a detail, and an extension not in `BUILT` keeps `REASONS["unsupported"]`. Phase 4 changes nothing here.
- **mpp.** For mpp and mpt, `pyenv.engine("mpp")` is checked before any child starts.
  - Not ready: deny at once with `<name>: Microsoft Project files are read only through MPXJ, which is not installed.` Then one of:
    - with Java on PATH and no decline marker: ` MPXJ 16.10.0 with JPype 1.7.1 (PyPI, about 32 MB, LGPL-2.1 and Apache-2.0) reads it using the Java on this machine. Ask the user whether to install it, and only on their yes run: python -I -X utf8 "<READ_PY>" setup --for mpp --venv "<DIR>" (or the same with --decline if they never want it).`
    - without Java: ` It also needs Java <floor> or later on PATH, which phro-project does not install.`
    - after a decline: ` (Declined; python -I -X utf8 "<READ_PY>" setup --for mpp installs it.)`
  - Ready: the child runs under the mpp venv interpreter with `--engine mpxj`, and `cache.key(path, engine)` takes the engine id, as for pdf.
- The approval flow is 2d's: Claude asks the user, naming the source, the size and the folder. On yes, Claude runs the exact command in Bash under Bash's own permission prompt. Nothing installs from a hook.

## README and notices

In "## Document reader":
- The built list gains the fourteen extensions.
- One paragraph covers:
  - djvu text layer only, no OCR;
  - Access tables as capped tables with their schema, queries as SQL that is never run, and forms and reports by name;
  - pub pages, tables, pictures and master pages;
  - vsd and vsdx connection lists and master text;
  - mpp through the opt-in MPXJ venv, Java not installed by the plugin, and MPXJ's LGPL-2.1 licence (installed, not copied).
- "Standard library only" is replaced by: no install needed for the built formats; the optional pdf and mpp engines run in their own venvs, installed on the user's yes; libraries are chosen by token and compute efficiency.
- The extension count changes, and the phase 4 bench lines are added after the phase.

In "## Not done yet", phase 4 is built.

`THIRD_PARTY_NOTICES.md` gains five sections, each with a `Ported:` line and the pinned version:
- **djvu-rs** (MIT): `djvu-bzz` and `djvu-zp` 0.45.0 in `scripts/reader/containers/bzz.py`.
- **Jackcess** (Apache-2.0, 5.0.4): `jet_pages.py`, `jet.py` (column types, compressed text, NUMERIC, usage maps, properties) and `jet_sql.py`.
- **Apache POI** (Apache-2.0, the bench checkout's commit): the Quill bit layout in `pub_quill.py` and the pointer cap in `vsd_stream.py`.
- **libmspub** (MPL-2.0, the 2026-11-14 master commit): `pub.py`, `pub_2k.py`, `pub_contents.py` and `pub_quill.py`.
- **libvisio** (MPL-2.0, the 2026-11-14 master commit): `vsd.py` and `vsd_stream.py`.

Each file ported from libmspub or libvisio starts with:

```
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub|libvisio <commit>, src/lib/<files>.
```

The MPL applies to those files only. mdbtools, MPXJ, DjVuLibre and djvu.js are used as format knowledge only and never copied.

## Testing

All fixtures are built inside the tests. The compound files come from `tests/reader_fix.cfb_bytes`.

**New builders.**
- `tests/djvu_fix.py`: an IFF writer, plus a ZP and BZZ encoder ported from djvu-bzz `encode.rs` (MIT, about 200 lines).
- `tests/jet_fix.py`: a Jet3, Jet4 and ACE page writer: the header with RC4, the catalog TDEF, user TDEFs, data and LVAL pages, and MSysQueries rows (about 250 lines).
- In `reader_fix.py`: `pub_contents`, `pub_quill`, `vsd_document`, `vsd_chunk` and `lz77`. Escher parts reuse `oa_record`, `oa_container`, `oa_fbse`, `oa_shape`, `dgg_with_bstore` and `blip_png`. The MSProject ProgID uses `comp_obj`.

**Cases.**
- bzz: round trips (empty, one byte, 4 MiB of zeros, random); a truncation, a missing marker, a bad `MARKERPOS`, an oversize block, `BZZ_CHUNK` and `BZZ_TOTAL`.
- djvu: one-page and bundled files, TXTa and TXTz; separators, line joins and the hyphen rule; a textless page, all pages textless, an empty text layer; indirect and NDIR; INCL; links, comments and the title; NAVM headings; overlapping offsets, an oversize chunk, and `DJVU_DEADLINE` with an injected clock.
- Jet: Jet3 (jump table, code page), Jet4 compressed text and ACE; every value type, Percent and Yes/No; inline, single and chained LVAL; overflow and deleted rows and a deleted column; key order; hidden tables and columns, system tables absent; linked tables without `PWD=`; attachments; each query type, a parameter query and one that cannot be rebuilt; encoding decoded, a Jet4 password, ACE encryption, MSISAM and Access 2.0; cyclic chains, out-of-range pointers, page type 8 and the caps.
- pub: 2002+ text boxes, bold, merged table cells, PNG, DIB and WMF pictures; master pages, the scratch area and links; 2000 and 97/98 text; a missing FDPC; a Quill list cycle, a zero or overlong block, a seqnum cycle and an oversized table.
- vsd: v11, v6 and v5; an inflated stream with literals and a back reference; groups, master text, hidden text, pictures, connections and VBA; Visio 4 `unsupported`; a pointer cycle of every type, negative and short chunk lengths, and `VSD_INFLATE`.
- vsdx: master text on a top-level shape and a sub-shape; labelled connections, a one-end connector, `CONNECTIONS` and `MASTERS`; vsdm VBA.
- mpp: the gate tests always run (the three deny texts, the 600-character limit, and a fake ready venv through `command=` with the engine id in the key). `pysetup` runs with `pip` and `java` stubbed; no test installs anything. `mpp_mpxj` tests run under the mpp venv and skip, saying so, without it; they read MPXJ test files from `PHRO_READER_MPP_SAMPLES`, outside this repository.

**Changed tests.**
- `test_reader_vsdx.py:216` (`test_shared_media_and_master_ignored`).
- The format counts in `test_reader_hwpx.py` and `test_reader_zipx.py`.
- `test_reader_gate.py`: unsupported now shows its detail.
- `test_notices_credit_port_sources` gains djvu-rs, Jackcess, POI, libmspub and libvisio. A new test checks that every reader file whose docstring names libmspub or libvisio starts with the MPL header.
- `test_reader_mutation.py` gains djvu, jet, pub and vsd seeds, read through the new in-memory entries `djvu.parse_bytes`, `jet.parse_bytes`, `pub.parse_compound` and `vsd.parse_compound`.

## Bench

The bench lives in `research/ai-document-reader-plugins/bench`, a local git repository. Corpus at f455ede: djvu 1, mdb 41, accdb 38, pub 21, vsd 13, vsdx 3, mpp 3. None has a truth file.

- **Adapters.**
  - `phro-read` gets djvu, mdb, accdb, pub, vsd and mpp. Without a venv, mpp fails as `unsupported`.
  - A new `phro-read-mpxj` runs under a bench venv made by `read.py setup --for mpp --venv <bench>/engines/phro-read-mpp/venv`, after the user's yes. It uses the bench's Temurin 21 JRE on its own PATH.
- **Routing.** `reports/written/routing.tsv` gains `mdb mdbtools`, `accdb mdbtools`, `pub lo>pdf>pymupdf4llm`, `vsd lo>pdf>pymupdf4llm`, `vsdx lo>pdf>pymupdf4llm` and `mpp mpxj`. These are the reports' picks, with no alt. djvu gets no row: no bench engine reads it.
- **Corpus.**
  - `expect` lists for `poi__Sample.pub` and `poi__Sample2.pub` to `poi__Sample4.pub`, one string per non-empty line of POI's `.txt` (Apache-2.0).
  - 12 MPXJ test files, chosen by version, named by doc_id only. MPXJ's test data stays outside this repository.
- **Bar.** Fidelity without truth is agreement with one peer, so the bar changes per format:

| ext | Bar |
|---|---|
| pub | the full bar, on the four documents with `expect` strings: fidelity at least the pick's minus 0.01. Tokens are listed, not gated, since the pick drops tables and master pages. The rest: no failure the pick does not share. |
| mdb, accdb | no failure the pick does not share (the pick fails Jet3; the engine must not). Tokens are listed beside the pick's, which cuts at 500 rows and shows no schema or SQL. A fidelity gap is reported with its diff and does not block. |
| vsd, vsdx | no failure the pick does not share, and every non-empty text line of the pick's output found in the engine's (a new check in `bar.py`). Tokens are listed, not gated, since the engine adds connections. |
| mpp | `bar --engine phro-read-mpxj` must PASS the full bar against `mpxj`; both run the same library. `phro-read` (no venv) is an expected MISS, by decision 3. |
| djvu | NODATA. The djvu-rs sample must convert, with as many page sections as DIRM lists pages and text on every page that has a text layer. That is the only check that the BZZ port matches an independent encoder. |

- **Addendum.** `reports/written/phro-read-phase4.md`.

## Review focus

The five inputs most likely to bite a user:

1. **A large Access database.**
   - One `sheet_grid.Budget` covers every table, so the first big table can use up `SHEET_CELLS` and cut the rest.
   - Primary-key sorting holds a whole table in memory within the caps.
2. **Access queries.** Crosstab, UNION, parameter and nested-join queries rebuilt from MSysQueries can come out wrong yet look right. The heading says "Query" and nothing runs it, but a wrong JOIN misleads.
3. **A long djvu book.**
   - Pure-Python BZZ speed decides how many pages fit in 36 seconds.
   - A partly OCRed scan reports its textless pages only as a count.
4. **Visio 5 and 2000 files, and connectors.**
   - 8-bit text in an unknown code page.
   - The `readMisc` byte pattern for connection ends.
   - Master text that duplicates or invents labels.
   - vsdx outputs change for existing users with the version bump.
5. **The mpp venv's life.** Each of these must end in a clean deny with a fresh offer, never a half-working engine:
   - Java removed or upgraded after setup, so the recorded path is gone;
   - a 32-bit Java with a 64-bit Python;
   - a plugin update that changes the pins;
   - MPXJ silently reading an MPP9 read-password file.

## Engine spec edits made with this amendment

| Line | Edit |
|---|---|
| 5 | Add the phase 4 extension variants (mde, accde, accdr, djv, vst, vsdm, vstx, mpt); mpp reads only through the opt-in venv. The format count follows. |
| 11 | "no software outside the plugin-managed venv" also covers the mpp venv. mpp has no stdlib path, and Java is never installed by the plugin. |
| 26 | Add decision 9 (2026-11-14): phase 4 as specified in `2026-11-15-doc-reader-phase4-design.md`; MPL-2.0 ports allowed; mpp opt-in. |
| 48 (Licenses) | libmspub and libvisio (MPL-2.0) are ported, with file headers. mdbtools, MPXJ and DjVuLibre stay knowledge only. |
| 64 | `iff` is already gone; no change. |
| 67 | `djvu.py jet.py jet_pages.py jet_sql.py pub.py pub_2k.py pub_contents.py pub_quill.py vsd.py vsd_stream.py mpp.py mpp_mpxj.py`; `pdfenv.py pdfsetup.py` → `pyenv.py pysetup.py`. |
| 83 | `setup [--for pdf\|mpp] [--venv DIR] [--check] [--decline] [--remove]`. |
| 106 | Add `## Table:`, `## Query:` and `## Master page:`, and the fenced `Code` block. |
| 123 | An unsupported denial shows its detail; the mpp offer rides on the deny. |
| 133 | Add `BZZ_TOTAL`, `DJVU_DEADLINE` and the Jet caps. |
| 151 | Phase 4 row: shared parts `bzz`, `jet_pages`, `vsd_stream`, `pub_contents`, `pub_quill`, `pyenv` (mpp); specified in this spec. |
| 153 | mpp: unsupported by default, opt-in through `read.py setup --for mpp`. |

## Out of scope

- OCR, djvu page images and IW44/JB2 decoding.
- Indirect DjVu.
- Running queries, or showing their results.
- Form, report and macro designs; VBA source.
- OLE objects in Access, beyond their size.
- Linked tables' data.
- Visio shape geometry, and pub WordArt and shape geometry.
- The pub mail Envelope.
- Visio 4 and older; Access 1.x/2.0; MSISAM.
- A pure-Python mpp reader.
- Project calendars, baselines and time-phased data.
- Installing Java.
- User passwords.
