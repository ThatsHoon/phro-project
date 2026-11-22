"""xlsb records, the workbook, styles and shared strings, sheets, drawings and the container checks.

Run: python -I -X utf8 tests/test_reader_xlsb.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers import biff12 as b12  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import xlsb  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Link, NoteRef, Para, Table, Text  # noqa: E402
from reader_fix import (biff12, make_cfb, make_xlsb, make_zip, xlsb_bundle, xlsb_cell, xlsb_row,  # noqa: E402
                        xlsb_wide)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def read(sheets, **kw):
    """Build an .xlsb, read its workbook, return (_Wb, ctx)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, sheets, **kw)
        ctx = Context()
        with Package(path) as pkg:
            return xlsb.read_workbook(pkg, ctx), ctx


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def test_varint_framing():
    assert biff12(156, bytes(300))[:4] == bytes.fromhex("9c01ac02")
    data = biff12(5, b"ab") + biff12(156, bytes(300)) + biff12(7)
    ctx = Context()
    got = [(rid, size) for rid, off, size in b12.records(data, ctx)]
    assert got == [(5, 2), (156, 300), (7, 0)] and not ctx.skipped, (got, ctx.skipped)
    rid, off, size = list(b12.records(data, Context()))[0]
    assert data[off:off + size] == b"ab"


def test_fifth_size_byte():
    ctx = Context()
    data = biff12(5, b"x") + bytes([0x05, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]) + biff12(6)
    got = list(b12.records(data, ctx))
    assert [g[0] for g in got] == [5] and ctx.skipped["record"] == 1, (got, ctx.skipped)
    ctx = Context()
    assert len(list(b12.records(biff12(5, b"abc")[:-1], ctx))) == 0 and ctx.skipped["record"] == 1
    ctx = Context()
    assert len(list(b12.records(bytes([0x05, 0x80]), ctx))) == 0 and ctx.skipped["record"] == 1


def test_fields_wide():
    body = xlsb_wide("hi") + xlsb_wide(None) + struct.pack("<I", 2) + b"\x00\xd8x\x00"  # lone high surrogate, then x
    f = b12.Fields(body, 0, len(body))
    assert f.wide() == "hi" and f.wide() is None and f.wide() == "�x"
    bad = struct.pack("<I", 5) + b"ab"
    try:
        b12.Fields(bad, 0, len(bad)).wide()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_record_cap():
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 5
    try:
        ctx = Context()
        got = list(b12.records(b"".join(biff12(5, b"x") for _ in range(9)), ctx))
        assert len(got) == 5 and ctx.skipped["record"] == 1, (len(got), dict(ctx.skipped))
        ctx = Context()  # exactly the cap is no overrun
        assert len(list(b12.records(b"".join(biff12(5, b"x") for _ in range(5)), ctx))) == 5 and not ctx.skipped
    finally:
        limits.BIN_RECORDS = old


def test_wide_does_not_run_into_the_next_record():
    first = struct.pack("<I", 4) + "ab".encode("utf-16-le")  # says 4 characters, holds 2
    data = first + biff12(6, "cdef".encode("utf-16-le"))
    f = b12.Fields(data, 0, len(first))
    try:
        f.wide()
    except ValueError:
        assert f.pos == 4, f.pos  # only the length was taken
        return
    raise AssertionError("expected ValueError")


def test_bad_sst_item_skipped():
    def good(s):
        return biff12(19, b"\0" + xlsb_wide(s))
    bad = biff12(19, b"\0" + struct.pack("<I", 500) + "no".encode("utf-16-le"))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, [{"name": "S"}], extra_parts={"xl/sharedStrings.bin": good("first") + bad + good("third")})
        ctx = Context()
        with Package(path) as pkg:
            wb = xlsb.read_workbook(pkg, ctx)
    assert wb.sst == ["first", "", "third"] and ctx.skipped["record"] == 1, (wb.sst, ctx.skipped)


def test_bundlesh_extra_dword():
    rec = xlsb_bundle("Data", "rId1", extra=bytes(4))
    wb, ctx = read([{"name": "x"}], workbook_records=[rec])
    assert wb.sheets == [("Data", "xl/worksheets/sheet1.bin", 0)] and not ctx.skipped, (wb.sheets, ctx.skipped)
    plain, ctx = read([{"name": "Plain"}])
    assert plain.sheets == [("Plain", "xl/worksheets/sheet1.bin", 0)]


def test_bundlesh_fallback():
    rec = biff12(156, struct.pack("<II", 0, 1) + struct.pack("<I", 900))
    wb, ctx = read([{"name": "x"}], workbook_records=[rec])
    assert wb.sheets == [("Sheet 1", "xl/worksheets/sheet1.bin", 0)], wb.sheets
    assert ctx.skipped["record"] == 1, ctx.skipped


