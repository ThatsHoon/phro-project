"""OCR page analysis on synthetic Gray8 pages built in memory: Otsu's threshold, ruled-table rules, unread ink and the
picture-area box, and the time `rules` plus `unread` take on an A4 page at 200 dpi.

Run: python -I -X utf8 tests/test_reader_ocr_pixels.py
"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, ocr_pixels  # noqa: E402
from reader.ocr_pixels import Gray  # noqa: E402
from ocr_fix import DIR, load  # noqa: E402

INK = 30


def blank(w, h, v=255):
    return bytearray([v]) * (w * h)


def fill(buf, w, x0, y0, x1, y1, v=INK):
    """Paint the box x0..x1, y0..y1 (exclusive ends) with value v."""
    run = bytes([v]) * (x1 - x0)
    for y in range(y0, y1):
        buf[y * w + x0:y * w + x1] = run


def near(a, b, tol=1):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def horizontal(boxes):
    return [b for b in boxes if b[2] - b[0] > b[3] - b[1]]


def vertical(boxes):
    return [b for b in boxes if b[3] - b[1] > b[2] - b[0]]


ROWS_Y = (100, 200, 300, 400, 500)    # 4 rows of cells: 5 horizontal rules
COLS_X = (100, 300, 500, 700)         # 3 columns of cells: 4 vertical rules


def grid_page(w=800, h=600):
    """A 4 x 3 ruled grid with 2 px lines, and a 30 x 10 text block in each cell; returns (Gray, word boxes)."""
    buf = blank(w, h)
    for y in ROWS_Y:
        fill(buf, w, COLS_X[0], y, COLS_X[-1] + 2, y + 2)
    for x in COLS_X:
        fill(buf, w, x, ROWS_Y[0], x + 2, ROWS_Y[-1] + 2)
    words = []
    for y in ROWS_Y[:-1]:
        for x in COLS_X[:-1]:
            fill(buf, w, x + 20, y + 40, x + 50, y + 50)
            words.append((x + 20, y + 40, 30, 10))
    return Gray(w, h, bytes(buf)), words


def test_threshold_splits_ink_and_paper():
    w = h = 100
    buf = blank(w, h, 230)
    fill(buf, w, 0, 0, w, 20, 30)  # 20 % of the pixels
    t = ocr_pixels.threshold(Gray(w, h, bytes(buf)))
    assert 60 < t < 200, t


def test_threshold_paper_noise_has_no_ink():
    """Paper with noise 248-255 and no ink: Otsu's two class means are under 40 levels apart, so nothing is dark."""
    w, h = 400, 300
    rnd = random.Random(11)
    g = Gray(w, h, bytes(rnd.randrange(248, 256) for _ in range(w * h)))
    t = ocr_pixels.threshold(g)
    assert t == 0, t
    u = ocr_pixels.unread(g, [], ocr_pixels.rules(g, 200, t), t)
    assert u.share == 0.0 and u.box is None, u


def test_grid_rules_found():
    g, _ = grid_page()
    thr = ocr_pixels.threshold(g)
    boxes = ocr_pixels.rules(g, 200, thr)
    hs, vs = sorted(horizontal(boxes), key=lambda b: b[1]), sorted(vertical(boxes))
    assert len(hs) == 5 and len(vs) == 4, boxes
    for b, y in zip(hs, ROWS_Y):
        assert near(b, (COLS_X[0], y, COLS_X[-1] + 2, y + 2)), (b, y)
    for b, x in zip(vs, COLS_X):
        assert near(b, (x, ROWS_Y[0], x + 2, ROWS_Y[-1] + 2)), (b, x)


def test_thick_bar_not_rule():
    w, h = 800, 600
    buf = blank(w, h)
    fill(buf, w, 100, 300, 700, 310)
    g = Gray(w, h, bytes(buf))
    assert ocr_pixels.rules(g, 200, ocr_pixels.threshold(g)) == []


