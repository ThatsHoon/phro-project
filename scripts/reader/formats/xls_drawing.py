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
"""
import struct

from .. import officeart
from ..containers import flate
from ..limits import IMAGE_BYTES
from ..model import Image

MAX_PLACEMENTS = 10_000  # pictures one workbook places, all its sheets together
_UNANCHORED = (1 << 32, 0)


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
