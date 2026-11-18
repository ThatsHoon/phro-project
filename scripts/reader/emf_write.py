"""A plain EMF from a drawing list, so GDI+ can draw a PICT or WPG the way it draws an EMF.

Only the plain EMF records are written: paths with pens and brushes (runs of lines as one POLYLINETO, runs of curves
as one POLYBEZIERTO), EXTTEXTOUTW at the baseline, STRETCHDIBITS for bitmaps, and clips. One logical unit is one
device pixel is 0.01 mm, the unit of rclFrame: the reference device is 1000 x 1000 pixels over 10 x 10 mm. Every
coordinate is clamped to the 32-bit range before it is written.
"""
import math
import struct
import time

from . import images, limits
from .limits import ReadError
from .vector import Bitmap, Clip, Path, Run

_I32_MIN, _I32_MAX = -2**31, 2**31 - 1

# record types (MS-EMF 2.1.1)
_HEADER, _EOF = 1, 14
_POLYBEZIERTO, _POLYLINETO = 5, 6
_SETBKMODE, _SETPOLYFILLMODE, _SETTEXTALIGN, _SETTEXTCOLOR = 18, 19, 22, 24
_MOVETOEX, _INTERSECTCLIPRECT, _SELECTOBJECT, _CREATEBRUSHINDIRECT, _DELETEOBJECT = 27, 30, 37, 39, 40
_BEGINPATH, _ENDPATH, _CLOSEFIGURE, _FILLPATH, _STROKEANDFILLPATH, _STROKEPATH = 59, 60, 61, 62, 63, 64
_SELECTCLIPPATH, _EXTSELECTCLIPRGN, _STRETCHDIBITS, _EXTCREATEFONTINDIRECTW, _EXTTEXTOUTW = 67, 75, 81, 82, 84
_EXTCREATEPEN = 95

_PEN, _BRUSH, _FONT = 1, 2, 3                                      # the object slots; 0 is the metafile itself
_NULL_BRUSH, _NULL_PEN, _SYSTEM_FONT = 0x80000005, 0x80000008, 0x8000000D
_TRANSPARENT, _TA_BASELINE, _RGN_COPY, _ALTERNATE, _WINDING = 1, 24, 5, 1, 2
_PS_GEOMETRIC, _ETO_GLYPH_INDEX, _SRCCOPY = 0x10000, 0x10, 0x00CC0020
_UNIT = 2540.0  # logical units (0.01 mm) per inch
CLOCK_EVERY = 4096  # items and segments between two reads of the clock


