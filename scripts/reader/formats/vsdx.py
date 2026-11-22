"""Visio drawing (vsdx, vsdm) to the document model: one section per page, shape text in reading order and picture
shapes, the page's connectors written as one `diagram` block.

Elements match by local name. A shape without a Text element takes its master's shape text (`visio/masters`, each
master part loaded once, up to MASTERS parts); a HideText cell, the shape's own else its master shape's, makes the text
hidden. Shape text, own and each inherited copy, is charged to one MAX_OUTPUT room by its characters and LINE per
line break. A vbaProject part's modules are read by containers.vba.

Each page is one `diagram.Diagram` scope: every shape is registered under its ID (a missing or repeated one gets a key
no glue names), textless ones too, and its slot in the flow carries that key. A shape's cells are its own, and for the
cells in INHERITED its master shape's when it has none (each master shape's cells read once).

- **Geometry**, in page units: the box is (PinX - LocPinX, PinY - LocPinY) and (Width, Height), mirrored about the pin
  by FlipX and FlipY, as the bound of the box turned by Angle about the pin, offset by the parent group's origin (its
  Pin less its LocPin, added up the groups). A missing LocPin term counts as 0; no pin or no size is no box.
- **Connectors**: a shape with BeginX and EndX cells, or one a Connects row names by BeginX or EndX. Its ends are glued
  by the page's Connects rows (FromCell BeginX / EndX to ToSheet, the first row of each end wins) and lie at (BeginX,
  BeginY) and (EndX, EndY) plus its parent's origin. Its shown text is the edge label; hidden text is none and stays in
  the flow, hidden.
- **Arrowheads**: BeginArrow and EndArrow, each from the shape's own cell, else its master shape's, else the LineStyle
  chain (the shape's LineStyle, else its master shape's) of the `visio/document.xml` StyleSheets, loaded once and
  walked through at most `limits.MAX_DEPTH` styles with a seen-set. A value above 0 is an arrowhead, any other number
  none, no value not known.
- **Kinds** from the NameU masters.xml gives the shape's own Master, else the shape's NameU, case-folded.
- **Containers**: a User row msvStructureType whose Value is Container, the shape's own else its master shape's; its
  shown text is the subgraph title.
- **Groups**: a sub-shape is registered with its group's key as its parent, as a part of it when it names a
  MasterShape and no Master of its own, and as no part when it names its own Master (with neither, no verdict); a
  shape with its own Master is named by that master's NameU less a trailing
  `.<digits>` copy number (what stands for it with no text).
- **Routes**: a connector's Geometry rows (merged as Shape Data is) of the vertex types, in IX order, Rel rows scaled
  by Width and Height, placed as its box is placed, are its route.
- **Shape Data and hyperlinks**: the Property rows (the master shape's in its order, each cell the shape gives with a
  value overriding, then the shape's own new rows; a Del row is gone) with a Value are `label: value` lines (Label,
  else the row name), and the Hyperlink rows `Link: description address#subaddress`; a "No Formula" cell holds no
  value and an Invisible row is not read. The lines follow the shape's text and picture in its slot and are its
  `data` in the diagram, never its label; each is charged as shape text. A master shape's rows are read once; the
  rows a shape takes and its own count against `limits.SHAPE_ROWS` a document, past which the shape's rows are not
  read (counted `shape_data` once for it), and a connector whose Geometry rows are not read is routed by its
  chord (counted `route`).
- **Background pages**: a page whose BackPage names a Background page's ID begins with `Background page: <name>`; a
  background page begins with `Background of: <names>`, or `Background page` when none names it.
"""
import math
import re

from .. import diagram, limits
from ..containers.zipx import Package, attr, kids, local
from ..limits import ReadError
from ..model import Break, Doc, Image, Para, Section, Text
from . import ooxml_base

