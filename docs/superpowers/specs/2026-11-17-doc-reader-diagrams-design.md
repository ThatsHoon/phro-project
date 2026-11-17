# doc-reader diagrams: shapes and connectors as Mermaid

Approved 2026-11-17 (the user: "Approve, write plan"), written from the user's answers in the brainstorm. It amends `docs/superpowers/specs/2026-11-11-doc-reader-engine-design.md` (the engine spec); the engine spec edits listed at the end are made in the commit that records its approval. It is the first of three content-gap specs, in the user's order: ① diagrams (this), ② WMF/EMF, ③ OCR. Research behind it, outside this repository: the session notes `research-wmf-diagram.md` (diagram part).

## Goal

A diagram in any document the engine reads reaches Claude as structure, not as loose shape text. Shapes joined by connectors become one Mermaid `flowchart` block. Its nodes carry the shapes' text and its edges carry the connectors' direction and labels. A diagram that is a branching tree, such as an org chart, becomes a nested list. All of this is text: nothing is installed and nothing is rendered.

Why Mermaid: on the TextFlow benchmark, Claude 3.5 Sonnet answered flowchart questions at 76.61 from the image and at 82.04 from Mermaid (82.74 from Graphviz, 71.22 from PlantUML). Vision models are weakest at edge direction, which is exactly what a connector list carries. Docling and MarkItDown drop connectors.

Success:

- Every connector the file glues to two shapes becomes an edge. vsd and vsdx already list these today; the Office, ODF, legacy binary and Hangul formats join them.
- A connector with a loose end becomes an edge when position places that end on a shape. It is marked as inferred.
- The connected shapes' text appears once, in the diagram. Shapes with no connector stay in the text flow as today.
- No bench regression: every non-empty text line the engine gave before is still found in its output. Text that moves into a node label is matched after the label escapes are undone.

## Decisions (user, 2026-11-17)

1. Three specs, in the order ① diagrams → ② WMF/EMF → ③ OCR. Each one goes through its own plan and implementation.
2. Common principle for all three: text first. Images are only links, which Claude opens when it needs them.
3. Scope: a common graph module that writes Mermaid; connectors in OOXML; connectors in ODF and in the legacy binaries; loose-end inference. Hangul (hwp, hwpx) is in.
4. Connected shapes move into the diagram and leave the text flow.
5. Approach A: one shared module, `reader/diagram.py`. Readers only extract; the module numbers, escapes, orients, infers and renders. The model does not change.
6. The output and inference rules below are approved as presented: block placement, node IDs, the 80-character label rule, arrowheads, trees as lists, the 2% tolerance and the `%% inferred` comment.

## Output

### Where the diagram goes

- **One block per scope.** A scope is one drawing surface:
  - a Visio page;
  - a slide (ppt, pptx, odp);
  - a sheet's drawing (xls, xlsx, ods);
  - a Publisher page;
  - a drawing group or canvas in a flowing document (doc, docx, odt, hwp, hwpx).
  - In hwp and hwpx, a connector that is not inside a group, or whose ends lie outside its group, uses the section as its scope.
- **Position.** The blocks go where the scope's first connected shape would have stood in the text flow.
- **Unchanged shapes.** A shape with no edge keeps its place and its output.
- **Container lists.** The Visio `Connections:` list, and the `vsdx.connections` function that writes it, are removed. The diagram replaces them.

### Node text

- **Node IDs.** `n1`, `n2`, … in reading order within the scope. Container IDs are `c1`, `c2`, ….
- **Short text.** A shape whose content is plain text of at most `NAME` (80) characters goes into its node whole. Each line break becomes `<br>`.
- **Long or rich content.** Any other shape still gets a node. Its label is the first line of its text, cut at 80 characters, plus `…`. A shape with no text shows a one-word label: `(picture)`, `(table)` or `(chart)`. The shape's own blocks then follow the Mermaid block in node order. Each set starts with a paragraph `nK:`, so nothing is lost.
- **Hidden and blank ends.** An edge end at a hidden shape, or at a shape with no text, gets a blank node `nK[" "]`. Hidden text stays under the existing hidden rules: it is left out and counted, and shown with `--hidden`. A hidden connector gives no edge; its text follows the same hidden rules in the flow.
- **Escapes.** Every label is quoted: `"` becomes `#quot;` and line breaks become `<br>`. Nothing else is escaped. Claude reads the source, so nothing needs to survive a renderer. The existing `Code` renderer already lengthens the fence past any backtick run.

### Shapes, edges and layout

