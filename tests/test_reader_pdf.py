"""pdf format module: each page a "## Page N" section with its lines, headings, paragraphs,
tables, images, fields and comments; hidden text, scanned pages, vector figures and the caps.

Run: python -I -X utf8 tests/test_reader_pdf.py
"""
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (JPEG, EncryptSpec, Name, Ref, acroform, font_type0, image_xobject,  # noqa: E402
                     link_annot, ocg_catalog, outline_items, rule_ops, simple_pdf, text_annot,
                     text_ops, widget)

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.formats import pdf  # noqa: E402
from reader.formats import pdf_layout, pdf_tables  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.pdfnative import crypto  # noqa: E402
from reader.pdfnative import document as dm  # noqa: E402

F1 = {"Font": {"F1": Ref(3)}}
OCG = {"Type": Name("OCG"), "Name": "Layer"}
FULL = b"q 612 0 0 792 0 0 cm /Im1 Do Q "


def md(data, hidden=False, **kw):
    ctx = Context()
    doc = pdf.parse_bytes(data, ctx, **kw)
    with tempfile.TemporaryDirectory() as tmp:
        text = markdown.render(doc, Path(tmp) / "assets", hidden, ctx.skipped)
    return text, ctx, doc


def with_image(content, image, **kw):
    res = dict(F1, XObject={"Im1": Ref(30)})
    return simple_pdf([content], resources=res, extra_objects={30: image}, **kw)


def patched(obj, name, value):
    """A context manager setting obj.name to value for its body."""
    class _Patch:
        def __enter__(self):
            self.saved = getattr(obj, name)
            setattr(obj, name, value)

        def __exit__(self, *exc):
            setattr(obj, name, self.saved)
    return _Patch()


def heads(text):
    return re.findall(r"^## Page \d+$", text, re.M)


def test_pages_numbered():
    text, _, doc = md(simple_pdf([text_ops("One"), b"", text_ops("Three")]))
    assert heads(text) == ["## Page 1", "## Page 2", "## Page 3"], text
    assert text.index("One") < text.index("## Page 2") < text.index("Three")
    assert markdown.stats(text)["pages"] == [[1, 1], [2, 5], [3, 7]], markdown.stats(text)
    assert doc.meta["engine"] == "native" and doc.meta["pages_total"] == 3


def _hidden_pdf():
    body = b" ".join([
        text_ops("Visible", y=700),
        text_ops("Offcrop", x=700, y=900),
        text_ops("Tiny", y=600, size=0.5),
        text_ops("Ghost", y=500, extra=b"3 Tr"),
        b"/OC /L1 BDC " + text_ops("Secret", y=400) + b" EMC",
    ])
    return simple_pdf([body], resources=dict(F1, Properties={"L1": Ref(50)}),
                      extra_objects={50: OCG}, catalog=ocg_catalog(off=[50]))


def test_hidden_rules():
    text, _, _ = md(_hidden_pdf())
    assert text.splitlines()[0] == "(4 hidden places left out)", text
    assert "Visible" in text
    for word in ("Offcrop", "Tiny", "Ghost", "Secret"):
        assert word not in text, (word, text)
    text, _, _ = md(_hidden_pdf(), hidden=True)
    for word in ("Offcrop", "Tiny", "Ghost", "Secret"):
        assert f"(hidden) {word}" in text, (word, text)
    assert "hidden places" not in text


def test_hidden_per_text_object():
    body = b"BT /F1 12 Tf 3 Tr 72 700 Td (one) Tj 0 -14 Td (two) Tj 0 -14 Td (three) Tj ET"
    text, _, _ = md(simple_pdf([text_ops("Seen", y=720) + b" " + body]))
    assert text.splitlines()[0] == "(1 hidden places left out)", text
    assert "two" not in text


def test_ocr_layer():
    content = FULL + text_ops("Scanned words", y=400, extra=b"3 Tr")
    text, ctx, doc = md(with_image(content, image_xobject(64, 64)))
    assert "Scanned words" in text and "![" not in text, text
    assert "hidden" not in text and not ctx.skipped, (text, ctx.skipped)
    assert doc.meta["unrendered"] == []


def test_scan_native_jpeg():
    text, ctx, doc = md(with_image(FULL, image_xobject(8, 8, data=JPEG, filter="DCTDecode")))
    assert "![Scanned page 1](assets/img-001.jpg)" in text, text
    assert doc.meta["unrendered"] == [] and not ctx.skipped


def test_scan_native_undecodable():
    image = image_xobject(100, 100, data=bytes(64), filter="CCITTFaxDecode")
    text, ctx, doc = md(with_image(FULL, image))
    assert doc.meta["unrendered"] == [1] and ctx.skipped["scan"] == 1, (doc.meta, ctx.skipped)
    assert ctx.skipped["image"] == 0 and "scanned page not shown" in text


