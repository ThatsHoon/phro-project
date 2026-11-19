"""Lone picture files: WMF, EMF, PICT, WPG and TIFF, and `wmz` and `emz` (gzip-compressed WMF and EMF).

Each picture is one Image block, so the output is its PNG link and its figure text (markdown and raster do the rest);
a TIFF gives one block a page, up to TIFF_PAGES. A file over IMAGE_BYTES is too_large, as embedded pictures are capped.
A `wmz` or `emz` is inflated within IMAGE_BYTES and the zip ratio rule (ZIP_RATIO times the input once past
ZIP_RATIO_FLOOR). Bytes that are not the format the extension names are
`damaged`.
"""
import time
from dataclasses import replace
from pathlib import Path

from .. import images, limits, ocr
from ..containers import flate
from ..limits import ReadError
from ..model import Doc, Image

KIND = {"wmf": "wmf", "wmz": "wmf", "emf": "emf", "emz": "emf", "tif": "tiff", "tiff": "tiff",
        "pct": "pict", "pict": "pict", "wpg": "wpg"}  # extension -> the kind images.sniff names
GZIPPED = frozenset({"wmz", "emz"})
_PICT_HEAD = 512
_PICT_VERSIONS = (b"\x11\x01", b"\x00\x11\x02\xff")  # PICT 1 and PICT 2, right after picSize and picFrame


def _pict(data: bytes) -> bytes:
    """A PICT with the zero 512-byte head images.sniff looks for: a file whose head holds an application's bytes gets
    zeros there, and one saved without the head gets it."""
    if images.sniff(data) == "pict":
        return data
    if data[_PICT_HEAD + 10:].startswith(_PICT_VERSIONS):
        return bytes(_PICT_HEAD) + data[_PICT_HEAD:]
    if data[10:].startswith(_PICT_VERSIONS):
        return bytes(_PICT_HEAD) + data
    return data


def unpack(data: bytes, ext: str) -> bytes:
    """The picture bytes of a file with extension `ext` (lower case, no dot): inflated for wmz and emz, a PICT given
    its head. ReadError("too_large", "part: …") past the inflate caps; ReadError("damaged", "not a <kind> file") when
    the bytes are not that kind."""
    kind = KIND[ext]
    if ext in GZIPPED:
        try:
            data = flate.gunzip(data, flate.Budget(), limits.IMAGE_BYTES)  # IMAGE_BYTES read at call time
        except ValueError:  # a corrupt stream, or flate.Truncated (a cut one)
            raise ReadError("damaged", f"not a {kind} file") from None
    if kind == "pict":
        data = _pict(data)
    if images.sniff(data) != kind:
        raise ReadError("damaged", f"not a {kind} file")
    return data


def parse(path, ctx) -> Doc:
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    if path.stat().st_size > limits.IMAGE_BYTES:  # the cap embedded pictures share, checked before reading
        raise ReadError("too_large", "picture past IMAGE_BYTES")
    data = unpack(path.read_bytes(), ext)
    if KIND[ext] != "tiff":
        return Doc(blocks=[Image(data)])
    from .. import raster
    cap = limits.TIFF_PAGES
    n = raster.tiff_pages(data, cap + 1)  # one past the cap tells a cut file from one of exactly TIFF_PAGES pages
    if n == 0:
        raise ReadError("damaged", "not a tiff file")
    warnings = [f"TIFF pages cut at {cap}"] if n > cap else []
    n = min(n, cap)
    blocks = [Image(data, f"page {i + 1}" if n > 1 else "", page=i) for i in range(n)]
    doc = Doc(blocks=blocks, warnings=warnings)
    s = ocr.session()
    if s is not None:
        _ocr(doc, s, data, ctx)
    return doc