def test_bundlesh_fallback_orders_rids_by_number():
    # rId10 sorts after rId9: the tenth short record falls back to the tenth worksheet, not to rId10's string place
    sheets = [{"name": f"s{i}", "cells": {(0, 0): f"v{i}"}} for i in range(1, 12)]
    records = [xlsb_bundle(f"s{i}", f"rId{i}", 0, i) for i in range(1, 12)]
    records[9] = biff12(156, struct.pack("<II", 0, 10))  # no strings: the tenth sheet gets its fallback name
    wb, ctx = read(sheets, workbook_records=records)
    assert wb.sheets[9] == ("Sheet 10", "xl/worksheets/sheet10.bin", 0), wb.sheets[9]
    assert ctx.skipped["record"] == 1, dict(ctx.skipped)


def test_cell_xfs_only():
    def xf(fid):
        return biff12(47, struct.pack("<HH", 0xFFFF, fid) + bytes(8))
    styles = b"".join([
        biff12(615, struct.pack("<I", 1)), biff12(44, struct.pack("<H", 164) + xlsb_wide("0.0%")), biff12(616),
        biff12(626, struct.pack("<I", 2)), xf(9), xf(10), biff12(627),
        biff12(617, struct.pack("<I", 2)), xf(0), xf(164), biff12(618),
        xf(14),  # outside the block: not counted
    ])
    wb, ctx = read([{"name": "S"}], extra_parts={"xl/styles.bin": styles})
    assert wb.xf_fmt == [0, 164] and wb.formats == {164: "0.0%"}, (wb.xf_fmt, wb.formats)
    wb, ctx = read([{"name": "S"}], date1904=True)
    assert wb.date1904 is True and wb.xf_fmt == [0]
    wb, ctx = read([{"name": "S"}])
    assert wb.date1904 is False


def test_encrypted_xlsb():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_cfb(path, {"EncryptionInfo": b"\x04\x00\x04\x00" + bytes(40), "EncryptedPackage": bytes(64)})
        kind, _ = denied(lambda: xlsb.parse(path, Context()))
        assert kind == "encrypted", kind
        plain = Path(d) / "b.xlsb"
        make_cfb(plain, {"Other": bytes(16)})
        assert denied(lambda: xlsb.parse(plain, Context()))[0] == "damaged"
        junk = Path(d) / "c.xlsb"
        junk.write_bytes(b"not a zip at all")
        assert denied(lambda: xlsb.parse(junk, Context()))[0] == "damaged"


def test_xls_saved_as_xlsb():
    """An Excel 97-2003 compound file named .xlsb is read by the xls parser, as sheets."""
    from reader_fix import make_xls
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xls(path, [{"name": "Data", "cells": {(0, 0): "a", (0, 1): "b"}}])
        ctx = Context()
        out = markdown.render(xlsb.parse(path, ctx), Path(d) / "assets", False, ctx.skipped)
    assert "## Sheet: Data" in out and "\na|b\n" in out and not ctx.skipped, (out, ctx.skipped)


def test_xlsx_saved_as_xlsb():
    """A package whose workbook part is XML is read by ooxml, whatever the extension."""
    from reader_fix import make_xlsx, xlsx_cell, xlsx_row
    sheet = {"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", inline="<t>a</t>"))}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsx(path, [sheet])
        ctx = Context()
        out = markdown.render(xlsb.parse(path, ctx), Path(d) / "assets", False, ctx.skipped)
    assert "## Sheet: S" in out and "a" in out and not ctx.skipped, (out, ctx.skipped)


def md(sheets, hidden=False, **kw):
    """Build an .xlsb, parse it, render it: (markdown, ctx)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, sheets, **kw)
        ctx = Context()
        doc = xlsb.parse(path, ctx)
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped), ctx


def rels_xml(*items):
    body = "".join(f'<Relationship Id="{i}" Type="{REL}{t}" Target="{g}"/>' for i, t, g in items)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>')


def anchor(row, inner, col=0, kind="twoCellAnchor"):
    return (f'<xdr:{kind}><xdr:from><xdr:col>{col}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row>'
            f'<xdr:rowOff>0</xdr:rowOff></xdr:from>{inner}<xdr:clientData/></xdr:{kind}>')


def chart_frame(rid):
    return ('<xdr:graphicFrame><a:graphic><a:graphicData><c:chart r:id="%s"/></a:graphicData></a:graphic>'
            '</xdr:graphicFrame>' % rid)


def pic(rid):
    return f'<xdr:pic><xdr:blipFill><a:blip r:embed="{rid}"/></xdr:blipFill></xdr:pic>'


def drawing_xml(*anchors):
    return ('<?xml version="1.0" encoding="UTF-8"?><xdr:wsDr '
            'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main" '
            f'xmlns:r="{REL[:-1]}">' + "".join(anchors) + "</xdr:wsDr>")


BAR = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
       '<c:barChart><c:ser><c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>S</c:v></c:pt></c:strCache></c:strRef></c:tx>'
       '<c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Q1</c:v></c:pt></c:strCache></c:strRef></c:cat>'
       '<c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>7</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser>'
       '</c:barChart></c:plotArea></c:chart></c:chartSpace>')


def with_drawing(*anchors, rels=(), extra=None):
    """extra_parts for a drawing on sheet 1; `rels` are the drawing's own (id, type, target) relationships."""
    parts = {
        "xl/worksheets/_rels/sheet1.bin.rels": rels_xml(("rId1", "drawing", "../drawings/drawing1.xml")),
        "xl/drawings/drawing1.xml": drawing_xml(*anchors),
        "xl/drawings/_rels/drawing1.xml.rels": rels_xml(*rels),
    }
    parts.update(extra or {})
    return parts


