"""Excel workbooks embedded as OLE objects in hwp and doc: the active sheet of a Workbook stream as tables (logic after
dochan office_binary/ole_objects.py _visible_workbook_chart and the Workbook branch of its object reader).

An embedded Excel object is a storage (or, in hwp, a nested compound file) with a `Workbook` or `Book` stream and a
`\\x01CompObj` naming its ProgID. What is read is what the object shows, its active sheet (WINDOW1 itabCur):
  * a stream whose first BOF is a chart substream (dt 0x20) is one chart, read by biff_chart.chart;
  * a chart sheet (kind 2) as the active sheet is one chart;
  * a worksheet as the active sheet gives its grid blocks (xls.Workbook.sheet_blocks) over the document's one
    sheet_grid.Budget; only its tables are kept, never its pictures, and a sheet of one cell (a Para from the grid)
    is a 1x1 Table. A chart reads the cached values it carries, so the other sheets are not walked.
The CompObj only routes MSGraph, to biff_chart.graph: Excel.Chart and Excel.Sheet objects are both read by their
active sheet, unlike dochan, which reads an Excel.Chart object's worksheet as a chart; the object's preview shows the
active sheet, so the grid is what a reader of the document saw.
An MS Graph object (`MSGraph.Chart`, whose Workbook is a datasheet and a chart in one substream) is one chart, read by
biff_chart.graph. A stream over EMBED_BYTES, a FILEPASS the default password does not open (xls.Workbook), BIFF5, a
Graph stream that does not parse, or a chart that gives no table is counted under chart. All of those give [].
"""
import re
import struct

from .. import limits
from ..containers.cfb import CfbError
from ..limits import ReadError
from ..model import Cell, Para, Table
from . import biff, biff_chart, sheet_grid, xls

_BOOKS = ("Workbook", "Book")
_COMP_OBJ = "\x01CompObj"
_COMP_OBJ_HEAD = 4096   # bytes of a CompObj read for its ProgID
_COMP_OBJ_MAX = 2**20   # a CompObj stream over this is not read
_GRAPH_PROG = re.compile(rb"MSGraph\.Chart(?:\.|\x00)")
_READ_KINDS = ("damaged", "too_large", "unsupported", "encrypted")  # a ReadError that skips the one object


def blocks(cf, ctx, budget: sheet_grid.Budget) -> list | None:
    """The tables of the Excel object in storage `cf`, or None when it holds no `Workbook` or `Book` stream.

    `cf` is any storage with `exists(name)`, `size(name)` and `read(name)` by stream name, as containers.cfb.Compound
    has (a KeyError or CfbError from them is a stream that cannot be read). A failure inside, or a chart that gives
    no table, is counted under `chart` once and gives []; a ReadError of another kind (a timeout) is raised."""
    name = next((n for n in _BOOKS if cf.exists(n)), None)
    if name is None:
        return None
    out = None
    before = ctx.skipped["chart"]
    try:
        with ctx.guard("chart"):  # any other failure is counted by the guard, and leaves out None
            out = _read(cf, name, ctx, budget)
    except ReadError as e:
        if e.kind not in _READ_KINDS:
            raise
    if out is None:
        if ctx.skipped["chart"] == before:
            ctx.skipped["chart"] += 1
        return []
    return out


def _read(cf, name, ctx, budget):
    if cf.size(name) > limits.EMBED_BYTES:
        return None
    data = cf.read(name)
    if _prog(cf, _GRAPH_PROG):  # an MS Graph object: its Workbook is a datasheet, not a worksheet
        tbl = biff_chart.graph(data, ctx)
        return [tbl] if tbl is not None else None
    st = biff.Stream(data, ctx)
    first = next(st.records(0), None)
    if first is None or first[0] != biff.BOF or first[2] < 4:
        raise ReadError("damaged", "not a BIFF8 workbook")
    vers, dt = struct.unpack_from("<HH", data, first[1])
    if vers == 0x0500:
        raise ReadError("unsupported", "Excel 5.0/95 workbook")
    if vers != 0x0600:
        raise ReadError("damaged", "not a BIFF8 workbook")
    if dt == xls._CHART_BOF:  # the stream is one chart; a FILEPASS before its first EOF is a protected one
        for rec in st.records(0):
            if rec[0] == biff.FILEPASS:
                raise ReadError("encrypted")
            if rec[0] == biff.EOF:
                break
        tbl, _ = biff_chart.chart(data, 0, biff.Globals(), {}, ctx, st)
        return [tbl] if tbl is not None else None
    wb = xls.Workbook(data, ctx, budget)  # decrypts a FILEPASS the default password opens, else raises encrypted
    sheets = wb.g.sheets
    if wb.g.active >= len(sheets):
        raise ReadError("damaged", "active sheet outside the workbook")
    ref = sheets[wb.g.active]
    if ref.kind == xls._CHART:
        if not 0 <= ref.offset < len(wb.data):
            raise ReadError("damaged", "chart sheet offset outside the stream")
        tbl, _ = biff_chart.chart(wb.data, ref.offset, wb.g, wb.cells, ctx, wb.st)
        return [tbl] if tbl is not None else None
    if ref.kind != xls._WORKSHEET:
        return None
    # a lone cell comes from the grid as a Para: it is kept as a 1x1 Table, which the placement code counts and places
    return [b if isinstance(b, Table) else Table([[Cell([b])]])
            for b in wb.sheet_blocks(wb.g.active) if isinstance(b, (Table, Para))]


def _prog(cf, pattern) -> bool:
    """Whether the first _COMP_OBJ_HEAD bytes of the \\x01CompObj stream match `pattern`; a missing, oversize or
    unreadable one does not."""
    try:
        if not cf.exists(_COMP_OBJ) or cf.size(_COMP_OBJ) > _COMP_OBJ_MAX:
            return False
        return pattern.search(cf.read(_COMP_OBJ)[:_COMP_OBJ_HEAD]) is not None
    except (KeyError, CfbError):
        return False
