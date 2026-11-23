"""XHTML walker, small CSS cascade and the epub parser: spine, notes, tables, images, DRM, hostile input.

Run: python -I -X utf8 tests/test_reader_epub.py
"""
import base64
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import html as xhtml  # noqa: E402
from reader import markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_zip  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
XHTML = "http://www.w3.org/1999/xhtml"
OPS = "http://www.idpf.org/2007/ops"
CONTAINER = ('<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')
OPF_HEAD = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
ENC = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
       '<enc:EncryptedData><enc:EncryptionMethod Algorithm="{alg}"/><enc:CipherData>'
       '<enc:CipherReference URI="OEBPS/c.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>')


def tmp():
    return Path(tempfile.mkdtemp())


def page(body, head=""):
    return (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="{XHTML}" xmlns:epub="{OPS}">'
            f"<head><title>t</title>{head}</head><body>{body}</body></html>")


def epub_book(chapters, spine=None, css="", extra=None, title="책", manifest_extra=""):
    """chapters: {name: body or full html}; spine: [(name, linear)]; css goes to OEBPS/style.css."""
    spine = spine if spine is not None else [(n, True) for n in chapters]
    members = {"mimetype": "application/epub+zip", "META-INF/container.xml": CONTAINER}
    items, refs = "", ""
    link = '<link rel="stylesheet" href="style.css"/>' if css else ""
    for i, (name, body) in enumerate(chapters.items()):
        members["OEBPS/" + name] = body if body.startswith("<?xml") else page(body, link)
        items += f'<item id="c{i}" href="{quote(name)}" media-type="application/xhtml+xml"/>'
    ids = {n: f"c{i}" for i, n in enumerate(chapters)}
    for name, linear in spine:
        refs += f'<itemref idref="{ids[name]}"' + ("" if linear else ' linear="no"') + "/>"
    if css:
        members["OEBPS/style.css"] = css
    members["OEBPS/content.opf"] = (
        OPF_HEAD + f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>'
        f"<manifest>{items}{manifest_extra}</manifest><spine>{refs}</spine></package>")
    members.update(extra or {})
    return make_zip(tmp() / "a.epub", members)


def raw_book(opf_body, members):
    """An epub with a hand-written OPF body (inside <package>) and the given OEBPS members."""
    m = {"mimetype": "application/epub+zip", "META-INF/container.xml": CONTAINER,
         "OEBPS/content.opf": OPF_HEAD + opf_body + "</package>"}
    m.update(members)
    return make_zip(tmp() / "r.epub", m)


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


def test_stdlib_html_and_reader_html_coexist():
    import html.parser
    assert html.parser.HTMLParser and xhtml.tree and xhtml.__name__ == "reader.html"


def test_spine_order_nonlinear_last():
    p = epub_book({"c1.xhtml": "<p>one</p>", "c2.xhtml": "<p>two</p>", "c3.xhtml": "<p>three</p>"},
                  spine=[("c2.xhtml", True), ("c1.xhtml", False), ("c3.xhtml", True)])
    out, _ = md(p)
    assert out.index("two") < out.index("three") < out.index("one")


def test_noteref_footnote():
    body = ('<p>text<a epub:type="noteref" href="#n1">1</a></p>'
            '<aside epub:type="footnote" id="n1"><p>주석</p></aside>')
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "[^1]" in out and "[^1]: 주석" in out, out
    assert out.count("주석") == 1


def test_unreferenced_footnote_dropped():
    out, ctx = md(epub_book({"c.xhtml": '<p>x</p><aside epub:type="footnote" id="n9"><p>고아</p></aside>'}))
    assert "고아" not in out and ctx.skipped["note"] == 1


def test_endnote_in_other_chapter():
    p = epub_book({"c1.xhtml": '<p>a<a epub:type="noteref" href="notes.xhtml#e1">1</a></p>',
                   "notes.xhtml": '<section epub:type="endnotes"><li epub:type="endnote" id="e1">끝주</li></section>'})
    out, _ = md(p)
    assert "[^1]: 끝주" in out, out


