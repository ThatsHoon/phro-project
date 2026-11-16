"""Access (Jet and ACE): the page layer in jet_pages (page 0, the header RC4, db_key pages, usage maps, chains, text).

Run: python -I -X utf8 tests/test_reader_jet.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.containers import crypto  # noqa: E402
from reader.formats import jet_pages  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from jet_fix import Col, Tbl, jet_db, LONG, TEXT, MEMO  # noqa: E402

# the first bytes of Jackcess's JetFormat.BASE_HEADER_MASK, the RC4 keystream of C7 DA 39 6B
JACKCESS_MASK = bytes.fromhex("B56F03626108C255")


def refused(data, kind, detail):
    try:
        jet_pages.open(data)
    except ReadError as e:
        assert (e.kind, e.detail) == (kind, detail), (e.kind, e.detail)
        return
    raise AssertionError("ReadError not raised")


def orders(n=3):
    cols = [Col("ID", LONG), Col("Name", TEXT), Col("Notes", MEMO)]
    return Tbl("Orders", cols, [[i, f"name {i}", "memo " * i] for i in range(1, n + 1)], pk=["ID"])


def owned_map(jet, tdef):
    """The owned-pages usage map row of the table whose TDEF is at `tdef`."""
    at = 35 if jet.version == 0 else 55
    head = jet.page(tdef)
    row, pg = head[at], int.from_bytes(head[at + 1:at + 4], "little")
    return row_bytes(jet, pg, row)


def row_bytes(jet, pg, row):
    page = jet.page(pg)
    base = 8 if jet.version == 0 else 12
    start = struct.unpack_from("<H", page, base + 2 + 2 * row)[0] & 0x1FFF
    end = jet.page_size if row == 0 else struct.unpack_from("<H", page, base + 2 * row)[0] & 0x1FFF
    return page[start:end]


def data_pages(jet, tdef):
    """Every data page whose tdef_pg names `tdef` (a scan of the whole file)."""
    return [n for n in range(1, jet.pages)
            if jet.page(n)[0] == 1 and int.from_bytes(jet.page(n)[4:8], "little") == tdef]


def test_open_versions():
    for v in (0, 1, 2, 3, 5, 6):
        data = jet_db(v)
        jet = jet_pages.open(data)
        assert jet.version == v
        assert jet.page_size == (2048 if v == 0 else 4096)
        assert jet.pages == len(data) // jet.page_size and jet.pages >= 3
        assert jet.page(2)[0] == 0x02  # the MSysObjects TDEF
        assert jet.db_key == 0


def test_refusals():
    data = bytearray(jet_db(1))
    data[4:19] = b"MSISAM Database"
    refused(bytes(data), "unsupported", "Microsoft Money database (MSISAM)")
    data = bytearray(jet_db(1))
    data[0:4] = b"\x01\x00\x00\x00"
    refused(bytes(data), "unsupported", "Access 1.x or 2.0 database")
    data = bytearray(jet_db(1))
    data[0x14] = 9
    refused(bytes(data), "unsupported", "unknown Jet version 9")
    refused(b"", "damaged", "jet: not a Jet database")
    refused(b"PK\x03\x04" + bytes(4092), "damaged", "jet: not a Jet database")
    refused(jet_db(1)[:2048], "damaged", "jet: shorter than one page")


def test_header_rc4():
    for v, size in ((0, 126), (1, 128), (2, 128)):
        data = jet_db(v)
        jet = jet_pages.open(data)
        assert len(jet.header) == size
        raw = data[0x18:0x18 + size]
        assert crypto.rc4(bytes.fromhex("C7DA396B"), jet.header) == raw
        assert bytes(a ^ b for a, b in zip(raw[:8], jet.header[:8])) == JACKCESS_MASK
        assert struct.unpack_from("<H", jet.header, 0x3C - 0x18)[0] == 1252
        assert struct.unpack_from("<H", raw, 0x3C - 0x18)[0] != 1252  # the stored bytes are encoded
        assert jet.codepage == "cp1252"
        assert jet.page(0)[0x18:0x18 + size] == jet.header and jet.page(0)[:0x18] == data[:0x18]
    assert jet_pages.open(jet_db(0, codepage=1251)).codepage == "cp1251"
    assert jet_pages.open(jet_db(0, codepage=932)).codepage == "cp932"
    for unknown in (0, 1, 12345):  # 0, no codec, and a number Python does not name: cp1252, nothing counted
        assert jet_pages.open(jet_db(0, codepage=unknown)).codepage == "cp1252"


def test_db_key_pages():
    for v in (0, 1):
        tables = [orders(40)]
        plain = jet_db(v, tables)
        coded = jet_db(v, tables, db_key=0x12345678)
        jet = jet_pages.open(coded)
        size = jet.page_size
        assert jet.db_key == 0x12345678 and len(coded) == len(plain)
        assert coded[3 * size:4 * size] != plain[3 * size:4 * size]
        for n in range(1, jet.pages):
            assert jet.page(n) == plain[n * size:(n + 1) * size], n
        assert jet.page(0)[0x3E:0x42] == (0x12345678).to_bytes(4, "little")
    # ACE with a db_key is Office encryption, not this encoding: pages come back as stored (jet.py refuses the file)
    coded = jet_db(2, [orders()], db_key=0x1234)
    jet = jet_pages.open(coded)
    assert jet.db_key == 0x1234 and jet.page(3) == coded[3 * 4096:4 * 4096]


def test_page_range():
    jet = jet_pages.open(jet_db(1))
    for n in (jet.pages, jet.pages + 7, -1):
        try:
            jet.page(n)
        except ValueError:
            continue
        raise AssertionError(n)
    assert len(jet.page(jet.pages - 1)) == jet.page_size


def test_usage_type0_and_type1():
    big = [Col("ID", LONG), Col("Name", TEXT)]
    for v in (0, 1, 3):
        for umap in (0, 1):
            tbl = Tbl("Big", big, [[i, "x" * 200] for i in range(120)], umap=umap)
            jet = jet_pages.open(jet_db(v, [tbl]))
            tdef = [n for n in range(3, jet.pages) if jet.page(n)[0] == 2][-1]  # the user table is the last TDEF
            mp = owned_map(jet, tdef)
            assert mp[0] == umap, (v, umap, mp[0])
            got = jet.usage(mp)
            want = data_pages(jet, tdef)
            assert len(want) > 4 and got == want, (v, umap, got, want)
    jet = jet_pages.open(jet_db(1, [orders()]))
    # type 0: pages past the file are dropped; page 0 is never a member
    mp = b"\x00" + (jet.pages - 2).to_bytes(4, "little") + b"\xff" * 4
    assert jet.usage(mp) == [jet.pages - 2, jet.pages - 1]
    assert jet.usage(b"\x00" + bytes(4) + b"\x03") == [1]
    # type 1: a pointer past the file, to page 0, or to a page that is not type 5 is dropped
    bad = b"\x01" + struct.pack("<4I", jet.pages + 3, 0, 2, 0)
    assert jet.usage(bad) == []
    for broken in (b"", b"\x00\x01", b"\x07" + bytes(8)):
        try:
            jet.usage(broken)
        except ValueError:
            continue
        raise AssertionError(broken)


def test_usage_type1_bits_past_file():
    # a type 5 page whose bitmap names pages far past the file: those ids are dropped
    data = bytearray(jet_db(1, [Tbl("T", [Col("ID", LONG)], [[1]], umap=1)]))
    jet = jet_pages.open(bytes(data))
    tdef = [n for n in range(3, jet.pages) if jet.page(n)[0] == 2][-1]
    mp = owned_map(jet, tdef)
    five = int.from_bytes(mp[1:5], "little")
    assert jet.page(five)[0] == 5
    data[five * 4096 + 4 + 1000] = 0xFF  # pages 8000-8007
    jet = jet_pages.open(bytes(data))
    assert jet.usage(mp) == data_pages(jet, tdef)


def test_chain_cycle():
    data = bytearray(jet_db(1, [orders()]))
    size = 4096
    pages = len(data) // size
    for pg, nxt in ((3, 4), (4, 3)):
        data[pg * size + 4:pg * size + 8] = nxt.to_bytes(4, "little")
    jet = jet_pages.open(bytes(data))
    assert list(jet.chain(3, jet.next_pg)) == [3, 4]
    data[4 * size + 4:4 * size + 8] = (pages + 9).to_bytes(4, "little")  # out of range ends it
    jet = jet_pages.open(bytes(data))
    assert list(jet.chain(3, jet.next_pg)) == [3, 4]
    data[4 * size + 4:4 * size + 8] = bytes(4)  # next_pg 0 ends it
    assert list(jet_pages.open(bytes(data)).chain(3, jet.next_pg)) == [3, 4]
    assert list(jet.chain(jet.pages, jet.next_pg)) == [] and list(jet.chain(0, jet.next_pg)) == []
    # a (page, row) step: the seen-set holds items, the page is range-checked, the walk ends at the page count
    hops = {(3, 0): (3, 1), (3, 1): (4, 0), (4, 0): (3, 1)}
    assert list(jet.chain((3, 0), hops.get)) == [(3, 0), (3, 1), (4, 0)]
    assert list(jet.chain((3, 0), lambda it: (it[0], it[1] + 1))) == [(3, r) for r in range(jet.pages)]
    assert list(jet.chain((3, 0), lambda it: (jet.pages, 0))) == [(3, 0)]


def test_text_forms():
    j3 = jet_pages.open(jet_db(0))
    assert j3.text(b"caf\xe9", False) == "café" and j3.text(b"caf\xe9", True) == "café"
    assert j3.text(b"\xff\xfeab", True) == "ÿþab"  # Jet3 has no compressed form
    assert jet_pages.open(jet_db(0, codepage=1251)).text(b"\xcf\xf0\xe8", False) == "При"
    j4 = jet_pages.open(jet_db(1))
    assert j4.text("café 한".encode("utf-16-le"), False) == "café 한"
    assert j4.text("café".encode("utf-16-le"), True) == "café"
    # FF FE: one-byte mode first, each 00 toggles. 가 (U+AC00) cannot follow a toggle (its low byte is 00, which
    # Jackcess and Access read as the next toggle), so 한 (U+D55C) stands in for it.
    assert j4.text(b"\xff\xfeab\x00" + "한".encode("utf-16-le") + b"\x00c", True) == "ab한c"
    assert j4.text(b"\xff\xfeab\x00" + "dé".encode("utf-16-le"), True) == "abdé"  # 64 00: the 00 is the high byte
    assert j4.text(b"\xff\xfe", True) == "" and j4.text(b"\xff\xfe\x00\x00", True) == ""
    assert j4.text(b"\xff\xfeab", False) == "﻿扡"  # not allowed the compressed form: plain UCS-2
    assert j4.text(b"a\x00b", False) == "a�"  # an odd byte left over
    assert jet_pages.open(jet_db(6)).text(b"\xff\xfeok", True) == "ok"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
