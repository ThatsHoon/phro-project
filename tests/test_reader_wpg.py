"""WPG parsing: WPG 1 and WPG 2 shapes, text, colormaps and bitmaps, the frame, skipped records, damage and hostile input.

Run: python -I -X utf8 tests/test_reader_wpg.py
"""
import math
import struct
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import (wpg1, wpg1_bitmap1, wpg1_bitmap2, wpg1_colormap, wpg1_ellipse, wpg1_fill, wpg1_line,  # noqa: E402
                          wpg1_line_attr, wpg1_poly, wpg1_rec, wpg1_rect, wpg1_text, wpg1_text2, wpg1_text_attr,
                          wpg2, wpg2_arc, wpg2_bitmap, wpg2_bitmap_data, wpg2_brush, wpg2_char, wpg2_palette,
                          wpg2_pen, wpg2_pen_size, wpg2_polycurve, wpg2_polyline, wpg2_rec, wpg2_rect, wpg2_textblock,
                          wpg2_textdata, wpg2_textline, wpg_head, wpg_varint)
from reader import images, limits, metafile, pict as pictmod, wpg  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.vector import Bitmap, Clip, Path as VPath, Run, text_lines  # noqa: E402


def runs(d):
    return [i for i in d.items if isinstance(i, Run)]


def paths(d):
    return [i for i in d.items if isinstance(i, VPath)]


def bitmaps(d):
    return [i for i in d.items if isinstance(i, Bitmap)]


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


def wpg2_pt4(*v):
    return struct.pack("<hhhh", *v)


def close(a, b, eps=1e-6):
    return abs(a - b) <= eps


def seg_close(seg, want, eps=1e-6):
    return seg[0] == want[0] and len(seg) == len(want) and all(close(a, b, eps) for a, b in zip(seg[1:], want[1:]))


def pts(path):
    return [(s[-2], s[-1]) for s in path.segs if s[0] in "ML"]


# ---- WPG 1 ------------------------------------------------------------------------------------------------------------

def test_wpg1_line_polyline_rect_ellipse():
    d = wpg.parse(wpg1([
        wpg1_line_attr(1, 4, 12), wpg1_fill(1, 12),
        wpg1_line(100, 200, 300, 400),
        wpg1_poly(0x06, [(0, 0), (100, 100), (200, 0)]),
        wpg1_rect(10, 20, 100, 50),
        wpg1_poly(0x08, [(0, 0), (50, 0), (50, 50)]),
        wpg1_ellipse(600, 600, 100, 50),
    ]))
    ps = paths(d)
    assert len(ps) == 5 and not d.damaged and d.skipped == 0
    line, poly, rect, tri, ell = ps
    assert line.segs == [("M", 100.0, 1000.0), ("L", 300.0, 800.0)] and line.brush is None
    assert line.pen.color == 0x7F0000 and line.pen.width == 12
    assert pts(poly) == [(0.0, 1200.0), (100.0, 1100.0), (200.0, 1200.0)] and poly.brush is None
    assert [s[0] for s in poly.segs] == ["M", "L", "L"]
    # the rectangle's corner is its lower left: y flips to 1200 - (20 + 50)
    assert pts(rect) == [(10.0, 1130.0), (110.0, 1130.0), (110.0, 1180.0), (10.0, 1180.0)]
    assert rect.segs[-1] == ("Z",) and rect.brush.color == 0xFF0000
    assert tri.segs[-1] == ("Z",) and tri.brush.color == 0xFF0000
    assert ell.segs[0] == ("M", 700.0, 600.0) and ell.segs[-1] == ("Z",) and ell.brush.color == 0xFF0000
    xs = [v for s in ell.segs if s[0] == "C" for v in s[1::2]]
    ys = [v for s in ell.segs if s[0] == "C" for v in s[2::2]]
    assert close(min(xs), 500) and close(max(xs), 700) and close(min(ys), 550) and close(max(ys), 650)
    assert d.frame == (0, 0, 1200, 1200) and d.dpi == 1200 and d.size_px() == (96, 96)


def test_wpg1_fill_none_and_pen_none():
    d = wpg.parse(wpg1([wpg1_fill(0, 0), wpg1_line_attr(1, 0, 5), wpg1_rect(0, 0, 10, 10),
                        wpg1_line_attr(0, 0, 5), wpg1_fill(1, 2), wpg1_rect(0, 0, 10, 10),
                        wpg1_line_attr(1, 0, 0), wpg1_rect(0, 0, 10, 10)]))
    a, b, c = paths(d)
    assert a.brush is None and a.pen is not None
    assert b.pen is None and b.brush.color == 0x007F00
    assert c.pen is None                      # a zero width is no line (libwpg)


def test_wpg1_ellipse_rotation():
    d = wpg.parse(wpg1([wpg1_ellipse(600, 600, 200, 50, rot=90)]))
    ell = paths(d)[0]
    xs = [v for s in ell.segs if s[0] == "C" for v in s[1::2]]
    ys = [v for s in ell.segs if s[0] == "C" for v in s[2::2]]
    assert close(min(xs), 550, 1e-4) and close(max(xs), 650, 1e-4) and close(min(ys), 400, 1e-4)


def test_wpg1_curved_polyline():
    pts7 = [(0, 0), (10, 0), (20, 10), (30, 10), (40, 20), (50, 20), (60, 30)]
    d = wpg.parse(wpg1([wpg1_rec(0x13, struct.pack("<IH", 0, 7) + b"".join(struct.pack("<hh", *p) for p in pts7))]))
    segs = paths(d)[0].segs
    assert [s[0] for s in segs] == ["M", "C", "C"] and segs[0] == ("M", 0.0, 1200.0)
    assert segs[2] == ("C", 40.0, 1180.0, 50.0, 1180.0, 60.0, 1170.0)