IMAGE_BYTES = 256 * 2**20  # image bytes one drawing may emit, copies counted
MAX_TITLE = 200
MAX_TEXT = 20_000          # characters of text one shape may emit
COORD = 1e12
MASTERS = 1_000            # master parts one vsdx may load; inherited text from the rest is counted `stencil`
# characters one line break of shape text is charged against the MAX_OUTPUT room, besides its own: a line is a Text
# and a Break in the model however short, so "a\n" text would otherwise make two model objects per two characters
LINE = 64
CELLS = ("PinX", "PinY", "LocPinX", "LocPinY", "HideText", "Width", "Height", "Angle", "FlipX", "FlipY", "BeginX",
         "BeginY", "EndX", "EndY", "BeginArrow", "EndArrow")
# cells a shape takes from its master shape when it has none (a master's pin is on the master's own page)
INHERITED = ("LocPinX", "LocPinY", "Width", "Height", "Angle", "FlipX", "FlipY", "BeginX", "BeginY", "EndX", "EndY",
             "BeginArrow", "EndArrow")
ARROWS = ("BeginArrow", "EndArrow")
_SUFFIX = re.compile(r"\.\d+$")
_VERTICES = frozenset(("MoveTo", "LineTo", "ArcTo", "EllipticalArcTo", "PolylineTo", "NURBSTo", "SplineStart",
                       "SplineKnot", "RelMoveTo", "RelLineTo", "RelCubBezTo", "RelQuadBezTo", "RelEllipticalArcTo"))
_KINDS = (("decision", "decision"), ("start/end", "terminator"), ("terminator", "terminator"), ("circle", "circle"),
          ("ellipse", "circle"), ("database", "store"), ("callout", "callout"), ("chevron", "chevron"))


def _kid(el, name):
    k = kids(el, name)
    return k[0] if k else None


def _rid(el):
    for k, v in el.attrib.items():
        if "}" in k and local(k) == "id":
            return v
    return None


def _cells(shape) -> dict:
    """{N: V} of the shape's own Cell elements named in CELLS (first one wins)."""
    out = {}
    for c in kids(shape, "Cell"):
        n = attr(c, "N")
        if n in CELLS and n not in out:
            out[n] = attr(c, "V")
    return out


def _structure(shape):
    """The Value of the shape's own User row msvStructureType (first one wins), or None when it has none."""
    for sec in kids(shape, "Section"):
        if attr(sec, "N") != "User":
            continue
        for row in kids(sec, "Row"):
            if attr(row, "N") != "msvStructureType":
                continue
            for c in kids(row, "Cell"):
                if attr(c, "N") == "Value" and attr(c, "V") is not None:
                    return attr(c, "V")
    return None


def _rows(shape, name) -> list:
    """[(row N or IX, {cell N: V, "T": row type}, deleted)] of the shape's first `name` section; a cell whose
    formula is "No Formula" holds no value ("")."""
    out = []
    for sec in kids(shape, "Section"):
        if attr(sec, "N") != name:
            continue
        for row in kids(sec, "Row"):
            cells = {"T": attr(row, "T")}
            for c in kids(row, "Cell"):
                n = attr(c, "N")
                if n is not None and n not in cells:
                    cells[n] = "" if attr(c, "F") == "No Formula" else attr(c, "V")
            out.append((attr(row, "N") or attr(row, "IX"), cells, _on(attr(row, "Del"))))
        break
    return out


def _shown(name, cells) -> bool:
    if name == "Geometry":
        return True
    if _on(cells.get("Invisible")):
        return False
    if name == "Property":
        return bool((cells.get("Value") or "").strip())
    return bool((cells.get("Address") or "").strip() or (cells.get("SubAddress") or "").strip())


def _data(props, links) -> list:
    out = []
    for key, c in props:
        if not _shown("Property", c):
            continue
        value = " ".join(c["Value"].split())
        label = " ".join((c.get("Label") or "").split()) or (key if isinstance(key, str) else "")
        out.append(f"{label}: {value}" if label else value)
    for _, c in links:
        if not _shown("Hyperlink", c):
            continue
        address, sub = (c.get("Address") or "").strip(), (c.get("SubAddress") or "").strip()
        target = address + ("#" + sub if sub else "")
        desc = " ".join((c.get("Description") or "").split())
        out.append(f"Link: {desc} {target}" if desc else f"Link: {target}")
    return out


