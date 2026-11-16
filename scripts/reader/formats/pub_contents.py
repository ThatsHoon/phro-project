# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub 0477b85184e02dc5276582d9cea969208d56c655, src/lib/MSPUBParser.cpp, MSPUBBlockType.h, MSPUBBlockID.h, MSPUBContentChunkType.h, MSPUBParser2k.cpp.
"""The Contents stream of a Publisher file (pub): its blocks and its chunk directory.

Ported from libmspub (MPL-2.0) at 0477b85184e02dc5276582d9cea969208d56c655, `src/lib/`: `MSPUBParser.cpp`
(`getBlockDataLength`, `parseBlock`, `skipBlock`, `parseContents`, `parseContentChunkReference`),
`MSPUBBlockType.h`, `MSPUBBlockID.h` (the chunk reference ids), `MSPUBContentChunkType.h` (the chunk types) and
`MSPUBParser2k.cpp` (`parseContents`, the Publisher 2000 directory table).

Blocks (`blocks`). A block is an id u8, a type u8 and its data. The type gives the data's length: 0, 2, 4, 8, 16 or 24
bytes (libmspub's table; an unknown type is 0, as there), or variable: 0x80, 0x82, 0x88, 0x8A, 0x90, 0x98, 0xA0 and
0xC0 start with a u32 length that counts itself. `Block(id, type, start, end, data)`: `start` is where the data
begins (libmspub's dataOffset; a variable block's length is there and its children start 4 bytes on), `end` where
the next block begins; `data` is the value of a 2- or 4-byte block (0 for an empty one), the bytes of an 8-, 16- or
24-byte block, the text bytes of a 0xC0 string block, None for another variable block (`children` walks it). A
variable length under 4 or past the parent's end ends the walk (counted `record` by the meter); a block cut short by
the parent's end ends it uncounted, as libmspub's end of stream does. So each block advances at least two bytes, and
a walk is linear in its bytes. Nesting deeper than `limits.MAX_DEPTH` is not walked (libmspub allows 100), counted
`record`.

The meter (`Meter`). `limits.BIN_RECORDS` blocks a document may walk, read at each tick; the first block refused
counts `record` once and every later walk of that document stops at once. `directory` makes one when it is not given
one; the pub reader passes one Meter to every walk of a document. A walk without a meter is bounded by its own bytes
only.

Directory (`directory`), Publisher 2002 and later. The u32 at 0x1A is the trailer's offset; the trailer is a u32
length (counting itself, so the trailer ends at offset + length; a length under 4 or past the stream reads to the
end) and then blocks, of which libmspub reads the first three: the first of type 0x90 (TRAILER_DIRECTORY) holds the
directory. Each of its blocks is one sequence number, counted from 0; a 0x88 (GENERAL_CONTAINER) block holds a chunk
reference: sub-block 0x02 the chunk type, 0x04 its offset, 0x05 its parent's sequence number. A reference with a
type and an offset is a `ChunkRef(type, offset, end, parent, seqnum)`; parent is None when not given.

Directory, Publisher 2000 (`trailer=TRAILER_2K`, 0x16). The trailer is a table (MSPUBParser2k): a u16 count, then
10-byte entries of 2 skipped bytes, the sequence number u16, the parent's u16 and the chunk offset u32. The type is
the chunk's first u16. That is the layout libmspub reads at 0x16; the plan's ruling F14 put the two layouts behind
one call, and they share everything after the read: the cap, the ends and the cycle cut.

For both: `type_of(data, ref)`, when given, gives the type instead. A chunk ends where the next one in directory
order begins; the last ends at the trailer, or at the end of the stream when it starts past the trailer; an end
before its own offset is the offset, and none passes the stream unless its offset does. Up to `PUB_CHUNKS`
references are read (0x88 blocks, or table entries); at the next one the directory stops, counted `record` once.
Parent links are walked once each with a seen-set; the link that closes a cycle is cut (its parent set to None,
nothing counted, nothing lost). The first reference of a sequence number is the one parents name.
"""
import struct
from collections import namedtuple
from collections.abc import Iterator

from .. import limits

