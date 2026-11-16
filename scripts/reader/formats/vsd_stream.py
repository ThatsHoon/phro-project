# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libvisio a42aaff13e4a67afcd077c3abb3ef13bb6463025, src/lib/VSDInternalStream.cpp, VSDParser.cpp, VSD6Parser.cpp, VSD5Parser.cpp.
"""The VisioDocument stream of a binary Visio file (vsd): its header, the LZ77 coding, pointer lists, chunk streams and
the walk of the pointer tree from the trailer.

Ported from libvisio (MPL-2.0) at a42aaff13e4a67afcd077c3abb3ef13bb6463025, `src/lib/`: `VSDInternalStream.cpp` (the
LZ77 inflate), `VSDParser.cpp` (`parseMain`, `readPointer`, `readPointerInfo`, `handleStreams`, `handleStream`,
`handleBlob`, `handleChunks`, `getChunkHeader`), `VSD6Parser.cpp` (the Visio 2000 chunk header) and `VSD5Parser.cpp`
(the Visio 5 pointer, pointer list and chunk header). The signature and the version byte are the format's. The
pointer cap follows Apache POI's HDGF (`hdgf/pointers/PointerFactory.java`, `MAX_NUMBER_OF_POINTERS`) at
47dcff769bb395034dddfd59c4609659b6dbc6a4. libvisio's 2026-11-15 hardening commits (a nesting cap, `seekPast`) were read
as knowledge only.

Header. `Visio (TM) Drawing\r\n`, the version byte at 0x1A (5, 6 or 11; libvisio sends 1-4 to its Visio 5 parser, this
reader refuses them) and the trailer pointer at 0x24.

Pointers. v6 and v11: type u32, 4 bytes skipped, offset u32, length u32, format u16 (18 bytes); v5: type u16 and format
u16 (low bytes), 4 skipped, offset u32, length u32 (16). Offsets are into the whole VisioDocument stream. Format bit 1
means the stream is LZ77-coded and its content starts 4 bytes in (libvisio's shift); format >> 4 is the kind: 0x0 and
0x4 a blob, 0x5 a blob holding a pointer list (not for colours, 0x16), 0x8, 0xC and 0xD a chunk stream. A list (v6,
v11) has a u32 at `shift`; at that value + shift - 4 come listSize u32, pointerCount s32, 4 skipped, the pointers and
then, when listSize is over 1, listSize u32 indexes giving an order. v5 has its s16 count at a fixed place per list
type and no order. `pointers` reads at most the bytes left ÷ the pointer size (so at most ÷ 16), skips type 0 as
libvisio does, and reads the order entries that fit; where libvisio drops a whole list at the end of its stream, this
keeps the pointers that fit.

LZ77 (`inflate`). A flag byte rules the next eight items, low bit first: 1 is a literal byte, 0 a two-byte reference,
length (second byte & 15) + 3 and a 12-bit window slot ((second byte >> 4) << 8 | first byte), less 4,078 if over it,
else plus 18, into a 4,096-byte window that starts zeroed with writes from slot 0. The slot is turned into a distance
back from the end of the output, so a reference is a slice, and a reference before the start reads zeros. At most 144
bytes come out of 17 (about 8.5x); the loop is linear and stops past `limit` with ValueError.

Chunks. Zero bytes before a header are skipped (libvisio's getChunkHeader). v6 and v11: type, id, list, dataLength
(u32 each), level u16, unknown u8 (19 bytes), then the data and a trailer whose size follows libvisio's rules for
v11 (`VSDParser::getChunkHeader`) or v6 (`VSD6Parser`). v5: type s16, id s16, level u8, unknown u8, list s16 (the
s16s widened as `VSD5Parser::getUInt`), dataLength u32 (12 bytes), no trailer. The next chunk starts after the data
and trailer, so each chunk advances at least its header. A dataLength that is negative (as s32) or past what is left
ends the stream (POI's NegativeChunkLength and ShortChunk samples), counted `record` once; a header cut short ends it
too, uncounted, as libvisio's end of stream does.

The walk (`Tree`). From the trailer pointer, as libvisio's handleStreams and handleStream: in each list the name lists
(0x32), the name indexes (0xC9, 0x34) and the font faces (0xD8) first, then the order list, then the rest by index.
`Tree.walk()` yields `Step("enter", pointer, index, level, None)` for each stream reached, then `Step("chunk", ...)`
for its blob (the stream from its shift, typed by the pointer, id = the pointer's index; an inflated blob's data is
the inflated bytearray itself, its prefix dropped in place, so it is held once) or for each of its chunks,
then the stream's pointer list's streams, then `Step("leave", ...)`. Levels are libvisio's: a stream one deeper than
its list, its blob or its chunk stream one deeper again, a chunk adding its own header level. `Tree.chunks()` yields
`(Pointer, Chunk)` for the chunk steps. A Tree is one document: its counters are spent across all its walks.

Bounds (each read from its constant at call time; each cut counted `record` once, for its list or stream):
- every pointer's `(offset, length)` is followed once, for every type (libvisio tracks only list offsets), the
  trailer's included;
- lists nest at most `limits.MAX_DEPTH` below the trailer's; a list stream deeper is read as a blob only, counted;
- `VSD_POINTERS` pointers are read per document (POI); a list cut by it is counted;
- `VSD_INFLATE` bytes of stream content are opened per document: what an inflate gives, and the length of a stored
  stream (opening it copies its blob and chunk bytes, and many pointers of different lengths over one region would
  rescan it). A stream that does not fit is not read and is counted. An inflate that passes the room spends all of it,
  so the next coded stream is not inflated at all;
- `limits.BIN_RECORDS` chunks (blobs included) per document; at the next one the walk stops, counted;
- stored streams are memoryview slices of the document, so a pointer costs no copy until its stream fits the room.
The work of a walk is linear in VSD_INFLATE, VSD_POINTERS and BIN_RECORDS together, whatever the file says.
"""
import re
import struct
from collections import namedtuple
from collections.abc import Iterator

