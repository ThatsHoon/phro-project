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
from reader import diagram, limits, markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Code, Context  # noqa: E402
from reader.formats import hwpx  # noqa: E402
from reader_fix import make_zip  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402

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
    assert "hwpx" in reader.BUILT and len(reader.FORMATS) == 63


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
    auto = ("그림입니다.\n원본 그림의 이름: CLP000012340001.bmp\n원본 그림의 크기: 가로 1024pixel, 세로 768pixel\n"
            "사진 찍은 날짜: 2019년 05월 21일 오후 3:25")
    p = hwpx_doc(pic % auto, hpf_items=items, extra={"BinData/image1.png": PNG})
    assert md(p) == "![](assets/img-001.png)\n"
    p = hwpx_doc(pic % (auto + "\n회사 로고"), hpf_items=items, extra={"BinData/image1.png": PNG})
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


def test_click_here_filled_value_kept():
    # a filled click-here field (dirty="1") holds its value between begin and end; the guide text is only in the
    # Direction parameter and never shows, filled or not
    begin = ('<hp:ctrl><hp:fieldBegin id="88" type="CLICK_HERE" name="성명" editable="1" dirty="%s"><hp:parameters>'
             '<hp:stringParam name="Direction">이름을 입력하세요</hp:stringParam></hp:parameters></hp:fieldBegin></hp:ctrl>')
    sec = ('<hp:p><hp:run><hp:t>성명: </hp:t>' + begin + '<hp:t>%s</hp:t>' + _fend(88) + '<hp:t> 끝</hp:t></hp:run></hp:p>')
    assert md(hwpx_doc(sec % ("1", "홍길동"))) == "성명: 홍길동 끝\n"
    assert md(hwpx_doc(sec % ("0", "이름을 입력하세요"))) == "성명:  끝\n"
    # a value written without the dirty mark (a form filled by a program) is not the guide text: it is kept
    assert md(hwpx_doc(sec % ("0", "홍길동"))) == "성명: 홍길동 끝\n"


def test_table_and_picture_captions():
    # a table's and a picture's caption list follow the object, as hwp has them
    cap = '<hp:caption side="%s"><hp:subList>%s</hp:subList></hp:caption>'
    tbl = ('<hp:p><hp:run><hp:t>표 앞</hp:t><hp:tbl>' + cap % ("TOP", P("표 7. 연도별 예산"))
           + "<hp:tr>" + _tc("구분", 0, 0) + _tc("금액", 1, 0) + "</hp:tr><hp:tr>" + _tc("인건비", 0, 1)
           + _tc("120", 1, 1) + "</hp:tr></hp:tbl></hp:run></hp:p>")
    items = '<opf:item id="image1" href="BinData/image1.png" media-type="image/png"/>'
    pic = ('<hp:p><hp:run><hp:pic>' + cap % ("BOTTOM", P("그림 3. 조직도"))
           + '<hc:img binaryItemIDRef="image1"/></hp:pic><hp:t>그림 뒤</hp:t></hp:run></hp:p>')
    out = md(hwpx_doc(tbl + pic, hpf_items=items, extra={"BinData/image1.png": PNG}))
    assert out == ("표 앞\n\n|구분|금액|\n|---|---|\n|인건비|120|\n\n표 7. 연도별 예산\n\n그림 뒤\n\n"
                   "![](assets/img-001.png)\n\n그림 3. 조직도\n"), out
    # a shape's caption is a paragraph of its own, not text run into the paragraph that holds the shape
    rect = ('<hp:p><hp:run><hp:t>앞</hp:t><hp:rect>' + cap % ("BOTTOM", P("도형 1")) + '<hp:drawText><hp:subList>'
            + P("상자") + '</hp:subList></hp:drawText></hp:rect></hp:run></hp:p>')
    assert md(hwpx_doc(rect)) == "앞\n\n상자\n\n도형 1\n"
    # any other object's caption is read too
    video = '<hp:p><hp:run><hp:t>앞</hp:t><hp:video>' + cap % ("BOTTOM", P("동영상 1. 시연")) + '</hp:video></hp:run></hp:p>'
    assert md(hwpx_doc(video)) == "앞\n\n동영상 1. 시연\n"


