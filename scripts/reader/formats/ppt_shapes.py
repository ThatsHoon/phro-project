"""ppt shapes: the OfficeArt shapes of a PPDrawing, their anchors and group transforms, native tables, reading order.

The geometry follows dochan `office_binary/ppt_shapes.py` and `ppt_render.py`: a ClientAnchor holds top, left, right,
bottom (i16, or i32 in 16 bytes) while a ChildAnchor and an FSPGR rectangle hold left, top, right, bottom (i32); a
group maps the rectangle of its children's coordinates (FSPGR) onto its own anchor, flips included, and the maps of
nested groups compose; a group whose FOPT property 0x039F has bit 0 set is a table whose grid is cut by the edges of
its cells. [MS-ODRAW] and [MS-PPT] are knowledge only. The text atoms of a shape are those of `ppt_text`; this module
also holds the record budget and lookups (`Walk`) and the text-atom walk the slide reader shares. For the slide's
diagram a shape keeps its arrowheads, and a connector or line its two ends: its anchor's start corner (the far one on
an axis its FSP flips) and the opposite corner, mapped like its box; the drawing's connector rules come with the shapes.
A shape's mouse-click InteractiveInfoAtom (in its ClientData) is its `click`; in a ClientTextbox one followed by a
TextInteractiveInfoAtom clicks that range of the text before it; the character a slide number, date, header or
footer MetaCharAtom marks is a field and is dropped. `Walk.link` turns a click into (URL, jump words) and
`Walk.styles` holds the master's bullets by text type for the text being read. The shapes read in `deck.order` over
their anchors, a group's members inside the group.

Hostile input: every record is bounded by `officeart.records`, the walk is iterative, groups nest `MAX_GROUPS` deep
(deeper ones are skipped and counted under `depth`), every record spends the presentation's `BIN_RECORDS`, and a table
charges its grid to `TABLE_NODES` and `GRID_SLOTS` (a grid past them becomes flat text and is counted under `cells`).
"""
import struct
from dataclasses import dataclass, field

from .. import limits, officeart
from ..model import Cell, Table
from . import deck, ppt_text
from . import ppt_records as rec

MAX_GROUPS = 32
# Table cell edges within CLUSTER master units (576 per inch) are one grid line. A cell's edges are whole master units
# that PowerPoint and the group map round one by one, so the cells of one grid line can differ by a unit or two;
# 4 units is half a point (the same size as rtf_table's 10 twips), far under the width of any real column or row.
CLUSTER = 4
SKIPPED = frozenset((7, 8, 9, 10))  # placeholders: date, slide number, footer, header
TITLES = frozenset((13, 15))        # placeholders: title, centered title
CHILD_ANCHOR, FSPGR, CLIENT_TEXTBOX, CLIENT_DATA, DG_CONTAINER = 0xF00F, 0xF009, 0xF00D, 0xF011, 0xF002
_TABLE_PROP = 0x039F
_EXTRA_FOPT = frozenset((0xF121, 0xF122))  # secondary and tertiary property tables
_LINES = frozenset((20, 32, 33, 34, 38))  # connector and line shape types: table borders, never cells
_BIG = 1e12
_IDENTITY = (1.0, 1.0, 0.0, 0.0)  # sx, sy, dx, dy: slide = scale * group + offset