from .. import limits
from ..limits import ReadError

VSD_POINTERS = 100_000             # pointers read per document (POI's MAX_NUMBER_OF_POINTERS)
VSD_INFLATE = limits.CFB_STREAM    # stream bytes opened per document, inflated or stored

MAGIC = b"Visio (TM) Drawing\r\n"
VERSIONS = (5, 6, 11)
TRAILER = 0x14
COLOURS = 0x16
NAME_LIST2 = 0x32
NAME_INDEXES = (0xC9, 0x34)        # VSD_NAMEIDX, VSD_NAMEIDX123
FONT_FACES = 0xD8
BLOB_KINDS = (0x0, 0x4, 0x5)
CHUNK_KINDS = (0x8, 0xC, 0xD)
LIST_KIND = 0x5
# where VSD5Parser::readPointerInfo reads a v5 list's s16 count, by the list's type; others 0x1E past 0x45, else 0x0A
_V5_COUNT = {TRAILER: 0x82, 0x15: 0x42, 0x18: 0x2E, 0x1A: 0x12, 0x1D: 0x1E, 0x4E: 0x1E, 0x1E: 0x36}
# chunk trailers: VSDParser::getChunkHeader (v11) and VSD6Parser::getChunkHeader (v6)
_EIGHT_11 = frozenset((0x71, 0x70, 0x6B, 0x6A, 0x69, 0x66, 0x65, 0x2C))
_FOUR_11 = frozenset((0x64, 0x65, 0x66, 0x69, 0x6A, 0x6B, 0x6F, 0x71, 0x92, 0xA9, 0xB4, 0xB6, 0xB9, 0xC7))
_NONE_11 = frozenset((0x1F, 0xC9, 0x2D, 0xD1))
_EIGHT_6 = frozenset((0x76, 0x73, 0x72, 0x71, 0x70, 0x6F, 0x6E, 0x6D, 0x6C, 0x6B, 0x6A, 0x69, 0x68, 0x67, 0x66, 0x65,
                      0x64, 0x2C, 0x0D))
_NONE_6 = frozenset((0x1F, 0xC9))
_NONZERO = re.compile(rb"[^\x00]")
_HEAD11 = struct.Struct("<IIIIHB")
_HEAD5 = struct.Struct("<hhBBhI")
_PTR11 = struct.Struct("<IIIIH")
_PTR5 = struct.Struct("<HHIII")

Pointer = namedtuple("Pointer", "type offset length format")
Chunk = namedtuple("Chunk", "type id list level data")
Step = namedtuple("Step", "kind pointer index level chunk")


def _pointer(buf, at: int, version: int):
    """The pointer at `at`, or None when it does not fit."""
    if version == 5:
        if at < 0 or at + 16 > len(buf):
            return None
        t, f, _, o, n = _PTR5.unpack_from(buf, at)
        return Pointer(t & 0xFF, o, n, f & 0xFF)
    if at < 0 or at + 18 > len(buf):
        return None
    t, _, o, n, f = _PTR11.unpack_from(buf, at)
    return Pointer(t, o, n, f)


def header(data) -> tuple:
    """(version, trailer pointer) of a VisioDocument stream; ReadError for anything this reader does not read."""
    if bytes(data[:len(MAGIC)]) != MAGIC or len(data) <= 0x1A:
        raise ReadError("damaged", "no Visio header")
    version = data[0x1A]
    if 1 <= version <= 4:
        raise ReadError("unsupported", "Visio 4 or older")
    if version not in VERSIONS:
        raise ReadError("unsupported", f"unknown Visio version {version}")
    trailer = _pointer(data, 0x24, version)
    if trailer is None:
        raise ReadError("damaged", "Visio header cut short")
    return version, trailer


