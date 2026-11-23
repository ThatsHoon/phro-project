"""BIFF chart substreams (chart sheets and charts inside worksheets) as chart tables.

Run: python -I -X utf8 tests/test_reader_biff_chart.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import biff_chart as bcm, sheet_grid, xls, xls_embedded  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import area3d, biff, biff_chart, biff_workbook, externsheet, make_xls, msgraph_object  # noqa: E402
import test_reader_doc_objects as dobj  # noqa: E402

AB = [{"name": "A", "vals": [1, 2, 3], "cats": ["Q1", "Q2", "Q3"]},
      {"name": "B", "vals": [4, 5, 6], "cats": ["Q1", "Q2", "Q3"]}]


def run(sheets, **kw):
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_xls(src, sheets, **kw)
        reader.convert(src, Path(d) / "out")
        return (Path(d) / "out" / "out.md").read_text(encoding="utf-8")


def chart_sheet(raw, name="Chart1"):
    return {"name": name, "kind": 2, "raw": raw}


DATA = {"name": "Data", "cells": {(0, 0): "top"}}


def test_chart_sheet_from_cache():
    md = run([DATA, chart_sheet(biff_chart(AB, title="Sales"))])
    assert "## Sheet: Chart1" in md and md.index("top") < md.index("Chart: column"), md
    assert "Chart: column — Sales\n||A|B|\n-|-|-\nQ1|1|4\nQ2|2|5\nQ3|3|6\n" in md, md
    assert "could not read" not in md, md


def test_cache_format():
    # the BRAI's ifmt (9 is 0%) formats a cached number; a cached string stays as it is
    md = run([DATA, chart_sheet(biff_chart([{"name": "R", "vals": [0.5, 0.25], "cats": ["a", "b"], "ifmt": 9}]))])
    assert "\na|50%\nb|25%\n" in md, md


def test_embedded_chart():
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "records": [biff_chart(AB, title="Sales")]}
    md = run([sheet])
    assert md.index("cell") < md.index("Chart: column — Sales"), md
    assert "\nQ2|2|5\n" in md, md
    two = {"name": "S", "cells": {(0, 0): "cell"}, "records": [biff_chart(AB, title="One"), biff_chart(AB, title="Two")]}
    md = run([two])
    assert md.index("Chart: column — One") < md.index("Chart: column — Two"), md


def test_chart_kinds():
    donut, plain = struct.pack("<HHH", 0, 50, 0), struct.pack("<HHH", 0, 0, 0)
    cases = [((0x1019, donut),), ((0x1019, plain),), ((0x101B, struct.pack("<HHH", 0, 0, 1)),),
             ((0x101B, struct.pack("<HHH", 0, 0, 0)),), ((0x1018, bytes(2)),), ((0x101A, bytes(6)),),
             ((0x1017, bytes(6)), (0x103A, bytes(14))), ((0x1017, bytes(6)),), ((0x103E, bytes(4)),),
             ((0x1040, bytes(4)),), ((0x103F, bytes(4)),), ((0x1018, bytes(2)), (0x1017, bytes(6)))]
    want = ["doughnut", "pie", "bubble", "scatter", "line", "area", "3-D column", "column", "radar", "radar",
            "surface", "line+column"]
    for kinds, name in zip(cases, want):
        md = run([DATA, chart_sheet(biff_chart(AB, kinds=kinds))])
        assert f"Chart: {name}\n||A|B|" in md, (name, md)


def test_brai_fallback():
    sheet = {"name": "Sheet1", "cells": {(0, 0): "Q1", (1, 0): "Q2", (2, 0): "Q3", (0, 1): ("fmt", 1234.5, "#,##0.00"),
                                         (1, 1): 7, (2, 1): "txt"}}
    refs = [(1, area3d(0, 0, 2, 1, 1)), (2, area3d(0, 0, 2, 0, 0))]
    chart = biff_chart([{"name": "V", "ref": refs}], cache=False)
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0))
    assert "Chart: column\n||V|\n-|-\nQ1|1,234.50\nQ2|7\nQ3|txt\n" in md, md
    # a reference to a sheet with no cells leaves them empty and breaks nothing
    chart = biff_chart([{"name": "V", "ref": [(1, area3d(1, 0, 2, 1, 1))]}], cache=False)
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0, 9))
    assert "Chart: column\n||V|\n-|-\n" in md and "1,234" not in md.split("Chart: column")[1], md


def test_huge_area_bounded():
    sheet = {"name": "Sheet1", "cells": {(0, 0): "a", (1, 0): "b", (2, 0): "c"}}
    chart = biff_chart([{"name": "V", "ref": [(1, area3d(0, 0, 65535, 0, 255)), (2, area3d(0, 0, 65535, 0, 255))]}],
                       cache=False)
    t0 = time.monotonic()
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0))
    assert time.monotonic() - t0 < 1, time.monotonic() - t0
    assert "Chart: column" in md, md


def test_unterminated_chart():
    md = run([DATA, chart_sheet(biff_chart(AB, eof=False))])
    assert "(could not read: 1 chart)" in md and "Chart:" not in md, md
    # a worksheet-only record inside the chart ends it the same way
    bad = biff_chart(AB, extra=[(0x0203, struct.pack("<HHH", 0, 0, 0) + struct.pack("<d", 1.0)), (0x0208, bytes(16))])
    md = run([DATA, chart_sheet(bad)])
    assert "(could not read: 1 chart)" in md, md


def test_chart_total():
    one = [{"name": "A", "vals": [1], "cats": ["x"]}]  # header row and one row of two cells: 4 cells
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "records": [biff_chart(one, title="One"),
                                                                  biff_chart(one, title="Two")]}
    old = limits.CHART_TOTAL
    limits.CHART_TOTAL = 5
    try:
        md = run([sheet])
    finally:
        limits.CHART_TOTAL = old
    assert md.count("Chart: column") == 1 and "Chart: column — One" in md, md
    assert "(could not read: 1 chart)" in md, md


def test_auxiliary_series_skipped():
    # a Series carrying SerParent (a trendline) is no column; the series after it keep their own cache column
    real = [{"name": "A", "vals": [1, 2], "cats": ["x", "y"]}, {"name": "T", "aux": True},
            {"name": "B", "vals": [3, 4], "cats": ["x", "y"]}]
    md = run([DATA, chart_sheet(biff_chart(real))])
    assert "Chart: column\n||A|B|\n-|-|-\nx|1|3\ny|2|4\n" in md, md


def test_auxiliary_series_gives_no_categories():
    # a trendline listed before the real series carries categories of its own: they are not the chart's
    real = [{"name": "T", "aux": True, "vals": [9, 9], "cats": ["z1", "z2", "z3"]},
            {"name": "A", "vals": [1, 2], "cats": ["x", "y"]}]
    md = run([DATA, chart_sheet(biff_chart(real))])
    assert "Chart: column\n||A|\n-|-\nx|1\ny|2\n" in md, md


def test_ser_parent_binds_to_the_series_it_follows():
    # a Series written without Begin, its SerParent right after: that Series is the auxiliary one
    bare = [(0x1003, struct.pack("<6H", 1, 1, 0, 0, 0, 0)), (0x104A, bytes(2))]
    md = run([DATA, chart_sheet(biff_chart(AB[:1], extra=bare))])
    assert "Chart: column\n||A|\n-|-\nQ1|1\nQ2|2\nQ3|3\n" in md, md


def patched(module, **names):
    """Run a callable with module constants replaced: patched(m, X=1)(fn) -> fn()'s result."""
    def call(fn):
        old = {n: getattr(module, n) for n in names}
        for n, v in names.items():
            setattr(module, n, v)
        try:
            return fn()
        finally:
            for n, v in old.items():
                setattr(module, n, v)
    return call


