"""Word 6/95 (.doc, wIdent 0xA5DC) reader: the Word 6 FIB, 8-bit text with and without a piece table, Word 6 sprms
and style sheet, tables, notes, comments, double-byte text and the corpus file.

Run: python -I -X utf8 tests/test_reader_doc6.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import markdown  # noqa: E402
from reader.formats import doc, doc_fib  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import _doc_fkp, doc6_stsh, make_cfb, make_doc6  # noqa: E402

CORPUS = Path("C:/Users/hunje/research/ai-document-reader-plugins/bench/corpus/public/word/tika__testWORD6.doc")

# Word 6 sprms: one-byte codes
ISTD, IN_TABLE, TTP, BOLD, VANISH, SPEC, RMARK, CISTD, DEF_TABLE = 2, 24, 25, 85, 92, 117, 66, 80, 190
JC, FTC, HPS, LID = 5, 93, 99, 97


def tmp():
    return Path(tempfile.mkdtemp())


def sp(op, *operand):
    return bytes([op]) + bytes(operand)


def sp16(op, v):
    return bytes([op]) + struct.pack("<H", v)


def tdef6(bounds, grf=()):
    """A Word 6 sprmTDefTable: u16 cb (the operand plus one), itcMac, the boundaries, a 10-byte TC a cell."""
    k = len(bounds) - 1
    body = bytes([k]) + struct.pack("<%dh" % (k + 1), *bounds)
    body += b"".join(struct.pack("<H", grf[i] if i < len(grf) else 0) + bytes(8) for i in range(k))
    return bytes([DEF_TABLE]) + struct.pack("<H", len(body) + 1) + body


def read(path, hidden=False):
    ctx = Context()
    d = doc.parse(Path(path), ctx)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped), ctx


def denied(path, kind):
    try:
        doc.parse(Path(path), Context())
    except ReadError as e:
        assert e.kind == kind, e
        return e
    raise AssertionError(f"expected ReadError {kind}")


def test_plain_text_without_a_piece_table():
    out, ctx = read(make_doc6(tmp() / "a.doc", ["The quick brown fox\rjumps\r"]))
    assert out == "The quick brown fox\n\njumps\n", out
    assert not ctx.skipped, ctx.skipped
    out, _ = read(make_doc6(tmp() / "a.doc", ["Word 95\r"], ident=0xA5EC, nfib=104))  # Word 95's own FIB
    assert out == "Word 95\n", out


def test_headers_and_footers_by_grpfihdt():
    # PlcfHdd holds only the stories the grpfIhdt bits name: the DOP's (two separators here), then the section's
    # sprmSGprfIhdt, odd header (bit 1) and odd footer (bit 3)
    main, header = "body\r", "sep\rcont\rTop\rBottom\r\r"
    stories = [0, 4, 9, 13, 20, 21]
    text = main + header
    sepx = struct.pack("<H", 2) + bytes([153, 0x02 | 0x08])
    at = 0x300 + len(text.encode("cp1252"))
    fc = {0: sepx, 6: struct.pack("<2I", 0, len(main)) + struct.pack("<hihi", 0, at, 0, -1),
          11: struct.pack("<%dI" % len(stories), *stories), 31: bytes([0, 0x01 | 0x02]) + bytes(82)}
    out, ctx = read(make_doc6(tmp() / "a.doc", [text], ccp={"text": len(main), "header": len(header)}, fc=fc))
    assert out == "Header: Top\nFooter: Bottom\n\nbody\n", out
    assert not ctx.skipped, ctx.skipped


def test_corpus_file():
    if not CORPUS.is_file():
        return
    out, ctx = read(CORPUS)
    assert out.endswith("\n\nThe quick brown fox jumps over the lazy dog\n") and "author: Nevin Nollop" in out, out
    assert not ctx.skipped, ctx.skipped


def test_piece_table():
    # fComplex: the Clx in WordDocument, each PCD's fc a plain file offset of 8-bit text
    p = make_doc6(tmp() / "a.doc", ["first ", "second\r", "third\r"], flags=0x0004)
    out, _ = read(p)
    assert out == "first second\n\nthird\n", out


def test_bold_hidden_and_inserted_runs():
    text = "a bold b gone c new\r"
    spans = [(2, 6, sp(BOLD, 1)), (9, 13, sp(VANISH, 1)), (16, 19, sp(RMARK, 1)), (0, 2, sp(FTC, 4, 0) + sp(HPS, 24, 0))]
    out, _ = read(make_doc6(tmp() / "a.doc", [text], chpx=spans))
    assert out == "(1 hidden places left out)\n\na **bold** b  c (inserted: new)\n", out
    shown, _ = read(make_doc6(tmp() / "a.doc", [text], chpx=spans), hidden=True)
    assert "gone" in shown, shown


def test_heading_style_and_its_run_properties():
    # istd 1 is "heading 1" (sti 1) with a bold UpxChpx; the paragraph names it with sprmPIstd in its PAPX. istd 2,
    # a user style, has a bold UpxChpx after a name of odd length, so its UPXs start one byte past the name's NUL
    stsh = doc6_stsh([(0, 1, 0xFFF, "Normal", sp(JC, 0), sp(HPS, 24, 0)),
                      (1, 1, 0, "heading 1", sp(JC, 1), sp(BOLD, 1) + sp(HPS, 28, 0)),
                      (0xFFE, 1, 0, "Bold2", b"", sp(BOLD, 1))])
    text = "Title\rbody\rplain\r"
    papx = [(0, 6, struct.pack("<H", 1)), (6, 11, struct.pack("<H", 2))]
    out, ctx = read(make_doc6(tmp() / "a.doc", [text], fc={1: stsh}, papx=papx))
    assert out == "# Title\n\n**body**\n\nplain\n", out
    assert not ctx.skipped, ctx.skipped


def test_unknown_sprm_sizes_walk_to_the_end():
    # sprms the reader does not read, each of the operand size Word 6 gives it, before the ones it reads
    grpprl = (sp(JC, 1) + sp16(16, 100) + sp16(17, 200) + bytes([20]) + bytes(4) + sp(44, 1) + sp(51, 1)
              + sp16(FTC, 3) + sp16(LID, 0x409) + sp(98, 2) + sp(95, 0, 0, 0) + bytes([68, 4]) + bytes(4) + sp(BOLD, 1))
    ops = doc_fib.sprms(grpprl, old=True)
    assert [op for op, _ in ops][-1] == 0x0835 and ops[-1][1] == b"\x01", ops


def test_table():
    a, b, c, d = "a\x07", "b\x07", "c\x07", "d\x07"
    text = a + b + "\x07" + c + d + "\x07" + "after\r"
    papx, at = [], 0
    for k, cell in enumerate((a, b, "\x07", c, d, "\x07")):
        end = at + len(cell)
        row = cell == "\x07"
        grpprl = sp(IN_TABLE, 1) + (sp(TTP, 1) + tdef6([0, 1000, 2000]) if row else b"")
        papx.append((at, end, struct.pack("<H", 0) + grpprl))
        at = end
    out, ctx = read(make_doc6(tmp() / "a.doc", [text], papx=papx))
    assert out == "a|b\n-|-\nc|d\n\nafter\n", out
    assert not ctx.skipped, ctx.skipped


def test_table_merge():
    text = "a\x07b\x07\x07c\x07d\x07\x07"
    papx, at = [], 0
    for cell, grf in (("a\x07", None), ("b\x07", None), ("\x07", (1, 2)), ("c\x07", None), ("d\x07", None),
                      ("\x07", ())):
        end = at + len(cell)
        grpprl = sp(IN_TABLE, 1) + (sp(TTP, 1) + tdef6([0, 1, 2], grf) if grf is not None else b"")
        papx.append((at, end, struct.pack("<H", 0) + grpprl))
        at = end
    out, _ = read(make_doc6(tmp() / "a.doc", [text], papx=papx))
    assert out == "|a<br>b||\n-|-\nc|d\n\nMerged: A1:B1\n", out


def test_footnote():
    main = "see\x02 here\r"
    note = "\x02 the note\r\r"
    fc = {2: struct.pack("<2I", 3, len(main)) + struct.pack("<h", 1),
          3: struct.pack("<3I", 0, len(note) - 1, len(note))}
    out, ctx = read(make_doc6(tmp() / "a.doc", [main + note], ccp={"text": len(main), "footnote": len(note)}, fc=fc))
    assert out == "see[^1] here\n\n[^1]: the note\n", out
    assert not ctx.skipped, ctx.skipped


def test_comment_with_its_author():
    # a Word 6 ATRD is 20 bytes: xstUsrInitl (u8 count, 9 chars), ibst u16 at 10, ak, grfbmc, lTagBkmk; the owners
    # are 8-bit Pascal strings
    main = "text\x05 more\r"
    comment = "\x05 a remark\r\r"
    atrd = bytes([2]) + b"JD".ljust(9, b"\0") + struct.pack("<HHHi", 1, 0, 0, -1)
    fc = {4: struct.pack("<2I", 4, len(main)) + atrd, 5: struct.pack("<3I", 0, len(comment) - 1, len(comment)),
          36: bytes([3]) + b"Ann" + bytes([4]) + b"Jane"}
    out, ctx = read(make_doc6(tmp() / "a.doc", [main + comment], ccp={"text": len(main), "annotation": len(comment)},
                              fc=fc))
    assert "Jane" in out and "a remark" in out, out
    assert not ctx.skipped, ctx.skipped


def test_double_byte_text_counts_a_byte_a_cp():
    # Korean Word 95: cp949 text, one CP a byte, so a run's CPs count the two bytes of each syllable
    text = "가나 abc 다\r"
    start = len("가나 ".encode("cp949"))
    p = make_doc6(tmp() / "a.doc", [text], lid=0x0412, codec="cp949", chpx=[(start, start + 3, sp(BOLD, 1))])
    out, _ = read(p)
    assert out == "가나 **abc** 다\n", out
    bold = len("가".encode("cp949"))
    p = make_doc6(tmp() / "a.doc", [text], lid=0x0412, codec="cp949", chpx=[(bold, 2 * bold, sp(BOLD, 1))])
    out, _ = read(p)
    assert out == "가**나** abc 다\n", out


def test_mac_character_set():
    out, _ = read(make_doc6(tmp() / "a.doc", ["café\r"], chse=256, codec="mac_roman"))
    assert out == "café\n", out


def test_pages_past_the_plcfbte():
    # cpnBteChp says more FKP pages than PlcfBteChpx lists: the others follow the last one listed
    text = "".join(f"w{k} " for k in range(400)) + "\r"
    spans = [(k * 6, k * 6 + 2, sp(BOLD, 1)) for k in range(120)]
    pcd = [(0, len(text), 0x300, 1)]
    full = make_doc6(tmp() / "a.doc", [text], chpx=spans)
    out_full, _ = read(full)
    pages, bte = _doc_fkp(spans, pcd, False, 0, old=True)
    n = len(pages) // 512
    assert n >= 2, n
    word = bytearray(_word(full))
    fc, lcb = struct.unpack_from("<II", word, 0x58 + 8 * 12)
    first_pn = struct.unpack_from("<H", word, fc + 4 * (n + 1))[0]
    one = struct.pack("<2IH", *struct.unpack_from("<2I", word, fc), first_pn)
    word[fc:fc + lcb] = one.ljust(lcb, b"\0")
    struct.pack_into("<I", word, 0x58 + 8 * 12 + 4, len(one))
    p = tmp() / "b.doc"
    make_cfb(p, {"WordDocument": bytes(word)})
    out, ctx = read(p)
    assert out == out_full and out.count("**") == 240, out[:200]
    assert not ctx.skipped, ctx.skipped


def _word(path):
    from reader.containers.cfb import Compound
    with Compound(str(path)) as cf:
        return cf.read("WordDocument")


def test_encrypted_and_older_files():
    denied(make_doc6(tmp() / "a.doc", ["x\r"], flags=0x0100), "encrypted")
    p = tmp() / "w2.doc"
    make_cfb(p, {"WordDocument": struct.pack("<2H", 0xA5DB, 45) + bytes(800)})
    assert denied(p, "unsupported").detail == "word 2"
    p = tmp() / "short.doc"
    make_cfb(p, {"WordDocument": struct.pack("<2H", 0xA5DC, 101) + bytes(300)})
    denied(p, "damaged")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
