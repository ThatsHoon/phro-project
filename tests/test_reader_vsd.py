"""Visio binary (vsd): the VisioDocument stream layer (header, LZ77, pointer lists, chunk streams, the trailer walk).

Run: python -I -X utf8 tests/test_reader_vsd.py
     python -I -X utf8 tests/test_reader_vsd.py measure   (the hostile-input probes, in memory: s/MiB and peak)
"""
import random
import struct
import sys
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits  # noqa: E402
from reader.formats import vsd_stream  # noqa: E402
from reader.formats.vsd_stream import Chunk, Pointer  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import lz77, vsd_chunk, vsd_document, vsd_head, vsd_list, vsd_pointer  # noqa: E402

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
    pairs = list(vsd_stream.Tree(data, 11, Context()).chunks())
    assert [(p.type, c.type) for p, c in pairs] == [(0x27, 0x27), (0x15, 0x15), (0x48, 0x48), (0x48, 0x0E)]


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


def measure():
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
