"""Access (Jet and ACE): the page layer in jet_pages (page 0, the header RC4, db_key pages, usage maps, chains, text)
and the reader in jet (encryption, catalog, TDEFs, rows, scalar values, output).

Run: python -I -X utf8 tests/test_reader_jet.py
"""
import datetime
import random
import tracemalloc
import struct
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import html, limits, markdown  # noqa: E402
from reader.containers import crypto, flate  # noqa: E402
from reader.formats import jet, jet_pages, sheet_grid  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Break, Context, Hidden, Image, Link, Para, Section, Table, Text  # noqa: E402
import jet_fix  # noqa: E402
from jet_fix import (Col, Query, Row, Tbl, jet_db, BINARY, BIGINT, BOOL, BYTE, COMPLEX, DATE, DOUBLE,  # noqa: E402
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
    # no flag: the FF FE header decides (Jackcess `decodeTextValue`), unless the compressed reading is not valid (a
    # two-byte segment that is not UTF-16, a control character), then plain UCS-2; a name (None) is never compressed
    assert j4.text(b"\xff\xfeab", False) == "ab"
    assert j4.text(b"\xff\xfeab\x00" + "한".encode("utf-16-le"), False) == "ab한"
    assert j4.text(b"\xff\xfe\x00\x01", False) == "\ufeff\u0100" and j4.text(b"\xff\xfe\x01a", False) == "\ufeff\u6101"
    assert j4.text(b"\xff\xfeab", None) == "\ufeff\u6261"
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
    """The summary paragraph's text, a line break as "\n"."""
    assert isinstance(doc.blocks[0], Para)
    return "".join("\n" if isinstance(x, Break) else x.text for x in doc.blocks[0].inlines)


def summary_lines(doc):
    return summary(doc).split("\n")


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
    assert columns_line(sec) == "Columns: ID (Long, PK) · Name (Text) · Notes (Memo)"
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
    assert summary_lines(convert(jet_db(1, two, queries=q, linked=linked))[0])[0] == \
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
    # a flagged column's FF FE value is the compressed form. So is an unflagged one's, as Access writes them and as
    # Jackcess reads them (the header decides), in a Text cell and a Memo; a plain UCS-2 value whose compressed reading
    # is not valid (U+FEFF U+0100: a one-byte two-byte segment) stays UCS-2
    cols = [Col("Flagged", TEXT), Col("Plain", TEXT, compressed=False), Col("Bare", TEXT, compressed="unflagged"),
            Col("Note", MEMO, compressed="unflagged")]
    note = "note " * 49 + "end"
    data = jet_db(1, [Tbl("Flags", cols, [["abc", "\ufeff\u0100", "def", note]])])
    assert b"\xff\xfeabc" in data and b"\xff\xfedef" in data and b"\xff\xfenote" in data
    db = jet._Db(jet_pages.open(data), Context())
    tdef = jet._tdef(db, tdef_page(data, "Flags"))
    assert [c.ext for c in tdef.columns] == [1, 0, 0, 0]
    doc, ctx, _ = convert(data)
    assert grid(section(doc, "Flags"))[1] == ["abc", "\ufeff\u0100", "def", note] and not ctx.skipped
    # the same in Jet3's neighbour ACE, and in a multi-value value (a flat table's cell)
    t = Tbl("M", [Col("ID", LONG), Col("V", COMPLEX, multi=TEXT)], [[1, ["red", "blue"]]])
    doc, _, _ = convert(jet_db(3, [Tbl("Flags", cols, [["abc", "\ufeff\u0100", "def", note]]), t]))
    assert grid(section(doc, "Flags"))[1] == ["abc", "\ufeff\u0100", "def", note]
    assert grid(section(doc, "M"))[1] == ["1", "red; blue"]


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


def catalog_spend(data, rels=True):
    """What reading the catalog and MSysRelationships charges against the work total (charged before any table);
    with rels False the catalog alone."""
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    if rels:
        jet._relationships(db, objs)
    return db.spent


def row_spend(data, name="M"):
    """(lead, per row) for table `name`, whose rows each cost the same: what the meter has charged when its first row
    is about to be read (the catalog and MSysRelationships, the TDEF, the map, the first page and its slots), and what
    each row then costs net (its row, mask byte, cells and value, the shown cells given back)."""
    db = jet._Db(jet_pages.open(data), Context())
    jet._relationships(db, jet._catalog(db))
    tdef = jet._tdef(db, tdef_page(data, name))
    spent = []
    for _, cells in jet._rows(db, tdef):
        jet._shown(db, tdef.columns, cells, [])
        spent.append(db.spent)
        if len(spent) == 3:
            break
    per = spent[2] - spent[1]
    assert spent[1] - spent[0] == per, spent
    return spent[0] - per, per


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
        # the total, lowered to what comes before the rows and five rows (each a row, its mask byte and its memo, and
        # room for a row's two cells, charged before the grid gives them back): five rows, then the cut
        lead, per = row_spend(data)
        assert per == jet._ROW + jet._GROUP + cost, (per, cost)
        limits.JET_WORK_TOTAL = lead + 5 * per + 2 * jet._CELL
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
    lead, per = row_spend(data)
    limits.JET_WORK_TOTAL = lead + 2 * per + 2 * jet._CELL
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
    # what each path costs: the map's bytes, each page it names _PAGE, each row slot _SLOT, a row visited _ROW, its
    # mask byte _GROUP, each present cell _CELL (given back when it makes a grid cell, which the grid's caps bound from
    # there), text its bytes; a null cell is not visited
    data = jet_db(1, [Tbl("E", [Col("ID", LONG), Col("T", TEXT), Col("M", MEMO), Col("N", LONG)],
                          [[i, "", "x" * 30, None] for i in range(50)])])
    db = jet._Db(jet_pages.open(data), Context())
    jet._catalog(db)
    base = db.spent
    tdef = jet._tdef(db, tdef_page(data, "E"))
    # its one page (a chain item), four column definitions and their names' bytes, its index blocks
    names = len("IDTMN".encode("utf-16-le"))
    idx = jet._IDX * (len(tdef.real) + len(tdef.logical))
    assert db.spent - base == jet_pages.CHAIN_STEP + 4 * jet._COLDEF + names + idx, (db.spent - base, idx)
    base = db.spent
    pages = db.jet.usage(tdef.owned)
    walk = db.spent - base + len(pages) * jet._PAGE + sum(db.rows_on(db.jet.page(p)) for p in pages) * jet._SLOT
    base = db.spent
    rows = list(jet._rows(db, tdef))
    assert db.spent - base == walk + 50 * (jet._ROW + jet._GROUP + 3 * jet._CELL), (db.spent - base, walk)
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


def patch_columns(data, name, kind=None, *, num=None, var=0):
    """Table `name`'s TDEF patched in memory: every column's var slot set to `var` (0: all share one slice), with
    `kind` every column's type set to it (a var GUID: no slice is 16 bytes, so each cell is a `record`), and with
    `num` every column's number set to it (one null-mask bit names them all)."""
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
        if num is not None:
            data[at(o + f.col_num)], data[at(o + f.col_num + 1)] = num & 0xFF, num >> 8
        data[at(o + f.col_var)], data[at(o + f.col_var + 1)] = var & 0xFF, var >> 8
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


# -- the work meter as the invariant

class Loops:
    """Counts the loop iterations (backward jumps) run in jet.py, jet_pages.py and crypto.py (the RC4 of db_key
    pages) while open, through sys.monitoring; any other code location, and every forward jump, is switched off at its
    first event."""
    TOOL = 4

    def __enter__(self):
        mon = sys.monitoring
        files = {jet.__file__, jet_pages.__file__, crypto.__file__}
        self.n = 0

        def jump(code, src, dst):
            if dst >= src or code.co_filename not in files:
                return mon.DISABLE
            self.n += 1
        mon.use_tool_id(self.TOOL, "jet loops")
        mon.register_callback(self.TOOL, mon.events.JUMP, jump)
        mon.set_events(self.TOOL, mon.events.JUMP)
        mon.restart_events()
        return self

    def __exit__(self, *exc):
        mon = sys.monitoring
        mon.set_events(self.TOOL, 0)
        mon.register_callback(self.TOOL, mon.events.JUMP, None)
        mon.free_tool_id(self.TOOL)


class Work:
    """What one parse did: `charged` the meter's units (given back or not), `total` its total, `loops` the loop
    iterations in jet.py and jet_pages.py, `puts` the grid cells put (bounded by the grid's own caps), `peak` the
    tracemalloc peak (0 untraced), `out` the Doc or the ReadError."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def bounded(self, size):
        """At most two loop iterations a unit charged and two a grid cell, past a constant for the fixed work (the
        header, page 0), and the units charged at most the file's total and the cells given back."""
        assert self.loops <= 2 * self.charged + 2 * self.puts + 2_000, (self.loops, self.charged, self.puts)
        top = min(limits.JET_WORK_TOTAL, jet_pages.WORK_PER_BYTE * size + 2 ** 20)
        assert self.charged <= top + jet._CELL * self.puts + 2 ** 18, (self.charged, top, self.puts)


def metered(data, trace=False):
    """Parse `data` once, counting (see `Work`); a ReadError is returned, any other exception raised."""
    meters = []
    orig = jet_pages.Meter.__init__

    def init(self, total):
        orig(self, total)
        meters.append(self)
    jet_pages.Meter.__init__ = init
    ctx = Context()
    try:
        with Count(sheet_grid.Grid, "put") as puts, Loops() as loops:
            if trace:
                tracemalloc.start()
            try:
                out = jet.parse_bytes(data, ctx)
            except ReadError as e:
                out = e
            finally:
                peak = tracemalloc.get_traced_memory()[1] if trace else 0
                if trace:
                    tracemalloc.stop()
    finally:
        jet_pages.Meter.__init__ = orig
    m = meters[-1]
    return ctx, Work(charged=m.charged, total=m.total, loops=loops.n, puts=puts.calls, peak=peak, out=out)


def page_of(tdef, rows, flags=None, ps=4096):
    """A data page (Jet4, or Jet3 with ps 2048) owned by `tdef` holding `rows` (bytes each) packed from its end,
    flags[i] OR-ed into row i's offset."""
    count = 8 if ps == 2048 else 12
    p = bytearray(ps)
    p[0], p[1] = 1, 1
    struct.pack_into("<I", p, 4, tdef)
    struct.pack_into("<H", p, count, len(rows))
    at = ps
    for i, r in enumerate(rows):
        at -= len(r)
        assert at >= count + 2 + 2 * len(rows)
        p[at:at + len(r)] = r
        struct.pack_into("<H", p, count + 2 + 2 * i, at | (flags[i] if flags else 0))
    return bytes(p)


def annex(data, tdefs, pages, ps=4096):
    """`pages` appended to a Jet4 (or Jet3, ps 2048) file, and one inline usage map naming them all (on a page after
    them) set as the owned map of each TDEF in `tdefs`, in memory."""
    data = bytearray(data)
    first = len(data) // ps
    for p in pages:
        data += p
    bitmap = bytearray((len(pages) + 7) // 8)
    for k in range(len(pages)):
        bitmap[k >> 3] |= 1 << (k & 7)
    mp = len(data) // ps
    data += page_of(0, [b"\x00" + struct.pack("<I", first) + bytes(bitmap)], ps=ps)
    owned = 35 if ps == 2048 else 55
    for t in tdefs:
        data[t * ps + owned:t * ps + owned + 4] = b"\x00" + mp.to_bytes(3, "little")
    return bytes(data)


def test_columns_charged():
    # the re-review's probes (New 1), in memory: 999 Text columns patched to share one column number. varnone: number
    # 0, var slot 500, rows of count 1 with no var offsets, so every column's slot is past the row's; dupnum: number
    # 7, rows of count 1, so every column is past the count. A row visits at most its own count of columns and
    # charges each before visiting it, so the loops stay within the meter's units (they ran 999 a row uncharged) ...
    base = jet_db(1, [Tbl("C", [Col(f"c{i}", TEXT) for i in range(999)], [])])
    for num, var, row in ((0, 500, struct.pack("<HHH", 1, 2, 0) + b"\x01"), (7, 0, struct.pack("<H", 1) + b"\xfe")):
        data = patch_columns(base, "C", num=num, var=var)
        tp = tdef_page(data, "C")
        k = (4096 - 14) // (len(row) + 2)
        data = annex(data, [tp], [page_of(tp, [row] * k) for _ in range(30)])
        ctx, w = metered(data)
        assert isinstance(w.out, jet.Doc) and not ctx.skipped, ctx.skipped
        w.bounded(len(data))
        # ... and a lowered total stops the table: one sheet_cut
        old = limits.JET_WORK_TOTAL
        limits.JET_WORK_TOTAL = catalog_spend(data) + 100_000
        try:
            ctx, w = metered(data)
        finally:
            limits.JET_WORK_TOTAL = old
        assert ctx.skipped == {"sheet_cut": 1}, ctx.skipped
        assert w.loops <= 2 * w.charged + 2 * w.puts + 2_000, (w.loops, w.charged)
    # a total that runs out inside the table's 999 column definitions: the table is not read, counted db_table (as a
    # table the meter stopped, not a damaged one)
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 10_000
    try:
        ctx, w = metered(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert ctx.skipped == {"db_table": 1} and not ctx.warnings and not sections(w.out), (ctx.skipped, ctx.warnings)


def test_jet3_offsets_charged():
    # a Jet3 row of count 1 whose 255 one-byte variable offsets name nothing (so it shows nothing and is not kept),
    # named by 289 pointer rows: each pointer reads it again, and its offsets and jump entries are charged first
    data = jet_db(0, [Tbl("J", [Col("T", TEXT)], [])])
    tp = tdef_page(data, "J")
    row = bytearray(300)
    row[0], row[298], row[299] = 1, 255, 0x01  # the count, nvar, the null mask (column 0 set)
    first = len(data) // 2048
    ptr = b"\x00" + first.to_bytes(3, "little")
    data = annex(data, [tp], [page_of(tp, [bytes(row)] + [ptr] * 289, [0] + [0x4000] * 289, ps=2048)], ps=2048)
    ctx, w = metered(data)
    assert section(w.out, "J").title == "J (0 rows)" and not ctx.skipped, ctx.skipped
    w.bounded(len(data))


def test_index_blocks_charged():
    # a TDEF patched to no columns and 300 real and 200 logical index blocks, read from its column area: each block
    # is charged before it is read
    data = bytearray(jet_db(1, [Tbl("X", [Col(f"c{i}", LONG) for i in range(1_000)], [])]))
    tp = tdef_page(data, "X")
    struct.pack_into("<HII", data, tp * 4096 + 45, 0, 200, 300)  # num_cols, the logical count, the real count
    ctx, w = metered(bytes(data))
    assert not ctx.skipped and columns_line(section(w.out, "X")) == "Columns: ", ctx.skipped
    w.bounded(len(data))
    # the TDEF read alone: its loops within its own charge, which holds each block and each continuation page
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    before = db.meter.charged
    with Loops() as loops:
        td = jet._tdef(db, tp)
    spent = db.meter.charged - before
    assert len(td.pages) > 2 and len(td.real) == 300 and len(td.logical) == 200, len(td.pages)
    assert loops.n <= 2 * spent + 16 and spent >= jet._IDX * 500 + jet._PAGE * (len(td.pages) - 1), (loops.n, spent)


def test_usage_bitmap_charged():
    # a file of 2,000 more pages, and usage maps whose bytes name all of them: zero bytes, 0xFF bytes, and a type 1
    # map's type 5 page. Each byte scanned is charged first (one, and MAP_BITS more for a non-zero byte's bits)
    data = bytearray(jet_db(1) + bytes(4096 * 2_000))
    data[1_000 * 4096] = 5
    j = jet_pages.open(bytes(data))
    for m in (b"\x00" + bytes(4) + bytes(600), b"\x00" + bytes(4) + b"\xff" * 600,
              b"\x01" + struct.pack("<I", 1_000)):
        before = j.meter.charged
        with Loops() as loops:
            got = j.usage(m)
        assert loops.n <= 2 * (j.meter.charged - before) + 16, (loops.n, j.meter.charged - before)
        assert len(got) == (j.pages - 1 if m[5:6] == b"\xff" else 0), len(got)


def test_usage_walks_charged():
    # the re-review's probe (usage map walk), in memory: 60 tables whose maps all name the same 1,000 pages, none of
    # them owned by a table. Each page a map names is charged, so the walks stay within the meter's units ...
    data = jet_db(1, [Tbl(f"t{i}", [Col("ID", LONG)], []) for i in range(60)])
    tps = [tdef_page(data, f"t{i}") for i in range(60)]
    data = annex(data, tps, [page_of(0, []) for _ in range(1_000)])
    ctx, w = metered(data)
    assert len(sections(w.out)) == 60 and not ctx.skipped, ctx.skipped
    w.bounded(len(data))
    # ... and past a lowered total the table being read stops (one sheet_cut) and no later table is read: not its
    # TDEF, not its map (each counted db_table)
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data) + 20 * 1_000 * jet._PAGE
    try:
        with Count(jet_pages.Jet, "usage") as maps, Count(jet, "_tdef") as tdefs:
            ctx, w = metered(data)
    finally:
        limits.JET_WORK_TOTAL = old
    started = maps.calls - 2  # the catalog's own map is the first walked, MSysRelationships' the second
    assert 0 < started < 30 and tdefs.calls == maps.calls, (maps.calls, tdefs.calls)
    assert ctx.skipped == {"sheet_cut": 1, "db_table": 60 - started}, ctx.skipped
    assert len(sections(w.out)) == started and summary(w.out).endswith(": 60 tables."), summary(w.out)


def tiny_targets(npg, shown=False):
    """The re-review's probe (New 2): `npg` pages of a one-column table, each holding 256 two-byte live rows (column
    count 0, nothing shown) and 500 pointers naming rows of the next pages, every pointer a distinct target; with
    `shown` the column is Yes/No and each page's 200 live rows are three bytes with the bit set, so each target
    shows "Yes"."""
    data = jet_db(1, [Tbl("C", [Col("F", BOOL if shown else LONG)], [])])
    tp = tdef_page(data, "C")
    first = len(data) // 4096
    live, n = (struct.pack("<HB", 1, 1), 200) if shown else (struct.pack("<H", 0), 256)
    pages = []
    for g in range(npg):
        ptrs = [bytes([k % n]) + (first + (g + 1 + k // n) % npg).to_bytes(3, "little") for k in range(500)]
        pages.append(page_of(tp, [live] * n + ptrs, [0] * n + [0x4000] * 500))
    return annex(data, [tp], pages)


def test_target_room():
    # pointers naming distinct targets that show nothing: no target is kept, so the memory does not grow with them
    # (it grew about 18 times the file)
    small, big = tiny_targets(16), tiny_targets(64)
    (cs, ws), (cb, wb) = metered(small, True), metered(big, True)
    assert not cs.skipped and not cb.skipped, (cs.skipped, cb.skipped)
    assert wb.peak - ws.peak < len(big) - len(small) and wb.peak < 3 * len(big) + 2 ** 21, (ws.peak, wb.peak, len(big))
    wb.bounded(len(big))
    # targets that show a value are kept while the room of the file's bytes lasts; past it a repeat is read again,
    # charged, and shows the same
    data = tiny_targets(16, shown=True)
    kept = []
    orig = jet._Db.keep

    def keep(self, size):
        ok = orig(self, size)
        kept.append(size + jet._KEEP if ok else 0)
        return ok
    jet._Db.keep = keep
    try:
        ctx, w = metered(data, True)
    finally:
        jet._Db.keep = orig
    assert 0 < sum(kept) <= len(data) and kept.count(0) > 0, (sum(kept), len(data), kept.count(0))
    want = grid(section(w.out, "C"))
    orig_init = jet._Db.__init__

    def roomless(self, *a):
        orig_init(self, *a)
        self.room = 0
    jet._Db.__init__ = roomless
    try:
        ctx2, w2 = metered(data)
    finally:
        jet._Db.__init__ = orig_init
    assert grid(section(w2.out, "C")) == want and ctx2.skipped == ctx.skipped, (ctx.skipped, ctx2.skipped)
    w2.bounded(len(data))


def test_long_value_room():
    # 200 distinct definitions naming one chain of 40,000 characters, each read and decoded (the meter bounds that)
    # and shown cut at JET_CELL_CHARS: the texts kept for repeats stay within the room of the file's bytes (they were
    # kept until the total ran out, about ten times the file)
    data = shared_memo(200, 40_000, distinct=True)
    dbs = []
    orig = jet._Db.__init__

    def init(self, *a):
        orig(self, *a)
        dbs.append(self)
    jet._Db.__init__ = init
    try:
        ctx, w = metered(data, True)
    finally:
        jet._Db.__init__ = orig
    assert ctx.skipped["sheet_cut"] == 1 and ctx.skipped["text"] > 10, ctx.skipped
    kept = sum(sys.getsizeof(s) + len(k[0]) for k, (s, _, _) in dbs[-1].lval_cache.items())
    assert 0 < kept <= len(data), (kept, len(data))
    # beside the grid's own strings (each cut value 32,769 characters of two bytes, "…" being past Latin-1), the
    # peak stays within three times the file (it was about 14 times)
    shown = sum(sys.getsizeof(v) for row in grid(section(w.out, "M")) for v in row)
    assert w.peak <= shown + 3 * len(data) + 2 ** 18, (w.peak, shown, len(data))
    w.bounded(len(data))


def opened(data):
    """(ctx, Work, the Jet the parse opened) for one metered parse of `data`."""
    jets = []
    orig = jet_pages.open

    def capture(*a):
        jets.append(orig(*a))
        return jets[-1]
    jet_pages.open = capture
    try:
        ctx, w = metered(data, True)
    finally:
        jet_pages.open = orig
    return ctx, w, jets[-1]


def test_page_cache_bound():
    # a db_key file read once keeps its decoded pages: the cache holds at most the file's own bytes (each page once)
    # and at most JET_PAGE_CACHE pages. The pages are measured in the cache itself; the traced peak, which also
    # holds each cached page's object header (about 1.03 times the file) and the noise of the plain run beside it,
    # gets a quarter of the file and 64 KiB of room
    tbl = Tbl("P", [Col("ID", LONG), Col("N", TEXT)], [[i, "x" * 60] for i in range(4_000)])
    plain, coded = jet_db(1, [tbl]), jet_db(1, [tbl], db_key=0x1234)
    (cp, wp, _), (cc, wc, j) = opened(plain), opened(coded)
    assert grid(section(wp.out, "P")) == grid(section(wc.out, "P")) and cp.skipped == cc.skipped
    cached = sum(len(p) for p in j._cache.values())
    assert 0 < cached <= len(coded) - j.page_size and len(j._cache) <= limits.JET_PAGE_CACHE, (cached, len(coded))
    assert wc.peak - wp.peak <= 1.25 * len(coded) + 2 ** 16, (wp.peak, wc.peak, len(coded))
    wc.bounded(len(coded))
    # with the cap lowered under the file's pages the cache keeps that many, and the output is the same
    old = limits.JET_PAGE_CACHE
    limits.JET_PAGE_CACHE = 4
    try:
        cs, ws, j = opened(coded)
    finally:
        limits.JET_PAGE_CACHE = old
    assert len(j._cache) == 4 and grid(section(ws.out, "P")) == grid(section(wp.out, "P")), len(j._cache)


def guard_fixtures():
    """Clean files for the mutation guard: Jet3, Jet4 (also under a db_key) and ACE (type 1 maps), each with fixed
    and variable columns, Yes/No, long values (inline, one row, chained), deleted and overflow rows, a hidden table;
    a 240-column table; Jet4 and Jet3 tables with properties, hyperlinks, hidden columns, a primary key and
    relationships; and an ACE file with attachments (deflated and raw), a multi-value column, calculated columns,
    object names and a linked table."""
    cols = [Col("ID", LONG), Col("Name", TEXT), Col("Notes", MEMO), Col("Flag", BOOL), Col("Price", MONEY),
            Col("When", DATE), Col("Pic", OLE), Col("Key", GUID)]
    key = uuid.UUID(int=0x0123456789ABCDEF0123456789ABCDEF).bytes_le

    def rows(n):
        out = [[i, f"n{i}" * (i % 4), ("memo " * i)[:300] if i % 6 else "m" * (900 + 40 * i), i % 3 == 0, i * 125,
                45000.5 + i, b"x" * (i % 5) or None, key] for i in range(n)]
        out[3] = Row("deleted", out[3])
        out[5] = Row("overflow", out[5])
        out[7] = Row("overflow", out[7])
        return out
    other = Tbl("H", [Col("A", INT), Col("B", TEXT)], [[i, "b" * i] for i in range(12)], flags=0x08)
    wide = Tbl("W", [Col(f"w{i}", TEXT if i % 3 else BOOL) for i in range(240)],
               [[f"{r}" if i % 3 else i % 2 for i in range(240)] for r in range(16)])
    # properties (Percent, Yes/No, rich text, required), a hyperlink column, hidden columns, a primary key the rows
    # are sorted by and a relationship naming it
    keyed = [Col("ID", LONG, flags=0x05), Col("P", DOUBLE, props={"Format": "Percent", "Required": True}),
             Col("Y", INT, props={"Format": "Yes/No"}), Col("R", MEMO, props={"TextFormat": 1}),
             Col("U", MEMO, flags=0x80), Col("s_GUID", GUID), Col("X", TEXT, flags=0x10)]
    krows = [[(7 * i) % 13, i / 8, i % 2, f"<div><b>r{i}</b> {'x' * (i * 40)}</div>", f"site{i}#https://e.com/{i}#",
              str(uuid.UUID(int=i)), "x" * i] for i in range(13)]
    child = Tbl("C", [Col("ID", LONG), Col("KID", LONG)], [[i, i % 13] for i in range(20)], pk="ID")
    rels = [("CK", "C", "KID", "K", "ID"), ("KK", "K", "ID", "C", "ID")]
    cplx = Tbl("X", [Col("ID", LONG), Col("F", COMPLEX, attachments=True), Col("V", COMPLEX, multi=TEXT),
                     Col("C", LONG, calc=LONG, props={"Expression": "[ID]*2"}),
                     Col("E", TEXT, calc=TEXT, props={"Expression": "[ID]"})],
               [[i, [("a.png", PNG, i % 2 == 0), ("b.txt", b"x" * (i * 30), True)], [f"v{k}" for k in range(i % 4)],
                 2 * i, f"t{i}"] for i in range(8)], pk="ID")
    ace = jet_db(3, [cplx, other], objects=[("F", -32768), ("M", -32761)], linked=[("L", ";DATABASE=x;PWD=y", "T")])
    return [jet_db(0, [Tbl("T", cols, rows(14)), other]), jet_db(1, [Tbl("T", cols, rows(14)), other]),
            jet_db(1, [Tbl("T", cols, rows(10))], db_key=0x2468), jet_db(3, [Tbl("T", cols, rows(14), umap=1), other]),
            jet_db(1, [wide, Tbl("S", [Col("A", LONG)], [[i] for i in range(300)])]),
            jet_db(1, [Tbl("K", keyed, krows, pk="ID", props={"Description": "keyed"}), child], relationships=rels),
            jet_db(0, [Tbl("K", keyed[:4], [r[:4] for r in krows], pk="ID"), child], relationships=rels[:1]), ace]


def mutate(rng, data):
    """One in-memory mutation of a clean file: random bytes, a u16 or u32 set to an edge value, column blocks given
    one number or slot, a usage map's bytes set, a row turned into a pointer, a chunk copied elsewhere, a data page
    filled with deleted slots, or every column of a TDEF given one number and slot."""
    data = bytearray(data)
    ps = 2048 if data[0x14] == 0 else 4096
    pages = len(data) // ps
    pg = rng.randrange(1, pages)
    at = pg * ps + rng.randrange(ps)
    op = rng.randrange(8)
    if op == 6:  # every slot a page's offset table can hold, flagged deleted (or, at random, some not)
        pg = rng.choice([p for p in range(1, pages) if data[p * ps] == 1] or [pg])
        base = pg * ps + (8 if ps == 2048 else 12)
        n = (ps - (base - pg * ps) - 2) // 2
        data[base:base + 2] = n.to_bytes(2, "little")
        for r in range(n):
            flag = 0x8000 if rng.random() < 0.97 else rng.choice([0, 0x4000])
            data[base + 2 + 2 * r:base + 4 + 2 * r] = (flag | rng.randrange(ps)).to_bytes(2, "little")
        return bytes(data)
    if op == 7:  # one TDEF's column blocks all given one number and one var slot
        pg = rng.choice([p for p in range(2, pages) if data[p * ps] == 2] or [2])
        b = data[pg * ps:(pg + 1) * ps]
        j3 = ps == 2048
        ncols = int.from_bytes(b[25:27] if j3 else b[45:47], "little")
        nidx = int.from_bytes(b[31:35] if j3 else b[51:55], "little")
        size, num, var = (18, 1, 3) if j3 else (25, 5, 7)
        col_at = (43 if j3 else 63) + nidx * (8 if j3 else 12)
        v, s = rng.randrange(0, 9).to_bytes(2, "little"), rng.randrange(0, 3).to_bytes(2, "little")
        for o in range(pg * ps + col_at, min(pg * ps + col_at + size * ncols, (pg + 1) * ps - size), size):
            data[o + num:o + num + 2], data[o + var:o + var + 2] = v, s
        return bytes(data)
    if op == 0:
        for _ in range(rng.randrange(1, 9)):
            data[pg * ps + rng.randrange(ps)] = rng.randrange(256)
    elif op == 1:
        v = rng.choice([0, 1, 2, 0xFF, 0xFFFF, 0x7FFF, 0x4000 | rng.randrange(ps), 0x8000, rng.randrange(65536)])
        n = rng.choice([2, 4])
        at = min(at, len(data) - n)
        data[at:at + n] = (v if n == 2 else v * 0x10001).to_bytes(n, "little")
    elif op == 2:
        pg = rng.choice([p for p in range(2, pages) if data[p * ps] == 2] or [2])
        size, num, var = (18, 1, 3) if ps == 2048 else (25, 5, 7)
        start = pg * ps + rng.randrange(40, 120)
        v = rng.randrange(0, 12).to_bytes(2, "little")
        for o in range(start, min(start + size * rng.randrange(2, 30), (pg + 1) * ps - size), size):
            data[o + rng.choice([num, var]):o + rng.choice([num, var]) + 2] = v
    elif op == 3:
        n = rng.randrange(1, 64)
        data[ps + rng.randrange(ps - n):][:n] = b"\xff" * n
    elif op == 4:
        b = ps * pg
        if data[b] == 1:
            base = 8 if ps == 2048 else 12
            nrows = int.from_bytes(data[b + base:b + base + 2], "little")
            if nrows:
                r = rng.randrange(min(nrows, (ps - base - 2) // 2))
                loc = int.from_bytes(data[b + base + 2 + 2 * r:b + base + 4 + 2 * r], "little") & 0x1FFF
                data[b + base + 2 + 2 * r:b + base + 4 + 2 * r] = (loc | 0x4000).to_bytes(2, "little")
                if loc + 4 <= ps:
                    data[b + loc:b + loc + 4] = bytes([rng.randrange(8)]) + rng.randrange(pages).to_bytes(3, "little")
    else:
        n = rng.randrange(8, 200)
        src, dst = rng.randrange(len(data) - n), rng.randrange(ps, len(data) - n)
        data[dst:dst + n] = data[src:src + n]
    return bytes(data)


def test_mutation_guard():
    # a structural backstop: 400 seeded in-memory mutations of the clean files (never written to disk), each parsed
    # with the default total or a small one. What it checks: each parse ends (a Doc or a ReadError, nothing else
    # escapes, the meter's own Spent included); the loop iterations of the whole parse stay within twice the units
    # charged, twice the grid cells put and a constant; the units within the file's total; the tracemalloc peak
    # within 4 times the file, 1 KiB a grid cell put (the document's own cells, which the grid's caps bound) and
    # 1.5 MiB. The loop bound is file-wide, so one loop left uncharged is diluted by every other charge and passes
    # here; each loop's own tick is pinned by its per-path test (test_columns_charged, test_index_blocks_charged,
    # test_usage_*, test_props_charged, test_relationships_charged, ...). A cache growing past the file does fail here
    rng = random.Random(20261116)
    clean = guard_fixtures()
    old = limits.JET_WORK_TOTAL
    try:
        for k in range(400):
            src = clean[k % len(clean)]
            data = mutate(rng, src)
            limits.JET_WORK_TOTAL = old if rng.random() < 0.5 else rng.randrange(200, 40_000)
            try:
                ctx, w = metered(data, trace=k % 4 == 0)
            except Exception as e:
                raise AssertionError(f"mutation {k}: {type(e).__name__}: {e}") from e
            assert isinstance(w.out, (jet.Doc, ReadError)), (k, w.out)
            try:
                w.bounded(len(data))
                assert w.peak <= 4 * len(data) + 1024 * w.puts + 3 * 2 ** 19, (w.peak, len(data), w.puts)
            except AssertionError as e:
                raise AssertionError(f"mutation {k}: {e}") from None
    finally:
        limits.JET_WORK_TOTAL = old


# -- long values, properties, formats, hyperlinks, hidden columns, key order, relationships

def lval_pages(data):
    """The LVAL pages of a file, ascending (data pages whose owner field reads "LVAL")."""
    j = jet_pages.open(data)
    return [n for n in range(1, j.pages) if j.page(n)[0] == 1 and j.page(n)[4:8] == b"LVAL"]


def cell_inlines(sec, r, c):
    """The inlines of grid cell (r, c) of a table section (row 0 the header)."""
    table = next(b for b in sec.blocks if isinstance(b, Table))
    return [x for p in table.rows[r][c].blocks for x in p.inlines]


def test_lval_forms():
    # a memo of each form, in Jet3 and Jet4: inline (0x80), one LVAL row (0x40) and a chain of three LVAL pages
    # (0x00); each reads back whole, nothing counted
    for v, chained in ((0, "é" * 5_000), (1, "한" * 5_000)):
        texts = ["inline memo", "o" * 1_500, chained]
        data = jet_db(v, [Tbl("L", [Col("ID", LONG), Col("Notes", MEMO)], [[i, t] for i, t in enumerate(texts)])])
        assert len(lval_pages(data)) == 4, (v, lval_pages(data))  # one for the single row, three for the chain
        doc, ctx, _ = convert(data)
        assert grid(section(doc, "L"))[1:] == [[str(i), t] for i, t in enumerate(texts)] and not ctx.skipped, v


def test_lval_cycle():
    # a three-page chain whose second row names the first page again: the walk stops at the repeat (its seen-set),
    # the text read so far is shown, counted `record` once (shorter than its length); a row naming itself the same
    text = "한" * 5_000
    clean = jet_db(1, [Tbl("L", [Col("ID", LONG), Col("Notes", MEMO)], [[1, text]])])
    first, second, _ = lval_pages(clean)
    chunk = (4076 - 4) // 2  # the characters one chain row holds
    for pg, want in ((second, 2 * chunk), (first, chunk)):
        data = bytearray(clean)
        at = pg * 4096 + (struct.unpack_from("<H", data, pg * 4096 + 14)[0] & 0x1FFF)
        data[at:at + 4] = b"\x00" + first.to_bytes(3, "little")
        with Count(jet._Db, "row_at") as rows:
            doc, ctx, _ = convert(bytes(data))
        assert grid(section(doc, "L"))[1] == ["1", "한" * want] and ctx.skipped == {"record": 1}, ctx.skipped
        assert rows.calls == want // chunk, rows.calls


def test_lval_cap():
    # JET_LVAL_BYTES lowered to 100: a 502-byte value (FF FE and 500 compressed a's) reads 100 bytes and is cut
    old = jet.JET_LVAL_BYTES
    jet.JET_LVAL_BYTES = 100
    try:
        with Count(jet._Db, "row_at") as rows:
            doc, ctx, _ = convert(jet_db(1, [Tbl("L", [Col("ID", LONG), Col("Notes", MEMO)],
                                                 [[1, "a" * 500], [2, "short"]])]))
    finally:
        jet.JET_LVAL_BYTES = old
    assert grid(section(doc, "L"))[1:] == [["1", "a" * 98 + "…"], ["2", "short"]] and ctx.skipped == {"text": 1}
    assert rows.bytes <= 502 and rows.calls == 1, (rows.bytes, rows.calls)


def test_ole_and_binary():
    # OLE (one LVAL row, and a chain) shows its length, counted `object`, and no LVAL row is read; binary its length
    cols = [Col("ID", LONG), Col("Pic", OLE), Col("Bin", BINARY)]
    data = jet_db(1, [Tbl("O", cols, [[1, bytes(range(256)) * 8, b"\x01\x02\x03"], [2, b"z" * 10_000, None]])])
    assert lval_pages(data), "the OLE values are long values"
    with Count(jet._Db, "row_at") as rows:
        doc, ctx, _ = convert(data)
    assert grid(section(doc, "O"))[1:] == [["1", "<OLE 2048 bytes>", "<binary 3 bytes>"],
                                           ["2", "<OLE 10000 bytes>", ""]] and ctx.skipped == {"object": 2}
    assert rows.calls == 0, rows.calls


def test_percent_and_yes_no():
    # Format Percent: x 100 with "%", a trailing ".0" dropped (Double, Single, Long); Yes/No on an Integer; any other
    # format (Currency here) is not applied; a column without properties is as before
    cols = [Col("ID", LONG), Col("P", DOUBLE, props={"Format": "Percent"}), Col("S", FLOAT, props={"Format": "Percent"}),
            Col("Y", INT, props={"Format": "Yes/No"}), Col("C", MONEY, props={"Format": "Currency"}),
            Col("N", LONG, props={"Format": "Percent"}), Col("D", DOUBLE)]
    rows = [[1, 0.25, 0.5, -1, 15000, 3, 0.25], [2, 0.125, 0.1, 0, 10000, -1, 0.125], [3] + [None] * 6]
    for v in (0, 1, 3):
        doc, ctx, _ = convert(jet_db(v, [Tbl("F", cols, rows)]))
        assert grid(section(doc, "F"))[1:] == [["1", "25%", "50%", "Yes", "1.5", "300%", "0.25"],
                                               ["2", "12.5%", "10%", "No", "1", "-100%", "0.125"],
                                               ["3", "", "", "", "", "", ""]], v
        assert not ctx.skipped, ctx.skipped


def test_rich_text():
    # TextFormat 1: the text of the HTML; a memo without it keeps its tags
    cols = [Col("ID", LONG), Col("Notes", MEMO, props={"TextFormat": 1}), Col("Plain", MEMO)]
    rows = [[1, "<div><b>Hi</b> there</div>", "<b>kept</b>"], [2, "<div>" + "y" * 3_000 + "</div>", None]]
    data = jet_db(1, [Tbl("R", cols, rows)])
    doc, ctx, _ = convert(data)
    sec = section(doc, "R")
    assert columns_line(sec) == "Columns: ID (Long) · Notes (Memo, rich text) · Plain (Memo)"
    assert grid(sec)[1:] == [["1", "Hi there", "<b>kept</b>"], ["2", "y" * 3_000, ""]] and not ctx.skipped
    # the HTML parser is fed at most the capped text; a cut value ends with "…", counted `text` once
    old = jet.JET_CELL_CHARS
    jet.JET_CELL_CHARS = 1_000
    try:
        with Count(html, "tree") as trees:
            doc, ctx, _ = convert(data)
    finally:
        jet.JET_CELL_CHARS = old
    assert grid(section(doc, "R"))[2] == ["2", "y" * (1_000 - len("<div>")) + "…", ""] and ctx.skipped == {"text": 1}
    assert trees.calls == 2 and trees.given <= 1_000 + len(rows[0][1]), (trees.calls, trees.given)


def test_hyperlinks():
    # a hyperlink column (Memo, flag 0x80): display#address#subaddress#tip. http, https, mailto and ftp addresses
    # are Links (the label the display, or the address); anything else, or an address holding a space, is
    # "display (address)"
    vals = ["Site#https://example.com/#", "Doc#file.docx#", "#mailto:a@example.com#", "Space#http://a b#",
            "Sub#https://e.com/p#top#tip", "Plain", "#ftp://f.example/x#"]
    cols = [Col("ID", LONG), Col("Link", MEMO, flags=0x80), Col("Text", MEMO)]
    doc, ctx, md = convert(jet_db(1, [Tbl("H", cols, [[i, v, v] for i, v in enumerate(vals)])]))
    sec = section(doc, "H")
    assert cell_inlines(sec, 1, 1) == [Link("https://example.com/", [Text("Site")])]
    assert grid(sec)[2][1] == "Doc (file.docx)"
    assert cell_inlines(sec, 3, 1) == [Link("mailto:a@example.com", [Text("mailto:a@example.com")])]
    assert grid(sec)[4][1] == "Space (http://a b)"
    assert cell_inlines(sec, 5, 1) == [Link("https://e.com/p#top", [Text("Sub")])]
    assert grid(sec)[6][1] == "Plain"
    assert cell_inlines(sec, 7, 1) == [Link("ftp://f.example/x", [Text("ftp://f.example/x")])]
    assert [r[2] for r in grid(sec)[1:]] == vals  # the same text in a plain memo
    assert "[Site](https://example.com/)" in md and not ctx.skipped, ctx.skipped


def orders_and_customers():
    orders = Tbl("Orders", [Col("OrderID", LONG, flags=0x05), Col("CustomerID", LONG), Col("OrderDate", DATE),
                            Col("Total", MONEY, props={"Required": True}),
                            Col("Notes", MEMO, props={"TextFormat": 1, "Description": "free text"})],
                 [[1, 7, 45000.0, 10000, "<p>n</p>"]], pk="OrderID")
    customers = Tbl("Customers", [Col("ID", LONG, flags=0x05), Col("Name", TEXT)], [[7, "Ann"]], pk="ID")
    rels = [("CustomersOrders", "Orders", "CustomerID", "Customers", "ID"),
            ("Gone", "Orders", "OrderDate", "Nowhere", "X")]  # a table the catalog does not hold: left out
    return [orders, customers], rels


def test_schema_line():
    # the spec's example: type, PK, autonumber, the relationship's target, required, rich text
    tables, rels = orders_and_customers()
    for v in (0, 1, 3):
        doc, ctx, md = convert(jet_db(v, tables, relationships=rels))
        assert columns_line(section(doc, "Orders")) == (
            "Columns: OrderID (Long, PK, autonumber) · CustomerID (Long → Customers.ID) · OrderDate (Date) · "
            "Total (Currency, required) · Notes (Memo, rich text)"), v
        assert columns_line(section(doc, "Customers")) == "Columns: ID (Long, PK, autonumber) · Name (Text)"
        assert grid(section(doc, "Orders"))[1] == ["1", "7", "2023-03-15", "1", "n"]
        assert not ctx.skipped and "MSysRelationships" not in md and "Columns: OrderID" in md, ctx.skipped


def test_hidden_columns():
    # a replication column (s_GUID), a Gen_ column naming a column of its table (Gen_Notes) and a column flagged
    # 0x10: Hidden inlines in the schema line (each holding its separator, so the visible line reads right) and
    # hidden columns of the grid. A user column that only looks like one (s_total, Gen_Other with no Other) is shown
    guid = "01234567-89ab-cdef-0123-456789abcdef"
    cols = [Col("ID", LONG), Col("s_GUID", GUID), Col("Secret", TEXT, flags=0x10), Col("Name", TEXT),
            Col("Gen_Notes", BINARY), Col("Notes", MEMO), Col("s_total", LONG), Col("Gen_Other", TEXT)]
    data = jet_db(1, [Tbl("H", cols, [[1, guid, "classified", "Ann", b"\x01", "a note", 12, "gen"]])])
    doc, ctx, md = convert(data)
    sec = section(doc, "H")
    line = next(b for b in sec.blocks if isinstance(b, Para))
    assert columns_line(sec) == ("Columns: ID (Long) · Name (Text) · Notes (Memo) · s_total (Long) · "
                                 "Gen_Other (Text)"), columns_line(sec)
    hidden = ["".join(t.text for t in x.inlines) for x in line.inlines if isinstance(x, Hidden)]
    assert hidden == [" · s_GUID (GUID)", " · Secret (Text)", " · Gen_Notes (Binary)"], hidden
    table = next(b for b in sec.blocks if isinstance(b, Table))
    assert table.hidden_cols == frozenset({1, 2, 4}) and grid(sec)[1][2] == "classified"
    assert "classified" not in md and "Secret" not in md, md
    assert "s_GUID" not in md and "Gen_Notes" not in md and not ctx.skipped
    assert grid(sec)[1][5:] == ["a note", "12", "gen"] and "|12|gen|" in md, md
    _, _, shown = convert(data, hidden=True)
    assert "classified" in shown and "s_GUID (GUID)" in shown and "Gen_Notes" in shown
    # the names are matched without case; flag 0x10 hides any column
    cols = [Col("ID", LONG), Col("S_LINEAGE", BINARY), Col("gen_notes", BINARY), Col("NOTES", MEMO),
            Col("Plain", TEXT, flags=0x10), Col("s_ColLineage", BINARY), Col("s_Generation", LONG)]
    doc, _, _ = convert(jet_db(1, [Tbl("C", cols, [[1, b"\x01", b"\x02", "n", "p", b"\x03", 4]])]))
    table = next(b for b in section(doc, "C").blocks if isinstance(b, Table))
    assert table.hidden_cols == frozenset({1, 2, 4, 5, 6}), table.hidden_cols
    # a hidden first column carries the separator after it
    doc, _, md = convert(jet_db(1, [Tbl("F", [Col("s_Lineage", BINARY), Col("A", LONG)], [[b"\x01", 5]])]))
    line = next(b for b in section(doc, "F").blocks if isinstance(b, Para))
    assert [x.inlines[0].text for x in line.inlines if isinstance(x, Hidden)] == ["s_Lineage (Binary) · "]
    assert "Columns: A (Long)\n" in md, md


def test_pk_order():
    # rows written 3, 1, 2 (the 1 an overflow row) under the primary key ID are shown 1, 2, 3
    rows = [[3, "c"], Row("overflow", [1, "a"]), [2, "b"]]
    for v in (0, 1):
        doc, ctx, _ = convert(jet_db(v, [Tbl("T", [Col("ID", LONG), Col("Name", TEXT)], rows, pk="ID")]))
        assert grid(section(doc, "T"))[1:] == [["1", "a"], ["2", "b"], ["3", "c"]] and not ctx.skipped, v
    # a two-column key: typed (10 after 2), None first, text without case; ties keep their physical order
    rows = [[2, "b"], [10, "a"], [1, "z"], [1, None], [2, "B"]]
    doc, ctx, _ = convert(jet_db(1, [Tbl("T", [Col("ID", LONG), Col("Name", TEXT)], rows, pk=["ID", "Name"])]))
    assert grid(section(doc, "T"))[1:] == [["1", ""], ["1", "z"], ["2", "b"], ["2", "B"], ["10", "a"]]
    # no primary key: physical order
    doc, ctx, _ = convert(jet_db(1, [Tbl("T", [Col("ID", LONG)], [[3], [1], [2]])]))
    assert grid(section(doc, "T"))[1:] == [["3"], ["1"], ["2"]]


def test_pk_sort_only_within_caps():
    tbl = Tbl("T", [Col("ID", LONG), Col("Name", TEXT)], [[3, "c"], [1, "a"], [2, "b"]], pk="ID")
    data = jet_db(1, [tbl])
    old = limits.SHEET_ROWS, limits.SHEET_CELLS
    try:
        limits.SHEET_ROWS = 4  # the header and three rows fit: sorted
        doc, ctx, _ = convert(data)
        assert [r[0] for r in grid(section(doc, "T"))[1:]] == ["1", "2", "3"] and not ctx.skipped
        limits.SHEET_ROWS = 3  # they do not: physical order, cut at the rows
        doc, ctx, _ = convert(data)
        assert grid(section(doc, "T"))[1:] == [["3", "c"], ["1", "a"]] and ctx.skipped == {"sheet_cut": 1}
        limits.SHEET_ROWS = old[0]
        limits.SHEET_CELLS = 7  # the rows' six cells do not fit the five the header leaves: physical order
        doc, ctx, _ = convert(data)
        assert [r[0] for r in grid(section(doc, "T"))[1:]] == ["3", "1", "2"] and ctx.skipped == {"sheet_cut": 1}
    finally:
        limits.SHEET_ROWS, limits.SHEET_CELLS = old
    # a sort the meter cannot afford leaves the rows in physical order (no count; the table is read whole)
    orig = jet_pages.Meter.afford
    jet_pages.Meter.afford = lambda self, n: False
    try:
        doc, ctx, _ = convert(data)
    finally:
        jet_pages.Meter.afford = orig
    assert [r[0] for r in grid(section(doc, "T"))[1:]] == ["3", "1", "2"] and not ctx.skipped


def test_budget_shared_across_tables():
    # one sheet_grid.Budget for the document: table A uses SHEET_CELLS up, so table B is cut at its first cell;
    # sheet_cut counts once per cut table, and both headings stay
    a = Tbl("A", [Col("ID", LONG), Col("N", TEXT)], [[i, f"a{i}"] for i in (2, 0, 1)], pk="ID")
    b = Tbl("B", [Col("ID", LONG), Col("N", TEXT)], [[i, f"b{i}"] for i in range(3)])
    old = limits.SHEET_CELLS
    try:
        limits.SHEET_CELLS = 8  # A's header and three rows, exactly
        doc, ctx, md = convert(jet_db(1, [a, b]))
        assert grid(section(doc, "A"))[1:] == [["0", "a0"], ["1", "a1"], ["2", "a2"]]
        assert ctx.skipped == {"sheet_cut": 1} and not any(isinstance(x, Table) for x in section(doc, "B").blocks)
        assert "## Table: A (3 rows)" in md and "## Table: B (3 rows)" in md
        limits.SHEET_CELLS = 6  # both cut, one count each
        doc, ctx, md = convert(jet_db(1, [a, b]))
        assert ctx.skipped == {"sheet_cut": 2} and len(ctx.warnings) == 2, (ctx.skipped, ctx.warnings)
        assert "## Table: A (3 rows)" in md and "## Table: B (3 rows)" in md
    finally:
        limits.SHEET_CELLS = old


def test_props_caps():
    tbl = Tbl("F", [Col("ID", LONG), Col("P", DOUBLE, props={"Format": "Percent"})], [[1, 0.25]])
    for v, head in ((0, b"KKD\x00"), (1, b"MR2\x00")):
        data = jet_db(v, [tbl])
        with Count(jet, "_props") as parsed:
            doc, ctx, _ = convert(data)
        assert grid(section(doc, "F"))[1] == ["1", "25%"] and not ctx.skipped and parsed.calls == 1, v
        old = jet.JET_PROP_CHUNKS, jet.JET_PROPS
        try:
            jet.JET_PROP_CHUNKS = 2  # the name list and the column's map
            assert grid(section(convert(data)[0], "F"))[1] == ["1", "25%"]
            jet.JET_PROP_CHUNKS = 1  # past it: the properties are ignored, counted `record`
            doc, ctx, _ = convert(data)
            assert grid(section(doc, "F"))[1] == ["1", "0.25"] and ctx.skipped == {"record": 1}, ctx.skipped
            jet.JET_PROP_CHUNKS = old[0]
            jet.JET_PROPS = 20  # a blob longer than JET_PROPS is not read
            with Count(jet, "_props") as parsed:
                doc, ctx, _ = convert(data)
            assert grid(section(doc, "F"))[1] == ["1", "0.25"] and ctx.skipped == {"record": 1}
            assert parsed.calls == 0
        finally:
            jet.JET_PROP_CHUNKS, jet.JET_PROPS = old
        # a blob that is not a property blob: ignored, counted `record`
        assert data.count(head) == 1
        doc, ctx, _ = convert(data.replace(head, b"XYZ\x00"))
        assert grid(section(doc, "F"))[1] == ["1", "0.25"] and ctx.skipped == {"record": 1}, ctx.skipped


def prop_blob(names, values, chunks):
    """A crafted Jet4 property blob: a name list of `names` empty names, a column map whose `values` entries each
    name name 0 with no data, then `chunks` empty index maps (type 2)."""
    def chunk(kind, body):
        return struct.pack("<IH", 6 + len(body), kind) + body
    name = struct.pack("<H", 2) + "c".encode("utf-16-le")
    out = b"MR2\x00" + chunk(0x80, struct.pack("<H", 0) * names)
    out += chunk(1, struct.pack("<I", 4 + len(name)) + name + struct.pack("<HBBHH", 8, 0, TEXT, 0, 0) * values)
    return out + chunk(2, b"") * chunks


def test_props_charged():
    # each chunk, name and value of a blob is charged before it is read: the loops stay within the charge
    db = jet._Db(jet_pages.open(jet_db(1)), Context())
    blob = prop_blob(3_000, 3_000, 3_000)
    before = db.meter.charged
    with Loops() as loops:
        out = jet._props(db, blob)
    spent = db.meter.charged - before
    assert out == {"c": {}}, out
    assert loops.n <= 2 * spent + 16 and spent >= jet._PROP * (3_000 + 3_000 + 3_002), (loops.n, spent)
    # a name's bytes are charged before they are decoded: 1,000 names of 100 characters (200 bytes each)
    name = "n" * 100
    names = b"".join(struct.pack("<H", 200) + name.encode("utf-16-le") for _ in range(1_000))
    blob = b"MR2\x00" + struct.pack("<IH", 6 + len(names), 0x80) + names
    before = db.meter.charged
    assert jet._props(db, blob) == {}
    assert db.meter.charged - before >= jet._PROP * 1_000 + 200 * 1_000, db.meter.charged - before
    # a table's blob is read once, its LVAL rows charged as a long value's
    data = jet_db(1, [Tbl("F", [Col("A", DOUBLE, props={"Format": "Percent", "Description": "d" * 3_000})], [[1.0]])])
    assert lval_pages(data)
    ctx, w = metered(data)
    assert grid(section(w.out, "F"))[1] == ["100%"] and not ctx.skipped
    w.bounded(len(data))


def test_relationships_charged():
    # 2,000 MSysRelationships rows naming one column: each row is charged (a row, its cells, its text) before it is
    # read, and the target is kept once
    data = jet_db(1, [Tbl("A", [Col("ID", LONG)], [[1]])],
                  relationships=[(f"r{i}", "A", "ID", "A", "ID") for i in range(2_000)])
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    before = db.meter.charged
    with Loops() as loops:
        rels = jet._relationships(db, objs)
    spent = db.meter.charged - before
    assert rels == {("a", "id"): {"A.ID": None}}, rels
    assert loops.n <= 2 * spent + 16 and spent >= 2_000 * (jet._ROW + 4 * jet._CELL), (loops.n, spent)
    doc, ctx, _ = convert(data)
    assert columns_line(section(doc, "A")) == "Columns: ID (Long → A.ID)" and not ctx.skipped
    # no room left for kept values: no target is kept, counted `record` once
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    db.room = 0
    assert jet._relationships(db, objs) == {} and db.ctx.skipped == {"record": 1}, db.ctx.skipped
    # the meter spent inside them: the rest is ignored, counted `record` once, and no table is read
    old = limits.JET_WORK_TOTAL
    limits.JET_WORK_TOTAL = catalog_spend(data, rels=False) + 5_000
    try:
        doc, ctx, _ = convert(data)
    finally:
        limits.JET_WORK_TOTAL = old
    assert ctx.skipped == {"record": 1, "db_table": 1}, ctx.skipped


def test_schema_targets_charged():
    # one column naming 300 tables: each target in the schema line is charged before it is written
    tables = [Tbl("A", [Col("ID", LONG)], [[1]])] + [Tbl(f"T{i}", [Col("ID", LONG)], []) for i in range(300)]
    data = jet_db(1, tables, relationships=[(f"r{i}", "A", "ID", f"T{i}", "ID") for i in range(300)])
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    rels = jet._relationships(db, objs)
    tdef = jet._tdef(db, tdef_page(data, "A"))
    before = db.meter.charged
    with Loops() as loops:
        line = jet._schema(db, tdef, "A", rels)
    spent = db.meter.charged - before
    assert "".join(x.text for x in line).count(" → T") == 300
    assert loops.n <= 2 * spent + 16 and spent >= 300 * jet._REF, (loops.n, spent)


def test_text_forms_charged():
    # rich text is charged _HTML a character fed to the parser, a hyperlink _LINK a character it splits, the key of
    # a sorted row _KEY a key column, and the sort _SORT a comparison (n times the bits of n), each before its work
    cols = [Col("ID", LONG), Col("R", MEMO, props={"TextFormat": 1}), Col("U", MEMO, flags=0x80)]
    data = jet_db(1, [Tbl("X", cols, [[1, "<b>bold</b>", "a#http://b/#"]])])
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    tdef = jet._tdef(db, tdef_page(data, "X"))
    jet._apply_props(db, tdef, db.props_of(next(o for o in objs if o.name == "X")))
    (_, cells), = list(jet._rows(db, tdef))
    for i, text, want, extra in ((1, "<b>bold</b>", "bold", jet._HTML), (2, "a#http://b/#", None, jet._LINK)):
        before = db.spent
        got = jet.value(db, tdef.columns[i], cells[i])
        assert db.spent - before == 2 + len(text) + extra * len(text), (i, db.spent - before)
        assert got == want or got == [Link("http://b/", [Text("a")])], got
    # the sort: the same 300 rows with and without a primary key differ by the key's index blocks, each row's key
    # and the comparisons
    rows = [[(7 * i) % 300] for i in range(300)]
    plain = metered(jet_db(1, [Tbl("S", [Col("ID", LONG)], rows)]))[1].charged
    keyed = metered(jet_db(1, [Tbl("S", [Col("ID", LONG)], rows, pk="ID")]))[1].charged
    assert keyed - plain == 2 * jet._IDX + 300 * jet._KEY + jet._SORT * 300 * (300).bit_length(), keyed - plain
    # a text key is charged a unit a character as well (its casefold, and its share of the comparisons)
    rows = [[f"k{(7 * i) % 300}"] for i in range(300)]
    plain = metered(jet_db(1, [Tbl("S", [Col("K", TEXT)], rows)]))[1].charged
    keyed = metered(jet_db(1, [Tbl("S", [Col("K", TEXT)], rows, pk="K")]))[1].charged
    chars = sum(len(r[0]) for r in rows)
    assert keyed - plain == 2 * jet._IDX + 300 * jet._KEY + chars + jet._SORT * 300 * (300).bit_length(), keyed - plain


# -- linked tables, object names, complex columns, attachments, calculated columns, page type 8

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
PNG2 = b"\x89PNG\r\n\x1a\n" + bytes(range(100, 160))


def images(sec):
    return [b for b in sec.blocks if isinstance(b, Image)]


def test_object_names():
    objs = [("Orders", -32768), ("Customers", -32768), ("Sales", -32764), ("AutoExec", -32766), ("Utils", -32761)]
    for v in (0, 1, 3):
        doc, ctx, md = convert(jet_db(v, [three()], objects=objs))
        lines = summary_lines(doc)
        assert lines[1] == "Forms: Orders, Customers. Reports: Sales. Macros: AutoExec. Modules: Utils (names only).", \
            lines
        assert len(lines) == 2 and ctx.skipped == {"vba": 1}, (lines, ctx.skipped)
        assert "VBA macros" in markdown.unread(ctx.skipped)
        assert "Forms: Orders, Customers. Reports: Sales." in md and "## Table: Orders (3 rows)" in md
    # each part only when it has names; no module, no VBA
    doc, ctx, _ = convert(jet_db(1, [three()], objects=[("Sales", -32764), ("Q4", -32764)]))
    assert summary_lines(doc)[1:] == ["Reports: Sales, Q4 (names only)."] and not ctx.skipped
    doc, ctx, _ = convert(jet_db(1, [three()]))
    assert summary_lines(doc) == ["Access 2000-2003 database (Jet 4): 1 table."] and not ctx.skipped
    # Access's temporary objects (names starting "~", hidden in its navigation pane) are left out
    doc, ctx, _ = convert(jet_db(1, [three()], objects=[("~TMPCLP1", -32766), ("F", -32768), ("~m", -32761)]))
    assert summary_lines(doc)[1:] == ["Forms: F (names only)."] and not ctx.skipped, (summary_lines(doc), ctx.skipped)


def test_linked_table():
    linked = [("Prices", ";DATABASE=C:\\x\\prices.accdb;PWD=hunter2;UID=bob", "Prices")]
    data = jet_db(1, [three()], linked=linked)
    doc, ctx, md = convert(data)
    lines = summary_lines(doc)
    assert lines == ["Access 2000-2003 database (Jet 4): 1 table, 1 linked table.",
                     "Linked tables: Prices → C:\\x\\prices.accdb (Prices)"], lines
    assert "hunter2" not in md and "bob" not in md and "hunter2" not in repr(doc) and "bob" not in repr(doc)
    assert ctx.skipped == {"linked_data": 1} and "linked tables' data (in other files)" in markdown.unread(ctx.skipped)
    # what the catalog keeps holds no credentials either
    db = jet._Db(jet_pages.open(data), Context())
    kept = repr(jet._catalog(db))
    assert "hunter2" not in kept and "bob" not in kept and "prices.accdb" in kept
    # mixed case parts; an ODBC string's DATABASE; one with no DATABASE keeps the rest of the string; an Access link
    # whose path is in the Database column (connect empty); several joined with "; "
    linked = [("Orders", "ODBC;DSN=Sales;pwd=Secret1;Uid=Zqx1;DATABASE=sales;APP=x", "dbo.Orders", 4),
              ("Stock", "ODBC;DSN=Inv;pWd=Secret2;uID=Zqx2;Password=Secret3;Trusted_Connection=No", "dbo.Stock", 4),
              ("T2", "", "Table1", 6, "Z:\\db\\other.accdb")]
    doc, ctx, md = convert(jet_db(3, [three()], linked=linked))
    assert summary_lines(doc)[1] == ("Linked tables: Orders → sales (dbo.Orders); Stock → ODBC;DSN=Inv;"
                                     "Trusted_Connection=No (dbo.Stock); T2 → Z:\\db\\other.accdb (Table1)"), \
        summary_lines(doc)
    for secret in ("Secret1", "Secret2", "Secret3", "Zqx1", "Zqx2", "pwd", "pWd", "Uid", "uID"):
        assert secret not in md and secret not in repr(doc), secret
    # forms and linked tables together: line 2, then line 3
    doc, _, _ = convert(jet_db(1, [three()], objects=[("F", -32768)], linked=linked[2:]))
    assert summary_lines(doc)[1:] == ["Forms: F (names only).", "Linked tables: T2 → Z:\\db\\other.accdb (Table1)"]


def test_linked_credentials():
    # no credential reaches the line, however the string is written: ODBC braces with }} escapes, quotes, keys with
    # spaces, case, other secret keys, a Database cell or DATABASE value holding one, a URL's user part; a string
    # that cannot be parsed cleanly shows only the file name of a DATABASE= read before the fault, or nothing
    db = jet._Db(jet_pages.open(jet_db(1)), Context())
    cases = [
        ("ODBC;DRIVER={SQL Server};SERVER=s;UID=u;PWD={pa}}ss;wo}rd};APP=x", "", ""),
        ("ODBC;DRIVER={SQL Server};SERVER=s;UID=u;PWD={pa}}ss;wo}}rd};APP=x", "",
         "ODBC;DRIVER={SQL Server};SERVER=s;APP=x"),
        ("ODBC;DATABASE=C:\\d\\a.mdb;PWD={x;s3cret", "", "a.mdb"),
        ("MS Access;Jet OLEDB:Database Password=s3cret", "", "MS Access"),
        ("ODBC;DSN=x;PASS=s3cret", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x; P W D =s3cret", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x;PWD=\"a;s3cret\"", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x;PWD='a;s3cret';APP=y", "", "ODBC;DSN=x;APP=y"),
        ("ODBC;DSN=x;PWD=\"a;s3cret", "", ""),
        ("MS Access;PWD=s3cret", "C:\\a.accdb;PWD=s3cret", "MS Access"),
        ("MS Access;PWD=s3cret", "C:\\a.accdb", "C:\\a.accdb"),
        ("ODBC;DATABASE={C:\\a;PWD=s3cret}", "", "ODBC"),
        ("ODBC;DATABASE=https://u:s3cret@h/x", "", "https://h/x"),
        ("ODBC;DSN=x;PWD\u00a0=s3cret", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x;\uff30\uff37\uff24=s3cret", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x;\tPWD=s3cret", "", "ODBC;DSN=x"),
        ("ODBC;DSN=x\nPWD=s3cret", "", "ODBC"),
        ("ODBC;DATABASE=;PWD=s3cret", "", "ODBC"),
        (";PWD=s3cret", "", ""),
        ("ODBC;DSN=x;Trusted_Connection=Yes;Token=s3cret;AccessToken=s3cret", "", "ODBC;DSN=x;Trusted_Connection=Yes"),
        ("ODBC;DSN=x;s3cret", "", "ODBC;DSN=x"),
    ]
    for connect, database, want in cases:
        got = jet._linked_target(db, connect, database)
        assert got == want and "s3cret" not in got and "ss;" not in got and "rd}" not in got, (connect, got)
    # a foreign name holding a connect string's marks is dropped
    data = jet_db(1, [three()], linked=[("P", "ODBC;DSN=x", "dbo.T;PWD=s3cret")])
    doc, _, md = convert(data)
    assert summary_lines(doc)[1] == "Linked tables: P → ODBC;DSN=x" and "s3cret" not in md, summary_lines(doc)


def test_linked_connect_as_text():
    # a hostile catalog whose Connect and ForeignName columns carry the hyperlink flag (0x80): they are read as plain
    # text, so the line is a string and the credentials still go
    data = bytearray(jet_db(1, [three()], linked=[("P", "a#;DATABASE=C:\\p.accdb;PWD=pw#", "Q#x#")]))
    db = jet._Db(jet_pages.open(bytes(data)), Context())
    td = jet._tdef(db, 2)
    assert len(td.pages) == 1
    f = db.f
    b = db.jet.page(2)
    col_at = f.idx_def + struct.unpack_from("<I", b, f.nidx)[0] * f.idx_def_size
    for i in range(len(td.columns)):
        o = col_at + i * f.col_size
        num = struct.unpack_from("<H", b, o + f.col_num)[0]
        if td.columns[num].name in ("Connect", "ForeignName"):
            data[2 * 4096 + o + f.col_flags] |= 0x80
    doc, ctx, md = convert(bytes(data))
    assert summary_lines(doc)[1] == "Linked tables: P → C:\\p.accdb (Q#x#)", summary_lines(doc)
    assert "pw" not in md and ctx.skipped == {"linked_data": 1}, ctx.skipped


def docs(rows, **kw):
    return Tbl("Docs", [Col("ID", LONG), Col("Files", COMPLEX, attachments=True)], rows, pk="ID", **kw)


def test_attachments():
    doc, ctx, md = convert(jet_db(3, [docs([[1, [("a.png", PNG, True), ("b.txt", b"hello", True)]]])]))
    sec = section(doc, "Docs")
    assert grid(sec)[1] == ["1", "a.png; b.txt"], grid(sec)
    assert columns_line(sec) == "Columns: ID (Long, PK) · Files (Attachment)", columns_line(sec)
    imgs = images(sec)
    assert [(i.data, i.alt) for i in imgs] == [(PNG, "Docs 1: a.png")], imgs
    assert sec.blocks.index(imgs[0]) > [type(b) for b in sec.blocks].index(Table)  # after the table
    assert "![Docs 1: a.png](" in md and not ctx.skipped, ctx.skipped
    # a raw (stored) file, rows in key order with their pictures, a row with no files, a null cell, a gif and a jpg
    # whose type is upper case
    gif, jpg = b"GIF89a" + bytes(30), b"\xff\xd8\xff\xe0" + bytes(30)
    rows = [[3, [("c.png", PNG2, False)]], [1, [("a.png", PNG, True), ("b.txt", b"hello", True)]], [2, []],
            [4, None], [5, [("x.gif", gif), ("Y.JPG", jpg, True), ("z.doc", b"d" * 100)]]]
    doc, ctx, md = convert(jet_db(3, [docs(rows)]))
    sec = section(doc, "Docs")
    assert grid(sec)[1:] == [["1", "a.png; b.txt"], ["2", ""], ["3", "c.png"], ["4", ""],
                             ["5", "x.gif; Y.JPG; z.doc"]], grid(sec)
    assert [(i.data, i.alt) for i in images(sec)] == [(PNG, "Docs 1: a.png"), (PNG2, "Docs 3: c.png"),
                                                      (gif, "Docs 5: x.gif"), (jpg, "Docs 5: Y.JPG")]
    assert not ctx.skipped, ctx.skipped
    # without a primary key the key is the row's number; a hidden table hides its pictures too
    t = Tbl("Docs", [Col("Files", COMPLEX, attachments=True)], [[None], [[("a.png", PNG)]]], flags=0x08)
    doc, ctx, md = convert(jet_db(3, [t]))
    (img,) = images(section(doc, "Docs"))
    assert img.alt == "Docs 2: a.png" and img.hidden == "hidden" and "Docs 2: a.png" not in md


def test_attachments_share_budget():
    # one flate.Budget(limits.IMAGE_BYTES) a document: every deflated attachment of every table goes through it
    seen = []
    orig = flate.inflate

    def spy(data, budget, limit=None):
        seen.append(budget)
        return orig(data, budget, limit)
    tables = [docs([[1, [("a.png", PNG, True)]], [2, [("b.png", PNG2, True)]]]),
              Tbl("More", [Col("ID", LONG), Col("F", COMPLEX, attachments=True)], [[1, [("c.png", PNG, True)]]])]
    data = jet_db(3, tables)
    flate.inflate = spy
    try:
        doc, ctx, _ = convert(data)
    finally:
        flate.inflate = orig
    assert len(seen) == 3 and len({id(b) for b in seen}) == 1, seen
    assert seen[0].total == limits.IMAGE_BYTES and seen[0].used >= 2 * len(PNG) + len(PNG2)
    assert len(images(section(doc, "Docs"))) == 2 and len(images(section(doc, "More"))) == 1 and not ctx.skipped
    # the budget lowered so the first file fits and the second does not: the second is counted `image` (the budget
    # is per document, so the third, in another table, is past it too); the text stays
    head = len(jet_fix_head("png"))
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = head + len(PNG) + 10
    try:
        doc, ctx, _ = convert(data)
    finally:
        limits.IMAGE_BYTES = old
    assert [i.alt for i in images(section(doc, "Docs"))] == ["Docs 1: a.png"] and not images(section(doc, "More"))
    assert ctx.skipped == {"image": 2}, ctx.skipped
    assert grid(section(doc, "Docs"))[1:] == [["1", "a.png"], ["2", "b.png"]] and grid(section(doc, "More"))[1] == \
        ["1", "c.png"]


def jet_fix_head(ext):
    """The content header Jackcess writes before a file's bytes (its length, 1, the extension's length, the
    extension and NUL in UTF-16)."""
    e = (ext + "\0").encode("utf-16-le")
    return struct.pack("<III", 12 + len(e), 1, len(ext) + 1) + e


def test_attachment_bomb():
    # a deflated file past ZIP_RATIO: no picture, counted `image`; the row and its file name stay
    bomb = bytes(2 * 2 ** 20)
    doc, ctx, md = convert(jet_db(3, [docs([[1, [("bomb.png", bomb, True)]], [2, [("ok.png", PNG, True)]]])]))
    sec = section(doc, "Docs")
    assert grid(sec)[1:] == [["1", "bomb.png"], ["2", "ok.png"]]
    assert [i.alt for i in images(sec)] == ["Docs 2: ok.png"] and ctx.skipped == {"image": 1}, ctx.skipped
    # a wrapper that cannot be read (an unknown type, a cut stream, a header past the data) is counted `image` too
    from jet_fix import attachment_blob
    bad = [b"\x07\x00\x00\x00" + attachment_blob(PNG, "png", False)[4:],
           attachment_blob(PNG, "png", True)[:-6],
           attachment_blob(PNG, "png", False)[:8] + struct.pack("<I", 9999) + attachment_blob(PNG, "png", False)[12:]]
    for blob in bad:
        data = jet_db(3, [docs([[1, [("a.png", PNG, False)]]])])
        good = attachment_blob(PNG, "png", False)
        assert data.count(good) == 1
        data = data.replace(good, (blob + bytes(len(good)))[:len(good)])
        doc, ctx, _ = convert(data)
        assert not images(section(doc, "Docs")) and ctx.skipped == {"image": 1}, ctx.skipped
        assert grid(section(doc, "Docs"))[1] == ["1", "a.png"]


def test_multivalue():
    t = Tbl("M", [Col("ID", LONG), Col("Colors", COMPLEX, multi=TEXT), Col("Nums", COMPLEX, multi=LONG)],
            [[1, ["red", "blue"], [3, 1]], [2, [], None], [3, None, [7]]], pk="ID")
    doc, ctx, md = convert(jet_db(3, [t]))
    sec = section(doc, "M")
    assert grid(sec)[1:] == [["1", "red; blue", "3; 1"], ["2", "", ""], ["3", "", "7"]], grid(sec)
    assert columns_line(sec) == "Columns: ID (Long, PK) · Colors (Complex) · Nums (Complex)"
    assert not ctx.skipped and "f_" not in md and "MSysComplex" not in md, ctx.skipped
    # values past JET_CELL_CHARS are cut, counted `text` once
    t = Tbl("M", [Col("ID", LONG), Col("V", COMPLEX, multi=TEXT)], [[1, ["x" * 30] * 10]])
    old = jet.JET_CELL_CHARS
    jet.JET_CELL_CHARS = 100
    try:
        doc, ctx, _ = convert(jet_db(3, [t]))
    finally:
        jet.JET_CELL_CHARS = old
    cell = grid(section(doc, "M"))[1][1]
    assert cell == "; ".join(["x" * 30] * 4)[:100] + "…" and ctx.skipped == {"text": 1}, (cell, ctx.skipped)
    # a complex id MSysComplexColumns does not hold, or one naming another table: empty cells, counted `record`
    data = jet_db(3, [Tbl("M", [Col("ID", LONG), Col("V", COMPLEX, multi=TEXT)], [[1, ["red"]], [2, ["blue"]]])])
    for bad in (struct.pack("<I", 99), struct.pack("<I", 1)):
        d = bytearray(data)
        db = jet._Db(jet_pages.open(bytes(d)), Context())
        tp = tdef_page(data, "M")
        f = db.f
        b = db.jet.page(tp)
        o = f.idx_def + struct.unpack_from("<I", b, f.nidx)[0] * f.idx_def_size + f.col_size  # the second column
        assert b[o] == COMPLEX and b[o + 11:o + 15] == struct.pack("<I", 1)
        if bad == struct.pack("<I", 1):  # MSysComplexColumns' row names another table
            ct = tdef_page(data, "MSysComplexColumns")
            assert d.count(struct.pack("<i", tp)) >= 1
            pages = data_pages(jet_pages.open(bytes(d)), ct)
            pg = bytearray(d[pages[0] * 4096:(pages[0] + 1) * 4096])
            assert pg.count(struct.pack("<i", tp)) == 1
            d[pages[0] * 4096:(pages[0] + 1) * 4096] = pg.replace(struct.pack("<i", tp), struct.pack("<i", tp + 1))
        else:
            d[tp * 4096 + o + 11:tp * 4096 + o + 15] = bad
        doc, ctx, _ = convert(bytes(d))
        assert grid(section(doc, "M"))[1:] == [["1", ""], ["2", ""]] and ctx.skipped == {"record": 1}, ctx.skipped


def test_calculated():
    scale4 = 12345678901234
    be = scale4.to_bytes(12, "big")
    numeric = struct.pack("<hBB", 14, 4, 0) + be[:4][::-1] + be[4:][::-1]  # CalculatedColumnUtil's numeric form
    cols = [Col("A", LONG), Col("C", LONG, calc=LONG, props={"Expression": "[A]*2"}),
            Col("M", DOUBLE, calc=MONEY, props={"Expression": "[A]/12"}),
            Col("T", TEXT, calc=TEXT, props={"Expression": "\"n\" & [A]"}),
            Col("B", INT, calc=BOOL, props={"Expression": "[A]>1"}),
            Col("N", DOUBLE, calc=NUMERIC, props={"Expression": "[A]/7"}),
            Col("L", TEXT, calc=MEMO, props={"Expression": "[T] & [T]"})]
    rows = [[21, 42, 15000, "n21", True, numeric, "word " * 29 + "end"], [1, 2, 0, "n1", False, b"", None], [2]]
    doc, ctx, md = convert(jet_db(3, [Tbl("K", cols, rows)]))
    sec = section(doc, "K")
    assert grid(sec)[1:] == [["21", "42", "1.5", "n21", "Yes", "1234567890.1234", "word " * 29 + "end"],
                             ["1", "2", "0", "n1", "No", "", ""], ["2", "", "", "", "", "", ""]], grid(sec)
    assert columns_line(sec) == (
        "Columns: A (Long) · C (Long, = [A]*2) · M (Currency, = [A]/12) · T (Text, = \"n\" & [A]) · "
        "B (Yes/No, = [A]>1) · N (Decimal, = [A]/7) · L (Memo, = [T] & [T])"), columns_line(sec)
    assert not ctx.skipped, ctx.skipped
    # the brief's case
    doc, ctx, _ = convert(jet_db(3, [Tbl("K", [Col("A", LONG), Col("C", LONG, calc=LONG,
                                                                     props={"Expression": "[A]*2"})], [[21, 42]])]))
    assert grid(section(doc, "K"))[1] == ["21", "42"] and "= [A]*2" in columns_line(section(doc, "K"))
    # an expression on one line, cut at JET_CELL_CHARS (counted `text`)
    long_expr = "[A]\n+" + "1+" * 60
    old = jet.JET_CELL_CHARS
    jet.JET_CELL_CHARS = 40
    try:
        doc, ctx, _ = convert(jet_db(3, [Tbl("K", [Col("A", LONG), Col("C", LONG, calc=LONG,
                                                                         props={"Expression": long_expr})], [[1, 2]])]))
    finally:
        jet.JET_CELL_CHARS = old
    line = columns_line(section(doc, "K"))
    assert line == "Columns: A (Long) · C (Long, = " + ("[A] +" + "1+" * 60)[:40] + "…)", line
    assert ctx.skipped == {"text": 1}, ctx.skipped


def test_page_type_8():
    for v in (1, 3):
        doc, ctx, md = convert(jet_db(v, [three()], page8=True))
        assert grid(section(doc, "Orders"))[1:] == [["1", "Ann", "first"], ["2", "Bob", ""], ["3", "Cy", "third note"]]
        assert ctx.skipped["jet_page8"] >= 1 and not markdown.unread(ctx.skipped), ctx.skipped
        assert set(ctx.skipped) == {"jet_page8"}, ctx.skipped


# -- the new loops, each charged before its work

def complex_spend(data, name):
    """(the db, the table's tdef, the catalog) just before the table's complex columns are resolved."""
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    jet._relationships(db, objs)
    return db, jet._tdef(db, tdef_page(data, name)), objs


def test_complex_columns_charged():
    # 150 complex columns: MSysComplexColumns' 150 rows are read through the charged row walk, each row's cells
    # charged before they are read
    t = Tbl("W", [Col(f"c{i}", COMPLEX, multi=TEXT) for i in range(150)], [[["v"]] * 150])
    data = jet_db(3, [t])
    db, tdef, objs = complex_spend(data, "W")
    before = db.meter.charged
    with Loops() as loops:
        cols = jet._complex_columns(db, objs)
    spent = db.meter.charged - before
    assert len(cols) == 150 and loops.n <= 2 * spent + 16, (len(cols), loops.n, spent)
    assert spent >= 150 * (jet._ROW + 5 * jet._CELL), spent
    doc, ctx, _ = convert(data)
    assert grid(section(doc, "W"))[1] == ["v"] * 150 and not ctx.skipped
    # MSysComplexColumns' rows take the room: past it the rest are dropped, counted `record` once
    db, tdef, objs = complex_spend(data, "W")
    db.room = 50 * (64 + sys.getsizeof("c10") + jet._KEEP) + 10
    cols = jet._complex_columns(db, objs)
    assert len(cols) == 50 and db.ctx.skipped == {"record": 1}, (len(cols), db.ctx.skipped)
    # a catalog of 20,000 objects: it is looked through once for the type tables' names, a unit an object first
    data = jet_db(3, [Tbl("W", [Col("c", COMPLEX, multi=TEXT)], [[["v"]]])],
                  objects=[(f"f{i}", -32768) for i in range(20_000)])
    db, tdef, objs = complex_spend(data, "W")
    before = db.meter.charged
    with Loops() as loops:
        cols = jet._complex_columns(db, objs)
    spent = db.meter.charged - before
    assert len(cols) == 1 and spent >= len(objs) > 20_000 and loops.n <= 2 * spent + 16, (loops.n, spent)


def test_flat_walk_and_join_charged():
    # one row whose multi-value cell names 3,000 values: the flat table's rows are charged as any table's, and the
    # cell's join charges each value (and its characters) before it is added
    t = Tbl("M", [Col("ID", LONG), Col("V", COMPLEX, multi=TEXT)], [[1, [f"v{i}" for i in range(3_000)]]])
    data = jet_db(3, [t])
    db, tdef, objs = complex_spend(data, "M")
    col = tdef.columns[1]
    before = db.meter.charged
    with Loops() as loops:
        info = jet._complex_info(db, objs, tdef, col)
    spent = db.meter.charged - before
    assert info is not None and loops.n <= 2 * spent + 16 and spent >= 3_000 * (jet._ROW + 3 * jet._CELL), \
        (loops.n, spent)
    raw = struct.pack("<i", 1)
    before = db.meter.charged
    with Loops() as loops:
        cell = info.cell(db, raw)
    spent = db.meter.charged - before
    assert cell.startswith("v0; v1; ") and loops.n <= 2 * spent + 16, (loops.n, spent)
    assert spent >= 3_000 * jet._CPLX + sum(len(f"v{i}") for i in range(3_000)), spent
    # a cut cell stops the loop: about JET_CELL_CHARS of values are visited
    old = jet.JET_CELL_CHARS
    jet.JET_CELL_CHARS = 50
    try:
        with Loops() as loops:
            cell = info.cell(db, raw)
    finally:
        jet.JET_CELL_CHARS = old
    assert cell.endswith("…") and loops.n < 200, loops.n
    # the values kept take the flat room (four times the file): past it the rest are dropped, counted `record` once
    db, tdef, objs = complex_spend(data, "M")
    db.flat_room = 10_000
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    assert 100 < len(info.rows[1]) < 200 and db.ctx.skipped == {"record": 1}, (len(info.rows[1]), db.ctx.skipped)


def test_attachment_rows_charged():
    # 400 attachments on one row (one a picture each tenth): the flat rows are charged, and the pictures' rows (each
    # one's long value read and a unit for its entry) before each is read
    files = [(f"f{i}.png" if i % 10 == 0 else f"f{i}.txt", PNG if i % 10 == 0 else b"t", i % 20 == 0)
             for i in range(400)]
    data = jet_db(3, [docs([[1, files]])])
    db, tdef, objs = complex_spend(data, "Docs")
    before = db.meter.charged
    with Loops() as loops:
        info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    spent = db.meter.charged - before
    assert loops.n <= 2 * spent + 16 and spent >= 400 * (jet._ROW + 6 * jet._CELL), (loops.n, spent)
    before = db.meter.charged
    with Loops() as loops:
        pics = info.pictures(db, 1, "Docs 1", False)
    spent = db.meter.charged - before
    assert len(pics) == 40 and loops.n <= 2 * spent + 16 and spent >= 400 * jet._CPLX + 40 * jet._ROW_COST, \
        (len(pics), loops.n, spent)
    doc, ctx, _ = convert(data)
    assert len(images(section(doc, "Docs"))) == 40 and not ctx.skipped, ctx.skipped
    # 1,000 pictures past the image budget: each file is charged `_CPLX` and each picture `_ROW_COST` before it is
    # looked at, though nothing more is read
    data = jet_db(3, [docs([[1, [(f"p{i}.png", PNG) for i in range(1_000)]]])])
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    db.images.used = db.images.total
    before = db.meter.charged
    with Loops() as loops:
        assert info.pictures(db, 1, "Docs 1", False) == []
    spent = db.meter.charged - before
    assert spent >= 1_000 * (jet._CPLX + jet._ROW_COST) and loops.n <= 2 * spent + 16, (loops.n, spent)
    assert db.ctx.skipped == {"image": 1_000}, db.ctx.skipped
    # a deflated picture of 1 MiB: its inflated bytes are charged, `_INFLATE` a unit
    big = PNG + bytes(2 ** 20 - 100)
    data = jet_db(3, [docs([[1, [("big.png", big, True)]]])])
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    before, kept = db.meter.charged, db.meter.spent
    (pic,) = info.pictures(db, 1, "Docs 1", False)
    spent, kept = db.meter.charged - before, db.meter.spent - kept
    # what it may produce is charged before, what it did not produce given back: kept, the output (and a pass)
    assert pic.data == big and spent >= kept >= len(big) // jet._INFLATE, (spent, kept)
    # with less left on the meter than the picture costs, inflation stops there: no picture, counted `image`
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    db.meter.spent = db.meter.total - 5_000
    assert info.pictures(db, 1, "Docs 1", False) == [] and db.ctx.skipped == {"image": 1}, db.ctx.skipped


def shared_lvals():
    """The fixture writer with one long value for equal bytes, as many flat rows naming one LVAL row look."""
    orig = jet_fix._Writer.long_value
    memo = {}

    def lv(self, data):
        k = bytes(data)
        if k not in memo:
            memo[k] = orig(self, data)
        return memo[k]
    return orig, lv


def test_inflate_charged():
    # a deflated bomb past ZIP_RATIO: the most inflation may produce is charged before it runs, kept when it fails
    bomb = bytes(1_200_000)
    data = jet_db(3, [docs([[1, [("b.png", bomb, True)]]])])
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    before = db.meter.charged
    assert info.pictures(db, 1, "Docs 1", False) == [] and db.ctx.skipped == {"image": 1}
    one = db.meter.charged - before
    assert one >= limits.ZIP_RATIO_FLOOR // jet._INFLATE, one
    # many attachments naming one LVAL row: the bomb is inflated (and paid) once, each repeat counted `image`
    orig, lv = shared_lvals()
    jet_fix._Writer.long_value = lv
    try:
        data = jet_db(3, [docs([[1, [(f"b{i}.png", bomb, True) for i in range(40)]]])])
    finally:
        jet_fix._Writer.long_value = orig
    calls = []
    real = flate.inflate

    def spy(d, budget, limit=None):
        calls.append(len(d))
        return real(d, budget, limit)
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    before = db.meter.charged
    flate.inflate = spy
    try:
        assert info.pictures(db, 1, "Docs 1", False) == []
    finally:
        flate.inflate = real
    spent = db.meter.charged - before
    assert len(calls) == 1 and db.ctx.skipped == {"image": 40}, (calls, db.ctx.skipped)
    assert spent < one + 40 * (jet._CPLX + jet._ROW_COST) + 4_000, (spent, one)
    # a good picture named by many rows: inflated once, each repeat shown and taking its bytes of the budget again
    orig, lv = shared_lvals()
    jet_fix._Writer.long_value = lv
    try:
        data = jet_db(3, [docs([[1, [(f"p{i}.png", PNG, True) for i in range(5)]]])])
    finally:
        jet_fix._Writer.long_value = orig
    calls.clear()
    flate.inflate = spy
    try:
        doc, ctx, _ = convert(data)
    finally:
        flate.inflate = real
    pics = images(section(doc, "Docs"))
    assert len(calls) == 1 and [p.data for p in pics] == [PNG] * 5 and not ctx.skipped, (calls, ctx.skipped)


def test_pictures_kept_one_by_one():
    # the meter running out in the middle of a row's pictures keeps those already made
    data = jet_db(3, [docs([[1, [(f"p{i}.png", PNG, True) for i in range(3)]]])])
    db, tdef, objs = complex_spend(data, "Docs")
    info = jet._complex_info(db, objs, tdef, tdef.columns[1])
    real, calls = jet._picture, []

    def fake(db, definition):
        calls.append(1)
        if len(calls) == 2:
            raise jet._Spent
        return real(db, definition)
    out = []
    jet._picture = fake
    try:
        info.pictures(db, 1, "Docs 1", False, out)
    except jet._Spent:
        pass
    finally:
        jet._picture = real
    assert [p.alt for p in out] == ["Docs 1: p0.png"], out


def test_connect_charged():
    # a connect string of 20,000 parts: its characters and parts are charged before it is split
    db = jet._Db(jet_pages.open(jet_db(1)), Context())
    s = "A=1;" * 20_000 + "PWD=x;DATABASE=d"
    before = db.meter.charged
    with Loops() as loops:
        target = jet._linked_target(db, s, "")
    spent = db.meter.charged - before
    assert target == "d" and loops.n <= 2 * spent + 16, (loops.n, spent)
    assert spent >= len(s) + jet._PART * s.count(";"), spent
    # a braced value of 20,000 escaped "}}": each brace is charged as a part before the value is scanned
    s = "DSN=x;A={" + "}}" * 20_000 + "};DATABASE=d"
    before = db.meter.charged
    with Loops() as loops:
        target = jet._linked_target(db, s, "")
    spent = db.meter.charged - before
    assert target == "d" and loops.n <= 2 * spent + 16, (loops.n, spent)
    assert spent >= len(s) + jet._PART * s.count("}"), spent
    # kept within the room: past it the target is dropped, counted `record`
    data = jet_db(1, linked=[("P", ";DATABASE=" + "p" * 3_000, "P")])
    db = jet._Db(jet_pages.open(data), Context())
    db.room = 100
    objs = jet._catalog(db)
    (p,) = [o for o in objs if o.name == "P"]
    assert p.connect == "" and db.ctx.skipped == {"record": 1}, (p.connect, db.ctx.skipped)


def test_object_names_charged():
    # 5,000 forms and 1,000 linked tables: each name in the summary is charged before it is written
    objs = [(f"form{i}", -32768) for i in range(5_000)]
    linked = [(f"L{i}", f";DATABASE=d{i}", f"T{i}") for i in range(1_000)]
    data = jet_db(1, [], objects=objs, linked=linked)
    db = jet._Db(jet_pages.open(data), Context())
    catalog = jet._catalog(db)
    before = db.meter.charged
    with Loops() as loops:
        inl = jet._summary(db, catalog, "x")
    spent = db.meter.charged - before
    text = "".join(x.text for x in inl if isinstance(x, Text))
    assert text.count("form") == 5_000 and text.count(" → d") == 1_000 and "record" not in db.ctx.skipped, text[:200]
    assert loops.n <= 2 * spent + 16 and spent >= 6_000 * jet._NAME, (loops.n, spent)


def test_expression_charged():
    # a 12,000-character expression: its characters are charged before the schema line holds them
    data = jet_db(3, [Tbl("K", [Col("A", LONG), Col("C", LONG, calc=LONG, props={"Expression": "[A]+" * 3_000})],
                          [[1, 2]])])
    db = jet._Db(jet_pages.open(data), Context())
    objs = jet._catalog(db)
    tdef = jet._tdef(db, tdef_page(data, "K"))
    jet._apply_props(db, tdef, db.props_of(next(o for o in objs if o.name == "K")))
    before = db.meter.charged
    with Loops() as loops:
        line = jet._schema(db, tdef, "K", {})
    spent = db.meter.charged - before
    assert "= [A]+[A]+" in "".join(x.text for x in line) and spent >= 12_000 and loops.n <= 2 * spent + 16, \
        (loops.n, spent)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
