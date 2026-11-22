"""Charts, pictures, SmartArt and shape text anchored on a sheet, shared by the xlsb and xlsx readers.

The drawing anchors were checked against anydoc `sheet/xlsx.rs` (drawing anchors). A sheet's drawing part is XML: its
charts (cached values as a table, charged against `CHART_TOTAL`), pictures (charged against `IMAGE_BYTES`) and SmartArt
text (a nested list, charged against `SMARTART_NODES`) and the text of shapes and text boxes (a paragraph each, charged
against `SHAPE_CHARS`) come in anchor order. One `Drawings` serves all the sheets of a workbook, so the image byte count,
the chart cell count, the SmartArt item count, the shape text count and the set of drawing parts already read are shared
by them.

Each drawing part is one diagram scope: its shapes, pictures and chart or SmartArt frames go to `diagram.Diagram` under
their `cNvPr@id`, and its connectors (`xdr:cxnSp`, or an `sp` whose preset is a line) with their glue and arrowheads.
Geometry is the `a:xfrm` through every enclosing group's chOff/chExt, else a oneCellAnchor's or absoluteAnchor's
`xdr:pos`/`xdr:ext`; it is handed over in points. A part with no connector reads as before.
"""
import re

from .. import diagram, limits
from ..containers.zipx import attr, kids, local, first
from ..limits import ReadError
from ..model import Para, Text
from . import ooxml_base, smartart
from .drawingml import Xf, arrows, glue, is_line, kid, kind, num, nv, rule, xfrm

MAX_ANCHORS = 10_000  # drawing anchors one drawing part reads
SHAPE_CHARS = 1_000_000  # characters of shape text one workbook's drawings emit; the rest is counted once as a shape
_ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")
EMU_PT = 12_700  # EMU a point: geometry goes to the diagram in points, which keeps deep anchors within its bound


def _points(v):
    """EMU numbers as points, or None."""
    return None if v is None else tuple(n / EMU_PT for n in v)


def _spot(a):
    """(x0, y0, x1, y1) in EMU of a oneCellAnchor's or absoluteAnchor's xdr:pos and xdr:ext; None for a
    twoCellAnchor or without both."""
    if local(a.tag) == "twoCellAnchor":
        return None
    pos, ext = kid(a, "pos"), kid(a, "ext")
    x, y, cx, cy = num(pos, "x"), num(pos, "y"), num(ext, "cx"), num(ext, "cy")
    if None in (x, y, cx, cy):
        return None
    return x, y, x + max(cx, 0), y + max(cy, 0)


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


