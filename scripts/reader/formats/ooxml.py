"""OOXML entry (Word and PowerPoint packages) and the helpers both body parsers share.

The main part is found through the package relationships, never through [Content_Types].xml,
and elements match by local name, so Transitional and Strict files take the same path.
The shared helpers live in ooxml_base and are re-exported here; the body parsers import ooxml_base
and this module imports them only inside parse(), so there is no import cycle.
"""
import struct
from pathlib import Path

from ..containers.zipx import Package, local
from ..limits import ReadError
from .ooxml_base import UNDERSTOOD, alternate, chart, image, load, rels  # noqa: F401 (re-exported)

_CFB_MAGIC = bytes.fromhex("d0cf11e0")
_ENCRYPTED = "EncryptedPackage".encode("utf-16-le")
_CFB_SCAN = 64 * 1024


# ---- entry
def _check_cfb(path):
    """An OLE file is an encrypted package or not an OOXML package; anything else falls through.

    EncryptedPackage is looked for in the first 64 KiB and in the first directory sector the header names.
    """
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != _CFB_MAGIC:
            return
        head += f.read(_CFB_SCAN - 8)
        found = _ENCRYPTED in head
        if not found and len(head) >= 0x34:
            shift = struct.unpack_from("<H", head, 0x1E)[0]
            sid = struct.unpack_from("<I", head, 0x30)[0]
            if 7 <= shift <= 16 and sid < 0xFFFFFFFA:
                f.seek((sid + 1) << shift)
                found = _ENCRYPTED in f.read(1 << shift)
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


def parse(path, ctx):
    _check_cfb(Path(path))
    with Package(path) as pkg:
        part = _main_part(pkg)
        root, _ = load(pkg, part)
        kind = local(root.tag)
        if kind == "document":
            from . import ooxml_word
            return ooxml_word.parse_word(pkg, part, ctx)
        if kind == "presentation":
            # Task 12: from . import ooxml_slides; return ooxml_slides.parse_slides(pkg, part, ctx)
            raise ReadError("unsupported", "pptm/ppsx arrive in Task 12")
        raise ReadError("damaged", "not a Word or PowerPoint package")
