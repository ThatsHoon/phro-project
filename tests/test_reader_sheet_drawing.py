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
    assert blocks[1].caption == "Chart: bar", blocks[1].caption
    assert not ctx.skipped, dict(ctx.skipped)


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
