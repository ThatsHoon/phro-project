"""Shapes joined by connectors, one drawing scope at a time, as one Mermaid `flowchart` block or a nested list.

Readers only extract: per scope they register each shape (`Diagram.shape`) and connector (`Diagram.connector`) under
their own shape keys, then hand the scope's flow to `Diagram.place` as `(key or None, blocks)` slots. The module
numbers, escapes, orients and renders, and builds model blocks only.

- **Edges.** A connector glued at both ends to registered shapes is an edge; a glue reference to a missing key or to a
  connector (itself included) is not glue. A hidden connector is never an edge. Edges keep `connector()` order and go
  through the document's `ConnectionBudget`.
- **Nodes.** The edges' ends, numbered `n1`, `n2`, ... in the order their keys first appear in the slots, then the
  slotless ones in `shape()` order. Plain text of at most NAME characters is the whole label; any other content gets
  its first line cut at NAME plus `…` (or `(picture)`, `(table)`, `(chart)`, `(content)`), and its blocks follow the
  diagram after a `nK:` paragraph. A hidden shape, or one with no text, is a blank node; a hidden one keeps its slot.
- **Containers.** A container shape (not hidden, with a box, not an edge end) holding a node's centre becomes
  `subgraph cK ["title"]`; the smallest box wins, containers nest the same way up to `limits.MAX_DEPTH`, and the nodes
  of deeper ones go to the deepest kept one. A container with no node is an ordinary shape.
- **Trees.** Directed unlabelled edges, one parent at most, one root reaching every node, a node with two or more
  children, at most `smartart.MAX_LEVEL` levels and no subgraph: a `-` list instead of Mermaid.
- **Placement.** The block goes at the first slot whose key is a node, an edge or a subgraph; the slots of the shown
  nodes, the edges and the subgraphs are taken out. With no edge the slots come back unchanged.

Loose ends (`p0`, `p1`, `line`) are stored for position inference; INFER_TOL and INFER_SHAPES are its caps.
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

_NEWLINE = re.compile(r"\r\n|\r|\n")
_KINDS = {"": ("[", "]"), "decision": ("{", "}"), "terminator": ("([", "])"), "circle": ("((", "))"),
          "store": ("[(", ")]")}


class ConnectionBudget:
    """The document's CONNECTIONS room (read when made): `take` keeps what fits of one scope's edges and counts
    `connections_cut` once for a list it cuts."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.room = CONNECTIONS

    def take(self, edges) -> list:
        if len(edges) <= self.room:
            self.room -= len(edges)
            return edges
        kept = edges[:max(0, self.room)]
        self.room = 0
        self.ctx.skipped["connections_cut"] += 1
        return kept


class _Shape:
    __slots__ = ("blocks", "kind", "box", "hidden", "container", "title")

    def __init__(self, blocks, kind, box, hidden, container, title):
        self.blocks, self.kind, self.box = blocks, kind, box
        self.hidden, self.container, self.title = hidden, container, title


class _Connector:
    __slots__ = ("begin", "end", "p0", "p1", "arrows", "label", "line", "hidden")

    def __init__(self, begin, end, p0, p1, arrows, label, line, hidden):
        self.begin, self.end, self.p0, self.p1 = begin, end, p0, p1
        self.arrows, self.label, self.line, self.hidden = arrows, label, line, hidden


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
              title: str = "") -> None:
        if key in self.shapes:
            return
        self.shapes[key] = _Shape(blocks, kind if kind in _KINDS else "", _box(box), bool(hidden), bool(container),
                                  (title or "").strip())

    def connector(self, key, begin=None, end=None, p0=None, p1=None, arrows=(None, None), label: str = "",
                  line: bool = False, hidden: bool = False) -> None:
        if key in self.connectors:
            return
        self.connectors[key] = _Connector(begin, end, p0, p1, arrows, (label or "").strip(), bool(line),
                                          bool(hidden))

    def _glued(self, key) -> bool:
        return key is not None and key in self.shapes and key not in self.connectors

    def place(self, slots: list) -> list:
        edges = [(k, c) for k, c in self.connectors.items()
                 if not c.hidden and self._glued(c.begin) and self._glued(c.end)]
        edges = self.budget.take(edges) if edges else []
        if not edges:
            return [b for _, blocks in slots for b in blocks]
        ends = {k for _, c in edges for k in (c.begin, c.end)}
        nodes = _ordered(ends, slots, self.shapes)
        nid = {k: f"n{i}" for i, k in enumerate(nodes, 1)}
        member, parent = self._containers(nodes, ends)
        groups = _ordered(set(parent) | set(member.values()), slots, self.shapes)
        cid = {k: f"c{i}" for i, k in enumerate(groups, 1)}

        labels, follows, long = {}, [], set()
        for k in nodes:
            s = self.shapes[k]
            labels[k], follow = ("", False) if s.hidden else _label(s.blocks)
            if follow:
                long.add(k)
                follows += [Para([Text(f"{nid[k]}:")])] + list(s.blocks)
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

        items = None if groups else _tree(edges, nodes, nid, labels, long)
        if items is None:
            items = [Code(self._mermaid(edges, nodes, nid, groups, cid, member, parent, labels), "mermaid")]
        block = items + follows

        removed = {k for k in nodes if not self.shapes[k].hidden} | {k for k, _ in edges} | set(groups)
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

    def _containers(self, nodes, ends):
        """({node: its innermost kept container}, {kept container: its parent or None}) for the nodes held."""
        cands = [k for k, s in self.shapes.items()
                 if s.container and not s.hidden and s.box and k not in ends and k not in self.connectors]
        if not cands:
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

    def _mermaid(self, edges, nodes, nid, groups, cid, member, parent, labels) -> str:
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
                else:
                    lines.append(f'{pad}{nid[k]}[" "]')

        emit(None, 1)
        for _, c in edges:
            arrow, swapped = _arrow(c.arrows)
            a, b = nid[c.begin], nid[c.end]
            if swapped:
                a, b = b, a
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
        inlines = _inlines(f"{nid[n]} {labels[n]}" if n in long else labels[n])
        items.append(ListItem(level, "-", inlines))
        stack += [(k, level + 1) for k in sorted(kids[n], key=rank.get, reverse=True)]
    return items if len(items) == len(nodes) else None
