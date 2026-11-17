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
from reader.model import Code, Context  # noqa: E402
from reader_fix import make_zip  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402

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
    """items: (id, type, target[, True]); a fourth item True marks the target External."""
    return (f'<Relationships xmlns="{PKG}">'
            + "".join(f'<Relationship Id="{i[0]}" Type="{REL}/{i[1]}" Target="{i[2]}"'
                      + (' TargetMode="External"' if len(i) > 3 and i[3] else "") + "/>" for i in items)
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


def sp(text="", ph=None, off=None, ext=None, extra_nv="", paras=None, hidden=False, id=2, prst=None):
    phx = f'<p:ph type="{ph}"/>' if ph and ph != "-" else ("<p:ph/>" if ph == "-" else "")
    h = ' hidden="1"' if hidden else ""
    xf = ""
    if off:
        xf = f'<a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{(ext or (1, 1))[0]}" cy="{(ext or (1, 1))[1]}"/></a:xfrm>'
    geom = f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>' if prst else ""
    body = paras if paras is not None else f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{id}" name="s"{h}/><p:cNvSpPr/><p:nvPr>{phx}</p:nvPr></p:nvSpPr>'
            f'<p:spPr>{xf}{geom}</p:spPr><p:txBody><a:bodyPr/>{body}</p:txBody></p:sp>')


def cxn(id, st=None, end=None, off=(0, 0), ext=(1, 1), head=None, tail=None, flip="", hidden=False, prst="straightConnector1"):
    """A p:cxnSp glued from shape id `st` to `end` (None: loose), arrowheads `head` (start) and `tail` (end) as
    a:ln types, `flip` any of "H" and "V"."""
    g = (f'<a:stCxn id="{st}" idx="0"/>' if st is not None else "") + (f'<a:endCxn id="{end}" idx="0"/>' if end is not None else "")
    fl = "".join(f' flip{c}="1"' for c in flip)
    ln = (f'<a:headEnd type="{head}"/>' if head else "") + (f'<a:tailEnd type="{tail}"/>' if tail else "")
    h = ' hidden="1"' if hidden else ""
    return (f'<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="{id}" name="c"{h}/><p:cNvCxnSpPr>{g}</p:cNvCxnSpPr><p:nvPr/></p:nvCxnSpPr>'
            f'<p:spPr><a:xfrm{fl}><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/></a:xfrm>'
            f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>' + (f"<a:ln>{ln}</a:ln>" if ln else "") + '</p:spPr></p:cxnSp>')


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


def link_deck(paras):
    return deck([sp(paras=paras)], srels={1: [("rL", "hyperlink", "https://example.com/x", True),
                                              ("rS", "slide", "slide2.xml")]})


def run_link(text, rid="rL", action=""):
    a = f' action="{action}"' if action else ""
    return f'<a:r><a:rPr><a:hlinkClick r:id="{rid}"{a}/></a:rPr><a:t>{text}</a:t></a:r>'


def test_slide_hyperlink_external():
    out = md(link_deck(f"<a:p>{run_link('site')}</a:p>"))
    assert "[site](https://example.com/x)" in out, out


def test_slide_hyperlink_internal():
    path = link_deck(f'<a:p>{run_link("next", "rS", "ppaction://hlinksldjump")}</a:p>')
    doc, ctx = parse(path)
    out = markdown.render(doc, tmp() / "assets", False)
    assert "next" in out and "](" not in out and ctx.skipped["link"] == 0, out


def test_slide_hyperlink_missing_rel():
    doc, ctx = parse(link_deck(f'<a:p>{run_link("text", "rNone")}</a:p>'))
    out = markdown.render(doc, tmp() / "assets", False)
    assert "text" in out and "](" not in out and ctx.skipped["link"] == 1, (out, ctx.skipped)


def test_slide_link_runs_join():
    out = md(link_deck(f"<a:p>{run_link('a')}{run_link('b')}</a:p>"))
    assert "[ab](https://example.com/x)" in out, out


def test_shape_link_ignored():
    s = sp("plain").replace('name="s"/>', 'name="s"><a:hlinkClick r:id="rL"/></p:cNvPr>')
    out = md(deck([s], srels={1: [("rL", "hyperlink", "https://example.com/x", True)]}))
    assert "plain" in out and "](" not in out, out


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


def _layout(shapes, tag="sldLayout"):
    return sld(shapes).replace("<p:sld ", f"<p:{tag} ").replace("</p:sld>", f"</p:{tag}>")


def _ph(ph_attrs, text=None, off=None):
    xf = f'<p:spPr><a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="1" cy="1"/></a:xfrm></p:spPr>' if off else "<p:spPr/>"
    body = f"<p:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody>" if text else ""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr><p:ph {ph_attrs}/></p:nvPr></p:nvSpPr>'
            f"{xf}{body}</p:sp>")


def test_placeholder_position_from_layout_and_master():
    slide = (_ph('type="title"', "Title") + _ph('idx="1"', "body text") + _ph('type="body" idx="7"', "from master")
             + sp("bottom text", off=(0, 6000000), ext=(1, 1)))
    layout = _layout(_ph('type="title"', off=(0, 300000)) + _ph('idx="1"', off=(0, 1600000)))
    master = _layout(_ph('type="body"', off=(0, 2000000)), "sldMaster")
    d = deck([slide], kind="pptm", srels={1: [("rL", "slideLayout", "../slideLayouts/slideLayout1.xml")]},
             extra={"ppt/slideLayouts/slideLayout1.xml": layout, "ppt/slideMasters/slideMaster1.xml": master,
                    "ppt/slideLayouts/_rels/slideLayout1.xml.rels":
                        rels_xml([("rM", "slideMaster", "../slideMasters/slideMaster1.xml")])})
    out = md(d)
    assert out == "## Slide 1: Title\n\nbody text\n\nfrom master\n\nbottom text\n", repr(out)


def test_unplaced_shape_keeps_its_spTree_place():
    out = md(deck([sp("first") + sp("later", off=(0, 500), ext=(1, 1)) + sp("top", off=(0, 100), ext=(1, 1))
                   + sp("last")]))
    assert out == "## Slide 1\n\nfirst\n\ntop\n\nlater\n\nlast\n", repr(out)


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


# ---- connectors
def mermaid(doc):
    return "\n".join(b.text for sec in doc.blocks for b in sec.blocks if isinstance(b, Code) and b.lang == "mermaid")


def test_slide_connector_mermaid():
    s = (sp("T", "title", id=1) + sp("Start", id=2, off=(0, 0), ext=(100, 100))
         + sp("End", id=3, off=(0, 1000), ext=(100, 100)) + cxn(4, 2, 3, off=(50, 100), ext=(0, 900), tail="triangle"))
    doc, ctx = parse(deck([s]))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "assets")
    assert out.startswith("## Slide 1: T\n\n```mermaid\n") and out.count("Start") == 1, out
    assert out.count("End") == 1 and ctx.skipped["connector_loose"] == 0, out


def test_slide_title_stays_title():
    s = (sp("T", "title", id=2, off=(0, 0), ext=(1000, 100)) + sp("Body", id=3, off=(0, 500), ext=(1000, 100))
         + cxn(4, 2, 3, tail="triangle"))
    doc, _ = parse(deck([s]))
    t = mermaid(doc)
    assert mermaid_ok(t) and '  n1["Body"]' in t and '  n2["T"]' in t and "  n2 --> n1" in t, t
    out = markdown.render(doc, tmp() / "assets")
    assert out.startswith("## Slide 1: T\n\n```mermaid\n") and out.count("Body") == 1, out
    assert doc.blocks[0].title == "T"


def test_group_scaled_connector_snaps():
    for c in (cxn(9, off=(5, 10), ext=(0, 40), tail="triangle"), cxn(9, off=(5, 10), ext=(0, 40), head="arrow", flip="V")):
        inner = (sp("A", id=2, off=(0, 0), ext=(10, 10)) + sp("B", id=3, off=(0, 50), ext=(10, 10)) + c)
        g = grp(inner, off=(1000, 1000), ext=(1000, 1000), choff=(0, 0), chext=(100, 100))
        doc, ctx = parse(deck([sp("Out", id=5, off=(0, 0), ext=(100, 100)) + g]))
        t = mermaid(doc)
        assert mermaid_ok(t) and t == ('flowchart TD\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n'
                                       '  n1 --> n2'), t
        out = markdown.render(doc, tmp() / "assets")
        assert out.startswith("## Slide 1\n\nOut\n\n```mermaid") and ctx.skipped["connector_loose"] == 0, out