def test_figure_native_unrendered():
    # 40 filled 5 x 5 squares over a 230 x 275 box: about 13% of the page
    body = b"".join(b"%d %d 5 5 re f " % (100 + 45 * (i % 6), 100 + 45 * (i // 6)) for i in range(40))
    text, ctx, doc = md(simple_pdf([text_ops("Caption", y=720) + b" " + body]))
    assert doc.meta["unrendered"] == [1] and doc.meta["figures"] == [], doc.meta
    assert ctx.skipped["figure"] == 1 and "Caption" in text, ctx.skipped


def test_renders_cap_unrendered():
    image = image_xobject(8, 8, data=JPEG, filter="DCTDecode")
    data = simple_pdf([FULL] * 5, resources=dict(F1, XObject={"Im1": Ref(30)}),
                      extra_objects={30: image})
    with patched(pdf, "PDF_RENDERS", 2):
        text, ctx, doc = md(data)
    assert text.count("![Scanned page") == 2, text
    assert doc.meta["unrendered"] == [3, 4, 5] and ctx.skipped["scan"] == 3, (doc.meta, ctx.skipped)


def test_image_floor_dedup():
    res = dict(F1, XObject={"Small": Ref(30), "Big": Ref(31)})
    body = (text_ops("Words", y=750) + b" q 100 0 0 100 72 500 cm /Small Do Q"
            b" q 10 0 0 100 200 500 cm /Big Do Q"
            b" q 100 0 0 100 72 300 cm /Big Do Q q 100 0 0 100 300 300 cm /Big Do Q")
    big = image_xobject(64, 64, data=bytes(range(256)) * 16)
    data = simple_pdf([body], resources=res,
                      extra_objects={30: image_xobject(20, 20), 31: big})
    ctx = Context()
    doc = pdf.parse_bytes(data, ctx)
    with tempfile.TemporaryDirectory() as tmp:
        text = markdown.render(doc, Path(tmp) / "assets", False, ctx.skipped)
        files = sorted(p.name for p in (Path(tmp) / "assets").iterdir())
    assert files == ["img-001.png"], files
    assert text.count("](assets/img-001.png)") == 2 and not ctx.skipped, (text, ctx.skipped)


def _form_pdf(flags=0):
    fields = acroform([widget((72, 600, 200, 620), Ref(4), T="Name", FT=Name("Tx"), V="Kim")])
    objects = dict(fields)
    objects[100] = text_annot((10, 10, 20, 20), "note", author="Ann", flags=flags)
    return simple_pdf([text_ops("Body text")], catalog={"AcroForm": Ref(200)},
                      extra_objects=objects, page_extra={"Annots": [Ref(100)]})


def test_fields_comments_end():
    text, _, _ = md(_form_pdf())
    assert "> Field Name: Kim\n> Comment by Ann: note" in text, text
    assert text.index("Body text") < text.index("> Field Name")


def test_hidden_comment():
    text, _, _ = md(_form_pdf(flags=2))
    assert "note" not in text and text.splitlines()[0] == "(1 hidden places left out)", text
    assert "> Field Name: Kim" in text


def _hidden_form_pdf():
    """Fields whose one widget has /F 2 (Hidden) or 32 (NoView), a field with a hidden and a shown
    widget, a shown field, and a comment with /F 32."""
    fields = acroform([
        widget((72, 600, 200, 620), Ref(4), T="Hid", FT=Name("Tx"), V="hidden value", F=2),
        widget((72, 570, 200, 590), Ref(4), T="NoView", FT=Name("Tx"), V="noview value", F=32),
        {"T": "Two", "FT": Name("Tx"), "V": "two value",
         "Kids": [widget((72, 540, 200, 560), Ref(4), F=2), widget((72, 510, 200, 530), Ref(4))]},
        widget((72, 480, 200, 500), Ref(4), T="Seen", FT=Name("Tx"), V="seen value"),
    ])
    objects = dict(fields)
    objects[100] = text_annot((10, 10, 20, 20), "noview note", author="Ann", flags=32)
    annots = [Ref(100)] + [Ref(n) for n, obj in sorted(fields.items()) if "Rect" in obj]
    return simple_pdf([text_ops("Body text")], catalog={"AcroForm": Ref(200)}, extra_objects=objects,
                      page_extra={"Annots": annots})


HIDDEN_FORM_LINES = ("> (hidden) Field Hid: hidden value", "> (hidden) Field NoView: noview value",
                     "> (hidden) Comment by Ann: noview note")


def test_hidden_fields():
    # a field all of whose widgets are Hidden or NoView, and a NoView comment, are hidden places
    data = _hidden_form_pdf()
    text, _, _ = md(data)
    assert text.splitlines()[0] == "(3 hidden places left out)", text
    assert "> Field Two: two value" in text and "> Field Seen: seen value" in text, text
    assert not any(w in text for w in ("hidden value", "noview value", "noview note")), text
    text, _, _ = md(data, hidden=True)
    for line in HIDDEN_FORM_LINES:
        assert line in text.splitlines(), (line, text)


def test_links_inline():
    data = simple_pdf([text_ops("here")], extra_objects={100: link_annot((70, 715, 100, 735), "https://ex.org")},
                      page_extra={"Annots": [Ref(100)]})
    text, _, _ = md(data)
    assert "[here](https://ex.org)" in text, text


def _grid_ops():
    ops = [rule_ops(100, y, 300, y) for y in (500, 530, 560)]
    ops += [rule_ops(x, 500, x, 560) for x in (100, 200, 300)]
    return b" ".join(ops)


def _table_page():
    cells = [("Alpha", 110, 540), ("Beta", 210, 540), ("Gamma", 110, 510), ("Delta", 210, 510)]
    words = b" ".join(text_ops(t, x=x, y=y, size=10) for t, x, y in cells)
    return text_ops("Before", y=700) + b" " + _grid_ops() + b" " + words + b" " + text_ops("After", y=400)


def test_table_on_page():
    text, ctx, _ = md(simple_pdf([_table_page()]))
    assert "|Alpha|Beta|" in text and "|Gamma|Delta|" in text, text
    assert text.count("Alpha") == 1 and text.count("Delta") == 1, text
    assert text.index("Before") < text.index("|Alpha") < text.index("After"), text
    assert not ctx.skipped


def test_outline_headings():
    data = simple_pdf([text_ops("Intro", y=720) + b" " + text_ops("Body line one", y=700)
                       + b" " + text_ops("Body line two", y=686)],
                      catalog={"Outlines": Ref(300)}, extra_objects=outline_items([(1, "Intro", 0)]))
    text, _, _ = md(data)
    assert "### Intro" in text and text.count("Intro") == 1, text


def test_size_heading():
    body = b" ".join([text_ops("Big Title", y=740, size=16)]
                     + [text_ops(f"Body line {i}", y=700 - 12 * i, size=10) for i in range(4)])
    text, _, _ = md(simple_pdf([body]))
    assert "### Big Title" in text, text
    assert "### Body" not in text


def test_running_headers():
    pages = [text_ops("Annual Report", y=770, size=10) + b" " + text_ops(f"Body of page {n}", y=600)
             for n in (1, 2, 3)]
    text, _, doc = md(simple_pdf(pages))
    assert doc.headers == ["Annual Report"], doc.headers
    assert text.count("Annual Report") == 1 and text.splitlines()[0] == "Header: Annual Report", text
    assert "Body of page 3" in text


def test_page_cap():
    with patched(pdf, "PDF_PAGES", 3):
        text, ctx, _ = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 6)]))
    assert heads(text) == ["## Page 1", "## Page 2", "## Page 3"], text
    assert ctx.skipped["page_cut"] == 2, ctx.skipped


