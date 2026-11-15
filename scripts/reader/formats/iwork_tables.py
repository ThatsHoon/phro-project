"""iWork tables (`TST`) to one Table each, for Numbers sheets and for the tables Pages and Keynote place.

Ported from docling (MIT) `docling/backend/iwork/cells.py` (`iwa_cell`, `iwa_legacy_cell` with its checks,
`read_decimal128`) and `docling/backend/iwork/archives.py` (`iwa_placements`, `iwa_tile_placements`,
`iwa_value_list`, `iwa_entry_string`, `iwa_entry_rich_text`), v2.150.0 (1ae470a1f4580881ed73853f64e0bf178f623c5e),
and from numbers-parser (MIT) `numbers_parser/cell.py` (`_from_storage`: the version 5 field order and the value
each cell type takes; `_unpack_decimal128`; `_custom_format`, `_format_decimal`, `_format_currency`),
`numbers_parser/model.py` (`storage_buffers`, `get_storage_buffers_for_row`, `calculate_merges_using_region_map`,
`DataLists`) and `numbers_parser/constants.py` (`FormatType`, `DECIMAL_PLACES_AUTO`, `DEFAULT_TILE_SIZE`, `EPOCH`;
the five currency symbols from `currencies.py`), v4.22.2 (f783064231a796df8ab5ef66daf7078c705df508).

Path: `TST.TableInfoArchive` (6000; in Pages `TST.WPTableInfoArchive`, 6007, whose field 1 is the 6000 message) 2 ->
model (6001: 4 the data store, 8 the name). The data store embeds 3 `TileStorage` {1 tiles {1 tileid, 2 -> Tile
6002}, 2 tile size, 256 when absent} and 1 the row headers {2 buckets -> 6006}, and names 2 the column bucket
(6006), 4 strings, 17 rich text (-> 6218 -> a text storage), 19 comments (-> 3056) and 22 formats (11 before
it) as `TableDataList`s (6005 {3 entries, 4 segments -> 6011}), and 13 the merges (6144 {1 CellRange {1 origin, 2
size}}, packed column << 16 | row). A tile row {1 tile_row_index, 6 buffer, 7 int16 offsets, 8 offsets x4} is table
row `tileid x tile size + tile_row_index` (docling adds field 1 as a first row, which is wrong past one tile); the
pre-BNC pair 3 and 4 is read only when 6 and 7 are absent. A cell is version 5 (u32 flags at 8, then the flagged
values in numbers-parser's order) or version 4, read as docling reads it.

Changes: rows are never allocated from a declared count (the model's 6 and 7 are not read); every value goes to
`sheet_grid.Grid`, whose caps stop the table, with `SHEET_SCAN` charged per unpacked cell; value lists, rich text
payloads, tiles and header buckets are read once a document by id, a tileid given twice keeps its last tile, a tile
is walked once a table and a row's offsets at most SHEET_COLS wide. Display follows the spec's table (decision 10): a number through its
format's Excel code (`format_code`, then `numfmt.display`), a date as the serial `36892 + s / 86400`, a duration as
`[h]:mm:ss`, a bool TRUE or FALSE, a formula error `#ERROR`; a format type numbers-parser renders otherwise (fraction,
scientific, base, rating, custom) shows the raw value. A cell comment becomes a footnote through the text reader's
`comment_thread`.

Charts (`chart_block`) are ported from docling `docling/backend/iwork/charts.py` (`iwa_chart`, `iwa_chart_grid`,
`iwa_chart_title`, `CHART_TYPES`) at the same pin: `TSCH.ChartDrawableArchive` (5021) extension 10000 ->
`ChartArchive` {1 type, 5 series direction, 7 grid, 10 -> 5023 title}, to `charts.series_table`. Changes from docling:
a grid past CHART_CELLS is read in part and counted `chart_cut` (docling drops its data), a series without a name is
"Series N" as in the other formats' charts, and a non-style archive is read once a document by id.
"""
import math
import struct
from decimal import Decimal
from functools import lru_cache

from .. import limits
from ..charts import series_table
from ..containers import pbwire
from ..model import Break, Cell, Hidden, NoteRef, Table
from ..numfmt import MAX_CODE, display
from . import sheet_grid
from .iwork_text import ref, refs

TABLE_SEGMENTS = 100_000   # segments of one value table, with a visited set; the rest unread, counted `cells`
TILE_SIZE, TILE_SIZE_MAX = 256, 65_536  # the tile size when absent, and the largest one read (1 at least)

TABLE_INFO, PAGES_TABLE_INFO, MODEL, TILE, DATA_LIST, HEADERS, SEGMENT, MERGES, RICH_PAYLOAD = (
    6000, 6007, 6001, 6002, 6005, 6006, 6011, 6144, 6218)

