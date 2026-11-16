"""Visio drawing (vsdx, vsdm) to the document model: one section per page, shape text in reading order, picture
shapes, then the page's connection list.

Elements match by local name. A shape without a Text element takes its master's shape text (`visio/masters`, each
master part loaded once, up to MASTERS parts); a HideText cell, the shape's own else its master shape's, makes the text
hidden. Shape text, own and each inherited copy, is charged to one MAX_OUTPUT room by its characters and LINE per
line break. Connectors glued at both ends are listed after the page's shapes as "A → B (label)", their shown text left out
of the shape flow; `name_of`, `connections` and `ConnectionBudget` are shared with the binary vsd reader. A vbaProject
part is named, never read.
"""
import math

from .. import limits
from ..containers.zipx import Package, attr, kids, local
from ..limits import ReadError
from ..model import Break, Doc, Image, ListItem, Para, Section, Text
from . import ooxml_base

IMAGE_BYTES = 256 * 2**20  # image bytes one drawing may emit, copies counted
MAX_TITLE = 200
MAX_TEXT = 20_000          # characters of text one shape may emit
COORD = 1e12
MASTERS = 1_000            # master parts one vsdx may load; inherited text from the rest is counted `stencil`
CONNECTIONS = 10_000       # connectors one document may list (vsd and vsdx); past it `connections_cut` per cut list
NAME = 80                  # characters of a shape's name in a connection list
# characters one line break of shape text is charged against the MAX_OUTPUT room, besides its own: a line is a Text
# and a Break in the model however short, so "a\n" text would otherwise make two model objects per two characters
LINE = 64


def name_of(text: str, name_u: str, name: str, sid: str) -> str:
    """A shape's name in a connection list: the first line of its text cut at NAME characters, else its NameU, else
    its Name, else `shape <ID>`."""
    line = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0].strip()
    if line:
        return line[:NAME]
    for v in (name_u, name):
        v = (v or "").strip()
        if v:
            return v[:NAME]
    return f"shape {str(sid)[:NAME]}"


def connections(edges: list, names: dict) -> list:
    """`Connections:` and one `- A → B` item per (begin id, end id, label) edge, ` (label)` added when the connector
    has text; `names` maps a shape id to its name_of (an id it lacks reads `shape <ID>`). [] for no edges."""
    if not edges:
        return []
    out = [Para([Text("Connections:")])]
    for begin, end, label in edges:
        line = f"{names.get(begin) or name_of('', '', '', begin)} → {names.get(end) or name_of('', '', '', end)}"
        label = " ".join((label or "").split())
        if label:
            line += f" ({label})"
        out.append(ListItem(0, "-", [Text(line)]))
    return out


