"""PresentationML (pptm, ppsx) to the document model. Elements match by local name.

Run hyperlinks and legacy slide comments are ported from dochan (`ooxml/pptx.py`); a14 math goes through `omml`.
Comments (legacy `p:cm`, modern `p188:cm` with replies) become the Section's `comments`, one note line each.
Layouts and masters are read only for the position of a placeholder that has none of its own.
Each slide is one diagram scope: every shape (titles, left-out placeholders and textless ones too) and connector
(`p:cxnSp`, or an `sp` whose preset is a line) goes to `diagram.Diagram` under its `cNvPr@id`, in slide EMU.
"""
import re

from .. import diagram, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Equation, Link, ListItem, Math, Para, Section, Table, Text, Image
from . import omml, ooxml_base, smartart
from .drawingml import Xf, arrows, glue, is_line, kind, rule
from .drawingml import kid as _kid, nv as _nv, xfrm as _xfrm
from .ooxml_base import int_of as _int
from .word_fields import MAX_URL, comment_note

IMAGE_BYTES = 256 * 2**20  # image bytes one deck may emit, copies counted
SPAN_CELLS = 500_000       # grid slots all spans of one table may cover; past it spans become 1
MAX_ALT, MAX_TITLE = 200, 200
MAX_COMMENTS = 10_000      # comments one deck may emit, replies counted; the rest are counted once as `comments_cut`
A14_M = "{http://schemas.microsoft.com/office/drawing/2010/main}m"

_TITLES = frozenset({"title", "ctrTitle"})
_PH_ALIAS = {"ctrTitle": ("title",), "subTitle": ("body",), "obj": ("body",)}  # inherited from these on a master
_SKIP_PH = frozenset({"sldNum", "dt", "ftr", "hdr", "sldImg"})
_SHAPES = frozenset({"sp", "pic", "graphicFrame", "cxnSp"})


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
        elif isinstance(it, (Bold, Link)):
            out += _flat(it.inlines)
        elif isinstance(it, Math):
            out += it.tex
        elif isinstance(it, Break):
            out += " "
    return out


class _Scope:
    def __init__(self, part, nsmap):
        self.part, self.nsmap = part, nsmap


