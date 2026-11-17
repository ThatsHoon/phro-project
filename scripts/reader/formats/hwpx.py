"""HWPX (Hancom Office, zip + XML) to the document model. Elements match by local name.

A section holding an `hp:connectLine` or `hp:line` reads its drawing objects as `hancom.Obj` for `hancom.diagrams`:
keyed by `instid`, else `id`; a connect line glued by `hp:startPt` and `hp:endPt` `@subjectIDRef` (`0` is not glue)
with its arrowheads from `@type`, a line by its line shape's head and tail styles; boxes in HWPUNIT from `hp:offset`
inside an `hp:container`, and at top level from `hp:pos` only when it is relative to the paper or page, sized by
`hp:sz`, else `hp:curSz`. The element and attribute names follow kordoc's `src/hwp5/to-hwpx/shapes.ts` (MIT).
"""
import fnmatch
import re

from .. import diagram, limits
from ..charts import chart_table
from ..containers.zipx import Package, attr, first, kids, local, parse_xml, resolve
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Image, ListItem, Math, NoteRef, Para, Table, Text
from .hancom import (
    AUTO_ALT, Frame, Obj, Scopes, State, comment_note, cut_command, diagrams, flat, heading_level, lone_equations,
    next_marker, place, plain, strip_marker, style_name_level, tex as _tex,
)

HPF = "Contents/content.hpf"
_MIME = "application/hwp+zip"
_CASE_NS = {"http://www.hancom.co.kr/hwpml/2016/paragraph", "http://www.hancom.co.kr/hwpml/2016/ooxmlchart"}
_BORDERS = ("leftBorder", "rightBorder", "topBorder", "bottomBorder")
_OBJECTS = frozenset(("rect", "ellipse", "arc", "polygon", "curve", "line", "connectLine", "container", "textart",
                      "pic"))
_EDGES = ("connectLine", "line")
_PAGE = ("PAPER", "PAGE")
_ARROWS = {"_NOARROW": (False, False), "_ONEWAY": (False, True), "_BOTH": (True, True)}  # connectLine@type endings


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _kid(el, name):
    return next((c for c in el if local(c.tag) == name), None)


def _unit(s):
    """A HWPUNIT attribute, or None; one written as an unsigned 32-bit number reads as the signed one it is."""
    v = _int(s, None)
    return v - 2 ** 32 if v is not None and 2 ** 31 <= v < 2 ** 32 else v


def _id(el, name):
    """An id attribute, or None when absent, blank or 0."""
    v = (attr(el, name) or "").strip() if el is not None else ""
    return v if v not in ("", "0") else None


def _origin(el, inner):
    """The object's top left in its parent's frame: hp:offset in a container, hp:pos at top level when relative to
    the paper or page; else None."""
    if inner:
        at = _kid(el, "offset")
        xy = (attr(at, "x"), attr(at, "y")) if at is not None else (None, None)
    else:
        pos = _kid(el, "pos")
        if pos is None or any((attr(pos, a) or "").upper() not in _PAGE for a in ("vertRelTo", "horzRelTo")):
            return None
        xy = attr(pos, "horzOffset"), attr(pos, "vertOffset")
    x, y = (_unit(v) for v in xy)
    return None if x is None or y is None else (x, y)


def _size(el):
    """(width, height) from hp:sz, else hp:curSz, where an axis written 0 is hp:orgSz's."""
    sz = _kid(el, "sz")
    if sz is not None:
        return _unit(attr(sz, "width")), _unit(attr(sz, "height"))
    cur, org = _kid(el, "curSz"), _kid(el, "orgSz")
    out = []
    for a in ("width", "height"):
        v = _unit(attr(cur, a)) if cur is not None else None
        out.append(v if v or org is None else _unit(attr(org, a)))
    return tuple(out)


