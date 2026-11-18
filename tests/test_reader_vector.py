"""The drawing model, figure text, arcs, budget and the Symbol table.

Run: python -I -X utf8 tests/test_reader_vector.py
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, text  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.vector import Budget, Drawing, Run, arc_beziers, text_lines  # noqa: E402


def draw(*runs):
    return Drawing((0, 0, 1000, 1000), 96.0, list(runs))


def run(t, x, y, h=10, **kw):
    return Run(t, x, y, "Arial", h, **kw)


def test_runs_on_one_baseline_join():
    assert text_lines(draw(run("Q1", 0, 100), run("120", 30, 100)))[0] == ["Q1 120"]


def test_gap_rule():
    assert text_lines(draw(run("a", 0, 100), run("b", 10, 100)))[0] == ["ab"]
    assert text_lines(draw(run("a", 0, 100), run("b", 20, 100)))[0] == ["a b"]


def test_lines_top_to_bottom():
    assert text_lines(draw(run("low", 0, 200), run("high", 0, 100)))[0] == ["high", "low"]


def test_baseline_tolerance():
    assert len(text_lines(draw(run("a", 0, 100), run("b", 50, 104)))[0]) == 1
    assert len(text_lines(draw(run("a", 0, 100), run("b", 50, 106)))[0]) == 2


def test_rotated_run_own_line():
    lines, _ = text_lines(draw(run("a", 0, 100), run("side", 5, 150, angle=90.0), run("b", 50, 100),
                               run("c", 0, 200)))
    assert lines == ["a b", "side", "c"]


def test_overprint_dropped():
    assert text_lines(draw(run("bold", 0, 100), run("bold", 1, 101)))[0] == ["bold"]
    assert text_lines(draw(run("bold", 0, 100), run("bold", 0, 120)))[0] == ["bold", "bold"]


def test_cut_at_figure_chars():
    old = limits.FIGURE_CHARS
    limits.FIGURE_CHARS = 10
    try:
        lines, cut = text_lines(draw(run("abcdefgh", 0, 100), run("ijklmnop", 0, 200)))
        assert cut and sum(map(len, lines)) <= 11 and lines[-1].endswith("…")
        lines, cut = text_lines(draw(run("abc", 0, 100)))
        assert lines == ["abc"] and not cut
    finally:
        limits.FIGURE_CHARS = old


def test_glyph_run_resolved_or_dropped():
    g = Run("", 0, 100, "Arial", 10, glyphs=(1, 2))
    assert text_lines(draw(g), resolve=lambda r: "x")[0] == ["x"]
    assert text_lines(draw(g), resolve=lambda r: None)[0] == []
    assert text_lines(draw(g))[0] == []


def test_size_px():
    assert Drawing((0, 0, 1440, 720), 1440, []).size_px() == (96, 48)
    assert Drawing((0, 0, 0, 0), 96, []).size_px() == (1, 1)


def test_arc_beziers_quadrants():
    segs = arc_beziers(10, 20, 5, 3, 30, 360)
    assert len(segs) == 4 and all(s[0] == "C" for s in segs)
    sx, sy = 10 + 5 * math.cos(math.radians(30)), 20 + 3 * math.sin(math.radians(30))
    assert abs(segs[-1][5] - sx) < 1e-6 and abs(segs[-1][6] - sy) < 1e-6
    assert len(arc_beziers(0, 0, 1, 1, 0, 45)) == 1


def test_budget_records():
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 3
    try:
        b = Budget()
        for _ in range(3):
            b.tick()
        try:
            b.tick()
        except ReadError as e:
            assert e.kind == "too_large"
        else:
            raise AssertionError("no error")
    finally:
        limits.METAFILE_RECORDS = old
    b = Budget(records=10, points=5)
    b.points(5)
    try:
        b.points(1)
    except ReadError as e:
        assert e.kind == "too_large"
    else:
        raise AssertionError("no error")


def test_symbol_table():
    assert text.SYMBOL[0x61] == "α" and text.SYMBOL[0x44] == "Δ"
    assert text.SYMBOL[0xA5] == "∞" and text.SYMBOL[0xB4] == "×"
    assert text.SYMBOL[0x20] == " " and 0xFE in text.SYMBOL


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
