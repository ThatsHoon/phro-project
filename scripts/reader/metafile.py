"""Metafile parsers for figures: WMF now, EMF and EMF+ beside it (Task 3).

`parse` dispatches on the magic and returns a vector.Drawing in the picture's frame units, y growing downward. The
WMF half follows MS-WMF: the placeable and plain headers, the record subset office pictures use, and a DC state
(`_DC`) the EMF half shares. Every length, count and index read from a record is checked against the record first; a
record that runs past the end stops the parse and marks the drawing damaged.
"""
import heapq
import math
import struct
from dataclasses import dataclass, replace

from . import limits
from .images import bmp_to_png
from .limits import ReadError
from .officeart import dib_to_bmp
from .text import SYMBOL
from .vector import Bitmap, Brush, Budget, Clip, Drawing, Path, Pen, Run, arc_beziers, run_width

# font charset -> the code page ANSI text in that charset is decoded with
CHARSETS: dict[int, str] = {
    0: "cp1252", 128: "cp932", 129: "cp949", 130: "cp1361", 134: "cp936", 136: "cp950", 161: "cp1253",
    162: "cp1254", 163: "cp1258", 177: "cp1255", 178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874",
    238: "cp1250",
}
SYMBOL_CHARSET = 2

_TA_UPDATECP, _TA_RIGHT, _TA_CENTER, _TA_BOTTOM, _TA_BASELINE = 1, 2, 6, 8, 24
_ETO_GLYPH_INDEX, _ETO_PDY = 0x10, 0x2000
_ASCENT = 0.8  # of the em: where a top-aligned run's baseline lies below the reference point
_DEFAULT_EM_PT = 12.0


def decode_ansi(raw: bytes, charset: int, face: str, codepage: str = "") -> str | None:
    """8-bit text of a font as str. A font named Symbol maps through the Adobe Symbol table in any charset; other
    SYMBOL_CHARSET fonts have no mapping and give None. Charset 1 and the charsets the table lacks use `codepage`."""
    if face.strip().lower() == "symbol":
        return "".join(SYMBOL.get(b, "") for b in raw)
    if charset == SYMBOL_CHARSET:
        return None
    enc = CHARSETS.get(charset) or codepage or "cp1252"
    try:
        s = raw.decode(enc, "replace")
    except LookupError:
        s = raw.decode("cp1252", "replace")
    return s.replace("�", "").replace("\x00", "")


@dataclass(frozen=True)
class _Pen:
    color: int
    width: float  # logical units; 0 is one device unit
    null: bool = False


@dataclass(frozen=True)
class _Brush:
    color: int | None  # None: hollow


@dataclass(frozen=True)
class _Font:
    height: int
    escapement: int
    weight: int
    italic: bool
    charset: int
    face: str


@dataclass
class _DC:
    """The device context state both metafile formats keep; SaveDC and RestoreDC copy it."""
    pen: _Pen = _Pen(0, 0.0)
    brush: _Brush = _Brush(None)
    font: _Font | None = None
    align: int = 0
    color: int = 0
    even_odd: bool = True  # ALTERNATE fill mode
    cp: tuple = (0, 0)
    mapmode: int = 1
    wo: tuple = (0, 0)
    we: tuple | None = None   # window extent, None until set
    vo: tuple = (0, 0)
    ve: tuple | None = None   # viewport extent, None until set
    clip: tuple | None = None  # (x0, y0, x1, y1) in frame units

    def copy(self) -> "_DC":
        return replace(self)


# map mode -> logical units per inch (the fixed modes; y grows up in them)
_FIXED = {2: 254.0, 3: 2540.0, 4: 100.0, 5: 1000.0, 6: 1440.0}


def _ok(*vals) -> bool:
    return all(map(math.isfinite, vals))


def _rect_segs(x0, y0, x1, y1) -> list:
    return [("M", x0, y0), ("L", x1, y0), ("L", x1, y1), ("L", x0, y1), ("Z",)]


def _rgb(b: bytes) -> int:
    return b[0] << 16 | b[1] << 8 | b[2]


