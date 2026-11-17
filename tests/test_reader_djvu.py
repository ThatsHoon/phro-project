"""DjVu text layers: one-page and bundled files, TXTa and TXTz, INCL, the paragraph and line rules, textless pages,
indirect and NDIR files, and the hostile-container rules (an oversize chunk, overlapping offsets, the pad byte, BZZ_TOTAL).
Annotations (links, comments, the metadata title), NAVM bookmarks as headings, the annotation and bookmark caps, the
page cap and the page loop's deadline.

Run: python -I -X utf8 tests/test_reader_djvu.py
"""
import random
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import markdown  # noqa: E402
from reader.formats import djvu  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Heading, Link, Para, Section, Text  # noqa: E402
from djvu_fix import _text, bzz_encode, chunk, djvm, form, iff, navm, page  # noqa: E402

INFO = chunk(b"INFO", struct.pack(">HHBB", 100, 100, 26, 0) + struct.pack("<HBB", 300, 22, 0))


def read(data):
    ctx = Context()
    return djvu.parse_bytes(data, ctx), ctx


def md(doc, ctx):
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", False, ctx.skipped)


def texts(section):
    return [b.inlines[0].text for b in section.blocks if isinstance(b, Para)]


def refused(data):
    try:
        read(data)
    except ReadError as e:
        return e
    raise AssertionError("ReadError not raised")


def test_single_page_txta():
    doc, ctx = read(iff(page("Hello\nworld")))
    assert len(doc.blocks) == 1
    sec = doc.blocks[0]
    assert isinstance(sec, Section) and sec.kind == "page" and sec.number == 1 and sec.title == ""
    assert texts(sec) == ["Hello world"]
    out = md(doc, ctx)
    assert "## Page 1\n" in out and "Hello world" in out, out


def test_bundled_txtz_titles():
    doc, ctx = read(djvm([page("A", z=True), page("B")], titles=["Intro", "2"]))
    assert [(s.number, s.title) for s in doc.blocks] == [(1, "Intro"), (2, "")]
    assert [texts(s) for s in doc.blocks] == [["A"], ["B"]]
    out = md(doc, ctx)
    assert "## Page 1: Intro" in out and "## Page 2\n" in out, out


def test_separators():
    paras = djvu._text("One\x1dTwo\x0bThree\x1fFour")
    assert [p.inlines[0].text for p in paras] == ["One", "Two", "Three", "Four"]
    assert all(isinstance(p, Para) and len(p.inlines) == 1 and isinstance(p.inlines[0], Text) for p in paras)
    # empty paragraphs and blank lines are left out; spaces around a line are trimmed
    assert [p.inlines[0].text for p in djvu._text("\x1f\x1f  a  \n\n b\x1f")] == ["a b"]


def test_line_joins():
    def one(raw):
        return [p.inlines[0].text for p in djvu._text(raw)]
    assert one("日本\n語") == ["日本語"]
    assert one("ひら\nがな") == ["ひらがな"]
    assert one("infor-\nmation") == ["information"]
    assert one("Self-\nTest") == ["Self- Test"]
    assert one("1-\nmore") == ["1- more"]       # the hyphen follows a digit, not a letter
    assert one("한국\n어") == ["한국 어"]        # Hangul is not Han or Kana: a space
    assert one("end.\nNext") == ["end. Next"]


def test_textless_page_counted():
    doc, ctx = read(djvm([page("Text here"), page(None)]))
    assert ctx.skipped["textless"] == 1
    assert [s.number for s in doc.blocks] == [1]
    assert "1 page with no text layer" in md(doc, ctx)


def test_some_pages_textless():
    doc, ctx = read(djvm([page(None), page("First"), page(None), page("Second", z=True), page(None)]))
    assert ctx.skipped["textless"] == 3
    assert [s.number for s in doc.blocks] == [2, 4]
    out = md(doc, ctx)
    assert "First" in out and "Second" in out and "3 pages with no text layer" in out, out


def test_all_textless_unsupported():
    e = refused(djvm([page(None), page(None, images=True)]))
    assert e.kind == "unsupported" and e.detail == "no text layer: 2 image-only pages (OCR is out of scope)", e.detail
    e = refused(iff(page(None)))
    assert e.kind == "unsupported" and e.detail == "no text layer: 1 image-only page (OCR is out of scope)", e.detail


def test_empty_text_layer():
    doc, ctx = read(iff(page("")))
    assert len(doc.blocks) == 1 and doc.blocks[0].blocks == []
    assert ctx.skipped["textless"] == 0
    assert "## Page 1" in md(doc, ctx)


