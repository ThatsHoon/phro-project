"""OfficeArt records (MS-ODRAW): bounded record walks, BLIPs as image bytes, the BStore and shapes by spid.

Format-neutral: doc reads its DggInfo and inline pictures through it, and the xls and ppt readers of phase 2b will.
Offsets are into the buffer a caller passes, a record's offset being where its 8-byte header starts. Logic follows
dochan office_binary/officeart.py (the record walk, the BLIP kinds and their UIDs, the DIB file header, the FBSE and
its delay-loaded BLIP, FOPT properties); where it differs MS-ODRAW is followed: 0xF02A is a JPEG too, the compression
byte of a metafile decides whether it is deflated, and a BStore file block may be a BLIP itself.

Every length and offset read from a record is checked against the buffer, a walk stops at a record whose body passes
its end and visits at most MAX_RECORDS records, and a record moves the walk on by at least its 8-byte header.
"""
import struct
from collections.abc import Iterator

from . import limits
from .containers import flate
from .images import sniff

MAX_RECORDS = 1_000_000  # records one walk visits
SP_CONTAINER, SPGR_CONTAINER, FSP, FOPT = 0xF004, 0xF003, 0xF00A, 0xF00B
DGG_CONTAINER, BSTORE, FBSE = 0xF000, 0xF001, 0xF007
CLIENT_ANCHOR = 0xF010
PIB = 0x0104           # the pib property's id (opid & 0x3FFF; fBid is 0x4000, fComplex 0x8000)
_NO_DELAY = 0xFFFFFFFF  # an FBSE foDelay with no delay-loaded BLIP
_DEFLATE, _STORED = 0, 0xFE  # OfficeArtMetafileHeader compression
_PICT_HEAD = 512       # the zero head a PICT file starts with; a PICT BLIP holds the picture after it

# BLIP record type -> (kind, the recInstance values of one UID; each value + 1 has a second UID)
_BLIPS = {
    0xF01A: ("emf", (0x3D4,)), 0xF01B: ("wmf", (0x216,)), 0xF01C: ("pict", (0x542,)),
    0xF01D: ("jpg", (0x46A, 0x6E2)), 0xF02A: ("jpg", (0x46A, 0x6E2)), 0xF01E: ("png", (0x6E0,)),
    0xF01F: ("dib", (0x7A8,)), 0xF029: ("tiff", (0x6E4,)),
}
_METAFILES = frozenset(("emf", "wmf", "pict"))


def header(data, off: int) -> tuple[int, int, int, int] | None:
    """The record header at `off`: (ver, inst, type, length); None when 8 bytes do not fit there."""
    if off < 0 or off + 8 > len(data):
        return None
    opt, typ, size = struct.unpack_from("<HHI", data, off)
    return opt & 0xF, opt >> 4, typ, size


def records(data, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, int, int, int]]:
    """The records one after another in data[start:end], as (ver, inst, type, body_offset, body_len). The walk stops
    at a header that does not fit, at a body that passes `end`, and after MAX_RECORDS records."""
    end = len(data) if end is None else min(end, len(data))
    pos, n = max(start, 0), 0
    while pos + 8 <= end and n < MAX_RECORDS:
        opt, typ, size = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        if size > end - body:
            return
        yield opt & 0xF, opt >> 4, typ, body, size
        pos, n = body + size, n + 1


def _dib(dib: bytes) -> bytes | None:
    """A DIB as a BMP file: the 14-byte BITMAPFILEHEADER, its bfOffBits past the header, the palette and the masks
    (BI_BITFIELDS 12 bytes, BI_ALPHABITFIELDS 16, after a 40-byte header). None when those pass the DIB."""
    if len(dib) < 12:
        return None
    size = struct.unpack_from("<I", dib)[0]
    if size == 12:  # BITMAPCOREHEADER: RGBTRIPLE entries
        bpp = struct.unpack_from("<H", dib, 10)[0]
        table = (1 << bpp) * 3 if bpp <= 8 else 0
    elif 40 <= size <= len(dib):
        bpp, comp = struct.unpack_from("<HI", dib, 14)
        used = struct.unpack_from("<I", dib, 32)[0]
        table = (used or ((1 << bpp) if bpp <= 8 else 0)) * 4
        table += {3: 12, 6: 16}.get(comp, 0) if size == 40 else 0
    else:
        return None
    if size + table > len(dib):
        return None
    return struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, 14 + size + table) + dib


