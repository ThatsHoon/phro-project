"""OpenDocument presentations (.odp): one slide section per draw:page, shapes in reading order.

The shapes read in `deck.Order` over their svg boxes (x, y, width, height; a line's or connector's two ends); a group
or a draw:a is one unit, at the union of its members' boxes, its members ordered inside it. Each slide is one diagram
scope: its shapes and connectors (groups flattened) go to `diagram.Diagram` through `odf_base.register`; titles are
registered without a slot.

A page's master (draw:master-page-name, else the only master) gives it the shapes on the master that are not
placeholders (no title, outline or subtitle class, no presentation:placeholder) unless the page's style sets
background-objects-visible false, and, when its style sets display-footer, the footer: the text of the
presentation:footer-decl its use-footer-name names, else the master's footer frame's own text. They go to
`deck.Repeated`: written once, on the page when only one shows them.

A shape inside a draw:a links to its href, and a dom:click presentation:event-listener names its move ("next slide",
"Slide 2" for a page jump by name, "end of show") or links to the document it shows or runs; a script event-listener
is "macro". A clicked shape's text is wrapped in the link, else a line "Picture" (a frame with an image) or "Link"
follows. A frame holding a draw:plugin starts with "Video: name" (Audio by its mime type; the frame's name, else the
file's), linked when the file is outside the package, "(poster below)" when an image follows.
"""
import posixpath
import re

from .. import diagram, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Doc, Image, Link, Para, Section, Text
from . import deck
from .odf_base import GROUPS as _GROUPS, SHAPES as _SHAPES, Body, Styles, _cm, _flat, _squash, register, shape_key
from .word_fields import MAX_URL

_SKIP = frozenset({"page-number", "date-time", "header", "footer", "page"})
_MASTER_SKIP = _SKIP | {"title", "outline", "subtitle"}
_DEFAULT_NAME = re.compile(r"^(page|slide|folie|diapositive|diapositiva|dia|슬라이드)\s*\d+$", re.I)
_MOVES = {"next-page": "next slide", "previous-page": "previous slide", "first-page": "first slide",
          "last-page": "last slide", "stop": "end of show"}
_CLICKS = frozenset({"dom:click", "on-click", "click"})
MAX_NAME = 200


def _box(el):
    """(x0, y0, x1, y1) in cm of a shape, or None when its position is missing or unreadable."""
    if local(el.tag) in ("line", "connector", "measure"):
        ends = [_cm(attr(el, k)) for k in ("x1", "y1", "x2", "y2")]
        return None if None in ends else tuple(ends)
    x, y = _cm(attr(el, "x")), _cm(attr(el, "y"))
    if x is None or y is None:
        return None
    return x, y, x + (_cm(attr(el, "width")) or 0), y + (_cm(attr(el, "height")) or 0)


def _union(boxes):
    return (min(min(b[0], b[2]) for b in boxes), min(min(b[1], b[3]) for b in boxes),
            max(max(b[0], b[2]) for b in boxes), max(max(b[1], b[3]) for b in boxes))


