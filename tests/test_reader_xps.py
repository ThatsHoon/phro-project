"""XPS / OpenXPS parser: chain, transforms, ordering, story fragments, images, hostile input.

Run: python -I -X utf8 tests/test_reader_xps.py
"""
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_zip  # noqa: E402

PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"ms": "http://schemas.microsoft.com/xps/2005/06", "oxps": "http://schemas.openxps.org/oxps/v1.0"}
REPR = {"ms": "http://schemas.microsoft.com/xps/2005/06/fixedrepresentation",
        "oxps": "http://schemas.openxps.org/oxps/v1.0/fixedrepresentation"}
STORY = "http://schemas.microsoft.com/xps/2005/06/storyfragments"


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


PNG = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + _chunk(b"IEND", b""))


def tmp():
    return Path(tempfile.mkdtemp())


def g(text, x=0, y=0, size=12, extra=""):
    t = f' UnicodeString="{text}"' if text is not None else ""
    return f'<Glyphs OriginX="{x}" OriginY="{y}" FontRenderingEmSize="{size}"{t} {extra}/>'


def page(body, ns="ms", width=600):
    return f'<FixedPage xmlns="{NS[ns]}" Width="{width}" Height="800">{body}</FixedPage>'


def rels(items):
    return f'<Relationships xmlns="{PKG}">' + "".join(
        f'<Relationship Id="r{i}" Type="{t}" Target="{tg}"/>' for i, (t, tg) in enumerate(items)) + "</Relationships>"


def xps_doc(pages, ns="ms", story=None, extra=None, order=None, ext="xps"):
    """pages: FixedPage xml strings (raw bodies are wrapped); order: page indexes listed in the fdoc."""
    order = order if order is not None else range(len(pages))
    members = {
        "_rels/.rels": rels([(REPR[ns], "/FixedDocSeq.fdseq")]),
        "FixedDocSeq.fdseq": (f'<FixedDocumentSequence xmlns="{NS[ns]}">'
                              '<DocumentReference Source="/Documents/1/FixedDoc.fdoc"/></FixedDocumentSequence>'),
        "Documents/1/FixedDoc.fdoc": f'<FixedDocument xmlns="{NS[ns]}">' + "".join(
            f'<PageContent Source="Pages/{i + 1}.fpage"/>' for i in order) + "</FixedDocument>",
    }
    for i, p in enumerate(pages):
        members[f"Documents/1/Pages/{i + 1}.fpage"] = p if p.startswith(("<FixedPage ", "<FixedPage>")) else page(p, ns)
    if story is not None:
        members["Documents/1/_rels/FixedDoc.fdoc.rels"] = rels([(STORY, "Structure/Story.xml")])
        members["Documents/1/Structure/Story.xml"] = story
    members.update(extra or {})
    return make_zip(tmp() / f"a.{ext}", members, stored=())


def md(path):
    ctx = Context()
    doc = reader.parse(path, ctx)
    return markdown.render(doc, tmp() / "assets"), ctx, doc


def text(path):
    return md(path)[0].strip()


def fails(path):
    try:
        reader.parse(path, Context())
    except ReadError as e:
        return e
    raise AssertionError("no error")


def test_chain_and_unicode_escape():
    assert text(xps_doc([g("{}{braces}", 10, 100)])) == "{braces}"
    assert text(xps_doc([g("a{}b", 10, 100)])) == "a{}b"  # the prefix counts only at the start


def test_oxps_namespace():
    body = g("가", 10, 100) + g("나", 200, 100)
    assert text(xps_doc([body], ns="oxps")) == text(xps_doc([body])) == "가 나"


def test_oxps_extension_converts():
    p = xps_doc([g("가", 10, 100)], ns="oxps", ext="oxps")
    out = tmp() / "o"
    reader.convert(p, out)
    assert (out / "out.md").read_text(encoding="utf-8").strip() == "가"


def test_lines_and_paragraphs():
    body = g("가", 10, 100) + g("나", 200, 100) + g("다", 10, 120) + g("라", 10, 300)
    assert text(xps_doc([body])) == "가 나 다\n\n라"


