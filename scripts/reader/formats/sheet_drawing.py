"""Charts and pictures anchored on a sheet, shared by the xlsb and xlsx readers.

The drawing anchors were checked against anydoc `sheet/xlsx.rs` (drawing anchors). A sheet's drawing part is XML: its
charts (cached values as a table, charged against `CHART_TOTAL`) and pictures (charged against `IMAGE_BYTES`) come in
anchor order. One `Drawings` serves all the sheets of a workbook, so the image byte count, the chart cell count and the
set of drawing parts already read are shared by them.
"""
from .. import limits
from ..containers.zipx import attr, local, first
from ..limits import ReadError
from . import ooxml_base

MAX_ANCHORS = 10_000  # drawing anchors one drawing part reads
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
        """Each anchor places every chart and picture it holds, group members included. Positioned anchors come in (row, column) order, then
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
            for el in self._descend(a, nsmap, ("chart", "pic")):
                if local(el.tag) == "chart":
                    self.chart(part, attr(el, "id"), out)
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
