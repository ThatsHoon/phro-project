"""SmartArt (DrawingML diagram) text as a nested list, in tree order.

Only the data part is read: its `dgm:pt` points that hold typed text (no type, `node` or `asst`) and the `parOf`
connections between them, ordered by `srcOrd`. The layout, drawing and style parts are never opened.
"""
import re

from .. import limits
from ..containers.zipx import attr, kids, local, parse_xml
from ..limits import ReadError
from ..model import ListItem, Text
from . import ooxml_base

_POINTS = (None, "node", "asst")
MAX_LEVEL = 8


def _text(pt) -> str:
    """The `dgm:t` paragraphs of a point (the a:t of each run and field), joined with a space."""
    paras = []
    for t in kids(pt, "t"):
        for p in kids(t, "p"):
            s = "".join(x.text or "" for r in p if local(r.tag) in ("r", "fld") for x in r if local(x.tag) == "t")
            s = re.sub(r"\s+", " ", s).strip()
            if s:
                paras.append(s)
    return re.sub(r"\s+", " ", " ".join(paras)).strip()


def _build(root):
    """([(level, text)] up to SMARTART_NODES of them, cut) of a parsed data model."""
    cap = 4 * limits.SMARTART_NODES
    cut = False
    nodes = []  # (modelId, text); the doc point's text is not a node's
    doc = None
    for lst in kids(root, "ptLst")[:1]:
        for n, pt in enumerate(kids(lst, "pt")):
            if n >= cap:
                cut = True
                break
            typ = attr(pt, "type")
            if typ == "doc":
                if doc is None:
                    doc = attr(pt, "modelId")
            elif typ in _POINTS:
                nodes.append((attr(pt, "modelId"), _text(pt)))
    by_id = {}
    for i, (mid, _) in enumerate(nodes):
        if mid is not None and mid not in by_id:
            by_id[mid] = i
    children = {}  # parent id -> [(sort key, child index)]
    for lst in kids(root, "cxnLst")[:1]:
        for n, cx in enumerate(kids(lst, "cxn")):
            if n >= cap:
                cut = True
                break
            if attr(cx, "type") not in (None, "parOf"):
                continue
            src, dest = attr(cx, "srcId"), attr(cx, "destId")
            if dest not in by_id or src is None or (src != doc and src not in by_id):
                continue
            try:
                key = (0, int(attr(cx, "srcOrd")), n)
            except (TypeError, ValueError):
                key = (1, 0, n)
            children.setdefault(src, []).append((key, by_id[dest]))
    for v in children.values():
        v.sort()
    return _walk(nodes, children, doc, limits.SMARTART_NODES, cut)


def _walk(nodes, children, doc, room, cut):
    """The tree from the doc point, iteratively, then the text nodes it did not reach at level 0."""
    seen = set()
    out = []

    def add(level, text):
        nonlocal cut
        if len(out) >= room:
            cut = True
            return False
        out.append((min(level, MAX_LEVEL), text))
        return True

    # stack entries: (node index or None for the doc root, level)
    stack = [(None, 0)] if doc is not None else []
    while stack:
        idx, level = stack.pop()
        if idx is None:
            parent, nxt = doc, level
        else:
            if idx in seen:
                continue
            seen.add(idx)
            mid, text = nodes[idx]
            parent, nxt = mid, level
            if text:
                if not add(level, text):
                    break
                nxt = level + 1
        for _, c in reversed(children.get(parent, ()) if parent is not None else ()):
            if c not in seen:
                stack.append((c, nxt))
    for i, (_, text) in enumerate(nodes):
        if i not in seen and text and not add(0, text):
            break
    return out, cut


def _model(pkg, name):
    c = ooxml_base._cache(pkg).setdefault("smartart", {})
    if name not in c:
        data = ooxml_base._member(pkg, name)
        res = None
        if data:
            try:
                res = _build(parse_xml(data))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
            except Exception:
                res = None
        c[name] = res
    return c[name]


def items(pkg, part: str, rid, ctx, room: int) -> list:
    """The text items of the diagram whose data part is relationship `rid` of `part`, at most `room` of them.

    A missing, external, damaged or oversized data part counts one `smartart` and gives []. Items dropped by `room`,
    or by the point and connection caps, count one `smartart_cut`.
    """
    name = ooxml_base._target(pkg, part, rid, "/diagramData")
    got = _model(pkg, name) if name else None
    if got is None:
        ctx.skipped["smartart"] += 1
        return []
    found, cut = got
    room = max(0, room)
    if cut or len(found) > room:
        ctx.skipped["smartart_cut"] += 1
    return [ListItem(level, "-", [Text(text)]) for level, text in found[:room]]