def test_series_cap():
    four = [{"name": n, "vals": [1], "cats": ["x"]} for n in "ABCD"]
    md = patched(bcm, MAX_SERIES=2)(lambda: run([DATA, chart_sheet(biff_chart(four))]))
    assert "Chart: column (first 2 series)\n||A|B|\n-|-|-\nx|1|1\n" in md and "|C|" not in md, md
    assert "(could not read: 1 chart cut short)" in md, md


def test_point_cap():
    two = [{"name": "A", "vals": [1, 2, 3], "cats": ["x", "y", "z"]}, {"name": "B", "vals": [4, 5, 6]}]
    md = patched(bcm, MAX_POINTS=4)(lambda: run([DATA, chart_sheet(biff_chart(two))]))
    assert "Chart: column (first 3 points)\n||A|B|\n-|-|-\n||1|4|\n||2||\n||3||\n" in md, md
    assert "(could not read: 1 chart cut short)" in md, md


def test_nesting_cap():
    # Begin records, or BOFs, nested past limits.MAX_DEPTH leave the chart unreadable, and it is counted
    begins = [(0x1033, b"")] * (limits.MAX_DEPTH + 2)
    md = run([DATA, chart_sheet(biff_chart(AB, extra=begins))])
    assert "(could not read: 1 chart)" in md and "Chart:" not in md, md
    bofs = [(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0, 0, 0, 0))] * (limits.MAX_DEPTH + 2)
    md = run([DATA, chart_sheet(biff_chart(AB, extra=bofs))])
    assert "(could not read: 1 chart)" in md and "Chart:" not in md, md


