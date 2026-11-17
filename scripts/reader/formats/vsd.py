# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libvisio a42aaff13e4a67afcd077c3abb3ef13bb6463025, src/lib/VSDParser.cpp, VSD6Parser.cpp, VSD5Parser.cpp, VSDStencils.cpp, VSDFieldList.cpp, VSDContentCollector.cpp.
"""Visio 2002-2013 (version 11), Visio 2000 (version 6) and Visio 5 drawings (vsd, vst) to the document model: one
section per page, each shape's text and picture together in reading order, then the page's connection list, as vsdx
gives them.

Ported from libvisio (MPL-2.0) at a42aaff13e4a67afcd077c3abb3ef13bb6463025, `src/lib/`: `VSDParser.cpp`
(`handleStream`'s page, stencil and shape context, `_handleLevelChange`, `readShape`, `readXFormData`, `readText`,
`readName`, `readName2`, `readNameIDX`, `readNameIDX123`, `readFieldList`, `readTextField`, `readMisc`, `readFontIX`,
`readForeignDataType`, `readForeignData`), `VSD6Parser.cpp` (the 8-bit `readText`, `readName`, `readName2`,
`readTextField`, `readCharIX` and the v6 `readMisc`), `VSD5Parser.cpp` (`handleChunkRecords`, `readShape`,
`readNameIDX`, `readTextField`, `readCharIX`, `readMisc`, the s16 `getUInt`), `VSDStencils.cpp` (`getStencilShape`,
the stencil pages and their shapes by id), `VSDFieldList.cpp` (`VSDFieldList::handle`'s order, the number and
date formats of `VSDNumericField::getString`) and `VSDContentCollector.cpp` (a field mark taking the next field's
value, the character runs of 8-bit text). The walk of the VisioDocument stream is `vsd_stream`'s; this module reads
one walk of it.

Context. As libvisio, by levels: a stream type 0x15 (outside the stencils, 0x1D) opens a page and its leave closes it,
background pages (format bit 0 clear) included. A chunk 0x48, 0x47 or 0x4E in a page, or in a stencil page (0x1E),
starts a shape; the chunks and streams deeper than it are the shape's, and the next step at its level or above ends
it. Its id is the chunk's, else its stream's pointer index; readShape gives its parent and master: v6 and v11 the
u32 at 10, the master page u32 at 18 and the master shape u32 at 26; v5 s16s at 2, 6 and 8, widened as getUInt does
(0xFFFFFFFF: none).

Visio 5 (VSD5Parser): 16-byte pointers and 12-byte chunks (`vsd_stream`); a list chunk (0x32, 0x65, 0x66, 0x68, 0x69,
0x6A, 0x6B, 0x6C) holds records (handleChunkRecords: a record count u16 and the end offset u16 at its end, before them a
table of (type u16, offset u16); a record runs from its offset, 4-aligned, to the previous one's offset), each read as
a chunk of its type, id its place in offset order, one level deeper. Records that are shapes or lists themselves are
not read (no recursion). A name list 0x32 starts a new name map, as in v6 (libvisio's v5 keeps adding to the old one).
Then v5 is read as v6, with its own layouts: the name index 0xC9 (u16 count, then u16 name id and u16 element id),
readName2's s16 skip, the text field (the cell type at 3, a name id s16 or a double at 4, its format unknown, so a
number field shows nothing, as libvisio), the CharIX run (u16 count, u16 font) and readMisc (the flags only: no glued
ends, so no v5 connector is listed).

Content of a shape: 0x9B gives PinX, PinY, LocPinX and LocPinY (a unit byte before each double); 0x0E is the text, 8
bytes skipped then UTF-16LE (v11) or 8-bit (v5, v6); 0x2C and 0x2D its names, 0x66 the field order and 0xA1 its fields
(a string field shows the shape name it points at, a number field its value in its format, found from 0x36 (v11) or
0x24 (v6) of the chunk); each field mark (U+FFFC, or the byte 0x1E in 8-bit text) takes the next field's value.
readFieldList reads only when the chunk header has a trailer; `vsd_stream` gives 0x66 chunks a trailer in v6 and v11,
so here it always reads. 8-bit text is decoded run by run (0x94 CharIX: its character count and font id; a count of
0 still covers one character, as the collector's loop does; past the runs the last one goes on): a font (0x19
FontIX, by chunk id, the last of an id winning) names its charset in the low byte at 2, and a charset of 0 is found
from the font name's suffix (" CE", " Cyrillic", ...: readFontIX). CHARSET_LID maps the charset to a codec (decision
8); any other charset, and text with no run, is 0x0409 (cp1252). libvisio decodes the bytes before a run boundary in
the next run's charset; here each run's bytes are its own.

0xA4 (readMisc) gives the hidden-text flag (0x20 of its first byte) and, from 45 (v11) or 23 (v6), blocks of a u32
length: a glue block (type 2 at 4, 0x74 at 6, 0x6000004E at 7, the shape id at 11, 0x7A at 15, 0x40000073 at 16)
gives the connector's begin id, the next one its end id. Probed on the bench samples: the glued connectors' misc
chunks hold their blocks at these offsets, inside the chunk's data; libvisio may read into the chunk's trailer, here
the blocks stop at the data's end.

Masters. The first stencil list (0x1D) is read (libvisio's `m_stencils.count()`: one with a stencil page read ends the
reading of later ones); each stencil page (0x1E) is keyed by its pointer index, up to `vsdx.MASTERS` pages, and its
shapes by id (the last of an id wins; the first shape read is the page's first shape). A stencil shape's text is read
as a page shape's, fields and runs included, once, and charged to a stencil room of `limits.MAX_OUTPUT` characters a
document (past it the shape is lost, and the shapes that would inherit it count `stencil`). A shape naming a master
page and shape (getStencilShape) takes its text when it has no text chunk of its own, its hidden flag when it has no
misc chunk of its own, and its XForm (PinX, PinY, LocPinX, LocPinY) when it has none of its own; v5 copies the text
only, as VSD5Parser::readShape. A master shape id of 0xFFFFFFFF names the master page's first shape: that is vsdx's
rule (the master's first top-level shape) and libvisio's for vsdx (`m_firstShapeId` is set only by
VSDXMLParserBase); libvisio's binary path never sets it, so there such an instance inherits nothing. Only completed
stencil pages are looked in, so a stencil shape naming its own page, itself, or a page read later inherits nothing, as
in libvisio, uncounted, and no lookup loops. A master page not read (past MASTERS, absent, or after the pages in the
walk) gives nothing, and a page shape that would have inherited its text counts `stencil`; so does one inheriting
through a stencil shape that would itself have inherited from a lost stencil shape or from a page past MASTERS (that
stencil shape is lost too). A master page without that shape gives nothing, uncounted. libvisio adds a stencil shape
when its stream ends; here every shape of a stencil page is added (a group master's sub-shapes too), so a stencil's
first shape is the group, as vsdx's first top-level shape. Pictures are not inherited (vsdx parity; libvisio copies
`m_foreign`).

Output: `Section("page", title=name)`; the shapes, each its text `Para` (hidden="hidden" when its text is hidden) and
its picture, sorted by (-PinY, PinX) on the page, a shape's PinY and PinX added to its group's origin (the group's own
origin plus its Pin less its LocPin, as vsdx places sub-shapes); shapes without an XForm follow in walk order. Then
`_connections` of the connectors glued at both ends, in walk order, through the document's one
`diagram.ConnectionBudget`: "A → B (label)", A and B the ends' `_name_of` (their shown text; hidden text never names
a shape), the label the connector's shown text, which leaves the shape flow (hidden connector text stays in the flow,
hidden). A `_VBA_PROJECT_CUR` storage counts `vba` once; macros are never read.

Page names: 0x32 starts the name list, each 0x33 is one name (a u32, s16 in v5, then text to a NUL); a name index
0xC9 (u32 count, 13-byte records: name id, name id, element id, a byte) or 0x34 (u32 pairs) maps element ids to
names for the streams one level above it, so a page takes the name its pointer index maps to at its level + 1. A page
without one, or whose lookup fails (counted `page`), is numbered and keeps its shapes.

Bounds (each read at call time): `vsd_stream`'s for the walk; `limits.BIN_RECORDS` items a document for the loops this
module runs over chunk data (v5 records, name index records, field order entries, field format blocks, misc blocks),
the first refusal counted `record` once; `ctx.guard("page")` and `ctx.guard("shape")` around every page and shape, a
failed one left out; a shape's text cut at `vsdx.MAX_TEXT` characters (only when the bytes past it hold more than NULs
and blanks), its names kept up to MAX_TEXT characters in all (a field showing a name not kept marks its text cut), its
character runs up to MAX_TEXT; then charged to the document's `limits.MAX_OUTPUT` room as vsdx charges it (its
characters and `vsdx.LINE` for each line break) and LINE more for each block emitted, a picture LINE; past the room
it is left out. Each cut or refusal counts once, `text` for own text, `stencil` for inherited, `image` for a
picture; blank text never counts. Pictures: a DIB gets its file header from `officeart.dib_to_bmp` and becomes a PNG;
each picture's emitted bytes are charged to `limits.IMAGE_BYTES` a document (past it counted `image`). Group chains
deeper than `limits.MAX_DEPTH` are left out, counted `depth` once a page. Masters up to `vsdx.MASTERS` pages; edges
kept per page up to the budget's room and one more. Text and names are cut to their caps before they are decoded, and
nothing is allocated from a declared count.
"""
import datetime
import math
import struct

