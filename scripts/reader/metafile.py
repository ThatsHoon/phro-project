"""Metafile parsers for figures: WMF, EMF, and the EMF+ records inside EMF (reader/emfplus.py).

`parse` dispatches on the magic and returns a vector.Drawing in the picture's frame units, y growing downward. The
WMF half follows MS-WMF: the placeable and plain headers, the record subset office pictures use, and a DC state
(`_DC`) the EMF half shares. The EMF half follows MS-EMF: its frame is rclFrame in 0.01 mm (dpi 2540); logical
coordinates go through the world transform, then the window and viewport, then the reference device's size in
0.01 mm. A dual EMF+ file follows its EMF+ records and skips the EMF drawing records they duplicate, except after
EmfPlusGetDC. Every length, count and index read from a record is checked against the record first; a record that
runs past the end stops the parse and marks the drawing damaged.
"""
import heapq
import math
import struct
from dataclasses import dataclass, replace

from . import emfplus, limits
from .emfplus import IDENT, angle_of, combine, good, mul
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


def _symbol_code(o: int) -> int | None:
    """The Adobe Symbol code of a wide character: its private-use form U+F020-U+F0FF, or itself under 0x100."""
    if 0xF000 <= o <= 0xF0FF:
        return o - 0xF000
    return o if o < 0x100 else None


def decode_wide(raw: bytes, charset: int, face: str) -> str | None:
    """UTF-16LE text of a font as str. A font named Symbol maps its private-use codes and codes under 0x100 through
    the Adobe Symbol table. Other SYMBOL_CHARSET fonts, and text made only of private-use symbol codes in any font (a
    symbol font EMF+ names without a charset), give None."""
    s = raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace")
    s = "".join(c for c in s if c not in "�\x00")
    if face.strip().lower() == "symbol":
        out = []
        for c in s:
            k = _symbol_code(ord(c))
            out.append(c if k is None else SYMBOL.get(k, ""))
        return "".join(out)
    if charset == SYMBOL_CHARSET:
        return None
    if s.strip() and all(0xF000 <= ord(c) <= 0xF0FF for c in s if not c.isspace()):
        return None
    return s


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
    xf: tuple = IDENT  # the EMF world transform
    clockwise: bool = False  # the EMF arc direction

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


# METAFILE_POINTS charged for an item that is not a path: a run or a bitmap costs far more to put out and to write
# than one point, and a clip is at least its rectangle
ITEM_WEIGHT = 16
CLIP_LEAST = 4


def parse(data: bytes, codepage: str = "", deadline: float | None = None) -> Drawing:
    """The drawing of a WMF or EMF. ReadError("damaged", "not a metafile") when the bytes are neither. Past
    `deadline` (a time.monotonic() value) the figure stops with ReadError("too_large", "figure time")."""
    return _parse(data, codepage, 0, Budget(deadline=deadline))


def _parse(data: bytes, codepage: str, depth: int, budget) -> Drawing:
    """`depth` counts the EMF+ image objects this metafile is nested in; `budget` is shared with the outer ones."""
    if data[:4] == b"\xd7\xcd\xc6\x9a" or _is_plain_wmf(data):
        return _Wmf(data, codepage, budget).run()
    if _is_emf(data):
        return _Emf(data, codepage, depth, budget).run()
    raise ReadError("damaged", "not a metafile")


