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
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import images, limits, markdown, officeart  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import cfb_route, pub, pub_2k, pub_contents, pub_quill, vsdx, zip_route  # noqa: E402
from reader.formats.pub_contents import Block, ChunkRef  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Break, Context, Image, Link, Para, Section, Table, Text  # noqa: E402
from reader_fix import (blip_bitmap, blip_metafile, blip_png, cfb_bytes, oa_container, oa_fbse,  # noqa: E402
                        oa_record, pub_2k_contents, pub_2k_file, pub_block, pub_cells, pub_contents as contents_fix,
                        pub_escher, pub_file, pub_group, pub_quill as quill_fix, pub_sp, pub_table)

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
    # byte 0x30 -> unsupported "unknown Publisher version (Contents byte 0x30)" (0x22 is Publisher 2000 or 97/98,
    # read by pub_2k); a Contents stream not starting E8 AC is damaged
    for byte, kind, detail in ((0x30, "unsupported", "unknown Publisher version (Contents byte 0x30)"),
                               (0xA1, "unsupported", "unknown Publisher version (Contents byte 0xA1)")):
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
    raw, info = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": b"CHNKINK ",
                           "Escher/EscherStm": pub_escher(), "Escher/EscherDelayStm": bytes(5000)})
    chain, blob = info["chains"]["Escher/EscherDelayStm"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    refused(lambda: read_pub(bytes(blob)), "damaged", "Escher/EscherDelayStm stream")
    raw, info = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": quill_fix([(1, "t")]),
                           "Escher/EscherDelayStm": bytes(5000)})
    chain, blob = info["chains"]["Escher/EscherDelayStm"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    doc, ctx = read_pub(bytes(blob))
    assert outline(doc) == [("Page 1", ["t"])] and not ctx.skipped, (outline(doc), ctx.skipped)


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
    # table shape with no rows, columns or cells gives its story as paragraphs, counted table
    extra = pub_block(0x0E, 0x20, 5) + pub_block(0x02, 0xA0, pub_block(0, 0x68, 0x31))
    blob = pub_file([page(0x21, [0x30, 0x32], extra=extra)],
                    [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3, type=pub_contents.TABLE)],
                    [(1, "kept"), (2, "not listed"), (3, "cell")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["kept", "cell"])] and ctx.skipped == {"table": 1}, (outline(doc), ctx.skipped)


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



# -- Task 19: tables and pictures

PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
JPEG = b"\xff\xd8\xff\xe0" + bytes(28)
# a 1x1 24-bit DIB: BITMAPINFOHEADER, then one row of one red pixel padded to 4 bytes
DIB = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
WMF = struct.pack("<HHHIHIH", 1, 9, 0x300, 20, 0, 3, 0) + struct.pack("<IH", 3, 0) + bytes(16)
TABLE_TEXT = "Head\rA\rB\r"   # cells end at the \r closing each: 4, 6 and the story's end, 9
TABLE_BOUNDS = [0, 4, 6, 9]
MERGED = [(0, 0, 0, 1), (1, 1, 0, 0), (1, 1, 1, 1)]   # a 2x2 table whose first row is one cell


def table_file(cells, rows=2, cols=2, text=TABLE_TEXT, bounds=TABLE_BOUNDS, *, tables=1, csn=0x50, more=(), **kw):
    """One page listing `tables` TABLE shapes (0x30, 0x31, ...: rows x cols, the CELLS chunk `csn` holding `cells`,
    text ids 1, 2, ...), each story `text` cut at `bounds` (a TCD bit per story, none when None); `more` shapes added
    to the page's list."""
    shapes = [shape(0x30 + k, k + 1, type=pub_contents.TABLE, extra=pub_table(rows, cols, csn)) for k in range(tables)]
    shapes.append(shape(0x50, type=pub_contents.CELLS, extra=pub_cells(cells)))
    shapes += list(more)
    stories = [(k + 1, text) for k in range(tables)]
    tcd = {k: list(bounds) for k in range(tables)} if bounds is not None else None
    return pub_file([page(0x20, [0x30 + k for k in range(tables)] + [s["seq"] for s in more])], shapes, stories,
                    tcd=tcd, **kw)


def grid(table):
    """[[(rowspan, colspan, [cell paragraph texts])]] of a Table."""
    return [[(c.rowspan, c.colspan, [text_of(p) for p in c.blocks]) for c in row] for row in table.rows]


def page_blocks(doc):
    return [b for s in doc.blocks if isinstance(s, Section) for b in s.blocks]


def test_table_merged_cells():
    # a 2x2 table whose first row is one cell (colspan 2): a Table, its cells cut from the story by the TCD bit, the
    # bold of a cell kept (the story's bold ranges, not read again)
    doc, ctx = read_pub(table_file(MERGED, bold=[(5, 6)]))
    (table,) = page_blocks(doc)
    assert isinstance(table, Table) and not ctx.skipped, (doc, ctx.skipped)
    assert grid(table) == [[(1, 2, ["Head"])], [(1, 1, ["**A**"]), (1, 1, ["B"])]], grid(table)
    got = md(doc, ctx)
    assert "**A**" in got and "Head" in got and 'colspan="2"' in got, got
    # a first column of one cell (rowspan 2)
    doc, ctx = read_pub(table_file([(0, 1, 0, 0), (0, 0, 1, 1), (1, 1, 1, 1)]))
    assert grid(page_blocks(doc)[0]) == [[(2, 1, ["Head"]), (1, 1, ["A"])], [(1, 1, ["B"])]] and not ctx.skipped
    # cells listed out of grid order: the text goes by the CELLS order, the rows by the grid
    doc, ctx = read_pub(table_file([(1, 1, 1, 1), (0, 0, 0, 1), (1, 1, 0, 0)]))
    assert grid(page_blocks(doc)[0]) == [[(1, 2, ["A"])], [(1, 1, ["B"]), (1, 1, ["Head"])]] and not ctx.skipped
    # a cell of two paragraphs (a \r inside it) and a line break
    doc, ctx = read_pub(table_file(MERGED, text="H1\rH2\rA\va\rB\r", bounds=[0, 5, 9, 12]))
    assert grid(page_blocks(doc)[0]) == [[(1, 2, ["H1", "H2"])], [(1, 1, ["A|a"]), (1, 1, ["B"])]], doc


def test_cell_paras_bold():
    # pub_quill: the story keeps its bold ranges and where it starts; a cell's paragraphs take them, a bold run that
    # crosses a cell boundary is cut there
    bits = pub_quill.bits(quill_fix([(1, "xy"), (2, "abc\rdef\r")], bold=[(0, 1), (3, 8)], tcd={1: [0, 3, 8]}))
    story = pub_quill.stories(bits)[2]
    assert (story.start, story.bold) == (2, ((3, 8),)), (story.start, story.bold)
    spans = list(pub_quill.cell_spans(pub_quill.table_bits(bits)[1], story))
    assert spans == [(0, 3, 4), (4, 7, 8)], spans
    got = pub_quill.cell_paras(story, [(a, b) for a, b, _ in spans])
    assert [[text_of(p) for p in c] for c in got] == [["a**bc**"], ["**de**f"]], got
    assert pub_quill.cells(bits, story) == ["abc", "def"]
    assert pub_quill.table_bits({}) == {} and list(pub_quill.cell_spans(None, story)) == []
    two = [pub_quill.Bit(b"first", 1, "PLC "), pub_quill.Bit(b"second", 1, "PLC ")]
    assert pub_quill.table_bits({"TCD ": two})[1] == b"first"


def test_table_too_big():
    # rows x columns over GRID_SLOTS, or cells over TABLE_NODES (the document's): no table, its story as paragraphs,
    # counted table once each
    blob = table_file(MERGED)
    with patched(limits, GRID_SLOTS=3):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1}, (outline(doc), ctx.skipped)
    with patched(limits, GRID_SLOTS=4):
        doc, ctx = read_pub(blob)
    assert isinstance(page_blocks(doc)[0], Table) and not ctx.skipped
    with patched(limits, TABLE_NODES=2):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1}, (outline(doc), ctx.skipped)
    # the node budget is the document's: two tables of 3 cells under 5 nodes, the second as paragraphs
    with patched(limits, TABLE_NODES=5):
        doc, ctx = read_pub(table_file(MERGED, tables=2))
    kinds = [type(b).__name__ for b in page_blocks(doc)]
    assert kinds == ["Table", "Para", "Para", "Para"] and ctx.skipped == {"table": 1}, (kinds, ctx.skipped)
    # a declared grid of 2^32 x 2^32 slots: refused before anything is laid out (its CELLS chunk never read)
    read = []
    real = pub._Reader._cells
    with patched(pub._Reader, _cells=lambda self, ref: read.append(ref) or real(self, ref)):
        doc, ctx = read_pub(table_file(MERGED, rows=0xFFFFFFFF, cols=0xFFFFFFFF))
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1} and read == []


