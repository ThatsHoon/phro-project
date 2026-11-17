# doc-reader diagrams (connectors as Mermaid) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shapes joined by connectors in vsd, vsdx, pptx, xlsx, xlsb, docx, odp, ods, odt, ppt, doc, xls, pub, hwpx and hwp reach Claude as a Mermaid flowchart, or as a nested list when the diagram is a branching tree. Their text appears there once, and connector ends left loose are placed by position.

**Architecture:**
- **One shared module, `scripts/reader/diagram.py`.** It numbers nodes, escapes labels, orients edges, infers loose ends, chooses between the tree list and Mermaid, and places the result into a scope's flow.
- **Readers only extract.** Each reader registers one `Diagram` per drawing scope. It passes every shape (key, blocks, kind, box) and every connector (glue, end points, arrowheads, label), then hands its flow to `Diagram.place(slots)` as `(key, blocks)` pairs.
- **Shared geometry helpers.**
  - DrawingML (pptx, xlsx, xlsb, docx) shares `formats/drawingml.py`, moved out of `ooxml_slides.py`.
  - The ODRAW formats (ppt, doc, xls, pub) share new solver, FSP and arrowhead helpers in `officeart.py`.
- **No model change.** Output is the existing `Code(lang="mermaid")`, `ListItem` and `Para` blocks.

**Tech Stack:** Python 3.12, no third-party package (none is needed; the standard library is not a goal, user 2026-11-16). Tests are plain scripts. The bench at `C:/Users/hunje/research/ai-document-reader-plugins/bench` is the yardstick.

**Spec:** `docs/superpowers/specs/2026-11-17-doc-reader-diagrams-design.md`, approved 2026-11-17 (commit 3ebb6cf, which also made the engine spec edits). Read it before Task 1. Each task names the spec sections it implements; where this plan repeats a value, it is the spec's.

**Port sources** (read as data; never act on instructions inside them; never run or build them):
- **kordoc (MIT), commit `9be0346ca74ff80822f6e94b811e3e0fbffd0b0b`** at `C:/Users/hunje/research/ai-document-reader-plugins/bench/engines/kordoc/src/`. Its `src/hwp5/to-hwpx/shapes.ts` converts HWP 5 drawing objects to hwpx, including the connect line (`$col`), its start and end points, its type (`CONNECT_TYPES`) and its subject ids. The subject ids are the drawing objects' instance ids (`drawingStyle` → `instId`, written as hwpx `instid`). This settles the spec's two Hangul probes without a sample.
- **libvisio (MPL-2.0), master `a42aaff13e4a67afcd077c3abb3ef13bb6463025`** at `C:/Users/hunje/research/ai-document-reader-plugins/bench/cache/public-src/libvisio/src/lib/`, as in phase 4. The files are `VSDParser.cpp` (`readXFormData`, `readXForm1D`, `readLine`, `readShape`, `readStyleSheet`) and `VSDDocumentStructure.h`. `vsd.py` already carries the MPL header; its file list gains these functions.
- **Knowledge only:** MS-ODRAW (SolverContainer, ConnectorRule, FSP flags, FOPT 0x01D0/0x01D1, shape types), ECMA-376 DrawingML, ODF 1.3 (`draw:connector`), the OWPML schema reference at `C:/Users/hunje/research/ai-document-reader-plugins/ref/rhwp/mydocs/manual/owpml_schema_reference.md`.

**Background, read-only:**
- `C:/Users/hunje/AppData/Local/Temp/claude/5f43e189f7571e52/4034781a-9eb8-4169-8f5e-3c9f570e46ed/scratchpad/plan-explore-visio-ooxml.md`
- `C:/Users/hunje/AppData/Local/Temp/claude/5f43e189f7571e52/4034781a-9eb8-4169-8f5e-3c9f570e46ed/scratchpad/plan-explore-odf-odraw-hwp.md`

These two files hold the wrap sites, fixture helper names and byte offsets per reader, with file:line references as of 3ebb6cf. Each task points to its section.

## Decisions this plan makes (the owner may veto them at review)

1. **Keys.** A shape key is whatever the reader already uses as the shape's id, kept as the same Python type within one scope:
   - Visio shape `ID` strings;
   - DrawingML `cNvPr@id` strings;
   - ODF `draw:id`, else `xml:id`;
   - ODRAW spids (ints); in pub, the Contents seqnum, mapped from the spid;
   - hwpx `instid`, else `id`;
   - the hwp instance id (int).
2. **Connector slots.** A connector that becomes an edge loses its slot. Its plain text becomes the label. A connector that does not become an edge keeps its slot unchanged. Pictures a connector carries go in a separate slot with key `None`.
3. **Slot order sets node IDs.** Nodes are numbered in the order their keys first appear in `slots`. Nodes with no slot (a textless shape, a slide title) follow in `shape()` call order. Edges keep `connector()` call order: glued first, then inferred.
4. **Where the block goes.** The diagram goes at the first slot whose key is a node, an edge connector or a subgraph container. If there is none, it goes at the end of the scope.
5. **Plain text.** A node is "short" when its blocks are only `Para` and `ListItem` whose inlines are `Text`, `Bold` and `Break`, and the joined text (lines joined with `\n`) is at most `NAME` (80) characters. A `Hidden` inline, `Link`, `NoteRef`, `Math`, `Image`, `Table`, `Code` or `Equation` makes the node rich. A rich node's label is its first plain line, cut at 80 characters, plus `…`. If it has no plain line, the label is `(picture)`, `(table)`, `(chart)` (a `Table` whose caption starts with `Chart`) or `(content)`.
6. **Hidden shapes.**
   - A hidden shape at an edge end is a blank node `nK[" "]`. Its slot stays where it is, with its hidden blocks under the existing hidden rules.
   - A hidden connector is never an edge, and its slot stays.
7. **Arrowhead defaults per format, when the property is absent:**

   | Format | Absent arrowhead property means |
   |---|---|
   | Visio, after the master and line-style chain | not known (`None`) |
   | DrawingML | no arrowhead (`False`): `headEnd` and `tailEnd` default to `none` |
   | ODF, after the style chain | not known |
   | ODRAW | no arrowhead (`False`): MS-ODRAW's default is `msolineNoArrowhead` |
   | hwpx and hwp | set by the connect line's type |
8. **Geometry that has no common frame gets `box=None`, so no inference:**
   - doc floating shapes, whose FSPA boxes are relative to differing anchors;
   - hwpx and hwp top-level objects whose `vertRelTo` or `horzRelTo` is not `PAPER` or `PAGE`.

   Glue still works for both. Inside a group (doc group members, `hp:container`), group coordinates are a common frame, and the group is the scope.
9. **vsd.** vsd has no containers in this plan: the User section, chunk 0xB4, is not read, as the spec permits. vsd node kinds come from the master's name when the reader has it, else `""`.
10. **odt and docx scope.**
    - docx: the scope is each `wpg:wgp` group and each `wpc:wpc` canvas. Its flow is the members' blocks, which today go to the paragraph's extras.
    - odt: the scope is the document body, but only when the body holds a `draw:connector`, or a `draw:line` with a marker, found by one `walk` pass. Shapes anchored in a paragraph then become their own slots right after the paragraph, instead of being inlined. A body with no connector reads exactly as today.
