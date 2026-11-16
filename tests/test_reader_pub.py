"""Publisher (pub): the Contents blocks and chunk directory (`pub_contents`) and the Quill bits, stories, table cells
and links (`pub_quill`).

Run: python -I -X utf8 tests/test_reader_pub.py
     python -I -X utf8 tests/test_reader_pub.py measure   (the hostile-input probes, in memory: s/MiB and peak)
"""
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
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import cfb_route, pub, pub_contents, pub_quill, vsdx, zip_route  # noqa: E402
from reader.formats.pub_contents import Block, ChunkRef  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Break, Context, Link, Para, Section, Text  # noqa: E402
from reader_fix import (cfb_bytes, oa_container, oa_record, pub_block, pub_contents as contents_fix,  # noqa: E402
                        pub_escher, pub_file, pub_group, pub_quill as quill_fix, pub_sp)

MIB = 2**20


class patched:
    """Set module attributes for a `with` block and restore them after."""

    def __init__(self, mod, **values):
        self.mod, self.values, self.old = mod, values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def three_chunks(parents=(None, 0, 1)):
    return [(0, pub_contents.DOCUMENT, parents[0], pub_block(1, 0x20, 7)),
            (1, pub_contents.PAGE, parents[1], pub_block(2, 0x20, 8) + pub_block(3, 0x10, 9)),
            (2, pub_contents.SHAPE, parents[2], pub_block(4, 0x20, 10))]


def like(raw, bit):
    """`raw` as a Quill bit with `bit`'s id, kind and offset."""
    return pub_quill.Bit(raw, bit.id, bit.kind, bit.offset)


def text_of(para):
    out = []
    for i in para.inlines:
        if isinstance(i, Text):
            out.append(i.text)
        elif isinstance(i, Bold):
            out.append("**" + "".join(t.text for t in i.inlines) + "**")
        elif isinstance(i, Break):
            out.append("|")
        elif isinstance(i, Link):
            out.append("".join(t.text for t in i.inlines))
    return "".join(out)


# -- pub_contents: blocks

def test_blocks_fixed_and_variable():
    # a fixed u32 block and a variable block -> both, right bounds
    data = b"xx" + pub_block(5, 0x20, 0x01020304) + pub_block(6, 0x88, pub_block(7, 0x10, 3)) + b"yy"
    got = list(pub_contents.blocks(data, 2, len(data) - 2))
    assert got[0] == Block(5, 0x20, 4, 8, 0x01020304), got
    assert got[1][:4] == (6, 0x88, 10, 18) and got[1].data is None, got
    assert len(got) == 2, got
    kids = list(pub_contents.children(data, got[1]))
    assert kids == [Block(7, 0x10, 16, 18, 3)], kids
    # a string block keeps its text bytes; 8-, 16- and 24-byte blocks their bytes; an unknown type is empty
    data = pub_block(1, 0xC0, "hi".encode("utf-16-le")) + pub_block(2, 0x28, bytes(range(8))) + bytes([3, 0x33])
    got = list(pub_contents.blocks(data, 0, len(data)))
    assert [b.data for b in got] == ["hi".encode("utf-16-le"), bytes(range(8)), 0], got
    assert got[2].end - got[2].start == 0 and got[2].end == len(data), got


def test_block_zero_and_overlong():
    # a variable length 0, 3 or past the parent -> the walk ends, no hang
    ok = pub_block(1, 0x20, 1)
    for size in (0, 3, 1000):
        bad = bytes([2, 0x88]) + struct.pack("<I", size) + bytes(8)
        data = ok + bad + ok
        ctx = Context()
        got = list(pub_contents.blocks(data, 0, len(data), meter=pub_contents.Meter(ctx)))
        assert [b.id for b in got] == [1], (size, got)
        assert ctx.skipped == {"record": 1}, (size, ctx.skipped)
    # a length that fits the stream but not its parent ends the parent's walk
    inner = bytes([3, 0x88]) + struct.pack("<I", 40)
    data = pub_block(9, 0x88, inner) + bytes(64)
    parent = next(pub_contents.blocks(data, 0, len(data)))
    assert list(pub_contents.children(data, parent)) == []
    # a fixed block cut short by the end ends the walk uncounted; one byte left over is not a block
    ctx = Context()
    data = ok + bytes([4, 0x20, 1, 2])
    assert [b.id for b in pub_contents.blocks(data, 0, len(data), meter=pub_contents.Meter(ctx))] == [1]
    assert [b.id for b in pub_contents.blocks(ok + b"\x05", 0, len(ok) + 1)] == [1]
    # a variable block whose length the parent's end cuts (the bytes after it are not read) ends the walk uncounted
    data = bytes([1, 0x88, 9, 9]) + bytes(8)
    assert list(pub_contents.blocks(data, 0, 4, meter=pub_contents.Meter(ctx))) == []
    assert ctx.skipped == {}, ctx.skipped
    # an end past the data is the data's end
    assert [b.id for b in pub_contents.blocks(ok + bytes([2, 0x78]), 0, 1000)] == [1, 2]


def test_blocks_advance():
    # zero-length blocks advance two bytes each: n of them in 2n bytes are n blocks, and the walk ends
    data = bytes([1, 0x78]) * 1000
    assert len(list(pub_contents.blocks(data, 0, len(data)))) == 1000
    # an empty variable block (length 4) advances six
    data = pub_block(1, 0x88) * 500
    assert len(list(pub_contents.blocks(data, 0, len(data)))) == 500


def test_block_depth():
    # nesting stops at limits.MAX_DEPTH, counted record once for the refused walk
    data = pub_block(9, 0x20, 1)
    for k in range(10):
        data = pub_block(k, 0x88, data)
    with patched(limits, MAX_DEPTH=3):
        ctx = Context()
        meter = pub_contents.Meter(ctx)
        depth, block = 0, next(pub_contents.blocks(data, 0, len(data), meter=meter))
        while block is not None:
            block = next(pub_contents.children(data, block, depth, meter=meter), None)
            depth += 1 if block is not None else 0
        assert depth == 3, depth
        assert ctx.skipped == {"record": 1}, ctx.skipped


def test_block_meter():
    # limits.BIN_RECORDS blocks a document: the first refused counts record once and later walks stop
    data = bytes([1, 0x78]) * 50
    with patched(limits, BIN_RECORDS=30):
        ctx = Context()
        meter = pub_contents.Meter(ctx)
        assert len(list(pub_contents.blocks(data, 0, len(data), meter=meter))) == 30
        assert list(pub_contents.blocks(data, 0, len(data), meter=meter)) == []
        assert list(pub_contents.blocks(data, 0, len(data), limits.MAX_DEPTH + 1, meter=meter)) == []
        assert not meter.tick() and not meter.tick()
        assert ctx.skipped == {"record": 1}, ctx.skipped


# -- pub_contents: directory

def test_directory():
    # three chunk refs -> ends at the next offset; a parent cycle cut
    data = contents_fix(three_chunks((2, 0, 1)))
    ctx = Context()
    refs = pub_contents.directory(data, ctx)
    assert [(r.type, r.seqnum) for r in refs] == [(0x44, 0), (0x43, 1), (0x01, 2)], refs
    assert refs[0].end == refs[1].offset and refs[1].end == refs[2].offset, refs
    toff = struct.unpack_from("<I", data, 0x1A)[0]
    assert refs[2].end == toff, refs
    assert data[refs[1].offset:refs[1].end] == struct.pack("<I", 14) + pub_block(2, 0x20, 8) + pub_block(3, 0x10, 9)
    # 0 -> 2 -> 1 -> 0: the walk from 0 cuts 1's link back to 0
    assert [r.parent for r in refs] == [2, None, 1], refs
    assert ctx.skipped == {}, ctx.skipped
    # without a cycle every parent stays; a self-parent is cut
    refs = pub_contents.directory(contents_fix(three_chunks((None, 0, 1))), Context())
    assert [r.parent for r in refs] == [None, 0, 1], refs
    refs = pub_contents.directory(contents_fix(three_chunks((0, 0, 2))), Context())
    assert [r.parent for r in refs] == [None, 0, None], refs
    # DUMMY blocks keep their sequence numbers
    refs = pub_contents.directory(contents_fix([(0x10D, pub_contents.PAGE, None, b"")]), Context())
    assert [(r.seqnum, r.end - r.offset) for r in refs] == [(0x10D, 4)], refs


def test_directory_long_chain():
    # a parent chain of 20,000 references is walked once (a seen-set across walks), whatever the order
    n = 20_000
    chunks = [(k, pub_contents.SHAPE, k + 1 if k + 1 < n else 0, b"") for k in range(n)]
    data = contents_fix(chunks)
    with patched(limits, BIN_RECORDS=10**7):
        t = time.perf_counter()
        refs = pub_contents.directory(data, Context())
        t = time.perf_counter() - t
    assert len(refs) == n
    assert t < 5, t  # walked once: well under a second; each walk from its start would be n * n / 2 steps
    assert [r.parent for r in refs].count(None) == 1 and refs[-1].parent is None, refs[-1]


def test_chunk_cap():
    # pub_contents.PUB_CHUNKS lowered to 2 of 3 -> 2 refs, record 1
    with patched(pub_contents, PUB_CHUNKS=2):
        ctx = Context()
        refs = pub_contents.directory(contents_fix(three_chunks()), ctx)
    assert [r.seqnum for r in refs] == [0, 1], refs
    assert ctx.skipped == {"record": 1}, ctx.skipped


def test_directory_hostile_trailer():
    good = contents_fix(three_chunks())
    # a trailer offset that points at itself, past the stream, or a stream too short for it: no references, no crash
    for toff in (0x1A, len(good), len(good) - 2, 0xFFFFFFFF):
        data = bytearray(good)
        struct.pack_into("<I", data, 0x1A, toff)
        assert pub_contents.directory(bytes(data), Context()) == [], toff
    assert pub_contents.directory(good[:0x1C], Context()) == []
    # a trailer length past the stream reads to its end
    data = bytearray(good)
    toff = struct.unpack_from("<I", data, 0x1A)[0]
    struct.pack_into("<I", data, toff, 0xFFFFFFF0)
    assert len(pub_contents.directory(bytes(data), Context())) == 3
    for size in (0, 3):
        struct.pack_into("<I", data, toff, size)
        assert len(pub_contents.directory(bytes(data), Context())) == 3, size
    # only the trailer's first three blocks are looked at for the directory
    trailer = data[toff + 4:]
    moved = bytearray(good[:toff]) + struct.pack("<I", 0) + pub_block(9, 0x20, 0) + bytes(trailer)
    assert pub_contents.directory(bytes(moved), Context()) == []


def test_directory_offsets_clamped():
    # out-of-order and out-of-range offsets: each end is at least its offset and no end passes the stream
    data = bytearray(contents_fix(three_chunks()))
    toff = struct.unpack_from("<I", data, 0x1A)[0]
    entries = [i for i in range(toff, len(data) - 5) if data[i:i + 2] == b"\x04\xB8"]
    assert len(entries) == 3, entries
    for at, value in zip(entries, (500, 100, 0x7FFFFFFF)):
        struct.pack_into("<I", data, at + 2, value)
    refs = pub_contents.directory(bytes(data), Context())
    assert [(r.offset, r.end) for r in refs] == [(500, 500), (100, len(data)), (0x7FFFFFFF, 0x7FFFFFFF)], refs
    # the last chunk after the trailer ends at the stream's end
    struct.pack_into("<I", data, entries[2] + 2, toff + 4)
    assert pub_contents.directory(bytes(data), Context())[2].end == len(data)