def test_table_spans_clamped():
    # spans past the grid are cut to it; a cell whose origin another covers joins that cell's text; a span reaching
    # into another cell is cut short; a place no cell covers is an empty cell: the text stays, record counted once
    # for the table
    def run(cells, **kw):
        doc, ctx = read_pub(table_file(cells, **kw))
        return grid(page_blocks(doc)[0]), dict(ctx.skipped)
    assert run([(0, 0, 0, 9), (1, 7, 0, 0), (1, 1, 1, 1)]) == (
        [[(1, 2, ["Head"])], [(1, 1, ["A"]), (1, 1, ["B"])]], {"record": 1})
    assert run([(0, 0, 0, 1), (0, 1, 0, 0), (1, 1, 1, 1)]) == (
        [[(1, 2, ["Head", "A"])], [(1, 1, []), (1, 1, ["B"])]], {"record": 1})
    assert run([(0, 0, 0, 0), (0, 0, 1, 1), (0, 1, 0, 1)]) == (
        [[(1, 1, ["Head", "B"]), (1, 1, ["A"])]], {"record": 1})
    assert run([(0, 0, 0, 1), (1, 1, 0, 1), (0, 0, 1, 1)]) == (
        [[(1, 2, ["Head", "B"])], [(1, 2, ["A"])]], {"record": 1})
    assert run([(0, 0, 0, 0), (1, 1, 1, 1), (0, 1, 1, 1)]) == (
        [[(1, 1, ["Head"]), (2, 1, ["B", "A"])], [(1, 1, [])]], {"record": 1})
    # a start past its end is a span of one
    assert run([(0, 0, 1, 0), (1, 1, 0, 0), (1, 1, 1, 1)]) == (
        [[(1, 1, []), (1, 1, ["Head"])], [(1, 1, ["A"]), (1, 1, ["B"])]], {"record": 1})
    # only a column end past the grid; a row end before its start; a start past the grid: as MERGED, record counted
    for cells in ([(0, 0, 0, 9), (1, 1, 0, 0), (1, 1, 1, 1)], [(0, 0, 0, 1), (1, 0, 0, 0), (1, 1, 1, 1)],
                  [(0, 0, 0, 1), (1, 1, 0, 0), (1, 1, 5, 5)]):
        assert run(cells) == ([[(1, 2, ["Head"])], [(1, 1, ["A"]), (1, 1, ["B"])]], {"record": 1}), cells
    # a row's span reaching a place a rowspan above covers is cut short there
    assert run([(0, 0, 0, 0), (0, 1, 1, 1), (1, 1, 0, 1)]) == (
        [[(1, 1, ["Head"]), (2, 1, ["A"])], [(1, 1, ["B"])]], {"record": 1})
    # places no cell covers, and nothing else wrong: empty cells, record counted
    assert run([(0, 0, 0, 0), (1, 1, 1, 1)], text="Head\rA\r", bounds=[0, 4, 6]) == (
        [[(1, 1, ["Head"]), (1, 1, [])], [(1, 1, []), (1, 1, ["A"])]], {"record": 1})


def test_table_tcd_past_story():
    # a TCD cell end past the story (pub_quill.cell_spans) is cut to the story's end: the table is laid out, its text
    # kept, nothing counted (before, an IndexError dropped the shape, counted shape)
    def run(bounds):
        doc, ctx = read_pub(table_file(MERGED, bounds=bounds))
        return grid(page_blocks(doc)[0]), dict(ctx.skipped)
    assert run([0, 4, 6, 50]) == ([[(1, 2, ["Head"])], [(1, 1, ["A"]), (1, 1, ["B"])]], {}), run([0, 4, 6, 50])
    assert run([0, 4, 60, 9]) == ([[(1, 2, ["Head"])], [(1, 1, ["A", "B"]), (1, 1, [])]], {}), run([0, 4, 60, 9])
    assert run([0, 40, 6, 9]) == ([[(1, 2, ["Head", "A", "B"])], [(1, 1, []), (1, 1, [])]], {}), run([0, 40, 6, 9])


def test_table_layout_ticks():
    # each step of the layout ticks the meter once: 3 cells, 2 places checked for the span cut short, 5 places
    # marked, and 6 steps of the row scan (one of them a gap's place)
    with Compound(table_file([(0, 1, 2, 2), (1, 1, 0, 2), (0, 0, 1, 1)], 2, 3)) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]

    def run():
        ctx = Context()
        reader = pub._Reader(*streams, b"", ctx)
        return reader.run(), reader.meter.used, ctx
    doc, full, ctx = run()
    assert grid(page_blocks(doc)[0]) == [[(1, 1, []), (1, 1, ["B"]), (2, 1, ["Head"])], [(1, 2, ["A"])]]
    assert ctx.skipped == {"record": 1}, ctx.skipped
    with patched(pub._Reader, _layout=lambda self, *a: None):
        _, base, _ = run()
    assert full - base == 16, full - base


def test_cells_hostile_items():
    # a CELLS list item of an id other than 0 is not a cell; a field that is not a number is 0; a CELLS chunk listing
    # more than TABLE_NODES cells is kept as too many, its walk stopped there
    def item(cell, extra=b""):
        return pub_block(0, 0x88, b"".join(pub_block(k + 1, 0x20, v) for k, v in enumerate(cell) if v) + extra)
    items = item(MERGED[0]) + pub_block(1, 0x88, b"") + item(MERGED[1], pub_block(3, 0x88, b"")) + item(MERGED[2])
    cells = pub_block(0x01, 0x18, 3) + pub_block(0x02, 0xA0, items)
    blob = pub_file([page(0x20, [0x30])], [shape(0x30, 1, type=pub_contents.TABLE, extra=pub_table(2, 2, 0x50)),
                                           shape(0x50, type=pub_contents.CELLS, extra=cells)],
                    [(1, TABLE_TEXT)], tcd={0: TABLE_BOUNDS})
    doc, ctx = read_pub(blob)
    assert grid(page_blocks(doc)[0]) == [[(1, 2, ["Head"])], [(1, 1, ["A"]), (1, 1, ["B"])]] and not ctx.skipped
    with Compound(table_file(MERGED)) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    with patched(limits, TABLE_NODES=2):
        reader = pub._Reader(*streams, b"", Context())
        assert reader._cells(reader.refs[0x50]) is pub._TOO_MANY
    assert type(pub._TOO_MANY) is object  # not a falsy value a caller could mistake for no cells


def test_table_text_and_cells_disagree():
    # more cell texts than cells: the rest follow the table as paragraphs, record counted; fewer: the last cells empty,
    # nothing counted (no cell starts in the second column, so the grid kept is one column wide)
    doc, ctx = read_pub(table_file([(0, 0, 0, 1), (1, 1, 0, 1)]))
    got = page_blocks(doc)
    assert grid(got[0]) == [[(1, 1, ["Head"])], [(1, 1, ["A"])]] and [text_of(p) for p in got[1:]] == ["B"]
    assert ctx.skipped == {"record": 1}, ctx.skipped
    doc, ctx = read_pub(table_file([(0, 0, 0, 0), (0, 0, 1, 1), (1, 1, 0, 0), (1, 1, 1, 1)]))
    assert grid(page_blocks(doc)[0]) == [[(1, 1, ["Head"]), (1, 1, ["A"])], [(1, 1, ["B"]), (1, 1, [])]]
    assert not ctx.skipped
    # text past the TCD bit's last cell is kept after the table
    doc, ctx = read_pub(table_file(MERGED, text="Head\rA\rB\rC\r"))
    got = page_blocks(doc)
    assert grid(got[0])[1][1] == (1, 1, ["B"]) and [text_of(p) for p in got[1:]] == ["C"]
    assert ctx.skipped == {"record": 1}, ctx.skipped


def test_table_damaged():
    # a table with no rows, no columns, a CELLS seqnum naming nothing or another chunk (a shape, the page), no cells,
    # rows that are not a number, or no TCD bit or a short one: its story as paragraphs, counted table
    for kw in (dict(rows=None), dict(cols=None), dict(rows=0), dict(csn=0x51), dict(bounds=None),
               dict(csn=0x40, more=[shape(0x40, 9)]), dict(csn=0x20)):
        doc, ctx = read_pub(table_file(MERGED, **kw))
        assert outline(doc)[0] == ("Page 1", ["Head", "A", "B"]) and ctx.skipped == {"table": 1}, (kw, ctx.skipped)
    doc, ctx = read_pub(table_file([]))
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1}, ctx.skipped
    rows = pub_block(0x66, 0xC0, b"ab") + pub_table(None, 2, 0x50)
    blob = pub_file([page(0x20, [0x30])], [shape(0x30, 1, type=pub_contents.TABLE, extra=rows),
                                           shape(0x50, type=pub_contents.CELLS, extra=pub_cells(MERGED))],
                    [(1, TABLE_TEXT)], tcd={0: TABLE_BOUNDS})
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1}, ctx.skipped
    with patched(pub_quill, table_bits=lambda bits: {0: pub_quill.Bit(bytes(8), 0, "PLC ")}):
        doc, ctx = read_pub(table_file(MERGED))
    assert outline(doc) == [("Page 1", ["Head", "A", "B"])] and ctx.skipped == {"table": 1}, ctx.skipped
    # a table with no story gives nothing, uncounted
    blob = pub_file([page(0x20, [0x30])], [shape(0x30, 9, type=pub_contents.TABLE, extra=pub_table(2, 2, 0x50)),
                                           shape(0x50, type=pub_contents.CELLS, extra=pub_cells(MERGED))], [(1, "x")])
    doc, ctx = read_pub(blob)
    assert doc.blocks == [] and not ctx.skipped


def test_cells_read_once():
    # two tables naming one CELLS chunk: that chunk is walked once, and both are tables
    blob = table_file(MERGED, tables=2)
    walked = []
    real = pub._Reader._chunk

    def chunk(self, ref):
        walked.append(ref.seqnum)
        return real(self, ref)
    with patched(pub._Reader, _chunk=chunk):
        doc, ctx = read_pub(blob)
    assert [type(b).__name__ for b in page_blocks(doc)] == ["Table", "Table"] and not ctx.skipped
    assert walked.count(0x50) == 1, walked