def test_ruby():
    out, _ = md(epub_book({"c.xhtml": "<p><ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby></p>"}))
    assert "漢字(かんじ)" in out, out


def test_display_none_linked_css():
    out, _ = md(epub_book({"c.xhtml": '<p>shown</p><p class="hide">secret</p>'}, css=".hide{display:none}"))
    assert "(1 hidden places left out)" in out and "secret" not in out and "shown" in out


def test_css_cascade():
    css = "p{display:none} p.show{display:block} .x{font-weight:bold} .y{display:none !important}"
    c = xhtml.Css([css])

    def n(tag, **a):
        return xhtml.Node(tag, {k.replace("_", "-"): v for k, v in a.items()}, [])
    assert c.visibility(n("p")) == "none"
    assert c.visibility(n("p", **{"class": "show"})) == ""
    assert c.visibility(n("p", **{"class": "show", "style": "display:block"})) == ""
    assert c.visibility(n("div", **{"class": "y", "style": "display:block"})) == "none"
    assert c.visibility(n("div", style="display: none")) == "none"
    assert c.bold(n("span", **{"class": "x"})) and not c.bold(n("span"))


def test_inline_hidden_and_hidden_attr():
    out, _ = md(epub_book({"c.xhtml": '<p>a <span hidden="">b</span> c</p><p hidden="">zzz</p>'}))
    assert "(2 hidden places left out)" in out and "zzz" not in out


def test_nav_toc_removed():
    body = '<nav epub:type="toc"><ol><li>목차항목</li></ol></nav><p>본문</p>'
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "목차항목" not in out and "본문" in out


def test_table_colspan_and_header():
    body = '<table><tr><th colspan="2">h</th></tr><tr><td>a</td><td>b</td></tr></table>'
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "\n|h||\n-|-\na|b\n\nMerged: A1:B1\n" in out, out


def test_table_hostile_spans_clamped():
    body = ('<table><tr><td rowspan="999999999" colspan="999999999">a</td><td rowspan="0">b</td></tr>'
            '<tr><td>c</td></tr></table>')
    t0 = time.monotonic()
    out, _ = md(epub_book({"c.xhtml": body}))
    assert time.monotonic() - t0 < 5
    assert "\n|a||b||\n-|-|-|-\n||||c|\n\nMerged: A1:B2, C1:C2\n" in out, out


def test_image_and_data_uri():
    data = base64.b64encode(PNG).decode()
    body = f'<p><img src="images/a.png" alt="a"/></p><p><img src="data:image/png;base64,{data}" alt="d"/></p>'
    out, _ = md(epub_book({"c.xhtml": body}, extra={"OEBPS/images/a.png": PNG + b"1"}))
    assert "![a](assets/img-001.png)" in out, out
    assert "![d](assets/img-002.png)" in out and "base64" not in out, out


def test_data_uri_limits_and_outside_images():
    assert xhtml._data_uri("data:image/png;base64," + "A" * (7 * 10**6)) is None
    assert xhtml._data_uri("data:image/png;base64," + "A" * (6 * 10**6)) is not None
    body = ('<img src="data:image/png;base64,AAAA"/><img src="data:image/png,abc"/>'
            '<img src="../../x.png"/><img src="http://e.com/x.png"/><img src="/etc/x.png"/><p>ok</p>')
    t0 = time.monotonic()
    out, ctx = md(epub_book({"c.xhtml": body}))
    assert time.monotonic() - t0 < 10 and "ok" in out and "![" not in out
    assert ctx.skipped["image"] == 4, ctx.skipped


def test_links():
    body = '<p><a href="https://e.com/x">ext</a> <a href="javascript:alert(1)">js</a> <a href="#a">in</a></p>'
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "[ext](https://e.com/x)" in out and "javascript" not in out and "js in" in out, out


def test_percent_encoded_href():
    out, _ = md(epub_book({"ch 1.xhtml": "<p>공백</p>"}))
    assert "공백" in out


