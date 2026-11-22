"""Shapes joined by connectors, one drawing scope at a time, as one Mermaid `flowchart` block or a nested list.

Readers only extract: per scope they register each shape (`Diagram.shape`) and connector (`Diagram.connector`) under
their own shape keys, then hand the scope's flow to `Diagram.place` as `(key or None, blocks)` slots. The module
numbers, escapes, orients and renders, and builds model blocks only.

- **Edges.** A connector glued at both ends to registered shapes is an edge. An end glued to another connector joins
  the shape glued at that connector's end nearest the end's point, followed through connectors up to
  `limits.MAX_DEPTH`; with no point, a tie, a loop, or a hidden or loose connector on the way, and for a reference to
  a missing key, it is not glue. A hidden connector is never an edge. Edges keep `connector()` order and go through
  the document's `ConnectionBudget`, the glued ones first. A connector given its slot's blocks is labelled with their
  plain text on one line when that is at most NAME characters; else as a node is (below), and as an edge its blocks
  follow the diagram after an `nA --> nB:` paragraph, the edge as written. The shapes inside a connector (their
  `parent` it) that show plain text join its label at the end nearer their box's centre along its route (`path`,
  else the chord between its end points; none, or a tie, the middle): the label is `start / label / end`, read from
  the written start, each slot's texts joined by a space and an empty slot kept, or the connector's own label alone
  when both end slots are empty. A one-line text box of at most NAME characters that is in no group, not an edge end
  and not inside an end's box joins the middle slot of the edge whose route passes nearest its centre, within its
  own height and INFER_TOL of the scope diagonal, when no other route is as near (none past FLOAT_WORK box-segment
  pairs a scope). Past NAME characters the label is cut with `…` and the taken shapes' blocks follow too; their
  `data` follows always.
- **Loose ends.** A connector with an end not glued (a plain `line` only with an arrowhead set True) is a candidate,
  up to CONNECTIONS a document, snapped or not (`infer_skipped` once for a scope that has some past them). Each loose
  end's point lands on the smallest box holding it, else on the nearest within INFER_TOL of the diagonal of the box
  around every shape box and every line end; containers and connectors are never targets, and a tie (equal areas, or
  distances within 1e-9 × tol) lands nowhere. When both ends land, on two shapes, it is an inferred edge, written
  after `%% inferred from position`; else it keeps its slot and counts `connector_loose`, unless it names a shape at
  neither end, lands on none and has no arrowhead set True: a decoration, not counted. A scope of more than
  INFER_SHAPES boxed shapes is not inferred (`infer_skipped` once); a zero-size scope places no end.
- **Nodes.** The edges' ends, numbered `n1`, `n2`, ... in the order their keys first appear in the slots, then the
  slotless ones in `shape()` order. Plain text of at most NAME characters is the whole label; any other content gets
  its first line cut at NAME plus `…` (or `(picture)`, `(table)`, `(chart)`, `(content)`), and its blocks follow the
  diagram after a `nK:` paragraph, and so does its `data` (Shape Data, never its label). A shape may name its
  group (`parent`). An end on a shape with no text and no text below it that is a part of its group moves up to the
  group, and on while each is a part of its own, stopping at the first with text or text below. The reader's
  `part` verdict is final; with none, a part lies inside its group's box and either spans its full width or height
  or touches its border at most PORT of its area. Never through a hidden shape, a container or a connector, at most
  `limits.MAX_DEPTH` up (text below counts only from shapes that are not edge ends). A textless node is labelled by
  the plain text of the shapes below it (not hidden, not containers, not ends, nor under one), in slot order, and
  those shapes leave the flow; else by its name. A hidden shape, or one left with no label, is a blank node; a hidden
  one keeps its slot. A blank node is declared (`nK[" "]`) only inside a subgraph; elsewhere it is written in its
  edges alone. The kinds decision, terminator, circle, store, chevron (`nK>"…"]`) and callout
  (`nK@{ shape: comment, label: "…" }`) have their own node forms.
- **Containers.** A container shape (not hidden, with a box, not an edge end) holding a node's centre becomes
  `subgraph cK ["title"]`; the smallest box wins, containers nest the same way up to `limits.MAX_DEPTH`, and the nodes
  of deeper ones go to the deepest kept one. A container with no node is an ordinary shape, and so is every
  container of a scope with more than INFER_SHAPES of them or with no node box.
- **Trees.** Directed unlabelled edges, one parent at most, one root reaching every node, a node with two or more
  children, at most `smartart.MAX_LEVEL` levels and no subgraph: a `-` list instead of Mermaid, a blank node's item
  `(unlabelled shape)`.
- **Placement.** The block goes at the first slot whose key is a node, an edge or a subgraph; the slots of the shown
  nodes, the edges, the subgraphs and the shapes their labels took are taken out. With no edge the slots come back
  unchanged.
"""
import math
import re

from . import limits
from .formats import smartart
from .model import Bold, Break, Code, Image, Link, ListItem, Para, Table, Text

CONNECTIONS = 10_000  # edges one document may write; past it `connections_cut` once per cut scope
NAME = 80             # characters of a shape's text that go into its node whole
INFER_TOL = 0.02      # of the scope diagonal: how far a loose end may lie from the box it snaps to
INFER_SHAPES = 2_000  # shapes a scope may have and still get its loose ends inferred
COORD = 1e9           # a coordinate past this, in absolute value, makes the box unknown
PORT = 0.125
PATH = 256
FLOAT_WORK = 4_000_000

_NEWLINE = re.compile(r"\r\n|\r|\n")
_KINDS = {"": ("[", "]"), "decision": ("{", "}"), "terminator": ("([", "])"), "circle": ("((", "))"),
          "store": ("[(", ")]"), "chevron": (">", "]"), "callout": ("@{ shape: comment, label: ", " }")}


class ConnectionBudget:
    """The document's CONNECTIONS room (read when made): `take` keeps what fits of one scope's edges and counts
    `connections_cut` once for a list it cuts. `infer` is the room for inference candidates, spent whether they become
    edges or stay loose."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.room = CONNECTIONS
        self.infer = CONNECTIONS

    def take(self, edges) -> list:
        if len(edges) <= self.room:
            self.room -= len(edges)
            return edges
        kept = edges[:max(0, self.room)]
        self.room = 0
        self.ctx.skipped["connections_cut"] += 1
        return kept


class _Shape:
    __slots__ = ("blocks", "kind", "box", "hidden", "container", "title", "parent", "name", "part", "data")

    def __init__(self, blocks, kind, box, hidden, container, title, parent, name, part, data):
        self.blocks, self.kind, self.box = blocks, kind, box
        self.hidden, self.container, self.title = hidden, container, title
        self.parent, self.name, self.part, self.data = parent, name, part, data


class _Connector:
    __slots__ = ("begin", "end", "p0", "p1", "arrows", "label", "line", "hidden", "follow", "path")

    def __init__(self, begin, end, p0, p1, arrows, label, line, hidden, follow=None, path=None):
        self.begin, self.end, self.p0, self.p1 = begin, end, p0, p1
        self.arrows, self.label, self.line, self.hidden = arrows, label, line, hidden
        self.follow = follow  # the blocks that follow the diagram when it is an edge, or None
        self.path = path


def _copy(c, **changes):
    out = _Connector(c.begin, c.end, c.p0, c.p1, c.arrows, c.label, c.line, c.hidden, c.follow, c.path)
    for k, v in changes.items():
        setattr(out, k, v)
    return out


def _box(box):
    """(x0, y0, x1, y1) with min first, or None when absent or not four finite numbers within COORD."""
    if box is None:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(v) and abs(v) <= COORD for v in (x0, y0, x1, y1)):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _point(p):
    """(x, y), or None when absent or not two finite numbers within COORD."""
    if p is None:
        return None
    try:
        x, y = (float(v) for v in p)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(v) and abs(v) <= COORD for v in (x, y)):
        return None
    return x, y


def _snap(p, targets, tol):
    """The key of the target (key, box, area) the point lands on: the smallest box holding it, else the nearest box
    within tol; None for no point, a tie or no box in reach."""
    if p is None:
        return None
    x, y = p
    best, tie = None, False
    for k, b, area in targets:
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            if best is None or area < best[0]:
                best, tie = (area, k), False
            elif area == best[0]:
                tie = True
    if best is not None:
        return None if tie else best[1]
    near, d1, d2 = None, math.inf, math.inf
    for k, b, _ in targets:
        d = math.hypot(max(b[0] - x, 0.0, x - b[2]), max(b[1] - y, 0.0, y - b[3]))
        if d < d1:
            near, d1, d2 = k, d, d1
        elif d < d2:
            d2 = d
    if near is None or d1 > tol or d2 - d1 <= 1e-9 * tol:
        return None
    return near


def _centre(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _esc(s: str) -> str:
    """A Mermaid label: `"` becomes `#quot;`, each line break `<br>`; nothing else is touched."""
    return _NEWLINE.sub("<br>", s.replace('"', "#quot;"))


def _arrow(arrows):
    """(arrow, swapped) for a connector's (start, end) arrowheads; None on one side reads False when the other is
    known, both None read as begin -> end."""
    a, b = arrows
    if a is None and b is None:
        return "-->", False
    if a and b:
        return "<-->", False
    if a:
        return "-->", True
    if b:
        return "-->", False
    return "---", False


def _plain(blocks):
    """The blocks' text, lines joined with \\n, when they are only Para and ListItem of Text, Bold and Break with
    nothing hidden; else None."""
    lines = []
    for b in blocks:
        if not isinstance(b, (Para, ListItem)) or b.hidden:
            return None
        parts, stack = [], list(reversed(b.inlines))
        while stack:
            i = stack.pop()
            if getattr(i, "hidden", ""):
                return None
            if isinstance(i, Text):
                parts.append(i.text)
            elif isinstance(i, Break):
                parts.append("\n")
            elif isinstance(i, Bold):
                stack.extend(reversed(i.inlines))
            else:
                return None
        lines.append("".join(parts))
    return "\n".join(lines)


def _first_line(blocks) -> str:
    """The first non-blank line of the shown text in the blocks' Para and ListItem (Text, in Bold and Link too)."""
    for b in blocks:
        if not isinstance(b, (Para, ListItem)) or b.hidden:
            continue
        parts, stack = [], list(reversed(b.inlines))
        while stack:
            i = stack.pop()
            if getattr(i, "hidden", ""):
                continue
            if isinstance(i, Text):
                parts.append(i.text)
            elif isinstance(i, Break):
                parts.append("\n")
            elif isinstance(i, (Bold, Link)):
                stack.extend(reversed(i.inlines))
        for line in _NEWLINE.split("".join(parts)):
            if line.strip():
                return line.strip()
    return ""


def _label(blocks):
    """(label, follow): plain text of at most NAME characters whole (blank when it has none), else the first line cut
    at NAME plus `…`, or a one-word stand-in, with `follow` set so the blocks follow the diagram."""
    text = _plain(blocks)
    if text is not None and len(text) <= NAME:
        return text.strip(), False
    line = _first_line(blocks)
    if line:
        return line[:NAME] + "…", True
    for b in blocks:
        if isinstance(b, Image):
            return "(picture)", True
        if isinstance(b, Table):
            return ("(chart)" if b.caption.startswith("Chart") else "(table)"), True
    return "(content)", True


def _edge_label(blocks):
    """(label, follow) of a connector's blocks: their plain text on one line, each run of white space one space, when
    that is at most NAME characters, with nothing to follow; else `_label`'s cut first line or stand-in, the blocks
    to follow the diagram."""
    text = _plain(blocks)
    if text is not None:
        line = " ".join(text.split())
        if len(line) <= NAME:
            return line, None
    return _label(blocks)[0], list(blocks)


def _along(path, p) -> tuple:
    best, at, total = math.inf, 0.0, 0.0
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 <= 0 else min(1.0, max(0.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
        d = math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)
        length = math.sqrt(length2)
        if d < best:
            best, at = d, total + t * length
        total += length
    return at, total


def _side(c, box) -> int:
    if box is None or c.path is None:
        return 1
    at, total = _along(c.path, _centre(box))
    if total <= 0:
        return 1
    return 0 if 2 * at < total else 2 if 2 * at > total else 1


def _seg_point(a, b, p) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 <= 0 else min(1.0, max(0.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _edge(c, nid) -> tuple:
    """(from, arrow, to) of an edge as written: its node ids, swapped when only the start has an arrowhead."""
    arrow, swapped = _arrow(c.arrows)
    a, b = nid[c.begin], nid[c.end]
    return (b, arrow, a) if swapped else (a, arrow, b)


def _inlines(label: str) -> list:
    """A label as list-item inlines, its line breaks as Break."""
    out = []
    for i, part in enumerate(_NEWLINE.split(label)):
        if i:
            out.append(Break())
        if part:
            out.append(Text(part))
    return out


class Diagram:
    """One drawing scope: shapes and connectors registered by the reader, written by place()."""

    def __init__(self, ctx, budget: ConnectionBudget):
        self.ctx, self.budget = ctx, budget
        self.shapes: dict = {}      # key -> _Shape, in shape() order
        self.connectors: dict = {}  # key -> _Connector, in connector() order

    def shape(self, key, blocks: list, kind: str = "", box=None, hidden: bool = False, container: bool = False,
              title: str = "", parent=None, name: str = "", part=None, data=()) -> None:
        """`parent` is the key of the group the shape sits in, and `part` is the reader's verdict on whether the shape
        is a piece of it (True for a master shape's sub-shape, False for an instance of its own master), None when the
        reader cannot tell; `name` stands for its text when it has none; `data` are blocks of its slot that describe
        it (Shape Data) and never make its label."""
        if key in self.shapes:
            return
        self.shapes[key] = _Shape(blocks, kind if kind in _KINDS else "", _box(box), bool(hidden), bool(container),
                                  (title or "").strip(), parent, " ".join((name or "").split())[:NAME],
                                  None if part is None else bool(part),
                                  list(data))

    def connector(self, key, begin=None, end=None, p0=None, p1=None, arrows=(None, None), label: str = "",
                  line: bool = False, hidden: bool = False, blocks=None, path=None, data=()) -> None:
        """`blocks`, when given, are what the connector's slot holds: they make the label (see `_edge_label`), and
        when they are more than a short plain text they follow the diagram should it become an edge; `data` follow
        it too and never make its label. `path` is its route's points on the page, begin to end."""
        if key in self.connectors:
            return
        follow = None
        if blocks is not None:
            label, follow = _edge_label(blocks)
        if data:
            follow = (follow or []) + list(data)
        p0, p1 = _point(p0), _point(p1)
        points = [q for q in (_point(p) for p in list(path or ())[:PATH]) if q is not None]
        route = points if len(points) >= 2 else [p0, p1] if p0 and p1 else None
        self.connectors[key] = _Connector(begin, end, p0, p1, arrows, (label or "").strip(), bool(line),
                                          bool(hidden), follow, route)

    def _glued(self, key) -> bool:
        return key is not None and key in self.shapes and key not in self.connectors

    def _through(self, key, p, own):
        start, seen = key, {own}
        for _ in range(limits.MAX_DEPTH):
            c = self.connectors.get(key)
            if c is None or key in seen or c.hidden or p is None or not all(
                    self._glued(k) or k in self.connectors for k in (c.begin, c.end)):
                return start
            seen.add(key)
            d0 = math.dist(p, c.p0) if c.p0 else math.inf
            d1 = math.dist(p, c.p1) if c.p1 else math.inf
            if d0 == d1:
                return start
            key, p = (c.begin, c.p0) if d0 < d1 else (c.end, c.p1)
            if self._glued(key):
                return key
        return start

    def _joined(self, key, c):
        b = self._through(c.begin, c.p0, key) if c.begin in self.connectors else c.begin
        e = self._through(c.end, c.p1, key) if c.end in self.connectors else c.end
        if b == c.begin and e == c.end:
            return c
        return _copy(c, begin=b, end=e)

    def _part(self, key) -> bool:
        s = self.shapes[key]
        if s.part is not None:
            return s.part
        p = self.shapes.get(s.parent)
        if s.box is None or p is None or p.box is None:
            return False
        (x0, y0, x1, y1), (px0, py0, px1, py1) = s.box, p.box
        area = (px1 - px0) * (py1 - py0)
        tol = 0.01 * math.hypot(px1 - px0, py1 - py0)
        inside = x0 >= px0 - tol and y0 >= py0 - tol and x1 <= px1 + tol and y1 <= py1 + tol
        touches = min(abs(x0 - px0), abs(y0 - py0), abs(x1 - px1), abs(y1 - py1)) <= tol
        band = max(abs(x0 - px0), abs(x1 - px1)) <= tol or max(abs(y0 - py0), abs(y1 - py1)) <= tol
        return area > 0 and inside and (band or touches and (x1 - x0) * (y1 - y0) <= PORT * area)

    def _kids(self, pos) -> dict:
        kids = {}
        for k, s in self.shapes.items():
            if s.parent is not None and s.parent != k:
                kids.setdefault(s.parent, []).append(k)
        return {p: sorted(v, key=pos.get) for p, v in kids.items()}

    def _below(self, key, kids, stop, pos) -> list:
        out, todo, seen = [], [(key, 0)], {key}
        while todo:
            k, d = todo.pop()
            for c in kids.get(k, ()):
                if c in seen or c in stop:
                    continue
                seen.add(c)
                s = self.shapes[c]
                if s.hidden or s.container:
                    continue
                text = _plain(s.blocks)
                if text is not None and text.strip():
                    out.append(c)
                if d + 1 < limits.MAX_DEPTH:
                    todo.append((c, d + 1))
        return sorted(out, key=pos.get)

    def _named(self, edges) -> list:
        raw = {k for _, c in edges for k in (c.begin, c.end)}
        texted = set()
        for k, s in self.shapes.items():
            if k in raw or s.hidden:
                continue
            text = _plain(s.blocks)
            if text is None or not text.strip():
                continue
            seen, up = {k}, s.parent
            for _ in range(limits.MAX_DEPTH):
                p = self.shapes.get(up)
                if p is None or up in seen or up in texted or p.hidden or p.container:
                    break
                texted.add(up)
                seen.add(up)
                up = p.parent
        memo = {}

        def named(k):
            if k not in memo:
                s, cur = self.shapes[k], k
                if not s.hidden and k not in texted and not _label(s.blocks)[0]:
                    seen = {k}
                    for _ in range(limits.MAX_DEPTH):
                        up = self.shapes[cur].parent
                        p = self.shapes.get(up)
                        if (not self._part(cur) or p is None or up in seen or p.hidden or p.container
                                or up in self.connectors):
                            break
                        seen.add(up)
                        cur = up
                        if up in texted or _label(p.blocks)[0]:
                            break
                    if cur != k and s.part is None and s.name and cur not in texted \
                            and not _label(self.shapes[cur].blocks)[0]:
                        cur = k
                memo[k] = cur
            return memo[k]

        out = []
        for key, c in edges:
            b, e = named(c.begin), named(c.end)
            out.append((key, c if (b, e) == (c.begin, c.end) else _copy(c, begin=b, end=e)))
        return out

    def _floating(self, edges, stop) -> dict:
        ends = [self.shapes[k].box for _, c in edges for k in (c.begin, c.end) if self.shapes[k].box]
        cands = []
        for k, s in self.shapes.items():
            if (k in stop or k in self.connectors or s.parent in self.shapes or s.hidden or s.container
                    or s.box is None):
                continue
            text = _plain(s.blocks)
            if text is None or not text.strip() or "\n" in text.strip() or len(text) > NAME:
                continue
            cx, cy = _centre(s.box)
            if s.box[3] > s.box[1] and not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in ends):
                cands.append(k)
        paths = [(i, c.path) for i, (_, c) in enumerate(edges) if c.path]
        if not cands or len(cands) * sum(len(p) - 1 for _, p in paths) > FLOAT_WORK:
            return {}
        out, reach = {}, INFER_TOL * self._diag([(k, s.box) for k, s in self.shapes.items() if s.box])
        for k in cands:
            box = self.shapes[k].box
            centre = _centre(box)
            best, second, at = math.inf, math.inf, None
            for i, path in paths:
                d = min(_seg_point(a, b, centre) for a, b in zip(path, path[1:]))
                if d < best:
                    best, second, at = d, best, i
                elif d < second:
                    second = d
            if at is not None and best <= min(box[3] - box[1], reach) and second > best:
                out.setdefault(at, []).append(k)
        return out

    def _labelled(self, c, below, floating):
        if not below and not floating:
            return c
        sides = ([], [c.label] if c.label else [], [])
        for d in below:
            s = self.shapes[d]
            sides[_side(c, s.box)].append(" ".join(_plain(s.blocks).split()))
        for d in floating:
            sides[1].append(" ".join(_plain(self.shapes[d].blocks).split()))
        a, m, e = (" ".join(p) for p in sides)
        if _arrow(c.arrows)[1]:
            a, e = e, a
        label = " ".join(f"{a} / {m} / {e}".split()) if a or e else m
        taken, follow = below + floating, c.follow
        if len(label) > NAME:
            label = label[:NAME] + "…"
            follow = (follow or []) + [b for d in taken for b in self.shapes[d].blocks]
        data = [b for d in taken for b in self.shapes[d].data]
        if data:
            follow = (follow or []) + data
        return _copy(c, label=label, follow=follow)

    def place(self, slots: list) -> list:
        glued, cands = [], []
        for k, c in self.connectors.items():
            if c.hidden:
                continue
            c = self._joined(k, c)
            if self._glued(c.begin) and self._glued(c.end):
                glued.append((k, c))
            elif not c.line or any(a is True for a in c.arrows):
                cands.append((k, c))
        edges = glued + self._infer(cands)
        taken = self.budget.take(edges) if edges else []
        edges, nglued = taken, min(len(glued), len(taken))
        if not edges:
            return [b for _, blocks in slots for b in blocks]
        rank = {}
        for i, (key, _) in enumerate(slots):
            rank.setdefault(key, i)
        pos = {k: (rank.get(k, len(slots)), i) for i, k in enumerate(self.shapes)}
        kids = self._kids(pos)
        edges = self._named(edges)
        ends = {k for _, c in edges for k in (c.begin, c.end)}
        nodes = _ordered(ends, slots, self.shapes)
        nid = {k: f"n{i}" for i, k in enumerate(nodes, 1)}
        member, parent = self._containers(nodes, ends)
        groups = _ordered(set(parent) | set(member.values()), slots, self.shapes)
        cid = {k: f"c{i}" for i, k in enumerate(groups, 1)}

        labels, follows, long, inside = {}, [], set(), set()
        for k in nodes:
            s = self.shapes[k]
            blocks = s.blocks
            labels[k], follow = ("", False) if s.hidden else _label(blocks)
            data = [] if s.hidden else list(s.data)
            if not s.hidden and not labels[k]:
                below = self._below(k, kids, ends, pos)
                if below:
                    inside.update(below)
                    blocks = [b for d in below for b in self.shapes[d].blocks]
                    data += [b for d in below for b in self.shapes[d].data]
                    labels[k], follow = _label(blocks)
                elif s.name:
                    labels[k] = s.name
            if follow or data:
                long.add(k)
                follows += [Para([Text(f"{nid[k]}:")])] + (list(blocks) if follow else []) + data
        for k in groups:
            s = self.shapes[k]
            text = _plain(s.blocks)
            if s.title:
                labels[k] = s.title
                follow = bool(s.blocks) and (text is None or text.strip() not in ("", s.title))
            else:
                labels[k], follow = _label(s.blocks)
            if follow:
                follows += [Para([Text(f"{cid[k]}:")])] + list(s.blocks)
        stop = ends | inside | set(groups)
        below = [self._below(k, kids, stop, pos) for k, _ in edges]
        floating = self._floating(edges, stop.union(*below))
        for i, (k, c) in enumerate(edges):
            edges[i] = (k, self._labelled(c, below[i], floating.get(i, [])))
            inside.update(below[i], floating.get(i, []))
        for _, c in edges:
            if c.follow:
                follows += [Para([Text(" ".join(_edge(c, nid)) + ":")])] + c.follow

        items = None if groups else _tree(edges, nodes, nid, labels, long)
        if items is None:
            items = [Code(self._mermaid(edges, nglued, nodes, nid, groups, cid, member, parent, labels), "mermaid")]
        block = items + follows

        removed = {k for k in nodes if not self.shapes[k].hidden} | {k for k, _ in edges} | set(groups) | inside
        anchors = (set(nodes) | removed) - {None}
        removed.discard(None)
        out, placed = [], False
        for key, blocks in slots:
            if not placed and key in anchors:
                out += block
                placed = True
            if key not in removed:
                out += blocks
        if not placed:
            out += block
        return out

    def _infer(self, cands) -> list:
        """The candidates that become edges, each as a copy of its connector with both ends set. Counts
        `connector_loose` for each other candidate it snaps (a decoration, naming and landing on no shape and with no
        arrowhead set True, uncounted), and `infer_skipped` once when the scope is too large or the document's candidate
        room cannot take all of them; the ones not snapped keep their slots uncounted."""
        if not cands:
            return []
        boxed = [(k, s.box) for k, s in self.shapes.items() if s.box]
        if len(boxed) > INFER_SHAPES or self.budget.infer < len(cands):
            self.ctx.skipped["infer_skipped"] += 1
            if len(boxed) > INFER_SHAPES or self.budget.infer <= 0:
                return []
            cands = cands[:self.budget.infer]
        self.budget.infer -= len(cands)
        diag = self._diag(boxed)
        targets = [] if diag <= 0 else [(k, b, (b[2] - b[0]) * (b[3] - b[1])) for k, b in boxed
                                        if not self.shapes[k].container and k not in self.connectors]
        tol = INFER_TOL * diag
        out = []
        for k, c in cands:
            a = c.begin if self._glued(c.begin) else _snap(c.p0, targets, tol)
            b = c.end if self._glued(c.end) else _snap(c.p1, targets, tol)
            if a is not None and b is not None and a != b:
                out.append((k, _copy(c, begin=a, end=b)))
            elif (a is not None or b is not None or c.begin is not None or c.end is not None
                  or any(x is True for x in c.arrows)):
                self.ctx.skipped["connector_loose"] += 1  # meant to join something: not a decoration
        return out

    def _diag(self, boxed) -> float:
        xs = [v for _, b in boxed for v in (b[0], b[2])]
        ys = [v for _, b in boxed for v in (b[1], b[3])]
        for c in self.connectors.values():
            for p in (c.p0, c.p1):
                if p:
                    xs.append(p[0])
                    ys.append(p[1])
        return math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0

    def _containers(self, nodes, ends):
        """({node: its innermost kept container}, {kept container: its parent or None}) for the nodes held. Nothing
        when no node has a box, or when the candidates pass INFER_SHAPES: the containers stay ordinary shapes."""
        if not any(self.shapes[n].box for n in nodes):
            return {}, {}
        cands = [k for k, s in self.shapes.items()
                 if s.container and not s.hidden and s.box and k not in ends and k not in self.connectors]
        if not cands or len(cands) > INFER_SHAPES:
            return {}, {}
        area = {k: (self.shapes[k].box[2] - self.shapes[k].box[0]) * (self.shapes[k].box[3] - self.shapes[k].box[1])
                for k in cands}

        def holder(box, larger=None):
            """The smallest candidate holding the box's centre (larger than `larger` when given); first one on a tie."""
            cx, cy = _centre(box)
            best = None
            for k in cands:
                b = self.shapes[k].box
                if (b[0] <= cx <= b[2] and b[1] <= cy <= b[3] and (larger is None or area[k] > larger)
                        and (best is None or area[k] < area[best])):
                    best = k
            return best

        up = {k: holder(self.shapes[k].box, area[k]) for k in cands}  # strictly larger: no cycle
        depth = {}
        for k in cands:
            chain = []
            c = k
            while c is not None and c not in depth:
                chain.append(c)
                c = up[c]
            d = 0 if c is None else depth[c]
            for c in reversed(chain):
                d += 1
                depth[c] = d
        member, parent = {}, {}
        for n in nodes:
            box = self.shapes[n].box
            c = holder(box) if box else None
            while c is not None and depth[c] > limits.MAX_DEPTH:
                c = up[c]
            if c is None:
                continue
            member[n] = c
            while c is not None and c not in parent:
                parent[c] = up[c]
                c = up[c]
        return member, parent

    def _mermaid(self, edges, nglued, nodes, nid, groups, cid, member, parent, labels) -> str:
        dx = dy = 0.0
        for _, c in edges:
            a, b = self.shapes[c.begin].box, self.shapes[c.end].box
            if a and b:
                (ax, ay), (bx, by) = _centre(a), _centre(b)
                dx += abs(ax - bx)
                dy += abs(ay - by)
        lines = ["flowchart LR" if dx > dy else "flowchart TD"]

        kids = {None: []}  # container (None: the top) -> [("c", key) | ("n", key)] in node order
        for n in nodes:
            chain = []
            c = member.get(n)
            while c is not None:
                chain.append(c)
                c = parent[c]
            up = None
            for c in reversed(chain):
                if c not in kids:
                    kids[c] = []
                    kids[up].append(("c", c))
                up = c
            kids[up].append(("n", n))

        def emit(at, indent):
            pad = "  " * indent
            for what, k in kids[at]:
                if what == "c":
                    lines.append(f'{pad}subgraph {cid[k]} ["{_esc(labels[k])}"]')
                    emit(k, indent + 1)
                    lines.append(f"{pad}end")
                elif labels[k]:
                    o, e = _KINDS[self.shapes[k].kind]
                    lines.append(f'{pad}{nid[k]}{o}"{_esc(labels[k])}"{e}')
                elif at is not None:  # a blank node is declared only to place it in its subgraph, else edges make it
                    lines.append(f'{pad}{nid[k]}[" "]')

        emit(None, 1)
        for i, (_, c) in enumerate(edges):
            if i == nglued:
                lines.append("  %% inferred from position")
            a, arrow, b = _edge(c, nid)
            label = f'|"{_esc(c.label)}"|' if c.label else ""
            lines.append(f"  {a} {arrow}{label} {b}")
        return "\n".join(lines)


def _ordered(keys: set, slots, shapes) -> list:
    """The keys in the order they first appear in the slots, then the slotless ones in shape() order."""
    out, seen = [], set()
    for key, _ in slots:
        if key in keys and key not in seen:
            seen.add(key)
            out.append(key)
    for key in shapes:
        if key in keys and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _tree(edges, nodes, nid, labels, long):
    """The list items when the edges form a branching tree, else None."""
    kids = {n: [] for n in nodes}
    parent = {}
    for _, c in edges:
        arrow, swapped = _arrow(c.arrows)
        if c.label or arrow != "-->":
            return None
        a, b = (c.end, c.begin) if swapped else (c.begin, c.end)
        if b in parent:
            return None
        parent[b] = a
        kids[a].append(b)
    roots = [n for n in nodes if n not in parent]
    if len(roots) != 1 or not any(len(v) >= 2 for v in kids.values()):
        return None
    rank = {n: i for i, n in enumerate(nodes)}
    items, stack = [], [(roots[0], 0)]
    while stack:
        n, level = stack.pop()
        if level + 1 > smartart.MAX_LEVEL or len(items) >= len(nodes):
            return None
        inlines = _inlines(f"{nid[n]} {labels[n]}" if n in long else labels[n] or "(unlabelled shape)")
        items.append(ListItem(level, "-", inlines))
        stack += [(k, level + 1) for k in sorted(kids[n], key=rank.get, reverse=True)]
    return items if len(items) == len(nodes) else None
