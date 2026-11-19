"""The pixels OCR reads: Gray8 bitmaps the reader decoded itself.

Every operation here works on whole rows or columns as byte slices, or on rows as big integers, so no Python loop
runs per pixel.
"""
import math
import re
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
    import math
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
    span of cut points ties (a histogram gap between paper and ink), the middle of the span is taken. 128 for a page
    of one value."""
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
    return 128 if first is None else (first + last) // 2 + 1


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


def _merge(lines: list[list[tuple[int, int]]], thick: int) -> list[tuple[int, int, int, int]]:
    """Runs on adjacent lines merged when they overlap by at least 90 % of the longer one (each run is compared with
    the runs of the line before only), as (start, end, first line, last line + 1): the merged groups at most `thick`
    lines thick."""
    done = []
    prev = []  # the line before: (start, end, group) for each run, a group being [lo, hi, first line, last line]
    for li, runs in enumerate(lines):
        cur, j = [], 0
        for s, e in runs:
            while j < len(prev) and prev[j][1] <= s:  # both lines' runs are sorted and disjoint
                j += 1
            grp = None
            for ps, pe, p in prev[j:j + 2]:
                ov = min(e, pe) - max(s, ps)
                if ov > 0 and ov * 10 >= 9 * max(e - s, pe - ps) and p[3] != li:
                    grp = p
                    break
            if grp is None:
                grp = [s, e, li, li]
            else:
                grp[0], grp[1], grp[3] = min(grp[0], s), max(grp[1], e), li
            cur.append((s, e, grp))
        done.extend(p for _, _, p in prev if p[3] != li)
        prev = cur
    done.extend(p for _, _, p in prev)
    return [(p[0], p[1], p[2], p[3] + 1) for p in done if p[3] + 1 - p[2] <= thick]


def rules(g: Gray, dpi: float, thr: int) -> list[tuple[int, int, int, int]]:
    """The ruled-table rules of a page as pixel boxes (x0, y0, x1, y1), y down, exclusive ends: horizontal rules
    first, then vertical ones. A rule is a dark run at least `OCR_RULE` of the page side long (rows: the width;
    columns: the height); runs on adjacent rows (columns) that overlap by 90 % merge, and a merged run thicker than
    1/100 inch is a bar, not a rule."""
    w, h = g.w, g.h
    m = g.data.translate(_dark(thr))
    thick = max(1, round(dpi / 100))
    rows = _runs((m[r * w:(r + 1) * w] for r in range(h)), max(1, math.ceil(limits.OCR_RULE * w)))
    cols = _runs((m[c::w] for c in range(w)), max(1, math.ceil(limits.OCR_RULE * h)))
    out = [(x0, y0, x1, y1) for x0, x1, y0, y1 in _merge(rows, thick)]
    out.sort(key=lambda b: (b[1], b[0]))
    vert = [(x0, y0, x1, y1) for y0, y1, x0, x1 in _merge(cols, thick)]
    vert.sort()
    return out + vert


@dataclass
class Unread:
    """The dark pixels of a page no word box or rule covers. `share`: their count over all dark pixels (0.0 with
    none). `box`: the fractional box (x0, y0, x1, y1), y down, that bounds them, None with none left after the 1 %
    rule of `unread`. `area`: the box's area over the page's (0.0 with no box)."""
    share: float
    box: tuple[float, float, float, float] | None
    area: float


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


def _ink_box(m: bytearray, w: int, h: int) -> tuple[float, float, float, float] | None:
    """The fractional box of the ones in the 0/1 page `m`, on a grid of 4 x 4 pixel cells. A cell is marked when any
    of its 16 full-resolution pixels is one; a grid row is kept when at least 1 % of the grid's width of its cells are
    marked, a grid column when at least 1 % of the grid's height are. None when no row or no column is kept."""
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
    return (cols[0] * 4 / w, rows[0] * 4 / h, min(cols[-1] * 4 + 4, w) / w, min(rows[-1] * 4 + 4, h) / h)


def unread(g: Gray, word_boxes, rule_boxes, thr: int) -> Unread:
    """The page's dark pixels (value < thr) that no word box and no rule covers (decision 10: rule pixels are not
    unread). `word_boxes` are pixel boxes (x, y, w, h), y down, as the `ocr.Word` fields give them, each grown by a
    quarter of its height on every side; `rule_boxes` are `rules`' boxes (x0, y0, x1, y1), exclusive ends, each grown
    by 1 px on every side for the rule's anti-aliased edges. `share` counts over all dark pixels, rules included."""
    w, h = g.w, g.h
    m = bytearray(g.data.translate(_dark(thr)))
    total = m.count(1)
    if not total:
        return Unread(0.0, None, 0.0)
    for x, y, bw, bh in word_boxes:
        grow = bh / 4
        _clear(m, w, h, x - grow, y - grow, x + bw + grow, y + bh + grow)
    for x0, y0, x1, y1 in rule_boxes:
        _clear(m, w, h, x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    left = m.count(1)
    if not left:
        return Unread(0.0, None, 0.0)
    box = _ink_box(m, w, h)
    area = 0.0 if box is None else (box[2] - box[0]) * (box[3] - box[1])
    return Unread(left / total, box, area)