def test_directory_2000():
    # Publisher 2000: the table at the u32 at 0x16, the type the chunk's first u16; type_of overrides it
    chunks = [(0x100, 0x15, None, bytes(10)), (0x101, 0x14, 0x100, bytes(6)), (0x102, 0x05, 0x101, bytes(4))]
    data = contents_fix(chunks, version=0x22)
    refs = pub_contents.directory(data, Context(), trailer=pub_contents.TRAILER_2K)
    assert [(r.type, r.seqnum, r.parent) for r in refs] == [(0x15, 0x100, 0xFFFF), (0x14, 0x101, 0x100),
                                                          (0x05, 0x102, 0x101)], refs
    assert refs[0].end == refs[1].offset and refs[2].end == struct.unpack_from("<I", data, 0x16)[0], refs
    refs = pub_contents.directory(data, Context(), trailer=0x16, type_of=lambda d, r: r.seqnum & 0xFF)
    assert [r.type for r in refs] == [0, 1, 2], refs
    # the cap, a count past the table and a parent cycle
    with patched(pub_contents, PUB_CHUNKS=1):
        ctx = Context()
        assert len(pub_contents.directory(data, ctx, trailer=0x16)) == 1 and ctx.skipped == {"record": 1}
    toff = struct.unpack_from("<I", data, 0x16)[0]
    big = bytearray(data)
    struct.pack_into("<H", big, toff, 0xFFFF)
    assert len(pub_contents.directory(bytes(big), Context(), trailer=0x16)) == 3
    for bad in (len(data), len(data) - 1, 0xFFFFFFFF):
        cut = bytearray(data)
        struct.pack_into("<I", cut, 0x16, bad)
        assert pub_contents.directory(bytes(cut), Context(), trailer=0x16) == [], bad
    far = bytearray(data)
    struct.pack_into("<I", far, toff + 2 + 2 * 10 + 6, 0xFFFFFF00)
    refs = pub_contents.directory(bytes(far), Context(), trailer=0x16)
    assert (refs[2].type, refs[2].offset, refs[2].end) == (None, 0xFFFFFF00, 0xFFFFFF00), refs
    cyc = contents_fix([(1, 0x14, 2, b""), (2, 0x14, 1, b"")], version=0x22)
    assert [r.parent for r in pub_contents.directory(cyc, Context(), trailer=0x16)] == [2, None]


def test_type_of():
    data = contents_fix(three_chunks())
    refs = pub_contents.directory(data, Context(), type_of=lambda d, r: d[r.offset + 4])
    assert [r.type for r in refs] == [1, 2, 4], refs


# -- pub_quill

STORIES = [(7, "Hello\rWorld"), (9, "A\vB")]


def test_quill_stories():
    # two stories "Hello\rWorld" and "A\vB" -> paragraphs and a line break, keyed by id
    bits = pub_quill.bits(quill_fix(STORIES))
    got = pub_quill.stories(bits)
    assert sorted(got) == [7, 9], got
    assert got[7].paras == (Para([Text("Hello")]), Para([Text("World")])), got[7]
    assert got[9].paras == (Para([Text("A"), Break(), Text("B")]),), got[9]
    assert (got[7].id, got[7].index, got[7].text) == (7, 0, "Hello\rWorld"), got[7]
    assert (got[9].index, got[9].text) == (1, "A\vB"), got[9]


def test_quill_paragraphs():
    # empty and blank paragraphs are left out; \n breaks a line too; other control characters go, a tab stays;
    # breaks at a paragraph's ends go
    bits = pub_quill.bits(quill_fix([(1, "\r\r a\tb\x01\r  \r\vx\v\ny\v\r")]))
    paras = pub_quill.stories(bits)[1].paras
    assert [text_of(p) for p in paras] == [" a\tb", "x||y"], paras


def test_quill_bold():
    # a bold run over "World" -> Bold inlines there
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)]))
    got = pub_quill.stories(bits)
    assert got[7].paras == (Para([Text("Hello")]), Para([Bold([Text("World")])])), got[7].paras
    assert got[9].paras == (Para([Text("A"), Break(), Text("B")]),), got[9].paras
    # a run across a paragraph end and a line break, and one inside a word
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(3, 8), (11, 14)]))
    got = pub_quill.stories(bits)
    assert [text_of(p) for p in got[7].paras] == ["Hel**lo**", "**Wo**rld"], got[7].paras
    assert [text_of(p) for p in got[9].paras] == ["**A**|**B**"], got[9].paras


def test_quill_default_bold():
    # libmspub's bold: the FDPC flag XOR the paragraph's default character style (FDPP block 0x19 indexes the even
    # entries of the second STSH bit; without the block the index is 0)
    def paras(**kw):
        got = pub_quill.stories(pub_quill.bits(quill_fix(STORIES, **kw)))
        return [text_of(p) for p in got[7].paras], [text_of(p) for p in got[9].paras]
    # inherited: "Hello\r" on default 1 (bold), the rest on default 0 (plain); no FDPC bold
    assert paras(fdpp=[(6, 1), (14, 0)], stsh=(False, True)) == (["**Hello**", "World"], ["A|B"])
    # un-bold: every paragraph on a bold default, an FDPC bold run over "World" makes it plain
    assert paras(bold=[(6, 11)], fdpp=[(14, 1)], stsh=(False, True)) == (["**Hello**", "World"], ["**A**|**B**"])
    # a paragraph style without block 0x19 takes default 0
    assert paras(fdpp=[(14, None)], stsh=(True,)) == (["**Hello**", "**World**"], ["**A**|**B**"])
    # an index past the defaults is not bold
    assert paras(bold=[(6, 11)], fdpp=[(14, 5)], stsh=(True,)) == (["Hello", "**World**"], ["A|B"])
    # bold ranges that meet are one run: FDPC bold "lo" (3-5) against the bold default of "Hel" (0-3) is "Hello",
    # never "**Hel****lo**" (the shared endpoint cancels in the XOR)
    assert paras(bold=[(3, 5)], fdpp=[(3, 1), (14, 0)], stsh=(False, True)) == (["**Hello**", "World"], ["A|B"])
    # the second STSH bit is the one read: the fixture writes three, as Publisher does, the third with no bold style
    bits = pub_quill.bits(quill_fix(STORIES, fdpp=[(6, 1), (14, 0)], stsh=(False, True)))
    assert len(bits["STSH"]) == 3 and not any(pub_quill._defaults(bits["STSH"][2:] * 2, pub_contents.Meter()))
    # a block 0x19 that is not a number leaves the index 0
    bits = pub_quill.bits(quill_fix(STORIES, fdpp=[(14, 1)], stsh=(True, False)))
    raw = bytes(bits["FDPP"][0])
    assert raw.count(bytes([0x19, 0x20, 1, 0, 0, 0])) == 1
    bits["FDPP"] = [like(raw.replace(bytes([0x19, 0x20, 1, 0, 0, 0]), pub_block(0x19, 0x88)), bits["FDPP"][0])]
    assert text_of(pub_quill.stories(bits)[7].paras[0]) == "**Hello**"
    assert text_of(pub_quill.stories(pub_quill.bits(quill_fix(STORIES, fdpp=[(14, 1)], stsh=(True, False))))[7]
                   .paras[0]) == "Hello"


def test_default_bold_missing():
    # without FDPP, without STSH, with one STSH bit only, or either malformed: FDPC alone, and the text stays
    want = (["Hello", "**World**"], ["A|B"])
    def paras(bits):
        got = pub_quill.stories(bits)
        return [text_of(p) for p in got[7].paras], [text_of(p) for p in got[9].paras]
    assert paras(pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], stsh=(True,)))) == want
    assert paras(pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], fdpp=[(14, None)]))) == want
    full = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], fdpp=[(14, None)], stsh=(True,)))
    assert paras(full) == (["**Hello**", "World"], ["**A**|**B**"])
    assert paras({**full, "STSH": full["STSH"][:1]}) == want
    assert paras({**full, "STSH": full["STSH"][1:]}) == want  # one STSH bit: libmspub reads only the second
    stsh = full["STSH"][1]
    for raw in (bytes(stsh)[:6], bytes(stsh)[:19], bytes(stsh)[:24],
                struct.pack("<II", 0, 0xFFFFFFFF) + bytes(stsh)[8:]):
        assert paras({**full, "STSH": [full["STSH"][0], like(raw, stsh)]})[0][1] in ("World", "**World**"), raw
    assert paras({**full, "STSH": [full["STSH"][0], like(bytes(stsh)[:19], stsh)]}) == want
    fdpp = full["FDPP"][0]
    bad = bytearray(fdpp)
    struct.pack_into("<H", bad, 0, 0xFFFF)
    assert paras({**full, "FDPP": [like(bytes(bad), fdpp)]}) == want
    for raw in (b"", b"\x01"):
        assert paras({**full, "FDPP": [like(raw, fdpp)]}) == want
    # a default style offset past the bit is not bold
    bad = bytearray(stsh)
    struct.pack_into("<I", bad, 20, 0xFFFFFF00)
    assert paras({**full, "STSH": [full["STSH"][0], like(bytes(bad), stsh)]}) == want


def test_default_bold_meter():
    # the FDPP runs, their styles and the default styles spend the same meter: past it bold is lost, record once, and
    # the text stays
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], fdpp=[(6, None), (14, None)], stsh=(True,)))
    full = pub_quill.stories(bits)
    assert [text_of(p) for p in full[7].paras] == ["**Hello**", "World"]
    for cap in range(12):
        with patched(limits, BIN_RECORDS=cap):
            ctx = Context()
            got = pub_quill.stories(bits, ctx)
        assert [s.text for s in got.values()] == ["Hello\rWorld", "A\vB"], cap
        assert ctx.skipped in ({}, {"record": 1}), (cap, ctx.skipped)
    with patched(limits, BIN_RECORDS=1):
        ctx = Context()
        pub_quill.stories(bits, ctx)
        assert ctx.skipped == {"record": 1}
    # the ticks, exactly: 3 FDPC runs and the bold style's block; one default style and its block; 2 FDPP runs (their
    # styles have no block). Without a bold default FDPP is not read.
    meter = pub_contents.Meter(Context())
    pub_quill.stories(bits, meter=meter)
    assert meter.used == 3 + 1 + 1 + 1 + 2, meter.used
    meter = pub_contents.Meter(Context())
    pub_quill.stories(pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], fdpp=[(6, None), (14, None)],
                                               stsh=(False,))), meter=meter)
    assert meter.used == 3 + 1 + 1, meter.used


def test_stories_meter_shared():
    # stories spends the caller's meter: a spent one reads no bold and counts nothing more
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)]))
    ctx = Context()
    meter = pub_contents.Meter(ctx)
    pub_quill.stories(bits, ctx, meter=meter)
    assert meter.used > 0
    with patched(limits, BIN_RECORDS=0):
        ctx = Context()
        meter = pub_contents.Meter(ctx)
        meter.tick()
        got = pub_quill.stories(bits, ctx, meter=meter)
    assert got[7].paras[1] == Para([Text("World")]) and ctx.skipped == {"record": 1}, ctx.skipped


def test_repeated_id_counted():
    # a text id named twice keeps its last story; each story replaced is counted record
    bits = pub_quill.bits(quill_fix([(7, "one"), (7, "two"), (9, "x"), (7, "three")]))
    ctx = Context()
    got = pub_quill.stories(bits, ctx)
    assert {k: s.text for k, s in got.items()} == {7: "three", 9: "x"} and got[7].index == 3, got
    assert ctx.skipped == {"record": 2}, ctx.skipped


def test_fdpc_short_bits():
    # FDPC bits of 0 and 1 bytes are not read: no bold, the stories stay
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)]))
    for raw in (b"", b"\x01"):
        got = pub_quill.stories({**bits, "FDPC": [like(raw, bits["FDPC"][0])]})
        assert [s.text for s in got.values()] == ["Hello\rWorld", "A\vB"], raw
        assert got[7].paras[1] == Para([Text("World")]), raw


def test_quill_without_fdpc():
    # fdpc=False -> the text, no Bold
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)], fdpc=False))
    got = pub_quill.stories(bits)
    assert got[7].paras == (Para([Text("Hello")]), Para([Text("World")])), got[7].paras
    assert "FDPC" not in bits


def test_quill_needs_text_strs_syid():
    data = quill_fix(STORIES)
    for name in ("TEXT", "STRS", "SYID"):
        bits = pub_quill.bits(data)
        del bits[name]
        assert pub_quill.stories(bits) == {}, name
    assert pub_quill.bits(b"CHNKINX " + data[8:]) == {}
    assert pub_quill.bits(b"") == {}