def test_indirect_and_ndir():
    e = refused(djvm([page("A")], indirect=True))
    assert e.kind == "unsupported" and e.detail == "indirect DjVu (its pages are separate files; bundle it first)"
    e = refused(djvm([page("A")], ndir=True))
    assert e.kind == "unsupported" and e.detail == "DjVu 2 directory (NDIR)", e.detail


def test_incl_shared():
    shared = form(b"DJVI", [chunk(b"TXTz", bzz_encode(_text("Shared words")))])
    data = djvm([page(None, incl="dict.djvi"), page("Own", incl="dict.djvi")], shared=[("dict.djvi", shared)])
    calls = []
    real = djvu.bzz.decode

    def counted(body, limit):
        calls.append(len(body))
        return real(body, limit)

    djvu.bzz.decode = counted
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.decode = real
    assert [texts(s) for s in doc.blocks] == [["Shared words"], ["Shared words", "Own"]]
    assert len(calls) == 2, calls  # the DIRM, then the shared TXTz once
    assert ctx.skipped["textless"] == 0


def test_shared_repeat_charged():
    # shared text a later page repeats is charged to BZZ_TOTAL as if decoded again, so one DJVI included by every
    # page cannot multiply the output without bound
    shared = form(b"DJVI", [chunk(b"TXTa", _text("w" * 500))])
    data = djvm([page(None, incl="s.djvi") for _ in range(3)], shared=[("s.djvi", shared)])
    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 700  # the DIRM (about 60 bytes) and one repeat fit; the second repeat does not
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert [s.number for s in doc.blocks] == [1, 2]
    assert ctx.skipped["textless"] == 1


def test_page_images_named():
    doc, ctx = read(djvm([page("Words", images=True), page("More")]))
    assert ctx.skipped["page_images"] > 0
    assert "page images (text layer only)" in markdown.unread(ctx.skipped)
    doc, ctx = read(iff(page("No pictures")))
    assert "page images (text layer only)" not in markdown.unread(ctx.skipped)


def test_overlapping_offsets_dropped():
    data = bytearray(djvm([page("First"), page("Second")]))
    assert data[24] == 0x81 and struct.unpack(">H", data[25:27])[0] == 2
    data[31:35] = data[27:31]  # the second DIRM offset repeats the first
    doc, ctx = read(bytes(data))
    assert [texts(s) for s in doc.blocks] == [["First"]]
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    # an offset outside the file is dropped too
    data = bytearray(djvm([page("First"), page("Second")]))
    data[31:35] = struct.pack(">I", len(data) + 100)
    doc, ctx = read(bytes(data))
    assert [texts(s) for s in doc.blocks] == [["First"]] and ctx.skipped["record"] == 1
    # one that overlaps another (a FORM:DJVU head planted inside the first page's FORM) is dropped
    data = bytearray(djvm([page("First"), page("Second")]))
    first = struct.unpack(">I", data[27:31])[0]
    data[31:35] = struct.pack(">I", first + 12)
    data[first + 12:first + 16] = b"FORM"  # INFO's id and the start of its data rewritten (in memory only)
    data[first + 20:first + 24] = b"DJVU"
    doc, ctx = read(bytes(data))
    assert [texts(s) for s in doc.blocks] == [["First"]]
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    # a dropped page keeps its number: the pages after it are numbered as DIRM lists them
    data = bytearray(djvm([page("First"), page("Second"), page("Third")]))
    data[31:35] = data[27:31]
    doc, ctx = read(bytes(data))
    assert [(s.number, texts(s)) for s in doc.blocks] == [(1, ["First"]), (3, ["Third"])]
    assert ctx.skipped["record"] == 1


def test_oversize_chunk_record():
    p2 = bytearray(page("Two"))
    at = p2.index(b"TXTa")
    p2[at + 4:at + 8] = struct.pack(">I", 0x7FFFFFFF)
    doc, ctx = read(djvm([page("One"), bytes(p2), page("Three")]))
    # the page is cut short: counted once, as record, never also textless; with nothing read it has no section
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    assert [(s.number, texts(s)) for s in doc.blocks] == [(1, ["One"]), (3, ["Three"])]
    # a dropped image chunk still names the page images; the text read before it is kept
    bad = form(b"DJVU", [INFO, chunk(b"TXTa", _text("Two")), b"Sjbz" + struct.pack(">I", 0x7FFFFFFF)])
    doc, ctx = read(djvm([page("One"), bad]))
    assert ctx.skipped["record"] == 1 and ctx.skipped["page_images"] > 0
    assert [texts(s) for s in doc.blocks] == [["One"], ["Two"]]
    # an oversize image chunk before the text (text comes after the image chunks in real files): one count, record
    bad = bytearray(form(b"DJVU", [INFO, chunk(b"BG44", bytes(10)), chunk(b"TXTa", _text("Lost"))]))
    at = bad.index(b"BG44")
    bad[at + 4:at + 8] = struct.pack(">I", 0x7FFFFFFF)
    doc, ctx = read(djvm([page("One"), bytes(bad)]))
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0 and ctx.skipped["page_images"] > 0
    assert [s.number for s in doc.blocks] == [1]


