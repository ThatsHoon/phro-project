# doc-reader metafiles: WMF, EMF, PICT, WPG and TIFF as viewable figures

Draft 2026-11-18, written from the user's answers in the brainstorm; it awaits the user's review. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec); the engine spec edits listed at the end are made in the commit that records its approval. It is the second of three content-gap specs, in the user's order: ① diagrams (done, reader 0.8.0), ② metafiles (this), ③ OCR. Research behind it, outside this repository: the session notes `research-wmf-diagram.md` (part 1).

## Goal

A vector picture in any document the engine reads reaches Claude in two forms. Its words come as text, from the picture's own records, on every OS with nothing installed. The picture itself comes as a PNG link, which Claude opens when it needs the drawing. Today WMF, EMF, PICT, WPG and TIFF pictures are kept as files Claude cannot view and listed once in a note.

Why both: a vector picture holds its meaning in two places. Text records carry labels, axis values and equation symbols. Line and shape records carry everything else, such as arrows, layout and clip art. In the bench corpus (counted 2026-11-18, `real-` documents left out), the 15 WMF pictures (Publisher clip art) have no text record at all and 8 to 322 drawing records each. The 5 Office EMF pictures have 5 to 64 characters of text and are all EMF+ dual. So text alone would leave most pictures empty, and a PNG alone makes Claude open an image to read a label.

Success:

- On Windows every WMF, EMF (with EMF+), PICT, WPG and TIFF picture in a document becomes a linked PNG with nothing installed. The documents with metafiles in the bench are 2 doc, 1 ppt, 1 pptx and 2 pub; with them come the hwp and hwpx documents and any rtf or wpd with such pictures.
- On macOS and Linux the same pictures become PNGs once the user has said yes to the image venv. Without it their text still comes out, and the gate offers the install.
- The text of a picture follows its link. Text stored as glyph numbers is turned back into characters when the font is on the machine.
- A lone `.wmf .emf .wmz .emz .tif .tiff .pct .pict .wpg` file converts through the gate like a document.
- No bench regression: every non-empty text line the engine gave before is still found in its output.

## Decisions (user, 2026-11-18)

1. Rendering: GDI+ on Windows, through ctypes with nothing installed; a WMF/EMF to SVG writer of our own with resvg_py elsewhere, in an opt-in venv. (Options turned down: GDI+ only; SVG and resvg on every OS; LibreOffice when present.)
2. Output: the PNG link, then the picture's text. A picture with no text gets the link alone.
3. Scope: lone metafile files (`.wmf .emf .wmz .emz`); TIFF, PICT and WPG as well; text stored as glyph numbers is recovered from the system's fonts.
4. Approach A: every vector format is parsed into one drawing list. On Windows WMF and EMF go to GDI+ as they are, and PICT and WPG reach GDI+ as an EMF the reader writes from the drawing list. Elsewhere the drawing list is written as SVG and resvg_py rasterizes it. (Approach B, rendering PICT and WPG through resvg on Windows too, was turned down.)

## Output

### Where the figure goes

An Image block whose bytes are WMF, EMF, PICT, WPG or TIFF is converted when it is rendered. On success the asset written is the PNG, named `img-NNN.png` as any image; the original bytes are not kept. The block renders as today's link, then its text:

```
![chart](assets/img-002.png)

Figure text: 2024 sales / Q1 120 / Q2 135
```

- `Figure text:` starts its own paragraph right after the link, inside the same hidden tag when the image is hidden.
- In an inline place, such as a table cell, a list item's first line or a heading, the text follows the link on the same line after one space: `![chart](assets/img-002.png) Figure text: 2024 sales / Q1 120`.
- A picture with no text, or whose text is only spaces, gets the link alone.
- A picture that does not convert is kept as its original file and listed in `Not viewable here:` as today. Its text is still written after the `[image: alt]` text, or alone when there is no alt.
- The same bytes twice share one PNG, as today, and repeat their text at each place.

### The figure text

The parser collects text runs from the drawing list with their position in the picture's own frame, after every transform (mapping mode, window and viewport, world transform, EMF+ page and world transforms, containers).

