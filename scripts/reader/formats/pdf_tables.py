"""pdf table geometry: the rule segments of a page, gathered from its paths.

Segment and PathCollector follow dochan 1.21.0 pdf/paths.py (MIT). A collector keeps each
subpath's bounding box and edges, and at a paint operator gives out the horizontal and
vertical rules: every edge of a stroke, and the long edges of a thin filled subpath (a
rectangle drawn as a line). It also keeps the union box and count of the painted paths that
gave no rule, the vector figure of the page.

A page holds at most PDF_SEGMENTS segments; past it the rest are ignored and not counted.

`tables` is the grid assembly after dochan 1.21.0 pdf/tables.py (`_build_tables_python`) and
pdf/connected_tables.py (MIT), with dochan's Table and Cell replaced by model.Table and
model.Cell and its C accelerator and warnings left out. Both pdf engines use it. Its caps are
dochan's, under the same names, and each cut is counted once under `cells`.
"""
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from math import hypot, isclose, isfinite
from typing import List, Optional

from .. import limits
from ..model import Cell, Para, Table
from . import pdf_layout

PDF_SEGMENTS = 20_000  # edges and subpaths per page, and rule segments kept
_THIN = 2.0            # a filled shape this thin or thinner is a line (device units)
_STRAIGHT = 1.0        # a curve whose control points stay this close to the chord is straight
_COORD = 1e9           # a coordinate beyond this is junk


@dataclass
class Segment:
    """A line segment in device coordinates, normalised so x0 <= x1 and y0 <= y1."""
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self):
        if self.x1 < self.x0:
            self.x0, self.x1 = self.x1, self.x0
        if self.y1 < self.y0:
            self.y0, self.y1 = self.y1, self.y0

    @property
    def length(self) -> float:
        return hypot(self.x1 - self.x0, self.y1 - self.y0)


def _sane(*values) -> bool:
    return all(isfinite(v) and abs(v) <= _COORD for v in values)


def _axis_segment(p0, p1) -> Optional[Segment]:
    """The axis-aligned segment between two points; None for a diagonal, a point or a bad
    coordinate."""
    x0, y0 = p0
    x1, y1 = p1
    if not _sane(x0, y0, x1, y1):
        return None
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    # Under 1 unit a piece may still be part of a dashed stroke; a short diagonal must not
    # pass the 0.6 axis tolerance, so only an exact axis is taken there.
    if dx == 0:
        return Segment(x0, y0, x1, y1) if dy > 0 else None
    if dy == 0:
        return Segment(x0, y0, x1, y1)
    if dx <= 0.6 or dy <= 0.6:
        if hypot(dx, dy) >= 1:
            return Segment(x0, y0, x1, y1)
    return None


def _deviation(p0, p1, control) -> float:
    """How far a control point lies from the chord p0 to p1."""
    (x0, y0), (x1, y1), (cx, cy) = p0, p1, control
    dx, dy = x1 - x0, y1 - y0
    length = hypot(dx, dy)
    if length < 1e-9:
        return hypot(cx - x0, cy - y0)
    return abs(dx * (cy - y0) - dy * (cx - x0)) / length


class _Subpath:
    """The bounding box and edges of one subpath. Points are not stored."""
    __slots__ = ("x0", "y0", "x1", "y1", "edges")

    def __init__(self, point):
        self.x0 = self.x1 = point[0]
        self.y0 = self.y1 = point[1]
        self.edges: List[Segment] = []

    def include(self, point) -> None:
        x, y = point
        if _sane(x, y):
            self.x0, self.x1 = min(self.x0, x), max(self.x1, x)
            self.y0, self.y1 = min(self.y0, y), max(self.y1, y)

    @property
    def thin(self) -> bool:
        return min(self.x1 - self.x0, self.y1 - self.y0) <= _THIN