def test_chart_count_cap():
    sheet = {"name": "S", "cells": {(0, 0): "cell"},
             "records": [biff_chart(AB, title=t) for t in ("One", "Two", "Three")]}
    md = patched(xls, MAX_CHARTS=2)(lambda: run([sheet]))
    assert "Chart: column — One" in md and "Chart: column — Two" in md and "Three" not in md, md
    assert "(could not read: 1 chart)" in md, md


def test_second_sections_call_gives_the_same_counts():
    sheet = {"name": "S", "cells": {(0, 0): "cell"},
             "records": [biff_chart(AB, title=t) for t in ("One", "Two", "Three")]}
    data = biff_workbook([sheet])

    def twice():
        ctx = Context()
        wb = xls.Workbook(data, ctx, sheet_grid.Budget())
        first = wb.sections()
        before = dict(ctx.skipped)
        second = wb.sections()
        return first, second, before, ctx.skipped

    def count(cap_charts, total):
        return patched(xls, MAX_CHARTS=cap_charts)(lambda: patched(limits, CHART_TOTAL=total)(twice))
    for cap, total in ((2, limits.CHART_TOTAL), (256, 9)):
        first, second, before, after = count(cap, total)
        shown = lambda secs: [(s.title, len([b for b in s.blocks if getattr(b, "caption", None)])) for s in secs]
        assert shown(first) == shown(second), (cap, total, shown(first), shown(second))
        assert after["chart"] == 2 * before["chart"], (cap, total, dict(before), dict(after))


def test_boolerr_cache():
    rows = [(0, bytes([1, 0])), (1, bytes([0, 0])), (2, bytes([0x2A, 1]))]
    extra = [(0x1065, struct.pack("<H", 1) + bytes(2))]
    extra += [(0x0205, struct.pack("<HHH", r, 0, 0) + v) for r, v in rows]
    md = run([DATA, chart_sheet(biff_chart([{"name": "A"}], cache=False, extra=extra))])
    assert "Chart: column\n||A|\n-|-\n||TRUE|\n||FALSE|\n||#N/A|\n" in md, md


def test_failed_sheet_leaves_no_cells():
    bad = biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0040, 0, 0, 0, 0)) + biff(0x000A)
    sheets = [{"name": "Bad", "raw": bad}, DATA,
              chart_sheet(biff_chart([{"name": "V", "ref": [(1, area3d(0, 0, 2, 1, 1)), (2, area3d(1, 0, 0, 0, 0))]}],
                                     cache=False))]
    ctx = Context()
    data = biff_workbook(sheets, globals_extra=externsheet(0, 1))
    secs = xls.Workbook(data, ctx, sheet_grid.Budget()).sections()
    assert [s.title for s in secs] == ["Data", "Chart1"] and ctx.skipped["sheet"] == 1, (secs, ctx.skipped)
    table = secs[1].blocks[0]
    texts = [[c.blocks[0].inlines[0].text if c.blocks else "" for c in row] for row in table.rows]
    assert texts == [["", "V"], ["top", ""]], texts  # sheet 0 failed: no values; sheet 1 gives the category


