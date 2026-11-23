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
    assert len(out) == 1 and isinstance(out[0], Table) and out[0].caption == "Chart: column — Sales", out
    assert len(out[0].rows) == 4 and not ctx.skipped, (out[0].rows, ctx.skipped)


def test_excel_sheet_object():
    out, ctx = blocks_of(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    assert len(out) == 1 and isinstance(out[0], Table), out
    text = markdown.render(Doc(blocks=out), tmp() / "a", False, {})
    assert text == "Embedded sheet S\n1|2\n-|-\n3|4\n", text
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
    assert len(out) == 1 and render(out) == "Chart: column\n||V|\n-|-\nQ1|5\nQ2|6\n", render(out)
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
    assert render(out) == "Chart: column\n||V|\n-|-\nz|7\n", render(out)
    assert budget.cells == budget.cap_cells, "a chart with cached values reads no sheet"


def test_cached_chart_with_a_name_reference_reads_no_sheet():
    # Excel links a series name to its header cell (a role 0 reference) beside the cached values: the name is the
    # SeriesText's, or else only what cells were already read give, and the data sheet is never read for a name alone
    head = (0, area3d(0, 0, 0, 1, 1))
    for name, want in (("V", "V"), (None, "Series 1")):
        cached = [{"name": name, "vals": [7], "cats": ["z"], "ref": [head]}]
        sheets = [CELLS, {"name": "Chart1", "kind": 2, "raw": biff_chart(cached)}]
        budget = sheet_grid.Budget()
        path = tmp() / "o.cfb"
        make_cfb(path, cacheless_object(sheets=sheets))
        ctx = Context()
        with Compound(path) as cf:
            out = xls_embedded.blocks(cf, ctx, budget)
        assert render(out) == f"Chart: column\n||{want}|\n-|-\nz|7\n", render(out)
        assert budget.cells == budget.cap_cells and budget.chars == budget.cap_chars, (name, budget.cells)
        assert not ctx.skipped, ctx.skipped
    # without a cache the name reference still reads the sheet it names
    refs = [{"name": None, "ref": [head, *REFS[0]["ref"]]}]
    path = tmp() / "o.cfb"
    make_cfb(path, cacheless_object(refs=refs))
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, Context(), sheet_grid.Budget())
    assert render(out) == "Chart: column\n||5|\n-|-\nQ1|5\nQ2|6\n", render(out)


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
    assert render(out) == "Chart: column\n||V|\n-|-\n", render(out)
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
    assert render(blocks_of(streams)[0]).startswith("Chart: column"), streams.keys()
    big = dict(streams, **{"\x01CompObj": streams["\x01CompObj"] + bytes(5000)})
    out, ctx = blocks_of(big)  # not routed to MS Graph: the 0x0680 stream is no worksheet, counted under chart
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def render(blocks):
    return markdown.render(Doc(blocks=blocks), tmp() / "a", False, {})


def test_whole_stream_chart():
    streams = {"Workbook": biff_chart(AB, title="Whole")}
    out, ctx = blocks_of(streams)
    assert len(out) == 1 and out[0].caption == "Chart: column — Whole", out


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


def test_junk_workbook_counts_only_the_chart():
    # bytes that are no BIFF stream at all are one chart, not also a damaged record
    for junk in (b"x" * 40, b"", b"\x09\x08", biff(0x0809, bytes(2)) + b"junk", biff(0x1234, bytes(8))):
        out, ctx = blocks_of({"Workbook": junk})
        assert out == [] and ctx.skipped == {"chart": 1}, (junk, out, ctx.skipped)
        d, ctx = dobj.parse(dobj.embed_doc({1: {"Workbook": junk}}, preview=dobj.PNG_PREVIEW))
        assert ctx.skipped == {"chart": 1}, (junk, ctx.skipped)
        out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
        assert out == "(could not read: 1 chart)\n\n![](assets/img-001.png)\n", (junk, out)


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
    assert out == "Chart: column — Sales\n||A|\n-|-\nQ1|1\nQ2|2\nQ3|3\n", out
    assert not ctx.skipped, ctx.skipped


