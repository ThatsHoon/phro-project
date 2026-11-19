"""Figures to PNG: target sizes, figure text and its warnings, the time reserve, the EMF writer, and GDI+ on Windows.

Run: python -I -X utf8 tests/test_reader_raster.py
Every fixture is built in memory and every PNG is checked in memory.
"""
import base64
import os
import random
import struct
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import (emf, emf_brush, emf_font, emf_glyphs, emf_pen, emf_rect, emf_textw, pict,  # noqa: E402
                          pict_longtext, pict_op, wmf, wmf_brush, wmf_font, wmf_rect, wmf_select, wmf_text, wmf_window, wpg1,
                          wpg1_fill, wpg1_rect, wpg1_text)
from reader import emf_write, fonts, images, limits, metafile, raster, svg_write  # noqa: E402
from reader import pict as pictmod, wpg as wpgmod  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.vector import Bitmap, Brush, Clip, Drawing, Path as VPath, Pen, Run, text_lines  # noqa: E402

WINDOWS = sys.platform == "win32"


def needs_gdiplus(fn):
    def wrapped():
        if not WINDOWS or raster.backend() != "gdiplus":
            print(f"skip: {fn.__name__} (needs Windows GDI+)")
            return
        fn()
    wrapped.__name__ = fn.__name__
    return wrapped


class no_backend:
    def __enter__(self):
        self.old = raster.backend
        raster.backend = lambda: "none"

    def __exit__(self, *exc):
        raster.backend = self.old


