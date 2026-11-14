"""ppt shapes: the OfficeArt shapes of a PPDrawing, their anchors and group transforms, native tables, reading order.

The geometry follows dochan `office_binary/ppt_shapes.py` and `ppt_render.py`: a ClientAnchor holds top, left, right,
bottom (i16, or i32 in 16 bytes) while a ChildAnchor and an FSPGR rectangle hold left, top, right, bottom (i32); a
group maps the rectangle of its children's coordinates (FSPGR) onto its own anchor, flips included, and the maps of
nested groups compose; a group whose FOPT property 0x039F has bit 0 set is a table whose grid is cut by the edges of
its cells. [MS-ODRAW] and [MS-PPT] are knowledge only. The text atoms of a shape are those of `ppt_text`; this module
also holds the record budget (`Walk`) and the text-atom walk the slide reader shares.

Hostile input: every record is bounded by `officeart.records`, the walk is iterative, groups nest `MAX_GROUPS` deep
(deeper ones are skipped and counted under `depth`), every record spends the presentation's `BIN_RECORDS`, and a table
charges its grid to `TABLE_NODES` and `GRID_SLOTS` (a grid past them becomes flat text and is counted under `cells`).
"""
import struct
from dataclasses import dataclass, field

from .. import limits, officeart
from ..model import Cell, Table
from . import ppt_records as rec
from . import ppt_text

MAX_GROUPS = 32
SKIPPED = frozenset((7, 8, 9, 10))  # placeholders: date, slide number, footer, header
TITLES = frozenset((13, 15))        # placeholders: title, centered title
CHILD_ANCHOR, FSPGR, CLIENT_TEXTBOX, CLIENT_DATA, DG_CONTAINER = 0xF00F, 0xF009, 0xF00D, 0xF011, 0xF002
_TABLE_PROP = 0x039F
_EXTRA_FOPT = frozenset((0xF121, 0xF122))  # secondary and tertiary property tables
_LINES = frozenset((20, 32, 33, 34, 38))  # connector and line shape types: table borders, never cells
_BIG = 1e12
_IDENTITY = (1.0, 1.0, 0.0, 0.0)  # sx, sy, dx, dy: slide = scale * group + offset


