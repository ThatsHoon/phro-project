"""PalmDB container of Kindle books: records, trailing entries, PalmDOC, HUFF/CDIC, caps.

Run: python -I -X utf8 tests/test_reader_mobi.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import html as xhtml  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.containers import palmdb  # noqa: E402
from reader.containers.palmdb import HuffCdic, Palm, palmdoc, record_cap, text_cap, trailing  # noqa: E402
from reader.formats import mobi  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Heading, Image, Para  # noqa: E402


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


def palmdoc_by_byte(rec, limit):
    """PalmDOC LZ77 one byte at a time, as the format describes it: what `palmdoc` must give."""
    out, i, end = bytearray(), 0, len(rec)
    while i < end:
        if len(out) >= limit:
            return bytes(out), True
        c = rec[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7F:
            out.append(c)
        elif c <= 8:
            n = min(c, end - i)
            take = min(n, limit - len(out))
            out += rec[i:i + take]
            i += c
            if take < n:
                return bytes(out), True
        elif c >= 0xC0:
            out.append(0x20)
            if len(out) >= limit:
                return bytes(out), True
            out.append(c ^ 0x80)
        else:
            if i >= end:
                break
            pair = (c << 8) | rec[i]
            i += 1
            dist, run = (pair >> 3) & 0x7FF, (pair & 7) + 3
            if dist == 0 or dist > len(out):
                raise ValueError("distance")
            for _ in range(run):
                if len(out) >= limit:
                    return bytes(out), True
                out.append(out[-dist])
    return bytes(out), False


def test_palmdoc_matches_the_byte_by_byte_decoder():
    import random
    rnd = random.Random(11)
    seen = set()
    for k in range(4000):
        n = rnd.randrange(0, 60)
        kind = k % 4
        rec = bytes(rnd.choice((rnd.randrange(256), rnd.randrange(9, 0x80), rnd.randrange(0x80, 0xC0), 1, 8))
                    if kind else rnd.randrange(9, 0x80) for _ in range(n))
        limit = rnd.choice((0, 1, 2, 7, 30, 200))
        try:
            want = palmdoc_by_byte(rec, limit)
        except ValueError:
            raises(ValueError, palmdoc, rec, limit)
            seen.add("error")
            continue
        assert palmdoc(rec, limit) == want, (rec, limit, palmdoc(rec, limit), want)
        seen.add(want[1])
    assert seen == {"error", True, False}, seen


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
    for src in ("kindle:embed:0_01", "kindle:embed:+1", "kindle:embed: 1", "kindle:embed:"):
        assert load(src) is None, src


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


def test_kf8_without_fdst_or_skeleton():
    # no FDST: the whole text is flow 0; no SKEL: it is read as XHTML, split at <html
    doc, ctx = _read(_mobi(b"<html><body><p>one</p><p>two</p></body></html>", version=8))
    assert [_text_of(b) for b in doc.blocks[0].blocks] == ["one", "two"] and not ctx.skipped
    doc, ctx = _read(_mobi(BOOK, exth={121: (9).to_bytes(4, "big")}))      # EXTH 121 past the file: MOBI 6
    assert len(doc.blocks) == 2 and not ctx.skipped


# ---- KF8
OLD = b"<html><body><p>old copy</p></body></html>"


def _u32(v):
    return v.to_bytes(4, "big")


def _vwi(v):
    """A forward varint: 7-bit groups, the high bit set on the last byte."""
    out = [v & 0x7F | 0x80]
    v >>= 7
    while v:
        out.append(v & 0x7F)
        v >>= 7
    return bytes(reversed(out))


def _indx_records(tagx, entries, cncx=b""):
    """An index: the INDX record with its TAGX, one INDX record of entries, then the CNCX record when given.

    tagx: [(tag, values per group, mask, end flag)] over one control byte. entries: [(label, {tag: values})]; a tag
    with k groups of values puts k in its mask bits."""
    body, offsets = bytearray(), []
    for label, tags in entries:
        label = label.encode() if isinstance(label, str) else label
        ctrl, vals = 0, b""
        for tag, nv, mask, end in tagx:
            if end or tag not in tags:
                continue
            ctrl |= len(tags[tag]) // max(nv, 1) << ((mask & -mask).bit_length() - 1)
            vals += b"".join(_vwi(x) for x in tags[tag])
        offsets.append(56 + len(body))
        body += bytes([len(label)]) + label + bytes([ctrl & 0xFF]) + vals
    rec = bytearray(56)
    rec[0:4], rec[4:8], rec[20:24], rec[24:28] = b"INDX", _u32(56), _u32(56 + len(body)), _u32(len(entries))
    rec += body + b"IDXT" + b"".join(o.to_bytes(2, "big") for o in offsets)
    defs = b"".join(bytes(t) for t in tagx)
    main = bytearray(56)
    main[0:4], main[4:8], main[24:28], main[28:32] = b"INDX", _u32(56), _u32(1), _u32(65001)
    main[36:40], main[52:56] = _u32(len(entries)), _u32(1 if cncx else 0)
    main += b"TAGX" + _u32(12 + len(defs)) + _u32(1) + defs
    return [bytes(main), bytes(rec)] + ([cncx] if cncx else [])


def _fdst(flows):
    return b"FDST" + _u32(12) + _u32(len(flows)) + b"".join(_u32(s) + _u32(e) for s, e in flows)


# kindlegen writes SKEL values twice: tag 1 [n, n], tag 6 [start, length, start, length]
SKEL_TAGX = [(1, 1, 0x03, 0), (6, 2, 0x0C, 0), (0, 0, 0, 1)]
FRAG_TAGX = [(2, 1, 0x01, 0), (3, 1, 0x02, 0), (4, 1, 0x04, 0), (6, 2, 0x08, 0), (0, 0, 0, 1)]


def _kf8_text(files):
    """flow 0, SKEL rows (count, start, length) and FRAG rows (insert, file, seq, offset, length) of files given as
    (skeleton, [(at, fragment)]), each `at` counted in the file as assembled so far."""
    flow, skels, frags = b"", [], []
    for n, (skel, fs) in enumerate(files):
        start, off = len(flow), 0
        for at, f in fs:
            frags.append((start + at, n, len(frags), off, len(f)))
            off += len(f)
        skels.append((len(fs), start, len(skel)))
        flow += skel + b"".join(f for _, f in fs)
    return flow, skels, frags


def _kf8_recs(files=None, flow0=None, sheets=(), images=(), *, skel_tagx=SKEL_TAGX, **kw):
    """KF8 records from its record 0: record 0, text, FDST, the SKEL and FRAG indexes (when files are given), images.
    The indexes and the first image are relative to record 0."""
    if files is not None:
        flow0, skels, frags = _kf8_text(files)
    text, flows = flow0, [(0, len(flow0))]
    for s in sheets:
        flows.append((len(text), len(text) + len(s)))
        text += s
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)] or [b""]
    tail = [_fdst(flows)]
    idx = {"fdst": 1 + len(chunks)}
    if files is not None:
        idx["skel"] = 1 + len(chunks) + len(tail)
        tail += _indx_records(skel_tagx, [(f"SKEL{i:010d}", {1: [c, c], 6: [s, ln, s, ln]})
                                          for i, (c, s, ln) in enumerate(skels)])
        idx["frag"] = 1 + len(chunks) + len(tail)
        tail += _indx_records(FRAG_TAGX, [(str(ins), {2: [0], 3: [f], 4: [q], 6: [o, ln]})
                                          for ins, f, q, o, ln in frags], cncx=_vwi(3) + b"P-1" + bytes(4))
    if images:
        idx["first_image"] = 1 + len(chunks) + len(tail)
    idx.update(kw)
    return [_rec0(version=8, text_length=len(text), text_records=len(chunks), **idx)] + chunks + tail + list(images)


def _combo(m6_text, kf8, shared=(), **kw):
    """A combo book: MOBI 6 record 0 and text, shared records, a BOUN record, then the KF8 records. EXTH 121 names
    KF8 record 0."""
    chunks = [m6_text[i:i + 4096] for i in range(0, len(m6_text), 4096)] or [b""]
    exth = {121: _u32(2 + len(chunks) + len(shared))}
    exth.update(kw.pop("exth", {}))
    rec0 = _rec0(version=6, text_length=len(m6_text), text_records=len(chunks), exth=exth, **kw)
    return _pdb([rec0] + chunks + list(shared) + [b"BOUN" + bytes(4)] + kf8)


def _two_files(out_of_order=False):
    s1 = b"<html><head></head><body><div></div><div></div></body></html>"
    a, b = b"<p>alpha</p>", b"<p>beta</p>"
    at_a = s1.index(b"</div>")
    built = s1[:at_a] + a + s1[at_a:]
    at_b = built.index(b"<p>alpha") if out_of_order else built.index(b"</div></body>")
    s2 = b"<html><head></head><body></body></html>"
    c = b"<p>gamma</p>"
    files = [(s1, [(at_a, a), (at_b, b)]), (s2, [(s2.index(b"</body>"), c)])]
    return files, [built[:at_b] + b + built[at_b:], s2.replace(b"</body>", c + b"</body>")]


def _seen_trees(data):
    """The document and context of a book, and the bytes every part handed to html.tree."""
    seen = []
    real = mobi.xhtml.tree
    mobi.xhtml.tree = lambda b: (seen.append(b), real(b))[1]
    try:
        doc, ctx = _read(data)
    finally:
        mobi.xhtml.tree = real
    return doc, ctx, b"".join(seen)


def _md(doc, ctx, hidden=False):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)


def _broken(recs, field):
    """recs with the record a record-0 field names given a bad magic."""
    at = getattr(mobi._head(recs[0]), field)
    recs = list(recs)
    recs[at] = b"XXXX" + recs[at][4:]
    return recs


def test_kf8_two_skeletons_three_fragments():
    for order in (False, True):
        files, want = _two_files(order)
        data = _pdb(_kf8_recs(files))
        palm = Palm(data)
        parts, flows = mobi._kf8_parts(palm, mobi._head(palm.record(0)), 0, Context())
        assert parts == want and flows[0] == _kf8_text(files)[0]
        doc, ctx = _read(data)
        assert [s.kind for s in doc.blocks] == ["chapter", "chapter"] and [s.number for s in doc.blocks] == [1, 2]
        t1, t2 = _text_of(doc.blocks[0]), _text_of(doc.blocks[1])
        assert (t1.index("alpha") < t1.index("beta")) != order and "gamma" in t2 and "gamma" not in t1
        assert not ctx.skipped


def test_fragment_work_cap():
    files, _ = _two_files(True)
    old = mobi.FRAG_WORK
    mobi.FRAG_WORK = 10
    try:
        doc, ctx = _read(_pdb(_kf8_recs(files)))
    finally:
        mobi.FRAG_WORK = old
    assert ctx.skipped == {"plain_text": 1} and "gamma" in _text_of(doc.blocks)
    # fragments outside their skeleton, or past it
    files, _ = _two_files()
    for bad in ((10 ** 6, b"<p>x</p>"),):
        doc, ctx = _read(_pdb(_kf8_recs([(files[0][0], files[0][1] + [bad])] + files[1:])))
        assert ctx.skipped == {"plain_text": 1}
    # an insert before the second skeleton's start (insert < start): falls back, never placed
    doc, ctx = _read(_pdb(_kf8_recs(files[:1] + [(files[1][0], [(-5, b"<p>x</p>")])])))
    assert ctx.skipped == {"plain_text": 1} and "gamma" not in _text_of(doc.blocks)


def test_kf8_no_skeleton_two_html():
    flow0 = b'<?xml version="1.0"?>' + b"<html><body><p>one</p></body></html><html><body><p>two</p></body></html>"
    doc, ctx = _read(_pdb(_kf8_recs(flow0=flow0)))
    assert len(doc.blocks) == 2 and _text_of(doc.blocks[0].blocks) == "one" and _text_of(doc.blocks[1].blocks) == "two"
    assert not ctx.skipped


def test_combo_reads_kf8():
    files, _ = _two_files()
    doc, ctx = _read(_combo(OLD, _kf8_recs(files)))
    text = _text_of(doc.blocks)
    assert "alpha" in text and "gamma" in text and "old copy" not in text and not ctx.skipped
    # EXTH 121 on a record without BOUN before it, or on one without a KF8 head: MOBI 6, nothing counted
    for at in (1, 2):
        doc, ctx = _read(_combo(OLD, _kf8_recs(files), exth={121: _u32(at)}))
        assert _text_of([c.blocks for c in doc.blocks]) == "old copy" and not ctx.skipped


def test_kf8_failure_falls_to_mobi6():
    files, _ = _two_files()
    for field in ("skel", "frag", "fdst"):
        doc, ctx = _read(_combo(OLD, _broken(_kf8_recs(files), field)))
        assert _text_of([c.blocks for c in doc.blocks]) == "old copy"
        assert ctx.skipped == {"kf8_fallback": 1}, ctx.skipped
        assert "KF8 layout (the MOBI 6 copy was read)" in markdown.unread(ctx.skipped)


def test_kf8_only_failure_stripped():
    files, _ = _two_files()
    doc, ctx = _read(_pdb(_broken(_kf8_recs(files, sheets=[b".x{color:red}"]), "skel")))
    assert "book markup (read as plain text)" in markdown.unread(ctx.skipped) and "kf8_fallback" not in ctx.skipped
    assert [_text_of(p) for p in doc.blocks[0].blocks] == ["alpha", "beta", "gamma"]    # flow 0, raw order
    # an unreadable FDST: the whole text
    doc, ctx = _read(_pdb(_broken(_kf8_recs(files, sheets=[b".x{color:red}"]), "fdst")))
    assert [_text_of(p) for p in doc.blocks[0].blocks] == ["alpha", "beta", "gamma", ".x{color:red}"]
    assert ctx.skipped == {"plain_text": 1}


def test_kindle_flow_css_display_none():
    link = b'<link rel="stylesheet" type="text/css" href="kindle:flow:0001?mime=text/css"/>'
    s = b"<html><head>" + link + b"</head><body></body></html>"
    frag = b'<p class="h">secret</p><p>shown</p>'
    doc, ctx = _read(_pdb(_kf8_recs([(s, [(s.index(b"</body>"), frag)])], sheets=[b".h{display:none}"])))
    out, shown = _md(doc, ctx), _md(doc, ctx, hidden=True)
    assert "secret" not in out and "shown" in out and "secret" in shown and "shown" in shown
    # a flow number past the FDST loads nothing
    s2 = s.replace(b"0001", b"0009")
    doc, ctx = _read(_pdb(_kf8_recs([(s2, [(s2.index(b"</body>"), frag)])], sheets=[b".h{display:none}"])))
    assert "secret" in _md(doc, ctx)


def test_hidden_attribute():
    doc, ctx = _read(_pdb(_kf8_recs(flow0=b"<html><body><p hidden>gone</p><p>kept</p></body></html>")))
    out = _md(doc, ctx)
    assert "gone" not in out and "kept" in out and "gone" in _md(doc, ctx, hidden=True)


def test_kindle_embed_image():
    flow0 = b'<html><body><p>x</p><img src="kindle:embed:0002?mime=image/png"/></body></html>'
    doc, ctx = _read(_pdb(_kf8_recs(flow0=flow0, images=[b"not a picture", PNG])))
    imgs = [b for s in doc.blocks for b in s.blocks if isinstance(b, Image)]
    assert [i.data for i in imgs] == [PNG] and not ctx.skipped


def _pos_files(fid=b"0000", off=b"0000000009", tail=b"?mime=text/html", quote=b'"'):
    s = b"<html><head></head><body><div></div></body></html>"
    a = b"<p>alpha target</p>"
    link = b"<p><a href=" + quote + b"kindle:pos:fid:" + fid + b":off:" + off + tail + quote + b' class="c">go</a></p>'
    at_a = s.index(b"</div>")
    built = s[:at_a] + a + s[at_a:]
    return [(s, [(at_a, a), (built.index(b"<p>alpha"), link)])]   # the link goes in before alpha and moves it


def test_kindle_pos_link():
    import re
    doc, ctx, raw = _seen_trees(_pdb(_kf8_recs(_pos_files())))
    m = re.search(rb'<a id="(kp\d+)"></a>target', raw)
    assert m and b'href="#' + m.group(1) + b'"' in raw and b"kindle:pos" not in raw
    assert "go" in _text_of(doc.blocks) and "alpha target" in _text_of(doc.blocks) and not ctx.skipped
    # a quoted tail past 200 characters, and an unquoted href: the whole attribute is rewritten, the rest kept
    for tail, quote in ((b"?mime=text/html&" + b"x" * 300, b'"'), (b"", b"")):
        doc, ctx, raw = _seen_trees(_pdb(_kf8_recs(_pos_files(tail=tail, quote=quote))))
        m = re.search(rb'<a id="(kp\d+)"></a>target', raw)
        assert m and b'<a href="#' + m.group(1) + b'" class="c">go' in raw and b"xxx" not in raw, raw
    # links across files: kp<skeleton start + position in the part>
    files, _ = _two_files()
    (s1, ((at_a, a), (at_b, b))), (s2, ((at, c),)) = files
    to_gamma = b'<p><a href="kindle:pos:fid:0002:off:0000000003">g</a></p>'
    files = [(s1, [(at_a, a), (at_b, b + to_gamma)]),
             (s2, [(at, c + b'<p><a href="kindle:pos:fid:0001:off:0000000003">b</a></p>')])]
    _, ctx, raw = _seen_trees(_pdb(_kf8_recs(files)))
    beta = at_b + 3                                                      # skeleton 1 starts at 0
    gamma = len(s1) + len(a) + len(b + to_gamma) + at + 3                # skeleton 2 starts after file 1's bytes
    for kp, word in ((beta, b"beta"), (gamma, b"gamma")):
        assert b'<a id="kp%d"></a>%s' % (kp, word) in raw and b'href="#kp%d"' % kp in raw, (kp, word)
    assert not ctx.skipped


def test_kindle_pos_past_end():
    for fid, off in ((b"0000", b"00000000VV"), (b"0007", b"0000000001"), (b"00Z0", b"0000000001")):
        doc, ctx, raw = _seen_trees(_pdb(_kf8_recs(_pos_files(fid, off))))
        assert b'id="kp' not in raw and b"#kp" not in raw and b"kindle:pos" not in raw
        assert "go" in _text_of(doc.blocks) and "alpha target" in _text_of(doc.blocks) and not ctx.skipped
    # past the anchor cap: the link keeps its text, counted once
    old = mobi.MOBI_ANCHORS
    mobi.MOBI_ANCHORS = 0
    try:
        doc, ctx, raw = _seen_trees(_pdb(_kf8_recs(_pos_files())))
    finally:
        mobi.MOBI_ANCHORS = old
    assert b"#kp" not in raw and "go" in _text_of(doc.blocks) and ctx.skipped == {"link": 1}


def test_combo_first_image_relative():
    flow0 = b'<html><body><p>x</p><img src="kindle:embed:0001?mime=image/png"/></body></html>'
    # the absolute reading lands on KF8 record 0, not a picture; the relative one on the PNG
    doc, ctx = _read(_combo(OLD, _kf8_recs(flow0=flow0, images=[PNG])))
    assert [b.data for s in doc.blocks for b in s.blocks if isinstance(b, Image)] == [PNG] and not ctx.skipped
    # images shared before the boundary: the absolute reading sniffs
    doc, ctx = _read(_combo(OLD, _kf8_recs(flow0=flow0, first_image=2), shared=[PNG]))
    assert [b.data for s in doc.blocks for b in s.blocks if isinstance(b, Image)] == [PNG] and not ctx.skipped


def test_cncx_zero_padding():
    cncx = _vwi(5) + b"first" + _vwi(6) + b"second" + bytes(7)
    recs = _indx_records([(3, 1, 0x01, 0), (0, 0, 0, 1)], [("a", {3: [0]}), ("b", {3: [6]})], cncx)
    idx = mobi._indx(Palm(_pdb(recs)), 0, Context())
    assert list(idx) == [("a", {3: [0]}), ("b", {3: [6]})]
    assert idx.cncx == {0: "first", 6: "second"}


def test_indx_caps():
    tagx = [(1, 1, 0x01, 0), (0, 0, 0, 1)]

    def fails(recs):
        raises(mobi._Fallback, mobi._indx, Palm(_pdb(recs)), 0, Context())

    three = _indx_records(tagx, [("a", {1: [1]}), ("b", {1: [2]}), ("c", {1: [3]})])
    assert len(mobi._indx(Palm(_pdb(three)), 0, Context())) == 3
    files, _ = _two_files()
    old = mobi.INDX_ENTRIES
    mobi.INDX_ENTRIES = 2
    try:
        fails(three)
        doc, ctx = _read(_pdb(_kf8_recs(files + files[1:])))         # three skeletons
        assert ctx.skipped == {"plain_text": 1} and "gamma" in _text_of(doc.blocks)
    finally:
        mobi.INDX_ENTRIES = old
    fails(_indx_records([(t, 1, 0x01, 0) for t in range(1, 66)], [("a", {})]))    # 65 tags
    assert len(mobi._indx(Palm(_pdb(_indx_records([(t, 1, 0, 0) for t in range(1, 65)], []))), 0, Context())) == 0
    wide = SKEL_TAGX[:2] + [(t, 1, 0, 0) for t in range(100, 162)] + SKEL_TAGX[2:]
    doc, ctx = _read(_pdb(_kf8_recs(files, skel_tagx=wide)))
    assert len(wide) == 65 and ctx.skipped == {"plain_text": 1}
    main, rec = _indx_records(tagx, [("a", {1: [1]})])
    m = bytearray(main)
    m[56 + 8:56 + 12] = _u32(9)                                                      # 9 control bytes
    fails([bytes(m), rec])
    m = bytearray(main)
    m[24:28] = _u32(2)                                                               # more records than left
    fails([bytes(m), rec])
    fails(_indx_records(tagx, [("a", {1: [2 ** 35]})]))                              # a 6-byte varint
    big = _indx_records(tagx, [("a", {1: [2 ** 35 - 1]})])                           # a 5-byte varint
    assert mobi._indx(Palm(_pdb(big)), 0, Context())[0][1] == {1: [2 ** 35 - 1]}
    fails(_indx_records(tagx, [("a", {1: [1]})])[:1] + [rec[:60]])                  # an entry past its record
    # FDST: 1,001 flows, a range past the text, a start after its end, overlapping or out-of-order flows
    recs = _kf8_recs(files)
    at = mobi._head(recs[0]).fdst
    n = mobi._head(recs[0]).text_length
    for flows in ([(0, n)] + [(n, n)] * 1000, [(0, n + 1)], [(5, 4)], [(0, n), (0, n)], [(4, n), (0, 4)]):
        recs[at] = _fdst(flows)
        palm = Palm(_pdb(recs))
        raises(mobi._Fallback, mobi._fdst, palm, mobi._head(palm.record(0)))
        doc, ctx = _read(_pdb(recs))
        assert ctx.skipped == {"plain_text": 1} and "gamma" in _text_of(doc.blocks)
    recs[at] = _fdst([(0, n)] + [(n, n)] * 999)
    palm = Palm(_pdb(recs))
    assert len(mobi._fdst(palm, mobi._head(palm.record(0)))) == 1000


def _peak(fn):
    """fn's result and the peak bytes it allocated."""
    import tracemalloc
    tracemalloc.start()
    try:
        out = fn()
        return out, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_repeated_index_entries_fall_back():
    import time
    tagx = [(1, 1, 0x03, 0), (0, 0, 0, 1)]                 # mask full: a byte count, then that many bytes of values
    vals = b"\x80" * 20_000
    entry = b"\x01a\x03" + _vwi(len(vals)) + vals

    def index(copies):
        rec = bytearray(56)
        rec[0:4], rec[4:8], rec[20:24], rec[24:28] = b"INDX", _u32(56), _u32(56 + len(entry)), _u32(copies)
        rec += entry + b"IDXT" + (56).to_bytes(2, "big") * copies
        return Palm(_pdb(_indx_records(tagx, [])[:1] + [bytes(rec)]))
    assert mobi._indx(index(1), 0, Context())[0][1] == {1: [0] * 20_000}
    t = time.perf_counter()
    raises(mobi._Fallback, mobi._indx, index(200), 0, Context())   # 200 offsets on one entry
    assert time.perf_counter() - t < 1
    three = Palm(_pdb(_indx_records([(1, 1, 0x01, 0), (0, 0, 0, 1)], [("a", {1: [1]}), ("b", {1: [2]}),
                                                                     ("c", {1: [3]})])))
    old = mobi.INDX_VALUES
    mobi.INDX_VALUES = 2
    try:
        raises(mobi._Fallback, mobi._indx, three, 0, Context())
    finally:
        mobi.INDX_VALUES = old
    assert len(mobi._indx(three, 0, Context())) == 3


def test_overlapping_flows_and_skeletons_stay_bounded():
    s = b"<html><body><p>" + b"x" * 200_000 + b"</p></body></html>"
    recs = _kf8_recs([(s, [])])
    head = mobi._head(recs[0])
    flows = list(recs)
    flows[head.fdst] = _fdst([(0, len(s))] * 1000)                 # 1,000 overlapping full-length flows
    skels = list(recs)
    skels[head.skel:head.skel + 2] = _indx_records(SKEL_TAGX, [(f"S{i:04d}", {1: [0, 0], 6: [0, len(s), 0, len(s)]})
                                                               for i in range(1000)])  # 1,000 overlapping skeletons
    for book in (flows, skels):
        data = _pdb(book)
        (doc, ctx), peak = _peak(lambda: _read(data))
        assert ctx.skipped == {"plain_text": 1} and "x" * 1000 in _text_of(doc.blocks)
        assert peak < 20 * len(s), peak


def test_kf8_regexes_stay_linear():
    import time
    for unit in (b"<a href=kindle:pos:fid:0000:off:0000000000 ", b"<a ", b"<html "):
        data = unit * (300_000 // len(unit))
        t = time.perf_counter()
        try:
            mobi.parse_bytes(_pdb(_kf8_recs(flow0=data)), Context())
        except ReadError:
            pass
        assert time.perf_counter() - t < 4, unit


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


# ---- NCX headings and footnotes
NCX_TAGX = [(1, 1, 0x01, 0), (3, 1, 0x02, 0), (4, 1, 0x04, 0), (0, 0, 0, 1)]


def _ncx_recs(entries):
    """The NCX index records of entries (position, depth, label)."""
    cncx, rows = b"", []
    for pos, depth, label in entries:
        raw = label.encode()
        rows.append((f"{len(rows):04d}", {1: [pos], 3: [len(cncx)], 4: [depth]}))
        cncx += _vwi(len(raw)) + raw
    return _indx_records(NCX_TAGX, rows, cncx=cncx + bytes(4))


def _ncx_book(text, entries, **kw):
    chunks = max(1, -(-len(text) // 4096))
    return _mobi(text, extra=_ncx_recs(entries), ncx=1 + chunks, **kw)


def _blocks(doc):
    return [b for s in doc.blocks for b in s.blocks]


def _headings(doc):
    return [(b.level, _text_of(b.inlines)) for b in _blocks(doc) if isinstance(b, Heading)]


NCX_BODY = b"<html><body><p>Intro</p><p>Chapter A</p><p>text</p><p>Sub B</p><p>body</p></body></html>"


def test_ncx_headings():
    entries = [(NCX_BODY.index(b"<p>Chapter A"), 0, "Chapter A"), (NCX_BODY.index(b"<p>Sub B"), 1, "Sub B")]
    doc, ctx = _read(_ncx_book(NCX_BODY, entries))
    assert _headings(doc) == [(1, "Chapter A"), (2, "Sub B")]
    assert [type(b).__name__ for b in _blocks(doc)] == ["Para", "Heading", "Para", "Heading", "Para"]
    assert not ctx.skipped
    assert mobi._ncx(Palm(_ncx_book(NCX_BODY, entries)), mobi._head(Palm(_ncx_book(NCX_BODY, entries)).record(0)), 0,
                     Context()) == [(e[0], e[1], e[2]) for e in entries]
    deep = mobi._mark_headings(b"<p>a</p>", [(3, 9, "")])
    assert deep == b"<p>\x1e5a</p>"


def test_ncx_marker_inside_tag():
    text = b'<html><body><p>x</p><p class="c">Title</p><p>y</p></body></html>'
    pos = text.index(b"class")
    assert mobi._mark_headings(text, [(pos, 0, "")]) == text.replace(b'class="c">', b'class="c">\x1e0')
    doc, _ = _read(_ncx_book(text, [(pos, 1, "Title")]))
    assert _headings(doc) == [(2, "Title")]


def test_ncx_marker_in_table_cell():
    text = b'<html><body><p>x</p><table><tr><td class="k">cell</td></tr></table></body></html>'
    doc, ctx = _read(_ncx_book(text, [(text.index(b"class"), 0, "cell")]))
    assert _headings(doc) == []
    md = _md(doc, ctx)
    assert "cell" in md and "\x1e" not in md and "\x0e" not in md
    doc, ctx = _read(_ncx_book(NCX_BODY, [(NCX_BODY.index(b"<p>text"), 0, "")]))
    assert "\x1e" not in _md(doc, ctx) and "\x0e" not in _md(doc, ctx)


def test_ncx_skipped_when_text_has_1e():
    text = NCX_BODY.replace(b"Intro", b"In\x1etro")
    doc, ctx = _read(_ncx_book(text, [(text.index(b"<p>Chapter A"), 0, "Chapter A")]))
    assert _headings(doc) == [] and "Chapter A" in _text_of(doc.blocks)
    assert "In tro" in _text_of(doc.blocks)   # the book's own byte is left as the html pass leaves it: white space
    assert not ctx.skipped
    doc, _ = _read(_ncx_book(NCX_BODY.replace(b"Intro", b"In\x0etro"), [(NCX_BODY.index(b"<p>Chapter A"), 0, "")]))
    assert _headings(doc) == []


def test_ncx_items_cap():
    entries = [(NCX_BODY.index(b"<p>Chapter A"), 0, "A"), (NCX_BODY.index(b"<p>Sub B"), 1, "B")]
    old = limits.TOC_ITEMS
    limits.TOC_ITEMS = 1
    try:
        doc, ctx = _read(_ncx_book(NCX_BODY, entries))
    finally:
        limits.TOC_ITEMS = old
    assert _headings(doc) == [(1, "Chapter A")] and not ctx.skipped
    long = _ncx_book(NCX_BODY, [(5, 0, "x" * 1000)])
    assert len(mobi._ncx(Palm(long), mobi._head(Palm(long).record(0)), 0, Context())[0][2]) == mobi.MAX_LABEL


def test_ncx_unreadable_gives_nothing():
    book = _ncx_book(NCX_BODY, [(NCX_BODY.index(b"<p>Chapter A"), 0, "A")])
    palm = Palm(book)
    head = mobi._head(palm.record(0))
    broken = _pdb([palm.record(i) if i != head.ncx else b"XXXX" + palm.record(i)[4:] for i in range(palm.count)])
    ctx = Context()
    assert mobi._ncx(Palm(broken), head, 0, ctx) == [] and not ctx.skipped and not ctx.warnings
    doc, ctx = _read(broken)
    assert _headings(doc) == [] and "Chapter A" in _text_of(doc.blocks) and not ctx.skipped


def test_ncx_in_kf8():
    for files in ([(b"<html><body><p>one</p><p>Two</p></body></html>", [])],):
        flow0, _, _ = _kf8_text(files)
        recs = _kf8_recs(files)
        ncx_at = len(recs)
        recs += _ncx_recs([(flow0.index(b"<p>Two"), 0, "Two")])
        recs[0] = _rec0(version=8, text_length=len(flow0), text_records=1, fdst=2, skel=3, frag=5, ncx=ncx_at)
        doc, ctx = _read(_pdb(recs))
        assert _headings(doc) == [(1, "Two")] and not ctx.skipped
    # no skeleton: two <html> parts, the second holds the position
    flow0 = b"<html><body><p>one</p></body></html><html><body><p>Two</p></body></html>"
    recs = _kf8_recs(flow0=flow0)
    ncx_at = len(recs)
    recs += _ncx_recs([(flow0.index(b"<p>Two"), 0, "Two")])
    recs[0] = _rec0(version=8, text_length=len(flow0), text_records=1, fdst=2, ncx=ncx_at)
    doc, _ = _read(_pdb(recs))
    assert _headings(doc) == [(1, "Two")]


NCX6_TAGX = [(1, 1, 0x01, 0), (3, 1, 0x02, 0), (4, 1, 0x04, 0), (6, 2, 0x08, 0), (0, 0, 0, 1)]


def test_ncx_in_kf8_fragments():
    """The NCX position (tag 6: fid, offset) lands in the assembled part wherever the fragment was inserted."""
    files, want = _two_files()
    recs = _kf8_recs(files)
    ncx_at = len(recs)
    cncx, rows = b"", []
    for label, fid, off, depth in (("Beta", 1, 3, 0), ("Gamma", 2, 3, 1)):
        rows.append((f"{len(rows):04d}", {1: [0], 3: [len(cncx)], 4: [depth], 6: [fid, off]}))
        cncx += _vwi(len(label)) + label.encode()
    ncx = _indx_records(NCX6_TAGX, rows, cncx=cncx + bytes(4))
    recs = _kf8_recs(files, ncx=ncx_at) + ncx
    doc, ctx = _read(_pdb(recs))
    assert _headings(doc) == [(1, "beta"), (2, "gamma")] and not ctx.skipped
    assert [type(b).__name__ for b in doc.blocks[0].blocks] == ["Para", "Heading"]


def test_footnote_last_chapter_is_the_last_with_text():
    pre = (b"<html><body><p>Intro<sup><a filepos=0000000000>1</a></sup> text.</p><mbp:pagebreak/>"
           b"<p>Chapter two start</p><p>1. The note text</p>" + b"<p>filler filler filler</p>" * 60)
    pre = pre.replace(b"filepos=0000000000", b"filepos=%010d" % pre.index(b"<p>1. The note"))
    for tail in (b"</body></html>", b"<mbp:pagebreak/><html><body></body></html>"):
        doc, ctx = _read(_mobi(pre + tail))
        md = _md(doc, ctx)
        assert "[^" in md and md.count("The note text") == 1


def test_one_chapter_book_takes_an_early_sup_link_as_a_note():
    """Spec-literal: the last chapter counts as a whole, so in a one-chapter book a short sup link to an early
    paragraph is a note."""
    doc, ctx = _read(_mobi(_fn_book(b"<a filepos=0000000000>1</a>", early=True)))
    md = _md(doc, ctx)
    assert "[^" in md and md.count("Start here") == 1


def test_strip_alt_of_images():
    from reader.model import Image as Img
    img = Img(b"x", "a" + chr(14) + "1b")
    xhtml.strip_marks([img])
    assert img.alt == "ab"


def _fn_book(link, *, early=False, sup=True, filler=40, body_note=b"<p>1. The note text</p>", tail=b""):
    """A book whose text holds a link, filler paragraphs and a note, with the link's filepos set to the note (or to the
    first paragraph when early)."""
    pre = b"<html><body><p>Start here</p>" + b"<p>Body text of the chapter." + b"</p><p>More.</p>" * 3
    mid = b"<p>filler filler filler</p>" * filler
    ref = b"<p>Claim" + (b"<sup>" + link + b"</sup>" if sup else link) + b" made.</p>"
    head = pre + ref + mid
    full = head + body_note + tail + b"</body></html>"
    pos = full.index(b"<p>Start here") if early else full.index(body_note)
    return full.replace(b"filepos=0000000000", b"filepos=%010d" % pos)


def test_footnote_heuristic_links():
    text = _fn_book(b"<a filepos=0000000000>1</a>")
    doc, ctx = _read(_mobi(text))
    md = _md(doc, ctx)
    assert "[^" in md and md.count("The note text") == 1
    body = "".join(_text_of(b) for s in doc.blocks for b in s.blocks)
    assert "The note text" not in body
    assert list(doc.notes) == ["#fp%d" % text.index(b"<p>1. The note")]


def test_footnote_rejects_index_entry():
    # a 2-character link in the last chapter to an early position of the body
    text = _fn_book(b"<a filepos=0000000000>ab</a>", early=True)
    text = text.replace(b"<p>Claim", b"<mbp:pagebreak/><p>Claim")
    doc, ctx = _read(_mobi(text))
    assert "[^" not in _md(doc, ctx) and not doc.notes
    # a 5-character link to a note near the end
    doc, ctx = _read(_mobi(_fn_book(b"<a filepos=0000000000>12345</a>")))
    assert "[^" not in _md(doc, ctx) and "The note text" in _md(doc, ctx)
    # a link with no sup and no noteref type
    doc, ctx = _read(_mobi(_fn_book(b"<a filepos=0000000000>1</a>", sup=False)))
    assert "[^" not in _md(doc, ctx)
    # a target in the middle of the book, not in the last chapter
    text = _fn_book(b"<a filepos=0000000000>1</a>", tail=b"<p>filler</p>" * 400)
    mid = text.index(b"<p>filler filler filler</p>")
    text = text.replace(b"filepos=%010d" % text.index(b"<p>1. The note"), b"filepos=%010d" % mid)
    text = text.replace(b"<p>1. The note", b"<mbp:pagebreak/><p>1. The note")
    doc, ctx = _read(_mobi(text))
    assert "[^" not in _md(doc, ctx)


def test_noteref_role_without_sup():
    text = _fn_book(b'<a epub:type="noteref" filepos=0000000000>*</a>', sup=False)
    doc, ctx = _read(_mobi(text))
    assert "[^" in _md(doc, ctx) and len(doc.notes) == 1
    text = _fn_book(b'<a role="doc-noteref" filepos=0000000000>*</a>', sup=False)
    doc, ctx = _read(_mobi(text))
    assert "[^" in _md(doc, ctx)


def test_note_never_whole_chapter():
    text = (b"<html><body><p>Start</p><p>See<sup><a filepos=0000000000>1</a></sup></p>" + b"<p>pad</p>" * 20
            + b"<br/>tail text outside any block</body></html>")
    text = text.replace(b"filepos=0000000000", b"filepos=%010d" % text.index(b"tail text"))
    doc, ctx = _read(_mobi(text))
    md = _md(doc, ctx)
    assert "[^" not in md and "tail text outside any block" in md and not doc.notes


def test_footnote_anchor_after_unclosed_paragraph():
    # no </p> before the note: the anchor lands inside the paragraph before it; the note is still the next one
    text = (b"<html><body><p>Claim<sup><a filepos=0000000000>1</a></sup> made." + b"<p>pad" * 30
            + b"<p>1. The note text<p>end</body></html>")
    text = text.replace(b"filepos=0000000000", b"filepos=%010d" % text.index(b"<p>1. The note"))
    doc, ctx = _read(_mobi(text))
    md = _md(doc, ctx)
    assert "[^" in md and md.count("The note text") == 1


def test_anchor_cap():
    old = mobi.MOBI_ANCHORS
    mobi.MOBI_ANCHORS = 1
    try:
        a = b"<p>Claim<sup><a filepos=0000000000>1</a></sup> and<sup><a filepos=1111111111>2</a></sup>.</p>"
        text = b"<html><body>" + a + b"<p>pad</p>" * 30 + b"<p>1. first note</p><p>2. second note</p></body></html>"
        text = text.replace(b"filepos=0000000000", b"filepos=%010d" % text.index(b"<p>1. first"))
        text = text.replace(b"filepos=1111111111", b"filepos=%010d" % text.index(b"<p>2. second"))
        doc, ctx = _read(_mobi(text))
    finally:
        mobi.MOBI_ANCHORS = old
    md = _md(doc, ctx)
    assert ctx.skipped["link"] == 1 and "[^" in md and "and2" in md and "second note" in md


def test_footnote_heuristic_is_linear():
    import time
    n = 6000
    links = b"".join(b"<p>c<sup><a filepos=0000000000>%d</a></sup></p>" % (i % 9 + 1) for i in range(n))
    text = b"<html><body>" + links + b"<p>pad</p>" * 50 + b"<p>1. the note</p></body></html>"
    text = text.replace(b"filepos=0000000000", b"filepos=%010d" % text.index(b"<p>1. the note"))
    t0 = time.perf_counter()
    doc, ctx = _read(_mobi(text))
    assert time.perf_counter() - t0 < 10 and len(doc.notes) == 1


def test_parse_path():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "b.mobi"
        p.write_bytes(_mobi(BOOK))
        assert len(mobi.parse(p, Context()).blocks) == 2


def test_ncx_list_item_becomes_heading():
    text = (b'<html><body><ol><li id="t1"><b>Plan</b></li></ol><p>text</p><ol><li><b>1. Part</b></li></ol>'
            b'<p>body</p><ol><li>item</li></ol></body></html>')
    entries = [(text.index(b"<li id"), 0, "Plan"), (text.index(b"<li><b>1."), 1, "1. Part")]
    doc, ctx = _read(_ncx_book(text, entries))
    assert _headings(doc) == [(1, "Plan"), (2, "1. Part")], _headings(doc)
    assert [type(b).__name__ for b in _blocks(doc)] == ["Heading", "Para", "Heading", "Para", "ListItem"]
    assert "\x0e" not in _md(doc, ctx)


def test_placeholder_title_left_out():
    doc, _ = _read(_mobi(BOOK, exth={503: b"Unknown"}, title=b"Unknown"))
    assert doc.title == "", doc.title
    doc, _ = _read(_mobi(BOOK, exth={503: b"Unknown"}, title=b"Real"))
    assert doc.title == "Real", doc.title


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