def test_pre_br_blockquote_lists_bold():
    body = ('<pre>a\n  b</pre><p>x<br/>y <b>B</b></p><blockquote><p>Q</p></blockquote>'
            '<ol start="3"><li>i<ul><li>j</li></ul></li><li>k</li></ol>')
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "a\n  b" in out and "x\ny **B**" in out and "> Q" in out, out
    assert "3. i" in out and "  - j" in out and "4. k" in out, out


def test_math_and_switch():
    body = ('<p><math alttext="x^2"><msup><mi>x</mi><mn>2</mn></msup></math></p>'
            '<epub:switch><epub:case required-namespace="m"><p>CASE</p></epub:case>'
            '<epub:default><p>DEF</p></epub:default></epub:switch>')
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "$x^{2}$" in out and "DEF" in out and "CASE" not in out, out


def test_drm():
    aes = ENC.format(alg="http://www.w3.org/2001/04/xmlenc#aes128-cbc")
    raises_kind(lambda: md(epub_book({"c.xhtml": "<p>x</p>"}, extra={"META-INF/encryption.xml": aes})), "drm")
    fonts = ENC.format(alg="http://www.idpf.org/2008/embedding")
    out, _ = md(epub_book({"c.xhtml": "<p>fine</p>"}, extra={"META-INF/encryption.xml": fonts}))
    assert "fine" in out
    raises_kind(lambda: md(epub_book({"c.xhtml": "<p>x</p>"}, extra={"META-INF/rights.xml": "<rights/>"})), "drm")


def test_title_not_repeated():
    out, _ = md(epub_book({"c.xhtml": "<h1>책</h1><p>본문</p>"}))
    assert out.count("# 책") == 1, out


def test_epub_not_a_package():
    p = make_zip(tmp() / "a.epub", {"mimetype": "application/epub+zip", "x.txt": "hi"})
    raises_kind(lambda: md(p), "damaged")


def test_damaged_chapter_loses_only_itself():
    p = raw_book('<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
                 '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>'
                 '<spine><itemref idref="b"/><itemref idref="a"/></spine>',
                 {"OEBPS/a.xhtml": page("<p>good</p>")})
    out, ctx = md(p)
    assert "good" in out and ctx.skipped["chapter"] == 1, ctx.skipped


def test_all_chapters_missing_is_damaged():
    p = raw_book('<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest>'
                 '<spine><itemref idref="a"/></spine>', {})
    raises_kind(lambda: md(p), "damaged")


def test_spine_duplicates_and_fallback_loop():
    p = raw_book('<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
                 '<item id="x" href="a.dat" media-type="text/x-x" fallback="y"/>'
                 '<item id="y" href="b.dat" media-type="text/x-y" fallback="x"/></manifest><spine>'
                 + '<itemref idref="c"/>' * 50 + '<itemref idref="x"/></spine>',
                 {"OEBPS/c.xhtml": page("<p>once</p>")})
    out, ctx = md(p)
    assert out.count("once") == 1 and ctx.warnings, (out, ctx.warnings)


def test_fallback_chain_resolves():
    p = raw_book('<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
                 '<item id="x" href="a.dat" media-type="text/x-x" fallback="c"/></manifest>'
                 '<spine><itemref idref="x"/></spine>', {"OEBPS/c.xhtml": page("<p>viaFallback</p>")})
    out, _ = md(p)
    assert "viaFallback" in out


def test_deep_nesting_fast():
    n = 80_000  # under the 1 MiB floor of the zip ratio check
    body = "<div>" * n + "<p>deep</p>" + "</div>" * n + "<p>after</p>"
    t0 = time.monotonic()
    out, ctx = md(epub_book({"c.xhtml": body}))
    assert time.monotonic() - t0 < 10 and "after" in out and "deep" in out
    assert ctx.skipped["depth"] > 0


