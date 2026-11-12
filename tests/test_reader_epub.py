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
    assert c.display_none(n("p"), ())
    assert not c.display_none(n("p", **{"class": "show"}), ())
    assert not c.display_none(n("p", **{"class": "show", "style": "display:block"}), ())
    assert c.display_none(n("div", **{"class": "y", "style": "display:block"}), ())
    assert c.display_none(n("div", style="display: none"), ())
    assert c.bold(n("span", **{"class": "x"})) and not c.bold(n("span"))
    assert c.display_none(n("div"), (n("div", style="display:none"),)) is True


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
    assert '<th colspan="2">' in out or '<td colspan="2">' in out, out


def test_table_hostile_spans_clamped():
    body = ('<table><tr><td rowspan="999999999" colspan="999999999">a</td><td rowspan="0">b</td></tr>'
            '<tr><td>c</td></tr></table>')
    t0 = time.monotonic()
    out, _ = md(epub_book({"c.xhtml": body}))
    assert time.monotonic() - t0 < 5
    assert 'rowspan="2"' in out and 'colspan="999' not in out and "c" in out, out


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
    body = ('<p><math alttext="x^2"><mi>x</mi></math></p>'
            '<epub:switch><epub:case required-namespace="m"><p>CASE</p></epub:case>'
            '<epub:default><p>DEF</p></epub:default></epub:switch>')
    out, _ = md(epub_book({"c.xhtml": body}))
    assert "$x^2$" in out and "DEF" in out and "CASE" not in out, out


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
    assert xhtml.Css([" " * 2**20 + ".a{display:none}"]).display_none(xhtml.Node("p", {"class": "a"}, []), ()) is False
    many = xhtml.Css(["".join(f".k{i}{{display:none}}" for i in range(20_000))])
    assert not many.display_none(xhtml.Node("p", {"class": "k19999"}, []), ())  # past the selector cap
    assert many.display_none(xhtml.Node("p", {"class": "k5"}, []), ()) and c


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
    assert 'colspan="2"' in out and 'colspan="9"' not in out, out


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