def test_quill_bits_shape():
    bits = pub_quill.bits(quill_fix(STORIES, tcd={0: [0, 2, 5]}, links=["https://a.example/"]))
    assert list(bits) == ["TEXT", "STRS", "SYID", "FDPC", "TCD ", "TOKN"], list(bits)
    tcd = bits["TCD "][0]
    assert isinstance(tcd, bytes) and (tcd.id, tcd.kind) == (0, "PLC "), tcd
    assert bits["TEXT"][0] == "Hello\rWorldA\vB".encode("utf-16-le") and bits["TEXT"][0].offset == 0x200


def test_quill_cycle():
    # cycle=True -> each bit once, no hang
    data = quill_fix(STORIES, cycle=True)
    bits = pub_quill.bits(data)
    assert {k: len(v) for k, v in bits.items()} == {"TEXT": 1, "STRS": 1, "SYID": 1, "FDPC": 1}, bits
    assert sorted(pub_quill.stories(bits)) == [7, 9]
    # a list naming itself as next, and a chain back to the head
    one = bytearray(quill_fix(STORIES))
    struct.pack_into("<I", one, 0x1C, 0x18)
    assert sum(len(v) for v in pub_quill.bits(bytes(one)).values()) == 4
    # a next list whose head passes the stream, and a list count past it, read what is there
    for at, fmt, value in ((0x1C, "<I", len(one) - 4), (0x1A, "<H", 0xFFFF)):
        data = bytearray(quill_fix(STORIES))
        struct.pack_into(fmt, data, at, value)
        bits = pub_quill.bits(bytes(data))
        assert sorted(pub_quill.stories(bits)) == [7, 9], (at, list(bits))


def test_quill_bits_cap():
    # pub_quill.QUILL_BITS lowered -> the rest ignored
    data = quill_fix(STORIES)
    with patched(pub_quill, QUILL_BITS=2):
        ctx = Context()
        bits = pub_quill.bits(data, ctx)
        assert list(bits) == ["TEXT", "STRS"], bits
        assert ctx.skipped == {"record": 1}, ctx.skipped
        assert pub_quill.stories(bits) == {}
        assert list(pub_quill.bits(data)) == ["TEXT", "STRS"]
    with patched(pub_quill, QUILL_BITS=4):
        ctx = Context()
        assert len(pub_quill.bits(data, ctx)) == 4 and ctx.skipped == {}, ctx.skipped


def test_quill_list_chain_cap():
    # a chain of empty lists, each naming the next: at most QUILL_BITS lists are visited, counted record once
    data = bytearray(quill_fix(STORIES))
    first = len(data)
    n = 50
    for k in range(n):
        data += struct.pack("<HHI", 0, 0, first + 8 * (k + 1) if k + 1 < n else 0xFFFFFFFF)
    struct.pack_into("<HHI", data, 0x18, 0, 0, first)
    with patched(pub_quill, QUILL_BITS=10):
        ctx = Context()
        assert pub_quill.bits(bytes(data), ctx) == {} and ctx.skipped == {"record": 1}, ctx.skipped
    ctx = Context()
    assert pub_quill.bits(bytes(data), ctx) == {} and ctx.skipped == {}, ctx.skipped


def test_quill_copy_peak():
    # a bit refused for the copy room is not copied first: 100 small bits spend the room, then one naming nearly the
    # whole stream is refused without a copy of it
    size = 4 * MIB
    small = size // 100
    entries = [struct.pack("<H4sHHH4sII", 0x18, b"JUNK", 0, 1, 0, b"JUNK", 1, small) for _ in range(100)]
    entries.append(struct.pack("<H4sHHH4sII", 0x18, b"JUNK", 0, 1, 0, b"JUNK", 1, size - 1))
    head = b"CHNKINK " + bytes(0x10) + struct.pack("<HHI", 0, len(entries), 0xFFFFFFFF) + b"".join(entries)
    data = head + bytes(size - len(head))
    tracemalloc.start()
    try:
        ctx = Context()
        bits = pub_quill.bits(data, ctx)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(bits["JUNK"]) == 100 and ctx.skipped == {"record": 1}, (len(bits["JUNK"]), ctx.skipped)
    assert peak < 1.5 * size, peak / size


def test_quill_copy_room():
    # a bit whose length passes the stream's end is read to the end, charged what it copies, nothing counted
    data = bytearray(quill_fix(STORIES))
    text = pub_quill.bits(bytes(data))["TEXT"][0]
    at = next(0x20 + 24 * k for k in range(4) if data[0x20 + 24 * k + 2:0x20 + 24 * k + 6] == b"TEXT")
    struct.pack_into("<I", data, at + 20, 0xFFFFFFF0)
    ctx = Context()
    bits = pub_quill.bits(bytes(data), ctx)
    assert bytes(bits["TEXT"][0]) == bytes(data[text.offset:]) and ctx.skipped == {}, ctx.skipped
    # bits that overlap copy at most the stream's size in all: here 9 entries each naming the whole stream
    data = bytearray(quill_fix(STORIES))
    entry = struct.pack("<H4sHHH4sII", 0x18, b"JUNK", 0, 1, 0, b"JUNK", 0, len(data) // 3)
    struct.pack_into("<HHI", data, 0x18, 0, 9, 0xFFFFFFFF)
    for k in range(9):
        data[0x20 + 24 * k:0x20 + 24 * (k + 1)] = entry
    ctx = Context()
    bits = pub_quill.bits(bytes(data), ctx)
    assert len(bits["JUNK"]) == 3 and ctx.skipped == {"record": 1}, (len(bits["JUNK"]), ctx.skipped)


def test_quill_counts_fit_bits():
    # STRS, SYID and FDPC counts past their bits read what the bits hold
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)]))
    for name, at, fmt in (("STRS", 0, "<I"), ("SYID", 4, "<I"), ("FDPC", 0, "<H")):
        raw = bytearray(bits[name][0])
        struct.pack_into(fmt, raw, at, 0xFFFF if fmt == "<H" else 0xFFFFFFFF)
        changed = dict(bits)
        changed[name] = [like(bytes(raw), bits[name][0])]
        got = pub_quill.stories(changed)
        assert [s.text for s in got.values()] == ["Hello\rWorld", "A\vB"], (name, got)
        changed[name] = [like(bytes(raw) + b"\x00", bits[name][0])]  # and a byte that is not a whole count
        assert [s.text for s in pub_quill.stories(changed).values()] == ["Hello\rWorld", "A\vB"], name
        if name == "FDPC":
            assert not any(isinstance(i, Bold) for s in got.values() for p in s.paras for i in p.inlines)
    # STRS lengths past the text give the stories what is left
    bits = pub_quill.bits(quill_fix(STORIES))
    raw = bytearray(bits["STRS"][0])
    struct.pack_into("<I", raw, 12, 0xFFFFFFFF)
    bits["STRS"] = [like(bytes(raw), bits["STRS"][0])]
    got = pub_quill.stories(bits)
    assert [got[7].text, got[9].text] == ["Hello\rWorldA\vB", ""], got


def test_story_room():
    # stories are charged to one limits.MAX_OUTPUT room (characters, and LINE a story and a break); past it the rest
    # are left out, counted text once
    stories = [(k, "abc\rdef") for k in range(10)]
    bits = pub_quill.bits(quill_fix(stories))
    cost = 7 + 2 * pub_quill.vsdx.LINE
    with patched(limits, MAX_OUTPUT=3 * cost):
        ctx = Context()
        got = pub_quill.stories(bits, ctx)
    assert sorted(got) == [0, 1, 2] and ctx.skipped == {"text": 1}, (sorted(got), ctx.skipped)
    ctx = Context()
    assert len(pub_quill.stories(bits, ctx)) == 10 and ctx.skipped == {}
    # bold runs are charged too
    bits = pub_quill.bits(quill_fix(stories, bold=[(7 * k, 7 * k + 1) for k in range(10)]))
    with patched(limits, MAX_OUTPUT=3 * cost):
        ctx = Context()
        got = pub_quill.stories(bits, ctx)
    assert sorted(got) == [0, 1] and ctx.skipped == {"text": 1}, (sorted(got), ctx.skipped)


def test_runs_meter():
    # the FDPC runs and their style blocks spend the document's limits.BIN_RECORDS: past it bold is lost (counted
    # record once) and the text stays; one style named by many runs is read once
    stories = [(1, "ab" * 200)]
    bold = [(k, k + 1) for k in range(0, 400, 2)]
    bits = pub_quill.bits(quill_fix(stories, bold=bold))
    with patched(limits, BIN_RECORDS=400 + 2):  # 400 runs and the bold style's one block (the plain one has none)
        ctx = Context()
        got = pub_quill.stories(bits, ctx)
        assert text_of(got[1].paras[0]) == "**a**b" * 200 and ctx.skipped == {}, ctx.skipped
    with patched(limits, BIN_RECORDS=100):
        ctx = Context()
        got = pub_quill.stories(bits, ctx)
        assert ctx.skipped == {"record": 1}, ctx.skipped
        assert text_of(got[1].paras[0]) == "**a**b" * 50 + "ab" * 150, got


def test_style_damaged():
    # a style whose length is under 4 or past its bit is not bold; the text stays
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(6, 11)]))
    raw = bytearray(bits["FDPC"][0])
    n = struct.unpack_from("<H", raw, 0)[0]
    strong = max(struct.unpack_from(f"<{n}H", raw, 8 + 4 * n))
    for size in (2, 0xFFFF):
        struct.pack_into("<I", raw, strong, size)
        changed = dict(bits)
        changed["FDPC"] = [like(bytes(raw), bits["FDPC"][0])]
        assert pub_quill.stories(changed)[7].paras[1] == Para([Text("World")]), size
    # a style offset at the bit's end is not bold
    raw = bytearray(bits["FDPC"][0])
    for i in range(n):
        struct.pack_into("<H", raw, 8 + 4 * n + 2 * i, len(raw) - 2)
    changed = {**bits, "FDPC": [like(bytes(raw), bits["FDPC"][0])]}
    assert pub_quill.stories(changed)[7].paras[1] == Para([Text("World")])


def test_runs_out_of_order():
    # run ends 6, 3, 5, 14 (styles plain, plain, bold, plain): the runs ending at or before the last end are skipped,
    # so the bold one covers nothing
    bits = pub_quill.bits(quill_fix(STORIES, bold=[(3, 5)]))
    raw = bits["FDPC"][0]
    n = struct.unpack_from("<H", raw, 0)[0]
    lasts = struct.unpack_from(f"<{n}I", raw, 8)
    plain, strong = struct.unpack_from("<2H", raw, 8 + 4 * n)
    assert n == 3 and pub_quill.stories(bits)[7].paras[0] == Para([Text("Hel"), Bold([Text("lo")])])
    def last(end):
        return lasts[0] + 2 * (end - 3)
    fdpc = (struct.pack("<H", 4) + raw[2:8] + struct.pack("<4I", last(6), last(3), last(5), last(14))
            + struct.pack("<4H", plain + 6, plain + 6, strong + 6, plain + 6) + raw[8 + 6 * n:])
    got = pub_quill.stories({**bits, "FDPC": [like(fdpc, raw)]})
    assert got[7].paras == (Para([Text("Hello")]), Para([Text("World")])), got[7].paras


def test_cells_and_links():
    # TCD offsets [0, 2, 5] over "abcde" -> ["ab", "cde"]; links -> the URL list
    bits = pub_quill.bits(quill_fix([(3, "xy"), (4, "abcde")], tcd={1: [0, 2, 5]},
                                    links=["https://a.example/", "https://b.example/"]))
    got = pub_quill.stories(bits)
    assert pub_quill.cells(bits, got[4]) == ["ab", "cde"]
    assert pub_quill.cells(bits, got[3]) == []
    assert pub_quill.links(bits) == ["https://a.example/", "https://b.example/"]
    # the file's form: each cell's end is the \r closing it, the last the story's end
    bits = pub_quill.bits(quill_fix([(5, "a1\r\rc3\r")], tcd={0: [0, 2, 3, 7]}))
    assert pub_quill.cells(bits, pub_quill.stories(bits)[5]) == ["a1", "", "c3"]


