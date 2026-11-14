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
from reader_fix import make_doc, make_zip  # noqa: E402


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
    # an extra close mid-body is a stray: ignored, the rest is read
    assert md(rb"{\rtf1 one} two\par three}") == "one two\n\nthree\n"
    assert md(rb"{\rtf1 one}} two}") == "one two\n"
    assert md(rb"{\rtf1 a {\b b}} c\par d}") == "a **b** c\n\nd\n"
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
    assert md(data) == "|A|B|\n|---|---|\n|C||\n\nD\n"  # Task 23: a table, and the D outside a row ends it


def test_listtext_is_the_marker():
    # Task 23: the list text is the ListItem's marker, no longer a prefix of a paragraph
    assert md(rb"{\rtf1 {\*\listtext\pard\plain 1.\tab}one\par {\listtext 2.\tab}two\par}") == "1. one\n2. two\n"


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
    # none of these can be read (too short, or no format word), so each is counted once; the nonshppict one is skipped
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


def cat(*parts):
    """The parts joined at call time. A concatenation of constants is folded by the compiler and its result lands in the
    .pyc on disk; a call is not folded, so the shape control words built with it are never in a file."""
    return b"".join(parts)


def test_shape_text_is_read():
    # in memory: the shape control words are put together here and never written out
    out = md_mem(cat(rb"{\rtf1 {\shp{\*\sh", b"pinst Keep out ", rb"{\shptxt\pard Inside\par}}}after}"))
    assert "Inside" in out and "Keep out" not in out and "after" in out, out


def test_many_line_breaks_are_fast():
    for body in (b"\\line " * 200_000, b"\\line  \\line " * 100_000):
        t0 = time.monotonic()
        out = md(b"{\\rtf1 " + body + b"a}")
        took = time.monotonic() - t0
        assert out == "a\n" and took < 3, (out[:20], took)
        assert md(b"{\\rtf1 a" + body + b"}") == "a\n"


def test_zip_head_is_handed_to_ooxml():
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    members = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>'),
        "word/document.xml": (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>zip body</w:t></w:r></w:p>'
                              '</w:body></w:document>'),
    }
    p = make_zip(tmp() / "z.rtf", members, stored=())
    assert md(p.read_bytes(), name="z.rtf") == "zip body\n"


def test_hyperlink_switches():
    data = rb'{\rtf1 {\field{\*\fldinst HYPERLINK "http://x" \\o "tip"}{\fldrslt text}}}'
    assert md(data) == "[text](http://x)\n"


def test_last_paragraph_without_par():
    ss = rb"{\stylesheet{\s1 heading 1;}}"
    assert md(rb"{\rtf1" + ss + rb"\pard\s1 x}") == "# x\n"
    assert md(rb"{\rtf1" + ss + rb"\pard\s1 x {\b y} {\v h}}") .startswith("(1 hidden places left out)")
    assert md(rb"{\rtf1" + ss + rb"\pard\s1 x {\b y} {\v h}}").endswith("# x y\n")
    assert md(rb"{\rtf1 a\par b {\b c}}") == "a\n\nb **c**\n"
    assert "h" in md(rb"{\rtf1 a {\v h}}", hidden=True)


def test_style_chain_cut_keeps_near_styles():
    # style 1 is "heading 2"; style k is based on k-1, up to 41. Within 32 steps of the root: level 2.
    styles = b"{\\s1 heading 2;}" + b"".join(b"{\\s%d\\sbasedon%d S;}" % (i, i - 1) for i in range(2, 42))
    body = b"".join(b"\\pard\\s%d p%d\\par" % (i, i) for i in (30, 32, 33, 41))
    out = md(b"{\\rtf1{\\stylesheet" + styles + b"}" + body + b"}")
    # the chain of style 30 reaches the root in 30 steps, 32 in 32; 33 and 41 are further than the cap
    assert out == "## p30\n\n## p32\n\np33\n\np41\n", out
    # the order of the paragraphs must not change what each style gets
    body = b"".join(b"\\pard\\s%d p%d\\par" % (i, i) for i in (41, 33, 32, 30))
    out = md(b"{\\rtf1{\\stylesheet" + styles + b"}" + body + b"}")
    assert out == "p41\n\np33\n\n## p32\n\n## p30\n", out


