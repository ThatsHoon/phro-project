"""Access databases (mdb, accdb): the catalog and the user tables, as one summary line and a section per table.

Ported from Jackcess 5.0.4 (Apache-2.0; the Maven Central sources jar), `com/healthmarketscience/jackcess/impl/`:
`DatabaseImpl.java` (`getDatabasePassword` and `getPasswordMask`, the catalog's columns, object types and flags),
`TableImpl.java` (the TDEF and its continuation pages, `readColumnDefinitions`, `readIndexDefinitions`, the row
offset table and its deleted and overflow bits, `positionAtRowData`, `getRowColumnData`,
`readJumpTableVarColOffsets`), `ColumnImpl.java` (the column block and `read`: currency, dates, extended dates,
numerics and GUIDs), `NumericColumnImpl.java` (precision and scale), `IndexData.java` and `IndexImpl.java` (the real
and logical index blocks, the primary key), `LongValueColumnImpl.java` (`readLongValue`: inline, one-row and chained
long values), `UsageMap.java` (the map row a TDEF points at), `NullMask.java` and `JetFormat.java` (the offsets of
each version). mdbtools' `HACKING.md` (LGPL) was read as format knowledge only (the ACE encryption structure whose
length is at 0x299); no code is taken from it. The page layer is `jet_pages`.

Encryption. A password at 0x42 (20 bytes in Jet3; 40 in Jet4 and ACE, XOR the int part of the creation date at 0x72,
as `getPasswordMask` reads it) is `encrypted`, "database password set", although the pages are not encrypted. ACE with
a db_key or an encryption structure is `encrypted`, "ACE encryption". A Jet3 or Jet4 db_key is decoded by `jet_pages`.

Catalog. MSysObjects (the TDEF at page 2) gives `Obj` rows, at most `JET_CATALOG` (the rest counted `record`).
Objects flagged 0x80000000 or 0x2 are system objects, never shown or counted; 0x08 marks a hidden table. Text cells,
the catalog's included, go through `Jet.text` with the column's compressed-unicode flag (ext 0x01); a column name
is read once with its TDEF.

Rows. Each data page the table's usage map owns is read: a row flagged 0x8000 is deleted (skipped, not counted), one
flagged 0x4000 is a pointer followed once, to a row that must not be a pointer again; a table reads the row a
pointer names once and keeps only the values it showed (no bytes of it), so many pointers naming one row cost a
lookup each. Fixed columns are read at their
`offset_F`, variable ones through their `offset_V` slot (Jet4's two-byte offset table, Jet3's one-byte offsets and
jump table), so rows written before a column was deleted read right. A Yes/No value is its null-mask bit. Every
offset and length is clipped to the row; a row or cell that cannot be read is counted `record`. A row visits only the
cells whose null-mask bits are set (a zero mask byte costs nothing more), each as a view into the row, never a copy.
Var slices are clipped but may overlap, so many cells can name the same bytes; the work total (below) bounds that.

Work total. Every path through the rows is charged against one total for the whole file, `limits.JET_WORK_TOTAL` or
`_PER_BYTE` times the file's bytes and 1 MiB if less, in units of about 0.2 microseconds (a byte of text decoded):
each row visited `_ROW`, each cell visited `_CELL` (given back when the cell becomes a non-empty grid cell, which
`SHEET_CELLS` bounds from there), each text byte decoded one, each LVAL row read its bytes and `_ROW_COST`. So an
empty cell, a cell that is counted `record` and a repeat of a kept value all cost what they take. A real file is
charged at most a few units a byte of itself. Past the total, the table being read stops with one `sheet_cut` (the
grid's own cut) and so does every later one; in the catalog the rest counts `record` once.

Long values. A MEMO is inline (0x80), one LVAL row (0x40) or an LVAL chain (0x00). An LVAL value read whole is
decoded once and kept by its definition, so a repeat costs its charge but no read; definitions that differ but share
a chain's tail are each charged, so the total bounds them too.

Caps. `JET_COLS` columns per TDEF (past it a user table counts `db_table`, the catalog is `damaged`), `JET_NAME`
characters per name and `JET_CELL_CHARS` per cell (cut, counted `text`), `JET_LVAL_BYTES` read per long value,
the work total per file. Every cap is read at call time, so a test can lower it.
"""
import collections
import copy
import datetime
import math
import re
import struct
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .. import limits
from ..limits import ReadError
from ..model import Doc, Para, Section, Text
from . import jet_pages, sheet_grid

