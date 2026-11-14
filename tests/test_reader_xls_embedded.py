"""Excel charts and sheets embedded as OLE objects in hwp and doc, through xls_embedded.blocks.

Run: python -I -X utf8 tests/test_reader_xls_embedded.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import sheet_grid  # noqa: E402
from reader.formats import xls_embedded  # noqa: E402  (ModuleNotFoundError before xls_embedded.py exists)
from reader.model import Context, Doc, Table  # noqa: E402
from reader_fix import area3d, biff, biff_chart, biff_workbook, comp_obj, excel_object, externsheet, make_cfb  # noqa: E402
import test_reader_doc_objects as dobj  # noqa: E402
import test_reader_hwp_chart as hchart  # noqa: E402

AB = [{"name": "A", "vals": [1, 2, 3], "cats": ["Q1", "Q2", "Q3"]}]
CHART = {"name": "Chart1", "kind": 2, "raw": biff_chart(AB, title="Sales")}
GRID = {"name": "S", "cells": {(0, 0): 1, (0, 1): 2, (1, 0): 3, (1, 1): 4}}


def tmp():
    return Path(tempfile.mkdtemp())


def blocks_of(streams, ctx=None):
    path = tmp() / "o.cfb"
    make_cfb(path, streams)
    ctx = ctx or Context()
    with Compound(path) as cf:
        return xls_embedded.blocks(cf, ctx, sheet_grid.Budget()), ctx


def object_streams(data):
    """The streams of a compound file `data` as the dict make_cfb takes."""
    path = tmp() / "o.cfb"
    path.write_bytes(data)
    with Compound(path) as cf:
        return {n: cf.read(n) for n in ("Workbook", "\x01CompObj")}


def test_excel_chart_object():
    out, ctx = blocks_of(object_streams(excel_object([CHART], b"Excel.Chart.8")))
    assert len(out) == 1 and isinstance(out[0], Table) and out[0].caption == "Chart: bar — Sales", out
    assert len(out[0].rows) == 4 and not ctx.skipped, (out[0].rows, ctx.skipped)


def test_excel_sheet_object():
    out, ctx = blocks_of(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    assert len(out) == 1 and isinstance(out[0], Table), out
    text = markdown.render(Doc(blocks=out), tmp() / "a", False, {})
    assert text == "|1|2|\n|---|---|\n|3|4|\n", text
    assert not ctx.skipped, ctx.skipped


REFS = [{"name": "V", "ref": [(1, area3d(0, 0, 1, 1, 1)), (2, area3d(0, 0, 1, 0, 0))]}]
CELLS = {"name": "Data", "cells": {(0, 0): "Q1", (1, 0): "Q2", (0, 1): 5, (1, 1): 6}}


def cacheless_object(refs=REFS, extra=None, sheets=None):
    """An Excel.Chart object whose active sheet (the second) is a chart with no cached values, series pointing at the
    object's own first sheet."""
    sheets = sheets or [CELLS, {"name": "Chart1", "kind": 2, "raw": biff_chart(refs, cache=False)}]
    return object_streams(excel_object(sheets, b"Excel.Chart.8", active=1, globals_extra=externsheet(0)))


def test_cacheless_chart_reads_its_own_sheet():
    budget = sheet_grid.Budget()
    path = tmp() / "o.cfb"
    make_cfb(path, cacheless_object())
    ctx = Context()
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, ctx, budget)
    assert len(out) == 1 and render(out) == "Chart: bar\n||V|\n|---|---|\n|Q1|5|\n|Q2|6|\n", render(out)
    assert not ctx.skipped, ctx.skipped
    assert budget.cells < budget.cap_cells, "the sheet's cells are charged to the document's budget"


def test_cached_chart_does_not_walk_sheets():
    cached = [{"name": "V", "vals": [7], "cats": ["z"]}]
    sheets = [CELLS, {"name": "Chart1", "kind": 2, "raw": biff_chart(cached)}]
    budget = sheet_grid.Budget()
    path = tmp() / "o.cfb"
    make_cfb(path, cacheless_object(sheets=sheets))
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, Context(), budget)
    assert render(out) == "Chart: bar\n||V|\n|---|---|\n|z|7|\n", render(out)
    assert budget.cells == budget.cap_cells, "a chart with cached values reads no sheet"


def test_reference_outside_the_workbook_stays_empty():
    # an XTI of sheet -1 (another workbook) has no cells to read; the chart is still the table of what it holds
    refs = [{"name": "V", "ref": [(1, area3d(0, 0, 1, 1, 1))]}]
    sheets = [CELLS, {"name": "Chart1", "kind": 2, "raw": biff_chart(refs, cache=False)}]
    streams = object_streams(excel_object(sheets, b"Excel.Chart.8", active=1, globals_extra=externsheet(-1)))
    budget = sheet_grid.Budget()
    path = tmp() / "o.cfb"
    make_cfb(path, streams)
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, Context(), budget)
    assert render(out) == "Chart: bar\n||V|\n|---|---|\n", render(out)
    assert budget.cells == budget.cap_cells


