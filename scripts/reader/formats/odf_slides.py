"""OpenDocument presentations (.odp): one slide section per draw:page, shapes in reading order.

Each slide is one diagram scope: its shapes and connectors (groups flattened) go to `diagram.Diagram` through
`odf_base.register`; titles are registered without a slot."""
import re

from .. import diagram, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Doc, Image, Para, Section, Text
from .odf_base import GROUPS as _GROUPS, SHAPES as _SHAPES, Body, Styles, _cm, _flat, _squash, register, shape_key

_SKIP = frozenset({"page-number", "date-time", "header", "footer", "page"})
_MASTER_SKIP = _SKIP | {"title", "outline", "subtitle"}
_DEFAULT_NAME = re.compile(r"^(page|slide|folie|diapositive|diapositiva|dia|슬라이드)\s*\d+$", re.I)


def _pos(el):
    """(row, column) in cm rounded to 0.1, or None when the position is missing or unreadable."""
    x, y = _cm(attr(el, "x")), _cm(attr(el, "y"))
    return None if x is None or y is None else (round(y, 1), round(x, 1))


class _Slides:
    def __init__(self, body):
        self.body = body
        self.budget = diagram.ConnectionBudget(body.ctx)  # the deck's edges, shared by every slide's diagram

    def leaves(self, cont, d, inherit, out):
        if d > limits.MAX_DEPTH:
            self.body.deep()
            return
        for el in cont:
            n = local(el.tag)
            if n in _GROUPS:
                self.leaves(el, d + 1, _pos(el) or inherit, out)
            elif n in _SHAPES:
                out.append((el, _pos(el) or inherit, d))

    def shapes(self, cont, skip, master=False):
        """[(element, class)] of the shapes under cont in reading order."""
        found = []
        self.leaves(cont, 0, None, found)
        keyed = sorted(((0, *p, i) if p else (1, 0, 0, i), el) for i, (el, p, _) in enumerate(found))
        out = []
        for _, el in keyed:
            cls = attr(el, "class")
            if cls in skip or (master and attr(el, "placeholder") == "true"):
                continue
            out.append((el, cls))
        return out

    def blocks(self, el):
        if local(el.tag) == "frame":
            return self.body.frame_blocks(el, 1)
        return self.body.blocks(el, 1)

    def slide(self, page, number) -> Section:
        title, slots, notes = [], [], []
        ctx, dia, keys = self.body.ctx, diagram.Diagram(self.body.ctx, self.budget), set()
        for el, cls in self.shapes(page, _SKIP):
            got = self.blocks(el)
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
            for el, _ in self.shapes(nt, _SKIP):
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

    def master_text(self, styles_root, used, footer=False) -> list:
        skip = _MASTER_SKIP - {"footer"} if footer else _MASTER_SKIP  # a footer shown on a slide is content
        masters = [e for e in styles_root.iter() if local(e.tag) == "master-page"]
        out, seen = [], set()
        for m in [m for m in masters if attr(m, "name") in used] or masters:
            try:
                with self.body.ctx.guard("master"):
                    for el, _ in self.shapes(m, skip, True):
                        text = _flat(self.blocks(el))
                        if text and text not in seen:
                            seen.add(text)
                            out.append(Para([Text(text)]))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.body.ctx.skipped["master"] += 1
        return out


def slides(pkg, ctx, content, styles_root) -> Doc:
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "presentation"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    sl, pages, made, used = _Slides(body), kids(office, "page"), [], set()
    footer = any(body.styles.prop(attr(p, "style-name"), "drawing-page", "display-footer") == "true" for p in pages)
    for number, page in enumerate(pages, 1):  # numbering counts hidden and unreadable slides
        used.add(attr(page, "master-page-name"))
        try:
            with ctx.guard("slide"):
                made.append(sl.slide(page, number))
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["slide"] += 1
    if pages and not made:
        raise ReadError("damaged", "no readable slides")
    if styles_root is not None:
        with ctx.guard("master"):
            body.doc.blocks.extend(sl.master_text(styles_root, used, footer))
    body.doc.blocks.extend(made)
    return body.doc