- **Node shapes.** Only common shapes are mapped; every other shape is `nK["…"]`.

  | Mermaid | Meaning | Visio master `NameU` contains | DrawingML `prst` / ODF `draw:type` | ODRAW shape type |
  |---|---|---|---|---|
  | `{"…"}` | decision | `Decision` | `flowChartDecision`, `diamond` / `flowchart-decision`, `diamond` | 110, 4 |
  | `(["…"])` | start or end | `Start/End`, `Terminator` | `flowChartTerminator` / `flowchart-terminator` | 116 |
  | `(("…"))` | circle | `Circle`, `Ellipse` | `ellipse`, `flowChartConnector` / `ellipse`, `circle`, `flowchart-connector` | 3, 120 |
  | `[("…")]` | data store | `Database` | `can`, `flowChartMagneticDisk` / `can`, `flowchart-magnetic-disk` | 22, 132 |

  ODF `draw:ellipse` and `draw:circle` elements are circles. In hwp and hwpx only the ellipse object is mapped. The match is case-folded.
- **Edges.** Direction comes from the connector's arrowheads:

  | Arrowheads | Output |
  |---|---|
  | at the end only | `nA --> nB` |
  | at the start only | `nB --> nA` |
  | at both ends | `nA <--> nB` |
  | at neither end | `nA --- nB` |
  | not known (the format's cell or property is absent) | `nA --> nB`, as today's list reads begin → end |

  A connector with text gets the label form `nA -->|"label"| nB`. Self-loops and repeated edges are kept, since Mermaid allows both.
- **Direction.**
  - When the scope has geometry, it sums the absolute x and y moves between the centres of each edge's two ends.
  - The diagram is `flowchart LR` when the x sum is larger, otherwise `flowchart TD`.
  - Without geometry it is `TD`.
- **Containers.**
  - Only Visio containers and swimlanes become `subgraph cK ["title"] … end`. They are the shapes whose User row `msvStructureType` is `Container`.
  - A node belongs to the smallest container whose box holds its centre. Containers nest the same way, up to `limits.MAX_DEPTH`.
  - Ordinary groups in any format are not subgraphs.
- **Order.** The block is written in this order:
  1. `flowchart TD|LR`;
  2. the node lines, in ID order, inside their subgraphs;
  3. the glued edges, in connector reading order;
  4. if any edges were inferred, the line `%% inferred from position`, then those edges.

### Trees as lists

The scope is written as a nested list instead of Mermaid when all of these hold:

- every edge is directed and has no label;
- every node has at most one incoming edge;
- there is exactly one root, and every node is reachable from it;
- some node has two or more children;
- the depth is at most `smartart.MAX_LEVEL` (8);
- there are no containers.

The list is `ListItem` blocks with `-` markers; children follow their parent's reading order. Long and rich content follows the same `nK:` rule, with `nK` written in front of the list item's label. A wrong call here loses nothing: the list holds the same parent and child pairs. SmartArt already reads as a list and does not change.

## Loose-end inference

- **Candidates.** These connectors are candidates when at least one end is not glued:
  - every connector shape: DrawingML `cxnSp` (in docx, a `wsp` with `cNvCnPr`), ODF `draw:connector`, Visio 1-D shapes, ODRAW connector types 32–40, hwp/hwpx connect lines;
  - a plain line shape (DrawingML `prst="line"`, ODF `draw:line`, ODRAW type 20, hwp/hwpx line) only when it has an arrowhead. This keeps underlines and dividers out.
- **Snapping.** The reader gives each shape's box and each end point in the scope's absolute coordinates, with group transforms, flips and rotation applied.
  - **Tolerance.** `tol` is 2% (`INFER_TOL = 0.02`) of the diagonal of the box that holds every shape and line in the scope.
  - **Inside.** If the point lies inside one or more boxes, the box with the smallest area wins.
  - **Outside.** Otherwise the box with the smallest distance from the point wins, if that distance is at most `tol`.
  - **Ties.** Equal areas, or equal distances within 1e-9 of `tol`, leave the end unresolved.
  - **Excluded boxes.** Containers, connectors and lines are never targets.
- **Becoming an edge.**
  - A glued end keeps its glue. An inferred end must resolve to a shape different from the other end's.
  - If both ends resolve, the connector becomes an inferred edge, using the arrowhead rules above.
  - If they do not, the connector is no edge. Its text stays in the flow, and it counts one `connector_loose`.
- **Size.** A scope with more than `INFER_SHAPES` (2,000) shapes is not inferred. Its loose connectors count one `infer_skipped` for the scope.

## The shared module: `reader/diagram.py`

```
class Diagram:                       # one scope
    def __init__(self, ctx, budget: vsdx.ConnectionBudget): ...
    def shape(self, key, blocks: list, kind: str = "", box=None, hidden=False, container=False, title="") -> None
    def connector(self, key, begin=None, end=None, p0=None, p1=None, arrows=(None, None), label="",
                  line=False, hidden=False) -> None
    def place(self, slots: list) -> list
```

- **Keys.** Each is the reader's own shape ID.
- **`kind`.** One of `""`, `decision`, `terminator`, `circle` or `store`.
- **`box`.** `(x0, y0, x1, y1)` in scope units.
- **Ends.**
  - `begin` and `end` are the glued shape keys, or `None` when not glued.
  - `p0` and `p1` are the end points.
  - `arrows` is `(start, end)`: `True`, `False` or `None` (not known).
  - `line=True` marks a plain line, which is a candidate only with an arrowhead.
- **`place(slots)`.**
  - **Input.** It takes the scope's flow as the reader built it, as `(key or None, blocks)` pairs.
  - **Output.** It returns the flow's blocks, with the diagram placed and the connected shapes taken out.
  - **No connectors.** A scope with no connector returns its slots unchanged, so readers call it unconditionally.
- **Shared budget.** The document's `ConnectionBudget` (`CONNECTIONS`, 10,000 edges) moves here from `vsdx.py`. A cut still counts `connections_cut`.

## Per-format extraction

| Format | Glue | Arrowheads | Geometry | Notes |
|---|---|---|---|---|
| vsdx | `Connects` rows (`FromCell` BeginX/EndX → `ToSheet`), as today | `BeginArrow`, `EndArrow` cells, from the shape, then its master shape, then its line style chain; none found is "not known" | `PinX`, `PinY`, `Width`, `Height`, `LocPinX`, `LocPinY`, `Angle`; 1-D `BeginX…EndY`; sub-shapes through their parents | containers from the `User.msvStructureType` row |
| vsd | glue blocks in `readMisc`, as today | the line block's arrow fields, through master and style as in vsdx | XForm, which is kept today, plus width, height and the XForm1D block | containers when the User section is read; otherwise none (no error) |
| pptx, xlsx | `cxnSp/nvCxnSpPr/cNvCxnSpPr`: `a:stCxn@id`, `a:endCxn@id` against `cNvPr@id` | `spPr/a:ln`: `a:headEnd@type` (start) and `a:tailEnd@type` (end), any type but `none`; absent is no arrowhead | `a:xfrm` off/ext/rot/flipH/flipV through `grpSpPr` chOff/chExt (as `ooxml_slides` does today); a connector runs from the box's start corner after flips to the opposite corner | xlsx shape geometry is in EMU from the anchor |
| docx | `wps:wsp` with `wps:cNvCnPr`: `a:stCxn`, `a:endCxn`; shapes are the `wps:wsp` of the same `wpg:wgp` group or `wpc:wpc` canvas | as pptx | as pptx, inside the group or canvas | the scope is the group or canvas |
| odp, odt, ods | `draw:connector@draw:start-shape`, `@draw:end-shape` against a shape's `draw:id` or `xml:id` | the graphic style chain's `draw:marker-start`, `draw:marker-end` | `svg:x`, `svg:y`, `svg:width`, `svg:height`; connector and line ends `svg:x1…y2`; `draw:g` children are already absolute | `draw:transform` is ignored |
| ppt, doc, xls, pub | `OfficeArtFConnectorRule` (0xF012) in the drawing's `OfficeArtSolverContainer` (0xF005): `spidA` start, `spidB` end, `spidC` connector | FOPT `lineStartArrowhead` (0x01D0), `lineEndArrowhead` (0x01D1), non-zero is an arrowhead | the client or child anchor through group `FSPGR` rects, flips from the `FSP` flags | read in `officeart.py`, used by `ppt_shapes`, `doc_objects`, `xls_drawing` and `pub` |
| hwpx | `hp:connectLine`: `hp:startPt@subjectIDRef`, `hp:endPt@subjectIDRef` against a drawing object's ID | `@type`: `*_ONEWAY` end, `*_BOTH` both, `*_NOARROW` neither; otherwise the line shape's head and tail styles | object position and size with group offsets; `startPt`/`endPt` `x`, `y` | which ID attribute `subjectIDRef` names is probed on a sample in the plan |
| hwp | the connect-line control's subject IDs, if the record holds them | the line record's arrow fields | the shape component's offsets and size, composed through groups | probed on a sample; if the subject IDs cannot be confirmed, hwp connectors use inference only |

Node text gaps that the diagram depends on are closed for the shapes it uses:

- **doc.** A connected shape's text-box text is taken from the text-box story by its `lTxid`. It leaves the after-the-main-text position, so it is not written twice.
- **xls.** A shape's TXO text is read. Today all of it is dropped, so this also gives unconnected text boxes their text, as a paragraph after the sheet's table.
- **pub.** Shapes already carry their text.

## Local caps

| Cap | Value | Past it |
|---|---|---|
| `CONNECTIONS` (moved to `diagram.py`) | 10,000 edges a document | the rest are dropped, `connections_cut` once per cut scope |
| `INFER_SHAPES` | 2,000 shapes a scope | no inference in that scope, `infer_skipped` once |
| `INFER_TOL` | 0.02 of the scope diagonal | an end farther than this from every box stays loose |
| tree depth | `smartart.MAX_LEVEL` (8) | the scope is written as Mermaid |
| container nesting | `limits.MAX_DEPTH` | deeper containers are not subgraphs; their nodes go to the deepest kept one |

The `skipped` names and their note wording in `markdown.py` are:

- `connector_loose`: "connector not attached" / "connectors not attached";
- `infer_skipped`: "diagram too large to place loose connectors" / "diagrams too large to place loose connectors".

## Hostile input

- **Bad coordinates.** NaN, infinite or huge coordinates (more than 1e9 in absolute value) disable inference for that shape, and a zero-size scope disables it for the scope. Neither raises.
- **Bad glue.** A glue reference to a missing key, to the connector itself, or to another connector is "not glued".
- **Self-loops.** Two glued ends on one shape are kept as a self-loop. A self-loop is never inferred.
- **Cycles.** They are fine in Mermaid. Tree detection runs on bounded sets, so a cycle is simply "not a tree".
- **Inference cost.** Inference is O(ends × shapes) within `INFER_SHAPES`, so it stays bounded per scope.
- **Labels.** Label text is never interpreted; the escapes above are the whole treatment.

## Testing

- **`tests/test_reader_diagram.py`:**
  - the four arrowhead cases and "not known";
  - the label escapes;
  - the 80-character rule and `nK:` follow-ups;
  - the LR/TD sums;
  - subgraph nesting;
  - the tree rule, including the cases that must stay Mermaid: a chain, a labelled edge, two parents and two roots;
  - inference: inside, nested-smallest, nearest within `tol`, exactly at `tol`, just past it, a tie, the same shape at both ends;
  - the `INFER_SHAPES` skip;
  - NaN boxes;
  - `place` with no connector, which returns the slots unchanged.
- **Grammar check.** A test-only checker accepts exactly the subset written here: the header line, node lines, edge lines, subgraph and `end`, and `%%` comments. It runs on every Mermaid block the reader tests produce.
- **Format tests.** Each format's existing test file gains fixtures built in the test, with no real documents:
  - two shapes and one glued connector;
  - one connector with a loose end;
  - arrowheads at the start only.
  - The vsdx and vsd tests that assert `Connections:` are replaced.
- **Bench.** Re-run the vsd and vsdx rows and every OOXML, ODF, ppt, doc, xls, pub, hwp and hwpx document. The check is that every non-empty text line of the previous engine output is still found, after undoing `#quot;` and `<br>`. The addendum lists the documents that gained a diagram.

## Review focus

1. **Visio files whose connectors carry no arrow cells.** The "not known" default keeps `-->`, but a style chain read wrongly turns real arrows into `---`.
2. **Group transforms.** A wrong composition of chOff/chExt, ODRAW group rects or Visio parent pins moves every box. Inference then snaps to the wrong shapes, silently.
3. **Placement in flowing documents.** In docx, odt and hwp, the diagram replaces a group's place in the paragraph flow. A connector scoped to a section in hwp moves text that was spread over several paragraphs to one spot.
4. **Duplicate text after the move.** The doc text-box story and the xls TXO text must not appear both in a node and in their old position.
5. **Version bump.** The vsd and vsdx outputs change for existing users. The cache key holds the reader version, so old outputs are re-converted.

## Engine spec edits made with this amendment

| Line | Edit |
|---|---|
| 26 | Add decision 10 (2026-11-17): diagrams as specified in `2026-11-17-doc-reader-diagrams-design.md`; connected shapes become a Mermaid flowchart or a nested list, with loose ends placed by position. |
| 106 | Add the fenced `mermaid` `Code` block for diagrams, and the `nK:` follow-up paragraphs. |
| 151 | Add a row: diagrams, every format with drawings; shared part `diagram`. |
| 169 | Out of scope stays, but now reads: "OCR of scanned pages or images (specified next, in spec ③); rendering WMF/EMF (spec ②); diagram structure in pdf and xps, which have no connector data." |

## Out of scope

- Keynote connection lines.
- Diagrams in pdf, xps and djvu, which have no connector data. Recovering them from vector paths is not attempted.
- Rendering a diagram to an image. Spec ② adds page rendering for metafiles only.
- Geometry of shapes with no connector.
- Connector routing points and the side of the shape an end attaches to.
- Mermaid output for SmartArt, which stays a list.
- Layer membership as subgraphs.
- Non-Visio groups as subgraphs.
- odg, which the engine does not read.