from .. import diagram, images, limits, officeart, text as textmod
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Image, ListItem, Para, Section, Text
from . import cfb_route, vsd_stream, vsdx, zip_route
from .vsd_stream import Chunk

PAGE = 0x15
STENCILS, STENCIL_PAGE = 0x1D, 0x1E
SHAPES = frozenset((0x47, 0x48, 0x4E))
NAME_LIST2, NAME2 = 0x32, 0x33
NAME_INDEXES = (0xC9, 0x34)
FONT = 0x19
V5_LISTS = frozenset((0x32, 0x65, 0x66, 0x68, 0x69, 0x6A, 0x6B, 0x6C))  # VSD5Parser's handleChunkRecords callers
NONE = 0xFFFFFFFF          # libvisio's MINUS_ONE
LID = 0x0409               # 8-bit text's code page without a font charset (decision 8)
MARK = "￼"             # a field's place in UTF-16 text
STRING_CELL, DATE_CELL = 232, 40
UNKNOWN_FORMAT = 0xFFFF
FONT_NAME = 256            # bytes of a FontIX name looked at for its charset suffix
_D = struct.Struct("<d")
_BLANKS = b"\x00 \t\r\n"
_SCAN = 1 << 16

# a font's charset to its codec (decision 8; N16: straight to a codec, rtf_lex untouched)
CHARSET_LID = {128: "cp932", 129: "cp949", 134: "gbk", 136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258",
               177: "cp1255", 178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250"}
# readFontIX: a charset of 0 from the name. " CE" must end the name; the others may stand anywhere in it after its
# first character (std::string::rfind from length - n), and "GOST" anywhere
_NAME_CHARSETS = ((b" Cyrillic", 0xCC), (b" Cyr", 0xCC), (b" CYR", 0xCC), (b" Baltic", 0xBA), (b" Greek", 0xA1),
                  (b" Tur", 0xA2), (b" TUR", 0xA2), (b" Hebrew", 0xB1), (b" Arabic", 0xB2), (b" Thai", 0xDE))

# VSDFieldList.cpp: unit strings and the factor from Visio's stored unit, by cell type
_UNITS = {33: "%", 36: " acres", 37: " ha", 43: " ew.", 44: " ed.", 45: " eh.", 46: " em.", 47: " es.", 50: " pt",
          51: " p", 53: " d", 54: " c", 65: " in", 66: " ft", 68: " mi", 69: " cm", 70: " mm", 71: " m", 72: " km",
          75: " yd", 76: " nm.", 81: " deg", 83: " rad", 84: " min", 85: " sec"}
_SCALE = {33: 100.0, 43: 1 / 7, 45: 24.0, 46: 1440.0, 47: 86400.0, 50: 72.0, 51: 6.0, 53: 67.75, 54: 5.644444444444,
          66: 0.0833333333, 68: 1 / 63360, 69: 2.54, 70: 25.4, 71: 0.0254, 72: 0.0000254, 75: 0.0277777778,
          76: 1 / 72913.386, 81: 57.2957795, 82: 57.2957795}
# number formats 0-9: (decimal places, or None for four significant digits; with the unit)
_PLACES = {0: (None, False), 1: (None, True), 2: (0, False), 3: (0, True), 4: (1, False), 5: (1, True),
           6: (2, False), 7: (2, True), 8: (3, False), 9: (3, True)}
# date and time formats; {d} is the day without padding. libvisio's "%H:%m" (month for minutes) is read as minutes.
_DATE = "%m/%d/%Y"
_TIME = "%H:%M:%S"
_DATES = {20: "%a", 21: "%A", 22: "%m/%d/%y", 23: "%m/%d/%y", 24: "%b {d}, %Y", 25: "%B {d}, %Y", 26: "{d}/%m/%y",
          27: "%d/%m/%y", 28: "{d} %b, %Y", 29: "{d} %B, %Y", 30: "%I:%M:%S %p", 31: "%H:%M", 32: "%H:%M",
          33: "%H:%M", 34: "%H:%M", 35: "%I:%M %p", 36: "%I:%M %p", 201: "%A, %B %d, %Y", 202: "%B {d}, %Y",
          203: "%m/%d/%y", 204: "%Y-%m-%d", 205: "{d}-%b-%y", 206: "%m.%d.%Y", 207: "%b.%d, %y", 208: "{d} %B %Y",
          209: "%B %y", 210: "%b-%y", 211: "%m/%d/%Y %I:%M %p", 212: "%m/%d/%Y %I:%M:%S %p", 213: "%I:%M %p",
          214: "%I:%M:%S %p", 215: "%H:%M", 216: "%H:%M:%S"}
_DATES.update(dict.fromkeys((46, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 80, 81), _TIME))
_DATES.update(dict.fromkeys((44, 45, *range(50, 66), 76, 77, 78, 79, 200, 217, 218, 219, 220, 221), _DATE))
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _name_of(text: str, name_u: str, name: str, sid: str) -> str:
    """A shape's name in a connection list: the first line of its text cut at diagram.NAME characters, else its NameU,
    else its Name, else `shape <ID>`."""
    line = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0].strip()
    if line:
        return line[:diagram.NAME]
    for v in (name_u, name):
        v = (v or "").strip()
        if v:
            return v[:diagram.NAME]
    return f"shape {str(sid)[:diagram.NAME]}"