def _master_name(name) -> str:
    return _SUFFIX.sub("", (name or "").strip())


def _placed(points, px, py, lx, ly, a, fx, fy, ox, oy) -> list:
    """Shape-local points on the page, as the box is placed: less the LocPin, mirrored by the flips, turned by Angle
    about the pin, plus the pin and the group's origin."""
    co, si = math.cos(a or 0.0), math.sin(a or 0.0)
    out = []
    for x, y in points:
        rx, ry = x - (lx or 0.0), y - (ly or 0.0)
        rx, ry = -rx if fx else rx, -ry if fy else ry
        out.append((px + ox + rx * co - ry * si, py + oy + rx * si + ry * co))
    return out


def _route(rows, c, ox, oy) -> list:
    """A 1-D shape's Geometry vertices on the page, in IX order; Rel rows scaled by Width and Height."""
    px, py, w, h = (_num(c.get(n)) for n in ("PinX", "PinY", "Width", "Height"))
    if px is None or py is None:
        return []
    order = sorted(rows, key=lambda r: (_num(r[0]) is None, _num(r[0]) or 0.0))
    points = []
    for _, cells in order:
        t = cells.get("T") or ""
        x, y = _num(cells.get("X")), _num(cells.get("Y"))
        if t not in _VERTICES or x is None or y is None:
            continue
        if t.startswith("Rel"):
            if w is None or h is None:
                continue
            x, y = x * w, y * h
        points.append((x, y))
    return _placed(points, px, py, _num(c.get("LocPinX")), _num(c.get("LocPinY")), _num(c.get("Angle")),
                   _on(c.get("FlipX")), _on(c.get("FlipY")), ox, oy)


def _on(v) -> bool:
    """A boolean cell's value: "1" or "TRUE" (any case)."""
    return (v or "").strip().lower() in ("1", "true")


def _num(v):
    """A finite float within bounds, else None (NaN, inf, junk all mean 'no value')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return max(-COORD, min(COORD, f))


def _arrow(v):
    """An arrow cell's value: True above 0, False for any other number, None for no number."""
    f = _num(v)
    return None if f is None else f > 0


def _kind(name) -> str:
    n = (name or "").casefold()
    for word, kind in _KINDS:
        if word in n:
            return kind
    return ""


def _box(c, ox, oy):
    """(x0, y0, x1, y1) on the page of the shape with cells `c` under a parent at origin (ox, oy), or None without a
    pin or a size: its box mirrored about the pin by FlipX / FlipY, bounded after the turn by Angle about the pin."""
    px, py, w, h = (_num(c.get(n)) for n in ("PinX", "PinY", "Width", "Height"))
    if px is None or py is None or w is None or h is None:
        return None
    lx, ly = _num(c.get("LocPinX")) or 0.0, _num(c.get("LocPinY")) or 0.0
    x0 = lx - w if _on(c.get("FlipX")) else -lx  # the box's left and bottom from the pin
    y0 = ly - h if _on(c.get("FlipY")) else -ly
    corners = [(x0, y0), (x0 + w, y0), (x0, y0 + h), (x0 + w, y0 + h)]
    a = _num(c.get("Angle")) or 0.0
    if a:
        co, si = math.cos(a), math.sin(a)
        corners = [(x * co - y * si, x * si + y * co) for x, y in corners]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return px + ox + min(xs), py + oy + min(ys), px + ox + max(xs), py + oy + max(ys)


def _end(c, which, ox, oy):
    """A 1-D shape's Begin or End point on the page, or None when either coordinate is missing."""
    x, y = _num(c.get(which + "X")), _num(c.get(which + "Y"))
    return None if x is None or y is None else (x + ox, y + oy)


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


def _glued(root) -> dict:
    """{connector ID: (begin ID or None, end ID or None)} of a page's Connects rows from BeginX and EndX (the first
    row of each end wins)."""
    conn = _kid(root, "Connects")
    ends = {}
    for c in kids(conn, "Connect") if conn is not None else ():
        cell, sheet, to = attr(c, "FromCell"), attr(c, "FromSheet"), attr(c, "ToSheet")
        if cell not in ("BeginX", "EndX") or sheet is None or to is None:
            continue
        e = ends.setdefault(sheet, [None, None])
        i = 0 if cell == "BeginX" else 1
        if e[i] is None:
            e[i] = to
    return {s: (b, e) for s, (b, e) in ends.items()}


