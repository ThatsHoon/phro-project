"""Image sniffing, BMP to PNG, asset files and image rendering.

Run: python -I -X utf8 tests/test_reader_images.py
"""
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import images, markdown  # noqa: E402
from reader.model import Cell, Doc, Image, Para, Table, Text  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 20
EMF = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 8
WMF = b"\xd7\xcd\xc6\x9a" + b"\x00" * 20


def bmp(w, h, bpp, pixels, palette=b"", comp=0, masks=b"", hdr=40):
    head = struct.pack("<IiiHHIIiiII", hdr, w, h, 1, bpp, comp, len(pixels), 0, 0,
                       len(palette) // 4, 0) + masks
    off = 14 + len(head) + len(palette)
    return b"BM" + struct.pack("<IHHI", off + len(pixels), 0, 0, off) + head + palette + pixels


def idat_of(png):
    pos, out = 8, b""
    while pos < len(png):
        n, tag = struct.unpack(">I4s", png[pos:pos + 8])
        if tag == b"IDAT":
            out += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    return out


def test_sniff():
    assert images.sniff(PNG) == "png"
    assert images.sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 12) == "jpg"
    assert images.sniff(GIF) == "gif"
    assert images.sniff(b"BM" + b"\x00" * 20) == "bmp"
    assert images.sniff(b"II*\x00" + b"\x00" * 12) == "tiff"
    assert images.sniff(b"MM\x00*" + b"\x00" * 12) == "tiff"
    assert images.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert images.sniff(WMF) == "wmf"
    assert images.sniff(b"\x01\x00\x09\x00" + b"\x00" * 12) == "wmf"
    assert images.sniff(EMF) == "emf"
    assert images.sniff(b'<?xml version="1.0"?><svg xmlns="x"/>') == "svg"
    assert images.sniff(b"<svg xmlns='x'></svg>") == "svg"
    assert images.sniff(b"hello") is None
    assert images.sniff(b"\x01\x00\x00\x00" + b"\x00" * 40) is None


def test_bmp24_to_png_pixels():
    # bottom-up: first stored row is the bottom (BL blue, BR white), rows padded to 4 bytes
    pix = b"\xff\x00\x00" + b"\xff\xff\xff" + b"\x00\x00" + b"\x00\x00\xff" + b"\x00\xff\x00" + b"\x00\x00"
    png = images.bmp_to_png(bmp(2, 2, 24, pix))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (2, 2)
    assert png[24:26] == b"\x08\x02"
    raw = zlib.decompress(idat_of(png))
    assert raw == b"\x00\xff\x00\x00\x00\xff\x00" + b"\x00\x00\x00\xff\xff\xff\xff"


def test_bmp_top_down_and_32bit_alpha():
    pix = b"\x01\x02\x03\x80" + b"\x04\x05\x06\x00"  # 2x1 BGRA
    png = images.bmp_to_png(bmp(2, -1, 32, pix))
    assert png[24:26] == b"\x08\x06"
    assert zlib.decompress(idat_of(png)) == b"\x00\x03\x02\x01\x80\x06\x05\x04\x00"
    noalpha = images.bmp_to_png(bmp(1, 1, 32, b"\x01\x02\x03\x00"))
    assert noalpha[24:26] == b"\x08\x02"
    assert zlib.decompress(idat_of(noalpha)) == b"\x00\x03\x02\x01"


def test_bmp8_palette():
    pal = b"\x01\x02\x03\x00" + b"\x00" * 4
    png = images.bmp_to_png(bmp(1, 1, 8, b"\x00\x00\x00\x00", palette=pal))
    assert zlib.decompress(idat_of(png)) == b"\x00\x03\x02\x01"


def test_bmp1_bit_palette():
    pal = b"\x00\x00\x00\x00" + b"\xff\xff\xff\x00"
    png = images.bmp_to_png(bmp(2, 1, 1, b"\x40\x00\x00\x00", palette=pal))  # bits 01
    assert zlib.decompress(idat_of(png)) == b"\x00\x00\x00\x00\xff\xff\xff"


def test_bmp16_bitfields():
    masks = struct.pack("<III", 0xF800, 0x07E0, 0x001F)
    png = images.bmp_to_png(bmp(1, 1, 16, b"\x00\xf8\x00\x00", masks=masks, comp=3))
    assert zlib.decompress(idat_of(png)) == b"\x00\xff\x00\x00"


def test_bmp_hostile_headers_and_speed():
    import time
    z8 = b"\x00" * 8
    t0 = time.time()
    assert images.bmp_to_png(bmp(65535, 65535, 1, z8, palette=z8)) is None
    assert images.bmp_to_png(bmp(2 ** 31 - 1, 1, 24, z8)) is None
    assert images.bmp_to_png(bmp(4097, 4096, 1, z8, palette=z8)) is None  # over the pixel cap
    data = bytearray(bmp(1, 1, 24, b"\x00" * 4))
    struct.pack_into("<I", data, 10, 10 ** 9)
    assert images.bmp_to_png(bytes(data)) is None
    assert time.time() - t0 < 1
    pal = b"\x09\x09\x09\x00" + b"\x01\x02\x03\x00"
    png = images.bmp_to_png(bmp(1, 1, 8, b"\x07\x00\x00\x00", palette=pal))  # index 7, 2 entries
    assert zlib.decompress(idat_of(png)) == b"\x00\x00\x00\x00"
    t0 = time.time()
    big = images.bmp_to_png(bmp(2000, 2000, 24, bytes(2000 * 3 * 2000)))
    assert big is not None and time.time() - t0 < 5
    t0 = time.time()
    assert images.bmp_to_png(bmp(4000, 4000, 1, bytes(500 * 4000), palette=z8)) is not None
    assert time.time() - t0 < 5


