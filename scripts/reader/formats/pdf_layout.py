"""pdf page types and layout: the text Fragment, the page data the pdf reader passes around,
and the lines, paragraphs, headings and running headers both pdf engines share.

Fragment follows dochan 1.21.0 pdf/content.py (MIT) without its bold, italic, note, comment,
marked-content and artifact fields, and adds the font name, the text object index and the
hidden kind. `writing_direction`, `along` and `across` are dochan's.

The layout half is after dochan 1.21.0 (MIT): `SpacingModel` and `load_model` are pdf/spacing.py
(the table is `pdf_spacing.json`, dochan's korean_spacing.json byte for byte); the line join
(`join_break`) and the paragraph merge are pdf/layout.py `merge_lines`, with a 1.5 x gap rule
and no full-line rule; `running` is pdf/running.py `detect_running` over line indexes; the
heading size ratios are pdf/reader.py:85-100. Glyph gaps, link runs, vertical columns and the
column fallback (`xps._columns`) are this reader's own.

Page space is y up. Every loop here runs over the fragments and lines the caller holds, which
are capped upstream. Sorts are O(n log n); line building, joins and `running` are linear.
"""
import bisect
import heapq
import json
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..model import Link, Para, Text
from . import xps


@dataclass
class Fragment:
    """The text of one Tj/TJ/'/\" operator and its start position."""
    x: float
    y: float
    width: float   # device width
    size: float    # effective font size
    text: str
    space_width: float  # device width of one space in this fragment's font
    order: int = 0
    dir_x: float = 1.0  # x part of the text x axis in device space; negative runs right to left
    dir_y: float = 0.0  # y part of the text x axis in device space
    up_x: float = 0.0   # the text y axis (the glyph's up) in device space
    up_y: float = 1.0
    # Where glyph k starts, along the text direction, from the fragment start; the last entry
    # is the end of the last glyph. A TJ number moves the glyphs after it, so it shows as a
    # larger step. Empty when positions were not tracked.
    char_offsets: tuple = ()
    link_spans: list = field(default_factory=list)
    link_geometry_reliable: bool = True
    # Drawn with render mode 3 or 7 (invisible text, an OCR layer).
    invisible: bool = False
    font: str = ""   # the BaseFont of the font, without its subset tag
    obj: int = 0     # index of its text object on the page
    hidden: str = ""  # "", "ocg", "invisible", "offcrop" or "tiny"
    # The displacement before glyph k beyond its advance (a TJ number, Tc and Tw of the glyph
    # before), in device units along the text direction; a kern is negative. Same length as the
    # text and under the same cap as char_offsets; empty when positions were not tracked.
    char_gaps: tuple = ()
    # Drawn with a vertical font (WMode 1): its glyphs stand upright and advance down the page.
    vertical: bool = False


def writing_direction(frag: Fragment) -> str:
    """The text x axis in device space as one of four writing directions."""
    if abs(frag.dir_x) >= abs(frag.dir_y):
        return "ltr" if frag.dir_x >= 0 else "rtl"
    return "down" if frag.dir_y < 0 else "up"


def along(frag: Fragment) -> float:
    """The fragment start projected on the axis whose positive side is the reading direction."""
    direction = writing_direction(frag)
    if direction == "ltr":
        return frag.x
    if direction == "rtl":
        return -frag.x
    if direction == "down":
        return -frag.y
    return frag.y


def across(frag: Fragment) -> float:
    """The between-lines coordinate; it falls on the next line (text turned 180 degrees has
    the next line above)."""
    direction = writing_direction(frag)
    if direction == "ltr":
        return frag.y
    if direction == "rtl":
        return -frag.y
    return frag.x if direction == "down" else -frag.x


@dataclass
class PageImage:
    """One image on a page: its box, its viewable bytes (or None) and its pixel size.
    `kind` is "jpg", "png", "jpx", "ccitt", "jbig2" or "raw"."""
    box: Tuple[float, float, float, float]
    data: Optional[bytes]
    px: Tuple[int, int]
    kind: str


@dataclass
class Comment:
    author: str
    text: str
    hidden: bool


