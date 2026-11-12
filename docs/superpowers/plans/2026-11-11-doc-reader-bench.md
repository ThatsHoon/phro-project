# Doc-reader Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bench on this Windows machine that installs every document engine from the survey, converts a three-source corpus covering 33 formats, scores fidelity and tokens, and writes six Korean family reports plus a summary for stage 4.

**Architecture:** A standard-library harness (`harness/bench.py`) drives one adapter per engine. Each conversion runs as a separate process in that engine's own environment, inside a Windows job object that enforces time and memory limits, and lands as `out.md`, `assets\` and `meta.json`. Scoring, token estimation, Claude calibration and reports read those folders only.

**Tech Stack:** Python 3.12 stdlib (harness, via `ctypes` for job objects); uv and npm for engine environments; LibreOffice headless and its bundled Python (UNO) for document generation; git.

**Spec:** `docs/superpowers/specs/2026-11-11-doc-reader-bench-design.md`

## Global Constraints

- Bench root `BENCH = C:\Users\hunje\research\ai-document-reader-plugins\bench`, overridable by env `BENCH_ROOT` (tests use a temp dir). It is its own local git repository, never pushed. Tracked: `harness/`, `tests/`, `engines.tsv`, `scores.jsonl`, `corpus/index.tsv`, `corpus/qa/`, `reports/`. Ignored: `engines/`, `tools/`, `runs/`, `cache/`, `corpus/generated/`, `corpus/public/`, `*.log`.
- The phro-project repository receives only this plan and the spec, on branch `doc-reader-bench`. Its uncommitted `scripts/clone.py` and `tests/test_clone.py` belong to the user: never stage, commit or revert them.
- Harness: stdlib only, run as `python -I -X utf8 harness/bench.py <command>`. Tests are scripts: `python -I -X utf8 tests/test_<x>.py` prints `pass` and exits 0.
- Nothing under `engines/` is imported by harness code. Engines run through their own command line, with paths as arguments, `cwd` set to the run folder.
- Caches stay under `BENCH/cache`: `UV_CACHE_DIR`, `HF_HOME`, `TORCH_HOME`, `MODELSCOPE_CACHE`, `npm_config_cache`, set by the runner for every engine process.
- Limits: 300 s per document; 900 s for `marker`, `mineru` and `docling-vlm`; 12 GB (12288 MB) per conversion's process tree.
- Run folder: `runs/<engine>/<doc_id>/` holds `out.md`, `assets/`, `meta.json`, `stderr.log`. `meta.json` keys: `engine, version, doc, ext, family, seconds, peak_mb, exit_code, failure, error_line`.
- Failure types, exact strings: `install_failed, unsupported, timeout, memory_limit, crash, empty_output, garbled`.
- Text match: `difflib.SequenceMatcher(None, a, b).ratio() >= 0.9` on normalized text. Normalization: NFKC, whitespace runs to one space, strip.
- A document's fidelity is the mean of the item scores that apply to it, 0 to 1; items it lacks are skipped. First place: highest fidelity; within 0.03, fewer tokens; then lighter install.
- No engine's cloud feature is enabled (anydoc's Firecrawl route stays off). Downloads happen only in `setup` and corpus building. Portable builds first; winget only as fallback, and the user is told before any administrator prompt.
- Real documents are read in place, never copied. Their names and content never enter the phro-project repository. Quotes from them appear only in `BENCH/reports/`.
- Every `claude -p` batch (`calibrate`, `qa`) prints its estimate and runs only with `--approved`, which the main session passes after the user says yes. Model `claude-opus-5-5`.
- Scope change (spec, "Scope change"): `BUILTIN = {pdf, docx, pptx, xlsx, xlsm}` are read by Anthropic's built-in skills and are not benchmarked: no index rows, no runs, no questions; they appear only as five routing rows in `summary.md`. The `claude-read` baseline is replaced by built-in route baselines `builtin>docx` (LibreOffice to docx, then `pandoc -t markdown`), `builtin>pptx` (LibreOffice to pptx, then the pptx skill's `slide_text.py` under `_gen`), `builtin>xlsx` (LibreOffice to xlsx, then `markitdown`) and `builtin>pdf` (Calibre or PyMuPDF to pdf, then `pdftotext` plus `pdftoppm -r 100` page images). `tools.py` adds portable pandoc. An engine whose formats are all in `BUILTIN` is cloned only, `not_testable`.
- Reports are Korean; code, comments and working files are English. Files are UTF-8 without BOM, LF; write them with the Write tool or Python, never PowerShell `Set-Content`/`Out-File`.