def test_huge_css_is_capped():
    css = "".join(f".c{i}{{display:none}}" for i in range(45_000))  # under the zip ratio floor
    t0 = time.monotonic()
    out, _ = md(epub_book({"c.xhtml": '<p class="c5">a</p><p class="c44999">b</p>'}, css=css))
    assert time.monotonic() - t0 < 10 and "b" in out  # past the rule cap: not applied
    t0 = time.monotonic()
    c = xhtml.Css(["x" * 3_000_000 + "{display:none}", "/*" * 500_000, ".a{display:none}" * 300_000])
    assert time.monotonic() - t0 < 5
    assert xhtml.Css([" " * 2**20 + ".a{display:none}"]).visibility(xhtml.Node("p", {"class": "a"}, [])) == ""
    many = xhtml.Css(["".join(f".k{i}{{display:none}}" for i in range(20_000))])
    assert not many.visibility(xhtml.Node("p", {"class": "k19999"}, []))  # past the selector cap
    assert many.visibility(xhtml.Node("p", {"class": "k5"}, [])) == "none" and c


def test_stylesheet_hrefs_stay_in_package():
    head = '<link rel="stylesheet" href="../../etc/x.css"/><link rel="stylesheet" href="http://e.com/x.css"/>'
    out, _ = md(epub_book({"c.xhtml": page("<p>ok</p>", head)}))
    assert "ok" in out


def test_decoding():
    latin = '<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>caf\xe9</p></body></html>'.encode("latin-1")
    assert "café" in xhtml.text_of(xhtml.tree(latin))
    meta = '<html><head><meta charset="euc-kr"></head><body><p>한글</p></body></html>'.encode("euc-kr")
    assert "한글" in xhtml.text_of(xhtml.tree(meta))
    assert "ok" in xhtml.text_of(xhtml.tree('<?xml version="1.0" encoding="nonsense"?><p>ok</p>'.encode()))
    assert "x" in xhtml.text_of(xhtml.tree(b"\xef\xbb\xbf<p>x</p>"))


def test_parser_hostile_runs_stay_fast():
    t0 = time.monotonic()
    for unit in ("<a ", "<!--", "<", "<p>x", "<div>", "<br>", "<!-- -->x", "<b>x</b>", "&amp"):
        root = xhtml.tree((unit * 100_000).encode())
        xhtml.blocks(root, css=xhtml.Css([]), load=lambda s: None, notes={}, ctx=Context())
    assert time.monotonic() - t0 < 20


def test_lt_inside_quoted_attribute_values():
    body = ('<p><img src="images/a.png" alt="a<b"/> after</p><p><a href="http://e.com/?a<b">link</a> tail</p>'
            '<span title="x < y">hello</span>')
    out, _ = md(epub_book({"c.xhtml": body}, extra={"OEBPS/images/a.png": PNG}))
    assert "![" in out and "after" in out, out
    assert "[link](http://e.com/?a" in out and "tail" in out, out
    assert "hello" in out and "<span" not in out and "title" not in out, out


def test_unclosed_tag_runs_are_fast():
    t0 = time.monotonic()
    for unit in ("<a ", "<", '<a "<a "', "<a '", '<a x="<a x="'):
        xhtml.tree((unit * 200_000).encode())
    assert time.monotonic() - t0 < 10


def test_at_rule_brace_bomb():
    sheet = "@m{" + "{" * 500_000 + "}" * 500_000
    t0 = time.monotonic()
    c = xhtml.Css([sheet] * 32)
    took = time.monotonic() - t0
    assert c.capped
    assert took < 10, took  # under a second alone; loose, as the file's other bounds, so a loaded run does not flake


def test_node_cap():
    root = xhtml.tree(b"<p>early</p>" + b"<i/>" * 600_000)
    ctx = Context()
    out = xhtml.blocks(root, css=xhtml.Css([]), load=lambda s: None, notes={}, ctx=ctx)
    assert xhtml.text_of(root).startswith("early") and out and ctx.skipped["node"] == 1
    assert any(w.startswith("node:") for w in ctx.warnings)


def test_media_type_with_parameters():
    p = raw_book('<manifest><item id="c" href="c.xhtml" media-type=" Application/XHTML+xml ; charset=utf-8"/>'
                 '</manifest><spine><itemref idref="c"/></spine>', {"OEBPS/c.xhtml": page("<p>param</p>")})
    out, _ = md(p)
    assert "param" in out