def test_extern_minus_one_and_short_externsheet():
    # a reference through an XTI of sheet -1 has no cells; one past a short EXTERNSHEET has none either, while an
    # XTI the record does hold still resolves
    sheet = {"name": "Sheet1", "cells": {(0, 0): "a"}}
    chart = biff_chart([{"name": "V", "ref": [(1, area3d(0, 0, 0, 0, 0)), (2, area3d(3, 0, 0, 0, 0))]}], cache=False)
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(-1))
    assert "could not read" not in md and "Chart: column\n||V|\n-|-\n" in md and "\na|" not in md, md
    short = biff(0x01AE, struct.pack("<HH", 1, 0x0401)) + biff(0x0017, struct.pack("<H", 5) + struct.pack("<Hhh", 0, 0, 0))
    md = run([sheet, chart_sheet(chart)], globals_extra=short)
    assert "could not read" not in md and "Chart: column\n||V|\n-|-\n||a|\n" in md, md


def test_series_text_split_by_continue():
    body = struct.pack("<H", 0) + bytes([6, 0]) + b"Alp"
    split = [(0x1003, struct.pack("<6H", 1, 1, 0, 0, 0, 0)), (0x1033, b""), (0x100D, body),
             (0x003C, bytes([0]) + b"ha1"), (0x1034, b"")]
    md = run([DATA, chart_sheet(biff_chart(AB[:1], extra=split))])
    assert "||A|Alpha1|" in md, md


def test_shared_record_budget():
    # worksheets and charts draw on one BIN_RECORDS: a later chart is cut off (counted under chart), not read afresh
    from reader.formats import sheet_grid, xls
    from reader.model import Context
    from reader.formats import biff
    one, two = biff_chart(AB, title="One"), biff_chart(AB, title="Two")
    data = biff_workbook([DATA, chart_sheet(one, "C1"), chart_sheet(two, "C2")])
    bare = xls.Workbook(biff_workbook([DATA, chart_sheet(b"", "C1"), chart_sheet(b"", "C2")]), Context(),
                        sheet_grid.Budget())
    bare.sections()
    alone = biff.Stream(one, Context())
    list(alone.records(0))
    limit = bare.st.count + alone.count + 3  # the worksheet and the first chart fit, the second does not
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = limit
    try:
        ctx = Context()
        secs = xls.Workbook(data, ctx, sheet_grid.Budget()).sections()
    finally:
        limits.BIN_RECORDS = old
    names = [s.title for s in secs]
    assert names == ["Data", "C1"] and ctx.skipped["chart"] == 1, (names, ctx.skipped)


# ---- MS Graph 97 datasheet charts
GRAPH = (["Q1", "Q2"], [("East", [1, 2]), ("West", [3, 4])])


def graph_streams(obj):
    path = Path(tempfile.mkdtemp()) / "g.cfb"
    path.write_bytes(obj)
    with Compound(path) as cf:
        return {n: cf.read(n) for n in ("Workbook", "CompObj")}


def graph_blocks(obj):
    from reader_fix import make_cfb
    path = Path(tempfile.mkdtemp()) / "g.cfb"
    make_cfb(path, graph_streams(obj))
    ctx = Context()
    with Compound(path) as cf:
        return xls_embedded.blocks(cf, ctx, sheet_grid.Budget()), ctx


def render(blocks, ctx):
    from reader.model import Doc
    return markdown.render(Doc(blocks=blocks), Path(tempfile.mkdtemp()) / "a", False, ctx.skipped)


def test_graph_datasheet():
    for by_rows in (False, True):
        out, ctx = graph_blocks(msgraph_object(*GRAPH, by_rows=by_rows))
        assert render(out, ctx) == "Chart: column\n||East|West|\n-|-|-\nQ1|1|3\nQ2|2|4\n", (by_rows, out)
        assert not ctx.skipped, ctx.skipped


def test_graph_title_kind():
    out, ctx = graph_blocks(msgraph_object(*GRAPH, kind="pie", title="Share"))
    assert len(out) == 1 and out[0].caption == "Chart: pie — Share", out
    assert not ctx.skipped, ctx.skipped