OUTLINE = ('<hh:numbering id="1"><hh:paraHead level="1" start="1" numFormat="DIGIT">^1.</hh:paraHead>'
           '<hh:paraHead level="2" start="1" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead>'
           '<hh:paraHead level="3" start="1" numFormat="DIGIT">^3)</hh:paraHead>'
           '<hh:paraHead level="4" start="1" numFormat="DIGIT"/></hh:numbering>'
           '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
           '<hh:paraPr id="2"><hh:heading type="OUTLINE" idRef="0" level="1"/></hh:paraPr>'
           '<hh:paraPr id="4"><hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/paragraph">'
           '<hh:heading type="OUTLINE" idRef="0" level="2"/></hp:case><hp:default><hh:heading type="NONE" idRef="0" '
           'level="0"/></hp:default></hp:switch></hh:paraPr>'
           '<hh:paraPr id="5"><hh:heading type="OUTLINE" idRef="0" level="3"/></hh:paraPr>')


def test_outline_numbers():
    # the section's outline numbering (secPr outlineShapeIDRef) numbers its outline paragraphs, a heading in a switch
    # too; a paragraph demoted for its length keeps the number it took, a number typed in the text is not doubled,
    # and a level whose format is empty gives none
    sec = ('<hp:p><hp:run><hp:secPr outlineShapeIDRef="1"/></hp:run></hp:p>' + P("사업 개요", pr="1")
           + P("본 사업은 지역 물류 체계를 개선하기 위하여 추진하는 것으로 세부 계획은 다음과 같이 정한다.", pr="2")
           + P("나. 추진 목표", pr="2") + P("세부 항목", pr="4") + P("번호 없음", pr="5") + P("사업 내용", pr="1"))
    out = md(hwpx_doc(sec, OUTLINE))
    assert out == ("# 1. 사업 개요\n\n가. 본 사업은 지역 물류 체계를 개선하기 위하여 추진하는 것으로 세부 계획은 다음과 같이 "
                   "정한다.\n\n## 나. 추진 목표\n\n### 1) 세부 항목\n\n#### 번호 없음\n\n# 2. 사업 내용\n"), out
    # without a section outline numbering, and in a table cell, the outline paragraphs keep their own text only
    assert md(hwpx_doc(P("사업 개요", pr="1"), OUTLINE)) == "# 사업 개요\n"
    cell = _tbl(_tc("x", 0, 0) + _tc("y", 1, 0), _tc("z", 0, 1) + "<hp:tc><hp:subList>" + P("칸 제목", pr="1")
                + '</hp:subList><hp:cellAddr colAddr="1" rowAddr="1"/></hp:tc>')
    sec = '<hp:p><hp:run><hp:secPr outlineShapeIDRef="1"/></hp:run></hp:p>' + cell
    assert "|z|1. 칸 제목|" in md(hwpx_doc(sec, OUTLINE))
    # the outline numbering is the section's own: a later section that names none numbers nothing
    item = '<opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>'
    first = '<hp:p><hp:run><hp:secPr outlineShapeIDRef="1"/></hp:run></hp:p>' + P("첫 구역", pr="1")
    second = '<hs:sec ' + NS + '><hp:p><hp:run><hp:secPr/></hp:run></hp:p>' + P("둘째 구역", pr="1") + '</hs:sec>'
    out = md(hwpx_doc(first, OUTLINE, item, {"Contents/section1.xml": second}))
    assert out == "# 1. 첫 구역\n\n# 둘째 구역\n", out


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
    assert "Chart: column" in out and "대체" not in out and "|A|5|" in out


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
    assert ctx.skipped["chart"] == n - keep and out.count("Chart: column") == keep, (ctx.skipped, out.count("Chart:"))


def test_equation():
    eq = '<hp:p><hp:run><hp:equation><hp:script>%s</hp:script></hp:equation>%s</hp:run></hp:p>'
    assert md(hwpx_doc(eq % ("a over b", ""))) == "$$\\frac{a}{b}$$\n"
    assert "값 $\\frac{a}{b}$" in md(hwpx_doc('<hp:p><hp:run><hp:t>값 </hp:t><hp:equation><hp:script>a over b</hp:script></hp:equation></hp:run></hp:p>'))