def test_table_meter():
    # the layout ticks the document meter (each grid place marked and scanned): at any cap the read ends, record
    # counted at most once, table never; a table the meter cuts gives its story as paragraphs
    blob = table_file(MERGED)
    kinds = set()
    for cap in range(0, 400):
        with patched(limits, BIN_RECORDS=cap):
            try:
                doc, ctx = read_pub(blob)
            except ReadError as e:
                assert e.kind == "damaged", e
                continue
        assert set(ctx.skipped) <= {"record"} and ctx.skipped["record"] <= 1, (cap, ctx.skipped)
        got = page_blocks(doc)
        kinds.add(tuple(type(b).__name__ for b in got))
    assert ("Table",) in kinds and ("Para", "Para", "Para") in kinds, kinds
    # the places are charged: a span of 1,000 columns costs the meter no more than one of 2 (the grid kept is the
    # places where cells start), and a table whose places pass the meter is given as paragraphs
    def used(cells, cols):
        with Compound(table_file(cells, cols=cols)) as cf:
            streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
        reader = pub._Reader(*streams, b"", Context())
        reader.run()
        return reader.meter.used
    assert used([(0, 0, 0, 999), (1, 1, 0, 0), (1, 1, 1, 999)], 1000) == used(MERGED, 2)
    cells = [(0, 0, k, k) for k in range(60)] + [(1, 1, 0, 59)]
    base = used(cells[:1], 60)
    with patched(limits, BIN_RECORDS=base + 200):
        doc, ctx = read_pub(table_file(cells, cols=60, text="x\r" * 61, bounds=list(range(0, 123, 2))))
    assert isinstance(page_blocks(doc)[0], Para) and ctx.skipped == {"record": 1}, ctx.skipped


def picture_file(blips, shapes, *, delay=None, texts=(), boxes=None):
    """One page listing `shapes` ((seq, pib or None) pairs: SHAPE chunks, the story `texts` gives the seq when it does)
    placed in the Escher stream (ClientData naming seq, a box when `boxes` gives one), with the BStore of `blips` (FBSE
    records) and the delay stream `delay`."""
    boxes = boxes or {}
    escher = pub_escher(*(pub_sp(seq, boxes.get(seq), pib=pib) for seq, pib in shapes), store=blips)
    text = dict(texts)
    stories = [(tid, f"text {tid}") for tid in text.values()]
    streams = {"Escher/EscherDelayStm": delay} if delay is not None else None
    return pub_file([page(0x20, [seq for seq, _ in shapes])], [shape(seq, text.get(seq)) for seq, _ in shapes],
                    stories, escher=escher, streams=streams)


def images_of(doc):
    return [b.data for b in page_blocks(doc) if isinstance(b, Image)]


def test_pictures():
    # a PNG BLIP -> an Image; a DIB -> a PNG Image (dib_to_bmp, then bmp_to_png); a WMF (deflated) -> an asset, not
    # viewable; a JPEG in the delay stream (its FBSE's foDelay) -> an Image
    delay = bytes(8) + blip_bitmap(0xF01D, 0x46A, JPEG)
    blips = [oa_fbse(blip_png(PNG)), oa_fbse(blip_bitmap(0xF01F, 0x7A8, DIB)), oa_fbse(blip_metafile(0xF01B, 0x216, WMF)),
             oa_fbse(delay=8)]
    doc, ctx = read_pub(picture_file(blips, [(0x30, 1), (0x31, 2), (0x32, 3), (0x33, 4)], delay=delay))
    got = images_of(doc)
    assert not ctx.skipped and len(got) == 4, (got, ctx.skipped)
    assert got[0] == PNG and got[1].startswith(b"\x89PNG\r\n\x1a\n") and got[2] == WMF and got[3] == JPEG
    with tempfile.TemporaryDirectory() as tmp:
        out = markdown.render(doc, Path(tmp) / "assets", False, ctx.skipped)
        assert (Path(tmp) / "assets" / "img-002.png").read_bytes() == got[1]
    assert out.count("![](assets/img-") == 3 and "assets/img-003.wmf" in out and "Not viewable" in out, out
    # a shape with a picture and a story: the picture first
    got = page_blocks(read_pub(picture_file([oa_fbse(blip_png(PNG))], [(0x30, 1)], texts=[(0x30, 1)]))[0])
    assert isinstance(got[0], Image) and text_of(got[1]) == "text 1", got


def test_picture_joined_by_client_data():
    # two shapes: the picture's ClientData id joins it to its Contents shape, so it takes that shape's place in the
    # page order (top, then left); a ClientData naming no listed shape places nothing; an unanchored picture is placed
    blips = [oa_fbse(blip_png(PNG)), oa_fbse(blip_bitmap(0xF01D, 0x46A, JPEG))]
    blob = picture_file(blips, [(0x30, None), (0x31, 1)], texts=[(0x30, 1)],
                        boxes={0x30: (0, 50, 10, 60), 0x31: (0, 10, 10, 20)})
    doc, ctx = read_pub(blob)
    got = page_blocks(doc)
    assert isinstance(got[0], Image) and got[0].data == PNG and text_of(got[1]) == "text 1" and not ctx.skipped
    blob = picture_file(blips, [(0x30, None), (0x31, 1)], texts=[(0x30, 1)],
                        boxes={0x30: (0, 10, 10, 20), 0x31: (0, 50, 10, 60)})
    got = page_blocks(read_pub(blob)[0])
    assert text_of(got[0]) == "text 1" and got[1].data == PNG
    escher = pub_escher(pub_sp(0x30, pib=1), pub_sp(0x77, (0, 0, 1, 1), pib=2), store=blips)
    doc, ctx = read_pub(pub_file([page(0x20, [0x30])], [shape(0x30), shape(0x77)], [], escher=escher))
    assert images_of(doc) == [PNG] and not ctx.skipped


def test_client_data_index():
    # pub's one Escher walker records each ClientData seqnum's SpContainer offset, for shapes with an anchor and
    # without one; officeart.shape_pib reads the pib there
    escher = pub_escher(pub_sp(0x30, (0, 1, 2, 3), pib=4), pub_sp(0x31, pib=5), pub_sp(0x32),
                        pub_group(pub_sp(0x40, (0, 0, 9, 9), group=(0, 0, 9, 9)), pub_sp(0x41, child=(0, 0, 1, 1))))
    walk = pub._Boxes(escher, pub_contents.Meter(), Context())
    walk.walk()
    assert sorted(walk.sp) == [0x30, 0x31, 0x32, 0x40, 0x41], walk.sp
    for seq, off in walk.sp.items():
        assert officeart.header(escher, off)[2] == officeart.SP_CONTAINER
        assert pub._values(escher, *_client_body(escher, off), pub_contents.Meter())[pub.FIELD_SHAPE_ID] == seq
    assert [officeart.shape_pib(escher, walk.sp[s]) for s in (0x30, 0x31, 0x32)] == [4, 5, None]
    assert sorted(walk.out) == [0x30, 0x40, 0x41], walk.out


def _client_body(data, sp_off):
    for _, _, typ, body, size in officeart.records(data, sp_off + 8, sp_off + 8 + officeart.header(data, sp_off)[3]):
        if typ == pub.CLIENT_DATA:
            return body, size
    raise AssertionError("no ClientData")


class _calls:
    """Count the calls of officeart.blip_image and images.bmp_to_png while patched in."""

    def __init__(self):
        self.blips, self.converts = [], []
        self.real = officeart.blip_image, images.bmp_to_png

    def blip_image(self, *a):
        self.blips.append(a[1])
        return self.real[0](*a)

    def bmp_to_png(self, data):
        self.converts.append(len(data))
        return self.real[1](data)

    def read(self, blob, **caps):
        self.blips.clear(), self.converts.clear()
        with patched(officeart, blip_image=self.blip_image), patched(images, bmp_to_png=self.bmp_to_png), \
                patched(limits, **caps):
            return read_pub(blob)


def test_picture_budget():
    # IMAGE_BYTES (the document's, copies counted): a placement past it is counted image; once spent no BLIP is
    # decoded or converted; a pib many shapes share is decoded once, and FBSEs naming one delay BLIP too
    blips = [oa_fbse(blip_bitmap(0xF01F, 0x7A8, DIB)), oa_fbse(delay=0), oa_fbse(delay=0)]
    shapes = [(0x30 + k, 1) for k in range(5)] + [(0x40, 2), (0x41, 3), (0x42, 2)]
    blob = picture_file(blips, shapes, delay=blip_png(PNG))
    calls = _calls()
    doc, ctx = calls.read(blob)
    got = images_of(doc)
    assert len(got) == 8 and got[-1] == PNG and not ctx.skipped, (len(got), ctx.skipped)
    assert len(calls.blips) == 2 and len(calls.converts) == 1, (calls.blips, calls.converts)
    seen, real = [], (officeart.bstore, officeart.fbse_delay)
    with patched(officeart, bstore=lambda d: seen.append("bstore") or real[0](d),
                 fbse_delay=lambda d, o: seen.append(o) or real[1](d, o)):
        read_pub(blob)
    assert seen.count("bstore") == 1 and len(seen) == 4, seen
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    with patched(limits, IMAGE_BYTES=12345):
        assert pub._Reader(*streams, b"", Context()).budget.total == 12345
    png = len(got[0])
    doc, ctx = calls.read(blob, IMAGE_BYTES=2 * png)
    assert len(images_of(doc)) == 2 and ctx.skipped == {"image": 6}, ctx.skipped
    assert len(calls.blips) == 1 and len(calls.converts) == 1, (calls.blips, calls.converts)
    doc, ctx = calls.read(blob, IMAGE_BYTES=0)
    assert images_of(doc) == [] and ctx.skipped == {"image": 8} and calls.blips == calls.converts == []
    # a BMP whose bytes alone pass what is left of the budget: not converted
    doc, ctx = calls.read(picture_file([oa_fbse(blip_bitmap(0xF01F, 0x7A8, DIB + bytes(400)))], [(0x30, 1)]),
                          IMAGE_BYTES=png)
    assert images_of(doc) == [] and ctx.skipped == {"image": 1} and calls.converts == [], ctx.skipped