def test_wpg1_graphics_text():
    d = wpg.parse(wpg1([wpg1_text_attr(avg=100, size=200, align=0, color=4), wpg1_text(100, 1000, b"Hello")]))
    r = runs(d)[0]
    assert text_lines(d)[0] == ["Hello"]
    assert (r.x, r.y, r.height, r.width, r.color) == (100, 200, 200, 500, 0x7F0000), r
    # centred text has its x in the middle of the string, right-aligned at its end
    for align, x in ((1, -150.0), (2, -400.0)):
        d = wpg.parse(wpg1([wpg1_text_attr(avg=100, size=200, align=align), wpg1_text(100, 1000, b"Hello")]))
        assert runs(d)[0].x == x, (align, runs(d)[0])
    # no attribute record: a 12 point default
    d = wpg.parse(wpg1([wpg1_text(0, 600, b"A")]))
    assert runs(d)[0].height == 200 and runs(d)[0].width is None
    # type 2 text is WordPerfect 5 text: ASCII and the extended-character group [C0 char set C0]
    d = wpg.parse(wpg1([wpg1_text_attr(size=100), wpg1_text2(10, 500, 90, 500, b"Hi \xc0\x27\x01\xc0")]))
    assert text_lines(d)[0] == ["Hi ç"], text_lines(d)
    assert (runs(d)[0].x, runs(d)[0].y) == (10, 700)
    # a count past the record keeps what is there
    bad = wpg1_rec(0x0C, struct.pack("<Hhh", 500, 0, 600) + b"abc")
    assert text_lines(wpg.parse(wpg1([bad])))[0] == ["abc"]


def test_wpg1_bitmap_rle():
    # 4x2, 8 bits: a run of four 1s (opcode 0x84), then four literal 2s
    d = wpg.parse(wpg1([wpg1_bitmap1(4, 2, 8, bytes([0x84, 1, 4, 2, 2, 2, 2]))]))
    bm = bitmaps(d)
    assert len(bm) == 1 and bm[0].kind == "png" and not d.damaged
    w, h, rows = png_pixels(bm[0].data)
    assert (w, h) == (4, 2) and rows[0] == [(0, 0, 127)] * 4 and rows[1] == [(0, 127, 0)] * 4, rows
    assert all(close(a, b) for a, b in zip(bm[0].box, (0, 0, 4 * 1200 / 72, 2 * 1200 / 72)))
    # opcode 0x00 copies the last scanline count times
    d = wpg.parse(wpg1([wpg1_bitmap1(4, 3, 8, bytes([0x84, 1, 0, 2]))]))
    w, h, rows = png_pixels(bitmaps(d)[0].data)
    assert h == 3 and rows[0] == rows[1] == rows[2] == [(0, 0, 127)] * 4
    # one bit: a set bit is white, a clear one black
    d = wpg.parse(wpg1([wpg1_bitmap1(8, 1, 1, bytes([1, 0x80]))]))
    rows = png_pixels(bitmaps(d)[0].data)[2]
    assert rows[0][0] == (255, 255, 255) and rows[0][1] == (0, 0, 0)
    # four bits: two pixels in a byte, the high nibble first
    d = wpg.parse(wpg1([wpg1_bitmap1(2, 1, 4, bytes([1, 0x12]))]))
    assert png_pixels(bitmaps(d)[0].data)[2][0] == [(0, 0, 127), (0, 127, 0)]
    # two bits: four pixels, rows padded to a byte
    d = wpg.parse(wpg1([wpg1_bitmap1(3, 2, 2, bytes([2, 0b00011011, 0b11100100]))]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(0, 0, 0), (0, 0, 127), (0, 127, 0)],
                                                  [(0, 127, 127), (0, 127, 0), (0, 0, 127)]]
    # type 2 has a corner pair (y up), and a short RLE is zero filled
    d = wpg.parse(wpg1([wpg1_bitmap2(100, 500, 300, 300, 2, 2, 8, bytes([2, 1, 1]))]))
    bm = bitmaps(d)[0]
    assert bm.box == (100, 700, 300, 900), bm.box
    assert png_pixels(bm.data)[2] == [[(0, 0, 127), (0, 0, 127)], [(0, 0, 0), (0, 0, 0)]]
    # a bad depth is dropped
    d = wpg.parse(wpg1([wpg1_bitmap1(2, 2, 3, bytes([1, 1]))]))
    assert not bitmaps(d)


def test_wpg1_colormap():
    d = wpg.parse(wpg1([wpg1_colormap(1, [(255, 0, 0), (0, 0, 255)]), wpg1_fill(1, 1), wpg1_rect(0, 0, 5, 5),
                        wpg1_bitmap1(2, 1, 8, bytes([1, 1, 1, 2]))]))
    assert paths(d)[0].brush.color == 0xFF0000
    assert png_pixels(bitmaps(d)[0].data)[2][0] == [(255, 0, 0), (0, 0, 255)]
    # a map running past 256 entries changes nothing; one with fewer entries than it says keeps those it has
    d = wpg.parse(wpg1([wpg1_colormap(250, [(255, 0, 0)] * 8), wpg1_fill(1, 250), wpg1_rect(0, 0, 5, 5)]))
    assert paths(d)[0].brush.color != 0xFF0000
    cut = wpg1_rec(0x0E, struct.pack("<HH", 1, 5) + bytes([255, 0, 0]))
    d = wpg.parse(wpg1([cut, wpg1_fill(1, 1), wpg1_rect(0, 0, 5, 5)]))
    assert paths(d)[0].brush.color == 0xFF0000 and not d.damaged