def test_line_tolerance_orders_by_x():
    assert text(xps_doc([g("B", 200, 99.8) + g("A", 10, 100)])) == "A B"


def test_render_transform():
    body = g("A", 10, 300) + '<Canvas RenderTransform="1,0,0,1,0,500">' + g("B", 10, 0) + "</Canvas>"
    assert text(xps_doc([body])) == "A\n\nB"
    flat = g("A", 10, 300) + "<Canvas>" + g("B", 10, 0) + "</Canvas>"
    assert text(xps_doc([flat])) == "B\n\nA"


def test_transform_forms():
    prop = ('<Canvas><Canvas.RenderTransform><MatrixTransform Matrix="1 0 0 1 0 500"/></Canvas.RenderTransform>'
            + g("B", 10, 0) + "</Canvas>")
    assert text(xps_doc([g("A", 10, 300) + prop])) == "A\n\nB"
    res = ('<FixedPage.Resources><ResourceDictionary><MatrixTransform x:Key="m" Matrix="1,0,0,1,0,500" '
           'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"/></ResourceDictionary></FixedPage.Resources>')
    ref = '<Canvas RenderTransform="{StaticResource m}">' + g("B", 10, 0) + "</Canvas>"
    assert text(xps_doc([res + g("A", 10, 300) + ref])) == "A\n\nB"


def test_bad_transforms_are_identity():
    for bad in ("1,0,0,1,0", "a,b,c,d,e,f", "nan,0,0,1,0,0", "inf,0,0,1,0,0", "1e300,0,0,1,0,0", "",
                "{StaticResource nope}"):
        body = g("A", 10, 300) + f'<Canvas RenderTransform="{bad}">' + g("B", 10, 0) + "</Canvas>"
        out, ctx, _ = md(xps_doc([body]))
        assert out.strip() == "B\n\nA", bad
        assert ctx.skipped["transform"] >= 1, bad


def test_huge_composed_transform():
    n = 10
    body = '<Canvas RenderTransform="1e9,0,0,1e9,0,0">' * n + g("A", 1, 1) + "</Canvas>" * n
    out, ctx, _ = md(xps_doc([body]))
    assert out.strip() == "A" and ctx.skipped["transform"] >= 1


def test_bad_origin_and_size():
    body = ('<Glyphs OriginX="x" OriginY="nan" FontRenderingEmSize="-5" UnicodeString="A"/>'
            '<Glyphs UnicodeString="B"/>' + g("C", 10, 0, size=0) + g("D", 20, 0, size="inf"))
    assert text(xps_doc([body])) == "A B C D"


def test_two_columns():
    body = "".join(g(f"L{i}", 50, 100 + 30 * i) + g(f"R{i}", 400, 100 + 30 * i) for i in range(3))
    out = text(xps_doc([body]))
    assert out.index("L2") < out.index("R0")
    crossing = body + g("X" * 40, 100, 400)  # a run reaching the middle keeps the page one flow
    out = text(xps_doc([crossing]))
    assert out.index("R0") < out.index("L1")


def test_story_fragments_order():
    body = g("one", 10, 100, extra='Name="g1"') + g("two", 10, 300, extra='Name="g2"') + g("rest", 10, 500)
    story = ('<StoryFragments xmlns="http://schemas.microsoft.com/xps/2005/06/documentstructure"><StoryFragment>'
             '<ParagraphStructure><NamedElement NameReference="g2"/><NamedElement NameReference="gone"/></ParagraphStructure>'
             '<ParagraphStructure><NamedElement NameReference="g1"/><NamedElement NameReference="g2"/></ParagraphStructure>'
             "</StoryFragment></StoryFragments>")
    assert text(xps_doc([body], story=story)) == "two\n\none\n\nrest"


def test_story_outside_package_ignored():
    p = xps_doc([g("one", 10, 100, extra='Name="g1"')],
                extra={"Documents/1/_rels/FixedDoc.fdoc.rels": rels([(STORY, "../../../x.xml")])})
    assert text(p) == "one"


def test_image_brush():
    body = '<Path><Path.Fill><ImageBrush ImageSource="/Resources/a.png"/></Path.Fill></Path>' + g("t", 10, 10)
    out = text(xps_doc([body], extra={"Resources/a.png": PNG}))
    assert "![](assets/img-001.png)" in out and "t" in out