JET_CATALOG = 100_000
JET_NAME = 64
JET_COLS = 1_024
JET_CELL_CHARS = 32_768
JET_LVAL_BYTES = 2 ** 20

BOOL, BYTE, INT, LONG, MONEY, FLOAT, DOUBLE, DATE, BINARY, TEXT, OLE, MEMO = range(1, 13)
GUID, NUMERIC, BIGBIN, COMPLEX, BIGINT, EXTDATE = 15, 16, 17, 18, 19, 20
TYPE_NAMES = {BOOL: "Yes/No", BYTE: "Byte", INT: "Integer", LONG: "Long", MONEY: "Currency", FLOAT: "Single",
              DOUBLE: "Double", DATE: "Date", BINARY: "Binary", TEXT: "Text", OLE: "OLE", MEMO: "Memo", GUID: "GUID",
              NUMERIC: "Decimal", BIGBIN: "Big binary", COMPLEX: "Complex", BIGINT: "BigInt",
              EXTDATE: "Extended date"}
FIXED_SIZE = {BYTE: 1, INT: 2, LONG: 4, MONEY: 8, FLOAT: 4, DOUBLE: 8, DATE: 8, GUID: 16, NUMERIC: 17, COMPLEX: 4,
              BIGINT: 8, EXTDATE: 42}

VERSION_NAMES = {0: "Access 97 database (Jet 3)", 1: "Access 2000-2003 database (Jet 4)",
                 2: "Access 2007 database (ACE 12)", 3: "Access 2010 database (ACE 14)",
                 5: "Access 2016 database (ACE 16)", 6: "Access 2019 database (ACE 17)"}
TABLE, LINKED_ODBC, QUERY, LINKED = 1, 4, 5, 6
FORM, REPORT, MACRO, MODULE = -32768, -32764, -32766, -32761
SYSTEM_FLAGS = 0x80000002
HIDDEN_FLAG = 0x08

_DATA_PAGE, _TDEF_PAGE = 0x01, 0x02
_DELETED, _OVERFLOW, _OFFSET = 0x8000, 0x4000, 0x1FFF
_UNUSED = 0xFFFF
_ROW_COST = 256  # charged on top of each LVAL row's bytes, so a chain of tiny rows costs as its steps
# the work total's units (about 0.2 microseconds each, a byte of text decoded), set from measurement above the
# dearest cell (a var GUID counted `record` through a 1 KiB slot, about 6.4 units) and row (an overflow pointer to an
# unreadable row, about 15 units); a cell that makes a grid cell is given back, the grid's caps bound those
_CELL = 8
_ROW = 16
_PER_BYTE = 10  # the total is at most this many units a byte of the file (and 1 MiB)


class _Spent(Exception):
    """The file's work total is spent: the table being read stops (one `sheet_cut`), the catalog's rest is cut."""
_PK = 1  # IndexImpl.PRIMARY_KEY_INDEX_TYPE
_ENCRYPTION_AT = 0x299  # the length of ACE's encryption structure (mdbtools HACKING.md; every real file read has 0)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_BAD = (ValueError, IndexError, struct.error, ArithmeticError)  # a row, cell or TDEF that cannot be read


class _Fmt:
    """The offsets of one version (Jackcess `JetFormat`): TDEF header, column and index blocks, data page."""

    def __init__(self, j3: bool):
        if j3:
            self.num_rows, self.num_cols, self.idx_slots, self.nidx, self.owned, self.idx_def = 12, 25, 27, 31, 35, 43
            self.idx_def_size, self.col_size, self.name_len = 8, 18, 1
            self.col_num, self.col_var, self.col_flags, self.col_ext = 1, 3, 13, None
            self.col_fixed, self.col_len = 14, 16
            self.real_block, self.real_skip, self.log_block, self.log_skip = 39, 0, 20, 0
            self.dp_rows, self.row_start = 8, 10
        else:
            self.num_rows, self.num_cols, self.idx_slots, self.nidx, self.owned, self.idx_def = 16, 45, 47, 51, 55, 63
            self.idx_def_size, self.col_size, self.name_len = 12, 25, 2
            self.col_num, self.col_var, self.col_flags, self.col_ext = 5, 7, 15, 16
            self.col_fixed, self.col_len = 21, 23
            self.real_block, self.real_skip, self.log_block, self.log_skip = 52, 4, 28, 4
            self.dp_rows, self.row_start = 12, 14
        self.precision, self.scale = 11, 12


@dataclass
class Obj:
    """One MSysObjects row."""
    id: int
    name: str
    type: int
    flags: int
    parent: int | None = None
    connect: str = ""
    foreign_name: str = ""


@dataclass
class Column:
    """One column block of a TDEF: `num` is the column number (the null-mask bit), `var` the variable offset slot
    (`offset_V`), `fixed_off` the fixed data offset (`offset_F`); flags 0x01 fixed; ext 0x01 compressed unicode."""
    name: str
    type: int
    num: int
    var: int = 0
    fixed_off: int = 0
    length: int = 0
    flags: int = 0x01
    ext: int = 0
    precision: int = 0
    scale: int = 0


@dataclass
class Tdef:
    """A table definition: its page chain, the row count it records, its columns in column-number order, the real
    indexes' column numbers, the logical indexes as (index type, real index), the primary key's column numbers and the
    owned-pages usage map."""
    page: int
    pages: list
    row_count: int
    columns: list
    real: list = field(default_factory=list)
    logical: list = field(default_factory=list)
    pk: list = field(default_factory=list)
    owned: bytes = b""
    groups: list = field(default_factory=list)  # (null-mask byte, [the (index, Column)s of each of its 8 bits])


def _u16(b, at):
    return struct.unpack_from("<H", b, at)[0]


def _u32(b, at):
    return struct.unpack_from("<I", b, at)[0]


def _need(raw: bytes, n: int) -> bytes:
    if len(raw) != n:
        raise ValueError(f"{len(raw)} bytes where {n} were expected")
    return raw


def _java_int(d: float) -> int:
    """Java's `(int)` of a double: NaN is 0, the rest truncated toward zero and saturated."""
    if d != d:
        return 0
    if d >= 2 ** 31 - 1:
        return 2 ** 31 - 1
    if d <= -2 ** 31:
        return -2 ** 31
    return int(d)