def test_text_box():
    sec = ('<hp:p><hp:run><hp:rect><hp:drawText><hp:subList>' + P("상자 글") + '</hp:subList></hp:drawText></hp:rect>'
           '<hp:t>밖</hp:t></hp:run></hp:p>')
    out = md(hwpx_doc(sec))
    assert "상자 글" in out and "밖" in out


# ---- inline objects
def test_ruby_keeps_base_and_ruby_text():
    ruby = ('<hp:dutmal posType="TOP" szRatio="0" option="0" styleIDRef="0" align="CENTER"><hp:mainText>%s</hp:mainText>'
            '<hp:subText>%s</hp:subText></hp:dutmal>')
    sec = '<hp:p><hp:run><hp:t>앞 </hp:t>' + ruby % ("漢字", "한자") + '<hp:t> 뒤</hp:t></hp:run></hp:p>'
    assert md(hwpx_doc(sec)) == "앞 漢字(한자) 뒤\n"
    sec = '<hp:p><hp:run charPrIDRef="1">' + ruby % ("본말", "") + '</hp:run></hp:p>'
    assert md(hwpx_doc(sec, '<hh:charPr id="1"><hh:bold/></hh:charPr>')) == "**본말**\n"


def _memo(fid, body, author=None):
    who = f'<hp:stringParam name="Author">{author}</hp:stringParam>' if author else ""
    return (f'<hp:ctrl><hp:fieldBegin id="{fid}" type="MEMO" editable="1" dirty="1"><hp:parameters>{who}'
            f'<hp:integerParam name="Number">1</hp:integerParam></hp:parameters><hp:subList>{body}</hp:subList>'
            '</hp:fieldBegin></hp:ctrl>')


def test_memo_becomes_a_comment():
    # a memo field's sub-list is a comment note by its author, marked after the text the memo is anchored to
    sec = ('<hp:p><hp:run><hp:t>대상 </hp:t>' + _memo(77, P("확인 바람"), "김검토") + '<hp:t>앵커 문구</hp:t>' + _fend(77)
           + '<hp:t> 끝</hp:t></hp:run></hp:p>')
    doc, _ = parse(hwpx_doc(sec))
    assert list(doc.notes) == ["cm-1"]
    assert md(hwpx_doc(sec)) == "대상 앵커 문구[^1] 끝\n\n[^1]: Comment by 김검토: 확인 바람\n"
    # no author; a memo whose end is missing is marked at its paragraph's end; memos and hidden comments share cm-N
    sec = ('<hp:p><hp:run><hp:ctrl><hp:hiddenComment><hp:subList>' + P("숨은") + '</hp:subList></hp:hiddenComment>'
           '</hp:ctrl><hp:t>가</hp:t>' + _memo(5, P("메모 둘")) + '<hp:t>나</hp:t></hp:run></hp:p>')
    doc, _ = parse(hwpx_doc(sec))
    assert list(doc.notes) == ["cm-1", "cm-2"]
    assert md(hwpx_doc(sec)) == "[^1]가나[^2]\n\n[^1]: Comment: 숨은\n[^2]: Comment: 메모 둘\n"


def test_text_art_gives_its_text():
    art = '<hp:textart id="5" text="%s"><hp:sz width="1000" height="1000"/><hp:textartPr fontName="굴림"/></hp:textart>'
    sec = '<hp:p><hp:run><hp:t>앞</hp:t>' + art % "글맵시 문구" + '</hp:run></hp:p>'
    assert md(hwpx_doc(sec)) == "앞\n\n글맵시 문구\n"
    # in a section read as a diagram it is a shape that holds its text
    sec = R(obj("rect", 1, "상자"), obj("textart", 2, attrs=' text="표어"'), conn(9, 1, 2))
    out = md(hwpx_doc(sec))
    assert "표어" in out and "상자" in out, out