11. **xls text boxes.** The k-th TXO of a sheet belongs to the k-th SpContainer with a ClientTextbox (0xF00D) in that sheet's drawing. A TXO with no partner is a paragraph after the table. The TXO text is the CONTINUE text after the TXO: flag byte 0 means 8-bit, 1 means UTF-16, `cchText` at offset 10. Its formatting-run CONTINUE is skipped.
12. **Versions.** The reader's `VERSION` goes 0.7.0 → 0.8.0, so cached outputs re-convert. `plugin.json` goes 0.18.0 → 0.19.0. `README.md` (there is no Korean README) changes in two places:
    - **The built-formats paragraph** (the one listing the built formats, near line 442): after "SmartArt text are read", add "; shapes joined by connectors (Visio, Office, ODF, Publisher, Hangul) come out as one Mermaid flowchart per page, slide, sheet or drawing, or as a nested list when the diagram is a branching tree, with loose connector ends placed by position and marked as inferred".
    - **The phase 4 Visio sentence:** "and, after the shapes, a list of the connectors glued at both ends (a Visio 5 file lists none)" becomes "and their connectors as a diagram (a Visio 5 file has no glue, so its connectors are placed by position only)".
13. **New files:**
    - `scripts/reader/diagram.py` and `scripts/reader/formats/drawingml.py`;
    - `tests/test_reader_diagram.py` and `tests/test_reader_drawingml.py`;
    - `tests/diagram_fix.py`, which holds the grammar check `mermaid_ok`. It is not a `test_*` file.

    The suite grows from 80 to 82 files.

## Global Constraints

- **Imports.** No third-party import. Every command runs as `python -I -X utf8`.
- **Leave `scripts/reader/formats/rtf_lex.py` alone.** Never edit, stage, restore or stash it.
- **Tests:**
  - They run as `python -I -X utf8 <worktree>/tests/<file>.py`. Each file defines `test_*` functions and a `main()` that runs them all and prints `pass` last.
  - Every fix gets a failing-first test.
  - Fixtures are built in memory, from bytes and XML strings, with the builders in `tests/reader_fix.py`.
  - No real document, and no real document's name or content, enters the repository, a test, a commit message or a report.
  - Every test that produces a Mermaid block asserts `diagram_fix.mermaid_ok(text)` on it.
- **Disk.** No hostile, mutated or fuzzed bytes are written to disk.
- **Output rules.**
  - Parsers never write Markdown; `diagram.py` builds model blocks only.
  - Inlines are `Text`, `Bold`, `Link`, `NoteRef`, `Break` and `Hidden` only.
- **Caps.**
  - The caps are module constants with the spec's names and values: `CONNECTIONS = 10_000`, `INFER_TOL = 0.02`, `INFER_SHAPES = 2_000`, `NAME = 80` (`diagram.py`).
  - Tree depth is `smartart.MAX_LEVEL`; container nesting is `limits.MAX_DEPTH`.
  - Each cap reads its module constant at call time, so a test can lower it.
  - Each cut is counted once, under one kind.
- **Work and memory.**
  - Never allocate from a declared count.
  - Every file-driven loop is capped. Inference is O(ends × shapes) within `INFER_SHAPES`.
  - Style, master and group chains keep a seen-set and stop at `limits.MAX_DEPTH`.
- **Guards.**
  - `ctx.guard("shape")` wraps each shape and connector a reader registers.
  - `Diagram.place` runs inside the scope's existing guard. If it raises anything but a `ReadError`, the reader falls back to the unchanged slots and counts one `shape`.
- **Errors.** Diagram data never makes a file fail. Bad glue, bad geometry and unknown kinds degrade as the spec's "Hostile input" says.
- **Porting.**
  - Only MIT, BSD, Apache-2.0 and MPL-2.0 code is ported.
  - A module that ports kordoc names `kordoc` and `src/hwp5/to-hwpx/shapes.ts` in its docstring. In the same commit, the `## kordoc` `Ported:` line in `THIRD_PARTY_NOTICES.md` gains the connect line and drawing-object geometry, with the pin. `test_notices_credit_port_sources` checks this.
  - `vsd.py`'s MPL header file list gains the libvisio functions it ports.
- **Installs and downloads.** None.
- **Untrusted files.** Python that reads a bench file runs with `-I`. Bench documents are named by doc_id only. Never read the user's Downloads, Documents or Desktop.
- **Commits.**
  - `git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a add <files>`, then `git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a commit --author "thatshoon <gagea45@gmail.com>" -m "<message>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`, as separate plain commands. Do not `cd`.
  - Stage only the files the task names, never `docs/` or `.superpowers/`.
  - Implementers never push, amend, rebase or stash, and never spawn subagents. Never touch the main checkout `C:/Users/hunje/phro-project`.
  - Messages read `reader: …` or `bench: …`.
- **Full-suite check** after every task:

  ```bash
  python -I -X utf8 C:/Users/hunje/AppData/Local/Temp/claude/5f43e189f7571e52/4034781a-9eb8-4169-8f5e-3c9f570e46ed/scratchpad/run_all.py C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a
  ```

  Expected: `N/N files exit 0`. N is 80 at the start, 81 after Task 1 and 82 after Task 3.
- **Bench.**
  - Never pass `--help` to `harness/bench.py`. Never run `bench.py calibrate` or `qa`, and never run `claude -p`.
  - Runs set `PHRO_READ_SCRIPTS=C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a/scripts`.

## Review Focus

1. **Visio arrows read through the style chain.**
   - A connector whose `EndArrow` lives only in its line style must come out as `-->`.
   - A style chain with a cycle must stop.
   - A shape whose own cell says 0 must override its master's arrow.

   Tests: Task 6 `test_arrow_from_line_style`, `test_style_chain_cycle`, `test_own_arrow_overrides_master`.
2. **Group transforms feeding inference.** A connector end inside a scaled, offset group must snap to the shape it visibly touches. Tests: Task 3 `test_group_scaled_connector_snaps`, Task 4 `test_sheet_group_transform`, Task 9 `test_ppt_child_anchor_snaps`.
3. **Flowing documents.**
   - In docx, the diagram replaces the group's extras, and nothing else in the paragraph moves.
   - In odt with no connector, output is byte-identical to before.
   - In hwpx, a section-scoped diagram lands at its first node's place.

   Tests: Task 5 `test_docx_group_only_moves_group`, Task 8 `test_odt_without_connector_unchanged`, Task 13 `test_section_scope_first_node_place`.
4. **No duplicate text.** A doc text box taken into a node is not repeated after the main text. An xls TXO is either a node or a paragraph, never both. Tests: Task 10 `test_textbox_node_not_repeated`, Task 11 `test_txo_node_not_repeated`.
5. **Escapes and fences.** Labels holding `"`, backticks, `|`, `%%`, `end`, `-->` and line breaks still pass `mermaid_ok`, and the fence still closes. Tests: Task 1 `test_label_escapes`, `test_backtick_label_fence`.

---

### Setup (before Task 1)

- Run:

  ```bash
  git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a log --oneline -1
  git -C C:/Users/hunje/phro-project/.claude/worktrees/doc-reader-phase2a status --short
  ```

  Expected: branch `doc-reader-diagrams` at the commit that adds this plan, or later. `status` may show ` M scripts/reader/formats/rtf_lex.py` (stat-dirty only).
- Run the full-suite check once. Expected: `80/80 files exit 0`.

