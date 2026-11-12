"""HWP OLE charts: the BinData OLE item (a 4-byte length, then a nested compound file) as a table.

Run: python -I -X utf8 tests/test_reader_hwp_chart.py
"""
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.formats import hwp, hwp_chart  # noqa: E402
from reader.formats import hwp_records as hr  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import hwp_para, hwp_record, make_cfb, make_hwp  # noqa: E402

FFFC = "￼"
CHART_XML = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
             '<c:barChart><c:ser><c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>S</c:v></c:pt></c:strCache>'
             '</c:strRef></c:tx><c:cat><c:strRef><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>A</c:v></c:pt>'
             '</c:strCache></c:strRef></c:cat><c:val><c:numRef><c:numCache><c:ptCount val="1"/>'
             '<c:pt idx="0"><c:v>5</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart>'
             '</c:plotArea></c:chart></c:chartSpace>').encode()


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def i16(n):
    return struct.pack("<h", n)


def i32(n):
    return struct.pack("<i", n)


def deflate(b):
    c = zlib.compressobj(wbits=-15)
    return c.compress(b) + c.flush()


# ---- the OLE item
def envelope(streams):
    """A BinData OLE item: the nested compound file's length (u32), then the file."""
    path = tmp() / "ole.cfb"
    make_cfb(path, streams)
    data = path.read_bytes()
    return struct.pack("<I", len(data)) + data


class Record(bytes):
    """A VtString cell whose record payload is these bytes, as they are."""


class Spy(bytes):
    """A stream that counts the substring searches made in it and the bytes each may scan."""
    calls = scanned = 0

    def find(self, sub, start=0, *rest):
        self.calls += 1
        self.scanned += len(self) - start
        return super().find(sub, start, *rest)


class Archive:
    """Writes the VtArchive grammar rhwp's grid.rs reads (its tests' synth_grid): an object is an i32 id and, the
    first time, its class (an i32 type id, the first time followed by a u16 length, the name and a u16 version),
    its fields, then its base class's."""

    def __init__(self):
        self.out = bytearray()
        self.types = []
        self.next_object = 1000

    def ty(self, name: bytes, version=1):
        if name in self.types:
            self.out += i32(self.types.index(name) + 1)
            return
        self.types.append(name)
        self.out += i32(len(self.types)) + u16(len(name)) + name + u16(version)

    def type_id(self, name):
        return self.types.index(name) + 1

    def value_base(self):
        self.ty(b"VtValue\0")
        self.ty(b"VtObject\0")

    def text(self, s: str):
        """A VtString's body: i16 length, the cp949 text, two NULs, the UTF-16LE copy, two NULs, one more byte."""
        payload = s.encode("cp949") + b"\0\0" + s.encode("utf-16-le") + b"\0\0"
        self.out += i16(len(payload)) + payload + b"\0"

    def cell(self, value):
        if value is None:
            self.out += i32(-1)
        elif isinstance(value, Record):
            self.out += i32(self.next_object)
            self.next_object += 1
            self.ty(b"VtString\0")
            self.out += i16(len(value)) + value + b"\0"
            self.value_base()
        elif isinstance(value, bytes):  # raw slot bytes
            self.out += value
        elif isinstance(value, str):
            self.out += i32(self.next_object)
            self.next_object += 1
            self.ty(b"VtString\0")
            self.text(value)
            self.value_base()
        else:
            self.out += i32(self.next_object)
            self.next_object += 1
            self.ty(b"VtDouble\0")
            self.out += struct.pack("<d", value) + i16(-1)
            self.value_base()

    def grid(self, rows, cols, cells, labels=(1, 1)):
        """A VtDataGrid object; `cells` maps a 1-based row-major slot to a number, a text or raw slot bytes."""
        self.out += i32(self.next_object)
        self.next_object += 1
        self.ty(b"VtDataGrid\0")
        self.ty(b"VtMatrix\0")
        self.ty(b"VtCollection\0")
        self.out += u16(len(cells) & 0xFFFF)  # VtCollection's count, which the reader does not use
        self.ty(b"VtObject\0")
        self.out += u16(rows) + u16(cols)
        for slot in range(rows * cols):
            self.cell(cells.get(slot + 1))
        lr, lc = labels
        self.out += u16(lr) + u16(lc) + u16(cols - lc) + u16(rows - lr)


def contents(rows, cols, cells, *, title="Sales 2024", kind=1, echo=(), labels=(1, 1), filler=b""):
    """A Hancom-native chart Contents stream: a 40-byte head (0x00010000, two sizes, the object start), the
    VtChart (version 6) class, its data grid, a title object, the label records in `echo` repeated in a legend
    after the grid, the `filler` bytes, then the VtChart tail: ChartType i16, four bytes, the VtWindow (version 2)
    base declared there, its VtObject base and two bytes."""
    a = Archive()
    a.out += i32(0) + u16(8) + b"VtChart\0" + u16(6)  # the VtChart class at offset 40, type id 0
    a.grid(rows, cols, cells, labels)
    if title is not None:  # the title text a few bytes after its class, up to the next sibling object
        a.out += i32(a.next_object)
        a.next_object += 1
        a.ty(b"VtChartTitle\0")
        a.out += bytes(8)
        a.text(title)
        a.out += i32(a.next_object) + u16(15) + b"VtChartSection\0"
    if echo:
        a.out += b"VtLegend\0" + bytes(4)
        for s in echo:
            payload = s.encode("cp949") + b"\0\0" + s.encode("utf-16-le") + b"\0\0"
            a.out += i16(len(payload)) + payload + bytes(3)
    a.out += filler
    a.out += i16(kind) + bytes(4)
    a.ty(b"VtWindow\0", 2)
    a.ty(b"VtObject\0")
    a.out += bytes(2)
    body = bytes(a.out)
    return struct.pack("<IIII", 0x00010000, len(body), len(body), 40) + bytes(24) + body


# two series (rows), three categories (columns)
ROWS = {2: "1분기", 3: "2분기", 4: "3분기", 5: "North", 6: 10.0, 7: 20.5, 8: 30.0,
        9: "South", 10: 5.0, 11: 0.0, 12: -2.5}
# the same chart with a series per column: rows are categories
COLS = {2: "North", 3: "South", 4: "1분기", 5: 10.0, 6: 5.0, 7: "2분기", 8: 20.5, 9: 0.0,
        10: "3분기", 11: 30.0, 12: -2.5}
TABLE = ("Chart: column — Sales 2024\n||North|South|\n|---|---|---|\n|1분기|10|5|\n|2분기|20.5|0|\n|3분기|30|-2.5|\n")


# ---- the HWP document around it
def items(*specs):
    """DocInfo BIN_DATA records: (kind, compress, storage id) per item; kind 2 storage (BIN%04X.OLE), 0 a link."""
    out = b""
    for kind, compress, sid in specs:
        if kind == 0:
            out += hwp_record(hr.BIN_DATA, 1, u16(compress << 4) + u16(0) + u16(0))
        else:
            out += hwp_record(hr.BIN_DATA, 1, u16(kind | compress << 4) + u16(sid))
    return out


def ole_shape(bid, *, old=False, size=24):
    """A gso control holding an OLE object: SHAPE_COMPONENT, then SHAPE_OLE one level deeper (or, in the older
    layout, SHAPE_OLE right under the control) with the BinData id at offset 12."""
    rec = (bytes(12) + u16(bid) + bytes(10))[:size]
    if old:
        records = hwp_record(hr.SHAPE_OLE, 2, rec)
    else:
        records = hwp_record(hr.SHAPE_COMPONENT, 2, b"elo$elo$" + bytes(32)) + hwp_record(hr.SHAPE_OLE, 3, rec)
    return 11, b"gso " + bytes(40) + u16(0), records


def doc(*controls, bins=None, streams=None, before="앞", after="뒤"):
    """One paragraph `before` holding the controls, then a paragraph `after`. `bins` defaults to one stored item
    per control, the k-th naming BIN%04X k; `streams` are the BinData streams by name (written as given)."""
    bins = items(*[(2, 0, k) for k in range(1, len(controls) + 1)]) if bins is None else bins
    body = hwp_para(before + FFFC * len(controls), controls=controls) + hwp_para(after)
    return make_hwp(tmp() / "a.hwp", [body], doc_info=bins, bins=streams)


def read(path):
    ctx = Context()
    d = hwp.parse(path, ctx)
    return markdown.render(d, tmp() / "assets", False, ctx.skipped), ctx


def native(**kw):
    return envelope({"Contents": contents(3, 4, ROWS, **kw)})


# ---- tests
def test_ooxml_chart_in_ole():
    # OOXMLChartContents with a barChart cache; the table sits where a picture would, between the paragraphs
    out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(envelope({"OOXMLChartContents": CHART_XML}))}))
    assert out == "앞\n\nChart: bar\n||S|\n|---|---|\n|A|5|\n\n뒤\n", out
    assert not ctx.skipped, ctx.skipped


def test_native_chart():
    # a minimal ChartOBJ Contents: two series, three categories, column
    out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(native())}, before="", after=""))
    assert out == TABLE, out
    assert not ctx.skipped, ctx.skipped


def test_native_chart_series_in_columns():
    # the column labels repeated in the legend after the grid make the columns the series (orientation.rs)
    item = envelope({"Contents": contents(4, 3, COLS, echo=("North", "South"))})
    out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(item)}, before="", after=""))
    assert out == TABLE, out
    # without the echo the rows are the series, by the writers' convention
    item = envelope({"Contents": contents(4, 3, COLS, title=None, kind=14)})
    out, _ = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(item)}, before="", after=""))
    assert out.startswith("Chart: pie\n||1분기|2분기|3분기|\n"), out


def test_native_chart_kinds():
    for kind, name in ((3, "line"), (11, "bar"), (14, "pie"), (26, "unknown")):
        table = hwp_chart.ole_table(native(kind=kind), Context())
        assert table.caption == f"Chart: {name} — Sales 2024", (kind, table.caption)
    # the VtWindow's VtObject base declared at the tail rather than referred to; a tail that does not end the archive
    # as VtChart's does leaves the kind unknown, and the data is still read
    c = contents(3, 4, ROWS, kind=10)
    declared = c[:-6] + i32(9) + u16(9) + b"VtObject\0" + u16(1) + bytes(2)
    for data, name in ((declared, "bar"), (c + b"\0", "unknown"), (c[:40] + i32(-1) + c[44:], "unknown")):
        table = hwp_chart.ole_table(envelope({"Contents": data}), Context())
        assert table.caption == f"Chart: {name} — Sales 2024" and len(table.rows) == 4, (name, table.caption)


def test_biff_chart_counted():
    item = envelope({"Workbook": b"\x09\x08" + bytes(30)})
    out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(item)}))
    assert out == "(could not read: 1 chart)\n\n앞\n\n뒤\n", out


def test_ole_not_cfb_counted():
    garbage = b"\x5a" * 64
    out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(struct.pack("<I", 64) + garbage)}))
    assert out == "(could not read: 1 embedded object)\n\n앞\n\n뒤\n", out
    # a length that disagrees with the data is not a compound file either, nor is one with no chart stream in it
    item = native()
    for data in (struct.pack("<I", len(item)) + item[4:], b"\x01\x02", envelope({"Other": b"x" * 10}),
                 envelope({"Contents": b"\x00" * 64})):
        ctx = Context()
        assert hwp_chart.ole_table(data, ctx) is None and ctx.skipped == {"object": 1}, ctx.skipped


def test_native_chart_cap():
    # with CHART_CELLS at 4 the grid is read only as far as the cap (its last row would not parse), and the table is
    # cut to four cells with one count
    cells = dict(ROWS)
    cells[9] = i32(5000) + i32(99) + u16(4) + b"Bad\0" + u16(1)  # an unknown class
    item = envelope({"Contents": contents(3, 4, cells)})
    old = limits.CHART_CELLS
    limits.CHART_CELLS = 4
    try:
        out, ctx = read(doc(ole_shape(1), streams={"BIN0001.OLE": deflate(item)}, before="", after=""))
    finally:
        limits.CHART_CELLS = old
    assert out == ("(could not read: 1 chart cut short)\n\n"
                   "Chart: column — Sales 2024 (first 1 points, first 1 series)\n||North|\n|---|---|\n|1분기|10|\n"), out
    assert ctx.skipped == {"chart_cut": 1}, ctx.skipped
    ctx = Context()  # read whole, the same grid does not parse
    assert hwp_chart.ole_table(item, ctx) is None and ctx.skipped == {"chart": 1}, ctx.skipped


def test_native_grid_is_bounded():
    # a grid of 3 x 40000 slots is read only past CHART_CELLS cells, quickly, into a table of at most that many
    import time
    rows, cols = 3, 40_000
    cells = {k: float(k) for k in range(cols + 2, rows * cols + 1) if (k - 1) % cols}
    item = envelope({"Contents": contents(rows, cols, cells)})
    ctx = Context()
    t0 = time.monotonic()
    table = hwp_chart.ole_table(item, ctx)
    took = time.monotonic() - t0
    assert took < 2, took
    assert sum(len(r) for r in table.rows) <= limits.CHART_CELLS and ctx.skipped == {"chart_cut": 1}, ctx.skipped
    assert table.caption == f"Chart: column — Sales 2024 (first {len(table.rows) - 1} points)", table.caption


def test_label_echo_search_is_bounded():
    # 4,471 labels that all refer back to one record, repeated only at the end of a tail (under 30,000 bytes, where
    # CPython's find is quadratic) of near-copies of it, each alignment matching 96 of its 98 bytes: the record is
    # searched for once, within the budget of _ECHO_FACTOR bytes scanned per byte of the stream
    rows, cols = 3, 4_470
    record = Record(bytes((96, 0)) * 47 + bytes((97, 0)))  # with its length in front: (96, 0) x 48, then (97, 0)
    filler = bytes((96, 0)) * 14_500 + i16(len(record)) + record
    first = 1001  # the grid is object 1000, so the first label is 1001
    refs = {k: i32(first) for k in range(3, cols + 1)} | {2: record, cols + 1: i32(first), 2 * cols + 1: i32(first)}
    # the same record written out again for every label rather than referred to is one search as well
    copies = {k: record for k in range(2, cols + 1)} | {cols + 1: record, 2 * cols + 1: record}
    for cells in (refs, copies):
        data = contents(rows, cols, cells, filler=filler)
        g = hwp_chart._grid(data)
        spy = Spy(data)
        assert hwp_chart._series_rows(spy, g) is True
        assert spy.calls == 1, spy.calls
        assert spy.scanned <= hwp_chart._ECHO_FACTOR * len(data), (spy.scanned, len(data))


