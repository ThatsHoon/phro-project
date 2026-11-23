"""HWP OLE items: a BinData OLE item to the blocks it shows, a chart's Table first of all.

The item is a 4-byte length, then a nested compound file; the length must be what follows it (dochan
hwp/charts.py). In it, OOXMLChartContents is a DrawingML chart read by charts.chart_table; Contents a Hancom-native
chart (ChartOBJ revision 1.2), ported from rhwp src/ole_chart: the probe (parser.rs), the VtDataGrid read in
archive slot order (grid.rs), the series axis from label records repeated after the grid (orientation.rs), the
title (parser.rs) and the chart type at the end of the VtChart archive (the field order of legacy_presentation.rs
parse_inner, whose whole object graph is not walked); a BIFF Workbook or Book is an embedded Excel object, read by
xls_embedded (its first table is the item's); any other item is read by embedded.storage (an equation, a package's
tables).
"""
import re
import struct
from dataclasses import dataclass

from .. import embedded, limits
from ..charts import caption, chart_table, series_table
from ..containers.cfb import Compound, is_cfb
from ..containers.zipx import parse_xml
from ..limits import ReadError
from ..model import Equation, Table
from ..numfmt import display
from . import sheet_grid, xls_embedded

# grid.rs: the classes a data grid is made of; any other name in it means the shape cannot be trusted
_GRID, _MATRIX, _COLLECTION, _OBJECT = b"VtDataGrid\0", b"VtMatrix\0", b"VtCollection\0", b"VtObject\0"
_DOUBLE, _INTEGER, _STRING, _VALUE = b"VtDouble\0", b"VtInteger\0", b"VtString\0", b"VtValue\0"
_CLASSES = frozenset((_GRID, _MATRIX, _COLLECTION, _OBJECT, _DOUBLE, _INTEGER, _STRING, _VALUE))
# the sibling objects whose names end the grid's window (grid.rs OBJECT_MARKERS)
_SIBLINGS = (b"VtBackdrop\0", b"VtBackDrop\0", b"VtChartSection\0", b"VtFootnote\0", b"VtLegend\0", b"VtPlot\0",
             b"VtPrintInformation\0", b"VtChartTitle\0", b"VtTitle\0")
_TITLES = (b"VtChartTitle\0", b"VtTitle\0")
_TITLE_SCAN = 4096   # bytes after a title marker searched for its text (a title sits right after its class)
_ECHO_FACTOR = 16    # bytes the label echo searches may scan together, per byte of the stream
# VtChChartType, MSChart's documented constants (3D and 2D bar, drawn vertical, line, area, step, combination, 2D
# pie, XY), with 10 and 11 (horizontal bar) and 13 (3D pie) from the 27-type table VtSeries' painter selectors index
# (rhwp legacy_presentation.rs reads 27 of them); anything else is unknown, as rhwp's OleChartType
_KINDS = {0: "column", 1: "column", 2: "line", 3: "line", 4: "area", 5: "area", 6: "step", 7: "step",
          8: "combo", 9: "combo", 10: "bar", 11: "bar", 13: "pie", 14: "pie", 16: "scatter"}
_LABEL = re.compile(r"[0-9A-Za-z가-힣 ,.\-_()/%]+")
_DATA_CHAR = re.compile(r"[0-9가-힣]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class _Bad(ValueError):
    """A native chart stream that does not parse."""


@dataclass
class _Text:
    """A text cell: its text and its record (the i16 length and the payload) in the stream."""
    text: str
    start: int
    end: int


@dataclass
class _Grid:
    rows: int          # rows read: all of them, or as far as the cell cap let the read go (cut)
    cols: int
    label_rows: int
    label_cols: int
    cells: dict        # (row, col) -> float | _Text
    end: int           # where the grid's window ends: its next sibling object, else the stream's end
    cut: bool

    def label(self, row, col):
        v = self.cells.get((row, col))
        return v.text if isinstance(v, _Text) else None


def ole_blocks(data: bytes, ctx, *, sheets: sheet_grid.Budget | None = None) -> list:
    """What an OLE item shows: its chart as one Table, else what embedded.storage reads in it (an Equation, a
    package's Tables). Nothing, counted under object, for an item that is not a 4-byte length and the compound file
    it gives, or that holds none of these; counted under chart for a BIFF Workbook with no table to show, a chart
    that does not parse or a chart object left unread. A chart without series is nothing, not counted, as
    charts.chart_table has it. A workbook's sheets draw on `sheets`, the document's sheet_grid.Budget (the one of `ctx`
    when None). An item reached after the document's embedded.Objects ran out of time is not read, counted under
    object."""
    objs = embedded.of(ctx)
    if objs.late():
        ctx.skipped["object"] += 1
        return []
    with objs.timed():
        return _item(data, ctx, sheets)


def _item(data: bytes, ctx, sheets) -> list:
    if len(data) < 12 or struct.unpack_from("<I", data)[0] != len(data) - 4 or not is_cfb(data[4:12]):
        ctx.skipped["object"] += 1
        return []
    if sheets is None:
        sheets = embedded.of(ctx).budget()
    try:
        with Compound(data[4:]) as cf:
            if cf.exists("OOXMLChartContents"):
                xml, native = cf.read("OOXMLChartContents"), None
            elif cf.exists("Contents") and _probe(native := cf.read("Contents")):
                xml = None
            elif cf.exists("Workbook") or cf.exists("Book"):
                before = ctx.skipped["chart"]
                found = xls_embedded.blocks(cf, ctx, sheets)
                table = next((b for b in found if isinstance(b, Table)), None)
                if table is None and ctx.skipped["chart"] == before:
                    ctx.skipped["chart"] += 1
                return [] if table is None else [table]
            else:
                got = embedded.storage(cf, ctx, sheets)
                kind = got[0] if got else ""
                if kind == "table":
                    return got[1]
                if kind == "equation":
                    if got[2]:
                        ctx.skipped["depth" if got[2] <= {"depth"} else "object"] += 1
                    return [Equation(got[1])]
                ctx.skipped["chart" if kind == "chart" else "object"] += 1
                return []
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["object"] += 1
        return []
    except (KeyError, ValueError):  # a broken chain in the nested file
        ctx.skipped["object"] += 1
        return []
    try:
        with ctx.guard("chart"):  # a chart that does not parse is one chart, never a failed section
            table = chart_table(parse_xml(xml), ctx) if xml is not None else _native(native, ctx)
            return [] if table is None else [table]
    except ReadError as e:  # XML that does not parse
        if e.kind != "damaged":
            raise
        ctx.skipped["chart"] += 1
    return []


# ---- Hancom-native charts (rhwp src/ole_chart)
def _probe(data: bytes) -> bool:
    """parser.rs probe: a legacy chart's head is 0x00010000, two sizes and the object start, and it has a grid."""
    if len(data) < 16:
        return False
    w0, _, _, w3 = struct.unpack_from("<4I", data)
    return w0 == 0x00010000 and 0x20 <= w3 < len(data) and _GRID in data


def _native(data: bytes, ctx) -> Table | None:
    """parser.rs parse_legacy_hwp_chart_contents: the grid's shape and cells make the table, its labels only name
    the series and categories."""
    g = _grid(data)
    by_rows = _series_rows(data, g)
    rows, cols = g.rows - g.label_rows, g.cols - g.label_cols
    # pos(series axis index, category axis index) -> (row, col); ls and lk are the label counts on those axes
    if by_rows:
        ls, lk, n_series, n_cats, pos = g.label_rows, g.label_cols, rows, cols, lambda a, b: (a, b)
    else:
        ls, lk, n_series, n_cats, pos = g.label_cols, g.label_rows, cols, rows, lambda a, b: (b, a)
    labels = [g.label(*pos(0, lk + k)) for k in range(n_cats)]
    named = any(t is not None for t in labels)
    cats = [t if t is not None else ("" if named else str(k + 1)) for k, t in enumerate(labels)]
    series = []
    for s in range(n_series):
        values = [g.cells.get(pos(ls + s, lk + k)) for k in range(n_cats)]
        name = g.label(*pos(ls + s, 0)) or f"Series {s + 1}"
        series.append((name, [display(v, "General") if isinstance(v, float) else "" for v in values]))
    return series_table(caption(_kind(data), _title(data)), cats, series, g.cut and by_rows, g.cut and not by_rows,
                        ctx)


class _Archive:
    """grid.rs ArchiveReader. An object is an i32 id (-1 none, an id seen before a reference to that object) and,
    the first time, its class: an i32 type id, the first time followed by a u16 length, the name and a u16 version.
    Then its fields, then its base class's. Only the grid's classes are known, each base is fixed and a cell holds
    a VtDouble, a VtInteger (an i32: Hancom's whole numbers) or a VtString, so the input cannot nest anything: the recursion is at most four classes deep."""

    def __init__(self, data: bytes, at: int):
        self.data, self.at = data, at
        self.types = {}     # type id -> class name
        self.objects = {}   # object id -> its value
        self.rows = self.cols = 0
        self.read_rows = 0  # whole rows of the matrix read
        self.cells = {}
        self.counts = None  # (label rows, label cols, data cols, data rows), after a matrix read whole

    def take(self, fmt: str, n: int):
        if self.at + n > len(self.data):
            raise _Bad("the grid runs past the stream")
        v = struct.unpack_from(fmt, self.data, self.at)[0]
        self.at += n
        return v

    def type(self) -> bytes:
        tid = self.take("<i", 4)
        name = self.types.get(tid)
        if name is None:
            n = self.take("<H", 2)
            name = self.data[self.at:self.at + n]
            if name not in _CLASSES:  # a cut name is not a class either
                raise _Bad("unknown class in the grid")
            self.at += n
            self.take("<H", 2)  # version
            self.types[tid] = name
        return name

    def base(self, expected: bytes):
        if self.type() != expected:
            raise _Bad("unexpected base class")
        return self.body(expected)

    def body(self, name: bytes):
        if name == _GRID:
            self.base(_MATRIX)
            if self.read_rows == self.rows:  # the counts follow the cells
                self.counts = tuple(self.take("<H", 2) for _ in range(4))
        elif name == _MATRIX:
            self.base(_COLLECTION)
            self.rows, self.cols = self.take("<H", 2), self.take("<H", 2)
            self.matrix()
        elif name == _COLLECTION:
            self.take("<H", 2)  # count
            self.base(_OBJECT)
        elif name == _DOUBLE:
            v = self.take("<d", 8)
            self.take("<H", 2)  # precision
            self.base(_VALUE)
            return v
        elif name == _INTEGER:
            v = float(self.take("<i", 4))
            self.base(_VALUE)
            return v
        elif name == _STRING:
            n = self.take("<h", 2)
            start = end = self.at
            text = ""
            if n > 0:
                end = self.at + n
                if end > len(self.data):
                    raise _Bad("text runs past the stream")
                text = _cell_text(self.data[self.at:end])
                self.at = end + 1  # the writer stores one byte more, its NUL
            self.base(_VALUE)
            return _Text(text, start - 2, end)
        elif name == _VALUE:
            self.base(_OBJECT)
        return None

    def matrix(self):
        """The rows * cols slots in row order, read whole rows at a time until more than CHART_CELLS cells are read
        (a table holds no more), then stopped; read_rows says how far it got."""
        rows, cols = self.rows, self.cols
        if 4 * rows * cols > len(self.data) - self.at:  # every slot takes at least its four-byte id
            raise _Bad("the grid is larger than its stream")
        read = 0
        for r in range(rows):
            if read > limits.CHART_CELLS:
                return
            for c in range(cols):
                v = self.object()
                if v is not None:
                    self.cells[r, c] = v
            read += cols
            self.read_rows = r + 1

    def object(self):
        oid = self.take("<i", 4)
        if oid == -1:
            return None
        if oid in self.objects:
            return self.objects[oid]
        name = self.type()
        if name not in (_DOUBLE, _INTEGER, _STRING):  # base checks alone would let a cell hold another grid
            raise _Bad("a cell that is not a value")
        v = self.objects[oid] = self.body(name)
        return v


def _next_sibling(data: bytes, start: int):
    found = [p for m in _SIBLINGS if (p := data.find(m, start)) >= 0]
    return min(found) if found else None


def _grid(data: bytes) -> _Grid:
    """grid.rs scan_legacy_grid: the grid declared before its name (an i32 type id and a u16 length), read as an
    archive; its dimensions and label counts must agree, and no number may sit in a label row or column."""
    marker = data.find(_GRID)
    start = marker + len(_GRID) + 6  # past the name, a u16 version and four more bytes
    if marker < 6 or start > len(data):
        raise _Bad("no grid")
    end = _next_sibling(data, start)
    a = _Archive(data, marker - 6)
    if a.type() != _GRID:
        raise _Bad("no grid")
    a.body(_GRID)
    rows, cols = a.rows, a.cols
    if rows < 2 or cols < 2:
        raise _Bad("empty grid")
    if a.counts is not None:
        label_rows, label_cols, data_cols, data_rows = a.counts
        if label_rows + data_rows != rows or label_cols + data_cols != cols:
            raise _Bad("grid shape")
        cut = False
    else:  # read only in part: its counts are past the cut, so the corpus's one label row and column are taken
        label_rows = label_cols = 1
        rows, cut = a.read_rows, True
    if any(isinstance(v, float) and (r < label_rows or c < label_cols) for (r, c), v in a.cells.items()):
        raise _Bad("a number among the labels")
    return _Grid(rows, cols, label_rows, label_cols, a.cells, len(data) if end is None else end, cut)


def _series_rows(data: bytes, g: _Grid) -> bool:
    """orientation.rs decide_series_axis: the series' names are repeated, record for record, in the series and
    legend objects after the grid, the categories' are not. Rows are the series when every row label is repeated
    and not every column label is; columns when it is the other way round; rows otherwise, as writers do. The
    evidence counts only with two or more of each (a pie's legend repeats its slices).
    Each distinct record is searched for once (a label that refers back to another is its record), the column
    labels first, and the search stops at the first label that settles it. Each search may scan the whole tail, and
    together they may scan _ECHO_FACTOR bytes per byte of the stream; past that, rows."""
    rows, cols = g.rows - g.label_rows, g.cols - g.label_cols
    if rows < 2 or cols < 2:
        return True
    tail, budget = len(data) - g.end, _ECHO_FACTOR * len(data)
    spans, records = {}, {}  # a record's place -> echoed; its bytes -> echoed

    def echoed(cell):
        """Whether the label's record is repeated after the grid; None when its search would pass the budget."""
        nonlocal budget
        v = g.cells.get(cell)
        if not isinstance(v, _Text):
            return False
        span = v.start, v.end
        if span not in spans:  # each place is sliced once, so the slices add up to at most the stream
            record = data[v.start:v.end]
            if record not in records:
                if not 0 < len(record) <= tail:
                    records[record] = False
                elif tail > budget:
                    return None
                else:
                    budget -= tail
                    records[record] = data.find(record, g.end) >= 0
            spans[span] = records[record]
        return spans[span]

    for i in range(cols):
        if not echoed((0, g.label_cols + i)):  # a column label not repeated, or no budget left: rows
            return True
    for i in range(rows):
        e = echoed((g.label_rows + i, 0))
        if e is None:
            return True
        if not e:  # every column label repeated, this row label not: columns
            return False
    return True


def _title(data: bytes) -> str:
    """parser.rs extract_chart_title: the longest plausible text record after the title object's name, up to the
    next sibling object (or VtList); searched over at most _TITLE_SCAN bytes."""
    found = [(p, m) for m in _TITLES if (p := data.find(m)) >= 0]
    if not found:
        return ""
    p, m = min(found)
    start = p + len(m) + 4
    if start > len(data):
        start = p
    end = _next_sibling(data, start)
    if end is None:
        end = data.find(b"VtList\0", start)
        end = len(data) if end < 0 else end
    end = min(end, start + _TITLE_SCAN)
    best, seen = "", set()
    at = start
    while at + 4 <= end:
        n = data[at] | data[at + 1] << 8
        if 2 <= n <= 128 and at + 2 + n <= end:
            text = _label(data[at + 2:at + 2 + n])
            if text is not None:
                if text not in seen:
                    seen.add(text)
                    if len(text) >= len(best):  # rhwp keeps the last of the longest
                        best = text
                at += 2 + n
                continue
        at += 1
    return best


def _label(payload: bytes):
    """parser.rs decode_legacy_text_payload: a text record that reads as a chart label, else None."""
    if payload.find(b"\0\0") < 2:
        return None
    text = _legacy(payload)
    if text is None:
        return None
    text = text.replace("　", " ").strip()
    plausible = (text and not text.startswith("Vt") and len(text.encode("utf-8")) <= 64
                 and _LABEL.fullmatch(text) and _DATA_CHAR.search(text))
    return text if plausible else None


def _legacy(payload: bytes):
    """legacy_string.rs: the cp949 text, two NULs, then a UTF-16LE copy and two NULs; the copy wins when it is
    there. None when the text does not decode."""
    cut = payload.find(b"\0\0")
    if cut >= 0:
        tail = payload[cut + 2:]
        if len(tail) >= 2 and tail.endswith(b"\0\0") and len(tail) % 2 == 0:
            try:
                return tail[:-2].decode("utf-16-le")
            except UnicodeDecodeError:
                return None
        payload = payload[:cut]
    try:
        return payload.decode("cp949")
    except UnicodeDecodeError:
        return None


def _cell_text(payload: bytes) -> str:
    """grid.rs decode_cell_text: empty when it does not decode or holds a control character."""
    text = _legacy(payload)
    text = "" if text is None else text.replace("　", " ").strip()
    return "" if _CONTROL.search(text) else text


def _kind(data: bytes) -> str:
    """The ChartType of the VtChart archive, which starts at offset 40 with the VtChart class (version 6) and ends
    with ChartType (i16), four bytes, its VtWindow base (version 2) declared there, that one's VtObject base and
    two bytes (legacy_presentation.rs parse_inner). Unknown when the archive does not end that way."""
    n = len(data)
    if not (n >= 56 and struct.unpack_from("<i", data, 40)[0] >= 0 and data[44:56] == b"\x08\x00VtChart\0\x06\x00"):
        return "unknown"
    p = data.rfind(b"VtWindow\0")
    if p < 68 or data[p - 2:p] != b"\x09\x00" or struct.unpack_from("<i", data, p - 6)[0] < 0:
        return "unknown"
    q = p + 11  # past the name and its u16 version
    if data[p + 9:q] != b"\x02\x00" or q + 4 > n:
        return "unknown"
    oid = struct.unpack_from("<i", data, q)[0]
    if n - q == 6:  # VtObject declared before: the id must be the one it was declared with
        decl = data.find(b"\x09\x00" + _OBJECT)
        if decl < 4 or struct.unpack_from("<i", data, decl - 4)[0] != oid:
            return "unknown"
    elif n - q != 19 or oid < 0 or data[q + 4:q + 17] != b"\x09\x00" + _OBJECT + b"\x01\x00":
        return "unknown"
    return _KINDS.get(struct.unpack_from("<h", data, p - 12)[0], "unknown")