def test_cells_displayed():
    records = [xlsb_cell(6, 1, 0, xlsb_wide("beta")),
               xlsb_cell(2, 3, 0, struct.pack("<I", (5 << 2) | 2)),
               xlsb_cell(4, 4, 0, b"\x01"),
               xlsb_cell(3, 5, 0, b"\x2a")]
    cells = {(0, 0): "alpha", (0, 2): ("fmt", 0.125, "0.00%"), (0, 6): ("fmt", 45000, 14)}
    out, ctx = md([{"name": "S", "cells": cells, "records": records}])
    assert "## Sheet: S" in out, out
    for want in ("alpha", "beta", "12.50%", "#N/A", "2023-03-15"):
        assert want in out, (want, out)
    assert "|5|" in out and not ctx.skipped, (out, ctx.skipped)


def test_value_hidden_by_format():
    out, ctx = md([{"name": "S", "cells": {(0, 0): ("fmt", 5, ";;;"), (0, 1): "x"}}])
    assert "5|x" not in out and "(1 hidden places left out)" in out, out


def parsed(rgce, extra=b""):
    """A CellParsedFormula: cce, the tokens, cb, the extra data."""
    return struct.pack("<I", len(rgce)) + rgce + struct.pack("<I", len(extra)) + extra


def fmla_num(col, rgce, extra=b"", value=1.0):
    """A BrtFmlaNum: the cell, the cached number, grbitFlags, then the formula."""
    return xlsb_cell(9, col, 0, struct.pack("<dH", value, 0) + parsed(rgce, extra))


def test_formulas_decoded_and_grouped():
    exp = struct.pack("<BI", 0x01, 1), struct.pack("<I", 3)
    rel = struct.pack("<BIH", 0x2C, 0, 0xC000 | 0x3FFE) + struct.pack("<BIH", 0x2C, 0, 0xC000 | 0x3FFF) + b"\x05"
    shr = biff12(427, struct.pack("<IIII", 1, 3, 3, 3) + parsed(rel))
    three_d = struct.pack("<BHIH", 0x3A, 0, 0, 0xC000) + struct.pack("<BI", 0x23, 1) + b"\x03"
    records = [xlsb_row(1), fmla_num(3, *exp), shr, fmla_num(4, three_d), fmla_num(5, b"\x18\x00\x00"),
               xlsb_row(2), fmla_num(3, *exp), xlsb_row(3), fmla_num(3, *exp)]
    book = [xlsb_bundle("Data", "rId1", 0, 1), xlsb_bundle("S", "rId2", 0, 2), biff12(353), biff12(357),
            biff12(362, struct.pack("<IIii", 1, 0, 0, 0)), biff12(354),
            biff12(39, struct.pack("<IBI", 0, 0, 0xFFFFFFFF) + xlsb_wide("TaxRate") + parsed(b"\x1e\x07\x00")
                   + xlsb_wide(None))]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, [{"name": "Data", "cells": {(0, 0): 5}}, {"name": "S", "records": records}],
                  workbook_records=book)
        ctx = Context()
        doc = xlsb.parse(path, ctx)
    p = doc.blocks[2].blocks[-1]
    assert "".join(i.text for i in p.inlines) == "Formulas: D2:D4 =B2*C2; E2 =Data!A1+TaxRate; F2 =?", p
    assert ctx.skipped["formula"] == 1 and ctx.skipped["record"] == 0, ctx.skipped


def test_formula_cached():
    tail = struct.pack("<H", 0) + parsed(struct.pack("<BH", 0x1E, 1))  # flags, then the formula =1
    records = [xlsb_row(0),
               xlsb_cell(8, 0, 0, xlsb_wide("cached") + tail),
               xlsb_cell(9, 1, 0, struct.pack("<d", 2.5) + tail),
               xlsb_cell(10, 2, 0, b"\x01" + tail),
               xlsb_cell(11, 3, 0, b"\x07" + tail)]
    out, ctx = md([{"name": "S", "records": records}])
    for want in ("cached", "2.5", "#DIV/0!"):
        assert want in out, (want, out)
    assert "TRUE" in out.upper() and not ctx.skipped, (out, ctx.skipped)
    assert "Formulas: A1:D1 =1" in out, out


def test_rich_string_cell():
    """BrtCellRString (62): a flags byte, the XLWideString, then (fRichStr) a run array that is never read."""
    runs = struct.pack("<I", 2) + struct.pack("<HH", 0, 1) + struct.pack("<HH", 3, 2)
    records = [xlsb_cell(62, 1, 0, b"\x00" + xlsb_wide("rich plain")),
               xlsb_cell(62, 2, 0, b"\x01" + xlsb_wide("rich runs") + runs)]
    out, ctx = md([{"name": "S", "cells": {(0, 0): "plain"}, "records": records}])
    for want in ("plain", "rich plain", "rich runs"):
        assert want in out, (want, out)
    assert not ctx.skipped and "could not read" not in out, (out, ctx.skipped)


