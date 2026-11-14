# doc-reader phase 2d: the engine reads pdf

2026-11-14. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec), whose decision 7 left pdf with `anthropic-skills:pdf` until this phase; the engine spec edits listed at the end are made in the same commit. Notes behind it, outside this repository: `research/ai-document-reader-plugins/notes/pdf-survey.md` (the survey), `notes/pdf-spike/spike-report.md` (the spike) and `notes/rea-decisions.md` (the rulings).

## Goal

The gate converts pdf. Reading moves here; editing, filling, merging and creating pdfs stay with `anthropic-skills:pdf`.

The gain is the page image. Claude's own Read sends every page as an image plus its text layer: about 2,590 to 2,720 tokens a text page and 1,640 to 1,750 a scanned page on the bench calibration. On the spike's 26 files the text alone cost about 41% of that (560,869 against 1,377,072 calibrated tokens). Dense English pages gain almost nothing (30,486 against 30,805); Korean, table-heavy and sparse documents cost about a third.

Success, as in the engine spec, with these baselines:

- The route is `builtin>pdf`, the bench's model of Claude's own Read: `pdftotext -enc UTF-8` plus a 100 dpi PNG of every page.
- The default engine (pdfium) reaches fidelity of at least the route's minus 0.01, at calibrated tokens of at most the route's plus 5%. Linked images count as viewed, as the bench counts them.
- The stdlib engine (native) is measured and reported against the same bar. A MISS there is recorded with its reason and does not block the phase.
- No software outside the plugin-managed venv. The venv holds pypdfium2 alone and is never created without the user's yes.

## Decisions (2026-11-14)

