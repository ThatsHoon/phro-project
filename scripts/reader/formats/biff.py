"""BIFF8 (Excel 97-2003) records, strings that continue across CONTINUE records, the SST and the workbook globals.

Offsets follow the [MS-XLS] layouts and were checked against dochan `office_binary/xls.py` (RK numbers, BOUNDSHEET,
FORMAT, XF, DATEMODE and the SST) and anydoc `sheet/xls.rs` (the record walk and the string continuation rule).
A record body stays an offset into the one stream `bytes`; only a string split by CONTINUE is assembled.

A BIFF5 (Excel 5.0/95) workbook has the same records with byte strings in the CODEPAGE's code page: a sheet name
and a format code take a one-byte length, a cell's text a two-byte one, and no string has a flags byte. Its globals
keep that code page in `ansi` (None for BIFF8); its names, external sheets and drawing records differ and are not
read, each NAME counted under `fact`.
"""
import codecs
import struct
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import limits
from ..limits import ReadError
from ..numfmt import code_for

CONTINUE = 0x003C
BOF = 0x0809
CHART_BOF = 0x0020  # the BOF substream type (dt) of a chart
EOF = 0x000A
FILEPASS = 0x002F
DATEMODE = 0x0022
WINDOW1 = 0x003D
BOUNDSHEET = 0x0085
FORMAT = 0x041E
XF = 0x00E0
SST = 0x00FC
MSODRAWINGGROUP = 0x00EB
SUPBOOK = 0x01AE
EXTERNSHEET = 0x0017
NAME = 0x0018
PROTECT = 0x0012
WINDOWPROTECT = 0x0019
CODEPAGE = 0x0042
_CODECS = {1200: "utf-16-le", 367: "ascii", 32768: "mac_roman"}
_BUILTIN = ("Consolidate_Area", "Auto_Open", "Auto_Close", "Extract", "Database", "Criteria", "Print_Area",
            "Print_Titles", "Recorder", "Data_Form", "Auto_Activate", "Auto_Deactivate", "Sheet_Title",
            "_FilterDatabase")

class Rec(NamedTuple):
    """One record of a stream; a plain 4-tuple unpacks the same way."""
    id: int
    off: int       # body offset in the stream
    size: int      # body size
    conts: tuple   # (offset, size) of each CONTINUE body right after it


class Stream:
    """One BIFF stream. Every walk over it draws on the same `limits.BIN_RECORDS`."""

    def __init__(self, data: bytes, ctx):
        self.data = data
        self.ctx = ctx
        self.count = 0

    def records(self, start: int, end: int | None = None):
        data = self.data
        end = len(data) if end is None else min(end, len(data))
        pos = start
        while pos < end:
            if self.count >= limits.BIN_RECORDS:
                self.ctx.skipped["record"] += 1
                return
            rec = self._one(pos, end)
            if rec is None:
                self.ctx.skipped["record"] += 1
                return
            rid, off, size = rec
            self.count += 1  # the record itself first, so its CONTINUEs cannot take the last place and leave it over
            nxt = off + size
            conts = []
            cut = False
            while nxt < end and data[nxt] == CONTINUE and nxt + 1 < end and data[nxt + 1] == 0 \
                    and self.count < limits.BIN_RECORDS:
                c = self._one(nxt, end)
                if c is None:
                    cut = True
                    break
                self.count += 1
                conts.append((c[1], c[2]))
                nxt = c[1] + c[2]
            yield Rec(rid, off, size, tuple(conts))
            if cut:
                self.ctx.skipped["record"] += 1
                return
            pos = nxt

    def _one(self, pos: int, end: int):
        if pos + 4 > end:
            return None
        rid, size = struct.unpack_from("<HH", self.data, pos)
        if pos + 4 + size > end:
            return None
        return rid, pos + 4, size


