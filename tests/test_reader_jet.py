"""Access (Jet and ACE): the page layer in jet_pages (page 0, the header RC4, db_key pages, usage maps, chains, text)
and the reader in jet (encryption, catalog, TDEFs, rows, scalar values, output).

Run: python -I -X utf8 tests/test_reader_jet.py
"""
import datetime
import tracemalloc
import struct
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers import crypto  # noqa: E402
from reader.formats import jet, jet_pages  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Para, Section, Table, Text  # noqa: E402
from jet_fix import (Col, Query, Row, Tbl, jet_db, BINARY, BIGINT, BOOL, BYTE, DATE, DOUBLE,  # noqa: E402
                     EXTDATE, FLOAT, GUID, INT, LONG, MEMO, MONEY, NUMERIC, OLE, TEXT)

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


# -- jet.py: the reader

def convert(data, hidden=False):
    ctx = Context()
    doc = jet.parse_bytes(data, ctx)
    with tempfile.TemporaryDirectory() as d:
        md = markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)
    return doc, ctx, md


def jet_refused(data):
    try:
        jet.parse_bytes(data, Context())
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("ReadError not raised")


def summary(doc):
    assert isinstance(doc.blocks[0], Para)
    return "".join(x.text for x in doc.blocks[0].inlines)


def sections(doc):
    return [b for b in doc.blocks if isinstance(b, Section)]


def section(doc, name):
    return next(s for s in sections(doc) if s.title.startswith(name + " ("))


def para_text(p):
    return "".join(x.text for x in p.inlines if isinstance(x, Text))


def grid(sec):
    """The table's cells as text, each row padded to the header's width (a Table keeps no trailing empty cells)."""
    table = next(b for b in sec.blocks if isinstance(b, Table))
    rows = [["".join(para_text(p) for p in cell.blocks) for cell in row] for row in table.rows]
    return [r + [""] * (len(rows[0]) - len(r)) for r in rows]


def columns_line(sec):
    return para_text(next(b for b in sec.blocks if isinstance(b, Para)))


def three():
    cols = [Col("ID", LONG), Col("Name", TEXT), Col("Notes", MEMO)]
    return Tbl("Orders", cols, [[1, "Ann", "first"], [2, "Bob", None], [3, "Cy", "third note"]], pk="ID")


def test_jet4_table():
    doc, ctx, md = convert(jet_db(1, [three()]))
    assert summary(doc) == "Access 2000-2003 database (Jet 4): 1 table."
    assert "## Table: Orders (3 rows)" in md
    sec = section(doc, "Orders")
    assert sec.kind == "table" and sec.title == "Orders (3 rows)" and not sec.hidden
    assert columns_line(sec) == "Columns: ID (Long) · Name (Text) · Notes (Memo)"
    assert grid(sec) == [["ID", "Name", "Notes"], ["1", "Ann", "first"], ["2", "Bob", ""], ["3", "Cy", "third note"]]
    assert not ctx.skipped, ctx.skipped
    for v in (0, 2, 3, 5, 6):  # the same table in every version
        doc, ctx, _ = convert(jet_db(v, [three()]))
        assert grid(section(doc, "Orders"))[1:] == [["1", "Ann", "first"], ["2", "Bob", ""], ["3", "Cy", "third note"]]


def test_version_names():
    names = {0: "Access 97 database (Jet 3)", 1: "Access 2000-2003 database (Jet 4)",
             2: "Access 2007 database (ACE 12)", 3: "Access 2010 database (ACE 14)",
             5: "Access 2016 database (ACE 16)", 6: "Access 2019 database (ACE 17)"}
    for v, name in names.items():
        assert summary(convert(jet_db(v))[0]) == f"{name}: 0 tables.", v
    two = [three(), Tbl("Two", [Col("A", LONG)], [[1]])]
    q = [Query("Q1", []), Query("Q2", [])]
    linked = [("Prices", ";DATABASE=prices.accdb", "Prices")]
    assert summary(convert(jet_db(1, two, queries=q, linked=linked))[0]) == \
        "Access 2000-2003 database (Jet 4): 2 tables, 2 queries, 1 linked table."
    one = "Access 2000-2003 database (Jet 4): 1 table, 1 query."
    assert summary(convert(jet_db(1, two[:1], queries=q[:1]))[0]) == one


def test_jet3_jump_table():
    # Jet3 rows past 256 and 512 bytes: the one-byte variable offsets go through the jump table
    cols = [Col("ID", LONG), Col("A", TEXT), Col("B", TEXT), Col("C", TEXT), Col("D", MEMO)]
    rows = [[1, "a" * 200, None, "c" * 150, "d" * 100], [2, "x" * 255, "y" * 255, "z" * 10, None],
            [3, None, None, "short", "m"]]
    doc, ctx, _ = convert(jet_db(0, [Tbl("Wide", cols, rows)]))
    want = [[str(r[0])] + ["" if v is None else v for v in r[1:]] for r in rows]
    assert grid(section(doc, "Wide"))[1:] == want
    assert not ctx.skipped, ctx.skipped
    assert summary(doc) == "Access 97 database (Jet 3): 1 table."


def ext_date(dt, frac7):
    days = dt.date().toordinal() - 1
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    return f"{days:019d}:{secs:012d}{frac7:07d}:7".encode("ascii") + b"\x00"


