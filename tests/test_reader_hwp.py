"""HWP 5.0 parser: body text, control characters, bold, headings, numbering, notes, fields, equations, tables,
drawing objects and pictures, sections.

Run: python -I -X utf8 tests/test_reader_hwp.py
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
from reader import limits, markdown  # noqa: E402
from reader.formats import hancom, hwp  # noqa: E402
from reader.formats import hwp_records as hr  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Code, Context, Table  # noqa: E402
from reader_fix import (  # noqa: E402
    hwp_para, hwp_picture, hwp_record, hwp_shape, hwp_table, make_cfb, make_hwp, make_zip,
)
from diagram_fix import mermaid_ok  # noqa: E402

FFFC = "\ufffc"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
RECT = hr.SHAPE_RECT


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def u32(n):
    return struct.pack("<I", n)


def wstr(s):
    return u16(len(s)) + s.encode("utf-16-le")


def deflate(b):
    c = zlib.compressobj(wbits=-15)
    return c.compress(b) + c.flush()


def info(sections=0, bold=(), shapes=(), styles=(), numberings=(), bullets=(), bins=()):
    """DocInfo records. shapes: (head, level, ref); numberings: lists of (format, code) per level; bins: (extension,
    compress[, storage id]) per BinData item, the k-th (1-based) embedding storage k unless given, an extension of
    None writing a link item."""
    out = hwp_record(hr.DOCUMENT_PROPERTIES, 0, u16(sections) + bytes(24))
    for k, (ext, compress, *sid) in enumerate(bins, 1):
        if ext is None:
            out += hwp_record(hr.BIN_DATA, 1, u16(compress << 4) + wstr("C:/x.png") + wstr(""))
        else:
            out += hwp_record(hr.BIN_DATA, 1, u16(1 | compress << 4) + u16(sid[0] if sid else k) + wstr(ext))
    for b in bold:
        cs = bytearray(72)
        struct.pack_into("<I", cs, 46, 2 if b else 0)
        out += hwp_record(hr.CHAR_SHAPE, 1, bytes(cs))
    for head, level, ref in shapes:
        ps = bytearray(54)
        struct.pack_into("<I", ps, 0, (head << 23) | (level << 25))
        struct.pack_into("<H", ps, 30, ref)
        out += hwp_record(hr.PARA_SHAPE, 1, bytes(ps))
    for name in styles:
        out += hwp_record(hr.STYLE, 1, wstr(name) + wstr("") + bytes(2) + struct.pack("<hHH", 0, 0, 0))
    for levels in numberings:
        levels = list(levels) + [("", 0)] * (7 - len(levels))
        body = b"".join(struct.pack("<IhhI", code << 5, 0, 0, 0) + wstr(fmt) for fmt, code in levels)
        out += hwp_record(hr.NUMBERING, 1, body + u16(1) + u32(1) * 7)
    for ch in bullets:
        out += hwp_record(hr.BULLET, 1, bytes(12) + u16(ord(ch)))
    return out


def hwp_file(*sections, doc_info=b"", flags=1, extra=None, bins=None, name="a.hwp"):
    return make_hwp(tmp() / name, list(sections), doc_info=doc_info, flags=flags, extra=extra, bins=bins)


def parse(path):
    ctx = Context()
    return hwp.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, ctx = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden, ctx.skipped)


def denied(path, kind):
    try:
        parse(path)
    except ReadError as e:
        assert e.kind == kind, e
        return e
    raise AssertionError(f"expected ReadError {kind}")


def sub_list(*paras, level=2):
    """A LIST_HEADER and its paragraphs, all at `level` as Hancom writes them."""
    return hwp_record(hr.LIST_HEADER, level, u16(len(paras)) + bytes(6)) + b"".join(paras)


def field(cid, command="", attr=0):
    return cid + u32(attr) + b"\0" + wstr(command)


def raw_para(units: bytes, level=0):
    return (hwp_record(hr.PARA_HEADER, level, struct.pack("<IIHBB", len(units) // 2, 0, 0, 0, 0))
            + hwp_record(hr.PARA_TEXT, level + 1, units))


def unit(code):
    return u16(code)


def ctl(code, cid=b"\0\0\0\0"):
    return u16(code) + cid[::-1] + bytes(8) + u16(code)


# ---- registration and plain text
def test_registered():
    assert "hwp" in reader.BUILT and reader.FORMATS["hwp"] == "hwp"


def test_plain_paragraphs():
    assert md(hwp_file(hwp_para("가나다") + hwp_para("abc"))) == "가나다\n\nabc\n"


def test_uncompressed():
    p = hwp_file(hwp_para("가나다") + hwp_para("abc"), doc_info=info(sections=1), flags=0)
    assert md(p) == "가나다\n\nabc\n"


def test_line_break():
    doc, _ = parse(hwp_file(hwp_para("a\nb")))
    assert len(doc.blocks) == 1
    assert md(hwp_file(hwp_para("a\nb"))) == "a\nb\n"


def test_extended_control_skipped():
    p = hwp_file(hwp_para("x" + FFFC + "y", controls=((11, b"zzzz", b""),)))
    out = md(p)
    assert out == "xy\n", repr(out)


def test_char_classes():
    a, b, c, d, e, f, g, h, z = (s.encode("utf-16-le") for s in "abcdefghz")
    units = (a + unit(0) + b + unit(24) + c + unit(30) + d + unit(31) + e + ctl(9) + f + ctl(5) + g
             + unit(0xD800) + h + "😀".encode("utf-16-le") + unit(13) + z)
    assert md(hwp_file(raw_para(units))) == "abc\u00a0d e fg□h😀\n"
    # a lone low surrogate, and a high one at the very end, become the box too; an odd tail byte is ignored
    assert md(hwp_file(raw_para(a + unit(0xDC00) + b + unit(0xDBFF) + b"\x00"))) == "a□b□\n"


def test_bold_runs():
    p = hwp_file(hwp_para("abcde", chars=((0, 0), (2, 1), (4, 0))), doc_info=info(bold=(False, True)))
    assert md(p) == "ab**cd**e\n"
    # ids past the char shape table are not bold; a pair list with a cut tail keeps its whole pairs
    p = hwp_file(hwp_para("abcde", chars=((0, 7), (3, 99))), doc_info=info(bold=(False, True)))
    assert md(p) == "abcde\n"
    cut = (hwp_record(hr.PARA_HEADER, 0, bytes(12)) + hwp_record(hr.PARA_TEXT, 1, "abcde".encode("utf-16-le"))
           + hwp_record(hr.PARA_CHAR_SHAPE, 1, u32(0) + u32(1) + u32(3) + u32(0) + b"\x01\x02\x03"))
    assert md(hwp_file(cut, doc_info=info(bold=(False, True)))) == "**abc**de\n"


# ---- headings
def test_outline_heading():
    p = hwp_file(hwp_para("제목"), doc_info=info(shapes=((1, 0, 0),)))
    assert md(p) == "# 제목\n"
    p = hwp_file(hwp_para("깊음"), doc_info=info(shapes=((1, 7, 0),)))
    assert md(p) == "###### 깊음\n"


def test_style_heading():
    p = hwp_file(hwp_para("본문 제목"), doc_info=info(styles=("개요 3",)))
    assert md(p) == "### 본문 제목\n"
    p = hwp_file(hwp_para("가", style=0) + hwp_para("나", style=1) + hwp_para("다", style=2),
                 doc_info=info(styles=("제목", "부제목", "바탕글")))
    assert md(p) == "# 가\n\n## 나\n\n다\n"


def test_long_heading_demoted():
    di = info(shapes=((1, 0, 0),))
    out = md(hwp_file(hwp_para("가" * 121), doc_info=di))
    assert "#" not in out and out.startswith("가")
    assert md(hwp_file(hwp_para("가" * 120), doc_info=di)).startswith("# ")
    assert "#" not in md(hwp_file(hwp_para("가" * 41 + "."), doc_info=di))


def test_no_heading_in_note():
    note = sub_list(hwp_para("주석 제목", level=2))
    p = hwp_file(hwp_para("본" + FFFC, controls=((17, b"fn  " + u32(1), note),)), doc_info=info(shapes=((1, 0, 0),)))
    out = md(p)
    assert out == "# 본[^1]\n\n[^1]: 주석 제목\n", repr(out)


# ---- numbering and bullets
def test_numbering():
    di = info(shapes=((2, 0, 1), (2, 1, 1)), numberings=([("^1.", 0), ("^1.^2", 0)],))
    p = hwp_file(hwp_para("a") + hwp_para("b") + hwp_para("c"), doc_info=di)
    assert md(p) == "1. a\n2. b\n3. c\n"
    p = hwp_file(hwp_para("x") + hwp_para("y", shape=1), doc_info=di)
    assert md(p) == "1. x\n  1.1 y\n"


def test_numbering_kinds_and_missing_table():
    di = info(shapes=((2, 0, 1), (2, 0, 9)), numberings=([("^1)", 8)],))
    p = hwp_file(hwp_para("a") + hwp_para("b") + hwp_para("c", shape=1), doc_info=di)
    assert md(p) == "가) a\n나) b\n1. c\n"
    # a format code outside the known kinds counts in digits
    di = info(shapes=((2, 0, 1),), numberings=([("^1.", 15)],))
    assert md(hwp_file(hwp_para("a"), doc_info=di)) == "1. a\n"


def test_outline_numbering_from_secd():
    secd = b"secd" + bytes(14) + u16(1) + bytes(20)
    first = hwp_para(FFFC + "개요", shape=0, controls=((2, secd, b""),))
    di = info(shapes=((1, 0, 0), (1, 1, 0)), numberings=([("^1.", 0), ("^2)", 10)],))
    p = hwp_file(first + hwp_para("둘째", shape=1) + hwp_para("셋째", shape=0), doc_info=di)
    assert md(p) == "# 1. 개요\n\n## ㄱ) 둘째\n\n# 2. 셋째\n"


def test_bullet():
    di = info(shapes=((3, 0, 1),), bullets=("●",))
    p = hwp_file(hwp_para("점") + hwp_para("● 둘") + hwp_para("-5도"), doc_info=di)
    assert md(p) == "- 점\n- 둘\n- -5도\n"


# ---- notes, header and footer, comments
def test_footnote():
    note = sub_list(hwp_para("주석", level=2))
    p = hwp_file(hwp_para("본문" + FFFC, controls=((17, b"fn  " + u32(1), note),)))
    assert md(p) == "본문[^1]\n\n[^1]: 주석\n"
    doc, _ = parse(p)
    assert list(doc.notes) == ["fn-1"]


def test_endnote_and_repeated_numbers():
    fn = sub_list(hwp_para("각주", level=2))
    en = sub_list(hwp_para("미주", level=2))
    s0 = hwp_para("가" + FFFC + FFFC, controls=((17, b"fn  " + u32(1), fn), (17, b"en  " + u32(1), en)))
    s1 = hwp_para("나" + FFFC, controls=((17, b"fn  " + u32(1), sub_list(hwp_para("다시 1", level=2))),))
    doc, _ = parse(hwp_file(s0, s1))
    assert len(doc.notes) == 3 and "fn-1" in doc.notes and "en-1" in doc.notes
    out = markdown.render(doc, tmp() / "a")
    assert "[^1]: 각주" in out and "[^2]: 미주" in out and "[^3]: 다시 1" in out, out


def test_header_footer():
    head = sub_list(hwp_para("머리말", level=2))
    foot = sub_list(hwp_para("꼬리말", level=2))
    p = hwp_file(hwp_para(FFFC + FFFC + "본문", controls=((16, b"head" + u32(0), head), (16, b"foot" + u32(0), foot))))
    doc, _ = parse(p)
    assert doc.headers == ["머리말"] and doc.footers == ["꼬리말"]
    assert md(p) == "Header: 머리말\nFooter: 꼬리말\n\n본문\n"


def test_hidden_comment():
    cm = sub_list(hwp_para("숨은 설명", level=2))
    p = hwp_file(hwp_para("글" + FFFC, controls=((15, b"tcmt", cm),)))
    assert md(p) == "글[^1]\n\n[^1]: Comment: 숨은 설명\n"


# ---- fields
def test_hyperlink_field():
    def link(cmd):
        return hwp_para("앞 " + FFFC + "링크" + FFFC + " 뒤",
                        controls=((3, field(b"%hlk", cmd), b""), (4, b"%hlk", b"")))
    assert md(hwp_file(link("https://example.com;1;0;0;"))) == "앞 [링크](https://example.com) 뒤\n"
    assert "(http://a.b/x;y)" in md(hwp_file(link(r"http\://a.b/x\;y;1")))
    assert md(hwp_file(link(""))) == "앞 링크 뒤\n"


def test_placeholder_field():
    def clk(attr):
        return hwp_para(FFFC + "여기에 입력" + FFFC + "남음", controls=((3, field(b"%clk", "", attr), b""),
                                                                (4, b"%clk", b"")))
    assert md(hwp_file(clk(0))) == "남음\n"
    assert md(hwp_file(clk(1 << 15))) == "여기에 입력남음\n"


def test_other_field_keeps_text():
    p = hwp_para("[" + FFFC + "2026" + FFFC + "]" + FFFC + "x",
                 controls=((3, field(b"%dte", "date"), b""), (4, b"%dte", b""), (11, b"zzzz", b"")))
    assert md(hwp_file(p)) == "[2026]x\n"


def test_nested_fields():
    p = hwp_para(FFFC + "a" + FFFC + "b" + FFFC + "c" + FFFC,
                 controls=((3, field(b"%hlk", "https://e.com;1"), b""), (3, field(b"%dte"), b""),
                           (4, b"%dte", b""), (4, b"%hlk", b"")))
    assert md(hwp_file(p)) == "[abc](https://e.com)\n"


# ---- equations
def eqed(script, attr=0):
    return (11, b"eqed" + u32(attr), hwp_record(hr.EQEDIT, 2, u32(0) + wstr(script) + bytes(8)))


def test_equation():
    tex = hancom.tex("a over b")
    assert tex == r"\frac{a}{b}"
    assert md(hwp_file(hwp_para(FFFC, controls=(eqed("a over b"),)))) == f"$${tex}$$\n"
    assert md(hwp_file(hwp_para("값 " + FFFC, controls=(eqed("a over b", 1),)))) == f"값 ${tex}$\n"
    # an inline equation alone in its paragraph is shown on its own, as hwpx does
    assert md(hwp_file(hwp_para(FFFC, controls=(eqed("a over b", 1),)))) == f"$${tex}$$\n"


# ---- tables
def table_md(rows, **kw):
    return md(hwp_file(hwp_para(FFFC, controls=(hwp_table(rows, **kw),))))


def test_table_simple():
    assert table_md([["a", "b"], ["c", "d"]]) == "|a|b|\n|---|---|\n|c|d|\n"
    # the table follows its paragraph's text
    p = hwp_file(hwp_para("앞" + FFFC, controls=(hwp_table([["a", "b"], ["c", "d"]]),)))
    assert md(p) == "앞\n\n|a|b|\n|---|---|\n|c|d|\n"


def test_table_merged():
    out = table_md([[("합", 2, 1)], ["c", "d"]])
    assert '<td colspan="2">합</td>' in out and "<tr><td>c</td><td>d</td></tr>" in out, out


def test_table_caption():
    out = table_md([["a", "b"], ["c", "d"]], caption="표 1")
    assert out == "|a|b|\n|---|---|\n|c|d|\n\n표 1\n", out


def test_table_cell_paragraphs():
    out = table_md([[["가", "나"], "다"], ["라", "마"]])
    assert out == "|가<br>나|다|\n|---|---|\n|라|마|\n", out


def test_nested_table():
    inner = hwp_para(FFFC, level=2, controls=(hwp_table([["1", "2"], ["3", "4"]], level=4),))
    p = hwp_file(hwp_para(FFFC, controls=(hwp_table([[inner, "z"], ["y", "w"]]),)))
    doc, _ = parse(p)
    nested = doc.blocks[0].rows[0][0].blocks
    assert len(nested) == 1 and isinstance(nested[0], Table) and len(nested[0].rows) == 2, nested
    # the renderer writes a table inside a cell on one line
    out = md(p)
    assert out == "|1 / 2 ; 3 / 4|z|\n|---|---|\n|y|w|\n", out


def test_nested_table_depth_cap():
    def nest(k, level):  # the k-th table, its records at `level`
        cell = (hwp_para("바닥", level=level) if k == 70
                else hwp_para(FFFC, level=level, controls=(nest(k + 1, level + 2),)))
        return hwp_table([[cell, str(k)]], level=level)
    doc, ctx = parse(hwp_file(hwp_para(FFFC, controls=(nest(0, 2),))))
    assert ctx.skipped["depth"] == 1, ctx.skipped
    assert "바닥" not in markdown.render(doc, tmp() / "a", False, ctx.skipped)


def test_table_budget():
    # a table past the document's TABLE_NODES is left out as one past MAX_DEPTH is, each counted under depth
    old, limits.TABLE_NODES = limits.TABLE_NODES, 2
    try:
        doc, ctx = parse(hwp_file(b"".join(hwp_para(FFFC, controls=(hwp_table([[str(k)]]),)) for k in range(4))))
    finally:
        limits.TABLE_NODES = old
    assert ctx.skipped["depth"] == 2 and len(doc.blocks) == 2, (ctx.skipped, doc.blocks)
    assert [b.rows[0][0].blocks[0].inlines[0].text for b in doc.blocks] == ["0", "1"], doc.blocks


# ---- pictures and drawing objects
def pic_file(*controls, bins=(("png", 2),), streams=None, flags=1):
    """One paragraph per control; `bins` are the DocInfo items, `streams` the BinData streams by name."""
    body = b"".join(hwp_para(FFFC, controls=(c,)) for c in controls)
    return hwp_file(body, doc_info=info(bins=bins), bins=streams, flags=flags)


def test_picture_png():
    # compress 0 follows the document, which is compressed
    p = pic_file(hwp_picture(1), bins=(("png", 0),), streams={"BIN0001.png": deflate(PNG)})
    assert md(p) == "![](assets/img-001.png)\n"


def test_picture_bmp_to_png():
    pixels = b"\x00\x00\xff\x00"  # one red pixel, the row padded to four bytes
    head = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    bmp = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54) + head + pixels
    # compress 2 is stored, though the document is compressed
    doc, ctx = parse(pic_file(hwp_picture(1), bins=(("bmp", 2),), streams={"BIN0001.bmp": bmp}))
    out_dir = tmp()
    out = markdown.render(doc, out_dir / "assets", False, ctx.skipped)
    assert out == "![](assets/img-001.png)\n", out
    assert (out_dir / "assets" / "img-001.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_picture_compressed_item():
    # compress 1 inflates in an uncompressed document; a second picture, or a second item naming the same stream,
    # reads and inflates nothing again
    packed = deflate(PNG)
    seen = []
    old = hwp.flate.inflate

    def spy(data, budget):
        seen.append(data)
        return old(data, budget)
    hwp.flate.inflate = spy
    try:
        p = pic_file(hwp_picture(1), hwp_picture(1), hwp_picture(2), bins=(("png", 1), ("PNG", 1, 1)),
                     streams={"BIN0001.png": packed}, flags=0)
        out = md(p)
    finally:
        hwp.flate.inflate = old
    assert out == "![](assets/img-001.png)\n\n" * 2 + "![](assets/img-001.png)\n", out
    assert seen == [packed], seen


def test_picture_placements_charged():
    # every placement is charged against IMAGE_BYTES, as in docx and doc: one 4 MiB picture placed 100 times is placed
    # while the total stays under the cap, each one past it is counted under image, and the render stays bounded
    big = PNG + bytes(4 * 2**20 - len(PNG))
    n, keep = 100, limits.IMAGE_BYTES // (4 * 2**20)
    p = pic_file(*[hwp_picture(1)] * n, streams={"BIN0001.png": big})
    t0 = time.monotonic()
    doc, ctx = parse(p)
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert time.monotonic() - t0 < 10
    assert ctx.skipped["image"] == n - keep and out.count("![") == keep, (ctx.skipped, out.count("!["))


def test_picture_missing_stream():
    p = pic_file(hwp_picture(2), hwp_picture(1), bins=(("png", 2), ("png", 2)), streams={"BIN0001.png": PNG})
    out = md(p)
    assert out == "(could not read: 1 image)\n\n![](assets/img-001.png)\n", out


def test_broken_pictures_counted():
    # a link item, a corrupt deflate stream, a cut one, an empty one, an id past the table and a SHAPE_PICTURE too
    # short for its id each cost one image, and the section goes on
    packed = deflate(bytes(range(256)) * 64)
    short = (11, b"gso ", hwp_record(hr.SHAPE_COMPONENT, 2, b"cip$cip$") + hwp_record(hr.SHAPE_PICTURE, 3, bytes(40)))
    bins = ((None, 0), ("png", 1), ("png", 1), ("png", 2), ("png", 2))
    streams = {"BIN0002.png": b"\xff" * 64, "BIN0003.png": packed[:len(packed) // 2], "BIN0004.png": b"",
               "BIN0005.png": PNG}
    pics = (hwp_picture(1), hwp_picture(2), hwp_picture(3), hwp_picture(4), hwp_picture(9), short, hwp_picture(5))
    doc, ctx = parse(pic_file(*pics, bins=bins, streams=streams))
    assert ctx.skipped["image"] == 6 and not ctx.skipped["section"], ctx.skipped
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert out == "(could not read: 6 images)\n\n![](assets/img-001.png)\n", out


def test_stored_picture_marked_compressed():
    # an item marked compressed whose stream is not deflate at all is shown from its raw bytes when they are an image
    doc, ctx = parse(pic_file(hwp_picture(1), bins=(("png", 1),), streams={"BIN0001.png": PNG}))
    assert not ctx.skipped["image"], ctx.skipped
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "![](assets/img-001.png)\n"


def test_picture_old_layout():
    # the SHAPE_PICTURE right under the control, with no SHAPE_COMPONENT, is read through the same walk
    old = (11, b"gso " + bytes(40) + wstr("로고"), hwp_record(hr.SHAPE_PICTURE, 2, bytes(71) + u16(1) + bytes(9)))
    doc, ctx = parse(pic_file(old, streams={"BIN0001.png": PNG}))
    assert not ctx.skipped["image"], ctx.skipped
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "![로고](assets/img-001.png)\n"


def test_auto_alt_dropped():
    streams = {"BIN0001.png": PNG}
    assert md(pic_file(hwp_picture(1, "그림입니다."), streams=streams)) == "![](assets/img-001.png)\n"
    assert md(pic_file(hwp_picture(1, "회사 로고"), streams=streams)) == "![회사 로고](assets/img-001.png)\n"


def test_picture_caption():
    p = pic_file(hwp_picture(1, caption="그림 1"), streams={"BIN0001.png": PNG})
    assert md(p) == "![](assets/img-001.png)\n\n그림 1\n"


def text_box(text, level):
    """A rectangle SHAPE_COMPONENT at `level` whose text box holds one paragraph, as Hancom writes it: the
    LIST_HEADER and its paragraph, then the rectangle record, all one level deeper."""
    return (hwp_record(hr.SHAPE_COMPONENT, level, b"cer$cer$")
            + sub_list(hwp_para(text, level=level + 1), level=level + 1) + hwp_record(RECT, level + 1, bytes(20)))


def group(*members, level):
    """A container SHAPE_COMPONENT at `level`: SHAPE_CONTAINER, then the members, written at level + 1."""
    return (hwp_record(hr.SHAPE_COMPONENT, level, b"noc$noc$")
            + hwp_record(hr.SHAPE_CONTAINER, level + 1, u16(len(members))) + b"".join(members))


def gso(records, alt=""):
    return 11, b"gso " + bytes(40) + wstr(alt), records


def test_group_shape_text():
    shape = gso(group(text_box("가", 3), text_box("나", 3), level=2), "묶음 개체입니다.")
    assert md(hwp_file(hwp_para("앞" + FFFC, controls=(shape,)))) == "앞\n\n가\n\n나\n"
    assert md(hwp_file(hwp_para(FFFC, controls=(gso(text_box("글", 2)),)))) == "글\n"
    # a picture inside a group does not take the group's comment as its alt text
    member = hwp_record(hr.SHAPE_COMPONENT, 3, b"cip$") + hwp_record(hr.SHAPE_PICTURE, 4, bytes(71) + u16(1))
    p = pic_file(gso(group(member, level=2), "묶음 개체입니다."), streams={"BIN0001.png": PNG})
    assert md(p) == "![](assets/img-001.png)\n"


def test_header_pictures_and_objects_skipped():
    # a header is a line of text, as in doc: its pictures (one deflated and large, one with no stream) and its OLE chart
    # are skipped without being read or counted, its text and a text box's are kept, and the body picture is read
    chart = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
             '<c:barChart><c:ser><c:val><c:numRef><c:numCache><c:ptCount val="1"/><c:pt idx="0"><c:v>5</c:v>'
             '</c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>')
    ole_path = tmp() / "ole.cfb"
    make_cfb(ole_path, {"OOXMLChartContents": chart.encode()})
    ole_item = u32(ole_path.stat().st_size) + ole_path.read_bytes()
    ole = (11, b"gso " + bytes(40) + u16(0), hwp_record(hr.SHAPE_COMPONENT, 4, b"elo$elo$" + bytes(32))
           + hwp_record(hr.SHAPE_OLE, 5, bytes(12) + u16(3) + bytes(10)))
    controls = (hwp_picture(1, level=4), hwp_picture(4, level=4), ole, gso(text_box("상자", 4)))
    head = sub_list(hwp_para("머리말" + FFFC * 4, level=2, controls=controls))
    body = hwp_para(FFFC + FFFC, controls=((16, b"head" + u32(0), head), hwp_picture(2)))
    bins = info(bins=(("png", 1), ("png", 2), ("OLE", 2), ("png", 2)))
    streams = {"BIN0001.png": deflate(PNG + bytes(2**20 - len(PNG))), "BIN0002.png": PNG, "BIN0003.OLE": ole_item}
    read = []
    old = hwp._Reader.load_bin

    def spy(self, name, packed, keep):
        read.append(name)
        return old(self, name, packed, keep)
    hwp._Reader.load_bin = spy
    try:
        doc, ctx = parse(hwp_file(body, doc_info=bins, bins=streams))
    finally:
        hwp._Reader.load_bin = old
    assert doc.headers == ["머리말 상자"] and not ctx.skipped, (doc.headers, ctx.skipped)
    assert read == ["BinData/BIN0002.png"], read
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "Header: 머리말 상자\n\n![](assets/img-001.png)\n"


def test_group_depth_cap():
    def nest(k, level):  # the k-th container, at group depth k
        inner = text_box("바닥", level + 1) if k == 40 else nest(k + 1, level + 1)
        return group(inner, level=level)
    shape = gso(group(text_box("얕음", 3), nest(2, 3), level=2))
    doc, ctx = parse(hwp_file(hwp_para(FFFC, controls=(shape,))))
    assert ctx.skipped["depth"] == 1, ctx.skipped
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert out == "(could not read: 1 place nested too deeply)\n\n얕음\n", out


# ---- connect lines as diagrams
def shape(kind, inst, text="", x=0, y=0, w=1000, h=1000, **kw):
    return hwp_shape(kind, inst, (x, y, w, h), text, **kw)


def conn(inst, a, b, ctype=1, ends=((0, 0), (0, 0)), **kw):
    """A connect line glued a -> b; ctype 1 is STRAIGHT_ONEWAY, 5 STROKE_BOTH, 6 ARC_NOARROW."""
    return shape("$col", inst, line=(ends[0], ends[1], ctype, a, b), fill=0, **kw)


def objs(*controls, text="", level=0):
    """A paragraph: some text, then the drawing objects."""
    return hwp_para(text + FFFC * len(controls), controls=controls, level=level)


def mermaid(doc):
    return "\n".join(b.text for b in doc.blocks if isinstance(b, Code) and b.lang == "mermaid")


def _connect_sec():
    return (hwp_para("before") + objs(shape("$rec", 5, "Alpha"), shape("$rec", 6, "Beta", x=5000),
                                      conn(7, 5, 6, x=1000, ends=((0, 500), (4000, 500))), text="lead")
            + hwp_para("after"))


def test_hwp_connect_line():
    doc, ctx = parse(hwp_file(_connect_sec()))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"before\n\nlead\n\n```mermaid\n{t}\n```\n\nafter\n", out  # each text once
    assert not ctx.skipped["shape"] and not ctx.skipped["connector_loose"], ctx.skipped


def test_hwp_both_and_none_arrows():
    p = objs(shape("$rec", 1, "A"), shape("$ell", 2, "B", y=3000), shape("$rec", 3, "C", y=6000),
             conn(4, 1, 2, 5), conn(5, 2, 3, 6))
    t = mermaid(parse(hwp_file(p))[0])
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["A"]\n  n2(("B"))\n  n3["C"]\n  n1 <--> n2\n  n2 --- n3', t


def test_hwp_glue_without_a_common_frame():
    # objects placed relative to their paragraphs have no box, but their glue still holds across paragraphs
    sec = (objs(shape("$rec", 11, "Alpha", rel="PARA"), text="one") + hwp_para("mid")
           + objs(shape("$rec", 12, "Beta", rel="PARA"), conn(13, 11, 12, rel="PARA"), text="three"))
    doc, _ = parse(hwp_file(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"one\n\n```mermaid\n{t}\n```\n\nmid\n\nthree\n", out


def member(kind, inst, text="", **kw):
    """A group member's records at level 3: the members of a gso whose records are at level 2."""
    if kind == "$col":
        return conn(inst, kw.pop("a"), kw.pop("b"), member=True, level=3, **kw)
    return shape(kind, inst, text, member=True, level=3, **kw)