class _Reader:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.part = ""
        self.image_bytes = 0
        self.main = ""
        self.master_parts = None  # Master ID -> master part, read from masters.xml on first use
        self.master_names = {}    # Master ID -> its NameU in masters.xml
        self.masters = {}         # master part -> ({shape ID: element}, first top-level shape), or None: unreadable
        self.shapes = {}          # master shape element -> (text, hides it), so a shared shape is read once
        self.mcells = {}
        self.mrows = {}
        self.rows_left = limits.SHAPE_ROWS
        self.style_index = None   # StyleSheet ID -> ({arrow cell: V}, its LineStyle), read on first use
        self.style_arrows = {}    # (StyleSheet ID, arrow cell) -> its value up the chain
        self.room = limits.MAX_OUTPUT  # shape text the document may still emit, copies counted (see `spend`)
        self.budget = diagram.ConnectionBudget(ctx)
        self.dia = None           # this page's diagram
        self.keys = set()         # this page's diagram keys given out so far
        self.glued = {}           # this page's Connects ends: connector ID -> (begin ID, end ID), either may be None

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    # ---- package
    def run(self) -> Doc:
        ooxml_base.macros(self.pkg, self.ctx)
        main = None
        for rel in ooxml_base.rels(self.pkg, "").values():
            if rel.type.endswith("/document") and not rel.external and self.pkg.has(rel.target):
                main = rel.target
                break
        if main is None:
            raise ReadError("damaged", "not a Visio package")
        self.main = main
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
        doc, seen, meta = Doc(), set(), []
        for number, entry in enumerate(entries, 1):
            if self.ctx.late():
                break
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
                    sec = self.page(rel.target, name)
                    doc.blocks.append(sec)
                    meta.append((attr(entry, "ID"), _on(attr(entry, "Background")), attr(entry, "BackPage"), sec))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["page"] += 1
        if entries and not doc.blocks:
            raise ReadError("damaged", "no readable page")
        _backgrounds(meta)
        return doc

    # ---- pages and shapes
    def page(self, part, name) -> Section:
        root, _ = ooxml_base.load(self.pkg, part)
        if local(root.tag) != "PageContents":
            raise ReadError("damaged", "not a page")
        self.part = part
        sec = Section("page", title=name)
        self.dia, self.keys, self.glued = diagram.Diagram(self.ctx, self.budget), set(), _glued(root)
        shapes = _kid(root, "Shapes")
        out = []
        if shapes is not None:
            self.walk(shapes, 0.0, 0.0, 0, out, None, None)
        placed = sorted((e for e in out if e[0] is not None), key=lambda e: e[0])
        slots = [(key, blocks) for _, key, blocks in placed + [e for e in out if e[0] is None]]
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = self.dia.place(slots)
        sec.blocks.extend(flow)
        self.dia, self.keys, self.glued = None, set(), {}
        return sec

    def key(self, sid):
        """A shape's diagram key: its ID, or a key no glue names when it has none or repeats one on the page."""
        k = sid if sid is not None and sid not in self.keys else ("unkeyed", len(self.keys))
        self.keys.add(k)
        return k

    def walk(self, container, dx, dy, d, out, master, parent):
        """Append (sort key or None, diagram key, blocks) for every Shape under `container`; (dx, dy) is the group
        origin, `master` the Master ID of the nearest enclosing instance (its sub-shapes name their master shape by
        MasterShape), `parent` the group's diagram key."""
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        for el in kids(container, "Shape"):
            if self.ctx.late():
                break
            with self.ctx.guard("shape"):
                self.shape(el, dx, dy, d, out, master, parent)

    def shape(self, el, dx, dy, d, out, master, parent):
        own_cells = _cells(el)
        x, y = _num(own_cells.get("PinX")), _num(own_cells.get("PinY"))
        sort = (-(y + dy), x + dx) if x is not None and y is not None else None
        own = attr(el, "Master")
        master = own if own is not None else master
        ref = own is not None or (master is not None and attr(el, "MasterShape") is not None)
        mel = self.master_el(master, attr(el, "MasterShape")) if ref else None
        inherited = self.master_text(mel) if ref else None
        has_text, hide = _kid(el, "Text") is not None, own_cells.get("HideText")
        inlines = None  # an inherited text's lines are built once per master shape and shared by its copies
        if has_text:
            text = _text(el)
            text = self.spend(text, "text") if text.strip() else ""
        elif ref and inherited is None:  # the master is not read: its text is lost
            self.ctx.skipped["stencil"] += 1
            text = ""
        else:
            text = self.spend(inherited[0], "stencil") if inherited else ""
            inlines = inherited[2] if text else None
        hidden = _on(hide) if hide is not None else bool(inherited and inherited[1])
        blocks = []
        if text:
            inlines = inlines if inlines is not None else _inlines(text.strip("\n"))
            blocks.append(Para(inlines, hidden="hidden" if hidden else ""))
        m_cells, m_struct, m_style = self.master_cells(mel)
        props, links = self.merged(mel, el, "Property"), self.merged(mel, el, "Hyperlink")
        if props is None or links is None:
            self.ctx.skipped["shape_data"] += 1
        data = [Para([Text(line)]) for line in _data(props or [], links or []) if self.spend(line, "text")]
        blocks.extend(self.picture(el))
        c = dict(own_cells)
        for n in INHERITED:
            if c.get(n) is None and m_cells.get(n) is not None:
                c[n] = m_cells[n]
        sid = attr(el, "ID")
        key = self.key(sid)
        slot = key
        glue = self.glued.get(sid) if key == sid else None  # Connects rows name the first shape of an ID
        if (c.get("BeginX") is not None and c.get("EndX") is not None) or glue is not None:
            begin, end = glue or (None, None)
            style = attr(el, "LineStyle")
            style = style if style is not None else m_style
            arrows = tuple(self.arrow(n, own_cells, m_cells, style) for n in ARROWS)
            rows = self.merged(mel, el, "Geometry")
            if rows is None:
                self.ctx.skipped["route"] += 1
            self.dia.connector(key, begin, end, _end(c, "Begin", dx, dy), _end(c, "End", dx, dy), arrows,
                               blocks=[] if hidden and text else blocks, path=_route(rows or [], c, dx, dy),
                               data=data)
            if hidden and text:  # hidden text is no label: it keeps its place in the flow
                slot = None
        else:
            mname = _master_name(self.master_names.get(own)) if own is not None else ""
            kind = _kind(mname if mname else attr(el, "NameU"))
            struct = _structure(el)
            struct = struct if struct is not None else m_struct
            container = (struct or "").strip().strip('"').strip().casefold() == "container"
            part = (False if own is not None
                    else True if master is not None and attr(el, "MasterShape") is not None else None)
            self.dia.shape(key, blocks, kind, _box(c, dx, dy), hidden and bool(text), container,
                           text if container and not hidden else "", parent, mname, part, data)
        out.append((sort, slot, blocks + data))
        sub = _kid(el, "Shapes")
        if sub is not None:  # a missing pin or loc-pin term counts as 0, the other term still applies
            ox = dx + (x or 0.0) - (_num(c.get("LocPinX")) or 0.0)
            oy = dy + (y or 0.0) - (_num(c.get("LocPinY")) or 0.0)
            self.walk(sub, ox, oy, d + 1, out, master, key)

    def arrow(self, name, own, inherited, style):
        """An arrow cell's value: the shape's own, else its master shape's, else its LineStyle chain's; None: none."""
        for v in (own.get(name), inherited.get(name)):
            f = _arrow(v)
            if f is not None:
                return f
        return self.style_arrow(style, name) if style is not None else None

    # ---- style sheets
    def style_arrow(self, style, name):
        """`name`'s value from the first style up the LineStyle chain from `style` that has one, walking at most
        `limits.MAX_DEPTH` styles and none twice; None when none has it."""
        if (style, name) not in self.style_arrows:
            index, seen, s, found = self.styles(), set(), style, None
            for _ in range(limits.MAX_DEPTH):
                if s is None or s in seen or s not in index:
                    break
                seen.add(s)
                cells, s_up = index[s]
                found = _arrow(cells.get(name))
                if found is not None:
                    break
                s = s_up
            self.style_arrows[(style, name)] = found
        return self.style_arrows[(style, name)]

    def styles(self) -> dict:
        """{StyleSheet ID: ({arrow cell: V}, its LineStyle)} from the main document part (the first of an ID wins),
        read once; {} when it cannot be read."""
        if self.style_index is None:
            self.style_index = {}  # set first: a part that fails is not read again
            try:  # a part the reader refuses (damaged, past ZIP_RATIO, ...) gives no style; only timeout stops
                root, _ = ooxml_base.load(self.pkg, self.main)
            except ReadError as e:
                if e.kind == "timeout":
                    raise
                return self.style_index
            sheets = _kid(root, "StyleSheets")
            for s in kids(sheets, "StyleSheet") if sheets is not None else ():
                sid = attr(s, "ID")
                if sid is None or sid in self.style_index:
                    continue
                cells = {}
                for c in kids(s, "Cell"):
                    n = attr(c, "N")
                    if n in ARROWS and n not in cells:
                        cells[n] = attr(c, "V")
                self.style_index[sid] = (cells, attr(s, "LineStyle"))
        return self.style_index

    # ---- masters
    def spend(self, text, kind) -> str:
        """`text` (a shape's own, or each inherited copy) charged to the document's MAX_OUTPUT room: its characters
        and LINE more for each line break, since a line is two model objects whatever its length. Past the room it is
        left out and counted `kind` (`text` for own text, `stencil` for inherited)."""
        cost = len(text) + LINE * (text.count("\n") + text.count("\r"))
        if cost > self.room:
            self.ctx.skipped[kind] += 1
            return ""
        self.room -= cost
        return text

    def master_el(self, mid, sid):
        """Master `mid`'s shape `sid` (sid None: the master's first top-level shape); None when the master has no such
        shape; False when the master is not read (not listed, unreadable, or past MASTERS parts)."""
        if self.master_parts is None:
            self.master_parts = {}  # set first: an index that fails is not tried again
            self.master_parts = self.master_index()
        part = self.master_parts.get(mid)
        if part is None:
            return False
        if part not in self.masters:
            if len(self.masters) >= MASTERS:
                return False
            self.masters[part] = None  # set first: a part that fails is not loaded again
            self.masters[part] = self.load_master(part)
        index = self.masters[part]
        if index is None:
            return False
        by_id, first = index
        return first if sid is None else by_id.get(sid)

    def master_text(self, el):
        """(text, hides its text, its inlines) of a master shape, text "" when it has only blanks; ("", False, [])
        for no master shape; None when the master is not read (`master_el` gave False)."""
        if el is False:
            return None
        if el is None:
            return "", False, []
        if el not in self.shapes:
            text = _text(el)
            text = text if text.strip() else ""
            self.shapes[el] = (text, _on(_cells(el).get("HideText")), _inlines(text.strip("\n")) if text else [])
        return self.shapes[el]

    def master_cells(self, el):
        """(cells, User msvStructureType, LineStyle) of a master shape, read once; ({}, None, None) for none."""
        if el is None or el is False:
            return {}, None, None
        if el not in self.mcells:
            self.mcells[el] = (_cells(el), _structure(el), attr(el, "LineStyle"))
        return self.mcells[el]

    def merged(self, mel, el, name):
        """[(row key, cells)] of the shape's `name` rows over its master shape's, in the master's order then the
        shape's new rows: each cell the shape gives with a value overriding, a Del row gone. The master's rows are
        read once and only its shown ones (`_shown`) are taken; the rows taken and the shape's own are charged to
        SHAPE_ROWS. None when they pass what is left."""
        own = _rows(el, name)
        index, shown = self.master_rows(mel, name)
        if len(own) + len(shown) > self.rows_left:
            self.rows_left = 0
            return None
        self.rows_left -= len(own) + len(shown)
        rows = {k: (at, cells) for at, k, cells in shown}
        for i, (key, cells, gone) in enumerate(own):
            k = ("own", i) if key is None else key
            if gone:
                rows.pop(k, None)
                continue
            at, base = rows.get(k) or index.get(k) or (len(index) + i, {})
            rows[k] = (at, {**base, **{n: v for n, v in cells.items() if v is not None}})
        return [(k, cells) for k, (_, cells) in sorted(rows.items(), key=lambda e: e[1][0])]

    def master_rows(self, el, name):
        if el is None or el is False:
            return {}, []
        if (el, name) not in self.mrows:
            index, shown = {}, []
            for i, (key, cells, gone) in enumerate(_rows(el, name)):
                k = ("master", i) if key is None else key
                if gone or k in index:
                    continue
                index[k] = (i, cells)
                if _shown(name, cells):
                    shown.append((i, k, cells))
            self.mrows[(el, name)] = (index, shown)
        return self.mrows[(el, name)]

    def master_index(self) -> dict:
        """{Master ID: part} from masters.xml through its relationships (the first entry of an ID wins); fills
        `master_names` with each listed master's NameU."""
        listing = "visio/masters/masters.xml"
        for rel in ooxml_base.rels(self.pkg, self.main).values():
            if rel.type.endswith("/masters") and not rel.external:
                listing = rel.target
                break
        if not self.pkg.has(listing):
            return {}
        try:  # a member the reader refuses (damaged, past ZIP_RATIO, ...) lists no master; only timeout stops
            root, _ = ooxml_base.load(self.pkg, listing)
            rels = ooxml_base.rels(self.pkg, listing, self.ctx)
        except ReadError as e:
            if e.kind == "timeout":
                raise
            return {}
        out = {}
        for m in kids(root, "Master"):
            mid, ref = attr(m, "ID"), _kid(m, "Rel")
            rel = rels.get(_rid(ref) or "") if ref is not None else None
            if mid is None or mid in out or rel is None or rel.external or not rel.type.endswith("/master"):
                continue
            if self.pkg.has(rel.target):
                out[mid] = rel.target
                self.master_names[mid] = attr(m, "NameU") or ""
        return out

    def load_master(self, part):
        """({shape ID: element}, first top-level shape) of one master part, or None when it cannot be read. Shapes
        nested past MAX_DEPTH are not indexed (counted `depth` once for the part)."""
        try:  # a part the reader refuses (damaged, past ZIP_RATIO, ...) is unread; only timeout stops the document
            root, _ = ooxml_base.load(self.pkg, part)
        except ReadError as e:
            if e.kind == "timeout":
                raise
            return None
        if local(root.tag) != "MasterContents":
            return None
        shapes = _kid(root, "Shapes")
        top = kids(shapes, "Shape") if shapes is not None else []
        by_id, stack, deep = {}, [(top, 0)], False
        while stack:
            items, d = stack.pop()
            for el in items:
                sid = attr(el, "ID")
                if sid is not None and sid not in by_id:
                    by_id[sid] = el
                sub = _kid(el, "Shapes")
                if sub is None:
                    continue
                if d + 1 > limits.MAX_DEPTH:
                    deep = True
                else:
                    stack.append((kids(sub, "Shape"), d + 1))
        if deep:
            self.deep()
        return by_id, (top[0] if top else None)

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


def _backgrounds(meta):
    back = {}
    for pid, bg, _, sec in meta:
        if bg and pid is not None:
            back.setdefault(pid, sec)
    under = {pid: [] for pid in back}
    for pid, _, ref, sec in meta:
        if ref in back and ref != pid:
            sec.blocks.insert(0, Para([Text(f"Background page: {back[ref].title}")]))
            under[ref].append(sec.title)
    for pid, sec in back.items():
        names = under[pid]
        sec.blocks.insert(0, Para([Text("Background of: " + ", ".join(names) if names else "Background page")]))


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
