"""PresentationML (pptm, ppsx) to the document model. Elements match by local name.

Layouts and masters are read only for the position of a placeholder that has none of its own.
"""
import re

from .. import limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, ListItem, Para, Section, Table, Text, Image
from . import ooxml_base
from .ooxml_base import int_of as _int

IMAGE_BYTES = 256 * 2**20  # image bytes one deck may emit, copies counted
CHART_CELLS = 200_000      # chart table cells one deck may emit, copies counted
SPAN_CELLS = 500_000       # grid slots all spans of one table may cover; past it spans become 1
MAX_ALT, MAX_TITLE = 200, 200
COORD = 10**13

_TITLES = frozenset({"title", "ctrTitle"})
_PH_ALIAS = {"ctrTitle": ("title",), "subTitle": ("body",), "obj": ("body",)}  # inherited from these on a master
_SKIP_PH = frozenset({"sldNum", "dt", "ftr", "hdr", "sldImg"})
_SHAPES = frozenset({"sp", "pic", "graphicFrame"})


def _num(el, name):
    v = attr(el, name) if el is not None else None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(-COORD, min(COORD, n))


def _kid(el, name):
    k = kids(el, name)
    return k[0] if k else None


def _rid(el):
    """The relationship id of an element: the namespaced `id` attribute (sldId also carries a plain numeric id)."""
    for k, v in el.attrib.items():
        if "}" in k and local(k) == "id":
            return v
    return None


def _flat(inlines) -> str:
    out = ""
    for it in inlines:
        if isinstance(it, Text):
            out += it.text
        elif isinstance(it, Bold):
            out += _flat(it.inlines)
        elif isinstance(it, Break):
            out += " "
    return out


def _nv(el):
    """(cNvPr, ph) of a shape, either may be None."""
    cnv = ph = None
    for c in el:
        if local(c.tag).startswith("nv"):
            cnv = cnv if cnv is not None else _kid(c, "cNvPr")
            for pr in kids(c, "nvPr"):
                ph = ph if ph is not None else _kid(pr, "ph")
    return cnv, ph


def _xfrm(el):
    for c in el:
        n = local(c.tag)
        if n == "xfrm":
            return c
        if n in ("spPr", "grpSpPr"):
            x = _kid(c, "xfrm")
            if x is not None:
                return x
    return None


class _Xf:
    """x' = ox + x * sx, y' = oy + y * sy: the map from a shape's own space to slide space."""

    def __init__(self, ox=0.0, sx=1.0, oy=0.0, sy=1.0):
        self.ox, self.sx, self.oy, self.sy = ox, sx, oy, sy

    @staticmethod
    def _scale(ext, ch):
        return ext / ch if ext is not None and ch is not None and ext >= 0 and ch > 0 else 1.0

    def group(self, xfrm):
        """The map for the children of a group whose xfrm is `xfrm`; identity scale when its numbers are unusable."""
        if xfrm is None:
            return self
        off, ext, cho, che = (_kid(xfrm, n) for n in ("off", "ext", "chOff", "chExt"))
        ox, oy, cx, cy = _num(off, "x"), _num(off, "y"), _num(cho, "x"), _num(cho, "y")
        if None in (ox, oy, cx, cy):
            return self
        sx = self._scale(_num(ext, "cx"), _num(che, "cx"))
        sy = self._scale(_num(ext, "cy"), _num(che, "cy"))
        return _Xf(max(-1e15, min(1e15, self.ox + (ox - cx * sx) * self.sx)), max(1e-9, min(1e9, sx * self.sx)),
                   max(-1e15, min(1e15, self.oy + (oy - cy * sy) * self.sy)), max(1e-9, min(1e9, sy * self.sy)))

    def point(self, xfrm):
        """(y, x) on the slide of a shape's offset, or None without a usable offset."""
        off = _kid(xfrm, "off") if xfrm is not None else None
        x, y = _num(off, "x"), _num(off, "y")
        if x is None or y is None:
            return None
        return (self.oy + y * self.sy, self.ox + x * self.sx)


class _Scope:
    def __init__(self, part, nsmap):
        self.part, self.nsmap = part, nsmap


