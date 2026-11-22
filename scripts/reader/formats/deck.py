"""What the slide readers (pptx, ppt, odp, Keynote) share: the reading order of a slide's shapes, and the content
the masters, layouts and footers put on more than one slide.

`order` is a recursive XY-cut over the shapes' boxes. A set of boxes is split in two at its widest gap, between rows
(a cut across y) or between columns (a cut across x), rows on a tie, and each part is read the same way; a set with no
gap reads by (top, left). A box counts from a quarter of the smallest height (width) in the set in from each edge, so
two rows (columns) that overlap by less than half the smallest box still part, a row whose boxes sit a fraction of a
line off is one row, and a small box at the edge of a large one does not cut it.
A shape with no box keeps its place in the source: it goes just before the first read of the boxed shapes that follow
it, or last. The work (boxes sorted, summed over every split) stops at XYCUT_WORK; the sets left then read by (top,
left).

`Repeated` gathers what each slide shows from its master, layout and footer, keyed by what it shows (text, pictures by
their bytes). `place` puts an item one slide shows on that slide, after its own blocks, and writes the items several
slides show once, before the slides, under one `Section("repeated")` per set of slides: "every slide" when it is all
of them, else the slide numbers ("slides 2-4, 7"). A set whose slides are all hidden is hidden.
"""
import dataclasses
import hashlib

from .. import limits
from ..model import Bold, Link, ListItem, Para, Section, Text


def _part(ids, boxes, axis):
    """The widest gap of `ids` across `axis` (1: y, 0: x) as (gap, first part, second part), or None without one."""
    lo, hi = axis, axis + 2
    tol = min((boxes[i][hi] - boxes[i][lo] for i in ids if boxes[i][hi] > boxes[i][lo]), default=0) / 4
    spans = []
    for i in ids:
        b = boxes[i]
        q = min(tol, (b[hi] - b[lo]) / 4)
        spans.append((b[lo] + q, b[hi] - q, b[lo], b[hi], i))
    spans.sort()
    low = [0.0] * len(spans)
    run = float("inf")
    for k in range(len(spans) - 1, -1, -1):
        run = min(run, spans[k][2])
        low[k] = run
    best, reach, top = None, spans[0][1], spans[0][3]
    for k in range(1, len(spans)):
        if spans[k][0] > reach:
            gap = low[k] - top
            if best is None or gap > best[0]:
                best = (gap, k)
        reach, top = max(reach, spans[k][1]), max(top, spans[k][3])
    if best is None:
        return None
    k = best[1]
    return best[0], [s[4] for s in spans[:k]], [s[4] for s in spans[k:]]


def order(boxes) -> list:
    """The indexes of `boxes` ((x0, y0, x1, y1) each, or None) in reading order."""
    norm = {i: (min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))
            for i, b in enumerate(boxes) if b is not None}
    seq, work, stack = [], limits.XYCUT_WORK, [list(norm)]
    while stack:
        ids = stack.pop()
        cut = None
        if len(ids) > 1 and work >= len(ids):
            work -= len(ids)
            rows, cols = _part(ids, norm, 1), _part(ids, norm, 0)
            cut = rows if cols is None or (rows is not None and rows[0] >= cols[0]) else cols
        if cut is None:
            seq.extend(sorted(ids, key=lambda i: (norm[i][1], norm[i][0], i)))
        else:
            stack += [cut[2], cut[1]]
    rank = {i: r for r, i in enumerate(seq)}
    keys, ahead = [], len(seq)
    for i in range(len(boxes) - 1, -1, -1):
        if i in rank:
            ahead = min(ahead, rank[i])
            keys.append((rank[i], 1, i))
        else:
            keys.append((ahead, 0, i))
    return [i for *_, i in sorted(keys)]


def _text(inlines) -> bool:
    return any((isinstance(it, Text) and it.text.strip()) or (isinstance(it, (Bold, Link)) and _text(it.inlines))
               for it in inlines)


def _wrap(inlines, url):
    """`inlines` with each run of them outside a Link put inside one to `url`."""
    out, run = [], []
    for it in inlines:
        if isinstance(it, Link):
            if run:
                out.append(Link(url, run))
                run = []
            out.append(it)
        else:
            run.append(it)
    if run:
        out.append(Link(url, run))
    return out


def linked(blocks, url, jump, label) -> list:
    """A clicked shape's blocks: when they are all text, the text inside the link to `url` and the jump words after
    the last paragraph; else (no text, or a picture or table with it) one line more of `label`, linked, with the
    jump."""
    texts = [b for b in blocks if isinstance(b, (Para, ListItem)) and _text(b.inlines)]
    if texts and len(texts) == len(blocks):
        if url:
            for b in texts:
                b.inlines = _wrap(b.inlines, url)
        if jump:
            texts[-1].inlines.append(Text(f" (→ {jump})"))
        return blocks
    line = [Link(url, [Text(label)])] if url else [Text(label)]
    if jump:
        line.append(Text(f" (→ {jump})"))
    return blocks + [Para(line)]


def _key(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return hashlib.sha1(v).digest()
    if isinstance(v, (list, tuple)):
        return tuple(_key(x) for x in v)
    if isinstance(v, frozenset):
        return tuple(sorted(v))
    if dataclasses.is_dataclass(v):
        return (type(v).__name__, *(_key(getattr(v, f.name)) for f in dataclasses.fields(v)))
    return v


def _span(numbers) -> str:
    out, k = [], 0
    while k < len(numbers):
        j = k
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        out.append(str(numbers[k]) if j == k else f"{numbers[k]}-{numbers[j]}")
        k = j + 1
    return "slides " + ", ".join(out)


class Repeated:
    def __init__(self):
        self.items = {}

    def add(self, number, blocks):
        """One item `number`'s slide shows from its master, layout or footer."""
        if blocks:
            self.items.setdefault(_key(blocks), (blocks, set()))[1].add(number)

    def place(self, slides: list) -> list:
        """Put the items on `slides` (their Sections): the Sections that go before them."""
        by_number = {s.number: s for s in slides}
        groups = {}
        for blocks, numbers in self.items.values():
            numbers = sorted(n for n in numbers if n in by_number)
            if len(numbers) == 1:
                by_number[numbers[0]].blocks.extend(blocks)
            elif numbers:
                groups.setdefault(tuple(numbers), []).extend(blocks)
        out = []
        for numbers, blocks in sorted(groups.items(), key=lambda g: (len(g[0]) != len(slides), g[0])):
            title = "every slide" if len(numbers) == len(slides) else _span(list(numbers))
            hidden = "hidden" if all(by_number[n].hidden for n in numbers) else ""
            out.append(Section("repeated", title, blocks=blocks, hidden=hidden))
        return out