def test_scalar_values():
    when = datetime.datetime(2026, 11, 16, 10, 30, 0)
    cols = [Col("Flag", BOOL), Col("B", BYTE), Col("I", INT), Col("L", LONG), Col("Big", BIGINT), Col("M", MONEY),
            Col("S", FLOAT), Col("D", DOUBLE), Col("Dt", DATE), Col("X", EXTDATE),
            Col("N", NUMERIC, precision=18, scale=2), Col("G", GUID), Col("Bin", BINARY)]
    guid = "01234567-89ab-cdef-0123-456789abcdef"
    rows = [[True, 7, -3, 70000, 2 ** 40, 12345678, 0.1, 0.1, 45000.0, ext_date(when, 5000000), -12345, guid,
             bytes(range(12))],
            [False, 255, 32767, -2 ** 31, -1, 15000, 3.0, 1e100, 45000.5, ext_date(when, 0), 12300, None, None],
            [None, None, None, None, None, 10000, None, None, -1.25, None, None, None, None]]
    doc, ctx, _ = convert(jet_db(6, [Tbl("Vals", cols, rows)]))
    sec = section(doc, "Vals")
    assert columns_line(sec) == ("Columns: Flag (Yes/No) · B (Byte) · I (Integer) · L (Long) · Big (BigInt) · "
                                 "M (Currency) · S (Single) · D (Double) · Dt (Date) · X (Extended date) · "
                                 "N (Decimal) · G (GUID) · Bin (Binary)")
    assert grid(sec)[1:] == [
        ["Yes", "7", "-3", "70000", "1099511627776", "1234.5678", "0.1", "0.1", "2023-03-15", "2026-11-16 10:30:00.5",
         "-123.45", "{01234567-89AB-CDEF-0123-456789ABCDEF}", "<binary 12 bytes>"],
        ["No", "255", "32767", "-2147483648", "-1", "1.5", "3", "1e+100", "2023-03-15 12:00:00",
         "2026-11-16 10:30:00", "123.00", "", ""],
        ["No", "", "", "", "", "1", "", "", "1899-12-29 06:00:00", "", "", "", ""]]
    assert not ctx.skipped, ctx.skipped
    # value() on raw bytes: the shortest Single that packs back, and the time fraction is a positive day part
    db = jet._Db(jet_pages.open(jet_db(1)), Context())
    single = jet.Column("S", FLOAT, 0)
    for f in (0.1, 1 / 3, 16777216.0, -2.5e-7):
        raw = struct.pack("<f", f)
        s = jet.value(db, single, raw)
        assert struct.pack("<f", float(s)) == raw and len(s) <= len(repr(struct.unpack("<f", raw)[0])), s
    assert jet.value(db, jet.Column("D", DATE, 0), struct.pack("<d", 45000.99999999)) == "2023-03-16"
    assert jet.value(db, jet.Column("D", DATE, 0), struct.pack("<d", -0.5)) == "1899-12-30 12:00:00"
    assert jet.value(db, jet.Column("M", MONEY, 0), struct.pack("<q", -15000)) == "-1.5"
    assert jet.value(db, jet.Column("M", MONEY, 0), struct.pack("<q", 0)) == "0"
    assert uuid.UUID(guid).bytes_le != uuid.UUID(guid).bytes  # the GUID's first three fields are little-endian


def test_memo_cut():
    for v in (0, 1):
        tbl = Tbl("Long", [Col("ID", LONG), Col("Notes", MEMO)], [[1, "x" * 40_000], [2, "short"]])
        doc, ctx, _ = convert(jet_db(v, [tbl]))
        cells = grid(section(doc, "Long"))
        assert cells[1] == ["1", "x" * 32_768 + "…"] and cells[2] == ["2", "short"], v
        assert ctx.skipped["text"] == 1 and set(ctx.skipped) == {"text"}, ctx.skipped
    # a Text column over the cap is cut the same way
    old = jet.JET_CELL_CHARS
    jet.JET_CELL_CHARS = 5
    try:
        doc, ctx, _ = convert(jet_db(1, [Tbl("T", [Col("A", TEXT)], [["abcdefgh"], ["abc"]])]))
        assert grid(section(doc, "T"))[1:] == [["abcde…"], ["abc"]] and ctx.skipped["text"] == 1
    finally:
        jet.JET_CELL_CHARS = old


def test_deleted_and_overflow_rows():
    cols = [Col("ID", LONG), Col("Name", TEXT)]
    rows = [[1, "a"], Row("deleted", [2, "b"]), Row("overflow", [3, "c"]), [4, "d"]]
    for v in (0, 1):
        doc, ctx, _ = convert(jet_db(v, [Tbl("T", cols, rows)]))
        assert grid(section(doc, "T"))[1:] == [["1", "a"], ["3", "c"], ["4", "d"]], v
        assert not ctx.skipped, ctx.skipped  # the deleted row and the overflow's own flagged copy are not counted
    # an overflow row pointing at itself is followed once: the row it names is again a pointer, so it is counted
    doc, ctx, _ = convert(jet_db(1, [Tbl("T", cols, [[1, "a"], Row("loop"), [2, "b"]])]))
    assert grid(section(doc, "T"))[1:] == [["1", "a"], ["2", "b"]]
    assert ctx.skipped["record"] == 1 and "db_table" not in ctx.skipped


def test_deleted_column_offsets():
    cols = [Col("A", LONG), Col("B", LONG, deleted=True), Col("C", LONG), Col("D", TEXT, deleted=True), Col("E", TEXT)]
    rows = [[1, 2, 3, "dd", "e"], [10, None, 30, None, "ee"], [100, 200, None, "d", None]]
    for v in (0, 1, 3):
        doc, ctx, _ = convert(jet_db(v, [Tbl("Old", cols, rows)]))
        sec = section(doc, "Old")
        assert columns_line(sec) == "Columns: A (Long) · C (Long) · E (Text)"
        assert grid(sec) == [["A", "C", "E"], ["1", "3", "e"], ["10", "30", "ee"], ["100", "", ""]], v
        assert not ctx.skipped


