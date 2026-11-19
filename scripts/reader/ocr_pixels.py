"""The pixels OCR reads: Gray8 bitmaps the reader decoded itself.

Every operation here works on whole rows or columns as byte slices, or on rows as big integers, so no Python loop
runs per pixel.
"""
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass

from . import limits


@dataclass(frozen=True)
class Gray:
    """A Gray8 bitmap, row-major with no padding: 0 is black, 255 is white."""
    w: int
    h: int
    data: bytes

    def __post_init__(self):
        if self.w <= 0 or self.h <= 0 or len(self.data) != self.w * self.h:
            raise ValueError(f"Gray {self.w}x{self.h} with {len(self.data)} bytes")


def from_pixels(w: int, h: int, buf, channels: int, stride: int | None = None) -> Gray:
    """The green byte of each pixel of a w x h buffer of `channels` bytes a pixel (1 gray; 3 BGR or RGB; 4 BGRA or
    RGBA: green is byte 1 either way), rows `stride` bytes apart (w * channels when None). ValueError for a buffer
    too short for its rows or an unknown channel count."""
    if channels not in (1, 3, 4) or w <= 0 or h <= 0:
        raise ValueError(f"from_pixels {w}x{h} with {channels} channels")
    row = w * channels
    stride = row if stride is None else stride
    if stride < row or len(buf) < stride * (h - 1) + row:
        raise ValueError(f"from_pixels {w}x{h}x{channels}: {len(buf)} bytes, stride {stride}")
    buf = bytes(buf) if not isinstance(buf, bytes) else buf
    first = 1 if channels > 1 else 0
    if stride == row:
        return Gray(w, h, buf[first:row * h:channels])
    return Gray(w, h, b"".join(buf[y * stride + first:y * stride + row:channels] for y in range(h)))


_INVERT = bytes(range(255, -1, -1))  # v -> 255 - v


def on_white(g: Gray, alpha) -> Gray:
    """`g` with each pixel's alpha (one byte a pixel, 0 clear, 255 opaque) laid on white as G | (255 - A): a clear
    pixel is white and an opaque one keeps its value; a partial one is never darker than its own value nor than
    255 - A (an approximation of G*A/255 + 255 - A, at C speed on big integers)."""
    alpha = bytes(alpha)
    if len(alpha) != len(g.data):
        raise ValueError(f"on_white: {len(alpha)} alpha bytes for {len(g.data)} pixels")
    n = len(alpha)
    clear = int.from_bytes(alpha.translate(_INVERT), "big")
    return Gray(g.w, g.h, (int.from_bytes(g.data, "big") | clear).to_bytes(n, "big"))


def reduction(w: int, h: int, max_pixels: int | None, max_side: int | None = None) -> tuple[int, int]:
    """(kx, ky), the powers of two `fit` divides the sides by: each side is halved (floor) while the area is over
    `max_pixels` or the long side over `max_side`; a side of 1 is not halved again. GDI+ and the image venv scale by
    the same factors, so every decoder gives the same size."""
    kx = ky = 1
    cw, ch = w, h
    while ((max_pixels is not None and cw * ch > max_pixels)
           or (max_side is not None and max(cw, ch) > max_side)) and (cw > 1 or ch > 1):
        if cw > 1:
            cw, kx = cw // 2, kx * 2
        if ch > 1:
            ch, ky = ch // 2, ky * 2
    return kx, ky


def _mean(a, b) -> bytes:
    """The byte-wise rounded-down mean of two equal-length byte strings, as big integers: x + y = 2(x & y) + (x ^ y),
    and the mask clears each byte's low bit so the shift never carries into the next byte."""
    n = len(a)
    if not n:
        return b""
    x, y = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    mask = int.from_bytes(b"\xfe" * n, "big")
    return ((x & y) + (((x ^ y) & mask) >> 1)).to_bytes(n, "big")