class _Reader:
    def __init__(self, pkg, part, ctx):
        self.pkg, self.part, self.ctx = pkg, part, ctx
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0
        self.smartart_nodes = 0
        self.authors = {}  # comment author id -> name, legacy and modern merged
        self.comment_count = 0
        self.cut = False
        self.ph_maps = {}  # layout or master part -> {("idx", i) | ("type", t): (y, x)}
        self.inherit = []  # the current slide's layout and master maps, in lookup order
        self.budget = diagram.ConnectionBudget(ctx)  # the deck's edges, shared by every slide's diagram
        self.dia = None    # the current slide's diagram
        self.keys = set()  # the current slide's diagram keys given out so far

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
        self.load_authors(rels)
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
        self.dia, self.keys = diagram.Diagram(self.ctx, self.budget), set()
        if tree is not None:
            self.walk(tree, Xf(), 0, False, out, titles)
        slots = [(key, blocks) for _, key, blocks in _reading_order(out)]
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = self.dia.place(slots)
        sec.blocks.extend(flow)
        sec.title = re.sub(r"\s+", " ", " ".join(titles)).strip()[:MAX_TITLE]
        notes = self.notes_part(name)
        if notes:
            sec.notes = notes
        sec.comments = self.comments_part(name)
        return sec

    # ---- comments
    def load_authors(self, rels):
        """Fill `self.authors` from the presentation's commentAuthors (legacy) and authors (modern) parts."""
        for rel in rels.values():
            if rel.external or not (rel.type.endswith("/commentAuthors") or rel.type.endswith("/authors")):
                continue
            try:
                with self.ctx.guard("comments"):
                    if not self.pkg.has(rel.target):
                        self.ctx.skipped["comments"] += 1
                        continue
                    root, _ = ooxml_base.load(self.pkg, rel.target)
                    for a in root:
                        if local(a.tag) in ("cmAuthor", "author") and attr(a, "id") is not None:
                            self.authors[attr(a, "id")] = attr(a, "name") or ""
            except ReadError as e:  # a member of an open package: any kind but a timeout skips this part
                if e.kind == "timeout":
                    raise
                self.ctx.skipped["comments"] += 1

    @staticmethod
    def _line(text):
        return re.sub(r"\s+", " ", text).strip()

    def _body_text(self, tx):
        """The text of a txBody's paragraphs, joined into one line."""
        paras = []
        for p in tx.iter():
            if local(p.tag) == "p":
                paras.append("".join(t.text or "" for t in p.iter() if local(t.tag) == "t"))
        return self._line(" ".join(paras))

    def comments_part(self, name):
        """The comments of a slide as Para lines, in file order, a reply right after its comment."""
        out = []
        for rel in ooxml_base.rels(self.pkg, name, self.ctx).values():
            if rel.external or not rel.type.endswith("/comments"):
                continue
            if not self.pkg.has(rel.target):
                self.ctx.skipped["comments"] += 1
                continue
            try:
                with self.ctx.guard("comments"):
                    root, _ = ooxml_base.load(self.pkg, rel.target)
                    found = []
                    for cm in root:
                        if local(cm.tag) != "cm":
                            continue
                        if _kid(cm, "text") is not None:  # legacy
                            found.append((attr(cm, "authorId"), self._line(_kid(cm, "text").text or "")))
                        elif _kid(cm, "txBody") is not None:  # modern, then its replies
                            found.append((attr(cm, "authorId"), self._body_text(_kid(cm, "txBody"))))
                            for lst in kids(cm, "replyLst"):
                                for rp in kids(lst, "reply"):
                                    if _kid(rp, "txBody") is not None:
                                        found.append((attr(rp, "authorId"), self._body_text(_kid(rp, "txBody"))))
                    for aid, text in found:
                        if not text:
                            continue
                        if self.comment_count >= MAX_COMMENTS:
                            if not self.cut:
                                self.cut = True
                                self.ctx.skipped["comments_cut"] += 1
                            break
                        self.comment_count += 1
                        out.append(comment_note([Para([Text(text)])], self.authors.get(aid or "", ""))[0])
            except ReadError as e:  # a member of an open package: any kind but a timeout skips this part
                if e.kind == "timeout":
                    raise
                self.ctx.skipped["comments"] += 1
        return out

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
                pos = Xf().point(_xfrm(el)) if ph is not None else None
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
            except ReadError as e:  # a member of an open package: any kind but a timeout skips this part
                if e.kind == "timeout":
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
        key, x, sppr = self.key(cnv), _xfrm(el), _kid(el, "spPr")
        ptype = (attr(ph, "type") or "body") if ph is not None else None
        body = _kid(el, "txBody") if n in ("sp", "cxnSp") else None
        if ptype in _SKIP_PH:  # left out of the flow; a connector may still end on it
            text = self._body_text(body) if body is not None else ""
            self.dia.shape(key, [Para([Text(text)])] if text else [], kind(sppr), xf.box(x), hid)
            return
        geom = xf.point(x)
        if geom is None and ph is not None:
            geom = self.inherited(ph)
        if n in ("sp", "cxnSp"):
            blocks = self.text_blocks(body)
            if ptype in _TITLES and not hid:  # the slide's heading: its text names the node, it has no slot
                text = " ".join(_flat(getattr(b, "inlines", [])) for b in blocks)
                if text.strip():
                    titles.append(text)
                eqs = [b for b in blocks if isinstance(b, Equation)]
                self.dia.shape(key, [b for b in blocks if not isinstance(b, Equation)], kind(sppr), xf.box(x))
                if eqs:
                    out.append((geom, None, eqs))
                return
        elif n == "pic":
            blocks = self.picture(el, cnv)
        else:
            blocks = self.frame(el)
        if n == "cxnSp" or (n == "sp" and is_line(sppr)):
            p0, p1 = xf.ends(x) or (None, None)
            st, en = glue(el)
            self.dia.connector(key, st, en, p0, p1, arrows(sppr), line=n != "cxnSp" or rule(sppr, (st, en)),
                               hidden=hid, blocks=blocks)
        else:
            self.dia.shape(key, blocks, kind(sppr), xf.box(x), hid)
        if hid:
            for b in blocks:
                b.hidden = "hidden"
        if blocks:
            out.append((geom, key, blocks))

    def key(self, cnv):
        """A shape's diagram key: its cNvPr id, or a key no glue names when it has none or repeats one on the slide."""
        k = attr(cnv, "id") if cnv is not None else None
        if k is None or k in self.keys:
            k = ("unkeyed", len(self.keys))
        self.keys.add(k)
        return k

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
                if tbl is None or self.chart_cells > limits.CHART_TOTAL:
                    self.ctx.skipped["chart"] += 1
                    return []
                return [tbl]
            if n == "relIds":
                got = smartart.items(self.pkg, self.scope.part, attr(e, "dm"), self.ctx,
                                     limits.SMARTART_NODES - self.smartart_nodes)
                self.smartart_nodes += len(got)
                return got
        return []

    # ---- text
    def para_children(self, p, d=0):
        """The children of a paragraph; a chosen mc:AlternateContent branch's children stand in as its own."""
        for c in p:
            if local(c.tag) != "AlternateContent":
                yield c
            elif d >= limits.MAX_DEPTH:
                self.deep()
            else:
                chosen = ooxml_base.alternate(c, self.scope.nsmap)
                if chosen is None:
                    self.ctx.skipped["alternate"] += 1
                else:
                    yield from self.para_children(chosen, d + 1)

    def run_url(self, r):
        """The external URL of a run's hyperlink, or "" (text only: no link, an action, an internal target)."""
        pr = _kid(r, "rPr")
        h = _kid(pr, "hlinkClick") if pr is not None else None
        rid = _rid(h) if h is not None else None
        if rid is None or (attr(h, "action") or "").startswith("ppaction://"):
            return ""
        rel = ooxml_base.rels(self.pkg, self.scope.part, self.ctx).get(rid)
        if rel is None:
            self.ctx.skipped["link"] += 1
            return ""
        if rel.external and rel.type.endswith("/hyperlink"):
            return rel.target[:MAX_URL]
        return ""

    def inlines(self, p, eqs=None):
        out, link, url = [], None, ""
        for c in self.para_children(p):
            n = local(c.tag)
            if n == "br":
                out.append(Break())
                link = None
            elif n in ("r", "fld"):
                t = "".join(x.text or "" for x in kids(c, "t"))
                if not t:
                    continue
                pr = _kid(c, "rPr")
                bold = pr is not None and attr(pr, "b") in ("1", "true")
                it = Bold([Text(t)]) if bold else Text(t)
                u = self.run_url(c)
                if not u:
                    out.append(it)
                    link = None
                elif link is not None and u == url:
                    link.inlines.append(it)
                else:
                    link, url = Link(u, [it]), u
                    out.append(link)
            elif c.tag == A14_M:
                link = None
                for m in c:
                    if omml.is_math(m, "oMathPara"):
                        for t in omml.lines(m):
                            if eqs is not None:
                                eqs.append(Equation(t, display=True))
                            else:
                                out.append(Math(t))
                    elif omml.is_math(m, "oMath"):
                        t = omml.tex(m)
                        if t:
                            out.append(Math(t))
            elif omml.is_math(c, "oMath"):
                link = None
                t = omml.tex(c)
                if t:
                    out.append(Math(t))
        return out

    def text_blocks(self, body, cell=False):
        out, counters = [], {}
        if body is None:
            return out
        for p in kids(body, "p"):
            eqs = None if cell else []
            inl = self.inlines(p, eqs)
            if not _flat(inl).strip():
                out.extend(eqs or [])
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
            out.extend(eqs or [])
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
                row.append(Cell(self.text_blocks(_kid(tc, "txBody"), True), rspan, span))
            out.append(row)
        return [Table(out)] if out else []


def _reading_order(out):
    """(position, key, blocks) entries sorted by position. One without a position keeps its spTree place: it goes just
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
