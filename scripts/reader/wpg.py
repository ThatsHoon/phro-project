# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libwpg 982c77da697b967ffed07fb01206698e7fd679d4, src/lib/WPG1Parser.cpp, WPG2Parser.cpp, WPGBitmap.cpp, WPGXParser.cpp, WPGHeader.cpp, WPGraphics.cpp.
"""WordPerfect Graphics (WPG 1 and WPG 2) for figures: `parse` returns a vector.Drawing, y growing downward.

Ported from libwpg (MPL-2.0; copyright 2004-2007 Ariya Hidayat, Fridrich Strba and Marc Oude Kotte, and the libwpg
contributors) at 982c77da697b967ffed07fb01206698e7fd679d4, `src/lib/`:
`WPGHeader.cpp` (`WPGHeader::load`, `isSupported`: the 16-byte WPC head, the file type 0x16, the version, the encryption
word), `WPGraphics.cpp` (`WPGraphics::parse`: the seek to `startOfDocument()`, the choice of WPG 1 or 2 by the major
version, and for WPG 1 the nested head libwpg's own dump once wrote, whose document starts at `startOfDocument()+16`),
`WPGXParser.cpp` (`readVariableLengthInteger`),
`WPG1Parser.cpp` (`parse`'s record loop, `handleStartWPG`, `handleColormap`, `handleFillAttributes`,
`handleLineAttributes`, `handleLine`, `handlePolyline`, `handlePolygon`, `handleRectangle`, `handleEllipse`,
`handleCurvedPolyline`, `handleGraphicsTextAttributes`, `handleGraphicsTextTypeOne`, `handleGraphicsTextTypeTwo`,
`decodeRLE`, `fillPixels`, `handleBitmapTypeOne`, `handleBitmapTypeTwo`, the default palette), `WPG2Parser.cpp`
(`parse`'s record loop and subgroup stack, `handleStartWPG`, `handleColorPalette`, `handleDPColorPalette`, the pen, brush
and pen-size records, `parseCharacterization`, `handlePolyline`, `handlePolycurve`, `handleRectangle`, `handleArc`,
`handleCompoundPolygon` with `flushCompoundPolygon`, `handleBitmap`, `handleBitmapData`, `handleObjectCapsule`,
`handleObjectImage`, `handleTextLine`, `handleTextBlock`, `handleTextData`, `transformXY`, the default palette) and
`WPGBitmap.cpp` (the bitmap as a bottom-up DIB, here 24 bits).

The frame is the Start WPG record's size. WPG 1 is 1200 units to the inch; WPG 2 takes the units per inch from Start
WPG (0 means 1200), with y scaled to the horizontal unit when the two differ, and reads its numbers as 16.16 when the
precision byte says double. Kept: lines, polylines, rectangles, polygons, ellipses, arcs, curves, text with its size,
colour and angle, colormaps, and bitmaps (WPG 1 types 1 and 2 with their RLE; WPG 2 formats 1-9 and 12, raw or RLE;
PNG and BMP objects of an Object Capsule). Text is decoded with `wpd_charsets`: WPG 2 text is a WordPerfect 6 function
stream and WPG 1 graphics text type 2 a WordPerfect 5 one; type 1 text is bytes (cp437 above 0x7F). Embedded
PostScript, other object formats and the records a figure has no use for are skipped and counted.

Where libwpg is wrong or reads past a record, this port differs: every length, count and offset is checked against the
record it sits in and the record against the file, a count the record cannot hold is cut to what it holds, RLE and
bitmap sizes come from the pixels and the data, never from a declared size, and a bitmap past images._MAX_PIXELS is
stepped over undecoded. Known differences: WPG 2's `m_yofs` takes the lower of the two image y values (libwpg takes
imageX2 for one branch); the y scale of a WPG 2 matrix is `sycos` (libwpg writes `sxcos` to both); the fraction of a
translation is kept; a compound polygon's matrix is applied after its member's own; a rectangle's four corners go
through the matrix; a rounded corner is drawn; WPG 2 text blocks are drawn (libwpg drops them); WPG 1 curved
polylines keep their last segment; the 5-5-5 and 4-4-4-4 pixels are scaled to 8 bits; graphics text type 2 is drawn
(libwpg has it under `#if 0`); a WPG 1 bitmap type 2 is placed by its raw 1200-unit corner coordinates; the RLE size
gates of libwpg (a record's bytes against the pixels it must fill) are gone, since pixels, `need` and the deadline
already bound the work; a bitmap record whose matrix scale is negative is mirrored (libwpg's `m_hFlipped` and
`m_vFlipped`, never reset there, belong to one record here); a matrix with zero determinant or a non-finite element is
the identity for its item and the item is counted in `skipped`; an embedded PNG is read for its size and skipped past
the pixel cap. A record that runs past the end of the file is dropped and ends the parse (`damaged`: a truncated final
record is not drawn); one too short for its own fields is dropped (`damaged`) and the next is read. The double-precision
translation of a matrix (`ix + fx/65536`, in coordinate units) is a guess: libwpg adds the integer part to 16.16
numbers, and no sample confirms either.
Every item is charged to the budget once, in `_charge`, kept or not.
"""
import math
import struct

from . import images, limits
from .formats.wpd_charsets import char
from .limits import ReadError
from .officeart import dib_to_bmp
from .pict import _tab16
from .vector import Bitmap, Brush, Budget, Drawing, Path, Pen, Run, arc_beziers, kept

# METAFILE_POINTS charged for an item that is not a path, as in metafile.py (kept equal there; a test checks it)
ITEM_WEIGHT = 16

