"""Which parser a compound file belongs to, by its top-level streams, so a file renamed between the compound-file
formats is read by its own parser whichever extension it came with.

Each of xls, ppt, doc and xlsb asks `route` when its own main stream is missing. An encrypted OOXML package
(EncryptedPackage) is `encrypted`; an Excel 5.0/95 workbook (Book) and a PowerPoint 95 presentation (PP40) are
`unsupported`; a Workbook, PowerPoint Document, WordDocument or hwp FileHeader stream names the parser that reads the
file, and the caller hands it on with `parse` once its own compound file is closed. Anything else keeps the caller's
damaged detail. Parsers are imported late, since they import this module.
"""
from ..limits import ReadError

EXCEL95 = "Excel 5.0/95 workbook"
PP95 = "PowerPoint 95 presentation"
_OWN = {"xls": "Workbook", "ppt": "PowerPoint Document", "doc": "WordDocument", "hwp": "FileHeader"}
# in this order: a stream naming a parser, or an old format's refusal
_STREAMS = (("Workbook", "xls"), ("Book", EXCEL95), ("PowerPoint Document", "ppt"), ("PP40", PP95),
            ("WordDocument", "doc"), ("FileHeader", "hwp"))


def route(cf, own: str, damaged: str) -> str | None:
    """The parser ("xls", "ppt", "doc" or "hwp") that reads the open compound file `cf`; None when that is `own`
    itself. Raises ReadError("encrypted") for an encrypted package, "unsupported" for an old format, and "damaged"
    with the detail `damaged` when no stream names a format."""
    if own in _OWN and cf.exists(_OWN[own]):
        return None
    if cf.exists("EncryptedPackage"):
        raise ReadError("encrypted")
    for stream, what in _STREAMS:
        if cf.exists(stream):
            if what in _OWN:
                return what
            raise ReadError("unsupported", what)
    raise ReadError("damaged", damaged)


def parse(fmt: str, path, ctx):
    """Read `path` with the parser `route` named."""
    from importlib import import_module
    return import_module(f"{__package__}.{fmt}").parse(path, ctx)
