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
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 2
    try:
        try:
            metafile.parse(wmf([wmf_text(0, 0, b"a"), wmf_text(0, 50, b"b"), wmf_text(0, 90, b"c")]))
        except ReadError as e:
            assert e.kind == "too_large"
        else:
            raise AssertionError("no too_large")
    finally:
        limits.METAFILE_RECORDS = old


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