PUB_CHUNKS = 100_000   # chunk references one Contents directory may give; past it the rest counted `record`

TRAILER = 0x1A          # where Publisher 2002 and later keep the trailer's offset
TRAILER_2K = 0x16       # Publisher 2000's: its trailer is a table, not blocks (MSPUBParser2k)
TRAILER_PARTS = 3       # blocks libmspub reads at the trailer, looking for the directory

# MSPUBBlockType.h
TRAILER_DIRECTORY = 0x90
GENERAL_CONTAINER = 0x88
STRING_CONTAINER = 0xC0
DUMMY = 0x78
SHAPE_SEQNUM = 0x70
# MSPUBBlockID.h, inside a GENERAL_CONTAINER of the directory
CHUNK_TYPE = 0x02
CHUNK_OFFSET = 0x04
CHUNK_PARENT_SEQNUM = 0x05
# MSPUBContentChunkType.h
SHAPE = 0x01
TABLE = 0x10
ALTSHAPE = 0x20
GROUP = 0x30
LOGO = 0x31
PAGE = 0x43
DOCUMENT = 0x44
BORDER_ART = 0x46
PALETTE = 0x5C
CELLS = 0x63
FONT = 0x6C

# getBlockDataLength: fixed data lengths by type (any other type not variable is 0)
_FIXED = {DUMMY: 0, 0x05: 0, 0x08: 0, 0x0A: 0,
          0x10: 2, 0x12: 2, 0x18: 2, 0x1A: 2, 0x07: 2,
          0x20: 4, 0x22: 4, 0x58: 4, 0x68: 4, SHAPE_SEQNUM: 4, 0xB8: 4,
          0x28: 8, 0x38: 16, 0x48: 24}
VARIABLE = frozenset((STRING_CONTAINER, 0x80, 0x82, GENERAL_CONTAINER, 0x8A, TRAILER_DIRECTORY, 0x98, 0xA0))
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_ENTRY_2K = struct.Struct("<HHHI")

Block = namedtuple("Block", "id type start end data")
# `parent` is None when a 2002+ reference has none; Publisher 2000's table keeps the raw u16 (0xFFFF for none)
ChunkRef = namedtuple("ChunkRef", "type offset end parent seqnum")


