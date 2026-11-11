"""PowerPoint (pptm, ppsx) parser: order, hidden, title, text, groups, tables, charts, notes, hostile input.

Run: python -I -X utf8 tests/test_reader_slides.py
"""
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_zip  # noqa: E402

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      f'xmlns:r="{REL}" '
      'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"')
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
MAIN = {"ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
        "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"}


def tmp():
    return Path(tempfile.mkdtemp())


def rels_xml(items):
    return (f'<Relationships xmlns="{PKG}">'
            + "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{tg}"/>' for i, t, tg in items)
            + "</Relationships>")


def sld(shapes, show=None):
    s = f' show="{show}"' if show is not None else ""
    return f'<p:sld {NS}{s}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>'


def deck(slides, order=None, notes=None, kind="ppsx", extra=None, srels=None, ids=None):
    """slides: spTree inner xml (or a full <p:sld>), stored as slide1.xml.. ; order: 1-based slide numbers listed."""
    notes = notes or {}
    order = order if order is not None else list(range(1, len(slides) + 1))
    srels = srels or {}
    lst = "".join(f'<p:sldId id="{256 + i}" r:id="rS{n}"/>' for i, n in enumerate(order))
    members = {
        "[Content_Types].xml": ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                f'<Override PartName="/ppt/presentation.xml" ContentType="{MAIN[kind]}"/></Types>'),
        "_rels/.rels": rels_xml([("rId1", "officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": f'<p:presentation {NS}><p:sldIdLst>{lst}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels_xml(
            [(f"rS{i}", "slide", f"slides/slide{i}.xml") for i in range(1, len(slides) + 1)]),
    }
    for i, s in enumerate(slides, 1):
        members[f"ppt/slides/slide{i}.xml"] = s if s.startswith("<p:sld") else sld(s)
        items = list(srels.get(i, []))
        if i in notes:
            items.append(("rN", "notesSlide", f"../notesSlides/notesSlide{i}.xml"))
            members[f"ppt/notesSlides/notesSlide{i}.xml"] = sld(notes[i]).replace("p:sld ", "p:notes ").replace(
                "</p:sld>", "</p:notes>")
        if items:
            members[f"ppt/slides/_rels/slide{i}.xml.rels"] = rels_xml(items)
    members.update(extra or {})
    return make_zip(tmp() / ("a." + kind), members)


def sp(text="", ph=None, off=None, ext=None, extra_nv="", paras=None, hidden=False):
    phx = f'<p:ph type="{ph}"/>' if ph and ph != "-" else ("<p:ph/>" if ph == "-" else "")
    h = ' hidden="1"' if hidden else ""
    xf = ""
    if off:
        xf = f'<a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{(ext or (1, 1))[0]}" cy="{(ext or (1, 1))[1]}"/></a:xfrm>'
    body = paras if paras is not None else f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"{h}/><p:cNvSpPr/><p:nvPr>{phx}</p:nvPr></p:nvSpPr>'
            f'<p:spPr>{xf}</p:spPr><p:txBody><a:bodyPr/>{body}</p:txBody></p:sp>')


def grp(children, off, ext, choff, chext, hidden=False):
    h = ' hidden="1"' if hidden else ""
    return (f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="3" name="g"{h}/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            f'<p:grpSpPr><a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/>'
            f'<a:chOff x="{choff[0]}" y="{choff[1]}"/><a:chExt cx="{chext[0]}" cy="{chext[1]}"/></a:xfrm></p:grpSpPr>'
            f'{children}</p:grpSp>')


def parse(path):
    ctx = Context()
    return reader.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, _ = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden)


def test_order_by_sldIdLst():
    out = md(deck([sp("t", "title"), sp("t2", "title")], order=[2, 1]))
    assert out.index("## Slide 1: t2") < out.index("## Slide 2: t"), out


def test_titles_and_body():
    out = md(deck([sp("Head", "ctrTitle") + sp("Body", "body") + sp("Free")]))
    assert out == "## Slide 1: Head\n\nBody\n\nFree\n", repr(out)


def test_title_paragraphs_joined_with_space():
    paras = "<a:p><a:r><a:t>One</a:t></a:r></a:p><a:p><a:r><a:t>Two</a:t></a:r></a:p>"
    out = md(deck([sp(ph="title", paras=paras)]))
    assert out.startswith("## Slide 1: One Two"), out


def test_hidden_slide_keeps_numbers():
    s = [sp("a", "title"), sld(sp("b", "title"), show="0"), sp("c", "title")]
    out = md(deck(s))
    assert "## Slide 3: c" in out and "Slide 2" not in out and out.startswith("(1 hidden places left out)"), out
    assert "## Slide 2: b (hidden)" in md(deck(s), hidden=True)


def test_hidden_shape():
    out = md(deck([sp("x", "title") + sp("secret", hidden=True)]))
    assert "secret" not in out and "(1 hidden places left out)" in out, out


