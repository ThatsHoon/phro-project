# doc-reader bench: test every document engine before building the reader

## Goal

phro-project will get a built-in document reader: a gate that notices when Claude is about to read a document, turns any of 33 formats into text Claude can read, and spends fewer tokens than reading the original. Which engines it calls is decided by measurement, not by the makers' READMEs. This spec covers that measurement only, stages 1 to 3 of the user's request:

1. Clone every engine and helper found in the earlier survey.
2. Install and test each one on this machine, downloading whatever it needs.
3. Write a verification report per format family.

Stage 4, the plugin feature itself, gets its own brainstorming, spec and plan once the reports exist. Its inputs are the summary report, the adapters and the output format defined here.

Success:

- Each of the 33 extensions has at least one test document, or its report says why not and where one could come from.
- Each engine is installed, or recorded as failed with its error.
- Each engine and format it claims has a run, or a recorded failure type.
- Six family reports and one summary exist, in Korean, and every number in them points to a row of `scores.jsonl` and a run folder.
- Token calibration ratios exist for Korean text, English text, tables, images, text PDF pages and scanned PDF pages.
- No real document's name or content enters this repository.

## Decisions (user, 2026-11-11)

1. Test documents come from three sources: generated with known answers, public test files, and the user's real work documents.
2. Scoring is local and automatic, plus a small number of real Claude measurements. Claude answers questions only for the top two engines per family, on real documents.
3. Reports: one per format family, six in all, plus one summary.
4. Method: one harness, one adapter per engine, each engine in its own environment.
5. Specs and plans live in this repository on the branch `doc-reader-bench`. The bench itself lives outside it.

Carried over from the survey: processing stays on this machine, and the reader is used from Claude Code directly.

## Machine

Windows 11, no NVIDIA GPU, about 15 GB RAM, 131 GB free on C:. Present: Python 3.12, Node 24, uv, git, winget, poppler (`pdftoppm`), Tesseract. Absent: LibreOffice, Calibre, Java, Rust, Docker, WSL.

The bench runs here and not in Linux, because the reader will run here.

## Formats

| Family | Extensions |
|---|---|
| Word processing | hwp, hwpx, doc, docx, docm, odt, pages, rtf, wpd |
| Spreadsheet | xls, xlsx, xlsm, xlsb, ods, numbers |
| Presentation | ppt, pptx, ppsx, pptm, odp, key |
| Fixed page and e-book | pdf, xps, epub, mobi, azw3, djvu |
| Database | accdb, mdb |
| Other office | vsd, vsdx, pub, mpp |

## Layout

```
C:\Users\hunje\research\ai-document-reader-plugins\bench\
  harness\              bench.py with subcommands setup, gen, run, score, calibrate, qa, report
    adapters\<engine>.py
    gen\<family>.py
    score.py, tokens.py
  tools\                portable helpers: LibreOffice, Calibre, Java (Temurin)
  engines\<engine>\     the clone, and that engine's own .venv or node_modules
  engines\_gen\         environment for document generators (python-docx, openpyxl, python-pptx and similar)
  engines\_tok\         environment for the local tokenizer
  corpus\
    generated\<family>\ documents plus truth.json
    public\<family>\    files plus sources.tsv (URL, license)
    real.tsv            paths of the real documents, which are not copied
  runs\<engine>\<doc>\  out.md, assets\, meta.json, stderr.log
  scores.jsonl
  engines.tsv           version, commit, license, install size, install method, status
  reports\
```

The harness uses the standard library only. Everything an engine needs stays in that engine's environment, and generators and the tokenizer get environments of their own.

## Adapters

Each adapter declares the formats its engine claims, the setup steps, how to read its version, and its convert command. The harness runs the convert command as a separate process in the engine's environment, passes paths as arguments, and collects every result in one shape:

