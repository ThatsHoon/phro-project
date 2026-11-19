"""OCR words through the pdf layout and table builder (`reader.ocr_layout`), on hand-built pages: no engine.

Run: python -I -X utf8 tests/test_reader_ocr_layout.py
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import markdown, ocr_layout  # noqa: E402
from reader.model import Doc, Heading, Para, Table, Text  # noqa: E402
from reader.ocr import OcrPage, Result, Word  # noqa: E402

SCANNED = Para([Text("_Scanned page, text by OCR._")])


def page(words, w=1000, h=1000) -> OcrPage:
    return OcrPage(w, h, words, 0.0, 0, "win-ko", "v1")


def word(text, x, y, w, h, line) -> Word:
    return Word(text, x, y, w, h, line, None)


def render(blocks) -> str:
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(Doc(blocks=blocks), Path(d))


def two_columns(spanning: bool) -> OcrPage:
    """Four rows of two columns, given row by row: the left row's words, then the right row's. `spanning`: one engine
    line holds both halves of a row, as Tesseract's single-column mode gives them."""
    words = []
    for r in range(4):
        y = 100 + 40 * r
        left = 2 * r if not spanning else r
        right = 2 * r + 1 if not spanning else r
        words += [word(f"L{r}a", 50, y, 60, 20, left), word(f"L{r}b", 120, y, 60, 20, left),
                  word(f"R{r}a", 560, y, 60, 20, right), word(f"R{r}b", 630, y, 60, 20, right)]
    return page(words)


def test_two_columns_read_in_column_order():
    want = [f"L{r}a L{r}b" for r in range(4)] + [f"R{r}a R{r}b" for r in range(4)]
    for spanning in (False, True):
        got = ocr_layout.reading_lines(two_columns(spanning))
        assert got == want, (spanning, got)


def test_narrow_gutter_keeps_engine_column_order():
    """A gutter of 2.5 em, inside the middle band `xps._columns` asks to be clear: the positions show no two columns,
    so the engine's column-by-column order stands, as Windows OCR gives a two-column page."""
    words, want = [], []
    for side, x in (("L", 50), ("R", 540)):
        for r in range(4):
            texts = [f"{side}{r}{k}" + "x" * 11 for k in range(3)]
            words += [word(t, x + 150 * k, 100 + 40 * r, 140, 20, len(want)) for k, t in enumerate(texts)]
            want.append(" ".join(texts))
    assert ocr_layout.reading_lines(page(words)) == want, ocr_layout.reading_lines(page(words))


GRID_X = (100, 300, 500, 700)  # pixels at 144 dpi: 50, 150, 250 and 350 pt
GRID_Y = (100, 160, 220, 280)
ROWS = (("Apples", "3", "5"), ("Pears", "4", "6"), ("Plums", "7", "8"))


def grid_rules(stair: bool = False) -> list:
    """The table's rules as pixel boxes (exclusive ends), 2 px thick. `stair`: each horizontal rule is a skewed scan's
    staircase of 1-line pieces, rising 1 px every 50 px."""
    out = []
    for y in GRID_Y:
        if stair:
            out += [(x, y + k, x + 50, y + k + 1) for k, x in enumerate(range(GRID_X[0], GRID_X[-1] + 2, 50))]
        else:
            out.append((GRID_X[0], y, GRID_X[-1] + 2, y + 2))
    out += [(x, GRID_Y[0], x + 2, GRID_Y[-1] + 2) for x in GRID_X]
    return out


def table_page() -> OcrPage:
    """The 3x3 table's words column by column, one engine line per cell, as Windows OCR reads a ruled table."""
    words = []
    for c in range(3):
        for r in range(3):
            words.append(word(ROWS[r][c], GRID_X[c] + 20, GRID_Y[r] + 20, 20 * len(ROWS[r][c]), 24, len(words)))
    return page(words)


def test_table_given_column_by_column_comes_out_in_rows():
    res = Result("done", page=table_page(), rules=grid_rules())
    blocks = ocr_layout.blocks(res, 1, 144.0, Counter())
    assert blocks[0] == SCANNED, blocks
    tables = [b for b in blocks if isinstance(b, Table)]
    assert len(tables) == 1 and len(blocks) == 2, blocks
    rows = [ln for ln in render(blocks).splitlines() if ln.startswith("|") and "---" not in ln]
    assert rows == [f"|{a}|{b}|{c}|" for a, b, c in ROWS], rows


def test_segments_are_rule_centre_lines():
    segs = ocr_layout.segments([(100, 100, 702, 102), (100, 100, 102, 282)], table_page(), 144.0)
    got = [(s.x0, s.y0, s.x1, s.y1) for s in segs]
    assert got == [(50.0, 449.5, 351.0, 449.5), (50.5, 359.0, 50.5, 450.0)], got


def test_skewed_rules_degrade_to_text():
    res = Result("done", page=table_page(), rules=grid_rules(stair=True))
    skipped = Counter()
    md = render(ocr_layout.blocks(res, 1, 144.0, skipped))
    for row in ROWS:
        for text in row:
            assert text in md, (text, md)


def test_line_shares_baseline():
    frags = ocr_layout.fragments(page([word("gap", 100, 100, 60, 30, 0), word("top", 170, 100, 60, 22, 0)]), 144.0)
    assert len(frags) == 2
    a, b = frags
    assert a.y == b.y == (1000 - 126) / 2, (a.y, b.y)
    assert a.size == b.size == 13.0, (a.size, b.size)
    assert (a.x, a.width, a.space_width, a.order) == (50.0, 30.0, 3.25, 0), a
    assert (b.x, b.order) == (85.0, 1), b
    assert a.char_offsets == (0.0, 10.0, 20.0, 30.0), a.char_offsets


def body_page(big: bool) -> OcrPage:
    words = [word("Results", 100, 60, 280, 40 if big else 20, 0)]
    for r in range(5):
        words += [word("body", 100, 140 + 30 * r, 80, 20, r + 1), word("text", 190, 140 + 30 * r, 80, 20, r + 1)]
    return page(words)


def test_heading_from_larger_line():
    blocks = ocr_layout.blocks(Result("done", page=body_page(True)), 1, 72.0, Counter())
    assert blocks[1] == Heading(3, [Text("Results")]), blocks
    assert blocks[2:] == [Para([Text(" ".join(["body text"] * 5))])], blocks
    plain = ocr_layout.blocks(Result("done", page=body_page(False)), 1, 72.0, Counter())
    assert not any(isinstance(b, Heading) for b in plain), plain


def test_blocks_start_with_scanned_line():
    blocks = ocr_layout.blocks(Result("done", page=body_page(False)), 3, 72.0, Counter())
    assert blocks[0] == SCANNED, blocks
    assert render(blocks).startswith("_Scanned page, text by OCR._\n"), render(blocks)
    empty = ocr_layout.blocks(Result("done", page=page([])), 3, 72.0, Counter())
    assert empty == [SCANNED], empty


def test_reading_lines_for_picture():
    words = [word("bottom", 40, 200, 90, 20, 0), word("line", 140, 200, 60, 20, 0),
             word("top", 40, 100, 50, 20, 1), word("line", 100, 100, 60, 20, 1)]
    assert ocr_layout.reading_lines(page(words, 300, 300)) == ["top line", "bottom line"]
    assert ocr_layout.reading_lines(page([], 300, 300)) == []


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