@dataclass
class PageData:
    """Everything the reader takes from one page.

    `box` is the crop box in page space; `short` the sub-1-unit rule pieces kept for the
    short-dash supplement; `figure` is (union box or None, non-rule path count); `links` is
    [(box, url)], `fields` [(label, value)]; `cut` counts what a cap dropped; `render` is
    (box, dpi) -> png bytes or None.
    """
    number: int
    box: Tuple[float, float, float, float]
    rotate: int
    fragments: List[Fragment] = field(default_factory=list)
    segments: list = field(default_factory=list)
    short: list = field(default_factory=list)
    images: List[PageImage] = field(default_factory=list)
    figure: tuple = (None, 0)
    links: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    chars: int = 0
    cut: Counter = field(default_factory=Counter)
    render: Optional[Callable] = None


# ---- rotation

def rotate_into_view(fragments, rotate, box):
    """The fragments with position and direction mapped through the page's /Rotate (0, 90, 180
    or 270, clockwise) into the viewer's space, whose origin is the rotated crop box corner.
    Other angles leave the fragments alone."""
    turn = 0
    if isinstance(rotate, (int, float)) and not isinstance(rotate, bool) and math.isfinite(rotate):
        turn = int(round(rotate)) % 360
    if turn not in (90, 180, 270):
        return list(fragments)
    x0, y0, x1, y1 = box
    out = []
    for f in fragments:
        px, py = f.x - x0, f.y - y0
        if turn == 90:
            x, y = py, (x1 - x0) - px
            dx, dy, ux, uy = f.dir_y, -f.dir_x, f.up_y, -f.up_x
        elif turn == 180:
            x, y = (x1 - x0) - px, (y1 - y0) - py
            dx, dy, ux, uy = -f.dir_x, -f.dir_y, -f.up_x, -f.up_y
        else:
            x, y = (y1 - y0) - py, px
            dx, dy, ux, uy = -f.dir_y, f.dir_x, -f.up_y, f.up_x
        out.append(replace(f, x=x, y=y, dir_x=dx, dir_y=dy, up_x=ux, up_y=uy))
    return out


_UPRIGHT = {"ltr": 0, "up": 90, "rtl": 180, "down": 270}  # the turn that makes it read ltr


def upright_turn(fragments, turn) -> int:
    """The quarter turn (0, 90, 180 or 270) to lay a page out in: the page's /Rotate `turn`,
    unless it leaves the page's text sideways, running up or down in the viewer. Then the turn
    in which that text reads left to right: Latin turned with the page is read along its own
    lines, not as vertical writing. The page's text is the writing direction of most of its
    visible chars outside vertical fonts."""
    counts = Counter()
    for f in fragments:
        if f.text and not f.hidden and not f.vertical:
            counts[writing_direction(f)] += sum(1 for ch in f.text if not ch.isspace())
    if not counts:
        return turn
    need = _UPRIGHT[max(counts, key=counts.get)]
    return need if (need - turn) % 180 == 90 else turn


# ---- the Korean spacing table

def _odds(probability: float) -> float:
    probability = max(0.01, min(0.99, probability))
    return probability / (1 - probability)


@dataclass
class SpacingModel:
    """Pairs seen often decide; the rest is decided by the odds of each side."""
    prior: float = 0.29
    last: Dict[str, float] = field(default_factory=dict)
    first: Dict[str, float] = field(default_factory=dict)
    pairs: Dict[str, float] = field(default_factory=dict)

    def space_probability(self, last: str, first: str) -> float:
        pair = last + first
        if pair in self.pairs:
            return self.pairs[pair]
        if last not in self.last and first not in self.first:
            return self.prior
        odds = (_odds(self.last.get(last, self.prior))
                * _odds(self.first.get(first, self.prior)) / _odds(self.prior))
        return odds / (1 + odds)

    def joins_with_space(self, last: str, first: str) -> bool:
        # an exact tie (0.5) can come out as 0.5000...01: a tie joins without a space
        return self.space_probability(last, first) > 0.5 + 1e-9


_DEFAULT_MODEL: Optional[SpacingModel] = None
_MODEL_PATH = Path(__file__).with_name("pdf_spacing.json")


