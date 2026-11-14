"""pdf table geometry: the rule segments of a page, gathered from its paths.

Segment and PathCollector follow dochan 1.21.0 pdf/paths.py (MIT). A collector keeps each
subpath's bounding box and edges, and at a paint operator gives out the horizontal and
vertical rules: every edge of a stroke, and the long edges of a thin filled subpath (a
rectangle drawn as a line). It also keeps the union box and count of the painted paths that
gave no rule, the vector figure of the page.

A page holds at most PDF_SEGMENTS segments; past it the rest are ignored and not counted.
"""
from dataclasses import dataclass
from math import hypot, isfinite
from typing import List, Optional

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
