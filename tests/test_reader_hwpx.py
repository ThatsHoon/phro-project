"""HWPX parser: text, headings, numbering, tables, objects, notes, fields and limits.

Run: python -I -X utf8 tests/test_reader_hwpx.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.formats import hwpx  # noqa: E402
from reader_fix import make_zip  # noqa: E402

NS = ('xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
      'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
      'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
      'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
      'xmlns:opf="http://www.idpf.org/2007/opf/"')
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
NONE4 = "".join(f'<hh:{s}Border type="NONE"/>' for s in ("left", "right", "top", "bottom"))


def tmp():
    return Path(tempfile.mkdtemp())


def hwpx_doc(section_xml, header_xml="", hpf_items="", extra=None):
    hpf = (f'<?xml version="1.0"?><opf:package {NS}><opf:manifest>'
           '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
           '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
           f'{hpf_items}</opf:manifest></opf:package>')
    members = {
        "mimetype": "application/hwp+zip",
        "Contents/content.hpf": hpf,
        "Contents/header.xml": f'<hh:head {NS}><hh:refList>{header_xml}</hh:refList></hh:head>',
        "Contents/section0.xml": f'<hs:sec {NS}>{section_xml}</hs:sec>',
    }
    members.update(extra or {})
    return make_zip(tmp() / "a.hwpx", members)


def P(text, pr="0", style="0", run="0"):
    return (f'<hp:p paraPrIDRef="{pr}" styleIDRef="{style}"><hp:run charPrIDRef="{run}">'
            f'<hp:t>{text}</hp:t></hp:run></hp:p>')


def parse(path):
    ctx = Context()
    return hwpx.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, _ = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden)


def test_registered():
    assert "hwpx" in reader.BUILT and len(reader.FORMATS) == 46


def test_text_tail_and_tab():
    p = hwpx_doc('<hp:p><hp:run><hp:t>가<hp:tab/>나</hp:t></hp:run></hp:p>')
    assert md(p) == "가\t나\n"
    p = hwpx_doc('<hp:p><hp:run><hp:t>a<hp:fwSpace/>b<hp:lineBreak/>c</hp:t></hp:run></hp:p>')
    assert md(p) == "a b\nc\n"


def test_bold_run():
    p = hwpx_doc(P("굵게", run="1") + P("보통"), '<hh:charPr id="1"><hh:bold/></hh:charPr>')
    assert md(p) == "**굵게**\n\n보통\n"


def test_outline_heading():
    hdr = '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
    assert md(hwpx_doc(P("제목", pr="1"), hdr)) == "# 제목\n"
    hdr = '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="8"/></hh:paraPr>'
    assert md(hwpx_doc(P("깊음", pr="1"), hdr)) == "###### 깊음\n"


def test_style_name_heading():
    hdr = ('<hh:style id="3" name="개요 2" paraPrIDRef="0"/>'
           '<hh:style id="4" name="제목" paraPrIDRef="0"/>'
           '<hh:style id="5" name="부제목" paraPrIDRef="0"/>')
    assert md(hwpx_doc(P("가", style="3") + P("나", style="4") + P("다", style="5"), hdr)) \
        == "## 가\n\n# 나\n\n## 다\n"


def test_long_outline_demoted():
    hdr = '<hh:style id="2" name="개요 2" paraPrIDRef="0"/>'
    out = md(hwpx_doc(P("가" * 149 + "다.", style="2"), hdr))
    assert "#" not in out and out.startswith("가")
    out = md(hwpx_doc(P("가" * 41 + "?", style="2"), hdr))
    assert "#" not in out
    assert md(hwpx_doc(P("가" * 41, style="2"), hdr)).startswith("## ")


def test_no_heading_in_cell():
    hdr = '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
    cell = (f'<hp:tc><hp:subList>{P("칸", pr="1")}</hp:subList><hp:cellAddr colAddr="0" rowAddr="0"/></hp:tc>'
            f'<hp:tc><hp:subList>{P("옆")}</hp:subList><hp:cellAddr colAddr="1" rowAddr="0"/></hp:tc>')
    row2 = (f'<hp:tc><hp:subList>{P("아래")}</hp:subList><hp:cellAddr colAddr="0" rowAddr="1"/></hp:tc>'
            f'<hp:tc><hp:subList>{P("끝")}</hp:subList><hp:cellAddr colAddr="1" rowAddr="1"/></hp:tc>')
    sec = f'<hp:p><hp:run><hp:tbl><hp:tr>{cell}</hp:tr><hp:tr>{row2}</hp:tr></hp:tbl></hp:run></hp:p>'
    out = md(hwpx_doc(sec, hdr))
    assert "#" not in out and "|칸|옆|" in out


NUM = ('<hh:numbering id="1"><hh:paraHead level="1" start="1" numFormat="DIGIT">^1.</hh:paraHead>'
       '<hh:paraHead level="2" start="1" numFormat="DIGIT">^2)</hh:paraHead></hh:numbering>'
       '<hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>'
       '<hh:paraPr id="2"><hh:heading type="NUMBER" idRef="1" level="1"/></hh:paraPr>'
       '<hh:paraPr id="3"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>')


def test_numbering_counters():
    sec = P("a", pr="1") + P("b", pr="1") + P("c", pr="2") + P("d", pr="1") + P("e", pr="2")
    assert md(hwpx_doc(sec, NUM)) == "1. a\n2. b\n  1) c\n3. d\n  1) e\n"
    out = md(hwpx_doc(P("1. x", pr="1"), NUM))
    assert out == "1. x\n"
    assert md(hwpx_doc(P("점", pr="3"), NUM)) == "- 점\n"


def test_numbering_formats():
    want = {"CIRCLED_DIGIT": ("①", "②"), "ROMAN_CAPITAL": ("I", "II"), "ROMAN_SMALL": ("i", "ii"),
            "LATIN_CAPITAL": ("A", "B"), "LATIN_SMALL": ("a", "b"), "HANGUL_SYLLABLE": ("가", "나"),
            "HANGUL_JAMO": ("ㄱ", "ㄴ"), "WEIRD": ("1", "2")}
    for f, (m1, m2) in want.items():
        hdr = (f'<hh:numbering id="1"><hh:paraHead level="1" start="1" numFormat="{f}">^1</hh:paraHead>'
               '</hh:numbering><hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>')
        assert md(hwpx_doc(P("x", pr="1") + P("y", pr="1"), hdr)) == f"{m1} x\n{m2} y\n", f


def _tc(text, col, row, cs=1, rs=1, header="0", bf="0"):
    return (f'<hp:tc header="{header}" borderFillIDRef="{bf}"><hp:subList>{P(text)}</hp:subList>'
            f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="{cs}" rowSpan="{rs}"/></hp:tc>')


def _tbl(*rows):
    return '<hp:p><hp:run><hp:tbl>' + "".join(f"<hp:tr>{r}</hp:tr>" for r in rows) + '</hp:tbl></hp:run></hp:p>'


def test_merged_table():
    sec = _tbl(_tc("합", 0, 0, rs=2) + _tc("우", 1, 0), _tc("하", 1, 1))
    out = md(hwpx_doc(sec))
    assert out.count('<td rowspan="2">') == 1 and "<td>우</td>" in out and "<td>하</td>" in out


def test_span_counts_compacted_columns():
    # column 1 never starts a cell, so the grid keeps columns 0, 2, 3; A (columns 0-2) covers two of them
    sec = _tbl(_tc("A", 0, 0, cs=3) + _tc("B", 3, 0),
               _tc("C", 0, 1, cs=2) + _tc("D", 2, 1) + _tc("E", 3, 1))
    doc, _ = parse(hwpx_doc(sec))
    spans = [[(c.colspan, c.rowspan) for c in row] for row in doc.blocks[0].rows]
    assert spans == [[(2, 1), (1, 1)], [(1, 1), (1, 1), (1, 1)]], spans
    out = md(hwpx_doc(sec))
    assert '<td colspan="2">A</td><td>B</td>' in out, out


def test_header_cell_pipe_table():
    sec = _tbl(_tc("머", 0, 0, header="1") + _tc("리", 1, 0, header="1"), _tc("값", 0, 1) + _tc("둘", 1, 1))
    doc, _ = parse(hwpx_doc(sec))
    assert doc.blocks[0].rows[0][0].header and not doc.blocks[0].rows[1][0].header
    assert "|머|리|\n|---|---|\n|값|둘|" in md(hwpx_doc(sec))


def test_table_budget():
    # a table past the document's TABLE_NODES is left out as one past MAX_DEPTH is, each counted under depth
    sec = "".join(_tbl(_tc(str(k), 0, 0) + _tc("x", 1, 0)) for k in range(4))
    old, limits.TABLE_NODES = limits.TABLE_NODES, 2
    try:
        doc, ctx = parse(hwpx_doc(sec))
    finally:
        limits.TABLE_NODES = old
    assert ctx.skipped["depth"] == 2 and len(doc.blocks) == 2, (ctx.skipped, doc.blocks)
    assert [b.rows[0][0].blocks[0].inlines[0].text for b in doc.blocks] == ["0", "1"], doc.blocks


def test_layout_table_unwrapped():
    hdr = f'<hh:borderFill id="5">{NONE4}</hh:borderFill>'
    sec = _tbl(_tc("왼쪽", 0, 0, bf="5") + _tc("오른쪽", 1, 0, bf="5"))
    assert md(hwpx_doc(sec, hdr)) == "왼쪽\n\n오른쪽\n"
    # a bordered one-row table stays a table
    hdr = '<hh:borderFill id="5"><hh:leftBorder type="SOLID"/></hh:borderFill>'
    assert "왼쪽" in md(hwpx_doc(sec, hdr)) and "|" in md(hwpx_doc(sec, hdr))


def test_switch_case():
    sec = ('<hp:p><hp:run><hp:switch>'
           '<hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/unknown"><hp:t>X</hp:t></hp:case>'
           '<hp:default><hp:t>Y</hp:t></hp:default></hp:switch></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "Y\n"
    sec = sec.replace("2016/unknown", "2016/paragraph")
    assert md(hwpx_doc(sec)) == "X\n"


def test_image_and_auto_alt():
    items = '<opf:item id="image1" href="BinData/image1.png" media-type="image/png"/>'
    pic = '<hp:p><hp:run><hp:pic><hp:shapeComment>%s</hp:shapeComment><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
    p = hwpx_doc(pic % "그림입니다.", hpf_items=items, extra={"BinData/image1.png": PNG})
    assert md(p) == "![](assets/img-001.png)\n"
    p = hwpx_doc(pic % "회사 로고", hpf_items=items, extra={"BinData/image1.png": PNG})
    assert md(p) == "![회사 로고](assets/img-001.png)\n"


def test_footnote():
    sec = ('<hp:p><hp:run><hp:t>본문</hp:t><hp:ctrl><hp:footNote><hp:subList>'
           + P("주석") + '</hp:subList></hp:footNote></hp:ctrl></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "본문[^1]\n\n[^1]: 주석\n"
    doc, _ = parse(hwpx_doc(sec))
    assert list(doc.notes) == ["fn-1"]


def test_endnote_and_comment():
    sec = ('<hp:p><hp:run><hp:t>본</hp:t><hp:ctrl><hp:endNote><hp:subList>' + P("끝") + '</hp:subList></hp:endNote></hp:ctrl>'
           '<hp:ctrl><hp:hiddenComment><hp:subList>' + P("메모") + '</hp:subList></hp:hiddenComment></hp:ctrl></hp:run></hp:p>')
    doc, _ = parse(hwpx_doc(sec))
    assert list(doc.notes) == ["en-1", "cm-1"]
    out = md(hwpx_doc(sec))
    assert "[^1]: 끝" in out and "[^2]: Comment: 메모" in out


def test_header_footer():
    sec = ('<hp:p><hp:run><hp:ctrl><hp:header><hp:subList>' + P("머리말") + '</hp:subList></hp:header></hp:ctrl>'
           '<hp:ctrl><hp:footer><hp:subList>' + P("꼬리말") + '</hp:subList></hp:footer></hp:ctrl>'
           '<hp:t>본문</hp:t></hp:run></hp:p>')
    doc, _ = parse(hwpx_doc(sec))
    assert doc.headers == ["머리말"] and doc.footers == ["꼬리말"]


def _field(fid, ftype, cmd=None):
    params = f'<hp:parameters><hp:stringParam name="Command">{cmd}</hp:stringParam></hp:parameters>' if cmd else ""
    return f'<hp:ctrl><hp:fieldBegin id="{fid}" type="{ftype}">{params}</hp:fieldBegin></hp:ctrl>'


def _fend(fid):
    return f'<hp:ctrl><hp:fieldEnd beginIDRef="{fid}"/></hp:ctrl>'


def test_hyperlink_field():
    sec = ('<hp:p><hp:run><hp:t>앞 </hp:t>' + _field(7, "HYPERLINK", "https://example.com;1;0;0;")
           + '<hp:t>링크</hp:t>' + _fend(7) + '<hp:t> 뒤</hp:t></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "앞 [링크](https://example.com) 뒤\n"
    sec = sec.replace("https://example.com;1", r"http\://a.b/x\;y;1")
    assert "(http://a.b/x;y)" in md(hwpx_doc(sec))


def test_click_here_dropped():
    sec = ('<hp:p><hp:run>' + _field(3, "CLICK_HERE") + '<hp:t>여기에 입력</hp:t>' + _fend(3)
           + '<hp:t>남음</hp:t></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "남음\n"


def test_track_change_deleted():
    sec = ('<hp:p><hp:run><hp:t>새<hp:deleteBegin Id="1"/>옛<hp:deleteEnd/>글</hp:t></hp:run></hp:p>')
    out = md(hwpx_doc(sec))
    assert out.splitlines()[0] == "(1 hidden places left out)" and "옛" not in out and "새글" in out
    assert "(hidden: 옛)" in md(hwpx_doc(sec), hidden=True)


CHART = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
         '<c:barChart><c:ser><c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>S</c:v></c:pt></c:strCache></c:strRef></c:tx>'
         '<c:cat><c:strRef><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>A</c:v></c:pt></c:strCache></c:strRef></c:cat>'
         '<c:val><c:numRef><c:numCache><c:ptCount val="1"/><c:pt idx="0"><c:v>5</c:v></c:pt></c:numCache></c:numRef></c:val>'
         '</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>')


def test_chart():
    sec = ('<hp:p><hp:run><hp:switch>'
           '<hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/ooxmlchart">'
           '<hp:chart chartIDRef="Chart/chart1.xml"/></hp:case><hp:default><hp:t>대체</hp:t></hp:default>'
           '</hp:switch></hp:run></hp:p>')
    out = md(hwpx_doc(sec, extra={"Chart/chart1.xml": CHART}))
    assert "Chart: bar" in out and "대체" not in out and "|A|5|" in out


def test_header_pictures_and_charts_skipped():
    # a header is a line of text, as in doc and hwp: its pictures (one with no part) and its chart are skipped without
    # being read or counted, its text and a text box's are kept, and the body picture is read
    pic = '<hp:pic><hc:img binaryItemIDRef="%s"/></hp:pic>'
    head = ('<hp:p><hp:run><hp:t>머리말</hp:t>' + pic % "image1" + pic % "image9"
            + '<hp:chart chartIDRef="Chart/chart1.xml"/><hp:rect><hp:drawText><hp:subList>' + P("상자")
            + '</hp:subList></hp:drawText></hp:rect></hp:run></hp:p>')
    sec = (f'<hp:p><hp:run><hp:ctrl><hp:header><hp:subList>{head}</hp:subList></hp:header></hp:ctrl></hp:run></hp:p>'
           f'<hp:p><hp:run>{pic % "image2"}</hp:run></hp:p>')
    items = ('<opf:item id="image1" href="BinData/image1.png"/><opf:item id="image2" href="BinData/image2.png"/>'
             '<opf:item id="image9" href="BinData/image9.png"/>')
    p = hwpx_doc(sec, hpf_items=items, extra={"BinData/image1.png": PNG + bytes(2**19), "BinData/image2.png": PNG,
                                              "Chart/chart1.xml": CHART})
    read = []
    old = hwpx._Reader.part_bytes

    def spy(self, href, kind):
        read.append(href)
        return old(self, href, kind)
    hwpx._Reader.part_bytes = spy
    try:
        doc, ctx = parse(p)
    finally:
        hwpx._Reader.part_bytes = old
    assert doc.headers == ["머리말 상자"] and not ctx.skipped, (doc.headers, ctx.skipped)
    assert read == ["BinData/image2.png"], read
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "Header: 머리말 상자\n\n![](assets/img-001.png)\n"


def _wide_chart(series, labels=0):
    cat = ""
    if labels:
        pts = "".join(f'<c:pt idx="{i}"><c:v>L{i}</c:v></c:pt>' for i in range(labels))
        cat = f'<c:cat><c:strRef><c:strCache><c:ptCount val="{labels}"/>{pts}</c:strCache></c:strRef></c:cat>'
    empty = ('<c:ser>' + '<c:val><c:numRef><c:numCache><c:ptCount val="10000"/></c:numCache></c:numRef></c:val></c:ser>')
    first = empty.replace("<c:ser>", "<c:ser>" + cat, 1)
    return ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
            '<c:barChart>' + first + empty * (series - 1) + '</c:barChart></c:plotArea></c:chart></c:chartSpace>')


def test_wide_chart_is_bounded():
    import time
    from reader import limits
    sec = '<hp:p><hp:run><hp:t>본문</hp:t><hp:chart chartIDRef="Chart/chart1.xml"/></hp:run></hp:p>'
    for labels, cut in ((0, 0), (10_000, 1)):
        path = hwpx_doc(sec, extra={"Chart/chart1.xml": _wide_chart(300, labels)})
        t0 = time.monotonic()
        doc, ctx = parse(path)
        tbl = doc.blocks[1]
        assert time.monotonic() - t0 < 2, time.monotonic() - t0
        assert sum(len(r) for r in tbl.rows) <= limits.CHART_CELLS and len(tbl.rows[0]) == 301, len(tbl.rows)
        assert ctx.skipped["chart_cut"] == cut, ctx.skipped
    assert len(tbl.rows) == limits.CHART_CELLS // 301 and tbl.caption.endswith(f"(first {len(tbl.rows) - 1} points)")


def test_chart_placements_charged():
    # every placement of one chart is charged against CHART_TOTAL, as in docx and pptx: a chart near CHART_CELLS placed
    # 50 times is placed while the cells stay under the cap, each one past it is counted under chart, and the render
    # stays bounded
    import time
    n = 50
    sec = '<hp:p><hp:run><hp:chart chartIDRef="Chart/chart1.xml"/></hp:run></hp:p>' * n
    path = hwpx_doc(sec, extra={"Chart/chart1.xml": _wide_chart(300, 10_000)})
    t0 = time.monotonic()
    doc, ctx = parse(path)
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert time.monotonic() - t0 < 10, time.monotonic() - t0
    keep = limits.CHART_TOTAL // sum(len(r) for r in next(b for b in doc.blocks if hasattr(b, "rows")).rows)
    assert ctx.skipped["chart"] == n - keep and out.count("Chart: bar") == keep, (ctx.skipped, out.count("Chart:"))


def test_equation():
    eq = '<hp:p><hp:run><hp:equation><hp:script>%s</hp:script></hp:equation>%s</hp:run></hp:p>'
    assert md(hwpx_doc(eq % ("a over b", ""))) == "$$\\frac{a}{b}$$\n"
    assert "값 $\\frac{a}{b}$" in md(hwpx_doc('<hp:p><hp:run><hp:t>값 </hp:t><hp:equation><hp:script>a over b</hp:script></hp:equation></hp:run></hp:p>'))


def test_equation_script_to_tex():
    tex = hwpx._tex
    assert tex("a over b") == r"\frac{a}{b}"
    assert tex("{a+b} over {c}") == r"\frac{a+b}{c}"
    assert tex("sqrt x") == r"\sqrt{x}"
    assert tex("sqrt {x+1}") == r"\sqrt{x+1}"
    assert tex("x^2 + y_1") == "x^2 + y_1"
    assert tex("a times b le c ge d") == r"a \times b \le c \ge d"
    assert tex("alpha + Omega + omega") == r"\alpha + \Omega + \omega"
    assert tex("foo bar") == "foo bar"
    assert tex("{" * 500 + "a" + "}" * 500)  # deep groups do not recurse forever


def test_text_box():
    sec = ('<hp:p><hp:run><hp:rect><hp:drawText><hp:subList>' + P("상자 글") + '</hp:subList></hp:drawText></hp:rect>'
           '<hp:t>밖</hp:t></hp:run></hp:p>')
    out = md(hwpx_doc(sec))
    assert "상자 글" in out and "밖" in out


def test_depth_counted_not_recursion_error():
    inner = P("바닥")
    for _ in range(80):
        inner = f'<hp:p><hp:run><hp:rect><hp:drawText><hp:subList>{inner}</hp:subList></hp:drawText></hp:rect></hp:run></hp:p>'
    doc, ctx = parse(hwpx_doc(inner))
    assert ctx.skipped["depth"] >= 1 and any(w.startswith("depth") for w in ctx.warnings)
    assert "바닥" not in markdown.render(doc, tmp() / "a")


def test_section_order_and_damaged_section():
    items = '<opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>'
    p = hwpx_doc(P("하나"), hpf_items=items, extra={"Contents/section1.xml": f'<hs:sec {NS}>{P("둘")}</hs:sec>'})
    assert md(p) == "하나\n\n둘\n"
    p = hwpx_doc(P("하나"), hpf_items=items, extra={"Contents/section1.xml": "<x"})
    doc, ctx = parse(p)
    assert markdown.render(doc, tmp() / "a") == "하나\n" and ctx.skipped["section"] == 1
    p = hwpx_doc("<x", extra={"Contents/section0.xml": "<x"})
    try:
        parse(p)
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged" and "no readable section" in e.detail


def test_duplicate_sections_and_parts():
    items = '<opf:item id="s" href="Contents/section0.xml" media-type="application/xml"/>' * 2000
    doc, ctx = parse(hwpx_doc(P("한번"), hpf_items=items))
    assert markdown.render(doc, tmp() / "a") == "한번\n"
    pic = '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
    p = hwpx_doc(pic, hpf_items='<opf:item id="image1" href="BinData/image1.png"/>', extra={"BinData/image1.png": PNG})
    doc, _ = parse(p)
    assert doc.blocks[0].data is doc.blocks[1].data
    doc, ctx = parse(hwpx_doc(pic))
    assert ctx.skipped["image"] == 2


def test_picture_placements_charged():
    # every placement is charged against IMAGE_BYTES, as in docx and doc: one 4 MiB picture placed 100 times is placed
    # while the total stays under the cap, each one past it is counted under image, and the render stays bounded
    import random
    import time
    big = PNG + random.Random(7).randbytes(4 * 2**20 - len(PNG))  # does not deflate past ZIP_RATIO
    n, keep = 100, limits.IMAGE_BYTES // (4 * 2**20)
    pic = '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
    p = hwpx_doc(pic * n, hpf_items='<opf:item id="image1" href="BinData/image1.png"/>',
                 extra={"BinData/image1.png": big})
    t0 = time.monotonic()
    doc, ctx = parse(p)
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert time.monotonic() - t0 < 10
    assert ctx.skipped["image"] == n - keep and out.count("![") == keep, (ctx.skipped, out.count("!["))


def test_huge_and_negative_addresses():
    import time
    t0 = time.monotonic()
    sec = _tbl(_tc("가", 0, 0) + _tc("나", 1, 0), _tc("먼", 0, 2000000000) + _tc("다", 1, 2000000000, rs=99999))
    out = md(hwpx_doc(sec))
    assert time.monotonic() - t0 < 5 and "먼" in out and "다" in out
    sec = _tbl(_tc("음", 0, -3) + _tc("옆", 1, -3), _tc("양", 0, 0) + _tc("끝", 1, 0))
    out = md(hwpx_doc(sec))
    assert "음" in out and "양" in out


def test_equation_deep_nesting():
    for src in ("sqrt {" * 600, "a over {" * 600):
        sec = f'<hp:p><hp:run><hp:t>글</hp:t><hp:equation><hp:script>{src}</hp:script></hp:equation></hp:run></hp:p>'
        assert "글" in md(hwpx_doc(sec))


def test_field_stack_capped():
    sec = ('<hp:p><hp:run>' + "".join(_field(i, "HYPERLINK", "https://e.com;1") for i in range(3000))
           + '<hp:t>남음</hp:t></hp:run></hp:p>')
    assert "남음" in md(hwpx_doc(sec))
    sec = '<hp:p><hp:run><hp:t>' + '<hp:deleteBegin Id="1"/>' * 3000 + '남음</hp:t></hp:run></hp:p>'
    parse(hwpx_doc(sec))


def test_marker_capped():
    hdr = ('<hh:numbering id="1"><hh:paraHead level="1" start="1" numFormat="DIGIT">' + "가" * 200 + '^1</hh:paraHead></hh:numbering>'
           '<hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>')
    doc, _ = parse(hwpx_doc(P("x", pr="1"), hdr))
    assert len(doc.blocks[0].marker) == 64


def test_fallback_natural_order():
    members = {"mimetype": "application/hwp+zip"}
    for n in (10, 2, 1):
        members[f"Contents/section{n}.xml"] = f'<hs:sec {NS}>{P(str(n))}</hs:sec>'
    assert md(make_zip(tmp() / "e.hwpx", members)) == "1\n\n2\n\n10\n"


def test_missing_chart_counted():
    sec = '<hp:p><hp:run><hp:chart chartIDRef="Chart/none.xml"/></hp:run></hp:p>'
    _, ctx = parse(hwpx_doc(sec))
    assert ctx.skipped["chart"] == 1


def _flat_bmp(w, h):
    import struct
    stride = (w * 3 + 3) // 4 * 4
    size = 54 + stride * h
    head = b"BM" + struct.pack("<IHHI", size, 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0)
    return head + b"\xff" * (stride * h)


def test_flat_bmp_image_is_skipped_not_fatal():
    bmp = _flat_bmp(3000, 1000)  # 9 MB that deflates far past the ratio limit
    items = '<opf:item id="image1" href="BinData/image1.bmp" media-type="image/bmp"/>'
    pic = '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
    path = hwpx_doc(P("본문") + pic, hpf_items=items, extra={"BinData/image1.bmp": bmp})
    assert path.stat().st_size < 50_000
    out = tmp() / "out"
    meta = reader.convert(path, out)
    text = (out / "out.md").read_text(encoding="utf-8")
    assert text == "(could not read: 1 image)\n\n본문\n", text
    assert meta["unread"] == ["1 image"] and meta["images"] == 0, meta


def test_body_part_over_ratio_limit_is_denied():
    from reader import gate, limits
    path = hwpx_doc(P("본문") + " " * (3 * 2**20))
    try:
        reader.convert(path, tmp() / "out")
        raise AssertionError("converted")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail == limits.BIG_PART + ": Contents/section0.xml", e
        why = gate._deny(path, e.kind, e.detail)["hookSpecificOutput"]["permissionDecisionReason"]
    assert why.startswith("a.hwpx: a part expands too much to read safely."), why


def test_nothing_readable_is_damaged():
    sec = '<hp:p><hp:run><hp:chart chartIDRef="Chart/none.xml"/></hp:run></hp:p>'
    try:
        reader.convert(hwpx_doc(sec), tmp() / "out")
    except ReadError as e:
        assert e.kind == "damaged" and e.detail == "nothing readable (could not read: 1 chart)", e
    else:
        raise AssertionError("no error")
    meta = reader.convert(hwpx_doc(P("본문") + sec), tmp() / "out")
    assert meta["unread"] == ["1 chart"], meta


def test_section_fallback_without_manifest_entries():
    members = {"mimetype": "application/hwp+zip",
               "Contents/section1.xml": f'<hs:sec {NS}>{P("둘")}</hs:sec>',
               "Contents/section0.xml": f'<hs:sec {NS}>{P("하나")}</hs:sec>'}
    assert md(make_zip(tmp() / "b.hwpx", members)) == "하나\n\n둘\n"


def test_encrypted_manifest():
    p = hwpx_doc(P("x"), extra={"META-INF/manifest.xml": "<manifest><encryption-data/></manifest>"})
    try:
        parse(p)
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "encrypted"


def test_hwpx_not_a_package():
    p = make_zip(tmp() / "c.hwpx", {"readme.txt": "hi"})
    try:
        parse(p)
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged" and "not a hwpx package" in e.detail


def test_hpf_marks_package_without_mimetype():
    members = {"Contents/content.hpf": f'<opf:package {NS}><opf:manifest/></opf:package>',
               "Contents/section0.xml": f'<hs:sec {NS}>{P("본")}</hs:sec>'}
    assert md(make_zip(tmp() / "d.hwpx", members, stored=())) == "본\n"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
