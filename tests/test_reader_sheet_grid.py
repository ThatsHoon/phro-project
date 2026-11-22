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
from reader.formats.sheet_grid import Budget, Grid, address, text_inlines  # noqa: E402
from reader.model import Bold, Break, Context, Doc, Hidden, Link, NoteRef, Para, Table, Text  # noqa: E402


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


def test_append_and_covers():
    g, ctx = mk("S", Budget(cells=2, chars=100))
    assert g.put(0, 0, "a") and g.put(0, 1, "b")
    assert g.put(0, 2, "c") is False and g.cut
    assert g.append(0, 0, [NoteRef("n")]) and g.get(0, 0) == [Text("a"), NoteRef("n")]  # works on a cut grid
    assert g.append(5, 5, [NoteRef("n")]) is False  # a cell that holds nothing
    assert g.budget.cells == 0
    g2, _ = mk("T")
    g2.merge(0, 0, 0, 1)
    g2.merge(0, 1, 0, 2)  # overlaps the first, so it does not render
    assert g2.covers([(0, 0), (0, 1), (0, 2), (3, 3)]) == {(0, 1): (0, 0)}


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
    merges that start or end there, not every merge still open. The merges past GRID_SLOTS of area are one cell each,
    so each row below holds their 979 filler cells before its own, and the table is cut at GRID_SLOTS."""
    g, ctx = mk()
    for c in range(999):
        g.merge(0, 99_999, c, c)
        g.put(0, c, f"m{c}")
    for r in range(100_000):
        g.put(r, 999, str(r))
    t0 = time.monotonic()
    t = table(g)
    took = time.monotonic() - t0
    assert took < 5, took
    assert len(t.rows[0]) == 1000 and [c.rowspan for c in t.rows[0][19:21]] == [100_000, 1], t.rows[0][19:21]
    assert [txt(c) for c in t.rows[1]] == [""] * 979 + ["1"], t.rows[1][-2:]
    assert txt(t.rows[-1][-1]) == str(len(t.rows) - 1) and len(t.rows) < 100_000 and ctx.skipped["sheet_cut"] == 1


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


def test_put_inlines():
    g, _ = mk()
    inl = [Link("https://e.x", [Text("site")]), NoteRef("n1")]
    assert g.put(0, 0, inl)
    assert g.blocks() == [Para(inl)]


def test_inlines_in_table():
    g, _ = mk()
    inl = [Link("https://e.x", [Text("site")]), NoteRef("n1")]
    g.put(0, 0, inl)
    g.put(0, 1, "b")
    t = table(g)
    assert t.rows[0][0].blocks[0].inlines == inl and txt(t.rows[0][1]) == "b"


def test_inline_charge():
    b = Budget(cells=10, chars=12)
    g, _ = mk("S", b)
    assert g.put(0, 0, [Link("u" * 4, [Text("abcd")])])
    assert b.chars == 4 and b.cells == 9, (b.chars, b.cells)


def test_hidden_charge():
    # hidden inlines (deleted text) are charged like the text around them
    b = Budget(cells=10, chars=12)
    g, _ = mk("S", b)
    assert g.put(0, 0, [Text("ab"), Hidden([Bold([Text("cdef")])], "deleted")])
    assert b.chars == 6, b.chars
    g, ctx = mk("S", Budget(chars=5))
    assert not g.put(0, 0, [Text("ab"), Hidden([Text("cdef")], "deleted")])
    assert "sheet S cut at 5 characters" in ctx.warnings, ctx.warnings


def test_replace_refunds():
    b = Budget(cells=1, chars=5)
    g, _ = mk("S", b)
    assert g.put(0, 0, "abc")
    assert g.put(0, 0, "abcde") is True
    assert b.chars == 0 and b.cells == 0, (b.chars, b.cells)


def test_get_and_held():
    g, _ = mk()
    for p in ((0, 0), (5, 5), (9, 9)):
        g.put(*p, "v%d" % p[0])
    assert g.held(0, 9, 0, 9)[0] == [(0, 0), (5, 5), (9, 9)]
    assert g.held(0, 1048575, 0, 16383)[1] == 3
    assert g.held(4, 6, 4, 6) == ([(5, 5)], 3)
    assert g.get(5, 5) == "v5" and g.get(1, 1) is None


def test_text_inlines():
    assert text_inlines("a\nb") == [Text("a"), Break(), Text("b")]


def test_blocks_frees_its_intermediates():
    # blocks() lets go of its per-cell maps as it lays the rows out: while it builds the table, the memory it holds
    # above the grid is about the output's, not that plus the origin map and the row lists (at SHEET_CELLS these
    # were 108 MiB of a 364 MiB peak)
    import gc
    import tracemalloc
    g, _ = mk()
    for r in range(200):
        for c in range(100):
            g.put(r, c, f"{r * 100 + c:09d}")
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        out = g.blocks()
        kept, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out[0].rows) == 200 and txt(out[0].rows[199][99]) == "000019999"
    assert peak - base < 1.2 * (kept - base), (peak - base, kept - base)


def fill(g, r1, r2, c1, c2, tag=""):
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            assert g.put(r, c, f"{tag}{r},{c}")


def para_text(b):
    assert isinstance(b, Para), b
    return "".join(i.text for i in b.inlines if isinstance(i, Text))


def test_address():
    assert [address(0, 0), address(8, 3), address(99_999, 25), address(0, 26), address(4, 701), address(0, 702)] == \
        ["A1", "D9", "Z100000", "AA1", "ZZ5", "AAA1"]


def test_three_islands_three_tables():
    g, _ = mk()
    fill(g, 0, 8, 0, 3, "a")    # A1:D9
    fill(g, 4, 8, 6, 8, "b")    # G5:I9
    fill(g, 13, 17, 2, 4, "c")  # C14:E18
    out = g.sheet_blocks()
    assert [type(b) for b in out] == [Table] * 3, out
    assert [(t.caption, t.origin) for t in out] == [("A1:D9", "A1"), ("G5:I9", "G5"), ("C14:E18", "C14")], \
        [(t.caption, t.origin) for t in out]
    assert [len(t.rows) for t in out] == [9, 5, 5] and [len(t.rows[0]) for t in out] == [4, 3, 3]
    assert txt(out[1].rows[0][0]) == "b4,6" and txt(out[2].rows[4][2]) == "c17,4"


def test_lone_cell_island_is_an_addressed_paragraph():
    g, _ = mk()
    g.put(0, 0, "Quarterly report")
    fill(g, 2, 4, 0, 2)
    out = g.sheet_blocks()
    assert para_text(out[0]) == "A1: Quarterly report" and (out[1].caption, out[1].origin) == ("A3:C5", "A3"), out
    g, _ = mk()
    g.put(2, 3, "only")
    assert [para_text(b) for b in g.sheet_blocks()] == ["D3: only"]
    g, _ = mk()  # a lone merged cell is led by its range
    g.put(8, 0, "summary")
    g.merge(8, 8, 0, 4)
    assert [para_text(b) for b in g.sheet_blocks()] == ["A9:E9: summary"]


def test_parts_with_the_same_span_stay_one_table():
    g, _ = mk()
    fill(g, 0, 0, 0, 3)          # a header row
    fill(g, 2, 6, 0, 3)          # a blank row, then the data under the same columns
    fill(g, 10, 14, 0, 0)        # a spacer column C between two parts on the same rows
    fill(g, 10, 14, 3, 4)
    out = g.sheet_blocks()
    assert [t.caption for t in out] == ["A1:D7", "A11:E15"], [getattr(t, "caption", t) for t in out]
    assert len(out[0].rows) == 7 and out[0].rows[1] == []


def test_a_long_empty_run_splits_parts_with_the_same_span():
    g, _ = mk()
    fill(g, 0, 1, 0, 1)
    fill(g, 66, 67, 0, 1)   # 64 empty rows between: one table
    fill(g, 200, 201, 0, 1)  # 132 empty rows: a table of its own
    out = g.sheet_blocks()
    assert [t.caption for t in out] == ["A1:B68", "A201:B202"], [getattr(t, "caption", t) for t in out]
    assert len(out[0].rows) == 68 and not g.ctx.skipped


def test_merge_joins_its_parts_and_counts_in_the_range():
    g, _ = mk()
    g.put(0, 0, "title")
    g.merge(0, 0, 0, 4)       # A1:E1 over the blank row's columns
    fill(g, 2, 3, 0, 4)
    out = g.sheet_blocks()
    assert len(out) == 1 and out[0].caption == "A1:E4" and out[0].rows[0][0].colspan == 5, out


def test_hidden_runs_named_in_the_caption():
    g, _ = mk()
    fill(g, 0, 19, 0, 5)
    for r in (6, 11, 12, 13):
        g.hide_row(r)
    g.hide_cols(3, 3)
    g.hide_cols(30, 40)  # outside the table: not named
    g.put(25, 30, "far")
    t = g.sheet_blocks()[0]
    assert t.caption == "A1:F20 (hidden rows 7, 12-14; hidden columns D)", t.caption
    assert t.hidden_rows == frozenset({6, 11, 12, 13}) and t.hidden_cols == frozenset({3})


def test_hidden_runs_listed_up_to_the_cap():
    g, _ = mk()
    fill(g, 0, 99, 0, 1)
    for r in range(0, 100, 2):
        g.hide_row(r)
    cap = g.sheet_blocks()[0].caption
    shown = ", ".join(str(r + 1) for r in range(0, 2 * limits.SHEET_RUNS, 2))
    assert cap == f"A1:B100 (hidden rows {shown} and {50 - limits.SHEET_RUNS} more)", cap


def test_hidden_lone_cell_keeps_its_table():
    g, _ = mk()
    g.put(4, 1, "secret")
    g.hide_row(4)
    t = g.sheet_blocks()[0]
    assert isinstance(t, Table) and t.caption == "B5 (hidden rows 5)" and t.origin == "B5", t


def test_row_cut_named_where_it_is():
    g, ctx = mk()
    g.declare(0, 119_999, 0, 4)
    for r in range(100_001):
        if not g.put(r, 0, "v"):
            break
    out = g.sheet_blocks()
    assert out[0].caption == "A1:A100000" and len(out[0].rows) == 100_000
    assert para_text(out[-1]) == ("(Sheet cut at A100001 by the 100,000-row limit; not read from there to E120000, "
                                  "20,000 rows)"), para_text(out[-1])
    assert "sheet S cut at 100000 rows" in ctx.warnings
    g, _ = mk()
    g.put(0, 0, "a")
    g.stop("scanned XML elements", 7, (3, None))
    assert para_text(g.sheet_blocks()[-1]) == "(Sheet cut at row 4 by the workbook's 7-element limit; not read from there on)"


def test_sheet_after_the_cell_budget_says_it_was_not_read():
    b = Budget(cells=2)
    g1, _ = mk("A", b)
    fill(g1, 0, 0, 0, 1)
    g2, ctx = mk("B", b)
    g2.declare(0, 9, 0, 2)
    assert not g2.put(0, 0, "x")
    assert [para_text(x) for x in g2.sheet_blocks()] == [
        "(Sheet not read: the document's 2-cell limit was used up by the sheets before it; it spans A1:C10, 10 rows)"]
    g3, _ = mk("C", b)
    assert g3.sheet_blocks() == []  # nothing was put, so nothing was left out


def test_cut_inside_a_sheet_names_the_cell():
    g, _ = mk("S", Budget(cells=5))
    fill(g, 0, 0, 0, 2)
    assert g.put(1, 0, "x") and g.put(1, 1, "y") and not g.put(1, 2, "z")
    out = g.sheet_blocks()
    assert out[0].caption == "A1:C2" and para_text(out[-1]) == \
        "(Sheet cut at C2 by the document's 5-cell limit; not read from there on)", out


def test_island_work_budget_still_splits_long_runs():
    old = limits.SHEET_ISLAND_STEPS
    limits.SHEET_ISLAND_STEPS = 1
    try:
        g, _ = mk()
        fill(g, 0, 1, 0, 1)
        fill(g, 0, 1, 5, 5)
        fill(g, 300, 301, 0, 1)
        out = g.sheet_blocks()
        assert [t.caption for t in out] == ["A1:F2", "A301:B302"], [getattr(t, "caption", t) for t in out]
    finally:
        limits.SHEET_ISLAND_STEPS = old


def test_islands_are_cheap_on_a_full_sheet():
    g, _ = mk()
    for r in range(1_000):
        for c in range(100):
            g.put(r, c, "v")
    t0 = time.monotonic()
    out = g.sheet_blocks()
    took = time.monotonic() - t0
    assert len(out) == 1 and out[0].caption == "A1:CV1000" and took < 3, took


def test_tall_islands_charge_their_rows():
    # 15 sparse columns 66 apart are 15 islands of 100,000 rows; their rows, empty ones too, are held to GRID_SLOTS
    # for the sheet with its cells, so 23,000 cells cannot make 1.5 million table rows past it
    g, ctx = mk()
    for k in range(15):
        for r in range(0, limits.SHEET_ROWS, 64):
            g.put(r, 66 * k, "v")
    limits.GRID_SLOTS, saved = 500_000, limits.GRID_SLOTS
    try:
        out = g.sheet_blocks()
    finally:
        limits.GRID_SLOTS = saved
    tables = [b for b in out if isinstance(b, Table)]
    assert sum(len(t.rows) for t in tables) <= 500_000 and len(tables) < 15, len(tables)
    assert "sheet S cut at 500000 grid slots" in ctx.warnings, ctx.warnings


def test_spans_share_one_area_budget_a_sheet():
    # three islands, each a 1000 x 2 merge: past GRID_SLOTS of span area for the sheet (not for each table), a merge
    # is laid out as one cell, as the renderer lays out one table's
    g, ctx = mk()
    for k in range(3):
        g.put(2000 * k, 0, "m")
        g.merge(2000 * k, 2000 * k + 999, 0, 1)
        g.put(2000 * k, 2, "n")
    limits.GRID_SLOTS, saved = 5_000, limits.GRID_SLOTS
    try:
        out = g.sheet_blocks()
    finally:
        limits.GRID_SLOTS = saved
    tables = [b for b in out if isinstance(b, Table)]
    assert [(t.rows[0][0].rowspan, t.rows[0][0].colspan) for t in tables] == [(1000, 2), (1000, 2), (1, 1)], tables
    assert [len(t.rows) for t in tables] == [1000] * 3 and not ctx.warnings, ctx.warnings


def test_header_rows_marked():
    g, _ = mk()
    fill(g, 0, 2, 0, 1)
    g.head_rows(0, 0)
    t = g.sheet_blocks()[0]
    assert t.rows[0][0].header and t.rows[0][1].header and not t.rows[1][0].header


def test_record_blocks_keep_one_table():
    g, _ = mk()
    fill(g, 0, 1, 0, 0)
    fill(g, 0, 1, 2, 2)
    out = g.blocks()
    assert len(out) == 1 and out[0].caption == "" and out[0].origin == "" and len(out[0].rows[0]) == 3


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