def inflate(data, limit: int) -> bytearray:
    """libvisio's LZ77 (VSDInternalStream): the bytes `data` codes, as a bytearray (not copied to bytes, so an
    inflated stream is held once); ValueError once the output passes `limit`."""
    n = len(data)
    out = bytearray()
    if n < 2:
        return out
    off = 0
    while off < n:
        flag = data[off]
        off += 1
        if off > n - 1:
            break
        if flag == 0xFF and off + 8 <= n:  # eight literals
            out += data[off:off + 8]
            off += 8
        else:
            mask = 1
            while mask < 0x100 and off < n:
                if flag & mask:
                    out.append(data[off])
                    off += 1
                else:
                    if off > n - 2:
                        break
                    a1, a2 = data[off], data[off + 1]
                    off += 2
                    length = (a2 & 15) + 3
                    slot = (a2 & 0xF0) << 4 | a1
                    slot = slot - 4078 if slot > 4078 else slot + 18
                    pos = len(out)
                    back = ((pos - slot - 1) & 4095) + 1  # the window slot's byte is this far back from the end
                    start = pos - back
                    if start >= 0 and back >= length:
                        out += out[start:start + length]
                    else:  # overlapping, or before the start (the zeroed window)
                        for k in range(start, start + length):
                            out.append(out[k] if k >= 0 else 0)
                mask <<= 1
        if len(out) > limit:
            raise ValueError("vsd: inflated stream past its limit")
    return out


def _list_info(stream, version: int, ptype: int, shift: int):
    """(first pointer's position, declared count, order entries) as readPointerInfo reads them, or None."""
    if version == 5:
        at = shift + _V5_COUNT.get(ptype, 0x1E if ptype > 0x45 else 0x0A)
        if at + 2 > len(stream):
            return None
        return at + 2, struct.unpack_from("<h", stream, at)[0], 0
    if shift + 4 > len(stream):
        return None
    at = max(0, struct.unpack_from("<I", stream, shift)[0] + shift - 4)  # a seek before the start stops at 0
    if at + 12 > len(stream):
        return None
    size, count = struct.unpack_from("<Ii", stream, at)
    return at + 12, count, size


