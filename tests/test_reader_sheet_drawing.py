"""Sheet drawings read straight from a package, the code xlsb and xlsx share.

Run: python -I -X utf8 tests/test_reader_sheet_drawing.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.containers.zipx import Package  # noqa: E402
from reader.formats.sheet_drawing import Drawings  # noqa: E402
from reader.model import Context, Image, Table  # noqa: E402
from reader_fix import make_zip  # noqa: E402
from test_reader_xlsb import BAR, PNG, anchor, chart_frame, drawing_xml, pic, rels_xml  # noqa: E402

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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