class empty_fonts:
    """An empty font folder and a temporary cache, so no real font is indexed and the real cache is not written."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("PHRO_READER_FONTS", "PHRO_READER_CACHE")}
        (Path(self.tmp.name) / "fonts").mkdir()
        os.environ["PHRO_READER_FONTS"] = str(Path(self.tmp.name) / "fonts")
        os.environ["PHRO_READER_CACHE"] = str(Path(self.tmp.name) / "cache")
        fonts.reset()

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        fonts.reset()
        self.tmp.cleanup()


# ---- PNG reading, in memory ------------------------------------------------------------------------------------------

def png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    return struct.unpack(">II", png[16:24])


def png_pixels(png: bytes):
    """(w, h, rows of (r, g, b) tuples) of an 8-bit RGB or RGBA PNG."""
    w, h = png_size(png)
    pos, idat, ctype = 8, b"", None
    while pos < len(png):
        n, tag = struct.unpack_from(">I4s", png, pos)
        body = png[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            depth, ctype = body[8], body[9]
            assert depth == 8 and ctype in (2, 6), (depth, ctype)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    rows = []
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pr) & 0xFF
        rows.append([tuple(line[x * bpp:x * bpp + 3]) for x in range(w)])
        prev = line
    return w, h, rows


def dark(px) -> bool:
    return max(px) < 100


def dark_count(rows, x0, y0, x1, y1) -> int:
    return sum(dark(rows[y][x]) for y in range(y0, y1) for x in range(x0, x1))


# ---- sizes, text and warnings ----------------------------------------------------------------------------------------

def test_target_size_rules():
    """Sized by content: never upscaled, except text under FIGURE_TEXT_PX (up to 2x, to FIGURE_EDGE); the long side
    at most FIGURE_EDGE_TEXT with text, FIGURE_EDGE_PLAIN without, FIGURE_EDGE_TEXT for TIFF."""
    ts = raster.target_size
    assert (limits.FIGURE_EDGE_TEXT, limits.FIGURE_EDGE_PLAIN, limits.FIGURE_TEXT_PX) == (1024, 768, 12)
    # no text: never upscaled, the long side at most 768
    assert ts(600, 400) == (600, 400) and ts(2000, 1000) == (768, 384) and ts(48, 48) == (48, 48)
    # text: no upscale at 12 px or more, the long side at most 1024
    assert ts(600, 400, text_px=20) == (600, 400) and ts(3000, 1500, text_px=20) == (1024, 512)
    assert ts(600, 400, text_px=12) == (600, 400)
    # text under 12 px: up by 12 / its height, at most 2x, then the long side at most 1568
    assert ts(600, 200, text_px=6) == (1200, 400) and ts(600, 200, text_px=10) == (720, 240)
    assert ts(600, 200, text_px=1) == (1200, 400) and ts(3000, 1500, text_px=6) == (1568, 784)
    # TIFF: never upscaled, the long side at most 1024
    assert ts(3000, 2000, kind="tiff") == (1024, 683) and ts(4, 3, kind="tiff") == (4, 3)
    # unknown content (the parser failed, GDI+ gave the size): as text, with no upscale
    assert ts(3000, 1500, kind="unknown") == (1024, 512) and ts(96, 96, kind="unknown") == (96, 96)
    assert ts(1, 1) == (1, 1) and ts(0, -3) == (1, 1)
    assert ts(10000, 1) == (768, 1)  # each side stays at least 1
    old = limits.FIGURE_EDGE_PLAIN
    limits.FIGURE_EDGE_PLAIN = 10_000  # the pixel cap holds when the edge does not
    try:
        w, h = ts(8000, 8000)
        assert w * h <= 16_777_216 and w == h and w > 4000
    finally:
        limits.FIGURE_EDGE_PLAIN = old


def test_convert_sizes_by_the_smallest_text():
    """convert hands the renderer the size worked out from the drawing: its smallest text in pixels at 96 dpi."""
    seen = []

    def run(jobs, mono):
        seen.append((jobs[0]["w"], jobs[0]["h"]))
        return [None]

    def pic(height=None):  # 600 x 200 px; a font of `height` twips is height / 15 px
        recs = [wmf_font(height), wmf_select(0), wmf_text(100, 1000, b"Hi")] if height else [wmf_rect(0, 0, 900, 900)]
        return wmf(recs, placeable=(0, 0, 9000, 3000, 1440))
    with fake_resvg(run):
        for height in (90, 150, 300, None):
            raster.convert("wmf", pic(height))
    assert seen == [(1200, 400), (720, 240), (600, 200), (600, 200)], seen


def test_text_without_backend():
    with no_backend():
        f = raster.convert("wmf", wmf([wmf_text(100, 200, b"Hello")]))
    assert f.png is None and f.lines == ["Hello"] and f.warnings == set()


def test_deadline_reserve_skips_render():
    data = wmf([wmf_text(100, 200, b"Hello")])
    f = raster.convert("wmf", data, deadline=time.time() + 1)
    assert f.png is None and f.lines == ["Hello"] and f.warnings == {"figures not rendered: time"}
    old = os.environ.get("PHRO_READER_DEADLINE")
    os.environ["PHRO_READER_DEADLINE"] = str(time.time() + 1)  # no deadline given: the env var is read
    try:
        f = raster.convert("wmf", data)
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_DEADLINE", None)
        else:
            os.environ["PHRO_READER_DEADLINE"] = old
    assert f.png is None and f.lines == ["Hello"] and "figures not rendered: time" in f.warnings
    f = raster.convert("wmf", data, deadline=time.time() - 5)  # past: the text is still read
    assert f.png is None and f.lines == ["Hello"] and "figures not rendered: time" in f.warnings


def test_parser_damage_warns():
    data = wmf([wmf_text(100, 200, b"Hello"), wmf_text(100, 400, b"World")])
    cut = data[:-12]  # the end record and part of the last text are gone
    with no_backend():
        f = raster.convert("wmf", cut)
        assert "figure damaged" in f.warnings and f.lines == ["Hello"], f
        g = raster.convert("emf", b"\x00" * 200)  # not a metafile: no lines, nothing read, nothing damaged
        assert g.png is None and g.lines == [] and g.warnings == set()


def test_text_cut_and_glyphs_unmapped_warn():
    old = limits.FIGURE_CHARS
    limits.FIGURE_CHARS = 4
    try:
        with no_backend():
            f = raster.convert("wmf", wmf([wmf_text(100, 200, b"Hello")]))
    finally:
        limits.FIGURE_CHARS = old
    assert f.lines == ["Hel…"] and f.warnings == {"figure text cut"}
    with empty_fonts(), no_backend():
        fonts.UNMAPPED = True  # a stale flag from an earlier caller is not this picture's
        g = raster.convert("emf", emf([emf_textw(100, 200, "Hi")]))
        assert g.warnings == set() and not fonts.UNMAPPED
        h = raster.convert("emf", emf([emf_glyphs(100, 200, [3, 4], "NoSuchFace")]))
        assert h.warnings == {"figure glyphs unmapped"} and h.lines == [] and not fonts.UNMAPPED


def test_kinds_and_garbage_never_raise():
    rnd = random.Random(20261118)
    with no_backend():
        for kind in ("wmf", "emf", "pict", "wpg", "tiff", "nonsense"):
            f = raster.convert(kind, rnd.randbytes(2048))
            assert f.png is None and f.lines == [], (kind, f)
            assert raster.convert(kind, b"").png is None
            assert raster.convert(kind, None).lines == []  # type: ignore[arg-type]


def test_backend_value_cached():
    a = raster.backend()
    assert a in ("gdiplus", "none") or a.startswith("resvg-"), a
    assert raster.backend() == a
    if WINDOWS:
        assert a == "gdiplus"


# ---- the EMF writer --------------------------------------------------------------------------------------------------

def sample_drawing() -> Drawing:
    box = [("M", 10, 10), ("L", 190, 10), ("L", 190, 90), ("L", 10, 90), ("Z",)]
    curve = [("M", 20, 50), ("C", 40, 20, 60, 80, 80, 50), ("C", 100, 20, 120, 80, 140, 50)]
    return Drawing((0, 0, 200, 100), 72.0, [
        VPath(box, Pen(0x000000, 1.0), Brush(0xCCCCCC)),
        VPath(curve, Pen(0xFF0000, 0.0), None),
        VPath(box, None, Brush(0x00FF00), even_odd=True),
        Clip([("M", 0, 0), ("L", 100, 0), ("L", 100, 100), ("L", 0, 100), ("Z",)]),
        Run("Sales", 20, 30, "Arial", 12),
        Run("2024", 80, 30, "Arial", 12, weight=700),
        Clip([("M", 0, 0), ("L", 100, 0), ("L", 50, 100), ("Z",)]),
        Clip(None),
        Run("Q1 120", 20, 60, "Times New Roman", 10, italic=True, color=0x0000FF),
        Run("up", 150, 80, "Arial", 8, angle=90.0),
        Bitmap(b"\x89PNG\r\n\x1a\n" + b"\0" * 32, "png", (0, 0, 10, 10)),
    ])


def test_emf_write_roundtrip():
    d = sample_drawing()
    data = emf_write.write(d)  # no decoder: the bitmap is left out
    assert data[:4] == b"\x01\x00\x00\x00" and data[40:44] == b" EMF"
    assert struct.unpack_from("<I", data, 48)[0] == len(data)  # nBytes
    back = metafile.parse(data)
    assert not back.damaged and back.skipped == 0, (back.damaged, back.skipped)
    assert back.size_px() == d.size_px()
    assert text_lines(back)[0] == text_lines(d)[0], (text_lines(back), text_lines(d))
    runs = [i for i in back.items if isinstance(i, Run)]
    assert [r.face for r in runs] == ["Arial", "Arial", "Times New Roman", "Arial"]
    assert runs[1].weight == 700 and runs[2].italic and runs[2].color == 0x0000FF and runs[3].angle == 90.0
    paths = [i for i in back.items if isinstance(i, VPath)]
    assert len(paths) == 3
    assert paths[0].brush.color == 0xCCCCCC and paths[0].pen.color == 0
    assert paths[1].brush is None and paths[1].pen.color == 0xFF0000
    assert paths[2].pen is None and paths[2].brush.color == 0x00FF00 and paths[2].even_odd
    assert any(s[0] == "C" for s in paths[1].segs)
    assert not any(isinstance(i, Bitmap) for i in back.items)
    clips = [i for i in back.items if isinstance(i, Clip)]
    assert clips and clips[-1].segs is None


def test_emf_write_glyphs_and_bitmap_decoder():
    d = Drawing((0, 0, 100, 100), 96.0, [Run("", 10, 50, "Arial", 20, glyphs=(36, 37)),
                                         Bitmap(b"\xff\xd8\xff" + b"\0" * 16, "jpg", (10, 10, 30, 20))])
    seen = []

    def decode(data, kind):
        seen.append(kind)
        return 2, 1, b"\x00\x00\xff\xff" + b"\xff\x00\x00\xff"  # a red then a blue pixel, BGRA, top-down

    back = metafile.parse(emf_write.write(d, decode))
    assert seen == ["jpg"]
    runs = [i for i in back.items if isinstance(i, Run)]
    assert runs[0].glyphs == (36, 37) and runs[0].text == ""
    bm = [i for i in back.items if isinstance(i, Bitmap)]
    assert len(bm) == 1 and bm[0].kind == "png"
    x0, y0, x1, y1 = bm[0].box
    k = 2540 / 96
    assert abs(x0 - 10 * k) < 2 and abs(x1 - 30 * k) < 2 and abs(y0 - 10 * k) < 2 and abs(y1 - 20 * k) < 2
    # a decoder that fails leaves the bitmap out
    back = metafile.parse(emf_write.write(d, lambda data, kind: None))
    assert not any(isinstance(i, Bitmap) for i in back.items)


def test_emf_write_hostile_values():
    big = 1e300
    d = Drawing((0, 0, 1e12, 5), 1.0, [VPath([("M", -big, big), ("L", float("nan"), 3), ("Z",)], Pen(0, 1e40), None),
                                       Run("far", big, -big, "A" * 80, 1e20, angle=float("inf")),
                                       Clip([])])
    data = emf_write.write(d)
    back = metafile.parse(data)
    assert text_lines(back)[0] == ["far"]
    for v in struct.unpack_from("<8i", data, 8):
        assert -2**31 <= v < 2**31
    empty = metafile.parse(emf_write.write(Drawing((0, 0, 0, 0), 0.0, [])))
    assert empty.size_px() == (1, 1)


def emf_records(data: bytes) -> list[tuple[int, bytes]]:
    """(type, body) of every record after the header."""
    out, pos = [], struct.unpack_from("<I", data, 4)[0]
    while pos + 8 <= len(data):
        typ, size = struct.unpack_from("<II", data, pos)
        out.append((typ, data[pos + 8:pos + size]))
        pos += size
    return out


def test_fixture_emf_counts():
    data = emf([emf_font(-100, index=1), emf_brush(0, index=7), emf_pen(index=3), emf_rect(0, 0, 5, 5)])
    n, handles = struct.unpack_from("<IH", data, 52)
    assert (n, handles) == (9, 8), (n, handles)  # header, three creates, three selects, a rect, EOF
    assert struct.unpack_from("<IH", emf([]), 52) == (2, 1)


def test_emf_write_batches_lines_and_curves():
    segs = [("M", 0, 0), ("L", 10, 0), ("L", 10, 10), ("L", 0, 10), ("C", 1, 1, 2, 2, 3, 3), ("C", 4, 4, 5, 5, 6, 6),
            ("L", 20, 20), ("L", 30, 30), ("Z",)]
    d = Drawing((0, 0, 100, 100), 96.0, [VPath(segs, Pen(0, 1), None)])
    recs = emf_records(emf_write.write(d))
    types = [t for t, _ in recs]
    assert 54 not in types, types  # no single LINETO
    polys = [(t, struct.unpack_from("<I", b, 16)[0]) for t, b in recs if t in (5, 6)]
    assert polys == [(6, 3), (5, 6), (6, 2)], polys
    back = metafile.parse(emf_write.write(d))
    p = [i for i in back.items if isinstance(i, VPath)][0]
    assert [s[0] for s in p.segs] == ["M", "L", "L", "L", "C", "C", "L", "L", "Z"], p.segs
    k = 2540 / 96
    assert abs(p.segs[2][1] - 10 * k) < 1 and abs(p.segs[7][2] - 30 * k) < 1


def test_emf_write_lone_close_writes_nothing():
    frame = (0, 0, 50, 50)
    empty = emf_write.write(Drawing(frame, 96.0, []))
    assert emf_write.write(Drawing(frame, 96.0, [VPath([("Z",)], Pen(0xFF0000, 2), Brush(0x00FF00))])) == empty
    assert emf_write.write(Drawing(frame, 96.0, [VPath([("Z",), ("Z",)], None, Brush(0))])) == empty


def test_emf_write_deadline():
    items = [VPath([("M", i, 0), ("L", i, 10)], Pen(0, 1), None) for i in range(5000)]
    d = Drawing((0, 0, 100, 100), 96.0, items)
    try:
        emf_write.write(d, deadline=time.monotonic() - 1)
    except ReadError as e:
        assert (e.kind, e.detail) == ("too_large", "figure time")
    else:
        raise AssertionError("no ReadError")
    assert emf_write.write(d, deadline=time.monotonic() + 60)[:4] == b"\x01\x00\x00\x00"


def test_emf_write_bitmap_budget():
    calls = []

    def decode(data, kind):
        calls.append(data)
        return 2, 1, b"\x00\x00\xff\xff" * 2

    items = [Bitmap(bytes([i]) * 20, "png", (0, 0, 10, 10)) for i in range(5)]
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = 16  # two 2 x 1 DIBs of 8 bytes
    try:
        out = emf_write.write(Drawing((0, 0, 100, 100), 96.0, items), decode)
    finally:
        limits.IMAGE_BYTES = old
    assert len(calls) == 2, len(calls)
    assert [t for t, _ in emf_records(out)].count(81) == 2
    limits.IMAGE_BYTES = 12  # the second DIB passes the budget: it is dropped and decoding stops
    calls.clear()
    try:
        out = emf_write.write(Drawing((0, 0, 100, 100), 96.0, items), decode)
    finally:
        limits.IMAGE_BYTES = old
    assert len(calls) == 2 and [t for t, _ in emf_records(out)].count(81) == 1


def test_cap_keeps_the_text_before_it():
    """A picture past METAFILE_POINTS keeps what was read before the cap: its text lines, with "figure damaged"."""
    rect = struct.pack(">hhhh", 36, 0, 72, 36)
    pictures = {
        "emf": emf([emf_font(-100), emf_textw(100, 200, "Label")] + [emf_rect(0, 0, 10, 10)] * 50
                   + [emf_textw(100, 900, "late")]),
        "wmf": wmf([wmf_font(100), wmf_text(100, 200, b"Label")] + [wmf_rect(0, 0, 10, 10)] * 50
                   + [wmf_text(100, 900, b"late")]),
        "pict": pict([pict_longtext(20, 10, b"Label")] + [pict_op(0x31, rect)] * 50
                     +[pict_longtext(20, 60, b"late")]),
        "wpg": wpg1([wpg1_text(700, 300, b"Label")] + [wpg1_rect(0, 0, 600, 600)] * 50 + [wpg1_text(700, 900, b"late")]),
    }
    old = limits.METAFILE_POINTS
    limits.METAFILE_POINTS = 100
    try:
        for kind, data in pictures.items():
            with no_backend():
                f = raster.convert(kind, data)
            assert f.lines == ["Label"] and f.warnings == {"figure damaged"}, (kind, f.lines, f.warnings)
    finally:
        limits.METAFILE_POINTS = old
    with no_backend():  # under the real cap every line is read
        assert raster.convert("emf", pictures["emf"]).lines == ["Label", "late"]


def test_size_cap_warns_damaged():
    data = emf([emf_textw(100, 200, "Hi")])
    old = limits.METAFILE_RECORDS
    limits.METAFILE_RECORDS = 1
    try:
        with no_backend():
            f = raster.convert("emf", data)
    finally:
        limits.METAFILE_RECORDS = old
    assert f.png is None and f.lines == ["Hi"] and f.warnings == {"figure damaged"}  # the text before the cap kept

# ---- GDI+ ------------------------------------------------------------------------------------------------------------

def render(d: Drawing) -> bytes:
    """A drawing through emf_write and GDI+, as convert draws a PICT or WPG."""
    return raster._render_gdiplus("pict", b"", d, 0)


@needs_gdiplus
def test_gdiplus_fallback_size_96dpi():
    """With the parser failed (a size cap now keeps the drawing, so a failure is stubbed), the PNG size comes from
    GDI+ at 96 dpi, and there is no text."""
    one_inch = emf([emf_brush(0x000000), emf_rect(0, 1270, 1270, 2540)])
    wide = emf_write.write(Drawing((0, 0, 200, 100), 96.0, [VPath([("M", 0, 50), ("L", 100, 50), ("L", 100, 100),
                                                                   ("L", 0, 100), ("Z",)], None, Brush(0))]))
    real = raster._parse

    def fail(*a):
        raise RuntimeError("parser failed")
    raster._parse = fail
    try:
        f = raster.convert("emf", one_inch)
        g = raster.convert("emf", wide)
    finally:
        raster._parse = real
    assert f.warnings == set() and f.lines == []
    # content unknown: sized as text, not upscaled
    assert png_size(f.png) == raster.target_size(96, 96, kind="unknown") == (96, 96)
    assert png_size(g.png) == raster.target_size(200, 100, kind="unknown") == (200, 100)
    w, h, rows = png_pixels(f.png)
    assert dark(rows[h * 3 // 4][w // 4]) and not dark(rows[h // 4][w * 3 // 4])


@needs_gdiplus
def test_gdiplus_emf_write_time_is_not_rendered():
    def late(d, decode=None, deadline=None):
        raise ReadError("too_large", "figure time")
    old = emf_write.write
    emf_write.write = late
    try:
        f = raster.convert("pict", pict([pict_longtext(20, 10, b"Mac")]))
    finally:
        emf_write.write = old
    assert f.png is None and f.lines == ["Mac"] and f.warnings == {"figures not rendered: time"}


@needs_gdiplus
def test_gdiplus_emf_write_text_pixels():
    d = Drawing((0, 0, 200, 100), 96.0, [Run("X", 20, 80, "Arial", 72)])
    png = raster._render_gdiplus("pict", b"", d, 0)
    w, h, rows = png_pixels(png)
    assert (w, h) == raster.drawing_size(d) == (200, 100)  # 72 px text: not upscaled
    assert dark_count(rows, 20, 20, 80, 85) > 75  # the X, above its baseline
    assert dark_count(rows, 100, 0, 200, 100) == 0


@needs_gdiplus
def test_gdiplus_emf_write_bitmap_lands_in_box():
    black = images._png(4, 4, [b"\x00\x00\x00" * 4] * 4, False)
    png = render(Drawing((0, 0, 100, 100), 96.0, [Bitmap(black, "png", (50, 50, 100, 100))]))
    w, h, rows = png_pixels(png)
    assert (w, h) == (100, 100)  # no text: not upscaled
    assert dark(rows[75][75]) and dark(rows[53][53]) and dark(rows[97][97])
    assert not dark(rows[45][75]) and not dark(rows[75][45]) and not dark(rows[25][25])


@needs_gdiplus
def test_gdiplus_emf_write_clips():
    whole = [("M", 0, 0), ("L", 100, 0), ("L", 100, 100), ("L", 0, 100), ("Z",)]
    rect = [("M", 0, 0), ("L", 50, 0), ("L", 50, 100), ("L", 0, 100), ("Z",)]
    tri = [("M", 0, 0), ("L", 100, 0), ("L", 0, 100), ("Z",)]
    corner = [("M", 90, 0), ("L", 100, 0), ("L", 100, 10), ("L", 90, 10), ("Z",)]
    for clip, inside, outside in ((rect, (25, 50), (75, 50)), (tri, (20, 20), (80, 80))):
        png = render(Drawing((0, 0, 100, 100), 96.0, [Clip(clip), VPath(whole, None, Brush(0)), Clip(None),
                                                      VPath(corner, None, Brush(0))]))
        w, h, rows = png_pixels(png)
        assert (w, h) == (100, 100)  # no text: not upscaled
        assert dark(rows[inside[1]][inside[0]]) and not dark(rows[outside[1]][outside[0]])
        assert dark(rows[5][95])  # drawn after the clip is removed

@needs_gdiplus
def test_gdiplus_wmf_png_size():
    data = wmf([wmf_text(100, 200, b"Hello")])  # placeable 1440 x 1440 at 1440 a inch: 96 px
    f = raster.convert("wmf", data)
    assert f.png is not None and f.lines == ["Hello"]
    assert png_size(f.png) == raster.drawing_size(metafile.parse(data))
    assert metafile.parse(data).size_px() == (96, 96)
    data = wmf([wmf_text(100, 200, b"Wide")], placeable=(0, 0, 2880, 720, 1440))
    d = metafile.parse(data)
    assert d.size_px() == (192, 48) and png_size(raster.convert("wmf", data).png) == raster.drawing_size(d)


@needs_gdiplus
def test_gdiplus_emf_png():
    data = emf([emf_brush(0x000000), emf_rect(0, 1270, 1270, 2540), emf_textw(100, 200, "Hi")])
    f = raster.convert("emf", data)
    assert f.png[:8] == b"\x89PNG\r\n\x1a\n" and f.lines == ["Hi"]
    want = raster.drawing_size(metafile.parse(data))
    assert png_size(f.png) == want and metafile.parse(data).size_px() == (96, 96)
    w, h, rows = png_pixels(f.png)
    assert dark(rows[h * 3 // 4][w // 4]) and not dark(rows[h // 4][w * 3 // 4])  # the black lower left quarter


@needs_gdiplus
def test_gdiplus_text_pixels():
    data = wmf([wmf_font(-72), wmf_select(0), wmf_text(20, 10, b"X")], placeable=(0, 0, 200, 100, 96))
    f = raster.convert("wmf", data)
    assert f.lines == ["X"]
    w, h, rows = png_pixels(f.png)
    assert (w, h) == (200, 100)  # 72 px text: not upscaled
    assert dark_count(rows, 20, 10, 85, 90) > 75  # the X
    assert dark_count(rows, 100, 0, 200, 100) == 0   # nothing to its right


@needs_gdiplus
def test_gdiplus_wmf_without_placeable():
    data = wmf([*wmf_window((0, 0), (300, 150)), wmf_brush(0x000000), wmf_select(0), wmf_rect(0, 75, 150, 150),
                wmf_text(200, 20, b"Plain")], placeable=None)
    f = raster.convert("wmf", data)
    assert f.lines == ["Plain"] and f.png is not None
    w, h, rows = png_pixels(f.png)
    assert (w, h) == raster.drawing_size(metafile.parse(data)) and w * 150 == h * 300
    k = w / 300
    assert dark(rows[int(112 * k)][int(75 * k)]) and not dark(rows[int(37 * k)][int(225 * k)])
    assert not dark(rows[int(112 * k)][int(225 * k)])
    # no window and no parser frame to take the box from: 512 px on the long side
    bare = raster._placeable(wmf([wmf_text(1, 1, b"a")], placeable=None), None)
    assert bare[6:14] == struct.pack("<4h", 0, 0, 512, 512)


@needs_gdiplus
def test_gdiplus_pict_via_emf():
    def rect(t, l, b, r):
        return struct.pack(">hhhh", t, l, b, r)
    data = pict([pict_op(0x31, rect(36, 0, 72, 36)), pict_longtext(20, 40, b"Mac")])
    f = raster.convert("pict", data)
    assert f.lines == ["Mac"] and f.png is not None
    w, h, rows = png_pixels(f.png)
    assert (w, h) == raster.drawing_size(pictmod.parse(data)) and w == h
    k = w / 192
    assert dark(rows[int(150 * k)][int(40 * k)]) and not dark(rows[int(150 * k)][int(150 * k)])


@needs_gdiplus
def test_gdiplus_wpg_via_emf():
    data = wpg1([wpg1_fill(1, 0), wpg1_rect(0, 0, 600, 600), wpg1_text(700, 900, b"Wpg")])
    f = raster.convert("wpg", data)
    assert f.lines == ["Wpg"] and f.png is not None
    w, h, rows = png_pixels(f.png)
    assert (w, h) == raster.drawing_size(wpgmod.parse(data)) and w == h
    k = w / 192
    assert dark(rows[int(150 * k)][int(40 * k)]) and not dark(rows[int(40 * k)][int(40 * k)])


def tiff(pages) -> bytes:
    """A little-endian, uncompressed 8-bit grey TIFF, one strip a page; pages are (w, h, value)."""
    out = bytearray(b"II*\x00\x00\x00\x00\x00")
    first = True
    for w, h, v in pages:
        pix = len(out)
        out += bytes([v]) * (w * h)
        out += b"\0" * (len(out) & 1)
        ifd = len(out)
        if first:
            struct.pack_into("<I", out, 4, ifd)
        else:
            struct.pack_into("<I", out, prev_next, ifd)
        first = False
        tags = [(256, 4, w), (257, 4, h), (258, 3, 8), (259, 3, 1), (262, 3, 1), (273, 4, pix), (277, 3, 1),
                (278, 4, h), (279, 4, w * h)]
        out += struct.pack("<H", len(tags))
        for tag, typ, val in tags:
            val = struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val)
            out += struct.pack("<HHI", tag, typ, 1) + val
        prev_next = len(out)
        out += b"\0\0\0\0"
    return bytes(out)


def test_tiff_pages_by_ifds():
    data = tiff([(4, 3, 0), (10, 5, 255)])
    assert raster._tiff_ifds(data) == 2
    loop = bytearray(tiff([(4, 3, 0)]))
    ifd = struct.unpack_from("<I", loop, 4)[0]
    struct.pack_into("<I", loop, ifd + 2 + 9 * 12, ifd)  # the next IFD is itself
    assert raster._tiff_ifds(bytes(loop)) == 1
    old = limits.TIFF_PAGES
    limits.TIFF_PAGES = 1
    try:
        assert raster._tiff_ifds(data) == 1
    finally:
        limits.TIFF_PAGES = old
    assert raster._tiff_ifds(b"II*\x00\xff\xff\xff\x7f") == 0 and raster._tiff_ifds(b"junk") == 0
    with no_backend():
        assert raster.tiff_pages(data) == 2
        f = raster.convert("tiff", data)
        assert f.png is None and f.lines == [] and f.warnings == set()


@needs_gdiplus
def test_gdiplus_tiff_page_two():
    data = tiff([(4, 3, 0), (10, 5, 255)])
    assert raster.tiff_pages(data) == 2
    one = raster.convert("tiff", data)
    assert png_size(one.png) == raster.target_size(4, 3, kind="tiff") == (4, 3) and one.lines == []
    two = raster.convert("tiff", data, page=1)
    assert png_size(two.png) == raster.target_size(10, 5, kind="tiff") == (10, 5)
    _, _, rows = png_pixels(two.png)
    assert rows[2][5] == (255, 255, 255)
    _, _, rows = png_pixels(one.png)
    assert dark(rows[1][2])
    assert raster.convert("tiff", data, page=2).png is None


@needs_gdiplus
def test_gdiplus_garbage_none():
    rnd = random.Random(7)
    for kind in ("wmf", "emf", "pict", "wpg", "tiff"):
        for n in (0, 16, 300, 5000):
            f = raster.convert(kind, rnd.randbytes(n))
            assert f.png is None, kind
    assert raster.tiff_pages(rnd.randbytes(100)) == 0


# ---- SVG, and the resvg and Pillow backend ----------------------------------------------------------------------------

SVG_NS = "{http://www.w3.org/2000/svg}"
FAKE_RESVG = "resvg-0.5.0-pillow-12.3.0"
PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def svg_root(d: Drawing, size=(400, 200), resolve=None):
    return ET.fromstring(svg_write.write(d, size, resolve))


def needs_image_venv(fn):
    def wrapped():
        if sys.platform == "win32" or raster._load_local() is None:
            print(f"skip: {fn.__name__} (needs the image venv)")
            return
        fn()
    wrapped.__name__ = fn.__name__
    return wrapped


class fake_resvg:
    """convert with a resvg backend named and the renderer replaced by `run(jobs, mono)`."""

    def __init__(self, run):
        self.run = run

    def __enter__(self):
        self.old = raster.backend, raster._run_jobs
        raster.backend = lambda: FAKE_RESVG
        raster._run_jobs = self.run

    def __exit__(self, *exc):
        raster.backend, raster._run_jobs = self.old


def test_svg_parses_as_xml():
    root = svg_root(sample_drawing())
    assert root.tag == SVG_NS + "svg"
    assert root.get("width") == "400" and root.get("height") == "200" and root.get("viewBox") == "0 0 200 100"
    first = root[0]
    assert first.tag == SVG_NS + "rect" and first.get("fill") == "#ffffff", first.attrib
    paths = [e for e in root.iter(SVG_NS + "path") if e.get("fill") is not None]  # not the clipPath ones
    assert len(paths) == 3
    assert paths[0].get("stroke") == "#000000" and paths[0].get("fill") == "#cccccc"
    assert paths[1].get("fill") == "none" and paths[1].get("stroke") == "#ff0000"
    assert paths[2].get("stroke") == "none" and paths[2].get("fill-rule") == "evenodd"
    assert paths[0].get("fill-rule") == "nonzero"
    assert paths[0].get("d").startswith("M10 10 L190 10") and paths[0].get("d").endswith("Z")
    assert " C" in paths[1].get("d")
    texts = root.findall(f".//{SVG_NS}text")
    assert [t.text for t in texts] == ["Sales", "2024", "Q1 120", "up"]
    t = texts[1]
    assert t.get("x") == "80" and t.get("y") == "30" and t.get("font-size") == "12"
    assert t.get("font-weight") == "700" and t.get("font-style") == "normal" and t.get("fill") == "#000000"
    assert texts[2].get("font-style") == "italic" and texts[2].get("fill") == "#0000ff"
    assert texts[2].get("font-family") == "Times New Roman, serif"
    assert texts[0].get("font-family") == "Arial, sans-serif"
    img = root.find(f".//{SVG_NS}image")
    assert img.get("href").startswith("data:image/png;base64,") and img.get("width") == "10"


def test_svg_escapes_text_and_face():
    d = Drawing((0, 0, 100, 50), 96.0, [Run('<&"', 5, 20, 'a"b', 10), Run("]]><x/>", 5, 40, "<i>", 10)])
    root = svg_root(d, (100, 50))
    t = root.findall(f".//{SVG_NS}text")
    assert t[0].text == '<&"' and t[0].get("font-family") == 'a"b, sans-serif'
    assert t[1].text == "]]><x/>" and t[1].get("font-family") == "<i>, sans-serif"
    assert not any(e.tag.endswith("}x") for e in root.iter())
    d = Drawing((0, 0, 100, 50), 96.0, [Run("a\x00b\x08c\ud800d", 5, 20, "F\x01", 10)])
    text = svg_write.write(d, (100, 50))
    assert ET.fromstring(text).find(f".//{SVG_NS}text").text == "abcd"


def test_svg_no_external_refs():
    root = svg_root(sample_drawing())
    hrefs = [v for e in root.iter() for k, v in e.attrib.items() if k.endswith("href")]
    assert hrefs and all(h.startswith("data:") for h in hrefs), hrefs
    text = svg_write.write(sample_drawing(), (400, 200)).replace('xmlns="http://www.w3.org/2000/svg"', "")
    for bad in ("<script", "<!DOCTYPE", "<!ENTITY", "<use", "<foreignObject", "http:", "file:"):
        assert bad not in text, bad
    # a bitmap of an unknown kind is left out, and a hostile box is clamped, never linked
    d = Drawing((0, 0, 10, 10), 96.0, [Bitmap(b"x", "gif", (0, 0, 5, 5)),
                                       Bitmap(b"\xff\xd8\xff", "jpg", (0, 0, float("nan"), 1e99))])
    imgs = svg_root(d, (10, 10)).findall(f".//{SVG_NS}image")
    assert len(imgs) == 1 and imgs[0].get("href").startswith("data:image/jpeg;base64,")
    assert float(imgs[0].get("height")) <= 2**32


def test_svg_cjk_fallback():
    d = Drawing((0, 0, 200, 100), 96.0, [
        Run("한글", 5, 20, "Batang", 10),
        Run("かな", 5, 40, "MS Mincho", 10),
        Run("汉字", 5, 60, "Microsoft YaHei", 10),
        Run("漢字", 5, 80, "Microsoft JhengHei", 10),
        Run("Plain", 5, 95, "Palatino Roman", 10),
        Run("Tight", 5, 99, "Calibri", 10)])
    t = [e.get("font-family") for e in svg_root(d, (200, 100)).findall(f".//{SVG_NS}text")]
    kr = "Malgun Gothic, Apple SD Gothic Neo, Noto Sans CJK KR, sans-serif"
    assert t[0] == "Batang, " + kr, t[0]
    assert t[1] == "MS Mincho, Yu Gothic, Hiragino Sans, Noto Sans CJK JP, " + kr, t[1]
    assert t[2] == "Microsoft YaHei, Microsoft YaHei, PingFang SC, Noto Sans CJK SC, " + kr, t[2]
    assert t[3] == "Microsoft JhengHei, Microsoft JhengHei, PingFang TC, Noto Sans CJK TC, " + kr, t[3]
    assert t[4] == "Palatino Roman, serif" and t[5] == "Calibri, sans-serif"
    assert svg_write._family("MS Sans Serif", "x") == "MS Sans Serif, sans-serif"  # "Sans" wins over "Serif"
    assert svg_write._family("", "x") == "sans-serif"


def test_svg_rotation():
    d = Drawing((0, 0, 100, 100), 96.0, [Run("up", 30, 70, "Arial", 10, angle=90.0), Run("flat", 5, 5, "Arial", 10),
                                         Run("full", 5, 9, "Arial", 10, angle=360.0),
                                         Run("nan", 5, 9, "Arial", 10, angle=float("nan"))])
    t = svg_root(d, (100, 100)).findall(f".//{SVG_NS}text")
    assert t[0].get("transform") == "rotate(-90 30 70)", t[0].attrib
    assert t[1].get("transform") is None and t[2].get("transform") is None and t[3].get("transform") is None


def test_svg_clip():
    sq = [("M", 0, 0), ("L", 50, 0), ("L", 50, 50), ("L", 0, 50), ("Z",)]
    d = Drawing((0, 0, 100, 100), 96.0, [
        VPath(sq, None, Brush(0xFF0000)),
        Clip(sq),
        VPath(sq, None, Brush(0x00FF00)),
        Clip([]),
        VPath(sq, None, Brush(0x0000FF)),
        Clip(None),
        VPath(sq, None, Brush(0x000000))])
    root = svg_root(d, (100, 100))
    clips = root.findall(f".//{SVG_NS}clipPath")
    assert len(clips) == 2 and all(c.get("id") for c in clips)
    ids = [c.get("id") for c in clips]
    assert len(set(ids)) == 2
    groups = root.findall(f"{SVG_NS}g")
    assert [g.get("clip-path") for g in groups] == [f"url(#{i})" for i in ids], [g.attrib for g in groups]
    assert groups[0].find(f"{SVG_NS}path").get("fill") == "#00ff00"
    assert clips[0].find(f"{SVG_NS}path").get("d").startswith("M0 0")
    assert clips[1].find(f"{SVG_NS}path").get("d") == "M0 0 L0 0 Z"  # an empty clip shows nothing
    top = [e.get("fill") for e in root if e.tag == SVG_NS + "path"]
    assert top == ["#ff0000", "#000000"], top


def test_svg_hostile_numbers():
    nan, inf = float("nan"), float("inf")
    d = Drawing((0, 0, 100, 100), 96.0, [
        VPath([("M", nan, inf), ("L", -1e300, 1e300), ("C", 1, 2, 3, 4, 5, nan), ("Z",), ("X", 1)],
              Pen(0, nan), Brush(0)),
        Run("t", nan, inf, "Arial", nan)])
    root = svg_root(d, (100, 100))
    dd = root.find(f".//{SVG_NS}path").get("d")
    assert "nan" not in dd.lower() and "inf" not in dd.lower() and "e+" not in dd, dd
    assert "2147483647" in dd and "-2147483648" in dd, dd
    t = root.find(f".//{SVG_NS}text")
    assert t is not None and "nan" not in t.get("x").lower() and "nan" not in t.get("font-size").lower()
    f = svg_write.write(Drawing((nan, 0, inf, 5), 0.0, []), (0, -4))
    assert ET.fromstring(f).get("width") == "1" and ET.fromstring(f).get("height") == "1"


def test_svg_glyph_runs_use_resolve():
    d = Drawing((0, 0, 100, 50), 96.0, [Run("", 5, 20, "Sym", 10, glyphs=(3, 4)), Run("", 5, 40, "Sym", 10, glyphs=(9,))])
    root = svg_root(d, (100, 50), resolve=lambda r: "AB" if r.glyphs == (3, 4) else None)
    assert [t.text for t in root.findall(f".//{SVG_NS}text")] == ["AB"]
    assert svg_root(d, (100, 50)).findall(f".//{SVG_NS}text") == []


def test_resvg_job_carries_our_svg():
    seen = []
    png = PNG_HEAD + b"\0" * 16
    with fake_resvg(lambda jobs, mono: seen.append(jobs) or [png]):
        g = raster.convert("wmf", wmf([wmf_rect(10, 10, 100, 100), wmf_text(100, 200, b"Hello")]))
    assert g.png == png and g.lines == ["Hello"], g
    job = seen[-1][0]
    assert job["svg"].startswith("<svg") and "Hello" in job["svg"] and len(seen[-1]) == 1
    assert job["w"] >= 1 and job["h"] >= 1 and f'width="{job["w"]}"' in job["svg"]


def test_figure_work_keeps_the_reserve():
    """Every figure step (the parser's Budget, emf_write, the image venv's child) stops FIGURE_RESERVE before the
    document deadline, so a figure still working leaves the rest of the document that time to be written."""
    data = wmf([wmf_text(100, 200, b"Hello")])
    given = []

    def slow(kind, data, codepage, mono):  # works until the time it is given, as Budget does, then stops
        given.append(mono)
        while mono is not None and time.monotonic() < mono:
            time.sleep(0.01)
        raise ReadError("too_large", "figure time")
    real = raster._parse
    raster._parse = slow
    deadline = time.time() + limits.FIGURE_RESERVE + 0.5
    try:
        with no_backend():
            f = raster.convert("wmf", data, deadline=deadline)
    finally:
        raster._parse = real
    left = deadline - time.time()
    assert f.png is None and f.warnings == {limits.FIGURES_LATE}, f
    assert left > limits.FIGURE_RESERVE - 1, left  # returned about half a second in, the reserve still ahead
    # the renderer (emf_write and GDI+, or the venv's child) is handed the same time: the deadline less the reserve
    deadline = time.time() + limits.FIGURE_RESERVE + 30
    with fake_resvg(lambda jobs, mono: given.append(mono) or [None]):
        g = raster.convert("wmf", data, deadline=deadline)
    want = time.monotonic() + (deadline - limits.FIGURE_RESERVE - time.time())
    assert g.lines == ["Hello"] and abs(given[-1] - want) < 1, (given[-1], want)


def test_resvg_failure_leaves_text():
    data = wmf([wmf_text(100, 200, b"Hello")])

    def boom(jobs, mono):
        raise RuntimeError("x")

    def late(jobs, mono):
        raise ReadError("too_large", "figure time")
    with fake_resvg(boom):
        f = raster.convert("wmf", data)
    assert f.png is None and f.lines == ["Hello"] and f.warnings == set()
    with fake_resvg(late):
        f = raster.convert("wmf", data)
    assert f.png is None and f.lines == ["Hello"] and f.warnings == {"figures not rendered: time"}
    with fake_resvg(lambda jobs, mono: [b"not a png"]):  # a renderer that answers garbage
        assert raster.convert("wmf", data).png is None
    with fake_resvg(lambda jobs, mono: [None]):
        assert raster.convert("wmf", data).png is None


def test_resvg_tiff_job_and_parser_failure():
    seen = []
    with fake_resvg(lambda jobs, mono: seen.append(jobs) or [PNG_HEAD + b"xx"]):
        f = raster.convert("tiff", tiff([(4, 4, 90), (4, 4, 200)]), page=1)
        assert f.png and seen[-1][0]["page"] == 1 and base64.b64decode(seen[-1][0]["tiff"])[:2] == b"II"
        n = len(seen)
        assert raster.convert("tiff", b"nope").png is None and len(seen) == n  # not a TIFF: no job
        assert raster.convert("tiff", tiff([(4, 4, 1)]), page=limits.TIFF_PAGES).png is None and len(seen) == n
        assert raster.convert("wmf", b"\0" * 40).png is None and len(seen) == n  # no drawing, no SVG


def test_worker_protocol_and_timeout():
    """The child entry takes a JSON batch on stdin and answers one result per job; no backend here, so null."""
    jobs = [{"svg": "<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/>", "w": 4, "h": 4},
            {"tiff": base64.b64encode(tiff([(4, 4, 9)])).decode("ascii"), "page": 0}]
    cmd = raster._child_cmd(sys.executable)
    assert cmd[1:5] == ["-I", "-X", "utf8", "-c"] and cmd[-1] == str(Path(raster.__file__).resolve().parents[1])
    out = raster._run_child(cmd, jobs, 60)
    assert len(out) == 2
    if raster._load_local() is None:
        assert out == [None, None], out
    slow = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
    t0 = time.monotonic()
    try:
        raster._run_child(slow, jobs, 1)
    except ReadError as e:
        assert e.kind == "too_large" and e.detail == "figure time"
    else:
        raise AssertionError("no timeout")
    assert time.monotonic() - t0 < 15
    junk = [sys.executable, "-I", "-c", "print('not json')"]
    assert raster._run_child(junk, jobs, 30) == [None, None]
    five = [sys.executable, "-I", "-c", "import json; print(json.dumps({'results': ['AAAA'] * 5}))"]
    assert raster._run_child(five, jobs, 30) == [None, None]  # the wrong count is not trusted
    fake = [sys.executable, "-I", "-c", "import json; print(json.dumps({'results': ['iVBORw0KGgo=', None]}))"]
    assert raster._run_child(fake, jobs, 30) == [base64.b64decode("iVBORw0KGgo="), None]


def test_backend_none_without_venv_or_libraries():
    if raster._load_local() is not None or sys.platform == "win32":
        return
    assert raster.backend() == "none"


@needs_image_venv
def test_resvg_render():
    d = sample_drawing()
    size = raster.drawing_size(d)
    png = raster._render_local({"svg": svg_write.write(d, size, fonts.resolve), "w": size[0], "h": size[1]})
    assert png[:8] == PNG_HEAD and png_size(png) == size


@needs_image_venv
def test_pillow_tiff_page():
    data = base64.b64encode(tiff([(6, 4, 10), (8, 5, 250)])).decode("ascii")
    png = raster._render_local({"tiff": data, "page": 1})
    assert png[:8] == PNG_HEAD and png_size(png) == raster.target_size(8, 5, kind="tiff")
    assert raster._render_local({"tiff": data, "page": 2}) is None


# ---- lone picture files (formats.picture) ------------------------------------------------------------------------------

def lone(name: str, data: bytes) -> Path:
    """A well-formed picture fixture in a fresh folder (only valid fixtures are written; hostile bytes stay in memory)."""
    p = Path(tempfile.mkdtemp()) / name
    p.write_bytes(data)
    return p


def convert_lone(p: Path) -> tuple[str, dict, Path]:
    import json
    import reader
    out = Path(tempfile.mkdtemp()) / "out"
    with empty_fonts():
        meta = reader.convert(p, out)
    return (out / "out.md").read_text(encoding="utf-8"), meta, out


def test_lone_emf_file():
    data = emf([emf_brush(0x000000), emf_rect(0, 1270, 1270, 2540), emf_textw(100, 200, "Hi")])
    md, meta, out = convert_lone(lone("a.emf", data))
    assert "Figure text: Hi" in md, md
    if WINDOWS and raster.backend() == "gdiplus":
        assert md.count("](assets/img-001.png)") == 1 and md.count("![") == 1, md
        assert meta["figures"] == 1 and meta["figures_left"] == 0 and meta["images"] == 1, meta
        want = raster.drawing_size(metafile.parse(data))
        assert png_size((out / "assets" / "img-001.png").read_bytes()) == want, want
    else:
        print("skip: test_lone_emf_file PNG half (needs Windows GDI+)")


def test_lone_wmz_inflates():
    import gzip
    from reader.formats import picture
    md, _, _ = convert_lone(lone("a.wmz", gzip.compress(wmf([wmf_text(100, 200, b"Zipped")]))))
    assert "Figure text: Zipped" in md, md
    md, _, _ = convert_lone(lone("b.EMZ", gzip.compress(emf([emf_textw(100, 200, "Emz")]))))
    assert "Figure text: Emz" in md, md
    raw = wmf([wmf_text(100, 200, b"Cap")])
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(raw) - 1  # read at call time: the inflated picture is capped at IMAGE_BYTES
    try:
        picture.unpack(gzip.compress(raw), "wmz")
        raise AssertionError("no cap")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith(limits.BIG_PART + ":"), (e.kind, e.detail)
    finally:
        limits.IMAGE_BYTES = old
    assert picture.unpack(gzip.compress(raw), "wmz") == raw
    for bad in (b"\x1f\x8b" + bytes(30), b"not gzip at all", gzip.compress(raw)[:-20]):
        try:
            picture.unpack(bad, "wmz")
            raise AssertionError(bad)
        except ReadError as e:
            assert e.kind == "damaged", (e.kind, e.detail)


def test_wmz_ratio_cap():
    import gzip
    from reader.formats import picture
    bomb = gzip.compress(wmf([wmf_text(1, 1, b"x")]) + bytes(16 * 2**20), 9)  # built in memory, never written
    assert len(bomb) * limits.ZIP_RATIO < 16 * 2**20, len(bomb)
    try:
        picture.unpack(bomb, "emz")
        raise AssertionError("no ratio cap")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith(limits.BIG_PART + ":"), (e.kind, e.detail)
    # under the floor the ratio is not checked: 900 KB of zeros from a 1 KB stream is a picture
    small = wmf([wmf_text(1, 1, b"x")]) + bytes(900_000)
    assert picture.unpack(gzip.compress(small, 9), "wmz") == small


def test_lone_tiff_pages_and_cap():
    from reader import model
    from reader.formats import picture
    data = tiff([(4, 3, 0), (10, 5, 255), (6, 6, 128)])
    p = lone("scan.tif", data)
    doc = picture.parse(p, model.Context())
    assert [b.page for b in doc.blocks] == [0, 1, 2] and all(b.data == data for b in doc.blocks), doc.blocks
    assert [b.alt for b in doc.blocks] == ["page 1", "page 2", "page 3"] and doc.warnings == [], doc
    old = limits.TIFF_PAGES
    limits.TIFF_PAGES = 2
    try:
        doc = picture.parse(lone("scan.TIFF", data), model.Context())
        assert [b.page for b in doc.blocks] == [0, 1] and doc.warnings == ["TIFF pages cut at 2"], doc
        two = picture.parse(lone("two.tiff", tiff([(4, 3, 0), (10, 5, 255)])), model.Context())
        assert [b.page for b in two.blocks] == [0, 1] and two.warnings == [], two  # exactly the cap: no cut
    finally:
        limits.TIFF_PAGES = old
    one = picture.parse(lone("one.tif", tiff([(4, 3, 0)])), model.Context())
    assert [(b.page, b.alt) for b in one.blocks] == [(0, "")], one.blocks
    md, meta, out = convert_lone(p)
    if WINDOWS and raster.backend() == "gdiplus":
        assert md.count("![page ") == 3 and meta["figures"] == 3 and meta["figures_left"] == 0, (md, meta)
        sizes = [png_size((out / "assets" / f"img-00{i}.png").read_bytes()) for i in (1, 2, 3)]
        assert sizes == [(4, 3), (10, 5), (6, 6)], sizes  # TIFF pages: not upscaled
    with no_backend():  # unconverted pages share one copy of the original file
        md, meta, out = convert_lone(p)
        assert sorted(f.name for f in (out / "assets").iterdir()) == ["img-001.tiff"], list((out / "assets").iterdir())
        assert meta["figures"] == 0 and meta["figures_left"] == 1, meta


def test_extension_mismatch_damaged():
    import gzip
    from reader.formats import picture
    w = wmf([wmf_text(1, 1, b"x")])
    e_ = emf([emf_textw(1, 1, "x")])
    cases = [(e_, "wmf", "wmf"), (w, "emf", "emf"), (w, "tif", "tiff"), (tiff([(2, 2, 0)]), "pct", "pict"),
             (w, "wpg", "wpg"), (gzip.compress(e_), "wmz", "wmf"), (b"\0" * 600, "pict", "pict")]
    for data, ext, kind in cases:
        try:
            picture.unpack(data, ext)
            raise AssertionError((ext, kind))
        except ReadError as err:
            assert err.kind == "damaged" and err.detail == f"not a {kind} file", (ext, err.kind, err.detail)
    assert picture.unpack(w, "wmf") == w and picture.unpack(e_, "emf") == e_
    # a PICT with its 512-byte head left out, or holding an application's bytes, gets a zero head
    pic = pict([pict_longtext(20, 40, b"Mac")])
    assert picture.unpack(pic, "pct") == pic
    assert picture.unpack(pic[512:], "pict") == pic
    assert picture.unpack(b"\x01" * 512 + pic[512:], "pict") == pic
    p = lone("a.wmf", e_)
    from reader import model
    try:
        picture.parse(p, model.Context())
        raise AssertionError("emf in a .wmf")
    except ReadError as err:
        assert err.kind == "damaged", err


def test_lone_picture_capped():
    """A lone picture past IMAGE_BYTES is too_large, worked out from its size before it is read (a lowered cap)."""
    from reader import model
    from reader.formats import picture
    data = emf([emf_textw(100, 200, "Big")])
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(data) - 1
    try:
        for name, body in (("a.emf", data), ("b.tif", tiff([(40, 40, 0)])), ("c.wpg", data), ("d.emz", data)):
            p = lone(name, body if len(body) > limits.IMAGE_BYTES else body + bytes(len(data)))
            real = Path.read_bytes
            Path.read_bytes = lambda self: (_ for _ in ()).throw(AssertionError("read before the size check"))
            try:
                picture.parse(p, model.Context())
                raise AssertionError(name)
            except ReadError as e:
                assert e.kind == "too_large" and e.detail == "picture past IMAGE_BYTES", (name, e.kind, e.detail)
            finally:
                Path.read_bytes = real
    finally:
        limits.IMAGE_BYTES = old
    assert picture.parse(lone("a.emf", data), model.Context()).blocks[0].data == data


def test_image_page_reaches_assets():
    """An Image block's page goes to raster.convert and is part of the Assets key; page 0 is the first page."""
    from reader.model import Image
    assert Image(b"x").page == 0
    seen = []
    real = raster.convert

    def fake(kind, data, **kw):
        seen.append(kw.get("page", 0))
        return raster.Figure(None, [], set())
    raster.convert = fake
    try:
        a = images.Assets(Path(tempfile.mkdtemp()))
        data = tiff([(4, 3, 0), (10, 5, 255)])
        first = a.add(data)
        assert a.add(data, page=1) == first and seen == [0, 1], (seen, first)  # one kept copy of the original
        assert a.add(data, page=1) == first and seen == [0, 1], seen  # the page's own entry: not converted again
    finally:
        raster.convert = real


