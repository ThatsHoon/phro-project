"""OCR words through the pdf layout and table builder: each word becomes a `pdf_layout.Fragment` and each rule a
`pdf_tables.Segment`, in points, y up, in the frame of the page's turned bitmap (`rotate(g, page.turn)`).

The words of one engine line share a baseline (the median box bottom) and a size: an estimate of the em, since a box holds only
its word's ink (`_em`). Tables take their words by position. Lines are read from positions in two clean columns, else
in the engine's order through `pdf_layout.reading_order` (`read`).
"""
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from itertools import groupby

from .formats import pdf_layout, pdf_tables
from .model import Para, Text
from .ocr import OcrPage, Result

SCANNED = "_Scanned page, text by OCR._"
BAND = 0.05  # a line within this share of the width from the middle crosses it, as in `xps._columns`
EM_SPREAD = 2.5  # a word box past this many times its line's median box is not the em (a word's box spans 0.5-1 em)


def _em(heights) -> int:
    """The tallest box of a line no taller than EM_SPREAD times its median box: a box with both an ascender and a
    descender spans about the em, and boxes grown by noise or reaching into the next line are passed over."""
    cap = EM_SPREAD * statistics.median(heights)
    return max(h for h in heights if h <= cap)


def fragments(page: OcrPage, dpi: float) -> list:
    """One Fragment per word, `order` its index, in a page of `page.w * 72 / dpi` by `page.h * 72 / dpi` points."""
    s = 72.0 / dpi
    by_line = defaultdict(list)
    for w in page.words:
        by_line[w.line].append(w)
    shared = {n: (statistics.median(w.y + w.h for w in ws), _em([w.h for w in ws])) for n, ws in by_line.items()}
    out = []
    for i, w in enumerate(page.words):
        bottom, height = shared[w.line]
        size, width, n = height * s, w.w * s, len(w.text)
        out.append(pdf_layout.Fragment(
            x=w.x * s, y=(page.h - bottom) * s, width=width, size=size, text=w.text, space_width=0.25 * size,
            order=i, char_offsets=tuple(width * k / n for k in range(n + 1)) if n else ()))
    return out


def segments(rules, page: OcrPage, dpi: float) -> list:
    """Each rule box (x0, y0, x1, y1), pixels, y down, exclusive ends, as its centre line in `fragments`' frame."""
    s = 72.0 / dpi
    out = []
    for x0, y0, x1, y1 in rules:
        if x1 - x0 >= y1 - y0:
            y = (page.h - (y0 + y1) / 2) * s
            out.append(pdf_tables.Segment(x0 * s, y, x1 * s, y))
        else:
            x = (x0 + x1) / 2 * s
            out.append(pdf_tables.Segment(x, (page.h - y1) * s, x, (page.h - y0) * s))
    return out


def _by_position(frags) -> tuple[list, list]:
    """The fragments in rows from the top, and the row of each. A row is cut where a gap passes `pdf_layout.GUTTER`
    sizes; its pieces go right to left, each piece's words left to right. So fed, `pdf_layout.lines` joins the words of
    a piece whatever their sizes or box overlaps, and refuses each piece the row on its left (`_Row.take_back`)."""
    rows, row, anchor, size = [], [], 0.0, 0.0
    for f in sorted(frags, key=lambda f: (-f.y, f.x)):
        if row and anchor - f.y > pdf_layout.LINE_TOLERANCE * max(f.size, size):
            rows.append(row)
            row = []
        if not row:
            anchor = f.y
        row.append(f)
        size = f.size
    if row:
        rows.append(row)
    ordered, row_of = [], []
    for r, row in enumerate(rows):
        pieces, end = [], 0.0
        for f in sorted(row, key=lambda f: f.x):
            if pieces and f.x - end <= pdf_layout.GUTTER * max(f.size, pieces[-1][-1].size):
                pieces[-1].append(f)
                end = max(end, f.x + f.width)
            else:
                pieces.append([f])
                end = f.x + f.width
        for piece in reversed(pieces):
            ordered += piece
            row_of += [r] * len(piece)
    return ordered, row_of


def _gutter(ordered, row_of, width):
    """The middle of the widest strip of the middle band, in whole points, that the words of at most a tenth of the
    rows cross; None when the strip is narrower than `pdf_layout.GUTTER` median sizes, so the rows are cut there into
    pieces (`_by_position`). A two-column page's gutter narrower than the band is such a strip; a page of one column,
    whose words cover the middle on most rows, has none."""
    lo, hi = width * (0.5 - BAND), width * (0.5 + BAND)
    n = math.ceil(hi - lo)
    if not ordered or n <= 0:
        return None
    count = [0] * n
    by_row = defaultdict(set)
    for f, r in zip(ordered, row_of):
        a, b = max(int(f.x - lo), 0), min(math.ceil(f.x + f.width - lo), n)
        by_row[r].update(range(a, b))
    for bins in by_row.values():
        for k in bins:
            count[k] += 1
    allowed = len(set(row_of)) // 10
    best, start = (0, 0), None
    for k, c in enumerate(count + [allowed + 1]):
        if c <= allowed:
            start = k if start is None else start
        elif start is not None:
            best, start = max(best, (k - start, start)), None
    if best[0] < pdf_layout.GUTTER * statistics.median(f.size for f in ordered):
        return None
    return lo + best[1] + best[0] / 2