def test_image_hostile():
    def brush(s):
        return f'<Path><Path.Fill><ImageBrush ImageSource="{s}"/></Path.Fill></Path>'
    body = "".join(brush(s) for s in ("../../x.png", "/Resources/missing.png", "/Resources/junk.bin", "..\\x.png"))
    out, ctx, _ = md(xps_doc([body + g("t", 1, 1)], extra={"Resources/junk.bin": b"not an image"}))
    assert out.strip() == "t" and ctx.skipped["image"] == 4


def test_image_shared_across_pages():
    body = '<Path><Path.Fill><ImageBrush ImageSource="/Resources/a.png"/></Path.Fill></Path>'
    doc = reader.parse(xps_doc([body] * 5, extra={"Resources/a.png": PNG}), Context())
    out = tmp() / "assets"
    markdown.render(doc, out)
    assert len(list(out.iterdir())) == 1


def test_glyphs_without_unicode():
    out, ctx, doc = md(xps_doc([g(None, 1, 1) + g("ok", 1, 50)]))
    assert out.strip() == "ok" and "1 text runs without Unicode" in doc.warnings


def test_xps_not_a_package():
    for members in ({"[Content_Types].xml": "<Types/>", "_rels/.rels": rels([("http://x/signature", "/sig.psdsxs")])},
                    {"a.txt": "x"}):
        e = fails(make_zip(tmp() / "n.xps", members, stored=()))
        assert e.kind == "damaged" and "not an XPS" in e.detail


def test_duplicate_pages_and_documents():
    doc = reader.parse(xps_doc([g("A", 1, 1), g("B", 1, 1)], order=[0, 1] * 500 + [0]), Context())
    assert len(doc.blocks) == 2
    members = {
        "_rels/.rels": rels([(REPR["ms"], "/S.fdseq")]),
        "S.fdseq": "<FixedDocumentSequence>" + '<DocumentReference Source="/D.fdoc"/>' * 300 + "</FixedDocumentSequence>",
        "D.fdoc": '<FixedDocument><PageContent Source="/P.fpage"/></FixedDocument>',
        "P.fpage": page(g("A", 1, 1)),
    }
    assert len(reader.parse(make_zip(tmp() / "d.xps", members, stored=()), Context()).blocks) == 1


def test_damaged_pages():
    p = xps_doc([g("A", 1, 1), g("B", 1, 1), g("C", 1, 1)], extra={
        "Documents/1/Pages/2.fpage": "<FixedPage><broken",
        "Documents/1/Pages/3.fpage": "<!DOCTYPE x [<!ENTITY a 'b'>]><FixedPage/>"})
    out, ctx, _ = md(p)
    assert out.strip() == "A" and ctx.skipped["page"] == 2
    bad = {f"Documents/1/Pages/{i}.fpage": "<bad" for i in (1, 2, 3)}
    assert fails(xps_doc([g("A", 1, 1)] * 3, extra=bad)).kind == "damaged"
    out, ctx, _ = md(xps_doc([g("A", 1, 1)], order=[0, 5]))  # a listed page that is not there
    assert out.strip() == "A" and ctx.skipped["page"] == 1


def test_deep_canvas():
    n = 5000
    body = "<Canvas>" * n + g("deep", 1, 1) + "</Canvas>" * n + g("top", 1, 100)
    t0 = time.monotonic()
    out, ctx, _ = md(xps_doc([body]))
    assert "top" in out and ctx.skipped["depth"] >= 1 and time.monotonic() - t0 < 10


def test_many_glyphs_fast():
    n = 200_000
    body = "".join(f'<Glyphs OriginX="{i % 500}" OriginY="{(i * 7) % 90000}" FontRenderingEmSize="1" UnicodeString="w"/>'
                   for i in range(n))
    p = xps_doc([body])
    t0 = time.monotonic()
    out, _, _ = md(p)
    assert out.count("w") == n and time.monotonic() - t0 < 30


def test_long_run_capped():
    assert len(text(xps_doc([g("x" * 300_000, 1, 1)]))) == 100_000


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