class Cursor:
    """Reads across the segments of one logical record: its body and the CONTINUE bodies after it."""

    def __init__(self, data: bytes, segs):
        self.data = data
        self.segs = list(segs)
        self.si = 0
        self.off, self.end = self.segs[0] if self.segs else (0, 0)
        if self.segs:
            self.end = self.off + self.end

    def _next_seg(self) -> bool:
        if self.si + 1 >= len(self.segs):
            return False
        self.si += 1
        off, size = self.segs[self.si]
        self.off, self.end = off, off + size
        return True

    def _fill(self) -> bool:
        """Move to a segment that has bytes left; False when none does."""
        while self.off >= self.end:
            if not self._next_seg():
                return False
        return True

    def at_end(self) -> bool:
        return not self._fill()

    def read(self, n: int) -> bytes:
        if self.off + n <= self.end:
            b = self.data[self.off:self.off + n]
            self.off += n
            return b
        out = []
        while n > 0:
            if not self._fill():
                raise ValueError("past the end of the record")
            take = min(n, self.end - self.off)
            out.append(self.data[self.off:self.off + take])
            self.off += take
            n -= take
        return b"".join(out)

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def skip(self, n: int) -> None:
        while n > 0:
            if not self._fill():
                raise ValueError("past the end of the record")
            take = min(n, self.end - self.off)
            self.off += take
            n -= take

    def _hop(self) -> bool:
        """Move to the next segment that has bytes (an empty CONTINUE has none, so no flags byte); False if none."""
        while self._next_seg():
            if self.off < self.end:
                return True
        return False

    def chars(self, n: int, wide: bool) -> str:
        """`n` characters; a segment boundary inside them starts with a flags byte that picks the width again
        (MS-XLS: the fHighByte byte of the CONTINUE). An empty CONTINUE is passed over. A 16-bit character whose
        low byte ends one segment takes its high byte from the byte after the next segment's flags byte; if that
        flags byte says 8-bit the character cannot be finished, and the read fails."""
        out = []
        while n > 0:
            if self.off >= self.end:
                if not self._hop():
                    raise ValueError("past the end of the record")
                wide = bool(self.data[self.off] & 1)
                self.off += 1
                continue
            unit = 2 if wide else 1
            take = min(n, (self.end - self.off) // unit)
            if take == 0:
                low = self.data[self.off]
                self.off += 1
                if not self._hop() or not self.data[self.off] & 1:
                    raise ValueError("a character split by a segment")
                self.off += 1
                if self.off >= self.end:
                    raise ValueError("a character split by a segment")
                out.append(bytes([low, self.data[self.off]]))
                self.off += 1
                n -= 1
                continue
            chunk = self.data[self.off:self.off + take * unit]
            self.off += take * unit
            n -= take
            out.append(chunk if wide else chunk.decode("latin-1").encode("utf-16-le"))
        return b"".join(out).decode("utf-16-le", "replace")

    def string(self, ansi: str | None = None) -> str:
        """An XLUnicodeString; rich-text runs and phonetic bytes are skipped. With a BIFF5 code page `ansi`, a
        two-byte length and that many bytes in it."""
        if ansi:
            return self.read(self.u16()).decode(ansi, "replace")
        n = self.u16()
        flags = self.u8()
        runs = self.u16() if flags & 0x08 else 0
        ext = self.u32() if flags & 0x04 else 0
        text = self.chars(n, bool(flags & 1))
        self.skip(runs * 4 + ext)
        return text


def short_string(data: bytes, off: int) -> tuple[str, int]:
    """A ShortXLUnicodeString at `off` and the offset after it."""
    if off + 2 > len(data):
        raise ValueError("short string past the end")
    n, flags = data[off], data[off + 1]
    unit = 2 if flags & 1 else 1
    end = off + 2 + n * unit
    if end > len(data):
        raise ValueError("short string past the end")
    raw = data[off + 2:end]
    return (raw.decode("utf-16-le", "replace") if unit == 2 else raw.decode("latin-1")), end


def sst(cur: Cursor) -> list[str]:
    """The strings of an SST record. cstUnique only caps the count; it never sizes anything."""
    cur.u32()
    unique = cur.u32()
    out: list[str] = []
    while len(out) < unique and not cur.at_end():
        try:
            out.append(cur.string())
        except ValueError:
            break
    return out


def rk(v: int) -> float | int:
    """An RkNumber: a 30-bit integer or the high 30 bits of a double, optionally divided by 100."""
    if v & 2:
        n = v >> 2
        if n & 0x20000000:
            n -= 0x40000000
        out = n
    else:
        out = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return out / 100 if v & 1 else out


@dataclass
class SheetRef:
    name: str
    offset: int
    state: int  # 0 visible, 1 hidden, 2 very hidden
    kind: int   # 0 sheet, 1 macro, 2 chart, 6 VBA


@dataclass
class Globals:
    sheets: list = field(default_factory=list)
    sst: list = field(default_factory=list)
    xf_fmt: list = field(default_factory=list)
    formats: dict = field(default_factory=dict)
    date1904: bool = False
    active: int = 0
    filepass: Rec | None = None
    drawing: bytes = b""
    supbooks: list = field(default_factory=list)  # one flag per SUPBOOK: this workbook itself
    externs: list = field(default_factory=list)  # one sheet index per XTI (EXTERNSHEET), -1 when not a sheet here
    externs_last: list = field(default_factory=list)  # the last sheet index of each XTI, -1 when not a sheet here
    names: list = field(default_factory=list)  # one DefinedName per NAME (Lbl) record, in order
    books: list = field(default_factory=list)
    protect: list = field(default_factory=list)
    ansi: str | None = None


@dataclass
class DefinedName:
    name: str
    flags: int    # Lbl grbit: 0x01 hidden, 0x20 built-in
    itab: int     # 0 for the workbook, else the one-based sheet index it is local to
    rgce: bytes   # the parsed formula
    extra: bytes  # its extra data


def _cursor(st: Stream, rec: Rec, skip: int = 0) -> Cursor:
    return Cursor(st.data, [(rec.off + skip, max(rec.size - skip, 0))] + list(rec.conts))


def read_globals(st: Stream) -> Globals:
    data = st.data
    it = st.records(0)
    first = next(it, None)
    if first is None or first.id != BOF or first.size < 4:
        raise ReadError("damaged", "not a BIFF8 workbook")
    vers, dt = struct.unpack_from("<HH", data, first.off)
    if vers not in (0x0500, 0x0600) or dt != 0x0005:
        raise ReadError("damaged", "not a BIFF8 workbook")
    g = Globals(ansi="cp1252" if vers == 0x0500 else None)
    drawing = []
    for rec in it:
        rid = rec.id
        if rid == EOF:
            break
        if rid == FILEPASS:
            g.filepass = rec
            break
        with st.ctx.guard("record"):
            _globals_record(st, g, rec, drawing)
    g.drawing = b"".join(drawing)
    return g


def _codec(n: int) -> str:
    """The Python codec of a CODEPAGE value; cp1252 for one Python does not know."""
    name = _CODECS.get(n, f"cp{n}")
    try:
        codecs.lookup(name)
    except LookupError:
        return "cp1252"
    return name


def _globals_record(st: Stream, g: Globals, rec: Rec, drawing: list) -> None:
    data = st.data
    rid, off, size = rec.id, rec.off, rec.size
    if g.ansi and rid in (SST, SUPBOOK, EXTERNSHEET, NAME, MSODRAWINGGROUP):
        if rid == NAME:
            st.ctx.skipped["fact"] += 1
        return
    if rid == CODEPAGE and g.ansi and size >= 2:
        g.ansi = _codec(struct.unpack_from("<H", data, off)[0])
    elif rid == BOUNDSHEET:
        try:
            if size < 8:
                raise ValueError("short BOUNDSHEET")
            pos, state, kind = struct.unpack_from("<IBB", data, off)
            if g.ansi:
                n = data[off + 6]
                if 7 + n > size:
                    raise ValueError("short BOUNDSHEET")
                name = data[off + 7:off + 7 + n].decode(g.ansi, "replace")
            else:
                name, _ = short_string(data[off + 6:off + size], 0)
            g.sheets.append(SheetRef(name, pos, state & 3, kind))
        except ValueError:
            # a damaged one keeps its place, so the sheet indexes after it (itabCur, EXTERNSHEET) stay right; its
            # offset is outside every stream, so the sheet walk counts it under `sheet`
            g.sheets.append(SheetRef("", -1, 0, 0))
    elif rid == FORMAT and size >= 3:
        fid = struct.unpack_from("<H", data, off)[0]
        if g.ansi:
            g.formats[fid] = data[off + 3:off + 3 + data[off + 2]].decode(g.ansi, "replace")
        elif size >= 4:
            g.formats[fid] = _cursor(st, rec, 2).string()
    elif rid == XF and size >= 4:
        g.xf_fmt.append(struct.unpack_from("<H", data, off + 2)[0])
    elif rid == DATEMODE and size >= 2:
        g.date1904 = bool(struct.unpack_from("<H", data, off)[0])
    elif rid == WINDOW1 and size >= 12:
        g.active = struct.unpack_from("<H", data, off + 10)[0]
    elif rid == SST and not g.sst:  # kept: a later SST fills an empty first one (a damaged first must not hide it)
        g.sst = sst(_cursor(st, rec))
    elif rid == SUPBOOK and size >= 4:
        cch = struct.unpack_from("<H", data, off + 2)[0]
        g.supbooks.append(cch == 0x0401)
        if cch not in (0x0401, 0x3A01):
            try:
                cur = _cursor(st, rec, 4)
                g.books.append(virtual_path(cur.chars(cch, bool(cur.u8() & 1))))
            except ValueError:
                g.books.append("")
    elif rid in (PROTECT, WINDOWPROTECT) and size >= 2:
        if struct.unpack_from("<H", data, off)[0]:
            g.protect.append("workbook structure" if rid == PROTECT else "workbook windows")
    elif rid == EXTERNSHEET and size >= 2 and not g.externs:
        body = data[off:off + size] + b"".join(data[o:o + n] for o, n in rec.conts)
        n = min(struct.unpack_from("<H", body)[0], (len(body) - 2) // 6)
        for book, first, last in struct.iter_unpack("<Hhh", body[2:2 + n * 6]):
            internal = book < len(g.supbooks) and g.supbooks[book]
            g.externs.append(first if internal and first >= 0 else -1)
            g.externs_last.append(last if internal and last >= first >= 0 else -1)
    elif rid == NAME:
        g.names.append(_name(data[off:off + size] + b"".join(data[o:o + n] for o, n in rec.conts)))
    elif rid == MSODRAWINGGROUP:
        drawing.append(data[off:off + size])
        drawing.extend(data[o:o + n] for o, n in rec.conts)


def virtual_path(s: str) -> str:
    """A SUPBOOK's VirtualPath ([MS-XLS] 2.5.277) as a path: 0x01 and a drive letter (`@` a UNC share), 0x02 the
    root of the same drive, 0x03 a separator, 0x04 the parent folder, 0x05 a length and a URL or UNC name kept as
    it is; 0x06 to 0x08 (Excel's startup and library folders) are left out."""
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\x01" and i + 1 < len(s):
            out.append("\\\\" if s[i + 1] == "@" else s[i + 1] + ":" + ("" if s[i + 2:i + 3] == "\x03" else "\\"))
            i += 2
            continue
        if ch == "\x05" and i + 1 < len(s):
            n = ord(s[i + 1])
            out.append(s[i + 2:i + 2 + n])
            i += 2 + n
            continue
        out.append({"\x02": "\\", "\x03": "\\", "\x04": "..\\"}.get(ch, "" if ch in "\x06\x07\x08" else ch))
        i += 1
    return "".join(out)


def _name(body: bytes) -> DefinedName:
    """A Lbl record ([MS-XLS] 2.4.150): grbit, chKey, cch, cce, two reserved bytes, itab, four reserved bytes, the
    name (a flag byte, then cch characters; a built-in name is one code) and cce bytes of formula. A damaged one keeps
    its place with an empty name, so a PtgName index after it stays right."""
    if len(body) < 15:
        return DefinedName("", 0, 0, b"", b"")
    flags, cch, cce, itab = struct.unpack_from("<HxBH2xH", body)
    wide = body[14] & 1
    end = 15 + cch * (2 if wide else 1)
    raw = body[15:end]
    if len(raw) != end - 15 or len(body) < end + cce:
        return DefinedName("", 0, 0, b"", b"")
    name = raw.decode("utf-16-le" if wide else "latin-1")
    if flags & 0x20 and len(name) == 1:
        code = ord(name)
        name = _BUILTIN[code] if code < len(_BUILTIN) else name
    return DefinedName(name, flags, itab, body[end:end + cce], body[end + cce:])


def fmt_code(g: Globals, ixfe: int) -> str:
    if 0 <= ixfe < len(g.xf_fmt):
        return code_for(g.xf_fmt[ixfe], g.formats)
    return "General"
