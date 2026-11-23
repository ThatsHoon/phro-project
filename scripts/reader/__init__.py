"""Document reader: turns documents into compact Markdown. The optional third-party engines (pdf, image) live in
plugin-managed venvs and are imported only there; heavier imports happen inside functions."""
import time
from pathlib import Path

from .exts import FORMATS

VERSION = "0.10.0"

# when the reader was imported: the CLI child passes it to the pdf and djvu page loops' deadlines
STARTED = time.monotonic()


# extensions with a working parser; each format task adds its own
BUILT = {"hwpx", "hwp", "doc", "xls", "odt", "ods", "odp", "docm", "pptm", "ppsx", "vsdx", "epub", "xps", "oxps", "xlsb", "ppt", "rtf",
         "docx", "dotx", "dotm", "pptx", "potx", "potm", "ppsm", "xlsx", "xlsm", "xltx", "xltm", "pdf",
         "wpd", "wp", "wp5", "wp6", "wp7", "wpt", "mobi", "azw3", "pages", "numbers", "key",
         "djvu", "djv", "mdb", "accdb", "mde", "accde", "accdr", "pub", "vsd", "vst", "vsdm", "vstx", "mpp", "mpt",
         "wmf", "emf", "wmz", "emz", "tif", "tiff", "pct", "pict", "wpg"}


def ext_of(path) -> str:
    return Path(path).suffix.lower().lstrip(".")


def precheck(path) -> None:
    """Cheap gate shared by parse and the CLI: extension and size."""
    from .limits import BIG_INPUT, MAX_INPUT, ReadError
    path = Path(path)
    ext = ext_of(path)
    if ext not in BUILT:
        raise ReadError("unsupported", ext or "no extension")
    size = path.stat().st_size
    if size > MAX_INPUT:
        raise ReadError("too_large", f"{BIG_INPUT}: {size} bytes")


def parse(path, ctx, **opts):
    """Dispatch to the format module for path's extension; opts (engine, pages, ...) go to its parse when given."""
    import importlib
    path = Path(path)
    precheck(path)
    ext = ext_of(path)
    mod = importlib.import_module("reader.formats." + FORMATS[ext])
    doc = mod.parse(path, ctx, **opts) if opts else mod.parse(path, ctx)
    if ctx.vba:
        doc.meta["vba"] = ctx.vba
    doc.notes.update(ctx.notes)
    return doc


def _end(default: float) -> float:
    """The time.monotonic() time PHRO_READER_DEADLINE names (a wall-clock time), `default` with none set."""
    import math
    import os
    import time
    try:
        wall = float(os.environ.get("PHRO_READER_DEADLINE", ""))
    except ValueError:
        return default
    return time.monotonic() + (wall - time.time()) if math.isfinite(wall) else default


def convert(src, out_dir, hidden=False, backend=None, **opts) -> dict:
    """Convert src into out_dir/out.md, out_dir/assets/ and out_dir/meta.json.

    With an OCR engine here (`backend`, the (id, version) the caller keyed the read with, else
    `ocr_probe.backend()`), an OCR session (`ocr.opened`) is open while a pdf or a picture file is parsed, and
    while a document holding a raster picture OCR may read is rendered; no other read loads the OCR modules. Its
    results are cached per picture under the cache root; its deadline is PHRO_READER_DEADLINE's, none without it.
    meta.json then holds the OCR counts ("ocr"), and OCR_LATE among the warnings when the time left pages or
    pictures unread. The convert ends at PHRO_READER_DEADLINE (CONVERT_SECONDS after the process start without it):
    a format reads no further part past READ_SECONDS in CONVERT_SECONDS of that time (`Context.late`) and the render
    writes no further block past RENDER_SECONDS in CONVERT_SECONDS of it. The output then holds what was read and says so, with READ_LATE
    among the warnings and the seconds the read was given as meta "read_budget"."""
    import contextlib
    import json
    import time
    from . import markdown
    from .limits import CONVERT_SECONDS, OCR_LATE, READ_LATE, READ_SECONDS, RENDER_SECONDS, ReadError
    from .model import Context
    from . import text
    t0 = time.monotonic()
    src, out_dir = Path(src), Path(out_dir)
    ctx = Context()
    ctx.name = src.name
    span = _end(STARTED + CONVERT_SECONDS) - STARTED
    ctx.stop = STARTED + span * READ_SECONDS / CONVERT_SECONDS
    if FORMATS.get(ext_of(src)) in ("pdf", "djvu"):  # the page loop's deadline runs from the process start
        opts.setdefault("started", STARTED)
    if backend is None:
        from . import ocr_probe
        backend = ocr_probe.backend()

    def session():
        from . import cache, ocr, raster
        wall = raster._env_deadline()
        deadline = None if wall is None else time.monotonic() + (wall - time.time())
        return stack.enter_context(ocr.opened(deadline, backend, store=cache.OcrResults(cache.root() / cache.OCR)))

    info = {}
    with contextlib.ExitStack() as stack:
        eager = backend[0] and FORMATS.get(ext_of(src)) in ("pdf", "picture")
        if eager:
            session()
        try:
            doc = parse(src, ctx, **opts)
        except ReadError as e:
            if e.kind == "damaged" and ctx.skipped["time"]:
                raise ReadError("timeout", "") from None
            raise
        out_dir.mkdir(parents=True, exist_ok=True)
        md = markdown.render(doc, out_dir / "assets", hidden, ctx.skipped, info,
                             ocr=session if backend[0] and not eager else None,
                             stop=STARTED + span * RENDER_SECONDS / CONVERT_SECONDS)
    if info["empty"] and info["lost"]:  # every content part failed: nothing was read
        raise ReadError("damaged", "nothing readable (could not read: " + ", ".join(info["lost"]) + ")")
    (out_dir / "out.md").write_text(md, encoding="utf-8", newline="\n")
    st = markdown.stats(md, info["pages"])
    warnings = list(ctx.warnings) + [w for w in doc.warnings if w not in ctx.warnings]
    if "garbled" not in warnings and text.garble_ratio(md) > text.GARBLED:  # a parser may have found it (pub 97)
        warnings.append("garbled")
    warnings += [w for w in info["warnings"] if w not in warnings]  # the figure warnings, each once
    if ctx.skipped["time"] or info["late"]:
        warnings.append(READ_LATE)
    counts = info["ocr"]
    if counts and (counts["stopped_at"] is not None or counts["pictures_left"]) and OCR_LATE not in warnings:
        warnings.append(OCR_LATE)
    meta = {
        "source": str(src),
        "size": src.stat().st_size,
        "version": VERSION,
        "seconds": round(time.monotonic() - t0, 3),
        "lines": st["lines"],
        "images": info["images"],  # viewable images out.md links to, not every file in assets/
        "hidden": st["hidden"],
        "headings": st["headings"],
        "unread": info["unread"],
        "warnings": warnings,
        "figures": info["figures"],  # metafile, PICT, WPG and TIFF pictures written as PNG
        "figures_left": info["figures_left"],  # those kept as the original file
    }
    if READ_LATE in warnings:
        meta["read_budget"] = round(ctx.stop - STARTED)  # seconds from the process start the read was given
    if doc.meta.get("vba"):
        meta["vba"] = len(doc.meta["vba"])  # VBA modules whose source is under assets/vba/
    if doc.meta and "engine" in doc.meta:
        meta.update(engine=doc.meta["engine"], pages_total=doc.meta.get("pages_total"),
                    unrendered=doc.meta.get("unrendered"), figures=doc.meta.get("figures"), pages=st["pages"])
    if counts is not None:
        meta["ocr"] = counts
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta
