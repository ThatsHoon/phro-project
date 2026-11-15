"""PalmDB container of Kindle books: records, trailing entries, PalmDOC, HUFF/CDIC, caps.

Run: python -I -X utf8 tests/test_reader_mobi.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.containers import palmdb  # noqa: E402
from reader.containers.palmdb import HuffCdic, Palm, palmdoc, record_cap, text_cap, trailing  # noqa: E402
from reader.formats import mobi  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Image, Para  # noqa: E402


def _pdb(records, type_creator=b"BOOKMOBI"):
    """A Palm database holding the records; the first starts right after the record table."""
    n = len(records)
    head = bytearray(78)
    head[0:4] = b"Test"
    head[60:68] = type_creator
    head[76:78] = n.to_bytes(2, "big")
    off = 78 + 8 * n
    table = bytearray()
    for i, r in enumerate(records):
        table += off.to_bytes(4, "big") + bytes([0, 0, 0, i & 0xFF])
        off += len(r)
    return bytes(head) + bytes(table) + b"".join(records)


def _palmdoc_literal(data):
    """PalmDOC of literal codes only: plain bytes as they are, the rest in runs of up to 8 behind a count."""
    out = bytearray()
    run = bytearray()

    def flush():
        if run:
            out.append(len(run))
            out.extend(run)
            run.clear()

    for b in data:
        if b == 0 or 9 <= b <= 0x7F:
            flush()
            out.append(b)
        else:
            run.append(b)
            if len(run) == 8:
                flush()
    flush()
    return bytes(out)


def _huff_tables(phrases, wide=None):
    """A HUFF and one CDIC record with 8-bit codes: code i is phrases[i].

    wide (three literals) adds codes that begin with the byte 0xFF, which is then not settled: a 9-bit 0x1FF and the
    10-bit 0x3FC and 0x3FD, for wide[0], wide[1] and wide[2]. The 8-bit codes are then 0..254.

    A phrase is bytes (a literal) or (bytes, False), a packed phrase: a string of 8-bit codes.
    """
    huff = bytearray(24)
    huff[0:4] = b"HUFF"
    huff[4:8] = (24).to_bytes(4, "big")
    huff[8:12] = (24).to_bytes(4, "big")
    huff[12:16] = (24 + 1024).to_bytes(4, "big")
    # every first byte is a settled 8-bit code; the index is 255 - code
    entry = (255 << 8) | 0x80 | 8
    huff += entry.to_bytes(4, "big") * 256
    lengths = bytearray(32 * 8)
    if wide:
        huff[24 + 255 * 4:24 + 256 * 4] = ((0x1FF << 8) | 9).to_bytes(4, "big")   # 9 bits, not settled
        lengths[8 * 8:8 * 8 + 8] = (0x1FF).to_bytes(4, "big") + (0x1FF).to_bytes(4, "big")      # length 9
        lengths[9 * 8:9 * 8 + 8] = (0x3FC).to_bytes(4, "big") + (0x3FE).to_bytes(4, "big")      # length 10
    huff += lengths
    slots = [(b"", True)] * 256
    if wide:
        slots[0], slots[2], slots[1] = (wide[0], True), (wide[1], True), (wide[2], True)
    for i, p in enumerate(phrases):
        lit, flag = (p, True) if isinstance(p, bytes) else p
        slots[255 - i] = (lit, flag)
    offsets = bytearray()
    body = bytearray()
    for lit, flag in slots:
        offsets += (512 + len(body)).to_bytes(2, "big")
        body += (len(lit) | (0x8000 if flag else 0)).to_bytes(2, "big") + lit
    cdic = b"CDIC" + (16).to_bytes(4, "big") + (256).to_bytes(4, "big") + (8).to_bytes(4, "big") + bytes(offsets) + bytes(body)
    return bytes(huff), [cdic]


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


def test_palm_records():
    p = Palm(_pdb([b"a", b"bc", b""]))
    assert p.count == 3
    assert p.record(0) == b"a" and p.record(1) == b"bc" and p.record(2) == b""
    assert p.name == b"Test" and p.type_creator == b"BOOKMOBI"
    raises(IndexError, p.record, 3)
    raises(IndexError, p.record, -1)


def test_palm_offsets_backwards_damaged():
    d = bytearray(_pdb([b"a" * 30, b"b" * 30]))
    d[78:82] = (100).to_bytes(4, "big")
    d[86:90] = (90).to_bytes(4, "big")
    assert raises(ReadError, Palm, bytes(d)).kind == "damaged"
    d = bytearray(_pdb([b"a", b"b"]))
    d[86:90] = (10**6).to_bytes(4, "big")
    assert raises(ReadError, Palm, bytes(d)).kind == "damaged"
    assert raises(ReadError, Palm, b"x" * 77).kind == "damaged"
    d = bytearray(_pdb([b"a"]))
    d[76:78] = (0).to_bytes(2, "big")
    assert raises(ReadError, Palm, bytes(d)).kind == "damaged"       # no records
    d = bytearray(_pdb([b"a", b"b"]))
    d[78:82] = (80).to_bytes(4, "big")                                # inside the record table
    assert raises(ReadError, Palm, bytes(d)).kind == "damaged"
    d = bytearray(_pdb([b"a"]))
    d[76:78] = (500).to_bytes(2, "big")
    assert raises(ReadError, Palm, bytes(d)).kind == "damaged"


def test_trailing_flags_3():
    body = b"text"
    entry = b"ab\x83"        # backward varint 0x83: size 3 with its own byte
    tail = b"\x00\x01"       # last & 3 == 1: two bytes
    assert trailing(body + tail + entry, 3) == body
    assert trailing(body + entry, 2) == body
    assert trailing(body + b"\x01", 0) == body + b"\x01"
    assert trailing(b"ab", 2) == b""                          # an entry larger than the record


def test_trailing_stacked_entries_and_clamp():
    body = b"text"
    a = b"ab\x83"         # size 3
    b = b"x\x82"          # size 2
    tail = b"\x00\x01"     # multibyte tail of 2
    assert trailing(body + tail + b + a, 7) == body
    assert trailing(b"ab\x03", 1) == b""                  # a tail of 4 on a record of 3: clamped
    assert trailing(b"", 7) == b""


def test_palmdoc_paths():
    assert palmdoc(b"A\x03xyz\xc1", 100) == (b"Axyz A", False)
    v = (4 << 3) | 0
    assert palmdoc(b"abcd" + bytes([0x80 | v >> 8, v & 0xFF]), 100) == (b"abcdabc", False)
    v = (1 << 3) | 2
    assert palmdoc(b"a" + bytes([0x80 | v >> 8, v & 0xFF]), 100) == (b"aaaaaa", False)
    assert palmdoc(b"\x00a", 100) == (b"\x00a", False)


def test_palmdoc_bad_distance():
    raises(ValueError, palmdoc, b"abc" + bytes([0x80, 0x00 | 3]), 100)          # distance 0
    v = (9 << 3) | 0
    raises(ValueError, palmdoc, b"abc" + bytes([0x80 | v >> 8, v & 0xFF]), 100)  # past the output


def test_palmdoc_distance_off_by_one():
    v = (4 << 3) | 0
    raises(ValueError, palmdoc, b"abc" + bytes([0x80 | v >> 8, v & 0xFF]), 100)   # len(out) + 1
    v = (3 << 3) | 0
    assert palmdoc(b"abc" + bytes([0x80 | v >> 8, v & 0xFF]), 100) == (b"abcabc", False)  # len(out)


def test_palmdoc_limit_inside_copies():
    v = (4 << 3) | 7                                     # distance 4, run 10
    assert palmdoc(b"abcd" + bytes([0x80 | v >> 8, v & 0xFF]), 6) == (b"abcdab", True)
    assert palmdoc(b"\x08abcdefgh", 3) == (b"abc", True)
    assert palmdoc(b"\x08abcdefgh", 8) == (b"abcdefgh", False)


def test_palmdoc_limit():
    assert palmdoc(b"x" * 200, 50) == (b"x" * 50, True)
    assert palmdoc(b"x" * 50, 50) == (b"x" * 50, False)
    assert palmdoc(b"\xc1", 1) == (b" ", True)
    data = bytes(range(256)) * 3
    out, cut = palmdoc(_palmdoc_literal(data), 10**6)
    assert out == data and not cut


def test_huffcdic_terminal_codes():
    h = HuffCdic(*_huff_tables([b"he", b"llo"]))
    assert h.unpack(bytes([0, 1]), 100) == (b"hello", False)
    assert h.unpack(bytes([0, 1]), 4) == (b"hell", True)


def test_huffcdic_recursive_phrase():
    h = HuffCdic(*_huff_tables([b"he", b"llo", (bytes([0, 1]), False)]))
    assert h.unpack(bytes([2, 2]), 100) == (b"hellohello", False)


def test_huffcdic_self_reference_fails():
    h = HuffCdic(*_huff_tables([(bytes([0]), False)]))
    raises(ValueError, h.unpack, bytes([0]), 100)


def test_huffcdic_depth_33_fails():
    def chain(n):
        return HuffCdic(*_huff_tables([(bytes([k + 1]), False) for k in range(n)] + [b"x"]))
    assert chain(32).unpack(bytes([0]), 100) == (b"x", False)
    raises(ValueError, chain(33).unpack, bytes([0]), 100)


def test_huffcdic_bad_table_damaged():
    huff, cdics = _huff_tables([b"a"])
    assert raises(ReadError, HuffCdic, b"HUFX" + huff[4:], cdics).kind == "damaged"
    assert raises(ReadError, HuffCdic, huff[:100], cdics).kind == "damaged"
    bad = bytearray(huff)
    bad[24:28] = ((255 << 8) | 0x80).to_bytes(4, "big")                # code length 0
    assert raises(ReadError, HuffCdic, bytes(bad), cdics).kind == "damaged"
    bad[24:28] = ((255 << 8) | 8).to_bytes(4, "big")                   # 8 bits, not settled
    assert raises(ReadError, HuffCdic, bytes(bad), cdics).kind == "damaged"
    assert raises(ReadError, HuffCdic, huff, [b"CDIX" + cdics[0][4:]]).kind == "damaged"
    assert raises(ReadError, HuffCdic, huff, [cdics[0][:100]]).kind == "damaged"
    over = bytearray(cdics[0])
    over[12:16] = (17).to_bytes(4, "big")
    assert raises(ReadError, HuffCdic, huff, [bytes(over)]).kind == "damaged"


def test_huffcdic_dictionary_and_phrase_caps():
    huff, cdics = _huff_tables([b"a"])
    # 256 phrases: 64 bytes charged each is 16385, 32 each would be 8193
    old = palmdb.HUFF_DICT
    palmdb.HUFF_DICT = 12000
    try:
        raises(ValueError, HuffCdic, huff, cdics)
        palmdb.HUFF_DICT = 16385
        HuffCdic(huff, cdics)
        palmdb.HUFF_DICT = 16384
        raises(ValueError, HuffCdic, huff, cdics)
    finally:
        palmdb.HUFF_DICT = old
    h = HuffCdic(*_huff_tables([b"abcdef", (bytes([0, 0]), False)]))
    old = palmdb.HUFF_PHRASE
    palmdb.HUFF_PHRASE = 10
    try:
        raises(ValueError, h.unpack, bytes([1]), 100)
    finally:
        palmdb.HUFF_PHRASE = old


def test_huffcdic_unsettled_long_codes():
    h = HuffCdic(*_huff_tables([b"a"], wide=[b"N", b"T", b"E"]))
    bits = "".join(format(c, f"0{n}b") for c, n in ((0x3FC, 10), (0x1FF, 9), (0x3FD, 10), (0, 8)))
    bits += "0" * (-len(bits) % 8)
    rec = int(bits, 2).to_bytes(len(bits) // 8, "big")
    assert h.unpack(rec, 100) == (b"TNEa", False)


def test_caps():
    assert record_cap(0) == 4104 and record_cap(100) == 108
    assert text_cap(10) == 320
    old = palmdb.MOBI_TEXT
    palmdb.MOBI_TEXT = 100
    try:
        assert text_cap(10**6) == 100
    finally:
        palmdb.MOBI_TEXT = old
    assert text_cap(10**9) == 64 * 2**20


# ---- the MOBI 6 reader
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


def _rec0(*, version=6, compression=1, text_length=0, text_records=1, encryption=0, encoding=65001,
          first_image=0xFFFFFFFF, exth=None, title=b"T", record_size=4096, drm_offset=0xFFFFFFFF, drm_count=0,
          huff_record=0, huff_count=0, extra_flags=0, **indexes):
    """Record 0: the PalmDOC head, a 264-byte MOBI head (fields at the offsets Tika's MobiHeader reads), EXTH, title.

    indexes: fdst, ncx, frag, skel (default 0xFFFFFFFF)."""
    r = bytearray(280)
    r[0:2] = compression.to_bytes(2, "big")
    r[4:8] = text_length.to_bytes(4, "big")
    r[8:10] = text_records.to_bytes(2, "big")
    r[10:12] = record_size.to_bytes(2, "big")
    r[12:14] = encryption.to_bytes(2, "big")
    r[16:20] = b"MOBI"
    r[20:24] = (264).to_bytes(4, "big")
    r[28:32] = encoding.to_bytes(4, "big")
    r[36:40] = version.to_bytes(4, "big")
    r[108:112] = first_image.to_bytes(4, "big")
    r[112:116] = huff_record.to_bytes(4, "big")
    r[116:120] = huff_count.to_bytes(4, "big")
    r[168:172] = drm_offset.to_bytes(4, "big")
    r[172:176] = drm_count.to_bytes(4, "big")
    for name, at in (("fdst", 192), ("ncx", 244), ("frag", 248), ("skel", 252)):
        r[at:at + 4] = indexes.pop(name, 0xFFFFFFFF).to_bytes(4, "big")
    assert not indexes, indexes
    r[240:244] = extra_flags.to_bytes(4, "big")
    if exth:
        items = [(k, v) for k, vs in exth.items() for v in (vs if isinstance(vs, list) else [vs])]
        recs = b"".join(k.to_bytes(4, "big") + (8 + len(v)).to_bytes(4, "big") + v for k, v in items)
        body = b"EXTH" + (12 + len(recs)).to_bytes(4, "big") + len(items).to_bytes(4, "big") + recs
        r += body + bytes(-len(body) % 4)
        r[128:132] = (0x40).to_bytes(4, "big")
    r[84:88] = len(r).to_bytes(4, "big")
    r[88:92] = len(title).to_bytes(4, "big")
    return bytes(r) + title


def _mobi(text, *, compression=1, extra=(), pack=None, **kw):
    """A BOOKMOBI file: record 0, the text in 4096-byte records (compressed with pack), then extra records."""
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)] or [b""]
    if pack:
        chunks = [pack(c) for c in chunks]
    rec0 = _rec0(compression=compression, text_length=len(text), text_records=len(chunks), **kw)
    return _pdb([rec0] + chunks + list(extra))


def _read(data):
    ctx = Context()
    return mobi.parse_bytes(data, ctx), ctx


def _flat(o):
    """Every string inside a document object, in order."""
    out = []
    if isinstance(o, str):
        out.append(o)
    elif isinstance(o, (list, tuple)):
        for x in o:
            out += _flat(x)
    elif hasattr(o, "__dataclass_fields__"):
        for f in o.__dataclass_fields__:
            out += _flat(getattr(o, f))
    return out


def _text_of(o):
    return "".join(_flat(o))


BOOK = (b"<html><head></head><body><h1>One</h1><p>first para</p><mbp:pagebreak/>"
        b"<p>second para</p><p>more</p></body></html>")


def test_mobi6_book():
    doc, ctx = _read(_mobi(BOOK, exth={503: b"Book"}, title=b"Other"))
    assert doc.title == "Book"
    assert [s.kind for s in doc.blocks] == ["chapter", "chapter"] and [s.number for s in doc.blocks] == [1, 2]
    assert "first para" in _text_of(doc.blocks[0]) and "second para" in _text_of(doc.blocks[1])
    assert "second" not in _text_of(doc.blocks[0])
    assert not ctx.skipped


def test_full_title_without_exth():
    doc, _ = _read(_mobi(BOOK, title="가나 title".encode()))
    assert doc.title == "가나 title"
    doc, _ = _read(_mobi(BOOK, title=b"x" * 1000))
    assert doc.title == "x" * mobi.MAX_TITLE


def test_cp1252_text():
    doc, _ = _read(_mobi(b"<html><body><p>\x93q\x94</p></body></html>", encoding=1252))
    assert "“q”" in _text_of(doc.blocks)
    # a <meta charset> cannot override the book's own encoding
    doc, _ = _read(_mobi(b'<html><head><meta charset="utf-16"></head><body><p>\x93q\x94</p></body></html>', encoding=1252))
    assert "“q”" in _text_of(doc.blocks)


def test_recindex_image():
    body = b'<html><body><p>x</p><img recindex="00001"><img recindex="00002"></body></html>'
    data = _mobi(body, first_image=3, extra=[b"filler", PNG, b"not a picture"])
    doc, ctx = _read(data)
    imgs = [b for s in doc.blocks for b in s.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data == PNG
    assert ctx.skipped["image"] == 1


def test_kindle_embed_loader():
    palm = Palm(_mobi(b"<p>x</p>", first_image=2, extra=[PNG, b"nope"]))
    head = mobi._head(palm.record(0))
    load = mobi._loader(palm, head, Context())
    assert load("kindle:embed:0001?mime=image/png") == PNG
    assert load("kindle:embed:0002") is None and load("recindex:0") is None and load("recindex:99") is None
    assert load("kindle:embed:ZZZ") is None and load("http://x/y.png") is None


def test_image_bytes_cap():
    from reader import limits
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(PNG) * 2 - 1
    try:
        body = b'<html><body><img recindex="1"><img recindex="1"></body></html>'
        doc, ctx = _read(_mobi(body, first_image=2, extra=[PNG]))
        assert sum(isinstance(b, Image) for s in doc.blocks for b in s.blocks) == 1 and ctx.skipped["image"] == 1
    finally:
        limits.IMAGE_BYTES = old


def test_filepos_inside_tag():
    text = b'<html><body><p>a <a filepos=0000000000>go</a></p><p class="x">target</p></body></html>'
    pos = text.index(b"class")
    text = text.replace(b"0000000000", f"{pos:010d}".encode())
    assert text.index(b"class") == pos
    out = mobi._anchor(text, {pos: "fp1"})
    assert b'<a id="fp1"></a><p class="x">' in out
    doc, _ = _read(_mobi(text))
    assert "class=" not in _text_of(doc.blocks) and "target" in _text_of(doc.blocks)


def test_anchor_edges():
    t = b"<p>ab</p>"
    assert mobi._anchor(t, {len(t) + 1: "z"}) == t                            # past the end: dropped
    assert mobi._anchor(t, {3: "a"}) == b'<p><a id="a"></a>ab</p>'
    assert mobi._anchor(t, {1: "a", 2: "b"}) == b'<a id="a"></a><a id="b"></a><p>ab</p>'   # both moved before the tag
    old = mobi.MOBI_ANCHORS
    mobi.MOBI_ANCHORS = 1
    try:
        assert mobi._anchor(t, {3: "a", 4: "b"}).count(b"<a id=") == 1
    finally:
        mobi.MOBI_ANCHORS = old


def test_anchor_cap_counted():
    old = mobi.MOBI_ANCHORS
    mobi.MOBI_ANCHORS = 1
    seen = []
    real = mobi.xhtml.tree
    mobi.xhtml.tree = lambda data: (seen.append(data), real(data))[1]
    try:
        body = b"<html><body><p><a filepos=0000000010>aaa</a> and <a filepos=0000000020>bbb</a></p></body></html>"
        doc, ctx = _read(_mobi(body))
    finally:
        mobi.MOBI_ANCHORS = old
        mobi.xhtml.tree = real
    assert ctx.skipped["link"] == 1
    para = doc.blocks[0].blocks[0]
    assert isinstance(para, Para) and _text_of(para) == "aaa and bbb"
    raw = b"".join(seen)
    assert b'href="#fp10"' in raw and b"fp20" not in raw and b"filepos" not in raw


def test_exth_hostile():
    base = bytearray(_rec0(exth={100: b"x"}))
    base[288:292] = (1000).to_bytes(4, "big")                       # a count larger than the data
    h = mobi._head(bytes(base))
    assert h.exth == {100: [b"x"]}
    base = bytearray(_rec0(exth={100: b"x"}))
    base[296:300] = (4).to_bytes(4, "big")                          # a size below 8
    assert mobi._head(bytes(base)).exth == {}
    h = mobi._head(_rec0(exth={100: [b"v"] * 70}))
    assert len(h.exth[100]) == mobi.MAX_EXTH_VALUES
    doc, _ = _read(_mobi(BOOK, exth={100: [b"v"] * 70}))
    assert len(doc.blocks) == 2


def test_title_fallbacks():
    doc, _ = _read(_mobi(BOOK, exth={503: b"  "}, title=b"Full"))
    assert doc.title == "Full"
    doc, _ = _read(_mobi(BOOK, exth={503: b""}, title=b"Full"))
    assert doc.title == "Full"
    d = bytearray(_mobi(BOOK, title=b"Full"))
    rec0 = bytearray(_rec0(title=b"Full"))
    rec0[84:88] = (10**6).to_bytes(4, "big")                         # a title offset past the record
    doc, _ = _read(_pdb([bytes(rec0), BOOK]))
    assert doc.title == ""
    doc, _ = _read(_mobi(BOOK, exth={503: b"y" * 5000}))
    assert doc.title == "y" * mobi.MAX_TITLE


def test_text_records_past_the_file():
    text = b"<html><body><p>before</p></body></html>"
    rec0 = _rec0(text_records=5, text_length=len(text))
    doc, ctx = _read(_pdb([rec0, text]))
    assert ctx.skipped["record"] == 1 and "before" in _text_of(doc.blocks)


def test_stripped_script_offsets_in_the_text_itself():
    out = mobi._stripped("<p>İİİİ a</p><script>x()</script><p>after</p>".encode(), Context())
    assert [_text_of(p) for p in out] == ["İİİİ a", "after"]
    out = mobi._stripped(b"<p>a</p><script>x</scripts>y</script ><p>b</p>", Context())
    assert [_text_of(p) for p in out] == ["a", "b"]
    out = mobi._stripped(b"<p>a</p><style>x</ style><p>b</p>", Context())
    assert [_text_of(p) for p in out] == ["a", "b"]


def test_regexes_stay_linear():
    import time
    for unit in (b"<mbp:pagebreak ", b"<p", b"<script ", b"<a filepos=1 ", b"<img recindex=1 "):
        data = unit * (300_000 // len(unit))
        t = time.perf_counter()
        try:
            mobi.parse_bytes(_mobi(data), Context())
        except ReadError:
            pass
        mobi._stripped(data, Context())
        assert time.perf_counter() - t < 2, unit


def test_filepos_mid_utf8():
    text = "<html><body><p>가나다</p></body></html>".encode()
    pos = text.index("가".encode()) + 1
    out = mobi._anchor(text, {pos: "fp"})
    assert b'<a id="fp"></a>' + "가나다".encode() in out
    body = "<html><body><p><a filepos=0000000000>go</a></p><p>가나다</p></body></html>".encode()
    pos = body.index("가".encode()) + 1
    body = body.replace(b"0000000000", f"{pos:010d}".encode())
    assert body.index("가".encode()) + 1 == pos
    doc, _ = _read(_mobi(body))
    assert "가나다" in _text_of(doc.blocks) and "�" not in _text_of(doc.blocks)


def test_huffcdic_book():
    huff, cdics = _huff_tables([b"<html><body>", b"<p>he", b"llo</p>", b"</body></html>"])
    rec0 = _rec0(compression=17480, text_length=40, text_records=1, huff_record=2, huff_count=2)
    doc, ctx = _read(_pdb([rec0, bytes([0, 1, 2, 3]), huff] + cdics))
    assert "hello" in _text_of(doc.blocks) and not ctx.skipped
    rec0 = _rec0(compression=17480, text_length=40, text_records=1, huff_record=2, huff_count=9)   # outside the file
    assert raises(ReadError, mobi.parse_bytes, _pdb([rec0, bytes([0]), huff] + cdics), Context()).kind == "damaged"


def test_palmdoc_book():
    doc, ctx = _read(_mobi(BOOK, compression=2, pack=_palmdoc_literal))
    assert "first para" in _text_of(doc.blocks) and "second para" in _text_of(doc.blocks) and not ctx.skipped


def test_trailing_entries_book():
    text = b"<html><body><p>hello</p></body></html>"
    rec0 = _rec0(version=6, text_length=len(text), text_records=1, extra_flags=2)
    doc, _ = _read(_pdb([rec0, text + b"ab\x83"]))
    assert "hello" in _text_of(doc.blocks) and "ab" not in _text_of(doc.blocks)


def test_record_cap_counts_record():
    text = b"<html><body><p>" + b"a" * 190 + b"</p></body></html>"
    rec0 = _rec0(compression=2, text_length=len(text), text_records=1, record_size=100)
    doc, ctx = _read(_pdb([rec0, _palmdoc_literal(text)]))
    assert ctx.skipped["record"] == 1 and "a" * 50 in _text_of(doc.blocks)
    bad = _rec0(compression=2, text_length=10, text_records=2)             # a failed record, a good one
    good = b"<html><body><p>kept</p></body></html>"
    doc, ctx = _read(_pdb([bad, b"abc" + bytes([0x80, 0x03]), _palmdoc_literal(good)]))
    assert ctx.skipped["record"] == 1 and "kept" in _text_of(doc.blocks)


def test_text_cap_truncated():
    old = palmdb.MOBI_TEXT
    palmdb.MOBI_TEXT = 100
    try:
        text = b"<html><body>" + b"<p>line</p>" * 30 + b"</body></html>"
        doc, ctx = _read(_mobi(text))
        assert ctx.skipped["truncated"] == 1
        assert _text_of(doc.blocks).count("line") < 30
    finally:
        palmdb.MOBI_TEXT = old


def test_stripped_text():
    ctx = Context()
    out = mobi._stripped(b"<p>a</p><div>b &amp; c</div>", ctx)
    assert [_text_of(p) for p in out] == ["a", "b & c"] and all(isinstance(p, Para) for p in out)
    assert ctx.skipped["plain_text"] == 1
    out = mobi._stripped(b"<style>p{}</style><p>x</p><script>y</script>", Context())
    assert [_text_of(p) for p in out] == ["x"]


def test_kf8_only_is_stripped_for_now():
    doc, ctx = _read(_mobi(b"<html><body><p>one</p><p>two</p></body></html>", version=8))
    assert [_text_of(b) for b in doc.blocks[0].blocks] == ["one", "two"] and ctx.skipped["plain_text"] == 1
    doc, ctx = _read(_mobi(BOOK, exth={121: (9).to_bytes(4, "big")}))      # a combo reads as MOBI 6
    assert len(doc.blocks) == 2 and not ctx.skipped


def test_head_fields():
    h = mobi._head(_rec0(version=8, compression=17480, text_records=3, encoding=1252, first_image=5,
                         huff_record=7, huff_count=4, ncx=11, frag=12, skel=13, fdst=14, extra_flags=3,
                         exth={100: [b"a", b"b"], 503: b"t"}, title=b"Full"))
    assert (h.version, h.compression, h.text_records, h.encoding, h.first_image) == (8, 17480, 3, 1252, 5)
    assert (h.huff_record, h.huff_count, h.ncx, h.frag, h.skel, h.fdst, h.extra_flags) == (7, 4, 11, 12, 13, 14, 3)
    assert h.exth == {100: [b"a", b"b"], 503: [b"t"]} and h.full_title == b"Full"
    assert h.drm_offset == 0xFFFFFFFF and h.drm_count == 0 and h.record_size == 4096
    assert mobi._head(_rec0(version=4, extra_flags=3)).extra_flags == 0         # flags are read from version 5
    r = bytearray(_rec0())
    r[20:24] = (4000).to_bytes(4, "big")
    assert raises(ReadError, mobi._head, bytes(r)).kind == "damaged"
    assert raises(ReadError, mobi._head, b"x" * 30).kind == "damaged"
    r = bytearray(_rec0())
    r[16:20] = b"XXXX"
    assert raises(ReadError, mobi._head, bytes(r)).kind == "damaged"


def test_refusals():
    def kind(data):
        return raises(ReadError, mobi.parse_bytes, data, Context()).kind
    ok = b"<html><body><p>x</p></body></html>"
    assert kind(_mobi(ok, encryption=2)) == "drm"
    assert kind(_mobi(ok, encryption=1)) == "drm"
    assert kind(_mobi(ok, drm_offset=0x100, drm_count=1)) == "drm"
    assert kind(_mobi(ok, compression=3)) == "unsupported"
    assert kind(_mobi(ok, encoding=1200)) == "unsupported"
    assert kind(b"TPZ0" + bytes(200)) == "unsupported"
    assert kind(b"CONT" + bytes(200)) == "unsupported"
    assert kind(_pdb([_rec0(), b"x"], b"TEXtREAd")) == "unsupported"
    assert kind(_pdb([_rec0(), b"x"], b"ABCDEFGH")) == "unsupported"
    e = raises(ReadError, mobi.parse_bytes, b"PK\x03\x04" + bytes(100), Context())
    assert e.kind == "unsupported" and "KFX or another zip container" in str(e)
    d = bytearray(_mobi(ok))
    d[78:82], d[86:90] = (200).to_bytes(4, "big"), (100).to_bytes(4, "big")
    assert kind(bytes(d)) == "damaged"
    assert kind(_mobi(b"<html><body></body></html>")) == "damaged"           # no readable chapter
    assert kind(_mobi(b"")) == "damaged"
    assert kind(_pdb([b"short"])) == "damaged"                                 # no MOBI head


def test_parse_path():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "b.mobi"
        p.write_bytes(_mobi(BOOK))
        assert len(mobi.parse(p, Context()).blocks) == 2


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
