"""Which parser a zip belongs to, so a package renamed between the zip formats is read by its own parser whichever
extension it came with.

A .doc, .rtf or .ppt file whose head is a zip is an OpenDocument package or an OOXML one. It is OpenDocument when
its `mimetype` member is `application/vnd.oasis.opendocument.*`, or when it has a `content.xml` and no
`[Content_Types].xml`; anything else goes to `ooxml`, which refuses what is no Word or PowerPoint package. A .xls
zip holding `xl/workbook.bin` goes to `xlsb`; any other goes through `parse` here, so an OpenDocument sheet is read by
`odf` and the rest by `ooxml`. A Visio package goes to `vsdx`, so a vsdx saved as .vsd is read too: one with an
internal root relationship of Visio's own type (`_VISIO`) whose target it holds, and no `/officeDocument` root
relationship. A Word, PowerPoint or Excel package keeps its own body: with a stray, dangling or Visio-typed
relationship beside its `/officeDocument` one it still goes to `ooxml`. Neither parser comes back to the caller, so
the routing cannot loop. Parsers are imported late.
"""
from ..containers.zipx import Package
from ..limits import ReadError

_MIME = "application/vnd.oasis.opendocument."
_MIME_MAX = 128  # bytes; a longer `mimetype` member is no ODF mimetype, and is not inflated to find that out
_VISIO = "http://schemas.microsoft.com/visio/2010/relationships/document"


def route(path) -> str:
    """"odf", "vsdx" or "ooxml" for the zip at `path` (a ReadError for a file that is no zip)."""
    with Package(path) as pkg:
        return kind(pkg)


def kind(pkg) -> str:
    """"odf", "vsdx" or "ooxml" for the open package `pkg`; its root relationships stay in ooxml_base's cache for
    the parser that reads it next."""
    from .ooxml_base import rels
    if pkg.has("mimetype") and pkg.size("mimetype") <= _MIME_MAX:
        try:
            mime = pkg.read("mimetype")[:_MIME_MAX].decode("utf-8", "replace").strip()
        except ReadError as e:
            if e.kind != "damaged":
                raise  # an encrypted ODF stays encrypted
            mime = ""
        if mime.startswith(_MIME):
            return "odf"
    if pkg.has("content.xml") and not pkg.has("[Content_Types].xml"):
        return "odf"
    root = [r for r in rels(pkg, "").values() if not r.external]
    if not any(r.type.endswith("/officeDocument") for r in root) and any(
            r.type == _VISIO and pkg.has(r.target) for r in root):
        return "vsdx"
    return "ooxml"


def parse(path, ctx):
    """Read the zip at `path` with the parser `route` named."""
    from importlib import import_module
    return import_module(f"{__package__}.{route(path)}").parse(path, ctx)
