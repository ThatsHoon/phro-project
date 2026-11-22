"""Visio (vsdx) parser: page order, text markers, groups, images, master text, diagrams (geometry, arrows through
masters and line styles, kinds, containers), macros, hostile input.

Run: python -I -X utf8 tests/test_reader_vsdx.py
     python -I -X utf8 tests/test_reader_vsdx.py measure   (times the hostile master and connection probes)
"""
import math
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import diagram, markdown  # noqa: E402
from reader import limits  # noqa: E402
from reader.formats import vsdx as VX  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Para, Text  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from reader_fix import make_zip  # noqa: E402

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
VNS = 'xmlns="http://schemas.microsoft.com/office/visio/2012/main"'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def tmp():
    return Path(tempfile.mkdtemp())


def rels_xml(items):
    return (f'<Relationships xmlns="{PKG}">'
            + "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{tg}"/>' for i, t, tg in items)
            + "</Relationships>")


def cells(x=None, y=None, lx=None, ly=None):
    out = ""
    for n, v in (("PinX", x), ("PinY", y), ("LocPinX", lx), ("LocPinY", ly)):
        if v is not None:
            out += f'<Cell N="{n}" V="{v}"/>'
    return out


def shape(i=1, text=None, pos=(), inner="", attrs=""):
    t = f"<Text>{text}</Text>" if text is not None else ""
    return f'<Shape ID="{i}" {attrs}>{cells(*pos)}{t}{inner}</Shape>'


def page_xml(shapes, connects=""):
    c = f"<Connects>{connects}</Connects>" if connects else ""
    return f'<PageContents xmlns:r="{REL}" {VNS}><Shapes>{shapes}</Shapes>{c}</PageContents>'


def connect(sheet, cell, to):
    return f'<Connect FromSheet="{sheet}" FromCell="{cell}" FromPart="9" ToSheet="{to}" ToCell="PinX" ToPart="3"/>'


def glued(sheet, begin, end):
    return connect(sheet, "BeginX", begin) + connect(sheet, "EndX", end)


def cell(**cells):
    """Cell elements, one per keyword, in order."""
    return "".join(f'<Cell N="{n}" V="{v}"/>' for n, v in cells.items())


def box_cells(x, y, w, h):
    """A 2-D shape's cells: pin (x, y) at the centre of a w by h box."""
    return cell(PinX=x, PinY=y, Width=w, Height=h, LocPinX=w / 2, LocPinY=h / 2)


def line(i, p0, p1, attrs=""):
    """A 1-D shape from p0 to p1 (BeginX..EndY), no text."""
    return f'<Shape ID="{i}" {attrs}>' + cell(BeginX=p0[0], BeginY=p0[1], EndX=p1[0], EndY=p1[1]) + "</Shape>"


CONTAINER = '<Section N="User"><Row N="msvStructureType"><Cell N="Value" V="Container" U="STR"/></Row></Section>'


def styles_xml(sheets):
    """{StyleSheet ID: (its LineStyle or None, cells xml)} -> visio/document.xml."""
    body = "".join(f'<StyleSheet ID="{i}"' + (f' LineStyle="{p}"' if p is not None else "") + f">{c}</StyleSheet>"
                   for i, (p, c) in sheets.items())
    return f"<VisioDocument {VNS}><StyleSheets>{body}</StyleSheets></VisioDocument>"


def mermaid_of(out):
    """The text of the only mermaid block in rendered Markdown."""
    assert out.count("```mermaid\n") == 1, out
    return out.split("```mermaid\n", 1)[1].split("\n```", 1)[0]


def master_members(masters, names=None):
    """{Master ID: shapes xml} -> masters.xml, its rels and one masterN.xml part each (N counts from 1); `names` maps
    a Master ID to its NameU (default M<ID>)."""
    ids = list(masters)
    names = names or {}
    out = {
        "visio/masters/masters.xml": f'<Masters xmlns:r="{REL}" {VNS}>' + "".join(
            f'<Master ID="{m}" NameU="{names.get(m, "M" + m)}"><Rel r:id="rId{n}"/></Master>'
            for n, m in enumerate(ids, 1)) + "</Masters>",
        "visio/masters/_rels/masters.xml.rels": rels_xml(
            [(f"rId{n}", "master", f"master{n}.xml") for n in range(1, len(ids) + 1)]),
    }
    for n, m in enumerate(ids, 1):
        out[f"visio/masters/master{n}.xml"] = f"<MasterContents {VNS}><Shapes>{masters[m]}</Shapes></MasterContents>"
    return out


def vsdx(pages, listed=None, extra=None, page_rels=None, masters=None, mem=False, mnames=None):
    """pages: {name: shapes xml (or full xml)}, stored as page1.xml..; listed: [(Name, index)] for pages.xml;
    masters: {Master ID: shapes xml}, mnames: {Master ID: NameU}; mem: return the zip's bytes (nothing written)."""
    names = list(pages)
    listed = listed if listed is not None else [(n, i + 1) for i, n in enumerate(names)]
    doc_rels = [("rId1", "pages", "pages/pages.xml")] + ([("rId2", "masters", "masters/masters.xml")] if masters else [])
    members = {
        "_rels/.rels": rels_xml([("rId1", "document", "visio/document.xml")]),
        "visio/document.xml": f"<VisioDocument {VNS}/>",
        "visio/_rels/document.xml.rels": rels_xml(doc_rels),
        "visio/pages/pages.xml": f'<Pages xmlns:r="{REL}" {VNS}>' + "".join(
            f'<Page ID="{i}" Name="{n}"><Rel r:id="rId{i}"/></Page>' for n, i in listed) + "</Pages>",
        "visio/pages/_rels/pages.xml.rels": rels_xml(
            [(f"rId{i + 1}", "page", f"page{i + 1}.xml") for i in range(len(names))]),
    }
    for i, n in enumerate(names, 1):
        body = pages[n]
        members[f"visio/pages/page{i}.xml"] = body if body.startswith("<PageContents") else page_xml(body)
    for i, r in (page_rels or {}).items():
        members[f"visio/pages/_rels/page{i}.xml.rels"] = rels_xml(r)
    members.update(master_members(masters, mnames) if masters else {})
    members.update(extra or {})
    return make_zip(None if mem else tmp() / "a.vsdx", members, stored=())


def md(path):
    ctx = Context()
    doc = reader.parse(path, ctx)
    return markdown.render(doc, tmp() / "assets"), ctx


def raises_kind(fn, kind):
    try:
        fn()
    except ReadError as e:
        assert e.kind == kind, (e.kind, kind)
        return
    raise AssertionError("ReadError not raised")


def test_pages_in_order():
    p = vsdx({"Page-1": shape(1, "one"), "Page-2": shape(1, "two")}, listed=[("Page-2", 2), ("Page-1", 1)])
    out, _ = md(p)
    assert out.index("## Page: Page-2") < out.index("two") < out.index("## Page: Page-1") < out.index("one")


def test_shape_text_markers():
    p = vsdx({"P": shape(1, 'Hello<cp IX="0"/>Router<pp IX="0"/> here<fld IX="0">7</fld> end')})
    out, _ = md(p)
    assert "HelloRouter here7 end" in out


def test_text_line_break_and_empty_skipped():
    out, _ = md(vsdx({"P": shape(1, "a\nb", (0, 9)) + shape(2, "   ", (0, 8)) + shape(3, None, (0, 7))}))
    assert out == "## Page: P\n\na\nb\n", repr(out)


def test_group_recursion_and_order():
    kids_ = shape(2, "low", (0, 5)) + shape(3, "high", (0, 8))
    p = vsdx({"P": shape(1, None, (1, 1), f"<Shapes>{kids_}</Shapes>", 'Type="Group"')})
    out, _ = md(p)
    assert out.index("high") < out.index("low")


def test_group_offset_and_page_order():
    # group at pin (0,10), locpin (0,0): child y 1 -> 11, above a page shape at y 5
    g = shape(1, None, (0, 10, 0, 0), "<Shapes>" + shape(2, "child", (0, 1)) + "</Shapes>")
    p = vsdx({"P": shape(9, "page", (0, 5)) + g})
    out, _ = md(p)
    assert out.index("child") < out.index("page")


def test_group_offset_sign():
    # offset = Pin - LocPin = 10 - 6 = +4: child y 2 -> 6, below the page shape at 9 (+LocPin would give 18)
    g = shape(1, None, (0, 10, 0, 6), "<Shapes>" + shape(2, "child", (0, 2)) + "</Shapes>")
    out, _ = md(vsdx({"P": shape(9, "page", (0, 9)) + g}))
    assert out.index("page") < out.index("child")
    # offset X = 10 - 6 = +4: child x 2 -> 6 stays right of the page shape at x 5, same y
    g = shape(1, None, (10, 0, 6, 0), "<Shapes>" + shape(2, "child", (2, 0)) + "</Shapes>")
    out, _ = md(vsdx({"P": shape(9, "page", (5, 0)) + g}))
    assert out.index("page") < out.index("child")
    g = shape(1, None, (10, 0, 6, 0), "<Shapes>" + shape(2, "child", (0, 0)) + "</Shapes>")
    out, _ = md(vsdx({"P": shape(9, "page", (5, 0)) + g}))  # child x 0 -> 4, left of 5
    assert out.index("child") < out.index("page")


def test_nested_group_offsets_add():
    # outer +4 (10-6), inner +3 (8-5): grandchild y 1 -> 8; sits between the page shapes at 9 and 7
    inner = shape(3, None, (0, 8, 0, 5), "<Shapes>" + shape(4, "grand", (0, 1)) + "</Shapes>")
    outer = shape(2, None, (0, 10, 0, 6), f"<Shapes>{inner}</Shapes>")
    out, _ = md(vsdx({"P": shape(8, "above", (0, 9)) + outer + shape(9, "below", (0, 7))}))
    assert out.index("above") < out.index("grand") < out.index("below")


def test_missing_locpin_counts_as_zero():
    # PinY 10, no LocPinY: offset +10, child y 1 -> 11, above the page shape at 9
    g = shape(1, None, (0, 10), "<Shapes>" + shape(2, "child", (0, 1)) + "</Shapes>")
    out, _ = md(vsdx({"P": shape(9, "page", (0, 9)) + g}))
    assert out.index("child") < out.index("page")
    # same on X: PinX 10, no LocPinX, child x 1 -> 11, right of the page shape at x 5
    g = shape(1, None, (10, 0), "<Shapes>" + shape(2, "child", (1, 0)) + "</Shapes>")
    out, _ = md(vsdx({"P": shape(9, "page", (5, 0)) + g}))
    assert out.index("page") < out.index("child")
    # LocPinY without PinY: offset -6, child y 8 -> 2, below the page shape at 5
    g = shape(1, None, (), "<Shapes>" + shape(2, "child", (0, 8)) + "</Shapes>")
    g = g.replace("<Shapes>", '<Cell N="LocPinY" V="6"/><Shapes>')
    out, _ = md(vsdx({"P": shape(9, "page", (0, 5)) + g}))
    assert out.index("page") < out.index("child")


def test_unpositioned_after_positioned():
    p = vsdx({"P": shape(1, "free") + shape(2, "placed", (0, 1))})
    out, _ = md(p)
    assert out.index("placed") < out.index("free")


def test_image_shape():
    fd = '<ForeignData ForeignType="Bitmap"><Rel r:id="rId1"/></ForeignData>'
    p = vsdx({"P": shape(1, None, (0, 0), fd)}, extra={"visio/media/image1.png": PNG},
             page_rels={1: [("rId1", "image", "../media/image1.png")]})
    out, _ = md(p)
    assert "![](assets/img-001.png)" in out


def test_vsdx_not_a_package():
    p = make_zip(tmp() / "a.vsdx", {"x.txt": "hi"}, stored=())
    raises_kind(lambda: reader.parse(p, Context()), "damaged")
    q = make_zip(tmp() / "b.vsdx", {"_rels/.rels": rels_xml([("rId1", "document", "visio/document.xml")])}, stored=())
    raises_kind(lambda: reader.parse(q, Context()), "damaged")


def test_zero_pages_is_empty():
    out, _ = md(vsdx({}, listed=[]))
    assert out.strip() == ""


def test_duplicate_page_part_listed_once():
    p = vsdx({"P": shape(1, "once")}, listed=[("P", 1)] * 500)
    out, _ = md(p)
    assert out.count("once") == 1 and out.count("## Page") == 1


def test_damaged_page_loses_only_itself():
    p = vsdx({"A": "<PageContents", "B": shape(1, "kept")})
    out, ctx = md(p)
    assert "kept" in out and ctx.skipped["page"] == 1
    raises_kind(lambda: reader.parse(vsdx({"A": "<PageContents"}), Context()), "damaged")