class PathCollector:
    """Collects subpaths and, at each paint operator, gives out the rules."""

    def __init__(self):
        self.segments: List[Segment] = []
        self.short_segments: List[Segment] = []
        self._short_count = 0
        self._short_disabled = False
        self._groups: List[_Subpath] = []
        self._current: Optional[_Subpath] = None
        self._edge_count = 0
        self._overflow = False
        self._point = None
        self._start = None
        self._figure_box = None
        self._figure_count = 0

    @property
    def figure(self):
        """(union box or None, count) of the painted paths that gave no rule segment."""
        return self._figure_box, self._figure_count

    def add_figure(self, box, count: int) -> None:
        """Merge the figure of a form drawn inside this page."""
        if box is not None and count:
            self._merge_box(box)
            self._figure_count += count

    def _merge_box(self, box) -> None:
        if self._figure_box is None:
            self._figure_box = tuple(box)
        else:
            a = self._figure_box
            self._figure_box = (min(a[0], box[0]), min(a[1], box[1]),
                                max(a[2], box[2]), max(a[3], box[3]))

    def _move(self, point) -> None:
        if len(self._groups) < PDF_SEGMENTS:
            self._current = _Subpath(point)
            self._groups.append(self._current)
        else:
            # past the cap the rest of the path is followed but not kept
            self._current = None
            self._overflow = True
        self._point = self._start = point

    def _line(self, point, emit=True, extra_points=()) -> None:
        if self._point is None:
            self._move(point)
            return
        if self._current is None and not self._overflow:
            self._move(self._point)
        subpath = self._current
        if subpath is not None:
            for extra in extra_points:
                subpath.include(extra)
            subpath.include(point)
            if emit:
                segment = _axis_segment(self._point, point)
                if segment is not None:
                    if segment.length < 1:
                        # short pieces of a dash do not spend the rule budget; past the cap
                        # only this supplement is given up
                        if not self._short_disabled:
                            self._short_count += 1
                            if self._short_count <= PDF_SEGMENTS:
                                subpath.edges.append(segment)
                            else:
                                self._short_disabled = True
                                self.short_segments.clear()
                    elif self._edge_count < PDF_SEGMENTS:
                        subpath.edges.append(segment)
                        self._edge_count += 1
                    else:
                        self._overflow = True
        self._point = point

    def _curve(self, controls, end) -> None:
        """A curve is a line only when its control points hug the chord; a bent curve gives
        no edge."""
        if self._point is None:
            self._move(end)
            return
        straight = all(_deviation(self._point, end, c) <= _STRAIGHT for c in controls)
        self._line(end, emit=straight, extra_points=controls)

    def operate(self, op, values, ctm) -> None:
        """One path construction or painting operator; `values` are its numeric operands."""
        def point(x, y):
            a, b, c, d, e, f = ctm
            return x * a + y * c + e, x * b + y * d + f

        if op == b"m" and len(values) >= 2:
            self._move(point(*values[-2:]))
        elif op == b"l" and len(values) >= 2:
            self._line(point(*values[-2:]))
        elif op == b"c" and len(values) >= 6:
            x1, y1, x2, y2, x3, y3 = values[-6:]
            self._curve((point(x1, y1), point(x2, y2)), point(x3, y3))
        elif op == b"v" and len(values) >= 4:
            x2, y2, x3, y3 = values[-4:]
            self._curve((point(x2, y2),), point(x3, y3))
        elif op == b"y" and len(values) >= 4:
            x1, y1, x3, y3 = values[-4:]
            self._curve((point(x1, y1),), point(x3, y3))
        elif op == b"re" and len(values) >= 4:
            x, y, w, h = values[-4:]
            corners = [point(x, y), point(x + w, y), point(x + w, y + h), point(x, y + h)]
            self._move(corners[0])
            for corner in corners[1:] + corners[:1]:
                self._line(corner)
        elif op == b"h" and self._start is not None:
            self._line(self._start)
        elif op in (b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"):
            if op in (b"s", b"b", b"b*") and self._start is not None:
                self._line(self._start)
            if op != b"n":
                self._paint(stroke=op not in (b"f", b"F", b"f*"))
            self.discard()

    def discard(self) -> None:
        """End the current path without painting it."""
        self._groups = []
        self._current = None
        self._edge_count = 0
        self._overflow = False
        self._point = self._start = None

    def _paint(self, stroke: bool) -> None:
        """A stroke gives every edge; a fill gives the long edges of a thin subpath only."""
        before = len(self.segments)
        capped = False
        for subpath in self._groups:
            if not subpath.edges or not (stroke or subpath.thin):
                continue
            for segment in subpath.edges:
                if not stroke and segment.length <= _THIN:
                    continue  # the short side of a thin rectangle is no column border
                if segment.length < 1:
                    if not self._short_disabled:
                        self.short_segments.append(segment)
                    continue
                if len(self.segments) >= PDF_SEGMENTS:
                    capped = True  # the rest is ignored and not counted
                    break
                self.segments.append(segment)
            if capped:
                break
        if capped or len(self.segments) > before:
            return
        boxes = [(g.x0, g.y0, g.x1, g.y1) for g in self._groups
                 if _sane(g.x0, g.y0, g.x1, g.y1) and (g.x1 > g.x0 or g.y1 > g.y0)]
        if boxes:
            self._merge_box((min(b[0] for b in boxes), min(b[1] for b in boxes),
                             max(b[2] for b in boxes), max(b[3] for b in boxes)))
            self._figure_count += 1


# ---- tables from the rules

MAX_PAGE_CELLS = 50_000
MAX_DOCUMENT_CELLS = 200_000
MAX_COMPONENT_LINES = 2_000
MAX_INTERSECTION_CHECKS = 2_000_000  # horizontal x vertical rule checks per page
MAX_NESTED_DEPTH = 32
MAX_PAGE_COMPONENTS = 1_000          # table candidates per page
MAX_SPLIT_CHECKS = 2_000_000         # graph and grid checks of the connected-grid split, per page
_TOLERANCE = 1.5


@dataclass
class TableBudget:
    """What the tables of a document may still take: cells, and table nodes (limits.TABLE_NODES)."""
    remaining: int = MAX_DOCUMENT_CELLS
    nodes: int = limits.TABLE_NODES


@dataclass
class Found:
    """A table of a page. `used` holds the `order` of every Fragment whose text went into the
    table, nested tables included (Fragment.order, not Line.order: a Line's order is only the
    smallest of its fragments'). Task 12 drops the fragments with those orders from the page flow
    and puts the table where the smallest of them was."""
    table: Table
    bbox: tuple   # x0, y0, x1, y1, y up
    used: set


class _Cell:
    __slots__ = ("row", "col", "row_span", "col_span", "blocks")

    def __init__(self, row, col, row_span, col_span):
        self.row, self.col, self.row_span, self.col_span = row, col, row_span, col_span
        self.blocks = []

    @property
    def merged_away(self):
        return self.row_span == 0 or self.col_span == 0


@dataclass
class _Node:
    bbox: tuple
    xs: list
    ys: list
    grid: list
    owners: list
    boxes: dict
    cells: int
    parent: Optional["_Node"] = None
    owner_key: Optional[int] = None
    depth: int = 0
    orders: set = field(default_factory=set)
    anchor: int = -1
    children: list = field(default_factory=list)


def _count(cut) -> None:
    if cut is not None:
        cut["cells"] += 1


class _Union:
    def __init__(self, count):
        self.parents = list(range(count))
        self.sizes = [1] * count

    def find(self, item):
        while self.parents[item] != item:
            self.parents[item] = self.parents[self.parents[item]]
            item = self.parents[item]
        return item

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.sizes[a] < self.sizes[b]:
            a, b = b, a
        self.parents[b] = a
        self.sizes[a] += self.sizes[b]


def _snap_runs(lines, tolerance):
    """(fixed coordinate, start, end) clustered, with collinear pieces joined."""
    clusters = []
    for line in sorted(lines):
        if not clusters or line[0] - clusters[-1][0][0] > tolerance:
            clusters.append([])
        clusters[-1].append(line)
    runs = []
    for cluster in clusters:
        coord = sum(v[0] for v in cluster) / len(cluster)
        spans = sorted((v[1], v[2]) for v in cluster)
        start, end = spans[0]
        for lo, hi in spans[1:]:
            if lo <= end + tolerance:
                end = max(end, hi)
            else:
                runs.append((coord, start, end))
                start, end = lo, hi
        runs.append((coord, start, end))
    return runs


def _components(horizontal, vertical, tolerance):
    """Connected components of the rules that touch. None past MAX_INTERSECTION_CHECKS (the
    bound is measured before any rule is tested)."""
    xs = [v[0] for v in vertical]
    spans = []
    checks = 0
    for y, left, right in horizontal:
        lo, hi = bisect_left(xs, left - tolerance), bisect_right(xs, right + tolerance)
        spans.append((lo, hi))
        checks += hi - lo
        if checks > MAX_INTERSECTION_CHECKS:
            return None
    union = _Union(len(horizontal) + len(vertical))
    for i, (y, left, right) in enumerate(horizontal):
        lo, hi = spans[i]
        for j in range(lo, hi):
            _, bottom, top = vertical[j]
            if bottom - tolerance <= y <= top + tolerance:
                union.join(i, len(horizontal) + j)
    groups = defaultdict(lambda: ([], []))
    for i, line in enumerate(horizontal):
        groups[union.find(i)][0].append(line)
    for j, line in enumerate(vertical):
        groups[union.find(len(horizontal) + j)][1].append(line)
    return list(groups.values())


def _covered(runs, lo, hi, tolerance):
    coverage = sum(max(0, min(hi, end + tolerance) - max(lo, start - tolerance))
                   for start, end in runs)
    return coverage >= (hi - lo) * 0.5


def _rectangles(indices, cols):
    """A merged region that is not a rectangle falls back to the runs of each row."""
    rows = defaultdict(list)
    for index in indices:
        r, c = divmod(index, cols)
        rows[r].append(c)
    r0, r1 = min(rows), max(rows)
    c0 = min(min(cs) for cs in rows.values())
    c1 = max(max(cs) for cs in rows.values())
    if len(indices) == (r1 - r0 + 1) * (c1 - c0 + 1):
        return [(r0, c0, r1 + 1, c1 + 1)]
    result = []
    for r, cs in sorted(rows.items()):
        start = end = min(cs)
        for c in sorted(cs)[1:]:
            if c != end + 1:
                result.append((r, start, r + 1, end + 1))
                start = c
            end = c
        result.append((r, start, r + 1, end + 1))
    return result


def _make_grid(horizontal, vertical, xs, ys, tolerance):
    rows, cols = len(ys) - 1, len(xs) - 1
    union = _Union(rows * cols)
    h_runs, v_runs = defaultdict(list), defaultdict(list)
    for coord, lo, hi in horizontal:
        h_runs[coord].append((lo, hi))
    for coord, lo, hi in vertical:
        v_runs[coord].append((lo, hi))
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c
            if c + 1 < cols and not _covered(v_runs[xs[c + 1]], ys[r + 1], ys[r], tolerance):
                union.join(index, index + 1)
            if r + 1 < rows and not _covered(h_runs[ys[r + 1]], xs[c], xs[c + 1], tolerance):
                union.join(index, index + cols)
    regions = defaultdict(list)
    for i in range(rows * cols):
        regions[union.find(i)].append(i)
    grid = [[_Cell(r, c, 0, 0) for c in range(cols)] for r in range(rows)]
    owners = [None] * (rows * cols)
    boxes = {}
    for indices in regions.values():
        for r0, c0, r1, c1 in _rectangles(indices, cols):
            grid[r0][c0] = _Cell(r0, c0, r1 - r0, c1 - c0)
            key = r0 * cols + c0
            boxes[key] = (xs[c0], ys[r1], xs[c1], ys[r0])
            for r in range(r0, r1):
                for c in range(c0, c1):
                    owners[r * cols + c] = key
    return grid, owners, boxes


def _reference_axes(frag):
    scale = (frag.dir_x ** 2 + frag.dir_y ** 2) ** 0.5
    dx, dy = (frag.dir_x / scale, frag.dir_y / scale) if scale else (1.0, 0.0)
    up_scale = (frag.up_x ** 2 + frag.up_y ** 2) ** 0.5
    ux, uy = (frag.up_x / up_scale, frag.up_y / up_scale) if up_scale else (-dy, dx)
    return dx, dy, ux, uy


def _reference_point(frag, axes=None):
    """The point that picks a fragment's cell: half the width along the writing axis and 0.35
    of the size along the glyph's up axis (the second basis of the text matrix, so a mirrored
    matrix does not flip it)."""
    dx, dy, ux, uy = _reference_axes(frag) if axes is None else axes
    return (frag.x + dx * frag.width / 2 + ux * 0.35 * frag.size,
            frag.y + dy * frag.width / 2 + uy * 0.35 * frag.size)


def _fragment_index(fragments):
    """Fragments by reference-point y, ascending, so a candidate box cuts its range by bisect."""
    points = []
    previous_key = None
    axes = None
    for frag in fragments:
        key = (frag.dir_x, frag.dir_y, frag.up_x, frag.up_y)
        if key != previous_key:
            axes = _reference_axes(frag)
            previous_key = key
        points.append((_reference_point(frag, axes), frag))
    points.sort(key=lambda item: item[0][1])
    by_x = sorted(points, key=lambda item: item[0][0])
    return points, [point[1] for point, _ in points], by_x, [point[0] for point, _ in by_x]


def _fragments_inside(index, bbox):
    """The fragments whose reference point is in bbox; the narrower axis is cut by bisect."""
    points, ys, by_x, xs = index
    lo, hi = bisect_left(ys, bbox[1]), bisect_right(ys, bbox[3])
    xlo, xhi = bisect_left(xs, bbox[0]), bisect_right(xs, bbox[2])
    if hi - lo <= xhi - xlo:
        return [frag for (x, _), frag in points[lo:hi] if bbox[0] <= x <= bbox[2]]
    return [frag for (_, y), frag in by_x[xlo:xhi] if bbox[1] <= y <= bbox[3]]


def _has_fragment_inside(index, bbox):
    points, ys, by_x, xs = index
    lo, hi = bisect_left(ys, bbox[1]), bisect_right(ys, bbox[3])
    xlo, xhi = bisect_left(xs, bbox[0]), bisect_right(xs, bbox[2])
    if hi - lo <= xhi - xlo:
        return any(bbox[0] <= x <= bbox[2] for (x, _), _ in points[lo:hi])
    return any(bbox[1] <= y <= bbox[3] for (_, y), _ in by_x[xlo:xhi])


def _reading_key(frag):
    direction = pdf_layout.writing_direction(frag)
    if direction == "ltr":
        return -frag.y, frag.x
    if direction == "rtl":
        return frag.y, -frag.x
    if direction == "down":
        return -frag.x, -frag.y
    return frag.x, frag.y


def _cell_lines(fragments, cache):
    """Lines of the fragments of a cell. The cache holds identical inputs once."""
    key = tuple(id(frag) for frag in fragments)
    if cache is not None and key in cache:
        return cache[key]
    # in reading order (dochan's key), whatever order the page drew them in; lines() follows
    # `order`, so the copies are numbered by that sort
    ordered = sorted(fragments, key=_reading_key)
    out = pdf_layout.lines([replace(f, order=i) for i, f in enumerate(ordered)])
    if cache is not None:
        cache[key] = out
    return out


def _assign_text(grid, owners, boxes, xs, ys, fragments, mdl, breaks, reference_points, line_cache):
    """Each fragment goes to the cell holding its reference point. Returns the orders taken
    and, per cell key, the (y, kind, order, seq, block) events of its paragraphs."""
    by_cell = defaultdict(list)
    cols = len(xs) - 1
    ascending_y = list(reversed(ys))
    consumed = set()
    for frag in fragments:
        x, y = reference_points[id(frag)]
        c = bisect_right(xs, x) - 1
        r = len(ys) - 2 - (bisect_right(ascending_y, y) - 1)
        if not (0 <= c < cols and 0 <= r < len(grid)):
            continue
        key = owners[r * cols + c]
        left, bottom, right, top = boxes[key]
        if left + 0.5 <= x <= right - 0.5 and bottom + 0.5 <= y <= top - 0.5:
            by_cell[key].append(frag)
            consumed.add(frag.order)
    events = defaultdict(list)
    for key, frags in by_cell.items():
        groups = [[]]
        for line in _cell_lines(frags, line_cache):
            # a line whose baseline reaches the top of a nested table is above it: break after it
            if groups[-1] and not line.vertical and any(
                    line.box[1] < y <= groups[-1][-1].box[1] for y in breaks.get(key, ())):
                groups.append([])
            groups[-1].append(line)
        for group in groups:
            if not group:
                continue
            for seq, para in enumerate(pdf_layout.paragraphs(group, mdl)):
                events[key].append((group[0].box[1], 0, group[0].order, seq, para))
    return consumed, events


_NUMBER = re.compile(r"[+−-]?\d[\d,]*(?:\.\d+)?%?")


def _numeric_row_rules(horizontal, vertical, xs, ys, fragments, tolerance, reference_points, line_cache):
    """Split a ruled band only when three numeric columns agree on every row: all text must align
    with the same baselines, and every inner column edge must be ruled across the whole band."""
    if (len(xs) < 4 or len(fragments) * len(ys) > MAX_INTERSECTION_CHECKS
            or len(xs) * len(ys) > MAX_PAGE_CELLS):
        return horizontal, ys
    h_runs, v_runs = defaultdict(list), defaultdict(list)
    for y, lo, hi in horizontal:
        h_runs[y].append((lo, hi))
    for x, lo, hi in vertical:
        v_runs[x].append((lo, hi))
    bands = defaultdict(list)
    ascending_y = list(reversed(ys))
    for fragment in fragments:
        x, y = reference_points[id(fragment)]
        band = len(ys) - 1 - bisect_right(ascending_y, y)
        if (0 <= band < len(ys) - 1
                and ys[band + 1] + 0.5 < y < ys[band] - 0.5
                and xs[0] + 0.5 < x < xs[-1] - 0.5):
            bands[band].append((bisect_right(xs, x) - 1, fragment))
    additions = []
    for band, (top, bottom) in enumerate(zip(ys, ys[1:])):
        if (not _covered(h_runs[top], xs[0], xs[-1], tolerance)
                or not _covered(h_runs[bottom], xs[0], xs[-1], tolerance)
                or any(not _covered(v_runs[x], bottom, top, tolerance) for x in xs[1:-1])):
            continue
        columns = defaultdict(list)
        for column, fragment in bands[band]:
            columns[column].append(fragment)
        lns = [_cell_lines(columns[c], line_cache) for c in range(len(xs) - 1)]
        numeric = [column for column in lns if len(column) >= 2 and all(
            not line.vertical and line.text.split() and all(
                _NUMBER.fullmatch(word) for word in line.text.split()) for line in column)]
        if len(numeric) < 3 or len(numeric) * 2 < len(lns):
            continue
        reference = numeric[0]
        if len(reference) > 500:
            continue
        row_tolerance = min(tolerance, min(line.size for line in reference) * 0.2)
        if row_tolerance <= 0 or any(len(column) != len(reference) or any(
                abs(a.box[1] - b.box[1]) > row_tolerance for a, b in zip(reference, column))
                for column in numeric):
            continue
        baselines = [line.box[1] for line in reference]
        # a label may be empty, but a wrapped label or spanning text is ambiguous
        if any(line.vertical or not any(abs(line.box[1] - y) <= row_tolerance for y in baselines)
               for column in lns for line in column):
            continue
        boundaries = []
        for upper, lower in zip(reference, reference[1:]):
            if upper.box[1] - lower.box[1] < max(upper.size, lower.size) * 1.2:
                break
            boundaries.append((upper.box[1] + lower.box[1] + lower.size) / 2)
        else:
            additions.extend((y, xs[0], xs[-1]) for y in boundaries)
    if not additions:
        return horizontal, ys
    return horizontal + additions, sorted(set(ys) | {y for y, _, _ in additions}, reverse=True)


def _shared_extent(values, tolerance, outermost):
    """The outermost coordinate two or more rules reach; one rule sticking out is ignored."""
    ordered = sorted(values, reverse=outermost)
    for first, second in zip(ordered, ordered[1:]):
        if abs(first - second) <= tolerance:
            return first
    return None


def _extend_to_rule_extents(xs, horizontal, tolerance):
    """A table with no outer vertical rules gets column edges where its horizontal rules end,
    so the first and last column do not leak into the body. Overshoot of a line end and a single
    rule sticking out do not count. Rows are not extended."""
    margin = max(6.0, 4 * tolerance)
    left = _shared_extent([h[1] for h in horizontal], tolerance, outermost=False)
    right = _shared_extent([h[2] for h in horizontal], tolerance, outermost=True)
    if left is not None and xs[0] - left > margin:
        xs.insert(0, left)
    if right is not None and right - xs[-1] > margin:
        xs.append(right)


def _inside(inner, outer):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def _owner_region(bbox, parent):
    """The owner key of the merged-cell region that holds all four corners of bbox, or None."""
    left, bottom, right, top = bbox
    xs, ys = parent.xs, parent.ys
    ascending_y = list(reversed(ys))
    columns = (bisect_right(xs, left) - 1, bisect_left(xs, right) - 1)
    rows = (len(ys) - 2 - (bisect_right(ascending_y, bottom) - 1),
            len(ys) - 2 - (bisect_left(ascending_y, top) - 1))
    if any(not 0 <= col < len(xs) - 1 for col in columns):
        return None
    if any(not 0 <= row < len(ys) - 1 for row in rows):
        return None
    keys = {parent.owners[row * (len(xs) - 1) + col] for row in rows for col in columns}
    if len(keys) != 1:
        return None
    key = keys.pop()
    return key if _inside(bbox, parent.boxes[key]) else None


def _blocks_have_text(blocks, depth=0):
    for block in blocks:
        if isinstance(block, Para):
            return True
        if isinstance(block, Table) and depth <= MAX_NESTED_DEPTH:
            if any(_blocks_have_text(cell.blocks, depth + 1) for row in block.rows for cell in row):
                return True
    return False


def _text_cell_count(node):
    return sum(_blocks_have_text(cell.blocks) for row in node.grid for cell in row
               if not cell.merged_away)


def _area(bbox):
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _empty_form(xs, ys, cells):
    """An empty form grid still counts as a table."""
    return len(xs) >= 3 and len(ys) >= 3 and cells >= 6


def _short_dash_runs(segments, tolerance):
    """Only strokes of equal-length pieces with an equal positive gap, three or more in a row,
    supplement a rule."""
    axes = defaultdict(set)
    for segment in segments or ():
        if segment.x0 == segment.x1:
            if 0 < segment.length < 1:
                axes[("v", segment.x0)].add((segment.y0, segment.y1))
        elif segment.y0 == segment.y1:
            if 0 < segment.length < 1:
                axes[("h", segment.y0)].add((segment.x0, segment.x1))
    result = {"h": defaultdict(list), "v": defaultdict(list)}
    for (axis, coord), parts in axes.items():
        run = []
        gap = None
        for lo, hi in sorted(parts):
            next_gap = lo - run[-1][1] if run else None
            equal = (run and 0 < next_gap <= tolerance
                     and isclose(hi - lo, run[0][1] - run[0][0], abs_tol=1e-7)
                     and (gap is None or isclose(next_gap, gap, abs_tol=1e-7)))
            if not equal:
                if len(run) >= 3:
                    result[axis][coord].append((run[0][0], run[-1][1]))
                run, gap = [], None
            if run:
                gap = next_gap
            run.append((lo, hi))
        if len(run) >= 3:
            result[axis][coord].append((run[0][0], run[-1][1]))
    return result


def _supplement_rules(horizontal, vertical, xs, ys, dash_runs):
    """Dashes only extend the axes and spans the grid already has; they make no new axis,
    component or border."""
    hs, vs = list(horizontal), list(vertical)
    for y in ys[1:-1]:
        for lo, hi in dash_runs["h"].get(y, ()):
            if xs[0] <= lo < hi <= xs[-1]:
                hs.append((y, lo, hi))
    for x in xs[1:-1]:
        for lo, hi in dash_runs["v"].get(x, ()):
            if ys[-1] <= lo < hi <= ys[0]:
                vs.append((x, lo, hi))

    def union_overlaps(rules):
        result = []
        for coord, lo, hi in sorted(rules):
            if result and result[-1][0] == coord and lo <= result[-1][2]:
                result[-1] = (coord, result[-1][1], max(hi, result[-1][2]))
            else:
                result.append((coord, lo, hi))
        return result

    return union_overlaps(hs), union_overlaps(vs)


# ---- the closed inner grid a shared rule joins to a bigger one (dochan connected_tables.py)

class _SplitBudget:
    """The graph and grid checks of one page, shared by all its connected components."""

    def __init__(self):
        self.remaining = MAX_SPLIT_CHECKS
        self.counted = False

    def spend(self, checks):
        if checks > self.remaining:
            self.remaining = 0
            return False
        self.remaining -= checks
        return True


def _grid_cost(horizontal, vertical, xs, ys):
    rows, cols = len(ys) - 1, len(xs) - 1
    hs = Counter(line[0] for line in horizontal)
    vs = Counter(line[0] for line in vertical)
    return (rows * cols + len(horizontal) + len(vertical)
            + rows * sum(vs[x] for x in xs[1:-1])
            + cols * sum(hs[y] for y in ys[1:-1]))


def _articulations(adjacency):
    """Cut vertices by an iterative DFS."""
    discovered = [-1] * len(adjacency)
    low = [0] * len(adjacency)
    parents = [-1] * len(adjacency)
    children = [0] * len(adjacency)
    cuts = set()
    clock = 0
    for root in range(len(adjacency)):
        if discovered[root] >= 0:
            continue
        discovered[root] = low[root] = clock
        clock += 1
        stack = [(root, iter(adjacency[root]))]
        while stack:
            node, neighbours = stack[-1]
            other = next(neighbours, None)
            if other is None:
                stack.pop()
                parent = parents[node]
                if parent >= 0:
                    low[parent] = min(low[parent], low[node])
                    if parents[parent] >= 0 and low[node] >= discovered[parent]:
                        cuts.add(parent)
                elif children[node] > 1:
                    cuts.add(node)
                continue
            if discovered[other] < 0:
                parents[other] = node
                children[node] += 1
                discovered[other] = low[other] = clock
                clock += 1
                stack.append((other, iter(adjacency[other])))
            elif other != parents[node]:
                low[node] = min(low[node], discovered[other])
    return sorted(cuts)


def _has_cycle(adjacency):
    """Peel vertices of degree 0 and 1: a forest has no closed grid."""
    degrees = [len(neighbours) for neighbours in adjacency]
    pending = [i for i, degree in enumerate(degrees) if degree < 2]
    removed = 0
    while pending:
        node = pending.pop()
        removed += 1
        for other in adjacency[node]:
            degrees[other] -= 1
            if degrees[other] == 1:
                pending.append(other)
    return removed < len(adjacency)


def _bbox(hs, vs):
    points = [(lo, y, hi, y) for y, lo, hi in hs]
    points += [(x, lo, x, hi) for x, lo, hi in vs]
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[2] for p in points), max(p[3] for p in points))