def test_system_tables_absent():
    doc, ctx, md = convert(jet_db(1, [three()], queries=[Query("Q", [])]))
    assert [s.title for s in sections(doc)] == ["Orders (3 rows)"]
    assert "MSys" not in md and "db_table" not in ctx.skipped
    assert summary(doc) == "Access 2000-2003 database (Jet 4): 1 table, 1 query."
    # the catalog's own columns carry no compressed flag (as in real files), user Text and Memo columns do
    db = jet._Db(jet_pages.open(jet_db(1, [three()])), Context())
    assert all(c.ext == 0 for c in jet._tdef(db, 2).columns)
    objs = jet._catalog(db)
    user = next(o for o in objs if o.name == "Orders")
    assert [c.ext for c in jet._tdef(db, user.id & 0xFFFFFF).columns] == [0, 1, 1]
    assert {o.name for o in objs if o.flags & 0x80000002} >= {"MSysObjects", "MSysQueries", "MSysRelationships"}


def test_compressed_flag():
    # a flagged column's FF FE value is the compressed form; an unflagged one is plain UCS-2 even when it starts FF FE
    cols = [Col("Flagged", TEXT), Col("Plain", TEXT, compressed=False)]
    data = jet_db(1, [Tbl("Flags", cols, [["abc", "﻿xy"]])])
    assert b"\xff\xfeabc" in data and "﻿xy".encode("utf-16-le") in data
    doc, ctx, _ = convert(data)
    assert grid(section(doc, "Flags"))[1] == ["abc", "﻿xy"]


def test_hidden_table():
    secret = Tbl("Secret", [Col("A", TEXT)], [["classified"]], flags=0x08)
    doc, ctx, md = convert(jet_db(1, [secret, three()]))
    sec = section(doc, "Secret")
    assert sec.hidden == "hidden" and sec.title == "Secret (1 row)"
    assert "Secret" not in md and "classified" not in md and "## Table: Orders (3 rows)" in md
    _, _, shown = convert(jet_db(1, [secret, three()]), hidden=True)
    assert "## Table: Secret (1 row) (hidden)" in shown and "classified" in shown
    assert summary(doc) == "Access 2000-2003 database (Jet 4): 2 tables."


def test_password_encrypted():
    assert jet_refused(jet_db(1, [three()], password="secret")) == ("encrypted", "database password set")
    assert jet_refused(jet_db(0, [three()], password="pw")) == ("encrypted", "database password set")
    assert jet_refused(jet_db(3, [three()], password="pw")) == ("encrypted", "database password set")
    # Jet4's empty password is stored as the creation-date mask itself: unmasked it is zero, so the file reads
    raw = jet_pages.open(jet_db(1)).page(0)[0x42:0x42 + 40]
    assert any(raw)
    assert summary(convert(jet_db(1, [three()]))[0]).endswith("1 table.")


def test_db_key_decoded():
    for v in (0, 1):
        plain = convert(jet_db(v, [three()]))[2]
        assert convert(jet_db(v, [three()], db_key=0x1234))[2] == plain, v


def test_ace_encryption():
    for v in (2, 3, 5, 6):
        assert jet_refused(jet_db(v, [three()], db_key=0x1234)) == ("encrypted", "ACE encryption"), v
    data = bytearray(jet_db(3, [three()]))
    data[0x299:0x29B] = (0x50).to_bytes(2, "little")  # the length of an Office encryption structure
    assert jet_refused(bytes(data)) == ("encrypted", "ACE encryption")
    data = bytearray(jet_db(1, [three()]))
    data[0x299:0x29B] = (0x50).to_bytes(2, "little")  # Jet4 has no such structure: not looked at
    assert summary(convert(bytes(data))[0]).endswith("1 table.")


def break_tdef(data, name):
    data = bytearray(data)
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    page = next(o for o in jet._catalog(db) if o.name == name).id & 0xFFFFFF
    data[page * db.jet.page_size] = 0x01  # no longer a TDEF page
    return bytes(data)


def test_bad_tdef_counted():
    tables = [three(), Tbl("Other", [Col("A", LONG)], [[5]])]
    for v in (0, 1):
        doc, ctx, _ = convert(break_tdef(jet_db(v, tables), "Orders"))
        assert [s.title for s in sections(doc)] == ["Other (1 row)"]
        assert ctx.skipped["db_table"] == 1
        both = break_tdef(break_tdef(jet_db(v, tables), "Orders"), "Other")
        assert jet_refused(both) == ("damaged", "no table could be read")
    # a broken table beside a saved query: the file still reads
    doc, ctx, _ = convert(break_tdef(jet_db(1, tables[:1], queries=[Query("Q", [])]), "Orders"))
    assert not sections(doc) and ctx.skipped["db_table"] == 1


def test_cols_cap():
    # the cap is set at the catalog's own width (every TDEF has it), so one more column refuses a user table
    data = jet_db(1, [])
    ncat = len(jet._tdef(jet._Db(jet_pages.open(data), Context()), 2).columns)
    old = jet.JET_COLS
    jet.JET_COLS = ncat
    try:
        tables = [Tbl("Wide", [Col(f"c{i}", LONG) for i in range(ncat + 1)], [[1] * (ncat + 1)]),
                  Tbl("Pair", [Col("A", LONG), Col("B", TEXT)], [[1, "x"]])]
        doc, ctx, _ = convert(jet_db(1, tables))
        assert [s.title for s in sections(doc)] == ["Pair (1 row)"] and ctx.skipped["db_table"] == 1
    finally:
        jet.JET_COLS = old


