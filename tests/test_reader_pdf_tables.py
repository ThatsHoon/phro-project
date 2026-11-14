"""pdf tables from rule segments: grid assembly, merged and nested cells, caps.

Run: python -I -X utf8 tests/test_reader_pdf_tables.py
"""
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits  # noqa: E402
from reader.formats import pdf_layout as L  # noqa: E402
from reader.formats import pdf_tables as T  # noqa: E402
from reader.formats.pdf_layout import Fragment  # noqa: E402
from reader.formats.pdf_tables import Segment  # noqa: E402
from reader.model import Para, Table, Text  # noqa: E402


def frag(text, x, y, order, size=10):
    return Fragment(x=x, y=y, size=size, text=text, width=len(text) * size * 0.5,
                    space_width=size * 0.25, order=order)


def grid(x0, y0, cell_w, cell_h, rows, cols, skip_v=(), skip_h=()):
    """Rules of a rows x cols grid with its top-left at (x0, y0 + rows * cell_h); y is up.
    skip_v: (row, col) inner vertical segments left out; skip_h: (row, col) inner horizontal
    segments left out (row = line index 1..rows-1, col = cell column)."""
    segs = []
    top = y0 + rows * cell_h
    for i in range(rows + 1):
        y = top - i * cell_h
        for c in range(cols):
            if (i, c) in skip_h:
                continue
            segs.append(Segment(x0 + c * cell_w, y, x0 + (c + 1) * cell_w, y))
    for j in range(cols + 1):
        x = x0 + j * cell_w
        for r in range(rows):
            if (r, j) in skip_v:
                continue
            segs.append(Segment(x, top - (r + 1) * cell_h, x, top - r * cell_h))
    return segs


def words(cells, x0, y0, cell_w, cell_h, rows, cols, start=0):
    top = y0 + rows * cell_h
    out = []
    for k, t in enumerate(cells):
        r, c = divmod(k, cols)
        out.append(frag(t, x0 + c * cell_w + 4, top - (r + 1) * cell_h + 8, start + k))
    return out


def text_of(cell):
    out = []
    for b in cell.blocks:
        if isinstance(b, Para):
            out.append("".join(i.text for i in b.inlines if isinstance(i, Text)))
    return "\n".join(out)


def rows_of(table):
    return [[text_of(c) for c in row] for row in table.rows]


def run(segs, frags, **kw):
    cut = Counter()
    found = T.tables(segs, frags, 1, T.TableBudget(), cut, **kw)
    return found, cut


def test_ruled_grid():
    segs = grid(100, 100, 60, 30, 3, 2)
    frags = words(list("abcdef"), 100, 100, 60, 30, 3, 2)
    found, cut = run(segs, frags)
    assert len(found) == 1 and not cut
    f = found[0]
    assert isinstance(f.table, Table)
    assert rows_of(f.table) == [["a", "b"], ["c", "d"], ["e", "f"]]
    assert f.used == {0, 1, 2, 3, 4, 5}
    assert f.bbox == (100, 100, 220, 190)


def test_colspan():
    segs = grid(100, 100, 60, 30, 3, 2, skip_v=((1, 1),))
    frags = words(list("abcdef"), 100, 100, 60, 30, 3, 2)
    frags[2] = frag("c", 104, 100 + 3 * 30 - 2 * 30 + 8, 2)
    del frags[3]
    found, _ = run(segs, frags)
    rows = found[0].table.rows
    assert [len(r) for r in rows] == [2, 1, 2]
    assert rows[1][0].colspan == 2 and rows[1][0].rowspan == 1
    assert text_of(rows[1][0]) == "c"


def test_rowspan():
    # the horizontal rule between row 0 and row 1 is missing in column 0
    segs = grid(100, 100, 60, 30, 3, 2, skip_h=((1, 0),))
    frags = words(list("abcdef"), 100, 100, 60, 30, 3, 2)
    del frags[2]  # the cell under "a" is part of "a"
    found, _ = run(segs, frags)
    rows = found[0].table.rows
    assert [len(r) for r in rows] == [2, 1, 2]
    assert rows[0][0].rowspan == 2 and rows[0][0].colspan == 1
    assert text_of(rows[1][0]) == "d"


def test_nested_table():
    outer = grid(100, 100, 150, 100, 2, 2)
    inner = grid(110, 210, 40, 20, 2, 2)  # inside cell (0, 0): x 100-250, y 200-300 -> inner y 210-250
    frags = [frag("p", 112, 218, 0), frag("q", 152, 218, 1), frag("r", 112, 238, 2), frag("s", 152, 238, 3),
             frag("x", 254, 250, 4), frag("y", 104, 150, 5), frag("z", 254, 150, 6)]
    found, cut = run(outer + inner, frags)
    assert len(found) == 1, found
    top = found[0].table
    cell = top.rows[0][0]
    nested = [b for b in cell.blocks if isinstance(b, Table)]
    assert len(nested) == 1
    assert sorted(t for row in rows_of(nested[0]) for t in row) == ["p", "q", "r", "s"]
    assert found[0].used == {0, 1, 2, 3, 4, 5, 6}


