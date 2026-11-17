"""Excel 97-2003 (.xls) pictures, for xls.py, through officeart.py (logic after dochan `office_binary/xls_drawing.py`:
the sheet's MSODRAWING records joined with their CONTINUEs, the BStore of the globals' MSODRAWINGGROUP, a shape's pib
and its ClientAnchor order).

A sheet's drawing is a DgContainer, split over its MSODRAWING records; xls.py joins their bodies, leaving out the TXO
and OBJ records between them. Each shape with a pib is one placement of that BStore entry's BLIP. A placement is
ordered by its anchor's first cell (row1, then col1; an OfficeArtClientAnchorSheet is flags u16, col1 u16, dx1 u16,
row1 u16, ...), the unanchored ones after, in record order. A shape with no anchor of its own (or one under 8 bytes)
takes the anchor of the nearest group around it that has one, as dochan does. A placement is counted under `image`
when its pib is outside the BStore, its BLIP cannot be read or is not a kind Assets keeps, past IMAGE_BYTES placed
(copies counted), and past MAX_PLACEMENTS in the workbook; the placements cut by that last cap are the last ones in
reading order. Each BStore entry is read once per workbook (`blips`), however many placements show it.

`shapes` gives the sheet's diagram (diagram.py, the sheet one scope): every SpContainer is registered, its box in cell
units (x = col + dx/1024, y = row + dy/256 from its own ClientAnchor, none without one). The k-th TXO text of the sheet
(xls.py reads them) belongs to the k-th shape with a ClientTextbox (0xF00D), in record order: a node's text, or a
connector's label. A connector (types 32 to 40, or a plain line, 20) runs from its box's start corner (the far one on
an axis its FSP flips) to the opposite one, glued by the drawing's connector rules. Each text is one paragraph slot,
in the anchor order the pictures use; a TXO with no text box follows them. The walk's depth and record cuts are
counted by `pictures`, which walks the same drawing.
"""
import re
import struct

from .. import limits, officeart
from ..containers import flate
from ..limits import IMAGE_BYTES
from ..model import Image, Para
from .sheet_grid import text_inlines

MAX_PLACEMENTS = 10_000  # pictures one workbook places, all its sheets together
TEXTBOX = 0xF00D         # OfficeArtClientTextbox: the shape's text is the sheet's next TXO
_UNANCHORED = (1 << 32, 0)
_BREAKS = re.compile(r"\r\n|\r")


def pictures(g, drawing: bytes, budget: flate.Budget, spent: list, ctx, blips: dict) -> list:
    """The pictures of one sheet: `drawing` is its MSODRAWING bodies joined, `g` the globals whose `drawing` holds the
    BStore, `budget` the document's flate budget, `spent[0]` the image bytes placed so far in the
    document and `spent[1]` the placements read so far in it (the workbook's cap, MAX_PLACEMENTS), and `blips` the
    workbook's cache of BStore entries read so far (pib -> image bytes, None when unreadable)."""
    shapes = officeart.Shapes()
    shapes.add(drawing, 0, len(drawing))
    if shapes.deep:
        ctx.skipped["depth"] += 1
    if shapes.cut:
        ctx.skipped["record"] += 1
    placed = list(shapes.pib.items())
    inherited = _inherited(shapes, len(placed))
    order = sorted(range(len(placed)), key=lambda k: _position(
        drawing, shapes.anchors.get(placed[k][0]), inherited[k]))  # stable: record order within a cell
    room = max(MAX_PLACEMENTS - spent[1], 0)
    if len(order) > room:  # the cut keeps the first in reading order
        ctx.skipped["image"] += len(order) - room
        order = order[:room]
    spent[1] += len(order)
    store = officeart.bstore(g.drawing)
    out = []
    for pib in (placed[k][1] for k in order):
        if not 0 < pib <= len(store):
            ctx.skipped["image"] += 1
            continue
        if pib not in blips:  # one inflate per picture for the workbook, not per placement or per sheet
            blips[pib] = officeart.blip_image(g.drawing, store[pib - 1], None, budget)
        data = blips[pib]
        if data is None:
            ctx.skipped["image"] += 1
            continue
        spent[0] += len(data)
        if spent[0] > IMAGE_BYTES:
            ctx.skipped["image"] += 1
            continue
        out.append(Image(data))
    return out


def _inherited(shapes, n: int) -> list:
    """For each of the first `n` pictures (in record order), the anchor of the nearest group around it that has a
    usable one, or None. One sweep: the groups come in record order, which is nesting order."""
    out = [None] * n
    todo = sorted((at, i, j) for at, (i, j) in shapes.groups.items()
                  if i < j and _usable(shapes.anchors.get(at)))
    stack, g = [], 0  # the open groups: (last picture + 1, anchor)
    for k in range(n):
        while g < len(todo) and todo[g][1] <= k:
            at, i, j = todo[g]
            g += 1
            while stack and stack[-1][0] <= i:
                stack.pop()
            stack.append((j, shapes.anchors[at]))
        while stack and stack[-1][0] <= k:
            stack.pop()
        if stack:
            out[k] = stack[-1][1]
    return out


def _usable(anchor) -> bool:
    return anchor is not None and anchor[1] >= 8


def _position(drawing: bytes, anchor, inherited=None) -> tuple:
    """(row1, col1) of an OfficeArtClientAnchorSheet at (body offset, size), else that of the `inherited` one; after
    every anchored one when there is neither."""
    if not _usable(anchor):
        anchor = inherited
    if not _usable(anchor):
        return _UNANCHORED
    _, col, _, row = struct.unpack_from("<4H", drawing, anchor[0])
    return row, col


class _Sp:
    """One SpContainer: its first FSP's spid, flags and type (the FSP's instance), its first FOPT's arrowheads, its
    own ClientAnchor (body offset, size), the anchor it is ordered by (its own, else its nearest group's) and whether
    it has a ClientTextbox."""
    __slots__ = ("spid", "flags", "stype", "arrows", "anchor", "order", "textbox")

    def __init__(self):
        self.spid = self.flags = self.stype = 0
        self.arrows = (False, False)
        self.anchor = self.order = None
        self.textbox = False


def shapes(drawing: bytes, texts: list, dia, ctx) -> list:
    """The slots [(key, blocks)] of one sheet for `dia`, its diagram scope: each shape with the paragraph of its text
    (`paragraph`), in anchor order, then the paragraphs of the TXO texts no text box took. `drawing` is the sheet's
    MSODRAWING bodies joined and `texts` its TXO texts in record order. Each shape is registered on `dia` under
    guard("shape"), keyed by its spid, or ("unkeyed", n) for spid 0 or one an earlier shape has."""
    found, rules = _read(drawing)
    glue = {}
    for a, b, c in rules:  # spidC -> (spidA, spidB); the first rule naming a connector wins
        glue.setdefault(c, (a, b))
    rest = iter(texts)
    keyed, keys = [], set()
    for sp in found:
        text = next(rest, "") if sp.textbox else ""
        key = sp.spid if sp.spid and sp.spid not in keys else ("unkeyed", len(keys))
        keys.add(key)
        blocks = paragraph(text)
        with ctx.guard("shape"):
            _register(dia, drawing, sp, key, blocks, glue)
        keyed.append((key, blocks, sp.order))
    order = sorted(range(len(keyed)), key=lambda k: _position(drawing, keyed[k][2]))  # stable: record order
    return [keyed[k][:2] for k in order] + [(None, paragraph(t)) for t in rest]


def paragraph(text: str) -> list:
    """A TXO text as one paragraph, its line breaks as Break; nothing for a blank one."""
    if not text.strip():
        return []
    return [Para(text_inlines(_BREAKS.sub("\n", text)))]


def _read(drawing: bytes) -> tuple[list, list]:
    """([_Sp] in record order, connector rules) of a sheet's drawing. Containers are entered up to limits.MAX_DEPTH and
    the walk visits at most officeart.MAX_RECORDS records; `pictures` counts both cuts."""
    found, rules, n = [], [], 0
    # a level: its walk, the anchor its shapes inherit, whether it is an SpgrContainer whose group shape is to come
    stack = [[officeart.records(drawing), None, False]]
    while stack and n < officeart.MAX_RECORDS:
        level = stack[-1]
        rec = next(level[0], None)
        if rec is None:
            stack.pop()
            continue
        n += 1
        ver, _, typ, body, size = rec
        if typ == officeart.SP_CONTAINER:
            sp = _Sp()
            n += _shape(drawing, body, size, sp)
            sp.order = sp.anchor if _usable(sp.anchor) else level[1]
            if level[2]:  # the group shape: its members inherit its anchor
                level[1], level[2] = sp.order, False
            found.append(sp)
        elif typ == officeart.SOLVER:
            rules.extend(officeart.rules(drawing, body - 8, body + size))
        elif ver == 0xF and len(stack) < limits.MAX_DEPTH:
            stack.append([officeart.records(drawing, body, body + size), level[1], typ == officeart.SPGR_CONTAINER])
    return found, rules


def _shape(drawing: bytes, body: int, size: int, sp: _Sp) -> int:
    """Read the SpContainer whose body is drawing[body:body + size] into `sp`; the number of records read."""
    n, fsp, fopt = 0, False, False
    for _, inst, typ, b, s in officeart.records(drawing, body, body + size):
        n += 1
        if typ == officeart.FSP and not fsp:
            fsp = True
            sp.spid, sp.flags = officeart.fsp(drawing[b:b + min(s, 8)])
            sp.stype = inst
        elif typ == officeart.FOPT and not fopt:
            fopt = True
            sp.arrows = officeart.arrows(drawing, inst, b, s)
        elif typ == officeart.CLIENT_ANCHOR and sp.anchor is None:
            sp.anchor = (b, s)
        elif typ == TEXTBOX:
            sp.textbox = True
    return n


def _register(dia, drawing: bytes, sp: _Sp, key, blocks: list, glue: dict):
    """One shape on the sheet's diagram: a connector or line from its start corner to the opposite one, glued to the
    ends of the rule that names its spid and labelled with its text; any other shape a node of its paragraph, with
    its type's kind and its box."""
    box = _box(drawing, sp.anchor)
    if sp.stype == officeart.LINE_TYPE or sp.stype in officeart.CONNECTOR_TYPES:
        begin, end = glue.get(key, (None, None))
        p0 = p1 = None
        if box is not None:
            x0, y0, x1, y1 = box
            if sp.flags & officeart.FLIP_H:
                x0, x1 = x1, x0
            if sp.flags & officeart.FLIP_V:
                y0, y1 = y1, y0
            p0, p1 = (x0, y0), (x1, y1)
        dia.connector(key, begin, end, p0, p1, sp.arrows, line=sp.stype == officeart.LINE_TYPE, blocks=blocks)
    else:
        dia.shape(key, blocks, officeart.KINDS.get(sp.stype, ""), box)


def _box(drawing: bytes, anchor):
    """(x0, y0, x1, y1) in cell units of an OfficeArtClientAnchorSheet at (body offset, size): flags, col1, dx1, row1,
    dy1, col2, dx2, row2, dy2 (u16 each), x = col + dx/1024 and y = row + dy/256; None when it is absent or short."""
    if anchor is None or anchor[1] < 18:
        return None
    _, c1, dx1, r1, dy1, c2, dx2, r2, dy2 = struct.unpack_from("<9H", drawing, anchor[0])
    return c1 + dx1 / 1024, r1 + dy1 / 256, c2 + dx2 / 1024, r2 + dy2 / 256
