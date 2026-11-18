"""Figures to PNG: target sizes, figure text and its warnings, the time reserve, the EMF writer, and GDI+ on Windows.

Run: python -I -X utf8 tests/test_reader_raster.py
Every fixture is built in memory and every PNG is checked in memory.
"""
import os
import random
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import (emf, emf_glyphs, emf_brush, emf_rect, emf_textw, pict, pict_longtext,  # noqa: E402
                          pict_op, wmf, wmf_brush, wmf_font, wmf_rect, wmf_select, wmf_text, wmf_window, wpg1,
                          wpg1_fill, wpg1_rect, wpg1_text)
from reader import emf_write, fonts, limits, metafile, raster  # noqa: E402
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
    assert raster.target_size(48, 48) == (96, 96)
    assert raster.target_size(4000, 1000) == (1568, 392)
    assert raster.target_size(100, 50) == (200, 100)
    assert raster.target_size(1, 1) == (2, 2)
    assert raster.target_size(0, -3) == (2, 2)
    assert raster.target_size(10000, 1) == (1568, 1)  # each side stays at least 1
    old = limits.FIGURE_EDGE
    limits.FIGURE_EDGE = 10_000  # the pixel cap holds when the edge does not
    try:
        w, h = raster.target_size(8000, 8000)
        assert w * h <= 16_777_216 and w == h and w > 4000
    finally:
        limits.FIGURE_EDGE = old


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
    assert a in ("gdiplus", "none")
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


# ---- GDI+ ------------------------------------------------------------------------------------------------------------

def counted(data: bytes) -> bytes:
    """A fixture EMF with nRecords and nHandles filled in, as every real EMF has them: GDI refuses a header with
    none."""
    b, pos, n = bytearray(data), 108, 1
    while pos < len(b):
        pos += struct.unpack_from("<I", b, pos + 4)[0]
        n += 1
    struct.pack_into("<IHH", b, 52, n, 16, 0)
    return bytes(b)


@needs_gdiplus
def test_gdiplus_wmf_png_size():
    data = wmf([wmf_text(100, 200, b"Hello")])  # placeable 1440 x 1440 at 1440 a inch: 96 px
    f = raster.convert("wmf", data)
    assert f.png is not None and f.lines == ["Hello"]
    assert png_size(f.png) == raster.target_size(96, 96) == (192, 192)
    data = wmf([wmf_text(100, 200, b"Wide")], placeable=(0, 0, 2880, 720, 1440))
    assert png_size(raster.convert("wmf", data).png) == (384, 96)


@needs_gdiplus
def test_gdiplus_emf_png():
    data = counted(emf([emf_brush(0x000000), emf_rect(0, 1270, 1270, 2540), emf_textw(100, 200, "Hi")]))
    f = raster.convert("emf", data)
    assert f.png[:8] == b"\x89PNG\r\n\x1a\n" and f.lines == ["Hi"]
    want = raster.target_size(*metafile.parse(data).size_px())
    assert png_size(f.png) == want == (192, 192)
    w, h, rows = png_pixels(f.png)
    assert dark(rows[h * 3 // 4][w // 4]) and not dark(rows[h // 4][w * 3 // 4])  # the black lower left quarter


@needs_gdiplus
def test_gdiplus_text_pixels():
    data = wmf([wmf_font(-72), wmf_select(0), wmf_text(20, 10, b"X")], placeable=(0, 0, 200, 100, 96))
    f = raster.convert("wmf", data)
    assert f.lines == ["X"]
    w, h, rows = png_pixels(f.png)
    assert (w, h) == (400, 200)
    assert dark_count(rows, 40, 20, 170, 180) > 300  # the X, at twice its size
    assert dark_count(rows, 200, 0, 400, 200) == 0   # nothing to its right


@needs_gdiplus
def test_gdiplus_wmf_without_placeable():
    data = wmf([*wmf_window((0, 0), (300, 150)), wmf_brush(0x000000), wmf_select(0), wmf_rect(0, 75, 150, 150),
                wmf_text(200, 20, b"Plain")], placeable=None)
    f = raster.convert("wmf", data)
    assert f.lines == ["Plain"] and f.png is not None
    w, h, rows = png_pixels(f.png)
    assert (w, h) == (600, 300)
    assert dark(rows[225][150]) and not dark(rows[75][450]) and not dark(rows[225][450])
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
    assert (w, h) == (192, 192)
    assert dark(rows[150][40]) and not dark(rows[150][150])


@needs_gdiplus
def test_gdiplus_wpg_via_emf():
    data = wpg1([wpg1_fill(1, 0), wpg1_rect(0, 0, 600, 600), wpg1_text(700, 900, b"Wpg")])
    f = raster.convert("wpg", data)
    assert f.lines == ["Wpg"] and f.png is not None
    w, h, rows = png_pixels(f.png)
    assert (w, h) == (192, 192)
    assert dark(rows[150][40]) and not dark(rows[40][40])


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
    assert png_size(one.png) == raster.target_size(4, 3) == (8, 6) and one.lines == []
    two = raster.convert("tiff", data, page=1)
    assert png_size(two.png) == raster.target_size(10, 5) == (20, 10)
    _, _, rows = png_pixels(two.png)
    assert rows[5][10] == (255, 255, 255)
    _, _, rows = png_pixels(one.png)
    assert dark(rows[3][4])
    assert raster.convert("tiff", data, page=2).png is None


@needs_gdiplus
def test_gdiplus_garbage_none():
    rnd = random.Random(7)
    for kind in ("wmf", "emf", "pict", "wpg", "tiff"):
        for n in (0, 16, 300, 5000):
            f = raster.convert(kind, rnd.randbytes(n))
            assert f.png is None, kind
    assert raster.tiff_pages(rnd.randbytes(100)) == 0


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
