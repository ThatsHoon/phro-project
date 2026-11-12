"""Word 97-2003 binary structures: the FIB, the piece table, the text of a story's CP range, and the formatting:
sprms, CHPX and PAPX FKPs, piece Prms and the style sheet.

Written from MS-DOC (2.5.1 Fib, 2.9.38 Clx, 2.9.177 PlcPcd, 2.9.73 FcCompressed, 2.4.1 retrieving text, 2.2.5 Sprm
and Prm, 2.9.33/2.9.175 ChpxFkp/PapxFkp, 2.9.271 STSH, 2.4.6 applying properties); logic follows dochan
office_binary/doc_binary.py (FIB walk, Clx, piece CPs, cumulative story ranges, decode_grpprl operand sizes, _fkps,
_read_styles) and anydoc formats/doc (Prm0 isprm table, STD walk, toggles against the style's value). Every count
and offset is checked against the stream it is read from.

FibRgFcLcb97 indices this reader uses (MS-DOC 2.5.6; dochan reads all but 73/74 at these indices, anydoc reads 1,
2/3, 12/13, 33, 46/47 and 73/74 at the matching absolute offsets): 1 STSH, 2/3 PlcffndRef/Txt, 4/5
PlcfandRef/Txt, 11 PlcfHdd, 12/13 PlcfBteChpx/Papx, 33 Clx, 36 GrpXstAtnOwners, 40 PlcSpaMom, 46/47
PlcfendRef/Txt, 50 DggInfo, 56 PlcftxbxTxt, 58 PlcfHdrtxbxTxt, 73 PlfLst, 74 PlfLfo.
"""
import struct
from bisect import bisect_right
from dataclasses import dataclass, replace

from ..limits import ReadError
from ..text import codepage

CLX = 33  # the FibRgFcLcb97 index of the Clx
STSH, BTE_CHPX, BTE_PAPX = 1, 12, 13  # FibRgFcLcb97 indices of the style sheet and the two PlcBte
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


def pieces(table: bytes, fc: int, lcb: int) -> list[Piece]:
    """The pieces of the Clx at table[fc:fc + lcb]: Prc records (clxt 1, u16 cb, grpprl) are skipped, then the Pcdt
    (clxt 2, u32 lcb) holds the PlcPcd, n + 1 CPs and n 8-byte PCDs (fc u32 at 2, prm u16 at 6). A Clx outside the
    stream, without a Pcdt, with a PlcPcd size that is not 4 + 12n or with decreasing CPs is damaged."""
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


# ---- formatting: sprms, FKPs, Prms and the style sheet
_FIXED = (1, 1, 2, 4, 2, 2, 0, 3)  # operand bytes by spra (op >> 13); 6 is variable
_WIDE = (0xD606, 0xD608)           # sprmTDefTable10 and sprmTDefTable: u16 cb, cb - 1 operand bytes
_CHG_TABS = 0xC615                 # sprmPChgTabs: u8 cb, 255 a sentinel


def sprms(grpprl: bytes) -> list[tuple[int, bytes]]:
    """The (op, operand) pairs of a grpprl. The operand size comes from op >> 13 (MS-DOC 2.2.5.1); a variable one is
    a u8 length, except sprmTDefTable and sprmTDefTable10, whose u16 cb counts the operand plus one, and
    sprmPChgTabs with cb 255, sized by its deleted (4 bytes each) and added (3 bytes each) tab counts. A truncated
    tail stops the list; every sprm moves on by at least 3 bytes."""
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
                    if pos >= n or pos + 1 + 4 * grpprl[pos] >= n:
                        break
                    size = 2 + 4 * grpprl[pos] + 3 * grpprl[pos + 1 + 4 * grpprl[pos]]
        if pos + size > n:
            break
        out.append((op, bytes(grpprl[pos:pos + size])))
        pos += size
    return out


_PAGE = 512
_PN = 0x3FFFFF  # PnFkpChpx / PnFkpPapx: the page number is the low 22 bits


