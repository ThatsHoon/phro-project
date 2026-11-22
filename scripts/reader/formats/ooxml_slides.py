"""PresentationML (pptm, ppsx) to the document model. Elements match by local name.

Run hyperlinks and legacy slide comments are ported from dochan (`ooxml/pptx.py`); a14 math goes through `omml`.
Comments (legacy `p:cm`, modern `p188:cm` with replies) become the Section's `comments`, one note line each.
A placeholder with no box of its own takes its layout's (by idx, then type), then its master's. A paragraph's bullet
comes from the first of its own pPr, its shape's lstStyle, its layout's and master's placeholder lstStyle and the
master's txStyles (title, body or other) that names one; `buNone` there makes it a paragraph.
The shapes read in `deck.Order` over their boxes, a group's members inside the group. The notes are every shape of the
notes page, its pictures too, but its slide image, number, date, header and footer placeholders. What is not a placeholder on the master
and the layout (unless the slide or the layout sets showMasterSp="0"), and the slide's footer and header text, go to
`deck.Repeated`. A shape's or picture's a:hlinkClick wraps its text in the link (an external URL) or follows it as
`(→ Slide 2)` (a slide or show jump); a textless one is named (its action button, its alt text) on a line of its own.
A picture that is a video or audio clip is named on a line before its poster.
Each slide is one diagram scope: every shape (titles, left-out placeholders and textless ones too) and connector
(`p:cxnSp`, or an `sp` whose preset is a line) goes to `diagram.Diagram` under its `cNvPr@id`, in slide EMU.
"""
import posixpath
import re

from .. import diagram, embedded, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Equation, Link, ListItem, Math, Para, Section, Table, Text, Image
from . import deck, omml, ooxml_base, smartart
from .drawingml import Xf, _prst as preset, arrows, glue, is_line, kind, rule
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
_FOOTERS = frozenset({"ftr", "hdr"})
_OTHER_PH = frozenset({"sldNum", "dt", "ftr", "hdr"})
_SHAPES = frozenset({"sp", "pic", "graphicFrame", "cxnSp"})
_BULLETS = frozenset({"buNone", "buAutoNum", "buChar", "buBlip"})
_JUMPS = {"nextslide": "next slide", "previousslide": "previous slide", "firstslide": "first slide",
          "lastslide": "last slide", "lastslideviewed": "last slide viewed", "endshow": "end of show"}
_BUTTONS = {"actionbuttonforwardnext": "Next", "actionbuttonbackprevious": "Previous", "actionbuttonbeginning": "First",
            "actionbuttonend": "Last", "actionbuttonhome": "Home", "actionbuttonreturn": "Return",
            "actionbuttoninformation": "Information", "actionbuttonhelp": "Help", "actionbuttondocument": "Document",
            "actionbuttonsound": "Sound", "actionbuttonmovie": "Movie", "actionbuttonblank": ""}
_MEDIA = {"videoFile": ("Video", "link"), "quickTimeFile": ("Video", "link"), "audioFile": ("Audio", "link"),
          "wavAudioFile": ("Audio", "embed")}


def _rid(el, name="id"):
    """A relationship attribute of an element: the namespaced one of that local name (sldId also carries a plain
    numeric id)."""
    for k, v in el.attrib.items():
        if "}" in k and local(k) == name:
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


def _alt(cnv) -> str:
    return re.sub(r"\s+", " ", (attr(cnv, "descr") if cnv is not None else "") or "").strip()[:MAX_ALT]


def _box_of(entry):
    """The box of a walked entry: a group's is the union of its members' (its own when none has one)."""
    box, _, _, members = entry
    if members is None:
        return box
    got = [b for b in map(_box_of, members) if b is not None]
    if not got:
        return box
    return (min(b[0] for b in got), min(b[1] for b in got), max(b[2] for b in got), max(b[3] for b in got))


def _reading_order(out, order) -> list:
    """(key, blocks) of the walked (box, key, blocks, members) entries in `deck.Order`, a group's members read in
    the group's place."""
    got = []
    for i in order([_box_of(e) for e in out]):
        _, key, blocks, members = out[i]
        if members is None:
            got.append((key, blocks))
        else:
            got.extend(_reading_order(members, order))
    return got


class _Scope:
    def __init__(self, part, nsmap):
        self.part, self.nsmap = part, nsmap