def test_short_run_not_rule():
    w = h = 1000
    buf = blank(w, h)
    fill(buf, w, 100, 500, 100 + int(0.04 * w), 501)
    g = Gray(w, h, bytes(buf))
    assert ocr_pixels.rules(g, 200, 128) == []
    fill(buf, w, 300, 800, 300 + int(0.06 * w), 801)  # the positive control: 6 % of the width is a rule
    assert ocr_pixels.rules(Gray(w, h, bytes(buf)), 200, 128) == [(300, 800, 360, 801)]


def test_noise_row_stops_at_2000_runs():
    """With the rule length lowered to 2 px, a row of 2100 three-pixel runs is scanned for its first 2000 only."""
    w, h = 4 * 2100, 3
    buf = blank(w, h)
    buf[w:2 * w] = (b"\x00\x00\x00\xff" * 2100)
    g = Gray(w, h, bytes(buf))
    saved = limits.OCR_RULE, limits.OCR_RULES_MAX
    limits.OCR_RULE = 0.0002  # ceil(0.0002 * 8400) == 2 along the rows, 1 down the columns
    limits.OCR_RULES_MAX = 10**6
    try:
        # at 100 dpi a rule is at most 2 px thick: the 3-column groups are bars, not vertical rules
        boxes = ocr_pixels.rules(g, 100, 128)
    finally:
        limits.OCR_RULE, limits.OCR_RULES_MAX = saved
    assert len(boxes) == 2000, len(boxes)
    assert all(b[1] == 1 and b[3] == 2 and b[2] - b[0] == 3 for b in boxes), boxes[:3]


def test_too_many_rules_is_texture():
    """More rules than OCR_RULES_MAX after the guards: the page is texture and gives none."""
    w, h = 4 * 2100, 3
    buf = blank(w, h)
    buf[w:2 * w] = (b"\x00\x00\x00\xff" * 2100)
    g = Gray(w, h, bytes(buf))
    saved = limits.OCR_RULE, limits.OCR_RULES_MAX
    limits.OCR_RULE, limits.OCR_RULES_MAX = 0.0002, 1999
    try:
        assert ocr_pixels.rules(g, 100, 128) == []
        limits.OCR_RULES_MAX = 2000
        assert len(ocr_pixels.rules(g, 100, 128)) == 2000
    finally:
        limits.OCR_RULE, limits.OCR_RULES_MAX = saved


def test_rules_dpi_clamped():
    """A non-finite dpi is read as OCR_DPI; a huge one as 1200, where a 40 px bar is still no rule."""
    g, _ = grid_page()
    assert ocr_pixels.rules(g, float("nan"), 128) == ocr_pixels.rules(g, limits.OCR_DPI, 128)
    assert ocr_pixels.rules(g, float("inf"), 128) == ocr_pixels.rules(g, limits.OCR_DPI, 128)
    w, h = 800, 600
    buf = blank(w, h)
    fill(buf, w, 100, 300, 700, 340)
    bar = Gray(w, h, bytes(buf))
    assert ocr_pixels.rules(bar, 1e9, 128) == []
    assert ocr_pixels.rules(bar, 1e9, 128) == ocr_pixels.rules(bar, 1200, 128)


def test_rule_thickness_from_limits():
    """A 4 px rule is a rule at 150 dpi (1/40 inch is 4 px there); a 5 px one is not; the bound is read at call
    time."""
    w, h = 800, 600
    buf = blank(w, h)
    fill(buf, w, 100, 100, 700, 104)
    fill(buf, w, 100, 300, 700, 305)
    g = Gray(w, h, bytes(buf))
    assert ocr_pixels.rules(g, 150, 128) == [(100, 100, 700, 104)]
    saved = limits.OCR_RULE_THICK
    limits.OCR_RULE_THICK = 1 / 25  # 6 px at 150 dpi
    try:
        assert len(ocr_pixels.rules(g, 150, 128)) == 2
    finally:
        limits.OCR_RULE_THICK = saved


def speckled_photo(w=1400, h=1400, box=(200, 200, 1200, 1200), seed=7):
    """A dark rectangle with 0-7 light specks a row at seeded places."""
    x0, y0, x1, y1 = box
    buf = blank(w, h)
    rnd = random.Random(seed)
    for y in range(y0, y1):
        buf[y * w + x0:y * w + x1] = bytes([40]) * (x1 - x0)
        for _ in range(rnd.randrange(0, 8)):
            buf[y * w + rnd.randrange(x0, x1)] = 255
    return Gray(w, h, bytes(buf))