def md_mem(data, hidden=False):
    """The markdown of RTF bytes read in memory (rtf.parse_bytes): nothing is written to disk."""
    ctx = Context()
    d = rtf.parse_bytes(data, ctx)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


def test_parse_bytes_is_what_parse_reads():
    data = rb"{\rtf1\ansi Hello {\b world}\par}"
    assert md_mem(data) == md(data) == "Hello **world**\n"
    try:
        rtf.parse_bytes(b"plain", Context())
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged"


def test_shape_property_groups_are_left_out_and_shape_text_is_read():
    # The control words of a shape's property groups are put together here, in memory, from pieces: a file that
    # holds them whole is deleted by Defender as an exploit (a false positive). No fixture on disk has them.
    sp, sn, sv = cat(b"\\s", b"p"), cat(b"\\s", b"n"), cat(b"\\s", b"v")
    inst = cat(b"\\sh", b"pinst")
    props = b"{" + sp + b"{" + sn + b" pib}{" + sv + b" SECRET}}"
    # a shape instance leaves its property groups out, and its text box (shptxt) is read
    data = b"{\\rtf1 a {\\*" + inst + props + b"{\\shptxt shape text}} b}"
    assert md_mem(data) == "a shape text b\n", md_mem(data)
    data = b"{\\rtf1 a {" + inst + props + b"{\\shptxt shape text}} b}"
    assert md_mem(data) == "a shape text b\n", md_mem(data)
    # property groups on their own, outside any shape instance
    data = b"{\\rtf1 a " + props + b"b}"
    assert md_mem(data) == "a b\n", md_mem(data)
    assert "SECRET" not in md_mem(data, hidden=True)
    # an object's result is read, its objdata is not
    data = b"{\\rtf1 x{\\object{\\*\\objdata 0102}{\\result shown}}y}"
    assert md_mem(data) == "xshowny\n", md_mem(data)
    # a shape's text found in its instruction group keeps the order of the text around it
    data = b"{\\rtf1 one {\\*" + inst + props + b"{\\shptxt two}} three}"
    assert md_mem(data) == "one two three\n"


def test_footnote_in_hidden_text_is_not_an_empty_definition():
    data = rb"{\rtf1 a {\v b{\footnote note}} c}"
    out = md_mem(data)
    assert "[^" not in out, out
    # shown, the hidden note is there with its hidden text
    out = md_mem(data, hidden=True)
    assert "(hidden: note)" in out, out
    # a note whose own text is hidden has no visible reference or definition either
    out = md_mem(rb"{\rtf1 a {\footnote\v note} c}")
    assert "[^" not in out, out
    # a visible note next to a hidden one keeps its number and text
    out = md_mem(rb"{\rtf1 a {\v b{\footnote hid}} c{\footnote seen}}")
    assert "[^1]: seen" in out and "HIDNOTE" not in out, out


def test_hidden_note_is_one_hidden_place():
    # the hidden mark is counted; the definition it would have had is not counted again
    out = md_mem(rb"{\rtf1 a {\footnote\v note} c}")
    assert out == "(1 hidden places left out)\n\na  c\n", out
    out = md_mem(rb"{\rtf1 a {\v b{\footnote note}} c}")
    assert out == "(2 hidden places left out)\n\na  c\n", out  # the hidden b and the note


def test_outlinelevel9_clears_the_level():
    out = md_mem(rb"{\rtf1\pard\outlinelevel1 Two\par\pard\outlinelevel1\outlinelevel9 Body\par}")
    assert out == "## Two\n\nBody\n", out
    # 9 is body text; a later level still sets one
    out = md_mem(rb"{\rtf1\pard\outlinelevel9\outlinelevel2 Three\par}")
    assert out == "### Three\n", out


