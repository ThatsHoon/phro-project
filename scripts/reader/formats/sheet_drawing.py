"""Charts, pictures, SmartArt and shape text anchored on a sheet, shared by the xlsb and xlsx readers.

The drawing anchors were checked against anydoc `sheet/xlsx.rs` (drawing anchors). A sheet's drawing part is XML: its
charts (cached values as a table, charged against `CHART_TOTAL`), pictures (charged against `IMAGE_BYTES`) and SmartArt
text (a nested list, charged against `SMARTART_NODES`) and the text of shapes and text boxes (a paragraph each, charged
against `SHAPE_CHARS`) come in anchor order. One `Drawings` serves all the sheets of a workbook, so the image byte count,
the chart cell count, the SmartArt item count, the shape text count and the set of drawing parts already read are shared
by them.
"""
import re

from .. import limits
from ..containers.zipx import attr, kids, local, first
from ..limits import ReadError
from ..model import Para, Text
from . import ooxml_base, smartart

MAX_ANCHORS = 10_000  # drawing anchors one drawing part reads
SHAPE_CHARS = 1_000_000  # characters of shape text one workbook's drawings emit; the rest is counted once as a shape
_ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")


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
        absolute ones in file order. An AlternateContent is replaced by the branch the reader understands."""
        found = []
        for a in self._descend(root, nsmap, _ANCHORS):
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
        out = []
        for *_, a in found:
            for el in self._descend(a, nsmap, ("chart", "pic", "relIds", "sp")):
                if local(el.tag) == "sp":
                    self.shape(el, out)
                elif local(el.tag) == "chart":
                    self.chart(part, attr(el, "id"), out)
                elif local(el.tag) == "relIds":
                    got = smartart.items(self.pkg, part, attr(el, "dm"), self.ctx,
                                         limits.SMARTART_NODES - self.smartart_nodes)
                    self.smartart_nodes += len(got)
                    out += got
                else:
                    blip = first(el, "blip")
                    self.picture(part, attr(blip, "embed") if blip is not None else None, out)
        return out

    def _descend(self, root, nsmap, names):
        """The elements under `root` named in `names`, in document order, not entered once found; an
        AlternateContent is replaced by its chosen branch (counted under alternate when there is none)."""
        stack = [iter(root)]
        while stack:
            for el in stack[-1]:
                name = local(el.tag)
                if name in names:
                    yield el
                elif name == "AlternateContent":
                    chosen = ooxml_base.alternate(el, nsmap)
                    if chosen is None:
                        self.ctx.skipped["alternate"] += 1
                    else:
                        stack.append(iter(chosen))
                        break
                else:
                    stack.append(iter(el))
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
        tbl = ooxml_base.chart(self.pkg, part, rid, self.ctx)
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