def holds(u, box, w, h):
    x0, y0, x1, y1 = box
    return (u.box is not None and u.box[0] <= x0 / w and u.box[1] <= y0 / h
            and u.box[2] >= x1 / w and u.box[3] >= y1 / h)


def test_speckled_photo_stays_unread():
    box = (200, 200, 1200, 1200)
    g = speckled_photo(box=box)
    thr = ocr_pixels.threshold(g)
    rb = ocr_pixels.rules(g, 200, thr)
    u = ocr_pixels.unread(g, [], rb, thr)
    assert u.share > limits.OCR_UNREAD, (u, len(rb))
    assert holds(u, box, g.w, g.h), u


def test_striped_photo_gives_no_rules():
    """A dark rectangle with a light row every 4th row (banding, halftone) gives no rule inside it."""
    w = h = 1400
    x0, y0, x1, y1 = box = (200, 200, 1200, 1200)
    buf = blank(w, h)
    for y in range(y0, y1):
        if (y - y0) % 4 != 3:
            buf[y * w + x0:y * w + x1] = bytes([40]) * (x1 - x0)
    g = Gray(w, h, bytes(buf))
    thr = ocr_pixels.threshold(g)
    for dpi in (150, 200, 300):
        rb = ocr_pixels.rules(g, dpi, thr)
        inside = [b for b in rb if b[0] < x1 and b[2] > x0 and b[1] < y1 and b[3] > y0]
        assert inside == [], (dpi, len(inside), inside[:3])
        u = ocr_pixels.unread(g, [], rb, thr)
        assert holds(u, box, w, h), (dpi, u)


def test_fixture_table_rules_and_unread():
    """The spike's ruled table at 150 dpi: 8 horizontal and 6 vertical rules, and with the spike's words no ink is left
    unread."""
    name = "mix-table-malgun.150dpi"
    g = load(name)
    thr = ocr_pixels.threshold(g)
    rb = ocr_pixels.rules(g, 150, thr)
    hs, vs = horizontal(rb), vertical(rb)
    assert len(hs) == 8 and len(vs) == 6, rb
    spike = json.loads((DIR / f"{name}.win.json").read_text(encoding="utf-8"))
    words = [(wd["x"], wd["y"], wd["w"], wd["h"]) for line in spike["lines"] for wd in line["words"]]
    u = ocr_pixels.unread(g, words, rb, thr)
    assert u.share == 0.0 and u.box is None, u


def test_unread_zero_when_words_cover_ink():
    w, h = 400, 300
    buf = blank(w, h)
    words = []
    for i in range(5):
        fill(buf, w, 20 + 70 * i, 50, 70 + 70 * i, 70)
        words.append((20 + 70 * i, 50, 50, 20))
    u = ocr_pixels.unread(Gray(w, h, bytes(buf)), words, [], 128)
    assert u.share == 0.0 and u.box is None and u.area == 0.0, u


def test_unread_empty_page():
    u = ocr_pixels.unread(Gray(50, 40, bytes(blank(50, 40))), [], [], 128)
    assert u == ocr_pixels.Unread(0.0, None, 0.0), u


def test_unread_counts_ink_outside_words():
    """Half the ink outside the (grown) word boxes gives a share of one half."""
    w, h = 400, 300
    buf = blank(w, h)
    fill(buf, w, 20, 50, 70, 70)      # 1000 px, inside the word box
    fill(buf, w, 200, 200, 250, 220)  # 1000 px, far from any word
    u = ocr_pixels.unread(Gray(w, h, bytes(buf)), [(20, 50, 50, 20)], [], 128)
    assert abs(u.share - 0.5) < 1e-9, u
    assert u.box is not None and u.box[0] <= 200 / w and u.box[2] >= 250 / w, u