class _Db:
    """One open database: the page layer, the parse context and the version's offsets."""

    def __init__(self, jet, ctx):
        self.jet, self.ctx = jet, ctx
        self.j3 = jet.version == 0
        self.f = _Fmt(self.j3)
        self.tdef_pages = set()  # continuation pages already part of a TDEF: a chain reaching one ends there
        self.spent = 0  # work charged against total
        self.total = min(limits.JET_WORK_TOTAL, _PER_BYTE * jet.pages * jet.page_size + 2 ** 20)
        self.lval_cache = {}  # (definition, compressed) -> (text, bytes read, charge) of a long value read whole

    # -- pages and rows

    def rows_on(self, page: bytes) -> int:
        """The row count of a data page, at most what its row offset table can hold."""
        return min(_u16(page, self.f.dp_rows), (len(page) - self.f.row_start) // 2)

    def span(self, page: bytes, r: int):
        """(start, end, flags) of row `r`: it ends where row r - 1 starts, row 0 at the page end."""
        at = self.f.row_start + 2 * r
        if r < 0 or at + 2 > len(page):
            raise ValueError(f"row {r} outside its page")
        loc = _u16(page, at)
        start = loc & _OFFSET
        end = len(page) if r == 0 else _u16(page, at - 2) & _OFFSET
        if not start < end <= len(page):
            raise ValueError(f"row {r} spans {start}..{end}")
        return start, end, loc & (_DELETED | _OVERFLOW)

    def row_at(self, pg: int, r: int) -> bytes:
        """The bytes of row `r` of data page `pg`, whatever its flags (an LVAL row, an overflow target, a map)."""
        page = self.jet.page(pg)
        if page[0] != _DATA_PAGE or r >= self.rows_on(page):
            raise ValueError(f"no row {r} on page {pg}")
        start, end, _ = self.span(page, r)
        return page[start:end]

    def row_data(self, page: bytes, r: int):
        """(bytes, None) for a live row; (None, (page, row)) for an overflow row, the row its pointer names."""
        start, end, flags = self.span(page, r)
        if not flags & _OVERFLOW:
            return page[start:end], None
        if end - start < 4:
            raise ValueError("overflow row shorter than its pointer")
        return None, (int.from_bytes(page[start + 1:start + 4], "little"), page[start])

    def target(self, tp: int, tr: int) -> bytes:
        """The bytes of the row an overflow pointer names, which must not be a pointer again."""
        target = self.jet.page(tp)
        if target[0] != _DATA_PAGE or tr >= self.rows_on(target):
            raise ValueError(f"overflow row points at no row ({tp}, {tr})")
        start, end, flags = self.span(target, tr)
        if flags & _OVERFLOW:
            raise ValueError("overflow row points at another overflow row")
        return target[start:end]

    def map_row(self, ptr: bytes) -> bytes:
        """The usage map row a TDEF's four-byte pointer (row, then a three-byte page) names."""
        r, pg = ptr[0], int.from_bytes(ptr[1:4], "little")
        if pg <= 0:
            raise ValueError(f"usage map on page {pg}")
        page = self.jet.page(pg)
        start, end, _ = self.span(page, r)
        if end - start < 5:
            raise ValueError("usage map row too short")
        return page[start:end]

    # -- text

    def name(self, s: str) -> str:
        """A table or column name on one line (control characters to spaces), cut at JET_NAME, counted `text`."""
        s = _CONTROL.sub(" ", s).strip()
        if len(s) > JET_NAME:
            s = s[:JET_NAME]
            self.ctx.skipped["text"] += 1
        return s

    def cut(self, s: str, more: bool = False) -> str:
        """A cell's text, at most JET_CELL_CHARS characters; a cut (or a long value read in part) ends with "…" and is
        counted `text` once."""
        if len(s) > JET_CELL_CHARS or more:
            self.ctx.skipped["text"] += 1
            return s[:JET_CELL_CHARS] + "…"
        return s

    def charge(self, n: int):
        """Charge `n` units of work; _Spent once the file's total is passed."""
        self.spent += n
        if self.spent > self.total:
            raise _Spent

    def decode(self, raw, compressed: bool) -> str:
        """A cell's text bytes decoded, charged their length first."""
        self.charge(len(raw))
        return self.jet.text(raw, compressed)

    # -- long values

    def memo(self, raw: bytes, compressed: bool) -> str:
        """A MEMO cell's text: inline (0x80), one LVAL row (0x40) or an LVAL chain (0x00), read up to JET_LVAL_BYTES
        and no further than JET_CELL_CHARS characters can take, each charged against the file's work total. A value
        shorter than its length is counted `record` (and only that, however long its text); a cut at a cap ends with
        "…", counted `text`."""
        if len(raw) < 12:
            raise ValueError("long value definition shorter than 12 bytes")
        head = _u32(raw, 0)
        length, kind = head & 0x3FFFFFFF, head >> 30
        # two bytes a character, twice that for mode switches, FF FE
        want = min(length, JET_LVAL_BYTES, 4 * JET_CELL_CHARS + 4)
        if kind == 2:
            data = raw[12:12 + want]
            return self._whole(self.decode(data, compressed), len(data), want, length)
        if kind == 3:
            raise ValueError("long value type 0xC0")
        key = (bytes(raw[:12]), compressed)
        hit = self.lval_cache.get(key)
        if hit is not None:
            s, n, charge = hit
            self.charge(charge)
            return self._whole(s, n, want, length)
        before = self.spent
        data = self._lval((int.from_bytes(raw[5:8], "little"), raw[4]), want, kind == 1)
        s = self.jet.text(data, compressed)
        self.lval_cache[key] = (s, len(data), self.spent - before)
        return self._whole(s, len(data), want, length)

    def _whole(self, s: str, n: int, want: int, length: int) -> str:
        if n < want:  # counted once, `record`: a cut of its text is not counted again
            self.ctx.skipped["record"] += 1
            return s if len(s) <= JET_CELL_CHARS else s[:JET_CELL_CHARS] + "…"
        return self.cut(s, length > want)

    def _lval(self, start, want: int, single: bool):
        """The bytes of one LVAL row or chain, each row charged as it is read. A chain row starts with the (row,
        three-byte page) of the next, 0 ending it; `Jet.chain` keeps the seen-set, so a loop ends the value, and an
        empty chunk ends it too."""
        out = bytearray()
        rows = {}

        def step(item):
            b = rows.pop(item)
            nxt = int.from_bytes(b[1:4], "little")
            return (nxt, b[0]) if nxt else None

        for item in [start] if single else self.jet.chain(start, step):
            b = self.row_at(item[0], item[1])
            self.charge(len(b) + _ROW_COST)
            if single:
                return b[:want]
            if len(b) <= 4:
                break
            rows[item] = b
            out += b[4:4 + want - len(out)]
            if len(out) >= want:
                break
        return bytes(out)


# -- encryption

def _refuse_encrypted(jet):
    page0 = jet.page(0)
    if jet.version >= 2 and (jet.db_key or _u16(page0, _ENCRYPTION_AT)):
        raise ReadError("encrypted", "ACE encryption")
    pw = page0[0x42:0x42 + (20 if jet.version == 0 else 40)]
    if jet.version:
        mask = _java_int(struct.unpack_from("<d", page0, 0x72)[0]).to_bytes(4, "little", signed=True)
        pw = bytes(b ^ mask[i % 4] for i, b in enumerate(pw))
    if any(pw):
        raise ReadError("encrypted", "database password set")


# -- table definitions

def _tdef(db: _Db, page: int) -> Tdef:
    """The TDEF at `page` with its continuation pages. Past JET_COLS columns it is refused (ValueError, so the caller
    counts it)."""
    jet, f = db.jet, db.f
    first = jet.page(page)
    if first[0] != _TDEF_PAGE:
        raise ValueError(f"page {page} is not a table definition")
    pages, parts = [page], [first]
    for pg in jet.chain(page, jet.next_pg):
        if pg == page:
            continue
        p = jet.page(pg)
        if p[0] != _TDEF_PAGE or pg in db.tdef_pages:  # no two tables share a page, so the work stays linear
            break
        db.tdef_pages.add(pg)
        pages.append(pg)
        parts.append(p[8:])
    b = b"".join(parts)
    row_count = _u32(b, f.num_rows)
    ncols = _u16(b, f.num_cols)
    if ncols > JET_COLS:
        raise ValueError(f"{ncols} columns, past JET_COLS")
    nlog, nidx = _u32(b, f.idx_slots), _u32(b, f.nidx)
    col_at = f.idx_def + nidx * f.idx_def_size
    pos = col_at + ncols * f.col_size
    if pos > len(b):
        raise ValueError("column blocks past the table definition")
    names = []
    for _ in range(ncols):
        n = b[pos] if f.name_len == 1 else _u16(b, pos)
        pos += f.name_len
        if pos + n > len(b):
            raise ValueError("column name past the table definition")
        names.append(db.name(jet.text(b[pos:pos + n], False)))
        pos += n
    cols = []
    for i, name in enumerate(names):
        o = col_at + i * f.col_size
        cols.append(Column(name, b[o], _u16(b, o + f.col_num), _u16(b, o + f.col_var), _u16(b, o + f.col_fixed),
                           _u16(b, o + f.col_len), b[o + f.col_flags], b[o + f.col_ext] if f.col_ext else 0,
                           b[o + f.precision], b[o + f.scale]))
    cols.sort(key=lambda c: c.num)
    real = []
    for _ in range(nidx):
        if pos + f.real_block > len(b):
            raise ValueError("index block past the table definition")
        o = pos + f.real_skip
        real.append([n for n in (_u16(b, o + 3 * k) for k in range(10)) if n != _UNUSED])
        pos += f.real_block
    logical = []
    for _ in range(nlog):
        if pos + f.log_block > len(b):
            raise ValueError("logical index block past the table definition")
        o = pos + f.log_skip
        logical.append((b[o + 19], _u32(b, o + 4)))
        pos += f.log_block
    pk = next((real[d] for kind, d in logical if kind == _PK and d < len(real)), [])
    owned = db.map_row(b[f.owned:f.owned + 4])
    groups = {}
    for i, c in enumerate(cols):
        groups.setdefault(c.num >> 3, [[] for _ in range(8)])[c.num & 7].append((i, c))
    return Tdef(page, pages, row_count, cols, real, logical, pk, owned, sorted(groups.items()))


# -- rows

def _rows(db: _Db, tdef: Tdef, kept=()):
    """Each live row of the table, in page order, as (key, {column index: raw cell}) (see `_cells`): key is None, or
    for an overflow row the (tdef page, page, row) its pointer names. When `kept` holds that key the row is not read
    again and its cells are None (the caller keeps what it made of it). Only data pages naming this TDEF are read, so
    a map naming other tables' pages costs nothing. Each row visited is charged `_ROW`; one that cannot be read is
    counted `record`; a bad usage map raises ValueError for the caller."""
    jet = db.jet
    for pg in jet.usage(tdef.owned):
        page = jet.page(pg)
        if page[0] != _DATA_PAGE or _u32(page, 4) != tdef.page:
            continue
        for r in range(db.rows_on(page)):
            if _u16(page, db.f.row_start + 2 * r) & _DELETED:
                continue
            db.charge(_ROW)
            try:
                buf, ptr = db.row_data(page, r)
                key = None if ptr is None else (tdef.page,) + ptr
                if key is not None and key in kept:
                    yield key, None
                    continue
                cells = _cells(db, tdef, buf if ptr is None else db.target(*ptr))
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            yield key, cells


def _cells(db: _Db, tdef: Tdef, buf: bytes) -> dict:
    """One row's raw cells as {column index: a memoryview into the row, or True for Yes/No}: the column count, the
    fixed data at offset_F, the variable data through offset_V, the null mask at the end; offsets clipped to the row.
    Only the columns whose null-mask bits are set (and under the row's count) are visited, each charged `_CELL`; a
    null or "No" cell, or one past the count, is absent."""
    head = 1 if db.j3 else 2
    n = len(buf)
    if n < head:
        raise ValueError("row shorter than its column count")
    ncols = buf[0] if db.j3 else _u16(buf, 0)
    mlen = (ncols + 7) // 8
    end = n - mlen  # the null mask is the row's last bytes
    if end < head:
        raise ValueError("null mask past the row")
    mask = buf[end:]
    row = memoryview(buf)
    var = None
    out = {}
    for byte, bits in tdef.groups:
        if byte >= mlen:
            break
        m = mask[byte]
        if not m:
            continue
        for bit in range(8):
            if not m >> bit & 1:
                continue
            for i, c in bits[bit]:
                if c.num >= ncols:
                    continue
                if c.type == BOOL:
                    out[i] = True
                elif c.flags & 0x01:
                    a = head + c.fixed_off
                    out[i] = row[min(a, end):min(a + FIXED_SIZE.get(c.type, c.length), end)]
                else:
                    if var is None:
                        var = (_jump_offsets if db.j3 else _var_offsets)(row, head, end)
                    v = var(c.var)
                    if v is not None:
                        out[i] = v
    db.charge(_CELL * len(out))
    return out


def _var_offsets(buf: bytes, head: int, end: int):
    """Jet4 and ACE: the variable column count before the null mask, the two-byte offsets before it (reversed), the
    end of the data before them. Returns slot -> bytes (None past the row's count)."""
    if end - 2 < head:
        raise ValueError("no variable offset table")
    nvar = _u16(buf, end - 2)
    data_end = end - 4 - 2 * nvar  # the end-of-data offset
    if data_end < head:
        raise ValueError("variable offset table past the row")

    def get(i):
        if i >= nvar:
            return None
        at = end - 4 - 2 * i
        s, e = _u16(buf, at), _u16(buf, at - 2)
        s, e = max(s, head), min(e, data_end)
        return buf[s:e] if s < e else b""
    return get


def _jump_offsets(buf: bytes, head: int, end: int):
    """Jet3: one-byte offsets, each past 256 * the jump table entries reached (`readJumpTableVarColOffsets`)."""
    row_end = len(buf) - 1
    nvar = buf[end - 1]
    jumps = (len(buf) - 1) // 256
    col_offset = row_end - (len(buf) - end) - jumps - 1
    if col_offset - nvar < head:
        raise ValueError("variable offsets past the row")
    if (col_offset - nvar) // 256 < jumps:  # the last jump entry is a dummy
        jumps -= 1
    offsets, used = [], 0
    for i in range(nvar + 1):
        while used < jumps and i == buf[end - used - 2]:
            used += 1
        offsets.append(buf[col_offset - i] + used * 256)
    data_end = col_offset - nvar

    def get(i):
        if i >= nvar:
            return None
        s, e = max(offsets[i], head), min(offsets[i + 1], data_end)
        return buf[s:e] if s < e else b""
    return get


# -- values

def value(db: _Db, col: Column, raw):
    """A cell as shown: Yes/No, integers, Currency, Single and Double, Date, extended date, Decimal, GUID, Text (cut at
    JET_CELL_CHARS, counted `text`), Memo, OLE (its length, counted `object`) and binary (its length). Raw bytes that
    do not fit the type raise ValueError (the caller counts `record`)."""
    t = col.type
    if t == BOOL:
        return "Yes" if raw else "No"
    if raw is None:
        return ""
    if t == BYTE:
        return str(_need(raw, 1)[0])
    if t in (INT, LONG, BIGINT, COMPLEX):
        return str(struct.unpack({INT: "<h", LONG: "<i", BIGINT: "<q", COMPLEX: "<i"}[t], raw)[0])
    if t == MONEY:
        return format(Decimal(f"{struct.unpack('<q', raw)[0]}e-4").normalize(), "f")
    if t == FLOAT:
        return _single(bytes(_need(raw, 4)))
    if t == DOUBLE:
        return repr(struct.unpack("<d", raw)[0])
    if t == DATE:
        return _date(struct.unpack("<d", raw)[0])
    if t == EXTDATE:
        return _ext_date(bytes(_need(raw, 42)))
    if t == NUMERIC:
        return _numeric(_need(raw, 17), col.scale)
    if t == GUID:
        return "{" + str(uuid.UUID(bytes_le=bytes(_need(raw, 16)))).upper() + "}"
    if t == TEXT:
        return db.cut(db.decode(raw, bool(col.ext & 0x01)))
    if t == MEMO:
        return db.memo(raw, bool(col.ext & 0x01))
    if t == OLE:
        if len(raw) < 4:
            raise ValueError("OLE definition shorter than its length")
        db.ctx.skipped["object"] += 1
        return f"<OLE {_u32(raw, 0) & 0x3FFFFFFF} bytes>"
    return f"<binary {len(raw)} bytes>"


def _single(raw: bytes) -> str:
    """The shortest %g form that packs back to the same float32."""
    f = struct.unpack("<f", raw)[0]
    if not math.isfinite(f):
        return repr(f)
    for p in range(1, 10):
        s = "%.*g" % (p, f)
        try:
            if struct.pack("<f", float(s)) == raw:
                return s
        except OverflowError:
            continue
    return repr(f)


def _date(d: float) -> str:
    """Days since 1899-12-30; the fraction is a positive part of the day whatever the sign (Jackcess
    `fromLocalDateDouble`). The time is left out at midnight."""
    if not math.isfinite(d):
        raise ValueError("date is not a number")
    day = int(d)
    secs = round(abs(d) % 1.0 * 86400)
    if secs >= 86400:
        day, secs = day + 1, secs - 86400
    dt = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=day, seconds=secs)
    return _iso(dt, 0)