# ---- gray pixels for OCR (ocr_pixels and raster.gray) ----------------------------------------------------------------

def test_from_pixels_takes_green():
    from reader import ocr_pixels
    bgra = bytes([10, 20, 30, 255, 11, 21, 31, 255, 12, 22, 32, 0, 13, 23, 33, 7])  # 2x2, B G R A
    g = ocr_pixels.from_pixels(2, 2, bgra, 4)
    assert (g.w, g.h, g.data) == (2, 2, bytes([20, 21, 22, 23]))
    bgr = bytes([1, 2, 3, 4, 5, 6, 99, 99]) + bytes([7, 8, 9, 10, 11, 12, 99, 99])  # 2x2 BGR, rows padded to 8
    assert ocr_pixels.from_pixels(2, 2, bgr, 3, stride=8).data == bytes([2, 5, 8, 11])
    assert ocr_pixels.from_pixels(3, 1, b"\x01\x02\x03", 1).data == b"\x01\x02\x03"
    for bad in ((2, 2, bgra[:-1], 4), (2, 2, bgra, 2), (2, 2, bgr, 3, 5)):
        try:
            ocr_pixels.from_pixels(*bad)
        except ValueError:
            continue
        raise AssertionError(f"from_pixels{bad[:2]} accepted a short or odd buffer")


