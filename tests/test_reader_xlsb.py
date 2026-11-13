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

from reader import markdown  # noqa: E402
from reader.containers import biff12 as b12  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import xlsb  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
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


def test_xlsx_saved_as_xlsb():
    rel = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
           'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
           'Target="xl/workbook.xml"/></Relationships>')
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_zip(path, {"_rels/.rels": rel, "xl/workbook.xml": "<workbook/>"}, stored=())
        got = denied(lambda: xlsb.parse(path, Context()))
    assert got == ("unsupported", "xlsx workbook saved as .xlsb"), got


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


def test_formula_cached():
    tail = bytes([0, 0, 0xFF, 0xFF, 1, 2, 3])  # flags and formula bytes: never read
    records = [xlsb_row(0),
               xlsb_cell(8, 0, 0, xlsb_wide("cached") + tail),
               xlsb_cell(9, 1, 0, struct.pack("<d", 2.5) + tail),
               xlsb_cell(10, 2, 0, b"\x01" + tail),
               xlsb_cell(11, 3, 0, b"\x07" + tail)]
    out, ctx = md([{"name": "S", "records": records}])
    for want in ("cached", "2.5", "#DIV/0!"):
        assert want in out, (want, out)
    assert "TRUE" in out.upper() and not ctx.skipped, (out, ctx.skipped)


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
    assert 'colspan="2"' in out and "wide" in out, out


def test_bad_cell_record_recovery():
    out, ctx = md([{"name": "S", "cells": {(0, 0): "first", (0, 2): "third"},
                    "records": [biff12(7, b"\x00\x00"), xlsb_cell(6, 1, 0, xlsb_wide("mid"))]}])
    assert "first" in out and "third" in out and "mid" in out, out
    assert "(could not read: 1 damaged record)" in out and ctx.skipped["record"] == 1, (out, ctx.skipped)


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
    assert "Chart: bar" in out and "|Q1|7|" in out, out
    assert out.index("top") < out.index("Chart: bar") < out.index("assets/img-001.png"), out  # anchor order
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
    assert "Chart: bar" in out and not ctx.skipped, (out, ctx.skipped)


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


def test_chartsheet_chart():
    parts = with_drawing('<xdr:absoluteAnchor><xdr:pos x="0" y="0"/>' + chart_frame("rId1")
                         + "<xdr:clientData/></xdr:absoluteAnchor>",
                         rels=[("rId1", "chart", "../charts/chart1.xml")], extra={"xl/charts/chart1.xml": BAR})
    out, ctx = md([{"name": "Graph", "chartsheet": True}, {"name": "B", "cells": {(0, 0): "x"}}], extra_parts=parts)
    assert "## Sheet: Graph" in out and "Chart: bar" in out and not ctx.skipped, (out, ctx.skipped)
    out, ctx = md([{"name": "Empty", "chartsheet": True}, {"name": "B", "cells": {(0, 0): "x"}}])
    assert "Empty" not in out and not ctx.skipped, (out, ctx.skipped)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