def tdef_page(data, name):
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    return next(o for o in jet._catalog(db) if o.name == name).id & 0xFFFFFF


def test_pages_not_shared():
    # a usage map naming another table's data pages reads none of them (each data page names its own TDEF)
    a = Tbl("A", [Col("ID", LONG), Col("Name", TEXT)], [[i, f"a{i}"] for i in range(5)])
    b = Tbl("B", [Col("ID", LONG), Col("Name", TEXT)], [[9, "b"]])
    data = bytearray(jet_db(1, [a, b]))
    ta, tb = tdef_page(data, "A"), tdef_page(data, "B")
    pages = jet_pages.open(bytes(data))
    ptr = data[tb * 4096 + 55:tb * 4096 + 59]
    row, pg = ptr[0], int.from_bytes(ptr[1:4], "little")
    start = struct.unpack_from("<H", pages.page(pg), 14 + 2 * row)[0] & 0x1FFF
    mine = owned_map(pages, ta)
    data[pg * 4096 + start:pg * 4096 + start + len(mine)] = mine
    doc, ctx, _ = convert(bytes(data))
    assert len(grid(section(doc, "A"))) == 6 and len(grid(section(doc, "B"))) == 1
    # a TDEF chain reaching a page another TDEF already holds ends there
    wide = Tbl("Wide", [Col(f"column number {i}", LONG) for i in range(200)], [[1] * 200])
    data = bytearray(jet_db(1, [wide, Tbl("Small", [Col("A", LONG)], [[7]])]))
    tw, ts = tdef_page(data, "Wide"), tdef_page(data, "Small")
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    cont = jet._tdef(db, tw).pages[1:]
    assert cont, "the wide TDEF spans more than one page"
    data[ts * 4096 + 4:ts * 4096 + 8] = cont[0].to_bytes(4, "little")
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    assert jet._tdef(db, tw).pages[1:] == cont and jet._tdef(db, ts).pages == [ts]
    doc, ctx, _ = convert(bytes(data))
    assert grid(section(doc, "Small"))[1:] == [["7"]] and len(grid(section(doc, "Wide"))[0]) == 200


def test_catalog_cap():
    # 7 containers and 3 system tables come first; a cap of 11 rows keeps the first user table only
    tables = [Tbl("N" * 70, [Col("A", LONG)], [[1]]), Tbl("Second", [Col("A", LONG)], [[2]])]
    old = jet.JET_CATALOG
    jet.JET_CATALOG = 11
    try:
        doc, ctx, _ = convert(jet_db(1, tables))
    finally:
        jet.JET_CATALOG = old
    assert [s.title for s in sections(doc)] == ["N" * 64 + " (1 row)"]
    assert ctx.skipped["record"] == 1 and ctx.skipped["text"] == 1
    doc, ctx, _ = convert(jet_db(1, tables))
    assert len(sections(doc)) == 2


def shared_memo(n, size=20_000, *, distinct=False, long_by=0):
    """A Jet4 table whose row 0 holds a memo of `size` a's (compressed up to 1,024 characters, else UCS-2; one LVAL row
    up to 4,076 bytes, else an LVAL chain) and whose n other rows hold inline memo definitions patched in memory to
    name it, as the review's probe. `distinct` gives each copy its own length (one byte less each), so no two
    definitions are alike; `long_by` raises row 0's declared length past the bytes stored."""
    tbl = Tbl("M", [Col("ID", LONG), Col("Notes", MEMO)], [[0, "a" * size]] + [[i, "zz"] for i in range(1, n + 1)])
    data = bytearray(jet_db(1, [tbl]))
    length = size + 2 if size <= 1024 else 2 * size
    kind = 0 if length > 4076 else 0x40000000
    at = data.find(struct.pack("<I", length | kind))
    assert at > 0 and data.find(struct.pack("<I", length | kind), at + 1) < 0
    head = bytes(data[at:at + 12])
    if long_by:
        data[at:at + 4] = struct.pack("<I", (length + long_by) | kind)
    inline = struct.pack("<I", 4 | 0x80000000) + bytes(8) + b"\xff\xfezz"
    spots = [i for i in range(len(data)) if data.startswith(inline, i)]
    assert len(spots) == n, len(spots)
    for k, i in enumerate(spots, 1):
        data[i:i + 12] = (struct.pack("<I", (length - k) | kind) + head[4:]) if distinct else head
    return bytes(data)


class Count:
    """Wraps a method or function for one block, counting the calls (`calls`), the bytes they return (`bytes`) and
    the bytes they are given (`given`); `big` counts the calls given a bytes argument over 1,000 bytes."""

    def __init__(self, owner, name):
        self.owner, self.name = owner, name
        self.calls = self.bytes = self.big = self.given = 0

    def __enter__(self):
        orig = self.orig = getattr(self.owner, self.name)

        def wrapped(*a, **k):
            self.calls += 1
            self.given += sum(len(x) for x in a if isinstance(x, (bytes, bytearray)))
            if any(isinstance(x, (bytes, bytearray)) and len(x) > 1000 for x in a):
                self.big += 1
            out = orig(*a, **k)
            if isinstance(out, (bytes, bytearray)):
                self.bytes += len(out)
            return out
        setattr(self.owner, self.name, wrapped)
        return self

    def __exit__(self, *exc):
        setattr(self.owner, self.name, self.orig)


