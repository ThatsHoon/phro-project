"""pdfnative objects, structure and filters: the lexer, xref tables and streams, object
streams, recovery by scan, the page tree, and the stream decoders under their caps.

Run: python -I -X utf8 tests/test_reader_pdf_objects.py
"""
import base64
import random
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import Name, Ref, Stream, make_pdf, pdf_obj, simple_pdf, text_ops  # noqa: E402
from reader import limits  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.pdfnative import filters, structure  # noqa: E402
from reader.pdfnative.filters import DecodeBudget, decode_stream  # noqa: E402
from reader.pdfnative.objects import (MAX_COLLECTION_ITEMS, PDFLexer, PDFName, PDFRef,  # noqa: E402
                                      PDFStream, PDFSyntaxError, parse_indirect_object,
                                      parse_object_stream)
from reader.pdfnative.structure import PDFFile  # noqa: E402


def flate(name="FlateDecode", **extra):
    return dict({"Filter": PDFName(name)}, **extra)


def page_text(pf, index=0):
    page, _ = pf.pages()[index]
    return pf.decode_stream_bytes(pf.resolve(page["Contents"]))


def test_lexer_objects():
    lexer = PDFLexer(b"<< /A 1 /B [true null 2.5 (a\\)b) <414243> /N#20x] /R 3 0 R >>")
    assert lexer.next_object() == {
        "A": 1, "B": [True, None, 2.5, b"a)b", b"ABC", PDFName("N x")], "R": PDFRef(3, 0)}
    assert not lexer.cut
    assert PDFLexer(b"(a\\101\\n)").next_object() == b"aA\n"
    assert PDFLexer(b"% note\n  -7").next_object() == -7


def test_classic_xref():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    assert len(pf.pages()) == 1
    assert pf.recovered is False
    assert pf.catalog["Type"] == "Catalog"
    assert page_text(pf) == text_ops("Hi")
    _, inherited = pf.pages()[0]
    assert inherited["MediaBox"] == [0, 0, 612, 792]
    assert "F1" in inherited["Resources"]["Font"]


def test_xref_stream_and_objstm():
    data = simple_pdf([text_ops("Hi")], xref="stream", objstm=True)
    assert b"/ObjStm" in data and b"/Type /Page " not in data  # the page is packed away
    pf = PDFFile(data)
    assert len(pf.pages()) == 1
    assert pf.recovered is False
    assert pf._compressed  # the page objects really come from the object stream
    assert page_text(pf) == text_ops("Hi")


def test_header_after_junk():
    pf = PDFFile(simple_pdf([text_ops("Hi")], junk=b"x" * 100))
    assert len(pf.pages()) == 1
    assert pf.recovered is False


def test_broken_offsets_recovered():
    pf = PDFFile(simple_pdf([text_ops("Hi")], shift=7))
    assert len(pf.pages()) == 1
    assert pf.recovered is True
    assert page_text(pf) == text_ops("Hi")
    pf = PDFFile(simple_pdf([text_ops("Hi")], xref="stream", shift=7))
    assert len(pf.pages()) == 1 and pf.recovered is True


def test_bad_startxref_recovered():
    data = simple_pdf([text_ops("Hi")])
    cut = data.rfind(b"startxref")
    pf = PDFFile(data[:cut] + b"startxref\n999999\n%%EOF\n")
    assert len(pf.pages()) == 1 and pf.recovered is True


def test_no_xref_recovered():
    pf = PDFFile(simple_pdf([text_ops("Hi")], xref="none"))
    assert len(pf.pages()) == 1
    assert pf.recovered is True


def test_no_catalog_damaged():
    try:
        PDFFile(b"%PDF-1.7\n" + bytes(64))
    except ReadError as e:
        assert (e.kind, e.detail) == ("damaged", "no catalog")
    else:
        raise AssertionError("expected ReadError")


def test_encrypted_refused():
    data = simple_pdf([text_ops("Hi")], trailer={"Encrypt": Ref(9)})
    try:
        PDFFile(data)
    except ReadError as e:
        assert (e.kind, e.detail) == ("encrypted", "not supported yet")
    else:
        raise AssertionError("expected ReadError")


def test_page_tree_cycle():
    objects = {
        1: {"Type": Name("Catalog"), "Pages": Ref(2)},
        2: {"Type": Name("Pages"), "Kids": [Ref(3), Ref(2)], "Count": 1},
        3: {"Type": Name("Page"), "Parent": Ref(2)},
    }
    start = time.perf_counter()
    pf = PDFFile(make_pdf(objects))
    assert len(pf.pages()) == 1
    assert time.perf_counter() - start < 1