def _point(origin, pt):
    """A connector end: its own box origin plus the point's x, y; None when either is unknown."""
    if origin is None or pt is None:
        return None
    dx, dy = _unit(attr(pt, "x")), _unit(attr(pt, "y"))
    return None if dx is None or dy is None else (origin[0] + dx, origin[1] + dy)


def _arrows(el) -> tuple:
    """(start, end) arrowheads: a connect line's from its @type, else the line shape's head and tail styles (NORMAL
    is none); None where neither says."""
    kind = (attr(el, "type") or "").upper() if local(el.tag) == "connectLine" else ""
    for end, arrows in _ARROWS.items():
        if kind.endswith(end):
            return arrows
    ls = _kid(el, "lineShape")
    styles = (attr(ls, "headStyle"), attr(ls, "tailStyle")) if ls is not None else (None, None)
    return tuple(None if v is None else v.strip().upper() not in ("", "NORMAL") for v in styles)


def _obj(el, inner, blocks, kids) -> Obj:
    """A drawing object with its geometry in its parent's frame (a container's, when inner)."""
    n = local(el.tag)
    origin, (w, h) = _origin(el, inner), _size(el)
    box = origin + (origin[0] + w, origin[1] + h) if origin and w is not None and h is not None else None
    edge = None
    if n in _EDGES:
        start, end = _kid(el, "startPt"), _kid(el, "endPt")
        glue = (_id(start, "subjectIDRef"), _id(end, "subjectIDRef")) if n == "connectLine" else (None, None)
        edge = glue + (_point(origin, start), _point(origin, end), _arrows(el), n == "line")
    return Obj(_id(el, "instid") or _id(el, "id"), blocks, kids, n == "container", "circle" if n == "ellipse" else "",
               box, edge)


class _Header:
    """Lookup tables from Contents/header.xml."""

    def __init__(self, root):
        self.para, self.style, self.numbering, self.plain, self.bold = {}, {}, {}, {}, set()
        if root is None:
            return
        for el in root.iter():
            name, eid = local(el.tag), attr(el, "id")
            if eid is None:
                continue
            if name == "paraPr":
                h = kids(el, "heading")
                self.para[eid] = ((attr(h[0], "type", "NONE").upper(), attr(h[0], "idRef", "0"),
                                   _int(attr(h[0], "level"))) if h else ("NONE", "0", 0))
            elif name == "style":
                self.style[eid] = (attr(el, "name", "").strip(), attr(el, "paraPrIDRef"))
            elif name == "numbering":
                self.numbering[eid] = {
                    _int(attr(ph, "level")): ((ph.text or "").strip(), (attr(ph, "numFormat") or "DIGIT").upper(),
                                              _int(attr(ph, "start"), 1))
                    for ph in kids(el, "paraHead")}
            elif name == "borderFill":
                types = [attr(b[0], "type", "") for n in _BORDERS if (b := kids(el, n))]
                self.plain[eid] = len(types) == 4 and all(t.upper() == "NONE" for t in types)
            elif name == "charPr" and kids(el, "bold"):
                self.bold.add(eid)


