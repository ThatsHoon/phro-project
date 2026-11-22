"""Word 97-2003 (.doc) character and paragraph properties: sprms, FKPs, the style sheet, headings, bold, hidden and
deleted text, fast-saved pieces and their Prms, hostile input.

Run: python -I -X utf8 tests/test_reader_doc_props.py
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
from reader.formats import doc, doc_fib  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import DOC_EXTRA, doc_stsh, make_doc  # noqa: E402

BOLD, VANISH, DELETED, CHAR_STYLE = 0x0835, 0x083C, 0x0800, 0x4A30
OUTLINE, ISTD, HUGE = 0x2640, 0x4600, 0x6646
NORMAL = (0, 1, 0xFFF, "Normal", b"", b"")


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def u32(n):
    return struct.pack("<I", n)


def sp(op, value=1):
    """One sprm: a number is sized by op >> 13; bytes are a variable operand (u8 length; u16 length + 1 for
    sprmTDefTable and sprmTDefTable10)."""
    if isinstance(value, bytes):
        if op in (0xD606, 0xD608):
            return u16(op) + u16(len(value) + 1) + value
        return u16(op) + bytes([len(value)]) + value
    return u16(op) + value.to_bytes((1, 1, 2, 4, 2, 2, 0, 3)[op >> 13], "little")


def clx(cps, pcds, prcs=()):
    """A Clx: Prc records (clxt 1, u16 cb, grpprl), then the Pcdt (clxt 2, u32 lcb, PlcPcd). pcds are (fc, prm)."""
    plc = b"".join(u32(c) for c in cps) + b"".join(u16(0) + u32(f) + u16(p) for f, p in pcds)
    return b"".join(b"\x01" + u16(len(g)) + g for g in prcs) + b"\x02" + u32(len(plc)) + plc


def doc_file(pieces, **kw):
    return make_doc(tmp() / "a.doc", pieces, **kw)


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    d, ctx = parse(path)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


# ---- the brief's behaviour
def test_bold_run():
    assert md(doc_file([("abcde\r", False)], chpx=[(2, 4, sp(BOLD, 1))])) == "ab**cd**e\n"
    assert md(doc_file([("abcde\r", True)], chpx=[(2, 4, sp(BOLD, 1))])) == "ab**cd**e\n"
    assert md(doc_file([("abcde\r", False)], chpx=[(2, 4, sp(BOLD, 0))])) == "abcde\n"


def test_bold_style_inverted():
    stsh = doc_stsh([NORMAL, None, (0xFFE, 2, 0xFFF, "Strong", b"", sp(BOLD, 1))])

    def out(run):
        return md(doc_file([("abcde\r", False)], fc={1: stsh}, chpx=[(0, 3, run)]))
    assert out(sp(CHAR_STYLE, 2)) == "**abc**de\n"
    assert out(sp(CHAR_STYLE, 2) + sp(BOLD, 129)) == "abcde\n"
    assert out(sp(CHAR_STYLE, 2) + sp(BOLD, 128)) == "**abc**de\n"
    assert out(sp(BOLD, 129)) == "**abc**de\n"  # no character style: the style's value is off, inverted on
    assert out(sp(BOLD, 128)) == "abcde\n"


def test_heading_by_style():
    styles = [NORMAL, None, (2, 1, 0, "heading 2", b"", b"")]
    for base in (18, 10):  # cbSTDBaseInFile of Word 2000 and of Word 97
        p = doc_file([("Title\rbody\r", False)], fc={1: doc_stsh(styles, base)}, papx=[(0, 6, u16(2))])
        assert md(p) == "## Title\n\nbody\n", base
    # a style based on "heading 2" whose own sti is user-defined inherits its outline level
    chain = [NORMAL, None, (2, 1, 0, "heading 2", sp(OUTLINE, 1), b""), (0xFFE, 1, 2, "Mine", b"", b"")]
    p = doc_file([("Title\r", False)], fc={1: doc_stsh(chain)}, papx=[(0, 6, u16(3))])
    assert md(p) == "## Title\n"


def test_heading_by_outline():
    def out(level):
        return md(doc_file([("Title\rbody\r", False)], papx=[(0, 6, u16(0) + sp(OUTLINE, level))]))
    assert out(0) == "# Title\n\nbody\n"
    assert out(2) == "### Title\n\nbody\n"
    assert out(9) == "Title\n\nbody\n" and out(200) == "Title\n\nbody\n"


def test_hidden_text():
    p = doc_file([("a secret b\r", False)], chpx=[(2, 8, sp(VANISH, 1))])
    assert md(p) == "(1 hidden places left out)\n\na  b\n"
    assert md(p, hidden=True) == "a (hidden: secret) b\n"
    # a hidden paragraph style hides its text; 129 shows a run again
    styles = [NORMAL, (0xFFE, 1, 0, "Hid", b"", sp(VANISH, 1))]
    q = doc_file([("zz\rok\r", False)], fc={1: doc_stsh(styles)}, papx=[(0, 3, u16(1))])
    assert md(q) == "(1 hidden places left out)\n\nok\n"
    r = doc_file([("zz\rok\r", False)], fc={1: doc_stsh(styles)}, papx=[(0, 3, u16(1))],
                 chpx=[(0, 1, sp(VANISH, 129))])
    assert md(r) == "(1 hidden places left out)\n\nz\n\nok\n"


def test_deleted_text():
    p = doc_file([("keep gone\r", False)], chpx=[(4, 9, sp(DELETED, 1))])
    assert md(p) == "(1 hidden places left out)\n\nkeep\n" and "gone" in md(p, hidden=True)


def test_deleted_mark_joins():
    assert md(doc_file([("ab\rcd\r", False)], chpx=[(2, 3, sp(DELETED, 1))])) == "abcd\n"
    # the joined paragraph takes the properties of the mark that ends it
    p = doc_file([("ab\rcd\r", False)], chpx=[(2, 3, sp(DELETED, 1))],
                 papx=[(0, 3, u16(0)), (3, 6, u16(0) + sp(OUTLINE, 0))])
    assert md(p) == "# abcd\n"
    assert md(doc_file([("ab\r", False)], chpx=[(2, 3, sp(DELETED, 1))])) == "ab\n"  # the last mark: still ends
    # a deleted cell mark or page break is no paragraph mark: it still ends its paragraph
    assert md(doc_file([("ab\x07cd\x0cef\r", False)], chpx=[(2, 3, sp(DELETED, 1)), (5, 6, sp(DELETED, 1))])) \
        == "ab\n\ncd\n\nef\n"


def test_fast_saved_pieces():
    # "hello " is stored after "WORLD\r": CP order is not FC order; the second piece's Prm0 is sprmCFBold 1
    word_extra = "WORLD\r".encode("utf-16-le") + "hello ".encode("utf-16-le")
    prm0 = 1 << 8 | 0x55 << 1  # val 1, isprm 0x55 (sprmCFBold)
    table = clx([0, 6, 12], [(DOC_EXTRA + 12, 0), (DOC_EXTRA, prm0)])
    p = doc_file([], fc={33: table}, word_extra=word_extra, ccp={"text": 12})
    assert md(p) == "hello **WORLD**\n"
    # a CHPX run over CPs 1-4 sits at FCs past the second piece's
    q = doc_file([], fc={33: table}, word_extra=word_extra, ccp={"text": 12}, chpx=[(1, 4, sp(BOLD, 1))])
    assert md(q) == "h**ell**o **WORLD**\n"
    # Prm1: bit 0 set, an index into the Clx Prc list
    prc = clx([0, 6, 12], [(DOC_EXTRA + 12, 0), (DOC_EXTRA, 0 << 1 | 1)], prcs=(sp(BOLD, 1),))
    assert md(doc_file([], fc={33: prc}, word_extra=word_extra, ccp={"text": 12})) == "hello **WORLD**\n"
    # Prm0 vanish (isprm 0x5C) and outline (isprm 0x78) on the piece that holds the paragraph mark
    hide = clx([0, 6, 12], [(DOC_EXTRA + 12, 1 << 8 | 0x5C << 1), (DOC_EXTRA, 0 << 8 | 0x78 << 1)])
    assert md(doc_file([], fc={33: hide}, word_extra=word_extra, ccp={"text": 12})) \
        == "(1 hidden places left out)\n\n# WORLD\n"


def test_paragraph_mark_owns_the_papx():
    # "Title" is stored after its paragraph mark; only the mark's FC is under the heading PAPX
    word_extra = "\r".encode("utf-16-le") + "Title".encode("utf-16-le")
    table = clx([0, 5, 6], [(DOC_EXTRA + 2, 0), (DOC_EXTRA, 0)])
    kw = dict(fc={33: table}, word_extra=word_extra, ccp={"text": 6})
    assert md(doc_file([], papx=[(5, 6, u16(0) + sp(OUTLINE, 0))], **kw)) == "# Title\n"
    assert md(doc_file([], papx=[(0, 5, u16(0) + sp(OUTLINE, 0))], **kw)) == "Title\n"


def test_huge_papx():
    styles = doc_stsh([NORMAL, (1, 1, 0, "heading 1", b"", b"")])
    g = sp(ISTD, 1)
    papx = [(0, 6, u16(0) + sp(HUGE, 100))]
    p = doc_file([("Title\r", False)], fc={1: styles}, papx=papx, streams={"Data": bytes(100) + u16(len(g)) + g})
    assert md(p) == "# Title\n"
    o = sp(OUTLINE, 1)
    q = doc_file([("Title\r", False)], papx=papx, streams={"Data": bytes(100) + u16(len(o)) + o})
    assert md(q) == "## Title\n"
    for streams in ({}, {"Data": bytes(50)}, {"Data": bytes(100) + u16(500) + g}):  # missing, short, cb past it
        d, ctx = parse(doc_file([("Title\r", False)], fc={1: styles}, papx=papx, streams=streams))
        assert markdown.render(d, tmp() / "a", False, ctx.skipped) == "Title\n" and ctx.skipped["styles"] == 1, \
            (streams, ctx.skipped)


def test_bad_fkp_page():
    bte = u32(DOC_EXTRA) + u32(DOC_EXTRA + 22) + u32(9999)  # one page, far past the end of WordDocument
    d, ctx = parse(doc_file([("plain text\r", False)], fc={12: bte, 13: bte}))
    assert markdown.render(d, tmp() / "a", False, ctx.skipped) == "plain text\n"
    assert ctx.skipped["styles"] == 2 and markdown.unread(ctx.skipped) == [], ctx.skipped
    bad = doc_file([("plain text\r", False)], fc={12: bytes(10), 1: b"\x07"})  # a BTE size not 4 + 8n; a cut STSH
    d, ctx = parse(bad)
    assert markdown.render(d, tmp() / "a") == "plain text\n" and ctx.skipped["styles"] == 2, ctx.skipped


# ---- CPs stay exact past surrogate pairs and compressed pieces
def test_bold_after_emoji():
    assert md(doc_file([("😀cd e\r", False)], chpx=[(2, 4, sp(BOLD, 1))])) == "😀**cd** e\n"
    assert md(doc_file([("ab", True), ("😀cd\r", False)], chpx=[(4, 6, sp(BOLD, 1))])) == "ab😀**cd**\n"
    assert md(doc_file([("a😀b\r", False)], chpx=[(1, 3, sp(BOLD, 1))])) == "a**😀**b\n"
    p = doc_file([("x😀y hid z\r", False)], chpx=[(5, 8, sp(VANISH, 1))])
    assert md(p) == "(1 hidden places left out)\n\nx😀y  z\n"


# ---- sprms, FKPs and the style sheet
def test_sprms():
    tdef = b"\x02" + bytes(6) + bytes(40)
    g = (sp(BOLD, 1) + sp(CHAR_STYLE, 7) + sp(0x6A03, 0x12345678) + sp(0x6646, 9) + sp(0xE001, 0x030201)
         + sp(0xC60D, b"\x01\x02") + sp(0xD608, tdef) + sp(0x8001, 5) + sp(0xA001, 6))
    assert doc_fib.sprms(g) == [(BOLD, b"\x01"), (CHAR_STYLE, u16(7)), (0x6A03, u32(0x12345678)), (0x6646, u32(9)),
                                (0xE001, b"\x01\x02\x03"), (0xC60D, b"\x01\x02"), (0xD608, tdef),
                                (0x8001, u16(5)), (0xA001, u16(6))]
    tabs = bytes([1]) + bytes(4) + bytes([2]) + bytes(6)  # sprmPChgTabs cb 255: one deleted tab, two added
    assert doc_fib.sprms(u16(0xC615) + b"\xff" + tabs + sp(BOLD, 1)) == [(0xC615, tabs), (BOLD, b"\x01")]
    assert doc_fib.sprms(u16(0xC615) + b"\x02ab" + sp(BOLD, 1)) == [(0xC615, b"ab"), (BOLD, b"\x01")]
    assert doc_fib.sprms(sp(BOLD, 1) + u16(0x6A03) + b"\x01\x02") == [(BOLD, b"\x01")]  # a cut operand stops
    assert doc_fib.sprms(sp(BOLD, 1) + u16(0xC60D) + b"\x09ab") == [(BOLD, b"\x01")]
    assert doc_fib.sprms(u16(0xD608) + u16(500) + b"x") == [] and doc_fib.sprms(u16(0xD608) + b"\x01") == []
    assert doc_fib.sprms(u16(0xC615) + b"\xff\x09") == [] and doc_fib.sprms(b"\x35") == []
    assert doc_fib.sprms(u16(0xD608) + u16(0) + sp(BOLD, 1)) == [(0xD608, b""), (BOLD, b"\x01")]
    many = sp(0xC60D, b"") * 50_000  # empty variable operands still move on
    assert len(doc_fib.sprms(many)) == 50_000


def test_last_sprms():
    # the sprms read, the last of each; a toggle operand that changes nothing is dropped before it can be the last
    g = sp(BOLD, 1) + sp(BOLD, 0x42) + sp(0xC60D, b"ab") + sp(OUTLINE, 3) + sp(BOLD, 0) + sp(OUTLINE, 1)
    assert doc_fib.last_sprms(doc_fib.sprms(g)) == [(BOLD, b"\x00"), (OUTLINE, b"\x01")]
    assert doc_fib.last_sprms(doc_fib.sprms(sp(BOLD, 1) + sp(BOLD, 0x42))) == [(BOLD, b"\x01")]
    s = doc_fib.Styles()
    for ops in (sp(BOLD, 1) + sp(BOLD, 0x42), sp(BOLD, 0) + sp(BOLD, 129), sp(VANISH, 1) + sp(BOLD, 1) + sp(BOLD, 0)):
        assert s.run(0, doc_fib.last_sprms(doc_fib.sprms(ops))) == s.run(0, doc_fib.sprms(ops)), ops
    assert len(doc_fib.last_sprms(doc_fib.sprms(sp(BOLD, 1) * 10_922))) == 1


def test_run_and_para_props():
    s = doc_fib.Styles(b"")
    r = s.run(0, doc_fib.sprms(sp(0x0855, 1) + sp(0x6A03, 77) + sp(0x080A, 1) + sp(0x680E, 9) + sp(DELETED, 1)))
    assert r == doc_fib.RunProps(False, False, True, True, 77, True, 9)
    assert s.run(0, []) == doc_fib.RunProps(False, False, False, False, None, False, None)
    tdef = b"\x01" + bytes(4) + bytes(20)
    g = (sp(0x2416, 1) + sp(0x2417, 1) + sp(0x244B, 1) + sp(0x244C, 1) + sp(0x6649, 2) + sp(0x260A, 3)
         + sp(0x460B, 0xF801) + sp(0xD608, tdef) + sp(OUTLINE, 4))
    assert s.para(5, doc_fib.sprms(g)) == doc_fib.ParaProps(5, 4, True, True, True, True, 2, 3, 0xF801, tdef)
    assert s.para(0, []) == doc_fib.ParaProps(0, 9, False, False, False, False, 0, 0, 0, None)
    assert s.para(0, doc_fib.sprms(sp(ISTD, 3))).istd == 3 and s.heading(3) == 0


def test_styles_chain_bounded():
    # 40 styles each based on the one before: resolution stops at 32 links, counted, and does not hang
    styles = [(1, 1, 0xFFF, "heading 1", sp(OUTLINE, 0), sp(BOLD, 1))]
    styles += [(0xFFE, 1, k, f"s{k}", b"", b"") for k in range(39)]
    s = doc_fib.Styles(doc_stsh(styles))
    assert s.heading(0) == 1 and s.heading(39) == 0
    assert s.para(5, []).outline == 0 and s.run(5, []).bold  # five links: inherited
    assert s.para(39, []).outline == 9 and not s.run(39, []).bold and s.bad >= 1
    # a cycle 1 -> 2 -> 1 resolves without hanging and is counted
    cyc = doc_fib.Styles(doc_stsh([NORMAL, (0xFFE, 1, 2, "a", sp(OUTLINE, 3), b""), (0xFFE, 1, 1, "b", b"", b"")]))
    assert cyc.para(1, []).outline == 3 and cyc.para(2, []).outline == 3 and cyc.bad >= 1
    self_based = doc_fib.Styles(doc_stsh([(0xFFE, 1, 0, "self", sp(OUTLINE, 2), b"")]))
    assert self_based.para(0, []).outline == 2


def test_stsh_bounded():
    good = doc_stsh([NORMAL, (2, 1, 0, "heading 2", b"", sp(BOLD, 1))])
    for cut in range(len(good)):  # every truncation reads what it holds, never raises
        doc_fib.Styles(good[:cut])
    assert doc_fib.Styles(good[:4]).bad == 1 and doc_fib.Styles(b"").bad == 0
    huge = bytearray(good)
    struct.pack_into("<H", huge, 2, 0xFFFF)  # cstd 65535 in a short STSH: the walk ends at its lcb
    t0 = time.monotonic()
    s = doc_fib.Styles(bytes(huge))
    assert s.heading(1) == 2 and time.monotonic() - t0 < 1
    long_name = bytearray(good)
    at = 2 + 20 + 2 + 18  # the first STD's name count
    struct.pack_into("<H", long_name, at, 0xFFFF)
    assert doc_fib.Styles(bytes(long_name)).heading(1) == 2  # the bad STD is skipped, the next one read


def fkp_page(fcs, cells, width, crun=None):
    """A 512-byte FKP page: rgfc, then the offset (or BX) cells, then raw blobs placed by (offset, bytes)."""
    page = bytearray(512)
    n = len(fcs) - 1
    for i, f in enumerate(fcs):
        struct.pack_into("<I", page, 4 * i, f)
    for j, (off, blob) in enumerate(cells):
        page[4 * (n + 1) + j * width] = off // 2
        if blob:
            page[off:off + len(blob)] = blob
    page[511] = n if crun is None else crun
    return bytes(page)


def bte(fcs, pns):
    return b"".join(u32(f) for f in fcs) + b"".join(u32(p) for p in pns)


def test_fkp_runs_bounded():
    good = fkp_page([0, 10, 20, 30], [(400, b"\x03" + sp(BOLD, 1)), (0, b""), (508, b"\x05")], 1)
    word = bytes(512) + good
    ctx = Context()
    assert doc_fib.fkp_runs(word, bte([0, 30], [1]), "chpx", ctx) == [(0, 10, sp(BOLD, 1)), (10, 20, b"")]
    assert ctx.skipped["styles"] == 1  # the third run's grpprl runs into byte 511 (crun): skipped and counted
    ctx = Context()
    assert len(doc_fib.fkp_runs(word, bte([0, 30, 60], [1, 1]), "chpx", ctx)) == 2  # one page named twice: once
    for crun in (102, 255):  # rgfc and rgb would run past the page
        ctx = Context()
        page = fkp_page([0, 10], [(400, b"\x00")], 1, crun=crun)
        assert doc_fib.fkp_runs(bytes(512) + page, bte([0, 10], [1]), "chpx", ctx) == [] and ctx.skipped["styles"] == 1
    ctx = Context()
    inside = fkp_page([0, 10, 20], [(4, b""), (0, b"")], 1)  # an offset into the rgfc table itself
    assert doc_fib.fkp_runs(bytes(512) + inside, bte([0, 20], [1]), "chpx", ctx) == [(10, 20, b"")]
    assert ctx.skipped["styles"] == 1
    # PAPX: 13-byte BX; cb odd size 2cb - 1, cb 0 then cb' for 2cb'; a size that runs past the page is counted
    papx = fkp_page([0, 6, 12, 18], [(300, b"\x02" + u16(4) + b"\x07"), (400, b"\x00\x03" + u16(2) + sp(ISTD, 3)),
                                     (508, b"\x00\x40")], 13)
    ctx = Context()
    assert doc_fib.fkp_runs(bytes(512) + papx, bte([0, 18], [1]), "papx", ctx) == [
        (0, 6, u16(4) + b"\x07"), (6, 12, u16(2) + sp(ISTD, 3))]
    assert ctx.skipped["styles"] == 1
    ctx = Context()
    assert doc_fib.fkp_runs(bytes(512) + fkp_page([0, 6], [(0, b"")], 13, crun=30), bte([0, 6], [1]), "papx",
                            ctx) == [] and ctx.skipped["styles"] == 1
    ctx = Context()
    assert doc_fib.fkp_runs(word, bte([0, 30], [7]), "chpx", ctx) == [] and ctx.skipped["styles"] == 1
    ctx = Context()
    assert doc_fib.fkp_runs(word, bytes(9), "chpx", ctx) == [] and ctx.skipped["styles"] == 1
    assert doc_fib.fkp_runs(word, b"", "chpx", ctx) == [] and ctx.skipped["styles"] == 1  # none is no loss
    ctx = Context()  # decreasing FCs: that run is skipped and counted
    back = fkp_page([0, 10, 5, 30], [(0, b""), (0, b""), (0, b"")], 1)
    assert doc_fib.fkp_runs(bytes(512) + back, bte([0, 30], [1]), "chpx", ctx) == [(0, 10, b""), (5, 30, b"")]
    assert ctx.skipped["styles"] == 1
    ctx = Context()  # a BTE naming more pages than WordDocument holds: those past that many, counted once
    one = fkp_page([0, 10], [(0, b"")], 1)
    assert doc_fib.fkp_runs(one, bte(range(0, 3_000_010, 10), [0] * 300_000), "chpx", ctx) == [(0, 10, b"")]
    assert ctx.skipped["styles"] == 1


def test_bad_prm_counted():
    word_extra = "abc\r".encode("utf-16-le")
    table = clx([0, 4], [(DOC_EXTRA, 5 << 1 | 1)], prcs=(sp(BOLD, 1),))  # Prc index 5 of one
    d, ctx = parse(doc_file([], fc={33: table}, word_extra=word_extra, ccp={"text": 4}))
    assert markdown.render(d, tmp() / "a") == "abc\n" and ctx.skipped["styles"] == 1


# ---- hidden and deleted content stays hidden
def test_hidden_places_match_docx_runs():
    # two vanished runs (bold differs) are two places, as two vanished w:r are in docx; a deletion spanning two
    # runs is one place, as one w:del is; a hidden run beside a deleted one is two
    p = doc_file([("a bcde f\r", False)], chpx=[(2, 4, sp(VANISH, 1)), (4, 6, sp(VANISH, 1) + sp(BOLD, 1))])
    assert md(p).startswith("(2 hidden places left out)") and md(p, hidden=True) == "a (hidden: bc)(hidden: **de**) f\n"
    q = doc_file([("a bcde f\r", False)], chpx=[(2, 4, sp(DELETED, 1)), (4, 6, sp(DELETED, 1) + sp(BOLD, 1))])
    assert md(q).startswith("(1 hidden places left out)")
    r = doc_file([("a bcde f\r", False)], chpx=[(2, 4, sp(VANISH, 1)), (4, 6, sp(DELETED, 1))])
    assert md(r).startswith("(2 hidden places left out)")


def sttb(names):
    """An extended STTB (fExtend 0xFFFF, u16 cData, cbExtra 0) of UTF-16 strings, as SttbfRMark is."""
    return u16(0xFFFF) + u16(len(names)) + u16(0) + b"".join(u16(len(s)) + s.encode("utf-16-le") for s in names)


def test_tracked_changes_say_what_and_who():
    INSERTED, IBST, IBST_DEL = 0x0801, 0x4804, 0x4863
    authors = {51: sttb(["Unknown", "Kim", "Lee"])}
    p = doc_file([("a new more old b\r", False)], fc=authors,
                 chpx=[(2, 6, sp(INSERTED, 1) + sp(IBST, 1)), (6, 10, sp(INSERTED, 1) + sp(IBST, 1) + sp(BOLD, 1)),
                       (11, 14, sp(DELETED, 1) + sp(IBST_DEL, 2) + sp(IBST, 2)), (15, 16, sp(INSERTED, 1) + sp(IBST, 9))])
    assert md(p) == "(1 hidden places left out)\n\na (inserted by Kim: new **more**)  (inserted: b)\n", md(p)
    assert md(p, hidden=True) == "a (inserted by Kim: new **more**) (deleted by Lee: old) (inserted: b)\n", md(p, True)
    q = doc_file([("a new b\r", False)], chpx=[(2, 5, sp(INSERTED, 1) + sp(IBST, 1))])  # no SttbfRMark
    assert md(q) == "a (inserted: new) b\n", md(q)


def test_hidden_link_shows_no_url():
    text = "a \x13 HYPERLINK \"https://x.example\" \x14go\x15 b\r"
    p = doc_file([(text, False)], chpx=[(2, len(text) - 3, sp(DELETED, 1))])
    out, shown = md(p), md(p, hidden=True)
    assert out == "(1 hidden places left out)\n\na  b\n" and "example" not in out + shown, (out, shown)
    q = doc_file([(text, False)])
    assert md(q) == "a [go](https://x.example) b\n"


def test_hidden_heading_promotes_nothing():
    p = doc_file([("secret\rok\r", False)], papx=[(0, 7, u16(0) + sp(OUTLINE, 0))], chpx=[(0, 6, sp(VANISH, 1))])
    assert md(p) == "(1 hidden places left out)\n\nok\n"
    assert md(p, hidden=True) == "(hidden: secret)\n\nok\n"
    q = doc_file([("gone\x0bx\r", False)], chpx=[(0, 5, sp(DELETED, 1))])  # a deleted line break stays deleted
    assert md(q) == "(1 hidden places left out)\n\nx\n"


# ---- complexity
def test_runs_and_pieces_linear():
    # 20,000 one-character pieces stored in reverse order, each under its own CHPX run: the intersection walks
    # runs + pieces, not runs x pieces
    n = 20_000
    word_extra = "".join("b" if k % 2 else "a" for k in reversed(range(n))).encode("utf-16-le") + "\r".encode(
        "utf-16-le")
    cps = list(range(n + 2))
    pcds = [(DOC_EXTRA + 2 * (n - 1 - k), 0) for k in range(n)] + [(DOC_EXTRA + 2 * n, 0)]
    spans = [(k, k + 1, sp(BOLD, k % 2)) for k in range(n)]
    p = doc_file([], fc={33: clx(cps, pcds)}, word_extra=word_extra, ccp={"text": n + 1}, chpx=spans)
    t0 = time.monotonic()
    out = md(p)
    took = time.monotonic() - t0
    assert out == "a**b**" * (n // 2) + "\n" and took < 10, took


def test_cp_runs_linear():
    # 200,000 two-byte pieces in reverse FC order against 200,000 one-character runs: runs x pieces would be 4e10
    n = 200_000
    pcs = [doc_fib.Piece(k, k + 1, 2 * (n - 1 - k), False, 0) for k in range(n)]
    runs = [(2 * j, 2 * j + 2, b"x" if j % 2 else b"") for j in range(n)]
    t0 = time.monotonic()
    out = doc_fib.cp_runs(runs, pcs)
    took = time.monotonic() - t0
    assert len(out) == n and out[0] == (0, 1, b"x" if (n - 1) % 2 else b"", 0) and took < 5, took
    # one run over every piece, and a run that covers half of a UTF-16 unit: a character goes with its first byte
    assert doc_fib.cp_runs([(0, 10**9, b"a")], pcs[:3]) == [(0, 1, b"a", 0), (1, 2, b"a", 0), (2, 3, b"a", 0)]
    half = [doc_fib.Piece(0, 4, 100, False, 7)]
    assert doc_fib.cp_runs([(101, 105, b"a")], half) == [(0, 1, b"", 7), (1, 3, b"a", 7), (3, 4, b"", 7)]
    assert doc_fib.cp_runs([(0, 50, b"a"), (40, 60, b"b")], [doc_fib.Piece(0, 60, 0, True, 0)]) == [
        (0, 50, b"a", 0), (50, 60, b"b", 0)]  # overlapping runs: the first keeps its bytes


def test_big_prc_on_many_runs_linear():
    # one piece whose Prm1 names a 32 KiB Prc, under 20,000 runs each with its own CHPX: the Prc is applied as the
    # few sprms that decide it, not 10,922 sprms a run
    n = 20_000
    table = clx([0, n + 1], [(DOC_EXTRA, 0 << 1 | 1)], prcs=(sp(BOLD, 0) * 10_922,))
    spans = [(k, k + 1, sp(0x6A03, k)) for k in range(n)]
    p = doc_file([], fc={33: table}, word_extra=("a" * n + "\r").encode("utf-16-le"), ccp={"text": n + 1}, chpx=spans)
    t0 = time.monotonic()
    out = md(p)
    took = time.monotonic() - t0
    assert out == "a" * n + "\n" and took < 10, took


def test_huge_papx_reads_bounded():
    # 5,000 paragraphs whose PAPXs point at 5,000 overlapping 32 KiB PrcData: one is read, the rest would read more
    # than the Data stream holds and are counted; one offset named by every paragraph is read once
    n, w = 5_000, 0x7FFE
    data = u16(w) * (n + w // 2 + 1)  # every even offset holds cb w, then w bytes of sprm 0x7FFE
    text = "p\r" * n
    marks = [(2 * k, 2 * k + 2, u16(0) + sp(HUGE, 2 * k)) for k in range(n)]
    t0 = time.monotonic()
    d, ctx = parse(doc_file([(text, False)], papx=marks, streams={"Data": data}))
    took = time.monotonic() - t0
    assert len(d.blocks) == n and ctx.skipped["styles"] == n - 1 and took < 10, (took, ctx.skipped)
    same = [(2 * k, 2 * k + 2, u16(k % 7) + sp(HUGE, 0)) for k in range(n)]
    d, ctx = parse(doc_file([(text, False)], papx=same, streams={"Data": data}))
    assert len(d.blocks) == n and ctx.skipped["styles"] == 0, ctx.skipped


def test_many_paragraphs_with_props_linear():
    n = 20_000
    text = "".join(f"p{k}\r" for k in range(n))
    marks = []
    at = 0
    for k in range(n):
        at += len(f"p{k}\r")
        marks.append((at - 1, at, u16(0) + sp(OUTLINE, k % 3)))
    p = doc_file([(text, False)], papx=marks, chpx=[(0, len(text), sp(BOLD, 1))])
    t0 = time.monotonic()
    d, _ = parse(p)
    assert len(d.blocks) == n and time.monotonic() - t0 < 10


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
