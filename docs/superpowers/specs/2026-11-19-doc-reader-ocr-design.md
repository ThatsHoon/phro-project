# doc-reader OCR: text from scanned pages and pictures

Approved 2026-11-19 (the user: "승인, 스펙 문서 작성해"; the written spec: "확정, 계획 작성"), written from the user's answers in the brainstorm. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec); the engine spec edits listed at the end are made in the commit that records its approval. It is the third of three content-gap specs, in the user's order: ① diagrams (done, reader 0.8.0), ② metafiles (done, reader 0.9.0), ③ OCR (this).

Research behind it, outside this repository, in `research/ai-document-reader-plugins/`:
- `reverse/rea/windows-ocr.md`: REA 6.8.0 on `Windows.Media.Ocr.dll` and its model file.
- `notes/ocr-spike/report.md`: both engines measured on 30 synthetic scans with known text (`make_pages.py`, `score.py`, `scores.tsv`).

## Goal

Text that exists only as pixels reaches Claude as text: in scanned pdf pages, lone TIFF scans and raster pictures inside documents. Today a scanned page goes out as a page image (about 1,660 tokens), a TIFF as a PNG, and a picture's words are invisible until Claude opens the image.

What OCR buys, from the spike:
- **Exact, searchable, quotable text.** This holds on every page.
- **Fewer tokens only on sparse pages** (forms, letters, slides, pictures with a few labels). A page image costs a fixed ~1,660 tokens. OCR text grows with the words: dense synthetic pages came to 1,300-3,500 tokens. An 8 pt two-column Korean page was ~3,500.
- So this spec never claims a token saving on dense pages. The output rule below decides when the image is still worth sending.

Success:
- **Clean pages.** On a Windows 11 machine with the Korean recognizer and nothing installed, a clean scanned Korean page converts to text at a character error rate (CER) of 3 % or less. English pages come in at 4 % or less with Windows OCR alone, and at 1 % or less with Tesseract on PATH.
- **Reading order.** Two-column pages read column by column. Ruled tables come out as Markdown tables in row order.
- **Doubtful pages.** A page the reader cannot trust goes out as text plus its image, never as text alone. Every noisy page of the spike corpus is flagged.
- **No regression.** With no OCR engine, or for a document without scans or raster pictures, the output is byte for byte today's.

## Decisions (user, 2026-11-19)

1. **Output for a scanned page: "depending on the page".** A trusted page gives text only. A doubtful page gives text plus its image. (Options turned down: text only; text and image always.)
2. **Scope:** scanned pdf pages, lone TIFF scans, and raster pictures inside documents, pdf pages included. Textless djvu is not in scope: it needs IW44 and JB2 decoders first.
3. **Engines: Windows first, Tesseract for Latin pages.**
   - Windows.Media.Ocr reads every page.
   - A page whose result is mostly Latin is read again by Tesseract `eng` when Tesseract is on PATH, and Tesseract's result is used.
   - Off Windows, Tesseract is used when present.
   - The reader never installs an engine. (Options turned down: Windows OCR only; Tesseract only.)
4. **Windows OCR is called through ctypes**, in-process WinRT COM, not through PowerShell.
   - Windows PowerShell 5.1 is the only shell that loads WinRT types, and it is in maintenance.
   - Group-policy execution policy and Constrained Language Mode can block it.
   - A hidden PowerShell spawned by a hook draws EDR attention.
   - The WinRT COM ABI is a stable binary contract, and GDI+ is already called through ctypes (`raster.py`).
5. **The engine choice is measured, not assumed.** RapidOCR/PaddleOCR (a download) and decompiling Windows OCR for a hidden confidence (Ghidra, a download) were left out by the user; the spec works with the two engines on the machine.

## Engines

### Windows: `reader/ocr_win.py`

WinRT through ctypes, no package:
- **Setup.** `RoInitialize(MTA)`, then `RoGetActivationFactory` for `Windows.Media.Ocr.OcrEngine`, `Windows.Globalization.Language`, `Windows.Graphics.Imaging.SoftwareBitmap` and `Windows.Security.Cryptography.CryptographicBuffer`. Strings are HSTRINGs (`WindowsCreateString` / `WindowsDeleteString`). Every interface is called through its vtable, with IIDs pinned in the module.
- **Engine.** `OcrEngine.TryCreateFromLanguage(Language("ko"))` when `ko` is among `AvailableRecognizerLanguages`. Otherwise `TryCreateFromUserProfileLanguages`, and the engine id records which.
- **Input.** Gray8 pixels from the reader's own decoders. `CryptographicBuffer.CreateFromByteArray` turns them into an IBuffer, and `SoftwareBitmap.CreateCopyFromBuffer` into a bitmap. No OS image decoder sees document bytes.
- **Recognize.** `RecognizeAsync` returns an `IAsyncOperation<OcrResult>`. It is polled through `IAsyncInfo.Status` and cancelled with `IAsyncInfo.Cancel` at the deadline.
- **Result.** `TextAngle`, then each line's words with `Text` and `BoundingRect`. Windows OCR gives no confidence.
- **Limit.** The long side is at most `MaxImageDimension` (10,000 on this machine, read at run time).
- **Version id.** The size and mtime of `Windows.Media.Ocr.dll` and of the recognizer's `MsOcrRes.orp`.

### Tesseract: `reader/ocr_tess.py`

- **Where.** `PHRO_READER_TESSERACT` (a path), else `tesseract` on PATH. Found by a PATH scan, not by running it.
- **Call.** `tesseract - stdout -l <langs> --psm <n> -c tessedit_create_tsv=1`, with the image as a PNG the reader encoded, sent on stdin (no temp file) and `OMP_THREAD_LIMIT=1`. Inline TSV is used because the `tsv` config file is often missing from a user's tessdata folder (it was on this machine).
- **Result.** Words with boxes and confidence (0-100).
- **Language data.** `TESSDATA_PREFIX` is passed through as set.
- **Page segmentation mode.** Set by measurement in the plan on the spike corpus, after the reader's own layout (below). Start values: `TESS_PSM_LATIN = 3`, `TESS_PSM_KOR = 4`. The layout is rebuilt from boxes, so the mode matters only for recognition.
- **Version id.** The size and mtime of the executable and of each traineddata used.

### Choosing the engine per page: `reader/ocr.py`

| Machine | Rule |
|---|---|
| Windows with an OCR engine | Windows reads the page. If at least `OCR_LATIN` (0.9) of its letters are Latin and Tesseract with `eng` exists, Tesseract `eng` reads it again and its result is used. If the Windows engine is not Korean and the page's junk share passes `OCR_JUNK`, Tesseract `kor+eng` reads it again when `kor` exists. |
| macOS, Linux, or Windows without an OCR engine | Tesseract `kor+eng` (the languages present among `kor`, `eng`). |
| No engine | No OCR. Output as today. |

`ocr.recognize(images, deadline) -> list[OcrPage]`. An `OcrPage` holds the words (text, box), the page angle, the engine id and the confidences when the engine gives them.

## Layout: reused from pdf

The engine's own line order is dropped. Windows OCR reads a ruled table column by column (spike: every label, then each column of numbers).
- **Lines and paragraphs.** Each word becomes a `pdf_layout.Fragment` in page space (y up). The font size is the box height. Then the pdf path runs as it is: `lines` → `reading_order` (columns) → `paragraphs` → `heading_level`.
- **Ruled tables.** Long horizontal and vertical dark runs are found in the bitmap: a run at least `OCR_RULE` (0.05 of the page side) long and at most 1/100 inch thick. They become `pdf_tables` rule segments, and `pdf_tables` builds the grid and assigns words to cells.
- Borderless tables are out of scope.

## Output

### Scanned pdf pages and lone TIFF pages

What counts as a page to OCR:
- A pdf page found as scanned by today's rule (no characters, one image covering at least 50 %).
- A pdf page that today goes out as a render because its text has no Unicode (`Page N, text without Unicode`).
- Each page of a lone TIFF.
- A pdf page with an OCR layer is read from that layer as today, never OCRed again.

The OCR input:
- pdfium renders the page at `OCR_DPI` (200). The spike gave the same result at 150 and 300 dpi; 200 leaves room for small type.
- Native mode uses the page's own image when it can be decoded. CCITT and JBIG2 pages it cannot decode stay as today.
- A TIFF page is used at its own resolution, scaled down to the pixel cap.
- When the page image is also written, it is the OCR render scaled to today's `RENDER_DPI` and long side, not a second render.
- The OCR render counts against `PDF_RENDERS`.

Each page is judged (next section) and goes out as one of three:

| Verdict | Output |
|---|---|
| trusted | The page's text only: paragraphs, headings, ruled tables. |
| trusted text, with a picture area | The text, then a crop of the area not covered by words, linked as `![Picture on page N](assets/img-NNN.png)`. If that area's box passes `FIGURE_WHOLE` (0.7) of the page, the whole page image is linked instead. |
| doubtful | The text, then the page image `![Scanned page N](assets/img-NNN.png)`. |

- Each OCRed page starts with the line `_Scanned page, text by OCR._`. A pdf page keeps its usual page heading before it.
- The document's notes gain `N scanned pages read by OCR` (singular `1 scanned page read by OCR`). The doubtful ones are counted as `M of them doubtful, shown with the page image`.
- Today's `scan` count ("scanned page not shown") now counts only pages neither OCRed nor shown.

### Raster pictures inside documents

- **What is read.** A PNG, JPEG, BMP, GIF or TIFF Image block, in any format, pdf pages included. The pictures' long side must be at least `OCR_MIN_EDGE` (64 px). WMF, EMF, PICT and WPG are left alone: their text comes from their records (metafiles spec).
- **Output.** The link stays as today, followed by `Picture text: …`. This uses the placement rules of `Figure text:`: its own paragraph after the link, or on the same line in an inline place such as a table cell. It goes inside the hidden tag when the image is hidden. Words are joined in reading order, lines with ` / `, and the text is capped at `FIGURE_CHARS`.
- **No text.** A picture whose result has fewer than `OCR_MIN_WORDS` (2) words, or whose junk share passes `OCR_JUNK`, gets no text. Photos give junk.
- The same bytes twice are read once and repeat their text.

### Doubt signals

A page is doubtful when any signal passes its threshold. The start values below are calibrated in the plan on the spike corpus and the bench's public scanned pdf (target: every noisy page doubtful, at least 90 % of clean pages trusted). The calibrated values are then fixed in `limits.py`.

| Name | Measures | Start |
|---|---|---|
| `OCR_JUNK` | Share of characters that are not Hangul syllables, Latin letters, digits or common punctuation: lone jamo, symbols, CJK compatibility forms. | 0.03 |
| `OCR_UNREAD` | Share of the page's dark pixels outside the word boxes (each grown by a quarter of its height). High means a photo, a drawing, handwriting or text the engine missed. | 0.15 |
| `OCR_CONF` | Mean word confidence, Tesseract pages only. Spike: 92-96 clean, 67-85 noisy. | doubtful under 88 |
| `OCR_LOW` | Share of words under confidence 60, Tesseract pages only. Spike: 0-3 % clean, 13-36 % noisy. | doubtful over 0.08 |

A trusted page whose unread pixels form a picture area: the unread pixels, grouped into one box, cover at least `OCR_AREA` (0.04) of the page. That page takes the crop row of the table above.

## Isolation, input and security

- **A worker of its own.** OCR runs in a worker process (`python -I -X utf8 read.py ocr-worker`), started once per conversion, never inside the conversion process. Requests and results go as length-prefixed JSON frames over stdin and stdout, with the pixels as raw Gray8 bytes.
  - If an engine faults natively, the worker dies, the pages it held go out as images (counted `ocr_failed`), and the document converts normally.
  - At the deadline the worker is killed.
- **Only clean pixels.** Engines receive only pixels the reader decoded itself (pdfium, the reader's own image decoders, GDI+ for TIFF on Windows). Windows OCR gets a `SoftwareBitmap` built from a buffer. Tesseract gets a PNG the reader encoded. A document's compressed image bytes never reach an engine's parser.
- **Pixel cap.** An image over `images._MAX_PIXELS` is scaled down before OCR, never refused.

## Time and compute

- **Deadline.** OCR stops at `deadline − FIGURE_RESERVE` (3 s), the rule figures already use.
  - Pages not read by then go out as images.
  - The notes say `OCR stopped at page N; read the file again to continue` (for pictures: `OCR stopped; K pictures not read`).
  - A conversion cut this way is not filed under the document key (as `FIGURES_LATE`; the warning is `OCR_LATE = "ocr not finished: time"`).
- **Per-item cache.** Results are cached per picture, so a second read continues where the first stopped. A 300-page scan becomes text over a few reads.
- **Parallelism.** `OCR_WORKERS` pages are recognized at once.
  - Windows: concurrent `RecognizeAsync` calls on one engine.
  - Tesseract: one process per page.
  - Start value `min(4, os.cpu_count())`, fixed in the plan by measurement on this machine.
- **Spike timings, per A4 page.** Windows 0.4-1.0 s, Tesseract 1-8 s. A dense pdf of 40 scanned pages fits one 45-second read on Windows.

## The gate and the cache

- **Engine detection at the gate.** The gate checks for the files only; it runs no engine:
  - the Windows build has `Windows.Media.Ocr.dll`;
  - the recognizer folders exist under `%WINDIR%\OCR`;
  - `tesseract` is found on PATH or through `PHRO_READER_TESSERACT`.
- **Document key.** The key gains an OCR backend id, beside the figure backend id: `win-ko`, `win-<lang>`, `tess`, `win-ko+tess`, or empty for none. It also takes the engines' version ids. Installing Tesseract or adding a recognizer language therefore converts a cached document again.
- **Per-picture key.** Results live under `cache.root()/ocr/`, keyed by the SHA-1 of the Gray8 pixels and size, the engine id and version, the OCR code digest and `reader.VERSION`. They are kept and pruned like figures.
- **The cache child.** The Windows cache child keys with the gate's backend id, as it does for figures.

## Messages

| Case | Text |
|---|---|
| Windows engine is not Korean, a page is junk, no Tesseract `kor` | Note: `Korean OCR is not installed in Windows: Settings > Time & language > Language > add Korean with "Optical character recognition".` Once per conversion. The reader never installs it (it needs an administrator). |
| No engine at all | Nothing new; output as today. |
| Worker failed | Note: `OCR failed on N pages; shown as images.` |

## Local caps

| Name | Value | Past it |
|---|---|---|
| `OCR_DPI` | 200 | — |
| `OCR_MIN_EDGE` | 64 px | the picture is not read |
| `OCR_MIN_WORDS` | 2 | no Picture text |
| `OCR_WORKERS` | min(4, CPUs), measured | — |
| `OCR_RULE` | 0.05 of the page side | the run is not a rule |
| `OCR_AREA` | 0.04 of the page | no crop |
| `FIGURE_CHARS` | 20,000 (existing) | Picture text is cut, counted |
| `PDF_RENDERS` | 100 (existing) | the page is neither OCRed nor shown, counted `scan` |
| worker frame | `images._MAX_PIXELS` Gray8 bytes plus 1 MiB of JSON | the item is refused, counted `ocr_failed` |

## Testing

Unit tests (`tests/test_reader_ocr*.py`):

| What | How |
|---|---|
| ctypes Windows OCR | The plan's first task: one fixture page read through ctypes is compared with its known text (CER) and with the PowerShell spike result. Skipped off Windows or without the `ko` recognizer. |
| Layout reuse | Word boxes built by hand, no engine: a two-column page comes out in column order; a table delivered column by column, as Windows OCR does, comes out in row order. |
| Ruled tables | A drawn grid bitmap: its rules are found, and `pdf_tables` builds the Markdown table. |
| Doubt signals | For each of `OCR_JUNK`, `OCR_UNREAD`, `OCR_CONF` and `OCR_LOW`, values just under and over the threshold flip the verdict. The picture-area crop flips to the whole page past `FIGURE_WHOLE`. |
| Engine choice | A stand-in engine module: a Latin page goes to Tesseract `eng`; a junk page on a non-Korean Windows engine goes to Tesseract `kor`. |
| Isolation | The worker is killed mid-page: the document converts, the page is an image, `ocr_failed` is counted. |
| Deadline | A slow stand-in engine: the late pages are images with the continue note. A second read continues from the per-picture cache. |
| Keys | A changed engine version or recognizer language changes the document and per-picture keys. |
| Engine input | Tesseract's stdin is a PNG with the reader's own IHDR, never the document's bytes. |
| Output | `Picture text:` as a block and inline, `(hidden)`, the page's first line, the notes and their singular forms. |
| Regression | With no engine, and for documents with no scans or raster pictures, the output is byte for byte today's. |

Fixtures:
- A few small synthetic pages from the spike, under 1 MB together, go in `tests/fixtures/ocr/` with the script that makes them.
- No real documents and no mutated or hostile bytes.

Bench: an addendum `reports/written/phro-read-ocr.md`.
- **Corpus:** the 30 synthetic pages; the same pages wrapped as scanned pdfs; the bench's public scanned pdf, its pages rendered and our OCR compared with its own OCR layer.
- **Measures:** CER, tokens against the page image, time per page, verdicts.
- **Whole corpus:** no change outside scanned pages and raster pictures.

What cannot be tested here:
- macOS and Linux Tesseract paths run only with stand-in engines.
- A Windows without the Korean recognizer is approximated by a stand-in engine. Adding a recognizer language needs an administrator and a download, so it is not done here.

## Review focus

1. **A scan in landscape or upside down.**
   - Windows OCR corrects small skew itself (`CorrectTextAngle`); a 90° or 180° page is not covered.
   - The reader tries 0°, then 90°, 180° and 270° when the first pass's junk share passes `OCR_JUNK`. It keeps the rotation with the least junk, within the deadline.
   - Tesseract can report the angle with `--psm 0` (osd).
2. **A scanned page with both photo and text,** such as a brochure: the crop must hold the photo and not cut the text.
3. **Huge TIFF scans** (A0 at 600 dpi): scale to the pixel cap before OCR, and stay inside memory.
4. **Handwriting:** neither engine reads it well. It must be flagged doubtful (by `OCR_UNREAD` or junk), never passed off as text.
5. **Mixed Korean and English on one page:** the Latin share stays under `OCR_LATIN`, so Windows keeps it. Check that English words inside Korean text come out right.

## Engine spec edits made with this amendment

| Line | Edit |
|---|---|
| 11 | Add: scanned pages and raster pictures are read by OCR when an engine is present (Windows OCR built in; Tesseract when the user has it). The reader installs no engine. |
| 29 | Add decision 12 (2026-11-19): OCR as specified in `2026-11-19-doc-reader-ocr-design.md`. Scanned pdf and TIFF pages give their text, plus the page image when doubtful. Pictures get `Picture text:`. Windows OCR is reached through ctypes, and Tesseract `eng` handles Latin pages. |
| 115 | Images: raster pictures are followed by their OCR text when an engine finds words. |
| 126 | The cache key also holds the OCR backend id and engine versions. |
| after 159 | Add a row: ocr; scanned pdf pages, TIFF pages and raster pictures in every format; shared parts `ocr`, `ocr_win`, `ocr_tess`, and the `ocr-worker` command. |
| 174 | Out of scope now reads: "OCR of textless djvu (needs IW44 and JB2 decoders), handwriting recognition and borderless tables in scans; diagram structure in pdf, xps and pictures, which have no connector data; …" |

## Out of scope

- **Textless djvu.** It needs IW44 and JB2 decoders first.
- **Handwriting recognition.** Handwritten pages are flagged doubtful, not read.
- **Borderless tables in scans.**
- **Further engines.** RapidOCR/PaddleOCR, macOS Vision, cloud OCR.
- **Installing engines or recognizer languages.**
- **Writing an OCR layer back into a pdf.**
