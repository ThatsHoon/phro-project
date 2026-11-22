"""pdf format module: each page a "## Page N" section with its lines, headings, paragraphs,
tables, images, fields and comments; hidden text, scanned pages, vector figures and the caps.

Run: python -I -X utf8 tests/test_reader_pdf.py
"""
import json
import os
import re
import struct
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PHRO_READER_CACHE", tempfile.mkdtemp())
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (JPEG, JPEG64, NAV_HEADS, EncryptSpec, Name, Ref, acroform, bar_chart, embedded_file,  # noqa: E402
                     font_type0, nav_pdf,
                     image_xobject,
                     link_annot, ocg_catalog, outline_items, rule_ops, simple_pdf, text_annot,
                     text_ops, widget)

import ocr_fix  # noqa: E402
import reader  # noqa: E402
from reader import images, limits, markdown, ocr, raster  # noqa: E402
from reader.ocr_pixels import box_pixels, fit  # noqa: E402
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
    text, ctx, doc, _ = md_assets(data, hidden, **kw)
    return text, ctx, doc


def md_assets(data, hidden=False, **kw):
    """md, and the written assets as {name: bytes}."""
    ctx = Context()
    doc = pdf.parse_bytes(data, ctx, **kw)
    with tempfile.TemporaryDirectory() as tmp:
        text = markdown.render(doc, Path(tmp) / "assets", hidden, ctx.skipped)
        folder = Path(tmp) / "assets"
        assets = {p.name: p.read_bytes() for p in (sorted(folder.iterdir()) if folder.is_dir() else ())}
    return text, ctx, doc, assets


def dims(assets):
    """{name: (width, height)} of PNG assets."""
    return {name: struct.unpack(">II", b[16:24]) for name, b in assets.items()}


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


def test_vector_chart_is_figure():
    """A vector chart is a figure: the test counts what its paths draw. Its labels leave the page's text for one
    `Figure text:` at its place, the paragraphs around it stay; native has no render, so it is unrendered."""
    text, ctx, doc = md(simple_pdf([bar_chart()]))
    assert doc.meta["unrendered"] == [1] and ctx.skipped["figure"] == 1, (doc.meta, ctx.skipped)
    paras = [p for p in text.split("\n\n") if p.strip()]
    fig = [p for p in paras if p.startswith("Figure text: ")]
    assert len(fig) == 1 and "Q1" in fig[0] and "160" in fig[0] and "Sales" not in fig[0], paras
    at = paras.index(fig[0])
    assert paras[at - 1].startswith("Sales") and paras[at + 1].startswith("The next"), paras
    assert "\nQ1" not in text and "\n160\n" not in text, text


def test_ruled_table_joints_are_no_figure():
    """The small squares a word processor fills at a ruled table's joints belong to the table: with a text box's
    frame elsewhere on the page they make no figure, and the lines between them stay the page's text."""
    xs, ys = (66, 226, 386, 546), (246, 260, 274)
    ops = [b"0.5 w"]
    for y in ys:
        ops.append(b"%d %d m %d %d l S" % (xs[0], y, xs[-1], y))
    for x in xs:
        ops.append(b"%d %d m %d %d l S" % (x, ys[0], x, ys[-1]))
    ops += [b"%d %d 1 1 re f" % (x, y) for x in xs for y in ys]
    ops.append(b"316 587 186 33 re f 316 587 186 33 re S")
    body = [text_ops("Bullet %d of the list" % k, x=108, y=582 - 16 * k, size=11) for k in range(3)]
    body.append(text_ops("Subject is here", x=75, y=315, size=11))
    body += [text_ops("Row %d Col %d" % (r, c), x=72 + 160 * c, y=264 - 14 * r, size=11)
             for r in range(2) for c in range(3)]
    text, ctx, doc = md(simple_pdf([b" ".join(ops + body)]))
    assert doc.meta["figures"] == [] and doc.meta["unrendered"] == [] and "Figure text" not in text, (doc.meta, text)
    assert "Bullet 0 of the list" in text and "Subject is here" in text, text


def _icons_page(squares=3):
    """Two columns of prose with an icon (`squares` nested filled squares, 4 pieces each) at three places apart."""
    icons = b" ".join(b" ".join(b"%d %d %d %d re f" % (x + 2 * k, y + 2 * k, 30 - 4 * k, 30 - 4 * k)
                                for k in range(squares))
                      for x, y in ((40, 600), (310, 610), (310, 300)))
    cols = [text_ops("Column %d prose line %d runs on" % (c, k), x=80 + 270 * c, y=700 - 14 * k, size=10)
            for c in range(2) for k in range(30)]
    return icons + b" " + b" ".join(cols)


def test_scattered_icons_are_no_figure():
    """Icons apart in two columns of prose make no figure: the columns' lines are prose by their length though each
    is narrower than half the icons' span, small icons (12 pieces each) or large (36 pieces each)."""
    for squares in (3, 9):
        text, ctx, doc = md(simple_pdf([_icons_page(squares)]))
        assert doc.meta["unrendered"] == [] and "Figure text" not in text and not ctx.skipped, (squares, doc.meta, ctx.skipped, text)
        assert "Column 0 prose line 0 runs on" in text and "Column 1 prose line 29 runs on" in text, text


def _blocks(cells, size=20):
    return b" ".join(b"%d %d %d %d re f" % (x, y, size, size) for x, y in cells)