def test_line_shape_and_textless_target():
    line = sp("", id=4, off=(50, 100), ext=(0, 900), prst="line", paras="").replace(
        "</p:spPr>", '<a:ln><a:tailEnd type="triangle"/></a:ln></p:spPr>')
    s = sp("", id=2, off=(0, 0), ext=(100, 100), paras="") + sp("Goal", id=3, off=(0, 1000), ext=(100, 100)) + line
    doc, _ = parse(deck([s]))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == ('flowchart TD\n  n1["Goal"]\n  n2[" "]\n  %% inferred from position\n'
                                   '  n2 --> n1'), t
    rule = sp("rule", id=4, off=(50, 100), ext=(0, 900), prst="line")  # no arrowhead: a divider, never an edge
    out = md(deck([sp("", id=2, off=(0, 0), ext=(100, 100), paras="") + sp("Goal", id=3, off=(0, 1000), ext=(100, 100))
                   + rule]))
    assert "mermaid" not in out and "rule" in out, out


def test_hidden_connector_and_duplicate_ids():
    base = sp("A", id=2, off=(0, 0), ext=(100, 100)) + sp("B", id=3, off=(0, 1000), ext=(100, 100))
    out = md(deck([base + cxn(4, 2, 3, tail="triangle", hidden=True)]))
    assert "mermaid" not in out and "A" in out and "B" in out, out
    # a second shape reusing id 2 keeps its text and cannot take the glue
    dup = sp("A2", id=2, off=(0, 2000), ext=(100, 100))
    doc, _ = parse(deck([base + dup + cxn(4, 2, 3, tail="triangle")]))
    t = mermaid(doc)
    assert mermaid_ok(t) and '"A"' in t and "A2" not in t, t
    assert "A2" in markdown.render(doc, tmp() / "assets")


def test_place_failure_keeps_the_flow():
    from reader import diagram

    def boom(self, slots):
        raise ValueError("bad")
    real = diagram.Diagram.place
    diagram.Diagram.place = boom
    try:
        doc, ctx = parse(deck([sp("A", id=2, off=(0, 0), ext=(100, 100)) + sp("B", id=3, off=(0, 1000), ext=(100, 100))
                               + cxn(4, 2, 3, tail="triangle")]))
    finally:
        diagram.Diagram.place = real
    out = markdown.render(doc, tmp() / "assets")
    assert out.endswith("## Slide 1\n\nA\n\nB\n") and ctx.skipped["shape"] == 1, (out, dict(ctx.skipped))


def _plain_decks():
    """(deck, hidden) pairs with no connector: the fixtures above, plus a textless shape and an arrowless line."""
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="5" name="p" descr="d"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="rI"/></p:blipFill><p:spPr><a:xfrm><a:off x="0" y="300"/>'
           '<a:ext cx="10" cy="10"/></a:xfrm><a:prstGeom prst="ellipse"/></p:spPr></p:pic>')
    a, b = sp("A", off=(0, 100), ext=(10, 10)), sp("B", off=(0, 0), ext=(10, 10))
    inner = grp(sp("In", off=(0, 50), ext=(1, 1)), off=(0, 0), ext=(200, 200), choff=(0, 0), chext=(100, 100))
    return [
        (deck([sp("Head", "ctrTitle") + sp("Body", "body") + sp("Free")]), False),
        (deck([grp(a + b, off=(0, 0), ext=(1000, 1000), choff=(0, 0), chext=(100, 100))
               + sp("C", off=(0, 5000), ext=(1, 1)) + sp("D")]), False),
        (deck([grp(inner, off=(0, 1000), ext=(100, 100), choff=(0, 0), chext=(100, 100))
               + sp("Mid", off=(0, 1050), ext=(1, 1)) + sp("Low", off=(0, 1200), ext=(1, 1))]), False),
        (deck([sp("first") + sp("later", off=(0, 500), ext=(1, 1)) + sp("top", off=(0, 100), ext=(1, 1))
               + sp("last")]), False),
        (deck([sp("x", "title") + sp("secret", hidden=True)]), False),
        (deck([sp("x", "title") + sp("secret", hidden=True)]), True),
        (deck([sp("t", "title") + sp("7", "sldNum") + sp("2026", "dt") + sp("f", "ftr") + sp("keep", "-")]), False),
        (deck([sp("T", "title", id=1) + sp("", id=2, off=(0, 0), ext=(100, 100), paras="", prst="diamond")
               + sp("rule", id=3, off=(0, 200), ext=(500, 0), prst="line") + pic
               + sp("after", id=6, off=(0, 400), ext=(10, 10), prst="flowChartDecision")],
              srels={1: [("rI", "image", "../media/i.png")]}, extra={"ppt/media/i.png": PNG}), False),
    ]