## Review Focus

1. Real file names with Hangul, spaces and parentheses, such as `(계획서)_자료 (1).hwp`, passed to engines on Windows: argv arrives intact and stdout decodes (UTF-8 first, then cp949). Test in Task 1.
2. A conversion whose child spawns its own children (LibreOffice starts `soffice.bin`) and then hangs: the timeout kills the whole tree and no process survives. Test in Task 1.
3. An engine that writes images next to the source, into its cwd, or links them by absolute path: the run folder ends up self-contained, with `assets/` links relative. Test in Task 2.
4. Duplicate real files (`x.pdf`, `x (1).pdf`, `x (2).pdf`): one corpus entry per content hash. Test in Task 5.
5. Output in the wrong code page (Korean read as cp1252, or U+FFFD runs): typed `garbled`, not scored as text. Test in Task 1.

---

### Task 1: Bench repo, `common.py`, `runner.py`

**Files:**
- Create: `BENCH/.gitignore`, `harness/bench.py`, `harness/common.py`, `harness/runner.py`
- Test: `tests/test_runner.py`, `tests/test_common.py`

**Interfaces:**
- Produces (`common.py`): `BENCH: Path`; `FAMILIES: dict[str, list[str]]` with keys `word, sheet, slides, pages, db, office` and the spec's Formats table in order; `family_of(ext: str) -> str`; `normalize(text: str) -> str`; `read_tsv(path) -> list[dict]`, `write_tsv(path, rows: list[dict], fields: list[str])`, `read_jsonl(path) -> list[dict]`, `append_jsonl(path, row: dict)`; `sha1_file(path) -> str`; `engine_env() -> dict[str, str]` (os.environ plus the cache variables).
- Produces (`runner.py`): `@dataclass RunResult(exit_code: int | None, seconds: float, peak_mb: float, stdout: str, stderr: str, failure: str | None)`; `run_isolated(cmd: list[str], cwd: Path, timeout_s: int, mem_mb: int = 12288, env: dict | None = None) -> RunResult`; `classify_output(md: str) -> str | None` returning `empty_output`, `garbled` or `None`.
- `bench.py` dispatches subcommands by name to functions later tasks add; unknown command prints `REFUSED\tunknown command` and exits 1.

- [ ] **Step 1: Write the failing tests.**
  - `test_common.py`: `sum(len(v) for v in FAMILIES.values()) == 33` with no duplicates; `family_of("HWPX") == "word"`; `normalize("Ａ  b\n\tc ") == "A b c"`; TSV and JSONL round-trip a row holding Hangul and a tab-free string.
  - `test_runner.py`, each with a tiny Python child script written to a temp dir:
    - argv: a child that prints its first argument gets `"(계획서)_자료 (1).hwp"` back exactly in `stdout`.
    - cp949: a child writing cp949 bytes of `한글` decodes to `한글`.
    - timeout: a child that starts a grandchild sleeping 60 s and then sleeps itself, with `timeout_s=2`, returns `failure == "timeout"` in under 10 s, and the grandchild's pid is gone afterwards.
    - memory: a child allocating 10 MB chunks up to 400 MB under `mem_mb=200` returns `failure == "memory_limit"`.
    - crash: a child exiting with code 3 returns `failure == "crash"`, `exit_code == 3`.
    - `classify_output("")` is `empty_output`; text that is 2 % U+FFFD is `garbled`; `"ìëíì¸ì"` repeated (Hangul read as cp1252) is `garbled`; a plain Korean paragraph is `None`.