class _Slides:
    def __init__(self, body):
        self.body = body
        self.budget = diagram.ConnectionBudget(body.ctx)  # the deck's edges, shared by every slide's diagram
        self.order = deck.Order()
        self.numbers = {}
        self.masters = {}

    def units(self, cont, d, link):
        """[(box, (element, link href) or [units])] of the shapes under cont, a group or draw:a one unit."""
        if d > limits.MAX_DEPTH:
            self.body.deep()
            return []
        out = []
        for el in cont:
            n = local(el.tag)
            if n in _GROUPS:
                inner = self.units(el, d + 1, (attr(el, "href") or "").strip() if n == "a" else link)
                boxes = [b for b, _ in inner if b is not None]
                out.append((_union(boxes) if boxes else None, inner))
            elif n in _SHAPES:
                out.append((_box(el), (el, link)))
        return out

    def read(self, units):
        for i in self.order([b for b, _ in units]):
            item = units[i][1]
            if isinstance(item, list):
                yield from self.read(item)
            else:
                yield item

    def shapes(self, cont, skip, master=False):
        """[(element, class, link href)] of the shapes under cont in reading order."""
        out = []
        for el, link in self.read(self.units(cont, 0, "")):
            cls = attr(el, "class")
            if cls in skip or (master and attr(el, "placeholder") == "true"):
                continue
            out.append((el, cls, link))
        return out

    def blocks(self, el):
        if local(el.tag) == "frame":
            return self.media(el) + self.body.frame_blocks(el, 1)
        return self.body.blocks(el, 1)

    def media(self, fr) -> list:
        """[the Para "Video: name"] of a frame holding a draw:plugin, else []."""
        plugin = next(iter(kids(fr, "plugin")), None)
        if plugin is None:
            return []
        href = (attr(plugin, "href") or "").strip()[:MAX_URL]
        mime = (attr(plugin, "mime-type") or "").lower()
        kind = "Audio" if mime.startswith("audio") else "Video" if mime.startswith("video") else "Media"
        name = _squash(attr(fr, "name") or "", MAX_NAME) or posixpath.basename(href)
        line = [Text(f"{kind}: ")]
        line.append(Link(href, [Text(name or href)]) if href and self.body.safe(href) is None else Text(name))
        if kids(fr, "image"):
            line.append(Text(" (poster below)"))
        return [Para(line)]

    def href(self, h):
        """(URL or "", jump words or "") of a link target: "#page name" a slide jump."""
        h = (h or "").strip()
        if h.startswith("#"):
            number = self.numbers.get(h[1:])
            return "", f"Slide {number}" if number else ""
        return h[:MAX_URL], ""

    def click(self, el, link):
        """(URL or "", jump words or "") of a shape inside a draw:a to `link` or with a click event-listener."""
        url, jump = self.href(link) if link else ("", "")
        for ev in (e for lst in kids(el, "event-listeners") for e in lst):
            if (attr(ev, "event-name") or "").lower() not in _CLICKS:
                continue
            action = attr(ev, "action")
            if action is None:
                jump = jump or "macro"
            elif action in _MOVES:
                jump = jump or _MOVES[action]
            elif action in ("show", "execute"):
                got = self.href(attr(ev, "href"))
                url, jump = url or got[0], jump or got[1]
        return url, jump

    def slide(self, page, number) -> Section:
        title, slots, notes = [], [], []
        ctx, dia, keys = self.body.ctx, diagram.Diagram(self.body.ctx, self.budget), set()
        for el, cls, link in self.shapes(page, _SKIP):
            got = self.blocks(el)
            url, jump = self.click(el, link)
            if url or jump:
                label = "Picture" if any(isinstance(b, Image) for b in got) else "Link"
                got = deck.linked(got, url, jump, label)
            key = shape_key(el, keys)
            with ctx.guard("shape"):
                register(dia, self.body.styles, el, key, got)
            if cls == "title":  # the slide's heading: its text names the node, it has no slot
                if t := _flat(got):
                    title.append(t)
            else:
                slots.append((key, got))
        blocks = [b for _, got in slots for b in got]
        with ctx.guard("shape"):
            blocks = dia.place(slots)
        for nt in kids(page, "notes"):
            for el, _, _ in self.shapes(nt, _SKIP):
                notes.extend(b for b in self.blocks(el) if not isinstance(b, Image))
        if not title:  # a slide the user renamed is labelled by that name
            name = (attr(page, "name") or "").strip()
            if name and not _DEFAULT_NAME.match(name):
                title.append(name)
        sec = Section("slide", _squash(" ".join(title), 200), number, blocks, notes)
        style = attr(page, "style-name")
        if style and self.body.styles.prop(style, "drawing-page", "visibility") == "hidden":
            sec.blocks, sec.hidden = self.body.hide(blocks), "hidden"
        return sec

    def master(self, m) -> list:
        """[blocks of each shape on master page `m` that is not a placeholder], read once per master."""
        if id(m) not in self.masters:
            self.masters[id(m)] = []
            try:
                with self.body.ctx.guard("master"):
                    got = [self.blocks(el) for el, _, _ in self.shapes(m, _MASTER_SKIP, True)]
                    self.masters[id(m)] = [b for b in got if b]
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.body.ctx.skipped["master"] += 1
        return self.masters[id(m)]

    def furniture(self, page, masters, decls, repeated, number):
        """Give `repeated` what page `number` shows from its master and its footer."""
        m = masters.get(attr(page, "master-page-name")) or (next(iter(masters.values())) if len(masters) == 1 else None)
        style = attr(page, "style-name")
        prop = (lambda key: self.body.styles.prop(style, "drawing-page", key)) if style else (lambda key: None)
        if m is not None and prop("background-objects-visible") != "false":
            for blocks in self.master(m):
                repeated.add(number, blocks)
        if prop("display-footer") != "true":
            return
        text = decls.get(attr(page, "use-footer-name"))
        if text is None and m is not None:
            with self.body.ctx.guard("master"):
                text = next((t for el in m if local(el.tag) in _SHAPES and attr(el, "class") == "footer"
                             and (t := _flat(self.blocks(el)))), None)
        if text:
            repeated.add(number, [Para([Text(text)])])


def slides(pkg, ctx, content, styles_root) -> Doc:
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "presentation"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    sl, pages, made = _Slides(body), kids(office, "page"), []
    for number, page in enumerate(pages, 1):
        sl.numbers.setdefault((attr(page, "name") or "").strip(), number)
    masters = {} if styles_root is None else {
        attr(e, "name"): e for e in styles_root.iter() if local(e.tag) == "master-page"}
    decls = {attr(e, "name"): _squash("".join(e.itertext()), MAX_NAME) for e in kids(office, "footer-decl")}
    repeated = deck.Repeated()
    for number, page in enumerate(pages, 1):  # numbering counts hidden and unreadable slides
        if ctx.late():
            break
        try:
            with ctx.guard("slide"):
                sec = sl.slide(page, number)
                made.append(sec)
                sl.furniture(page, masters, decls, repeated, number)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["slide"] += 1
    if pages and not made:
        raise ReadError("damaged", "no readable slides")
    body.doc.blocks.extend(repeated.place(made))
    body.doc.blocks.extend(made)
    return body.doc
