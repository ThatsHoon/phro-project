"""Access fixtures built in memory: a Jet3, Jet4 and ACE writer.

The layouts follow Jackcess 5.0.4 (Apache-2.0), `impl/JetFormat.java` (offsets per version), `TableImpl.java`
(`createRow`, the row and data page layout), `ColumnImpl.java` (`writeDefinition`, the 18- and 25-byte column blocks;
`encodeTextValue`, the compressed unicode form), `IndexData.java` and `IndexImpl.java` (index blocks),
`LongValueColumnImpl.java` (inline, single-row and chained long values), `PropertyMaps.java` (LvProp blobs),
`impl/complex/ComplexColumnInfoImpl.java` and `AttachmentColumnInfoImpl.java` (flat tables, the attachment wrapper),
`ComplexColumnSupport.java` (MSysComplexColumns and the type tables) and `CalculatedColumnUtil.java` (the wrapped value
of a calculated column); the Jet3 jump table is written the way `TableImpl.readJumpTableVarColOffsets` reads it.
mdbtools' `HACKING.md` (LGPL) was read as format knowledge only. `scripts/reader/formats/jet_pages.py` reads what this writes.

`jet_db(version, tables, ...)` returns the file's bytes: page 0 with its header RC4-masked (code page, db_key,
password, creation date), page 1 holding the usage map rows, the MSysObjects TDEF at page 2, then the TDEFs of
MSysQueries, MSysRelationships and the user tables in order, then data, LVAL and type 5 pages. The writer is
deterministic, so the same call with `db_key=0` gives the plain pages of a db_key file.

Values by column type: None is null; BOOL any truth value (its null-mask bit); integers for BYTE, INT, LONG,
COMPLEX, BIGINT and MONEY (raw units of 1/10,000); floats for FLOAT, DOUBLE and DATE (days since 1899-12-30); str
for TEXT and MEMO (in Jet4 and ACE a column of a user table carries the compressed unicode flag, ext 0x01, unless
`Col(compressed=False)`, and a flagged value of 1 to 1,024 characters all in U+0001-U+00FF is stored in the compressed
form, as Jackcess does; `Col(compressed="unflagged")` stores that form with the flag unset, as Access does in some
real files; the system tables' columns are unflagged plain UCS-2, as in real files); bytes for BINARY and
OLE; for NUMERIC the unscaled integer (its sign byte and four little-endian u32 parts, the most significant first),
with `Col(precision=, scale=)` in the column block. Bytes given for any other column are written as they are (fixed
columns padded or cut to their size), so a test can store any raw value. A long value (MEMO, OLE) of at most 64
bytes is inline (0x80), one of at most one LVAL row is a single row (0x40), anything longer an LVAL chain (0x00),
one row per LVAL page.

A table's `rows` may hold `Row` markers: a deleted row (0x8000), an overflow row (a 0x4000 pointer to its data, stored
flagged 0x8000 on a page of its own, as Jackcess's `updateRow` writes it) or an overflow row pointing at itself. A
`Col(deleted=True)` keeps its column number, fixed offset and variable slot, and its values are written into the rows,
but the TDEF leaves it out (num_cols drops, max_cols does not), as rows written before a column was deleted look.

Complex columns (ACE type 18): `Col(name, COMPLEX, attachments=True)` takes, per row, a list of files `(name, bytes[,
deflated])`; `Col(name, COMPLEX, multi=TEXT)` (or another type) a list of values; None is null. Each gets a complex id
(in the column block at offset 11), a flat table `f_<id>_<column>` (system flags 0x800A0000: the foreign key column
`_<column>` with ext 0x08, the type columns, the autonumber `<table>_<column>`), the shared type table
(`MSysComplexType_Attachment`, `MSysComplexType_Text`, ...) and a row of MSysComplexColumns (written only then); the
cell holds the foreign key, the row's number from 1. A file is stored in Jackcess's wrapper: a u32 type (1 deflated, 0
raw), the content's length, then the content (zlib-deflated when 1): a header (its length, 1, the extension's length
with its NUL, the extension in UTF-16 and NUL) and the file's bytes. `Col(name, type, calc=result)` is a calculated
column: ext 0xC0, variable, the property ResultType; each value is wrapped (16 zero bytes, a u32 length, the data, three
zero bytes), from bytes as given, a bool (FF or 00), text (its compressed form) or the result type's fixed bytes; a MEMO
result is then a long value. `Col(ext=)` sets the ext byte outright.
"""
import zlib
import struct
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader.containers import crypto  # noqa: E402

BOOL, BYTE, INT, LONG, MONEY, FLOAT, DOUBLE, DATE, BINARY, TEXT, OLE, MEMO = range(1, 13)
GUID, NUMERIC, BIGBIN, COMPLEX, BIGINT, EXTDATE = 15, 16, 17, 18, 19, 20

FIXED = {BOOL: 1, BYTE: 1, INT: 2, LONG: 4, MONEY: 8, FLOAT: 4, DOUBLE: 8, DATE: 8, GUID: 16, NUMERIC: 17,
         COMPLEX: 4, BIGINT: 8, EXTDATE: 42}
_PACK = {FLOAT: "<f", DOUBLE: "<d", DATE: "<d"}
LONG_VALUES = (OLE, MEMO)

DB_PARENT = 0x0F000000
TABLES, DATABASES, RELATIONSHIPS, FORMS, REPORTS, SCRIPTS, MODULES = range(0x0F000001, 0x0F000008)
CONTAINER_OF = {-32768: FORMS, -32764: REPORTS, -32766: SCRIPTS, -32761: MODULES, 8: RELATIONSHIPS}
SYSTEM = 0x80000002
CREATED = 45000.25  # the creation date at 0x72 (Jet4 and ACE), whose int part masks the password
HEADER_KEY = b"\xc7\xda\x39\x6b"
TAG, INDEX_TAG = 1625, 1923


class Col:
    """A column: `type` one of the type codes above; `length` the TDEF col_len (0: the type's default); `flags`
    the column flags, with 0x01 (fixed) set or cleared from the type; `props` its LvProp properties."""

    def __init__(self, name, type, *, length=0, flags=0x01, props=None, compressed=None, deleted=False, precision=0,
                 scale=0, ext=None, calc=None, attachments=False, multi=None):
        self.name, self.type, self.length, self.flags, self.props = name, type, length, flags, props or {}
        self.compressed, self.deleted, self.precision, self.scale = compressed, deleted, precision, scale
        self.ext, self.calc, self.attachments, self.multi = ext, calc, attachments, multi
        self.complex_id = 0


class Row:
    """A row placed specially: `kind` "deleted" (flag 0x8000), "overflow" (a 0x4000 pointer to the data on a page of
    its own) or "loop" (a 0x4000 pointer to itself, no data)."""

    def __init__(self, kind, values=()):
        assert kind in ("deleted", "overflow", "loop"), kind
        self.kind, self.values = kind, list(values)


class Tbl:
    """A table: `rows` lists of values in column order; `flags` its MSysObjects flags (0x08 hidden); `pk` the
    primary key's column names; `props` its LvProp properties; `umap` 0 or 1 forces the owned-pages map type
    (None: type 0 when the pages fit its bitmap, else type 1)."""

    def __init__(self, name, cols, rows, *, flags=0, pk=None, props=None, umap=None):
        self.name, self.cols, self.rows, self.flags = name, list(cols), list(rows), flags
        self.pk = [pk] if isinstance(pk, str) else list(pk or ())
        self.props, self.umap = props or {}, umap


class Query:
    """A saved query: `rows` its MSysQueries rows, each (Attribute, Expression, Flag, Name1, Name2[, Order[,
    LvExtra]]), written in that order (Name1 and Name2 Text, Expression Memo, LvExtra Long, as in real files);
    `flags` its MSysObjects flags (the query type)."""

    def __init__(self, name, rows, *, flags=0):
        self.name, self.rows, self.flags = name, list(rows), flags


def _codec(n):
    try:
        if n > 0:
            "".encode(f"cp{n}")
            return f"cp{n}"
    except LookupError:
        pass
    return "cp1252"