def test_hidden_row_col_sheet():
    cells = {(0, 0): "a", (0, 1): "b", (1, 0): "c", (1, 1): "d"}
    colinfo = biff12(60, struct.pack("<II", 1, 1) + bytes(8) + struct.pack("<H", 1))
    out, _ = md([{"name": "S", "cells": cells, "records": [colinfo, xlsb_row(1, hidden=True)]},
                 {"name": "H", "state": 2, "cells": {(0, 0): "secret"}}])
    assert "(3 hidden places left out)" in out and "secret" not in out, out
    shown, _ = md([{"name": "H", "state": 1, "cells": {(0, 0): "secret"}}, {"name": "S", "cells": {(0, 0): "x"}}],
                  hidden=True)
    assert "secret" in shown, shown


def test_merge():
    out, _ = md([{"name": "S", "cells": {(0, 0): "wide", (1, 0): "x", (1, 1): "y"},
                  "records": [biff12(176, struct.pack("<IIII", 0, 0, 0, 1))]}])
    assert out.endswith("\nA1:B2\n|wide||\n-|-\nx|y\n\nMerged: A1:B1\n"), out


def test_bad_cell_record_recovery():
    out, ctx = md([{"name": "S", "cells": {(0, 0): "first", (0, 2): "third"},
                    "records": [biff12(7, b"\x00\x00"), xlsb_cell(6, 1, 0, xlsb_wide("mid"))]}])
    assert "first" in out and "third" in out and "mid" in out, out
    assert "(could not read: 1 damaged record)" in out and ctx.skipped["record"] == 1, (out, ctx.skipped)


def test_bad_row_header_counts_its_row_once():
    # a BrtRowHdr too short to hold its row: the cells of that row go with it, counted once; the next row is read
    records = [xlsb_row(0), xlsb_cell(5, 0, 0, struct.pack("<d", 1.0)),
               biff12(0, b"\x01"), xlsb_cell(5, 0, 0, struct.pack("<d", 2.0)),
               xlsb_cell(5, 1, 0, struct.pack("<d", 3.0)), xlsb_cell(5, 2, 0, struct.pack("<d", 4.0)),
               xlsb_row(2), xlsb_cell(6, 0, 0, xlsb_wide("after"))]
    out, ctx = md([{"name": "S", "records": records}])
    lines = out.splitlines()
    assert "after" in lines and "1" in lines and "2" not in lines, out
    assert ctx.skipped["record"] == 1, dict(ctx.skipped)
    # a cell before any row header has no row, and is counted on its own
    out, ctx = md([{"name": "S", "records": [xlsb_cell(5, 0, 0, struct.pack("<d", 1.0)), xlsb_row(1),
                                              xlsb_cell(6, 0, 0, xlsb_wide("ok"))]}])
    assert "ok" in out and ctx.skipped["record"] == 1, (out, dict(ctx.skipped))


def test_framing_break_keeps_rows():
    cells = {(0, 0): "r0", (1, 0): "r1", (2, 0): "r2"}
    out, ctx = md([{"name": "S", "cells": cells, "records": [bytes([5, 50])]}])
    assert "r0" in out and "r1" in out and "r2" in out, out
    assert "1 damaged record" in out and ctx.skipped["record"] == 1, (out, ctx.skipped)


def test_chart_in_xlsb():
    parts = with_drawing(anchor(4, pic("rId2")), anchor(1, chart_frame("rId1")),
                         rels=[("rId1", "chart", "../charts/chart1.xml"), ("rId2", "image", "../media/image1.png")],
                         extra={"xl/charts/chart1.xml": BAR, "xl/media/image1.png": PNG})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "top"}}], extra_parts=parts)
    assert "Chart: column" in out and "\nQ1|7\n" in out, out
    assert out.index("top") < out.index("Chart: column") < out.index("assets/img-001.png"), out  # anchor order
    assert not ctx.skipped, ctx.skipped


def test_picture_in_xlsb():
    parts = with_drawing(anchor(0, pic("rId1"), kind="oneCellAnchor"),
                         rels=[("rId1", "image", "../media/image1.png")], extra={"xl/media/image1.png": PNG})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert "assets/img-001.png" in out and not ctx.skipped, (out, ctx.skipped)


def test_unlisted_bin_ignored():
    out, ctx = md([{"name": "S", "cells": {(0, 0): "ok"}}],
                  extra_parts={"xl/printerSettings/printerSettings1.bin": bytes(range(256)) * 4})
    assert "ok" in out and not ctx.skipped and "could not read" not in out, (out, ctx.skipped)


def test_two_sheets_one_bad():
    out, ctx = md([{"name": "A", "cells": {(0, 0): "fine"}}, {"name": "B"}],
                  extra_parts={"xl/worksheets/sheet2.bin": None})
    assert "fine" in out and "(could not read: 1 sheet)" in out and ctx.skipped["sheet"] == 1, (out, ctx.skipped)


def test_bundlesh_short_record_keeps_index():
    short = biff12(156, b"\x00\x00\x00\x00")
    odd = biff12(156, struct.pack("<III", 0, 1, 900))
    wb, ctx = read([{"name": "x"}, {"name": "y"}], workbook_records=[short, odd])
    assert wb.sheets == [("Sheet 2", "xl/worksheets/sheet2.bin", 0)], wb.sheets
    assert ctx.skipped["record"] == 2, ctx.skipped


