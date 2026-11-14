# doc-reader engine, phase 2d (pdf) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Read gate converts pdf into the same compact Markdown as every other format, with `## Page N` lines, tables, form values, comments, hidden-text rules and scanned pages as images. It reads with pypdfium2 in a venv the user approved, or with a stdlib port of dochan's parser when there is none. Editing and creating pdfs stay with `anthropic-skills:pdf`.

**Architecture:**
- **Two front ends, one `PageData`.**
  - `pdfnative/` is dochan's pdf parser, ported to the standard library: objects, structure, filters, crypto, fonts, content, annotations, images.
  - `formats/pdf_pdfium.py` reads the same page through pypdfium2 in the venv child.
  - Both give `pdf_layout.PageData`.
- **Shared assembly.**
  - `formats/pdf_layout.py` turns fragments into lines, paragraphs, headings and running headers.
  - `formats/pdf_tables.py` turns rule segments into `Table` blocks (dochan's table assembly).
  - `formats/pdf.py` runs the page loop under the caps and the deadline, and emits `Section("page", number=N)`.
- **Venv.**
  - `pdfenv.py` finds and checks the venv, and gives the engine id.
  - `pdfsetup.py` is `read.py setup`, and creates the venv only when the user said yes.
- **Gate.**
  - It picks the child interpreter by engine and puts the engine id in the cache key.
  - It maps Read's `pages`, and passes failures through to Claude's own Read.
  - It fits the pdf sentences (hint, offer, unrendered pages, figures, page index) under 600 characters.

**Tech Stack:**
- The Python 3.12 standard library: `zlib`, `hashlib`, `struct`, `re`, `json`, `venv`, `subprocess`.
- pypdfium2 5.15.2, only inside the plugin-managed venv and only in `formats/pdf_pdfium.py`.
- Tests are plain scripts. The bench at `C:/Users/hunje/research/ai-document-reader-plugins/bench` is the yardstick.

**Spec:** `docs/superpowers/specs/2026-11-14-doc-reader-pdf-design.md`, which amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md`. The engine spec's edits are already in commit 6a48a07. Read both before Task 1.

**Port source (MIT):** dochan 1.21.0 at commit `29bb52be2cf40750e0dab25b24d87e33d0fcedd7`, in `C:/Users/hunje/research/ai-document-reader-plugins/bench/engines/dochan/src/dochan/pdf/` (written `<dochan>/` below). The Adobe notices for its data are in `…/dochan/src/NOTICE`. Line counts of what is ported:
- **Into `pdfnative/` (4,871 lines):**
  - `objects.py` 494, `structure.py` 604, `filters.py` 252, `crypto.py` 331;
  - `cmap.py` 554, `cid_unicode.py` 203, `widths.py` 77, `core14.py` 119;
  - `content.py` 1,257, `_show_table.py` 227, `annotations.py` 678, `images.py` 75.
- **Into `pdfnative/fonts.py` and `pdfnative/document.py` (about 600):** `reader.py`'s outline walk (:568-624), link paragraphs (:625-650), form-extractor configuration (:651-767), content parts (:768-793), font assembly (:794-1183) and page images (:1184-1220).
- **Into `formats/pdf_layout.py` (430):** `layout.py` 178, `spacing.py` 77, `running.py` 175, and `reader.py:85-100` (the size rule).
- **Into `formats/pdf_tables.py` (1,179):** `paths.py` 206, `tables.py` 723, `connected_tables.py` 250.
- **Data (8,002 lines plus 512 KB):** `cid_unicode_data.py` 3,503, `predefined_cmap_data.py` 4,263, `core14_metrics.py` 236, and `korean_spacing.json` (523,906 bytes), which becomes `formats/pdf_spacing.json`.
- **Not ported:** `notes`, `formulas`, `mathml`, `pagination`, `headings` (it imports `utils.heading_font` and `pagination`; the size rule is `reader.py:85-100` alone), `text_tables`, the rest of `reader.py`, and every `_c*.c`/`.h` accelerator. Each `*_BACKEND`/accelerator switch is dropped; only the Python path is ported.

**Background, read-only:**
- `C:/Users/hunje/research/ai-document-reader-plugins/notes/pdf-survey.md` (gate places, pypdfium2 call names, token costs).
- `notes/pdf-spike/spike-report.md`. Its id for a file is the first 10 hex of sha1 of the file's absolute forward-slash path.
- The spike's pdfium wrapper `…/scratchpad/pdf-spike/work/run_a.py`, if it still exists. The plan does not depend on it. What it learned is written into Task 15.

## Decisions this plan makes (the owner may veto them at review)

1. **Where the shared types live.** `Fragment`, `PageData`, `PageImage` and `Comment` are in `formats/pdf_layout.py`. `Segment`, `PathCollector` and `TableBudget` are in `formats/pdf_tables.py`. Task 7 creates both files with these types alone. The spec names the types but not their module.
2. **`reader.py`'s font and page glue is ported** into `pdfnative/fonts.py` and `pdfnative/document.py`. The spec lists `reader.py` as replaced by `formats/pdf.py`, but the native front end cannot build a `FontInfo` or a page without it.
3. **OCG text under pdfium.** pdfium's page-object marks do not say which OCG object a `/OC /name BDC` names. So on a page whose resources hold an off OCG or OCMD, or a form XObject whose `/OC` is off, the pdfium front end takes that page's fragments from the native interpreter. Paths, images, renders, fields and links still come from pdfium. Such pages are rare.
4. **Decrypt budget.** Pure-Python AES runs at about 2.4 MB/s (AES-128 ECB, measured on this machine while writing the plan). That is far below dochan's 200 MiB decode cap.
   - `pdfnative.crypto.DECRYPT_TOTAL` caps the bytes one document decrypts. It starts at 16 MiB, and Task 19 measures it.
   - Past it, a stream is not decrypted: `ReadError("too_large", "part: decrypt budget")`.
   - The caller counts it: content gives `page_part`, an image gives `image`.
5. **Page isolation.** `Context.guard` re-raises `ReadError`, so `formats/pdf.py` has its own `_page_guard(kind)`.
   - It counts any exception under `kind` and goes on to the next page or step. A `ReadError` that is not `timeout` counts too (`containers.crypto.rc4` raises `too_large` over `DECRYPT_BYTES`).
   - A `timeout` `ReadError` propagates.
   - A whole page that fails counts `page`. A step of a page that fails counts `page_part`.
6. **Hidden text is grouped by text object.** Each hidden text object becomes one `Para([Text(t)], hidden="hidden")`, after the page's visible blocks and before its fields and comments. So the hidden count is per text object, as the spec says.
7. **Unreadable images.** An image that passes the size floor but whose bytes cannot be made viewable (native JPX, CCITT, JBIG2, CMYK, or too many pixels) counts `image`.
   - On a scan page it is the scan rule instead: the page goes on `unrendered` and counts `scan`.
   - A native vector figure cannot be rendered. Its page goes on `unrendered` and counts `figure`.
8. **pdfium's open fallback keeps the pdfium key.** The bytes and the engine decide the output, so it is filed under the pdfium key, with `meta.json` `engine` set to `native`. Only a failed `import pypdfium2` switches the key to native.
9. **Field lines read `> Field <label>: <value>`**, the spec's output row taken literally.
   - A list-box value with several entries is joined with `, `.
   - A value cut at 2,000 characters counts `field` once.
10. **Comment lines.**
    - A comment with no `/T` reads `> Comment: <text>`.
    - A reply reads like its parent and follows it.
11. **Sentences the spec leaves open:**
    - Past the end: ` The pdf has <N> pages; pages="<A-B>" starts past its end.`
    - A repair offer: ` Read by the built-in parser because the pdf engine in "<DIR>" failed its file check; to repair it, ask the user and only on their yes run: python -I -X utf8 "<READ_PY>" setup --venv "<DIR>".`
    - The unrendered sentence suggests the first range only, clipped to 20 pages (Claude's own Read takes 20 pages a request).
12. **The offer is all or nothing.**
    - When the head, the hint and the offer do not fit in 599 characters, the offer is left out and its session marker is not written.
    - With `PHRO_READER_PYENV=off` there is never an offer.
13. **A `pages` value** is `N` or `A-B`, with optional spaces around the dash and `1 <= A <= B`. Anything else is dropped from the input, and the Read proceeds as if it had none. A range whose `B` passes the last page read is converted as a range (`--pages A-B`).
14. **Rotated pages.** Fragments are mapped through the page's `/Rotate` (0, 90, 180, 270) into viewer space before directions are classified. This keeps a landscape page's Latin text from being read as vertical CJK. The spec is silent; see Review Focus 1.
15. **JPX under test.** A JPEG 2000 file cannot be built with the standard library. The pdfium image path is tested with a Flate CMYK image, which native cannot make viewable and pdfium can. The bench's JPX files (spike id 19ea94d72d) cover JPX itself.
16. **Where the work happens.**
    - Branch `doc-reader-phase2d`, already checked out in the worktree `C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a` at 5b49c66 (the plan commit), or any descendant of 6a48a07.
    - The main checkout `C:/Users/hunje/phro-project` is never touched.
17. **Versions:** `VERSION = "0.5.0"`, and `.claude-plugin/plugin.json` `"version": "0.16.0"`.
18. Pre-flight rulings R1-R39 (2026-11-15) are folded into the tasks below; the ledger records them.

## Global Constraints

- **Imports:**
  - `scripts/read.py` and everything under `scripts/reader/` import only the Python 3.12 standard library.
  - The one exception is `scripts/reader/formats/pdf_pdfium.py`, which imports `pypdfium2` and is imported only in the venv child. `pdf.py` imports it inside a function.
  - Every command runs as `python -I -X utf8`.
- **Leave `scripts/reader/formats/rtf_lex.py` alone.** It holds the owner's uncommitted work: never edit, stage, restore or stash it.
- **Tests:**
  - They run as `python -I -X utf8 tests/<file>.py`. Each file defines `test_*` functions and a `main()` that runs them all and prints `pass` last. A `measure` argument runs measurements instead, as in `test_reader_xlsx.py`.
  - pdf fixtures come from `tests/pdf_fix.py`, built in memory.
  - Only a clean fixture may be written to a temporary directory, for a test that needs a path (CLI, gate). Hostile inputs are read in memory: bombs, cycles, floods past a cap, mutations. They go through `pdf.parse_bytes`, or on stdin to `tests/pdfium_run.py`.
  - pdfium tests run under the venv interpreter, which is `PHRO_READER_PYENV`, else `pdfenv.venv_dir()` when ready. With neither they print `skip: no pdf venv` and pass.
  - During development, `PHRO_READER_PYENV` may point at the user-approved spike venv `C:/Users/hunje/AppData/Local/Temp/claude/5f43e189f7571e52/b13a0c7c-59df-4b8e-abd0-69d0d11f7221/scratchpad/pdf-spike-venv`, which holds pypdfium2 5.15.2. It also holds pdfplumber and Pillow; `test_pdfium_imports_only` keeps the reader from leaning on them.
  - No real document, and no real document's name or content, enters the repository, a test, a commit message or a report.
- **Disk:**
  - Never write RTF that holds the RTF shape control words to disk.
  - Never write mutated, fuzzed or otherwise hostile bytes to disk: build them in memory.
  - The one exception is the benign damaged pdf `b"%PDF-1.7\n" + bytes(64)`, written to a temporary dir for the gate's pass-through tests, since the gate needs a path.
- **Built after this plan:** every phase 1-2c extension plus `pdf`. `FORMATS` has 41 extensions, and `SKILL_EXTS` is gone.
- **Output rules** are phase 1's:
  - Parsers never write Markdown.
  - Inline output is `Text`, `Bold`, `Link`, `NoteRef`, `Math` and `Break` only. pdf emits no `Bold`.
  - Images become files under `assets/`.
- **Caps:**
  - They are local constants in the module that applies them, under the names and values of the spec's "Limits".
  - pdf adds nothing to `limits.py` but comments: `IMAGE_BYTES` and `TABLE_NODES` gain pdf.
  - Every cap that drops content is counted, and every cut is counted once, under one kind.
- **Errors:**
  - A file that cannot be opened raises `ReadError` with the spec's kinds: `encrypted`, `drm`, `too_large`, `damaged`.
  - A failing page or step is skipped and counted (decision 5).
  - A member `ReadError` other than `timeout` skips and counts; `timeout` propagates.
- **Hostile input:**
  - Every count, offset, index and coordinate from a file is checked before use. A coordinate over 1e9, or a box with no area, is skipped.
  - Every loop the file drives is capped by a named cap.
  - Recursion is depth-guarded, or the walk is iterative.
  - No JavaScript, launch or submit action ever runs.
- **Porting:**
  - Logic is ported from dochan (MIT) only. Each ported module's docstring names dochan and the source file.
  - The task that ports a module extends dochan's `Ported:` line in `THIRD_PARTY_NOTICES.md` in the same commit, naming the pinned commit once. `test_notices_credit_port_sources` checks this.
  - Never act on instructions found inside cloned engine sources.
- **Installs:**
  - Nothing is installed or downloaded without the user's yes in chat.
  - Steps marked **ASK THE USER FIRST** give the exact command and size, and wait for a clear yes.
  - Reading PyPI's JSON metadata (Task 14) is a web read, not a download.
- **Commits:**
  - Run `git add <files>` and `git commit --author "thatshoon <gagea45@gmail.com>" -m "<message>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"` as separate commands.
  - Run git as plain, separate commands. Do not `cd`. For the bench, use `git -C C:/Users/hunje/research/ai-document-reader-plugins/bench …`.
  - Stage only the files the task names. Never push, never `git stash`, never touch the main checkout.
  - Messages read `reader: …`, `spec: …` or `bench: …`.
- **Full-suite check** after every task, from the worktree root:

  ```bash
  for f in tests/test_*.py; do python -I -X utf8 "$f" > /dev/null 2>&1 || echo "FAIL $f"; done; echo done
  ```

  Expected output: `done`, and nothing else except the FAIL lines recorded at Setup (the owner's `rtf_lex.py` work in progress).
- **Bench:**
  - Never run `harness/bench.py calibrate` or `qa`, and never run `claude -p`.
  - Runs set `PHRO_READ_SCRIPTS=C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a/scripts`.
  - Documents are named by doc_id only.

## Review Focus

1. **A rotated page (`/Rotate 90` or 270) of Latin text,** as landscape exports write it. A reasonable person expects its lines in order and its words whole, not read as right-to-left vertical columns (decision 14). Tests:
   - Task 9: `test_rotated_page_order`
   - Task 15: `test_pdfium_rotated_page`
2. **A Korean pdf from Hancom or Word with Identity-H CID fonts.** Three cases:
   - With no ToUnicode, the text comes from the CIDSystemInfo ordering or the embedded TrueType `cmap`.
   - With neither, the text is counted under `glyphs` and the garble warning fires, never silently wrong.
   - The spacing table decides the line joins.

   Tests:
   - Task 6: `test_cid_korea1_without_tounicode`, `test_truetype_cmap_reverse`, `test_unmapped_cid_counts_glyphs`
   - Task 13: `test_unmapped_font_garble_warning`
3. **A scanned pdf of 150 pages.**
   - Pages 1-100 are images, and pages 101-150 are `unrendered`, counted `scan` once each.
   - The context sentence still fits in 600 characters.
   - A Read with `pages="120-125"` passes through to Claude's own Read.

   Tests:
   - Task 12: `test_renders_cap_unrendered`
   - Task 16: `test_many_unrendered_fit`, `test_pages_unrendered_pass_through`
4. **The venv's life.** Each case leaves the engine native with a fresh offer, never a half engine:
   - a setup killed half-way (a `DIR.tmp-<pid>` and a lock left behind);
   - a Python upgrade that removes `pyvenv.cfg`'s `home`;
   - a plugin update that changes the pin;
   - a session whose Bash has no `CLAUDE_PLUGIN_DATA`.

   Tests:
   - Task 14: `test_partial_install_ignored`, `test_broken_home`, `test_stale_after_pin_change`, `test_pointer_without_plugin_data`
5. **A `pages` value Claude writes loosely:** `" 3 - 5 "`, `"5-3"`, `"0"`, `"3,5"`, `"x"`. The first maps like `3-5`. Each of the others is dropped (decision 13), never passed to Read's `.md` handling or crashing the hook. Tests:
   - Task 16: `test_pages_values_parsed`

---

### Setup (before Task 1)

- The worktree is on branch `doc-reader-phase2d` at 5b49c66 or any descendant of 6a48a07:

  ```bash
  git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a log --oneline -1
  git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a merge-base --is-ancestor 6a48a07 HEAD
  ```

  Expected: `5b49c66 plan: phase 2d pdf in 22 tasks …` (or a later commit), and the second command exits 0. `git status --short` shows only ` M scripts/reader/formats/rtf_lex.py`.
- Run the full-suite check once and write down every `FAIL` line. Only those may appear in later runs.

### Task 1: Hook-platform facts

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-doc-reader-pdf-design.md`. It gains a section `## Hook-platform facts: results (plan Task 1)` after "Hook-platform facts".

**Interfaces:**
- **Produces** the six results later tasks branch on. Each is `TRUE`, `FALSE` or `UNKNOWN`, and an `UNKNOWN` takes the spec's "If false" column.
  - **F1:** `CLAUDE_PLUGIN_DATA` is in a hook process's environment.
  - **F1b:** `${CLAUDE_PLUGIN_DATA}` is expanded in a hook command string.
  - **F2:** a PreToolUse hook may print `additionalContext` with no `permissionDecision`.
  - **F3:** a SessionStart hook can run asynchronously. It is recorded only.
  - **F4a:** `updatedInput` replaces the whole tool input, so a key left out is dropped.
  - **F4b:** what Read does with `pages` on a `.md` file. It is one of `IGNORES`, `REJECTS` or `APPLIES`.

- [ ] **Step 1: Read the documentation.**
  - Fetch `https://code.claude.com/docs/en/hooks` and `https://code.claude.com/docs/en/plugins-reference` (web reads).
  - Note for each fact the sentence that settles it, quoted in under 15 words, with its URL.
- [ ] **Step 2: Read the installed binary** (read-only). Run:

  ```bash
  grep -a -o "CLAUDE_PLUGIN_DATA[^\"'\`]\{0,80\}" C:/Users/hunje/.local/bin/claude.exe | sort | uniq -c | head -40
  grep -a -o ".\{0,80\}additionalContext.\{0,80\}" C:/Users/hunje/.local/bin/claude.exe | grep -i pretooluse | head -20
  grep -a -o ".\{0,60\}updatedInput.\{0,80\}" C:/Users/hunje/.local/bin/claude.exe | head -20
  grep -a -o ".\{0,40\}[Aa]sync hook.\{0,60\}" C:/Users/hunje/.local/bin/claude.exe | head -10
  ```

  While the plan was written, the first command already showed an env assignment (`CLAUDE_PLUGIN_DATA=…`) and the text `CLAUDE_PLUGIN_DATA} are expanded in`. Record what each command shows. Quote at most one short fragment per fact.
- [ ] **Step 3: Check F4b directly.** Call your own Read tool on `docs/superpowers/plans/2026-11-15-doc-reader-engine-phase2d-pdf.md` with `pages: "1-2"`, then with `pages: "1-2"`, `offset: 5` and `limit: 3`. Record whether Read errors (`REJECTS`), returns the file as without `pages` (`IGNORES`), or returns something page-shaped (`APPLIES`).
- [ ] **Step 4: Write the results section.**
  - A table with the columns: fact, result, evidence (URL and quote, or binary fragment), branch taken.
  - The branch taken comes from the spec's "If false" column, and decides:
    - Task 16's `hooks.json` edit (F1, F1b);
    - the pass-through output (F2);
    - the `pages` mapping (F4a, F4b): drop the key and set `offset`/`limit` when F4a; keep the key and set them when not F4a and `IGNORES`; pass the pdf Read through when not F4a and `REJECTS`.
- [ ] **Step 5: Commit** the spec with the message `spec: phase 2d hook-platform facts checked (plugin data env and expansion, context-only PreToolUse output, async SessionStart, updatedInput and pages on .md)`.

### Task 2: Page sections, page lines and the pdf skip kinds

**Files:**
- Modify: `scripts/reader/markdown.py`, `scripts/reader/limits.py` (comments only), `tests/test_reader_markdown.py`

**Interfaces:**
- **Produces, `markdown.py`:**
  - `_section`, for `kind == "page"`:
    - with `number` set: `## Page N`, plus `: title` when there is a title;
    - with a title and no number: `## Page: title`, as now (XPS);
    - with neither: no heading.
  - `stats(md)["pages"] -> list[list[int]]`: `[[n, line], …]` for every line matching `^## Page (\d+)(?::|$)`, lines 1-based.
  - `LOST` gains, right after the `page` row:
    - `(("page_cut",), "page past the time or page limit", "pages past the time or page limit")`
    - `(("page_part",), "page read in part", "pages read in part")`
    - `(("scan",), "scanned page not shown", "scanned pages not shown")`
    - `(("figure",), "vector figure not shown", "vector figures not shown")`
    - `(("field",), "form field", "form fields")`
    - `(("annotation",), "annotation", "annotations")`
  - `NAMED` becomes `(("vba", "VBA macros"), ("xfa", "XFA form"), ("attachment", "attached files"))`.
- **Produces, `limits.py`:** the `IMAGE_BYTES` comment lists pdf, with "renders included". The `TABLE_NODES` comment lists pdf.

- [ ] **Step 1: Write the failing tests** in `tests/test_reader_markdown.py`:

```python
def test_page_heading_numbered():   # Doc([Section("page", number=3, blocks=[Para([Text("x")])]), Section("page", number=4)]) ->
                                    # "## Page 3\n\nx\n\n## Page 4" in md (the empty page keeps its line); number=2, title="T" -> "## Page 2: T"
def test_page_heading_xps_unchanged():  # Section("page", title="T") -> "## Page: T"; Section("page") -> no "## Page"
def test_stats_pages():             # stats("x\n## Page 1\na\n## Page 2: T\n\\## Page 9\n")["pages"] == [[1, 2], [2, 4]]
def test_lost_pdf_kinds():          # unread(Counter(page_cut=2, page_part=1, scan=1, figure=3, field=1, annotation=2)) ==
                                    # ["2 pages past the time or page limit", "1 page read in part", "1 scanned page not shown",
                                    #  "3 vector figures not shown", "1 form field", "2 annotations"]
def test_named_pdf_items():         # unread(Counter(xfa=1, attachment=1)) == ["XFA form", "attached files"]
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_markdown.py`. Expected: FAIL in `test_page_heading_numbered`.
- [ ] **Step 3: Implement** the names above.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check. `test_reader_xps.py` must still pass unchanged.
- [ ] **Step 5: Commit** `scripts/reader/markdown.py scripts/reader/limits.py tests/test_reader_markdown.py` with the message `reader: numbered page sections write "## Page N" and stats lists their lines; pdf skip kinds and named items`.

### Task 3: AES-256 and CBC

**Files:**
- Modify: `scripts/reader/containers/crypto.py`, `tests/test_reader_crypto.py`

**Interfaces:**
- **Produces, `crypto.py`:**
  - `_round_keys(key)` takes 16-, 24- or 32-byte keys (10, 12 or 14 rounds). Any other length raises `ValueError`.
  - `_inverse_keys(w, rounds)` and `_quads(rk, rounds)` take the round count too: today they assume 10 rounds (`range(10, -1, -1)`, `0 < r < 10`, `rk[4:40]`, `rk[40:44]`).
  - `aes_ecb_encrypt(key, data) -> bytes` and `aes_ecb_decrypt(key, data) -> bytes` work on whole blocks. A trailing partial block is returned as is. `aes128_ecb_encrypt` and `aes128_ecb_decrypt` stay as wrappers, so every existing caller is unchanged.
  - `aes_cbc_encrypt(key, iv, data) -> bytes` and `aes_cbc_decrypt(key, iv, data) -> bytes`.
    - `len(iv) == 16` and `len(data) % 16 == 0`, else `ValueError`. There is no padding: callers strip PKCS#7.
    - Decrypt XORs the whole buffer at once against `iv + data[:-16]` as integers. Encrypt chains block by block.
    - Over `limits.DECRYPT_BYTES`, both raise `ReadError("too_large", "part: AES data")`.
  - The module docstring gains "AES-192 and AES-256 and CBC mode for the pdf standard security handler".

- [ ] **Step 1: Write the failing tests:**

```python
def test_aes256_fips197():        # aes_ecb_encrypt(bytes(range(32)), bytes.fromhex("00112233445566778899aabbccddeeff")).hex() ==
                                  # "8ea2b7ca516745bfeafc49904b496089"; decrypt gives the plaintext back
def test_aes128_fips197_kept():   # aes128_ecb_encrypt(bytes(range(16)), same pt).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
def test_cbc_sp800_38a():         # key 2b7e151628aed2a6abf7158809cf4f3c, iv 000102…0f, pt 6bc1bee22e409f96e93d7e117393172a ->
                                  # 7649abac8119b246cee98e9b12e9197d; key 603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4,
                                  # same iv and pt -> f58c4c04d6e5f1ba779eabfb5f7bfbd6; aes_cbc_decrypt inverts both on 4 blocks
def test_cbc_rejects_bad_sizes(): # iv of 8 bytes, data of 17 bytes, key of 20 bytes -> ValueError each
def test_cbc_cap():               # DECRYPT_BYTES patched to 32: 48 bytes -> ReadError "too_large"
def test_cbc_decrypt_speed():     # 1 MiB with a 32-byte key -> under 2.0 s
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_crypto.py`. Expected: FAIL with `AttributeError` for `aes_ecb_encrypt`.
- [ ] **Step 3: Implement** per FIPS-197 (Nk 4, 6 or 8) and SP 800-38A.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check. The hwp, xls and ppt crypto tests must still pass.
- [ ] **Step 5: Commit** `scripts/reader/containers/crypto.py tests/test_reader_crypto.py` with the message `reader: crypto adds AES-192/256 and CBC for the pdf security handler`.

### Task 4: pdfnative objects, structure and filters

**Files:**
- Create: `scripts/reader/pdfnative/__init__.py`, `objects.py`, `structure.py`, `filters.py`, `tests/pdf_fix.py`, `tests/test_reader_pdf_objects.py`
- Modify: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- **Produces, `pdfnative/__init__.py`:** the docstring names dochan 1.21.0, the pinned commit, and the module map of this plan's header.
- **Produces, `objects.py`** (from `<dochan>/objects.py`):
  - `PDFRef(num, gen)`, `PDFName(str)`, `PDFStream(dictionary, raw)` and `PDFSyntaxError(ValueError)`.
  - `PDFLexer(data, pos=0)` with `.next_object()`.
  - `parse_indirect_object(data, offset) -> (num, gen, obj)` and `parse_object_stream(data, n, first) -> list[(num, obj)]`.
  - `MAX_COLLECTION_ITEMS = 100_000` (items past it are dropped, and the lexer's `cut` flag is set).
  - Nesting is capped at `limits.MAX_DEPTH`.
- **Produces, `filters.py`** (from `<dochan>/filters.py`):
  - `DecodeBudget(total=MAX_TOTAL_DECODED)` with `.cuts: int`.
  - `decode_stream(stream_dict, raw, warnings, budget) -> bytes` decodes Flate, LZW (with `EarlyChange`), ASCIIHex, ASCII85, RunLength, and the PNG and TIFF predictors.
  - The image filters (DCT, JPX, CCITTFax, JBIG2) end the chain and return the bytes so far.
  - `MAX_DECODED_SIZE = 50 * 2**20` caps each stream and `budget.total` caps the document. Output stops at a cap, and `budget.cuts` grows by 1 for that stream.
- **Produces, `structure.py`** (from `<dochan>/structure.py`):
  - `PDFFile(data: bytes, password: str = "")` with:
    - `.trailer`, `.catalog`, `.recovered: bool`, `.warnings: list[str]` and `.budget: DecodeBudget`;
    - `.get_object(ref)` and `.resolve(obj)`, which return `None` past `MAX_RESOLVE_DEPTH = 32`;
    - `.pages() -> list[tuple[dict, dict]]` (the page dict, and the inherited `Resources`, `MediaBox`, `CropBox` and `Rotate`);
    - `.decode_stream_bytes(stream)`, `.decode_image_bytes(stream)` and `.decode_form_bytes(stream)`.
  - Caps: `MAX_XREF_SECTIONS = 32`, `MAX_OBJECTS = 500_000`, `MAX_TREE_DEPTH = 64` and `MAX_PAGES = 10_000`, with seen-sets on the page tree and on `/Prev`.
  - Recovery:
    - a bad `startxref` or xref offset falls back to the `N G obj` scan;
    - a file with no catalog after the scan raises `ReadError("damaged", "no catalog")`;
    - the `%PDF-` header may sit up to 1,024 bytes in.
  - An `/Encrypt` trailer raises `ReadError("encrypted", "not supported yet")` until Task 5.
- **Produces, `tests/pdf_fix.py`:**
  - `Name(str)`, `Ref(int)` and `Stream(dict, data: bytes, flate=False)`.
  - `pdf_obj(value) -> bytes` serializes dict, list, `Name`, `Ref`, str (a literal, escaped), bytes (hex), int, float, bool and None.
  - The module inserts `Path(__file__).resolve().parents[1] / "scripts"` into `sys.path` itself, so a caller that loads it by file path (the bench, Task 21) can import `reader`.
  - `make_pdf(objects: dict[int, object], *, root=1, xref="table"|"stream"|"none", objstm=False, trailer=None, header=b"%PDF-1.7", junk=b"", shift=0) -> bytes`. Offsets are true unless `shift` moves every xref offset by that many bytes. `junk` goes before the header. With `objstm=True`, every object other than the streams, the root and the xref goes into one object stream; it requires `xref="stream"`.
  - `text_ops(text, x=72, y=720, size=12, font="F1", extra=b"") -> bytes` gives `BT /F1 12 Tf 72 720 Td (text) Tj ET`.
  - `simple_pdf(pages: list[bytes], *, resources=None, media=(0, 0, 612, 792), catalog=None, page_extra=None, **kw) -> bytes`. It builds the catalog, the page tree and Helvetica `F1`, with each page's content as one stream.
- **Produces, notices:** dochan's `Ported:` line gains "the pdf parser of dochan 1.21.0 (commit 29bb52b) in `scripts/reader/pdfnative/*.py`".

- [ ] **Step 1: Write the failing tests** in `tests/test_reader_pdf_objects.py`:

```python
def test_lexer_objects():          # << /A 1 /B [true null 2.5 (a\)b) <414243> /N#20x] /R 3 0 R >> -> {"A": 1, "B": [True, None, 2.5, b"a)b",
                                   # b"ABC", PDFName("N x")], "R": PDFRef(3, 0)}
def test_classic_xref():           # simple_pdf([text_ops("Hi")]) -> len(PDFFile(data).pages()) == 1, recovered False
def test_xref_stream_and_objstm(): # xref="stream", objstm=True (the page objects in an object stream) -> one page, its content decodes to the text ops
def test_header_after_junk():      # junk=b"x" * 100 -> one page
def test_broken_offsets_recovered():  # shift=7 -> one page, recovered True
def test_no_xref_recovered():      # xref="none" -> one page, recovered True
def test_no_catalog_damaged():     # b"%PDF-1.7\n" + bytes(64) -> ReadError ("damaged", "no catalog")
def test_page_tree_cycle():        # Pages /Kids [3 0 R 2 0 R] where 2 is the Pages node itself -> one page, under 1 s
def test_xref_prev_loop():         # a classic xref whose trailer /Prev points at itself -> opens, under 1 s
def test_resolve_depth():          # 40 objects each "n+1 0 R" -> resolve(first) is None, no RecursionError
def test_flate_bomb():             # zlib of 60 MiB of zeros as a content stream -> decoded length == 50 * 2**20, budget.cuts == 1, under 5 s
def test_document_decode_cap():    # budget total patched to 3 MiB, four 1 MiB streams -> the fourth comes back empty, cuts == 1
def test_lzw_runlength_ascii():    # LZW of b"ABABAB" + "-" * 20, RunLength, ASCIIHex, ASCII85 -> the plain bytes
def test_png_predictor():          # FlateDecode with /DecodeParms << /Predictor 12 /Columns 3 >> -> rows unfiltered
def test_collection_cap():         # an array of 100,001 ints -> 100,000 kept, lexer cut set
def test_deep_nesting():           # "[" * 10_000 -> no RecursionError, value cut at MAX_DEPTH
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_objects.py`. Expected: FAIL with `ModuleNotFoundError: reader.pdfnative`.
- [ ] **Step 3: Implement** the three modules after `<dochan>/objects.py`, `structure.py` and `filters.py`, plus `pdf_fix.py` and the notices line. RunLength is new: dochan has no RunLength decoder.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/pdfnative/__init__.py scripts/reader/pdfnative/objects.py scripts/reader/pdfnative/structure.py scripts/reader/pdfnative/filters.py tests/pdf_fix.py tests/test_reader_pdf_objects.py THIRD_PARTY_NOTICES.md` with the message `reader: pdfnative reads pdf objects, xref tables and streams, object streams and broken files by scan, under dochan's caps (port of dochan 1.21.0)`.

### Task 5: The pdf standard security handler

**Files:**
- Create: `scripts/reader/pdfnative/crypto.py`, `tests/test_reader_pdf_crypt.py`
- Modify: `scripts/reader/pdfnative/structure.py`, `tests/pdf_fix.py`, `THIRD_PARTY_NOTICES.md` (only if the Ported line needs a new path)

**Interfaces:**
- **Consumes:** Task 3's `aes_ecb_*`, `aes_cbc_*` and `rc4`.
- **Produces, `pdfnative/crypto.py`** (from `<dochan>/crypto.py`; dochan's own `utils/aes.py` is replaced by `containers.crypto`):
  - `open_handler(encrypt: dict, id0: bytes, password: str = "") -> StandardSecurityHandler`. It raises:
    - `ReadError("encrypted", "public-key handler")` for `/Filter /Adobe.PubSec`;
    - `ReadError("drm", f"security handler {name}")` for any other filter that is not `/Standard`;
    - `ReadError("encrypted", "user password")` when the empty user password fails.
  - It covers V1, V2, V4 (`/CF`, `/StmF`, `/StrF`, `/V2`, `/AESV2` and `/Identity`) and V5 with R5 and R6 (`/AESV3`, Algorithm 2.B), and `/EncryptMetadata false`.
  - `StandardSecurityHandler.decrypt(num, gen, data, string=False) -> bytes`. An AES IV comes first, and PKCS#7 padding is stripped when valid.
  - `DECRYPT_TOTAL = 16 * 2**20` bytes per handler (decision 4; Task 19 measures it). Past it, `decrypt` raises `ReadError("too_large", "part: decrypt budget")`. A stream or string refused this way consumes none of the budget, so a smaller one after it still decrypts.
- **Produces, `structure.py`:** `PDFFile` opens the handler from `/Encrypt` and the trailer's `/ID`. It decrypts strings and streams of every object except the xref stream and the `/Encrypt` dictionary itself.
- **Produces, `pdf_fix.py`:**
  - `make_pdf(..., encrypt=EncryptSpec(revision, user=b"", owner=b"owner", aes=True|False, metadata=True, filter="Standard"))`.
  - It writes `/Encrypt` and `/ID`, and encrypts every string and stream, using Algorithms 2-5 (R2-R4) and 8-10 (R6) through `containers.crypto`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_r2_rc4():               # simple_pdf text "Hi", EncryptSpec(2, aes=False) -> the page's decoded content holds b"(Hi) Tj"
def test_r3_rc4_128():           # EncryptSpec(3, aes=False) -> the same
def test_r4_aes128():            # EncryptSpec(4) -> the same
def test_r6_aes256():            # EncryptSpec(6) -> the same
def test_strings_decrypted():    # an /Info-free catalog string (Lang (en)) under R4 -> b"en"
def test_identity_crypt_filter():  # V4 with /StmF /Identity -> streams read in the clear
def test_metadata_unencrypted(): # /EncryptMetadata false, a /Metadata stream left clear -> read as is
def test_user_password():        # EncryptSpec(4, user=b"secret") -> ReadError ("encrypted", "user password")
def test_pubsec():               # filter="Adobe.PubSec" -> ReadError ("encrypted", "public-key handler")
def test_other_handler_drm():    # filter="FOPN_foweb" -> ReadError kind "drm"
def test_decrypt_budget():       # DECRYPT_TOTAL patched to 64, R4, content of 200 bytes -> handler.decrypt raises ReadError "too_large";
                                 # a 48-byte stream after it still decrypts (the refused one consumed nothing)
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_crypt.py`. Expected: FAIL with `ReadError: encrypted: not supported yet`.
- [ ] **Step 3: Implement** after `<dochan>/crypto.py`, with `_saslprep` kept for R6 passwords.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/pdfnative/crypto.py scripts/reader/pdfnative/structure.py tests/pdf_fix.py tests/test_reader_pdf_crypt.py` with the message `reader: pdfnative opens RC4 and AES (R2-R6) pdfs with the empty user password; a user password or public-key handler is encrypted, another handler is drm`.

### Task 6: Fonts — CMaps, CID to Unicode, widths, Core 14 and their data

**Files:**
- Create: `scripts/reader/pdfnative/cmap.py`, `cid_unicode.py`, `cid_unicode_data.py`, `predefined_cmap_data.py`, `widths.py`, `core14.py`, `core14_metrics.py`, `fonts.py`, `tests/test_reader_pdf_fonts.py`
- Modify: `tests/pdf_fix.py`, `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- **Produces**, ported from the dochan file of the same name:
  - `cmap.py`: `ToUnicodeCMap`, `parse_tounicode(data, warnings)`, `EncodingCMap`, `parse_encoding_cmap(…)`, `predefined_cmap(name)` and `encoding_wmode(…)`. dochan's caps stay: `MAX_TOUNICODE_BYTES`, `MAX_MAPPING_ENTRIES = 100_000`, `MAX_ENCODING_RANGES` and `MAX_CODESPACE_RANGES`.
  - `cid_unicode.py`: `adobe_cid(ordering, cid) -> str`, `adobe_space_cid(ordering)`, `reverse_truetype_cmap(font_bytes) -> dict[int, int]` (formats 4 and 12, with dochan's caps) and `CIDDecoder`.
  - `widths.py`: `WidthMap`.
  - `core14.py`: `canonical_font(name)`, `glyph_names(base_font, encoding, truetype=False)`, `glyph_unicode(name)` and `Core14Decoder`.
  - The three data modules are copied byte for byte, with their generated headers. Each gains one docstring line naming dochan's file and the commit.
- **Produces, `fonts.py`** (from `<dochan>/reader.py:794-1183`):
  - `FontInfo` is dochan's dataclass (`content.py:219-232`) plus `unmapped: bool = False`. That flag is true for a font whose codes have no ToUnicode entry, no known CID ordering and no usable embedded `cmap`.
  - `font_infos(pdf, resources, cache: dict) -> dict[str, FontInfo]`. Fonts are cached by object ref.
  - `cid_widths`, `simple_widths`, `build_decoder` and `cid_fallback_decoder` are module functions where dochan had methods.
- **Produces, `pdf_fix.py`:**
  - `font_type0(name="F2", *, tounicode: dict[int, str] | None, ordering="Korea1", embedded_cmap: dict[int, int] | None = None, vertical=False) -> dict[int, object]`, the objects of an Identity-H (or Identity-V) Type0 font.
  - `truetype_cmap_font(gid_to_code: dict[int, int]) -> bytes`, a minimal sfnt with `head` and a format-4 `cmap`.
- **Produces, notices:**
  - dochan's `Ported:` line is already covered by Task 4's `pdfnative/*.py` glob.
  - A new `## Adobe` section reproduces from `<dochan>/../../NOTICE`:
    - the mapping-resources-pdf notice and the cmap-resources notice (BSD-3-Clause);
    - the Core 14 AFM notice;
    - the Adobe Glyph List notice (BSD-3-Clause);
    - and it names the three data modules.

- [ ] **Step 1: Write the failing tests.** The tests decode through `font_infos(PDFFile(data), resources, {})[name].decode(code_bytes)`, with no content stream:

```python
def test_helvetica_widths():          # F1 Helvetica: decode(b"Hello") == "Hello"; widths of "A" == 667 (AFM)
def test_winansi_differences():       # /Encoding << /Differences [65 /Euro] >> -> decode(b"A") == "€"
def test_tounicode_bfchar_bfrange():  # ToUnicode bfchar <0001> <D55C>, bfrange <0002> <0003> <AE00> -> decode(b"\x00\x01\x00\x02\x00\x03") == "한글긁"
def test_tounicode_entry_cap():       # MAX_MAPPING_ENTRIES patched to 10, a bfrange of 100 -> the first 10 decode, no hang
def test_cid_korea1_without_tounicode():  # ordering Korea1, no ToUnicode, cid = the first c with adobe_cid("Korea1", c) == "가" ->
                                          # decode(cid as 2 bytes) == "가", unmapped False
def test_predefined_uniks():          # /Encoding /UniKS-UCS2-H, code 0xAC00 -> decode == "가"
def test_truetype_cmap_reverse():     # Identity-H, no ToUnicode, ordering "Identity", embedded truetype_cmap_font({5: 0xD55C}) -> decode(b"\x00\x05") == "한"
def test_unmapped_cid_counts_glyphs():  # Identity-H, no ToUnicode, ordering "Identity", no embedded font -> unmapped True
def test_cid_w_array():               # /W [1 [500 600] 10 20 700] -> widths 1: 500, 2: 600, 15: 700
def test_vertical_wmode():            # Identity-V -> FontInfo.wmode == 1
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_fonts.py`. Expected: FAIL with `ModuleNotFoundError` for `fonts`.
- [ ] **Step 3: Implement** after the named dochan files, and add the Adobe notices.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check. `test_notices_credit_port_sources` must pass.
- [ ] **Step 5: Commit** the nine created files, `tests/pdf_fix.py` and `THIRD_PARTY_NOTICES.md` with the message `reader: pdfnative decodes simple, Core 14, CID and TrueType fonts through ToUnicode, Adobe CID tables and predefined CMaps (port of dochan, with the Adobe notices)`.

### Task 7: Content and text show, rule segments, OCG state

**Files:**
- Create: `scripts/reader/pdfnative/content.py`, `show_table.py`, `ocg.py`, `scripts/reader/formats/pdf_layout.py` (the types only), `scripts/reader/formats/pdf_tables.py` (the types and `PathCollector` only), `scripts/reader/formats/pdf.py` (the caps `PDF_PAGE_OBJECTS` and `PDF_PAGE_IMAGES` only), `tests/test_reader_pdf_content.py`
- Modify: `tests/pdf_fix.py`, `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- **Produces, `pdf_layout.py`:**
  - `Fragment` is dochan's `content.Fragment` fields (`content.py:234-258`) minus `bold`, `italic`, `note_ref`, `comment_spans`, `comment_markers`, `mcids` and `artifact`. It gains:
    - `font: str = ""`;
    - `obj: int = 0`, the index of its text object on the page;
    - `hidden: str = ""`, one of `""`, `"ocg"`, `"invisible"`, `"offcrop"` or `"tiny"`.

    `invisible: bool` (render mode 3 or 7) stays. `writing_direction`, `along` and `across` come with it.
  - `PageImage(box, data: bytes | None, px: tuple[int, int], kind: str)`, where `kind` is `"jpg"`, `"png"`, `"jpx"`, `"ccitt"`, `"jbig2"` or `"raw"`.
  - `Comment(author: str, text: str, hidden: bool)`.
  - `PageData(number, box, rotate, fragments, segments, short, images, figure, links, fields, comments, chars, cut, render)`:
    - `box` is the crop box in page space;
    - `short` is `PathCollector.short_segments`, the sub-1-unit pieces dochan keeps for its short-dash supplement (Task 10);
    - `figure` is `(union box or None, non-rule path count)`;
    - `links` is `[(box, url)]`, `fields` is `[(label, value)]`, and `cut` is a `Counter`;
    - `render` is `Callable[[box, dpi], bytes | None] | None`.
- **Produces, `pdf_tables.py`:**
  - `Segment` and `PathCollector`, from `<dochan>/paths.py`, with `MAX_SEGMENTS` renamed `PDF_SEGMENTS = 20_000` and dochan's `_THIN = 2.0` and `_STRAIGHT = 1.0`.
  - `PathCollector.figure` gives the union box and the count of painted paths that gave no rule segment.
  - `PathCollector.short_segments` keeps the pieces under 1 unit apart from `segments`, as dochan does (`paths.py:87`, `127-136`, `200`).
- **Produces, `formats/pdf.py`:** only `PDF_PAGE_OBJECTS = 100_000` and `PDF_PAGE_IMAGES = 64` (the spec puts pdf caps in `formats/pdf.py`). Task 12 fills the rest of the module. `pdfnative` and `pdf_pdfium` read them as `pdf.<NAME>` at call time, so a test patches `reader.formats.pdf` alone.
- **Produces, `ocg.py`:**
  - `OCState.from_catalog(pdf) -> OCState` reads `/OCProperties /D` (`/BaseState`, `/ON`, `/OFF`).
  - `.hidden(props) -> bool` takes an OCG or OCMD dictionary or ref. An OCMD follows its `/P` policy (`AnyOn` by default, `AllOn`, `AnyOff`, `AllOff`). A `/VE` counts as visible.
  - `.any_off(resources) -> bool` is true when the page's `/Properties`, or a form XObject's `/OC`, names an off group. The pdfium front end uses it (decision 3).
- **Produces, `content.py`** (from `<dochan>/content.py`) and `show_table.py` (from `<dochan>/_show_table.py`):
  - `interpret(pdf, content: bytes, resources, fonts_cache, oc: OCState, *, box) -> PageContent`, where `PageContent(fragments, segments, short, figure, image_draws, objects, cut)` and `short` is `PathCollector.short_segments`.
    - `image_draws` is `[(name, box)]` in content order.
    - `objects` counts the page objects walked, form XObjects included.
  - Text spacing comes from glyph geometry. TJ numbers move the glyphs (`char_offsets`) but never insert spaces: Task 9's gap rule decides spaces (spec fault 1).
  - A fragment inside `/OC /x BDC`, or inside a form whose `/OC` is off, gets `hidden = "ocg"`. Render mode 3 or 7 sets `invisible`.
  - Caps, each counted once on `cut`:
    - `MAX_FORM_DEPTH = 16`, the form seen-set and `MAX_FORM_OPERATORS = 32_768`, under `page_part`;
    - `pdf.PDF_PAGE_OBJECTS`, read at call time, under `page_part`;
    - `PDF_SEGMENTS`, with the rest of the rules ignored and not counted (the spec's row).
- **Produces, `pdf_fix.py`:** `ocg_catalog(off: list[int], on: list[int] = ()) -> dict`, `form_xobject(content: bytes, *, oc=None, matrix=None) -> Stream` and `rule_ops(x0, y0, x1, y1, w=0.5) -> bytes`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_text_fragment():        # text_ops("Hello") -> one Fragment x 72, y 720, size 12, text "Hello", font "Helvetica"
def test_ctm_scales():           # b"2 0 0 2 0 0 cm " + text_ops("A", x=10, y=10) -> x 20, y 20, size 24
def test_tj_moves_not_spaces():  # TJ [(He) -2000 (llo)] -> text "Hello" (no space), char_offsets show a 24-unit jump after "e"
def test_render_mode_invisible():  # "3 Tr" -> invisible True; "7 Tr" too; "0 Tr" False
def test_ocg_off_hidden():       # ocg_catalog(off=[10]) and "/OC /L1 BDC" + text + "EMC" with /Properties /L1 10 0 R -> hidden "ocg"; on -> ""
def test_ocmd_policies():        # OCMD /OCGs [off, on]: AnyOn -> visible, AllOn -> hidden, AnyOff -> hidden, AllOff -> visible; with /VE -> visible
def test_form_xobject():         # a form with text, /Matrix [1 0 0 1 100 0] -> the fragment x moved by 100
def test_form_oc_off():          # a form with /OC naming an off group -> its text hidden "ocg"
def test_form_self_reference():  # a form that Do's itself -> stops at depth 16, cut["page_part"] == 1, under 1 s
def test_form_operator_cap():    # 32,769 "q Q" in one form -> cut["page_part"] == 1
def test_page_objects_cap():     # reader.formats.pdf.PDF_PAGE_OBJECTS patched to 50, 100 text objects -> 50 fragments, cut["page_part"] == 1
def test_rule_segments():        # a horizontal line, a thin filled rect, a diagonal and a flat curve -> 4 Segments (the thin rect gives
                                 # both long edges, as dochan's _paint does; no diagonal)
def test_segment_cap():          # PDF_SEGMENTS patched to 10, 20 lines -> 10 segments, nothing counted
def test_figure_paths():         # 40 small filled circles -> figure count 40, union box over them
def test_image_draws():          # "q 100 0 0 50 72 600 cm /Im1 Do Q" -> image_draws == [("Im1", (72, 600, 172, 650))]
def test_vertical_text():        # an Identity-V font -> writing_direction(frag) == "down"
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_content.py`. Expected: FAIL with `ModuleNotFoundError`.
- [ ] **Step 3: Implement** after `<dochan>/content.py`, `_show_table.py` and `paths.py`. Leave out dochan's line assembly (`assemble_lines`, `_Line`): it moves to `pdf_layout` in Task 9. The notices line gains `scripts/reader/formats/pdf_tables.py` and `scripts/reader/formats/pdf_layout.py` (it holds dochan's `Fragment`).
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** the six created modules, the test file, `tests/pdf_fix.py` and `THIRD_PARTY_NOTICES.md` with the message `reader: pdfnative interprets content streams into fragments, rule segments and figure paths, with OCG and OCMD visibility and dochan's operator caps`.

### Task 8: Annotations, fields, outline and images

**Files:**
- Create: `scripts/reader/pdfnative/annotations.py`, `scripts/reader/pdfnative/images.py`, `tests/test_reader_pdf_annots.py`
- Modify: `tests/pdf_fix.py`

**Interfaces:**
- **Produces, `annotations.py`** (from `<dochan>/annotations.py` and `reader.py:568-650`):
  - `SCHEMES = ("http", "https", "mailto", "ftp")`.
  - `page_annots(pdf, page) -> tuple[list[tuple[box, str]], list[Comment], Counter]`.
    - Links are `/Link` with a `/URI` action of an allowed scheme. Anything else (GoTo, Launch, JavaScript, `javascript:`) gives no link and no count.
    - Comments are Text, FreeText, and the markup kinds with `/Contents`, in file order. A reply (`/IRT`) follows its parent, through `order_comments`. `/Popup` is skipped. `/F` bit 2 gives `hidden=True`.
    - `PDF_ANNOTS = 256` a page and 65,536 characters each. Past either, the rest are dropped and `annotation` is counted once.
  - `order_comments(items) -> list[Comment]`, a pure helper: `items` is `[(key, irt_key or None, Comment)]` in file order, and each reply comes right after its parent. Task 15 calls it.
  - `field_entry(label_tu, name, value, kind) -> tuple[str, str, bool] | None`, a pure helper holding the field rules below; it returns `(label, value, cut)`, or None for an entry left out. `kind` is `text`, `choice`, `check`, `radio`, `sig` or `push`, and a choice `value` may be a list. Task 15 calls it.
  - `fields(pdf, cut: Counter) -> dict[int, list[tuple[str, str]]]`, by 0-based page index (spec fault 4: values come from `/AcroForm /Fields`, not the widgets alone). Each field goes through `field_entry`.
    - The tree walk keeps a seen-set and is capped at depth 32.
    - The label is `/TU`, else the last dot-part of the full name.
    - Text and choice values: newlines become ` / `, a value is cut at 2,000 characters (counted `field` on `cut`), and a list joins with `, `.
    - A check box or radio gives its `/V` name unless `Off`.
    - `/Sig` and push buttons (Ff bit 17) are skipped. Empty values are skipped.
    - The page comes from the widget's `/P`, else from a scan of each page's `/Annots`.
  - `outline(pdf, page_index: dict[ref, int]) -> list[tuple[int, str, int]]`: (depth from 1, title, 0-based page). It is capped at `OUTLINE_ITEMS = 1_000` and `OUTLINE_DEPTH = 32`, with a seen-set on `/First` and `/Next`.
  - `has_xfa(pdf) -> bool` (`/AcroForm /XFA` present) and `has_attachments(pdf) -> bool` (`/Names /EmbeddedFiles`, or any `/FileAttachment` annotation).
- **Produces, `images.py`** (from `<dochan>/images.py`; Pillow is replaced by `reader.images._png`):
  - `image(pdf, stream) -> PageImage`, with `box` set by the caller.
  - DCT keeps its bytes as `jpg`. JPX, CCITT and JBIG2 give `data=None` with their kind.
  - Flate, LZW, RunLength or unfiltered samples become a PNG, for DeviceGray, CalGray, DeviceRGB, CalRGB, ICCBased with `/N` 1 or 3, and Indexed over Gray or RGB. Gray and Indexed take bpc 1, 2, 4 or 8; RGB takes 8. Gray is written as RGB rows, since `_png` writes colour type 2.
  - CMYK, any other space, and over `images._MAX_PIXELS` give `None`.
- **Produces, `pdf_fix.py`:**
  - `link_annot(rect, uri)`, `text_annot(rect, contents, *, author=None, subtype="Text", irt=None, flags=0)` and `popup(parent)`.
  - `acroform(fields: list[dict]) -> dict` plus `widget(rect, page_ref, **field)`.
  - `outline_items(items: list[tuple[int, str, int]]) -> dict[int, object]`.
  - `image_xobject(w, h, *, cs="DeviceGray", bpc=8, data=None, filter="FlateDecode", palette=None) -> Stream`.
  - `JPEG`, a hand-made 8×8 baseline JPEG as bytes.

- [ ] **Step 1: Write the failing tests:**

```python
def test_uri_links():            # link_annot over (72,710,120,730) "https://ex.org/a" -> links == [((72,710,120,730), "https://ex.org/a")]
def test_link_schemes():         # mailto: kept; ftp: kept; javascript:alert(1), a GoTo and a Launch -> no link, nothing counted
def test_comments_kinds():       # Text, FreeText and Highlight with /Contents, author "Ann" -> three Comment("Ann", …, False) in file order
def test_comment_reply_order():  # a reply (/IRT parent) written before its parent -> parent first, then the reply
def test_popup_and_hidden():     # a Popup -> skipped; /F 2 -> Comment hidden True
def test_annot_caps():           # 257 Text annots -> 256 comments, cut["annotation"] == 1; one of 70,000 chars -> cut to 65,536, counted once
def test_field_tu_label():       # /T "f1_01[0]" under parents "topmostSubform[0].Page1[0]", /TU "Name", /V "Kim" -> {0: [("Name", "Kim")]}
def test_field_partial_name():   # no /TU -> label "f1_01[0]"
def test_checkbox_radio():       # Btn /V /Yes -> ("Agree", "Yes"); /V /Off -> no entry; radio parent /V /Two -> ("Pick", "Two")
def test_empty_sig_button():     # empty /V, a /Sig field, a push button -> no entries
def test_field_newlines_and_cut():  # /V "a\nb" -> "a / b"; 2,500 chars -> 2,000 kept, cut["field"] == 1
def test_field_entry_pure():     # field_entry("Name", "a.b", "Kim", "text") == ("Name", "Kim", False); (None, "a.b", "x", "text") -> label "b";
                                 # (None, "c", "Off", "check"), (None, "s", "v", "sig"), (None, "p", "v", "push"), (None, "e", "", "text") -> None;
                                 # (None, "l", ["x", "y"], "choice") -> value "x, y"; 2,500 chars -> cut True
def test_order_comments_pure():  # [("r", "p", reply), ("p", None, parent)] -> [parent, reply]
def test_field_tree_cycle():     # a /Kids cycle -> each field once, under 1 s
def test_xfa_attachment():       # /AcroForm /XFA [] -> has_xfa True; /Names /EmbeddedFiles -> has_attachments True
def test_outline():              # items (1, "Intro", 0), (2, "Part", 1) -> the same list; a /Next cycle -> listed once
def test_image_dct_kept():       # image_xobject with filter DCTDecode and data JPEG -> data == JPEG, kind "jpg"
def test_image_gray_png():       # 40x40 gray Flate -> sniff(data) == "png", px (40, 40)
def test_image_indexed_png():    # Indexed over DeviceRGB, bpc 4 -> a PNG whose first pixel is the palette's entry
def test_image_predictor_rgb():  # RGB Flate with predictor 15 -> a PNG
def test_image_not_viewable():   # JPX, CCITTFax, JBIG2, DeviceCMYK, and 5000x5000 gray -> data None
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_annots.py`. Expected: FAIL with `ModuleNotFoundError`.
- [ ] **Step 3: Implement** after `<dochan>/annotations.py`, `reader.py:568-650` and `images.py`.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/pdfnative/annotations.py scripts/reader/pdfnative/images.py tests/test_reader_pdf_annots.py tests/pdf_fix.py` with the message `reader: pdfnative reads links of four schemes, comments with replies, filled AcroForm values, the outline, and images as JPEG or a stdlib PNG`.

### Task 9: Layout — lines, spacing, joins, order, headings, running headers

**Files:**
- Modify: `scripts/reader/formats/pdf_layout.py`, `scripts/reader/formats/xps.py` (`columns` lifted to a module function), `THIRD_PARTY_NOTICES.md`
- Create: `scripts/reader/formats/pdf_spacing.json` (a copy of `<dochan>/korean_spacing.json`), `tests/test_reader_pdf_layout.py`

**Interfaces:**
- **Consumes:** Task 7's `Fragment`; `xps._columns`, which this task lifts out of `_Reader.columns`.
- **Produces, `xps.py`:** `_columns(runs, width) -> list[list[run]]` is the body of `_Reader.columns` as a module function, and the method calls it. XPS output is unchanged.
- **Produces, `pdf_layout.py`** (docstring names dochan `layout.py`, `spacing.py`, `running.py` and `reader.py:85-100`):
  - `SpacingModel` and `load_model(path=None) -> SpacingModel`. The model loads `pdf_spacing.json` once per process, and `joins_with_space(last, first) -> bool` is dochan's.
  - `rotate_into_view(fragments, rotate, box)` maps each fragment's position and direction through `/Rotate` (decision 14).
  - `Line(text, runs: list[tuple[str, str]], box, size, order, vertical: bool)`, where a run is `(text, url)`.
  - `lines(fragments, links=()) -> list[Line]`. Hidden fragments are never passed in.
    - A space goes between two glyphs when the gap is over `0.2 * size` and neither side is a space already. `GAP = 0.2`.
    - A run whose glyph centres lie in a link box carries its url.
  - `vertical_lines(fragments) -> list[Line]`: columns right to left, characters top to bottom, and one `Line` per column (spec fault 3).
  - `reading_order(lines, width) -> list[Line]`, where `width` is the page box width. Content order stays, unless more than 30% (`UPWARD = 0.3`) of line-to-line steps go up by more than one line height. Then the lines go to `xps._columns` as XPS runs `(box_top_from_top, x, size, text, i)`, with the line's index `i` in the name slot. Each column comes back sorted by (top, x) and is mapped back to its `Line`s by index, so links and runs are kept. `xps._paras` is not used.
  - `join_break(left: str, right: str, model) -> str` applies, in order:
    1. a line-end `-` after a letter, followed by a lowercase letter, is dropped and the word joined;
    2. Han or Kana on both sides: no space;
    3. Hangul on both sides: `model.joins_with_space(left[-1], right[0])`;
    4. otherwise one space.
  - `paragraphs(lines, model) -> list[Para]` breaks on a vertical gap over 1.5 × the line height, with no full-line rule. Lines join through `join_break`, links become `Link` inlines, and nothing is bold.
  - `body_size(all_lines) -> float` is the median line size.
  - `heading_level(text, size, body) -> int | None`: 3 when `size >= 1.5 * body`, 4 when `>= 1.25 * body`, and None past 120 characters or with fewer than 2 letters.
  - `outline_level(depth) -> int` is `min(6, depth + 2)`.
  - `running(pages: list[list[Line]], boxes) -> tuple[list[str], list[str], list[set[int]]]` (headers, footers, the line indexes to drop on each page), after dochan `detect_running`. Page-number-only lines are dropped without becoming a header.
- **Produces, notices:** the Ported line gains `pdf_spacing.json` (Task 7 already added `scripts/reader/formats/pdf_layout.py`).

- [ ] **Step 1: Write the failing tests.** Fragments are built directly with a helper `frag(text, x, y, size=12, **kw)`:

```python
def test_space_by_gap():         # "Hel" ending at x 90, "lo" at 91 -> "Hello"; "lo" at 93 -> "Hel lo" (gap 3 > 2.4)
def test_no_double_space():      # "a " then "b" 5 apart -> "a b"
def test_letter_spaced():        # one-glyph fragments 0.5 apart spelling WORD, a 4.0 gap, then ONE -> "WORD ONE"
def test_join_latin():           # join_break("end of", "line", m) == "end of line"
def test_join_han_kana():        # ("中文", "字") -> "中文字"; ("かな", "カナ") -> "かなカナ"
def test_join_hangul_model():    # m = SpacingModel(prior=0.25, pairs={"의운": 0.9, "안보": 0.5}): ("…의", "운…") -> space; ("…안", "보…") -> none
def test_spacing_table_loads():  # load_model(): 0 < prior < 1 and len(pairs) > 1000
def test_hyphen_join():          # ("inter-", "national") -> "international"; ("well-", "Known") -> "well- Known"; ("1-", "2") -> "1- 2"
def test_paragraph_break():      # three lines 14 apart then one 40 below -> two Paras
def test_content_order_kept():   # a left column then a right column, in content order -> left text first
def test_column_fallback():      # lines drawn alternating left and right rows, three a side (every step goes up), reading_order(lines, 612) ->
                                 # left column's text, then the right's
def test_vertical_cjk():         # two vertical columns at x 500 and 480 -> the x-500 column's text first, each column one line, no spaces
def test_rotated_page_order():   # a /Rotate 90 page whose three Latin lines are drawn rotated -> lines in reading order, words whole
def test_heading_by_size():      # body 10: a 15 line -> 3; 12.5 -> 4; 12 -> None; a 130-char 15 line -> None
def test_outline_level():        # outline_level(1) == 3, (2) == 4, (9) == 6
def test_running_header_footer():  # pages 1-3 share top line "ACME report" and bottom "Page n" -> headers == ["ACME report"], footers == [],
                                   # each page drops both lines
def test_link_runs():            # a link box over "here" in "see here now" -> runs [("see ", ""), ("here", "https://x"), (" now", "")]
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdf_layout.py`. Expected: FAIL with `AttributeError` for `lines`.
- [ ] **Step 3: Implement** after the named dochan files. Copy the json byte for byte. `code_digest` picks it up in Task 13.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check. `test_reader_xps.py` must still pass unchanged.
- [ ] **Step 5: Commit** `scripts/reader/formats/pdf_layout.py scripts/reader/formats/xps.py scripts/reader/formats/pdf_spacing.json tests/test_reader_pdf_layout.py THIRD_PARTY_NOTICES.md` with the message `reader: pdf layout joins glyphs by geometry, lines by script (dochan's Korean spacing table), sorts columns only when content order runs backwards, and finds headings and running headers`.

### Task 10: Tables from rule segments

**Files:**
- Modify: `scripts/reader/formats/pdf_tables.py`
- Create: `tests/test_reader_pdf_tables.py`

**Interfaces:**
- **Consumes:** Task 7's `Segment` and `PathCollector.short_segments` (`PageData.short`), and Task 9's `lines`, `paragraphs` and `load_model`.
- **Produces, `pdf_tables.py`** (docstring names dochan `paths.py`, `tables.py` and `connected_tables.py`):
  - `TableBudget(remaining=MAX_DOCUMENT_CELLS)`.
  - `tables(segments, fragments, page_number, budget, cut: Counter, short=()) -> list[Found]`, where `short` feeds dochan's short-dash supplement (`tables.py:442-451`, `473`, `501`, `524`), and `Found(table: model.Table, bbox, used: set[int])` and `used` holds the `order` of the fragments the table took.
    - Each cell is `Cell([Para…], rowspan, colspan)`. Its text goes through `pdf_layout.lines` and `paragraphs`, so a Korean cell joins by the table.
    - A nested table is a `Table` inside its cell's blocks, as in dochan.
    - A grid with fewer than 2 rows or 2 columns is not a table.
  - Caps, each counted once under `cells`: dochan's `MAX_PAGE_CELLS = 50_000`, `MAX_DOCUMENT_CELLS = 200_000`, `MAX_INTERSECTION_CHECKS = 2_000_000`, `MAX_PAGE_COMPONENTS = 1_000`, `MAX_COMPONENT_LINES = 2_000`, `MAX_NESTED_DEPTH = 32` and `MAX_SPLIT_CHECKS = 2_000_000`, plus `limits.TABLE_NODES` charged per table node.

- [ ] **Step 1: Write the failing tests**, with segments and fragments built directly:

```python
def test_ruled_grid():           # a 3x2 grid of rules, one word per cell -> one Table, rows [["a","b"],["c","d"],["e","f"]], used covers all six
def test_colspan():              # the inner vertical rule missing in row 1 -> row 1 one Cell colspan 2
def test_rowspan():              # the inner horizontal rule missing in column 1 -> a Cell rowspan 2
def test_nested_table():         # a 2x2 grid inside cell (0,0) of a 2x2 grid -> that cell's blocks hold a Table
def test_rule_light_stays_text():  # horizontal rules only -> []
def test_cell_korean_join():     # one cell with lines "가나" and "다라", m from load_model() -> the cell text follows join_break with that model
def test_page_cell_cap():        # MAX_PAGE_CELLS patched to 3, a 2x2 grid -> no table, cut["cells"] == 1
def test_intersection_cap():     # MAX_PAGE_CELLS raised to 10**7, 1,500 horizontal and 1,500 vertical rules (2.25 M checks) ->
                                 # returns under 3 s, cut["cells"] == 1
def test_component_lines_cap():  # MAX_COMPONENT_LINES patched to 4, a 3x3 grid (8 rules) -> no table, cut["cells"] == 1
def test_dash_rules():           # a grid drawn in 0.5-unit dashes, passed as short= -> one Table (dochan's short-dash supplement)
```

- [ ] **Step 2: Run the tests to see them fail.** Expected: FAIL with `AttributeError` for `tables`.
- [ ] **Step 3: Implement** after `<dochan>/tables.py` and `connected_tables.py`, with dochan's `Table` replaced by `model.Table` and `model.Cell`.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/formats/pdf_tables.py tests/test_reader_pdf_tables.py` with the message `reader: pdf tables come from rule segments by dochan's grid assembly, with merged cells, nested tables and its caps`.

### Task 11: The native document front end

**Files:**
- Create: `scripts/reader/pdfnative/document.py`, `tests/test_reader_pdf_native.py`

**Interfaces:**
- **Consumes:** Tasks 4-8: Task 7's `PDF_PAGE_IMAGES` in `formats/pdf.py`, and Task 8's `fields(pdf, cut)`.
- **Produces, `document.py`** (docstring names dochan `reader.py:651-793` and `1184-1220`; the page loop is `formats/pdf.py`'s):
  - `NativeDoc(data: bytes)` raises the structure and crypto `ReadError`s. A decrypt-budget `too_large` met while opening (object streams, strings) is re-raised as `ReadError("damaged", "decrypt budget")`, so the gate passes it through. It has:
    - `.page_count`;
    - `.oc: OCState`;
    - `.resources(index) -> dict`, the page's inherited `Resources` (from `pdf.pages()[index][1]`). Task 15 uses it for decision 3;
    - `.outline() -> list[tuple[int, str, int]]`;
    - `.fields: dict[int, list]`, read once through `fields(pdf, self.cut)`;
    - `.xfa: bool` and `.attachments: bool`.
  - `.page(index: int) -> PageData` fills:
    - `fragments` from `content.interpret` over the page's content parts. The part walk follows `reader.py:768-793`: at most `MAX_CONTENT_PARTS = 256` parts and `MAX_PAGE_CONTENT_BYTES = 64 * 2**20` bytes of decoded content a page; past either the rest are dropped and `page_part` counted once;
    - `segments`, `short` and `figure`;
    - `images`, one per `image_draws` entry, through `images.image`, with the draw box. At most `pdf.PDF_PAGE_IMAGES`, read at call time; the rest count `image`.
    - `links` and `comments` from `annotations.page_annots`, and `fields` from `self.fields`;
    - `chars` as the count of non-space chars;
    - `rotate` and `box` as the crop box (defaulting to the media box, inherited), and `render = None`.
  - `.cut` gathers the page's counts: `page_part`, `image`, `annotation`, `field`, and `glyphs` (once per unmapped font ref per document).

- [ ] **Step 1: Write the failing tests:**

```python
def test_native_page_text():     # simple_pdf two pages -> page_count 2, page(1).fragments[0].text, number 2
def test_native_crop_inherited():  # /CropBox on the Pages node -> page(0).box == that box
def test_native_rotate():        # /Rotate 90 -> page(0).rotate == 90
def test_native_images():        # one Im1 draw of a gray Flate image -> images == [PageImage(draw box, png bytes, (40, 40), "png")]
def test_native_image_cap():     # 70 draws of one image -> 64 images, cut["image"] == 1
def test_native_content_parts_cap():  # /Contents of 300 parts -> the first 256 read, cut["page_part"] == 1; MAX_PAGE_CONTENT_BYTES patched
                                      # to 100, two 80-byte parts -> the first read, cut["page_part"] == 1
def test_native_resources():     # /Resources on the Pages node -> resources(0) is that dict
def test_native_open_decrypt_budget():  # DECRYPT_TOTAL patched to 64, R4 with objstm=True (an object stream over 64 bytes) ->
                                        # ReadError ("damaged", "decrypt budget")
def test_native_fields_comments():  # an AcroForm field on page 1 and a Text annot -> page(0).fields and .comments filled;
                                    # a 2,500-char value -> cut["field"] == 1
def test_native_glyphs_once():   # an unmapped Type0 font used on two pages -> cut["glyphs"] totals 1
def test_native_outline():       # outline_items on pages 0 and 1 -> outline() as built
```

- [ ] **Step 2: Run the tests to see them fail.** Expected: FAIL with `ModuleNotFoundError`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/pdfnative/document.py tests/test_reader_pdf_native.py` with the message `reader: pdfnative gives each page as PageData (fragments, rules, figure, images, links, fields, comments)`.

### Task 12: `formats/pdf.py` — the page loop, hidden rules, scans, figures and caps

**Files:**
- Modify: `scripts/reader/formats/pdf.py` (Task 7 created it with two caps), `scripts/reader/__init__.py` (`STARTED` only)
- Create: `tests/test_reader_pdf.py`

**Interfaces:**
- **Consumes:** Tasks 2 and 7-11.
- **Produces, `__init__.py`:** `STARTED = time.monotonic()` at import, nothing else. Task 13's `convert` passes it.
- **Produces, `pdf.py`:**
  - The caps, after Task 7's `PDF_PAGE_OBJECTS = 100_000` and `PDF_PAGE_IMAGES = 64`, which stay here:

    ```python
    PDF_PAGES = 5_000
    PDF_DEADLINE = 36.0
    PDF_PAGE_SECONDS = 4.0
    PDF_PAGE_CHARS = 200_000
    PDF_RENDERS = 100
    RENDER_DPI, CROP_DPI, RENDER_LONG = 110, 150, 1_568
    SCAN_COVER = 0.5
    OCR_COVER = 0.8
    FIGURE_PATHS = 32
    FIGURE_COVER = 0.04
    FIGURE_PAD = 6.0
    FIGURE_WHOLE = 0.7
    TINY_PT = 1.0
    IMAGE_MIN_PX = 32
    IMAGE_MIN_PT = 16.0
    ```

  - `parse(path, ctx, *, engine="native", pages=None, clock=time.monotonic, started=None) -> Doc` reads the file and calls `parse_bytes`.
  - `parse_bytes(data, ctx, *, engine="native", pages=None, clock=time.monotonic, started=None) -> Doc`.
    - When `started` is None, it takes `clock()` at entry. Only `reader.convert` (the CLI child, Task 13) passes `reader.STARTED`, so an in-process caller never inherits a stale clock.
    - `pages=(A, B)` reads those pages alone, numbered as in the file.
    - `engine="pdfium"` is wired in Task 15. Until then, any other engine raises `ValueError`.
  - **Open:** `NativeDoc(data)`. Its `encrypted`, `drm` and `too_large` errors propagate; a decrypt budget spent while opening arrives as `damaged` (Task 11). A `damaged` error is final for native. Task 15 lets pdfium try first.
  - **Page loop**, for index in range:
    - Past `PDF_PAGES`, or when `clock() - started > PDF_DEADLINE`, the rest count `page_cut` (one per page) and the loop ends with the output kept.
    - Each page runs under `_page_guard("page")` (decision 5).
    - The steps (text and layout, then tables, then images, then renders) are separated by `PDF_PAGE_SECONDS` checks against the page's start. At a failed check the steps left are skipped and `page_part` counted once.
  - **Hidden rules**, applied to fragments before layout:
    - `ocg` stays as given.
    - The box wholly outside `PageData.box` gives `offcrop`; size under `TINY_PT` gives `tiny`.
    - `invisible` with its centre under no image gives `invisible`.
    - Invisible text over an image covering at least `OCR_COVER` of the page is read as normal, and that image is left out uncounted.
    - Hidden fragments form one hidden `Para` per `obj` (decision 6).
  - **Chars:** past `PDF_PAGE_CHARS`, the rest of the page's fragments are dropped, counted `text` once.
  - **Scan page:** `chars == 0` and one image covering at least `SCAN_COVER`.
    - pdfium: `render(None, RENDER_DPI)`.
    - native: the image's own bytes when viewable.
    - Either gives `Image(data, alt=f"Scanned page {N}")`. Otherwise the page goes on `unrendered`, counted `scan`.
  - **Figure:** at least `FIGURE_PATHS` non-rule paths covering at least `FIGURE_COVER`.
    - The box is the union grown by `FIGURE_PAD`. Over `FIGURE_WHOLE` of the page, or on a page with no text and only paths, it is the whole page.
    - It is rendered at `CROP_DPI` with the long side at most `RENDER_LONG`, and placed before the first block whose top is below the figure's top. The page goes on `figures`.
    - With no render, the page goes on `unrendered`, counted `figure` (decision 7).
  - **Render budget:** a render past `PDF_RENDERS`, or past `limits.IMAGE_BYTES` with the images, puts the page on `unrendered` with `scan` or `figure`.
  - **Images:** under `IMAGE_MIN_PX` pixels or `IMAGE_MIN_PT` points on a side, left out uncounted. A `None` data counts `image`. The rest become `Image` blocks in reading order (`Assets` dedups).
  - **Tables:** `pdf_tables.tables(…, short=page.short)`, then the fragments in `used` leave layout.
  - **Lines:** `pdf_layout` lines and `reading_order(lines, box width)`. Each page's `Line`s are kept, with its tables and images, for the pass after the loop; paragraphs are not built inside the loop.
  - **Section:** `Section("page", number=N, blocks=…, comments=[Para([Text(f"Field {l}: {v}")]) …] + [Para([Text(f"Comment by {a}: {t}" if a else f"Comment: {t}")], hidden="hidden" if h else "") …])`.
  - **After the loop**, in this order:
    1. `running` over the kept lines fills `Doc.headers` and `Doc.footers` and drops their lines.
    2. Outline titles on their page promote the first matching line (case-folded, spaces squashed) to `Heading(outline_level(depth))`. Other lines get `heading_level(text, size, body_size)`. A heading line is split out of the page's lines here, before paragraphs merge it.
    3. `paragraphs` per page over the lines between headings. The page's blocks are its headings, paragraphs, tables and images placed by their top, then the hidden `Para`s (decision 6), filling the `Section`.
    - The outline is never printed.
  - **Named items:** `xfa` and `attachment` are counted 1 when present.
  - **No page read:** `ReadError("damaged", "no readable pages")`.
  - `Doc.meta = {"engine": <engine used>, "pages_total": n, "unrendered": [N…], "figures": [N…]}`.

- [ ] **Step 1: Write the failing tests.** `md(data, hidden=False, **kw)` runs `parse_bytes` and `markdown.render` into a temporary assets dir, giving `(markdown, ctx, doc)`:

```python
def test_pages_numbered():       # 3 pages, the second empty -> "## Page 1", "## Page 2", "## Page 3" in order
def test_hidden_rules():         # off-crop text, 0.5 pt text, Tr 3 text with no image, OCG-off text -> none in md; first line
                                 # "(4 hidden places left out)"; hidden=True -> all four, each marked "(hidden)"
def test_hidden_per_text_object():  # one BT…ET of three Tj, all Tr 3 -> 1 hidden place
def test_ocr_layer():            # a full-page gray image and Tr 3 text over it -> the text read, no image link, nothing hidden
def test_scan_native_jpeg():     # no text, a full-page DCT image -> "![Scanned page 1](assets/img-001.jpg)"
def test_scan_native_undecodable():  # no text, a full-page CCITT image -> meta unrendered [1], skipped["scan"] == 1
def test_figure_native_unrendered():  # 40 filled paths over 10% of the page -> unrendered [1], skipped["figure"] == 1
def test_renders_cap_unrendered():   # PDF_RENDERS patched to 2, 5 scanned DCT pages (native "renders" are its own images) -> 2 images,
                                     # unrendered [3, 4, 5], skipped["scan"] == 3
def test_image_floor_dedup():    # a 20x20 image, an image drawn 10 pt wide, and one 64x64 image drawn twice -> one img file, one link per draw
def test_fields_comments_end():  # a field and a comment on page 1 -> "> Field Name: Kim\n> Comment by Ann: note" after the page's text
def test_hidden_comment():       # /F 2 -> left out, counted hidden
def test_links_inline():         # a link box over "here" -> "[here](https://ex.org)"
def test_table_on_page():        # a ruled 2x2 grid with text -> a pipe table, its words not repeated as paragraphs
def test_outline_headings():     # outline (1, "Intro", 0) and a body-size line "Intro" -> "### Intro"; no outline list anywhere
def test_size_heading():         # body 10 pt, one 16 pt line -> "### " + that line
def test_running_headers():      # 3 pages with the same top line -> it appears once at the top, as the renderer's header line
def test_page_cap():             # PDF_PAGES patched to 3, 5 pages -> pages 1-3, skipped["page_cut"] == 2
def test_deadline_page_cut():    # clock advancing 10 s per call, started 0 -> the pages read before 36 s kept, the rest page_cut, no ReadError
def test_page_seconds_part():    # clock jumping 5 s inside the text step -> text kept, tables and images skipped, skipped["page_part"] == 1
def test_page_chars_cap():       # PDF_PAGE_CHARS patched to 10, 50 chars -> skipped["text"] == 1
def test_page_raises_skipped():  # NativeDoc.page patched to raise for index 1 -> pages 1 and 3 read, skipped["page"] == 1
def test_member_readerror():     # R4, DECRYPT_TOTAL patched below page 2's stream and above pages 1 and 3's (a refused stream consumes
                                 # nothing) -> skipped counts it, page 3 read; a page step raising ReadError("timeout") -> it propagates
def test_started_default():      # started=None, clock always reader.STARTED + 100 -> every page read, no page_cut (the deadline runs from
                                 # parse_bytes' entry, not from import)
def test_no_readable_page():     # every page raises -> ReadError ("damaged", "no readable pages")
def test_pages_range():          # pages=(2, 3) of 5 -> only "## Page 2" and "## Page 3", meta pages_total 5
def test_xfa_attachment_named(): # both present -> "XFA form" and "attached files" in the could-not-read line
def test_encrypted_kinds():      # user password -> ReadError "encrypted"; a FOPN handler -> "drm"
def test_broken_xref_read():     # shift=7 -> the text is read
```

  `test_unmapped_font_garble_warning` goes through `reader.convert`, which needs pdf in `BUILT`, so it is written in Task 13.

- [ ] **Step 2: Run the tests to see them fail.** Expected: FAIL with `AttributeError` for `parse_bytes` (Task 7's `pdf.py` holds only two caps).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/formats/pdf.py scripts/reader/__init__.py tests/test_reader_pdf.py` with the message `reader: pdf pages become "## Page N" sections with tables, images, fields and comments; hidden text, scans, figures and every cap counted`.

### Task 13: pdf is built — the format table, convert, cache key and CLI

**Files:**
- Modify: `scripts/reader/__init__.py`, `scripts/reader/cache.py`, `scripts/read.py`, `README.md` (the built list only), `tests/test_reader_cache.py`, `tests/test_reader_hwpx.py`, `tests/test_reader_zipx.py`, `tests/test_reader_gate.py` (`:591-592` only), `tests/test_reader_pdf.py` (`test_unmapped_font_garble_warning`, moved here from Task 12)

**Interfaces:**
- **Produces, `__init__.py`:**
  - `FORMATS["pdf"] = "pdf"` and `"pdf" in BUILT`, for 41. `SKILL_EXTS` is deleted. `VERSION = "0.5.0"`.
  - `STARTED = time.monotonic()` at import is already there (Task 12). `convert` passes `started=STARTED` to pdf's `parse`; no other caller does, and `pdf.parse_bytes` takes `clock()` at entry when `started` is None.
  - `parse(path, ctx, **opts)` passes `opts` to the format's `parse` only when given.
  - `convert(src, out_dir, hidden=False, **opts)`. When `doc.meta` has an `engine`, `meta.json` gains:
    - `engine`, `pages_total`, `unrendered` and `figures` from it;
    - `pages` from `markdown.stats(md)["pages"]`.
- **Produces, `cache.py`:**
  - `code_digest` hashes `*.py` and `*.json` under `reader/`.
  - `key(path, engine="")` hashes `engine + "\0"` between the code digest and the bytes, only when `engine` is not empty. Every other format's key is unchanged.
  - `store(k, src, hidden=False, **opts)`.
  - `prune(days)` also removes files under `root()/offers` and `root()/failed` older than one day. It never touches `pdf-declined`, `pdf-venv.json` or anything outside the root.
- **Produces, `read.py convert`:**
  - It gains `--engine {pdfium,native}` and `--pages A-B`.
  - For pdf, the engine actually used comes first, and then the key:
    - `--engine pdfium` with a failed `import pypdfium2` falls back to `native`;
    - the key is `cache.key(src, engine_id)`, where `engine_id` is `pdfenv.engine_id_from_module()` for pdfium (Task 14) and `"native"` otherwise;
    - `-p{A}-{B}` is added for a range, and then `-hidden`.
  - Until Task 14 lands, pdfium is never chosen: the engine is native.
- **Produces, `README.md`:** `` `pdf` `` joins the "Built formats:" list. Task 17 writes the pdf paragraph.

- [ ] **Step 1: Write the failing tests:**
  - `test_reader_cache.py:259-262` loses the `x.pdf` unsupported case.
  - `test_cli_formats` expects 41.
  - `test_reader_zipx.py:35` becomes `assert len(reader.FORMATS) == 41 and not hasattr(reader, "SKILL_EXTS")`.
  - `test_reader_hwpx.py:64` expects 41.
  - `test_reader_gate.py:591-592`: the damaged `b"%PDF-1.7\n" + bytes(64)` file now expects a deny (pdf is built). Task 16 changes it again, to pass-through.
  - Add:

```python
# tests/test_reader_cache.py
def test_key_engine():           # k0 = key(p); key(p, "native") != k0; key(p, "pdfium-5.15.2-7000") not in (k0, key(p, "native"))
def test_code_digest_json():     # code_digest(base) changes when a .json file under base changes, not when a .txt does
def test_prune_markers():        # offers/x and failed/y aged 2 days -> removed; aged 1 hour -> kept; pdf-declined aged 2 days -> kept
def test_cli_convert_pdf():      # simple_pdf([text_ops("Hi")]) saved as x.pdf -> returncode 0; out.md has "## Page 1" and "Hi"; meta engine "native",
                                 # pages [[1, n]], pages_total 1
def test_cli_pages_range():      # 4 pages, --pages 2-3 -> "## Page 2" and "## Page 3" only; the entry's folder name ends "-p2-3"
def test_cli_pdfium_without_module():  # --engine pdfium under sys.executable (no pypdfium2) -> native output, meta engine "native",
                                       # entry == cache.key(src, "native")
# tests/test_reader_pdf.py (moved from Task 12)
def test_unmapped_font_garble_warning():  # through reader.convert into a temp dir: an unmapped Type0 font -> "1 text run without Unicode"
                                          # in unread, and "garbled" in meta warnings when its output is U+FFFD
```

- [ ] **Step 2: Run the tests to see them fail.** Run the three files and `tests/test_reader_pdf.py`. Expected: FAIL, with `len(FORMATS) == 40`.
- [ ] **Step 3: Implement**, and add `` `pdf` `` to the README's built list.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check. `test_reader_gate.py` must pass too: the damaged pdf now denies, and `test_readme_formats_match` finds `` `pdf` ``.
- [ ] **Step 5: Commit** `scripts/reader/__init__.py scripts/reader/cache.py scripts/read.py README.md tests/test_reader_cache.py tests/test_reader_hwpx.py tests/test_reader_zipx.py tests/test_reader_gate.py tests/test_reader_pdf.py` with the message `reader: pdf is built (41 formats); convert takes --engine and --pages, the cache key holds the engine id, and the code digest covers json data; version 0.5.0`.

### Task 14: `pdfenv`, `read.py setup` and the pinned requirements

**Files:**
- Create: `scripts/reader/pdfenv.py`, `scripts/reader/pdfsetup.py`, `scripts/reader/requirements-pdf.txt`, `tests/test_reader_pdfenv.py`
- Modify: `scripts/read.py`, `tests/pdf_fix.py`

**Interfaces:**
- **Produces, `requirements-pdf.txt`:**
  - `pypdfium2==5.15.2 \` followed by one `--hash=sha256:<64 hex>` line for each of the seven `py3-none` platform wheels: win_amd64, win_arm64, macosx arm64, macosx x86_64, manylinux x86_64, manylinux aarch64, musllinux x86_64.
  - Read the digests from `https://pypi.org/pypi/pypdfium2/5.15.2/json` (`urls[].digests.sha256` with `filename`; a web read, not a download), and name each wheel in a comment line above its hash.
- **Produces, `pdfenv.py`** (stdlib, light: no import beyond `os`, `sys`, `json`, `hashlib`, `pathlib` at module level):
  - `venv_dir() -> Path` takes the first that is set:
    1. `PHRO_READER_PYENV`, unless `off`;
    2. `$CLAUDE_PLUGIN_DATA/pdf-venv`;
    3. the folder in `cache.root() / "pdf-venv.json"`;
    4. the default `<cache base>/pdf-venv`, where `<cache base>` is the parent of `cache.root()`.
  - `forced_native() -> bool` is true for `PHRO_READER_PYENV=off`.
  - `interpreter(venv) -> Path` is `Scripts/python.exe` on Windows and `bin/python3` elsewhere.
  - `REQUIREMENTS` is the path of `requirements-pdf.txt`. `requirements_hash() -> str` is the sha256 of its bytes.
  - `state(venv) -> str` is the cheap check: `"READY"`, `"MISSING"`, `"STALE"` (the hash differs) or `"BROKEN"` (no interpreter, a `ready.json` that does not parse, or a `pyvenv.cfg` `home` that does not exist).
  - `engine_id(version, build) -> str` is `f"pdfium-{version}-{build}"`.
  - `engine(venv=None) -> str` is the engine id from `ready.json` when READY and not forced native, else `"native"`.
  - `engine_id_from_module() -> str` is used in the venv child, from `pypdfium2.version.PYPDFIUM_INFO` and `PDFIUM_INFO.build`.
  - `verify(venv) -> bool` is the full check: both `RECORD` sha256 values match `ready.json`, and every file listed in them with a digest hashes to it.
  - `declined() -> bool` is true when `cache.root() / "pdf-declined"` exists.
- **Produces, `pdfsetup.py`:**
  - `Steps` holds the replaceable steps: `make_venv(tmp)`, `pip(python, requirements) -> (ok, output)`, `versions(python) -> (version, build, records: dict)` and `smoke(python) -> bool`.
  - `setup(venv, steps=Steps()) -> str` returns one line, `READY\t<dir>\t<engine id>` or `FAIL\t<step>\t<detail>`.
    1. Take `DIR.lock`, created exclusively. A lock older than 600 s is stale and removed. A fresh lock gives `FAIL\tlock\tanother setup is running`.
    2. Remove leftover `DIR.tmp-*` folders older than 600 s.
    3. Run `make_venv(DIR.tmp-<pid>)`, which is `sys.executable -m venv`.
    4. Run `pip` with `-m pip install --no-input --disable-pip-version-check --only-binary :all: --no-deps --require-hashes -r <REQUIREMENTS>`. Output naming no matching distribution gives `FAIL\tpip\tno wheel for this platform`.
    5. Run `versions`, then `smoke`, which writes `_smoke_pdf()` to `DIR.tmp…/smoke.pdf`, runs `<python> -I -X utf8 read.py convert smoke.pdf --engine pdfium --out smoke-out`, checks `phro smoke` in its out.md, and removes both.
    6. Write `ready.json` last: `requirements`, `python` (version and home), `pypdfium2`, `pdfium` and `records`.
    7. Move an old `DIR` aside, rename the tmp folder to `DIR`, remove the old one, and write the pointer file `{"venv": DIR}`.
    8. Remove `pdf-declined`.
    - On any FAIL, the tmp folder is removed and `DIR` is untouched. The lock is always released.
  - `check(venv) -> str` gives `READY\t<dir>\t<engine id>`, or `MISSING`, `STALE` or `BROKEN` with `\t<dir>`.
  - `decline() -> str` writes `pdf-declined`. `remove(venv) -> str` deletes the venv and the pointer.
  - `_smoke_pdf() -> bytes` is a one-page Helvetica pdf of `phro smoke`, with true offsets.
- **Produces, `read.py`:**
  - `setup [--venv DIR] [--check] [--decline] [--remove]` prints the line and exits 0 on READY or a check, 1 on FAIL.
  - `convert` of a `.pdf` with no `--engine`:
    - when `pdfenv.state(venv_dir()) == "READY"` and `sys.executable` is not that interpreter, it re-runs itself as `[interpreter, "-I", "-X", "utf8", READ_PY, "convert", …, "--engine", "pdfium"]` and relays the output and exit code;
    - otherwise it uses native.
- **Produces, `tests/pdf_fix.py`:** `fake_venv(dir, *, build=7000) -> Path`, a fake READY venv for this file's tests and Task 16's: an interpreter file, `pyvenv.cfg` whose `home` is an existing dir, `RECORD` files for `pypdfium2` and `pypdfium2_raw` listing one file each with its digest, and `ready.json` (the current requirements hash, 5.15.2, `build`, the two `RECORD` digests). `state` is READY and `verify` is True on it.

- [ ] **Step 1: Write the failing tests.** Each builds its folders in a temporary dir, with `PHRO_READER_CACHE`, `PHRO_READER_PYENV` and `CLAUDE_PLUGIN_DATA` set and restored by a context manager:

```python
def test_venv_dir_order():       # PYENV=X -> X; unset + PLUGIN_DATA=D -> D/pdf-venv; both unset + pointer {"venv": P} -> P; none -> cache base/pdf-venv
def test_off_forces_native():    # PYENV=off -> forced_native() and engine() == "native" even with a READY venv at the default place
def test_ready_engine_id():      # pdf_fix.fake_venv(DIR) (interpreter file, pyvenv.cfg home = an existing dir, ready.json with the current
                                 # hash, 5.15.2, build 7000) -> state READY, verify True, engine() == "pdfium-5.15.2-7000"
def test_stale_after_pin_change():  # ready.json requirements "0" * 64 -> STALE, engine native
def test_broken_home():          # pyvenv.cfg home = a missing dir -> BROKEN, engine native
def test_missing():              # no folder -> MISSING
def test_verify_record_mismatch():  # RECORD lists a.py with its digest; a.py changed -> verify False; restored -> True
def test_partial_install_ignored():  # DIR.tmp-99 with no ready.json, and a lock 20 minutes old -> state(DIR) MISSING; setup with stub steps
                                     # -> READY, the old tmp and the lock gone
def test_pointer_without_plugin_data():  # setup with PLUGIN_DATA=D, then PLUGIN_DATA unset -> venv_dir() == D/pdf-venv through the pointer
def test_setup_success():        # stub steps -> "READY\t<dir>\tpdfium-5.15.2-7000"; ready.json written; DIR.tmp-* gone; pdf-declined removed
def test_setup_replaces_old():   # an existing DIR with a marker file -> the new DIR has no marker; no "DIR.old*" left
def test_setup_pip_fail():       # pip stub (False, "ERROR: …") -> "FAIL\tpip\t…"; no DIR, no tmp, no pointer
def test_setup_no_wheel():       # pip stub output "No matching distribution found for pypdfium2" -> "FAIL\tpip\tno wheel for this platform"
def test_setup_smoke_fail():     # smoke stub False -> "FAIL\tsmoke\t…"; nothing left
def test_setup_lock_busy():      # a fresh DIR.lock -> "FAIL\tlock\tanother setup is running"; the lock kept
def test_check_decline_remove(): # check on MISSING -> "MISSING\t<dir>"; decline -> declined() True; remove -> DIR and the pointer gone
def test_requirements_pinned():  # the file has "pypdfium2==5.15.2" and exactly 7 lines matching --hash=sha256:[0-9a-f]{64}
def test_smoke_pdf_reads():      # pdf.parse_bytes(_smoke_pdf(), Context()) -> "phro smoke" in the render
def test_convert_reexec():       # a READY fake venv; subprocess.run patched in-process -> cmd_convert calls it with argv[0] == interpreter(venv)
                                 # and "--engine", "pdfium"; with PYENV=off it is not called
def test_pdfenv_import_light():  # a child imports reader.pdfenv -> no reader.formats module and no subprocess in sys.modules
```

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_pdfenv.py`. Expected: FAIL with `ModuleNotFoundError: reader.pdfenv`.
- [ ] **Step 3: Read the wheel digests** from PyPI's JSON (a web read) and write `requirements-pdf.txt`.
- [ ] **Step 4: Implement** `pdfenv.py`, `pdfsetup.py` and the `read.py` commands. No test may run the real `Steps`.
- [ ] **Step 5: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 6: Commit** `scripts/reader/pdfenv.py scripts/reader/pdfsetup.py scripts/reader/requirements-pdf.txt scripts/read.py tests/pdf_fix.py tests/test_reader_pdfenv.py` with the message `reader: read.py setup creates the pinned, hash-checked pypdfium2 venv only when run, with a ready marker, a pointer file and check, decline and remove modes`.

### Task 15: The pdfium front end

**Files:**
- Create: `scripts/reader/formats/pdf_pdfium.py`, `tests/pdfium_run.py`, `tests/test_reader_pdfium.py`
- Modify: `scripts/reader/formats/pdf.py`

**Interfaces:**
- **Consumes:** Task 7's types (`PageData.short` included), Task 8's `field_entry` and `order_comments`, Task 11's `NativeDoc` (`.resources(index)` included), Task 12's caps, and `PDF_PAGE_OBJECTS` and `PDF_PAGE_IMAGES` in `formats/pdf.py`, read as `pdf.<NAME>` at call time.
- **Produces, `pdf_pdfium.py`:**
  - `PdfiumDoc(data: bytes, native: NativeDoc | None)` raises:
    - `ReadError("encrypted", "user password")` on pdfium's password error;
    - `PdfiumOpenError` on any other open failure.
  - It has `.page_count` and `.outline()`, which comes from `get_toc()`: depth is `level + 1`, and the page is `dest.get_index()`.
  - `.page(index) -> PageData`. What the spike learned is kept:
    - `init_forms()` is called before any page is loaded.
    - **Chars:** from `PdfTextPage`.
      - `FPDFText_GetUnicode` joins surrogate pairs. The soft hyphen (0x02, 0xFFFE) at a line end becomes `-` for the hyphen rule. Generated `\r\n` breaks the run.
      - The size is the loose char box height.
      - Consecutive chars of one text object (`FPDFText_GetTextObject`) on one line form one `Fragment`, with `char_offsets` from the char boxes.
      - The render mode from `FPDFTextObj_GetTextRenderMode` sets `invisible`.
    - **Paths:** `get_objects(filter=[FPDF_PAGEOBJ_PATH], max_depth=16)`. Each segment point goes through the object's matrix into `PathCollector`, with dochan's thin-fill and straight-curve rules; its `segments` and `short_segments` fill `PageData.segments` and `.short`. The walk stops at `pdf.PDF_PAGE_OBJECTS`, counted `page_part`.
    - **Images:** `FPDF_PAGEOBJ_IMAGE` objects, with their bounds and `get_px_size()`. At most `pdf.PDF_PAGE_IMAGES` a page; the rest count `image`.
      - DCT keeps its raw data.
      - Over `images._MAX_PIXELS` pixels, the box is rendered at `CROP_DPI` instead.
      - Anything else becomes a PNG from `get_bitmap(render=False)`, through `images._png`.
    - **Render:** `render(box, dpi)` calls `page.render(scale=dpi / 72, crop=…, rev_byteorder=True)` with the long side capped at `RENDER_LONG`. A page over 200 inches renders at the cap. RGB rows go to `images._png`.
    - **Links:** `FPDFLink_Enumerate`, `FPDFLink_GetAnnotRect`, `FPDFLink_GetAction`, `FPDFAction_GetType` and `FPDFAction_GetURIPath`, with `annotations.SCHEMES` only.
    - **Comments:** `FPDFAnnot_GetSubtype`, `GetStringValue("T" / "Contents")`, `FPDFAnnot_GetFlags` and the `IRT` link, under `PDF_ANNOTS`. The reply order comes from `annotations.order_comments`.
    - **Fields:** `FPDFAnnot_GetFormFieldName`, `…AlternateName`, `…Value` and `…Type`, with check boxes through `FPDFAnnot_IsChecked` and the export value. UTF-16LE buffers have the NUL stripped. Each field goes through `annotations.field_entry`, and a returned `cut` counts `field`.
  - **OCG pages:** when `native` is given and `native.oc.any_off(native.resources(index))`, that page's fragments come from `native.page(index).fragments` (decision 3).
  - The module docstring says: pypdfium2's pdfium has no V8 or XFA, so no JavaScript runs.
- **Produces, `pdf.py`:**
  - `parse_bytes(engine="pdfium")` opens the native structure first. Its `encrypted` and `drm` errors propagate, and its `damaged` error is kept for later.
  - Then it tries `PdfiumDoc(data, native_or_None)`. On `PdfiumOpenError`, it falls back to the native document, or re-raises the kept `damaged` error when native also failed.
  - `Doc.meta["engine"]` is `"pdfium"` or `"native"`.
- **Produces, `tests/pdfium_run.py`** (run under the venv interpreter):
  - It reads pdf bytes on stdin and a JSON option object from `argv[1]`: `{"patch": {name: value}, "pages": [a, b] | null, "hidden": bool}`.
  - It patches `reader.formats.pdf` constants and runs `parse_bytes(data, ctx, engine="pdfium")` with `markdown.render` into a temporary dir.
  - It prints one JSON line: `{"md", "skipped", "meta", "assets": [[name, size, sniffed width, height]], "modules": sorted(sys.modules)}`.
  - On a `ReadError` it prints `{"error": [kind, detail]}` instead.
- **Produces, `test_reader_pdfium.py`:** `run(data, **opts)` calls `subprocess.run([interpreter, "-I", "-X", "utf8", "tests/pdfium_run.py", json], input=data)`. `main()` prints `skip: no pdf venv` and `pass` when there is no venv.

- [ ] **Step 1: Write the failing tests:**

```python
def test_pdfium_text():          # simple_pdf([text_ops("Hello world")]) -> "Hello world" under "## Page 1", meta engine "pdfium"
def test_pdfium_surrogates():    # a ToUnicode mapping one code to U+1F600 -> "😀" once, no U+FFFD
def test_pdfium_spacing():       # one-glyph TJ text with 0.5 gaps and one 4.0 gap -> "WORD ONE"
def test_pdfium_table():         # a ruled 2x2 grid with text -> a pipe table
def test_pdfium_bitmap_png():    # a 64x64 DeviceCMYK Flate image -> one png asset 64x64 (decision 15)
def test_pdfium_scan_rendered(): # a Letter page with no text and a full-page gray image -> "![Scanned page 1](" and an asset 935 px wide
def test_pdfium_figure_crop():   # 40 filled paths over 10% of the page -> one png linked, meta figures [1]
def test_pdfium_renders_cap():   # patch PDF_RENDERS 2, 4 scanned pages -> 2 images, unrendered [3, 4], skipped scan 2
def test_pdfium_pixel_cap():     # a 5000x5000 gray image -> the asset is a CROP_DPI render of its box (long side <= 1568), not 5000 px
def test_pdfium_image_cap():     # patch PDF_PAGE_IMAGES 2, three distinct 64x64 images on one page -> 2 assets, skipped image 1
def test_pdfium_fields():        # text with /TU, a checked box, an empty field -> "> Field Name: Kim", "> Field Agree: Yes", nothing for the empty one
def test_pdfium_comments():      # a Text annot by Ann and its reply -> two "> Comment by" lines, the parent first
def test_pdfium_link_schemes():  # https and javascript: links -> one "[…](https://…)", the other text only
def test_pdfium_hidden_rules():  # Tr 3 with no image, off-crop and 0.5 pt text -> 3 hidden places
def test_pdfium_ocg_via_native():  # an OCG-off text object -> left out and counted hidden; visible text on the same page read
def test_pdfium_encrypted():     # R6 with the empty password -> text read; user password -> the child prints {"error": ["encrypted", …]}
def test_pdfium_no_xref_falls_back():  # make_pdf(xref="none") -> text read, meta engine "native"
def test_pdfium_outline_headings():    # outline (1, "Intro", 0) -> "### Intro"
def test_pdfium_rotated_page():  # /Rotate 90 with three rotated Latin lines -> lines in order, words whole
def test_pdfium_imports_only():  # "PIL" and "pdfplumber" not in modules
```

- [ ] **Step 2: Run the tests to see them fail**, with `PHRO_READER_PYENV` set to the spike venv. Run `python -I -X utf8 tests/test_reader_pdfium.py`. Expected: FAIL with `ValueError` for the engine. Without the variable: `skip: no pdf venv`, then `pass`.
- [ ] **Step 3: Implement.** Check every `raw.FPDF*` name against the venv's `pypdfium2/raw.py` before using it.
- [ ] **Step 4: Run the tests to see them pass** under the spike venv, then the full-suite check without it.
- [ ] **Step 5: Commit** `scripts/reader/formats/pdf_pdfium.py scripts/reader/formats/pdf.py tests/pdfium_run.py tests/test_reader_pdfium.py` with the message `reader: the pdfium front end gives pages as PageData (chars by geometry, path rules, bitmaps, renders, fields, links, comments), falls back to native on open failure, and takes off-layer pages' text from native`.

### Task 16: The gate reads pdf

**Files:**
- Modify: `scripts/reader/gate.py`, `scripts/read.py` (only `hook --data`, if Task 1 takes that branch), `hooks/hooks.json` (the same branch), `tests/test_reader_gate.py`

**Interfaces:**
- **Consumes:** Task 1's F1, F1b, F2, F4a and F4b; Task 13's `cache.key(path, engine)` and `convert --engine/--pages`; Task 14's `pdfenv` and `pdf_fix.fake_venv(dir, *, build=7000)`.
- **Produces, `gate.py`:**
  - **Text check:** `looks_like_text` returns False when `b"%PDF-"` is in the first 1,024 bytes.
  - **Skill:** `SKILL_OF["pdf"] = "pdf"`. `EDIT_HINT` stays OOXML-only.
  - **Hint:** `PDF_HINT = ' Pages start at "## Page N" lines. Read-only view; to edit or create a pdf, use the pdf skill.'`
  - **Offer:** `OFFER = ' Built-in parser used; pypdfium2 (PyPI, 4 MB) reads tables, scans and forms better. Ask the user; only on yes run: python -I -X utf8 "{read_py}" setup --venv "{dir}" (--decline instead if they never want it).'` (194 characters without the two paths; `CONTEXT_MAX` stays 600) and `REPAIR` (decision 11).
  - `parse_pages(v) -> tuple[int, int] | None` (decision 13).
  - `_pdf(event, path, start, timeout, command) -> dict | None`, which `decide` calls for pdf:
    - **Engine:**
      - `pdfenv.engine()` gives the id.
      - On a cache miss with pdfium, `pdfenv.verify(venv)` runs. A False makes the engine native and the offer the repair one.
      - The child is `[pdfenv.interpreter(venv), …, "--engine", "pdfium"]` for pdfium and `[sys.executable, …, "--engine", "native"]` otherwise.
      - For pdf, `command` is a prefix: the default `[<interpreter>, "-I", "-X", "utf8", READ_PY, "convert"]`, or `command=` in tests. The gate appends `[path, "--engine", e]`, plus `["--pages", "A-B"]` for a range. Other formats keep `command=` as the full argv.
    - **Failure marker:** `cache.root() / "failed" / k` younger than 86,400 s passes through at once.
    - **Pass-through:** `damaged`, `timeout` (including the hook's own budget) and an internal error write the marker and pass through.
      - With F2 TRUE they return `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": f"phro-project could not convert {name} ({why}); Claude's own Read is used."}}`, where `why` is the deny text's reason.
      - With F2 FALSE they return None.
    - **Deny:** `encrypted`, `drm` and `too_large` deny through `_deny`, which names the pdf skill.
    - **`pages`** (from the input, through `parse_pages`):
      - Within the pages read: `offset` is the line of `## Page A`. `limit` is the line of `## Page B+1` minus `offset`, or runs to the end.
        - With F4a, the `pages` key is removed from `updatedInput`. Without F4a and with `IGNORES`, it is kept.
        - Without F4a and with `REJECTS`, any pdf Read with `pages` passes through.
      - Every page of the range in `unrendered`: pass through (return None).
      - `B` past the last page read and `A <= pages_total`: convert with `--pages A-B` under key `k + f"-p{A}-{B}"`, served whole.
      - `A > pages_total`: `pages` is dropped, and the context adds the past-the-end sentence.
    - **Offer:** in native mode, when not forced native and not `pdfenv.declined()`.
      - It comes once per session: the marker is `cache.root() / "offers" / sha1(session_id)`, and an event with no `session_id` gets it every time.
      - The marker is written only when the offer went into the context (decision 12).
  - **`_context` for pdf** fits, in order:
    1. the head with the hidden count;
    2. `PDF_HINT`;
    3. the past-the-end sentence (decision 11), when `A > pages_total`;
    4. the offer, whole or not at all;
    5. ` Pages not shown as images: <ranges>; Read the pdf with pages="<first range, at most 20 pages>" to see them.`;
    6. ` Vector figures on pages <list> are linked as images.`;
    7. the unread list;
    8. for long outputs, ` Pages at lines: <n>:<line>, …` with every k-th page for the smallest k that fits, replacing the heading index;
    9. the garble warning.

    The garble tail is reserved first, as now. Lists 5-8 are cut with `…` by `_fit`.
- **Produces, F1 branch:** when Task 1 found F1 FALSE and F1b TRUE:
  - `hooks.json`'s Read command gains ` --data "${CLAUDE_PLUGIN_DATA}"`.
  - `read.py hook --data D` sets `os.environ["CLAUDE_PLUGIN_DATA"] = D` when `D` is non-empty and not literally `${CLAUDE_PLUGIN_DATA}`.
  - Otherwise these files are not touched.

- [ ] **Step 1: Write the failing tests:**
  - `test_pass_through` drops `"a.pdf"`.
  - The `%PDF-1.7` lines at `:591-592` now expect the F2 pass-through result for a damaged pdf (Task 13 made them expect a deny).
  - `test_offer_fits_with_hint` uses install-length paths (`read.py` about 85 characters, the venv dir about 68): the head, `PDF_HINT` and the offer must fit in 600 together.
  - Add, using `stub()`'s cache env and `pdf_fix` files written to a temp dir:

```python
def test_pdf_converts_native():  # simple_pdf([text_ops("Hi")]) -> "allow"; context starts with the head, has PDF_HINT and the offer
def test_engine_in_key():        # pdf_fix.fake_venv(DIR) READY with PYENV=DIR; an entry pre-stored under cache.key(p, "pdfium-5.15.2-7000");
                                 # command= a script that exits 3 -> "allow" served from that entry, the child never run
def test_verify_fail_repairs():  # the fake venv's RECORD file altered -> converted native, context has "failed its file check"
def test_pages_values_parsed():  # parse_pages(" 3 - 5 ") == (3, 5); "7" -> (7, 7); "5-3", "0", "3,5", "x", 7 -> None
def test_pages_offset_limit():   # 4 pages read, pages="2-3" -> offset == line of "## Page 2", limit == line of "## Page 4" - offset; "pages" per F4a
def test_pages_past_cut_range(): # PDF_PAGES patched to 2 in the child (a stub CLI given as the command= prefix), pages="3-4" -> the child gets
                                 # "--pages", "3-4" appended; served out.md under "-p3-4" holds "## Page 3"
def test_pages_unrendered_pass_through():  # meta unrendered [2, 3], pages="2-3" -> None; pages="1-3" -> allow
def test_pages_past_end():       # 4 pages, pages="9" -> allow, no "pages" in updatedInput, context has 'The pdf has 4 pages; pages="9-9" starts past its end.'
def test_pdf_damaged_pass_through():  # b"%PDF-1.7\n" + bytes(64) -> F2 output (or None); failed/<key> exists; a second decide never runs
                                      # the child (command= a script that exits 3) and gives the same result
def test_pdf_timeout_pass_through():  # command= a child that sleeps, timeout=1 -> F2 output naming the timeout; the marker written
def test_pdf_encrypted_denies(): # a user-password pdf -> "deny", the reason ends "Read it with the pdf skill instead."
def test_offer_once_per_session():  # session_id "s1": the first decide has the offer, the second not; "s2" has it; no session_id -> every time
def test_offer_never_after_decline():  # pdf-declined exists -> no offer
def test_offer_not_when_off():   # PHRO_READER_PYENV=off -> no offer
def test_offer_fits_with_hint(): # install-length paths: READ_PY patched to an 85-character path, the venv dir 68 characters; name "n" * 24 +
                                 # ".pdf", 120 lines, 3 images, hidden 3, unread 5 kinds -> head 149 + PDF_HINT 94 + offer 347 = 590 fit in
                                 # 600 (at most 599); len < 600, the hint and the offer whole, the unread list left to _fit
def test_offer_dropped_when_long():  # name "n" * 250 + ".pdf" -> no offer, no offers/ marker written
def test_many_unrendered_fit():  # meta unrendered = every odd page of 1..300, figures 1..50 -> len < 600, the unrendered sentence present with
                                 # "…" and pages="1-1"
def test_pdf_context_order():    # all items present -> index order: hint < "starts past its end" < offer < "Pages not shown" < "Vector figures" <
                                 # "Some parts" < "Pages at lines" < "garbled"
def test_page_index_long():      # 3,000 lines over 300 pages -> "Pages at lines: 1:" in context, no "Sections at lines"
def test_pdf_reader_off():       # PHRO_READER_OFF=pdf -> None
def test_ascii_pdf_not_text():   # simple_pdf written with no binary comment line (pure ASCII) -> converted, not None
def test_hook_data_arg():        # read.py hook --data D sets the env for pdfenv
```

  `test_hook_data_arg` is defined only when Task 1 took the F1 branch; otherwise it is not written.

- [ ] **Step 2: Run the tests to see them fail.** Run `python -I -X utf8 tests/test_reader_gate.py`. Expected: FAIL in `test_pdf_converts_native`.
- [ ] **Step 3: Implement.** `test_import_is_light` must still hold: `pdfenv` and `cache` are imported inside `_pdf`.
- [ ] **Step 4: Run the tests to see them pass**, then the full-suite check.
- [ ] **Step 5: Commit** `scripts/reader/gate.py tests/test_reader_gate.py`, plus `scripts/read.py hooks/hooks.json` on the F1 branch, with the message `reader: the gate converts pdf with the engine in the key, maps Read's pages onto the page index, passes failures through to Claude's own Read, and offers the pypdfium2 setup once a session`.

### Task 17: README, plugin version

**Files:**
- Modify: `README.md`, `.claude-plugin/plugin.json`

**Interfaces:**
- **Produces, `README.md` (`## Document reader`):**
  - "`pdf` alone is never touched … until its phase" becomes a pdf paragraph. It covers:
    - native by default, and the optional pypdfium2 venv: pinned and hash-checked, about 4.2 MB downloaded and 8.4 MB on disk, under `${CLAUDE_PLUGIN_DATA}/pdf-venv`, or Task 1's resolved folder;
    - `read.py setup` and its modes, and that it installs only on the user's yes;
    - scanned pages as images with no OCR, fields and comments, hidden layers and hidden text, and `pages`;
    - editing and creating with `anthropic-skills:pdf`;
    - pypdfium2's licence (Apache-2.0 or BSD-3-Clause; it is installed, not copied).
  - `` `pdf` `` is already in the built list (Task 13); this task keeps it there.
  - "Standard library only" gains "except the optional pdf engine in its own venv".
  - "40 in-scope extensions" becomes 41.
  - "Not done yet" reads "Phases 1, 2a, 2b, 2c and 2d (…, pdf) are built".
- **Produces, `plugin.json`:** `"version": "0.16.0"`.

- [ ] **Step 1: Run** the whole file, `python -I -X utf8 tests/test_reader_gate.py` (its `main()` ignores arguments, so no single test can be named). Expected: `pass`, `test_readme_formats_match` included, since Task 13 already put `` `pdf` `` in the built list; the README edit below has no failing test of its own.
- [ ] **Step 2: Edit** the README and `plugin.json`.
- [ ] **Step 3: Run the full-suite check.** It includes `test_readme_formats_match` and `test_notices_credit_port_sources`.
- [ ] **Step 4: Commit** `README.md .claude-plugin/plugin.json` with the message `reader: the README describes pdf reading, the optional pypdfium2 venv and read.py setup; plugin 0.16.0`.

### Task 18: pdf in the mutation tests

**Files:**
- Modify: `tests/test_reader_mutation.py`

**Interfaces:**
- **Consumes:** `pdf.parse_bytes` (with `started` left None, so each case's deadline runs from its own entry; Task 12), `pdf_fix` (`make_pdf`'s `objstm`, Task 4).
- **Produces:**
  - `pdf_seeds() -> list[bytes]`, each built in memory:
    - a two-page pdf with Helvetica text, a CID font with ToUnicode, a ruled table, a gray Flate image, a link, a comment, a filled field and an outline;
    - the same with an xref stream and object streams (`make_pdf(…, xref="stream", objstm=True)`);
    - the same under R4 AES.
  - Each seed is damaged with `plan(rng, size)` and read through `pdf.parse_bytes(data, Context(), engine="native")` and `markdown.render` with no image written. Every outcome is a `Doc` or a `ReadError`, under `SLOW`.
  - `outcome`'s accepted kinds gain `"drm"`: a mutated R4 seed (`/Standard` to `/Stbndard`) is a non-Standard handler.
  - The module docstring names pdf among the formats.

- [ ] **Step 1: Write the test** `test_pdf_mutations`, `CASES` cases per seed, as the other formats do.
- [ ] **Step 2: Run** `python -I -X utf8 tests/test_reader_mutation.py`. A case that raises anything else, or passes `SLOW`, is a reader defect. Fix it in the owning module with a test there, then run again.
- [ ] **Step 3: Run the full-suite check.**
- [ ] **Step 4: Commit** `tests/test_reader_mutation.py`, plus any fixed module and its test, with the message `reader: mutation tests cover pdf (plain, object streams, AES), read in memory`.

### Task 19: Measure the caps

**Files:**
- Modify: `tests/test_reader_pdf.py` (`measure`), `tests/pdfium_run.py` (the `"measure": "tables"` option), `scripts/reader/formats/pdf.py`, `scripts/reader/formats/pdf_tables.py`, `scripts/reader/pdfnative/crypto.py` (cap values and comments only)

**Interfaces:**
- **Produces, `tests/test_reader_pdf.py measure <what> [path]`:**
  - `renders`: 100 Letter pages, each a full-page 1700×2200 gray Flate scan, built in memory and run through `pdfium_run.py` with the venv interpreter. It prints `RENDERS 100 <s>`.
  - `tables <path>`: the 384-page file's pages through `NativeDoc.page` and `pdf_tables.tables`, timing the table assembly alone. It prints `TABLES <pages> <s>`, then the same under pdfium through `pdfium_run.py` with `{"measure": "tables"}`.
  - `native <path>`: the whole native conversion of the same file. It prints `NATIVE <pages read> <s>`.
  - `decrypt`: `DECRYPT_TOTAL` bytes of AES-256-CBC through `StandardSecurityHandler.decrypt`. It prints `DECRYPT <bytes> <s>`.
- **Produces, `tests/pdfium_run.py`:** the option `"measure": "tables"` runs each page's `PageData` through `pdf_tables.tables` alone and prints `{"tables": [<pages>, <s>]}` instead of the usual line.
- **The 384-page file:** spike id `e4bc4dca33`, where the id is sha1 of the absolute forward-slash path, first 10 hex. Find it with:

  ```bash
  python -I -X utf8 -c "import hashlib,pathlib; [print(p) for p in pathlib.Path('C:/Users/hunje/research/ai-document-reader-plugins/bench/engines').rglob('*.pdf') if hashlib.sha1(p.as_posix().encode()).hexdigest()[:10]=='e4bc4dca33']"
  ```

  It is named in no committed file. Only its timings are written down.

- [ ] **Step 1: Write `measure`** as above. `main()` runs it on `sys.argv[1] == "measure"`.
- [ ] **Step 2: Renders.** With `PHRO_READER_PYENV` set to the spike venv, run `python -I -X utf8 tests/test_reader_pdf.py measure renders`. Expected: `RENDERS 100 <s>` with `<s>` at most 10.
  - If it is over, lower `PDF_RENDERS` to the largest multiple of 10 that measures at most 10 s.
  - If the spike venv is gone, this step and the pdfium half of Step 3 move to Task 22 after its venv exists.
- [ ] **Step 3: Tables.** Run `… measure tables "<path>"`. Expected: `TABLES 384 <s>` at most 15 s for each engine. If one is over, lower `MAX_INTERSECTION_CHECKS` and `MAX_PAGE_CELLS` in `pdf_tables.py` by halves until it fits.
- [ ] **Step 4: Native deadline.** Run `… measure native "<path>"`. Expected: `NATIVE 384 <s>` with `<s>` under `PDF_DEADLINE` (the spike: 9.8-10.3 s for dochan).
- [ ] **Step 5: Decrypt.** Run `… measure decrypt`. Expected: at most 10 s. If it is over, set `DECRYPT_TOTAL` to the largest whole MiB that measures at most 10 s.
- [ ] **Step 6: The existing worst case.** Run `python -I -X utf8 tests/test_reader_xlsx.py measure`. Expected: `WORST <s>` with `<s>` at most 20.
- [ ] **Step 7: Record** each measured figure in the comment of its cap, both figures where a cap was lowered. Run the full-suite check.
- [ ] **Step 8: Commit** the changed files with the message `reader: pdf caps measured (100 renders, tables on a 384-page file, the native page loop, AES decryption)`.

### Task 20: Bench — scope, the baseline and the adapters

In this task and the next two, `<bench>` is `C:/Users/hunje/research/ai-document-reader-plugins/bench`. Write the path out in full in every command.

**Files (bench repository):**
- Modify: `harness/common.py`, `harness/adapters/baseline.py`, `harness/adapters/phro_read.py`, `harness/adapters/dochan.py`, `xberg.py`, `anydoc.py`, `markitdown.py`, `harness/report.py`, `tests/test_common.py`, `tests/test_baseline.py`, `tests/test_report.py`, `tests/test_adapters.py`

**Interfaces:**
- **Produces, `common.py`:** `BUILTIN = {}`, so `SCOPE` takes pdf and `len(SCOPE) == 33`. The comment says the bench spec's scope change ended with phase 2d.
- **Produces, `baseline.py`:**
  - `builtin>pdf` registers `{"pdf", "xps", "epub", "mobi", "azw3"}`.
  - `builtin_pdf` on a `.pdf` source skips Calibre: `[[pdftotext, "-enc", "UTF-8", src, out.md], [pdftoppm, "-r", "100", "-png", src, <run>/assets/page]]`.
  - The docstring says so.
- **Produces, `phro_read.py`:**
  - `phro-read` gains `"pdf"` and `env_extra={"PHRO_READER_PYENV": "off"}` (native).
  - A second `register(Adapter(name="phro-read-pdfium", kind="engine", formats=frozenset({"pdf"}), env="phro-read", env_extra={"PHRO_READER_PYENV": str(common.BENCH / "engines" / "phro-read-pdf" / "venv")}, repo=<the phro-read adapter's repo>, install=[], install_alt=[], convert=convert, version_cmd=…))`. `repo`, `install` and `install_alt` have no defaults (`adapters/base.py:15-31`), so `repo` is copied from the existing phro-read adapter. Its version command prints `VERSION` and `pdfenv.engine()`.
- **Produces, peers:**
  - dochan, xberg, anydoc and markitdown gain `"pdf"` where their README names PDF, each with a `README.md:<line>` citation beside `formats=`.
  - docling is left out: the spec makes it optional, and it is heavy.
- **Produces, `report.py`:** the "내장 스킬로 보내는 형식" line and the built-in rows are written only when `common.BUILTIN` is not empty.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_common.py, test_builtin_scope rewritten
#   BUILTIN == {}; len(SCOPE) == 33; in_scope(".PDF")
# tests/test_baseline.py
def test_builtin_pdf_skips_calibre():  # builtin_pdf(Path("x/a.pdf"), run_dir) -> two steps, step[0] starts with pdftotext and holds "…a.pdf"
def test_builtin_xps_keeps_conversion():  # builtin_pdf(Path("x/a.xps"), run_dir) -> three steps
# tests/test_adapters.py
def test_phro_read_pdf_engines():  # phro-read formats has pdf and env_extra PYENV "off"; phro-read-pdfium formats == {"pdf"}, env "phro-read",
                                   # env_extra PYENV ends "engines/phro-read-pdf/venv"
# tests/test_report.py
def test_no_builtin_line():        # with BUILTIN {} the pages family section has no "내장 스킬로 보내는 형식"
```

- [ ] **Step 2: Run** `python -I -X utf8 <bench>/tests/test_common.py`, then the other three. Expected: FAIL.
- [ ] **Step 3: Implement**, reading each peer README for its citation line.
- [ ] **Step 4: Run** the four files again. Expected: `pass` from each.
- [ ] **Step 5: Commit** in the bench repository (`git -C <bench> add …`, then `git -C <bench> commit …`) the files above, with the message `bench: pdf comes into scope; builtin>pdf reads a pdf without Calibre; phro-read reads pdf natively and phro-read-pdfium under its own venv; peers gain pdf`.

### Task 21: Bench — the pdf corpus by feature, generated pdfs, routing and the bar

**Files (bench repository):**
- Create: `corpus/pdf_picks.tsv`, `harness/gen/pdfs.py`
- Modify: `harness/corpus.py`, `harness/gen/build.py`, `harness/bar.py`, `reports/written/routing.tsv`, `tests/test_corpus.py`, `tests/test_gen.py`, `tests/test_bar.py`

**Interfaces:**
- **Produces, `corpus/pdf_picks.tsv`:**
  - Columns `feature` and `path`, the path relative to `<bench>`, one row per spike file.
  - Every spike id of `notes/pdf-spike/spike-report.md` section 2 except `5eeaad290b`, which is covered by `106d1d006e` and `05b3be9b99`.
  - Each id is found by sha1 of the absolute forward-slash path, as in Task 19.
  - `05b3be9b99` (no xref) and the timing-only `b85770793d` are kept.
- **Produces, `corpus.py`:**
  - `pdf_pick_rows() -> list[dict]` gives one public row per pick, with `source` `public`, no truth and no expect.
  - `build_index` adds them.
  - The generated pdfs under `corpus/generated/pdf/` are indexed with `expect` set to their `<name>.expect.json`.
  - `corpus/generated/word/seed.pdf` is indexed with the word family's `truth.json`.
- **Produces, `gen/pdfs.py`:**
  - `pdf_fix()` loads `Path(PHRO_READ_SCRIPTS).parent / "tests" / "pdf_fix.py"` with `importlib.util.spec_from_file_location`. `pdf_fix.py` puts its own `scripts/` on `sys.path` when loaded (phro-project Task 4), so its `reader` imports work here.
  - `build() -> list[Path]` writes four clean pdfs, each with an expect list of the strings that must appear:
    - `comments.pdf`: a Text comment, a FreeText comment and a reply;
    - `off-layer.pdf`: visible text plus OCG-off text, whose expect holds only the visible text;
    - `user-password.pdf`: R4 with user password `bench`, with no expect, since it is a failure case;
    - `invisible.pdf`: visible text plus Tr 3 text with no image, whose expect holds only the visible text.
- **Produces, `gen/build.py`:** `cmd_gen --only pdf` runs `pdfs.build()` alone.
- **Produces, `bar.py`:** for `ext == "pdf"`, rows compare whole calibrated tokens (linked images counted as viewed, not `TEXT_BAR`) and gain `text` and `img` columns. `cmd_bar` prints them for pdf.
- **Produces, `routing.tsv`:** the row `pdf	builtin>pdf	dochan`.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_corpus.py
def test_pdf_pick_rows():          # a temp picks file of two rows -> two public rows, ext pdf, the paths as given
# tests/test_gen.py
def test_pdf_seeds():              # pdfs.build() into a temp dir -> four files starting b"%PDF-"; the off-layer expect lacks its hidden string
# tests/test_bar.py
def test_pdf_bar_whole_with_parts():  # pick builtin>pdf 2,000 whole, engine 1,900 whole (1,200 text, 700 img) -> PASS, text 1200, img 700
```

- [ ] **Step 2: Run** the three files. Expected: FAIL.
- [ ] **Step 3: Implement**, and write `pdf_picks.tsv` from the sha1 lookup.
- [ ] **Step 4: Run** the three files again. Expected: `pass`.
- [ ] **Step 5: Commit** in the bench the files above, with the message `bench: the pdf corpus by feature from the spike's picks, four generated pdfs, the routing row and the pdf bar with text and image tokens beside`.

### Task 22: Bench run and the phase 2d addendum

**Files:**
- Modify, in phro-project: `README.md` (the pdf result line).
- Modify, in the bench: `corpus/index.tsv`, `corpus/generated/pdf/*`, `reports/written/routing.tsv` (`alt`), `engines.tsv`, `scores.jsonl`, `tokens.jsonl`, `status.txt`, `reports/*.md`.
- Create, in the bench: `reports/written/phro-read-phase2d.md`.

- [ ] **Step 1: ASK THE USER FIRST — the bench venv.**
  - Say in chat: "To bench the pdfium engine I need to create a venv at `C:/Users/hunje/research/ai-document-reader-plugins/bench/engines/phro-read-pdf/venv`. It downloads pypdfium2 5.15.2 for win_amd64 from PyPI (about 4.2 MB), pinned and hash-checked. It installs about 8.4 MB, plus the venv's own bundled pip. The command is:"

    ```bash
    python -I -X utf8 "C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a/scripts/read.py" setup --venv "C:/Users/hunje/research/ai-document-reader-plugins/bench/engines/phro-read-pdf/venv"
    ```

    Then ask "May I?"
  - Wait for a clear yes, then run it. Expected: `READY\t…\tpdfium-5.15.2-<build>`.
  - Without a yes, `phro-read-pdfium` is not run, the addendum says so, and only native is benched.
  - If Task 19's pdfium measures were deferred, run them now with `PHRO_READER_PYENV` set to this venv, and commit their figures in phro-project as Task 19 says.
- [ ] **Step 2: Seeds and corpus.** Run:
  - `PHRO_READ_SCRIPTS=… python -I -X utf8 <bench>/harness/bench.py gen --only pdf`. Expected: four `GEN corpus/generated/pdf/…` lines.
  - `python -I -X utf8 <bench>/harness/bench.py corpus --only pdf --no-scan --no-fetch`. Expected: a `COVERAGE` line for pdf with generated 5 and public at least 45 (the pdf rows already in `corpus/index.tsv`, Tika's 42 among them) plus the rows of `corpus/pdf_picks.tsv`.
  - `git -C <bench> diff --unified=0 corpus/index.tsv`. No removed row may show an old doc_id.
- [ ] **Step 3: Run.** Use the doc_ids whose ext is pdf in `corpus/index.tsv`.
  - `PHRO_READ_SCRIPTS=C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a/scripts python -I -X utf8 <bench>/harness/bench.py run --doc <id> [--doc <id> …]` runs every engine.
  - Then `… run --engine phro-read --engine phro-read-pdfium --doc <id> … --force`.
  - Then `bench.py score`, `bench.py tokens`, `bench.py report`, `bench.py bar --engine phro-read-pdfium` and `bench.py bar --engine phro-read`, each with the same `PHRO_READ_SCRIPTS`.
  - Expected: a pdf `BAR` line for each engine. The other extensions keep their phase 2c verdicts. `engines.tsv` shows phro-read `0.5.0`.
- [ ] **Step 4: Fix any pdfium `MISS`.**
  - A `MISS` that comes from a reader defect gets a test and a fix commit in the worktree, then Step 3 runs again.
  - A native `MISS` is recorded with its reason and does not block the phase (spec "Success").
- [ ] **Step 5: Write the addendum** `phro-read-phase2d.md` in Korean, like `phro-read-phase2c.md`. It names documents by doc_id only and holds:
  - the pdf verdicts for both engines: fidelity, calibrated tokens with linked images, text and image tokens side by side, and the route's figures;
  - the reason for each `MISS`;
  - the failure kinds by doc_id (encrypted, damaged, pass-through);
  - how the corpus was chosen (the spike's picks by feature, Tika, the generated four, no real pdf);
  - the routing row, and whether the bench venv was created.
- [ ] **Step 6: README result.** Add one result line for `pdf`, both engines, to the README's result list, in its style.
- [ ] **Step 7: Optional — ASK THE USER FIRST — the user's own pdf venv.** Only when the user wants pdfium in their sessions now:
  - Say in chat that `python -I -X utf8 "C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a/scripts/read.py" setup` downloads the same 4.2 MB and installs about 8.4 MB into `<the folder pdfenv.venv_dir() prints>`.
  - Run it only on a clear yes.
  - Without one, the native-mode offer asks in their sessions later.
- [ ] **Step 8: Run the full-suite check**, then commit:
  - In phro-project: `README.md`, with the message `reader: bench results for pdf (native and pdfium) in the README`.
  - In the bench: the files above, with the message `bench: phro-read phase 2d (pdf, native and pdfium)`.

## Spec coverage

| Spec section | Tasks |
|---|---|
| Goal, Success (route, the two engine bars, no software outside the venv) | 14, 20-22 |
| The spec's numbered decisions | 1 (hook facts), 4-11 (the native port), 12 (page loop, scans, figures), 13 and 16 (engine in the key, gate), 14 (venv and setup), 15 (pdfium), 17 (README), 20-22 (bench) |
| Defaults | 16 (failure pass-through, 24 h, offer once, decline, pages past a cut), 15 (pdfium to native), 12 (hidden kinds, no bold, headings, field labels, link schemes, tables per page) |
| Modules and data flow, `PageData` | 7, 11, 12, 15 |
| Native front end and its four faults | 4-8, 11; spacing 7 and 9, outline 8 and 12, vertical 9, widget values 8 |
| Layout rules, OCG state | 7, 9 |
| The venv (location, marker, engine id, checks, setup, approval flow, child) | 13, 14, 16 |
| Hook-platform facts | 1, and branches in 16 |
| Gate (format table, text check, engine and key, pages, failure, context, meta, page headings) | 2, 13, 16 |
| Output table | 2, 8, 9, 10, 12, 15 |
| Limits and the measure | 4-8, 10, 12, 19 |
| Hostile input | 4, 5, 7, 8, 12, 15, 18 |
| Errors and LOST counts | 2, 5, 12 |
| Cache | 13 |
| README and notices | 4, 6, 7, 9, 17 |
| Bench | 20-22 |
| Testing (fixture builder, the two engines, changed tests) | 4-16, 18 |