def test_xref_prev_loop():
    data = simple_pdf([text_ops("Hi")])
    start_at = int(data[data.rfind(b"startxref") + 10:].split()[0])
    data = data.replace(b"/Root 1 0 R", b"/Root 1 0 R /Prev %d" % start_at)
    # the trailer grew, so startxref moved nothing: it still names the xref keyword
    start = time.perf_counter()
    pf = PDFFile(data)
    assert len(pf.pages()) == 1
    assert time.perf_counter() - start < 1


def test_resolve_depth():
    objects = {1: {"Type": Name("Catalog")}}
    for n in range(2, 42):
        objects[n] = Ref(n + 1)
    objects[42] = 5
    pf = PDFFile(make_pdf(objects))
    assert pf.resolve(PDFRef(2, 0)) is None  # 40 hops is past MAX_RESOLVE_DEPTH
    assert pf.resolve(PDFRef(20, 0)) == 5  # 23 hops is not
    assert structure.MAX_RESOLVE_DEPTH == 32


def test_flate_bomb():
    bomb = zlib.compress(bytes(60 * 2**20), 9)
    pdf = make_pdf({1: {"Type": Name("Catalog"), "Pages": Ref(2)},
                    2: {"Type": Name("Pages"), "Kids": [Ref(3)], "Count": 1},
                    3: {"Type": Name("Page"), "Contents": Ref(4)},
                    4: Stream({"Filter": Name("FlateDecode")}, bomb)})
    pf = PDFFile(pdf)
    start = time.perf_counter()
    out = page_text(pf)
    assert len(out) == 50 * 2**20 == filters.MAX_DECODED_SIZE
    assert pf.budget.cuts == 1
    assert time.perf_counter() - start < 5


def test_document_decode_cap():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    pf.budget.total = 3 * 2**20
    outs = [pf.decode_stream_bytes(PDFStream(flate(), zlib.compress(bytes(2**20))))
            for _ in range(4)]
    assert [len(o) for o in outs] == [2**20, 2**20, 2**20, 0]
    assert pf.budget.cuts == 1
    # a stream that only partly fits is cut, once
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    pf.budget.total = 100
    assert len(pf.decode_stream_bytes(PDFStream(flate(), zlib.compress(bytes(1000))))) == 100
    assert pf.budget.cuts == 1


def test_same_stream_decodes_once():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    stream = PDFStream(flate(), zlib.compress(bytes(1000)))
    pf.decode_stream_bytes(stream)
    pf.decode_stream_bytes(stream)
    assert pf.budget.total == filters.MAX_TOTAL_DECODED - 1000


def lzw_encode(data, early=1):
    table = {bytes([i]): i for i in range(256)}
    nxt, width = 258, 9
    bits, nbits = 0, 0
    out = bytearray()

    def emit(code):
        nonlocal bits, nbits
        bits = (bits << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 255)
        bits &= (1 << nbits) - 1

    emit(256)
    current = b""
    for byte in data:
        joined = current + bytes([byte])
        if joined in table:
            current = joined
            continue
        emit(table[current])
        table[joined] = nxt
        nxt += 1
        if width < 12 and nxt - 1 + early == 1 << width:
            width += 1
        current = bytes([byte])
    if current:
        emit(table[current])
        nxt += 1
        if width < 12 and nxt - 1 + early == 1 << width:
            width += 1
    emit(257)
    if nbits:
        out.append((bits << (8 - nbits)) & 255)
    return bytes(out)


def decode(name, raw, **parms):
    d = {"Filter": PDFName(name)}
    if parms:
        d["DecodeParms"] = parms
    return decode_stream(d, raw, [], DecodeBudget())


def test_lzw_runlength_ascii():
    plain = b"ABABAB" + b"-" * 20
    assert decode("LZWDecode", lzw_encode(plain)) == plain
    rng = random.Random(7)
    long = bytes(rng.choice(b"abcdefgh") for _ in range(6000))
    assert decode("LZWDecode", lzw_encode(long)) == long
    assert decode("LZWDecode", lzw_encode(long, 0), EarlyChange=0) == long
    rl = bytes([2]) + b"abc" + bytes([256 - 4]) + b"z" + bytes([128]) + b"ignored"
    assert decode("RunLengthDecode", rl) == b"abc" + b"zzzzz"
    assert decode("RL", bytes([0]) + b"x") == b"x"
    assert decode("ASCIIHexDecode", b"41 42\n4> junk") == b"AB@"
    assert decode("ASCII85Decode", b"<~" + base64.a85encode(plain) + b"~>") == plain
    assert decode("AHx", b"414243>") == b"ABC"