def _ext_date(raw: bytes) -> str:
    """ACE's extended date: 19 digits of days since 0001-01-01, ':', 12 digits of seconds, 7 of 100 ns, ':7' and NUL
    (`readExtendedDateValue`)."""
    if raw[19:20] != b":" or not (raw[:19].isdigit() and raw[20:39].isdigit()):
        raise ValueError("extended date is not digits")
    dt = datetime.datetime(1, 1, 1) + datetime.timedelta(days=int(raw[:19]), seconds=int(raw[20:32]))
    return _iso(dt, int(raw[32:39]))


def _iso(dt: datetime.datetime, frac7: int) -> str:
    s = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    if dt.hour or dt.minute or dt.second or frac7:
        s += f" {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        if frac7:
            s += f".{frac7:07d}".rstrip("0")
    return s


def _numeric(raw: bytes, scale: int) -> str:
    """A sign byte, then 16 bytes as four little-endian u32s, the most significant first; exact, at the column's
    scale."""
    n = 0
    for k in range(4):
        n = n << 32 | _u32(raw, 1 + 4 * k)
    return format(Decimal(f"{-n if raw[0] else n}e-{scale}"), "f")


# -- catalog

def _catalog(db: _Db) -> list:
    """MSysObjects as `Obj` rows, at most JET_CATALOG (one more row counts `record` and ends the read)."""
    try:
        tdef = _tdef(db, 2)
    except _BAD as e:
        raise ReadError("damaged", f"jet: no readable catalog ({e})") from None
    at = {c.name.lower(): i for i, c in reversed(list(enumerate(tdef.columns)))}
    if not all(k in at for k in ("id", "name", "type", "flags")):
        raise ReadError("damaged", "jet: the catalog has no Id, Name, Type or Flags column")
    cols = tdef.columns

    def cell(cells, key, default=None):
        i = at.get(key)
        return default if i is None else (cells.get(i), cols[i])

    out, read = [], 0
    try:
        for _, cells in _rows(db, tdef):  # each catalog cell is charged; none is given back
            if read >= JET_CATALOG:
                db.ctx.skipped["record"] += 1
                break
            read += 1
            try:
                obj = _obj(db, cells, cell)
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            if obj is not None:
                out.append(obj)
    except _Spent:
        db.ctx.skipped["record"] += 1  # the rest of the catalog, once
    except _BAD as e:
        raise ReadError("damaged", f"jet: no readable catalog ({e})") from None
    return out


