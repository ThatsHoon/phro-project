# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub 0477b85184e02dc5276582d9cea969208d56c655, src/lib/MSPUBParser.cpp.
"""The Quill stream of a Publisher file (`Quill/QuillSub/CONTENTS`): its bits, stories, table cells and links.

Ported from libmspub (MPL-2.0) at 0477b85184e02dc5276582d9cea969208d56c655, `src/lib/MSPUBParser.cpp`
(`parseQuill`, `parseQuillChunkReference`, `parseCharacterStyles`, `getCharacterStyle`,
`parseTableCellDefinitions`). The bit entry fields and the PLC bits (`PLC ` kind: the count and type first, and
type 12's hyperlink list) follow Apache POI's `hpbf/model/QuillContents.java` and `hpbf/model/qcbits/QCPLCBit.java`
at 47dcff769bb395034dddfd59c4609659b6dbc6a4.

Bits (`bits`). The stream starts `CHNKINK `. At 0x18 is a list: 2 bytes, a u16 count, the u32 offset of the next
list (0xFFFFFFFF ends) and the count's 24-byte entries: a u16, the name (4 bytes), the id u16, 4 bytes, the kind (4
bytes: `PLC ` for the PLC bits, else the name again), the bit's offset and length (u32). The lists are followed as
libmspub does, with a seen-set of list offsets. `bits` gives {name: [Bit]} in directory order; a `Bit` is the bit's
bytes (a bytes subclass) with its `id`, `kind` and stream `offset`. Up to `QUILL_BITS` entries are read and at most
as many lists visited; the bits copy at most the stream's size in all (overlapping bits only come from a damaged
file). Past any of these the rest is ignored, counted `record` once when a `ctx` is given.

Stories (`stories`). TEXT (UTF-16LE; libmspub's last TEXT bit) cut by the STRS lengths (characters; a PLC whose
lengths start 4 bytes past the u32 at +4), in order, keyed by the SYID text ids (a count at +4, the ids from +8);
STRS and SYID bits add up in directory order, and the stories pair them as far as both go. A text id named twice
keeps its last story (libmspub's map). `\\r` ends a paragraph, `\\v` and `\\n` break a line (libmspub breaks on
`\\n`), a tab stays and other control characters are left out; a paragraph with no visible character is left out
and breaks at its ends are dropped. Bold comes from the FDPC bits (u16 count, 6 bytes, the count's u32 run ends as
stream byte offsets past the TEXT bit's start, then the count's u16 style offsets in the bit; a style is a u32
length counting itself and blocks, bold when block id 0x02 is present, as `getCharacterStyle` reads BOLD_1). Only
TEXT, STRS and SYID are needed: without FDPC the text stays and only bold is lost, where libmspub drops the text
(it also waits for FDPP, STSH and FONT, which give nothing this reader keeps).

`Story(id, index, text, paras)`: the SYID text id, the story's place in STRS order (a `TCD ` bit names its table
story by it), the story's characters as stored and its paragraphs, `model.Para`s of `Text`, `Bold` and `Break`. The
Paras are built once per document and shared: a caller copies one before changing it.

Bounds. The FDPC runs and the blocks of their styles (each style read once) tick one `pub_contents.Meter` of
`limits.BIN_RECORDS`; past it bold is lost for the rest, counted `record` once. Each story is charged, before it is
built, to one room of `limits.MAX_OUTPUT`: its characters, and `vsdx.LINE` for itself, each paragraph end or line
break and each bold run in it (each is a model object however short). Past the room the remaining stories are left
out, counted `text` once. A count past its bit reads what the bit holds; an FDPC whose count does not fit its bit is
not read. Every loop is linear in the bytes it reads.

Cells (`cells(bits, story)`). The `TCD ` bit whose id is the story's index: a u32 count less one, then from +0xC the
count's u32 cell ends in characters. Each cell's end is the `\\r` closing it, which is left out, and the next cell
starts past it; the last cell's end is the story's end and its closing `\\r` is dropped. An end before the cell's
start gives an empty cell; at most one cell more than the story's characters is read.

Links (`links(bits)`). Each `PLC ` bit of type 12, read as POI's `QCPLCBit.Type12`: a 0x34-byte bit holds none;
otherwise the count's entries start at 0x4C (one) or 0x68 + 22 per entry past two, each a u16 length and that many
UTF-16 characters, or, when the first character is 0, an empty entry the length in bytes long. A bit naming more
than POI's 1,000 PLCs is not read; an entry past the bit ends that bit (POI then ignores the record). Empty URLs and
repeats are left out; the rest come in directory order.
"""
import bisect
import re
import struct
from dataclasses import dataclass

from .. import limits
from ..model import Bold, Break, Para, Text
from . import pub_contents, vsdx