### Task 1: `diagram.py` — model, Mermaid, trees, placement (glued edges only)

**Spec:** "Output" (all subsections), "Trees as lists", "The shared module", Local caps `CONNECTIONS`; this plan's decisions 2-6.

**Files:**
- Create: `scripts/reader/diagram.py`, `tests/test_reader_diagram.py`, `tests/diagram_fix.py`
- Modify: `scripts/reader/markdown.py` (the skip-kind table near line 69: add `connector_loose` "connector not attached" / "connectors not attached" and `infer_skipped` "diagram too large to place loose connectors" / "diagrams too large to place loose connectors")

**Interfaces:**
- **Produces:**
  - `CONNECTIONS = 10_000`, `NAME = 80`, `INFER_TOL = 0.02`, `INFER_SHAPES = 2_000`.
  - `class ConnectionBudget`, the same code as `vsdx.ConnectionBudget`, reading `diagram.CONNECTIONS`: `__init__(ctx)`, `.room`, `take(edges: list) -> list`, which counts `connections_cut` once per cut list. `vsdx.py` and `vsd.py` keep using their old copy until Task 6 deletes it from `vsdx.py` and Task 7 moves `vsd.py` to `diagram.ConnectionBudget`.
  - `class Diagram(ctx, budget: ConnectionBudget)`:
    - `shape(key, blocks: list, kind: str = "", box=None, hidden: bool = False, container: bool = False, title: str = "") -> None`. `kind` is one of `""`, `"decision"`, `"terminator"`, `"circle"`, `"store"`. `box` is `(x0, y0, x1, y1)`, normalised by the module (min, max). A repeated key keeps its first registration.
    - `connector(key, begin=None, end=None, p0=None, p1=None, arrows=(None, None), label: str = "", line: bool = False, hidden: bool = False) -> None`.
    - `place(slots: list[tuple]) -> list`. `slots` is `[(key or None, blocks)]`. It returns a flat block list. With no edge it returns the concatenated slot blocks unchanged.
  - **Mermaid text.** Lines are joined with `\n` and indented two spaces per nesting level:

    ```
    flowchart TD
      n1["Start"]
      n2{"Tests pass?"}
      n1 --> n2
      n2 -->|"No"| n3
    ```

    - Kinds render as `{"…"}`, `(["…"])`, `(("…"))`, `[("…")]`, `["…"]`.
    - Arrows per the spec's table: `(None, None)` and `(False, True)` give `-->`; `(True, False)` swaps the ends; `(True, True)` gives `<-->`; `(False, False)` gives `---`. A `None` on one side counts as `False` when the other side is known.
    - Subgraphs are `subgraph cK ["title"]` … `end`.
  - **Escapes.** `"` becomes `#quot;`; `\r\n`, `\r` and `\n` become `<br>`. They apply to node labels, edge labels and subgraph titles.
  - **Direction.** `LR` when the sum of `|dx|` over edges whose two nodes have boxes exceeds the sum of `|dy|`; otherwise `TD`.
  - **Tree output.** `ListItem(depth, "-", inlines)` with line breaks as `Break`. Follow-ups are `Para([Text("nK:")])` followed by the node's blocks, in node order, after the Mermaid block or the list.
  - **`tests/diagram_fix.py`:** `mermaid_ok(text: str) -> bool` accepts exactly these lines:
    - the header line `flowchart (TD|LR)`;
    - node lines `\s*n\d+(\["[^"\n]*"\]|\{"[^"\n]*"\}|\(\["[^"\n]*"\]\)|\(\("[^"\n]*"\)\)|\[\("[^"\n]*"\)\])`;
    - edge lines `\s*n\d+ (-->|<-->|---)(\|"[^"\n]*"\|)? n\d+`;
    - `\s*subgraph c\d+ \["[^"\n]*"\]` and `\s*end`;
    - `\s*%% .*`.

    Every edge must name declared nodes.

- [ ] **Step 1: Write the failing tests** in `tests/test_reader_diagram.py` (helpers: `d = Diagram(Ctx(), ConnectionBudget(ctx))`; `text(blocks)` joins `Code.text`):

```python
def test_no_edges_returns_slots():          # 2 shapes, no connector -> place() == concatenated slot blocks
def test_glued_edge_mermaid():              # A, B, connector glued A->B arrows (False, True) -> Code lang "mermaid",
                                            # lines 'flowchart TD', '  n1["A"]', '  n2["B"]', '  n1 --> n2'; mermaid_ok
def test_arrows_four_cases_and_unknown():   # (F,T) -->, (T,F) ends swapped, (T,T) <-->, (F,F) ---, (None,None) -->
def test_edge_label():                      # label 'yes' -> 'n1 -->|"yes"| n2'; the connector's slot is gone
def test_label_escapes():                   # 'say "hi"\nnow' -> '["say #quot;hi#quot;<br>now"]'; labels with
                                            # '|', '%%', 'end', '-->' pass mermaid_ok unchanged
def test_backtick_label_fence():            # label '```x' -> markdown.render fence longer than 3 and closes
def test_kinds():                           # decision {"…"}, terminator (["…"]), circle (("…")), store [("…")]
def test_direction_lr_and_td():             # boxes side by side -> LR; stacked -> TD; no boxes -> TD
def test_node_ids_follow_slots():           # slots order B, A -> B is n1; a slotless shape at an end is numbered last
def test_block_at_first_connected_slot():   # slots [title(None), X(unconnected), A, B] -> [title, X, Code]
def test_long_text_follow_up():             # 100-char text -> label first 80 chars + '…'; after Code: Para 'n1:' + blocks
def test_rich_node_follow_up():             # node with an Image only -> label '(picture)'; follow-up holds the Image
def test_hidden_end_blank_node():           # hidden shape at an end -> 'n2[" "]'; its slot stays in place
def test_hidden_connector_no_edge():        # hidden connector -> no Code; slots unchanged
def test_self_loop_kept():                  # glued A->A -> 'n1 --> n1'
def test_tree_as_list():                    # root R with children A, B; A with child C; unlabeled, directed ->
                                            # ListItems (0,R) (1,A) (2,C) (1,B); no Code
def test_not_trees():                       # a chain, a labeled edge, two parents, two roots, a cycle -> Mermaid
def test_tree_depth_cap():                  # depth 9 (smartart.MAX_LEVEL lowered to 2 in a try/finally) -> Mermaid
def test_container_subgraph():              # container C (title 'Lane') holding A and B by centre -> 'subgraph c1
                                            # ["Lane"]', nodes inside, 'end'; C's slot gone; no tree list
def test_nested_containers():               # outer and inner containers -> nested subgraphs; limits.MAX_DEPTH lowered
                                            # to 1 -> inner nodes go to the outer one
def test_budget_cut():                      # CONNECTIONS lowered to 1, two edges -> one edge, connections_cut == 1
def test_skip_kind_wording():               # markdown.render of a doc with skipped connector_loose 2 and
                                            # infer_skipped 1 -> 'connectors not attached', 'diagram too large ...'