def test_plain_link_to_footnote():
    for kind in ("footnote", "endnote", "rearnote", "note"):
        body = f'<p>t<a href="#n1">1</a></p><aside epub:type="{kind}" id="n1"><p>정의</p></aside>'
        out, _ = md(epub_book({"c.xhtml": body}))
        assert "[^1]" in out and "[^1]: 정의" in out, (kind, out)
    p = epub_book({"c1.xhtml": '<p>a<a href="notes.xhtml#e1">1</a></p>',
                   "notes.xhtml": '<aside epub:type="endnote" id="e1">끝</aside>'})
    out, _ = md(p)
    assert "[^1]: 끝" in out, out


def test_dangling_noteref_stays_text():
    body = ('<p>x<a epub:type="noteref" href="../q.xhtml#n1">A</a><a epub:type="noteref" href="#nope">B</a>'
            '<a epub:type="noteref" href="http://e.com/#n">C</a><a href="#top">D</a></p>')
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "[^" not in out and "xABC" in out.replace(" ", "").replace("[C](http://e.com/#n)", "C"), out


def test_colspan_capped_to_real_width():
    body = '<table><tr><td colspan="9">a</td></tr><tr><td>b</td><td>c</td></tr></table>'
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "\n|a||\n-|-\nb|c\n\nMerged: A1:B1\n" in out, out


def test_nested_table_list_quote_past_depth():
    n = 300
    cases = {"quote": "<blockquote>" * n + "<p>core</p>" + "</blockquote>" * n,
             "list": "<ul><li>" * n + "core" + "</li></ul>" * n,
             "table": "<table><tr><td>" * n + "core" + "</td></tr></table>" * n}
    for name, body in cases.items():
        t0 = time.monotonic()
        out, ctx = md(epub_book({"c.xhtml": body + "<p>tail</p>"}))
        assert time.monotonic() - t0 < 5 and "tail" in out, name
        assert name == "table" or "core" in out, name  # text flattened into a table row has no cell to live in
        assert ctx.skipped["depth"] > 0, name


def test_damaged_encryption_xml_is_a_warning():
    out, ctx = md(epub_book({"c.xhtml": "<p>fine</p>"}, extra={"META-INF/encryption.xml": "<encryption"}))
    assert "fine" in out and any("encryption" in w for w in ctx.warnings)


def test_tolerant_html():
    root = xhtml.tree(b"<p>a<br>b<p>c<img src=x>d</b></i>")
    assert xhtml.text_of(root).replace("\n", "") == "abcd"


def test_referenced_linear():
    # 60,000 notes each referenced from the body, and every note naming the next: one pass, in first-use order
    from reader.model import Para, Text
    n = 60_000
    notes = {f"n{i}": [Para([xhtml.Ref(f"n{i + 1}", inlines=[Text("next")])])] for i in range(n)}
    body = [Para([xhtml.Ref(f"n{i}", inlines=[Text(str(i))]) for i in range(n)])]
    t0 = time.perf_counter()
    keep = xhtml.referenced(body, notes)
    took = time.perf_counter() - t0
    assert keep == [f"n{i}" for i in range(n)] and took < 2, took


NCX_HEAD = '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>'
NCX_ITEM = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'


def nav_point(label, src, inner=""):
    return f'<navPoint><navLabel><text>{label}</text></navLabel><content src="{src}"/>{inner}</navPoint>'


