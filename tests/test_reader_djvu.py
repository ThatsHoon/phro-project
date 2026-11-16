"""DjVu text layers: one-page and bundled files, TXTa and TXTz, INCL, the paragraph and line rules, textless pages,
indirect and NDIR files, and the hostile-container rules (an oversize chunk, overlapping offsets, the pad byte, BZZ_TOTAL).

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
from reader.model import Context, Para, Section, Text  # noqa: E402
from djvu_fix import _text, bzz_encode, chunk, djvm, form, iff, page  # noqa: E402

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
    # the dropped TXTa is counted once, as record: its page is an empty section, not also textless
    assert ctx.skipped["record"] == 1 and ctx.skipped["textless"] == 0
    assert [texts(s) for s in doc.blocks] == [["One"], [], ["Three"]]
    # a dropped image chunk still names the page images
    bad = form(b"DJVU", [INFO, chunk(b"TXTa", _text("Two")), b"Sjbz" + struct.pack(">I", 0x7FFFFFFF)])
    doc, ctx = read(djvm([page("One"), bad]))
    assert ctx.skipped["record"] == 1 and ctx.skipped["page_images"] > 0
    assert [texts(s) for s in doc.blocks] == [["One"], ["Two"]]


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


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