- [ ] **Step 2: Run** both test files. Expected: FAIL (no modules).
- [ ] **Step 3: Implement.** `run_isolated` starts the process with `subprocess.Popen`, assigns it to a job object at once (`CreateJobObjectW`, `SetInformationJobObject` with `JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `AssignProcessToJobObject`), kills with `TerminateJobObject` on timeout, and reads `PeakJobMemoryUsed` from `JobObjectExtendedLimitInformation`. A run that fails with a peak at 95 % of the cap or more is `memory_limit`. `classify_output`: `empty_output` below 20 normalized characters; `garbled` when U+FFFD exceeds 1 % of characters, or characters in U+00C0 to U+00FF reach 5 % while Hangul is under 1 %. `.gitignore` as in Global Constraints; `git init` in BENCH.
- [ ] **Step 4: Run** both test files. Expected: `pass`.
- [ ] **Step 5: Commit** in BENCH: `git add .gitignore harness tests` and `git commit -m "bench: common helpers and isolated runner"`.

### Task 2: Adapter contract, `run` command, MCP client

**Files:**
- Create: `harness/adapters/__init__.py`, `harness/adapters/base.py`, `harness/run.py`, `harness/mcp_client.py`
- Test: `tests/test_run.py`, `tests/test_mcp.py`, `tests/fixtures/fake_engine.py`, `tests/fixtures/fake_mcp.py`

**Interfaces:**
- Consumes: `run_isolated`, `classify_output`, `read_tsv`, `BENCH` from Task 1.
- Produces (`base.py`): `@dataclass Adapter(name: str, kind: str, formats: frozenset[str], repo: str | None, install: list[list[str]], install_alt: list[list[str]], version_cmd: list[str], convert: Callable[[Path, Path], list[list[str]]], long_timeout: bool = False, post: Callable[[Path], None] | None = None, weight: int = 0, sample: int | None = None)`. `kind` is `engine`, `pipeline` or `baseline`. `sample` caps the documents per extension (generated first, then real, then public, smallest file first); it is 3 for `marker`, `mineru` and `docling-vlm`, which the spec runs on CPU on a small sample. `convert(src, run_dir)` returns one or more commands run in order (pipelines return two). `post(run_dir)` turns engine-native output (JSON, HTML) into `out.md`. `weight` counts Java, GPU orientation and model weights over 1 GB, for ranking. `env_dir(a) -> Path` is `BENCH/engines/<name>`.
- Produces (`adapters/__init__.py`): `REGISTRY: dict[str, Adapter]`, `register(a: Adapter) -> Adapter`; importing the package imports every module in it.
- Produces (`run.py`): `run_one(a: Adapter, doc: dict, force: bool = False) -> dict` (the meta) and `run_all(engines: list[str] | None, docs: list[str] | None, force: bool) -> list[dict]`; `doc` rows come from `corpus/index.tsv` (Task 5) with keys `doc_id, family, ext, source, path, truth`. Subcommand `run [--engine E ...] [--doc D ...] [--force]`.
- Produces (`mcp_client.py`): `call_tool(cmd: list[str], tool: str, arguments: dict, timeout_s: int, cwd: Path) -> str`, JSON-RPC 2.0 over newline-delimited stdio: `initialize` (protocolVersion `2025-06-18`), `notifications/initialized`, `tools/call`; returns the joined text content; raises `RuntimeError` with the server's error message.

- [ ] **Step 1: Write the failing tests.**
  - `test_run.py` registers a fake adapter whose command runs `fake_engine.py <src> <run_dir> <mode>`:
    - mode `ok` writes `out.md` with an image link to an absolute path outside the run folder and a second image into its cwd's parent: after `run_one`, both images are under `assets/`, `out.md` links them as `assets/<name>`, and `meta.json` has every key from Global Constraints with `failure == None`.
    - a doc whose ext the adapter does not claim gives `failure == "unsupported"` without starting a process.
    - a second `run_one` on the same doc and version returns the stored meta without running (the fake engine appends to a counter file; count stays 1); `force=True` runs again.
    - mode `empty` gives `failure == "empty_output"`; mode `crash` gives `crash` and `error_line` equal to the last non-empty stderr line.
    - an adapter with `sample=1` and three documents of one extension: `run_all` runs one, the generated one.
  - `test_mcp.py`: `fake_mcp.py` implements `initialize`, `tools/list` and `tools/call` for a tool `read_sheet` echoing its `path` argument: `call_tool` returns that path; an error response raises `RuntimeError` holding the server's message.
- [ ] **Step 2: Run** both. Expected: FAIL.
- [ ] **Step 3: Implement** `base.py`, `__init__.py`, `run.py`, `mcp_client.py`. `run_one` uses the 900 s limit when `long_timeout`, writes `stderr.log`, and applies `classify_output` to `out.md` when the process succeeded. `run_all` runs one conversion at a time.
- [ ] **Step 4: Run** both. Expected: `pass`.
- [ ] **Step 5: Commit** `bench: adapter contract, run command and MCP client`.

### Task 3: `setup`: helper tools, helper environments, every engine installed

**Files:**
- Create: `harness/setup.py`, `harness/tools.py`
- Modify: `harness/bench.py` (subcommand `setup [--engine E ...] [--tools]`)
- Test: `tests/test_setup.py`
- Output: `BENCH/engines.tsv`

**Interfaces:**
- Consumes: `REGISTRY`, `Adapter`, `run_isolated`, `write_tsv`.
- Produces: `setup_engine(a: Adapter) -> dict` with `engines.tsv` fields `name, kind, repo, commit, version, license, install_route, install_mb, weight, status, error`; `install_route` is `primary` or `alt`; `status` is `ok`, `install_failed` or `not_testable`. `install_tools() -> list[dict]` (same fields) for `libreoffice`, `calibre`, `java`. `tool_path(name: str) -> Path` where `name` is `soffice`, `ebook-convert` or `java`, and `lo_python() -> Path` for LibreOffice's bundled `python.exe`.

- [ ] **Step 1: Write the failing test** `test_setup.py`: a fake adapter whose `repo` is a local bare git repo made in the test and whose install creates a uv venv with no packages: row has `status == "ok"`, a 40-char `commit`, `license` read from the repo's `LICENSE` first line, `install_mb > 0`. A fake adapter whose primary install exits 1 and alt route succeeds: `install_route == "alt"`. One whose both routes fail: `status == "install_failed"` and `error` is the last stderr line.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement `setup.py`.** Clone `git clone --depth 1 <repo> engines/<name>/src`, record `git rev-parse HEAD`. Python engines: `uv venv engines/<name>/.venv --python 3.12` (an engine that refuses 3.12 gets the version its package metadata asks for), then `uv pip install --python <venv python> <package>`. Node engines: `npm install --prefix engines/<name>/node <package>`. `install_mb` is the size of `engines/<name>`.
- [ ] **Step 4: Run** the test. Expected: `pass`.
- [ ] **Step 5: Implement `tools.py`.** Portable first, into `BENCH/tools/<name>`: LibreOffice from the official MSI by administrative extract (`msiexec /a <msi> /qn TARGETDIR=<dir>`), Calibre from its official portable installer, Java as the Eclipse Temurin 21 JRE zip. If a portable route fails, `winget install --scope user`, then machine scope after telling the user an administrator prompt is coming. Then build the helper environments: `engines/_gen` (python-docx, openpyxl, python-pptx, img2pdf, pillow) and `engines/_tok` (tiktoken; first run downloads `o200k_base` into `BENCH/cache`).
- [ ] **Step 6: Register a stub adapter per engine** in `harness/adapters/<name>.py`, with `repo`, `install`, `install_alt` and `version_cmd`; `formats` stays empty and `convert` raises `NotImplementedError` until Task 6 reads them from the clone. Engines and repos:

  | name | repo | install |
  |---|---|---|
  | xberg | github.com/xberg-io/xberg | release package (pip or binary) |
  | dochan | github.com/illuwa/dochan | pip `dochan` |
  | kordoc | github.com/chrisryugj/kordoc | npm `kordoc` |
  | hwp-mcp | github.com/treesoop/hwp-mcp | per README; `not_testable` with the reason if Hancom Office is required and absent |
  | docling, docling-vlm | github.com/docling-project/docling | pip `docling` (one env, two adapters) |
  | markitdown | github.com/microsoft/markitdown | pip `markitdown[all]` |
  | marker | github.com/datalab-to/marker | pip `marker-pdf`, CPU torch |
  | mineru | github.com/opendatalab/MinerU | pip per README, CPU pipeline backend |
  | opendataloader | github.com/opendataloader-project/opendataloader-pdf | pip `opendataloader-pdf` (needs Java) |
  | anydoc | github.com/firecrawl/anydoc | per README, Firecrawl unset |
  | pymupdf4llm | the source repository its PyPI page lists | pip `pymupdf4llm` |
  | numbers-parser | github.com/masaccio/numbers-parser | pip `numbers-parser` |
  | mpxj | github.com/joniles/mpxj | pip `mpxj` (needs Java) |
  | mdbtools | github.com/mdbtools/mdbtools | a prebuilt Windows build; alt: Access Database Engine ODBC driver with `pyodbc` |
  | excel-mcp-negokaz | github.com/negokaz/excel-mcp-server | npm `@negokaz/excel-mcp-server` |
  | excel-mcp-haris | github.com/haris-musa/excel-mcp-server | pip `excel-mcp-server` |
  | olmocr | github.com/allenai/olmocr | cloned only; `not_testable`: needs a GPU |

  The last four rows of the survey (numbers-parser, both Excel MCP servers, olmocr) were archived by the survey but missing from the spec's engine table; they are in because the spec covers every engine the survey found.
- [ ] **Step 7: Run** `python -I -X utf8 harness/bench.py setup --tools` then `setup` for all, in the background, reporting one line per engine. Verify: `engines.tsv` has one row per registry entry plus three tools, each `ok`, `install_failed` with an error, or `not_testable` with a reason; `tool_path("soffice") --version` prints a LibreOffice version.
- [ ] **Step 8: Report** the `engines.tsv` summary to the user (counts per status, each failure's error line). Commit `bench: setup, tools and engine stubs` with `engines.tsv`.

### Task 4: Generated corpus with answers

**Files:**
- Create: `harness/gen/seeds.py`, `harness/gen/lo_word.py`, `harness/gen/sheet.py`, `harness/gen/slides.py`, `harness/gen/pages.py`, `harness/gen/survive.py`
- Modify: `harness/bench.py` (subcommand `gen`)
- Test: `tests/test_gen.py`

**Interfaces:**
- Produces `corpus/generated/<family>/<name>.<ext>` and one `truth.json` per seed with keys: `seed, family, headings: [[level, text]], paragraphs: [str], tables: [{cells: [[str]], merged: [[r, c, rows, cols]]}], charts: [{title, categories: [str], series: [{name, values: [number]}]}], images: [{text}], footnotes: [str], header: str, footer: str, notes: [str], sheets: [{name, hidden: bool, cells: {ref: value}, formulas: {ref: [formula, value]}}], equations: [str]`.
- Produces `corpus/generated/features.tsv`: `file, tables, charts, images, footnotes, notes` counts that survived in that file, read back through LibreOffice. Scoring (Task 8) ignores truth items a file lost in generation.

- [ ] **Step 1: Write the failing test** `test_gen.py` (runs only when `tool_path("soffice")` exists): after `gen`, every extension in the generated list below exists; each `truth.json` has the keys above; `seeds.py` content holds Hangul and Latin; the scanned PDF has no text layer (`pdftotext` output under 20 characters) while the text PDF has the seed's first paragraph; the long PDF has more than 100 pages (`pdfinfo`); `features.tsv` has a row per generated file.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement.** Seeds per family hold exactly the spec's Corpus table items. Masters: the word seed is built in Writer by `lo_word.py` under `lo_python()` (UNO), because python-docx writes neither charts nor footnotes, and saved to odt, docx, doc, docm, rtf, pdf, a two-column pdf, and a long pdf (the seed repeated to more than 100 pages). The sheet seed is built by openpyxl in `_gen` (with the 10,000-row sheet), then converted by LibreOffice to xls, xlsm, ods. The slides seed is built by python-pptx in `_gen`, then converted to ppt, pptm, ppsx, odp. `pages.py` makes the scanned PDF (`pdftoppm -r 150` then `img2pdf`), and epub from the docx by `ebook-convert`, then mobi and azw3 from the epub. hwpx and djvu are attempted with a writer library and a PDF-to-DjVu tool; failure prints `SKIPPED\t<ext>\t<reason>` and leaves them to Task 5. `survive.py` converts each file to its ODF type with LibreOffice and counts `table:table`, `draw:object` (charts), `draw:image`, `text:note`, presentation notes in `content.xml`.
- [ ] **Step 4: Run** the test. Expected: `pass`.
- [ ] **Step 5: Commit** `bench: generated corpus with answers` (code only; generated files are ignored).

### Task 5: Public and real corpus, index and coverage

**Files:**
- Create: `harness/corpus.py`, `harness/public_sources.tsv`
- Modify: `harness/bench.py` (subcommand `corpus`)
- Test: `tests/test_corpus.py`
- Output: `corpus/public/<family>/`, `corpus/public/sources.tsv`, `corpus/real.tsv`, `corpus/index.tsv`, `corpus/gaps.tsv`

**Interfaces:**
- Consumes: `FAMILIES`, `sha1_file`, the clones under `engines/*/src`.
- Produces: `build_index() -> list[dict]` writing `corpus/index.tsv` with `doc_id, family, ext, source, path, truth, expect` where `source` is `generated`, `public` or `real`, `doc_id` is `<source>-<ext>-<sha1[:10]>`, and `expect` is a path to a JSON list of expected strings or empty. `gaps.tsv`: `ext, reason, where_to_find`.

- [ ] **Step 1: Write the failing test** `test_corpus.py` in a temp `BENCH_ROOT`: three byte-identical files named `x.pdf`, `x (1).pdf`, `x (2).pdf` in a real-documents list give one index row; a public file without a license in `sources.tsv` is refused with `REFUSED\tno license\t<file>`; an extension with no document of any source appears in `gaps.tsv`; `doc_id` matches `^(generated|public|real)-[a-z0-9]+-[0-9a-f]{10}$`.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement `corpus.py`.** Public files: copy matching extensions from the clones' test folders, plus a sparse clone of `github.com/apache/tika` (pattern `**/test-documents/**`) and the HWP parser samples of `github.com/mete0r/pyhwp`, `github.com/neolord0/hwplib` and `github.com/hahnlee/hwp.js`; at most 3 public files per extension, the smallest first. Each file's repo URL, path and the repo's license go to `sources.tsv`. Expected strings come from string literals in the same repo's tests that name the file. Real documents: the work files in `C:\Users\hunje\Downloads` with one of the 33 extensions, deduplicated by hash; `C:\Users\hunje\OneDrive` is not read.
- [ ] **Step 4: Run** the test. Expected: `pass`.
- [ ] **Step 5: Run** `bench.py corpus`. Verify: every extension has at least one row in `index.tsv` or a row in `gaps.tsv` with a reason and a place to look. Report the coverage table (extension, generated, public, real counts) to the user.
- [ ] **Step 6: Commit** `bench: public and real corpus index` with `harness`, `tests` and `corpus/index.tsv`.

### Task 6: Engine adapters

**Files:**
- Modify: `harness/adapters/<name>.py` for every row of Task 3's table; create `harness/adapters/pipelines.py`, `harness/adapters/baseline.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: `Adapter`, `register`, `call_tool`, `tool_path`, `lo_python`.
- Produces: a working `convert` (and `post` where needed) per adapter, and these extra entries: pipelines `lo>docx>dochan`, `lo>docx>docling`, `lo>xlsx>markitdown`, `lo>pptx>markitdown`, `lo>pdf>pymupdf4llm`, `calibre>epub>docling`, `calibre>epub>markitdown`, and the baseline `claude-read` (formats `pdf` plus the image types; its `out.md` is the original's text layer by `pdftotext` and its `assets/` the page images `pdftoppm -r 100`, which is what Claude Code's `Read` sends). Pipelines and the baseline get `engines.tsv` rows whose `status` is `ok` only when every part they call is `ok`. Every adapter's `formats` now comes from its README in the clone.

- [ ] **Step 1: Write the failing test** `test_adapters.py`: for each registry entry with `status == "ok"` in `engines.tsv`, run it on the smallest corpus document of each format it claims (generated first, else public); assert `meta["failure"] in (None, "timeout", "memory_limit", "crash", "empty_output", "garbled")` and that `out.md` exists when `failure is None`. The test prints one line per pair and fails only on an exception inside the harness.
- [ ] **Step 2: Run** it. Expected: FAIL (`NotImplementedError`).
- [ ] **Step 3: Implement each adapter's `convert`** from its README in the clone; put the README line the command came from in a comment above it. Adapters that only serve MCP (`hwp-mcp`, both Excel servers, and kordoc or dochan if their CLI lacks Markdown output) call `call_tool` from a small runner script in `harness/adapters/mcp_run.py`, executed under the engine's own Python or Node.
- [ ] **Step 4: Run** it. Expected: `pass`, and every `ok` engine has at least one pair with `failure is None`, or a written reason why not in its adapter module docstring.
- [ ] **Step 5: Commit** `bench: engine adapters, pipelines and baseline`.

### Task 7: Full run

**Files:**
- Modify: `harness/run.py` (subcommand `status`: counts per engine and failure type)

- [ ] **Step 1: Run** `bench.py run` in the background over every engine and document. One line to the user per engine finished.
- [ ] **Step 2: Verify** with `bench.py status`: for every `ok` engine and every index row with a claimed extension there is a `meta.json`; no row is missing.
- [ ] **Step 3: Rerun** each `timeout` or `crash` once with `--force` to separate flaky from stable failures; the second meta replaces the first, and `status` shows how many changed.
- [ ] **Step 4: Report** the failure table to the user. Commit `bench: full run status`.

### Task 8: Scoring and ranking

**Files:**
- Create: `harness/score.py`
- Modify: `harness/bench.py` (subcommand `score`)
- Test: `tests/test_score.py`
- Output: `scores.jsonl`

**Interfaces:**
- Consumes: run folders, `truth.json`, `features.tsv`, `index.tsv`, `engines.tsv`.
- Produces: one `scores.jsonl` row per run: `id` (`<engine>/<doc_id>`), `engine, doc, family, ext, source, failure, items: {text, order, tables, charts, images, structure, specific, expected, agreement}` (absent items omitted), `fidelity`, `chart_image_only: bool`, `seconds`. `rank(rows: list[dict], ext: str, tokens: dict[str, float]) -> list[str]` returns engines best first by the Global Constraints rule, using `engines.tsv` `weight` for the last tie; `tokens` maps engine to its mean calibrated tokens on that extension. `rank_family(rows: list[dict], family: str, tokens: dict[str, float]) -> list[str]` applies the same rule to mean fidelity and mean tokens over all of a family's documents.

- [ ] **Step 1: Write the failing tests** with hand-written truth and outputs:
  - text: 3 answer paragraphs, output holding 2 verbatim and 1 at ratio 0.85: `text == 2/3`; `order` is 1.0 for the same order, lower when swapped.
  - tables: a 3x3 answer and a Markdown table with one wrong cell: `tables == 8/9`; an HTML table with `colspan` matching a merged answer scores the merged cell right.
  - charts: series values present as a table: `charts == 1.0`; chart only as `![](assets/chart.png)`: `charts == 0` and `chart_image_only` true.
  - images: 2 answer images, 2 extracted, both linked, one image's text recovered: `images == 1.0` (mean of count match and position, plus 0.1 per recovered text, capped at 1).
  - structure: share of checks passed among headings levels, lists, footnotes, notes, hidden sheets, formulas; a header line repeated on at least half the pages counts as one failed check.
  - fidelity: the mean of present items; a run with `failure` set has `fidelity == 0`.
  - agreement: with 4 engines, the reference is the normalized lines found in at least 2 outputs; an engine holding all of them scores 1.0; agreement is absent when fewer than 3 engines succeeded.
  - truth items a file lost in generation (`features.tsv`) are skipped.
  - `rank`: fidelities 0.90 and 0.88 with tokens 5000 and 3000 rank the second first; 0.90 and 0.85 rank the first first; equal fidelity and tokens with weights 2 and 0 rank the weight-0 engine first. `rank_family` over two extensions averages before ranking.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement `score.py`.** Tables are parsed from Markdown pipes and from HTML through `html.parser`.
- [ ] **Step 4: Run** it. Expected: `pass`.
- [ ] **Step 5: Run** `bench.py score`. Verify one row per run folder. Commit `bench: scoring and ranking` with `scores.jsonl`.

### Task 9: Tokens and calibration

**Files:**
- Create: `harness/tokens.py`, `harness/claude_runs.py`
- Modify: `harness/bench.py` (subcommands `tokens`, `calibrate [--approved]`)
- Test: `tests/test_tokens.py`, `tests/fixtures/fake_claude.py`
- Output: `tokens.jsonl` (`id, text_tokens, image_tokens, estimate, calibrated`), `calibration.json` (category to ratio)

**Interfaces:**
- Produces: `image_tokens(w: int, h: int) -> int` (scale down to a long edge of 1568 px, then `ceil(w * h / 750)`); `text_tokens(paths: list[Path]) -> list[int]` (one `_tok` subprocess for the batch, `o200k_base`); `doc_cost(run_dir: Path) -> dict`; `claude_usage(json_text: str) -> int` (`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`); `claude_cmd(prompt: str, add_dir: Path) -> list[str]` = `[CLAUDE, "-p", prompt, "--output-format", "json", "--model", "claude-opus-5-5", "--max-turns", "3", "--allowedTools", "Read", "--add-dir", str(add_dir)]` where `CLAUDE` is env `BENCH_CLAUDE` or `claude`. Before first use, check each flag against `claude --help` and adjust the names there if the installed version differs.
- Categories: `text_ko, text_en, table_md, image, pdf_text_page, pdf_scan_page`.

- [ ] **Step 1: Write the failing tests:** `image_tokens(1568, 1568) == 3279`; `image_tokens(3136, 1000)` equals `image_tokens(1568, 500)`; `claude_usage` on a fixture JSON with 100, 2000 and 30 gives 2130; `calibrate` without `--approved` prints `ESTIMATE\t<runs>\t<tokens>` and runs nothing (the `fake_claude.py` stub writes a counter file that must not exist); with `--approved` and the stub, each pair's ratio is `(usage_read - usage_plain) / estimate` and `calibration.json` holds the median per category.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement.** Pairs: the plain run's prompt is `Reply OK.`; the read run's is `Read <file> and reply OK.` Samples: 20 pairs across the six categories, files under 2,000 lines so `Read` returns them whole. `tokens` writes `tokens.jsonl` for every successful run and for the `claude-read` baseline.
- [ ] **Step 4: Run** it. Expected: `pass`.
- [ ] **Step 5: Run** `bench.py tokens`, then `bench.py calibrate`. Show the user the printed estimate and wait for a yes; then `calibrate --approved` in the background. Afterwards rerun `tokens` so `calibrated` is filled. Commit `bench: token estimates and calibration` with `tokens.jsonl` and `calibration.json`.

### Task 10: Question answering on real documents

**Files:**
- Modify: `harness/claude_runs.py` (subcommand `qa [--approved]`)
- Create: `corpus/qa/<doc_id>.json` (written by the main session, not by a subagent)
- Test: `tests/test_qa.py`
- Output: `qa.jsonl` (`id, engine, doc, question, kind, answer, grade`)

**Interfaces:**
- Consumes: `rank_family`, `claude_cmd`, `claude_usage`, `index.tsv`.
- Produces: `grade(answer: str, expected: str, kind: str) -> str` returning `right`, `partial`, `wrong` or `manual`. `kind` is `number` (every number in `expected` found in `answer` within 1 %: all `right`, some `partial`, none `wrong`), `string` (normalized `expected` in normalized `answer`: `right`, else `wrong`), or `free` (`manual`).

- [ ] **Step 1: Write the questions.** For every real non-PDF document and 6 PDFs chosen for tables, charts and scans (about 14), the main session views the original as page images (non-PDF rendered to PDF by LibreOffice first, then `pdftoppm -r 100`) and writes 5 to 8 questions with answers and kinds, covering text facts, table cells, chart values and trends, image content and cross-section context.
- [ ] **Step 2: Write the failing test** for `grade` with the cases above, and for `qa` without `--approved` printing `ESTIMATE` and running nothing.
- [ ] **Step 3: Run** it. Expected: FAIL.
- [ ] **Step 4: Implement `qa`.** It takes the top two engines per family from `rank_family`, gives `claude -p` only that engine's run folder for each real document (`--add-dir`) and the questions, and asks for a JSON list of answers.
- [ ] **Step 5: Run** the test. Expected: `pass`.
- [ ] **Step 6: Run** `qa`, show the estimate, wait for the user's yes, run `qa --approved` in the background. The main session grades every `manual` row and records it in `qa.jsonl`.
- [ ] **Step 7: Commit** `bench: question answering on real documents` with `corpus/qa` and `qa.jsonl`.

### Task 11: Reports

**Files:**
- Create: `harness/report.py`
- Modify: `harness/bench.py` (subcommand `report`)
- Test: `tests/test_report.py`
- Output: `reports/word.md`, `reports/sheet.md`, `reports/slides.md`, `reports/pages.md`, `reports/db.md`, `reports/office.md`, `reports/summary.md`

**Interfaces:**
- Consumes: `scores.jsonl`, `tokens.jsonl`, `calibration.json`, `qa.jsonl`, `engines.tsv`, `index.tsv`, `gaps.tsv`, `rank`.
- Produces: `cover(rows, exts) -> tuple[list[str], float]`: the smallest engine set covering every extension that any engine covers, where an engine covers an extension when its mean fidelity there is at least 0.6; among sets of that size, the one with the highest mean best fidelity; returns the set and the mean fidelity given up against the best engine per extension. Exhaustive over subsets with bit masks.

- [ ] **Step 1: Write the failing tests:** `cover` on a toy table (engines A covers a, b; B covers b, c; C covers a, b, c at lower fidelity) returns `["C"]` and the loss; every number `report.py` writes is followed by a reference `[<engine>/<doc_id>]` that exists in `scores.jsonl` or `tokens.jsonl` (check by regex over the generated files); each family report has the five sections of the spec's Reports list, in order, with Korean headings `결론`, `확장자별 결과`, `문답 결과`, `엔진 메모`, `남은 공백`; every extension section states its failure rate and one representative `error_line` per failing engine; `summary.md` has a routing table of 33 rows and a table of install weight and license per engine.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement `report.py`.** Tables and numbers come from data; the sections that need judgment (failure examples, chart and image recommendation, gaps prose) are left as `<!-- write: ... -->` markers.
- [ ] **Step 4: Run** it. Expected: `pass`.
- [ ] **Step 5: Write the judgment sections** in the main session, in Korean, from the run folders: failure examples quote the original and the output side by side; the summary's recommendation on charts and images and its list of questions for stage 4 follow the spec. Then check no `<!-- write:` marker remains.
- [ ] **Step 6: Commit** `bench: reports` in BENCH. In the phro-project repository nothing is committed by this task.
- [ ] **Step 7: Hand off:** give the user the summary's conclusion in chat (Korean) and links to the seven reports, and name stage 4's starting point: a new brainstorming for the phro-project reader, taking `reports/summary.md`, `harness/adapters/`, the `out.md` shape and `scores.jsonl` as input.
