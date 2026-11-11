"""Stdlib-only document reader. Heavier imports happen inside functions."""
from pathlib import Path

VERSION = "0.1.0"

# extension -> module name under reader.formats
FORMATS = {
    "hwpx": "hwpx", "hwp": "hwp",
    "odt": "odf", "ods": "odf", "odp": "odf",
    "docm": "ooxml", "pptm": "ooxml", "ppsx": "ooxml",
    "vsdx": "vsdx", "epub": "epub", "xps": "xps", "oxps": "xps",
    "doc": "doc", "xls": "xls", "ppt": "ppt", "xlsb": "xlsb",
    "rtf": "rtf", "wpd": "wpd",
    "mobi": "mobi", "azw3": "mobi",
    "pages": "iwork", "numbers": "iwork", "key": "iwork",
    "djvu": "djvu",
    "mdb": "jet", "accdb": "jet",
    "pub": "pub", "vsd": "vsd", "mpp": "mpp",
}

# extensions with a working parser; each format task adds its own
BUILT = {"hwpx", "odt", "ods", "odp", "docm", "pptm", "ppsx", "vsdx", "epub", "xps", "oxps"}

# handled by anthropic-skills:*, never touched here
SKILL_EXTS = frozenset({"pdf", "docx", "pptx", "xlsx", "xlsm"})


def ext_of(path) -> str:
    return Path(path).suffix.lower().lstrip(".")


def precheck(path) -> None:
    """Cheap gate shared by parse and the CLI: extension and size."""
    from .limits import MAX_INPUT, ReadError
    path = Path(path)
    ext = ext_of(path)
    if ext not in BUILT:
        raise ReadError("unsupported", ext or "no extension")
    size = path.stat().st_size
    if size > MAX_INPUT:
        raise ReadError("too_large", f"{size} bytes")


def parse(path, ctx):
    """Dispatch to the format module for path's extension."""
    import importlib
    path = Path(path)
    precheck(path)
    mod = importlib.import_module("reader.formats." + FORMATS[ext_of(path)])
    return mod.parse(path, ctx)


def convert(src, out_dir, hidden=False) -> dict:
    """Convert src into out_dir/out.md, out_dir/assets/ and out_dir/meta.json."""
    import json
    import time
    from . import markdown
    from .model import Context
    from .text import garble_ratio
    t0 = time.monotonic()
    src, out_dir = Path(src), Path(out_dir)
    ctx = Context()
    doc = parse(src, ctx)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"
    md = markdown.render(doc, assets, hidden)
    (out_dir / "out.md").write_text(md, encoding="utf-8", newline="\n")
    st = markdown.stats(md)
    warnings = list(ctx.warnings)
    if garble_ratio(md) > 0.05:
        warnings.append("garbled")
    meta = {
        "source": str(src),
        "size": src.stat().st_size,
        "version": VERSION,
        "seconds": round(time.monotonic() - t0, 3),
        "lines": st["lines"],
        "images": sum(1 for p in assets.iterdir() if p.is_file()) if assets.is_dir() else 0,
        "hidden": st["hidden"],
        "headings": st["headings"],
        "warnings": warnings,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta
