"""The pixels OCR reads: Gray8 bitmaps the reader decoded itself.

Every operation here works on whole rows or columns as byte slices, or on rows as big integers, so no Python loop
runs per pixel.
"""
from dataclasses import dataclass


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