def _closed(hs, vs, box, tolerance):
    left, bottom, right, top = box
    return (all(any(abs(y - edge) <= tolerance and lo <= left + tolerance
                    and hi >= right - tolerance for y, lo, hi in hs)
                for edge in (bottom, top))
            and all(any(abs(x - edge) <= tolerance and lo <= bottom + tolerance
                        and hi >= top - tolerance for x, lo, hi in vs)
                    for edge in (left, right)))


def _split_connected(horizontal, vertical, tolerance, fragment_index, cut, split_budget,
                     dash_runs, max_cells):
    """One closed inner grid hung on a single shared rule is split from its parent. An
    ambiguous candidate or an exhausted budget returns the input unchanged; the rules given are
    never changed."""
    unchanged = [(horizontal, vertical)]
    cell_limit = min(MAX_PAGE_CELLS, max_cells)

    def axes(hs, vs):
        xs = sorted(set(line[0] for line in vs))
        _extend_to_rule_extents(xs, hs, tolerance)
        return xs, sorted(set(line[0] for line in hs), reverse=True)

    def exhausted():
        if not split_budget.counted:
            split_budget.counted = True
            _count(cut)
        return unchanged

    if not fragment_index[0]:
        return unchanged
    size = len(horizontal) + len(vertical)
    if size > MAX_COMPONENT_LINES or len(horizontal) < 2 or len(vertical) < 2:
        return unchanged
    if not split_budget.spend(size):
        return exhausted()
    adjacency = [set() for _ in range(size)]
    xs = [line[0] for line in vertical]
    for i, (y, left, right) in enumerate(horizontal):
        lo, hi = bisect_left(xs, left - tolerance), bisect_right(xs, right + tolerance)
        if not split_budget.spend(hi - lo):
            return exhausted()
        for j in range(lo, hi):
            if vertical[j][1] - tolerance <= y <= vertical[j][2] + tolerance:
                adjacency[i].add(len(horizontal) + j)
                adjacency[len(horizontal) + j].add(i)
    graph_cost = size + sum(len(neighbours) for neighbours in adjacency)
    if not split_budget.spend(graph_cost):
        return exhausted()
    if graph_cost - size < 2 * size:
        if not _has_cycle(adjacency):
            return unchanged
        if not split_budget.spend(graph_cost):
            return exhausted()
    accepted = None
    for cutpoint in _articulations(adjacency):
        neighbours = adjacency[cutpoint]
        if len(neighbours) < 3:
            continue
        seen = {cutpoint}
        branches = []
        for start in sorted(neighbours):
            if start in seen:
                continue
            branch, pending = set(), [start]
            seen.add(start)
            while pending:
                item = pending.pop()
                branch.add(item)
                if not split_budget.spend(1 + len(adjacency[item])):
                    return exhausted()
                for other in adjacency[item]:
                    if other not in seen:
                        seen.add(other)
                        pending.append(other)
            branches.append(branch)
        if len(branches) < 2:
            continue
        for branch in branches:
            if len(branch) < 3:
                continue
            if not split_budget.spend(size):
                return exhausted()
            hs = [line for i, line in enumerate(horizontal) if i in branch]
            vs = [line for i, line in enumerate(vertical) if len(horizontal) + i in branch]
            if not hs or not vs:
                continue
            box = _bbox(hs, vs)
            left, bottom, right, top = box
            if right - left < 8 or top - bottom < 8:
                continue
            if cutpoint < len(horizontal):
                hs = sorted(hs + [(horizontal[cutpoint][0], left, right)])
            else:
                vs = sorted(vs + [(vertical[cutpoint - len(horizontal)][0], bottom, top)])
            if not _closed(hs, vs, box, tolerance):
                continue
            fragments = [f for f in _fragments_inside(fragment_index, box) if f.text.strip()]
            if not fragments:
                continue
            parent_h = [line for i, line in enumerate(horizontal) if i not in branch]
            parent_v = [line for i, line in enumerate(vertical) if len(horizontal) + i not in branch]
            if len(parent_h) < 2 or len(parent_v) < 2:
                continue
            parent_box = _bbox(parent_h, parent_v)
            if box == parent_box or not _inside(box, parent_box):
                continue
            cx, cy = axes(hs, vs)
            child_cells = (len(cx) - 1) * (len(cy) - 1)
            grid_hs, grid_vs = _supplement_rules(hs, vs, cx, cy, dash_runs)
            if child_cells > cell_limit:
                return unchanged   # the cell caps count what they drop, in `tables`
            if not split_budget.spend(_grid_cost(grid_hs, grid_vs, cx, cy) + len(fragments)):
                return exhausted()
            _, owners, child_boxes = _make_grid(grid_hs, grid_vs, cx, cy, tolerance)
            filled = set()
            ascending_y = list(reversed(cy))
            for fragment in fragments:
                x, y = _reference_point(fragment)
                c = bisect_right(cx, x) - 1
                r = len(cy) - 2 - (bisect_right(ascending_y, y) - 1)
                if not (0 <= c < len(cx) - 1 and 0 <= r < len(cy) - 1):
                    continue
                key = owners[r * (len(cx) - 1) + c]
                left_edge, bottom_edge, right_edge, top_edge = child_boxes[key]
                if (left_edge + 0.5 <= x <= right_edge - 0.5
                        and bottom_edge + 0.5 <= y <= top_edge - 0.5):
                    filled.add(key)
            if not filled or not (len(filled) >= 2 or child_cells >= 4 or
                                  (child_cells == 1 and right - left >= 30 and top - bottom >= 12)):
                continue
            px, py = axes(parent_h, parent_v)
            cells = (len(px) - 1) * (len(py) - 1)
            grid_ph, grid_pv = _supplement_rules(parent_h, parent_v, px, py, dash_runs)
            if cells + child_cells > cell_limit:
                return unchanged
            if not split_budget.spend(_grid_cost(grid_ph, grid_pv, px, py)):
                return exhausted()
            _, _, boxes = _make_grid(grid_ph, grid_pv, px, py, tolerance)
            containers = [outer for outer in boxes.values() if _inside(box, outer)]
            if len(containers) != 1:
                continue
            gaps = [abs(a - b) for a, b in zip(box, containers[0])]
            if sum(gap <= tolerance for gap in gaps) != 1:
                continue
            if accepted is not None:
                return unchanged
            accepted = [(parent_h, parent_v), (hs, vs)]
    return accepted if accepted is not None else unchanged