def test_bmp_rle_not_converted():
    data = bmp(1, 1, 8, b"\x00" * 4, palette=b"\x00" * 8, comp=1)
    assert images.bmp_to_png(data) is None
    assert images.bmp_to_png(data[:30]) is None
    assert images.bmp_to_png(bmp(1, 1, 24, b"\x00" * 4)[:-2]) is None
    with tempfile.TemporaryDirectory() as d:
        path, ok = images.Assets(Path(d)).add(data)
        assert path == "assets/img-001.bmp" and ok is False
        assert (Path(d) / "img-001.bmp").exists()


def test_assets_dedupe_and_names():
    with tempfile.TemporaryDirectory() as d:
        a = images.Assets(Path(d) / "assets")
        assert a.add(PNG) == ("assets/img-001.png", True)
        assert a.add(PNG) == ("assets/img-001.png", True)
        assert a.add(GIF) == ("assets/img-002.gif", True)
        assert a.add(EMF) == ("assets/img-003.emf", False)
        assert a.add(b"tiny") == ("", False)
        assert a.add(b"x" * 40) == ("", False)
        assert sorted(p.name for p in (Path(d) / "assets").iterdir()) == [
            "img-001.png", "img-002.gif", "img-003.emf"]
        b = images.Assets(Path(d) / "b")
        assert b.add(bmp(1, 1, 24, b"\x00" * 4)) == ("assets/img-001.png", True)


def test_render_image_links():
    with tempfile.TemporaryDirectory() as d:
        doc = Doc(blocks=[Image(PNG, alt="로고"), Image(EMF)])
        md = markdown.render(doc, Path(d))
        assert "![로고](assets/img-001.png)" in md
        assert md.rstrip().endswith("Not viewable here: assets/img-002.emf")
        assert md.count("Not viewable") == 1
        assert (Path(d) / "img-001.png").exists() and (Path(d) / "img-002.emf").exists()


def test_render_not_viewable_alt_and_list():
    with tempfile.TemporaryDirectory() as d:
        doc = Doc(blocks=[Image(EMF, alt="chart"), Image(WMF), Image(b"tiny", alt="x"), Image(b"tiny")])
        md = markdown.render(doc, Path(d))
        assert md == "[image: chart]\n\n[image: x]\n\nNot viewable here: assets/img-001.emf, assets/img-002.wmf\n"


def test_render_alt_escaping_and_cells():
    with tempfile.TemporaryDirectory() as d:
        md = markdown.render(Doc(blocks=[Image(PNG, alt="a]b\\c")]), Path(d))
        assert md == "![a\\]b\\\\c](assets/img-001.png)\n"
        t = Table([[Cell([Image(PNG, alt="p|q")]), Cell([Para([Text("z")])])]])
        md = markdown.render(Doc(blocks=[t]), Path(d))
        assert "|![p\\|q](assets/img-001.png)|z|" in md


def test_render_images_in_cells():
    with tempfile.TemporaryDirectory() as d:
        t = Table([[Cell([Image(PNG, alt="a")]), Cell([Para([Text("z")])])]])
        md = markdown.render(Doc(blocks=[t]), Path(d))
        assert "|![a](assets/img-001.png)|z|" in md
        t = Table([[Cell([Image(EMF, alt="<e>")]), Cell([Image(WMF)], colspan=2)],
                   [Cell([Para([Text("y")])]), Cell([Para([Text("w")])]), Cell([Para([Text("v")])])]])
        md = markdown.render(Doc(blocks=[t]), Path(d))
        assert "<td>[image: &lt;e>]</td>" in md
        assert md.rstrip().endswith("Not viewable here: assets/img-001.emf, assets/img-002.wmf")
        inner = Table([[Cell([Image(PNG)]), Cell([Para([Text("n")])])]])
        outer = Table([[Cell([inner]), Cell([Para([Text("o")])])]])
        md = markdown.render(Doc(blocks=[outer]), Path(d))
        assert "![](assets/img-001.png) / n" in md


def test_render_no_images_creates_no_dir():
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "assets"
        markdown.render(Doc(blocks=[Para([Text("hi")])]), target)
        assert not target.exists()


def test_truncated_output_keeps_marker_last():
    from reader import limits
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 300
    try:
        with tempfile.TemporaryDirectory() as d:
            blocks = [Para([Text("line %d " % i * 5)]) for i in range(60)] + [Image(EMF)]
            doc = Doc(blocks=blocks, notes={"n": [Para([Text("note")])]})
            md = markdown.render(doc, Path(d))
    finally:
        limits.MAX_OUTPUT = old
    assert md.rstrip().endswith("(output truncated at 20 MB)")
    assert "Not viewable" not in md


def test_sniff_phase3_kinds():
    wpg = b"\xffWPC" + (16).to_bytes(4, "little") + bytes([1, 0x16, 1, 0]) + bytes(20)
    wpd = b"\xffWPC" + (16).to_bytes(4, "little") + bytes([1, 0x0A, 2, 1]) + bytes(20)
    assert images.sniff(wpg) == "wpg" and images.sniff(wpd) is None
    assert images.sniff(bytes(4) + b"ftypheic" + bytes(20)) == "heic" == images.sniff(bytes(4) + b"ftypmif1" + bytes(20))
    assert images.sniff(bytes(4) + b"ftypheix" + bytes(20)) == "heic"
    assert images.sniff(b"%PDF-1.4\n" + bytes(20)) == "pdf"
    assert not {"wpg", "heic", "pdf"} & images.VIEWABLE
    with tempfile.TemporaryDirectory() as d:
        assert images.Assets(Path(d) / "assets").add(wpg) == ("assets/img-001.wpg", False)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
