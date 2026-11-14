"""ppt embedded OLE objects: ExObjList -> ExOleObjStg, Excel charts and sheets, MS Graph, Equation Native, budgets.

Run: python -I -X utf8 tests/test_reader_ppt_objects.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.formats import ppt_objects  # noqa: E402  (ModuleNotFoundError before ppt_objects.py exists)
from reader.containers.officecrypt import PPT_DEFAULT  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import (biff_chart, blip_png, eq_native, excel_object, make_cfb, make_ppt, msgraph_object,  # noqa: E402
                        mtef_v5)

PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24
CHART = {"name": "Chart1", "kind": 2,
         "raw": biff_chart([{"name": "A", "vals": [1, 2, 3], "cats": ["Q1", "Q2", "Q3"]}], title="Sales")}
GRID = {"name": "S", "cells": {(0, 0): 1, (0, 1): 2, (1, 0): 3, (1, 1): 4}}
GRAPH = (["Q1", "Q2"], [("East", [1, 2]), ("West", [3, 4])])
X = mtef_v5(b"\x01\x00" + b"\x02\x00\x83x\x00" + b"\x00")  # a v5 line holding the character x


def build(slides, **kw):
    """Build a .ppt, parse and render it: (markdown, ctx, {asset name: bytes})."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_ppt(path, slides, **kw)
    ctx = Context()
    doc = reader.parse(path, ctx)
    out = markdown.render(doc, d / "assets", False, ctx.skipped)
    assets = {p.name: p.read_bytes() for p in (d / "assets").glob("*")} if (d / "assets").exists() else {}
    return out, ctx, assets


def cfb(streams):
    path = Path(tempfile.mkdtemp()) / "o.cfb"
    make_cfb(path, streams)
    return path.read_bytes()


def obj(ex_obj_id=1, pib=None, box=(0, 200, 400, 300), **kw):
    return {"kind": "object", "ex_obj_id": ex_obj_id, "pib": pib, "box": box, **kw}


def test_excel_chart_in_ppt():
    slide = {"shapes": [{"text": "above", "box": (0, 10, 400, 50)}, obj(),
                        {"text": "below", "box": (0, 500, 400, 550)}]}
    out, ctx, assets = build([slide], objects=[(excel_object([CHART], b"Excel.Chart.8"), {"compressed": True})])
    assert "Chart: bar — Sales" in out and "|Q2|2|" in out, out
    assert out.index("above") < out.index("Chart: bar") < out.index("below"), out
    assert not ctx.skipped, dict(ctx.skipped)
    # the same workbook raw (instance 0), as a sheet
    out, ctx, _ = build([{"shapes": [obj()]}], objects=[excel_object([GRID], b"Excel.Sheet.8")])
    assert "|1|2|\n|---|---|\n|3|4|" in out and not ctx.skipped, (out, dict(ctx.skipped))


def test_msgraph_in_ppt():
    out, ctx, _ = build([{"shapes": [obj()]}], objects=[(msgraph_object(*GRAPH), {"compressed": True})])
    assert "Chart: bar\n||East|West|\n|---|---|---|\n|Q1|1|3|\n|Q2|2|4|" in out, out
    assert not ctx.skipped, dict(ctx.skipped)


def test_equation_in_ppt():
    stg = cfb({"Equation Native": eq_native(X)})
    out, ctx, _ = build([{"shapes": [obj()]}], objects=[stg])
    assert "$x$" in out and not ctx.skipped, (out, dict(ctx.skipped))
    out, ctx, _ = build([{"shapes": [obj()]}], objects=[(stg, {"compressed": True})])
    assert "$x$" in out and not ctx.skipped, (out, dict(ctx.skipped))
    # an equation that does not convert is not read: counted, nothing placed
    bad = cfb({"Equation Native": eq_native(b"garbage, not MTEF")})
    out, ctx, _ = build([{"shapes": [obj()]}], objects=[bad])
    assert "$" not in out and "(could not read: 1 embedded object)" in out, out


