"""OCR words through the pdf layout and table builder (`reader.ocr_layout`), on hand-built pages: no engine.

Run: python -I -X utf8 tests/test_reader_ocr_layout.py
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import limits, markdown, ocr_layout  # noqa: E402
from reader.formats.pdf_tables import TableBudget  # noqa: E402
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


def test_title_over_columns_given_in_rows():
    """Tesseract psm 4: one engine line across both columns, under a full-width title that crosses the middle band."""
    p = two_columns(True)
    title = [word("Overview", 400, 40, 100, 20, 9), word("section", 510, 40, 100, 20, 9)]
    got = ocr_layout.reading_lines(page(title + p.words))
    want = ["Overview section"] + [f"L{r}a L{r}b" for r in range(4)] + [f"R{r}a R{r}b" for r in range(4)]
    assert got == want, got


def test_row_split_by_engine_or_overlap_stays_one_line():
    """In two clean columns, a right-column row of two engine lines with other box heights, and a row whose second box
    overlaps the first by 0.2 em, each stay one line."""
    p = two_columns(False)
    words = []
    for w in p.words:
        if w.text == "R0b":
            w = word("R0b", w.x, w.y - 6, w.w, 26, 99)
        elif w.text == "R1b":
            w = word("R1b", 616, w.y, w.w, w.h, w.line)
        words.append(w)
    want = [f"L{r}a L{r}b" for r in range(4)] + ["R0a R0b", "R1aR1b", "R2a R2b", "R3a R3b"]
    got = ocr_layout.reading_lines(page(words))
    assert got == want, got


def test_column_test_uses_measured_boxes():
    """Wide capitals reach across the middle, though their character count says they stop short of it: the page has no
    two columns and reads top down."""
    words, want = [], []
    for r in range(6):
        y = 100 + 40 * r
        if r % 2 == 0:
            texts = [f"W{r}" + "W" * 6, f"M{r}" + "M" * 6]
            words += [word(texts[0], 50, y, 380, 20, r), word(texts[1], 450, y, 380, 20, r)]
        else:
            texts = [f"r{r}a", f"r{r}b"]
            words += [word(texts[0], 600, y, 60, 20, r), word(texts[1], 670, y, 60, 20, r)]
        want.append(" ".join(texts))
    got = ocr_layout.reading_lines(page(words))
    assert got == want, got


def test_narrow_gutter_keeps_engine_column_order():
    """A gutter of 2.5 em, inside the middle band that must be clear: the positions show no two columns, so the engine's
    column-by-column order stands, as Windows OCR gives a two-column page. A short row of the left column alone, clear
    of the band, does not cut the page there."""
    words, want = [], []
    for side, x, rows in (("L", 50, range(5)), ("R", 540, (0, 1, 3, 4))):
        for r in rows:
            texts = [f"{side}{r}{k}" + "x" * 11 for k in range(1 if (side, r) == ("L", 2) else 3)]
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


def test_paragraphs_above_and_below_a_table():
    words = [word("Fruit", GRID_X[0], 40, 120, 48, 50)] + table_page().words
    words += [word("Prices", GRID_X[0], 300, 120, 24, 51), word("are", 230, 300, 60, 24, 51),
              word("per", GRID_X[0], 330, 60, 24, 52), word("kilo.", 170, 330, 100, 24, 52)]
    blocks = ocr_layout.blocks(Result("done", page=page(words), rules=grid_rules()), 1, 144.0, Counter())
    assert [type(b).__name__ for b in blocks] == ["Para", "Heading", "Table", "Para"], blocks
    assert blocks[1].inlines == [Text("Fruit")] and blocks[3] == Para([Text("Prices are per kilo.")]), blocks


def test_blocks_share_a_table_budget():
    budget = TableBudget()
    ocr_layout.blocks(Result("done", page=table_page(), rules=grid_rules()), 1, 144.0, Counter(), budget=budget)
    assert budget.nodes < limits.TABLE_NODES and budget.remaining < TableBudget().remaining, budget
    skipped = Counter()
    blocks = ocr_layout.blocks(Result("done", page=table_page(), rules=grid_rules()), 2, 144.0, skipped,
                               budget=TableBudget(nodes=0))
    assert not any(isinstance(b, Table) for b in blocks) and skipped["cells"] >= 1, (blocks, skipped)


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
    assert a.size == b.size == 15.0, (a.size, b.size)
    assert (a.x, a.width, a.space_width, a.order) == (50.0, 30.0, 3.75, 0), a
    assert (b.x, b.order) == (85.0, 1), b
    assert a.char_offsets == (0.0, 10.0, 20.0, 30.0), a.char_offsets


def body_page(big: bool) -> OcrPage:
    words = [word("Results", 100, 60, 280, 40 if big else 20, 0)]
    for r in range(5):
        words += [word("body", 100, 140 + 24 * r, 80, 20, r + 1), word("text", 190, 140 + 24 * r, 80, 20, r + 1)]
    return page(words)


def test_heading_from_larger_line():
    blocks = ocr_layout.blocks(Result("done", page=body_page(True)), 1, 72.0, Counter())
    assert blocks[1] == Heading(3, [Text("Results")]), blocks
    assert blocks[2:] == [Para([Text(" ".join(["body text"] * 5))])], blocks
    plain = ocr_layout.blocks(Result("done", page=body_page(False)), 1, 72.0, Counter())
    assert not any(isinstance(b, Heading) for b in plain), plain


LATIN = (24, 18, 14, 18, 19, 24, 19, 19, 19, 19, 19)  # one spike line's Windows OCR box heights, 200 dpi, em 25 px


def spike_line(words, r, heights, base, texts=None):
    """One engine line at baseline `base` (px): a 24 px or taller box reaches 5 px below it, as a descender does."""
    for k, h in enumerate(heights):
        bottom = base + (5 if h >= 24 else 0)
        words.append(word(texts[k] if texts else f"w{r}{k}", 100 + 75 * k, bottom - h, 60, h, r))


def test_single_spaced_latin_is_one_paragraph():
    words = []
    for r in range(4):
        spike_line(words, r, LATIN, 200 + 30 * r)
    blocks = ocr_layout.blocks(Result("done", page=page(words, 1653, 2338)), 1, 200.0, Counter())
    assert len(blocks) == 2 and isinstance(blocks[1], Para), blocks


def test_short_descender_line_is_no_heading():
    words, body = [], (22, 29, 15, 22, 29, 22, 22, 15)  # Times 11 pt at 200 dpi: em 30.6 px
    for r in range(4):
        spike_line(words, r, body, 200 + 37 * r)
    spike_line(words, 4, (29, 29), 200 + 37 * 4, ["Shipping", "by"])
    blocks = ocr_layout.blocks(Result("done", page=page(words, 1653, 2338)), 1, 200.0, Counter())
    assert not any(isinstance(b, Heading) for b in blocks), blocks
    assert len(blocks) == 2 and blocks[1].inlines[0].text.endswith("Shipping by"), blocks


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