class ConnectionBudget:
    """The document's CONNECTIONS room (read when made): `take` keeps what fits of one page's edges and counts
    `connections_cut` once for a list it cuts."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.room = CONNECTIONS

    def take(self, edges) -> list:
        if len(edges) <= self.room:
            self.room -= len(edges)
            return edges
        kept = edges[:max(0, self.room)]
        self.room = 0
        self.ctx.skipped["connections_cut"] += 1
        return kept


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
        if n in ("PinX", "PinY", "LocPinX", "LocPinY", "HideText") and n not in out:
            out[n] = attr(c, "V")
    return out


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
    """{connector ID: (begin ID, end ID)} of a page's Connects, for connectors glued at both ends (BeginX and EndX;
    the first Connect row of each end wins), in the order their first row appears."""
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
    return {s: (b, e) for s, (b, e) in ends.items() if b is not None and e is not None}


class _Reader:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.part = ""
        self.image_bytes = 0
        self.main = ""
        self.master_parts = None  # Master ID -> master part, read from masters.xml on first use
        self.masters = {}         # master part -> ({shape ID: element}, first top-level shape), or None: unreadable
        self.shapes = {}          # master shape element -> (text, hides it), so a shared shape is read once
        self.room = limits.MAX_OUTPUT  # shape text the document may still emit, copies counted (see `spend`)
        self.budget = ConnectionBudget(ctx)
        self.glued = {}           # this page's connectors glued at both ends: ID -> (begin ID, end ID)
        self.info = {}            # this page's shapes by ID (first wins): (text, NameU, Name), kept when glued

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    # ---- package
    def run(self) -> Doc:
        if any(n.rsplit("/", 1)[-1].lower() == "vbaproject.bin" for n in self.pkg.names):
            self.ctx.skipped["vba"] = 1  # macros are never read; named once, however many projects there are
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
        self.glued, self.info = _glued(root), {}
        shapes = _kid(root, "Shapes")
        if shapes is not None:
            out = []
            self.walk(shapes, 0.0, 0.0, 0, out, None)
            placed = sorted((e for e in out if e[0] is not None), key=lambda e: e[0])
            for _, blocks in placed + [e for e in out if e[0] is None]:
                sec.blocks.extend(blocks)
        # a Connect row naming a connector that is not a shape on this page lists nothing
        edges = [(b, e, self.info[c][0]) for c, (b, e) in self.glued.items() if c in self.info]
        edges = self.budget.take(edges)
        names = {}
        for b, e, _ in edges:
            for sid in (b, e):
                if sid not in names and sid in self.info:
                    names[sid] = name_of(*self.info[sid], sid)
        sec.blocks.extend(connections(edges, names))
        self.glued, self.info = {}, {}
        return sec

    def walk(self, container, dx, dy, d, out, master):
        """Append (sort key or None, blocks) for every Shape under `container`; (dx, dy) is the group offset, `master`
        the Master ID of the nearest enclosing instance (its sub-shapes name their master shape by MasterShape)."""
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        for el in kids(container, "Shape"):
            with self.ctx.guard("shape"):
                self.shape(el, dx, dy, d, out, master)

    def shape(self, el, dx, dy, d, out, master):
        cells = _cells(el)
        x, y = _num(cells.get("PinX")), _num(cells.get("PinY"))
        key = (-(y + dy), x + dx) if x is not None and y is not None else None
        own = attr(el, "Master")
        master = own if own is not None else master
        ref = own is not None or (master is not None and attr(el, "MasterShape") is not None)
        has_text, hide = _kid(el, "Text") is not None, cells.get("HideText")
        # the master shape this one inherits from: its text when it has none, its HideText when it sets none
        need = ref and not (has_text and hide is not None)
        inherited = self.master_shape(master, attr(el, "MasterShape")) if need else None
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
        sid = attr(el, "ID")
        if self.glued and sid is not None and sid not in self.info:  # hidden text never names a shape
            self.info[sid] = ("" if hidden else text, attr(el, "NameU"), attr(el, "Name"))
        blocks = []
        if text and (hidden or sid not in self.glued):  # a glued connector's shown text is in the list
            inlines = inlines if inlines is not None else _inlines(text.strip("\n"))
            blocks.append(Para(inlines, hidden="hidden" if hidden else ""))
        blocks.extend(self.picture(el))
        if blocks:
            out.append((key, blocks))
        sub = _kid(el, "Shapes")
        if sub is not None:  # a missing pin or loc-pin term counts as 0, the other term still applies
            ox = dx + (x or 0.0) - (_num(cells.get("LocPinX")) or 0.0)
            oy = dy + (y or 0.0) - (_num(cells.get("LocPinY")) or 0.0)
            self.walk(sub, ox, oy, d + 1, out, master)

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

    def master_shape(self, mid, sid):
        """(text, hides its text, its inlines) of master `mid`'s shape `sid` (sid None: the master's first top-level
        shape), text "" when it has only blanks; ("", False, []) when the master has no such shape; None when the master is not read (not
        listed, unreadable, or past MASTERS parts)."""
        if self.master_parts is None:
            self.master_parts = {}  # set first: an index that fails is not tried again
            self.master_parts = self.master_index()
        part = self.master_parts.get(mid)
        if part is None:
            return None
        if part not in self.masters:
            if len(self.masters) >= MASTERS:
                return None
            self.masters[part] = None  # set first: a part that fails is not loaded again
            self.masters[part] = self.load_master(part)
        index = self.masters[part]
        if index is None:
            return None
        by_id, first = index
        el = first if sid is None else by_id.get(sid)
        if el is None:
            return "", False, []
        if el not in self.shapes:
            text = _text(el)
            text = text if text.strip() else ""
            self.shapes[el] = (text, _on(_cells(el).get("HideText")), _inlines(text.strip("\n")) if text else [])
        return self.shapes[el]

    def master_index(self) -> dict:
        """{Master ID: part} from masters.xml through its relationships (the first entry of an ID wins)."""
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


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