def test_sheet_without_readable_part():
    out, ctx = md([{"name": "A", "cells": {(0, 0): "fine"}}, {"name": "B", "cells": {(0, 0): "unused"}}],
                  workbook_records=[xlsb_bundle("A", "rId1"), xlsb_bundle("Lost", "rId9")])
    assert "fine" in out and "Lost" not in out and "(could not read: 1 sheet)" in out, out
    assert ctx.skipped["sheet"] == 1, ctx.skipped


def test_absolute_anchor_chart():
    parts = with_drawing('<xdr:absoluteAnchor><xdr:pos x="0" y="0"/>' + chart_frame("rId1")
                         + "<xdr:clientData/></xdr:absoluteAnchor>",
                         rels=[("rId1", "chart", "../charts/chart1.xml")], extra={"xl/charts/chart1.xml": BAR})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert "Chart: column" in out and not ctx.skipped, (out, ctx.skipped)


def test_alternate_anchor_places_one_image():
    inner = ('<mc:AlternateContent><mc:Choice Requires="a14">' + pic("rId1") + "</mc:Choice><mc:Fallback>"
             + pic("rId1") + "</mc:Fallback></mc:AlternateContent>")
    wrapped = ('<mc:AlternateContent><mc:Choice Requires="a14">' + anchor(0, pic("rId1"))
               + "</mc:Choice></mc:AlternateContent>")
    parts = with_drawing(wrapped, anchor(3, inner), rels=[("rId1", "image", "../media/image1.png")],
                         extra={"xl/media/image1.png": PNG})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert out.count("assets/img-") == 2 and "img-003" not in out and not ctx.skipped, (out, ctx.skipped)
    one = with_drawing(anchor(0, inner), rels=[("rId1", "image", "../media/image1.png")],
                       extra={"xl/media/image1.png": PNG})
    out, _ = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=one)
    assert out.count("assets/img-") == 1, out


def test_grouped_pictures():
    inner = ("<xdr:grpSp>" + pic("rId1") + "<xdr:grpSp>" + pic("rId2") + "</xdr:grpSp></xdr:grpSp>")
    parts = with_drawing(anchor(0, inner), rels=[("rId1", "image", "../media/image1.png"),
                                                 ("rId2", "image", "../media/image2.png")],
                         extra={"xl/media/image1.png": PNG + b"1", "xl/media/image2.png": PNG + b"2"})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert out.index("assets/img-001.png") < out.index("assets/img-002.png") and "img-003" not in out, out
    assert not ctx.skipped, ctx.skipped


def test_unreadable_chart_counted():
    parts = with_drawing(anchor(0, chart_frame("rId9")), anchor(1, chart_frame("rId1")),
                         rels=[("rId1", "chart", "../charts/missing.xml")])
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert "t" in out and "Chart:" not in out, out
    assert "(could not read: 2 charts)" in out and ctx.skipped["chart"] == 2, (out, ctx.skipped)


def test_chart_past_chart_total():
    parts = with_drawing(anchor(0, chart_frame("rId1")), anchor(1, chart_frame("rId2")),
                         rels=[("rId1", "chart", "../charts/chart1.xml"), ("rId2", "chart", "../charts/chart1.xml")],
                         extra={"xl/charts/chart1.xml": BAR})
    old, limits.CHART_TOTAL = limits.CHART_TOTAL, 6  # one BAR table is 4 cells
    try:
        out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    finally:
        limits.CHART_TOTAL = old
    assert out.count("Chart: column") == 1 and ctx.skipped["chart"] == 1, (out, dict(ctx.skipped))
    assert "(could not read: 1 chart)" in out, out


def test_pictures_past_image_bytes():
    parts = with_drawing(anchor(0, pic("rId1")), anchor(1, pic("rId1")),
                         rels=[("rId1", "image", "../media/image1.png")], extra={"xl/media/image1.png": PNG})
    old, limits.IMAGE_BYTES = limits.IMAGE_BYTES, len(PNG) + 1  # copies are counted: the second does not fit
    try:
        out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    finally:
        limits.IMAGE_BYTES = old
    assert out.count("assets/img-") == 1 and ctx.skipped["image"] == 1, (out, dict(ctx.skipped))


def test_drawing_part_named_twice():
    again = rels_xml(("rId1", "drawing", "../drawings/drawing1.xml"))
    parts = with_drawing(anchor(0, pic("rId1")), rels=[("rId1", "image", "../media/image1.png")],
                         extra={"xl/media/image1.png": PNG, "xl/worksheets/_rels/sheet2.bin.rels": again})
    out, ctx = md([{"name": "A", "cells": {(0, 0): "a"}}, {"name": "B", "cells": {(0, 0): "b"}}], extra_parts=parts)
    assert out.count("assets/img-") == 1 and ctx.skipped["object"] == 1, (out, dict(ctx.skipped))
    assert "(could not read: 1 embedded object)" in out, out