def test_picture_delay_nested():
    # FBSEs whose foDelay offsets name nested records sharing one body: each delay record is charged 8 + its length
    # against the delay stream before it is read, so only the first that fits is decoded; the others are counted
    # image, unread. An offset two FBSEs share is charged once (test_picture_budget)
    inner = blip_png(PNG)
    middle = oa_record(0xF01E, bytes(range(16)) + b"\xff" + inner, inst=0x6E0)
    outer = oa_record(0xF01E, bytes(range(16)) + b"\xff" + middle, inst=0x6E0)
    at = [len(outer) - len(inner), len(outer) - len(middle), 0]
    calls = _calls()
    doc, ctx = calls.read(picture_file([oa_fbse(delay=a) for a in at], [(0x30, 1), (0x31, 2), (0x32, 3)], delay=outer))
    assert images_of(doc) == [PNG] and ctx.skipped == {"image": 2}, (images_of(doc), ctx.skipped)
    assert calls.blips == [at[0]], calls.blips
    # the outer first: it fits (and is no image), the two inside it are refused unread
    doc, ctx = calls.read(picture_file([oa_fbse(delay=a) for a in at[::-1]], [(0x30, 1), (0x31, 2), (0x32, 3)],
                                       delay=outer))
    assert images_of(doc) == [] and ctx.skipped == {"image": 3} and calls.blips == [0], (ctx.skipped, calls.blips)
    # a record running past the delay stream (though shorter than it) is refused unread
    doc, ctx = calls.read(picture_file([oa_fbse(delay=8)], [(0x30, 1)], delay=bytes(8) + inner[:-1]))
    assert ctx.skipped == {"image": 1} and calls.blips == [], (ctx.skipped, calls.blips)


def test_picture_bmp_kept():
    # a DIB that bmp_to_png refuses (16 bits a pixel) is kept as its BMP: an asset, not viewable, not lost
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 16, 0, 4, 0, 0, 0, 0) + b"\x00\x7c\x00\x00"
    doc, ctx = read_pub(picture_file([oa_fbse(blip_bitmap(0xF01F, 0x7A8, dib))], [(0x30, 1)]))
    (got,) = images_of(doc)
    assert got.startswith(b"BM") and got.endswith(dib) and not ctx.skipped, (got, ctx.skipped)
    assert "Not viewable" in md(doc, ctx), md(doc, ctx)


def test_picture_hostile():
    # a pib past the BStore, an empty slot (cRef 0), a DIB whose header size is bogus, a BLIP past the delay stream,
    # and a deflate bomb: each placement counted image, the page's text kept; pib 0 is no picture; no delay stream
    bad_dib = struct.pack("<I", 0xFFFFFF00) + DIB[4:]
    bomb = blip_metafile(0xF01B, 0x216, b"\x01\x00\x09\x00" + bytes(8 * MIB))
    blips = [oa_fbse(blip_bitmap(0xF01F, 0x7A8, bad_dib)), oa_fbse(blip_png(PNG), cref=0), oa_fbse(delay=10**6),
             oa_fbse(bomb)]
    shapes = [(0x30, 9), (0x31, 1), (0x32, 2), (0x33, 3), (0x34, 4), (0x35, 0)]
    doc, ctx = read_pub(picture_file(blips, shapes, delay=bytes(16), texts=[(0x35, 1)]))
    assert images_of(doc) == [] and ctx.skipped == {"image": 5}, ctx.skipped
    assert [text_of(b) for b in page_blocks(doc)] == ["text 1"]
    doc, ctx = read_pub(picture_file([oa_fbse(delay=0)], [(0x30, 1)]))
    assert images_of(doc) == [] and ctx.skipped == {"image": 1}, ctx.skipped
    # a picture that fails in the reading is counted image; its shape's text stays

    def boom(self, pib):
        raise ValueError("boom")
    with patched(pub._Reader, _blip=boom):
        doc, ctx = read_pub(picture_file([oa_fbse(blip_png(PNG))], [(0x30, 1)], texts=[(0x30, 1)]))
    assert [text_of(b) for b in page_blocks(doc)] == ["text 1"] and ctx.skipped == {"image": 1}, ctx.skipped


def test_overlong_chunk_counted_once():
    # a chunk whose length runs past the next chunk, cut there, leaving a variable block past the cut: record once
    # (the clamp), not again for the cut walk
    doc_ = pub_block(0x02, 0xA0, pub_block(0, 0x70, 2))
    pg = pub_block(0x02, 0xA0, pub_block(0, 0x70, 3))
    sh = pub_block(0x27, 0x20, 1) + bytes([0x10, 0xC0]) + struct.pack("<I", 200) + b"x" * 10
    S, P, D = pub_contents.SHAPE, pub_contents.PAGE, pub_contents.DOCUMENT
    contents = bytearray(_aliased([doc_, pg, sh, pub_block(0x27, 0x20, 2)], [(D, 0), (P, 1), (S, 2), (S, 3)]))
    at = pub_contents.directory(bytes(contents), Context())[2].offset
    struct.pack_into("<I", contents, at, 0x7FFFFFF0)
    ctx = Context()
    doc = pub._Reader(bytes(contents), quill_fix([(1, "kept"), (2, "other")]), b"", ctx).run()
    assert outline(doc) == [("Page 1", ["kept"])] and ctx.skipped == {"record": 1}, (outline(doc), ctx.skipped)


def test_read_lists_once():
    # a page chunk's 0x02 list is held once in the offset cache (its 0x70 entries), not again as a page list
    blob = pub_file([page(0x20, [0x30, 0x31, 0x32])], [shape(0x30, 1), shape(0x31, 2), shape(0x32, 3)],
                    [(1, "a"), (2, "b"), (3, "c")])
    with Compound(blob) as cf:
        streams = [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]
    reader = pub._Reader(*streams, b"", Context())
    doc = reader.run()
    assert outline(doc) == [("Page 1", ["a", "b", "c"])]
    entry = reader.chunks[reader.refs[0x20].offset]
    held = [x for part in entry if isinstance(part, list) for x in part]
    assert sorted(held) == [0x30, 0x31, 0x32], entry


# -- pub_2k.py: Publisher 2000 (Quill text) and 97/98 (8-bit text)

PG = 0x10A          # a normal page seqnum in both versions


def text2k(seq, tid, parent=PG, box=None):
    """A Publisher 2000 text box (type 0x08, its text id at +0x58)."""
    return dict(seq=seq, type=0x08, parent=parent, text=tid, box=box)


def text97(seq, tid, parent=PG, box=None):
    """A Publisher 97 text box (type 0x00, its text id at +0x46)."""
    return dict(seq=seq, type=0x00, parent=parent, text=tid, at=0x46, box=box)


def table2k(seq, tid, parent=PG, box=None):
    """A Publisher 2000 table (type 0x0A, its text id at +0x66, so a chunk of 0x70 bytes)."""
    return dict(seq=seq, type=0x0A, parent=parent, text=tid, at=0x66, size=0x70, box=box)


def picture2k(seq, data, parent=PG, box=None, data_seq=None):
    """A picture shape (0x02) and its image data chunk (0x21) naming it as parent."""
    return [dict(seq=seq, type=0x02, parent=parent, box=box),
            dict(seq=data_seq or seq + 0x40, type=0x21, parent=seq, data=data)]


def file97(text, shapes=None, pages=(PG,), **kw):
    """A Publisher 97 file whose text boxes 0, 1, ... are shapes 0x30, 0x31, ... on page PG (as many as `text` has
    0x0C-split boxes, unless `shapes` is given)."""
    if shapes is None:
        shapes = [text97(0x30 + k, k) for k in range(text.count(b"\x0c") + 1)]
    return pub_2k_file(list(pages), shapes, quill=False, text97=text, **kw)


def contents_of(blob):
    with Compound(blob) as cf:
        return cf.read("Contents")


def test_pub2000_text():
    # a 0x22 Contents with a Quill story at text id +0x58 -> the text in its page; bold from FDPC as in 2002; the links
    # line; pages in seqnum order, a page without text left out; a non-text shape's +0x58 names no story
    blob = pub_2k_file([0x127, PG, 0x12C], [text2k(0x125, 1), text2k(0x12A, 2, parent=0x127),
                                            dict(seq=0x12E, type=0x05, parent=0x127, text=3)],
                       [(1, "Hello\rWorld"), (2, "Second"), (3, "Not a text box")], bold=[(0, 5)],
                       links=["https://a.example/"])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["**Hello**", "World"]), ("Page 2", ["Second"]),
                            (None, ["Links: https://a.example/"])] and not ctx.skipped, (outline(doc), ctx.skipped)
    # top, then left, by the chunk's own box at +6 (xs, ys, xe, ye; either corner order)
    shapes = [text2k(0x30, 1, box=(0, 100, 10, 110)), text2k(0x31, 2, box=(0, 60, 10, 50)),
              text2k(0x33, 4, box=(-5, 110, -1, 100))]
    doc, _ = read_pub(pub_2k_file([PG], shapes, [(1, "Low"), (2, "High"), (4, "Left")]))
    assert outline(doc) == [("Page 1", ["High", "Left", "Low"])], outline(doc)


