"""Visio binary (vsd): the VisioDocument stream layer (header, LZ77, pointer lists, chunk streams, the trailer walk)
and the drawing reader (pages, shapes, text, fields, pictures, routing).

Run: python -I -X utf8 tests/test_reader_vsd.py
     python -I -X utf8 tests/test_reader_vsd.py measure   (the hostile-input probes, in memory: s/MiB and peak)
"""
import math
import random
import struct
import sys
import tempfile
import time
import tracemalloc
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import diagram, limits, markdown  # noqa: E402
from reader.formats import vsd, vsd_stream, vsdx  # noqa: E402
from reader.formats.vsd_stream import Chunk, Pointer  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Break, Code, Context, Image, ListItem, Para, Section, Text  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from reader_fix import cfb_bytes, lz77, make_zip, vsd_chunk, vsd_document, vsd_head, vsd_list, vsd_pointer  # noqa: E402

MIB = 2**20


def refused(fn, kind, detail):
    try:
        fn()
    except ReadError as e:
        assert (e.kind, e.detail) == (kind, detail), (e.kind, e.detail)
        return
    raise AssertionError("ReadError not raised")


def walk(data):
    ctx = Context()
    version, _ = vsd_stream.header(data)
    return list(vsd_stream.Tree(data, version, ctx).walk()), ctx


def entered(steps):
    return [(s.pointer.type, s.index) for s in steps if s.kind == "enter"]


def chunk_types(steps):
    return [s.chunk.type for s in steps if s.kind == "chunk"]


class Layout:
    """A hand-made VisioDocument: `put` appends a stream and returns its (type, offset, length, format) pointer;
    `done` writes the header with the trailer pointer."""

    def __init__(self, version=11):
        self.version, self.body = version, bytearray(0x40)

    def put(self, raw, ptype, fmt):
        self.body += raw
        return ptype, len(self.body) - len(raw), len(raw), fmt

    def done(self, trailer):
        return vsd_head(self.version, trailer, self.body)


def test_header():
    for v in (11, 6, 5):
        data = vsd_document(v, [[{"text": "A"}]])
        version, trailer = vsd_stream.header(data)
        assert version == v
        # the fixture places the trailer list last
        assert trailer == Pointer(0x14, trailer.offset, len(data) - trailer.offset, 0x50), trailer
    data = bytearray(vsd_document(11, []))
    for old in (1, 4):
        data[0x1A] = old
        refused(lambda: vsd_stream.header(bytes(data)), "unsupported", "Visio 4 or older")
    for odd in (0, 7, 12):
        data[0x1A] = odd
        refused(lambda: vsd_stream.header(bytes(data)), "unsupported", f"unknown Visio version {odd}")
    data[0x1A] = 11
    data[0] = ord("v")
    refused(lambda: vsd_stream.header(bytes(data)), "damaged", "no Visio header")
    refused(lambda: vsd_stream.header(b"Visio"), "damaged", "no Visio header")
    # the signature and a version, but no room for the trailer pointer
    refused(lambda: vsd_stream.header(vsd_document(11, [])[:0x30]), "damaged", "Visio header cut short")


def test_inflate():
    # hand-coded: three literals, a back reference of 6 at distance 3 (it overlaps the bytes it writes), a literal
    coded = bytes.fromhex("17616263eef358")
    assert vsd_stream.inflate(coded, 100) == b"abcabcabcX"
    assert lz77(b"abcabcabcX") == coded
    assert vsd_stream.inflate(coded, 10) == b"abcabcabcX"
    try:
        vsd_stream.inflate(coded, 5)
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError not raised")
    # a reference into the window before anything was written reads its zeros
    assert vsd_stream.inflate(bytes([0x00, 0x00, 0x0F]), 100) == bytes(18)
    # libvisio's short inputs: under two bytes nothing; a flag with nothing after it ends; a reference cut short ends
    assert vsd_stream.inflate(b"\xff", 100) == b""
    assert vsd_stream.inflate(b"\x01A\x00", 100) == b"A"
    assert vsd_stream.inflate(b"\x00\x41", 100) == b""
    rng = random.Random(14)
    words = [bytes(rng.choice(b"abcdefgh ") for _ in range(rng.randrange(1, 9))) for _ in range(200)]
    for size in (1, 17, 4095, 4096, 4097, 20_000):
        data = b"".join(rng.choice(words) for _ in range(size))[:size]
        assert vsd_stream.inflate(lz77(data), len(data)) == data, size
    noise = bytes(rng.randrange(256) for _ in range(9000))
    assert vsd_stream.inflate(lz77(noise), len(noise)) == noise


def _window_inflate(data):
    """VSDInternalStream's loop as written: a zeroed 4,096-byte window, every reference copied byte by byte."""
    window, pos, off, n, out = bytearray(4096), 0, 0, len(data), bytearray()
    if n < 2:
        return out
    while off < n:
        flag = data[off]
        off += 1
        if off > n - 1:
            break
        for bit in range(8):
            if off >= n:
                break
            if flag >> bit & 1:
                window[pos & 4095] = data[off]
                out.append(data[off])
                off, pos = off + 1, pos + 1
                continue
            if off > n - 2:
                break
            a1, a2 = data[off], data[off + 1]
            off += 2
            length, slot = (a2 & 15) + 3, (a2 & 0xF0) << 4 | a1
            slot = slot - 4078 if slot > 4078 else slot + 18
            for j in range(length):
                window[(pos + j) & 4095] = window[(slot + j) & 4095]
                out.append(window[(slot + j) & 4095])
            pos += length
    return out


def _code(items, pos=0):
    """LZ77 bytes for `items`, `pos` bytes out: an int is a literal, (distance, length) a reference that far back."""
    coded = bytearray()
    for g in range(0, len(items), 8):
        group = items[g:g + 8]
        coded.append(sum(1 << i for i, item in enumerate(group) if isinstance(item, int)))
        for item in group:
            if isinstance(item, int):
                coded.append(item)
                pos += 1
                continue
            back, length = item
            slot = (pos - back) & 4095            # the window slot `back` bytes from the end
            s = slot - 18 if slot >= 18 else slot + 4078
            coded += bytes((s & 0xFF, (s >> 8) << 4 | (length - 3)))
            pos += length
    return bytes(coded)