def test_hwp_group_inference():
    # a loose connect line inside a group snaps to the members' boxes, offset in the group's frame
    box = (member("$rec", 21, "Left"), member("$rec", 22, "Right", x=3000),
           member("$col", 23, a=0, b=0, x=500, y=500, ends=((0, 0), (3000, 0))))
    sec = objs(shape("$con", 20, w=4000, members=box), text="lead") + objs(shape("$rec", 30, "Zed", x=90000))
    doc, ctx = parse(hwp_file(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == ('flowchart LR\n  n1["Left"]\n  n2["Right"]\n  %% inferred from position\n'
                                   '  n1 --> n2'), t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"lead\n\n```mermaid\n{t}\n```\n\nZed\n", out
    assert not ctx.skipped["connector_loose"], ctx.skipped


def test_hwp_group_members_glued_by_instance_id():
    box = (member("$rec", 21, "In1"), member("$rec", 22, "In2", y=3000), member("$col", 23, a=21, b=22))
    doc, _ = parse(hwp_file(objs(shape("$con", 20, h=5000, members=box), text="lead")))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["In1"]\n  n2["In2"]\n  n1 --> n2', t
    assert markdown.render(doc, tmp() / "a") == f"lead\n\n```mermaid\n{t}\n```\n"


def test_hwp_arrowed_line_and_divider():
    sec = objs(shape("$rec", 1, "A"), shape("$rec", 2, "B", x=5000),
               shape("$lin", 3, line=((500, 500), (5500, 500)), arrows=(0, 1), fill=0),
               shape("$lin", 4, line=((0, 2000), (6000, 2000)), fill=0))
    doc, ctx = parse(hwp_file(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  n1 --> n2', t
    assert markdown.render(doc, tmp() / "a") == f"```mermaid\n{t}\n```\n"
    assert not ctx.skipped["connector_loose"], ctx.skipped


def test_hwp_truncated_component():
    # a connect line whose LINE record is cut after its end points has no glue and no points: no edge, no error
    cut = struct.pack("<4i", 0, 500, 4000, 500)
    sec = objs(shape("$rec", 5, "Alpha"), shape("$rec", 6, "Beta", x=5000),
               shape("$col", 7, x=1000, line=cut, fill=0), text="lead")
    doc, ctx = parse(hwp_file(sec))
    assert not mermaid(doc) and markdown.render(doc, tmp() / "a") == "lead\n\nAlpha\n\nBeta\n"
    assert ctx.skipped["connector_loose"] == 1 and not ctx.skipped["shape"], ctx.skipped
    # a shape whose component is cut to its ids has no box and no drawing style: the CTRL_HEADER's id is its key
    sec = objs(shape("$rec", 5, "Alpha"), shape("$rec", 0, "Beta", head_id=6, component=b"cer$cer$"),
               conn(7, 5, 6), text="lead")
    doc, _ = parse(hwp_file(sec))
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', t
    # a component and a LINE record cut anywhere read without an error
    _, head, recs = hwp_shape("$col", 7, (0, 0, 9, 9), line=((0, 0), (9, 9), 1, 5, 6))
    full, line = (r.data for r in hr.records(recs, Context()))
    for n in range(len(full) + 1):  # the ids twice, the offset, 4 bytes, then the original size, ending at 28
        (o,) = hwp._objs(full[:n], head, False, line, [])  # cut inside its control id, it is no connect line
        assert (o.box is None) == (n < 28) and (o.edge is None if n < 4 else o.edge[:2] == (5, 6)), (n, o.box)
    for n in range(len(line) + 1):  # the glue ends at 36: the points, the type, the subjects and their indexes
        (o,) = hwp._objs(full, head, False, line[:n], [])
        assert (o.edge[:2] == (None, None)) == (n < 36) and (o.edge[2] is None) == (n < 36), (n, o.edge)
    for n in range(len(head) + 1):  # the offsets end at 16: the id, the attribute, the vertical and horizontal ones
        (o,) = hwp._objs(full, head[:n], False, line, [])
        assert (o.box is None) == (n < 16) and o.edge[:2] == (5, 6), (n, o.box)


def test_hwp_fill_brush_before_the_instance_id():
    # every fill kind at once (kordoc fillBrushXml): solid, a three-colour gradation whose positions come first, an
    # image, two extra bytes, then an alpha per kind; the shadow follows, then the instance id
    style = (struct.pack("<IiIB", 0, 1, 3 << 16, 0) + u32(7) + bytes(12)
             + struct.pack("<B4ii", 0, 0, 0, 0, 0, 3) + bytes(12) + bytes(12) + bytes(6)
             + u32(2) + bytes(2) + bytes(3) + bytes(16) + u32(42) + bytes(2))
    assert hwp._style(style, 0) == (3 << 16, 42)
    assert hwp._style(style[:-6], 0) == (3 << 16, None)  # cut inside the id


def test_hwp_shapes_without_line_unchanged():
    box = (member("$rec", 1, "A"), member("$ell", 2, "B"))
    sec = objs(shape("$con", 20, members=box), text="lead") + objs(shape("$rec", 3, "C"))
    assert md(hwp_file(sec)) == "lead\n\nA\n\nB\n\nC\n"  # as the reader read it before diagrams
    divider = objs(shape("$lin", 9, line=((0, 0), (1, 1)), fill=0))  # makes the section a scope; no text, no edge
    assert md(hwp_file(sec + divider)) == md(hwp_file(sec))


def test_hwp_line_in_a_note_reads_as_before():
    # a note is read apart from the section's drawing: its connect line and shapes are read as before
    note = sub_list(objs(shape("$rec", 1, "In", level=4), conn(2, 1, 3, level=4), text="주석", level=2))
    sec = objs((17, b"fn  " + u32(1), note), shape("$rec", 3, "Out"), text="본문")
    doc, ctx = parse(hwp_file(sec))
    assert not mermaid(doc) and not ctx.skipped["shape"], ctx.skipped
    out = markdown.render(doc, tmp() / "a")
    assert out == "본문[^1]\n\nOut\n\n[^1]: 주석 In\n", out


# ---- sections
def test_two_sections_one_bad():
    p = hwp_file(hwp_para("하나"), doc_info=info(sections=2), extra={"BodyText/Section1": b"\xff" * 64})
    out = md(p)
    assert out.splitlines()[0] == "(could not read: 1 section)", out
    assert "하나" in out


def test_all_sections_bad():
    e = denied(hwp_file(extra={"BodyText/Section0": b"\xff" * 64}), "damaged")
    assert e.detail == "no readable section", e


def test_declared_section_missing():
    out = md(hwp_file(hwp_para("하나"), doc_info=info(sections=2)))
    assert out == "(could not read: 1 section)\n\n하나\n", out


def test_section_number_order():
    extra = {"BodyText/Section10": deflate(hwp_para("10")), "BodyText/Section2": deflate(hwp_para("2")),
             "BodyText/Section1000": deflate(hwp_para("천"))}
    out = md(hwp_file(hwp_para("0"), hwp_para("1"), extra=extra))
    assert out == "(could not read: 1 section)\n\n0\n\n1\n\n2\n\n10\n", out


def test_truncated_section_counted():
    body = hwp_para("앞부분") + hwp_para("x" * 3000)
    c = zlib.compressobj(level=0, wbits=-15)  # stored blocks: a cut keeps an exact prefix
    packed = c.compress(body) + c.flush()
    p = hwp_file(extra={"BodyText/Section0": packed[:len(packed) * 3 // 5]})
    doc, ctx = parse(p)
    assert ctx.skipped["record"] == 1 and not ctx.skipped["section"], ctx.skipped
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert out.startswith("(could not read: 1 damaged record)\n\n앞부분"), out


def test_one_budget_per_document():
    seen = []
    old = hwp.flate.inflate

    def spy(data, budget):
        seen.append(budget)
        return old(data, budget)
    hwp.flate.inflate = spy
    try:
        parse(hwp_file(hwp_para("a"), hwp_para("b"), doc_info=info(sections=2)))
    finally:
        hwp.flate.inflate = old
    assert len(seen) == 3 and seen[0] is seen[1] is seen[2], seen


# ---- what is not an HWP 5 body
NS = ('xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
      'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"')


def test_hwp_zip_is_hwpx():
    members = {"mimetype": "application/hwp+zip",
               "Contents/section0.xml": f'<hs:sec {NS}><hp:p><hp:run><hp:t>하나</hp:t></hp:run></hp:p></hs:sec>'}
    p = make_zip(tmp() / "x.hwp", members)
    assert md(p) == "하나\n"
    out = tmp() / "out"
    reader.convert(p, out)
    assert (out / "out.md").read_text(encoding="utf-8") == "하나\n"


def test_hwp3():
    p = tmp() / "old.hwp"
    p.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + bytes(200))
    e = denied(p, "unsupported")
    assert e.detail == "hwp 3.x"


def test_password():
    denied(hwp_file(hwp_para("x"), flags=1 | 2), "encrypted")
    denied(hwp_file(hwp_para("x"), flags=1 | (1 << 8)), "encrypted")


def test_drm_flag():
    denied(hwp_file(hwp_para("x"), flags=1 | 16), "drm")
    denied(hwp_file(hwp_para("x"), flags=1 | (1 << 10)), "drm")


def test_not_hwp():
    p = tmp() / "m.hwp"
    make_cfb(p, {"Other": b"x"})
    denied(p, "damaged")
    for data in (b"", b"just some text", bytes(600)):
        q = tmp() / "n.hwp"
        q.write_bytes(data)
        e = denied(q, "damaged")
        assert e.detail == "not an HWP file", e


def test_convert_end_to_end():
    out = tmp() / "out"
    meta = reader.convert(hwp_file(hwp_para("본문"), doc_info=info(sections=1)), out)
    assert (out / "out.md").read_text(encoding="utf-8") == "본문\n"
    assert meta["lines"] > 0 and meta["unread"] == [], meta


# ---- the handler interface Tasks 6-8 extend, and hostile input
def test_controls_table_and_handler_end():
    for name in hwp._Reader.CONTROLS.values():
        assert callable(getattr(hwp._Reader, name, None)), name
    ctx = Context()
    data = hwp_para("x" + FFFC + "y", controls=((16, b"head" + u32(0), sub_list(hwp_para("h", level=2))),))
    recs = hr.records(data, ctx)
    r = hwp._Reader(None, ctx)
    i = next(k for k, rec in enumerate(recs) if rec.tag == hr.CTRL_HEADER)
    assert r.header(recs, i, None, 0) == len(recs) and r.doc.headers == ["h"]
    assert r.skip(recs, i, None, 0) == len(recs)


def test_hostile_fields_do_not_fail():
    short = (hwp_record(hr.PARA_HEADER, 0, b"\x01")  # too short for shape and style ids
             + hwp_record(hr.PARA_TEXT, 1, "짧은".encode("utf-16-le")))
    p = hwp_para("a" + FFFC + "b" + FFFC + "c" + FFFC + "d" + FFFC + FFFC + "e", shape=50, style=200,
                 controls=((3, b"%hlk" + u32(0) + b"\0" + u16(60000) + "ab".encode("utf-16-le"), b""),
                           (4, b"%hlk", b""),
                           (17, b"fn  ", b""),                       # no number, no paragraphs
                           (11, b"eqed", hwp_record(hr.EQEDIT, 2, u32(0) + u16(9999) + b"x\0")),
                           (16, b"he", b"")))                        # a CTRL_HEADER shorter than its id
    numbered = hwp_para("번호", shape=0)  # a numbering id past the numbering table
    di = info(shapes=((2, 0, 300),), numberings=())
    doc, ctx = parse(hwp_file(short + p + hwp_record(hr.CTRL_HEADER, 1, b"x") + numbered, doc_info=di))
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert "짧은" in out and "a[b](ab)c" in out and "e" in out and "1. 번호" in out, out


def test_depth_counted_not_recursion_error():
    def nest(k):
        lvl = 2 * k
        if k == 80:
            return hwp_para("바닥", level=lvl)
        note = hwp_record(hr.LIST_HEADER, lvl + 2, bytes(8)) + nest(k + 1)
        return hwp_para(str(k) + FFFC, level=lvl, controls=((17, b"fn  " + u32(1), note),))
    doc, ctx = parse(hwp_file(nest(0)))
    assert ctx.skipped["depth"] >= 1 and any(w.startswith("depth") for w in ctx.warnings)
    assert "바닥" not in markdown.render(doc, tmp() / "a")


def test_field_stack_capped():
    n = 3000
    ctrls = tuple((3, field(b"%hlk", "https://e.com;1"), b"") for _ in range(n))
    p = hwp_para(FFFC * n + "남음", controls=ctrls)
    assert "남음" in md(hwp_file(p))


def test_large_section_is_linear():
    paras = b"".join(hwp_para("문단 %d" % k, chars=((0, 0), (2, 1), (3, 0))) for k in range(20_000))
    long = hwp_para("가나다라" * 50_000, chars=tuple((k, (k // 4) % 2) for k in range(0, 200_000, 4)))
    p = hwp_file(paras + long, doc_info=info(bold=(False, True)))
    t0 = time.monotonic()
    doc, _ = parse(p)
    took = time.monotonic() - t0
    assert len(doc.blocks) == 20_001 and took < 10, took


def test_many_char_shape_records_are_linear():
    # one paragraph, 200,000 PARA_CHAR_SHAPE records (2.4 MB): about half a minute while the pairs were concatenated
    n = 200_000
    shapes = b"".join(hwp_record(hr.PARA_CHAR_SHAPE, 1, u32(k) + u32(k % 2)) for k in range(n))
    p = hwp_file(raw_para("abc".encode("utf-16-le") + unit(13)) + shapes, doc_info=info(bold=(False, True)))
    t0 = time.monotonic()
    doc, ctx = parse(p)
    took = time.monotonic() - t0
    assert markdown.render(doc, tmp() / "a") == "a**b**c\n" and took < 5, took


def test_many_inline_equations_are_linear():
    # one paragraph of nothing but 150,000 inline equations: each one used to be removed by its own list scan
    n = 150_000
    eq = hwp_record(hr.CTRL_HEADER, 1, b"deqe" + u32(1)) + hwp_record(hr.EQEDIT, 2, u32(0) + wstr("x"))
    # stored, as it deflates past the ratio cap; 6 MB, inside what make_cfb writes
    p = hwp_file(raw_para(ctl(11, b"eqed") * n + unit(13)) + eq * n, flags=0)
    t0 = time.monotonic()
    doc, _ = parse(p)
    took = time.monotonic() - t0
    assert len(doc.blocks) == n and all(type(b).__name__ == "Equation" and b.tex == "x" for b in doc.blocks)
    assert took < 5, took


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
