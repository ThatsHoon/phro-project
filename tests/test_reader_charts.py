"""DrawingML chart caches to tables.

Run: python -I -X utf8 tests/test_reader_charts.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.charts import chart_table  # noqa: E402
from reader.containers.zipx import parse_xml  # noqa: E402

NS = ('xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')


def space(plot, title=""):
    t = (f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>"
         if title else "")
    xml = f"<c:chartSpace {NS}><c:chart>{t}<c:plotArea>{plot}</c:plotArea></c:chart></c:chartSpace>"
    return parse_xml(xml.encode("utf-8"))


def cache(items, kind="num", fmt=""):
    f = f"<c:formatCode>{fmt}</c:formatCode>" if fmt else ""
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in items)
    return f'<c:{kind}Cache>{f}<c:ptCount val="{len(items)}"/>{pts}</c:{kind}Cache>'


def ser(name, val, cat=None):
    c = f"<c:cat><c:strRef>{cache(cat, 'str')}</c:strRef></c:cat>" if cat else ""
    tx = f"<c:tx><c:strRef>{cache([(0, name)], 'str')}</c:strRef></c:tx>" if name else ""
    return f"<c:ser>{tx}{c}<c:val><c:numRef>{val}</c:numRef></c:val></c:ser>"


def cells(t):
    return [["".join(x.text for b in c.blocks for x in b.inlines) for c in row] for row in t.rows]


def test_bar_by_idx():
    cat = [(0, "1분기"), (1, "2분기")]
    plot = ("<c:barChart>"
            + ser("A", cache([(0, "5200"), (1, "4800")]), cat)
            + ser("B", cache([(1, "20"), (0, "10")]))
            + "</c:barChart>")
    t = chart_table(space(plot, "매출"))
    assert t.caption == "Chart: bar — 매출"
    assert cells(t) == [["", "A", "B"], ["1분기", "5200", "10"], ["2분기", "4800", "20"]]


def test_percent_and_thousands():
    plot = ("<c:barChart>"
            + ser("P", cache([(0, "0.125")], fmt="0.0%"), [(0, "a")])
            + ser("N", cache([(0, "1234567")], fmt="#,##0"))
            + ser("Q", cache([(0, "0.5")], fmt="0%"))
            + "</c:barChart>")
    t = chart_table(space(plot))
    assert t.caption == "Chart: bar"
    assert cells(t) == [["", "P", "N", "Q"], ["a", "12.5%", "1,234,567", "50%"]]


def test_plain_values_and_missing():
    plot = ("<c:lineChart>"
            + ser("", cache([(0, "3.0"), (2, "x.0"), (1, "2.5")]), [(0, "a"), (2, "c")])
            + "</c:lineChart>")
    t = chart_table(space(plot))
    assert cells(t) == [["", "Series 1"], ["a", "3"], ["", "2.5"], ["c", "x"]]


def test_scatter():
    plot = ("<c:scatterChart><c:ser><c:tx><c:v>S</c:v></c:tx>"
            f"<c:xVal><c:numRef>{cache([(0, '1'), (1, '2')])}</c:numRef></c:xVal>"
            f"<c:yVal><c:numRef>{cache([(0, '3'), (1, '4')])}</c:numRef></c:yVal>"
            "</c:ser></c:scatterChart>")
    t = chart_table(space(plot))
    assert cells(t) == [["x", "S"], ["1", "3"], ["2", "4"]]


def test_combo_type():
    plot = ("<c:barChart>" + ser("A", cache([(0, "1")]), [(0, "a")]) + "</c:barChart>"
            "<c:lineChart>" + ser("B", cache([(0, "2")])) + "</c:lineChart>")
    t = chart_table(space(plot, "T"))
    assert t.caption.startswith("Chart: bar+line")
    assert cells(t)[0] == ["", "A", "B"]


def test_no_series_none():
    assert chart_table(space("<c:barChart></c:barChart>")) is None
    assert chart_table(space("")) is None


def test_row_cap_is_fast():
    import time
    big = ('<c:numCache><c:ptCount val="999999999"/>'
           '<c:pt idx="999999999"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt></c:numCache>')
    plot = "<c:barChart>" + ser("A", big) + "</c:barChart>"
    t0 = time.time()
    t = chart_table(space(plot))
    assert time.time() - t0 < 1
    assert len(t.rows) <= 10_001
    assert t.caption.endswith("(first 10000 points)")


def test_rows_follow_points_not_ptCount_and_series_are_capped():
    from reader import limits
    from reader.model import Context
    empty = '<c:numCache><c:ptCount val="10000"/></c:numCache>'
    ctx = Context()
    t = chart_table(space("<c:barChart>" + ser("A", empty) * 50 + "</c:barChart>"), ctx)
    assert len(t.rows) == 1 and t.caption == "Chart: bar" and not ctx.skipped
    plot = "<c:barChart>" + ser("A", cache([(0, "1")])) * 1500 + "</c:barChart>"
    t = chart_table(space(plot), ctx)
    assert len(t.rows[0]) == limits.GRID_COLS and t.caption == f"Chart: bar (first {limits.GRID_COLS - 1} series)"
    assert ctx.skipped["chart_cut"] == 1


def test_fmt_overflow_and_nan():
    cases = [("1e309", "0.0%"), ("1e30", "#,##0"), ("1" + "0" * 29, "#,##0"), ("NaN", "0%")]
    for v, fmt in cases:
        plot = "<c:barChart>" + ser("A", cache([(0, v)], fmt=fmt)) + "</c:barChart>"
        t = chart_table(space(plot))
        assert cells(t)[1][1] == v, (v, cells(t))


def test_strip_and_bubble():
    plot = "<c:barChart>" + ser("A", cache([(0, " 3.0 ")])) + "</c:barChart>"
    assert cells(chart_table(space(plot)))[1][1] == "3"
    b = ("<c:bubbleChart><c:ser><c:tx><c:v>S</c:v></c:tx>"
         f"<c:xVal><c:numRef>{cache([(0, '1')])}</c:numRef></c:xVal>"
         f"<c:yVal><c:numRef>{cache([(0, '3')])}</c:numRef></c:yVal>"
         f"<c:bubbleSize><c:numRef>{cache([(0, '9')])}</c:numRef></c:bubbleSize>"
         "</c:ser></c:bubbleChart>")
    assert cells(chart_table(space(b))) == [["x", "S"], ["1", "3"]]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