def test_skip_number_placeholders():
    out = md(deck([sp("t", "title") + sp("7", "sldNum") + sp("2026", "dt") + sp("f", "ftr") + sp("keep", "-")]))
    assert "7" not in out and "2026" not in out and "keep" in out and "\nf" not in out, out


def test_notes_blockquote():
    out = md(deck([sp("t", "title")], notes={1: sp("메모", "body") + sp("9", "sldNum")}))
    assert "> Notes: 메모" in out and "9" not in out, out


def test_group_geometry_order():
    a = sp("A", off=(0, 100), ext=(10, 10))
    b = sp("B", off=(0, 0), ext=(10, 10))
    g = grp(a + b, off=(0, 0), ext=(1000, 1000), choff=(0, 0), chext=(100, 100))
    out = md(deck([g + sp("C", off=(0, 5000), ext=(1, 1)) + sp("D")]))
    # A is at y=100*10=1000, B at 0 -> B A C then D (no geometry)
    assert out.index("B") < out.index("A") < out.index("C") < out.index("D"), out


def test_group_mapping_nested():
    inner = grp(sp("In", off=(0, 50), ext=(1, 1)), off=(0, 0), ext=(200, 200), choff=(0, 0), chext=(100, 100))
    outer = grp(inner, off=(0, 1000), ext=(100, 100), choff=(0, 0), chext=(100, 100))
    out = md(deck([outer + sp("Mid", off=(0, 1050), ext=(1, 1)) + sp("Low", off=(0, 1200), ext=(1, 1))]))
    assert out.index("Mid") < out.index("In") < out.index("Low"), out  # In lands at 1000 + 50*2 = 1100


def test_group_zero_and_negative_chext():
    a = sp("A", off=(0, 7), ext=(1, 1))
    b = sp("B", off=(0, 3), ext=(1, 1))
    for chext in ((0, 0), (-5, -5)):
        out = md(deck([grp(a + b, (0, 0), (100, 100), (0, 0), chext)]))
        assert out.index("B") < out.index("A"), out


def test_absurd_and_nonnumeric_offsets():
    shapes = (sp("A", off=("9" * 40, "x"), ext=(1, 1)) + sp("B", off=(0, 1), ext=(1, 1))
              + grp(sp("C", off=(1, 1), ext=(1, 1)), ("1e9", 0), (1, 1), (0, 0), (1, 1)))
    out = md(deck([shapes]))
    assert all(x in out for x in "ABC"), out


def test_deep_groups_stop():
    inner = sp("core", off=(0, 0), ext=(1, 1))
    for _ in range(200):
        inner = grp(inner, (0, 0), (10, 10), (0, 0), (10, 10))
    t = time.time()
    doc, ctx = parse(deck([sp("t", "title") + inner]))
    assert time.time() - t < 5 and ctx.skipped["depth"] >= 1
    assert doc.blocks[0].title == "t"


def test_table_spans():
    tbl = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="t"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
           '<a:graphic><a:graphicData><a:tbl><a:tblGrid><a:gridCol/><a:gridCol/></a:tblGrid>'
           '<a:tr><a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>Head</a:t></a:r></a:p></a:txBody></a:tc>'
           '<a:tc hMerge="1"><a:txBody><a:p><a:r><a:t>x</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
           '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>a</a:t></a:r></a:p></a:txBody></a:tc>'
           '<a:tc><a:txBody><a:p><a:r><a:t>b</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
           '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    out = md(deck([tbl]))
    assert '<td colspan="2">Head</td>' in out and "<td>a</td><td>b</td>" in out and ">x<" not in out, out


def test_table_huge_spans_fast():
    cells = "".join('<a:tc gridSpan="9999999" rowSpan="9999999"><a:txBody><a:p><a:r><a:t>c</a:t></a:r></a:p></a:txBody></a:tc>'
                    for _ in range(5))
    rows = f"<a:tr>{cells}</a:tr>" * 50
    tbl = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="t"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
           f'<a:graphic><a:graphicData><a:tbl>{rows}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    t = time.time()
    out = md(deck([tbl + sp("after")]))
    assert time.time() - t < 5 and "after" in out and "c" in out


def test_bullets_and_runs():
    paras = ('<a:p><a:pPr lvl="1"><a:buChar char="x"/></a:pPr><a:r><a:t>항목</a:t></a:r></a:p>'
             '<a:p><a:r><a:t>12</a:t></a:r><a:r><a:rPr b="1"/><a:t>분</a:t></a:r></a:p>'
             '<a:p><a:pPr><a:buNone/></a:pPr><a:r><a:t>plain</a:t></a:r></a:p>'
             '<a:p><a:pPr><a:buAutoNum type="arabicPeriod"/></a:pPr><a:r><a:t>n1</a:t></a:r></a:p>'
             '<a:p><a:pPr><a:buAutoNum type="arabicPeriod"/></a:pPr><a:r><a:t>n2</a:t><a:br/><a:t>q</a:t></a:r></a:p>')
    out = md(deck([sp(paras=paras, ph="body")]))
    assert "  - 항목" in out and "12**분**" in out and "\n\nplain\n\n" in out, out
    assert "1. n1\n2. n2" in out, out