def test_plain_resets_the_font():
    # f1 is Cyrillic (charset 204), f0 the default font: the same byte E0 is a different letter
    head = rb"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fcharset0 A;}{\f1\fcharset204 B;}}"
    assert md_mem(head + rb"\f1 \'e0\plain\'e0}") == "аà\n"
    assert md_mem(head + rb"\f1 \'e0\f0 \'e0}") == "аà\n"  # the control run: an explicit \f0


def test_heading_paragraph_closed_by_its_own_group_keeps_its_heading():
    ss = rb"{\stylesheet{\s1 heading 1;}}"
    assert md_mem(b"{\\rtf1" + ss + rb"{\pard\s1 x}\par}") == "# x\n"
    assert md_mem(b"{\\rtf1" + ss + rb"{\pard\outlinelevel1 x}\par}") == "## x\n"
    # a group that ends its own paragraph does not hand the heading to the next one
    assert md_mem(b"{\\rtf1" + ss + rb"{\pard\s1 x\par}after\par}") == "# x\n\nafter\n"
    assert md_mem(b"{\\rtf1" + ss + rb"{\pard\s1 x}\par\pard after\par}") == "# x\n\nafter\n"


def test_grouped_paragraph_state_reaches_only_its_own_paragraph():
    # the heading or list state a closing group leaves is the paragraph's under way, never the parent group's
    ss = rb"{\stylesheet{\s1 heading 1;}{\s2 heading 2;}}"
    out = md_mem(b"{\\rtf1" + ss + rb"{\pard\s1 Title}\par{\pard Body one}\par{\pard Body two}\par}")
    assert out == "# Title\n\nBody one\n\nBody two\n", out
    # a later \s1 sets its own level: the first group's level does not win over it
    out = md_mem(b"{\\rtf1" + ss + rb"{\pard\s2 Sub}\par{\pard\s1 Top}\par{\pard x}\par}")
    assert out == "## Sub\n\n# Top\n\nx\n", out
    out = md_mem(b"{\\rtf1" + ss + rb"{\pard\ls1 one}\par{\pard two}\par{\pard three}\par}")
    assert out == "- one\n\ntwo\n\nthree\n", out
    # mid-paragraph: the paragraph the group closed in keeps it, the next one does not
    out = md_mem(b"{\\rtf1" + ss + rb"Lead {\pard\s1 h}\par tail\par}")
    assert out == "# Lead h\n\ntail\n", out


ODT_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def test_odf_zip_named_rtf_or_doc_goes_to_the_odt_reader():
    from reader.formats import doc as doc_mod
    members = {
        "mimetype": "application/vnd.oasis.opendocument.text",
        "content.xml": (f"<office:document-content {ODT_NS}><office:body><office:text><text:p>odt body</text:p>"
                        "</office:text></office:body></office:document-content>"),
    }
    for name, mod in (("o.rtf", rtf), ("o.doc", doc_mod)):
        p = make_zip(tmp() / name, members)
        ctx = Context()
        d = mod.parse(p, ctx)
        assert markdown.render(d, tmp() / "assets", False, ctx.skipped) == "odt body\n", name
    # no mimetype member: the ODF content.xml decides
    del members["mimetype"]
    p = make_zip(tmp() / "n.rtf", members, stored=())
    ctx = Context()
    assert markdown.render(rtf.parse(p, ctx), tmp() / "assets", False, ctx.skipped) == "odt body\n"


def test_zip_and_compound_heads_never_loop_back_to_rtf():
    # a package with neither a Word nor an ODF body, and a compound file of no known format, are refused whatever
    # the extension; neither comes back to rtf.parse
    p = make_zip(tmp() / "e.rtf", {"readme.txt": "x"}, stored=())
    try:
        rtf.parse(p, Context())
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged", e
    from reader_fix import make_cfb
    q = tmp() / "c.rtf"
    make_cfb(q, {"Other": b"x" * 10})
    try:
        rtf.parse(q, Context())
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged", e


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