def test_cells_bounded():
    # a TCD naming more cells than the story has characters gives at most one more than the characters; ends out of
    # order give empty cells
    bits = pub_quill.bits(quill_fix([(1, "abc")], tcd={0: [0] + [3, 1, 2] * 300}))
    story = pub_quill.stories(bits)[1]
    got = pub_quill.cells(bits, story)
    assert len(got) == 4 and got[0] == "abc", got
    bits = pub_quill.bits(quill_fix([(1, "ab\rc\rde")], tcd={0: [0, 2, 1, 7]}))
    story = pub_quill.stories(bits)[1]
    assert pub_quill.cells(bits, story) == ["ab", "", "c\rde"]
    short = {"TCD ": [like(b"\x01\x00", bits["TCD "][0])]}
    assert pub_quill.cells(short, story) == []
    bits = pub_quill.bits(quill_fix([(1, "abc")], tcd={0: [0] + [3, 1, 2] * 300}))
    story = pub_quill.stories(bits)[1]
    raw = bytearray(bits["TCD "][0])
    struct.pack_into("<I", raw, 0, 0xFFFFFFFF)
    bits["TCD "] = [like(bytes(raw), bits["TCD "][0])]
    assert len(pub_quill.cells(bits, story)) == 4


def test_links_hostile():
    # POI's rules: a 0x34-byte bit holds none; more than 1,000 PLCs, or a list past the bit, stops that bit; an
    # empty link (first char 0) is skipped; a URL seen before is not listed twice
    good = pub_quill.bits(quill_fix(STORIES, links=["https://a.example/", "https://b.example/"]))["TOKN"][0]
    def links_of(*raws):
        return pub_quill.links({"TOKN": [like(r, good) for r in raws]})
    assert links_of(bytes(good)) == ["https://a.example/", "https://b.example/"]
    assert links_of(bytes(good), bytes(good)) == ["https://a.example/", "https://b.example/"]
    assert links_of(bytes(good)[:0x34]) == []
    raw = bytearray(good)
    struct.pack_into("<I", raw, 0, 1001)
    assert links_of(bytes(raw)) == []
    assert links_of(bytes(good)[:-4]) == ["https://a.example/"]
    assert links_of(bytes(good)[:0x6A]) == [] and links_of(bytes(good)[:6]) == []
    with patched(pub_quill, PLCS=1):
        assert links_of(bytes(good)) == []
    with patched(pub_quill, PLCS=2):
        assert links_of(bytes(good)) == ["https://a.example/", "https://b.example/"]
    # the first entry empty (its first character 0): its length in bytes leads to the second
    raw = bytes(good)
    assert links_of(raw[:0x68] + struct.pack("<HH", 4, 0) + raw[0x68 + 2 + 36:]) == ["https://b.example/"]
    # another PLC type, or a bit that is not a PLC, is not a link list
    raw = bytearray(good)
    struct.pack_into("<I", raw, 4, 8)
    assert links_of(bytes(raw)) == []
    assert pub_quill.links({"TOKN": [pub_quill.Bit(bytes(good), 0, "TOKN", 0)]}) == []


# -- pub.py: Publisher 2002 and later

def read_pub(blob, ctx=None):
    ctx = ctx if ctx is not None else Context()
    return pub.parse_compound(blob, ctx), ctx


def md(doc, ctx, hidden=False):
    with tempfile.TemporaryDirectory() as tmp:
        return markdown.render(doc, Path(tmp) / "assets", hidden, ctx.skipped)


def outline(doc):
    """[(heading or None, [para texts])] of the Doc's top-level blocks."""
    out = []
    for b in doc.blocks:
        if isinstance(b, Section):
            head = {"page": "Page", "master": "Master page"}[b.kind]
            head += f" {b.number}" if b.number is not None else f": {b.title}"
            out.append((head + (" (hidden)" if b.hidden else ""), [text_of(p) for p in b.blocks]))
        else:
            out.append((None, [text_of(b)]))
    return out


def page(seq, shapes=(), **kw):
    return dict(seq=seq, shapes=list(shapes), **kw)


def shape(seq, text=None, **kw):
    return dict(seq=seq, text=text, **kw)


def one_page(texts, **kw):
    """A file of one page whose shapes 0x30, 0x31, ... hold the stories `texts` (text ids 1, 2, ...)."""
    seqs = [0x30 + k for k in range(len(texts))]
    return pub_file([page(0x20, seqs)], [shape(s, k + 1) for k, s in enumerate(seqs)],
                    [(k + 1, t) for k, t in enumerate(texts)], **kw)


def test_text_boxes_in_order():
    # page 1, boxes at top 100 and top 50 -> the top-50 text first; at the same top the left one first (a negative
    # left included); a box with no position last. The Escher stream's Dgg and Dg carry Publisher's 4-byte tails.
    escher = pub_escher(pub_sp(0x30, (0, 100, 10, 110)), pub_sp(0x31, (0, 50, 10, 60)), pub_sp(0x33, (-5, 100, 0, 110)))
    doc, ctx = read_pub(one_page(["Low", "High", "Free", "Left"], escher=escher))
    assert outline(doc) == [("Page 1", ["High", "Left", "Low", "Free"])], outline(doc)
    assert not ctx.skipped
    # without the Escher stream the list order stays
    doc, _ = read_pub(one_page(["Low", "High", "Free", "Left"]))
    assert outline(doc) == [("Page 1", ["Low", "High", "Free", "Left"])], outline(doc)
    # an anchor's corners in either order: the box is arranged, its top the smaller y
    escher = pub_escher(pub_sp(0x30, (0, 100, 10, 90)), pub_sp(0x31, (0, 95, 10, 99)))
    doc, _ = read_pub(one_page(["A", "B"], escher=escher))
    assert outline(doc) == [("Page 1", ["A", "B"])], outline(doc)


def test_group_boxes():
    # a group (0x30) listed on the page brings its members (directory parent 0x40), placed by their child anchors in
    # the group's coordinate system, mapped to the group's box as libmspub does: (child - cs) * box / cs + box
    group = pub_group(pub_sp(0x40, (0, 200, 100, 300), group=(0, 0, 10, 10)),
                      pub_sp(0x41, child=(0, 8, 10, 10)), pub_sp(0x42, child=(0, 0, 10, 2)))
    escher = pub_escher(pub_sp(0x30, (0, 250, 10, 260)), group)
    blob = pub_file([page(0x20, [0x40, 0x30])],
                    [shape(0x30, 1), shape(0x40, type=0x30), shape(0x41, 2, parent=0x40), shape(0x42, 3, parent=0x40)],
                    [(1, "Mid"), (2, "Bottom"), (3, "Top")], escher=escher)
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Top", "Mid", "Bottom"])], outline(doc)
    assert not ctx.skipped
    # a nested group (0x31, LOGO) inside the group: the members in directory order (0x42 before 0x43's member)
    blob = pub_file([page(0x20, [0x40, 0x42])],
                    [shape(0x40, type=0x30), shape(0x43, type=0x31, parent=0x40),
                     shape(0x41, 2, parent=0x43), shape(0x42, 3, parent=0x40)],
                    [(2, "Inner"), (3, "Outer")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Outer", "Inner"])] and not ctx.skipped, outline(doc)
    # two groups each the other's member (the directory cuts the link closing the cycle), and a group its own member:
    # each shape once, no loop
    blob = pub_file([page(0x20, [0x40, 0x43, 0x44, 0x41])],
                    [shape(0x40, type=0x30, parent=0x43), shape(0x43, type=0x31, parent=0x40),
                     shape(0x44, type=0x30, parent=0x44), shape(0x41, 2, parent=0x43), shape(0x42, 3, parent=0x40)],
                    [(2, "Inner"), (3, "Outer")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Outer", "Inner"])] and not ctx.skipped, outline(doc)


def test_group_depth():
    # groups nested past limits.MAX_DEPTH: the deeper members are left out, counted depth once
    shapes = [shape(0x40, type=0x30)] + [shape(0x41 + k, type=0x30, parent=0x40 + k) for k in range(5)]
    shapes.append(shape(0x50, 1, parent=0x45))
    shapes.append(shape(0x51, 2, parent=0x40))
    blob = pub_file([page(0x20, [0x40])], shapes, [(1, "Deep"), (2, "Shallow")])
    with patched(limits, MAX_DEPTH=3):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Shallow"])] and ctx.skipped == {"depth": 1}, (outline(doc), ctx.skipped)
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Deep", "Shallow"])] and not ctx.skipped, (outline(doc), ctx.skipped)


def test_escher_depth():
    # Escher groups nested past limits.MAX_DEPTH: the deeper boxes are not read (their shapes follow, unplaced),
    # counted depth once
    inner = pub_group(pub_sp(group=(0, 0, 10, 10)), pub_sp(0x30, (0, 1, 1, 2)))
    for _ in range(4):
        inner = pub_group(pub_sp(group=(0, 0, 10, 10)), inner)
    escher = pub_escher(inner, pub_sp(0x31, (0, 5, 1, 6)))
    with patched(limits, MAX_DEPTH=3):
        doc, ctx = read_pub(one_page(["Deep", "Placed"], escher=escher))
    assert outline(doc) == [("Page 1", ["Placed", "Deep"])] and ctx.skipped == {"depth": 1}, (outline(doc),
                                                                                              ctx.skipped)
    doc, ctx = read_pub(one_page(["Deep", "Placed"], escher=escher))
    assert outline(doc) == [("Page 1", ["Deep", "Placed"])] and not ctx.skipped


def test_escher_hostile():
    # records that overrun, short anchors and FSPGRs, ClientData without a shape id, values cut mid-pair: no box,
    # nothing counted, the text stays in list order
    def order(escher):
        doc, ctx = read_pub(one_page(["A", "B"], escher=escher))
        assert not ctx.skipped, ctx.skipped
        return outline(doc)[0][1]
    good = pub_escher(pub_sp(0x30, (0, 9, 1, 10)), pub_sp(0x31, (0, 1, 1, 2)))
    assert order(good) == ["B", "A"]
    assert order(good[:-30]) == ["A", "B"]                      # the Dg overruns the stream
    assert order(b"") == ["A", "B"]
    bad = bytearray(good)
    struct.pack_into("<I", bad, 4, 0xFFFFFFF0)                  # the Dgg's length passes the stream
    assert order(bytes(bad)) == ["A", "B"]
    # a ClientData with no 0x6801 value, and one cut after 3 bytes of its pair
    nodata = oa_container(0xF004, oa_record(0xF010, bytes(4) + struct.pack("<HI", 0x2002, 1)),
                          oa_record(0xF011, bytes(4) + struct.pack("<HI", 0x6802, 0x30)))
    cut = oa_container(0xF004, oa_record(0xF010, bytes(4) + struct.pack("<HI", 0x2002, 1)),
                       oa_record(0xF011, bytes(4) + struct.pack("<HI", 0x6801, 0x30)[:3]))
    assert order(pub_escher(nodata, cut, pub_sp(0x31, (0, 5, 1, 6)))) == ["B", "A"]
    # a ClientData with no anchor; a child anchor of 15 bytes; an FSPGR of 15 bytes (the coordinate system stays)
    noanchor = oa_container(0xF004, oa_record(0xF011, bytes(4) + struct.pack("<HI", 0x6801, 0x30)))
    short = oa_container(0xF004, oa_record(0xF00F, bytes(15)),
                         oa_record(0xF011, bytes(4) + struct.pack("<HI", 0x6801, 0x30)))
    assert order(pub_escher(noanchor, pub_sp(0x31, (0, 5, 1, 6)))) == ["B", "A"]
    assert order(pub_escher(short, pub_sp(0x31, (0, 5, 1, 6)))) == ["B", "A"]
    lead = oa_container(0xF004, oa_record(0xF009, bytes(15)),
                        oa_record(0xF010, bytes(4) + struct.pack("<4HI", 0, 0, 0, 0x2002, 0)[6:]))
    group = pub_group(lead, pub_sp(0x30, child=(0, 9, 1, 10)))
    assert order(pub_escher(group, pub_sp(0x31, (0, 5, 1, 6)))) == ["A", "B"]


def test_bold_runs():
    # a FDPC run bold over the second paragraph -> **World**
    doc, ctx = read_pub(one_page(["Hello\rWorld"], bold=[(6, 11)]))
    assert outline(doc) == [("Page 1", ["Hello", "**World**"])], outline(doc)
    assert "**World**" in md(doc, ctx) and "**Hello**" not in md(doc, ctx)


def test_pages_in_document_order():
    # page list [seq 2, seq 1] -> "## Page 1" holds seq 2's text
    blob = pub_file([page(0x21, [0x30]), page(0x22, [0x31])], [shape(0x30, 1), shape(0x31, 2)],
                    [(1, "first chunk"), (2, "second chunk")], order=[0x22, 0x21])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["second chunk"]), ("Page 2", ["first chunk"])], outline(doc)
    text = md(doc, ctx)
    assert text.index("## Page 1") < text.index("second chunk") < text.index("## Page 2") < text.index("first chunk")
    # no page list: the pages in seqnum order; a page with no text takes its number and gives no section
    blob = pub_file([page(0x22, [0x31]), page(0x21, [0x30]), page(0x23, [])], [shape(0x30, 1), shape(0x31, 2)],
                    [(1, "a"), (2, "b")], order=[])
    assert outline(read_pub(blob)[0]) == [("Page 1", ["a"]), ("Page 2", ["b"])]
    blob = pub_file([page(0x21, [0x30]), page(0x22, []), page(0x23, [0x31])], [shape(0x30, 1), shape(0x31, 2)],
                    [(1, "a"), (2, "b")])
    assert outline(read_pub(blob)[0]) == [("Page 1", ["a"]), ("Page 3", ["b"])]


def test_page_list_hostile():
    # a page listed twice, a seqnum no chunk has, a shape's seqnum and the DOCUMENT's own: each page once, in order
    blob = pub_file([page(0x21, [0x30]), page(0x22, [0x31])], [shape(0x30, 1), shape(0x31, 2)],
                    [(1, "one"), (2, "two")], order=[0x21, 0x21, 0x99, 0x30, 1, 0x22, 0x21])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["one"]), ("Page 2", ["two"])] and not ctx.skipped, outline(doc)
    # a page naming itself as its master, and its own seqnum among its shapes
    blob = pub_file([page(0x21, [0x21, 0x30], applied=0x21)], [shape(0x30, 1)], [(1, "one")])
    assert outline(read_pub(blob)[0]) == [("Page 1", ["one"])]