def test_cut_single_page_damaged():
    # a one-page file cut inside its image data (a partial download) is damaged, not "no text layer"
    txta = chunk(b"TXTa", _text("Words"))
    data = iff(form(b"DJVU", [INFO, chunk(b"Sjbz", bytes(200)), txta]))
    e = refused(data[:-60])
    assert e.kind == "damaged", (e.kind, e.detail)
    # cut on a chunk boundary (the FORM says more bytes follow than the file has): damaged too
    e = refused(data[:-len(txta)])
    assert e.kind == "damaged", (e.kind, e.detail)
    # text read before the cut is kept, and the cut is counted once
    data = iff(form(b"DJVU", [INFO, txta, chunk(b"Sjbz", bytes(200))]))
    doc, ctx = read(data[:-60])
    assert [texts(s) for s in doc.blocks] == [["Words"]] and ctx.skipped["record"] == 1
    assert ctx.skipped["textless"] == 0
    doc, ctx = read(data[:-208])  # the Sjbz chunk gone whole: the file still ends before its FORM
    assert [texts(s) for s in doc.blocks] == [["Words"]] and ctx.skipped["record"] == 1
    # a bundle whose every page is cut: damaged too
    bad = bytearray(page("Gone", images=True))
    at = bad.index(b"Sjbz")
    bad[at + 4:at + 8] = struct.pack(">I", 0x7FFFFFFF)
    e = refused(djvm([bytes(bad), bytes(bad)]))
    assert e.kind == "damaged", (e.kind, e.detail)


def test_over_cap_chunk_walk_goes_on():
    # a chunk that fits its page but is over a cap is skipped (record) and the page's later chunks are still read
    one = form(b"DJVU", [INFO, chunk(b"TXTz", bzz_encode(_text("y" * 3000))), chunk(b"TXTa", _text("after"))])
    saved = djvu.bzz.BZZ_CHUNK
    djvu.bzz.BZZ_CHUNK = 1000
    try:
        doc, ctx = read(iff(one))
    finally:
        djvu.bzz.BZZ_CHUNK = saved
    assert [texts(s) for s in doc.blocks] == [["after"]]
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0


def test_budget_spent_not_unsupported():
    # text dropped by BZZ_TOTAL is not "no text layer": the file is not refused as a scan
    z = chunk(b"TXTz", bzz_encode(_text("a" * 400)))
    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 500  # one 403-byte chunk fits, the second spends the budget
    try:
        doc, ctx = read(iff(form(b"DJVU", [INFO, z, z])))
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert doc.blocks == [] and ctx.skipped["textless"] == 1
    # the review's probe: the budget spent on page 1 (three chunks), page 2 after it
    djvu.bzz.BZZ_TOTAL = 1000
    try:
        doc, ctx = read(djvm([form(b"DJVU", [INFO, z, z, z]), page(None)]))
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert doc.blocks == [] and ctx.skipped["textless"] == 2


def test_bzz_total():
    data = djvm([page("Short one", z=True), page("x" * 2000, z=True), page("third", z=True), page("fourth")])
    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 1000  # the DIRM and page 1 fit; page 2's 2,003 bytes pass it
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert [texts(s) for s in doc.blocks] == [["Short one"]]
    assert ctx.skipped["textless"] == 3 and ctx.skipped["record"] == 0
    # the budget is the document's: chunks that each fit add up, and the one that passes what is left is cut
    data = djvm([page("a" * 400, z=True) for _ in range(3)])
    djvu.bzz.BZZ_TOTAL = 1000  # the DIRM (45 bytes) and pages 1 and 2 (403 each) leave 149 for page 3
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert [s.number for s in doc.blocks] == [1, 2]
    assert ctx.skipped["textless"] == 1 and ctx.skipped["record"] == 0


