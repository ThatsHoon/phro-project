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
    cases = [("1e309", "0.0%", "1e309"), ("1e30", "#,##0", "1," + ",".join(["000"] * 10)),
             ("1" + "0" * 29, "#,##0", "100," + ",".join(["000"] * 9)), ("NaN", "0%", "NaN")]
    for v, fmt, want in cases:
        plot = "<c:barChart>" + ser("A", cache([(0, v)], fmt=fmt)) + "</c:barChart>"
        t = chart_table(space(plot))
        assert cells(t)[1][1] == want, (v, cells(t))


def test_strip_and_bubble():
    plot = "<c:barChart>" + ser("A", cache([(0, " 3.0 ")])) + "</c:barChart>"
    assert cells(chart_table(space(plot)))[1][1] == "3"
    b = ("<c:bubbleChart><c:ser><c:tx><c:v>S</c:v></c:tx>"
         f"<c:xVal><c:numRef>{cache([(0, '1')])}</c:numRef></c:xVal>"
         f"<c:yVal><c:numRef>{cache([(0, '3')])}</c:numRef></c:yVal>"
         f"<c:bubbleSize><c:numRef>{cache([(0, '9')])}</c:numRef></c:bubbleSize>"
         "</c:ser></c:bubbleChart>")
    assert cells(chart_table(space(b))) == [["x", "S"], ["1", "3"]]


def ref(f, items, kind="num", fmt=""):
    return f"<c:{kind}Ref><c:f>{f}</c:f>{cache(items, kind, fmt)}</c:{kind}Ref>"


def axis(tag, pos, title="", deleted=False):
    t = (f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>"
         if title else "")
    return (f'<c:{tag}><c:axId val="1"/><c:delete val="{1 if deleted else 0}"/><c:axPos val="{pos}"/>{t}'
            f"</c:{tag}>")


def test_caption_kind_direction_grouping_and_axes():
    one = ser("A", cache([(0, "1")]), [(0, "a")])
    cases = [('<c:barDir val="col"/><c:grouping val="clustered"/>', "column"),
             ('<c:barDir val="bar"/><c:grouping val="clustered"/>', "bar"),
             ('<c:barDir val="col"/><c:grouping val="stacked"/>', "stacked column"),
             ('<c:barDir val="bar"/><c:grouping val="percentStacked"/>', "100% stacked bar"),
             ('<c:barDir/>', "column")]
    for head, want in cases:
        t = chart_table(space(f"<c:barChart>{head}{one}</c:barChart>", "T"))
        assert t.caption == f"Chart: {want} — T", (head, t.caption)
    t = chart_table(space(f'<c:bar3DChart><c:barDir val="col"/><c:grouping val="standard"/>{one}</c:bar3DChart>'))
    assert t.caption == "Chart: 3-D column", t.caption
    t = chart_table(space(f'<c:lineChart><c:grouping val="stacked"/>{one}</c:lineChart>'))
    assert t.caption == "Chart: stacked line", t.caption
    plot = (f'<c:barChart><c:barDir val="bar"/>{one}</c:barChart>'
            + axis("catAx", "l", "Region") + axis("valAx", "b", "Units") + axis("valAx", "r", "Gone", deleted=True))
    t = chart_table(space(plot, "Sales"))
    assert t.caption == "Chart: bar — Sales (x: Units; y: Region)", t.caption
    plot = (f'<c:barChart><c:barDir val="col"/>{one}</c:barChart>' + axis("catAx", "b", "Year")
            + axis("valAx", "l", "Number of Ducks"))
    assert chart_table(space(plot)).caption == "Chart: column (x: Year; y: Number of Ducks)"
    linked = (f"<c:chartSpace {NS}><c:chart><c:title><c:tx><c:strRef><c:f>Sheet1!$B$1</c:f>"
              f"{cache([(0, 'Linked')], 'str')}</c:strRef></c:tx></c:title><c:plotArea>"
              f"<c:pieChart>{one}</c:pieChart></c:plotArea></c:chart></c:chartSpace>")
    assert chart_table(parse_xml(linked.encode())).caption == "Chart: pie — Linked"


def test_dates_and_general_digits():
    cat = f"<c:cat><c:numRef>{cache([(0, '46023'), (1, '46054')], fmt='mmm-yy')}</c:numRef></c:cat>"
    vals = cache([(0, "4.4000000000000004"), (1, "0.30000000000000004")], fmt="General")
    plot = f"<c:lineChart><c:ser><c:tx><c:v>S</c:v></c:tx>{cat}<c:val><c:numRef>{vals}</c:numRef></c:val></c:ser></c:lineChart>"
    assert cells(chart_table(space(plot))) == [["", "S"], ["Jan-26", "4.4"], ["Feb-26", "0.3"]]
    s1904 = (f"<c:chartSpace {NS}><c:date1904 val=\"1\"/><c:chart><c:plotArea>{plot}</c:plotArea></c:chart>"
             "</c:chartSpace>")
    assert cells(chart_table(parse_xml(s1904.encode())))[1][0] == "Jan-30"