def _to_table(grid) -> Table:
    """The origin cells of a grid, row by row."""
    dropped = {r for r, row in enumerate(grid) if all(c.merged_away for c in row)}
    rows = []
    for r, row in enumerate(grid):
        if r in dropped:
            continue
        # a row with no origin cell is left out, so a span over it shrinks by one
        cells = [Cell(list(c.blocks),
                      c.row_span - sum(1 for d in dropped if c.row < d < c.row + c.row_span),
                      c.col_span) for c in row if not c.merged_away]
        rows.append(cells)
    return Table(rows)


def tables(segments, fragments, page_number, budget, cut, short=(), model=None,
           tolerance=_TOLERANCE) -> List[Found]:
    """The tables of one page, from its rule segments (dochan tables.py `_build_tables_python`).

    Rules that touch make a component; its rule coordinates are the grid axes, and a missing
    inner rule merges the cells it would have parted. A component inside the cell of a bigger one
    is a nested table in that cell's blocks. Text goes by fragment to the cell holding its
    reference point, through pdf_layout.lines and paragraphs, with `model` (default `load_model()`)
    for Korean joins. `short` (PathCollector.short_segments) supplements a rule drawn in dashes.
    A grid under 2 rows or 2 columns is no table. Fragment.order must be unique on the page.

    Every cap that drops content adds 1 to cut["cells"] per event: the page and document cell
    caps and the component, nesting, rule-check and split-check caps, and limits.TABLE_NODES
    through budget.nodes, which every adopted table node (a nested one too) spends 1 of.
    Rule segments of form XObjects are not part of `segments`.
    """
    mdl = model if model is not None else pdf_layout.load_model()
    tol = tolerance
    horizontal, vertical = [], []
    for s in segments:
        if abs(s.y1 - s.y0) <= tol:
            horizontal.append(((s.y0 + s.y1) / 2, s.x0, s.x1))
        elif abs(s.x1 - s.x0) <= tol:
            vertical.append(((s.x0 + s.x1) / 2, s.y0, s.y1))
    horizontal = _snap_runs(horizontal, tol)
    vertical = _snap_runs(vertical, tol)
    grouped = _components(horizontal, vertical, tol)
    if grouped is None:
        _count(cut)
        return []
    if not grouped or not any(len(hs) >= 2 and len(vs) >= 2 for hs, vs in grouped):
        return []
    fragments = [f for f in fragments if f.text and not f.hidden]
    dash_runs = _short_dash_runs(short, tol)
    index = _fragment_index(fragments)
    line_cache = {}
    reference_points = {id(frag): point for point, frag in index[0]}
    separated = []
    component_boxes = {}
    split_budget = _SplitBudget()
    for i, (hs, vs) in enumerate(grouped):
        if len(hs) >= 2 and len(vs) >= 2:
            xs = sorted(set(v[0] for v in vs))
            _extend_to_rule_extents(xs, hs, tol)
            component_boxes[i] = (xs[0], min(h[0] for h in hs), xs[-1], max(h[0] for h in hs))
    for i, (hs, vs) in enumerate(grouped):
        eligible = (len(grouped) < MAX_PAGE_COMPONENTS and MAX_NESTED_DEPTH >= 1
                    and i in component_boxes and not any(
                        _inside(component_boxes[i], box)
                        for j, box in component_boxes.items() if i != j))
        split = (_split_connected(hs, vs, tol, index, cut, split_budget, dash_runs,
                                  budget.remaining if budget is not None else MAX_PAGE_CELLS)
                 if eligible else [(hs, vs)])
        separated.extend(split)
    if len(separated) > len(grouped):
        # all components of the page together, so a parent is not kept without its child
        needed = 0
        for hs, vs in separated:
            if len(hs) < 2 or len(vs) < 2:
                continue
            xs = sorted(set(v[0] for v in vs))
            _extend_to_rule_extents(xs, hs, tol)
            needed += (len(xs) - 1) * (len(set(h[0] for h in hs)) - 1)
        if (len(separated) > MAX_PAGE_COMPONENTS or needed > MAX_PAGE_CELLS
                or (budget is not None and needed > budget.remaining)):
            separated = grouped
    components = []
    for hs, vs in separated:
        if len(hs) < 2 or len(vs) < 2:
            continue
        xs, ys = sorted(set(v[0] for v in vs)), sorted(set(h[0] for h in hs), reverse=True)
        _extend_to_rule_extents(xs, hs, tol)
        bbox = (xs[0], ys[-1], xs[-1], ys[0])
        if bbox[2] - bbox[0] >= 8 and bbox[3] - bbox[1] >= 8:
            if len(xs) >= 4:   # the numeric rule needs three columns
                hs, ys = _numeric_row_rules(hs, vs, xs, ys, _fragments_inside(index, bbox), tol,
                                            reference_points, line_cache)
            components.append((bbox, hs, vs, xs, ys))
    components.sort(key=lambda v: -_area(v[0]))
    remaining = MAX_PAGE_CELLS
    reserved = 0   # a provisional reservation; the real charge is made when a table is adopted
    reserved_nodes = 0
    nodes = []
    examined = 0
    for bbox, hs, vs, xs, ys in components:
        examined += 1
        if examined > MAX_PAGE_COMPONENTS:   # the biggest are looked at first; the rest are given up
            _count(cut)
            break
        if len(hs) + len(vs) > MAX_COMPONENT_LINES:
            _count(cut)
            continue
        has_text = _has_fragment_inside(index, bbox)
        if not has_text and (len(xs) < 3 or len(ys) < 3):
            continue
        if len(nodes) >= MAX_PAGE_COMPONENTS:
            _count(cut)
            break
        parent = min((node for node in nodes if _inside(bbox, node.bbox)),
                     key=lambda node: _area(node.bbox), default=None)
        owner_key = _owner_region(bbox, parent) if parent is not None else None
        if parent is not None and owner_key is None:
            continue
        cells = (len(xs) - 1) * (len(ys) - 1)
        if parent is not None and not has_text and not _empty_form(xs, ys, cells):
            continue
        depth = parent.depth + 1 if parent is not None else 0
        if depth > MAX_NESTED_DEPTH:
            _count(cut)
            continue
        if cells > remaining - reserved:
            _count(cut)
            continue
        if budget is not None and (cells > budget.remaining - reserved
                                   or budget.nodes - reserved_nodes < 1):
            _count(cut)
            continue
        grid_hs, grid_vs = (_supplement_rules(hs, vs, xs, ys, dash_runs) if has_text else (hs, vs))
        grid, owners, cell_boxes = _make_grid(grid_hs, grid_vs, xs, ys, tol)
        reserved += cells
        reserved_nodes += 1
        nodes.append(_Node(bbox, xs, ys, grid, owners, cell_boxes, cells, parent, owner_key, depth))

    used_orders = set()
    result = []
    # the smallest first, so a child is done before its parent (the deeper one on a tie)
    for node in sorted(nodes, key=lambda item: (_area(item.bbox), -item.depth)):
        breaks = defaultdict(list)
        for child in node.children:
            breaks[child.owner_key].append(child.bbox[3])
        own_orders, events = _assign_text(
            node.grid, node.owners, node.boxes, node.xs, node.ys,
            [frag for frag in _fragments_inside(index, node.bbox) if frag.order not in used_orders],
            mdl, breaks, reference_points, line_cache)
        for child in node.children:
            events[child.owner_key].append((child.bbox[3], 1, child.anchor, 0, _to_table(child.grid)))
        for key, blocks in events.items():
            row, col = divmod(key, len(node.xs) - 1)
            node.grid[row][col].blocks = [block for *_, block in sorted(
                blocks, key=lambda item: (-item[0], item[1], item[2], item[3]))]
        subtree_orders = own_orders.union(*(child.orders for child in node.children))
        if node.parent is None:
            keep = bool(subtree_orders) or (len(node.xs) >= 3 and len(node.ys) >= 3)
        else:
            text_cells = _text_cell_count(node)
            width = node.bbox[2] - node.bbox[0]
            height = node.bbox[3] - node.bbox[1]
            keep = (text_cells >= 1 and (text_cells >= 2 or node.cells >= 4 or
                                         (node.cells == 1 and width >= 30 and height >= 12))
                    or (text_cells == 0 and _empty_form(node.xs, node.ys, node.cells)))
        # a grid under 2 rows or 2 columns is no table
        if len(node.xs) < 3 or len(node.ys) < 3:
            keep = False
        if not keep:
            # a refused node gives its text back to the parent, and its adopted children go up to
            # the grandparent cell (or the top level)
            for child in node.children:
                child.parent, child.owner_key = node.parent, node.owner_key
                if node.parent is not None:
                    node.parent.children.append(child)
                else:
                    result.append(Found(_to_table(child.grid), child.bbox, set(child.orders)))
            continue
        if budget is not None:
            budget.remaining -= node.cells
            budget.nodes -= 1
        node.orders = subtree_orders
        node.anchor = min(subtree_orders, default=-1)
        used_orders.update(own_orders)
        if node.parent is None:
            result.append(Found(_to_table(node.grid), node.bbox, set(subtree_orders)))
        else:
            node.parent.children.append(node)
    return sorted(result, key=lambda found: min(found.used, default=-1))