def _columns(lns, mid, row_of):
    """The left lines, then the right, each by row and x, when the lines (none crossing the middle band or the gutter
    around `mid`) hold at least 3 rows a side, as `xps._columns` asks; else None."""
    if mid <= 0 or len(lns) < 6:
        return None
    left = [ln for ln in lns if ln.box[0] < mid]
    right = [ln for ln in lns if ln.box[0] >= mid]
    if min(len({row_of[ln.order] for ln in side}) for side in (left, right)) < 3:
        return None
    return [ln for side in (left, right) for ln in sorted(side, key=lambda ln: (row_of[ln.order], ln.box[0]))]


def read(frags, width, links=()) -> list:
    """The lines of `frags` in reading order; a character in a box of `links` [(box, url)] carries its url. The rows
    holding a line whose measured box reaches the middle band (a title, a full-width line, a narrow gutter) cut the
    page into bands; when the rows leave a gutter clear inside the band (`_gutter`), only the rows with a line over the
    gutter's middle do. A band clear of it with two clean columns reads its left column, then its right, a line that
    goes on along the one before it made one with it (`pdf_layout._whole`: the bands are cut on lines made row by row,
    so that each lies in one row). The rows
    between such bands keep the engine's order through `pdf_layout.reading_order`, taken over all of them at once."""
    ordered, row_of = _by_position(frags)
    numbered = [replace(f, order=k) for k, f in enumerate(ordered)]
    gap = pdf_layout.word_gap(sorted((f for f in numbered if f.text and not f.hidden), key=lambda f: f.order))
    placed = [ln for _row, fs in groupby(numbered, key=lambda f: row_of[f.order])
              for ln in pdf_layout.lines(list(fs), links, gap, whole=False)]
    by_row = defaultdict(list)
    for ln in placed:
        by_row[row_of[ln.order]].append(ln)
    mid = _gutter(ordered, row_of, width)
    lo, hi = (mid, mid) if mid is not None else (width * (0.5 - BAND), width * (0.5 + BAND))
    crossing = {r for r, lns in by_row.items() if any(ln.box[0] < hi and ln.box[2] > lo for ln in lns)}
    out, rest = [], []

    def engine():
        out.extend(pdf_layout.reading_order(pdf_layout.lines(rest, links), width))
        rest.clear()

    for cross, band in groupby(range(len(ordered)), key=lambda k: row_of[k] in crossing):
        band = list(band)
        split = None if cross else _columns([ln for r in sorted({row_of[k] for k in band}) for ln in by_row[r]],
                                            (lo + hi) / 2, row_of)
        if split is None:
            rest += [ordered[k] for k in band]
        else:
            engine()
            out += pdf_layout._whole(split)
    engine()
    return out


def reading_lines(page: OcrPage, dpi: float = 72.0) -> list[str]:
    """The text of each line, in reading order."""
    return [ln.text for ln in read(fragments(page, dpi), page.w * 72.0 / dpi)]


def blocks(res: Result, number: int, dpi: float, skipped: Counter, *,
           budget: pdf_tables.TableBudget | None = None) -> list:
    """A lone TIFF page as blocks: the scanned line, then the ruled tables placed by their tops among the headings and
    paragraphs of the other words, as pdf places a page's tables. `skipped` takes the table caps' cuts. `budget` is the
    document's table budget, held across its pages; a new one when None."""
    page = res.page
    model = pdf_layout.load_model()
    frags = fragments(page, dpi)
    found = pdf_tables.tables(segments(res.rules, page, dpi), frags, number,
                              budget if budget is not None else pdf_tables.TableBudget(), skipped, model=model)
    used = set().union(*(got.used for got in found))
    items = [(got.bbox[3], got.bbox[0], k, got.table) for k, got in enumerate(found)]
    lns = read([f for f in frags if f.order not in used], page.w * 72.0 / dpi)
    body = pdf_layout.body_size(lns)
    levels = {i: lvl for i, ln in enumerate(lns)
              if (lvl := pdf_layout.heading_level(ln.text, ln.size, body)) is not None}
    return [Para([Text(SCANNED)])] + pdf_layout.place(lns, items, levels, model)
