"""Word 97-2003 binary structures: the FIB, the piece table and the text of a story's CP range.

Written from MS-DOC (2.5.1 Fib, 2.9.38 Clx, 2.9.177 PlcPcd, 2.9.73 FcCompressed, 2.4.1 retrieving text); logic follows
dochan office_binary/doc_binary.py (FIB walk, Clx, piece CPs, cumulative story ranges). Every count and offset is
checked against the stream it is read from.

FibRgFcLcb97 indices this reader uses (MS-DOC 2.5.6; dochan reads all but 73/74 at these indices, anydoc reads 1,
2/3, 12/13, 33, 46/47 and 73/74 at the matching absolute offsets): 1 STSH, 2/3 PlcffndRef/Txt, 4/5
PlcfandRef/Txt, 11 PlcfHdd, 12/13 PlcfBteChpx/Papx, 33 Clx, 36 GrpXstAtnOwners, 40 PlcSpaMom, 46/47
PlcfendRef/Txt, 50 DggInfo, 56 PlcftxbxTxt, 58 PlcfHdrtxbxTxt, 73 PlfLst, 74 PlfLfo.
"""
import struct
from bisect import bisect_right
from dataclasses import dataclass

from ..limits import ReadError
from ..text import codepage

CLX = 33  # the FibRgFcLcb97 index of the Clx
# the stories in CP order, from FibRgLw97 entries 3-10; "macro" (reserved3, formerly ccpMcr) keeps its place in the
# sum as the Word 97 layout gives it; MS-DOC says it is 0
STORIES = ("text", "footnote", "header", "macro", "annotation", "endnote", "textbox", "header_textbox")
PIECES = 1_000_000  # pieces one PlcPcd may declare; past it the piece table is not read
_IDENT = 0xA5EC
_OLD_IDENTS = (0xA5DB, 0xA5DC)  # the wIdent of Word 2 and of Word 6/95
_WORD97 = 0x00C1  # the lowest nFib of the Word 97 layout


@dataclass
class Fib:
    nfib: int
    lid: int
    lid_fe: int
    far_east: bool
    encrypted: bool
    obfuscated: bool
    table: str                   # the table stream: "1Table" or "0Table"
    ccp: dict[str, int]          # story name -> its length in CPs
    fc: list[tuple[int, int]]    # FibRgFcLcb97 (fc, lcb) pairs

    def pair(self, i: int) -> tuple:
        """The (fc, lcb) of FibRgFcLcb97 entry i; (0, 0) when the FIB has no such entry."""
        return self.fc[i] if 0 <= i < len(self.fc) else (0, 0)


@dataclass(slots=True)
class Piece:
    cp: int
    cp_end: int
    fc: int          # the byte offset of the text in WordDocument
    compressed: bool  # 8-bit text, one byte a CP, else UTF-16LE
    prm: int


def fib(word: bytes) -> Fib:
    """The FIB at the head of the WordDocument stream. Past FibBase an encrypted FIB is ciphertext, so for an
    encrypted or obfuscated document only FibBase is read."""
    n = len(word)
    if n < 2 or struct.unpack_from("<H", word)[0] not in (_IDENT, *_OLD_IDENTS):
        raise ReadError("damaged", "not a Word document")
    if n < 34:
        raise ReadError("damaged", "FIB")
    ident, nfib, _, lid, _, flags = struct.unpack_from("<6H", word)
    if ident != _IDENT or nfib < _WORD97:
        raise ReadError("unsupported", "word 6/95")
    out = Fib(nfib, lid, 0, bool(flags & 0x4000), bool(flags & 0x0100), bool(flags & 0x8000),
              "1Table" if flags & 0x0200 else "0Table", {name: 0 for name in STORIES}, [])
    if out.encrypted or out.obfuscated:
        return out

    def count(pos, width):
        """The u16 count at pos and the end of the array of `width`-byte entries after it, both inside the stream."""
        if pos + 2 > n:
            raise ReadError("damaged", "FIB")
        k = struct.unpack_from("<H", word, pos)[0]
        if pos + 2 + k * width > n:
            raise ReadError("damaged", "FIB")
        return k, pos + 2 + k * width

    csw, pos = count(0x20, 2)
    if csw > 13:
        out.lid_fe = struct.unpack_from("<H", word, 0x22 + 26)[0]
    cslw, end = count(pos, 4)
    lw = struct.unpack_from("<%dI" % min(cslw, 3 + len(STORIES)), word, pos + 2)
    for k, name in enumerate(STORIES):
        if 3 + k < len(lw):
            out.ccp[name] = lw[3 + k]
    _, stop = count(end, 8)
    out.fc = list(struct.iter_unpack("<II", word[end + 2:stop]))
    return out


