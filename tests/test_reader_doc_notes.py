"""Word 97-2003 (.doc) stories past the main one: footnotes, endnotes, comments and their authors, headers and footers,
text boxes; references matched at their exact CPs, nested and hidden references, hostile PLCs.

Run: python -I -X utf8 tests/test_reader_doc_notes.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import doc  # noqa: E402
from reader.model import Context, NoteRef, Para, Text  # noqa: E402
from reader_fix import doc_plc, doc_stories, make_cfb, make_doc, make_zip  # noqa: E402

BOLD, VANISH, DELETED = 0x0835, 0x083C, 0x0800
IN_TABLE, TTP = 0x2416, 0x2417
SEP = [""] * 6  # the six PlcfHdd separator stories, empty
PAGE = "\x13 PAGE \x141\x15\r"  # a page-number field whose result is 1
FIB_PAIRS = 0x9A  # where make_doc's FibRgFcLcb97 starts in WordDocument
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def sp(op, value=1):
    """One sprm with a fixed-size operand, sized by op >> 13."""
    return u16(op) + value.to_bytes((1, 1, 2, 4, 2, 2, 0, 3)[op >> 13], "little")


def ftxbxs(n, reusable=()):
    """n FTXBXS (cTxbx 1, fReusable 1 for the indices in `reusable`), then the dummy one."""
    return b"".join(struct.pack("<iihiii", 1, 0, k in reusable, -1, k + 1, 0) for k in range(n)) + bytes(22)


def story_doc(main, *, fc=None, ccp=None, split=0, chpx=None, papx=None, **kw):
    """A .doc of doc_stories(main, **kw). `fc` entries replace the built ones (None leaves one out) and `ccp` story
    lengths; the first `split` characters of the text become a compressed piece."""
    s = doc_stories(main, **kw)
    for i, v in (fc or {}).items():
        if v is None:
            s["fc"].pop(i, None)
        else:
            s["fc"][i] = v
    s["ccp"].update(ccp or {})
    if split:
        text = s["pieces"][0][0]
        s["pieces"] = [(text[:split], True), (text[split:], False)]
    return make_doc(tmp() / "a.doc", chpx=chpx, papx=papx, **s)


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    d, ctx = parse(path)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


def set_pair(path, index, fc, lcb):
    """Rewrite FibRgFcLcb97 entry `index` of a make_doc file."""
    with Compound(path) as cf:
        streams = {n: cf.read(n) for n in cf.names()}
    word = bytearray(streams["WordDocument"])
    struct.pack_into("<II", word, FIB_PAIRS + 8 * index, fc, lcb)
    streams["WordDocument"] = bytes(word)
    make_cfb(path, streams)
    return path


def docx(body, parts, rels):
    """A Word package (saved as .doc, which the reader opens as docx): rels are (id, type, target)."""
    rel_xml = "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{tg}"/>' for i, t, tg in rels)
    pkg = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    members = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": pkg + f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                             '</Relationships>',
        "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": pkg + rel_xml + "</Relationships>",
        **parts,
    }
    return make_zip(tmp() / "x.doc", members, stored=())


# ---- the brief's behaviour
def test_footnote():
    p = story_doc("본문\x02\r", footnotes=[(2, "\x02 주석\r")])
    assert md(p) == "본문[^1]\n\n[^1]: 주석\n", md(p)
    d, ctx = parse(p)
    assert d.blocks == [Para([Text("본문"), NoteRef("fn-1")])], d.blocks
    assert d.notes == {"fn-1": [Para([Text(" 주석")])]} and not ctx.skipped, (d.notes, ctx.skipped)


def test_endnote():
    p = story_doc("본문\x02\r", endnotes=[(2, "\x02 미주\r")])
    assert md(p) == "본문[^1]\n\n[^1]: 미주\n"
    d, _ = parse(p)
    assert list(d.notes) == ["en-1"]
    q = story_doc("a\x02b\x02\r", footnotes=[(3, "\x02 각주\r")], endnotes=[(1, "\x02 미주\r")])
    assert md(q) == "a[^1]b[^2]\n\n[^1]: 미주\n[^2]: 각주\n"


def test_comment_with_author():
    p = story_doc("문장\x05\r", comments=[(2, 0, "\x05검토\r", "K")], authors=["Kim"])
    assert md(p) == "문장[^1]\n\n[^1]: Comment by Kim: 검토\n", md(p)
    d, _ = parse(p)
    assert d.notes == {"cm-1": [Para([Text("Comment by Kim: "), Text("검토")])]}, d.notes


def test_headers_footers():
    p = story_doc("본문\r", headers=SEP + ["", "머리글\r", "", PAGE, "", ""])
    assert md(p) == "Header: 머리글\n\n본문\n", md(p)
    d, ctx = parse(p)
    assert d.headers == ["머리글"] and d.footers == ["1"] and not ctx.skipped, (d.headers, d.footers, ctx.skipped)


def test_textbox_appended():
    p = story_doc("본문\r", textboxes=["상자\r"])
    assert md(p) == "본문\n\n상자\n", md(p)
    q = story_doc("본문\r", textboxes=["하나\r", "둘\r"], header_textboxes=["위\r"])
    assert md(q) == "Header: 위\n\n본문\n\n하나\n\n둘\n", md(q)


def test_bad_footnote_story_counted():
    p = story_doc("본문\x02\r", footnotes=[(2, "\x02 주석\r")], fc={3: doc_plc([0, 100, 101])})
    assert md(p) == "(could not read: 1 footnotes part)\n\n본문\n", md(p)


# ---- references at their CPs, nested and hidden
def test_reference_at_its_cp():
    # a surrogate pair is two CPs: the reference after an emoji is CP 4, its str index 3 matches nothing
    p = story_doc("😀본문\x02\r", footnotes=[(4, "\x02 주석\r")])
    assert md(p) == "😀본문[^1]\n\n[^1]: 주석\n", md(p)
    q = story_doc("😀본문\x02\r", footnotes=[(3, "\x02 주석\r")])
    assert md(q) == "😀본문\n\n[^1]: 주석\n", md(q)
    # a compressed piece first, then an emoji: CPs still count one a byte and two a pair
    r = story_doc("ab😀\x02x\x05\r", footnotes=[(4, "\x02 각주\r")], comments=[(6, 0, "\x05의견\r")], authors=["Lee"],
                  split=2)
    assert md(r) == "ab😀[^1]x[^2]\n\n[^1]: 각주\n[^2]: Comment by Lee: 의견\n", md(r)


def test_nested_note():
    # note 1's text holds the reference of note 2: both are read, each into its own slot
    p = story_doc("본문\x02\r", footnotes=[(2, "\x02 바깥\x02\r"), (8, "\x02 안쪽\r")])
    assert md(p) == "본문[^1]\n\n[^1]: 바깥[^2]\n[^2]: 안쪽\n", md(p)
    d, ctx = parse(p)
    assert d.notes == {"fn-1": [Para([Text(" 바깥"), NoteRef("fn-2")])], "fn-2": [Para([Text(" 안쪽")])]}, d.notes
    assert not ctx.skipped
    # a note whose own text holds its reference
    q = story_doc("본문\r", footnotes=[(7, "\x02 자기\x02\r")])
    assert md(q) == "본문\n\n[^1]: 자기[^1]\n", md(q)
    # a footnote and a comment that refer to each other: neither is read twice, and rendering ends
    # footnote story "\x02 가\x05\r" + guard at CPs 3-8, comment story "\x05나\x02\r" + guard at 9-13
    r = story_doc("본문\r", footnotes=[(11, "\x02 가\x05\r")], comments=[(6, 0, "\x05나\x02\r")], authors=["A"])
    assert md(r) == "본문\n\n[^1]: 가[^2]\n[^2]: Comment by A: 나[^1]\n", md(r)


def test_nested_chain_past_max_depth():
    # each note refers to the next, 80 deep: read once each, in turn, never recursively, and nothing is counted
    texts = [f"\x02n{k}\x02\r" for k in range(80)]
    refs, at = [2], 4  # the main story "본문\x02\r" is CPs 0-3
    for t in texts:
        refs.append(at + len(t) - 2)
        at += len(t)
    p = story_doc("본문\x02\r", footnotes=list(zip(refs, texts)))
    d, ctx = parse(p)
    out = markdown.render(d, tmp() / "a", False, ctx.skipped)
    assert not ctx.skipped and len(d.notes) == 80, ctx.skipped
    assert out.startswith("본문[^1]\n\n[^1]: n0[^2]\n[^2]: n1[^3]\n") and out.endswith("[^80]: n79\n"), out[-200:]


def test_hidden_reference_matches_docx():
    # a reference in vanished or deleted text is a hidden place and its note still follows, as the docx reader gives
    # a w:footnoteReference in a w:vanish run or in w:del
    fn = (f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p><w:r><w:footnoteRef/>'
          '<w:t xml:space="preserve"> 주석</w:t></w:r></w:p></w:footnote></w:footnotes>')
    for op, wrap in ((VANISH, '<w:r><w:rPr><w:vanish/></w:rPr><w:footnoteReference w:id="1"/></w:r>'),
                     (DELETED, '<w:del><w:r><w:footnoteReference w:id="1"/></w:r></w:del>')):
        p = story_doc("본문\x02\r", footnotes=[(2, "\x02 주석\r")], chpx=[(2, 3, sp(op, 1))])
        x = docx(f'<w:p><w:r><w:t>본문</w:t></w:r>{wrap}</w:p>', {"word/footnotes.xml": fn},
                 [("rF", "footnotes", "footnotes.xml")])
        assert md(p) == md(x) == "(1 hidden places left out)\n\n본문\n\n[^1]: 주석\n", (md(p), md(x))
        assert md(p, True) == md(x, True) == "본문(hidden: [^1])\n\n[^1]: 주석\n", (md(p, True), md(x, True))
    q = story_doc("a\x05\r", comments=[(1, 0, "\x05말\r")], authors=["Kim"], chpx=[(1, 2, sp(VANISH, 1))])
    assert md(q) == "(1 hidden places left out)\n\na\n\n[^1]: Comment by Kim: 말\n", md(q)


def test_comment_matches_docx():
    cm = (f'<w:comments xmlns:w="{W}"><w:comment w:id="0" w:author="Kim"><w:p><w:r><w:annotationRef/>'
          '<w:t>검토</w:t></w:r></w:p></w:comment></w:comments>')
    x = docx('<w:p><w:r><w:t>문장</w:t></w:r><w:r><w:commentReference w:id="0"/></w:r></w:p>',
             {"word/comments.xml": cm}, [("rC", "comments", "comments.xml")])
    p = story_doc("문장\x05\r", comments=[(2, 0, "\x05검토\r")], authors=["Kim"])
    assert md(p) == md(x) == "문장[^1]\n\n[^1]: Comment by Kim: 검토\n", (md(p), md(x))
    # a comment whose text starts with a table (CPs 3-5 of the comment story at 3): its own lead paragraph, as in docx
    q = story_doc("a\x05\r", comments=[(1, 0, "x\x07\x07\r")], authors=["Kim"],
                  papx=[(3, 5, u16(0) + sp(IN_TABLE, 1)), (5, 6, u16(0) + sp(IN_TABLE, 1) + sp(TTP, 1))])
    d, _ = parse(q)
    assert d.notes["cm-1"][0] == Para([Text("Comment by Kim: ")]) and len(d.notes["cm-1"]) == 2, d.notes


def test_stories_use_the_walk():
    # bold, hidden text, fields and tables work inside notes, headers and text boxes as in the main story
    p = story_doc("본문\x02\r", footnotes=[(2, "\x02 굵게 숨김\r")], chpx=[(6, 8, sp(BOLD, 1)), (9, 11, sp(VANISH, 1))])
    assert md(p) == "(1 hidden places left out)\n\n본문[^1]\n\n[^1]: **굵게**\n", md(p)
    hdr = "\x13 HYPERLINK \"https://h.example\" \x14링크\x15\r"
    q = story_doc("본문\r", headers=SEP + ["", hdr, "", "", "", ""], textboxes=["a\x07b\x07\x07"],
                  papx=[(3 + len(hdr) + 1 + 0, 3 + len(hdr) + 1 + 4, u16(0) + sp(IN_TABLE, 1)),
                        (3 + len(hdr) + 1 + 4, 3 + len(hdr) + 1 + 5, u16(0) + sp(IN_TABLE, 1) + sp(TTP, 1))])
    assert md(q) == "Header: 링크\n\n본문\n\n|a|b|\n|---|---|\n", md(q)


# ---- headers and footers
def test_headers_by_section():
    # the separators are skipped; each section's six stories go to headers or footers, empty ones are none
    hdrs = ["구분선\r"] * 6 + ["짝\r", "머리\r", "", "바닥\r", "첫\r", ""] + ["", "머리\r", "", "다른\r", "", ""]
    p = story_doc("본문\r", headers=hdrs)
    d, ctx = parse(p)
    assert d.headers == ["짝", "머리", "첫", "머리"] and d.footers == ["바닥", "다른"] and not ctx.skipped
    assert md(p) == "Header: 짝 | 머리 | 첫\nFooter: 바닥 | 다른\n\n본문\n", md(p)
    # a section cut short in the PLC: the stories it has still read
    q = story_doc("본문\r", headers=SEP + ["", "머리\r", "", "바닥\r"])
    d, ctx = parse(q)
    assert d.headers == ["머리"] and d.footers == ["바닥"] and not ctx.skipped, (d.headers, d.footers, ctx.skipped)
    # only separators: nothing
    d, ctx = parse(story_doc("본문\r", headers=["구분\r"] * 6))
    assert d.headers == d.footers == [] and not ctx.skipped


def test_textbox_dummy_and_reusable():
    # the last FTXBXS is the dummy of the story's guard: its range is not read, even when it holds text; a reusable
    # FTXBXS holds no text box
    p = story_doc("본문\r", textboxes=["상자\r", "남은\r"], fc={56: doc_plc([0, 3, 6]) + ftxbxs(1)})
    assert md(p) == "본문\n\n상자\n", md(p)
    q = story_doc("본문\r", textboxes=["지운\r", "상자\r"], fc={56: doc_plc([0, 3, 6, 7]) + ftxbxs(2, reusable={0})})
    assert md(q) == "본문\n\n상자\n", md(q)
    r = story_doc("본문\r", header_textboxes=["위\r", "남은\r"], fc={58: doc_plc([0, 2, 5]) + ftxbxs(1)})
    assert md(r) == "Header: 위\n\n본문\n", md(r)


# ---- hostile input
def test_bad_plcs_counted():
    # a PLC whose size is not 4 + (4 + cb)n, whose CPs decrease, or that runs past its story loses that story only
    base = dict(footnotes=[(2, "\x02 주석\r")], comments=[(3, 0, "\x05말\r")], authors=["Kim"],
                headers=SEP + ["", "머리\r", "", "바닥\r", "", ""], textboxes=["상자\r"], header_textboxes=["위\r"])
    whole = story_doc("본문\x02\x05\r", **base)
    assert md(whole) == ("Header: 머리 | 위\nFooter: 바닥\n\n본문[^1][^2]\n\n상자\n\n[^1]: 주석\n"
                         "[^2]: Comment by Kim: 말\n"), md(whole)
    cases = [
        ({2: bytes(7)}, "footnotes", "본문[^1]"),                        # PlcffndRef: 7 bytes is no 4 + 6n
        ({3: doc_plc([5, 0, 6])}, "footnotes", "본문[^1]"),               # PlcffndTxt CPs decrease
        ({4: doc_plc([3, 9]) + bytes(29)}, "comments", "본문[^1]"),        # PlcfandRef: a 29-byte ATRDPre10
        ({5: doc_plc([0, 2**32 - 1, 2**32 - 1])}, "comments", "본문[^1]"),  # PlcfandTxt past the story
        ({11: bytes(6)}, "header", "본문[^1][^2]"),                        # PlcfHdd: no whole CPs
        ({11: doc_plc([0] * 7 + [50, 60])}, "header", "본문[^1][^2]"),     # PlcfHdd past the header story
        ({56: bytes(31)}, "block", "본문[^1][^2]"),                        # PlcftxbxTxt: no 4 + 26n
        ({56: doc_plc([0, 90, 91]) + ftxbxs(1)}, "block", "본문[^1][^2]"),   # past the text box story
        ({58: doc_plc([2, 0, 3]) + ftxbxs(1)}, "header", "본문[^1][^2]"),  # PlcfHdrtxbxTxt CPs decrease
    ]
    for fc, kind, line in cases:
        d, ctx = parse(story_doc("본문\x02\x05\r", fc=fc, **base))
        out = markdown.render(d, tmp() / "a", False, ctx.skipped)
        assert ctx.skipped == {kind: 1} and "본문" in out and line + "\n" in out, (fc, kind, ctx.skipped, out)
    # a PLC outside the table stream
    for index, kind in ((3, "footnotes"), (5, "comments"), (11, "header"), (56, "block"), (58, "header")):
        p = set_pair(story_doc("본문\x02\x05\r", **base), index, 10**6, 40)
        d, ctx = parse(p)
        assert ctx.skipped == {kind: 1} and d.blocks[0].inlines[0] == Text("본문"), (index, ctx.skipped)


def test_unmatched_entries_counted():
    # two references and one text: the second has no note and its \x02 is dropped
    p = story_doc("a\x02b\x02\r", footnotes=[(1, "\x02 하나\r"), (3, "\x02 둘\r")], fc={3: doc_plc([0, 5])})
    assert md(p) == "(could not read: 1 footnotes part)\n\na[^1]b\n\n[^1]: 하나\n", md(p)
    # one reference and three texts: two have no reference
    q = story_doc("a\x02\r", footnotes=[(1, "\x02 하나\r"), (1, "\x02 둘\r"), (1, "\x02 셋\r")],
                  fc={2: doc_plc([1, 9]) + struct.pack("<h", 1)})
    assert md(q) == "(could not read: 2 footnotes parts)\n\na[^1]\n\n[^1]: 하나\n", md(q)
    # texts without any reference PLC, and a reference PLC without texts
    r = story_doc("a\x05\r", comments=[(1, 0, "\x05말\r")], fc={4: None})
    assert md(r) == "(could not read: 1 comments part)\n\na\n", md(r)
    s = story_doc("a\x05\r", comments=[(1, 0, "\x05말\r")], fc={5: None})
    assert md(s) == "(could not read: 1 comments part)\n\na\n", md(s)
    # a reference no \x02 sits at: its note is not lost, it follows unreferenced
    t = story_doc("ab\x02\r", footnotes=[(0, "\x02 주석\r")])
    assert md(t) == "ab\n\n[^1]: 주석\n", md(t)
    # nor is a reference inside a field instruction, which is never shown
    f = story_doc("\x13 X\x02 \x14r\x15\r", footnotes=[(3, "\x02 주석\r")])
    assert md(f) == "r\n\n[^1]: 주석\n", md(f)


def test_comment_author_bounds():
    # an ibst past GrpXstAtnOwners, or no list at all, or an empty name: the initials the ATRDPre10 holds
    for authors in ((), ("Kim",), ("", "Lee")):
        p = story_doc("a\x05\r", comments=[(1, 0 if "" in authors else 5, "\x05말\r", "JK")], authors=list(authors))
        assert md(p) == "a[^1]\n\n[^1]: Comment by JK: 말\n", (authors, md(p))
    # an Xst cut short ends the list; one name control characters and all, cut to MAX_NAME
    owners = u16(3) + "Kim".encode("utf-16-le") + u16(50) + b"x"
    p = story_doc("a\x05b\x05\r", comments=[(1, 0, "\x05하나\r"), (3, 1, "\x05둘\r", "Z")], authors=["?"],
                  fc={36: owners})
    assert md(p) == "a[^1]b[^2]\n\n[^1]: Comment by Kim: 하나\n[^2]: Comment by Z: 둘\n", md(p)
    long = "N\x00\x07" + "M" * 200
    q = story_doc("a\x05\r", comments=[(1, 0, "\x05말\r")], authors=[long])
    assert md(q) == f"a[^1]\n\n[^1]: Comment by N  {'M' * 61}: 말\n", md(q)
    r = story_doc("a\x05\r", comments=[(1, 0xFFFF, "\x05말\r")], authors=["Kim"])
    assert md(r) == "a[^1]\n\n[^1]: Comment by : 말\n", md(r)


def test_ranges_clamped_to_the_story():
    # a story longer than the pieces hold: its entries are read up to the text there is
    p = story_doc("본문\x02\r", footnotes=[(2, "\x02 주석\r")], ccp={"footnote": 60}, fc={3: doc_plc([0, 50, 60])})
    assert md(p) == "본문[^1]\n\n[^1]: 주석\n", md(p)


def test_plc_entries_capped():
    # entries past PLC_MAX: the PLC is not read and the story is counted (3 notes make a text PLC of 4 entries)
    old = doc.PLC_MAX
    doc.PLC_MAX = 4
    try:
        notes = [(k, f"\x02{k}\r") for k in range(5)]
        d, ctx = parse(story_doc("\x02" * 5 + "\r", footnotes=notes))
        assert ctx.skipped == {"footnotes": 1} and d.notes == {}, ctx.skipped
        d, ctx = parse(story_doc("\x02" * 3 + "\r", footnotes=notes[:3]))
        assert not ctx.skipped and len(d.notes) == 3, ctx.skipped
    finally:
        doc.PLC_MAX = old


def test_damaged_story_loses_only_itself():
    # an unexpected failure while one note, header, footer or text box is walked costs that one only
    real = doc._Reader.walk

    def walk(self, st):
        if "폭탄" in st.story.text:
            raise RuntimeError("boom")
        return real(self, st)

    doc._Reader.walk = walk
    try:
        p = story_doc("본문\x02\x02\x05\r", footnotes=[(2, "\x02 폭탄\r"), (3, "\x02 무사\r")],
                      comments=[(4, 0, "\x05폭탄\r")], authors=["Kim"],
                      headers=SEP + ["", "폭탄\r", "", "폭탄\r", "", "바닥\r"], textboxes=["폭탄\r", "상자\r"],
                      header_textboxes=["폭탄\r"])
        d, ctx = parse(p)
        out = markdown.render(d, tmp() / "a", False, ctx.skipped)
    finally:
        doc._Reader.walk = real
    assert ctx.skipped == {"footnotes": 1, "comments": 1, "header": 2, "footer": 1, "block": 1}, ctx.skipped
    assert out == ("(could not read: 2 headers, 1 footer, 1 footnotes part, 1 comments part, 1 text block)\n\n"
                   "Footer: 바닥\n\n본문[^1]\n\n상자\n\n[^1]: 무사\n"), out


def test_many_stories_linear():
    n = 20_000
    main = "\x02" * n + "\r"
    notes = [(k, f"\x02 {k}\r") for k in range(n)]
    hdrs = SEP + ["", "머리\r", "", "바닥\r", "", ""] * 3_000
    p = story_doc(main, footnotes=notes, comments=[(k, 0, f"\x05{k}\r") for k in range(0, n, 2)], authors=["A"],
                  headers=hdrs, textboxes=[f"상자{k}\r" for k in range(n)])
    t0 = time.monotonic()
    d, ctx = parse(p)
    assert time.monotonic() - t0 < 10 and not ctx.skipped, (time.monotonic() - t0, ctx.skipped)
    assert len(d.notes) == n + n // 2 and len(d.headers) == len(d.footers) == 3_000 and len(d.blocks) == n + 1


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
