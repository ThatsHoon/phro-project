"""OCR words through the pdf layout and table builder: each word becomes a `pdf_layout.Fragment` and each rule a
`pdf_tables.Segment`, in points, y up, in the frame of the page's turned bitmap (`rotate(g, page.turn)`).

The words of one engine line share a baseline (the median box bottom) and a size (the median box height). Tables take
their words by position. Lines follow the positions where these show two clean columns, else the engine's order
through `pdf_layout.reading_order` (`_read`).
"""
import statistics
from collections import Counter, defaultdict
from dataclasses import replace

from .formats import pdf_layout, pdf_tables, xps
from .model import Heading, Para, Text
from .ocr import OcrPage, Result

SCANNED = "_Scanned page, text by OCR._"


def fragments(page: OcrPage, dpi: float) -> list:
    """One Fragment per word, `order` its index, in a page of `page.w * 72 / dpi` by `page.h * 72 / dpi` points."""
    s = 72.0 / dpi
    by_line = defaultdict(list)
    for w in page.words:
        by_line[w.line].append(w)
    shared = {n: (statistics.median(w.y + w.h for w in ws), statistics.median(w.h for w in ws))
              for n, ws in by_line.items()}
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


def _by_position(frags) -> list:
    """The fragments renumbered in rows from the top, each row right to left. So ordered, `pdf_layout.lines` joins a
    word to its row only within the gutter rule of `_Row.take_back`: two columns across one baseline stay two lines.
    Left to right, it would join them whatever the gap."""
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
    ordered = [f for row in rows for f in sorted(row, key=lambda f: -f.x)]
    return [replace(f, order=k) for k, f in enumerate(ordered)]


def _read(frags, width) -> list:
    """The lines of `frags` in reading order. When their positions show two clean columns (`xps._columns`, the rule
    of `pdf_layout.reading_order`), the left column, then the right, each by (top, x). Otherwise
    `pdf_layout.reading_order` over the lines in the engine's order: on a gutter narrower than that rule's band, the
    engine's order is the only column order there is."""
    placed = pdf_layout.lines(_by_position(frags))
    runs = [(-ln.box[3], ln.box[0], ln.size, ln.text, i) for i, ln in enumerate(placed)]
    columns = xps._columns(runs, width)
    if len(columns) == 2:
        return [placed[r[4]] for column in columns for r in sorted(column, key=lambda r: (r[0], r[1]))]
    return pdf_layout.reading_order(pdf_layout.lines(frags), width)


def reading_lines(page: OcrPage, dpi: float = 72.0) -> list[str]:
    """The text of each line, in reading order."""
    return [ln.text for ln in _read(fragments(page, dpi), page.w * 72.0 / dpi)]


def blocks(res: Result, number: int, dpi: float, skipped: Counter) -> list:
    """A lone TIFF page as blocks: the scanned line, then the ruled tables placed by their tops among the headings and
    paragraphs of the other words, as pdf.py places a page's tables. `skipped` takes the table caps' cuts."""
    page = res.page
    model = pdf_layout.load_model()
    frags = fragments(page, dpi)
    found = pdf_tables.tables(segments(res.rules, page, dpi), frags, number, pdf_tables.TableBudget(), skipped,
                              model=model)
    used = set().union(*(got.used for got in found))
    items = sorted(((got.bbox[3], got.bbox[0], k, got.table) for k, got in enumerate(found)),
                   key=lambda it: (-it[0], it[1], it[2]))
    lns = _read([f for f in frags if f.order not in used], page.w * 72.0 / dpi)
    body = pdf_layout.body_size(lns)
    out, run, n = [Para([Text(SCANNED)])], [], 0

    def flush():
        out.extend(pdf_layout.paragraphs(run, model))
        run.clear()

    for ln in lns:
        while n < len(items) and items[n][0] > ln.box[3]:
            flush()
            out.append(items[n][3])
            n += 1
        level = pdf_layout.heading_level(ln.text, ln.size, body)
        if level is None:
            run.append(ln)
        else:
            flush()
            out.append(Heading(level, [Text(ln.text)]))
    flush()
    out.extend(it[3] for it in items[n:])
    return out