def test_toc_targets_become_headings():
    body = ('<ol><li id="toc_1"><b>Plan</b></li></ol><ol><li id="toc_2"><b>1. Background</b></li></ol>'
            '<p>body text</p><ol><li value="2" id="toc_3"><b>1.1 Sub part</b></li></ol>'
            '<div id="wrap"><p>a long first paragraph</p></div><ol><li>an item</li></ol>')
    ncx = NCX_HEAD + nav_point("Plan", "c.xhtml", nav_point("1. Background", "c.xhtml#toc_2", nav_point(
        "1.1 Sub part", "c.xhtml#toc_3")) + nav_point("Wrapped", "c.xhtml#wrap")) + "</navMap></ncx>"
    out, _ = md(epub_book({"c.xhtml": body}, extra={"OEBPS/toc.ncx": ncx}, manifest_extra=NCX_ITEM))
    assert out.startswith("# 책\n\n# Plan\n\n## 1. Background\n\nbody text\n\n### 1.1 Sub part\n\n"), out
    assert "a long first paragraph\n\n1. an item\n" in out and "**" not in out, out
    nav = page('<nav epub:type="toc"><ol><li><a href="c.xhtml#big">Big  title</a><ol><li>'
               '<a href="c.xhtml#small">Small</a></li></ol></li></ol></nav>')
    body = '<p id="big" class="t">Big title</p><p>text</p><p><a id="small"/>Small</p><p>Small</p>'
    item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    out, _ = md(epub_book({"c.xhtml": body}, css=".t{font-size:2em;font-weight:bold}",
                          extra={"OEBPS/nav.xhtml": nav, "OEBPS/toc.ncx": ncx}, manifest_extra=item + NCX_ITEM))
    assert "# Big title\n\ntext\n\n## Small\n\nSmall\n" in out, out
    out, _ = md(epub_book({"c.xhtml": "<p>Plan</p><p>x\x0e1y</p>"}, extra={"OEBPS/toc.ncx": ncx},
                          manifest_extra=NCX_ITEM))
    assert "# Plan" not in out and "x\x0e1y" in out, out


def test_toc_marking_is_budgeted():
    deep = "<div>" * 59 + "<i></i>" * 1500 + "</div>" * 59
    body = f'<div id="deep">{deep}</div><p id="t">Real title</p><p>text</p>'
    item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'

    def book(labels):
        nav = page('<nav epub:type="toc"><ol>' + "".join(f'<li><a href="c.xhtml#deep">{x}</a></li>' for x in labels)
                   + '<li><a href="c.xhtml#t">Real title</a></li></ol></nav>')
        return epub_book({"c.xhtml": body}, extra={"OEBPS/nav.xhtml": nav}, manifest_extra=item)
    t0 = time.perf_counter()
    out, ctx = md(book(["x" * 300] * 2000))
    assert time.perf_counter() - t0 < 5 and "# Real title\n\ntext" in out and not ctx.skipped["toc"], (
        time.perf_counter() - t0, ctx.skipped)
    t0 = time.perf_counter()
    out, ctx = md(book([f"{k} " + "x" * 300 for k in range(2000)]))
    assert time.perf_counter() - t0 < 8 and ctx.skipped["toc"] == 1 and "Real title" in out, (
        time.perf_counter() - t0, ctx.skipped)


def test_unreadable_toc_counted():
    out, ctx = md(epub_book({"c.xhtml": "<p>Plan</p>"}, extra={"OEBPS/toc.ncx": "<ncx><navMap"},
                            manifest_extra=NCX_ITEM))
    assert "Plan" in out and "# Plan" not in out and ctx.skipped["toc"] == 1, (out, ctx.skipped)
    assert markdown.unread(ctx.skipped) == ["table of contents (its headings, or the rest of them, are not marked)"], \
        ctx.skipped


def test_placeholder_title_left_out():
    out, _ = md(epub_book({"c.xhtml": "<p>본문</p>"}, title="Unknown"))
    assert out == "본문\n", out
    out, _ = md(epub_book({"c.xhtml": "<p>본문</p>"}, title=" unknown "))
    assert out == "본문\n", out