def test_fit_within_cap():
    """Each reduction averages 2x2 blocks (a power of two), never keeps every n-th pixel."""
    from reader import ocr_pixels
    rnd = random.Random(3)
    src = ocr_pixels.Gray(1000, 1000, rnd.randbytes(1000 * 1000))
    assert ocr_pixels.fit(src, 1_000_000) is src
    g = ocr_pixels.fit(src, 250_000)
    assert (g.w, g.h) == (500, 500)
    d, w = src.data, src.w
    for r, c in [(0, 0), (499, 499), (123, 456)] + [(rnd.randrange(500), rnd.randrange(500)) for _ in range(500)]:
        block = d[2 * r * w + 2 * c] + d[2 * r * w + 2 * c + 1] + d[(2 * r + 1) * w + 2 * c] + d[(2 * r + 1) * w + 2 * c + 1]
        assert abs(g.data[r * g.w + c] - block // 4) <= 1, (r, c, g.data[r * g.w + c], block / 4)
    assert ocr_pixels.reduction(1000, 1000, 250_000) == (2, 2)
    assert ocr_pixels.reduction(1000, 1000, 249_999) == (4, 4)
    q = ocr_pixels.fit(src, 249_999)
    assert (q.w, q.h) == (250, 250)


def test_fit_odd_sides_and_long_side():
    from reader import ocr_pixels
    src = ocr_pixels.Gray(5, 3, bytes([0, 0, 200, 200, 9,
                                       0, 0, 200, 200, 9,
                                       77, 77, 77, 77, 77]))
    g = ocr_pixels.fit(src, 4)  # the odd last row and column are dropped
    assert (g.w, g.h, g.data) == (2, 1, bytes([0, 200]))
    assert ocr_pixels.fit(src, None, max_side=5) is src
    assert ocr_pixels.reduction(1754, 1240, None, max_side=1000) == (2, 2)
    assert ocr_pixels.reduction(1754, 1240, None, max_side=438) == (4, 4)
    thin = ocr_pixels.Gray(1, 64, bytes(range(64)))  # a side of 1 is not halved further
    t = ocr_pixels.fit(thin, 16)
    assert (t.w, t.h) == (1, 16) and ocr_pixels.reduction(1, 64, 16) == (1, 4)
    assert abs(t.data[0] - 1) <= 1 and abs(t.data[15] - 61) <= 1


def test_rotate_quarter_turns():
    from reader import ocr_pixels
    g = ocr_pixels.Gray(3, 2, b"abcdef")  # rows "abc", "def"
    r90 = ocr_pixels.rotate(g, 90)
    assert (r90.w, r90.h, r90.data) == (2, 3, b"daebfc")
    r270 = ocr_pixels.rotate(g, 270)
    assert (r270.w, r270.h, r270.data) == (2, 3, b"cfbead")
    assert ocr_pixels.rotate(g, 180).data == b"fedcba" and ocr_pixels.rotate(g, 0) == g
    x = g
    for _ in range(4):
        x = ocr_pixels.rotate(x, 90)
    assert x == g


def test_crop_png_box():
    from reader import ocr_pixels
    g = ocr_pixels.Gray(4, 2, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    png = ocr_pixels.crop_png(g, (0.5, 0.0, 1.0, 1.0), 1024)
    assert images.png_pixels(png) == (2, 2, 1, bytes([3, 4, 7, 8]))
    big = ocr_pixels.Gray(100, 40, bytes(range(100)) * 40)
    w, h, ch, raw = images.png_pixels(ocr_pixels.crop_png(big, (0.0, 0.0, 1.0, 1.0), 25))
    assert (w, h, ch) == (25, 10, 1) and raw[:3] == bytes([0, 4, 8])  # every 4th row and column


def gdip_bitmap_bytes(w, h, bgra: bytes, clsid: str) -> bytes:
    """A w x h BGRA buffer encoded in memory by GDI+ with the encoder `clsid`."""
    import uuid
    g = raster._gdip()
    ct = g.ct
    buf = ct.create_string_buffer(bgra, len(bgra))
    bmp = ct.c_void_p()
    raster._ok(g.GdipCreateBitmapFromScan0(w, h, 4 * w, raster._ARGB32, buf, ct.byref(bmp)))
    try:
        out = raster._stream(g, b"")
        try:
            enc = g.GUID.from_buffer_copy(uuid.UUID(clsid).bytes_le)
            raster._ok(g.GdipSaveImageToStream(bmp, out, ct.byref(enc), None))
            return raster._stream_bytes(g, out)
        finally:
            raster._release(g, out)
    finally:
        g.GdipDisposeImage(bmp)


JPEG_CLSID = "{557CF401-1A04-11D3-9A73-0000F81EF32E}"


@needs_gdiplus
def test_gray_jpeg_via_gdiplus():
    w, h = 256, 32
    ramp = bytes(x for _ in range(h) for x in range(w))
    jpg = gdip_bitmap_bytes(w, h, b"".join(bytes([v, v, v, 255]) for v in ramp), JPEG_CLSID)
    assert images.sniff(jpg) == "jpg"
    g, dpi = raster.gray(jpg)
    assert (g.w, g.h) == (w, h) and dpi == 96.0, (g.w, g.h, dpi)
    worst = max(abs(a - b) for a, b in zip(g.data, ramp))
    assert worst <= 8, worst
    small, dpi2 = raster.gray(jpg, max_pixels=w * h // 4)
    assert (small.w, small.h) == (w // 2, h // 2) and dpi2 == 48.0
    assert abs(small.data[5 * small.w + 64] - 129) <= 8  # columns 128 and 129 of the ramp
    assert raster.gray(jpg, page=1) is None  # only a TIFF has pages


@needs_gdiplus
def test_gray_tiff_page():
    data = tiff([(4, 3, 0), (10, 5, 255)])
    g, dpi = raster.gray(data, page=1)
    assert (g.w, g.h) == (10, 5) and set(g.data) == {255} and dpi == 96.0
    g, _ = raster.gray(data)
    assert (g.w, g.h) == (4, 3) and max(g.data) < 30
    assert raster.gray(data, page=2) is None


def peak_working_set() -> int:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
    c = Counters()
    c.cb = ctypes.sizeof(Counters)
    assert k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize


def bilevel_tiff(w, h, rows: bytes, strip_rows: int, dpi: int | None = None) -> bytes:
    """A little-endian uncompressed 1-bit TIFF (WhiteIsZero: a set bit is black) of packed `rows`; with `dpi`, its
    X and Y resolution in pixels per inch, else none at all."""
    line = (w + 7) // 8
    strips = [(y, min(strip_rows, h - y)) for y in range(0, h, strip_rows)]
    n = 9 if dpi is None else 12
    head = 8
    ifd_size = 2 + 12 * n + 4
    offs_at = head + ifd_size
    counts_at = offs_at + 4 * len(strips)
    res_at = counts_at + 4 * len(strips)
    pix = res_at + (0 if dpi is None else 8)
    offsets = [pix + y * line for y, _ in strips]
    counts = [k * line for _, k in strips]
    one = len(strips) == 1
    tags = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, 1), (259, 3, 1, 1), (262, 3, 1, 0),
            (273, 4, len(strips), offsets[0] if one else offs_at), (277, 3, 1, 1), (278, 4, 1, strip_rows),
            (279, 4, len(strips), counts[0] if one else counts_at)]
    if dpi is not None:  # XResolution and YResolution share one RATIONAL dpi/1; ResolutionUnit inch
        tags += [(282, 5, 1, res_at), (283, 5, 1, res_at), (296, 3, 1, 2)]
    out = bytearray(b"II*\x00" + struct.pack("<I", head) + struct.pack("<H", n))
    for tag, typ, cnt, val in tags:
        out += struct.pack("<HHI", tag, typ, cnt) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
    out += b"\0\0\0\0"
    out += struct.pack(f"<{len(strips)}I", *offsets) + struct.pack(f"<{len(strips)}I", *counts)
    if dpi is not None:
        out += struct.pack("<II", dpi, 1)
    assert len(out) == pix
    return bytes(out) + rows


@needs_gdiplus
def test_gray_dpi_from_the_picture():
    """The picture's own resolution when it is 50-2400 dpi, else 96; a fitted picture's dpi is divided by the
    factor."""
    w, h = 64, 32
    rows = (b"\0" * 8) * h
    for dpi, want in ((300, 300.0), (2400, 2400.0), (49, 96.0), (5000, 96.0), (None, 96.0)):
        g, got = raster.gray(bilevel_tiff(w, h, rows, h, dpi))
        assert (g.w, g.h) == (w, h) and got == want, (dpi, got)
    g, got = raster.gray(bilevel_tiff(w, h, rows, h, 300), max_pixels=w * h // 16)
    assert (g.w, g.h, got) == (16, 8, 75.0), (g.w, g.h, got)


@needs_gdiplus
def test_huge_tiff_gray_within_cap():
    """A 20000 x 15000 scan (37.5 MB packed, in memory only) comes back within the pixel cap, its marks in place, and
    the process's peak working set rises by less than 400 MB."""
    w, h = 20000, 15000
    line = w // 8
    white = b"\0" * line
    bar = b"\0" * 500 + b"\xff" * 1500 + b"\0" * 500  # columns 4000-15999 black
    rows = white * 7000 + bar * 1000 + white * 7000
    for strip_rows in (h, 512):  # one 37.5 MB strip, or 512-row strips if GDI+ refuses that
        data = bilevel_tiff(w, h, rows, strip_rows)
        before = peak_working_set()
        t0 = time.monotonic()
        res = raster.gray(data)
        spent = time.monotonic() - t0
        rise = peak_working_set() - before
        del data
        if res is not None:
            break
        print(f"note: GDI+ refused {strip_rows}-row strips")
    assert res is not None
    g, dpi = res
    assert g.w * g.h <= images._MAX_PIXELS and (g.w, g.h) == (w // 8, h // 8), (g.w, g.h)
    assert dpi == 96.0 / 8, dpi  # the file gives no resolution: 96, at 1/8
    k = 8
    assert g.data[(7500 // k) * g.w + 10000 // k] < 60  # inside the bar
    assert g.data[(3000 // k) * g.w + 10000 // k] > 200 and g.data[(7500 // k) * g.w + 1000 // k] > 200
    assert rise < 400 * 2**20, (rise >> 20, spent)
    print(f"note: huge TIFF gray {spent:.2f} s, peak working set +{rise >> 20} MB")


def test_gray_none_for_unknown_bytes():
    assert raster.gray(b"not an image") is None
    assert raster.gray(b"") is None


def test_gray_own_png_without_gdiplus():
    """With no backend the reader's own PNG decoder gives the pixels: RGB, RGBA, gray and a BMP through bmp_to_png."""
    rows = [bytes([10, 20, 30, 40, 50, 60]), bytes([70, 80, 90, 100, 110, 120])]
    with no_backend():
        g, dpi = raster.gray(images._png(2, 2, rows, False, 1))
        assert (g.w, g.h, g.data, dpi) == (2, 2, bytes([20, 50, 80, 110]), 96.0)
        rgba = [bytes([1, 2, 3, 255, 4, 5, 6, 255])]
        assert raster.gray(images._png(2, 1, rgba, True)).__getitem__(0).data == bytes([2, 5])
        from reader import ocr_pixels
        gp = images.gray_png(ocr_pixels.Gray(4, 4, bytes(range(16))))
        g, dpi = raster.gray(gp, max_pixels=4)
        assert (g.w, g.h, dpi) == (2, 2, 48.0) and abs(g.data[0] - 2) <= 1, g
        px = bytes([30, 20, 10]) + b"\0"  # one BGR pixel, row padded to 4
        bmp = (b"BM" + struct.pack("<IHHI", 58, 0, 0, 54)
               + struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + px)
        assert raster.gray(bmp)[0].data == bytes([20])
        assert raster.gray(images._png(2, 2, rows, False, 1), page=1) is None
        jpg_head = b"\xff\xd8\xff\xe0" + b"\0" * 40
        assert raster.gray(jpg_head) is None  # nothing here decodes a JPEG


def test_gray_from_pillow_child_frame():
    """The image venv's gray job answers a GRAY frame (w, h, dpi, then the pixels); a bad frame is not trusted."""
    seen = []
    pixels = bytes(range(6))
    frame = b"GRAY" + struct.pack(">IIf", 3, 2, 150.0) + pixels

    def run(jobs, mono):
        seen.append(jobs)
        return [answer[0]]
    answer = [frame]
    png = images._png(2, 1, [bytes([1, 2, 3, 4, 5, 6])], False)
    with fake_resvg(run):
        g, dpi = raster.gray(png, page=0, max_pixels=100)
        assert (g.w, g.h, g.data, dpi) == (3, 2, pixels, 150.0)
        job = seen[-1][0]
        assert job["op"] == "gray" and job["page"] == 0 and job["max"] == 100
        assert base64.b64decode(job["data"]) == png
        for bad in (frame[:-1], b"GRAX" + frame[4:], b"GRAY" + struct.pack(">IIf", 3, 2, float("nan")) + pixels,
                    b"GRAY" + struct.pack(">IIf", 30, 20, 96.0) + bytes(600), PNG_HEAD + b"xx", None):
            answer[0] = bad
            g, dpi = raster.gray(png, max_pixels=100)  # the reader's own decoder takes over
            assert (g.w, g.h, g.data, dpi) == (2, 1, bytes([2, 5]), 96.0), bad
        answer[0] = None
        assert raster.gray(b"GIF89a" + b"\0" * 30) is None


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