QUILL_BITS = 1_000   # bit directory entries (and lists) one Quill stream may give; past it the rest ignored

HEAD = b"CHNKINK "
DIRECTORY = 0x18
END = 0xFFFFFFFF
BOLD_1 = 0x02        # MSPUBBlockID.h BOLD_1_ID, in a character style
PLC = "PLC "
LINKS = 12           # the PLC type of a hyperlink list (POI's Type12)
PLCS = 1_000         # POI's MAX_NUMBER_OF_PLCS: a PLC bit naming more is not read

_LIST = struct.Struct("<HI")
_ENTRY = struct.Struct("<H4sHHH4sII")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_CUT = re.compile("[\r\n\x0b]")
_DROP = re.compile("[\x00-\x08\x0c\x0e-\x1f\x7f]")


class Bit(bytes):
    """One Quill bit's bytes, with its directory `id`, its `kind` (the entry's second name) and its stream `offset`."""

    def __new__(cls, raw, id: int = 0, kind: str = "", offset: int = 0):
        bit = super().__new__(cls, raw)
        bit.id, bit.kind, bit.offset = id, kind, offset
        return bit


@dataclass(frozen=True)
class Story:
    """One Quill story (see the module docstring): text id, index in STRS order, stored text, `model.Para`s."""
    id: int
    index: int
    text: str
    paras: tuple