def _connections(edges: list, names: dict) -> list:
    """`Connections:` and one `- A → B` item per (begin id, end id, label) edge, ` (label)` added when the connector
    has text; `names` maps a shape id to its _name_of (an id it lacks reads `shape <ID>`). [] for no edges."""
    if not edges:
        return []
    out = [Para([Text("Connections:")])]
    for begin, end, label in edges:
        line = f"{names.get(begin) or _name_of('', '', '', begin)} → {names.get(end) or _name_of('', '', '', end)}"
        label = " ".join((label or "").split())
        if label:
            line += f" ({label})"
        out.append(ListItem(0, "-", [Text(line)]))
    return out


def _f64(data, at):
    """The double at `at`, finite and clamped to vsdx.COORD, else None (also when it does not fit): a coordinate."""
    v = _double(data, at)
    return None if v is None else max(-vsdx.COORD, min(vsdx.COORD, v))


def _double(data, at):
    """The double at `at` when it fits and is finite, else None: a field's value, not clamped."""
    if at + 8 > len(data):
        return None
    v = _D.unpack_from(data, at)[0]
    return v if math.isfinite(v) else None


def _u32(data, at, default=0):
    return struct.unpack_from("<I", data, at)[0] if at + 4 <= len(data) else default


def _s16w(data, at, default=0):
    """VSD5Parser::getUInt: an s16 widened to unsigned 32 bits (-1 is 0xFFFFFFFF)."""
    return struct.unpack_from("<h", data, at)[0] & NONE if at + 2 <= len(data) else default


def _blank_after(data, at, wide) -> bool:
    """True when the bytes of `data` from `at` hold only NULs and blanks (UTF-16 units when `wide`), so text cut
    there loses nothing. Read in blocks of _SCAN bytes: the chunk is the walk's, nothing is copied whole."""
    n = len(data)
    if wide and at < n:
        n -= (n - at) % 2
    for i in range(at, n, _SCAN):
        blk = bytes(data[i:min(i + _SCAN, n)])
        if wide:
            if blk[1::2].strip(b"\x00") or blk[0::2].translate(None, _BLANKS):
                return False
        elif blk.translate(None, _BLANKS):
            return False
    return True


def _name_charset(name: bytes) -> int:
    n = len(name)
    if n > 3 and name.endswith(b" CE"):
        return 0xEE
    for suffix, charset in _NAME_CHARSETS:
        if n > len(suffix) and suffix in name:
            return charset
    return 0xCC if n >= 4 and b"GOST" in name else 0


def _decode(data, codec) -> str:
    return bytes(data).decode(codec, "replace") if codec else textmod.decode_8bit(data, LID)


def _fixed(value, places, unit=""):
    """doubleToString: near-zero values as 0; `places` None is %.4g."""
    if -0.0001 < value < 0.0001:
        value = 0.0
    return (f"{value:.4g}" if places is None else f"{value:.{places}f}") + unit


def _date(fmt, value) -> str:
    try:
        t = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=value)
        fmt = (fmt.replace("{d}", str(t.day)).replace("%A", _DAYS[t.weekday()])
               .replace("%a", _DAYS[t.weekday()][:3]).replace("%B", _MONTHS[t.month - 1])
               .replace("%b", _MONTHS[t.month - 1][:3]).replace("%p", "AM" if t.hour < 12 else "PM"))
        return t.strftime(fmt)
    except (OverflowError, ValueError):  # a day outside datetime's years, or one strftime refuses
        return ""


