"""pdf page types and layout: the text Fragment, the page data the pdf reader passes around,
and the lines, paragraphs, headings and running headers both pdf engines share.

Fragment follows dochan 1.21.0 pdf/content.py (MIT) without its bold, italic, note, comment,
marked-content and artifact fields, and adds the font name, the text object index and the
hidden kind. `writing_direction`, `along` and `across` are dochan's.

The layout half is after dochan 1.21.0 (MIT): `SpacingModel` and `load_model` are pdf/spacing.py
(the table is `pdf_spacing.json`, dochan's korean_spacing.json byte for byte); the line join
(`join_break`) and the paragraph merge are pdf/layout.py `merge_lines`, with no full-line rule;
`running` is pdf/running.py `detect_running` over line indexes; the heading size ratios are
pdf/reader.py:85-100. The word gap and the body pitch measured on each page (`word_gap`,
`pitch`), link runs, vertical columns and the column fallback (`xps._columns`) are this reader's
own.

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

from .. import images, limits
from ..model import Cell, Heading, Link, ListItem, Para, Table, Text
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
    mcid: int = -1          # the marked-content id it is drawn in (ISO 32000-1 14.6), -1 for none
    artifact: bool = False  # drawn inside /Artifact marked content


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
    `kind` is "jpg", "png", "jpx", "ccitt", "jbig2" or "raw". `under`: the page's own text lies
    under it (`text_under`), as an OCR layer lies on its scan, so its pixels are not taken.
    `load`, when set, decodes the samples only when called and gives what `picture` gives.
    `mcid` and `artifact` are its marked content's, as a Fragment's."""
    box: Tuple[float, float, float, float]
    data: Optional[bytes]
    px: Tuple[int, int]
    kind: str
    under: bool = False
    load: Optional[Callable] = None
    mcid: int = -1
    artifact: bool = False

    def picture(self):
        """(bytes or None, flat): `data`, or the PNG `load` decodes and encodes; flat is True for
        a picture of one colour, which has no bytes."""
        return (self.data, False) if self.load is None else self.load()


def flat(rows, span: int) -> bool:
    """Whether every channel of 8-bit `rows` (`span` bytes a pixel) stays within
    limits.OCR_FLAT levels: a picture of one colour."""
    low, high = [255] * span, [0] * span
    for row in rows:
        for c in range(span):
            line = row[c::span]
            if line:
                low[c], high[c] = min(low[c], min(line)), max(high[c], max(line))
                if high[c] - low[c] > limits.OCR_FLAT:
                    return False
    return True


def picture(width: int, height: int, rows, alpha: bool):
    """(PNG at zlib level 1, False) of RGB (or RGBA) rows, or (None, True) when they are flat."""
    if flat(rows, 4 if alpha else 3):
        return None, True
    return images._png(width, height, rows, alpha, 1), False


