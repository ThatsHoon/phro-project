"""xls sheets: displayed values, cached formulas, merges, hidden parts, bad sheets and the container checks.

Run: python -I -X utf8 tests/test_reader_xls.py
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
from reader import limits  # noqa: E402
from reader.formats import biff, sheet_grid, xls, xls_drawing  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Link, NoteRef, Para, Table, Text  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from reader_fix import (biff as rec, biff_chart, biff_workbook, blip_metafile, blip_png,  # noqa: E402
                        cfb_bytes, externsheet, make_cfb, make_xls, oa_container, oa_fbse, oa_group, oa_record,
                        oa_shape, oa_solver, xls_txo)


def run(sheets, hidden=False, **kw):
    """Build an .xls from `sheets`, convert it and return (out.md text, meta)."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_xls(src, sheets, **kw)
        meta = reader.convert(src, Path(d) / "out", hidden=hidden)
        return (Path(d) / "out" / "out.md").read_text(encoding="utf-8"), meta


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def bound_offsets(data):
    return [struct.unpack_from("<I", data, off)[0] for rid, off, size, _ in biff.Stream(data, Context()).records(0)
            if rid == biff.BOUNDSHEET]


def patch_offsets(data, new):
    """The stream with BOUNDSHEET offsets replaced: new maps sheet index -> offset, or a function of the old one."""
    buf = bytearray(data)
    n = 0
    for rid, off, size, _ in biff.Stream(bytes(data), Context()).records(0):
        if rid == biff.BOUNDSHEET:
            if n in new:
                v = new[n]
                struct.pack_into("<I", buf, off, v(struct.unpack_from("<I", buf, off)[0]) if callable(v) else v)
            n += 1
    return bytes(buf)


def convert_bytes(data, hidden=False):
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_cfb(src, {"Workbook": data})
        reader.convert(src, Path(d) / "out", hidden=hidden)
        return (Path(d) / "out" / "out.md").read_text(encoding="utf-8")


def two_sheets():
    return biff_workbook([{"name": "One", "cells": {(0, 0): "first"}}, {"name": "Two", "cells": {(0, 0): "second"}}])


def test_values_displayed():
    cells = {
        (0, 0): "plain text",
        (0, 1): ("fmt", 1385.5, '"₩"#,##0.00'),
        (0, 2): ("fmt", 45000, "yyyy-mm-dd"),
        (0, 3): True,
        (0, 4): ("err", 7),
        (0, 5): 100,
        (0, 6): ("fmt", 0.5, "0%"),
    }
    md, _ = run([{"name": "S", "cells": cells}], formats={14: "yyyy-mm-dd"})
    for want in ("plain text", "₩1,385.50", "2023-03-15", "TRUE", "#DIV/0!", "100", "50%"):
        assert want in md, (want, md)


def test_value_hidden_by_format():
    md, _ = run([{"name": "S", "cells": {(0, 0): ("fmt", 5, ";;;"), (0, 1): "x"}}])
    assert "5|x" not in md and "(1 hidden places left out)" in md, md
    md, _ = run([{"name": "S", "cells": {(0, 0): ("fmt", 5, ";;;"), (0, 1): "x"}}], hidden=True)
    assert "5" in md.replace("A1:B1", ""), md


def fmla(row, col, rgce, value=1.0, extra=b""):
    """A FORMULA record with a cached number: rw, col, ixfe, the result, grbit, chn, cce, then the tokens."""
    return rec(0x0006, struct.pack("<HHHdHIH", row, col, 15, value, 0, 0, len(rgce)) + rgce + extra)


def ref_n(drow, dcol):
    return struct.pack("<BhH", 0x2C, drow, 0xC000 | (dcol & 0xFF))


def lbl(name, rgce):
    return rec(0x0018, struct.pack("<HBBHHH4x", 0, 0, len(name), len(rgce), 0, 0) + b"\0" + name.encode() + rgce)


def test_formulas_decoded_and_grouped():
    shared = ref_n(0, -2) + ref_n(0, -1) + b"\x05"
    exp = struct.pack("<BHH", 0x01, 1, 3)
    recs = [fmla(r, 3, exp) for r in (1, 2, 3)]
    recs.insert(1, rec(0x04BC, struct.pack("<HHBBBBH", 1, 3, 3, 3, 0, 3, len(shared)) + shared))
    three_d = struct.pack("<BHHH", 0x3A, 0, 0, 0xC000) + struct.pack("<BHH", 0x23, 1, 0) + b"\x03"
    recs.append(fmla(1, 4, three_d))
    recs.append(fmla(1, 5, b"\x18\0\0"))
    data = biff_workbook([{"name": "Data", "cells": {(0, 0): 5}}, {"name": "S", "records": recs}],
                         globals_extra=externsheet(0) + lbl("TaxRate", struct.pack("<BH", 0x1E, 7)))
    ctx = Context()
    secs = xls.Workbook(data, ctx, sheet_grid.Budget()).sections()
    p = secs[1].blocks[-1]
    assert "".join(i.text for i in p.inlines) == "Formulas: D2:D4 =B2*C2; E2 =Data!A1+TaxRate; F2 =?", p
    assert ctx.skipped["formula"] == 1, ctx.skipped


def test_unread_valueless_formula_counted_once():
    empty = rec(0x0006, struct.pack("<HHH", 1, 5, 15) + bytes([3, 0, 0, 0, 0, 0, 0xFF, 0xFF])
                + struct.pack("<HIH", 0, 0, 3) + b"\x18\0\0")
    for blank, shown in ((0, False), (5, True)):
        old, limits.BLANK_FORMULAS = limits.BLANK_FORMULAS, blank
        try:
            ctx = Context()
            secs = xls.Workbook(biff_workbook([{"name": "S", "cells": {(0, 0): "a"}, "records": [empty]}]), ctx,
                                sheet_grid.Budget()).sections()
        finally:
            limits.BLANK_FORMULAS = old
        texts = ["".join(i.text for i in b.inlines) for b in secs[0].blocks]
        assert ctx.skipped["formula"] == 1 and ("Formulas: F2 =?" in texts) == shown, (blank, ctx.skipped, texts)


def texts(blocks) -> list:
    """The text of each paragraph, a Hidden run's text in brackets."""
    return ["".join(i.text if isinstance(i, Text) else "[" + "".join(t.text for t in i.inlines) + "]"
                    for i in b.inlines) for b in blocks if isinstance(b, Para)]


def book(sheets, **kw):
    """The Doc of a compound file whose Workbook stream is biff_workbook(sheets, **kw), and its ctx; `streams` adds
    other streams to the compound file."""
    streams = kw.pop("streams", {})
    blob, _ = cfb_bytes({"Workbook": biff_workbook(sheets, **kw), **streams})
    ctx = Context()
    return xls.parse_compound(blob, ctx), ctx


def lbl_at(name, rgce, itab=0, flags=0):
    """A NAME record local to one-based sheet `itab` (0 the workbook) with grbit `flags`."""
    return rec(0x0018, struct.pack("<HBBHHH4x", flags, 0, len(name), len(rgce), 0, itab) + b"\0"
               + name.encode("latin-1") + rgce)


def xlstr(s):
    """An XLUnicodeString of 8-bit characters."""
    return struct.pack("<HB", len(s), 0) + s.encode("latin-1")


def refs(*ranges):
    """A SqRefU of Ref8U (first row, last row, first col, last col)."""
    return struct.pack("<H", len(ranges)) + b"".join(struct.pack("<4H", *r) for r in ranges)