class Drawings:
    """The drawings of one workbook, sharing one image byte count and one chart cell count."""

    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.seen: set[str] = set()  # drawing parts read
        self.image_bytes = 0         # image bytes placed (IMAGE_BYTES)
        self.chart_cells = 0         # chart table cells placed (CHART_TOTAL)
        self.smartart_nodes = 0      # SmartArt items placed (SMARTART_NODES)
        self.shape_chars = 0         # shape text characters placed (SHAPE_CHARS)
        self.shape_cut = False       # shape text was dropped past SHAPE_CHARS (counted once)
        self.budget = diagram.ConnectionBudget(ctx)  # the workbook's edges, shared by every drawing part's diagram

    def blocks(self, part: str, rels: dict | None = None) -> list:
        """The charts and pictures of the drawing parts that the sheet relationships name, in anchor order. `rels` is
        the sheet's relationships when the caller has read them already."""
        out = []
        for rel in (ooxml_base.rels(self.pkg, part, self.ctx) if rels is None else rels).values():
            if rel.external or not rel.type.endswith("/drawing"):
                continue
            if rel.target in self.seen:
                self.ctx.skipped["object"] += 1
                continue
            self.seen.add(rel.target)
            try:
                root, nsmap = ooxml_base.load(self.pkg, rel.target)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.ctx.skipped["object"] += 1
                continue
            out += self.anchors(rel.target, root, nsmap)
        return out

    def anchors(self, part: str, root, nsmap) -> list:
        """Each anchor places every chart, picture, SmartArt and shape text it holds, group members included. Positioned anchors come in (row, column) order, then
        absolute ones in file order. An AlternateContent is replaced by the branch the reader understands. The part
        is one diagram scope: its shapes and connectors go to `diagram.Diagram`, which writes the joined ones as one
        block."""
        found = []
        for a, *_ in self._descend(root, nsmap, _ANCHORS):
            if len(found) >= MAX_ANCHORS:
                self.ctx.skipped["object"] += 1
                break
            at = first(a, "from")
            if at is None:
                found.append((1, 0, 0, len(found), a))
            else:
                r = ooxml_base.int_of(_text(first(at, "row")))
                c = ooxml_base.int_of(_text(first(at, "col")))
                found.append((0, r, c, len(found), a))
        found.sort(key=lambda t: t[:4])
        dia, keys, slots = diagram.Diagram(self.ctx, self.budget), set(), []
        for *_, a in found:
            spot = _spot(a)
            for el, xf, frame, depth in self._descend(a, nsmap, ("chart", "pic", "relIds", "sp", "cxnSp"), Xf()):
                name, out = local(el.tag), []
                if name == "sp":
                    self.shape(el, out)
                elif name == "chart":
                    self.chart(part, attr(el, "id"), out)
                elif name == "relIds":
                    got = smartart.items(self.pkg, part, attr(el, "dm"), self.ctx,
                                         limits.SMARTART_NODES - self.smartart_nodes)
                    self.smartart_nodes += len(got)
                    out += got
                elif name == "pic":
                    blip = first(el, "blip")
                    self.picture(part, attr(blip, "embed") if blip is not None else None, out)
                owner = el if name in ("sp", "pic", "cxnSp") else frame
                cnv = nv(owner)[0] if owner is not None else None
                key = attr(cnv, "id") if cnv is not None else None
                if key is None or key in keys:
                    key = ("unkeyed", len(keys))
                keys.add(key)
                slots.append((key, out))
                if owner is not None:
                    with self.ctx.guard("shape"):
                        self.register(dia, key, owner, cnv, out, xf, spot if depth == 0 else None)
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = dia.place(slots)
        return flow

    @staticmethod
    def register(dia, key, el, cnv, blocks, xf, spot) -> None:
        """One shape or connector of the scope, its geometry in points: a connector is an `xdr:cxnSp` or a shape whose
        preset is a line. A shape without an xfrm takes `spot`, its anchor's pos/ext when it sits right in one."""
        x, sppr = xfrm(el), kid(el, "spPr")
        hidden = cnv is not None and attr(cnv, "hidden") in ("1", "true")
        if local(el.tag) == "cxnSp" or (local(el.tag) == "sp" and is_line(sppr)):
            ends = xf.ends(x) if xf is not None and x is not None else None
            p0, p1 = (_points(e) for e in ends) if ends else (None, None)
            st, en = glue(el)
            dia.connector(key, st, en, p0, p1, arrows(sppr), line=local(el.tag) == "sp" or rule(sppr, (st, en)),
                          hidden=hidden, blocks=blocks)
        else:
            box = (xf.box(x) if xf is not None else None) if x is not None else spot
            dia.shape(key, blocks, kind(sppr), _points(box), hidden)

    def _descend(self, root, nsmap, names, xf=None):
        """(element, map, frame, depth) for the elements under `root` named in `names`, in document order, not
        entered once found; an AlternateContent is replaced by its chosen branch (counted under alternate when there is
        none). `map` is `xf` through the enclosing groups' transforms (None past limits.MAX_DEPTH groups, or when `xf`
        is), `frame` the enclosing graphicFrame or None, `depth` the number of enclosing groups."""
        stack = [(iter(root), xf, 0, None)]
        while stack:
            it, at, depth, frame = stack[-1]
            for el in it:
                name = local(el.tag)
                if name in names:
                    yield el, at, frame, depth
                elif name == "AlternateContent":
                    chosen = ooxml_base.alternate(el, nsmap)
                    if chosen is None:
                        self.ctx.skipped["alternate"] += 1
                    else:
                        stack.append((iter(chosen), at, depth, frame))
                        break
                elif name == "grpSp":
                    inner = at.group(xfrm(el)) if at is not None and depth < limits.MAX_DEPTH else None
                    stack.append((iter(el), inner, depth + 1, frame))
                    break
                else:
                    stack.append((iter(el), at, depth, el if name == "graphicFrame" else frame))
                    break
            else:
                stack.pop()

    def shape(self, sp, out: list) -> None:
        """A shape's text body as one Para per paragraph, its runs and fields joined as the slide reader joins a text
        body; hidden when the shape's cNvPr is."""
        cnv = first(sp, "cNvPr")
        hidden = "hidden" if cnv is not None and attr(cnv, "hidden") in ("1", "true") else ""
        for tx in kids(sp, "txBody"):
            for p in kids(tx, "p"):
                text = re.sub(r"\s+", " ", "".join(t.text or "" for t in p.iter() if local(t.tag) == "t")).strip()
                if not text:
                    continue
                self.shape_chars += len(text)
                if self.shape_chars > SHAPE_CHARS:
                    if not self.shape_cut:
                        self.shape_cut = True
                        self.ctx.skipped["shape"] += 1
                    return
                out.append(Para([Text(text)], hidden=hidden))

    def chart(self, part: str, rid, out: list) -> None:
        tbl = ooxml_base.chart(self.pkg, part, rid, self.ctx, workbook=True)
        if tbl is None:  # kept: a chart part with no series is "could not read" too, as in the slide and Word readers
            self.ctx.skipped["chart"] += 1
            return
        self.chart_cells += sum(len(r) for r in tbl.rows)
        if self.chart_cells > limits.CHART_TOTAL:
            self.ctx.skipped["chart"] += 1
        else:
            out.append(tbl)

    def picture(self, part: str, rid, out: list) -> None:
        img = ooxml_base.image(self.pkg, part, rid)
        if img is None:
            self.ctx.skipped["image"] += 1
            return
        self.image_bytes += len(img.data)
        if self.image_bytes > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
        else:
            out.append(img)