def test_mathml_becomes_tex():
    def tex(math):
        out, _ = md(epub_book({"c.xhtml": f"<p>f: {math}</p>"}, title=""))
        return out.strip()
    assert tex("<math><mfrac><mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow><msup><mi>c</mi><mn>2</mn></msup>"
               "</mfrac></math>") == "f: $\\frac{a+b}{c^{2}}$"
    assert tex("<math><msqrt><mi>x</mi></msqrt><mo>=</mo><mroot><mi>y</mi><mn>3</mn></mroot></math>") \
        == "f: $\\sqrt{x}=\\sqrt[3]{y}$"
    assert tex("<math><msubsup><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></msubsup>"
               "<msub><mi>x</mi><mi>i</mi></msub><mo>≤</mo><mi>α</mi></math>") \
        == "f: $\\sum_{i=1}^{n}x_{i}\\le\\alpha$"
    assert tex("<math><mi>sin</mi><mo>(</mo><mi>θ</mi><mo>)</mo><mtext>if ok</mtext></math>") \
        == "f: $\\sin(\\theta)\\text{if ok}$"
    assert tex("<math><munderover><mo>∫</mo><mn>0</mn><mn>1</mn></munderover><mover><mi>v</mi><mo>→</mo></mover>"
               "</math>") == "f: $\\int_{0}^{1}\\vec{v}$"
    assert tex('<math><semantics><mi>z</mi><annotation encoding="application/x-tex">z_0</annotation>'
               "</semantics></math>") == "f: $z_0$"
    assert tex('<math alttext="x squared"><msup><mi>x</mi><mn>2</mn></msup></math>') == "f: $x^{2}$"
    assert tex('<math alttext="q"></math>') == "f: $q$"
    got = tex('<math alttext="\\mathbb{R}^n"><msup><mi mathvariant="double-struck">R</mi><mi>n</mi>'
              '</msup></math>')
    assert got == "f: ${\\mathbb{R}}^{n}$", got
    got = tex('<math><mi mathvariant="bold">v</mi><mo>∈</mo><mi mathvariant="script">F</mi>'
              '<mi mathvariant="normal">kg</mi></math>')
    assert got == "f: $\\mathbf{v}\\in\\mathcal{F}\\mathrm{kg}$", got
    assert tex('<math alttext="E=mc^2"><apply><eq/><ci>E</ci><apply><times/><ci>m</ci><apply><power/><ci>c</ci>'
               '<cn>2</cn></apply></apply></apply></math>') == "f: $E=mc^2$"
    assert tex('<m:math xmlns:m="http://www.w3.org/1998/Math/MathML"><m:mi>k</m:mi></m:math>') == "f: $k$"
    deep = "<mrow>" * 300 + "<mi>d</mi>" + "</mrow>" * 300
    assert "d" in tex(f"<math>{deep}</math>")


def test_mathml_fonts_and_annotations():
    """mathvariant comes down from mstyle and math, a token's own overriding it; a styled letter is not styled again;
    the two-style variants keep both; the presentation in annotation-xml stands for content markup, and alttext for
    a tree with content markup in it; a text/plain annotation is not TeX."""
    def tex(math):
        out, _ = md(epub_book({"c.xhtml": f"<p>f: {math}</p>"}, title=""))
        return out.strip()[3:]
    cases = [
        ('<math><mstyle mathvariant="bold"><mi>v</mi></mstyle><mo>+</mo><mi>w</mi></math>', "$\\mathbf{v}+w$"),
        ('<math mathvariant="bold"><mi>x</mi></math>', "$\\mathbf{x}$"),
        ('<math><mstyle mathvariant="bold"><mi mathvariant="normal">x</mi><mn>2</mn></mstyle></math>',
         "$x\\mathbf{2}$"),
        ('<math><mi mathvariant="double-struck">ℝ</mi></math>', "$\\mathbb{R}$"),
        ('<math><mi mathvariant="bold-sans-serif">A</mi></math>', "$\\boldsymbol{\\mathsf{A}}$"),
        ('<math><mi mathvariant="sans-serif-bold-italic">a</mi></math>',
         "$\\boldsymbol{\\mathsf{\\mathit{a}}}$"),
        ('<math><mi mathvariant="bold-script">C</mi></math>', "$\\boldsymbol{\\mathcal{C}}$"),
        ('<math><mi mathvariant="italic">ab</mi><mi mathvariant="italic">x</mi></math>', "$\\mathit{ab}x$"),
        ('<math><semantics><apply><eq/><ci>E</ci><ci>m</ci></apply><annotation-xml encoding="MathML-Presentation">'
         '<mrow><mi>E</mi><mo>=</mo><mi>m</mi></mrow></annotation-xml></semantics></math>', "$E=m$"),
        ('<math alttext="x=a+b"><mrow><mi>x</mi><mo>=</mo><apply><plus/><ci>a</ci><ci>b</ci></apply></mrow></math>',
         "$x=a+b$"),
        ('<math alttext="z"><semantics><mi>z</mi><annotation encoding="text/plain">zed</annotation></semantics>'
         '</math>', "$z$"),
        ('<math alttext="q"><semantics><apply><plus/><ci>a</ci><ci>b</ci></apply>'
         '<annotation encoding="text/plain">a plus b</annotation></semantics></math>', "$q$"),
    ]
    bad = [(m, got, want) for m, want in cases if (got := tex(m)) != want]
    assert not bad, bad