class _Reader:
    def __init__(self, pkg, part, ctx):
        self.pkg, self.part, self.ctx = pkg, part, ctx
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0
        self.ph_maps = {}  # layout or master part -> {("idx", i) | ("type", t): (y, x)}
        self.inherit = []  # the current slide's layout and master maps, in lookup order

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def open(self, name):
        root, nsmap = ooxml_base.load(self.pkg, name)
        self.scope = _Scope(name, nsmap)
        return root

    # ---- deck
    def run(self) -> Doc:
        root = self.open(self.part)
        rels = ooxml_base.rels(self.pkg, self.part, self.ctx)
        lst = _kid(root, "sldIdLst")
        entries = kids(lst, "sldId") if lst is not None else []
        doc = Doc()
        seen = set()
        for number, entry in enumerate(entries, 1):
            rel = rels.get(_rid(entry) or "")
            if rel is None or rel.external or not rel.type.endswith("/slide") or not self.pkg.has(rel.target):
                self.ctx.skipped["slide"] += 1
                continue
            if rel.target in seen:
                continue
            seen.add(rel.target)
            try:
                with self.ctx.guard("slide"):
                    doc.blocks.append(self.slide(rel.target, number))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["slide"] += 1
        if entries and not doc.blocks:
            raise ReadError("damaged", "no readable slide")
        return doc

    def slide(self, name, number) -> Section:
        root = self.open(name)
        if local(root.tag) != "sld":
            raise ReadError("damaged", "not a slide")
        sec = Section("slide", number=number, hidden="hidden" if attr(root, "show") in ("0", "false") else "")
        tree = _kid(_kid(root, "cSld"), "spTree") if _kid(root, "cSld") is not None else None
        out, titles = [], []
        self.inherit = self.layout_maps(name)
        if tree is not None:
            self.walk(tree, _Xf(), 0, False, out, titles)
        for _, blocks in _reading_order(out):
            sec.blocks.extend(blocks)
        sec.title = re.sub(r"\s+", " ", " ".join(titles)).strip()[:MAX_TITLE]
        notes = self.notes_part(name)
        if notes:
            sec.notes = notes
        return sec

    # ---- inherited placeholder positions
    def related(self, part, suffix):
        for rel in ooxml_base.rels(self.pkg, part, self.ctx).values():
            if not rel.external and rel.type.endswith(suffix) and self.pkg.has(rel.target):
                return rel.target
        return None

    def layout_maps(self, slide):
        layout = self.related(slide, "/slideLayout")
        master = self.related(layout, "/slideMaster") if layout else None
        return [self.ph_map(p) for p in (layout, master) if p]

    def ph_map(self, part):
        """Positions of the placeholders a layout or master gives, by idx and by type; cached per part."""
        if part not in self.ph_maps:
            m = {}
            try:
                root, _ = ooxml_base.load(self.pkg, part)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                root = None
            csld = _kid(root, "cSld") if root is not None else None
            tree = _kid(csld, "spTree") if csld is not None else None
            for el in (tree if tree is not None else ()):
                _, ph = _nv(el)
                pos = _Xf().point(_xfrm(el)) if ph is not None else None
                if pos is None:
                    continue
                if attr(ph, "idx") is not None:
                    m.setdefault(("idx", attr(ph, "idx")), pos)
                m.setdefault(("type", attr(ph, "type") or "body"), pos)
            self.ph_maps[part] = m
        return self.ph_maps[part]

    def inherited(self, ph):
        """Slide position of a placeholder from its layout (by idx, then type), then its master; None if neither has it."""
        idx, kind = attr(ph, "idx"), attr(ph, "type") or "body"
        for m in self.inherit:
            if idx is not None and ("idx", idx) in m:
                return m[("idx", idx)]
            for t in (kind, *_PH_ALIAS.get(kind, ())):
                if ("type", t) in m:
                    return m[("type", t)]
        return None

    def notes_part(self, name):
        for rel in ooxml_base.rels(self.pkg, name, self.ctx).values():
            if rel.external or not rel.type.endswith("/notesSlide"):
                continue
            if not self.pkg.has(rel.target):
                self.ctx.skipped["notes"] += 1
                return []
            try:
                with self.ctx.guard("notes"):
                    root = self.open(rel.target)
                    tree = _kid(_kid(root, "cSld"), "spTree") if _kid(root, "cSld") is not None else None
                    out = []
                    for sp in (kids(tree, "sp") if tree is not None else []):
                        _, ph = _nv(sp)
                        if ph is not None and (attr(ph, "type") or "body") == "body":
                            out.extend(self.text_blocks(_kid(sp, "txBody")))
                    return out
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["notes"] += 1
            return []
        return []

    # ---- shapes
    def walk(self, container, xf, d, hidden, out, titles):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        for el in container:
            with self.ctx.guard("shape"):
                self.shape(el, xf, d, hidden, out, titles)

    def shape(self, el, xf, d, hidden, out, titles):
        n = local(el.tag)
        if n == "AlternateContent":
            chosen = ooxml_base.alternate(el, self.scope.nsmap)
            if chosen is None:
                self.ctx.skipped["alternate"] += 1
            else:
                self.walk(chosen, xf, d + 1, hidden, out, titles)
            return
        if n != "grpSp" and n not in _SHAPES:
            return
        cnv, ph = _nv(el)
        hid = hidden or (cnv is not None and attr(cnv, "hidden") in ("1", "true"))
        if n == "grpSp":
            self.walk(el, xf.group(_xfrm(el)), d + 1, hid, out, titles)
            return
        ptype = (attr(ph, "type") or "body") if ph is not None else None
        if ptype in _SKIP_PH:
            return
        geom = xf.point(_xfrm(el))
        if geom is None and ph is not None:
            geom = self.inherited(ph)
        if n == "sp":
            body = _kid(el, "txBody")
            blocks = self.text_blocks(body)
            if ptype in _TITLES and not hid:
                text = " ".join(_flat(b.inlines) for b in blocks)
                if text.strip():
                    titles.append(text)
                return
        elif n == "pic":
            blocks = self.picture(el, cnv)
        else:
            blocks = self.frame(el)
        if hid:
            for b in blocks:
                b.hidden = "hidden"
        if blocks:
            out.append((geom, blocks))

    def picture(self, el, cnv):
        blip = next((e for e in el.iter() if local(e.tag) == "blip"), None)
        if blip is None:
            return []
        img = ooxml_base.image(self.pkg, self.scope.part, attr(blip, "embed"))
        if img is not None:
            self.image_bytes += len(img.data)
        if img is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return []
        alt = re.sub(r"\s+", " ", (attr(cnv, "descr") if cnv is not None else "") or "").strip()[:MAX_ALT]
        return [Image(img.data, alt)]

    def frame(self, el):
        for e in el.iter():
            n = local(e.tag)
            if n == "tbl":
                return self.table(e)
            if n == "chart":
                tbl = ooxml_base.chart(self.pkg, self.scope.part, attr(e, "id"), self.ctx)
                if tbl is not None:
                    self.chart_cells += sum(len(r) for r in tbl.rows)
                if tbl is None or self.chart_cells > CHART_CELLS:
                    self.ctx.skipped["chart"] += 1
                    return []
                return [tbl]
        return []

    # ---- text
    @staticmethod
    def inlines(p):
        out = []
        for c in p:
            n = local(c.tag)
            if n == "br":
                out.append(Break())
            elif n in ("r", "fld"):
                t = "".join(x.text or "" for x in kids(c, "t"))
                if not t:
                    continue
                pr = _kid(c, "rPr")
                bold = pr is not None and attr(pr, "b") in ("1", "true")
                out.append(Bold([Text(t)]) if bold else Text(t))
        return out

    def text_blocks(self, body):
        out, counters = [], {}
        if body is None:
            return out
        for p in kids(body, "p"):
            inl = self.inlines(p)
            if not _flat(inl).strip():
                continue
            ppr = _kid(p, "pPr")
            lvl = min(max(_int(attr(ppr, "lvl")), 0), 8) if ppr is not None else 0
            names = {local(c.tag): c for c in ppr} if ppr is not None else {}
            marker = None
            if "buNone" not in names:
                if "buAutoNum" in names:
                    start = min(max(_int(attr(names["buAutoNum"], "startAt"), 1), 1), 10**6)
                    counters[lvl] = counters.get(lvl, start - 1) + 1
                    marker = f"{counters[lvl]}."
                elif "buChar" in names:
                    marker = "-"
            numbered = marker is not None and marker != "-"
            for k in [k for k in counters if k > lvl or (k == lvl and not numbered)]:
                del counters[k]
            out.append(ListItem(lvl, marker, inl) if marker else Para(inl))
        return out

    # ---- tables
    def table(self, tbl):
        rows = kids(tbl, "tr")
        grid = _kid(tbl, "tblGrid")
        width = min(max(len(kids(grid, "gridCol")) if grid is not None else 0,
                        max((len(kids(r, "tc")) for r in rows), default=0), 1), limits.GRID_COLS)
        out, budget = [], SPAN_CELLS
        for ri, tr in enumerate(rows):
            row = []
            for col, tc in enumerate(kids(tr, "tc")):
                span = max(_int(attr(tc, "gridSpan"), 1), 1)
                rspan = max(_int(attr(tc, "rowSpan"), 1), 1)
                covered = attr(tc, "hMerge") in ("1", "true") or attr(tc, "vMerge") in ("1", "true")
                span = min(span, max(width - col, 1))
                rspan = min(rspan, len(rows) - ri)
                if span * rspan > budget:
                    span = rspan = 1
                if covered:
                    continue
                budget -= span * rspan
                row.append(Cell(self.text_blocks(_kid(tc, "txBody")), rspan, span))
            out.append(row)
        return [Table(out)] if out else []


def _reading_order(out):
    """(position, blocks) entries sorted by position. One without a position keeps its spTree place: it goes just
    before the highest shape that follows it, or last when none with a position follows."""
    keys, low = [], None
    for i in range(len(out) - 1, -1, -1):
        pos = out[i][0]
        if pos is not None:
            low = pos if low is None or pos < low else low
        at = pos if pos is not None else low
        keys.append(((0, at) if at is not None else (1,), i))
    order = sorted(keys)
    return [out[i] for _, i in order]


def parse_slides(pkg, part, ctx) -> Doc:
    return _Reader(pkg, part, ctx).run()