class _Wmf:
    def __init__(self, data: bytes, codepage: str, budget=None):
        self.data = data
        self.codepage = codepage
        self.budget = budget or Budget()
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
            self._emit(Clip(None if self._clip_out is None else _rect_segs(*self._clip_out)))

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

    def _charge(self, segs=None, least=0):
        """The one place METAFILE_POINTS is charged: every item as it goes out, kept or dropped. A path costs its
        points (a close counts as one), a clip its points but at least `least`, a run or a bitmap ITEM_WEIGHT.
        Geometry drawn again, a path object filled many times or a nested picture placed many times, is so paid for
        each time it is drawn."""
        if segs is None and not least:
            n = ITEM_WEIGHT
        else:
            n = max(least, sum((len(s) - 1) // 2 or 1 for s in segs or ()))
        self.budget.points(n)

    def _emit(self, item):
        """A Clip or Bitmap, charged; one with a non-finite coordinate is dropped and counted."""
        if isinstance(item, Clip):
            self._charge(item.segs, CLIP_LEAST)
            vals = () if item.segs is None else [v for s in item.segs for v in s[1:]]
        else:
            self._charge()
            vals = item.box
        if _ok(*vals):
            self.items.append(item)
        else:
            self.skipped += 1

    def _put(self, segs, pen, fill, even_odd=False):
        """Adds a Path; a path with no pen and no brush shows nothing, one with a non-finite coordinate is dropped."""
        self._charge(segs)
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
        self._put([("M", ax, ay), ("L", bx, by)], True, False)

    def _rect(self, p):
        x0, y0, x1, y1 = self._box(p)
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
        self._put(segs, True, True)

    def _ellipse(self, p):
        x0, y0, x1, y1 = self._box(p)
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
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
        out = []
        for sg in segs:
            pts = [self._xy(sg[i], sg[i + 1]) for i in range(1, len(sg), 2)]
            out.append((sg[0],) + tuple(v for q in pts for v in q))
        self._put(out, True, kind != "arc")

    def _poly(self, p, closed):
        n = struct.unpack_from("<h", p)[0]
        if n < 0 or 2 + 4 * n > len(p):
            raise ValueError
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
        self._charge()
        self.items.append(run)

    # ---- bitmaps -------------------------------------------------------------------------------------------------

    def _place(self, png, dest):
        x, y, w, h = dest
        (x0, y0), (x1, y1) = self._xy(x, y), self._xy(x + w, y + h)
        if _ok(x0, y0, x1, y1):
            self._emit(Bitmap(png, "png", (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))

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


# ---- EMF -------------------------------------------------------------------------------------------------------------

# EMF record types (MS-EMF 2.1.1)
_E_EOF, _E_COMMENT = 14, 70
_EMF_PLUS = 0x2B464D45  # "EMF+": the GDICOMMENT identifier of EMF+ records
# records that draw, skipped while a dual file's EMF+ records are followed (the path bracket and its clip too)
_E_DRAW = frozenset({2, 3, 4, 5, 6, 7, 8, 41, 42, 43, 44, 45, 46, 47, 53, 54, 55, 56, 59, 60, 61, 62, 63, 64, 67, 68,
                     71, 72, 73, 74, 76, 77, 78, 79, 80, 81, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 96, 97, 108, 114,
                     116, 118})
# records that change nothing the figure shows, so they are not counted as skipped
_E_QUIET = frozenset({13, 15, 16, 18, 20, 21, 23, 25, 28, 48, 50, 51, 52, 58, 65, 66, 98, 99, 100, 101, 104, 105, 106,
                      109, 110, 111, 112, 113, 115, 119, 120, 121, 122})
_STOCK = 0x80000000
_STOCK_BRUSH = {0: 0xFFFFFF, 1: 0xC0C0C0, 2: 0x808080, 3: 0x404040, 4: 0x000000, 5: None, 0x12: 0xFFFFFF}
_STOCK_PEN = {6: _Pen(0xFFFFFF, 0.0), 7: _Pen(0, 0.0), 8: _Pen(0, 0.0, True), 0x13: _Pen(0, 0.0)}
_STOCK_FONT = {0xA, 0xB, 0xC, 0xD, 0xE, 0x10, 0x11}
_ETO_NO_RECT, _ETO_SMALL_CHARS = 0x100, 0x200
_RGN_MODES = {1: emfplus.INTERSECT, 2: emfplus.UNION, 3: emfplus.XOR, 4: emfplus.EXCLUDE, 5: emfplus.REPLACE}
_ERRORS = (struct.error, IndexError, ValueError, ZeroDivisionError, OverflowError)
_UNSET = object()


def _bbox(pts) -> tuple:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _round_segs(x0, y0, x1, y1, ew, eh) -> list:
    """A rounded rectangle in its own units; the corner ellipse is ew by eh, at most the rectangle."""
    rx = min(abs(ew) / 2, abs(x1 - x0) / 2) * (1 if x1 >= x0 else -1)
    ry = min(abs(eh) / 2, abs(y1 - y0) / 2) * (1 if y1 >= y0 else -1)
    k = 0.5523
    a, b, c, d = x0, y0, x1, y1
    return [("M", a + rx, b),
            ("L", c - rx, b), ("C", c - rx + k * rx, b, c, b + ry - k * ry, c, b + ry),
            ("L", c, d - ry), ("C", c, d - ry + k * ry, c - rx + k * rx, d, c - rx, d),
            ("L", a + rx, d), ("C", a + rx - k * rx, d, a, d - ry + k * ry, a, d - ry),
            ("L", a, b + ry), ("C", a, b + ry - k * ry, a + rx - k * rx, b, a + rx, b), ("Z",)]


class _Emf(_Wmf):
    """EMF records over the DC state and drawing helpers of _Wmf, with EMF's own transform: logical -> world
    transform -> window and viewport -> device pixels -> frame units through the reference device's size."""

    def __init__(self, data: bytes, codepage: str, depth: int = 0, budget=None):
        self.data = data
        self.codepage = codepage
        self.depth = depth
        self.budget = budget or Budget()
        self.items = []
        self.skipped = 0
        self.damaged = False
        self.dc = _DC(brush=_Brush(0xFFFFFF))  # GDI's defaults: a white brush and a black pen
        self.stack = []
        self.objs: dict = {}
        self.dpi = 2540.0
        self._clip_out = None
        self._placeable = False
        self.path = None        # the path bracket's segments in frame units, kept after ENDPATH until used
        self.in_path = False
        self.path_open = False  # the path's last figure is open at the current position
        self.plus = None
        self._ghost = False     # a skipped record is run for its current position alone: nothing goes out
        hsize = struct.unpack_from("<I", data, 4)[0]
        if hsize < 88 or hsize > len(data):
            raise ReadError("damaged", "emf header")
        bl, bt, br, bb, fl, ft, fr, fb = struct.unpack_from("<8i", data, 8)
        devx, devy, mmx, mmy = struct.unpack_from("<4i", data, 72)
        umx, umy = struct.unpack_from("<2i", data, 100) if hsize >= 108 else (0, 0)
        self.k = (self._unit(umx / 10, mmx * 100, devx), self._unit(umy / 10, mmy * 100, devy))
        if fl != fr and ft != fb:
            self.frame = (float(min(fl, fr)), float(min(ft, fb)), float(max(fl, fr)), float(max(ft, fb)))
        elif br >= bl and bb >= bt:  # no frame: the inclusive device bounds
            self.frame = (bl * self.k[0], bt * self.k[1], (br + 1) * self.k[0], (bb + 1) * self.k[1])
        else:
            self.frame = None
        self.pos = hsize

    @staticmethod
    def _unit(um, mm, dev) -> float:
        """Frame units (0.01 mm) per device pixel: szlMicrometers, else szlMillimeters, over szlDevice; else 96 dpi."""
        if dev > 0:
            for v in (um, mm):
                if v > 0:
                    return v / dev
        return 2540 / 96

    def run(self) -> Drawing:
        d, n = self.data, len(self.data)
        pos = self.pos
        while pos < n:
            if n - pos < 8:
                self.damaged = True
                break
            typ, size = struct.unpack_from("<II", d, pos)
            if size < 8 or pos + size > n:
                self.damaged = True
                break
            self.budget.tick()
            if typ == _E_EOF:
                break
            if typ in _E_DRAW and not self._follow():  # the EMF+ records drew this
                if typ in _E_MOVES:
                    self._move_only(_EMF_HANDLERS[typ], d[pos + 8:pos + size])
                pos += size
                continue
            h = _EMF_HANDLERS.get(typ)
            try:
                if h is not None:
                    h(self, d[pos + 8:pos + size])
                elif typ not in _E_QUIET:
                    self.skipped += 1
            except _ERRORS:
                self.damaged = True
                self.skipped += 1
            pos += size
        frame = self.frame or self._extent()
        if not _ok(*frame):
            frame = (0.0, 0.0, 1.0, 1.0)
        return Drawing(frame, self.dpi, self.items, self.damaged, self.skipped)

    def _move_only(self, h, p):
        """A drawing record the EMF+ records drew still moves the current position, which a later GetDC section
        draws from; it runs with nothing going out, and only the position is kept."""
        skipped, damaged = self.skipped, self.damaged
        self._ghost = True
        try:
            h(self, p)
        except _ERRORS:
            pass
        finally:
            self._ghost = False
            self.skipped, self.damaged = skipped, damaged

    def _follow(self) -> bool:
        """EMF drawing records are followed: no EMF+ header was seen, or EmfPlusGetDC handed back to them."""
        return self.plus is None or not self.plus.active or self.plus.gdi

    # ---- the transform -------------------------------------------------------------------------------------------

    def _freeze(self):
        pass  # the frame comes from the header

    def _page_axis(self, v: float, a: int) -> float:
        dc, k = self.dc, self.k[a]
        if dc.mapmode in _FIXED:  # device units per logical unit times frame units per device unit: 2540 / per inch
            s = 2540.0 / _FIXED[dc.mapmode] / k
            return ((v - dc.wo[a]) * (-s if a else s) + dc.vo[a]) * k
        if dc.mapmode in (7, 8) and dc.we and dc.we[a]:  # MM_ISOTROPIC, MM_ANISOTROPIC
            if dc.ve is not None:
                s = dc.ve[a] / dc.we[a]
                if dc.mapmode == 7 and dc.we[0] and dc.we[1]:
                    s = math.copysign(min(abs(dc.ve[0] / dc.we[0]), abs(dc.ve[1] / dc.we[1])), s)
                return ((v - dc.wo[a]) * s + dc.vo[a]) * k
            if self.frame is not None:  # no viewport: the window maps onto the frame
                return self.frame[a] + (v - dc.wo[a]) / dc.we[a] * (self.frame[a + 2] - self.frame[a])
        return (v - dc.wo[a] + dc.vo[a]) * k

    def _xy(self, x, y) -> tuple[float, float]:
        m = self.dc.xf
        return self._page_axis(x * m[0] + y * m[2] + m[4], 0), self._page_axis(x * m[1] + y * m[3] + m[5], 1)

    def _vec(self, x, y) -> tuple[float, float]:
        (ax, ay), (bx, by) = self._xy(0, 0), self._xy(x, y)
        return bx - ax, by - ay

    def _dist(self, v: float, a: int) -> float:
        return math.hypot(*self._vec(0 if a else v, v if a else 0))

    def _map(self, segs) -> list:
        out = []
        for s in segs:
            v = []
            for i in range(1, len(s), 2):
                v += self._xy(s[i], s[i + 1])
            out.append((s[0],) + tuple(v))
        return out

    def _lbox(self, l, t, r, b) -> tuple:
        return _bbox([self._xy(l, t), self._xy(r, t), self._xy(l, b), self._xy(r, b)])

    # ---- output, shared with emfplus.Plus ------------------------------------------------------------------------

    def _add_path(self, segs, pen, brush, even_odd):
        if not segs:
            return
        self._charge(segs)
        if pen is None and brush is None:
            return
        if not all(_ok(*s[1:]) for s in segs):
            self.skipped += 1
            return
        if pen is not None and not _ok(pen.width):
            pen = Pen(pen.color, self.dpi / 96.0)
        self.items.append(Path(segs, pen, brush, even_odd))

    def _add_run(self, run: Run):
        if self._ghost:
            return
        self._charge()
        if not _ok(run.x, run.y, run.height, run.angle) or (run.width is not None and not _ok(run.width)):
            self.skipped += 1
            return
        self.items.append(run)

    def _sync_clip(self):
        """States the clip of whichever records are followed when it differs from the one last stated."""
        want = self.dc.clip if self._follow() else self.plus.clip
        if want is not None and not _ok(*want):
            want = None
        if want != self._clip_out:
            self._clip_out = want
            self._emit(Clip(None if want is None else _rect_segs(*want)))

    def _force_clip(self):
        self._clip_out = _UNSET  # a nested picture stated its own clips: state the current one again
        self._sync_clip()

    def _wide(self, raw: bytes, face: str):
        return decode_wide(raw, 1, face)

    def _nested(self, data: bytes):
        """The drawing of a metafile inside an EMF+ image object, or None; past METAFILE_NESTING it is skipped."""
        if self.depth >= limits.METAFILE_NESTING:
            self.skipped += 1
            return None
        try:
            d = _parse(bytes(data), self.codepage, self.depth + 1, self.budget)
        except ReadError as e:
            if e.kind == "too_large":
                raise
            self.skipped += 1
            return None
        self.skipped += d.skipped
        self.damaged = self.damaged or d.damaged
        return d

    def _put(self, segs, pen, fill, even_odd=False):
        """Segments in frame units: into an open path bracket, else drawn with the DC's pen and brush."""
        if self._ghost:
            return
        if self.in_path:
            self.path.extend(segs)
            self.path_open = False
            return
        brush = Brush(self.dc.brush.color) if fill and self.dc.brush.color is not None else None
        self._add_path(segs, self._pen() if pen else None, brush, even_odd)

    def _to(self, rest, last):
        """Frame segments that continue from the current position, which then moves to `last` (logical)."""
        start = ("M",) + self._xy(*self.dc.cp)
        self.dc.cp = last
        if self._ghost:
            return
        if self.in_path:
            if not self.path_open:
                self.path.append(start)
            self.path.extend(rest)
            self.path_open = True
            return
        self._put([start] + rest, True, False)

    # ---- objects and state ---------------------------------------------------------------------------------------

    def _e_add(self, i, obj):
        if i == 0 or i >= limits.METAFILE_OBJECTS:  # index 0 is the metafile itself
            self.skipped += 1
            return
        self.objs[i] = obj

    def _e_pen(self, p):
        i, style, w = struct.unpack_from("<IIi", p)
        self._e_add(i, ("pen", _Pen(_rgb(p[16:19]), float(abs(w)), style & 0xF == 5)))

    def _e_extpen(self, p):
        i, = struct.unpack_from("<I", p)
        style, w, bstyle = struct.unpack_from("<III", p, 20)
        width = float(w) if style & 0x10000 else 0.0  # a cosmetic pen is one device unit wide
        self._e_add(i, ("pen", _Pen(_rgb(p[32:35]), width, style & 0xF == 5 or bstyle == 1)))

    def _e_brush(self, p):
        i, style = struct.unpack_from("<II", p)
        self._e_add(i, ("brush", _Brush(_rgb(p[8:11]) if style in (0, 2) else None)))

    def _e_other(self, p):  # palettes and pattern brushes hold their slot only
        self._e_add(struct.unpack_from("<I", p)[0], ("other", None))

    def _e_font(self, p):
        if len(p) < 96:
            raise ValueError
        i, height, _w, esc, _o, weight = struct.unpack_from("<I5i", p)
        face = p[32:96].decode("utf-16-le", "replace").split("\0")[0]
        self._e_add(i, ("font", _Font(height, esc, weight or 400, bool(p[24]), p[27], face)))

    def _e_select(self, p):
        i, = struct.unpack_from("<I", p)
        if i & _STOCK:
            j = i & 0x7FFFFFFF
            if j in _STOCK_BRUSH:
                self.dc.brush = _Brush(_STOCK_BRUSH[j])
            elif j in _STOCK_PEN:
                self.dc.pen = _STOCK_PEN[j]
            elif j in _STOCK_FONT:
                self.dc.font = None
            return
        o = self.objs.get(i)
        if o is None:
            return
        kind, v = o
        if kind == "pen":
            self.dc.pen = v
        elif kind == "brush":
            self.dc.brush = v
        elif kind == "font":
            self.dc.font = v

    def _e_delete(self, p):
        self.objs.pop(struct.unpack_from("<I", p)[0], None)

    def _e_pair(self, name, p):
        setattr(self.dc, name, struct.unpack_from("<ii", p))

    def _e_scale(self, name, p):
        xn, xd, yn, yd = struct.unpack_from("<4i", p)
        cur = getattr(self.dc, name) or (1, 1)
        if xd and yd:
            setattr(self.dc, name, (cur[0] * xn / xd, cur[1] * yn / yd))

    def _e_moveto(self, p):
        self.dc.cp = struct.unpack_from("<ii", p)
        self.path_open = False

    def _e_restore(self, p):
        n, = struct.unpack_from("<i", p)
        i = len(self.stack) + n if n < 0 else n - 1
        if 0 <= i < len(self.stack):
            self.dc = self.stack[i]
            del self.stack[i:]
            self._sync_clip()

    def _e_world(self, p):
        m = struct.unpack_from("<6f", p)
        self.dc.xf = m if good(m) else IDENT

    def _e_modify(self, p):
        m = struct.unpack_from("<6f", p)
        mode, = struct.unpack_from("<I", p, 24)
        if mode == 1:
            xf = IDENT
        elif not good(m):  # a bad matrix is the identity: multiplying by it changes nothing
            return
        elif mode == 2:  # MWT_LEFTMULTIPLY: the new matrix first
            xf = mul(m, self.dc.xf)
        elif mode == 3:  # MWT_RIGHTMULTIPLY
            xf = mul(self.dc.xf, m)
        elif mode == 4:  # MWT_SET
            xf = m
        else:
            return
        self.dc.xf = xf if good(xf) else IDENT

    def _e_arcdir(self, p):
        self.dc.clockwise = struct.unpack_from("<I", p)[0] == 2

    # ---- clipping ------------------------------------------------------------------------------------------------

    def _clip(self, box, mode):
        self.dc.clip = combine(self.dc.clip, box, mode)
        self._sync_clip()

    def _e_intersect_clip(self, p):
        self._clip(self._lbox(*struct.unpack_from("<4i", p)), emfplus.INTERSECT)

    def _e_offset_clip(self, p):
        if self.dc.clip is not None:
            vx, vy = self._vec(*struct.unpack_from("<ii", p))
            c = self.dc.clip
            self._clip((c[0] + vx, c[1] + vy, c[2] + vx, c[3] + vy), emfplus.REPLACE)

    def _e_select_clip_rgn(self, p):
        """A region's bounding box, in device units; RGN_COPY with no region removes the clip."""
        cb, mode = struct.unpack_from("<II", p)
        m = _RGN_MODES.get(mode)
        if m is None:
            return
        if cb == 0:
            if m == emfplus.REPLACE:
                self._clip(None, m)
            return
        if cb < 32 or 8 + cb > len(p):
            raise ValueError
        l, t, r, b = struct.unpack_from("<4i", p, 24)
        kx, ky = self.k
        self._clip((min(l, r) * kx, min(t, b) * ky, max(l, r) * kx, max(t, b) * ky), m)

    def _e_select_clip_path(self, p):
        m = _RGN_MODES.get(struct.unpack_from("<I", p)[0])
        segs, self.path, self.in_path = self.path, None, False
        pts = [q for s in segs or () for q in zip(s[1::2], s[2::2])]
        if pts and m is not None:
            self._clip(_bbox(pts), m)

    # ---- paths ---------------------------------------------------------------------------------------------------

    def _e_beginpath(self, p):
        self.path, self.in_path, self.path_open = [], True, False

    def _e_endpath(self, p):
        self.in_path = False

    def _e_closefigure(self, p):
        if self.in_path and self.path:
            self.path.append(("Z",))
            self.path_open = False

    def _e_abortpath(self, p):
        self.path, self.in_path = None, False

    def _e_usepath(self, stroke, fill):
        segs, self.path, self.in_path = self.path, None, False
        if not segs:
            return
        brush = Brush(self.dc.brush.color) if fill and self.dc.brush.color is not None else None
        self._add_path(segs, self._pen() if stroke else None, brush, self.dc.even_odd if fill else False)

    # ---- drawing -------------------------------------------------------------------------------------------------

    def _e_pts(self, p, off, n, wide) -> list:
        if n < 0 or off + (8 if wide else 4) * n > len(p):
            raise ValueError
        v = struct.unpack_from(("<%di" if wide else "<%dh") % (2 * n), p, off)
        return [(v[2 * i], v[2 * i + 1]) for i in range(n)]

    def _e_poly(self, p, kind, wide):
        n, = struct.unpack_from("<I", p, 16)
        pts = self._e_pts(p, 20, n, wide)
        if kind == "lineto":
            if pts:
                self._to(self._map([("L",) + q for q in pts]), pts[-1])
            return
        if kind == "bezierto":
            rest = [("C",) + pts[i] + pts[i + 1] + pts[i + 2] for i in range(0, len(pts) - 2, 3)]
            if rest:
                self._to(self._map(rest), pts[3 * len(rest) - 1])
            return
        if len(pts) < 2:
            return
        if kind == "bezier":
            segs = [("M",) + pts[0]] + [("C",) + pts[i] + pts[i + 1] + pts[i + 2] for i in range(1, len(pts) - 2, 3)]
            if len(segs) > 1:
                self._put(self._map(segs), True, False)
        elif kind == "polygon":
            self._put(self._map([("M",) + pts[0]] + [("L",) + q for q in pts[1:]] + [("Z",)]), True, True,
                      self.dc.even_odd)
        else:
            self._put(self._map([("M",) + pts[0]] + [("L",) + q for q in pts[1:]]), True, False)

    def _e_polypoly(self, p, closed, wide):
        npoly, total = struct.unpack_from("<II", p, 16)
        if 24 + 4 * npoly > len(p):
            raise ValueError
        counts = struct.unpack_from("<%dI" % npoly, p, 24)
        if sum(counts) > total:
            raise ValueError
        pts = self._e_pts(p, 24 + 4 * npoly, total, wide)
        segs, at = [], 0
        for c in counts:
            q, at = pts[at:at + c], at + c
            if len(q) >= 2:
                segs += [("M",) + q[0]] + [("L",) + v for v in q[1:]] + ([("Z",)] if closed else [])
        if segs:
            self._put(self._map(segs), True, closed, self.dc.even_odd if closed else False)

    def _e_lineto(self, p):
        x, y = struct.unpack_from("<ii", p)
        self._to([("L",) + self._xy(x, y)], (x, y))

    def _e_rect(self, p):
        self._put(self._map(_rect_segs(*struct.unpack_from("<4i", p))), True, True)

    def _e_ellipse(self, p):
        l, t, r, b = struct.unpack_from("<4i", p)
        cx, cy, rx, ry = (l + r) / 2, (t + b) / 2, abs(r - l) / 2, abs(b - t) / 2
        self._put(self._map([("M", cx + rx, cy)] + arc_beziers(cx, cy, rx, ry, 0, 360) + [("Z",)]), True, True)

    def _e_roundrect(self, p):
        self._put(self._map(_round_segs(*struct.unpack_from("<6i", p))), True, True)

    def _e_arc(self, p, kind):
        """ARC, CHORD, PIE and ARCTO: angles and sweep in logical units, in the DC's arc direction."""
        l, t, r, b, xs, ys, xe, ye = struct.unpack_from("<8i", p)
        cx, cy, rx, ry = (l + r) / 2, (t + b) / 2, abs(r - l) / 2, abs(b - t) / 2
        if not rx or not ry:
            return
        a1 = math.degrees(math.atan2((ys - cy) / ry, (xs - cx) / rx))
        a2 = math.degrees(math.atan2((ye - cy) / ry, (xe - cx) / rx))
        if self.dc.clockwise:
            sweep = ((a2 - a1) % 360.0) or 360.0
        else:
            sweep = -(((a1 - a2) % 360.0) or 360.0)  # counterclockwise with y down, the GDI default
        arcs = arc_beziers(cx, cy, rx, ry, a1, sweep)
        if not arcs:
            self.skipped += 1
            return
        a = math.radians(a1)
        start = ("M", cx + rx * math.cos(a), cy + ry * math.sin(a))
        if kind == "arcto":
            self._to(self._map([("L",) + start[1:]] + arcs), arcs[-1][5:7])
            return
        segs = [start] + arcs
        if kind == "pie":
            segs += [("L", cx, cy), ("Z",)]
        elif kind == "chord":
            segs += [("Z",)]
        self._put(self._map(segs), True, kind != "arc")

    def _e_anglearc(self, p):
        x, y, rad = struct.unpack_from("<iiI", p)
        start, sweep = struct.unpack_from("<ff", p, 12)
        arcs = arc_beziers(x, y, rad, rad, -start, -sweep)  # counterclockwise as seen
        if not arcs:
            self.skipped += 1
            return
        a = math.radians(-start)
        self._to(self._map([("L", x + rad * math.cos(a), y + rad * math.sin(a))] + arcs), arcs[-1][5:7])

    def _rgn_rects(self, p, at, cb) -> list:
        """The rectangles of RegionData at `at` (a 32-byte header, then nCount rectangles in device units)."""
        if cb < 32 or at + cb > len(p):
            raise ValueError
        n, = struct.unpack_from("<I", p, at + 8)
        if at + 32 + 16 * n > len(p):
            raise ValueError
        kx, ky = self.k
        segs = []
        for i in range(n):
            l, t, r, b = struct.unpack_from("<4i", p, at + 32 + 16 * i)
            segs += _rect_segs(l * kx, t * ky, r * kx, b * ky)
        return segs

    def _e_fillrgn(self, p):
        cb, ih = struct.unpack_from("<II", p, 16)
        if ih & _STOCK:
            color = _STOCK_BRUSH.get(ih & 0x7FFFFFFF)
        else:
            o = self.objs.get(ih)
            color = o[1].color if o is not None and o[0] == "brush" else None
        segs = self._rgn_rects(p, 24, cb)
        if color is not None and segs:
            self._add_path(segs, None, Brush(color), False)

    def _e_paintrgn(self, p):
        cb, = struct.unpack_from("<I", p, 16)
        segs = self._rgn_rects(p, 20, cb)
        if self.dc.brush.color is not None and segs:
            self._add_path(segs, None, Brush(self.dc.brush.color), False)

    # ---- text ----------------------------------------------------------------------------------------------------

    def _e_exttext(self, p, wide):
        self._emrtext(p, 28, wide, False)  # the EmrText follows the bounds, graphics mode and scales

    def _e_polytext(self, p, wide):
        """POLYTEXTOUTA/W: cStrings EmrText entries of 40 bytes, each drawn as an EXTTEXTOUT."""
        c, = struct.unpack_from("<I", p, 28)
        if 32 + 40 * c > len(p):
            raise ValueError
        for i in range(c):
            self._emrtext(p, 32 + 40 * i, wide, True)

    def _emrtext(self, p, at, wide, fixed):
        """An EmrText at `at`: its string and dx offsets count from the record's start, 8 bytes before `p`. Windows,
        Wine and LibreOffice keep the 16-byte rectangle even under ETO_NO_RECT, though MS-EMF says it is left out:
        offDx is looked for after the rectangle first, and at the spec's place only when that one does not fit."""
        x, y, n, off_s, opts = struct.unpack_from("<iiIII", p, at)
        glyph = bool(opts & _ETO_GLYPH_INDEX)
        nbytes = 2 * n if wide or glyph else n
        s0 = off_s - 8
        if s0 < 0 or s0 + nbytes > len(p):
            raise ValueError
        per = 2 if opts & _ETO_PDY else 1  # ETO_PDY: an x and a y advance for each character
        d0 = self._dx_at(p, at + 36, at + 40, n * per, s0, nbytes)
        if d0 is None and opts & _ETO_NO_RECT and not fixed:
            d0 = self._dx_at(p, at + 20, at + 24, n * per, s0, nbytes)
        dx = struct.unpack_from("<%di" % (n * per), p, d0)[::per] if d0 is not None else None
        self._etext(x, y, p[s0:s0 + nbytes], wide, glyph, dx)

    @staticmethod
    def _dx_at(p, field, floor, count, s0, nbytes):
        """Where in `p` the `count` advances start, by the offDx at `field`, or None when it points before `floor`
        (the end of the EmrText it belongs to), past the record, or into the string."""
        if not count or field + 4 > len(p):
            return None
        d0 = struct.unpack_from("<I", p, field)[0] - 8
        end = d0 + 4 * count
        if d0 < floor or end > len(p) or (d0 < s0 + nbytes and s0 < end):
            return None
        return d0

    def _e_smalltext(self, p):
        """SMALLTEXTOUT: 8-bit characters with ETO_SMALL_CHARS, else UTF-16; bounds unless ETO_NO_RECT."""
        x, y, n, opts = struct.unpack_from("<iiII", p)
        at = 28 + (0 if opts & _ETO_NO_RECT else 16)
        glyph = bool(opts & _ETO_GLYPH_INDEX)
        small = bool(opts & _ETO_SMALL_CHARS) and not glyph
        nbytes = n if small else 2 * n
        if at + nbytes > len(p):
            raise ValueError
        self._etext(x, y, p[at:at + nbytes], not small, glyph, None)

    def _etext(self, x, y, raw: bytes, wide: bool, glyph: bool, dx):
        dc, f = self.dc, self.dc.font
        face = f.face if f else ""
        charset = f.charset if f else 1
        glyphs = None
        if glyph:
            text, glyphs = "", struct.unpack("<%dH" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
        else:
            text = decode_wide(raw, charset, face) if wide else decode_ansi(raw, charset, face, self.codepage)
            if text is None:
                self.skipped += 1
                return
        if not text and not glyphs:
            return
        if dc.align & _TA_UPDATECP:
            x, y = dc.cp
        ox, oy = self._xy(x, y)
        ux, uy = self._vec(1, 0)
        along = math.hypot(ux, uy)
        if f is not None and f.height:
            em = abs(f.height) * math.hypot(*self._vec(0, 1))
        else:
            em = _DEFAULT_EM_PT * self.dpi / 72.0
        width = abs(sum(dx)) * along if dx is not None else None
        angle = (f.escapement / 10.0 if f else 0.0) + angle_of((ux, uy, 0.0, 0.0, 0.0, 0.0))
        run = Run(text, ox, oy, face, em, f.weight if f else 400, bool(f and f.italic), angle, dc.color, glyphs,
                  width)
        w = width if width is not None else run_width(run, text if text else "\0" * len(glyphs))
        t = math.radians(angle)
        bx, by, nx, ny = math.cos(t), -math.sin(t), math.sin(t), math.cos(t)  # along the baseline, and down across it
        v = dc.align & _TA_BASELINE
        drop = _ASCENT * em if v == 0 else -(1 - _ASCENT) * em if v == _TA_BOTTOM else 0.0
        h = dc.align & _TA_CENTER
        back = w / 2 if h == _TA_CENTER else w if h == _TA_RIGHT else 0.0
        run.x, run.y = ox + nx * drop - bx * back, oy + ny * drop - by * back
        if dc.align & _TA_UPDATECP:
            adv = sum(map(abs, dx)) if dx is not None else w / (along or 1.0)
            dc.cp = (x + adv, y)
        self._add_run(run)

    # ---- bitmaps -------------------------------------------------------------------------------------------------

    def _e_image(self, p, off_bmi, cb_bmi, off_bits, cb_bits):
        """(bytes, kind) of a DIB whose header and bits lie at record offsets; BI_JPEG and BI_PNG bits are kept."""
        b0, s0 = off_bmi - 8, off_bits - 8
        if cb_bmi < 12 or b0 < 0 or s0 < 0 or b0 + cb_bmi > len(p) or s0 + cb_bits > len(p):
            return None
        bmi, bits = bytes(p[b0:b0 + cb_bmi]), bytes(p[s0:s0 + cb_bits])
        if cb_bmi >= 20 and struct.unpack_from("<I", bmi)[0] >= 40:
            comp, = struct.unpack_from("<I", bmi, 16)
            if comp == 4:
                return (bits, "jpg") if bits[:3] == b"\xff\xd8\xff" else None
            if comp == 5:
                return (bits, "png") if bits[:8] == b"\x89PNG\r\n\x1a\n" else None
        bmp = dib_to_bmp(bmi + bits)
        png = bmp_to_png(bmp) if bmp else None
        return (png, "png") if png else None

    def _e_put_image(self, got, x, y, w, h):
        box = self._lbox(x, y, x + w, y + h)
        if got is None or not _ok(*box):
            self.skipped += 1
            return
        self._emit(Bitmap(got[0], got[1], box))

    def _e_blt(self, p, any_rop=False):
        """BITBLT, STRETCHBLT, ALPHABLEND and TRANSPARENTBLT share the layout up to the source bitmap's offsets."""
        x, y, w, h, rop = struct.unpack_from("<4iI", p, 16)
        off_bmi, cb_bmi, off_bits, cb_bits = struct.unpack_from("<4I", p, 76)
        if cb_bmi == 0:
            if rop == _PATCOPY and not any_rop:
                self._put(self._map(_rect_segs(x, y, x + w, y + h)), False, True)
            else:
                self.skipped += 1
            return
        if not any_rop and rop not in _SRC_ROPS:
            self.skipped += 1
            return
        self._e_put_image(self._e_image(p, off_bmi, cb_bmi, off_bits, cb_bits), x, y, w, h)

    def _e_stretchdib(self, p):
        x, y = struct.unpack_from("<ii", p, 16)
        off_bmi, cb_bmi, off_bits, cb_bits, _usage, rop, w, h = struct.unpack_from("<6Iii", p, 40)
        if rop not in _SRC_ROPS:
            self.skipped += 1
            return
        self._e_put_image(self._e_image(p, off_bmi, cb_bmi, off_bits, cb_bits), x, y, w, h)

    def _e_setdib(self, p):
        x, y = struct.unpack_from("<ii", p, 16)
        w, h = struct.unpack_from("<ii", p, 32)  # the source size, drawn one to one
        off_bmi, cb_bmi, off_bits, cb_bits = struct.unpack_from("<4I", p, 40)
        self._e_put_image(self._e_image(p, off_bmi, cb_bmi, off_bits, cb_bits), x, y, w, h)

    # ---- EMF+ ----------------------------------------------------------------------------------------------------

    def _e_comment(self, p):
        if len(p) < 8:
            return
        size, ident = struct.unpack_from("<II", p)
        if ident != _EMF_PLUS:
            return
        if size < 4 or 4 + size > len(p):
            raise ValueError
        if self.plus is None:
            self.plus = emfplus.Plus(self)
        self.plus.feed(p[8:4 + size])
        self._sync_clip()


_EMF_HANDLERS = {
    2: lambda s, p: s._e_poly(p, "bezier", True), 3: lambda s, p: s._e_poly(p, "polygon", True),
    4: lambda s, p: s._e_poly(p, "polyline", True), 5: lambda s, p: s._e_poly(p, "bezierto", True),
    6: lambda s, p: s._e_poly(p, "lineto", True), 7: lambda s, p: s._e_polypoly(p, False, True),
    8: lambda s, p: s._e_polypoly(p, True, True),
    85: lambda s, p: s._e_poly(p, "bezier", False), 86: lambda s, p: s._e_poly(p, "polygon", False),
    87: lambda s, p: s._e_poly(p, "polyline", False), 88: lambda s, p: s._e_poly(p, "bezierto", False),
    89: lambda s, p: s._e_poly(p, "lineto", False), 90: lambda s, p: s._e_polypoly(p, False, False),
    91: lambda s, p: s._e_polypoly(p, True, False),
    9: lambda s, p: s._e_pair("we", p), 10: lambda s, p: s._e_pair("wo", p),
    11: lambda s, p: s._e_pair("ve", p), 12: lambda s, p: s._e_pair("vo", p),
    31: lambda s, p: s._e_scale("ve", p), 32: lambda s, p: s._e_scale("we", p),
    17: _Wmf._mapmode, 19: _Wmf._polyfill, 22: _Wmf._textalign, 24: _Wmf._textcolor,
    26: _Emf._e_offset_clip, 27: _Emf._e_moveto, 29: _Wmf._skip, 30: _Emf._e_intersect_clip,
    33: _Wmf._save, 34: _Emf._e_restore, 35: _Emf._e_world, 36: _Emf._e_modify,
    37: _Emf._e_select, 38: _Emf._e_pen, 39: _Emf._e_brush, 40: _Emf._e_delete, 49: _Emf._e_other,
    93: _Emf._e_other, 94: _Emf._e_other, 95: _Emf._e_extpen, 82: _Emf._e_font, 57: _Emf._e_arcdir,
    41: _Emf._e_anglearc, 42: _Emf._e_ellipse, 43: _Emf._e_rect, 44: _Emf._e_roundrect,
    45: lambda s, p: s._e_arc(p, "arc"), 46: lambda s, p: s._e_arc(p, "chord"), 47: lambda s, p: s._e_arc(p, "pie"),
    55: lambda s, p: s._e_arc(p, "arcto"), 54: _Emf._e_lineto,
    59: _Emf._e_beginpath, 60: _Emf._e_endpath, 61: _Emf._e_closefigure, 68: _Emf._e_abortpath,
    62: lambda s, p: s._e_usepath(False, True), 63: lambda s, p: s._e_usepath(True, True),
    64: lambda s, p: s._e_usepath(True, False), 67: _Emf._e_select_clip_path, 75: _Emf._e_select_clip_rgn,
    71: _Emf._e_fillrgn, 74: _Emf._e_paintrgn, _E_COMMENT: _Emf._e_comment,
    76: _Emf._e_blt, 77: _Emf._e_blt, 114: lambda s, p: s._e_blt(p, True), 116: lambda s, p: s._e_blt(p, True),
    80: _Emf._e_setdib, 81: _Emf._e_stretchdib,
    83: lambda s, p: s._e_exttext(p, False), 84: lambda s, p: s._e_exttext(p, True),
    96: lambda s, p: s._e_polytext(p, False), 97: lambda s, p: s._e_polytext(p, True), 108: _Emf._e_smalltext,
}
# drawing records that move the current position: LINETO, POLYLINETO, POLYBEZIERTO (and their 16-bit forms), ARCTO,
# ANGLEARC, and the text records under TA_UPDATECP
_E_MOVES = frozenset({5, 6, 41, 54, 55, 83, 84, 88, 89, 96, 97, 108})