def test_unknown_object():
    stg = cfb({"Contents": b"opaque"})
    slide = {"shapes": [{"text": "kept", "box": (0, 10, 400, 50)}, obj(pib=1)]}
    out, ctx, assets = build([slide], objects=[stg], pictures=[blip_png(PNG)])
    assert assets == {"img-001.png": PNG} and "kept" in out, (out, assets)
    assert "(could not read: 1 embedded object)" in out and "could not read: 1 image" not in out, out
    # no preview: still counted
    out, ctx, assets = build([{"shapes": [obj()]}], objects=[stg])
    assert not assets and "(could not read: 1 embedded object)" in out, out
    # an id no ExEmbed names
    out, ctx, assets = build([{"shapes": [obj(7, pib=1)]}], objects=[stg], pictures=[blip_png(PNG)])
    assert assets == {"img-001.png": PNG} and "(could not read: 1 embedded object)" in out, out


def test_bad_zlib_counted():
    slide = {"title": "T", "shapes": [{"text": "still here", "box": (0, 10, 400, 50)}, obj(pib=1)]}
    garbage = struct_size(1000) + b"this is not zlib data"
    out, ctx, assets = build([slide], objects=[{"compressed": True, "body": garbage}], pictures=[blip_png(PNG)])
    assert "still here" in out and "(could not read: 1 embedded object)" in out, out
    assert assets == {"img-001.png": PNG}, assets
    # not a compound file, and a stream cut short
    for body in (b"not a compound file at all", b"\x01"):
        out, ctx, _ = build([{"shapes": [{"text": "ok", "box": (0, 10, 4, 5)}, obj()]}], objects=[{"body": body}])
        assert "ok" in out and "(could not read: 1 embedded object)" in out, out


def test_failed_workbook_shows_preview():
    junk = cfb({"Workbook": b"junk", "CompObj": b"x"})
    slide = {"shapes": [obj(pib=1)]}
    out, ctx, assets = build([slide], objects=[junk], pictures=[blip_png(PNG)])
    assert assets == {"img-001.png": PNG} and "could not read: 1 chart" in out, (out, assets)
    assert ctx.skipped["object"] == 0 and ctx.skipped["chart"] == 1, dict(ctx.skipped)
    # placed on two slides: read once, a preview and a chart count at each placement
    out, ctx, assets = build([slide, slide], objects=[junk], pictures=[blip_png(PNG)])
    assert out.count("![](assets/") == 2 and "could not read: 2 charts" in out, out
    assert ctx.skipped["object"] == 0 and ctx.skipped["chart"] == 2, dict(ctx.skipped)
    # no preview: the chart is still counted
    out, ctx, assets = build([{"shapes": [obj()]}], objects=[junk])
    assert not assets and ctx.skipped["chart"] == 1 and ctx.skipped["object"] == 0, dict(ctx.skipped)


def test_truncated_storage_counted():
    stg = excel_object([GRID], b"Excel.Sheet.8")[:600]
    out, ctx, _ = build([{"shapes": [{"text": "ok", "box": (0, 10, 4, 5)}, obj()]}], objects=[stg])
    assert "ok" in out and "(could not read: 1 embedded object)" in out, out


def test_object_in_encrypted_ppt():
    out, ctx, _ = build([{"shapes": [obj()]}], encrypt=PPT_DEFAULT,
                        objects=[(excel_object([CHART], b"Excel.Chart.8"), {"compressed": True})])
    assert "Chart: bar — Sales" in out and not ctx.skipped, (out, dict(ctx.skipped))


def struct_size(n):
    import struct
    return struct.pack("<I", n)


def test_object_read_once_placed_twice():
    slides = [{"shapes": [obj()]}, {"shapes": [obj()]}]
    out, ctx, _ = build(slides, objects=[excel_object([CHART], b"Excel.Chart.8")])
    assert out.count("Chart: bar — Sales") == 2 and not ctx.skipped, (out, dict(ctx.skipped))
    # a bad object is counted at each placement, read once
    slides = [{"shapes": [obj()]}, {"shapes": [obj()]}]
    out, ctx, _ = build(slides, objects=[cfb({"Contents": b"x"})])
    assert ctx.skipped["object"] == 2, dict(ctx.skipped)


def test_object_cap():
    stg = excel_object([GRID], b"Excel.Sheet.8")
    n = ppt_objects.MAX_OBJECTS + 3
    slide = {"shapes": [obj(k + 1) for k in range(n)]}
    out, ctx, _ = build([slide], objects=[stg] * n)
    assert ctx.skipped["object"] == 3, dict(ctx.skipped)
    assert out.count("|1|2|") == ppt_objects.MAX_OBJECTS, out.count("|1|2|")