def _i(v) -> int:
    """A finite value rounded and clamped to the 32-bit range; anything else is 0."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(v):
        return 0
    return int(max(_I32_MIN, min(_I32_MAX, round(v))))


def _colorref(rgb) -> int:
    rgb = int(rgb) & 0xFFFFFF
    return (rgb >> 16) | (rgb & 0xFF00) | (rgb & 0xFF) << 16


def _rec(typ: int, body: bytes = b"") -> bytes:
    body += b"\0" * (-len(body) % 4)
    return struct.pack("<II", typ, 8 + len(body)) + body


class _Writer:
    def __init__(self, d, decode, deadline=None):
        self.decode = decode
        self.deadline = deadline  # a time.monotonic() value, or None
        self.work = 0
        dpi = float(d.dpi) if isinstance(d.dpi, (int, float)) and math.isfinite(d.dpi) and d.dpi > 0 else 96.0
        fr = [float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0 for v in d.frame]
        self.x0, self.y0 = min(fr[0], fr[2]), min(fr[1], fr[3])
        self.s = _UNIT / dpi
        self.w = self._extent(abs(fr[2] - fr[0]), dpi)
        self.h = self._extent(abs(fr[3] - fr[1]), dpi)
        self.out: list[bytes] = []
        self.pen = self.brush = self.font = self.color = self.fill = None
        self.failed: set = set()  # bitmaps that did not decode, so a repeat is not decoded again
        self.bitmap_bytes = 0     # DIB bytes written; at IMAGE_BYTES no further bitmap is decoded

    def tick(self):
        self.work += 1
        if self.deadline is not None and self.work % CLOCK_EVERY == 0 and time.monotonic() > self.deadline:
            raise ReadError("too_large", "figure time")

    def _extent(self, span: float, dpi: float) -> int:
        """The frame side in 0.01 mm, nudged so that it gives back the drawing's own pixel size."""
        px = max(1, round(span * 96.0 / dpi))
        v = max(1, _i(span * self.s))
        for c in (v, v - 1, v + 1):
            if c >= 1 and max(1, round(c * 96.0 / _UNIT)) == px:
                return c
        return v

    def x(self, v) -> int:
        return _i((v - self.x0) * self.s)

    def y(self, v) -> int:
        return _i((v - self.y0) * self.s)

    def add(self, typ, body=b""):
        self.out.append(_rec(typ, body))

    # ---- objects -------------------------------------------------------------------------------------------------

    def use_pen(self, pen):
        w = pen.width if isinstance(pen.width, (int, float)) and math.isfinite(pen.width) else 0.0
        key = (int(pen.color) & 0xFFFFFF, max(1, _i(w * self.s)) if w > 0 else 0)
        if key == self.pen:
            return
        if self.pen is not None:
            self.add(_SELECTOBJECT, struct.pack("<I", _NULL_PEN))
            self.add(_DELETEOBJECT, struct.pack("<I", _PEN))
        color, width = key
        style = _PS_GEOMETRIC if width else 0  # a zero width is a cosmetic pen: one pixel
        self.add(_EXTCREATEPEN, struct.pack("<5I5I", _PEN, 0, 0, 0, 0, style, max(width, 1), 0, _colorref(color), 0)
                 + struct.pack("<I", 0))
        self.add(_SELECTOBJECT, struct.pack("<I", _PEN))
        self.pen = key

    def use_brush(self, brush):
        key = int(brush.color) & 0xFFFFFF
        if key == self.brush:
            return
        if self.brush is not None:
            self.add(_SELECTOBJECT, struct.pack("<I", _NULL_BRUSH))
            self.add(_DELETEOBJECT, struct.pack("<I", _BRUSH))
        self.add(_CREATEBRUSHINDIRECT, struct.pack("<4I", _BRUSH, 0, _colorref(key), 0))
        self.add(_SELECTOBJECT, struct.pack("<I", _BRUSH))
        self.brush = key

    def use_font(self, r):
        h = r.height if isinstance(r.height, (int, float)) and math.isfinite(r.height) else 0.0
        a = r.angle if isinstance(r.angle, (int, float)) and math.isfinite(r.angle) else 0.0
        face = "".join(c for c in str(r.face or "") if c != "\0")[:31]
        key = (face, -max(1, _i(abs(h) * self.s)), _i(a * 10) % 3600, max(0, min(1000, _i(r.weight))), bool(r.italic))
        if key == self.font:
            return
        if self.font is not None:
            self.add(_SELECTOBJECT, struct.pack("<I", _SYSTEM_FONT))
            self.add(_DELETEOBJECT, struct.pack("<I", _FONT))
        face, height, esc, weight, italic = key
        name = face.encode("utf-16-le", "replace")[:62].ljust(64, b"\0")
        # LogFont: height, width, escapement, orientation, weight, italic, underline, strikeout, charset 1
        # (DEFAULT_CHARSET), out and clip precision, quality 4 (ANTIALIASED), pitch and family, the face
        lf = struct.pack("<5i8B", height, 0, esc, esc, weight, int(italic), 0, 0, 1, 0, 0, 4, 0) + name
        self.add(_EXTCREATEFONTINDIRECTW, struct.pack("<I", _FONT) + lf)
        self.add(_SELECTOBJECT, struct.pack("<I", _FONT))
        self.font = key

    # ---- items ---------------------------------------------------------------------------------------------------

    def segs(self, segs) -> list[bytes]:
        """The path's segments as MOVETOEX, POLYLINETO, POLYBEZIERTO and CLOSEFIGURE records. A run of lines or of
        curves is one record. A close before any point, and a segment that is not one, write nothing."""
        out: list[bytes] = []
        started, i = False, 0
        segs = list(segs or ())
        n = len(segs)
        while i < n:
            s = segs[i]
            op = s[0] if s else None
            want = 3 if op == "L" else 7 if op == "C" else 0
            if want and len(s) >= want:
                pts = []
                while i < n and segs[i] and segs[i][0] == op and len(segs[i]) >= want:
                    self.tick()
                    c = segs[i]
                    pts += [(self.x(c[k]), self.y(c[k + 1])) for k in range(1, want, 2)]
                    i += 1
                if not started:
                    out.append(_rec(_MOVETOEX, struct.pack("<ii", *pts[0])))
                    started = True
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                out.append(_rec(_POLYLINETO if op == "L" else _POLYBEZIERTO,
                                struct.pack("<4iI", min(xs), min(ys), max(xs), max(ys), len(pts))
                                + struct.pack("<%di" % (2 * len(pts)), *[v for p in pts for v in p])))
                continue
            self.tick()
            if op == "M" and len(s) >= 3:
                out.append(_rec(_MOVETOEX, struct.pack("<ii", self.x(s[1]), self.y(s[2]))))
                started = True
            elif op == "Z" and started:
                out.append(_rec(_CLOSEFIGURE))
            i += 1
        return out

    def path(self, p):
        pen, brush = p.pen, p.brush
        if pen is None and brush is None:
            return
        recs = self.segs(p.segs)
        if not recs:
            return
        if pen is not None:
            self.use_pen(pen)
        if brush is not None:
            self.use_brush(brush)
            mode = _ALTERNATE if p.even_odd else _WINDING
            if mode != self.fill:
                self.add(_SETPOLYFILLMODE, struct.pack("<I", mode))
                self.fill = mode
        self.add(_BEGINPATH)
        self.out += recs
        self.add(_ENDPATH)
        bounds = struct.pack("<4i", 0, 0, self.w, self.h)
        self.add(_STROKEANDFILLPATH if pen is not None and brush is not None else _FILLPATH if brush is not None
                 else _STROKEPATH, bounds)

    def run(self, r):
        glyphs = r.glyphs if not r.text and r.glyphs else None
        if glyphs:
            raw = struct.pack("<%dH" % len(glyphs), *[int(g) & 0xFFFF for g in glyphs])
            n, opts = len(glyphs), _ETO_GLYPH_INDEX
        else:
            text = str(r.text or "")
            try:
                raw = text.encode("utf-16-le", "surrogatepass")
            except UnicodeEncodeError:
                raw = text.encode("utf-16-le", "replace")
            n, opts = len(raw) // 2, 0
        if not n:
            return
        self.use_font(r)
        color = int(r.color) & 0xFFFFFF if isinstance(r.color, int) else 0
        if color != self.color:
            self.add(_SETTEXTCOLOR, struct.pack("<I", _colorref(color)))
            self.color = color
        # bounds, graphics mode 1 (GM_COMPATIBLE), the scales; the EmrText: reference point, count, the string's
        # offset (76, right after it), options, an empty rectangle, no dx; then the string
        body = struct.pack("<4iIff", 0, 0, 0, 0, 1, 0.0, 0.0)
        body += struct.pack("<iiIII4iI", self.x(r.x), self.y(r.y), n, 76, opts, 0, 0, 0, 0, 0) + raw
        self.add(_EXTTEXTOUTW, body)

    def clip(self, c):
        self.add(_EXTSELECTCLIPRGN, struct.pack("<II", 0, _RGN_COPY))  # no region, RGN_COPY: the clip is removed
        if c.segs is None:
            return
        box = self._rect(c.segs)
        if box is not None:
            self.add(_INTERSECTCLIPRECT, struct.pack("<4i", *box))
            return
        recs = self.segs(c.segs)
        if not recs:
            self.add(_INTERSECTCLIPRECT, struct.pack("<4i", 0, 0, 0, 0))  # an empty clip shows nothing
            return
        self.add(_BEGINPATH)
        self.out += recs
        self.add(_ENDPATH)
        self.add(_SELECTCLIPPATH, struct.pack("<I", _RGN_COPY))

    def _rect(self, segs):
        """The logical box of an axis-aligned rectangle path, else None; an empty path gives an empty box."""
        if not segs:
            return (0, 0, 0, 0)
        if any(not s or s[0] not in "MLZ" for s in segs) or segs[0][0] != "M":
            return None
        pts = [(self.x(s[1]), self.y(s[2])) for s in segs if s[0] in "ML" and len(s) >= 3]
        if len(pts) == 5 and pts[4] == pts[0]:
            pts = pts[:4]
        if len(pts) != 4:
            return None
        for a, b in zip(pts, pts[1:] + pts[:1]):
            if a[0] != b[0] and a[1] != b[1]:
                return None
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if len(set(xs)) > 2 or len(set(ys)) > 2:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def bitmap(self, b):
        """STRETCHDIBITS of a decoded PNG or JPEG. Once IMAGE_BYTES of DIBs are written, nothing more is decoded;
        a DIB is not kept after its record is written."""
        if self.decode is None or b.kind not in ("png", "jpg") or self.bitmap_bytes >= limits.IMAGE_BYTES:
            return
        box = list(b.box) if len(b.box) == 4 else [0, 0, 0, 0]
        x0, y0, x1, y1 = self.x(box[0]), self.y(box[1]), self.x(box[2]), self.y(box[3])
        cx, cy = _i(x1 - x0), _i(y1 - y0)
        key = (b.kind, bytes(b.data))
        if not cx or not cy or key in self.failed:
            return
        try:
            got = self.decode(key[1], b.kind)
        except Exception:  # noqa: BLE001 - a bitmap that does not decode is left out
            got = None
        if got is not None:
            w, h, bits = got
            size = 4 * w * h if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0 else -1
            if size <= 0 or w * h > images._MAX_PIXELS or len(bits) != size:
                got = None
            elif self.bitmap_bytes + size > limits.IMAGE_BYTES:
                self.bitmap_bytes = limits.IMAGE_BYTES  # the budget is spent: this and the rest are left out
                return
        if got is None:
            self.failed.add(key)
            return
        self.bitmap_bytes += size
        # BITMAPINFOHEADER: 32 bits, BI_RGB, a negative height for top-down rows
        bmi = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(bits), 0, 0, 0, 0)
        body = struct.pack("<4i6i6I2i", min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), x0, y0, 0, 0, w, h,
                           80, len(bmi), 80 + len(bmi), len(bits), 0, _SRCCOPY, cx, cy)
        self.add(_STRETCHDIBITS, body + bmi + bits)