def test_deadline_page_cut():
    now = [0.0]

    def clock():
        now[0] += 10.0
        return now[0]

    text, ctx, _ = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 6)]), clock=clock, started=0.0)
    # clock calls: 10 at the loop check of page 1 (under 36), 20 at its start, 30 before its
    # tables (10 s on the page: read in part); 40 at the loop check of page 2 (past 36): cut
    assert heads(text) == ["## Page 1"] and "P1" in text, text
    assert ctx.skipped == {"page_cut": 4, "page_part": 1}, ctx.skipped


def test_image_budget_counted_once():
    import random
    noise = random.Random(7).randbytes(64 * 64)  # incompressible: its stream stays over 1000 bytes
    content = text_ops("Words", y=750) + b" q 100 0 0 100 72 500 cm /Im1 Do Q"
    data = with_image(content, image_xobject(64, 64, data=noise), encrypt=EncryptSpec(4))
    with patched(crypto, "DECRYPT_TOTAL", 1000):
        text, ctx, _ = md(data)
    assert "Words" in text and ctx.skipped == {"image": 1}, ctx.skipped


def test_bad_coord_is_text():
    far = b"BT /F1 12 Tf 1 0 0 1 2000000000 100 Tm (Far) Tj ET"
    text, ctx, _ = md(simple_pdf([text_ops("Kept") + b" " + far + b" " + far]))
    assert "Kept" in text and "Far" not in text, text
    assert ctx.skipped == {"text": 1}, ctx.skipped
    assert "char_positions" in markdown.INTERNAL


def test_after_loop_page_isolated():
    real = pdf_layout.paragraphs

    def paragraphs(lns, model):
        if any(ln.text == "P2" for ln in lns):
            raise ValueError("broken paragraphs")
        return real(lns, model)

    with patched(pdf_layout, "paragraphs", paragraphs):
        text, ctx, _ = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 4)]))
    assert "P1" in text and "P3" in text and "P2" not in text, text
    assert ctx.skipped == {"page": 1}, ctx.skipped

    def timeout(lns, model):
        raise ReadError("timeout", "paragraphs")

    with patched(pdf_layout, "paragraphs", timeout):
        try:
            md(simple_pdf([text_ops("A")]))
        except ReadError as e:
            assert e.kind == "timeout", e
        else:
            raise AssertionError("expected the timeout to propagate")


def _render_raises(data):
    real = dm.NativeDoc.page

    def render(box, dpi):
        raise ValueError("render failed")

    def page(self, index):
        got = real(self, index)
        got.render = render
        return got

    with patched(dm.NativeDoc, "page", page):
        return md(data)