def test_shapes_once():
    # a shape on two pages, twice on one page, and two shapes naming one story: each text once, where first met
    blob = pub_file([page(0x21, [0x30, 0x30, 0x31]), page(0x22, [0x30, 0x32])],
                    [shape(0x30, 1), shape(0x31, 1), shape(0x32, 2)], [(1, "shared"), (2, "own")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["shared"]), ("Page 2", ["own"])] and not ctx.skipped, outline(doc)
    # a shape naming a text id no story has, and one with no text id: nothing, nothing counted
    blob = pub_file([page(0x21, [0x30, 0x31, 0x32])], [shape(0x30, 9), shape(0x31), shape(0x32, 1)], [(1, "kept")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["kept"])] and not ctx.skipped


def test_master_page():
    # master "Master A" with a footer box, two pages naming it -> one "## Master page: Master A" after the pages;
    # the footer text appears once
    blob = pub_file([page(0x25, [0x32], master="Master A"), page(0x21, [0x30], applied=0x25),
                     page(0x22, [0x31], applied=0x25)],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3)], [(1, "one"), (2, "two"), (3, "Footer")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["one"]), ("Page 2", ["two"]), ("Master page: Master A", ["Footer"])], \
        outline(doc)
    text = md(doc, ctx)
    assert text.count("Footer") == 1 and "## Master page: Master A" in text, text
    assert text.index("## Page 2") < text.index("## Master page: Master A")


def test_master_cycle():
    # masters naming themselves and each other, one not in the page list, one with no shapes: each once, no loop
    blob = pub_file([page(0x25, [0x32], master="M1", applied=0x26), page(0x26, [0x33], master="M2", applied=0x25),
                     page(0x27, [], master="M3", applied=0x27), page(0x28, [0x34], master="M4", applied=0x28),
                     page(0x21, [0x30], applied=0x25)],
                    [shape(0x30, 1), shape(0x32, 2), shape(0x33, 3), shape(0x34, 4)],
                    [(1, "body"), (2, "m1"), (3, "m2"), (4, "m4")], order=[0x25, 0x26, 0x27, 0x21])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["body"]), ("Master page: M1", ["m1"]), ("Master page: M2", ["m2"]),
                            ("Master page: M4", ["m4"])] and not ctx.skipped, outline(doc)


def test_scratch_area_hidden():
    # a DUMMY 0x10D shape -> absent by default, under "Scratch area" with --hidden; a DUMMY page with a master name
    # is still the scratch area
    blob = pub_file([page(0x21, [0x30]), page(0x10D, [0x31]), page(0x117, [0x32], master="X"), page(0x110, [])],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3)], [(1, "Body"), (2, "Secret"), (3, "Note")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Body"]), ("Page: Scratch area (hidden)", ["Secret", "Note"])], outline(doc)
    left, shown = md(doc, ctx), md(doc, ctx, hidden=True)
    assert "Secret" not in left and "Scratch area" not in left and "Body" in left, left
    assert "Scratch area" in shown and "Secret" in shown and "Note" in shown, shown


def test_links_line():
    # two URLs -> the last block "Links: https://a.example/, https://b.example/"
    doc, ctx = read_pub(one_page(["Body"], links=["https://a.example/", "https://b.example/"]))
    last = doc.blocks[-1]
    assert isinstance(last, Para) and last.inlines == [
        Text("Links: "), Link("https://a.example/", [Text("https://a.example/")]), Text(", "),
        Link("https://b.example/", [Text("https://b.example/")])], last
    assert md(doc, ctx).rstrip().splitlines()[-1] == "Links: https://a.example/, https://b.example/"
    doc, _ = read_pub(one_page(["Body"]))
    assert not isinstance(doc.blocks[-1], Para)


def test_output_room():
    # each story emitted and each link is charged to the document's room (its characters, and vsdx.LINE for the story
    # and each break and bold run, a link LINE): past it the story is left out, counted text, the rest of the links
    # cut, counted link once
    blob = one_page(["a" * 100, "b" * 10, "c\rd"], bold=[(0, 5)], links=["https://a.example/", "https://b.example/"])
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    line = vsdx.LINE

    def run(room):
        ctx = Context()
        reader = pub._Reader(*streams, b"", ctx)
        reader.room = room
        return outline(reader.run()), ctx.skipped
    full = 100 + 2 * line + 10 + line + 3 + 2 * line + 2 * (len("https://a.example/") + line)
    got, skipped = run(full)
    assert got == [("Page 1", ["**aaaaa**" + "a" * 95, "b" * 10, "c", "d"]),
                   (None, ["Links: https://a.example/, https://b.example/"])] and not skipped, (got, skipped)
    got, skipped = run(full - 1)
    assert got[-1] == (None, ["Links: https://a.example/"]) and skipped == {"link": 1}, (got, skipped)
    got, skipped = run(10 + line + 3 + 2 * line - 1)  # the first and third stories do not fit, the second does
    assert got == [("Page 1", ["b" * 10]), (None, ["Links: https://a.example/"])], got
    assert skipped == {"text": 2, "link": 1}, skipped
    # a master's name is charged too: past the room it is left out (text)
    blob = pub_file([page(0x25, [0x30], master="N" * 50)], [shape(0x30, 1)], [(1, "x")])
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    ctx = Context()
    reader = pub._Reader(*streams, b"", ctx)
    reader.room = 1 + line + 10
    assert outline(reader.run()) == [("Master page: ", ["x"])] and ctx.skipped == {"text": 1}, ctx.skipped


def test_guards():
    # a shape that fails is left out (shape), the page's other shapes stay; a page that fails (page) and a master that
    # fails (master_page) are left out, the others stay
    blob = pub_file([page(0x25, [0x32], master="M"), page(0x21, [0x30, 0x31]), page(0x22, [0x33])],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3), shape(0x33, 4)],
                    [(1, "a"), (2, "b"), (3, "m"), (4, "c")], order=[0x21, 0x22])
    real_text, real_shapes = pub._Reader._text, pub._Reader._shapes

    def bad_text(self, ref):
        if ref.seqnum == 0x31:
            raise ValueError("boom")
        return real_text(self, ref)

    def bad_shapes(self, seqs):
        if 0x33 in seqs or 0x32 in seqs:
            raise ValueError("boom")
        return real_shapes(self, seqs)
    with patched(pub._Reader, _text=bad_text):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["a"]), ("Page 2", ["c"]), ("Master page: M", ["m"])], outline(doc)
    assert ctx.skipped == {"shape": 1}, ctx.skipped
    with patched(pub._Reader, _shapes=bad_shapes):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["a", "b"])] and ctx.skipped == {"page": 1, "master_page": 1}, ctx.skipped
    # a scratch page that fails is counted page; the other scratch pages stay
    blob = pub_file([page(0x10D, [0x33]), page(0x110, [0x30])], [shape(0x30, 1), shape(0x33, 4)],
                    [(1, "kept"), (4, "lost")])
    with patched(pub._Reader, _shapes=bad_shapes):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page: Scratch area (hidden)", ["kept"])] and ctx.skipped == {"page": 1}, ctx.skipped


def test_meter():
    # one meter of limits.BIN_RECORDS for the document's Contents walks, its Escher records and its Quill styles: at
    # any cap the read ends without error, record counted at most once; with room, nothing counted
    escher = pub_escher(pub_sp(0x30, (0, 9, 1, 10)), pub_sp(0x31, (0, 1, 1, 2)))
    blob = pub_file([page(0x25, [0x32], master="M"), page(0x21, [0x30, 0x31]), page(0x10D, [0x33])],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3), shape(0x33, 4)],
                    [(1, "a"), (2, "b"), (3, "m"), (4, "s")], escher=escher, bold=[(0, 1)], order=[0x21])
    seen = set()
    for cap in range(0, 1200, 7):
        with patched(limits, BIN_RECORDS=cap):
            try:
                doc, ctx = read_pub(blob)
            except ReadError as e:
                assert (e.kind, e.detail) == ("damaged", "no document chunk"), (cap, e.kind, e.detail)
                continue
        assert set(ctx.skipped) <= {"record"} and ctx.skipped["record"] <= 1, (cap, ctx.skipped)
        seen.add(len(doc.blocks))
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["b", "**a**"]), ("Master page: M", ["m"]),
                            ("Page: Scratch area (hidden)", ["s"])] and not ctx.skipped, outline(doc)
    assert len(seen) > 2, seen
    # the Escher walk ticks it: a cap that the Contents and Quill walks fit, but not the Escher records
    ctx = Context()
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS", "Escher/EscherStm")]
    meter = pub_contents.Meter(ctx)
    assert pub.boxes(streams[2], meter, ctx) == {0x30: (9, 0), 0x31: (1, 0)} and meter.used > 10, meter.used
    with patched(limits, BIN_RECORDS=3):
        ctx = Context()
        meter = pub_contents.Meter(ctx)
        assert pub.boxes(streams[2], meter, ctx) == {} and ctx.skipped == {"record": 1}