# WMF function numbers (MS-WMF 2.1.1.1)
_EOF, _SAVEDC, _RESTOREDC = 0x0000, 0x001E, 0x0127
_SETMAPMODE, _SETPOLYFILLMODE, _SETTEXTCOLOR, _SETTEXTALIGN = 0x0103, 0x0106, 0x0209, 0x012E
_SETWINDOWORG, _SETWINDOWEXT, _SETVIEWPORTORG, _SETVIEWPORTEXT = 0x020B, 0x020C, 0x020D, 0x020E
_OFFSETWINDOWORG, _SCALEWINDOWEXT, _OFFSETVIEWPORTORG, _SCALEVIEWPORTEXT = 0x020F, 0x0410, 0x0211, 0x0412
_LINETO, _MOVETO, _EXCLUDECLIPRECT, _INTERSECTCLIPRECT = 0x0213, 0x0214, 0x0415, 0x0416
_ARC, _ELLIPSE, _PIE, _RECTANGLE, _ROUNDRECT, _CHORD = 0x0817, 0x0418, 0x081A, 0x041B, 0x061C, 0x0830
_TEXTOUT, _EXTTEXTOUT, _POLYGON, _POLYLINE, _POLYPOLYGON = 0x0521, 0x0A32, 0x0324, 0x0325, 0x0538
_SELECTCLIPREGION, _SELECTOBJECT, _DELETEOBJECT = 0x012C, 0x012D, 0x01F0
_CREATEPALETTE, _CREATEPATTERNBRUSH, _DIBCREATEPATTERNBRUSH = 0x00F7, 0x01F9, 0x0142
_CREATEPEN, _CREATEFONT, _CREATEBRUSH, _CREATEREGION = 0x02FA, 0x02FB, 0x02FC, 0x06FF
_DIBBITBLT, _DIBSTRETCHBLT, _STRETCHDIB, _SETDIBTODEV = 0x0940, 0x0B41, 0x0F43, 0x0D33
# records that change nothing the figure shows, so they are not counted as skipped
_QUIET = {0x0102, 0x0104, 0x0105, 0x0107, 0x0108, 0x0201, 0x020A, 0x0231, 0x0626, 0x0234, 0x0035, 0x0436, 0x0037,
          0x0139, 0x0220}
_PATCOPY = 0x00F00021
_SRC_ROPS = {0x00CC0020, 0x00EE0086, 0x008800C6}  # SRCCOPY, SRCPAINT, SRCAND


def _is_plain_wmf(d: bytes) -> bool:
    if len(d) < 18:
        return False
    typ, size, ver = struct.unpack_from("<HHH", d)
    return typ in (1, 2) and size == 9 and ver in (0x0100, 0x0300)


def _is_emf(d: bytes) -> bool:
    return len(d) >= 44 and struct.unpack_from("<I", d)[0] == 1 and d[40:44] == b" EMF"


def parse(data: bytes, codepage: str = "") -> Drawing:
    """The drawing of a WMF or EMF. ReadError("damaged", "not a metafile") when the bytes are neither."""
    if data[:4] == b"\xd7\xcd\xc6\x9a" or _is_plain_wmf(data):
        return _Wmf(data, codepage).run()
    if _is_emf(data):
        return _parse_emf(data, codepage)
    raise ReadError("damaged", "not a metafile")


def _parse_emf(data: bytes, codepage: str) -> Drawing:
    raise ReadError("unsupported", "emf")  # Task 3