def number(fmt: int, cell: int, value: float) -> str:
    """A number field's shown value (VSDNumericField::getString); "" for the unknown format 0xFFFF; a format the
    switch does not name is the plain value with four decimals (RVNGDoubleProp)."""
    if fmt == UNKNOWN_FORMAT or not math.isfinite(value):
        return ""
    own = cell
    if cell in (63, 64):  # drawing and page units: the drawing's unit, not read here
        cell = 0
    if cell == 80:        # angle units
        cell = 83 if fmt == 11 else 81 if fmt == 12 else 82
    if own == 82 and fmt == 1:  # the cell's own type, as libvisio tests it
        fmt = 12
    scaled = value * _SCALE.get(cell, 1.0)
    unit = _UNITS.get(cell, "")
    if fmt in _PLACES:
        places, units = _PLACES[fmt]
        return _fixed(scaled, places, unit if units else "")
    if fmt == 11:
        return _fixed(scaled, 4, unit)
    if fmt == 12:
        if cell == 82:
            frac, deg = math.modf(value * 57.2957795)
            mfrac, minutes = math.modf(frac * 60)
            return f"{deg:.0f} deg {minutes:.0f} min {60.0 * mfrac:.0f} sec"
        return _fixed(scaled, 0, unit)
    if fmt in _DATES:
        return _date(_DATES[fmt], value)
    return f"{value:.4f}"


class _Shape:
    __slots__ = ("sid", "level", "parent", "master", "home", "pin", "text", "cut", "names", "names_size",
                 "names_cut", "order", "fields", "ftype", "fformat", "data", "broken", "has_misc", "hide", "begin",
                 "end", "runs")

    def __init__(self, sid, level, parent, master, home):
        self.sid, self.level, self.parent, self.master, self.home = sid, level, parent, master, home
        self.pin = None       # [PinX, PinY, LocPinX, LocPinY], each None when absent
        self.text = None      # the text chunk's bytes, cut to MAX_TEXT characters; None: no text chunk
        self.cut = False      # the text chunk held more than blanks past the cut
        self.names = {}       # 0x2D: name id -> bytes, MAX_TEXT characters in all
        self.names_size = 0
        self.names_cut = False
        self.order = None     # 0x66: field ids in order
        self.fields = {}      # 0xA1: field id -> ("s", name id) or ("n", format, cell, value)
        self.ftype = self.fformat = None
        self.data = None
        self.broken = False
        self.has_misc = False  # a misc chunk of its own sets the hidden flag
        self.hide = False
        self.begin = self.end = None  # glued ends (readMisc)
        self.runs = None      # 8-bit text's runs: [(count, codec)]


class _Entry:
    """A stencil shape as its inheritors see it: its text (fields resolved), cut, hidden flag, XForm (or None);
    inlines built once."""
    __slots__ = ("text", "cut", "hide", "pin", "_inlines")

    def __init__(self, text, cut, hide, pin=None):
        self.text, self.cut, self.hide, self.pin, self._inlines = text, cut, hide, pin, None

    def inlines(self):
        if self._inlines is None:
            self._inlines = vsdx._inlines(self.text.strip("\n"))
        return self._inlines


_EMPTY = _Entry("", False, False)  # the master page has no such shape, or a blank, shown, unplaced one (kept as none)
_LOST = _Entry("", False, False)   # the stencil room was spent on it, or it inherits from a lost one


class _Stencil:
    __slots__ = ("ptr", "idx", "shapes", "first")

    def __init__(self, ptr, idx):
        self.ptr, self.idx = ptr, idx
        self.shapes = {}      # shape id -> _Entry or _LOST; a blank shape keeps no entry (it reads as _EMPTY)
        self.first = NONE


class _Page:
    __slots__ = ("ptr", "title", "entries", "ids", "names", "edges")

    def __init__(self, ptr):
        self.ptr, self.title = ptr, ""
        self.entries = []     # (parent, PinX, PinY, blocks) of the shapes that show something, in walk order
        self.ids = {}         # shape id -> (parent, PinX, PinY, LocPinX, LocPinY), the first shape of an id; the
        #                       parent alone (an int) for a shape with no XForm, which offsets nothing
        self.names = {}       # shape id -> its shown text (the first shape of an id with some)
        self.edges = []       # (begin id, end id, label) of the connectors glued at both ends


