"""QuickDraw PICT (version 1 and 2) for figures: `parse` returns a vector.Drawing in the picture's frame units, y
growing downward, as QuickDraw has it.

The opcode numbers and data lengths follow Inside Macintosh: Imaging With QuickDraw, Appendix A. The bytes may come
with the 512-byte file head or without it (an OfficeArt BLIP, an rtf `\\macpict` once given its head). The frame is
picFrame at 72 dpi; a version-2 extended header (HeaderOp, version -2) gives the resolution (hRes) and the source
rectangle the opcodes are drawn in. Kept: lines, rects, round rects, ovals, arcs, polygons and regions (as their
boxes) in frame, paint, erase and fill forms; the pen size and the colors; text (LongText, DHText, DVText, DHDVText)
with TxFont, FontName, TxSize and TxFace, decoded as Mac Roman or, when the font id is in a CJK script range, as
Mac Japanese, Korean or Chinese; bitmaps (BitsRect, BitsRgn, PackBitsRect, PackBitsRgn, DirectBitsRect,
DirectBitsRgn); QuickTime compressed pictures whose codec is `jpeg`. Comments, other QuickTime codecs, invert forms
and the origin are skipped and counted. Every length and count read from an opcode is checked against the bytes; an
opcode that runs past the end stops the parse and marks the drawing damaged. A bitmap's rows are unpacked within
their declared row bytes, and a bitmap past images._MAX_PIXELS is stepped over without being decoded.
"""
import math
import struct

from . import images
from .limits import ReadError
from .officeart import dib_to_bmp
from .vector import Bitmap, Brush, Budget, Clip, Drawing, Path, Pen, Run, arc_beziers, run_width

# METAFILE_POINTS charged for an item that is not a path, as in metafile.py (kept equal there; a test checks it)
ITEM_WEIGHT = 16
CLIP_LEAST = 4

_END = 0xFF
_HEADER_OP, _QT_COMPRESSED, _QT_UNCOMPRESSED = 0x0C00, 0x8200, 0x8201

# QuickDraw's old 32-bit color constants (FgColor, BkColor)
_OLD_COLORS = {33: 0x000000, 30: 0xFFFFFF, 205: 0xFF0000, 341: 0x00FF00, 409: 0x0000FF, 273: 0x00FFFF,
               137: 0xFF00FF, 69: 0xFFFF00}
# Mac font ids with a well-known name; others have none
_FONTS = {0: "Chicago", 1: "Geneva", 2: "New York", 3: "Geneva", 4: "Monaco", 5: "Venice", 6: "London", 7: "Athens",
          8: "San Francisco", 9: "Toronto", 11: "Cairo", 12: "Los Angeles", 13: "Zapf Dingbats", 14: "Bookman",
          15: "Helvetica Narrow", 16: "Palatino", 18: "Zapf Chancery", 20: "Times", 21: "Helvetica", 22: "Courier",
          23: "Symbol", 24: "Taliesin"}
MIN_DPI, MAX_DPI = 1.0, 4800.0   # the resolution of an extended header is clamped to these
_DEFAULT_FACE = "Geneva"
_DEFAULT_SIZE = 12.0

_VERBS = ("frame", "paint", "erase", "invert", "fill")


def _ok(*vals) -> bool:
    return all(map(math.isfinite, vals))


def _encoding(font_id: int) -> str:
    """The codec of text in the font with this id: a CJK Mac script by the id range, else Mac Roman."""
    if font_id >= 28672:
        return "gb2312"
    if font_id >= 19456:
        return "euc_kr"
    if font_id >= 18432:
        return "big5"
    if font_id >= 16384:
        return "shift_jis"
    return "mac_roman"


def _decode(raw: bytes, font_id: int) -> str:
    try:
        s = raw.decode(_encoding(font_id), "replace")
    except LookupError:
        s = raw.decode("mac_roman", "replace")
    return "".join(" " if c in "\t\r\n" else c for c in s if c != "�" and (c >= " " or c in "\t\r\n") and c != "\x7f")


def _rect_segs(x0, y0, x1, y1) -> list:
    return [("M", x0, y0), ("L", x1, y0), ("L", x1, y1), ("L", x0, y1), ("Z",)]