```

- [ ] **Step 2: Run** `python -I -X utf8 <worktree>/tests/test_reader_diagram.py`. Expected: FAIL (`diagram` not found).
- [ ] **Step 3: Implement** `diagram.py`, `diagram_fix.py` and the two skip kinds as in Interfaces. The tree rule is the spec's: every edge directed and unlabeled, in-degree ≤ 1, one root reaching every node, some node with two or more children, depth ≤ `smartart.MAX_LEVEL`, no containers. Containers are assigned by the smallest box holding the node's centre. A container with no member node is an ordinary shape.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.** Expected: `81/81`.
- [ ] **Step 6: Commit** `scripts/reader/diagram.py`, `scripts/reader/markdown.py`, `tests/test_reader_diagram.py`, `tests/diagram_fix.py` with the message `reader: diagram module writes connected shapes as Mermaid or a tree list`.

### Task 2: `diagram.py` — loose-end inference, caps and hostile geometry

**Spec:** "Loose-end inference", Local caps `INFER_TOL`, `INFER_SHAPES`, "Hostile input".

**Files:**
- Modify: `scripts/reader/diagram.py`, `tests/test_reader_diagram.py`

**Interfaces:**
- **Consumes:** Task 1's `Diagram`.
- **Produces:**
  - **Candidates.** A connector with at least one end unglued is a candidate. `line=True` makes it a candidate only when one of its `arrows` is `True`.
  - **Snapping.**
    - `tol = INFER_TOL × diagonal` of the box spanning every shape box and every line end in the scope.
    - If the point is inside one or more boxes, the smallest area wins.
    - Otherwise the nearest box within `tol` wins.
    - Equal areas, or equal distances within `1e-9 × tol`, leave the end unresolved.
    - Containers and connectors are never targets.
  - **Outcome.**
    - When both ends resolve, to different shapes, the connector becomes an inferred edge. It is written after `%% inferred from position`.
    - Otherwise it counts `connector_loose` once per connector, and its slot stays.
  - **Size skip.** More than `INFER_SHAPES` shapes with boxes in the scope means no inference, `infer_skipped` once for the scope, and each loose connector's slot stays (not counted as `connector_loose`).
  - **Hostile values.** A coordinate that is NaN, infinite or of absolute value over `1e9` turns that box or point into `None`. A zero-size scope (diagonal 0) infers nothing.
  - **Bad glue.** A glue key that names no registered shape, names the connector itself, or names another connector is treated as unglued.

- [ ] **Step 1: Write the failing tests:**

```python
def test_infer_inside_box():             # p0 inside A, p1 inside B -> '%% inferred from position' then 'n1 --> n2'
def test_infer_nested_smallest():        # p0 inside both Outer and Inner -> Inner
def test_infer_within_tol():             # end at 0.019 x diagonal from A's edge -> snaps; at 0.021 -> loose
def test_infer_exactly_at_tol():         # distance == tol -> snaps
def test_infer_tie_unresolved():         # equidistant from A and B -> connector_loose 1, slot kept
def test_infer_same_shape_both_ends():   # both ends in A -> no edge, connector_loose 1
def test_one_end_glued_other_inferred(): # begin glued A, p1 inside B -> inferred edge A->B
def test_plain_line_needs_arrow():       # line=True arrows (False, False) -> no edge, not counted; (False, True) -> edge
def test_infer_shapes_cap():             # INFER_SHAPES lowered to 2 with 3 boxed shapes -> infer_skipped 1, no edge
def test_nan_and_huge_geometry():        # NaN box and 1e10 point -> no inference, no exception
def test_zero_size_scope():              # all boxes at one point -> no inference
def test_bad_glue_is_unglued():          # begin='missing', end=self key, end=another connector -> treated as loose
```

- [ ] **Step 2: Run** the file. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `diagram.py` and its test with the message `reader: diagram places loose connector ends by position, within caps`.

### Task 3: `formats/drawingml.py` and pptx

**Spec:** "Per-format extraction" rows pptx; "Shapes, edges and layout" (DrawingML `prst`). Background: explore file "3. ooxml_slides.py".

**Files:**
- Create: `scripts/reader/formats/drawingml.py`, `tests/test_reader_drawingml.py`
- Modify: `scripts/reader/formats/ooxml_slides.py` (move `_Xf`, `_num`, `_xfrm`, `_kid`, `_nv` out; let `cxnSp` in at the `_SHAPES` check near line 356; build slots from `_reading_order(out)` with the `cNvPr@id` key; register shapes, including titles and textless shapes, and connectors), `tests/test_reader_slides.py`

**Interfaces:**
- **Produces in `drawingml.py`:**
  - `Xf` (the moved `_Xf`, public), with these additions:
    - `box(xfrm) -> tuple | None`: off/ext mapped through the transform. With `rot` (60000ths of a degree), it is the axis-aligned bound of the rotated rectangle about its centre.
    - `ends(xfrm) -> tuple | None`: `((x0, y0), (x1, y1))`, the start corner after `flipH`/`flipV` and the opposite corner, rotated about the box centre.
  - `num`, `xfrm`, `kid`, `nv` (the moved helpers, public).
  - `arrows(sppr) -> tuple`: `(start, end)` from `a:ln/a:headEnd@type` and `a:tailEnd@type`. Present and not `none` is `True`; otherwise `False` (decision 7).
  - `kind(sppr) -> str` from `a:prstGeom@prst`:
    - `flowChartDecision` and `diamond` give `decision`;
    - `flowChartTerminator` gives `terminator`;
    - `ellipse` and `flowChartConnector` give `circle`;
    - `can` and `flowChartMagneticDisk` give `store`;
    - anything else gives `""`.
  - `is_line(sppr) -> bool`: `prst` is `line` or `straightConnector1`.
  - `glue(el) -> tuple`: `(stCxn@id, endCxn@id)`, each `None` when absent. It looks under `nvCxnSpPr/cNvCxnSpPr`, or under `wps:cNvCnPr` for Word.
- **pptx slide scope:**
  - **Diagram calls.**
    - `shape()` is called for every `sp`, `pic` and `graphicFrame`. Titles and skipped placeholders are passed with their box but have no slot.
    - `connector()` is called for every `cxnSp`, with its glue, ends, arrows and plain text.
    - An `sp` with `is_line` is passed as `connector(line=True)`.
  - **Slots** are `[(id, blocks)]` from `_reading_order`.
  - **Units** are EMU.

- [ ] **Step 1: Write the failing tests.** `tests/test_reader_drawingml.py`:

```python
def test_box_identity_and_group():       # off (10,20) ext (30,40) in a group off 100 chOff 0 scale 2 -> (120,140,180,220)
def test_box_rotated_90():               # 100x20 box rot=5400000 -> bound 20x100 about the same centre
def test_ends_flips():                   # off (0,0) ext (10,5): no flip ((0,0),(10,5)); flipH ((10,0),(0,5)); flipV ((0,5),(10,0))
def test_arrows_and_kind():              # tailEnd triangle -> (False, True); no ln -> (False, False); prst map table
def test_glue_pptx_and_word():           # cNvCxnSpPr stCxn id=2 endCxn id=3 -> ('2','3'); wps:cNvCnPr same
```

Add to `tests/test_reader_slides.py`; a `cxn(id, st, end, off, ext, head, tail, flip)` fixture builds a `p:cxnSp`:

```python
def test_slide_connector_mermaid():      # sp 'Start' id 2, sp 'End' id 3, cxnSp 4 glued 2->3 tailEnd -> slide body holds
                                         # 'n1["Start"]', 'n1 --> n2', and 'Start' appears once in the markdown
