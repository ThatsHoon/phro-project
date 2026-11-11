"""ODT parser and the shared ODF layer: text, styles, lists, tables, notes, hidden, objects, hostile input.

Run: python -I -X utf8 tests/test_reader_odt.py
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
from reader.containers.zipx import Package, parse_xml  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.formats import odf, odf_base  # noqa: E402
from reader_fix import make_zip  # noqa: E402

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" '
      'xmlns:dc="http://purl.org/dc/elements/1.1/" '
      'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0" '
      'xmlns:math="http://www.w3.org/1998/Math/MathML" '
      'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"')
MIME = "application/vnd.oasis.opendocument.text"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
NUM = ('<text:list-style style:name="L1"><text:list-level-style-number text:level="1" '
       'style:num-format="1" style:num-suffix="."/></text:list-style>')


def tmp():
    return Path(tempfile.mkdtemp())


def odt_doc(body_xml, auto_styles="", styles_xml="", extra=None, mime=MIME, masters=""):
    members = {
        "mimetype": mime,
        "content.xml": (f'<office:document-content {NS}><office:automatic-styles>{auto_styles}'
                        f'</office:automatic-styles><office:body><office:text>{body_xml}'
                        '</office:text></office:body></office:document-content>'),
        "styles.xml": (f'<office:document-styles {NS}><office:styles>{styles_xml}</office:styles>'
                       f'<office:master-styles>{masters}</office:master-styles></office:document-styles>'),
        "META-INF/manifest.xml": f'<manifest:manifest {NS}/>',
    }
    members.update(extra or {})
    if mime is None:
        del members["mimetype"]
    return make_zip(tmp() / "a.odt", members)


def P(text):
    return f"<text:p>{text}</text:p>"


def parse(path):
    ctx = Context()
    return odf.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, _ = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden)


def table_xml(rows):
    return "<table:table>" + "".join(f"<table:table-row>{r}</table:table-row>" for r in rows) + "</table:table>"


def cell(text="", extra=""):
    return f"<table:table-cell {extra}>{P(text) if text else ''}</table:table-cell>"


def frame(inner, extra=""):
    return f"<draw:frame {extra}>{inner}</draw:frame>"


def test_registered():
    assert "odt" in reader.BUILT and reader.FORMATS["odt"] == "odf"


def test_heading_level():
    assert md(odt_doc('<text:h text:outline-level="2">T</text:h>')) == "## T\n"
    assert md(odt_doc("<text:h>T</text:h>")) == "# T\n"
    assert md(odt_doc('<text:h text:outline-level="99">T</text:h>')) == "###### T\n"


def test_bold_via_parent_chain():
    auto = '<style:style style:name="T1" style:family="text" style:parent-style-name="Strong"/>'
    sty = ('<style:style style:name="Strong" style:family="text"><style:text-properties '
           'fo:font-weight="700"/></style:style>')
    body = P('a <text:span text:style-name="T1">x</text:span>')
    assert md(odt_doc(body, auto, sty)) == "a **x**\n"
    light = ('<style:style style:name="Strong" style:family="text"><style:text-properties '
             'fo:font-weight="400"/></style:style>')
    assert md(odt_doc(body, auto, light)) == "a x\n"


def test_styles_chain_cycle_and_defaults():
    xml = (f'<office:document-styles {NS}><office:styles>'
           '<style:default-style style:family="text"><style:text-properties fo:font-weight="bold"/>'
           '</style:default-style>'
           '<style:style style:name="A" style:family="text" style:parent-style-name="B"/>'
           '<style:style style:name="B" style:family="text" style:parent-style-name="A"/>'
           '</office:styles></office:document-styles>')
    s = odf_base.Styles(None, parse_xml(xml.encode()))
    assert s.prop("A", "text", "font-weight") == "bold"  # cycle ends, family default answers
    assert s.prop("A", "text", "font-style") is None
    assert s.prop("missing", "text", "font-weight") == "bold"


def test_numbered_list_continue():
    body = ('<text:list text:style-name="L1"><text:list-item>' + P("a") + '</text:list-item>'
            '<text:list-item>' + P("b") + '</text:list-item></text:list>'
            '<text:list text:style-name="L1" text:continue-numbering="true"><text:list-item>'
            + P("c") + '</text:list-item></text:list>')
    assert md(odt_doc(body, NUM)) == "1. a\n2. b\n3. c\n"


def test_list_formats_start_header_and_bullet():
    st = ('<text:list-style style:name="R"><text:list-level-style-number text:level="1" style:num-format="I" '
          'style:num-prefix="(" style:num-suffix=")" text:start-value="4"/>'
          '<text:list-level-style-number text:level="2" style:num-format="a"/></text:list-style>'
          '<text:list-style style:name="B"><text:list-level-style-bullet text:level="1"/></text:list-style>')
    body = ('<text:list text:style-name="R"><text:list-header>' + P("제목") + '</text:list-header>'
            '<text:list-item>' + P("x") + '<text:list><text:list-item>' + P("y") + '</text:list-item>'
            '<text:list-item>' + P("z") + '</text:list-item></text:list></text:list-item>'
            '<text:list-item text:start-value="9">' + P("w") + '</text:list-item></text:list>'
            '<text:list text:style-name="B"><text:list-item>' + P("u") + '</text:list-item></text:list>')
    assert md(odt_doc(body, st)) == "제목\n\n(IV) x\n  a. y\n  b. z\n(IX) w\n- u\n"


def test_covered_cells_span():
    body = table_xml([cell("A", 'table:number-columns-spanned="2"') + "<table:covered-table-cell/>",
                      cell("x") + cell("y")])
    out = md(odt_doc(body))
    assert '<td colspan="2">A</td>' in out and out.count("<td") == 3


def test_header_rows_and_repeated_cells():
    body = ('<table:table><table:table-header-rows><table:table-row>' + cell("h1") + cell("h2")
            + '</table:table-row></table:table-header-rows><table:table-row>'
            + cell("v", 'table:number-columns-repeated="2"') + '</table:table-row></table:table>')
    doc, _ = parse(odt_doc(body))
    t = doc.blocks[0]
    assert [c.header for c in t.rows[0]] == [True, True] and [c.header for c in t.rows[1]] == [False, False]
    assert md(odt_doc(body)).splitlines()[2] == "|v|v|"


def test_footnote_and_comment():
    body = P('글<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation>'
             '<text:note-body>' + P("각주") + '</text:note-body></text:note>다'
             '<office:annotation><dc:creator>홍</dc:creator><dc:date>2026-01-01</dc:date>' + P("확인")
             + '</office:annotation>라<office:annotation-end/>')
    out = md(odt_doc(body))
    assert "글[^1]다[^2]라" in out
    assert "[^1]: 각주" in out and "[^2]: Comment by 홍: 확인" in out


def test_endnote():
    body = P('a<text:note text:note-class="endnote"><text:note-body>' + P("끝") + '</text:note-body></text:note>')
    doc, _ = parse(odt_doc(body))
    assert "en-1" in doc.notes


def test_hidden_section_and_text():
    body = ('<text:section text:display="none">' + P("숨김절") + '</text:section>'
            + P('보임<text:hidden-text text:string-value="숨김글">숨김글</text:hidden-text>'))
    p = odt_doc(body)
    out = md(p)
    assert out.startswith("(2 hidden places left out)") and "숨김" not in out and "보임" in out
    shown = md(p, hidden=True)
    assert "숨김절" in shown and "(hidden: 숨김글)" in shown


def test_hidden_paragraph():
    out = md(odt_doc('<text:hidden-paragraph text:condition="x">c</text:hidden-paragraph>'.join(["<text:p>", "숨김</text:p>"])
                     + P("보임")))
    assert "(1 hidden places left out)" in out and "숨김" not in out


def test_tracked_deletion():
    body = ('<text:tracked-changes><text:changed-region><text:deletion><office:change-info>'
            '<dc:creator>kim</dc:creator></office:change-info>' + P("옛말") + '</text:deletion>'
            '</text:changed-region></text:tracked-changes>' + P("새말"))
    p = odt_doc(body)
    out = md(p)
    assert "(1 hidden places left out)" in out and "옛말" not in out and "kim" not in out and "새말" in out
    assert "(hidden: 옛말)" in md(p, hidden=True)


def test_page_fields_dropped():
    assert md(odt_doc(P("쪽 <text:page-number>3</text:page-number>"))) == "쪽\n"
    assert md(odt_doc(P("총 <text:page-count>9</text:page-count>쪽"))) == "총 쪽\n"


def test_link_and_whitespace():
    body = P('<text:a xlink:href="http://x.test/">홈</text:a> a   b<text:s text:c="2"/>c<text:tab/>d')
    assert md(odt_doc(body)) == "[홈](http://x.test/) a b  c\td\n"


def test_image_alt():
    body = P(frame('<draw:image xlink:href="Pictures/a.png"/><svg:title>로고</svg:title><svg:desc>설명</svg:desc>'))
    assert md(odt_doc(body, extra={"Pictures/a.png": PNG})) == "![로고](assets/img-001.png)\n"
    body = P(frame('<draw:image xlink:href="./Pictures/a.png"/><svg:desc>설명</svg:desc>'))
    assert md(odt_doc(body, extra={"Pictures/a.png": PNG})) == "![설명](assets/img-001.png)\n"


def chart_xml(extra_ns="", title="매출", cls="chart:bar"):
    return (f'<office:document-content {NS} {extra_ns}><office:body><office:chart>'
            f'<chart:chart chart:class="{cls}"><chart:title>{P(title)}</chart:title>'
            + table_xml([cell("연도") + cell("값"), cell("2025") + cell("3")])
            + '</chart:chart></office:chart></office:body></office:document-content>')


def test_chart_object():
    body = P(frame('<draw:object xlink:href="./Object 1"/>'))
    out = md(odt_doc(body, extra={"Object 1/content.xml": chart_xml()}))
    assert "Chart: bar — 매출" in out and "|연도|값|" in out and "|2025|3|" in out


def test_formula_object():
    math = (f'<math:math {NS}><math:semantics><math:mfrac><math:mi>a</math:mi><math:mi>b</math:mi></math:mfrac>'
            '<math:annotation math:encoding="StarMath 5.0">a over b</math:annotation></math:semantics></math:math>')
    body = frame('<draw:object xlink:href="./Object 2"/>')
    assert md(odt_doc(body, extra={"Object 2/content.xml": math})) == "$$a over b$$\n"
    plain = f'<math:math {NS}><math:mi>x</math:mi><math:mo>+</math:mo><math:mn>1</math:mn></math:math>'
    assert md(odt_doc(body, extra={"Object 2/content.xml": plain})) == "$$x + 1$$\n"


def test_charts_xmlns_math_not_formula():
    body = frame('<draw:object xlink:href="./Object 1"/>')
    xml = chart_xml()  # NS declares xmlns:math on the root, as LibreOffice charts do
    assert 'xmlns:math="http://www.w3.org/1998/Math/MathML"' in xml
    out = md(odt_doc(body, extra={"Object 1/content.xml": xml}))
    assert "Chart: bar" in out and "$$" not in out


def test_headers_and_footers():
    m = ('<style:master-page style:name="Standard"><style:header>' + P("머리글")
         + '</style:header><style:footer>' + P('바닥글 <text:page-number/>') + '</style:footer></style:master-page>')
    out = md(odt_doc(P("본문"), masters=m))
    assert "Header: 머리글" in out and "Footer: 바닥글" in out


def test_encrypted():
    man = f'<manifest:manifest {NS}><manifest:file-entry><manifest:encryption-data/></manifest:file-entry></manifest:manifest>'
    try:
        parse(odt_doc(P("x"), extra={"META-INF/manifest.xml": man}))
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "encrypted"


def test_odt_not_a_package():
    try:
        parse(make_zip(tmp() / "n.odt", {"mimetype": MIME}))
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged" and "not an OpenDocument package" in e.detail


def test_dispatch_by_mimetype_and_body():
    for mime in ("application/vnd.oasis.opendocument.spreadsheet", "application/vnd.oasis.opendocument.presentation"):
        try:
            parse(odt_doc(P("x"), mime=mime))
            raise AssertionError("expected ReadError")
        except ReadError as e:
            assert e.kind == "unsupported"
    assert md(odt_doc(P("본"), mime=None)) == "본\n"  # no mimetype: root body child decides
    assert md(odt_doc(P("본"), mime=MIME + "-template")) == "본\n"
    ods = odt_doc(P("x"), mime=None, extra={
        "content.xml": f'<office:document-content {NS}><office:body><office:spreadsheet/></office:body></office:document-content>'})
    try:
        parse(ods)
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "unsupported"


def test_para_inlines_public():
    root = parse_xml(f'<text:p {NS}>가<text:span text:style-name="S">나</text:span></text:p>'.encode())
    sty = odf_base.Styles(parse_xml(
        f'<office:document-content {NS}><office:automatic-styles><style:style style:name="S" style:family="text">'
        '<style:text-properties fo:font-weight="bold"/></style:style></office:automatic-styles>'
        '</office:document-content>'.encode()), None)
    inl = odf_base.para_inlines(root, sty, None, Context())
    assert [type(i).__name__ for i in inl] == ["Text", "Bold"]


# ---- hostile input
def test_damaged_parts_lose_only_themselves():
    p = odt_doc(P("본문"), extra={"styles.xml": "<office:document-styles"})
    doc, ctx = parse(p)
    assert ctx.skipped["styles"] == 1 and markdown.render(doc, tmp() / "a") == "본문\n"
    doctype = '<!DOCTYPE x [<!ENTITY a "b">]><office:document-styles ' + NS + "/>"
    doc, ctx = parse(odt_doc(P("본문"), extra={"styles.xml": doctype}))
    assert ctx.skipped["styles"] == 1
    body = P(frame('<draw:object xlink:href="./Object 1"/>')) + P("뒤")
    doc, ctx = parse(odt_doc(body, extra={"Object 1/content.xml": "<broken"}))
    assert ctx.skipped["object"] == 1 and markdown.render(doc, tmp() / "a") == "뒤\n"
    try:
        parse(odt_doc(P("x"), extra={"content.xml": "<broken"}))
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged" and "no readable" in e.detail


def test_duplicate_references_read_once():
    body = P(frame('<draw:image xlink:href="Pictures/a.png"/>') + frame('<draw:image xlink:href="Pictures/a.png"/>')
             + frame('<draw:object xlink:href="./Object 1"/>') + frame('<draw:object xlink:href="Object 1"/>'))
    p = odt_doc(body, extra={"Pictures/a.png": PNG, "Object 1/content.xml": chart_xml()})
    reads = []
    orig = Package.read

    def spy(self, name):
        reads.append(name)
        return orig(self, name)
    Package.read = spy
    try:
        out = md(p)
    finally:
        Package.read = orig
    assert reads.count("Pictures/a.png") == 1 and reads.count("Object 1/content.xml") == 1
    assert out.count("Chart: bar") == 2 and out.count("![](") == 2


def test_hostile_repeats_and_spans():
    huge = "1000000000"
    rows = [cell("a") + cell("", f'table:number-columns-repeated="{huge}"'),
            cell("b", f'table:number-columns-repeated="{huge}"'),
            cell("c", f'table:number-columns-spanned="{huge}" table:number-rows-spanned="{huge}"'),
            cell("d", f'table:number-columns-repeated="2"')]
    body = ('<table:table>' + "".join(f"<table:table-row>{r}</table:table-row>" for r in rows)
            + f'<table:table-row table:number-rows-repeated="{huge}">{cell("e")}</table:table-row>'
            + f'<table:table-row table:number-rows-repeated="{huge}">{cell("")}</table:table-row></table:table>'
            + f'<text:p>x<text:s text:c="{huge}"/>y</text:p>')
    t0 = time.monotonic()
    doc, ctx = parse(odt_doc(body))
    out = markdown.render(doc, tmp() / "a")
    assert time.monotonic() - t0 < 5
    t = doc.blocks[0]
    assert len(t.rows[0]) == 1  # the repeated empty trailing cell is dropped
    assert len(t.rows[1]) == 100 and ctx.skipped["repeat"] >= 2  # non-empty repeat capped and counted
    assert all(c.colspan <= 1000 and c.rowspan <= len(t.rows) for r in t.rows for c in r)
    assert len(t.rows) == 4 + 100  # e repeated is capped, the empty trailing repeat is dropped
    assert "a" in out and "x" in out and len(out) < 200_000


def test_nested_repeat_bomb_is_budgeted():
    xml = "<text:p>leaf</text:p>"
    for _ in range(3):  # each level repeats its cell and its row 100 times around the level below
        xml = ('<table:table><table:table-row table:number-rows-repeated="100">'
               f'<table:table-cell table:number-columns-repeated="100"><text:p>첫</text:p>{xml}'
               '</table:table-cell></table:table-row></table:table>')
    t0 = time.monotonic()
    doc, ctx = parse(odt_doc(xml))
    out = markdown.render(doc, tmp() / "a")
    took = time.monotonic() - t0
    assert took < 2 and ctx.skipped["cells"] > 0 and "첫" in out and "leaf" in out, (took, dict(ctx.skipped), len(out))


def test_object_chain_bomb_is_budgeted():
    n = 30
    members = {}
    for k in range(1, n + 1):
        ref = frame(f'<draw:object xlink:href="./Object {k + 1}"/>') * 2 if k < n else P("끝")
        members[f"Object {k}/content.xml"] = (
            f'<office:document-content {NS}><office:body><office:chart><chart:chart chart:class="chart:bar">'
            + table_xml([f"<table:table-cell>{P('첫' + str(k))}{ref}</table:table-cell>"])
            + '</chart:chart></office:chart></office:body></office:document-content>')
    t0 = time.monotonic()
    doc, ctx = parse(odt_doc(frame('<draw:object xlink:href="./Object 1"/>'), extra=members))
    out = markdown.render(doc, tmp() / "a")
    assert time.monotonic() - t0 < 2 and ctx.skipped["cells"] > 0 and "첫1" in out


def test_hidden_section_is_one_place():
    body = '<text:section text:display="none">' + P("하나") + P("둘") + P("셋") + "</text:section>" + P("보임")
    p = odt_doc(body)
    assert md(p).startswith("(1 hidden places left out)") and "하나" not in md(p)
    shown = md(p, hidden=True)
    assert "(hidden) 하나" in shown and "(hidden) 셋" in shown


def test_hidden_copy_does_not_touch_cached_object():
    chart = frame('<draw:object xlink:href="./Object 1"/>')
    body = chart + '<text:section text:display="none">' + chart + "</text:section>"
    out = md(odt_doc(body, extra={"Object 1/content.xml": chart_xml()}))
    assert out.count("Chart: bar") == 1 and "(1 hidden places left out)" in out


def test_nested_bold_is_flat():
    auto = '<style:style style:name="B" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>'
    body = P('<text:span text:style-name="B">a<text:span text:style-name="B">b</text:span>c</text:span>')
    assert md(odt_doc(body, auto)) == "**abc**" + chr(10)


def test_list_item_not_opening_with_paragraph_takes_no_number():
    body = ('<text:list text:style-name="L1"><text:list-item><text:h>제목</text:h></text:list-item>'
            '<text:list-item>' + P("a") + '</text:list-item></text:list>')
    assert md(odt_doc(body, NUM)) == "# 제목" + chr(10) * 2 + "1. a" + chr(10)


def test_trailing_repeats_are_not_counted_as_cut():
    huge = "1000000000"
    body = ('<table:table><table:table-row>' + cell("a") + cell("", f'table:number-columns-repeated="{huge}"')
            + f'</table:table-row><table:table-row table:number-rows-repeated="{huge}">'
            + cell("") + '</table:table-row></table:table>')
    _, ctx = parse(odt_doc(body))
    assert ctx.skipped["repeat"] == 0
    gap = table_xml([cell("a") + cell("", f'table:number-columns-repeated="{huge}"') + cell("b")])
    _, ctx = parse(odt_doc(gap))
    assert ctx.skipped["repeat"] == 1


def test_empty_trailing_repeats_vanish():
    huge = "1000000000"
    body = ('<table:table><table:table-row>' + cell("a") + cell("", f'table:number-columns-repeated="{huge}"')
            + f'</table:table-row><table:table-row table:number-rows-repeated="{huge}">'
            + cell("", f'table:number-columns-repeated="{huge}"') + '</table:table-row></table:table>')
    doc, _ = parse(odt_doc(body))
    assert [len(r) for r in doc.blocks[0].rows] == [1]


def nest(open_tag, close_tag, n, inner):
    return open_tag * n + inner + close_tag * n


def test_deep_nesting_is_capped():
    inner = P("끝")
    cases = {
        "lists": nest('<text:list><text:list-item>', "</text:list-item></text:list>", 300, inner),
        "tables": nest('<table:table><table:table-row><table:table-cell>', "</table:table-cell></table:table-row></table:table>",
                       300, inner),
        "spans": P(nest("<text:span>", "</text:span>", 400, "끝")),
        "sections": nest("<text:section>", "</text:section>", 300, inner),
        "boxes": nest('<text:p><draw:frame><draw:text-box>', "</draw:text-box></draw:frame></text:p>", 300, inner),
    }
    for name, xml in cases.items():
        t0 = time.monotonic()
        doc, ctx = parse(odt_doc(xml + P("뒤")))
        out = markdown.render(doc, tmp() / "a")
        assert time.monotonic() - t0 < 5, name
        assert "뒤" in out and ctx.skipped["depth"] >= 1, name


def test_hostile_targets_are_missing_not_read():
    imgs = ["../x.png", "/Pictures/a.png", "Pictures\\a.png", "http://x.test/a.png", "Pictures/%2e%2e/%2e%2e/a.png",
            "Pictures/none.png"]
    body = "".join(P(frame(f'<draw:image xlink:href="{h}"/>') + frame(f'<draw:object xlink:href="{h}"/>'))
                   for h in imgs) + P("본문")
    doc, ctx = parse(odt_doc(body, extra={"Pictures/a.png": PNG, "x.png": PNG}))
    assert ctx.skipped["image"] == len(imgs) and ctx.skipped["object"] == len(imgs)
    assert markdown.render(doc, tmp() / "a") == "본문\n"


def test_self_referencing_object_terminates():
    loop = (f'<office:document-content {NS}><office:body><office:chart><chart:chart chart:class="chart:bar">'
            + table_xml([cell("", "") .replace("</table:table-cell>", frame('<draw:object xlink:href="./Object 1"/>') * 2
                                               + "</table:table-cell>")])
            + '</chart:chart></office:chart></office:body></office:document-content>')
    t0 = time.monotonic()
    out = md(odt_doc(frame('<draw:object xlink:href="./Object 1"/>') + P("뒤"), extra={"Object 1/content.xml": loop}))
    assert time.monotonic() - t0 < 5 and "뒤" in out


def test_strings_are_capped():
    st = ('<text:list-style style:name="L"><text:list-level-style-number text:level="1" style:num-format="1" '
          f'style:num-prefix="{"p" * 5000}" style:num-suffix="{"s" * 5000}"/></text:list-style>')
    body = ('<text:list text:style-name="L"><text:list-item>' + P("x") + '</text:list-item></text:list>'
            + frame('<draw:object xlink:href="./Object 1"/>'))
    doc, _ = parse(odt_doc(body, st, extra={"Object 1/content.xml": chart_xml(title="t" * 5000, cls="chart:" + "c" * 5000)}))
    assert len(doc.blocks[0].marker) <= 64 and len(doc.blocks[1].caption) <= 200
    long_alt = frame('<draw:image xlink:href="Pictures/a.png"/><svg:title>' + "a" * 5000 + "</svg:title>")
    doc, _ = parse(odt_doc(P(long_alt), extra={"Pictures/a.png": PNG}))
    assert len(doc.blocks[0].alt) <= 200


def test_one_bad_block_loses_only_itself():
    orig = odf_base.Body.table

    def boom(self, tbl, d):
        raise ValueError("boom")
    odf_base.Body.table = boom
    try:
        doc, ctx = parse(odt_doc(table_xml([cell("x")]) + P("뒤")))
    finally:
        odf_base.Body.table = orig
    assert ctx.skipped["block"] == 1 and markdown.render(doc, tmp() / "a") == "뒤\n"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