def test_chart_without_series_is_unread():
    # kept: a chart part with no series reads as "could not read", as in the slide and Word readers
    bare = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
            '<c:barChart/></c:plotArea></c:chart></c:chartSpace>')
    parts = with_drawing(anchor(0, chart_frame("rId1")), rels=[("rId1", "chart", "../charts/chart1.xml")],
                         extra={"xl/charts/chart1.xml": bare})
    out, ctx = md([{"name": "S", "cells": {(0, 0): "t"}}], extra_parts=parts)
    assert "Chart:" not in out and ctx.skipped["chart"] == 1, (out, dict(ctx.skipped))


def test_swallowed_error_leaves_no_section():
    real = xlsb._Sheets.sheet

    def flaky(self, name, part, no, hidden):
        if name in ("A", "B"):
            raise RuntimeError("boom")  # the first sheet too: nothing earlier to be mixed up with
        return real(self, name, part, no, hidden)

    xlsb._Sheets.sheet = flaky
    try:
        out, ctx = md([{"name": "A", "cells": {(0, 0): "a"}}, {"name": "B", "cells": {(0, 0): "b"}},
                       {"name": "C", "cells": {(0, 0): "c"}}])
    finally:
        xlsb._Sheets.sheet = real
    assert "## Sheet: C" in out and "## Sheet: A" not in out and "## Sheet: B" not in out, out
    assert ctx.skipped["sheet"] == 2, dict(ctx.skipped)


def test_chartsheet_chart():
    parts = with_drawing('<xdr:absoluteAnchor><xdr:pos x="0" y="0"/>' + chart_frame("rId1")
                         + "<xdr:clientData/></xdr:absoluteAnchor>",
                         rels=[("rId1", "chart", "../charts/chart1.xml")], extra={"xl/charts/chart1.xml": BAR})
    out, ctx = md([{"name": "Graph", "chartsheet": True}, {"name": "B", "cells": {(0, 0): "x"}}], extra_parts=parts)
    assert "## Sheet: Graph" in out and "Chart: column" in out and not ctx.skipped, (out, ctx.skipped)
    out, ctx = md([{"name": "Empty", "chartsheet": True}, {"name": "B", "cells": {(0, 0): "x"}}])
    assert "Empty" not in out and not ctx.skipped, (out, ctx.skipped)


def doc_of(sheets, **kw):
    """Build an .xlsb and parse it: (Doc, ctx)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, sheets, **kw)
        ctx = Context()
        return xlsb.parse(path, ctx), ctx


def texts(blocks) -> list:
    """The text of each paragraph, a Hidden run's text in brackets."""
    return ["".join(i.text if isinstance(i, Text) else "[" + "".join(t.text for t in i.inlines) + "]"
                    for i in b.inlines) for b in blocks if isinstance(b, Para)]


def cell_inlines(table, r, c):
    return table.rows[r][c].blocks[0].inlines


def rels(*items):
    """A relationships part; an item is (id, type, target) or (id, type, target, True) for an external target."""
    body = "".join(f'<Relationship Id="{i[0]}" Type="{REL}{i[1]}" Target="{i[2]}"'
                   + (' TargetMode="External"' if i[3:] else "") + "/>" for i in items)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>')


def rfx(r1, r2, c1, c2):
    return struct.pack("<4I", r1, r2, c1, c2)


def sqrfx(*ranges):
    return struct.pack("<i", len(ranges)) + b"".join(rfx(*r) for r in ranges)


def hlink(r, c, rid="", place=""):
    """A BrtHLink: the range, relId, location, tooltip and display."""
    return biff12(494, rfx(r, r, c, c) + xlsb_wide(rid) + xlsb_wide(place) + xlsb_wide("") + xlsb_wide(""))


def test_hyperlinks_on_their_cells():
    records = [hlink(0, 0, rid="rId1"), hlink(0, 1, place="Data!A1"), hlink(0, 2, rid="rId9")]
    parts = {"xl/worksheets/_rels/sheet1.bin.rels": rels(("rId1", "hyperlink", "https://example.com/north", True))}
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "site", (0, 1): "go", (0, 2): "odd"}, "records": records}],
                      extra_parts=parts)
    t = doc.blocks[0].blocks[0]
    assert isinstance(t, Table), doc.blocks[0].blocks
    assert cell_inlines(t, 0, 0) == [Link("https://example.com/north", [Text("site")])], cell_inlines(t, 0, 0)
    assert cell_inlines(t, 0, 1) == [Text("go"), Text(" → Data!A1")], cell_inlines(t, 0, 1)
    assert cell_inlines(t, 0, 2) == [Text("odd")] and ctx.skipped["link"] == 1, (cell_inlines(t, 0, 2), ctx.skipped)


def comment(author, r, c, text):
    """BrtBeginComment (iauthor, the cell, a GUID), BrtCommentText (a RichStr with no runs), BrtEndComment."""
    return (biff12(635, struct.pack("<i", author) + rfx(r, r, c, c) + bytes(16))
            + biff12(637, b"\x01" + xlsb_wide(text) + struct.pack("<I", 0)) + biff12(636))