def test_chart_and_image():
    chart = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea><c:pieChart>'
             '<c:ser><c:cat><c:strLit><c:ptCount val="1"/><c:pt idx="0"><c:v>A</c:v></c:pt></c:strLit></c:cat>'
             '<c:val><c:numLit><c:ptCount val="1"/><c:pt idx="0"><c:v>5</c:v></c:pt></c:numLit></c:val></c:ser>'
             '</c:pieChart></c:plotArea></c:chart></c:chartSpace>')
    gf = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="c"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
          '<a:graphic><a:graphicData><c:chart r:id="rC"/></a:graphicData></a:graphic></p:graphicFrame>')
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="5" name="p" descr="설명"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="rI"/></p:blipFill><p:spPr/></p:pic>')
    d = deck([gf + pic], srels={1: [("rC", "chart", "../charts/chart1.xml"), ("rI", "image", "../media/i.png")]},
             extra={"ppt/charts/chart1.xml": chart, "ppt/media/i.png": PNG})
    out = md(d)
    assert "Chart: pie" in out and "![설명](assets/img-001.png)" in out, out


def test_pptm_same_path():
    s = [sp("T", "title") + sp("body")]
    assert md(deck(s, kind="pptm")) == md(deck(s, kind="ppsx"))


# ---- hostile input
def test_duplicate_sldIdLst_entries():
    out = md(deck([sp("only", "title")], order=[1] * 50_000))
    assert out.count("## Slide") == 1 and "## Slide 1: only" in out


def test_damaged_slide_keeps_numbering():
    d = deck([sp("a", "title"), sp("b", "title"), sp("c", "title")])
    import zipfile
    out = tmp() / "b.ppsx"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:sld" if i.filename.endswith("slide2.xml") else zi.read(i.filename))
    doc, ctx = parse(out)
    text = markdown.render(doc, tmp() / "assets")
    assert "## Slide 1: a" in text and "## Slide 3: c" in text and "Slide 2" not in text
    assert ctx.skipped["slide"] == 1


def test_damaged_slide_is_reported():
    import zipfile
    from reader import gate
    d = deck([sp("a", "title"), sp("b", "title"), sp("c", "title")], kind="pptm")
    bad = tmp() / "b.pptm"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(bad, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:sld" if i.filename.endswith("slide2.xml") else zi.read(i.filename))
    out = tmp() / "out"
    meta = reader.convert(bad, out)
    text = (out / "out.md").read_text(encoding="utf-8")
    assert text.startswith("(could not read: 1 slide)\n\n## Slide 1: a"), text
    assert meta["unread"] == ["1 slide"], meta
    c = gate._context("b.pptm", meta, out / "out.md")
    assert c.endswith(" Some parts could not be read: 1 slide."), c


def test_all_slides_damaged():
    d = deck([sp("a", "title")])
    import zipfile
    out = tmp() / "b.ppsx"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:sld" if i.filename.endswith("slide1.xml") else zi.read(i.filename))
    try:
        parse(out)
        assert False
    except ReadError as e:
        assert e.kind == "damaged" and "no readable slide" in e.detail


def test_empty_list_is_empty_doc():
    doc, _ = parse(deck([], order=[]))
    assert doc.blocks == []


def test_missing_notes_and_image_counted():
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="5" name="p"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="nope"/></p:blipFill><p:spPr/></p:pic>')
    doc, ctx = parse(deck([sp("t", "title") + pic]))
    assert ctx.skipped["image"] == 1 and doc.blocks[0].title == "t"


def test_image_over_ratio_limit_is_skipped():
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="5" name="p"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="rI"/></p:blipFill><p:spPr/></p:pic>')
    d = deck([sp("t", "title") + sp("body") + pic], srels={1: [("rI", "image", "../media/i.png")]},
             extra={"ppt/media/i.png": PNG + bytes(3 * 2**20)})
    doc, ctx = parse(d)
    assert ctx.skipped["image"] == 1 and "body" in markdown.render(doc, tmp() / "assets")


def test_notes_missing_and_damaged():
    d = deck([sp("t", "title")], srels={1: [("rN", "notesSlide", "../notesSlides/none.xml")]})
    doc, ctx = parse(d)
    assert ctx.skipped["notes"] == 1 and doc.blocks[0].title == "t"
    d = deck([sp("t", "title") + sp("body", "-")], notes={1: sp("m", "body")})
    import zipfile
    out = tmp() / "b.ppsx"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:notes" if "notesSlide" in i.filename and "_rels" not in i.filename
                        else zi.read(i.filename))
    doc, ctx = parse(out)
    assert ctx.skipped["notes"] == 1 and "body" in markdown.render(doc, tmp() / "assets")


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