def test_vba():
    # a VBA storage sets vba (named once); a stream merely named VBA does not
    doc, ctx = read_pub(one_page(["t"], streams={"VBA/dir": b"\x01" * 16, "VBA/ThisDocument": b"x"}))
    assert ctx.skipped == {"vba": 1}, ctx.skipped
    assert "VBA macros" in md(doc, ctx)
    doc, ctx = read_pub(one_page(["t"], streams={"Macros/VBA/dir": b"\x01" * 16}))
    assert ctx.skipped == {"vba": 1}, ctx.skipped
    doc, ctx = read_pub(one_page(["t"], streams={"VBA": b"\x01" * 16}))
    assert not ctx.skipped


def refused(fn, kind, detail):
    try:
        fn()
    except ReadError as e:
        assert (e.kind, e.detail) == (kind, detail), (e.kind, e.detail)
        return
    raise AssertionError("ReadError not raised")


def test_unknown_version():
    # byte 0x30 -> unsupported "unknown Publisher version (Contents byte 0x30)"; 0x22 is Publisher 2000 or 98, not
    # read yet; a Contents stream not starting E8 AC is damaged
    for byte, kind, detail in ((0x30, "unsupported", "unknown Publisher version (Contents byte 0x30)"),
                               (0xA1, "unsupported", "unknown Publisher version (Contents byte 0xA1)"),
                               (0x22, "unsupported", "Publisher 2000 or 98")):
        refused(lambda: read_pub(one_page(["t"], version=byte)), kind, detail)
    with Compound(one_page(["t"])) as cf:
        quill = cf.read("Quill/QuillSub/CONTENTS")
    for contents in (b"", b"\xE8\xAC\x2C", b"\xE8\xAD\x2C\x00" + bytes(60)):
        blob = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": quill})[0]
        refused(lambda: read_pub(blob), "damaged", "not a Publisher Contents stream")


def test_no_quill_no_document():
    # without a Quill stream, or without a DOCUMENT chunk, a 2002+ file is damaged
    refused(lambda: read_pub(one_page(["t"], quill=False)), "damaged", "no Quill stream")
    with Compound(one_page(["t"])) as cf:
        quill = cf.read("Quill/QuillSub/CONTENTS")
    contents = contents_fix([(2, pub_contents.PAGE, None, pub_block(0x02, 0xA0))])
    refused(lambda: read_pub(cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": quill})[0]),
            "damaged", "no document chunk")


def test_no_contents_routes():
    # a .pub compound with only "WordDocument" -> routed to doc; one with neither Contents nor another format's stream
    # (a Quill stream alone included) is damaged
    assert read_pub(cfb_bytes({"WordDocument": bytes(64)})[0])[0] == "doc"
    for streams in ({"Other": b"x"}, {"Quill/QuillSub/CONTENTS": b"CHNKINK "}):
        refused(lambda: read_pub(cfb_bytes(streams)[0]), "damaged", "no Contents stream")
    # parse: a file that is not a compound file is damaged; a clean one is read through parse_compound
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "a.pub"
        p.write_bytes(b"not a compound file at all")
        refused(lambda: pub.parse(p, Context()), "damaged", "not a Publisher file")
        p.write_bytes(one_page(["hello"]))
        assert outline(pub.parse(p, Context())) == [("Page 1", ["hello"])]
        # a zip goes to zip_route; a compound file of another format to the parser cfb_route names
        p.write_bytes(b"PK\x03\x04" + bytes(60))
        with patched(zip_route, parse=lambda path, ctx: ("zip", path)):
            assert pub.parse(p, Context()) == ("zip", p)
        p.write_bytes(cfb_bytes({"VisioDocument": b"Visio (TM) Drawing\r\n"})[0])
        with patched(cfb_route, parse=lambda fmt, path, ctx: ("routed", fmt)):
            assert pub.parse(p, Context()) == ("routed", "vsd")
    # a Contents stream whose sector chain loops: damaged "Contents stream"
    with Compound(one_page(["t"])) as cf:
        quill = cf.read("Quill/QuillSub/CONTENTS")
    raw, info = cfb_bytes({"Contents": b"\xE8\xAC\x2C\x00" + bytes(5000), "Quill/QuillSub/CONTENTS": quill})
    chain, blob = info["chains"]["Contents"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    refused(lambda: read_pub(bytes(blob)), "damaged", "Contents stream")


def _aliased(chunks, refs):
    """A 2002 Contents stream of the chunks `chunks` (block bytes) whose directory entry k + 1 (entry 0 a DUMMY block)
    is refs[k], a (chunk type, chunk index) pair: many entries may name one chunk's offset."""
    body = bytearray(b"\xE8\xAC\x2C\x00" + bytes(0x3C))
    offsets = []
    for blocks in chunks:
        offsets.append(len(body))
        body += struct.pack("<I", len(blocks) + 4) + blocks
    entries = bytearray(pub_block(0, 0x78))
    for ctype, k in refs:
        entries += pub_block(0, 0x88, pub_block(0x02, 0x18, ctype) + pub_block(0x04, 0xB8, offsets[k]))
    trailer = pub_block(1, 0x20, 0) + pub_block(2, 0x20, 0) + pub_block(3, 0x90, bytes(entries))
    toff = len(body)
    body += struct.pack("<I", len(trailer) + 4) + trailer
    struct.pack_into("<I", body, 0x1A, toff)
    return bytes(body)


def _shape_aliases(n, size):
    """Contents: DOCUMENT (seq 1) listing page 2, page 2 listing shapes 3 .. n + 2, every one of them a directory entry
    naming one shape chunk (text id 1, and a 0xC0 block of `size` bytes)."""
    doc = pub_block(0x02, 0xA0, pub_block(0, 0x70, 2))
    pg = pub_block(0x02, 0xA0, b"".join(pub_block(0, 0x70, 3 + k) for k in range(n)))
    sh = pub_block(0x27, 0x20, 1) + pub_block(0x10, 0xC0, b"s" * size)
    return _aliased([doc, pg, sh], [(pub_contents.DOCUMENT, 0), (pub_contents.PAGE, 1)] + [(pub_contents.SHAPE, 2)] * n)


def _page_aliases(n, size):
    """Contents: DOCUMENT (seq 1) listing pages 2 .. n + 1, every one naming one PAGE chunk whose 0x0E master name is
    `size` bytes and whose shape list is seq n + 2 (text id 1)."""
    doc = pub_block(0x02, 0xA0, b"".join(pub_block(0, 0x70, 2 + k) for k in range(n)))
    pg = pub_block(0x02, 0xA0, pub_block(0, 0x70, n + 2)) + pub_block(0x0E, 0xC0, b"M\x00" * (size // 2))
    sh = pub_block(0x27, 0x20, 1)
    return _aliased([doc, pg, sh], [(pub_contents.DOCUMENT, 0)] + [(pub_contents.PAGE, 1)] * n
                    + [(pub_contents.SHAPE, 2)])


def _counting_chunks(contents, quill):
    """(outline, skipped, the chunk offsets walked, meter used) of reading `contents` with `quill`."""
    walked = []
    real = pub._Reader._chunk

    def chunk(self, ref):
        walked.append(ref.offset)
        return real(self, ref)
    ctx = Context()
    with patched(pub._Reader, _chunk=chunk):
        reader = pub._Reader(contents, quill, b"", ctx)
        doc = reader.run()
    return outline(doc), ctx.skipped, walked, reader.meter.used


def test_aliased_offsets():
    # many directory entries naming one chunk offset: the chunk is walked once (its string blocks copied once), so the
    # work is linear in the bytes. The meter: 3 ticks a directory entry, 1 a list entry, 1 a shape placed (a page in
    # each of the page and master passes); a walk of the aliased chunk per entry would add 2 or 3 more
    quill = quill_fix([(1, "hello")])
    got, skipped, walked, used = _counting_chunks(_shape_aliases(2000, 4096), quill)
    assert got == [("Page 1", ["hello"])] and not skipped, (got, skipped)
    assert len(walked) == len(set(walked)) == 3, len(walked)
    assert used <= 2000 * 5 + 100, used
    got, skipped, walked, used = _counting_chunks(_page_aliases(2000, 4096), quill)
    assert got == [("Master page: " + "M" * 2048, ["hello"])] and not skipped, (got, skipped)
    assert len(walked) == len(set(walked)) == 3, len(walked)
    assert used <= 2000 * 6 + 100, used
    # timing-bounded: 20,000 aliases of a 256 KiB string block, well under a second where each alias walked would copy
    # 5 GiB
    contents = _shape_aliases(20_000, 256 * 1024)
    t = time.perf_counter()
    got, skipped, walked, _ = _counting_chunks(contents, quill)
    assert got == [("Page 1", ["hello"])] and len(walked) == 3 and time.perf_counter() - t < 2.0


def test_chunk_span():
    # a chunk whose declared length runs past the next chunk is read to that chunk only, counted record once
    blob = one_page(["a", "b", "c"])
    with Compound(blob) as cf:
        contents, quill = (cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS"))
    refs = {r.seqnum: r for r in pub_contents.directory(contents, Context())}
    bad = bytearray(contents)
    for seq in (0x30, 0x31):
        struct.pack_into("<I", bad, refs[seq].offset, 0x7FFFFFF0)
    ctx = Context()
    doc = pub._Reader(bytes(bad), quill, b"", ctx).run()
    assert outline(doc) == [("Page 1", ["a", "b", "c"])] and ctx.skipped == {"record": 1}, (outline(doc), ctx.skipped)


def test_span_by_offset():
    # a chunk's span ends at the next chunk in byte order, whatever the directory order: the page (its length too long)
    # stops at shape A, which follows it in the stream though its directory entry comes after B's, so A's shape list
    # (naming C) is not read as the page's
    doc = pub_block(0x02, 0xA0, pub_block(0, 0x70, 2))
    pg = pub_block(0x02, 0xA0, pub_block(0, 0x70, 3))
    a = pub_block(0x27, 0x20, 2) + pub_block(0x02, 0xA0, pub_block(0, 0x70, 5))
    b, c = pub_block(0x27, 0x20, 1), pub_block(0x27, 0x20, 3)
    S, P, D = pub_contents.SHAPE, pub_contents.PAGE, pub_contents.DOCUMENT
    contents = bytearray(_aliased([doc, pg, a, b, c], [(D, 0), (P, 1), (S, 3), (S, 2), (S, 4)]))
    at = pub_contents.directory(bytes(contents), Context())[1].offset
    struct.pack_into("<I", contents, at, 0x7FFFFFF0)
    ctx = Context()
    doc = pub._Reader(bytes(contents), quill_fix([(1, "b"), (2, "a"), (3, "c")]), b"", ctx).run()
    assert outline(doc) == [("Page 1", ["b"])] and ctx.skipped == {"record": 1}, (outline(doc), ctx.skipped)


def test_page_list_entries():
    # the DOCUMENT page list takes its entries of id 0 only
    doc = pub_block(0x02, 0xA0, pub_block(0, 0x70, 2) + pub_block(1, 0x70, 3))
    pages = [pub_block(0x02, 0xA0, pub_block(0, 0x70, 4)), pub_block(0x02, 0xA0, pub_block(0, 0x70, 5))]
    shapes = [pub_block(0x27, 0x20, 1), pub_block(0x27, 0x20, 2)]
    S, P, D = pub_contents.SHAPE, pub_contents.PAGE, pub_contents.DOCUMENT
    contents = _aliased([doc] + pages + shapes, [(D, 0), (P, 1), (P, 2), (S, 3), (S, 4)])
    doc = pub._Reader(contents, quill_fix([(1, "listed"), (2, "not listed")]), b"", Context()).run()
    assert outline(doc) == [("Page 1", ["listed"])], outline(doc)


def test_escher_share():
    # an Escher stream of filler records spends at most half the meter: the pages are still read, their text in list
    # order, counted record once
    filler = pub_escher(*[oa_record(0xF00B)] * 3000)
    with patched(limits, BIN_RECORDS=2000):
        doc, ctx = read_pub(one_page(["A", "B"], escher=filler))
    assert outline(doc) == [("Page 1", ["A", "B"])] and ctx.skipped == {"record": 1}, (outline(doc), ctx.skipped)
    # the share and then the meter itself run out: record still counted once
    blob = one_page(["A", "B"], escher=filler)
    for cap in range(0, 200, 3):
        with patched(limits, BIN_RECORDS=cap):
            try:
                _, ctx = read_pub(blob)
            except ReadError:
                continue
        assert ctx.skipped == {"record": 1}, (cap, ctx.skipped)
    with patched(limits, BIN_RECORDS=2000):
        doc, ctx = read_pub(one_page(["A", "B"], escher=pub_escher(pub_sp(0x30, (0, 9, 1, 10)),
                                                                     pub_sp(0x31, (0, 1, 1, 2)))))
    assert outline(doc) == [("Page 1", ["B", "A"])] and not ctx.skipped


def test_broken_stream_named():
    # a stream whose sector chain loops is named in the damaged detail
    with Compound(one_page(["t"])) as cf:
        contents = cf.read("Contents")
    raw, info = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": b"CHNKINK " + bytes(5000)})
    chain, blob = info["chains"]["Quill/QuillSub/CONTENTS"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    refused(lambda: read_pub(bytes(blob)), "damaged", "Quill/QuillSub/CONTENTS stream")
    raw, info = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": b"CHNKINK ",
                           "Escher/EscherStm": bytes(5000)})
    chain, blob = info["chains"]["Escher/EscherStm"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    refused(lambda: read_pub(bytes(blob)), "damaged", "Escher/EscherStm stream")


def _anchor(box):
    """A ClientAnchor record of the values 0x2001-0x2004 (xs, ys, xe, ye)."""
    return oa_record(0xF010, bytes(4) + b"".join(struct.pack("<HI", k, v & 0xFFFFFFFF)
                                                 for k, v in zip((0x2001, 0x2002, 0x2003, 0x2004), box)))


def _data(seq):
    """A ClientData record naming the Contents shape `seq`."""
    return oa_record(0xF011, bytes(4) + struct.pack("<HI", 0x6801, seq))


def test_escher_records():
    # within an SpContainer the first anchor, ClientData and FSPGR count; a 15-byte FSPGR sets no coordinate system
    # and no group box; outside the DgContainer no shape is placed
    def order(escher, texts=("A", "B")):
        doc, ctx = read_pub(one_page(list(texts), escher=escher))
        assert not ctx.skipped, ctx.skipped
        return outline(doc)[0][1]
    ref = pub_sp(0x31, (0, 5, 1, 6))
    assert order(pub_escher(oa_container(0xF004, _anchor((0, 1, 1, 2)), _anchor((0, 9, 1, 10)), _data(0x30)),
                            ref)) == ["A", "B"]
    assert order(pub_escher(oa_container(0xF004, _anchor((0, 1, 1, 2)), _data(0x30), _data(0x31)), ref)) == ["A", "B"]
    lead = oa_container(0xF004, oa_record(0xF009, struct.pack("<4i", 0, 0, 10, 10)),
                        oa_record(0xF009, struct.pack("<4i", 0, 0, 1000, 1000)), _anchor((0, 100, 10, 200)),
                        _data(0x50))
    group = pub_group(lead, pub_sp(0x30, child=(0, 5, 1, 6)))         # top 100 + 5 * 100 / 10 = 150
    assert order(pub_escher(group, pub_sp(0x31, (0, 120, 1, 121)))) == ["B", "A"]
    lead = oa_container(0xF004, oa_record(0xF009, bytes(15)), _anchor((0, 100, 10, 200)), _data(0x50))
    group = pub_group(lead, pub_sp(0x30, child=(0, 9, 1, 10)))         # the box stays (0, 0, 0, 0): top 0
    assert order(pub_escher(group, ref)) == ["A", "B"]
    outside = oa_container(0xF001, pub_group(pub_sp(group=(0, 0, 10, 10)), pub_sp(0x30, (0, 1, 1, 2))))
    assert order(outside + pub_escher(ref)) == ["B", "A"]
    # a child anchor is taken relative to its group's coordinate system: x 105 in (100..110) maps to left 50
    group = pub_group(pub_sp(0x50, (0, 200, 100, 300), group=(100, 0, 110, 10)),
                      pub_sp(0x30, child=(105, 0, 106, 1)), pub_sp(0x31, child=(102, 0, 103, 1)))
    assert order(pub_escher(group, pub_sp(0x32, (500, 200, 510, 210))), "ABC") == ["B", "A", "C"]
    # a nested group's coordinate system is its own: the outer group's later members keep the outer one
    inner = pub_group(pub_sp(0x51, child=(0, 0, 10, 10), group=(0, 0, 1000, 1000)),
                      pub_sp(0x30, child=(0, 0, 1000, 100)))
    outer = pub_group(pub_sp(0x50, (0, 200, 100, 300), group=(0, 0, 10, 10)), inner,
                      pub_sp(0x32, child=(0, 5, 10, 6)))
    assert order(pub_escher(outer, pub_sp(0x31, (0, 220, 10, 221))), "ABC") == ["A", "B", "C"]
    # two Escher branches past limits.MAX_DEPTH: depth counted once
    deep = pub_group(pub_sp(group=(0, 0, 10, 10)), pub_sp(0x30, (0, 1, 1, 2)))
    for _ in range(4):
        deep = pub_group(pub_sp(group=(0, 0, 10, 10)), deep)
    with patched(limits, MAX_DEPTH=3):
        _, ctx = read_pub(one_page(["A", "B"], escher=pub_escher(deep, deep, ref)))
    assert ctx.skipped == {"depth": 1}, ctx.skipped


def test_escher_values():
    # the (id, value) pairs tick the meter, and a pair cut short is not read (the bytes past the record unread)
    body = bytes(4) + b"".join(struct.pack("<HI", k, k) for k in range(10))
    with patched(limits, BIN_RECORDS=3):
        assert pub._values(body, 0, len(body), pub_contents.Meter()) == {0: 0, 1: 1, 2: 2}
    body = bytes(4) + struct.pack("<HI", 1, 7) + struct.pack("<HI", 0x6801, 0x30)[:3] + bytes(8)
    assert pub._values(body, 0, 13, pub_contents.Meter()) == {1: 7}


def test_reads_once():
    # each page chunk is read once (cached between the page, master and scratch passes); each shape is placed once a
    # document (listed twice, or a group listed on two pages); a page's own seqnum, or another page's, is no shape
    blob = pub_file([page(0x25, [0x32], master="M"), page(0x21, [0x30, 0x30, 0x40, 0x21, 0x22]),
                     page(0x22, [0x40, 0x31]), page(0x10D, [0x33])],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3), shape(0x33, 4), shape(0x40, type=0x30),
                     shape(0x41, 5, parent=0x40)],
                    [(1, "a"), (2, "b"), (3, "m"), (4, "s"), (5, "g")], order=[0x21, 0x22])
    chunks, texts = [], []
    real_chunk, real_text = pub._Reader._chunk, pub._Reader._text

    def chunk(self, ref):
        chunks.append(ref.seqnum)
        return real_chunk(self, ref)

    def text(self, ref):
        texts.append(ref.seqnum)
        return real_text(self, ref)
    with patched(pub._Reader, _chunk=chunk, _text=text):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["a", "g"]), ("Page 2", ["b"]), ("Master page: M", ["m"]),
                            ("Page: Scratch area (hidden)", ["s"])] and not ctx.skipped, outline(doc)
    pages = [s for s in chunks if s in (0x21, 0x22, 0x25, 0x10D)]
    assert sorted(pages) == [0x21, 0x22, 0x25, 0x10D], pages
    assert texts == [0x30, 0x41, 0x31, 0x32, 0x33], texts
    # a spent meter: no page is read, no shape placed, record counted once
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    ctx = Context()
    reader = pub._Reader(*streams, b"", ctx)
    read = []
    real_page = pub._Reader._page

    def page_of(self, ref):
        read.append(ref.seqnum)
        return real_page(self, ref)
    with patched(limits, BIN_RECORDS=0), patched(pub._Reader, _page=page_of):
        assert reader.run().blocks == [] and read == [] and ctx.skipped == {"record": 1}, (read, ctx.skipped)
        assert reader._expand([0x30, 0x40]) == []
    # a chunk whose offset leaves no room for its length has no blocks
    assert list(reader._chunk(ChunkRef(pub_contents.PAGE, len(reader.data) - 3, 0, None, 99))) == []


def test_group_depth_once():
    # two members past limits.MAX_DEPTH (groups 0x40 > 0x41 > 0x42 > 0x43, then two text shapes): depth counted once
    shapes = [shape(0x40, type=0x30)] + [shape(0x41 + k, type=0x30, parent=0x40 + k) for k in range(3)]
    shapes += [shape(0x50, 1, parent=0x43), shape(0x51, 2, parent=0x43), shape(0x52, 3, parent=0x40)]
    blob = pub_file([page(0x20, [0x40])], shapes, [(1, "a"), (2, "b"), (3, "kept")])
    with patched(limits, MAX_DEPTH=3):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["kept"])] and ctx.skipped == {"depth": 1}, (outline(doc), ctx.skipped)