class _Reader:
    def __init__(self, data, version, ctx):
        self.data, self.v, self.ctx = data, version, ctx
        self.wide = version == 11
        self.items = limits.BIN_RECORDS  # chunk-data items the document may still read (see `take`)
        self.items_cut = False
        self.room = limits.MAX_OUTPUT    # shape text the document may still emit (vsdx's charge)
        self.stencil_room = limits.MAX_OUTPUT  # stencil shape text the document may keep
        self.image_bytes = 0
        self.budget = diagram.ConnectionBudget(ctx)
        self.names = {}       # libvisio's m_names: 0x33 id -> its bytes; a 0x32 starts a new one
        self.index = {}       # level -> [name index chunk data, its type, the names it maps to, parsed or None]
        self.fonts = {}       # 0x19 id -> codec (None: LID)
        self.stencil = 0      # stencil (0x1D) streams entered and not left
        self.collecting = False  # this stencil list is read (no stencil page read before it)
        self.spage = None     # the stencil page being read
        self.spages = {}      # completed stencil pages by pointer index
        self.masters_read = 0
        self.pages = []       # pages open (nested pages close first)
        self.shape = None
        self.stream_sid = NONE  # the pointer index of the shape stream last entered (libvisio's m_currentShapeID)
        self.started = 0
        self.sections = []

    def take(self, n: int) -> int:
        """How many of `n` items the document's BIN_RECORDS meter lets a loop read; the first refusal counts
        `record` once for the document."""
        if n <= self.items:
            self.items -= n
            return n
        got, self.items = max(self.items, 0), 0
        if not self.items_cut:
            self.items_cut = True
            self.ctx.skipped["record"] += 1
        return got

    # ---- the walk
    def run(self) -> Doc:
        for step in vsd_stream.Tree(self.data, self.v, self.ctx).walk():
            if self.shape is not None and step.level <= self.shape.level:
                self.flush()
            if step.kind == "enter":
                self.enter(step)
            elif step.kind == "chunk":
                self.chunk(step)
            else:
                self.leave(step)
        if self.shape is not None:
            self.flush()
        while self.pages:
            self.end_page()
        if not self.sections:
            raise ReadError("damaged", "no readable page")
        return Doc(blocks=self.sections)

    def enter(self, step):
        t = step.pointer.type
        if t == STENCILS:
            self.stencil += 1
            if self.stencil == 1:
                self.collecting = not self.spages
        elif t == STENCIL_PAGE:
            if self.stencil and self.collecting and self.spage is None and self.masters_read < vsdx.MASTERS:
                self.masters_read += 1
                self.spage = _Stencil(step.pointer, step.index)
        elif t == PAGE and not self.stencil:
            self.start_page(step)
        elif t in SHAPES:
            self.stream_sid = step.index

    def leave(self, step):
        t = step.pointer.type
        if t == STENCILS:
            self.stencil -= 1
        elif t == STENCIL_PAGE and self.spage is not None and self.spage.ptr is step.pointer:
            if self.shape is not None:
                self.flush()
            self.spages[self.spage.idx] = self.spage
            self.spage = None
        elif t == PAGE and self.pages and self.pages[-1].ptr is step.pointer:
            self.end_page()

    def home(self):
        """Where a shape starting now belongs: the stencil page being read, the open page, or None."""
        if self.stencil:
            return self.spage
        return self.pages[-1] if self.pages else None

    def chunk(self, step):
        c = step.chunk
        if self.v == 5 and c.type in V5_LISTS:
            if c.type == NAME_LIST2:
                self.names = {}
            level = step.level + 1
            for i, (t, data) in enumerate(self.records(c.data)):
                if t not in SHAPES and t not in V5_LISTS:
                    self.dispatch(Chunk(t, i, 0, level, data), level, None)
            return
        self.dispatch(c, step.level, step)

    def dispatch(self, c, level, step):
        t = c.type
        if t == NAME_LIST2:
            self.names = {}
        elif t == NAME2:
            self.names[c.id] = bytes(c.data[:4 + 2 * vsdx.MAX_TITLE + 2])  # all name2 reads
        elif t in NAME_INDEXES:
            self.index[level] = [bytes(c.data), t, self.names, None]
        elif t == FONT:
            if not self.wide:
                self.font(c)
        elif t in SHAPES:
            if step is not None and self.home() is not None:
                self.start_shape(step)
        elif self.shape is not None and not self.shape.broken:
            handler = _HANDLERS.get(t)
            if handler is not None:
                self.guarded(handler, c)

    def guarded(self, name, c):
        s, ok = self.shape, False
        with self.ctx.guard("shape"):
            getattr(self, name)(s, c if c.type in _BY_CHUNK else c.data)
            ok = True
        if not ok:
            s.broken = True

    def records(self, data) -> list:
        """VSD5Parser::handleChunkRecords: [(type, data)] of a v5 list chunk's records in offset order, each from its
        4-aligned offset to the previous record's offset (the first to the end offset); a later record at the same
        offset replaces the earlier. The table's entries take from the item meter."""
        n = len(data)
        if n < 4:
            return []
        num = struct.unpack_from("<H", data, n - 4)[0]
        head = n - 4 * (num + 1)
        if head <= 0:
            return []
        end = min(struct.unpack_from("<H", data, n - 2)[0], head)
        found = {}
        for i in range(self.take(num)):
            t, off = struct.unpack_from("<HH", data, head + 4 * i)
            start = (off + 3) // 4 * 4
            if start < end:
                found[start] = (t, end - start)
                end = off
        return [(t, data[at:at + length]) for at, (t, length) in sorted(found.items())]

    def font(self, c):
        """readFontIX: the charset in the low byte at 2 (of a u32; an s16 in v5); 0 is found from the name after it."""
        d, at = c.data, (4 if self.v == 5 else 6)
        if len(d) < at:
            return
        charset = d[2]
        if not charset:
            charset = _name_charset(bytes(d[at:at + FONT_NAME]).split(b"\x00", 1)[0])
        self.fonts[c.id] = CHARSET_LID.get(charset)

    # ---- pages
    def start_page(self, step):
        page = _Page(step.pointer)
        self.pages.append(page)
        self.started += 1
        title = ""
        with self.ctx.guard("page"):  # a failed lookup numbers the page; its shapes stay
            title = self.page_name(step.index, step.level + 1).strip()
        page.title = (title or str(self.started))[:vsdx.MAX_TITLE]

    def end_page(self):
        if self.shape is not None:
            self.flush()
        page = self.pages.pop()
        sec = None
        with self.ctx.guard("page"):
            sec = self.finish(page)
        if sec is not None:
            self.sections.append(sec)

    def page_name(self, index, level) -> str:
        entry = self.index.get(level)
        if entry is None:
            return ""
        if entry[3] is None:
            entry[3] = {}  # set first: an index that fails is not parsed again
            entry[3] = self.parse_index(entry[0], entry[1], entry[2])
        raw = entry[3].get(index)
        return self.name2(raw) if raw is not None else ""

    def parse_index(self, data, kind, names) -> dict:
        """{element id: name bytes} of a name index (readNameIDX: u32 count and 13-byte records, v5 u16 count and
        (u16 name id, u16 element id); readNameIDX123: u32 pairs to the end), the later record of an element
        winning."""
        out = {}
        if kind == 0xC9 and self.v == 5:
            count = min(struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0, max(len(data) - 2, 0) // 4)
            for i in range(self.take(count)):
                name_id, element = struct.unpack_from("<HH", data, 2 + 4 * i)
                if name_id in names:
                    out[element] = names[name_id]
        elif kind == 0xC9:
            count = min(_u32(data, 0), max(len(data) - 4, 0) // 13)
            for i in range(self.take(count)):
                at = 4 + 13 * i
                name_id, element = struct.unpack_from("<I", data, at)[0], struct.unpack_from("<I", data, at + 8)[0]
                if name_id in names:
                    out[element] = names[name_id]
        else:
            for i in range(self.take(len(data) // 8)):
                name_id, element = struct.unpack_from("<II", data, 8 * i)
                if name_id in names:
                    out[element] = names[name_id]
        return out

    def name2(self, raw) -> str:
        """A 0x33 name: a u32 (v5 an s16), then text to its NUL, cut to MAX_TITLE characters before it is decoded."""
        if self.wide:
            body = bytes(raw[4:4 + 2 * vsdx.MAX_TITLE + 2])
            body = body[:len(body) // 2 * 2].decode("utf-16-le", "replace")
            return body.split("\x00", 1)[0]
        skip = 2 if self.v == 5 else 4
        body = bytes(raw[skip:skip + vsdx.MAX_TITLE + 1]).split(b"\x00", 1)[0]
        return textmod.decode_8bit(body, LID)

    def finish(self, page) -> Section:
        sec = Section("page", title=page.title)
        memo, placed, loose, deep = {}, [], [], False
        for parent, x, y, blocks in page.entries:
            base = self.origin(page.ids, memo, parent)
            if base is None or base[2] + 1 > limits.MAX_DEPTH:
                deep = True
                continue
            if x is None or y is None:
                loose.append(blocks)
            else:
                placed.append(((-(y + base[1]), x + base[0]), blocks))
        if deep:
            self.ctx.skipped["depth"] += 1
            if self.ctx.skipped["depth"] == 1:
                self.ctx.warnings.append("depth: skipped content nested too deeply")
        placed.sort(key=lambda e: e[0])
        for _, blocks in placed:
            sec.blocks.extend(blocks)
        for blocks in loose:
            sec.blocks.extend(blocks)
        edges = self.budget.take(page.edges)
        names = {sid: _name_of(page.names[sid], "", "", sid)
                 for b, e, _ in edges for sid in (b, e) if sid in page.names}
        sec.blocks.extend(_connections(edges, names))
        return sec

    @staticmethod
    def origin(ids, memo, sid):
        """(dx, dy, depth) for the children of shape `sid`: the group's origin and its own nesting depth, (0, 0, -1)
        for no group, None when it is nested past MAX_DEPTH. A parent the page does not hold, or one met again on
        the way up (a cycle), counts as no group. Each walk up is at most MAX_DEPTH + 1 steps, and a resolved
        shape's origin is kept, so a page costs at most its shapes times that."""
        root = (0.0, 0.0, -1)
        path, seen, cur = [], set(), sid
        while True:
            if cur in memo:
                base = memo[cur]
                break
            rec = ids.get(cur) if cur not in (0, NONE) else None
            if rec is None or cur in seen:
                base = root
                break
            if len(path) > limits.MAX_DEPTH:
                memo[sid] = None
                return None
            path.append(cur)
            seen.add(cur)
            cur = rec if type(rec) is int else rec[0]
        for node in reversed(path):
            if base is not None:
                rec = ids[node]
                d = base[2] + 1
                if d > limits.MAX_DEPTH:
                    base = None
                elif type(rec) is int:
                    base = (base[0], base[1], d)
                else:
                    _, x, y, lx, ly = rec
                    base = (base[0] + (x or 0.0) - (lx or 0.0), base[1] + (y or 0.0) - (ly or 0.0), d)
            memo[node] = base
        return base

    # ---- shapes
    def start_shape(self, step):
        if self.shape is not None:
            self.flush()
        c = step.chunk
        sid = c.id if c.id != NONE else self.stream_sid
        self.stream_sid = NONE
        d = c.data
        if self.v == 5:
            parent, master = _s16w(d, 2), (_s16w(d, 6, NONE), _s16w(d, 8, NONE))
        else:
            parent, master = _u32(d, 10), (_u32(d, 18, NONE), _u32(d, 26, NONE))
        self.shape = _Shape(sid, step.level, parent, master, self.home())

    def flush(self):
        s, self.shape = self.shape, None
        if s.broken or s.home is None:
            return
        with self.ctx.guard("shape"):
            if isinstance(s.home, _Page):
                self.place(s, s.home)
            else:
                self.keep(s, s.home)

    def master_shape(self, page_id, shape_id):
        """The stencil shape a shape names (getStencilShape over completed stencil pages): its _Entry; _EMPTY when
        the page lacks that shape; None when the page is not read or the shape was lost to the stencil room."""
        page = self.spages.get(page_id)
        if page is None:
            return None
        e = page.shapes.get(page.first if shape_id == NONE else shape_id)
        if e is None:
            return _EMPTY
        return None if e is _LOST else e

    def inherited(self, s):
        """(entry, hidden) for shape `s`: the master shape when it may give the text, the hidden flag or the XForm."""
        entry = None
        if s.master[0] != NONE and (s.text is None or not s.has_misc or (s.pin is None and self.v != 5)):
            entry = self.master_shape(*s.master)
        if s.has_misc:
            return entry, s.hide
        return entry, bool(entry is not None and self.v != 5 and entry.hide)  # v5 copies no misc

    def keep(self, s, spage):
        """A stencil shape: its text resolved once and kept (charged to the stencil room) for its inheritors. One
        that would inherit its text from a lost shape, or from a page past MASTERS, is lost too; one naming its own
        page or a page not yet read inherits nothing, as in libvisio."""
        entry, hide = self.inherited(s)
        if spage.first == NONE:
            spage.first = s.sid
        if s.text is None and entry is None and s.master[0] != NONE:
            mp = s.master[0]
            if mp in self.spages or (mp != spage.idx and self.masters_read >= vsdx.MASTERS):
                spage.shapes[s.sid] = _LOST
                return
        if s.text is not None:
            text, cut = self.text_of(s)
        elif entry is not None:
            text, cut = entry.text, entry.cut
        else:
            text, cut = "", False
        if not text.strip():
            text = ""
        cost = len(text) + vsdx.LINE * (text.count("\n") + text.count("\r"))
        pin = s.pin if s.pin is not None or entry is None or self.v == 5 else entry.pin
        if cost > self.stencil_room:
            spage.shapes[s.sid] = _LOST
        elif not (text or cut or hide or pin):
            spage.shapes.pop(s.sid, None)  # blank: no entry, as a missing id reads _EMPTY (an earlier one dropped)
        else:
            self.stencil_room -= cost
            spage.shapes[s.sid] = _Entry(text, cut, hide, pin)

    def place(self, s, page):
        """A page shape: its text (its own, else its master's), hidden flag, glued ends and picture."""
        entry, hidden = self.inherited(s)
        kind, inlines = "text", None
        if s.text is not None:
            text, cut = self.text_of(s)
        elif s.master[0] == NONE:
            text, cut = "", False
        elif entry is None:  # the master is not read: its text is lost
            self.ctx.skipped["stencil"] += 1
            text, cut = "", False
        else:
            text, cut, kind = entry.text, entry.cut, "stencil"
        glued = s.begin is not None and s.end is not None
        show = hidden or not glued  # a glued connector's shown text is its label in the connection list
        if text.strip():
            cost = len(text) + vsdx.LINE * (text.count("\n") + text.count("\r") + show)
            if cost > self.room:
                self.ctx.skipped[kind] += 1
                text = ""
            else:
                self.room -= cost
                if cut:
                    self.ctx.skipped[kind] += 1
                if kind == "stencil":
                    inlines = entry.inlines()
        else:
            text = ""
            if cut:
                self.ctx.skipped[kind] += 1
        if text and not hidden and s.sid not in page.names:  # hidden text never names a shape
            page.names[s.sid] = text
        if glued and len(page.edges) <= self.budget.room:  # one past the room, so the budget sees the cut
            page.edges.append((s.begin, s.end, "" if hidden else text))
        blocks = []
        if text and show:
            blocks.append(Para(inlines if inlines is not None else vsdx._inlines(text.strip("\n")),
                               hidden="hidden" if hidden else ""))
        blocks.extend(self.picture(s))
        if s.pin is None and entry is not None and self.v != 5:  # readShape copies the master's XForm (v6, v11)
            s.pin = entry.pin
        pin = s.pin or (None, None, None, None)
        if s.sid not in page.ids and (s.pin is not None or s.parent not in (0, NONE)):
            page.ids[s.sid] = s.parent if s.pin is None else (s.parent, *pin)  # the parent alone: no 5-tuple of Nones
        if blocks:
            page.entries.append((s.parent, pin[0], pin[1], blocks))

    # ---- chunk readers (libvisio's names)
    def xform(self, s, data):
        """readXFormData: PinX at 1, PinY at 10, LocPinX at 37, LocPinY at 46 (a unit byte before each double);
        a value the chunk does not hold keeps what the shape had."""
        pin = list(s.pin or (None, None, None, None))
        for i, at in enumerate((1, 10, 37, 46)):
            if at + 8 <= len(data):
                pin[i] = _f64(data, at)
        s.pin = pin

    def text(self, s, data):
        """readText: 8 bytes skipped, the rest kept up to MAX_TEXT characters (the chunk's last wins); cut when the
        bytes past that hold more than NULs and blanks."""
        cap = vsdx.MAX_TEXT * (2 if self.wide else 1)
        s.text = bytes(data[8:8 + cap])
        s.cut = not _blank_after(data, 8 + cap, self.wide)

    def name_list(self, s, data):
        s.names, s.names_size, s.names_cut = {}, 0, False

    def listed(self, s, c):
        """readName (0x2D): a shape name a string field shows; the shape keeps MAX_TEXT characters of names in all,
        a name past that is not kept (a field showing it marks the text cut)."""
        unit = 2 if self.wide else 1
        size = len(c.data) // unit
        if c.id in s.names or s.names_size + size > vsdx.MAX_TEXT:
            if c.id not in s.names:
                s.names_cut = True
            return
        s.names[c.id] = bytes(c.data[:size * unit])
        s.names_size += size

    def field_list(self, s, data):
        """readFieldList: a sub-header length, the children list length, the sub-header, then the field ids (u32);
        each id once, in its first place."""
        if len(data) < 8:
            return
        sub, size = struct.unpack_from("<II", data, 0)
        start = 8 + sub
        n = self.take(max(0, min(size, len(data) - start)) // 4) if start < len(data) else 0
        s.order = list(dict.fromkeys(struct.unpack_from(f"<{n}I", data, start))) if n else []

    def field(self, s, c):
        self.text_field(s, c.id, c.data)

    def misc(self, s, data):
        """readMisc: the hidden-text flag; v6 and v11 also the glue blocks giving the connector's begin, then end id
        (each block one item of the meter, read until both ends are found or the data ends)."""
        if not len(data):
            return
        s.has_misc = True
        s.hide = bool(data[0] & 0x20)
        if self.v == 5:
            return
        at, n = (45 if self.wide else 23), len(data)
        while at + 4 <= n and (s.begin is None or s.end is None) and self.take(1):
            length = struct.unpack_from("<I", data, at)[0]
            if not length:
                break
            if (at + 20 <= n and data[at + 4] == 2 and data[at + 6] == 0x74
                    and struct.unpack_from("<I", data, at + 7)[0] == 0x6000004E and data[at + 15] == 0x7A
                    and struct.unpack_from("<I", data, at + 16)[0] == 0x40000073):
                sid = struct.unpack_from("<I", data, at + 11)[0]
                if s.begin is None:
                    s.begin = sid
                else:
                    s.end = sid
            at += length

    def char_ix(self, s, data):
        """readCharIX of 8-bit text: the run's character count (u32, v5 u16) and font id (u16), the font's codec as
        it is when the run is read; MAX_TEXT runs a shape."""
        if self.wide:
            return
        if self.v == 5:
            if len(data) < 4:
                return
            count, font = struct.unpack_from("<HH", data, 0)
        else:
            if len(data) < 6:
                return
            count, font = struct.unpack_from("<IH", data, 0)
        if s.runs is None:
            s.runs = []
        if len(s.runs) < vsdx.MAX_TEXT:
            s.runs.append((count, self.fonts.get(font)))

    def foreign_type(self, s, data):
        """readForeignDataType: the type u16 at 36 (4 when the map mode u16 at 38 is 8), the format u32 at 49."""
        if len(data) < 40:
            return
        kind, mode = struct.unpack_from("<HH", data, 36)
        s.ftype = 4 if mode == 8 else kind
        s.fformat = _u32(data, 49)

    def foreign_data(self, s, data):
        s.data = bytes(data)  # an inflated blob's buffer is not kept by the walk

    # ---- a shape's text and picture
    def text_field(self, s, cid, data):
        """readTextField: v6 and v11 the cell type at 7, a string field's name id at 8, a number's value at 8 and
        its format in the block (index 2, then 0x80 0xC2) found from 0x36 (v11) or 0x24 (v6); v5 the cell type at
        3, the name id (s16) or value at 4, the format unknown. The first of an id wins."""
        if cid in s.fields:
            return
        cell_at = 3 if self.v == 5 else 7
        if len(data) <= cell_at:
            return
        cell = data[cell_at]
        if cell == STRING_CELL:
            if self.v == 5:
                if len(data) >= 6:
                    s.fields[cid] = ("s", struct.unpack_from("<h", data, 4)[0])
            elif len(data) >= 12:
                s.fields[cid] = ("s", struct.unpack_from("<i", data, 8)[0])
            return
        value = _double(data, cell_at + 1)
        if value is None:
            return
        if self.v == 5:
            s.fields[cid] = ("n", UNKNOWN_FORMAT, cell, value)
            return
        at, fmt = (0x36 if self.wide else 0x24), None
        while at + 6 <= len(data) and self.take(1):
            length = struct.unpack_from("<I", data, at)[0]
            if not length:
                break
            if data[at + 5] == 2 and at + 11 <= len(data) and data[at + 9] == 0x80 and data[at + 10] == 0xC2:
                fmt = struct.unpack_from("<H", data, at + 7)[0]
                break
            at += length
        if fmt is None:
            fmt = 200 if cell == DATE_CELL else UNKNOWN_FORMAT
        s.fields[cid] = ("n", fmt, cell, value)

    def field_value(self, s, field, cache) -> tuple:
        """(the field's shown text, whether it lost something): a string field's name, or "" and the shape's names
        cut when the name was not kept; a number in its format."""
        if field[0] != "s":
            return number(field[1], field[2], field[3]), False
        key = field[1]
        if key not in cache:
            raw = s.names.get(key)
            if raw is None:
                cache[key] = ("", s.names_cut)
            elif self.wide:
                cache[key] = (raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace").replace("\x00", ""), False)
            else:
                cache[key] = (textmod.decode_8bit(raw, LID).replace("\x00", ""), False)
        return cache[key]

    def decode8(self, raw, runs) -> str:
        """8-bit text run by run: each run's bytes in its font's codec (a count of 0 covers one byte, as the
        collector's loop), the rest in the last run's."""
        if not runs:
            return _decode(raw, None)
        parts, pos, n, codec = [], 0, len(raw), None
        for count, codec in runs:
            if pos >= n:
                break
            step = max(count, 1)
            parts.append(_decode(raw[pos:pos + step], codec))
            pos += step
        if pos < n:
            parts.append(_decode(raw[pos:], codec))
        return "".join(parts)

    def text_of(self, s) -> tuple:
        """(the shape's text with its fields' values, cut at MAX_TEXT; whether it was cut). Nothing is counted here."""
        raw = s.text
        if not raw:
            return "", s.cut
        if self.wide:
            pieces = raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace").split(MARK)
        else:
            pieces = self.decode8(raw, s.runs).split("\x1e")
        ids = s.order if s.order else sorted(s.fields)
        values = (s.fields[i] for i in ids if i in s.fields)
        out, size, cut, cache = [], 0, s.cut, {}
        for k, piece in enumerate(pieces):
            if k:
                field = next(values, None)
                if field is not None:
                    shown, lost = self.field_value(s, field, cache)
                    cut = cut or lost
                    piece = shown + piece
            piece = piece.replace("\x00", "")
            if size + len(piece) > vsdx.MAX_TEXT:
                out.append(piece[:vsdx.MAX_TEXT - size])
                cut = True
                break
            out.append(piece)
            size += len(piece)
        return "".join(out), cut

    def picture(self, s) -> list:
        """The shape's picture: an OLE object counted `object`; a DIB through officeart.dib_to_bmp to a PNG; its
        emitted bytes charged to IMAGE_BYTES and LINE to the room (past either counted `image`)."""
        if s.ftype == 2:
            self.ctx.skipped["object"] += 1
            return []
        data = s.data
        if data is None:
            if s.ftype is not None:
                self.ctx.skipped["image"] += 1
            return []
        if self.image_bytes >= limits.IMAGE_BYTES or vsdx.LINE > self.room:  # spent: no picture work at all
            self.ctx.skipped["image"] += 1
            return []
        if s.ftype == 1 and s.fformat == 0:
            data = officeart.dib_to_bmp(data)
            if data is None:
                self.ctx.skipped["image"] += 1
                return []
            data = images.bmp_to_png(data) or data  # a BMP it cannot convert stays a BMP, not tried again
        else:
            kind = images.sniff(data) if len(data) >= 16 else None
            if kind == "bmp":
                data = images.bmp_to_png(data) or data
        kind = images.sniff(data) if len(data) >= 16 else None
        if kind is None:
            self.ctx.skipped["image"] += 1
            return []
        self.image_bytes += len(data)
        if self.image_bytes > limits.IMAGE_BYTES or vsdx.LINE > self.room:
            self.ctx.skipped["image"] += 1
            return []
        self.room -= vsdx.LINE
        return [Image(data)]


# chunk type -> the _Reader method that reads it (looked up per call); _BY_CHUNK's take the chunk, the rest its data
_HANDLERS = {0x9B: "xform", 0x0E: "text", 0x2C: "name_list", 0x2D: "listed", 0x66: "field_list", 0xA1: "field",
             0xA4: "misc", 0x94: "char_ix", 0x98: "foreign_type", 0x0C: "foreign_data"}
_BY_CHUNK = frozenset((0x2D, 0xA1))


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:2] == b"PK":  # a zip named .vsd (a vsdx saved as .vsd): zip_route picks its parser, never this one
        return zip_route.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a Visio file")
    got = parse_compound(path, ctx)
    if isinstance(got, str):  # another format's compound file named .vsd
        return cfb_route.parse(got, path, ctx)
    return got


def parse_compound(src, ctx) -> Doc | str:
    """Read the compound file `src` (a path or its bytes, nothing is written): the Doc, or the name of the other
    parser `cfb_route` picks for a compound file that holds no VisioDocument stream. `parse` ends in it."""
    vba = False
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "vsd", "no VisioDocument stream")
            data = cf.read("VisioDocument") if other is None else None
            vba = any(n.upper().startswith("_VBA_PROJECT_CUR/") for n in cf.names())
    except (KeyError, CfbError):
        raise ReadError("damaged", "VisioDocument stream") from None
    if other is not None:
        return other
    if vba:
        ctx.skipped["vba"] = 1  # macros are never read; named once
    version, _ = vsd_stream.header(data)
    return _Reader(data, version, ctx).run()