def _oval_segs(x0, y0, x1, y1) -> list:
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
    return [("M", cx + rx, cy)] + arc_beziers(cx, cy, rx, ry, 0, 360) + [("Z",)]


def _round_segs(x0, y0, x1, y1, ew, eh) -> list:
    """A rectangle with corners of diameter ew by eh, clamped to the box."""
    rx, ry = min(abs(ew) / 2, abs(x1 - x0) / 2), min(abs(eh) / 2, abs(y1 - y0) / 2)
    if rx <= 0 or ry <= 0:
        return _rect_segs(x0, y0, x1, y1)
    out = [("M", x0 + rx, y0), ("L", x1 - rx, y0)]
    out += arc_beziers(x1 - rx, y0 + ry, rx, ry, -90, 90) + [("L", x1, y1 - ry)]
    out += arc_beziers(x1 - rx, y1 - ry, rx, ry, 0, 90) + [("L", x0 + rx, y1)]
    out += arc_beziers(x0 + rx, y1 - ry, rx, ry, 90, 90) + [("L", x0, y0 + ry)]
    out += arc_beziers(x0 + rx, y0 + ry, rx, ry, 180, 90) + [("Z",)]
    return out


def _unpack(src: bytes, expected: int, unit: int) -> bytes:
    """PackBits of `src` into `expected` bytes at most; `unit` is the size of a repeated item (2 for the words of a
    16-bit pixel row). A row that ends short is padded with zeros. The output never passes expected + 256 before it
    is cut, whatever the counts say."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < expected:
        c = src[i]
        i += 1
        if c < 128:
            k = (c + 1) * unit
            out += src[i:i + k]
            i += k
        elif c > 128:
            if i + unit > n:
                break
            out += src[i:i + unit] * (257 - c)
            i += unit
    out = out[:expected]
    if len(out) < expected:
        out += bytes(expected - len(out))
    return bytes(out)


_TAB16: list = []


def _tab16() -> list:
    """The BGR bytes of each 5-5-5 pixel word (built once)."""
    if not _TAB16:
        for v in range(65536):
            r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
            _TAB16.append(bytes((b << 3 | b >> 2, g << 3 | g >> 2, r << 3 | r >> 2)))
    return _TAB16


class _Pict:
    def __init__(self, data: bytes, budget):
        self.budget = budget
        n = len(data)
        # the version opcode at offset 10 is a PICT without its head, at 522 one with it
        if self._version(data, 10):
            self.d = data
        elif n > 522 and self._version(data, 522):
            self.d = data[512:]
        else:
            raise ReadError("damaged", "not a PICT")
        self.v2 = self.d[10:11] == b"\x00"
        d = self.d
        top, left, bottom, right = struct.unpack_from(">hhhh", d, 2)
        self.frame = (float(min(left, right)), float(min(top, bottom)), float(max(left, right)), float(max(top, bottom)))
        self.dpi = 72.0
        self.items: list = []
        self.skipped = 0
        self.damaged = False
        # QuickDraw state
        self.pen = (0, 0)       # (x, y) of the pen
        self.pnsize = (1, 1)    # (v, h)
        self.ovsize = (0, 0)    # (v, h)
        self.fg, self.bk = 0x000000, 0xFFFFFF
        self.fill_color = 0x000000
        self.font, self.face, self.size = 0, 0, _DEFAULT_SIZE
        self.names: dict[int, str] = {}
        self.last: dict[int, tuple] = {}   # group (0x3 rect .. 0x6 arc) -> the last rect (t, l, b, r)
        self.last_poly: list = []
        self.last_rgn: tuple | None = None

    @staticmethod
    def _version(d, at: int) -> bool:
        return d[at:at + 2] == b"\x11\x01" or d[at:at + 4] == b"\x00\x11\x02\xff"

    # ---- the opcode loop ------------------------------------------------------------------------------------------

    def run(self) -> Drawing:
        d = self.d
        n = len(d)
        pos = 14 if self.v2 else 12
        while True:
            if pos + (2 if self.v2 else 1) > n:
                self.damaged = True
                break
            if self.v2:
                op = struct.unpack_from(">H", d, pos)[0]
                pos += 2
            else:
                op = d[pos]
                pos += 1
            self.budget.tick()
            if op == _END:
                break
            try:
                pos = self._op(op, pos)
            except (struct.error, IndexError, ValueError):
                self.damaged = True
                break
            if self.v2 and pos & 1:
                pos += 1
        frame = self.frame
        if not _ok(*frame, self.dpi):
            frame, self.dpi = (0.0, 0.0, 1.0, 1.0), 72.0
        return Drawing(frame, self.dpi, self.items, self.damaged, self.skipped)

    def _need(self, end: int) -> int:
        if end > len(self.d):
            raise struct.error("past the end")
        return end

    def _skip_len(self, p: int, size: int, inclusive=False) -> int:
        """Past a u16 (size 2) or u32 (size 4) length and the data it counts."""
        n = struct.unpack_from(">H" if size == 2 else ">I", self.d, p)[0]
        return self._need(p + (n if inclusive else size + n))

    def _op(self, op: int, p: int) -> int:
        d = self.d
        if op >= 0x100:
            return self._big_op(op, p)
        if op >= 0x30:
            return self._shape_op(op, p) if op < 0x90 else self._data_op(op, p)
        if op == 0x00:
            return p
        if op == 0x01:                                  # Clip: a region, kept as its box
            box = self._region(p)
            self._emit(Clip(_rect_segs(*box)))
            return self._need(p + max(10, struct.unpack_from(">H", d, p)[0]))
        if op == 0x03:                                  # TxFont
            self.font = struct.unpack_from(">H", d, p)[0]
            return self._need(p + 2)
        if op == 0x04:                                  # TxFace
            self.face = d[self._need(p + 1) - 1]
            return p + 1
        if op in (0x05, 0x08, 0x15, 0x16):              # TxMode, PnMode, PnLocHFrac, ChExtra
            return self._need(p + 2)
        if op == 0x06:                                  # SpExtra
            return self._need(p + 4)
        if op == 0x07:                                  # PnSize
            self.pnsize = struct.unpack_from(">hh", d, p)
            return p + 4
        if op in (0x09, 0x02, 0x10):                    # PnPat, BkPat, TxRatio
            return self._need(p + 8)
        if op == 0x0A:                                  # FillPat: all clear is the back color, anything else the front
            self.fill_color = self.bk if not any(d[p:self._need(p + 8)]) else self.fg
            return p + 8
        if op == 0x0B:                                  # OvSize
            self.ovsize = struct.unpack_from(">hh", d, p)
            return p + 4
        if op == 0x0C:                                  # Origin
            self.skipped += 1
            return self._need(p + 4)
        if op == 0x0D:                                  # TxSize
            self.size = float(struct.unpack_from(">H", d, p)[0]) or _DEFAULT_SIZE
            return self._need(p + 2)
        if op in (0x0E, 0x0F):                          # FgColor, BkColor: the old color constants
            v = struct.unpack_from(">I", d, p)[0]
            c = _OLD_COLORS.get(v, 0x000000 if op == 0x0E else 0xFFFFFF)
            if op == 0x0E:
                self.fg = c
            else:
                self.bk = c
            return p + 4
        if op == 0x11:                                  # Version
            return self._need(p + 1)
        if op in (0x12, 0x13, 0x14):                    # BkPixPat, PnPixPat, FillPixPat
            return self._pixpat(p)
        if op in (0x17, 0x18, 0x19, 0x1C, 0x1E):
            return p
        if op in (0x1A, 0x1B, 0x1D, 0x1F):              # RGBFgCol, RGBBkCol, HiliteColor, OpColor
            r, g, b = d[p], d[p + 2], d[p + 4]
            self._need(p + 6)
            c = r << 16 | g << 8 | b
            if op == 0x1A:
                self.fg = c
            elif op == 0x1B:
                self.bk = c
            return p + 6
        if op == 0x20:                                  # Line
            y0, x0, y1, x1 = struct.unpack_from(">hhhh", d, p)
            self._line(x0, y0, x1, y1)
            self.pen = (x1, y1)
            return p + 8
        if op == 0x21:                                  # LineFrom
            y1, x1 = struct.unpack_from(">hh", d, p)
            self._line(self.pen[0], self.pen[1], x1, y1)
            self.pen = (x1, y1)
            return p + 4
        if op == 0x22:                                  # ShortLine
            y0, x0, dh, dv = struct.unpack_from(">hhbb", d, p)
            self._line(x0, y0, x0 + dh, y0 + dv)
            self.pen = (x0 + dh, y0 + dv)
            return p + 6
        if op == 0x23:                                  # ShortLineFrom
            dh, dv = struct.unpack_from(">bb", d, p)
            self._line(self.pen[0], self.pen[1], self.pen[0] + dh, self.pen[1] + dv)
            self.pen = (self.pen[0] + dh, self.pen[1] + dv)
            return p + 2
        if 0x28 <= op <= 0x2B:
            return self._text(op, p)
        if op == 0x2C:                                  # FontName: length, old font id, name length, name
            n = struct.unpack_from(">H", d, p)[0]
            end = self._need(p + 2 + n)
            if n >= 3:
                fid, ln = struct.unpack_from(">HB", d, p + 2)
                name = d[p + 5:min(p + 5 + ln, end)].decode("mac_roman", "replace").strip()
                if name and len(self.names) < 4096:
                    self.names[fid] = name
            return end
        if op in (0x2D, 0x2E, 0x2F, 0x24, 0x25, 0x26, 0x27):   # LineJustify, GlyphState and reserved: length, data
            return self._skip_len(p, 2)
        raise ValueError("unreachable opcode")

    def _big_op(self, op: int, p: int) -> int:
        if op == _HEADER_OP:
            end = self._need(p + 24)
            d = self.d
            if struct.unpack_from(">h", d, p)[0] == -2:  # extended: hRes, vRes (Fixed) and the source rectangle
                h = struct.unpack_from(">i", d, p + 4)[0] / 65536.0
                t, l, b, r = struct.unpack_from(">hhhh", d, p + 12)
                if h > 0 and _ok(h):
                    self.dpi = min(max(h, MIN_DPI), MAX_DPI)
                if r != l and b != t:
                    self.frame = (float(min(l, r)), float(min(t, b)), float(max(l, r)), float(max(t, b)))
            return end
        if op < 0x8000:                                 # reserved: the high byte counts words of data
            return self._need(p + (op >> 8) * 2)
        if op < 0x8100:
            return p
        if op in (_QT_COMPRESSED, _QT_UNCOMPRESSED):
            return self._quicktime(op, p)
        return self._skip_len(p, 4)                     # reserved: a u32 length and its data

    # ---- state and emitting ---------------------------------------------------------------------------------------

    def _charge(self, segs=None, least=0):
        """The one place METAFILE_POINTS is charged: every item as it goes out, kept or dropped. A path costs its
        points (a close counts as one), a clip its points but at least `least`, a run or a bitmap ITEM_WEIGHT."""
        if segs is None and not least:
            n = ITEM_WEIGHT
        else:
            n = max(least, sum((len(s) - 1) // 2 or 1 for s in segs or ()))
        self.budget.points(n)

    def _emit(self, item):
        """A Clip, Run or Bitmap, charged; one with a non-finite coordinate is dropped and counted."""
        if isinstance(item, Clip):
            self._charge(item.segs, CLIP_LEAST)
            vals = [v for s in item.segs or () for v in s[1:]]
        elif isinstance(item, Run):
            self._charge()
            vals = (item.x, item.y, item.height)
        else:
            self._charge()
            vals = item.box
        if _ok(*vals):
            self.items.append(item)
        else:
            self.skipped += 1

    def _pen(self) -> Pen | None:
        v, h = self.pnsize
        if v <= 0 or h <= 0:
            return None
        return Pen(self.fg, (v + h) / 2.0)

    def _put(self, segs, verb: str):
        """A Path in the form `verb` takes: frame strokes with the pen; paint fills with the front color, erase with
        the back color, fill with the fill pattern's. Invert has no color to give and is skipped."""
        self._charge(segs)
        if verb == "invert":
            self.skipped += 1
            return
        pen = brush = None
        if verb == "frame":
            pen = self._pen()
        else:
            brush = Brush({"paint": self.fg, "erase": self.bk, "fill": self.fill_color}[verb])
        if pen is None and brush is None:
            return
        if _ok(*[v for s in segs for v in s[1:]]):
            self.items.append(Path(segs, pen, brush))
        else:
            self.skipped += 1

    def _line(self, x0, y0, x1, y1):
        self._charge([("M", x0, y0), ("L", x1, y1)])
        pen = self._pen()
        if pen is not None:
            self.items.append(Path([("M", float(x0), float(y0)), ("L", float(x1), float(y1))], pen, None))

    # ---- text ------------------------------------------------------------------------------------------------------

    def _text(self, op: int, p: int) -> int:
        d = self.d
        x, y = self.pen
        if op == 0x28:                                  # LongText: pen point (v, h), count, text
            v, h, n = struct.unpack_from(">hhB", d, p)
            x, y, p = h, v, p + 5
        elif op == 0x29:                                # DHText
            dh, n = struct.unpack_from(">BB", d, p)
            x, p = x + dh, p + 2
        elif op == 0x2A:                                # DVText
            dv, n = struct.unpack_from(">BB", d, p)
            y, p = y + dv, p + 2
        else:                                           # DHDVText
            dh, dv, n = struct.unpack_from(">BBB", d, p)
            x, y, p = x + dh, y + dv, p + 3
        end = self._need(p + n)
        text = _decode(d[p:end], self.font)
        self.pen = (x, y)
        if text.strip():
            face = self.names.get(self.font) or _FONTS.get(self.font, _DEFAULT_FACE)
            run = Run(text, float(x), float(y), face, self.size, 700 if self.face & 1 else 400, bool(self.face & 2),
                      0.0, self.fg)
            self._emit(run)
            self.pen = (x + run_width(run, text), y)
        return end

    # ---- shapes ----------------------------------------------------------------------------------------------------

    def _region(self, p: int) -> tuple:
        """The box (x0, y0, x1, y1) of the region at p: u16 size (at least 10), then its rectangle."""
        size = struct.unpack_from(">H", self.d, p)[0]
        if size < 10:
            raise ValueError("region size")
        self._need(p + size)
        t, l, b, r = struct.unpack_from(">hhhh", self.d, p + 2)
        return (min(l, r), min(t, b), max(l, r), max(t, b))

    def _shape_op(self, op: int, p: int) -> int:
        d = self.d
        group, low = op >> 4, op & 15
        if group in (3, 4, 5, 6):                       # rect, round rect, oval, arc
            arc = group == 6
            if low <= 7:                                # with its rectangle (and the arc's two angles)
                rect = struct.unpack_from(">hhhh", d, p)
                self.last[group] = rect
                p += 8
            elif low <= 12:                             # the same rectangle as the last one of its kind
                rect = self.last.get(group, (0, 0, 0, 0))
            else:
                return self._need(p + (4 if arc else 0))
            at = p
            if arc:
                p = self._need(p + 4)
            if low in (5, 6, 7):
                return p
            verb = _VERBS[low % 8]
            if arc:
                self._arc(verb, rect, at)
            else:
                self._shape_draw(group, verb, rect)
            return p
        if group == 7:                                  # polygons: u16 size, rect, points
            if low <= 4 or low in (5, 6, 7):
                size = struct.unpack_from(">H", d, p)[0]
                if size < 10:
                    raise ValueError("polygon size")
                end = self._need(p + size)
                if low > 4:
                    return end
                pts = [struct.unpack_from(">hh", d, p + 10 + 4 * i) for i in range((size - 10) // 4)]
                self.last_poly = [(float(h), float(v)) for v, h in pts]
                self._poly(_VERBS[low])
                return end
            if low <= 12:
                self._poly(_VERBS[low - 8])
            return p
        # group 8: regions, kept as their boxes
        if low <= 7:
            size = struct.unpack_from(">H", d, p)[0]
            box = self._region(p)
            end = self._need(p + size)
            if low <= 4:
                self.last_rgn = box
                self._put(_rect_segs(*box), _VERBS[low])
            return end
        if low <= 12 and self.last_rgn is not None:
            self._put(_rect_segs(*self.last_rgn), _VERBS[low - 8])
        return p

    def _shape_draw(self, group: int, verb: str, rect: tuple):
        t, l, b, r = rect
        if group == 3:
            segs = _rect_segs(l, t, r, b)
        elif group == 4:
            segs = _round_segs(l, t, r, b, self.ovsize[1], self.ovsize[0])
        else:
            segs = _oval_segs(l, t, r, b)
        self._put(segs, verb)

    def _arc(self, verb: str, rect: tuple, at: int):
        """An arc of `rect`; the two angle words at `at` are the start and the extent, in degrees clockwise from
        12 o'clock. Frame is the open curve; the other forms fill the wedge."""
        start, extent = struct.unpack_from(">hh", self.d, at)
        t, l, b, r = rect
        cx, cy, rx, ry = (l + r) / 2, (t + b) / 2, abs(r - l) / 2, abs(b - t) / 2
        a0 = start - 90.0
        a = math.radians(a0)
        sx, sy = cx + rx * math.cos(a), cy + ry * math.sin(a)
        curve = arc_beziers(cx, cy, rx, ry, a0, float(extent))
        if verb == "frame":
            segs = [("M", sx, sy)] + curve
        else:
            segs = [("M", cx, cy), ("L", sx, sy)] + curve + [("Z",)]
        self._put(segs, verb)

    def _poly(self, verb: str):
        pts = self.last_poly
        if not pts:
            return
        segs = [("M", *pts[0])] + [("L", x, y) for x, y in pts[1:]]
        if verb != "frame":
            segs.append(("Z",))
        self._put(segs, verb)

    # ---- bitmaps ---------------------------------------------------------------------------------------------------

    def _data_op(self, op: int, p: int) -> int:
        d = self.d
        if op in (0x90, 0x91, 0x98, 0x99, 0x9A, 0x9B):
            return self._bits(p, packed=op in (0x98, 0x99, 0x9A, 0x9B), direct=op in (0x9A, 0x9B),
                              rgn=op in (0x91, 0x99, 0x9B))
        if op in (0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x9C, 0x9D, 0x9E, 0x9F, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8,
                  0xA9, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            return self._skip_len(p, 2)
        if op == 0xA0:                                  # ShortComment
            self.skipped += 1
            return self._need(p + 2)
        if op == 0xA1:                                  # LongComment: kind, size, data
            self.skipped += 1
            n = struct.unpack_from(">HH", d, p)[1]
            return self._need(p + 4 + n)
        if op < 0xD0:
            return p
        return self._skip_len(p, 4)                     # 0xD0-0xFE: a u32 length and its data

    def _pixpat(self, p: int) -> int:
        """BkPixPat, PnPixPat, FillPixPat: a type, the 8-byte pattern, then a PixMap with its data (type 1) or an
        RGB color (type 2). The pattern is not drawn; its bytes are stepped over."""
        d = self.d
        kind = struct.unpack_from(">H", d, p)[0]
        p += 2
        self._need(p + 8)
        if kind == 2:
            return self._need(p + 8 + 6)
        if kind != 1:
            raise ValueError("pixpat type")
        info, p = self._pixmap_head(p + 8, direct=False, force_pixmap=True)
        return self._rows(p, info, packed=True, keep=False)[1]

    def _pixmap_head(self, p: int, direct: bool, force_pixmap=False):
        """The PixMap or BitMap at p (past a direct opcode's base address): returns (info, position of the rects)."""
        d = self.d
        rb = struct.unpack_from(">H", d, p)[0]
        pixmap = bool(rb & 0x8000) or direct or force_pixmap
        info = {"rowbytes": rb & 0x3FFF}
        t, l, b, r = struct.unpack_from(">hhhh", d, p + 2)
        info.update(w=r - l, h=b - t)
        p += 10
        if pixmap:
            packtype, = struct.unpack_from(">H", d, p + 2)
            psize, cmp = struct.unpack_from(">HH", d, p + 18)
            info.update(packtype=packtype, psize=psize, cmp=cmp)
            self._need(p + 36)
            p += 36
            pal = [0x000000] * 256
            if not direct:                              # the color table: seed, flags, count - 1, then entries
                flags, count = struct.unpack_from(">HH", d, p + 4)
                count += 1
                self._need(p + 8 + 8 * count)
                for i in range(count):
                    value, r16, g16, b16 = struct.unpack_from(">HHHH", d, p + 8 + 8 * i)
                    idx = i if flags & 0x8000 or value >= 256 else value
                    if idx < 256:
                        pal[idx] = d[p + 8 + 8 * i + 2] << 16 | d[p + 8 + 8 * i + 4] << 8 | d[p + 8 + 8 * i + 6]
                p += 8 + 8 * count
            info["pal"] = pal
        else:                                           # a 1-bit BitMap: 0 is white, 1 is black
            info.update(packtype=0, psize=1, cmp=1, pal=[0xFFFFFF] + [0x000000] * 255)
        info["end"] = p
        return info, p

    def _bits(self, p: int, packed: bool, direct: bool, rgn: bool) -> int:
        d = self.d
        if direct:
            p = self._need(p + 4)                       # the base address
        info, p = self._pixmap_head(p, direct)
        self._need(p + 18)
        sr = struct.unpack_from(">hhhh", d, p)
        dt, dl, db, dr = struct.unpack_from(">hhhh", d, p + 8)
        p += 18                                         # srcRect, dstRect, mode
        if rgn:
            p = self._need(p + max(10, struct.unpack_from(">H", d, p)[0]))
        w, h = info["w"], info["h"]
        keep = 0 < w and 0 < h and w * h <= images._MAX_PIXELS
        rows, p = self._rows(p, info, packed, keep)
        png = self._png(rows, info) if keep else None
        if png is None:                                 # dropped, and charged like a kept one
            self._charge()
            self.skipped += 1
            return p
        self._emit(Bitmap(png, "png", (float(dl), float(dt), float(dr), float(db))))
        return p

    def _rows(self, p: int, info: dict, packed: bool, keep: bool):
        """Walks the pixel rows from p. Returns (rows, end); rows holds bytes per row only when `keep`."""
        d = self.d
        rb, w, h, ps, cmp = info["rowbytes"], info["w"], max(0, info["h"]), info["psize"], info["cmp"]
        pt = info["packtype"]
        if ps not in (1, 2, 4, 8, 16, 32):
            raise ValueError("pixel size")
        need = (w * ps + 7) // 8 if ps <= 8 else w * ps // 8
        if rb < max(need, 0):
            raise ValueError("row bytes")
        compressed = packed and rb >= 8 and pt != 1
        unit = 1
        if ps == 16:
            pt = 3 if pt == 0 else pt
            unit = 2 if pt == 3 else 1
            keep_len = 2 * w
        elif ps == 32:
            pt = 4 if pt == 0 else pt
            cmpn = cmp if cmp in (3, 4) else 3
            # what a row holds once unpacked: planes, or 3-byte pixels, or (not compressed) pad, R, G, B pixels
            keep_len = w * cmpn if compressed and pt == 4 else (3 * w if compressed and pt == 2 else 4 * w)
        else:
            keep_len = need
        # a row is cut to what its pixels need, never padded out to the declared rowBytes, and the bytes kept are capped
        room = images._MAX_PIXELS * 4
        rows = []
        for _ in range(h):
            if compressed:
                if rb > 250:
                    n = struct.unpack_from(">H", d, p)[0]
                    p += 2
                else:
                    n = d[p]
                    p += 1
                end = self._need(p + n)
                if keep:
                    rows.append(_unpack(d[p:end], keep_len, unit))
                p = end
            else:
                end = self._need(p + rb)
                if keep:
                    rows.append(d[p:min(end, p + keep_len)])
                p = end
            if keep:
                room -= keep_len
                if room < 0:
                    raise ValueError("rows too large")
            self.budget.points(0)                       # the deadline is read here, once in CLOCK_EVERY calls
        info["_mode"] = (ps, pt, cmp, compressed)
        return rows, p

    def _png(self, rows: list, info: dict) -> bytes | None:
        """The rows as a PNG: through a DIB, then images.bmp_to_png."""
        w, h = info["w"], info["h"]
        ps, pt, cmp, compressed = info["_mode"]
        out = []
        if ps <= 8:
            per = 8 // ps
            mask = (1 << ps) - 1
            expand = [bytes((v >> (8 - ps - i * ps)) & mask for i in range(per)) for v in range(256)]
            pad = (-w) % 4
            for row in rows:
                idx = b"".join(map(expand.__getitem__, row))[:w] if ps < 8 else row[:w]
                out.append(idx + bytes(pad))
            pal = info["pal"]
            table = b"".join(bytes((c & 0xFF, c >> 8 & 0xFF, c >> 16, 0)) for c in pal)
            bpp, used = 8, 256
        else:
            tab = _tab16() if ps == 16 else None
            pad = (-3 * w) % 4
            for row in rows:
                if ps == 16:
                    words = struct.unpack(">%dH" % w, row[:2 * w])
                    bgr = b"".join(map(tab.__getitem__, words))
                elif compressed and pt == 4:
                    planes = cmp if cmp in (3, 4) else 3
                    r_, g_, b_ = (row[(planes - 3 + k) * w:(planes - 2 + k) * w] for k in range(3))
                    ba = bytearray(3 * w)
                    ba[0::3], ba[1::3], ba[2::3] = b_, g_, r_
                    bgr = bytes(ba)
                elif compressed and pt == 2:            # pad byte dropped: R, G, B in pixel order
                    ba = bytearray(row[:3 * w])
                    ba[0::3], ba[2::3] = ba[2::3], ba[0::3]
                    bgr = bytes(ba)
                else:                                   # unpacked: pad, R, G, B
                    px = row[:4 * w]
                    ba = bytearray(3 * w)
                    ba[0::3], ba[1::3], ba[2::3] = px[3::4], px[2::4], px[1::4]
                    bgr = bytes(ba)
                out.append(bgr + bytes(pad))
            table, bpp, used = b"", 24, 0
        body = b"".join(reversed(out))
        dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, 0, len(body), 0, 0, used, 0) + table + body
        bmp = dib_to_bmp(dib)
        return images.bmp_to_png(bmp) if bmp is not None else None

    # ---- QuickTime -------------------------------------------------------------------------------------------------

    def _quicktime(self, op: int, p: int) -> int:
        """CompressedQuickTime (0x8200): a u32 length, then version, the matrix, matte and mask sizes, the source
        rectangle, the ImageDescription and the image. Only the `jpeg` codec is kept. Uncompressed is skipped."""
        d = self.d
        n = struct.unpack_from(">I", d, p)[0]
        end = self._need(p + 4 + n)
        if op == _QT_UNCOMPRESSED or not self._qt_jpeg(d[p + 4:end]):
            self._charge()
            self.skipped += 1
        return end

    def _qt_jpeg(self, q: bytes) -> bool:
        try:
            m = struct.unpack_from(">9i", q, 2)
            matte, = struct.unpack_from(">I", q, 38)
            st, sl, sb, sr = struct.unpack_from(">hhhh", q, 52)
            mask, = struct.unpack_from(">I", q, 64)
            at = 68 + matte + mask
            idsize, ctype = struct.unpack_from(">I4s", q, at)
            if ctype != b"jpeg" or idsize < 46 or at + idsize > len(q):
                return False
            width, height = struct.unpack_from(">HH", q, at + 32)
            dsize, = struct.unpack_from(">I", q, at + 44)
            data = q[at + idsize:at + idsize + dsize] if dsize else q[at + idsize:]
            if data[:3] != b"\xff\xd8\xff":
                return False
            if sr == sl or sb == st:
                st, sl, sb, sr = 0, 0, height, width
            a, b, c, dd, tx, ty = m[0] / 65536.0, m[1] / 65536.0, m[3] / 65536.0, m[4] / 65536.0, m[6] / 65536.0, m[7] / 65536.0
            xs, ys = [], []
            for x, y in ((sl, st), (sr, st), (sl, sb), (sr, sb)):
                xs.append(a * x + c * y + tx)
                ys.append(b * x + dd * y + ty)
            self._emit(Bitmap(bytes(data), "jpg", (min(xs), min(ys), max(xs), max(ys))))
            return True
        except struct.error:
            return False


def parse(data: bytes, deadline: float | None = None) -> Drawing:
    """The drawing of a PICT, with or without the 512-byte file head. ReadError("damaged", "not a PICT") when the
    bytes are not one. Past `deadline` (a time.monotonic() value) the figure stops with ReadError("too_large",
    "figure time")."""
    return _Pict(bytes(data), Budget(deadline=deadline)).run()
