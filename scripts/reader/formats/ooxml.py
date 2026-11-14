"""OOXML entry (Word, PowerPoint and Excel packages) and the helpers the body parsers share.

The main part is found through the package relationships, never through [Content_Types].xml,
and elements match by local name, so Transitional and Strict files take the same path.
The shared helpers live in ooxml_base and are re-exported here; the body parsers import ooxml_base
and this module imports them only inside parse(), so there is no import cycle.
"""
from pathlib import Path

from ..containers import cfb
from ..containers.zipx import Package, local
from ..limits import ReadError
from .ooxml_base import UNDERSTOOD, alternate, chart, image, load, rels  # noqa: F401 (re-exported)


# ---- entry
def _check_cfb(path):
    """An OLE file is an encrypted package or not an OOXML package; anything else falls through.

    EncryptedPackage is looked for among every stream of the compound file.
    """
    with open(path, "rb") as f:
        if f.read(8) != cfb.MAGIC:
            return
    try:
        with cfb.Compound(path) as c:
            found = any(n.upper() == "ENCRYPTEDPACKAGE" for n in c.names())
    except ReadError:
        found = False
    if found:
        raise ReadError("encrypted")
    raise ReadError("damaged", "not an OOXML package (OLE file)")


def _main_part(pkg) -> str:
    for rel in rels(pkg, "").values():
        if rel.type.endswith("/officeDocument") and not rel.external:
            if pkg.has(rel.target):
                return rel.target
            break
    raise ReadError("damaged", "not an OOXML package")


def read_package(pkg, ctx):
    """The Doc of an open package, by the root of its main part: a Word document, a presentation or a workbook."""
    part = _main_part(pkg)
    root, _ = load(pkg, part)
    kind = local(root.tag)
    if kind == "document":
        from . import ooxml_word
        return ooxml_word.parse_word(pkg, part, ctx)
    if kind == "presentation":
        from . import ooxml_slides
        return ooxml_slides.parse_slides(pkg, part, ctx)
    if kind == "workbook":
        from . import ooxml_sheets
        return ooxml_sheets.parse_sheets(pkg, part, ctx)
    raise ReadError("damaged", "not a Word, PowerPoint or Excel package")


def parse_package(src, ctx):
    """Read the OOXML package `src` (a path or its bytes, nothing is written). `parse` ends in it."""
    with Package(src) as pkg:
        return read_package(pkg, ctx)


def parse(path, ctx):
    _check_cfb(Path(path))
    return parse_package(path, ctx)