AREA3D = struct.pack("<BHHHHH", 0x3B, 0, 0, 40, 0, 3)


def test_names_workbook_first_sheet_scoped_on_the_sheet():
    names = (externsheet(0) + lbl_at("TaxRate", struct.pack("<BH", 0x1E, 7))
             + lbl_at("Local", struct.pack("<BHHH", 0x3A, 0, 0, 0), itab=1)
             + lbl_at("\x0d", AREA3D, itab=1, flags=0x21))
    doc, _ = book([{"name": "Data", "cells": {(0, 0): 5}}], globals_extra=names)
    assert texts(doc.blocks[:1]) == ["Names: TaxRate = 7"], doc.blocks[:1]
    assert texts(doc.blocks[1].blocks)[-1] == "Names: Local = Data!$A$1", doc.blocks[1].blocks


def test_names_past_the_listed_ones_are_not_read():
    from reader import limits
    bad = bytes([0x18, 0, 0])
    names = b"".join(lbl_at(f"N{k}", bad if k >= limits.FACT_ITEMS else struct.pack("<BH", 0x1E, 7), itab=k % 2)
                     for k in range(2 * limits.FACT_ITEMS + 6))
    doc, ctx = book([{"name": "Data", "cells": {(0, 0): 5}}], globals_extra=names)
    assert ctx.skipped["formula"] == limits.FACT_ITEMS and ctx.skipped["fact"] == 6, ctx.skipped
    assert texts(doc.blocks[:1])[0].endswith("N1998 = ?; (3 more not listed)"), texts(doc.blocks[:1])[0][-60:]
    assert texts(doc.blocks[1].blocks)[-1].endswith("N1999 = ?; (3 more not listed)"), texts(doc.blocks[1].blocks)[-1][-60:]


GUID_STDLINK = bytes.fromhex("d0c9ea79f9bace118c8200aa004ba90b")
GUID_URL = bytes.fromhex("e0c9ea79f9bace118c8200aa004ba90b")
GUID_FILE = bytes.fromhex("0303000000000000c000000000000046")


def hstr(s):
    """A HyperlinkString: its length in characters with the NUL, then UTF-16 with the NUL."""
    return struct.pack("<I", len(s) + 1) + (s + "\0").encode("utf-16-le")


def hlink(r1, r2, c1, c2, url=None, path=None, place=None):
    """An HLINK: the range, the StdLink CLSID, then a Hyperlink object with a URL or file moniker and a location."""
    flags, body = 0, b""
    if url is not None:
        u = (url + "\0").encode("utf-16-le")
        flags |= 0x03
        body += GUID_URL + struct.pack("<I", len(u)) + u
    if path is not None:
        a = path.encode("latin-1") + b"\0"
        flags |= 0x01
        body += (GUID_FILE + struct.pack("<HI", 1, len(a)) + a + struct.pack("<HH", 0xFFFF, 0xDEAD) + bytes(20)
                 + struct.pack("<I", 0))
    if place is not None:
        flags |= 0x08
        body += hstr(place)
    return rec(0x01B8, struct.pack("<4H", r1, r2, c1, c2) + GUID_STDLINK + struct.pack("<II", 2, flags) + body)


def cell_inlines(table, r, c):
    return table.rows[r][c].blocks[0].inlines


def test_hyperlinks_on_their_cells():
    records = [hlink(0, 0, 0, 0, url="https://example.com/north"), hlink(0, 0, 1, 1, path="budget.xls"),
               hlink(0, 0, 2, 2, place="Data!A1"), hlink(0, 0, 3, 3)]
    doc, ctx = book([{"name": "S", "cells": {(0, 0): "site", (0, 1): "file", (0, 2): "go", (0, 3): "odd"},
                      "records": records}])
    t = doc.blocks[0].blocks[0]
    assert isinstance(t, Table), doc.blocks[0].blocks
    assert cell_inlines(t, 0, 0) == [Link("https://example.com/north", [Text("site")])], cell_inlines(t, 0, 0)
    assert cell_inlines(t, 0, 1) == [Link("..\\budget.xls", [Text("file")])], cell_inlines(t, 0, 1)
    assert cell_inlines(t, 0, 2) == [Text("go"), Text(" → Data!A1")], cell_inlines(t, 0, 2)
    assert cell_inlines(t, 0, 3) == [Text("odd")] and not ctx.skipped, (cell_inlines(t, 0, 3), ctx.skipped)


def obj(oid, ot=0x19):
    """An OBJ whose FtCmo gives the object type and id (0x19 a note's comment box)."""
    return rec(0x005D, struct.pack("<HHHHH12x", 0x15, 0x12, ot, oid, 0) + bytes(4))


def note(r, c, oid, author):
    return rec(0x001C, struct.pack("<HHHH", r, c, 0, oid) + xlstr(author) + b"\0")


def test_notes_on_their_cells_with_author():
    records = [obj(1), xls_txo("Ann:\nDivision by zero on purpose"), obj(2, ot=6), xls_txo("box text"),
               note(0, 0, 1, "Ann")]
    doc, ctx = book([{"name": "S", "cells": {(0, 0): "x", (1, 0): "y"}, "records": records}])
    t = doc.blocks[0].blocks[0]
    key = next(iter(doc.notes))
    assert cell_inlines(t, 0, 0) == [Text("x"), NoteRef(key)], cell_inlines(t, 0, 0)
    assert texts(doc.notes[key]) == ["Comment by Ann: Division by zero on purpose"], doc.notes
    assert texts(doc.blocks[0].blocks) == ["box text"] and not ctx.skipped, (doc.blocks[0].blocks, ctx.skipped)


def dv(flags, f1, f2, *ranges):
    """A DV: dwDvFlags, four empty strings (prompt and error titles and texts), two formulas, the ranges."""
    empty = xlstr("\0")
    return rec(0x01BE, struct.pack("<I", flags) + empty * 4 + struct.pack("<HH", len(f1), 0) + f1
               + struct.pack("<HH", len(f2), 0) + f2 + refs(*ranges))


def test_validations():
    pick = dv(3 | 0x80, b"\x17" + bytes([11, 0]) + b"Open\0Closed", b"", (1, 8, 0, 0))
    whole = dv(1, struct.pack("<BH", 0x1E, 0), struct.pack("<BH", 0x1E, 1000), (1, 8, 1, 2))
    above = dv(2 | 4 << 20, struct.pack("<Bd", 0x1F, 0.5), b"", (0, 0, 7, 7))
    records = [rec(0x01B2, struct.pack("<HIIII", 0, 0, 0, 0xFFFFFFFF, 3)), pick, whole, above]
    doc, _ = book([{"name": "S", "cells": {(0, 0): "x"}, "records": records}])
    assert texts(doc.blocks[0].blocks)[-1] == ('Validation: A2:A9 list "Open,Closed"; B2:C9 whole between 0 and 1000; '
                                              "H1 decimal > 0.5"), doc.blocks[0].blocks


def test_conditional_rules_one_line_each():
    bound = (4, 7, 5, 5)
    condfmt = rec(0x01B0, struct.pack("<HH4H", 2, 0, *bound) + refs(bound))
    cell = rec(0x01B1, struct.pack("<BBHH", 1, 5, 3, 0) + bytes(6) + struct.pack("<BH", 0x1E, 150))
    test = struct.pack("<BHH", 0x24, 4, 0xC005) + struct.pack("<BH", 0x1E, 100) + b"\x0d"
    form = rec(0x01B1, struct.pack("<BBHH", 2, 0, len(test), 0) + bytes(6) + test)
    doc, _ = book([{"name": "S", "cells": {(0, 0): "x"}, "records": [condfmt, cell, form]}])
    assert texts(doc.blocks[0].blocks)[1:] == ["Conditional: F5:F8 cell > 150; F5:F8 formula =F5>100"], \
        doc.blocks[0].blocks


def test_pivot_view_names_its_range_and_values_and_unread_facts_are_counted():
    view = rec(0x00B0, struct.pack("<4H", 2, 6, 0, 1) + struct.pack("<16H", *[0] * 16)
               + struct.pack("<HH", 10, 4) + b"\0PivotSales" + b"\0Data")
    item = rec(0x00C5, struct.pack("<7H", 3, 0, 0, 0, 0, 0, 13) + b"\0Sum of Amount")
    bare = rec(0x00B0, struct.pack("<4H", 9, 12, 3, 4) + struct.pack("<16H", *[0] * 16)
               + struct.pack("<HH", 2, 0) + b"\x01" + "피벗".encode("utf-16-le"))
    unread = [rec(0x0872, bytes(30)), rec(0x087A, bytes(30)), rec(0x00B0, bytes(10))]
    doc, ctx = book([{"name": "S", "cells": {(0, 0): "x"}, "records": [view, item, bare, *unread]}])
    assert texts(doc.blocks[0].blocks)[1:] == ["Pivot PivotSales A3:B7: values Sum of Amount", "Pivot: 피벗 D10:E13"], \
        doc.blocks[0].blocks
    assert ctx.skipped == {"fact": 2, "record": 1}, ctx.skipped


def test_hyperlink_with_a_location_keeps_its_file():
    records = [hlink(0, 0, 0, 0, url="https://example.com/page", place="section2"),
               hlink(0, 0, 1, 1, path="other.xls", place="Sheet1!A1")]
    doc, ctx = book([{"name": "S", "cells": {(0, 0): "site", (0, 1): "file"}, "records": records}])
    t = doc.blocks[0].blocks[0]
    assert cell_inlines(t, 0, 0) == [Link("https://example.com/page#section2", [Text("site")])], cell_inlines(t, 0, 0)
    assert cell_inlines(t, 0, 1) == [Link("..\\other.xls#Sheet1!A1", [Text("file")])], cell_inlines(t, 0, 1)


def test_formula_with_an_empty_text_result_is_listed():
    def formula(r, value):
        tokens = struct.pack("<BHH", 0x44, r, 0xC000) + struct.pack("<BH", 0x1E, 2) + b"\x05"
        return rec(0x0006, struct.pack("<HHH", r, 1, 15) + value + struct.pack("<HIH", 0, 0, len(tokens)) + tokens)

    empty = bytes([3, 0, 0, 0, 0, 0, 0xFF, 0xFF])
    records = [formula(0, struct.pack("<d", 2.0)), formula(1, empty), formula(2, struct.pack("<d", 6.0))]
    doc, ctx = book([{"name": "S", "cells": {(0, 0): "1", (1, 0): "2", (2, 0): "3"}, "records": records}])
    assert texts(doc.blocks[0].blocks)[-1] == "Formulas: B1:B3 =A1*2" and not ctx.skipped, \
        (doc.blocks[0].blocks, ctx.skipped)


def test_autofilter_range_from_its_name_and_criteria():
    def doper_str(sign, s):
        return struct.pack("<BB4xBBBx", 6, sign, len(s), 1, 0)

    def doper_num(sign, v):
        return struct.pack("<BBd", 4, sign, v)

    names = externsheet(0) + lbl_at("\x0d", AREA3D, itab=1, flags=0x21)
    records = [rec(0x009D, struct.pack("<H", 4)),
               rec(0x009E, struct.pack("<HH", 2, 0x04) + doper_str(2, "Open") + bytes(10) + b"\0Open"),
               rec(0x009E, struct.pack("<HH", 3, 0) + doper_num(4, 5) + doper_num(1, 9))]
    doc, _ = book([{"name": "S", "cells": {(0, 0): "x"}, "records": records}], globals_extra=names)
    assert texts(doc.blocks[0].blocks)[-1] == "Filter A1:D41: C = Open; D > 5 and D < 9", doc.blocks[0].blocks


def test_protection_and_external_links():
    supbooks = (rec(0x01AE, struct.pack("<HH", 1, 0x0401))
                + rec(0x01AE, struct.pack("<HH", 1, 15) + b"\0" + b"\x01C\x03x\x03budget.xls" + xlstr("Sheet1")))
    extra = supbooks + rec(0x0012, struct.pack("<H", 1)) + rec(0x0019, struct.pack("<H", 1))
    doc, _ = book([{"name": "S", "cells": {(0, 0): "x"}, "records": [rec(0x0012, struct.pack("<H", 1))]},
                   {"name": "T", "cells": {(0, 0): "y"}, "records": [rec(0x0012, struct.pack("<H", 0))]}],
                  globals_extra=extra)
    assert texts(doc.blocks[:2]) == ["External links: [1] C:\\x\\budget.xls",
                                     "Protected: workbook structure; workbook windows"], doc.blocks[:2]
    assert texts(doc.blocks[2].blocks)[-1] == "Protected: sheet", doc.blocks[2].blocks
    assert texts(doc.blocks[3].blocks) == ["A1: y"], doc.blocks[3].blocks


def test_macros_present_line():
    _, ctx = book([{"name": "S", "cells": {(0, 0): "x"}}], streams={"_VBA_PROJECT_CUR/VBA/dir": b"x" * 8})
    assert ctx.skipped["vba"] == 1, ctx.skipped
    _, ctx = book([{"name": "S", "cells": {(0, 0): "x"}}])
    assert not ctx.skipped["vba"], ctx.skipped


def test_formula_cached():
    md, _ = run([{"name": "S", "cells": {(0, 0): ("f", 3.0), (1, 0): ("f", "total")}}])
    assert "3" in md and "total" in md, md
    # a SHRFMLA between the FORMULA and its STRING
    pre = struct.pack("<HHH", 0, 0, 15)
    formula = rec(0x0006, pre + bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF]) + struct.pack("<HIH", 0, 0, 0))
    shr = rec(0x04BC, struct.pack("<HHBB", 0, 0, 0, 0) + b"\0\0\0\0")
    string = rec(0x0207, struct.pack("<HB", 5, 0) + b"total")
    md, _ = run([{"name": "S", "records": [formula + shr + string]}])
    assert "total" in md, md


def test_merged_cells():
    md, _ = run([{"name": "S", "cells": {(0, 0): "wide", (1, 0): "a", (1, 1): "b"}, "merges": [(0, 0, 0, 1)]}])
    assert md.endswith("\nA1:B2\n|wide||\n-|-\na|b\n\nMerged: A1:B1\n"), md


def test_hidden_row_col_sheet():
    sheets = [
        {"name": "Open", "cells": {(0, 0): "keepme", (1, 0): "rowsecret", (0, 1): "colsecret", (0, 2): "tail"},
         "hidden_rows": [1], "hidden_cols": [1]},
        {"name": "Shy", "state": 1, "cells": {(0, 0): "sheetsecret"}},
    ]
    md, _ = run(sheets)
    assert "(3 hidden places left out)" in md, md
    for gone in ("rowsecret", "colsecret", "sheetsecret"):
        assert gone not in md, (gone, md)
    assert "keepme" in md and "tail" in md
    md, _ = run(sheets, hidden=True)
    for shown in ("rowsecret", "colsecret", "sheetsecret"):
        assert shown in md, (shown, md)
    assert "(hidden)" in md


def test_two_sheets_one_bad():
    data = two_sheets()
    md = convert_bytes(patch_offsets(data, {1: len(data) + 500}))
    assert "first" in md and "second" not in md, md
    assert "(could not read: 1 sheet)" in md, md
    md = convert_bytes(patch_offsets(data, {1: lambda o: o + 3}))  # an offset that is not a BOF
    assert "first" in md and "second" not in md and "could not read: 1 sheet" in md, md


def test_damaged_boundsheet_keeps_its_place():
    # a BOUNDSHEET whose name runs past its record is a placeholder, so the sheet indexes after it stay right
    data = biff_workbook([{"name": "A", "cells": {(0, 0): "one"}}, {"name": "B", "cells": {(0, 0): "two"}},
                          {"name": "C", "cells": {(0, 0): "three"}}])
    buf = bytearray(data)
    seen = 0
    for rid, off, size, _ in biff.Stream(data, Context()).records(0):
        if rid == biff.BOUNDSHEET:
            seen += 1
            if seen == 2:
                buf[off + 6] = 250  # cch past the record
    ctx = Context()
    wb = xls.Workbook(bytes(buf), ctx, sheet_grid.Budget())
    assert [s.name for s in wb.g.sheets] == ["A", "", "C"], [s.name for s in wb.g.sheets]
    secs = wb.sections()
    assert [s.title for s in secs] == ["A", "C"], [s.title for s in secs]
    assert ctx.skipped["sheet"] == 1 and not ctx.skipped["record"], dict(ctx.skipped)
    assert 2 in wb.cells and 1 not in wb.cells  # sheet C is still index 2
    short = biff.Stream(rec(0x0809, struct.pack("<HHHHII", 0x0600, 5, 0, 0, 0, 6)) + rec(0x0085, b"\x00\x00\x00\x00")
                        + rec(0x000A), Context())
    assert [s.name for s in biff.read_globals(short).sheets] == [""]  # too short to hold a name: still a place


def test_fixture_encodings_round_trip():
    # the fixture writes what BIFF8 says: COLINFO before DIMENSIONS, a ROW's colMic and colMac, a long SST in CONTINUEs
    words = [f"w{i:05d}" + "x" * 40 for i in range(1800)]  # over 65535 bytes of SST
    cells = {(0, 0): "a", (0, 3): 2.5, (2, 1): 7, (2, 2): True, (3, 0): ("err", 0x07), (3, 1): ("f", "txt"),
             (3, 2): ("f", 4.0), (4, 0): ("fmt", 0.5, "0%")}
    cells.update({(5 + i, 0): w for i, w in enumerate(words)})
    data = biff_workbook([{"name": "S", "cells": cells, "hidden_cols": [1]}])
    st = biff.Stream(data, Context())
    g = biff.read_globals(st)
    assert g.sst[:2] == ["a", words[0]] and len(g.sst) == 1 + len(words) and len(set(g.sst)) == len(g.sst)
    recs = list(st.records(g.sheets[0].offset))
    ids = [r[0] for r in recs]
    assert ids.index(0x007D) < ids.index(0x0200), ids[:6]
    rows = {struct.unpack_from("<H", data, r[1])[0]: struct.unpack_from("<HH", data, r[1] + 2)
            for r in recs if r[0] == 0x0208}
    assert rows[0] == (0, 4) and rows[2] == (1, 3) and rows[3] == (0, 3), rows
    wb = xls.Workbook(data, Context(), sheet_grid.Budget())
    wb.sections()
    got = wb.cells[0]
    assert got[(0, 0)] == "a" and got[(0, 3)] == "2.5" and got[(2, 1)] == "7" and got[(2, 2)] == "TRUE", got
    assert got[(3, 0)] == "#DIV/0!" and got[(3, 1)] == "txt" and got[(3, 2)] == "4" and got[(4, 0)] == "50%", got
    assert got[(5 + 1799, 0)] == words[1799]


def test_failed_sheet_leaves_no_cells():
    bof = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0, 0, 0, 6))
    deep = {"name": "Deep", "cells": {(0, 0): "kept?"}, "records": [bof] * (limits.MAX_DEPTH + 2)}
    ok = {"name": "Ok", "cells": {(0, 0): "fine"}}
    ctx = Context()
    wb = xls.Workbook(biff_workbook([deep, ok]), ctx, sheet_grid.Budget())
    secs = wb.sections()
    assert [s.title for s in secs] == ["Ok"] and ctx.skipped["sheet"] == 1, (secs, dict(ctx.skipped))
    assert 0 not in wb.cells and wb.cells[1] == {(0, 0): "fine"}, wb.cells


def cells_of(sheet, **kw):
    """(the Workbook's cells of sheet 0, its context) for a one-sheet workbook."""
    ctx = Context()
    wb = xls.Workbook(biff_workbook([dict(sheet, name="S")], **kw), ctx, sheet_grid.Budget())
    wb.sections()
    return wb.cells[0], ctx


def test_colinfo_last_column_0x100():
    info = rec(0x007D, struct.pack("<HHHHHH", 1, 0x100, 2560, 15, 1, 0))
    wb = xls.Workbook(biff_workbook([{"name": "S", "cells": {(0, c): "a" for c in range(1, 301)},
                                      "records": [info]}]), Context(), sheet_grid.Budget())
    (t,) = wb.sheet_blocks(0)
    assert t.hidden_cols == frozenset(range(256)), sorted(t.hidden_cols)[-3:]  # columns 1..256 of 1..300


def test_labelsst_out_of_range_is_dropped():
    pre = struct.pack("<HHH", 0, 1, 15)
    bad = rec(0x00FD, pre + struct.pack("<I", 99))
    got, ctx = cells_of({"cells": {(0, 0): "ok"}, "records": [bad]})
    assert got == {(0, 0): "ok"} and not ctx.skipped, (got, dict(ctx.skipped))


def test_formula_bool_and_error_results():
    got, _ = cells_of({"cells": {(0, 0): ("f", True), (0, 1): ("f", ("err", 0x07)), (0, 2): ("f", ("err", 0x63))}})
    assert got == {(0, 0): "TRUE", (0, 1): "#DIV/0!", (0, 2): "#N/A"}, got


def test_lost_string_after_formula():
    pre = struct.pack("<HHH", 0, 0, 15)
    formula = rec(0x0006, pre + bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF]) + struct.pack("<HIH", 0, 0, 0))
    number = rec(0x0203, struct.pack("<HHH", 1, 0, 15) + struct.pack("<d", 5.0))
    stray = rec(0x0207, struct.pack("<HB", 3, 0) + b"abc")  # a STRING with no FORMULA before it
    got, ctx = cells_of({"records": [formula + number + stray]})
    assert got == {(1, 0): "5"} and not ctx.skipped, (got, dict(ctx.skipped))


def test_very_hidden_sheet():
    sheets = [{"name": "Open", "cells": {(0, 0): "shown"}}, {"name": "Deep", "state": 2, "cells": {(0, 0): "buried"}}]
    md, _ = run(sheets)
    assert "shown" in md and "buried" not in md and "(1 hidden places left out)" in md, md
    md, _ = run(sheets, hidden=True)
    assert "buried" in md, md


def test_ixfe_out_of_range_is_general():
    num = rec(0x0203, struct.pack("<HHH", 0, 0, 999) + struct.pack("<d", 1234.5))
    got, ctx = cells_of({"records": [num]})
    assert got == {(0, 0): "1234.5"} and not ctx.skipped, (got, dict(ctx.skipped))


def test_sheet_without_eof_is_read():
    data = biff_workbook([{"name": "S", "cells": {(0, 0): "last"}}])
    assert data.endswith(rec(0x000A))
    wb = xls.Workbook(data[:-4], Context(), sheet_grid.Budget())
    wb.sections()
    assert wb.cells[0] == {(0, 0): "last"}, wb.cells


def test_swallowed_error_leaves_no_section():
    # a non-ReadError out of a sheet is swallowed by ctx.guard (counted under `sheet`): the sheet has no section
    # and does not take the blocks of the one before it
    real = xls.Workbook.sheet_blocks

    def flaky(self, i):
        if i == 1:
            raise RuntimeError("boom")
        return real(self, i)

    xls.Workbook.sheet_blocks = flaky
    try:
        ctx = Context()
        data = biff_workbook([{"name": "A", "cells": {(0, 0): "a"}}, {"name": "B", "cells": {(0, 0): "b"}},
                              {"name": "C", "cells": {(0, 0): "c"}}])
        secs = xls.Workbook(data, ctx, sheet_grid.Budget()).sections()
    finally:
        xls.Workbook.sheet_blocks = real
    assert [s.title for s in secs] == ["A", "C"] and ctx.skipped["sheet"] == 1, (secs, dict(ctx.skipped))


def test_all_sheets_bad():
    data = biff_workbook([{"name": "One", "cells": {(0, 0): "first"}}])
    got = denied(lambda: convert_bytes(patch_offsets(data, {0: len(data) + 500})))
    assert got == ("damaged", "no readable sheets"), got


def test_mulrk_length_wins():
    vals = b"".join(struct.pack("<HI", 15, (v << 2) | 2) for v in (11, 22, 33))
    body = struct.pack("<HH", 0, 0) + vals + struct.pack("<H", 200)
    ctx = Context()
    wb = xls.Workbook(biff_workbook([{"name": "S", "records": [(0x00BD, body)]}]), ctx, sheet_grid.Budget())
    wb.sections()
    assert wb.cells[0] == {(0, 0): "11", (0, 1): "22", (0, 2): "33"}, wb.cells[0]
    assert not ctx.skipped, ctx.skipped


def test_sheet_cut():
    old = limits.SHEET_CELLS
    limits.SHEET_CELLS = 5
    try:
        md, _ = run([{"name": "S", "cells": {(r, 0): f"v{r}" for r in range(10)}}])
    finally:
        limits.SHEET_CELLS = old
    assert "1 sheet cut short" in md, md


def test_duplicate_offset():
    data = two_sheets()
    md = convert_bytes(patch_offsets(data, {1: bound_offsets(data)[0]}))
    assert md.count("## Sheet:") == 1 and "## Sheet: One" in md, md
    assert "(could not read: 1 sheet)" in md, md


def test_sparse_far_rows():
    ctx = Context()
    t0 = time.monotonic()
    wb = xls.Workbook(biff_workbook([{"name": "S", "cells": {(0, 0): "top", (65535, 0): "bottom"}}]), ctx,
                      sheet_grid.Budget())
    secs = wb.sections()
    assert time.monotonic() - t0 < 2
    assert len(secs) == 1 and not ctx.skipped, ctx.skipped
    assert ["".join(i.text for i in b.inlines) for b in secs[0].blocks] == ["A1: top", "A65536: bottom"], secs[0].blocks


def test_macro_sheet_quiet():
    md, _ = run([{"name": "Real", "cells": {(0, 0): "data"}}, {"name": "Macro", "kind": 1}])
    assert "## Sheet: Macro" not in md and "could not read" not in md, md
    assert "## Sheet: Real" in md


def test_chart_sheet_table():
    chart = biff_chart([{"name": "A", "vals": [1, 2], "cats": ["x", "y"]}], title="T")
    md, _ = run([{"name": "Real", "cells": {(0, 0): "data"}}, {"name": "Chart1", "kind": 2, "raw": chart}])
    assert "## Sheet: Chart1\n\nChart: column — T\n||A|\n-|-\nx|1\ny|2\n" in md, md
    assert "could not read" not in md, md


def test_filepass_encrypted():
    # an encryption type no later decryption knows (2), so the file stays denied
    got = denied(lambda: run([{"name": "S", "cells": {(0, 0): "x"}}], filepass=struct.pack("<H", 2) + bytes(20)))
    assert got[0] == "encrypted", got


def biff5_book(cells, *, globals_extra=b"", name="Лист1", codepage=1251, enc="cp1251"):
    """A BIFF5 Book stream by hand: BOF 0x0500, CODEPAGE, a FORMAT (164, "0.00") and two XFs (the second uses it),
    `globals_extra`, one BOUNDSHEET in the code page, then the sheet: BOF 0x0500 (dt 0x10), the `cells` records,
    EOF."""
    head = (rec(0x0809, struct.pack("<HHHH", 0x0500, 0x0005, 0, 0)) + rec(0x0042, struct.pack("<H", codepage))
            + rec(0x041E, struct.pack("<HB", 164, 4) + b"0.00")
            + rec(0x00E0, struct.pack("<HH", 0, 0) + bytes(12)) + rec(0x00E0, struct.pack("<HH", 0, 164) + bytes(12))
            + globals_extra)
    raw = name.encode(enc)
    bound_len = 4 + 7 + len(raw)
    eof = rec(0x000A)
    pos = len(head) + bound_len + len(eof)
    sheet = rec(0x0809, struct.pack("<HHHH", 0x0500, 0x0010, 0, 0)) + b"".join(cells) + eof
    return head + rec(0x0085, struct.pack("<IBBB", pos, 0, 0, len(raw)) + raw) + eof + sheet


def label5(r, c, text, enc="cp1251"):
    raw = text.encode(enc)
    return rec(0x0204, struct.pack("<HHHH", r, c, 0, len(raw)) + raw)


def note5(r, c, raw, total=None):
    return rec(0x001C, struct.pack("<HHH", r, c, len(raw) if total is None else total) + raw)


def book5(stream):
    blob, _ = cfb_bytes({"Book": stream})
    ctx = Context()
    return xls.parse_compound(blob, ctx), ctx