def memo_cost(data):
    """What reading row 0's memo once charges against the file's work total."""
    db = jet._Db(jet_pages.open(data), Context())
    tdef = next(jet._tdef(db, o.id & 0xFFFFFF) for o in jet._catalog(db) if o.name == "M")
    _, raw = next(jet._rows(db, tdef))
    before = db.spent
    jet.value(db, tdef.columns[1], raw[1])
    return db.spent - before


def catalog_spend(data):
    """What reading the catalog charges against the work total (charged before any table)."""
    db = jet._Db(jet_pages.open(data), Context())
    jet._catalog(db)
    return db.spent


def test_long_value_budget():
    # 200 cells naming one 40,000-byte chain: read and decoded once; each repeat is charged, so the file's work total
    # (ten times the file and 1 MiB here) stops the table when it is spent: one sheet_cut, no row past it
    data = shared_memo(200)
    with Count(jet_pages.Jet, "text") as text, Count(jet._Db, "row_at") as rows:
        doc, ctx, _ = convert(data)
    assert text.big == 1 and rows.calls == 10, (text.big, rows.calls)  # ten LVAL rows, read once
    cost = memo_cost(data)
    assert 40_000 < cost < 40_000 + 10 * 300, cost
    total = jet._Db(jet_pages.open(data), Context()).total
    assert total == 10 * len(data) + 2 ** 20 < limits.JET_WORK_TOTAL, total
    shown = [r[1] for r in grid(section(doc, "M"))[1:]]
    assert 0 < len(shown) <= total // cost and set(shown) == {"a" * 20_000} and ctx.skipped == {"sheet_cut": 1}
    old = limits.JET_WORK_TOTAL
    try:
        # the total, lowered to the catalog and five rows (each a row and its memo, and room for a row's two cells,
        # charged before the grid gives them back): five rows, then the cut
        limits.JET_WORK_TOTAL = catalog_spend(data) + 5 * (jet._ROW + cost) + 2 * jet._CELL
        doc, ctx, _ = convert(data)
        assert [r[1] for r in grid(section(doc, "M"))[1:]] == ["a" * 20_000] * 5 and ctx.skipped == {"sheet_cut": 1}
        # distinct definitions defeat the cache: the total still bounds the LVAL bytes read, the work stops
        data = shared_memo(200, distinct=True)
        limits.JET_WORK_TOTAL = catalog_spend(data) + 5 * (jet._ROW + cost) + 2 * jet._CELL
        with Count(jet._Db, "row_at") as rows:
            doc, ctx, _ = convert(data)
        assert rows.bytes <= 5 * cost and rows.calls <= 5 * 10 + 1, (rows.bytes, rows.calls)
        assert len(grid(section(doc, "M"))) <= 6 and ctx.skipped == {"sheet_cut": 1}, ctx.skipped
    finally:
        limits.JET_WORK_TOTAL = old


def test_long_value_forms():
    # one LVAL row (0x40): its text, read once for 50 cells; a total of two rows stops the table after them
    data = shared_memo(50, 500)
    with Count(jet._Db, "row_at") as rows:
        doc, ctx, _ = convert(data)
    assert [r[1] for r in grid(section(doc, "M"))[1:]] == ["a" * 500] * 51 and not ctx.skipped
    assert rows.calls == 1, rows.calls
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 2 * (jet._ROW + memo_cost(data)) + 2 * jet._CELL
    try:
        doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert [r[1] for r in grid(section(doc, "M"))[1:]] == ["a" * 500] * 2 and ctx.skipped == {"sheet_cut": 1}
    # a chain shorter than its declared length: what is there is shown, counted `record` (each cell naming it)
    doc, ctx, _ = convert(shared_memo(0, 5_000, long_by=1_000))
    assert grid(section(doc, "M"))[1] == ["0", "a" * 5_000] and ctx.skipped == {"record": 1}, ctx.skipped
    # a value over JET_LVAL_BYTES is read to the cap and cut, counted `text`
    old = jet.JET_LVAL_BYTES
    jet.JET_LVAL_BYTES = 1_000
    try:
        doc, ctx, _ = convert(shared_memo(3, 5_000))
    finally:
        jet.JET_LVAL_BYTES = old
    assert [r[1] for r in grid(section(doc, "M"))[1:]] == ["a" * 500 + "…"] * 4 and ctx.skipped == {"text": 4}


def null_rows(cols, n):
    """A table of `cols` Text columns whose first data page is rewritten in memory to n two-byte rows (column count
    0, no null mask: every cell null), as the review's probe."""
    data = bytearray(jet_db(1, [Tbl("Wide", [Col(f"c{i}", TEXT) for i in range(cols)], [["x"] + [None] * (cols - 1)])]))
    pages = jet_pages.open(bytes(data))
    pg = data_pages(pages, tdef_page(data, "Wide"))[0]
    page = bytearray(pages.page(pg)[:14]) + bytes(4096 - 14)
    struct.pack_into("<H", page, 12, n)
    for i in range(n):
        struct.pack_into("<H", page, 14 + 2 * i, 4096 - 2 * (i + 1))
    data[pg * 4096:(pg + 1) * 4096] = page
    return bytes(data)


class Walked(list):
    """A column list counting the columns every walk over it reaches."""
    n = 0

    def __iter__(self):
        for x in list.__iter__(self):
            Walked.n += 1
            yield x


