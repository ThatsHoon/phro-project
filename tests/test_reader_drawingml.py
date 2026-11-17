"""DrawingML geometry and glue helpers: boxes through group transforms, rotation, connector ends after flips,
arrowheads, preset kinds and lines, and connector glue in PresentationML and Word.

Run: python -I -X utf8 tests/test_reader_drawingml.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader.containers.zipx import parse_xml  # noqa: E402
from reader.formats.drawingml import Xf, arrows, glue, is_line, kind, xfrm  # noqa: E402

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = (f'xmlns:a="{A}" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')


def el(xml):
    return parse_xml(xml.encode())


def xf(off, ext, extra=""):
    return el(f'<a:xfrm {NS}{extra}><a:off x="{off[0]}" y="{off[1]}"/><a:ext cx="{ext[0]}" cy="{ext[1]}"/></a:xfrm>')


def sppr(inner):
    return el(f"<p:spPr {NS}>{inner}</p:spPr>")


def close(a, b):
    return all(abs(x - y) < 1e-6 for x, y in zip(a, b)) and len(a) == len(b)


def test_box_identity_and_group():
    x = xf((10, 20), (30, 40))
    assert Xf().box(x) == (10, 20, 40, 60), Xf().box(x)
    grp = el(f'<p:grpSp {NS}><p:grpSpPr><a:xfrm><a:off x="100" y="100"/><a:ext cx="200" cy="200"/>'
             '<a:chOff x="0" y="0"/><a:chExt cx="100" cy="100"/></a:xfrm></p:grpSpPr></p:grpSp>')
    assert xfrm(grp) is not None
    g = Xf().group(xfrm(grp))
    assert close(g.box(x), (120, 140, 180, 220)), g.box(x)
    assert Xf().box(None) is None and Xf().box(el(f'<a:xfrm {NS}><a:off x="1" y="1"/></a:xfrm>')) is None


def test_box_rotated_90():
    b = Xf().box(xf((0, 0), (100, 20), ' rot="5400000"'))
    assert close(b, (40, -40, 60, 60)), b  # centre (50, 10), now 20 wide and 100 high
    assert close(Xf().box(xf((0, 0), (100, 20), ' rot="10800000"')), (0, 0, 100, 20))
    b = Xf().box(xf((0, 0), (100, 20), ' rot="2700000"'))  # 45 degrees: (100 + 20) / sqrt(2) each way
    s = 120 / 2 ** 0.5
    assert close(b, (50 - s / 2, 10 - s / 2, 50 + s / 2, 10 + s / 2)), b


def test_ends_flips():
    assert Xf().ends(xf((0, 0), (10, 5))) == ((0, 0), (10, 5))
    assert Xf().ends(xf((0, 0), (10, 5), ' flipH="1"')) == ((10, 0), (0, 5))
    assert Xf().ends(xf((0, 0), (10, 5), ' flipV="1"')) == ((0, 5), (10, 0))
    assert Xf().ends(xf((0, 0), (10, 5), ' flipH="1" flipV="1"')) == ((10, 5), (0, 0))
    # a horizontal line turned 90 degrees clockwise about its centre runs from top to bottom
    p0, p1 = Xf().ends(xf((0, 0), (10, 0), ' rot="5400000"'))
    assert close(p0, (5, -5)) and close(p1, (5, 5)), (p0, p1)
    assert Xf().ends(None) is None


def test_arrows_and_kind():
    assert arrows(sppr('<a:ln><a:tailEnd type="triangle"/></a:ln>')) == (False, True)
    assert arrows(sppr('<a:ln><a:headEnd type="arrow"/><a:tailEnd type="none"/></a:ln>')) == (True, False)
    assert arrows(sppr('<a:ln><a:headEnd/></a:ln>')) == (False, False)
    assert arrows(sppr("")) == (False, False) and arrows(None) == (False, False)
    table = {"flowChartDecision": "decision", "diamond": "decision", "flowChartTerminator": "terminator",
             "ellipse": "circle", "flowChartConnector": "circle", "can": "store", "flowChartMagneticDisk": "store",
             "rect": "", "roundRect": "", "line": ""}
    for prst, want in table.items():
        got = kind(sppr(f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'))
        assert got == want, (prst, got)
    assert kind(sppr("")) == "" and kind(None) == ""
    assert is_line(sppr('<a:prstGeom prst="line"/>')) and is_line(sppr('<a:prstGeom prst="straightConnector1"/>'))
    assert not is_line(sppr('<a:prstGeom prst="rect"/>')) and not is_line(None)


def test_glue_pptx_and_word():
    cxn = el(f'<p:cxnSp {NS}><p:nvCxnSpPr><p:cNvPr id="4" name="c"/><p:cNvCxnSpPr><a:stCxn id="2" idx="1"/>'
             '<a:endCxn id="3" idx="0"/></p:cNvCxnSpPr><p:nvPr/></p:nvCxnSpPr><p:spPr/></p:cxnSp>')
    assert glue(cxn) == ("2", "3"), glue(cxn)
    wsp = el(f'<wps:wsp {NS}><wps:cNvPr id="4" name="c"/><wps:cNvCnPr><a:stCxn id="2" idx="1"/>'
             '<a:endCxn id="3" idx="0"/></wps:cNvCnPr><wps:spPr/></wps:wsp>')
    assert glue(wsp) == ("2", "3"), glue(wsp)
    half = el(f'<p:cxnSp {NS}><p:nvCxnSpPr><p:cNvPr id="4" name="c"/><p:cNvCxnSpPr><a:endCxn id="9"/>'
              '</p:cNvCxnSpPr></p:nvCxnSpPr></p:cxnSp>')
    assert glue(half) == (None, "9") and glue(el(f"<p:sp {NS}/>")) == (None, None)


def test_hostile_numbers_never_raise():
    for extra in (' rot="x"', ' rot="99999999999999999999999"', ' rot="-5400000"'):
        Xf().box(xf((0, 0), (10, 10), extra))
        Xf().ends(xf((0, 0), (10, 10), extra))
    assert Xf().box(xf(("9" * 40, "x"), (1, 1))) is None
    huge = Xf().box(xf(("9" * 40, 0), ("9" * 40, 1)))
    assert huge is not None and all(abs(v) <= 1e15 for v in huge), huge


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