def text_marks(frags):
    """(fragment, its non-space chars, its centre x, y) of each fragment with text, for `text_under`."""
    marks = []
    for f in frags:
        n = sum(1 for ch in f.text if not ch.isspace())
        if n:
            fb = box_of([f])
            marks.append((f, n, (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2))
    return marks


def text_under(box, marks, before, share) -> bool:
    """Whether the page's text (`text_marks`) lies under an image drawn in `box`: an invisible
    fragment has its centre in the box (hidden text laid on the image, read as pdf._hide reads
    it), or the fragments drawn before the image (`before(fragment)`) with their centre in the box
    hold at least `share` of the page's chars (the image is drawn over the page's text). A label
    the image's box merely reaches holds a small share."""
    total = sum(n for _f, n, _x, _y in marks)
    held = 0
    for f, n, cx, cy in marks:
        if (f.invisible or before(f)) and box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            if f.invisible:
                return True
            held += n
    return total > 0 and held >= share * total


@dataclass
class Comment:
    author: str
    text: str
    hidden: bool


@dataclass
class PageData:
    """Everything the reader takes from one page.

    `box` is the crop box in page space; `short` the sub-1-unit rule pieces kept for the
    short-dash supplement; `marks` are (box, pieces, rule) of the painted paths
    (`pdf_tables.PathCollector.marks`); `links` is
    [(box, url)], `fields` [(label, value, hidden, box or None)]; `cut` counts what a cap dropped; `render` is
    (box, dpi) -> png bytes or None; `unmapped` counts the glyphs dropped for having no Unicode.
    """
    number: int
    box: Tuple[float, float, float, float]
    rotate: int
    fragments: List[Fragment] = field(default_factory=list)
    segments: list = field(default_factory=list)
    short: list = field(default_factory=list)
    images: List[PageImage] = field(default_factory=list)
    marks: list = field(default_factory=list)
    links: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    chars: int = 0
    cut: Counter = field(default_factory=Counter)
    render: Optional[Callable] = None
    unmapped: int = 0


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
_COLUMNS = {"down": 0, "ltr": 90, "up": 180, "rtl": 270}  # the turn that makes it run down


def upright_turn(fragments, turn) -> int:
    """The quarter turn (0, 90, 180 or 270) to lay a page out in. Each visible char votes for
    the turn in which it reads as written: left to right for horizontal fonts, down the page
    for vertical writing, which is the chars of vertical fonts and, with them, the chars of
    other fonts set along the same direction (Latin turned clockwise inside vertical text). The
    page's /Rotate `turn` stays unless another turn has more votes: Latin turned with the page,
    sideways or upside down, is then read along its own lines."""
    counts, columns = Counter(), Counter()
    for f in fragments:
        if f.text and not f.hidden:
            n = sum(1 for ch in f.text if not ch.isspace())
            (columns if f.vertical else counts)[writing_direction(f)] += n
    votes = Counter()
    if columns:
        along_columns = max(columns, key=columns.get)
        votes[_COLUMNS[along_columns]] += sum(columns.values()) + counts.pop(along_columns, 0)
    for d, n in counts.items():
        votes[_UPRIGHT[d]] += n
    if not votes:
        return turn
    return max(votes, key=lambda t: (votes[t], t == turn))


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

GAP = 0.2
GAP_FLOOR = 0.08
GAP_CEIL = 1.0
GAP_BIN = 0.02
GAP_SAMPLES = 32
GAP_WORDS = 8
GAP_SPACES = 0.5
UPWARD = 0.3     # reading order is rebuilt when this share of line steps go up
LINE_TOLERANCE = 0.5   # fragments within this many sizes of one baseline are one line
LEADING = 1.2
PARA_GAP = 1.25
PITCH_BIN = 0.05
PITCH_MAX = 2.0
PITCH_SAMPLES = 3
FIT_SLACK = 2    # chars past a first word that must fit after a line for it to have been ended
LIST_MARK = re.compile(r"[•◦▪▫‣⁃●○■□]|\d{1,3}[.)]")
TIER_STEP = 1.25  # a step of over this many sizes between rows of vertical glyphs, none of any column between, parts tiers
# a table-of-contents entry's ending, read on a line's last TOC_TAIL chars, its trailing space
# stripped: a leader of TOC_LEADER_DOTS dots or more (TOC_DOTS counts each mark; marks may mix,
# and stand a single space apart), a gap of TOC_GAP, then a page label (TOC_LABEL): arabic, a
# well-formed roman numeral (so "...did" or "...civil" is no label) or a chapter page (3-1,
# A-1), bare or set between hyphens (- 3 -). A prose "..." is 3 dots, short of a leader.
TOC_DOTS = {".": 1, "·": 1, "・": 1, "．": 1, "_": 1, "‥": 2, "…": 3}
TOC_LEADER_DOTS = 4
TOC_GAP = " \t\xa0\u3000"  # space, tab, no-break space, ideographic space
_TOC_LABEL = (r"(?:\d+-\d+|[a-z]-\d+|\d+|(?=[ivxlcdm])m{0,4}(?:cm|cd|d?c{0,3})"
              r"(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))")
TOC_LABEL = re.compile(r"-[%s]*%s[%s]*-|%s" % (TOC_GAP, _TOC_LABEL, TOC_GAP, _TOC_LABEL),
                       re.IGNORECASE)
TOC_TAIL = 64
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
    direction: str = "ltr"
    tag: int = -1   # the structure element whose block it is, on a tagged page (`tagged_place`)


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


def _chars_of(f, urls, gap, spaces=True):
    """[(char, url)] of one fragment, with a space before each glyph whose `char_gaps` entry is
    over `gap` x the size, when neither side is a space."""
    n = len(f.text)
    gaps = f.char_gaps if len(f.char_gaps) == n else ()
    out = []
    for k, ch in enumerate(f.text):
        if (spaces and gaps and k and gaps[k] > gap * f.size
                and ch != " " and f.text[k - 1] != " "):
            out.append((" ", urls[k] if urls and urls[k] == urls[k - 1] else ""))
        out.append((ch, urls[k] if urls else ""))
    return out


def _plain(ch: str) -> str:
    """A mathematical alphanumeric symbol (U+1D400-1D7FF, and U+210E, the italic h that block
    leaves to Letterlike Symbols) as the letter or digit it styles."""
    return unicodedata.normalize("NFKC", ch) if "\U0001d400" <= ch <= "\U0001d7ff" or ch == "ℎ" else ch


def _runs(chars):
    """Chars as trimmed (text, url) runs, mathematical alphanumerics as plain letters."""
    a, b = 0, len(chars)
    while a < b and chars[a][0].isspace():
        a += 1
    while b > a and chars[b - 1][0].isspace():
        b -= 1
    runs = []
    for ch, url in chars[a:b]:
        ch = _plain(ch)
        if runs and runs[-1][1] == url:
            runs[-1][0].append(ch)
        else:
            runs.append(([ch], url))
    return [("".join(t), u) for t, u in runs]


def _make_line(frags, chars, vertical=False):
    runs = _runs(chars)
    main = max(frags, key=lambda f: len(f.text))
    return Line("".join(t for t, _ in runs), runs, box_of(frags), main.size,
                min(f.order for f in frags), vertical, writing_direction(main))


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


def _pieces(f, urls, gap):
    """[(start, end, [(char, url)], size)]: the fragment's words along the reading axis, split
    where a glyph's gap is over `gap` x the size, with the spaces at their edges trimmed."""
    n = len(f.text)
    gaps = f.char_gaps if len(f.char_gaps) == n else ()
    out, cur = [], []
    for k, g in enumerate(_glyphs(f, urls) + [None]):
        if g is None or (k and gaps and gaps[k] > gap * f.size):
            while cur and cur[-1][2].isspace():
                cur.pop()
            if cur:
                out.append((cur[0][0], max(c[1] for c in cur), [(c[2], c[3]) for c in cur], f.size))
            cur = []
        if g is not None and (cur or not g[2].isspace()):
            cur.append(g)
    return out


def _piece_chars(pieces, gap):
    """[(char, url)] of pieces sorted by start: a space between two where the gap is over `gap` x
    the later one's size."""
    out, end = [], None
    for start, stop, chars, size in pieces:
        if end is not None and start - end > gap * size:
            out.append((" ", chars[0][1] if out[-1][1] == chars[0][1] else ""))
        out.extend(chars)
        end = stop if end is None else max(end, stop)
    return out


class _Row:
    """A line being built: its fragments in content order, its span along the reading axis and,
    once a fragment was drawn out of reading order, the word pieces of all of them by start."""

    def __init__(self, f, urls, direction, anchor, gap):
        s = along(f)
        self.gap = gap
        self.frags, self.parts, self.chars = [f], [(f, urls)], _chars_of(f, urls, gap)
        self.direction, self.anchor = direction, anchor
        self.lo, self.hi, self.end = s, s + f.width, s + f.width
        self.starts = None   # piece starts, sorted; None while the row is in reading order
        self.pieces = None

    def index(self):
        if self.starts is None:
            self.pieces = sorted((p for f, urls in self.parts for p in _pieces(f, urls, self.gap)),
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
            if (s - self.end > self.gap * f.size and self.chars[-1][0] != " "
                    and not f.text[0].isspace()):
                same = self.chars[-1][1] == (urls[0] if urls else "")
                self.chars.append((" ", self.chars[-1][1] if same else ""))
            self.chars.extend(_chars_of(f, urls, self.gap))
        else:
            pieces = _pieces(f, urls, self.gap)
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
        pieces = _pieces(f, urls, self.gap)
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
        pieces = [p for f, urls in other.parts for p in _pieces(f, urls, self.gap)]
        if len(self.starts) + len(pieces) > LINE_PIECES or not self.free(pieces):
            return False
        self._place(pieces)
        self.frags = other.frags + self.frags
        self.parts = other.parts + self.parts
        return True

    def line_chars(self):
        return self.chars if self.starts is None else _piece_chars(self.pieces, self.gap)


def _em_gaps(frags):
    """The glyph gaps of fragments in content order, in sizes: each `char_gaps` entry between two
    glyphs that are no spaces, and the gap from a fragment to the next one on its baseline."""
    out, prev = [], None
    for f in frags:
        size = abs(f.size)
        if size <= 0 or not math.isfinite(size):
            prev = None
            continue
        n = len(f.text)
        if len(f.char_gaps) == n:
            out.extend(f.char_gaps[k] / size for k in range(1, n)
                       if not f.text[k].isspace() and not f.text[k - 1].isspace())
        if (prev is not None and writing_direction(prev) == writing_direction(f)
                and not prev.text[-1].isspace() and not f.text[0].isspace()):
            near = max(size, abs(prev.size))
            gap = along(f) - (along(prev) + prev.width)
            if abs(across(f) - across(prev)) <= LINE_TOLERANCE * near and gap >= -LINE_TOLERANCE * near:
                out.append(gap / near)
        prev = f
    return out


def _mode(bins):
    return max(bins, key=lambda b: (bins[b], -b))


def word_gap(fragments) -> float:
    """The gap, in sizes, over which two glyphs of these fragments stand a word apart, split from
    their own gaps (`_em_gaps`): binned by GAP_BIN, the most common bin is the gap between the
    letters of a word, and the most common bin from GAP_FLOOR up and two bins clear of it the
    gap between words. The threshold is the middle of the longest empty run of bins between the
    two, else of the emptiest bin, and never under GAP_FLOOR. Gaps past GAP_CEIL (a column
    gutter, a tab) are left out. With under GAP_SAMPLES gaps, under GAP_WORDS from GAP_FLOOR up,
    space glyphs as many as GAP_SPACES of those (the text sets its words apart itself, so its
    other gaps are letter spacing), or a most common gap of GAP_FLOOR or more (the fragments are
    words, as OCR gives them, and no gap is a letter gap), it is GAP."""
    frags = sorted((f for f in fragments if f.text and not f.hidden), key=lambda f: f.order)
    values = [v for v in _em_gaps(frags) if -GAP_CEIL <= v <= GAP_CEIL]
    if len(values) < GAP_SAMPLES:
        return GAP
    bins = Counter(math.floor(v / GAP_BIN) for v in values)
    letters = _mode(bins)
    if letters * GAP_BIN >= GAP_FLOOR:
        return GAP
    floor = max(letters + 2, math.ceil(GAP_FLOOR / GAP_BIN - 1e-9))
    above = Counter({b: n for b, n in bins.items() if b >= floor})
    wide = sum(above.values())
    if wide < GAP_WORDS or sum(ch.isspace() for f in frags for ch in f.text) >= GAP_SPACES * wide:
        return GAP
    words = _mode(above)
    runs, start = [], None
    for b in range(letters + 1, words + 1):
        if b < words and not bins.get(b):
            start = b if start is None else start
        elif start is not None:
            runs.append((b - start, -start))
            start = None
    if runs:
        width, start = max(runs)
        middle = (-start + width / 2) * GAP_BIN
    else:
        low = min(range(letters + 1, words), key=lambda b: (bins.get(b, 0), b))
        middle = (low + 0.5) * GAP_BIN
    return max(GAP_FLOOR, middle)


def _stacked(frags) -> set:
    """The ids of the one-glyph fragments that stand in a column: each next in content order to
    another one-glyph fragment at its x, up to 1.5 sizes below it."""
    out = set()
    for a, b in zip(frags, frags[1:]):
        if len(a.text.strip()) != 1 or len(b.text.strip()) != 1:
            continue
        size = max(abs(a.size), abs(b.size))
        if abs(a.x - b.x) <= LINE_TOLERANCE * size and 0 < a.y - b.y <= 1.5 * size:
            out.update((id(a), id(b)))
    return out


def lines(fragments, links=(), gap=None, whole=True) -> List[Line]:
    """Lines in content order: consecutive fragments of one direction on one baseline are one
    line. A space goes between two glyphs when the gap is over `gap` x the size (when None, the
    `word_gap` of these fragments) and neither side is a space. Vertical writing comes last, as
    `vertical_lines`: on a page with vertical fonts, the fragments that run down the page and the
    one-glyph fragments stacked in a column (`_stacked`: a horizontal font's glyphs placed one by
    one down a column, among a vertical font's punctuation). Other text running down the page is
    read as lines like any other. A line that goes on along the one before it (a piece set off by
    a superscript or a font change) is one line with it (`_whole`), unless `whole` is False.

    A fragment that starts before the last one ended (a row whose cells are drawn right cell
    first, words drawn into the gaps a later string leaves) joins the line as `_Row.take_back`
    allows. The line is then built from its word pieces in order of position, and each later
    fragment joins only where its pieces find free slots. A line that lies inside the next
    one's span, in free slots, is taken into it (`_Row.take_row`).

    `links` is [(box, url)]; a character whose centre lies in a link box carries its url.
    """
    frags = sorted((f for f in fragments if f.text and not f.hidden), key=lambda f: f.order)
    gap = word_gap(frags) if gap is None else gap
    grid = _LinkGrid(links) if links else None
    out, vertical = [], []
    row, last = None, None
    columns = any(f.vertical and writing_direction(f) == "down" for f in frags)
    stacked = _stacked(frags) if columns else set()

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
        if (d == "down" and (f.vertical or columns)) or id(f) in stacked:
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
            row = _Row(f, urls, d, a, gap)
        row.lo, row.hi, row.end = min(row.lo, s), max(row.hi, s + f.width), s + f.width
    flush(row, last)
    return (_whole(out) if whole else out) + vertical_lines(vertical, links)


def _tiers(frags) -> list:
    """The fragments by tier, top first: the bands of the page that their glyph origins cover,
    parted where the step from one row of glyphs to the next, in all columns, is over TIER_STEP
    sizes (the empty row between the tiers of a page set in vertical columns)."""
    spans = []
    for f in frags:
        n = len(f.text)
        last = f.char_offsets[n - 1] if len(f.char_offsets) == n + 1 else f.width * (n - 1) / n
        _ux, uy = _unit(f.dir_x, f.dir_y, (1.0, 0.0))
        spans.append((max(f.y, f.y + uy * last), min(f.y, f.y + uy * last), f))
    spans.sort(key=lambda s: (-s[0], s[2].order))
    tiers, low = [], 0.0
    for top, bottom, f in spans:
        if not tiers or low - top > TIER_STEP * f.size:
            tiers.append([])
            low = bottom
        tiers[-1].append(f)
        low = min(low, bottom)
    return tiers


def vertical_lines(fragments, links=()) -> List[Line]:
    """Tiers top down (`_tiers`), in each the columns right to left, the characters of each from
    the top down, one Line per column. No spaces are added inside a column."""
    grid = _LinkGrid(links) if links else None
    columns = []
    for tier in _tiers([f for f in fragments if f.text and not f.hidden]):
        tier.sort(key=lambda f: (-f.x, -f.y, f.order))
        start, anchor = len(columns), 0.0
        for f in tier:
            if len(columns) > start and anchor - f.x <= LINE_TOLERANCE * max(f.size, columns[-1][-1].size):
                columns[-1].append(f)
            else:
                columns.append([f])
                anchor = f.x
    out = []
    for col in columns:
        col.sort(key=lambda f: (-f.y, f.order))
        chars = [c for f in col for c in _chars_of(f, _glyph_urls(f, grid), GAP, spaces=False)]
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


def toc_entry(text: str) -> bool:
    """Whether a line ends as a table-of-contents entry does: a leader of 4 dots or more, then
    a page label. Only the line's end is looked at, and only its last TOC_TAIL chars, so a long
    line costs no more than a short one."""
    tail = text.rstrip()[-TOC_TAIL:]
    # a label holds no leader mark: the leader ends at the last mark, and the gap and a whole
    # label follow it
    end = max(tail.rfind(mark) for mark in TOC_DOTS) + 1
    if not end or TOC_LABEL.fullmatch(tail[end:].lstrip(TOC_GAP)) is None:
        return False
    dots = 0
    while end > 0:
        ch = tail[end - 1]
        if ch in TOC_DOTS:
            dots += TOC_DOTS[ch]
            if dots >= TOC_LEADER_DOTS:
                return True
        elif not (ch == " " and dots and end > 1 and tail[end - 2] in TOC_DOTS):
            return False  # one space between marks, no more
        end -= 1
    return False


def _step(prev, ln):
    """The baseline step from one line to the next, in sizes, or None between a vertical and a
    horizontal line."""
    if ln.vertical != prev.vertical:
        return None
    step = prev.box[0] - ln.box[0] if ln.vertical else prev.box[1] - ln.box[1]
    return step / max(prev.size, ln.size, 1e-9)


def pitch(lns) -> Tuple[float, float]:
    """(horizontal, vertical) body pitch of a page's lines, in sizes: the most common baseline
    step in PITCH_BIN bins, each step weighted by the chars of its line (a stack of one-glyph
    lines does not outweigh the text), as the median of the steps in that bin. A direction with
    under PITCH_SAMPLES steps of up to PITCH_MAX sizes has LEADING."""
    out = []
    for vertical in (False, True):
        steps = []
        for a, b in zip(lns, lns[1:]):
            if a.vertical == vertical and b.vertical == vertical:
                st = _step(a, b)
                if st is not None and 0 < st <= PITCH_MAX:
                    steps.append((st, len(b.text.strip()) or 1))
        if len(steps) < PITCH_SAMPLES:
            out.append(LEADING)
            continue
        bins = Counter()
        for st, w in steps:
            bins[math.floor(st / PITCH_BIN)] += w
        top = _mode(bins)
        out.append(float(statistics.median(st for st, _ in steps if math.floor(st / PITCH_BIN) == top)))
    return out[0], out[1]


def _breaks(prev, ln, body) -> bool:
    """Whether a block ends between two lines: a step over PARA_GAP x the body pitch `body`
    ((horizontal, vertical), as `pitch` gives it), or one that does not go forward."""
    step = _step(prev, ln)
    return step is None or step <= 0 or step > PARA_GAP * body[1 if ln.vertical else 0]


ALONG_SPACE = 0.15


def _along(prev, ln) -> bool:
    """Whether `ln` goes on along the visual line of `prev`: on its baseline within half a size,
    starting where it ends, under a size apart (a piece set off by a superscript or a font change)."""
    step = _step(prev, ln)
    if step is None or abs(step) >= 0.5 or prev.direction != "ltr" or ln.direction != "ltr":
        return False
    gap = prev.box[1] - ln.box[3] if ln.vertical else ln.box[0] - prev.box[2]
    return -0.5 * prev.size < gap < prev.size


def _whole(lns) -> list:
    """`lns` with each line that goes on along the one before it (`_along`) made one line with it, a space between
    them when they are over ALONG_SPACE of the smaller one's size apart. The line has the size that holds the most of
    its chars over all its pieces (a large number or initial before the body's words does not make a heading of
    them)."""
    out, chain = [], []

    def close():
        if len(chain) > 1:
            first = chain[0]
            runs, box = list(first.runs), first.box
            for prev, ln in zip(chain, chain[1:]):
                if ln.box[0] - prev.box[2] > ALONG_SPACE * min(prev.size, ln.size) and not ln.vertical:
                    runs.append((" ", ""))
                runs.extend(ln.runs)
                box = (min(box[0], ln.box[0]), min(box[1], ln.box[1]), max(box[2], ln.box[2]), max(box[3], ln.box[3]))
            chars = Counter()
            for ln in chain:
                chars[ln.size] += len(ln.text)
            out[-1] = replace(first, text="".join(t for t, _ in runs), runs=runs, box=box,
                              size=max(chars, key=chars.get))
        chain.clear()
    reach = None
    for ln in lns:
        if chain and _along(reach, ln):
            reach = replace(reach, box=(min(reach.box[0], ln.box[0]), min(reach.box[1], ln.box[1]),
                                        max(reach.box[2], ln.box[2]), max(reach.box[3], ln.box[3])))
        else:
            close()
            out.append(ln)
            reach = ln
        chain.append(ln)
    close()
    return out


def _ended(prev, ln, right) -> bool:
    """Whether `prev` was ended rather than wrapped before `ln`: `ln` starts with a list marker
    (LIST_MARK: a bullet, or a number of up to 3 digits closed by `.` or `)`), or past the end of
    `prev`, up to `right` (the right end of its block's lines so far and of `ln`), is room for
    the first word of `ln` (its first char in Han or Kana) and FIT_SLACK chars more, at the mean
    char width of `ln`. Horizontal left-to-right lines only."""
    words = ln.text.split()
    if prev.vertical or ln.vertical or prev.direction != "ltr" or ln.direction != "ltr" or not words:
        return False
    if LIST_MARK.fullmatch(words[0]):
        return True
    char = (ln.box[2] - ln.box[0]) / max(1, len(ln.text))
    first = words[0][:1] if _is_han_kana(words[0][0]) else words[0]
    return right - prev.box[2] > (len(first) + FIT_SLACK) * char


def _joined(group, model) -> list:
    """The lines of one block as inlines, joined by `join_break`; a run with a url is a Link."""
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
    return [Link(u, [Text(t)]) if u else Text(t) for t, u in merged]


def paragraphs(lns, model, body=None) -> List[Para]:
    """Paragraphs of `_groups`. Links become Link inlines; nothing is bold."""
    return [Para(got) for got in (_joined(group, model) for group in _groups(lns, body)) if got]


def _groups(lns, body=None) -> list:
    """The lines of each paragraph: a baseline step over PARA_GAP x the body pitch `body` (the
    `pitch` of these lines when None), a step that does not go forward, or a line that was ended
    rather than wrapped (`_ended`) starts a new one; a table-of-contents entry (`toc_entry`) ends its own."""
    body = pitch(lns) if body is None else body
    groups, cur, prev, right = [], [], None, -math.inf
    for ln in lns:
        if prev is not None and cur and (_breaks(prev, ln, body) or _ended(prev, ln, max(right, ln.box[2]))):
            groups.append(cur)
            cur, right = [], -math.inf
        cur.append(ln)
        right = max(right, ln.box[2])
        prev = ln
        if toc_entry(ln.text):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def inlines(ln) -> list:
    """A line's runs as Text inlines, a run with a url as a Link."""
    return [Link(u, [Text(t)]) if u else Text(t) for t, u in ln.runs if t]


def _slot(lns, item) -> int:
    """The index of the line `item` goes before. An item (top, x0, seq, block, x1) goes before the first line of its
    column (a line whose x range meets x0..x1) whose top is below its own top, else after the last line of its column;
    one with no x1, or whose x range meets no line, before the first line whose top is below its top."""
    top = item[0]
    if len(item) > 4:
        x0, x1 = item[1], item[4]
        last = None
        for i, ln in enumerate(lns):
            if ln.box[0] < x1 and ln.box[2] > x0:
                if ln.box[3] < top:
                    return i
                last = i
        if last is not None:
            return last + 1
    return next((i for i, ln in enumerate(lns) if ln.box[3] < top), len(lns))


def place(lns, items, levels, model) -> list:
    """Headings and paragraphs in line order, with each item placed by `_slot`, items of one slot by top, then x, then
    seq; an item slotted inside a paragraph goes after it. `items` are (top, x, seq, block) or (top, x0, seq, block,
    x1); `levels` maps a line index to its heading level. Paragraphs break by the `pitch` of all the lines; consecutive
    lines of one level within a paragraph's step of each other and at a size within SIZE_TOLERANCE are one heading (a
    title that wraps)."""
    slots = defaultdict(list)
    for it in sorted(items, key=lambda it: (-it[0], it[1], it[2])):
        slots[_slot(lns, it)].append(it)
    body = pitch(lns)
    out, run, head, held = [], [], [], {}

    def flush():
        start = 0
        for group in _groups(run, body):
            end = start + len(group)
            out.extend(it[3] for it in held.get(start, ()))
            got = _joined(group, model)
            if got:
                out.append(Para(got))
            out.extend(it[3] for at in range(start + 1, end) for it in held.get(at, ()))
            start = end
        run.clear()
        held.clear()
        if head:
            out.append(Heading(levels[head[0][0]], _joined([ln for _, ln in head], model)))
            head.clear()

    for i, ln in enumerate(lns):
        if i in slots:
            if run and not head and i not in levels:
                held[len(run)] = slots[i]
            else:
                flush()
                out.extend(it[3] for it in slots[i])
        if i in levels:
            if head and not (levels[head[-1][0]] == levels[i] and head[-1][0] == i - 1
                             and abs(head[-1][1].size - ln.size) <= SIZE_TOLERANCE
                             and not _breaks(head[-1][1], ln, body)):
                flush()
            elif run:
                flush()
            head.append((i, ln))
        else:
            if head:
                flush()
            run.append(ln)
    flush()
    out.extend(it[3] for it in slots.get(len(lns), ()))
    return out


# ---- headings

def body_size(all_lines) -> float:
    """The size the median char is set in, chars other than spaces counted: how the text is cut
    into lines (a tagged table's cells, each its own line) moves no body."""
    sizes = sorted((ln.size, sum(1 for ch in ln.text if not ch.isspace())) for ln in all_lines if ln.size > 0)
    half, seen = sum(n for _s, n in sizes) / 2, 0
    for size, n in sizes:
        seen += n
        if seen >= half and seen:
            return float(size)
    return 0.0


HEAD_STEP = 1.15    # x the body size a rare size starts at to be a heading's
HEAD_SHARE = 0.03   # of a document's chars set at a size or above it: under it the size is rare
HEAD_CHARS = 60     # chars of a line that is a heading by a rare size under 1.25 x the body


def heading_floor(all_lines, body: float) -> float:
    """The smallest size from HEAD_STEP x `body` up at which and above which under HEAD_SHARE of
    the chars (spaces aside) are set, for `heading_level`; 1.25 x the body when there is none."""
    sizes = Counter()
    for ln in all_lines:
        if ln.size > 0:
            sizes[ln.size] += sum(1 for ch in ln.text if not ch.isspace())
    most, seen, floor = HEAD_SHARE * sum(sizes.values()), 0, body * 1.25
    for size in sorted(sizes, reverse=True):
        seen += sizes[size]
        if size < body * HEAD_STEP or seen > most:
            break
        floor = min(floor, size)
    return floor


def heading_level(text: str, size: float, body: float, floor: Optional[float] = None) -> Optional[int]:
    """3 at 1.5 x the body size, 4 at 1.25 x, and 4 for a line of up to HEAD_CHARS chars from
    `floor` (`heading_floor`) up; none for a long line or one with under 2 letters."""
    if body <= 0 or size <= 0 or len(text) > 120 or sum(c.isalpha() for c in text) < 2:
        return None
    if size >= body * 1.5:
        return 3
    if size >= body * 1.25 or (floor is not None and size >= floor and len(text) <= HEAD_CHARS):
        return 4
    return None


def outline_level(depth: int) -> int:
    return min(6, depth + 2)


# ---- tagged pages

def span_width(frags) -> float:
    """The width from the first fragment's start to the farthest end of `frags`, along the first's
    direction: the width of one fragment that stands for them all."""
    first = frags[0]
    norm = math.hypot(first.dir_x, first.dir_y) or 1.0
    ends = [((f.x - first.x) * first.dir_x + (f.y - first.y) * first.dir_y) / norm + f.width for f in frags]
    return max(0.0, max(ends))


def _marker(text: str) -> Tuple[str, str]:
    """(list marker, text left in the item) of a list label: a number like `1.` or `2)` is the
    marker, a bullet glyph gives `-`, any other label stays in the item's text after `-`."""
    label = text.strip()
    if re.fullmatch(r"\d{1,4}[.)]", label):
        return label, ""
    if len(label) <= 2 and not any(ch.isalnum() for ch in label):
        return "-", ""
    return "-", label


def tagged_place(lns, items, levels, model, tags, order, alts) -> list:
    """The blocks of a tagged page (ISO 32000-1 14.8.4), its lines in structure order: each run of
    lines with one `tag` is that block element's, and its type makes the block. A heading type is a
    Heading (H1-H6 at `outline_level` of their number, H and Title as H1), and so is a P whose
    lines all have one outline level in `levels` (a tree that tags its headings as paragraphs); a
    list item's label and body are a ListItem, its level the number of lists around it less one,
    its marker the label's (`_marker`); the first cell of a table makes its Table from the tree's
    rows that hold the page's cells (`tags.rows`), each cell its own lines' text, a TH a header
    cell, /RowSpan and /ColSpan kept; a Figure with an /Alt that no picture took (`alts`, {figure
    element: alt}) gives `Figure: <alt>` before its own text, or, with no lines on the page, at
    its place in structure order (`order`, {block: walk sequence}); any other block is one Para of
    its lines. Lines of no block (tag -1) are read by `place` with their outline `levels`. Items
    go where `_slot` puts them, as in `place`."""
    slots = defaultdict(list)
    for it in sorted(items, key=lambda it: (-it[0], it[1], it[2])):
        slots[_slot(lns, it)].append(it)
    of = defaultdict(list)
    for ln in lns:
        of[ln.tag].append(ln)
    pending = sorted((order.get(f, math.inf), f) for f in alts if f not in of)
    out, tables, labels = [], set(), {}

    def figure_alt(f):
        out.append(Para([Text("Figure: " + alts.pop(f))]))

    i = 0
    while i < len(lns):
        tag = lns[i].tag
        j = i
        while j < len(lns) and lns[j].tag == tag:
            j += 1
        for x in range(i, j):
            out.extend(it[3] for it in slots.get(x, ()))
        while pending and tag >= 0 and pending[0][0] < order.get(tag, math.inf):
            figure_alt(pending.pop(0)[1])
        group = lns[i:j]
        i = j
        if tag < 0:
            run = {k: levels[n] for k, n in enumerate(range(j - len(group), j)) if n in levels}
            out.extend(place(group, [], run, model))
            continue
        kind = tags.elements[tag].kind
        inl = _joined(group, model)
        level = {levels.get(n) for n in range(j - len(group), j)}
        if kind == "P" and len(level) == 1 and None not in level:
            out.append(Heading(level.pop(), inl))
        elif kind in ("Title", "H", "H1", "H2", "H3", "H4", "H5", "H6"):
            out.append(Heading(outline_level(int(kind[1:]) if kind[1:].isdigit() else 1), inl))
        elif kind == "Lbl":
            item = tags.nearest(tag, ("LI",))
            nxt = lns[j].tag if j < len(lns) else -1
            if item >= 0 and nxt >= 0 and tags.nearest(nxt, ("LI",)) == item and tags.elements[nxt].kind != "Lbl":
                labels[item] = "".join(ln.text + " " for ln in group).strip()
            else:
                out.append(ListItem(max(0, tags.depth(tag, "L") - 1), "-", inl))
        elif kind in ("LBody", "LI"):
            marker, rest = _marker(labels.pop(tags.nearest(tag, ("LI",)), ""))
            out.append(ListItem(max(0, tags.depth(tag, "L") - 1), marker,
                                ([Text(rest + " ")] if rest else []) + inl))
        elif kind in ("TD", "TH") and tags.nearest(tag, ("Table",)) >= 0:
            table = tags.nearest(tag, ("Table",))
            if table not in tables:
                tables.add(table)
                cells = [c for c in of if c >= 0 and tags.elements[c].kind in ("TD", "TH")]
                rows = [[Cell([Para(_joined(of[c], model))] if of.get(c) else [], *tags.elements[c].span,
                              header=tags.elements[c].kind == "TH") for c in row]
                        for row in tags.rows(table, cells)]
                if any(cell.blocks for row in rows for cell in row):
                    out.append(Table([row for row in rows if row]))
        else:
            if kind == "Figure" and tag in alts:
                figure_alt(tag)
            out.append(Para(inl))
    out.extend(it[3] for it in slots.get(len(lns), ()))
    for _seq, f in pending:
        figure_alt(f)
    return out


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


def _side_lines(items, box):
    """(index, Line) of the lines turned along the page (running up or down in a horizontal font)
    that lie wholly in the left or right EDGE_FRACTION of its width: a margin notice."""
    span = (box[2] - box[0]) * EDGE_FRACTION
    return [(i, ln) for i, ln in items if ln.direction in ("up", "down") and _readable(ln)
            and (ln.box[2] <= box[0] + span or ln.box[0] >= box[2] - span)]


def _key(text: str) -> str:
    """A running line's text with its numbers as #, when letters are left to tell it apart (a
    page number inside a header); a line of numbers alone stays as it is."""
    keyed = _DIGITS.sub("#", text)
    return keyed if sum(ch.isalpha() for ch in keyed) >= 3 else text


def marked_running(pages, boxes, headers, footers):
    """Add to `headers` and `footers` the /Artifact lines of tagged pages (`pages`, each page's lines
    in its `boxes` view), each text once a document by its `_key`: a line in the upper half of its
    page is a header. Page-number lines and the texts already there are left out."""
    seen = {_key(_normal(t)) for t in headers + footers}
    for page, box in zip(pages, boxes):
        for ln in sorted(page, key=lambda ln: -ln.box[1]):
            text = _normal(ln.text)
            if not text or _PAGE_NUMBER.fullmatch(text) or _key(text) in seen:
                continue
            seen.add(_key(text))
            (headers if 2 * ln.box[1] > box[1] + box[3] else footers).append(ln.text)


def running(pages, boxes) -> Tuple[List[str], List[str], List[set]]:
    """(headers, footers, drops): the repeated header and footer texts at their first page,
    and for each page the indexes of its lines to drop. A line repeats when its text, numbers
    read as # (`_key`), comes back on the pages, in the top or bottom edge block or turned in a
    side margin (`_side_lines`, kept with the headers). Page-number lines are dropped when they
    count up from page to page, and never become a header or footer."""
    drops = [set() for _ in pages]
    text_pages = sum(1 for p in pages if any(not ln.vertical and ln.text for ln in p))
    if text_pages < 2:
        return [], [], drops
    occurrences = defaultdict(list)
    for n, (page, box) in enumerate(zip(pages, boxes)):
        items = [(i, ln) for i, ln in enumerate(page) if ln.text and not ln.vertical]
        zones = [(zone, block, _margin_gap_ok(items, block, zone))
                 for zone, block in ((z, _edge_block(items, (box[1], box[3]), z)) for z in ("header", "footer"))]
        zones.append(("header", _side_lines(items, box), True))
        for zone, block, gap_ok in zones:
            for i, ln in block:
                text = _normal(ln.text)
                if _ARTICLE_HEADING.fullmatch(text):
                    continue
                is_page = bool(_PAGE_NUMBER.fullmatch(text))
                text = _DIGITS.sub("#", text) if is_page else _key(text)
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