def test_graph_in_doc():
    p = dobj.embed_doc({1: graph_streams(msgraph_object(*GRAPH))}, preview=dobj.PNG_PREVIEW)
    d, ctx = dobj.parse(p)
    out = markdown.render(d, Path(tempfile.mkdtemp()) / "assets", False, ctx.skipped)
    assert "Chart: column\n||East|West|\n-|-|-\nQ1|1|3\nQ2|2|4\n" in out, out
    assert "could not read" not in out and not ctx.skipped, (out, ctx.skipped)


def test_graph_bad_counted():
    obj = graph_streams(msgraph_object(*GRAPH))
    wb = obj["Workbook"]
    i = wb.index(struct.pack("<HH", 3, 15))  # the first NUMBER cell, cut to 8 bytes
    obj["Workbook"] = wb[:i] + struct.pack("<HH", 3, 8) + wb[i + 4:i + 12] + wb[i + 19:]
    from reader_fix import make_cfb
    path = Path(tempfile.mkdtemp()) / "g.cfb"
    make_cfb(path, obj)
    ctx = Context()
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, ctx, sheet_grid.Budget())
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def test_graph_orientation_non_square():
    cats, series = ["a", "b", "c"], [("X", [1, 2, 3]), ("Y", [4, 5, 6])]
    want = "Chart: column@||X|Y|@-|-|-@a|1|4@b|2|5@c|3|6@".replace("@", "\n")
    for by_rows in (False, True):
        out, ctx = graph_blocks(msgraph_object(cats, series, by_rows=by_rows))
        assert render(out, ctx) == want, (by_rows, render(out, ctx))
        assert not ctx.skipped, ctx.skipped


def test_graph_codepage_labels():
    cats, series = ["한글", "둘"], [("이름", [1, 2])]
    want = "Chart: column@||이름|@-|-@한글|1@둘|2@".replace("@", "\n")
    out, ctx = graph_blocks(msgraph_object(cats, series, codepage=949))
    assert render(out, ctx) == want, render(out, ctx)
    assert not ctx.skipped, ctx.skipped


def test_graph_codepage_before_bof_and_late():
    cats, series = ["한글", "둘"], [("이름", [1, 2])]
    want = "Chart: column@||이름|@-|-@한글|1@둘|2@".replace("@", "\n")
    for at in ("before", "late"):
        out, ctx = graph_blocks(msgraph_object(cats, series, codepage=949, codepage_at=at))
        assert render(out, ctx) == want, (at, render(out, ctx))
        assert not ctx.skipped, (at, ctx.skipped)


def test_graph_codepage_after_eof_and_nested():
    # dochan takes the first CODEPAGE anywhere in the stream: after the datasheet's EOF, or inside a nested BOF/EOF
    cats, series = ["한글", "둘"], [("이름", [1, 2])]
    want = "Chart: column@||이름|@-|-@한글|1@둘|2@".replace("@", "\n")
    for at in ("after", "nested"):
        out, ctx = graph_blocks(msgraph_object(cats, series, codepage=949, codepage_at=at))
        assert render(out, ctx) == want, (at, render(out, ctx))
        assert not ctx.skipped, (at, ctx.skipped)


def test_graph_first_codepage_wins():
    cats, series = ["한글"], [("이름", [1])]
    obj = graph_streams(msgraph_object(cats, series, codepage=949))
    obj["Workbook"] += biff(0x0042, struct.pack("<H", 1252))  # a second one, after the EOF, is not the page
    from reader_fix import make_cfb
    path = Path(tempfile.mkdtemp()) / "g.cfb"
    make_cfb(path, obj)
    with Compound(path) as cf:
        ctx = Context()
        out = xls_embedded.blocks(cf, ctx, sheet_grid.Budget())
    assert render(out, ctx) == "Chart: column\n||이름|\n-|-\n한글|1\n", render(out, ctx)


def test_graph_codepage_search_after_eof_counts_nothing():
    # no CODEPAGE: the best-effort search past the substream's EOF meets two pad bytes, which are not a damaged record
    obj = graph_streams(msgraph_object(["a"], [("S", [1])]))
    cp = biff(0x0042, struct.pack("<H", 1252))
    assert obj["Workbook"].count(cp) == 1
    ctx = Context()
    tbl = bcm.graph(obj["Workbook"].replace(cp, b"") + b"\x00\x00", ctx)  # in memory only
    assert tbl is not None and len(tbl.rows) == 2 and not ctx.skipped, (tbl, ctx.skipped)