def test_deep_groups_do_not_recurse_forever():
    xml = shape(1, "leaf")
    for i in range(5000):
        xml = shape(i + 2, "g", (0, 0), f"<Shapes>{xml}</Shapes>")
    t0 = time.monotonic()
    out, ctx = md(vsdx({"P": xml}))
    assert time.monotonic() - t0 < 10 and ctx.skipped["depth"] >= 1 and "g" in out


def test_bad_numbers_mean_no_position():
    bad = ["nan", "inf", "-inf", "x", "", "1e999"]
    shapes = "".join(shape(i + 1, f"t{i}", (b, b)) for i, b in enumerate(bad))
    shapes += shape(20, "ok", (1, 1)) + shape(21, "half", ("nan", 3), "<Shapes>" + shape(22, "kid", (1, 1)) + "</Shapes>")
    out, _ = md(vsdx({"P": shapes}))
    assert out.index("ok") < out.index("t0") and "kid" in out


def test_shared_media_and_master_text():
    fd = '<ForeignData ForeignType="Bitmap"><Rel r:id="rId1"/></ForeignData>'
    many = "".join(shape(i, None, (i, 0), fd) for i in range(1, 300))
    many += shape(999, None, (0, 9), "", 'Master="2" MasterShape="3"')
    master = (f'<MasterContents {VNS}><Shapes><Shape ID="3"><Text>MASTERTEXT</Text></Shape></Shapes></MasterContents>')
    p = vsdx({"P": many}, extra={"visio/media/image1.png": PNG, "visio/masters/master1.xml": master,
                                 "visio/masters/masters.xml": f'<Masters xmlns:r="{REL}" {VNS}><Master ID="2">'
                                                              '<Rel r:id="rId1"/></Master></Masters>',
                                 "visio/masters/_rels/masters.xml.rels": rels_xml([("rId1", "master", "master1.xml")])},
             page_rels={1: [("rId1", "image", "../media/image1.png")]})
    out, ctx = md(p)
    assert out.count("![](") == 299 and not ctx.skipped["image"] and out.count("MASTERTEXT") == 1, out[-300:]


MASTER1 = (shape(5, "FIRST") + shape(6, "SIXTH")
           + shape(8, None, (), "<Shapes>" + shape(9, None, (), "<Shapes>" + shape(7, "SEVENTH") + "</Shapes>")
                   + "</Shapes>"))


def test_master_text_top_and_sub_shape():
    group = shape(3, None, (0, 7), "<Shapes>" + shape(4, None, (0, 0), "", 'MasterShape="7"') + "</Shapes>",
                  'Master="1" MasterShape="8"')
    page = (shape(1, None, (0, 9), "", 'Master="1"') + shape(2, None, (0, 8), "", 'Master="1" MasterShape="6"')
            + group)
    out, ctx = md(vsdx({"P": page}, masters={"1": MASTER1}))
    assert out.index("FIRST") < out.index("SIXTH") < out.index("SEVENTH"), out
    assert out.count("FIRST") == 1 and out.count("SIXTH") == 1 and out.count("SEVENTH") == 1
    assert not ctx.skipped["stencil"], ctx.skipped


def test_own_text_wins():
    page = shape(1, "mine", (0, 9), "", 'Master="1"') + shape(2, "", (0, 8), "", 'Master="1"')
    out, _ = md(vsdx({"P": page}, masters={"1": MASTER1}))
    assert "mine" in out and "FIRST" not in out, out


HIDE = '<Cell N="HideText" V="1"/>'
SHOW = '<Cell N="HideText" V="0"/>'


def test_hidden_text_own_and_inherited():
    # a master's placeholder text hidden by the master's HideText stays hidden on its instances unless they show it
    m = shape(5, None, (), HIDE + "<Text>PLACEHOLDER</Text>")
    page = (shape(1, None, (0, 9), "", 'Master="1"') + shape(2, None, (0, 8), SHOW, 'Master="1"')
            + shape(3, "OWNHIDDEN", (0, 7), HIDE) + shape(4, "shown", (0, 6)))
    p = vsdx({"P": page}, masters={"1": m})
    doc = reader.parse(p, Context())
    out = markdown.render(doc, tmp() / "assets")
    assert out.count("PLACEHOLDER") == 1 and "OWNHIDDEN" not in out and "shown" in out, out
    assert out.startswith("(2 hidden places left out)"), out
    shown = markdown.render(doc, tmp() / "assets", hidden=True)
    assert shown.count("PLACEHOLDER") == 2 and "OWNHIDDEN" in shown, shown