Decision 4 (no OCR, page images) and the downloads behind the bench corpus are the user's answers. Decision 11 is the user's standing rule. Decisions 1-3 are rulings from the spike (`notes/pdf-spike/spike-report.md`: word recall 0.977 for pypdfium2, 0.898 for dochan's parser, 0.957 for pdf_oxide). The rest are rulings from the survey (`notes/rea-decisions.md`).

1. The default engine is pypdfium2 alone, pinned and hash-checked, in a plugin-managed venv under `${CLAUDE_PLUGIN_DATA}`. pdfplumber and pdf_oxide are not used.
2. Tables come from dochan's table assembly (MIT), ported onto rule segments. pdfium path objects feed it in the default engine.
3. With no venv, a stdlib port of dochan's pdf parser reads the file.
4. A scanned page (no text layer) goes out as a page PNG for Claude to view. There is no OCR in 2d.
5. Filled form values and annotation comments are read. They go at the end of their page.
6. Text in an optional-content group (OCG) that is off by default is hidden and counted.
7. A vector-heavy page is listed, and its figure is linked as a cropped PNG within the image caps.
8. Read's `pages` argument maps to the page index of `out.md`.
9. `SKILL_EXTS` goes away. The edit hint says that editing and creating pdfs stay with `anthropic-skills:pdf`.
10. The engine spec's success condition "no other software installed" becomes "no software outside the plugin-managed venv".
11. Every install needs the user's yes. Setup never runs from a hook.
12. The hook-platform facts the survey could not verify are the plan's first task (see "Hook-platform facts").

## Defaults (proposed with this spec, approved with it)

- **Failure falls back to Claude's own Read.** A pdf that fails as `damaged` or `timeout` is passed through, not denied, because Claude's own Read can open pdfs. `encrypted`, `drm` and `too_large` still deny, and the reason names the pdf skill. A pass-through is remembered for 24 hours, so a file that hangs costs 45 seconds once, not on every Read.
- **pdfium falls back to native.** When pdfium cannot open a file for any reason other than a password, the native parser tries it in the same child. In the spike, native opened the one file pdfium could not open: it has no xref.
- **Hidden text, beyond OCGs.** Three more kinds of text are hidden and counted: invisible text (render mode 3 or 7) with no image under it, text wholly outside the crop box, and text under 1 pt. Invisible text over an image is an OCR layer and is read as normal text.
- **No title, no bold.** `/Info /Title` is not used, because it is often a file name. Bold is not emitted: font weight is unreliable, and bold on every word made pdf_oxide's output 32% larger in the spike.
- **Heading levels.** `##` is the page line, so document headings start at `###`. They come from the outline (bookmarks) where there is one, and from dochan's size rule otherwise. The outline itself is never printed.
- **Field labels.** A field is labelled by its tooltip name (`/TU`) when it has one, else by the last part of its full name. `topmostSubform[0].Page1[0].f1_01[0]` costs tokens and tells Claude nothing.
- **Link schemes.** Only `http`, `https`, `mailto` and `ftp` links become links. Any other link, and an internal jump, keeps its text only.
- **Tables stay on their page.** A table that continues on the next page is not joined, so the page index stays true.
- **The setup offer** is made once per session. `setup --decline` stops it for good.
- **Pages past a cut.** A Read whose `pages` lie past the point where a long document was cut converts that range alone. A Read whose pages were all left unrendered passes through to Claude's own Read, which renders pages itself.

## Design

### Modules and data flow

```
scripts/reader/
  pdfenv.py             venv location, ready marker, verification, engine id (stdlib; the gate imports it for pdf only)
  pdfsetup.py           read.py setup: venv, pip, smoke test, ready marker, pointer file
  requirements-pdf.txt  pypdfium2==5.15.2 with one --hash line per platform wheel
  formats/pdf.py        parse(): opens the file, page loop, deadline, Sections, outline, Doc
  formats/pdf_pdfium.py pdfium front end: page -> PageData
  formats/pdf_layout.py Fragment, lines, spacing, joins, columns, headings, running headers and footers
  formats/pdf_tables.py Segment, rule grid, cells, nested tables
  formats/pdf_spacing.json  dochan's Korean line-break spacing table (512 KB)
  pdfnative/            stdlib parser: page -> PageData
  containers/crypto.py  + AES-256 key schedule, CBC
```

Both front ends emit the same `PageData` for each page:

- `fragments`: text pieces in content order, each with a box, size, font, direction and hidden flag. This is dochan's `Fragment`, so its table code ports unchanged.
- `segments`: horizontal and vertical rule segments in device space (dochan `Segment`).
- `images`: each image's box and either viewable bytes or None.
- `figure`: the union box of non-rule path objects, with their count.
- `links`, `fields`, `comments`, and the char count.
- `render(box, dpi)`: pdfium only; native gives None.

`formats/pdf.py` turns each `PageData` into `Section("page", number=N)`:

- `pdf_layout` builds the blocks: `Para`, `Heading`, `Image`, and `Table` from `pdf_tables`.
- Fields and comments become the section's `comments`.
- After the loop, outline titles promote matching lines to headings, and `running` detects headers and footers for `Doc.headers` and `Doc.footers`.

The parser never writes Markdown.

**pdfium front end** (`pdf_pdfium.py`). This holds the layout code the spike wrapper carried, corrected four times during the spike.
- Chars come from `PdfTextPage`: char boxes, font size, and the text object of each char (render mode, OC mark).
- Surrogate pairs are joined. Form and annotation strings are read as UTF-16LE with the NUL stripped.
- Path objects are walked with `get_objects(filter=[FPDF_PAGEOBJ_PATH])`, form XObjects included. Their segments go through each object's matrix into `Segment`s, using dochan's thin-fill and straight-curve rules.
- Fields: `init_forms()` is called before any page is loaded, then each widget is read with `FPDFAnnot_GetFormFieldName`, `…AlternateName`, `…Value` and `…Type`.
- Links: `FPDFLink_*`. Annotations: `FPDFAnnot_GetSubtype` and `GetStringValue("T" / "Contents")`. The outline: `get_toc()`.
- pypdfium2's pdfium build has no V8 or XFA, so no JavaScript runs.

**Native front end** (`pdfnative/`). This is ported from dochan 1.21.0 at commit `29bb52be2cf40750e0dab25b24d87e33d0fcedd7`.
- Modules: `objects`, `structure`, `filters`, `crypto` (PDF standard security handler R2-R6), `cmap`, `cid_unicode` with its data, the predefined CMap data, `widths`, `core14` with its metrics, `content` with `_show_table`, `annotations` and `images`.
- About 7,300 lines of code plus 8,000 lines of generated data.
- Not ported: `reader.py` (replaced by `formats/pdf.py`), `notes`, `formulas`, `mathml`, `pagination`, `text_tables` and the C accelerators.
- `layout`, `spacing`, `running` and `headings` go to `pdf_layout.py`. `paths`, `tables` and `connected_tables` go to `pdf_tables.py`.
- The spike found four faults to fix in the port:
  - Spacing comes from glyph geometry, not from TJ numbers. This is the shared rule below, and it fixes word recall of 0.11 on letter-spaced newspaper text.
  - The outline is not printed.
  - Vertical CJK is joined column by column.
  - Widget values are read from `/AcroForm /Fields`.

**Layout rules** (shared by both front ends):
- A space goes between two glyphs on a line when the gap is over 0.2 × font size and neither side is a space already.
- A line break inside a paragraph:
  - becomes a space between Latin letters;
  - is dropped between Han or Kana;
  - between Hangul, follows dochan's spacing table.
- A line-end `-` after a letter, followed by a lowercase letter, is dropped and the word is joined.
- Order: content order first. A page where more than 30% of line-to-line steps go up by more than one line height is re-sorted by the XPS column sort (`formats/xps.py` `_lines`, `_paras`).
- Vertical text: columns run right to left, and characters run top to bottom.

**OCG state.** The off set is read from the catalog's `/OCProperties /D` (`/BaseState`, `/ON`, `/OFF`). The native object parser reads it, in both engines.
- A text object inside `/OC /name BDC`, or inside a form XObject whose `/OC` is off, is hidden.
- An `/OCMD` follows its `/P` policy, AnyOn by default. `/VE` expressions count as visible.
- pdfium's page render already honours the default configuration.

### The venv

**Where it is.** `pdfenv.venv_dir()` takes the first of these that is set:
1. `PHRO_READER_PYENV`: a folder, or `off` to force native. The bench and the tests use it.
2. `$CLAUDE_PLUGIN_DATA/pdf-venv`.
3. The folder named in the pointer file `<cache root>/pdf-venv.json`, which setup writes. It lets `read.py convert` find the venv from a shell that does not have `CLAUDE_PLUGIN_DATA`.

The interpreter is `Scripts/python.exe` on Windows and `bin/python3` elsewhere. `-I` keeps the venv's site-packages on the path.

**Ready marker** `<venv>/ready.json`, written last:
- `requirements`: the sha256 of `requirements-pdf.txt`.
- `python`: the version and the base `home` from `pyvenv.cfg`.
- `pypdfium2`: the package version, and `pdfium`: the build number.
- `records`: the sha256 of the `RECORD` file of `pypdfium2` and of `pypdfium2_raw`.

**Engine id.**
- `pdfium-<pypdfium2 version>-<pdfium build>` when the venv is ready.
- `native` otherwise.

**Checks.**
- Cheap, on every pdf Read: the interpreter file exists, `ready.json` parses, its `requirements` hash matches the plugin's current file, and `pyvenv.cfg`'s `home` exists. If they pass, the engine id is pdfium; if not, it is native.
- Full, on a cache miss with pdfium: both `RECORD` files hash to the recorded values, and every file listed in them, the native library included, hashes to its `RECORD` digest. That is about 8.4 MB, some tens of milliseconds. A mismatch makes the engine native, and the offer says the venv needs repair.

A plugin update that changes `requirements-pdf.txt` makes the old venv stale in the same way.

**Setup** (`read.py setup [--venv DIR] [--check] [--decline] [--remove]`):
1. Take a lock file `DIR.lock`, created exclusively. A lock older than 10 minutes is stale.
2. `sys.executable -m venv DIR.tmp-<pid>`, which uses the bundled pip and needs no network.
3. `DIR.tmp…/python -m pip install --no-input --disable-pip-version-check --only-binary :all: --no-deps --require-hashes -r requirements-pdf.txt`. This downloads about 4.2 MB from PyPI and installs 8.4 MB. pip's own configuration (proxy, index) is honoured, so `--isolated` is not passed.
4. Read the versions, then convert an in-memory one-page pdf with `--engine pdfium`, and check its text.
5. Write `ready.json`, rename the folder to `DIR` (an old `DIR` is moved aside and removed), and write the pointer file.
6. Print `READY\t<dir>\t<engine id>`, or `FAIL\t<step>\t<detail>`. On FAIL nothing is left behind, and native stays in use.

The other modes:
- `--check` prints READY, MISSING, STALE or BROKEN, with versions.
- `--decline` writes `<cache root>/pdf-declined`, and the offer stops. A later `setup` removes it.
- `--remove` deletes the venv and the pointer. Claude runs it only when the user asks.

`requirements-pdf.txt` pins `pypdfium2==5.15.2` with the sha256 of each `py3-none` platform wheel: win_amd64, win_arm64, macOS arm64 and x86_64, manylinux x86_64 and aarch64, musllinux x86_64. The plan reads them from PyPI's JSON metadata, which is a web read, not a download. A platform with no wheel gets `FAIL\tpip\tno wheel for this platform`.

**Approval flow.**
1. A pdf Read with no ready venv converts with native, allows, and adds the offer sentence to `additionalContext`. This happens on the first pdf Read of a session (`<cache root>/offers/<sha1(session_id)>`; every time when the event has no `session_id`), and never after `--decline`.
2. Claude asks the user, naming the source, the size and the folder.
3. On yes, Claude runs the exact command from the sentence in Bash, where Bash's own permission prompt applies. If the user says never, Claude runs it with `--decline`.
4. The next pdf Read has a new engine id, so the cache misses and pdfium converts.

Nothing installs from a hook, at session start, or without that command.

**Child process.**
- The gate runs `<interpreter> -I -X utf8 read.py convert <path> --engine pdfium|native [--pages A-B]`, using the venv interpreter for pdfium and `sys.executable` for native.
- The child computes the cache key from the engine it actually used. A failed `import pypdfium2` falls back to native and never files native output under a pdfium key.
- `read.py convert` run by hand without `--engine` re-runs itself under the venv interpreter when the venv is ready.

### Hook-platform facts (the plan's first task)

| Fact | Used by | If false |
|---|---|---|
| 1. `CLAUDE_PLUGIN_DATA` is set in the hook's environment | venv location | `hooks.json` passes `--data "${CLAUDE_PLUGIN_DATA}"` to `read.py hook`, if the command string expands it (the same task checks this). If neither works, the venv lives at `<cache base>/pdf-venv` (`%LOCALAPPDATA%\phro-project`, `~/.cache/phro-project`), and the README says so. In every branch the offer names the resolved folder, and setup writes the pointer, so Claude's Bash never needs the variable. |
| 2. A PreToolUse hook may print `additionalContext` with no `permissionDecision` | the one-line reason on a failure pass-through | The pass-through prints nothing, and Claude's own Read reads the pdf as before 2d. The setup offer and every other pdf sentence ride on an `allow`, so they do not depend on this fact. |
| 3. A SessionStart hook can run asynchronously | nothing | No change. 2d adds no SessionStart hook: install never runs from a hook, and a SessionStart hook would print into sessions that read no document. Recorded so that later work knows. |
| 4. (From the rulings' `pages` item.) `updatedInput` replaces the whole input, and what Read does with `pages` on a `.md` file | `pages` mapping | If the key cannot be dropped and Read ignores `pages` on `.md`, the key stays and `offset`/`limit` still apply. If Read rejects it, a pdf Read with `pages` passes through to Claude's own Read. |

### Gate

`scripts/reader/__init__.py`:
- `FORMATS["pdf"] = "pdf"`, and `BUILT` gets `pdf`. The count goes from 40 to 41.
- `SKILL_EXTS` is deleted.
- `code_digest` hashes `*.json` under `reader/` as well as `*.py`, so a spacing-table change misses the cache.

`gate.py`:
- **Text check.** `looks_like_text` returns False when `%PDF-` occurs in the first 1,024 bytes. A pdf written in ASCII only is no longer passed through as text.
- **Engine and key.** For pdf, `pdfenv.engine()` gives the engine id, and `cache.key(path, engine)` takes it.
- **`pages`.** It is parsed as `N` or `A-B`; any other value is dropped.
  - Within the pages read: `offset` is the line of `## Page A` and `limit` runs to the line before `## Page B+1`. `pages` is removed from the input.
  - Past a cut: `convert --pages A-B`, cached under `<key>-pA-B`, and served whole.
  - Past the last page: `pages` is dropped, and the context says how many pages there are.
  - Every page in the range is unrendered: pass through.
- **Failure.**
  - `damaged`, `timeout` and an internal error pass through. With fact 2 true they add `phro-project could not convert <name> (<reason>); Claude's own Read is used.`
  - A marker `<cache root>/failed/<key>` makes the next 24 hours pass through at once.
  - `encrypted`, `drm` and `too_large` deny. `SKILL_OF["pdf"] = "pdf"` adds "or read it with the pdf skill", since the skill can take a password the user knows.
- **Context.** These are fitted in order under `CONTEXT_MAX` (600):
  1. The existing head, with the hidden count.
  2. ` Pages start at "## Page N" lines. Read-only view; to edit or create a pdf, use the pdf skill.` This is `PDF_HINT`; `EDIT_HINT` stays OOXML-only.
  3. The offer, in native mode only: ` Read by the built-in parser; pypdfium2 5.15.2 (PyPI, about 4 MB) reads tables, scans and forms better. Ask the user whether to install it, and only on their yes run: python -I -X utf8 "<READ_PY>" setup --venv "<DIR>" (or the same with --decline if they never want it).`
  4. ` Pages not shown as images: 3-9; Read the pdf with pages="3-9" to see them.`
  5. ` Vector figures on pages 4, 7 are linked as images.`
  6. The unread list.
  7. For outputs over 2,000 lines, ` Pages at lines: 1:3, 50:2210, …`, every k-th page with the smallest k that fits. This replaces the heading index for pdf.
  8. The garble warning.

`meta.json` gains `engine`, `pages` (`[[n, line], …]` for every page read), `pages_total`, `unrendered` and `figures`.

`markdown._section` writes `## Page N` for a page section with a number, and `## Page N: title` with a title too. XPS sets no number, so its output does not change. `stats` returns the page lines.

### Output

| Content | Out.md | Token notes |
|---|---|---|
| Page | `## Page N` before each page's blocks, empty pages included | about 4 tokens a page |
| Text | paragraphs by the layout rules; no bold, no layout spaces; headers and footers as one line at the top | 41% of Claude's own Read on the spike corpus; about equal on dense English; about a third on Korean, tables and sparse pages |
| Headings | `###` to `######`: outline depth + 2, else dochan's 1.5× / 1.25× median size rule (at most 120 characters) | about 2 a heading |
| Tables | `Table` blocks: GFM pipes, HTML when cells merge; nested tables as in dochan | Korean ruled table (spike): 9,318 against Read's 17,910 |
| Links | `[text](url)` on the glyphs inside the link rectangle | the URL's length |
| Images | `![](assets/img-NNN.png)` in reading order. DCT data is kept as is. Anything else becomes a PNG through the reader's encoder: pdfium's bitmap of the object, or the native decoder for Flate, LZW and RunLength in Gray, RGB or Indexed. Duplicates are written once. Images under 32 px or drawn under 16 pt on a side are left out, uncounted. | about 10 tokens a link; viewing costs ceil(w·h/750) after the long side is cut to 1,568 px |
| Scanned page | no chars, and one image covering at least 50% of the page: `![Scanned page N](assets/img-NNN.png)`. pdfium renders the page at 110 dpi. Native writes the page image when it can decode it. | A4 at 110 dpi is 910×1286, 1,560 tokens when viewed (Claude's own Read: about 1,640-1,750) |
| OCR layer | invisible text over an image covering at least 80% of the page: the text is read, and that image is left out uncounted | text only |
| Vector figure | at least 32 non-rule path objects covering at least 4% of the page: the union box plus 6 pt, rendered at 150 dpi with the long side at most 1,568 px, and linked in place. A box over 70% of the page renders the whole page. A page with no text and only paths is rendered whole. | a half A4 at 150 dpi is about 1,450 when viewed |
| Form fields | `> Field Label: value` at the end of the page, for filled fields only. A check box or radio shows its export value unless Off. Newlines become ` / `. Values are cut at 2,000 characters. Signatures and buttons are skipped. | 8-15 tokens a field |
| Comments | `> Comment by Author: text` after the fields, in file order, replies included. This covers Text, FreeText and the markup kinds that carry `/Contents`. Popups are skipped. A hidden annotation (`/F` bit 2) is hidden. | the text's length |
| Hidden | OCG-off text, invisible text with no image under it, text outside the crop box, text under 1 pt: left out and counted per text object; `--hidden` shows it marked `(hidden)` | the existing first line |

Spike evidence for the choices above:
- The outline matched 8 of 8 headings on two files, where the size rule made an author's name a heading.
- pdfium read the filled values of 6 of 6 fields.
- Scan detection had no false positives in pdfium's char count. pdf_oxide's classifier flagged 43 text pages as scans.

### Limits

These are local caps in `formats/pdf.py` unless a module is named. The times rest on the spike, which measured A without pdfplumber and native on this machine:

| Document | pdfium | native |
|---|---|---|
| 384 dense pages | 6.8 s | 9.8 to 10.3 s |
| 763 sparse pages | 1.5 s | 6.4 to 6.9 s |
| 82 pages, OCR over scans | 4.5 s | 2.4 s |

| Cap | Value | Past it |
|---|---|---|
| `PDF_PAGES` | 5,000 pages read per conversion | the rest counted `page_cut` |
| `PDF_DEADLINE` | 36 s from the child's start for the page loop. Start-up is 0.2-0.3 s (cold imports: pypdfium2 182 ms, native 324 ms), and rendering and writing 20 MB take 3 s at most, so the child stays inside `CONVERT_SECONDS` (45) and the gate inside `HOOK_SECONDS` (55). That is about 2,000 dense pages with pdfium and 1,400 with native. | the rest counted `page_cut`; the output is kept, not timed out |
| `PDF_PAGE_SECONDS` | 4 s for one page, checked between text, tables, images and renders | that page's remaining steps skipped, counted `page_part` |
| `PDF_PAGE_CHARS` | 200,000 chars on one page (a dense page has about 5,000) | counted `text` |
| `PDF_PAGE_OBJECTS` | 100,000 page objects walked on one page, form XObjects included | the walk stops, counted `page_part` |
| `PDF_SEGMENTS` (`pdf_tables`) | 20,000 rule segments on one page (dochan `MAX_SEGMENTS`) | the remaining rules ignored |
| table caps (`pdf_tables`) | dochan's: 50,000 cells a page, 200,000 a document, 2,000,000 intersection checks, 1,000 candidates a page, nesting 32; `TABLE_NODES` applies too | counted `cells` |
| `PDF_PAGE_IMAGES` | 64 images on one page (dochan) | counted `image` |
| `PDF_RENDERS` | 100 renders per conversion, scans and figures together | the page goes on `unrendered`, counted `scan` or `figure` |
| `RENDER_DPI`, `CROP_DPI`, `RENDER_LONG` | 110 dpi, 150 dpi, 1,568 px on the long side | the scale drops to fit |
| `PDF_ANNOTS` | 256 annotations on one page; 65,536 characters of text each (dochan) | counted `annotation` |
| `OUTLINE_ITEMS`, `OUTLINE_DEPTH` | 1,000 items, depth 32 (dochan) | the rest unused |
| `IMAGE_BYTES` (`limits.py`) | 256 MiB, renders included; pdf joins the comment's list | counted `image` |

`pdfnative` keeps dochan's own caps:
- Structure: 32 xref sections, 500,000 objects, resolve depth 32, page-tree depth 64.
- Decoding: 50 MiB decoded per stream and 200 MiB per document.
- Content: 256 content parts and 64 MiB of content a page, form depth 16, 32,768 operators a form.
- Objects: 100,000 items a collection, nesting `MAX_DEPTH`.

The plan's measure task checks the values. 100 renders at 110 dpi must fit in 10 s, and table assembly on the 384-page file in 15 s; a cap is lowered if either does not.

### Hostile input

- **Structure.** Cycles in the page tree, the outline (`/Next`) and `/Parent` chains are cut by seen-sets and the depth caps. An xref loop is cut by the section cap. Object-stream recursion is cut by the resolve depth.
- **Bombs.** Flate, LZW and predictor outputs stop at the decode caps. In pdfium, an image's pixel count is checked before its bitmap is taken: over `images._MAX_PIXELS` (16,777,216), its box is rendered at `CROP_DPI` instead. A page larger than 200 inches renders at the long-side cap. A box with no area, or a coordinate over 1e9, is skipped.
- **Operator floods.** Content-part, operator, char, object and segment caps apply per page, and the deadline applies to the document.
- **Active content.** JavaScript, launch and submit actions are never run. Attachments and portfolios are not read; they are a named item. Links keep only the four schemes above.
- **Prompt injection.** Invisible, off-crop, tiny and OCG-off text are hidden and counted, so planted instructions do not reach Claude by default. Heading text in the context is cleaned by `_heading`, as now.
- **Crashes and hangs.** A pdfium crash or a hang inside a pdfium call kills only the child. The gate sees no output or a timeout, and passes the file through.
- **Encryption.**
  - An empty user password opens in both engines (R2, R4 and R6 in the spike).
  - A user password, or the public-key handler (`/Adobe.PubSec`), is `encrypted`.
  - A security handler that is not `/Standard` (FileOpen, LockLizard and others) is `drm`.

### Errors and LOST counts

Kinds:

| Kind | When | Gate |
|---|---|---|
| `encrypted` | a user password, or the public-key handler | deny |
| `drm` | a security handler that is not `/Standard` | deny |
| `too_large` | over `MAX_INPUT` | deny |
| `damaged` | both engines fail to open the file, or no page is readable | pass through |
| `timeout` | the child runs past its budget | pass through |

A page that raises is skipped under `ctx.guard("page")`.

New `LOST` entries, after `page`:
- `page_cut`: "page past the time or page limit" / "pages past the time or page limit".
- `page_part`: "page read in part" / "pages read in part".
- `scan`: "scanned page not shown" / "scanned pages not shown".
- `figure`: "vector figure not shown" / "vector figures not shown".
- `field`: "form field" / "form fields".
- `annotation`: "annotation" / "annotations".

Kinds that already exist, used as they are:
- `image`, `link` and `cells`.
- `glyphs`: a font with no ToUnicode and no CID mapping.
- `text`: the char cap.

New `NAMED` items:
- `xfa`, "XFA form": `/AcroForm /XFA` is present. Its placeholder text, if any, is read as page text.
- `attachment`, "attached files": `/EmbeddedFiles` or annotation attachments.

### Cache

- `cache.key(path, engine="")` hashes `engine` between the code digest and the bytes, when it is not empty. Keys of other formats do not change.
- `meta.json` records the engine.
- `prune` also removes `offers/` and `failed/` markers older than one day.
- The venv is not under the cache root, so `prune` never touches it.

### README

In section "## Document reader":
- "`pdf` alone is never touched and belongs to `anthropic-skills:pdf` until its phase" becomes a pdf paragraph. It covers:
  - native by default, and the optional pypdfium2 venv: pinned, hash-checked, about 4.2 MB downloaded and 8.4 MB on disk, under `${CLAUDE_PLUGIN_DATA}/pdf-venv`;
  - `read.py setup` and its modes, and that it installs only on the user's yes;
  - scanned pages as images with no OCR, fields and comments, hidden layers and hidden text, and `pages`;
  - editing and creating with `anthropic-skills:pdf`;
  - pypdfium2's licence (Apache-2.0 or BSD-3-Clause; it is installed, not copied).
- `pdf` joins the built list.
- "Standard library only" gains "except the optional pdf engine in its own venv".
- "40 in-scope extensions" becomes 41.
- The pdf bench line is added after the phase.

In section "## Not done yet", 2d is built.

`THIRD_PARTY_NOTICES.md`:
- The dochan `Ported:` line names the pdf modules, with the pinned commit.
- A new section reproduces the Adobe notices that come with the ported data: mapping-resources-pdf and cmap-resources (BSD-3-Clause), the Core 14 AFM notice, and the Adobe Glyph List (BSD-3-Clause).

### Bench

The bench lives in `research/ai-document-reader-plugins/bench`, a local git repository.

- **Harness.**
  - `common.BUILTIN` becomes empty, so `SCOPE` takes pdf. The bench tests and `report.py`'s built-in line change with it.
  - `adapters/baseline.py` skips Calibre when the input already is a pdf.
- **Adapters.**
  - `phro-read` gets pdf with `PHRO_READER_PYENV=off` (native).
  - A new `phro-read-pdfium` runs the reader under a bench venv made by `read.py setup --venv <bench>/engines/phro-read-pdf/venv`, after the user's yes.
  - dochan, xberg, anydoc and markitdown get pdf for peer agreement. docling is optional.
- **Routing.** `reports/written/routing.tsv` gets `pdf	builtin>pdf	dochan`.
- **Bar.**
  - `bar --engine phro-read-pdfium` must PASS: fidelity at least the pick's minus 0.01, and calibrated tokens with linked images counted at most the pick's plus 5%. Text-only and image tokens are listed beside.
  - `bar --engine phro-read` (native) is reported against the same bar.
- **Corpus.** Engine test files already on disk, chosen by feature: two-column, newspaper, ruled, rule-light and borderless tables, images, charts and JPX, bookmarks, filled and empty forms, annotations, R2/R4/R6 encryption, broken and missing xref, scans, an OCR layer, Korean and vertical Japanese, OCG, and 384- and 763-page files. Added to them:
  - the generated Word seed pdf;
  - generated files for what the spike lacked: comment annotations, an OFF-layer pdf, a user-password pdf, and invisible text with no image;
  - Tika's 42 pdfs, downloaded with the user's yes on 2026-11-14 and indexed in bench commit f455ede;
  - the user's Downloads pdfs by doc_id only, also allowed that day. The folder held none, so the corpus has no real pdf yet; `scan_real` picks them up on a later run.
- **Addendum.** `reports/written/phro-read-phase2d.md`.

## Testing

All fixtures are built inside the tests.

- **Fixture builder.** A new `tests/pdf_fix.py` writes tiny pdf bytes with stdlib only.
  - Objects, a classic xref or an xref stream with true offsets, optional Flate.
  - Standard-handler encryption (RC4 R2/R3, AES R4/R6) through `containers/crypto.py`.
  - Helvetica text, CID text with and without ToUnicode, rules, image XObjects (gray Flate and a hand-made JPEG), forms, annotations, outlines and `/OCProperties`.
- **The two engines.**
  - Native tests always run.
  - pdfium tests run the reader in a subprocess under the venv interpreter (from `PHRO_READER_PYENV` or `pdfenv`). Without a venv they skip and print that they skipped.
  - Layout and table tests feed `Fragment`s and `Segment`s directly, so they need neither engine.

**pdf.**
- Spacing by gap.
- Line joins: Latin, Han, Hangul, and the hyphen rule.
- Content order, and the column fallback.
- Vertical CJK.
- Headings from the outline and from size, and the outline never printed.
- Tables: ruled, merged and nested; a rule-light table stays text.
- Images: dedup, the size floor, and JPX to PNG (pdfium).
- Scan pages: rendered (pdfium), extracted (native) and not decodable (native, so unrendered).
- An OCR layer drops its backdrop.
- Vector figure crops, and `PDF_RENDERS`.
- Fields: text, check box, radio, the `/TU` label, empty skipped.
- Comments, replies included; a hidden annotation.
- Links: the four schemes, and `javascript:` dropped to text.
- Hidden: OCG off, OCMD, Tr 3 with and without an image, off-crop and under 1 pt.
- Encryption: R2, R4 and R6 with an empty password; a user password; the public-key handler; a non-Standard handler.
- Broken xref recovered; no xref falls back from pdfium to native.
- Caps:
  - each cap above, with a page tree cycle, an outline cycle, an xref loop and a Flate bomb;
  - `PDF_DEADLINE` and `page_cut` (with an injected clock);
  - `PDF_PAGE_SECONDS` and `page_part`.

**Gate.**
- pdf converts (native).
- With a fake ready venv (`command=`), the engine id goes into the key.
- `pages` maps to `offset`/`limit`, converts a range past a cut, passes through when unrendered, and reports past the end.
- `damaged` and `timeout` pass through, with the 24-hour marker.
- `encrypted` denies and names the pdf skill.
- The offer comes once per session, never after `--decline`, and fits in 600 characters with the hint.
- `PHRO_READER_OFF=pdf`.
- An ASCII-only `%PDF-` file is not text.

**Venv.**
- `pdfenv` reads the location order, a stale or broken marker, and a `RECORD` mismatch, all on folders built in the test.
- `setup` is tested with `pip` swapped for a stub; no test installs anything.

**Changed tests.**
- `test_reader_gate.py:97` (`a.pdf` leaves the pass-through list) and `:591-592`.
- `test_reader_cache.py:259-262` and `:265-268`.
- `test_reader_hwpx.py:64` and `test_reader_zipx.py:35`: 41, and no `SKILL_EXTS`.
- The README and notices checks.
- `test_reader_mutation.py` gets pdf.

## Review focus

The five inputs most likely to bite a user:

1. **A scanned pdf of many pages.**
   - Only `PDF_RENDERS` pages become images, and there is no OCR.
   - In native mode, CCITT and JBIG2 scans cannot be decoded at all. Those pages reach Claude only through the `pages` pass-through, at the same cost as before 2d.
2. **A long pdf, or a `pages` Read past the cut.**
   - The deadline cuts the output (about 2,000 dense pages with pdfium, 1,400 with native).
   - Correct page numbers then rest on the range conversion and on the page index in `meta.json`.
3. **Korean pdfs exported from Hancom or Word with CID fonts.**
   - Without ToUnicode, text becomes `glyphs` or garble.
   - Line-break spacing rests on dochan's table.
   - The spike's word recall is 0.97-0.99 with pdfium; native is lower.
4. **Planted hidden text.**
   - The four hidden-text rules catch invisible, off-page, tiny and off-layer text.
   - White text on a white page is not detected and is read as normal text.
5. **The venv's life.** All of these must leave a working native read and a fresh offer, never a half-installed engine:
   - a Python upgrade that breaks `pyvenv.cfg`'s `home`;
   - a plugin update that changes the pin;
   - a partial install;
   - a session whose Bash has no `CLAUDE_PLUGIN_DATA`.

## Engine spec edits made with this amendment

The three edits asked for, in exact text:

| Line | Now | Becomes |
|---|---|---|
| 11 | `- Every in-scope extension converts through the gate with no other software installed.` | `- Every in-scope extension converts through the gate with no software outside the plugin-managed venv. pdf converts with none at all through its stdlib parser; the optional venv holds only pypdfium2 and is created only with the user's yes (phase 2d).` |
| 144 | `\| 2d \| pdf \| starts with a reverse-engineering note (rea), like phase 1-3's \|` | `\| 2d \| pdf \| `pdfenv` and `read.py setup` (the pypdfium2 venv), `pdfnative/` (dochan's parser), `pdf_layout`, `pdf_tables` (dochan's table assembly), AES-256 and CBC in `crypto`; specified in `2026-11-14-doc-reader-pdf-design.md` \|` |
| 160 | `OCR of scanned pages or images; user passwords and DRM removal; editing or writing documents; pdf until phase 2d; rendering WMF/EMF.` | `OCR of scanned pages or images (a scanned pdf page goes out as a page image); user passwords and DRM removal; editing or writing documents (pdf editing and creation stay with `anthropic-skills:pdf`); rendering WMF/EMF.` |

The edits that keep the rest consistent:

| Line | Edit |
|---|---|
| 5 | After "…xltx and xltm", add "and since decision 8 pdf". The format count becomes 41. |
| 25 | Add decision 8 (2026-11-14). pdf is read by the engine, as specified in `2026-11-14-doc-reader-pdf-design.md`; editing and creation stay with `anthropic-skills:pdf`. |
| 64-66 | Add `pdf.py pdf_pdfium.py pdf_layout.py pdf_tables.py` to `formats/`, and the lines `pdfnative/` and `pdfenv.py, pdfsetup.py`. |
| 78 | After the `prune` row, add the `setup [--venv DIR] [--check] [--decline] [--remove]` command row. |
| 114 | Delete "pdf passes through." |
| 117 | Add the pdf sentence, the native-mode offer and the page index. |
| 118 | A pdf that fails as damaged or timeout passes through to Claude's own Read; the pdf skill is named in a pdf denial. |
| 124 | The key also holds the engine id for pdf. |
| 128 | Add the pdf caps (`PDF_PAGES`, `PDF_DEADLINE`, `PDF_RENDERS`). |
| 132 | Add AES-256 and CBC for the pdf standard security handler. |
| 152-156 | Add the pdf fixture builder, the skip rule for pdfium tests, and the pdf bench bar. |

The bench spec (`2026-11-11-doc-reader-bench-design.md`) gets the same scope change: pdf leaves `BUILTIN`.

## Out of scope

- OCR.
- Tagged-pdf structure: reading order, alt text and headings from tags.
- Formula and MathML to TeX.
- Linking footnotes.
- Joining tables across pages.
- Borderless tables (dochan `text_tables`, which is off by default there too).
- Page labels.
- Attachments and portfolios.
- Rendering XFA.
- Digital signatures.
- White-text detection.
- Editing, filling, merging and creating pdfs.
- User passwords.
- pdfplumber, pdf_oxide and uv.