def test_graph_gaps_and_empty_head_cell():
    cats, series = ["a", "b", "c"], [("X", [1, 2, 3]), ("Y", [4, 5, 6])]
    # (2, 1) is series X at point 2, (0, 2) series Y's head: the value is an empty cell, the name "Series 2"
    out, ctx = graph_blocks(msgraph_object(cats, series, drop={(2, 1), (0, 2)}))
    assert render(out, ctx) == "Chart: column\n||X|Series 2|\n-|-|-\na|1|4\nb||5\nc|3|6\n", render(out, ctx)
    assert not ctx.skipped, ctx.skipped


def test_graph_series_and_point_caps():
    cats, series = ["a", "b"], [("X", [1, 2]), ("Y", [3, 4]), ("Z", [5, 6])]
    out, ctx = patched(bcm, MAX_SERIES=2)(lambda: graph_blocks(msgraph_object(cats, series)))
    assert "Chart: column (first 2 series)\n||X|Y|\n-|-|-\na|1|3\nb|2|4\n" in render(out, ctx), render(out, ctx)
    assert ctx.skipped == {"chart_cut": 1}, ctx.skipped
    out, ctx = patched(bcm, MAX_POINTS=5)(lambda: graph_blocks(msgraph_object(cats, series)))
    assert ctx.skipped == {"chart_cut": 1} and "points)" in render(out, ctx), (ctx.skipped, render(out, ctx))


def test_graph_codepage_65001_and_unsupported():
    cats, series = ["한글"], [("이름", [1])]
    out, ctx = graph_blocks(msgraph_object(cats, series, codepage=65001, label_codec="utf-8"))
    assert render(out, ctx) == "Chart: column\n||이름|\n-|-\n한글|1\n", render(out, ctx)
    # a page Python has no codec for: the chart is counted once, not shown with replacement characters
    out, ctx = graph_blocks(msgraph_object(cats, series, codepage=60000, label_codec="cp949"))
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)
    # ASCII labels need no codec
    out, ctx = graph_blocks(msgraph_object(["a"], [("S", [1])], codepage=60000, label_codec="ascii"))
    assert render(out, ctx) == "Chart: column\n||S|\n-|-\na|1\n", render(out, ctx)


def test_graph_point_count_against_selection():
    # a Series that says 3 points where the datasheet selection holds 2 is counted under chart (dochan: unsupported)
    obj = graph_streams(msgraph_object(["a", "b"], [("X", [1, 2])]))
    wb = obj["Workbook"]
    series = biff(0x1003, struct.pack("<6H", 1, 1, 2, 2, 0, 0))
    assert wb.count(series) == 1
    obj["Workbook"] = wb.replace(series, biff(0x1003, struct.pack("<6H", 1, 1, 2, 3, 0, 0)))
    from reader_fix import make_cfb
    path = Path(tempfile.mkdtemp()) / "g.cfb"
    make_cfb(path, obj)
    ctx = Context()
    with Compound(path) as cf:
        out = xls_embedded.blocks(cf, ctx, sheet_grid.Budget())
    assert out == [] and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def test_graph_errors_are_damaged():
    from reader.limits import ReadError
    obj = graph_streams(msgraph_object(*GRAPH))
    wb = obj["Workbook"]
    i = wb.index(struct.pack("<HH", 3, 15))
    cut = wb[:i] + struct.pack("<HH", 3, 8) + wb[i + 4:i + 12] + wb[i + 19:]
    try:
        bcm.graph(cut, Context())
    except ReadError as e:
        assert e.kind == "damaged" and "truncated MS Graph datasheet cell" in e.detail, (e.kind, e.detail)
    else:
        raise AssertionError("a cut datasheet cell was read")


def axis_title(link, text):
    return [(0x1025, bytes(32)), (0x1033, b""), (0x1027, struct.pack("<HHH", link, 0, 0)),
            (0x100D, struct.pack("<H", 0) + bytes([len(text), 0]) + text.encode("latin-1")), (0x1034, b"")]