def test_null_rows_linear():
    # 1,000 null rows under 1,000 columns: each row walks only the columns it holds (none), so the work is the
    # header's, not rows x columns
    data = null_rows(1_000, 1_000)
    orig = jet._tdef

    def counted(*a, **k):
        t = orig(*a, **k)
        t.columns = Walked(t.columns)
        return t
    Walked.n = 0
    jet._tdef = counted
    try:
        with Count(jet, "value") as values:
            doc, ctx, _ = convert(data)
    finally:
        jet._tdef = orig
    assert section(doc, "Wide").title == "Wide (1 row)" and not ctx.skipped, ctx.skipped
    assert values.calls == 0 and Walked.n < 6 * 1_000 + 2 * 1_000, (values.calls, Walked.n)
    # Yes/No cells still read from the null mask, "No" included (each one a grid cell, so the grid's caps bound it)
    doc, ctx, _ = convert(jet_db(1, [Tbl("B", [Col("A", LONG), Col("F", BOOL)], [[1, True], [2, False]])]))
    assert grid(section(doc, "B"))[1:] == [["1", "Yes"], ["2", "No"]]


def test_catalog_cols_cap():
    data = jet_db(1, [Tbl("Pair", [Col("A", LONG), Col("B", TEXT)], [[1, "x"]])])
    ncat = len(jet._tdef(jet._Db(jet_pages.open(data), Context()), 2).columns)
    old = jet.JET_COLS
    jet.JET_COLS = ncat - 1
    try:
        kind, detail = jet_refused(data)
    finally:
        jet.JET_COLS = old
    assert kind == "damaged" and "past JET_COLS" in detail, detail


def test_db_key_page_cache():
    # 100 overflow rows patched to name one target row: under a db_key each page is decoded once
    cols = [Col("ID", LONG), Col("Name", TEXT)]
    data = bytearray(jet_db(1, [Tbl("T", cols, [Row("overflow", [i, f"r{i}"]) for i in range(100)])], db_key=0x1234))
    pages = jet_pages.open(bytes(data))
    first = None
    for pg in data_pages(pages, tdef_page(data, "T")):
        page = bytearray(pages.page(pg))
        for r in range(struct.unpack_from("<H", page, 12)[0]):
            loc = struct.unpack_from("<H", page, 14 + 2 * r)[0]
            if loc & 0x4000 and not loc & 0x8000:
                at = loc & 0x1FFF
                first = first or bytes(page[at:at + 4])
                page[at:at + 4] = first
        key = ((0x1234 ^ pg) & 0xFFFFFFFF).to_bytes(4, "little")
        data[pg * 4096:(pg + 1) * 4096] = crypto.rc4(key, bytes(page))
    with Count(crypto, "rc4") as rc4:
        doc, ctx, _ = convert(bytes(data))
    assert grid(section(doc, "T"))[1:] == [["0", "r0"]] * 100 and not ctx.skipped, ctx.skipped
    assert rc4.calls <= pages.pages, (rc4.calls, pages.pages)
    # the cache holds at most JET_PAGE_CACHE pages, and what it gives back is the page
    old = limits.JET_PAGE_CACHE
    limits.JET_PAGE_CACHE = 2
    try:
        small = jet_pages.open(bytes(data))
        got = [small.page(n) for n in (1, 2, 3, 1, 2, 3, 3)]
        assert len(small._cache) == 2 and got == [pages.page(n) for n in (1, 2, 3, 1, 2, 3, 3)]
    finally:
        limits.JET_PAGE_CACHE = old


def test_duplicate_catalog_row():
    # two catalog rows naming one TDEF: one section, the second row counted, the summary counting the table once
    doc, ctx, _ = convert(jet_db(1, [Tbl("T", [Col("A", LONG)], [[1]]), Tbl("T", [Col("A", LONG)], [[2]])]))
    assert len(sections(doc)) == 1 and ctx.skipped == {"record": 1}, ctx.skipped
    assert summary(doc).endswith(": 1 table."), summary(doc)



def overlap_probe(n):
    """The re-review's probe: an ID and 400 compressed Text columns. The one target row's column 0 holds FF FE and
    1,000 zero bytes (text "", so no grid cap stops anything) and its 200 even var slots are patched to name that
    whole data area; n overflow rows point at it (with n 0 the row is an ordinary one)."""
    cols = [Col("ID", LONG)] + [Col(f"t{i}", TEXT) for i in range(400)]
    vals = [7, "a" * 1000] + [""] * 399
    data = bytearray(jet_db(1, [Tbl("O", cols, [Row("overflow", vals) for _ in range(n)] if n else [vals])]))
    pages = jet_pages.open(bytes(data))
    target = None
    for pg in data_pages(pages, tdef_page(data, "O")):
        page = pages.page(pg)
        for r in range(struct.unpack_from("<H", page, 12)[0]):
            loc = struct.unpack_from("<H", page, 14 + 2 * r)[0]
            at = loc & 0x1FFF
            if n and loc & 0x4000 and not loc & 0x8000:
                target = target or bytes(page[at:at + 4])
                data[pg * 4096 + at:pg * 4096 + at + 4] = target
            elif not n and not loc & 0xC000:
                target = bytes([r]) + pg.to_bytes(3, "little")
    tr, tp = target[0], int.from_bytes(target[1:4], "little")
    page = pages.page(tp)
    start = struct.unpack_from("<H", page, 14 + 2 * tr)[0] & 0x1FFF
    stop = 4096 if tr == 0 else struct.unpack_from("<H", page, 14 + 2 * tr - 2)[0] & 0x1FFF
    b = bytearray(page[start:stop])
    end = len(b) - (401 + 7) // 8
    nvar = struct.unpack_from("<H", b, end - 2)[0]
    s0, top = struct.unpack_from("<H", b, end - 4)[0], struct.unpack_from("<H", b, end - 4 - 2 * nvar)[0]
    assert nvar == 400 and b[s0:s0 + 2] == b"\xff\xfe" and top == s0 + 1002, (nvar, s0, top)
    b[s0 + 2:s0 + 1002] = bytes(1000)
    for i in range(nvar):
        struct.pack_into("<H", b, end - 4 - 2 * i, s0 if i % 2 == 0 else top)
    data[tp * 4096 + start:tp * 4096 + stop] = b
    return bytes(data)