_PLAIN = [  # what the reader wrote for these decks before it read connectors
    '## Slide 1: Head\n\nBody\n\nFree\n',
    '## Slide 1\n\nB\n\nA\n\nC\n\nD\n',
    '## Slide 1\n\nMid\n\nIn\n\nLow\n',
    '## Slide 1\n\nfirst\n\ntop\n\nlater\n\nlast\n',
    '(1 hidden places left out)\n\n## Slide 1: x\n',
    '## Slide 1: x\n\n(hidden) secret\n',
    '## Slide 1: t\n\nkeep\n',
    '## Slide 1: T\n\nrule\n\n![d](assets/img-001.png)\n\nafter\n',
]


def test_slide_without_connector_unchanged():
    got = [md(d, h) for d, h in _plain_decks()]
    assert got == _PLAIN, got

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


P14 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
PM = "http://schemas.openxmlformats.org/presentationml/2006/main"
PRELS = "ppt/_rels/presentation.xml.rels"


def _pres_rels(n, extra=()):
    return rels_xml([(f"rS{i}", "slide", f"slides/slide{i}.xml") for i in range(1, n + 1)] + list(extra))


def _legacy(*cms):
    body = "".join(f'<p:cm authorId="{a}" dt="2020-01-01T00:00:00" idx="{i}"><p:pos x="0" y="0"/><p:text>{t}</p:text></p:cm>'
                   for i, (a, t) in enumerate(cms, 1))
    return f'<p:cmLst xmlns:p="{PM}">{body}</p:cmLst>'


def _authors(name):
    return f'<p:cmAuthorLst xmlns:p="{PM}"><p:cmAuthor id="0" name="{name}" initials="x" lastIdx="2" clrIdx="0"/></p:cmAuthorLst>'


def _legacy_deck(cms, notes=None, show=None, authors=True):
    members = {"ppt/comments/comment1.xml": _legacy(*cms)}
    extra = [("rA", "commentAuthors", "commentAuthors.xml")] if authors else []
    members[PRELS] = _pres_rels(1, extra)
    if authors:
        members["ppt/commentAuthors.xml"] = _authors("Ann")
    slide = sld(sp("t", "title"), show) if show is not None else sp("t", "title")
    return deck([slide], notes=notes, srels={1: [("rC", "comments", "../comments/comment1.xml")]}, extra=members)


def test_legacy_comments():
    out = md(_legacy_deck([(0, "first"), (0, "second")], notes={1: sp("n", "body")}))
    assert "> Notes: n\n> Comment by Ann: first\n> Comment by Ann: second" in out, out


def test_modern_comments_and_replies():
    def tx(t):
        return f"<p188:txBody><a:bodyPr/><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p188:txBody>"
    cm = (f'<p188:cmLst xmlns:p188="{P14}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
          f'<p188:cm id="{{C1}}" authorId="{{G1}}">{tx("q")}<p188:replyLst>'
          f'<p188:reply id="{{R1}}" authorId="{{G2}}">{tx("a")}</p188:reply></p188:replyLst></p188:cm></p188:cmLst>')
    au = (f'<p188:authorLst xmlns:p188="{P14}"><p188:author id="{{G1}}" name="Bo" initials="B"/>'
          f'<p188:author id="{{G2}}" name="Cy" initials="C"/></p188:authorLst>')
    members = {"ppt/comments/modernComment_1.xml": cm, "ppt/authors.xml": au,
               PRELS: _pres_rels(1, [("rA", "authors", "authors.xml")])}
    d = deck([sp("t", "title")], srels={1: [("rC", "comments", "../comments/modernComment_1.xml")]}, extra=members)
    out = md(d)
    assert "> Comment by Bo: q\n> Comment by Cy: a" in out, out


