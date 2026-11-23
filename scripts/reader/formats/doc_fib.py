"""Word 97-2003 and Word 6/95 binary structures: the FIB, the piece table, the text of a story's CP range, the
formatting (sprms, CHPX and PAPX FKPs, piece Prms and the style sheet), a table row's TDefTable, and the lists with
their numbers.

Word 6/95 (an nFib from 101 to below 0xC1, wIdent 0xA5EC or 0xA5DC) keeps every table in WordDocument, its text 8-bit in the code page
of the document's language (one byte a CP, a double-byte character two CPs), its sprms one-byte codes that
_OLD_SPRMS maps to the Word 97 sprms they match, its PlcBte PNs u16 and its PAPX BXs 7 bytes; its FIB is a fixed
layout (FibRgFcLcb entries 0-37 from 0x58, 38 on from 0x192), its STD base 8 bytes with an 8-bit name. Written from
the Word 6 binary file format (the FIB, the FKPs, the STSH and the sprm table), checked against a Word 6 file.

Written from MS-DOC (2.5.1 Fib, 2.9.38 Clx, 2.9.177 PlcPcd, 2.9.73 FcCompressed, 2.4.1 retrieving text, 2.2.5 Sprm
and Prm, 2.9.33/2.9.175 ChpxFkp/PapxFkp, 2.9.271 STSH, 2.4.6 applying properties, TDefTableOperand and TCGRF, PlfLst,
LVL, PlfLfo and LFOData, determining the level number of a paragraph); logic follows dochan
office_binary/doc_binary.py (FIB walk, Clx, piece CPs, cumulative story ranges, decode_grpprl operand sizes, _fkps,
_read_styles) and doc_tables.py (the TDefTable checks), and anydoc formats/doc (Prm0 isprm table, STD walk, toggles
against the style's value; lists.rs: the PlfLst and PlfLfo walk, numbers shared per lsid). Every count and offset is
checked against the stream it is read from.

FibRgFcLcb97 indices this reader uses (MS-DOC 2.5.6; dochan reads all but 73/74 at these indices, anydoc reads 1,
2/3, 12/13, 33, 46/47 and 73/74 at the matching absolute offsets): 1 STSH, 2/3 PlcffndRef/Txt, 4/5
PlcfandRef/Txt, 11 PlcfHdd, 12/13 PlcfBteChpx/Papx, 33 Clx, 36 GrpXstAtnOwners, 40 PlcSpaMom, 46/47
PlcfendRef/Txt, 50 DggInfo, 56 PlcftxbxTxt, 58 PlcfHdrtxbxTxt, 73 PlfLst, 74 PlfLfo.
"""
import codecs
import datetime
import re
import struct
from bisect import bisect_right
from dataclasses import dataclass, replace

from ..limits import ReadError
from ..text import codepage
from .hancom import number

CLX = 33  # the FibRgFcLcb97 index of the Clx
STSH, BTE_CHPX, BTE_PAPX = 1, 12, 13  # FibRgFcLcb97 indices of the style sheet and the two PlcBte
# the stories in CP order, from FibRgLw97 entries 3-10; "macro" (reserved3, formerly ccpMcr) keeps its place in the
# sum as the Word 97 layout gives it; MS-DOC says it is 0
STORIES = ("text", "footnote", "header", "macro", "annotation", "endnote", "textbox", "header_textbox")
PIECES = 1_000_000  # pieces one PlcPcd may declare; past it the piece table is not read
_IDENT = 0xA5EC
_OLD_IDENTS = (0xA5DB, 0xA5DC)  # the wIdent of Word 2 and of Word 6/95
_WORD6, _WORD97 = 101, 0x00C1  # the lowest nFib of the Word 6 and of the Word 97 layout
_OLD_HEAD = 0x192   # where a Word 6 FIB's FibRgFcLcb entry 38 starts, after the PN fields at 0x18A
_OLD_PAIRS = 59     # Word 6 FibRgFcLcb entries read: 0 to 58 (PlcfHdrtxbxTxt)
_OLD_UNUSED = (40, 41, 50)  # entries Word 97 gives PlcSpaMom, PlcSpaHdr and DggInfo; Word 6 leaves them unused
MAC = 256  # a Word 6 FIB's chse for text in the Macintosh character set


@dataclass
class Fib:
    nfib: int
    lid: int
    lid_fe: int
    far_east: bool
    encrypted: bool
    obfuscated: bool
    table: str                   # the table stream: "1Table", "0Table", or "WordDocument" for Word 6
    ccp: dict[str, int]          # story name -> its length in CPs
    fc: list[tuple[int, int]]    # FibRgFcLcb97 (fc, lcb) pairs
    old: bool = False            # the Word 6/95 layout
    complex: bool = False        # Word 6 fComplex: the text is a piece table, else fcMin..fcMac
    chse: int = 0                # Word 6: the character set, MAC or Windows
    fc_min: int = 0
    fc_mac: int = 0
    pn_first: tuple = (0, 0)     # Word 6 pnChpFirst, pnPapFirst: the first FKP page of each kind
    cpn: tuple = (0, 0)          # Word 6 cpnBteChp, cpnBtePap: how many FKP pages of each kind

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
    """The FIB at the head of the WordDocument stream, Word 97 or Word 6/95 (an nFib below 0xC1, _old_fib). Past
    FibBase an encrypted FIB is ciphertext, so for an encrypted or obfuscated document only FibBase is read. Word 2
    and older (wIdent 0xA5DB, or an nFib below 101) are unsupported."""
    n = len(word)
    if n < 2 or struct.unpack_from("<H", word)[0] not in (_IDENT, *_OLD_IDENTS):
        raise ReadError("damaged", "not a Word document")
    if n < 34:
        raise ReadError("damaged", "FIB")
    ident, nfib, _, lid, _, flags = struct.unpack_from("<6H", word)
    if ident == _OLD_IDENTS[0] or nfib < _WORD6:
        raise ReadError("unsupported", "word 2")
    if nfib < _WORD97:
        return _old_fib(word, nfib, lid, flags)
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
    if n >= 0x3E:  # FibRgW97 is 14 words (MS-DOC 2.5.2: csw MUST be 14): a FIB that says fewer has lidFE there too
        out.lid_fe = struct.unpack_from("<H", word, 0x3C)[0]
    cslw, end = count(pos, 4)
    lw = struct.unpack_from("<%dI" % min(cslw, 3 + len(STORIES)), word, pos + 2)
    for k, name in enumerate(STORIES):
        if 3 + k < len(lw):
            out.ccp[name] = lw[3 + k]
    _, stop = count(end, 8)
    out.fc = list(struct.iter_unpack("<II", word[end + 2:stop]))
    return out