def _is_probability(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1


def _read_model(path) -> SpacingModel:
    """A missing or damaged table is an empty model (no pair is known, the prior decides)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            return SpacingModel()
        if not _is_probability(data.get("prior")):
            return SpacingModel()
        for name, length in (("last", 1), ("first", 1), ("pairs", 2)):
            table = data.get(name)
            if not isinstance(table, dict) or any(
                    len(key) != length or not all("가" <= c <= "힣" for c in key)
                    or not _is_probability(value) for key, value in table.items()):
                return SpacingModel()
        return SpacingModel(prior=data["prior"], last=data["last"],
                            first=data["first"], pairs=data["pairs"])
    except (OSError, ValueError, UnicodeError):
        return SpacingModel()


def load_model(path=None) -> SpacingModel:
    """The shipped table is read once per process; an explicit path is read each time."""
    global _DEFAULT_MODEL
    if path is not None:
        return _read_model(path)
    if _DEFAULT_MODEL is None:
        _DEFAULT_MODEL = _read_model(_MODEL_PATH)
    return _DEFAULT_MODEL


# ---- lines

GAP = 0.2        # a gap over this many sizes between two glyphs is a space
UPWARD = 0.3     # reading order is rebuilt when this share of line steps go up
LINE_TOLERANCE = 0.5   # fragments within this many sizes of one baseline are one line
PARA_GAP = 1.5   # a baseline step over this many line heights ends a paragraph
GRID = 64.0      # link grid cell, in page units
GRID_SPAN = 32   # a link or fragment over this many cells a side skips the grid
# A fragment drawn out of reading order on a line (see `lines`) joins from wholly before the line
# only within GUTTER sizes of it and at a size within SIZE_TOLERANCE points. A word space is about
# 0.25 em and the tab after a heading number 1-2 em; a column gutter or a box beside another is
# wider (the bench's heading beside a box stands 4.8 em off), so 2 em keeps the first and splits
# the second.
GUTTER = 2.0
SIZE_TOLERANCE = 1.0
PIECE_OVERLAP = 0.1   # a piece overlapping a placed one by over this many sizes has no slot
LINE_PIECES = 2_000   # word pieces one out-of-order line holds; past it a fragment starts a line


@dataclass
class Line:
    text: str
    runs: List[Tuple[str, str]]   # (text, url); the texts join to `text`
    box: Tuple[float, float, float, float]   # x0, y0, x1, y1, y up; y0 is the baseline
    size: float
    order: int
    vertical: bool = False


def _unit(x, y, default):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 0 and math.isfinite(n) else default


def _extent(f):
    """The corner points of a fragment's glyph box: start and end on the baseline, and the same
    lifted by the size along the glyph's up."""
    ux, uy = _unit(f.dir_x, f.dir_y, (1.0, 0.0))
    vx, vy = _unit(f.up_x, f.up_y, (0.0, 1.0))
    ex, ey = f.x + ux * f.width, f.y + uy * f.width
    return [(f.x, f.y), (ex, ey), (f.x + vx * f.size, f.y + vy * f.size),
            (ex + vx * f.size, ey + vy * f.size)]


def box_of(frags):
    """The (x0, y0, x1, y1) box, y up, of the fragments' glyph boxes."""
    pts = [p for f in frags for p in _extent(f)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


class _LinkGrid:
    """Links bucketed by 64-unit cells, so a fragment only tests the links near it."""

    def __init__(self, links):
        self.cells = defaultdict(list)
        self.big = []
        self.all = []
        for box, url in links:
            try:
                x0, y0, x1, y1 = box
                x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
                if not all(math.isfinite(v) and abs(v) <= 1e9 for v in (x0, y0, x1, y1)):
                    continue
            except (TypeError, ValueError):
                continue
            item = ((x0, y0, x1, y1), url)
            self.all.append(item)
            cx0, cx1 = math.floor(x0 / GRID), math.floor(x1 / GRID)
            cy0, cy1 = math.floor(y0 / GRID), math.floor(y1 / GRID)
            if cx1 - cx0 > GRID_SPAN or cy1 - cy0 > GRID_SPAN:
                self.big.append(item)
                continue
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self.cells[(cx, cy)].append(item)

    def near(self, box):
        if not self.all:
            return []
        cx0, cx1 = math.floor(box[0] / GRID), math.floor(box[2] / GRID)
        cy0, cy1 = math.floor(box[1] / GRID), math.floor(box[3] / GRID)
        if cx1 - cx0 > GRID_SPAN or cy1 - cy0 > GRID_SPAN:
            return self.all
        seen, out = set(), []
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for item in self.cells.get((cx, cy), ()):
                    if id(item) not in seen:
                        seen.add(id(item))
                        out.append(item)
        for item in self.big:
            if id(item) not in seen:
                out.append(item)
        return out


def _glyph_urls(f, grid):
    """The url of each character of a fragment (the link whose box holds the glyph centre), or
    None when no link lies near it."""
    n = len(f.text)
    near = grid.near(box_of([f])) if grid is not None else []
    if not near:
        return None
    offs = f.char_offsets if len(f.char_offsets) == n + 1 else tuple(
        f.width * k / n for k in range(n + 1))
    ux, uy = _unit(f.dir_x, f.dir_y, (1.0, 0.0))
    vx, vy = _unit(f.up_x, f.up_y, (0.0, 1.0))
    out = []
    half = f.size / 2
    for k in range(n):
        mid = (offs[k] + offs[k + 1]) / 2
        cx, cy = f.x + ux * mid + vx * half, f.y + uy * mid + vy * half
        url = ""
        for (x0, y0, x1, y1), u in near:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                url = u
                break
        out.append(url)
    return out


def _chars_of(f, urls, spaces=True):
    """[(char, url)] of one fragment, with a space before each glyph whose `char_gaps` entry is
    over GAP x the size, when neither side is a space."""
    n = len(f.text)
    gaps = f.char_gaps if len(f.char_gaps) == n else ()
    out = []
    for k, ch in enumerate(f.text):
        if (spaces and gaps and k and gaps[k] > GAP * f.size
                and ch != " " and f.text[k - 1] != " "):
            out.append((" ", urls[k] if urls and urls[k] == urls[k - 1] else ""))
        out.append((ch, urls[k] if urls else ""))
    return out


def _runs(chars):
    """Chars as trimmed (text, url) runs."""
    a, b = 0, len(chars)
    while a < b and chars[a][0].isspace():
        a += 1
    while b > a and chars[b - 1][0].isspace():
        b -= 1
    runs = []
    for ch, url in chars[a:b]:
        if runs and runs[-1][1] == url:
            runs[-1][0].append(ch)
        else:
            runs.append(([ch], url))
    return [("".join(t), u) for t, u in runs]


def _make_line(frags, chars, vertical=False):
    runs = _runs(chars)
    main = max(frags, key=lambda f: len(f.text))
    return Line("".join(t for t, _ in runs), runs, box_of(frags), main.size,
                min(f.order for f in frags), vertical)


def _glyphs(f, urls):
    """[(start, end, char, url, size)] of a fragment's glyphs along the reading axis: glyph k
    starts at its char offset and ends where the next one starts, less the next one's gap."""
    n = len(f.text)
    s = along(f)
    offs = f.char_offsets if len(f.char_offsets) == n + 1 else tuple(
        f.width * k / n for k in range(n + 1))
    gaps = f.char_gaps if len(f.char_gaps) == n else (0.0,) * n
    out = []
    for k, ch in enumerate(f.text):
        end = offs[k + 1] - (gaps[k + 1] if k + 1 < n else 0.0)
        out.append((s + offs[k], s + max(offs[k], end), ch, urls[k] if urls else "", f.size))
    return out


def _pieces(f, urls):
    """[(start, end, [(char, url)], size)]: the fragment's words along the reading axis, split
    where a glyph's gap is over GAP x the size, with the spaces at their edges trimmed."""
    n = len(f.text)
    gaps = f.char_gaps if len(f.char_gaps) == n else ()
    out, cur = [], []
    for k, g in enumerate(_glyphs(f, urls) + [None]):
        if g is None or (k and gaps and gaps[k] > GAP * f.size):
            while cur and cur[-1][2].isspace():
                cur.pop()
            if cur:
                out.append((cur[0][0], max(c[1] for c in cur), [(c[2], c[3]) for c in cur], f.size))
            cur = []
        if g is not None and (cur or not g[2].isspace()):
            cur.append(g)
    return out


def _piece_chars(pieces):
    """[(char, url)] of pieces sorted by start: a space between two where the gap is over GAP x
    the later one's size."""
    out, end = [], None
    for start, stop, chars, size in pieces:
        if end is not None and start - end > GAP * size:
            out.append((" ", chars[0][1] if out[-1][1] == chars[0][1] else ""))
        out.extend(chars)
        end = stop if end is None else max(end, stop)
    return out


class _Row:
    """A line being built: its fragments in content order, its span along the reading axis and,
    once a fragment was drawn out of reading order, the word pieces of all of them by start."""

    def __init__(self, f, urls, direction, anchor):
        s = along(f)
        self.frags, self.parts, self.chars = [f], [(f, urls)], _chars_of(f, urls)
        self.direction, self.anchor = direction, anchor
        self.lo, self.hi, self.end = s, s + f.width, s + f.width
        self.starts = None   # piece starts, sorted; None while the row is in reading order
        self.pieces = None

    def index(self):
        if self.starts is None:
            self.pieces = sorted((p for f, urls in self.parts for p in _pieces(f, urls)),
                                 key=lambda p: p[0])
            self.starts = [p[0] for p in self.pieces]

    def free(self, pieces) -> bool:
        """Whether no piece overlaps a placed one by over PIECE_OVERLAP x the size."""
        for a, b, _, size in pieces:
            i = bisect.bisect_left(self.starts, b)
            for j in range(max(0, i - 4), i):
                p0, p1, _, other = self.pieces[j]
                if min(b, p1) - max(a, p0) > PIECE_OVERLAP * min(size, other):
                    return False
        return True

    def repeats(self, pieces) -> bool:
        """Whether every piece lies over a placed piece of the same text by over half its width:
        text drawn again over itself (a fake bold, a shadow), read once."""
        for a, b, chars, _ in pieces:
            text = "".join(c for c, _ in chars)
            i = bisect.bisect_left(self.starts, b)
            if not any(min(b, p1) - max(a, p0) > 0.5 * (b - a) and "".join(c for c, _ in got) == text
                       for p0, p1, got, _ in self.pieces[max(0, i - 4):i]):
                return False
        return True

    def _place(self, pieces):
        for p in pieces:
            k = bisect.bisect_right(self.starts, p[0])
            self.starts.insert(k, p[0])
            self.pieces.insert(k, p)

    def take_forward(self, f, urls) -> bool:
        """Join a fragment that starts where the last one ended: always while the row is in
        reading order, else only when its pieces find free slots. True also when it repeats
        placed text over itself, which is then left out."""
        if self.starts is None:
            s = along(f)
            if (s - self.end > GAP * f.size and self.chars[-1][0] != " "
                    and not f.text[0].isspace()):
                same = self.chars[-1][1] == (urls[0] if urls else "")
                self.chars.append((" ", self.chars[-1][1] if same else ""))
            self.chars.extend(_chars_of(f, urls))
        else:
            pieces = _pieces(f, urls)
            if not self.free(pieces):
                return self.repeats(pieces)
            self._place(pieces)
        self.frags.append(f)
        self.parts.append((f, urls))
        return True

    def take_back(self, f, urls, s) -> bool:
        """Join a fragment that starts before the last one ended: not when it lies wholly before
        the line over GUTTER sizes away or at another size (a box or a heading beside it), nor
        when one of its pieces overlaps a placed one. A fragment that repeats placed text over
        itself (a fake bold, a shadow) is taken and left out."""
        self.index()
        if len(self.starts) >= LINE_PIECES:
            return False
        if s + f.width < self.lo:
            near = self.pieces[0][3] if self.pieces else f.size
            if (self.lo - (s + f.width) > GUTTER * max(f.size, near)
                    or abs(f.size - near) > SIZE_TOLERANCE):
                return False
        pieces = _pieces(f, urls)
        if not self.free(pieces):
            return self.repeats(pieces)
        self._place(pieces)
        self.frags.append(f)
        self.parts.append((f, urls))
        return True

    def take_row(self, other) -> bool:
        """Take in the line built just before, on the same baseline, when it lies inside this
        one's span with every piece in a free slot (a row's right cell drawn before the row)."""
        if not (other.direction == self.direction and self.lo <= other.lo and other.hi <= self.hi
                and abs(other.anchor - self.anchor)
                <= LINE_TOLERANCE * max(other.frags[0].size, self.frags[0].size)):
            return False
        self.index()
        pieces = [p for f, urls in other.parts for p in _pieces(f, urls)]
        if len(self.starts) + len(pieces) > LINE_PIECES or not self.free(pieces):
            return False
        self._place(pieces)
        self.frags = other.frags + self.frags
        self.parts = other.parts + self.parts
        return True

    def line_chars(self):
        return self.chars if self.starts is None else _piece_chars(self.pieces)


def lines(fragments, links=()) -> List[Line]:
    """Lines in content order: consecutive fragments of one direction on one baseline are one
    line. A space goes between two glyphs when the gap is over GAP x the size and neither side
    is a space. Vertical text comes last, as `vertical_lines`.

    A fragment that starts before the last one ended (a row whose cells are drawn right cell
    first, words drawn into the gaps a later string leaves) joins the line as `_Row.take_back`
    allows. The line is then built from its word pieces in order of position, and each later
    fragment joins only where its pieces find free slots. A line that lies inside the next
    one's span, in free slots, is taken into it (`_Row.take_row`).

    `links` is [(box, url)]; a character whose centre lies in a link box carries its url.
    """
    frags = sorted((f for f in fragments if f.text and not f.hidden), key=lambda f: f.order)
    grid = _LinkGrid(links) if links else None
    out, vertical = [], []
    row, last = None, None

    def flush(row, last):
        """Emit the row; returns it, or None when it holds no text."""
        if row is None:
            return last
        if last is not None and row.take_row(last):
            out.pop()
        chars = row.line_chars()
        if any(not c[0].isspace() for c in chars):
            out.append(_make_line(row.frags, chars))
            return row
        return None

    for f in frags:
        d = writing_direction(f)
        if d == "down":
            vertical.append(f)
            continue
        a, s = across(f), along(f)
        urls = _glyph_urls(f, grid)
        joined = False
        if (row is not None and d == row.direction
                and abs(a - row.anchor) <= LINE_TOLERANCE * max(f.size, row.frags[-1].size)):
            if s >= row.end - LINE_TOLERANCE * f.size:
                joined = row.take_forward(f, urls)
            else:
                joined = row.take_back(f, urls, s)
        if not joined:
            last = flush(row, last)
            row = _Row(f, urls, d, a)
        row.lo, row.hi, row.end = min(row.lo, s), max(row.hi, s + f.width), s + f.width
    flush(row, last)
    return out + vertical_lines(vertical, links)


def vertical_lines(fragments, links=()) -> List[Line]:
    """Columns right to left, the characters of each from the top down, one Line per column.
    No spaces are added inside a column."""
    frags = [f for f in fragments if f.text and not f.hidden]
    grid = _LinkGrid(links) if links else None
    frags.sort(key=lambda f: (-f.x, -f.y, f.order))
    columns, anchor = [], 0.0
    for f in frags:
        if columns and anchor - f.x <= LINE_TOLERANCE * max(f.size, columns[-1][-1].size):
            columns[-1].append(f)
        else:
            columns.append([f])
            anchor = f.x
    out = []
    for col in columns:
        col.sort(key=lambda f: (-f.y, f.order))
        chars = [c for f in col for c in _chars_of(f, _glyph_urls(f, grid), spaces=False)]
        if any(not c[0].isspace() for c in chars):
            out.append(_make_line(col, chars, vertical=True))
    return out


# ---- reading order

def reading_order(lns, width) -> List[Line]:
    """Content order stays, unless over UPWARD of the steps from line to line go up by more than
    a line height. Then the lines are sorted into columns by `xps._columns` and each column by
    (top, x). Vertical lines keep their place after the horizontal ones."""
    flat = [ln for ln in lns if not ln.vertical]
    tail = [ln for ln in lns if ln.vertical]
    if len(flat) < 2:
        return list(lns)
    ups = sum(1 for a, b in zip(flat, flat[1:]) if b.box[3] - a.box[3] > a.size)
    if ups <= UPWARD * (len(flat) - 1):
        return list(lns)
    runs = [(-ln.box[3], ln.box[0], ln.size, ln.text, i) for i, ln in enumerate(flat)]
    out = []
    for column in xps._columns(runs, width):
        column = sorted(column, key=lambda r: (r[0], r[1]))
        out.extend(flat[r[4]] for r in column)
    return out + tail


# ---- joins and paragraphs

def _is_han_kana(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30ff or 0x3400 <= o <= 0x4dbf or 0x4e00 <= o <= 0x9fff
            or 0xf900 <= o <= 0xfaff or 0x20000 <= o <= 0x2fa1f)


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _join_parts(left: str, right: str, model) -> Tuple[bool, str]:
    """(drop the hyphen ending `left`, the separator between the two)."""
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return False, ""
    a, b = left[-1], right[0]
    if a == "-" and len(left) > 1 and left[-2].isalpha() and b.isalpha() and b.islower():
        return True, ""
    if _is_han_kana(a) and _is_han_kana(b):
        return False, ""
    if _is_hangul(a) and _is_hangul(b):
        return False, " " if model.joins_with_space(a, b) else ""
    return False, " "


def join_break(left: str, right: str, model) -> str:
    """Two lines as one text: a hyphen before a lowercase letter is dropped, Han and Kana join
    bare, Hangul joins as the spacing model says, anything else takes one space."""
    drop, sep = _join_parts(left, right, model)
    return (left[:-1] if drop else left) + sep + right


def paragraphs(lns, model) -> List[Para]:
    """Paragraphs: a baseline step over PARA_GAP x the line height (or a step that does not go
    forward) starts a new one. Links become Link inlines; nothing is bold."""
    groups, cur, prev = [], [], None
    for ln in lns:
        if prev is not None:
            if ln.vertical != prev.vertical:
                step = None
            elif ln.vertical:
                step = prev.box[0] - ln.box[0]
            else:
                step = prev.box[1] - ln.box[1]
            if step is None or step <= 0 or step > PARA_GAP * max(prev.size, ln.size):
                groups.append(cur)
                cur = []
        cur.append(ln)
        prev = ln
    if cur:
        groups.append(cur)
    out = []
    for group in groups:
        runs: List[Tuple[str, str]] = []
        for ln in group:
            if runs:
                drop, sep = _join_parts(runs[-1][0], ln.text, model)
                if drop:
                    t, u = runs.pop()
                    if t[:-1]:
                        runs.append((t[:-1], u))
                if sep:
                    runs.append((sep, ""))
            runs.extend(ln.runs)
        merged: List[Tuple[str, str]] = []
        for t, u in runs:
            if merged and merged[-1][1] == u:
                merged[-1] = (merged[-1][0] + t, u)
            elif t:
                merged.append((t, u))
        inlines = [Link(u, [Text(t)]) if u else Text(t) for t, u in merged]
        if inlines:
            out.append(Para(inlines))
    return out


# ---- headings

def body_size(all_lines) -> float:
    """The median line size."""
    sizes = [ln.size for ln in all_lines if ln.size > 0]
    return float(statistics.median(sizes)) if sizes else 0.0


def heading_level(text: str, size: float, body: float) -> Optional[int]:
    """3 at 1.5 x the body size, 4 at 1.25 x; none for a long line or one with under 2 letters."""
    if body <= 0 or size <= 0 or len(text) > 120 or sum(c.isalpha() for c in text) < 2:
        return None
    if size >= body * 1.5:
        return 3
    if size >= body * 1.25:
        return 4
    return None


def outline_level(depth: int) -> int:
    return min(6, depth + 2)


# ---- running headers and footers

EDGE_FRACTION = 0.12
MARGIN_GAP_FACTOR = 2.0   # the edge block must lie this many sizes clear of the body
_WHITESPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_UNREADABLE = re.compile(r"[\x00-\x1f\x7f-\x9f�]")
_PAGE_NUMBER = re.compile(
    r"(?:[-–—]\s*)?(?:\d{1,4}|[(\[]\d{1,4}[)\]]|"
    r"\d{1,4}\s*/\s*\d{1,4}|"
    r"(?:page|p\.?)\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?|"
    r"\d{1,4}\s*(?:쪽|페이지|页)|(?:쪽|페이지|页)\s*\d{1,4})"
    r"(?:\s*[-–—])?", re.IGNORECASE)
_ARTICLE_HEADING = re.compile(r"제\s*\d+\s*조(?:\s*\([^)]*\))?")


def _normal(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def _readable(ln) -> bool:
    visible = "".join(ln.text.split())
    return bool(visible) and len(_UNREADABLE.findall(visible)) * 2 < len(visible)


def _edge_block(items, bounds, zone):
    """Up to three (index, Line) nearest the page edge, each within 1.5 sizes of the last."""
    bottom, top = bounds
    span = (top - bottom) * EDGE_FRACTION
    if zone == "header":
        cand = heapq.nlargest(3, (it for it in items if top - span <= it[1].box[1] <= top
                                  and _readable(it[1])), key=lambda it: it[1].box[1])
    else:
        cand = heapq.nsmallest(3, (it for it in items if bottom <= it[1].box[1] <= bottom + span
                                   and _readable(it[1])), key=lambda it: it[1].box[1])
    block = []
    for it in cand:
        if block and abs(it[1].box[1] - block[-1][1].box[1]) > 1.5 * block[-1][1].size:
            break
        block.append(it)
    return block


def _margin_gap_ok(items, block, zone):
    """Is the edge block 2 x its size clear of the rest? None when nothing lies beyond it."""
    if not block:
        return False
    ids = {i for i, _ in block}
    if zone == "header":
        edge = min(ln.box[1] for _, ln in block)
        rest = [ln.box[1] for i, ln in items if i not in ids and ln.box[1] < edge]
        gap = edge - max(rest) if rest else None
    else:
        edge = max(ln.box[1] for _, ln in block)
        rest = [ln.box[1] for i, ln in items if i not in ids and ln.box[1] > edge]
        gap = min(rest) - edge if rest else None
    if gap is None:
        return None
    return gap >= MARGIN_GAP_FACTOR * max(ln.size for _, ln in block)


def running(pages, boxes) -> Tuple[List[str], List[str], List[set]]:
    """(headers, footers, drops): the repeated header and footer texts at their first page,
    and for each page the indexes of its lines to drop. Page-number lines are dropped when they
    count up from page to page, and never become a header or footer."""
    drops = [set() for _ in pages]
    text_pages = sum(1 for p in pages if any(not ln.vertical and ln.text for ln in p))
    if text_pages < 2:
        return [], [], drops
    occurrences = defaultdict(list)
    for n, (page, box) in enumerate(zip(pages, boxes)):
        items = [(i, ln) for i, ln in enumerate(page) if ln.text and not ln.vertical]
        for zone in ("header", "footer"):
            block = _edge_block(items, (box[1], box[3]), zone)
            gap_ok = _margin_gap_ok(items, block, zone)
            for i, ln in block:
                text = _normal(ln.text)
                if _ARTICLE_HEADING.fullmatch(text):
                    continue
                is_page = bool(_PAGE_NUMBER.fullmatch(text))
                if is_page:
                    text = _DIGITS.sub("#", text)
                occurrences[(zone, text, is_page)].append((n, i, ln, gap_ok))
    threshold = max(2, math.ceil(0.3 * text_pages))
    firsts = defaultdict(list)
    for key, found in occurrences.items():
        median = statistics.median(ln.size for _, _, ln, _ in found)
        found = [f for f in found if abs(f[2].size - median) <= 1.0]
        if len({n for n, _, _, _ in found}) < threshold:
            continue
        with_gap = {n for n, _, _, g in found if g is True}
        voting = {n for n, _, _, g in found if g is not None}
        if len(with_gap) < threshold or len(with_gap) * 2 < len(voting):
            continue
        if key[2]:
            found = [(n, i, ln, _DIGITS.search(ln.text)) for n, i, ln, _ in found]
            found = [f for f in found if f[3]]
            if len({f[0] for f in found}) < threshold:
                continue
            nums = [int(f[3].group()) for f in found]
            if nums != sorted(nums):
                continue
            for n, i, _, _ in found:
                drops[n].add(i)
            continue
        for n, i, ln, _ in found:
            drops[n].add(i)
        first = min(found, key=lambda f: f[0])
        firsts[(first[0], key[0])].append(first[2])
    headers, footers = [], []
    for (_, zone), lns in sorted(firsts.items(), key=lambda kv: kv[0][0]):
        texts = [ln.text for ln in sorted(lns, key=lambda ln: -ln.box[1])]
        (headers if zone == "header" else footers).extend(texts)
    return headers, footers, drops
