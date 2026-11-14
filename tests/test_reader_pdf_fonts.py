"""pdfnative fonts: simple and Core 14 fonts, ToUnicode, Adobe CID tables, predefined CMaps,
embedded TrueType cmaps, CID widths and the writing mode.

Run: python -I -X utf8 tests/test_reader_pdf_fonts.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import Name, Ref, Stream, font_type0, simple_pdf, text_ops, truetype_cmap_font  # noqa: E402
import struct  # noqa: E402

from reader.pdfnative import cmap, fonts  # noqa: E402
from reader.pdfnative.cid_unicode import adobe_cid, reverse_truetype_cmap  # noqa: E402
from reader.pdfnative.fonts import font_infos  # noqa: E402
from reader.pdfnative.structure import PDFFile  # noqa: E402

F1 = {"Font": {"F1": Ref(3)}}
F2 = {"Font": {"F2": Ref(100)}}


def load(objects=None, resources=None, name="F2"):
    """The PDFFile and the FontInfo of font `name` in a one-page pdf holding `objects`."""
    resources = resources or F2
    pf = PDFFile(simple_pdf([text_ops("x")], resources=resources, extra_objects=objects))
    return pf, font_infos(pf, pf.pages()[0][1]["Resources"], {})[name]


def test_helvetica_widths():
    pf, info = load(resources=F1, name="F1")
    assert info.decode(b"Hello") == "Hello"
    assert info.widths.advance(ord("A")) == 667  # the AFM width of Helvetica A
    assert not info.unmapped


def test_winansi_differences():
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
            "Encoding": {"BaseEncoding": Name("WinAnsiEncoding"), "Differences": [65, Name("Euro")]}}
    _, info = load({100: font})
    assert info.decode(b"A") == "€"
    assert info.decode(b"B") == "B"


def test_tounicode_bfchar_bfrange():
    objects = font_type0(tounicode=None)
    cmap_data = (b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                 b"1 beginbfchar <0001> <D55C> endbfchar\n"
                 b"1 beginbfrange <0002> <0003> <AE00> endbfrange\n")
    objects[100]["ToUnicode"] = Ref(103)
    objects[103] = Stream({}, cmap_data)
    _, info = load(objects)
    assert info.decode(b"\x00\x01\x00\x02\x00\x03") == "한글긁"
    assert info.code_bytes == 2


def test_tounicode_entry_cap():
    objects = font_type0(tounicode=None)
    objects[100]["ToUnicode"] = Ref(103)
    objects[103] = Stream({}, b"1 beginbfrange <0000> <0063> <AE00> endbfrange\n")
    old = cmap.MAX_MAPPING_ENTRIES
    cmap.MAX_MAPPING_ENTRIES = 10
    try:
        pf, info = load(objects)
    finally:
        cmap.MAX_MAPPING_ENTRIES = old
    out = info.decode(b"".join(code.to_bytes(2, "big") for code in range(12)))
    assert out[:10] == "".join(chr(0xAE00 + k) for k in range(10))
    assert out[10:] == "��"
    assert pf.cuts["tounicode_entries"] == 1, pf.cuts
    assert any("mapping cap" in w for w in pf.warnings)


def test_cid_korea1_without_tounicode():
    cid = next(c for c in range(1, 65536) if adobe_cid("Korea1", c) == "가")
    _, info = load(font_type0(tounicode=None, ordering="Korea1"))
    assert info.decode(cid.to_bytes(2, "big")) == "가"
    assert info.unmapped is False


def test_predefined_uniks():
    objects = font_type0(tounicode=None, ordering="Korea1")
    objects[100]["Encoding"] = Name("UniKS-UCS2-H")
    _, info = load(objects)
    assert info.decode(b"\xac\x00") == "가"
    assert info.wmode == 0


def test_truetype_cmap_reverse():
    assert reverse_truetype_cmap(truetype_cmap_font({5: 0xD55C})) == {5: "한"}
    _, info = load(font_type0(tounicode=None, ordering="Identity", embedded_cmap={5: 0xD55C}))
    assert info.decode(b"\x00\x05") == "한"
    assert info.unmapped is False


def test_unmapped_cid_counts_glyphs():
    pf, info = load(font_type0(tounicode=None, ordering="Identity"))
    assert info.unmapped is True
    assert info.decode(b"\x00\x05") == ""
    assert any("without ToUnicode" in w for w in pf.warnings)


def test_cid_w_array():
    objects = font_type0(tounicode={1: "a"})
    objects[101]["W"] = [1, [500, 600], 10, 20, 700]
    _, info = load(objects)
    assert info.widths.advance(1) == 500
    assert info.widths.advance(2) == 600
    assert info.widths.advance(15) == 700
    assert info.widths.advance(99) == 1000


def test_vertical_wmode():
    _, info = load(font_type0(tounicode={1: "a"}, vertical=True))
    assert info.wmode == 1
    assert info.vertical_metrics is not None


def test_font_cache_by_ref():
    pf = PDFFile(simple_pdf([text_ops("x")], resources=F2, extra_objects=font_type0(tounicode={1: "a"})))
    resources, cache = pf.pages()[0][1]["Resources"], {}
    first = font_infos(pf, resources, cache)["F2"]
    assert font_infos(pf, resources, cache)["F2"] is first and len(cache) == 1


def test_encoding_range_cap_counted():
    # codespace + 2 cidranges fit under the patched cap of 3; the third range is dropped
    a = next(c for c in range(1, 65536) if adobe_cid("Korea1", c) == "가")
    b = next(c for c in range(1, 65536) if adobe_cid("Korea1", c) == "나")
    data = (b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 0 >> def\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"3 begincidrange <0001> <0001> %d <0002> <0002> %d <0003> <0003> %d endcidrange\n"
            % (a, b, a))
    objects = font_type0(tounicode=None, ordering="Korea1")
    objects[100]["Encoding"] = Ref(105)
    objects[105] = Stream({"Type": Name("CMap"),
                           "CIDSystemInfo": {"Registry": b"Adobe", "Ordering": b"Korea1"}}, data)
    old = cmap.MAX_ENCODING_RANGES
    cmap.MAX_ENCODING_RANGES = 3
    try:
        pf, info = load(objects)
    finally:
        cmap.MAX_ENCODING_RANGES = old
    assert info.decode(b"\x00\x01\x00\x02\x00\x03") == "가나"
    assert pf.cuts["encoding_ranges"] == 1, pf.cuts


def test_truetype_font_bytes_cap_counted():
    objects = font_type0(tounicode=None, ordering="Identity", embedded_cmap={5: 0xD55C})
    old = fonts.MAX_FONT_BYTES
    fonts.MAX_FONT_BYTES = 10
    try:
        pf, info = load(objects)
    finally:
        fonts.MAX_FONT_BYTES = old
    assert info.decode(b"\x00\x05") == "" and info.unmapped
    assert pf.cuts["truetype_font_bytes"] == 1, pf.cuts


def test_truetype_fonts_cut_counts_once_per_font():
    objects = font_type0(tounicode=None, ordering="Identity", embedded_cmap={5: 0xD55C})
    old = fonts.MAX_TRUETYPE_CMAP_FONTS
    fonts.MAX_TRUETYPE_CMAP_FONTS = 0
    try:
        pf = PDFFile(simple_pdf([text_ops("x")], resources=F2, extra_objects=objects))
        resources = pf.pages()[0][1]["Resources"]
        for _ in range(3):  # the same font read again with a fresh cache
            assert font_infos(pf, resources, {})["F2"].decode(b"\x00\x05") == ""
    finally:
        fonts.MAX_TRUETYPE_CMAP_FONTS = old
    assert pf.cuts["truetype_fonts"] == 1, pf.cuts


def _cmap_sfnt(subtable, cmap_records=1):
    cmap_table = struct.pack(">HH", 0, cmap_records) + struct.pack(">HHI", 3, 10, 12) + subtable
    return (struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0) + b"cmap"
            + struct.pack(">III", 0, 28, len(cmap_table)) + cmap_table)


def test_truetype_cmap_caps_noted():
    notes = []

    def note(kind, message):
        notes.append(kind)

    # a format 12 group wider than MAX_CMAP_ENTRIES
    group = struct.pack(">III", 0x20000, 0x20000 + 100001, 1)
    sub = struct.pack(">HHIII", 12, 0, 16 + 12, 0, 1) + group
    assert reverse_truetype_cmap(_cmap_sfnt(sub), note) == {}
    assert notes == ["truetype_cmap"], notes
    # more than 256 sfnt tables, and more than 256 cmap records
    notes.clear()
    assert reverse_truetype_cmap(struct.pack(">IHHHH", 0x00010000, 300, 0, 0, 0) + bytes(16 * 300), note) == {}
    assert notes == ["truetype_cmap"], notes
    notes.clear()
    assert reverse_truetype_cmap(_cmap_sfnt(sub, cmap_records=300), note) == {}
    assert notes == ["truetype_cmap"], notes


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