def test_overlap_copies():
    # an overlapping reference (distance under its length) repeats the last `distance` bytes; each distance and length
    # gives the byte-by-byte window's output, before and after 4,096 bytes out, and reaching before the start
    rng = random.Random(1414)
    items = []
    for _ in range(300):
        items += [rng.randrange(1, 256) for _ in range(rng.randrange(1, 5))]
        items.append((rng.choice((1, 2, 3, 17)), rng.choice((4, 5, 7, 10, 11, 13, 16, 18))))
        items.append((rng.randrange(1, 18), rng.randrange(3, 19)))
    head = [(5, 18), 65, (3, 10), (17, 18)]          # references before anything was written read zeros
    for case in (items, head + items, head):
        coded = _code(case)
        assert vsd_stream.inflate(coded, 1 << 20) == _window_inflate(coded)
    assert vsd_stream.inflate(_code([65, 66, 67, (3, 10)]), 100) == b"ABCABCABCABCA"
    assert vsd_stream.inflate(_code([65, 66, (1, 7)]), 100) == b"ABBBBBBBB"
    # the copy is in bulk: 16 MiB of distance-1 references inflate about as fast as 16 MiB of plain ones
    def rate(back):
        coded = _repeat(back, 16 * MIB // 36_864)
        t0 = time.perf_counter()
        out = vsd_stream.inflate(coded, 32 * MIB)
        return time.perf_counter() - t0, out

    plain, out = rate(18)
    overlap, out1 = rate(1)
    assert len(out) == len(out1) > 16 * MIB - 36_864 and out1 == bytes([66]) * len(out1)
    assert overlap < 2 * plain + 0.1 and overlap / (len(out1) / MIB) < 0.2, (overlap, plain)


def _repeat(back, blocks):
    """16 literals, then `blocks` x 2,048 references of 18 bytes at `back`: 36,864 bytes out a block, the same bytes
    each block since the window slots come round every 4,096 bytes out."""
    return _code([66] * 16) + _code([(back, 18)] * 2048, pos=16) * blocks


def test_pointer_sizes():
    pair = [(0x48, 100, 20, 0xD0), (0x15, 200, 30, 0x51)]
    want = [Pointer(*p) for p in pair]
    assert vsd_stream.pointers(vsd_list(pair), 11) == want
    assert vsd_stream.pointers(vsd_list(pair, version=6), 6) == want
    assert vsd_stream.pointers(vsd_list(pair, version=5), 5) == want
    assert vsd_stream.pointers(vsd_list(pair, version=5, ptype=0x15), 5, ptype=0x15) == want
    assert vsd_stream.pointers(vsd_list(pair, version=5, ptype=0x27), 5, ptype=0x27) == want
    assert len(vsd_pointer(1, 2, 3, 4)) == 18 and len(vsd_pointer(1, 2, 3, 4, version=5)) == 16
    # a compressed list stream is read after its four-byte prefix (libvisio's shift)
    assert vsd_stream.pointers(bytes(4) + vsd_list(pair), 11, shift=4) == want
    # type 0 is skipped; a count past the bytes reads what fits; a negative count reads nothing
    assert vsd_stream.pointers(vsd_list([(0, 1, 2, 0x50)] + pair), 11) == want
    assert vsd_stream.pointers(vsd_list(pair, count=1000), 11) == want
    assert vsd_stream.pointers(vsd_list(pair, version=5, count=1000), 5) == want
    assert vsd_stream.pointers(vsd_list(pair, count=-1), 11) == []
    assert vsd_stream.pointers(b"\x40\x00", 11) == [] and vsd_stream.pointers(b"\x00" * 3, 5) == []


def test_order_list_bounded():
    # an order list declaring far more entries than the list has pointers: only as many as the pointers read are kept
    stream = vsd_list([(0x48, 100, 20, 0xD0)] * 3, order=[2, 0, 1] + [7] * 200_000)
    entries, order, read, cut = vsd_stream._read_list(stream, 11, 0x14, 0, 10)
    assert order == [2, 0, 1] and read == 3 and not cut
    entries, order, read, cut = vsd_stream._read_list(stream, 11, 0x14, 0, 2)
    assert order == [2, 0] and read == 2 and cut


def test_chunk_headers():
    stream = (b"\x00\x00" + vsd_chunk(0x48, b"S" * 6, list_=2, level=1) + vsd_chunk(0x6A, b"P" * 3, level=1)
              + vsd_chunk(0x2D, b"N", level=3) + vsd_chunk(0x0E, b"T", cid=7, level=3) + vsd_chunk(0x48, b"E")
              + b"\x00" * 5)
    assert len(vsd_chunk(0x48, b"S" * 6, list_=2, level=1)) == 19 + 6 + 12  # list: 8 + 4
    assert len(vsd_chunk(0x6A, b"P" * 3, level=1)) == 19 + 3 + 12          # 8, then the trailer list's 4
    assert len(vsd_chunk(0x2D, b"N", level=3)) == 19 + 1                   # never a trailer
    assert len(vsd_chunk(0x0E, b"T", level=3)) == 19 + 1 + 4               # level 3, unknown 0
    assert list(vsd_stream.chunks(stream, 11)) == [Chunk(0x48, 0, 2, 1, b"SSSSSS"), Chunk(0x6A, 0, 0, 1, b"PPP"),
                                                   Chunk(0x2D, 0, 0, 3, b"N"), Chunk(0x0E, 7, 0, 3, b"T"),
                                                   Chunk(0x48, 0, 0, 0, b"E")]
    # level 2 with unknown 0x55, and 0xAA with 0x54, take four trailer bytes; level 3 with 0x50 or 0x54 does not
    odd = b"".join(struct.pack("<IIIIHB", t, 0, 0, 1, lvl, unk) + b"d" + b"\xff" * tr
                   for t, lvl, unk, tr in ((0x48, 2, 0x55, 4), (0xAA, 2, 0x54, 4), (0x48, 2, 0x54, 0),
                                           (0x48, 3, 0x50, 0), (0x48, 3, 0x54, 0), (0x64, 1, 0, 4)))
    assert [(c.type, c.level) for c in vsd_stream.chunks(odd + vsd_chunk(0x48, b""), 11)] == [
        (0x48, 2), (0xAA, 2), (0x48, 2), (0x48, 3), (0x48, 3), (0x64, 1), (0x48, 0)]
    six = (vsd_chunk(0x0D, b"x", version=6) + vsd_chunk(0x6A, b"y", version=6, level=1)
           + vsd_chunk(0x48, b"z", version=6))
    assert len(vsd_chunk(0x0D, b"x", version=6)) == 19 + 1 + 8
    assert list(vsd_stream.chunks(six, 6)) == [Chunk(0x0D, 0, 0, 0, b"x"), Chunk(0x6A, 0, 0, 1, b"y"),
                                               Chunk(0x48, 0, 0, 0, b"z")]
    five = vsd_chunk(0x48, b"ab", version=5, cid=3, level=1) + vsd_chunk(0xFFFF, b"c", version=5, cid=-1, list_=-1)
    assert len(five) == 12 + 2 + 12 + 1
    # v5 types, ids and lists are s16 widened as VSD5Parser::getUInt does
    assert list(vsd_stream.chunks(five, 5)) == [Chunk(0x48, 3, 0, 1, b"ab"),
                                                Chunk(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0, b"c")]
    # a header cut short ends the stream; nothing is counted
    ctx = Context()
    assert list(vsd_stream.chunks(vsd_chunk(0x48, b"") + b"\x0e\x00\x00", 11, ctx=ctx)) == [Chunk(0x48, 0, 0, 0, b"")]
    assert not ctx.skipped


def test_negative_and_short_chunk():
    ok, after = vsd_chunk(0x48, b"ab"), vsd_chunk(0x48, b"cd")
    for length, tail in ((0xFFFFFFFF, b"zz"), (100, b"z" * 10), (0x80000000, b"")):
        bad = struct.pack("<IIIIHB", 0x0E, 0, 0, length, 1, 0) + tail
        ctx = Context()
        assert list(vsd_stream.chunks(ok + bad + after, 11, ctx=ctx)) == [Chunk(0x48, 0, 0, 0, b"ab")]
        assert ctx.skipped == {"record": 1}, ctx.skipped
    ctx = Context()
    bad5 = struct.pack("<HHBBHI", 0x0E, 0, 1, 0, 0, 0xFFFFFFFF)
    five = vsd_chunk(0x48, b"ab", version=5) + bad5
    assert list(vsd_stream.chunks(five, 5, ctx=ctx)) == [Chunk(0x48, 0, 0, 0, b"ab")]
    assert ctx.skipped == {"record": 1}
    # through the walk: the shape stream ends at the bad chunk, counted once, and the walk goes on
    bad = struct.pack("<IIIIHB", 0x0E, 0, 0, 0xFFFFFFFF, 2, 0)
    steps, ctx = walk(vsd_document(11, [[{"chunks": [bad]}, {"text": "B"}]]))
    assert chunk_types(steps) == [0x27, 0x15, 0x48, 0x48, 0x0E], chunk_types(steps)
    assert ctx.skipped == {"record": 1}


def test_document_walk():
    for v in (11, 6, 5):
        steps, ctx = walk(vsd_document(v, [[{"text": "Hi"}]]))
        assert [(s.kind, s.pointer.type) for s in steps] == [
            ("enter", 0x27), ("chunk", 0x27), ("enter", 0x15), ("chunk", 0x15), ("enter", 0x48), ("chunk", 0x48),
            ("chunk", 0x48), ("leave", 0x48), ("leave", 0x15), ("leave", 0x27)], (v, steps)
        text = [s.chunk for s in steps if s.kind == "chunk" and s.chunk.type == 0x0E]
        assert text[0].data == bytes(8) + "Hi".encode("utf-16-le" if v == 11 else "cp1252")
        # libvisio's levels: a stream one deeper than its list, its blob or chunks one deeper again, a chunk adding
        # its own header level
        assert [s.level for s in steps if s.kind == "enter"] == [1, 3, 5]
        assert text[0].level == 2 + 6 and not ctx.skipped
        # the blob of a list stream is the whole stream; the page blob's bytes are the page list
        page = next(s for s in steps if s.kind == "chunk" and s.chunk.type == 0x15)
        assert page.chunk.level == 4 and page.index == 0 and page.pointer.format == 0x51
    steps, ctx = walk(vsd_document(11, [{"shapes": [{"text": "B"}], "background": True}], masters=[[{"text": "M"}]]))
    assert entered(steps) == [(0x27, 0), (0x15, 0), (0x48, 0), (0x1D, 1), (0x1E, 0), (0x48, 0)]
    assert next(s for s in steps if s.kind == "enter" and s.pointer.type == 0x15).pointer.format == 0x50
    # Tree.chunks() is the walk's chunks with their stream's pointer
    data = vsd_document(11, [[{"text": "Hi"}]])
    tree = vsd_stream.Tree(data, 11, Context())
    pairs = list(tree.chunks())
    assert [(p.type, c.type) for p, c in pairs] == [(0x27, 0x27), (0x15, 0x15), (0x48, 0x48), (0x48, 0x0E)]
    # a Tree is walked once: a second walk raises at the call instead of yielding nothing
    for again in (tree.walk, tree.chunks):
        try:
            again()
        except RuntimeError:
            pass
        else:
            raise AssertionError("RuntimeError not raised")


def test_compressed_streams():
    pages = [[{"text": "One"}, {"text": "Two", "chunks": [(0x9B, b"\x01" * 40)]}], [{"text": "Three"}]]
    stored, _ = walk(vsd_document(11, pages))
    packed = [{"shapes": [dict(s, compressed=True) for s in p], "compressed": True} for p in pages]
    data = vsd_document(11, packed)
    steps, ctx = walk(data)
    assert {s.pointer.format for s in steps if s.kind == "enter" and s.pointer.type in (0x15, 0x48)} == {0x53, 0xD2}
    # a coded chunk stream is read from its start, as libvisio's handleChunks does (real ones start with their first
    # chunk); a blob and a pointer list after the four-byte prefix (the list blobs differ only in the offsets they hold)
    lists = (0x15, 0x27)
    assert ([(s.chunk.type, s.chunk.data) for s in steps if s.kind == "chunk" and s.chunk.type not in lists]
            == [(s.chunk.type, s.chunk.data) for s in stored if s.kind == "chunk" and s.chunk.type not in lists])
    page = next(s.chunk.data for s in steps if s.kind == "chunk" and s.chunk.type == 0x15)
    assert page[:4] == struct.pack("<I", 16)  # the blob starts after the prefix: the list offset, then the blob
    assert entered(steps) == entered(stored) and not ctx.skipped


def test_walk_order():
    # names (0x32), name indexes (0xC9, 0x34) and font faces (0xD8) first, then the order list, then index order
    lay = Layout()
    shape = vsd_chunk(0x48, b"", level=1)
    a = lay.put(shape, 0x48, 0xD0)
    b = lay.put(shape + b"\x00", 0x48, 0xD0)
    c = lay.put(shape + b"\x00\x00", 0x48, 0xD0)
    names = lay.put(b"\x00" * 8, 0x32, 0x40)
    faces = lay.put(b"\x00" * 9, 0xD8, 0x40)
    idx = lay.put(b"\x00" * 10, 0xC9, 0x40)
    trailer = lay.put(vsd_list([a, b, faces, names, c, idx], order=[4, 0]), 0x14, 0x50)
    steps, _ = walk(lay.done(trailer))
    assert entered(steps) == [(0x32, 3), (0xC9, 5), (0xD8, 2), (0x48, 4), (0x48, 0), (0x48, 1)], entered(steps)
    # colours (0x16) in a list format are read as a blob only
    lay = Layout()
    inner = lay.put(shape, 0x48, 0xD0)
    colours = lay.put(vsd_list([inner]), 0x16, 0x50)
    steps, _ = walk(lay.done(lay.put(vsd_list([colours]), 0x14, 0x50)))
    assert entered(steps) == [(0x16, 0)]


def test_pointer_cycles():
    lay = Layout()
    stream = vsd_chunk(0x48, b"s" * 4, level=1)
    shape = lay.put(stream, 0x48, 0xD0)
    size = len(vsd_list([shape] * 3))
    at = len(lay.body)
    self_list = lay.put(vsd_list([(0x15, at, size, 0x51), shape, shape]), 0x27, 0x50)
    assert self_list == (0x27, at, size, 0x50)
    trailer_at = len(lay.body)
    size = len(vsd_list([self_list] * 3))
    trailer = lay.put(vsd_list([self_list, self_list, (0x27, trailer_at, size, 0x50)]), 0x14, 0x50)
    steps, ctx = walk(lay.done(trailer))
    # the list, its pointer to itself, the shape stream twice and the trailer: each (offset, length) once
    assert entered(steps) == [(0x27, 0), (0x48, 1)], entered(steps)
    assert chunk_types(steps) == [0x27, 0x48] and not ctx.skipped


def test_pointer_cap():
    old = vsd_stream.VSD_POINTERS
    vsd_stream.VSD_POINTERS = 3
    try:
        steps, ctx = walk(vsd_document(11, [[{"text": "A"}, {"text": "B"}, {"text": "C"}]]))
    finally:
        vsd_stream.VSD_POINTERS = old
    # the trailer, the pages list and the page each read one pointer: the page's other two are cut, counted once
    assert entered(steps) == [(0x27, 0), (0x15, 0), (0x48, 0)], entered(steps)
    assert ctx.skipped == {"record": 1}
    steps, ctx = walk(vsd_document(11, [[{"text": "A"}, {"text": "B"}, {"text": "C"}]]))
    assert len(entered(steps)) == 5 and not ctx.skipped


def test_inflate_cap():
    shapes = b"".join(vsd_chunk(0x48, b"x" * 40, level=1) for _ in range(100))
    packed = lz77(shapes)
    data = vsd_document(11, [], trailer_extra=[(0x48, 0xD2, packed), (0x48, 0xD2, packed + b"\x00")])
    steps, ctx = walk(data)
    assert chunk_types(steps).count(0x48) == 200 and not ctx.skipped
    old, real, calls = vsd_stream.VSD_INFLATE, vsd_stream.inflate, []
    vsd_stream.VSD_INFLATE = len(data)  # every stored stream fits, neither inflated one does
    vsd_stream.inflate = lambda raw, limit: calls.append(limit) or real(raw, limit)
    try:
        steps, ctx = walk(data)
    finally:
        vsd_stream.VSD_INFLATE, vsd_stream.inflate = old, real
    assert len(shapes) > len(data)
    # both streams are entered and counted; the first spends the room, so the second is not inflated at all
    assert entered(steps) == [(0x27, 0), (0x48, 1), (0x48, 2)] and chunk_types(steps) == [0x27]
    assert ctx.skipped == {"record": 2}, ctx.skipped
    assert len(calls) == 1 and 0 < calls[0] < len(data), calls


def test_stored_streams_charged():
    # one stored region reached by many pointers of different lengths: each opened stream is charged to VSD_INFLATE
    lay = Layout()
    region = lay.put(b"".join(vsd_chunk(0x48, b"", level=1) for _ in range(100)), 0x48, 0xD0)
    ptrs = [(0x48, region[1], region[2] - k, 0xD0) for k in range(20)]
    data = lay.done(lay.put(vsd_list(ptrs), 0x14, 0x50))
    trailer = vsd_stream.header(data)[1]
    old = vsd_stream.VSD_INFLATE
    vsd_stream.VSD_INFLATE = trailer.length + 3 * region[2]
    try:
        steps, ctx = walk(data)
    finally:
        vsd_stream.VSD_INFLATE = old
    assert len(entered(steps)) == 20 and chunk_types(steps).count(0x48) == 100 + 99 + 99
    assert ctx.skipped == {"record": 17}, ctx.skipped


def test_depth():
    lay = Layout()
    shape = lay.put(vsd_chunk(0x48, b"", level=1), 0x48, 0xD0)
    inner = shape
    for _ in range(limits.MAX_DEPTH + 5):
        inner = lay.put(vsd_list([inner]), 0x27, 0x50)
    steps, ctx = walk(lay.done(lay.put(vsd_list([inner]), 0x14, 0x50)))
    # the trailer's list is depth 0; MAX_DEPTH lists are read below it and the next one is not
    assert len(entered(steps)) == limits.MAX_DEPTH + 1 and 0x48 not in [t for t, _ in entered(steps)]
    assert ctx.skipped == {"record": 1}
    lay = Layout()
    inner = lay.put(vsd_chunk(0x48, b"", level=1), 0x48, 0xD0)
    for _ in range(limits.MAX_DEPTH - 1):
        inner = lay.put(vsd_list([inner]), 0x27, 0x50)
    steps, ctx = walk(lay.done(lay.put(vsd_list([inner]), 0x14, 0x50)))
    assert entered(steps)[-1] == (0x48, 0) and not ctx.skipped


def test_chunk_cap():
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 4
    try:
        steps, ctx = walk(vsd_document(11, [[{"text": "A"}, {"text": "B"}]]))
    finally:
        limits.BIN_RECORDS = old
    assert chunk_types(steps) == [0x27, 0x15, 0x48, 0x0E] and ctx.skipped == {"record": 1}
    # the walk stops at the fifth chunk (shape B's first), and every stream it entered is left
    assert [s.kind for s in steps].count("enter") == [s.kind for s in steps].count("leave") == 4
    # exactly BIN_RECORDS chunks and no more: nothing is cut
    limits.BIN_RECORDS = 4
    try:
        steps, ctx = walk(vsd_document(11, [[{"text": "A"}]]))
    finally:
        limits.BIN_RECORDS = old
    assert len(chunk_types(steps)) == 4 and not ctx.skipped


# -- vsd.py: the drawing (pages, shapes, text, fields, pictures)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
EMF = struct.pack("<II", 1, 88) + bytes(32) + b" EMF" + bytes(44)
NONE = 0xFFFFFFFF


def xform(px, py, lx=0.0, ly=0.0, w=1.0, h=1.0, angle=0.0, fx=0, fy=0):
    """A 0x9B chunk's data: PinX, PinY, Width, Height, LocPinX, LocPinY, Angle, each a unit byte and a double, then
    the two flip bytes."""
    return b"".join(b"\x00" + struct.pack("<d", v) for v in (px, py, w, h, lx, ly, angle)) + bytes([fx, fy])


def xform1d(bx, by, ex, ey):
    """A 0x9D chunk's data (readXForm1D): BeginX, BeginY, EndX, EndY, each a unit byte and a double."""
    return b"".join(b"\x00" + struct.pack("<d", v) for v in (bx, by, ex, ey))


def line(start=0, end=0, version=11):
    """A 0x85 chunk's data (readLine): the start and end markers at 25 and 26 (v5: 21 and 22), then the cap byte."""
    return bytes(21 if version == 5 else 25) + bytes([start, end, 0])


def shape_data(parent=0, master=None, version=11, line_style=NONE):
    """A shape chunk's data (readShape): v6 and v11 the parent u32 at 10, the master page u32 at 18, the master
    shape u32 at 26, then the fill, line and text style ids (u32 at 34, 42, 50); v5 the parent s16 at 2, the master
    page and shape s16 at 6 and 8, the line, fill and text style s16 at 10, 12 and 14. No master: all ones; the fill
    and text styles are all ones."""
    page, shape = master if master is not None else (NONE, NONE)
    if version == 5:
        d = bytearray(0x22)
        struct.pack_into("<HxxHHHHH", d, 2, parent & 0xFFFF, page & 0xFFFF, shape & 0xFFFF, line_style & 0xFFFF,
                         0xFFFF, 0xFFFF)
        return bytes(d)
    d = bytearray(0x36)
    struct.pack_into("<I", d, 10, parent)
    struct.pack_into("<I", d, 18, page)
    struct.pack_into("<I", d, 26, shape)
    struct.pack_into("<I", d, 34, NONE)
    struct.pack_into("<I", d, 42, line_style)
    struct.pack_into("<I", d, 50, NONE)
    return bytes(d)


def sheet_data(parent=NONE, version=11):
    """A style sheet chunk's data (0x4A, readStyleSheet): v6 and v11 the parent line, fill and text style u32 at 0x22,
    0x2A and 0x32; v5 the s16s at 10, 12 and 14. The fill and text parents are all ones."""
    if version == 5:
        return bytes(10) + struct.pack("<HHH", parent & 0xFFFF, 0xFFFF, 0xFFFF)
    d = bytearray(0x36)
    struct.pack_into("<I", d, 0x22, parent)
    struct.pack_into("<I", d, 0x2A, NONE)
    struct.pack_into("<I", d, 0x32, NONE)
    return bytes(d)


def encode(text, version):
    return text.encode("utf-16-le" if version == 11 else "cp1252")


def v5_records(records):
    """A Visio 5 list chunk's data (VSD5Parser::handleChunkRecords): each record's data at a 4-aligned offset, then
    the table of (type u16, offset u16), last record first, then the record count u16 and the end offset u16."""
    body, offsets = bytearray(), []
    for _, data in records:
        offsets.append(len(body))
        body += data
        body += bytes(-len(body) % 4)
    table = b"".join(struct.pack("<HH", t, o) for (t, _), o in reversed(list(zip(records, offsets))))
    return bytes(body) + table + struct.pack("<HH", len(records), len(body))


def misc(version=11, hide=False, ends=()):
    """A 0xA4 chunk's data (readMisc): the flags byte (0x20 hides the text), then from 45 (v11) or 23 (v6) the
    blocks, one 21-byte block a glued end: length, type 2, a byte, 0x74, 0x6000004E, the shape id, 0x7A,
    0x40000073, a byte. Visio 5 has the flags only."""
    start = {11: 45, 6: 23}.get(version, 1)
    d = bytearray(start)
    d[0] = 0x20 if hide else 0
    for sid in ends:
        d += struct.pack("<IBBBIIBIB", 21, 2, 0x0B, 0x74, 0x6000004E, sid, 0x7A, 0x40000073, 0xFE)
    return bytes(d)


def char_ix(count, font, version=6):
    """A 0x94 chunk (readCharIX): the run's character count (u32 v6, u16 v5) and its font id u16."""
    return struct.pack("<IH", count, font) + bytes(30) if version != 5 else struct.pack("<HH", count, font) + bytes(20)


def font_ix(charset, name=b"Arial", version=6):
    """A 0x19 chunk (readFontIX): 2 bytes, the charset in the low byte of a u32 (v6) or s16 (v5), the name to a NUL."""
    head = struct.pack("<HI", 0, charset) if version != 5 else struct.pack("<HH", 0, charset)
    return head + name + b"\x00"


def text_field(name_id):
    """A 0xA1 chunk for a string field (cell type 232) showing name `name_id` of its shape's 0x2D names."""
    return bytes(7) + bytes([232]) + struct.pack("<i", name_id) + bytes(6) + struct.pack("<i", -1)


def number_field(value, fmt, cell=32, start=0x36):
    """A 0xA1 chunk for a number field: the cell type at 7, the value at 8, and at `start` the block naming its
    format (length, a byte, block index 2, a byte, the format u16, then 0x80 0xC2)."""
    head = bytearray(start)
    head[7] = cell
    struct.pack_into("<d", head, 8, value)
    return bytes(head) + struct.pack("<IBBBHBB", 11, 0, 2, 0, fmt, 0x80, 0xC2)


def shape_chunks(s, version, level=1):
    """One shape's chunks: the shape chunk (header `level`) and its own chunks one level deeper (the XForm two).
    A shape dict: "id", "type", "parent", "master" ((page, shape)), "text" (or "raw": the text's bytes), "pin"
    (PinX, PinY[, LocPinX, LocPinY]), "misc" (readMisc data), "runs" ([(count, font id)]) and "chunks" ((type,
    data[, keywords])), "line_style" (the shape chunk's line style id), "data" (the shape chunk's data in place of
    shape_data's), "ends" (BeginX, BeginY, EndX, EndY: an XForm1D) and "line" (0x85 data). Visio 5 keeps the XForm,
    XForm1D, line and misc as records of a 0x68 list chunk and the runs in a 0x69."""
    head = s["data"] if "data" in s else shape_data(s.get("parent", 0), s.get("master"), version,
                                                    s.get("line_style", NONE))
    parts = [vsd_chunk(s.get("type", 0x48), head, version=version, cid=s.get("id", NONE), level=level)]
    sub = level + 1
    if "text" in s or "raw" in s:
        raw = s["raw"] if "raw" in s else encode(s["text"], version)
        parts.append(vsd_chunk(0x0E, bytes(8) + raw, version=version, level=sub))
    if version == 5:
        props = ([(0x9B, xform(*s["pin"]))] if "pin" in s else []) + ([(0xA4, s["misc"])] if "misc" in s else [])
        props += ([(0x9D, xform1d(*s["ends"]))] if "ends" in s else []) + ([(0x85, s["line"])] if "line" in s else [])
        if props:
            parts.append(vsd_chunk(0x68, v5_records(props), version=5, level=sub))
        if s.get("runs"):
            parts.append(vsd_chunk(0x69, v5_records([(0x94, char_ix(n, f, 5)) for n, f in s["runs"]]), version=5,
                                   level=sub))
    else:
        if "pin" in s:
            parts.append(vsd_chunk(0x9B, xform(*s["pin"]), version=version, level=sub + 1))
        if "ends" in s:
            parts.append(vsd_chunk(0x9D, xform1d(*s["ends"]), version=version, level=sub + 1))
        if "line" in s:
            parts.append(vsd_chunk(0x85, s["line"], version=version, level=sub + 1))
        if "misc" in s:
            parts.append(vsd_chunk(0xA4, s["misc"], version=version, level=sub))
        for n, f in s.get("runs", ()):
            parts.append(vsd_chunk(0x94, char_ix(n, f, version), version=version, level=sub))
    for c in s.get("chunks", ()):
        parts.append(vsd_chunk(c[0], c[1], version=version, **{"level": sub, **(c[2] if len(c) > 2 else {})}))
    return b"".join(parts)


def page_stream(shapes, version, back=None):
    """A page as real files hold it: one chunk stream, the page chunk 0x15 (readPage: its background page's pointer
    index `back`, u32 at 8, v5 s16 at 0; zero bytes when None) then each shape's chunks (`shape_chunks`, the shape
    chunk at header level 1)."""
    head = bytearray(0x30)
    if back is not None:
        struct.pack_into("<h" if version == 5 else "<I", head, 0 if version == 5 else 8, back)
    return (vsd_chunk(0x15, bytes(head), version=version, cid=NONE)
            + b"".join(shape_chunks(s, version) for s in shapes))


def style_list(lay, sheets, version):
    """The styles stream (0x1A, a list) of `sheets`, each a chunk stream: the style sheet chunk 0x4A (its id "id", its
    parent line style "parent", or "data" in place of sheet_data's) and, when given, its "line" (0x85 data)."""
    ptrs = []
    for sh in sheets:
        raw = vsd_chunk(0x4A, sh["data"] if "data" in sh else sheet_data(sh.get("parent", NONE), version),
                        version=version, cid=sh["id"], level=0)
        if "line" in sh:
            raw += vsd_chunk(0x85, sh["line"], version=version, level=1)
        ptrs.append(lay.put(raw, 0x4A, 0xD0))
    return lay.put(vsd_list(ptrs, version=version, ptype=0x1A), 0x1A, 0x50)


def drawing(pages, version=11, stencils=(), extra=(), more_stencils=(), styles=(), late_styles=False, snames=()):
    """A VisioDocument laid out as real files: the trailer list holds the name list (0x32: one 0x33 name per page;
    Visio 5 one blob of 0x33 records), the style sheets (`styles`, see style_list), the stencils (0x1D: each stencil
    page 0x1E a list of shape streams, a shape's id its "id" else its index), `extra` ((type, format, stream bytes);
    real files hold their font list 0x18 before the pages), then the pages list (0x27: each page a chunk stream, then
    the name index 0xC9 naming page k by name k), then the style sheets when `late_styles`. A page is a dict: "name",
    "shapes", "background", "back" (its background page's index), "compressed" (the stream LZ77-coded, format bit 1).
    `more_stencils` is a second stencil list after the first. `snames` name the first list's stencil pages in order
    (names after the pages' in the name list, and a name index 0xC9 in the stencil list)."""
    lay = Layout(version)
    unit = 2 if version == 11 else 1
    if version == 5:  # readName2 skips an s16 (VSD5Parser::getInt), then 8-bit text to a NUL
        recs = [(0x33, struct.pack("<h", 1) + encode(n, 5) + b"\x00") for n in [p.get("name", "") for p in pages]
                + list(snames)]
        top = [lay.put(v5_records(recs), 0x32, 0x40)]
    else:
        names = [lay.put(struct.pack("<I", 1) + encode(n, version) + bytes(unit), 0x33, 0x40)
                 for n in [p.get("name", "") for p in pages] + list(snames)]
        top = [lay.put(vsd_list(names, version=version), 0x32, 0x50)]
    if styles and not late_styles:
        top.append(style_list(lay, styles, version))
    for stencil_list in (stencils, more_stencils):
        if not stencil_list:
            continue
        spages = []
        for sp in stencil_list:
            shapes = [lay.put(shape_chunks(s, version, level=0), s.get("type", 0x48), 0xD0) for s in sp]
            spages.append(lay.put(vsd_list(shapes, version=version, ptype=0x1E), 0x1E, 0x50))
        if snames and stencil_list is stencils:
            first = len(pages)
            if version == 5:
                index = struct.pack("<H", len(snames)) + b"".join(struct.pack("<HH", first + k, k)
                                                                  for k in range(len(snames)))
            else:
                index = struct.pack("<I", len(snames)) + b"".join(struct.pack("<IIIB", first + k, first + k, k, 0)
                                                                  for k in range(len(snames)))
            spages.append(lay.put(index, 0xC9, 0x40))
        top.append(lay.put(vsd_list(spages, version=version, ptype=0x1D), 0x1D, 0x50))
    top += [lay.put(raw, t, fmt) for t, fmt, raw in extra]
    ptrs = []
    for p in pages:
        raw, fmt = page_stream(p["shapes"], version, p.get("back")), 0xD0 if p.get("background") else 0xD1
        if p.get("compressed"):
            raw, fmt = lz77(raw), fmt | 2
        ptrs.append(lay.put(raw, 0x15, fmt))
    if version == 5:  # VSD5Parser::readNameIDX: an u16 count, then (name id u16, element id u16)
        index = struct.pack("<H", len(pages)) + b"".join(struct.pack("<HH", k, k) for k in range(len(pages)))
    else:
        index = struct.pack("<I", len(pages)) + b"".join(struct.pack("<IIIB", k, k, k, 0) for k in range(len(pages)))
    ptrs.append(lay.put(index, 0xC9, 0x40))
    top.append(lay.put(vsd_list(ptrs, version=version, ptype=0x27), 0x27, 0x50))
    if styles and late_styles:
        top.append(style_list(lay, styles, version))
    return lay.done(lay.put(vsd_list(top, version=version), 0x14, 0x50))


def fonts(items, version=6):
    """An `extra` font list stream (0x18, a chunk stream of 0x19 chunks): (font id, charset[, name])."""
    return (0x18, 0xD0, b"".join(vsd_chunk(0x19, font_ix(*it[1:], version=version), version=version, cid=it[0])
                                 for it in items))


def read(data, streams=None):
    ctx = Context()
    got = vsd.parse_compound(cfb_bytes({"VisioDocument": data, **(streams or {})})[0], ctx)
    return got, ctx


def lines(sec):
    """Each block of a section as text: a Para's inlines joined (Break as a newline; "hidden:" before hidden text),
    a list item after "- ", an Image as its kind, a Mermaid block as its text (checked by mermaid_ok)."""
    out = []
    for b in sec.blocks:
        if isinstance(b, Code):
            assert b.lang == "mermaid" and mermaid_ok(b.text), b
            out.append(b.text)
        elif isinstance(b, Para):
            out.append(("hidden:" if b.hidden else "")
                       + "".join(i.text if isinstance(i, Text) else "\n" for i in b.inlines))
        elif isinstance(b, ListItem):
            out.append("- " + "".join(i.text for i in b.inlines))
        elif isinstance(b, Image):
            out.append("image:" + (vsd.images.sniff(b.data) or "?"))
    return out


def test_v11_page_and_shapes():
    data = drawing([{"name": "Overview", "shapes": [
        {"id": 1, "text": "low", "pin": (1.0, 5.0)},
        {"id": 2, "text": "high", "pin": (1.0, 9.0)},
        {"id": 3, "text": "right", "pin": (4.0, 9.0)},
        {"id": 4, "text": "unplaced"},
        {"id": 5, "text": "two\nlines", "pin": (0.0, 1.0)}]}])
    doc, ctx = read(data)
    assert len(doc.blocks) == 1 and isinstance(doc.blocks[0], Section)
    sec = doc.blocks[0]
    assert sec.kind == "page" and sec.title == "Overview", sec
    # (-PinY, PinX): the PinY 9 shapes first, left to right, then PinY 5, then 1; a shape with no XForm last
    assert lines(sec) == ["high", "right", "low", "two\nlines", "unplaced"], lines(sec)
    assert sec.blocks[3].inlines == [Text("two"), Break(), Text("lines")]
    assert not ctx.skipped, ctx.skipped
    # the shapes as their own streams in the page's pointer list (vsd_document's layout) read the same
    doc, ctx = read(vsd_document(11, [[{"text": "low", "chunks": [(0x9B, xform(1.0, 5.0), {"level": 3})]},
                                       {"text": "high", "chunks": [(0x9B, xform(1.0, 9.0), {"level": 3})]}]]))
    assert lines(doc.blocks[0]) == ["high", "low"] and doc.blocks[0].title == "1" and not ctx.skipped
    # pages keep their order, each its own section; a page without a name is numbered
    doc, _ = read(drawing([{"name": "A", "shapes": [{"text": "a"}]}, {"shapes": [{"text": "b"}]}]))
    assert [(s.title, lines(s)) for s in doc.blocks] == [("A", ["a"]), ("2", ["b"])]


def test_v6_text():
    for data in (vsd_document(6, [[{"text": "Caf\xe9"}]]), drawing([{"name": "Caf\xe9", "shapes": [
            {"text": "Caf\xe9", "pin": (1.0, 1.0)}]}], version=6)):
        doc, ctx = read(data)
        assert lines(doc.blocks[0]) == ["Caf\xe9"] and not ctx.skipped
    assert doc.blocks[0].title == "Caf\xe9"


def test_v5_drawing():
    # Visio 5: 16-byte pointers and 12-byte chunks read as v6; the page name and the XForm and misc come from the
    # records of list chunks (handleChunkRecords); a stencil shape's text is inherited; a font run's charset decodes
    stencils = [[{"id": 4, "text": "Master"}]]
    shapes = [{"id": 1, "text": "low", "pin": (0.0, 1.0)}, {"id": 2, "text": "high", "pin": (0.0, 9.0)},
              {"id": 3, "master": (0, 4), "pin": (0.0, 5.0)},
              {"id": 5, "text": "quiet", "pin": (0.0, 3.0), "misc": misc(5, hide=True)},
              {"id": 6, "raw": "Мир".encode("cp1251"), "runs": [(3, 7)], "pin": (0.0, 2.0)}]
    data = drawing([{"name": "Front", "shapes": shapes}], version=5, stencils=stencils,
                   extra=[fonts([(7, 204, b"Arial")], version=5)])
    assert vsd_stream.header(data)[0] == 5
    doc, ctx = read(data)
    sec = doc.blocks[0]
    assert sec.title == "Front", sec.title
    assert lines(sec) == ["high", "Master", "hidden:quiet", "Мир", "low"], lines(sec)
    assert not ctx.skipped, ctx.skipped
    # the reader's own fixture (vsd_document's v5 lists) reads too
    doc, ctx = read(vsd_document(5, [[{"text": "A"}]]))
    assert lines(doc.blocks[0]) == ["A"] and not ctx.skipped


def test_v5_records():
    # handleChunkRecords: records in position order, each up to the next one's offset; a table past the data or a
    # count the data cannot hold gives none
    data = v5_records([(0x9B, b"abc"), (0xA4, b"defgh")])
    assert [(t, bytes(d)) for t, d in vsd._Reader(b"", 5, Context()).records(data)] == [
        (0x9B, b"abc\x00"), (0xA4, b"defgh\x00\x00\x00")]
    assert vsd._Reader(b"", 5, Context()).records(b"\x01\x00") == []
    assert vsd._Reader(b"", 5, Context()).records(b"\x00" * 8 + struct.pack("<HH", 100, 8)) == []
    # the records take from the document's item meter, past it counted `record` once
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 1
    try:
        r = vsd._Reader(b"", 5, Context())
        assert len(r.records(data)) == 1 and r.ctx.skipped == {"record": 1}
    finally:
        limits.BIN_RECORDS = old


def test_group_offsets():
    # a group at PinY 10 (LocPin 1, 2) with a child at local PinY 1: the child sits at 1 + 10 - 2 = 9 on the page,
    # above a top-level shape at PinY 8.5, as vsdx places it
    shapes = [{"id": 1, "type": 0x47, "text": "group", "pin": (5.0, 10.0, 1.0, 2.0)},
              {"id": 7, "text": "free", "pin": (0.0, 8.5)},
              {"id": 2, "parent": 1, "text": "child", "pin": (0.5, 1.0)},
              {"id": 3, "parent": 2, "text": "grandchild", "pin": (0.0, 0.25)}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    # group 10; child 1 + (10 - 2) = 9; grandchild 0.25 + child's origin (8 + 1 - 0) = 9.25; free 8.5
    assert lines(doc.blocks[0]) == ["group", "grandchild", "child", "free"], lines(doc.blocks[0])
    assert not ctx.skipped
    # a parent the page does not hold and a parent cycle place the shape at its own pin
    shapes = [{"id": 1, "parent": 9, "text": "orphan", "pin": (0.0, 3.0)},
              {"id": 2, "parent": 3, "text": "a", "pin": (0.0, 1.0)},
              {"id": 3, "parent": 2, "text": "b", "pin": (0.0, 2.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    assert sorted(lines(doc.blocks[0])) == ["a", "b", "orphan"]
    assert not ctx.skipped


def test_group_depth():
    # a chain of groups deeper than MAX_DEPTH: shapes past it are left out, counted `depth` once for the page
    n = limits.MAX_DEPTH + 3
    shapes = [{"id": k + 1, "parent": k, "type": 0x47, "text": f"s{k}", "pin": (0.0, 1.0)} for k in range(n)]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    assert len(lines(doc.blocks[0])) == limits.MAX_DEPTH + 1
    assert ctx.skipped == {"depth": 1}, ctx.skipped


def test_group_without_xform():
    # a group with no XForm adds nothing to its children's pins, and passes on its own parent's offset
    shapes = [{"id": 1, "type": 0x47, "text": "top", "pin": (0.0, 10.0, 0.0, 0.0)},
              {"id": 2, "parent": 1, "type": 0x47},
              {"id": 3, "parent": 2, "text": "deep", "pin": (0.0, -5.0)},
              {"id": 4, "text": "mid", "pin": (0.0, 6.0)},
              {"id": 5, "text": "low", "pin": (0.0, 4.0)},
              {"id": 6, "parent": 7, "type": 0x47},
              {"id": 7, "parent": 6, "text": "loop", "pin": (0.0, 1.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    assert lines(doc.blocks[0]) == ["top", "mid", "deep", "low", "loop"], lines(doc.blocks[0])
    assert not ctx.skipped


def test_parented_ids_compact():
    # a parented shape with no XForm keeps only its parent for the group walk, not a tuple of four empty pins: Task
    # 16's probe of 1.9M such shapes (27 MiB coded) peaked at 404 MiB, 225 MiB of it these entries
    n = 30_000

    def peak(parent):
        data = drawing([{"shapes": [{"id": k, "parent": parent} for k in range(2, n + 2)]}])
        tracemalloc.start()
        try:
            _read_doc(data)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    extra = peak(1) - peak(0)  # parent 0 keeps no entry
    assert extra < 115 * n, extra / n  # about 146 bytes a shape with the tuple, 89 without


def test_fields():
    # a string field shows the shape's 0x2D name it points at; a number field its formatted value; the field list
    # (0x66) gives their order; a mark with no field left shows nothing
    order = struct.pack("<II", 0, 8) + struct.pack("<II", 4, 3)
    chunks = [(0x66, order, {"list_": 2}), (0xA1, number_field(41.6, 2), {"cid": 3}), (0xA1, text_field(5), {"cid": 4}),
              (0x2D, "Alice".encode("utf-16-le"), {"cid": 5, "level": 3})]
    doc, ctx = read(drawing([{"shapes": [{"text": "By \ufffc, page \ufffc\ufffc.", "chunks": chunks}]}]))
    assert lines(doc.blocks[0]) == ["By Alice, page 42."], lines(doc.blocks[0])
    # no field list: the fields in id order; a date and a unit
    chunks = [(0xA1, number_field(2.5, 3, cell=65), {"cid": 2}),
              (0xA1, number_field(30060.0, 204, cell=40), {"cid": 1})]
    doc, _ = read(drawing([{"shapes": [{"text": "\ufffc / \ufffc", "chunks": chunks}]}]))
    assert lines(doc.blocks[0]) == ["1982-04-19 / 2 in"], lines(doc.blocks[0])
    # Visio 2000: the 0x1E byte marks a field, the format block starts at 0x24, names are 8-bit
    chunks = [(0xA1, text_field(1), {"cid": 0}), (0x2D, b"Bob", {"cid": 1, "level": 3}),
              (0xA1, number_field(7.25, 6, start=0x24), {"cid": 1})]
    data = drawing([{"shapes": [{"text": "\x1e:\x1e", "chunks": chunks}]}], version=6)
    doc, _ = read(data)
    assert lines(doc.blocks[0]) == ["Bob:7.25"], lines(doc.blocks[0])


def dib(w=1, h=1):
    """A 24-bit DIB (no file header), as Visio stores a bitmap's foreign data."""
    row = b"\x00\x00\xff" * w
    row += bytes(-len(row) % 4)
    return struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(row) * h, 0, 0, 0, 0) + row * h


def foreign_type(kind, fmt=0, mode=0):
    """A 0x98 chunk: four unit/double pairs, the type u16, the map mode u16, 9 bytes, the format u32."""
    return b"".join(b"\x00" + struct.pack("<d", 1.0) for _ in range(4)) + struct.pack("<HH", kind, mode) + bytes(9) \
        + struct.pack("<I", fmt)


def test_pictures():
    def foreign(kind, fmt, data, pin, text=None):
        s = {"type": 0x4E, "pin": pin, "chunks": [(0x98, foreign_type(kind, fmt), {"level": 3}),
                                                  (0x0C, data, {"level": 3})]}
        if text is not None:
            s["text"] = text
        return s

    shapes = [foreign(1, 4, PNG, (0.0, 3.0), "caption"), foreign(4, 0, EMF, (0.0, 2.0)),
              foreign(1, 0, dib(), (0.0, 1.0)), {"text": "top", "pin": (0.0, 9.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    # a shape's text and picture stay together in reading order; the DIB gets its file header and becomes a PNG
    assert lines(doc.blocks[0]) == ["top", "caption", "image:png", "image:emf", "image:png"], lines(doc.blocks[0])
    assert not ctx.skipped
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        md = markdown.render(doc, out / "assets", False, ctx.skipped)
        assert "![](assets/img-001.png)" in md and "Not viewable here: assets/img-002.emf" in md, md
        assert (out / "assets" / "img-002.emf").read_bytes() == EMF
    # an OLE object (type 2) is counted, not shown; a picture with no data or of no kind is counted
    shapes = [foreign(2, 0, b"\x01" * 40, (0.0, 1.0)), foreign(1, 4, b"no picture here at all", (0.0, 2.0)),
              {"type": 0x4E, "chunks": [(0x98, foreign_type(1, 4), {"level": 3})]}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    assert lines(doc.blocks[0]) == [] and ctx.skipped == {"object": 1, "image": 2}, ctx.skipped


def test_image_bytes():
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(PNG) * 2
    try:
        shapes = [{"type": 0x4E, "pin": (0.0, float(k)), "chunks": [(0x98, foreign_type(1, 4), {"level": 3}),
                                                                   (0x0C, PNG, {"level": 3})]} for k in range(3)]
        doc, ctx = read(drawing([{"shapes": shapes}]))
    finally:
        limits.IMAGE_BYTES = old
    assert lines(doc.blocks[0]) == ["image:png", "image:png"] and ctx.skipped == {"image": 1}, ctx.skipped


def test_background_page():
    doc, ctx = read(drawing([{"name": "Front", "shapes": [{"text": "f"}]},
                             {"name": "Back", "background": True, "shapes": [{"text": "b"}]}]))
    assert [(s.title, lines(s), s.hidden) for s in doc.blocks] == [("Front", ["f"], ""),
                                                                   ("Back", ["Background page", "b"], "")]
    doc, _ = read(vsd_document(11, [{"shapes": [{"text": "x"}], "background": True}]))
    assert lines(doc.blocks[0]) == ["Background page", "x"]
    for version in (11, 6, 5):
        pages = [{"name": "Front", "shapes": [{"text": "f"}], "back": 1},
                 {"name": "Back", "background": True, "shapes": [{"text": "b"}], "back": 1},
                 {"name": "Other", "shapes": [{"text": "o"}], "back": 0}]
        doc, _ = read(drawing(pages, version=version))
        assert [(s.title, lines(s)) for s in doc.blocks] == [
            ("Front", ["Background page: Back", "f"]), ("Back", ["Background of: Front", "b"]),
            ("Other", ["o"])], (version, [(s.title, lines(s)) for s in doc.blocks])


def test_group_and_connector_texts():
    for version in (11, 6):
        shapes = [{"id": 1, "type": 0x47, "pin": (1.0, 5.0)},
                  {"id": 2, "parent": 1, "text": "Routes", "pin": (0.0, 0.5)},
                  {"id": 3, "parent": 1, "pin": (0.0, 0.0)},
                  {"id": 10, "type": 0x47, "pin": (5.0, 5.0)},
                  {"id": 11, "parent": 10, "text": "Bus", "pin": (0.0, 0.5)},
                  {"id": 20, "pin": (2.0, 5.5), "ends": (2.0, 5.5, 5.0, 5.5), "misc": misc(version, ends=(1, 10))},
                  {"id": 21, "parent": 20, "text": "-have", "pin": (0.1, 0.0)},
                  {"id": 22, "parent": 20, "text": "1..*", "pin": (2.8, 0.0)}]
        doc, ctx = read(drawing([{"shapes": shapes}], version=version))
        assert lines(doc.blocks[0]) == ['flowchart LR\n  n1["Routes"]\n  n2["Bus"]\n  n1 -->|"-have / / 1..*"| n2'], (
            version, lines(doc.blocks[0]))
        assert not ctx.skipped, ctx.skipped


def point(x, y):
    """A MoveTo or LineTo chunk's data (readMoveTo): X and Y, each a unit byte and a double."""
    return b"\x00" + struct.pack("<d", x) + b"\x00" + struct.pack("<d", y)


def test_connector_route_from_geometry():
    route = [(0x89, bytes(16)), (0x8A, point(0.0, 0.0)), (0x8B, point(3.0, 0.0)), (0x8B, point(3.0, 2.0)),
             (0x89, bytes(16)), (0x8B, point(9.0, 9.0))]
    for version in (11, 6):
        shapes = [{"id": 1, "text": "A", "pin": (1.0, 5.0, 0.5, 0.5, 1.0, 1.0)},
                  {"id": 2, "text": "B", "pin": (5.0, 7.5, 0.5, 0.5, 1.0, 1.0)},
                  {"id": 20, "pin": (3.0, 6.0, 1.5, 1.0, 3.0, 2.0), "ends": (1.5, 5.0, 4.5, 7.0),
                   "misc": misc(version, ends=(1, 2)), "chunks": route},
                  {"id": 21, "parent": 20, "text": "-has", "pin": (2.3, -0.15, 0.2, 0.1, 0.4, 0.2)},
                  {"id": 22, "parent": 20, "text": "1", "pin": (3.1, 1.6, 0.1, 0.1, 0.2, 0.2)},
                  {"id": 30, "text": "uses", "pin": (3.0, 5.08, 0.5, 0.1, 1.0, 0.2)}]
        doc, ctx = read(drawing([{"shapes": shapes}], version=version))
        assert lines(doc.blocks[0]) == ['flowchart LR\n  n1["B"]\n  n2["A"]\n  n2 -->|"-has / uses / 1"| n1'], (
            version, lines(doc.blocks[0]))
        assert not ctx.skipped, ctx.skipped


def test_own_master_instances_in_a_group_keep_their_ends():
    stencils = [[{"id": 1, "pin": (0.0, 0.0)}], [{"id": 1, "pin": (0.0, 0.0)}]]
    for version in (11, 6):
        shapes = [{"id": 1, "type": 0x47, "pin": (5.0, 5.0, 5.0, 5.0, 10.0, 10.0)},
                  {"id": 2, "parent": 1, "text": "DMZ", "pin": (5.0, 9.0)},
                  {"id": 3, "parent": 1, "master": (0, 1), "pin": (3.0, 5.0)},
                  {"id": 4, "parent": 1, "master": (1, 1), "pin": (7.0, 5.0)},
                  {"id": 5, "parent": 1, "misc": misc(version, ends=(3, 4))},
                  {"id": 6, "text": "Internet", "pin": (-5.0, 5.0)},
                  {"id": 7, "misc": misc(version, ends=(6, 3))}]
        doc, ctx = read(drawing([{"shapes": shapes}], version=version, stencils=stencils,
                                snames=["Firewall", "Web server.3"]))
        want = 'flowchart LR\n  n1["Internet"]\n  n2["Firewall"]\n  n3["Web server"]\n  n2 --> n3\n  n1 --> n2'
        assert lines(doc.blocks[0]) == ["DMZ", want], (version, lines(doc.blocks[0]))
        assert not ctx.skipped, ctx.skipped


def test_master_names_and_kinds():
    stencils = [[{"id": 1, "pin": (0.0, 0.0)}], [{"id": 1, "pin": (0.0, 0.0)}],
                [{"id": 1, "type": 0x47, "pin": (0.0, 0.0)}, {"id": 2, "parent": 1, "pin": (0.0, 0.0)}]]
    for version in (11, 6):
        shapes = [{"id": 1, "master": (0, 1), "text": "ok?", "pin": (0.0, 9.0)},
                  {"id": 2, "master": (1, 1), "pin": (0.0, 5.0)},
                  {"id": 3, "misc": misc(version, ends=(1, 2))},
                  {"id": 4, "type": 0x47, "master": (2, NONE), "pin": (5.0, 9.0)},
                  {"id": 5, "parent": 4, "master": (2, 2), "pin": (0.0, 0.0)},
                  {"id": 6, "misc": misc(version, ends=(5, 2))}]
        doc, ctx = read(drawing([{"shapes": shapes}], version=version, stencils=stencils,
                                snames=["Decision", "Firewall", "Class"]))
        want = 'flowchart TD\n  n1{"ok?"}\n  n2["Class"]\n  n3["Firewall"]\n  n1 --> n3\n  n2 --> n3'
        assert lines(doc.blocks[0]) == [want], (version, lines(doc.blocks[0]))
        assert not ctx.skipped, ctx.skipped


def test_inflated_stream():
    pages = [{"name": "P", "shapes": [{"text": "one", "pin": (0.0, 2.0)}, {"text": "two", "pin": (0.0, 1.0)}]}]
    stored, _ = read(drawing(pages))
    packed, ctx = read(drawing([dict(pages[0], compressed=True)]))
    assert packed == stored and lines(packed.blocks[0]) == ["one", "two"] and not ctx.skipped
    # vsd_document's coded lists, blobs and shape streams
    pages = [[{"text": "A", "compressed": True}]]
    doc, ctx = read(vsd_document(11, [{"shapes": pages[0], "compressed": True}]))
    assert lines(doc.blocks[0]) == ["A"] and not ctx.skipped


def test_text_caps():
    # a shape's text past MAX_TEXT is cut there, counted `text`; text past the document's MAX_OUTPUT room is left
    # out, counted `text`, as vsdx charges it (its characters and LINE per break), and LINE for the block
    doc, ctx = read(drawing([{"shapes": [{"text": "x" * (vsdx.MAX_TEXT + 5)}]}]))
    assert lines(doc.blocks[0]) == ["x" * vsdx.MAX_TEXT] and ctx.skipped == {"text": 1}, ctx.skipped
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 9 + 2 * vsdx.LINE
    try:
        doc, ctx = read(drawing([{"shapes": [{"text": "abcd\nefgh", "pin": (0.0, 3.0)},
                                             {"text": "z", "pin": (0.0, 2.0)}, {"text": "y", "pin": (0.0, 1.0)}]}]))
    finally:
        limits.MAX_OUTPUT = old
    assert lines(doc.blocks[0]) == ["abcd\nefgh"] and ctx.skipped == {"text": 2}, ctx.skipped


def test_record_meter():
    # the per-document item meter (BIN_RECORDS, read at call time) bounds the name index records; past it the rest
    # is not read, counted `record` once
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 100
    try:
        doc, ctx = read(drawing([{"name": "Named", "shapes": [{"text": "t"}]}]))
        assert doc.blocks[0].title == "Named" and not ctx.skipped
        # 500 records, only the last naming page 0
        index = struct.pack("<I", 500) + struct.pack("<IIIB", 0, 0, 9, 0) * 499 + struct.pack("<IIIB", 0, 0, 0, 0)
        lay = Layout()
        name = lay.put(struct.pack("<I", 1) + encode("Late", 11) + bytes(2), 0x33, 0x40)
        names = lay.put(vsd_list([name]), 0x32, 0x50)
        page = lay.put(page_stream([{"text": "t"}], 11), 0x15, 0xD1)
        idx = lay.put(index, 0xC9, 0x40)
        pages = lay.put(vsd_list([page, idx]), 0x27, 0x50)
        doc, ctx = read(lay.done(lay.put(vsd_list([names, pages]), 0x14, 0x50)))
    finally:
        limits.BIN_RECORDS = old
    assert doc.blocks[0].title == "1" and ctx.skipped == {"record": 1}, ctx.skipped


def test_shape_guard():
    # a shape whose reading fails is left out, counted `shape` once; the page and the other shapes stay
    real = vsd._Reader.xform

    def boom(self, shape, data):
        if data == xform(0.0, 7.0):
            raise struct.error("bad")
        return real(self, shape, data)

    vsd._Reader.xform = boom
    try:
        doc, ctx = read(drawing([{"shapes": [{"text": "bad", "pin": (0.0, 7.0)},
                                             {"text": "good", "pin": (0.0, 1.0)}]}]))
    finally:
        vsd._Reader.xform = real
    assert lines(doc.blocks[0]) == ["good"] and ctx.skipped == {"shape": 1}, ctx.skipped


def test_stencil_shapes_not_pages():
    # stencil (master) shapes are not page content
    doc, ctx = read(vsd_document(11, [[{"text": "page"}]], masters=[[{"text": "master"}]]))
    assert [lines(s) for s in doc.blocks] == [["page"]] and not ctx.skipped


def test_master_text():
    # a shape naming stencil shape 2 (stencil page 0) with no text chunk of its own takes that shape's text, its
    # fields' values included; a shape with text of its own keeps it; a stencil shape the master lacks gives nothing
    def stencils(version):
        mark, start = ("￼", 0x36) if version == 11 else ("", 0x24)
        return [[{"id": 1, "text": "first"},
                 {"id": 2, "text": "Pump " + mark, "chunks": [(0xA1, number_field(3.0, 2, start=start), {"cid": 0})]}],
                [{"text": "other page"}]]

    shapes = [{"id": 1, "master": (0, 2), "pin": (0.0, 9.0)}, {"id": 2, "master": (0, 2), "pin": (0.0, 8.0)},
              {"id": 3, "master": (0, 1), "text": "own", "pin": (0.0, 7.0)},
              {"id": 4, "master": (1, 0), "pin": (0.0, 6.0)}, {"id": 5, "master": (0, 9), "pin": (0.0, 5.0)}]
    for version in (11, 6):
        doc, ctx = read(drawing([{"shapes": shapes}], version=version, stencils=stencils(version)))
        assert lines(doc.blocks[0]) == ["Pump 3", "Pump 3", "own", "other page"], (version, lines(doc.blocks[0]))
        assert not ctx.skipped, ctx.skipped
    # a master page past MASTERS is not read, nor one the file does not hold: their shapes count `stencil`
    old = vsdx.MASTERS
    vsdx.MASTERS = 1
    try:
        doc, ctx = read(drawing([{"shapes": shapes + [{"id": 6, "master": (7, 0)}]}], stencils=stencils(11)))
    finally:
        vsdx.MASTERS = old
    assert lines(doc.blocks[0]) == ["Pump 3", "Pump 3", "own"] and ctx.skipped == {"stencil": 2}, ctx.skipped
    # stencils read after the pages give the pages nothing (libvisio's one walk): counted, never read later
    data = vsd_document(11, [[{"data": shape_data(master=(0, 0))}]], masters=[[{"text": "late"}]])
    doc, ctx = read(data)
    assert lines(doc.blocks[0]) == [] and ctx.skipped == {"stencil": 1}, ctx.skipped


def test_master_text_only_without_own_text():
    # an empty text chunk of its own: the shape stays empty, the stencil text is not taken, nothing is counted
    stencils = [[{"id": 2, "text": "Master"}]]
    doc, ctx = read(drawing([{"shapes": [{"master": (0, 2), "text": ""}]}], stencils=stencils))
    assert lines(doc.blocks[0]) == [] and not ctx.skipped, ctx.skipped


def test_master_ids_resolve_once():
    # a stencil shape naming its own page or itself as master, and two stencil pages naming each other: only
    # completed stencil pages are looked in, so nothing loops and each stencil shape is read once
    stencils = [[{"id": 1, "master": (0, 1)}, {"id": 2, "master": (1, 1), "text": "two"}],
                [{"id": 1, "master": (0, 2)}, {"id": 3, "master": (1, 3)}]]
    shapes = [{"master": (0, 1), "pin": (0.0, 3.0)}, {"master": (1, 1), "pin": (0.0, 2.0)},
              {"master": (1, 3), "pin": (0.0, 1.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}], stencils=stencils))
    assert lines(doc.blocks[0]) == ["two"] and not ctx.skipped, (lines(doc.blocks[0]), ctx.skipped)


def test_connections():
    # a connector glued at both ends (readMisc's blocks: v11 from 45, v6 from 23) is an edge of the page's Mermaid
    # block, placed at its first node's slot; its text is the label and leaves the flow, as do the nodes' texts. One
    # glued end and no XForm1D is a loose connector: counted, its text kept
    for version in (11, 6):
        shapes = [{"id": 1, "text": "Start", "pin": (0.0, 9.0)}, {"id": 2, "text": "End", "pin": (0.0, 5.0)},
                  {"id": 3, "text": "flows", "pin": (0.0, 7.0), "misc": misc(version, ends=(1, 2))},
                  {"id": 4, "text": "loose", "pin": (0.0, 3.0), "misc": misc(version, ends=(1,))}]
        doc, ctx = read(drawing([{"shapes": shapes}], version=version))
        got = lines(doc.blocks[0])
        assert got == ['flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 -->|"flows"| n2', "loose"], (version, got)
        assert not any("Connections:" in t for t in got)
        assert ctx.skipped == {"connector_loose": 1}, ctx.skipped
    # a block of another kind, or with the pattern broken, is passed over by its length
    odd = bytearray(misc(11, ends=(1, 2)))
    odd[45 + 6] = 0x75  # the first block is no longer a glue block: the second gives the begin only
    doc, ctx = read(drawing([{"shapes": [{"id": 1, "text": "a"}, {"id": 3, "text": "c", "misc": bytes(odd)}]}]))
    assert lines(doc.blocks[0]) == ["a", "c"] and ctx.skipped == {"connector_loose": 1}, lines(doc.blocks[0])


def test_connection_budget_shared():
    # one ConnectionBudget for the document: CONNECTIONS 1, two pages with one connector each -> the first an edge,
    # the second cut (counted once), its text kept in the flow
    def page(tag):
        return {"shapes": [{"id": 1, "text": f"A{tag}"}, {"id": 2, "text": f"B{tag}"},
                           {"id": 3, "text": f"c{tag}", "misc": misc(11, ends=(1, 2))}]}

    old = diagram.CONNECTIONS
    diagram.CONNECTIONS = 1
    try:
        doc, ctx = read(drawing([page(1), page(2)]))
    finally:
        diagram.CONNECTIONS = old
    assert [lines(s) for s in doc.blocks] == [['flowchart TD\n  n1["A1"]\n  n2["B1"]\n  n1 -->|"c1"| n2'],
                                              ["A2", "B2", "c2"]], [lines(s) for s in doc.blocks]
    assert ctx.skipped == {"connections_cut": 1}, ctx.skipped


def test_hidden_text():
    # readMisc's flag 0x20: the text is a hidden Para, left out unless --hidden and counted as hidden; a stencil
    # shape's flag is inherited unless the shape has a misc chunk of its own. A hidden shape is a blank node that
    # keeps its slot; a connector's hidden text is no label and stays in the flow, hidden
    stencils = [[{"id": 1, "text": "veiled", "misc": misc(hide=True)}]]
    shapes = [{"id": 1, "text": "secret", "pin": (0.0, 9.0), "misc": misc(hide=True)},
              {"id": 2, "master": (0, 1), "pin": (0.0, 8.0)},
              {"id": 3, "master": (0, 1), "pin": (0.0, 7.0), "misc": misc()},
              {"id": 4, "text": "link", "pin": (0.0, 6.0), "misc": misc(hide=True, ends=(1, 3))}]
    doc, ctx = read(drawing([{"shapes": shapes}], stencils=stencils))
    assert lines(doc.blocks[0]) == ['flowchart TD\n  n2["veiled"]\n  n1 --> n2', "hidden:secret",
                                    "hidden:veiled", "hidden:link"], lines(doc.blocks[0])
    assert not ctx.skipped
    with tempfile.TemporaryDirectory() as tmp:
        shown = markdown.render(doc, Path(tmp) / "assets", True, ctx.skipped)
        left = markdown.render(doc, Path(tmp) / "assets", False, ctx.skipped)
    assert "secret" in shown and "secret" not in left and "(3 hidden places left out)" in left, left


AB = [{"id": 1, "text": "A", "pin": (1.0, 5.0, 1.0, 0.5, 2.0, 1.0)},  # boxes (0, 4.5)-(2, 5.5) and (5, 4.5)-(7, 5.5)
      {"id": 2, "text": "B", "pin": (6.0, 5.0, 1.0, 0.5, 2.0, 1.0)}]
INFERRED_AB = 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 -->{} n2'


def test_xform_extras_and_1d():
    # 0x9B's Width, Height, Angle and flips give each shape its box (about the pin, as vsdx), 0x9D its ends: an
    # unglued 1-D shape whose ends lie in two boxes is an inferred edge, its text the label
    go = {"id": 3, "text": "go", "pin": (3.5, 5.0, 2.0, 0.0, 4.0, 0.0), "ends": (1.5, 5.0, 5.5, 5.0)}
    # an XForm cut before Width (the pin only) gives no box: nothing to land on, the connector stays loose
    short = [{"id": k, "text": t, "chunks": [(0x9B, xform(x, 5.0, 1.0, 0.5, 2.0, 1.0)[:19], {"level": 3})]}
             for k, t, x in ((1, "A", 1.0), (2, "B", 6.0))]
    # turned: A is 4 x 1 turned a quarter about its pin, so (0, 1.8) is in it; B is mirrored by FlipX about its pin
    turned = [{"id": 1, "text": "A", "pin": (0.0, 0.0, 2.0, 0.5, 4.0, 1.0, math.pi / 2)},
              {"id": 2, "text": "B", "pin": (10.0, 0.0, 0.0, 0.5, 2.0, 1.0, 0.0, 1)},
              {"id": 3, "ends": (0.0, 1.8, 9.0, 0.0)}]
    # a connector in a group: its ends are in the group's frame, offset by the group's origin (50, 50)
    grouped = AB + [{"id": 10, "type": 0x47, "pin": (50.0, 50.0, 0.0, 0.0, 0.5, 0.5)},
                    {"id": 11, "parent": 10, "ends": (-48.5, -45.0, -44.5, -45.0)}]
    # master entries carry the XForm's size and the XForm1D: B and the connector take their master's
    stencils = [[{"id": 7, "text": "B", "pin": (6.0, 5.0, 1.0, 0.5, 2.0, 1.0)},
                 {"id": 8, "ends": (1.5, 5.0, 5.5, 5.0)}]]
    mastered = [AB[0], {"id": 2, "master": (0, 7)}, {"id": 3, "master": (0, 8)}]
    pages = [{"shapes": AB + [go]}, {"shapes": short + [go]}, {"shapes": turned}, {"shapes": grouped},
             {"shapes": mastered}]
    doc, ctx = read(drawing(pages, stencils=stencils))
    got = [lines(s) for s in doc.blocks]
    assert got[0] == [INFERRED_AB.format('|"go"|')], got[0]
    assert got[1] == ["A", "go", "B"], got[1]
    assert got[2] == [INFERRED_AB.format("")], got[2]
    assert got[3] == [INFERRED_AB.format("")], got[3]
    assert got[4] == [INFERRED_AB.format("")], got[4]
    assert not ctx.skipped, ctx.skipped  # the short one lands nowhere, unglued and arrowless: a decoration
    # a 0x9D cut short (BeginX, BeginY and EndX only) is no end point: loose
    cut = {"id": 3, "chunks": [(0x9D, xform1d(1.5, 5.0, 5.5, 5.0)[:27], {"level": 3})]}
    doc, ctx = read(drawing([{"shapes": AB + [cut]}]))
    assert lines(doc.blocks[0]) == ["A", "B"] and ctx.skipped == {"connector_loose": 1}, ctx.skipped


def test_line_markers():
    # 0x85's start (byte 25) and end (26) markers, nonzero an arrowhead, 0 none; no 0x85 is not known ('-->')
    def page(**conn):
        return {"shapes": [{"id": 1, "text": "A", "pin": (0.0, 9.0)}, {"id": 2, "text": "B", "pin": (0.0, 5.0)},
                           {"id": 3, "misc": misc(conn.pop("version", 11), ends=(1, 2)), **conn}]}

    want = 'flowchart TD\n  n1["A"]\n  n2["B"]\n  {}'
    for version in (11, 6):
        lines_ = [line(0, 13), line(5, 13), line(0, 0), line(4, 0), line(4, 13)[:26], None]
        pages = [page(version=version, **({"line": d} if d is not None else {})) for d in lines_]
        doc, ctx = read(drawing(pages, version=version))
        got = [lines(s) for s in doc.blocks]
        assert got == [[want.format(e)] for e in ("n1 --> n2", "n1 <--> n2", "n1 --- n2", "n2 --> n1",
                                                  "n2 --> n1", "n1 --> n2")], (version, got)
        assert not ctx.skipped, ctx.skipped
    # a master shape's markers when the shape has no 0x85 of its own; its own one wins
    stencils = [[{"id": 1, "line": line(0, 0)}]]
    doc, ctx = read(drawing([page(master=(0, 1)), page(master=(0, 1), line=line(0, 9))], stencils=stencils))
    assert [lines(s) for s in doc.blocks] == [[want.format("n1 --- n2")], [want.format("n1 --> n2")]], doc
    assert not ctx.skipped, ctx.skipped


def test_style_sheet_arrow():
    # style sheets: 0x4A chunks in the styles stream 0x1A, each its parent line style at 0x22 and its own 0x85; a
    # shape's line style (the u32 at 42 of its chunk) is walked up the chain to the first sheet with a 0x85
    styles = [{"id": 2, "line": line(4, 0)}, {"id": 3, "parent": 2}, {"id": 4, "line": line(0, 0)},
              {"id": 5, "parent": 6}, {"id": 6, "parent": 5},
              {"id": 7, "data": sheet_data(4)[:0x25]}]  # cut before its parent: no parent

    def page(**conn):
        return {"shapes": [{"id": 1, "text": "A", "pin": (0.0, 9.0)}, {"id": 2, "text": "B", "pin": (0.0, 5.0)},
                           {"id": 3, "misc": misc(11, ends=(1, 2)), **conn}]}

    want = 'flowchart TD\n  n1["A"]\n  n2["B"]\n  {}'
    stencils = [[{"id": 1, "line_style": 4, "pin": (0.0, 0.0)}]]  # (a line style alone keeps no master entry)
    cases = [(dict(line_style=3), "n2 --> n1"),                    # the marker only on sheet 2, through sheet 3
             (dict(line_style=4), "n1 --- n2"),                    # explicit 0s: no arrowhead
             (dict(line_style=4, line=line(0, 9)), "n1 --> n2"),   # the shape's own 0x85 first
             (dict(line_style=5), "n1 --> n2"),                    # a cycle: none found, not known
             (dict(line_style=9), "n1 --> n2"),                    # no such sheet
             (dict(line_style=7), "n1 --> n2"),                    # a sheet cut short has no parent
             (dict(master=(0, 1)), "n1 --- n2"),                   # no line style of its own: its master's
             (dict(master=(0, 1), line_style=3), "n2 --> n1"),     # its own line style first
             (dict(data=shape_data(line_style=4)[:45]), "n1 --> n2")]  # a shape chunk cut before its line style
    doc, ctx = read(drawing([page(**c) for c, _ in cases], styles=styles, stencils=stencils))
    got = [lines(s) for s in doc.blocks]
    assert got == [[want.format(e)] for _, e in cases], got
    assert not ctx.skipped, ctx.skipped
    # the chain walks at most MAX_DEPTH sheets (read at call time): 10 -> 11 -> 12 -> 13, the marker on 13
    chain = [{"id": 10, "parent": 11}, {"id": 11, "parent": 12}, {"id": 12, "parent": 13}, {"id": 13, "line": line(4)}]
    old = limits.MAX_DEPTH
    limits.MAX_DEPTH = 3
    try:
        doc, ctx = read(drawing([page(line_style=10), page(line_style=11)], styles=chain))
    finally:
        limits.MAX_DEPTH = old
    assert [lines(s) for s in doc.blocks] == [[want.format("n1 --> n2")], [want.format("n2 --> n1")]], doc
    # style sheets read after the pages give them nothing (libvisio's one walk)
    doc, ctx = read(drawing([page(line_style=4)], styles=styles, late_styles=True))
    assert lines(doc.blocks[0]) == [want.format("n1 --> n2")] and not ctx.skipped, lines(doc.blocks[0])
    # each style sheet and its 0x85, each XForm's size, XForm1D, 0x85 and shape line style read takes an item of the
    # BIN_RECORDS meter: past it nothing is read, counted `record` once
    from types import SimpleNamespace
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 2
    try:
        r = vsd._Reader(b"", 11, Context())
        r.styles = 1
        r.style_sheet(Chunk(0x4A, 2, 0, 1, sheet_data()))
        r.style_line(line(4, 0))
        r.style_sheet(Chunk(0x4A, 3, 0, 1, sheet_data(2)))
        s = SimpleNamespace(pin=None, geo=None, ends=None, arrows=None)
        r.xform(s, xform(1.0, 2.0, 0.0, 0.0, 3.0, 4.0))
        r.xform1d(s, xform1d(1.0, 2.0, 3.0, 4.0))
        r.line(s, line(4, 4))
    finally:
        limits.BIN_RECORDS = old
    assert r.sheets == {2: [None, (True, False)]} and r.ctx.skipped == {"record": 1}, (r.sheets, r.ctx.skipped)
    assert (s.pin, s.geo, s.ends, s.arrows) == ([1.0, 2.0, 0.0, 0.0], None, None, None), s


def test_v5_inference_only():
    # Visio 5 has no glue (readMisc's flags only), so its connectors are inferred from their XForm1D; its 0x85
    # markers are at 21 and 22 (VSD5Parser::readLine); a v5 instance copies its master's text only, no XForm1D
    stencils = [[{"id": 7, "ends": (1.5, 5.0, 5.5, 5.0)}]]
    shapes = AB + [{"id": 3, "ends": (1.5, 5.0, 5.5, 5.0), "line": line(4, 0, version=5), "misc": misc(5)},
                   {"id": 4, "text": "x", "pin": (3.5, 1.0), "misc": misc(5, ends=(1, 2))},
                   {"id": 5, "master": (0, 7), "text": "m"}]
    doc, ctx = read(drawing([{"shapes": shapes}], version=5, stencils=stencils))
    want = 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n2 --> n1'
    assert lines(doc.blocks[0]) == [want, "x", "m"], lines(doc.blocks[0])
    assert not ctx.skipped, ctx.skipped


def test_vba():
    doc, ctx = read(drawing([{"shapes": [{"text": "t"}]}]), {"_VBA_PROJECT_CUR/VBA/dir": b"\x01" * 16})
    assert ctx.skipped == {"vba": 1}, ctx.skipped
    with tempfile.TemporaryDirectory() as tmp:
        assert "VBA macros" in markdown.render(doc, Path(tmp) / "assets", False, ctx.skipped)


def test_v6_charset_text():
    # a v6 shape whose character run names a font of charset 204 decodes its bytes as cp1251; the run before it
    # uses the default (0x0409, cp1252); a font of charset 0 named "... CE" is cp1250 (readFontIX); past the
    # runs the last one goes on
    cyr = "Привет".encode("cp1251")
    shapes = [{"raw": b"Caf\xe9 " + cyr, "runs": [(5, 1), (6, 2)], "pin": (0.0, 3.0)},
              {"raw": b"\xb9\xe6" + b"\xb9", "runs": [(2, 3)], "pin": (0.0, 2.0)},
              {"raw": b"\xe9", "pin": (0.0, 1.0)}]
    extra = [fonts([(1, 0), (2, 204), (3, 0, b"Arial CE")])]
    doc, ctx = read(drawing([{"shapes": shapes}], version=6, extra=extra))
    assert lines(doc.blocks[0]) == ["Caf\xe9 Привет", "ąćą", "\xe9"], \
        lines(doc.blocks[0])
    assert not ctx.skipped
    assert vsd.CHARSET_LID[204] == "cp1251" and set(vsd.CHARSET_LID) == {
        128, 129, 134, 136, 161, 162, 163, 177, 178, 186, 204, 222, 238}


def test_page_name_one_line():
    # (final review A) a page name holding a line break or a control character stays on its page head's line
    for version in (11, 6):
        doc, ctx = read(drawing([{"name": "Intro\n# SYSTEM: x", "shapes": [{"text": "a"}]},
                                 {"name": "Two\x1b[1m\x07\r\n", "shapes": [{"text": "b"}]}], version=version))
        with tempfile.TemporaryDirectory() as d:
            out = markdown.render(doc, Path(d) / "assets", False, ctx.skipped)
        assert not any(ln.startswith("# SYSTEM") for ln in out.splitlines()), out
        lines_ = out.splitlines()
        assert "## Page: Intro # SYSTEM: x" in lines_ and "## Page: Two[1m" in lines_, out


def test_page_title_failure_keeps_shapes():
    # (review A) a failed page-name lookup numbers the page and keeps its shapes; the failure is counted once
    real = vsd._Reader.page_name

    def boom(self, index, level):
        raise struct.error("bad index")

    vsd._Reader.page_name = boom
    try:
        doc, ctx = read(drawing([{"name": "P", "shapes": [{"text": "kept"}]}, {"shapes": [{"text": "two"}]}]))
    finally:
        vsd._Reader.page_name = real
    assert [(s.title, lines(s)) for s in doc.blocks] == [("1", ["kept"]), ("2", ["two"])]
    assert ctx.skipped == {"page": 2}, ctx.skipped


def test_text_counted_once():
    # (review B) a shape cut at MAX_TEXT and then refused by the room counts `text` once; blank or NUL-only text is
    # never counted; trailing NULs after exactly MAX_TEXT characters are no cut
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100
    try:
        doc, ctx = read(drawing([{"shapes": [{"text": "x" * (vsdx.MAX_TEXT + 5)}]}]))
        assert lines(doc.blocks[0]) == [] and ctx.skipped == {"text": 1}, ctx.skipped
        limits.MAX_OUTPUT = 3 + vsdx.LINE
        doc, ctx = read(drawing([{"shapes": [{"text": "abc", "pin": (0.0, 3.0)}, {"text": "   ", "pin": (0.0, 2.0)},
                                             {"text": "\x00\x00", "pin": (0.0, 1.0)}]}]))
        assert lines(doc.blocks[0]) == ["abc"] and not ctx.skipped, ctx.skipped
    finally:
        limits.MAX_OUTPUT = old
    doc, ctx = read(drawing([{"shapes": [{"text": "y" * vsdx.MAX_TEXT + "\x00\x00"}]}]))
    assert lines(doc.blocks[0]) == ["y" * vsdx.MAX_TEXT] and not ctx.skipped, ctx.skipped


def test_marks_after_meter():
    # (review C) once the item meter is spent, text after a field mark is still kept
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 100
    try:
        index = struct.pack("<I", 500) + struct.pack("<IIIB", 0, 0, 9, 0) * 500
        lay = Layout()
        name = lay.put(struct.pack("<I", 1) + encode("N", 11) + bytes(2), 0x33, 0x40)
        names = lay.put(vsd_list([name]), 0x32, 0x50)
        chunks = [(0xA1, text_field(5), {"cid": 1}), (0x2D, "7".encode("utf-16-le"), {"cid": 5, "level": 3})]
        page = lay.put(page_stream([{"text": "a￼b", "chunks": chunks}], 11), 0x15, 0xD1)
        idx = lay.put(index, 0xC9, 0x40)
        pages = lay.put(vsd_list([page, idx]), 0x27, 0x50)
        doc, ctx = read(lay.done(lay.put(vsd_list([names, pages]), 0x14, 0x50)))
    finally:
        limits.BIN_RECORDS = old
    assert lines(doc.blocks[0]) == ["a7b"] and ctx.skipped == {"record": 1}, (lines(doc.blocks[0]), ctx.skipped)


def _png_rows(png):
    """The decompressed IDAT bytes of a PNG (filter byte, then the row) and its colour type."""
    at, idat, ctype = 8, b"", None
    while at + 8 <= len(png):
        n, kind = struct.unpack_from(">I4s", png, at)
        if kind == b"IHDR":
            ctype = png[at + 8 + 9]
        if kind == b"IDAT":
            idat += png[at + 8:at + 8 + n]
        at += 12 + n
    return zlib.decompress(idat), ctype


def test_dib_pictures():
    # (review D, G) DIBs get their file header from officeart.dib_to_bmp: BI_BITFIELDS masks after a 40-byte
    # header, and an 8-bit palette; a DIB whose palette passes its end is counted `image`
    def pic(data, y):
        return {"type": 0x4E, "pin": (0.0, y), "chunks": [(0x98, foreign_type(1, 0), {"level": 3}),
                                                           (0x0C, data, {"level": 3})]}

    masks = struct.pack("<III", 0xFF0000, 0xFF00, 0xFF)
    fields = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 32, 3, 8, 0, 0, 0, 0) + masks + struct.pack("<II", 0xFF0000, 0xFF)
    pal = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 8, 0, 4, 0, 0, 2, 0) + bytes([0, 0, 255, 0, 255, 0, 0, 0]) \
        + bytes([1, 0, 0, 0])
    bad = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 8, 0, 4, 0, 0, 200, 0) + bytes(8)
    doc, ctx = read(drawing([{"shapes": [pic(fields, 3.0), pic(pal, 2.0), pic(bad, 1.0)]}]))
    imgs = [b.data for b in doc.blocks[0].blocks]
    assert len(imgs) == 2 and ctx.skipped == {"image": 1}, ctx.skipped
    rows, ctype = _png_rows(imgs[0])
    assert ctype == 2 and rows == b"\x00" + bytes([255, 0, 0, 0, 0, 255]), rows  # red, blue
    rows, ctype = _png_rows(imgs[1])
    assert ctype == 2 and rows == b"\x00" + bytes([0, 0, 255, 255, 0, 0]), rows  # palette 1 (blue), 0 (red)


def test_png_charged_after_conversion():
    # (review E) a DIB is charged the PNG it becomes, not its input size
    rng = random.Random(5)
    w = h = 64
    noise = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 1, 0, 0, 0, 0, 2, 0) + bytes([0, 0, 0, 0, 255, 255, 255, 0]) \
        + bytes(rng.randrange(256) for _ in range(8 * h))
    png = vsd.images.bmp_to_png(vsd.officeart.dib_to_bmp(noise))
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(noise) + 14 + 10
    try:
        assert len(png) > limits.IMAGE_BYTES, len(png)
        doc, ctx = read(drawing([{"shapes": [{"type": 0x4E, "chunks": [(0x98, foreign_type(1, 0), {"level": 3}),
                                                                       (0x0C, noise, {"level": 3})]}]}]))
    finally:
        limits.IMAGE_BYTES = old
    assert lines(doc.blocks[0]) == [] and ctx.skipped == {"image": 1}, ctx.skipped


def test_block_charge_and_names_cap():
    # (review F) each emitted block is charged LINE besides its text, pictures too; a shape keeps at most MAX_TEXT
    # characters of names, and a field showing a name past that counts its text cut
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 2 * (1 + vsdx.LINE) + vsdx.LINE
    try:
        shapes = [{"text": "a", "pin": (0.0, 9.0)}, {"text": "b", "pin": (0.0, 8.0)},
                  {"type": 0x4E, "pin": (0.0, 7.0), "chunks": [(0x98, foreign_type(1, 4), {"level": 3}),
                                                              (0x0C, PNG, {"level": 3})]},
                  {"text": "c", "pin": (0.0, 6.0)},
                  {"type": 0x4E, "pin": (0.0, 5.0), "chunks": [(0x98, foreign_type(1, 4), {"level": 3}),
                                                              (0x0C, PNG, {"level": 3})]}]
        doc, ctx = read(drawing([{"shapes": shapes}]))
    finally:
        limits.MAX_OUTPUT = old
    assert lines(doc.blocks[0]) == ["a", "b", "image:png"] and ctx.skipped == {"text": 1, "image": 1}, ctx.skipped
    half = vsdx.MAX_TEXT // 2 + 1
    chunks = [(0xA1, text_field(0), {"cid": 0}), (0xA1, text_field(2), {"cid": 2})] + [
        (0x2D, ch.encode("utf-16-le") * half, {"cid": k, "level": 3}) for k, ch in enumerate("PQR")]
    doc, ctx = read(drawing([{"shapes": [{"text": "￼|￼", "chunks": chunks}]}]))
    assert lines(doc.blocks[0]) == ["P" * half + "|"], lines(doc.blocks[0])[0][-8:]
    assert ctx.skipped == {"text": 1}, ctx.skipped


def test_name_index_123():
    # (review G) the 0x34 name index (readNameIDX123): u32 pairs (name id, element id) to the chunk's end
    lay = Layout()
    names = [lay.put(struct.pack("<I", 1) + encode(n, 11) + bytes(2), 0x33, 0x40) for n in ("Zero", "One")]
    name_list = lay.put(vsd_list(names), 0x32, 0x50)
    p0 = lay.put(page_stream([{"text": "a"}], 11), 0x15, 0xD1)
    p1 = lay.put(page_stream([{"text": "b"}], 11), 0x15, 0xD1)
    idx = lay.put(struct.pack("<IIII", 1, 0, 0, 1) + b"\x07", 0x34, 0x40)
    pages = lay.put(vsd_list([p0, p1, idx]), 0x27, 0x50)
    doc, ctx = read(lay.done(lay.put(vsd_list([name_list, pages]), 0x14, 0x50)))
    assert [s.title for s in doc.blocks] == ["One", "Zero"] and not ctx.skipped, ctx.skipped


def test_zip_saved_as_vsd():
    # (review H) a vsdx package saved as .vsd goes to zip_route, which hands a Visio package to vsdx
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns = 'xmlns="http://schemas.microsoft.com/office/visio/2012/main"'

    def rels(items):
        return f'<Relationships xmlns="{pkg}">' + "".join(
            f'<Relationship Id="{i}" Type="{rel}/{t}" Target="{tg}"/>' for i, t, tg in items) + "</Relationships>"

    root = f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Target="visio/document.xml" ' \
        'Type="http://schemas.microsoft.com/visio/2010/relationships/document"/></Relationships>'
    members = {
        "_rels/.rels": root,  # Visio's own type, as every bench Visio package has it
        "visio/document.xml": f"<VisioDocument {ns}/>",
        "visio/_rels/document.xml.rels": rels([("rId1", "pages", "pages/pages.xml")]),
        "visio/pages/pages.xml": f'<Pages xmlns:r="{rel}" {ns}><Page ID="0" Name="Zipped"><Rel r:id="rId1"/></Page>'
                                 "</Pages>",
        "visio/pages/_rels/pages.xml.rels": rels([("rId1", "page", "page1.xml")]),
        "visio/pages/page1.xml": f'<PageContents {ns}><Shapes><Shape ID="1"><Text>inside</Text></Shape></Shapes>'
                                 "</PageContents>"}
    with tempfile.TemporaryDirectory() as tmp:
        p = make_zip(Path(tmp) / "z.vsd", members, stored=())
        doc = vsd.parse(p, Context())
    assert [(s.title, lines(s)) for s in doc.blocks] == [("Zipped", ["inside"])]


def test_number_fidelity():
    # (review J) the DMS switch tests the cell's own type (an angle cell 80 with format 1 is %.4g degrees); an
    # unknown format prints %.4f; a field value is not clamped as a coordinate
    assert vsd.number(1, 80, math.pi / 4) == "45"
    assert vsd.number(1, 82, math.pi / 4) == "44 deg 59 min 60 sec"
    assert vsd.number(99, 32, 2.5) == "2.5000"
    chunks = [(0xA1, number_field(5e15, 2), {"cid": 1})]
    doc, _ = read(drawing([{"shapes": [{"text": "￼", "chunks": chunks}]}]))
    assert lines(doc.blocks[0]) == ["5000000000000000"], lines(doc.blocks[0])


def test_edge_cap_per_page():
    # (fix round 1) a page of more glued connectors than the budget's room: the first ones are edges, the cut is
    # counted once, and the page's every connector is registered, so the budget sees the cut
    seen, real = [], vsd.diagram.Diagram.place

    def spy(self, slots):
        seen.append(len(self.connectors))
        return real(self, slots)

    shapes = [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}] + [
        {"id": k, "misc": misc(11, ends=(1, 2))} for k in (3, 4, 5)]
    old = diagram.CONNECTIONS
    diagram.CONNECTIONS, vsd.diagram.Diagram.place = 1, spy
    try:
        doc, ctx = read(drawing([{"shapes": shapes}]))
    finally:
        diagram.CONNECTIONS, vsd.diagram.Diagram.place = old, real
    assert seen == [3], seen
    assert lines(doc.blocks[0]) == ['flowchart TD\n  n1["A"]\n  n2["B"]\n  n1 --> n2'], lines(doc.blocks[0])
    assert ctx.skipped == {"connections_cut": 1}, ctx.skipped


def test_misc_block_meter():
    # (fix round 1) each readMisc block takes an item of the BIN_RECORDS meter: glue blocks past it are not read
    glue = misc(11, ends=(1, 2))
    filler = glue[:45] + struct.pack("<I", 4) * 500 + glue[45:]
    shapes = [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}, {"id": 3, "text": "c", "misc": filler}]
    doc, ctx = read(drawing([{"shapes": shapes}]))
    want = 'flowchart TD\n  n1["A"]\n  n2["B"]\n  n1 -->|"c"| n2'
    assert lines(doc.blocks[0]) == [want] and not ctx.skipped  # the meter's room reaches the glue
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 100
    try:
        doc, ctx = read(drawing([{"shapes": shapes}]))
    finally:
        limits.BIN_RECORDS = old
    assert lines(doc.blocks[0]) == ["A", "B", "c"] and ctx.skipped == {"record": 1}, ctx.skipped


def test_v5_name_index_meter():
    # (fix round 1) the v5 name index (u16 count, u16 pairs) takes from the meter: 500 records, only the last naming
    # page 0; past the meter the page is numbered, counted `record` once
    lay = Layout(5)
    names = lay.put(v5_records([(0x33, struct.pack("<h", 1) + b"Late\x00")]), 0x32, 0x40)
    page = lay.put(page_stream([{"text": "t"}], 5), 0x15, 0xD1)
    idx = lay.put(struct.pack("<H", 500) + struct.pack("<HH", 0, 9) * 499 + struct.pack("<HH", 0, 0), 0xC9, 0x40)
    pages = lay.put(vsd_list([page, idx], version=5, ptype=0x27), 0x27, 0x50)
    data = lay.done(lay.put(vsd_list([names, pages], version=5), 0x14, 0x50))
    doc, ctx = read(data)
    assert doc.blocks[0].title == "Late" and not ctx.skipped, (doc.blocks[0].title, ctx.skipped)
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 100
    try:
        doc, ctx = read(data)
    finally:
        limits.BIN_RECORDS = old
    assert doc.blocks[0].title == "1" and ctx.skipped == {"record": 1}, (doc.blocks[0].title, ctx.skipped)


def test_stencil_room_and_lost_inheritance():
    # (fix round 1) stencil text is charged to its own MAX_OUTPUT room: a stencil shape past it is lost and its
    # inheritor counts `stencil`; so does one inheriting through a stencil shape that names the lost one, or a master
    # page past MASTERS. A stencil shape naming its own page inherits nothing, uncounted (test_master_ids_resolve_once).
    stencils = [[{"id": 1, "text": "a" * 120}, {"id": 2, "text": "b" * 120}, {"id": 3, "text": "ok"}],
                [{"id": 4, "master": (0, 2)}]]
    shapes = [{"master": (0, 2), "pin": (0.0, 9.0)}, {"master": (1, 4), "pin": (0.0, 8.0)},
              {"master": (0, 3), "pin": (0.0, 7.0)}]
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 200
    try:
        doc, ctx = read(drawing([{"shapes": shapes}], stencils=stencils))
    finally:
        limits.MAX_OUTPUT = old
    assert lines(doc.blocks[0]) == ["ok"] and ctx.skipped == {"stencil": 2}, (lines(doc.blocks[0]), ctx.skipped)
    old = vsdx.MASTERS
    vsdx.MASTERS = 1
    try:
        doc, ctx = read(drawing([{"shapes": [{"master": (0, 1)}]}], stencils=[[{"id": 1, "master": (1, 0)}],
                                                                           [{"text": "far"}]]))
    finally:
        vsdx.MASTERS = old
    assert lines(doc.blocks[0]) == [] and ctx.skipped == {"stencil": 1}, ctx.skipped


def test_zip_route_visio_only():
    # (fix rounds 1 and 2) zip_route sends a zip to vsdx only on an internal root relationship of Visio's own type
    # whose target exists, in a package without an `/officeDocument` one; any other zip keeps its old route
    from reader.formats import zip_route
    od = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    vis = "http://schemas.microsoft.com/visio/2010/relationships/document"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"

    def rels(items):
        return f'<Relationships xmlns="{pkg}">' + "".join(
            f'<Relationship Id="r{i}" Type="{t}" Target="{tg}"/>' for i, (t, tg) in enumerate(items)) + \
            "</Relationships>"

    ct = {"[Content_Types].xml": "<Types/>"}
    docx = {**ct, "word/document.xml": "<w/>"}
    cases = [
        ({**docx, "_rels/.rels": rels([(f"{od}/officeDocument", "word/document.xml"), (f"{od}/document", "x.xml")])},
         "ooxml"),   # a dangling /document rel
        ({**docx, "_rels/.rels": rels([(f"{od}/officeDocument", "word/document.xml"),
                                       (f"{od}/document", "word/document.xml")])}, "ooxml"),  # an odd one
        ({**ct, "ppt/presentation.xml": "<p/>",
          "_rels/.rels": rels([(f"{od}/officeDocument", "ppt/presentation.xml")])}, "ooxml"),
        ({**ct, "xl/workbook.xml": "<x/>", "_rels/.rels": rels([(f"{od}/officeDocument", "xl/workbook.xml")])},
         "ooxml"),
        ({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "<c/>"}, "odf"),
        ({**ct, "visio/document.xml": "<v/>", "_rels/.rels": rels([(vis, "visio/document.xml")])}, "vsdx"),
        ({**ct, "visio/document.xml": "<v/>", "_rels/.rels": rels([(f"{od}/document", "visio/document.xml")])},
         "ooxml"),  # a `/document` rel of another type
        ({**ct, "_rels/.rels": rels([(f"{od}/document", "visio/document.xml")])}, "ooxml"),  # no such part
        ({**ct, "_rels/.rels": rels([(vis, "visio/document.xml")])}, "ooxml"),  # Visio's type, no such part
        ({**docx, "_rels/.rels": rels([(f"{od}/officeDocument", "word/document.xml"), (vis, "visio/document.xml")])},
         "ooxml"),  # (round 2) a docx with a dangling Visio-typed rel
        ({**docx, "visio/document.xml": "<v/>",
          "_rels/.rels": rels([(f"{od}/officeDocument", "word/document.xml"), (vis, "visio/document.xml")])},
         "ooxml"),  # (round 2) a Word+Visio hybrid keeps its Word body
    ]
    for members, want in cases:
        assert zip_route.route(make_zip(None, members)) == want, (want, sorted(members))


def test_no_picture_work_when_spent():
    # (fix round 1) a spent image budget converts nothing; a DIB whose PNG conversion fails is tried once and kept
    calls, real = [], vsd.images.bmp_to_png

    def spy(data):
        calls.append(len(data))
        return real(data)

    def pic(kind, fmt, data, y):
        return {"type": 0x4E, "pin": (0.0, y), "chunks": [(0x98, foreign_type(kind, fmt), {"level": 3}),
                                                           (0x0C, data, {"level": 3})]}

    bmp = vsd.officeart.dib_to_bmp(dib(2, 2))
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES, vsd.images.bmp_to_png = len(PNG), spy
    try:
        doc, ctx = read(drawing([{"shapes": [pic(1, 4, PNG, 3.0), pic(1, 4, bmp, 2.0), pic(1, 0, dib(), 1.0)]}]))
        assert lines(doc.blocks[0]) == ["image:png"] and ctx.skipped == {"image": 2} and calls == [], (calls, ctx)
        limits.IMAGE_BYTES = old
        rgb16 = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 16, 0, 4, 0, 0, 0, 0) + bytes(4)  # bmp_to_png refuses it
        doc, ctx = read(drawing([{"shapes": [pic(1, 0, rgb16, 1.0)]}]))
    finally:
        limits.IMAGE_BYTES, vsd.images.bmp_to_png = old, real
    assert lines(doc.blocks[0]) == ["image:bmp"] and len(calls) == 1 and not ctx.skipped, (calls, ctx.skipped)


def test_master_first_shape_and_xform():
    # (fix round 1) master shape 0xFFFFFFFF is the master page's first shape (vsdx's rule); a v11 shape without an
    # XForm of its own is placed by its master shape's (readShape copies m_xform)
    stencils = [[{"id": 5, "text": "first", "pin": (2.0, 4.0)}, {"id": 6, "text": "second"}]]
    shapes = [{"text": "own", "pin": (0.0, 3.0)}, {"master": (0, 5)}, {"master": (0, NONE), "pin": (0.0, 1.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}], stencils=stencils))
    assert lines(doc.blocks[0]) == ["first", "own", "first"] and not ctx.skipped, lines(doc.blocks[0])


def test_blank_stencil_shape_replaces():
    # a blank stencil shape gives its inheritors nothing, also when an earlier shape of its id had text (the last of
    # an id wins) and when it is the page's first shape (master shape 0xFFFFFFFF); nothing is counted
    stencils = [[{"id": 5}, {"id": 2, "text": "Old"}, {"id": 2}, {"id": 6, "text": "second"}]]
    shapes = [{"master": (0, 2), "pin": (0.0, 3.0)}, {"master": (0, NONE), "pin": (0.0, 2.0)},
              {"master": (0, 6), "pin": (0.0, 1.0)}]
    doc, ctx = read(drawing([{"shapes": shapes}], stencils=stencils))
    assert lines(doc.blocks[0]) == ["second"] and not ctx.skipped, (lines(doc.blocks[0]), ctx.skipped)


def test_blank_stencil_ids_cost_no_entry():
    # blank stencil shapes of distinct ids keep no entry each (a missing id reads as blank): Task 16's probe of 1.9M
    # of them (27 MiB coded) held an 80 MiB dict and 51 MiB of id ints at its 279 MiB peak
    n = 20_000

    def peak(ids):
        data = drawing([{"shapes": [{"text": "p"}]}], stencils=[[{"id": k} for k in ids]])
        tracemalloc.start()
        try:
            _read_doc(data)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    extra = peak(range(2, n + 2)) - peak([2] * n)  # one id n times keeps one entry
    assert extra < 30 * n, extra / n


def test_run_and_cut_fidelity():
    # (fix round 1) a zero-count run still covers one byte (the collector's loop); a UTF-16 unit past MAX_TEXT whose
    # low byte is NUL but high byte is not is no blank, so the text is cut; v5 copies no hidden flag from its master
    doc, _ = read(drawing([{"shapes": [{"raw": b"\xe9\xe9", "runs": [(0, 2), (1, 1)]}]}], version=6,
                          extra=[fonts([(1, 0), (2, 204)])]))
    assert lines(doc.blocks[0]) == ["й\xe9"], lines(doc.blocks[0])
    doc, ctx = read(drawing([{"shapes": [{"text": "y" * vsdx.MAX_TEXT + "ऀ"}]}]))
    assert ctx.skipped == {"text": 1}, ctx.skipped
    stencils = [[{"id": 1, "text": "veil", "misc": misc(5, hide=True)}]]
    doc, ctx = read(drawing([{"shapes": [{"master": (0, 1), "pin": (0.0, 1.0)}]}], version=5, stencils=stencils))
    assert lines(doc.blocks[0]) == ["veil"] and not ctx.skipped, lines(doc.blocks[0])


def test_first_stencil_list_only():
    # (fix round 2) only the first stencil list is read (`collecting`): a later list's page 0 does not replace the
    # first one's, and its page 1 is unread, its inheritor counted `stencil`
    pages = [{"shapes": [{"id": 1, "master": (0, 4), "pin": (0.0, 2.0)},
                         {"id": 2, "master": (1, 4), "pin": (0.0, 1.0)}]}]
    for version in (11, 6):
        data = drawing(pages, version=version, stencils=[[{"id": 4, "text": "first"}]],
                       more_stencils=[[{"id": 4, "text": "second"}], [{"id": 4, "text": "other"}]])
        doc, ctx = read(data)
        assert (lines(doc.blocks[0]), ctx.skipped) == (["first"], {"stencil": 1}), (version, lines(doc.blocks[0]),
                                                                                     ctx.skipped)


def _v5_names(*names, nested=False):
    """A Visio 5 name list stream (an `extra`): one 0x33 record a name; `nested` adds a 0x32 record after them."""
    recs = [(0x33, struct.pack("<h", 1) + n + b"\x00") for n in names]
    return 0x32, 0x40, v5_records(recs + ([(0x32, bytes(4))] if nested else []))


def test_v5_fresh_name_map():
    # (fix round 2) a Visio 5 name list (0x32) starts a fresh name map: a later list of one name leaves page 2
    # unnamed (numbered), where the old map would still name it
    pages = [{"name": "A", "shapes": [{"text": "a"}]}, {"name": "B", "shapes": [{"text": "b"}]}]
    doc, ctx = read(drawing(pages, version=5, extra=[_v5_names(b"Z")]))
    assert [s.title for s in doc.blocks] == ["Z", "2"], [s.title for s in doc.blocks]


def test_v5_no_nested_lists():
    # (fix round 2) a Visio 5 list's records are not read as lists again: a 0x32 record inside a name list does not
    # empty the name map it is part of
    pages = [{"shapes": [{"text": "a"}]}]
    doc, ctx = read(drawing(pages, version=5, extra=[_v5_names(b"A", nested=True)]))
    assert [s.title for s in doc.blocks] == ["A"], [s.title for s in doc.blocks]


def test_field_order_meter():
    # (fix round 2, Task 15's guard) the field list's ids take from the item meter: past it the order is cut and
    # `record` counted once
    from types import SimpleNamespace
    data = struct.pack("<II", 0, 12) + struct.pack("<III", 4, 3, 9)
    old = limits.BIN_RECORDS
    try:
        for cap, want, skipped in ((100, [4, 3, 9], {}), (2, [4, 3], {"record": 1})):
            limits.BIN_RECORDS = cap
            r, s = vsd._Reader(b"", 11, Context()), SimpleNamespace(order=None)
            r.field_list(s, data)
            assert (s.order, r.ctx.skipped) == (want, skipped), (cap, s.order, r.ctx.skipped)
    finally:
        limits.BIN_RECORDS = old


def test_format_block_meter():
    # (fix round 2, Task 15's guard) each format block looked at takes one item: past the meter the number's format
    # is unknown (shown as nothing) and `record` counted once
    from types import SimpleNamespace
    filler = struct.pack("<IBBBHBB", 11, 0, 1, 0, 5, 0, 0)  # block index 1: not the format block
    data = number_field(41.6, 2)
    data = data[:0x36] + filler + data[0x36:]
    old = limits.BIN_RECORDS
    try:
        for cap, fmt, skipped in ((100, 2, {}), (1, vsd.UNKNOWN_FORMAT, {"record": 1})):
            limits.BIN_RECORDS = cap
            r, s = vsd._Reader(b"", 11, Context()), SimpleNamespace(fields={})
            r.text_field(s, 3, data)
            assert (s.fields[3][1], r.ctx.skipped) == (fmt, skipped), (cap, s.fields, r.ctx.skipped)
    finally:
        limits.BIN_RECORDS = old


def test_no_page():
    try:
        read(vsd_document(11, []))
    except ReadError as e:
        assert (e.kind, e.detail) == ("damaged", "no readable page")
    else:
        raise AssertionError("ReadError not raised")


def test_no_visio_stream_routes():
    ctx = Context()
    assert vsd.parse_compound(cfb_bytes({"WordDocument": bytes(64)})[0], ctx) == "doc"
    try:
        vsd.parse_compound(cfb_bytes({"Other": bytes(64)})[0], ctx)
    except ReadError as e:
        assert (e.kind, e.detail) == ("damaged", "no VisioDocument stream")
    else:
        raise AssertionError("ReadError not raised")
    # a clean fixture through the path entry and cfb_route.parse
    from reader.formats import cfb_route
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "d.vsd"
        p.write_bytes(cfb_bytes({"VisioDocument": drawing([{"name": "P", "shapes": [{"text": "hi"}]}])})[0])
        for doc in (vsd.parse(p, Context()), cfb_route.parse("vsd", p, Context())):
            assert lines(doc.blocks[0]) == ["hi"]


# -- measure: the hostile-input probes, all in memory

def _walk_all(data):
    ctx = Context()
    n = 0
    version, _ = vsd_stream.header(data)
    for step in vsd_stream.Tree(data, version, ctx).walk():
        n += step.kind == "chunk"
    return n, ctx


def _probe(name, data):
    """One walk timed, then one under tracemalloc for the peak (tracemalloc slows allocation-heavy loops)."""
    t0 = time.perf_counter()
    n, ctx = _walk_all(data)
    dt = time.perf_counter() - t0
    tracemalloc.start()
    _walk_all(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    mib = len(data) / MIB
    print(f"{name:<26} {mib:7.2f} MiB {dt:6.2f} s {dt / mib:6.2f} s/MiB  peak {peak / MIB:7.1f} MiB "
          f"({peak / len(data):6.1f}x/file)  chunks {n:>9,}  {dict(ctx.skipped)}", flush=True)


def _bomb(n_items):
    """Eight 18-byte references a flag byte, into the zero window: 144 bytes out per 17 in."""
    return (b"\x00" + b"\x00\x0f" * 8) * (n_items // 8)


def _read_doc(data):
    ctx = Context()
    version, _ = vsd_stream.header(data)
    try:
        doc = vsd._Reader(data, version, ctx).run()
    except ReadError as e:
        return f"{e.kind}: {e.detail}", ctx
    pages = [b for b in doc.blocks if isinstance(b, Section)]
    return f"pages {len(pages):,} blocks {sum(len(b.blocks) for b in pages):,}", ctx


def _probe_doc(name, data):
    """vsd's reader over one VisioDocument stream: timed, then once more under tracemalloc for the peak."""
    t0 = time.perf_counter()
    what, ctx = _read_doc(data)
    dt = time.perf_counter() - t0
    tracemalloc.start()
    _read_doc(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    mib = len(data) / MIB
    print(f"vsd {name:<22} {mib:7.2f} MiB {dt:6.2f} s {dt / mib:6.2f} s/MiB  peak {peak / MIB:7.1f} MiB "
          f"({peak / len(data):6.1f}x/file)  {what}  {dict(ctx.skipped)}", flush=True)


def measure_vsd():
    pic = [(0x98, foreign_type(1, 4), {"level": 3}), (0x0C, PNG + bytes(1000), {"level": 3})]
    bmp = [(0x98, foreign_type(1, 0), {"level": 3}), (0x0C, dib(64, 64), {"level": 3})]
    cases = [
        ("pages x20k", [{"name": f"p{k}", "shapes": [{"text": "x", "pin": (1.0, 1.0)}]} for k in range(20_000)]),
        ("shapes x200k", [{"shapes": [{"text": f"s{k}", "pin": (float(k % 50), float(k // 50))}
                                      for k in range(200_000)]}]),
        ("text chunk 16 MiB", [{"shapes": [{"text": ("abcdefg" + chr(10)) * MIB}]}]),
        ("group chain x100k", [{"shapes": [{"id": k, "parent": k - 1 if k > 1 else 0, "text": "g",
                                            "pin": (1.0, 1.0, 0.5, 0.5)} for k in range(1, 100_001)]}]),
        ("group chain x100k rev", [{"shapes": [{"id": k, "parent": k - 1 if k > 1 else 0, "text": "g",
                                                "pin": (1.0, 1.0, 0.5, 0.5)} for k in range(100_000, 0, -1)]}]),
        ("png pictures x50k", [{"shapes": [{"type": 0x4E, "pin": (0.0, float(k)), "chunks": pic}
                                           for k in range(50_000)]}]),
        ("dib pictures x2k", [{"shapes": [{"type": 0x4E, "pin": (0.0, float(k)), "chunks": bmp}
                                          for k in range(2_000)]}]),
        ("field marks x1M", [{"shapes": [{"text": chr(0xFFFC) * 1_000_000,
                                          "chunks": [(0xA1, number_field(1.5, 2), {"cid": k}) for k in range(50)]}]}]),
    ]
    for name, pages in cases:
        _probe_doc(name, drawing(pages))
    measure_vsd16()


def _coded_page(shape, blocks):
    """A page chunk stream LZ77-coded by hand (the fixture encoder is too slow at this size): the page chunk and one
    `shape` (zero-padded to a divisor of 36,864, so each block of 2,048 18-byte references, distance len(shape),
    repeats it 36,864 / len bytes) and `blocks` such blocks. Returns (coded bytes, shapes)."""
    head = vsd_chunk(0x15, bytes(0x30), cid=NONE)
    first = head + bytes(-len(head) % 8) + shape  # whole flag groups of literals before the references
    unit = len(shape)
    assert 36_864 % unit == 0, unit
    block = _code([(unit, 18)] * 2048, pos=len(first))
    return _code(list(first)) + block * blocks, 1 + blocks * 36_864 // unit


def _pad(raw, unit):
    assert len(raw) <= unit, len(raw)
    return raw + bytes(unit - len(raw))  # zero bytes before a chunk header are skipped


def measure_vsd16():
    """Task 16's probes: masters, connectors, master-id cycles, v5 records and pointers, coded shapes, names."""
    # many masters: 5,000 stencil pages (MASTERS 1,000 read) and 50,000 page shapes naming them, none with own text
    stencils = [[{"id": 1, "text": "m" * 100}] for _ in range(5_000)]
    shapes = [{"master": (k % 5_000, 1), "pin": (0.0, float(k))} for k in range(50_000)]
    _probe_doc("masters x5k", drawing([{"shapes": shapes}], stencils=stencils))
    # master-id cycles: 1,000 stencil pages of 50 shapes, each naming itself, the next page and the previous one
    # (inherited copies of 2,000 characters charged to the stencil room, past it lost); page shapes naming each one
    stencils = [[{"id": j, "master": ((p, j), ((p + 1) % 1_000, j), ((p - 1) % 1_000, j))[j % 3],
                  **({"text": "c" * 2_000} if p == 0 else {})} for j in range(50)] for p in range(1_000)]
    shapes = [{"master": (k // 50 % 1_000, k % 50), "pin": (0.0, float(k))} for k in range(100_000)]
    _probe_doc("master cycles x50k", drawing([{"shapes": shapes}], stencils=stencils))
    # many connectors: 200,000 glued between two shapes, past CONNECTIONS
    shapes = [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}] + [
        {"id": k, "text": "c", "misc": misc(11, ends=(1, 2))} for k in range(3, 200_003)]
    _probe_doc("connectors x200k", drawing([{"shapes": shapes}]))
    # misc chunks of 4-byte blocks: every block an item of the meter
    shapes = [{"id": k, "misc": bytes(45) + struct.pack("<I", 4) * 4_000} for k in range(1_000)]
    _probe_doc("misc blocks x4M", drawing([{"shapes": shapes}]))
    # Visio 5: list chunks whose record tables are all 65,535 entries (past BIN_RECORDS), and a page list whose
    # declared count passes its pointers, the last one cut short
    table = bytes(4) + struct.pack("<HH", 0xA4, 0) * 65_535 + struct.pack("<HH", 65_535, 0xFFFF)
    shapes = [{"id": k, "text": "v", "chunks": [(0x68, table)]} for k in range(100)]
    _probe_doc("v5 record tables x6.5M", drawing([{"shapes": shapes}], version=5))
    lay = Layout(5)
    plist = vsd_list([lay.put(page_stream([{"text": "p"}], 5), 0x15, 0xD1) for _ in range(20_000)], version=5,
                     ptype=0x27, count=32_767)
    plist = lay.put(plist[:-7], 0x27, 0x50)
    _probe_doc("v5 cut pointers x20k", lay.done(lay.put(vsd_list([plist], version=5), 0x14, 0x50)))
    # 1M coded shapes (shape and text chunks) and 660k coded shapes with an XForm each (BIN_RECORDS reached)
    for name, s, unit in (("coded shapes", {"text": "x"}, 128),
                          ("coded pinned shapes", {"text": "x", "pin": (1.0, 2.0)}, 256)):
        raw = _pad(shape_chunks(s, 11), unit)
        coded, n = _coded_page(raw, -(-1_000_000 * unit // 36_864))
        lay = Layout()
        pages = lay.put(vsd_list([lay.put(coded, 0x15, 0xD3)]), 0x27, 0x50)
        _probe_doc(f"{name} ({n:,})", lay.done(lay.put(vsd_list([pages]), 0x14, 0x50)))
    # 2,000 large names in one shape (10,000 characters each), each shown by a field: MAX_TEXT kept in all
    names = [(0x2D, chr(0x41 + k % 26).encode("utf-16-le") * 10_000, {"cid": k, "level": 3}) for k in range(2_000)]
    fields = [(0xA1, text_field(k), {"cid": k}) for k in range(2_000)]
    _probe_doc("large names x2k", drawing([{"shapes": [{"text": "￼" * 2_000, "chunks": fields + names}]}]))
    # (fix round 1) 1.9M bare shapes of distinct ids: parented page shapes (one page.ids entry each) and stencil
    # shapes (one stencil dict entry each, the blank ones sharing one _Entry)
    n = 1_900_000
    lay = Layout()
    page = lay.put(_coded_ids(vsd_chunk(0x15, bytes(0x30), cid=NONE), n, 1), 0x15, 0xD3)
    pages = lay.put(vsd_list([page]), 0x27, 0x50)
    _probe_doc("parented ids x1.9M", lay.done(lay.put(vsd_list([pages]), 0x14, 0x50)))
    lay = Layout()
    spage = lay.put(vsd_list([lay.put(_coded_ids(b"", n, 0), 0x48, 0xD2)]), 0x1E, 0x50)
    sten = lay.put(vsd_list([spage]), 0x1D, 0x50)
    pages = lay.put(vsd_list([lay.put(page_stream([{"text": "p"}], 11), 0x15, 0xD1)]), 0x27, 0x50)
    _probe_doc("stencil ids x1.9M", lay.done(lay.put(vsd_list([sten, pages]), 0x14, 0x50)))


def _coded_ids(head, n, parent):
    """`head`, then `n` shape chunks (0x48, level 1, `parent`) of ids 2 .. n + 1, LZ77-coded by hand: each shape's
    first four bytes and the rest after its id copied from the shape before it, its id four literals."""
    def one(k):
        return vsd_chunk(0x48, shape_data(parent=parent), cid=k, level=1)

    unit = len(one(2))
    first = head + bytes(-(len(head) + unit) % 8) + one(2)  # whole flag groups of literals before the references
    rest, left = [], unit - 8
    while left:
        step = 18 if left - 18 >= 3 or left == 18 else min(left, 18) if left <= 18 else left - 3
        rest.append(step)
        left -= step
    out, pos, batch, shapes = [_code(list(first))], len(first), [], 0
    for k in range(3, n + 2):
        batch += [(unit, 4)] + list(struct.pack("<I", k)) + [(unit, r) for r in rest]
        shapes += 1
        if len(batch) >= 4096 and len(batch) % 8 == 0:
            out.append(_code(batch, pos=pos))
            pos, batch, shapes = pos + shapes * unit, [], 0
    if batch:
        out.append(_code(batch, pos=pos))
    return b"".join(out)


def measure():
    measure_vsd()
    # a pointer list pointing at itself, 100,000 times (the pointer cap and the visited set)
    lay = Layout()
    at = len(lay.body)
    size = len(vsd_list([(0x27, 0, 0, 0)] * 120_000))
    lst = lay.put(vsd_list([(0x27, at, size, 0x50)] * 120_000), 0x27, 0x50)
    _probe("list at itself x120k", lay.done(lay.put(vsd_list([lst]), 0x14, 0x50)))
    # one big compressed stream (a bomb, about 8.5x) reached by 100,000 pointers of different lengths
    lay = Layout()
    big = lay.put(_bomb(8 * 120_000), 0x48, 0xD2)
    ptrs = [(0x48, big[1], big[2] - k, 0xD2) for k in range(100_000)]
    _probe("compressed stream x100k", lay.done(lay.put(vsd_list(ptrs), 0x14, 0x50)))
    # the same region stored: each stream is charged its length
    lay = Layout()
    region = lay.put(b"".join(vsd_chunk(0x48, b"", level=1) for _ in range(60_000)), 0x48, 0xD0)
    ptrs = [(0x48, region[1], region[2] - k, 0xD0) for k in range(100_000)]
    _probe("stored stream x100k", lay.done(lay.put(vsd_list(ptrs), 0x14, 0x50)))
    # deep nesting: 10,000 lists, each holding the next
    lay = Layout()
    inner = lay.put(vsd_chunk(0x48, b"", level=1), 0x48, 0xD0)
    for _ in range(10_000):
        inner = lay.put(vsd_list([inner]), 0x27, 0x50)
    _probe("nesting x10k", lay.done(lay.put(vsd_list([inner]), 0x14, 0x50)))
    # a chunk stream of minimum-size chunks (19-byte headers, no data): 16 MiB, then past BIN_RECORDS
    for count in (880_000, 2_500_000):
        lay = Layout()
        s = lay.put(vsd_chunk(0x48, b"", level=1) * count, 0x48, 0xD0)
        _probe(f"min chunks x{count:,}", lay.done(lay.put(vsd_list([s]), 0x14, 0x50)))
    # inflate bombs: 16 MiB in (about 135 MiB out) and 32 MiB in (past VSD_INFLATE)
    for mib in (16, 32):
        lay = Layout()
        s = lay.put(_bomb(mib * MIB * 8 // 17), 0x46, 0x42)
        _probe(f"inflate bomb {mib} MiB", lay.done(lay.put(vsd_list([s]), 0x14, 0x50)))
    # an overlap bomb: every reference 18 bytes at distance 1, 32 MiB (past VSD_INFLATE)
    lay = Layout()
    s = lay.put(_repeat(1, 32 * MIB // 4352), 0x46, 0x42)
    _probe("overlap bomb 32 MiB", lay.done(lay.put(vsd_list([s]), 0x14, 0x50)))
    # inflate rates alone: coded text and noise (the fixture encoder is slow, so these are small), and hand-made
    # streams of single literals (flag 0x55: literal, reference, ...) and of eight literals a flag
    rng = random.Random(1)
    words = [bytes(rng.choice(b"abcdefghij ") for _ in range(rng.randrange(2, 12))) for _ in range(5000)]
    text = b"".join(rng.choice(words) for _ in range(60_000))
    noise = bytes(rng.randrange(256) for _ in range(MIB // 2))
    cases = [("text", lz77(text), text), ("noise", lz77(noise), noise),
             ("literal/reference", (b"\x55" + b"a\x00\x0fb\x00\x0fc\x00\x0fd\x00\x0f") * (1 << 18), None),
             ("eight literals", (b"\xff" + b"abcdefgh") * (1 << 20), None)]
    for name, packed, raw in cases:
        t0 = time.perf_counter()
        out = vsd_stream.inflate(packed, 1 << 30)
        dt = time.perf_counter() - t0
        assert raw is None or out == raw
        print(f"inflate {name:<18} {len(packed) / MIB:7.2f} MiB in {len(out) / MIB:7.2f} MiB out {dt:6.2f} s "
              f"{dt / (len(out) / MIB):6.3f} s/MiB out", flush=True)


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
