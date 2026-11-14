"""RTF reader: text, destinations, Unicode and code pages, hidden runs, headings, notes, fields, headers, hostile input.

Run: python -I -X utf8 tests/test_reader_rtf.py
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
from reader.formats import rtf  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_doc  # noqa: E402


def uword(n, fallback=b"?"):
    """The control word for code unit n, with its fallback text."""
    return b"\\u%d" % n + fallback


def tmp():
    return Path(tempfile.mkdtemp())


def rtf_file(data, name="a.rtf"):
    p = tmp() / name
    p.write_bytes(data)
    return p


def parse(path):
    ctx = Context()
    return rtf.parse(Path(path), ctx), ctx


def md(data, hidden=False, name="a.rtf"):
    d, ctx = parse(rtf_file(data, name))
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


def damaged(data, name="a.rtf"):
    try:
        parse(rtf_file(data, name))
    except ReadError as e:
        assert e.kind == "damaged", e
        return e
    raise AssertionError("expected ReadError damaged")


# ---- the brief's tests
def test_star_mid_group():
    assert md(rb"{\rtf1\ansi The quick {\b brown \*\xyz fox} jumps.}") == "The quick **brown fox** jumps.\n"


def test_star_destination():
    out = md(rb"{\rtf1 keep{\*\generator Riched20}}")
    assert out == "keep\n", out
    out = md(rb'{\rtf1 {\field{\*\fldinst HYPERLINK "http://x"}{\fldrslt site}}}')
    assert out == "[site](http://x)\n", out


def test_unicode_uc():
    assert md(b"{\\rtf1\\ansi\\uc1 " + uword(54620) + uword(44544) + b"}") == "한글\n"
    assert md(b"{\\rtf1 {\\uc2 " + uword(54620, b"") + b"\\'3f\\'3f}x}") == "한x\n"
    # \uc is per group: back to 1 after the group, so one byte of the fallback is skipped
    assert md(rb"{\rtf1 {\uc2 \u65 ab}\u66 cd}") == "ABd\n"


def test_unicode_skip_stops_at_braces_and_words():
    out = md(rb"{\rtf1\uc3 \u65 a{\b b}}")
    assert out == "A**b**\n", out
    out = md(rb"{\rtf1\uc3 \u65 a\b b}")
    assert out == "A**b**\n", out


def test_surrogate_pair():
    assert md(rb"{\rtf1 \u-10179?\u-8704?}") == "😀\n"
    assert md(rb"{\rtf1 \u-10179?x}") == "�x\n"
    assert md(rb"{\rtf1 a\u-8704?b}") == "a�b\n"


def test_uc_huge_clamped():
    assert md(b"{\\rtf1\\" + b"uc999999\\u65 BCDEFGHIJKLMNOP}") == "ALMNOP\n"


def test_dbcs_cp949():
    data = rb"{\rtf1\ansi\ansicpg949{\fonttbl{\f0\fcharset129 Gulim;}}\f0 \'c7\'d1\'b1\'db}"
    assert md(data) == "한글\n"
    assert md(rb"{\rtf1\ansi\ansicpg949 \'c7\'d1\'b1\'db}") == "한글\n"  # no font: the ansicpg


def test_cp932_font():
    data = rb"{\rtf1\ansi{\fonttbl{\f0\fnil\fcharset0 Arial;}{\f1\fcharset128 MS Gothic;}}\f1 \'93\'fa\'96\'7b\f0  ok}"
    assert md(data) == "日本 ok\n"


def test_hidden_v():
    data = rb"{\rtf1 visible {\v secret} shown}"
    out = md(data)
    assert "(1 hidden places left out)" in out and "secret" not in out, out
    assert "visible" in out and "shown" in out
    assert "secret" in md(data, hidden=True)
    out = md(rb"{\rtf1 a \v hidden\v0  b}")
    assert "hidden places" in out and "b" in out


def test_hidden_bytes_feed_the_skip():
    # a hidden \u still consumes its fallback bytes
    out = md(rb"{\rtf1 x{\v \u65 ?}y}", hidden=True)
    assert "A" in out and "?" not in out, out


def test_headings():
    ko = "제목".encode("cp949")
    hex_ko = b"".join(b"\\'%02x" % b for b in ko)
    data = (rb"{\rtf1\ansi\ansicpg949{\stylesheet{\s0 Normal;}{\s1 heading 1;}{\s2 Heading 2;}"
            rb"{\s3\sbasedon2 Sub;}{\s4 " + hex_ko + rb" 1;}{\s5\sbasedon4 Mine;}}"
            rb"\pard\outlinelevel0 Top\par"
            rb"\pard\s2 Second\par"
            rb"\pard\s3 Third\par"
            rb"\pard\s5 Korean\par"
            rb"\pard\s0 Plain\par}")
    assert md(data) == "# Top\n\n## Second\n\n## Third\n\n# Korean\n\nPlain\n"


def test_heading_bold_is_dropped_and_pard_resets():
    data = rb"{\rtf1{\stylesheet{\s1 heading 1;}}\pard\s1\b Big\b0\par\pard After\par}"
    assert md(data) == "# Big\n\nAfter\n"


def test_footnote():
    data = rb"{\rtf1 text{\super\chftn}{\footnote\pard\plain{\super\chftn} note text}}"
    out = md(data)
    assert out.count("[^1]") == 2 and "[^1]: note text" in out and out.startswith("text[^1]"), out
    d, _ = parse(rtf_file(data))
    assert list(d.notes) == ["fn-1"]


def test_endnote():
    data = rb"{\rtf1 a{\footnote\ftnalt\pard end}b{\footnote\pard foot}}"
    d, _ = parse(rtf_file(data))
    assert list(d.notes) == ["en-1", "fn-1"], list(d.notes)


def test_header_footer():
    out = md(rb"{\rtf1{\header\pard Top\par}{\footer\pard Bottom\par}Body\par}")
    assert "Header: Top" in out and "Footer: Bottom" in out and "Body" in out, out
    assert out.count("Top") == 1 and out.count("Bottom") == 1, out  # the header text is not also body text


def test_info_title():
    out = md(rb"{\rtf1{\info{\title Report}{\author Someone}}Body text\par}")
    assert out.startswith("# Report\n") and "Body text" in out and "Someone" not in out, out


def test_group_depth():
    data = b"{\\rtf1 before " + b"{" * 5000 + b"deep" + b"}" * 5000 + b"}"
    d, ctx = parse(rtf_file(data))
    assert ctx.skipped["depth"] == 1, ctx.skipped
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert "before" in out and "\ndeep" not in out and "1 place nested too deeply" in out, out


def test_unbalanced_close():
    # a missing close is repaired at the end of the input
    assert md(rb"{\rtf1 one {\b two") == "one **two**\n"
    # a close that ends the root group ends the reading: what follows is not text of the document
    assert md(rb"{\rtf1 one}} two}") == "one\n"
    # an empty group and a nested extra group are fine
    assert md(rb"{\rtf1 one {\i x}{} y}") == "one x y\n"


def test_doc_saved_as_rtf():
    p = make_doc(tmp() / "w.rtf", [("워드 본문\r", False)])
    d, ctx = parse(p)
    assert markdown.render(d, tmp() / "assets", False, ctx.skipped) == "워드 본문\n"


# ---- more
def test_paragraphs_and_breaks():
    assert md(rb"{\rtf1 a\par b\line c\tab d\par\par\par e}") == "a\n\nb\nc d\n\ne\n"
    assert md(rb"{\rtf1 a\sect b\page c\par}").count("\n\n") == 1


def test_special_characters():
    out = md(rb"{\rtf1 a\emdash b\endash c\lquote d\rquote \ldblquote e\rdblquote \bullet f\enspace g\emspace h\qmspace i}")
    assert out == "a—b–c‘d’“e”•f g h i\n", out
    assert md(rb"{\rtf1 a\~b\_c\-d\zwj e\zwnj f}") == "a b-cd‍e‌f\n"
    assert md(rb"{\rtf1 \{a\} b}") == "{a} b\n"


def test_bom_and_leading_space():
    assert md(b"\xef\xbb\xbf  \n{\\rtf1 hi}") == "hi\n"


def test_not_rtf_is_damaged():
    for data in (b"plain text file", b"", b"{\\rt hi}", b"\xef\xbb\xbfhello {\\rtf1 x}"):
        assert damaged(data).detail == "not an RTF file"


def test_text_after_root_close_ignored():
    assert md(rb"{\rtf1 one}" + b"\r\n trailing {\\b junk}") == "one\n"


def test_cell_and_row_separate_text():
    data = rb"{\rtf1\trowd\cellx1000\cellx2000 \pard\intbl A\cell B\cell\row\trowd\cellx1000 C\cell\row D}"
    assert md(data) == "A\n\nB\n\nC\n\nD\n"


def test_listtext_stays_a_prefix():
    assert md(rb"{\rtf1 {\*\listtext\pard\plain 1.\tab}one\par {\listtext 2.\tab}two\par}") == "1\\. one\n\n2\\. two\n"  # the renderer escapes the dot


def test_sbasedon_cycle_does_not_loop():
    data = rb"{\rtf1{\stylesheet{\s1\sbasedon2 A;}{\s2\sbasedon1 B;}{\s3\sbasedon3 C;}}\pard\s1 x\par\pard\s3 y\par}"
    assert md(data) == "x\n\ny\n"


def test_sbasedon_chain_is_capped():
    # 40 styles deep: the heading at the far end is past the cap of 32, so it is not reached
    styles = b"{\\s1 heading 1;}" + b"".join(b"{\\s%d\\sbasedon%d S;}" % (i, i - 1) for i in range(2, 41))
    data = b"{\\rtf1{\\stylesheet" + styles + b"}\\pard\\s5 near\\par\\pard\\s40 far\\par}"
    assert md(data) == "# near\n\nfar\n"


def test_field_other_kind_keeps_result():
    data = rb'{\rtf1 page {\field{\*\fldinst PAGE}{\fldrslt 7}} end}'
    assert md(data) == "page 7 end\n"
    data = rb'{\rtf1 {\field{\*\fldinst HYPERLINK \\l "mark"}{\fldrslt jump}} x}'
    assert md(data) == "jump x\n"


def test_field_nesting_cap_keeps_text():
    n = 40
    data = (b"{\\rtf1 " + b"".join(b"{\\field{\\*\\fldinst PAGE}{\\fldrslt " for _ in range(n)) + b"core"
            + b"}}" * n + b"}")
    d, ctx = parse(rtf_file(data))
    assert ctx.skipped["depth"] == n - 32, ctx.skipped
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.endswith("\n\ncore\n") and "8 places nested too deeply" in out, out


def test_font_table_text_is_not_output():
    out = md(rb"{\rtf1{\fonttbl{\f0 Times;}}{\colortbl;\red0;}{\stylesheet{\s1 Name;}}Body}")
    assert out == "Body\n"


def test_note_inside_note_is_text_of_outer():
    data = rb"{\rtf1 a{\footnote\pard outer {\footnote inner} tail}}"
    d, ctx = parse(rtf_file(data))
    assert list(d.notes) == ["fn-1"]
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert "[^1]: outer inner tail" in out, out


def test_empty_note_is_dropped():
    d, _ = parse(rtf_file(rb"{\rtf1 a{\footnote\pard }b}"))
    assert d.notes == {}
    assert md(rb"{\rtf1 a{\footnote\pard }b}") == "ab\n"


def test_too_many_notes_counted():
    data = b"{\\rtf1 " + b"x{\\footnote n}" * 10_005 + b"}"
    d, ctx = parse(rtf_file(data))
    assert len(d.notes) == 10_000 and ctx.skipped["note"] == 5, (len(d.notes), ctx.skipped)


def test_gate_dispatch_through_reader():
    out = tmp() / "out"
    meta = reader.convert(rtf_file(rb"{\rtf1\ansi hello}"), out)
    assert (out / "out.md").read_text(encoding="utf-8") == "hello\n" and meta["unread"] == []
    assert "rtf" in reader.BUILT


def test_cfb_head_is_handed_off():
    p = make_doc(tmp() / "x.rtf", [("hi\r", False)])
    assert md(p.read_bytes(), name="y.rtf") == "hi\n"


def test_large_file_is_fast():
    para = rb"\pard\plain\f0\fs24 Hello {\b bold} world \'e9" + uword(8212) + rb" done {\i it}\par" + b"\r\n"
    data = b"{\\rtf1\\ansi{\\fonttbl{\\f0 X;}}" + para * (2 * 2**20 // len(para)) + b"}"
    assert len(data) >= 2 * 2**20 - 100
    t0 = time.monotonic()
    d, _ = parse(rtf_file(data))
    took = time.monotonic() - t0
    print(f"2 MB RTF: {len(d.blocks)} blocks in {took:.2f}s")
    assert len(d.blocks) > 20_000 and took < 8, (len(d.blocks), took)


def test_unknown_symbol_and_nul():
    assert md(b"{\\rtf1 a\\\x00b\\\x07c \\x}") == "abc\n"


def test_bin_is_skipped():
    data = b"{\\rtf1 a\\bin4 \x00}{\\y}"  # four raw bytes follow \bin4: the brace and backslash among them are no tokens
    assert md(data) == "ay\n", md(data)


def test_pict_counted_once_each():
    data = (rb"{\rtf1 a{\pict\pngblip 89504e47}b"
            rb"{\*\shppict{\pict\jpegblip ffd8}}{\nonshppict{\pict\wmetafile8 0102}}"
            rb"{\pict\bin4 \}\{}c}")
    d, ctx = parse(rtf_file(data))
    assert ctx.skipped["image"] == 3, ctx.skipped
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert "3 images" in out and "89504e47" not in out and "ffd8" not in out, out
    assert "abc" in out


def test_pntext_and_unknown_star_group():
    assert md(rb"{\rtf1 {\pntext\pard 3)\tab}three{\*\other hidden}}") == "3) three\n"


def test_shape_text_is_read():
    out = md(rb"{\rtf1 {\shp{\*\shpinst Keep out {\shptxt\pard Inside\par}}}after}")
    assert "Inside" in out and "Keep out" not in out and "after" in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