def _prose(y0, lines=10):
    return b" ".join(text_ops("Prose line %d of the page runs on across the column to its end" % k, x=72,
                              y=y0 - 14 * k, size=10) for k in range(lines))


def test_two_drawings_apart_are_two_figures():
    """Drawings apart on a page are figures each, and the prose between them stays the page's text."""
    upper = _blocks([(100 + 24 * c, 560 + 24 * r) for c in range(10) for r in range(5)])
    lower = _blocks([(100 + 24 * c, 100 + 24 * r) for c in range(10) for r in range(5)])
    text, ctx, doc = md(simple_pdf([upper + b" " + lower + b" " + _prose(480)]))
    assert doc.meta["unrendered"] == [1, 1] and ctx.skipped["figure"] == 2, (doc.meta, ctx.skipped)
    assert "Prose line 0 of the page" in text and "Prose line 9 of the page" in text, text


def test_bars_on_one_axis_are_one_figure():
    """Bars apart that stand on one ruled axis are one drawing; without the axis they are marks apart."""
    bars = _blocks([(100 + 80 * c, 560 + 24 * r) for c in range(3) for r in range(4)])
    rest = _blocks([(500, 100)]) + b" " + _prose(480)
    text, ctx, doc = md(simple_pdf([bars + b" 0.5 w 90 556 m 290 556 l S " + rest]))
    assert doc.meta["unrendered"] == [1] and "Prose line 9 of the page" in text, (doc.meta, text)
    text, ctx, doc = md(simple_pdf([bars + b" " + rest]))
    assert doc.meta["unrendered"] == [] and not ctx.skipped, (doc.meta, ctx.skipped)


def test_drawing_of_miniature_text_is_a_figure():
    """Lines too small to read inside a drawing are its text; lines of body size in a frame are a text box."""
    ring = _blocks([(100 + 24 * c, y) for c in range(11) for y in (560, 680)]
                   + [(x, 584 + 24 * r) for x in (100, 340) for r in range(4)])
    for size, figure in ((1.5, True), (10, False)):
        inner = b" ".join(text_ops("Words of a page shown small, line %02d of it" % k, x=150, y=600 + 12 * k, size=size)
                          for k in range(6))
        text, ctx, doc = md(simple_pdf([ring + b" " + inner + b" " + _prose(480)]))
        assert (doc.meta["unrendered"] == [1]) == figure and ("Figure text: Words" in text) == figure, (size, doc.meta, text)
        assert "line 05 of it" in text and "Prose line 9 of the page" in text, text


def test_table_after_a_line_with_a_superscript_keeps_its_place():
    """A raised citation makes pieces of one line; the table drawn inside the paragraph after it still comes out,
    after the paragraph."""
    full = "Words that fill the whole width of the column of this page, line %d of the text"
    ops = [text_ops(full % 0, x=72, y=700, size=10), text_ops("and the count went up in the species.", x=72, y=688, size=10)]
    ops += [text_ops(t, x=237 + dx, y=692, size=6) for t, dx in (("2", 0), (",", 3.4), ("3", 5.2))]
    ops.append(text_ops("It is therefore said that this goes on to the end", x=247, y=688, size=10))
    ops += [text_ops(full % k, x=72, y=700 - 12 * k, size=10) for k in (2, 3, 4)]
    ops += [rule_ops(72, 590 + 20 * r, 272, 590 + 20 * r) for r in range(4)]
    ops += [rule_ops(72 + 100 * c, 590, 72 + 100 * c, 650) for c in range(3)]
    ops += [text_ops("cell %d%d" % (r, c), x=80 + 100 * c, y=596 + 20 * r, size=10) for r in range(3) for c in range(2)]
    ops.append(text_ops(full % 5, x=72, y=560, size=10))
    text, ctx, _doc = md(simple_pdf([b" ".join(ops)]))
    assert text.count("cell 00") == 1 and text.count("cell 21") == 1 and not ctx.skipped, (text, ctx.skipped)
    assert text.index("line 4 of the text") < text.index("cell 21") < text.index("line 5 of the text"), text
    assert "species.2,3It is therefore said" in text, text


def test_picture_between_paragraphs_of_its_column():
    """A picture in the right column goes between that column's paragraphs, not into the left column's text."""
    left = b" ".join(text_ops("Left column line %d of text" % k, x=72, y=700 - 14 * k, size=10) for k in range(30))
    right = (b" ".join(text_ops("Right line %d above" % k, x=330, y=700 - 14 * k, size=10) for k in range(5))
             + b" " + b" ".join(text_ops("Right line %d below" % k, x=330, y=400 - 14 * k, size=10) for k in range(5)))
    picture = b" q 200 0 0 100 330 520 cm /Im1 Do Q "
    text, _, _ = md(with_image(left + b" " + right + picture, image_xobject(64, 64, data=bytes(range(64)) * 64)))
    at = text.index("![")
    assert text.index("Right line 4 above") < at < text.index("Right line 0 below"), text
    assert not text.index("Left column line 0") < at < text.index("Left column line 29"), text


def test_picture_does_not_split_a_paragraph():
    """A picture whose top falls inside a paragraph's lines goes after that paragraph."""
    lines = b" ".join(text_ops("Wrapped paragraph line %d of text" % k, x=72, y=700 - 14 * k, size=10)
                      for k in range(8))
    after = text_ops("Next paragraph starts here", x=72, y=500, size=10)
    picture = b" q 200 0 0 100 150 560 cm /Im1 Do Q "
    text, _, _ = md(with_image(picture + lines + b" " + after, image_xobject(64, 64, data=bytes(range(64)) * 64)))
    at = text.index("![")
    assert "line 3 of text Wrapped" in text and "line 4 of text" in text, text
    assert text.index("line 7 of text") < at < text.index("Next paragraph"), text


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