def _obj(db, cells, cell):
    (oid, _), (name, ncol), (kind, _), (flags, _) = (cell(cells, k) for k in ("id", "name", "type", "flags"))
    if oid is None or name is None or kind is None or flags is None:
        return None
    kind = struct.unpack("<h", kind)[0]
    flags = struct.unpack("<I", flags)[0]
    parent = cell(cells, "parentid", (None, None))[0]
    obj = Obj(struct.unpack("<i", oid)[0], db.name(db.decode(name, bool(ncol.ext & 0x01))), kind, flags,
              struct.unpack("<i", parent)[0] if parent is not None else None)
    if kind in (LINKED, LINKED_ODBC) and not flags & SYSTEM_FLAGS:
        raw, ccol = cell(cells, "connect", (None, None))
        if raw is not None:
            obj.connect = value(db, ccol, raw) if ccol.type in (TEXT, MEMO) else ""
        raw, fcol = cell(cells, "foreignname", (None, None))
        if raw is not None and fcol.type in (TEXT, MEMO):
            obj.foreign_name = value(db, fcol, raw)
    return obj


# -- output

def _hide(blocks: list) -> list:
    """Hidden copies of a hidden table's blocks, so showing hidden content marks them (as xls's hidden sheets)."""
    out = []
    for b in blocks:
        c = copy.copy(b)
        c.hidden = "hidden"
        out.append(c)
    return out


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _shown(db: _Db, cols: list, cells: dict, bools: list) -> list:
    """A row's non-empty values as (column index, text); a Yes/No cell absent from the row is "No". A visited cell
    that gives a value has its `_CELL` given back: the grid charges it from there."""
    out = []
    for i, raw in cells.items():
        try:
            v = value(db, cols[i], raw)
        except _BAD:
            db.ctx.skipped["record"] += 1
            continue
        if v:
            out.append((i, v))
    db.spent -= _CELL * len(out)
    for i in bools:
        if i not in cells:
            out.append((i, "No"))
    return out


