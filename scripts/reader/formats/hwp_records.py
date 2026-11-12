"""HWP 5.0 records, FileHeader and DocInfo.

Offsets follow reverse/hwp.md and were checked against dochan hwp/doc_info.py, hwp/records/char_shape.py and
hwp/records/style.py and kordoc hwp5/record.ts (NUMBERING, BULLET, PARA_SHAPE).
"""
import struct
from dataclasses import dataclass

from .. import limits
from ..containers.cfb import Compound
from ..limits import ReadError
from .hancom import NUM_KINDS

DOCUMENT_PROPERTIES = 16
ID_MAPPINGS = 17
BIN_DATA = 18
CHAR_SHAPE = 21
NUMBERING = 23
BULLET = 24
PARA_SHAPE = 25
STYLE = 26
DISTRIBUTE_DOC_DATA = 28

PARA_HEADER = 66
PARA_TEXT = 67
PARA_CHAR_SHAPE = 68
PARA_RANGE_TAG = 70
CTRL_HEADER = 71
LIST_HEADER = 72
FOOTNOTE_SHAPE = 74
SHAPE_COMPONENT = 76
TABLE = 77
SHAPE_OLE = 84
SHAPE_PICTURE = 85
SHAPE_CONTAINER = 86
EQEDIT = 88

SIGNATURE = b"HWP Document File"


@dataclass
class Record:
    tag: int
    level: int
    data: bytes


def records(data: bytes, ctx) -> list[Record]:
    out: list[Record] = []
    n = len(data)
    pos = 0
    unpack = struct.unpack_from
    while pos < n:
        if len(out) >= limits.HWP_RECORDS:
            ctx.skipped["record"] += 1
            ctx.warnings.append(f"record: more than {limits.HWP_RECORDS} records, the rest is dropped")
            break
        if pos + 4 > n:
            ctx.skipped["record"] += 1
            break
        w = unpack("<I", data, pos)[0]
        pos += 4
        size = w >> 20
        if size == 0xFFF:
            if pos + 4 > n:
                ctx.skipped["record"] += 1
                break
            size = unpack("<I", data, pos)[0]
            pos += 4
        if size > n - pos:
            ctx.skipped["record"] += 1
            break
        out.append(Record(w & 0x3FF, (w >> 10) & 0x3FF, data[pos:pos + size]))
        pos += size
    return out


@dataclass
class Header:
    version: str
    compressed: bool
    password: bool
    distribution: bool
    drm: bool
    cert_encrypted: bool
    cert_drm: bool
    track_changes: bool


def file_header(cf: Compound) -> Header:
    try:
        data = cf.read("FileHeader")
    except KeyError:
        raise ReadError("damaged", "not an HWP file")
    if len(data) < 40 or not data.startswith(SIGNATURE):
        raise ReadError("damaged", "not an HWP file")
    v, flags = struct.unpack_from("<II", data, 32)
    return Header(
        version=f"{v >> 24}.{(v >> 16) & 255}.{(v >> 8) & 255}.{v & 255}",
        compressed=bool(flags & 1), password=bool(flags & 2), distribution=bool(flags & 4),
        drm=bool(flags & 16), cert_encrypted=bool(flags & 256), cert_drm=bool(flags & 1024),
        track_changes=bool(flags & 16384))


@dataclass
class BinItem:
    kind: str       # "link", "embed" or "storage"
    id: int         # the storage id: BinData/BIN%04X.<ext>
    ext: str
    compress: int   # 0 follow the document, 1 compressed, 2 stored


@dataclass
class ParaShape:
    head: int       # 0 none, 1 outline, 2 numbering, 3 bullet
    level: int
    ref: int        # numbering or bullet id, 1-based


@dataclass
class Style:
    name: str
    local: str
    para: int
    char: int


@dataclass
class Numbering:
    formats: list[str]
    kinds: list[str]
    starts: list[int]


@dataclass
class DocInfo:
    sections: int
    bins: dict[int, BinItem]    # keyed by the 1-based position of the BIN_DATA record, which pictures refer to
    bold: list[bool]
    para: list[ParaShape]
    styles: list[Style]
    numberings: list[Numbering]
    bullets: list[str]


