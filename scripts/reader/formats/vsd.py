# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libvisio a42aaff13e4a67afcd077c3abb3ef13bb6463025, src/lib/VSDParser.cpp, VSD6Parser.cpp, VSDFieldList.cpp, VSDContentCollector.cpp.
"""Visio 2002-2013 (version 11) and Visio 2000 (version 6) drawings (vsd, vst) to the document model: one section per
page, each shape's text and picture together in reading order, as vsdx gives them.

Ported from libvisio (MPL-2.0) at a42aaff13e4a67afcd077c3abb3ef13bb6463025, `src/lib/`: `VSDParser.cpp`
(`handleStream`'s page and shape context, `_handleLevelChange`, `readShape`, `readXFormData`, `readText`, `readName`,
`readName2`, `readNameIDX`, `readNameIDX123`, `readFieldList`, `readTextField`, `readForeignDataType`,
`readForeignData`), `VSD6Parser.cpp` (the 8-bit `readText`, `readName`, `readName2` and `readTextField`),
`VSDFieldList.cpp` (`VSDFieldList::handle`'s order, the number and date formats of `VSDNumericField::getString`) and
`VSDContentCollector.cpp` (a field mark taking the next field's value, `_handleForeignData`'s bitmap header and
`computeBMPDataOffset`). The walk of the VisioDocument stream is `vsd_stream`'s; this module reads one walk of it.

Context. As libvisio, by levels: a stream type 0x15 (outside the stencils, 0x1D) opens a page and its leave closes it,
background pages (format bit 0 clear) included. A chunk 0x48, 0x47 or 0x4E in a page starts a shape; the chunks and
streams deeper than it are the shape's, and the next step at its level or above ends it. Its id is the chunk's, else
its stream's pointer index; its parent is the u32 at 10 of the shape chunk (0 or 0xFFFFFFFF: none).

Content of a shape: 0x9B gives PinX, PinY, LocPinX and LocPinY (a unit byte before each double); 0x0E is the text, 8
bytes skipped then UTF-16LE (v11) or 8-bit (v6, `text.decode_8bit` with 0x0409); 0x2C and 0x2D its names, 0x66 the
field order and 0xA1 its fields (a string field shows the shape name it points at, a number field its value in its
format, found from 0x36 (v11) or 0x24 (v6) of the chunk); each field mark (U+FFFC, or the byte 0x1E in 8-bit text)
takes the next field's value. 0x98 gives the picture's type and format, 0x0C its bytes: a DIB (type 1, format 0) gets
the file header libvisio writes for it and becomes a PNG through `images.bmp_to_png`; other bitmaps and metafiles
(types 0 and 4: EMF, WMF) are kept as they are, so the renderer shows the PNG, JPEG and GIF and lists the rest as
assets. An OLE object (type 2) is counted `object`.

Page names: 0x32 starts the name list, each 0x33 is one name (a u32, then text to a NUL); a name index 0xC9 (u32
count, 13-byte records: name id, name id, element id, a byte) or 0x34 (u32 pairs) maps element ids to names for the
streams one level above it, so a page takes the name its pointer index maps to at its level + 1. A page without one is
numbered.

Output: `Section("page", title=name)`; the shapes, each its text `Para` (through `vsdx._inlines`) and its picture,
sorted by (-PinY, PinX) on the page, a shape's PinY and PinX added to its group's origin (the group's own origin plus
its Pin less its LocPin, as vsdx places sub-shapes); shapes without an XForm follow in walk order. Masters, connection
lists, hidden text and macros are not read here. Visio 5 (version 5) is refused as `unsupported` ("Visio 5 drawing"):
its shape, field and name-index layouts differ (VSD5Parser) and are not read.

Bounds (each read at call time): `vsd_stream`'s for the walk; `limits.BIN_RECORDS` items a document for the loops this
module runs over chunk data (name index records, field order entries, field format blocks, field marks), the first
refusal counted `record` once; `ctx.guard("page")` and `ctx.guard("shape")` around every page and shape, a failed
one left out; a shape's text cut at `vsdx.MAX_TEXT` characters (counted `text`), then charged to the document's
`limits.MAX_OUTPUT` room as vsdx charges it (its characters and `vsdx.LINE` for each line break; past it the text is
left out, counted `text`); picture bytes charged to `limits.IMAGE_BYTES` a document, each placement (past it
counted `image`); group chains deeper than `limits.MAX_DEPTH` left out, counted `depth` once a page. Text and names
are cut to their caps before they are decoded, and nothing is allocated from a declared count.
"""
import datetime
import math
import struct

