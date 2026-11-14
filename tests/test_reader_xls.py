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
from reader.model import Context  # noqa: E402
from reader_fix import (biff as rec, biff_chart, biff_workbook, blip_metafile, blip_png, make_cfb, make_xls,  # noqa: E402
                        oa_container, oa_fbse, oa_group, oa_shape)


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
    wb = xls.Workbook(biff_workbook([{"name": "S", "cells": {(0, 1): "a", (0, 300): "c"},
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