def blip(data, off: int, budget: flate.Budget) -> bytes | None:
    """The BLIP record at `off` as image bytes for Assets. A metafile (EMF, WMF, PICT) has its UIDs, the 34-byte
    OfficeArtMetafileHeader (cbSave u32 at 28, compression u8 at 32), then cbSave bytes, inflated within `budget`
    when compression is 0 (deflate) and kept when 0xFE; a PICT gets the 512-byte head of a PICT file. A bitmap has its
    UIDs, a tag byte, then the image; a DIB gets a BITMAPFILEHEADER, so Assets makes it a PNG. None for another record,
    an instance not of its kind, a body that passes the buffer, or a deflate stream that is corrupt or cut; a
    ReadError ("too_large" past the ratio cap or the document total) is the caller's."""
    h = header(data, off)
    if h is None or h[2] not in _BLIPS:
        return None
    kind, insts = _BLIPS[h[2]]
    body, end = off + 8, off + 8 + h[3]
    if end > len(data):
        return None
    uids = next((1 + h[1] - i for i in insts if h[1] in (i, i + 1)), 0)
    if not uids:
        return None
    pos = body + 16 * uids
    if kind in _METAFILES:
        if pos + 34 > end:
            return None
        save, comp = struct.unpack_from("<IB", data, pos + 28)
        pos += 34
        raw = bytes(data[pos:pos + min(save, end - pos)])
        if comp == _DEFLATE:
            try:
                raw = flate.inflate(raw, budget)
            except ValueError:  # flate.Truncated or not deflate at all
                return None
        elif comp != _STORED:
            return None
        if kind == "pict" and raw:
            raw = bytes(_PICT_HEAD) + raw
        return raw or None
    if pos + 1 >= end:
        return None
    raw = bytes(data[pos + 1:end])
    return _dib(raw) if kind == "dib" else raw


def fbse_delay(data, off: int) -> int | None:
    """The foDelay of the FBSE at `off` when its BLIP is delay-loaded: the FBSE is whole and in use (cRef > 0), and no
    BLIP header fits after its name. None for any other block, which fbse_blip reads without a delay stream; a caller
    reads the delay BLIP itself, so it can budget and memoize that read (fbse_blip's delay argument reads it each
    call)."""
    h = header(data, off)
    if h is None or h[2] != FBSE or h[3] < 36 or off + 8 + h[3] > len(data):
        return None
    body, end = off + 8, off + 8 + h[3]
    cref, at = struct.unpack_from("<II", data, body + 24)
    pos = body + 36 + data[body + 33]
    if not cref or pos > end or pos + 8 <= end or at == _NO_DELAY:
        return None
    return at


def fbse_blip(data, off: int, delay, budget: flate.Budget) -> bytes | None:
    """The image of the BStore file block at `off`. An FBSE (0xF007) has a 36-byte head (cRef u32 at 24, foDelay u32
    at 28, cbName u8 at 33), the name, then the BLIP embedded; without one the BLIP is the one at delay[foDelay:]
    (the delay stream, WordDocument for doc). A block that is a BLIP itself (MS-ODRAW
    OfficeArtBStoreContainerFileBlock) is read as one. None for an empty slot (cRef 0), a name or BLIP that passes
    its record or the delay stream, or no BLIP at all; see blip()."""
    h = header(data, off)
    if h is None:
        return None
    if h[2] != FBSE:
        return blip(data, off, budget) if h[2] in _BLIPS else None
    body, end = off + 8, off + 8 + h[3]
    if end > len(data) or h[3] < 36:
        return None
    cref, at = struct.unpack_from("<II", data, body + 24)
    pos = body + 36 + data[body + 33]
    if not cref or pos > end:
        return None
    if pos + 8 <= end:
        return blip(bytes(data[pos:end]), 0, budget)  # the embedded BLIP, kept inside its FBSE
    if delay is None or at == _NO_DELAY or at >= len(delay):
        return None
    return blip(delay, at, budget)


def blip_image(data, off: int, delay, budget: flate.Budget) -> bytes | None:
    """The image bytes of the BStore file block at `off` as xls and ppt place them: fbse_blip's, or None when the block
    is unreadable (a damaged or oversize BLIP, or one that inflates past the ratio cap or the document's total: that
    costs the one picture) or is not a kind Assets keeps (under 16 bytes, or images.sniff does not know it). `delay` is
    the stream a delay-loaded BLIP is read from (None when the host has none)."""
    try:
        got = fbse_blip(data, off, delay, budget)
    except limits.ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        return None
    return got if got is not None and len(got) >= 16 and sniff(got) is not None else None


def bstore(dgg) -> list[int]:
    """The offsets of the BStoreContainer's (0xF001) file blocks in the DggContainer (0xF000) that starts `dgg`, in
    order: block k is pib k + 1. Empty when there is no such container."""
    first = next(records(dgg), None)
    if first is None or first[2] != DGG_CONTAINER:
        return []
    for _, _, typ, body, size in records(dgg, first[3], first[3] + first[4]):
        if typ == BSTORE:
            return [b - 8 for _, _, _, b, _ in records(dgg, body, body + size)]
    return []


def shape_pib(data, sp_off: int) -> int | None:
    """The pib (property 0x4104, a 1-based BStore index) of the SpContainer at `sp_off`, from its OfficeArtFOPT
    (0xF00B): inst properties of 6 bytes (opid u16, op u32), as many as fit its body. None without one."""
    h = header(data, sp_off)
    if h is None or h[2] != SP_CONTAINER:
        return None
    for _, inst, typ, body, size in records(data, sp_off + 8, sp_off + 8 + h[3]):
        if typ == FOPT:
            return _fopt_pib(data, inst, body, size)
    return None


def _fopt_pib(data, inst: int, body: int, size: int) -> int | None:
    """The pib of the FOPT whose body is data[body:body + size]; a complex one (fComplex) is not a pib."""
    return fopt_property(data, inst, body, size, PIB)


def fopt_property(data, inst: int, body: int, size: int, prop: int) -> int | None:
    """The value of property `prop` (opid & 0x3FFF) in the FOPT whose body is data[body:body + size]: inst properties
    of 6 bytes (opid u16, op u32), as many as fit. The first one wins; None when it is absent or complex (fComplex)."""
    n = min(inst, size // 6)
    for opid, op in struct.iter_unpack("<HI", data[body:body + 6 * n]):
        if opid & 0x3FFF == prop and not opid & 0x8000:
            return op
    return None


class Shapes:
    """The shapes of OfficeArt drawings, one walk over their records (O(records)):

    - `at`: spid -> the offset of the SpContainer whose FSP (0xF00A) names it, the first one winning;
    - `pib`: SpContainer offset -> its pib (the first FOPT's 0x4104), for each shape with one that is not 0;
    - `pics`: those pibs in record order;
    - `anchors`: SpContainer offset -> the body offset and size of its first ClientAnchor (0xF010), each host reading
      that body itself (xls: an OfficeArtClientAnchorSheet);
    - `groups`: the offset of each group shape (MS-ODRAW: the first SpContainer of an SpgrContainer) -> (i, j), its
      members' pictures being pics[i:j], those of nested groups included.

    Containers (ver 0xF) are entered; an SpContainer's own children are read, not entered. A container past
    limits.MAX_DEPTH is not entered and counted in `deep`, its shapes left out of every group; `cut` is set when the
    walks of this index pass MAX_RECORDS records together, the groups still open ending there."""

    def __init__(self):
        self.at: dict[int, int] = {}
        self.pib: dict[int, int] = {}
        self.pics: list[int] = []
        self.anchors: dict[int, tuple[int, int]] = {}
        self.groups: dict[int, tuple[int, int]] = {}
        self.deep = 0
        self.cut = False
        self._n = 0

    def add(self, data, start: int, end: int):
        """Index the records in data[start:end]."""
        # a level: its walk, and for an SpgrContainer's walk its group shape's offset (-1 until that shape is met)
        stack = [[records(data, start, end), None]]
        while stack:
            level = stack[-1]
            rec = next(level[0], None)
            if rec is None:
                self._close(stack.pop())
                continue
            self._n += 1
            if self._n > MAX_RECORDS:
                self.cut = True
                break
            ver, _, typ, body, size = rec
            if typ == SP_CONTAINER:
                self._shape(data, body, size)
                if level[1] == -1:
                    level[1] = body - 8
                    self.groups[body - 8] = (len(self.pics), len(self.pics))
            elif ver == 0xF:
                if len(stack) >= limits.MAX_DEPTH:
                    self.deep += 1
                else:
                    stack.append([records(data, body, body + size), -1 if typ == SPGR_CONTAINER else None])
        while stack:
            self._close(stack.pop())

    def _close(self, level):
        """An SpgrContainer's walk ends: its group's members are the pictures met since its group shape."""
        if level[1] is not None and level[1] >= 0:
            self.groups[level[1]] = (self.groups[level[1]][0], len(self.pics))

    def _shape(self, data, body: int, size: int):
        """The SpContainer whose body is data[body:body + size]: its first FSP's spid, first FOPT's pib and first
        ClientAnchor."""
        spid = pib = None
        for _, inst, t, b, s in records(data, body, body + size):
            self._n += 1
            if t == FSP and spid is None and s >= 4:
                spid = struct.unpack_from("<I", data, b)[0]
                self.at.setdefault(spid, body - 8)
            elif t == FOPT and pib is None:
                pib = _fopt_pib(data, inst, b, s) or 0
                if pib:
                    self.pib[body - 8] = pib
                    self.pics.append(pib)
            elif t == CLIENT_ANCHOR:
                self.anchors.setdefault(body - 8, (b, s))