from .. import images, limits, text as textmod
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Image, Para, Section
from . import cfb_route, vsd_stream, vsdx

PAGE = 0x15
STENCILS = 0x1D
SHAPES = frozenset((0x47, 0x48, 0x4E))
NAME_LIST2, NAME2 = 0x32, 0x33
NAME_INDEXES = (0xC9, 0x34)
NONE = 0xFFFFFFFF          # libvisio's MINUS_ONE
LID = 0x0409               # 8-bit text's code page (decision 8)
MARK = "\ufffc"             # a field's place in UTF-16 text
STRING_CELL, DATE_CELL = 232, 40
UNKNOWN_FORMAT = 0xFFFF
_D = struct.Struct("<d")

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


def _f64(data, at):
    """The double at `at`, finite and clamped to vsdx.COORD, else None (also when it does not fit)."""
    if at + 8 > len(data):
        return None
    v = _D.unpack_from(data, at)[0]
    return max(-vsdx.COORD, min(vsdx.COORD, v)) if math.isfinite(v) else None


def _u32(data, at, default=0):
    return struct.unpack_from("<I", data, at)[0] if at + 4 <= len(data) else default


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
    """A number field's shown value (VSDNumericField::getString); "" for an unknown format."""
    if fmt == UNKNOWN_FORMAT or not math.isfinite(value):
        return ""
    if cell in (63, 64):  # drawing and page units: the drawing's unit, not read here
        cell = 0
    if cell == 80:        # angle units
        cell = 83 if fmt == 11 else 81 if fmt == 12 else 82
    if cell == 82 and fmt == 1:
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
            deg, frac = divmod(value * 57.2957795, 1.0) if value >= 0 else (math.trunc(value * 57.2957795),
                                                                            math.fmod(value * 57.2957795, 1.0))
            minutes = math.trunc(frac * 60)
            return f"{deg:.0f} deg {minutes:.0f} min {60.0 * (frac * 60 - minutes):.0f} sec"
        return _fixed(scaled, 0, unit)
    if fmt in _DATES:
        return _date(_DATES[fmt], value)
    return f"{value:g}"


def bmp_file(dib) -> bytes:
    """A DIB with the BITMAPFILEHEADER libvisio writes before it (computeBMPDataOffset): the pixel offset past the
    info header and its palette; a BI_BITFIELDS header of 40 bytes also passes its three masks."""
    n = len(dib)
    head = _u32(dib, 0, 40)
    if head > n:
        head = 40
    off = head
    bpp = struct.unpack_from("<H", dib, 14)[0] if n >= 16 else 0
    bpp = next((b for b in (1, 4, 8, 16, 24, 32) if b >= min(bpp, 32)), 32)
    colors = _u32(dib, 32)
    if bpp < 16 and colors == 0:
        colors = 1 << bpp
    if 0 < colors < (n - off) // 4:
        off += 4 * colors
    if head == 40 and _u32(dib, 16) == 3:
        off += 12
    return b"BM" + struct.pack("<IHHI", n + 14, 0, 0, off + 14) + bytes(dib)


class _Shape:
    __slots__ = ("sid", "level", "parent", "pin", "text", "cut", "names", "order", "fields", "ftype", "fformat",
                 "data", "broken")

    def __init__(self, sid, level, parent):
        self.sid, self.level, self.parent = sid, level, parent
        self.pin = None       # [PinX, PinY, LocPinX, LocPinY], each None when absent
        self.text = None      # the text chunk's bytes, cut to MAX_TEXT characters
        self.cut = False      # the text chunk held more
        self.names = {}       # 0x2D: name id -> bytes
        self.order = None     # 0x66: field ids in order
        self.fields = {}      # 0xA1: field id -> ("s", name id) or ("n", format, cell, value)
        self.ftype = self.fformat = None
        self.data = None
        self.broken = False


class _Page:
    __slots__ = ("ptr", "title", "entries", "ids", "broken")

    def __init__(self, ptr):
        self.ptr, self.title, self.broken = ptr, "", False
        self.entries = []     # (parent, PinX, PinY, blocks) of the shapes that show something, in walk order
        self.ids = {}         # shape id -> (parent, PinX, PinY, LocPinX, LocPinY), the first shape of an id