def test_pub2000_wmf_and_master():
    # a 0x21 picture -> an asset, not viewable; a 0x109 master -> "## Master page: Background", after the pages
    blob = pub_2k_file([PG, 0x109], picture2k(0x30, WMF, box=(0, 0, 9, 9)) +
                       [text2k(0x31, 1, box=(0, 20, 9, 29)), text2k(0x32, 2, parent=0x109)],
                       [(1, "Body"), (2, "Footer")])
    doc, ctx = read_pub(blob)
    got = page_blocks(doc)
    assert isinstance(got[0], Image) and got[0].data == WMF and text_of(got[1]) == "Body", got
    master = doc.blocks[1]
    assert (master.kind, master.title, [text_of(p) for p in master.blocks]) == ("master", "Background", ["Footer"])
    assert len(doc.blocks) == 2 and not ctx.skipped, (doc.blocks, ctx.skipped)
    text = md(doc, ctx)
    assert "## Master page: Background" in text and text.index("## Page 1") < text.index("## Master page"), text
    assert "Not viewable here: assets/img-001.wmf" in text and "![" not in text, text
    # a picture shape with no data chunk gives nothing, uncounted; a data chunk naming no picture shape is not placed
    blob = pub_2k_file([PG], [dict(seq=0x30, type=0x02, parent=PG), dict(seq=0x31, type=0x21, parent=0x55, data=WMF),
                              text2k(0x32, 1)], [(1, "t")])
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["t"])] and not ctx.skipped, (outline(doc), ctx.skipped)


def test_pub2000_scratch():
    # a 0x116 DUMMY shape -> hidden; every 2000 DUMMY seqnum is the scratch area, a 2002 one (0x110) a normal page
    dummies = (0x108, 0x10B, 0x10D, 0x116, 0x119)
    shapes = [text2k(0x30, 1), text2k(0x37, 8, parent=0x110)]
    shapes += [text2k(0x31 + k, 2 + k, parent=p) for k, p in enumerate(dummies)]
    stories = [(1, "Body"), (2, "s108"), (3, "s10B"), (4, "s10D"), (5, "Secret"), (6, "s119"), (8, "Next")]
    doc, ctx = read_pub(pub_2k_file([PG, 0x110, *dummies], shapes, stories))
    assert outline(doc) == [("Page 1", ["Body"]), ("Page 2", ["Next"]),
                            ("Page: Scratch area (hidden)", ["s108", "s10B", "s10D", "Secret", "s119"])], outline(doc)
    left, shown = md(doc, ctx), md(doc, ctx, hidden=True)
    assert "Secret" not in left and "Scratch area" not in left and "Secret" in shown, (left, shown)


def test_pub97_text():
    # b"Hello\r\nWorld\x0bnext\x0cBox two" -> paragraphs, a break, two text boxes (text ids 0 and 1 at +0x46)
    doc, ctx = read_pub(file97(b"Hello\r\nWorld\x0bnext\x0cBox two"))
    assert outline(doc) == [("Page 1", ["Hello", "World|next", "Box two"])] and not ctx.skipped, outline(doc)
    assert not doc.warnings
    # a lone CR is dropped (libmspub), a lone LF breaks the line, other controls go; a box that ends the text with
    # 0x0C has no box after it; text boxes are ordered by their box; a 2000 text box (0x08) holds no 97 text
    shapes = [text97(0x30, 0, box=(0, 50, 9, 59)), text97(0x31, 1, box=(0, 10, 9, 19)), text2k(0x32, 0)]
    doc, ctx = read_pub(file97(b"a\rb\nc\x01d\x0cTop\x0c", shapes))
    assert outline(doc) == [("Page 1", ["Top", "ab|cd"])] and not ctx.skipped, outline(doc)


def test_pub97_pictures_counted():
    # one picture -> skipped["image"] == 1; a picture shape with no data chunk, or a data chunk naming a text box,
    # counts nothing
    shapes = [text97(0x30, 0)] + picture2k(0x31, WMF) + [dict(seq=0x33, type=0x02, parent=PG),
                                                         dict(seq=0x34, type=0x21, parent=0x30, data=WMF)]
    doc, ctx = read_pub(file97(b"Text", shapes))
    assert outline(doc) == [("Page 1", ["Text"])] and ctx.skipped == {"image": 1}, (outline(doc), ctx.skipped)


def test_pub2000_table_story():
    # a 2000 table (0x0A) names its story by the u16 at +0x66 (seen in files; libmspub reads no 2000 table): the
    # story's paragraphs in reading order, its structure not read, counted table once; a second chunk naming the
    # story gives nothing more; a table naming no story (id 0 here) counts nothing
    shapes = [text2k(0x30, 1, box=(0, 0, 9, 9)), table2k(0x31, 2, box=(0, 20, 9, 29)), table2k(0x32, 2),
              table2k(0x33, 0)]
    doc, ctx = read_pub(pub_2k_file([PG], shapes, [(1, "Body"), (2, "Cell one\rCell two")]))
    assert outline(doc) == [("Page 1", ["Body", "Cell one", "Cell two"])] and ctx.skipped == {"table": 1}, (
        outline(doc), ctx.skipped)
    # the id's place goes by type: a table's +0x58 and a text box's +0x66 name nothing
    shapes = [dict(seq=0x30, type=0x0A, parent=PG, text=1, size=0x70),
              dict(seq=0x31, type=0x08, parent=PG, text=2, at=0x66, size=0x70)]
    doc, ctx = read_pub(pub_2k_file([PG], shapes, [(1, "a"), (2, "b")]))
    assert outline(doc) == [] and not ctx.skipped, (outline(doc), ctx.skipped)
    # a table past the room: counted text, not table
    with patched(limits, MAX_OUTPUT=10):
        doc, ctx = read_pub(pub_2k_file([PG], [table2k(0x31, 2)], [(2, "Cell one\rCell two")]))
    assert outline(doc) == [] and ctx.skipped == {"text": 1}, (outline(doc), ctx.skipped)
    # Publisher 97 has no such table: a 0x0A chunk names no 97 text box
    doc, ctx = read_pub(file97(b"one", [table2k(0x30, 0), dict(seq=0x31, type=0x0A, parent=PG, text=0, at=0x46)]))
    assert outline(doc) == [] and not ctx.skipped, (outline(doc), ctx.skipped)


def test_pub2000_errors_not_masked():
    # parse_2000 takes the Quill stream's bytes; a reader bug (a KeyError past the stream reads) is not reported as a
    # damaged Quill stream
    blob = pub_2k_file([PG], [text2k(0x30, 1)], [(1, "t")])
    with Compound(blob) as cf:
        contents, quill = cf.read("Contents"), cf.read(pub.QUILL)
    assert outline(pub_2k.parse_2000(quill, contents, Context())) == [("Page 1", ["t"])]

    def bug(self, doc):
        raise KeyError("bug")
    with patched(pub_2k._Reader2000, _page_list=bug):
        try:
            read_pub(blob)
        except ReadError as e:
            raise AssertionError(f"masked as {e}") from None
        except KeyError:
            pass
        else:
            raise AssertionError("no error")


