"""Shared OpenDocument layer: styles, numbering and the Body walker for odt, ods and odp. Elements match by local name.

Drawing shapes and connectors go to `diagram.Diagram` through `register`: keyed by `draw:id`, else `xml:id`; glued by
`draw:start-shape` and `draw:end-shape`; boxes from `svg:x`, `svg:y`, `svg:width`, `svg:height` and ends from
`svg:x1`…`svg:y2`, in cm (`draw:transform` is not applied); arrowheads from the graphic style chain. An odt body that
holds a connector is one scope: the shapes connectors glue and the connectors leave a `_Slot` in the flow, and
`Body.place` puts the diagram at the first one that anchors it once the body is read.

An object a frame names (`Object N/content.xml`) is read once, through the document's embedded.Objects: past
MAX_OBJECTS, or once the objects have taken EMBED_SECONDS, it is not read; its tree is charged to EMBED_NODES. One
not read, or that does not parse, is counted under object.
"""
import collections
import copy
import math
import re

from .. import diagram, embedded, limits
from ..charts import caption, kind
from ..containers.zipx import attr, charged, first, kids, local, parse_xml, resolve
from ..limits import ReadError
from ..markdown import blocks_len
from ..model import (
    Bold, Break, Cell, Doc, Equation, Heading, Hidden, Image, Inserted, Link, ListItem,
    NoteRef, Para, Section, Table, Text,
)

_DROP = frozenset({"page-number", "page-count", "annotation-end", "soft-page-break", "number", "bookmark",
                   "bookmark-start", "bookmark-end", "hidden-paragraph", "tracked-changes"})
_LEAVES = frozenset({"s", "tab", "line-break", "note", "annotation"})
_CONTAINERS = frozenset({"table-of-content", "user-index", "alphabetical-index", "bibliography",
                         "illustration-index", "table-index", "object-index", "index-body",
                         "index-title", "numbered-paragraph"})
_REPEAT = 100      # a repeated non-empty row or cell is expanded at most this often
_GAP = 64          # a repeated empty row or cell between content is expanded at most this often
_SPAN = 1000       # largest span taken from a file
_BUDGET = 50_000  # cells one document may expand to, nested tables and reused objects included
_SPACES = 200
_SYLLABLE = "가나다라마바사아자차카타파하"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
_UNITS = {"cm": 1.0, "mm": 0.1, "in": 2.54, "pt": 2.54 / 72, "px": 2.54 / 96, "pc": 2.54 / 6}
_LEN = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(cm|mm|in|pt|px|pc)\s*$")
SHAPES = frozenset({"frame", "custom-shape", "rect", "ellipse", "circle", "polygon", "polyline", "path",
                    "caption", "line", "connector", "measure", "regular-polygon"})
GROUPS = frozenset({"g", "a"})
_KINDS = {"flowchart-decision": "decision", "diamond": "decision", "flowchart-terminator": "terminator",
          "ellipse": "circle", "circle": "circle", "flowchart-connector": "circle", "can": "store",
          "flowchart-magnetic-disk": "store"}
_CHART_KINDS = {"circle": "pie", "ring": "doughnut", "filled-radar": "filled radar"}
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _squash(s, n=200):
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _itext(inlines) -> str:
    out = ""
    for it in inlines:
        if isinstance(it, Text):
            out += it.text
        elif isinstance(it, (Bold, Link, Inserted)):
            out += _itext(it.inlines)
        elif isinstance(it, Break):
            out += " "
    return out


def _join(out, it):
    """Append `it`; an Inserted right after one by the same author joins it."""
    if isinstance(it, Inserted) and out and isinstance(out[-1], Inserted) and out[-1].author == it.author:
        out[-1].inlines.extend(it.inlines)
    else:
        out.append(it)


def _lift(make, sub):
    """`make(sub)`, or an Inserted around `make` of its content when all of `sub` is one author's insertion."""
    if sub and all(isinstance(x, Inserted) and x.author == sub[0].author for x in sub):
        return Inserted([make([y for x in sub for y in x.inlines])], sub[0].author)
    return make(sub)