def test_chain_and_image_filters():
    plain = b"hello pdf" * 10
    d = {"Filter": [PDFName("ASCII85Decode"), PDFName("FlateDecode")]}
    raw = base64.a85encode(zlib.compress(plain)) + b"~>"
    assert decode_stream(d, raw, [], DecodeBudget()) == plain
    # an image filter ends the chain and returns the bytes so far
    d = {"Filter": [PDFName("ASCIIHexDecode"), PDFName("DCTDecode")]}
    assert decode_stream(d, b"4142>", [], DecodeBudget()) == b"AB"
    warnings = []
    assert decode_stream({"Filter": PDFName("Nope")}, b"x", warnings, DecodeBudget()) == b""
    assert warnings


def paeth_encode(rows, columns, bpp=1):
    out = bytearray()
    prev = bytes(columns)
    for kind, row in rows:
        out.append(kind)
        for i, value in enumerate(row):
            left = row[i - bpp] if i >= bpp else 0
            up = prev[i]
            up_left = prev[i - bpp] if i >= bpp else 0
            if kind == 0:
                pred = 0
            elif kind == 1:
                pred = left
            elif kind == 2:
                pred = up
            elif kind == 3:
                pred = (left + up) >> 1
            else:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
            out.append((value - pred) & 255)
        prev = bytes(row)
    return bytes(out)


def test_png_predictor():
    rows = [bytes([1, 2, 3]), bytes([4, 5, 6])]
    up = bytes([2, 1, 2, 3, 2, 3, 3, 3])
    assert decode("FlateDecode", zlib.compress(up), Predictor=12, Columns=3) == b"".join(rows)
    mixed = [(0, [10, 20, 30]), (1, [5, 9, 200]), (3, [7, 250, 3]), (4, [1, 2, 3]), (2, [9, 9, 9])]
    plain = b"".join(bytes(r) for _, r in mixed)
    packed = paeth_encode(mixed, 3)
    assert decode("FlateDecode", zlib.compress(packed), Predictor=15, Columns=3) == plain
    # TIFF predictor 2, 8 bits, one color: running sums per row
    tiff = bytes([1, 1, 1, 5, 0, 2])
    assert decode("FlateDecode", zlib.compress(tiff), Predictor=2, Columns=3) == bytes([1, 2, 3, 5, 5, 7])


def test_collection_cap():
    lexer = PDFLexer(b"[" + b"1 " * (MAX_COLLECTION_ITEMS + 1) + b"]")
    value = lexer.next_object()
    assert len(value) == MAX_COLLECTION_ITEMS == 100_000
    assert lexer.cut is True
    small = PDFLexer(b"[1 2 3]")
    assert small.next_object() == [1, 2, 3] and small.cut is False


def test_deep_nesting():
    lexer = PDFLexer(b"[" * 10_000)
    try:
        lexer.next_object()
    except PDFSyntaxError:
        pass
    assert lexer.cut is True
    # exactly at the cap parses; one deeper is cut
    ok = PDFLexer(b"[" * limits.MAX_DEPTH + b"]" * limits.MAX_DEPTH)
    ok.next_object()
    assert ok.cut is False
    over = PDFLexer(b"[" * (limits.MAX_DEPTH + 1) + b"]" * (limits.MAX_DEPTH + 1))
    try:
        over.next_object()
    except PDFSyntaxError:
        pass
    assert over.cut is True
    d = PDFLexer(b"<< /A " * 10_000)
    try:
        d.next_object()
    except PDFSyntaxError:
        pass
    assert d.cut is True


def test_indirect_stream_and_object_stream():
    data = b"7 0 obj\n<< /Length 5 >>\nstream\nhello\nendstream\nendobj\n"
    num, gen, obj = parse_indirect_object(data, 0)
    assert (num, gen, obj.raw) == (7, 0, b"hello")
    # a wrong /Length falls back to the endstream search
    data = b"7 0 obj\n<< /Length 99 >>\nstream\nhello\nendstream\nendobj\n"
    assert parse_indirect_object(data, 0)[2].raw == b"hello"
    body = b"10 0 11 5 (ab) [1 2]"
    assert parse_object_stream(body, 2, 10) == [(10, b"ab"), (11, [1, 2])]
    # offsets that all name one object do not multiply the work
    many = b" ".join(b"%d 0" % i for i in range(5000)) + b"\n"
    start = time.perf_counter()
    parse_object_stream(many + b"[" + b"1 " * 50_000 + b"]", 5000, len(many))
    assert time.perf_counter() - start < 2


def test_pdf_obj_round_trip():
    value = {"A": [1, 2.5, True, None, b"\x00\xff", "a(b)\\"], "N": Name("x y"), "R": Ref(4)}
    back = PDFLexer(pdf_obj(value)).next_object()
    assert back == {"A": [1, 2.5, True, None, b"\x00\xff", b"a(b)\\"], "N": PDFName("x y"),
                    "R": PDFRef(4, 0)}

def test_length_chain_no_recursion():
    objs = {1: {"Type": Name("Catalog"), "Pages": Ref(2)},
            2: {"Type": Name("Pages"), "Kids": [], "Count": 0}}
    for n in range(3, 3003):
        objs[n] = Stream({"Length": Ref(n + 1)}, b"x")
    objs[3003] = 1
    pf = PDFFile(make_pdf(objs))
    pf.get_object(PDFRef(3, 0))  # no RecursionError


def deep_tree(levels):
    objs = {1: {"Type": Name("Catalog"), "Pages": Ref(2)}}
    for n in range(2, 2 + levels):
        objs[n] = {"Type": Name("Pages"), "Kids": [Ref(n + 1)], "Count": 1}
    objs[2 + levels] = {"Type": Name("Page")}
    return make_pdf(objs)


def test_caps_are_counted():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    assert not pf.cuts and not any("cap" in w for w in pf.warnings)
    # a collection past the item cap, in a plain object and inside an object stream
    big = [1] * (MAX_COLLECTION_ITEMS + 1)
    pf = PDFFile(simple_pdf([text_ops("Hi")], page_extra={"Big": big}))
    pf.pages()
    assert pf.cuts["collection"] == 1 and any("cap" in w for w in pf.warnings)
    pf = PDFFile(simple_pdf([text_ops("Hi")], xref="stream", objstm=True, page_extra={"Big": big}))
    pf.pages()
    assert pf.cuts["collection"] == 1
    # a page tree deeper than MAX_TREE_DEPTH drops its page
    pf = PDFFile(deep_tree(structure.MAX_TREE_DEPTH + 5))
    assert pf.pages() == [] and pf.cuts["tree_depth"] == 1
    assert len(PDFFile(deep_tree(10)).pages()) == 1


def test_pages_cap_only_when_dropped():
    old = structure.MAX_PAGES
    structure.MAX_PAGES = 3
    try:
        pf = PDFFile(simple_pdf([b"", b"", b""]))
        assert len(pf.pages()) == 3 and not pf.cuts["pages"]
        assert not any("page count" in w for w in pf.warnings)
        pf = PDFFile(simple_pdf([b"", b"", b"", b""]))
        assert len(pf.pages()) == 3 and pf.cuts["pages"] == 1
        assert any("page count" in w for w in pf.warnings)
    finally:
        structure.MAX_PAGES = old


def test_one_budget_for_content_form_and_image():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    pf.budget.total = 1500
    body = pf.decode_stream_bytes(PDFStream(flate(), zlib.compress(bytes(1000))))
    image = pf.decode_image_bytes(PDFStream(flate(), zlib.compress(bytes(1000))))
    assert len(body) == 1000 and len(image) == 500 and pf.budget.cuts == 1
    assert pf.decode_form_bytes(PDFStream(flate(), zlib.compress(bytes(10)))) is None


def byte0_junk_pdf(**kw):
    d = simple_pdf([text_ops("Hi")], shift=100, **kw)
    i = d.rfind(b"startxref") + 10
    j = d.index(b"\n", i)
    return b"x" * 100 + d[:i] + str(int(d[i:j]) + 100).encode() + d[j:]


def test_junk_with_byte0_offsets():
    pf = PDFFile(byte0_junk_pdf(xref="stream", objstm=True))
    assert len(pf.pages()) == 1 and pf.recovered is False
    assert page_text(pf) == text_ops("Hi")
    pf = PDFFile(byte0_junk_pdf())
    assert len(pf.pages()) == 1 and pf.recovered is False


def test_root_not_a_catalog_is_damaged():
    objs = {1: {"Type": Name("Catalog"), "Pages": Ref(2)}, 2: {"Type": Name("Pages"), "Kids": []}}
    pdf = make_pdf(objs, root=2)
    pdf = pdf.replace(b"/Type /Catalog", b"/Type /Other  ")
    try:
        PDFFile(pdf)
    except ReadError as e:
        assert (e.kind, e.detail) == ("damaged", "no catalog")
    else:
        raise AssertionError("expected ReadError")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    for test in TESTS:
        test()
    print("pass")


if __name__ == "__main__":
    main()
