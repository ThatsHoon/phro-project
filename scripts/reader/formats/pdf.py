"""pdf to the document model: one "## Page N" section per page, with its headings, paragraphs,
tables, images, fields and comments.

The caps of the pdf reader live here; callers read them as `pdf.<NAME>` at call time, so a test
patches this module alone. `parse_bytes` opens the file with pdfnative (`NativeDoc`), or with
pdfium (`pdf_pdfium.PdfiumDoc`, falling back to native), and runs the page loop over either:
each page's `PageData` goes through the hidden-text rules, the char cap, rotation into
viewer space, lines, tables, images and renders (scanned pages, vector figures), with a
PDF_PAGE_SECONDS check between the steps. Paragraphs, headings and running headers are made after
the loop, over the lines every page kept.

Counts. A page that raises counts `page` (`_page_guard`); a step that raises, or a page that
runs out of time between steps, counts `page_part` once for that page; a page whose headings,
paragraphs or placement fail after the loop is left out and counts `page`. A render that fails
puts the page on `unrendered` like one with no render. A `timeout` ReadError always propagates.
The document totals of what pdfnative's caps dropped come from `NativeDoc.cut` alone, added once
at the end, except `bad_coord` (fragments with coordinates out of range): text was lost, so the
page counts `text`, once with the char cap.
"""
import math
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .. import images, limits
from ..limits import ReadError
from ..model import Doc, Heading, Image, Link, Para, Section, Text
from . import pdf_layout, pdf_tables

PDF_PAGE_OBJECTS = 100_000  # page objects (text, path, image, form) interpreted per page
PDF_PAGE_IMAGES = 64        # images kept per page
PDF_PAGES = 5_000           # pages read per conversion; the rest count page_cut
# seconds from the start for the page loop; the rest count page_cut. Measured on the 384-page spike file (id
# e4bc4dca33): native 11.1 s for all 384 pages, pdfium 9.2-9.6 s in the child; both finish under it
PDF_DEADLINE = 36.0
PDF_PAGE_SECONDS = 4.0      # seconds for one page, checked between its steps
PDF_PAGE_CHARS = 200_000    # chars on one page; past it the rest of its text counts `text`
# renders per conversion, scans and figures together. Measured (test_reader_pdf measure renders, 100 Letter
# scans of 1700x2200 gray Flate with 16-level paper grain, pypdfium2 5.15.2): 100 renders took 27.2-28.2 s, over
# the 10 s budget with zlib level 6 for the PNG (0.19 s of each 0.21 s render: pdfium 0.023, rows 0.002). Renders
# now compress at level 1: 100 took 6.1-6.4 s (white noise 6.9-7.0 s), so the cap stays 100. (Level 6 would have
# needed 30: 6.9-7.3 s; 40: 10.4-10.8 s.) A flat page costs 2.9 s for 100.
PDF_RENDERS = 100
RENDER_DPI, CROP_DPI, RENDER_LONG = 110, 150, 1_568
# pdfium: image pixels decoded per page; a page whose images total more has no render, and its
# images past the budget are not decoded (each counts `image`)
PDF_PAGE_DECODE_PIXELS = 4 * images._MAX_PIXELS
SCAN_COVER = 0.5            # no chars and one image covering this share of the page: a scan
OCR_COVER = 0.8             # invisible text over an image this big is an OCR layer, read as text
FIGURE_PATHS = 32           # non-rule paths that make a vector figure
FIGURE_COVER = 0.04         # share of the page the figure's paths must cover
FIGURE_PAD = 6.0            # points added around the figure's union box
FIGURE_WHOLE = 0.7          # a figure box over this share of the page renders the whole page
TINY_PT = 1.0               # text under this size is hidden ("tiny")
IMAGE_MIN_PX = 32           # images under this many pixels a side are left out, uncounted
IMAGE_MIN_PT = 16.0         # images drawn under this many points a side are left out, uncounted

ENGINES = ("native", "pdfium")  # pdfium only where pypdfium2 imports (the pdf venv)


# ---- geometry

def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _cover(b, page) -> float:
    """The share of `page` that box `b` covers."""
    whole = _area(page)
    if whole <= 0:
        return 0.0
    clip = (max(b[0], page[0]), max(b[1], page[1]), min(b[2], page[2]), min(b[3], page[3]))
    return _area(clip) / whole


