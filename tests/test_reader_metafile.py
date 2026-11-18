"""WMF parsing: text and its code pages, the frame and mapping modes, shapes, the DC stack and hostile input.

Run: python -I -X utf8 tests/test_reader_metafile.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import (wmf, wmf_bitblt, wmf_bitmap16, wmf_stretchblt, wmf_brush, wmf_exttext, wmf_font, wmf_polygon, wmf_rec, wmf_rect,  # noqa: E402
                          wmf_select, wmf_text, wmf_window)
from metafile_fix import (PLUS_VERSION, emf, emf_brush, emf_dib, emf_font, emf_glyphs, emf_modify_world, emf_pen,  # noqa: E402
                          emf_poly, emf_polytextw, emf_rec, emf_rect, emf_smalltext, emf_texta, emf_textw, emf_world,
                          emfplus, plus_draw_image, plus_driver, plus_fill_rect, plus_font, plus_getdc, plus_header,
                          plus_image_bitmap, plus_image_metafile, plus_object, plus_rec, plus_solid, plus_string,
                          plus_world)
from reader import limits, metafile  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.vector import Bitmap, Clip, Path as VPath, Run, text_lines  # noqa: E402


def lines(d):
    return text_lines(d)[0]


def runs(d):
    return [i for i in d.items if isinstance(i, Run)]


def test_wmf_textout_text_and_frame():
    d = metafile.parse(wmf([wmf_text(100, 200, b"Hello")]))
    assert lines(d) == ["Hello"]
    assert d.size_px() == (96, 96)
    assert not d.damaged


def test_wmf_exttextout_with_dx():
    d = metafile.parse(wmf([wmf_font(-100), wmf_select(0), wmf_exttext(100, 200, b"AB", dx=[50, 60])]))
    (r,) = runs(d)
    assert r.text == "AB" and r.width == 110 and r.x == 100
    assert r.height == 100
    assert lines(d) == ["AB"]
    g = metafile.parse(wmf([wmf_exttext(0, 0, b"\x01\x00\x02\x00", options=0x10)]))
    assert runs(g)[0].text == "" and runs(g)[0].glyphs == (1, 2)


def _font_text(charset, raw, face="Arial", codepage=""):
    return metafile.parse(wmf([wmf_font(-100, charset, face), wmf_select(0), wmf_text(0, 100, raw)]), codepage)


def test_wmf_charset_cp949():
    assert lines(_font_text(129, "한글".encode("cp949"))) == ["한글"]


def test_wmf_charset_shiftjis():
    assert lines(_font_text(128, "日本語".encode("cp932"))) == ["日本語"]


def test_wmf_charset_ansi():
    assert lines(_font_text(0, "café".encode("cp1252"))) == ["café"]
    assert metafile.CHARSETS[129] == "cp949" and metafile.CHARSETS[130] == "cp1361"
    assert lines(_font_text(130, "한".encode("johab"))) == ["한"]


def test_wmf_default_charset_uses_codepage():
    raw = "한글".encode("cp949")
    assert lines(_font_text(1, raw, codepage="cp949")) == ["한글"]
    assert lines(_font_text(1, raw, codepage="")) == [raw.decode("cp1252")]


def test_wmf_symbol_font():
    assert lines(_font_text(2, b"a", face="Symbol")) == ["α"]
    assert lines(_font_text(0, b"a", face="Symbol")) == ["α"]


def test_wmf_wingdings_dropped():
    d = _font_text(2, b"abc", face="Wingdings")
    assert lines(d) == [] and d.skipped > 0


def test_wmf_negative_extent_keeps_reading_order():
    recs = wmf_window((0, 1000), (1000, -1000)) + [wmf_text(0, 100, b"bottom"), wmf_text(0, 900, b"top")]
    d = metafile.parse(wmf(recs, placeable=(0, 0, 1000, 1000, 1000)))
    assert lines(d) == ["top", "bottom"]


def test_wmf_anisotropic_viewport_scale():
    recs = wmf_window((0, 0), (1000, 1000)) + [wmf_rec(0x020E, 250, 500), wmf_rect(0, 0, 1000, 1000)]
    d = metafile.parse(wmf(recs, placeable=None))
    assert d.size_px() == (500, 250)
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    assert p.segs[2][1:] == (500, 250)


def test_wmf_no_placeable_uses_window():
    d = metafile.parse(wmf(wmf_window((0, 0), (200, 100)) + [wmf_text(10, 10, b"x")], placeable=None))
    assert d.size_px() == (200, 100)
    # no window either: the extent of what was drawn
    d = metafile.parse(wmf([wmf_rect(10, 20, 110, 70)], placeable=None))
    assert d.frame == (10, 20, 110, 70)


def test_wmf_rect_and_polygon_become_paths():
    recs = [wmf_brush(0xFF0000), wmf_select(0), wmf_rect(10, 20, 110, 70), wmf_polygon([(0, 0), (100, 0), (50, 100)])]
    d = metafile.parse(wmf(recs))
    rect, poly = [i for i in d.items if isinstance(i, VPath)]
    assert [s[0] for s in rect.segs] == ["M", "L", "L", "L", "Z"]
    assert rect.segs[0][1:] == (10, 20) and rect.segs[2][1:] == (110, 70)
    assert rect.brush.color == 0xFF0000 and rect.pen is not None
    assert [s[0] for s in poly.segs] == ["M", "L", "L", "Z"] and poly.even_odd


def test_wmf_save_restore_dc():
    recs = [wmf_font(-100), wmf_font(-300), wmf_select(0), wmf_rec(0x001E), wmf_select(1), wmf_text(0, 0, b"big"),
            wmf_rec(0x0127, -1), wmf_text(0, 500, b"small")]
    big, small = runs(metafile.parse(wmf(recs)))
    assert (big.height, small.height) == (300, 100)


def test_wmf_clip_and_restore():
    recs = [wmf_rec(0x001E), wmf_rec(0x0416, 50, 60, 10, 20), wmf_rec(0x0127, -1)]
    d = metafile.parse(wmf(recs))
    clips = [i for i in d.items if isinstance(i, Clip)]
    assert [c.segs is None for c in clips] == [False, True]
    assert clips[0].segs[0][1:] == (20, 10) and clips[0].segs[2][1:] == (60, 50)


def test_wmf_truncated_record_keeps_partial():
    data = wmf([wmf_text(0, 100, b"kept")])
    cut = data[:-6] + struct.pack("<IH", 5000, 0x0521)  # the EOF record becomes one whose size passes the end
    d = metafile.parse(cut)
    assert d.damaged and lines(d) == ["kept"]


def test_wmf_record_cap():
    # past the cap the drawing read so far is kept, marked damaged
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 2
    try:
        d = metafile.parse(wmf([wmf_text(0, 0, b"a"), wmf_text(0, 50, b"b"), wmf_text(0, 90, b"c")]))
    finally:
        limits.METAFILE_RECORDS = old
    assert d.damaged and "".join(lines(d)) == "ab", (d.damaged, lines(d))  # "c" is past the cap


def test_wmf_select_missing_object_ignored():
    d = metafile.parse(wmf([wmf_select(5), wmf_rec(0x01F0, 9), wmf_text(0, 100, b"ok")]))
    assert lines(d) == ["ok"] and not d.damaged


def test_wmf_object_slots_are_reused_and_capped():
    old = limits.METAFILE_OBJECTS
    limits.METAFILE_OBJECTS = 2
    try:
        recs = [wmf_font(-10), wmf_font(-20), wmf_font(-30), wmf_rec(0x01F0, 0), wmf_font(-40), wmf_select(0),
                wmf_text(0, 100, b"x")]
        d = metafile.parse(wmf(recs))
        assert runs(d)[0].height == 40 and d.skipped == 1
    finally:
        limits.METAFILE_OBJECTS = old


def test_wmf_text_align_baseline():
    top = runs(metafile.parse(wmf([wmf_font(-100), wmf_select(0), wmf_text(0, 500, b"a")])))[0]
    base = runs(metafile.parse(wmf([wmf_font(-100), wmf_select(0), wmf_rec(0x012E, 24), wmf_text(0, 500, b"a")])))[0]
    assert base.y == 500 and top.y > 500


def test_wmf_hostile_counts_are_checked():
    # a polygon that declares far more points than its record holds is skipped, the rest is read
    d = metafile.parse(wmf([wmf_rec(0x0324, 30000, 1, 2), wmf_text(0, 100, b"after")]))
    assert d.damaged and lines(d) == ["after"]


def test_wmf_arcs_and_round_rect():
    arc, rr, ell = [i for i in metafile.parse(wmf([wmf_rec(0x0817, 0, 100, 50, 0, 100, 100, 0, 0),
                                                   wmf_rec(0x061C, 20, 20, 100, 100, 0, 0),
                                                   wmf_rec(0x0418, 100, 100, 0, 0)])).items if isinstance(i, VPath)]
    assert arc.brush is None and arc.segs[0][0] == "M" and arc.segs[1][0] == "C"
    assert rr.segs[-1] == ("Z",) and ell.segs[0][1:] == (100.0, 50.0)


def test_wmf_dib_becomes_png_bitmap():
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    d = metafile.parse(wmf([wmf_rec(0x0F43, 0x0020, 0x00CC, 0, 1, 1, 0, 0, 10, 10, 40, 40, dib)]))
    (b,) = [i for i in d.items if isinstance(i, Bitmap)]
    assert b.kind == "png" and b.data[:4] == b"\x89PNG" and b.box == (40, 40, 50, 50)
    # a raster operation that is not a source copy is skipped and counted
    d = metafile.parse(wmf([wmf_rec(0x0F43, 0x0020, 0x0042, 0, 1, 1, 0, 0, 10, 10, 40, 40, dib)]))
    assert d.skipped == 1


def test_wmf_bitblt_bitmap16():
    one = wmf_bitmap16(8, 2, 1, b"\xff\x00\x00\x00")
    px24 = wmf_bitmap16(1, 1, 24, b"\x00\x00\xff\x00")
    px32 = wmf_bitmap16(1, 1, 32, b"\x00\x00\xff\x00")
    d = metafile.parse(wmf([wmf_bitblt(10, 20, 8, 2, bitmap=one), wmf_stretchblt(0, 0, 4, 4, bitmap=px24),
                            wmf_bitblt(5, 5, 1, 1, bitmap=px32)]))
    bs = [i for i in d.items if isinstance(i, Bitmap)]
    assert len(bs) == 3 and all(b.kind == "png" and b.data[:4] == b"\x89PNG" for b in bs)
    assert bs[0].box == (10, 20, 18, 22) and bs[1].box == (0, 0, 4, 4) and d.skipped == 0
    # another depth, a truncated bitmap and a raster operation that is not a source copy are skipped and counted
    d = metafile.parse(wmf([wmf_bitblt(0, 0, 8, 2, bitmap=wmf_bitmap16(8, 2, 8, b"\0" * 16)),
                            wmf_bitblt(0, 0, 8, 2, bitmap=wmf_bitmap16(8, 2, 1, b"\0\0")),
                            wmf_bitblt(0, 0, 8, 2, rop=0x00420066, bitmap=one)]))
    assert not [i for i in d.items if isinstance(i, Bitmap)] and d.skipped == 3


def test_wmf_bitblt_without_bitmap_is_a_pattern_fill():
    d = metafile.parse(wmf([wmf_brush(0x00FF00), wmf_select(0), wmf_bitblt(10, 20, 30, 40, rop=0x00F00021),
                            wmf_stretchblt(0, 0, 5, 5, rop=0x00F00021)]))
    p1, p2 = [i for i in d.items if isinstance(i, VPath)]
    assert p1.brush.color == 0x00FF00 and p1.pen is None and p1.segs[2][1:] == (40, 60) and d.skipped == 0
    assert p2.segs[2][1:] == (5, 5)
    d = metafile.parse(wmf([wmf_bitblt(10, 20, 30, 40, rop=0x00CC0020)]))
    assert not d.items and d.skipped == 1


def test_wmf_placeable_frame_ignores_the_viewport():
    recs = wmf_window((0, 0), (1000, 1000)) + [wmf_rec(0x020D, 7, 9), wmf_rec(0x020E, 300, 300),
                                               wmf_rec(0x0211, 5, 5), wmf_rec(0x0412, 1, 2, 1, 2),
                                               wmf_rect(0, 0, 1000, 1000)]
    d = metafile.parse(wmf(recs, placeable=(0, 0, 1000, 1000, 1000)))
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    assert p.segs[0][1:] == (0, 0) and p.segs[2][1:] == (1000, 1000)


def test_wmf_frame_keeps_a_lines_extent():
    d = metafile.parse(wmf([wmf_rec(0x0214, 50, 10), wmf_rec(0x0213, 50, 210)], placeable=None))
    assert d.frame == (10, 50, 210, 50)
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    xs = [v for s in p.segs for v in s[1::2]]
    assert d.frame[0] <= min(xs) and max(xs) <= d.frame[2] and p.segs[0][2] == d.frame[1]
    assert d.size_px() == (200, 1)


def test_wmf_window_maps_onto_the_placeable_box():
    box = (0, 0, 1000, 1000, 1000)
    vp = [wmf_rec(0x020D, 7, 9), wmf_rec(0x020E, 300, 300)]
    d = metafile.parse(wmf(wmf_window((0, 0), (2000, 2000)) + vp + [wmf_rect(0, 0, 2000, 2000)], placeable=box))
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    assert p.segs[0][1:] == (0, 0) and p.segs[2][1:] == (1000, 1000) and d.frame == (0, 0, 1000, 1000)
    d = metafile.parse(wmf(wmf_window((0, 2000), (2000, -2000)) + vp + [wmf_rect(0, 0, 2000, 2000)], placeable=box))
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    assert p.segs[0][1:] == (0, 1000) and p.segs[2][1:] == (1000, 0)


def test_wmf_never_emits_non_finite_values():
    recs = wmf_window((0, 0), (100, 100)) + [wmf_rec(0x0410, 1, 32767, 1, 32767)] * 100 + [
        wmf_font(-10), wmf_select(0), wmf_text(5, 5, b"t"), wmf_rect(0, 0, 10, 10)]
    d = metafile.parse(wmf(recs, placeable=None))
    _assert_finite(d)
    assert d.frame == (0, 0, 1, 1)


def test_wmf_ext_text_pdy_reads_two_values_a_character():
    d = metafile.parse(wmf([wmf_exttext(0, 100, b"AB", options=0x2000, dx=[10, 5, 20, -5])]))
    assert runs(d)[0].width == 30


def test_wmf_empty_text_is_not_a_run():
    d = metafile.parse(wmf([wmf_text(0, 0, b""), wmf_exttext(0, 0, b"", options=0x10)]))
    assert runs(d) == []


def _arc_mid(recs, placeable):
    d = metafile.parse(wmf(recs + [wmf_rec(0x0817, 100, 50, 0, 50, 100, 100, 0, 0)], placeable=placeable))
    (p,) = [i for i in d.items if isinstance(i, VPath)]
    return p.segs[1][5:7], p.segs[0][1:]  # the end of the first quarter, the start


def test_wmf_arc_keeps_its_logical_side_under_a_flip():
    # from the top centre to the bottom centre of a 100 box, counterclockwise: through the left side
    (mx, _my), _ = _arc_mid([], (0, 0, 1000, 1000, 1000))
    assert mx < 50
    flip = wmf_window((0, 1000), (1000, -1000))
    (mx, my), (sx, sy) = _arc_mid(flip, (0, 0, 1000, 1000, 1000))
    assert mx < 50 and my > 940 and sy > 990  # the box sits at the bottom of the frame, the arc still goes left


def _assert_finite(d):
    import math
    assert all(map(math.isfinite, d.frame))
    d.size_px()
    for it in d.items:
        if isinstance(it, VPath):
            assert all(math.isfinite(v) for s in it.segs for v in s[1:])
            assert it.pen is None or math.isfinite(it.pen.width)
        elif isinstance(it, Run):
            assert all(map(math.isfinite, (it.x, it.y, it.height)))
        elif isinstance(it, Bitmap):
            assert all(map(math.isfinite, it.box))


def test_wmf_mutated_records_never_raise():
    import random
    import time
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    seeds = [wmf([
        wmf_font(-100, 129), wmf_select(0), wmf_text(0, 100, b"ab"), wmf_exttext(1, 2, b"xy", dx=[1, 2]),
        wmf_polygon([(0, 0), (5, 5), (9, 0)]), wmf_rect(0, 0, 5, 5), wmf_rec(0x0817, 0, 100, 50, 0, 100, 100, 0, 0),
        wmf_rec(0x0F43, 0x0020, 0x00CC, 0, 1, 1, 0, 0, 10, 10, 40, 40, dib),
        wmf_rec(0x0B41, 0x0020, 0x00CC, 1, 1, 0, 0, 10, 10, 5, 5, dib),
        wmf_bitblt(1, 1, 8, 2, bitmap=wmf_bitmap16(8, 2, 1, b"\xff\x00\x00\x00")),
        wmf_stretchblt(1, 1, 1, 1, bitmap=wmf_bitmap16(1, 1, 24, b"\x00\x00\xff\x00")),
        wmf_bitblt(0, 0, 3, 3, rop=0x00F00021),
        wmf_rec(0x06FF, struct.pack("<HHIhhhhhhhh", 0, 6, 1, 0, 0, 0, 0, 0, 5, 5, 0)),
        wmf_rec(0x012C, 1), wmf_rec(0x0416, 50, 60, 10, 20), wmf_rec(0x0538, 2, 3, 3, *[1] * 12),
        wmf_rec(0x0410, 1, 2, 1, 2), wmf_rec(0x020F, 3, 4), wmf_rec(0x0412, 1, 2, 1, 2), wmf_rec(0x0211, 3, 4),
        wmf_rec(0x01F0, 0), wmf_rec(0x0213, 5, 5), wmf_rec(0x020E, 50, 50), wmf_rec(0x020C, 9, 9),
        wmf_rec(0x001E), wmf_rec(0x0127, -1)]),
        wmf(wmf_window((0, 1000), (1000, -1000)) + [wmf_text(0, 100, b"x"), wmf_rec(0x061C, 5, 5, 90, 90, 1, 1)],
            placeable=None)]
    rnd = random.Random(7)
    t0 = time.time()
    for n in range(1200):
        b = bytearray(seeds[n % len(seeds)])
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        try:
            d = metafile.parse(bytes(b))
        except ReadError:
            continue
        _assert_finite(d)
    assert time.time() - t0 < 5


# ---- EMF ---------------------------------------------------------------------------------------------------------------

K = 2540 / 96  # frame units (0.01 mm) per EMF+ pixel at the header's 96 dpi
BASELINE = emf_rec(22, struct.pack("<I", 24))  # SETTEXTALIGN TA_BASELINE


def paths(d):
    return [i for i in d.items if isinstance(i, VPath)]


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


def test_emf_exttextoutw():
    d = metafile.parse(emf([emf_font(-100), emf_textw(100, 200, "Hello", dx=[50] * 5)]))
    (r,) = runs(d)
    assert r.text == "Hello" and r.face == "Arial" and r.height == 100 and r.width == 250
    assert r.x == 100 and r.y == 280  # top aligned: the baseline lies 0.8 em below the reference point
    assert lines(d) == ["Hello"] and not d.damaged and d.skipped == 0
    (r,) = runs(metafile.parse(emf([emf_font(-100), BASELINE, emf_textw(100, 200, "Hi")])))
    assert (r.x, r.y) == (100, 200) and r.width is None
    # ETO_PDY: an x and a y advance a character, the x ones summed
    (r,) = runs(metafile.parse(emf([emf_textw(0, 0, "AB", options=0x2000, dx=[10, 5, 20, -5])])))
    assert r.width == 30


def test_emf_exttexta_charset():
    raw = "한글".encode("cp949")
    assert lines(metafile.parse(emf([emf_font(-100, charset=129), emf_texta(0, 100, raw)]))) == ["한글"]
    assert lines(metafile.parse(emf([emf_font(-100, charset=128), emf_texta(0, 100, "日本".encode("cp932"))]))) == ["日本"]
    assert lines(metafile.parse(emf([emf_font(-100, charset=0), emf_texta(0, 100, "café".encode("cp1252"))]))) == ["café"]
    assert lines(metafile.parse(emf([emf_font(-100, charset=1), emf_texta(0, 100, raw)]), "cp949")) == ["한글"]
    # wide text: Symbol maps its private-use codes, another symbol font is dropped and counted
    assert lines(metafile.parse(emf([emf_font(-100, "Symbol", 2), emf_textw(0, 100, "b")]))) == ["αβ"]
    d = metafile.parse(emf([emf_font(-100, "Wingdings", 2), emf_textw(0, 100, "")]))
    assert lines(d) == [] and d.skipped == 1


def test_emf_smalltextout():
    d = metafile.parse(emf([emf_font(-100, charset=0), emf_smalltext(10, 100, "café".encode("cp1252")),
                            emf_smalltext(10, 500, "Wide 한"), emf_smalltext(10, 900, "norect", options=0x100)]))
    assert lines(d) == ["café", "Wide 한", "norect"] and not d.damaged
    assert runs(d)[0].x == 10 and runs(d)[0].y == 180


def test_emf_polytextoutw_as_several():
    d = metafile.parse(emf([emf_font(-100), emf_polytextw([(10, 100, "One"), (10, 500, "Two")])]))
    assert [(r.text, r.x) for r in runs(d)] == [("One", 10), ("Two", 10)]
    assert lines(d) == ["One", "Two"] and not d.damaged


def test_emf_frame_from_rclframe():
    d = metafile.parse(emf([], frame=(0, 0, 2540, 2540)))
    assert d.frame == (0, 0, 2540, 2540) and d.dpi == 2540 and d.size_px() == (96, 96)
    assert metafile.parse(emf([], frame=(100, 200, 5180, 2740))).size_px() == (192, 96)


def test_emf_world_transform_flip():
    recs = [emf_world(1, 0, 0, -1, 0, 1000), emf_font(-100), emf_textw(0, 100, "bottom"), emf_textw(0, 900, "top")]
    d = metafile.parse(emf(recs, frame=(0, 0, 1000, 1000)))
    assert lines(d) == ["top", "bottom"]
    assert all(r.angle == 0 and r.height == 100 for r in runs(d))
    # a quarter turn: the baseline runs down the frame
    (r,) = runs(metafile.parse(emf([emf_world(0, 1, -1, 0, 500, 0), emf_font(-100), BASELINE, emf_textw(0, 0, "r")])))
    assert r.angle % 360 == 270 and close(r.height, 100) and (r.x, r.y) == (500, 0)


def test_emf_modify_world_transform():
    def x_of(recs):
        return runs(metafile.parse(emf([BASELINE] + recs + [emf_textw(10, 0, "a")])))[0].x
    t = emf_world(1, 0, 0, 1, 100, 0)
    assert x_of([t, emf_modify_world(2, 0, 0, 2, 0, 0, 2)]) == 120  # left multiply: the scale first
    assert x_of([t, emf_modify_world(2, 0, 0, 2, 0, 0, 3)]) == 220  # right multiply: the translation first
    assert x_of([t, emf_modify_world(5, 0, 0, 5, 0, 0, 1)]) == 10   # identity
    assert x_of([t, emf_modify_world(3, 0, 0, 3, 7, 0, 4)]) == 37   # set


def test_emf_path_bracket_fill():
    pt = lambda x, y: struct.pack("<ii", x, y)  # noqa: E731
    bounds = struct.pack("<4i", 0, 0, 0, 0)
    recs = [emf_brush(0xFF0000), emf_rec(59), emf_rec(27, pt(10, 10)), emf_rec(54, pt(110, 10)), emf_rec(54, pt(60, 90)),
            emf_rec(61), emf_rec(60), emf_rec(62, bounds)]
    (p,) = paths(metafile.parse(emf(recs)))
    assert [s[0] for s in p.segs] == ["M", "L", "L", "Z"] and p.segs[0][1:] == (10, 10)
    assert p.brush.color == 0xFF0000 and p.pen is None
    # stroke and fill of a rectangle and a polygon in one bracket; nothing inside the bracket draws on its own
    recs = [emf_rec(59), emf_rect(0, 0, 10, 10), emf_poly(3, [(0, 0), (5, 5), (9, 0)]), emf_rec(60), emf_rec(63, bounds)]
    (p,) = paths(metafile.parse(emf(recs)))
    assert p.pen is not None and p.brush is not None and [s[0] for s in p.segs].count("M") == 2
    # an aborted path draws nothing; a stroked one has no brush
    assert paths(metafile.parse(emf([emf_rec(59), emf_rect(0, 0, 9, 9), emf_rec(60), emf_rec(68), emf_rec(62, bounds)]))) == []
    (p,) = paths(metafile.parse(emf([emf_rec(59), emf_rect(0, 0, 9, 9), emf_rec(60), emf_rec(64, bounds)])))
    assert p.brush is None and p.pen is not None


def test_emf_polybezier16():
    d = metafile.parse(emf([emf_poly(85, [(0, 0), (10, 20), (30, 20), (40, 0)], wide=False),
                            emf_poly(86, [(0, 0), (5, 5), (9, 0)], wide=False)]))
    bez, poly = paths(d)
    assert [s[0] for s in bez.segs] == ["M", "C"] and bez.segs[1][1:] == (10, 20, 30, 20, 40, 0) and bez.brush is None
    assert [s[0] for s in poly.segs] == ["M", "L", "L", "Z"] and poly.brush is not None
    # POLYBEZIERTO16 starts at the current position; the 32-bit POLYBEZIER reads the same way
    d = metafile.parse(emf([emf_rec(27, struct.pack("<ii", 5, 5)), emf_poly(88, [(10, 20), (30, 20), (40, 0)], wide=False),
                            emf_poly(2, [(0, 0), (1, 2), (3, 2), (4, 0)])]))
    a, b = paths(d)
    assert a.segs == [("M", 5, 5), ("C", 10, 20, 30, 20, 40, 0)] and b.segs[1][1:] == (1, 2, 3, 2, 4, 0)


def test_emf_glyph_index_run():
    d = metafile.parse(emf([emf_glyphs(10, 100, [36, 37, 38], "Calibri")]))
    (r,) = runs(d)
    assert r.text == "" and r.glyphs == (36, 37, 38) and r.face == "Calibri"
    assert lines(d) == []


def test_emf_nan_transform_identity():
    nan, inf = float("nan"), float("inf")
    recs = [emf_font(-100), BASELINE, emf_world(nan, 0, 0, 1, 0, 0), emf_textw(100, 200, "a"),
            emf_world(0, 0, 0, 0, 5, 5), emf_textw(100, 600, "b"),
            emf_world(1, 0, 0, 1, 0, 0), emf_modify_world(inf, 0, 0, 1, 0, 0, 2), emf_textw(100, 1000, "c")]
    d = metafile.parse(emf(recs))
    assert [(r.x, r.y) for r in runs(d)] == [(100, 200), (100, 600), (100, 1000)]
    _assert_finite(d)
    d = metafile.parse(emf([emfplus([plus_header(False), plus_world(nan, 0, 0, 1, 0, 0), plus_font(0, 12, "Arial"),
                                     plus_driver(0, "x", x=96, y=96)])]))
    (r,) = runs(d)
    assert close(r.x, 96 * K) and close(r.y, 96 * K)


def test_emf_bitmap_stretchdibits():
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    d = metafile.parse(emf([emf_dib(40, 40, 10, 10, dib)]))
    (b,) = [i for i in d.items if isinstance(i, Bitmap)]
    assert b.kind == "png" and b.data[:4] == b"\x89PNG" and b.box == (40, 40, 50, 50)
    d = metafile.parse(emf([emf_dib(40, 40, 10, 10, dib, rop=0x00420066)]))
    assert d.skipped == 1 and not d.items


def test_emf_truncated_record_keeps_partial():
    data = emf([emf_font(-100), emf_textw(0, 100, "kept")])
    d = metafile.parse(data[:-20] + struct.pack("<II", 84, 5000))  # the EOF record becomes one that passes the end
    assert d.damaged and lines(d) == ["kept"]
    # a header that does not fit is not an EMF
    try:
        metafile.parse(data[:44])
    except ReadError as e:
        assert e.kind == "damaged"
    else:
        raise AssertionError("no ReadError")


def test_emf_stock_objects_and_pens():
    recs = [emf_pen(0x0000FF, 40), emf_brush(0x00FF00), emf_rect(0, 0, 10, 10), emf_rec(37, struct.pack("<I", 0x80000005)),
            emf_rec(37, struct.pack("<I", 0x80000008)), emf_rect(0, 0, 10, 10), emf_rec(37, struct.pack("<I", 0x80000004)),
            emf_rect(0, 0, 10, 10), emf_rec(37, struct.pack("<I", 0x80001234)), emf_rec(37, struct.pack("<I", 77))]
    d = metafile.parse(emf(recs))
    a, b = paths(d)
    assert a.pen.color == 0x0000FF and a.pen.width == 40 and a.brush.color == 0x00FF00
    assert b.pen is None and b.brush.color == 0 and not d.damaged


# ---- EMF+ ---------------------------------------------------------------------------------------------------------------

def test_emfplus_drawstring():
    d = metafile.parse(emf([emfplus([plus_header(dual=False), plus_font(0, 12, "Arial"), plus_string(0, 96, 96, "Hello")])]))
    (r,) = runs(d)
    assert r.text == "Hello" and r.face == "Arial" and r.angle == 0
    assert close(r.height, 12 * K) and close(r.x, 96 * K) and close(r.y, (96 + 0.8 * 12) * K)
    assert lines(d) == ["Hello"] and not d.damaged and d.skipped == 0
    # a centred format in a layout box, two lines, under a world scale
    fmt = plus_object(1, 7, struct.pack("<IIIII", PLUS_VERSION, 0, 0, 1, 0) + b"\0" * 40)
    d = metafile.parse(emf([emfplus([plus_header(False), plus_font(0, 10, "Arial"), fmt, plus_world(2, 0, 0, 2, 0, 0),
                                     plus_string(0, 0, 0, "One\r\nTwo", w=200, h=100, fmt=1)])]))
    one, two = runs(d)
    assert lines(d) == ["One", "Two"] and close(one.height, 20 * K) and two.y > one.y
    assert close(one.x, (100 - 0.5 * 3 * 10 / 2) * 2 * K)  # the estimated width, centred in the box


def test_emfplus_page_units():
    def x_of(head, unit, scale=1.0):
        page = plus_rec(0x4030, unit, struct.pack("<f", scale))
        d = metafile.parse(emf([emfplus([head, plus_font(0, 12, "Arial"), page, plus_driver(0, "a", x=10, y=0)])]))
        return runs(d)[0].x
    assert close(x_of(plus_header(False), 2), 10 * K) and close(x_of(plus_header(False), 6, 2.0), 2000)  # pixels; 2 mm
    assert close(x_of(plus_header(False, dpi=192), 2), 10 * 2540 / 192) and close(x_of(plus_header(False), 4), 25400)
    # display units are pixels on a video reference device and hundredths of an inch on a printer
    assert close(x_of(plus_header(False), 1), 10 * K) and close(x_of(plus_header(False, video=False), 1), 254)


def test_emfplus_driverstring_cmap_and_glyphs():
    d = metafile.parse(emf([emfplus([plus_header(False), plus_font(0, 12, "Arial"),
                                     plus_driver(0, "Hi", x=10, y=50, advance=6),
                                     plus_driver(0, [43, 44], cmap=False, x=10, y=200)])]))
    a, b = runs(d)
    assert a.text == "Hi" and close(a.x, 10 * K) and close(a.y, 50 * K) and close(a.width, 12 * K)
    assert b.text == "" and b.glyphs == (43, 44) and b.face == "Arial"
    assert lines(d) == ["Hi"]
    # glyphs on two baselines in one record are two runs
    d = metafile.parse(emf([emfplus([plus_header(False), plus_font(0, 12, "Arial"),
                                     plus_driver(0, "ab", positions=[(0, 10), (0, 100)])])]))
    assert [r.text for r in runs(d)] == ["a", "b"] and lines(d) == ["a", "b"]


def test_dual_text_once():
    recs = [emfplus([plus_header(), plus_font(0, 12, "Arial"), plus_string(0, 10, 10, "Sales")]),
            emf_font(-317), emf_textw(1000, 1500, "Sales"), emf_rect(0, 0, 100, 100)]
    d = metafile.parse(emf(recs))
    assert lines(d) == ["Sales"] and len(runs(d)) == 1 and paths(d) == []


def test_dual_getdc_text_kept():
    head = [emfplus([plus_header(), plus_fill_rect(0, 0, 50, 50), plus_getdc()]), emf_font(-100), emf_textw(100, 100, "Q1")]
    d = metafile.parse(emf(head))
    assert lines(d) == ["Q1"] and len(paths(d)) == 1
    # the next EMF+ drawing record ends the hand-back; a state record does not
    d = metafile.parse(emf(head + [emfplus([plus_world(1, 0, 0, 1, 0, 0)]), emf_textw(100, 500, "Q2"),
                                   emfplus([plus_fill_rect(0, 0, 5, 5)]), emf_textw(100, 900, "Q3")]))
    assert lines(d) == ["Q1", "Q2"] and len(paths(d)) == 2


def test_dual_clip_follows_the_records_followed():
    def box(c):
        return c.segs[0][1:] + c.segs[2][1:]
    recs = [emfplus([plus_header(), plus_rec(0x4032, 0, struct.pack("<4f", 0, 0, 48, 48))]),
            emf_rec(30, struct.pack("<4i", 0, 0, 100, 100)),  # an EMF clip while EMF+ is followed: kept, not stated
            emfplus([plus_getdc()]), emf_rec(30, struct.pack("<4i", 0, 0, 50, 50)),
            emfplus([plus_fill_rect(0, 0, 1, 1)])]
    clips = [i for i in metafile.parse(emf(recs)).items if isinstance(i, Clip)]
    assert [box(c) for c in clips] == [(0, 0, 48 * K, 48 * K), (0, 0, 100, 100), (0, 0, 50, 50), (0, 0, 48 * K, 48 * K)]


def test_emfplus_only_file():
    recs = [emfplus([plus_header(dual=False), plus_fill_rect(10, 10, 20, 20, 0xFF00FF00), plus_font(0, 12, "Arial"),
                     plus_string(0, 10, 50, "Only")])]
    d = metafile.parse(emf(recs))
    assert lines(d) == ["Only"]
    (p,) = paths(d)
    assert p.brush.color == 0x00FF00 and p.pen is None and close(p.segs[0][1], 10 * K) and close(p.segs[2][2], 30 * K)
    # an EMF-only file follows its EMF records; EMF+ records with no EMF+ header are ignored
    assert lines(metafile.parse(emf([emf_font(-100), emf_textw(0, 100, "plain")]))) == ["plain"]
    d = metafile.parse(emf([emfplus([plus_font(0, 12, "Arial"), plus_string(0, 10, 10, "ghost")]),
                            emf_textw(0, 100, "gdi")]))
    assert lines(d) == ["gdi"]


def test_emfplus_container_depth_capped():
    n = 200
    begin = [plus_rec(0x4028, 0, struct.pack("<I", i)) for i in range(n)]
    end = [plus_rec(0x4029, 0, struct.pack("<I", i)) for i in reversed(range(n))]
    d = metafile.parse(emf([emfplus([plus_header(False), plus_font(0, 12, "Arial")] + begin
                                    + [plus_string(0, 10, 10, "deep")] + end)]))
    assert lines(d) == ["deep"] and d.skipped == n - limits.MAX_DEPTH
    # a container maps its source rectangle onto its destination until it ends
    box = plus_rec(0x4027, 0, struct.pack("<8fI", 100, 100, 200, 200, 0, 0, 100, 100, 7))
    d = metafile.parse(emf([emfplus([plus_header(False), plus_font(0, 12, "Arial"), box, plus_driver(0, "a", x=10, y=10),
                                     plus_rec(0x4029, 0, struct.pack("<I", 7)), plus_driver(0, "b", x=10, y=10)])]))
    a, b = runs(d)
    assert close(a.x, 120 * K) and close(a.height, 24 * K) and close(b.x, 10 * K)


def _wrap(inner: bytes) -> bytes:
    return emf([emfplus([plus_header(False), plus_image_metafile(0, inner), plus_draw_image(0, 0, 0, 96, 96)])])


def test_emfplus_nested_metafile_capped():
    inner = emf([emf_font(-100), BASELINE, emf_textw(100, 300, "deep")])
    d = metafile.parse(_wrap(inner))
    (r,) = runs(d)
    assert lines(d) == ["deep"] and close(r.x, 100) and close(r.y, 300) and close(r.height, 100)
    x = inner
    for _ in range(limits.METAFILE_NESTING):
        x = _wrap(x)
    assert lines(metafile.parse(x)) == ["deep"]
    d = metafile.parse(_wrap(_wrap(x)))  # past the cap: skipped, not recursed into
    assert lines(d) == [] and d.skipped == 1
    # a destination with no area cannot hold the picture: skipped and counted
    flat = emf([emfplus([plus_header(False), plus_image_metafile(0, inner), plus_draw_image(0, 0, 0, 96, 0)])])
    d = metafile.parse(flat)
    assert lines(d) == [] and d.skipped == 1


def test_emfplus_bad_object_id_ignored():
    recs = [plus_header(False), plus_object(200, 6, b"\0" * 8), plus_font(0, 12, "Arial"),
            plus_string(70, 0, 0, "bad font"), plus_string(5, 0, 0, "missing font"),
            plus_rec(0x4015, 63, struct.pack("<I", 99)),  # DrawPath of a path that is not there, with no pen
            plus_rec(0x400A, 0, struct.pack("<II4f", 40, 1, 0, 0, 5, 5)),  # FillRects with a brush that is not there
            plus_rec(0x401A, 9, struct.pack("<Ii4f4f", 0, 2, 0, 0, 1, 1, 0, 0, 5, 5)),  # DrawImage of no image
            plus_string(0, 10, 10, "ok")]
    d = metafile.parse(emf([emfplus(recs)]))
    assert lines(d) == ["ok"] and not d.damaged and paths(d) == []


def test_emfplus_continued_object_joined():
    face = "Arial".encode("utf-16-le")
    font = struct.pack("<IfIiII", PLUS_VERSION, 12, 2, 0, 0, 5) + face
    a, b = font[:10], font[10:]
    d = metafile.parse(emf([emfplus([plus_header(False), plus_object(0, 6, a, cont=len(font)), plus_object(0, 6, b),
                                     plus_string(0, 10, 10, "joined")])]))
    assert lines(d) == ["joined"] and runs(d)[0].face == "Arial"
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = 16
    try:
        d = metafile.parse(emf([emfplus([plus_header(False), plus_object(0, 6, a, cont=len(font)), plus_object(0, 6, b),
                                         plus_string(0, 10, 10, "dropped")])]))
        assert lines(d) == [] and d.skipped >= 1
    finally:
        limits.IMAGE_BYTES = old


def test_emfplus_brushes_average_and_foreground():
    linear = plus_object(1, 1, struct.pack("<IIIi4fIIII", PLUS_VERSION, 4, 0, 0, 0, 0, 10, 10,
                                           0xFFFF0000, 0xFF0000FF, 0, 0))
    hatch = plus_object(2, 1, struct.pack("<IIIII", PLUS_VERSION, 1, 3, 0xFF00FF00, 0xFFFFFFFF))
    clear = plus_solid(3, 0x00FFFFFF)
    fills = [plus_rec(0x400A, i, struct.pack("<II4f", i, 1, 0, 0, 5, 5)) for i in (1, 2, 3)]
    d = metafile.parse(emf([emfplus([plus_header(False), linear, hatch, clear] + fills)]))
    assert [p.brush.color for p in paths(d)] == [0x7F007F, 0x00FF00]  # a fully transparent brush fills nothing


def test_emfplus_lines_curves_and_paths():
    pen = plus_object(0, 2, struct.pack("<IIIIf", PLUS_VERSION, 0, 0, 2, 1.0) + struct.pack("<III", PLUS_VERSION, 0, 0xFF0000FF))
    path = plus_object(1, 3, struct.pack("<IiI", PLUS_VERSION, 4, 0) + struct.pack("<8f", 0, 0, 10, 0, 10, 10, 0, 10)
                       + bytes((0, 1, 1, 0x81)))
    lines_ = plus_rec(0x400D, 0x2000, struct.pack("<I6f", 3, 0, 0, 10, 0, 10, 10))  # closed, pen 0
    curve = plus_rec(0x4018, 0, struct.pack("<fIII6f", 0.5, 0, 2, 3, 0, 0, 10, 10, 20, 0))
    fill_path = plus_rec(0x4014, 0x8001, struct.pack("<I", 0xFF00FF00))
    d = metafile.parse(emf([emfplus([plus_header(False), pen, path, lines_, curve, fill_path])]))
    ln, cv, fp = paths(d)
    assert ln.pen.color == 0x0000FF and [s[0] for s in ln.segs] == ["M", "L", "L", "Z"] and ln.brush is None
    assert [s[0] for s in cv.segs] == ["M", "C", "C"] and close(cv.segs[2][5], 20 * K)
    # the first control point leaves the first point along the first chord, a sixth of the way at tension 0.5
    assert close(cv.segs[1][1], 10 / 6 * K) and close(cv.segs[1][2], 10 / 6 * K)
    assert fp.brush.color == 0x00FF00 and [s[0] for s in fp.segs] == ["M", "L", "L", "L", "Z"]


def _kind_under_points_cap(cap, data):
    """"cut" when parsing `data` with METAFILE_POINTS lowered to `cap` stops at the cap (the drawing kept, marked
    damaged), else None."""
    old = limits.METAFILE_POINTS
    limits.METAFILE_POINTS = cap
    try:
        d = metafile.parse(data)
    finally:
        limits.METAFILE_POINTS = old
    return "cut" if d.damaged else None


def _place_many(inner: bytes, k: int) -> bytes:
    return emf([emfplus([plus_header(False), plus_image_metafile(0, inner)] + [plus_draw_image(0, 0, 0, 96, 96)] * k)])


def test_emfplus_redrawn_pictures_charged():
    # one rectangle, placed 10 times at each of 3 levels: the picture is parsed once a level, drawn 1000 times
    x = emf([emf_rect(0, 0, 10, 10)])
    for _ in range(3):
        x = _place_many(x, 10)
    d = metafile.parse(x)
    assert len(paths(d)) == 1000 and not d.damaged and d.skipped == 0
    assert _kind_under_points_cap(2000, x) == "cut"


def test_emfplus_path_object_drawn_many_times_charged():
    path = plus_object(1, 3, struct.pack("<IiI", PLUS_VERSION, 4, 0) + struct.pack("<8f", 0, 0, 10, 0, 10, 10, 0, 10)
                       + bytes((0, 1, 1, 0x81)))
    fill = plus_rec(0x4014, 0x8001, struct.pack("<I", 0xFF00FF00))  # FillPath of path 1
    clip = plus_rec(0x4033, 1 | 1 << 8)  # SetClipPath of path 1, intersect
    many = emf([emfplus([plus_header(False), path] + [fill] * 300)])
    assert len(paths(metafile.parse(many))) == 300
    assert _kind_under_points_cap(1000, many) == "cut"
    clips = emf([emfplus([plus_header(False), path] + [clip] * 300)])
    assert len([i for i in metafile.parse(clips).items if isinstance(i, Clip)]) == 1
    assert _kind_under_points_cap(1000, clips) == "cut"


def test_items_weighed_by_cost():
    # a Run or a Bitmap costs ITEM_WEIGHT points, a Clip its points but at least 4, a Path its points
    w = metafile.ITEM_WEIGHT
    assert w == 16
    three = emf([emf_font(-100), BASELINE] + [emf_textw(0, 100 * i, "t") for i in range(3)])
    assert len(runs(metafile.parse(three))) == 3
    assert _kind_under_points_cap(3 * w, three) is None and _kind_under_points_cap(3 * w - 1, three) == "cut"
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
    two = emf([emfplus([plus_header(False), plus_image_bitmap(0, png)] + [plus_draw_image(0, 0, 0, 96, 48)] * 2)])
    assert _kind_under_points_cap(2 * w, two) is None and _kind_under_points_cap(2 * w - 1, two) == "cut"
    # SAVEDC, a clip rectangle (5 points), RESTOREDC back to no clip (Clip(None): 4), then a rectangle (5 points)
    clipped = emf([emf_rec(33), emf_rec(30, struct.pack("<4i", 0, 0, 50, 50)), emf_rec(34, struct.pack("<i", -1)),
                   emf_rect(0, 0, 10, 10)])
    d = metafile.parse(clipped)
    assert [type(i).__name__ for i in d.items] == ["Clip", "Clip", "Path"] and d.items[1].segs is None
    assert _kind_under_points_cap(14, clipped) is None and _kind_under_points_cap(13, clipped) == "cut"


def _nested_runs(depth: int) -> bytes:
    x = emf([emf_font(-100), BASELINE, emf_textw(100, 300, "deep")])
    for _ in range(depth):
        x = _place_many(x, 10)
    return x


def test_nested_runs_stop_at_the_deadline():
    import time
    x = _nested_runs(7)  # 10**7 runs if nothing stopped it
    t = time.monotonic()
    try:
        metafile.parse(x, deadline=t + 0.2)
    except ReadError as e:
        assert e.kind == "too_large" and e.detail == "figure time", e
    else:
        raise AssertionError("no too_large")
    assert time.monotonic() - t < 2.0
    # a deadline that is not reached changes nothing
    d = metafile.parse(_nested_runs(1), deadline=time.monotonic() + 3600)
    assert [r.text for r in runs(d)] == ["deep"] * 10


def _padded_nest(pad: int, levels: int) -> bytes:
    """A picture with the text "deep" and `pad` bytes in a GDI comment that is not EMF+ (read past, never drawn),
    nested `levels` deep in EMF+ image objects, the outermost with its own text "outer" before its EMF+ records."""
    x = emf([emf_font(-100), BASELINE, emf_textw(100, 300, "deep"), emf_rec(70, struct.pack("<I", pad) + bytes(pad))])
    for _ in range(levels - 1):
        x = _wrap(x)
    return emf([emf_font(-100), BASELINE, emf_textw(100, 2000, "outer"),
                emfplus([plus_header(False), plus_image_metafile(0, x), plus_draw_image(0, 0, 0, 96, 96)])])


def test_nested_pictures_share_their_bytes():
    """A picture nested METAFILE_NESTING deep is parsed without a copy of its bytes at each level: the peak stays a
    small multiple of the file (the copies once made it about fifty times)."""
    import tracemalloc
    x = _padded_nest(1 << 20, limits.METAFILE_NESTING)
    tracemalloc.start()
    try:
        d = metafile.parse(x)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert set(lines(d)) == {"outer", "deep"} and not d.damaged, (lines(d), d.damaged)
    assert peak < 2 * len(x), (peak, len(x))


def test_nested_bytes_capped():
    """The bytes of every nested parse count against one cap, max(2 x the picture, METAFILE_NESTED): past it the
    nested picture is skipped and the drawing marked damaged, and the outer text stays."""
    x = _padded_nest(1 << 16, limits.METAFILE_NESTING)
    old = getattr(limits, "METAFILE_NESTED", None)
    limits.METAFILE_NESTED = 0  # the cap is then twice the picture: about two of its sixteen levels
    try:
        d = metafile.parse(x)
    finally:
        limits.METAFILE_NESTED = old
    assert d.damaged and lines(d) == ["outer"], (d.damaged, lines(d))
    d = metafile.parse(x)  # under the real cap the whole nest is read
    assert set(lines(d)) == {"outer", "deep"} and not d.damaged


def test_dual_skipped_records_still_move_the_current_point():
    def last_path(skipped):
        recs = [emfplus([plus_header(), plus_fill_rect(0, 0, 5, 5)]), emf_rec(27, struct.pack("<ii", 10, 10))]
        recs += skipped + [emfplus([plus_getdc()]), emf_rec(54, struct.pack("<ii", 500, 100))]
        d = metafile.parse(emf(recs))
        assert d.skipped == 0 and len(paths(d)) == 2
        return paths(d)[-1]
    p = last_path([emf_rec(54, struct.pack("<ii", 300, 400))])  # a LINETO the EMF+ records drew
    assert p.segs[0][1:] == (300, 400) and p.segs[1][1:] == (500, 100)
    p = last_path([emf_poly(89, [(30, 40), (60, 70)], wide=False)])  # POLYLINETO16
    assert p.segs[0][1:] == (60, 70)
    p = last_path([emf_rec(22, struct.pack("<I", 1)), emf_font(-100), emf_textw(0, 0, "ab", dx=[7, 8]),
                   emf_rec(22, struct.pack("<I", 0))])  # text under TA_UPDATECP advances by its dx
    assert p.segs[0][1:] == (25, 10)
    p = last_path([emf_rec(22, struct.pack("<I", 0)), emf_textw(0, 0, "ab", dx=[7, 8])])  # no TA_UPDATECP: no move
    assert p.segs[0][1:] == (10, 10)


def test_emf_no_rect_flag_with_the_rect_kept():
    # Windows keeps the rectangle under ETO_NO_RECT: offDx follows it
    d = metafile.parse(emf([emf_font(-100), emf_textw(0, 100, "abc", options=0x100, dx=[30, 40, 50], keep_rect=True)]))
    (r,) = runs(d)
    assert r.text == "abc" and close(r.width, 120)
    # laid out as MS-EMF says, with the rectangle left out: offDx at the spec's place
    d = metafile.parse(emf([emf_font(-100), emf_textw(0, 100, "abc", options=0x100, dx=[30, 40, 50])]))
    (r,) = runs(d)
    assert r.text == "abc" and close(r.width, 120)


def test_emfplus_image_bitmap_kept():
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
    d = metafile.parse(emf([emfplus([plus_header(False), plus_image_bitmap(0, png), plus_draw_image(0, 0, 0, 96, 48)])]))
    (b,) = [i for i in d.items if isinstance(i, Bitmap)]
    assert b.kind == "png" and b.data == png and close(b.box[2], 2540) and close(b.box[3], 1270)


def test_emf_mutated_records_never_raise():
    import random
    import time
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    bounds = struct.pack("<4i", 0, 0, 0, 0)
    pen = plus_object(4, 2, struct.pack("<IIIIf", PLUS_VERSION, 0, 0, 2, 1.0) + struct.pack("<III", PLUS_VERSION, 0, 0xFF000000))
    path = plus_object(1, 3, struct.pack("<IiI", PLUS_VERSION, 3, 0) + struct.pack("<6f", 0, 0, 5, 5, 9, 0) + bytes((0, 1, 0x81, 0)))
    region = plus_object(2, 4, struct.pack("<III4f", PLUS_VERSION, 0, 0x10000000, 0, 0, 10, 10))
    seeds = [
        emf([emf_font(-100, charset=129), emf_texta(0, 100, "한".encode("cp949")), emf_textw(5, 5, "ab", dx=[1, 2]),
             emf_smalltext(1, 1, b"s"), emf_polytextw([(1, 1, "p"), (2, 2, "q")]), emf_glyphs(1, 1, [3, 4], "Arial"),
             emf_pen(0xFF, 3), emf_brush(0xFF00), emf_rect(0, 0, 5, 5), emf_poly(3, [(0, 0), (5, 5), (9, 0)]),
             emf_poly(86, [(0, 0), (5, 5), (9, 0)], wide=False), emf_poly(85, [(0, 0), (1, 1), (2, 2), (3, 3)], wide=False),
             emf_rec(42, struct.pack("<4i", 0, 0, 9, 9)), emf_rec(44, struct.pack("<6i", 0, 0, 9, 9, 2, 2)),
             emf_rec(45, struct.pack("<8i", 0, 0, 9, 9, 9, 5, 0, 5)), emf_rec(47, struct.pack("<8i", 0, 0, 9, 9, 9, 5, 0, 5)),
             emf_rec(8, struct.pack("<4iIII4i", 0, 0, 0, 0, 1, 2, 2, 0, 0, 5, 5)),
             emf_world(1, 0, 0, -1, 0, 100), emf_modify_world(2, 0, 0, 2, 0, 0, 2), emf_rec(33), emf_rec(34, struct.pack("<i", -1)),
             emf_rec(17, struct.pack("<I", 8)), emf_rec(9, struct.pack("<ii", 100, 100)), emf_rec(11, struct.pack("<ii", 50, 50)),
             emf_rec(30, struct.pack("<4i", 0, 0, 50, 50)), emf_rec(59), emf_rec(27, struct.pack("<ii", 1, 1)),
             emf_rec(54, struct.pack("<ii", 9, 9)), emf_rec(61), emf_rec(60), emf_rec(63, bounds), emf_dib(1, 1, 2, 2, dib),
             emf_rec(75, struct.pack("<II8I4i", 48, 1, 32, 1, 1, 16, 0, 0, 5, 5, 0, 0, 5, 5)), emf_rec(40, struct.pack("<I", 2))]),
        emf([emfplus([plus_header(), plus_font(0, 12, "Arial"), plus_string(0, 1, 1, "x\ny", w=50, h=50), pen, path, region,
                      plus_driver(0, "ab"), plus_driver(0, [5, 6], cmap=False), plus_fill_rect(0, 0, 5, 5),
                      plus_rec(0x400D, 4, struct.pack("<I4f", 2, 0, 0, 5, 5)), plus_rec(0x4015, 1, struct.pack("<I", 4)),
                      plus_rec(0x4014, 0x8001, struct.pack("<I", 0xFF00FF00)), plus_rec(0x4018, 4, struct.pack("<fIII6f", 0.5, 0, 2, 3, 0, 0, 1, 1, 2, 0)),
                      plus_rec(0x4016, 0x8000, struct.pack("<IfI6f", 0xFF000000, 0.5, 3, 0, 0, 1, 1, 2, 0)),
                      plus_rec(0x4010, 0x8000, struct.pack("<Iff4f", 0xFF000000, 0, 90, 0, 0, 9, 9)),
                      plus_rec(0x4012, 4, struct.pack("<ff4f", 0, 90, 0, 0, 9, 9)), plus_rec(0x400E, 0x8000, struct.pack("<I4f", 0xFF000000, 0, 0, 9, 9)),
                      plus_rec(0x4019, 4, struct.pack("<I8f", 4, 0, 0, 1, 1, 2, 2, 3, 3)), plus_rec(0x4013, 0x8002, struct.pack("<I", 0xFF000000)),
                      plus_rec(0x4032, 1 << 8, struct.pack("<4f", 0, 0, 50, 50)), plus_rec(0x4034, 2, b""), plus_rec(0x4033, 1, b""),
                      plus_rec(0x4031), plus_world(2, 0, 0, 2, 0, 0), plus_rec(0x402F, 0, struct.pack("<f", 30)),
                      plus_rec(0x402D, 0, struct.pack("<ff", 1, 1)), plus_rec(0x402E, 0x2000, struct.pack("<ff", 2, 2)),
                      plus_rec(0x4030, 2, struct.pack("<f", 1.5)), plus_rec(0x4025, 0, struct.pack("<I", 1)),
                      plus_rec(0x4027, 0, struct.pack("<8fI", 0, 0, 9, 9, 0, 0, 1, 1, 2)), plus_rec(0x4029, 0, struct.pack("<I", 2)),
                      plus_rec(0x4026, 0, struct.pack("<I", 1)), plus_image_bitmap(3, b"\x89PNG\r\n\x1a\n" + b"\0" * 8),
                      plus_draw_image(3, 0, 0, 5, 5), plus_getdc()]),
             emf_font(-50), emf_textw(1, 1, "gdi"), _wrap(emf([emf_textw(1, 1, "in")]))[108:-20]]),
        _wrap(emf([emf_font(-10), emf_textw(1, 1, "nested"), emf_rect(0, 0, 5, 5)]))]
    rnd = random.Random(11)
    t0 = time.time()
    for n in range(1500):
        b = bytearray(seeds[n % len(seeds)])
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        try:
            d = metafile.parse(bytes(b))
        except ReadError:
            continue
        _assert_finite(d)
    assert time.time() - t0 < 10


def test_not_a_metafile():
    try:
        metafile.parse(b"\x00" * 64)
    except ReadError as e:
        assert e.kind == "damaged"
    else:
        raise AssertionError("no ReadError")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