def test_slide_title_stays_title():      # a title glued to a shape -> '## Slide 1: T' stays; the node uses T's text
def test_group_scaled_connector_snaps(): # shapes and an unglued connector inside a scaled grpSp -> inferred edge
def test_slide_without_connector_unchanged():  # the existing deck fixtures render byte-identical to before
```

- [ ] **Step 2: Run** both files. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. `ooxml_slides` imports the moved helpers from `drawingml`.
- [ ] **Step 4: Run** both files. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.** Expected: `82/82`.
- [ ] **Step 6: Commit** `drawingml.py`, `ooxml_slides.py` and both tests with the message `reader: DrawingML geometry helpers; pptx connectors become a diagram`.

### Task 4: xlsx and xlsb sheet drawings

**Spec:** "Per-format extraction" rows pptx, xlsx. Background: explore file "4. sheet_drawing.py".

**Files:**
- Modify: `scripts/reader/formats/sheet_drawing.py` (`_descend` tracks `grpSp` transforms through `drawingml.Xf.group` and yields `cxnSp`; `anchors` builds `(id, blocks)` slots and calls `place()`), `tests/test_reader_sheet_drawing.py`

**Interfaces:**
- **Consumes:** `drawingml.Xf`, `xfrm`, `arrows`, `kind`, `is_line`, `glue`, `nv`; `diagram.Diagram`.
- **Produces:**
  - **Scope.** One drawing part is one scope: one `Diagram` per `anchors()` call.
  - **Units.**
    - A shape's box is its `xdr:spPr/a:xfrm` in EMU.
    - A `oneCellAnchor` or `absoluteAnchor` shape without an `xfrm` uses `xdr:pos`/`xdr:ext`.
    - A `twoCellAnchor` without an `xfrm` has `box=None`.
  - **Order.** Shape text keeps its `SHAPE_CHARS` charging and its anchor order.

- [ ] **Step 1: Write the failing tests:**

```python
def test_sheet_connector_xlsx():        # two xdr:sp and a glued xdr:cxnSp -> Mermaid after the sheet table; texts once
def test_sheet_connector_xlsb():        # the same drawing through the xlsb helpers
def test_sheet_group_transform():       # members in a grpSp with chOff/chExt scale 2 and an unglued connector -> inferred
def test_sheet_without_connector_unchanged():  # the existing SHAPES fixture renders as before
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file and `tests/test_reader_xlsx.py`, `tests/test_reader_xlsb.py`. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `sheet_drawing.py` and its test with the message `reader: sheet drawings group transforms and connectors become a diagram`.

### Task 5: docx groups and canvases

**Spec:** "Per-format extraction" row docx; decision 10. Background: explore file "5. ooxml_word.py".