def test_comments_without_notes():
    out = md(_legacy_deck([(0, "only")]))
    assert "> Comment by Ann: only" in out and "> Notes:" not in out, out


def test_comments_on_hidden_slide():
    d = _legacy_deck([(0, "c")], show="0")
    assert "Comment by" not in md(d)
    assert "Comment by Ann: c" in md(d, hidden=True)
    assert "(1 hidden places left out)" in md(d), md(d)


def test_comments_part_missing():
    d = deck([sp("t", "title")], srels={1: [("rC", "comments", "../comments/none.xml")]})
    doc, ctx = parse(d)
    assert ctx.skipped["comments"] == 1 and doc.blocks[0].title == "t"
    d = _legacy_deck([(0, "c")])
    import zipfile
    out = tmp() / "c.ppsx"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:cmLst" if i.filename == "ppt/comments/comment1.xml" else zi.read(i.filename))
    doc, ctx = parse(out)
    assert ctx.skipped["comments"] == 1 and doc.blocks[0].title == "t"


def test_comments_unknown_author_reads_plain():
    out = md(_legacy_deck([(0, "q")], authors=False))
    assert "> Comment: q" in out and "Comment by" not in out, out
    d = _legacy_deck([(0, "q")])
    import zipfile
    bad = tmp() / "d.ppsx"
    with zipfile.ZipFile(d) as zi, zipfile.ZipFile(bad, "w") as zo:
        for i in zi.infolist():
            zo.writestr(i.filename, b"<p:cmAuthorLst" if i.filename == "ppt/commentAuthors.xml" else zi.read(i.filename))
    doc, ctx = parse(bad)
    assert ctx.skipped["comments"] == 1 and "> Comment: q" in markdown.render(doc, tmp() / "assets")


def test_empty_comment_skipped():
    out = md(_legacy_deck([(0, "  "), (0, "kept")]))
    assert out.count("Comment by") == 1 and "> Comment by Ann: kept" in out, out


def test_comments_cut_counts_once():
    from reader.formats import ooxml_slides
    old = ooxml_slides.MAX_COMMENTS
    ooxml_slides.MAX_COMMENTS = 2
    try:
        doc, ctx = parse(_legacy_deck([(0, "a"), (0, "b"), (0, "c"), (0, "d")]))
    finally:
        ooxml_slides.MAX_COMMENTS = old
    out = markdown.render(doc, tmp() / "assets")
    assert "Comment by Ann: b" in out and "Comment by Ann: c" not in out, out
    assert ctx.skipped["comments_cut"] == 1 and ctx.skipped["comments"] == 0, dict(ctx.skipped)


def _refusing(names, kind="too_large"):
    """Make ooxml_base.load refuse the parts whose name contains one of `names` the way a size limit does."""
    from reader.formats import ooxml_base
    real = ooxml_base.load

    def load(pkg, part):
        if any(n in part for n in names):
            raise ReadError(kind, "part: " + part)
        return real(pkg, part)
    ooxml_base.load = load
    return lambda: setattr(ooxml_base, "load", real)


def test_too_large_comments_and_authors_skip_the_part():
    for names in (("comments/comment1",), ("commentAuthors",)):
        undo = _refusing(names)
        try:
            doc, ctx = parse(_legacy_deck([(0, "c")]))
        finally:
            undo()
        assert ctx.skipped["comments"] == 1 and doc.blocks[0].title == "t", dict(ctx.skipped)


def test_too_large_notes_skips_the_part():
    undo = _refusing(("notesSlide",))
    try:
        doc, ctx = parse(deck([sp("t", "title")], notes={1: sp("m", "body")}))
    finally:
        undo()
    assert ctx.skipped["notes"] == 1 and doc.blocks[0].title == "t", dict(ctx.skipped)

def test_timeout_on_comments_authors_notes_aborts():
    for names, d in ((("comments/comment1",), _legacy_deck([(0, "c")])), (("commentAuthors",), _legacy_deck([(0, "c")])),
                     (("notesSlide",), deck([sp("t", "title")], notes={1: sp("m", "body")}))):
        undo = _refusing(names, "timeout")
        try:
            parse(d)
        except ReadError as e:
            assert e.kind == "timeout", e.kind
        else:
            raise AssertionError(f"timeout on {names} was swallowed")
        finally:
            undo()


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