def test_page_block_types():
    # a 0x0E block of a fixed type names no master; a page's shape entry of a type other than 0x70 names no shape; a
    # table shape gives its story
    extra = pub_block(0x0E, 0x20, 5) + pub_block(0x02, 0xA0, pub_block(0, 0x68, 0x31))
    blob = pub_file([page(0x21, [0x30, 0x32], extra=extra)],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3, type=pub_contents.TABLE)],
                    [(1, "kept"), (2, "not listed"), (3, "cell")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["kept", "cell"])] and not ctx.skipped, (outline(doc), ctx.skipped)


def test_titles_and_links_room():
    # a master name of an odd byte count loses its last byte; each name is charged in turn (past the room: left out,
    # text); a link past the room ends the line (counted link once) though a later one would fit
    line = vsdx.LINE
    blob = pub_file([page(0x25, [0x30], master="A" * 20), page(0x26, [0x31], master="B")],
                    [shape(0x30, 1), shape(0x31, 2)], [(1, "x"), (2, "y")])
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]

    def run(room):
        ctx = Context()
        reader = pub._Reader(*streams, b"", ctx)
        reader.room = room
        return outline(reader.run()), ctx.skipped
    full = (1 + line) + (20 + line) + (1 + line) + (1 + line)
    assert run(full) == ([("Master page: " + "A" * 20, ["x"]), ("Master page: B", ["y"])], {})
    assert run(full - 1) == ([("Master page: " + "A" * 20, ["x"]), ("Master page: ", ["y"])], {"text": 1})
    assert pub._Reader(*streams, b"", Context())._title(b"A\x00B") == "A"
    urls = ["https://a.example/", "https://" + "b" * 40 + ".example/", "https://c.example/"]
    with Compound(one_page(["Body"], links=urls)) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    ctx = Context()
    reader = pub._Reader(*streams, b"", ctx)
    reader.room = 4 + line + len(urls[0]) + line + len(urls[2]) + line
    assert outline(reader.run())[-1] == (None, ["Links: https://a.example/"]) and ctx.skipped == {"link": 1}


# -- measure: hostile-input probes, in memory

def _probe(name, fn, size):
    """`fn` timed, then run once more under tracemalloc for its peak (the input itself not counted)."""
    t = time.perf_counter()
    result = fn()
    took = time.perf_counter() - t
    tracemalloc.start()
    fn()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    mib = size / MIB
    print(f"{name:34} {mib:8.2f} MiB {took:7.2f} s {took / mib:7.3f} s/MiB  peak {peak / MIB:7.1f} MiB"
          f" ({peak / size:4.1f}x)  {result}", flush=True)


def _contents_with(directory_body, size):
    """A Contents stream of about `size` bytes whose trailer's directory body is `directory_body`."""
    head = bytearray(b"\xE8\xAC\x2C\x00" + bytes(0x3C))
    trailer = pub_block(1, 0x20, 0) + pub_block(2, 0x20, 0) + pub_block(3, 0x90, directory_body)
    pad = max(0, size - len(head) - len(trailer) - 4)
    head += bytes(pad)
    toff = len(head)
    struct.pack_into("<I", head, 0x1A, toff)
    return bytes(head) + struct.pack("<I", len(trailer) + 4) + trailer