class _Part:
    """A layout or master: its placeholders by ("idx", i) and ("type", t), its txStyles by name, its showMasterSp."""

    def __init__(self, name, root):
        self.name, self.phs, self.styles = name, {}, {}
        self.show_master = root is None or attr(root, "showMasterSp") not in ("0", "false")
        csld = _kid(root, "cSld") if root is not None else None
        tree = _kid(csld, "spTree") if csld is not None else None
        for el in (tree if tree is not None else ()):
            _, ph = _nv(el)
            if ph is None:
                continue
            if attr(ph, "idx") is not None:
                self.phs.setdefault(("idx", attr(ph, "idx")), el)
            self.phs.setdefault(("type", attr(ph, "type") or "body"), el)
        tx = _kid(root, "txStyles") if root is not None else None
        for c in (tx if tx is not None else ()):
            self.styles.setdefault(local(c.tag), c)


class _Reader:
    def __init__(self, pkg, part, ctx):
        self.pkg, self.part, self.ctx = pkg, part, ctx
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0
        self.objects = embedded.Parts(pkg, ctx)
        self.smartart_nodes = 0
        self.authors = {}  # comment author id -> name, legacy and modern merged
        self.comment_count = 0
        self.cut = False
        self.parts = {}
        self.inherit = []
        self.mode = "slide"
        self.footers = []
        self.furniture = {}
        self.numbers = {}
        self.repeated = deck.Repeated()
        self.order = deck.Order()
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
            if rel is not None and not rel.external:
                self.numbers.setdefault(rel.target, number)
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
        doc.blocks[:0] = self.repeated.place(doc.blocks)
        return doc

    def slide(self, name, number) -> Section:
        root = self.open(name)
        if local(root.tag) != "sld":
            raise ReadError("damaged", "not a slide")
        sec = Section("slide", number=number, hidden="hidden" if attr(root, "show") in ("0", "false") else "")
        tree = _kid(_kid(root, "cSld"), "spTree") if _kid(root, "cSld") is not None else None
        out, titles = [], []
        layout = self.related(name, "/slideLayout")
        master = self.related(layout, "/slideMaster") if layout else None
        layout, master = (self.part_of(p) if p else None for p in (layout, master))
        self.inherit = [p for p in (layout, master) if p is not None]
        self.mode, self.footers = "slide", []
        self.dia, self.keys = diagram.Diagram(self.ctx, self.budget), set()
        if tree is not None:
            self.walk(tree, Xf(), 0, False, out, titles)
        slots = _reading_order(out, self.order)
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = self.dia.place(slots)
        sec.blocks.extend(flow)
        sec.title = re.sub(r"\s+", " ", " ".join(titles)).strip()[:MAX_TITLE]
        if attr(root, "showMasterSp") not in ("0", "false"):
            shown = [master] if master is not None and (layout is None or layout.show_master) else []
            for part in shown + ([layout] if layout is not None else []):
                for blocks in self.shapes_of(part):
                    self.repeated.add(number, blocks)
        for blocks in self.footers:
            self.repeated.add(number, blocks)
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

    # ---- layouts and masters
    def related(self, part, suffix):
        for rel in ooxml_base.rels(self.pkg, part, self.ctx).values():
            if not rel.external and rel.type.endswith(suffix) and self.pkg.has(rel.target):
                return rel.target
        return None

    def part_of(self, name):
        """The _Part of a layout or master, cached; one that cannot be read has no placeholder and counts `master`."""
        if name not in self.parts:
            try:
                root, _ = ooxml_base.load(self.pkg, name)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.ctx.skipped["master"] += 1
                root = None
            self.parts[name] = _Part(name, root)
        return self.parts[name]

    def matches(self, ph):
        """The placeholders a slide placeholder inherits from, layout first: each part's by idx, else by type."""
        idx, kind = attr(ph, "idx"), attr(ph, "type") or "body"
        got = []
        for part in self.inherit:
            el = part.phs.get(("idx", idx)) if idx is not None else None
            if el is None:
                el = next((part.phs[("type", t)] for t in (kind, *_PH_ALIAS.get(kind, ())) if ("type", t) in part.phs),
                          None)
            if el is not None:
                got.append(el)
        return got

    def inherited(self, ph):
        """Slide box of a placeholder from the first part it inherits from that gives one; None when none does."""
        return next((b for b in (Xf().box(_xfrm(el)) for el in self.matches(ph)) if b is not None), None)

    def styles(self, ph, body):
        """The list styles a shape's paragraphs inherit, nearest first: its own lstStyle, and for a placeholder its
        layout's and master's placeholder lstStyle and the master's title, other or body txStyle."""
        own = _kid(body, "lstStyle") if body is not None else None
        got = [own] if own is not None else []
        if ph is None:
            return got
        for el in self.matches(ph):
            tx = _kid(el, "txBody")
            lst = _kid(tx, "lstStyle") if tx is not None else None
            if lst is not None:
                got.append(lst)
        kind = attr(ph, "type") or "body"
        name = "titleStyle" if kind in _TITLES else "otherStyle" if kind in _OTHER_PH else "bodyStyle"
        got.extend(p.styles[name] for p in self.inherit if name in p.styles)
        return got

    def shapes_of(self, part):
        """[blocks] of the shapes of a layout or master that are not placeholders, in reading order; read once a part,
        under its own relationships. One that cannot be read counts `master`."""
        if part.name not in self.furniture:
            self.furniture[part.name] = []
            saved = self.scope, self.dia, self.keys, self.inherit, self.mode
            try:
                with self.ctx.guard("master"):
                    root = self.open(part.name)
                    csld = _kid(root, "cSld")
                    tree = _kid(csld, "spTree") if csld is not None else None
                    self.dia, self.keys = diagram.Diagram(self.ctx, self.budget), set()
                    self.inherit, self.mode = [], "master"
                    out = []
                    if tree is not None:
                        self.walk(tree, Xf(), 0, False, out, [])
                    self.furniture[part.name] = [blocks for _, blocks in _reading_order(out, self.order)]
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.ctx.skipped["master"] += 1
            finally:
                self.scope, self.dia, self.keys, self.inherit, self.mode = saved
        return self.furniture[part.name]

    def notes_part(self, name):
        for rel in ooxml_base.rels(self.pkg, name, self.ctx).values():
            if rel.external or not rel.type.endswith("/notesSlide"):
                continue
            if not self.pkg.has(rel.target):
                self.ctx.skipped["notes"] += 1
                return []
            saved = self.dia, self.keys, self.inherit, self.mode
            try:
                with self.ctx.guard("notes"):
                    root = self.open(rel.target)
                    tree = _kid(_kid(root, "cSld"), "spTree") if _kid(root, "cSld") is not None else None
                    self.dia, self.keys = diagram.Diagram(self.ctx, self.budget), set()
                    self.inherit, self.mode = [], "notes"
                    out = []
                    if tree is not None:
                        self.walk(tree, Xf(), 0, False, out, [])
                    return [b for _, blocks in _reading_order(out, self.order) for b in blocks]
            except ReadError as e:  # a member of an open package: any kind but a timeout skips this part
                if e.kind == "timeout":
                    raise
                self.ctx.skipped["notes"] += 1
            finally:
                self.dia, self.keys, self.inherit, self.mode = saved
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
        if ph is not None and self.mode == "master":
            return
        hid = hidden or (cnv is not None and attr(cnv, "hidden") in ("1", "true"))
        if n == "grpSp":
            members = []
            self.walk(el, xf.group(_xfrm(el)), d + 1, hid, members, titles)
            if members:
                out.append((xf.box(_xfrm(el)), None, None, members))
            return
        key, x, sppr = self.key(cnv), _xfrm(el), _kid(el, "spPr")
        ptype = (attr(ph, "type") or "body") if ph is not None else None
        body = _kid(el, "txBody") if n in ("sp", "cxnSp") else None
        box = xf.box(x)
        if ptype in _SKIP_PH:  # left out of the flow; a connector may still end on it
            text = self._body_text(body) if body is not None else ""
            self.dia.shape(key, [Para([Text(text)])] if text else [], kind(sppr), box, hid)
            if ptype in _FOOTERS and text and not hid and self.mode == "slide":
                self.footers.append(self.text_blocks(body))
            return
        if box is None and ph is not None:
            box = self.inherited(ph)
        url, jump = self.target(_kid(cnv, "hlinkClick") if cnv is not None else None)
        if n in ("sp", "cxnSp"):
            blocks = self.text_blocks(body, styles=self.styles(ph, body))
            if ptype in _TITLES and not hid:  # the slide's heading: its text names the node, it has no slot
                text = " ".join(_flat(getattr(b, "inlines", [])) for b in blocks)
                if text.strip():
                    titles.append(text)
                eqs = [b for b in blocks if isinstance(b, Equation)]
                self.dia.shape(key, [b for b in blocks if not isinstance(b, Equation)], kind(sppr), xf.box(x))
                if eqs:
                    out.append((box, None, eqs, None))
                return
            if (url or jump) and n == "sp":
                button = _BUTTONS.get(preset(sppr))
                label = f"{button} button" if button else "Button" if button is not None else _alt(cnv) or "Link"
                blocks = deck.linked(blocks, url, jump, label)
        elif n == "pic":
            blocks = self.picture(el, cnv, url, jump)
        else:
            blocks = self.frame(el)
            if url or jump:
                blocks = deck.linked(blocks, url, jump, _alt(cnv) or "Link")
        if n == "cxnSp" or (n == "sp" and is_line(sppr)):
            p0, p1 = xf.ends(x) or (None, None)
            st, en = glue(el)
            self.dia.connector(key, st, en, p0, p1, arrows(sppr), line=n != "cxnSp" or rule(sppr, (st, en)),
                               hidden=hid, blocks=blocks)
        else:
            self.dia.shape(key, blocks, kind(sppr), box, hid)
        if hid:
            for b in blocks:
                b.hidden = "hidden"
        if blocks:
            out.append((box, key, blocks, None))

    def key(self, cnv):
        """A shape's diagram key: its cNvPr id, or a key no glue names when it has none or repeats one on the slide."""
        k = attr(cnv, "id") if cnv is not None else None
        if k is None or k in self.keys:
            k = ("unkeyed", len(self.keys))
        self.keys.add(k)
        return k

    def picture(self, el, cnv, url="", jump=""):
        """A picture's Image and the line its click makes, after a line naming its clip when it is one."""
        out = []
        blip = next((e for e in el.iter() if local(e.tag) == "blip"), None)
        if blip is not None:
            img = ooxml_base.image(self.pkg, self.scope.part, attr(blip, "embed"))
            if img is not None:
                self.image_bytes += len(img.data)
            if img is None or self.image_bytes > IMAGE_BYTES:
                self.ctx.skipped["image"] += 1
            else:
                out.append(Image(img.data, _alt(cnv)))
        clip = self.media(el, cnv, bool(out))
        if url or jump:
            out = deck.linked(out, url, jump, _alt(cnv) or "Picture")
        return clip + out

    def media(self, el, cnv, poster):
        """[one Para "Video: name"] for a picture whose nvPr holds a video or audio file (or only p14:media), else []:
        the name is its cNvPr name, else its file's; a file outside the package is linked."""
        nvpr = next((e for e in el.iter() if local(e.tag) == "nvPr"), None)
        found = None
        for c in (nvpr.iter() if nvpr is not None else ()):
            n = local(c.tag)
            if n in _MEDIA and (found is None or found[0] == "Media"):
                found = (_MEDIA[n][0], _rid(c, _MEDIA[n][1]))
            elif n == "media" and found is None:
                found = ("Media", _rid(c, "embed"))
        if found is None:
            return []
        rel = ooxml_base.rels(self.pkg, self.scope.part, self.ctx).get(found[1] or "")
        target = rel.target[:MAX_URL] if rel is not None else ""
        name = re.sub(r"\s+", " ", (attr(cnv, "name") if cnv is not None else "") or "").strip()[:MAX_ALT]
        name = name or posixpath.basename(target)
        line = [Text(f"{found[0]}: ")]
        line.append(Link(target, [Text(name or target)]) if rel is not None and rel.external and target else Text(name))
        if poster:
            line.append(Text(" (poster below)"))
        return [Para(line)]

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
            if n == "oleObj":
                return self.ole(el, attr(e, "id"))
        return []

    def ole(self, el, rid):
        """An embedded object (p:oleObj) of frame `el`, by embedded.Parts: its equation or its tables; one that is not
        read shows the frame's preview picture and is counted under object, a chart left unread under chart. An
        object the package does not hold (a link) is counted only when it has no preview either."""
        got = self.objects.get(self.scope.part, rid)
        kind = got[0] if got else ""
        if kind == "equation":
            if "depth" in got[2]:
                self.deep()
            if got[2] <= {"depth"}:
                return [Equation(got[1], False)]
        elif kind == "table":
            tables = self.objects.place(got)
            if tables is not None:
                return tables
            kind = "chart"
        blocks = [Equation(got[1], False)] if kind == "equation" else []
        blocks += self.picture(el, next((e for e in el.iter() if local(e.tag) == "cNvPr"), None))
        if kind == "chart":
            self.ctx.skipped["chart"] += 1
        elif kind or not blocks:
            self.ctx.skipped["object"] += 1
        return blocks

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

    def target(self, h):
        """(external URL or "", the words of a jump or "") of an a:hlinkClick: a slide jump names the slide ("Slide 2";
        nothing when the slide is not in the deck), a show jump its move, a macro its name; another action (media, OLE,
        a program, a custom show) gives neither. A relationship that is missing is counted under `link`."""
        if h is None:
            return "", ""
        action = attr(h, "action") or ""
        if action.startswith("ppaction://hlinkshowjump"):
            return "", _JUMPS.get(action.partition("jump=")[2].split("&")[0].casefold(), "")
        if action.startswith("ppaction://macro"):
            name = re.sub(r"\s+", " ", action.partition("name=")[2]).strip()[:MAX_ALT]
            return "", f"macro {name}" if name else "macro"
        if action and not action.startswith(("ppaction://hlinksldjump", "ppaction://hlinkfile",
                                              "ppaction://hlinkpres")):
            return "", ""
        rid = _rid(h)
        if not rid:
            return "", ""
        rel = ooxml_base.rels(self.pkg, self.scope.part, self.ctx).get(rid)
        if rel is None:
            self.ctx.skipped["link"] += 1
            return "", ""
        if rel.external:
            return (rel.target[:MAX_URL], "") if rel.type.endswith("/hyperlink") else ("", "")
        number = self.numbers.get(rel.target) if rel.type.endswith("/slide") else None
        return "", f"Slide {number}" if number else ""

    def inlines(self, p, eqs=None):
        pieces = []
        for c in self.para_children(p):
            n = local(c.tag)
            if n == "br":
                pieces.append((Break(), None))
            elif n in ("r", "fld"):
                t = "".join(x.text or "" for x in kids(c, "t"))
                if not t:
                    continue
                pr = _kid(c, "rPr")
                bold = pr is not None and attr(pr, "b") in ("1", "true")
                pieces.append((Bold([Text(t)]) if bold else Text(t),
                               self.target(_kid(pr, "hlinkClick") if pr is not None else None)))
            elif c.tag == A14_M:
                for m in c:
                    if omml.is_math(m, "oMathPara"):
                        for t in omml.lines(m):
                            if eqs is not None:
                                eqs.append(Equation(t, display=True))
                            else:
                                pieces.append((Math(t), None))
                    elif omml.is_math(m, "oMath"):
                        t = omml.tex(m)
                        if t:
                            pieces.append((Math(t), None))
            elif omml.is_math(c, "oMath"):
                t = omml.tex(c)
                if t:
                    pieces.append((Math(t), None))
        out, k = [], 0
        while k < len(pieces):
            it, click = pieces[k]
            j = k + 1
            if click is None or click == ("", ""):
                out.append(it)
            else:
                while j < len(pieces) and pieces[j][1] == click:
                    j += 1
                run = [q for q, _ in pieces[k:j]]
                if click[0]:
                    out.append(Link(click[0], run))
                else:
                    out.extend(run)
                if click[1]:
                    out.append(Text(f" (→ {click[1]})"))
            k = j
        return out

    def text_blocks(self, body, cell=False, styles=()):
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
            tag = f"lvl{lvl + 1}pPr"
            bullet = None
            for el in [ppr, *(_kid(s, tag) for s in styles)]:
                bullet = next((c for c in (el if el is not None else ()) if local(c.tag) in _BULLETS), None)
                if bullet is not None:
                    break
            marker, name = None, local(bullet.tag) if bullet is not None else ""
            if name == "buAutoNum":
                start = min(max(_int(attr(bullet, "startAt"), 1), 1), 10**6)
                counters[lvl] = counters.get(lvl, start - 1) + 1
                marker = f"{counters[lvl]}."
            elif name in ("buChar", "buBlip"):
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


def parse_slides(pkg, part, ctx) -> Doc:
    return _Reader(pkg, part, ctx).run()