# ---- WPG 2 ------------------------------------------------------------------------------------------------------------

def test_wpg2_polyline_and_text():
    d = wpg.parse(wpg2([wpg2_pen(0xFF0000), wpg2_polyline([(0, 0), (100, 100), (200, 0)]),
                        wpg2_textline(100, 200), wpg2_textdata(b"Hello")]))
    p = paths(d)[0]
    assert pts(p) == [(0.0, 1200.0), (100.0, 1100.0), (200.0, 1200.0)] and p.brush is None
    assert p.pen.color == 0xFF0000 and [s[0] for s in p.segs] == ["M", "L", "L"]
    assert text_lines(d)[0] == ["Hello"] and not d.damaged
    r = runs(d)[0]
    assert (r.x, r.y) == (100, 1000) and r.height == 200
    # extended characters [F0 char set F0], a paragraph end and a space byte
    d = wpg.parse(wpg2([wpg2_textline(0, 200), wpg2_textdata(b"Hi\xf0\x27\x01\xf0\xb4B\x80C")]))
    assert text_lines(d)[0] == ["Hiç", "B C"], text_lines(d)
    # a closed or filled polyline is a polygon; two points stay a line
    d = wpg.parse(wpg2([wpg2_brush(0x00FF00), wpg2_polyline([(0, 0), (10, 0), (10, 10)], filled=True),
                        wpg2_polyline([(0, 0), (10, 0), (10, 10)], closed=True),
                        wpg2_polyline([(0, 0), (10, 10)], closed=True)]))
    a, b, c = paths(d)
    assert a.segs[-1] == ("Z",) and a.brush.color == 0x00FF00 and a.pen is not None
    assert b.segs[-1] == ("Z",) and b.brush is None and b.pen is not None
    assert [s[0] for s in c.segs] == ["M", "L"]
    # text blocks flow down from the top of their box, one run a line
    d = wpg.parse(wpg2([wpg2_textblock(100, 400, 500, 100), wpg2_textdata(b"A\xb4B")]))
    rs = runs(d)
    assert [r.text for r in rs] == ["A", "B"] and rs[0].x == 100 and rs[1].y > rs[0].y, rs
    # a text line's baseline angle turns the run
    d = wpg.parse(wpg2([wpg2_textline(100, 200, angle=90.0), wpg2_textdata(b"Up")]))
    assert close(runs(d)[0].angle, 90.0)


def test_wpg2_double_precision_and_origin():
    d = wpg.parse(wpg2([wpg2_polyline([(150, 250), (160, 250)], dp=True)], dp=True, size=(1200, 600), org=(100, 200)))
    assert pts(paths(d)[0]) == [(50.0, 550.0), (60.0, 550.0)], pts(paths(d)[0])
    assert d.frame == (0, 0, 1200, 600)
    # a double precision colour is u16 a channel
    rec = wpg2_rec(0x26, struct.pack("<HHHH", 0xFF00, 0x0000, 0x8000, 0))
    d = wpg.parse(wpg2([rec, wpg2_polyline([(0, 0), (5, 5)], dp=True)], dp=True))
    assert paths(d)[0].pen.color == 0xFF0080


def test_wpg2_matrix():
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (100, 0)], move=(10, 20))]))
    assert pts(paths(d)[0]) == [(10.0, 1180.0), (110.0, 1180.0)]
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (100, 0)], scale=(2.0, 3.0))]))
    assert pts(paths(d)[0]) == [(0.0, 1200.0), (200.0, 1200.0)]
    # a quarter turn: (100, 0) goes to (0, 100) and its y flips
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (100, 0)], rotate=90)]))
    (_, _), (x1, y1) = pts(paths(d)[0])
    assert close(x1, 0, 1e-3) and close(y1, 1100, 1e-3), (x1, y1)


def test_wpg2_shapes():
    d = wpg.parse(wpg2([wpg2_rect(100, 100, 300, 200), wpg2_rect(300, 400, 100, 300, rx=20, ry=20, filled=True),
                        wpg2_arc(500, 500, 100, 50, 100, 0, 100, 0),
                        wpg2_arc(500, 500, 100, 100, 100, 0, 0, 100),
                        wpg2_polycurve([((0, 0), (0, 0), (10, 0)), ((20, 10), (30, 10), (40, 10))], closed=True)]))
    rect, rrect, ell, arc, cur = paths(d)
    assert pts(rect) == [(100.0, 1100.0), (300.0, 1100.0), (300.0, 1000.0), (100.0, 1000.0)]
    assert rrect.brush is not None and any(s[0] == "C" for s in rrect.segs)
    xs = [s[-2] for s in rrect.segs if s[0] != "Z"]
    assert min(xs) >= 100 - 1e-9 and max(xs) <= 300 + 1e-9
    # the same start and end point: a whole ellipse
    assert ell.segs[-1] == ("Z",) and sum(s[0] == "C" for s in ell.segs) == 4
    # a quarter arc counterclockwise (y up) from (600, 500) to (500, 600): ends above the centre after the flip
    assert seg_close(arc.segs[0], ("M", 600.0, 700.0)) and arc.segs[-1][0] == "C" and len(arc.segs) == 2
    assert close(arc.segs[-1][5], 500.0) and close(arc.segs[-1][6], 600.0), arc.segs
    assert cur.segs[0] == ("M", 0.0, 1200.0) and cur.segs[1][0] == "C" and cur.segs[-1] == ("Z",)
    assert cur.segs[1] == ("C", 10.0, 1200.0, 20.0, 1190.0, 30.0, 1190.0), cur.segs[1]


def test_wpg2_compound_polygon():
    comp = wpg2_rec(0x1A, wpg2_char(filled=True, closed=True, winding=False), ext=2)
    a = wpg2_polyline([(0, 0), (100, 0), (100, 100)])
    b = wpg2_polyline([(20, 20), (60, 20), (60, 60)])
    d = wpg.parse(wpg2([wpg2_brush(0x0000FF), comp, a, b, wpg2_polyline([(0, 0), (1, 1)])]))
    ps = paths(d)
    assert len(ps) == 2, ps           # the compound is one path; the polyline after it is its own
    c = ps[0]
    assert [s[0] for s in c.segs] == ["M", "L", "L", "M", "L", "L", "Z"] and c.even_odd and c.brush.color == 0x0000FF
    # winding rule
    comp = wpg2_rec(0x1A, wpg2_char(filled=True, winding=True), ext=1)
    d = wpg.parse(wpg2([comp, a]))
    assert paths(d)[0].even_odd is False


def test_wpg2_bitmap():
    pal = wpg2_palette(0, [(255, 0, 0), (0, 255, 0)])
    d = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50), wpg2_bitmap_data(2, 1, 4, 0, b"\x00\x01")]))
    bm = bitmaps(d)[0]
    assert bm.kind == "png" and bm.box == (0, 1150, 100, 1200), bm.box
    assert png_pixels(bm.data)[2] == [[(255, 0, 0), (0, 255, 0)]]
    # RGB, run-length encoded: data size 3, a run of two pixels
    rle = bytes([0x7D, 3, 0x81, 10, 20, 30])
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(2, 1, 8, 1, rle)]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(10, 20, 30)] * 2]
    # repeat raster (0xFE), a run of black (0x7F) and of white (0xFF) with the default colours
    rle = bytes([0x01, 5, 6, 0xFE, 0])
    d = wpg.parse(wpg2([wpg2_palette(5, [(1, 2, 3), (4, 5, 6)]), wpg2_bitmap(0, 0, 10, 10),
                        wpg2_bitmap_data(2, 2, 4, 1, rle)]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(1, 2, 3), (4, 5, 6)]] * 2
    # XOR: the next raster is xored with the one before it
    rle = bytes([0x01, 0x0F, 0xF0, 0x7E, 0x01, 0xFF, 0x00])
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(2, 2, 12, 1, rle)]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(15, 15, 15), (240, 240, 240)], [(240, 240, 240), (240, 240, 240)]], \
        png_pixels(bitmaps(d)[0].data)[2]
    # 1 bit, MSB first, the default palette: 0 is black and 1 is white
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(3, 1, 1, 0, b"\xa0")]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(255, 255, 255), (0, 0, 0), (255, 255, 255)]]
    # 16 bit grey, 5-5-5 RGB, RGBA
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1, 1, 5, 0, b"\x00\x80")]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(128, 128, 128)]]
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1, 1, 6, 0, struct.pack("<H", 0x7C00))]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(255, 0, 0)]]
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1, 1, 9, 0, bytes([1, 2, 3, 4]))]))
    assert png_pixels(bitmaps(d)[0].data)[2] == [[(1, 2, 3)]]
    # a format the figure cannot draw is dropped and counted
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1, 1, 13, 0, b"\0\0")]))
    assert not bitmaps(d) and d.skipped >= 1


def test_wpg2_object_image():
    png = images._png(1, 1, [bytes([1, 2, 3])], False)
    desc = bytes([0x25]) + bytes(7)
    cap = wpg2_rec(0x21, wpg2_char() + wpg2_pt4(10, 10, 110, 60) + struct.pack("<H", 1) + desc)
    img = wpg2_rec(0x12, struct.pack("<H", 0) + png)
    d = wpg.parse(wpg2([cap, img]))
    bm = bitmaps(d)
    assert len(bm) == 1 and bm[0].kind == "png" and bm[0].data == png and bm[0].box == (10, 1140, 110, 1190), bm
    # another format is skipped and counted
    cap = wpg2_rec(0x21, wpg2_char() + wpg2_pt4(10, 10, 110, 60) + struct.pack("<H", 1) + bytes([0x0C]) + bytes(7))
    d = wpg.parse(wpg2([cap, img]))
    assert not bitmaps(d) and d.skipped == 1


def test_wpg2_pen_and_size():
    d = wpg.parse(wpg2([wpg2_pen_size(24), wpg2_polyline([(0, 0), (5, 5)])]))
    assert paths(d)[0].pen.width == 24
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (5, 5)])]))
    assert 0 < paths(d)[0].pen.width <= 20           # a hairline
    # pen and brush records inside the page attributes (the Start WPG group) are not applied
    d = wpg.parse(wpg2([wpg2_pen(0x010203), wpg2_polyline([(0, 0), (5, 5)])], start_ext=1))
    assert paths(d)[0].pen.color == 0
    d = wpg.parse(wpg2([wpg2_pen(0x010203), wpg2_pen(0x040506), wpg2_polyline([(0, 0), (5, 5)])], start_ext=1))
    assert paths(d)[0].pen.color == 0x040506


# ---- every format -----------------------------------------------------------------------------------------------------

def test_wpg_postscript_skipped():
    ps1 = wpg1_rec(0x11, struct.pack("<hhhh", 0, 100, 100, 0) + b"%!PS-Adobe")
    ps2 = wpg1_rec(0x1B, struct.pack("<IhhhhH", 10, 0, 0, 100, 100, 0) + bytes(48) + b"%!PS")
    d = wpg.parse(wpg1([ps1, ps2, wpg1_rec(0x55, b"abc"), wpg1_line(0, 0, 5, 5), wpg1_rec(0x04, b"")]))
    assert d.skipped == 4 and len(paths(d)) == 1 and not bitmaps(d) and not d.damaged
    # WPG 2: an unknown record, and the records a figure has no use for
    d = wpg.parse(wpg2([wpg2_rec(0x3A, b"xx"), wpg2_rec(0x0B, b""), wpg2_polyline([(0, 0), (9, 9)])]))
    assert d.skipped == 2 and len(paths(d)) == 1
    # the records after a skipped one are read
    d = wpg.parse(wpg1([ps1, wpg1_text(0, 600, b"after")]))
    assert text_lines(d)[0] == ["after"]


def test_wpg_truncated():
    full = wpg1([wpg1_line(0, 0, 10, 10), wpg1_text(5, 600, b"ok"), wpg1_line(0, 0, 20, 20)])
    cut = full[:-8]                               # the End record and part of the last line
    d = wpg.parse(cut)
    assert d.damaged and len(paths(d)) == 1 and text_lines(d)[0] == ["ok"]
    assert not wpg.parse(full).damaged
    full2 = wpg2([wpg2_polyline([(0, 0), (9, 9)]), wpg2_textline(0, 200), wpg2_textdata(b"t"),
                  wpg2_polyline([(0, 0), (8, 8)])])
    d = wpg.parse(full2[:-12])
    assert d.damaged and len(paths(d)) == 1 and text_lines(d)[0] == ["t"], d
    # cut inside the Start WPG record: nothing was drawn, no picture
    for blob in (wpg1([])[:20], wpg2([])[:22]):
        try:
            wpg.parse(blob)
        except ReadError as e:
            assert e.kind == "damaged"
        else:
            raise AssertionError("no ReadError")
    # a record that claims more than the file has
    big = wpg1([wpg1_line(0, 0, 1, 1)])[:-2] + b"\x05\xff\xff\xff\xff\xff"
    d = wpg.parse(big)
    assert d.damaged and len(paths(d)) == 1
    # a record too short for its fields is damaged, and the next one is still read
    short = wpg1_rec(0x05, b"\x01\x00")
    d = wpg.parse(wpg1([short, wpg1_line(0, 0, 3, 3)]))
    assert d.damaged and len(paths(d)) == 1


def test_wpg_frame_size():
    d = wpg.parse(wpg1([], size=(2400, 1200)))
    assert d.frame == (0, 0, 2400, 1200) and d.dpi == 1200 and d.size_px() == (192, 96)
    d = wpg.parse(wpg2([], units=(600, 600), size=(600, 300)))
    assert d.frame == (0, 0, 600, 300) and d.dpi == 600 and d.size_px() == (96, 48)
    d = wpg.parse(wpg2([], units=(1200, 1200), size=(1200, 600), dp=True))
    assert d.frame == (0, 0, 1200, 600) and d.size_px() == (96, 48)
    # a unit of zero is 1200 per inch
    d = wpg.parse(wpg2([], units=(0, 0), size=(1200, 600)))
    assert d.dpi == 1200 and d.size_px() == (96, 48)
    # different horizontal and vertical units: y is scaled to the horizontal one
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (0, 100)])], units=(1200, 600), size=(1200, 600)))
    assert d.frame == (0, 0, 1200, 1200) and d.size_px() == (96, 96)
    assert pts(paths(d)[0]) == [(0.0, 1200.0), (0.0, 1000.0)], pts(paths(d)[0])
    # a zero size is a pixel
    assert wpg.parse(wpg1([], size=(0, 0))).size_px() == (1, 1)


def test_wpg_head():
    inner = wpg1([wpg1_line(0, 0, 4, 4)])
    # libwpg's own mess: a head, and at its document pointer another head (the pointer there counts from 16)
    nested = wpg_head(1, doc=16) + inner
    d = wpg.parse(nested)
    assert len(paths(d)) == 1
    # the head's document pointer
    odd = wpg_head(1, doc=20) + b"\0\0\0\0" + inner[16:]
    assert len(paths(wpg.parse(odd))) == 1
    bad = [b"", b"\xffWPC", inner[16:], b"\xffWPD" + inner[4:],
           inner[:9] + b"\x01" + inner[10:],             # file type 1
           inner[:10] + b"\x03" + inner[11:],            # version 3
           inner[:12] + b"\x01\x00" + inner[14:]]        # encrypted
    for blob in bad:
        try:
            wpg.parse(blob)
        except ReadError as e:
            assert e.kind in ("damaged", "encrypted"), e.kind
        else:
            raise AssertionError("no ReadError")
    # a document pointer past the file
    try:
        wpg.parse(wpg_head(1, doc=5000) + inner[16:])
    except ReadError:
        pass
    else:
        raise AssertionError("no ReadError")
    # a second Start WPG ends the picture
    two = wpg1([wpg1_line(0, 0, 4, 4), wpg1_rec(0x0F, struct.pack("<BBHH", 1, 0, 5, 5)), wpg1_line(0, 0, 9, 9)])
    assert len(paths(wpg.parse(two))) == 1
    # a precision that is not 0 or 1
    blob = bytearray(wpg2([]))
    blob[16 + 4 + 4] = 7
    try:
        wpg.parse(bytes(blob))
    except ReadError as e:
        assert e.kind == "damaged"
    else:
        raise AssertionError("no ReadError")


def test_wpg_varint():
    # a length in the two-byte and four-byte forms is read
    for n in (254, 255, 300, 0x7FFF, 0x8000, 70000):
        data = bytes(n)
        rec = wpg1_rec(0x55, data)
        assert rec[1:1 + len(wpg_varint(n))] == wpg_varint(n)
        d = wpg.parse(wpg1([rec, wpg1_line(0, 0, 1, 1)]))
        assert d.skipped == 1 and len(paths(d)) == 1 and not d.damaged, n
    # a length of four billion is past the end
    d = wpg.parse(wpg1([wpg1_line(0, 0, 1, 1)])[:-2] + b"\x05\xff\xff\xff\xff\xff")
    assert d.damaged
    # a variable-length integer cut by the end
    d = wpg.parse(wpg1([wpg1_line(0, 0, 1, 1)])[:-2] + b"\x05\xff\xff")
    assert d.damaged and len(paths(d)) == 1


def test_wpg_counts_are_clamped():
    d = wpg.parse(wpg1([wpg1_poly(0x06, [(0, 0), (5, 5), (9, 9)], count=60000)]))
    assert len(pts(paths(d)[0])) == 3 and not d.damaged
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (5, 5), (9, 9)], count=60000)]))
    assert len(pts(paths(d)[0])) == 3
    d = wpg.parse(wpg2([wpg2_rec(0x17, wpg2_char() + struct.pack("<H", 9999) + wpg2_pt4(0, 0, 1, 1) + wpg2_pt4(2, 2, 0, 0))]))
    assert not d.damaged and len(paths(d)) == 1 and len(paths(d)[0].segs) == 1


def test_wpg_bitmap_hostile():
    import tracemalloc
    # a bitmap past the pixel cap is stepped over without allocating
    huge = wpg1_bitmap1(30000, 30000, 8, bytes([0x84, 1]))
    old = images._MAX_PIXELS
    t0 = time.time()
    tracemalloc.start()
    d = wpg.parse(wpg1([huge, wpg1_line(0, 0, 1, 1)]))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert not bitmaps(d) and d.skipped == 1 and len(paths(d)) == 1 and peak < 10 * 2**20, peak
    # WPG 2: the same, for plain data and for RLE
    for comp, data in ((0, b"\x00" * 4), (1, bytes([0x7F, 255]))):
        tracemalloc.start()
        d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(60000, 60000, 4, comp, data)]))
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert not bitmaps(d) and peak < 10 * 2**20, (comp, peak)
    # the pixel cap itself, lowered
    images._MAX_PIXELS = 10
    try:
        d = wpg.parse(wpg1([wpg1_bitmap1(4, 4, 8, bytes([0x84, 1])), wpg1_text(0, 600, b"x")]))
    finally:
        images._MAX_PIXELS = old
    assert not bitmaps(d) and d.skipped == 1 and text_lines(d)[0] == ["x"]
    # a data size of 255 in an RLE does not grow the buffer past the format's size
    tracemalloc.start()
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(100, 100, 4, 1, bytes([0x7D, 255, 0xFF, 255]) * 50)]))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 20 * 2**20 and time.time() - t0 < 20, peak


def test_wpg_tall_narrow_bitmap_is_bounded_by_its_pixels():
    import tracemalloc
    one = wpg1([wpg1_bitmap1(1, 30000, 8, bytes([0x80 | 100, 1]) * 300)])
    two = wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1, 30000, 4, 0, bytes(30000))])
    for blob in (one, two):
        t0 = time.time()
        d = wpg.parse(blob)
        took = time.time() - t0
        assert len(bitmaps(d)) == 1 and not d.damaged and took < 3, took
        tracemalloc.start()
        wpg.parse(blob)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak < 40 * 2**20, peak
        # the deadline is read while the rows are built
        try:
            wpg.parse(blob, deadline=time.monotonic() - 1)
        except ReadError as e:
            assert e.kind == "too_large"
        else:
            raise AssertionError("no ReadError")


def _png_size(png):
    return struct.unpack(">II", png[16:24])


def test_wpg_heavily_compressed_blank_bitmaps_are_kept():
    # a blank 1700x2200 1-bit WPG 1 page: 213 bytes a row, runs of 255 bytes
    need = 213 * 2200
    rle = bytes([0x80, 255]) * (need // 255) + bytes([0x80, need % 255])
    d = wpg.parse(wpg1([wpg1_bitmap1(1700, 2200, 1, rle)]))
    assert len(bitmaps(d)) == 1 and d.skipped == 0 and _png_size(bitmaps(d)[0].data) == (1700, 2200)
    # a blank 2000x2000 8-bit WPG 2 bitmap: one raster of runs, then the raster repeated
    rle = bytes([0xFF, 255]) * 7 + bytes([0xFF, 207]) + bytes([0xFE, 255]) * 7 + bytes([0xFE, 242])
    d = wpg.parse(wpg2([wpg2_bitmap(0, 0, 100, 100), wpg2_bitmap_data(2000, 2000, 4, 1, rle)]))
    assert len(bitmaps(d)) == 1 and d.skipped == 0 and _png_size(bitmaps(d)[0].data) == (2000, 2000)


def test_wpg_rle_bombs_stay_bounded():
    import tracemalloc
    old = images._MAX_PIXELS
    images._MAX_PIXELS = 1_000_000
    try:
        blobs = [wpg1([wpg1_bitmap1(1000, 1000, 8, bytes([0x80, 255]) * 3922)]),
                 # one byte a step and no output: 0x7E only sets a flag
                 wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1000, 1000, 4, 1, bytes([0x7E]) * 500_000)]),
                 # three bytes of data for a million pixels
                 wpg2([wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(1000, 1000, 4, 1, bytes([0xFF, 255, 0x7F]))])]
        for blob in blobs:
            t0 = time.time()
            wpg.parse(blob)
            took = time.time() - t0
            assert took < 1, took
            tracemalloc.start()
            wpg.parse(blob)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            assert peak < 50 * 2**20, peak
    finally:
        images._MAX_PIXELS = old


def test_wpg2_mirrored_bitmaps():
    pal = wpg2_palette(0, [(255, 0, 0), (0, 255, 0)])
    red, green = (255, 0, 0), (0, 255, 0)
    data = b"\x00\x01\x01\x01"
    plain = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50), wpg2_bitmap_data(2, 2, 4, 0, data)]))
    assert png_pixels(bitmaps(plain)[0].data)[2] == [[red, green], [green, green]]
    h = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50, scale=(-1.0, 1.0)), wpg2_bitmap_data(2, 2, 4, 0, data)]))
    assert png_pixels(bitmaps(h)[0].data)[2] == [[green, red], [green, green]]
    v = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50, scale=(1.0, -1.0)), wpg2_bitmap_data(2, 2, 4, 0, data)]))
    assert png_pixels(bitmaps(v)[0].data)[2] == [[green, green], [red, green]]
    both = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50, scale=(-1.0, -1.0)), wpg2_bitmap_data(2, 2, 4, 0, data)]))
    assert png_pixels(bitmaps(both)[0].data)[2] == [[green, green], [green, red]]
    # the flip belongs to its bitmap record: the next, unmirrored one is as it was
    two = wpg.parse(wpg2([pal, wpg2_bitmap(0, 0, 100, 50, scale=(-1.0, 1.0)), wpg2_bitmap_data(2, 1, 4, 0, b"\x00\x01"),
                          wpg2_bitmap(0, 0, 100, 50), wpg2_bitmap_data(2, 1, 4, 0, b"\x00\x01")]))
    assert [png_pixels(b.data)[2][0] for b in bitmaps(two)] == [[green, red], [red, green]]


def test_wpg2_degenerate_matrix_is_identity_and_counted():
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (100, 0)], scale=(0.0, 1.0)), wpg2_polyline([(0, 0), (100, 0)])]))
    a, b = paths(d)
    assert pts(a) == pts(b) == [(0.0, 1200.0), (100.0, 1200.0)] and d.skipped == 1
    d = wpg.parse(wpg2([wpg2_polyline([(5, 5), (100, 5)], skew=(1.0, 1.0))]))     # [[1, 1], [1, 1]]: determinant 0
    assert d.skipped == 1 and pts(paths(d)[0])[0] == (5.0, 1195.0)
    d = wpg.parse(wpg2([wpg2_polyline([(0, 0), (100, 0)], scale=(2.0, 1.0))]))
    assert d.skipped == 0


def test_wpg2_png_object_size_is_checked():
    cap = wpg2_rec(0x21, wpg2_char() + wpg2_pt4(10, 10, 110, 60) + struct.pack("<H", 1) + bytes([0x25]) + bytes(7))

    def img(png):
        return wpg2_rec(0x12, struct.pack("<H", 0) + png)
    big = images._png(4, 4, [bytes(12)] * 4, False)
    old = images._MAX_PIXELS
    images._MAX_PIXELS = 10
    try:
        d = wpg.parse(wpg2([cap, img(big)]))
        assert not bitmaps(d) and d.skipped == 1
        d = wpg.parse(wpg2([cap, img(big[:20])]))           # a head cut before its IHDR
        assert not bitmaps(d) and d.skipped == 1
        small = images._png(3, 3, [bytes(9)] * 3, False)
        assert len(bitmaps(wpg.parse(wpg2([cap, img(small)])))) == 1
    finally:
        images._MAX_PIXELS = old


def test_wpg_group_depth_and_records_cap():
    # a deep chain of groups ends the parse and marks the drawing damaged
    recs = [wpg2_rec(0x15, wpg2_char() + struct.pack("<H", 0), ext=100)] * (limits.MAX_DEPTH + 10)
    d = wpg.parse(wpg2(recs))
    assert d.damaged
    # past a size cap the drawing read so far is kept, marked damaged
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 3
    try:
        d = wpg.parse(wpg1([wpg1_line(0, 0, 1, 1)] * 10))
    finally:
        limits.METAFILE_RECORDS = old
    assert d.damaged and 0 < len(d.items) < 10, (d.damaged, len(d.items))
    old = limits.METAFILE_POINTS
    limits.METAFILE_POINTS = 20
    try:
        d = wpg.parse(wpg1([wpg1_line(0, 0, 1, 1), wpg1_poly(0x06, [(i, i) for i in range(30)])]))
    finally:
        limits.METAFILE_POINTS = old
    assert d.damaged and len(d.items) == 1, (d.damaged, d.items)


def test_wpg_deadline():
    blob = wpg1([wpg1_line(0, 0, 1, 1)] * 5000)
    try:
        wpg.parse(blob, deadline=time.monotonic() - 1)
    except ReadError as e:
        assert e.kind == "too_large" and "time" in e.detail, e
    else:
        raise AssertionError("no ReadError")
    assert len(paths(wpg.parse(blob, deadline=time.monotonic() + 60))) == 5000


def test_wpg_item_weight_matches_metafile():
    assert wpg.ITEM_WEIGHT == metafile.ITEM_WEIGHT == pictmod.ITEM_WEIGHT
    # a run is charged ITEM_WEIGHT, kept or not
    old = limits.METAFILE_POINTS
    limits.METAFILE_POINTS = wpg.ITEM_WEIGHT * 2 - 1
    try:  # the second run passes the cap: the first is kept, the drawing marked damaged
        d = wpg.parse(wpg1([wpg1_text(0, 600, b"a"), wpg1_text(0, 600, b"b")]))
    finally:
        limits.METAFILE_POINTS = old
    assert d.damaged and [r.text for r in d.items if isinstance(r, Run)] == ["a"], d.items


def test_wpg_images_sniff():
    assert images.sniff(wpg1([wpg1_line(0, 0, 1, 1)])) == "wpg"


# ---- mutation ---------------------------------------------------------------------------------------------------------

def _assert_finite(d):
    assert all(map(math.isfinite, d.frame)) and math.isfinite(d.dpi)
    d.size_px()
    for it in d.items:
        if isinstance(it, VPath):
            assert all(math.isfinite(v) for s in it.segs for v in s[1:])
            assert it.pen is None or math.isfinite(it.pen.width)
        elif isinstance(it, Run):
            assert all(map(math.isfinite, (it.x, it.y, it.height, it.angle)))
        elif isinstance(it, Bitmap):
            assert all(map(math.isfinite, it.box))
        elif isinstance(it, Clip) and it.segs:
            assert all(math.isfinite(v) for s in it.segs for v in s[1:])


def _seeds():
    one = wpg1([wpg1_colormap(1, [(255, 0, 0), (0, 0, 255)]), wpg1_fill(1, 1), wpg1_line_attr(1, 4, 12),
                wpg1_line(0, 0, 100, 100), wpg1_poly(0x06, [(0, 0), (5, 5), (9, 1)]), wpg1_rect(1, 2, 30, 40),
                wpg1_poly(0x08, [(0, 0), (50, 0), (50, 50)]), wpg1_ellipse(600, 600, 100, 50, 30),
                wpg1_rec(0x13, struct.pack("<IH", 0, 4) + b"".join(struct.pack("<hh", i, i * 2) for i in range(4))),
                wpg1_text_attr(avg=100, size=200, align=1, color=4), wpg1_text(100, 1000, b"Hello"),
                wpg1_text2(10, 500, 90, 500, b"Hi \xc0\x27\x01\xc0"),
                wpg1_bitmap1(4, 2, 8, bytes([0x84, 1, 4, 2, 2, 2, 2])), wpg1_bitmap1(8, 2, 1, bytes([1, 0x80, 0, 1])),
                wpg1_bitmap2(100, 500, 300, 300, 2, 2, 4, bytes([1, 0x12, 1, 0x21])),
                wpg1_rec(0x11, struct.pack("<hhhh", 0, 100, 100, 0) + b"%!PS")])
    two = wpg2([wpg2_pen(0xFF0000), wpg2_pen_size(12), wpg2_brush(0x00FF00),
                wpg2_palette(0, [(255, 0, 0), (0, 255, 0)]),
                wpg2_polyline([(0, 0), (100, 100), (200, 0)], filled=True, closed=True, move=(5, 5)),
                wpg2_polyline([(0, 0), (100, 100), (200, 0)], rotate=30),
                wpg2_rect(100, 100, 300, 200, rx=10, ry=10), wpg2_arc(500, 500, 100, 50, 100, 0, 0, 50),
                wpg2_polycurve([((0, 0), (0, 0), (10, 0)), ((20, 10), (30, 10), (40, 10))]),
                wpg2_rec(0x1A, wpg2_char(filled=True, closed=True), ext=2),
                wpg2_polyline([(0, 0), (100, 0), (100, 100)]), wpg2_polyline([(20, 20), (60, 20), (60, 60)]),
                wpg2_textline(100, 200, angle=15), wpg2_textdata(b"Hi\xf0\x27\x01\xf0\xb4B\x80C"),
                wpg2_textblock(100, 400, 500, 100), wpg2_textdata(b"Block"),
                wpg2_bitmap(0, 0, 100, 50), wpg2_bitmap_data(2, 1, 4, 0, b"\x00\x01"),
                wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(2, 2, 8, 1, bytes([0x7D, 3, 0x81, 10, 20, 30, 0xFE, 0])),
                wpg2_bitmap(0, 0, 10, 10), wpg2_bitmap_data(2, 2, 12, 1, bytes([1, 15, 240, 0x7E, 1, 255, 0]))])
    dp = wpg2([wpg2_polyline([(10, 10), (50, 20), (90, 90)], dp=True), wpg2_rect(1, 2, 30, 40, dp=True),
               wpg2_arc(50, 50, 20, 20, 20, 0, 0, 20, dp=True), wpg2_textline(5, 5, dp=True), wpg2_textdata(b"dp"),
               wpg2_rec(0x26, struct.pack("<HHHH", 0xFF00, 0, 0x8000, 0))], dp=True)
    return [one, two, dp]


def test_wpg_mutated_never_raises():
    import random
    seeds = _seeds()
    for s in seeds:
        d = wpg.parse(s)
        assert not d.damaged, s
        _assert_finite(d)
    rnd = random.Random(7)
    t0 = time.time()
    for n in range(1500):
        b = bytearray(seeds[n % len(seeds)])
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(16, len(b))] = rnd.randrange(256)
        if n % 5 == 0:
            del b[rnd.randrange(16, len(b)):]
        try:
            d = wpg.parse(bytes(b))
        except ReadError:
            continue
        _assert_finite(d)
    assert time.time() - t0 < 20


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
