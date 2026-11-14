"""The sparse sheet-grid builder xls, xlsb and embedded workbooks share.

Run: python -I -X utf8 tests/test_reader_sheet_grid.py
"""
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.formats.sheet_grid import Budget, Grid  # noqa: E402
from reader.model import Break, Context, Doc, Para, Table, Text  # noqa: E402


def mk(name="S", budget=None):
    ctx = Context()
    return Grid(name, budget or Budget(), ctx), ctx


def txt(cell):
    return "".join(i.text for b in cell.blocks for i in b.inlines if isinstance(i, Text))


def table(g):
    out = g.blocks()
    assert len(out) == 1 and isinstance(out[0], Table), out
    return out[0]


def test_dense_table():
    g, _ = mk()
    for r, c, s in ((0, 0, "a"), (0, 1, "b"), (1, 0, "c"), (1, 1, "d")):
        assert g.put(r, c, s)
    t = table(g)
    assert [[txt(x) for x in row] for row in t.rows] == [["a", "b"], ["c", "d"]]


def test_any_order_and_replace():
    g, _ = mk()
    g.put(1, 1, "d")
    g.put(0, 0, "a")
    g.put(1, 1, "D")
    t = table(g)
    assert [[txt(x) for x in row] for row in t.rows] == [["a"], ["", "D"]]
    g2, _ = mk()
    assert g2.put(0, 0, "") and g2.blocks() == []


def test_leading_empty_trimmed():
    g, _ = mk()
    g.put(5, 3, "a")
    g.put(5, 4, "b")
    g.put(6, 3, "c")
    t = table(g)
    assert [[txt(x) for x in row] for row in t.rows] == [["a", "b"], ["c"]]


def test_gap_capped():
    g, ctx = mk()
    g.put(0, 0, "a")
    g.put(60000, 0, "z")
    t0 = time.monotonic()
    t = table(g)
    assert time.monotonic() - t0 < 1
    assert len(t.rows) == 66 and ctx.skipped["repeat"] == 1, (len(t.rows), ctx.skipped)
    assert txt(t.rows[-1][0]) == "z"


def test_huge_extent_is_cheap():
    g, _ = mk()
    g.put(0, 0, "a")
    g.put(99_999, 999, "z")
    t0 = time.monotonic()
    g.blocks()
    assert time.monotonic() - t0 < 2


def test_merge_spans():
    g, _ = mk()
    g.put(0, 0, "a")
    g.put(1, 1, "covered")
    g.merge(0, 1, 0, 1)
    t = table(g)
    cell = t.rows[0][0]
    assert cell.rowspan == 2 and cell.colspan == 2 and txt(cell) == "a"
    assert "covered" not in str([[txt(x) for x in row] for row in t.rows])


def test_merge_dropped_when_reversed_or_overlapping():
    g, _ = mk()
    g.put(0, 0, "a")
    g.put(0, 3, "b")
    g.merge(1, 0, 0, 0)  # reversed
    g.merge(0, 1, 0, 1)
    g.merge(1, 2, 1, 2)  # overlaps the earlier merge at (1,1)
    t = table(g)
    assert (t.rows[0][0].rowspan, t.rows[0][0].colspan) == (2, 2)


def test_hidden_merge_origin():
    g, _ = mk()
    g.hide_row(0)
    g.put(0, 0, "keep me")
    g.merge(0, 1, 0, 0)
    out = markdown.render(Doc(blocks=g.blocks()), Path(tempfile.mkdtemp()))
    assert "keep me" in out, out


def test_hidden_rows_and_cols_trimmed():
    g, _ = mk()
    g.put(0, 1, "a")
    g.put(0, 2, "b")
    g.put(0, 3, "c")
    g.put(1, 1, "d")
    g.hide_cols(2, 2)
    g.hide_row(1)
    t = table(g)
    assert t.hidden_cols == frozenset({1}) and t.hidden_rows == frozenset({1}), (t.hidden_cols, t.hidden_rows)