def test_comments_on_their_cells_with_author():
    part = (biff12(628) + biff12(630) + biff12(632, xlsb_wide("Ann")) + biff12(632, xlsb_wide("Bob")) + biff12(631)
            + biff12(633) + comment(0, 0, 0, "Ann:\nDivision by zero on purpose") + comment(1, 3, 2, "Check  this")
            + biff12(634) + biff12(629))
    parts = {"xl/worksheets/_rels/sheet1.bin.rels": rels(("rId1", "comments", "../comments1.bin")),
             "xl/comments1.bin": part}
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x", (0, 1): "y", (1, 0): "z"}}], extra_parts=parts)
    t = doc.blocks[0].blocks[0]
    first = cell_inlines(t, 0, 0)
    assert first[0] == Text("x") and isinstance(first[1], NoteRef), first
    assert texts(doc.notes[first[1].id]) == ["Comment by Ann: Division by zero on purpose"], doc.notes
    second = doc.blocks[0].blocks[1].inlines
    assert second[0] == Text("C4: ") and isinstance(second[1], NoteRef), second
    assert texts(doc.notes[second[1].id]) == ["Comment by Bob: Check this"] and not ctx.skipped, (doc.notes, ctx.skipped)


def name(flags, itab, text, rgce):
    """A BrtName: flags, chKey, itab (0xFFFFFFFF the workbook), the name, its formula, a null comment."""
    return biff12(39, struct.pack("<IBI", flags, 0, itab) + xlsb_wide(text) + parsed(rgce) + xlsb_wide(None))


def test_names_workbook_first_sheet_scoped_on_the_sheet():
    book = [xlsb_bundle("S", "rId1", 0, 1),
            name(0, 0xFFFFFFFF, "Rate", struct.pack("<Bd", 0x1F, 0.07)),
            name(0, 0, "Local", struct.pack("<BH", 0x1E, 5)),
            name(1, 0xFFFFFFFF, "Secret", struct.pack("<BH", 0x1E, 9)),
            name(0x21, 0, "_xlnm._FilterDatabase", struct.pack("<BH", 0x1E, 1))]
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}}], workbook_records=book)
    assert texts(doc.blocks[:1]) == ["Names: Rate = 0.07[; Secret = 9]"], doc.blocks[:1]
    assert texts(doc.blocks[1].blocks)[1:] == ["Names: Local = 5"] and not ctx.skipped, (doc.blocks[1].blocks, ctx.skipped)


def dval(flags, f1, f2, *ranges):
    """A BrtDVal: the flags, the ranges, four null strings, two DVParsedFormulas."""
    return biff12(64, struct.pack("<I", flags) + sqrfx(*ranges) + xlsb_wide(None) * 4 + parsed(f1) + parsed(f2))


def test_validation_lists_and_bounds():
    pick = dval(3, b"\x17" + struct.pack("<H", 11) + "Open\0Closed".encode("utf-16-le"), b"", (1, 8, 0, 0))
    whole = dval(1, struct.pack("<BH", 0x1E, 0), struct.pack("<BH", 0x1E, 1000), (1, 8, 1, 2))
    above = dval(2 | 4 << 20, struct.pack("<Bd", 0x1F, 0.5), b"", (0, 0, 7, 7))
    records = [biff12(573, bytes(18)), pick, whole, above, biff12(574)]
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}, "records": records}])
    assert texts(doc.blocks[0].blocks)[1:] == ['Validation: A2:A9 list "Open,Closed"; B2:C9 whole between 0 and 1000; '
                                           "H1 decimal > 0.5"] and not ctx.skipped, (doc.blocks[0].blocks, ctx.skipped)


def cf_rule(kind, template, param=0, flags=0, text=None, f1=b""):
    """A BrtBeginCFRule (iType, iTemplate, dxfId, iPri, iParam, two reserved, flags, three cbFmla, strParam, rgce1)
    and its BrtEndCFRule."""
    body = struct.pack("<7IH3I", kind, template, 0, 1, param, 0, 0, flags, len(f1), 0, 0) + xlsb_wide(text)
    return biff12(463, body + (parsed(f1) if f1 else b"")) + biff12(464)


def test_conditional_formats_one_line_a_rule():
    above = struct.pack("<BI", 0x24, 4) + struct.pack("<H", 0xC000 | 5) + struct.pack("<BH", 0x1E, 100) + b"\x0d"
    records = [biff12(461, struct.pack("<II", 6, 0) + sqrfx((4, 7, 5, 5))),
               cf_rule(1, 0, param=5, f1=struct.pack("<BH", 0x1E, 150)),
               cf_rule(2, 1, f1=above),
               cf_rule(5, 5, param=10, flags=0x08 | 0x10),
               cf_rule(2, 8, text="late", f1=b"\x1d\x01"),
               cf_rule(4, 3),
               cf_rule(2, 0x19, param=1, flags=0x04),
               biff12(462)]
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}, "records": records}])
    assert texts(doc.blocks[0].blocks)[1:] == ['Conditional: F5:F8 cell > 150; F5:F8 formula =F5>100; '
                                               'F5:F8 bottom 10%; F5:F8 contains "late"; F5:F8 data bar; '
                                               'F5:F8 1 std dev above average'], doc.blocks[0].blocks
    assert not ctx.skipped, ctx.skipped


