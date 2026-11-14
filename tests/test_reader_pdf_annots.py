"""pdfnative annotations, form fields, the outline and page images: links of four schemes,
comments with replies, filled AcroForm values, outline entries, the XFA and attachment flags, and
images as JPEG bytes or a stdlib PNG.

Run: python -I -X utf8 tests/test_reader_pdf_annots.py
"""
import struct
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (JPEG, Name, Ref, Stream, acroform, image_xobject, link_annot,  # noqa: E402
                     outline_items, popup, simple_pdf, text_annot, widget)

from reader import images as reader_images  # noqa: E402
from reader.formats.pdf_layout import Comment, PageImage  # noqa: E402
from reader.pdfnative import annotations as an  # noqa: E402
from reader.pdfnative import images as im  # noqa: E402
from reader.pdfnative.objects import PDFRef  # noqa: E402
from reader.pdfnative.structure import PDFFile  # noqa: E402

RECT = (72, 710, 120, 730)
PAGE = Ref(4)


def with_annots(annots, **kw):
    """(PDFFile, page dict) of a one-page pdf whose /Annots are objects 100 and up."""
    objects = {100 + i: a for i, a in enumerate(annots)}
    objects.update(kw.pop("extra_objects", {}))
    pf = PDFFile(simple_pdf([b""], page_extra={"Annots": [Ref(100 + i) for i in range(len(annots))]},
                            extra_objects=objects, **kw))
    return pf, pf.pages()[0][0]


def with_form(fields, **kw):
    objects = acroform(fields)
    objects.update(kw.pop("extra_objects", {}))
    catalog = dict(kw.pop("catalog", {}), AcroForm=Ref(200))
    return PDFFile(simple_pdf([b""], catalog=catalog, extra_objects=objects, **kw))


def tx(value, name="t", **more):
    return widget(RECT, PAGE, T=name, FT=Name("Tx"), V=value, **more)


def png_pixels(data):
    """(width, height, rows of (r, g, b)) of a PNG made by images._png (colour type 2, filter 0)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(data):
        n, tag = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            size = struct.unpack(">II", body[:8])
            assert body[8:10] == b"\x08\x02"
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    w, h = size
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (1 + 3 * w):(y + 1) * (1 + 3 * w)]
        assert line[0] == 0
        rows.append([tuple(line[1 + 3 * x:4 + 3 * x]) for x in range(w)])
    return w, h, rows


def get_image(stream, extra=None):
    objects = {100: stream}
    objects.update(extra or {})
    pf = PDFFile(simple_pdf([b""], extra_objects=objects))
    return pf, pf.resolve(PDFRef(100, 0))


def decode(stream, extra=None):
    pf, s = get_image(stream, extra)
    return im.image(pf, s)


# -- links --

def test_uri_links():
    pf, page = with_annots([link_annot(RECT, "https://ex.org/a")])
    links, comments, cut = an.page_annots(pf, page)
    assert links == [(RECT, "https://ex.org/a")] and comments == [] and not cut


def test_link_schemes():
    goto = {"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": list(RECT),
            "A": {"S": Name("GoTo"), "D": [PAGE, Name("Fit")]}}
    launch = {"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": list(RECT),
              "A": {"S": Name("Launch"), "F": "x.exe"}}
    pf, page = with_annots([link_annot(RECT, "mailto:a@ex.org"), link_annot(RECT, "ftp://ex.org/f"),
                            link_annot(RECT, "javascript:alert(1)"), goto, launch,
                            link_annot(RECT, "java\tscript:alert(1)"),
                            link_annot(RECT, "file:///etc/passwd")])
    links, comments, cut = an.page_annots(pf, page)
    assert [u for _b, u in links] == ["mailto:a@ex.org", "ftp://ex.org/f"]
    assert comments == [] and not cut
    assert an.SCHEMES == ("http", "https", "mailto", "ftp")


# -- comments --

def test_comments_kinds():
    pf, page = with_annots([text_annot(RECT, "one", author="Ann"),
                            text_annot(RECT, "two", author="Ann", subtype="FreeText"),
                            text_annot(RECT, "three", author="Ann", subtype="Highlight")])
    links, comments, cut = an.page_annots(pf, page)
    assert comments == [Comment("Ann", "one", False), Comment("Ann", "two", False),
                        Comment("Ann", "three", False)]
    assert links == [] and not cut


def test_comment_reply_order():
    pf, page = with_annots([text_annot(RECT, "reply", author="Bo", irt=Ref(101)),
                            text_annot(RECT, "parent", author="Ann")])
    _l, comments, _c = an.page_annots(pf, page)
    assert [c.text for c in comments] == ["parent", "reply"]


def test_popup_and_hidden():
    pf, page = with_annots([text_annot(RECT, "shown", author="Ann"), popup(Ref(100)),
                            text_annot(RECT, "hid", author="Ann", flags=2)])
    _l, comments, _c = an.page_annots(pf, page)
    assert comments == [Comment("Ann", "shown", False), Comment("Ann", "hid", True)]


def test_annot_caps():
    pf, page = with_annots([text_annot(RECT, f"n{i}") for i in range(257)])
    _l, comments, cut = an.page_annots(pf, page)
    assert len(comments) == 256 and cut["annotation"] == 1 and an.PDF_ANNOTS == 256
    pf, page = with_annots([text_annot(RECT, "a" * 70_000)])
    _l, comments, cut = an.page_annots(pf, page)
    assert len(comments) == 1 and len(comments[0].text) == 65_536 and cut["annotation"] == 1


def test_order_comments_pure():
    parent, reply, other = Comment("a", "p", False), Comment("b", "r", False), Comment("c", "o", False)
    assert an.order_comments([("r", "p", reply), ("p", None, parent)]) == [parent, reply]
    # a reply to an unknown parent stays where it is; a reply cycle is listed once
    assert an.order_comments([("o", "gone", other), ("p", None, parent)]) == [other, parent]
    a, b = Comment("a", "a", False), Comment("b", "b", False)
    assert an.order_comments([("a", "b", a), ("b", "a", b)]) == [a, b]


# -- fields --

def test_field_tu_label():
    pf = with_form([{"T": "topmostSubform[0]", "Kids": [
        {"T": "Page1[0]", "Kids": [tx("Kim", "f1_01[0]", TU="Name")]}]}])
    assert an.fields(pf, Counter()) == {0: [("Name", "Kim")]}


def test_field_partial_name():
    pf = with_form([{"T": "topmostSubform[0]", "Kids": [
        {"T": "Page1[0]", "Kids": [tx("Kim", "f1_01[0]")]}]}])
    assert an.fields(pf, Counter()) == {0: [("f1_01[0]", "Kim")]}


def test_checkbox_radio():
    box = widget(RECT, PAGE, T="Agree", FT=Name("Btn"), V=Name("Yes"))
    off = widget(RECT, PAGE, T="Skip", FT=Name("Btn"), V=Name("Off"))
    radio = {"T": "Pick", "FT": Name("Btn"), "Ff": 1 << 15, "V": Name("Two"),
             "Kids": [widget(RECT, PAGE), widget(RECT, PAGE)]}
    pf = with_form([box, off, radio])
    assert an.fields(pf, Counter()) == {0: [("Agree", "Yes"), ("Pick", "Two")]}


def test_empty_sig_button():
    sig = widget(RECT, PAGE, T="s", FT=Name("Sig"), V={"Type": Name("Sig")})
    push = widget(RECT, PAGE, T="p", FT=Name("Btn"), Ff=1 << 16, V="x")
    pf = with_form([tx("", "e"), sig, push])
    assert an.fields(pf, Counter()) == {}


def test_field_newlines_and_cut():
    cut = Counter()
    pf = with_form([tx("a\nb", "n"), tx("c\r\nd", "m"), tx("x" * 2_500, "long")])
    got = an.fields(pf, cut)[0]
    assert got[0] == ("n", "a / b") and got[1] == ("m", "c / d")
    assert len(got[2][1]) == 2_000 and cut["field"] == 1


def test_field_entry_pure():
    assert an.field_entry("Name", "a.b", "Kim", "text") == ("Name", "Kim", False)
    assert an.field_entry(None, "a.b", "x", "text") == ("b", "x", False)
    assert an.field_entry(None, "c", "Off", "check") is None
    assert an.field_entry(None, "c", "Yes", "check") == ("c", "Yes", False)
    assert an.field_entry(None, "s", "v", "sig") is None
    assert an.field_entry(None, "p", "v", "push") is None
    assert an.field_entry(None, "e", "", "text") is None
    assert an.field_entry(None, "l", ["x", "y"], "choice") == ("l", "x, y", False)
    assert an.field_entry(None, "l", "a\nb", "choice") == ("l", "a / b", False)
    label, value, cut = an.field_entry(None, "t", "x" * 2_500, "text")
    assert len(value) == 2_000 and cut is True


def test_field_tree_cycle():
    start = time.time()
    objects = {200: {"Fields": [Ref(201)]},
               201: {"T": "a", "Kids": [Ref(202), Ref(203)]},
               202: {"T": "b", "Parent": Ref(201), "Kids": [Ref(201)]},
               203: widget(RECT, PAGE, T="v", FT=Name("Tx"), V="one", Parent=Ref(201))}
    pf = PDFFile(simple_pdf([b""], catalog={"AcroForm": Ref(200)}, extra_objects=objects))
    got = an.fields(pf, Counter())
    assert got == {0: [("v", "one")]} and time.time() - start < 1


def test_field_depth_cap():
    node = tx("deep", "leaf")
    for i in range(40):
        node = {"T": f"p{i}", "Kids": [node]}
    cut = Counter()
    pf = with_form([node])
    assert an.fields(pf, cut) == {} and cut["field"] >= 1


def test_field_page_scan():
    # no /P: the page whose /Annots lists the widget gets the field
    w = widget(RECT, None, T="t", FT=Name("Tx"), V="v")
    objects = acroform([w])
    pf = PDFFile(simple_pdf([b"", b""], catalog={"AcroForm": Ref(200)}, extra_objects=objects,
                            page_extra=[{}, {"Annots": [Ref(201)]}]))
    assert an.fields(pf, Counter()) == {1: [("t", "v")]}


def test_field_choice_list():
    ch = widget(RECT, PAGE, T="c", FT=Name("Ch"), V=["x", "y"])
    pf = with_form([ch])
    assert an.fields(pf, Counter()) == {0: [("c", "x, y")]}


def test_xfa_attachment():
    pf = PDFFile(simple_pdf([b""], catalog={"AcroForm": {"Fields": [], "XFA": []}}))
    assert an.has_xfa(pf) is True and an.has_attachments(pf) is False
    pf = PDFFile(simple_pdf([b""], catalog={"AcroForm": {"Fields": []}}))
    assert an.has_xfa(pf) is False
    pf = PDFFile(simple_pdf([b""], catalog={"Names": {"EmbeddedFiles": {"Names": []}}}))
    assert an.has_attachments(pf) is True and an.has_xfa(pf) is False
    pf, _page = with_annots([text_annot(RECT, "f", subtype="FileAttachment")])
    assert an.has_attachments(pf) is True


# -- outline --

def outline_pdf(items, **kw):
    objects = outline_items(items)
    mutate = kw.pop("mutate", None)
    if mutate:
        mutate(objects)
    return PDFFile(simple_pdf([b"", b""], catalog={"Outlines": Ref(300)}, extra_objects=objects, **kw))


PAGES = {PDFRef(4, 0): 0, PDFRef(6, 0): 1}


def test_outline():
    items = [(1, "Intro", 0), (2, "Part", 1)]
    assert an.outline(outline_pdf(items), PAGES) == items
    items = [(1, "A", 0), (2, "A1", 1), (2, "A2", 1), (1, "B", 1)]
    assert an.outline(outline_pdf(items), PAGES) == items
    assert an.outline(outline_pdf([]), PAGES) == []


def test_outline_cycle():
    def loop(objects):
        objects[302]["Next"] = Ref(301)
        objects[301]["First"] = Ref(301)

    pf = outline_pdf([(1, "Intro", 0), (1, "End", 1)], mutate=loop)
    assert an.outline(pf, PAGES) == [(1, "Intro", 0), (1, "End", 1)]


def test_outline_caps():
    pf = outline_pdf([(1, f"s{i}", 0) for i in range(1_001)])
    got = an.outline(pf, PAGES)
    assert len(got) == 1_000 and pf.cuts["outline"] == 1
    assert an.OUTLINE_ITEMS == 1_000 and an.OUTLINE_DEPTH == 32
    pf = outline_pdf([(d, f"d{d}", 0) for d in range(1, 41)])
    got = an.outline(pf, PAGES)
    assert len(got) == 32 and got[-1][0] == 32 and pf.cuts["outline"] == 1


def test_outline_named_dest_and_action():
    objects = outline_items([(1, "Chap", 0), (1, "Sec", 0), (1, "Web", 0), (1, "Lost", 0),
                             (1, "Dict", 0)])
    objects[301]["Dest"] = Name("chap")  # the catalog's /Dests dictionary
    del objects[302]["Dest"]
    objects[302]["A"] = {"S": Name("GoTo"), "D": b"sec"}  # the /Names /Dests name tree
    del objects[303]["Dest"]
    objects[303]["A"] = {"S": Name("URI"), "URI": "https://ex.org"}
    objects[304]["Dest"] = b"nope"
    objects[305]["Dest"] = b"zzz"  # a name tree value that is a dictionary with /D
    objects[400] = {"Names": [b"sec", [Ref(4), Name("Fit")], b"zzz", {"D": [Ref(6), Name("Fit")]}]}
    pf = PDFFile(simple_pdf([b"", b""], extra_objects=objects, catalog={
        "Outlines": Ref(300), "Dests": {"chap": [Ref(6), Name("Fit")]}, "Names": {"Dests": Ref(400)}}))
    assert an.outline(pf, PAGES) == [(1, "Chap", 1), (1, "Sec", 0), (1, "Dict", 1)]
    assert pf.cuts["outline"] == 2  # the web link and the missing name have no page: dropped, counted


def test_page_index():
    pf = outline_pdf([(1, "x", 0)])
    assert an.page_index(pf) == PAGES


# -- images --

def test_image_dct_kept():
    got = decode(image_xobject(8, 8, filter="DCTDecode", data=JPEG))
    assert isinstance(got, PageImage) and got.data == JPEG and got.kind == "jpg" and got.px == (8, 8)
    assert JPEG[:3] == b"\xff\xd8\xff"


def test_image_gray_png():
    data = bytes(range(256)) * 7
    got = decode(image_xobject(40, 40, data=data[:1600]))
    assert reader_images.sniff(got.data) == "png" and got.px == (40, 40) and got.kind == "png"
    w, h, rows = png_pixels(got.data)
    assert (w, h) == (40, 40) and rows[0][3] == (3, 3, 3) and rows[1][0] == (40, 40, 40)


def test_image_gray_low_bpc():
    got = decode(image_xobject(8, 1, bpc=1, data=bytes([0b10100000])))
    assert [p[0] for p in png_pixels(got.data)[2][0]] == [255, 0, 255, 0, 0, 0, 0, 0]
    got = decode(image_xobject(3, 1, bpc=2, data=bytes([0b00011011])))
    assert [p[0] for p in png_pixels(got.data)[2][0]] == [0, 85, 170]
    got = decode(image_xobject(2, 1, bpc=4, data=bytes([0x0F])))
    assert [p[0] for p in png_pixels(got.data)[2][0]] == [0, 255]
    assert decode(image_xobject(2, 1, bpc=16, data=bytes(4))).data is None


def test_image_indexed_png():
    palette = bytes([10, 20, 30, 200, 100, 50, 7, 8, 9])
    got = decode(image_xobject(2, 1, cs="Indexed", bpc=4, palette=palette, data=bytes([0x10])))
    w, h, rows = png_pixels(got.data)
    assert rows[0] == [(200, 100, 50), (10, 20, 30)]
    got = decode(image_xobject(2, 1, cs="Indexed", bpc=8, palette=palette, data=bytes([2, 0])))
    assert png_pixels(got.data)[2][0] == [(7, 8, 9), (10, 20, 30)]
    # an index past the palette is black, not an error
    got = decode(image_xobject(1, 1, cs="Indexed", bpc=8, palette=palette, data=bytes([9])))
    assert png_pixels(got.data)[2][0] == [(0, 0, 0)]


def test_image_indexed_gray_base():
    cs = [Name("Indexed"), Name("DeviceGray"), 1, bytes([5, 250])]
    got = decode(image_xobject(2, 1, cs=cs, bpc=8, data=bytes([1, 0])))
    assert png_pixels(got.data)[2][0] == [(250, 250, 250), (5, 5, 5)]


def test_image_icc_and_cal():
    pixels = bytes([1, 2, 3, 4, 5, 6])
    got = decode(image_xobject(2, 1, cs=[Name("ICCBased"), Ref(101)], data=pixels),
                 {101: Stream({"N": 3}, b"")})
    assert png_pixels(got.data)[2][0] == [(1, 2, 3), (4, 5, 6)]
    got = decode(image_xobject(2, 1, cs=[Name("ICCBased"), Ref(101)], data=bytes([9, 8])),
                 {101: Stream({"N": 1}, b"")})
    assert png_pixels(got.data)[2][0] == [(9, 9, 9), (8, 8, 8)]
    got = decode(image_xobject(1, 1, cs=[Name("CalRGB"), {"WhitePoint": [1, 1, 1]}], data=pixels[:3]))
    assert png_pixels(got.data)[2][0] == [(1, 2, 3)]
    got = decode(image_xobject(1, 1, cs=[Name("CalGray"), {"WhitePoint": [1, 1, 1]}], data=b"\x07"))
    assert png_pixels(got.data)[2][0] == [(7, 7, 7)]
    assert decode(image_xobject(1, 1, cs=[Name("ICCBased"), Ref(101)], data=bytes(4)),
                  {101: Stream({"N": 4}, b"")}).data is None


def test_image_predictor_rgb():
    rows = [[(10, 20, 30), (11, 22, 33), (15, 25, 35)], [(0, 0, 0), (1, 1, 1), (2, 2, 2)]]
    raw = b""
    for row in rows:  # PNG filter type 1 (Sub), bytes per pixel 3
        flat = bytes(c for px in row for c in px)
        raw += b"\x01" + bytes((flat[i] - (flat[i - 3] if i >= 3 else 0)) & 255
                               for i in range(len(flat)))
    parms = {"Predictor": 15, "Columns": 3, "Colors": 3, "BitsPerComponent": 8}
    got = decode(image_xobject(3, 2, cs="DeviceRGB", data=raw, parms=parms))
    assert got.kind == "png" and png_pixels(got.data)[2] == rows


def test_image_unfiltered_and_decode_invert():
    got = decode(image_xobject(2, 1, filter=None, data=bytes([0, 255])))
    assert [p[0] for p in png_pixels(got.data)[2][0]] == [0, 255]
    s = image_xobject(2, 1, filter=None, data=bytes([0, 255]))
    s.dictionary["Decode"] = [1, 0]
    assert [p[0] for p in png_pixels(decode(s).data)[2][0]] == [255, 0]


def test_image_not_viewable():
    for name, kind in (("JPXDecode", "jpx"), ("CCITTFaxDecode", "ccitt"), ("JBIG2Decode", "jbig2")):
        got = decode(image_xobject(8, 8, filter=name, data=b"xx"))
        assert got.data is None and got.kind == kind and got.px == (8, 8), name
    got = decode(image_xobject(4, 4, cs="DeviceCMYK"))
    assert got.data is None and got.kind == "raw"
    got = decode(image_xobject(5_000, 5_000))
    assert got.data is None and got.px == (5_000, 5_000)
    got = decode(image_xobject(4, 4, data=bytes(3)))  # fewer samples than the size needs
    assert got.data is None
    got = decode(image_xobject(4, 4, cs=[Name("Separation"), Name("Spot"), Name("DeviceRGB"), {}]))
    assert got.data is None
    got = decode(image_xobject(2, 2, filter="DCTDecode", data=b"not a jpeg"))
    assert got.data is None


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