class _Reader:
    def __init__(self, data, version, ctx):
        self.data, self.v, self.ctx = data, version, ctx
        self.wide = version == 11
        self.items = limits.BIN_RECORDS  # chunk-data items the document may still read (see `take`)
        self.items_cut = False
        self.room = limits.MAX_OUTPUT    # shape text the document may still emit (vsdx's charge)
        self.image_bytes = 0
        self.names = {}       # libvisio's m_names: 0x33 id -> its bytes; a 0x32 starts a new one
        self.index = {}       # level -> [name index chunk data, its type, the names it maps to, parsed or None]
        self.stencil = 0      # stencil (0x1D) streams entered and not left
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
        elif t == PAGE and not self.stencil:
            self.start_page(step)
        elif t in SHAPES:
            self.stream_sid = step.index

    def leave(self, step):
        t = step.pointer.type
        if t == STENCILS:
            self.stencil -= 1
        elif t == PAGE and self.pages and self.pages[-1].ptr is step.pointer:
            self.end_page()

    def chunk(self, step):
        c = step.chunk
        t = c.type
        if t == NAME_LIST2:
            self.names = {}
        elif t == NAME2:
            self.names[c.id] = bytes(c.data[:4 + 2 * vsdx.MAX_TITLE + 2])  # all name2 reads
        elif t in NAME_INDEXES:
            self.index[step.level] = [bytes(c.data), t, self.names, None]
        elif not self.pages or self.stencil:
            return
        elif t in SHAPES:
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

    # ---- pages
    def start_page(self, step):
        page = _Page(step.pointer)
        self.pages.append(page)
        self.started += 1
        ok = False
        with self.ctx.guard("page"):
            page.title = (self.page_name(step.index, step.level + 1).strip() or str(self.started))[:vsdx.MAX_TITLE]
            ok = True
        page.broken = not ok

    def end_page(self):
        if self.shape is not None:
            self.flush()
        page = self.pages.pop()
        if page.broken:
            return
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
        """{element id: name bytes} of a name index (readNameIDX: u32 count and 13-byte records; readNameIDX123: u32
        pairs to the end), the later record of an element winning."""
        out = {}
        if kind == 0xC9:
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
        """A 0x33 name: a u32, then text to its NUL, cut to MAX_TITLE characters before it is decoded."""
        if self.wide:
            body = bytes(raw[4:4 + 2 * vsdx.MAX_TITLE + 2])
            body = body[:len(body) // 2 * 2].decode("utf-16-le", "replace")
            return body.split("\x00", 1)[0]
        body = bytes(raw[4:4 + vsdx.MAX_TITLE + 1]).split(b"\x00", 1)[0]
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
            cur = rec[0]
        for node in reversed(path):
            if base is not None:
                _, x, y, lx, ly = ids[node]
                d = base[2] + 1
                base = None if d > limits.MAX_DEPTH else (
                    base[0] + (x or 0.0) - (lx or 0.0), base[1] + (y or 0.0) - (ly or 0.0), d)
            memo[node] = base
        return base

    # ---- shapes
    def start_shape(self, step):
        if self.shape is not None:
            self.flush()
        c = step.chunk
        sid = c.id if c.id != NONE else self.stream_sid
        self.stream_sid = NONE
        self.shape = _Shape(sid, step.level, _u32(c.data, 10))

    def flush(self):
        s, self.shape = self.shape, None
        if s.broken or not self.pages:
            return
        page = self.pages[-1]
        ok = False
        with self.ctx.guard("shape"):
            blocks = []
            text = self.text_of(s)
            if text:
                blocks.append(Para(vsdx._inlines(text.strip("\n"))))
            blocks.extend(self.picture(s))
            ok = True
        if not ok:
            return
        pin = s.pin or (None, None, None, None)
        if s.sid not in page.ids and (s.pin is not None or s.parent not in (0, NONE)):
            page.ids[s.sid] = (s.parent, *pin)
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
        """readText: 8 bytes skipped, the rest kept up to MAX_TEXT characters (the chunk's last wins)."""
        cap = vsdx.MAX_TEXT * (2 if self.wide else 1)
        s.text = bytes(data[8:8 + cap])
        s.cut = len(data) - 8 > cap

    def name_list(self, s, data):
        s.names = {}

    def listed(self, s, c):
        """readName (0x2D): a shape name a string field shows, kept up to MAX_TEXT characters."""
        s.names[c.id] = bytes(c.data[:vsdx.MAX_TEXT * (2 if self.wide else 1)])

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
        """readTextField (v11 and v6): the cell type at 7; a string field's name id at 8, a number's value at 8 and
        its format in the block (index 2, then 0x80 0xC2) found from 0x36 (v11) or 0x24 (v6); the first of an id
        wins."""
        if cid in s.fields or len(data) < 8:
            return
        cell = data[7]
        if cell == STRING_CELL:
            if len(data) >= 12:
                s.fields[cid] = ("s", struct.unpack_from("<i", data, 8)[0])
            return
        value = _f64(data, 8) if len(data) >= 16 else None
        if value is None:
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

    def field_value(self, s, field) -> str:
        if field[0] == "s":
            raw = s.names.get(field[1])
            if raw is None:
                return ""
            if self.wide:
                return raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace").replace("\x00", "")
            return textmod.decode_8bit(raw, LID).replace("\x00", "")
        return number(field[1], field[2], field[3])

    def text_of(self, s) -> str:
        """The shape's text with its fields' values, cut at MAX_TEXT (counted `text`), charged to the room."""
        raw = s.text
        if not raw:
            return ""
        if self.room <= 0:
            self.ctx.skipped["text"] += 1
            return ""
        if self.wide:
            pieces = raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace").split(MARK)
        else:
            pieces = [textmod.decode_8bit(p, LID) for p in raw.split(b"\x1e")]
        marks = self.take(len(pieces) - 1)
        ids = s.order if s.order else sorted(s.fields)
        values = (s.fields[i] for i in ids if i in s.fields)
        out, size, cut = [], 0, s.cut
        for k, piece in enumerate(pieces):
            if k:
                if k > marks:
                    break
                field = next(values, None)
                piece = (self.field_value(s, field) if field is not None else "") + piece
            piece = piece.replace("\x00", "")
            if size + len(piece) > vsdx.MAX_TEXT:
                out.append(piece[:vsdx.MAX_TEXT - size])
                cut = True
                break
            out.append(piece)
            size += len(piece)
        text = "".join(out)
        if cut:
            self.ctx.skipped["text"] += 1
        if not text.strip():
            return ""
        cost = len(text) + vsdx.LINE * (text.count("\n") + text.count("\r"))
        if cost > self.room:
            self.ctx.skipped["text"] += 1
            return ""
        self.room -= cost
        return text

    def picture(self, s) -> list:
        if s.ftype == 2:
            self.ctx.skipped["object"] += 1
            return []
        data = s.data
        if data is None:
            if s.ftype is not None:
                self.ctx.skipped["image"] += 1
            return []
        dib = s.ftype == 1 and s.fformat == 0
        self.image_bytes += len(data) + (14 if dib else 0)
        if self.image_bytes > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return []
        if dib:
            data = bmp_file(data)
        kind = images.sniff(data) if len(data) >= 16 else None
        if kind is None:
            self.ctx.skipped["image"] += 1
            return []
        if kind == "bmp":
            data = images.bmp_to_png(data) or data
        return [Image(data)]


# chunk type -> the _Reader method that reads it (looked up per call); _BY_CHUNK's take the chunk, the rest its data
_HANDLERS = {0x9B: "xform", 0x0E: "text", 0x2C: "name_list", 0x2D: "listed", 0x66: "field_list", 0xA1: "field",
             0x98: "foreign_type", 0x0C: "foreign_data"}
_BY_CHUNK = frozenset((0x2D, 0xA1))


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(8)
    if not is_cfb(head):
        raise ReadError("damaged", "not a Visio file")
    got = parse_compound(path, ctx)
    if isinstance(got, str):  # another format's compound file named .vsd
        return cfb_route.parse(got, path, ctx)
    return got


def parse_compound(src, ctx) -> Doc | str:
    """Read the compound file `src` (a path or its bytes, nothing is written): the Doc, or the name of the other
    parser `cfb_route` picks for a compound file that holds no VisioDocument stream. `parse` ends in it."""
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "vsd", "no VisioDocument stream")
            data = cf.read("VisioDocument") if other is None else None
    except (KeyError, CfbError):
        raise ReadError("damaged", "VisioDocument stream") from None
    if other is not None:
        return other
    version, _ = vsd_stream.header(data)
    if version == 5:
        raise ReadError("unsupported", "Visio 5 drawing")
    return _Reader(data, version, ctx).run()
