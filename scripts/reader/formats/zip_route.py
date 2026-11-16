"""Which parser a zip belongs to, so a package renamed between the zip formats is read by its own parser whichever
extension it came with.

A .doc, .rtf or .ppt file whose head is a zip is an OpenDocument package or an OOXML one. It is OpenDocument when
its `mimetype` member is `application/vnd.oasis.opendocument.*`, or when it has a `content.xml` and no
`[Content_Types].xml`; anything else goes to `ooxml`, which refuses what is no Word or PowerPoint package. A .xls
zip holding `xl/workbook.bin` goes to `xlsb`; any other goes through `parse` here, so an OpenDocument sheet is read by
`odf` and the rest by `ooxml`. A Visio package (a root relationship of type `.../document`, as `vsdx` finds its main
part) goes to `vsdx`, so a vsdx saved as .vsd is read too. Neither parser comes back to the caller, so the routing
cannot loop. Parsers are imported late.
"""
from ..containers.zipx import Package
from ..limits import ReadError

_MIME = "application/vnd.oasis.opendocument."
_MIME_MAX = 128  # bytes; a longer `mimetype` member is no ODF mimetype, and is not inflated to find that out


def route(path) -> str:
    """"odf", "vsdx" or "ooxml" for the zip at `path` (a ReadError for a file that is no zip)."""
    with Package(path) as pkg:
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
        try:
            root = pkg.rels("")
        except ReadError as e:
            if e.kind != "damaged":
                raise
            root = {}
        if any(r.type.endswith("/document") and not r.external for r in root.values()):
            return "vsdx"
    return "ooxml"


def parse(path, ctx):
    """Read the zip at `path` with the parser `route` named."""
    from importlib import import_module
    return import_module(f"{__package__}.{route(path)}").parse(path, ctx)
