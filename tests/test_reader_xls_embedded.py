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
from reader_fix import biff, biff_chart, biff_workbook, comp_obj, excel_object, make_cfb  # noqa: E402
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
    # the chart follows the paragraph its object is in, and the text after it stays after it
    obj = object_streams(excel_object([CHART], b"Excel.Chart.8"))
    text = "before " + dobj.EMBED + " after\r"
    p = dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.index("before") < out.index("Chart: bar") and out.index("after") > out.index("before"), out
    assert "![" not in out and "could not read" not in out, out


def test_hwp_embedded_sheet():
    item = hchart.envelope(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    path = hchart.doc(hchart.ole_shape(1), streams={"BIN0001.OLE": hchart.deflate(item)})
    out, ctx = hchart.read(path)
    assert out == "앞\n\n|1|2|\n|---|---|\n|3|4|\n\n뒤\n", out
    assert not ctx.skipped, ctx.skipped


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