def test_embed_bytes_cap():
    old = ppt_objects.limits.EMBED_BYTES
    stg = cfb({"Contents": b"x" * 600})
    ppt_objects.limits.EMBED_BYTES = len(stg) - 1
    try:
        for compressed in (False, True):
            out, ctx, _ = build([{"shapes": [obj()]}], objects=[(excel_object([GRID]), {"compressed": compressed})])
            assert "(could not read: 1 embedded object)" in out, (compressed, out)
    finally:
        ppt_objects.limits.EMBED_BYTES = old


def test_compressed_storage_inflates_only_to_embed_bytes():
    # the size the body claims is small, the zlib data is not: inflation stops at EMBED_BYTES, not after the bomb
    import struct
    import zlib
    from reader.containers import flate
    from reader.limits import ReadError
    bomb = zlib.compress(bytes(500_000))
    old, real = ppt_objects.limits.EMBED_BYTES, flate.inflate
    sizes = []

    def spy(data, budget, limit=None):
        try:
            # the two-argument form when no limit is passed, so a reader without the limit fails on the size
            out = real(data, budget) if limit is None else real(data, budget, limit)
        except ReadError:
            sizes.append(-1)
            raise
        sizes.append(len(out))
        return out
    ppt_objects.limits.EMBED_BYTES = 100_000
    ppt_objects.flate.inflate = spy
    try:
        body = struct.pack("<I", 10) + bomb
        out, ctx, _ = build([{"shapes": [obj()]}], objects=[{"data": b"", "compressed": True, "body": body}])
    finally:
        ppt_objects.limits.EMBED_BYTES = old
        ppt_objects.flate.inflate = real
    assert "(could not read: 1 embedded object)" in out, out
    assert sizes and max(sizes) <= 100_000, sizes  # -1: stopped on the limit; never a 500,000-byte result


def test_flate_limit():
    import zlib
    from reader.containers import flate
    from reader.limits import ReadError
    bomb = zlib.compress(bytes(500_000))
    budget = flate.Budget()
    try:
        flate.inflate(bomb, budget, 1000)
    except ReadError as e:
        assert e.kind == "too_large", e.kind
    else:
        raise AssertionError("inflated past the limit")
    assert budget.used == 0, budget.used
    assert len(flate.inflate(bomb, budget, 500_000)) == 500_000  # exactly the limit is allowed


def test_chart_total_charged():
    old = ppt_objects.limits.CHART_TOTAL
    ppt_objects.limits.CHART_TOTAL = 6  # the grid holds 4 cells
    try:
        slides = [{"shapes": [obj(pib=1)]}, {"shapes": [obj(pib=1)]}]
        out, ctx, assets = build(slides, objects=[excel_object([GRID], b"Excel.Sheet.8")], pictures=[blip_png(PNG)])
    finally:
        ppt_objects.limits.CHART_TOTAL = old
    # the placement past CHART_TOTAL shows the shape's preview and is counted under chart, as an unread workbook is
    assert out.count("|1|2|") == 1 and "(could not read: 1 chart)" in out, out
    assert assets == {"img-001.png": PNG} and out.count("![](assets/") == 1, (out, assets)
    assert ctx.skipped["chart"] == 1 and ctx.skipped["object"] == 0, dict(ctx.skipped)


def test_failed_workbook_leaves_no_warning():
    # a failure the guard counts leaves a warning; the unread chart is counted where it is placed, with no warning
    from reader.formats import xls_embedded
    old = xls_embedded._read
    xls_embedded._read = lambda *a: (_ for _ in ()).throw(ValueError("boom"))
    try:
        out, ctx, _ = build([{"shapes": [obj()]}], objects=[excel_object([GRID], b"Excel.Sheet.8")])
    finally:
        xls_embedded._read = old
    assert dict(ctx.skipped) == {"chart": 1} and ctx.warnings == [], (dict(ctx.skipped), ctx.warnings)


def test_shared_helper_with_doc():
    from reader.formats import doc_objects, mtef
    assert hasattr(mtef, "native_tex") and doc_objects.mtef is mtef


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
