"""Which parser a compound file belongs to, by its top-level streams, so a file renamed between the compound-file
formats is read by its own parser whichever extension it came with.

Each of xls, ppt, doc, xlsb, wpd, vsd, pub and mpp asks `route` when its own main stream is missing. An encrypted OOXML
package (EncryptedPackage) is `encrypted`; a PowerPoint 95 presentation (PP40) is `unsupported`; a Workbook or Book
(Excel 5.0/95), PowerPoint Document, WordDocument, hwp FileHeader, WordPerfect PerfectOffice_MAIN,
VisioDocument or Publisher Quill/QuillSub/CONTENTS stream names the parser that reads the file, and the caller hands
it on with `parse` once its own compound file is closed. A stream naming the caller itself is passed over (pub's own
stream is Contents, so a Quill stream without one is not sent back to pub). After the streams, a `\\x01CompObj` whose
clipboard format or ProgID starts `MSProject.` names mpp (Project has no stream name of its own; mpp itself asks
`route` when its CompObj does not name it). Anything else keeps the caller's damaged detail. Parsers are imported
late, since they import this module.
"""
import struct

from ..containers.cfb import CfbError
from ..limits import ReadError

PP95 = "PowerPoint 95 presentation"
_OWN = {"xls": ("Workbook", "Book"), "ppt": ("PowerPoint Document",), "doc": ("WordDocument",),
        "hwp": ("FileHeader",), "wpd": ("PerfectOffice_MAIN",), "vsd": ("VisioDocument",), "pub": ("Contents",)}
# in this order: a stream naming a parser, or an old format's refusal
_STREAMS = (("Workbook", "xls"), ("Book", "xls"), ("PowerPoint Document", "ppt"), ("PP40", PP95),
            ("WordDocument", "doc"), ("FileHeader", "hwp"), ("PerfectOffice_MAIN", "wpd"), ("VisioDocument", "vsd"),
            ("Quill/QuillSub/CONTENTS", "pub"))
COMP_OBJ = "\x01CompObj"
COMP_OBJ_MAX = 4096  # bytes of a CompObj stream read for its ProgID (a real one is a few dozen); past it, not read
MSPROJECT = "MSProject."
_CLIP_IDS = (0xFFFFFFFF, 0xFFFFFFFE)  # an AnsiClipboardFormat marker followed by a u32 format id, not a string


def _compobj(cf) -> tuple:
    """(clipboard format, ProgID) of the compound file's `\\x01CompObj` (MS-OLEDS CompObjStream: a 28-byte header, the
    AnsiUserType, the AnsiClipboardFormat, then the ProgID, each string a u32 length and its bytes), each up to its
    first NUL. The clipboard format is "" when it is a marker and id (or empty), or when its string does not fit in the
    stream; both are "" when the stream is missing, unreadable, over COMP_OBJ_MAX bytes (not read at all, as
    doc_objects and xls_embedded do) or too short to hold the field. Two length fields are skipped, so the work is
    fixed."""
    try:
        if not cf.exists(COMP_OBJ) or cf.size(COMP_OBJ) > COMP_OBJ_MAX:
            return "", ""
        data = cf.read(COMP_OBJ)
    except (KeyError, CfbError):
        return "", ""
    off, clip = 28, ""
    for field in ("user type", "clipboard format"):
        if off + 4 > len(data):
            return clip, ""
        n = struct.unpack_from("<I", data, off)[0]
        if field == "clipboard format" and n in _CLIP_IDS:
            n = 4
        elif field == "clipboard format" and off + 4 + n <= len(data):
            clip = _ansi(data[off + 4:off + 4 + n])
        off += 4 + n
    if off + 4 > len(data):
        return clip, ""
    n = struct.unpack_from("<I", data, off)[0]
    return clip, _ansi(data[off + 4:off + 4 + n])


def _ansi(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("latin-1")


def progid(cf) -> str:
    """The ANSI ProgID of the compound file's `\\x01CompObj` ("" as `_compobj` says)."""
    return _compobj(cf)[1]


def msproject(cf) -> bool:
    """Whether the `\\x01CompObj` names Microsoft Project: its clipboard-format string (what MPXJ keys on, as
    MSProject.MPP9) or its ProgID (MSProject.Project.9) starts `MSProject.`."""
    return any(v.startswith(MSPROJECT) for v in _compobj(cf))


def route(cf, own: str, damaged: str) -> str | None:
    """The parser ("xls", "ppt", "doc", "hwp", "wpd", "vsd", "pub" or "mpp") that reads the open compound file `cf`;
    None when that is `own` itself. Raises ReadError("encrypted") for an encrypted package, "unsupported" for an old
    format, and "damaged" with the detail `damaged` when no other stream or ProgID names a format."""
    if any(cf.exists(s) for s in _OWN.get(own, ())):
        return None
    if cf.exists("EncryptedPackage"):
        raise ReadError("encrypted")
    for stream, what in _STREAMS:
        if what != own and cf.exists(stream):
            if what in _OWN:
                return what
            raise ReadError("unsupported", what)
    if msproject(cf):
        return None if own == "mpp" else "mpp"
    raise ReadError("damaged", damaged)


def parse(fmt: str, path, ctx):
    """Read `path` with the parser `route` named."""
    from importlib import import_module
    return import_module(f"{__package__}.{fmt}").parse(path, ctx)