def test_field_at_its_place_comments_end():
    text, _, _ = md(_form_pdf())
    assert "\n\nField Name: Kim\n\n" in text and text.rstrip().endswith("> Comment by Ann: note"), text
    assert text.index("Body text") < text.index("Field Name") < text.index("> Comment"), text


def test_fields_every_kind_at_their_place():
    """Every field but a push button is out, at the line of its widget: an empty one as (empty), a check box checked
    or not, a choice with its options' display texts and the display text of its value, a signature."""
    fields = acroform([
        widget((300, 690, 400, 705), Ref(4), T="Empty", FT=Name("Tx")),
        widget((300, 650, 315, 665), Ref(4), T="Agree", FT=Name("Btn"), V=Name("Off")),
        widget((300, 610, 400, 625), Ref(4), T="Colour", FT=Name("Ch"), Ff=1 << 17, V="g",
               Opt=[["r", "Red"], ["g", "Green"], "Blue"]),
        widget((300, 570, 400, 585), Ref(4), T="Sign", FT=Name("Sig")),
        widget((300, 530, 400, 545), Ref(4), T="Go", FT=Name("Btn"), Ff=1 << 16),
    ])
    body = b" ".join(text_ops(t, y=y) for t, y in (("Your name", 695), ("Do you agree", 655),
                                                  ("Pick a colour", 615), ("Signature", 575), ("The end", 500)))
    annots = [Ref(n) for n, obj in sorted(fields.items()) if "Rect" in obj]
    text, _, _ = md(simple_pdf([body], catalog={"AcroForm": Ref(200)}, extra_objects=fields,
                               page_extra={"Annots": annots}))
    paras = [p for p in text.split("\n\n") if p.strip()]
    want = ["Your name", "Field Empty: (empty)", "Do you agree", "Field Agree (checkbox): not checked",
            "Pick a colour", "Field Colour (choice: Red, Green, Blue): Green", "Signature",
            "Field Sign (signature): (empty)", "The end"]
    assert [p.strip() for p in paras if not p.startswith("##")] == want, paras


def test_hidden_comment():
    text, _, _ = md(_form_pdf(flags=2))
    assert "note" not in text and text.splitlines()[0] == "(1 hidden places left out)", text
    assert "\nField Name: Kim\n" in text


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


HIDDEN_FORM_LINES = ("(hidden) Field Hid: hidden value", "(hidden) Field NoView: noview value",
                     "> (hidden) Comment by Ann: noview note")


def test_hidden_fields():
    # a field all of whose widgets are Hidden or NoView, and a NoView comment, are hidden places
    data = _hidden_form_pdf()
    text, _, _ = md(data)
    assert text.splitlines()[0] == "(3 hidden places left out)", text
    assert "\nField Two: two value\n" in text and "\nField Seen: seen value\n" in text, text
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


def test_outline_title_over_two_lines_is_one_heading():
    data = simple_pdf([text_ops("Paying Wages, Pensions,", y=720) + b" " + text_ops("or  Annuities", y=706)
                       + b" " + text_ops("Body line one that is the text", y=680)
                       + b" " + text_ops("Body line two", y=666)],
                      catalog={"Outlines": Ref(300)},
                      extra_objects=outline_items([(1, "Paying Wages, Pensions, or Annuities", 0)]))
    text, _, _ = md(data)
    assert "### Paying Wages, Pensions, or Annuities" + chr(10) in text and text.count("Annuities") == 1, text


def test_outline_and_page_labels():
    """An outline title matching no line is a heading where its destination's top is, or at its page's top; a page's
    label names its section."""
    text, _, _ = md(nav_pdf())
    assert re.findall(r"^#+ .*$", text, re.M) == NAV_HEADS, text
    assert text.index("Body line one.") < text.index("### Second part") < text.index("Body line two."), text
    assert text.index("### Appendix") < text.index("Page two text."), text


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
    real = pdf_layout._groups

    def groups(lns, *body):
        if any(ln.text == "P2" for ln in lns):
            raise ValueError("broken paragraphs")
        return real(lns, *body)

    with patched(pdf_layout, "_groups", groups):
        text, ctx, _ = md(simple_pdf([text_ops(f"P{n}") for n in range(1, 4)]))
    assert "P1" in text and "P3" in text and "P2" not in text, text
    assert ctx.skipped == {"page": 1}, ctx.skipped

    def timeout(lns, *body):
        raise ReadError("timeout", "paragraphs")

    with patched(pdf_layout, "_groups", timeout):
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
        meta = reader.convert(src, Path(tmp) / "o1", started=time.monotonic())
        assert "1 text run without Unicode" in meta["unread"], meta
        assert "garbled" not in meta["warnings"], meta
        src2 = Path(tmp) / "b.pdf"
        src2.write_bytes(simple_pdf([b"BT /F2 12 Tf 72 700 Td <0001> Tj ET"], resources=res,
                                    extra_objects=font_type0(tounicode={1: "�" * 12})))
        meta = reader.convert(src2, Path(tmp) / "o2", started=time.monotonic())
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
        meta = reader.convert(src, Path(tmp) / "o", started=time.monotonic())
        text = (Path(tmp) / "o" / "out.md").read_text(encoding="utf-8")
    assert re.findall(r"^## Page.*$", text, re.M) == ["## Page 1", "## Page 2"], text
    assert meta["pages"] == [[1, 1], [2, text.splitlines().index("## Page 2") + 1]], meta["pages"]
    quoted = [ln for ln in text.splitlines() if ln.startswith("> ")]
    assert any("Ann / " in ln and "line one / " in ln and " / line three" in ln for ln in quoted), quoted
    assert any(ln.startswith("Field Label / ") and ln.endswith(" / x: v") for ln in text.splitlines()), text


def test_xfa_attachment_named():
    catalog = {"AcroForm": {"Fields": [], "XFA": []}, "Names": {"EmbeddedFiles": {"Names": [b"a", Ref(500)]}}}
    text, ctx, _ = md(simple_pdf([text_ops("A")], catalog=catalog, extra_objects=embedded_file(500, "a.txt")))
    line = next(ln for ln in text.splitlines() if ln.startswith("(could not read:"))
    assert "XFA form" in line and "attached files" in line, line
    assert ctx.skipped["xfa"] == 1 and ctx.skipped["attachment"] == 1


def test_attachments_listed():
    """A document's embedded files are listed at its start, a file attachment annotation's where it sits."""
    files = {**embedded_file(500, "data.csv", b"a,b\n1,2\n", kind="text/csv"), **embedded_file(502, "note.txt")}
    annot = dict(text_annot((72, 600, 90, 620), "see file", subtype="FileAttachment"), FS=Ref(502))
    body = text_ops("Above", y=650) + b" " + text_ops("Below", y=500)
    text, ctx, _ = md(simple_pdf([body], catalog={"Names": {"EmbeddedFiles": {"Names": [b"d", Ref(500)]}}},
                                 extra_objects={**files, 510: annot}, page_extra={"Annots": [Ref(510)]}))
    paras = [p for p in text.split("\n\n") if p.strip() and not p.startswith(("#", "(could not", ">"))]
    assert paras == ["Attached file: data.csv (8 bytes, text/csv)", "Above",
                     "Attached file: note.txt (3 bytes, text/plain)", "Below"], text
    assert ctx.skipped["attachment"] == 1


def test_encrypted_payload_is_drm():
    """A Microsoft IRM wrapper (an unencrypted PDF whose real document is an encrypted payload) is `drm`."""
    wrapped = embedded_file(500, "Protected.pdf", AFRelationship=Name("EncryptedPayload"),
                            EP={"Type": Name("EncryptedPayload"), "Subtype": Name("MicrosoftIRMServices")})
    catalog = {"AF": [Ref(500)], "Names": {"EmbeddedFiles": {"Names": [b"p", Ref(500)]}}}
    try:
        md(simple_pdf([text_ops("This document is protected")], catalog=catalog, extra_objects=wrapped))
    except ReadError as e:
        assert e.kind == "drm" and "MicrosoftIRMServices" in e.detail, e
    else:
        raise AssertionError("an IRM wrapper read")


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
    assert (pdf.SCAN_COVER, pdf.FIGURE_PATHS, pdf.FIGURE_COVER) == (0.5, 32, 0.03)
    assert (pdf.FIGURE_PAD, limits.FIGURE_WHOLE, pdf.TINY_PT) == (6.0, 0.7, 1.0)
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


# ---- scanned pages read by OCR, through the stand-in worker (tests/ocr_stub_worker.py)

SCANNED = "_Scanned page, text by OCR._"
KO = "ko-malgun-11.150dpi"


ocr_stub = ocr_fix.ocr_stub


def fixture_gray():
    """The Korean fixture's gray."""
    return ocr_fix.load(KO)


def fixture_scan(pages=1, g=None):
    """A pdf of `pages` Letter pages, each `g` (`fixture_gray()` if None) as a full-page Flate image; and the gray."""
    g = g or fixture_gray()
    data = simple_pdf([FULL] * pages, resources=dict(F1, XObject={"Im1": Ref(30)}),
                      extra_objects={30: image_xobject(g.w, g.h, data=g.data)})
    return data, g


def canned(g, unread=None):
    """A worker answer for a page of `g`'s size: two paragraphs, the first on two lines."""
    def line(texts, y, n):
        out, x = [], 100
        for t in texts:
            out.append({"text": t, "x": x, "y": y, "w": 24 * len(t), "h": 30, "line": n, "conf": None})
            x += 24 * len(t) + 18
        return out

    words = (line(["Alpha", "beta", "gamma"], 200, 0) + line(["delta", "epsilon"], 240, 1)
             + line(["Second", "paragraph", "here"], 400, 2))
    page = {"w": g.w, "h": g.h, "words": words, "angle": 0.0, "turn": 0, "engine": "stub", "version": "1"}
    return {"page": page, "rules": [], "unread": unread or {"share": 0.0, "box": None, "area": 0.0}}


PARAS = "Alpha beta gamma delta epsilon\n\nSecond paragraph here"
READ = "(1 scanned page read by OCR)\n\n"  # the status line of one page OCR read


def test_scanned_page_text_with_stub_worker():
    data, g = fixture_scan()
    with ocr_stub(answer=canned(g)):
        text, ctx, doc = md(data)
    assert text == f"{READ}## Page 1\n\n{SCANNED}\n\n{PARAS}\n", text
    assert doc.meta["ocr"] == {"pages": 1, "doubtful": 0, "failed": 0, "stopped_at": None}, doc.meta
    assert not ctx.skipped and doc.meta["unrendered"] == [] and not doc.warnings, (ctx.skipped, doc.meta)


def test_page_without_gray_not_read_as_picture():
    data, g = fixture_scan()
    with patched(pdf._Reader, "_gray", lambda self, make: None), ocr_stub(answer=canned(g)) as (_, sent):
        text, _, doc = md(data)
    assert text == "## Page 1\n\n![Scanned page 1](assets/img-001.png)\n" and sent == [], (text, sent)


def test_doubtful_page_text_then_image():
    data, g = fixture_scan()
    with ocr_stub(answer=canned(g, {"share": 0.5, "box": None, "area": 0.0})):
        text, _, doc, assets = md_assets(data)
    assert text == ("(1 scanned page read by OCR; 1 of them doubtful, shown with the page image)\n\n"
                    f"## Page 1\n\n{SCANNED}\n\n{PARAS}\n\n![Scanned page 1](assets/img-001.png)\n"), text
    assert dims(assets) == {"img-001.png": (g.w, g.h)}, dims(assets)
    assert doc.meta["ocr"] == {"pages": 1, "doubtful": 1, "failed": 0, "stopped_at": None}, doc.meta


def test_scan_with_photo_gets_crop_link():
    data, g = fixture_scan()
    box = [0.1, 0.18, 0.7, 0.68]  # 30 % of the page, its top between the first paragraph and the second
    with ocr_stub(answer=canned(g, {"share": 0.02, "box": box, "area": 0.3, "density": 0.5})):
        text, _, doc, assets = md_assets(data)
    x0, y0, x1, y1 = box_pixels(g.w, g.h, box)
    assert text == (f"{READ}## Page 1\n\n{SCANNED}\n\nAlpha beta gamma delta epsilon\n\n"
                    "![Picture on page 1](assets/img-001.png)\n\nSecond paragraph here\n"), text
    assert dims(assets) == {"img-001.png": (x1 - x0, y1 - y0)}, dims(assets)
    assert doc.meta["ocr"] == {"pages": 1, "doubtful": 0, "failed": 0, "stopped_at": None}, doc.meta
    whole = [0.0, 0.1, 1.0, 0.85]  # 75 %: past FIGURE_WHOLE, the whole page instead
    with ocr_stub(answer=canned(g, {"share": 0.02, "box": whole, "area": 0.75, "density": 0.5})):
        text, _, doc, assets = md_assets(data)
    assert "![Picture on page 1](assets/img-001.png)" in text, text
    assert dims(assets) == {"img-001.png": (g.w, g.h)}, dims(assets)


def test_turned_page_crop_cut_from_the_page_image():
    """OCR read the page turned (its boxes are in the turned bitmap's frame): the crop is cut from the page image as
    it stands, at the box turned back."""
    data, g = fixture_scan()
    page_png = md_assets(data)[3]["img-001.png"]
    box = [0.1, 0.18, 0.7, 0.68]
    for turn, back in ((90, (0.18, 0.3, 0.68, 0.9)), (180, (0.3, 0.32, 0.9, 0.82)), (270, (0.32, 0.1, 0.82, 0.7))):
        answer = canned(g, {"share": 0.02, "box": box, "area": 0.3, "density": 0.5})
        side = (g.h, g.w) if turn in (90, 270) else (g.w, g.h)
        answer["page"].update(turn=turn, w=side[0], h=side[1])
        with ocr_stub(answer=answer):
            text, _, _, assets = md_assets(data)
        assert "![Picture on page 1](assets/img-001.png)" in text, text
        assert assets["img-001.png"] == images.png_crop(page_png, back), (turn, dims(assets))


def test_failed_worker_page_is_image():
    data, g = fixture_scan()
    with ocr_stub(die_after=0):
        text, ctx, doc = md(data)
    assert text == "(OCR failed on 1 page; shown as an image.)\n\n## Page 1\n\n![Scanned page 1](assets/img-001.png)\n", text
    assert doc.meta["ocr"] == {"pages": 0, "doubtful": 0, "failed": 1, "stopped_at": None}, doc.meta
    assert not ctx.skipped and not doc.warnings