**Files:**
- Modify: `scripts/reader/formats/ooxml_word.py` (a `wpg:wgp` or `wpc:wpc` reached in `node()` opens a scope collector; each member `wps:wsp`'s `txbxContent` blocks become one slot keyed by `wps:cNvPr@id`; at the group's end `place()` runs, and its result goes to `st.extras` in place of the members' extras), `tests/test_reader_docm.py`

**Interfaces:**
- **Consumes:** `drawingml` helpers; `diagram.Diagram`.
- **Produces:**
  - **Scope** per group or canvas, with boxes from `wps:spPr/a:xfrm` through `wpg:grpSpPr`.
  - **Connectors** are a `wps:wsp` with `wps:cNvCnPr`.
  - **Unchanged paths.** A `wps:wsp` outside any group, and VML, read exactly as today.

- [ ] **Step 1: Write the failing tests:**

```python
def test_docx_group_connector():          # wpg:wgp with two wps text boxes and a glued wps connector -> one Mermaid
                                          # block after the anchoring paragraph; each box text once
def test_docx_canvas_connector():         # the same inside wpc:wpc
def test_docx_group_only_moves_group():   # paragraph text before and after the drawing keeps its order and text
def test_alternate_textbox_once():        # existing test still passes (VML fallback not read twice)
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `ooxml_word.py` and its test with the message `reader: docx group and canvas connectors become a diagram`.

### Task 6: vsdx — geometry, arrows through masters and styles, containers; the list goes

**Spec:** "Per-format extraction" row vsdx; "Containers"; "Where the diagram goes" (the `Connections:` list is removed). Background: explore file "1. vsdx.py".

**Files:**
- Modify: `scripts/reader/formats/vsdx.py`, `tests/test_reader_vsdx.py` (the tests listed in the background file that assert `Connections:` are rewritten to assert the diagram)

**Interfaces:**
- **Consumes:** `diagram.Diagram`, `diagram.ConnectionBudget`.
- **Produces:**
  - **Cells.** `_cells` also reads `Width`, `Height`, `Angle`, `FlipX`, `FlipY`, `BeginX`, `BeginY`, `EndX`, `EndY`, `BeginArrow` and `EndArrow`. A cell missing on the shape is taken from its master shape element. `BeginArrow` and `EndArrow` then fall back to the `LineStyle` chain in `visio/document.xml` `StyleSheets/StyleSheet`: loaded once and cached, with a seen-set, up to `limits.MAX_DEPTH`. A value > 0 is an arrowhead; a chain with no value is `None`.
  - **Geometry.**
    - The box is `(PinX - LocPinX, PinY - LocPinY)` plus `(Width, Height)` in page units, offset by the parent group's origin. `Angle` is applied as the bound of the rotated box about the pin.
    - A shape with `BeginX` and `EndX` is a connector. Its ends are `(BeginX, BeginY)` and `(EndX, EndY)` plus its parent's origin.
  - **Kinds** come from the master's `NameU` (`masters.xml`), else the shape's `NameU`, per the spec's table, case-folded.
  - **Containers.** `Section N="User"`, `Row N="msvStructureType"`, `Cell N="Value"`, value `Container` (own or master), make a container. Its text is the title.
  - **Slots** are `out` with the shape `ID` as key. Every shape is registered, including textless ones.
  - **Removed:** `connections`, `name_of`, `NAME`, `CONNECTIONS`, `ConnectionBudget` and the glued-only text rule (Task 1's `place` now drops edge connectors' slots).
  - **Bridge for vsd.** `vsd.py` calls `vsdx.connections`, `vsdx.name_of` and `vsdx.ConnectionBudget` until Task 7. So Task 6 moves `connections` and `name_of` into `vsd.py` as private `_connections` and `_name_of`, unchanged, and points vsd at `diagram.ConnectionBudget`. vsd's output and its tests stay as they are until Task 7 removes those two functions. The `VX.name_of`, `VX.connections` and `VX.CONNECTIONS` uses in `tests/test_reader_vsd.py` switch to the vsd and diagram names.

- [ ] **Step 1: Write the failing tests** (rewrite the listed `Connections:` tests and add):

```python
def test_connections():                  # now: 'flowchart' block, 'n1["Start"]', 'n1 --> n2'; no 'Connections:'
def test_arrow_from_line_style():        # EndArrow only in StyleSheet 3 (LineStyle="3") -> '-->'; BeginArrow there -> '<-->'
def test_style_chain_cycle():            # StyleSheet 3 -> 4 -> 3 with no arrows -> stops, '-->' (not known)
def test_own_arrow_overrides_master():   # master EndArrow 13, shape EndArrow 0, BeginArrow 0 -> '---'
def test_vsdx_kinds():                   # master NameU 'Decision' -> {"…"}; 'Start/End' -> (["…"])
def test_vsdx_container():               # a shape with User.msvStructureType=Container around two connected shapes ->
                                         # 'subgraph c1 ["<its text>"]'
def test_one_end_connector_inferred():   # BeginX glued, EndX free but inside shape B's box -> inferred edge
def test_group_child_box():              # sub-shapes of a group get page-absolute boxes (pins + parent origin)
```

- [ ] **Step 2: Run** `tests/test_reader_vsdx.py`. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** `tests/test_reader_vsdx.py` and `tests/test_reader_vsd.py`. Expected: `pass`, with vsd's output unchanged.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `vsdx.py`, `vsd.py` and both tests with the message `reader: vsdx pages write their connectors as a diagram, arrows through masters and line styles`.

### Task 7: vsd — XForm, XForm1D, line markers and style sheets

**Spec:** "Per-format extraction" row vsd; this plan's decision 9. Background: explore file "2. vsd.py" (byte offsets from libvisio).

**Files:**
- Modify: `scripts/reader/formats/vsd.py` (MPL header file list), `tests/test_reader_vsd.py` (`test_connections`, `test_connection_budget_shared`, `test_hidden_text`, `test_edge_cap_per_page` rewritten; `shape_data` grows to hold the style ids)

**Interfaces:**
- **Consumes:** `diagram.Diagram`, `diagram.ConnectionBudget`.
- **Produces:**
  - **0x9B XForm** also gives Width (offset 19), Height (28), Angle (55), FlipX (byte 63) and FlipY (byte 64).
  - **New handler `0x9D: "xform1d"`:** BeginX (1), BeginY (10), EndX (19), EndY (28).
  - **0x85 line:** the start marker (byte 25) and end marker (byte 26); nonzero is an arrowhead.
  - **Style ids.** The shape chunk's line style id is the u32 at offset 42, read only when the chunk data is long enough.
  - **Style sheets** are 0x4A chunks in the 0x1A stream. Each has its parent line style at 0x22 and its own 0x85 child. The chain is walked like vsdx's.
  - **Master entries** carry the new fields.
  - **Slots** are `placed + loose` keyed by `s.sid`. Edges come from `s.begin`/`s.end` as glue. Visio 5 has none, so it uses inference only.
  - **Removed:** Task 6's bridge, `_connections` and `_name_of`, and the glued-only text rule.

- [ ] **Step 1: Write the failing tests** (rewrite the four listed and add):

```python
def test_connections():               # v11 and v6: Mermaid with 'n1 -->|"flows"| n2'; 'Connections:' gone
def test_xform_extras_and_1d():       # width/height give boxes; an unglued 1-D shape with ends inside two boxes -> inferred
def test_line_markers():              # 0x85 end marker 13 -> '-->'; start and end -> '<-->'; both 0 -> '---'
def test_style_sheet_arrow():         # marker only on line style sheet 2 (0x4A in 0x1A) -> '-->'
def test_v5_inference_only():         # Visio 5 drawing with boxes -> inferred edge, no glue
```

- [ ] **Step 2: Run** `tests/test_reader_vsd.py`. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces, following libvisio `readXFormData`, `readXForm1D`, `readLine`, `readShape` and `readStyleSheet`. Every new read is checked against the chunk length and charged to `BIN_RECORDS`.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `vsd.py` and its test with the message `reader: vsd pages write their connectors as a diagram, with geometry, markers and line styles`.

### Task 8: ODF — odp, ods, odt

**Spec:** "Per-format extraction" row odp/odt/ods; decision 10. Background: explore file "1. ODF".

**Files:**
- Modify: `scripts/reader/formats/odf_slides.py` (slots in `_Slides.slide`, titles out of slots); `scripts/reader/formats/odf_sheets.py` (all children of `table:shapes`, not only frames); `scripts/reader/formats/odf_base.py` and `scripts/reader/formats/odf.py` (the odt connector pre-scan and shape slots); `tests/test_reader_ods_odp.py`, `tests/test_reader_odt.py`

**Interfaces:**
- **Consumes:** `diagram.Diagram`.
- **Produces:**
  - **Keys:** `draw:id`, else `xml:id`.
  - **Glue:** `draw:start-shape` and `draw:end-shape`.
  - **Ends:** `svg:x1`…`svg:y2` in cm, through the existing `_cm`.
  - **Boxes:** `svg:x`, `svg:y`, `svg:width`, `svg:height` in cm. `draw:transform` is ignored.
  - **Arrows** are read with `body.styles.prop(style, "graphic", "marker-start"/"marker-end")` on `draw:style-name`, then `presentation:style-name`. A name gives `True`, an empty value or `none` gives `False`, and absent gives `None`.
  - **Kinds:** `draw:enhanced-geometry@draw:type` per the spec's table; `draw:ellipse` and `draw:circle` are circles.
  - **Lines:** a `draw:line` is a connector with `line=True`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_odp_connector():                   # two custom-shapes with draw:id and a draw:connector start/end -> Mermaid; texts once
def test_odp_marker_from_style_chain():     # marker-end on the parent graphic style -> '-->'; none -> '---'
def test_ods_shapes_connector():            # table:shapes holding two shapes and a connector -> Mermaid after the table
def test_odt_connector():                   # shapes anchored in two paragraphs, a connector -> one Mermaid at the first
                                            # shape's place, shapes' text not inlined in the paragraphs
def test_odt_without_connector_unchanged(): # an odt with custom-shapes and no connector -> markdown identical to before
def test_odt_divider_line_ignored():        # a draw:line with no markers and no glue -> not an edge, not counted
```

- [ ] **Step 2: Run** both files. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** both files. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** the four modules and two tests with the message `reader: ODF connectors in slides, sheets and text become a diagram`.

### Task 9: OfficeArt solver and ppt

**Spec:** "Per-format extraction" row ppt/doc/xls/pub; the ODRAW column of the kinds table. Background: explore file "0. Cross-cutting" and "2. ppt".

**Files:**
- Modify: `scripts/reader/officeart.py`, `scripts/reader/formats/ppt_shapes.py` (`_shape` keeps flags and arrows; `walk` reads 0xF005 and returns the rules alongside the shapes), `scripts/reader/formats/ppt.py` (`_drawn` carries the spid; `_slide_at` builds slots without the title and calls `place()`), `tests/reader_fix.py` (`oa_shape` gains `inst=75`, `flags=0xA00` and `props=b""` keywords; new `oa_solver(*rules)`, where a rule is `(spidA, spidB, spidC)`), `tests/test_reader_ppt_shapes.py` (it also holds the new `officeart` tests; there is no officeart test file)

**Interfaces:**
- **Produces in `officeart.py`:**
  - `SOLVER = 0xF005`, `CONNECTOR_RULE = 0xF012`.
  - `rules(data, start, end) -> list[tuple[int, int, int]]`: `(spidA, spidB, spidC)` for each 24-byte ConnectorRule in the SolverContainers in range, capped by `MAX_RECORDS`.
  - `fsp(body) -> tuple[int, int]`: `(spid, flags)`.
  - `FLIP_H = 0x40`, `FLIP_V = 0x80`.
  - `arrows(data, inst, body, size) -> tuple[bool, bool]`: through `fopt_property` 0x01D0 and 0x01D1; nonzero is `True`, absent is `False`.
  - `KINDS = {110: "decision", 4: "decision", 116: "terminator", 3: "circle", 120: "circle", 22: "store", 132: "store"}`.
  - `CONNECTOR_TYPES = range(32, 41)`, `LINE_TYPE = 20`.
- **ppt:** the slide is the scope. Boxes and ends are in master units after the group map. A connector runs from its box's start corner after the leaf flips. A rule names connector `spidC` with ends `spidA` and `spidB`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_rules_walk():                # DgContainer + oa_solver((2,3,4)) -> rules == [(2,3,4)]; truncated rule ignored
def test_fsp_flags_and_arrows():      # flags 0xA40 -> FLIP_H set; FOPT 0x01D1=1 -> (False, True)
def test_ppt_connector_mermaid():     # slide shapes 'Start' spid 2, 'End' 3, connector type 32 spid 4, rule (2,3,4),
                                      # end arrow -> Mermaid 'n1 --> n2'; texts once; slide title kept as title
def test_ppt_child_anchor_snaps():    # shapes in a group with ChildAnchor and an unglued connector -> inferred edge
def test_ppt_without_solver_unchanged():  # existing slide fixtures render as before
```

- [ ] **Step 2: Run** the files. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the files and `tests/test_reader_ppt.py`. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `officeart.py`, `ppt_shapes.py`, `ppt.py`, `tests/reader_fix.py` and the tests with the message `reader: OfficeArt connector rules; ppt connectors become a diagram`.

### Task 10: doc — connected floating shapes and their text boxes

**Spec:** "Per-format extraction" row doc; "Node text gaps" (doc); decision 8. Background: explore file "3. doc".

**Files:**
- Modify:
  - `scripts/reader/formats/doc.py`: `boxes()` returns `{lid: blocks}` with order kept. `drawn()` adds a slot for a shape whose spid is a rule end, carrying its text-box blocks. `run()` skips those lids after the main text and calls `place()` over the main story.
  - `scripts/reader/formats/doc_pictures.py`: `Drawing` keeps the rules from `officeart.rules`.
  - `tests/reader_fix.py`: `dgg_with_bstore` takes `solver=b""`.
  - `tests/test_reader_doc_notes.py`.

**Interfaces:**
- **Consumes:** `officeart.rules`, `officeart.arrows`, `officeart.fsp`, `officeart.KINDS`; `diagram.Diagram`.
- **Produces:**
  - **Scope:** the main story.
  - **Boxes:** `box=None` for top-level floats (decision 8), so glue only.
  - **Text-box link:** a text box belongs to the shape whose spid equals its FTXBXS `lid` (offset 14).

- [ ] **Step 1: Write the failing tests:**

```python
def test_doc_connector_textboxes():      # two text boxes (lid 1, 2), a connector rule (1,2,3) -> Mermaid at the first
                                         # anchor's paragraph; box texts are node labels
def test_textbox_node_not_repeated():    # those box texts do not appear after the main text
def test_unconnected_textbox_unchanged():# a third text box stays after the main text, as today
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file and the other `tests/test_reader_doc*.py`. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `doc.py`, `doc_pictures.py`, `tests/reader_fix.py` and the test with the message `reader: doc connectors become a diagram of their text boxes`.

### Task 11: xls — TXO text and connectors

**Spec:** "Per-format extraction" row xls; "Node text gaps" (xls); decision 11. Background: explore file "4. xls".

**Files:**
- Modify:
  - `scripts/reader/formats/xls.py`: `_walk` collects TXO text per sheet.
  - `scripts/reader/formats/xls_drawing.py`: a `shapes(...)` function next to `pictures` registers every shape, using cell boxes `x = col + dx/1024`, `y = row + dy/256`, and builds slots.
  - `tests/reader_fix.py`: `xls_txo(text)` builds the TXO record and its two CONTINUEs.
  - `tests/test_reader_xls.py`.

**Interfaces:**
- **Consumes:** `officeart` helpers; `diagram.Diagram`.
- **Produces:**
  - **Scope:** the sheet.
  - **Unconnected text boxes:** a paragraph each, after the sheet's table, in anchor order.
  - **Pictures:** unchanged.
  - **Embedded workbooks** (`pictures=False`) still skip drawings.

- [ ] **Step 1: Write the failing tests:**

```python
def test_xls_textbox_text():          # one text box with TXO 'note' -> 'note' after the table (was dropped)
def test_xls_connector():             # two text boxes, a connector shape, rule (spids) -> Mermaid; labels from TXO
def test_txo_node_not_repeated():     # node texts are not also written as paragraphs
def test_txo_utf16_and_8bit():        # flag byte 1 with Korean text and flag 0 with Latin text both read
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `xls.py`, `xls_drawing.py`, `tests/reader_fix.py` and the test with the message `reader: xls text boxes are read, and their connectors become a diagram`.

### Task 12: pub

**Spec:** "Per-format extraction" row pub. Background: explore file "5. pub".

**Files:**
- Modify:
  - `scripts/reader/formats/pub.py`: `_Boxes.shape` keeps the full box, the FSP spid and the arrows. `_Boxes.walk` reads 0xF005 in the DG. `_Pages._shapes` builds `(seqnum, blocks)` slots and calls `place()`.
  - `scripts/reader/formats/pub_2k.py`: boxes only, for inference.
  - `tests/reader_fix.py`: `pub_sp` takes `spid=0` and `props=b""`; `pub_escher` takes `solver=b""`.
  - `tests/test_reader_pub.py`.

**Interfaces:**
- **Consumes:** `officeart` helpers; `diagram.Diagram`.
- **Produces:**
  - **Scope:** a page.
  - **Rules:** a rule is kept on a page only when both ends' seqnums are among the page's shapes. The spid maps to the seqnum through each SpContainer's ClientData.
  - **Publisher 2000 and 97/98** have no Escher, so they get inference only.

- [ ] **Step 1: Write the failing tests:**

```python
def test_pub_connector():            # two text shapes and a connector with a solver rule -> Mermaid on that page
def test_pub_rule_other_page():      # a rule whose ends are on different pages -> no edge on either page
def test_pub2k_inferred():           # 2000-format shapes and a connector line with an arrow touching both -> inferred
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the file. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `pub.py`, `pub_2k.py`, `tests/reader_fix.py` and the test with the message `reader: Publisher connectors become a diagram`.

### Task 13: hwpx (kordoc)

**Spec:** "Per-format extraction" row hwpx; decision 8. Port source: kordoc `src/hwp5/to-hwpx/shapes.ts` (`$col` → `hp:connectLine`, `CONNECT_TYPES`). Background: explore file "6. Hangul / hwpx".

**Files:**
- Modify: `scripts/reader/formats/hwpx.py`, `THIRD_PARTY_NOTICES.md` (`## kordoc` line), `tests/test_reader_hwpx.py`

**Interfaces:**
- **Consumes:** `diagram.Diagram`.
- **Produces:**
  - **Recognised elements:** `node()` recognises `rect`, `ellipse`, `arc`, `polygon`, `curve`, `line`, `connectLine`, `container`, `textart` and `pic` as drawing objects. Each one's `drawText` blocks become one slot keyed by `instid`, else `id`. The slot is carried through `para()` to the section.
  - **Scope.** The section is the scope, and an `hp:container` is its own nested scope when every connector end inside it is inside it.
  - **Glue.** `hp:connectLine/hp:startPt@subjectIDRef` and `hp:endPt@subjectIDRef`; `0` or absent means not glued.
  - **Arrows** come from `@type`: `*_NOARROW` gives `(False, False)`, `*_ONEWAY` gives `(False, True)`, `*_BOTH` gives `(True, True)`.
  - **Boxes.**
    - In a container: `hp:offset@x,y` plus `hp:sz@width,height` (else `hp:curSz`), in HWPUNIT.
    - At top level: only when `hp:pos@vertRelTo` and `@horzRelTo` are `PAPER` or `PAGE`, from `vertOffset`/`horzOffset` and `hp:sz`.
  - **Connector ends** are the connector's own box origin plus `startPt`/`endPt` `x, y`.
  - **Kinds:** `ellipse` gives `circle`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_hwpx_connectline():                # two hp:rect with instid 11, 12 and drawText, connectLine STRAIGHT_ONEWAY
                                            # subjectIDRef 11 -> 12 -> Mermaid 'n1 --> n2'; texts once
def test_hwpx_both_and_none_arrows():       # STROKE_BOTH -> '<-->', ARC_NOARROW -> '---'
def test_section_scope_first_node_place():  # shapes in paragraphs 1 and 3 -> the block sits at paragraph 1's shape place
def test_hwpx_container_inference():        # hp:container with two rects and an unglued connectLine whose points fall
                                            # in both -> inferred edge
def test_text_box():                        # the existing text-box test still passes
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces, and extend the kordoc `Ported:` line ("the connect line and drawing-object glue and geometry, from `src/hwp5/to-hwpx/shapes.ts` at commit 9be0346ca74ff80822f6e94b811e3e0fbffd0b0b").
- [ ] **Step 4: Run** the file and `tests/test_reader_gate.py` (`test_notices_credit_port_sources`). Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `hwpx.py`, `THIRD_PARTY_NOTICES.md` and the test with the message `reader: hwpx connect lines become a diagram (kordoc)`.

### Task 14: hwp (kordoc)

**Spec:** "Per-format extraction" row hwp; decision 8. Port source: kordoc `shapes.ts` (`drawingStyle` for the instance id; the `$col` case for the LINE record layout). Background: explore file "6. Hangul / hwp".

**Files:**
- Modify:
  - `scripts/reader/formats/hwp.py`: `drawing()` and `component()` read the SHAPE_COMPONENT data and keep an offset stack for groups. The instance id comes after line, fill and shadow, as in kordoc's `drawingStyle`. A `$col` component's LINE record (tag 78) gives start x/y, end x/y, type, then the four u32 `startSubject`, `startIdx`, `endSubject` and `endIdx`.
  - `scripts/reader/formats/hwp_records.py`: `SHAPE_LINE = 78` and `SHAPE_RECT = 79` constants.
  - `tests/reader_fix.py`: `hwp_shape(kind, inst_id, box, text, line=None)`.
  - `THIRD_PARTY_NOTICES.md`.
  - `tests/test_reader_hwp.py`.

**Interfaces:**
- **Consumes:** `diagram.Diagram`.
- **Produces:**
  - **Keys:** the instance id.
  - **Arrows** from the connect type, as hwpx.
  - **Boxes** follow hwpx's rule, from the CTRL_HEADER offsets and the component sizes.
  - **Truncated records.** Every read checks the record length. A truncated component gives `box=None` and no glue, never an error.

- [ ] **Step 1: Write the failing tests:**

```python
def test_hwp_connect_line():         # two rect gso with instance ids 5, 6 and a $col gso glued 5 -> 6, ONEWAY -> Mermaid
def test_hwp_truncated_component():  # a $col LINE record cut after the end points -> no edge, no exception
def test_group_shape_text():         # existing group tests still pass
```

- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces; extend the kordoc `Ported:` line with `hwp.py`.
- [ ] **Step 4: Run** the file and `tests/test_reader_gate.py`. Expected: `pass`.
- [ ] **Step 5: Run the full-suite check.**
- [ ] **Step 6: Commit** `hwp.py`, `hwp_records.py`, `tests/reader_fix.py`, `THIRD_PARTY_NOTICES.md` and the test with the message `reader: hwp connect lines become a diagram (kordoc)`.

### Task 15: Mutation tests and versions

**Spec:** "Hostile input"; this plan's decision 12.

**Files:**
- Modify: `tests/test_reader_mutation.py`, `scripts/reader/__init__.py` (`VERSION = "0.8.0"`), `.claude-plugin/plugin.json` (`0.19.0`), `README.md`

**Interfaces:**
- **Consumes:** the fixtures of Tasks 3-14.
- **Produces:** `test_diagram_mutations`. It mutates these fixtures in memory, with the existing `CASES` per seed and under `SLOW`:
  - a pptx slide with a glued and a loose connector;
  - a vsdx page with a style-chain arrow;
  - a ppt slide with a solver;
  - an hwpx section with a connectLine;
  - an hwp `$col`.

  Each outcome must be a `Doc` or a `ReadError`.

- [ ] **Step 1: Write the test.**
- [ ] **Step 2: Run** `python -I -X utf8 <worktree>/tests/test_reader_mutation.py`. A case that raises anything else, or passes `SLOW`, is a defect: fix it in the owning module with a failing-first test there, then run again.
- [ ] **Step 3: Bump** the versions and add the README lines (decision 12).
- [ ] **Step 4: Run the full-suite check.** Expected: `82/82`.
- [ ] **Step 5: Commit** the test, any fixed module, `__init__.py`, `plugin.json` and `README.md` with the message `reader: diagram mutation tests; version 0.8.0`.

### Task 16 (controller): Bench run and addendum

**Spec:** Success; Testing ("Bench").

- [ ] **Step 1: Run the bench before and after.** Keep `phro-read` outputs from 23578a2 (main) as the before set: check out main's `scripts` into a temporary worktree outside the repository, or reuse the last stored run. Then run the engine at this branch on every corpus document of these formats: vsd, vsdx, pptx, pptm, ppsx, potx, xlsx, xlsm, xlsb, docx, docm, odp, ods, odt, ppt, doc, xls, pub, hwp, hwpx. Use the phase 4 commands (`harness/bench.py run --engine phro-read` with `PHRO_READ_SCRIPTS`).
- [ ] **Step 2: Check lines.** Every non-empty line of each before output must be found in the after output, once `#quot;` → `"` and `<br>` → newline are undone and Mermaid node and edge syntax is stripped to the label text. Any miss is a defect, fixed with a failing-first test in the owning task's file.
- [ ] **Step 3: Write the addendum.** `reports/written/phro-read-diagrams.md`, in Korean as before, lists:
  - the documents (by doc_id) that gained a diagram;
  - their node and edge counts, and how many edges were inferred;
  - the token change;
  - the line-check result.
- [ ] **Step 4: Commit** the addendum in the bench repository with the message `bench: diagrams addendum`.

## Open points for the owner

1. **Real samples.** The corpus has no connector in hwp, hwpx, pptx, docx or xlsx; one odt divider line; and three vsdx files with arrows and styles, one with containers. Glue in those formats is tested only on fixtures built from the specs and kordoc. Getting real samples needs a download, which needs your yes.
2. **vsd containers** are not read (decision 9). Visio binary swimlanes come out as plain shapes.
3. **doc inference** is off (decision 8). Loose connectors in doc are counted, not placed.