_PAL1 = bytes.fromhex(
    "00000000007f007f00007f7f7f00007f007f7f3f007f7f7fc0c0c00000ff00ff0000ffffff0000ff00ffffff00ffffff"
    "0000001414142020202c2c2c383838454545515151616161717171828282929292a2a2a2b6b6b6cbcbcbe3e3e3ffffff"
    "0000ff4100ff7d00ffbe00ffff00ffff00beff007dff0041ff0000ff4100ff7d00ffbe00ffff00beff007dff0041ff00"
    "00ff0000ff4100ff7d00ffbe00ffff00beff007dff0041ff7d7dff9e7dffbe7dffdf7dffff7dffff7ddfff7dbeff7d9e"
    "ff7d7dff9e7dffbe7dffdf7dffff7ddfff7dbeff7d9eff7d7dff7d7dff9e7dffbe7dffdf7dffff7ddfff7dbeff7d9eff"
    "b6b6ffc7b6ffdbb6ffebb6ffffb6ffffb6ebffb6dbffb6c7ffb6b6ffc7b6ffdbb6ffebb6ffffb6ebffb6dbffb6c7ffb6"
    "b6dfb6b6ffc7b6ffdbb6ffebb6ffffb6ebffb6dbffb6c7ff0000711c007138007155007171007171005571003871001c"
    "710000711c007138007155007171005571003871001c710000710000711c007138007155007171005571003871001c71"
    "383871453871553871613871713871713861713855713845713838714538715538716138717138617138557138457138"
    "387138387145387155387161387171386171385571384571515171595171615171695171715171715169715161715159"
    "715151715951716151716951717151697151617151597151517151517159517161517169517171516971516171515971"
    "000041100041200041300041410041410030410020410010410000411000412000413000414100304100204100104100"
    "004100004110004120004130004141003041002041001041202041282041302041382041412041412038412030412028"
    "412020412820413020413820414120384120304120284120204120204128204130204138204141203841203041202841"
    "3c3c41303c41343c413c3c41413c41413c3c413c34413c30413c3c41303c41343c413c3c41413c3c413c34413c30413c"
    "2c412c2c41302c41342c413c2c41412c3c412c34412c3041000000000000000000000000000000000000000000000000"
)
_PAL2 = bytes.fromhex(
    "000000ffffff7f7f7fbfbfbf00007f007f00007f7f7f00007f007f7f7f000000ff00ff0000ffffff0000ff00ffffff00"
    "000000111111222222333333444444555555666666777777888888999999aaaaaabbbbbbccccccddddddeeeeeeffffff"
    "7b0000910000a70000bd0000d30000e90000ff0000ff1616ff2c2cff4242ff5858ff6e6eff8484ff9a9affb0b0ffc6c6"
    "7b3d00914800a75300bd5e00d36900e97400ff7f00ff8a16ff952cffa042ffab58ffb66effc184ffcc9affd7b0ffe2c6"
    "7b7b00919100a7a700bdbd00d3d300e9e900ffff00ffff16ffff2cffff42ffff58ffff6effff84ffff9affffb0ffffc6"
    "3d7b0048910053a7005ebd0069d30074e9007fff008aff1695ff2ca0ff42abff58b6ff6ec1ff84ccff9ad7ffb0e2ffc6"
    "007b0000910000a70000bd0000d30000e90000ff0016ff162cff2c42ff4258ff586eff6e84ff849aff9ab0ffb0c6ffc6"
    "007b3d00914800a75300bd5e00d36900e97400ff7f16ff8a2cff9542ffa058ffab6effb684ffc19affccb0ffd7c6ffe2"
    "007b7b00919100a7a700bdbd00d3d300e9e900ffff16ffff2cffff42ffff58ffff6effff84ffff9affffb0ffffc6ffff"
    "00567b0064910072a70080b0008ed3009ce400aaff16b1ff2cb8ff42bfff58c6ff6ecdff84d4ff9adbffb0e2ffc6e9ff"
    "002b7b0032910039a70040bd0047d3004ee40055ff1663ff2c71ff427fff588dff6e9bff84a9ff9ab7ffb0c5ffc6d3ff"
    "00007b0000910000a70000bd0000d30000e40000ff1616ff2c2cff4242ff5858ff6e6eff8484ff9a9affb0b0ffc6c6ff"
    "3d007b4800915300a75e00bd6900d37400e47f00ff8a16ff952cffa042ffab58ffb66effc184ffcc9affd7b0ffe2c6ff"
    "7b007b910091a700a7bd00bdd300d3e400e4ff00ffff16ffff2cffff42ffff58ffff6effff84ffff9affffb0ffffc6ff"
    "7b003d910048a70053bd005ed30069e40074ff007fff168aff2c95ff42a0ff58abff6eb6ff84c1ff9accffb0d7ffc6e2"
    "33291147381761451c734f24875c299c632bb0692bc7d430cc8747d48f57db9c69e3a878e8b38cf0c49cf7cfb0ffe0c7"
)

_PAL1_RGB = [int.from_bytes(_PAL1[i:i + 3], "big") for i in range(0, 768, 3)]
_PAL2_RGB = [int.from_bytes(_PAL2[i:i + 3], "big") for i in range(0, 768, 3)]

_MAGIC = b"\xffWPC"
_HEAD = 16
_WPG_TYPE = 0x16
_FACE = "Times New Roman"
_LEADING = 1.2              # line height of multi-line text, in ems
_PNG = b"\x89PNG\r\n\x1a\n"
# the text bytes of WordPerfect 6 (as formats/wpd.py reads them): 0x01-0x1F are international characters
_INTL = "åÅæÆäÄáàâãÃçÇëéÉèêíñÑøØõÕöÖüÜúù"
_SPACE6 = frozenset((0x80, 0x81, 0xBA, 0xBB, 0xBC, 0xCD, 0xCE, 0xCF))
_PARA6 = frozenset((0x87, *range(0xB4, 0xBA), *range(0xBD, 0xCD)))
_FIXED6 = (4, 5, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 8, 8)       # the fixed groups 0xF0-0xFE, closing byte included
_EOL_SPACE = frozenset((1, 2, 3, 0x14, 0x15, 0x16))
_EOL_PARA = frozenset((*range(4, 0x14), *range(0x17, 0x1D)))
_FIXED5 = (4, 9, 11, 3, 3, 5, 6, 7, 4, 5, 6, 6, 8, 10, 10, 12)  # the fixed groups 0xC0-0xCF of WordPerfect 5
_PARA5 = frozenset((0x0A, 0x0C, 0x8C, 0x90, 0x99))
_SPACE5 = frozenset((0x0B, 0x0D, 0x93, 0x94, 0x95, 0xA0))
_HYPHEN5 = frozenset((0xA9, 0xAA, 0xAB))
# WPG 1 record types that carry structure only, and the WPG 2 ones (nothing to draw, nothing to count)
_QUIET1 = frozenset((0x03, 0x12, 0x15, 0x19))
_QUIET2 = frozenset((0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0A, 0x10, 0x11, 0x20, 0x22, 0x27, 0x28, 0x29, 0x2A, 0x2D,
                     0x2E, 0x2F, 0x30, 0x33, 0x34, 0x35, 0x38, 0x39))
_OBJ_MIME = {0x06: "bmp", 0x25: "png"}                         # the Object Capsule descriptions kept


class _Short(Exception):
    """A field read past the end of its record."""


class _R:
    """A little-endian reader of d[p:end]; every read is checked against `end`."""
    __slots__ = ("d", "p", "end")

    def __init__(self, d: bytes, p: int, end: int):
        self.d, self.p, self.end = d, p, end

    def left(self) -> int:
        return self.end - self.p

    def take(self, n: int) -> int:
        q = self.p + n
        if n < 0 or q > self.end:
            raise _Short
        p, self.p = self.p, q
        return p

    def skip(self, n: int):
        self.take(n)

    def u8(self) -> int:
        return self.d[self.take(1)]

    def u16(self) -> int:
        return struct.unpack_from("<H", self.d, self.take(2))[0]

    def s16(self) -> int:
        return struct.unpack_from("<h", self.d, self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.d, self.take(4))[0]

    def s32(self) -> int:
        return struct.unpack_from("<i", self.d, self.take(4))[0]

    def rest(self) -> bytes:
        p, self.p = self.p, self.end
        return self.d[p:self.end]

    def varint(self) -> int:
        """libwpg's readVariableLengthInteger: a byte up to 0xFE, else a u16, or with its top bit set the low 15 bits
        of it and a second u16 (a 31-bit value)."""
        b = self.u8()
        if b <= 0xFE:
            return b
        v = self.u16()
        if v >> 15:
            return ((v & 0x7FFF) << 16) + self.u16()
        return v


