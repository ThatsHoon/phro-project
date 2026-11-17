"""DrawingML shape geometry and connector glue, shared by the readers whose drawings are DrawingML (PresentationML
slides, spreadsheet drawings, Word drawing groups and canvases). Elements match by local name; numbers are EMU.

`Xf` maps a shape's own space to the scope's through every enclosing group's off/ext/chOff/chExt. A shape's box is
its off/ext, bound around its rotation; a connector runs from its box's start corner after flips to the opposite
corner, turned with the box. Group rotation is not applied.
"""
import math

from ..containers.zipx import attr, kids, local

COORD = 10**13  # an EMU number past this, in absolute value, is clamped to it

_KINDS = {"flowchartdecision": "decision", "diamond": "decision", "flowchartterminator": "terminator",
          "ellipse": "circle", "flowchartconnector": "circle", "can": "store", "flowchartmagneticdisk": "store"}
_LINES = frozenset({"line", "straightconnector1"})
_ON = ("1", "true")


def num(el, name):
    """An integer attribute clamped to ±COORD, or None when the element or a whole number is missing."""
    v = attr(el, name) if el is not None else None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(-COORD, min(COORD, n))


def kid(el, name):
    """The first child with this local name, or None."""
    k = kids(el, name)
    return k[0] if k else None


def nv(el):
    """(cNvPr, ph) of a shape, either may be None."""
    cnv = ph = None
    for c in el:
        if local(c.tag).startswith("nv"):
            cnv = cnv if cnv is not None else kid(c, "cNvPr")
            for pr in kids(c, "nvPr"):
                ph = ph if ph is not None else kid(pr, "ph")
    return cnv, ph


def xfrm(el):
    """A shape's own a:xfrm (or a graphic frame's p:xfrm), from spPr or grpSpPr; None without one."""
    for c in el:
        n = local(c.tag)
        if n == "xfrm":
            return c
        if n in ("spPr", "grpSpPr"):
            x = kid(c, "xfrm")
            if x is not None:
                return x
    return None


def _rect(x):
    """(x, y, w, h, angle in radians, flipH, flipV) of an xfrm in its own space, or None without off and ext."""
    off, ext = kid(x, "off"), kid(x, "ext")
    ox, oy, cx, cy = num(off, "x"), num(off, "y"), num(ext, "cx"), num(ext, "cy")
    if None in (ox, oy, cx, cy):
        return None
    rot = num(x, "rot") or 0
    return (ox, oy, max(cx, 0), max(cy, 0), math.radians(rot % 21_600_000 / 60_000),
            attr(x, "flipH") in _ON, attr(x, "flipV") in _ON)


class Xf:
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
        off, ext, cho, che = (kid(xfrm, n) for n in ("off", "ext", "chOff", "chExt"))
        ox, oy, cx, cy = num(off, "x"), num(off, "y"), num(cho, "x"), num(cho, "y")
        if None in (ox, oy, cx, cy):
            return self
        sx = self._scale(num(ext, "cx"), num(che, "cx"))
        sy = self._scale(num(ext, "cy"), num(che, "cy"))
        return Xf(max(-1e15, min(1e15, self.ox + (ox - cx * sx) * self.sx)), max(1e-9, min(1e9, sx * self.sx)),
                  max(-1e15, min(1e15, self.oy + (oy - cy * sy) * self.sy)), max(1e-9, min(1e9, sy * self.sy)))

    def point(self, xfrm):
        """(y, x) on the slide of a shape's offset, or None without a usable offset."""
        off = kid(xfrm, "off") if xfrm is not None else None
        x, y = num(off, "x"), num(off, "y")
        if x is None or y is None:
            return None
        return (self.oy + y * self.sy, self.ox + x * self.sx)

    def _map(self, x, y):
        return self.ox + x * self.sx, self.oy + y * self.sy

    def box(self, xfrm):
        """(x0, y0, x1, y1) in slide space of the shape's off/ext, bound around its rotation about the centre; None
        without off and ext."""
        r = _rect(xfrm) if xfrm is not None else None
        if r is None:
            return None
        x, y, w, h, a = r[:5]
        c, s = abs(math.cos(a)), abs(math.sin(a))
        mx, my = x + w / 2, y + h / 2
        hw, hh = (w * c + h * s) / 2, (w * s + h * c) / 2
        return (*self._map(mx - hw, my - hh), *self._map(mx + hw, my + hh))

    def ends(self, xfrm):
        """((x0, y0), (x1, y1)) in slide space: the start corner after flipH/flipV and the opposite corner, turned
        clockwise by the shape's rotation about its centre; None without off and ext."""
        r = _rect(xfrm) if xfrm is not None else None
        if r is None:
            return None
        x, y, w, h, a, fh, fv = r
        x0, x1 = (x + w, x) if fh else (x, x + w)
        y0, y1 = (y + h, y) if fv else (y, y + h)
        if not a:
            return self._map(x0, y0), self._map(x1, y1)
        mx, my = x + w / 2, y + h / 2
        c, s = math.cos(a), math.sin(a)
        return tuple(self._map(mx + (px - mx) * c - (py - my) * s, my + (px - mx) * s + (py - my) * c)
                     for px, py in ((x0, y0), (x1, y1)))


def arrows(sppr) -> tuple:
    """(start, end) arrowheads from spPr's a:ln headEnd and tailEnd: True when present with a type other than
    `none` (the schema's default), else False."""
    ln = kid(sppr, "ln") if sppr is not None else None
    if ln is None:
        return False, False
    return tuple((e := kid(ln, n)) is not None and (attr(e, "type") or "none") != "none"
                 for n in ("headEnd", "tailEnd"))


def _prst(sppr) -> str:
    g = kid(sppr, "prstGeom") if sppr is not None else None
    return (attr(g, "prst") or "").casefold() if g is not None else ""


def kind(sppr) -> str:
    """The diagram node kind of spPr's preset geometry: decision, terminator, circle, store, or ""."""
    return _KINDS.get(_prst(sppr), "")


def is_line(sppr) -> bool:
    """Whether spPr's preset geometry is a plain line (`line` or `straightConnector1`)."""
    return _prst(sppr) in _LINES


def glue(el) -> tuple:
    """(stCxn@id, endCxn@id) of a connector, each None when absent: under nvCxnSpPr/cNvCxnSpPr, or Word's
    wps:cNvCnPr."""
    nvc = kid(el, "nvCxnSpPr")
    pr = kid(nvc, "cNvCxnSpPr") if nvc is not None else kid(el, "cNvCnPr")
    if pr is None:
        return None, None
    return tuple((attr(e, "id") if (e := kid(pr, n)) is not None else None) for n in ("stCxn", "endCxn"))