def test_rule_light_stays_text():
    segs = [Segment(100, 100 + 30 * i, 220, 100 + 30 * i) for i in range(4)]
    frags = words(list("abcdef"), 100, 100, 60, 30, 3, 2)
    found, cut = run(segs, frags)
    assert found == [] and not cut


def test_cell_korean_join():
    m = L.load_model()
    segs = grid(100, 100, 120, 60, 2, 2)
    f1 = frag("가나", 104, 100 + 2 * 60 - 20, 0)
    f2 = frag("다라", 104, 100 + 2 * 60 - 32, 1)
    f3 = frag("m", 224, 100 + 2 * 60 - 20, 2)
    f4 = frag("n", 104, 100 + 60 - 20, 3)
    f5 = frag("o", 224, 100 + 60 - 20, 4)
    found, _ = run(segs, [f1, f2, f3, f4, f5], model=m)
    got = text_of(found[0].table.rows[0][0])
    assert got == L.join_break("가나", "다라", m), got


def test_page_cell_cap():
    segs = grid(100, 100, 60, 30, 2, 2)
    frags = words(list("abcd"), 100, 100, 60, 30, 2, 2)
    old = T.MAX_PAGE_CELLS
    T.MAX_PAGE_CELLS = 3
    try:
        found, cut = run(segs, frags)
    finally:
        T.MAX_PAGE_CELLS = old
    assert found == [] and cut["cells"] == 1 and sum(cut.values()) == 1


def test_intersection_cap():
    segs = [Segment(0, 3 * i, 4500, 3 * i) for i in range(1500)]
    segs += [Segment(3 * j, 0, 3 * j, 4500) for j in range(1500)]
    frags = [frag("a", 5, 5, 0)]
    old = T.MAX_PAGE_CELLS
    T.MAX_PAGE_CELLS = 10 ** 7
    try:
        t0 = time.time()
        found, cut = run(segs, frags)
        spent = time.time() - t0
    finally:
        T.MAX_PAGE_CELLS = old
    assert found == [] and cut["cells"] == 1 and sum(cut.values()) == 1
    assert spent < 3, spent


def test_component_lines_cap():
    segs = grid(100, 100, 60, 30, 3, 3)
    assert len(segs) == 24
    # a 3x3 grid has 4 horizontal + 4 vertical lines: 8 rules
    frags = words(list("abcdefghi"), 100, 100, 60, 30, 3, 3)
    old = T.MAX_COMPONENT_LINES
    T.MAX_COMPONENT_LINES = 4
    try:
        found, cut = run(segs, frags)
    finally:
        T.MAX_COMPONENT_LINES = old
    assert found == [] and cut["cells"] == 1 and sum(cut.values()) == 1


def test_dash_rules():
    # the middle rules are solid on one side and drawn in 0.5-unit dashes on the other
    long_segs = [Segment(100, 100, 220, 100), Segment(100, 160, 220, 160),
                 Segment(100, 100, 100, 160), Segment(220, 100, 220, 160),
                 Segment(160, 100, 160, 130), Segment(100, 130, 160, 130)]
    short = []
    x = 160.0
    while x < 220:
        short.append(Segment(x, 130, x + 0.5, 130))
        x += 1.0
    y = 130.0
    while y < 160:
        short.append(Segment(160, y, 160, y + 0.5))
        y += 1.0
    frags = [frag("a", 104, 108, 0), frag("b", 164, 108, 1), frag("c", 104, 138, 2), frag("d", 164, 138, 3)]
    plain, _ = run(long_segs, frags)
    assert not plain or rows_of(plain[0].table) != [["c", "d"], ["a", "b"]]
    found, _ = run(long_segs, frags, short=short)
    assert len(found) == 1
    assert rows_of(found[0].table) == [["c", "d"], ["a", "b"]]


def test_table_nodes_cap():
    segs = grid(100, 100, 60, 30, 2, 2)
    frags = words(list("abcd"), 100, 100, 60, 30, 2, 2)
    budget = T.TableBudget()
    budget.nodes = 0
    cut = Counter()
    assert T.tables(segs, frags, 1, budget, cut) == []
    assert cut["cells"] == 1
    assert limits.TABLE_NODES == T.TableBudget().nodes


def test_budget_spent_by_cells():
    segs = grid(100, 100, 60, 30, 2, 2)
    frags = words(list("abcd"), 100, 100, 60, 30, 2, 2)
    budget = T.TableBudget()
    cut = Counter()
    assert len(T.tables(segs, frags, 1, budget, cut)) == 1
    assert budget.remaining == T.MAX_DOCUMENT_CELLS - 4
    assert budget.nodes == limits.TABLE_NODES - 1
    budget.remaining = 3
    assert T.tables(segs, frags, 1, budget, cut) == [] and cut["cells"] == 1


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
