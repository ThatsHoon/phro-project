"""Excel 97-2003 (.xls) pictures, for xls.py, through officeart.py (logic after dochan `office_binary/xls_drawing.py`:
the sheet's MSODRAWING records joined with their CONTINUEs, the BStore of the globals' MSODRAWINGGROUP, a shape's pib
and its ClientAnchor order).

A sheet's drawing is a DgContainer, split over its MSODRAWING records; xls.py joins their bodies, leaving out the TXO
and OBJ records between them. Each shape with a pib is one placement of that BStore entry's BLIP. A placement is
ordered by its anchor's first cell (row1, then col1; an OfficeArtClientAnchorSheet is flags u16, col1 u16, dx1 u16,
row1 u16, ...), the unanchored ones after, in record order. A placement is counted under `image` when its pib is
outside the BStore, its BLIP cannot be read or is not a kind Assets keeps, past IMAGE_BYTES placed (copies counted),
and past MAX_PLACEMENTS in the workbook.
"""
import struct

from .. import officeart
from ..containers import flate
from ..images import sniff
from ..limits import IMAGE_BYTES, ReadError
from ..model import Image

MAX_PLACEMENTS = 10_000  # pictures one workbook places, all its sheets together
_UNANCHORED = (1 << 32, 0)


def pictures(g, drawing: bytes, budget: flate.Budget, spent: list, ctx) -> list:
    """The pictures of one sheet: `drawing` is its MSODRAWING bodies joined, `g` the globals whose `drawing` holds the
    BStore, `budget` the document's flate budget and `spent[0]` the image bytes placed so far in the
    document and `spent[1]` the placements read so far in it (the workbook's cap, MAX_PLACEMENTS)."""
    shapes = officeart.Shapes()
    shapes.add(drawing, 0, len(drawing))
    if shapes.deep:
        ctx.skipped["depth"] += 1
    if shapes.cut:
        ctx.skipped["record"] += 1
    placed = list(shapes.pib.items())
    room = max(MAX_PLACEMENTS - spent[1], 0)
    if len(placed) > room:
        ctx.skipped["image"] += len(placed) - room
        placed = placed[:room]
    spent[1] += len(placed)
    placed.sort(key=lambda item: _position(drawing, shapes.anchors.get(item[0])))  # stable: record order within a cell
    store = officeart.bstore(g.drawing)
    blips: dict[int, bytes | None] = {}
    out = []
    for _, pib in placed:
        if not 0 < pib <= len(store):
            ctx.skipped["image"] += 1
            continue
        if pib not in blips:
            blips[pib] = _blip(g.drawing, store[pib - 1], budget)
        data = blips[pib]
        if data is None or len(data) < 16 or sniff(data) is None:
            ctx.skipped["image"] += 1
            continue
        spent[0] += len(data)
        if spent[0] > IMAGE_BYTES:
            ctx.skipped["image"] += 1
            continue
        out.append(Image(data))
    return out


def _position(drawing: bytes, anchor) -> tuple:
    """(row1, col1) of an OfficeArtClientAnchorSheet at (body offset, size); after every anchored one when absent."""
    if anchor is None or anchor[1] < 8:
        return _UNANCHORED
    _, col, _, row = struct.unpack_from("<4H", drawing, anchor[0])
    return row, col


def _blip(dgg: bytes, off: int, budget: flate.Budget) -> bytes | None:
    """The image of the BStore block at `off`; None when it is unreadable, a BLIP that inflates past the ratio cap or
    the document's total included (that costs the one picture)."""
    try:
        return officeart.fbse_blip(dgg, off, None, budget)
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        return None
