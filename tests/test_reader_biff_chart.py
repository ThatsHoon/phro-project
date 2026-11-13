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
from reader import limits  # noqa: E402
from reader_fix import area3d, biff_chart, biff_workbook, externsheet, make_xls  # noqa: E402

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
    assert "## Sheet: Chart1" in md and md.index("top") < md.index("Chart: bar"), md
    assert "Chart: bar — Sales\n||A|B|\n|---|---|---|\n|Q1|1|4|\n|Q2|2|5|\n|Q3|3|6|\n" in md, md
    assert "could not read" not in md, md


def test_cache_format():
    # the BRAI's ifmt (9 is 0%) formats a cached number; a cached string stays as it is
    md = run([DATA, chart_sheet(biff_chart([{"name": "R", "vals": [0.5, 0.25], "cats": ["a", "b"], "ifmt": 9}]))])
    assert "|a|50%|\n|b|25%|\n" in md, md


def test_embedded_chart():
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "records": [biff_chart(AB, title="Sales")]}
    md = run([sheet])
    assert md.index("cell") < md.index("Chart: bar — Sales"), md
    assert "|Q2|2|5|" in md, md
    two = {"name": "S", "cells": {(0, 0): "cell"}, "records": [biff_chart(AB, title="One"), biff_chart(AB, title="Two")]}
    md = run([two])
    assert md.index("Chart: bar — One") < md.index("Chart: bar — Two"), md


def test_chart_kinds():
    donut, plain = struct.pack("<HHH", 0, 50, 0), struct.pack("<HHH", 0, 0, 0)
    cases = [((0x1019, donut),), ((0x1019, plain),), ((0x101B, struct.pack("<HHH", 0, 0, 1)),),
             ((0x101B, struct.pack("<HHH", 0, 0, 0)),), ((0x1018, bytes(2)),), ((0x101A, bytes(6)),),
             ((0x1017, bytes(6)), (0x103A, bytes(14))), ((0x1017, bytes(6)),), ((0x103E, bytes(4)),),
             ((0x1040, bytes(4)),), ((0x103F, bytes(4)),), ((0x1018, bytes(2)), (0x1017, bytes(6)))]
    want = ["doughnut", "pie", "bubble", "scatter", "line", "area", "bar3D", "bar", "radar", "radar", "surface",
            "line+bar"]
    for kinds, name in zip(cases, want):
        md = run([DATA, chart_sheet(biff_chart(AB, kinds=kinds))])
        assert f"Chart: {name}\n||A|B|" in md, (name, md)


def test_brai_fallback():
    sheet = {"name": "Sheet1", "cells": {(0, 0): "Q1", (1, 0): "Q2", (2, 0): "Q3", (0, 1): ("fmt", 1234.5, "#,##0.00"),
                                         (1, 1): 7, (2, 1): "txt"}}
    refs = [(1, area3d(0, 0, 2, 1, 1)), (2, area3d(0, 0, 2, 0, 0))]
    chart = biff_chart([{"name": "V", "ref": refs}], cache=False)
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0))
    assert "Chart: bar\n||V|\n|---|---|\n|Q1|1,234.50|\n|Q2|7|\n|Q3|txt|\n" in md, md
    # a reference to a sheet with no cells leaves them empty and breaks nothing
    chart = biff_chart([{"name": "V", "ref": [(1, area3d(1, 0, 2, 1, 1))]}], cache=False)
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0, 9))
    assert "Chart: bar\n||V|\n|---|---|\n" in md and "1,234" not in md.split("Chart: bar")[1], md


def test_huge_area_bounded():
    sheet = {"name": "Sheet1", "cells": {(0, 0): "a", (1, 0): "b", (2, 0): "c"}}
    chart = biff_chart([{"name": "V", "ref": [(1, area3d(0, 0, 65535, 0, 255)), (2, area3d(0, 0, 65535, 0, 255))]}],
                       cache=False)
    t0 = time.monotonic()
    md = run([sheet, chart_sheet(chart)], globals_extra=externsheet(0))
    assert time.monotonic() - t0 < 1, time.monotonic() - t0
    assert "Chart: bar" in md, md


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
    assert md.count("Chart: bar") == 1 and "Chart: bar — One" in md, md
    assert "(could not read: 1 chart)" in md, md


def test_auxiliary_series_skipped():
    # a Series carrying SerParent (a trendline) is no column; the series after it keep their own cache column
    real = [{"name": "A", "vals": [1, 2], "cats": ["x", "y"]}, {"name": "T", "aux": True},
            {"name": "B", "vals": [3, 4], "cats": ["x", "y"]}]
    md = run([DATA, chart_sheet(biff_chart(real))])
    assert "Chart: bar\n||A|B|\n|---|---|---|\n|x|1|3|\n|y|2|4|\n" in md, md


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