- Runs whose baselines lie within half the larger font height of each other form one line. Within a line, runs go left to right; a gap wider than the font height between two runs becomes one space, a smaller gap none. Lines go top to bottom.
- Rotated text (escapement or a world transform that is not a scale and offset) is its own line, placed by its anchor point.
- A run repeated within a quarter of its font height of an earlier one with the same text (shadows, bold by overprinting) is dropped.
- Lines are joined with ` / `. Each line's spaces are collapsed and the Markdown escapes of a paragraph are applied once.
- A picture's text is cut at `FIGURE_CHARS` (20,000) characters with `…`, and the warning `figure text cut` is added once per document.

### The PNG

- Size: the picture's frame at 96 dpi. WMF uses the placeable header's box and units per inch; a WMF without one uses its `SetWindowExt` extent as pixels, else 512 px on the long side. EMF uses `rclFrame` (0.01 mm). PICT uses `picFrame` at 72 dpi, WPG its page size, TIFF its pixels.
- The long side is scaled down to at most `FIGURE_EDGE` (1568 px), the size above which Claude's image input is downscaled anyway. A small picture is scaled up at most 2×. The pixel count stays under the existing 16,777,216 cap.
- The background is white: a transparent PNG shows dark in some viewers, and office pictures assume paper.
- TIFF inside a document gives its first page. A lone TIFF file gives every page, up to `TIFF_PAGES` (100), one image each.

### Lone files

The new extensions `wmf emf wmz emz tif tiff pct pict wpg` go to a new format module `picture`. It returns one Image block per picture (one per TIFF page), so the output is the link and its figure text. `wmz` and `emz` are gzip-compressed WMF and EMF. Their inflated size is checked against `ZIP_RATIO` the way zip members are, and capped at `IMAGE_BYTES`. A file whose bytes are not the format its extension names is `damaged`.

## The drawing list: `reader/vector.py`

One small model that every vector parser fills and every writer reads. It lives in the reader package and holds no format knowledge.

- `Drawing(frame, items)`: `frame` is the picture's box in its own units, with the dpi that turns them into pixels.
- Items, in paint order:
  - `Path(segments, pen, brush, fill_rule)`: moves, lines, cubic Béziers and arcs, already transformed.
  - `Text(runs)`: each run has text, anchor x and y, font face, height, weight, italic, angle and color.
  - `Bitmap(png_or_jpeg, box)`: device-independent bitmaps are turned into PNG with the existing pure-Python PNG writer. A JPEG inside a picture is kept as JPEG.
  - `Clip(path or None)`: rectangles and paths. Region-combine modes other than intersect and copy approximate to the new region's box.
- `text_lines(drawing) -> list[str]` builds the figure text as specified above. It is the only text path: every OS and backend gives the same text.

## Parsers

Each parser takes bytes and a budget and returns a `Drawing` or raises the reader's `ReadError` kinds. They run in the conversion child like every format parser.

### `reader/metafile.py`: WMF, EMF and EMF+

- **WMF** (MS-WMF): placeable and plain headers. The records kept are the subset that office pictures use: object creation and selection (pen, brush, font, palette, region); the mapping mode, window and viewport origin and extent and their offsets and scales; save and restore DC; lines, polylines, polygons, polypolygons, rectangles, rounded rectangles, ellipses, arcs, chords and pies; text out and ext text out; BitBlt, StretchBlt, StretchDIB, DIBBitBlt, DIBStretchBlt and SetDIBitsToDevice with SRCCOPY-like raster operations; clip rectangles and region selection. Others are skipped and counted.
- **EMF** (MS-EMF): the same drawing set in its EMF form, plus world transforms (set and modify), paths (begin, end, fill, stroke, stroke-and-fill, select clip path), poly-Bézier records, both 16 and 32 bit, and `SMALLTEXTOUT`. `POLYTEXTOUTA/W` is read as several `EXTTEXTOUTW`, as MS-EMF says.
- **EMF+** (MS-EMFPLUS), inside `GDICOMMENT` records:
  - Objects: brush (solid, hatch as solid, linear gradient as its average color), pen, path, font, image (bitmap and nested metafile), string format and region.
  - Drawing: fill and draw for rects, ellipses, pies, arcs, polygons, lines, paths, Béziers and curves (cardinal splines as Béziers); `DrawImage` and `DrawImagePoints`; `DrawString`; `DrawDriverString`.
  - Transforms: world and page transforms, save and restore, and containers.
  - **Dual files** follow the EMF+ records and skip the EMF records they duplicate. EMF records after `EmfPlusGetDC` are followed until the next EMF+ drawing record. An EMF-only file follows the EMF records.
