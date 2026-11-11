"""Visio (vsdx) parser: page order, text markers, groups, images, hostile input.

Run: python -I -X utf8 tests/test_reader_vsdx.py
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


def page_xml(shapes):
    return f'<PageContents xmlns:r="{REL}" {VNS}><Shapes>{shapes}</Shapes></PageContents>'


def vsdx(pages, listed=None, extra=None, page_rels=None):
    """pages: {name: shapes xml (or full xml)}, stored as page1.xml..; listed: [(Name, index)] for pages.xml."""
    names = list(pages)
    listed = listed if listed is not None else [(n, i + 1) for i, n in enumerate(names)]
    members = {
        "_rels/.rels": rels_xml([("rId1", "document", "visio/document.xml")]),
        "visio/document.xml": f"<VisioDocument {VNS}/>",
        "visio/_rels/document.xml.rels": rels_xml([("rId1", "pages", "pages/pages.xml")]),
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
    members.update(extra or {})
    return make_zip(tmp() / "a.vsdx", members, stored=())


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
    p = vsdx({"P": shape(1, "a\nb") + shape(2, "   ") + shape(3, None)})
    out, _ = md(p)
    assert "a" in out and "b" in out and out.count("\n\n") <= 3


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


def test_shared_media_and_master_ignored():
    fd = '<ForeignData ForeignType="Bitmap"><Rel r:id="rId1"/></ForeignData>'
    many = "".join(shape(i, None, (i, 0), fd) for i in range(1, 300))
    many += shape(999, None, (0, 9), "", 'Master="2" MasterShape="3"')
    p = vsdx({"P": many}, extra={"visio/media/image1.png": PNG},
             page_rels={1: [("rId1", "image", "../media/image1.png")]})
    out, ctx = md(p)
    assert out.count("![](") == 299 and not ctx.skipped["image"]


def test_missing_image_counted():
    fd = '<ForeignData><Rel r:id="rId9"/></ForeignData>'
    out, ctx = md(vsdx({"P": shape(1, "t", (0, 0), fd)}))
    assert "t" in out and ctx.skipped["image"] == 1


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