def _read_list(stream, version: int, ptype: int, shift: int, room: int):
    """([(index, Pointer)] without type 0, order indexes, pointer slots read, cut) for a list stream, reading at most
    `room` pointers; `cut` is True when the room stopped it before the pointers the bytes hold."""
    info = _list_info(stream, version, ptype, shift)
    if info is None:
        return [], [], 0, False
    at, count, size = info
    width = 16 if version == 5 else 18
    fit = max(0, min(count, (len(stream) - at) // width))
    take = min(fit, max(room, 0))
    entries = []
    for i in range(take):
        p = _pointer(stream, at + i * width, version)
        if p.type:
            entries.append((i, p))
    order = []
    if size > 1 and count == fit:
        start = at + count * width
        n = min(size, (len(stream) - start) // 4, take)  # an order names the pointers read; no more entries are kept
        order = list(struct.unpack_from(f"<{n}I", stream, start)) if n > 0 else []
    return entries, order, take, take < fit


def pointers(stream, version: int, *, ptype: int = TRAILER, shift: int = 0) -> list:
    """The pointers of a list stream (`ptype` places a v5 list's count; `shift` is 4 for a coded stream), type 0
    left out, at most VSD_POINTERS."""
    return [p for _, p in _read_list(stream, version, ptype, shift, VSD_POINTERS)[0]]


def _trailer(version: int, ctype: int, lst: int, level: int, unknown: int) -> int:
    if version == 6:
        return 0 if ctype in _NONE_6 else 8 if lst or ctype in _EIGHT_6 else 0
    if ctype in _NONE_11:
        return 0
    n = 8 if lst or ctype in _EIGHT_11 else 0
    if lst or (level == 2 and unknown == 0x55) or (level == 2 and unknown == 0x54 and ctype == 0xAA) \
            or (level == 3 and unknown != 0x50 and unknown != 0x54):
        n += 4
    if ctype in _FOUR_11 and n != 12 and n != 4:
        n += 4
    return n


def chunks(stream, version: int, *, ctx=None) -> Iterator:
    """The chunks of a chunk stream in order. A negative or overlong dataLength ends the stream, counted `record`
    in `ctx` when one is given."""
    n = len(stream)
    v5 = version == 5
    size = 12 if v5 else 19
    pos = 0
    while True:
        m = _NONZERO.search(stream, pos)
        if m is None or m.start() + size > n:
            return
        pos = m.start()
        if v5:
            ctype, cid, level, _, lst, length = _HEAD5.unpack_from(stream, pos)
            ctype, cid, lst, trailer = ctype & 0xFFFFFFFF, cid & 0xFFFFFFFF, lst & 0xFFFFFFFF, 0
        else:
            ctype, cid, lst, length, level, unknown = _HEAD11.unpack_from(stream, pos)
            trailer = _trailer(version, ctype, lst, level, unknown)
        start = pos + size
        if length >= 0x80000000 or length > n - start:
            if ctx is not None:
                ctx.skipped["record"] += 1
            return
        yield Chunk(ctype, cid, lst, level, bytes(stream[start:start + length]))
        pos = start + length + trailer


class Tree:
    """The pointer tree of one VisioDocument stream (see the module docstring)."""

    def __init__(self, data, version: int, ctx):
        self.data, self.version, self.ctx = memoryview(data), version, ctx
        self.trailer = _pointer(data, 0x24, version)
        self.seen = set()
        self.pointers = 0   # pointer slots read
        self.opened = 0     # stream bytes opened (VSD_INFLATE)
        self.records = 0    # chunks yielded (BIN_RECORDS)
        self.stopped = False

    def _cut(self):
        self.ctx.skipped["record"] += 1

    def _open(self, ptr):
        """(content, shift) of a pointer's stream, charged to VSD_INFLATE, or None when it does not fit."""
        raw = self.data[ptr.offset:ptr.offset + ptr.length]
        if len(raw) < 2:  # libvisio's internal stream holds nothing under two bytes
            return b"", 0
        room = VSD_INFLATE - self.opened
        if ptr.format & 2:
            if room <= 0:
                self._cut()
                return None
            try:
                content = inflate(raw, room)
            except ValueError:
                self.opened = VSD_INFLATE  # the work is done; the room is spent
                self._cut()
                return None
            self.opened += len(content)
            return content, 4
        if len(raw) > room:
            self._cut()
            return None
        self.opened += len(raw)
        return raw, 0

    def _more(self) -> bool:
        """Whether another chunk may be yielded; the first refused stops the walk, counted."""
        if self.records >= limits.BIN_RECORDS:
            if not self.stopped:
                self.stopped = True
                self._cut()
            return False
        self.records += 1
        return True

    def walk(self) -> Iterator:
        if self.trailer is None or self.stopped:
            return
        self.seen.add((self.trailer.offset, self.trailer.length))
        opened = self._open(self.trailer)
        if opened is not None:
            yield from self._list(opened[0], TRAILER, opened[1], 0, 0)

    def chunks(self) -> Iterator:
        for step in self.walk():
            if step.kind == "chunk":
                yield step.pointer, step.chunk

    def _list(self, stream, ptype, shift, level, depth):
        entries, order, read, cut = _read_list(stream, self.version, ptype, shift, VSD_POINTERS - self.pointers)
        self.pointers += read
        if cut:
            self._cut()
        first, rest = [], {}
        for i, p in entries:
            if p.type == NAME_LIST2:
                first.append((0, i, p))
            elif p.type in NAME_INDEXES:
                first.append((1, i, p))
            elif p.type == FONT_FACES:
                first.append((2, i, p))
            else:
                rest[i] = p
        sequence = [(i, p) for _, i, p in sorted(first, key=lambda e: e[:2])]
        for j in order:
            p = rest.pop(j, None)
            if p is not None:
                sequence.append((j, p))
        sequence += rest.items()
        for i, p in sequence:
            if self.stopped:
                return
            yield from self._stream(p, i, level + 1, depth)

    def _stream(self, ptr, index, level, depth):
        key = (ptr.offset, ptr.length)
        if key in self.seen:
            return
        self.seen.add(key)
        yield Step("enter", ptr, index, level, None)
        kind = ptr.format >> 4
        opened = self._open(ptr) if kind in BLOB_KINDS or kind in CHUNK_KINDS else None
        if opened is not None:
            stream, shift = opened
            if kind in BLOB_KINDS:
                if isinstance(stream, bytearray):  # inflated: drop the prefix in place, the blob is the stream itself
                    del stream[:shift]
                    blob, shift = stream, 0      # (a list read from 0 of the rest is readPointerInfo's shift)
                else:
                    blob = bytes(stream[shift:])
                if self._more():
                    yield Step("chunk", ptr, index, level + 1, Chunk(ptr.type, index, 0, level + 1, blob))
                    if kind == LIST_KIND and ptr.type != COLOURS:
                        if depth + 1 > limits.MAX_DEPTH:
                            self._cut()
                        else:
                            yield from self._list(stream, ptr.type, shift, level + 1, depth + 1)
            else:
                for c in chunks(stream, self.version, ctx=self.ctx):  # read from 0, as libvisio's handleChunks
                    if not self._more():
                        break
                    at = c.level + level + 1
                    yield Step("chunk", ptr, index, at, c._replace(level=at))
        yield Step("leave", ptr, index, level, None)