def test_bzz_chunk_record():
    data = djvm([page("y" * 3000, z=True), page("kept", z=True)])
    saved = djvu.bzz.BZZ_CHUNK
    djvu.bzz.BZZ_CHUNK = 1000  # page 1's chunk passes BZZ_CHUNK: that chunk counts record, page 2 is read
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_CHUNK = saved
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    assert [texts(s) for s in doc.blocks] == [[], ["kept"]]
    # a cut TXTz is a damaged chunk (Truncated, or no EOB marker): record
    rng = random.Random(6)
    body = bzz_encode(_text("".join(rng.choice("abcdefghij ") for _ in range(3000))))
    cut = form(b"DJVU", [INFO, chunk(b"TXTz", body[:len(body) // 2])])
    doc, ctx = read(djvm([cut, page("ok")]))
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    assert [texts(s) for s in doc.blocks] == [[], ["ok"]]


def test_odd_length_pad():
    one = form(b"DJVU", [INFO, chunk(b"XTRA", b"abc"), chunk(b"TXTa", _text("After pad"))])
    assert len(one) == 12 + 18 + 8 + 3 + 1 + 8 + 12 and one[12 + 18 + 8 + 3] == 0  # the pad after the 3-byte chunk
    doc, ctx = read(iff(one))
    assert [texts(s) for s in doc.blocks] == [["After pad"]] and ctx.skipped["record"] == 0


def test_page_cap():
    saved = djvu.DJVU_PAGES
    djvu.DJVU_PAGES = 2
    try:
        doc, ctx = read(djvm([page("a"), page("b"), page("c")]))
    finally:
        djvu.DJVU_PAGES = saved
    assert ctx.skipped["page_cut"] == 1
    assert [texts(s) for s in doc.blocks] == [["a"], ["b"]]


def test_page_guard():
    # a page whose reading fails is counted `page`; the pages around it are kept with their numbers
    real = djvu._text

    def boom(raw):
        if raw == "Bad":
            raise ValueError("boom")
        return real(raw)

    djvu._text = boom
    try:
        doc, ctx = read(djvm([page("Good"), page("Bad"), page("Also")]))
    finally:
        djvu._text = real
    assert [(s.number, texts(s)) for s in doc.blocks] == [(1, ["Good"]), (3, ["Also"])]
    assert ctx.skipped["page"] == 1 and ctx.skipped["textless"] == 0


def test_not_djvu_damaged():
    for data in (b"", b"AT&T", b"AT&TFORM", b"%PDF-1.4", b"AT&T" + chunk(b"INFO", b"xx")):
        e = refused(data)
        assert e.kind == "damaged", (data, e.kind)
    e = refused(iff(form(b"DJVM", [chunk(b"NAVM", b"")])))
    assert e.kind == "damaged", e.kind


def test_parse_path_and_started():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "one.djvu"
        p.write_bytes(iff(page("On disk")))  # a clean fixture
        doc = djvu.parse(p, Context(), started=0.0)
    assert texts(doc.blocks[0]) == ["On disk"]


def test_short_and_spent_counted_once():
    # a one-page file cut at its FORM's end whose page also spends the BZZ budget: one event, one kind (textless)
    z = chunk(b"TXTz", bzz_encode(_text("a" * 400)))
    data = iff(form(b"DJVU", [INFO, z, z, chunk(b"Sjbz", bytes(200))]))[:-208]  # the Sjbz chunk gone whole
    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 500  # the first chunk fits, the second spends the budget
    try:
        doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert doc.blocks == []
    assert ctx.skipped["textless"] == 1 and ctx.skipped["record"] == 0, ctx.skipped


def comments(section):
    return [[(type(i).__name__, i.text if isinstance(i, Text) else (i.url, i.inlines[0].text)) for i in c.inlines]
            for c in section.comments]


def test_link_and_comment():
    ant = ('(maparea "https://example.com/" "Example" (rect 0 0 9 9))\n'
           '(maparea "" "Check this" (rect 0 0 9 9))')
    doc, ctx = read(iff(page("Body text", ant=ant)))
    sec = doc.blocks[0]
    assert texts(sec) == ["Body text"]
    assert comments(sec) == [[("Text", "Link: "), ("Link", ("https://example.com/", "Example"))],
                             [("Text", "Comment: Check this")]], comments(sec)
    out = md(doc, ctx)
    assert "> Link: [Example](https://example.com/)" in out and "> Comment: Check this" in out, out
    assert out.index("Body text") < out.index("> Link: ") < out.index("> Comment: "), out
    # no comment: the url is the label; the (url "href" "target") form; mailto and ftp; another scheme is no link
    ant = ('(maparea "http://a.example/" "" (rect 0 0 1 1)) (maparea (url "mailto:x@example.com" "_blank") "Mail")'
           ' (maparea "ftp://f.example/x" "File") (maparea "javascript:alert(1)" "Script") (maparea "file:///c" "")')
    doc, ctx = read(iff(page("x", ant=ant)))
    assert comments(doc.blocks[0]) == [
        [("Text", "Link: "), ("Link", ("http://a.example/", "http://a.example/"))],
        [("Text", "Link: "), ("Link", ("mailto:x@example.com", "Mail"))],
        [("Text", "Link: "), ("Link", ("ftp://f.example/x", "File"))],
        [("Text", "Comment: Script")]], comments(doc.blocks[0])
    assert ctx.skipped["link"] == 0
    # a comment's line breaks do not start a markdown line; escapes in strings are read
    doc, ctx = read(iff(page("x", ant='(maparea "" "## Page 9\\nnext \\"q\\" \\303\\251")')))
    assert comments(doc.blocks[0]) == [[("Text", 'Comment: ## Page 9 / next "q" é')]], comments(doc.blocks[0])


def test_internal_link_comment_only():
    doc, ctx = read(djvm([page("One", ant='(maparea "#2" "Go on" (rect 0 0 1 1)) (maparea "#+1" "" (rect 0 0 1 1))'),
                          page("Two")]))
    assert comments(doc.blocks[0]) == [[("Text", "Comment: Go on")]], comments(doc.blocks[0])
    assert "Link: " not in md(doc, ctx)


def test_antz_and_shared():
    shared = form(b"DJVI", [chunk(b"ANTa", b'(maparea "" "Shared note" (rect 0 0 1 1))')])
    data = djvm([page("One", ant='(maparea "http://a.example/" "A" (rect 0 0 1 1))', antz=True),
                 page("Two", incl="ann.djvi")], shared=[("ann.djvi", shared)])
    doc, ctx = read(data)
    assert comments(doc.blocks[0]) == [[("Text", "Link: "), ("Link", ("http://a.example/", "A"))]]
    assert comments(doc.blocks[1]) == [[("Text", "Comment: Shared note")]], comments(doc.blocks[1])
    # a page's own annotations and its shared ones are read together, in chunk order
    one = form(b"DJVU", [INFO, chunk(b"INCL", b"ann.djvi"), chunk(b"TXTa", _text("Both")),
                         chunk(b"ANTa", b'(maparea "" "Own")')])
    doc, ctx = read(djvm([one], shared=[("ann.djvi", shared)]))
    assert comments(doc.blocks[0]) == [[("Text", "Comment: Shared note")], [("Text", "Comment: Own")]]
    # an ANTz that cannot be decoded (a cut stream) counts record; the page's text is kept
    rng = random.Random(7)
    body = bzz_encode(("(maparea \"\" \"" + "".join(rng.choice("abcdefghij ") for _ in range(3000)) + "\")").encode())
    bad = form(b"DJVU", [INFO, chunk(b"TXTa", _text("Kept")), chunk(b"ANTz", body[:len(body) // 2])])
    doc, ctx = read(iff(bad))
    assert texts(doc.blocks[0]) == ["Kept"] and doc.blocks[0].comments == [] and ctx.skipped["record"] == 1


def test_shared_annotations_charged_per_repeat():
    # the review's probe shape: one shared ANTz of 256 long mapareas, included by 1,000 pages. Each repeat's lines
    # are charged to BZZ_TOTAL as shared text is; a page whose repeat does not fit loses its annotations only,
    # counted link once, and keeps its text
    import time
    ant = " ".join(f'(maparea "" "{i:03d}{"c" * 400}" (rect 0 0 1 1))' for i in range(256))
    shared = form(b"DJVI", [chunk(b"ANTz", bzz_encode(ant.encode()))])
    pages = 1000
    data = djvm([page(f"p{k}", incl="a.djvi") for k in range(pages)], shared=[("a.djvi", shared)])
    calls = []
    real = djvu._sexp

    def counted(text, ctx=None):
        calls.append(len(text))
        return real(text, ctx)

    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 2 * 2**20
    djvu._sexp = counted
    try:
        t0 = time.monotonic()
        doc, ctx = read(data)
        took = time.monotonic() - t0
    finally:
        djvu.bzz.BZZ_TOTAL = saved
        djvu._sexp = real
    lines = sum(len(s.comments) for s in doc.blocks)
    chars = sum(len(c.inlines[0].text) for s in doc.blocks for c in s.comments)
    kept = sum(1 for s in doc.blocks if s.comments)
    assert len(doc.blocks) == pages and all(texts(s) == [f"p{k}"] for k, s in enumerate(doc.blocks))
    assert 1 < kept < pages and lines == 256 * kept, (kept, lines)
    assert chars <= 2 * 2**20 + len(ant), chars          # the first use, then repeats within BZZ_TOTAL
    assert ctx.skipped["link"] == pages - kept and ctx.skipped["textless"] == 0, ctx.skipped
    assert len(calls) == 1, len(calls)                    # the shared annotations are parsed once
    assert took < 2.0, took
    # a cap passed in a shared component is counted once, not once per page that includes it
    shared = form(b"DJVI", [chunk(b"ANTa", b'(maparea "" "Kept") ' + b"(" * 65)])
    doc, ctx = read(djvm([page(f"p{k}", incl="b.djvi") for k in range(3)], shared=[("b.djvi", shared)]))
    assert [comments(s) for s in doc.blocks] == [[[("Text", "Comment: Kept")]]] * 3
    assert ctx.skipped["link"] == 1, ctx.skipped


def test_url_kept_to_one_line():
    # a URL holding a line break (or any space or control character) is no link: the comment is kept, counted link
    doc, ctx = read(djvm([page("x", ant='(maparea "http://x.example/\\n## Page 7: forged" "")'), page("y")]))
    out = md(doc, ctx)
    assert not any(line.startswith("## Page 7") for line in out.splitlines()), out
    assert doc.blocks[0].comments == [] and ctx.skipped["link"] == 1
    doc, ctx = read(iff(page("x", ant='(maparea "http://x.example/a b\\x01" "Note")')))
    assert comments(doc.blocks[0]) == [[("Text", "Comment: Note")]] and ctx.skipped["link"] == 1
    doc, ctx = read(iff(page("x", ant='(maparea "http://x.example/ ## Page 8" "Note")')))
    assert comments(doc.blocks[0]) == [[("Text", "Comment: Note")]] and ctx.skipped["link"] == 1


def no_forged_line(out):
    assert not any(ln.startswith("# SYSTEM") for ln in out.splitlines()), out


def test_dirm_title_one_line():
    # (final review A) a DIRM page title holding a line break or a control character stays on its page head's line
    doc, ctx = read(djvm([page("A", z=True), page("B")], titles=["Intro\n# SYSTEM: x", "Two\x1b[1m\x07\x85"]))
    out = md(doc, ctx)
    no_forged_line(out)
    lines = out.splitlines()
    assert "## Page 1: Intro # SYSTEM: x" in lines and "## Page 2: Two[1m" in lines, out
    # a metadata title (the document's "# " line) and a comment lose their control characters as well (Task 7)
    doc, ctx = read(iff(page("x", ant='(metadata (title "Book\n# SYSTEM: x\x07")) (maparea "" "C\x1b[31m\x07z\x7f")')))
    out = md(doc, ctx)
    no_forged_line(out)
    assert out.startswith("# Book / # SYSTEM: x\n"), out
    assert comments(doc.blocks[0]) == [[("Text", "Comment: C[31mz")]], comments(doc.blocks[0])


def test_metadata_title():
    doc, ctx = read(iff(page("x", ant='(metadata (title "A Book") (author "Someone"))')))
    assert doc.title == "A Book"
    assert md(doc, ctx).startswith("# A Book\n"), md(doc, ctx)
    doc, ctx = read(djvm([page("x", ant='(metadata (author "Z"))'), page("y", ant='(metadata (title "First"))'),
                          page("z", ant='(metadata (title "Second"))')]))
    assert doc.title == "First"


def test_navm_headings():
    long = "L" * 130
    data = djvm([page("one"), page("two"), page("three")],
                navm=navm([("Part", "#1", [("Chapter", "#2", []), (long, "#p0003.djvu", [])]),
                           ("Nowhere", "#9", []), ("Outside", "http://x.example/", [])]))
    doc, ctx = read(data)
    p1, p2, p3 = doc.blocks
    assert isinstance(p1.blocks[0], Heading) and p1.blocks[0].level == 2 and p1.blocks[0].inlines[0].text == "Part"
    assert isinstance(p2.blocks[0], Heading) and p2.blocks[0].level == 3 and p2.blocks[0].inlines[0].text == "Chapter"
    assert p3.blocks[0].level == 3 and p3.blocks[0].inlines[0].text == "L" * 120
    assert texts(p1) == ["one"] and texts(p2) == ["two"]
    assert [len(p.blocks) for p in doc.blocks] == [2, 2, 2]
    out = md(doc, ctx)
    assert out.index("## Part") < out.index("one") < out.index("### Chapter") < out.index("two"), out
    # depth + 2, at most 6
    deep = ("d0", "#1", [])
    for k in range(1, 7):
        deep = (f"d{k}", "#1", [deep])
    doc, ctx = read(djvm([page("one")], navm=navm([deep])))
    assert [b.level for b in doc.blocks[0].blocks if isinstance(b, Heading)] == [2, 3, 4, 5, 6, 6, 6]
    # a NAVM that cannot be decoded counts record; the pages are read
    data = djvm([page("one")], navm=chunk(b"NAVM", b"\xff\xff\xff"))
    doc, ctx = read(data)
    assert texts(doc.blocks[0]) == ["one"] and ctx.skipped["record"] == 1
    # a textless page keeps its heading (and is still counted textless)
    doc, ctx = read(djvm([page("one"), page(None)], navm=navm([("Plate", "#2", [])])))
    assert [s.number for s in doc.blocks] == [1, 2] and ctx.skipped["textless"] == 1
    assert doc.blocks[1].blocks[0].inlines[0].text == "Plate"


def test_sexp_caps():
    got = djvu._sexp('(a "b\\"c" (d 1)) stray ) (e)')
    assert got == [["a", 'b"c', ["d", "1"]], ["e"]], got
    assert isinstance(got[0][0], djvu._Sym) and not isinstance(got[0][1], djvu._Sym)
    two = '(maparea "" "One" (rect 0 0 1 1)) (maparea "" "Two" (rect 0 0 1 1))'
    saved = djvu.ANT_AREAS
    djvu.ANT_AREAS = 1
    try:
        doc, ctx = read(iff(page("x", ant=two)))
    finally:
        djvu.ANT_AREAS = saved
    assert comments(doc.blocks[0]) == [[("Text", "Comment: One")]] and ctx.skipped["link"] == 1
    # depth 65: the rest is ignored and counted link once
    deep = '(maparea "" "Kept") ' + "(" * 65 + ")" * 65 + ' (maparea "" "Lost")'
    doc, ctx = read(iff(page("x", ant=deep)))
    assert comments(doc.blocks[0]) == [[("Text", "Comment: Kept")]] and ctx.skipped["link"] == 1, ctx.skipped
    doc, ctx = read(iff(page("x", ant='(maparea "" "Kept") ' + "(" * 64 + ")" * 64)))
    assert ctx.skipped["link"] == 0
    # DJVU_ANT characters and ANT_STRING per string
    for name, value in (("DJVU_ANT", 30), ("ANT_STRING", 10)):
        saved = getattr(djvu, name)
        setattr(djvu, name, value)
        try:
            doc, ctx = read(iff(page("x", ant='(maparea "" "Kept") (maparea "" "Lost, a long one")')))
        finally:
            setattr(djvu, name, saved)
        assert comments(doc.blocks[0]) == [[("Text", "Comment: Kept")]] and ctx.skipped["link"] == 1, name
    # an expression that never closes is dropped and counted once too
    ctx = Context()
    assert djvu._sexp('(a b) (c "d', ctx) == [["a", "b"]] and ctx.skipped["link"] == 1


def test_bookmark_caps():
    data = djvm([page("one"), page("two")], navm=navm([("First", "#1", [("Inner", "#1", [])]), ("Second", "#2", [])]))
    saved = djvu.DJVU_BOOKMARKS
    djvu.DJVU_BOOKMARKS = 1
    try:
        doc, ctx = read(data)
    finally:
        djvu.DJVU_BOOKMARKS = saved
    assert [[b.inlines[0].text for b in s.blocks if isinstance(b, Heading)] for s in doc.blocks] == [["First"], []]
    saved = djvu.BOOKMARK_DEPTH
    djvu.BOOKMARK_DEPTH = 1  # the top level only; the deeper one is walked past, not used
    try:
        doc, ctx = read(data)
    finally:
        djvu.BOOKMARK_DEPTH = saved
    assert [[b.inlines[0].text for b in s.blocks if isinstance(b, Heading)] for s in doc.blocks] == [["First"],
                                                                                                    ["Second"]]


def test_deadline_page_cut():
    t = [0.0]

    def clock():  # 20 s per call: started at 20, page 1 at 40 (20 s in), page 2 at 60 (40 s, past 36)
        t[0] += 20.0
        return t[0]

    ctx = Context()
    doc = djvu.parse_bytes(djvm([page("a"), page("b"), page("c")]), ctx, clock=clock)
    assert [texts(s) for s in doc.blocks] == [["a"]]
    assert ctx.skipped["page_cut"] == 2
    # nothing read before the deadline: the output (empty) is kept, not refused
    t[0] = 0.0
    ctx = Context()
    doc = djvu.parse_bytes(djvm([page("a"), page("b")]), ctx, started=-100.0, clock=clock)
    assert doc.blocks == [] and ctx.skipped["page_cut"] == 2


class Decodes:
    """djvu's bzz.decode, counted: the calls, and the symbols the failing ones report."""

    def __init__(self):
        self.calls, self.failed_work, self.real = 0, 0, djvu.bzz.decode

    def __call__(self, data, limit):
        self.calls += 1
        try:
            return self.real(data, limit)
        except (djvu.bzz.Limit, djvu.bzz.Truncated, ReadError) as e:
            self.failed_work += e.work
            raise

    def __enter__(self):
        djvu.bzz.decode = self
        return self

    def __exit__(self, *exc):
        djvu.bzz.decode = self.real


def test_failed_bzz_charged():
    # (final review B) the review's probe: a 13-byte TXTz that decodes 2^20 - 2 symbols from its padding and fails
    # for want of an EOB marker. Each failure is charged to the document's BZZ budget for the symbols it decoded, so
    # many on one page stop once the budget is spent; uncharged, every one of them ran (about 0.2 s each)
    probe = chunk(b"TXTz", bzz_encode(b"a" * (2**20 - 3), eob=False))
    assert len(probe) < 24, len(probe)
    data = iff(form(b"DJVU", [INFO] + [probe] * 40))
    saved = djvu.bzz.BZZ_TOTAL
    djvu.bzz.BZZ_TOTAL = 3 * 2**20  # read at call time: three failures spend all but 6 symbols of it
    try:
        with Decodes() as dec:
            doc, ctx = read(data)
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert dec.calls == 4 and dec.failed_work == 3 * (2**20 - 2), (dec.calls, dec.failed_work)
    assert doc.blocks == [] and ctx.skipped == {"record": 3, "textless": 1}, ctx.skipped
    # one probe at the default budget: counted record, and the page after it is read (the failed page keeps its
    # empty section, as any page whose text chunk is damaged); the budget is the document's, so with little left a
    # later page's text is cut
    doc, ctx = read(djvm([form(b"DJVU", [INFO, probe]), page("kept", z=True)]))
    assert [texts(s) for s in doc.blocks] == [[], ["kept"]] and ctx.skipped == {"record": 1}, ctx.skipped
    djvu.bzz.BZZ_TOTAL = 2**20 + 100  # the DIRM and the probe leave 57: page 2's 7 bytes decode, page 3's 203 not
    try:
        doc, ctx = read(djvm([form(b"DJVU", [INFO, probe]), page("kept", z=True), page("y" * 200, z=True)]))
    finally:
        djvu.bzz.BZZ_TOTAL = saved
    assert [texts(s) for s in doc.blocks] == [[], ["kept"]], doc.blocks
    assert ctx.skipped == {"record": 1, "textless": 1}, ctx.skipped


def test_deadline_checked_per_chunk():
    # (final review B) the deadline is checked before each BZZ chunk, not only before each page: a page of many
    # chunks stops at the deadline; it and the pages after it count page_cut, once each
    t = [0.0]

    def clock():  # 10 s per call: started at 10; page 1 at 20; its chunks at 30, 40 and 50 (40 s in, past 36)
        t[0] += 10.0
        return t[0]

    z = chunk(b"TXTz", bzz_encode(_text("a")))
    with Decodes() as dec:
        ctx = Context()
        doc = djvu.parse_bytes(djvm([form(b"DJVU", [INFO] + [z] * 5), page("b"), page("c")]), ctx, clock=clock)
    assert dec.calls == 1 + 2, dec.calls  # the DIRM, then page 1's first two chunks
    assert doc.blocks == [] and ctx.skipped == {"page_cut": 3}, ctx.skipped
    # the probe's failures run against the clock as well: no more decodes past the deadline, counted once
    probe = chunk(b"TXTz", bzz_encode(b"a" * (2**20 - 3), eob=False))
    t[0] = 0.0
    with Decodes() as dec:
        ctx = Context()
        doc = djvu.parse_bytes(djvm([page("a"), form(b"DJVU", [INFO] + [probe] * 50)]), ctx, clock=clock)
    assert dec.calls == 1 + 1, dec.calls  # the DIRM; page 2 at 30 (20 s), its first probe at 40, its second at 50
    assert [texts(s) for s in doc.blocks] == [["a"]] and ctx.skipped == {"record": 1, "page_cut": 1}, ctx.skipped


def test_convert_passes_started():
    import reader
    seen = {}
    real = djvu.parse_bytes

    def stub(data, ctx, **kw):
        seen.update(kw)
        return real(data, ctx, **kw)

    djvu.parse_bytes = stub
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "book.djvu"
            p.write_bytes(iff(page("Converted")))  # a clean fixture
            reader.convert(p, Path(d) / "out")
            out = (Path(d) / "out" / "out.md").read_text(encoding="utf-8")
    finally:
        djvu.parse_bytes = real
    assert seen.get("started") == reader.STARTED, seen
    assert "Converted" in out


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