def test_caption_direction_grouping_and_axes():
    cases = [(0, "column"), (1, "bar"), (2, "stacked column"), (1 | 2 | 4, "100% stacked bar")]
    for flags, name in cases:
        md = run([DATA, chart_sheet(biff_chart(AB, kinds=((0x1017, struct.pack("<HHH", 0, 0, flags)),)))])
        assert f"Chart: {name}\n||A|B|" in md, (flags, md)
    md = run([DATA, chart_sheet(biff_chart(AB, kinds=((0x101A, struct.pack("<H", 3)),)))])
    assert "Chart: 100% stacked area\n" in md, md
    titled = biff_chart(AB, title="Sales", extra=axis_title(3, "Quarter") + axis_title(2, "Units"))
    md = run([DATA, chart_sheet(titled)])
    assert "Chart: column — Sales (x: Quarter; y: Units)\n||A|B|" in md, md
    across = biff_chart(AB, kinds=((0x1017, struct.pack("<HHH", 0, 0, 1)),), extra=axis_title(3, "Quarter"))
    assert "Chart: bar (y: Quarter)\n" in run([DATA, chart_sheet(across)])


def test_data_range_names_the_sheet_cells():
    cells = {(0, 1): "A", (0, 2): "B", (1, 0): "Q1", (2, 0): "Q2", (1, 1): 1, (2, 1): 2, (1, 2): 3, (2, 2): 4}
    series = [{"ref": [(0, area3d(0, 0, 0, 1, 1)), (1, area3d(0, 1, 2, 1, 1)), (2, area3d(0, 1, 2, 0, 0))]},
              {"ref": [(0, area3d(0, 0, 0, 2, 2)), (1, area3d(0, 1, 2, 2, 2)), (2, area3d(0, 1, 2, 0, 0))]}]
    chart = biff_chart(series, cache=False)
    md = run([{"name": "Data", "cells": cells}, chart_sheet(chart)], globals_extra=externsheet(0))
    assert md.rstrip().endswith("Chart: column. Data: Data!A1:C3") and md.count("\nQ2|2|4\n") == 1, md
    md = run([{"name": "Data", "cells": cells, "state": 1}, chart_sheet(chart)], globals_extra=externsheet(0))
    assert "Chart: column. Data: Data!A1:C3\n||A|B|" in md and md.count("\nQ2|2|4\n") == 1, md
    md = run([{"name": "Data", "cells": cells, "hidden_rows": [1]}, chart_sheet(chart)], globals_extra=externsheet(0))
    assert "Chart: column. Data: Data!A1:C3\n||A|B|" in md and md.count("\nQ2|2|4\n") == 2, md
    md = run([{"name": "Data", "cells": cells, "merges": [(1, 0, 2, 0)]}, chart_sheet(chart)],
             globals_extra=externsheet(0))
    assert "Chart: column. Data: Data!A1:C3\n||A|B|" in md, md
    gap = {k: v for k, v in cells.items() if k != (2, 2)}
    md = run([{"name": "Data", "cells": gap}, chart_sheet(biff_chart(series))], globals_extra=externsheet(0))
    assert "Chart: column. Data: Data!A1:C3\n||A|B|" in md, md
    for first in ({"state": 1}, {}):
        twins = [{"name": "Data", "cells": cells, **first}, {"name": "data", "cells": cells}]
        for sheets in (twins, twins[::-1]):
            md = run(sheets + [chart_sheet(chart)], globals_extra=externsheet(0))
            assert md.rstrip().endswith("\nQ2|2|4"), (first, md)
    md = patched(limits, CHART_CELLS=6)(lambda: run([{"name": "Data", "cells": cells}, chart_sheet(chart)],
                                                     globals_extra=externsheet(0)))
    assert "Chart: column. Data: Data!A1:C3 (" in md and md.rstrip().endswith("\nQ1|1|3"), md
    half = [series[0], {"name": "Typed", "ref": series[1]["ref"][1:]}]
    md = run([{"name": "Data", "cells": cells}, chart_sheet(biff_chart(half, cache=False))],
             globals_extra=externsheet(0))
    assert "Data:" not in md and "\nQ2|2|4\n" in md, md


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
