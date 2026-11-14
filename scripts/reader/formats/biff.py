"""BIFF8 (Excel 97-2003) records, strings that continue across CONTINUE records, the SST and the workbook globals.

Offsets follow the [MS-XLS] layouts and were checked against dochan `office_binary/xls.py` (RK numbers, BOUNDSHEET,
FORMAT, XF, DATEMODE and the SST) and anydoc `sheet/xls.rs` (the record walk and the string continuation rule).
A record body stays an offset into the one stream `bytes`; only a string split by CONTINUE is assembled.
"""
import struct
from dataclasses import dataclass, field

from .. import limits
from ..limits import ReadError
from ..numfmt import code_for

CONTINUE = 0x003C
BOF = 0x0809
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

Rec = tuple  # (record id, body offset, body size, ((offset, size) of each CONTINUE body right after it,))


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
            yield (rid, off, size, tuple(conts))
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

    def string(self) -> str:
        """An XLUnicodeString; rich-text runs and phonetic bytes are skipped."""
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
    filepass: tuple | None = None
    drawing: bytes = b""
    supbooks: list = field(default_factory=list)  # one flag per SUPBOOK: this workbook itself
    externs: list = field(default_factory=list)  # one sheet index per XTI (EXTERNSHEET), -1 when not a sheet here


def _cursor(st: Stream, rec: Rec, skip: int = 0) -> Cursor:
    return Cursor(st.data, [(rec[1] + skip, max(rec[2] - skip, 0))] + list(rec[3]))


def read_globals(st: Stream) -> Globals:
    data = st.data
    it = st.records(0)
    first = next(it, None)
    if first is None or first[0] != BOF or first[2] < 4:
        raise ReadError("damaged", "not a BIFF8 workbook")
    vers, dt = struct.unpack_from("<HH", data, first[1])
    if vers == 0x0500:
        raise ReadError("unsupported", "Excel 5.0/95 workbook")
    if vers != 0x0600 or dt != 0x0005:
        raise ReadError("damaged", "not a BIFF8 workbook")
    g = Globals()
    drawing = []
    for rec in it:
        rid, off, size = rec[0], rec[1], rec[2]
        if rid == EOF:
            break
        if rid == FILEPASS:
            g.filepass = rec
            break
        with st.ctx.guard("record"):
            _globals_record(st, g, rec, drawing)
    g.drawing = b"".join(drawing)
    return g


def _globals_record(st: Stream, g: Globals, rec: Rec, drawing: list) -> None:
    data = st.data
    rid, off, size = rec[0], rec[1], rec[2]
    if rid == BOUNDSHEET:
        try:
            if size < 8:
                raise ValueError("short BOUNDSHEET")
            pos, state, kind = struct.unpack_from("<IBB", data, off)
            name, _ = short_string(data[off + 6:off + size], 0)
            g.sheets.append(SheetRef(name, pos, state & 3, kind))
        except ValueError:
            # a damaged one keeps its place, so the sheet indexes after it (itabCur, EXTERNSHEET) stay right; its
            # offset is outside every stream, so the sheet walk counts it under `sheet`
            g.sheets.append(SheetRef("", -1, 0, 0))
    elif rid == FORMAT and size >= 4:
        fid = struct.unpack_from("<H", data, off)[0]
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
        g.supbooks.append(struct.unpack_from("<H", data, off + 2)[0] == 0x0401)
    elif rid == EXTERNSHEET and size >= 2 and not g.externs:
        body = data[off:off + size] + b"".join(data[o:o + n] for o, n in rec[3])
        n = min(struct.unpack_from("<H", body)[0], (len(body) - 2) // 6)
        for book, first, _last in struct.iter_unpack("<Hhh", body[2:2 + n * 6]):
            internal = book < len(g.supbooks) and g.supbooks[book]
            g.externs.append(first if internal and first >= 0 else -1)
    elif rid == MSODRAWINGGROUP:
        drawing.append(data[off:off + size])
        drawing.extend(data[o:o + n] for o, n in rec[3])


def fmt_code(g: Globals, ixfe: int) -> str:
    if 0 <= ixfe < len(g.xf_fmt):
        return code_for(g.xf_fmt[ixfe], g.formats)
    return "General"