def test_overflow_target_decoded_once():
    # 100 overflow rows naming one row whose 200 even slots overlap: the target is read and decoded once
    data = overlap_probe(100)
    with Count(jet_pages.Jet, "text") as text, Count(jet, "_cells") as cells:
        doc, ctx, _ = convert(data)
    rows = grid(section(doc, "O"))[1:]
    assert len(rows) == 100 and all(r == ["7"] + [""] * 400 for r in rows) and not ctx.skipped, ctx.skipped
    assert text.given < 250_000 and cells.calls < 20, (text.given, cells.calls)


def test_cell_decode_total():
    # one ordinary row whose 200 even slots each name the whole 1,002-byte data area: 200 KB decoded, under the
    # file's work total (ten times the file and 1 MiB) ...
    data = overlap_probe(0)
    doc, ctx, _ = convert(data)
    assert grid(section(doc, "O"))[1] == ["7"] + [""] * 400 and not ctx.skipped, ctx.skipped
    # ... and with the total lowered, the text decoded stops at it and so does the table (one sheet_cut)
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 50_000
    try:
        with Count(jet_pages.Jet, "text") as text:
            doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert text.given <= 50_000 + 10_000, text.given  # the TDEF's column names are read beside it
    assert len(grid(section(doc, "O"))) == 1 and ctx.skipped == {"sheet_cut": 1}, ctx.skipped


def repoint(data, name):
    """Every overflow row of table `name` patched in memory to name the first one's target row."""
    data = bytearray(data)
    pages = jet_pages.open(bytes(data))
    first = None
    for pg in data_pages(pages, tdef_page(data, name)):
        page = pages.page(pg)
        for r in range(struct.unpack_from("<H", page, 12)[0]):
            loc = struct.unpack_from("<H", page, 14 + 2 * r)[0]
            if loc & 0x4000 and not loc & 0x8000:
                at = pg * 4096 + (loc & 0x1FFF)
                first = first or bytes(data[at:at + 4])
                data[at:at + 4] = first
    return bytes(data)


def test_kept_target_counts_repeat():
    # a target row decoded once still counts for each row naming it: three OLE cells, `object` 3
    cols = [Col("ID", LONG), Col("Pic", OLE)]
    data = repoint(jet_db(1, [Tbl("P", cols, [Row("overflow", [i, b"x" * 10]) for i in range(3)])]), "P")
    doc, ctx, _ = convert(data)
    assert grid(section(doc, "P"))[1:] == [["0", "<OLE 10 bytes>"]] * 3 and ctx.skipped == {"object": 3}, ctx.skipped


def test_work_charges():
    # what each path costs: a row visited _ROW, each present cell _CELL (given back when it makes a grid cell, which
    # the grid's caps bound from there), text its bytes; a null cell is not visited
    data = jet_db(1, [Tbl("E", [Col("ID", LONG), Col("T", TEXT), Col("M", MEMO), Col("N", LONG)],
                          [[i, "", "x" * 30, None] for i in range(50)])])
    db = jet._Db(jet_pages.open(data), Context())
    jet._catalog(db)
    base = db.spent
    tdef = jet._tdef(db, tdef_page(data, "E"))
    rows = list(jet._rows(db, tdef))
    assert db.spent - base == 50 * (jet._ROW + 3 * jet._CELL), db.spent - base
    before = db.spent
    shown = jet._shown(db, tdef.columns, rows[0][1], [])
    assert shown == [(0, "0"), (2, "x" * 30)], shown
    assert db.spent - before == 32 - 2 * jet._CELL, db.spent - before  # FF FE and 30 bytes; ID and memo given back


def test_short_row_count():
    # a row written before columns were added holds a smaller column count: a column past it is absent, even when a
    # mask bit (in the same byte) names it
    data = bytearray(jet_db(1, [Tbl("S", [Col(c, LONG) for c in "ABCD"], [[1, 2, 3, 4]])]))
    pj = jet_pages.open(bytes(data))
    pg = data_pages(pj, tdef_page(data, "S"))[0]
    at = pg * 4096 + (struct.unpack_from("<H", pj.page(pg), 14)[0] & 0x1FFF)
    assert struct.unpack_from("<H", data, at)[0] == 4
    struct.pack_into("<H", data, at, 2)
    doc, ctx, _ = convert(bytes(data))
    assert grid(section(doc, "S"))[1] == ["1", "2", "", ""] and not ctx.skipped, (grid(section(doc, "S")), ctx.skipped)


def test_catalog_spent():
    # a total spent inside the catalog cuts its rest, counted `record` once; each table then stops at once
    data = jet_db(1, [three()])
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) // 2
    try:
        doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert ctx.skipped["record"] == 1, ctx.skipped