def test_word_box_grows_by_a_quarter_of_its_height():
    """Ink 4 px beyond a 20 px tall word box is still read (the box grows by 5 px); 7 px beyond is not."""
    w, h = 400, 300
    buf = blank(w, h)
    fill(buf, w, 100, 100, 150, 120)
    fill(buf, w, 154, 105, 155, 115)  # 4 px right of the box
    fill(buf, w, 157, 105, 158, 115)  # 7 px right of the box
    u = ocr_pixels.unread(Gray(w, h, bytes(buf)), [(100, 100, 50, 20)], [], 128)
    assert abs(u.share - 10 / 1020) < 1e-9, u


def test_rule_pixels_not_unread():
    g, words = grid_page()
    thr = ocr_pixels.threshold(g)
    u = ocr_pixels.unread(g, words, ocr_pixels.rules(g, 200, thr), thr)
    assert u.share < 0.01, u
    plain = ocr_pixels.unread(g, words, [], thr)
    assert plain.share > 0.5, plain  # without the rules the grid itself would be unread


def test_picture_area_box_spares_text_rows():
    w, h = 1000, 1400
    buf = blank(w, h)
    fill(buf, w, 200, 100, 800, 600, 40)  # the photo
    words = []
    for line in range(8):
        y = 800 + 50 * line
        for i in range(10):
            x = 100 + 80 * i
            fill(buf, w, x, y, x + 60, y + 20)
            words.append((x, y, 60, 20))
    fill(buf, w, 50, 1300, 52, 1302)  # a stray speck the 1 % rule drops
    g = Gray(w, h, bytes(buf))
    thr = ocr_pixels.threshold(g)
    u = ocr_pixels.unread(g, words, ocr_pixels.rules(g, 200, thr), thr)
    assert u.box is not None, u
    x0, y0, x1, y1 = u.box
    assert x0 <= 200 / w and x1 >= 800 / w and y0 <= 100 / h and y1 >= 600 / h, u
    assert y1 < 800 / h and x0 > 50 / w, u
    assert abs(u.area - (x1 - x0) * (y1 - y0)) < 1e-12 and 0.2 < u.area < 0.25, u


def word_page(w=1000, h=1400):
    """Lines of word blocks over the top and bottom of a white page; returns (buffer, word boxes)."""
    buf = blank(w, h)
    words = []
    for y in list(range(80, 420, 40)) + list(range(1000, 1340, 40)):
        for i in range(10):
            x = 100 + 80 * i
            fill(buf, w, x, y, x + 60, y + 20)
            words.append((x, y, 60, 20))
    return buf, words