def test_pub2k_routes():
    # 0x22 with a Quill stream: Publisher 2000; without: 97/98; a VBA storage is named once in both
    blob = pub_2k_file([PG], [text2k(0x30, 1)], [(1, "quill")], streams={"VBA/dir": b"\x01" * 16})
    doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["quill"])] and ctx.skipped == {"vba": 1}, (outline(doc), ctx.skipped)
    doc, ctx = read_pub(file97(b"eight", streams={"VBA/dir": b"\x01" * 16}))
    assert outline(doc) == [("Page 1", ["eight"])] and ctx.skipped == {"vba": 1}, (outline(doc), ctx.skipped)
    # no DOCUMENT chunk: damaged, as 2002+
    contents = contents_fix([(PG, 0x14, None, bytes(8))], version=0x22)
    refused(lambda: read_pub(cfb_bytes({"Contents": contents})[0]), "damaged", "no document chunk")
    # a Quill stream whose sector chain loops: damaged "Quill/QuillSub/CONTENTS stream"
    contents = contents_of(pub_2k_file([PG], [text2k(0x30, 1)], [(1, "t")]))
    raw, info = cfb_bytes({"Contents": contents, "Quill/QuillSub/CONTENTS": b"CHNKINK " + bytes(5000)})
    chain, blob = info["chains"]["Quill/QuillSub/CONTENTS"], bytearray(raw)
    struct.pack_into("<I", blob, (info["fat"][chain[-1] // 128] + 1) * 512 + chain[-1] % 128 * 4, chain[0])
    refused(lambda: read_pub(bytes(blob)), "damaged", "Quill/QuillSub/CONTENTS stream")


def test_pub2k_groups():
    # a group (0x0F) stands for its members, the chunks naming it as parent, groups inside included; a group cycle
    # and a shape under no page are read once or not at all; members past MAX_DEPTH counted depth once
    shapes = [dict(seq=0x40, type=0x0F, parent=PG), text2k(0x41, 1, parent=0x40),
              dict(seq=0x42, type=0x0F, parent=0x40), text2k(0x43, 2, parent=0x42),
              dict(seq=0x44, type=0x0F, parent=0x45), dict(seq=0x45, type=0x0F, parent=0x44), text2k(0x46, 3, parent=0x45),
              text2k(0x47, 4, parent=0x99),
              # seqnum 0x48 listed twice (a group on the page and a member of its own member 0x49): read once
              dict(seq=0x48, type=0x0F, parent=PG), dict(seq=0x49, type=0x0F, parent=0x48),
              dict(seq=0x48, type=0x0F, parent=0x49), text2k(0x4A, 5, parent=0x49)]
    doc, ctx = read_pub(pub_2k_file([PG], shapes, [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")]))
    assert outline(doc) == [("Page 1", ["a", "b", "e"])] and not ctx.skipped, (outline(doc), ctx.skipped)
    chain = [dict(seq=0x40, type=0x0F, parent=PG)]
    chain += [dict(seq=0x41 + k, type=0x0F, parent=0x40 + k) for k in range(6)] + [text2k(0x60, 1, parent=0x46)]
    chain += [text2k(0x61, 2, parent=0x43), text2k(0x62, 3, parent=0x44)]
    blob = pub_2k_file([PG], chain, [(1, "deep"), (2, "two"), (3, "three")])
    with patched(limits, MAX_DEPTH=4):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["two"])] and ctx.skipped == {"depth": 1}, (outline(doc), ctx.skipped)


def test_pub2k_text_ids():
    # shapes naming one text id give it once; a text id past the stream names nothing; a missing story gives nothing
    shapes = [text2k(0x30 + k, 1) for k in range(5)] + [text2k(0x40, 9)]
    doc, ctx = read_pub(pub_2k_file([PG], shapes, [(1, "once")]))
    assert outline(doc) == [("Page 1", ["once"])] and not ctx.skipped, outline(doc)
    contents = bytearray(contents_of(pub_2k_file([PG], [text2k(0x30, 1)], [(1, "t")])))
    toff = struct.unpack_from("<I", contents, 0x16)[0]
    struct.pack_into("<I", contents, toff + 2 + 2 * 10 + 6, len(contents) - 0x50)  # the shape at the stream's end
    struct.pack_into("<H", contents, len(contents) - 0x50, 0x08)
    reader = pub_2k._Reader2000(bytes(contents), quill_fix([(1, "t")]), Context())
    ref = reader.refs[0x30]
    assert ref.offset + 0x58 + 2 > len(contents) and reader._tid(ref) is None
    # a box past the stream is none: the shape follows the placed ones
    assert reader._box(ref._replace(offset=len(contents) - 22)) is not None
    assert reader._box(ref._replace(offset=len(contents) - 21)) is None


def _entry_offset(contents, k, offset):
    """`contents` with the Publisher 2000 directory entry k's chunk offset set to `offset`."""
    out = bytearray(contents)
    struct.pack_into("<I", out, struct.unpack_from("<I", out, 0x16)[0] + 2 + 10 * k + 6, offset)
    return bytes(out)


def test_pub2000_wmf_hostile():
    # a data length past its chunk's span, bytes that are no image, or past limits.IMAGE_BYTES: counted image, nothing
    # given; two data chunks for one picture: the first; directory entries naming one data offset share one copy
    def run(shapes, contents=None, cap=None):
        blob = pub_2k_file([PG], shapes, [(1, "t")])
        if contents is not None:
            blob = cfb_bytes({"Contents": contents(contents_of(blob)), "Quill/QuillSub/CONTENTS": quill_fix([])})[0]
        with patched(limits, IMAGE_BYTES=cap if cap is not None else limits.IMAGE_BYTES):
            doc, ctx = read_pub(blob)
        return images_of(doc), ctx.skipped
    long = picture2k(0x30, WMF) + [text2k(0x31, 1)]   # the text chunk follows the data: a longer length is in-stream
    for n in (len(WMF) + 40, 0xFFFFFFFF):
        got, skipped = run(long, lambda c: c.replace(struct.pack("<I", len(WMF)) + WMF[:4], struct.pack("<I", n) + WMF[:4]))
        assert got == [] and skipped == {"image": 1}, (n, got, skipped)
    assert run(picture2k(0x30, b"not an image at all"))[1] == {"image": 1}
    assert run(picture2k(0x30, b""))[1] == {"image": 1}
    assert run(picture2k(0x30, WMF), cap=len(WMF) - 1) == ([], {"image": 1})
    # past IMAGE_BYTES nothing is copied
    contents = contents_of(pub_2k_file([PG], picture2k(0x30, WMF)))
    with patched(limits, IMAGE_BYTES=len(WMF) - 1):
        reader = pub_2k._Reader2000(contents, b"", Context())
        reader.run()
    assert list(reader.wmfs.values()) == [None], reader.wmfs
    # a data chunk at the stream's end, its length past it: counted image, no damaged-part warning
    cut = contents + b"\x21\x00\x00\x00"
    reader = pub_2k._Reader2000(_entry_offset(cut, 3, len(contents)), b"", Context())
    reader.run()
    assert reader.ctx.skipped == {"image": 1} and not reader.ctx.warnings, (reader.ctx.skipped, reader.ctx.warnings)
    # a data chunk naming a text box gives no picture (only a picture shape's data is read)
    doc, ctx = read_pub(pub_2k_file([PG], [text2k(0x30, 1), dict(seq=0x31, type=0x21, parent=0x30, data=WMF)],
                                    [(1, "t")]))
    assert outline(doc) == [("Page 1", ["t"])] and not ctx.skipped, (outline(doc), ctx.skipped)
    assert run(picture2k(0x30, WMF) + picture2k(0x31, WMF), cap=len(WMF)) == ([WMF], {"image": 1})
    other = WMF[:-1] + b"\x01"
    got, skipped = run(picture2k(0x30, WMF) + [dict(seq=0x80, type=0x21, parent=0x30, data=other)])
    assert got == [WMF] and not skipped, (got, skipped)
    # entry 5 (the second picture's data chunk) made to name entry 3's (the first's): one copy, charged twice
    shapes = picture2k(0x30, WMF) + picture2k(0x31, other)
    contents = contents_of(pub_2k_file([PG], shapes))
    toff = struct.unpack_from("<I", contents, 0x16)[0]
    first = struct.unpack_from("<I", contents, toff + 2 + 10 * 3 + 6)[0]
    reader = pub_2k._Reader2000(_entry_offset(contents, 5, first), b"", Context())
    got = [b.data for s in reader.run().blocks for b in s.blocks]
    assert got == [WMF, WMF] and got[0] is got[1] and reader.img == 2 * len(WMF), got


def test_pub2k_guards():
    # a shape that fails is left out (shape); a page (page) and the master (master_page) that fail are left out
    blob = pub_2k_file([PG, 0x127, 0x109], [text2k(0x30, 1), text2k(0x31, 2), text2k(0x32, 3, parent=0x127),
                                           text2k(0x33, 4, parent=0x109)], [(1, "a"), (2, "b"), (3, "c"), (4, "m")])
    real_tid, real_shapes = pub_2k._Reader2000._tid, pub_2k._Reader2000._shapes

    def bad_tid(self, ref):
        if ref.seqnum == 0x31:
            raise ValueError("boom")
        return real_tid(self, ref)

    def bad_shapes(self, seqs):
        if 0x32 in seqs or 0x33 in seqs:
            raise ValueError("boom")
        return real_shapes(self, seqs)
    with patched(pub_2k._Reader2000, _tid=bad_tid):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["a"]), ("Page 2", ["c"]), ("Master page: Background", ["m"])], outline(doc)
    assert ctx.skipped == {"shape": 1}, ctx.skipped
    with patched(pub_2k._Reader2000, _shapes=bad_shapes):
        doc, ctx = read_pub(blob)
    assert outline(doc) == [("Page 1", ["a", "b"])] and ctx.skipped == {"page": 1, "master_page": 1}, ctx.skipped
    # a picture whose read fails: counted image, the page's text stays
    with patched(pub_2k._Reader2000, _wmf=lambda self, at: 1 / 0):
        doc, ctx = read_pub(pub_2k_file([PG], picture2k(0x30, WMF) + [text2k(0x31, 1)], [(1, "a")]))
    assert outline(doc) == [("Page 1", ["a"])] and ctx.skipped == {"image": 1}, ctx.skipped


def test_pub2k_meter():
    # one meter of limits.BIN_RECORDS a document: the directory table, each page, each shape placed and the Quill
    # styles (2000) or each 97 text box tick it; at any cap the read ends without error, record counted at most once
    shapes = [text2k(0x30, 1), dict(seq=0x40, type=0x0F, parent=PG), text2k(0x41, 2, parent=0x40),
              text2k(0x32, 3, parent=0x109), text2k(0x33, 4, parent=0x10D)]
    shapes97 = [text97(0x30, 0), dict(seq=0x40, type=0x0F, parent=PG), text97(0x41, 1, parent=0x40),
                text97(0x32, 2, parent=0x109), text97(0x33, 3, parent=0x10D)]
    for blob, first in ((pub_2k_file([PG, 0x109, 0x10D], shapes, [(1, "a"), (2, "b"), (3, "m"), (4, "s")],
                                     bold=[(0, 1)]), "**a**"),
                        (file97(b"a\x0cb\x0cm\x0cs", shapes97, pages=(PG, 0x109, 0x10D)), "a")):
        full = outline(read_pub(blob)[0])
        assert full == [("Page 1", [first, "b"]), ("Master page: Background", ["m"]),
                        ("Page: Scratch area (hidden)", ["s"])], full
        seen = set()
        for cap in range(0, 120):
            with patched(limits, BIN_RECORDS=cap):
                try:
                    doc, ctx = read_pub(blob)
                except ReadError as e:
                    assert (e.kind, e.detail) == ("damaged", "no document chunk"), (cap, e.kind, e.detail)
                    seen.add("refused")
                    continue
            assert set(ctx.skipped) <= {"record"} and ctx.skipped["record"] <= 1, (cap, ctx.skipped)
            seen.add(str(outline(doc)))
        assert len(seen) > 3 and str(full) in seen, seen


def test_directory_2000_meter():
    # the Publisher 2000 table ticks the meter before each entry: at the cap the rest are left out, record once
    data = contents_fix([(0x100, 0x15, None, bytes(10)), (0x101, 0x14, 0x100, bytes(6)), (0x102, 0x05, 0x101, b"")],
                        version=0x22)
    with patched(limits, BIN_RECORDS=2):
        ctx = Context()
        refs = pub_contents.directory(data, ctx, trailer=0x16)
    assert [r.seqnum for r in refs] == [0x100, 0x101] and ctx.skipped == {"record": 1}, (refs, ctx.skipped)
    meter = pub_contents.Meter(Context())
    assert len(pub_contents.directory(data, Context(), trailer=0x16, meter=meter)) == 3 and meter.used == 3


def _text_head(contents, start=None, end=None, head=None):
    """`contents` (a 97 one) with its text start, end or the header's offset (the u32 at 0x12) replaced."""
    out = bytearray(contents)
    if head is not None:
        struct.pack_into("<I", out, 0x12, head)
    at = struct.unpack_from("<I", out, 0x12)[0] + 14
    if start is not None:
        struct.pack_into("<I", out, at, start)
    if end is not None:
        struct.pack_into("<I", out, at + 4, end)
    return bytes(out)


def run97(contents):
    ctx = Context()
    doc = pub_2k.parse_97(contents, ctx)
    return outline(doc), ctx.skipped, doc.warnings


def test_pub97_text_range():
    # the header's text range is clamped to the stream: an end past it reads to the stream's end (record once), a start
    # past it or a header past it reads no text (record once), an end before the start no text (record once)
    contents = contents_of(file97(b"one\x0ctwo"))
    n = len(contents)
    assert run97(contents) == ([("Page 1", ["one", "two"])], {}, [])
    assert run97(_text_head(contents, end=0xFFFFFFFF)) == ([("Page 1", ["one", "two"])], {"record": 1}, [])
    assert run97(_text_head(contents, end=n + 1))[:2] == ([("Page 1", ["one", "two"])], {"record": 1})
    assert run97(_text_head(contents, end=n))[:2] == ([("Page 1", ["one", "two"])], {})
    for bad in (dict(start=n + 1), dict(start=0xFFFFFFFF, end=0xFFFFFFFF), dict(head=n - 21), dict(head=0xFFFFFFF0),
                dict(start=n - 2, end=n - 3)):
        assert run97(_text_head(contents, **bad)) == ([], {"record": 1}, []), bad
    assert run97(_text_head(contents, head=n - 22, start=n, end=n)) == ([], {}, [])
    # a Contents stream too short for the header's offset: no text, then damaged (no document chunk)
    refused(lambda: run97(b"\xE8\xAC\x22\x00" + bytes(14)), "damaged", "no document chunk")


def test_pub97_boxes_bounded():
    # past pub_2k.BOXES boxes (the u16 text ids' range) the rest is left out, record once; past the room the rest is
    # left out, text once; each box ticks the meter
    contents = contents_of(file97(b"a\x0cb\x0cc\x0cd"))
    with patched(pub_2k, BOXES=2):
        assert run97(contents)[:2] == ([("Page 1", ["a", "b"])], {"record": 1})
    with patched(pub_2k, BOXES=4):
        assert run97(contents)[:2] == ([("Page 1", ["a", "b", "c", "d"])], {})
    with patched(limits, MAX_OUTPUT=2 * (1 + vsdx.LINE)):
        assert run97(contents)[:2] == ([("Page 1", ["a", "b"])], {"text": 1})
    with patched(limits, MAX_OUTPUT=2 * (1 + vsdx.LINE) + 3):  # a break and a paragraph end charge LINE each
        assert run97(contents_of(file97(b"a\x0cb\x0bc\x0cd")))[:2] == ([("Page 1", ["a"])], {"text": 1})
    meter = pub_contents.Meter(Context())
    boxes, _ = pub_2k.text_boxes(contents, Context(), meter)
    assert sorted(boxes) == [0, 1, 2, 3] and meter.used == 4, meter.used
    with patched(limits, BIN_RECORDS=2):
        ctx = Context()
        boxes, _ = pub_2k.text_boxes(contents, ctx, pub_contents.Meter(ctx))
    assert sorted(boxes) == [0, 1] and ctx.skipped == {"record": 1}, (boxes, ctx.skipped)


def test_pub97_garbled():
    # 8-bit text the code page leaves undefined (U+FFFD) or C1 controls past 5% -> the "garbled" warning, no skip kind
    for text, warned in ((b"\x81\x8d\x8f" * 3 + b"x" * 40, True), (b"caf\xe9 \x93ok\x94\r\n" * 5, False),
                         (b"line\r\n" * 30, False), (b"a\x0bb\x0cc\x0c" * 10, False)):
        got = run97(contents_of(file97(text, [text97(0x30, 0)])))
        assert got[2] == (["garbled"] if warned else []) and not got[1], (text, got)
    assert outline(read_pub(file97(b"caf\xe9 \x93ok\x94"))[0]) == [("Page 1", ["café “ok”"])]


def test_convert_garbled_once():
    # reader.convert adds "garbled" from the Markdown only when the parser has not: the warning is listed once
    import reader
    from reader.model import Doc
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.pub"
        src.write_bytes(b"x")
        for warned in (["garbled"], []):
            doc = Doc(blocks=[Para([Text("�" * 50)])], warnings=list(warned))
            with patched(reader, parse=lambda path, ctx, **kw: doc):
                meta = reader.convert(src, Path(tmp) / "out")
            assert meta["warnings"] == ["garbled"], meta["warnings"]
        # one threshold, text.GARBLED, read at call time by both convert and the 97 reader
        from reader import text as text_mod
        contents = contents_of(file97(b"\x81\x8d\x8f" * 3 + b"x" * 40, [text97(0x30, 0)]))
        doc = Doc(blocks=[Para([Text("�" * 5 + "x" * 45)])])
        with patched(text_mod, GARBLED=0.5):
            assert run97(contents)[2] == []
            with patched(reader, parse=lambda path, ctx, **kw: doc):
                assert reader.convert(src, Path(tmp) / "out")["warnings"] == []
        with patched(reader, parse=lambda path, ctx, **kw: doc):
            assert reader.convert(src, Path(tmp) / "out")["warnings"] == ["garbled"]


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
    _table_probes()
    _picture_probes()


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


def _streams(blob):
    """The Contents and Quill streams of a fixture compound file."""
    with Compound(blob) as cf:
        return [cf.read(n) for n in ("Contents", "Quill/QuillSub/CONTENTS")]


def _run_reader(contents, quill, escher=b"", delay=b""):
    ctx = Context()
    reader = pub._Reader(contents, quill, escher, ctx, delay)
    doc = reader.run()
    got = page_blocks(doc)
    tables = [b for b in got if isinstance(b, Table)]
    cells = sum(len(r) for t in tables for r in t.rows)
    pics = sum(isinstance(b, Image) for b in got)
    return f"tables {len(tables)} cells {cells} images {pics} meter {reader.meter.used} {dict(ctx.skipped)}"


def _deflate_zeros(n):
    z, out, chunk = zlib.compressobj(9), [], bytes(MIB)
    for _ in range(n // MIB):
        out.append(z.compress(chunk))
    out.append(z.flush())
    return b"".join(out)


def _table_probes():
    # Task 19, tables: a declared grid of 2^32 x 2^32 over 100k cells (refused before any layout), a grid at
    # GRID_SLOTS with TABLE_NODES cells, 1,000 diagonal spans each reaching into the next (one cell covers all, the
    # others join it), 1,414 diagonal cells of one place each (2M kept places, nearly all gaps: the meter cuts the
    # scan), a TCD bit of 2M offsets over a 3-cell table and 20k tables naming one CELLS chunk
    n = 100_000
    many = [(r, r, c, c) for r in range(50) for c in range(2000)]
    text, bounds = "x\r" * n, list(range(0, 2 * n + 1, 2))
    for name, blob in (("pub: grid 2^32 x 2^32, 100k cells", table_file(many, 0xFFFFFFFF, 0xFFFFFFFF, text, bounds)),
                       ("pub: grid 50 x 2000, 100k cells", table_file(many, 50, 2000, text, bounds)),
                       ("pub: 1000 diagonal spans", table_file([(k, 999, k, 999) for k in range(1000)], 1000, 1000,
                                                               "y\r" * 1000, list(range(0, 2001, 2)))),
                       ("pub: 1414 diagonal cells", table_file([(k, k, k, k) for k in range(1414)], 1414, 1414,
                                                               "y\r" * 1414, list(range(0, 2829, 2))))):
        streams = _streams(blob)
        _probe(name, lambda: _run_reader(*streams), sum(map(len, streams)))
    # a TCD bit of 2M ends over a 1 Mi-character story of a 3-cell table: the cells past the table's are not read
    contents = _streams(table_file(MERGED))[0]
    ends = list(range(1, 2 * MIB + 1))
    tcd = struct.pack("<III", len(ends) - 1, 0, 0xFF00) + struct.pack(f"<{len(ends)}I", *ends)
    quill = _quill_with([(b"TEXT", b"TEXT", 0, ("z" * MIB).encode("utf-16-le")),
                         (b"STRS", b"PLC ", 0, _plc_lengths([MIB])), (b"SYID", b"SYID", 0, struct.pack("<III", 8, 1, 1)),
                         (b"TCD ", b"PLC ", 0, tcd)])
    _probe("pub: TCD 2M offsets, 3 cells", lambda: _run_reader(contents, quill), len(contents) + len(quill))
    # 20k TABLE shapes naming one CELLS chunk of 100 cells (10 x 10), each its own story of 100 cells, the first 990 a
    # TCD bit (the Quill directory holds QUILL_BITS entries): the CELLS chunk walked once, TABLE_NODES spent by the
    # 1,000th table, the rest counted table
    n, cells = 20_000, [(r, r, c, c) for r in range(10) for c in range(10)]
    shapes = [shape(3 + k, k + 1, type=pub_contents.TABLE, extra=pub_table(10, 10, 3 + n)) for k in range(n)]
    shapes.append(shape(3 + n, type=pub_contents.CELLS, extra=pub_cells(cells)))
    contents = _streams(pub_file([page(2, [3 + k for k in range(n)])], shapes))[0]
    ends = list(range(2, 201, 2))
    tcd = struct.pack("<III", len(ends) - 1, 0, 0xFF00) + struct.pack(f"<{len(ends)}I", *ends)
    quill = _quill_with([(b"TEXT", b"TEXT", 0, ("c\r" * 100 * n).encode("utf-16-le")),
                         (b"STRS", b"PLC ", 0, _plc_lengths([200] * n)),
                         (b"SYID", b"SYID", 0, struct.pack("<II", 8, n) + struct.pack(f"<{n}I", *range(1, n + 1)))]
                        + [(b"TCD ", b"PLC ", k, tcd) for k in range(990)])
    _probe("pub: 20k tables, one CELLS chunk", lambda: _run_reader(contents, quill), len(contents) + len(quill))


def _picture_probes():
    # Task 19, pictures: 90k shapes naming one 1 MiB PNG (decoded once, placements charged to IMAGE_BYTES), 90k FBSEs
    # naming one delay-stream BLIP, a delay stream of 8 deflate bombs of 1 GiB each, 4,096 small ones (1 KiB, just
    # past flate's 1 MiB floor) and 1,000 DIBs with bogus header sizes and dimensions
    n = 90_000
    contents, quill = _streams(pub_file([page(2, [3 + k for k in range(n)])], [shape(3 + k) for k in range(n)]))
    png = PNG + bytes(MIB)
    escher = pub_escher(*(pub_sp(3 + k, pib=1) for k in range(n)), store=[oa_fbse(blip_png(png))])
    _probe("pub: 90k shapes, one 1 MiB PNG", lambda: _run_reader(contents, quill, escher), len(escher))
    delay = blip_png(png)
    escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(n)), store=[oa_fbse(delay=0)] * n)
    _probe("pub: 90k FBSEs, one delay BLIP", lambda: _run_reader(contents, quill, escher, delay),
           len(escher) + len(delay))
    bomb = _deflate_zeros(1024 * MIB)
    head = struct.pack("<I16s8sIBB", 1024 * MIB, bytes(16), bytes(8), len(bomb), 0, 0xFE)
    one = oa_record(0xF01B, bytes(range(16)) + head + bomb, inst=0x216)
    delay = one * 8
    k8 = 8
    escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(k8)),
                        store=[oa_fbse(delay=k * len(one)) for k in range(k8)])
    _probe("pub: delay stream, 8 x 1 GiB bombs", lambda: _run_reader(contents, quill, escher, delay),
           len(escher) + len(delay))
    wmf = b"\x01\x00\x09\x00" + bytes(MIB)
    bomb = zlib.compress(wmf, 9)
    head = struct.pack("<I16s8sIBB", len(wmf), bytes(16), bytes(8), len(bomb), 0, 0xFE)
    one = oa_record(0xF01B, bytes(range(16)) + head + bomb, inst=0x216)
    k4 = 4096
    delay = one * k4
    escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(k4)),
                        store=[oa_fbse(delay=k * len(one)) for k in range(k4)])
    _probe("pub: delay stream, 4096 small bombs", lambda: _run_reader(contents, quill, escher, delay),
           len(escher) + len(delay))
    m = 1000
    dibs = []
    for k in range(m):
        # header sizes past the DIB, a core header, a 40-byte one; 2^31 x 2^31 pixels; a palette size of 2^32 - 1 on
        # every seventh, none (so the DIB reaches bmp_to_png) on the rest
        size = (0xFFFFFFFF, 40, 12, 0x7FFFFFFF, 40)[k % 5]
        dib = struct.pack("<IiiHHIIiiII", size, 0x7FFFFFFF, -0x7FFFFFFF, 1, (1, 4, 8, 24, 32, 16)[k % 6], 0,
                          0xFFFFFFFF, 0, 0, 0xFFFFFFFF if k % 7 == 0 else 0, 0) + bytes(1100)
        dibs.append(oa_fbse(blip_bitmap(0xF01F, 0x7A8, dib)))
    escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(m)), store=dibs)
    _probe("pub: 1000 bogus DIBs", lambda: _run_reader(contents, quill, escher), len(escher))
    # nested delay records (fix round 1): one FBSE and one shape each, the records sharing one body, so before the
    # delay read was charged each decoded the shared bytes again
    for n in (400, 1000):
        delay = _nested_bombs(n)
        escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(n)),
                            store=[oa_fbse(delay=63 * k) for k in range(n)])
        _probe(f"pub: {n} nested delay bombs", lambda: _run_reader(contents, quill, escher, delay),
               len(escher) + len(delay))
    for n in (8, 64):
        delay = _nested_dibs(n)
        escher = pub_escher(*(pub_sp(3 + k, pib=k + 1) for k in range(n)),
                            store=[oa_fbse(delay=73 * k) for k in range(n)])
        _probe(f"pub: {n} nested delay DIBs", lambda: _run_reader(contents, quill, escher, delay),
               len(escher) + len(delay))
    _pub_2k_probes()


def _run_2000(contents, quill):
    ctx = Context()
    reader = pub_2k._Reader2000(contents, quill, ctx)
    doc = reader.run()
    paras = sum(len(s.blocks) for s in doc.blocks if isinstance(s, Section))
    return f"paras {paras} meter {reader.meter.used} {dict(ctx.skipped)}"


def _run_97(contents):
    ctx = Context()
    doc = pub_2k.parse_97(contents, ctx)
    paras = sum(len(s.blocks) for s in doc.blocks if isinstance(s, Section))
    return f"paras {paras} {dict(ctx.skipped)} {doc.warnings}"


def _pub_2k_probes():
    # pub_2k.py: a Publisher 2000 directory at the u16 count's cap, every chunk a text box on one page; then every box
    # naming one 1 MiB story
    stories = [(k + 1, f"story {k}\r") for k in range(1000)]
    n = 65_000
    contents = pub_2k_contents([PG], [text2k(0x200 + k, k % 1000 + 1) for k in range(n)])
    quill = quill_fix(stories)
    _probe("pub2000: 65k chunks, one page", lambda: _run_2000(contents, quill), len(contents) + len(quill))
    contents = pub_2k_contents([PG], [text2k(0x200 + k, 1) for k in range(n)])
    quill = quill_fix([(1, "x" * MIB + "\r")])
    _probe("pub2000: 65k text ids, one 1 Mi story", lambda: _run_2000(contents, quill), len(contents) + len(quill))
    contents = pub_2k_contents([PG], [table2k(0x200 + k, k % 1000 + 1) for k in range(n)])
    quill = quill_fix(stories)
    _probe("pub2000: 65k tables, 1000 stories", lambda: _run_2000(contents, quill), len(contents) + len(quill))
    # Publisher 97: a text range ending at 2^32 - 1 over a 64 MiB stream of short lines; 8 MiB of 0x0C (box ends)
    contents = _text_head(pub_2k_contents([PG], [text97(0x30, 0)], text97=b"a") + b"line\r\n" * (64 * MIB // 6),
                          end=0xFFFFFFFF)
    _probe("pub97: text range to 4 GiB, 64 MiB", lambda: _run_97(contents), len(contents))
    contents = pub_2k_contents([PG], [text97(0x30, 0)], text97=b"\x0c" * 8 * MIB)
    _probe("pub97: 8 Mi 0x0C boxes", lambda: _run_97(contents), len(contents))
    contents = pub_2k_contents([PG], [text97(0x200 + k, k) for k in range(1000)], text97=b"ab\x0c" * (8 * MIB // 3))
    _probe("pub97: 2.8 Mi short boxes, 1000 read", lambda: _run_97(contents), len(contents))


def _nested_bombs(n):
    """n WMF BLIP records 63 bytes apart, each running to the stream's end: a record's deflate data is a stored block
    holding the next record's 58 head bytes, then the next one's data, the last one's a raw deflate of 64 MiB of zeros,
    so every record inflates the one shared bomb."""
    z = zlib.compressobj(9, zlib.DEFLATED, -15)
    bomb = b"".join(z.compress(bytes(MIB)) for _ in range(64)) + z.flush()
    total = 63 * (n - 1) + 58 + len(bomb)
    out = bytearray()
    for k in range(n):
        at = 63 * k
        save = total - (at + 58)
        out += struct.pack("<HHI", 0x2160, 0xF01B, total - at - 8) + bytes(16)
        out += struct.pack("<I16s8sIBB", 64 * MIB, bytes(16), bytes(8), save, 0, 0xFE)
        if k < n - 1:
            out += b"\x00" + struct.pack("<HH", 58, 58 ^ 0xFFFF)
    return bytes(out + bomb)


def _nested_dibs(n):
    """n DIB BLIP records 73 bytes apart, each running to the stream's end over one 4096 x 4096 1-bit image: a
    record's pixels start where the next record starts, so all share the 2 MiB of pixels."""
    pixels = bytes(4096 // 8 * 4096)
    total = 73 * n + len(pixels)
    out = bytearray()
    for k in range(n):
        at = 73 * k
        out += struct.pack("<HHI", 0x7A80, 0xF01F, total - at - 8) + bytes(16) + b"\xff"
        out += struct.pack("<IiiHHIIiiII", 40, 4096, 4096, 1, 1, 0, 0, 0, 0, 2, 0) + bytes(8)
    return bytes(out + pixels)


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