def _table(db: _Db, obj: Obj, budget) -> Section:
    tdef = _tdef(db, obj.id & 0xFFFFFF)
    title = f"{obj.name} ({_count(tdef.row_count, 'row', 'rows')})"
    schema = " · ".join(f"{c.name} ({TYPE_NAMES.get(c.type, f'type {c.type}')})" for c in tdef.columns)
    grid = sheet_grid.Grid(obj.name, budget, db.ctx)
    for i, c in enumerate(tdef.columns):
        if not grid.put(0, i, c.name):
            break
    cols = tdef.columns
    bools = [i for i, c in enumerate(cols) if c.type == BOOL]  # "No" past a row's count: each one a grid cell
    done = {}  # an overflow target's key -> (its non-empty values, the counts making them added); no bytes kept
    r = 1
    try:
        for key, cells in _rows(db, tdef, done):
            if grid.cut:
                break
            if cells is None:  # a row many pointers name: read once, its values and counts repeated for each
                shown, made = done[key]
                db.ctx.skipped.update(made)
            elif key is not None:
                before = collections.Counter(db.ctx.skipped)
                shown = _shown(db, cols, cells, bools)
                done[key] = shown, db.ctx.skipped - before
            else:
                shown = _shown(db, cols, cells, bools)
            for i, v in shown:
                if not grid.put(r, i, v):
                    break
            r += 1
    except _Spent:
        if not grid.cut:
            grid.stop("units of work", db.total)
    blocks = [Para([Text("Columns: " + schema)])] + grid.blocks()
    if obj.flags & HIDDEN_FLAG:
        return Section("table", title, blocks=_hide(blocks), hidden="hidden")
    return Section("table", title, blocks=blocks)