class Walk:
    """Shared budgets of one presentation (records walked, text characters emitted, table grid nodes and slots) and
    its lookups: `styles`, {text type: [bullet per indent level]} of the master of what is read, and `link`, a click
    (exHyperlinkIdRef, action, jump, hyperlinkType) to (URL or "", jump words or "")."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.records = limits.BIN_RECORDS
        self.chars = limits.MAX_OUTPUT
        self.nodes = limits.TABLE_NODES
        self.slots = limits.GRID_SLOTS
        self.styles = {}
        self.link = lambda click: ("", "")

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


def click(data, body, size, walk):
    """(exHyperlinkIdRef, action, jump, hyperlinkType) of the InteractiveInfoAtom inside an InteractiveInfo container,
    or None without one."""
    for _, _, typ, b, s in officeart.records(data, body, body + size):
        if not walk.spend():
            return None
        if typ == rec.INTERACTIVE_INFO_ATOM and s >= 13:
            ref, action, _, jump, _, kind = struct.unpack_from("<4xIBBBBB", data, b)
            return ref, action, jump, kind
    return None


def texts(data, start, end, walk):
    """The text in data[start:end] in document order, containers entered, as ["text", kind, text, style, clicked
    ranges (begin, end, click), field positions] and ["ref", index] items."""
    items = []
    stack = [(officeart.records(data, start, end), 4, None, None)]
    while stack:
        it, kind, last, pending = stack.pop()
        for ver, inst, typ, body, size in it:
            if not walk.spend():
                return items
            if typ == rec.TEXT_HEADER_ATOM and size >= 4:
                # not validated: only 0 and 6 (titles) and 2 (notes) change what is done with a run, so a type outside
                # 0..8 reads as body text (test_unknown_text_header_type_is_body_text pins it)
                kind = struct.unpack_from("<I", data, body)[0]
            elif typ in (rec.TEXT_CHARS_ATOM, rec.TEXT_BYTES_ATOM):
                last = ["text", kind, _decode(data, body, size, typ), None, [], []]
                items.append(last)
                kind, pending = 4, None
            elif typ == rec.STYLE_TEXT_PROP_ATOM and last is not None and last[3] is None:
                last[3] = bytes(data[body:body + size])
            elif typ == rec.OUTLINE_TEXT_REF_ATOM and size >= 4:
                items.append(["ref", struct.unpack_from("<I", data, body)[0]])
            elif typ == rec.INTERACTIVE_INFO:
                if inst == 0:
                    pending = click(data, body, size, walk)
            elif typ in rec.META_CHARS and size >= 4 and last is not None:
                last[5].append(struct.unpack_from("<I", data, body)[0])
            elif typ == rec.TEXT_INTERACTIVE_INFO_ATOM and size >= 8:
                if pending is not None and last is not None:
                    last[4].append((*struct.unpack_from("<II", data, body), pending))
                pending = None
            elif ver == 0xF:
                if len(stack) >= limits.MAX_DEPTH:
                    walk.ctx.skipped["record"] += 1
                    continue
                stack.append((it, kind, last, pending))
                stack.append((officeart.records(data, body, body + size), 4, None, None))
                break
    return items


def _levels(styles, kind) -> list:
    """The master's bullets by indent level of text type `kind`: its own TextMasterStyleAtom's, a centre body, half or
    quarter body falling back to the body's and a centre title to the title's, level by level."""
    own, base = styles.get(kind, []), styles.get({5: 1, 6: 0, 7: 1, 8: 1}.get(kind), [])
    return [a if a is not None else (base[k] if k < len(base) else None) for k, a in
            enumerate(own + [None] * max(0, len(base) - len(own)))]


def text_blocks(items, walk):
    """[(kind, blocks, plain text)] of the "text" items."""
    out = []
    for item in items:
        if item[0] != "text":
            continue
        walk.chars -= len(item[2])
        if walk.chars < 0:
            raise ValueError("text budget")
        text = item[2]
        for at in item[5]:
            if at < len(text):
                text = text[:at] + "\x00" + text[at + 1:]
        links = [(b, e, *walk.link(c)) for b, e, c in item[4]]
        blocks = ppt_text.blocks(text, item[3], walk.ctx, _levels(walk.styles, item[1]), links)
        plain = text.replace("\x00", "").replace("\x0b", " ").replace("\r", " ")
        out.append((item[1], blocks, " ".join(plain.split())))
    return out


@dataclass
class Shape:
    """One shape in slide coordinates. `runs` holds the shape's text as (TextHeaderAtom type, blocks, plain text);
    `blocks` is the blocks of those runs (or a table's one Table). `text_ref` is the OutlineTextRefAtom index, `pib`
    the picture's BStore index and `ole_ref` the ExObjRefAtom id; `anchored` is false when the shape had no anchor
    (a group with none gets it from `ordered` when a member has one)."""
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
    arrows: tuple = (False, False)
    ends: tuple | None = None  # a connector's or line's (start, end) points; None for another shape or no anchor
    click: tuple | None = None  # its mouse-click (exHyperlinkIdRef, action, jump, hyperlinkType)


class _Raw:
    """What one SpContainer says before it is placed."""
    __slots__ = ("flags", "stype", "spid", "table", "pib", "client", "child", "rect", "runs", "text_ref",
                 "placeholder", "ole_ref", "arrows", "click")

    def __init__(self):
        self.flags = self.stype = self.spid = 0
        self.arrows = (False, False)
        self.table, self.pib, self.client, self.child, self.rect = False, None, None, None, None
        self.runs, self.text_ref, self.placeholder, self.ole_ref, self.click = [], None, None, None, None


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
            raw.spid, raw.flags = officeart.fsp(data[b:b + 8])
            raw.stype = inst
        elif typ == officeart.FOPT and not seen_opt:
            seen_opt = True
            prop = officeart.fopt_property(data, inst, b, s, _TABLE_PROP)
            raw.table = raw.table or bool(prop is not None and prop & 1)
            raw.pib = officeart.fopt_property(data, inst, b, s, officeart.PIB) or None
            raw.arrows = officeart.arrows(data, inst, b, s)
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
            for _, ci, t, cb, cs in officeart.records(data, b, b + s):
                if not walk.spend():
                    break
                if t == rec.INTERACTIVE_INFO and ci == 0 and raw.click is None:
                    raw.click = click(data, cb, cs, walk)
                elif t == rec.OE_PLACEHOLDER_ATOM and cs >= 5:
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


def _ends(raw: _Raw, box, tf):
    """A connector's (start, end): the start corner of its raw box (left, top; right on fFlipH, bottom on fFlipV) and
    the opposite corner, mapped through `tf`; None without a box."""
    if box is None:
        return None
    left, top, right, bottom = box
    x0, x1 = (right, left) if raw.flags & officeart.FLIP_H else (left, right)
    y0, y1 = (bottom, top) if raw.flags & officeart.FLIP_V else (top, bottom)
    sx, sy, dx, dy = tf
    return ((_clamp(sx * x0 + dx), _clamp(sy * y0 + dy)), (_clamp(sx * x1 + dx), _clamp(sy * y1 + dy)))


def _leaf(raw: _Raw, tf) -> Shape:
    if raw.ole_ref is not None:  # an OLE object's shape also carries its preview as a pib
        kind = "object"
    elif raw.pib:
        kind = "picture"
    else:
        kind = "text"
    shape = Shape(kind, text_ref=raw.text_ref, placeholder=raw.placeholder, pib=raw.pib, ole_ref=raw.ole_ref,
                  runs=raw.runs, blocks=[b for _, bl, _ in raw.runs for b in bl], spid=raw.spid, stype=raw.stype,
                  arrows=raw.arrows, click=raw.click)
    _place(shape, _box(raw), tf)
    if raw.stype == officeart.LINE_TYPE or raw.stype in officeart.CONNECTOR_TYPES:
        shape.ends = _ends(raw, _box(raw), tf)
    return shape


def walk(data, start: int, end: int, ctx, budget: Walk | None = None) -> tuple[list, list]:
    """(shapes, rules) of the PPDrawing (or DgContainer) records in data[start:end]: the top-level Shapes of its
    SpgrContainer, a group with its members in `children`, in record order and in slide coordinates (see `ordered`),
    and the (spidA, spidB, spidC) connector rules of its SolverContainers, each spending a record. A group nested more
    than MAX_GROUPS deep is skipped and counted under `depth`."""
    budget = budget or Walk(ctx)
    top = Shape("group")
    pending, found, rules = [officeart.records(data, start, end)], [], []
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
            elif typ == officeart.SOLVER:
                for rule in officeart.rules(data, body - 8, body + size):
                    if not budget.spend():
                        break
                    rules.append(rule)
        else:
            pending.pop()
    for body, size in found:
        _group(data, body, size, budget, top)
    return top.children, rules


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


def _lines(values) -> tuple[dict, list]:
    """({value: grid line index}, [grid lines]) of the edge `values`: an edge within CLUSTER of the first of its
    cluster is that cluster's line."""
    index, lines = {}, []
    for v in sorted(values):
        if not lines or v - lines[-1] > CLUSTER:
            lines.append(v)
        index[v] = len(lines) - 1
    return index, lines


def _table(group: Shape, budget: Walk):
    """Turn `group` into a table shape when its children are cells that share edges (within CLUSTER); otherwise it stays a
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
    xi, xs = _lines({v for c in cells for v in (c.left, c.right)})
    yi, ys = _lines({v for c in cells for v in (c.top, c.bottom)})
    cols, rows = len(xs) - 1, len(ys) - 1
    if rows * cols > min(budget.nodes, budget.slots):
        budget.ctx.skipped["cells"] += 1
        return
    origin, covered = {}, set()
    for c in cells:
        r0, r1, c0, c1 = yi[c.top], yi[c.bottom], xi[c.left], xi[c.right]
        if r1 <= r0 or c1 <= c0:  # thinner than CLUSTER: a line, not a cell
            if c.blocks:
                return
            continue
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
    """`shapes` in `deck.order` over their anchors; a group sits where its anchored members are (their union; its own
    anchor when none is anchored) and its members are ordered inside it. A shape with no anchor, and a group with no
    anchor and no anchored member, keeps its record place; it does not pull a group's union to 0."""
    boxes = []
    for s in shapes:
        box = (s.left, s.top, s.right, s.bottom) if s.anchored else None
        if s.kind == "group" and s.children:
            s.children = ordered(s.children)
            placed = [c for c in s.children if c.anchored]
            if placed:
                box = (min(c.left for c in placed), min(c.top for c in placed),
                       max(c.right for c in placed), max(c.bottom for c in placed))
                if not s.anchored:
                    s.left, s.top, s.right, s.bottom = box
                    s.anchored = True
        boxes.append(box)
    return [shapes[i] for i in deck.order(boxes)]


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