class _Writer:
    def __init__(self, version, codepage):
        self.v = version
        self.j3 = version == 0
        self.ps = 2048 if self.j3 else 4096
        self.cp = _codec(codepage)
        self.codepage = codepage
        self.pages = {}
        self.top = 3  # 0 header, 1 usage maps, 2 the MSysObjects TDEF
        self.dp_rows = 8 if self.j3 else 12  # num_rows on a data page; the row offsets follow it
        self.map_len = 5 + (128 if self.j3 else 64)  # a usage map row (Jackcess USAGE_MAP_TABLE_BYTE_LENGTH)
        self.lval_row = 2032 if self.j3 else 4076  # Jackcess MAX_LONG_VALUE_ROW_SIZE

    def alloc(self):
        self.top += 1
        return self.top - 1

    # -- text

    def name(self, s):
        if self.j3:
            b = s.encode(self.cp, "replace")
            return bytes([len(b)]) + b
        b = s.encode("utf-16-le")
        return struct.pack("<H", len(b)) + b

    def text(self, s, compressed=True):
        if self.j3:
            return s.encode(self.cp, "replace")
        if compressed and s and len(s) <= 1024 and all(1 <= ord(c) <= 0xFF for c in s):
            return b"\xff\xfe" + s.encode("latin-1")
        return s.encode("utf-16-le")

    # -- pages

    def data_page(self, tdef4, rows):
        """A data or LVAL page: `rows` bytes, or (bytes, flags) for the deleted (0x8000) and overflow (0x4000)
        bits, stored from the end of the page upward."""
        page = bytearray(self.ps)
        page[0], page[1] = 1, 1
        page[4:8] = tdef4
        struct.pack_into("<H", page, self.dp_rows, len(rows))
        pos = self.ps
        for i, row in enumerate(rows):
            data, flag = row if isinstance(row, tuple) else (row, 0)
            pos -= len(data)
            page[pos:pos + len(data)] = data
            struct.pack_into("<H", page, self.dp_rows + 2 + 2 * i, pos | flag)
        struct.pack_into("<H", page, 2, pos - (self.dp_rows + 2 + 2 * len(rows)))
        return bytes(page)

    @staticmethod
    def size(row):
        return len(row[0] if isinstance(row, tuple) else row)

    def fits(self, sizes):
        return len(sizes) <= 255 and self.dp_rows + 2 + 2 * len(sizes) + sum(sizes) <= self.ps

    def pack_rows(self, tdef, rows):
        """Data pages for `rows`; their page numbers."""
        out, batch = [], []
        for row in rows:
            if not self.fits([self.size(row)]):
                raise ValueError(f"row of {self.size(row)} bytes does not fit a page")
            if not self.fits([self.size(r) for r in batch] + [self.size(row)]):
                out.append(self.flush(tdef, batch))
                batch = []
            batch.append(row)
        if batch:
            out.append(self.flush(tdef, batch))
        return out

    def flush(self, tdef, batch):
        n = self.alloc()
        self.pages[n] = self.data_page(tdef.to_bytes(4, "little"), batch)
        return n

    # -- long values

    def long_value(self, data):
        if len(data) <= 64:
            return struct.pack("<I", len(data) | 0x80000000) + bytes(8) + data
        if len(data) <= self.lval_row:
            n = self.alloc()
            self.pages[n] = self.data_page(b"LVAL", [data])
            return struct.pack("<I", len(data) | 0x40000000) + bytes([0]) + n.to_bytes(3, "little") + bytes(4)
        size = self.lval_row - 4
        chunks = [data[i:i + size] for i in range(0, len(data), size)]
        numbers = [self.alloc() for _ in chunks]
        for k, (n, chunk) in enumerate(zip(numbers, chunks)):
            nxt = bytes(4) if k == len(chunks) - 1 else bytes([0]) + numbers[k + 1].to_bytes(3, "little")
            self.pages[n] = self.data_page(b"LVAL", [nxt + chunk])
        return struct.pack("<I", len(data)) + bytes([0]) + numbers[0].to_bytes(3, "little") + bytes(4)

    # -- tables

    def layout(self, t, system=False):
        """Column numbers, fixed offsets and variable slots, as Jackcess's TableCreator assigns them."""
        cols = []
        fixed_at = 0
        for i, c in enumerate(t.cols):
            fixed = c.type in FIXED and c.calc is None
            length = c.length or FIXED.get(c.type) or {TEXT: 255 if self.j3 else 510, BINARY: 255}.get(c.type, 0)
            flags = (c.flags & ~0x01) | (0x01 if fixed else 0) | (0x10 if system else 0)
            ext = 0x01 if (not self.j3 and not system and c.type in (TEXT, MEMO)
                           and c.compressed not in (False, "unflagged")) else 0
            if c.calc is not None:
                ext = 0xC0
            if c.ext is not None:
                ext = c.ext
            col = {"c": c, "num": i, "fixed": fixed, "len": length, "flags": flags, "ext": ext, "off_f": 0,
                   "off_v": 0}
            if fixed and c.type != BOOL:
                col["off_f"] = fixed_at
                fixed_at += FIXED[c.type]
            cols.append(col)
        var = [x for x in cols if not x["fixed"] and x["c"].type not in LONG_VALUES]
        var += [x for x in cols if not x["fixed"] and x["c"].type in LONG_VALUES]
        for k, x in enumerate(var):
            x["off_v"] = k
        return cols, var, fixed_at

    def fixed_value(self, c, v):
        size = FIXED[c.type]
        if isinstance(v, (bytes, bytearray)):
            return bytes(v[:size]).ljust(size, b"\0")
        if c.type in (BYTE, INT, LONG, MONEY, COMPLEX, BIGINT):  # two's complement, so 0x80000002 fits a LONG
            return (v & ((1 << 8 * size) - 1)).to_bytes(size, "little")
        if c.type in _PACK:
            return struct.pack(_PACK[c.type], v)
        if c.type == GUID:
            return uuid.UUID(v).bytes_le
        if c.type == NUMERIC:
            m = abs(v)
            return bytes([0x80 if v < 0 else 0]) + b"".join(
                ((m >> 32 * k) & 0xFFFFFFFF).to_bytes(4, "little") for k in (3, 2, 1, 0))
        raise ValueError(f"no value encoder for type {c.type}")

    def var_value(self, x, v):
        c = x["c"]
        if c.calc is not None:
            return self.calc_value(c, v)
        if isinstance(v, str):
            v = self.text(v, bool(x["ext"] & 0x01) or c.compressed == "unflagged")
        if c.type in LONG_VALUES:
            return self.long_value(bytes(v))
        return bytes(v)

    def calc_value(self, c, v):
        """A calculated column's value, wrapped (`CalculatedColumnUtil.wrapCalculatedValue`)."""
        t = c.calc
        if isinstance(v, (bytes, bytearray)):
            inner = bytes(v)
        elif t == BOOL:
            inner = b"\xff" if v else b"\x00"
        elif t in (TEXT, MEMO):
            inner = self.text(v, True)
        else:
            inner = self.fixed_value(Col("", t), v)
        wrapped = bytes(16) + struct.pack("<I", len(inner)) + inner + bytes(3)
        return self.long_value(wrapped) if t == MEMO else wrapped

    def row(self, t, cols, var, fixed_size, values):
        n = len(cols)
        mask = bytearray((n + 7) // 8)
        fixed = bytearray(fixed_size)
        datas = {}
        for x, v in zip(cols, list(values) + [None] * (n - len(values))):
            c = x["c"]
            if c.type == BOOL and c.calc is None:
                if v:
                    mask[x["num"] // 8] |= 1 << x["num"] % 8
                continue
            if v is None:
                continue
            mask[x["num"] // 8] |= 1 << x["num"] % 8
            if x["fixed"]:
                fixed[x["off_f"]:x["off_f"] + FIXED[c.type]] = self.fixed_value(c, v)
            else:
                datas[x["off_v"]] = self.var_value(x, v)
        head = bytes([n]) if self.j3 else struct.pack("<H", n)
        body = bytearray(head + fixed)
        if not var:
            return bytes(body + mask)
        offsets = []
        for k in range(len(var)):
            offsets.append(len(body))
            body += datas.get(k, b"")
        offsets.append(len(body))  # eod
        if not self.j3:
            trailer = struct.pack("<H", offsets[-1]) + b"".join(struct.pack("<H", o) for o in reversed(offsets[:-1]))
            return bytes(body + trailer + struct.pack("<H", len(var)) + mask)
        return bytes(body) + self.jump_trailer(offsets, len(mask)) + bytes(mask)

    def jump_trailer(self, offsets, nmask):
        """Jet3: eod and the offsets in one byte each (reversed), the jump table, the count. Jump entry j is the
        first offset index at or past (j + 1) * 256; a last entry no offset reaches is the dummy 0xFF."""
        nvar = len(offsets) - 1
        eod = offsets[-1]
        jumps = 0
        while True:
            length = eod + (nvar + 1) + jumps + 1 + nmask
            need = (length - 1) // 256
            if need == jumps:
                break
            jumps = need
        table = []
        for j in range(jumps):
            hit = next((i for i, o in enumerate(offsets) if o >= (j + 1) * 256), None)
            if hit is None:
                assert j == jumps - 1, "two dummy jump entries"
                hit = 0xFF
            table.append(hit)
        out = bytes(o & 0xFF for o in reversed(offsets))
        return out + bytes(reversed(table)) + bytes([nvar])

    def ptr(self, page, row):
        return bytes([row]) + page.to_bytes(3, "little")

    def tdef(self, t, every, var, nrows, owned, empty, system):
        j3 = self.j3
        cols = [x for x in every if not x["c"].deleted]
        idx = [x["num"] for x in cols if x["c"].name in t.pk]
        nidx = 1 if idx else 0
        longs = [x for x in cols if x["c"].type in LONG_VALUES]
        b = bytearray(b"\x02\x01" + (b"VC" if j3 else bytes(2)) + bytes(4))
        b += bytes(4)  # tdef_len
        if not j3:
            b += struct.pack("<I", TAG)
        b += struct.pack("<II", nrows, 0)
        if not j3:
            b += bytes(4) + bytes(4) + bytes(8)  # autonumber flag and zeros, complex autonumber, residue
        b += bytes([0x53 if system else 0x4E]) + struct.pack("<HHH", len(every), len(var), len(cols))
        b += struct.pack("<II", nidx, nidx) + owned + empty
        for _ in range(nidx):
            b += struct.pack("<II", nrows, nrows) + (b"" if j3 else bytes(4))
        for x in cols:
            c = x["c"]
            if c.type in (TEXT, MEMO):
                misc = struct.pack("<HH", 0x409, self.codepage & 0xFFFF) if j3 else struct.pack("<HH", 0x409, 0)
            elif c.type == NUMERIC:  # precision and scale at 11 and 12 of the column block
                misc = bytes(2) + bytes([c.precision, c.scale]) if j3 else bytes([c.precision, c.scale]) + bytes(2)
            elif c.type == COMPLEX and not j3:  # the complex id where a text column keeps its sort order
                misc = struct.pack("<I", c.complex_id)
            else:
                misc = bytes(4)
            length = 0 if c.type in LONG_VALUES else x["len"]
            if j3:
                b += (bytes([c.type]) + struct.pack("<HHH", x["num"], x["off_v"], x["num"]) + bytes(2) + misc
                      + bytes([x["flags"]]) + struct.pack("<HH", x["off_f"], length))
            else:
                b += (bytes([c.type]) + struct.pack("<IHHH", TAG, x["num"], x["off_v"], x["num"]) + misc
                      + bytes([x["flags"], x["ext"]]) + bytes(4) + struct.pack("<HH", x["off_f"], length))
        for x in cols:
            b += self.name(x["c"].name)
        if nidx:
            slots = b"".join(struct.pack("<HB", n, 1) for n in idx[:10]) + b"\xff\xff\x00" * (10 - len(idx[:10]))
            if j3:
                b += slots + empty + bytes(4) + bytes([0x81])
                b += struct.pack("<IIBIIBBB", 0, 0, 0, 0xFFFFFFFF, 0, 0, 0, 1)
            else:
                b += struct.pack("<I", INDEX_TAG) + slots + empty + bytes(4) + bytes(4) + bytes([0x81]) + bytes(5)
                b += struct.pack("<IIIBIIBBB", TAG, 0, 0, 0, 0xFFFFFFFF, 0, 0, 0, 1) + bytes(4)
            b += self.name("PrimaryKey")
        for x in longs:
            b += struct.pack("<H", x["num"]) + empty + empty
        b += b"\xff\xff"
        struct.pack_into("<I", b, 8, len(b))
        return bytes(b)

    def tdef_pages(self, size):
        if size <= self.ps:
            return 1
        return 1 + -(-(size - self.ps) // (self.ps - 8))

    def write_tdef(self, numbers, body):
        first = bytearray(body[:self.ps].ljust(self.ps, b"\0"))
        parts = [first]
        rest = body[self.ps:]
        while rest:
            parts.append(bytearray(b"\x02\x01\x00\x00" + bytes(4) + rest[:self.ps - 8].ljust(self.ps - 8, b"\0")))
            rest = rest[self.ps - 8:]
        for k, part in enumerate(parts):
            nxt = numbers[k + 1] if k + 1 < len(parts) else 0
            struct.pack_into("<I", part, 4, nxt)
            if not self.j3 and k == 0:
                struct.pack_into("<H", part, 2, max(0, self.ps - len(body) - 8))
            self.pages[numbers[k]] = bytes(part)

    # -- usage maps

    def owned_map(self, pages, umap):
        bits = (self.map_len - 5) * 8
        if not pages:
            return bytes(self.map_len)
        lo, hi = min(pages), max(pages)
        if umap == 0 or (umap is None and hi - lo < bits):
            if hi - lo >= bits:
                raise ValueError("pages do not fit an inline usage map")
            bm = bytearray(self.map_len - 5)
            for p in pages:
                bm[(p - lo) // 8] |= 1 << (p - lo) % 8
            return b"\x00" + struct.pack("<I", lo) + bytes(bm)
        per = (self.ps - 4) * 8
        slots = (self.map_len - 1) // 4
        out = bytearray(b"\x01" + bytes(4 * slots))
        for i in range(slots):
            mine = [p for p in pages if i * per <= p < (i + 1) * per]
            if not mine:
                continue
            n = self.alloc()
            page = bytearray(self.ps)
            page[0], page[1] = 5, 1
            for p in mine:
                page[4 + (p - i * per) // 8] |= 1 << (p - i * per) % 8
            self.pages[n] = bytes(page)
            struct.pack_into("<I", out, 1 + 4 * i, n)
        if any(p >= slots * per for p in pages):
            raise ValueError("pages past a reference usage map")
        return bytes(out)

    def place_maps(self, rows):
        """Put map rows on page 1, then on more pages; their (page, row) pointers."""
        where, batch, page = [], [], 1
        for r in rows:
            if not self.fits([len(x) for x in batch] + [len(r)]):
                self.pages[page] = self.data_page(bytes(4), batch)
                page, batch = self.alloc(), []
            where.append(self.ptr(page, len(batch)))
            batch.append(r)
        self.pages[page] = self.data_page(bytes(4), batch)
        return where

    # -- properties

    def props(self, table_props, col_props):
        """A table's LvProp blob: the name list chunk, then the table's own (default, 0x00) map when it has properties
        and a column (0x01) map for each column that has some; None without any."""
        maps = ([(0, "", table_props)] if table_props else []) + [(1, name, p) for name, p in col_props if p]
        if not maps:
            return None
        names = []
        for _, _, p in maps:
            names += [k for k in p if k not in names]

        def chunk(kind, body):
            return struct.pack("<IH", 6 + len(body), kind) + body

        def pname(s):
            b = s.encode(self.cp, "replace") if self.j3 else s.encode("utf-16-le")
            return struct.pack("<H", len(b)) + b

        out = bytearray(b"KKD\0" if self.j3 else b"MR2\0")
        out += chunk(0x80, b"".join(pname(n) for n in names))
        for kind, name, p in maps:
            nm = pname(name)
            body = bytearray(struct.pack("<I", 4 + len(nm)) + nm)
            for key, value in p.items():
                dtype, data = self.prop_value(key, value)
                body += struct.pack("<HBBHH", 8 + len(data), 0, dtype, names.index(key), len(data)) + data
            out += chunk(kind, bytes(body))
        return bytes(out)

    def prop_value(self, key, value):
        if isinstance(value, tuple):
            return value
        if isinstance(value, bool):
            return BOOL, b"\xff" if value else b"\x00"
        if isinstance(value, int):
            return (BYTE, struct.pack("<B", value)) if key in ("TextFormat", "DecimalPlaces", "ResultType") else \
                (LONG, struct.pack("<i", value))
        if isinstance(value, (bytes, bytearray)):
            return BINARY, bytes(value)
        data = value.encode(self.cp, "replace") if self.j3 else value.encode("utf-16-le")
        return (MEMO if key in ("Description", "Expression") else TEXT), data


CATALOG = [("Id", LONG), ("ParentId", LONG), ("Name", TEXT), ("Type", INT), ("Flags", LONG), ("DateCreate", DATE),
           ("DateUpdate", DATE), ("Owner", BINARY), ("Connect", MEMO), ("Database", MEMO), ("ForeignName", TEXT),
           ("RmtInfoShort", BINARY), ("RmtInfoLong", OLE), ("Lv", OLE), ("LvExtra", OLE), ("LvModule", OLE),
           ("LvProp", OLE)]
QUERIES = [("Attribute", BYTE), ("Expression", MEMO), ("Flag", INT), ("LvExtra", LONG), ("Name1", TEXT),
           ("Name2", TEXT), ("ObjectId", LONG), ("Order", BINARY)]
CPLX = [("ColumnName", TEXT), ("ComplexTypeObjectID", LONG), ("FlatTableID", LONG), ("ConceptualTableID", LONG),
        ("ComplexID", LONG)]
MULTI_NAMES = {BYTE: "UnsignedByte", INT: "Short", LONG: "Long", FLOAT: "IEEESingle", DOUBLE: "IEEEDouble",
               GUID: "GUID", NUMERIC: "Decimal", TEXT: "Text"}
ATTACHMENT_TYPE = [("FileURL", MEMO), ("FileName", TEXT), ("FileType", TEXT), ("FileData", OLE),
                   ("FileTimeStamp", DATE), ("FileFlags", LONG)]
ATTACHMENT_FLAT = [("FileData", OLE), ("FileFlags", LONG), ("FileName", TEXT), ("FileTimeStamp", DATE),
                   ("FileType", TEXT), ("FileURL", MEMO)]


def attachment_blob(data: bytes, ext: str, deflated: bool) -> bytes:
    """A file in Jackcess's attachment wrapper (`AttachmentColumnInfoImpl.encodeData`)."""
    e = (ext + "\0").encode("utf-16-le")
    head = struct.pack("<III", 12 + len(e), 1, len(ext) + 1) + e
    body = head + bytes(data)
    return struct.pack("<iI", 1 if deflated else 0, len(body)) + (zlib.compress(body) if deflated else body)


def _complex(tables):
    """The flat and type tables, the rows with each complex cell its foreign key (by table id), and the
    MSysComplexColumns rows as (column, type table, flat table, table, complex id) names, for the complex columns."""
    extra, specs, written, types = [], [], {}, {}
    cid = 0
    for t in tables:
        found = [(k, c) for k, c in enumerate(t.cols) if c.type == COMPLEX and (c.attachments or c.multi is not None)]
        if not found:
            continue
        rows = [Row(r.kind, r.values) if isinstance(r, Row) else list(r) for r in t.rows]
        for k, c in found:
            cid += 1
            c.complex_id = cid
            if c.attachments:
                tname, tcols, fcols = "MSysComplexType_Attachment", ATTACHMENT_TYPE, ATTACHMENT_FLAT
            else:
                tname = "MSysComplexType_" + MULTI_NAMES.get(c.multi, str(c.multi))
                tcols = fcols = [("Value", c.multi)]
            if tname not in types:
                types[tname] = Tbl(tname, [Col(n, ty) for n, ty in tcols], [], flags=0x80030000)
                extra.append(types[tname])
            cols = [Col("_" + c.name, LONG, ext=0x08)] + [Col(n, ty) for n, ty in fcols]
            cols.append(Col(f"{t.name}_{c.name}", LONG, flags=0x05))
            frows = []
            for ri, r in enumerate(rows):
                vals = r.values if isinstance(r, Row) else r
                v = vals[k] if k < len(vals) else None
                if v is None:
                    continue
                fk = ri + 1
                for item in v:
                    pk = len(frows) + 1
                    if c.attachments:
                        name, data, deflated = (tuple(item) + (False,))[:3]
                        ext = name.rpartition(".")[2] if "." in name else ""
                        frows.append([fk, attachment_blob(data, ext, deflated), 0, name, None, ext, None, pk])
                    else:
                        frows.append([fk, item, pk])
                vals[k] = fk
            flat = Tbl(f"f_{cid:032X}_{c.name}", cols, frows, flags=0x800A0000)
            extra.append(flat)
            specs.append((c.name, tname, flat.name, t.name, cid))
        written[id(t)] = rows
    return extra, specs, written


RELS = [("ccolumn", LONG), ("grbit", LONG), ("icolumn", LONG), ("szColumn", TEXT), ("szObject", TEXT),
        ("szReferencedColumn", TEXT), ("szReferencedObject", TEXT), ("szRelationship", TEXT)]


def jet_db(version: int, tables=(), *, queries=(), objects=(), relationships=(), linked=(), db_key=0, password="",
           page8=False, codepage=1252) -> bytes:
    """A whole Jet (0, 1) or ACE (2, 3, 5, 6) file. `objects`: (name, type) for forms (-32768), reports (-32764),
    macros (-32766) and modules (-32761). `relationships`: (name, table, column, ref_table, ref_column), one
    MSysRelationships row each. `linked`: (name, connect, foreign_name[, type[, database]]), type 6 by default.
    Complex columns add their flat and type tables and MSysComplexColumns (see the module's docstring).
    `page8` adds a page of type 8 to the first user table's owned pages. With `db_key`, every page but 0 is RC4
    keyed by `db_key XOR n` (in ACE too, which is how an encrypted ACE file looks to the page layer)."""
    w = _Writer(version, codepage)
    tables = list(tables)
    extra, specs, written = _complex(tables)
    tables += extra
    system = [Tbl("MSysObjects", [Col(n, t) for n, t in CATALOG], []),
              Tbl("MSysQueries", [Col(n, t) for n, t in QUERIES], []),
              Tbl("MSysRelationships", [Col(n, t) for n, t in RELS], [])]
    if specs:
        system.append(Tbl("MSysComplexColumns", [Col(n, t) for n, t in CPLX], []))
    everything = system + tables
    plans = []
    for k, t in enumerate(everything):
        cols, var, fixed = w.layout(t, system=k < len(system))
        size = len(w.tdef(t, cols, var, len(t.rows), bytes(4), bytes(4), False))
        count = w.tdef_pages(size)
        numbers = [2] + [w.alloc() for _ in range(count - 1)] if k == 0 else [w.alloc() for _ in range(count)]
        plans.append({"t": t, "cols": cols, "var": var, "fixed": fixed, "pages": numbers})
    tdef_of = {p["t"].name: p["pages"][0] for p in plans}

    def cat(oid, parent, name, kind, flags=0, connect=None, database=None, foreign=None, props=None):
        return [oid, parent, name, kind, flags, CREATED, CREATED, None, connect, database, foreign, None, None,
                None, None, None, props]

    rows = [cat(c, DB_PARENT, n, 3) for c, n in ((TABLES, "Tables"), (DATABASES, "Databases"),
                                                 (RELATIONSHIPS, "Relationships"), (FORMS, "Forms"),
                                                 (REPORTS, "Reports"), (SCRIPTS, "Scripts"), (MODULES, "Modules"))]
    rows += [cat(tdef_of[t.name], TABLES, t.name, 1, SYSTEM) for t in system]
    for t in tables:
        blob = w.props(t.props, [(c.name, c.props if c.calc is None else {**c.props, "ResultType": c.calc})
                                 for c in t.cols if not c.deleted])
        rows.append(cat(tdef_of[t.name], TABLES, t.name, 1, t.flags, props=blob))
    qrows = []
    for i, q in enumerate(queries):
        oid = 0x00100000 + i
        rows.append(cat(oid, TABLES, q.name, 5, q.flags))
        for r in q.rows:
            r = list(r) + [None] * (7 - len(r))
            qrows.append([r[0], r[1], r[2], r[6], r[3], r[4], oid, r[5]])
    for i, (name, kind) in enumerate(objects):
        rows.append(cat(0x00200000 + i, CONTAINER_OF.get(kind, TABLES), name, kind))
    for i, link in enumerate(linked):
        link = list(link) + [None] * (5 - len(link))
        rows.append(cat(0x00300000 + i, TABLES, link[0], link[3] or 6, connect=link[1], database=link[4],
                        foreign=link[2]))
    rrows = []
    for i, (name, table, column, ref_table, ref_column) in enumerate(relationships):
        rows.append(cat(0x00400000 + i, RELATIONSHIPS, name, 8))
        rrows.append([1, 0, 0, column, table, ref_column, ref_table, name])
    plans[0]["t"].rows, plans[1]["t"].rows, plans[2]["t"].rows = rows, qrows, rrows
    if specs:
        plans[3]["t"].rows = [[n, tdef_of[tn], tdef_of[fn], tdef_of[on], cid] for n, tn, fn, on, cid in specs]

    for p in plans:
        t = p["t"]
        tdef4 = p["pages"][0].to_bytes(4, "little")
        encoded, extra = [], []
        for r in written.get(id(t), t.rows):
            if not isinstance(r, Row):
                encoded.append(w.row(t, p["cols"], p["var"], p["fixed"], r))
            elif r.kind == "deleted":
                encoded.append((w.row(t, p["cols"], p["var"], p["fixed"], r.values), 0x8000))
            else:
                n = w.alloc()
                if r.kind == "overflow":
                    w.pages[n] = w.data_page(tdef4, [(w.row(t, p["cols"], p["var"], p["fixed"], r.values), 0x8000)])
                    encoded.append((w.ptr(n, 0), 0x4000))
                else:
                    w.pages[n] = w.data_page(tdef4, [(w.ptr(n, 0), 0x4000)])
                extra.append(n)
        p["data"] = w.pack_rows(p["pages"][0], encoded) + extra
    if page8:
        target = plans[len(system)] if tables else plans[0]
        n = w.alloc()
        w.pages[n] = bytes([8, 1]) + bytes(w.ps - 2)
        target["data"].append(n)

    maps = [bytes(w.map_len), bytes(w.map_len)]  # the global map, then the empty map every free-space pointer uses
    for p in plans:
        maps.append(w.owned_map(sorted(p["data"]), p["t"].umap))
    where = w.place_maps(maps)
    empty = where[1]
    for k, p in enumerate(plans):
        t = p["t"]
        body = w.tdef(t, p["cols"], p["var"], len(t.rows), where[2 + k], empty, k < len(system))
        w.write_tdef(p["pages"], body)

    page0 = bytearray(w.ps)
    page0[0:4] = b"\x00\x01\x00\x00"
    page0[4:19] = b"Standard Jet DB" if version <= 1 else b"Standard ACE DB"
    page0[0x14] = version
    struct.pack_into("<HI", page0, 0x3C, codepage & 0xFFFF, db_key)
    if w.j3:
        struct.pack_into("<H", page0, 0x3A, 0x409)  # the sort order
        pw = password.encode(w.cp, "replace")[:20].ljust(20, b"\0")
    else:
        struct.pack_into("<Id", page0, 0x6E, 0x409, CREATED)
        mask = struct.pack("<i", int(CREATED))
        pw = bytes(b ^ mask[i % 4] for i, b in enumerate(password.encode("utf-16-le")[:40].ljust(40, b"\0")))
    page0[0x42:0x42 + len(pw)] = pw
    size = 126 if w.j3 else 128
    page0[0x18:0x18 + size] = crypto.rc4(HEADER_KEY, bytes(page0[0x18:0x18 + size]))
    out = bytearray(page0)
    for n in range(1, w.top):
        page = w.pages.get(n, bytes(w.ps))
        if db_key:
            page = crypto.rc4(((db_key ^ n) & 0xFFFFFFFF).to_bytes(4, "little"), page)
        out += page
    return bytes(out)