def parse_bytes(data, ctx) -> Doc:
    """An Access database held in memory: the summary line, then a table section per user table."""
    jet = jet_pages.open(data)
    _refuse_encrypted(jet)
    db = _Db(jet, ctx)
    objs = [o for o in _catalog(db) if not o.flags & SYSTEM_FLAGS]
    tables = [o for o in objs if o.type == TABLE]
    queries = [o for o in objs if o.type == QUERY]
    linked = [o for o in objs if o.type in (LINKED, LINKED_ODBC)]
    others = [o for o in objs if o.type in (FORM, REPORT, MACRO, MODULE)]
    budget = sheet_grid.Budget(limits.SHEET_CELLS, limits.SHEET_CHARS)
    out, seen = [], set()
    for o in tables:
        page = o.id & 0xFFFFFF
        if page in seen:  # a second catalog row for one TDEF
            ctx.skipped["record"] += 1
            continue
        seen.add(page)
        sec = None
        with ctx.guard("db_table"):
            sec = _table(db, o, budget)
        if sec is not None:
            out.append(sec)
    if tables and not out and not (queries or linked or others):
        raise ReadError("damaged", "no table could be read")
    parts = [_count(len(seen), "table", "tables")]
    if queries:
        parts.append(_count(len(queries), "query", "queries"))
    if linked:
        parts.append(_count(len(linked), "linked table", "linked tables"))
    line = f"{VERSION_NAMES[jet.version]}: {', '.join(parts)}."
    return Doc(blocks=[Para([Text(line)])] + out)


def parse(path, ctx) -> Doc:
    return parse_bytes(Path(path).read_bytes(), ctx)