def _ocr(doc: Doc, s, data: bytes, ctx) -> None:
    """A lone TIFF's pages read by OCR (decisions 1 and 6 of the OCR plan). Each page's pixels (raster.gray) go to
    the session in turn; then each page read gives its text (`ocr_layout.blocks`, one table budget for the file): a
    doubtful page adds its image after the text, a picture area a gray crop of the pixels OCR read (the whole page
    past pdf.FIGURE_WHOLE). A page not queued by FIGURE_RESERVE before the session's stop goes late at once, and no
    later page is sent. A page OCR could not read (failed, late, or not sent for the time) stays as today's image; one
    whose pixels do not decode, too, counted as failed. A layout thrown away leaves no skip counts. doc.meta["ocr"]
    holds the counts, and OCR_LATE joins the warnings when a page went late. Every page image is marked `ocr="page"`, so no picture pass reads it again."""
    from .. import raster
    from . import pdf, pdf_tables
    stop = s.deadline - limits.FIGURE_RESERVE
    until = stop - limits.FIGURE_RESERVE
    pages = [replace(b, ocr="page") for b in doc.blocks]
    sent, late = [], False  # per page: its Ticket, "late" (not sent for the time), "small" (under OCR_MIN_EDGE: not sent, not counted) or None (no pixels)
    for i in range(len(pages)):
        got = None if late else raster.gray(data, page=i, deadline=stop)
        if got is None:
            late = late or time.monotonic() >= stop
            sent.append("late" if late else None)
            continue
        if max(got[0].w, got[0].h) < limits.OCR_MIN_EDGE:
            sent.append("small")
            continue
        t = s.submit(got[0], "page", got[1], until=until)
        r = s.peek(t)
        late = r is not None and r.status == "late"
        sent.append(t)
    counts = {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": None}
    budget, out = pdf_tables.TableBudget(), []
    for i, (im, t) in enumerate(zip(pages, sent)):
        if t == "small":
            out.append(im)
            continue
        r = s.wait(t) if isinstance(t, ocr.Ticket) else None
        status = (t or "failed") if r is None else r.status
        if status == "done":
            got = _read(data, i, im, r, t.dpi, ctx.skipped, budget, stop, pdf.FIGURE_WHOLE)
            if got is not None:
                out += got
                counts["pages"] += 1
                counts["doubtful"] += r.verdict.kind == "doubtful"
                continue
            status = "failed"
        if status == "failed":
            counts["failed"] += 1
        elif status == "late" and counts["stopped_at"] is None:
            counts["stopped_at"] = i + 1
        out.append(im)
    doc.blocks = out
    doc.meta["ocr"] = counts
    if counts["stopped_at"] is not None:
        doc.warnings.append(limits.OCR_LATE)


def _read(data, i, im, r, dpi, skipped, budget, stop, whole) -> list | None:
    """The blocks of page `i` OCR read: its text, then its image (`im`) when doubtful, or its picture area; None when
    laying them out fails, which leaves the page as today's image and `skipped` as it was."""
    from collections import Counter
    from .. import ocr_layout
    own = Counter()
    try:
        got = ocr_layout.blocks(r, i + 1, dpi, own, budget=budget)
        if r.verdict.kind == "doubtful":
            got.append(im)
        elif r.verdict.kind == "area":
            got.append(_area(data, i, r, stop, whole) or replace(im, alt=f"Picture on page {i + 1}"))
        skipped.update(own)
        return got
    except ReadError as e:
        if e.kind == "timeout":
            raise
    except Exception:  # noqa: BLE001 - OCR never makes a document fail
        pass
    return None


def _area(data: bytes, i: int, r, stop: float, whole: float) -> Image | None:
    """The picture area of page `i`: a gray PNG cut from the pixels as OCR read them (turned as it read them), at
    most FIGURE_EDGE_TEXT on the long side (decision 6); None past `whole` of the page, or when the pixels no longer
    decode."""
    from .. import ocr_pixels, raster
    if r.unread.area > whole:
        return None
    got = raster.gray(data, page=i, deadline=stop)
    if got is None:
        return None
    crop = ocr_pixels.crop_png(ocr_pixels.rotate(got[0], r.page.turn), r.verdict.box, limits.FIGURE_EDGE_TEXT)
    return Image(crop, f"Picture on page {i + 1}", ocr="page")