def test_late_pages_are_images_and_named():
    data, g = fixture_scan(pages=2)
    with ocr_stub(deadline=time.monotonic() + limits.FIGURE_RESERVE + 1, delay=30, answer=canned(g)):
        text, ctx, doc = md(data)
    assert text.count("![Scanned page") == 2 and SCANNED not in text, text
    assert doc.meta["ocr"] == {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": 1}, doc.meta
    assert doc.warnings == [limits.OCR_LATE] and not ctx.skipped, (doc.warnings, ctx.skipped)


def test_full_queue_stops_ocr_not_the_page_loop():
    """Past OCR_QUEUE_BYTES a page waits for room only until the loop's deadline less a page's time: that page is then
    late, OCR stops there for this read, and every page still comes out, the rest as images."""
    data, g = fixture_scan(pages=8, g=fit(fixture_gray(), None, max_side=440))  # small: 8 pages stay quick
    saved = limits.OCR_QUEUE_BYTES
    limits.OCR_QUEUE_BYTES = 1
    try:
        deadline = time.monotonic() + limits.FIGURE_RESERVE + 10
        with patched(pdf, "PDF_DEADLINE", 3.0), ocr_stub(deadline=deadline, delay=1, answer=canned(g)) as (s, sent):
            text, ctx, doc = md(data, started=time.monotonic())
    finally:
        limits.OCR_QUEUE_BYTES = saved
    stop = doc.meta["ocr"]["stopped_at"]
    assert not ctx.skipped and stop is not None, (ctx.skipped, doc.meta["ocr"])
    assert len(sent) == stop and doc.meta["ocr"]["pages"] == stop - 1, (len(sent), doc.meta["ocr"])
    assert text.count(SCANNED) == stop - 1 and text.count("![Scanned page") == 9 - stop, text
    assert doc.warnings == [limits.OCR_LATE], doc.warnings


def test_ocr_layer_page_not_ocred():
    content = FULL + text_ops("Scanned words", y=400, extra=b"3 Tr")
    with ocr_stub() as (s, sent):
        text, _, doc = md(with_image(content, image_xobject(64, 64)))
    assert sent == [] and "Scanned words" in text and "![" not in text and SCANNED not in text, (sent, text)
    assert doc.meta["ocr"]["pages"] == 0


PICTURE = b"q 200 0 0 200 100 300 cm /Im1 Do Q "


def test_picture_under_text_layer_left_out():
    """Decision 8: a picture the page's own text lies under (invisible text over it, or most of the page's text drawn
    before it: an OCR layer) is not a picture of the output, nor read by OCR, its text being out already, nor is the
    same picture drawn again on that page; a picture with a label drawn over it, one drawn over a label on a page of
    other text, or one with none, is, and OCR reads it."""
    image = image_xobject(64, 64, data=bytes(range(64)) * 64)
    body = text_ops("The page's own paragraph of text", y=700)
    for content, read in ((PICTURE + text_ops("Layer words", x=150, y=400, extra=b"3 Tr"), False),
                          (text_ops("Layer words", x=150, y=400) + b" " + PICTURE, False),
                          (PICTURE + text_ops("Layer words", x=150, y=400) + b" " + PICTURE, False),
                          (text_ops("Label", x=150, y=400) + b" " + PICTURE + body, True),
                          (PICTURE + text_ops("Label", x=150, y=400), True),
                          (PICTURE + text_ops("Caption", x=72, y=200), True)):
        with ocr_stub() as (s, sent):
            text, _, doc = md(with_image(content, image))
        assert [k for _, _, k in sent] == (["picture"] if read else []), (content, sent)
        assert ("](assets/img-001.png)" in text) == read, text


def test_flat_picture_left_out():
    """Decision 8: a picture of one colour (every channel within OCR_FLAT levels) is left out, uncounted; one with
    any detail is kept."""
    for data, kept in ((bytes(64 * 64), False), (bytes([200 + (k % 7) for k in range(64 * 64)]), False),
                       (bytes(range(64)) * 64, True)):
        text, ctx, _ = md(with_image(PICTURE + text_ops("Caption", x=72, y=200), image_xobject(64, 64, data=data)))
        assert ("![" in text) == kept and not ctx.skipped, (kept, text, ctx.skipped)
    rgb = image_xobject(64, 64, cs="DeviceRGB", data=bytes([10, 120, 240]) * 4096)
    text, ctx, _ = md(with_image(PICTURE + text_ops("Caption", x=72, y=200), rgb))
    assert "![" not in text and not ctx.skipped, (text, ctx.skipped)


def test_picture_off_the_page_left_out():
    """A picture drawn wholly outside the page's crop is not seen: left out, uncounted; one reaching into it is kept."""
    for place, kept in ((b"q 200 0 0 200 -400 300 cm /Im1 Do Q ", False), (b"q 200 0 0 200 -100 300 cm /Im1 Do Q ", True)):
        text, ctx, _ = md(with_image(place + text_ops("Caption", x=72, y=200), image_xobject(64, 64, data=bytes(range(64)) * 64)))
        assert ("![" in text) == kept and not ctx.skipped, (kept, text, ctx.skipped)


def test_picture_encoded_once_chosen():
    """A page picture is encoded to PNG only once it is chosen for the output, at zlib level 1: a picture under the
    page's text and a flat one are never encoded."""
    levels, real = [], images._png_rows

    def png_rows(width, height, ctype, rows, level):
        levels.append(level)
        return real(width, height, ctype, rows, level)

    detail = image_xobject(64, 64, data=bytes(range(64)) * 64)
    other = image_xobject(64, 64, data=bytes(range(63, -1, -1)) * 64)
    flat = image_xobject(64, 64)
    res = dict(F1, XObject={"Im1": Ref(30), "Im2": Ref(31), "Im3": Ref(32)})
    content = (PICTURE + text_ops("Layer words", x=150, y=400, extra=b"3 Tr")
               + b" q 100 0 0 100 300 100 cm /Im2 Do Q q 100 0 0 100 300 600 cm /Im3 Do Q "
               + text_ops("Caption", x=72, y=80))
    data = simple_pdf([content], resources=res, extra_objects={30: detail, 31: flat, 32: other})
    with patched(images, "_png_rows", png_rows):
        text, ctx, _, assets = md_assets(data)
    assert levels == [1] and list(assets) == ["img-001.png"] and not ctx.skipped, (levels, assets, ctx.skipped)
    png = assets["img-001.png"]
    idat = png.index(b"IDAT") + 4
    assert png[idat:idat + 2] == b"x", png[idat:idat + 2]


def _image_bytes_at_after(data, **conf):
    """The reader's image bytes once OCR is collected (as `_after` starts), the Markdown and the Doc."""
    seen, real = [], pdf._Reader._after

    def after(self, kept):
        seen.append(self.image_bytes)
        return real(self, kept)

    with patched(pdf._Reader, "_after", after), ocr_stub(**conf):
        text, _, doc = md(data)
    return seen[0], text, doc


def test_trusted_page_refunds_image_bytes():
    data, g = fixture_scan()
    trusted, text, _ = _image_bytes_at_after(data, answer=canned(g))
    assert trusted == 0 and "![" not in text, (trusted, text)
    doubtful, text, _ = _image_bytes_at_after(data, answer=canned(g, {"share": 0.5, "box": None, "area": 0.0}))
    page_png = md_assets(data)[3]["img-001.png"]
    assert doubtful == len(page_png) and "![Scanned page 1]" in text, (doubtful, len(page_png))


def test_ocr_answer_that_raises_leaves_the_page_as_today():
    """A step of placing an OCR answer that raises, before or after the page image's refund, leaves the page as it
    was: today's image, still charged, no OCR text; the page counts as failed."""
    from reader import ocr_layout
    data, g = fixture_scan()
    page_png = md_assets(data)[3]["img-001.png"]

    def boom(*a, **kw):
        raise RuntimeError("boom")

    photo = {"share": 0.02, "box": [0.1, 0.18, 0.7, 0.68], "area": 0.3, "density": 0.5}
    for obj, name, unread in ((ocr_layout, "segments", None), (images, "png_crop", photo)):
        with patched(obj, name, boom):
            charged, text, doc = _image_bytes_at_after(data, answer=canned(g, unread))
        assert text == ("(could not read: 1 page read in part)\n\n(OCR failed on 1 page; shown as an image.)\n\n"
                        "## Page 1\n\n![Scanned page 1](assets/img-001.png)\n"), (name, text)
        assert charged == len(page_png), (name, charged, len(page_png))
        assert doc.meta["ocr"] == {"pages": 0, "doubtful": 0, "failed": 1, "stopped_at": None}, (name, doc.meta)


def test_renders_counted_once_per_page():
    data, g = fixture_scan(pages=2)
    with patched(pdf, "PDF_RENDERS", 1), ocr_stub(answer=canned(g)) as (s, sent):
        text, ctx, doc = md(data)
    assert len(sent) == 1 and sent[0][2] == "page", sent
    assert text.count(SCANNED) == 1 and text.index(SCANNED) < text.index("## Page 2"), text
    assert "![" not in text and doc.meta["unrendered"] == [2] and ctx.skipped == {"scan": 1}, (doc.meta, ctx.skipped)
    assert doc.meta["ocr"]["pages"] == 1


# captured from the code before OCR reached pdf pages (3925efe): a Flate scan, a text page and a JPEG scan
NO_OCR = ("## Page 1\n\n![Scanned page 1](assets/img-001.png)\n\n## Page 2\n\nPlain words\n\n"
          "## Page 3\n\n![Scanned page 3](assets/img-002.jpg)\n")


def test_no_session_output_unchanged():
    g = fixture_gray()
    data = simple_pdf([FULL, text_ops("Plain words"), b"q 612 0 0 792 0 0 cm /Im2 Do Q "],
                      resources=dict(F1, XObject={"Im1": Ref(30), "Im2": Ref(31)}),
                      extra_objects={30: image_xobject(g.w, g.h, data=g.data),
                                     31: image_xobject(8, 8, data=JPEG, filter="DCTDecode")})
    assert ocr.session() is None
    text, ctx, doc = md(data)
    assert text == NO_OCR, text
    assert not ctx.skipped and "ocr" not in doc.meta, doc.meta


def test_native_scan_uses_page_image():
    if sys.platform != "win32":
        print("skip: test_native_scan_uses_page_image (needs GDI+)")
        return
    decoded, real = [], raster.gray

    def gray(data, **kw):
        decoded.append((data, kw.get("deadline")))
        return real(data, **kw)

    deadline = time.monotonic() + 60
    with patched(raster, "gray", gray), ocr_stub(deadline=deadline) as (s, sent):
        text, _, doc = md(with_image(FULL, image_xobject(64, 64, data=JPEG64, filter="DCTDecode")))
    assert decoded == [(JPEG64, deadline - limits.FIGURE_RESERVE)], decoded
    assert len(sent) == 1 and sent[0][:3] == (64, 64, "page"), sent
    assert text == f"{READ}## Page 1\n\n{SCANNED}\n\nw64\n", text
    assert doc.meta["ocr"]["pages"] == 1


def test_native_jpeg_scan_photo_cropped():
    """A native JPEG scan with a photo on 30 % of it links a crop of that photo, cut from the pixels the reader
    decodes, not the whole page."""
    if sys.platform != "win32":
        print("skip: test_native_jpeg_scan_photo_cropped (needs GDI+)")
        return
    data = with_image(FULL, image_xobject(64, 64, data=JPEG64, filter="DCTDecode"))
    g = raster.gray(JPEG64)[0]
    box = [0.1, 0.18, 0.7, 0.68]
    with ocr_stub(answer=canned(g, {"share": 0.02, "box": box, "area": 0.3, "density": 0.5})):
        text, _, doc, assets = md_assets(data)
    x0, y0, x1, y1 = box_pixels(g.w, g.h, box)
    assert "![Picture on page 1](assets/img-001.png)" in text, text
    assert dims(assets) == {"img-001.png": (x1 - x0, y1 - y0)}, dims(assets)
    assert doc.meta["ocr"]["pages"] == 1


def test_small_native_scan_not_sent():
    """A scanned page whose pixels have a long side under OCR_MIN_EDGE is not read: today's image, not read again as a
    picture."""
    if sys.platform != "win32":
        print("skip: test_small_native_scan_not_sent (needs GDI+)")
        return
    with ocr_stub() as (s, sent):
        text, ctx, doc = md(with_image(FULL, image_xobject(8, 8, data=JPEG, filter="DCTDecode")))
    assert sent == [] and text == "## Page 1\n\n![Scanned page 1](assets/img-001.jpg)\n", (sent, text)
    assert doc.meta["ocr"] == {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": None} and not ctx.skipped


def _answer(p):
    """The stand-in worker's answer for an OcrPage."""
    words = [{"text": w.text, "x": w.x, "y": w.y, "w": w.w, "h": w.h, "line": w.line, "conf": None} for w in p.words]
    page = {"w": p.w, "h": p.h, "words": words, "angle": 0.0, "turn": 0, "engine": "stub", "version": "1"}
    return {"page": page, "rules": [], "unread": {"share": 0.0, "box": None, "area": 0.0}}


def test_scanned_two_columns_read_in_column_order():
    """Engine lines across both columns (Tesseract psm 4) read left column, then right, as a lone TIFF page reads."""
    from test_reader_ocr_layout import two_columns
    data, _ = fixture_scan()
    with ocr_stub(answer=_answer(two_columns(True))):
        text, _, _ = md(data)
    want = [f"L{r}{h}" for r in range(4) for h in "ab"] + [f"R{r}{h}" for r in range(4) for h in "ab"]
    assert re.findall(r"[LR]\d[ab]", text) == want, text


def test_native_scan_leaves_other_images_to_the_picture_pass():
    """The native engine reads only the scan image's own pixels: another picture on the page is not in them, so it
    is read as a picture of its own. (Through pdfium, OCR reads a render of the whole page: test_reader_pdfium.)"""
    g = fixture_gray()
    data = simple_pdf([FULL + b"q 100 0 0 100 72 72 cm /Im2 Do Q"],
                      resources=dict(F1, XObject={"Im1": Ref(30), "Im2": Ref(31)}),
                      extra_objects={30: image_xobject(g.w, g.h, data=g.data),
                                     31: image_xobject(64, 64, data=bytes(range(64)) * 64)})
    with ocr_stub(answer=canned(g)) as (s, sent):
        text, _, _ = md(data)
    assert [k for _, _, k in sent] == ["page", "picture"], sent
    assert text.count("](assets/") == 1 and SCANNED in text and "Picture text" in text, text


def _rendered(png):
    """NativeDoc.page patched so every page renders as `png`."""
    real = dm.NativeDoc.page

    def page(self, index):
        pd = real(self, index)
        pd.render = lambda box, dpi, gray=False: png
        return pd
    return patched(dm.NativeDoc, "page", page)


def test_figure_render_with_text_not_read_as_picture():
    """A vector figure render holding the page's own text is not read by OCR (its text is already out); one holding
    none is, and so is a page with no text (outlined letters)."""
    from reader.ocr_pixels import Gray
    png = images.gray_png(Gray(200, 200, bytes(range(200)) * 200))
    crop = b"".join(b"%d %d 5 5 re f " % (100 + 45 * (i % 6), 100 + 45 * (i // 6)) for i in range(40))
    whole = b"".join(b"%d %d 5 5 re f " % (20 + 110 * (i % 6), 20 + 110 * (i // 6)) for i in range(42))
    for body, words, read in ((crop, text_ops("Label", x=150, y=200), False),
                              (whole, text_ops("Caption", y=740), False),
                              (crop, text_ops("Caption", y=740), True),
                              (crop, b"", True)):
        with _rendered(png), ocr_stub() as (s, sent):
            text, _, doc = md(simple_pdf([words + b" " + body]))
        assert doc.meta["figures"] == [1], doc.meta
        assert [k for _, _, k in sent] == (["picture"] if read else []), (words, sent)
        assert "Picture text" not in text, text


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


def test_overlapping_figures_are_merged_within_a_budget():
    """A page of many figures whose boxes overlap, among many lines, is judged within the page's time."""
    from types import SimpleNamespace
    page, step, run = (0.0, 0.0, 4800.0, 4800.0), 36, 900
    marks, lines = [], []
    for i in range(110):
        x0, y0 = i * step, 4800 - run - i * step
        marks += [((x0 + 12 * t + 2.0, y0 + 12 * t + 2.0, x0 + 12 * t + 6.0, y0 + 12 * t + 6.0), 4, False)
                  for t in range(run // 12)]
        lines.append(SimpleNamespace(box=(x0 - 2.0, y0 - step + 14.0, x0 + 44.0, y0 - step + 18.0), size=4.0,
                                     text="Prose words of body %03d" % i))
    lines += [SimpleNamespace(box=(20.0 + 6 * (k // 300), 20.0 + 5 * (k % 300), 22.0 + 6 * (k // 300), 24.0 + 5 * (k % 300)),
                              size=4.0, text="a") for k in range(20000)]
    t = time.perf_counter()
    pdf._figure_boxes(marks, page, [], lines)
    assert time.perf_counter() - t < 3.0, time.perf_counter() - t


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