def patch_columns(data, name, kind=None):
    """Table `name`'s TDEF patched in memory: every column's var slot set to 0 (all share one slice), and with `kind`
    every column's type set to it (a var GUID: no slice is 16 bytes, so each cell is a `record`)."""
    data = bytearray(data)
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    page = tdef_page(data, name)
    td, f = jet._tdef(db, page), db.f
    b = db.jet.page(page) + b"".join(db.jet.page(p)[8:] for p in td.pages[1:])

    def at(o):
        if o < 4096:
            return page * 4096 + o
        k, r = divmod(o - 4096, 4088)
        return td.pages[1 + k] * 4096 + 8 + r
    col_at = f.idx_def + struct.unpack_from("<I", b, f.nidx)[0] * f.idx_def_size
    for i in range(struct.unpack_from("<H", b, f.num_cols)[0]):
        o = col_at + i * f.col_size
        if kind is not None:
            data[at(o)] = kind
        data[at(o + f.col_var)] = data[at(o + f.col_var + 1)] = 0
    return bytes(data)


def compact(kind=None, pages=4):
    """The re-review's shape: 1,000 Text columns sharing var slot 0 (with `kind`, retyped), each of the table's data
    pages rewritten in memory to 30 rows of 133 bytes: column count 1,000, one empty slot, every null bit set."""
    data = jet_db(1, [Tbl("C", [Col(f"c{i}", TEXT) for i in range(1_000)], [[""] * 1_000] * pages)])
    data = bytearray(patch_columns(data, "C", kind))
    row = struct.pack("<HHHH", 1_000, 2, 2, 1) + b"\xff" * 125
    pj = jet_pages.open(bytes(data))
    for pg in data_pages(pj, tdef_page(data, "C")):
        page = bytearray(pj.page(pg)[:14]) + bytes(4096 - 14)
        struct.pack_into("<H", page, 12, 30)
        for i in range(30):
            at = 4096 - 133 * (i + 1)
            struct.pack_into("<H", page, 14 + 2 * i, at)
            page[at:at + 133] = row
        data[pg * 4096:(pg + 1) * 4096] = page
    return bytes(data)


def test_empty_cells_bounded():
    # 120 compact rows of 1,000 empty cells (no grid cell each): the work total, lowered, stops the table
    data = compact()
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 100_000
    try:
        with Count(jet, "value") as values:
            doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert values.calls <= 100_000 // jet._CELL + 1_000 and ctx.skipped == {"sheet_cut": 1}, (values.calls, ctx.skipped)


def test_record_cells_bounded():
    # the same rows with every column a var GUID: each cell is a `record`, charged like any other, so the total stops it
    data = compact(GUID)
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 100_000
    try:
        doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert ctx.skipped["record"] <= 100_000 // jet._CELL + 1_000 and ctx.skipped["sheet_cut"] == 1, ctx.skipped
    assert set(ctx.skipped) == {"record", "sheet_cut"}, ctx.skipped


def target_peak(n):
    """(peak traced bytes, file bytes) parsing n overflow rows, each its own target, whose 1,000 columns (var GUIDs,
    each a `record`) share one 1,002-byte slot."""
    vals = ["a" * 1000] + [""] * 999
    data = jet_db(1, [Tbl("C", [Col(f"c{i}", TEXT) for i in range(1_000)], [Row("overflow", vals)] * n)])
    data = patch_columns(data, "C", GUID)
    tracemalloc.start()
    try:
        ctx = Context()
        jet.parse_bytes(data, ctx)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert ctx.skipped == {"record": 1_000 * n}, ctx.skipped
    return peak, len(data)


def test_target_memory():
    # nothing of a target is kept past its row (no slices, no bytes), so 40 more targets add no more than their own
    # pages (slices kept per target would add 1 MB each); beside that, a 1,000-column table costs a constant: its
    # Columns (about 400 KB) and one row's 1,000 views while it is read (about 450 KB), both bounded by JET_COLS
    (p20, n20), (p60, n60) = target_peak(20), target_peak(60)
    assert p60 - p20 < n60 - n20 and p60 < 4 * n60 + 2 ** 21, (p20, n20, p60, n60)


def test_short_value_counted_once():
    # a chain shorter than its declared length whose text is over JET_CELL_CHARS: one count, `record`
    doc, ctx, _ = convert(shared_memo(0, 40_000, long_by=1_000))
    assert grid(section(doc, "M"))[1] == ["0", "a" * 32_768 + "…"] and ctx.skipped == {"record": 1}, ctx.skipped


def test_rc4_total():
    # a cache of one page and a cycle over every page: each read misses; past twice the file's bytes (DECRYPT_BYTES
    # lowered under it) decoding stops with too_large
    data = jet_db(1, [three()], db_key=0x1234)
    old = limits.JET_PAGE_CACHE, limits.DECRYPT_BYTES
    limits.JET_PAGE_CACHE, limits.DECRYPT_BYTES = 1, 4096
    try:
        j = jet_pages.open(data)
        with Count(crypto, "rc4") as rc4:
            try:
                for _ in range(10):
                    for n in range(1, j.pages):
                        j.page(n)
            except ReadError as e:
                assert (e.kind, e.detail) == ("too_large", "part: RC4 pages"), (e.kind, e.detail)
            else:
                raise AssertionError("ReadError not raised")
        assert rc4.calls * j.page_size <= 2 * len(data), (rc4.calls, j.pages)
        # the file read through once stays under it
        limits.JET_PAGE_CACHE = old[0]
        assert summary(convert(data)[0]).endswith("1 table.")
    finally:
        limits.JET_PAGE_CACHE, limits.DECRYPT_BYTES = old


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