def _head(data: bytes) -> tuple[int, int]:
    """(where the records start, the major version) of a WPC-headed WPG, as WPGraphics::parse picks them."""
    n = len(data)
    if n < _HEAD or data[:4] != _MAGIC:
        raise ReadError("damaged", "not a WPG")

    def check(at: int) -> tuple[int, int]:
        if at + 26 > n or data[at:at + 4] != _MAGIC:
            raise ReadError("damaged", "not a WPG")
        doc, = struct.unpack_from("<I", data, at + 4)
        ftype, major, enc = data[at + 9], data[at + 10], struct.unpack_from("<H", data, at + 12)[0]
        if ftype != _WPG_TYPE:
            raise ReadError("damaged", "not a WPG")
        if enc:
            raise ReadError("damaged", "encrypted WPG")
        if major not in (1, 2):
            raise ReadError("damaged", "unknown WPG version")
        return doc, major

    if n < 26:
        raise ReadError("damaged", "not a WPG")
    doc, major = check(0)
    if doc < _HEAD or doc >= n:
        raise ReadError("damaged", "WPG document pointer")
    if major == 1 and data[doc:doc + 4] == _MAGIC:
        try:
            doc2, major2 = check(doc)
        except ReadError:
            pass
        else:
            doc, major = doc2 + _HEAD, major2    # libwpg's own dump put a head before a whole file
            if doc >= n:
                raise ReadError("damaged", "WPG document pointer")
    return doc, major


def _ext(cset: int, code: int, wp5: bool) -> str | None:
    """An extended character; set 0 is ASCII from 0x20."""
    if cset == 0:
        return char(0, code - 0x20) if code >= 0x20 else None
    return char(cset, code, wp5=wp5)


def _lines6(b: bytes) -> list[str]:
    """The lines of a WordPerfect 6 text stream: ASCII, the international bytes, the space and paragraph function
    bytes, the end-of-line group and the extended-character group [F0 char set F0]; other groups are stepped over."""
    out, cur = [], []
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if c < 0x80:
            if 0x20 <= c < 0x7F:
                cur.append(chr(c))
            elif 1 <= c <= 0x1F:
                cur.append(_INTL[c - 1] if c <= len(_INTL) else "")
            i += 1
        elif c < 0xD0:
            if c in _PARA6:
                out.append("".join(cur))
                cur = []
            elif c in _SPACE6:
                cur.append(" ")
            elif c == 0x84:
                cur.append("-")
            i += 1
        elif c == 0xFF:
            i += 1
        elif c < 0xF0:                                  # [id][sub][u16 size] ... [u16 size][id]
            size = struct.unpack_from("<H", b, i + 2)[0] if i + 4 <= n else 0
            if size < 7 or i + size > n or b[i + size - 1] != c:
                i += 1
                continue
            sub = b[i + 1]
            if c == 0xE0 or (c == 0xD0 and sub in _EOL_SPACE):
                cur.append(" ")
            elif c == 0xD0 and sub in _EOL_PARA:
                out.append("".join(cur))
                cur = []
            i += size
        else:
            size = _FIXED6[c - 0xF0] if c < 0xFF else 1
            if i + size > n or b[i + size - 1] != c:
                i += 1
                continue
            if c == 0xF0:
                ch = _ext(b[i + 2], b[i + 1], False)
                if ch:
                    cur.append(ch)
            i += size
    out.append("".join(cur))
    return out


def _lines5(b: bytes) -> list[str]:
    """The lines of a WordPerfect 5 text stream: ASCII, the hard returns, the spaces and hyphens, and the
    extended-character group [C0 char set C0]; other groups [Cx ... Cx] are stepped over."""
    out, cur = [], []
    i, n = 0, len(b)
    while i < n:
        c = b[i]
        if 0x20 <= c < 0x7F:
            cur.append(chr(c))
            i += 1
        elif 0xC0 <= c <= 0xCF:
            size = _FIXED5[c - 0xC0]
            if i + size > n or b[i + size - 1] != c:
                i += 1
                continue
            if c == 0xC0:
                ch = _ext(b[i + 2], b[i + 1], True)
                if ch:
                    cur.append(ch)
            i += size
        else:
            if c in _PARA5:
                out.append("".join(cur))
                cur = []
            elif c in _SPACE5:
                cur.append(" ")
            elif c in _HYPHEN5:
                cur.append("-")
            i += 1
    out.append("".join(cur))
    return out


def _decode1(b: bytes) -> str:
    """WPG 1 graphics text type 1: ASCII, and cp437 for the bytes above it."""
    return "".join(chr(c) if 0x20 <= c < 0x7F else (bytes((c,)).decode("cp437") if c >= 0x80 else " ") for c in b)


def _map(segs: list, f) -> list:
    """The segments with every (x, y) pair sent through f."""
    out = []
    for s in segs:
        if s[0] == "Z":
            out.append(s)
            continue
        pts: list = []
        for i in range(1, len(s), 2):
            pts += f(s[i], s[i + 1])
        out.append((s[0], *pts))
    return out


def _oval(cx, cy, rx, ry, start=0.0, sweep=360.0) -> list:
    """An elliptical arc (a whole ellipse by default) from its start point, in y-up coordinates, counterclockwise."""
    a = math.radians(start)
    segs = [("M", cx + rx * math.cos(a), cy + ry * math.sin(a))] + arc_beziers(cx, cy, rx, ry, start, sweep)
    return segs + ([("Z",)] if abs(sweep) >= 360.0 else [])


def _round_rect(x0, y0, x1, y1, rx, ry) -> list:
    """A rectangle in y-up coordinates, its corners cut by arcs of radii rx and ry (clamped to the box)."""
    rx, ry = min(abs(rx), abs(x1 - x0) / 2), min(abs(ry), abs(y1 - y0) / 2)
    if rx <= 0 or ry <= 0:
        return [("M", x0, y0), ("L", x1, y0), ("L", x1, y1), ("L", x0, y1), ("Z",)]
    out = [("M", x0 + rx, y0), ("L", x1 - rx, y0)]
    out += arc_beziers(x1 - rx, y0 + ry, rx, ry, -90, 90) + [("L", x1, y1 - ry)]
    out += arc_beziers(x1 - rx, y1 - ry, rx, ry, 0, 90) + [("L", x0 + rx, y1)]
    out += arc_beziers(x0 + rx, y1 - ry, rx, ry, 90, 90) + [("L", x0, y0 + ry)]
    out += arc_beziers(x0 + rx, y0 + ry, rx, ry, 180, 90) + [("Z",)]
    return out


_TAB4: list = []


def _tab4() -> list:
    """The BGR bytes of each 4-4-4-4 pixel word (red in the low nibble, as libwpg reads it), built once."""
    if not _TAB4:
        for v in range(65536):
            r, g, b = v & 15, (v >> 4) & 15, (v >> 8) & 15
            _TAB4.append(bytes((b * 17, g * 17, r * 17)))
    return _TAB4