class Meter:
    """The blocks one document may walk: `limits.BIN_RECORDS`, read at each tick. The first block refused counts
    `record` once (in `ctx`, when one is given) and every later walk stops at once; `cut` counts one damaged walk."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.used = 0
        self.spent = False

    def tick(self) -> bool:
        if self.spent:
            return False
        if self.used >= limits.BIN_RECORDS:
            self.spent = True
            self.cut()
            return False
        self.used += 1
        return True

    def cut(self):
        if self.ctx is not None:
            self.ctx.skipped["record"] += 1


def blocks(data, start: int, end: int, depth: int = 0, *, meter: Meter | None = None) -> Iterator[Block]:
    """The blocks of `data[start:end]` in order (see the module docstring); none deeper than `limits.MAX_DEPTH`."""
    if meter is not None and meter.spent:
        return
    if depth > limits.MAX_DEPTH:
        if meter is not None:
            meter.cut()
        return
    end = min(end, len(data))
    pos = start
    while pos + 2 <= end:
        if meter is not None and not meter.tick():
            return
        bid, btype = data[pos], data[pos + 1]
        at = pos + 2
        if btype in VARIABLE:
            if at + 4 > end:
                return
            size = _U32.unpack_from(data, at)[0]
            if size < 4 or size > end - at:  # a length counts itself and must fit its parent
                if meter is not None:
                    meter.cut()
                return
            stop = at + size
            body = bytes(data[at + 4:stop]) if btype == STRING_CONTAINER else None
            yield Block(bid, btype, at, stop, body)
        else:
            size = _FIXED.get(btype, 0)
            stop = at + size
            if stop > end:
                return
            if size == 2:
                value = _U16.unpack_from(data, at)[0]
            elif size == 4:
                value = _U32.unpack_from(data, at)[0]
            elif size:
                value = bytes(data[at:stop])
            else:
                value = 0
            yield Block(bid, btype, at, stop, value)
        pos = stop


def children(data, block: Block, depth: int = 0, *, meter: Meter | None = None) -> Iterator[Block]:
    """The blocks inside a variable `block` found at `depth` (none for a fixed one)."""
    if block.type not in VARIABLE:
        return iter(())
    return blocks(data, block.start + 4, block.end, depth + 1, meter=meter)


def _refs_2002(data, ctx, trailer: int, meter: Meter) -> tuple:
    """([type, offset, end, parent, seqnum] lists, the trailer's offset) from the block directory."""
    refs = []
    if trailer + 4 > len(data):
        return refs, len(data)
    toff = _U32.unpack_from(data, trailer)[0]
    if toff + 4 > len(data):
        return refs, len(data)
    size = _U32.unpack_from(data, toff)[0]
    tend = toff + size if 4 <= size <= len(data) - toff else len(data)
    for k, part in enumerate(blocks(data, toff + 4, tend, meter=meter)):
        if k >= TRAILER_PARTS:
            break
        if part.type != TRAILER_DIRECTORY:
            continue
        read = 0
        for seq, b in enumerate(children(data, part, meter=meter)):
            if b.type != GENERAL_CONTAINER:
                continue
            if read >= PUB_CHUNKS:
                ctx.skipped["record"] += 1
                break
            read += 1
            ctype = offset = parent = None
            for sub in children(data, b, 1, meter=meter):
                if not isinstance(sub.data, int):
                    continue
                if sub.id == CHUNK_TYPE:
                    ctype = sub.data
                elif sub.id == CHUNK_OFFSET:
                    offset = sub.data
                elif sub.id == CHUNK_PARENT_SEQNUM:
                    parent = sub.data
            if ctype is not None and offset is not None:
                refs.append([ctype, offset, 0, parent, seq])
        break
    return refs, toff


def _refs_2000(data, ctx, trailer: int) -> tuple:
    """([type, offset, end, parent, seqnum] lists, the trailer's offset) from Publisher 2000's directory table; the
    type is the chunk's first u16 (None when the chunk is past the stream)."""
    refs = []
    if trailer + 4 > len(data):
        return refs, len(data)
    toff = _U32.unpack_from(data, trailer)[0]
    if toff + 2 > len(data):
        return refs, len(data)
    count = _U16.unpack_from(data, toff)[0]
    fits = (len(data) - toff - 2) // _ENTRY_2K.size
    for i in range(min(count, fits)):
        if i >= PUB_CHUNKS:
            ctx.skipped["record"] += 1
            break
        _, seq, parent, offset = _ENTRY_2K.unpack_from(data, toff + 2 + i * _ENTRY_2K.size)
        ctype = _U16.unpack_from(data, offset)[0] if offset + 2 <= len(data) else None
        refs.append([ctype, offset, 0, parent, seq])
    return refs, toff


def _cut_cycles(refs: list):
    """Cut the parent link that closes each cycle; every reference is walked once (a seen-set across the walks)."""
    first = {}
    for r in refs:
        first.setdefault(r[4], r)
    done = set()
    for r in first.values():
        path, on = [], set()
        cur = r
        while cur is not None and cur[4] not in done:
            if cur[4] in on:
                path[-1][3] = None
                break
            on.add(cur[4])
            path.append(cur)
            cur = first.get(cur[3]) if cur[3] is not None else None
        done |= on


def directory(data, ctx, *, trailer: int = TRAILER, type_of=None, meter: Meter | None = None) -> list:
    """The chunk references of a Contents stream, in directory order (see the module docstring)."""
    if meter is None:
        meter = Meter(ctx)
    if trailer == TRAILER_2K:
        refs, toff = _refs_2000(data, ctx, trailer)
    else:
        refs, toff = _refs_2002(data, ctx, trailer, meter)
    n = len(data)
    for i, r in enumerate(refs):
        nxt = refs[i + 1][1] if i + 1 < len(refs) else (toff if r[1] <= toff else n)
        r[2] = max(r[1], min(nxt, n))
    _cut_cycles(refs)
    out = [ChunkRef(*r) for r in refs]
    if type_of is not None:
        out = [ref._replace(type=type_of(data, ref)) for ref in out]
    return out
