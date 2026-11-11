"""Visio drawing (vsdx) to the document model: one section per page, shape text in reading order, picture shapes.

Elements match by local name. Masters are not read: a shape's text comes from the shape itself.
"""
import math

from .. import limits
from ..containers.zipx import Package, attr, kids, local
from ..limits import ReadError
from ..model import Break, Doc, Image, Para, Section, Text
from . import ooxml_base

IMAGE_BYTES = 256 * 2**20  # image bytes one drawing may emit, copies counted
MAX_TITLE = 200
MAX_TEXT = 20_000          # characters of text one shape may emit
COORD = 1e12


def _kid(el, name):
    k = kids(el, name)
    return k[0] if k else None


def _rid(el):
    for k, v in el.attrib.items():
        if "}" in k and local(k) == "id":
            return v
    return None


def _cells(shape) -> dict:
    """{N: V} of the shape's own Cell elements (first one wins)."""
    out = {}
    for c in kids(shape, "Cell"):
        n = attr(c, "N")
        if n in ("PinX", "PinY", "LocPinX", "LocPinY") and n not in out:
            out[n] = attr(c, "V")
    return out


def _num(v):
    """A finite float within bounds, else None (NaN, inf, junk all mean 'no value')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return max(-COORD, min(COORD, f))


def _text(shape) -> str:
    t = _kid(shape, "Text")
    if t is None:
        return ""
    parts = [t.text or ""]
    for c in t:  # cp, pp, tp, fld markers are dropped; what follows each stays, and a field's shown value
        if local(c.tag) == "fld":
            parts.append(c.text or "")
        parts.append(c.tail or "")
    return "".join(parts)[:MAX_TEXT]


def _inlines(text):
    out = []
    for i, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if i:
            out.append(Break())
        if line:
            out.append(Text(line))
    return out


class _Reader:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.part = ""
        self.image_bytes = 0

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    # ---- package
    def run(self) -> Doc:
        main = None
        for rel in ooxml_base.rels(self.pkg, "").values():
            if rel.type.endswith("/document") and not rel.external and self.pkg.has(rel.target):
                main = rel.target
                break
        if main is None:
            raise ReadError("damaged", "not a Visio package")
        pages = "visio/pages/pages.xml"
        for rel in ooxml_base.rels(self.pkg, main).values():
            if rel.type.endswith("/pages") and not rel.external:
                pages = rel.target
                break
        if not self.pkg.has(pages):
            raise ReadError("damaged", "not a Visio package")
        root, _ = ooxml_base.load(self.pkg, pages)
        rels = ooxml_base.rels(self.pkg, pages, self.ctx)
        entries = kids(root, "Page")
        doc, seen = Doc(), set()
        for number, entry in enumerate(entries, 1):
            rel = rels.get(_rid(_kid(entry, "Rel")) or "") if _kid(entry, "Rel") is not None else None
            if rel is None or rel.external or not rel.type.endswith("/page") or not self.pkg.has(rel.target):
                self.ctx.skipped["page"] += 1
                continue
            if rel.target in seen:
                continue
            seen.add(rel.target)
            name = (attr(entry, "Name") or attr(entry, "NameU") or str(number)).strip()[:MAX_TITLE]
            try:
                with self.ctx.guard("page"):
                    doc.blocks.append(self.page(rel.target, name))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["page"] += 1
        if entries and not doc.blocks:
            raise ReadError("damaged", "no readable page")
        return doc

    # ---- pages and shapes
    def page(self, part, name) -> Section:
        root, _ = ooxml_base.load(self.pkg, part)
        if local(root.tag) != "PageContents":
            raise ReadError("damaged", "not a page")
        self.part = part
        sec = Section("page", title=name)
        shapes = _kid(root, "Shapes")
        if shapes is not None:
            for _, blocks in self.order(self.walk(shapes, 0.0, 0.0, 0)):
                sec.blocks.extend(blocks)
        return sec

    @staticmethod
    def order(entries):
        placed = sorted((e for e in entries if e[0] is not None), key=lambda e: e[0])
        return placed + [e for e in entries if e[0] is None]

    def walk(self, container, dx, dy, d):
        """[(sort key or None, blocks)] for the Shape children of `container`; (dx, dy) is the group offset."""
        out = []
        if d > limits.MAX_DEPTH:
            self.deep()
            return out
        for el in kids(container, "Shape"):
            with self.ctx.guard("shape"):
                self.shape(el, dx, dy, d, out)
        return out

    def shape(self, el, dx, dy, d, out):
        cells = _cells(el)
        x, y = _num(cells.get("PinX")), _num(cells.get("PinY"))
        key = (-(y + dy), x + dx) if x is not None and y is not None else None
        blocks = []
        text = _text(el)
        if text.strip():
            blocks.append(Para(_inlines(text.strip("\n"))))
        blocks.extend(self.picture(el))
        sub = _kid(el, "Shapes")
        if sub is not None:
            lx, ly = _num(cells.get("LocPinX")), _num(cells.get("LocPinY"))
            ox = dx + x - lx if None not in (x, lx) else dx
            oy = dy + y - ly if None not in (y, ly) else dy
            for _, b in self.order(self.walk(sub, ox, oy, d + 1)):
                blocks.extend(b)
        if blocks:
            out.append((key, blocks))

    def picture(self, el):
        fd = _kid(el, "ForeignData")
        rel = _kid(fd, "Rel") if fd is not None else None
        if rel is None:
            return []
        img = ooxml_base.image(self.pkg, self.part, _rid(rel))
        if img is not None:
            self.image_bytes += len(img.data)
        if img is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return []
        return [Image(img.data)]


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