def _run_directory(data):
    ctx = Context()
    refs = pub_contents.directory(data, ctx)
    return f"refs {len(refs)} {dict(ctx.skipped)}"


def _quill_with(bits, extra_lists=b"", next_list=0xFFFFFFFF):
    """A Quill stream holding `bits` ((name, kind, id, raw) tuples) in one list at 0x18."""
    entries, data = [], bytearray()
    at = 0x18 + 8 + 24 * len(bits) + len(extra_lists)
    for name, kind, bid, raw in bits:
        entries.append(struct.pack("<H4sHHH4sII", 0x18, name, bid, 1, 0, kind, at + len(data), len(raw)))
        data += raw
    return (b"CHNKINK " + bytes(0x10) + struct.pack("<HHI", 0, len(bits), next_list) + b"".join(entries)
            + extra_lists + bytes(data))


def _run_quill(data):
    ctx = Context()
    bits = pub_quill.bits(data, ctx)
    got = pub_quill.stories(bits, ctx)
    cells = sum(len(pub_quill.cells(bits, s)) for s in got.values()) if "TCD " in bits else 0
    links = len(pub_quill.links(bits))
    return f"bits {sum(map(len, bits.values()))} stories {len(got)} cells {cells} links {links} {dict(ctx.skipped)}"


def measure():
    size = 8 * MIB
    # a trailer that points at itself
    data = bytearray(contents_fix(three_chunks()) + bytes(size))
    struct.pack_into("<I", data, 0x1A, 0x1A)
    _probe("trailer points at itself", lambda: _run_directory(bytes(data)), len(data))
    # a trailer at the end whose length runs past the stream and whose directory is one deep nest
    nest = pub_block(9, 0x20, 1)
    for k in range(200_000):
        nest = bytes([k & 0xFF, 0x88]) + struct.pack("<I", len(nest) + 4) + nest
    data = _contents_with(nest, len(nest) + 0x100)
    def deep():
        ctx = Context()
        meter = pub_contents.Meter(ctx)
        toff = struct.unpack_from("<I", data, 0x1A)[0]
        depth, block = 0, list(pub_contents.blocks(data, toff + 4, len(data), meter=meter))[2]
        while block is not None:
            block = next(pub_contents.children(data, block, depth, meter=meter), None)
            depth += 1
        return f"depth {depth} {dict(ctx.skipped)} | " + _run_directory(data)
    _probe("block nesting 200k deep", deep, len(data))
    # variable blocks of length 4 repeated: as directory references, and as sub-blocks of one reference
    unit = pub_block(0, 0x88)
    data = _contents_with(unit * (size // len(unit)), size)
    _probe("empty 0x88 x1.4M (references)", lambda: _run_directory(data), len(data))
    one = pub_block(0, 0x88, pub_block(5, 0x88) * (size // 6))
    data = _contents_with(one, size)
    _probe("empty 0x88 x1.4M (sub-blocks)", lambda: _run_directory(data), len(data))
    data = _contents_with(bytes([0, 0x78]) * (size // 2), size)
    _probe("DUMMY blocks x4M", lambda: _run_directory(data), len(data))
    # a Quill bit list that cycles: 40,000 lists of 20 entries each naming the next, the last naming the first
    text = ("Hello\r" * 10).encode("utf-16-le")
    base = [(b"TEXT", b"TEXT", 0, text), (b"STRS", b"PLC ", 0, _plc_lengths([60])),
            (b"SYID", b"SYID", 0, struct.pack("<III", 8, 1, 1))]
    lists = bytearray()
    first = 0x18 + 8 + 24 * len(base)
    n = 40_000
    entry = struct.pack("<H4sHHH4sII", 0x18, b"JUNK", 0, 1, 0, b"JUNK", 0, 4)
    for k in range(n):
        nxt = first + (k + 1) * (8 + 24 * 20) if k + 1 < n else first
        lists += struct.pack("<HHI", 0, 20, nxt) + entry * 20
    data = _quill_with(base, bytes(lists), first)
    _probe("Quill list cycle 40k x20", lambda: _run_quill(data), len(data))
    # a huge TEXT bit: one 32 Mi-character story past the room, and 4 Mi stories of 8 characters
    chars = 32 * MIB
    big = ("abcdefg\r" * (chars // 8)).encode("utf-16-le")
    data = _quill_with([(b"TEXT", b"TEXT", 0, big), (b"STRS", b"PLC ", 0, _plc_lengths([chars])),
                        (b"SYID", b"SYID", 0, struct.pack("<III", 8, 1, 1))])
    _probe("TEXT 64 MiB, one story", lambda: _run_quill(data), len(data))
    many = chars // 8
    data = _quill_with([(b"TEXT", b"TEXT", 0, big), (b"STRS", b"PLC ", 0, _plc_lengths([8] * many)),
                        (b"SYID", b"SYID", 0, struct.pack("<II", 8, many) + struct.pack(f"<{many}I", *range(many)))])
    _probe("TEXT 64 MiB, 4M stories", lambda: _run_quill(data), len(data))
    # STRS lengths that overrun: 2M lengths of 0xFFFFFFFF over a short text
    m = 2 * MIB
    data = _quill_with([(b"TEXT", b"TEXT", 0, text), (b"STRS", b"PLC ", 0, _plc_lengths([0xFFFFFFFF] * m)),
                        (b"SYID", b"SYID", 0, struct.pack("<II", 8, m) + struct.pack(f"<{m}I", *range(m)))])
    _probe("STRS overrun x2M", lambda: _run_quill(data), len(data))
    # TCD offsets out of order: 2M descending ends over a 1 Mi-character table story
    story = ("x" * (MIB - 1) + "\r").encode("utf-16-le")
    ends = list(range(2 * MIB, 0, -1))
    tcd = struct.pack("<III", len(ends) - 1, 0, 0xFF00) + struct.pack(f"<{len(ends)}I", *ends)
    data = _quill_with([(b"TEXT", b"TEXT", 0, story), (b"STRS", b"PLC ", 0, _plc_lengths([MIB])),
                        (b"SYID", b"SYID", 0, struct.pack("<III", 8, 1, 1)), (b"TCD ", b"PLC ", 0, tcd)])
    _probe("TCD out of order x2M", lambda: _run_quill(data), len(data))
    # many PLC links: 1,000 TOKN bits (QUILL_BITS) of 1,000 links each, and FDPC runs of 65,535 entries
    urls = [f"https://h{k}.example/" + "p" * 40 for k in range(1000)]
    tokn = _tokn(urls)
    data = _quill_with([(b"TOKN", b"PLC ", k, tokn) for k in range(1000)])
    _probe("PLC links 1000 bits x1000", lambda: _run_quill(data), len(data))
    # FDPC runs: 250 bits of 10,000 runs, each bit's one style 2,000 bold blocks long, past BIN_RECORDS in all
    runs, nbits = 10_000, 250
    text_at = 0x20 + 24 * (3 + nbits)
    fdpcs = []
    for k in range(nbits):
        ends = [text_at + 2 * (k * runs + i) for i in range(1, runs + 1)]
        fdpcs.append(struct.pack("<H", runs) + bytes(6) + struct.pack(f"<{runs}I", *ends)
                     + struct.pack(f"<{runs}H", *([8 + 6 * runs] * runs)) + struct.pack("<I", 4 + 2 * 2000)
                     + bytes([0x02, 0x0A]) * 2000)
    chars = runs * nbits
    data = _quill_with([(b"TEXT", b"TEXT", 0, ("a" * chars).encode("utf-16-le")),
                        (b"STRS", b"PLC ", 0, _plc_lengths([chars])),
                        (b"SYID", b"SYID", 0, struct.pack("<III", 8, 1, 1))]
                       + [(b"FDPC", b"FDPC", k, f) for k, f in enumerate(fdpcs)])
    _probe("FDPC 250 bits x10k runs", lambda: _run_quill(data), len(data))
    # pub.py, whole files (built in memory, read with parse_compound): seqnums are directory indexes, so the pages and
    # shapes below stay under PUB_CHUNKS; the stories are 1,000 short ones unless named
    stories = [(k + 1, f"story {k}\r") for k in range(1000)]
    n = 45_000
    blob = pub_file([page(2 + k, [2 + n + k]) for k in range(n)], [shape(2 + n + k, k % 1000 + 1) for k in range(n)],
                    stories)
    _probe("pub: 45k pages, a shape each", lambda: _run_pub(blob), len(blob))
    n = 90_000
    shapes = [shape(3 + k, k % 1000 + 1) for k in range(n)]
    escher = pub_escher(*(pub_sp(3 + k, (k % 7, n - k, k % 7 + 1, n - k + 1)) for k in range(n)))
    blob = _v4(pub_file([page(2, [3 + k for k in range(n)])], shapes, stories), {"Escher/EscherStm": escher})
    _probe("pub: 90k boxed shapes on one page", lambda: _run_pub(blob), len(blob))
    blob = pub_file([page(2, [3 + k for k in range(n)])], [shape(3 + k, 1) for k in range(n)],
                    [(1, "x" * MIB + "\r")])
    _probe("pub: one 1 Mi story x90k shapes", lambda: _run_pub(blob), len(blob))
    n = 45_000
    masters = [page(2 + k, [2 + n + k], master=f"M{k}", applied=2 + (k + 1) % n) for k in range(n)]
    order = [2 + k for k in range(n)] * 2
    blob = pub_file(masters, [shape(2 + n + k, k % 1000 + 1) for k in range(n)], stories, order=order)
    _probe("pub: 45k masters in a cycle", lambda: _run_pub(blob), len(blob))
    tokn = _tokn([f"https://h{k}.example/" + "p" * 40 for k in range(1000)])
    quill = _quill_with([(b"TOKN", b"PLC ", k, tokn) for k in range(400)])
    blob = _v4(pub_file([page(2, [])]), {"Quill/QuillSub/CONTENTS": quill})
    _probe("pub: 400k links", lambda: _run_pub(blob), len(blob))
    _alias_probes()


def _alias_probes():
    # 99,000 directory entries naming one chunk that holds a 1 MiB string block: as shapes, and as pages (the master
    # name 1 MiB)
    quill = quill_fix([(1, "hello")])
    for name, contents in (("pub: 99k shape aliases x1 MiB", _shape_aliases(99_000, MIB)),
                           ("pub: 99k page aliases x1 MiB", _page_aliases(99_000, MIB))):
        def run():
            ctx = Context()
            reader = pub._Reader(contents, quill, b"", ctx)
            doc = reader.run()
            return f"blocks {len(doc.blocks)} meter {reader.meter.used} {dict(ctx.skipped)}"
        _probe(name, run, len(contents))


def _v4(blob, streams):
    """`blob`'s Contents and Quill streams with `streams` over them, in a compound file of 4096-byte sectors (the
    512-byte one the fixture writes holds no more than about 7 MiB)."""
    with Compound(blob) as cf:
        out = {n: cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")}
    return cfb_bytes({**out, **streams}, 4096)[0]


def _run_pub(blob):
    ctx = Context()
    doc = pub.parse_compound(blob, ctx)
    sections = [b for b in doc.blocks if isinstance(b, Section)]
    paras = sum(len(s.blocks) for s in sections)
    links = sum(isinstance(i, Link) for b in doc.blocks if isinstance(b, Para) for i in b.inlines)
    return f"sections {len(sections)} paras {paras} links {links} {dict(ctx.skipped)}"


def _plc_lengths(lengths):
    return struct.pack("<III", len(lengths), 8, 255) + struct.pack(f"<{len(lengths)}I", *lengths)


def _tokn(urls):
    n = len(urls)
    head = struct.pack("<II", n, 12) + bytes(4 * (n + 2))
    head += bytes(0x68 + (n - 2) * 22 - len(head))
    return head + b"".join(struct.pack("<H", len(u)) + u.encode("utf-16-le") for u in urls)


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