- **Text decoding**:
  - Wide text is UTF-16LE.
  - ANSI text (WMF text, `EXTTEXTOUTA`, `SMALLTEXTOUT` with 8-bit characters) is decoded with the code page of the selected font's charset: 0 ANSI cp1252, 128 cp932, 129 cp949, 130 Johab cp1361, 134 cp936, 136 cp950, 161 cp1253, 162 cp1254, 163 cp1258, 177 cp1255, 178 cp1256, 186 cp1257, 204 cp1251, 222 cp874, 238 cp1250. A font named Symbol, in any charset, maps its codes 0x20 to 0xFE to Unicode by the Adobe Symbol encoding. Equation Editor and MathType pictures write their Greek letters and operators this way. The table goes in `reader/text.py`. Text in other SYMBOL_CHARSET (2) fonts, such as Wingdings, is dropped and counted as unmapped glyphs.
  - Charset 1 (DEFAULT) uses the document's code page when the caller passes one (hwp and doc know theirs), else cp1252.
  - Text with `ETO_GLYPH_INDEX`, and EMF+ driver strings without the cmap-lookup flag, are glyph numbers: see Fonts.
- Sources: MS-WMF, MS-EMF and MS-EMFPLUS are the specification. Apache POI's `hwmf` and `hemf` (Apache-2.0) and LibreOffice's `emfio` (MPL-2.0) may be ported with credit in `THIRD_PARTY_NOTICES.md`, and each ported file carries the MPL-2.0 header where the MPL code is used. libUEMF and libemf2svg (GPL) are knowledge only.

### `reader/pict.py`: QuickDraw PICT

