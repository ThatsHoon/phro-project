"""pdfnative content streams: text fragments, rule segments, figure paths, image draws, OCG and
OCMD visibility, forms and the operator caps.

Run: python -I -X utf8 tests/test_reader_pdf_content.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (Name, Ref, Stream, font_type0, form_xobject, ocg_catalog,  # noqa: E402
                     rule_ops, simple_pdf, text_ops)

from reader.formats import pdf as pdf_caps  # noqa: E402
from reader.formats import pdf_tables  # noqa: E402
from reader.formats.pdf_layout import writing_direction  # noqa: E402
from reader.pdfnative import content as cm  # noqa: E402
from reader.pdfnative.ocg import OCState  # noqa: E402
from reader.pdfnative.objects import PDFRef  # noqa: E402
from reader.pdfnative.structure import PDFFile  # noqa: E402

F1 = {"Font": {"F1": Ref(3)}}
BOX = (0, 0, 612, 792)
OCG = {"Type": Name("OCG"), "Name": "Layer"}


def res(**more):
    return dict(F1, **more)


def load(content, resources=None, extra=None, catalog=None):
    pf = PDFFile(simple_pdf([content], resources=resources or F1, extra_objects=extra,
                            catalog=catalog))
    return pf, pf.pages()[0][1]["Resources"]


def run(content, resources=None, extra=None, catalog=None):
    pf, resources = load(content, resources, extra, catalog)
    return cm.interpret(pf, content, resources, {}, OCState.from_catalog(pf), box=BOX)


def circle(cx, cy, r):
    k = 0.5523 * r
    return (b"%g %g m %g %g %g %g %g %g c %g %g %g %g %g %g c %g %g %g %g %g %g c "
            b"%g %g %g %g %g %g c f " % (
                cx + r, cy, cx + r, cy + k, cx + k, cy + r, cx, cy + r,
                cx - k, cy + r, cx - r, cy + k, cx - r, cy,
                cx - r, cy - k, cx - k, cy - r, cx, cy - r,
                cx + k, cy - r, cx + r, cy - k, cx + r, cy))


def test_text_fragment():
    pc = run(text_ops("Hello"))
    assert len(pc.fragments) == 1
    f = pc.fragments[0]
    assert (f.x, f.y, f.size, f.text, f.font) == (72, 720, 12, "Hello", "Helvetica")
    assert f.hidden == "" and f.invisible is False and f.obj == 0 and f.order == 0
    assert abs(f.width - (722 + 556 + 222 + 222 + 556) * 12 / 1000) < 1e-6
    assert pc.objects == 1 and not pc.cut


def test_ctm_scales():
    f = run(b"2 0 0 2 0 0 cm " + text_ops("A", x=10, y=10)).fragments[0]
    assert (f.x, f.y, f.size) == (20, 20, 24)


def test_tj_moves_not_spaces():
    pc = run(b"BT /F1 12 Tf 72 720 Td [(He) -2000 (llo)] TJ ET")
    assert len(pc.fragments) == 1
    f = pc.fragments[0]
    assert f.text == "Hello"
    o = f.char_offsets
    assert len(o) == 6
    # char_offsets[k] is where glyph k starts; the 24-unit jump follows "e"
    assert abs(o[1] - 722 * 12 / 1000) < 1e-6
    assert abs((o[2] - o[1]) - (556 * 12 / 1000 + 24)) < 1e-6
    assert abs((o[3] - o[2]) - 222 * 12 / 1000) < 1e-6
    assert abs(f.width - o[-1]) < 1e-9


def test_char_gaps():
    # [(He) -2000 (llo)]: 24 units before the first "l"; kerning (+30) is a small negative gap
    f = run(b"BT /F1 12 Tf 72 720 Td [(He) -2000 (l) 30 (lo)] TJ ET").fragments[0]
    g = f.char_gaps
    assert len(g) == len(f.text) == 5 and g[0] == 0 and g[1] == 0
    assert abs(g[2] - 24) < 1e-6 and abs(g[3] - (-0.36)) < 1e-6 and g[4] == 0
    # Tc and Tw move the glyph after the one they follow
    t = run(b"BT /F1 12 Tf 2 Tc 5 Tw 72 720 Td (a b) Tj ET").fragments[0]
    assert t.char_gaps == (0.0, 2.0, 7.0)
    # the CTM scales the gaps
    d = run(b"2 0 0 2 0 0 cm BT /F1 12 Tf 72 720 Td [(a) -1000 (b)] TJ ET").fragments[0]
    assert abs(d.char_gaps[1] - 24) < 1e-6


def test_tj_edges():
    # a trailing number is not in the width, but the next text starts after it
    pc = run(b"BT /F1 12 Tf 72 720 Td [(ab) -1000] TJ (c) Tj ET")
    a, c = pc.fragments
    assert a.text == "ab" and abs(a.width - (556 + 556) * 12 / 1000) < 1e-6
    assert abs(c.x - (72 + a.width + 12)) < 1e-6
    # a leading number moves the start
    first = run(b"BT /F1 12 Tf 72 720 Td [-1000 (a)] TJ ET").fragments[0]
    assert abs(first.x - 84) < 1e-6 and first.char_offsets[0] == 0


def test_render_mode_invisible():
    assert run(text_ops("A", extra=b"3 Tr")).fragments[0].invisible is True
    assert run(text_ops("A", extra=b"7 Tr")).fragments[0].invisible is True
    assert run(text_ops("A", extra=b"0 Tr")).fragments[0].invisible is False


def test_ocg_off_hidden():
    props = res(Properties={"L1": Ref(10)})
    body = b"/OC /L1 BDC " + text_ops("A") + b" EMC " + text_ops("B", y=600)
    pc = run(body, props, {10: OCG}, ocg_catalog(off=[10]))
    assert [f.hidden for f in pc.fragments] == ["ocg", ""]
    pc = run(body, props, {10: OCG}, ocg_catalog(off=[], on=[10]))
    assert [f.hidden for f in pc.fragments] == ["", ""]
    pc = run(body, props, {10: OCG})  # no OCProperties: everything is on
    assert [f.hidden for f in pc.fragments] == ["", ""]


def test_ocg_base_state_off():
    cat = ocg_catalog(off=[], on=[11])
    cat["OCProperties"]["D"]["BaseState"] = Name("OFF")
    pf, _ = load(b"", extra={10: OCG, 11: OCG}, catalog=cat)
    state = OCState.from_catalog(pf)
    assert state.hidden(PDFRef(10, 0)) is True
    assert state.hidden(PDFRef(11, 0)) is False


def test_ocmd_policies():
    def ocmd(num, **kw):
        return {num: dict({"Type": Name("OCMD"), "OCGs": [Ref(10), Ref(11)]}, **kw)}

    extra = {10: OCG, 11: OCG}
    for num, kw in ((12, {}), (13, {"P": Name("AnyOn")}), (14, {"P": Name("AllOn")}),
                    (15, {"P": Name("AnyOff")}), (16, {"P": Name("AllOff")}),
                    (17, {"VE": [Name("And"), Ref(10)], "P": Name("AllOn")})):
        extra.update(ocmd(num, **kw))
    extra[18] = {"Type": Name("OCMD"), "OCGs": Ref(10), "P": Name("AllOff")}
    pf, _ = load(b"", extra=extra, catalog=ocg_catalog(off=[10], on=[11]))
    state = OCState.from_catalog(pf)
    got = {n: state.hidden(PDFRef(n, 0)) for n in range(10, 19)}
    # one group off (10) and one on (11); the policies of ISO 32000-1 8.11.2.2
    assert got[10] is True and got[11] is False
    assert got[12] is False  # AnyOn is the default
    assert got[13] is False  # AnyOn: visible, one is on
    assert got[14] is True   # AllOn: hidden, one is off
    assert got[15] is False  # AnyOff: visible, one is off
    assert got[16] is True   # AllOff: hidden, one is on
    assert got[17] is False  # /VE counts as visible
    assert got[18] is False  # AllOff over the only group, which is off


def test_ocmd_hides_text():
    extra = {10: OCG, 12: {"Type": Name("OCMD"), "OCGs": [Ref(10)], "P": Name("AllOn")}}
    body = b"/OC /M1 BDC " + text_ops("A") + b" EMC"
    pc = run(body, res(Properties={"M1": Ref(12)}), extra, ocg_catalog(off=[10]))
    assert pc.fragments[0].hidden == "ocg"


def test_any_off():
    pf, resources = load(b"", res(Properties={"L1": Ref(10)}), {10: OCG}, ocg_catalog(off=[10]))
    assert OCState.from_catalog(pf).any_off(resources) is True
    pf, resources = load(b"", res(XObject={"Fm": Ref(20)}),
                         {10: OCG, 20: form_xobject(b"", oc=10)}, ocg_catalog(off=[10]))
    assert OCState.from_catalog(pf).any_off(resources) is True
    pf, resources = load(b"", res(Properties={"L1": Ref(10)}), {10: OCG}, ocg_catalog(off=[]))
    assert OCState.from_catalog(pf).any_off(resources) is False
    assert OCState.from_catalog(pf).any_off(F1) is False


def test_form_xobject():
    form = form_xobject(text_ops("Hi"), matrix=[1, 0, 0, 1, 100, 0])
    pc = run(b"/Fm1 Do", res(XObject={"Fm1": Ref(20)}), {20: form})
    assert [(f.text, f.x, f.y) for f in pc.fragments] == [("Hi", 172, 720)]
    assert pc.objects == 2  # the Do and the text object inside the form


def test_form_oc_off():
    pc = run(b"/Fm1 Do", res(XObject={"Fm1": Ref(20)}),
             {10: OCG, 20: form_xobject(text_ops("Hi"), oc=10)}, ocg_catalog(off=[10]))
    assert pc.fragments[0].hidden == "ocg"


def test_form_bbox_clips():
    form = form_xobject(text_ops("In", x=10, y=10) + b" " + text_ops("Out", x=300, y=10))
    form.dictionary["BBox"] = [0, 0, 100, 100]
    pc = run(b"/Fm1 Do", res(XObject={"Fm1": Ref(20)}), {20: form})
    assert [f.text for f in pc.fragments] == ["In"]


def test_form_self_reference():
    form = form_xobject(b"/Fm1 Do")
    form.dictionary["Resources"] = {"XObject": {"Fm1": Ref(20)}}
    start = time.time()
    pc = run(b"/Fm1 Do", res(XObject={"Fm1": Ref(20)}), {20: form})
    assert pc.cut["page_part"] == 1 and sum(pc.cut.values()) == 1, pc.cut
    assert time.time() - start < 1


def test_form_depth_cap():
    extra = {}
    for i in range(20):
        body = text_ops("x", y=100 + i) + (b" /Fm Do" if i < 19 else b"")
        form = form_xobject(body)
        form.dictionary["Resources"] = dict(F1, XObject={"Fm": Ref(21 + i)})
        extra[20 + i] = form
    pc = run(b"/Fm Do", res(XObject={"Fm": Ref(20)}), extra)
    assert len(pc.fragments) == cm.MAX_FORM_DEPTH == 16
    assert pc.cut["page_part"] == 1, pc.cut


def test_form_operator_cap():
    pc = run(b"/Fm1 Do " + text_ops("after"), res(XObject={"Fm1": Ref(20)}),
             {20: form_xobject(b"q Q " * 32769)})
    assert pc.cut["page_part"] == 1 and [f.text for f in pc.fragments] == ["after"], pc.cut
    # exactly the cap is fine
    pc = run(b"/Fm1 Do", res(XObject={"Fm1": Ref(20)}), {20: form_xobject(b"q Q " * 16384)})
    assert not pc.cut, pc.cut


def test_page_objects_cap():
    body = b" ".join(text_ops("x", y=700 - i) for i in range(100))
    old = pdf_caps.PDF_PAGE_OBJECTS
    pdf_caps.PDF_PAGE_OBJECTS = 50
    try:
        pc = run(body)
    finally:
        pdf_caps.PDF_PAGE_OBJECTS = old
    assert len(pc.fragments) == 50 and pc.objects == 50
    assert pc.cut["page_part"] == 1 and sum(pc.cut.values()) == 1, pc.cut
    assert pdf_caps.PDF_PAGE_OBJECTS == 100_000 and pdf_caps.PDF_PAGE_IMAGES == 64


def test_rule_segments():
    body = (rule_ops(72, 500, 272, 500) + b" 100 400 200 1 re f "
            + rule_ops(0, 0, 100, 100) + b" 100 300 m 150 300.5 200 300.5 250 300 c S")
    pc = run(body)
    assert len(pc.segments) == 4, pc.segments
    assert sorted((s.y0, s.y1) for s in pc.segments) == [
        (300, 300), (400, 400), (401, 401), (500, 500)]
    assert pc.figure[1] == 1  # the diagonal gave no rule segment


def test_segment_cap():
    body = b" ".join(rule_ops(10, 10 + 5 * i, 200, 10 + 5 * i) for i in range(20))
    old = pdf_tables.PDF_SEGMENTS
    pdf_tables.PDF_SEGMENTS = 10
    try:
        pc = run(body)
    finally:
        pdf_tables.PDF_SEGMENTS = old
    assert len(pc.segments) == 10 and not pc.cut, (len(pc.segments), pc.cut)
    assert old == 20_000


def test_short_segments():
    pc = run(b"0.5 w 10 10 m 10.5 10 l S")
    assert len(pc.segments) == 0 and len(pc.short) == 1


def test_figure_paths():
    body = b"".join(circle(50 + 12 * i, 100 + 3 * i, 5) for i in range(40))
    pc = run(body)
    assert pc.figure[1] == 40 and not pc.segments
    assert pc.figure[0] == (45, 95, 50 + 12 * 39 + 5, 100 + 3 * 39 + 5)
    assert run(text_ops("A")).figure == (None, 0)


def test_off_layer_paths_dropped():
    body = b"/OC /L1 BDC " + rule_ops(0, 0, 100, 0) + b" EMC " + rule_ops(0, 5, 100, 5)
    pc = run(body, res(Properties={"L1": Ref(10)}), {10: OCG}, ocg_catalog(off=[10]))
    assert len(pc.segments) == 1


IMAGE = Stream({"Type": Name("XObject"), "Subtype": Name("Image"), "Width": 1, "Height": 1,
                "ColorSpace": Name("DeviceGray"), "BitsPerComponent": 8}, b"\x00")


def test_image_draws():
    pf, resources = load(b"", res(XObject={"Im1": Ref(30)}), {30: IMAGE})
    body = b"q 100 0 0 50 72 600 cm /Im1 Do Q /Im2 Do"
    pc = cm.interpret(pf, body, resources, {}, OCState.from_catalog(pf), box=BOX)
    assert [(n, b) for n, b, _s in pc.image_draws] == [("Im1", (72, 600, 172, 650))]
    assert pc.image_draws[0][2] is pf.resolve(resources["XObject"]["Im1"])
    off = run(b"/OC /L1 BDC /Im1 Do EMC",
              res(XObject={"Im1": Ref(30)}, Properties={"L1": Ref(10)}),
              {30: IMAGE, 10: OCG}, ocg_catalog(off=[10]))
    assert off.image_draws == []


def test_image_in_form_carries_its_own_stream():
    other = Stream(dict(IMAGE.dictionary), b"")
    form = form_xobject(b"q 10 0 0 10 20 20 cm /Im0 Do Q")
    form.dictionary["Resources"] = {"XObject": {"Im0": Ref(31)}}
    pf, resources = load(b"", res(XObject={"Im0": Ref(30), "Fm": Ref(20)}),
                         {30: IMAGE, 31: other, 20: form})
    body = b"q 50 0 0 50 0 0 cm /Im0 Do Q /Fm Do"
    pc = cm.interpret(pf, body, resources, {}, OCState.from_catalog(pf), box=BOX)
    assert [n for n, _b, _s in pc.image_draws] == ["Im0", "Im0"]
    assert pc.image_draws[0][2] is pf.resolve(PDFRef(30, 0)) and pc.image_draws[1][2] is pf.resolve(PDFRef(31, 0))
    assert pc.image_draws[0][2] is not pc.image_draws[1][2]


def test_image_in_form_clipped_to_bbox():
    form = form_xobject(b"q 10 0 0 10 20 20 cm /Im1 Do Q q 10 0 0 10 300 300 cm /Im1 Do Q")
    form.dictionary["BBox"] = [0, 0, 100, 100]
    pc = run(b"/Fm Do", res(XObject={"Fm": Ref(20), "Im1": Ref(30)}), {20: form, 30: IMAGE})
    assert [b for _n, b, _s in pc.image_draws] == [(20, 20, 30, 30)]


def test_form_operator_cap_counted_once():
    inner = form_xobject(b"q Q " * 20000)
    outer = form_xobject(b"/In Do /In Do /In Do")
    outer.dictionary["Resources"] = {"XObject": {"In": Ref(21)}}
    pc = run(b"/Out Do /Out Do /Out Do", res(XObject={"Out": Ref(20)}), {20: outer, 21: inner})
    assert pc.cut["page_part"] == 1 and sum(pc.cut.values()) == 1, pc.cut


def test_base_state_off_ignores_property_lists():
    cat = ocg_catalog(off=[], on=[])
    cat["OCProperties"]["D"]["BaseState"] = Name("OFF")
    body = (b"/OC /P1 BDC " + text_ops("A") + b" EMC /OC /G1 BDC " + text_ops("B", y=600) + b" EMC")
    props = res(Properties={"P1": Ref(40), "G1": Ref(10)})
    pc = run(body, props, {40: {"MCID": 0}, 10: OCG}, cat)
    assert [f.hidden for f in pc.fragments] == ["", "ocg"]
    pf, resources = load(b"", res(Properties={"P1": Ref(40)}), {40: {"MCID": 0}}, cat)
    assert OCState.from_catalog(pf).any_off(resources) is False


def test_any_off_walks_nested_forms():
    inner = form_xobject(b"", oc=10)
    mid = form_xobject(b"")
    mid.dictionary["Resources"] = {"XObject": {"In": Ref(21)}}
    pf, resources = load(b"", res(XObject={"Mid": Ref(20)}), {10: OCG, 20: mid, 21: inner},
                         ocg_catalog(off=[10]))
    assert OCState.from_catalog(pf).any_off(resources) is True
    prop = form_xobject(b"")
    prop.dictionary["Resources"] = {"Properties": {"L": Ref(10)}}
    pf, resources = load(b"", res(XObject={"Mid": Ref(20)}), {10: OCG, 20: prop},
                         ocg_catalog(off=[10]))
    assert OCState.from_catalog(pf).any_off(resources) is True
    loop = form_xobject(b"")
    loop.dictionary["Resources"] = {"XObject": {"Me": Ref(20)}}
    pf, resources = load(b"", res(XObject={"Me": Ref(20)}), {20: loop}, ocg_catalog(off=[]))
    assert OCState.from_catalog(pf).any_off(resources) is False


def test_inline_image_keeps_following_text():
    data = b"xEI \x00EIx\xffEI"
    body = b"q 2 0 0 2 0 0 cm BI /W 2 /H 2 /BPC 8 /CS /G ID " + data + b"\nEI Q " + text_ops("after")
    pc = run(body)
    assert [f.text for f in pc.fragments] == ["after"] and pc.objects == 2


def test_deep_q_nesting():
    body = b"2 0 0 2 0 0 cm " + b"q " * 200000 + b"Q " * 300000 + text_ops("A")
    start = time.time()
    f = run(body).fragments[0]
    assert f.size == 24 and f.x == 144
    assert time.time() - start < 10


def test_vertical_text():
    pc = run(b"BT /F2 12 Tf 72 720 Td <0001> Tj ET", {"Font": {"F2": Ref(100)}},
             font_type0(tounicode={1: "A"}, vertical=True))
    assert len(pc.fragments) == 1
    assert writing_direction(pc.fragments[0]) == "down" and pc.fragments[0].text == "A"


def test_unmapped_font_counted():
    objects = font_type0(tounicode=None, ordering="Identity")
    pc = run(b"BT /F2 12 Tf 72 720 Td <0001> Tj ET", {"Font": {"F2": Ref(100)}}, objects)
    assert pc.fragments == [] and pc.unmapped == 1


def test_bad_coordinates_dropped():
    pc = run(text_ops("A", x=1e12) + b" " + text_ops("B"))
    assert [f.text for f in pc.fragments] == ["B"] and pc.cut["bad_coord"] == 1, pc.cut
    # an integer operand too big for a float must not raise
    run(b"1" + b"0" * 400 + b" 0 0 1 0 0 cm " + text_ops("A"))


def test_q_restores_state():
    body = b"q 2 0 0 2 0 0 cm " + text_ops("A") + b" Q " + text_ops("B")
    a, b = run(body).fragments
    assert (a.size, b.size, b.x) == (24, 12, 72)


def test_text_state_operators():
    body = b"BT /F1 10 Tf 14 TL 50 700 Td (a) Tj T* (b) Tj 0 -14 TD (c) Tj ET"
    a, b, c = run(body).fragments
    assert (a.y, b.y, c.y) == (700, 686, 672) and a.obj == b.obj == c.obj == 0


def test_text_object_index():
    pc = run(text_ops("a") + b" " + text_ops("b", y=600))
    assert [f.obj for f in pc.fragments] == [0, 1]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