- `out.md`: the text Claude would read.
- `assets\`: images and chart images the engine extracted, linked from `out.md`.
- `meta.json`: engine, version, document, seconds, peak memory, exit code, failure type.

Formats an engine does not claim are not run; they are recorded as unsupported.

Every engine is cloned, pinned to a commit. The clone serves two uses: reading the source for behaviour and limits, and taking the engine's own test files into the public corpus. Engines run from their released package (pip, npm, a release binary) where one exists, since building from source would need toolchains this machine lacks, such as Rust for Xberg. Only an engine with no release is installed from source.

A tool that only speaks MCP is driven by a small stdio JSON-RPC client in the harness.

A pipeline counts as an engine. A format that needs conversion first runs as, for example, `libreoffice>docx>dochan` or `calibre>epub>docling`, scored like any other engine.

Claude Code's own `Read` is an engine too, the baseline: PDFs and images read as they are. Formats it cannot read are marked so.

## Engines

| Kind | Members |
|---|---|
| Document engines | Xberg, dochan, kordoc, hwp-mcp, Docling, MarkItDown, Marker, MinerU, OpenDataLoader PDF, anydoc, pymupdf4llm |
| Helpers | LibreOffice headless, Calibre `ebook-convert`, MPXJ, mdbtools, negokaz excel-mcp |
| Baseline | Claude Code `Read` |

Known risks, checked at setup and reported, not worked around in silence:

- hwp-mcp may need Hancom Office installed. Without it the engine is recorded as not testable.
- Marker and MinerU assume a GPU. They run on CPU on a small sample with the long time limit.
- mdbtools has no clean Windows build. If it fails, the Access Database Engine ODBC driver is tried, then the format is reported as uncovered.
- Cloud options stay off, such as anydoc's Firecrawl route.

## Corpus

### Generated, with known answers

One seed of content per family, written in Korean and English, rendered into every format that can be produced here, so that formats are compared on the same content. Answers go to `truth.json`.

| Family | Seed holds |
|---|---|
| Word processing | heading levels, paragraphs, a table with merged cells, lists, footnotes, header and footer, an image carrying known text, a native chart with known series, an equation |
| Spreadsheet | several sheets, formulas and their values, merged cells, date and currency formats, a chart, a hidden sheet, a 10,000-row sheet for partial reads |
| Presentation | titles, text boxes, a table, a chart, an image, speaker notes, grouped shapes |
| Fixed page and e-book | a text PDF, the same PDF as page images only (OCR test), a two-column layout, a PDF over 100 pages (token test), e-book chapters |

Produced by:

- Python libraries in `_gen`: docx, xlsx, pptx.
- LibreOffice: doc, docm, odt, rtf, xls, xlsm, ods, ppt, pptm, ppsx, odp, pdf.
- Calibre: epub, mobi, azw3.
- Only if a working tool is found: hwpx (an HWPX writer library) and djvu (a PDF to DjVu converter).
- The scanned PDF: `pdftoppm` pages rebuilt into an image-only PDF.

### Public test files

For formats nothing here can write: hwp, pages, numbers, key, wpd, xlsb, xps, accdb, mdb, vsd, vsdx, pub, mpp, and hwpx or djvu if generation fails. Sources: the cloned engines' test files, Apache Tika's test documents, the samples of HWP parsers (pyhwp, hwplib, hwp.js), and MPXJ's test files. Each file gets its URL and license in `sources.tsv`. Strings an engine's own tests expect from a file become partial answers for it.

### Real documents

About 28 work documents from the user's Downloads folder: HWP, HWPX, DOCX, XLSX, KEY and about 20 PDFs. Personal files were left out. Their paths are listed in `real.tsv`, outside this repository; they are read in place. Their content leaves the machine only when Claude reads it during calibration and question answering, which is the same as the user's normal use.

## Scoring

### Fidelity on generated documents (no model)

Answers and outputs are normalized first: whitespace, Unicode form, full and half width.

| Item | Measure |
|---|---|
| Text | share of answer paragraphs found in the output at a `difflib` ratio of 0.9 or more, and their order |
| Tables | Markdown and HTML tables parsed from the output; share of cells right in position and value; merged cells |
| Charts | series names, categories and values present as text or table. A chart left only as an image scores 0 and is flagged "image only" |
| Images | count extracted, position marked in the text, text inside the image recovered (bonus) |
| Structure | heading levels, lists, footnotes, speaker notes, hidden sheets, formulas and values. A header repeated on every page costs points as wasted tokens |
| Format-specific | database: tables, columns, rows. mpp: tasks, durations, dependencies |

A document's fidelity is the mean of the item scores that apply to it, from 0 to 1; items the document lacks are skipped.

Public files: share of expected strings recovered, or agreement between engines when there are none.

### Fidelity on real documents

- Agreement: the content most engines share is the reference, and each engine's coverage of it is measured.
- Questions: for each of about 14 real documents (every non-PDF one plus about 6 PDFs chosen for tables, charts and scans), the main session writes 5 to 8 questions with answers from the original, viewed as page images (non-PDF formats rendered to PDF by LibreOffice first). They cover text facts, table cells, chart values and trends, image content and context across sections. Then `claude -p` answers them from one engine's output alone, for the top two engines of each family, about 28 runs. The database and other-office families have no real documents, so they get no questions. A script grades numbers and exact strings; the main session grades free-form answers as right, partial or wrong.

### Tokens

- Estimate: text through a local tokenizer (tiktoken `o200k_base`, in `_tok`), images through the image size formula. A document's cost is `out.md` plus the images Claude would need to view.
- Calibration: pairs of headless `claude -p --output-format json` runs on the model the user works with (Opus 5.5 at the time of writing), one that reads a file and one that does not; the difference in input tokens, cache tokens included, is the file's real cost. About 20 pairs across Korean text, English text, table-heavy Markdown, images of several sizes, text PDF pages and scanned PDF pages. Each category gets a ratio, applied to every estimate.
- Baseline: the original read by Claude Code's `Read`. For PDFs that is page images plus text.

### Ranking

Fidelity and tokens stay two axes; there is no blended score. First place per format goes to the highest fidelity; within 0.03, the fewer tokens; then the lighter install (no Java, GPU or large model). License, install size, Windows behaviour and time are recorded, not scored.

## Run

| Step | Does | Asks the user |
|---|---|---|
| `setup` | clone, environment, install, a smoke test on a small document; fills `engines.tsv` | no; results reported |
| `gen` | generated corpus, public downloads, `real.tsv` | no |
| `run` | every engine and document it claims | no |
| `score` | local scoring | no |
| `calibrate`, `qa` | headless Claude runs | yes: an estimate from the actual document sizes, before any run |
| `report` | the seven reports | no |

- One conversion at a time, so time and memory are comparable. Long steps run in the background, with one line to the user per stage.
- Limits: 300 seconds per document, 900 for the GPU-oriented engines (Marker, MinerU, Docling's VLM). A Windows job object caps each conversion's process tree at 12 GB and reports its peak.
- A run whose `meta.json` exists for the same engine version is skipped, so a stopped run resumes.
- Expected size: several hundred conversions over a few hours; 30 to 40 GB of disk, mostly CPU torch environments and model weights, with uv's cache shared between environments.
- Claude usage: about 40 calibration runs and about 28 question runs.

## Errors

Each failure is typed in `meta.json`: install failed, unsupported, timeout, memory limit, crash, empty output, garbled text. A failed run scores 0, and the report gives the failure rate and a representative error line.

A failed install is retried once by another route, for example from source after pip fails. If it still fails, the engine is recorded as failed and the rest go on.

## Safety and privacy

- Clones and downloads are untrusted data. Bench code lives only in `harness\`. Engines run through their own command line with paths as arguments, and no bench code runs from inside an engine folder.
- Downloads happen at setup (packages, model weights) and corpus building (public files). No engine's cloud feature is turned on.
- Portable builds come first. winget is used only for LibreOffice, Calibre or Java when a portable build fails, and the user is told before an administrator prompt appears.
- Quotes from real documents appear only in `bench\reports\`. Specs and plans in this repository say "N real documents" and nothing more.

## Reports

Six family reports, in Korean, in the same order:

1. Conclusion: per extension, first and second engine, fidelity, tokens as a share of the baseline, time.
2. Per extension: test documents and sources; a results table per engine with each fidelity item, tokens, time and failure type; failure examples quoting the original and the output side by side; how each engine keeps charts and images.
3. Question results for the top two engines.
4. Engine notes: install, license, Windows issues, runtimes needed.
5. Gaps: formats without test documents, what was not measured, and why.

The summary report feeds stage 4:

- A routing table of 33 rows: first engine, fallback, pre-conversion, runtime needed.
- The smallest set of engines that covers all 33 formats, and the fidelity it gives up against the best engine per format.
- Token savings, calibrated.
- Whether charts and images should become data tables or stay image files Claude opens only when needed.
- Install weight and license terms (AGPL and GPL included).
- Questions stage 4 must decide: whether the gate is a hook in front of `Read`, where converted files are cached, whether page-range reads are offered, whether engines install on first use or up front.

## Out of scope

- Building the reader (stage 4).
- GPU runs.
- Any engine's cloud feature.
- Scoring an engine on a format it does not claim.
- A single blended score.

## Handoff to stage 4

The summary report, the adapters, the `out.md` shape and the scores. Stage 4 starts a new brainstorming that respects phro-project's always-on cost rules and its original and per-project copy structure, and gets its own spec and plan approved.