def test_doc_text_order():
    # the paragraph's text stays in order, the chart follows that paragraph, and the next paragraph follows the chart
    obj = object_streams(excel_object([CHART], b"Excel.Chart.8"))
    text = "before " + dobj.EMBED + " after\rnext\r"
    p = dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.index("before") < out.index("after") < out.index("Chart: column") < out.index("next"), out
    assert "![" not in out and "could not read" not in out, out


def test_hwp_embedded_sheet():
    item = hchart.envelope(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    path = hchart.doc(hchart.ole_shape(1), streams={"BIN0001.OLE": hchart.deflate(item)})
    out, ctx = hchart.read(path)
    assert out == "앞\n\nEmbedded sheet S\n1|2\n-|-\n3|4\n\n뒤\n", out
    assert not ctx.skipped, ctx.skipped


def test_one_cell_sheet_placed():
    """A worksheet holding one cell, which the grid gives as a lone paragraph, is placed in doc and hwp, never
    dropped."""
    one = object_streams(excel_object([{"name": "S", "cells": {(0, 0): "only cell"}}], b"Excel.Sheet.8"))
    p = dobj.embed_doc({1: one}, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out == "Embedded sheet S\n\nonly cell\n", (out, ctx.skipped)
    assert not ctx.skipped, ctx.skipped
    path = hchart.doc(hchart.ole_shape(1), streams={"BIN0001.OLE": hchart.deflate(hchart.envelope(one))})
    out, ctx = hchart.read(path)
    assert out == "앞\n\nEmbedded sheet S\n\nonly cell\n\n뒤\n" and not ctx.skipped, (out, ctx.skipped)


G1 = {"name": "S", "cells": {(0, 0): 1, (0, 1): 2, (1, 0): 3, (1, 1): 4}}
G2 = {"name": "S", "cells": {(0, 0): 5, (0, 1): 6, (1, 0): 7, (1, 1): 8}}


def doc_with_sheets(n):
    """A .doc with n EMBED fields, the k-th naming ObjectPool/_k, a workbook of the 2x2 grid G1 (k odd) or G2."""
    objs = {k + 1: object_streams(excel_object([(G1, G2)[k % 2]], b"Excel.Sheet.8")) for k in range(n)}
    text = ("a" + dobj.EMBED) * n + "\r"
    spans, k = [], 0
    for i, ch in enumerate(text):
        if ch == "\x14":
            k += 1
            spans.append((i, i + 1, dobj.sp(dobj.SPEC) + dobj.sp(dobj.OLE) + dobj.sp(dobj.PIC, k)))
        elif ch == "\x01":
            spans.append((i, i + 1, dobj.sp(dobj.SPEC) + dobj.sp(dobj.PIC, dobj.AT)))
    return dobj.embed_doc(objs, text=text, preview=dobj.PNG_PREVIEW, chpx=spans)


def test_doc_objects_share_one_sheet_budget():
    # two 2x2 workbooks over a budget of 4 cells: the first is read whole, the second finds it spent, so it is an
    # unread chart: its preview stays and it is counted
    old = limits.SHEET_CELLS
    limits.SHEET_CELLS = 4
    try:
        d, ctx = dobj.parse(doc_with_sheets(2))
    finally:
        limits.SHEET_CELLS = old
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out == ("(could not read: 1 chart, 1 sheet cut short)\n\naa\n\nEmbedded sheet S\n1|2\n-|-\n3|4\n\n"
                   "![](assets/img-001.png)\n"), out
    assert "5|" not in out and ctx.skipped["chart"] == 1, ctx.skipped


def test_doc_object_cap():
    from reader.formats import doc_objects
    old = doc_objects.MAX_OBJECTS
    doc_objects.MAX_OBJECTS = 2
    try:
        d, ctx = dobj.parse(doc_with_sheets(3))
    finally:
        doc_objects.MAX_OBJECTS = old
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.count("\n1|2\n") == 1 and out.count("\n5|6\n") == 1 and ctx.skipped == {"object": 1}, (out, ctx.skipped)
    assert "(could not read: 1 embedded object)" in out and out.count("![](") == 1, out


def test_hwp_object_cap():
    from reader.formats import hwp
    items = [object_streams(excel_object([(G1, G2)[k % 2]], b"Excel.Sheet.8")) for k in range(3)]
    items[2] = object_streams(excel_object([{"name": "S", "cells": {(0, 0): 9, (0, 1): 9}}], b"Excel.Sheet.8"))
    shapes = [hchart.ole_shape(k) for k in (1, 2, 3)]
    streams = {f"BIN000{k + 1}.OLE": hchart.deflate(hchart.envelope(s)) for k, s in enumerate(items)}
    old = hwp.MAX_OBJECTS
    hwp.MAX_OBJECTS = 2
    try:
        out, ctx = hchart.read(hchart.doc(*shapes, streams=streams))
    finally:
        hwp.MAX_OBJECTS = old
    assert out.count("\n1|2\n") == 1 and out.count("\n5|6\n") == 1 and "\n9|9\n" not in out, out
    assert ctx.skipped == {"object": 1}, ctx.skipped


def test_failed_workbook_leaves_no_warning():
    # a failure the guard counts leaves a warning; a workbook that is only an unread chart (counted where it is
    # placed) leaves neither the count nor the warning behind
    old = xls_embedded._read
    xls_embedded._read = lambda *a: (_ for _ in ()).throw(ValueError("boom"))
    try:
        obj = object_streams(excel_object([GRID], b"Excel.Sheet.8"))
        d, ctx = dobj.parse(dobj.embed_doc({1: obj}, preview=dobj.PNG_PREVIEW))
    finally:
        xls_embedded._read = old
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out == "(could not read: 1 chart)\n\n![](assets/img-001.png)\n", out
    assert ctx.skipped == {"chart": 1} and ctx.warnings == [], (ctx.skipped, ctx.warnings)


def test_doc_workbook_past_chart_total_shows_preview():
    # the second placement of a 4-cell workbook passes CHART_TOTAL: its preview shows and the chart is counted, as
    # for a workbook that gave no table (ppt does the same)
    obj = object_streams(excel_object([GRID], b"Excel.Sheet.8"))
    text = "a" + dobj.EMBED + "b" + dobj.EMBED + "\r"
    old = limits.CHART_TOTAL
    limits.CHART_TOTAL = 6
    try:
        d, ctx = dobj.parse(dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW))
    finally:
        limits.CHART_TOTAL = old
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.count("\n1|2\n") == 1 and out.count("![](") == 1, out
    assert "(could not read: 1 chart)" in out and ctx.skipped == {"chart": 1}, (out, ctx.skipped)


def test_doc_workbook_in_vanished_text_is_hidden():
    # vanished text goes through the Hidden path, so --hidden shows the table; tracked-deleted text stays dropped
    obj = object_streams(excel_object([GRID], b"Excel.Sheet.8"))
    text = "a" + dobj.EMBED + "\r"
    p = dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW, props=dobj.sp(dobj.VANISH))
    shown = dobj.md(p, hidden=True)
    assert "1|2\n-|-\n3|4" in shown, shown
    plain = dobj.md(p)
    assert "\n1|2\n" not in plain and "hidden places left out" in plain, plain
    d, ctx = dobj.parse(p)
    assert not ctx.skipped, ctx.skipped
    p = dobj.embed_doc({1: obj}, text=text, preview=dobj.PNG_PREVIEW, props=dobj.sp(dobj.DELETED))
    d, ctx = dobj.parse(p)
    assert not any(isinstance(b, Table) for b in d.blocks) and not ctx.skipped, (d.blocks, ctx.skipped)
    assert "\n1|2\n" not in dobj.md(p, hidden=True), dobj.md(p, hidden=True)


def test_ole_blocks_share_budget():
    # ole_blocks' sheets keyword is the document's budget: the embedded sheet's cells are drawn from it
    from reader.formats import hwp_chart
    item = hchart.envelope(object_streams(excel_object([GRID], b"Excel.Sheet.8")))
    budget = sheet_grid.Budget()
    got = hwp_chart.ole_blocks(item, Context(), sheets=budget)
    assert got and budget.cells == budget.cap_cells - 4, budget.cells


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