def _wstr(data: bytes, pos: int) -> tuple[str, int]:
    """u16 character count, then that many UTF-16LE characters; (text, next offset). Bounds-checked."""
    n = struct.unpack_from("<H", data, pos)[0]
    end = pos + 2 + n * 2
    if end > len(data):
        raise ValueError("string runs past the record")
    return data[pos + 2:end].decode("utf-16-le", errors="replace"), end


def _bin_item(data: bytes) -> BinItem:
    attr = struct.unpack_from("<H", data, 0)[0]
    compress = (attr >> 4) & 3
    t = attr & 0xF
    if t == 0:
        return BinItem("link", 0, "", compress)
    bid = struct.unpack_from("<H", data, 2)[0]
    if t == 2:
        return BinItem("storage", bid, "", compress)
    ext = _wstr(data, 4)[0] if len(data) >= 6 else ""
    return BinItem("embed", bid, ext, compress)


def _para_shape(data: bytes) -> ParaShape:
    attr = struct.unpack_from("<I", data, 0)[0]
    ref = struct.unpack_from("<H", data, 30)[0] if len(data) >= 32 else 0
    return ParaShape((attr >> 23) & 3, (attr >> 25) & 7, ref)


def _style(data: bytes) -> Style:
    name, pos = _wstr(data, 0)
    local, pos = _wstr(data, pos)
    # type u8, next style u8, language i16, then the two ids
    para, char = struct.unpack_from("<HH", data, pos + 4)
    return Style(name, local, para, char)


def _numbering(data: bytes) -> Numbering:
    if len(data) < 14:
        raise ValueError("short NUMBERING record")
    formats, kinds = [], []
    pos = 0
    for _ in range(7):
        if pos + 12 > len(data):
            formats.append("")
            kinds.append("DIGIT")
            continue
        code = (struct.unpack_from("<I", data, pos)[0] >> 5) & 0xF  # 12-byte head: attr, widths, char shape
        kinds.append(NUM_KINDS[code] if code < len(NUM_KINDS) else "DIGIT")
        text, pos = _wstr(data, pos + 12)
        formats.append(text)
    base = 1
    if pos + 2 <= len(data):
        base = struct.unpack_from("<H", data, pos)[0] or 1
        pos += 2
    starts = []
    for _ in range(7):
        if pos + 4 <= len(data):
            starts.append(struct.unpack_from("<I", data, pos)[0] or 1)
            pos += 4
        else:
            starts.append(base)
    return Numbering(formats, kinds, starts)


def doc_info(data: bytes, ctx) -> DocInfo:
    info = DocInfo(0, {}, [], [], [], [], [])
    n_bins = 0
    for rec in records(data, ctx):
        d, tag = rec.data, rec.tag
        # a damaged record still takes its slot so later ids keep pointing at the right entry
        with ctx.guard("styles"):
            if tag == DOCUMENT_PROPERTIES:
                info.sections = struct.unpack_from("<H", d, 0)[0]
            elif tag == BIN_DATA:
                n_bins += 1
                info.bins[n_bins] = BinItem("link", 0, "", 0)
                info.bins[n_bins] = _bin_item(d)
            elif tag == CHAR_SHAPE:
                info.bold.append(False)
                info.bold[-1] = bool(struct.unpack_from("<I", d, 46)[0] & 2)
            elif tag == PARA_SHAPE:
                info.para.append(ParaShape(0, 0, 0))
                info.para[-1] = _para_shape(d)
            elif tag == STYLE:
                info.styles.append(Style("", "", 0, 0))
                info.styles[-1] = _style(d)
            elif tag == NUMBERING:
                info.numberings.append(Numbering([""] * 7, ["DIGIT"] * 7, [1] * 7))
                info.numberings[-1] = _numbering(d)
            elif tag == BULLET:
                info.bullets.append("•")
                code = struct.unpack_from("<H", d, 12)[0]
                if code:
                    # a lone surrogate cannot be written as UTF-8, so it becomes the replacement box
                    info.bullets[-1] = "□" if 0xD800 <= code <= 0xDFFF else chr(code)
    return info