class Walk:
    """Shared budgets of one presentation: records walked, text characters emitted, table grid nodes and slots."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.records = limits.BIN_RECORDS
        self.chars = limits.MAX_OUTPUT
        self.nodes = limits.TABLE_NODES
        self.slots = limits.GRID_SLOTS

    def spend(self) -> bool:
        self.records -= 1
        if self.records < 0:
            if self.records == -1:
                self.ctx.skipped["record"] += 1
            return False
        return True


def _decode(data, start, size, typ) -> str:
    raw = bytes(data[start:start + size])
    if typ == rec.TEXT_CHARS_ATOM:
        return raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace")
    return raw.decode("latin-1")


def texts(data, start, end, walk):
    """The text in data[start:end] in document order, containers entered, as ["text", kind, text, style] and
    ["ref", index] items."""
    items = []
    stack = [(officeart.records(data, start, end), 4, None)]
    while stack:
        it, kind, last = stack.pop()
        for ver, inst, typ, body, size in it:
            if not walk.spend():
                return items
            if typ == rec.TEXT_HEADER_ATOM and size >= 4:
                # not validated: only 0 and 6 (titles) and 2 (notes) change what is done with a run, so a type outside
                # 0..8 reads as body text (test_unknown_text_header_type_is_body_text pins it)
                kind = struct.unpack_from("<I", data, body)[0]
            elif typ in (rec.TEXT_CHARS_ATOM, rec.TEXT_BYTES_ATOM):
                last = ["text", kind, _decode(data, body, size, typ), None]
                items.append(last)
                kind = 4
            elif typ == rec.STYLE_TEXT_PROP_ATOM and last is not None and last[3] is None:
                last[3] = bytes(data[body:body + size])
            elif typ == rec.OUTLINE_TEXT_REF_ATOM and size >= 4:
                items.append(["ref", struct.unpack_from("<I", data, body)[0]])
            elif ver == 0xF:
                if len(stack) >= limits.MAX_DEPTH:
                    walk.ctx.skipped["record"] += 1
                    continue
                stack.append((it, kind, last))
                stack.append((officeart.records(data, body, body + size), 4, None))
                break
    return items


def text_blocks(items, walk):
    """[(kind, blocks, plain text)] of the "text" items."""
    out = []
    for item in items:
        if item[0] != "text":
            continue
        walk.chars -= len(item[2])
        if walk.chars < 0:
            raise ValueError("text budget")
        blocks = ppt_text.blocks(item[2], item[3], walk.ctx)
        out.append((item[1], blocks, " ".join(item[2].replace("\x0b", " ").replace("\r", " ").split())))
    return out


@dataclass
class Shape:
    """One shape in slide coordinates. `runs` holds the shape's text as (TextHeaderAtom type, blocks, plain text);
    `blocks` is the blocks of those runs (or a table's one Table). `text_ref` is the OutlineTextRefAtom index, `pib`
    the picture's BStore index and `ole_ref` the ExObjRefAtom id; `anchored` is false when the shape had no anchor."""
    kind: str
    top: float = 0.0
    left: float = 0.0
    bottom: float = 0.0
    right: float = 0.0
    blocks: list = field(default_factory=list)
    text_ref: int | None = None
    placeholder: int | None = None
    pib: int | None = None
    ole_ref: int | None = None
    children: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    anchored: bool = False
    spid: int = 0
    stype: int = 0
    table: bool = False


class _Raw:
    """What one SpContainer says before it is placed."""
    __slots__ = ("flags", "stype", "spid", "table", "pib", "client", "child", "rect", "runs", "text_ref",
                 "placeholder", "ole_ref")

    def __init__(self):
        self.flags = self.stype = self.spid = 0
        self.table, self.pib, self.client, self.child, self.rect = False, None, None, None, None
        self.runs, self.text_ref, self.placeholder, self.ole_ref = [], None, None, None


class _Frame:
    __slots__ = ("group", "it", "tf", "head", "patriarch")

    def __init__(self, group, it, tf, patriarch):
        self.group, self.it, self.tf, self.head, self.patriarch = group, it, tf, True, patriarch


def _clamp(v: float) -> float:
    return max(-_BIG, min(_BIG, v))


def _shape(data, body, size, walk) -> _Raw:
    """The SpContainer whose body is data[body:body + size], read."""
    raw, seen_opt = _Raw(), False
    for _, inst, typ, b, s in officeart.records(data, body, body + size):
        if not walk.spend():
            break
        if typ == officeart.FSP and s >= 8:
            raw.spid, raw.flags = struct.unpack_from("<II", data, b)
            raw.stype = inst
        elif typ == officeart.FOPT and not seen_opt:
            seen_opt = True
            prop = officeart.fopt_property(data, inst, b, s, _TABLE_PROP)
            raw.table = raw.table or bool(prop is not None and prop & 1)
            raw.pib = officeart.fopt_property(data, inst, b, s, officeart.PIB) or None
        elif typ in _EXTRA_FOPT:  # PowerPoint keeps the table flag in the tertiary properties
            prop = officeart.fopt_property(data, inst, b, s, _TABLE_PROP)
            raw.table = raw.table or bool(prop is not None and prop & 1)
        elif typ == officeart.CLIENT_ANCHOR and raw.client is None:
            if s == 8:
                top, left, right, bottom = struct.unpack_from("<4h", data, b)
            elif s == 16:
                top, left, right, bottom = struct.unpack_from("<4i", data, b)
            else:
                walk.ctx.skipped["record"] += 1
                continue
            raw.client = (left, top, right, bottom)
        elif typ == CHILD_ANCHOR and raw.child is None and s >= 16:
            raw.child = struct.unpack_from("<4i", data, b)
        elif typ == FSPGR and raw.rect is None and s >= 16:
            raw.rect = struct.unpack_from("<4i", data, b)
        elif typ == CLIENT_TEXTBOX:
            items = texts(data, b, b + s, walk)
            raw.runs.extend(text_blocks(items, walk))
            for item in items:
                if item[0] == "ref" and raw.text_ref is None:
                    raw.text_ref = item[1]
        elif typ == CLIENT_DATA:
            for _, _, t, cb, cs in officeart.records(data, b, b + s):
                if not walk.spend():
                    break
                if t == rec.OE_PLACEHOLDER_ATOM and cs >= 5:
                    raw.placeholder = data[cb + 4]
                elif t == rec.EX_OBJ_REF_ATOM and cs >= 4:
                    raw.ole_ref = struct.unpack_from("<I", data, cb)[0]
                elif t == rec.OUTLINE_TEXT_REF_ATOM and cs >= 4:
                    raw.text_ref = struct.unpack_from("<I", data, cb)[0]
    return raw


def _box(raw: _Raw):
    """The raw (left, top, right, bottom) of a shape: its ChildAnchor, else its ClientAnchor; None without one."""
    return raw.child if raw.child is not None else raw.client


def _place(shape: Shape, box, tf):
    """Set shape's bounds from `box` mapped through `tf`."""
    if box is None:
        return
    sx, sy, dx, dy = tf
    x0, x1 = _clamp(sx * box[0] + dx), _clamp(sx * box[2] + dx)
    y0, y1 = _clamp(sy * box[1] + dy), _clamp(sy * box[3] + dy)
    shape.left, shape.right = min(x0, x1), max(x0, x1)
    shape.top, shape.bottom = min(y0, y1), max(y0, y1)
    shape.anchored = True


def _child_tf(raw: _Raw, box, tf):
    """The map of the coordinates inside a group onto the slide: the group's FSPGR rectangle onto its anchor `box`
    (in the parent's coordinates, which `tf` maps), mirrored for fFlipH 0x40 and fFlipV 0x80."""
    if box is None or raw.rect is None:
        return tf
    x0, y0, x1, y1 = raw.rect
    if x1 == x0 or y1 == y0:
        return tf
    left, top, right, bottom = box
    ax, ay = (right - left) / (x1 - x0), (bottom - top) / (y1 - y0)
    ox, oy = left, top
    if raw.flags & 0x40:
        ax, ox = -ax, right
    if raw.flags & 0x80:
        ay, oy = -ay, bottom
    sx, sy, dx, dy = tf
    return (sx * ax, sy * ay, dx + sx * (ox - x0 * ax), dy + sy * (oy - y0 * ay))


def _leaf(raw: _Raw, tf) -> Shape:
    if raw.ole_ref is not None:  # an OLE object's shape also carries its preview as a pib
        kind = "object"
    elif raw.pib:
        kind = "picture"
    else:
        kind = "text"
    shape = Shape(kind, text_ref=raw.text_ref, placeholder=raw.placeholder, pib=raw.pib, ole_ref=raw.ole_ref,
                  runs=raw.runs, blocks=[b for _, bl, _ in raw.runs for b in bl], spid=raw.spid, stype=raw.stype)
    _place(shape, _box(raw), tf)
    return shape


def walk(data, start: int, end: int, ctx, budget: Walk | None = None) -> list:
    """The shapes of the PPDrawing (or DgContainer) records in data[start:end], as the top-level Shapes of its
    SpgrContainer: a group has its members in `children`, in record order and in slide coordinates (see `ordered`).
    A group nested more than MAX_GROUPS deep is skipped and counted under `depth`."""
    budget = budget or Walk(ctx)
    top = Shape("group")
    pending, found = [officeart.records(data, start, end)], []
    while pending:
        for ver, _, typ, body, size in pending[-1]:
            if not budget.spend():
                pending.clear()
                break
            if typ == rec.PP_DRAWING or typ == DG_CONTAINER:
                if len(pending) < 4:
                    pending.append(officeart.records(data, body, body + size))
                    break
            elif typ == officeart.SPGR_CONTAINER:
                found.append((body, size))
            elif typ == officeart.SP_CONTAINER:
                top.children.append(_leaf(_shape(data, body, size, budget), _IDENTITY))
        else:
            pending.pop()
    for body, size in found:
        _group(data, body, size, budget, top)
    return top.children


def _group(data, body, size, budget, top):
    """One SpgrContainer (the patriarch, or a root group of the drawing) and the groups inside it, iteratively."""
    ctx = budget.ctx
    root = Shape("group")
    stack = [_Frame(root, officeart.records(data, body, body + size), _IDENTITY, True)]
    while stack:
        f = stack[-1]
        for _, _, typ, b, s in f.it:
            if not budget.spend():
                stack.clear()
                break
            head, f.head = f.head, False
            if typ == officeart.SP_CONTAINER:
                raw = _shape(data, b, s, budget)
                if head:
                    _enter(f, raw)
                else:
                    f.group.children.append(_leaf(raw, f.tf))
            elif typ == officeart.SPGR_CONTAINER:
                if len(stack) > MAX_GROUPS:
                    ctx.skipped["depth"] += 1
                    continue
                group = Shape("group")
                f.group.children.append(group)
                stack.append(_Frame(group, officeart.records(data, b, b + s), f.tf, False))
                break
        else:
            _close(stack.pop(), budget)
    top.children.extend(root.children)


def _enter(f: _Frame, raw: _Raw):
    """The first SpContainer of a group's SpgrContainer is the group's own shape."""
    if f.patriarch:
        return
    g = f.group
    g.spid, g.table = raw.spid, raw.table
    box = _box(raw)
    _place(g, box, f.tf)
    f.tf = _child_tf(raw, box, f.tf)


def _close(f: _Frame, budget):
    """A group's members are all in: a table group becomes a table when its cells make a grid."""
    g = f.group
    if g.table and not f.patriarch:
        _table(g, budget)


def _table(group: Shape, budget: Walk):
    """Turn `group` into a table shape when its children are cells that share exact edges; otherwise it stays a
    plain group and its children's text stays in reading order. A grid past TABLE_NODES or GRID_SLOTS counts `cells`."""
    cells = []
    for child in group.children:
        if child.kind != "text" or child.text_ref is not None:
            return
        wide = child.right > child.left and child.bottom > child.top
        if not wide or child.stype in _LINES:
            if child.blocks:
                return
            continue
        cells.append(child)
    if not cells:
        return
    xs = sorted({v for c in cells for v in (c.left, c.right)})
    ys = sorted({v for c in cells for v in (c.top, c.bottom)})
    cols, rows = len(xs) - 1, len(ys) - 1
    if rows * cols > min(budget.nodes, budget.slots):
        budget.ctx.skipped["cells"] += 1
        return
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    origin, covered = {}, set()
    for c in cells:
        r0, r1, c0, c1 = yi[c.top], yi[c.bottom], xi[c.left], xi[c.right]
        for r in range(r0, r1):
            for k in range(c0, c1):
                if (r, k) in covered:  # overlapping cells: at most rows * cols slots were visited
                    return
                covered.add((r, k))
        origin[(r0, c0)] = (c, r1 - r0, c1 - c0)
    budget.nodes -= rows * cols
    budget.slots -= rows * cols
    grid = []
    for r in range(rows):
        row = []
        for k in range(cols):
            if (r, k) in origin:
                c, rs, cs = origin[(r, k)]
                row.append(Cell(c.blocks, rs, cs))
            elif (r, k) not in covered:
                row.append(Cell([]))
        grid.append(row)
    group.kind = "table"
    group.blocks = [Table(grid)]
    group.runs = [(4, group.blocks, "")]
    group.children = []


def ordered(shapes: list) -> list:
    """`shapes` in reading order, by (top, left); a group sits where its own anchor puts it (the union of its members
    when it has none) and its members are ordered inside it."""
    for s in shapes:
        if s.kind == "group" and s.children:
            s.children = ordered(s.children)
            if not s.anchored:
                s.top, s.left = min(c.top for c in s.children), min(c.left for c in s.children)
                s.bottom, s.right = max(c.bottom for c in s.children), max(c.right for c in s.children)
    return sorted(shapes, key=lambda s: (s.top, s.left))


def flatten(shapes: list):
    """The shapes that are not groups, depth first, in the order of `shapes`."""
    stack = [iter(shapes)]
    while stack:
        for s in stack[-1]:
            if s.kind == "group":
                stack.append(iter(s.children))
                break
            yield s
        else:
            stack.pop()