def test_hyperlink_with_a_location_keeps_its_file_and_a_pivot_part_is_counted():
    records = [hlink(0, 0, rid="rId1", place="Sheet1!A1")]
    parts = {"xl/worksheets/_rels/sheet1.bin.rels": rels(("rId1", "hyperlink", "other.xlsb", True),
                                                         ("rId2", "pivotTable", "../pivotTables/pivotTable1.bin"))}
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "file", (0, 1): "y"}, "records": records}], extra_parts=parts)
    t = doc.blocks[0].blocks[0]
    assert cell_inlines(t, 0, 0) == [Link("other.xlsb#Sheet1!A1", [Text("file")])], cell_inlines(t, 0, 0)
    assert ctx.skipped == {"fact": 1}, ctx.skipped


def column(col, *inner):
    return biff12(163, struct.pack("<IH", col, 0)) + b"".join(inner) + biff12(164)


def test_autofilter_range_and_criteria():
    records = [biff12(161, rfx(0, 40, 0, 3)),
               column(2, biff12(165, struct.pack("<II", 0, 0)), biff12(167, xlsb_wide("Open")),
                      biff12(167, xlsb_wide("Hold")), biff12(166)),
               column(3, biff12(172, struct.pack("<I", 0)), biff12(174, struct.pack("<BBd", 4, 4, 5)),
                      biff12(174, struct.pack("<BBd", 4, 1, 9)), biff12(173)),
               column(1, biff12(170, struct.pack("<Bdd", 0x01, 3, 0))),
               column(0, biff12(171, struct.pack("<IBdd", 9, 0, 0, 0))),
               biff12(162)]
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}, "records": records}])
    assert texts(doc.blocks[0].blocks)[1:] == ["Filter A1:D41: C is Open, Hold; D > 5 and D < 9; B top 3; A today"], \
        doc.blocks[0].blocks
    assert not ctx.skipped, ctx.skipped


def test_table_parts_give_name_range_and_totals_row():
    table = biff12(343, rfx(0, 9, 0, 2) + struct.pack("<5I", 0, 1, 1, 1, 0) + struct.pack("<6I", *[0xFFFFFFFF] * 6)
                   + struct.pack("<I", 0) + xlsb_wide("Table1") + xlsb_wide("Sales") + xlsb_wide(None) * 4) + biff12(344)
    records = [biff12(660, struct.pack("<I", 1)), biff12(661, xlsb_wide("rId1")), biff12(662)]
    parts = {"xl/worksheets/_rels/sheet1.bin.rels": rels(("rId1", "table", "../tables/table1.bin")),
             "xl/tables/table1.bin": table}
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}, "records": records}], extra_parts=parts)
    assert texts(doc.blocks[0].blocks)[1:] == ["Table Sales A1:C10: header row 1; totals row 10"], doc.blocks[0].blocks
    assert not ctx.skipped, ctx.skipped


def test_protection_and_external_links():
    book = [xlsb_bundle("S", "rId1", 0, 1), biff12(353), biff12(355, xlsb_wide("rId9")),
            biff12(355, xlsb_wide("rId10")), biff12(357), biff12(362, struct.pack("<I", 0)), biff12(354),
            biff12(534, struct.pack("<HHH", 0, 0, 3))]
    parts = {"xl/_rels/workbook.bin.rels": rels(("rId1", "worksheet", "worksheets/sheet1.bin"),
                                                ("rId2", "styles", "styles.bin"),
                                                ("rId3", "sharedStrings", "sharedStrings.bin"),
                                                ("rId9", "externalLink", "externalLinks/externalLink1.bin"),
                                                ("rId10", "externalLink", "externalLinks/externalLink2.bin")),
             "xl/externalLinks/externalLink1.bin": biff12(360, struct.pack("<H", 0) + xlsb_wide("rId1")
                                                          + xlsb_wide(None)) + biff12(361),
             "xl/externalLinks/_rels/externalLink1.bin.rels": rels(("rId1", "externalLinkPath",
                                                                    "file:///C:/x/budget.xlsb", True)),
             "xl/externalLinks/externalLink2.bin": biff12(360, struct.pack("<H", 1) + xlsb_wide("Excel")
                                                          + xlsb_wide("Book1")) + biff12(361)}
    records = [biff12(535, struct.pack("<HI", 0, 1) + struct.pack("<15I", *[0] * 15))]
    doc, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}, "records": records}], workbook_records=book,
                      extra_parts=parts)
    assert texts(doc.blocks[:2]) == ["External links: [1] file:///C:/x/budget.xlsb; [2] DDE Excel|Book1",
                                     "Protected: workbook structure; workbook windows"], doc.blocks[:2]
    assert texts(doc.blocks[2].blocks)[1:] == ["Protected: sheet"] and not ctx.skipped, (doc.blocks[2].blocks, ctx.skipped)


def test_macros_present_line():
    _, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}}], extra_parts={"xl/vbaProject.bin": b"\0" * 8})
    assert ctx.skipped["vba"] == 1, ctx.skipped
    _, ctx = doc_of([{"name": "S", "cells": {(0, 0): "x"}}])
    assert not ctx.skipped["vba"], ctx.skipped


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
