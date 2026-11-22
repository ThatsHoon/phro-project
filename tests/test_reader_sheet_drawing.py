"""Sheet drawings read straight from a package, the code xlsb and xlsx share.

Run: python -I -X utf8 tests/test_reader_sheet_drawing.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import ooxml, sheet_drawing  # noqa: E402
from reader.formats.sheet_drawing import Drawings  # noqa: E402
from reader.model import Context, Image, ListItem, Table  # noqa: E402
from reader_fix import make_xlsx, make_zip, xlsx_cell, xlsx_row  # noqa: E402
from test_reader_xlsb import BAR, PNG, anchor, chart_frame, drawing_xml, md, pic, rels_xml, with_drawing  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402

SHEET = "xl/worksheets/sheet1.xml"


def package():
    members = {
        "xl/worksheets/_rels/sheet1.xml.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
        "xl/drawings/drawing1.xml": drawing_xml(anchor(2, chart_frame("rId1")), anchor(0, pic("rId2"))),
        "xl/drawings/_rels/drawing1.xml.rels": rels_xml(("rId1", "chart", "../charts/chart1.xml"),
                                                        ("rId2", "image", "../media/image1.png")),
        "xl/charts/chart1.xml": BAR,
        "xl/media/image1.png": PNG,
    }
    return Package(make_zip(None, members))


def test_drawings_direct():
    ctx = Context()
    blocks = Drawings(package(), ctx).blocks(SHEET)
    assert [type(b) for b in blocks] == [Image, Table], blocks  # row order: the picture at row 0 first
    assert blocks[1].caption == "Chart: column", blocks[1].caption
    assert not ctx.skipped, dict(ctx.skipped)


def test_chart_names_its_range_and_keeps_its_cells():
    ranged = BAR.replace("<c:strRef>", "<c:strRef><c:f>Sheet1!$B$1</c:f>", 1).replace(
        "<c:cat><c:strRef>", "<c:cat><c:strRef><c:f>Sheet1!$A$2</c:f>").replace(
        "<c:numRef>", "<c:numRef><c:f>Sheet1!$B$2</c:f>")
    members = {
        "xl/worksheets/_rels/sheet1.xml.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
        "xl/drawings/drawing1.xml": drawing_xml(anchor(2, chart_frame("rId1"))),
        "xl/drawings/_rels/drawing1.xml.rels": rels_xml(("rId1", "chart", "../charts/chart1.xml")),
        "xl/charts/chart1.xml": ranged,
    }
    (t,) = Drawings(Package(make_zip(None, members)), Context()).blocks(SHEET)
    assert (t.caption, len(t.rows)) == ("Chart: column. Data: Sheet1!A1:B2", 2), (t.caption, t.rows)


def test_drawing_seen_once():
    ctx = Context()
    d = Drawings(package(), ctx)
    assert len(d.blocks(SHEET)) == 2
    assert d.blocks(SHEET) == [] and ctx.skipped["object"] == 1, dict(ctx.skipped)


DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


def smartart_frame(rid):
    return (f'<xdr:graphicFrame><a:graphic><a:graphicData><dgm:relIds xmlns:dgm="{DGM}" r:dm="{rid}"/>'
            '</a:graphicData></a:graphic></xdr:graphicFrame>')


def smartart_package(data=True):
    members = {
        "xl/worksheets/_rels/sheet1.xml.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
        "xl/drawings/drawing1.xml": drawing_xml(anchor(3, smartart_frame("rId3")), anchor(0, pic("rId2"))),
        "xl/drawings/_rels/drawing1.xml.rels": rels_xml(("rId2", "image", "../media/image1.png"),
                                                        ("rId3", "diagramData", "../diagrams/data1.xml")),
        "xl/media/image1.png": PNG,
    }
    if data:
        members["xl/diagrams/data1.xml"] = (
            f'<dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<dgm:ptLst><dgm:pt modelId="0" type="doc"/>'
            '<dgm:pt modelId="A"><dgm:t><a:bodyPr/><a:p><a:r><a:t>top</a:t></a:r></a:p></dgm:t></dgm:pt>'
            '<dgm:pt modelId="B"><dgm:t><a:bodyPr/><a:p><a:r><a:t>under</a:t></a:r></a:p></dgm:t></dgm:pt>'
            '</dgm:ptLst><dgm:cxnLst><dgm:cxn modelId="c0" srcId="0" destId="A" srcOrd="0"/>'
            '<dgm:cxn modelId="c1" srcId="A" destId="B" srcOrd="0"/></dgm:cxnLst></dgm:dataModel>')
    return Package(make_zip(None, members))


def test_smartart_on_sheet():
    ctx = Context()
    blocks = Drawings(smartart_package(), ctx).blocks(SHEET)
    assert [type(b) for b in blocks] == [Image, ListItem, ListItem], blocks  # anchor order: the picture at row 0
    assert [(b.level, b.inlines[0].text) for b in blocks[1:]] == [(0, "top"), (1, "under")], blocks
    assert not ctx.skipped, dict(ctx.skipped)


def test_smartart_missing_data_counted():
    ctx = Context()
    blocks = Drawings(smartart_package(data=False), ctx).blocks(SHEET)
    assert [type(b) for b in blocks] == [Image], blocks
    assert ctx.skipped["smartart"] == 1, dict(ctx.skipped)


def test_smartart_cap_counted():
    old, limits.SMARTART_NODES = limits.SMARTART_NODES, 1
    try:
        ctx = Context()
        blocks = Drawings(smartart_package(), ctx).blocks(SHEET)
    finally:
        limits.SMARTART_NODES = old
    assert sum(isinstance(b, ListItem) for b in blocks) == 1, blocks
    assert ctx.skipped["smartart_cut"] == 1, dict(ctx.skipped)


def shape(text, hidden=False, n=2):
    """A text box: one paragraph per `|`-separated piece of `text`; `a~b` is a run `a` then a field `b`."""
    hid = ' hidden="1"' if hidden else ""
    paras = ""
    for t in text.split("|"):
        run, _, fld = t.partition("~")
        paras += f'<a:p><a:r><a:t>{run}</a:t></a:r>' + (f'<a:fld id="x"><a:t>{fld}</a:t></a:fld>' if fld else "") + "</a:p>"
    return (f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{n}" name="TextBox {n}"{hid}/><xdr:cNvSpPr txBox="1"/></xdr:nvSpPr>'
            f'<xdr:spPr/><xdr:txBody><a:bodyPr/>{paras}<a:p/></xdr:txBody></xdr:sp>')


SHAPES = (anchor(5, shape("LATER-BOX")), anchor(0, shape("HOW TO FILL|step one ~and two")),
          anchor(3, f'<xdr:grpSp><xdr:nvGrpSpPr/>{shape("SECRET-BOX", hidden=True, n=4)}</xdr:grpSp>'))


def check_shapes(out, shown, ctx):
    assert "HOW TO FILL" in out and "step one and two" in out and "LATER-BOX" in out, out
    assert out.index("cell") < out.index("HOW TO FILL") < out.index("step one") < out.index("LATER-BOX"), out
    assert ("SECRET-BOX" in out) == shown, out
    if shown:
        assert "(hidden) SECRET-BOX" in out, out
        assert out.index("step one") < out.index("SECRET-BOX") < out.index("LATER-BOX"), out  # anchor order
    else:
        assert "(1 hidden places left out)" in out, out
    assert not ctx.skipped, dict(ctx.skipped)


def test_shape_text_xlsx():
    parts = {"xl/drawings/drawing1.xml": drawing_xml(*SHAPES), "xl/drawings/_rels/drawing1.xml.rels": rels_xml()}
    data = make_xlsx(None, [{"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", "cell", t="str")),
                             "rels": [("rD", "drawing", "../drawings/drawing1.xml", False)]}], extra_parts=parts)
    for shown in (False, True):
        ctx = Context()
        doc = ooxml.parse_package(data, ctx)
        with tempfile.TemporaryDirectory() as d:
            out = markdown.render(doc, Path(d) / "assets", shown, ctx.skipped)
        check_shapes(out, shown, ctx)


def test_shape_text_xlsb():
    for shown in (False, True):
        out, ctx = md([{"name": "S", "cells": {(0, 0): "cell"}}], hidden=shown, extra_parts=with_drawing(*SHAPES))
        check_shapes(out, shown, ctx)


def test_shape_text_cap_counted_once():
    members = {"xl/worksheets/_rels/sheet1.xml.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
               "xl/drawings/drawing1.xml": drawing_xml(*SHAPES), "xl/drawings/_rels/drawing1.xml.rels": rels_xml()}
    old, sheet_drawing.SHAPE_CHARS = getattr(sheet_drawing, "SHAPE_CHARS", None), 12
    try:
        ctx = Context()
        blocks = Drawings(Package(make_zip(None, members)), ctx).blocks(SHEET)
    finally:
        sheet_drawing.SHAPE_CHARS = old
    assert [b.inlines[0].text for b in blocks] == ["HOW TO FILL"], blocks  # 11 chars fit, the rest does not
    assert ctx.skipped["shape"] == 1, dict(ctx.skipped)  # past the cap, counted once


# ---- connectors
def sp(text, n, off=None, ext=(1, 1), prst=None, ln=""):
    """A shape with cNvPr id `n`, its a:xfrm at `off` (none when None), preset `prst` and a:ln children `ln`."""
    xf = f'<a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/></a:xfrm>' if off else ""
    geom = f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>' if prst else ""
    body = f"<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></xdr:txBody>" if text else ""
    return (f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{n}" name="s{n}"/><xdr:cNvSpPr/></xdr:nvSpPr>'
            f'<xdr:spPr>{xf}{geom}' + (f"<a:ln>{ln}</a:ln>" if ln else "") + f'</xdr:spPr>{body}</xdr:sp>')


def cxn(n, st=None, end=None, off=(0, 0), ext=(1, 1), tail="triangle"):
    """An xdr:cxnSp glued from shape id `st` to `end` (None: loose), with a tailEnd of type `tail`."""
    g = ((f'<a:stCxn id="{st}" idx="0"/>' if st is not None else "")
         + (f'<a:endCxn id="{end}" idx="0"/>' if end is not None else ""))
    ln = f'<a:ln><a:tailEnd type="{tail}"/></a:ln>' if tail else ""
    return (f'<xdr:cxnSp macro=""><xdr:nvCxnSpPr><xdr:cNvPr id="{n}" name="c{n}"/><xdr:cNvCxnSpPr>{g}</xdr:cNvCxnSpPr>'
            f'</xdr:nvCxnSpPr><xdr:spPr><a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/>'
            f'</a:xfrm><a:prstGeom prst="straightConnector1"><a:avLst/></a:prstGeom>{ln}</xdr:spPr></xdr:cxnSp>')


def grp(children, off, ext, choff, chext):
    return (f'<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="90" name="g"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr><xdr:grpSpPr>'
            f'<a:xfrm><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/>'
            f'<a:chOff x="{choff[0]}" y="{choff[1]}"/><a:chExt cx="{chext[0]}" cy="{chext[1]}"/></a:xfrm>'
            f'</xdr:grpSpPr>{children}</xdr:grpSp>')


GLUED = (anchor(0, sp("Start", 2, off=(0, 0), ext=(1_000_000, 500_000))),
         anchor(4, sp("End", 3, off=(0, 2_000_000), ext=(1_000_000, 500_000))),
         anchor(2, cxn(4, 2, 3, off=(500_000, 500_000), ext=(0, 1_500_000))))
GLUED_MERMAID = 'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 --> n2'


def mermaid_of(out):
    """The text of the only mermaid block in rendered Markdown."""
    assert out.count("```mermaid\n") == 1, out
    return out.split("```mermaid\n", 1)[1].split("\n```", 1)[0]


def xlsx_md(anchors, shown=False):
    parts = {"xl/drawings/drawing1.xml": drawing_xml(*anchors), "xl/drawings/_rels/drawing1.xml.rels": rels_xml()}
    data = make_xlsx(None, [{"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", "cell", t="str")),
                             "rels": [("rD", "drawing", "../drawings/drawing1.xml", False)]}], extra_parts=parts)
    ctx = Context()
    doc = ooxml.parse_package(data, ctx)
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", shown, ctx.skipped), ctx


def xlsb_md(anchors, shown=False):
    return md([{"name": "S", "cells": {(0, 0): "cell"}}], hidden=shown, extra_parts=with_drawing(*anchors))


def check_glued(out, ctx):
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == GLUED_MERMAID, t
    assert out.index("cell") < out.index("```mermaid"), out  # after the sheet table
    assert out.count("Start") == 1 and out.count("End") == 1, out
    assert not ctx.skipped, dict(ctx.skipped)


def test_sheet_connector_xlsx():
    check_glued(*xlsx_md(GLUED))


def test_sheet_connector_xlsb():
    check_glued(*xlsb_md(GLUED))


def test_sheet_unglued_line_is_a_rule():
    # a cxnSp of preset `line` with no glue and no arrowhead, its ends on both boxes: a rule, like a plain line
    rule = cxn(4, off=(500_000, 500_000), ext=(0, 1_500_000), tail=None).replace("straightConnector1", "line")
    out, ctx = xlsx_md(GLUED[:2] + (anchor(2, rule),))
    assert "```mermaid" not in out and out.count("Start") == out.count("End") == 1, out
    assert not ctx.skipped, dict(ctx.skipped)


def test_sheet_group_transform():
    # scale 2 from (1e6, 1e6): A lands on (1e6..1.2e6, 1e6..1.2e6), B on (1e6..1.2e6, 2e6..2.2e6), the connector's
    # ends on their edges. Read untransformed, its end would land on the smaller decoy D instead of B.
    inner = (sp("A", 2, off=(0, 0), ext=(100_000, 100_000)) + sp("B", 3, off=(0, 500_000), ext=(100_000, 100_000))
             + cxn(4, off=(50_000, 100_000), ext=(0, 400_000)))
    g = grp(inner, off=(1_000_000, 1_000_000), ext=(2_000_000, 2_000_000), choff=(0, 0), chext=(1_000_000, 1_000_000))
    decoy = sp("Decoy", 5, off=(20_000, 470_000), ext=(60_000, 60_000))
    for read in (xlsx_md, xlsb_md):
        out, ctx = read((anchor(0, g), anchor(9, decoy)))
        t = mermaid_of(out)
        assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 --> n2', t
        assert out.index("```mermaid") < out.index("D\n"), out
        assert not ctx.skipped, dict(ctx.skipped)


def _record_boxes(anchors):
    """{key: box} that the drawing hands to Diagram.shape."""
    from reader import diagram
    got, real = {}, diagram.Diagram.shape

    def spy(self, key, blocks, kind="", box=None, *a, **kw):
        got[key] = box
        return real(self, key, blocks, kind, box, *a, **kw)
    members = {"xl/worksheets/_rels/sheet1.xml.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
               "xl/drawings/drawing1.xml": drawing_xml(*anchors), "xl/drawings/_rels/drawing1.xml.rels": rels_xml()}
    diagram.Diagram.shape = spy
    try:
        Drawings(Package(make_zip(None, members)), Context()).blocks(SHEET)
    finally:
        diagram.Diagram.shape = real
    return got


def test_sheet_anchor_boxes():
    pt = 12_700  # EMU a point: Diagram gets points
    absolute = ('<xdr:absoluteAnchor><xdr:pos x="127000" y="254000"/><xdr:ext cx="12700" cy="25400"/>'
                f'{sp("abs", 5)}<xdr:clientData/></xdr:absoluteAnchor>')
    one = ('<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>7</xdr:row>'
           '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:pos x="0" y="0"/><xdr:ext cx="127000" cy="127000"/>'
           f'{sp("one", 6)}<xdr:clientData/></xdr:oneCellAnchor>')
    nested = grp(grp(sp("in", 8, off=(10 * pt, 0), ext=(pt, pt)), off=(0, 0), ext=(200, 200), choff=(0, 0),
                     chext=(100, 100)), off=(100 * pt, 0), ext=(300, 300), choff=(0, 0), chext=(100, 100))
    turned = sp("rot", 4, off=(0, 0), ext=(2 * pt, 2 * pt)).replace("<a:xfrm>", '<a:xfrm rot="5400000">')
    got = _record_boxes((anchor(0, sp("two", 2)), anchor(1, sp("boxed", 3, off=(pt, 2 * pt), ext=(3 * pt, 4 * pt))),
                         anchor(2, turned), absolute, one, anchor(3, nested)))
    assert got["2"] is None, got  # a twoCellAnchor shape without xfrm has no box
    assert got["3"] == (1, 2, 4, 6), got
    assert got["4"] == (0, 0, 2, 2), got
    assert got["5"] == (10, 20, 11, 22), got  # pos/ext of an absoluteAnchor
    assert got["6"] == (0, 0, 10, 10), got
    assert got["8"] == (160, 0, 166, 6), got  # scale 2 inside scale 3 from x = 100pt: 100 + 10 * 6, 6pt wide


def test_sheet_place_failure_keeps_flow():
    from reader import diagram
    real = diagram.Diagram.place, diagram.Diagram.connector

    def boom(self, *a, **kw):
        raise ValueError("bad")
    for which in (0, 1):
        setattr(diagram.Diagram, ("place", "connector")[which], boom)
        try:
            out, ctx = xlsx_md(GLUED)
        finally:
            diagram.Diagram.place, diagram.Diagram.connector = real
        assert "mermaid" not in out and out.index("cell") < out.index("Start") < out.index("End"), out
        assert ctx.skipped["shape"] == 1, dict(ctx.skipped)


def _plain_drawings():
    """Drawings with no connector: SHAPES, and SHAPES with boxed shapes and an arrowless line among them."""
    extra = (anchor(7, sp("Boxed", 7, off=(0, 0), ext=(100, 100), prst="diamond")),
             anchor(8, sp("rule", 8, off=(50, 100), ext=(0, 900), prst="line")),
             anchor(9, sp("Goal", 9, off=(0, 1000), ext=(100, 100))))
    return [SHAPES, SHAPES + extra]


_HID = "(1 hidden places left out)\n\n"
_TOP = "## Sheet: S\n\nA1: cell\n\nHOW TO FILL\n\nstep one and two\n\n"
_MORE = "\n\nBoxed\n\nrule\n\nGoal\n"
_PLAIN = [  # what the reader wrote for these drawings before it read connectors: xlsx, xlsb; hidden left out, shown
    _HID + _TOP + "LATER-BOX\n", _TOP + "(hidden) SECRET-BOX\n\nLATER-BOX\n",
    _HID + _TOP + "LATER-BOX\n", _TOP + "(hidden) SECRET-BOX\n\nLATER-BOX\n",
    _HID + _TOP + "LATER-BOX" + _MORE, _TOP + "(hidden) SECRET-BOX\n\nLATER-BOX" + _MORE,
    _HID + _TOP + "LATER-BOX" + _MORE, _TOP + "(hidden) SECRET-BOX\n\nLATER-BOX" + _MORE,
]


def test_sheet_without_connector_unchanged():
    got = [read(d, shown)[0] for d in _plain_drawings() for read in (xlsx_md, xlsb_md) for shown in (False, True)]
    assert got == _PLAIN, got


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