def test_form_objects_give_their_value():
    sec = ('<hp:p><hp:run><hp:t>동의 </hp:t><hp:checkBtn caption="동의함" value="CHECKED" name="c1"/><hp:t>, </hp:t>'
           '<hp:checkBtn caption="거부" value="UNCHECKED"/><hp:t>, </hp:t><hp:radioBtn caption="남" value="CHECKED"/>'
           '<hp:t> 지역 </hp:t><hp:comboBox name="cb" selectedValue="SEOUL"><hp:listItem displayText="부산" value="BUSAN"/>'
           '<hp:listItem displayText="서울" value="SEOUL"/></hp:comboBox><hp:t> 이름 </hp:t><hp:edit name="e1">'
           '<hp:text>홍길동</hp:text></hp:edit><hp:t> 목록 </hp:t><hp:listBox selectedValue="기타"/>'
           '<hp:btn caption="제출"/></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "동의 ☑ 동의함, ☐ 거부, ☑ 남 지역 서울 이름 홍길동 목록 기타\n"
    # a third state is said; a combo box with no selected value shows its one list item, as Hancom writes it
    sec = ('<hp:p><hp:run><hp:checkBtn caption="보류" value="INDETERMINATE"/><hp:t> 지역 </hp:t>'
           '<hp:comboBox selectedValue=""><hp:listItem displayText="" value="대전"/></hp:comboBox></hp:run></hp:p>')
    assert md(hwpx_doc(sec)) == "☐ 보류 (indeterminate) 지역 대전\n"


def test_ole_object_counted():
    items = '<opf:item id="ole1" href="BinData/ole1.ole" media-type="application/ole"/>'
    sec = ('<hp:p><hp:run><hp:t>앞</hp:t><hp:ole id="11" objectType="EMBEDDED" binaryItemIDRef="ole1">'
           '<hp:sz width="100" height="100"/></hp:ole><hp:t>뒤</hp:t></hp:run></hp:p>')
    p = hwpx_doc(sec, hpf_items=items, extra={"BinData/ole1.ole": b"\xd0\xcf\x11\xe0" + bytes(60)})
    doc, ctx = parse(p)
    assert ctx.skipped["object"] == 1, ctx.skipped
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "(could not read: 1 embedded object)\n\n앞뒤\n"


def test_equation_never_wrong_tex():
    eq = '<hp:p><hp:run><hp:t>식 </hp:t><hp:equation><hp:script>%s</hp:script></hp:equation></hp:run></hp:p>'
    assert md(hwpx_doc(eq % "x_i over n")) == "식 $\\frac{x_i}{n}$\n"
    assert md(hwpx_doc(eq % "rm{kg} cdot m")) == "식 `rm{kg} cdot m`\n"
    assert md(hwpx_doc(eq % "a`b")) == "식 $a\\,b$\n"
    assert md(hwpx_doc(eq % "a`b over c over d")) == "식 ``a`b over c over d``\n"
    lone = '<hp:p><hp:run><hp:equation><hp:script>%s</hp:script></hp:equation></hp:run></hp:p>'
    assert md(hwpx_doc(lone % "sum from{i=1} to {n} x_i")) == "$$\\sum_{i=1}^n x_i$$\n"
    assert md(hwpx_doc(lone % "a over b over c")) == "`a over b over c`\n"


# ---- connect lines as diagrams
def obj(tag, inst, text="", x=0, y=0, w=1000, h=1000, rel="PAPER", inner=False, attrs="", body=""):
    """A drawing object: at top level placed by hp:pos relative to `rel`, inside a container by hp:offset."""
    if inner:
        place = f'<hp:offset x="{x}" y="{y}"/><hp:orgSz width="{w}" height="{h}"/><hp:curSz width="0" height="0"/>'
    else:
        place = (f'<hp:sz width="{w}" height="{h}"/><hp:pos treatAsChar="0" vertRelTo="{rel}" horzRelTo="{rel}" '
                 f'vertOffset="{y}" horzOffset="{x}"/>')
    text = f'<hp:drawText><hp:subList>{P(text)}</hp:subList></hp:drawText>' if text else ""
    return f'<hp:{tag} id="0" instid="{inst}" {attrs}>{body}{text}{place}</hp:{tag}>'


def conn(inst, a, b, kind="STRAIGHT_ONEWAY", ends=((0, 0), (0, 0)), **kw):
    (x0, y0), (x1, y1) = ends
    body = (f'<hp:startPt x="{x0}" y="{y0}" subjectIDRef="{a}" subjectIdx="0"/>'
            f'<hp:endPt x="{x1}" y="{y1}" subjectIDRef="{b}" subjectIdx="0"/>')
    return obj("connectLine", inst, attrs=f'type="{kind}"', body=body, **kw)


def line(inst, head, tail, ends, **kw):
    (x0, y0), (x1, y1) = ends
    body = (f'<hp:lineShape headStyle="{head}" tailStyle="{tail}"/>'
            f'<hc:startPt x="{x0}" y="{y0}"/><hc:endPt x="{x1}" y="{y1}"/>')
    return obj("line", inst, body=body, **kw)


def R(*objs, text=""):
    """A paragraph whose run holds some text, then the objects."""
    t = f"<hp:t>{text}</hp:t>" if text else ""
    return f'<hp:p><hp:run>{t}{"".join(objs)}</hp:run></hp:p>'


def mermaid(doc):
    return "\n".join(b.text for b in doc.blocks if isinstance(b, Code) and b.lang == "mermaid")


def _connectline_sec():
    return (P("before") + R(obj("rect", 11, "Alpha"), obj("rect", 12, "Beta", x=5000),
                            conn(13, 11, 12, x=1000, ends=((0, 500), (4000, 500))), text="lead") + P("after"))


def test_hwpx_connectline():
    doc, ctx = parse(hwpx_doc(_connectline_sec()))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"before\n\nlead\n\n```mermaid\n{t}\n```\n\nafter\n", out  # each text once
    assert not ctx.skipped["shape"] and not ctx.skipped["connector_loose"], ctx.skipped


def test_hwpx_both_and_none_arrows():
    sec = R(obj("rect", 1, "A"), obj("ellipse", 2, "B", y=3000), obj("rect", 3, "C", y=6000),
            conn(4, 1, 2, "STROKE_BOTH"), conn(5, 2, 3, "ARC_NOARROW"))
    doc, _ = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2(("B"))\n  n3["C"]\n  n1 <--> n2\n  n2 --- n3', t


def test_section_scope_first_node_place():
    sec = (R(obj("rect", 11, "Alpha", rel="PARA"), text="one") + P("mid")
           + R(obj("rect", 12, "Beta", rel="PARA"), conn(13, 11, 12, rel="PARA"), text="three"))
    doc, _ = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"one\n\n```mermaid\n{t}\n```\n\nmid\n\nthree\n", out


def test_hwpx_container_inference():
    box = (obj("rect", 21, "Left", inner=True) + obj("rect", 22, "Right", x=3000, inner=True)
           + conn(23, 0, 0, x=500, y=500, ends=((0, 0), (3000, 0)), inner=True))
    sec = R(obj("container", 20, w=4000, body=box), text="lead") + R(obj("rect", 30, "Zed", x=90000))
    doc, ctx = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == ('flowchart LR\n  n1["Left"]\n  n2["Right"]\n  %% inferred from position\n'
                                   '  n1 --> n2'), t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"lead\n\n```mermaid\n{t}\n```\n\nZed\n", out
    assert not ctx.skipped["connector_loose"], ctx.skipped


def test_hwpx_container_connector_leaving_it_uses_the_section():
    box = obj("rect", 21, "Inner", inner=True) + conn(23, 21, 30, inner=True)
    sec = R(obj("container", 20, body=box), text="lead") + P("mid") + R(obj("rect", 30, "Outer"))
    doc, _ = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Inner"]\n  n2["Outer"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"lead\n\n```mermaid\n{t}\n```\n\nmid\n", out


def test_hwpx_arrowed_line_and_divider():
    sec = R(obj("rect", 1, "A"), obj("rect", 2, "B", x=5000),
            line(3, "NORMAL", "ARROW", ((500, 500), (5500, 500))),
            line(4, "NORMAL", "NORMAL", ((0, 2000), (6000, 2000))))
    doc, ctx = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 --> n2', t
    assert markdown.render(doc, tmp() / "a") == f"```mermaid\n{t}\n```\n"
    assert not ctx.skipped["connector_loose"], ctx.skipped


def test_hwpx_shape_in_table_cell():
    inner = R(obj("rect", 12, "Beta", rel="PARA"), conn(13, 11, 12, rel="PARA"))
    cell = f'<hp:tc><hp:subList>{inner}</hp:subList></hp:tc>'
    tbl = f'<hp:tbl><hp:tr>{cell}<hp:tc><hp:subList>{P("cell2")}</hp:subList></hp:tc></hp:tr></hp:tbl>'
    sec = R(obj("rect", 11, "Alpha", rel="PARA"), text="one") + R(tbl)
    doc, _ = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out.startswith(f"one\n\n```mermaid\n{t}\n```\n\n") and out.count("Beta") == 1 and "cell2" in out, out


def test_shapes_without_connector_unchanged():
    box = obj("rect", 1, "A", inner=True) + obj("ellipse", 2, "B", inner=True)
    sec = R(obj("container", 20, body=box), text="lead") + R(obj("rect", 3, "C"))
    assert md(hwpx_doc(sec)) == "lead\n\nA\n\nB\n\nC\n"  # as the reader read it before diagrams


DIVIDER = R(line(9, "NORMAL", "NORMAL", ((0, 0), (1, 1))))  # makes the section a scope; adds no text, no edge


def _cells(*texts):
    cells = "".join(f"<hp:tc><hp:subList>{t}</hp:subList></hp:tc>" for t in texts)
    return f"<hp:tbl><hp:tr>{cells}</hp:tr></hp:tbl>"


def test_table_in_a_drawing_object_reads_as_before():
    tbl = _cells(R(obj("rect", 50, "Held", rel="PARA")), P("c2"))
    in_box = obj("container", 20, body=obj("rect", 21, "In", inner=True) + tbl)
    for sec in (R(obj("rect", 1, "A", body=tbl)), R(in_box)):
        assert md(hwpx_doc(sec + DIVIDER)) == md(hwpx_doc(sec)), sec  # no shape is left in the model


def test_container_keeps_reading_order():
    box = obj("rect", 21, "First", inner=True) + _cells(P("Mid")) + obj("rect", 22, "Last", inner=True)
    sec = R(obj("container", 20, body=box))
    assert md(hwpx_doc(sec + DIVIDER)) == md(hwpx_doc(sec)) == "First\n\nMid\n\nLast\n"


def test_hwpx_container_end_outside_its_box_uses_the_section():
    box = obj("rect", 21, "In", w=500, h=500, inner=True) + conn(23, 0, 0, ends=((100, 100), (5500, 500)), inner=True)
    sec = R(obj("container", 20, body=box), text="lead") + R(obj("rect", 30, "Out", x=5000))
    doc, ctx = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["In"]\n  n2["Out"]\n  %% inferred from position\n  n1 --> n2', t
    assert markdown.render(doc, tmp() / "a") == f"lead\n\n```mermaid\n{t}\n```\n"
    assert not ctx.skipped["connector_loose"], ctx.skipped


def test_hwpx_glue_to_a_connector_keeps_the_container():
    box = obj("rect", 21, "In1", inner=True) + obj("rect", 22, "In2", x=3000, inner=True) + conn(23, 21, 22, inner=True)
    sec = R(obj("container", 20, rel="PARA", body=box)) + R(obj("rect", 30, "Out", rel="PARA"),
                                                             conn(31, 23, 30, rel="PARA"))
    doc, _ = parse(hwpx_doc(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["In1"]\n  n2["In2"]\n  n1 --> n2', t  # the container's frame
    assert markdown.render(doc, tmp() / "a") == f"```mermaid\n{t}\n```\n\nOut\n"


def test_place_failure_keeps_text():
    def boom(self, slots):
        raise ValueError("boom")
    real, diagram.Diagram.place = diagram.Diagram.place, boom
    try:
        doc, ctx = parse(hwpx_doc(_connectline_sec()))
    finally:
        diagram.Diagram.place = real
    assert markdown.render(doc, tmp() / "a") == "before\n\nlead\n\nAlpha\n\nBeta\n\nafter\n"
    assert ctx.skipped["shape"] == 1, ctx.skipped


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
