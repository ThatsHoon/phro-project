"""OOXML entry and the Word (docm) parser: structure, styles, numbering, tables, fields, objects, hostile input.

Run: python -I -X utf8 tests/test_reader_docm.py
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
from reader.formats import ooxml  # noqa: E402
from reader_fix import make_zip  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RELS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
NSX = ('xmlns:r="{rel}" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
       'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
       'xmlns:v="urn:schemas-microsoft-com:vml" '
       'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"')
MACRO = "application/vnd.ms-word.document.macroEnabled.main+xml"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def tmp():
    return Path(tempfile.mkdtemp())


def docm(body, parts=None, rels=(), strict=False):
    """A macro-enabled package. rels: (id, type suffix, target, external) for the main part."""
    w, rel = (WS, RELS) if strict else (W, REL)
    ns = f'xmlns:w="{w}" ' + NSX.format(rel=rel)
    rel_xml = "".join(
        f'<Relationship Id="{i}" Type="{rel}/{t}" Target="{tg}"' + (' TargetMode="External"' if ext else "") + "/>"
        for i, t, tg, ext in rels)
    members = {
        "[Content_Types].xml": ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                f'<Override PartName="/word/document.xml" ContentType="{MACRO}"/></Types>'),
        "_rels/.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/></Relationships>'),
        "word/document.xml": f'<w:document {ns}><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                         f'{rel_xml}</Relationships>'),
    }
    for name, data in (parts or {}).items():
        members[name] = data.replace("{NS}", ns) if isinstance(data, str) else data
    return make_zip(tmp() / "a.docm", members)


def P(text, ppr=""):
    return f'<w:p><w:pPr>{ppr}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def parse(path):
    ctx = Context()
    return reader.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, _ = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden)


def styles(xml):
    return {"word/styles.xml": f'<w:styles {{NS}}>{xml}</w:styles>'}


STYLES_REL = (("rS", "styles", "styles.xml", False),)


# ---- entry
def test_registered():
    assert "docm" in reader.BUILT


def test_macro_main_type_accepted():
    assert "Hello" in md(docm(P("Hello")))


def test_strict_namespace():
    body = P("본문") + '<w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>제목</w:t></w:r></w:p>'
    assert md(docm(body, strict=True)) == md(docm(body))
    assert "# 제목" in md(docm(body, strict=True))


def test_encrypted_cfb():
    p = tmp() / "e.docm"
    blob = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 600 + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 100
    p.write_bytes(blob)
    try:
        parse(p)
        assert False
    except ReadError as e:
        assert e.kind == "encrypted"


def test_plain_cfb_is_damaged():
    p = tmp() / "e.docm"
    p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 600)
    try:
        parse(p)
        assert False
    except ReadError as e:
        assert e.kind == "damaged" and "OLE" in e.detail


def test_docm_not_a_package():
    p = make_zip(tmp() / "a.docm", {"word/document.xml": "<x/>"})
    try:
        parse(p)
        assert False
    except ReadError as e:
        assert e.kind == "damaged"


def test_wrong_root_is_damaged():
    p = docm("")
    # swap the main part for a workbook root
    import zipfile
    out = tmp() / "b.docm"
    with zipfile.ZipFile(p) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            data = zi.read(i.filename)
            if i.filename == "word/document.xml":
                data = b"<workbook/>"
            zo.writestr(i.filename, data)
    try:
        parse(out)
        assert False
    except ReadError as e:
        assert e.kind == "damaged" and "Word or PowerPoint" in e.detail


def test_presentation_not_yet():
    p = docm("")
    import zipfile
    out = tmp() / "b.docm"
    with zipfile.ZipFile(p) as zi, zipfile.ZipFile(out, "w") as zo:
        for i in zi.infolist():
            data = zi.read(i.filename)
            if i.filename == "word/document.xml":
                data = b"<presentation/>"
            zo.writestr(i.filename, data)
    try:
        parse(out)
        assert False
    except ReadError as e:
        assert e.kind == "unsupported"


# ---- word rules
def test_heading_outline_and_chain():
    st = styles('<w:style w:styleId="H2"><w:name w:val="heading 2"/></w:style>'
                '<w:style w:styleId="MyH"><w:name w:val="My"/><w:basedOn w:val="H2"/></w:style>')
    body = (P("A", '<w:outlineLvl w:val="0"/>') + P("B", '<w:pStyle w:val="MyH"/>')
            + P("C", '<w:pStyle w:val="MyH"/><w:outlineLvl w:val="9"/>'))
    out = md(docm(body, st, STYLES_REL))
    assert "# A" in out and "## B" in out
    assert "\n\nC" in "\n\n" + out and "# C" not in out


def test_style_outline_and_cycle():
    st = styles('<w:style w:styleId="X"><w:name w:val="x"/><w:basedOn w:val="Y"/></w:style>'
                '<w:style w:styleId="Y"><w:name w:val="y"/><w:basedOn w:val="X"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
                '<w:style w:styleId="T"><w:name w:val="Title"/></w:style>')
    out = md(docm(P("deep", '<w:pStyle w:val="X"/>') + P("top", '<w:pStyle w:val="T"/>'), st, STYLES_REL))
    assert "### deep" in out and "# top" in out


NUMBERING = ('<w:numbering {NS}><w:abstractNum w:abstractNumId="0">'
             '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
             '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2."/></w:lvl>'
             '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')


def num(ilvl, numid=1):
    return f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{numid}"/></w:numPr>'


def test_numbering_restart():
    body = P("a", num(0)) + P("b", num(0)) + P("c", num(1)) + P("d", num(0))
    out = md(docm(body, {"word/numbering.xml": NUMBERING}, (("rN", "numbering", "numbering.xml", False),)))
    assert "1. a\n2. b\n  a. c\n3. d" in out, out


def test_numbered_heading_and_formats():
    nm = ('<w:numbering {NS}><w:abstractNum w:abstractNumId="0">'
          '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="upperRoman"/><w:lvlText w:val="%1)"/></w:lvl>'
          '<w:lvl w:ilvl="1"><w:numFmt w:val="ganada"/><w:lvlText w:val="%1-%2"/></w:lvl>'
          '<w:lvl w:ilvl="2"><w:numFmt w:val="bullet"/><w:lvlText w:val="o"/></w:lvl>'
          '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/>'
          '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="3"/></w:lvlOverride></w:num></w:numbering>')
    body = (P("H", num(0) + '<w:outlineLvl w:val="0"/>') + P("k", num(1)) + P("m", num(2)))
    out = md(docm(body, {"word/numbering.xml": nm}, (("rN", "numbering", "numbering.xml", False),)))
    assert "# III) H" in out and "  III-가 k" in out, out
    assert "- m" in out


def test_numbering_many_placeholders():
    nm = ('<w:numbering {NS}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/>'
          f'<w:lvlText w:val="{"%1" * 5000}"/></w:lvl></w:abstractNum>'
          '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
    t0 = time.time()
    out = md(docm("".join(P("x", num(0)) for _ in range(200)), {"word/numbering.xml": nm},
                  (("rN", "numbering", "numbering.xml", False),)))
    assert time.time() - t0 < 5 and "x" in out


def tc(text, props=""):
    return f'<w:tc><w:tcPr>{props}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'


def test_vmerge_gridspan():
    body = ('<w:tbl><w:tr>' + tc("A", '<w:vMerge w:val="restart"/>') + tc("B", '<w:gridSpan w:val="2"/>') + '</w:tr>'
            '<w:tr>' + tc("", "<w:vMerge/>") + tc("C") + tc("D") + '</w:tr></w:tbl>')
    out = md(docm(body))
    assert '<td rowspan="2">A</td>' in out and '<td colspan="2">B</td>' in out, out
    assert "C" in out and "D" in out


def test_gridbefore_and_header():
    body = ('<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>' + tc("H1") + tc("H2") + '</w:tr>'
            '<w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>' + tc("x") + '</w:tr></w:tbl>')
    out = md(docm(body))
    assert "H1" in out and "H2" in out and "x" in out
    doc, _ = parse(docm(body))
    t = doc.blocks[0]
    assert t.rows[0][0].header and t.rows[1][0].colspan == 1 and t.rows[1][0].blocks == []


def test_alternate_content():
    ac = ('<mc:AlternateContent><mc:Choice Requires="wps"><w:r><w:t>X</w:t></w:r></mc:Choice>'
          '<mc:Fallback><w:r><w:t>W</w:t></w:r></mc:Fallback></mc:AlternateContent>'
          '<mc:AlternateContent><mc:Choice Requires="zz"><w:r><w:t>Y</w:t></w:r></mc:Choice>'
          '<mc:Fallback><w:r><w:t>Z</w:t></w:r></mc:Fallback></mc:AlternateContent>')
    out = md(docm(f"<w:p>{ac}</w:p>"))
    assert "XZ" in out and "Y" not in out and "W" not in out, out


def test_alternate_textbox_once():
    box = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx><w:txbxContent>'
           '<w:p><w:r><w:t>boxed</w:t></w:r></w:p></w:txbxContent></wps:txbx></wps:wsp></w:drawing></mc:Choice>'
           '<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r><w:t>boxed</w:t></w:r></w:p>'
           '</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    out = md(docm(f"<w:p><w:r><w:t>lead</w:t></w:r>{box}</w:p>"))
    assert out.count("boxed") == 1 and "lead" in out, out


def test_vanish_and_del():
    st = styles('<w:style w:styleId="Hid"><w:name w:val="hid"/><w:rPr><w:vanish/></w:rPr></w:style>')
    body = ('<w:p><w:r><w:t>keep </w:t></w:r><w:r><w:rPr><w:vanish/></w:rPr><w:t>secret</w:t></w:r>'
            '<w:del><w:r><w:delText>gone</w:delText></w:r></w:del><w:ins><w:r><w:t>added</w:t></w:r></w:ins></w:p>')
    out = md(docm(body, st, STYLES_REL))
    assert "(2 hidden places left out)" in out and "added" in out and "secret" not in out and "gone" not in out
    shown = md(docm(body, st, STYLES_REL), hidden=True)
    assert "secret" in shown and "gone" in shown
    out2 = md(docm('<w:p><w:r><w:rPr><w:rStyle w:val="Hid"/></w:rPr><w:t>zz</w:t></w:r></w:p>', st, STYLES_REL))
    assert "(1 hidden places left out)" in out2 and "zz" not in out2


def test_sdt_placeholder_dropped():
    sdt = ('<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>' + P("여기에 입력") + '</w:sdtContent></w:sdt>'
           '<w:sdt><w:sdtPr/><w:sdtContent>' + P("채움") + '</w:sdtContent></w:sdt>'
           '<w:p><w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent><w:r><w:t>자리</w:t></w:r></w:sdtContent></w:sdt>'
           '<w:sdt><w:sdtContent><w:r><w:t>인라인</w:t></w:r></w:sdtContent></w:sdt></w:p>')
    out = md(docm(sdt))
    assert "여기에 입력" not in out and "자리" not in out and "채움" in out and "인라인" in out


def test_sdt_row_level():
    body = ('<w:tbl><w:sdt><w:sdtContent><w:tr>' + tc("r1") + tc("r2") + '</w:tr></w:sdtContent></w:sdt></w:tbl>')
    assert "r1" in md(docm(body)) and "r2" in md(docm(body))


def test_footnote_skip_separators():
    fn = ('<w:footnotes {NS}><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
          '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:t>nope</w:t></w:r></w:p></w:footnote>'
          '<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/><w:t>각주 본문</w:t></w:r></w:p></w:footnote></w:footnotes>')
    body = '<w:p><w:r><w:t>글</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
    out = md(docm(body, {"word/footnotes.xml": fn}, (("rF", "footnotes", "footnotes.xml", False),)))
    assert "글[^1]" in out and "[^1]: 각주 본문" in out and "nope" not in out, out


def test_comment():
    cm = ('<w:comments {NS}><w:comment w:id="0" w:author="김"><w:p><w:r><w:t>수정 필요</w:t></w:r></w:p></w:comment></w:comments>')
    body = ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>문장</w:t></w:r><w:commentRangeEnd w:id="0"/>'
            '<w:r><w:commentReference w:id="0"/></w:r></w:p>')
    out = md(docm(body, {"word/comments.xml": cm}, (("rC", "comments", "comments.xml", False),)))
    assert "Comment by 김: 수정 필요" in out and "문장[^1]" in out, out


def test_field_hyperlink():
    body = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://example.com" </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>여기</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
            '<w:r><w:t> 후</w:t></w:r></w:p>'
            '<w:p><w:fldSimple w:instr=" HYPERLINK &quot;https://s.example&quot; "><w:r><w:t>간단</w:t></w:r></w:fldSimple>'
            '<w:fldSimple w:instr=" PAGE "><w:r><w:t>7</w:t></w:r></w:fldSimple></w:p>')
    out = md(docm(body))
    assert "[여기](https://example.com) 후" in out and "[간단](https://s.example)7" in out, out


def test_hyperlink_rel_and_anchor():
    body = ('<w:p><w:hyperlink r:id="rH"><w:r><w:t>외부</w:t></w:r></w:hyperlink>'
            '<w:hyperlink w:anchor="mark"><w:r><w:t>내부</w:t></w:r></w:hyperlink></w:p>')
    out = md(docm(body, rels=(("rH", "hyperlink", "https://ex.org/a", True),)))
    assert "[외부](https://ex.org/a)내부" in out, out


def chart_xml():
    return ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
            '<c:lineChart><c:ser><c:tx><c:v>S</c:v></c:tx><c:cat><c:strLit><c:ptCount val="1"/><c:pt idx="0"><c:v>a</c:v></c:pt></c:strLit></c:cat>'
            '<c:val><c:numLit><c:ptCount val="1"/><c:pt idx="0"><c:v>5</c:v></c:pt></c:numLit></c:val></c:ser></c:lineChart>'
            '</c:plotArea></c:chart></c:chartSpace>')


def test_image_alt_and_chart():
    pic = ('<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="p" descr="도표"/><a:graphic><a:graphicData>'
           '<pic:pic><pic:blipFill><a:blip r:embed="rI"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
           '<w:p><w:r><w:t>앞</w:t></w:r><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><c:chart r:id="rC"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
           '<w:p><w:r><w:t>뒤</w:t></w:r></w:p>')
    p = docm(pic, {"word/media/i.png": PNG, "word/charts/c1.xml": chart_xml()},
             (("rI", "image", "media/i.png", False), ("rC", "chart", "charts/c1.xml", False)))
    out = md(p)
    assert "![도표](assets/img-001.png)" in out, out
    assert "Chart: line" in out and out.index("앞") < out.index("Chart: line") < out.index("뒤"), out


def test_vml_image_and_missing_image():
    body = ('<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rI"/></v:shape></w:pict></w:r></w:p>'
            '<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rX"/>'
            '</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    p = docm(body, {"word/media/i.png": PNG}, (("rI", "image", "media/i.png", False), ("rX", "image", "../../evil.png", False)))
    doc, ctx = parse(p)
    assert "assets/img-001.png" in markdown.render(doc, tmp() / "a")
    assert ctx.skipped["image"] == 1


def test_runs_join():
    body = '<w:p><w:r><w:t>12</w:t></w:r><w:r><w:t>분</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>굵게</w:t></w:r><w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>평</w:t></w:r></w:p>'
    out = md(docm(body))
    assert "12분**굵게**평" in out, out


def test_headers_footers():
    hd = '<w:hdr {NS}>' + P("머리글") + '</w:hdr>'
    ft = '<w:ftr {NS}>' + P("바닥글") + '</w:ftr>'
    out = md(docm(P("본문"), {"word/header1.xml": hd, "word/footer1.xml": ft},
                  (("rH", "header", "header1.xml", False), ("rF", "footer", "footer1.xml", False))))
    assert "Header: 머리글" in out and "Footer: 바닥글" in out


# ---- hostile input
def test_damaged_parts_lose_only_themselves():
    p = docm(P("살아있다") + P("n", num(0)), {"word/styles.xml": "<w:styles", "word/numbering.xml": "<!DOCTYPE x><a/>",
                                             "word/footnotes.xml": "garbage", "word/header1.xml": "<x"},
             (("a", "styles", "styles.xml", False), ("b", "numbering", "numbering.xml", False),
              ("c", "footnotes", "footnotes.xml", False), ("d", "header", "header1.xml", False),
              ("e", "footer", "missing.xml", False)))
    doc, ctx = parse(p)
    out = markdown.render(doc, tmp() / "a")
    assert "살아있다" in out
    for k in ("styles", "numbering", "footnotes", "header"):
        assert ctx.skipped[k] >= 1, (k, ctx.skipped)


def test_grid_numbers_in_millions():
    body = ('<w:tbl><w:tr><w:trPr><w:gridBefore w:val="9000000"/></w:trPr>' + tc("a", '<w:gridSpan w:val="99999999"/>') + tc("b")
            + '</w:tr><w:tr>' + tc("c", '<w:gridSpan w:val="8000000"/>') + '</w:tr></w:tbl>')
    t0 = time.time()
    out = md(docm(body))
    assert time.time() - t0 < 3 and "a" in out and "b" in out and "c" in out


def test_field_begin_without_end_and_deep_fields():
    body = ('<w:p>' + '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>HYPERLINK "https://x.example"</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' * 5000 + '<w:r><w:t>끝없는</w:t></w:r></w:p>' + P("다음"))
    t0 = time.time()
    out = md(docm(body))
    assert time.time() - t0 < 5 and "끝없는" in out and "다음" in out
    out = md(docm('<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:t>고아</w:t></w:r></w:p>'))
    assert "고아" in out


def test_deep_nesting_tables_sdt_and_textbox():
    t = '<w:p><w:r><w:t>깊은</w:t></w:r></w:p>'
    for _ in range(300):
        t = '<w:tbl><w:tr><w:tc>' + t + '</w:tc></w:tr></w:tbl>'
    body = '<w:p><w:r><w:t>머리</w:t></w:r></w:p>' + t
    t0 = time.time()
    out = md(docm(body))
    assert time.time() - t0 < 5 and "머리" in out
    sdt = P("속")
    for _ in range(500):
        sdt = f"<w:sdt><w:sdtContent>{sdt}</w:sdtContent></w:sdt>"
    out = md(docm(P("앞") + sdt))
    assert "앞" in out
    box = "<w:r><w:t>상자</w:t></w:r>"
    for _ in range(300):
        box = f"<w:r><w:txbxContent><w:p>{box}</w:p></w:txbxContent></w:r>"
    out = md(docm(P("앞") + f"<w:p>{box}</w:p>"))
    assert "앞" in out


def test_repeated_parts_and_references():
    refs = "".join('<w:headerReference w:type="default" r:id="rH"/>' for _ in range(2000))
    body = ('<w:p><w:r><w:t>본</w:t></w:r></w:p><w:sectPr>' + refs + '</w:sectPr>'
            + "".join('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><c:chart r:id="rC"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
                      '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>' for _ in range(300)))
    fn = '<w:footnotes {NS}><w:footnote w:id="1"><w:p><w:r><w:t>하나</w:t></w:r></w:p></w:footnote></w:footnotes>'
    rels = (("rH", "header", "header1.xml", False), ("rH2", "header", "header1.xml", False),
            ("rC", "chart", "charts/c1.xml", False), ("rF", "footnotes", "footnotes.xml", False))
    p = docm(body, {"word/header1.xml": '<w:hdr {NS}>' + P("머리글") + '</w:hdr>', "word/charts/c1.xml": chart_xml(),
                    "word/footnotes.xml": fn}, rels)
    t0 = time.time()
    doc, _ = parse(p)
    out = markdown.render(doc, tmp() / "a")
    assert time.time() - t0 < 5
    assert doc.headers == ["머리글"]
    assert out.count("[^1]: 하나") == 1


def test_image_reference_budget():
    from reader.formats import ooxml_word
    big = PNG + b"\x01" * 1_000_000
    body = "".join('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rI"/>'
                    '</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>' for _ in range(400))
    p = docm(body, {"word/media/i.png": big}, (("rI", "image", "media/i.png", False),))
    old = ooxml_word.IMAGE_BYTES
    ooxml_word.IMAGE_BYTES = 50 * 1_000_000
    try:
        doc, ctx = parse(p)
    finally:
        ooxml_word.IMAGE_BYTES = old
    assert sum(1 for b in doc.blocks if type(b).__name__ == "Image") <= 51
    assert ctx.skipped["image"] > 300


def test_href_escape_is_missing():
    body = '<w:p><w:r><w:t>t</w:t></w:r></w:p>'
    p = docm(body, {"word/header1.xml": "<w:hdr {NS}/>"},
             (("a", "header", "../../../x.xml", False), ("b", "styles", "..\\\\styles.xml", False)))
    doc, ctx = parse(p)
    assert ctx.skipped["header"] >= 1 and ctx.skipped["styles"] >= 1


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