def _outside(b, page) -> bool:
    return b[2] <= page[0] or b[0] >= page[2] or b[3] <= page[1] or b[1] >= page[3]


def _turn(rotate) -> int:
    turn = 0
    if isinstance(rotate, (int, float)) and not isinstance(rotate, bool) and math.isfinite(rotate):
        turn = int(round(rotate)) % 360
    return turn if turn in (90, 180, 270) else 0


def _view_point(x, y, turn, crop):
    """A page-space point in viewer space, as pdf_layout.rotate_into_view maps a fragment."""
    if not turn:
        return x, y
    x0, y0, x1, y1 = crop
    px, py = x - x0, y - y0
    if turn == 90:
        return py, (x1 - x0) - px
    if turn == 180:
        return (x1 - x0) - px, (y1 - y0) - py
    return (y1 - y0) - py, px


def _view_box(box, turn, crop):
    if not turn:
        return tuple(box)
    pts = [_view_point(x, y, turn, crop) for x in (box[0], box[2]) for y in (box[1], box[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _view_page(turn, crop):
    """The crop box in viewer space: its width and height swap on a quarter turn."""
    if not turn:
        return tuple(crop)
    w, h = crop[2] - crop[0], crop[3] - crop[1]
    return (0.0, 0.0, h, w) if turn in (90, 270) else (0.0, 0.0, w, h)


def _dpi(box, dpi) -> float:
    """`dpi`, lowered so the long side of `box` (points) renders at most RENDER_LONG pixels."""
    long_side = max(box[2] - box[0], box[3] - box[1])
    if long_side <= 0:
        return dpi
    return min(dpi, RENDER_LONG * 72.0 / long_side)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _inlines(ln):
    return [Link(u, [Text(t)]) if u else Text(t) for t, u in ln.runs if t]


# ---- one page

class _Kept:
    """What a page keeps for the pass after the loop."""

    def __init__(self, number, view):
        self.number = number
        self.view = view          # the crop box in viewer space
        self.lines = []
        self.items = []           # (top, x, seq, block): tables, images and renders
        self.hidden = []          # hidden Paras, one per text object
        self.comments = []
        self.blocks = []


class _Reader:
    def __init__(self, native, ctx, clock, started, engine):
        self.native, self.ctx, self.clock, self.started = native, ctx, clock, started
        self.engine = engine
        self.budget = pdf_tables.TableBudget()
        self.model = pdf_layout.load_model()
        self.renders = 0
        self.image_bytes = 0
        self.unrendered = []
        self.figures = []
        self.part = False   # page_part already counted for the page being read
        self.text_cut = False  # text already counted for the page being read

    # -- counting

    @contextmanager
    def _page_guard(self, kind):
        """Decision 5: any exception, a non-timeout ReadError included, counts under `kind`
        ("page" or "page_part") and the loop goes on; a timeout propagates."""
        try:
            yield
        except ReadError as e:
            if e.kind == "timeout":
                raise
            self._fail(kind)
        except Exception:
            self._fail(kind)

    def _fail(self, kind):
        if kind == "page_part":
            self._part()
        else:
            self.ctx.skipped[kind] += 1
        if self.ctx.skipped[kind] == 1:
            self.ctx.warnings.append(f"{kind}: skipped damaged part")

    def _part(self):
        if not self.part:
            self.part = True
            self.ctx.skipped["page_part"] += 1

    def _lost_text(self):
        """Text of the page being read was dropped (the char cap, or fragments whose
        coordinates were not finite): `text`, once for the page."""
        if not self.text_cut:
            self.text_cut = True
            self.ctx.skipped["text"] += 1

    def _late(self, t0) -> bool:
        if self.clock() - t0 > PDF_PAGE_SECONDS:
            self._part()
            return True
        return False

    # -- the loop

    def run(self, pages) -> Doc:
        native = self.native
        count = native.page_count
        if pages is None:
            indices = range(count)
        else:
            first, last = int(pages[0]), int(pages[1])
            if first > count:
                raise ReadError("damaged", f"pages {first}-{last} past the end ({count} pages)")
            indices = range(max(0, first - 1), min(count, last))
        kept, stopped = [], False
        for n, index in enumerate(indices):
            if n >= PDF_PAGES or self.clock() - self.started > PDF_DEADLINE:
                self.ctx.skipped["page_cut"] += len(indices) - n
                stopped = True
                break
            got = []
            with self._page_guard("page"):
                got.append(self._page(index))
            if got:
                kept.append(got[0])
        for kind, value in native.cut.items():
            if value > 0 and kind != "bad_coord":  # bad_coord is counted `text` per page
                self.ctx.skipped[kind] += value
        if native.xfa:
            self.ctx.skipped["xfa"] = 1
        if native.attachments:
            self.ctx.skipped["attachment"] = 1
        if not kept and not stopped:
            raise ReadError("damaged", "no readable pages")
        doc = Doc()
        doc.headers, doc.footers, kept = self._after(kept)
        if not kept and not stopped:
            raise ReadError("damaged", "no readable pages")
        doc.blocks = [Section("page", number=k.number, blocks=k.blocks, comments=k.comments)
                      for k in kept]
        doc.meta = {"engine": self.engine, "pages_total": count,
                    "unrendered": self.unrendered, "figures": self.figures}
        return doc

    def _page(self, index) -> _Kept:
        self.part = self.text_cut = False
        t0 = self.clock()
        pd = self.native.page(index)
        if pd.cut.get("page_part", 0) > 0:
            self.part = True  # pdfnative counted this page read in part already
        if pd.cut.get("bad_coord", 0) > 0:
            self._lost_text()
        turn = _turn(pd.rotate)
        view = _view_page(turn, pd.box)
        keep = _Kept(pd.number, view)
        keep.comments = ([Para([Text(f"Field {label}: {value}")]) for label, value in pd.fields]
                         + [Para([Text(f"Comment by {c.author}: {c.text}" if c.author
                                       else f"Comment: {c.text}")],
                                 hidden="hidden" if c.hidden else "") for c in pd.comments])
        scan = None
        if pd.chars == 0:
            covers = [(_cover(im.box, pd.box), i) for i, im in enumerate(pd.images)]
            best = max(covers, default=(0.0, -1))
            if best[0] >= SCAN_COVER:
                scan = pd.images[best[1]]
        state = {"frags": [], "ocr": set(), "links": []}
        steps = (lambda: self._text(pd, turn, keep, state),
                 lambda: self._tables(pd, turn, keep, state),
                 lambda: self._images(pd, turn, keep, state, scan),
                 lambda: self._renders(pd, turn, keep, scan))
        for k, step in enumerate(steps):
            if k and self._late(t0):
                break
            with self._page_guard("page_part"):
                step()
        return keep

    # -- steps

    def _text(self, pd, turn, keep, state):
        frags = sorted(pd.fragments, key=lambda f: f.order)
        frags, ocr = self._hide(frags, pd.box, pd.images)
        frags = self._chars(frags)
        view = pdf_layout.rotate_into_view(frags, turn, pd.box)
        state["frags"], state["ocr"] = view, ocr
        state["links"] = [(_view_box(b, turn, pd.box), url) for b, url in pd.links]
        self._layout(keep, state)
        groups = defaultdict(list)
        for f in view:
            if f.hidden:
                groups[f.obj].append(replace(f, hidden=""))
        for obj in sorted(groups):
            text = " ".join(ln.text for ln in pdf_layout.lines(groups[obj]))
            if text.strip():
                keep.hidden.append(Para([Text(text)], hidden="hidden"))

    def _layout(self, keep, state):
        visible = [f for f in state["frags"] if not f.hidden]
        lns = pdf_layout.lines(visible, state["links"])
        keep.lines = pdf_layout.reading_order(lns, keep.view[2] - keep.view[0])

    def _hide(self, frags, box, images):
        """The hidden rules, in page space: ocg as given, then offcrop, tiny and invisible.
        Invisible text whose centre lies on an image is read; an image of OCR_COVER or more
        under such text is an OCR layer's scan, left out uncounted."""
        out, ocr = [], set()
        for f in frags:
            kind = f.hidden
            if not kind:
                fb = pdf_layout.box_of([f])
                if _outside(fb, box):
                    kind = "offcrop"
                elif abs(f.size) < TINY_PT:
                    kind = "tiny"
                elif f.invisible:
                    cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
                    under = [i for i, im in enumerate(images)
                             if im.box[0] <= cx <= im.box[2] and im.box[1] <= cy <= im.box[3]]
                    if not under:
                        kind = "invisible"
                    ocr.update(i for i in under if _cover(images[i].box, box) >= OCR_COVER)
            out.append(replace(f, hidden=kind) if kind != f.hidden else f)
        return out, ocr

    def _chars(self, frags):
        total = 0
        for k, f in enumerate(frags):
            total += sum(1 for ch in f.text if not ch.isspace())
            if total > PDF_PAGE_CHARS:
                self._lost_text()
                return frags[:k]
        return frags

    def _tables(self, pd, turn, keep, state):
        def view(items):
            return [pdf_tables.Segment(*_view_box((s.x0, s.y0, s.x1, s.y1), turn, pd.box))
                    for s in items]

        found = pdf_tables.tables(view(pd.segments), state["frags"], pd.number, self.budget,
                                  self.ctx.skipped, short=view(pd.short), model=self.model)
        used = set()
        for k, got in enumerate(found):
            used |= set(got.used)
            keep.items.append((got.bbox[3], got.bbox[0], k, got.table))
        if used:
            state["frags"] = [f for f in state["frags"] if f.order not in used]
            self._layout(keep, state)

    def _images(self, pd, turn, keep, state, scan):
        shown = []
        for i, im in enumerate(pd.images):
            if im is scan or i in state["ocr"]:
                continue
            w, h = im.box[2] - im.box[0], im.box[3] - im.box[1]
            if min(im.px) < IMAGE_MIN_PX or min(w, h) < IMAGE_MIN_PT:
                continue
            if im.data is None or self.image_bytes + len(im.data) > limits.IMAGE_BYTES:
                self.ctx.skipped["image"] += 1
                continue
            self.image_bytes += len(im.data)
            vb = _view_box(im.box, turn, pd.box)
            shown.append((vb[3], vb[0], 1000 + i, Image(im.data)))
        keep.items.extend(shown)

    def _render(self, make):
        """Bytes of one render, or None past PDF_RENDERS or the image bytes cap, or when the
        render fails (the page then goes on `unrendered` like one with no render)."""
        if self.renders >= PDF_RENDERS:
            return None
        try:
            data = make()
        except ReadError as e:
            if e.kind == "timeout":
                raise
            return None
        except Exception:
            return None
        if not data or self.image_bytes + len(data) > limits.IMAGE_BYTES:
            return None
        self.renders += 1
        self.image_bytes += len(data)
        return data

    def _renders(self, pd, turn, keep, scan):
        if scan is not None:
            if pd.render is not None:
                data = self._render(lambda: pd.render(None, _dpi(pd.box, RENDER_DPI)))
            else:
                data = self._render(lambda: scan.data)  # native: the page's own image
            if data:
                keep.items.append((math.inf, 0.0, -1, Image(data, alt=f"Scanned page {pd.number}")))
            else:
                self.unrendered.append(pd.number)
                self.ctx.skipped["scan"] += 1
            return
        box, paths = pd.figure
        if box is None or paths < FIGURE_PATHS or _cover(box, pd.box) < FIGURE_COVER:
            return
        crop = pd.box
        grown = (max(crop[0], box[0] - FIGURE_PAD), max(crop[1], box[1] - FIGURE_PAD),
                 min(crop[2], box[2] + FIGURE_PAD), min(crop[3], box[3] + FIGURE_PAD))
        whole = _cover(grown, crop) > FIGURE_WHOLE or (pd.chars == 0 and not pd.images)
        target = None if whole else grown
        data = None
        if pd.render is not None:
            data = self._render(lambda: pd.render(target, _dpi(target or crop, CROP_DPI)))
        if not data:
            self.unrendered.append(pd.number)
            self.ctx.skipped["figure"] += 1
            return
        top = math.inf if whole else _view_box(grown, turn, crop)[3]
        keep.items.append((top, 0.0, -1, Image(data)))
        self.figures.append(pd.number)

    # -- after the loop

    def _after(self, kept):
        headers, footers, drops = pdf_layout.running([k.lines for k in kept], [k.view for k in kept])
        for k, drop in zip(kept, drops):
            if drop:
                k.lines = [ln for i, ln in enumerate(k.lines) if i not in drop]
        outline = defaultdict(list)
        try:
            for depth, title, page in self.native.outline():
                outline[page].append((depth, _norm(title)))
        except ReadError as e:
            if e.kind == "timeout":
                raise
        except Exception:
            pass  # no outline: the size rule alone makes headings
        body = pdf_layout.body_size([ln for k in kept for ln in k.lines])
        done = []
        for k in kept:
            # a page whose headings or paragraphs fail is lost alone (`page`), as in the loop
            with self._page_guard("page"):
                k.blocks = self._page_blocks(k, outline.get(k.number - 1, ()), body)
                done.append(k)
        return headers, footers, done

    def _page_blocks(self, k, titles, body):
        levels = {}
        if titles:
            at = defaultdict(list)  # normalized line text -> its line indexes, last first
            for i in range(len(k.lines) - 1, -1, -1):
                at[_norm(k.lines[i].text)].append(i)
            for depth, title in titles:
                found = at.get(title)
                if found:
                    levels[found.pop()] = pdf_layout.outline_level(depth)
        for i, ln in enumerate(k.lines):
            if i not in levels:
                level = pdf_layout.heading_level(ln.text, ln.size, body)
                if level is not None:
                    levels[i] = level
        return self._blocks(k, levels) + k.hidden

    def _blocks(self, k, levels):
        """Headings and paragraphs in line order, with each table, image and render placed
        before the first line whose top is below its own top."""
        items = sorted(k.items, key=lambda it: (-it[0], it[1], it[2]))
        out, run, n = [], [], 0

        def flush():
            out.extend(pdf_layout.paragraphs(run, self.model))
            run.clear()

        for i, ln in enumerate(k.lines):
            while n < len(items) and items[n][0] > ln.box[3]:
                flush()
                out.append(items[n][3])
                n += 1
            if i in levels:
                flush()
                out.append(Heading(levels[i], _inlines(ln)))
            else:
                run.append(ln)
        flush()
        out.extend(it[3] for it in items[n:])
        return out


def parse_bytes(data, ctx, *, engine="native", pages=None, clock=time.monotonic, started=None) -> Doc:
    """The pdf in `data` as a Doc. `pages=(A, B)` reads pages A to B alone (1-based, numbered as
    in the file). The deadline runs from `started`, which is `clock()` at entry unless given.

    engine="pdfium" needs pypdfium2 (the pdf venv); without it the engine is not available
    (ValueError). It opens the native structure first: its `encrypted` and `drm` errors (and a
    timeout) propagate, any other failure is kept. Then pdfium opens the bytes, with the native
    document for off-layer pages (decision 3). When pdfium cannot open them (no password at
    fault), the native document is read instead, or the kept error is raised when native failed
    too. Doc.meta["engine"] says which engine read the pages."""
    if started is None:
        started = clock()
    if engine not in ENGINES:
        raise ValueError(f"pdf engine not available: {engine}")
    from ..pdfnative.document import NativeDoc
    if engine == "native":
        return _Reader(NativeDoc(data), ctx, clock, started, "native").run(pages)
    try:
        from . import pdf_pdfium
    except ImportError:
        raise ValueError(f"pdf engine not available: {engine}") from None
    native, kept = None, None
    try:
        native = NativeDoc(data)
    except ReadError as e:
        if e.kind in ("encrypted", "drm", "timeout"):
            raise
        kept = e
    except Exception as e:  # noqa: BLE001 - the native parser failed; pdfium may still read it
        kept = ReadError("damaged", f"native parser: {type(e).__name__}")
    try:
        doc = pdf_pdfium.PdfiumDoc(data, native)
    except pdf_pdfium.PdfiumOpenError:
        if native is None:
            raise kept from None
        return _Reader(native, ctx, clock, started, "native").run(pages)
    try:
        return _Reader(doc, ctx, clock, started, "pdfium").run(pages)
    finally:
        doc.close()


def parse(path, ctx, *, engine="native", pages=None, clock=time.monotonic, started=None) -> Doc:
    return parse_bytes(Path(path).read_bytes(), ctx, engine=engine, pages=pages, clock=clock,
                       started=started)
