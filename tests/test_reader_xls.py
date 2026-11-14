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
from reader.formats import biff, sheet_grid, xls  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.formats import xls_drawing  # noqa: E402
from reader_fix import (biff as rec, biff_chart, biff_workbook, blip_png, make_cfb, make_xls, oa_container, oa_fbse,  # noqa: E402
                        oa_group, oa_shape)


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
    assert 'colspan="2"' in md, md


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
    assert ctx.skipped["repeat"] == 1 and len(secs) == 1, ctx.skipped


def test_macro_sheet_quiet():
    md, _ = run([{"name": "Real", "cells": {(0, 0): "data"}}, {"name": "Macro", "kind": 1}])
    assert "## Sheet: Macro" not in md and "could not read" not in md, md
    assert "## Sheet: Real" in md


def test_chart_sheet_table():
    chart = biff_chart([{"name": "A", "vals": [1, 2], "cats": ["x", "y"]}], title="T")
    md, _ = run([{"name": "Real", "cells": {(0, 0): "data"}}, {"name": "Chart1", "kind": 2, "raw": chart}])
    assert "## Sheet: Chart1\n\nChart: bar — T\n||A|\n|---|---|\n|x|1|\n|y|2|\n" in md, md
    assert "could not read" not in md, md


def test_filepass_encrypted():
    # an encryption type no later decryption knows (2), so the file stays denied
    got = denied(lambda: run([{"name": "S", "cells": {(0, 0): "x"}}], filepass=struct.pack("<H", 2) + bytes(20)))
    assert got[0] == "encrypted", got


def test_book_stream():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_cfb(src, {"Book": biff_workbook([{"name": "S"}])})
        got = denied(lambda: reader.convert(src, Path(d) / "out"))
    assert got == ("unsupported", "Excel 5.0/95 workbook"), got


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
        assets = {f.name: f.read_bytes() for f in (Path(d) / "out" / "assets").glob("*")}             if (Path(d) / "out" / "assets").exists() else {}
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