def test_scatter_series_keep_their_own_x():
    def xy(name, xs, ys):
        return (f"<c:ser><c:tx><c:v>{name}</c:v></c:tx>"
                f"<c:xVal><c:numRef>{cache(list(enumerate(xs)))}</c:numRef></c:xVal>"
                f"<c:yVal><c:numRef>{cache(list(enumerate(ys)))}</c:numRef></c:yVal></c:ser>")
    plot = "<c:scatterChart>" + xy("Lab A", ["1", "2"], ["10", "20"]) + xy("Lab B", ["5", "7", "9"],
                                                                          ["1", "2", "3"]) + "</c:scatterChart>"
    assert cells(chart_table(space(plot))) == [["x", "Lab A", "x", "Lab B"], ["1", "10", "5", "1"],
                                               ["2", "20", "7", "2"], ["", "", "9", "3"]]
    same = "<c:scatterChart>" + xy("A", ["1", "2"], ["3", "4"]) + xy("B", ["1", "2"], ["5", "6"]) + "</c:scatterChart>"
    assert cells(chart_table(space(same))) == [["x", "A", "B"], ["1", "3", "5"], ["2", "4", "6"]]


def test_multi_level_categories():
    lvl = ('<c:multiLvlStrRef><c:multiLvlStrCache><c:ptCount val="4"/>'
           '<c:lvl><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt>'
           '<c:pt idx="2"><c:v>Q1</c:v></c:pt><c:pt idx="3"><c:v>Q2</c:v></c:pt></c:lvl>'
           '<c:lvl><c:pt idx="0"><c:v>2025</c:v></c:pt><c:pt idx="2"><c:v>2026</c:v></c:pt></c:lvl>'
           '</c:multiLvlStrCache></c:multiLvlStrRef>')
    vals = cache([(0, "1"), (1, "2"), (2, "3"), (3, "4")])
    plot = f"<c:barChart><c:ser><c:tx><c:v>S</c:v></c:tx><c:cat>{lvl}</c:cat><c:val><c:numRef>{vals}</c:numRef></c:val></c:ser></c:barChart>"
    assert cells(chart_table(space(plot))) == [["", "", "S"], ["2025", "Q1", "1"], ["", "Q2", "2"],
                                               ["2026", "Q1", "3"], ["", "Q2", "4"]]


def ranged(name_f, cat_f, val_f, name="A"):
    tx = f"<c:tx>{ref(name_f, [(0, name)], 'str')}</c:tx>" if name_f else ""
    cat = f"<c:cat>{ref(cat_f, [(0, 'a'), (1, 'b')], 'str')}</c:cat>" if cat_f else ""
    return f"<c:ser>{tx}{cat}<c:val>{ref(val_f, [(0, '1'), (1, '2')])}</c:val></c:ser>"


def test_data_range_instead_of_cells():
    cols = ("<c:barChart>" + ranged("Sheet1!$B$1", "Sheet1!$A$2:$A$3", "Sheet1!$B$2:$B$3")
            + ranged("Sheet1!$C$1", "Sheet1!$A$2:$A$3", "Sheet1!$C$2:$C$3", "B") + "</c:barChart>")
    t = chart_table(space(cols, "T"), sheets={"Sheet1"})
    assert t.caption == "Chart: bar — T. Data: Sheet1!A1:C3" and t.rows == [], (t.caption, t.rows)
    rows = ("<c:barChart>" + ranged("'My Data'!$A$2", "'My Data'!$B$1:$C$1", "'My Data'!$B$2:$C$2")
            + ranged("'My Data'!$A$3", "'My Data'!$B$1:$C$1", "'My Data'!$B$3:$C$3", "B") + "</c:barChart>")
    t = chart_table(space(rows), sheets={"My Data"})
    assert t.caption == "Chart: bar. Data: 'My Data'!A1:C3, series in rows" and t.rows == [], t.caption
    keep = [(cols, None), (cols, {"Other"}),
            ("<c:barChart>" + ranged("", "Sheet1!$A$2:$A$3", "Sheet1!$B$2:$B$3")
             + ranged("Sheet1!$C$1", "Sheet1!$A$2:$A$3", "Sheet1!$C$2:$C$3") + "</c:barChart>", {"Sheet1"}),
            ("<c:barChart>" + ranged("[1]Sheet1!$B$1", "[1]Sheet1!$A$2:$A$3", "[1]Sheet1!$B$2:$B$3")
             + "</c:barChart>", {"Sheet1"}),
            ("<c:barChart>" + ranged("Sheet1!$B$1", "Sheet1!$A$2:$A$3", "Sheet1!$B$2:$B$3")
             + ranged("Sheet1!$D$1", "Sheet1!$A$2:$A$3", "Sheet1!$D$2:$D$3") + "</c:barChart>", {"Sheet1"}),
            ("<c:barChart>" + ranged("Sheet1!$B$1", "Sheet1!$A$2:$A$3", "Sheet1!$B$2:$B$3")
             + ranged("Sheet2!$C$1", "Sheet1!$A$2:$A$3", "Sheet1!$C$2:$C$3") + "</c:barChart>", {"Sheet1", "Sheet2"}),
            ("<c:barChart><c:ser><c:tx><c:v>L</c:v></c:tx><c:val>" + ref("Sheet1!$B$2:$B$3", [(0, "1")])
             + "</c:val></c:ser></c:barChart>", {"Sheet1"})]
    for plot, sheets in keep:
        t = chart_table(space(plot), sheets=sheets)
        assert "Data:" not in t.caption and len(t.rows) > 1, (sheets, t.caption)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