def test_hidden_cols_trimmed():
    g, _ = mk()
    for c in (1, 2, 3):
        g.put(0, c, "x")
    g.hide_cols(2, 2)
    assert table(g).hidden_cols == frozenset({1})


def test_col_cap():
    old = limits.SHEET_COLS
    limits.SHEET_COLS = 3
    try:
        g, ctx = mk("S")
        assert g.put(0, 0, "a")
        assert g.put(0, 5, "x") is False
        assert ctx.skipped["sheet_cut"] == 1 and "sheet S cut at 3 columns" in ctx.warnings
        assert g.put(0, 1, "y") is False and ctx.skipped["sheet_cut"] == 1
    finally:
        limits.SHEET_COLS = old


def test_row_cap():
    old = limits.SHEET_ROWS
    limits.SHEET_ROWS = 4
    try:
        g, ctx = mk()
        assert g.put(3, 0, "a") and not g.put(4, 0, "b")
        assert "sheet S cut at 4 rows" in ctx.warnings
    finally:
        limits.SHEET_ROWS = old


def test_budget_shared():
    b = Budget(cells=3)
    g1, c1 = mk("A", b)
    g2, c2 = mk("B", b)
    assert g1.put(0, 0, "a") and g1.put(0, 1, "b")
    assert g2.put(0, 0, "c")
    assert g2.put(0, 1, "d") is False
    assert c2.skipped["sheet_cut"] == 1 and "sheet B cut at 3 cells" in c2.warnings
    assert not c1.skipped


def test_chars_budget():
    g, ctx = mk("S", Budget(chars=5))
    assert g.put(0, 0, "abc") and not g.put(0, 1, "def")
    assert ctx.skipped["sheet_cut"] == 1 and "characters" in ctx.warnings[0]


def test_lone_cell():
    g, _ = mk()
    g.put(3, 3, "only")
    out = g.blocks()
    assert len(out) == 1 and isinstance(out[0], Para) and out[0].inlines == [Text("only")]
    assert mk()[0].blocks() == []


def test_newline_break():
    g, _ = mk()
    g.put(0, 0, "a\nb")
    assert g.blocks() == [Para([Text("a"), Break(), Text("b")])]


def test_merge_cap():
    g, ctx = mk()
    g.put(0, 0, "a")
    for i in range(100_000):
        g.merge(i, i, 0, 1)
    assert not ctx.skipped
    g.merge(0, 0, 5, 6)
    g.merge(1, 1, 5, 6)
    assert ctx.skipped["sheet_cut"] == 1


def test_dense_merges_are_fast():
    g, _ = mk()
    t0 = time.monotonic()
    for r in range(40):  # 20,000 1x2 merges packed into the first rows
        for k in range(500):
            g.merge(r, r, 2 * k, 2 * k + 1)
            g.put(r, 2 * k, "x")
            g.put(r, 2 * k + 1, "covered")
    g.put(5, 999, "tail")
    t = table(g)
    assert time.monotonic() - t0 < 3
    assert t.rows[0][0].colspan == 2 and txt(t.rows[0][1]) == "x"
    assert not any("covered" in txt(c) for row in t.rows for c in row)


def test_tall_merges_are_fast():
    g, _ = mk()
    t0 = time.monotonic()
    for k in range(900):  # tall 1-column merges, stacked twice
        g.merge(0, 40_000, k, k)
        g.merge(40_001, 99_999, k, k)
        g.put(0, k, "a")
        g.put(40_001, k, "b")
        g.put(20_000, k, "covered")
    t = table(g)
    assert time.monotonic() - t0 < 3
    assert len(t.rows) <= 140 and t.rows[0][0].rowspan > 1