def test_biff5_book_read_in_its_code_page():
    """An Excel 5.0/95 Book: its sheet name, cell text and format code in the CODEPAGE's code page, a formula's
    cached value (its tokens are BIFF5's, counted under formula), a NOTE split over a continuation NOTE (row
    0xFFFF), and a NAME counted under fact."""
    first, rest = "Заметка ".encode("cp1251"), "вторая часть".encode("cp1251")
    cells = [label5(0, 0, "Привет"), rec(0x0203, struct.pack("<HHHd", 0, 1, 1, 1.5)),
             rec(0x0006, struct.pack("<HHHdHIH", 0, 2, 0, 3.0, 0, 0, 3) + b"\x1e\x03\x00"),
             note5(0, 0, first, total=len(first) + len(rest)), note5(0xFFFF, 0, rest)]
    name = rec(0x0018, struct.pack("<HBBHHHHBBBB", 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0) + b"Rate")
    doc, ctx = book5(biff5_book(cells, globals_extra=name))
    sec = doc.blocks[0]
    assert sec.title == "Лист1", sec
    t = sec.blocks[0]
    assert isinstance(t, Table), sec.blocks
    key = next(iter(doc.notes))
    assert cell_inlines(t, 0, 0) == [Text("Привет"), NoteRef(key)], cell_inlines(t, 0, 0)
    assert [cell_inlines(t, 0, c) for c in (1, 2)] == [[Text("1.50")], [Text("3")]], t.rows
    assert texts(doc.notes[key]) == ["Comment: Заметка вторая часть"], doc.notes
    assert dict(ctx.skipped) == {"formula": 1, "fact": 1}, ctx.skipped


def test_book_stream_routes_to_xls():
    """A compound file holding only a Book stream is xls's own, whichever reader opened it."""
    from reader.containers.cfb import Compound
    from reader.formats import cfb_route
    with Compound(cfb_bytes({"Book": biff5_book([label5(0, 0, "x")])})[0]) as cf:
        assert cfb_route.route(cf, "xls", "no Workbook stream") is None
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "xls"


def test_biff5_codepages():
    """CODEPAGE 1200 is UTF-16 (the bytes of a BIFF5 string are still its count), 32768 Mac Roman, and one Python
    has no codec for is read as cp1252."""
    assert biff._codec(1200) == "utf-16-le" and biff._codec(32768) == "mac_roman", biff._codec(1200)
    assert biff._codec(1251) == "cp1251" and biff._codec(54321) == "cp1252", biff._codec(54321)
    doc, _ = book5(biff5_book([label5(0, 0, "café", "cp1252")], name="Hoja", codepage=54321, enc="cp1252"))
    assert doc.blocks[0].title == "Hoja" and texts(doc.blocks[0].blocks) == ["A1: café"], doc.blocks


def test_biff5_filepass_encrypted():
    stream = biff5_book([label5(0, 0, "x")], globals_extra=rec(0x002F, struct.pack("<HH", 0x1234, 0x5678)))
    got = denied(lambda: book5(stream))
    assert got[0] == "encrypted", got


def obj5(ot):
    """A BIFF5 OBJ: cObj, the object type, its id, then the rest of the record."""
    return rec(0x005D, struct.pack("<IHH", 1, ot, 1) + bytes(26))


def test_biff5_chart_and_objects_counted():
    """A chart substream inside a BIFF5 sheet is counted under chart, not read as BIFF8; its chart OBJ is not counted
    again, a picture OBJ is counted under image and a text box under shape."""
    chart = rec(0x0809, struct.pack("<HHHH", 0x0500, 0x0020, 0, 0)) + rec(0x1001, b"\0\0") + rec(0x000A)
    doc, ctx = book5(biff5_book([label5(0, 0, "x"), obj5(5), chart, obj5(8), obj5(6)]))
    assert texts(doc.blocks[0].blocks) == ["A1: x"], doc.blocks
    assert dict(ctx.skipped) == {"chart": 1, "image": 1, "shape": 1}, ctx.skipped


def test_container_denials():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_cfb(src, {"Other": b"x"})
        got = denied(lambda: reader.convert(src, Path(d) / "out"))
        assert got == ("damaged", "no Workbook stream"), got
        src.write_bytes(b"\x00" * 64)
        got = denied(lambda: reader.convert(src, Path(d) / "out"))
        assert got == ("damaged", "not an Excel workbook"), got


def test_encrypted_package_named_xls():
    """A password-protected xlsx (EncryptionInfo + EncryptedPackage) renamed .xls is encrypted, not damaged."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_cfb(src, {"EncryptionInfo": b"\x04\x00\x04\x00" + bytes(60), "EncryptedPackage": bytes(4096)})
        got = denied(lambda: reader.convert(src, Path(d) / "out"))
    assert got == ("encrypted", ""), got


def test_ppt_named_xls():
    """A PowerPoint compound file named .xls is read by the ppt parser, as slides."""
    from reader_fix import make_ppt
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_ppt(src, [{"title": "Deck title", "body": ["slide text"]}])
        reader.convert(src, Path(d) / "out")
        out = (Path(d) / "out" / "out.md").read_text(encoding="utf-8")
    assert "## Slide 1: Deck title" in out and "slide text" in out, out


def test_nested_bof_skipped():
    # a chart substream inside a worksheet: its records are not cells, and the sheet's own EOF ends the sheet
    inner = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0020, 0, 0, 0, 6)) + \
        rec(0x0203, struct.pack("<HHH", 0, 0, 15) + struct.pack("<d", 987.0)) + rec(0x000A)
    md, _ = run([{"name": "S", "cells": {(1, 0): "outer"}, "records": [inner]}])
    assert "outer" in md and "987" not in md, md


PNG_A = bytes.fromhex("89504e470d0a1a0a") + b"A" * 24
PNG_B = bytes.fromhex("89504e470d0a1a0a") + b"B" * 24
PNG_C = bytes.fromhex("89504e470d0a1a0a") + b"C" * 24


def dgg(*pngs):
    """The globals' DggContainer: a BStoreContainer with one FBSE embedding each PNG."""
    return oa_container(0xF000, oa_container(0xF001, *(oa_fbse(blip_png(p)) for p in pngs)))


def anchor(row, col=0):
    return struct.pack("<9H", 0, col, 0, row, 0, col + 1, 0, row + 1, 0)


def dg(*shapes):
    """A sheet's DgContainer holding its shapes in an SpgrContainer."""
    return oa_container(0xF002, oa_group(1024, *shapes))


def pics(sheets, **kw):
    """Convert a workbook; returns (out.md text, meta, {asset name: bytes})."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_xls(src, sheets, **kw)
        meta = reader.convert(src, Path(d) / "out", hidden=False)
        text = (Path(d) / "out" / "out.md").read_text(encoding="utf-8")
        folder = Path(d) / "out" / "assets"
        assets = {f.name: f.read_bytes() for f in folder.glob("*")} if folder.exists() else {}
        return text, meta, assets


def test_picture_in_sheet():
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "drawing": dg(oa_shape(1025, 1, anchor(2)))}
    text, meta, assets = pics([sheet], drawing=dgg(PNG_A))
    assert assets == {"img-001.png": PNG_A}, assets
    assert text.index("cell") < text.index("![](assets/img-001.png)"), text
    assert "could not read" not in text, text


def test_pictures_anchor_order():
    sheet = {"name": "S", "cells": {(0, 0): "cell"},
             "drawing": dg(oa_shape(1025, 1, anchor(5)), oa_shape(1026, 2, anchor(1)))}
    text, meta, assets = pics([sheet], drawing=dgg(PNG_A, PNG_B))
    assert assets["img-001.png"] == PNG_B and assets["img-002.png"] == PNG_A, assets
    unanchored = {"name": "S", "drawing": dg(oa_shape(1025, 1), oa_shape(1026, 2, anchor(9)))}
    _, _, assets = pics([unanchored], drawing=dgg(PNG_A, PNG_B))
    assert assets["img-001.png"] == PNG_B and assets["img-002.png"] == PNG_A, assets


def test_missing_blip_counted():
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "drawing": dg(oa_shape(1025, 3, anchor(0)))}
    text, meta, assets = pics([sheet], drawing=dgg(PNG_A))
    assert not assets, assets
    assert "(could not read: 1 image)" in text, text


def test_image_budget():
    sheet = {"name": "S", "drawing": dg(oa_shape(1025, 1, anchor(0)), oa_shape(1026, 1, anchor(1)))}
    old = xls_drawing.IMAGE_BYTES
    xls_drawing.IMAGE_BYTES = len(PNG_A) + 1
    try:
        text, meta, assets = pics([sheet], drawing=dgg(PNG_A))
    finally:
        xls_drawing.IMAGE_BYTES = old
    assert len(assets) == 1 and "(could not read: 1 image)" in text, (assets, text)


def test_placement_cap_per_workbook():
    two = dg(oa_shape(1025, 1, anchor(0)), oa_shape(1026, 1, anchor(1)))
    sheets = [{"name": "One", "drawing": two}, {"name": "Two", "drawing": two}]
    old = xls_drawing.MAX_PLACEMENTS
    xls_drawing.MAX_PLACEMENTS = 3  # each sheet is under it alone, the two together are not
    try:
        text, meta, assets = pics(sheets, drawing=dgg(PNG_A))
    finally:
        xls_drawing.MAX_PLACEMENTS = old
    assert text.count("![](assets/") == 3 and "(could not read: 1 image)" in text, text


def test_drawing_continue_joined():
    body = dg(oa_shape(1025, 1, anchor(0)))
    sheet = {"name": "S", "cells": {(0, 0): "cell"}, "drawing": body, "drawing_split": len(body) - 20}
    text, meta, assets = pics([sheet], drawing=dgg(PNG_A))
    assert assets == {"img-001.png": PNG_A}, assets
    both = dgg(PNG_A)
    text, meta, assets = pics([sheet], drawing=both, drawing_split=len(both) - 7)
    assert assets == {"img-001.png": PNG_A}, assets


def test_blip_cache_is_per_workbook():
    # one picture placed on three sheets is inflated once against the document's flate budget
    emf = bytes(40) + b" EMF" + bytes(200)
    store = oa_container(0xF000, oa_container(0xF001, oa_fbse(blip_metafile(0xF01A, 0x3D4, emf))))
    draw = dg(oa_shape(1025, 1, anchor(0)))
    sheets = [{"name": n, "drawing": draw} for n in "ABC"]
    wb = xls.Workbook(biff_workbook(sheets, drawing=store), Context(), sheet_grid.Budget())
    wb.sections()
    assert wb.flate.used == len(emf), (wb.flate.used, len(emf))


def test_group_anchor_is_inherited():
    # a picture with no anchor of its own is placed where its group is, as dochan does; the nearest group wins
    inner = oa_group(2001, oa_shape(1027, 3), anchor=anchor(2))
    outer = oa_group(2000, oa_shape(1026, 2), inner, anchor=anchor(1))
    sheet = {"name": "S", "drawing": dg(oa_shape(1025, 1, anchor(5)), outer)}
    _, _, assets = pics([sheet], drawing=dgg(PNG_A, PNG_B, PNG_C))
    assert [assets[f"img-00{i}.png"] for i in (1, 2, 3)] == [PNG_B, PNG_C, PNG_A], assets
    # a group with no anchor, or a cut one, gives nothing: its pictures follow the anchored ones
    bare = oa_group(2000, oa_shape(1026, 2), anchor=b"\0\0\0")
    sheet = {"name": "S", "drawing": dg(bare, oa_shape(1025, 1, anchor(5)))}
    _, _, assets = pics([sheet], drawing=dgg(PNG_A, PNG_B))
    assert [assets[f"img-00{i}.png"] for i in (1, 2)] == [PNG_A, PNG_B], assets


def test_placements_past_the_cap_are_cut_in_reading_order():
    sheet = {"name": "S", "drawing": dg(oa_shape(1025, 1, anchor(5)), oa_shape(1026, 2, anchor(1)))}
    old = xls_drawing.MAX_PLACEMENTS
    xls_drawing.MAX_PLACEMENTS = 1
    try:
        text, _, assets = pics([sheet], drawing=dgg(PNG_A, PNG_B))
    finally:
        xls_drawing.MAX_PLACEMENTS = old
    assert assets == {"img-001.png": PNG_B} and "(could not read: 1 image)" in text, (assets, text)


def test_repeated_picture_is_placed_each_time():
    sheet = {"name": "S", "drawing": dg(oa_shape(1025, 1, anchor(0)), oa_shape(1026, 1, anchor(1)))}
    text, _, assets = pics([sheet], drawing=dgg(PNG_A))
    assert text.count("![](assets/") == 2 and set(assets.values()) == {PNG_A}, (text, assets)


def test_txo_and_obj_between_drawing_records():
    body = dg(oa_shape(1025, 1, anchor(0)))
    cut = len(body) // 2
    records = [rec(0x00EC, body[:cut]) + rec(0x005D, b"obj") + rec(0x01B6, b"txo") + rec(0x003C, b"text")
               + rec(0x00EC, body[cut:])]
    text, _, assets = pics([{"name": "S", "records": records}], drawing=dgg(PNG_A))
    assert assets == {"img-001.png": PNG_A} and "could not read" not in text, (assets, text)


def test_short_anchor_is_unanchored():
    sheet = {"name": "S", "drawing": dg(oa_shape(1025, 1, b"\0\0\0"), oa_shape(1026, 2, anchor(9)))}
    text, _, assets = pics([sheet], drawing=dgg(PNG_A, PNG_B))
    assert [assets[f"img-00{i}.png"] for i in (1, 2)] == [PNG_B, PNG_A], assets
    assert "could not read" not in text, text


# ---- text boxes (TXO) and connectors

def cells(c1, r1, c2, r2, dx1=0, dy1=0, dx2=0, dy2=0):
    """An OfficeArtClientAnchorSheet from cell (c1, r1) + (dx1/1024, dy1/256) to cell (c2, r2) + (dx2, dy2)."""
    return struct.pack("<9H", 0, c1, dx1, r1, dy1, c2, dx2, r2, dy2)


def tbox(spid, at=None, *, inst=202, flags=0xA00, props=b"", textbox=True):
    """An SpContainer: FSP (type `inst`), an FOPT of `props` when given, a ClientAnchor `at`, and a ClientTextbox
    (0xF00D) when `textbox`, so the next TXO of the sheet is its text."""
    out = oa_record(0xF00A, struct.pack("<II", spid, flags), ver=2, inst=inst)
    if props:
        out += oa_record(0xF00B, props, ver=3, inst=len(props) // 6)
    if at is not None:
        out += oa_record(0xF010, at)
    if textbox:
        out += oa_record(0xF00D, bytes(4))
    return oa_container(0xF004, out)


END_ARROW = struct.pack("<HI", 0x01D1, 1)


def drawn(shapes, texts, rules=(), **sheet):
    """A sheet whose drawing (one MSODRAWING) holds `shapes` and a SolverContainer of `rules`, then a TXO with its
    CONTINUEs for each of `texts` (a str, or the records already built)."""
    extra = [oa_solver(*rules)] if rules else []
    body = oa_container(0xF002, oa_group(1024, *shapes), *extra)
    records = [rec(0x00EC, body)] + [xls_txo(t) if isinstance(t, str) else t for t in texts]
    return {"name": "S", "cells": {(0, 0): "cell"}, "records": records, **sheet}


def flow(md):
    """The Mermaid block's text in out.md (the first one), or None."""
    if "```mermaid\n" not in md:
        return None
    return md.split("```mermaid\n", 1)[1].split("\n```", 1)[0]