# the bytes a row of each WPG 2 bitmap format takes, from its width: formats 1-4 are palette indexes, 5 grey, 6 5-5-5,
# 7 4-4-4-4, 8 RGB, 9 RGBA, 12 grey; 10 and 11 (48 and 64 bits) and 0 are not drawn
_ROW2 = {1: lambda w: (w + 7) // 8, 2: lambda w: (w + 3) // 4, 3: lambda w: (w + 1) // 2, 4: lambda w: w,
         5: lambda w: 2 * w, 6: lambda w: 2 * w, 7: lambda w: 2 * w, 8: lambda w: 3 * w, 9: lambda w: 4 * w,
         12: lambda w: w}
_BITS = {1: 1, 2: 2, 3: 4, 4: 8}


def _bgr_body(budget, buf: bytes, w: int, h: int, rb: int, fmt: str, bits: int, pal: list, hflip=False,
              vflip=False) -> bytes:
    """The pixels of `buf` (rb bytes a row, h rows, at least rb * h bytes) as the body of a bottom-up 24-bit DIB:
    BGR rows of 3 * w bytes padded to four. One output buffer, filled a row at a time, so memory is the picture's own
    size; a mirrored picture has its columns (hflip) or rows (vflip) reversed. fmt is "idx" (bits a pixel, through `pal`), "g16", "g8", "555", "4444", "rgb" or "rgba"."""
    stride = 3 * w + (-3 * w) % 4
    body = bytearray(stride * h)
    if fmt == "idx":
        per = 8 // bits
        mask = (1 << bits) - 1
        expand = [bytes((v >> (8 - bits - i * bits)) & mask for i in range(per)) for v in range(256)]
        tb, tg, tr = (bytes((pal[i] >> s) & 0xFF for i in range(256)) for s in (0, 8, 16))
    elif fmt == "555":
        tab = _tab16()
    elif fmt == "4444":
        tab = _tab4()
    for y in range(h):
        row = buf[y * rb:(y + 1) * rb]
        ba = bytearray(3 * w)
        if fmt == "idx":
            idx = b"".join(map(expand.__getitem__, row))[:w] if bits < 8 else row[:w]
            ba[0::3], ba[1::3], ba[2::3] = idx.translate(tb), idx.translate(tg), idx.translate(tr)
        elif fmt == "g16":
            ba[0::3] = ba[1::3] = ba[2::3] = row[1:2 * w:2]
        elif fmt == "g8":
            ba[0::3] = ba[1::3] = ba[2::3] = row[:w]
        elif fmt in ("555", "4444"):
            ba = bytearray(b"".join(map(tab.__getitem__, struct.unpack("<%dH" % w, row[:2 * w]))))
        elif fmt == "rgb":
            ba[0::3], ba[1::3], ba[2::3] = row[2:3 * w:3], row[1:3 * w:3], row[0:3 * w:3]
        else:
            ba[0::3], ba[1::3], ba[2::3] = row[2:4 * w:4], row[1:4 * w:4], row[0:4 * w:4]
        if hflip:
            ba[0::3], ba[1::3], ba[2::3] = ba[0::3][::-1], ba[1::3][::-1], ba[2::3][::-1]
        at = (y if vflip else h - 1 - y) * stride       # the DIB is bottom-up
        body[at:at + 3 * w] = ba
        budget.points(0)                                # the deadline is read here, once in CLOCK_EVERY calls
    return bytes(body)


def _body_png(body: bytes, w: int, h: int) -> bytes | None:
    """The body of a 24-bit bottom-up DIB as a PNG, through images.bmp_to_png."""
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 0, 0, 0, 0) + body
    bmp = dib_to_bmp(dib)
    return images.bmp_to_png(bmp) if bmp is not None else None


class _Wpg:
    """What the two parsers share: the drawing under construction, and the one place the budget is charged."""

    def __init__(self, data: bytes, start: int, budget: Budget):
        self.d = data
        self.start = start
        self.budget = budget
        self.items: list = []
        self.skipped = 0
        self.damaged = False
        self.frame = None
        self.dpi = 1200.0
        self.started = False

    def _charge(self, segs=None):
        """The one place METAFILE_POINTS is charged: every item as it goes out, kept or dropped. A path costs its
        points (a close counts as one), a run or a bitmap ITEM_WEIGHT."""
        self.budget.points(ITEM_WEIGHT if segs is None else sum((len(s) - 1) // 2 or 1 for s in segs))

    def _path(self, segs, pen, brush, even_odd=False, charge=True):
        if charge:
            self._charge(segs)
        if pen is None and brush is None:
            return
        if all(math.isfinite(v) for s in segs for v in s[1:]):
            self.items.append(Path(segs, pen, brush, even_odd))
        else:
            self.skipped += 1

    def _run(self, run: Run):
        self._charge()
        if all(map(math.isfinite, (run.x, run.y, run.height, run.angle, run.width or 0.0))):
            self.items.append(run)
        else:
            self.skipped += 1

    def _emit_bitmap(self, data: bytes, kind: str, box: tuple):
        self._charge()
        if all(map(math.isfinite, box)):
            self.items.append(Bitmap(data, kind, box))
        else:
            self.skipped += 1

    def _drop(self):
        """A picture or bitmap not drawn: charged like a kept one, and counted."""
        self._charge()
        self.skipped += 1

    def _finish(self) -> Drawing:
        if not self.started or self.frame is None:
            raise ReadError("damaged", "no Start WPG record")
        frame = self.frame
        if not all(map(math.isfinite, (*frame, self.dpi))) or self.dpi <= 0:
            frame, self.dpi = (0.0, 0.0, 1.0, 1.0), 1200.0
        return Drawing(frame, self.dpi, self.items, self.damaged, self.skipped)

    def _bitmap_png(self, buf, w, h, rb, fmt, bits, pal, hflip=False, vflip=False) -> bytes | None:
        return _body_png(_bgr_body(self.budget, buf, w, h, rb, fmt, bits, pal, hflip, vflip), w, h)


# ---- WPG 1 ------------------------------------------------------------------------------------------------------------

class _Wpg1(_Wpg):
    def __init__(self, data, start, budget):
        super().__init__(data, start, budget)
        self.pal = list(_PAL1_RGB)
        self.W = self.H = 0
        self.pen_on, self.pen_color, self.pen_w = True, 0, None     # a hairline until a Line Attributes record
        self.fill_on, self.fill_color = True, 0
        self.avg = self.fsize = self.align = 0
        self.fcolor = 0
        self.handlers = {0x01: self._fill, 0x02: self._line_attr, 0x05: self._line, 0x06: self._polyline,
                         0x07: self._rect, 0x08: self._polygon, 0x09: self._ellipse, 0x0B: self._bitmap1,
                         0x0C: self._text1, 0x0D: self._text_attr, 0x0E: self._colormap, 0x13: self._curve,
                         0x14: self._bitmap2, 0x18: self._text2}

    def run(self) -> Drawing:
        d, n = self.d, len(self.d)
        pos = self.start
        while pos < n:
            r = _R(d, pos, n)
            try:
                typ = r.u8()
                if typ == 0:
                    break
                length = r.varint()
            except _Short:
                self.damaged = True
                break
            end = r.p + length
            if end > n:
                self.damaged = True         # the record runs past the file
                break
            self.budget.tick()
            rec = _R(d, r.p, end)
            pos = end
            if typ == 0x10:
                break
            if typ == 0x0F:
                if self.started:
                    break                   # a second Start WPG is damage: the picture ends
                try:
                    self._start(rec)
                except _Short:
                    self.damaged = True
                continue
            if not self.started:
                self.skipped += 1
                continue
            h = self.handlers.get(typ)
            if h is None:
                if typ not in _QUIET1:      # unknown records, markers, PostScript, charts
                    self.skipped += 1
                continue
            try:
                h(rec)
            except _Short:
                self.damaged = True
        return self._finish()

    # state
    def _start(self, rec: _R):
        rec.skip(2)
        self.W, self.H = rec.u16(), rec.u16()
        self.frame = (0.0, 0.0, float(max(self.W, 1)), float(max(self.H, 1)))
        self.started = True

    def _pen(self) -> Pen | None:
        return Pen(self.pen_color, self.pen_w or self.dpi / 96.0) if self.pen_on else None

    def _brush(self) -> Brush | None:
        return Brush(self.fill_color) if self.fill_on else None

    def _fill(self, rec: _R):
        style, color = rec.u8(), rec.u8()
        if style == 0:
            self.fill_on = False
        elif style == 1:
            self.fill_on = True
        self.fill_color = self.pal[color]

    def _line_attr(self, rec: _R):
        style, color, width = rec.u8(), rec.u8(), rec.u16()
        if style >= 16:
            style = 1
        self.pen_on = bool(style and width)
        self.pen_color = self.pal[color]
        self.pen_w = float(width)

    def _colormap(self, rec: _R):
        start, num = rec.u16(), rec.u16()
        if start > 255 or num > 256 or start + num > 256:
            return
        for i in range(min(num, rec.left() // 3)):
            self.pal[start + i] = rec.u8() << 16 | rec.u8() << 8 | rec.u8()

    # shapes (WPG y points up: y -> H - y)
    def _pt(self, rec: _R) -> tuple:
        x, y = rec.s16(), rec.s16()
        return float(x), float(self.H - y)

    def _line(self, rec: _R):
        a, b = self._pt(rec), self._pt(rec)
        self._path([("M", *a), ("L", *b)], self._pen(), None)

    def _points(self, rec: _R) -> list:
        count = min(rec.u16(), rec.left() // 4)
        return [self._pt(rec) for _ in range(count)]

    def _polyline(self, rec: _R):
        pts = self._points(rec)
        if pts:
            self._path([("M", *pts[0])] + [("L", *p) for p in pts[1:]], self._pen(), None)

    def _polygon(self, rec: _R):
        pts = self._points(rec)
        if pts:
            self._path([("M", *pts[0])] + [("L", *p) for p in pts[1:]] + [("Z",)], self._pen(), self._brush())

    def _rect(self, rec: _R):
        x, y, w, h = rec.s16(), rec.s16(), rec.s16(), rec.s16()
        y0, y1 = self.H - y - h, self.H - y
        self._path([("M", x, y0), ("L", x + w, y0), ("L", x + w, y1), ("L", x, y1), ("Z",)], self._pen(),
                   self._brush())

    def _ellipse(self, rec: _R):
        cx, cy, rx, ry, rot = rec.s16(), rec.s16(), rec.s16(), rec.s16(), rec.s16()
        a = math.radians(rot)
        ca, sa = math.cos(a), math.sin(a)

        def f(x, y):
            dx, dy = x - cx, y - cy
            return [cx + dx * ca - dy * sa, self.H - (cy + dx * sa + dy * ca)]
        self._path(_map(_oval(cx, cy, abs(rx), abs(ry)), f), self._pen(), self._brush())

    def _curve(self, rec: _R):
        rec.skip(4)
        pts = self._points(rec)
        if len(pts) >= 4:
            segs = [("M", *pts[0])]
            for i in range(1, len(pts) - 2, 3):
                segs.append(("C", *pts[i], *pts[i + 1], *pts[i + 2]))
            self._path(segs, self._pen(), None)

    # text
    def _text_attr(self, rec: _R):
        avg, size = rec.u16(), rec.u16()
        rec.skip(13)
        align = rec.u8()
        rec.skip(1)
        color = rec.u8()
        self.avg, self.fsize, self.align = avg, size, align if align <= 2 else 0
        self.fcolor = self.pal[color]

    def _text1(self, rec: _R):
        n, x, y = rec.u16(), rec.s16(), rec.s16()
        raw = rec.d[rec.p:rec.p + min(n, rec.left())]
        text = _decode1(raw)
        if not text.strip():
            return
        width = len(raw) * self.avg if self.avg > 0 else None
        shift = 0 if width is None else (0, width / 2, width)[self.align]
        self._run(Run(text, float(x - shift), float(self.H - y), _FACE, float(self.fsize or self.dpi / 6.0),
                      color=self.fcolor, width=None if width is None else float(width)))

    def _text2(self, rec: _R):
        rec.skip(4)
        rot, n = rec.s16(), rec.u16()
        x1, y1 = rec.s16(), rec.s16()
        rec.skip(9)                                     # the end point, the scale and the type
        lines = _lines5(rec.d[rec.p:rec.p + min(n, rec.left())])
        h = float(self.fsize or self.dpi / 6.0)
        a = math.radians(rot)
        for i, line in enumerate(lines):
            if line.strip():
                k = i * h * _LEADING
                self._run(Run(line, x1 + k * math.sin(a), float(self.H - y1) + k * math.cos(a), _FACE, h,
                              angle=float(rot % 360), color=self.fcolor))

    # bitmaps
    def _rle(self, rec: _R, need: int, scan: int) -> bytes:
        """decodeRLE: opcodes 0x80 | n (a run of n copies of the next byte; n = 0: the next byte is a count of 0xFF),
        n (n literal bytes; n = 0: the next byte repeats the last scanline that many times). At most `need` bytes,
        zero filled when the data ends first."""
        d, p, end = self.d, rec.p, rec.end
        out = bytearray()
        while p < end and len(out) < need:
            op = d[p]
            p += 1
            cnt = op & 0x7F
            if op & 0x80:
                if p >= end:
                    break
                if cnt:
                    px = d[p]
                else:
                    px, cnt = 0xFF, d[p]
                p += 1
                out += bytes((px,)) * min(cnt, need - len(out))
            elif cnt:
                out += d[p:min(p + cnt, end)]
                p += cnt
            else:
                if p >= end or len(out) < scan:
                    break
                reps = min(d[p], -(-(need - len(out)) // scan))
                p += 1
                out += bytes(out[-scan:]) * reps
            self.budget.points(0)
        del out[need:]
        out += bytes(need - len(out))
        return bytes(out)

    def _bitmap_common(self, rec: _R, w: int, h: int, depth: int, hres: int, vres: int, place):
        """The shared part of bitmap types 1 and 2: decode, colour, and place the PNG with place(w, h, hres, vres)."""
        if depth not in (1, 2, 4, 8):
            self._drop()                                # corruption, libwpg bails out
            return
        if hres <= 0:
            hres = 72
        if vres <= 0:
            vres = 72
        w, h = max(w, 0), max(h, 0)
        scan = (w * depth + 7) // 8
        need = scan * h
        if not need or w * h > images._MAX_PIXELS or rec.left() <= 0:
            self._drop()
            return
        buf = self._rle(rec, need, scan)
        if depth == 1:
            pal = [0x000000, 0xFFFFFF] + [0x000000] * 254   # a set bit is white
        else:
            pal = self.pal
        png = self._bitmap_png(buf, w, h, scan, "idx", depth, pal)
        if png is None:
            self._drop()
            return
        self._emit_bitmap(png, "png", place(w, h, hres, vres))

    def _bitmap1(self, rec: _R):
        w, h, depth, hres, vres = rec.s16(), rec.s16(), rec.s16(), rec.s16(), rec.s16()
        # type 1 has no position: libwpg puts it at the corner (0, 0)
        self._bitmap_common(rec, w, h, depth, hres, vres,
                            lambda w, h, hr, vr: (0.0, 0.0, w * 1200.0 / hr, h * 1200.0 / vr))

    def _bitmap2(self, rec: _R):
        rot = rec.s16()
        x1, y1, x2, y2 = rec.s16(), rec.s16(), rec.s16(), rec.s16()
        w, h, depth, hres, vres = rec.s16(), rec.s16(), rec.s16(), rec.s16(), rec.s16()
        if not 0 <= rot <= 359:
            self._drop()
            return
        ya, yb = self.H - y1, self.H - y2
        box = (float(min(x1, x2)), float(min(ya, yb)), float(max(x1, x2)), float(max(ya, yb)))
        self._bitmap_common(rec, w, h, depth, hres, vres, lambda *_: box)


# ---- WPG 2 ------------------------------------------------------------------------------------------------------------

_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)     # a, b, c, d, e, f: x' = a x + c y + e, y' = b x + d y + f


def _compose(inner: tuple, outer: tuple) -> tuple:
    """The matrix that applies `inner`, then `outer`."""
    a, b, c, d, e, f = inner
    pa, pb, pc, pd, pe, pf = outer
    return (pa * a + pc * b, pb * a + pd * b, pa * c + pc * d, pb * c + pd * d, pa * e + pc * f + pe,
            pb * e + pd * f + pf)


class _Char:
    """An object characterization (parseCharacterization)."""
    __slots__ = ("filled", "closed", "framed", "winding", "m", "angle", "hflip", "vflip")

    @staticmethod
    def plain() -> "_Char":
        ch = _Char()
        ch.filled = ch.closed = ch.winding = False
        ch.framed = True
        ch.m, ch.angle = _IDENT, 0.0
        ch.hflip = ch.vflip = False
        return ch


class _Group:
    """A subgroup: the records still to come in it, and for a compound polygon its path and the flags it took."""
    __slots__ = ("parent", "sub", "path", "ch")

    def __init__(self, parent: int, sub: int, ch=None):
        self.parent, self.sub, self.path, self.ch = parent, sub, [], ch


class _Wpg2(_Wpg):
    def __init__(self, data, start, budget):
        super().__init__(data, start, budget)
        self.pal = list(_PAL2_RGB)
        self.xres = self.yres = 1200.0
        self.dp = False
        self.xofs = self.yofs = self.W = self.H = 0.0
        self.ky = 1.0
        self.pen_color, self.pen_w, self.pen_on = 0, None, True
        self.brush_color, self.brush_on = 0, True
        self.stack: list[_Group] = []
        self.cp = None                  # the last compound polygon's characterization
        self.bm = None                  # (box, hflip, vflip) of the last Bitmap record
        self.text = None                # (x, y, angle, block) of the last text line or block
        self.cap = None                 # the last Object Capsule: [box, kinds, next index]
        self.handlers = {0x0C: self._palette, 0x0D: self._palette_dp, 0x0E: self._bitmap_data, 0x0F: self._text_data,
                         0x12: self._object_image, 0x15: self._polyline, 0x16: self._polyline, 0x17: self._polycurve,
                         0x18: self._rect, 0x19: self._arc, 0x1A: self._compound, 0x1B: self._bitmap_rec,
                         0x1C: self._text_line, 0x1D: self._text_block, 0x1E: self._text_path, 0x21: self._capsule,
                         0x25: self._pen_fore, 0x26: self._pen_fore_dp, 0x2B: self._pen_size, 0x2C: self._pen_size_dp,
                         0x31: self._brush_fore, 0x32: self._brush_fore_dp}

    def run(self) -> Drawing:
        d, n = self.d, len(self.d)
        pos = self.start
        while pos < n:
            r = _R(d, pos, n)
            try:
                r.u8()                                  # flags
                typ = r.u8()
                if typ == 0 or typ > 0x3F:
                    break
                ext = r.varint()
                length = r.varint()
            except _Short:
                self.damaged = True
                break
            end = r.p + length
            if end > n:
                self.damaged = True
                break
            self.budget.tick()
            rec = _R(d, r.p, end)
            pos = end
            if typ == 0x02:
                break
            if typ == 0x01:
                if self.started:
                    break                               # a second Start WPG is damage: the picture ends
            elif not self.started:
                self.skipped += 1
                continue
            if self.stack:
                self.stack[-1].sub -= 1
            h = self._start if typ == 0x01 else self.handlers.get(typ)
            try:
                if h is not None:
                    h(rec)
                elif typ not in _QUIET2:
                    self.skipped += 1
            except _Short:
                self.damaged = True
            if self.stack and self.stack[-1].sub <= 0:
                g = self.stack.pop()
                if g.parent == 0x1A:
                    self._flush(g)
            if ext > 0:
                if len(self.stack) >= limits.MAX_DEPTH:
                    self.damaged = True
                    break
                self.stack.append(_Group(typ, ext, (self.cp or _Char.plain()) if typ == 0x1A else None))
        return self._finish()

    # the page
    def _num(self, rec: _R) -> float:
        return rec.s32() / 65536.0 if self.dp else float(rec.s16())

    def _start(self, rec: _R):
        hu, vu, prec = rec.u16(), rec.u16(), rec.u8()
        if prec not in (0, 1):
            raise ReadError("damaged", "WPG precision")
        self.xres, self.yres = (float(hu), float(vu)) if hu and vu else (1200.0, 1200.0)
        self.dp = prec == 1
        rec.skip(16 if self.dp else 8)                  # the viewport
        x1, y1, x2, y2 = (self._num(rec) for _ in range(4))
        self.xofs, self.yofs = min(x1, x2), min(y1, y2)
        self.W, self.H = abs(x2 - x1), abs(y2 - y1)
        self.ky = self.xres / self.yres
        self.dpi = self.xres
        self.frame = (0.0, 0.0, max(self.W, 1.0), max(self.H * self.ky, 1.0))
        self.started = True

    def _t(self, m: tuple, x: float, y: float) -> list:
        """A point through the matrix and into the frame: libwpg's transformXY with y flipped and scaled."""
        a, b, c, d, e, f = m
        return [a * x + c * y + e - self.xofs, (self.H - (b * x + d * y + f - self.yofs)) * self.ky]

    def _pt(self, rec: _R, m: tuple) -> list:
        x = self._num(rec)
        return self._t(m, x, self._num(rec))

    def _char(self, rec: _R) -> _Char:
        """parseCharacterization: the flags and the transform they ask for, then the inside a compound adds."""
        ch = _Char()
        flags = rec.u16()
        ch.filled, ch.closed, ch.framed = bool(flags & 0x2000), bool(flags & 0x4000), bool(flags & 0x8000)
        ch.winding = bool(flags & 0x1000)
        taper, translate, skew, scale, rotate = (bool(flags & v) for v in (1, 2, 4, 8, 0x10))
        if flags & 0x80:
            rec.skip(4)                                 # the lock flags
        oid = rec.u16() if flags & 0x20 else 0
        if oid >> 15:
            rec.skip(2)                                 # an object id in 4 bytes
        angle = 0.0
        if rotate:
            rec.skip(4)                                 # the angle, which the cosines and sines already carry
        a = d = 1.0
        b = c = 0.0
        if rotate or scale:
            a, d = rec.s32() / 65536.0, rec.s32() / 65536.0
        if rotate or skew:
            c, b = rec.s32() / 65536.0, rec.s32() / 65536.0
        e = f = 0.0
        if translate:
            fx, ix, fy, iy = rec.u16(), rec.s32(), rec.u16(), rec.s32()
            e, f = ix + fx / 65536.0, iy + fy / 65536.0
        if taper:
            rec.skip(8)
        m = (a, b, c, d, e, f)
        ch.hflip, ch.vflip = bool(scale and a < 0), bool(scale and d < 0)
        if not all(map(math.isfinite, m)) or a * d - b * c == 0:
            m, ch.hflip, ch.vflip = _IDENT, False, False   # a matrix that squashes the item flat: the identity
            self.skipped += 1
        if self._in_compound():
            m = _compose(m, self.stack[-1].ch.m)        # inside a compound: its own transform goes on top
        if m[0] or m[1]:
            angle = math.degrees(math.atan2(m[1], m[0]))
        ch.m, ch.angle = m, angle
        return ch

    def _in_compound(self) -> bool:
        return bool(self.stack) and self.stack[-1].parent == 0x1A

    def _style_ok(self) -> bool:
        """False while the open group is a compound polygon or the page attributes of Start WPG."""
        return not self.stack or self.stack[-1].parent not in (0x1A, 0x01)

    def _pen(self, ch: _Char) -> Pen | None:
        if not ch.framed or not self.pen_on:
            return None
        return Pen(self.pen_color, self.pen_w or self.dpi / 96.0)

    def _brush(self, ch: _Char) -> Brush | None:
        return Brush(self.brush_color) if ch.filled and self.brush_on else None

    # colours and pens
    def _rgba(self, rec: _R, dp: bool) -> tuple:
        if dp:
            r, g, b, a = (rec.u16() >> 8 for _ in range(4))
        else:
            r, g, b, a = rec.u8(), rec.u8(), rec.u8(), rec.u8()
        return r << 16 | g << 8 | b, 0xFF - a

    def _palette(self, rec: _R, dp=False):
        start, num = rec.u16(), rec.u16()
        for i in range(min(num, rec.left() // (8 if dp else 4))):
            rgb, _alpha = self._rgba(rec, dp)
            if start + i < 256:
                self.pal[start + i] = rgb

    def _palette_dp(self, rec: _R):
        self._palette(rec, True)

    def _pen_fore(self, rec: _R, dp=False):
        if not self._style_ok():
            return
        rgb, alpha = self._rgba(rec, dp)
        self.pen_color, self.pen_on = rgb, alpha != 0

    def _pen_fore_dp(self, rec: _R):
        self._pen_fore(rec, self.dp)

    def _pen_size(self, rec: _R):
        if self._style_ok():
            self.pen_w = float(rec.u16())

    def _pen_size_dp(self, rec: _R):
        if self._style_ok():
            raw = rec.u32()
            self.pen_w = (raw / 65536.0 if self.dp else float(raw)) / 256.0

    def _brush_fore_dp(self, rec: _R):
        self._brush_fore(rec, self.dp)

    def _brush_fore(self, rec: _R, dp=False):
        if not self._style_ok():
            return
        if rec.u8() == 0:
            rgb, alpha = self._rgba(rec, dp)
        else:                                           # a gradient: its first colour stands for it
            count = min(rec.u16(), rec.left() // (8 if dp else 4))
            if not count:
                return
            rgb, alpha = self._rgba(rec, dp)
        self.brush_color, self.brush_on = rgb, alpha != 0

    # shapes
    def _poly_segs(self, pts: list, close: bool) -> list:
        return [("M", *pts[0])] + [("L", *p) for p in pts[1:]] + ([("Z",)] if close else [])

    def _put(self, ch: _Char, segs: list, shape_closed: bool):
        """A shape's path: into the open compound polygon, or drawn."""
        if self._in_compound():
            self._charge(segs)
            self.stack[-1].path += segs
            return
        self._path(segs, self._pen(ch), self._brush(ch) if shape_closed else None, not ch.winding)

    def _polyline(self, rec: _R):
        ch = self._char(rec)
        count = min(rec.u16(), rec.left() // (8 if self.dp else 4))
        pts = [self._pt(rec, ch.m) for _ in range(count)]
        if len(pts) < 2:
            return
        closed = len(pts) > 2 and (ch.filled or ch.closed)
        self._put(ch, self._poly_segs(pts, closed), closed)

    def _polycurve(self, rec: _R):
        ch = self._char(rec)
        count = min(rec.u16(), rec.left() // (24 if self.dp else 12))
        segs: list = []
        prev = None
        for i in range(count):
            ip, ap, tp = self._pt(rec, ch.m), self._pt(rec, ch.m), self._pt(rec, ch.m)
            segs.append(("M", *ap) if i == 0 else ("C", *prev, *ip, *ap))
            prev = tp
        if not segs:
            return
        if ch.closed:
            segs.append(("Z",))
        self._put(ch, segs, ch.closed or ch.filled)

    def _rect(self, rec: _R):
        ch = self._char(rec)
        x1, y1, x2, y2 = (self._num(rec) for _ in range(4))
        rx, ry = self._num(rec), self._num(rec)
        segs = _map(_round_rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), rx, ry),
                    lambda x, y: self._t(ch.m, x, y))
        self._put(ch, segs, True)

    def _arc(self, rec: _R):
        ch = self._char(rec)
        cx, cy, rx, ry = (self._num(rec) for _ in range(4))
        ix, iy, ex, ey = (self._num(rec) for _ in range(4))
        rx, ry = abs(rx), abs(ry)
        if (ix, iy) == (ex, ey) or not rx or not ry:
            local, closed = _oval(cx, cy, rx, ry), True
        else:
            a0 = math.atan2(iy / ry, ix / rx)
            sweep = (math.atan2(ey / ry, ex / rx) - a0) % (2 * math.pi)
            if sweep < 1e-9:
                sweep = 2 * math.pi
            local = _oval(cx, cy, rx, ry, math.degrees(a0), math.degrees(sweep))
            closed = ch.closed or ch.filled
            if closed and local[-1] != ("Z",):
                local.append(("Z",))
        self._put(ch, _map(local, lambda x, y: self._t(ch.m, x, y)), closed)

    def _compound(self, rec: _R):
        self.cp = None
        self.cp = self._char(rec)

    def _flush(self, g: _Group):
        """A compound polygon's members, drawn as one path when its last member has been read."""
        if not g.path:
            return
        segs = g.path + ([("Z",)] if g.ch.closed else [])
        self._path(segs, self._pen(g.ch), self._brush(g.ch), not g.ch.winding, charge=False)

    # text
    def _text_line(self, rec: _R):
        ch = self._char(rec)
        rec.skip(2)                                     # the text flags
        x, y = self._pt(rec, ch.m)
        rec.skip(2)                                     # the alignments
        self.text = (x, y, rec.s32() / 65536.0 + ch.angle, False)       # the baseline angle is 16.16

    def _text_block(self, rec: _R):
        ch = self._char(rec)
        p1, p2 = self._pt(rec, ch.m), self._pt(rec, ch.m)
        self.text = (min(p1[0], p2[0]), min(p1[1], p2[1]), ch.angle, True)

    def _text_path(self, rec: _R):
        self.text = None
        self.skipped += 1

    def _text_data(self, rec: _R):
        if self.text is None:
            return
        x, y, angle, block = self.text
        self.text = None
        h = self.dpi / 6.0                              # no font size is read: 12 points
        a = math.radians(angle)
        for i, line in enumerate(_lines6(rec.rest())):
            if line.strip():
                k = i * h * _LEADING + (h if block else 0.0)    # a block's first baseline is an em below its top
                self._run(Run(line, x + k * math.sin(a), y + k * math.cos(a), _FACE, h, angle=angle,
                              color=self.brush_color))

    # bitmaps
    def _bitmap_rec(self, rec: _R):
        ch = self._char(rec)
        p1, p2 = self._pt(rec, ch.m), self._pt(rec, ch.m)
        self.bm = ((min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])), ch.hflip, ch.vflip)

    def _rle(self, rec: _R, need: int, raster: int, width: int) -> bytes:
        """handleBitmapData's RLE: 0x7D sets the bytes per pixel, 0x7F and 0xFF are runs of 0 and 255, 0xFD repeats
        the last run, 0xFE the last raster, 0x7E xors the next raster with the one before, 0x80 | n is a run of the
        next pixel, n a copy of n + 1 pixels. At most `need` bytes, zero filled when the data ends first."""
        d, p, end = self.d, rec.p, rec.end
        out = bytearray()
        size = 1
        last = bytes(1)
        xor_at = 0
        while p < end and len(out) < need:
            op = d[p]
            p += 1
            if op == 0x7D:
                if p >= end:
                    break
                size, p = max(d[p], 1), p + 1
                raster = size * width
                last = bytes(size)
            elif op in (0x7F, 0xFF, 0xFD):
                if p >= end:
                    break
                cnt = 1 + d[p]
                p += 1
                unit = bytes((0 if op == 0x7F else 255,)) * size if op != 0xFD else last
                out += unit * min(cnt, -(-(need - len(out)) // size))
            elif op == 0xFE:
                if p >= end or raster <= 0 or len(out) < raster:
                    break
                cnt = 1 + d[p]
                p += 1
                out += bytes(out[-raster:]) * min(cnt, -(-(need - len(out)) // raster))
            elif op == 0x7E:
                xor_at = len(out) + raster
            elif op >= 0x80:
                if p >= end:
                    break
                last = d[p:min(p + size, end)].ljust(size, b"\0")
                p += size
                out += last * min(1 + (op & 0x7F), -(-(need - len(out)) // size))
            else:
                take = min((op + 1) * size, end - p)
                out += d[p:p + take]
                p += take
            if xor_at and len(out) >= xor_at:           # the raster just finished is xored with the one before it
                cur = xor_at - raster
                if raster > 0 and cur - raster >= 0:
                    a = int.from_bytes(out[cur:xor_at], "big") ^ int.from_bytes(out[cur - raster:cur], "big")
                    out[cur:xor_at] = a.to_bytes(raster, "big")
                xor_at = 0
            self.budget.points(0)
        del out[need:]
        out += bytes(need - len(out))
        return bytes(out)

    def _bitmap_data(self, rec: _R):
        w, h, fmt, comp = rec.u16(), rec.u16(), rec.u8(), rec.u8()
        if fmt > 12 or fmt not in _ROW2 or self.bm is None:
            self._drop()
            return
        box, hflip, vflip = self.bm
        rb = _ROW2[fmt](w)
        need = rb * h
        avail = rec.left()
        if (not w or not h or w * h > images._MAX_PIXELS or need > images._MAX_PIXELS * 4
                or (comp == 0 and need > avail) or comp > 1):
            self._drop()
            return
        buf = rec.rest()[:need] if comp == 0 else self._rle(rec, need, rb, w)
        if fmt in _BITS:
            png = self._bitmap_png(buf, w, h, rb, "idx", _BITS[fmt], self.pal, hflip, vflip)
        else:
            png = self._bitmap_png(buf, w, h, rb, {5: "g16", 6: "555", 7: "4444", 8: "rgb", 9: "rgba", 12: "g8"}[fmt], 8,
                                   self.pal, hflip, vflip)
        if png is None:
            self._drop()
            return
        self._emit_bitmap(png, "png", box)

    def _capsule(self, rec: _R):
        ch = self._char(rec)
        p1, p2 = self._pt(rec, ch.m), self._pt(rec, ch.m)
        box = (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))
        kinds = []
        for _ in range(min(rec.u16(), rec.left() // 8)):
            desc = rec.u8()
            rec.skip(7)
            if desc < 0x27:
                kinds.append(_OBJ_MIME.get(desc, ""))
        self.cap = [box, kinds, 0]

    def _object_image(self, rec: _R):
        if self.cap is None or self.cap[2] >= len(self.cap[1]):
            return
        box, kinds, i = self.cap
        self.cap[2] += 1
        rec.skip(rec.u16())                             # the accessory data
        data = rec.rest()
        kind = kinds[i]
        if kind == "png" and data.startswith(_PNG):
            pw, ph = struct.unpack_from(">II", data, 16) if len(data) >= 24 and data[12:16] == b"IHDR" else (0, 0)
            if 0 < pw * ph <= images._MAX_PIXELS:       # the size in its IHDR is checked, not trusted
                self._emit_bitmap(data, "png", box)
            else:
                self._drop()
        elif kind == "bmp" and (png := images.bmp_to_png(data)) is not None:
            self._emit_bitmap(png, "png", box)
        else:
            self._drop()                                # another format, or bytes that are not the one named


def parse(data: bytes, deadline: float | None = None) -> Drawing:
    """The drawing of a WPG with its 16-byte WPC head. ReadError("damaged", ...) when the bytes are not one or have
    no Start WPG record. Past `deadline` (a time.monotonic() value) the figure stops with ReadError("too_large",
    "figure time"); past a size cap the drawing read so far is kept, marked damaged."""
    data = bytes(data)
    start, major = _head(data)
    p = (_Wpg1 if major == 1 else _Wpg2)(data, start, Budget(deadline=deadline, size=len(data)))
    return kept(p, p._finish)