def pieces(table: bytes, fc: int, lcb: int) -> list[Piece]:
    """The pieces of the Clx at table[fc:fc + lcb]: Prc records (clxt 1, u16 cb, grpprl) are skipped, then the Pcdt
    (clxt 2, u32 lcb) holds the PlcPcd, n + 1 CPs and n 8-byte PCDs (fc u32 at 2, prm u16 at 6). A Clx outside the
    stream, without a Pcdt, with a PlcPcd size that is not 4 + 12n or with decreasing CPs is damaged."""
    if lcb == 0:
        raise ReadError("damaged", "no piece table")
    if fc + lcb > len(table):
        raise ReadError("damaged", "piece table outside the table stream")
    clx = memoryview(table)[fc:fc + lcb]
    pos = 0
    while pos + 3 <= lcb and clx[pos] == 1:  # each Prc moves on by at least 3 bytes
        pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
    if pos + 5 > lcb or clx[pos] != 2:
        raise ReadError("damaged", "no piece table")
    size = struct.unpack_from("<I", clx, pos + 1)[0]
    pos += 5
    if size < 4 or (size - 4) % 12 or pos + size > lcb:
        raise ReadError("damaged", "piece table size")
    k = (size - 4) // 12
    if k > PIECES:
        raise ReadError("damaged", "piece table size")
    cps = struct.unpack_from("<%dI" % (k + 1), clx, pos)
    out = []
    for i, (_, raw, prm) in enumerate(struct.iter_unpack("<HIH", clx[pos + 4 * (k + 1):pos + size])):
        a, b = cps[i], cps[i + 1]
        if b < a:
            raise ReadError("damaged", "piece table CPs")
        compressed = bool(raw & 0x40000000)
        at = raw & 0x3FFFFFFF
        out.append(Piece(a, b, at // 2 if compressed else at, compressed, prm))
    return out


def fit(pcs: list[Piece], size: int) -> tuple[list[Piece], int]:
    """The pieces cut to the bytes a `size`-byte WordDocument holds, empty ones left out, and how many were cut. The
    pieces of one document never share bytes, so together they hold at most `size`; past that the rest are cut too,
    which keeps the text O(input)."""
    out, cut, room = [], 0, size
    for p in pcs:
        n = p.cp_end - p.cp
        if n <= 0:
            continue
        w = 1 if p.compressed else 2
        keep = max(0, min(n, (size - p.fc) // w, room // w))
        if keep < n:
            cut += 1
        if keep:
            room -= keep * w
            out.append(Piece(p.cp, p.cp + keep, p.fc, p.compressed, p.prm))
    return out, cut


def story_ranges(ccp: dict, end: int) -> dict:
    """Story name -> its (cp0, cp1): the stories follow one another in STORIES order, each ccp long, clamped to
    `end`, the last CP the pieces hold."""
    out, cp = {}, 0
    for name in STORIES:
        nxt = cp + ccp.get(name, 0)
        out[name] = (min(cp, end), min(nxt, end))
        cp = nxt
    return out


def _cp(p: Piece) -> int:
    return p.cp


_DBCS = frozenset(("cp932", "cp936", "cp949", "cp950"))


def compressed_codepage(lid: int) -> str:
    """The codec of a compressed piece: one byte is one CP (MS-DOC 2.9.73), so the code page of `lid` only when it is
    single-byte; under a double-byte one (Japanese, Chinese, Korean) the bytes are cp1252."""
    cp = codepage(lid)
    return "cp1252" if cp in _DBCS else cp


@dataclass
class Story:
    """The text of CPs [cp0, cp1), one str unit a CP: a surrogate pair stays two units until a Text is built. A part
    is the run of one piece inside the range; part k starts at text index idx[k] and CP cps[k]."""
    text: str
    idx: list[int]
    cps: list[int]
    cp0: int
    cp1: int

    def cp_of(self, i: int) -> int:
        """The CP of text index i; len(text) is the CP after the last part."""
        k = bisect_right(self.idx, i) - 1
        return self.cp0 if k < 0 else self.cps[k] + i - self.idx[k]

    def index_of(self, cp: int) -> int:
        """The text index of `cp`; a CP no piece holds maps to the index of the next CP that one does."""
        k = bisect_right(self.cps, cp) - 1
        if k < 0:
            return 0
        n = (self.idx[k + 1] if k + 1 < len(self.idx) else len(self.text)) - self.idx[k]
        return self.idx[k] + min(cp - self.cps[k], n)


def _units(raw: bytes) -> str:
    """UTF-16LE code units, one str unit each: a pair stays two surrogates (utf-16 with surrogatepass joins them,
    so the units are widened to UTF-32 first); a unit cut off by the stream's end is left out."""
    raw = raw[:len(raw) // 2 * 2]
    s = raw.decode("utf-16-le", "surrogatepass")
    if len(s) * 2 == len(raw):
        return s
    w = bytearray(len(raw) * 2)
    w[0::4], w[1::4] = raw[0::2], raw[1::2]
    return w.decode("utf-32-le", "surrogatepass")


def story(word: bytes, pcs: list[Piece], cp0: int, cp1: int, lid: int) -> Story:
    """The Story of CPs [cp0, cp1): compressed pieces in compressed_codepage(lid), the rest UTF-16LE units. Pieces
    are in CP order; the parts are joined once."""
    parts, idx, cps, n = [], [], [], 0
    codec = compressed_codepage(lid)
    for i in range(max(bisect_right(pcs, cp0, key=_cp) - 1, 0), len(pcs)):
        p = pcs[i]
        if p.cp >= cp1:
            break
        a, b = max(cp0, p.cp), min(cp1, p.cp_end)
        if a >= b:
            continue
        if p.compressed:
            s = bytes(word[p.fc + a - p.cp:p.fc + b - p.cp]).decode(codec, "replace")
        else:
            s = _units(word[p.fc + 2 * (a - p.cp):p.fc + 2 * (b - p.cp)])
        if s:
            parts.append(s)
            idx.append(n)
            cps.append(a)
            n += len(s)
    return Story("".join(parts), idx, cps, cp0, cp1)


def story_text(word: bytes, pcs: list[Piece], cp0: int, cp1: int, lid: int) -> str:
    """The text of story(); its surrogates are still unpaired units."""
    return story(word, pcs, cp0, cp1, lid).text