def _halve(data: bytes, w: int, h: int, cols: bool, rows: bool) -> tuple[bytes, int, int]:
    """One 2x2 (or 2x1, 1x2) box reduction; an odd last row or column is dropped."""
    nw = w // 2 * 2 if cols else w
    mv = memoryview(data)
    if rows:
        nh = h // 2
        data = _mean(b"".join(mv[2 * r * w:2 * r * w + nw] for r in range(nh)),
                     b"".join(mv[(2 * r + 1) * w:(2 * r + 1) * w + nw] for r in range(nh)))
        h = nh
    elif nw != w:
        data = b"".join(mv[r * w:r * w + nw] for r in range(h))
    if cols:
        data = _mean(data[0::2], data[1::2])  # nw is even, so the pairs never cross a row
        nw //= 2
    return data, nw, h


def fit(g: Gray, max_pixels: int | None, *, max_side: int | None = None) -> Gray:
    """`g` reduced by repeated 2x2 averaging (sides divided by `reduction`'s powers of two) until its area is within
    `max_pixels` (None: no area cap) and its long side within `max_side`. `g` itself when it already fits."""
    kx, ky = reduction(g.w, g.h, max_pixels, max_side)
    data, w, h = g.data, g.w, g.h
    while kx > 1 or ky > 1:
        data, w, h = _halve(data, w, h, kx > 1, ky > 1)
        kx, ky = max(1, kx // 2), max(1, ky // 2)
    return g if (w, h) == (g.w, g.h) else Gray(w, h, data)


def rotate(g: Gray, turn: int) -> Gray:
    """`g` turned clockwise by 0, 90, 180 or 270 degrees."""
    turn %= 360
    d, w = g.data, g.w
    if turn == 0:
        return g
    if turn == 180:
        return Gray(g.w, g.h, d[::-1])
    if turn == 90:  # new row c is old column c, read bottom to top
        return Gray(g.h, g.w, b"".join(d[c::w][::-1] for c in range(w)))
    if turn == 270:  # new row j is old column w-1-j, read top to bottom
        return Gray(g.h, g.w, b"".join(d[c::w] for c in range(w - 1, -1, -1)))
    raise ValueError(f"rotate by {turn}")


def box_pixels(w: int, h: int, box) -> tuple[int, int, int, int] | None:
    """A fractional box (x0, y0, x1, y1), y down, as whole pixels inside w x h, at least 1 x 1; None when a value is
    not a finite number."""
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    px0 = min(max(math.floor(x0 * w), 0), w - 1)
    py0 = min(max(math.floor(y0 * h), 0), h - 1)
    px1 = min(max(math.ceil(x1 * w), px0 + 1), w)
    py1 = min(max(math.ceil(y1 * h), py0 + 1), h)
    return px0, py0, px1, py1


def crop_png(g: Gray, box, long: int) -> bytes:
    """A gray PNG of the fractional box (x0, y0, x1, y1) of `g`, keeping every step-th row and column so the long side
    is at most `long` (decision 6). ValueError for a box that is not numbers."""
    from . import images
    px = box_pixels(g.w, g.h, box)
    if px is None:
        raise ValueError(f"crop box {box!r}")
    x0, y0, x1, y1 = px
    edge = max(x1 - x0, y1 - y0)
    step = -(-edge // max(1, long)) if edge > long else 1
    d, w = g.data, g.w
    rows = range(y0, y1, step)
    data = b"".join(d[r * w + x0:r * w + x1:step] for r in rows)
    return images.gray_png(Gray(len(range(x0, x1, step)), len(rows), data))


# Page analysis after recognition: the threshold that splits ink from paper, the ruled-table rules, and the dark
# pixels no word box covers. "Dark" is a value under the threshold.


def _dark(thr: int) -> bytes:
    """A translate table mapping a dark pixel (value < thr) to 1 and any other to 0."""
    thr = min(max(int(thr), 0), 256)
    return b"\x01" * thr + b"\x00" * (256 - thr)


def threshold(g: Gray) -> int:
    """Otsu's threshold over a histogram of every 4th pixel: the value t such that pixels under t are ink. When a
    span of cut points ties (a histogram gap between paper and ink), the middle of the span is taken. 0 (no pixel is
    dark) when the two classes' means are under `_INK_GAP` levels apart: paper noise or a vignette, not ink. 128 for
    a page of one value."""
    hist = Counter(g.data[::4])
    n = sum(hist.values())
    s = sum(v * c for v, c in hist.items())
    n0 = s0 = 0
    best = first = last = None
    for k in range(255):  # cut after k: classes 0..k and k+1..255
        c = hist.get(k, 0)
        n0 += c
        s0 += k * c
        if n0 == 0:
            continue
        if n0 == n:
            break
        num, den = (s * n0 - n * s0) ** 2, n0 * (n - n0)  # between-class variance times n**3, as a fraction
        if best is None or num * best[1] > best[0] * den:
            best, first, last = (num, den), k, k
        elif num * best[1] == best[0] * den:
            last = k
    if first is None:
        return 128
    t = (first + last) // 2 + 1
    n0 = sum(c for v, c in hist.items() if v < t)
    s0 = sum(v * c for v, c in hist.items() if v < t)
    n1, s1 = n - n0, s - s0
    if s1 * n0 - s0 * n1 < _INK_GAP * n0 * n1:  # mean(paper) - mean(ink) < _INK_GAP, both sides times n0 * n1
        return 0
    return t


_INK_GAP = 40  # levels the ink and paper means must differ by for a page to have ink


_RUNS = 2000  # runs one row or column is scanned for: a noise page must not stall


def _runs(lines, n: int) -> list[list[tuple[int, int]]]:
    """For each 0/1 line, the spans (start, end) of its runs of at least n ones, the first `_RUNS` only."""
    pat = re.compile(rb"\x01{%d,}" % n)
    needle = b"\x01" * n
    out = []
    for line in lines:
        found = []
        if needle in line:
            for m in pat.finditer(line):
                found.append(m.span())
                if len(found) >= _RUNS:
                    break
        out.append(found)
    return out


def _merge(lines: list[list[tuple[int, int]]]) -> list[tuple[int, int, int, int]]:
    """Runs on adjacent lines merged when they overlap by at least 90 % of the longer one (each run is compared with
    the runs of the line before only), as groups (start, end, first line, last line + 1)."""
    done = []
    prev = []  # the line before: (start, end, group) for each run, a group being [lo, hi, first line, last line]
    for li, runs in enumerate(lines):
        cur, j = [], 0
        for s, e in runs:
            while j < len(prev) and prev[j][1] <= s:  # both lines' runs are sorted and disjoint
                j += 1
            grp = None
            k = j
            while k < len(prev) and prev[k][0] < e:  # every run of the line before that overlaps this one
                ps, pe, p = prev[k]
                ov = min(e, pe) - max(s, ps)
                if ov > 0 and ov * 10 >= 9 * max(e - s, pe - ps) and p[3] != li:
                    grp = p
                    break
                k += 1
            if grp is None:
                grp = [s, e, li, li]
            else:
                grp[0], grp[1], grp[3] = min(grp[0], s), max(grp[1], e), li
            cur.append((s, e, grp))
        done.extend(p for _, _, p in prev if p[3] != li)
        prev = cur
    done.extend(p for _, _, p in prev)
    return [(p[0], p[1], p[2], p[3] + 1) for p in done]


class _Near:
    """Groups (lo, hi, first line, last line + 1) indexed by every line they occupy, each line's entries sorted by
    lo, so the groups that overlap a span on a range of lines are found without scanning every group."""

    def __init__(self, groups):
        self.groups = groups
        self.at: dict[int, list[tuple[int, int]]] = {}
        for i, (lo, hi, l0, l1) in enumerate(groups):
            for li in range(l0, l1):
                self.at.setdefault(li, []).append((lo, i))
        for entries in self.at.values():
            entries.sort()
        self.span = max((hi - lo for lo, hi, _, _ in groups), default=0)

    def overlapping(self, lo: int, hi: int, first: int, stop: int) -> set[int]:
        """The groups on lines first..stop-1 whose span overlaps lo..hi."""
        out = set()
        for li in range(first, stop):
            entries = self.at.get(li)
            if not entries:
                continue
            a = bisect_right(entries, (lo - self.span, len(self.groups)))  # an entry's hi is at most lo + span
            b = bisect_left(entries, (hi, -1))
            out.update(i for glo, i in entries[a:b] if self.groups[i][1] > lo)
        return out


def _coalesce(groups: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Groups whose lines touch or overlap joined into one, transitively, when their spans overlap by at least 90 %
    of the shorter one. The spans compared are those of the blocks joined so far, so the anti-aliased edge row of a
    rule rejoins it, and the fragments a speckled dark area splits into, its edge rows included, join into one thick
    block."""
    parent = list(range(len(groups)))
    box = [list(g) for g in groups]  # each root's joined (lo, hi, first line, last line + 1)

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    near = _Near(groups)
    order = sorted(range(len(groups)), key=lambda i: (groups[i][2], groups[i][0]))
    pairs = [(i, j) for i in order for j in sorted(near.overlapping(*groups[i][:2], groups[i][2] - 1, groups[i][3] + 1))
             if j != i]
    for _ in range(_COALESCE_PASSES):  # again while anything joins: a block that grew may now hold an edge fragment
        joined = False
        for i, j in pairs:
            ri, rj = root(i), root(j)
            if ri == rj:
                continue
            a, b = box[ri], box[rj]
            ov = min(a[1], b[1]) - max(a[0], b[0])
            if ov * 10 >= 9 * min(a[1] - a[0], b[1] - b[0]):
                parent[rj] = ri
                a[0], a[1], a[2], a[3] = min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])
                joined = True
        if not joined:
            break
    return [tuple(box[i]) for i in range(len(groups)) if parent[i] == i]


_COALESCE_PASSES = 4  # passes over the touching pairs; a texture needs two or three, a ruled table one


def _rules_along(lines: list[list[tuple[int, int]]], thick: int, dark) -> list[tuple[int, int, int, int]]:
    """The rules among one direction's runs, as (lo, hi, first line, last line + 1). Runs merge into groups
    (`_merge`), the groups coalesce (`_coalesce`), and a group is a rule when it is at most `thick` lines thick and
    neither guard drops it:
    - neighbour guard: the line just before it and the line just after it are both more than half dark along its
      span (`dark(line, lo, hi)` counts; a line off the page is paper): it lies inside a dark area;
    - stack guard: more than two other groups cover at least half its span within 2 * thick + 2 lines of it:
      stripes, halftone or banding, never a ruled table's rules."""
    groups = _coalesce(_merge(lines))
    near = _Near(groups)
    reach = 2 * thick + 2
    out = []
    for i, (lo, hi, l0, l1) in enumerate(groups):
        if l1 - l0 > thick:
            continue
        span = hi - lo
        if dark(l0 - 1, lo, hi) * 2 > span and dark(l1, lo, hi) * 2 > span:
            continue
        stack = 0
        for j in near.overlapping(lo, hi, l0 - reach, l1 + reach):
            if j != i and (min(hi, groups[j][1]) - max(lo, groups[j][0])) * 2 >= span:
                stack += 1
                if stack > 2:
                    break
        if stack <= 2:
            out.append((lo, hi, l0, l1))
    return out


def rules(g: Gray, dpi: float, thr: int) -> list[tuple[int, int, int, int]]:
    """The ruled-table rules of a page as pixel boxes (x0, y0, x1, y1), y down, exclusive ends: horizontal rules
    first, then vertical ones. A rule is a dark run at least `OCR_RULE` of the page side long (rows: the width;
    columns: the height); runs on adjacent rows (columns) that overlap by 90 % merge, and a group thicker than
    `OCR_RULE_THICK` inch (at least 2 px) is a bar, not a rule. Groups inside a dark area or stacked like stripes are
    texture, not rules (`_rules_along`), and a page with more than `OCR_RULES_MAX` rules has none. `dpi` is clamped to
    50-1200; a value that is not a finite number is read as `OCR_DPI`."""
    try:
        dpi = float(dpi)
    except (TypeError, ValueError):
        dpi = float(limits.OCR_DPI)
    if not math.isfinite(dpi):
        dpi = float(limits.OCR_DPI)
    dpi = min(max(dpi, 50.0), 1200.0)
    w, h = g.w, g.h
    m = g.data.translate(_dark(thr))
    thick = max(2, round(dpi * limits.OCR_RULE_THICK))

    def row_dark(r, lo, hi):
        return m.count(1, r * w + lo, r * w + hi) if 0 <= r < h else 0

    def col_dark(c, lo, hi):
        return m[lo * w + c:(hi - 1) * w + c + 1:w].count(1) if 0 <= c < w else 0

    rows = _runs((m[r * w:(r + 1) * w] for r in range(h)), max(1, math.ceil(limits.OCR_RULE * w)))
    out = sorted(((x0, y0, x1, y1) for x0, x1, y0, y1 in _rules_along(rows, thick, row_dark)),
                 key=lambda b: (b[1], b[0]))
    cols = _runs((m[c::w] for c in range(w)), max(1, math.ceil(limits.OCR_RULE * h)))
    out += sorted((x0, y0, x1, y1) for y0, y1, x0, x1 in _rules_along(cols, thick, col_dark))
    return [] if len(out) > limits.OCR_RULES_MAX else out


@dataclass
class Unread:
    """The ink of a page no word box or rule covers. `share`: the dark pixels left over all dark pixels (0.0 with
    none). `box`: the fractional box (x0, y0, x1, y1), y down, that bounds the pixels left that are not paper (darker
    than the paper by OCR_PAPER: light pictures too), None with none left after the 1 % rule of `unread`. `area`: the
    box's area over the page's (0.0 with no box). `density`: those pixels inside the box over the box's pixels (0.0
    with no box). `spread`: the page's gray range, the levels between its darkest and lightest 0.5 %."""
    share: float
    box: tuple[float, float, float, float] | None
    area: float
    density: float = 0.0
    spread: int = 0


def _clear(m: bytearray, w: int, h: int, x0, y0, x1, y1) -> None:
    """Zero the pixels of the box x0..x1, y0..y1 (floats allowed; widened to whole pixels, clipped to the page), row
    by row or column by column, whichever is fewer slices."""
    try:
        x0, y0 = max(math.floor(x0), 0), max(math.floor(y0), 0)
        x1, y1 = min(math.ceil(x1), w), min(math.ceil(y1), h)
    except (ValueError, OverflowError, TypeError):  # NaN, infinity, not a number
        return
    if x1 <= x0 or y1 <= y0:
        return
    if x1 - x0 >= y1 - y0:
        zero = bytes(x1 - x0)
        for r in range(y0, y1):
            m[r * w + x0:r * w + x1] = zero
    else:
        zero = bytes(y1 - y0)
        for c in range(x0, x1):
            m[y0 * w + c:(y1 - 1) * w + c + 1:w] = zero


def _or(parts) -> bytes:
    """The byte-wise OR of equal-length byte strings, as big integers."""
    acc, n = 0, 0
    for p in parts:
        acc |= int.from_bytes(p, "big")
        n = len(p)
    return acc.to_bytes(n, "big")


def _ink_box(m: bytearray, w: int, h: int) -> tuple[int, int, int, int] | None:
    """The pixel box (x0, y0, x1, y1), exclusive ends, of the ones in the 0/1 page `m`, on a grid of 4 x 4 pixel
    cells. A cell is marked when any of its 16 full-resolution pixels is one; a grid row is kept when at least 1 % of
    the grid's width of its cells are marked, a grid column when at least 1 % of the grid's height are. None when no
    row or no column is kept."""
    gw, gh = -(-w // 4), -(-h // 4)
    wp, hp = 4 * gw, 4 * gh
    if wp == w:
        flat = bytes(m)
    else:
        mv, pad = memoryview(m), bytes(wp - w)
        flat = b"".join(b for r in range(h) for b in (mv[r * w:(r + 1) * w], pad))
    flat += bytes(wp * (hp - h))
    across = _or(flat[k::4] for k in range(4))  # gw cells a row, hp rows
    grid = _or(b"".join(across[(4 * i + k) * gw:(4 * i + k + 1) * gw] for i in range(gh)) for k in range(4))
    rows = [i for i in range(gh) if grid[i * gw:(i + 1) * gw].count(1) * 100 >= gw]
    cols = [c for c in range(gw) if grid[c::gw].count(1) * 100 >= gh]
    if not rows or not cols:
        return None
    return cols[0] * 4, rows[0] * 4, min(cols[-1] * 4 + 4, w), min(rows[-1] * 4 + 4, h)


def _levels(g: Gray, thr: int) -> tuple[int, float]:
    """(spread, paper) over every 4th pixel: the levels between the darkest and the lightest 0.5 % of them, and the
    mean of those at or over `thr` (of all of them when none is)."""
    hist = sorted(Counter(g.data[::4]).items())
    n = sum(c for _, c in hist)
    cut = n // 200

    def edge(items):
        seen = 0
        for v, c in items:
            seen += c
            if seen > cut:
                return v
        return items[-1][0]
    paper = [(v, c) for v, c in hist if v >= thr] or hist
    return edge(hist[::-1]) - edge(hist), sum(v * c for v, c in paper) / sum(c for _, c in paper)


def unread(g: Gray, word_boxes, rule_boxes, thr: int) -> Unread:
    """The page's ink that no word box and no rule covers (decision 10: rule pixels are not unread). `word_boxes` are
    pixel boxes (x, y, w, h), y down, as the `ocr.Word` fields give them, each grown by a quarter of its height on
    every side; `rule_boxes` are `rules`' boxes (x0, y0, x1, y1), exclusive ends, each grown by 1 px on every side for
    the rule's anti-aliased edges. `share` counts the dark pixels (value < thr) left over all dark pixels, rules
    included. The box and its density count the pixels left that are not paper: dark, or darker than the mean of the
    pixels at or over `thr` by OCR_PAPER, so a picture lighter than the Otsu split still has its box."""
    w, h = g.w, g.h
    spread, paper = _levels(g, thr)
    m = bytearray(g.data.translate(_dark(thr)))
    p = bytearray(g.data.translate(_dark(max(thr, math.ceil(paper - limits.OCR_PAPER)))))
    total = m.count(1)
    for x, y, bw, bh in word_boxes:
        grow = bh / 4
        for mask in (m, p):
            _clear(mask, w, h, x - grow, y - grow, x + bw + grow, y + bh + grow)
    for x0, y0, x1, y1 in rule_boxes:
        for mask in (m, p):
            _clear(mask, w, h, x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    share = m.count(1) / total if total else 0.0
    px = _ink_box(p, w, h) if 1 in p else None
    if px is None:
        return Unread(share, None, 0.0, 0.0, spread)
    x0, y0, x1, y1 = px
    inside = sum(p.count(1, r * w + x0, r * w + x1) for r in range(y0, y1))
    box = (x0 / w, y0 / h, x1 / w, y1 / h)
    return Unread(share, box, (box[2] - box[0]) * (box[3] - box[1]), inside / ((x1 - x0) * (y1 - y0)), spread)
