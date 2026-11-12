"""Word 97-2003 (.doc) reader: FIB, piece table, story text with code pages, special characters, fields, hostile input.

Run: python -I -X utf8 tests/test_reader_doc.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import doc, doc_fib  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import DOC_EXTRA, make_cfb, make_doc, make_zip  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def u32(n):
    return struct.pack("<I", n)


def doc_file(pieces, name="a.doc", **kw):
    return make_doc(tmp() / name, pieces, **kw)


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    d, ctx = parse(path)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


def denied(path, kind):
    try:
        parse(path)
    except ReadError as e:
        assert e.kind == kind, e
        return e
    raise AssertionError(f"expected ReadError {kind}")


def streams_of(path):
    with Compound(path) as cf:
        return {n: cf.read(n) for n in cf.names()}


def clx(cps, pcds, prcs=()):
    """A Clx: Prc records (clxt 1, u16 cb, grpprl), then the Pcdt (clxt 2, u32 lcb, PlcPcd). pcds are (fc, prm)."""
    plc = b"".join(u32(c) for c in cps) + b"".join(u16(0) + u32(f) + u16(p) for f, p in pcds)
    return b"".join(b"\x01" + u16(len(g)) + g for g in prcs) + b"\x02" + u32(len(plc)) + plc


# ---- text and code pages
def test_unicode_text():
    assert md(doc_file([("안녕하세요\r두 번째\r", False)])) == "안녕하세요\n\n두 번째\n"


def test_compressed_cp1252():
    assert md(doc_file([("café\r", True)])) == "café\n"


def test_compressed_cyrillic_lid():
    assert md(doc_file([("Привет\r", True)], lid=0x0419)) == "Привет\n"
    same = "Привет\r".encode("cp1251").decode("cp1252")  # the very bytes, written under lid 0x0409
    out = md(doc_file([(same, True)], lid=0x0409))
    assert out == same.strip() + "\n" and not any("Ѐ" <= ch <= "ӿ" for ch in out), out


def test_far_east_lid():
    # a Far East document records lid as American: the language is lidFE's (anydoc), when it is single-byte
    p = doc_file([("Привет\r", True)], lid=0x0409, lid_fe=0x0419, flags=0x0200 | 0x4000)
    assert md(p) == "Привет\n"
    same = "Привет\r".encode("cp1251").decode("cp1252")  # the cp1251 bytes, in a document that is not Far East
    q = doc_file([(same, True)], lid=0x0409, lid_fe=0x0419, flags=0x0200)
    assert md(q) == same.strip() + "\n"


def test_compressed_under_double_byte_lid_is_cp1252():
    # compressed text is one byte a character (MS-DOC 2.9.73): under a double-byte code page it is cp1252 (dochan)
    text = "“Hello” – a café…\r"
    p = doc_file([(text, True)], lid=0x0409, lid_fe=0x0412, flags=0x0200 | 0x4000)  # English typed in Korean Word
    assert md(p) == "“Hello” – a café…\n"
    for lid in (0x0412, 0x0411, 0x0404, 0x0804):
        assert md(doc_file([(text, True)], lid=lid, flags=0x0200 | 0x4000)) == "“Hello” – a café…\n", hex(lid)
    raw = "한글\r".encode("cp949").decode("cp1252")  # cp949 bytes in a compressed piece are cp1252 characters
    r = doc_file([(raw, True)], lid=0x0409, lid_fe=0x0412, flags=0x0200 | 0x4000)
    assert md(r) == raw.strip() + "\n"
    assert doc_fib.compressed_codepage(0x0412) == "cp1252" and doc_fib.compressed_codepage(0x0419) == "cp1251"


def test_mixed_pieces():
    p = doc_file([("plain ", True), ("한국어 ", False), ("café\r", True), ("끝\r", False)])
    assert md(p) == "plain 한국어 café\n\n끝\n"


def test_breaks():
    d, _ = parse(doc_file([("a\x0bb\x0cc\r", False)]))
    assert len(d.blocks) == 2
    assert md(doc_file([("a\x0bb\x0cc\r", False)])) == "a\nb\n\nc\n"


def test_special_characters():
    p = doc_file([("a\x01\x02\x05\x08\x1fb\x1ec\x00\r", False)])
    assert md(p) == "ab-c\n"
    assert md(doc_file([("a\tb\r", False)])) == "a\tb\n"


def test_cell_mark_ends_a_paragraph():
    # without sprmPFInTable a cell mark ends a plain paragraph (tables: test_reader_doc_tables) and no control
    # character reaches the output
    assert md(doc_file([("a\x07b\x07\x07c\r", False)])) == "a\n\nb\n\nc\n"


def test_text_without_final_mark():
    assert md(doc_file([("a\rb", False)])) == "a\n\nb\n"


# ---- fields
def test_field_result_kept():
    assert md(doc_file([("\x13 PAGE \x14" + "3" + "\x15\r", False)])) == "3\n"
    nested = "a \x13 IF 1 = 1 \x14\x13 PAGE \x143\x15 left\x15 b\r"
    assert md(doc_file([(nested, False)])) == "a 3 left b\n"
    in_code = "\x13 IF \x13 PAGE \x143\x15 = 3 \"x\" \x14yes\x15\r"  # a field inside an instruction is instruction
    assert md(doc_file([(in_code, False)])) == "yes\n"
    assert md(doc_file([("\x13 TIME \x15x\r", False)])) == "x\n"  # no separator: no result


def test_hyperlink_field():
    p = doc_file([("\x13 HYPERLINK \"https://example.com\" \x14click\x15\r", False)])
    assert md(p) == "[click](https://example.com)\n"
    inner = "\x13 HYPERLINK \"https://a.example\" \x14go \x13 PAGE \x142\x15 now\x15\r"
    assert md(doc_file([(inner, False)])) == "[go 2 now](https://a.example)\n"
    anchor = "\x13 HYPERLINK \\l \"_Toc1\" \x14Intro\x15\r"  # a bookmark is no URL: the text stays plain
    assert md(doc_file([(anchor, False)])) == "Intro\n"


def test_unterminated_field_stays_in_its_paragraph():
    assert md(doc_file([("a\x13 PAGE \rb\x15c\r", False)])) == "a\n\nbc\n"


def test_field_depth():
    deep = "\x13 A \x14" * 70 + "deep" + "\x15" * 70 + "ok\r"
    d, ctx = parse(doc_file([(deep, False)]))
    assert ctx.skipped["depth"] == 1, ctx.skipped
    assert markdown.render(d, tmp() / "assets") == "ok\n"
    code = "\x13" * 70 + " X \x14deep" + "\x15" * 70 + "ok\r"
    d, ctx = parse(doc_file([(code, False)]))
    assert ctx.skipped["depth"] == 1 and markdown.render(d, tmp() / "assets") == "ok\n"
    assert limits.MAX_DEPTH < 70


# ---- protection, other formats
def test_encrypted():
    denied(doc_file([("x\r", False)], flags=0x0200 | 0x0100), "encrypted")
    denied(doc_file([("x\r", False)], flags=0x0200 | 0x8000), "encrypted")
    # past FibBase an encrypted FIB is ciphertext: its counts are not read, so it never reads as damaged
    p = tmp() / "e.doc"
    word = bytearray(streams_of(doc_file([("x\r", False)]))["WordDocument"])
    struct.pack_into("<H", word, 0x0A, 0x0200 | 0x0100)
    word[0x40:0x400] = b"\xff" * (0x400 - 0x40)
    make_cfb(p, {"WordDocument": bytes(word), "1Table": b""})
    denied(p, "encrypted")


def test_word95():
    e = denied(doc_file([("x\r", False)], nfib=0x0068), "unsupported")
    assert e.detail == "word 6/95"
    p = tmp() / "w6.doc"
    make_cfb(p, {"WordDocument": u16(0xA5DC) + u16(0x0065) + bytes(500)})  # Word 6/95's own wIdent
    assert denied(p, "unsupported").detail == "word 6/95"


def test_not_word():
    p = tmp() / "n.doc"
    make_cfb(p, {"Other": b"x" * 10})
    denied(p, "damaged")
    q = tmp() / "m.doc"
    make_cfb(q, {"WordDocument": b"\x00\x01" + bytes(100)})
    assert denied(q, "damaged").detail == "not a Word document"
    r = tmp() / "t.doc"
    r.write_bytes(b"\x00" * 600)
    denied(r, "damaged")


def test_encrypted_package_saved_as_doc():
    p = tmp() / "enc.doc"
    make_cfb(p, {"EncryptionInfo": b"\x04\x00\x04\x00" + bytes(60), "EncryptedPackage": bytes(64)})
    denied(p, "encrypted")


def test_rtf_saved_as_doc():
    p = tmp() / "r.doc"
    p.write_bytes(b"{\\rtf1\\ansi hello}")
    assert denied(p, "unsupported").detail == "rtf"
    q = tmp() / "b.doc"
    q.write_bytes(b"\xef\xbb\xbf{\\rtf1 hi}")
    assert denied(q, "unsupported").detail == "rtf"


def test_doc_zip_is_word():
    members = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>'),
        "word/document.xml": (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>워드 본문</w:t></w:r></w:p>'
                              '</w:body></w:document>'),
    }
    p = make_zip(tmp() / "x.doc", members, stored=())
    assert md(p) == "워드 본문\n"
    out = tmp() / "out"
    reader.convert(p, out)
    assert (out / "out.md").read_text(encoding="utf-8") == "워드 본문\n"


# ---- the piece table and the FIB, hostile input
def test_bad_clx():
    p = doc_file([("x\r", False)], streams={"1Table": bytes(8)})  # the Clx's fc and lcb now run past the stream
    denied(p, "damaged")
    q = doc_file([("x\r", False)], fc={33: b"\x02" + u32(10**6) + bytes(16)})  # the Pcdt runs past the Clx
    denied(q, "damaged")
    r = doc_file([("x\r", False)], fc={33: b"\x02" + u32(4 + 12 + 5) + bytes(21)})  # lcb not 4 + 12n
    denied(r, "damaged")
    s = doc_file([("x\r", False)], fc={33: b"\x07" + bytes(20)})  # no Pcdt at all
    denied(s, "damaged")
    t = doc_file([("x\r", False)], fc={33: b""})  # no Clx
    denied(t, "damaged")
    out = tmp() / "out"
    try:
        reader.convert(p, out)
    except ReadError as e:
        assert e.kind == "damaged" and "Traceback" not in str(e)
    else:
        raise AssertionError("expected ReadError damaged")


def test_prc_skipped():
    at = DOC_EXTRA
    table = clx([0, 3], [(at, 0)], prcs=(b"\x35\x08\x01", b"", bytes(300)))
    p = doc_file([], fc={33: table}, word_extra="ab\r".encode("utf-16-le"), ccp={"text": 3})
    assert md(p) == "ab\n"


def test_pieces_fields():
    data = clx([0, 4, 9], [(0x40000000 | 2 * 1000, 7), (2000, 0x0101)])
    pcs = doc_fib.pieces(b"pad" + data, 3, len(data))
    assert [(p.cp, p.cp_end, p.fc, p.compressed, p.prm) for p in pcs] == [(0, 4, 1000, True, 7),
                                                                         (4, 9, 2000, False, 0x0101)]


def test_cp_decreasing():
    p = doc_file([], fc={33: clx([0, 5, 3], [(DOC_EXTRA, 0), (DOC_EXTRA, 0)])}, word_extra=bytes(20))
    denied(p, "damaged")


def test_piece_past_stream_cut():
    word_extra = "abc".encode("utf-16-le")
    p = doc_file([], fc={33: clx([0, 3, 1000], [(DOC_EXTRA, 0), (DOC_EXTRA, 0)])}, word_extra=word_extra,
                 ccp={"text": 1000})
    d, ctx = parse(p)
    assert ctx.skipped["text"] == 1, ctx.skipped  # the second piece holds 997 CPs past the end of WordDocument
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert out.startswith("(could not read: 1 text run cut short)") and "abc" in out, out
    q = doc_file([], fc={33: clx([0, 3], [(10**6, 0)])}, ccp={"text": 3})  # wholly outside
    d, ctx = parse(q)
    assert ctx.skipped["text"] == 1 and d.blocks == []


def test_overlapping_pieces_bounded():
    # 2000 pieces all naming the same 600 bytes: the text read stays within the stream's size, the rest counted
    n, size = 2000, 300
    cps = [k * size for k in range(n + 1)]
    p = doc_file([], fc={33: clx(cps, [(DOC_EXTRA, 0)] * n)}, word_extra=("x" * size).encode("utf-16-le"),
                 ccp={"text": n * size})
    d, ctx = parse(p)
    word = DOC_EXTRA + 2 * size
    text = "".join(it.text for b in d.blocks for it in b.inlines)
    assert len(text) <= word // 2 + size and ctx.skipped["text"] > 0, (len(text), ctx.skipped)


def test_story_ranges():
    # the main story is the first ccpText CPs; later stories follow it, macro included (Word 97 order)
    p = doc_file([("main\rnote\r", False)], ccp={"text": 5, "footnote": 5})
    assert md(p) == "main\n"
    ranges = doc_fib.story_ranges({"text": 5, "footnote": 2, "header": 3, "macro": 1, "annotation": 4}, 100)
    assert ranges["text"] == (0, 5) and ranges["header"] == (7, 10) and ranges["annotation"] == (11, 15)
    assert ranges["header_textbox"] == (15, 15)
    assert doc_fib.story_ranges({"text": 10**9, "footnote": 10**9}, 7) == {
        **{name: (7, 7) for name in doc_fib.STORIES}, "text": (0, 7)}
    big = doc_file([("abc\r", False)], ccp={"text": 2**32 - 1})  # clamped to the text the pieces hold
    assert md(big) == "abc\n"


def test_fib_fields():
    word = streams_of(doc_file([("x\r", False)], lid=0x0419, lid_fe=0x0412, flags=0x0200 | 0x4000,
                               ccp={"text": 1, "footnote": 1}))["WordDocument"]
    f = doc_fib.fib(word)
    assert (f.nfib, f.lid, f.lid_fe, f.far_east, f.encrypted, f.obfuscated, f.table) == (
        0xC1, 0x0419, 0x0412, True, False, False, "1Table")
    assert f.ccp == {"text": 1, "footnote": 1, "header": 0, "macro": 0, "annotation": 0, "endnote": 0,
                     "textbox": 0, "header_textbox": 0}
    assert len(f.fc) == 93 and f.fc[33][1] > 0 and f.pair(33) == f.fc[33] and f.pair(500) == (0, 0)
    flags0 = bytearray(word)
    struct.pack_into("<H", flags0, 0x0A, 0)
    assert doc_fib.fib(bytes(flags0)).table == "0Table"


def test_fib_counts_bounded():
    word = bytearray(streams_of(doc_file([("x\r", False)]))["WordDocument"])
    for off, value in ((0x20, 0xFFFF), (0x3E, 0xFFFF), (0x98, 0xFFFF)):  # csw, cslw, cbRgFcLcb
        bad = bytearray(word)
        struct.pack_into("<H", bad, off, value)
        try:
            doc_fib.fib(bytes(bad[:2000]))
        except ReadError as e:
            assert e.kind == "damaged", e
        else:
            raise AssertionError(hex(off))
    for cut in (1, 20, 33, 0x3F, 0x90, 0x99, 0x200):
        try:
            doc_fib.fib(bytes(word[:cut]))
        except ReadError as e:
            assert e.kind == "damaged", (cut, e)
        else:
            raise AssertionError(cut)
    short = bytearray(word[:0x40])  # cslw 0 and cbRgFcLcb 0: no stories and no pairs, no error
    struct.pack_into("<H", short, 0x3E, 0)
    f = doc_fib.fib(bytes(short) + u16(0))
    assert set(f.ccp.values()) == {0} and f.fc == []


def test_table_stream_missing_or_other():
    p = doc_file([("x\r", False)], flags=0x0200)
    s = streams_of(p)
    q = tmp() / "q.doc"
    make_cfb(q, {"WordDocument": s["WordDocument"], "0Table": s["1Table"]})  # the flag names 1Table
    assert md(q) == "x\n"
    r = tmp() / "r.doc"
    make_cfb(r, {"WordDocument": s["WordDocument"]})
    denied(r, "damaged")


def test_story_text_slices():
    word = b"\0" * 10 + "가나다".encode("utf-16-le") + "xyz".encode("cp1251")
    pcs = [doc_fib.Piece(0, 3, 10, False, 0), doc_fib.Piece(3, 6, 16, True, 0)]
    assert doc_fib.story_text(word, pcs, 0, 6, 0x0419) == "가나다xyz"
    assert doc_fib.story_text(word, pcs, 1, 5, 0x0419) == "나다xy"
    assert doc_fib.story_text(word, pcs, 5, 99, 0x0419) == "z"
    assert doc_fib.story_text(word, pcs, 6, 6, 0x0419) == ""
    odd = "가".encode("utf-16-le") + b"A"  # a Unicode piece whose last unit is cut: the whole units only
    assert doc_fib.story_text(odd, [doc_fib.Piece(0, 2, 0, False, 0)], 0, 2, 0x0409) == "가"


# ---- the walk Tasks 10-14 extend
class _Marks(doc._Reader):
    """The reader with a \\x02 handler that records the CP it is given and the index Story maps it back to."""
    SPECIAL = {**doc._Reader.SPECIAL, "\x02": "mark"}

    def mark(self, st, cp):
        self.marks.append((cp, st.story.index_of(cp), st.story.cp_of(st.story.index_of(cp))))


def marks(path):
    with Compound(path) as cf:
        r = _Marks(cf, Context())
        r.marks = []
        r.run()
    for cp, _, back in r.marks:
        assert back == cp, r.marks
    return [cp for cp, _, _ in r.marks]


def test_handler_cp_after_emoji():
    assert marks(doc_file([("😀\x02\r", False)])) == [2]  # a surrogate pair is two CPs
    assert marks(doc_file([("a😀\x02😀😀b\x02\r", False)])) == [3, 9]


def test_handler_cp_after_compressed_piece():
    assert marks(doc_file([("ab", True), ("y\x02\r", False)])) == [3]
    # two bytes that would be one cp949 character are two CPs, as two cp1252 characters
    p = doc_file([("ÇÑ", True), ("y\x02\r", False)], lid=0x0409, lid_fe=0x0412, flags=0x0200 | 0x4000)
    assert marks(p) == [3]


def test_handler_cp_after_cut_piece():
    # CPs 3-99 are a piece wholly past the stream: the \x02 after the gap is still CP 100
    word_extra = "abc\x02\r".encode("utf-16-le")
    table = clx([0, 3, 100, 102], [(DOC_EXTRA, 0), (10**6, 0), (DOC_EXTRA + 6, 0)])
    assert marks(doc_file([], fc={33: table}, word_extra=word_extra, ccp={"text": 102})) == [100]


def test_emoji_renders_as_one_character():
    assert md(doc_file([("a😀b 𠀀\r", False)])) == "a😀b 𠀀\n"
    link = "\x13 HYPERLINK \"https://e.example/😀\" \x14go 😀\x15\r"
    assert md(doc_file([(link, False)])) == "[go 😀](https://e.example/😀)\n"
    lone = u16(0xD800) + "x\r".encode("utf-16-le")  # an unpaired surrogate becomes U+FFFD
    p = doc_file([], fc={33: clx([0, 3], [(DOC_EXTRA, 0)])}, word_extra=lone, ccp={"text": 3})
    assert md(p) == "�x\n"


def test_story_maps_index_and_cp():
    hi, lo = chr(0xD83D), chr(0xDE00)  # 😀 as the two UTF-16 units Story keeps
    word = "a😀".encode("utf-16-le") + b"xyz"
    pcs = [doc_fib.Piece(0, 3, 0, False, 0), doc_fib.Piece(10, 13, 6, True, 0)]
    s = doc_fib.story(word, pcs, 0, 13, 0x0409)
    assert s.text == "a" + hi + lo + "xyz" and doc_fib.story_text(word, pcs, 0, 13, 0x0409) == s.text
    assert [s.cp_of(i) for i in range(7)] == [0, 1, 2, 10, 11, 12, 13]
    assert [s.index_of(cp) for cp in (0, 2, 3, 5, 10, 12, 13, 99)] == [0, 2, 3, 3, 3, 5, 6, 6]
    part = doc_fib.story(word, pcs, 2, 12, 0x0409)  # starts inside the pair: one unit per CP still
    assert part.text == lo + "xy" and part.cp_of(0) == 2 and part.cp_of(1) == 10 and part.index_of(11) == 2
    empty = doc_fib.story(word, pcs, 4, 9, 0x0409)  # all in the gap
    assert empty.text == "" and empty.cp_of(0) == 4 and empty.index_of(7) == 0


def test_hyperlink_rule_shared():
    from reader.formats import ooxml_word, word_fields
    assert doc.hyperlink_url is word_fields.hyperlink_url and ooxml_word.hyperlink_url is word_fields.hyperlink_url
    assert word_fields.hyperlink_url(' HYPERLINK "https://a.example" \\o "tip"') == "https://a.example"
    assert word_fields.hyperlink_url("HYPERLINK https://b.example") == "https://b.example"
    assert word_fields.hyperlink_url(' HYPERLINK \\l "_Toc1"') == "" and word_fields.hyperlink_url(" PAGE ") == ""
    assert len(word_fields.hyperlink_url('HYPERLINK "' + "u" * 5000 + '"')) == word_fields.MAX_URL


def test_special_handlers_exist():
    for ch, name in doc._Reader.SPECIAL.items():
        assert len(ch) == 1 and ord(ch) < 0x20 and callable(getattr(doc._Reader, name, None)), (ch, name)
    for ch in "\r\x07\x0c":
        assert doc._Reader.SPECIAL[ch] == "para_end"


def test_large_text_is_linear():
    text = "".join(f"줄 {k} \x13 PAGE \x14{k}\x15 끝\r" for k in range(40_000))
    p = doc_file([(text, False)])
    t0 = time.monotonic()
    d, _ = parse(p)
    assert len(d.blocks) == 40_000 and time.monotonic() - t0 < 10


def test_convert_end_to_end():
    out = tmp() / "out"
    meta = reader.convert(doc_file([("본문\r", False)]), out)
    assert (out / "out.md").read_text(encoding="utf-8") == "본문\n"
    assert meta["lines"] == 1 and meta["unread"] == [], meta
    assert "doc" in reader.BUILT and reader.FORMATS["doc"] == "doc"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
