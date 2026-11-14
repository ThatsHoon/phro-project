"""xlsx workbooks: the sheet list, styles, shared strings, cell types and the streamed sheet walk under SHEET_SCAN.

Run: python -I -X utf8 tests/test_reader_xlsx.py
"""
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.formats import ooxml  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import XLSX_REL, XLSX_SML, make_xlsx, xlsx_cell, xlsx_row  # noqa: E402

MACROSHEET = "http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"
DOCTYPE = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'


def md(sheets, hidden=False, name="a.xlsx", **kw):
    """Build an .xlsx, parse it, render it: (markdown, ctx)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / name
        make_xlsx(path, sheets, **kw)
        ctx = Context()
        doc = ooxml.parse(path, ctx)
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped), ctx


def parsed(sheets, **kw):
    """Parse an in-memory .xlsx: (doc, ctx, seconds)."""
    data = make_xlsx(None, sheets, **kw)
    ctx = Context()
    t0 = time.perf_counter()
    doc = ooxml.parse_package(data, ctx)
    return doc, ctx, time.perf_counter() - t0


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


@contextmanager
def patched(name, value):
    old = getattr(limits, name)
    setattr(limits, name, value)
    try:
        yield
    finally:
        setattr(limits, name, old)


def col_name(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def sheet(rows, **kw):
    return {"name": kw.pop("name", "S"), "rows": rows, **kw}


def warned(ctx, text):
    return any(text in w for w in ctx.warnings)


def test_cell_types():
    rows = xlsx_row(1, xlsx_cell("A1", "3.5"), xlsx_cell("B1", 0, t="s"), xlsx_cell("C1", "res", t="str"),
                    xlsx_cell("D1", inline="<r><t>a</t></r><r><t>b</t></r>"), xlsx_cell("E1", 1, t="b"),
                    xlsx_cell("F1", "#DIV/0!", t="e"), xlsx_cell("G1", "2024-03-05T00:00:00", t="d"))
    out, ctx = md([sheet(rows)], shared=["<t>hi</t>"])
    assert "|3.5|hi|res|ab|TRUE|#DIV/0!|2024-03-05T00:00:00|" in out and not ctx.skipped, (out, ctx.skipped)


def test_number_formats():
    rows = xlsx_row(1, xlsx_cell("A1", 0.125, s=1), xlsx_cell("B1", 45356, s=2),
                    xlsx_cell("C1", "2024-03-05", t="d", s=2), xlsx_cell("D1", 0.5, s=9))
    out, ctx = md([sheet(rows)], numfmts={164: "0.00%"}, xfs=[0, 164, 14])
    assert "|12.50%|2024-03-05|2024-03-05|0.5|" in out, out


def test_date1904():
    rows = xlsx_row(1, xlsx_cell("A1", 0, s=1), xlsx_cell("B1", 1, s=1))
    out, _ = md([sheet(rows)], xfs=[0, 14], date1904=True)
    assert "|1904-01-01|1904-01-02|" in out, out


def test_rich_and_phonetic():
    si = '<r><t>a</t></r><r><rPr><b/></rPr><t>b</t></r><rPh sb="0" eb="1"><t>x</t></rPh><phoneticPr fontId="0"/>'
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 1, t="s"))
    out, _ = md([sheet(rows)], shared=[si, "<t>y</t>"])
    assert "|ab|y|" in out and "x" not in out.replace("xlsx", ""), out


def test_escapes():
    shared = ["<t>a_x000D_\nb</t>", "<t>_x005F_x000D_</t>", "<t>_xD83D__xDE00_ _xD800_</t>"]
    rows = xlsx_row(1, *(xlsx_cell(f"{col_name(i)}1", i, t="s") for i in range(3)))
    out, _ = md([sheet(rows)], shared=shared)
    assert "|a<br>b|" in out and out.count("x000D") == 1, out
    assert "\U0001F600 �" in out, out


def test_formula_cache_wins():
    rows = xlsx_row(1, xlsx_cell("A1", 2, f="1+1"), xlsx_cell("B1", "k", t="str", f='"k"'))
    out, _ = md([sheet(rows)])
    assert "|2|k|" in out and "1+1" not in out, out


def test_formula_without_cache():
    rows = xlsx_row(1, xlsx_cell("A1", f="SUM(A1:A2)"), xlsx_cell("B1", 3))
    out, _ = md([sheet(rows)])
    assert "|=SUM(A1:A2)|3|" in out, out


def test_shared_formula_empty():
    rows = (xlsx_row(1, xlsx_cell("A1", f='<f t="shared" si="0"/>'), xlsx_cell("B1", 5))
            + xlsx_row(2, xlsx_cell("A2", 7)))
    out, _ = md([sheet(rows)])
    assert "||5|" in out and "|7||" in out and "=" not in out, out


def test_hidden_sheets():
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "seen", t="str")), name="V"),
              sheet(xlsx_row(1, xlsx_cell("A1", "secret1", t="str")), name="H", state="hidden"),
              sheet(xlsx_row(1, xlsx_cell("A1", "secret2", t="str")), name="W", state="veryHidden")]
    out, ctx = md(sheets)
    assert "## Sheet: V" in out and "seen" in out and "secret" not in out, out
    assert "## Sheet: H (hidden, left out)" in out and "## Sheet: W (hidden, left out)" in out, out
    out, ctx = md(sheets, hidden=True)
    assert "## Sheet: H (hidden)" in out and "## Sheet: W (hidden)" in out and "secret2" in out, out
    assert not ctx.skipped, ctx.skipped


def test_sheet_order():
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", 2)), name="B", number=2),
              sheet(xlsx_row(1, xlsx_cell("A1", 1)), name="A", number=1)]
    out, _ = md(sheets)
    assert "## Sheet: B" in out and out.index("## Sheet: B") < out.index("## Sheet: A"), out


def test_styled_empty_cells_cut():
    empty = "".join(xlsx_row(r, *['<c s="1"/>'] * 100) for r in range(1, 51))
    sheets = [sheet(empty + xlsx_row(51, xlsx_cell("A51", 0, t="s")), name="S1"),
              sheet(xlsx_row(1, xlsx_cell("A1", 1, t="s")), name="S2")]
    with patched("SHEET_SCAN", 1000):
        out, ctx = md(sheets, shared=["<t>late</t>", "<t>two</t>"], xfs=[0, 0])
    assert "late" not in out and "two" not in out, out
    assert warned(ctx, "cut at 1000 scanned XML elements") and ctx.skipped["sheet_cut"] == 2, (ctx.warnings,
                                                                                             ctx.skipped)


def test_styled_empty_cells_fast():
    rows = "".join(xlsx_row(r, *(f'<c r="{col_name(c)}{r}" s="1"/>' for c in range(1000))) for r in range(1, 201))
    rows += xlsx_row(201, xlsx_cell("A201", 0, t="s"))
    doc, ctx, secs = parsed([sheet(rows)], shared=["<t>end</t>"], xfs=[0, 0])
    out = markdown.render(doc, Path(tempfile.gettempdir()) / "unused-assets", False, ctx.skipped)
    assert secs < 2.0 and "end" in out and not ctx.skipped and not ctx.warnings, (secs, ctx.skipped, ctx.warnings)


def test_shared_strings_past_cap():
    shared = [f"<t>{ch * 10}</t>" for ch in "abcdefghij"]
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 9, t="s"), xlsx_cell("C1", 4, t="s"))
    with patched("SHEET_CHARS", 50):
        out, ctx = md([sheet(rows)], shared=shared)
    assert "aaaaaaaaaa" in out and "eeeeeeeeee" in out and "jjjjjjjjjj" not in out, out
    assert ctx.skipped["sheet_cut"] == 1 and warned(ctx, "shared strings cut at 50 characters"), (ctx.warnings,
                                                                                                ctx.skipped)


def test_shared_strings_entry_cap():
    rows = xlsx_row(1, xlsx_cell("A1", 1000, t="s"), xlsx_cell("B1", 99, t="s"))
    shared = [""] * 99 + ["<t>kept</t>"] + [""] * 900 + ["<t>zz9</t>"]
    with patched("SHEET_SCAN", 100):
        doc, ctx, secs = parsed([sheet(rows)], shared=shared)
    out = markdown.render(doc, Path(tempfile.gettempdir()) / "unused-assets", False, ctx.skipped)
    assert "zz9" not in out and "kept" in out and secs < 1.0, (out, secs)
    assert ctx.skipped["sheet_cut"] == 1 and warned(ctx, "shared strings cut at 100 entries"), (ctx.warnings,
                                                                                              ctx.skipped)


def test_doctype_in_sheet_part():
    bad = DOCTYPE + f'<worksheet xmlns="{XLSX_SML}"><sheetData/></worksheet>'
    sheets = [sheet("", name="A", part=bad.encode()), sheet(xlsx_row(1, xlsx_cell("A1", "ok", t="str")), name="B")]
    out, ctx = md(sheets)
    assert "ok" in out and "(could not read: 1 sheet)" in out and ctx.skipped["sheet"] == 1, (out, ctx.skipped)
    wb = (DOCTYPE + f'<workbook xmlns="{XLSX_SML}" xmlns:r="{XLSX_REL}"><sheets>'
          '<sheet name="A" sheetId="1" r:id="rId1"/></sheets></workbook>')
    kind, _ = denied(lambda: md(sheets, extra_parts={"xl/workbook.xml": wb}))
    assert kind == "damaged", kind


def test_xml_error_keeps_rows():
    rows = xlsx_row(1, xlsx_cell("A1", "one", t="str")) + xlsx_row(2, xlsx_cell("A2", "two", t="str"))
    part = f'<?xml version="1.0"?><worksheet xmlns="{XLSX_SML}"><sheetData>{rows}<row r="3"><c r="A3"><v>th'
    out, ctx = md([sheet("", part=part)])
    assert "one" in out and "two" in out and "th" not in out.replace("Sheet", ""), out
    assert ctx.skipped["sheet_cut"] == 1 and warned(ctx, "cut at 1 XML error") and not ctx.skipped["sheet"], (
        ctx.warnings, ctx.skipped)


def test_failed_sheet_among_good():
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "one", t="str")), name="A"),
              sheet("", name="B", missing=True),
              sheet(xlsx_row(1, xlsx_cell("A1", "three", t="str")), name="C")]
    out, ctx = md(sheets)
    assert "one" in out and "three" in out and "(could not read: 1 sheet)" in out, out
    assert ctx.skipped["sheet"] == 1, ctx.skipped


def test_no_readable_sheet():
    assert denied(lambda: md([sheet("", part=b"not xml")])) == ("damaged", "no readable sheets")


def test_macrosheet_counted():
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "macro", t="str")), name="M", rel_type=MACROSHEET),
              sheet(xlsx_row(1, xlsx_cell("A1", "ok", t="str")), name="W")]
    out, ctx = md(sheets)
    assert ctx.skipped["sheet"] == 1 and "ok" in out and "macro" not in out, (out, ctx.skipped)


def measure(n=2_000_000):
    """Not a test: one sheet of n styled empty cells (1,000 a row) through the whole reader."""
    rows = "".join(xlsx_row(r, *(f'<c r="{col_name(c)}{r}" s="1"/>' for c in range(1000)))
                   for r in range(1, n // 1000 + 1))
    _, ctx, secs = parsed([sheet(rows)], xfs=[0, 0])
    print(f"SCAN {n} {secs:.2f}")


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
