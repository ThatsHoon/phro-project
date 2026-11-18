"""PICT parsing: text and its scripts, shapes, bitmaps, QuickTime JPEG, PICT 1, damage and hostile input.

Run: python -I -X utf8 tests/test_reader_pict.py
"""
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import pict, pict_longtext, pict_op  # noqa: E402
from reader import limits, metafile, pict as pictmod  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.vector import Bitmap, Clip, Path as VPath, Run, run_width, text_lines  # noqa: E402


def runs(d):
    return [i for i in d.items if isinstance(i, Run)]


def paths(d):
    return [i for i in d.items if isinstance(i, VPath)]


def bitmaps(d):
    return [i for i in d.items if isinstance(i, Bitmap)]


def rect(t, l, b, r):
    return struct.pack(">hhhh", t, l, b, r)


def png_pixels(png):
    """(width, height, rows of RGB tuples) of a 24-bit PNG made by images._png."""
    w, h = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        if png[pos + 4:pos + 8] == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (1 + 3 * w) + 1:(y + 1) * (1 + 3 * w)]
        rows.append([tuple(line[3 * x:3 * x + 3]) for x in range(w)])
    return w, h, rows


def test_pict2_longtext():
    d = pictmod.parse(pict([pict_longtext(30, 20, b"Hello")]))
    assert text_lines(d)[0] == ["Hello"]
    r = runs(d)[0]
    assert (r.x, r.y) == (20, 30) and r.width is None
    assert d.size_px() == (96, 96) and d.frame == (0, 0, 72, 72) and not d.damaged


def test_pict_dhdvtext_moves_pen():
    ops = [pict_longtext(10, 5, b"A"),
           pict_op(0x2B, struct.pack(">BBB", 4, 3, 1) + b"B"),   # DHDVText dh dv count text
           pict_op(0x29, struct.pack(">BB", 7, 1) + b"C"),        # DHText
           pict_op(0x2A, struct.pack(">BB", 2, 1) + b"D")]        # DVText
    a, b, c, dd = runs(pictmod.parse(pict(ops)))
    xb = 5 + run_width(a, "A") + 4
    assert (a.x, a.y) == (5, 10) and (b.x, b.y) == (xb, 13), (b.x, b.y)
    xc = xb + run_width(b, "B") + 7
    assert (c.x, c.y) == (xc, 13) and (dd.x, dd.y) == (xc + run_width(c, "C"), 15)


def test_pict_txsize_and_fontname():
    ops = [pict_op(0x2C, struct.pack(">HHB", 2 + 1 + 9, 21, 9) + b"Helvetica"),
           pict_op(0x03, struct.pack(">H", 21)), pict_op(0x0D, struct.pack(">H", 18)),
           pict_op(0x04, b"\x03"), pict_op(0x1A, struct.pack(">HHH", 0xFF00, 0, 0)),
           pict_longtext(40, 2, b"Hi")]
    r = runs(pictmod.parse(pict(ops)))[0]
    assert (r.face, r.height, r.weight, r.italic, r.color) == ("Helvetica", 18, 700, True, 0xFF0000), r


def test_pict_frame_paint_rect_paths():
    ops = [pict_op(0x07, struct.pack(">hh", 2, 2)), pict_op(0x1A, struct.pack(">HHH", 0xFF00, 0, 0)),
           pict_op(0x30, rect(10, 20, 30, 50)), pict_op(0x31, rect(10, 20, 30, 50)),
           pict_op(0x3A), pict_op(0x32, rect(0, 0, 5, 5)),
           pict_op(0x41, rect(0, 0, 20, 20)), pict_op(0x51, rect(1, 1, 9, 9)),
           pict_op(0x71, struct.pack(">H", 22) + rect(0, 0, 9, 9) + struct.pack(">hhhhhh", 0, 0, 9, 0, 0, 9)),
           pict_op(0x61, rect(0, 0, 10, 10) + struct.pack(">hh", 0, 90))]
    d = pictmod.parse(pict(ops))
    p = paths(d)
    assert p[0].pen.color == 0xFF0000 and p[0].pen.width == 2 and p[0].brush is None
    assert p[0].segs == [("M", 20, 10), ("L", 50, 10), ("L", 50, 30), ("L", 20, 30), ("Z",)]
    assert p[1].pen is None and p[1].brush.color == 0xFF0000 and p[1].segs == p[0].segs
    assert p[2].brush.color == 0xFFFFFF and p[2].segs == p[0].segs   # sameRect erase: the last rect, the back color
    assert len(p) == 8 and all(it.segs for it in p)
    assert not d.damaged


def _pixmap(w, h, rowbytes, pixsize, packtype=0, cmp=1, cmpsize=None, table=None, direct=False):
    out = struct.pack(">H", 0x8000 | rowbytes) + rect(0, 0, h, w)
    out += struct.pack(">HHIiiHHHHIII", 0, packtype, 0, 72 << 16, 72 << 16, 16 if direct else 0, pixsize, cmp,
                       cmpsize or pixsize // cmp, 0, 0, 0)
    if table is not None:
        out += struct.pack(">IHH", 0, 0, len(table) - 1)
        for i, (r, g, b) in enumerate(table):
            out += struct.pack(">HHHH", i, r << 8, g << 8, b << 8)
    return out


def test_pict_packbitsrect_bitmap():
    # an 8-bit PixMap, 10x2, rowBytes 10 (packed rows); index 0 black, 1 red
    row1 = bytes([256 - 9, 1])       # repeat 10 times
    row0 = bytes([256 - 9, 0])
    data = (_pixmap(10, 2, 10, 8, table=[(0, 0, 0), (255, 0, 0)]) + rect(0, 0, 2, 10) + rect(5, 6, 7, 16)
            + struct.pack(">H", 0)
            + bytes([len(row1)]) + row1 + bytes([len(row0)]) + row0)
    d = pictmod.parse(pict([pict_op(0x98, data)]))
    bm = bitmaps(d)
    assert len(bm) == 1 and bm[0].kind == "png" and bm[0].box == (6, 5, 16, 7), d.items
    w, h, rows = png_pixels(bm[0].data)
    assert (w, h) == (10, 2) and rows[0][0] == (255, 0, 0) and rows[1][9] == (0, 0, 0)
    # a 1-bit BitMap, 8x1, rowBytes 1 (unpacked): the first pixel set is black, the rest white
    bits = struct.pack(">H", 1) + rect(0, 0, 1, 8) + rect(0, 0, 1, 8) + rect(0, 0, 1, 8) + struct.pack(">H", 0) + b"\x80"
    d = pictmod.parse(pict([pict_op(0x90, bits)]))
    w, h, rows = png_pixels(bitmaps(d)[0].data)
    assert rows[0][0] == (0, 0, 0) and rows[0][1] == (255, 255, 255)


def test_pict_directbitsrect_bitmap():
    # 32-bit, packType 4 (RGB planes), 4x1: red
    row = bytes([253, 255]) + bytes([253, 0]) + bytes([253, 0])
    data = (struct.pack(">I", 0xFF) + _pixmap(4, 1, 16, 32, packtype=4, cmp=3, cmpsize=8, direct=True)
            + rect(0, 0, 1, 4) + rect(2, 2, 3, 6) + struct.pack(">H", 0) + bytes([len(row)]) + row)
    d = pictmod.parse(pict([pict_op(0x9A, data)]))
    bm = bitmaps(d)[0]
    w, h, rows = png_pixels(bm.data)
    assert (w, h) == (4, 1) and rows[0] == [(255, 0, 0)] * 4 and bm.box == (2, 2, 6, 3)
    # 16-bit, packType 3 (words), 4x1, rowBytes 8: 0x7C00 is red
    row = bytes([0xFD, 0x7C, 0x00])
    data = (struct.pack(">I", 0xFF) + _pixmap(4, 1, 8, 16, packtype=3, cmp=3, cmpsize=5, direct=True)
            + rect(0, 0, 1, 4) + rect(0, 0, 1, 4) + struct.pack(">H", 0) + bytes([len(row)]) + row)
    d = pictmod.parse(pict([pict_op(0x9A, data)]))
    w, h, rows = png_pixels(bitmaps(d)[0].data)
    assert rows[0] == [(255, 0, 0)] * 4, rows


JPG = b"\xff\xd8\xff\xe0" + b"jfif-bytes!"


def _quicktime(ctype: bytes, image: bytes, src=(0, 0, 10, 20), dx=5, dy=7) -> bytes:
    one = 0x10000
    qt = struct.pack(">h9i", 0, one, 0, 0, 0, one, 0, dx * one, dy * one, 0x40000000)
    qt += struct.pack(">I", 0) + rect(0, 0, 0, 0) + struct.pack(">H", 0) + rect(*src) + struct.pack(">II", 0, 0)
    desc = struct.pack(">I4sIHHHHIIIHHiiIH", 86, ctype, 0, 0, 0, 0, 0, 0, 0, 0, src[3], src[2], 72 << 16, 72 << 16,
                       len(image), 1) + bytes(32) + struct.pack(">Hh", 24, -1)
    assert len(desc) == 86, len(desc)
    body = qt + desc + image
    return pict_op(0x8200, struct.pack(">I", len(body)) + body)


def test_pict_quicktime_jpeg():
    d = pictmod.parse(pict([_quicktime(b"jpeg", JPG)]))
    bm = bitmaps(d)
    assert len(bm) == 1 and bm[0].kind == "jpg" and bm[0].data == JPG and bm[0].box == (5, 7, 25, 17), bm
    assert not d.damaged


def test_pict_quicktime_other_codec_skipped():
    d = pictmod.parse(pict([_quicktime(b"rpza", b"\x01" * 20), pict_longtext(5, 5, b"x")]))
    assert not bitmaps(d) and d.skipped >= 1
    assert text_lines(d)[0] == ["x"] and not d.damaged


def test_pict_without_head():
    ops = [pict_longtext(30, 20, b"Hello"), pict_op(0x30, rect(1, 1, 5, 5))]
    a = pictmod.parse(pict(ops, head=False))
    b = pictmod.parse(pict(ops, head=True))
    assert text_lines(a)[0] == text_lines(b)[0] == ["Hello"] and len(paths(a)) == len(paths(b)) == 1
    assert a.frame == b.frame


def test_pict_extended_header_resolution():
    d = pictmod.parse(pict([], frame=(0, 0, 144, 144), dpi=144))
    assert d.dpi == 144 and d.size_px() == (96, 96)


def test_pict1():
    ops = [pict_longtext(30, 20, b"Old", v2=False), pict_op(0x30, rect(1, 2, 9, 8), v2=False),
           pict_op(0x22, struct.pack(">hhbb", 0, 0, 5, 5), v2=False), pict_op(0xA0, b"\x00\x01", v2=False),
           pict_op(0x03, struct.pack(">H", 3), v2=False), pict_op(0x0D, b"\x00\x0a", v2=False)]
    d = pictmod.parse(pict(ops, version=1))
    assert text_lines(d)[0] == ["Old"] and len(paths(d)) == 2 and d.skipped == 1 and not d.damaged
    assert runs(d)[0].x == 20


def test_pict_truncated_opcode():
    data = pict([pict_op(0x30, rect(1, 1, 5, 5)), pict_longtext(30, 20, b"Hello")])
    d = pictmod.parse(data[:-6])        # the end opcode and the text's tail are gone
    assert d.damaged and len(paths(d)) == 1 and not runs(d)
    d = pictmod.parse(data[:-2])        # only OpEndPic is missing
    assert d.damaged and text_lines(d)[0] == ["Hello"]


def test_pict_korean_script():
    ops = [pict_op(0x03, struct.pack(">H", 19456)), pict_longtext(20, 5, "한글".encode("euc_kr")),
           pict_op(0x03, struct.pack(">H", 16384)), pict_longtext(40, 5, "日本".encode("shift_jis")),
           pict_op(0x03, struct.pack(">H", 28672)), pict_longtext(50, 5, "中".encode("gb2312")),
           pict_op(0x03, struct.pack(">H", 18432)), pict_longtext(60, 5, "中".encode("big5")),
           pict_op(0x03, struct.pack(">H", 3)), pict_longtext(70, 5, "é".encode("mac_roman"))]
    d = pictmod.parse(pict(ops))
    assert [r.text for r in runs(d)] == ["한글", "日本", "中", "中", "é"]


def test_pict_clip_region_and_pen_hidden():
    ops = [pict_op(0x01, struct.pack(">H", 10) + rect(0, 0, 50, 50)), pict_op(0x07, struct.pack(">hh", 0, 0)),
           pict_op(0x30, rect(1, 1, 5, 5)), pict_op(0x20, struct.pack(">hhhh", 0, 0, 9, 9))]
    d = pictmod.parse(pict(ops))
    assert isinstance(d.items[0], Clip) and not paths(d)


def test_pict_pixpat_is_stepped_over():
    pat = struct.pack(">H", 1) + bytes(8) + _pixmap(2, 2, 2, 8, table=[(0, 0, 0), (255, 255, 255)]) + bytes([0, 1, 1, 0])
    d = pictmod.parse(pict([pict_op(0x14, pat), pict_longtext(5, 5, b"x")]))
    assert text_lines(d)[0] == ["x"] and not d.damaged


def test_pict_tall_narrow_bitmap_is_bounded_by_its_width():
    import time
    import tracemalloc
    # 8-bit, w=1, h=32000, rowBytes 16383: each row is a u16 count and two packed bytes, but is kept as 1 byte
    row = struct.pack(">H", 2) + bytes([0, 1])
    data = (_pixmap(1, 32000, 16383, 8, table=[(0, 0, 0), (255, 0, 0)]) + rect(0, 0, 32000, 1) + rect(0, 0, 32000, 1)
            + struct.pack(">H", 0) + row * 32000)
    blob = pict([pict_op(0x98, data)])
    t0 = time.time()
    d = pictmod.parse(blob)
    took = time.time() - t0
    assert len(bitmaps(d)) == 1 and not d.damaged and took < 1, took
    tracemalloc.start()                                 # the clock apart from tracemalloc's own slowing
    pictmod.parse(blob)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 50 * 2**20, peak


def test_pict_extended_dpi_is_clamped():
    assert pictmod.parse(pict([], dpi=1 / 65536)).dpi == 1.0
    assert pictmod.parse(pict([], dpi=30000)).dpi == 4800.0
    assert pictmod.parse(pict([], dpi=1 / 65536)).size_px()[0] <= 72 * 96


def test_pict_unpacked_32bit_row_is_pad_r_g_b():
    for pt in (0, 2, 4):
        data = (struct.pack(">I", 0xFF) + _pixmap(1, 1, 4, 32, packtype=pt, cmp=3, cmpsize=8, direct=True)
                + rect(0, 0, 1, 1) + rect(0, 0, 1, 1) + struct.pack(">H", 0) + bytes([0, 255, 0, 0]))
        d = pictmod.parse(pict([pict_op(0x9A, data)]))
        assert png_pixels(bitmaps(d)[0].data)[2] == [[(255, 0, 0)]], pt


def test_pict_font_zero_is_chicago():
    d = pictmod.parse(pict([pict_longtext(5, 5, b"x")]))
    assert runs(d)[0].face == "Chicago"


def test_pict_not_a_pict_and_caps():
    for bad in (b"\x00" * 600, b"abc", b""):
        try:
            pictmod.parse(bad)
        except ReadError as e:
            assert e.kind == "damaged"
        else:
            raise AssertionError("no ReadError")
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 3
    try:
        pictmod.parse(pict([pict_op(0x30, rect(0, 0, 1, 1))] * 10))
    except ReadError as e:
        assert e.kind == "too_large"
    else:
        raise AssertionError("no ReadError")
    finally:
        limits.METAFILE_RECORDS = old


def test_pict_item_weight_matches_metafile():
    assert pictmod.ITEM_WEIGHT == metafile.ITEM_WEIGHT


def test_pict_bitmap_over_pixel_cap_is_skipped_not_allocated():
    row = bytes([256 - 9, 1])
    data = (_pixmap(10, 2, 10, 8, table=[(0, 0, 0), (255, 0, 0)]) + rect(0, 0, 2, 10) + rect(0, 0, 2, 10)
            + struct.pack(">H", 0) + bytes([2]) + row + bytes([2]) + row)
    from reader import images
    old = images._MAX_PIXELS
    images._MAX_PIXELS = 10
    try:
        d = pictmod.parse(pict([pict_op(0x98, data), pict_longtext(5, 5, b"x")]))
    finally:
        images._MAX_PIXELS = old
    assert not bitmaps(d) and d.skipped == 1 and text_lines(d)[0] == ["x"] and not d.damaged


def _assert_finite(d):
    import math
    assert all(map(math.isfinite, d.frame))
    d.size_px()
    for it in d.items:
        if isinstance(it, VPath):
            assert all(math.isfinite(v) for s in it.segs for v in s[1:])
        elif isinstance(it, Run):
            assert all(map(math.isfinite, (it.x, it.y, it.height)))
        elif isinstance(it, Bitmap):
            assert all(map(math.isfinite, it.box))
        elif isinstance(it, Clip) and it.segs:
            assert all(math.isfinite(v) for s in it.segs for v in s[1:])


def test_pict_mutated_never_raises():
    import random
    import time
    row = bytes([253, 255]) + bytes([253, 0]) + bytes([253, 0])
    dbits = (struct.pack(">I", 0xFF) + _pixmap(4, 1, 16, 32, packtype=4, cmp=3, cmpsize=8, direct=True)
             + rect(0, 0, 1, 4) + rect(2, 2, 3, 6) + struct.pack(">H", 0) + bytes([len(row)]) + row)
    pbits = (_pixmap(10, 2, 10, 8, table=[(0, 0, 0), (255, 0, 0)]) + rect(0, 0, 2, 10) + rect(5, 6, 7, 16)
             + struct.pack(">H", 0) + bytes([2, 247, 1, 2, 247, 0]))
    poly = struct.pack(">H", 22) + rect(0, 0, 9, 9) + struct.pack(">hhhhhh", 0, 0, 9, 0, 0, 9)
    seeds = [pict([pict_op(0x01, struct.pack(">H", 10) + rect(0, 0, 50, 50)),
                   pict_op(0x2C, struct.pack(">HHB", 6, 19456, 3) + b"abc"), pict_op(0x03, struct.pack(">H", 19456)),
                   pict_longtext(30, 20, "한".encode("euc_kr")), pict_op(0x2B, struct.pack(">BBB", 4, 3, 1) + b"B"),
                   pict_op(0x30, rect(1, 1, 5, 5)), pict_op(0x41, rect(1, 1, 50, 50)), pict_op(0x59),
                   pict_op(0x51, rect(0, 0, 9, 9)), pict_op(0x71, poly), pict_op(0x61, rect(0, 0, 9, 9) + b"\0\x10\0\x50"),
                   pict_op(0x69, b"\x00\x10\x01\x00"), pict_op(0x81, struct.pack(">H", 10) + rect(0, 0, 4, 4)),
                   pict_op(0x20, struct.pack(">hhhh", 0, 0, 9, 9)), pict_op(0x22, struct.pack(">hhbb", 0, 0, 5, 5)),
                   pict_op(0x0B, struct.pack(">hh", 4, 4)), pict_op(0x1B, struct.pack(">HHH", 1, 2, 3)),
                   pict_op(0xA1, struct.pack(">HH", 1, 2) + b"ab"), pict_op(0x98, pbits), pict_op(0x9A, dbits),
                   _quicktime(b"jpeg", JPG), pict_op(0x8201, struct.pack(">I", 4) + b"abcd"),
                   pict_op(0x12, struct.pack(">H", 2) + bytes(8) + bytes(6)), pict_op(0x0E, struct.pack(">I", 205))]),
             pict([pict_longtext(30, 20, b"Old", v2=False), pict_op(0x30, rect(1, 2, 9, 8), v2=False),
                   pict_op(0x90, struct.pack(">H", 1) + rect(0, 0, 1, 8) * 3 + b"\0\0\x80", v2=False)], version=1)]
    rnd = random.Random(5)
    t0 = time.time()
    for n in range(1500):
        b = bytearray(seeds[n % len(seeds)])
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(512, len(b))] = rnd.randrange(256)
        try:
            d = pictmod.parse(bytes(b))
        except ReadError:
            continue
        _assert_finite(d)
    assert time.time() - t0 < 10


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