def test_light_block_on_word_page_gives_area():
    """I9: a pastel picture lighter than the Otsu split is no unread ink, but it is not paper either: its box is the
    picture area, dense, and the page an area."""
    from reader import ocr
    w, h = 1000, 1400
    buf, words = word_page(w, h)
    x0, y0, x1, y1 = 200, 520, 800, 900
    for y in range(y0, y1):
        buf[y * w + x0:y * w + x1] = bytes(160 + (x * 40) // (x1 - x0) for x in range(x1 - x0))
    g = Gray(w, h, bytes(buf))
    thr = ocr_pixels.threshold(g)
    assert thr <= 160, thr
    u = ocr_pixels.unread(g, words, ocr_pixels.rules(g, 200, thr), thr)
    assert u.share == 0.0, u
    assert u.box is not None and holds(u, (x0, y0, x1, y1), w, h), u
    assert u.density > 0.5, u
    page = ocr.OcrPage(w, h, [ocr.Word("가나", *b, 0, None) for b in words], 0.0, 0, "win-ko", "v1")
    v = ocr.judge(page, u)
    assert v.kind == "area" and v.box == u.box, v


def test_density_tells_a_block_from_scattered_specks():
    """I3: specks spread over a large box leave it sparse; a block of the same box is dense."""
    w, h = 1000, 1400
    buf, words = word_page(w, h)
    rnd = random.Random(7)
    for _ in range(40):
        x, y = rnd.randrange(100, 900), rnd.randrange(450, 950)
        fill(buf, w, x, y, x + 6, y + 6)
    g = Gray(w, h, bytes(buf))
    u = ocr_pixels.unread(g, words, [], 128)
    assert u.box is not None and u.area > 0.1 and u.density < limits.OCR_AREA_DENSITY, u
    buf, words = word_page(w, h)
    fill(buf, w, 300, 600, 700, 800)
    u = ocr_pixels.unread(Gray(w, h, bytes(buf)), words, [], 128)
    assert u.box is not None and u.density > 0.5, u


def test_spread_flat_noisy_and_faint():
    """M3: the gray range a page with no words is judged on. A flat or lightly noisy page is under OCR_FLAT; faint
    writing, which the Otsu split calls no ink, is over it."""
    w, h = 600, 800
    flat = Gray(w, h, bytes(blank(w, h, 240)))
    assert ocr_pixels.unread(flat, [], [], ocr_pixels.threshold(flat)).spread == 0
    rnd = random.Random(3)
    noisy = Gray(w, h, bytes(240 + rnd.randrange(-4, 5) for _ in range(w * h)))
    u = ocr_pixels.unread(noisy, [], [], ocr_pixels.threshold(noisy))
    assert u.spread <= limits.OCR_FLAT and u.share == 0.0, u
    buf = blank(w, h, 245)
    for y in range(100, 700, 30):
        fill(buf, w, 80, y, 520, y + 8, 215)
    faint = Gray(w, h, bytes(buf))
    assert ocr_pixels.threshold(faint) == 0
    u = ocr_pixels.unread(faint, [], [], 0)
    assert u.share == 0.0 and u.spread > limits.OCR_FLAT, u


def a4_page():
    """An A4 page at 200 dpi: lines of word blocks and a ruled table; returns (Gray, word boxes)."""
    w, h = 1654, 2339
    buf = blank(w, h)
    words = []
    for line in range(30):
        y = 120 + 40 * line
        for i in range(19):
            x = 120 + 75 * i
            fill(buf, w, x, y, x + 60, y + 20)
            words.append((x, y, 60, 20))
    top, left, right = 1400, 120, 1530
    for r in range(9):
        fill(buf, w, left, top + 90 * r, right, top + 90 * r + 2)
    for c in range(6):
        x = left + 282 * c
        fill(buf, w, x, top, x + 2, top + 90 * 8 + 2)
    for r in range(8):
        for c in range(5):
            x, y = left + 282 * c + 30, top + 90 * r + 35
            fill(buf, w, x, y, x + 120, y + 20)
            words.append((x, y, 120, 20))
    rnd = random.Random(5)  # a speckled photo no word covers, below the table
    for y in range(PHOTO[1], PHOTO[3]):
        buf[y * w + PHOTO[0]:y * w + PHOTO[2]] = bytes([40]) * (PHOTO[2] - PHOTO[0])
        for _ in range(rnd.randrange(0, 8)):
            buf[y * w + rnd.randrange(PHOTO[0], PHOTO[2])] = 255
    return Gray(w, h, bytes(buf)), words


PHOTO = (400, 2140, 1200, 2320)


def test_speed_a4_200dpi():
    """1654 is not a multiple of 4, so the photo's box runs `unread`'s padded grid."""
    g, words = a4_page()
    t0 = time.perf_counter()
    thr = ocr_pixels.threshold(g)
    t1 = time.perf_counter()
    boxes = ocr_pixels.rules(g, 200, thr)
    t2 = time.perf_counter()
    u = ocr_pixels.unread(g, words, boxes, thr)
    t3 = time.perf_counter()
    secs = t3 - t1
    print(f"A4 200 dpi: threshold {t1 - t0:.3f}s, rules {t2 - t1:.3f}s ({len(boxes)} found), "
          f"unread {t3 - t2:.3f}s (share {u.share:.4f}); rules + unread {secs:.3f}s")
    assert len(horizontal(boxes)) == 9 and len(vertical(boxes)) == 6, boxes
    assert u.share > 0.1, u
    assert holds(u, PHOTO, g.w, g.h) and u.box[1] > 2122 / g.h, u  # the box starts below the table
    assert secs < 1.0, secs


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