- PICT 1 and 2, with or without the 512-byte file header. A PICT from an OfficeArt BLIP or rtf `\macpict` has no header, so `sniff` gains a check on `picSize`, `picFrame` and the version opcode at offset 0 for bytes handed over as PICT.
- Kept: lines, rects, round rects, ovals, arcs, polygons and regions as their boxes, in frame, paint and fill forms; pen size and colors; text (`LongText`, `DHText`, `DVText`, `DHDVText`) with `TxFont`, `FontName`, `TxSize` and `TxFace`, decoded as Mac Roman (Mac Japanese, Korean and Chinese when the font's script says so); bitmaps (`BitsRect`, `BitsRgn`, `PackBitsRect`, `PackBitsRgn`, `DirectBitsRect`, `DirectBitsRgn`); QuickTime compressed pictures whose codec is `jpeg`, as JPEG. Other QuickTime codecs and comments are skipped and counted.
- rtf `\macpict` leaves `_UNREAD` and becomes an Image.
- Sources: Apple's Inside Macintosh: Imaging With QuickDraw (Appendix A, the opcode table) is the specification. Apache Tika and POI are Apache-2.0 if any PICT code there helps.

### `reader/wpg.py`: WordPerfect Graphics

- WPG 1 and WPG 2: lines, polylines, rectangles, polygons, ellipses and arcs, curves, text with its attributes, colormaps, and bitmaps (WPG 1 types 1 and 2 with their RLE, WPG 2 bitmaps). Embedded PostScript is skipped and counted.
- Text is decoded with the WordPerfect character sets the wpd reader already has (`wpd_charsets`).
- Sources: libwpg is used for its format knowledge. Its files may be ported only where they carry the MPL-2.0 option, with the MPL-2.0 header and credit, as was done for libvisio. Otherwise it is knowledge only. Which applies is checked file by file in the plan.

### TIFF

TIFF is a raster, so there is no drawing list and no text; scanned pages wait for spec ③. On Windows GDI+ decodes it. Elsewhere Pillow in the image venv does. Without either it stays as it is today.

### Fonts: `reader/fonts.py`

- Glyph numbers are mapped back through the font named by the run (the EMF `LOGFONT` face or the EMF+ font object).
- The font is found in the system's font folders:
  - Windows: `%WINDIR%\Fonts` and `%LOCALAPPDATA%\Microsoft\Windows\Fonts`.
  - macOS: `/System/Library/Fonts`, `/Library/Fonts` and `~/Library/Fonts`.
  - Linux: `/usr/share/fonts`, `/usr/local/share/fonts`, `~/.local/share/fonts` and `~/.fonts`.
- TrueType, OpenType and collection files are matched by the family and full names (name IDs 1, 4, 16) in any language, ignoring case. Only the table directory and the `name` table are read to build the index.
- The index (path, size, mtime, names) is kept in the reader cache folder and rebuilt for files whose size or mtime changed.
- The chosen font's `cmap` (formats 4 and 12) is inverted: each glyph maps to its lowest code point. Glyphs with no code point, such as ligatures and alternates, are dropped from the run, and the document gets the warning `figure glyphs unmapped` once.
- With no matching font, glyph-number runs are left out of the figure text and the same warning is added. GDI+ still draws them from the glyph numbers.
- Caps: at most `FONT_FILES` (4,000) files are indexed, at most `FONT_BYTES` (64 MiB) of one file is read, and an inverted cmap is kept per font for the process.

## Writers and backends: `reader/raster.py`

`raster.convert(kind, data, *, deadline) -> (png | None, lines)` is the one call `Assets` makes. It parses the picture into its drawing list, takes the text lines from it, and picks a backend for the pixels.

### Windows: GDI+ through ctypes

- `gdiplus.dll`, `ole32.dll` and `shlwapi.dll` through `ctypes` from the standard library: GDI+ is part of Windows, so nothing is installed.
- GDI+ starts up once per process and shuts down at exit.
- WMF and EMF bytes are handed over as they are: GDI+ is the renderer Office uses, and it plays EMF+.
  - A memory stream (`SHCreateMemStream`) is loaded with `GdipCreateMetafileFromStream`.
  - A WMF without the placeable header is wrapped in one, built from its window extent, first.
- PICT and WPG reach GDI+ as an EMF that `reader/emf_write.py` writes from the drawing list, using only the plain EMF records (paths, fills, pens, `EXTTEXTOUTW`, `STRETCHDIBITS`). JPEG bitmaps are decoded by GDI+ from a memory stream, then drawn.
- TIFF is loaded with `GdipCreateBitmapFromStream`, and a page is picked with `GdipImageSelectActiveFrame`.
- Drawing: a 32-bit ARGB bitmap of the target size, filled white, with high-quality smoothing and text rendering. The image is drawn into it, then saved to a memory stream with the PNG encoder.
- Any GDI+ status other than Ok leaves the picture unconverted. Each call that can fail is checked.

### Elsewhere: SVG and resvg_py

- `reader/svg_write.py` writes the drawing list as SVG 1.1:
  - paths with stroke and fill;
  - text as `<text>` with the run's font family, size, weight, style, rotation and color, plus a generic fallback: `sans-serif` with a CJK face for charsets 128, 129, 134 and 136, and `serif` for Times-like faces;
  - bitmaps as `data:` URIs;
  - clips as `clipPath`.
- resvg_py renders the SVG to PNG with the system fonts loaded. It runs in the image venv, in one subprocess per document: every SVG of the document goes in one batch, and the subprocess is given the time left before the deadline.
- TIFF goes to Pillow in the same subprocess.

### The image venv

- A third kind `image` in `pyenv` and `pysetup`, beside `pdf` and `mpp`. `requirements-image.txt` pins resvg-py 0.5.0 (MIT) and a Pillow release (MIT-CMU), as binary wheels with sha256 hashes, in the same way as `requirements-pdf.txt`. The Pillow version is chosen in the plan as the newest whose wheels cover the Python versions the plugin supports.
- It is made only by `read.py setup --for image`, run on the user's yes, with a smoke render of a tiny SVG holding one line of text.
- On Windows the image venv is never offered: GDI+ does the work. If it exists there, it is still not used.
- The env var `PHRO_READER_IMAGEENV` names the venv folder, or is `off` to turn it off, as for the other kinds.

### Time

- Before each picture is converted, the reader checks the deadline (`PHRO_READER_DEADLINE`). With less than `FIGURE_RESERVE` (3 s) left, that picture and the ones after it stay unconverted. Their text is still written, because parsing is cheap, and the document gets the warning `figures not rendered: time`.
- A GDI+ call that hangs is ended by the hook's own timeout on the child, which the gate already handles (`LATE_TEXT`).

## The gate and the cache

- The new extensions go into `FORMATS` and `BUILT`, so the gate converts them.
- The cache key gains the image backend id for every format: `gdiplus`, `resvg-<resvg_py version>`, or `none`. A document read with no backend and read again after the venv is installed is converted again.
- `meta.json` gains `figures`, the count of pictures converted to PNG, and `figures_left`, the count left as unviewable files. `images` still counts the viewable images `out.md` links to.
- The setup offer applies when `figures_left` > 0 and the backend is `none` (macOS or Linux with no image venv). It reads: `k figures (WMF/EMF/PICT/WPG/TIFF) are not shown as images; their text is included. To show them, run python <READ_PY> setup --for image (on the user's yes).` It is offered at most once a session, and it fits under `CONTEXT_MAX` as the pdf offer does: whole or not at all.

## Local caps

All of these go in `limits.py`:

| Name | Value | Bounds |
|---|---|---|
| `FIGURE_CHARS` | 20,000 | figure text per picture |
| `FIGURE_EDGE` | 1,568 | long side of a figure PNG, in pixels |
| `FIGURE_RESERVE` | 3 | seconds left before the deadline under which no further picture is rendered |
| `METAFILE_RECORDS` | 1,000,000 | records of one WMF, EMF, PICT or WPG |
| `METAFILE_POINTS` | 4,000,000 | points across all paths of one picture |
| `METAFILE_OBJECTS` | 65,535 | WMF and EMF object table slots; EMF+ uses its own 64 |
| `METAFILE_NESTING` | 16 | metafiles nested inside EMF+ image objects |

Saved DC states and EMF+ containers are bounded by the existing `MAX_DEPTH` (64).
| `TIFF_PAGES` | 100 | pages of a lone TIFF |
| `FONT_FILES` | 4,000 | font files indexed |
| `FONT_BYTES` | 64 MiB | bytes read from one font file |

Bitmaps inside pictures use the existing `_MAX_PIXELS` and `IMAGE_BYTES`.

## Hostile input

- Every length, count and offset read from a record is checked against the record and the file before use. A record whose size runs past the end ends the parse. The drawing up to that point is kept, and the document gets the warning `figure damaged`.
- The object tables, the DC stack and the EMF+ container stack are bounded by the caps above. Selecting an object that does not exist is ignored.
- Transforms with NaN, infinite or zero-determinant matrices are replaced by the identity for that item. Coordinates are clamped to ±2^31 before they are written to SVG or EMF.
- The SVG writer escapes all text and font names as XML. It writes no external references and no scripts, so resvg has nothing to fetch.
- GDI+ parses the original WMF and EMF bytes itself. It runs inside the conversion child, so a crash in it ends the child, and the gate's existing failure path hands the Read to Claude's own Read. Nothing is written to disk: streams are in memory.
- Fuzzing: the mutation test gains WMF, EMF (with EMF+), PICT and WPG seeds, mutated in memory only. The GDI+ path is not fuzzed in the test suite, because it is the OS's parser.

## Testing

- **Fixture builders** in `tests/metafile_fix.py` write tiny WMF, EMF, EMF+ dual, PICT and WPG bytes in memory: a box with text, an ANSI text with a font charset, a glyph-index run, and a dual file whose EMF+ and EMF records carry the same text.
- **`vector.text_lines`:**
  - two runs on one baseline;
  - runs on different lines;
  - the gap rule, a rotated run and an overprinted shadow;
  - the `FIGURE_CHARS` cut.
- **Parsers:**
  - each format's text, and its frame size;
  - the charset table, at least 129 (cp949), 128 and 0;
  - dual files giving their text once;
  - `GetDC` handing back to EMF records;
  - mapping modes and window and viewport math in WMF; world transforms in EMF;
  - PICT with and without the 512-byte header.
- **Fonts:** an index and cmap test on a TrueType font built in the test, with a `name` table and a format 4 `cmap`, and no system font needed.
- **GDI+:** Windows only; elsewhere the tests skip, saying so. Each kind becomes a PNG of the expected size. A white box with black text has dark pixels where the text is. A GDI+ failure leaves the picture unconverted.
- **SVG writer:** the output parses as XML, holds the expected elements and escapes text. Rendering through resvg runs under the image venv and skips without it, saying so.
- **Rendering in Markdown:**
  - the figure-text paragraph, and its inline form in a table cell;
  - a picture with no text;
  - the unconverted path with its text;
  - hidden images;
  - the `meta.json` counts.
- **Gate:** the new extensions; the cache key's backend id; the setup offer on a non-Windows backend `none`, simulated.
- **Bench:** re-run every document whose output listed a WMF, EMF, PICT, WPG or TIFF asset, every hwp, hwpx, rtf and wpd document, and the doc, ppt, pptx and pub documents.
  - The check is that every non-empty text line of the previous output is still found.
  - The addendum lists the pictures that became PNGs, their figure text lengths and the token change.

## Review focus

1. **Coordinate math.** Mapping modes, window and viewport extents with negative signs, and world transforms put text runs in the wrong order when they are wrong. GDI+ hides this on Windows, because it draws from the original bytes, so the figure text is the only check there.
2. **EMF+ dual files.** Text drawn by both the EMF+ and EMF records appears twice, or not at all if the `GetDC` rule is wrong.
3. **Korean and other ANSI text.** WMF pictures in hwp and doc files carry cp949 text under a HANGUL_CHARSET font, or under DEFAULT_CHARSET with the document's code page. A wrong choice gives mojibake that the garble check may not catch in short labels.
4. **The cache key.** Without the backend id, a user who installs the image venv keeps the old unconverted output until the version changes.
5. **Inline places.** A figure-text paragraph inside a table cell or list item breaks the table or the list.

## Engine spec edits made with this amendment

| Line | Edit |
|---|---|
| 5 | The extension count grows by nine (`wmf emf wmz emz tif tiff pct pict wpg`), and the library sentence names resvg_py and Pillow in the opt-in image venv. |
| 11 | Add: pictures convert to PNG on Windows with nothing installed (GDI+); elsewhere through the image venv, made only on the user's yes; their text needs nothing. |
| 28 | Add decision 11 (2026-11-18): metafiles as specified in `2026-11-18-doc-reader-metafiles-design.md`. WMF, EMF, PICT, WPG and TIFF pictures become PNG links followed by their text, drawn by GDI+ on Windows and by our SVG writer and resvg_py elsewhere. |
| 74 | `pyenv.py pysetup.py` covers the pdf, mpp and image venvs. |
| 87 | `setup --for pdf\|mpp\|image`. |
| 114 | Images: WMF, EMF, PICT, WPG and TIFF are converted to PNG when a backend is present, and their text follows the link; a picture that does not convert is kept as a file and listed once in a note, as before. |
| 134 | The cache key also holds the image backend id. |
| after 157 | Add a row: metafiles; wmf, emf, wmz, emz, tif, tiff, pct, pict, wpg and the pictures in every format; shared parts `vector`, `metafile`, `pict`, `wpg`, `fonts`, `raster`, `emf_write`, `svg_write` and `pyenv` (image). |
| 172 | Out of scope now reads: "OCR of scanned pages or images (spec ③; until then a scanned pdf page goes out as a page image and a scanned TIFF as a PNG); diagram structure in pdf, xps and pictures, which have no connector data; …" |

## Out of scope

- HEIC, and SVG images in OOXML (`asvg:svgBlip`), which always carry a PNG fallback that the reader already links.
- OS/2 metafiles (rtf `\pmmetafile`) and Windows device-dependent bitmaps (rtf `\wbitmap`).
- Turning the shapes inside a picture into a Mermaid diagram: pictures hold no connector data.
- Office-identical rendering in the SVG path: shadows, soft edges, theme effects, gradients beyond their average color, hatch patterns, and raster operations other than copy-like ones.
- OCR of text that exists only as pixels inside a picture (spec ③).
- Rendering pages of a document to images, beyond the pictures it holds.
