"""Visio binary (vsd): the VisioDocument stream layer (header, LZ77, pointer lists, chunk streams, the trailer walk)
and the drawing reader (pages, shapes, text, fields, pictures, routing).

Run: python -I -X utf8 tests/test_reader_vsd.py
     python -I -X utf8 tests/test_reader_vsd.py measure   (the hostile-input probes, in memory: s/MiB and peak)
"""
import random
import struct
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.formats import vsd, vsd_stream, vsdx  # noqa: E402
from reader.formats.vsd_stream import Chunk, Pointer  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Break, Context, Image, Para, Section, Text  # noqa: E402
from reader_fix import cfb_bytes, lz77, vsd_chunk, vsd_document, vsd_head, vsd_list, vsd_pointer  # noqa: E402

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


def xform(px, py, lx=0.0, ly=0.0):
    """A 0x9B chunk's data: PinX, PinY, Width, Height, LocPinX, LocPinY, Angle, each a unit byte and a double, then
    the two flip bytes."""
    return b"".join(b"\x00" + struct.pack("<d", v) for v in (px, py, 1.0, 1.0, lx, ly, 0.0)) + b"\x00\x00"


def shape_data(parent=0):
    """A shape chunk's data: parent at 10 (readShape), the rest zero."""
    d = bytearray(0x30)
    struct.pack_into("<I", d, 10, parent)
    return bytes(d)


def encode(text, version):
    return text.encode("utf-16-le" if version == 11 else "cp1252")


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


def page_stream(shapes, version):
    """A page as real files hold it: one chunk stream, the page chunk 0x15 then each shape's chunk (header level 1)
    with its chunks (level 2; the XForm at level 3). A shape dict: "id", "type", "parent", "text", "pin" (PinX,
    PinY[, LocPinX, LocPinY]) and "chunks" ((type, data[, keywords]))."""
    parts = [vsd_chunk(0x15, bytes(0x30), version=version, cid=NONE)]
    for s in shapes:
        parts.append(vsd_chunk(s.get("type", 0x48), shape_data(s.get("parent", 0)), version=version,
                               cid=s.get("id", NONE), level=1))
        if "text" in s:
            parts.append(vsd_chunk(0x0E, bytes(8) + encode(s["text"], version), version=version, level=2))
        if "pin" in s:
            parts.append(vsd_chunk(0x9B, xform(*s["pin"]), version=version, level=3))
        for c in s.get("chunks", ()):
            parts.append(vsd_chunk(c[0], c[1], version=version, **{"level": 2, **(c[2] if len(c) > 2 else {})}))
    return b"".join(parts)


def drawing(pages, version=11):
    """A VisioDocument laid out as real files: the trailer list holds the name list (0x32: one 0x33 name per page)
    and the pages list (0x27: each page a chunk stream, then the name index 0xC9 naming page k by name k). A page
    is a dict: "name", "shapes", "background", "compressed" (the stream LZ77-coded, format bit 1)."""
    lay = Layout(version)
    unit = 2 if version == 11 else 1
    names = [lay.put(struct.pack("<I", 1) + encode(p.get("name", ""), version) + bytes(unit), 0x33, 0x40)
             for p in pages]
    name_list = lay.put(vsd_list(names, version=version), 0x32, 0x50)
    ptrs = []
    for p in pages:
        raw, fmt = page_stream(p["shapes"], version), 0xD0 if p.get("background") else 0xD1
        if p.get("compressed"):
            raw, fmt = lz77(raw), fmt | 2
        ptrs.append(lay.put(raw, 0x15, fmt))
    index = struct.pack("<I", len(pages)) + b"".join(struct.pack("<IIIB", k, k, k, 0) for k in range(len(pages)))
    ptrs.append(lay.put(index, 0xC9, 0x40))
    pages_list = lay.put(vsd_list(ptrs, version=version), 0x27, 0x50)
    return lay.done(lay.put(vsd_list([name_list, pages_list], version=version), 0x14, 0x50))


def read(data, streams=None):
    ctx = Context()
    got = vsd.parse_compound(cfb_bytes({"VisioDocument": data, **(streams or {})})[0], ctx)
    return got, ctx


def lines(sec):
    """Each block of a section as text: a Para's inlines joined (Break as a newline), an Image as its kind."""
    out = []
    for b in sec.blocks:
        if isinstance(b, Para):
            out.append("".join(i.text if isinstance(i, Text) else "\n" for i in b.inlines))
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


def test_visio5_refused():
    try:
        read(vsd_document(5, [[{"text": "A"}]]))
    except ReadError as e:
        assert (e.kind, e.detail) == ("unsupported", "Visio 5 drawing")
    else:
        raise AssertionError("ReadError not raised")


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
    out = Path(tempfile.mkdtemp())

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
    assert [(s.title, lines(s), s.hidden) for s in doc.blocks] == [("Front", ["f"], ""), ("Back", ["b"], "")]
    doc, _ = read(vsd_document(11, [{"shapes": [{"text": "x"}], "background": True}]))
    assert lines(doc.blocks[0]) == ["x"]


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
    # out, counted `text`, as vsdx charges it (its characters and LINE per break)
    doc, ctx = read(drawing([{"shapes": [{"text": "x" * (vsdx.MAX_TEXT + 5)}]}]))
    assert lines(doc.blocks[0]) == ["x" * vsdx.MAX_TEXT] and ctx.skipped == {"text": 1}, ctx.skipped
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 9 + vsdx.LINE
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
    p = Path(tempfile.mkdtemp()) / "d.vsd"
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