class _Reader(Scopes):
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.doc = Doc()
        self.hdr = _Header(None)
        self.images = {}
        self.counts = {"fn": 0, "en": 0, "cm": 0}
        self.numbers = {}
        self.cache = {}
        self.charts = {}  # a chart part's name -> (its table or None, the table's cells)
        self.opened = 0  # tables opened so far (TABLE_NODES)
        self.image_bytes = 0  # picture bytes placed so far, copies counted (IMAGE_BYTES)
        self.chart_cells = 0  # chart table cells placed so far, copies counted (CHART_TOTAL)
        self.margins = False  # walking a header or footer, whose pictures and charts are skipped
        self.budget = diagram.ConnectionBudget(ctx)  # the document's edges, shared by every section's diagrams
        self.collect = False  # reading drawing objects as hancom.Obj: in a section with a line, not in a sub-list
        self.grouped = False  # reading the objects a container holds

    # ---- package
    def find(self, href):
        for cand in (resolve("x", href or ""), resolve(HPF, href or "")):
            if href and self.pkg.has(cand):
                return cand
        return None

    def part_bytes(self, href, kind):
        name = self.find(href)
        if name is None:
            self.ctx.skipped[kind] += 1
            return None
        if name not in self.cache:
            try:
                self.cache[name] = self.pkg.read(name)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):  # an image or object over a limit is skipped
                    raise
                self.ctx.skipped[kind] += 1
                self.cache[name] = None
        return self.cache[name]

    def run(self):
        pkg = self.pkg
        mime = pkg.read("mimetype").decode("utf-8", "replace").strip() if pkg.has("mimetype") else ""
        if mime != _MIME and not pkg.has(HPF):
            raise ReadError("damaged", "not a hwpx package")
        if pkg.has("META-INF/manifest.xml") and b"encryption-data" in pkg.read("META-INF/manifest.xml").lower():
            raise ReadError("encrypted")
        items = []
        if pkg.has(HPF):
            root = pkg.xml(HPF)
            items = [(attr(i, "id"), attr(i, "href", "")) for i in root.iter() if local(i.tag) == "item"]
        self.images = {i: h for i, h in items if i}
        hrefs = [h for _, h in items if "section" in h.lower() and h.lower().endswith(".xml")]
        names = []
        for h in hrefs:
            n = self.find(h)
            if n is None:
                self.ctx.skipped["section"] += 1
            elif n not in names:
                names.append(n)
        if not hrefs:
            names = sorted((n for n in pkg.names if fnmatch.fnmatch(n.lower(), "contents/section*.xml")),
                           key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n.lower())])
        if pkg.has("Contents/header.xml"):
            try:
                self.hdr = _Header(pkg.xml("Contents/header.xml"))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["styles"] += 1  # header.xml holds the style and numbering tables
        good = 0
        for name in names:
            with self.ctx.guard("section"):
                try:
                    root = pkg.xml(name)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["section"] += 1
                    continue
                scoped = self.collect = any(local(e.tag) in _EDGES for e in root.iter())
                try:
                    blocks = self.blocks(root, 0, False)
                finally:
                    self.collect = False
                self.doc.blocks.extend(diagrams(self.ctx, self.budget, blocks) if scoped else blocks)
                good += 1
        if not good:
            raise ReadError("damaged", "no readable section")
        return self.doc

    # ---- blocks
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def pick(self, sw):
        for c in kids(sw, "case"):
            if attr(c, "required-namespace") in _CASE_NS:
                return c
        d = kids(sw, "default")
        return d[0] if d else None

    def blocks(self, container, d, nohead):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        for c in container:
            n = local(c.tag)
            if n == "p":
                out.extend(self.para(c, d, nohead))
            elif n == "switch":
                chosen = self.pick(c)
                if chosen is not None:
                    out.extend(self.blocks(chosen, d + 1, nohead))
        return out

    def sub(self, el, d, nohead=True):
        """A text box, note, comment, header or footer: drawing objects in it read as before, with no diagram."""
        out, outer, self.collect = [], self.collect, False
        try:
            for sl in kids(el, "subList"):
                out.extend(self.blocks(sl, d + 1, nohead))
        finally:
            self.collect = outer
        return out

    def para(self, p, d, nohead):
        st = State(nohead)
        for c in p:
            self.node(c, st, d + 1)
        self.close_to(st, 1)
        inl = st.frames[0].inl
        out = lone_equations(inl, st.eqs)
        text = plain(inl)
        htype, hid, hlvl = self.hdr.para.get(attr(p, "paraPrIDRef"), ("NONE", "0", 0))
        level = 0
        if not nohead:
            if htype == "OUTLINE":
                level = min(hlvl + 1, 6)
            else:
                level = self.style_level(attr(p, "styleIDRef"))
            level = heading_level(level, text)
        main = None
        if level and text:
            main = Heading(level, inl)
        elif htype in ("NUMBER", "BULLET"):
            marker = self.marker(hid, hlvl) if htype == "NUMBER" else "-"
            strip_marker(inl, marker)
            if inl:
                main = ListItem(min(hlvl, 6), marker, inl)
        elif inl:
            main = Para(inl)
        return ([main] if main else []) + out + st.extras

    def style_level(self, sid):
        return style_name_level(self.hdr.style.get(sid, ("", None))[0])

    def marker(self, nid, lvl):
        return next_marker(self.hdr.numbering.get(nid, {}), self.numbers.setdefault(nid, {}), lvl)

    # ---- inline scopes (add, push, close_to and close_field come from hancom.Scopes)
    def text(self, s, st):
        if s:
            t = Text(s)
            self.add(st, Bold([t]) if st.bold else t)

    def field_begin(self, el, st):
        kind = (attr(el, "type") or "").upper()
        fid = attr(el, "id")
        if kind == "HYPERLINK":
            params = {attr(p, "name"): (p.text or "") for p in el.iter() if local(p.tag) == "stringParam"}
            url = cut_command(params["Command"]) if params.get("Command") else (params.get("Path") or "").strip()
            self.push(st, Frame("link" if url else "pass", fid, url))
        elif kind == "CLICK_HERE":
            self.push(st, Frame("drop", fid))

    def delete_end(self, st):
        for i in range(len(st.frames) - 1, 0, -1):
            if st.frames[i].kind == "deleted":
                self.close_to(st, i)
                return

    # ---- paragraph content
    def node(self, el, st, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        n = local(el.tag)
        if n == "run":
            old, st.bold = st.bold, attr(el, "charPrIDRef") in self.hdr.bold
            for c in el:
                self.node(c, st, d + 1)
            st.bold = old
        elif n == "t":
            self.text(el.text, st)
            for c in el:
                self.node(c, st, d + 1)
                self.text(c.tail, st)
        elif n == "tab":
            self.text("\t", st)
        elif n == "lineBreak":
            self.add(st, Break())
        elif n in ("fwSpace", "nbSpace"):
            self.text(" ", st)
        elif n == "fieldBegin":
            self.field_begin(el, st)
        elif n == "fieldEnd":
            self.close_field(st, attr(el, "beginIDRef"))
        elif n == "deleteBegin":
            self.push(st, Frame("deleted"))
        elif n == "deleteEnd":
            self.delete_end(st)
        elif n == "switch":
            chosen = self.pick(el)
            for c in (chosen if chosen is not None else ()):
                self.node(c, st, d + 1)
        elif n == "tbl":
            st.extras.extend(self.table(el, d))
        elif n in _OBJECTS and self.collect:
            self.drawing(el, st, d)
        elif n == "pic":
            self.picture(el, st)
        elif n == "chart":
            self.chart(el, st)
        elif n == "equation":
            s = first(el, "script")
            tex = _tex(s.text or "") if s is not None else ""
            if tex:
                m = Math(tex)
                st.eqs.append(m)
                self.add(st, m)
        elif n in ("footNote", "endNote"):
            key = "fn" if n == "footNote" else "en"
            self.counts[key] += 1
            nid = f"{key}-{self.counts[key]}"
            self.doc.notes[nid] = self.sub(el, d)
            self.add(st, NoteRef(nid))
        elif n == "hiddenComment":
            self.counts["cm"] += 1
            nid = f"cm-{self.counts['cm']}"
            self.doc.notes[nid] = comment_note(self.sub(el, d))
            self.add(st, NoteRef(nid))
        elif n in ("header", "footer"):  # a line of text, as in doc and hwp: pictures and charts are skipped unread
            outer, self.margins = self.margins, True
            try:
                text = flat(self.sub(el, d))
            finally:
                self.margins = outer
            if text:
                (self.doc.headers if n == "header" else self.doc.footers).append(text)
        elif n == "drawText":
            st.extras.extend(self.sub(el, d, st.nohead))
        elif n != "secPr":
            for c in el:
                self.node(c, st, d + 1)

    def drawing(self, el, st, d):
        """A drawing object as one hancom.Obj among the paragraph's extras, holding the blocks it adds there (a
        picture's image, a text box's text); a container's objects become its kids, anything else's follow it."""
        n, inner = local(el.tag), self.grouped
        start = len(st.extras)
        if n == "pic":
            self.picture(el, st)
        else:
            self.grouped = n == "container"
            try:
                for c in el:
                    self.node(c, st, d + 1)
            finally:
                self.grouped = inner
        got = st.extras[start:]
        del st.extras[start:]
        objs = [b for b in got if isinstance(b, Obj)]
        group = n == "container"
        st.extras.append(_obj(el, inner, [b for b in got if not isinstance(b, Obj)], objs if group else ()))
        if not group:
            st.extras.extend(objs)

    def picture(self, pic, st):
        """Every placement is charged against IMAGE_BYTES, as in the Word readers; one past it is counted under image
        instead. In a header or footer the picture is skipped, neither read nor counted."""
        if self.margins:
            return
        img = first(pic, "img")
        data = self.part_bytes(self.images.get(attr(img, "binaryItemIDRef")), "image") if img is not None else None
        if not data:
            return
        self.image_bytes += len(data)
        if self.image_bytes > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        sc = first(pic, "shapeComment")
        alt = re.sub(r"\s+", " ", sc.text or "").strip() if sc is not None else ""
        st.extras.append(Image(data, "" if AUTO_ALT.match(alt) else alt))

    def chart(self, el, st):
        """A part two charts name is read once, but every placement is charged against CHART_TOTAL, as in the Word
        and slide readers; one past it is counted under chart instead."""
        if self.margins:  # in a header or footer: skipped, neither read nor counted
            return
        href = attr(el, "chartIDRef")
        name = self.find(href)
        if name not in self.charts:
            data = self.part_bytes(href, "chart")
            tbl = None
            if data:
                try:
                    tbl = chart_table(parse_xml(data), self.ctx)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["chart"] += 1
            self.charts[name] = tbl, sum(len(r) for r in tbl.rows) if tbl is not None else 0
        tbl, cells = self.charts[name]
        if tbl is None:
            return
        self.chart_cells += cells
        if self.chart_cells > limits.CHART_TOTAL:
            self.ctx.skipped["chart"] += 1
            return
        st.extras.append(tbl)

    # ---- tables
    def table(self, tbl, d):
        if self.opened >= limits.TABLE_NODES:  # left out as past MAX_DEPTH
            self.deep()
            return []
        self.opened += 1
        raw = []  # (row, col, rowspan, colspan, Cell)
        plain = True
        seen = {}
        for r, tr in enumerate(kids(tbl, "tr")):
            for tc in kids(tr, "tc"):
                addr = kids(tc, "cellAddr")
                span = kids(tc, "cellSpan")
                row = _int(attr(addr[0], "rowAddr"), r) if addr else r
                col = _int(attr(addr[0], "colAddr"), seen.get(row, 0)) if addr else seen.get(row, 0)
                seen[row] = seen.get(row, 0) + 1
                cs = max(1, _int(attr(span[0], "colSpan"), 1)) if span else 1
                rs = max(1, _int(attr(span[0], "rowSpan"), 1)) if span else 1
                blocks = []
                for sl in kids(tc, "subList"):
                    blocks.extend(self.blocks(sl, d + 1, True))
                raw.append((row, col, rs, cs, Cell(blocks, header=attr(tc, "header") == "1")))
                plain = plain and self.hdr.plain.get(attr(tc, "borderFillIDRef"), False)
        if not raw:
            return []
        nrows, ncols = len({x[0] for x in raw}), len({x[1] for x in raw})
        rows = place(raw)
        if plain and (nrows == 1 or ncols == 1):
            return [b for row in rows for c in row for b in c.blocks]
        return [Table(rows)]


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