# cell types (byte 1 of a packed cell)
NUMBER, TEXT, DATE, BOOL, DURATION, ERROR, RICH, CURRENCY = 2, 3, 5, 6, 7, 8, 9, 10
NUMERIC_V4 = frozenset({NUMBER, DATE, BOOL, DURATION, CURRENCY})  # a version 4 cell carrying a double (docling)
# TSK.FormatStructArchive format types (numbers-parser FormatType)
F_BOOL, F_DECIMAL, F_CURRENCY, F_PERCENT, F_DATE, F_CHECKBOX = 1, 256, 257, 258, 261, 263
DECIMALS_AUTO = 253
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "JP¥", "KRW": "₩"}
EPOCH_SERIAL = 36892  # 2001-01-01, the iWork epoch, as a 1900-system serial
ISO_DATE, ISO_TIME = "yyyy-mm-dd", "yyyy-mm-dd hh:mm:ss"
DURATION_CODE = "[h]:mm:ss"
BOOL_CODE = '"TRUE";"FALSE";"FALSE"'  # positive TRUE, else FALSE (numbers-parser: a double above 0)
PERCENT_AUTO = "General%"  # not an Excel code: `show` scales the value by 100, shows it as General and adds "%"
DECIMAL128_BIAS = 6176
ERROR_TEXT = "#ERROR"

# version 5: (flag, width, name or None) in storage order; nothing after the comment id is read
_V5 = ((0x1, 16, "decimal"), (0x2, 8, "double"), (0x4, 8, "seconds"), (0x8, 4, "string"), (0x10, 4, "rich"),
       (0x20, 4, None), (0x40, 4, None), (0x80, 4, None), (0x100, 4, None), (0x200, 4, None), (0x400, 4, None),
       (0x800, 4, None), (0x1000, 4, None), (0x2000, 4, "number_format"), (0x4000, 4, "currency_format"),
       (0x8000, 4, "date_format"), (0x10000, 4, "duration_format"), (0x20000, 4, "text_format"),
       (0x40000, 4, "bool_format"), (0x80000, 4, "comment"))
_V5_MASK = 0xFFFFF
_ICU = {"yyyy": "yyyy", "yy": "yy", "MMMM": "mmmm", "MMM": "mmm", "MM": "mm", "M": "m", "dd": "dd", "d": "d",
        "EEEE": "dddd", "EEE": "ddd", "HH": "hh", "H": "h", "hh": "hh", "h": "h", "mm": "mm", "ss": "ss",
        "a": "AM/PM"}
_SIGN = bytes(1 if b < 0x80 else 0 for b in range(256))  # high byte of an int16: 1 where the value is not negative
_BROKEN = object()  # a cached part whose bytes could not be read
_LOST = object()  # a cell's string or rich key that its list does not hold


class _Cut(dict):
    """A value list read only up to TABLE_SEGMENTS: a key it lacks is already counted `cells`."""


def _str(v) -> str | None:
    return bytes(v).decode("utf-8", errors="replace") if isinstance(v, memoryview) else None


def decimal128(b: bytes) -> Decimal:
    """An IEEE 754-2008 decimal128 in its binary integer encoding (numbers-parser `_unpack_decimal128`, docling
    `read_decimal128`): sign bit 127, a 14-bit exponent biased by 6176, a 113-bit coefficient. ValueError for other
    than 16 bytes, or for the combination bits of an infinity or a NaN."""
    b = bytes(b)
    if len(b) != 16:
        raise ValueError("a decimal128 is 16 bytes")
    if b[15] & 0x78 == 0x78:
        raise ValueError("decimal128 infinity or NaN")
    exponent = (((b[15] & 0x7F) << 7) | (b[14] >> 1)) - DECIMAL128_BIAS
    coefficient = ((b[14] & 1) << 112) | int.from_bytes(b[:14], "little")
    if b[15] & 0x80:
        coefficient = -coefficient
    return Decimal(f"{coefficient}e{exponent}")  # scaleb would round to the context's 28 digits


def _lit(s: str) -> str:
    """Literal text in an Excel code: every character escaped."""
    return "".join("\\" + c for c in s)