class _Wmf:
    def __init__(self, data: bytes, codepage: str):
        self.data = data
        self.codepage = codepage
        self.budget = Budget()
        self.items: list = []
        self.skipped = 0
        self.damaged = False
        self.dc = _DC()
        self.stack: list[_DC] = []
        self.objs: list = []
        self.free: list[int] = []  # freed slots, lowest first
        self.dpi = 96.0
        self.frame: tuple | None = None
        self._frozen = False
        self._placeable = False  # a placeable box fixes the frame: the viewport records are ignored
        self._clip_out = None
        self.pos = 0
        if data[:4] == b"\xd7\xcd\xc6\x9a":
            if len(data) < 22 + 18 or not _is_plain_wmf(data[22:]):
                raise ReadError("damaged", "not a metafile")
            left, top, right, bottom, inch = struct.unpack_from("<hhhhH", data, 6)
            x0, x1, y0, y1 = min(left, right), max(left, right), min(top, bottom), max(top, bottom)
            if inch > 0:
                self.dpi = float(inch)
            if x1 > x0 and y1 > y0:
                self.frame = (float(x0), float(y0), float(x1), float(y1))
                self._frozen = True
                self._placeable = True
            self.pos = 22
        self.pos += 18

    # ---- the transform -------------------------------------------------------------------------------------------

    def _freeze(self):
        """The frame is fixed at the first drawing: the placeable box, else the window mapped through the DC."""
        if self._frozen:
            return
        self._frozen = True
        dc = self.dc
        if dc.we and dc.mapmode not in _FIXED and dc.we[0] and dc.we[1]:
            if dc.ve is not None:
                xs = [self._axis(dc.wo[0] + e, 0) for e in (0, dc.we[0])]
                ys = [self._axis(dc.wo[1] + e, 1) for e in (0, dc.we[1])]
            else:
                xs = [dc.wo[0], dc.wo[0] + dc.we[0]]
                ys = [dc.wo[1], dc.wo[1] + dc.we[1]]
            self.frame = (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))

    def _axis(self, v: float, a: int) -> float:
        dc = self.dc
        wo = dc.wo[a]
        if dc.mapmode in _FIXED:
            s = self.dpi / _FIXED[dc.mapmode]
            return (v - wo) * (-s if a else s) + dc.vo[a]
        we = dc.we[a] if dc.we else 0
        if we and dc.ve is not None:
            return (v - wo) * dc.ve[a] / we + dc.vo[a]
        if we and self.frame is not None:  # no viewport: the window maps onto the frame, flipped for a negative extent
            return self.frame[a] + (v - wo) / we * (self.frame[a + 2] - self.frame[a])
        return (v - wo) + dc.vo[a]

    def _xy(self, x, y) -> tuple[float, float]:
        self._freeze()
        return self._axis(x, 0), self._axis(y, 1)

    def _dist(self, v: float, a: int) -> float:
        """A length along one axis, in frame units."""
        self._freeze()
        wo = self.dc.wo[a]
        return abs(self._axis(wo + v, a) - self._axis(wo, a))

    # ---- the parse loop ------------------------------------------------------------------------------------------

    def run(self) -> Drawing:
        d, n = self.data, len(self.data)
        pos = self.pos
        while pos < n:
            if n - pos < 6:
                self.damaged = True
                break
            words, fn = struct.unpack_from("<IH", d, pos)
            size = words * 2
            if words < 3 or pos + size > n:
                self.damaged = True
                break
            self.budget.tick()
            if fn == _EOF:
                break
            h = _HANDLERS.get(fn)
            try:
                if h is not None:
                    h(self, d[pos + 6:pos + size])
                elif fn not in _QUIET:
                    self.skipped += 1
            except (struct.error, IndexError, ValueError):
                self.damaged = True
                self.skipped += 1
            pos += size
        self._freeze()
        frame = self.frame or self._extent()
        if not _ok(*frame):  # a zero width or height stays: size_px clamps each side to a pixel
            frame = (0.0, 0.0, 1.0, 1.0)
        return Drawing(frame, self.dpi, self.items, self.damaged, self.skipped)

    def _extent(self) -> tuple:
        xs, ys = [], []
        for it in self.items:
            if isinstance(it, Path):
                for s in it.segs:
                    xs += s[1::2]
                    ys += s[2::2]
            elif isinstance(it, Run):
                w = it.width if it.width is not None else run_width(it, it.text)
                xs += [it.x, it.x + w]
                ys += [it.y - it.height, it.y + 0.2 * it.height]
            elif isinstance(it, Bitmap):
                xs += [it.box[0], it.box[2]]
                ys += [it.box[1], it.box[3]]
        if not xs or not ys:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(xs), min(ys), max(xs), max(ys))

    # ---- objects -------------------------------------------------------------------------------------------------

    def _add(self, obj):
        if self.free:
            self.objs[heapq.heappop(self.free)] = obj
        elif len(self.objs) < limits.METAFILE_OBJECTS:
            self.objs.append(obj)
        else:
            self.skipped += 1

    def _create_pen(self, p):
        style, w = struct.unpack_from("<Hh", p)
        self._add(("pen", _Pen(_rgb(p[6:9]) if len(p) >= 10 else 0, abs(w), style & 0xF == 5)))

    def _create_brush(self, p):
        style = struct.unpack_from("<H", p)[0]
        color = _rgb(p[2:5]) if len(p) >= 6 else 0
        self._add(("brush", _Brush(color if style in (0, 2) else None)))

    def _create_font(self, p):
        if len(p) < 14:
            raise ValueError
        height, _w, esc, _o, weight = struct.unpack_from("<5h", p)
        italic, charset = p[10], p[13]
        face = bytes(p[18:50]).split(b"\0")[0]
        try:
            name = face.decode(CHARSETS.get(charset) or self.codepage or "cp1252", "replace")
        except LookupError:
            name = face.decode("cp1252", "replace")
        self._add(("font", _Font(height, esc, weight or 400, bool(italic), charset, name)))

    def _create_region(self, p):  # the Region object's bounding rectangle at 14 is all that is kept
        x0, y0, x1, y1 = struct.unpack_from("<4h", p, 14)
        self._add(("region", (x0, y0, x1, y1)))

    def _create_other(self, p):  # palette and pattern brush objects only hold their slot
        self._add(("other", None))

    def _select(self, p):
        i = struct.unpack_from("<H", p)[0]
        if i >= len(self.objs) or self.objs[i] is None:
            return
        kind, v = self.objs[i]
        if kind == "pen":
            self.dc.pen = v
        elif kind == "brush":
            self.dc.brush = v
        elif kind == "font":
            self.dc.font = v

    def _delete(self, p):
        i = struct.unpack_from("<H", p)[0]
        if i < len(self.objs) and self.objs[i] is not None:
            self.objs[i] = None
            heapq.heappush(self.free, i)

    # ---- state ---------------------------------------------------------------------------------------------------

    def _save(self, p):
        if len(self.stack) >= limits.MAX_DEPTH:
            self.skipped += 1
            return
        self.stack.append(self.dc.copy())

    def _restore(self, p):
        n = struct.unpack_from("<h", p)[0]
        i = len(self.stack) + n if n < 0 else n - 1
        if 0 <= i < len(self.stack):
            self.dc = self.stack[i]
            del self.stack[i:]
            self._sync_clip()

    def _sync_clip(self):
        if self.dc.clip != self._clip_out:
            self._clip_out = self.dc.clip
            self.items.append(Clip(None if self._clip_out is None else _rect_segs(*self._clip_out)))

    def _mapmode(self, p):
        self.dc.mapmode = struct.unpack_from("<H", p)[0]

    def _polyfill(self, p):
        self.dc.even_odd = struct.unpack_from("<H", p)[0] != 2

    def _textcolor(self, p):
        self.dc.color = _rgb(p[:3]) if len(p) >= 3 else 0

    def _textalign(self, p):
        self.dc.align = struct.unpack_from("<H", p)[0]

    def _pair(self, name, p):
        y, x = struct.unpack_from("<hh", p)
        setattr(self.dc, name, (x, y))

    def _window_org(self, p):
        self._pair("wo", p)

    def _window_ext(self, p):
        self._pair("we", p)

    def _viewport_org(self, p):
        if not self._placeable:
            self._pair("vo", p)

    def _viewport_ext(self, p):
        if not self._placeable:
            self._pair("ve", p)

    def _offset(self, name, p):
        if name == "vo" and self._placeable:
            return
        y, x = struct.unpack_from("<hh", p)
        ox, oy = getattr(self.dc, name)
        setattr(self.dc, name, (ox + x, oy + y))

    def _scale(self, name, p):
        if name == "ve" and self._placeable:
            return
        yd, yn, xd, xn = struct.unpack_from("<4h", p)
        cur = getattr(self.dc, name) or (1, 1)
        if xd and yd:
            setattr(self.dc, name, (cur[0] * xn / xd, cur[1] * yn / yd))

    def _box(self, p):
        b, r, t, l = struct.unpack_from("<4h", p)
        (x0, y0), (x1, y1) = self._xy(l, t), self._xy(r, b)
        return x0, y0, x1, y1

    def _intersect_clip(self, p):
        x0, y0, x1, y1 = self._box(p)
        nx0, nx1, ny0, ny1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        if self.dc.clip is not None:
            cx0, cy0, cx1, cy1 = self.dc.clip
            nx0, ny0, nx1, ny1 = max(nx0, cx0), max(ny0, cy0), min(nx1, cx1), min(ny1, cy1)
            nx1, ny1 = max(nx0, nx1), max(ny0, ny1)
        self.dc.clip = (nx0, ny0, nx1, ny1)
        self._sync_clip()

    def _select_clip_region(self, p):
        i = struct.unpack_from("<H", p)[0]
        if i < len(self.objs) and self.objs[i] is not None and self.objs[i][0] == "region":
            l, t, r, b = self.objs[i][1]
            (x0, y0), (x1, y1) = self._xy(l, t), self._xy(r, b)
            self.dc.clip = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            self._sync_clip()

    def _skip(self, p):
        self.skipped += 1

    # ---- drawing -------------------------------------------------------------------------------------------------

    def _pen(self):
        pen = self.dc.pen
        if pen.null:
            return None
        w = max(self._dist(pen.width, 0), self.dpi / 96.0)
        return Pen(pen.color, w if math.isfinite(w) else 1.0)

    def _put(self, segs, pen, fill, even_odd=False):
        """Adds a Path; a path with no pen and no brush shows nothing, one with a non-finite coordinate is dropped."""
        brush = Brush(self.dc.brush.color) if fill and self.dc.brush.color is not None else None
        pen = self._pen() if pen else None
        if pen is None and brush is None:
            return
        if not all(_ok(*s[1:]) for s in segs):
            self.skipped += 1
            return
        self.items.append(Path(segs, pen, brush, even_odd))

    def _moveto(self, p):
        y, x = struct.unpack_from("<hh", p)
        self.dc.cp = (x, y)

    def _lineto(self, p):
        y, x = struct.unpack_from("<hh", p)
        (ax, ay), (bx, by) = self._xy(*self.dc.cp), self._xy(x, y)
        self.dc.cp = (x, y)
        self.budget.points(2)
        self._put([("M", ax, ay), ("L", bx, by)], True, False)

    def _rect(self, p):
        x0, y0, x1, y1 = self._box(p)
        self.budget.points(4)
        self._put(_rect_segs(x0, y0, x1, y1), True, True)

    def _roundrect(self, p):
        eh, ew, b, r, t, l = struct.unpack_from("<6h", p)
        x0, y0, x1, y1 = self._box(struct.pack("<4h", b, r, t, l))
        rx = min(self._dist(ew, 0) / 2, abs(x1 - x0) / 2)
        ry = min(self._dist(eh, 1) / 2, abs(y1 - y0) / 2)
        sx, sy = (1 if x1 >= x0 else -1), (1 if y1 >= y0 else -1)
        k = 0.5523
        a, b2, c, d = x0, y0, x1, y1
        rx, ry = rx * sx, ry * sy
        segs = [("M", a + rx, b2),
                ("L", c - rx, b2), ("C", c - rx + k * rx, b2, c, b2 + ry - k * ry, c, b2 + ry),
                ("L", c, d - ry), ("C", c, d - ry + k * ry, c - rx + k * rx, d, c - rx, d),
                ("L", a + rx, d), ("C", a + rx - k * rx, d, a, d - ry + k * ry, a, d - ry),
                ("L", a, b2 + ry), ("C", a, b2 + ry - k * ry, a + rx - k * rx, b2, a + rx, b2), ("Z",)]
        self.budget.points(12)
        self._put(segs, True, True)

    def _ellipse(self, p):
        x0, y0, x1, y1 = self._box(p)
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
        self.budget.points(13)
        self._put([("M", cx + rx, cy)] + arc_beziers(cx, cy, rx, ry, 0, 360) + [("Z",)], True, True)

    def _arc(self, p, kind):
        """Angles and sweep are worked out in logical units, so a flipping transform mirrors the arc instead of
        reversing it; the curve's points are then mapped one by one."""
        ye, xe, ys, xs, b, r, t, l = struct.unpack_from("<8h", p)
        cx, cy, rx, ry = (l + r) / 2, (t + b) / 2, abs(r - l) / 2, abs(b - t) / 2
        if not rx or not ry:
            return
        a1 = math.degrees(math.atan2((ys - cy) / ry, (xs - cx) / rx))
        a2 = math.degrees(math.atan2((ye - cy) / ry, (xe - cx) / rx))
        sweep = -(((a1 - a2) % 360.0) or 360.0)  # counterclockwise with y down, the GDI default
        arcs = arc_beziers(cx, cy, rx, ry, a1, sweep)
        if not arcs:
            self.skipped += 1
            return
        a = math.radians(a1)
        segs = [("M", cx + rx * math.cos(a), cy + ry * math.sin(a))] + arcs
        if kind == "pie":
            segs += [("L", cx, cy), ("Z",)]
        elif kind == "chord":
            segs += [("Z",)]
        self.budget.points(len(segs) * 3)
        out = []
        for sg in segs:
            pts = [self._xy(sg[i], sg[i + 1]) for i in range(1, len(sg), 2)]
            out.append((sg[0],) + tuple(v for q in pts for v in q))
        self._put(out, True, kind != "arc")

    def _poly(self, p, closed):
        n = struct.unpack_from("<h", p)[0]
        if n < 0 or 2 + 4 * n > len(p):
            raise ValueError
        self.budget.points(n)
        pts = [self._xy(*struct.unpack_from("<hh", p, 2 + 4 * i)) for i in range(n)]
        if len(pts) < 2:
            return
        segs = [("M",) + pts[0]] + [("L",) + q for q in pts[1:]]
        if closed:
            self._put(segs + [("Z",)], True, True, self.dc.even_odd)
        else:
            self._put(segs, True, False)

    def _polypolygon(self, p):
        n = struct.unpack_from("<H", p)[0]
        if 2 + 2 * n > len(p):
            raise ValueError
        counts = struct.unpack_from("<%dH" % n, p, 2)
        total = sum(counts)
        if 2 + 2 * n + 4 * total > len(p):
            raise ValueError
        self.budget.points(total)
        off, segs = 2 + 2 * n, []
        for c in counts:
            pts = [self._xy(*struct.unpack_from("<hh", p, off + 4 * i)) for i in range(c)]
            off += 4 * c
            if len(pts) >= 2:
                segs += [("M",) + pts[0]] + [("L",) + q for q in pts[1:]] + [("Z",)]
        if segs:
            self._put(segs, True, True, self.dc.even_odd)

    def _polygon(self, p):
        self._poly(p, True)

    def _polyline(self, p):
        self._poly(p, False)

    def _arc_rec(self, p):
        self._arc(p, "arc")

    def _pie_rec(self, p):
        self._arc(p, "pie")

    def _chord_rec(self, p):
        self._arc(p, "chord")

    # ---- text ----------------------------------------------------------------------------------------------------

    def _textout(self, p):
        n = struct.unpack_from("<h", p)[0]
        if n < 0 or 2 + n + (n & 1) + 4 > len(p):
            raise ValueError
        y, x = struct.unpack_from("<hh", p, 2 + n + (n & 1))
        self._text(x, y, bytes(p[2:2 + n]), None, False)

    def _exttextout(self, p):
        y, x, n, opts = struct.unpack_from("<hhhH", p)
        glyph = bool(opts & _ETO_GLYPH_INDEX)
        off = 8 + (8 if opts & 6 else 0)
        nbytes = n * 2 if glyph else n
        per = 2 if opts & _ETO_PDY else 1  # ETO_PDY: an x and a y advance for each character
        if n < 0 or off + nbytes > len(p):
            raise ValueError
        raw = bytes(p[off:off + nbytes])
        off += nbytes + (nbytes & 1)
        dx = None
        if n and off + 2 * n * per <= len(p):
            dx = struct.unpack_from("<%dh" % (n * per), p, off)[::per]
        self._text(x, y, raw, dx, glyph)

    def _text(self, x, y, raw: bytes, dx, glyph: bool):
        dc, f = self.dc, self.dc.font
        if dc.align & _TA_UPDATECP:
            x, y = dc.cp
        fx, fy = self._xy(x, y)
        if f is not None and f.height:
            em = self._dist(abs(f.height), 1)
        else:
            em = _DEFAULT_EM_PT * self.dpi / 72.0
        face = f.face if f else ""
        charset = f.charset if f else 1
        if glyph:
            text, glyphs = "", struct.unpack("<%dH" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
        else:
            text, glyphs = decode_ansi(raw, charset, face, self.codepage), None
            if text is None:
                self.skipped += 1
                return
        if not text and not glyphs:
            return
        width = self._dist(sum(map(abs, dx)), 0) if dx is not None else None
        run = Run(text, fx, fy, face, em, f.weight if f else 400, bool(f and f.italic),
                  (f.escapement / 10.0) if f else 0.0, dc.color, glyphs, width)
        w = width if width is not None else run_width(run, text if text else "\0" * len(glyphs or ()))
        v = dc.align & _TA_BASELINE
        run.y = fy + _ASCENT * em if v == 0 else fy - (1 - _ASCENT) * em if v == _TA_BOTTOM else fy
        h = dc.align & _TA_CENTER
        run.x = fx - w / 2 if h == _TA_CENTER else fx - w if h == _TA_RIGHT else fx
        if not _ok(run.x, run.y, run.height, w):
            self.skipped += 1
            return
        if dc.align & _TA_UPDATECP:
            adv = sum(map(abs, dx)) if dx is not None else (w / (self._dist(1, 0) or 1.0))
            dc.cp = (x + adv, y)
        self.items.append(run)

    # ---- bitmaps -------------------------------------------------------------------------------------------------

    def _place(self, png, dest):
        x, y, w, h = dest
        (x0, y0), (x1, y1) = self._xy(x, y), self._xy(x + w, y + h)
        if _ok(x0, y0, x1, y1):
            self.items.append(Bitmap(png, "png", (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))

    def _bitmap(self, p, rop, dib_at, dest):
        """`dest` is the (x, y, w, h) of the destination rectangle from the record; the DIB starts at `dib_at`."""
        if rop is not None and rop not in _SRC_ROPS:
            self.skipped += 1
            return
        bmp = dib_to_bmp(bytes(p[dib_at:])) if len(p) > dib_at else None
        png = bmp_to_png(bmp) if bmp else None
        if png is None:
            self.skipped += 1
            return
        self._place(png, dest)

    def _blit(self, p, rop, at, dest):
        """BitBlt and StretchBlt: a Bitmap16 (1, 24 or 32 bits a pixel) at `at`, or none, which is a pattern fill."""
        if len(p) < at + 10:
            if rop == _PATCOPY:
                x, y, w, h = dest
                (x0, y0), (x1, y1) = self._xy(x, y), self._xy(x + w, y + h)
                self.budget.points(4)
                self._put(_rect_segs(x0, y0, x1, y1), False, True)
            else:
                self.skipped += 1
            return
        if rop not in _SRC_ROPS:
            self.skipped += 1
            return
        _typ, w, h, stride, _planes, bpp = struct.unpack_from("<hhhhBB", p, at)
        bits = bytes(p[at + 10:])
        row = (w * bpp + 7) // 8
        png = None
        if bpp in (1, 24, 32) and w > 0 and h > 0 and stride >= row and stride * h <= len(bits):
            pad = -row % 4  # a BMP row ends on 4 bytes, a DDB row on 2
            rows = b"".join(bits[i * stride:i * stride + row] + b"\0" * pad for i in range(h))
            pal = struct.pack("<8B", 0, 0, 0, 0, 255, 255, 255, 0) if bpp == 1 else b""
            head = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, bpp, 0, len(rows), 0, 0, 2 if bpp == 1 else 0, 0)
            png = bmp_to_png(struct.pack("<2sIHHI", b"BM", 54 + len(pal) + len(rows), 0, 0, 54 + len(pal))
                             + head + pal + rows)
        if png is None:
            self.skipped += 1
            return
        self._place(png, dest)

    def _bitblt(self, p):
        rop, _ys, _xs, h, w, yd, xd = struct.unpack_from("<I6h", p)
        self._blit(p, rop, 16, (xd, yd, w, h))

    def _stretchblt(self, p):
        rop, _sh, _sw, _ys, _xs, dh, dw, yd, xd = struct.unpack_from("<I8h", p)
        self._blit(p, rop, 20, (xd, yd, dw, dh))

    def _dibstretchblt(self, p):
        rop, _sh, _sw, _ys, _xs, dh, dw, yd, xd = struct.unpack_from("<I8h", p)
        self._bitmap(p, rop, 20, (xd, yd, dw, dh))

    def _dibbitblt(self, p):
        rop, _ys, _xs, h, w, yd, xd = struct.unpack_from("<I6h", p)
        self._bitmap(p, rop, 16, (xd, yd, w, h))

    def _stretchdib(self, p):
        rop, _usage, _sh, _sw, _ys, _xs, dh, dw, yd, xd = struct.unpack_from("<IH8h", p)
        self._bitmap(p, rop, 22, (xd, yd, dw, dh))

    def _setdibtodev(self, p):
        _usage, _scan, _start, _yd, _xd, h, w, yd, xd = struct.unpack_from("<H8h", p)
        self._bitmap(p, None, 18, (xd, yd, w, h))


_HANDLERS = {
    _SAVEDC: _Wmf._save, _RESTOREDC: _Wmf._restore,
    _SETMAPMODE: _Wmf._mapmode, _SETPOLYFILLMODE: _Wmf._polyfill, _SETTEXTCOLOR: _Wmf._textcolor,
    _SETTEXTALIGN: _Wmf._textalign,
    _SETWINDOWORG: _Wmf._window_org, _SETWINDOWEXT: _Wmf._window_ext,
    _SETVIEWPORTORG: _Wmf._viewport_org, _SETVIEWPORTEXT: _Wmf._viewport_ext,
    _OFFSETWINDOWORG: lambda s, p: s._offset("wo", p), _OFFSETVIEWPORTORG: lambda s, p: s._offset("vo", p),
    _SCALEWINDOWEXT: lambda s, p: s._scale("we", p), _SCALEVIEWPORTEXT: lambda s, p: s._scale("ve", p),
    _LINETO: _Wmf._lineto, _MOVETO: _Wmf._moveto,
    _INTERSECTCLIPRECT: _Wmf._intersect_clip, _EXCLUDECLIPRECT: _Wmf._skip, _SELECTCLIPREGION: _Wmf._select_clip_region,
    _ARC: _Wmf._arc_rec, _PIE: _Wmf._pie_rec, _CHORD: _Wmf._chord_rec, _ELLIPSE: _Wmf._ellipse,
    _RECTANGLE: _Wmf._rect, _ROUNDRECT: _Wmf._roundrect,
    _POLYGON: _Wmf._polygon, _POLYLINE: _Wmf._polyline, _POLYPOLYGON: _Wmf._polypolygon,
    _TEXTOUT: _Wmf._textout, _EXTTEXTOUT: _Wmf._exttextout,
    _SELECTOBJECT: _Wmf._select, _DELETEOBJECT: _Wmf._delete,
    _CREATEPEN: _Wmf._create_pen, _CREATEBRUSH: _Wmf._create_brush, _CREATEFONT: _Wmf._create_font,
    _CREATEREGION: _Wmf._create_region, _CREATEPALETTE: _Wmf._create_other,
    _CREATEPATTERNBRUSH: _Wmf._create_other, _DIBCREATEPATTERNBRUSH: _Wmf._create_other,
    _DIBBITBLT: _Wmf._dibbitblt, _DIBSTRETCHBLT: _Wmf._dibstretchblt, _STRETCHDIB: _Wmf._stretchdib,
    _SETDIBTODEV: _Wmf._setdibtodev, 0x0922: _Wmf._bitblt, 0x0B23: _Wmf._stretchblt,
}