def test_connector_long_text_follows():
    long = "v" * 90
    shapes = shape(1, "Start", (0, 9)) + shape(2, "End", (0, 1)) + shape(3, long, (0, 5))
    out, _ = md(vsdx({"P": page_xml(shapes, glued(3, 1, 2))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t.endswith('\n  n1 -->|"' + "v" * 80 + '…"| n2'), t
    assert out.endswith(f"```\n\nn1 --> n2:\n\n{long}\n") and out.count(long) == 1, out


def test_hidden_connector_text_not_a_label():
    shapes = (shape(1, "Start", (0, 9)) + shape(2, None, (0, 1), HIDE + "<Text>secret</Text>", 'NameU="End"')
              + shape(3, None, (0, 5), HIDE + "<Text>quiet</Text>"))
    p = vsdx({"P": page_xml(shapes, glued(3, 1, 2))})
    out, _ = md(p)
    t = mermaid_of(out)
    # a hidden end is a blank node; the connector's hidden text is no label and stays in the flow, hidden
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Start"]\n  n1 --> n2', t
    assert "secret" not in out and "quiet" not in out, out
    shown = markdown.render(reader.parse(p, Context()), tmp() / "assets", hidden=True)
    assert "quiet" in shown and "secret" in shown and mermaid_of(shown) == t, shown


def test_masters_cap():
    old = VX.MASTERS
    VX.MASTERS = 1
    try:
        page = shape(1, None, (0, 9), "", 'Master="1"') + shape(2, None, (0, 8), "", 'Master="2"')
        out, ctx = md(vsdx({"P": page}, masters={"1": shape(1, "MONE"), "2": shape(1, "MTWO")}))
    finally:
        VX.MASTERS = old
    assert "MONE" in out and "MTWO" not in out and ctx.skipped["stencil"] == 1, (out, ctx.skipped)


def test_connections():
    shapes = shape(1, "Start", (0, 9)) + shape(2, "End", (0, 1)) + shape(3, None, (0, 5)) + shape(4, "yes", (0, 4))
    out, ctx = md(vsdx({"P": page_xml(shapes, glued(3, 1, 2) + glued(4, 1, 2))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 --> n2\n  n1 -->|"yes"| n2', t
    assert "Connections:" not in out and "→" not in out and out.count("yes") == 1, out
    assert out == f"## Page: P\n\n```mermaid\n{t}\n```\n", repr(out)


def test_duplicate_id_not_glued_again():
    # Connects rows name the first shape of an ID: a later shape repeating it is an ordinary shape
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 1)) + shape(3) + shape(3, "dup", (0, 5))
    out, _ = md(vsdx({"P": page_xml(shapes, glued(3, 1, 2))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2["B"]\n  n1 --> n2', t
    assert out.endswith("```\n\ndup\n"), repr(out)


def test_diagram_before_later_pictures():
    # the diagram stands where the first connected shape stood; a picture below it keeps its place after it
    fd = '<ForeignData ForeignType="Bitmap"><Rel r:id="rId1"/></ForeignData>'
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 8)) + shape(3) + shape(5, None, (0, 1), fd)
    p = vsdx({"P": page_xml(shapes, glued(3, 1, 2))}, extra={"visio/media/image1.png": PNG},
             page_rels={1: [("rId1", "image", "../media/image1.png")]})
    out, _ = md(p)
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2["B"]\n  n1 --> n2', t
    assert out.index("```mermaid") < out.index("![](") and out.endswith("![](assets/img-001.png)\n"), out


def test_one_end_connector():
    # glued at one end, with no end point to place: no diagram, its text stays, counted loose
    shapes = shape(1, "Start", (0, 9)) + shape(2, "End", (0, 1)) + shape(3, "dangle", (0, 5))
    out, ctx = md(vsdx({"P": page_xml(shapes, connect(3, "BeginX", 1))}))
    assert "```mermaid" not in out and "dangle" in out and ctx.skipped["connector_loose"] == 1, (out, ctx.skipped)
    out, ctx = md(vsdx({"P": page_xml(shapes, connect(3, "EndX", 2) + connect(3, "PinX", 1))}))
    assert "```mermaid" not in out and "dangle" in out and ctx.skipped["connector_loose"] == 1, (out, ctx.skipped)


def test_one_end_connector_inferred():
    # BeginX glued to A, EndX free but inside B's box: an inferred edge
    shapes = (shape(1, "A", (), box_cells(0.5, 0.5, 1, 1)) + shape(2, "B", (), box_cells(4.5, 0.5, 1, 1))
              + line(3, (1, 0.5), (4.5, 0.5)))
    out, ctx = md(vsdx({"P": page_xml(shapes, connect(3, "BeginX", 1))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 --> n2', t
    assert not ctx.skipped["connector_loose"], ctx.skipped
    # the free end outside every box (and past INFER_TOL): no edge, counted loose
    shapes = shapes.replace(line(3, (1, 0.5), (4.5, 0.5)), line(3, (1, 0.5), (2.5, 3)))
    out, ctx = md(vsdx({"P": page_xml(shapes, connect(3, "BeginX", 1))}))
    assert "```mermaid" not in out and ctx.skipped["connector_loose"] == 1, (out, ctx.skipped)


def test_node_labels():
    # textless ends are blank nodes; long text is cut with … and follows the diagram after "nK:"; an end at a missing
    # shape is no glue (counted loose) and that connector's other end keeps its text in the flow
    shapes = (shape(1, None, (), "", 'NameU="Alpha" Name="Ignored"') + shape(2, None, (), "", 'Name="Beta"')
              + shape(4) + shape(5, "x" * 100 + "\nsecond") + shape(6, "\n  lead \nmore", (), "", 'NameU="No"')
              + shape(10) + shape(11) + shape(12))
    out, ctx = md(vsdx({"P": page_xml(shapes, glued(10, 1, 2) + glued(11, 4, 5) + glued(12, 6, 99))}))
    t = mermaid_of(out)
    want = ('flowchart TD\n  n4["' + "x" * 80 + '…"]\n'
            '  n1 --> n2\n  n3 --> n4')
    assert mermaid_ok(t) and t == want, t
    assert out.endswith("```\n\nn4:\n\n" + "x" * 100 + "\nsecond\n\n  lead\nmore\n"), repr(out)
    assert "Alpha" not in out and "Beta" not in out and ctx.skipped["connector_loose"] == 1, (out, ctx.skipped)


def test_connections_cap():
    old = diagram.CONNECTIONS
    diagram.CONNECTIONS = 1
    try:
        shapes = shape(1, "Start", (0, 9)) + shape(2, "End", (0, 1)) + shape(3) + shape(4)
        out, ctx = md(vsdx({"P": page_xml(shapes, glued(3, 1, 2) + glued(4, 2, 1))}))
    finally:
        diagram.CONNECTIONS = old
    t = mermaid_of(out)
    assert mermaid_ok(t) and t.endswith('"]\n  n1 --> n2') and t.count("-->") == 1, t
    assert ctx.skipped["connections_cut"] == 1, ctx.skipped


def edge(attrs="", inner="", styles=None, masters=None):
    """The edge line of A -> B joined by connector 3 (its attributes and cells given), with document.xml's style
    sheets and masters given."""
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 1)) + shape(3, None, (), inner, attrs)
    extra = {"visio/document.xml": styles_xml(styles)} if styles is not None else None
    out, _ = md(vsdx({"P": page_xml(shapes, glued(3, 1, 2))}, extra=extra, masters=masters))
    t = mermaid_of(out)
    assert mermaid_ok(t), t
    return t.split("\n")[-1].strip()


def test_arrow_from_line_style():
    assert edge() == "n1 --> n2"  # not known
    assert edge('LineStyle="3"', styles={"3": (None, cell(EndArrow=13))}) == "n1 --> n2"
    assert edge('LineStyle="3"', styles={"3": (None, cell(BeginArrow=13))}) == "n2 --> n1"
    assert edge('LineStyle="3"', styles={"3": (None, cell(BeginArrow=13, EndArrow=4))}) == "n1 <--> n2"
    assert edge('LineStyle="3"', styles={"3": (None, cell(BeginArrow=0, EndArrow=0))}) == "n1 --- n2"
    # each cell from the first style of the chain that has it
    chain = {"5": ("3", cell(EndArrow=0)), "3": (None, cell(BeginArrow=1, EndArrow=1))}
    assert edge('LineStyle="5"', styles=chain) == "n2 --> n1"
    # the master shape's LineStyle when the shape names none; a style not listed, or junk, gives nothing
    m = {"1": shape(1, None, (), "", 'LineStyle="3"')}
    assert edge('Master="1"', styles={"3": (None, cell(BeginArrow=2))}, masters=m) == "n2 --> n1"
    assert edge('LineStyle="9"', styles={"3": (None, cell(BeginArrow=2))}) == "n1 --> n2"
    assert edge('LineStyle="3"', styles={"3": (None, cell(BeginArrow="x"))}) == "n1 --> n2"


def test_style_sheets_loaded_once():
    loads = []
    real = VX.ooxml_base.load

    def counting(pkg, part):
        loads.append(part)
        return real(pkg, part)
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 1)) + "".join(
        shape(i, None, (), "", f'LineStyle="{i % 2 + 3}"') for i in range(3, 103))
    links = "".join(glued(i, 1, 2) for i in range(3, 103))
    styles = styles_xml({"3": (None, cell(EndArrow=1)), "4": ("3", "")})
    data = vsdx({"P": page_xml(shapes, links), "Q": page_xml(shapes, links)}, extra={"visio/document.xml": styles},
                mem=True)
    VX.ooxml_base.load = counting
    try:
        doc, ctx = parse_mem(data)
    finally:
        VX.ooxml_base.load = real
    assert loads.count("visio/document.xml") == 1, loads.count("visio/document.xml")
    out = markdown.render(doc, tmp() / "assets")
    blocks = [b.split("\n```", 1)[0] for b in out.split("```mermaid\n")[1:]]
    assert len(blocks) == 2 and all(mermaid_ok(t) for t in blocks), out
    assert out.count("n1 --> n2") == 200 and not ctx.skipped, (out.count("n1 --> n2"), ctx.skipped)


def test_style_chain_cycle():
    assert edge('LineStyle="3"', styles={"3": ("4", ""), "4": ("3", "")}) == "n1 --> n2"
    assert edge('LineStyle="3"', styles={"3": ("3", "")}) == "n1 --> n2"
    # MAX_DEPTH styles are walked: an arrow on the last one is found, one style further it is not
    for n, want in ((limits.MAX_DEPTH, "n2 --> n1"), (limits.MAX_DEPTH + 1, "n1 --> n2")):
        chain = {str(i): (str(i + 1), "") for i in range(1, n)}
        chain[str(n)] = (None, cell(BeginArrow=1))
        assert edge('LineStyle="1"', styles=chain) == want, n


def test_own_arrow_overrides_master():
    m = {"1": shape(1, None, (), cell(EndArrow=13))}
    assert edge('Master="1"', cell(BeginArrow=0, EndArrow=0), masters=m) == "n1 --- n2"
    assert edge('Master="1"', masters={"1": shape(1, None, (), cell(BeginArrow=13))}) == "n2 --> n1"
    # the master's cell over the style chain, the shape's own over both
    m = {"1": shape(1, None, (), cell(BeginArrow=0), 'LineStyle="3"')}
    assert edge('Master="1"', masters=m, styles={"3": (None, cell(BeginArrow=1))}) == "n1 --- n2"
    styles = {"3": (None, cell(BeginArrow=1, EndArrow=1))}
    assert edge('LineStyle="3"', cell(BeginArrow=0), styles=styles) == "n1 --> n2"
    # a sub-shape takes its master shape's cells by MasterShape
    group = shape(3, None, (), "<Shapes>" + shape(4, None, (), "", 'MasterShape="7"') + "</Shapes>", 'Master="1"')
    m = {"1": shape(6, None, (), "<Shapes>" + shape(7, None, (), cell(BeginArrow=1)) + "</Shapes>")}
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 1)) + group
    out, _ = md(vsdx({"P": page_xml(shapes, glued(4, 1, 2))}, masters=m))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t.endswith("n2 --> n1"), out


def test_vsdx_kinds():
    masters = {"1": shape(1), "2": shape(1), "3": shape(1)}
    names = {"1": "Decision", "2": "Start/End", "3": "Process"}
    shapes = (shape(1, "Q", (0, 9), "", 'Master="1"') + shape(2, "S", (0, 8), "", 'Master="2"')
              + shape(3, "C", (0, 7), "", 'NameU="CIRCLE.12"') + shape(4, "D", (0, 6), "", 'NameU="Database"')
              + shape(5, "P", (0, 5), "", 'Master="3" NameU="Decision.4"')
              + "".join(shape(i) for i in range(11, 15)))
    links = glued(11, 1, 2) + glued(12, 2, 3) + glued(13, 3, 4) + glued(14, 4, 5)
    out, _ = md(vsdx({"P": page_xml(shapes, links)}, masters=masters, mnames=names))
    t = mermaid_of(out)
    want = ('flowchart TD\n  n1{"Q"}\n  n2(["S"])\n  n3(("C"))\n  n4[("D")]\n  n5["P"]\n'
            '  n1 --> n2\n  n2 --> n3\n  n3 --> n4\n  n4 --> n5')
    assert mermaid_ok(t) and t == want, t


def test_vsdx_container():
    a = shape(1, "A", (), box_cells(1, 3, 1, 1))
    b = shape(2, "B", (), box_cells(3, 1, 1, 1))
    want = 'flowchart TD\n  subgraph c1 ["Lane"]\n    n1["A"]\n    n2["B"]\n  end\n  n1 --> n2'
    lane = shape(10, "Lane", (), box_cells(2, 2, 4, 4) + CONTAINER)
    out, _ = md(vsdx({"P": page_xml(lane + a + b + shape(3), glued(3, 1, 2))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == want and out.count("Lane") == 1, out
    # the User row on the master shape
    lane = shape(10, "Lane", (), box_cells(2, 2, 4, 4), 'Master="1"')
    m = {"1": shape(1, None, (), CONTAINER)}
    out, _ = md(vsdx({"P": page_xml(lane + a + b + shape(3), glued(3, 1, 2))}, masters=m))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == want and out.count("Lane") == 1, out
    # another structure type is an ordinary shape
    lane = shape(10, "Lane", (), box_cells(2, 2, 4, 4) + CONTAINER.replace("Container", "List"))
    out, _ = md(vsdx({"P": page_xml(lane + a + b + shape(3), glued(3, 1, 2))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and "subgraph" not in t and "Lane" in out, out


def test_group_port_and_connector_glue():
    # glue on an actor's textless sub-shape goes to the actor; a connector whose begin is glued to another connector
    # joins the shape glued at that connector's nearest end
    actor = shape(81, "User", (), box_cells(1, 5, 1, 1) + "<Shapes>" + shape(82, None, (), box_cells(0.5, 0.5, 0.4, 1))
                  + "</Shapes>")
    a = shape(3, "Add", (), box_cells(5, 6, 1, 0.5))
    b = shape(4, "Edit", (), box_cells(5, 4, 1, 0.5))
    plain = cell(BeginArrow=0, EndArrow=0)
    trunk = line(21, (1.5, 5), (4.5, 6)).replace("</Shape>", plain + "</Shape>")
    branch = line(16, (1.5, 5), (4.5, 4)).replace("</Shape>", plain + "</Shape>")
    links = glued(21, 82, 3) + glued(16, 21, 4)
    out, ctx = md(vsdx({"P": page_xml(actor + a + b + trunk + branch, links)}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["Add"]\n  n2["User"]\n  n3["Edit"]\n  n2 --- n1\n  n2 --- n3', t
    assert not ctx.skipped["connector_loose"] and out.count("User") == 1, (out, ctx.skipped)


def test_group_text_names_group_and_connector_text_labels_edge():
    # a UML class: glue on the group, its name in a sub-shape; the association's end texts are its sub-shapes
    cls = lambda i, name, x: shape(i, None, (), box_cells(x, 5, 1, 1) + "<Shapes>"  # noqa: E731
                                   + shape(i + 1, name, (), box_cells(0.5, 0.8, 1, 0.3))
                                   + shape(i + 2, None, (), box_cells(0.5, 0.3, 1, 0.6)) + "</Shapes>")
    assoc = (f'<Shape ID="20">' + cell(BeginX=1.5, BeginY=5, EndX=4.5, EndY=5, PinX=3, PinY=5, Width=3, Height=0,
                                       LocPinX=1.5, LocPinY=0)
             + "<Shapes>" + shape(21, "-have", (), box_cells(0.2, 0.1, 0.4, 0.2))
             + shape(22, "1..*", (), box_cells(2.8, 0.1, 0.4, 0.2)) + "</Shapes></Shape>")
    out, _ = md(vsdx({"P": page_xml(cls(1, "Routes", 1) + cls(10, "Bus", 5) + assoc, glued(20, 1, 10))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["Routes"]\n  n2["Bus"]\n  n1 -->|"-have / 1..*"| n2', t
    assert out == f"## Page: P\n\n```mermaid\n{t}\n```\n", repr(out)


def paras(doc):
    """Each Para of each page as text, "hidden:" before a hidden one."""
    return [("hidden:" if b.hidden else "") + "".join(t.text for t in b.inlines if isinstance(t, Text))
            for sec in doc.blocks for b in sec.blocks if isinstance(b, Para)]


def test_master_name_shape_data_and_links():
    # a textless instance is named by its master; Shape Data rows (the master's labels and order, the instance's
    # values) and hyperlinks are lines of the shape; an invisible row is hidden
    master = shape(1, None, (), '<Section N="Property"><Row N="IP"><Cell N="Label" V="IP address"/>'
                                '<Cell N="Value" V=""/></Row><Row N="Spare"><Cell N="Label" V="Spare"/>'
                                '<Cell N="Value" V=""/></Row></Section>')
    props = ('<Section N="Property"><Row N="IP"><Cell N="Value" V="10.0.0.5" U="STR"/></Row>'
             '<Row N="Owner"><Cell N="Label" V="Owner"/><Cell N="Value" V="Ops team"/></Row></Section>'
             '<Section N="Hyperlink"><Row N="Row_1"><Cell N="Description" V="Runbook"/>'
             '<Cell N="Address" V="https://example.com/runbook"/><Cell N="SubAddress" V=""/></Row></Section>')
    server = shape(1, "Server", (0, 9), props)
    fw = shape(2, None, (0, 5), "", 'Master="7"')
    out, ctx = md(vsdx({"P": page_xml(server + fw + shape(3), glued(3, 1, 2))}, masters={"7": master},
                       mnames={"7": "Firewall"}))
    t = mermaid_of(out)
    want = ('flowchart TD\n  n1["Server<br>IP: 10.0.0.5<br>Owner: Ops team<br>Link: Runbook '
            'https://example.com/runbook"]\n  n2["Firewall"]\n  n1 --> n2')
    assert mermaid_ok(t) and t == want, t
    secret = '<Row N="Key"><Cell N="Label" V="Key"/><Cell N="Value" V="s3cret"/><Cell N="Invisible" V="1"/></Row>'
    own = props.replace("10.0.0.5", "10.0.0.6").replace("</Section><Section", secret + "</Section><Section")
    doc = reader.parse(vsdx({"P": server + shape(2, None, (0, 5), own, 'Master="7"')}, masters={"7": master}),
                       Context())
    assert paras(doc) == ["Server", "IP: 10.0.0.5", "Owner: Ops team", "Link: Runbook https://example.com/runbook",
                          "IP address: 10.0.0.6", "Owner: Ops team", "hidden:Key: s3cret",
                          "Link: Runbook https://example.com/runbook"], paras(doc)


def test_background_page():
    # a foreground page names its background page, and the background page names the pages it lies under
    pages = (f'<Pages xmlns:r="{REL}" {VNS}><Page ID="0" Name="Front" BackPage="4"><Rel r:id="rId1"/></Page>'
             '<Page ID="4" Name="Back" Background="1"><Rel r:id="rId2"/></Page>'
             '<Page ID="5" Name="Spare" Background="1"><Rel r:id="rId3"/></Page></Pages>')
    p = vsdx({"Front": shape(1, "f"), "Back": shape(1, "b"), "Spare": shape(1, "s")},
             extra={"visio/pages/pages.xml": pages})
    out, _ = md(p)
    assert out == ("## Page: Front\n\nBackground page: Back\n\nf\n\n## Page: Back\n\nBackground of: Front\n\nb\n\n"
                   "## Page: Spare\n\nBackground page\n\ns\n"), repr(out)


def test_group_child_box():
    # sub-shapes of a group at origin (10, 10): their boxes and the group's connector ends are page-absolute
    kids_ = shape(2, "A", (), box_cells(1, 1, 1, 1)) + shape(3, "B", (), box_cells(3, 1, 1, 1))
    group = shape(1, None, (), cell(PinX=10, PinY=10, Width=4, Height=2, LocPinX=0, LocPinY=0)
                  + f"<Shapes>{kids_}</Shapes>")
    want = 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 --> n2'
    out, _ = md(vsdx({"P": group + line(4, (11, 11), (13, 11))}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == want, t
    inner = group.replace("</Shapes>", line(4, (1, 1), (3, 1)) + "</Shapes>")
    out, _ = md(vsdx({"P": inner}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == want, t
    # the same ends in the group's own units land on nothing: unglued and arrowless, a decoration
    out, ctx = md(vsdx({"P": group + line(4, (1, 1), (3, 1))}))
    assert "```mermaid" not in out and not ctx.skipped["connector_loose"], (out, ctx.skipped)


def test_box_geometry():
    c = {"PinX": "2", "PinY": "3", "Width": "4", "Height": "2", "LocPinX": "1", "LocPinY": "1"}
    assert VX._box(c, 10, 20) == (11, 22, 15, 24), VX._box(c, 10, 20)
    # FlipX mirrors the box about the pin: LocPinX 1 of 4 -> 3 left of the pin, 1 right
    assert VX._box(dict(c, FlipX="1"), 10, 20) == (9, 22, 13, 24), VX._box(dict(c, FlipX="1"), 10, 20)
    assert VX._box(dict(c, FlipY="1"), 10, 20) == (11, 22, 15, 24)
    # a quarter turn about the pin (12, 23): the box (11, 22)-(15, 24) turns to (11, 22)-(13, 26)
    got = VX._box(dict(c, Angle=str(math.pi / 2)), 10, 20)
    assert all(abs(g - w) < 1e-9 for g, w in zip(got, (11, 22, 13, 26))), got
    assert VX._box(dict(c, Width="nan"), 0, 0) is None and VX._box({"PinX": "1", "PinY": "1"}, 0, 0) is None
    assert VX._box(dict(c, LocPinX="x"), 0, 0) == (2, 2, 6, 4)  # a junk loc pin counts as 0


def test_removed_names():
    for name in ("connections", "name_of", "NAME", "CONNECTIONS", "ConnectionBudget"):
        assert not hasattr(VX, name), name


def test_vsdm_vba():
    out, ctx = md(vsdx({"P": shape(1, "t")}, extra={"visio/vbaProject.bin": b"\xcc" * 16}))
    assert "VBA macros" in markdown.unread(ctx.skipped) and "t" in out
    _, ctx = md(vsdx({"P": shape(1, "t")}))
    assert "VBA macros" not in markdown.unread(ctx.skipped)


# ---- hostile masters and connections (in memory; nothing written)
def parse_mem(data):
    ctx = Context()
    return VX.parse(data, ctx), ctx


def page_text(doc):
    return "".join(t.text for sec in doc.blocks for b in sec.blocks for t in getattr(b, "inlines", [])
                   if isinstance(t, Text))


def test_shared_master_loaded_once():
    loads = []
    real = VX.ooxml_base.load

    def counting(pkg, part):
        loads.append(part)
        return real(pkg, part)
    page = "".join(shape(i, None, (0, i), "", 'Master="1"') for i in range(1, 2001))
    good = vsdx({"P": page}, masters={"1": shape(1, "M")}, mem=True)
    bad = vsdx({"P": page}, masters={"1": shape(1, "M")}, mem=True, extra={"visio/masters/master1.xml": "<Master"})
    VX.ooxml_base.load = counting
    try:
        doc, ctx = parse_mem(good)
        n_good = loads.count("visio/masters/master1.xml")
        loads.clear()
        doc2, ctx2 = parse_mem(bad)
        n_bad = loads.count("visio/masters/master1.xml")
    finally:
        VX.ooxml_base.load = real
    assert page_text(doc) == "M" * 2000 and not ctx.skipped["stencil"] and n_good == 1, (n_good, ctx.skipped)
    assert ctx2.skipped["stencil"] == 2000 and not page_text(doc2) and n_bad == 1, (n_bad, ctx2.skipped)


def test_inherited_text_charged():
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 25
    try:
        page = "".join(shape(i, None, (0, 10 - i), "", 'Master="1"') for i in range(1, 6))
        doc, ctx = parse_mem(vsdx({"P": page}, masters={"1": shape(1, "0123456789")}, mem=True))
    finally:
        limits.MAX_OUTPUT = old
    assert page_text(doc) == "0123456789" * 2 and ctx.skipped["stencil"] == 3, (page_text(doc), ctx.skipped)


def test_master_refs_do_not_loop():
    # master shapes naming their own master, unknown masters and shape ids, then no masters part at all
    m = shape(1, None, (), "", 'Master="1" MasterShape="1"') + shape(2, None, (), "", 'Master="1"')
    page = (shape(1, None, (0, 3), "", 'Master="1"') + shape(2, None, (0, 2), "", 'Master="1" MasterShape="2"')
            + shape(3, None, (0, 1), "", 'Master="7"') + shape(4, None, (0, 0), "", 'MasterShape="5"'))
    doc, ctx = parse_mem(vsdx({"P": page}, masters={"1": m}, mem=True))
    assert not page_text(doc) and ctx.skipped["stencil"] == 1, ctx.skipped  # Master="7" is not listed
    doc, ctx = parse_mem(vsdx({"P": page}, mem=True))
    assert not page_text(doc) and ctx.skipped["stencil"] == 3, ctx.skipped


def test_deep_master_group():
    xml = shape(1, "deepest")
    for i in range(300):
        xml = shape(i + 2, None, (), f"<Shapes>{xml}</Shapes>")
    page = shape(1, None, (0, 0), "", 'Master="1" MasterShape="1"')
    t0 = time.monotonic()
    doc, ctx = parse_mem(vsdx({"P": page}, masters={"1": xml}, mem=True))
    assert time.monotonic() - t0 < 10 and not page_text(doc) and ctx.skipped["depth"] >= 1, ctx.skipped


def test_master_too_large_counted():
    # a master part, or the masters list, past ZIP_RATIO loses only the text that would be inherited from it
    big = "<MasterContents>" + " " * 3_000_000 + "</MasterContents>"
    page = shape(1, "own", (0, 9), "", 'Master="1"') + shape(2, None, (0, 8), "", 'Master="1"')
    doc, ctx = parse_mem(vsdx({"P": page}, masters={"1": shape(1, "M")}, mem=True,
                              extra={"visio/masters/master1.xml": big}))
    assert page_text(doc) == "own" and ctx.skipped["stencil"] == 1, (page_text(doc), ctx.skipped)
    listing = f'<Masters xmlns:r="{REL}" {VNS}>' + " " * 3_000_000 + "</Masters>"
    doc, ctx = parse_mem(vsdx({"P": page}, masters={"1": shape(1, "M")}, mem=True,
                              extra={"visio/masters/masters.xml": listing}))
    assert page_text(doc) == "own" and ctx.skipped["stencil"] == 1, (page_text(doc), ctx.skipped)


def inline_items(doc):
    """(inline items the renderer visits, distinct inline lists) over the doc's paragraphs."""
    paras = [b for sec in doc.blocks for b in sec.blocks if isinstance(b, Para)]
    return sum(len(p.inlines) for p in paras), len({id(p.inlines) for p in paras})


def test_line_dense_text_charged_by_lines():
    # "a\n" text makes two model objects every two characters: each line break is charged LINE characters, so a
    # room of 1,000,000 holds one copy (20,000 + 10,000 x LINE) where characters alone would hold 50
    dense = "a\n" * 10_000
    cost = len(dense) + dense.count("\n") * VX.LINE
    keep = 1_000_000 // cost
    assert 1 <= keep < 5, keep
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 1_000_000
    try:
        refs = "".join(shape(i, None, (0, i), "", 'Master="1"') for i in range(1, 51))
        doc, ctx = parse_mem(vsdx({"P": refs}, masters={"1": shape(1, dense)}, mem=True))
        own = "".join(shape(i, dense, (0, i)) for i in range(1, 51))
        doc2, ctx2 = parse_mem(vsdx({"P": own}, mem=True))
    finally:
        limits.MAX_OUTPUT = old
    items, lists = inline_items(doc)
    assert items == keep * 19_999 and lists == 1 and ctx.skipped["stencil"] == 50 - keep, (items, lists, ctx.skipped)
    items, _ = inline_items(doc2)
    assert items == keep * 19_999 and ctx2.skipped["text"] == 50 - keep, (items, ctx2.skipped)


def test_line_dense_master_memory():
    # 2,000 references to one master of "a\n" x 10,000 in a 13 KB file: measured 17 s and a 1.8 GB traced peak
    # before lines were charged
    import tracemalloc
    refs = "".join(shape(i, None, (0, i), "", 'Master="1"') for i in range(1, 2_001))
    data = vsdx({"P": refs}, masters={"1": shape(1, "a\n" * 10_000)}, mem=True)
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        ctx = Context()
        out = markdown.render(VX.parse(data, ctx), tmp() / "assets")
        took = time.perf_counter() - t0
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    kept = limits.MAX_OUTPUT // (20_000 + 10_000 * VX.LINE)  # copies the room holds
    assert out.count("\n\na\na\n") == kept and ctx.skipped["stencil"] == 2_000 - kept, (kept, ctx.skipped)
    assert peak < 200 * 2**20 and took < 20, (peak, took)


def test_phantom_connector_not_listed():
    # Connect rows naming a connector that is not a shape on the page list nothing
    shapes = shape(1, "A", (0, 9)) + shape(2, "B", (0, 1)) + shape(3)
    rows = "".join(glued(i, 1, 2) for i in range(90, 110)) + glued(3, 1, 2)
    out, _ = md(vsdx({"P": page_xml(shapes, rows)}))
    t = mermaid_of(out)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2["B"]\n  n1 --> n2', t


def test_missing_image_counted():
    fd = '<ForeignData><Rel r:id="rId9"/></ForeignData>'
    out, ctx = md(vsdx({"P": shape(1, "t", (0, 0), fd)}))
    assert "t" in out and ctx.skipped["image"] == 1


def probes():
    """(name, zip bytes) of the dearest master and connection shapes, built in memory."""
    n = 50_000
    big = "w" * VX.MAX_TEXT
    refs = "".join(shape(i, None, (0, i % 97), "", 'Master="1"') for i in range(1, n + 1))
    yield "shared big master text", vsdx({"P": refs}, masters={"1": shape(1, big)}, mem=True)
    bad = "<MasterContents>" + "<Shape>" * 100_000  # under 1 MiB, so ZIP_RATIO lets it be read
    yield "shared damaged master", vsdx({"P": refs}, masters={"1": shape(1, "x")}, mem=True,
                                        extra={"visio/masters/master1.xml": bad})
    many = {str(i): shape(1, f"m{i}") for i in range(1, 5_001)}
    refs = "".join(shape(i, None, (0, i % 97), "", f'Master="{i % 5_000 + 1}"') for i in range(1, n + 1))
    yield "5,000 masters past MASTERS", vsdx({"P": refs}, masters=many, mem=True)
    deep = shape(1, "deepest")
    for i in range(2, 2_000):
        deep = shape(i, None, (), f"<Shapes>{deep}</Shapes>")
    refs = "".join(shape(i, None, (0, i % 97), "", 'Master="1" MasterShape="1"') for i in range(1, n + 1))
    yield "deep master, missing shape", vsdx({"P": refs}, masters={"1": deep}, mem=True)
    refs = "".join(shape(i, None, (0, i), "", 'Master="1"') for i in range(1, 2_001))
    yield "line-dense master, 2,000 refs", vsdx({"P": refs}, masters={"1": shape(1, "a\n" * 10_000)}, mem=True)
    own = "".join(shape(i, "a\n" * 10_000, (0, i)) for i in range(1, 50))
    yield "line-dense own text, 20 pages x 49", vsdx({f"P{k}": own for k in range(20)}, mem=True)
    m = 30_000
    shapes = shape(1, "A" * 200) + shape(2, "B" * 200) + "".join(
        shape(i, None, (), "", 'Master="1"') for i in range(3, m + 3))
    links = "".join(glued(i, 1, 2) for i in range(3, m + 3))
    pages = {f"P{k}": page_xml(shapes, links) for k in range(2)}
    yield "60,000 labelled connectors", vsdx(pages, masters={"1": shape(1, big)}, mem=True)


def measure():
    """Not a test: parse and render each probe; prints name, zip MiB, parse s, parse and render s, s/MiB, peak MiB,
    output MiB, skipped."""
    import tracemalloc
    for name, data in probes():
        t0 = time.perf_counter()
        ctx = Context()
        doc = VX.parse(data, ctx)
        parsed = time.perf_counter() - t0
        out = markdown.render(doc, tmp() / "assets")
        took = time.perf_counter() - t0
        tracemalloc.start()
        markdown.render(VX.parse(data, Context()), tmp() / "assets")
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        mib = len(data) / 2**20
        print(f"{name}: {mib:.2f} MiB, parse {parsed:.2f} s, with render {took:.2f} s, {took / mib:.1f} s/MiB, "
              f"peak {peak / 2**20:.0f} MiB, "
              f"output {len(out) / 2**20:.1f} MiB, {dict(ctx.skipped)}")


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