def test_render_raises_unrendered():
    _, ctx, doc = _render_raises(with_image(FULL, image_xobject(8, 8, data=JPEG, filter="DCTDecode")))
    assert doc.meta["unrendered"] == [1] and ctx.skipped == {"scan": 1}, (doc.meta, ctx.skipped)
    body = b"".join(b"%d %d 5 5 re f " % (100 + 45 * (i % 6), 100 + 45 * (i // 6)) for i in range(40))
    _, ctx, doc = _render_raises(simple_pdf([text_ops("Caption", y=720) + b" " + body]))
    assert doc.meta["unrendered"] == [1] and ctx.skipped == {"figure": 1}, (doc.meta, ctx.skipped)


def _turned(text, vx, vy, size=10):
    """Text that reads left to right once a /Rotate 90 page (612 wide) is turned: the viewer's
    (vx, vy) is the page's (612 - vy, vx), and the text runs up the page."""
    return b"BT /F1 %d Tf 0 1 -1 0 %d %d Tm (%s) Tj ET" % (size, 612 - vy, vx, text.encode())


def _turned_rule(vx0, vy0, vx1, vy1):
    return rule_ops(612 - vy0, vx0, 612 - vy1, vx1)


def test_rotated_page():
    ops = [_turned("Report Title", 72, 560, size=18),
           _turned("First body line", 72, 520), _turned("second body line", 72, 507),
           _turned("third body line", 72, 494)]
    ops += [_turned_rule(100, y, 300, y) for y in (300, 330, 360)]
    ops += [_turned_rule(x, 300, x, 360) for x in (100, 200, 300)]
    ops += [_turned(t, x, y) for t, x, y in
            (("Alpha", 110, 340), ("Beta", 210, 340), ("Gamma", 110, 310), ("Delta", 210, 310))]
    ops.append(_turned("Closing words", 72, 200))
    text, ctx, _ = md(simple_pdf([b" ".join(ops)], page_extra={"Rotate": 90}))
    assert "### Report Title" in text, text
    assert "First body line second body line third body line" in text, text
    assert "|Alpha|Beta|" in text and "|Gamma|Delta|" in text, text
    order = [text.index(s) for s in ("### Report Title", "First body", "|Alpha|Beta|",
                                     "|Gamma|Delta|", "Closing words")]
    assert order == sorted(order) and text.count("Alpha") == 1, text
    assert not ctx.skipped, ctx.skipped


def test_sideways_page_read_upright():
    # a /Rotate 90 page whose text the content draws left to right: the viewer shows it running
    # down the page, Latin turned with the page, not vertical writing; it reads as lines with
    # the glyph-gap spaces ("Apache" is 40.692 wide at 12 pt Helvetica)
    ops = [text_ops("Apache", 72, 700), text_ops("Tika", 116, 700), text_ops("toolkit", 72, 686)]
    text, _, _ = md(simple_pdf([b" ".join(ops)], page_extra={"Rotate": 90}))
    assert "Apache Tika toolkit" in text, text
    # vertical writing (an Identity-V font) on an upright page stays columns, right one first
    objs = font_type0(tounicode={1: "縦", 2: "書", 3: "日", 4: "本"}, vertical=True)
    cols = (b"BT /F2 12 Tf 500 700 Td <00030004> Tj ET BT /F2 12 Tf 480 700 Td <00010002> Tj ET")
    text, _, _ = md(simple_pdf([cols], resources={"Font": {"F1": Ref(3), "F2": Ref(100)}},
                               extra_objects=objs))
    assert "日本" in text and "縦書" in text, text
    assert text.index("日本") < text.index("縦書"), text


def _apache(rotate):
    ops = [text_ops("Apache", 72, 700), text_ops("Tika", 116, 700), text_ops("toolkit", 72, 686)]
    return simple_pdf([b" ".join(ops)], page_extra={"Rotate": rotate})


def test_turned_pages_read_upright():
    # /Rotate 270 leaves the text running up the viewer, /Rotate 180 upside down: both read upright
    for rotate in (270, 180):
        text, _, _ = md(_apache(rotate))
        assert "Apache Tika toolkit" in text, (rotate, text)


_CJK = "縦書日本語文章"
_CJK_CODES = {c: 0x100 + i for i, c in enumerate(_CJK)}


def _column(text, x, y=700):
    return b"BT /F2 12 Tf %d %d Td <%s> Tj ET" % (
        x, y, "".join("%04X" % _CJK_CODES[c] for c in text).encode())


def test_vertical_writing_outvotes_sideways_latin():
    # Identity-V columns with Latin set clockwise inside them: the page keeps its turn, the
    # columns read whole and right first, the Latin keeps its spaces
    objs = font_type0(tounicode={code: c for c, code in _CJK_CODES.items()}, vertical=True)
    latin = b"BT /F1 10 Tf 0 -1 1 0 %d %d Tm (%s) Tj ET"
    body = [_column("縦書日本", 500), latin % (496, 640, b"Unicode"),
            _column("語文章", 480), latin % (476, 650, b"OpenType")]
    text, _, _ = md(simple_pdf([b" ".join(body)], resources={"Font": {"F1": Ref(3), "F2": Ref(100)}},
                               extra_objects=objs))
    assert "縦書日本" in text and "語文章" in text, text
    assert text.index("縦書日本") < text.index("語文章"), text
    assert pdf_layout.upright_turn(dm.NativeDoc(simple_pdf(
        [b" ".join(body)], resources={"Font": {"F1": Ref(3), "F2": Ref(100)}},
        extra_objects=objs)).page(0).fragments, 0) == 0


def test_sideways_latin_is_not_vertical_writing():
    # an unrotated page: a table set sideways (running up) turns the page; the upright footer,
    # now running down the viewer, is Latin, not vertical writing, and keeps its spaces
    up = b"BT /F1 10 Tf 0 1 -1 0 %d 100 Tm (Row %d of the sideways landscape table body) Tj ET"
    rows = [up % (100 + 14 * k, k) for k in range(8)]
    footer = [text_ops("Quarterly", 200, 40, size=10), text_ops("results", 248, 40, size=10)]
    text, _, _ = md(simple_pdf([b" ".join(rows + footer)]))
    assert "Row 3 of the sideways" in text and "Quarterly results" in text, text


def test_line_drawn_twice_reads_once():
    # a line of two fragments drawn twice, 0.4 pt apart (a fake bold or a shadow): read once
    ops = [text_ops("Annual", 72, 700), text_ops("Report", 120, 700),
           text_ops("Annual", 72.4, 700), text_ops("Report", 120.4, 700)]
    text, _, _ = md(simple_pdf([b" ".join(ops)]))
    assert "AAnn" not in text and text.count("Annual") == 1 and "Annual Report" in text, text
    # a stamp drawn five times over itself
    stamp = [text_ops("Processed with", 72 + 0.2 * k, 600) + b" "
             + text_ops("evaluation edition", 160 + 0.2 * k, 600) for k in range(5)]
    text, _, _ = md(simple_pdf([b" ".join(stamp)]))
    assert "PPPP" not in text and text.count("Processed") == 1, text
    assert "Processed with evaluation edition" in text, text


def test_overlapping_fragments_stay_words():
    # a name drawn after a date, over its last glyph by under half a glyph (K over 5 by 2.7 pt),
    # within the line's baseline tolerance: no free slot, so the name starts a line of its own
    ops = [text_ops("Minister", 150, 700), text_ops("2025", 249, 703), text_ops("20.", 300, 703),
           text_ops("Kim", 273, 698)]
    text, _, _ = md(simple_pdf([b" ".join(ops)]))
    assert "2025" in text and re.search(r"\bKim\b", text), text


def test_boxes_side_by_side_stay_apart():
    # two boxes on one baseline, the right one drawn first: no join across the gutter, and a
    # larger heading to the left of a sentence stays out of it
    ops = [text_ops("Send your order to:", 320, 700), text_ops("You can visit the site", 72, 700)]
    text, _, _ = md(simple_pdf([b" ".join(ops)]))
    assert "You can visit the site Send" not in text and "to: You" not in text, text
    assert "Send your order to:" in text and "You can visit the site" in text, text
    ops = [text_ops("See the notes in the introduction", 132, 600, size=10),
           text_ops("Index", 42, 600, size=12)]
    text, _, _ = md(simple_pdf([b" ".join(ops)]))
    assert "Index See" not in text and "introduction Index" not in text, text


def test_baseline_drawn_out_of_order():
    # a table row whose cells the content stream draws right cell first: one line, left to right
    row = [text_ops("9.50", 400, 700), text_ops("0001", 72, 700), text_ops("PLAN", 120, 700),
           text_ops("19.00", 480, 700), text_ops("LUMP", 300, 700)]
    text, _, _ = md(simple_pdf([b" ".join(row)]))
    assert "0001 PLAN LUMP 9.50 19.00" in text, text
    # words drawn first into the gaps a later TJ string leaves: the glyphs interleave by position
    # (IMPORT is 46.668 wide at 12 pt Helvetica; -2250 moves TABLE 27 pt past its end)
    gaps = [text_ops("and", 121.668, 650),
            b"BT /F1 12 Tf 72 650 Td [(IMPORT) -2250 (TABLE)] TJ ET"]
    text, _, _ = md(simple_pdf([b" ".join(gaps)]))
    assert "IMPORT and TABLE" in text, text
    # the same text drawn twice over itself (fake bold) is not interleaved glyph by glyph
    text, _, _ = md(simple_pdf([text_ops("Bold", 72, 600) + b" " + text_ops("Bold", 72.3, 600)]))
    assert "BBoolldd" not in text and text.count("Bold") == 1, text


def test_page_seconds_part():
    now = [0.0]
    real = pdf_layout.lines

    def slow_lines(*a, **kw):
        now[0] += 5.0
        return real(*a, **kw)

    content = _table_page() + b" q 100 0 0 100 350 600 cm /Im1 Do Q"
    data = with_image(content, image_xobject(64, 64, data=bytes(range(256)) * 16))
    with patched(pdf_layout, "lines", slow_lines):
        text, ctx, _ = md(data, clock=lambda: now[0], started=0.0)
    assert "Before" in text and "After" in text, text
    assert "|" not in text and "![" not in text, text
    assert ctx.skipped["page_part"] == 1, ctx.skipped


def test_page_chars_cap():
    body = b" ".join(text_ops(c * 10, y=700 - 14 * i) for i, c in enumerate("abcde"))
    with patched(pdf, "PDF_PAGE_CHARS", 10):
        text, ctx, _ = md(simple_pdf([body]))
    assert ctx.skipped["text"] == 1, ctx.skipped
    assert "aaaaaaaaaa" in text and "eeeeeeeeee" not in text, text


def test_page_chars_stop_the_interpreter():
    # past 2 x PDF_PAGE_CHARS shown chars the native interpreter makes no more fragments and counts
    # `text` once for the page; the show operators after that cost little (loose bounds: 300,000 Tj
    # made 300,000 fragments, about 240 MB)
    import time
    import tracemalloc
    # TJ arrays, Td and a nested string among the shows passed over; the rule after them is still read
    shows = b"(A) Tj " * 300_000 + b"[(B) -20 <43>] TJ 0 -1 Td (x(y)z) Tj (C) ' 1 2 (D) \" "
    data = simple_pdf([b"BT /F1 12 Tf 72 700 Td " + shows + b"ET " + rule_ops(72, 100, 540, 100)])
    with patched(pdf, "PDF_PAGE_CHARS", 1_000):
        doc = dm.NativeDoc(data)
        tracemalloc.start()
        t0 = time.perf_counter()
        try:
            pd = doc.page(0)
            took, peak = time.perf_counter() - t0, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert len(pd.fragments) <= 2_001 and pd.cut["text"] == 1, (len(pd.fragments), pd.cut)
        assert len(pd.segments) == 1, pd.segments
        assert peak < 64 * 2 ** 20 and took < 60, (peak, took)
        text, ctx, _ = md(data)
    assert ctx.skipped == {"text": 1}, ctx.skipped
    assert "AAAA" in text, text[:200]


def test_page_raises_skipped():
    real = dm.NativeDoc.page

    def page(self, index):
        if index == 1:
            raise ValueError("broken page")
        return real(self, index)

    with patched(dm.NativeDoc, "page", page):
        text, ctx, _ = md(simple_pdf([text_ops("P1"), text_ops("P2"), text_ops("P3")]))
    assert heads(text) == ["## Page 1", "## Page 3"] and "P3" in text, text
    assert ctx.skipped["page"] == 1, ctx.skipped


def test_member_readerror():
    pages = [text_ops("First"), text_ops("Second " * 150), text_ops("Third")]
    data = simple_pdf(pages, encrypt=EncryptSpec(4))
    with patched(crypto, "DECRYPT_TOTAL", 400):
        text, ctx, _ = md(data)
    assert "First" in text and "Third" in text and "Second" not in text, text
    assert ctx.skipped["page_part"] == 1, ctx.skipped

    def timeout(*a, **kw):
        raise ReadError("timeout", "page step")

    with patched(pdf_tables, "tables", timeout):
        try:
            md(simple_pdf([text_ops("A")]))
        except ReadError as e:
            assert e.kind == "timeout", e
        else:
            raise AssertionError("expected the timeout to propagate")


def test_started_default():
    text, ctx, _ = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 4)]),
                      clock=lambda: reader.STARTED + 100)
    assert len(heads(text)) == 3 and not ctx.skipped["page_cut"], (text, ctx.skipped)


def test_no_readable_page():
    def page(self, index):
        raise ValueError("broken page")

    with patched(dm.NativeDoc, "page", page):
        try:
            md(simple_pdf([text_ops("A"), text_ops("B")]))
        except ReadError as e:
            assert (e.kind, e.detail) == ("damaged", "no readable pages"), e
        else:
            raise AssertionError("expected ReadError")


def test_pages_range():
    text, _, doc = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 6)]), pages=(2, 3))
    assert heads(text) == ["## Page 2", "## Page 3"], text
    assert "P1" not in text and "P4" not in text and doc.meta["pages_total"] == 5


def test_pages_past_end():
    data = simple_pdf([text_ops(f"P{n}") for n in range(1, 6)])
    text, _, doc = md(data, pages=(4, 9))
    assert heads(text) == ["## Page 4", "## Page 5"], text
    try:
        md(data, pages=(7, 9))
    except ReadError as e:
        assert e.kind == "damaged" and e.detail == "pages 7-9 past the end (5 pages)", (e.kind, e.detail)
    else:
        raise AssertionError("expected ReadError")


def test_unmapped_font_garble_warning():
    import json
    res = {"Font": {"F1": Ref(3), "F2": Ref(100)}}
    body = text_ops("Plain") + b" BT /F2 12 Tf 72 650 Td <0001> Tj ET"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.pdf"
        src.write_bytes(simple_pdf([body], resources=res,
                                   extra_objects=font_type0(tounicode=None, ordering="Identity")))
        meta = reader.convert(src, Path(tmp) / "o1")
        assert "1 text run without Unicode" in meta["unread"], meta
        assert "garbled" not in meta["warnings"], meta
        src2 = Path(tmp) / "b.pdf"
        src2.write_bytes(simple_pdf([b"BT /F2 12 Tf 72 700 Td <0001> Tj ET"], resources=res,
                                    extra_objects=font_type0(tounicode={1: "�" * 12})))
        meta = reader.convert(src2, Path(tmp) / "o2")
        assert "garbled" in meta["warnings"], meta
        assert json.loads((Path(tmp) / "o2" / "meta.json").read_text(encoding="utf-8"))["warnings"] == meta["warnings"]


def _unmapped_page(lines, plain):
    """`lines` lines of four codes in an Identity font with no ToUnicode and no font file, then `plain`."""
    body = b" ".join(b"BT /F2 12 Tf 72 %d Td <0001000200030004> Tj ET" % (750 - 20 * i) for i in range(lines))
    return simple_pdf([body + b" " + text_ops(plain, y=100)], resources={"Font": {"F1": Ref(3), "F2": Ref(100)}},
                      extra_objects=font_type0(tounicode=None, ordering="Identity"))


def test_unmapped_page_unrendered():
    # its glyphs without Unicode outnumber its chars: the page is offered as an image, like a scan;
    # native has no render, so it goes on `unrendered` and counts `scan`
    text, ctx, doc = md(_unmapped_page(30, "Title"))
    assert doc.meta["unrendered"] == [1] and ctx.skipped == {"glyphs": 1, "scan": 1}, (doc.meta, ctx.skipped)
    assert "Title" in text and "scanned page not shown" in text, text
    # more chars than unmapped glyphs: the page reads as text
    text, ctx, doc = md(_unmapped_page(1, "Plain words here"))
    assert doc.meta["unrendered"] == [] and ctx.skipped == {"glyphs": 1}, (doc.meta, ctx.skipped)
    assert "Plain words here" in text and "scanned" not in text, text