def test_open_merges_do_not_cost_every_row():
    """999 full-height merges, each in its own column, over 100,000 one-cell rows: a row costs its own cells and the
    merges that start or end there, not every merge still open."""
    g, _ = mk()
    for c in range(999):
        g.merge(0, 99_999, c, c)
        g.put(0, c, f"m{c}")
    for r in range(100_000):
        g.put(r, 999, str(r))
    t0 = time.monotonic()
    t = table(g)
    took = time.monotonic() - t0
    assert took < 5, took
    assert len(t.rows) == 100_000 and len(t.rows[0]) == 1000 and t.rows[0][0].rowspan == 100_000
    assert [txt(c) for c in t.rows[1]] == ["1"] and [txt(c) for c in t.rows[-1]] == ["99999"], (t.rows[1], t.rows[-1])


def test_merge_cap_dense():
    g, ctx = mk()
    t0 = time.monotonic()
    for i in range(100_001):
        g.merge(i // 500, i // 500, 2 * (i % 500), 2 * (i % 500) + 1)
    for r in range(200):
        for c in range(0, 1000, 2):
            g.put(r, c, "v")
    g.blocks()
    assert time.monotonic() - t0 < 6
    assert ctx.skipped["sheet_cut"] == 1


def test_overlap_in_sweep_drops_later():
    g, _ = mk()
    g.put(0, 0, "a")
    g.put(1, 1, "b")
    g.merge(0, 1, 0, 1)
    g.merge(1, 2, 1, 2)
    t = table(g)
    assert (t.rows[0][0].rowspan, t.rows[0][0].colspan) == (2, 2) and len(t.rows) == 2


def test_span_over_hidden_and_visible_counts_hidden_text():
    # a merge over a hidden and a visible column loses the hidden column's share of its text: one place, counted
    g, _ = mk()
    g.put(0, 0, "wide")
    g.put(1, 0, "x")
    g.merge(0, 0, 0, 1)
    g.hide_cols(1, 1)
    out = markdown.render(Doc(blocks=g.blocks()), Path(tempfile.mkdtemp()))
    assert "(1 hidden places left out)" in out, out
    shown = markdown.render(Doc(blocks=g.blocks()), Path(tempfile.mkdtemp()), hidden=True)
    assert "hidden places" not in shown, shown


def test_merge_over_hidden_rows_or_cols_only():
    # a merge whose rows are all hidden, or whose columns are all hidden, keeps its text at its first slot
    g, _ = mk()
    g.put(0, 0, "r")
    g.put(2, 0, "z")
    g.hide_row(0)
    g.hide_row(1)
    g.merge(0, 1, 0, 0)
    t = table(g)
    assert (t.rows[0][0].rowspan, t.hidden_rows) == (2, frozenset({0, 1})), (t.rows[0][0].rowspan, t.hidden_rows)
    assert "r" in markdown.render(Doc(blocks=[t]), Path(tempfile.mkdtemp()), hidden=True)
    g, _ = mk()
    g.put(0, 0, "c")
    g.put(1, 3, "z")
    g.hide_cols(0, 1)
    g.merge(0, 0, 0, 1)
    t = table(g)
    assert (t.rows[0][0].colspan, t.hidden_cols) == (2, frozenset({0, 1})), (t.rows[0][0].colspan, t.hidden_cols)


def test_sweep_orders_merges_by_reading_order():
    # the merge whose first cell is first in reading order wins an overlap, whatever its last row
    g, _ = mk()
    g.put(0, 0, "a")
    g.put(0, 1, "b")
    g.merge(0, 5, 0, 1)   # first cell (0, 0)
    g.merge(0, 1, 1, 2)   # first cell (0, 1); ends on an earlier row
    t = table(g)
    assert (t.rows[0][0].rowspan, t.rows[0][0].colspan) == (6, 2), (t.rows[0][0].rowspan, t.rows[0][0].colspan)


def test_duplicate_merges_do_not_count_toward_the_cap():
    g, ctx = mk()
    g.put(0, 0, "a")
    for _ in range(100_005):
        g.merge(0, 1, 0, 1)
    assert not ctx.skipped and len(g.merges) == 1, (dict(ctx.skipped), len(g.merges))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