def bits(data, ctx=None) -> dict:
    """{name: [Bit]} of a Quill stream in directory order; {} when it does not start `CHNKINK `."""
    out = {}
    if bytes(data[:len(HEAD)]) != HEAD:
        return out
    n = len(data)
    room = n
    at, seen, lists, entries = DIRECTORY, set(), 0, 0

    def cut():
        if ctx is not None:
            ctx.skipped["record"] += 1
        return out

    while at != END and at not in seen:
        if lists >= QUILL_BITS:
            return cut()
        seen.add(at)
        lists += 1
        if at + 8 > n:
            break
        count, nxt = _LIST.unpack_from(data, at + 2)
        for i in range(min(count, (n - at - 8) // _ENTRY.size)):
            if entries >= QUILL_BITS:
                return cut()
            entries += 1
            _, name, bid, _, _, kind, off, length = _ENTRY.unpack_from(data, at + 8 + i * _ENTRY.size)
            raw = bytes(data[off:off + length]) if off < n else b""
            if len(raw) > room:
                return cut()
            room -= len(raw)
            out.setdefault(name.decode("latin-1"), []).append(Bit(raw, bid, kind.decode("latin-1"), off))
        at = nxt
    return out


def _u32s(bit, start: int, count: int):
    """Up to `count` u32s of `bit` from `start`, as many as the bit holds."""
    k = min(count, (len(bit) - start) // 4)
    return (v for (v,) in _U32.iter_unpack(bit[start:start + 4 * k]))


def _lengths(bitlist):
    for bit in bitlist:
        if len(bit) >= 8:
            count, skip = struct.unpack_from("<II", bit, 0)
            yield from _u32s(bit, 4 + skip, count)


def _ids(bitlist):
    for bit in bitlist:
        if len(bit) >= 8:
            yield from _u32s(bit, 8, _U32.unpack_from(bit, 4)[0])


def _bold_style(bit, at: int, meter) -> bool:
    if at + 4 > len(bit):
        return False
    size = _U32.unpack_from(bit, at)[0]
    if size < 4 or size > len(bit) - at:
        return False
    return any(b.id == BOLD_1 for b in pub_contents.blocks(bit, at + 4, at + size, meter=meter))


def _bold(bitlist, text_at: int, meter) -> list:
    """The bold [start, end) character ranges of the TEXT bit at stream offset `text_at`, sorted and disjoint."""
    out = []
    prev = 0
    for bit in bitlist:
        if len(bit) < 8:
            continue
        count = _U16.unpack_from(bit, 0)[0]
        if 8 + 6 * count > len(bit):
            continue
        styles = {}
        for i in range(count):
            if not meter.tick():
                return out
            last = _U32.unpack_from(bit, 8 + 4 * i)[0]
            at = _U16.unpack_from(bit, 8 + 4 * count + 2 * i)[0]
            end = (last - text_at + 1) // 2
            if end <= prev:
                continue
            bold = styles.get(at)
            if bold is None:
                bold = styles[at] = _bold_style(bit, at, meter)
            if bold:
                if out and out[-1][1] == prev:
                    out[-1][1] = end
                else:
                    out.append([prev, end])
            prev = end
    return out


def _inlines(text: str, start: int, bold: list, j: int) -> tuple:
    """(inlines of `text`, a piece with no breaks at character `start`, the bold ranges' first index still open)."""
    out = []
    pos, stop = start, start + len(text)
    while j < len(bold) and bold[j][1] <= pos:
        j += 1
    k = j
    while pos < stop:
        if k < len(bold) and bold[k][0] < stop:
            s, e = max(bold[k][0], pos), min(bold[k][1], stop)
        else:
            s = e = stop
        for a, b, strong in ((pos, s, False), (s, e, True)):
            piece = _DROP.sub("", text[a - start:b - start])
            if piece:
                out.append(Bold([Text(piece)]) if strong else Text(piece))
        pos = e
        if k < len(bold) and bold[k][1] <= stop:
            k += 1
    return out, j


def _paras(text: str, start: int, bold: list, ends: list) -> tuple:
    paras, inl, pos, j = [], [], 0, bisect.bisect_right(ends, start)

    def close():
        while inl and isinstance(inl[-1], Break):
            inl.pop()
        first = next((i for i, x in enumerate(inl) if not isinstance(x, Break)), len(inl))
        body = inl[first:]
        if any(isinstance(x, Bold) and x.inlines[0].text.strip() or isinstance(x, Text) and x.text.strip()
               for x in body):
            paras.append(Para(body))

    for m in _CUT.finditer(text):
        got, j = _inlines(text[pos:m.start()], start + pos, bold, j)
        inl += got
        if m.group() == "\r":
            close()
            inl = []
        else:
            inl.append(Break())
        pos = m.end()
    got, j = _inlines(text[pos:], start + pos, bold, j)
    inl += got
    close()
    return tuple(paras)


def stories(bits_, ctx=None) -> dict:
    """{text id: Story} (see the module docstring); {} without TEXT, STRS or SYID."""
    texts = bits_.get("TEXT")
    if not texts or "STRS" not in bits_ or "SYID" not in bits_:
        return {}
    raw = texts[-1]
    text = str(memoryview(raw)[:len(raw) // 2 * 2], "utf-16-le", "replace")
    bold = _bold(bits_.get("FDPC", ()), raw.offset, pub_contents.Meter(ctx))
    starts = [b[0] for b in bold]
    ends = [b[1] for b in bold]
    room = limits.MAX_OUTPUT
    line = vsdx.LINE
    out = {}
    pos = 0
    for index, (tid, length) in enumerate(zip(_ids(bits_["SYID"]), _lengths(bits_["STRS"]))):
        start = pos
        pos = min(start + length, len(text))
        runs = bisect.bisect_left(starts, pos) - bisect.bisect_right(ends, start)
        breaks = text.count("\r", start, pos) + text.count("\n", start, pos) + text.count("\x0b", start, pos)
        cost = pos - start + line * (1 + breaks + max(0, runs))
        if cost > room:
            if ctx is not None:
                ctx.skipped["text"] += 1
            break
        room -= cost
        piece = text[start:pos]
        out[tid] = Story(tid, index, piece, _paras(piece, start, bold, ends))
    return out


def cells(bits_, story: Story) -> list:
    """The cell texts of a table story (see the module docstring); [] when no `TCD ` bit names it."""
    bit = next((b for b in bits_.get("TCD ", ()) if b.id == story.index), None)
    if bit is None or len(bit) < 0xC:
        return []
    text = story.text
    out = []
    pos = 0
    for end in _u32s(bit, 0xC, min(_U32.unpack_from(bit, 0)[0] + 1, len(text) + 1)):
        end = max(pos, end)
        cell = text[pos:end]
        out.append(cell[:-1] if cell.endswith("\r") else cell)
        pos = end + 1 if end < len(text) and text[end] == "\r" else end
    return out


def _links(bit) -> list:
    count = _U32.unpack_from(bit, 0)[0]
    if count > PLCS:
        return []
    at = 0x4C if count == 1 else 0x68 + (count - 2) * 22  # a 0x34-byte bit ends before either: it holds none
    out = []
    for _ in range(count):
        if at + 4 > len(bit):
            break
        size, first = struct.unpack_from("<HH", bit, at)
        if first == 0:
            at += size
            continue
        stop = at + 2 + 2 * size
        if stop > len(bit):
            break
        out.append(bit[at + 2:stop].decode("utf-16-le", "replace"))
        at = stop
    return out


def links(bits_) -> list:
    """The URLs of the type 12 `PLC ` bits (see the module docstring)."""
    out, seen = [], set()
    for bitlist in bits_.values():
        for bit in bitlist:
            if bit.kind != PLC or len(bit) < 8 or _U32.unpack_from(bit, 4)[0] != LINKS:
                continue
            for url in _links(bit):
                url = url.strip()
                if url and url not in seen:
                    seen.add(url)
                    out.append(url)
    return out