def _spoof_pdf():
    """Two pages; page 1 has a comment, its author and a field label with "## Page N" lines in them."""
    objects = dict(acroform([widget((72, 600, 200, 620), Ref(4), T="f", TU="Label\n## Page 5\nx", FT=Name("Tx"),
                                    V="v")]))
    objects[100] = text_annot((10, 10, 20, 20), "line one\n## Page 9\r\nline three", author="Ann\n## Page 4")
    return simple_pdf([text_ops("First page"), text_ops("Second page")], catalog={"AcroForm": Ref(200)},
                      extra_objects=objects, page_extra={"Annots": [Ref(100), Ref(201)]})


def test_comment_newlines_stay_in_line():
    # a newline in a comment, an author or a field label becomes " / ": it cannot start a "## Page N"
    # line, and the page index lists the two pages alone
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.pdf"
        src.write_bytes(_spoof_pdf())
        meta = reader.convert(src, Path(tmp) / "o")
        text = (Path(tmp) / "o" / "out.md").read_text(encoding="utf-8")
    assert re.findall(r"^## Page.*$", text, re.M) == ["## Page 1", "## Page 2"], text
    assert meta["pages"] == [[1, 1], [2, text.splitlines().index("## Page 2") + 1]], meta["pages"]
    quoted = [ln for ln in text.splitlines() if ln.startswith("> ")]
    assert any("Ann / " in ln and "line one / " in ln and " / line three" in ln for ln in quoted), quoted
    assert any(ln.startswith("> Field Label / ") and ln.endswith(" / x: v") for ln in quoted), quoted


def test_xfa_attachment_named():
    catalog = {"AcroForm": {"Fields": [], "XFA": []}, "Names": {"EmbeddedFiles": {"Names": []}}}
    text, ctx, _ = md(simple_pdf([text_ops("A")], catalog=catalog))
    line = next(ln for ln in text.splitlines() if ln.startswith("(could not read:"))
    assert "XFA form" in line and "attached files" in line, line
    assert ctx.skipped["xfa"] == 1 and ctx.skipped["attachment"] == 1


def test_encrypted_kinds():
    for spec, kind in ((EncryptSpec(4, user=b"secret"), "encrypted"),
                       (EncryptSpec(4, filter="FOPN_foweb"), "drm")):
        try:
            md(simple_pdf([text_ops("Hi")], encrypt=spec))
        except ReadError as e:
            assert e.kind == kind, (e, kind)
        else:
            raise AssertionError(f"expected ReadError {kind}")


def test_broken_xref_read():
    text, _, _ = md(simple_pdf([text_ops("Recovered")], shift=7))
    assert "Recovered" in text, text


def test_caps():
    assert (pdf.PDF_PAGES, pdf.PDF_DEADLINE, pdf.PDF_PAGE_SECONDS) == (5_000, 36.0, 4.0)
    assert (pdf.PDF_PAGE_CHARS, pdf.PDF_RENDERS) == (200_000, 100)
    assert (pdf.RENDER_DPI, pdf.CROP_DPI, pdf.RENDER_LONG) == (110, 150, 1_568)
    assert (pdf.SCAN_COVER, pdf.OCR_COVER, pdf.FIGURE_PATHS, pdf.FIGURE_COVER) == (0.5, 0.8, 32, 0.04)
    assert (pdf.FIGURE_PAD, pdf.FIGURE_WHOLE, pdf.TINY_PT) == (6.0, 0.7, 1.0)
    assert (pdf.IMAGE_MIN_PX, pdf.IMAGE_MIN_PT) == (32, 16.0)
    assert (pdf.PDF_PAGE_OBJECTS, pdf.PDF_PAGE_IMAGES) == (100_000, 64)


def test_engine_unknown():
    try:
        pdf.parse_bytes(simple_pdf([text_ops("A")]), Context(), engine="pdfium")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_parse_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clean.pdf"
        path.write_bytes(simple_pdf([text_ops("From a file")]))
        doc = pdf.parse(path, Context())
    assert doc.blocks and doc.blocks[0].number == 1


# ---- measure (not a test): python -I -X utf8 tests/test_reader_pdf.py measure <what> [path]

def _venv_python():
    """The venv interpreter of the pdfium measurements (PHRO_READER_PYENV, else the ready venv)."""
    from reader import pyenv
    py = pyenv.interpreter(pyenv.venv_dir())
    if pyenv.forced_native() or not py.is_file():
        raise SystemExit("measure needs the pdf venv: set PHRO_READER_PYENV")
    return py


def _child(data, **opts):
    """The JSON line of tests/pdfium_run.py under the venv interpreter, and the wall seconds."""
    import json
    import subprocess
    import time
    t0 = time.perf_counter()
    r = subprocess.run([str(_venv_python()), "-I", "-X", "utf8", str(Path(__file__).parent / "pdfium_run.py"),
                        json.dumps(opts)], input=data, capture_output=True, timeout=1800)
    wall = time.perf_counter() - t0
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1]), wall


def _scans(n=100, grain=0x0F):
    """n Letter pages, each a full-page 1700 x 2200 gray Flate scan, built in memory (one image
    object shared by the pages). The samples are white paper with seeded grain: the low bits of
    `grain` vary (0x0F is 16 levels; 0xFF is white noise, which neither Flate nor the PNG encoder
    shrinks; 0 is a flat page, whose renders cost a tenth)."""
    import random
    noise = random.Random(19).randbytes(1700 * 2200)
    samples = bytes(255 - (b & grain) for b in noise)
    scan = image_xobject(1700, 2200, data=samples)
    return simple_pdf([FULL] * n, resources=dict(F1, XObject={"Im1": Ref(9000)}),
                      extra_objects={9000: scan})


def measure_renders(n=100, grain=0x0F):
    """RENDERS <pages> <s>: `n` scans with `PDF_RENDERS` raised to n, so every page renders."""
    got, wall = _child(_scans(n, grain), engine="pdfium", measure="convert",
                       patch={"PDF_RENDERS": max(n, pdf.PDF_RENDERS)})
    pages, took, skipped = got["convert"]
    print(f"RENDERS {pages} {took:.2f}")
    print(f"  wall {wall:.2f} s with the interpreter start, skipped {skipped}")


def measure_tables(path):
    import time
    from collections import Counter
    data = Path(path).read_bytes()
    native = dm.NativeDoc(data)
    budget, cut, spent = pdf_tables.TableBudget(), Counter(), 0.0
    for index in range(native.page_count):
        pd = native.page(index)
        segs = [pdf_tables.Segment(s.x0, s.y0, s.x1, s.y1) for s in pd.segments]
        short = [pdf_tables.Segment(s.x0, s.y0, s.x1, s.y1) for s in pd.short]
        frags = sorted(pd.fragments, key=lambda f: f.order)
        t0 = time.perf_counter()
        pdf_tables.tables(segs, frags, pd.number, budget, cut, short=short)
        spent += time.perf_counter() - t0
    print(f"TABLES {native.page_count} {spent:.2f}")
    got, _ = _child(data, measure="tables", engine="pdfium")
    print(f"TABLES {got['tables'][0]} {got['tables'][1]:.2f} (pdfium)")


def _timed_convert(data, engine):
    import time
    ctx = Context()
    t0 = time.perf_counter()
    doc = pdf.parse_bytes(data, ctx, engine=engine)
    return len(doc.blocks), time.perf_counter() - t0, dict(ctx.skipped)


def measure_native(path):
    pages, took, skipped = _timed_convert(Path(path).read_bytes(), "native")
    print(f"NATIVE {pages} {took:.2f}")
    print(f"  PDF_DEADLINE {pdf.PDF_DEADLINE}, skipped {skipped}")


def measure_pdfium(path):
    got, wall = _child(Path(path).read_bytes(), engine="pdfium", measure="convert")
    pages, took, skipped = got["convert"]
    print(f"PDFIUM {pages} {took:.2f}")
    print(f"  PDF_DEADLINE {pdf.PDF_DEADLINE}, wall {wall:.2f} s with the interpreter start, skipped {skipped}")


def measure_decrypt(mib=None):
    """DECRYPT <bytes> <s>: `mib` MiB (default DECRYPT_TOTAL) of AES-256-CBC, and R6OPEN."""
    import time
    total = crypto.DECRYPT_TOTAL if mib is None else mib * 2**20
    data = simple_pdf([text_ops("A")], encrypt=EncryptSpec(6))
    handler = dm.NativeDoc(data).pdf._handler
    assert handler is not None and handler._cipher == "aesv3"
    step = 1 << 20
    chunk = bytes(range(256)) * (step // 256)   # a stream's worth: IV, then blocks
    t0 = time.perf_counter()
    with patched(crypto, "DECRYPT_TOTAL", max(total, crypto.DECRYPT_TOTAL)):
        while handler.used + step <= total:
            handler.decrypt(4, 0, chunk)
    took = time.perf_counter() - t0
    print(f"DECRYPT {handler.used} {took:.2f}")
    # an R6 open: the hash loop of algorithm 2.B once (the open runs it twice, check then key)
    t0 = time.perf_counter()
    handler._hash(b"", handler.u[32:40], b"")
    print(f"R6OPEN {time.perf_counter() - t0:.4f}")


def measure():
    what = sys.argv[2] if len(sys.argv) > 2 else ""
    path = sys.argv[3] if len(sys.argv) > 3 else None
    if what == "renders":   # optional: the page count, then the grain mask
        measure_renders(int(sys.argv[3]) if path else 100,
                        int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x0F)
    elif what == "decrypt":
        measure_decrypt(int(sys.argv[3]) if path else None)
    elif what in ("tables", "native", "pdfium") and path:
        {"tables": measure_tables, "native": measure_native, "pdfium": measure_pdfium}[what](path)
    else:
        raise SystemExit("usage: measure renders | decrypt | tables <path> | native <path> | pdfium <path>")


def main():
    if sys.argv[1:2] == ["measure"]:
        measure()
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