def fkp_runs(word: bytes, bte: bytes, kind: str, ctx) -> list[tuple[int, int, bytes]]:
    """The (fc0, fc1, data) runs of the FKP pages a PlcBteChpx or PlcBtePapx (`bte`: n + 1 FCs, n PnFkp u32) names;
    `kind` is "chpx" (data: the CHPX grpprl) or "papx" (data: GrpPrlAndIstd, istd u16 then the grpprl). A page is
    512 bytes at (pn & 0x3FFFFF) * 512 in WordDocument, crun at byte 511, then crun + 1 FCs and crun one-byte
    offsets (CHPX) or 13-byte BXs (PAPX); an offset times 2 is where the CHPX (cb u8, grpprl) or PapxInFkp (cb u8:
    2cb - 1 bytes, or 0 then cb' u8: 2cb' bytes) starts, 0 being no properties. A page named twice is read once.
    A BTE whose size is not 4 + 8n, a page past the stream, a crun whose tables pass byte 511, and a run with
    decreasing FCs or whose offset points into the tables or past byte 511 are skipped and counted under styles;
    so are the entries of a BTE naming more pages than WordDocument holds, past that many."""
    if not bte:
        return []
    if len(bte) < 12 or (len(bte) - 4) % 8:
        ctx.skipped["styles"] += 1
        return []
    n = (len(bte) - 4) // 8
    k = min(n, len(word) // _PAGE)  # every page is a different 512 bytes of WordDocument
    if k < n:
        ctx.skipped["styles"] += 1
    papx = kind == "papx"
    width = 13 if papx else 1
    out, seen = [], set()
    for pn in struct.unpack_from("<%dI" % k, bte, 4 * (n + 1)):
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


def prm_sprms(prm: int, prc: list[bytes]) -> list[tuple[int, bytes]] | None:
    """The sprms a piece's Prm adds: bit 0 clear is Prm0, isprm (bits 1-7) and a one-byte val (bits 8-15); bit 0 set
    is Prm1, igrpprl (bits 1-15) indexing the Clx Prc list, its sprms cut to last_sprms. None when that index is past
    the list."""
    if prm & 1:
        k = prm >> 1
        return last_sprms(sprms(prc[k])) if k < len(prc) else None
    op = _ISPRM.get(prm >> 1 & 0x7F)
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
    """Character properties: bold, vanish (hidden), deleted (a tracked deletion), special (fSpec: \\x01 and \\x08
    are objects), pic (PicLocation: a Data stream offset), ole (fOle2) and obj (ObjLocation)."""
    bold: bool = False
    vanish: bool = False
    deleted: bool = False
    special: bool = False
    pic: int | None = None
    ole: bool = False
    obj: int | None = None


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


def _apply_run(props: RunProps, ops, style: RunProps) -> RunProps:
    """props with the character sprms `ops` applied, toggles against `style`."""
    bold, vanish, deleted, special = props.bold, props.vanish, props.deleted, props.special
    pic, ole, obj = props.pic, props.ole, props.obj
    for op, arg in ops:
        if op == 0x0835:
            bold = _toggle(arg, bold, style.bold)
        elif op == 0x083C:
            vanish = _toggle(arg, vanish, style.vanish)
        elif op == 0x0800:
            deleted = _toggle(arg, deleted, style.deleted)
        elif op == 0x0855:
            special = _toggle(arg, special, style.special)
        elif op == 0x080A:
            ole = _toggle(arg, ole, style.ole)
        elif op == 0x6A03:
            pic = int.from_bytes(arg, "little")
        elif op == 0x680E:
            obj = int.from_bytes(arg, "little")
    return RunProps(bold, vanish, deleted, special, pic, ole, obj)


def _apply_para(props: ParaProps, ops) -> ParaProps:
    """props with the paragraph sprms `ops` applied; sprmPIstd and sprmPHugePapx are the caller's."""
    p = replace(props)
    for op, arg in ops:
        if op == 0x2640:
            p.outline = arg[0]
        elif op == 0x2416:
            p.in_table = arg[0] != 0
        elif op == 0x2417:
            p.ttp = arg[0] != 0
        elif op == 0x244B:
            p.inner_cell = arg[0] != 0
        elif op == 0x244C:
            p.inner_row = arg[0] != 0
        elif op == 0x6649:
            p.itap = max(0, int.from_bytes(arg, "little", signed=True))
        elif op == 0x260A:
            p.ilvl = arg[0]
        elif op == 0x460B:
            p.ilfo = int.from_bytes(arg, "little")
        elif op == 0xD608:
            p.tdef = arg
    return p


ISTD_NIL = 0x0FFF   # no base style
STYLE_DEPTH = 32    # istdBase links one style chain may follow
_CHAR_ISTD, _PARA_ISTD = 0x4A30, 0x4600  # sprmCIstd (character style), sprmPIstd (paragraph style)
_TOGGLES = frozenset((0x0835, 0x083C, 0x0800, 0x0855, 0x080A))
# every sprm _apply_run, _apply_para and Styles read; one they come to read must be added here, or last_sprms drops it
_READ = _TOGGLES | frozenset((0x6A03, 0x680E, _CHAR_ISTD, _PARA_ISTD, 0x2640, 0x2416, 0x2417, 0x244B, 0x244C,
                              0x6649, 0x260A, 0x460B, 0xD608, 0x6646))


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

    def __init__(self, stsh: bytes = b""):
        self.stds: dict[int, _Std] = {}
        self.bad = 0
        self._chains: dict[int, list] = {}
        self._runs: dict[tuple, RunProps] = {}
        self._paras: dict[int, ParaProps] = {}
        if stsh:
            self._read(stsh)

    def _read(self, stsh: bytes):
        """cbStshi u16 and the Stshi (cstd u16, cbSTDBaseInFile u16, ...), then cstd LPStds (cbStd u16, the STD; 0 is
        an empty slot), all inside the STSH's own lcb."""
        n = len(stsh)
        if n < 6:
            self.bad += 1
            return
        cb, cstd, base = struct.unpack_from("<3H", stsh)
        if cb < 4 or 2 + cb > n or base < 10:
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
        Stdf, the name (u16 count, UTF-16, u16 0), then cupx UPXs (cbUpx u16 and its bytes), each from an even
        offset. A paragraph style (stk 1) has UpxPapx (istd + grpprl) and UpxChpx; a character style (2) UpxChpx; a
        table style (3) UpxTapx, UpxPapx and UpxChpx; a numbering style (4) UpxPapx. A cut STD is counted: its
        name cut, it is left out; its UPXs cut, it keeps those read."""
        n = len(raw)
        if n < base + 2:
            self.bad += 1
            return None
        w0, w1, w2 = struct.unpack_from("<3H", raw)
        stk, cupx = w1 & 0xF, w2 & 0xF
        at = base + 2 + 2 * struct.unpack_from("<H", raw, base)[0]
        if at > n:
            self.bad += 1
            return None
        name = raw[base + 2:at].decode("utf-16-le", "replace")
        at += 2
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
        return _Std(w0 & 0xFFF, w1 >> 4, name, last_sprms(sprms(papx)), last_sprms(sprms(chpx)))

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