def test_embedded_worksheet_pictures_not_decoded():
    # the pictures of an embedded worksheet are dropped, so they are not decoded (and charge no flate or image budget)
    from reader.formats import xls, xls_drawing
    calls = []
    old = xls_drawing.pictures
    xls_drawing.pictures = lambda *a, **k: calls.append(a) or []
    try:
        sheet = dict(GRID, drawing=b"\x00" * 16)
        out, ctx = blocks_of(object_streams(excel_object([sheet], b"Excel.Sheet.8")))
        assert len(out) == 1 and not ctx.skipped, (out, ctx.skipped)
        assert calls == [], "an embedded worksheet's pictures were decoded"
        wb = xls.Workbook(biff_workbook([sheet]), Context(), sheet_grid.Budget())  # a workbook of its own still does
        wb.sheet_blocks(0)
        assert len(calls) == 1, calls
    finally:
        xls_drawing.pictures = old


def test_comp_obj_over_4096_bytes_is_not_read():
    from reader_fix import msgraph_object
    streams = object_streams(msgraph_object(["a"], [("S", [1])]))
    assert render(blocks_of(streams)[0]).startswith("Chart: bar"), streams.keys()
    big = dict(streams, **{"\x01CompObj": streams["\x01CompObj"] + bytes(5000)})
    out, ctx = blocks_of(big)  # not routed to MS Graph: the 0x0680 stream is no worksheet, counted under chart
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def render(blocks):
    return markdown.render(Doc(blocks=blocks), tmp() / "a", False, {})


def test_whole_stream_chart():
    streams = {"Workbook": biff_chart(AB, title="Whole")}
    out, ctx = blocks_of(streams)
    assert len(out) == 1 and out[0].caption == "Chart: bar — Whole", out


def test_no_workbook():
    out, ctx = blocks_of({"Contents": b"x" * 40})
    assert out is None and not ctx.skipped, (out, ctx.skipped)


def test_encrypted_object():
    # a FILEPASS the default password does not open (an XOR header with a wrong verifier), and one in a whole chart
    sheet = biff_workbook([GRID], filepass=bytes(6))
    out, ctx = blocks_of({"Workbook": sheet})
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)
    chart = biff_chart(AB)
    out, ctx = blocks_of({"Workbook": chart[:20] + biff(0x002F, bytes(6)) + chart[20:]})
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def test_rejected_streams():
    # over EMBED_BYTES, BIFF5, and MS Graph (Task 12's) are counted under chart
    old = limits.EMBED_BYTES
    limits.EMBED_BYTES = 100
    try:
        out, ctx = blocks_of({"Workbook": biff_workbook([GRID])})
    finally:
        limits.EMBED_BYTES = old
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)
    out, ctx = blocks_of({"Workbook": biff(0x0809, b"\x00\x05\x05\x00" + bytes(4)) + biff(0x000A)})
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)
    out, ctx = blocks_of({"Workbook": biff_workbook([GRID]), "\x01CompObj": comp_obj(b"MSGraph.Chart.8")})
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def test_doc_embedded_chart():
    obj = object_streams(excel_object([CHART], b"Excel.Chart.8"))
    p = dobj.embed_doc({1: obj}, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out == "Chart: bar — Sales\n||A|\n|---|---|\n|Q1|1|\n|Q2|2|\n|Q3|3|\n", out
    assert not ctx.skipped, ctx.skipped


def test_doc_text_order():
    # the paragraph's text stays in order, the chart follows that paragraph, and the next paragraph follows the chart
    obj = object_streams(excel_object([CHART], b"Excel.Chart.8"))
    text = "before " + dobj.EMBED + " after\rnext\r"
    p = dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.index("before") < out.index("after") < out.index("Chart: bar") < out.index("next"), out
    assert "![" not in out and "could not read" not in out, out


def test_hwp_embedded_sheet():
    item = hchart.envelope(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    path = hchart.doc(hchart.ole_shape(1), streams={"BIN0001.OLE": hchart.deflate(item)})
    out, ctx = hchart.read(path)
    assert out == "앞\n\n|1|2|\n|---|---|\n|3|4|\n\n뒤\n", out
    assert not ctx.skipped, ctx.skipped


def test_one_cell_sheet_placed():
    """A worksheet holding one cell, which the grid gives as a lone paragraph, is placed in doc and hwp, never
    dropped."""
    one = object_streams(excel_object([{"name": "S", "cells": {(0, 0): "only cell"}}], b"Excel.Sheet.8"))
    p = dobj.embed_doc({1: one}, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out == "only cell\n", (out, ctx.skipped)
    assert not ctx.skipped, ctx.skipped
    path = hchart.doc(hchart.ole_shape(1), streams={"BIN0001.OLE": hchart.deflate(hchart.envelope(one))})
    out, ctx = hchart.read(path)
    assert out == "앞\n\nonly cell\n\n뒤\n" and not ctx.skipped, (out, ctx.skipped)


def test_ole_table_shares_budget():
    # ole_table's sheets keyword is the document's budget: the embedded sheet's cells are drawn from it
    from reader.formats import hwp_chart
    item = hchart.envelope(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    budget = sheet_grid.Budget()
    tbl = hwp_chart.ole_table(item, Context(), sheets=budget)
    assert tbl is not None and budget.cells == budget.cap_cells - 4, budget.cells


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