def _flat(blocks) -> str:
    parts = []
    for b in blocks:
        if hasattr(b, "inlines"):
            parts.append(_itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(_flat(c.blocks) for row in b.rows for c in row)
    return " ".join(_squash(p, 10_000) for p in parts if p.strip())


def _empty(blocks) -> bool:
    return all(isinstance(b, Para) and not _itext(b.inlines).strip() and not b.hidden for b in blocks)


# ---- drawing shapes and connectors
def _cm(s):
    m = _LEN.match(s or "")
    if not m:
        return None
    v = float(m.group(1)) * _UNITS[m.group(2)]
    return v if math.isfinite(v) else None


def _named(el, name, ns_part):
    """The attribute with this local name whose namespace URI holds ns_part, or None."""
    for k, v in el.attrib.items():
        if local(k) == name and ns_part in k.partition("}")[0]:
            return v
    return None


def _own_id(el):
    return _named(el, "id", "drawing") or el.get(_XML_ID) or None


def shape_key(el, keys: set):
    """A shape's diagram key: its draw:id, else its xml:id; a key no glue names when it has neither or repeats one
    already given out in the scope."""
    k = _own_id(el)
    if k is None or k in keys:
        k = ("unkeyed", len(keys))
    keys.add(k)
    return k


def arrows(styles, el) -> tuple:
    """(start, end) arrowheads from the graphic style chain of draw:style-name, then the presentation style chain of
    presentation:style-name: a marker name is True, an empty value or `none` False, absent None."""
    draw, pres = _named(el, "style-name", "drawing"), _named(el, "style-name", "presentation")
    out = []
    for key in ("marker-start", "marker-end"):
        v = styles.prop(draw, "graphic", key)
        if v is None and pres:
            v = styles.prop(pres, "presentation", key)
        out.append(None if v is None else v.strip().casefold() not in ("", "none"))
    return tuple(out)


def edge_like(el, styles) -> bool:
    """Whether the element is a connector, or a line with an arrowhead: what can make a diagram edge."""
    n = local(el.tag)
    return n == "connector" or (n == "line" and True in arrows(styles, el))


def register(dia, styles, el, key, blocks, hidden=False):
    """One drawing element to the scope's diagram: a draw:connector or draw:line as a connector, anything else as a
    shape with its kind and box in cm."""
    n = local(el.tag)
    if n in ("connector", "line"):
        p0, p1 = ((_cm(attr(el, f"x{i}")), _cm(attr(el, f"y{i}"))) for i in (1, 2))
        begin, end = (attr(el, "start-shape"), attr(el, "end-shape")) if n == "connector" else (None, None)
        dia.connector(key, begin, end, p0, p1, arrows(styles, el), line=n == "line", hidden=hidden, blocks=blocks)
        return
    if n in ("ellipse", "circle"):
        kind = "circle"
    else:
        geo = kids(el, "enhanced-geometry") if n == "custom-shape" else []
        kind = _KINDS.get((attr(geo[0], "type") or "").casefold(), "") if geo else ""
    x, y, w, h = (_cm(attr(el, a)) for a in ("x", "y", "width", "height"))
    box = None if None in (x, y, w, h) else (x, y, x + w, y + h)
    dia.shape(key, blocks, kind, box, hidden)


class _Slot:
    """Where an odt diagram member stood in the flow, until `Body.place` puts the diagram's flow there. It carries the
    member's blocks, so the copy budgets (`blocks_len`, `Body.weight`) charge them wherever a repeat copies the slot."""

    def __init__(self, key, blocks):
        self.key, self.blocks, self.hidden = key, blocks, ""


def _swap(blocks, fn, d=0) -> list:
    """The blocks with each _Slot replaced by fn(slot), through sections and table cells."""
    if d > limits.MAX_DEPTH:
        return blocks
    out = []
    for b in blocks:
        if isinstance(b, _Slot):
            out.extend(fn(b))
            continue
        if isinstance(b, Section):
            b.blocks = _swap(b.blocks, fn, d + 1)
        elif isinstance(b, Table):
            for row in b.rows:
                for cell in row:
                    cell.blocks = _swap(cell.blocks, fn, d + 1)
        out.append(b)
    return out


# ---- numbering markers
def _bijective(n, alphabet):
    out = ""
    while n > 0:
        n, r = divmod(n - 1, len(alphabet))
        out = alphabet[r] + out
    return out or str(n)


def _roman(n):
    if not 0 < n < 4000:
        return str(n)
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def _fmt(n, kind):
    if n < 1:
        return str(n)
    if kind == "a":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "A":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "i":
        return _roman(n).lower()
    if kind == "I":
        return _roman(n)
    if kind == "가":
        return _bijective(n, _SYLLABLE)
    if kind == "①":
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    return str(n)


class Styles:
    """Named styles from content.xml and styles.xml, with parent chains and family defaults."""

    def __init__(self, content_root, styles_root):
        self.styles, self.defaults, self.lists = {}, {}, {}
        for root in (content_root, styles_root):  # content first: its automatic styles win
            if root is None:
                continue
            for el in root.iter():
                n = local(el.tag)
                if n == "style":
                    self.styles.setdefault((attr(el, "family", ""), attr(el, "name", "")), el)
                elif n == "default-style":
                    self.defaults.setdefault(attr(el, "family", ""), el)
                elif n == "list-style":
                    self.lists.setdefault(attr(el, "name", ""), {
                        _int(attr(c, "level")): c for c in el if attr(c, "level") is not None})

    @staticmethod
    def _own(el, key):
        for c in el:
            if local(c.tag).endswith("properties"):
                v = attr(c, key)
                if v is not None:
                    return v
        return None

    def prop(self, name, family, key):
        seen, cur = set(), name
        while cur and cur not in seen:
            seen.add(cur)
            el = self.styles.get((family, cur))
            if el is None:
                break
            v = self._own(el, key)
            if v is not None:
                return v
            cur = attr(el, "parent-style-name")
        d = self.defaults.get(family)
        return self._own(d, key) if d is not None else None

    def list_level(self, name, level):
        return self.lists.get(name, {}).get(level)


def _chart_props(root, name) -> dict:
    """The style:chart-properties attributes, by local name, of the automatic style `name` of a chart object."""
    if not name:
        return {}
    for styles in kids(root, "automatic-styles"):
        for st in kids(styles, "style"):
            if attr(st, "name") == name:
                props = next(iter(kids(st, "chart-properties")), None)
                return {local(k): v for k, v in props.attrib.items()} if props is not None else {}
    return {}


class _Folder:
    """The members under `prefix` of package `pkg`, as the package of the OpenDocument object that folder holds: its
    content.xml, styles.xml and pictures, its links resolved inside it."""

    def __init__(self, pkg, prefix):
        self.pkg, self.prefix = pkg, prefix
        self.names = [n[len(prefix):] for n in pkg.names if n.startswith(prefix)]

    def has(self, name) -> bool:
        return self.pkg.has(self.prefix + name)

    def size(self, name) -> int:
        return self.pkg.size(self.prefix + name)

    def read(self, name) -> bytes:
        return self.pkg.read(self.prefix + name)

    def xml(self, name):
        return self.pkg.xml(self.prefix + name)


class Body:
    """Walks ODF text content into model blocks and inlines. Task 10 reuses it for cells and shapes."""

    def __init__(self, pkg, ctx, styles, doc=None):
        self.pkg, self.ctx, self.styles = pkg, ctx, styles
        self.doc = doc if doc is not None else Doc()
        self.extras = []  # blocks produced by frames met inside the current paragraph
        self.cache, self.objects, self.bold_of = {}, {}, {}
        self.counts = collections.Counter()
        self.last, self.ids = None, {}  # list counters: the previous list's, and by xml:id
        self.left = _BUDGET  # expansion budget still available
        self.chars_left = limits.SHEET_CHARS  # characters that copies (repeats, reused objects) may still add
        self.wt = {}  # id(Table) -> (Table, cells it expands to)
        self.charts = {}
        self.glued = None  # odt read as one diagram scope: the shape ids connectors glue; None otherwise
        self.members = {}  # diagram key -> its element, waiting for place()
        self.keys = set()  # diagram keys given out
        self.changes = {}  # changed-region id -> ("insertion", author) or ("deletion", its Hidden)
        self.ins = None  # (id, author) of the insertion the walk is inside

    def hide(self, blocks):
        """Hidden copies of blocks; the originals may be shared with a cached object."""
        out = []
        for b in blocks:
            c = copy.copy(b)
            c.hidden = "hidden"
            if id(b) in self.wt:
                self.wt[id(c)] = (c, self.wt[id(b)][1])
            out.append(c)
        return out

    def take(self, n, force=False, chars=0) -> bool:
        """Charge n cells and `chars` characters of copied text to the document budgets; False (and counted) once
        either is spent. A forced charge (the first copy of something the file spells out) always succeeds."""
        if force:
            self.left = max(0, self.left - n)
            return True
        if n > self.left:
            self.left = 0
            self.ctx.skipped["cells"] += 1
            return False
        if chars > self.chars_left:
            self.ctx.skipped["cells"] += 1
            return False
        self.left -= n
        self.chars_left -= chars
        return True

    def weight(self, blocks) -> int:
        """Cells the blocks expand to when rendered: 1 plus every nested table's weight."""
        w = 1
        for b in blocks:
            if isinstance(b, Table):
                w += self.wt.get(id(b), (b, 1))[1]
            elif isinstance(b, (Section, _Slot)):
                w += self.weight(b.blocks)
        return w

    # ---- package
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    @staticmethod
    def safe(href):
        """A package-internal member path for href, or None for external, absolute and escaping targets."""
        h = (href or "").strip()
        if not h or h[0] in "/#" or "\\" in h or re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", h):
            return None
        name = resolve("content.xml", h)
        return None if name.startswith("..") or name == "." else name

    def data(self, name, kind):
        if name not in self.cache:
            try:
                self.cache[name] = self.pkg.read(name)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):  # an image or object over a limit is skipped
                    raise
                self.cache[name] = None
        if not self.cache[name]:
            self.ctx.skipped[kind] += 1
        return self.cache[name]

    # ---- inlines
    def is_bold(self, name):
        if name not in self.bold_of:
            v = self.styles.prop(name, "text", "font-weight") if name else None
            self.bold_of[name] = v == "bold" or _int(v, 0) >= 600
        return self.bold_of[name]

    def inlines(self, el, d=0):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        self._text(el.text, out)
        for c in el:
            ins = self.ins
            if ins is not None and local(c.tag) in _LEAVES:
                sub = []
                self._child(c, sub, d)
                for x in sub:
                    _join(out, Inserted([x], ins[1]))
            else:
                self._child(c, out, d)
            self._text(c.tail, out)
        return out

    def _text(self, s, out):
        if s:
            t = Text(re.sub(r"[ \t\r\n]+", " ", s))
            _join(out, Inserted([t], self.ins[1]) if self.ins is not None else t)

    def _child(self, c, out, d):
        n = local(c.tag)
        if self.glued is not None and n in SHAPES and (slot := self.member(c, d)) is not None:
            self.extras.append(slot)
        elif n == "span":
            sub = self.inlines(c, d + 1)
            if sub and self.is_bold(attr(c, "style-name")):
                _join(out, _lift(lambda xs: Bold([y for x in xs for y in (
                    x.inlines if isinstance(x, Bold) and not x.hidden else [x])]), sub))
            else:
                for x in sub:
                    _join(out, x)
        elif n == "a":
            sub = self.inlines(c, d + 1)
            href = (attr(c, "href") or "").strip()[:2000]
            if href and not href.startswith("#"):
                _join(out, _lift(lambda xs: Link(href, xs), sub))
            else:
                for x in sub:
                    _join(out, x)
        elif n == "change-start":
            got = self.changes.get(attr(c, "change-id"))
            if got is not None and got[0] == "insertion":
                self.ins = (attr(c, "change-id"), got[1])
        elif n == "change-end":
            if self.ins is not None and self.ins[0] == attr(c, "change-id"):
                self.ins = None
        elif n == "change":
            got = self.changes.pop(attr(c, "change-id"), None)
            if got is not None and got[0] == "deletion":
                out.append(got[1])
        elif n == "s":
            out.append(Text(" " * min(max(_int(attr(c, "c"), 1), 1), _SPACES)))
        elif n == "tab":
            out.append(Text("\t"))
        elif n == "line-break":
            out.append(Break())
        elif n == "note":
            self.note(c, out, d)
        elif n == "annotation":
            self.annotation(c, out, d)
        elif n == "ruby":
            for b in kids(c, "ruby-base"):
                for x in self.inlines(b, d + 1):
                    _join(out, x)
            reading = _squash(" ".join("".join(t.itertext()) for t in kids(c, "ruby-text")))
            if reading:
                self._text(f"({reading})", out)
        elif n == "hidden-text":
            out.append(Hidden([Text(_squash(attr(c, "string-value") or "".join(c.itertext())))]))
        elif n == "frame":
            self.frame(c, d)
        elif n not in _DROP:
            for x in self.inlines(c, d + 1):
                _join(out, x)

    def note(self, el, out, d):
        key = "en" if attr(el, "note-class") == "endnote" else "fn"
        self.counts[key] += 1
        nid = f"{key}-{self.counts[key]}"
        body = kids(el, "note-body")
        self.doc.notes[nid] = self.blocks(body[0], d + 1) if body else []
        out.append(NoteRef(nid))

    def annotation(self, el, out, d):
        self.counts["cm"] += 1
        nid = f"cm-{self.counts['cm']}"
        who = next((_squash("".join(k.itertext()), 64) for k in kids(el, "creator")), "")
        prefix = f"Comment by {who}: " if who else "Comment: "
        blocks = self.blocks(el, d + 1)
        if blocks and isinstance(blocks[0], Para):
            blocks[0].inlines.insert(0, Text(prefix))
        else:
            blocks.insert(0, Para([Text(prefix.rstrip())]))
        self.doc.notes[nid] = blocks
        out.append(NoteRef(nid))

    # ---- frames and objects
    def frame_blocks(self, fr, d):
        saved, self.extras = self.extras, []
        try:
            self.frame(fr, d)
            return self.extras
        finally:
            self.extras = saved

    def frame(self, fr, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        alt = ""
        for tag in ("title", "desc"):
            alt = next((t for k in kids(fr, tag) if (t := _squash("".join(k.itertext())))), "")
            if alt:
                break
        found = False
        for k in fr:
            if local(k.tag) in ("object", "object-ole"):
                blocks = self.obj(k, d + 1)
                self.extras.extend(blocks)
                found = found or bool(blocks)
        if found:
            return
        for k in fr:
            n = local(k.tag)
            if n == "image":
                name = self.safe(attr(k, "href"))
                if name is None or not self.pkg.has(name):
                    self.ctx.skipped["image"] += 1
                    continue
                data = self.data(name, "image")
                if data:
                    self.extras.append(Image(data, alt))
            elif n == "text-box":
                self.extras.extend(self.blocks(k, d + 1))
            elif n == "table":  # an Impress table sits directly in its frame
                t = self.table(k, d + 1)
                if t:
                    self.extras.append(t)
            elif n == "frame":
                self.frame(k, d + 1)

    def obj(self, el, d):
        """The blocks of a draw:object or draw:object-ole, read once a target. A folder of the package that holds a
        content.xml is an OpenDocument object: a formula, a chart, or a text or presentation read whole as a document
        (embedded.document) from that folder (`_Folder`). A member file is another application's object, read by
        embedded.read: its equation, its tables or its document; a chart left unread is counted under chart, anything
        else under object, and the frame then shows its replacement picture."""
        href = attr(el, "href")
        if not href:
            for c in el:
                if local(c.tag) in ("document", "math"):
                    return self.object_blocks(c, d)
            return []
        base = self.safe(href)
        name = base + "/content.xml" if base else None
        if name is None or not (self.pkg.has(name) or self.pkg.has(base)):
            self.ctx.skipped["object"] += 1
            return []
        if base in self.objects:  # a reused object costs what it expands to
            blocks = self.objects[base]
            return blocks if self.take(self.weight(blocks), chars=blocks_len(blocks)) else []
        self.objects[base] = []  # also breaks an object that refers to itself
        objs = embedded.of(self.ctx)
        if objs.count >= embedded.MAX_OBJECTS or objs.late():
            self.ctx.skipped["object"] += 1
            return []
        objs.count += 1
        if not self.pkg.has(name):
            self.objects[base] = self.foreign(base)
            return self.objects[base]
        data = self.data(name, "object")
        if data:
            try:
                with objs.timed(), charged(objs):
                    self.objects[base] = self.object_blocks(parse_xml(data), d, base)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.ctx.skipped["object"] += 1
        return self.objects[base]

    def foreign(self, name) -> list:
        """The blocks of member `name`, another application's object (embedded.read)."""
        data = self.data(name, "object")
        objs = embedded.of(self.ctx)
        got = embedded.read(data, self.ctx, objs.budget()) if data else None
        kind = got[0] if got else ""
        if kind == "equation":
            if got[2]:
                self.ctx.skipped["depth" if got[2] <= {"depth"} else "object"] += 1
            return [Equation(got[1])]
        placed = objs.place(got) if kind in ("table", "document") else None
        if placed is not None:
            return placed
        if data:
            self.ctx.skipped["chart" if kind in ("chart", "table") else "object"] += 1
        return []

    def object_blocks(self, root, d, folder=None):
        """The blocks of an OpenDocument object whose content root is `root`: a formula, a chart, or (with the
        package `folder` that holds it) a text or presentation read whole."""
        if local(root.tag) == "math":
            tex = ""
            for a in root.iter():
                if local(a.tag) == "annotation" and (attr(a, "encoding") or "").startswith("StarMath"):
                    tex = _squash("".join(a.itertext()), 2000)
                    break
            tex = tex or _squash(" ".join(t.strip() for t in root.itertext() if t.strip()), 2000)
            return [Equation(tex)] if tex else []
        for body in kids(root, "body"):
            for c in body:
                if local(c.tag) == "chart":
                    return self.chart(c, d, root)
                if local(c.tag) in ("text", "presentation") and folder is not None:
                    from . import odf
                    what = "OpenDocument text" if local(c.tag) == "text" else "OpenDocument presentation"
                    view = _Folder(self.pkg, folder + "/")
                    got = embedded.document(lambda own: odf.read_package(view, own), self.ctx, what)
                    if got is not None:
                        return got[1]
                break
        self.ctx.skipped["object"] += 1
        return []

    def chart(self, ch, d, root):
        """A chart object as its local table under charts.caption: chart:class (circle a pie, ring a doughnut), the
        plot area's chart:vertical (horizontal bars; vertical ones by default), chart:stacked, chart:percentage and
        chart:three-dimensional, the title, and each axis title by its chart:dimension, x and y trading places when
        the bars are horizontal."""
        ch = next(iter(kids(ch, "chart")), ch)  # office:chart wraps chart:chart
        cls = (attr(ch, "class") or "").replace("chart:", "", 1)[:64]
        title = next((_squash("".join(t.itertext()), 100) for t in kids(ch, "title")), "")
        plot = next(iter(kids(ch, "plot-area")), None)
        props = _chart_props(root, attr(plot, "style-name") if plot is not None else None)
        across = props.get("vertical") == "true"
        on = {k for k, v in props.items() if v == "true"}
        grouping = "percent" if "percentage" in on else "stacked" if "stacked" in on else ""
        name = kind(_CHART_KINDS.get(cls, cls), across, grouping, "three-dimensional" in on) if cls else ""
        axes = []
        for ax in kids(plot, "axis") if plot is not None else ():
            dim = attr(ax, "dimension") or ""
            text = next((_squash("".join(t.itertext()), 100) for t in kids(ax, "title")), "")
            if dim in ("x", "y", "z") and text:
                axes.append((({"x": "y", "y": "x"}.get(dim, dim)) if across else dim, text))
        cap = caption(name, title, axes)
        src = first(ch, "table")
        tbl = self.table(src, d + 1) if src is not None else None
        if tbl is None:
            tbl = Para([Text(cap)])
        else:
            tbl.caption = cap
        self.charts[id(tbl)] = tbl
        return [tbl]

    # ---- an odt body as one diagram scope
    def scope(self, office):
        """Read the body as one diagram scope when it holds a connector or an arrowed line; else nothing changes."""
        glued, found = set(), False
        for el in office.iter():  # the parsed tree's elements, once each
            if local(el.tag) in ("connector", "line") and edge_like(el, self.styles):
                found = True
                glued.update(v for v in (attr(el, "start-shape"), attr(el, "end-shape")) if v)
        if found:
            self.glued = glued

    def member(self, el, d):
        """A connector, an arrowed line, or the first shape of an id a connector glues: its blocks wait under its key
        and the returned _Slot marks its place. None for anything else, which reads as before."""
        own = _own_id(el)
        if not edge_like(el, self.styles) and (own is None or own in self.keys or own not in self.glued):
            return None
        key = shape_key(el, self.keys)
        saved, self.glued = self.glued, None  # what a member holds reads as before
        try:
            blocks = self.frame_blocks(el, d) if local(el.tag) == "frame" else self.blocks(el, d + 1)
        finally:
            self.glued = saved
        self.members[key] = el
        return _Slot(key, blocks)

    def swap(self, fn):
        self.doc.blocks = _swap(self.doc.blocks, fn)
        for k, v in self.doc.notes.items():
            self.doc.notes[k] = _swap(v, fn)

    def own(self, s) -> list:
        """A slot's member blocks, hidden when the slot was."""
        return self.hide(s.blocks) if s.hidden else s.blocks

    def place(self, budget):
        """Write the scope: each _Slot gives way to its member's blocks when the diagram keeps them (hidden as the slot
        was), and the diagram goes at the first slot that anchors it, or at the end when none does. On any failure
        every slot left gives way to its member's blocks, so no text is lost."""
        self.glued = None
        try:
            self._place(budget)
        except Exception:
            self.swap(self.own)
            raise

    def _place(self, budget):
        dia, slots, index = diagram.Diagram(self.ctx, budget), [], {}

        def collect(s):  # flow order; a member in hidden content is a hidden shape or connector
            if s.key not in index and s.key in self.members:
                index[s.key] = len(slots)
                with self.ctx.guard("shape"):
                    register(dia, self.styles, self.members[s.key], s.key, s.blocks, bool(s.hidden))
                slots.append((s.key, [object()]))
            return [s]

        self.swap(collect)
        flow = [m for _, marks in slots for m in marks]
        with self.ctx.guard("shape"):
            flow = dia.place(slots)
        mark = {id(marks[0]): i for i, (_, marks) in enumerate(slots)}
        kept, made, at, last = set(), [], len(slots), -1
        for b in flow:  # the kept marks in order, the diagram's blocks just before the anchor's slot
            i = mark.get(id(b))
            if i is not None:
                kept.add(i)
                last = i
            else:
                if not made:
                    at = last + 1
                made.append(b)
        todo = [made]

        def put(s):
            i, out = index.get(s.key), []
            if i is not None and i == at and todo:
                out += todo.pop()
            if i in kept:
                out += self.own(s)
            return out

        self.swap(put)
        if todo:
            self.doc.blocks.extend(todo.pop())

    # ---- blocks
    def blocks(self, el, d, aside=None):
        """The blocks of el's children; with `aside`, the blocks of a frame right under el (a shape anchored to a sheet
        cell) that holds only charts go there instead."""
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        for c in el:
            if self.ctx.late():
                break
            if aside is not None and local(c.tag) == "frame":
                got = self.frame_blocks(c, d)
                (aside if got and all(id(b) in self.charts for b in got) else out).extend(got)
            else:
                out.extend(self.block(c, d))
        return out

    def block(self, c, d):
        n = local(c.tag)
        if self.glued is not None and n in SHAPES and (slot := self.member(c, d)) is not None:
            return [slot]
        if n in ("p", "h"):
            return self.para(c, d)
        if n == "list":
            return self.list(c, d)
        if n == "table":
            t = self.table(c, d)
            return [t] if t else []
        if n == "section":
            inner = self.blocks(c, d + 1)
            if attr(c, "display") != "none":
                return inner
            return [Section("chapter", blocks=self.hide(inner), hidden="hidden")] if inner else []
        if n == "tracked-changes":
            self.tracked(c, d)
            return []
        if n in _CONTAINERS:
            return self.blocks(c, d + 1)
        if n == "frame":
            return self.frame_blocks(c, d)
        return []

    def para(self, p, d):
        saved, self.extras = self.extras, []
        try:
            inl = self.inlines(p, d + 1)
            extras = self.extras
        finally:
            self.extras = saved
        if local(p.tag) == "h":
            level = min(max(_int(attr(p, "outline-level"), 1), 1), 6)
            main = Heading(level, inl)
        else:
            main = Para(inl)
        out = ([main] if inl else []) + extras
        if any(local(c.tag) == "hidden-paragraph" for c in p):
            out = self.hide(out)
        return out

    def tracked(self, tc, d):
        """The changed regions, by id, for the change marks of the text that follows: an insertion's author, and a
        deletion's text as a Hidden of kind "deleted" with its author, its paragraphs joined by spaces."""
        for reg in kids(tc, "changed-region"):
            rid = attr(reg, "id")
            for ch in reg:
                kind = local(ch.tag)
                if kind not in ("insertion", "deletion"):
                    continue
                who = next((_squash("".join(k.itertext()), 64) for info in kids(ch, "change-info")
                            for k in kids(info, "creator")), "")
                if kind == "insertion":
                    self.changes[rid] = (kind, who)
                    continue
                inl = []
                for k in ch:
                    if local(k.tag) == "change-info":
                        continue
                    if inl:
                        inl.append(Text(" "))
                    inl.extend(self.inlines(k, d + 1) if local(k.tag) in ("p", "h") else
                               [Text(_squash("".join(k.itertext()), 10_000))])
                self.changes[rid] = (kind, Hidden(inl, "deleted", who))

    # ---- lists
    def list(self, lst, d):
        cnt = {}
        ref = attr(lst, "continue-list")
        if ref in self.ids:
            cnt = self.ids[ref]
        elif attr(lst, "continue-numbering") == "true" and self.last is not None:
            cnt = self.last
        out = self.items(lst, d, 0, attr(lst, "style-name"), cnt)
        self.last = cnt
        if attr(lst, "id"):
            self.ids[attr(lst, "id")] = cnt
        return out

    def items(self, lst, d, level, style, cnt):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        style = attr(lst, "style-name") or style
        out = []
        for it in lst:
            n = local(it.tag)
            if n == "list-header":
                out.extend(self.blocks(it, d + 1))
            elif n == "list-item":
                for k in [k for k in cnt if k > level]:
                    del cnt[k]
                head = next(iter(it), None)  # only an item that opens with a paragraph is numbered
                marker = (self.marker(style, level, cnt, attr(it, "start-value"))
                          if head is not None and local(head.tag) == "p" else None)
                for c in it:
                    cn = local(c.tag)
                    if cn == "list":
                        out.extend(self.items(c, d + 1, level + 1, style, cnt))
                    elif c is head and marker is not None:
                        got = self.para(c, d + 1)
                        if got and isinstance(got[0], Para):
                            got[0] = ListItem(min(level, 6), marker, got[0].inlines, hidden=got[0].hidden)
                        out.extend(got)
                    else:
                        out.extend(self.block(c, d + 1))
        return out

    def marker(self, style, level, cnt, start_value):
        lv = self.styles.list_level(style, level + 1)
        fmt = attr(lv, "num-format") if lv is not None else None
        if not fmt:
            return "-"
        n = cnt[level] + 1 if level in cnt else _int(attr(lv, "start-value"), 1)
        if start_value is not None:
            n = _int(start_value, n)
        cnt[level] = n
        pre, suf = (attr(lv, "num-prefix") or "")[:16], (attr(lv, "num-suffix") or "")[:16]
        return (pre + _fmt(n, fmt) + (suf if pre or suf else "."))[:64]

    # ---- tables
    def table(self, tbl, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return None
        rows, cut = [], []
        self.rows_of(tbl, d, False, rows, cut)
        while rows and all(_empty(c.blocks) for c in rows[-1]):
            rows.pop()
        self.count_cut(cut, len(rows))
        if not rows:
            return None
        for r, row in enumerate(rows):
            for cell in row:
                cell.rowspan = min(cell.rowspan, len(rows) - r)
        t = Table(rows)
        self.wt[id(t)] = (t, sum(self.weight(c.blocks) for row in rows for c in row) or 1)
        return t

    def rows_of(self, cont, d, header, rows, cut):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        for c in cont:
            if self.left <= 0:
                self.ctx.skipped["cells"] += 1
                return
            n = local(c.tag)
            if n == "table-row":
                cells = self.row(c, d, header)
                empty = all(_empty(x.blocks) for x in cells)
                rep, cut_here = self.repeat(attr(c, "number-rows-repeated"), empty)
                rw = sum(self.weight(x.blocks) for x in cells) or 1
                rc = sum(blocks_len(x.blocks) for x in cells)
                for i in range(rep):
                    if not self.take(rw if i else (0 if cells else 1), force=i == 0, chars=rc):
                        return
                    rows.append([Cell(x.blocks, x.rowspan, x.colspan, x.header) for x in cells])
                if cut_here:
                    cut.append((len(rows), empty))
            elif n in ("table-header-rows", "table-rows", "table-row-group"):
                self.rows_of(c, d + 1, header or n == "table-header-rows", rows, cut)

    @staticmethod
    def repeat(raw, empty):
        """(copies to expand, whether the repeat was cut short)."""
        rep, cap = max(1, _int(raw, 1)), _GAP if empty else _REPEAT
        return min(rep, cap), rep > cap

    def count_cut(self, cut, kept):
        """Count truncated repeats that still show: content, or a gap followed by more content."""
        for end, empty in cut:
            if not empty or end < kept:
                self.ctx.skipped["repeat"] += 1

    def row(self, tr, d, header):
        cells, cut = [], []
        for c in tr:
            if local(c.tag) != "table-cell":
                continue  # covered cells carry no content; the spans of their origin cover them
            blocks = self.blocks(c, d + 1)
            cs = min(max(_int(attr(c, "number-columns-spanned"), 1), 1), _SPAN)
            rs = min(max(_int(attr(c, "number-rows-spanned"), 1), 1), _SPAN)
            w, size = self.weight(blocks), blocks_len(blocks)
            n, cut_here = self.repeat(attr(c, "number-columns-repeated"), _empty(blocks))
            for i in range(n):
                if not self.take(w if i else 1, force=i == 0, chars=size):  # a first copy was charged as built
                    return cells
                cells.append(Cell(blocks, rs, cs, header))
            if cut_here:
                cut.append((len(cells), _empty(blocks)))
        while cells and _empty(cells[-1].blocks):
            cells.pop()
        self.count_cut(cut, len(cells))
        return cells

    # ---- headers and footers
    def master(self, styles_root):
        for mp in (e for e in styles_root.iter() if local(e.tag) == "master-page"):
            for el in mp:
                n = local(el.tag)
                kind = "headers" if n.startswith("header") else "footers" if n.startswith("footer") else None
                if kind and (text := _flat(self.blocks(el, 0))):
                    getattr(self.doc, kind).append(text)


def para_inlines(el, styles, pkg, ctx, body=None) -> list:
    """Inlines of one paragraph-like element; a caller that needs its notes and frames passes its Body."""
    return (body or Body(pkg, ctx, styles)).inlines(el)
