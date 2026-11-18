"""The drawing model every vector parser (WMF, EMF, PICT, WPG) fills and every writer reads, and the figure text.

It holds no format knowledge. Colors are 0xRRGGBB; None means no pen or no fill. Coordinates are in the picture's
own frame units, y growing downward.
"""
import math
import unicodedata
from dataclasses import dataclass, field

from . import limits
from .limits import ReadError


@dataclass
class Pen:
    color: int
    width: float


@dataclass
class Brush:
    color: int


@dataclass
class Path:
    segs: list  # ("M", x, y), ("L", x, y), ("C", x1, y1, x2, y2, x, y) or ("Z",)
    pen: Pen | None
    brush: Brush | None
    even_odd: bool = False


@dataclass
class Run:
    text: str
    x: float
    y: float  # the baseline
    face: str
    height: float  # the em height
    weight: int = 400
    italic: bool = False
    angle: float = 0.0
    color: int = 0
    glyphs: tuple | None = None  # glyph numbers when text is ""
    width: float | None = None  # advance width in frame units, when the parser knows it


@dataclass
class Bitmap:
    data: bytes
    kind: str  # "png" or "jpg"
    box: tuple  # (x0, y0, x1, y1)


@dataclass
class Clip:
    segs: list | None


@dataclass
class Drawing:
    frame: tuple  # (x0, y0, x1, y1)
    dpi: float    # frame units per inch
    items: list = field(default_factory=list)
    damaged: bool = False
    skipped: int = 0

    def size_px(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.frame
        k = 96.0 / self.dpi if self.dpi > 0 else 1.0
        return max(1, round(abs(x1 - x0) * k)), max(1, round(abs(y1 - y0) * k))


def arc_beziers(cx, cy, rx, ry, start, sweep) -> list[tuple]:
    """Cubic "C" segments (at most 90 degrees each) for an elliptical arc; angles in degrees."""
    if not all(map(math.isfinite, (cx, cy, rx, ry, start, sweep))):
        return []
    sweep = max(-360.0, min(360.0, sweep))
    n = max(1, math.ceil(abs(sweep) / 90.0 - 1e-9))
    step = math.radians(sweep) / n
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    a = math.radians(start)
    out = []
    for _ in range(n):
        b = a + step
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        out.append(("C", cx + rx * (ca - k * sa), cy + ry * (sa + k * ca),
                    cx + rx * (cb + k * sb), cy + ry * (sb - k * cb),
                    cx + rx * cb, cy + ry * sb))
        a = b
    return out


class Budget:
    """Counts records and points against the caps, read at call time unless given."""

    def __init__(self, records=None, points=None):
        self._records = records
        self._points = points
        self.n = 0
        self.p = 0

    def tick(self):
        self.n += 1
        cap = limits.METAFILE_RECORDS if self._records is None else self._records
        if self.n > cap:
            raise ReadError("too_large", "figure")

    def points(self, n):
        self.p += n
        cap = limits.METAFILE_POINTS if self._points is None else self._points
        if self.p > cap:
            raise ReadError("too_large", "figure")


def run_width(r: Run, text: str) -> float:
    if r.width is not None:
        return r.width
    # no font metrics here: one em for a wide East Asian character, half an em for the rest
    return r.height * sum(1.0 if unicodedata.east_asian_width(c) in "WF" else 0.5 for c in text)


def _seen(grid: dict, r: Run, t: str) -> bool:
    """True when an earlier run with the same text lies within a quarter of r's height; r is added otherwise.

    Kept runs sit in a grid per text, cell size a power of two near a quarter of their own height, so a lookup
    visits a few cells instead of every earlier run.
    """
    tol = 0.25 * r.height
    levels = grid.setdefault(t, {})
    for e, cells in levels.items():
        c = 2.0 ** e
        if tol / c > 2:  # a coarse query over a fine grid: scan the level
            if any(abs(r.x - q.x) <= tol and abs(r.y - q.y) <= tol for lst in cells.values() for q in lst):
                return True
            continue
        for i in range(math.floor((r.x - tol) / c), math.floor((r.x + tol) / c) + 1):
            for j in range(math.floor((r.y - tol) / c), math.floor((r.y + tol) / c) + 1):
                for q in cells.get((i, j), ()):
                    if abs(r.x - q.x) <= tol and abs(r.y - q.y) <= tol:
                        return True
    h = 0.25 * r.height
    e = math.ceil(math.log2(h)) if h > 0 and math.isfinite(h) else 0
    c = 2.0 ** e
    if math.isfinite(r.x) and math.isfinite(r.y):
        levels.setdefault(e, {}).setdefault((math.floor(r.x / c), math.floor(r.y / c)), []).append(r)
    return False


def text_lines(d: Drawing, resolve=None) -> tuple[list[str], bool]:
    """Reading-order lines of a drawing's text, and whether FIGURE_CHARS cut them."""
    kept: list[tuple[Run, str]] = []
    grid: dict = {}
    for r in d.items:
        if not isinstance(r, Run):
            continue
        t = r.text
        if not t and r.glyphs is not None and resolve is not None:
            t = resolve(r)
        if not t or not t.strip():
            continue
        if _seen(grid, r, t):
            continue
        kept.append((r, t))
    rows: list[list] = []  # [anchor y, runs, largest height]
    flat = sorted((p for p in kept if p[0].angle % 360 == 0), key=lambda p: p[0].y)
    cur = None
    for r, t in flat:
        if cur is not None and abs(r.y - cur[0]) <= 0.5 * max(r.height, cur[2]):
            cur[1].append((r, t))
            cur[2] = max(cur[2], r.height)
        else:
            cur = [r.y, [(r, t)], r.height]
            rows.append(cur)
    lines: list[tuple[float, str]] = []
    for y, runs, _h in rows:
        runs.sort(key=lambda p: p[0].x)
        s = ""
        prev = None
        for r, t in runs:
            if prev is not None:
                gap = r.x - (prev[0].x + run_width(*prev))
                if gap > max(r.height, prev[0].height):
                    s += " "
            s += t
            prev = (r, t)
        lines.append((y, " ".join(s.split())))
    for r, t in kept:
        if r.angle % 360 != 0:
            lines.append((r.y, " ".join(t.split())))
    lines.sort(key=lambda p: p[0])  # stable: a rotated run follows a horizontal line at the same y
    out = [s for _, s in lines if s]
    cap = limits.FIGURE_CHARS
    if sum(map(len, out)) <= cap:
        return out, False
    res, used = [], 0
    for s in out:
        room = cap - 1 - used  # one character stays for the ellipsis
        if len(s) <= room:
            res.append(s)
            used += len(s)
            continue
        res.append(s[:max(room, 0)] + "…")
        break
    return res, True