def write(d, decode=None, deadline: float | None = None) -> bytes:
    """The EMF of a drawing. `decode(data, kind)` turns a PNG or JPEG into (width, height, BGRA rows top-down) or
    None; without one, or when it fails, the bitmap is left out. Past `deadline` (a time.monotonic() value, read
    every CLOCK_EVERY items and segments) it raises ReadError("too_large", "figure time")."""
    wr = _Writer(d, decode, deadline)
    wr.add(_SETBKMODE, struct.pack("<I", _TRANSPARENT))
    wr.add(_SETTEXTALIGN, struct.pack("<I", _TA_BASELINE))
    for it in d.items:
        wr.tick()
        if isinstance(it, Path):
            wr.path(it)
        elif isinstance(it, Run):
            wr.run(it)
        elif isinstance(it, Clip):
            wr.clip(it)
        elif isinstance(it, Bitmap):
            wr.bitmap(it)
    eof = _rec(_EOF, struct.pack("<III", 0, 16, 20))
    recs = b"".join(wr.out) + eof
    n = len(wr.out) + 2
    # EMR_HEADER: bounds (inclusive device pixels), frame (0.01 mm), " EMF", version, bytes, records, handles,
    # reserved, no description or palette, the reference device (1000 x 1000 pixels over 10 x 10 mm), no pixel
    # format, no OpenGL, and the device in micrometers
    w, h = wr.w, wr.h
    head = struct.pack("<II4i4iIIIIHHIII2i2iIII2i", _HEADER, 108, 0, 0, w - 1, h - 1, 0, 0, w, h, 0x464D4520,
                       0x10000, 108 + len(recs), n, 4, 0, 0, 0, 0, 1000, 1000, 10, 10, 0, 0, 0, 10000, 10000)
    return head + recs