def test_xls_textbox_text():
    md, _ = run([drawn([tbox(1025, cells(1, 3, 3, 5))], ["note"])])
    assert "note" in md and md.index("cell") < md.index("note"), md
    assert "could not read" not in md and flow(md) is None, md


def test_xls_connector():
    shapes = [tbox(1025, cells(0, 0, 2, 1)), tbox(1026, cells(0, 4, 2, 5)),
              tbox(1027, cells(1, 1, 1, 4), inst=32, flags=0xB00, props=END_ARROW)]
    md, meta = run([drawn(shapes, ["Start", "End", "go"], rules=[(1025, 1026, 1027)])])
    t = flow(md)
    assert t is not None and mermaid_ok(t), md
    assert t == 'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 -->|"go"| n2', t
    assert md.index("cell") < md.index("```mermaid"), md
    assert md.count("go") == 1 and "could not read" not in md, md


def test_txo_node_not_repeated():
    shapes = [tbox(1025, cells(0, 0, 2, 1)), tbox(1026, cells(0, 4, 2, 5)),
              tbox(1027, cells(1, 1, 1, 4), inst=32, textbox=False), tbox(1028, cells(0, 9, 2, 10))]
    md, _ = run([drawn(shapes, ["Start", "End", "aside"], rules=[(1025, 1026, 1027)])])
    t = flow(md)
    assert t is not None and mermaid_ok(t), md
    assert md.count("Start") == 1 and md.count("End") == 1, md
    assert '"aside"' not in t and md.index("```mermaid") < md.index("aside"), md


def test_txo_utf16_and_8bit():
    shapes = [tbox(1025, cells(0, 2, 1, 3)), tbox(1026, cells(0, 5, 1, 6)), tbox(1027, cells(0, 7, 1, 8))]
    md, _ = run([drawn(shapes, ["가나다", "caf\xe9", xls_txo("wide latin", wide=True)])])
    assert md.index("가나다") < md.index("caf\xe9") < md.index("wide latin"), md
    assert "could not read" not in md, md


def test_textboxes_in_anchor_order_and_unpaired_txo_last():
    # TXOs pair with the text boxes in record order; the paragraphs follow anchor order; a TXO with no text box
    # comes after them, and a text box with no TXO, or a shape with no ClientTextbox, gives nothing
    shapes = [tbox(1025, cells(0, 8, 1, 9)), tbox(1026, None, textbox=False), tbox(1027, cells(0, 2, 1, 3))]
    md, _ = run([drawn(shapes, ["lower", "upper", "spare"])])
    assert md.index("cell") < md.index("upper") < md.index("lower") < md.index("spare"), md
    md, _ = run([drawn([tbox(1025, cells(0, 2, 1, 3)), tbox(1026, cells(0, 3, 1, 4))], ["only"])])
    assert "only" in md and "could not read" not in md, md


def test_txo_between_drawing_records():
    # as Excel writes it: each shape's MSODRAWING, then its OBJ and TXO, then the next shape's MSODRAWING
    first, second = tbox(1025, cells(0, 6, 1, 7)), tbox(1026, cells(0, 2, 1, 3))
    head = oa_container(0xF002, oa_group(1024, first, second))
    cut = len(head) - len(second)
    records = [rec(0x00EC, head[:cut]) + rec(0x005D, b"obj") + xls_txo("one")
               + rec(0x00EC, head[cut:]) + rec(0x005D, b"obj") + xls_txo("two")]
    md, _ = run([{"name": "S", "cells": {(0, 0): "cell"}, "records": records}])
    assert md.index("cell") < md.index("two") < md.index("one"), md


def test_txo_without_drawing_is_a_paragraph():
    md, _ = run([{"name": "S", "cells": {(0, 0): "cell"}, "records": [xls_txo("loose")]}])
    assert md.index("cell") < md.index("loose") and "could not read" not in md, md


def test_damaged_txo():
    short = rec(0x01B6, b"txo") + rec(0x003C, b"\0text")             # no cchText: no text, nothing counted
    bare = rec(0x01B6, struct.pack("<HH6xHHH2x", 0, 0, 4, 16, 0))    # cchText 4, no CONTINUE
    long = xls_txo("abc", cch=9)                                       # cchText past its CONTINUE
    odd = rec(0x01B6, struct.pack("<HH6xHHH2x", 0, 0, 2, 16, 0)) + rec(0x003C, b"\1abc") + rec(0x003C, bytes(16))
    shapes = [tbox(1025 + k, cells(0, 2 + k, 1, 3 + k)) for k in range(5)]
    md, meta = run([drawn(shapes, [short, bare, long, odd, "kept"])])
    assert "kept" in md and "cell" in md and "abc" not in md, md
    assert "(could not read: 3 damaged records)" in md, md


def test_txo_charged_to_sheet_characters():
    sheet = drawn([tbox(1025, cells(0, 2, 1, 3))], ["note"])
    old = limits.SHEET_CHARS
    limits.SHEET_CHARS = 6  # "cell" fits, "note" does not
    try:
        md, _ = run([sheet])
    finally:
        limits.SHEET_CHARS = old
    assert "cell" in md and "note" not in md, md
    wb = xls.Workbook(biff_workbook([sheet]), Context(), sheet_grid.Budget(chars=100))
    blocks = wb.sheet_blocks(0)
    wb.sheet_blocks(0)
    assert wb.budget.chars == 100 - len("cell") - len("note"), wb.budget.chars
    assert len(blocks) == 2 and blocks[-1] == Para([Text("note")]), blocks


def test_embedded_workbook_skips_text_boxes():
    sheet = drawn([tbox(1025, cells(0, 2, 1, 3))], ["note"])
    wb = xls.Workbook(biff_workbook([sheet]), Context(), sheet_grid.Budget(chars=100), pictures=False)
    plain = xls.Workbook(biff_workbook([{"name": "S", "cells": {(0, 0): "cell"}}]), Context(), sheet_grid.Budget(),
                         pictures=False)
    assert wb.sheet_blocks(0) == plain.sheet_blocks(0) and wb.budget.chars == 100 - len("cell"), wb.sheet_blocks(0)


def test_connector_ends_inferred_after_flips():
    # no rule: the connector's anchor runs from its start corner (the far one on a flipped axis) to the opposite one
    shapes = [tbox(1025, cells(0, 0, 2, 1)), tbox(1026, cells(0, 4, 2, 5)),
              tbox(1027, cells(1, 0, 1, 4, dy1=128, dy2=128), inst=32, flags=0xB80, props=END_ARROW, textbox=False)]
    md, _ = run([drawn(shapes, ["Start", "End"])])
    t = flow(md)
    assert t is not None and mermaid_ok(t), md
    assert t.endswith("  %% inferred from position\n  n2 --> n1"), t


def test_hidden_sheet_text_box():
    sheet = drawn([tbox(1025, cells(0, 2, 1, 3))], ["note"], state=1)
    md, _ = run([sheet, {"name": "T", "cells": {(0, 0): "shown"}}])
    assert "note" not in md, md
    md, _ = run([sheet, {"name": "T", "cells": {(0, 0): "shown"}}], hidden=True)
    assert "note" in md, md


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
