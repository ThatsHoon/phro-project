"""Which parser a zip belongs to, so a package renamed between the zip formats is read by its own parser whichever
extension it came with.

A Word-family file (.doc, .rtf) whose head is a zip is an OpenDocument package or an OOXML one. It is OpenDocument when
its `mimetype` member is `application/vnd.oasis.opendocument.*`, or when it has a `content.xml` and no
`[Content_Types].xml`; anything else goes to `ooxml`, which refuses what is no Word or PowerPoint package. Neither
parser comes back to the caller, so the routing cannot loop. Parsers are imported late.
"""
from ..containers.zipx import Package
from ..limits import ReadError

_MIME = "application/vnd.oasis.opendocument."


def route(path) -> str:
    """"odf" or "ooxml" for the zip at `path` (a ReadError for a file that is no zip)."""
    with Package(path) as pkg:
        if pkg.has("mimetype"):
            try:
                mime = pkg.read("mimetype")[:128].decode("utf-8", "replace").strip()
            except ReadError as e:
                if e.kind != "damaged":
                    raise  # an encrypted ODF stays encrypted
                mime = ""
            if mime.startswith(_MIME):
                return "odf"
        if pkg.has("content.xml") and not pkg.has("[Content_Types].xml"):
            return "odf"
    return "ooxml"


def parse(path, ctx):
    """Read the zip at `path` with the parser `route` named."""
    from importlib import import_module
    return import_module(f"{__package__}.{route(path)}").parse(path, ctx)
