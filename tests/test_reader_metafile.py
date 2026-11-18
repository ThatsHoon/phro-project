"""WMF parsing: text and its code pages, the frame and mapping modes, shapes, the DC stack and hostile input.

Run: python -I -X utf8 tests/test_reader_metafile.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import (wmf, wmf_brush, wmf_exttext, wmf_font, wmf_polygon, wmf_rec, wmf_rect,  # noqa: E402
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


def test_wmf_mutated_records_never_raise():
    import random
    base = wmf([wmf_font(-100, 129), wmf_select(0), wmf_text(0, 100, b"ab"), wmf_exttext(1, 2, b"xy", dx=[1, 2]),
                wmf_polygon([(0, 0), (5, 5), (9, 0)]), wmf_rect(0, 0, 5, 5), wmf_rec(0x0817, 0, 100, 50, 0, 100, 100, 0, 0)])
    rnd = random.Random(7)
    for _ in range(2000):
        b = bytearray(base)
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        try:
            metafile.parse(bytes(b))
        except ReadError:
            pass


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
