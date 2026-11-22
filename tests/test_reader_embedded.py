"""Tests for reader.embedded: one dispatcher for the objects a document embeds."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import embedded, limits  # noqa: E402  (ModuleNotFoundError before embedded.py exists)
from reader.formats import sheet_grid  # noqa: E402
from reader.model import Context, Table, Text  # noqa: E402
from reader_fix import cfb_bytes, comp_obj, eq_native, make_xlsx, make_zip, xlsx_cell, xlsx_row  # noqa: E402
import test_reader_doc_objects as in_doc  # noqa: E402
import test_reader_odt as odt  # noqa: E402
import test_reader_ppt_objects as in_ppt  # noqa: E402

CHART_MIME = "application/vnd.oasis.opendocument.chart"
FORMULA_MIME = "application/vnd.oasis.opendocument.formula"
MATH = (f'<math:math {odt.NS}><math:semantics><math:mi>a</math:mi>'
        '<math:annotation math:encoding="StarMath 5.0">a over b</math:annotation></math:semantics></math:math>')


def chart_package(title="매출"):
    return make_zip(None, {"mimetype": CHART_MIME, "content.xml": odt.chart_xml(title=title)})


def workbook(active=0):
    rows = [xlsx_row(1, xlsx_cell("A1", inline="<t>Item</t>"), xlsx_cell("B1", inline="<t>Qty</t>")),
            xlsx_row(2, xlsx_cell("A2", inline="<t>Pen</t>"), xlsx_cell("B2", "3"))]
    other = xlsx_row(1, xlsx_cell("A1", inline="<t>Other</t>"), xlsx_cell("B1", "9"))
    book = f'<bookViews><workbookView activeTab="{active}"/></bookViews>' if active else ""
    return make_xlsx(None, [{"name": "Data", "rows": "".join(rows)}, {"name": "Second", "rows": other}], book=book)


def read(data, depth=0):
    ctx = Context()
    return embedded.read(data, ctx, sheet_grid.Budget(), depth), ctx


def cells(tables):
    assert all(isinstance(t, Table) for t in tables), tables
    return [["".join(i.text for p in cell.blocks for i in p.inlines if isinstance(i, Text)) for cell in row]
            for t in tables for row in t.rows]


def test_xlsx_gives_its_active_sheets_tables():
    got, ctx = read(workbook())
    assert got[0] == "table" and cells(got[1]) == [["Item", "Qty"], ["Pen", "3"]] and not ctx.skipped, (got, ctx.skipped)
    got, _ = read(workbook(active=1))
    assert got[0] == "table" and cells(got[1]) == [["Other", "9"]], got


def test_odf_chart_and_formula_packages():
    got, ctx = read(chart_package())
    assert got[0] == "table" and got[1][0].caption == "Chart: column — 매출" and not ctx.skipped, (got, ctx.skipped)
    assert cells(got[1]) == [["연도", "값"], ["2025", "3"]], cells(got[1])
    got, _ = read(make_zip(None, {"mimetype": FORMULA_MIME, "content.xml": MATH}))
    assert got == ("equation", "a over b", frozenset()), got


def test_compound_objects():
    blob, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
    got, _ = read(blob)
    assert got == ("equation", "x", frozenset()), got
    for name in ("package_stream", "EmbeddedOdf", "Package"):
        blob, _ = cfb_bytes({name: chart_package(), "\x01CompObj": comp_obj(b"opendocument.ChartDocument.1")})
        got, _ = read(blob)
        assert got[0] == "table" and got[1][0].caption == "Chart: column — 매출", (name, got)
    blob, _ = cfb_bytes({"Package": workbook()})
    got, _ = read(blob)
    assert got[0] == "table" and cells(got[1]) == [["Item", "Qty"], ["Pen", "3"]], got
    blob, _ = cfb_bytes({"\x01CompObj": comp_obj(b"MSGraph.Chart.8")})
    assert read(blob)[0] == ("chart", [], frozenset())


def test_what_is_not_read_gives_none():
    for data in (b"", b"plain bytes", cfb_bytes({"CONTENTS": b"%PDF-1.4"})[0], b"PK\x03\x04" + b"\0" * 40,
                 make_zip(None, {"mimetype": CHART_MIME, "content.xml": "<a"}),
                 make_zip(None, {"word/document.xml": "<w/>"})):
        got, ctx = read(data)
        assert got is None and not ctx.skipped and not ctx.warnings, (data[:20], got, ctx.skipped, ctx.warnings)


def test_nesting_and_size_are_bounded():
    blob = chart_package()
    for _ in range(embedded.DEPTH + 1):
        blob, _ = cfb_bytes({"Package": blob})
    assert read(blob)[0] is None
    inner, _ = cfb_bytes({"Package": chart_package()})
    assert read(inner, embedded.DEPTH - 1)[0] is not None and read(inner, embedded.DEPTH)[0] is None
    saved = limits.EMBED_BYTES
    limits.EMBED_BYTES = 64
    try:
        assert read(chart_package())[0] is None and read(inner)[0] is None
    finally:
        limits.EMBED_BYTES = saved


def test_chart_package_in_a_doc():
    p = in_doc.embed_doc({1: {"package_stream": chart_package()}}, preview=in_doc.PNG_PREVIEW)
    out = in_doc.md(p)
    assert out == "Chart: column — 매출\n연도|값\n-|-\n2025|3\n", out
    p = in_doc.embed_doc({1: {"Package": workbook()}}, preview=in_doc.PNG_PREVIEW)
    assert in_doc.md(p) == "A1:B2\nItem|Qty\n-|-\nPen|3\n", in_doc.md(p)


def test_chart_package_in_a_ppt():
    stg, _ = cfb_bytes({"package_stream": chart_package()})
    slide = {"shapes": [{"text": "above", "box": (0, 10, 400, 50)}, in_ppt.obj(pib=1)]}
    out, ctx, assets = in_ppt.build([slide], objects=[stg], pictures=[in_ppt.blip_png(in_ppt.PNG)])
    assert "Chart: column — 매출\n연도|값\n-|-\n2025|3\n" in out and not assets and not ctx.skipped, (out, assets, ctx.skipped)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
