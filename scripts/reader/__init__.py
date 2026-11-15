"""Stdlib-only document reader. Heavier imports happen inside functions."""
import time
from pathlib import Path

VERSION = "0.6.0"

# when the reader was imported: the CLI child passes it to the pdf page loop's deadline
STARTED = time.monotonic()

# extension -> module name under reader.formats
FORMATS = {
    "hwpx": "hwpx", "hwp": "hwp",
    "odt": "odf", "ods": "odf", "odp": "odf",
    "docm": "ooxml", "pptm": "ooxml", "ppsx": "ooxml",
    "docx": "ooxml", "dotx": "ooxml", "dotm": "ooxml",
    "pptx": "ooxml", "potx": "ooxml", "potm": "ooxml", "ppsm": "ooxml",
    "xlsx": "ooxml", "xlsm": "ooxml", "xltx": "ooxml", "xltm": "ooxml",
    "vsdx": "vsdx", "epub": "epub", "xps": "xps", "oxps": "xps",
    "doc": "doc", "xls": "xls", "ppt": "ppt", "xlsb": "xlsb",
    "rtf": "rtf", "wpd": "wpd", "wp": "wpd", "wp5": "wpd", "wp6": "wpd", "wp7": "wpd", "wpt": "wpd",
    "mobi": "mobi", "azw3": "mobi",
    "pages": "iwork", "numbers": "iwork", "key": "iwork",
    "djvu": "djvu",
    "mdb": "jet", "accdb": "jet",
    "pub": "pub", "vsd": "vsd", "mpp": "mpp",
    "pdf": "pdf",
}

# extensions with a working parser; each format task adds its own
BUILT = {"hwpx", "hwp", "doc", "xls", "odt", "ods", "odp", "docm", "pptm", "ppsx", "vsdx", "epub", "xps", "oxps", "xlsb", "ppt", "rtf",
         "docx", "dotx", "dotm", "pptx", "potx", "potm", "ppsm", "xlsx", "xlsm", "xltx", "xltm", "pdf",
         "wpd", "wp", "wp5", "wp6", "wp7", "wpt", "mobi", "azw3", "pages", "numbers", "key"}


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
    return mod.parse(path, ctx, **opts) if opts else mod.parse(path, ctx)


def convert(src, out_dir, hidden=False, **opts) -> dict:
    """Convert src into out_dir/out.md, out_dir/assets/ and out_dir/meta.json."""
    import json
    import time
    from . import markdown
    from .limits import ReadError
    from .model import Context
    from .text import garble_ratio
    t0 = time.monotonic()
    src, out_dir = Path(src), Path(out_dir)
    ctx = Context()
    if ext_of(src) == "pdf":  # the page loop's deadline runs from the process start
        opts.setdefault("started", STARTED)
    doc = parse(src, ctx, **opts)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"
    info = {}
    md = markdown.render(doc, assets, hidden, ctx.skipped, info)
    if info["empty"] and info["lost"]:  # every content part failed: nothing was read
        raise ReadError("damaged", "nothing readable (could not read: " + ", ".join(info["lost"]) + ")")
    (out_dir / "out.md").write_text(md, encoding="utf-8", newline="\n")
    st = markdown.stats(md, info["pages"])
    warnings = list(ctx.warnings) + [w for w in doc.warnings if w not in ctx.warnings]
    if garble_ratio(md) > 0.05:
        warnings.append("garbled")
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
    }
    if doc.meta and "engine" in doc.meta:
        meta.update(engine=doc.meta["engine"], pages_total=doc.meta.get("pages_total"),
                    unrendered=doc.meta.get("unrendered"), figures=doc.meta.get("figures"), pages=st["pages"])
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta
