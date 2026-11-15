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
from reader.limits import ReadError  # noqa: E402


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


def _huff_tables(phrases):
    """A HUFF and one CDIC record with 8-bit codes: code i is phrases[i].

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
    huff += bytes(32 * 8)
    slots = [(b"", True)] * 256
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
    old = palmdb.HUFF_DICT
    palmdb.HUFF_DICT = 1000
    try:
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