def test_visibility_hidden_is_hidden():
    out, _ = md(epub_book({"c.xhtml": '<p>shown</p><p class="v">secret</p><p style="visibility: collapse">x2</p>'
                                      '<p>a <span style="visibility:hidden">b</span> c</p>'},
                          css=".v{visibility:hidden}"))
    assert "secret" not in out and "x2" not in out and " b " not in out and "(3 hidden places left out)" in out, out
    c = xhtml.Css([".v{visibility:hidden} .w{visibility:visible} .n{display:none;visibility:visible}"])
    assert c.visibility(xhtml.Node("p", {"class": "v"}, [])) == "hidden"
    assert c.visibility(xhtml.Node("p", {"class": "w"}, [])) == "visible"
    assert c.visibility(xhtml.Node("p", {"class": "n"}, [])) == "none"
    assert c.visibility(xhtml.Node("p", {}, [])) == ""


def test_visible_child_of_a_visibility_hidden_parent_shows():
    body = ('<p style="visibility:hidden">x<span style="visibility:visible">y</span></p>'
            '<div class="v"><p>a1</p><p class="w">b1</p><blockquote><p>c1</p><p class="w">d1</p></blockquote></div>'
            '<p>e1 <span class="v">f1 <b>g1 <span class="w">h1</span></b> i1</span> j1</p>'
            '<ul class="v"><li>k1</li><li class="w">l1</li></ul>'
            '<div style="display:none"><p class="w">gone1</p></div><p hidden=""><span class="w">gone2</span></p>'
            '<p class="v">m1 <span style="display:none">n1 <span class="w">gone3</span></span></p>')
    path = epub_book({"c.xhtml": body}, css=".v{visibility:hidden} .w{visibility:visible}")
    out, _ = md(path)
    for word in ("y", "b1", "d1", "e1", "**h1**", "j1", "- l1"):
        assert word in out, (word, out)
    for word in ("x", "a1", "c1", "f1", "g1", "i1", "k1", "gone", "m1", "n1"):
        assert word not in out.replace("hidden places", ""), (word, out)
    doc = reader.parse(path, Context())
    shown = markdown.render(doc, tmp() / "assets", True)
    assert "(hidden: x)y" in shown and "(hidden) a1" in shown and "_Shown" not in shown, shown


def test_drop_cap_picture_gives_its_letter():
    cap = PNG + b"cap"
    body = ('<p class="nind"><span class="letra"><img alt="M" src="m.png"/></span>R. BENNET was among</p>'
            '<p><img alt="A" src="m.png"/> lone picture</p><p><b><img alt="T" src="m.png"/>he end</b></p>')
    out, _ = md(epub_book({"c.xhtml": body}, extra={"OEBPS/m.png": cap}))
    assert out.startswith("# 책\n\nMR. BENNET was among\n\n"), out
    assert "lone picture\n\n![A](assets/img-001.png)" in out and "**The end**" in out, out
    assert out.count("![") == 1, out
    body = ('<p><img alt="주의" src="m.png"/>전원을 끄십시오</p><p><img alt="OK" src="m.png"/>done</p>'
            '<p><img alt="“I" src="m.png"/>t was late</p>')
    out, _ = md(epub_book({"c.xhtml": body}, extra={"OEBPS/m.png": cap}))
    assert "주의전원" not in out and "OKdone" not in out and "“It was late" in out and out.count("![") == 2, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