def _old_fib(word: bytes, nfib: int, lid: int, flags: int) -> Fib:
    """A Word 6/95 FIB: flags fComplex 0x0004 and fEncrypted 0x0100; chse u16 at 0x14; fcMin and fcMac at 0x18; the
    story lengths, u32 in STORIES order, at 0x34; FibRgFcLcb entries 0-37 at 0x58 and 38 on at 0x192, (fc, lcb) u32
    pairs into WordDocument, read to entry 58 or as far as the stream holds; pnChpFirst, pnPapFirst, cpnBteChp and
    cpnBtePap u16 at 0x18A. A stream shorter than the fixed fields is damaged."""
    n = len(word)
    if n < _OLD_HEAD:
        raise ReadError("damaged", "FIB")
    out = Fib(nfib, lid, 0, False, bool(flags & 0x0100), False, "WordDocument", {name: 0 for name in STORIES}, [],
              old=True, complex=bool(flags & 0x0004))
    if out.encrypted:
        return out
    out.chse = struct.unpack_from("<H", word, 0x14)[0]
    out.fc_min, out.fc_mac = struct.unpack_from("<II", word, 0x18)
    out.ccp = dict(zip(STORIES, struct.unpack_from("<%dI" % len(STORIES), word, 0x34)))
    more = min(_OLD_PAIRS - 38, (n - _OLD_HEAD) // 8)
    pairs = list(struct.iter_unpack("<II", word[0x58:0x58 + 8 * 38]))
    pairs += struct.iter_unpack("<II", word[_OLD_HEAD:_OLD_HEAD + 8 * more])
    out.fc = [(0, 0) if i in _OLD_UNUSED else p for i, p in enumerate(pairs)]
    chp, pap, cpn_chp, cpn_pap = struct.unpack_from("<4H", word, 0x18A)
    out.pn_first, out.cpn = (chp, pap), (cpn_chp, cpn_pap)
    return out


def old_codec(f: Fib) -> str:
    """The codec of a Word 6 file's 8-bit text: Mac Roman for the Macintosh character set, else the code page of its
    language, a double-byte one too (story() then gives each double-byte character its two CPs)."""
    return "mac_roman" if f.chse == MAC else codepage(f.lid)


def old_text(f: Fib) -> list["Piece"]:
    """The one piece of a Word 6 file without a piece table: fcMin..fcMac of WordDocument, 8-bit; damaged when
    fcMin is inside the FIB or past fcMac."""
    if not _OLD_HEAD <= f.fc_min <= f.fc_mac:
        raise ReadError("damaged", "text range")
    return [Piece(0, f.fc_mac - f.fc_min, f.fc_min, True, 0)]


def _prc_walk(clx) -> tuple[list[tuple[int, int]], int]:
    """The (start, end) of each Prc grpprl (clxt 1, u16 cb, grpprl) at the head of a Clx, cut to the Clx, and where
    the Prcs end. Each Prc moves on by at least 3 bytes."""
    spans, pos, n = [], 0, len(clx)
    while pos + 3 <= n and clx[pos] == 1:
        end = pos + 3 + struct.unpack_from("<H", clx, pos + 1)[0]
        spans.append((pos + 3, min(end, n)))
        pos = end
    return spans, pos


def prcs(table: bytes, fc: int, lcb: int) -> list[bytes]:
    """The grpprls of the Clx's Prc records, the list a Prm1 indexes; empty when the Clx is outside the stream."""
    if fc + lcb > len(table):
        return []
    clx = table[fc:fc + lcb]
    return [clx[a:b] for a, b in _prc_walk(clx)[0]]


def pieces(table: bytes, fc: int, lcb: int, old: bool = False) -> list[Piece]:
    """The pieces of the Clx at table[fc:fc + lcb]: Prc records (clxt 1, u16 cb, grpprl) are skipped, then the Pcdt
    (clxt 2, u32 lcb) holds the PlcPcd, n + 1 CPs and n 8-byte PCDs (fc u32 at 2, prm u16 at 6). A Word 97 fc is an
    FcCompressed; an `old` (Word 6) one is the plain offset of 8-bit text. A Clx outside the stream, without a Pcdt,
    with a PlcPcd size that is not 4 + 12n or with decreasing CPs is damaged."""
    if lcb == 0:
        raise ReadError("damaged", "no piece table")
    if fc + lcb > len(table):
        raise ReadError("damaged", "piece table outside the table stream")
    clx = memoryview(table)[fc:fc + lcb]
    pos = _prc_walk(clx)[1]
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
        if old:
            out.append(Piece(a, b, raw, True, prm))
            continue
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
    """The text of CPs [cp0, cp1), one str unit a character of width[k] CPs: one, a surrogate pair staying two units
    until a Text is built, or two for a double-byte character of Word 6 text. A part is a run of one piece inside the
    range whose characters have one width; part k starts at text index idx[k] and CP cps[k]."""
    text: str
    idx: list[int]
    cps: list[int]
    cp0: int
    cp1: int
    width: list[int]

    def cp_of(self, i: int) -> int:
        """The CP of text index i; len(text) is the CP after the last part."""
        k = bisect_right(self.idx, i) - 1
        return self.cp0 if k < 0 else self.cps[k] + (i - self.idx[k]) * self.width[k]

    def index_of(self, cp: int) -> int:
        """The text index of `cp`; a CP no piece holds maps to the index of the next CP that one does, and so does a
        CP inside a double-byte character: the character belongs to the run holding its first byte."""
        k = bisect_right(self.cps, cp) - 1
        if k < 0:
            return 0
        n = (self.idx[k + 1] if k + 1 < len(self.idx) else len(self.text)) - self.idx[k]
        return self.idx[k] + min(-(-(cp - self.cps[k]) // self.width[k]), n)


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


_LEADS = {"cp932": rb"\x81-\x9f\xe0-\xfc", "cp936": rb"\x81-\xfe", "cp949": rb"\x81-\xfe", "cp950": rb"\x81-\xfe"}
_DBCS_RUNS = {c: re.compile(rb"((?:[%s].)+)|([^%s]+)|." % (lead, lead), re.S) for c, lead in _LEADS.items()}


def _fixed(raw: bytes, codec: str, width: int) -> str:
    """raw decoded `width` bytes a character: the whole run at once, else, when the code page does not define one,
    each character alone, U+FFFD for one that is not one character."""
    s = raw.decode(codec, "replace")
    if len(s) * width == len(raw):
        return s
    return "".join(t if len(t := raw[i:i + width].decode(codec, "replace")) == 1 else "�"
                   for i in range(0, len(raw), width))


def _dbcs(raw: bytes, codec: str):
    """(text, width, byte offset) of each run of double-byte (a lead byte and the byte after it) or single-byte
    characters of raw in a double-byte code page; a lead byte without a byte after it is U+FFFD, one byte."""
    for m in _DBCS_RUNS[codec].finditer(raw):
        if m.group(1) is not None:
            yield _fixed(m.group(1), codec, 2), 2, m.start()
        elif m.group(2) is not None:
            yield _fixed(m.group(2), codec, 1), 1, m.start()
        else:
            yield "�", 1, m.start()


def story(word: bytes, pcs: list[Piece], cp0: int, cp1: int, codec: str) -> Story:
    """The Story of CPs [cp0, cp1): compressed pieces one byte a CP in `codec` (compressed_codepage for Word 97,
    old_codec for Word 6, whose double-byte characters are two CPs), the rest UTF-16LE units. Pieces are in CP order;
    the parts are joined once."""
    parts, idx, cps, width, n = [], [], [], [], 0
    dbcs = codec in _DBCS
    for i in range(max(bisect_right(pcs, cp0, key=_cp) - 1, 0), len(pcs)):
        p = pcs[i]
        if p.cp >= cp1:
            break
        a, b = max(cp0, p.cp), min(cp1, p.cp_end)
        if a >= b:
            continue
        if p.compressed:
            raw = bytes(word[p.fc + a - p.cp:p.fc + b - p.cp])
            runs = _dbcs(raw, codec) if dbcs else ((raw.decode(codec, "replace"), 1, 0),)
        else:
            runs = ((_units(word[p.fc + 2 * (a - p.cp):p.fc + 2 * (b - p.cp)]), 1, 0),)
        for s, w, at in runs:
            if s:
                parts.append(s)
                idx.append(n)
                cps.append(a + at)
                width.append(w)
                n += len(s)
    return Story("".join(parts), idx, cps, cp0, cp1, width)


def story_text(word: bytes, pcs: list[Piece], cp0: int, cp1: int, codec: str) -> str:
    """The text of story(); its surrogates are still unpaired units."""
    return story(word, pcs, cp0, cp1, codec).text


# ---- formatting: sprms, FKPs, Prms and the style sheet
_FIXED = (1, 1, 2, 4, 2, 2, 0, 3)  # operand bytes by spra (op >> 13); 6 is variable
_WIDE = (0xD606, 0xD608)           # sprmTDefTable10 and sprmTDefTable: u16 cb, cb - 1 operand bytes
_CHG_TABS = 0xC615                 # sprmPChgTabs: u8 cb, 255 a sentinel


def _chg_tabs(grpprl: bytes, pos: int) -> int | None:
    """The size of a sprmPChgTabs operand of cb 255 at pos: its deleted (4 bytes each) and added (3 bytes each) tab
    counts; None when they do not fit."""
    n = len(grpprl)
    if pos >= n or pos + 1 + 4 * grpprl[pos] >= n:
        return None
    return 2 + 4 * grpprl[pos] + 3 * grpprl[pos + 1 + 4 * grpprl[pos]]


def sprms(grpprl: bytes, old: bool = False) -> list[tuple[int, bytes]]:
    """The (op, operand) pairs of a grpprl; `old` reads Word 6 sprms (_old_sprms). The operand size comes from
    op >> 13 (MS-DOC 2.2.5.1); a variable one is a u8 length, except sprmTDefTable and sprmTDefTable10, whose u16 cb
    counts the operand plus one, and sprmPChgTabs with cb 255 (_chg_tabs). A truncated tail stops the list; every
    sprm moves on by at least 3 bytes."""
    if old:
        return _old_sprms(grpprl)
    out, pos, n = [], 0, len(grpprl)
    while pos + 2 <= n:
        op = grpprl[pos] | grpprl[pos + 1] << 8
        pos += 2
        size = _FIXED[op >> 13]
        if not size:
            if op in _WIDE:
                if pos + 2 > n:
                    break
                size = max(0, (grpprl[pos] | grpprl[pos + 1] << 8) - 1)
                pos += 2
            else:
                if pos >= n:
                    break
                size = grpprl[pos]
                pos += 1
                if op == _CHG_TABS and size == 255:
                    size = _chg_tabs(grpprl, pos)
                    if size is None:
                        break
        if pos + size > n:
            break
        out.append((op, bytes(grpprl[pos:pos + size])))
        pos += size
    return out


# Word 6 sprms: the operand bytes of each one-byte code (_V a u8 length first, _V2 a u16 cb counting the operand
# plus one, _TABS sprmPChgTabs); a code not listed stops the grpprl, as its size is unknown
_V, _V2, _TABS = -1, -2, -3
_OLD_SIZES = {
    2: 2, 3: _V, **dict.fromkeys(range(4, 12), 1), 12: _V, 13: 1, 14: 1, 15: _V, **dict.fromkeys(range(16, 20), 2),
    20: 4, 21: 2, 22: 2, 23: _TABS, 24: 1, 25: 1, **dict.fromkeys(range(26, 29), 2), 29: 1,
    **dict.fromkeys(range(30, 37), 2), 37: 1, **dict.fromkeys(range(38, 44), 2), 44: 1,
    **dict.fromkeys(range(45, 50), 2), **dict.fromkeys(range(50, 59), 1), 59: 2, 60: 2, 61: 1,
    65: 1, 66: 1, 67: 1, 68: _V, 69: 2, 70: 4, 71: 1, 72: 2, 73: 3, 74: _V, 75: 1, 80: 2, 81: _V, 82: _V, 83: 0,
    **dict.fromkeys(range(85, 93), 1), 93: 2, 94: 1, 95: 3, 96: 2, 97: 2, 98: 1, 99: 2, 100: 1, 101: 2, 102: 1,
    103: _V, 104: 1, 105: _V, 106: _V, 107: 2, 108: _V, 109: 2, 110: 2, 117: 1, 118: 1, 119: 1, 120: 12,
    **dict.fromkeys(range(121, 125), 2),
    131: 1, 132: 1, 133: _V, 136: 3, 137: 3, 138: 1, 139: 1, 140: 2, 141: 2, 142: 1, 143: 1, 144: 2, 145: 2, 146: 1,
    147: 1, 148: 2, 149: 2, 150: 1, 151: 1, 152: 1, 153: 1, 154: 2, 155: 2, 156: 2, 157: 2, 158: 1, 159: 1, 160: 2,
    161: 2, 162: 1, 163: 1, **dict.fromkeys(range(164, 172), 2),
    182: 2, 183: 2, 184: 2, 185: 1, 186: 1, 187: 12, 188: _V2, 189: 2, 190: _V2, 191: _V, 192: 4, 193: 5, 194: 4,
    195: 2, 196: 4, 197: 2, 198: 2, 199: 5, 200: 4,
}
# the Word 6 sprms this reader reads -> the Word 97 sprm of the same meaning and operand (sprmTDefTable's operand
# is rebuilt, _tdef6)
_OLD_SPRMS = {2: 0x4600, 24: 0x2416, 25: 0x2417, 65: 0x0800, 66: 0x0801, 68: 0x6A03, 69: 0x4804, 75: 0x080A,
              80: 0x4A30, 85: 0x0835, 92: 0x083C, 117: 0x0855, 190: 0xD608}
_OLD_DEF_TABLE = 190
_TC6 = 10          # bytes of a Word 6 TC: rgf u16, then four 2-byte BRCs
_TC6_GRF = 0x01E3  # the TC bits a TCGRF reads the same: fFirstMerged and fMerged (horzMerge), fVertMerge and
                   # fVertRestart (vertMerge), vertAlign


def _tdef6(arg: bytes) -> bytes:
    """A Word 6 sprmTDefTable operand as a Word 97 one: itcMac and the cell boundaries kept, each whole 10-byte TC
    widened to a 20-byte TC80 holding its TCGRF bits."""
    if not arg or len(arg) < 1 + 2 * (arg[0] + 1):
        return arg
    head = 1 + 2 * (arg[0] + 1)
    tcs = [struct.unpack_from("<H", arg, at)[0] & _TC6_GRF
           for at in range(head, head + _TC6 * min(arg[0], (len(arg) - head) // _TC6), _TC6)]
    return arg[:head] + b"".join(struct.pack("<H", g) + bytes(TC80 - 2) for g in tcs)


def _old_sprms(grpprl: bytes) -> list[tuple[int, bytes]]:
    """The (op, operand) pairs of a Word 6 grpprl, each op its _OLD_SPRMS Word 97 sprm, else the Word 6 code (below
    256, no Word 97 sprm this reader reads). A code without a known size or a truncated tail stops the list; every
    sprm moves on by at least one byte."""
    out, pos, n = [], 0, len(grpprl)
    while pos < n:
        op = grpprl[pos]
        pos += 1
        kind = _OLD_SIZES.get(op)
        if kind is None:
            break
        size = kind
        if kind == _V2:
            if pos + 2 > n:
                break
            size = max(0, (grpprl[pos] | grpprl[pos + 1] << 8) - 1)
            pos += 2
        elif kind < 0:
            if pos >= n:
                break
            size = grpprl[pos]
            pos += 1
            if kind == _TABS and size == 255:
                size = _chg_tabs(grpprl, pos)
                if size is None:
                    break
        if pos + size > n:
            break
        arg = bytes(grpprl[pos:pos + size])
        pos += size
        out.append((_OLD_SPRMS.get(op, op), _tdef6(arg) if op == _OLD_DEF_TABLE else arg))
    return out


_PAGE = 512
_PN = 0x3FFFFF  # PnFkpChpx / PnFkpPapx: the page number is the low 22 bits


def fkp_runs(word: bytes, bte: bytes, kind: str, ctx, old: Fib | None = None) -> list[tuple[int, int, bytes]]:
    """The (fc0, fc1, data) runs of the FKP pages a PlcBteChpx or PlcBtePapx (`bte`: n + 1 FCs, n PnFkp u32) names;
    `kind` is "chpx" (data: the CHPX grpprl) or "papx" (data: GrpPrlAndIstd, istd u16 then the grpprl). A page is
    512 bytes at (pn & 0x3FFFFF) * 512 in WordDocument, crun at byte 511, then crun + 1 FCs and crun one-byte
    offsets (CHPX) or 13-byte BXs (PAPX); an offset times 2 is where the CHPX (cb u8, grpprl) or PapxInFkp (cb u8:
    2cb - 1 bytes, or 0 then cb' u8: 2cb' bytes) starts, 0 being no properties. A page named twice is read once.
    With `old`, the Word 6 FIB, the PNs are u16, a BX is 7 bytes and a PAPX cw u8 then 2cw bytes; a PlcBte naming
    fewer pages than cpnBte stands for that many pages from pnFirst on (Word 6 writes the PlcBte short). A BTE
    whose size is not 4 + 8n (4 + 6n), a page past the stream, a crun whose tables pass byte 511, and a run with
    decreasing FCs or whose offset points into the tables or past byte 511 are skipped and counted under styles;
    so are the entries of a BTE naming more pages than WordDocument holds, past that many."""
    if not bte:
        return []
    papx = kind == "papx"
    each = 6 if old else 8
    if len(bte) < 4 + each or (len(bte) - 4) % each:
        ctx.skipped["styles"] += 1
        return []
    n = (len(bte) - 4) // each
    pns = struct.unpack_from("<%d%s" % (n, "H" if old else "I"), bte, 4 * (n + 1))
    if old:
        first, cpn = old.pn_first[papx], old.cpn[papx]
        if n < cpn and first:
            pns = range(first, first + cpn)
    k = min(len(pns), len(word) // _PAGE)  # every page is a different 512 bytes of WordDocument
    if k < len(pns):
        ctx.skipped["styles"] += 1
    width = (7 if old else 13) if papx else 1
    out, seen = [], set()
    for pn in pns[:k]:
        pn &= _PN
        if pn in seen:
            continue
        seen.add(pn)
        at = pn * _PAGE
        if at + _PAGE > len(word):
            ctx.skipped["styles"] += 1
            continue
        page = word[at:at + _PAGE]
        crun = page[511]
        head = 4 * (crun + 1) + width * crun
        if head > 511:
            ctx.skipped["styles"] += 1
            continue
        fcs = struct.unpack_from("<%dI" % (crun + 1), page)
        for j in range(crun):
            f0, f1 = fcs[j], fcs[j + 1]
            if f1 <= f0:
                if f1 < f0:
                    ctx.skipped["styles"] += 1
                continue
            off = page[4 * (crun + 1) + width * j] * 2
            if not off:
                out.append((f0, f1, b""))
                continue
            if off < head:
                ctx.skipped["styles"] += 1
                continue
            cb = page[off]
            if not papx:
                start, size = off + 1, cb
            elif old:
                start, size = off + 1, 2 * cb
            elif cb:
                start, size = off + 1, 2 * cb - 1
            else:
                start, size = off + 2, 2 * page[off + 1]
            if start + size > 511 or (papx and size < 2):
                ctx.skipped["styles"] += 1
                continue
            out.append((f0, f1, bytes(page[start:start + size])))
    return out


def cp_runs(runs: list[tuple[int, int, bytes]], pcs: list[Piece]) -> list[tuple[int, int, bytes, int]]:
    """FKP runs in FCs as (cp0, cp1, data, prm) runs over the pieces (in CP order, as fit() leaves them): each piece's
    FC range is intersected with the runs, so a fast-saved file whose pieces are out of FC order maps right, and
    the CPs a piece holds that no run covers are runs without data; every run carries its piece's Prm. A character
    belongs to the run holding its first byte. Overlapping runs are cut to the first. O((runs + pieces) log runs)
    plus the runs inside each piece, which never share bytes in a well-formed file."""
    flat, end = [], -1
    for f0, f1, data in sorted(runs, key=lambda r: r[0]):
        f0 = max(f0, end)
        if f0 < f1:
            flat.append((f0, f1, data))
            end = f1
    starts = [r[0] for r in flat]
    out = []
    for p in pcs:
        w = 1 if p.compressed else 2
        lo, hi, cp = p.fc, p.fc + (p.cp_end - p.cp) * w, p.cp
        i = max(bisect_right(starts, lo) - 1, 0)
        while i < len(flat) and flat[i][0] < hi:
            f0, f1, data = flat[i]
            i += 1
            c0 = p.cp - (lo - max(f0, lo)) // w  # ceil: the first character starting at or after f0
            c1 = p.cp - (lo - min(f1, hi)) // w
            if c1 <= max(c0, cp):
                continue
            if c0 > cp:
                out.append((cp, c0, b"", p.prm))
            out.append((max(c0, cp), c1, data, p.prm))
            cp = c1
        if cp < p.cp_end:
            out.append((cp, p.cp_end, b"", p.prm))
    return out


# MS-DOC 2.2.5.4 Prm0: the isprm of a one-sprm Prm -> its sprm, for the sprms this reader reads (0x0C, 0x18, 0x19,
# 0x55 and 0x78 as anydoc has them)
_ISPRM = {0x0C: 0x260A, 0x18: 0x2416, 0x19: 0x2417, 0x41: 0x0800, 0x4B: 0x080A, 0x55: 0x0835, 0x5C: 0x083C,
          0x75: 0x0855, 0x78: 0x2640}


def prm_sprms(prm: int, prc: list[bytes], old: bool = False) -> list[tuple[int, bytes]] | None:
    """The sprms a piece's Prm adds: bit 0 clear is Prm0, isprm (bits 1-7) and a one-byte val (bits 8-15); bit 0 set
    is Prm1, igrpprl (bits 1-15) indexing the Clx Prc list, its sprms cut to last_sprms. An `old` (Word 6) Prm0's
    isprm is the sprm itself, read when its operand is one byte; its Prc grpprls are Word 6 sprms. None when the
    index is past the list."""
    if prm & 1:
        k = prm >> 1
        return last_sprms(sprms(prc[k], old)) if k < len(prc) else None
    isprm = prm >> 1 & 0x7F
    if old:
        op = _OLD_SPRMS.get(isprm) if _OLD_SIZES.get(isprm) == 1 else None
    else:
        op = _ISPRM.get(isprm)
    return [(op, bytes((prm >> 8,)))] if op else []


def huge_papx(data: bytes, at: int) -> bytes | None:
    """The grpprl of the PrcData a sprmPHugePapx points at in the Data stream: u16 cb, then cb bytes; None when it
    is not all inside the stream."""
    if at + 2 > len(data):
        return None
    end = at + 2 + struct.unpack_from("<H", data, at)[0]
    return data[at + 2:end] if end <= len(data) else None


@dataclass(slots=True)
class RunProps:
    """Character properties: bold, vanish (hidden), deleted and inserted (tracked changes; rmark and rmark_del are
    their authors' indices in SttbfRMark), special (fSpec: \\x01 and \\x08 are objects), pic (PicLocation: a Data
    stream offset), ole (fOle2) and obj (ObjLocation)."""
    bold: bool = False
    vanish: bool = False
    deleted: bool = False
    special: bool = False
    pic: int | None = None
    ole: bool = False
    obj: int | None = None
    inserted: bool = False
    rmark: int | None = None
    rmark_del: int | None = None


@dataclass(slots=True)
class ParaProps:
    """Paragraph properties: the style, the outline level (0-8, 9 body text), the table marks (in table, TTP, inner
    cell and row mark, itap depth), the list (ilvl, ilfo) and the TDefTable operand."""
    istd: int = 0
    outline: int = 9
    in_table: bool = False
    ttp: bool = False
    inner_cell: bool = False
    inner_row: bool = False
    itap: int = 0
    ilvl: int = 0
    ilfo: int = 0
    tdef: bytes | None = None


def _toggle(arg: bytes, now: bool, style: bool) -> bool:
    """A ToggleOperand: 0 off, 1 on, 0x80 the style's value, 0x81 its opposite; anything else leaves it."""
    v = arg[0]
    return False if v == 0 else True if v == 1 else style if v == 0x80 else (not style) if v == 0x81 else now


def _u8(arg: bytes) -> int:
    return arg[0]


def _flag(arg: bytes) -> bool:
    return arg[0] != 0


def _uint(arg: bytes) -> int:
    return int.from_bytes(arg, "little")


def _depth(arg: bytes) -> int:
    return max(0, int.from_bytes(arg, "little", signed=True))


# The sprms the reader handles, each table the only place its handler looks an op up, so _READ (what last_sprms
# keeps) is derived from them and cannot drift: a character toggle (ToggleOperand) -> its RunProps field; a
# character value (u32) -> its field; a paragraph sprm -> (its ParaProps field, operand -> value).
RUN_TOGGLES = {0x0835: "bold", 0x083C: "vanish", 0x0800: "deleted", 0x0801: "inserted", 0x0855: "special",
               0x080A: "ole"}
RUN_VALUES = {0x6A03: "pic", 0x680E: "obj", 0x4804: "rmark", 0x4863: "rmark_del"}
PARA_SPRMS = {0x2640: ("outline", _u8), 0x2416: ("in_table", _flag), 0x2417: ("ttp", _flag),
              0x244B: ("inner_cell", _flag), 0x244C: ("inner_row", _flag), 0x6649: ("itap", _depth),
              0x260A: ("ilvl", _u8), 0x460B: ("ilfo", _uint), 0xD608: ("tdef", bytes)}


def _apply_run(props: RunProps, ops, style: RunProps) -> RunProps:
    """props with the character sprms `ops` applied, toggles against `style`."""
    p = replace(props)
    for op, arg in ops:
        name = RUN_TOGGLES.get(op)
        if name is not None:
            setattr(p, name, _toggle(arg, getattr(p, name), getattr(style, name)))
        elif op in RUN_VALUES:
            setattr(p, RUN_VALUES[op], _uint(arg))
    return p


def _apply_para(props: ParaProps, ops) -> ParaProps:
    """props with the paragraph sprms `ops` applied; sprmPIstd and sprmPHugePapx are the caller's."""
    p = replace(props)
    for op, arg in ops:
        h = PARA_SPRMS.get(op)
        if h is not None:
            setattr(p, h[0], h[1](arg))
    return p


ISTD_NIL = 0x0FFF   # no base style
STYLE_DEPTH = 32    # istdBase links one style chain may follow
_CHAR_ISTD, _PARA_ISTD = 0x4A30, 0x4600  # sprmCIstd (character style), sprmPIstd (paragraph style): Styles reads them
_HUGE_PAPX = 0x6646  # sprmPHugePapx: doc.py replaces it by the grpprl it points at
_TOGGLES = frozenset(RUN_TOGGLES)
_READ = _TOGGLES | frozenset(RUN_VALUES) | frozenset(PARA_SPRMS) | {_CHAR_ISTD, _PARA_ISTD, _HUGE_PAPX}


def last_sprms(ops: list) -> list:
    """The sprms of `ops` this reader reads, only the last of each op. Every property they set is its own and is set
    outright (a toggle against a style value that does not change while one grpprl applies), so the last one decides
    it; a toggle operand other than 0, 1, 0x80 and 0x81 changes nothing and is dropped first. A grpprl of up to
    64 KiB (a Prc, a huge PAPX, a style's UPX) so becomes at most len(_READ) sprms, however often it is applied."""
    last = {}
    for op, arg in ops:
        if op in _READ and (op not in _TOGGLES or arg[0] in (0, 1, 0x80, 0x81)):
            last[op] = arg
    return list(last.items())


@dataclass(slots=True)
class _Std:
    sti: int
    base: int
    name: str
    papx: list  # the sprms of the UpxPapx after its istd
    chpx: list  # the sprms of the UpxChpx


class Styles:
    """The style sheet (STSH) and the properties its istdBase chains give. `bad` counts the damage met: a cut STSH or
    STD, a chain cut at STYLE_DEPTH links or by a cycle; the caller adds it to ctx.skipped["styles"]."""

    def __init__(self, stsh: bytes = b"", old: bool = False, codec: str = "cp1252"):
        self.stds: dict[int, _Std] = {}
        self.bad = 0
        self.old, self.codec = old, codec  # a Word 6 STSH: Word 6 sprms, names 8-bit in codec
        self._chains: dict[int, list] = {}
        self._runs: dict[tuple, RunProps] = {}
        self._paras: dict[int, ParaProps] = {}
        if stsh:
            self._read(stsh)

    def _read(self, stsh: bytes):
        """cbStshi u16 and the Stshi (cstd u16, cbSTDBaseInFile u16: 10 or more, 8 for Word 6, ...), then cstd
        LPStds (cbStd u16, the STD; 0 is an empty slot), all inside the STSH's own lcb."""
        n = len(stsh)
        if n < 6:
            self.bad += 1
            return
        cb, cstd, base = struct.unpack_from("<3H", stsh)
        if cb < 4 or 2 + cb > n or base < (8 if self.old else 10):
            self.bad += 1
            return
        pos = 2 + cb
        for istd in range(cstd):
            if pos + 2 > n:
                self.bad += 1
                break
            size = struct.unpack_from("<H", stsh, pos)[0]
            pos += 2
            if not size:
                continue
            if pos + size > n:
                self.bad += 1
                break
            std = self._std(stsh[pos:pos + size], base)
            pos += size
            if std is not None:
                self.stds[istd] = std

    def _std(self, raw: bytes, base: int) -> "_Std | None":
        """One STD: StdfBase (sti in the low 12 bits; stk and istdBase; cupx), the rest of the cbSTDBaseInFile-byte
        Stdf, the name (u16 count, UTF-16, u16 0; Word 6: u8 count, 8-bit, a 0 byte), then cupx UPXs (cbUpx u16 and
        its bytes), each from an even offset. A paragraph style (stk 1) has UpxPapx (istd + grpprl) and UpxChpx; a
        character style (2) UpxChpx; a table style (3) UpxTapx, UpxPapx and UpxChpx; a numbering style (4) UpxPapx.
        A cut STD is counted: its name cut, it is left out; its UPXs cut, it keeps those read."""
        n = len(raw)
        if n < base + 2:
            self.bad += 1
            return None
        w0, w1, w2 = struct.unpack_from("<3H", raw)
        stk, cupx = w1 & 0xF, w2 & 0xF
        if self.old:
            at = base + 1 + raw[base]
            name = raw[base + 1:at].decode(self.codec, "replace")
            at += 1
        else:
            at = base + 2 + 2 * struct.unpack_from("<H", raw, base)[0]
            name = raw[base + 2:at].decode("utf-16-le", "replace")
            at += 2
        if at > n:
            self.bad += 1
            return None
        upx = []
        for _ in range(cupx):
            at += at & 1
            end = at + 2 + struct.unpack_from("<H", raw, at)[0] if at + 2 <= n else n + 1
            if end > n:
                self.bad += 1
                break
            upx.append(raw[at + 2:end])
            at = end
        papx_at = {1: 0, 3: 1, 4: 0}.get(stk)
        chpx_at = {1: 1, 2: 0, 3: 2}.get(stk)
        papx = upx[papx_at][2:] if papx_at is not None and papx_at < len(upx) else b""
        chpx = upx[chpx_at] if chpx_at is not None and chpx_at < len(upx) else b""
        return _Std(w0 & 0xFFF, w1 >> 4, name, last_sprms(sprms(papx, self.old)), last_sprms(sprms(chpx, self.old)))

    def chain(self, istd: int) -> list:
        """The STDs from the root of istd's istdBase chain down to istd's own: at most STYLE_DEPTH, a cycle cut
        where it closes; both counted."""
        hit = self._chains.get(istd)
        if hit is not None:
            return hit
        out, seen, cur = [], set(), istd
        while cur != ISTD_NIL and cur in self.stds:
            if cur in seen or len(out) == STYLE_DEPTH:
                self.bad += 1
                break
            seen.add(cur)
            out.append(self.stds[cur])
            cur = out[-1].base
        out.reverse()
        self._chains[istd] = out
        return out

    def heading(self, istd: int) -> int:
        """1-9 for a style whose sti is 1-9 (the built-in "heading 1" to "heading 9"), else 0."""
        s = self.stds.get(istd)
        return s.sti if s is not None and 1 <= s.sti <= 9 else 0

    def _style_run(self, istd: int, cstyle: int | None) -> RunProps:
        """The character properties of paragraph style istd, then of character style cstyle on top, each chain
        applied from its root, a style's toggles against what it is based on."""
        key = (istd, cstyle)
        hit = self._runs.get(key)
        if hit is None:
            hit = RunProps()
            for s in self.chain(istd) + (self.chain(cstyle) if cstyle is not None else []):
                hit = _apply_run(hit, s.chpx, hit)
            self._runs[key] = hit
        return hit

    def run(self, istd: int, ops: list) -> RunProps:
        """A run's properties: paragraph style istd's, the character style the run's sprmCIstd names, then the run's
        own sprms, whose toggles 128 and 129 take the value of those styles and its opposite."""
        cstyle = None
        for op, arg in ops:
            if op == _CHAR_ISTD and len(arg) == 2:
                cstyle = arg[0] | arg[1] << 8
        style = self._style_run(istd, cstyle)
        return _apply_run(style, ops, style) if ops else style

    def para(self, istd: int, ops: list) -> ParaProps:
        """A paragraph's properties: the style (istd, or the last sprmPIstd among `ops`) resolved down its chain,
        then `ops`."""
        for op, arg in ops:
            if op == _PARA_ISTD and len(arg) == 2:
                istd = arg[0] | arg[1] << 8
        base = self._paras.get(istd)
        if base is None:
            base = ParaProps()
            for s in self.chain(istd):
                base = _apply_para(base, s.papx)
            base.istd = istd
            self._paras[istd] = base
        return _apply_para(base, ops) if ops else base


# ---- tables: the TDefTable operand
TC80 = 20  # bytes of one TC80 cell definition; its first u16 is the TCGRF (MS-DOC 2.9.328)
MAX_CELLS = 63  # cells one row may define


def table_def(arg: bytes) -> tuple[list[int], list[int]] | None:
    """A sprmTDefTable operand after its cb (MS-DOC 2.9.307 TDefTableOperand): itcMac u8, itcMac + 1 i16 cell
    boundaries (rgdxaCenter), then up to itcMac 20-byte TC80s. Returns the boundaries and each cell's TCGRF, 0 for a
    cell whose TC80 the operand does not hold whole; None when itcMac is 0 or past 63, or the boundaries do not fit
    the operand or do not increase."""
    if not arg:
        return None
    k = arg[0]
    head = 1 + 2 * (k + 1)
    if not 0 < k <= MAX_CELLS or len(arg) < head:
        return None
    edges = list(struct.unpack_from("<%dh" % (k + 1), arg, 1))
    if any(a >= b for a, b in zip(edges, edges[1:])):
        return None
    have = min(k, (len(arg) - head) // TC80)
    return edges, [struct.unpack_from("<H", arg, head + TC80 * i)[0] for i in range(have)] + [0] * (k - have)


# ---- lists: PlfLst, its LVLs, PlfLfo and its LFOData
PLF_LST, PLF_LFO = 73, 74  # FibRgFcLcb97 indices of the list definitions and of their overrides
LEVELS = 9
MAX_MARKER = 64  # characters of one marker
_LSTF, _LVLF, _LFO, _LFOLVL = 28, 28, 16, 8  # fixed sizes of an LSTF, LVLF, LFO and LFOLVL
_XST = 256       # number-text units read: rgbxchNums offsets are u8, and a marker is cut to MAX_MARKER anyway
_BULLET, _NO_NUMBER = 23, 255  # nfc of a bullet level (its number text is the bullet) and of a level without one
_SUPPRESSED = 0xF801  # an ilfo that takes the paragraph out of its style's list
_NFC = {0: "DIGIT", 1: "ROMAN_CAPITAL", 2: "ROMAN_SMALL", 3: "LATIN_CAPITAL", 4: "LATIN_SMALL"}  # hancom.number


@dataclass(slots=True)
class Level:
    """One list level: its iStartAt, nfc, and number text as literal str parts and placeholder level indices."""
    start: int
    nfc: int
    text: tuple


def _lvl(table: bytes, pos: int) -> tuple[Level, int, int] | None:
    """The LVL at table[pos:] (MS-DOC 2.9.150): the 28-byte LVLF (iStartAt i32, nfc u8, flags, rgbxchNums 9 u8 at 6,
    ixchFollow, dxaIndentSav, unused, cbGrpprlChpx u8 at 24, cbGrpprlPapx u8 at 25, ilvlRestartLim, grfhic), then
    grpprlPapx and grpprlChpx, then the number text xst (u16 count, UTF-16 units). rgbxchNums are the 1-based offsets
    of the placeholders in xst, zero-terminated unless all nine are used; a placeholder unit 0-8 is that level's
    number. Returns the Level, where the LVL ends, and 1 when an offset points past xst (read as no placeholder), else
    0; None when the LVL does not fit the stream. Control characters in the number text are left out."""
    n = len(table)
    if pos + _LVLF + 2 > n:
        return None
    start, nfc = struct.unpack_from("<iB", table, pos)
    nums = table[pos + 6:pos + 15].split(b"\0", 1)[0]
    at = pos + _LVLF + table[pos + 24] + table[pos + 25]
    if at + 2 > n:
        return None
    cch = struct.unpack_from("<H", table, at)[0]
    end = at + 2 + 2 * cch
    if end > n:
        return None
    places = {k - 1 for k in nums}
    parts, lit = [], []
    for i, ch in enumerate(_units(table[at + 2:at + 2 + 2 * min(cch, _XST)])):
        if i in places and ord(ch) < LEVELS:
            if lit:
                parts.append("".join(lit))
                lit = []
            parts.append(ord(ch))
        elif ch >= " ":
            lit.append(ch)
    if lit:
        parts.append("".join(lit))
    text = tuple(p.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace") if isinstance(p, str) else p
                 for p in parts)
    return Level(start, nfc, text), end, int(any(k > cch for k in nums))


class Lists:
    """The list definitions (PlfLst, MS-DOC 2.9.191: cLst i16, then 28-byte LSTFs, lsid i32 and fSimpleList bit 0 of
    byte 26; each list's LVLs, one for a simple list else nine, follow the PlfLst past its lcb) and their overrides
    (PlfLfo 2.9.183: lfoMac u32, 16-byte LFOs, lsid i32 and clfolvl u8 at 12; then an LFOData for each LFO, cp u32
    and clfolvl 8-byte LFOLVLs, iStartAt i32 then iLvl bits 0-3, fStartAt 0x10 and fFormatting 0x20, an LVL following
    when fFormatting is set). A start-at override (fStartAt) starts at the LFOLVL's iStartAt, or at that LVL's
    iStartAt when fFormatting is set too: MS-DOC says iStartAt is then undefined and MUST be ignored, and anydoc
    takes the LVL's. The LSTF count is bounded by the PlfLst's lcb, the LFO count and the LFOData by the
    table stream, and every LVL walk moves on; what does not fit is left out and counted in `bad`, which the caller
    adds to ctx.skipped["numbering"]. Logic follows anydoc formats/doc/lists.rs (numbers shared per lsid, start-at
    overrides on first use) and MS-DOC "Determining Level Number of a Paragraph"."""

    def __init__(self, table: bytes = b"", plf_lst: tuple[int, int] = (0, 0), plf_lfo: tuple[int, int] = (0, 0)):
        self.bad = 0
        self.lists: dict[int, list[Level]] = {}       # lsid -> its levels
        self.lfos: list[tuple[int, dict, dict]] = []  # ilfo - 1 -> (lsid, {ilvl: start-at}, {ilvl: Level})
        self.counters: dict[int, dict[int, int]] = {}  # lsid -> {ilvl: the number last given}, nine at most
        self.used: set[tuple[int, int]] = set()      # (ilfo, ilvl) whose start-at override has applied
        if plf_lst[1]:
            self._read_lists(table, *plf_lst)
        if plf_lfo[1]:
            self._read_lfos(table, *plf_lfo)

    def _read_lists(self, table, fc, lcb):
        n = len(table)
        if lcb < 2 or fc + lcb > n:
            self.bad += 1
            return
        clst = struct.unpack_from("<h", table, fc)[0]
        k = min(max(clst, 0), (lcb - 2) // _LSTF)
        if k != clst:
            self.bad += 1
        heads = [(struct.unpack_from("<i", table, at)[0], table[at + 26] & 1)
                 for at in range(fc + 2, fc + 2 + _LSTF * k, _LSTF)]
        pos = fc + lcb
        for lsid, simple in heads:
            levels = []
            for _ in range(1 if simple else LEVELS):
                got = _lvl(table, pos)
                if got is None:  # the LVLs after this one cannot be found
                    self.bad += 1
                    return
                level, pos, off = got
                self.bad += off
                levels.append(level)
            self.lists.setdefault(lsid, levels)

    def _read_lfos(self, table, fc, lcb):
        n = len(table)
        if lcb < 4 or fc + lcb > n:
            self.bad += 1
            return
        mac = struct.unpack_from("<I", table, fc)[0]
        k = min(mac, (n - fc - 4) // _LFO)
        if k < mac:
            self.bad += 1
        heads = [(struct.unpack_from("<i", table, at)[0], table[at + 12])
                 for at in range(fc + 4, fc + 4 + _LFO * k, _LFO)]
        pos, ok = fc + 4 + _LFO * k, True
        for lsid, count in heads:
            starts, formats = {}, {}
            if ok and pos + 4 > n:
                ok = False
                self.bad += 1
            if ok:
                pos += 4  # cp: where the list's first paragraph is, or 0xFFFFFFFF
                for _ in range(count):
                    if pos + _LFOLVL > n:
                        ok = False
                        self.bad += 1
                        break
                    at, bits = struct.unpack_from("<iI", table, pos)
                    pos += _LFOLVL
                    ilvl = bits & 0xF
                    if bits & 0x20:
                        got = _lvl(table, pos)
                        if got is None:
                            ok = False
                            self.bad += 1
                            break
                        level, pos, off = got
                        self.bad += off
                        if ilvl < LEVELS:
                            formats[ilvl] = level
                    if bits & 0x10 and ilvl < LEVELS:  # with fFormatting the LFOLVL's iStartAt is undefined
                        starts[ilvl] = level.start if bits & 0x20 else at
            self.lfos.append((lsid, starts, formats))

    def marker(self, ilfo: int, ilvl: int) -> str | None:
        """The marker of the next paragraph in list `ilfo` (1-based into the LFOs; 0 is no list, 0xF801 suppressed)
        at level `ilvl` (0-8, a larger one the last level; a simple list has level 0 only), its level being the LFO's
        override LVL or the list's. The numbers are shared by every LFO naming the same lsid: the level's number goes
        up by one from iStartAt, or is the LFO's start-at override the first time that LFO numbers the level, and the
        deeper levels start again. A bullet level (nfc 23) is "-" and a level without numbers (nfc 255) "", neither
        numbered; None when ilfo names no list read. At most MAX_MARKER characters."""
        if ilfo == _SUPPRESSED or not 0 < ilfo <= len(self.lfos):
            return None
        lsid, starts, formats = self.lfos[ilfo - 1]
        levels = self.lists.get(lsid)
        if levels is None:
            return None
        ilvl = min(ilvl, len(levels) - 1)
        level = formats.get(ilvl) or levels[ilvl]
        cnt = self.counters.setdefault(lsid, {})
        for k in [k for k in cnt if k > ilvl]:
            del cnt[k]
        if level.nfc in (_BULLET, _NO_NUMBER):
            return "-" if level.nfc == _BULLET else ""
        if ilvl in starts and (ilfo, ilvl) not in self.used:
            self.used.add((ilfo, ilvl))
            cnt[ilvl] = starts[ilvl]
        else:
            cnt[ilvl] = cnt.get(ilvl, level.start - 1) + 1
        out = []
        for part in level.text:
            if isinstance(part, str):
                out.append(part)
            else:
                lv = formats.get(part) or (levels[part] if part < len(levels) else level)
                out.append(number(cnt.get(part, lv.start), _NFC.get(lv.nfc, "DIGIT")))
        return "".join(out)[:MAX_MARKER].strip()


# ---- property sets (MS-OLEPS 2.21 PropertySetStream, 2.20 PropertySet, 2.15 TypedPropertyValue, 2.17 Dictionary)
SUMMARY_STREAM, DOC_SUMMARY_STREAM = "\x05SummaryInformation", "\x05DocumentSummaryInformation"
_SUMMARY = bytes.fromhex("e0859ff2f94f6810ab9108002b27b3d9")      # FMTID_SummaryInformation
_DOC_SUMMARY = bytes.fromhex("02d5cdd59c2e1b10939708002b2cf9ae")  # FMTID_DocSummaryInformation
_USER = bytes.fromhex("05d5cdd59c2e1b10939708002b2cf9ae")         # FMTID_UserDefinedProperties
_SUMMARY_LABELS = {2: "title", 3: "subject", 4: "author", 5: "keywords", 8: "last author", 12: "created",
                   13: "modified"}
_DOC_SUMMARY_LABELS = {15: "company"}
_EPOCH = datetime.datetime(1601, 1, 1)


def _codec(cp: int) -> str:
    """The Python codec of a property set's CodePage: 1200 is UTF-16LE, an unknown one cp1252."""
    name = "utf-16-le" if cp == 1200 else "utf-8" if cp == 65001 else f"cp{cp}"
    try:
        codecs.lookup(name)
    except LookupError:
        return "cp1252"
    return name


def _filetime(ticks: int) -> str | None:
    """A FILETIME (100 ns ticks since 1601) as `YYYY-MM-DD HH:MM`; None for 0 or past year 9999."""
    try:
        return (_EPOCH + datetime.timedelta(microseconds=ticks // 10)).strftime("%Y-%m-%d %H:%M") if ticks else None
    except OverflowError:
        return None


def _value(data: bytes, at: int, end: int, codec: str):
    """The TypedPropertyValue at `at`: text for VT_LPSTR (in the set's code page) and VT_LPWSTR, a number for VT_I2,
    VT_I4, VT_UI4 and VT_R8, yes or no for VT_BOOL, a date for VT_FILETIME; None for another type or one that does
    not fit before `end`."""
    if at + 4 > end:
        return None
    vt = struct.unpack_from("<H", data, at)[0]
    at += 4
    fixed = {0x02: "<h", 0x03: "<i", 0x13: "<I", 0x05: "<d", 0x0B: "<h", 0x40: "<Q"}.get(vt)
    if fixed is not None:
        if at + struct.calcsize(fixed) > end:
            return None
        v = struct.unpack_from(fixed, data, at)[0]
        return ("yes" if v else "no") if vt == 0x0B else _filetime(v) if vt == 0x40 else v
    if vt not in (0x1E, 0x1F) or at + 4 > end:
        return None
    n = struct.unpack_from("<I", data, at)[0] * (2 if vt == 0x1F else 1)
    if at + 4 + n > end:
        return None
    raw = data[at + 4:at + 4 + n]
    return raw.decode("utf-16-le" if vt == 0x1F else codec, "replace").split("\0", 1)[0]


def _dictionary(data: bytes, at: int, end: int, codec: str) -> dict:
    """{pid: name} of the Dictionary at `at`: a u32 count, then a pid, a u32 length (characters under code page 1200,
    padded to 4 bytes, else bytes) and the name each; the entries that fit before `end`."""
    out = {}
    if at + 4 > end:
        return out
    count = struct.unpack_from("<I", data, at)[0]
    pos, wide = at + 4, codec == "utf-16-le"
    for _ in range(min(count, (end - pos) // 8)):
        if pos + 8 > end:
            break
        pid, n = struct.unpack_from("<II", data, pos)
        size = n * (2 if wide else 1)
        if pos + 8 + size > end:
            break
        out[pid] = data[pos + 8:pos + 8 + size].decode(codec, "replace").split("\0", 1)[0]
        pos += 8 + size + (-size % 4 if wide else 0)
    return out


def property_sets(data: bytes) -> dict:
    """{FMTID: ({pid: value}, {pid: name})} of a PropertySetStream: its byte order 0xFFFE, one or two sets after the
    28-byte header, each a FMTID and an offset; a set is its size, a count and (pid, offset) pairs, the CodePage at pid
    1 and the Dictionary at pid 0. ValueError when the header or a set does not fit in the stream; a value that does
    not fit is left out. One pass over the stream's bytes."""
    if len(data) < 28 or data[:2] != b"\xfe\xff":
        raise ValueError("not a property set stream")
    n = struct.unpack_from("<I", data, 24)[0]
    if not 0 < n <= 2 or 28 + 20 * n > len(data):
        raise ValueError("property set count")
    out = {}
    for k in range(n):
        fmtid, off = data[28 + 20 * k:44 + 20 * k], struct.unpack_from("<I", data, 44 + 20 * k)[0]
        if off + 8 > len(data):
            raise ValueError("property set outside the stream")
        size, count = struct.unpack_from("<II", data, off)
        end = off + size
        if end > len(data) or size < 8 or count > (size - 8) // 8:
            raise ValueError("property set size")
        pairs = struct.unpack_from("<%dI" % (2 * count), data, off + 8)
        entries = dict(zip(pairs[::2], pairs[1::2]))
        cp = _value(data, off + entries[1], end, "cp1252") if 1 in entries else None
        codec = _codec(cp & 0xFFFF if isinstance(cp, int) else 1252)
        values = {pid: v for pid, rel in entries.items() if pid > 1
                  and (v := _value(data, off + rel, end, codec)) is not None}
        names = _dictionary(data, off + entries[0], end, codec) if 0 in entries else {}
        out[fmtid] = (values, names)
    return out


def summary(streams: dict) -> tuple[str, list]:
    """(title, (label, value) pairs) of the property sets read from SUMMARY_STREAM and DOC_SUMMARY_STREAM
    ({stream: property_sets}): the summary's title, subject, author, keywords, last author and creation and save
    times, the document summary's company, then the user-defined properties by their dictionary names."""
    title, pairs = "", []
    for stream, fmtid, labels in ((SUMMARY_STREAM, _SUMMARY, _SUMMARY_LABELS),
                                  (DOC_SUMMARY_STREAM, _DOC_SUMMARY, _DOC_SUMMARY_LABELS)):
        values = streams.get(stream, {}).get(fmtid, ({}, {}))[0]
        for pid, label in labels.items():
            v = values.get(pid)
            if label == "title":
                title = " ".join(str(v or "").split())[:200]
            elif v is not None:
                pairs.append((label, v))
    values, names = streams.get(DOC_SUMMARY_STREAM, {}).get(_USER, ({}, {}))
    pairs += [(names[pid], v) for pid, v in values.items() if pid in names]
    return title, pairs
