"""SmartArt text in docx and pptx: a nested list in tree order, the point and connection filters, caps, wiring.

Run: python -I -X utf8 tests/test_reader_smartart.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from test_reader_docm import docm, parse, tmp  # noqa: E402
from test_reader_slides import deck, md as slide_md  # noqa: E402

DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
RELS_D = (("rD", "diagramData", "diagrams/data1.xml", False),)
REF = f'<dgm:relIds xmlns:dgm="{DGM}" r:dm="rD"/>'


def data_xml(points, cxns=()):
    """points: (modelId, type or None, text or None); cxns: (src, dest, srcOrd or None, type or None)."""
    pts = ""
    for mid, typ, text in points:
        t = f' type="{typ}"' if typ else ""
        body = f'<dgm:t><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></dgm:t>' if text else ""
        pts += f'<dgm:pt modelId="{mid}"{t}>{body}</dgm:pt>'
    cx = ""
    for i, (s, d, o, typ) in enumerate(cxns):
        oo = f' srcOrd="{o}"' if o is not None else ""
        t = f' type="{typ}"' if typ else ""
        cx += f'<dgm:cxn modelId="c{i}" srcId="{s}" destId="{d}"{oo}{t}/>'
    return (f'<dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="{A}"><dgm:ptLst>{pts}</dgm:ptLst>'
            f'<dgm:cxnLst>{cx}</dgm:cxnLst></dgm:dataModel>')


def word(data, n=1):
    para = ('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>' + REF +
            '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    body = para * n + '<w:p><w:r><w:t>뒤</w:t></w:r></w:p>'
    return docm(body, {"word/diagrams/data1.xml": data}, RELS_D)


def lines(path):
    doc, ctx = parse(path)
    return markdown.render(doc, tmp() / "assets", skipped=ctx.skipped).split("\n"), ctx


def items(ls):
    return [l for l in ls if l.strip().startswith("- ")]


DOC = ("0", "doc", None)


def test_tree_order_by_srcOrd():
    d = data_xml([DOC, ("A", None, "A"), ("B", "node", "B"), ("A1", None, "A1")],
                 [("0", "A", 1, None), ("0", "B", 0, "parOf"), ("A", "A1", 0, None)])
    ls, _ = lines(word(d))
    assert items(ls) == ["- B", "- A", "  - A1"], ls


def test_textless_node_promotes():
    d = data_xml([DOC, ("X", None, None), ("Y", None, "Y")], [("0", "X", 0, None), ("X", "Y", 0, None)])
    ls, _ = lines(word(d))
    assert "- Y" in ls and "  - Y" not in ls, ls


def test_cycle_listed_once():
    d = data_xml([DOC, ("A", None, "A"), ("B", None, "B"), ("C", None, "C")],
                 [("0", "A", 0, None), ("B", "C", 0, "parOf"), ("C", "B", 0, "parOf")])
    t = time.time()
    ls, _ = lines(word(d))
    assert time.time() - t < 1
    assert items(ls) == ["- A", "- B", "- C"], ls


def test_missing_parent_listed():
    d = data_xml([DOC, ("Q", None, "Q")], [("ghost", "Q", 0, None)])
    ls, _ = lines(word(d))
    assert items(ls) == ["- Q"], ls


def test_node_cap():
    d = data_xml([DOC] + [(f"n{i}", None, f"T{i}") for i in range(5)], [("0", f"n{i}", i, None) for i in range(5)])
    old, limits.SMARTART_NODES = limits.SMARTART_NODES, 3
    try:
        doc, ctx = parse(word(d))
        out = markdown.render(doc, tmp() / "assets", skipped=ctx.skipped)
    finally:
        limits.SMARTART_NODES = old
    assert out.count("- T") == 3 and ctx.skipped["smartart_cut"] == 1, out
    assert "(could not read: 1 SmartArt diagram cut short)" in out, out


def test_point_types_ignored():
    d = data_xml([DOC, ("P", "parTrans", "ptx"), ("S", "sibTrans", "stx"), ("R", "pres", "prx")],
                 [("0", "P", 0, "presOf"), ("0", "R", 1, "presOf")])
    ls, _ = lines(word(d))
    assert not any(x in l for l in ls for x in ("ptx", "stx", "prx")), ls


def test_assistant_listed():
    d = data_xml([DOC, ("Boss", None, "Boss"), ("Aide", "asst", "Aide"), ("Team", "node", "Team")],
                 [("0", "Boss", 0, None), ("Boss", "Aide", 0, "parOf"), ("Boss", "Team", 1, None)])
    ls, _ = lines(word(d))
    assert items(ls) == ["- Boss", "  - Aide", "  - Team"], ls


def test_missing_data_part():
    body = ('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>'
            f'<dgm:relIds xmlns:dgm="{DGM}" r:dm="nope"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
            '<w:p><w:r><w:t>뒤</w:t></w:r></w:p>')
    doc, ctx = parse(docm(body))
    out = markdown.render(doc, tmp() / "assets", skipped=ctx.skipped)
    assert "뒤" in out and "(could not read: 1 SmartArt diagram)" in out, out


def test_slide_smartart():
    d = data_xml([DOC, ("A", None, "알파"), ("B", None, "베타")], [("0", "A", 0, None), ("A", "B", 0, None)])
    gf = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="d"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
          f'<a:graphic><a:graphicData>{REF}</a:graphicData></a:graphic></p:graphicFrame>')
    out = slide_md(deck([gf], srels={1: [("rD", "diagramData", "../diagrams/data1.xml")]},
                        extra={"ppt/diagrams/data1.xml": d}))
    assert out.index("## Slide 1") < out.index("- 알파") < out.index("  - 베타"), out


def test_placements_charged():
    d = data_xml([DOC, ("A", None, "A"), ("B", None, "B")], [("0", "A", 0, None), ("0", "B", 1, None)])
    old, limits.SMARTART_NODES = limits.SMARTART_NODES, 4
    try:
        doc, ctx = parse(word(d, 3))
        out = markdown.render(doc, tmp() / "assets", skipped=ctx.skipped)
    finally:
        limits.SMARTART_NODES = old
    assert out.count("- A") + out.count("- B") == 4, out
    assert ctx.skipped["smartart_cut"] >= 1, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