def test_label_echo_search_budget():
    # forty column labels, each repeated after the grid, and row labels that are not: the columns are the series.
    # With a tail this long, a search for every label would pass the budget, so the searches stop within it and the
    # rows are the series, by the writers' convention
    names = [f"c{k}" for k in range(40)]
    cols = len(names) + 1
    cells = {k + 2: name for k, name in enumerate(names)} | {cols + 1: "r1", 2 * cols + 1: "r2"}
    data = contents(3, cols, cells, echo=names, filler=bytes(20_000))
    g = hwp_chart._grid(data)
    budget = hwp_chart._ECHO_FACTOR * len(data)
    assert len(names) * (len(data) - g.end) > budget  # the fixture's searches would pass it
    spy = Spy(data)
    assert hwp_chart._series_rows(spy, g) is True
    assert 0 < spy.scanned <= budget, (spy.scanned, budget)
    old = hwp_chart._ECHO_FACTOR
    hwp_chart._ECHO_FACTOR = 10 ** 6
    try:
        assert hwp_chart._series_rows(data, g) is False  # given room, the echo makes the columns the series
    finally:
        hwp_chart._ECHO_FACTOR = old


def test_malformed_native_chart_counted():
    # an unknown class in a cell, a cell holding another grid, a grid larger than its stream, a cut stream: each is
    # one chart, and the section goes on
    bad = dict(ROWS)
    bad[6] = i32(5000) + i32(99) + u16(4) + b"Bad\0" + u16(1)
    nested = dict(ROWS)
    nested[6] = i32(5001) + i32(1)  # type id 1: VtDataGrid
    good = contents(3, 4, ROWS)
    at = good.index(b"VtObject\0" + u16(1)) + 11  # the matrix's row and column counts follow its first base
    huge = good[:at] + u16(4000) + u16(4000) + good[at + 4:]
    streams = {f"BIN{k:04X}.OLE": deflate(envelope({"Contents": c})) for k, c in enumerate(
        (contents(3, 4, bad), contents(3, 4, nested), huge, good[:200]), 1)}
    out, ctx = read(doc(*(ole_shape(k) for k in range(1, 5)), streams=streams))
    assert ctx.skipped == {"chart": 4}, ctx.skipped
    assert out == "(could not read: 4 charts)\n\n앞\n\n뒤\n", out


def test_ole_old_layout():
    # a SHAPE_OLE right under the gso control is read through the same SHAPES entry
    out, ctx = read(doc(ole_shape(1, old=True), streams={"BIN0001.OLE": deflate(native())}, before="", after=""))
    assert out == TABLE, out


def test_broken_ole_items_counted():
    # a link item, a missing stream, a corrupt deflate stream, a cut one, an id past the table and a SHAPE_OLE too
    # short for its id each cost one embedded object, never an image
    packed = deflate(native())
    bins = items((0, 0, 0), (2, 0, 2), (2, 1, 3), (2, 1, 4))
    streams = {"BIN0003.OLE": b"\x5a" * 64, "BIN0004.OLE": packed[:len(packed) // 2]}
    shapes = (ole_shape(1), ole_shape(2), ole_shape(3), ole_shape(4), ole_shape(9), ole_shape(1, size=13))
    out, ctx = read(doc(*shapes, bins=bins, streams=streams))
    assert ctx.skipped == {"object": 6}, ctx.skipped
    assert out == "(could not read: 6 embedded objects)\n\n앞\n\n뒤\n", out


def test_stored_ole_marked_compressed():
    # an item marked compressed whose stream is not deflate is read from its stored bytes when they are an OLE item
    out, ctx = read(doc(ole_shape(1), bins=items((2, 1, 1)), streams={"BIN0001.OLE": native()}, before="", after=""))
    assert out == TABLE and not ctx.skipped, (out, ctx.skipped)


def test_ole_named_twice_read_once():
    calls = []
    old = hwp.ole_table

    def spy(data, ctx):
        calls.append(len(data))
        return old(data, ctx)
    hwp.ole_table = spy
    try:
        out, ctx = read(doc(ole_shape(1), ole_shape(2), bins=items((2, 0, 1), (2, 0, 1)),
                            streams={"BIN0001.OLE": deflate(native())}, before="", after=""))
    finally:
        hwp.ole_table = old
    assert out == TABLE + "\n" + TABLE, out
    assert len(calls) == 1, calls


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