def _date_code(pattern: str) -> str:
    """An ICU date pattern as an Excel code: the tokens of `_ICU`, quoted text and other characters as literals.
    Any other letter token, an unclosed quote or a code past numfmt's MAX_CODE gives ISO_DATE."""
    if len(pattern) > 2 * MAX_CODE:  # every two pattern characters give at least one code character
        return ISO_DATE
    out, i, n = [], 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "'":
            if i + 1 < n and pattern[i + 1] == "'":
                out.append(_lit("'"))
                i += 2
                continue
            j, buf = i + 1, []
            while j < n:
                if pattern[j] == "'":
                    if j + 1 < n and pattern[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(pattern[j])
                j += 1
            if j >= n:
                return ISO_DATE
            out.append(_lit("".join(buf)))
            i = j + 1
        elif ch.isascii() and ch.isalpha():
            j = i
            while j < n and pattern[j] == ch:
                j += 1
            tok = _ICU.get(pattern[i:j])
            if tok is None:
                return ISO_DATE
            out.append(tok)
            i = j
        else:
            out.append(_lit(ch))
            i += 1
    code = "".join(out)
    return code if code and len(code) <= MAX_CODE else ISO_DATE


def format_code(fmt: dict) -> str | None:
    """The Excel code `numfmt.display` shows a value with, for a `TSK.FormatStructArchive` {1 type, 2 decimals,
    3 currency code, 5 thousands, 14 ICU date pattern} as `pbwire.fields`; None to show the raw value. Decimal (256)
    is `0` or `#,##0` with the decimals (253, auto, is General); currency (257) is the symbol of USD, EUR, GBP, JPY or
    KRW, else the code and a space, before the number; percent (258) adds `%` (auto decimals give PERCENT_AUTO, which
    `show` handles); a date (261) is its ICU pattern (`_date_code`), else ISO_DATE; bool (1) and checkbox (263) are
    TRUE or FALSE. Any other type gives None (decision 10)."""
    t = pbwire.one(fmt, 1)
    if t in (F_BOOL, F_CHECKBOX):
        return BOOL_CODE
    if t == F_DATE:
        pattern = _str(pbwire.one(fmt, 14))
        return _date_code(pattern) if pattern else ISO_DATE
    if t not in (F_DECIMAL, F_CURRENCY, F_PERCENT):
        return None
    dec = pbwire.one(fmt, 2, 0)
    dec = dec if isinstance(dec, int) else 0
    number = None  # auto decimals: General
    if dec != DECIMALS_AUTO and dec <= MAX_CODE:
        thousands = pbwire.one(fmt, 5, 0)
        number = ("#,##0" if isinstance(thousands, int) and thousands else "0") + ("." + "0" * dec if dec else "")
    if t == F_DECIMAL:
        return number or "General"
    if t == F_PERCENT:
        return number + "%" if number else PERCENT_AUTO
    code = (_str(pbwire.one(fmt, 3)) or "").strip()[:16]
    symbol = SYMBOLS.get(code, code + " " if code else "")
    return _lit(symbol) + (number or "General")


def show(value: float, fmt: dict | None) -> str:
    """A number (or a bool's double) through its format; the raw value (General) without one or for a format
    `format_code` does not map."""
    return _apply(value, format_code(fmt) if fmt is not None else None)


def _apply(value: float, code: str | None) -> str:
    if code is None:
        return display(value, "General")
    if code == PERCENT_AUTO:
        return display(value * 100, "General") + "%"
    return display(value, code)


@lru_cache(maxsize=1024)
def _layout(flags: int) -> tuple[dict, int]:
    """{name: offset after the 12-byte head} of the version 5 values `flags` carries, and their total width."""
    at, where = 0, {}
    for flag, width, name in _V5:
        if flags & flag:
            if name:
                where[name] = at
            at += width
    return where, at


def _v5(buf, start: int) -> dict | None:
    """The values of a version 5 cell, None when they run past the buffer. ValueError for a bad decimal128."""
    end = len(buf)
    if start + 12 > end:
        return None
    where, total = _layout(int.from_bytes(buf[start + 8:start + 12], "little") & _V5_MASK)
    base = start + 12
    if base + total > end:
        return None
    out = {}
    for name, at in where.items():
        p = base + at
        if name == "decimal":
            out[name] = decimal128(buf[p:p + 16])
        elif name in ("double", "seconds"):
            out[name] = struct.unpack_from("<d", buf, p)[0]
        else:
            out[name] = int.from_bytes(buf[p:p + 4], "little")
    return out


def _v4(buf, start: int, ctype: int) -> dict | None:
    """A version 4 cell as docling's `iwa_legacy_cell` reads it: one 4-byte identifier per bit of the u32 at 4, an
    IEEE double before the last for a numeric type, a text cell's string key as the last. A layout that does not fit
    the bytes, or a type it does not carry, gives None."""
    if start + 12 > len(buf):
        return None
    ids = bin(int.from_bytes(buf[start + 4:start + 8], "little")).count("1")
    if not ids:
        return None
    numeric = ctype in NUMERIC_V4
    length = 12 + 4 * ids + (8 if numeric else 0)
    if start + length > len(buf):
        return None
    last = start + length - 4
    if numeric:
        return {"double": struct.unpack_from("<d", buf, last - 8)[0]}
    if ctype != TEXT:
        return None
    return {"string": int.from_bytes(buf[last:last + 4], "little")}


def _present(offsets):
    """(column, start) of each cell a row's int16 offsets place (a negative offset is an empty column), found by
    a C-speed scan of the offsets' high bytes, so a long run of empty columns costs no Python steps."""
    raw = bytes(offsets)
    n = len(raw) // 2
    marks = raw[1:2 * n:2].translate(_SIGN)
    c = marks.find(1)
    while c >= 0:
        yield c, struct.unpack_from("<h", raw, 2 * c)[0]
        c = marks.find(1, c + 1)


class _Reader:
    """One table read into one Grid. The store's `cache` keeps what tables share by id (value lists, tile rows,
    header buckets, rich text inlines); `store.scan` counts the document's unpacked cells against SHEET_SCAN."""

    def __init__(self, store, ctx, budget, text, notes):
        self.store, self.ctx, self.budget, self.text, self.notes = store, ctx, budget, text, notes
        self.cache = store.cache

    # ---- shared parts, read once a document by id
    def _cached(self, key, fn):
        """fn() once a document for `key`. A part whose bytes are broken is remembered as broken: every table that
        uses it fails (`table`) without parsing it again."""
        if key not in self.cache:
            try:
                self.cache[key] = fn()
            except ValueError:
                self.cache[key] = _BROKEN
        got = self.cache[key]
        if got is _BROKEN:
            raise ValueError(f"broken table part {key!r}")
        return got

    def values(self, ds: dict, field: int, kind: str) -> dict:
        """key -> value of the TableDataList data-store field `field` names, segments followed (at most
        TABLE_SEGMENTS, each once; past it the rest unread, counted `cells` once). Missing: {}."""
        lid = ref(ds, field)
        if lid is None:
            return {}
        return self._cached((kind, lid), lambda: self._read_list(lid, kind))

    def _read_list(self, lid, kind) -> dict:
        main = self.store.get(lid, DATA_LIST)
        if main is None:
            return {}
        parts, seen, cut = [main], {lid}, False
        for s in refs(main, 4):
            if s in seen:
                continue
            if len(seen) > TABLE_SEGMENTS:
                self.ctx.skipped["cells"] += 1
                cut = True
                break
            seen.add(s)
            try:
                f = self.store.get(s, SEGMENT)
            except ValueError:
                self.ctx.skipped["record"] += 1
                continue
            if f:
                parts.append(f)
        decode = getattr(self, "_" + kind)
        out = _Cut() if cut else {}
        for f in parts:
            for e in f.get(3, []):
                if not isinstance(e, memoryview):
                    continue
                try:
                    ef = pbwire.fields(e)
                    key, value = pbwire.one(ef, 1), decode(ef)
                except ValueError:
                    self.ctx.skipped["record"] += 1
                    continue
                if isinstance(key, int) and value is not None:
                    out[key] = value
        return out

    @staticmethod
    def _string(ef):
        return _str(pbwire.one(ef, 3))

    def _rich(self, ef):
        """A rich text entry: 9 -> TST.RichTextPayloadArchive (6218) 1 -> the storage id; each payload read once a
        document, however many entries name it."""
        pid = ref(ef, 9)
        if pid is None:
            return None
        return self._cached(("rich_payload", pid), lambda: self._payload(pid))

    def _payload(self, pid):
        """The storage id a payload names; None for a missing one, or a broken one (counted `record` once)."""
        try:
            payload = self.store.get(pid, RICH_PAYLOAD)
        except ValueError:
            self.ctx.skipped["record"] += 1
            return None
        return ref(payload, 1) if payload is not None else None

    @staticmethod
    def _comment(ef):
        return ref(ef, 10)

    @staticmethod
    def _format(ef):
        return pbwire.msg(ef, 6) or None

    def rows(self, tile_id) -> list:
        """(tile_row_index, buffer, offsets, scale, over) of each row of a tile, by index; [] for a missing tile. A
        row's offsets are cut at SHEET_COLS columns, so a row costs at most that many offsets however often its tile is
        placed; `over` says a cell is present past the cut (found once, at C speed), and the table stops `columns`."""
        def read():
            f = self.store.get(tile_id, TILE)
            if f is None:
                self.ctx.skipped["record"] += 1
                return []
            out = []
            for v in f.get(5, []):
                if not isinstance(v, memoryview):
                    continue
                r = pbwire.fields(v)
                index = pbwire.one(r, 1)
                if not isinstance(index, int):
                    continue
                buf, offs = pbwire.one(r, 6), pbwire.one(r, 7)
                wide = pbwire.one(r, 8, 0)
                scale = 4 if isinstance(wide, int) and wide else 1
                if not isinstance(buf, memoryview) or not isinstance(offs, memoryview):
                    buf, offs, scale = pbwire.one(r, 3), pbwire.one(r, 4), 1
                if isinstance(buf, memoryview) and isinstance(offs, memoryview):
                    cut = limits.SHEET_COLS * 2
                    over = len(offs) > cut and bytes(offs[cut + 1::2]).translate(_SIGN).find(1) >= 0
                    out.append((index, buf, offs[:cut], scale, over))
            out.sort(key=lambda x: x[0])
            return out
        return self._cached(("tile", tile_id), read)

    def hidden(self, bucket_ids, limit: int) -> set:
        """The indexes below `limit` that a nonzero hidingState hides, from TST.HeaderStorageBucket (6006)
        {2 headers {1 index, 3 hidingState}} objects; a bucket named twice is joined once."""
        out = set()
        for b in dict.fromkeys(bucket_ids):
            out |= self._cached(("hidden", b, limit), lambda b=b: self._bucket(b, limit))
        return out

    def _bucket(self, b, limit) -> set:
        f = self.store.get(b, HEADERS)
        found = set()
        for h in (f or {}).get(2, []):
            if not isinstance(h, memoryview):
                continue
            e = pbwire.fields(h)
            index, state = pbwire.one(e, 1), pbwire.one(e, 3, 0)
            if isinstance(index, int) and index < limit and isinstance(state, int) and state:
                found.add(index)
        return found

    def rich_inlines(self, sid) -> list:
        """A rich text storage's blocks as one cell's inlines (paragraphs joined by Break, a hidden block kept as
        Hidden); a block without inlines (a drawable) is counted `object`. Read once a document; a storage that cannot
        be read leaves its cells empty, counted `record` once."""
        def read():
            out = []
            try:
                blocks = self.text.blocks(sid, repeat=True)  # shared with the text reader's cache: only read here
            except ValueError:
                self.ctx.skipped["record"] += 1
                return []
            for b in blocks:
                inl = getattr(b, "inlines", None)
                if inl is None:
                    self.ctx.skipped["object"] += 1
                    continue
                if out:
                    out.append(Break())
                out.extend([Hidden(list(inl), b.hidden)] if b.hidden else inl)
            return out
        return self._cached(("rich_inlines", sid), read)

    # ---- the table
    def read(self, model_id) -> list:
        model = self.store.get(model_id, MODEL)
        name = (_str(pbwire.one(model, 8)) or "").strip()
        ds = pbwire.msg(model, 4)
        tiles = pbwire.msg(ds, 3)
        size = pbwire.one(tiles, 2, TILE_SIZE)
        if not isinstance(size, int) or not 1 <= size <= TILE_SIZE_MAX:
            raise ValueError(f"tile size {size!r}")
        by_tid: dict = {}  # a tileid given twice keeps its last tile, as a repeated protobuf key does
        for v in tiles.get(1, []):
            if isinstance(v, memoryview):
                e = pbwire.fields(v)
                tid, target = pbwire.one(e, 1), ref(e, 2)
                if isinstance(tid, int) and target is not None:
                    by_tid[tid] = target
        entries = sorted(by_tid.items())

        grid = sheet_grid.Grid(name or "table", self.budget, self.ctx)
        for r in self.hidden(refs(pbwire.msg(ds, 1), 2), limits.SHEET_ROWS):
            grid.hide_row(r)
        col_bucket = ref(ds, 2)
        for c in self.hidden([col_bucket] if col_bucket is not None else [], limits.SHEET_COLS):
            grid.hide_cols(c, c)
        self._merges(ds, grid)
        strings = self.values(ds, 4, "string")
        rich = self.values(ds, 17, "rich")
        comments = self.values(ds, 19, "comment")
        formats = self.values(ds, 22, "format") if ref(ds, 22) is not None else self.values(ds, 11, "format")
        marks: dict = {}
        self._cells(grid, entries, size, strings, rich, comments, formats, marks)
        self._place(grid, marks)
        out = grid.blocks()
        if not out:
            return []
        if isinstance(out[0], Table):
            out[0].caption = name
            return out[:1]
        return [Table([[Cell(out[:1])]], caption=name)]  # one cell: Grid gives a paragraph

    def _merges(self, ds, grid):
        """13 -> TST.MergeRegionMapArchive (6144) {1 CellRange {1 origin {1 packed}, 2 size {1 packed}}}, each packed
        value column << 16 | row (numbers-parser `calculate_merges_using_region_map`)."""
        mid = ref(ds, 13)
        f = self.store.get(mid, MERGES) if mid is not None else None
        for v in (f or {}).get(1, []):
            if not isinstance(v, memoryview):
                continue
            cr = pbwire.fields(v)
            origin, extent = _packed(pbwire.msg(cr, 1)), _packed(pbwire.msg(cr, 2))
            if origin is None or extent is None:
                continue
            row, col, rows, cols = origin & 0xFFFF, origin >> 16, extent & 0xFFFF, extent >> 16
            if rows and cols:
                grid.merge(row, row + rows - 1, col, col + cols - 1)

    def _cells(self, grid, entries, size, strings, rich, comments, formats, marks):
        store, cap = self.store, limits.SHEET_SCAN
        codes: dict = {}
        walked: set = set()  # a tile placed under two tileids is read at the first only, counted `record` once
        repeated = False
        for tid, tile_id in entries:
            if tile_id in walked:
                if not repeated:
                    repeated = True
                    self.ctx.skipped["record"] += 1
                continue
            walked.add(tile_id)
            for index, buf, offs, scale, over in self.rows(tile_id):
                row = tid * size + index
                if row >= limits.SHEET_ROWS:
                    grid.stop("rows", limits.SHEET_ROWS)
                    return
                for col, start in _present(offs):
                    if store.scan >= cap:  # this cell cannot be unpacked inside the cap
                        grid.stop("unpacked cells", cap)
                        return
                    store.scan += 1
                    value, comment = self._cell(buf, start * scale, strings, rich, comments, formats, codes)
                    if comment is not None:
                        marks.setdefault((row, col), []).append(comment)
                    if value and not grid.put(row, col, value):
                        return  # the grid is cut
                if over:  # a cell past SHEET_COLS, as Grid.put would stop on it
                    grid.stop("columns", limits.SHEET_COLS)
                    return

    def _cell(self, buf, start, strings, rich, comments, formats, codes):
        """(the displayed value or None, the comment storage id or None) of the cell at `start`."""
        if start >= len(buf):
            self.ctx.skipped["record"] += 1
            return None, None
        version, ctype = buf[start], buf[start + 1] if start + 1 < len(buf) else -1
        try:
            if version == 5:
                v = _v5(buf, start)
            elif version == 4:
                v = _v4(buf, start, ctype)
                if v is None:  # docling's checks: a layout that does not fit gives nothing
                    return None, None
            else:
                v = None
        except ValueError:
            v = None
        if v is None:
            self.ctx.skipped["record"] += 1
            return None, None
        comment = comments.get(v["comment"]) if "comment" in v else None
        value = self._value(v, ctype, strings, rich, formats, codes)
        lost = value is _LOST or ("comment" in v and comment is None and not isinstance(comments, _Cut))
        if value is _LOST:
            value = None
        if lost and version == 5:  # a key absent from its list, counted once a cell (v4 stays silent, as docling)
            self.ctx.skipped["record"] += 1
        return value, comment

    @staticmethod
    def _code(key, formats, codes):
        """(format type, Excel code) of a format key, computed once a table; (None, None) without one."""
        if key is None:
            return None, None
        if key not in codes:
            f = formats.get(key)
            codes[key] = (pbwire.one(f, 1), format_code(f)) if f is not None else (None, None)
        return codes[key]

    def _value(self, v, ctype, strings, rich, formats, codes):
        """The displayed value of an unpacked cell, None, or _LOST when its string or rich key is not in its list."""
        if ctype in (NUMBER, CURRENCY):
            number = v.get("decimal", v.get("double"))
            if number is None:
                return None
            return _apply(_float(number), self._code(v.get("currency_format", v.get("number_format")), formats,
                                                     codes)[1])
        if ctype == TEXT:
            if "string" not in v:
                return None
            got = strings.get(v["string"])
            return _LOST if got is None and not isinstance(strings, _Cut) else got
        if ctype == RICH:
            if "rich" not in v:
                return None
            sid = rich.get(v["rich"])
            if sid is None:
                return None if isinstance(rich, _Cut) else _LOST
            return list(self.rich_inlines(sid)) or None
        if ctype == DATE:
            s = v.get("seconds", v.get("double"))
            if s is None:
                return None
            kind, c = self._code(v.get("date_format"), formats, codes)
            c = c if kind == F_DATE else ISO_DATE
            if c == ISO_DATE and s % 86400:
                c = ISO_TIME
            return display(EPOCH_SERIAL + s / 86400, c)
        if ctype == BOOL:
            return "TRUE" if (v.get("double") or 0) > 0 else "FALSE"
        if ctype == DURATION:
            s = v.get("double")
            if s is None:
                return None
            return ("-" if s < 0 else "") + display(abs(s) / 86400, DURATION_CODE)
        if ctype == ERROR:
            return ERROR_TEXT
        return None  # empty (0) or a type not displayed

    def _place(self, grid, marks):
        """Each cell comment as a footnote mark after its cell's value (`Grid.append`, which works on a cut grid), or
        alone in an empty cell (`Grid.put`); a mark on a merged-away cell goes to the merge's origin
        (`Grid.covers`). The note comes from the text reader's `comment_thread`, made only once its mark is sure to
        be placed; a mark that cannot be (the grid cut or its cells gone) counts `note`."""
        if not marks:
            return
        moved = grid.covers(list(marks))
        placed: set = set()
        for pos in sorted(marks):
            t = moved.get(pos, pos)
            for cid in marks[pos]:
                if (t, cid) in placed:
                    continue
                placed.add((t, cid))
                has = grid.get(*t) is not None
                if not has and (grid.cut or self.budget.cells <= 0 or t[0] >= limits.SHEET_ROWS
                                or t[1] >= limits.SHEET_COLS):
                    self.ctx.skipped["note"] += 1
                    continue
                nid = self.text.comment_thread(cid)
                if nid is None or nid not in self.notes:
                    continue
                if has:
                    grid.append(t[0], t[1], [NoteRef(nid)])
                else:
                    grid.put(t[0], t[1], [NoteRef(nid)])


def _packed(f: dict) -> int | None:
    """A CellID or TableSize packedData (field 1, fixed32; a varint is taken too)."""
    v = pbwire.one(f, 1)
    if isinstance(v, bytes) and len(v) == 4:
        return int.from_bytes(v, "little")
    return v if isinstance(v, int) and v < 2**32 else None


def _float(x) -> float:
    try:
        return float(x)
    except (OverflowError, ValueError):
        return float("nan")


def table_blocks(store, table_id: int, ctx, budget: sheet_grid.Budget, text, notes: dict) -> list:
    """The table drawable `table_id` (6000, or a Pages 6007) as one Table, captioned with its name, through
    `Grid.blocks()`; [] for a table with no values. Its model is entered through `Store.follow`, so one placed twice
    is read once. A table that fails (not a table, no model, a tile size outside 1-65,536, broken bytes) is skipped
    and counts `table`. `text` is the document's `iwork_text.TextReader`, `notes` its Doc.notes."""
    out = []
    with ctx.guard("table"):
        item = store.objects.get(table_id)
        info = store.get(table_id, (TABLE_INFO, PAGES_TABLE_INFO))
        if info is None:
            raise ValueError("not a table")
        if item[0] == PAGES_TABLE_INFO:
            info = pbwire.msg(info, 1)
        model_id = ref(info, 2)
        if model_id is None or store.objects.get(model_id, (None,))[0] != MODEL:
            raise ValueError("no table model")
        reader = _Reader(store, ctx, budget, text, notes)
        for got in store.follow([model_id], lambda i, d: reader.read(i), store.depth):
            out.extend(got)
    return out


# ---- charts (TSCH), from docling `charts.py`
CHART_DRAWABLE, CHART_NON_STYLE = 5021, 5023  # TSCH.ChartDrawableArchive, TSCH.ChartNonStyleArchive
CHART_EXTENSION = 10000  # the drawable's ChartArchive, and the non-style archive's property map
SERIES_BY_ROW = 1        # TSCH.SeriesDirection: each grid row a series (else each column)
MAX_CHART_TITLE = 200    # characters of a chart title in its caption
# TSCH.ChartType -> the kind docling's CHART_TYPES gives it (3D read as the flat kind); any other value is "other"
CHART_KINDS = {1: "column", 2: "bar", 3: "line", 4: "area", 5: "pie", 6: "column", 7: "bar", 8: "area", 9: "scatter",
               10: "mixed", 11: "mixed", 12: "column", 13: "bar", 14: "line", 15: "area", 16: "pie", 17: "column",
               18: "bar", 19: "area", 20: "column", 21: "bar", 22: "bubble", 23: "scatter", 24: "bubble",
               25: "donut", 26: "donut", 27: "radar"}


def _chart_value(v) -> str:
    """A TSCH.GridValue's numeric_value (field 1, a double) as text; "" when it has none or it is not finite."""
    if not isinstance(v, memoryview):
        return ""
    x = pbwire.one(pbwire.fields(v), 1)
    if not isinstance(x, bytes) or len(x) != 8:
        return ""
    x = pbwire.f64(x)
    if not math.isfinite(x):
        return ""
    return str(int(x)) if x == int(x) and abs(x) < 1e16 else repr(x)


def _name(raw: list, i: int) -> str:
    return (_str(raw[i]) or "").strip() if i < len(raw) else ""


def _chart_title(store, nid, ctx) -> str:
    """The title a TSCH.ChartNonStyleArchive (5023) shows: its property map (10000) field 23 when field 21 (show
    title) is 1, else "". Read once a document by id; one that cannot be read counts `record` once."""
    key = ("chart_title", nid)
    if key not in store.cache:
        title = ""
        try:
            f = store.get(nid, CHART_NON_STYLE)
            if f is not None:
                props = pbwire.msg(f, CHART_EXTENSION)
                if pbwire.one(props, 21) == 1:
                    title = " ".join((_str(pbwire.one(props, 23)) or "").split())[:MAX_CHART_TITLE]
        except ValueError:
            ctx.skipped["record"] += 1
        store.cache[key] = title
    return store.cache[key]


def _chart(store, chart_id, ctx) -> Table:
    d = store.get(chart_id, CHART_DRAWABLE)
    if d is None or CHART_EXTENSION not in d:
        raise ValueError("no ChartArchive")
    c = pbwire.msg(d, CHART_EXTENSION)
    kind = pbwire.one(c, 1)
    kind = CHART_KINDS.get(kind, "other") if isinstance(kind, int) else "other"
    by_row = pbwire.one(c, 5) == SERIES_BY_ROW
    grid = pbwire.msg(c, 7)
    row_names, col_names = grid.get(1, []), grid.get(2, [])
    # whole rows are read until more than CHART_CELLS values are (an empty row counts one), as hwp_chart reads its
    # grid; one row is read to CHART_CELLS values
    rows, width, read, cut = [], 0, 0, False
    for row in grid.get(3, []):
        if read > limits.CHART_CELLS:
            cut = True
            break
        if not isinstance(row, memoryview):
            continue
        values = pbwire.fields(row).get(1, [])
        if len(values) > limits.CHART_CELLS:
            values, cut = values[:limits.CHART_CELLS], True
        rows.append([_chart_value(v) for v in values])
        width = max(width, len(rows[-1]))
        read += max(len(rows[-1]), 1)
    nid = ref(c, 10)
    title = _chart_title(store, nid, ctx) if nid is not None else ""
    caption = f"Chart: {kind} — {title}" if title else f"Chart: {kind}"
    # series are built only as far as series_table places them (GRID_COLS - 1 of them, points near CHART_CELLS)
    if by_row:
        n_series, names = len(rows), row_names
        cats = [_name(col_names, i) for i in range(width)]

        def column(s):
            return rows[s] + [""] * (width - len(rows[s]))
    else:
        n_series, names = width, col_names
        cats = [_name(row_names, r) for r in range(len(rows))]

        def column(s):
            return [row[s] if s < len(row) else "" for row in rows]
    series, points = [], len(cats)
    for s in range(min(n_series, limits.GRID_COLS)):
        if points > limits.CHART_CELLS:
            break
        values = column(s)
        series.append((_name(names, s) or f"Series {s + 1}", values))
        points += len(values)
    if not series:
        raise ValueError("a chart with no values")
    return series_table(caption, cats, series, len(series) < n_series or (cut and by_row), cut and not by_row, ctx)


def chart_block(store, chart_id: int, ctx) -> Table | None:
    """A chart drawable (TSCH.ChartDrawableArchive, 5021) as one Table through `charts.series_table`: extension
    10000 is the ChartArchive {1 type, 5 series direction, 7 grid {1 row names, 2 column names, 3 rows {1 values
    {1 numeric}}}, 10 -> 5023 title}, captioned `Chart: <type> — <title>` (the type as docling's table names it).
    By column, the default, each column is a series over the row names; by row, each row over the column names. A
    grid read only in part, or cut to CHART_CELLS, counts `chart_cut` once (in series_table). A chart that cannot be
    read or holds no values counts `chart`, as does one that would take the document past CHART_TOTAL table cells
    (each placement charged, as in the other formats)."""
    table = None
    with ctx.guard("chart"):
        table = _chart(store, chart_id, ctx)
    if table is None:
        return None
    store.chart_cells += sum(len(r) for r in table.rows)
    if store.chart_cells > limits.CHART_TOTAL:
        ctx.skipped["chart"] += 1
        return None
    return table
