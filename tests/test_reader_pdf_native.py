"""pdfnative document front end: NativeDoc gives each page as one PageData (fragments, rules,
figure, images, links, fields, comments), with the page caps counted once.

Run: python -I -X utf8 tests/test_reader_pdf_native.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (EncryptSpec, Name, Ref, Stream, acroform, font_type0,  # noqa: E402
                     image_xobject, outline_items, simple_pdf, text_annot, text_ops, widget)

from reader.formats import pdf as pdf_caps  # noqa: E402
from reader.formats.pdf_layout import PageData, PageImage  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.pdfnative import crypto  # noqa: E402
from reader.pdfnative import document as dm  # noqa: E402
from reader.pdfnative.ocg import OCState  # noqa: E402


LATE = 100


def test_native_page_text():
    doc = dm.NativeDoc(simple_pdf([text_ops("One"), text_ops("Two")]))
    assert doc.page_count == 2 and isinstance(doc.oc, OCState)
    page = doc.page(1)
    assert isinstance(page, PageData) and page.number == 2
    assert page.fragments[0].text == "Two" and page.chars == 3 and page.render is None
    assert page.box == (0, 0, 612, 792) and page.rotate == 0
    assert doc.page(0).number == 1


def test_native_crop_inherited():
    pdf = simple_pdf([text_ops("A")])
    doc = dm.NativeDoc(pdf.replace(b"/MediaBox", b"/CropBox [10 20 300 400] /MediaBox", 1))
    assert doc.page(0).box == (10, 20, 300, 400)


def test_native_rotate():
    doc = dm.NativeDoc(simple_pdf([text_ops("A")], page_extra={"Rotate": 90}))
    assert doc.page(0).rotate == 90


def test_native_images():
    draws = b"q 100 0 0 50 72 600 cm /Im1 Do Q"
    data = bytes(range(256)) * 7
    pdf = simple_pdf([draws], resources={"XObject": {"Im1": Ref(30)}},
                     extra_objects={30: image_xobject(40, 40, data=data[:1600])})
    page = dm.NativeDoc(pdf).page(0)
    assert len(page.images) == 1
    got = page.images[0]
    assert isinstance(got, PageImage) and got.box == (72, 600, 172, 650)
    assert got.px == (40, 40) and got.kind == "png" and got.data is None
    data, flat = got.picture()
    assert data[:4] == b"\x89PNG" and not flat


def test_native_image_cap():
    draws = b"q 10 0 0 10 20 20 cm /Im1 Do Q " * 70
    pdf = simple_pdf([draws], resources={"XObject": {"Im1": Ref(30)}},
                     extra_objects={30: image_xobject(2, 2)})
    page = dm.NativeDoc(pdf).page(0)
    assert len(page.images) == 64 and page.cut["image"] == 1, page.cut


def test_native_content_parts_cap():
    pdf = simple_pdf([b""], page_extra={"Contents": [Ref(500 + i) for i in range(300)]},
                     extra_objects={500 + i: Stream({}, text_ops("P%d" % i, y=700 - i))
                                    for i in range(300)})
    page = dm.NativeDoc(pdf).page(0)
    texts = {f.text for f in page.fragments}
    assert "P255" in texts and "P256" not in texts and page.cut["page_part"] == 1, page.cut
    saved = dm.MAX_PAGE_CONTENT_BYTES
    dm.MAX_PAGE_CONTENT_BYTES = 100
    try:
        # two parts of 80 bytes each: the first is read, the second passes the cap
        pdf = simple_pdf([b""], page_extra={"Contents": [Ref(500), Ref(501)]},
                         extra_objects={500: Stream({}, text_ops("A").ljust(80, b" ")),
                                        501: Stream({}, text_ops("B", y=600).ljust(80, b" "))})
        page = dm.NativeDoc(pdf).page(0)
    finally:
        dm.MAX_PAGE_CONTENT_BYTES = saved
    assert [f.text for f in page.fragments] == ["A"] and page.cut["page_part"] == 1, page.cut


def test_native_resources():
    res = {"Font": {"F1": Ref(3)}, "ProcSet": [Name("PDF")]}
    doc = dm.NativeDoc(simple_pdf([text_ops("A")], resources=res))
    got = doc.resources(0)
    assert isinstance(got, dict) and "Font" in got and got["ProcSet"] == [Name("PDF")]


def test_native_open_decrypt_budget():
    saved = crypto.DECRYPT_TOTAL
    crypto.DECRYPT_TOTAL = 64
    try:
        data = simple_pdf([text_ops("Hi" * 40)], encrypt=EncryptSpec(4), xref="stream", objstm=True)
        try:
            dm.NativeDoc(data)
        except ReadError as e:
            assert (e.kind, e.detail) == ("damaged", "decrypt budget"), (e.kind, e.detail)
        else:
            raise AssertionError("expected ReadError")
    finally:
        crypto.DECRYPT_TOTAL = saved


def test_native_fields_comments():
    fields = acroform([widget((72, 700, 200, 720), Ref(4), T="Name", FT=Name("Tx"), V="Kim"),
                       widget((72, 600, 200, 620), Ref(4), T="Long", FT=Name("Tx"), V="x" * 2_500)])
    objects = dict(fields)
    objects[100] = text_annot((10, 10, 20, 20), "note", author="Lee")
    pdf = simple_pdf([text_ops("A")], catalog={"AcroForm": Ref(200)}, extra_objects=objects,
                     page_extra={"Annots": [Ref(100)]})
    doc = dm.NativeDoc(pdf)
    page = doc.page(0)
    assert page.fields[0] == ("Name", "Kim", False, (72, 700, 200, 720)) and len(page.fields[1][1]) == 2_000
    assert [(c.author, c.text) for c in page.comments] == [("Lee", "note")]
    assert page.links == [] and doc.cut["field"] == 1
    assert doc.fields == {0: page.fields} and doc.xfa is False and doc.attachments == []


def test_native_glyphs_once():
    objects = font_type0(tounicode=None, ordering="Identity")
    body = b"BT /F2 12 Tf 72 720 Td <0001> Tj ET"
    pdf = simple_pdf([body, body], resources={"Font": {"F2": Ref(100)}}, extra_objects=objects)
    doc = dm.NativeDoc(pdf)
    doc.page(0)
    doc.page(1)
    assert doc.cut["glyphs"] == 1, doc.cut


def test_native_outline():
    items = [(1, "Intro", 0), (2, "Detail", 1)]
    pdf = simple_pdf([text_ops("A"), text_ops("B")], catalog={"Outlines": Ref(300)},
                     extra_objects=outline_items(items))
    assert dm.NativeDoc(pdf).outline() == [(*it, None) for it in items]


def test_native_late_decrypt_budget():
    saved = crypto.DECRYPT_TOTAL
    crypto.DECRYPT_TOTAL = LATE
    try:
        doc = dm.NativeDoc(simple_pdf([text_ops("Hi" * 40)], encrypt=EncryptSpec(4)))
        page = doc.page(0)
    finally:
        crypto.DECRYPT_TOTAL = saved
    assert page.fragments == [] and doc.pdf.cuts["decrypt_budget"] >= 1
    assert page.cut["page_part"] == 1 and doc.cut["page_part"] == 1, (page.cut, doc.cut)


def test_native_glyphs_direct_font():
    objects = font_type0(tounicode=None, ordering="Identity")
    direct = objects.pop(100)
    objects[101]  # descendant stays an object
    body = b"BT /F2 12 Tf 72 720 Td <0001> Tj ET"
    pdf = simple_pdf([body, body], resources={"Font": {"F2": direct}}, extra_objects=objects)
    doc = dm.NativeDoc(pdf)
    doc.page(0)
    doc.page(1)
    assert doc.cut["glyphs"] == 1, doc.cut


def test_native_page_read_twice():
    doc = dm.NativeDoc(simple_pdf([b"", b""], page_extra={"Contents": [Ref(500 + i) for i in range(300)]},
                                  extra_objects={500 + i: Stream({}, b"") for i in range(300)}))
    doc.page(0)
    again = doc.page(0)
    assert doc.cut["page_part"] == 1 and again.cut["page_part"] == 1, doc.cut


def main():
    assert pdf_caps.PDF_PAGE_IMAGES == 64
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
