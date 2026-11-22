"""xlsx workbooks: the sheet list, styles, shared strings, cell types and the streamed sheet walk under SHEET_SCAN.

Run: python -I -X utf8 tests/test_reader_xlsx.py
"""
import io
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.formats import ooxml, ooxml_sheets  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Hidden, Link, NoteRef, Para, Table, Text  # noqa: E402
from reader_fix import XLSX_REL, XLSX_SML, make_xlsx, xlsx_cell, xlsx_row  # noqa: E402
import test_reader_xlsb as xb  # noqa: E402  (the drawing, anchor and chart fixtures)

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
def patched(name, value, module=limits):
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


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
    assert "\n3.5|hi|res|ab|TRUE|#DIV/0!|2024-03-05T00:00:00\n" in out and not ctx.skipped, (out, ctx.skipped)


def test_number_formats():
    rows = xlsx_row(1, xlsx_cell("A1", 0.125, s=1), xlsx_cell("B1", 45356, s=2),
                    xlsx_cell("C1", "2024-03-05", t="d", s=2), xlsx_cell("D1", 0.5, s=9))
    out, ctx = md([sheet(rows)], numfmts={164: "0.00%"}, xfs=[0, 164, 14])
    assert "\n12.50%|2024-03-05|2024-03-05|0.5\n" in out, out


def test_date1904():
    rows = xlsx_row(1, xlsx_cell("A1", 0, s=1), xlsx_cell("B1", 1, s=1))
    out, _ = md([sheet(rows)], xfs=[0, 14], date1904=True)
    assert "\n1904-01-01|1904-01-02\n" in out, out


def test_rich_and_phonetic():
    si = '<r><t>a</t></r><r><rPr><b/></rPr><t>b</t></r><rPh sb="0" eb="1"><t>x</t></rPh><phoneticPr fontId="0"/>'
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 1, t="s"))
    out, _ = md([sheet(rows)], shared=[si, "<t>y</t>"])
    assert "\nab|y\n" in out and "x" not in out.replace("xlsx", ""), out


def test_escapes():
    shared = ["<t>a_x000D_\nb</t>", "<t>_x005F_x000D_</t>", "<t>_xD83D__xDE00_ _xD800_</t>"]
    rows = xlsx_row(1, *(xlsx_cell(f"{col_name(i)}1", i, t="s") for i in range(3)))
    out, _ = md([sheet(rows)], shared=shared)
    assert "\na<br>b|_x000D_|" in out and out.count("x000D") == 1, out
    assert "\U0001F600 �" in out, out


def test_formula_cache_wins():
    rows = xlsx_row(1, xlsx_cell("A1", 2, f="1+1"), xlsx_cell("B1", "k", t="str", f='"k"'))
    out, _ = md([sheet(rows)])
    assert "\n2|k\n" in out and out.count("1+1") == 1 and 'Formulas: A1 =1+1; B1 ="k"' in out, out


def test_formula_without_cache():
    rows = xlsx_row(1, xlsx_cell("A1", f="SUM(A1:A2)"), xlsx_cell("B1", 3))
    out, _ = md([sheet(rows)])
    assert "\n=SUM(A1:A2)|3\n" in out, out


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
    # styled empties past SHEET_ROWS end their sheet at the first row it cannot hold: the scan count is kept for S2
    empty = "".join(xlsx_row(r, *['<c s="1"/>'] * 100) for r in range(1, 51))
    sheets = [sheet(empty + xlsx_row(51, xlsx_cell("A51", 0, t="s")), name="S1"),
              sheet(xlsx_row(1, xlsx_cell("A1", 1, t="s")), name="S2")]
    with patched("SHEET_SCAN", 1000), patched("SHEET_ROWS", 5):
        out, ctx = md(sheets, shared=["<t>late</t>", "<t>two</t>"], xfs=[0, 0])
    assert "late" not in out and "two" in out, out
    assert warned(ctx, "sheet S1 cut at 5 rows") and not warned(ctx, "scanned XML elements"), ctx.warnings
    assert dict(+ctx.skipped) == {"sheet_cut": 1}, ctx.skipped


def test_styled_rows_past_the_last_row_keep_later_sheets():
    # one value, then one styled empty column down far past SHEET_ROWS, then a sheet of data
    tail = "".join(f'<row r="{r}"><c r="B{r}" s="1"/></row>' for r in range(2, 3001))
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "first", t="str")) + tail, name="A"),
              sheet(xlsx_row(1, xlsx_cell("A1", "data", t="str")), name="B")]
    with patched("SHEET_SCAN", 2000), patched("SHEET_ROWS", 100):
        out, ctx = md(sheets, xfs=[0, 0])
    assert "first" in out and "## Sheet: B\n\nA1: data" in out, out
    assert "A1: first\n\n(Sheet cut at row 101 by the 100-row limit; not read from there on)" in out, out
    assert warned(ctx, "sheet A cut at 100 rows") and dict(+ctx.skipped) == {"sheet_cut": 1}, (ctx.warnings,
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
    assert "A1:A2\none\ntwo\n\n(Sheet cut at row 3 by damaged XML; not read from there on)" in out, out
    assert "th" not in out.replace("Sheet", "").replace("there", ""), out
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


# ---- fix round 1


def test_shared_strings_element_cap():
    # 2 x SHEET_SCAN closed elements: a run flood inside one si stops the table there
    shared = ["<t>a</t>", "<r/>" * 250, "<t>c</t>"]
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 2, t="s"), xlsx_cell("C1", 3))
    with patched("SHEET_SCAN", 100):
        out, ctx = md([sheet(rows)], shared=shared)
    assert "\na||3\n" in out, out
    assert warned(ctx, "shared strings cut at 200 XML elements") and ctx.skipped["sheet_cut"] == 1, (ctx.warnings,
                                                                                                   ctx.skipped)


def test_styles_element_cap():
    flood = "<xf/>" * 100_000  # with the two xf above, past a styles cap of 100,000 closed elements
    styles = (f'<styleSheet xmlns="{XLSX_SML}"><cellXfs><xf numFmtId="0"/><xf numFmtId="10"/>{flood}</cellXfs>'
              "</styleSheet>")
    rows = xlsx_row(1, xlsx_cell("A1", 0.5, s=1), xlsx_cell("B1", 2))
    with patched("STYLE_ELEMENTS", 100_000, ooxml_sheets):
        doc, ctx, secs = parsed([sheet(rows)], xfs=[0], extra_parts={"xl/styles.xml": styles})
    out = markdown.render(doc, Path(tempfile.gettempdir()) / "unused-assets", False, ctx.skipped)
    assert "\n50.00%|2\n" in out and ctx.skipped["document"] == 1 and secs < 2.0, (out, ctx.skipped, secs)
    assert "(could not read: 1 document part)" in out, out


def bloated_styles(n, after=""):
    """A styles part whose `cellStyleXfs` holds n style xfs (three closes each) ahead of `cellXfs`; xf 1 is yyyy-mm-dd."""
    style_xfs = "".join(f'<xf numFmtId="0" fontId="{i % 977}" applyAlignment="1"><alignment vertical="center"/>'
                        f'<protection locked="{i % 2}"/></xf>' for i in range(n))
    return (f'<styleSheet xmlns="{XLSX_SML}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
            f'</numFmts><cellStyleXfs count="{n}">{style_xfs}</cellStyleXfs>'
            f'<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>{after}'
            '</styleSheet>')


def test_bloated_cell_style_xfs_keep_formats():
    # 64,000 style xfs (about Excel's format limit) close 192,000 elements before `cellXfs` is reached
    rows = xlsx_row(1, xlsx_cell("A1", 45231, s=1))
    out, ctx = md([sheet(rows)], extra_parts={"xl/styles.xml": bloated_styles(64_000)})
    assert "2023-11-01" in out and "45231" not in out and not ctx.skipped, (out, ctx.skipped)


def test_styles_stop_after_cell_xfs():
    # what follows `cellXfs` is never used: it is not read, so it cannot reach the cap
    rows = xlsx_row(1, xlsx_cell("A1", 45231, s=1))
    tail = "<dxfs>" + "<dxf/>" * 500 + "</dxfs>"
    with patched("STYLE_ELEMENTS", 100, ooxml_sheets):
        out, ctx = md([sheet(rows)], extra_parts={"xl/styles.xml": bloated_styles(10, tail)})
    assert "2023-11-01" in out and not ctx.skipped, (out, ctx.skipped)


def test_styles_damaged_counts_document():
    rows = xlsx_row(1, xlsx_cell("A1", 45356, s=1), xlsx_cell("B1", 2))
    bad = DOCTYPE + f'<styleSheet xmlns="{XLSX_SML}"/>'
    out, ctx = md([sheet(rows)], xfs=[0, 14], extra_parts={"xl/styles.xml": bad})
    assert "\n45356|2\n" in out and ctx.skipped["document"] == 1 and not ctx.skipped["styles"], (out, ctx.skipped)


def test_named_part_missing_counts_document():
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 2))
    out, ctx = md([sheet(rows)], shared=["<t>hi</t>"], extra_parts={"xl/sharedStrings.xml": None})
    assert "hi" not in out and ctx.skipped["document"] == 1, (out, ctx.skipped)
    out, ctx = md([sheet(rows)], numfmts={}, extra_parts={"xl/styles.xml": None})
    assert ctx.skipped["document"] == 1, ctx.skipped


def test_shared_strings_damaged():
    sst = f'<sst xmlns="{XLSX_SML}"><si><t>hi</t></si><si><t>lo'
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 1, t="s"), xlsx_cell("C1", 3))
    out, ctx = md([sheet(rows)], shared=[], extra_parts={"xl/sharedStrings.xml": sst})
    assert "\nhi||3\n" in out and ctx.skipped["document"] == 1, (out, ctx.skipped)


def test_scan_exact_cap_no_cut():
    # worksheet, sheetData, row, c and v: five elements close
    rows = xlsx_row(1, xlsx_cell("A1", 7))
    with patched("SHEET_SCAN", 5):
        out, ctx = md([sheet(rows)])
    assert "7" in out and not ctx.skipped and not ctx.warnings, (out, ctx.skipped, ctx.warnings)
    with patched("SHEET_SCAN", 5):
        out, ctx = md([sheet(rows), sheet(xlsx_row(1, xlsx_cell("A1", 8)), name="T")])
    assert "8" not in out and ctx.skipped["sheet_cut"] == 1 and warned(ctx, "sheet T cut at 5"), (out, ctx.skipped)
    with patched("SHEET_SCAN", 4):
        _, ctx = md([sheet(rows)])
    assert ctx.skipped["sheet_cut"] == 1, ctx.skipped


def test_shared_string_guard_factor():
    # a surrogate pair is 14 raw characters for 1: the table still holds it right up to SHEET_CHARS
    shared = ["<t>" + "a" * 45 + "</t>", "<t>" + "_xD83D__xDE00_" * 3 + "</t>", "<t>_x000D__x000A_</t>", "<t>q</t>"]
    rows = xlsx_row(1, *(xlsx_cell(f"{col_name(i)}1", i, t="s") for i in range(4)))
    with patched("SHEET_CHARS", 50):
        out, ctx = md([sheet(rows)], shared=shared)
    assert "|" + "\U0001F600" * 3 + "|<br>|q\n" in out and not ctx.skipped, (out, ctx.skipped)


def test_unreadable_styles_and_strings_go_on():
    def bad_styles(pkg, part, wb, ctx):
        wb.xf_fmt.extend([0, 10])
        raise ValueError("bug")

    def bad_strings(pkg, part, wb, ctx):
        wb.sst.append("hi")
        raise ValueError("bug")
    rows = xlsx_row(1, xlsx_cell("A1", 0.5, s=1), xlsx_cell("B1", 0, t="s"), xlsx_cell("C1", 3))
    with patched("_styles", bad_styles, ooxml_sheets), patched("_strings", bad_strings, ooxml_sheets):
        out, ctx = md([sheet(rows)], shared=["<t>hi</t>"], xfs=[0, 10])
    assert "\n0.5||3\n" in out and ctx.skipped["document"] == 2, (out, ctx.skipped)


def test_bool_values():
    vals = ["2", "-0.5", "0", "0.0", "True", "FALSE", "maybe"]
    rows = xlsx_row(1, *(xlsx_cell(f"{col_name(i)}1", v, t="b") for i, v in enumerate(vals)))
    out, _ = md([sheet(rows)])
    assert "\nTRUE|TRUE|FALSE|FALSE|TRUE|FALSE|maybe\n" in out, out


def test_unescape_beyond_shared():
    rows = xlsx_row(1, xlsx_cell("A1", "a_x000D_b", t="str"), xlsx_cell("B1", "_x0023_N/A", t="e"),
                    xlsx_cell("C1", f='"_x0041_"'))
    out, _ = md([sheet(rows, name="S_x0031_")])
    assert "## Sheet: S1" in out and '\na<br>b|#N/A|="A"\n' in out and "_x" not in out, out


def test_ref_fallbacks():
    rows = (xlsx_row(2, xlsx_cell("A2", "a", t="str"), xlsx_cell(None, "b", t="str"), xlsx_cell("1A", "c", t="str"))
            + xlsx_row(None, xlsx_cell(None, "d", t="str"))
            + xlsx_row("abc", xlsx_cell(None, "e", t="str"), xlsx_cell(None, "f", t="str"))
            + xlsx_row(0, xlsx_cell("B9", "g", t="str")))
    out, ctx = md([sheet(rows)])
    assert "a|b|c\n-|-|-\n|d|||\n|e|f||\n||g||" in out and not ctx.skipped, out


def test_huge_row_number():
    sheets = [sheet(xlsx_row(99999999999, xlsx_cell("A1", 1)), name="A"), sheet(xlsx_row(1, xlsx_cell("A1", 2)))]
    out, ctx = md(sheets)
    assert warned(ctx, "sheet A cut at 100000 rows") and ctx.skipped["sheet_cut"] == 1 and "2" in out, (out, ctx)


def test_missing_workbook_relationship():
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId2" Type="{XLSX_REL}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "gone", t="str")), name="A"),
              sheet(xlsx_row(1, xlsx_cell("A1", "kept", t="str")), name="B")]
    out, ctx = md(sheets, extra_parts={"xl/_rels/workbook.xml.rels": rels})
    assert "kept" in out and "gone" not in out and ctx.skipped["sheet"] == 1, (out, ctx.skipped)


def test_read_error_kinds():
    real = ooxml_sheets.iterparse

    def failing(kind):
        state = {"done": False}

        def it(stream, events=("end",), chunk=65536):
            for ev, el in real(stream, events, chunk):
                yield ev, el
                if ev == "end" and el.tag.endswith("row") and not state["done"]:
                    state["done"] = True
                    raise ReadError(kind, "injected")
        return it
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "one", t="str")) + xlsx_row(2, xlsx_cell("A2", "two", t="str")),
                    name="A"), sheet(xlsx_row(1, xlsx_cell("A1", "next", t="str")), name="B")]
    with patched("iterparse", failing("timeout"), ooxml_sheets):
        assert denied(lambda: md(sheets))[0] == "timeout"
    for kind in ("too_large", "encrypted", "unsupported"):
        with patched("iterparse", failing(kind), ooxml_sheets):
            out, ctx = md(sheets)
        assert "one" not in out and "next" in out and ctx.skipped["sheet"] == 1, (kind, out, ctx.skipped)
    with patched("iterparse", failing("damaged"), ooxml_sheets):
        out, ctx = md(sheets)
    assert "one" in out and "two" not in out and "next" in out and ctx.skipped["sheet_cut"] == 1, (out, ctx.skipped)

LINK = "https://example.com/a"


def linked(rows, links, rels=True, **kw):
    """md of a sheet whose `after` holds a `hyperlinks` element of raw `hyperlink` XML strings."""
    sh = sheet(rows, after="<hyperlinks>" + "".join(links) + "</hyperlinks>")
    if rels:
        sh["rels"] = [("rL", "hyperlink", LINK, True)]
    return md([sh], **kw)


def texts(blocks) -> list:
    """The text of each paragraph, a Hidden run's text in brackets."""
    out = []
    for b in blocks:
        if isinstance(b, Para):
            out.append("".join(i.text if isinstance(i, Text) else "[" + "".join(t.text for t in i.inlines) + "]"
                               for i in b.inlines))
    return out


ONE = xlsx_row(1, xlsx_cell("A1", "v", t="str"))
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"


def test_defined_names_workbook_first_sheet_scoped_on_the_sheet():
    book = ('<definedNames><definedName name="TaxRate">Lists!$B$1</definedName>'
            '<definedName name="_xlnm._FilterDatabase" localSheetId="1" hidden="1">S!$A$1:$B$3</definedName>'
            '<definedName name="Secret" localSheetId="1" hidden="1">S!$D$1</definedName>'
            '<definedName name="_xlnm.Print_Area" localSheetId="1">S!$A$1:$C$9</definedName>'
            '<definedName name="Local" localSheetId="1">S!$C$1</definedName></definedNames>')
    doc, ctx, _ = parsed([sheet(ONE, name="Lists"), sheet(ONE)], book=book)
    assert texts(doc.blocks[:1]) == ["Names: TaxRate = Lists!$B$1"], doc.blocks[:1]
    assert texts(doc.blocks[2].blocks)[-1] == ("Names: Print_Area = S!$A$1:$C$9; Local = S!$C$1"
                                              "[; Secret = S!$D$1]"), \
        doc.blocks[2].blocks
    assert texts(doc.blocks[1].blocks) == ["A1: v"], doc.blocks[1].blocks


def test_workbook_names_past_the_cap_are_counted():
    book = "<definedNames>" + "".join(f'<definedName name="N{k}">S!$A${k + 1}</definedName>'
                                      for k in range(limits.FACT_ITEMS + 7)) + "</definedNames>"
    doc, ctx, _ = parsed([sheet(ONE)], book=book)
    names = texts(doc.blocks[:1])[0]
    assert names.startswith("Names: N0 = S!$A$1; ") and f"N{limits.FACT_ITEMS} =" not in names, names[-80:]
    assert ctx.skipped["fact"] == 7, ctx.skipped


def test_slicer_and_timeline_cache_names_left_out():
    """The name Excel keeps for each slicer and timeline cache (`Slicer_Product = #N/A`) is its own record, not
    content: a workbook name that a slicerCache or timelineCache part names is left out."""
    book = ('<definedNames><definedName name="NativeTimeline_Date">#N/A</definedName>'
            '<definedName name="Rate">0.07</definedName><definedName name="Slicer_Product">#N/A</definedName>'
            '</definedNames>')
    rels = [("rS", "http://schemas.microsoft.com/office/2007/relationships/slicerCache",
             "slicerCaches/slicerCache1.xml", False),
            ("rT", "http://schemas.microsoft.com/office/2011/relationships/timelineCache",
             "timelineCaches/timelineCache1.xml", False)]
    parts = {"xl/slicerCaches/slicerCache1.xml": f'<slicerCacheDefinition xmlns="{X14}" name="Slicer_Product" '
                                                 'sourceName="Product"/>',
             "xl/timelineCaches/timelineCache1.xml":
                 '<timelineCacheDefinition xmlns="http://schemas.microsoft.com/office/spreadsheetml/2010/11/main" '
                 'name="NativeTimeline_Date" sourceName="Date"/>'}
    doc, ctx, _ = parsed([sheet(ONE)], book=book, wb_rels=rels, extra_parts=parts)
    assert texts(doc.blocks[:1]) == ["Names: Rate = 0.07"] and not ctx.skipped, (doc.blocks[:1], ctx.skipped)


def test_data_validation_lists_and_bounds():
    after = ('<dataValidations count="2"><dataValidation type="list" allowBlank="1" sqref="J5:J8">'
             '<formula1>"Open,Closed,Pending"</formula1></dataValidation>'
             '<dataValidation type="whole" sqref="D5:E8 G1"><formula1>0</formula1><formula2>1000</formula2>'
             '</dataValidation><dataValidation type="decimal" operator="greaterThan" sqref="H1">'
             '<formula1>0.5</formula1></dataValidation></dataValidations>'
             f'<extLst><ext uri="{{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}}" xmlns:x14="{X14}">'
             f'<x14:dataValidations count="1" xmlns:xm="{XM}"><x14:dataValidation type="list">'
             '<x14:formula1><xm:f>Lists!$A$1:$A$3</xm:f></x14:formula1><xm:sqref>B2:B9</xm:sqref>'
             '</x14:dataValidation></x14:dataValidations></ext></extLst>')
    doc, _, _ = parsed([sheet(ONE, after=after)])
    assert texts(doc.blocks[0].blocks)[-1] == ('Validation: J5:J8 list "Open,Closed,Pending"; '
                                              "D5:E8, G1 whole between 0 and 1000; H1 decimal > 0.5; "
                                              "B2:B9 list =Lists!$A$1:$A$3"), doc.blocks[0].blocks


def test_conditional_formats_one_line_a_rule():
    after = ('<conditionalFormatting sqref="F5:F8"><cfRule type="cellIs" dxfId="0" priority="1" '
             'operator="greaterThan"><formula>150</formula></cfRule><cfRule type="expression" priority="2">'
             '<formula>$A5="x"</formula></cfRule></conditionalFormatting><conditionalFormatting sqref="B1:B9 C1">'
             '<cfRule type="top10" rank="3" bottom="1" priority="3"/><cfRule type="colorScale" priority="4">'
             '<colorScale><cfvo type="min"/><cfvo type="max"/><color rgb="FFFF0000"/><color rgb="FF00FF00"/>'
             '</colorScale></cfRule><cfRule type="containsText" operator="containsText" text="ok" priority="5">'
             '<formula>NOT(ISERROR(SEARCH("ok",B1)))</formula></cfRule></conditionalFormatting>')
    doc, _, _ = parsed([sheet(ONE, after=after)])
    assert texts(doc.blocks[0].blocks)[1:] == ['Conditional: F5:F8 cell > 150; F5:F8 formula =$A5="x"; '
                                               'B1:B9, C1 bottom 3; B1:B9, C1 color scale; B1:B9, C1 contains "ok"'], \
        doc.blocks[0].blocks


def test_conditional_formats_of_one_rule_are_one_entry_inside_the_budget():
    after = "".join(f'<conditionalFormatting sqref="A{i}:B{i} D{i}"><cfRule type="cellIs" operator="greaterThan" '
                    f'priority="{i}"><formula>5</formula></cfRule></conditionalFormatting>' for i in range(1, 3001))
    doc, ctx, _ = parsed([sheet(ONE, after=after)])
    got = texts(doc.blocks[0].blocks)[1:]
    first = ", ".join(x for i in range(1, 11) for x in (f"A{i}:B{i}", f"D{i}"))
    assert got == [f"Conditional: {first} and 5,980 more cell > 5"], got[0][:300]


def test_x14_conditional_format_takes_its_ranges_from_its_sqref_child():
    after = ('<conditionalFormatting sqref="D1:D9"><cfRule type="dataBar" priority="1"><dataBar><cfvo type="min"/>'
             '<cfvo type="max"/><color rgb="FF638EC6"/></dataBar></cfRule></conditionalFormatting>'
             f'<extLst><ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}" xmlns:x14="{X14}">'
             f'<x14:conditionalFormattings xmlns:xm="{XM}"><x14:conditionalFormatting>'
             '<x14:cfRule type="dataBar" id="{1}"><x14:dataBar/></x14:cfRule><xm:sqref>D1:D9</xm:sqref>'
             '</x14:conditionalFormatting><x14:conditionalFormatting><x14:cfRule type="expression" priority="2">'
             '<xm:f>Lists!$A1&gt;3</xm:f></x14:cfRule><x14:cfRule type="iconSet" priority="3"/>'
             '<xm:sqref>E1:E9 G1</xm:sqref></x14:conditionalFormatting><x14:conditionalFormatting>'
             '<x14:cfRule type="expression"><xm:f>1</xm:f></x14:cfRule></x14:conditionalFormatting>'
             '</x14:conditionalFormattings></ext></extLst>')
    doc, ctx, _ = parsed([sheet(ONE, after=after)])
    assert texts(doc.blocks[0].blocks)[1:] == ["Conditional: D1:D9 data bar; E1:E9, G1 formula =Lists!$A1>3; "
                                               "E1:E9, G1 icon set"], doc.blocks[0].blocks
    assert ctx.skipped == {"fact": 1}, ctx.skipped


def test_autofilter_range_and_criteria():
    after = ('<autoFilter ref="A1:D41"><filterColumn colId="2"><filters><filter val="Open"/><filter val="Pending"/>'
             '</filters></filterColumn><filterColumn colId="3"><customFilters and="1">'
             '<customFilter operator="greaterThan" val="5"/><customFilter operator="lessThan" val="9"/>'
             '</customFilters></filterColumn></autoFilter>')
    doc, _, _ = parsed([sheet(ONE, after=after)])
    assert texts(doc.blocks[0].blocks)[-1] == "Filter A1:D41: C is Open, Pending; D > 5 and D < 9", \
        doc.blocks[0].blocks
    doc, _, _ = parsed([sheet(ONE, after='<autoFilter ref="A1:B3"/>')])
    assert texts(doc.blocks[0].blocks)[-1] == "Filter: A1:B3", doc.blocks[0].blocks
    after = ('<autoFilter ref="A1:B3"><filterColumn colId="0"><filters blank="1"><filter val="x"/>'
             '<dateGroupItem year="2024" month="3" dateTimeGrouping="month"/>'
             '<dateGroupItem year="2025" month="1" day="9" dateTimeGrouping="day"/>'
             '<dateGroupItem year="2026" dateTimeGrouping="year"/><dateGroupItem dateTimeGrouping="year"/>'
             '<dateGroupItem month="4" dateTimeGrouping="month"/></filters></filterColumn></autoFilter>')
    doc, _, _ = parsed([sheet(ONE, after=after)])
    assert texts(doc.blocks[0].blocks)[-1] == "Filter A1:B3: A is x, 2024-03, 2025-01-09, 2026, (blank)", \
        doc.blocks[0].blocks


def test_sheet_and_workbook_protection_are_flags():
    doc, _, _ = parsed([sheet(ONE, after='<sheetProtection sheet="1" objects="1"/>'),
                        sheet(ONE, name="T", after='<sheetProtection sheet="0"/>')],
                       book='<workbookProtection lockStructure="1" lockWindows="0"/>')
    assert texts(doc.blocks[:1]) == ["Protected: workbook structure"], doc.blocks[:1]
    assert texts(doc.blocks[1].blocks)[-1] == "Protected: sheet", doc.blocks[1].blocks
    assert texts(doc.blocks[2].blocks) == ["A1: v"], doc.blocks[2].blocks


TABLE = ('<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="Table1" '
         'displayName="Orders" ref="A1:D6" totalsRowCount="1"><autoFilter ref="A1:D5"/><tableColumns count="1">'
         '<tableColumn id="1" name="Item"/></tableColumns></table>')


def test_table_parts_give_name_range_and_totals_row():
    sh = sheet(ONE, after='<tableParts count="1"><tablePart r:id="rT"/></tableParts>',
               rels=[("rT", "table", "../tables/table1.xml", False)])
    doc, ctx, _ = parsed([sh], extra_parts={"xl/tables/table1.xml": TABLE})
    assert texts(doc.blocks[0].blocks)[-1] == "Table Orders A1:D6: header row 1; totals row 6", doc.blocks[0].blocks
    assert not ctx.skipped, ctx.skipped


PIVOT = ('<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="PivotSales" '
         'cacheId="1" dataCaption="Values"><location ref="A3:B7" firstHeaderRow="1" firstDataRow="1" '
         'firstDataCol="1"/><pivotFields count="4"><pivotField axis="axisRow"/><pivotField axis="axisPage"/>'
         '<pivotField/><pivotField dataField="1"/></pivotFields><rowFields count="1"><field x="0"/></rowFields>'
         '<colFields count="2"><field x="2"/><field x="-2"/></colFields><pageFields count="1">'
         '<pageField fld="1" hier="-1"/></pageFields><dataFields count="1"><dataField name="Sum of Amount" fld="3"/>'
         '</dataFields></pivotTableDefinition>')
CACHE = ('<pivotCacheDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
         '<cacheSource type="worksheet"><worksheetSource ref="A1:D41" sheet="Data"/></cacheSource>'
         '<cacheFields count="4"><cacheField name="Product"/><cacheField name="Region"/><cacheField name="Date"/>'
         '<cacheField name="Amount"/></cacheFields></pivotCacheDefinition>')


def test_pivot_table_source_and_fields():
    sh = sheet(ONE, rels=[("rP", "pivotTable", "../pivotTables/pivotTable1.xml", False)])
    parts = {"xl/pivotTables/pivotTable1.xml": PIVOT, "xl/pivotCache/pivotCacheDefinition1.xml": CACHE,
             "xl/pivotTables/_rels/pivotTable1.xml.rels":
                 xb.rels_xml(("rId1", "pivotCacheDefinition", "../pivotCache/pivotCacheDefinition1.xml"))}
    doc, ctx, _ = parsed([sh], extra_parts=parts)
    assert texts(doc.blocks[0].blocks)[-1] == ("Pivot PivotSales A3:B7: source Data!A1:D41; rows Product; "
                                              "columns Date; values Sum of Amount; filters Region"), \
        doc.blocks[0].blocks
    assert not ctx.skipped, ctx.skipped


def test_external_link_targets_listed_by_number():
    parts = {"xl/externalLinks/externalLink1.xml":
             f'<externalLink xmlns="{XLSX_SML}" xmlns:r="{XLSX_REL}"><externalBook r:id="rId1"/></externalLink>',
             "xl/externalLinks/_rels/externalLink1.xml.rels":
                 '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                 'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                 '2006/relationships/externalLinkPath" Target="file:///C:/x/budget.xlsx" TargetMode="External"/>'
                 '</Relationships>'}
    doc, ctx, _ = parsed([sheet(ONE)], book='<externalReferences><externalReference r:id="rX"/></externalReferences>',
                         wb_rels=[("rX", "externalLink", "externalLinks/externalLink1.xml", False)],
                         extra_parts=parts)
    assert texts(doc.blocks[:1]) == ["External links: [1] file:///C:/x/budget.xlsx"], doc.blocks[:1]
    assert not ctx.skipped, ctx.skipped


def test_slicer_and_timeline_named_not_their_fallback_text():
    def slicer(kind, uri, prefix, name):
        frame = (f'<xdr:graphicFrame><a:graphic><a:graphicData uri="{uri}"><{prefix}:{kind} xmlns:{prefix}="{uri}" '
                 f'name="{name}"/></a:graphicData></a:graphic></xdr:graphicFrame>')
        fallback = ('<xdr:sp><xdr:txBody><a:p><a:r><a:t>This shape represents a table slicer. Table slicers are '
                    'not supported in this version of Excel.</a:t></a:r></a:p></xdr:txBody></xdr:sp>')
        return (f'<mc:AlternateContent><mc:Choice Requires="{prefix}" xmlns:{prefix}="{uri}">{frame}</mc:Choice>'
                f'<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent>')
    parts = drawn(1, xb.anchor(0, slicer("slicer", "http://schemas.microsoft.com/office/drawing/2010/slicer",
                                         "sle15", "Product")),
                  xb.anchor(5, slicer("timeslicer", "http://schemas.microsoft.com/office/drawing/2012/timeslicer",
                                      "tsle", "Date")))
    doc, ctx, _ = parsed([sheet(ONE, rels=[DRAWING])], extra_parts=parts)
    assert texts(doc.blocks[0].blocks) == ["A1: v", "Slicer: Product", "Timeline: Date"], doc.blocks[0].blocks


def test_hidden_rows_cols():
    rows = (xlsx_row(1, xlsx_cell("A1", "shown", t="str"), xlsx_cell("B1", "hc", t="str"))
            + xlsx_row(2, xlsx_cell("A2", "hr", t="str"), attrs='hidden="1"'))
    sh = sheet(rows, before='<cols><col min="2" max="2" width="9" hidden="1"/></cols>')
    out, _ = md([sh])
    assert "shown" in out and "hr" not in out and "hc" not in out and "hidden places left out" in out, out
    out, _ = md([sh], hidden=True)
    assert "hr" in out and "hc" in out, out


def test_merge():
    rows = xlsx_row(1, xlsx_cell("A1", "m", t="str"), xlsx_cell("C1", "n", t="str"))
    out, _ = md([sheet(rows, after='<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>')])
    assert out.endswith("\nA1:C1\nm||n\n-|-|-\n\nMerged: A1:B1\n"), out
    out, ctx = md([sheet(rows, after='<mergeCells><mergeCell ref="bad"/><mergeCell ref="A1:B1"/></mergeCells>')])
    assert out.endswith("\nm||n\n-|-|-\n\nMerged: A1:B1\n") and not ctx.skipped, (out, ctx.skipped)


def formulas_of(doc):
    p = doc.blocks[0].blocks[-1]
    return "".join(i.text for i in p.inlines)


def test_shared_formula_fill_down_is_one_entry():
    rows = [xlsx_row(r, xlsx_cell(f"B{r}", r), xlsx_cell(f"C{r}", 2),
                     xlsx_cell(f"D{r}", 2 * r, f='<f t="shared" ref="D2:D5" si="0">B2*C2</f>' if r == 2
                               else '<f t="shared" si="0"/>')) for r in range(2, 6)]
    doc, ctx, _ = parsed([sheet("".join(rows))])
    assert formulas_of(doc) == "Formulas: D2:D5 =B2*C2" and not ctx.skipped, (formulas_of(doc), ctx.skipped)


def test_formula_pattern_break_listed_on_its_own():
    rows = [xlsx_row(r, xlsx_cell(f"B{r}", r), xlsx_cell(f"D{r}", 2 * r, f="B4+1" if r == 4 else f"B{r}*2"))
            for r in range(2, 6)]
    doc, _, _ = parsed([sheet("".join(rows))])
    assert formulas_of(doc) == "Formulas: D2:D3, D5 =B2*2; D4 =B4+1", formulas_of(doc)


def test_formula_without_cached_value_shows_in_its_cell():
    rows = xlsx_row(1, xlsx_cell("A1", 3), xlsx_cell("B1", f="<f>A1*2</f><v></v>"),
                    xlsx_cell("C1", f='<f t="shared" ref="C1:C2" si="1">A1+1</f>'))
    rows += xlsx_row(2, xlsx_cell("A2", 4), xlsx_cell("C2", f='<f t="shared" si="1"/>'))
    doc, _, _ = parsed([sheet(rows)])
    t = doc.blocks[0].blocks[0]
    assert [c.blocks[0].inlines[0].text for c in t.rows[0]] == ["3", "=A1*2", "=A1+1"], t.rows
    assert t.rows[1][2].blocks[0].inlines[0].text == "=A2+1", t.rows[1]
    assert formulas_of(doc) == "Formulas: B1 =A1*2; C1:C2 =A1+1", formulas_of(doc)


def test_formula_with_an_empty_text_result_keeps_the_sheet_and_its_formula():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", r),
                            xlsx_cell(f"B{r}", "" if r % 2 else "v", t="str", f=f'IF(A{r}>4,"",A{r}*2)'))
                   for r in range(1, 5))
    doc, ctx, _ = parsed([sheet(rows)])
    assert formulas_of(doc) == 'Formulas: B1:B4 =IF(A1>4,"",A1*2)' and not ctx.skipped, (formulas_of(doc), ctx.skipped)


def test_hyperlink_external():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="A1" r:id="rL"/>'])
    assert f"[go]({LINK})" in out and not ctx.skipped, (out, ctx.skipped)


def test_hyperlink_location():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="A1" location="Sheet2!A1"/>'])
    assert "go" in out and "](" not in out and ctx.skipped["link"] == 0, (out, ctx.skipped)


def test_hyperlink_location_is_named():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"), xlsx_cell("B1", "jump", t="str"))
    sh = sheet(rows, after='<hyperlinks><hyperlink ref="A1" location="Lists!A1"/>'
                           '<hyperlink ref="B1" r:id="rL" location="\'My Sheet\'!B2"/></hyperlinks>',
               rels=[("rL", "hyperlink", "../a.xlsx", True)])
    doc, ctx, _ = parsed([sh])
    cells = doc.blocks[0].blocks[0].rows[0]
    assert cells[0].blocks[0].inlines == [Text("go"), Text(" → Lists!A1")], cells[0].blocks
    assert cells[1].blocks[0].inlines == [Link("../a.xlsx#'My%20Sheet'!B2", [Text("jump")])], cells[1].blocks
    assert ooxml_sheets.sheet_grid.fragment("시트 1!A(1") == "시트%201!A%281", ooxml_sheets.sheet_grid.fragment("시트 1!A(1")
    assert not ctx.skipped, ctx.skipped
    ctx = Context()
    ctx.name = "a.xlsx"
    doc = ooxml.parse_package(make_xlsx(None, [sh]), ctx)
    cells = doc.blocks[0].blocks[0].rows[0]
    assert cells[1].blocks[0].inlines == [Text("jump"), Text(" → 'My Sheet'!B2")], cells[1].blocks


def test_value_hidden_by_format():
    # a ;;; section shows nothing in Excel: the value is hidden content, left out and counted unless --hidden
    rows = xlsx_row(1, xlsx_cell("A1", 5, s=1), xlsx_cell("B1", "x", t="str"))
    doc, _, _ = parsed([sheet(rows)], numfmts={164: ";;;"}, xfs=[0, 164])
    assert doc.blocks[0].blocks[0].rows[0][0].blocks[0].inlines == [Hidden([Text("5")])], doc.blocks[0].blocks
    out, _ = md([sheet(rows)], numfmts={164: ";;;"}, xfs=[0, 164])
    assert "5" not in out.replace("A1:B1", "") and "(1 hidden places left out)" in out, out


def test_hyperlink_range():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", f"v{r}", t="str")) for r in (1, 2, 3))
    out, _ = linked(rows, ['<hyperlink ref="A1:A3" r:id="rL"/>'])
    assert out.count(f"]({LINK})") == 3, out


def test_hyperlink_missing_rel():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="A1" r:id="rX"/>'])
    assert "go" in out and "](" not in out and ctx.skipped["link"] == 1, (out, ctx.skipped)
    out, ctx = linked(rows, ['<hyperlink ref="A1" r:id="rL"/>'], rels=False)
    assert ctx.skipped["link"] == 1, ctx.skipped


def test_hyperlink_empty_cell():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="C5" r:id="rL"/>'])
    assert "](" not in out and not ctx.skipped, (out, ctx.skipped)


def test_hyperlink_huge_range():
    rows = xlsx_row(1, xlsx_cell("A1", "a", t="str")) + xlsx_row(90000, xlsx_cell("C90000", "b", t="str"))
    t0 = time.perf_counter()
    out, _ = linked(rows, ['<hyperlink ref="A1:XFD1048576" r:id="rL"/>'])
    assert out.count(f"]({LINK})") == 2 and time.perf_counter() - t0 < 1, out


def test_hyperlink_flood():
    # many links over the whole sheet of valued cells: the held cost is charged to the scan count, and the links
    # past it are counted, not walked
    n = 3000
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", "x", t="str")) for r in range(1, n + 1))
    links = ['<hyperlink ref="A1:ZZ1048576" r:id="rL"/>'] * 500
    with patched("SHEET_SCAN", 4 * n + 3 * n // 2):
        t0 = time.perf_counter()
        out, ctx = linked(rows, links)
        spent = time.perf_counter() - t0
    assert out.count(f"]({LINK})") == n and spent < 3, (out.count("]("), spent)
    assert 0 < ctx.skipped["link"] < 500 and ctx.skipped["sheet_cut"] == 0, ctx.skipped


def test_hyperlink_count_cap():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    with patched("_LINKS", 5, ooxml_sheets):
        out, ctx = linked(rows, ['<hyperlink ref="A1" r:id="rL"/>'] * 20)
    assert f"[go]({LINK})" in out and ctx.skipped["links_cut"] == 1 and warned(ctx, "hyperlinks"), (out, ctx.skipped)
    assert not ctx.skipped["sheet_cut"] and "hyperlink list cut short" in out, (out, ctx.skipped)


def test_hyperlink_rels_read_once():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    calls = []
    real = ooxml_sheets.ooxml_base.rels

    def counting(pkg, part, ctx=None):
        if part.endswith("sheet1.xml"):
            calls.append(part)
        return real(pkg, part, ctx)

    links = ['<hyperlink ref="A1" r:id="rL"/>'] * 3
    with patched("rels", counting, ooxml_sheets.ooxml_base):
        out, ctx = md([sheet(rows, after="<hyperlinks>" + "".join(links) + "</hyperlinks>")],
                      extra_parts={"xl/worksheets/_rels/sheet1.xml.rels": b"<not xml"})
    assert "go" in out and "](" not in out, out
    assert ctx.skipped["link"] == 3 and ctx.skipped["rels"] == 1 and len(calls) == 1, (ctx.skipped, calls)


def test_hyperlink_rels_failure_read_once():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    calls = []

    real = ooxml_sheets.ooxml_base.rels

    def failing(pkg, part, ctx=None):
        calls.append(part)
        if not part.endswith("sheet1.xml"):
            return real(pkg, part, ctx)
        raise ReadError("unsupported", "no")

    links = ['<hyperlink ref="A1" r:id="rL"/>'] * 3
    with patched("rels", failing, ooxml_sheets.ooxml_base):
        _, ctx = md([sheet(rows, after="<hyperlinks>" + "".join(links) + "</hyperlinks>")])
    sheet_calls = [c for c in calls if c.endswith("sheet1.xml")]
    assert ctx.skipped["link"] == 3 and ctx.skipped["rels"] == 1 and len(sheet_calls) == 1, (ctx.skipped, calls)


def test_hyperlink_first_wins():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", f"v{r}", t="str")) for r in (1, 2))
    sh = sheet(rows, after='<hyperlinks><hyperlink ref="A1" r:id="rL"/><hyperlink ref="A1:A2" r:id="rM"/></hyperlinks>',
               rels=[("rL", "hyperlink", LINK, True), ("rM", "hyperlink", "https://example.com/b", True)])
    out, _ = md([sh])
    assert f"[v1]({LINK})" in out and "[v2](https://example.com/b)" in out and "[v1](https://example.com/b)" not in out, out


def test_hyperlink_noteref_after_link():
    from reader.model import Link, NoteRef, Text
    ctx = Context()
    grid = ooxml_sheets.sheet_grid.Grid("S", ooxml_sheets.sheet_grid.Budget(), ctx)
    grid.put(0, 0, [NoteRef("n1"), Text("a"), NoteRef("n2"), Text("b")])
    me = ooxml_sheets._Sheets.__new__(ooxml_sheets._Sheets)
    me.ctx, me.budget = ctx, grid.budget
    me._link(grid, [(0, 0, 0, 0, LINK, None)])
    assert grid.get(0, 0) == [Link(LINK, [Text("a"), Text("b")]), NoteRef("n1"), NoteRef("n2")], grid.get(0, 0)


def test_hidden_in_merge():
    # the merge's origin row is hidden: its text moves to the first visible row of the merge
    rows = (xlsx_row(1, xlsx_cell("A1", "mm", t="str"), attrs='hidden="1"')
            + xlsx_row(3, xlsx_cell("A3", "zz", t="str")))
    sh = sheet(rows, after='<mergeCells><mergeCell ref="A1:A2"/></mergeCells>')
    out, _ = md([sh])
    assert "mm" in out and "zz" in out, out


LEGACY = f"{XLSX_REL}/comments"
THREADED = "http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"
PERSON = "http://schemas.microsoft.com/office/2017/10/relationships/person"
TC_NS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
LEG_PART, THR_PART, PER_PART = "xl/comments1.xml", "xl/threadedComments/threadedComment1.xml", "xl/persons/person.xml"


def legacy_xml(*items, authors=("Ann",)):
    """A legacy comments part from (ref, authorId, text) items."""
    body = "".join(f'<comment ref="{r}" authorId="{a}"><text><r><t>{t}</t></r></text></comment>' for r, a, t in items)
    return (f'<comments xmlns="{XLSX_SML}"><authors>' + "".join(f"<author>{a}</author>" for a in authors)
            + f"</authors><commentList>{body}</commentList></comments>")


def threaded_xml(*items):
    """A threaded comments part from (ref, personId, id, parentId or None, text) items."""
    body = "".join(f'<threadedComment ref="{r}" personId="{p}" id="{i}"' + (f' parentId="{q}"' if q else "")
                   + f"><text>{t}</text></threadedComment>" for r, p, i, q, t in items)
    return f'<ThreadedComments xmlns="{TC_NS}">{body}</ThreadedComments>'


def persons_xml(**names):
    return (f'<personList xmlns="{TC_NS}">'
            + "".join(f'<person id="{k}" displayName="{v}"/>' for k, v in names.items()) + "</personList>")


def commented(rows, leg=None, thr=None, people=None, **kw):
    """md of a sheet with a legacy comments part and/or a threaded one (and the workbook's persons)."""
    rels, extra, wb_rels = [], {}, []
    if leg is not None:
        rels.append(("rC", LEGACY, "../comments1.xml", False))
        extra[LEG_PART] = leg
    if thr is not None:
        rels.append(("rT", THREADED, "../threadedComments/threadedComment1.xml", False))
        extra[THR_PART] = thr
    if people is not None:
        wb_rels.append(("rP", PERSON, "persons/person.xml", False))
        extra[PER_PART] = people
    return md([sheet(rows, rels=rels, **kw.pop("sheet_kw", {}))], extra_parts=extra, wb_rels=wb_rels, **kw)


B2 = xlsx_row(2, xlsx_cell("B2", "v", t="str"))
BO = persons_xml(p1="Bo", p2="Cy")


def test_legacy_comment():
    out, ctx = commented(B2, leg=legacy_xml(("B2", 0, "check")))
    assert "v[^1]" in out and "[^1]: Comment by Ann: check" in out and not ctx.skipped, (out, ctx.skipped)


def test_legacy_comment_author_prefix_dropped():
    # Excel starts a legacy comment's text with its author's name and a colon; the note names the author once
    out, ctx = commented(B2, leg=legacy_xml(("B2", 0, "Ann: check")))
    assert "[^1]: Comment by Ann: check" in out and "Ann: Ann" not in out, out


def test_threaded_wins():
    out, _ = commented(B2, leg=legacy_xml(("B2", 0, "[Threaded comment] placeholder")),
                       thr=threaded_xml(("B2", "p1", "t1", None, "real")), people=BO)
    assert "Comment by Bo: real" in out and "placeholder" not in out and "[^2]" not in out, out


def test_threaded_without_legacy():
    rows = xlsx_row(3, xlsx_cell("C3", "w", t="str"))
    out, _ = commented(rows, thr=threaded_xml(("C3", "p1", "t1", None, "real")), people=BO)
    assert "[^1]: Comment by Bo: real" in out, out


def test_legacy_kept_elsewhere():
    rows = B2 + xlsx_row(4, xlsx_cell("D4", "z", t="str"))
    out, _ = commented(rows, leg=legacy_xml(("B2", 0, "old"), ("D4", 0, "kept")),
                       thr=threaded_xml(("B2", "p1", "t1", None, "real")), people=BO)
    assert "Comment by Bo: real" in out and "Comment by Ann: kept" in out and "old" not in out, out


def test_thread_replies():
    out, _ = commented(B2, thr=threaded_xml(("B2", "p1", "t1", None, "q"), ("B2", "p2", "t2", "t1", "a")), people=BO)
    assert "[^1]: Comment by Bo: q Comment by Cy: a" in out and "[^2]" not in out, out


def test_reply_unknown_parent_own_thread():
    out, _ = commented(B2, thr=threaded_xml(("B2", "p1", "t1", "gone", "orphan")), people=BO)
    assert "[^1]: Comment by Bo: orphan" in out, out


def test_thread_cycle():
    thr = threaded_xml(("B2", "p1", "a", "b", "first"), ("B2", "p2", "b", "a", "second"),
                       ("B2", "p1", "c", None, "root"))
    out, ctx = commented(B2, thr=thr, people=BO)
    for word in ("first", "second", "root"):
        assert out.count(word) == 1, (word, out)
    assert not ctx.skipped, ctx.skipped


def test_comment_cap():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", "x", t="str")) for r in range(1, 6))
    leg = legacy_xml(*[(f"A{r}", 0, f"n{r}") for r in range(1, 6)])
    with patched("SHEET_COMMENTS", 3):
        out, ctx = commented(rows, leg=leg)
    assert "[^3]" in out and "[^4]" not in out and ctx.skipped["comments_cut"] == 1, (out, ctx.skipped)


def test_comment_with_link():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, _ = commented(rows, leg=legacy_xml(("A1", 0, "n")), sheet_kw={"after": '<hyperlinks><hyperlink ref="A1" r:id="rL"/></hyperlinks>'})
    assert out  # rendered; the exact order is checked on the model below
    sh = sheet(rows, after='<hyperlinks><hyperlink ref="A1" r:id="rL"/></hyperlinks>',
               rels=[("rC", LEGACY, "../comments1.xml", False), ("rL", "hyperlink", LINK, True)])
    doc, _, _ = parsed([sh], extra_parts={LEG_PART: legacy_xml(("A1", 0, "n"))})
    cell = doc.blocks[0].blocks[0].inlines
    assert cell[0] == Text("A1: ") and isinstance(cell[1], Link) and isinstance(cell[-1], NoteRef), cell


def test_comment_on_empty_cell():
    rows = xlsx_row(1, xlsx_cell("A1", "x", t="str"))
    out, _ = commented(rows, leg=legacy_xml(("D4", 0, "empty")))
    assert "[^1]" in out and "[^1]: Comment by Ann: empty" in out, out


def test_comment_on_hidden_sheet():
    def book(**kw):
        shs = [sheet(xlsx_row(1, xlsx_cell("A1", "keep", t="str")), name="Open"),
               sheet(B2, name="Secret", state="hidden",
                     rels=[("rC", LEGACY, "../comments1.xml", False)])]
        return md(shs, extra_parts={LEG_PART: legacy_xml(("B2", 0, "hush"))}, **kw)
    out, _ = book()
    assert "[^" not in out and "Comment by" not in out and "(1 hidden places left out)" in out, out
    out, _ = book(hidden=True)
    assert "v[^1]" in out and "[^1]: (hidden) Comment by Ann: hush" in out, out


def test_comments_part_missing():
    sh = sheet(B2, rels=[("rC", LEGACY, "../comments1.xml", False)])
    out, ctx = md([sh])
    assert "v" in out and "[^" not in out and ctx.skipped["comments"] == 1, (out, ctx.skipped)


def test_comments_part_damaged():
    out, ctx = commented(B2, leg="<comments")
    assert "v" in out and ctx.skipped["comments"] == 1, (out, ctx.skipped)


def orphans(out):
    """Footnote numbers defined in the output but marked nowhere."""
    import re
    return [n for n in re.findall(r"^\[\^(\d+)\]:", out, re.M) if out.count(f"[^{n}]") < 2]


def test_comment_hidden_row():
    rows = xlsx_row(1, xlsx_cell("A1", "keep", t="str")) + xlsx_row(2, xlsx_cell("B2", "v", t="str"), attrs='hidden="1"')
    leg = legacy_xml(("B2", 0, "hush"))
    out, _ = commented(rows, leg=leg)
    assert "hush" not in out and "[^" not in out and "(1 hidden places left out)" in out, out
    out, _ = commented(rows, leg=leg, hidden=True)
    assert "[^1]: (hidden) Comment by Ann: hush" in out and "v[^1]" in out, out
    out, _ = commented(xlsx_row(1, xlsx_cell("A1", "keep", t="str")) + xlsx_row(2, "", attrs='hidden="1"'), leg=leg)
    assert "hush" not in out, out


def test_comment_hidden_column():
    rows = xlsx_row(1, xlsx_cell("A1", "keep", t="str"), xlsx_cell("B1", "v", t="str"))
    leg = legacy_xml(("B1", 0, "hush"))
    kw = {"before": '<cols><col min="2" max="2" width="9" hidden="1"/></cols>'}
    out, _ = commented(rows, leg=leg, sheet_kw=dict(kw))
    assert "hush" not in out and "[^" not in out and "(1 hidden places left out)" in out, out
    out, _ = commented(rows, leg=leg, hidden=True, sheet_kw=dict(kw))
    assert "[^1]: (hidden) Comment by Ann: hush" in out, out


def test_comment_on_merged_away_cell():
    rows = xlsx_row(1, xlsx_cell("A1", "m", t="str"), xlsx_cell("B1", "gone", t="str"))
    merge = {"after": '<mergeCells><mergeCell ref="A1:B1"/></mergeCells>'}
    out, _ = commented(rows, leg=legacy_xml(("B1", 0, "kept")), sheet_kw=merge)
    assert "m[^1]" in out and "[^1]: Comment by Ann: kept" in out, out
    hid = xlsx_row(1, xlsx_cell("A1", "m", t="str"), attrs='hidden="1"') + xlsx_row(2, xlsx_cell("A2", "z", t="str"))
    out, _ = commented(hid, leg=legacy_xml(("B1", 0, "kept")), sheet_kw=dict(merge))
    assert orphans(out) == [] and "kept" not in out and "hidden places left out" in out, out
    out, _ = commented(hid, leg=legacy_xml(("B1", 0, "kept")), sheet_kw=dict(merge), hidden=True)
    assert "Comment by Ann: kept" in out and orphans(out) == [], out


def test_comment_merge_chain():
    # A1:B1 renders; B1:C1 overlaps it and is dropped, so C1 is an ordinary cell and keeps its own comment
    rows = xlsx_row(1, xlsx_cell("A1", "a", t="str"))
    merges = {"after": '<mergeCells><mergeCell ref="A1:B1"/><mergeCell ref="B1:C1"/></mergeCells>'}
    leg = legacy_xml(("A1", 0, "ca"), ("B1", 0, "cb"), ("C1", 0, "cc"))
    out, ctx = commented(rows, leg=leg, sheet_kw=merges)
    assert "a[^1][^2]" in out and "Comment by Ann: ca" in out and "Comment by Ann: cb" in out, out
    assert "Comment by Ann: cc" in out and orphans(out) == [] and not ctx.skipped["comments_cut"], (out, ctx.skipped)


def test_comment_origin_first():
    rows = xlsx_row(1, xlsx_cell("A1", "m", t="str"))
    merge = {"after": '<mergeCells><mergeCell ref="A1:B1"/></mergeCells>'}
    out, _ = commented(rows, leg=legacy_xml(("B1", 0, "second"), ("A1", 0, "first")), sheet_kw=merge)
    assert "m[^1][^2]" in out and out.index("first") < out.index("second"), out


def test_comment_kept_on_cut_sheet():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", f"v{r}", t="str")) for r in range(1, 31))
    leg = legacy_xml(("A2", 0, "kept"), ("A30", 0, "unread"))
    with patched("SHEET_SCAN", 40):
        out, ctx = commented(rows, leg=leg)
    assert "v2[^1]" in out and "[^1]: Comment by Ann: kept" in out and "unread" not in out, out
    assert orphans(out) == [] and ctx.skipped["comments_cut"] == 1, (out, ctx.skipped)


def test_comment_cells_before_notes():
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", "x", t="str")) for r in range(1, 5))
    leg = legacy_xml(*[(f"A{r}", 0, f"c{r}") for r in range(1, 5)], ("D9", 0, "empty"))
    with patched("SHEET_CELLS", 2):
        out, ctx = commented(rows, leg=leg)
    assert orphans(out) == [] and "x[^1]" in out and "x[^2]" in out, out
    assert "Comment by Ann: c1" in out and "Comment by Ann: c2" in out, out
    assert "c3" not in out and "c4" not in out and "empty" not in out, out
    assert ctx.skipped["comments_cut"] == 1, ctx.skipped


def test_comment_text_past_chars():
    rows = xlsx_row(1, xlsx_cell("A1", "abcd", t="str"), xlsx_cell("B1", "efgh", t="str"), xlsx_cell("C1", "ijkl", t="str"))
    with patched("SHEET_CHARS", 12):
        out, ctx = commented(rows, leg=legacy_xml(("B1", 0, "tiny")))
    assert "abcd" in out and "efgh" in out and "ijkl" in out and "[^" not in out and "tiny" not in out, out
    assert ctx.skipped["comments_cut"] == 1, ctx.skipped


def test_comment_stream_cap():
    import random
    pad = "<!--" + random.Random(0).randbytes(900_000).hex() + "-->"  # keeps the part under ZIP_RATIO
    body = "<comment/>" * 200_000
    part = f'<comments xmlns="{XLSX_SML}">{pad}<authors><author>Ann</author></authors><commentList>{body}</commentList></comments>'
    with patched("_COMMENT_ELEMENTS", 1000, ooxml_sheets):
        t0 = time.perf_counter()
        out, ctx = commented(B2, leg=part)
        secs = time.perf_counter() - t0
    assert "v" in out and ctx.skipped["comments_cut"] == 1 and secs < 1, (ctx.skipped, secs)


# ---- drawings and chartsheets (Task 12)
DRAWING = ("rD", "drawing", "../drawings/drawing1.xml", False)


def drawn(n, *anchors, rels=()):
    """extra_parts of drawing part n (its anchors and its own (id, type, target) relationships)."""
    return {f"xl/drawings/drawing{n}.xml": xb.drawing_xml(*anchors),
            f"xl/drawings/_rels/drawing{n}.xml.rels": xb.rels_xml(*rels)}


def test_drawing_image_and_chart():
    parts = drawn(1, xb.anchor(3, xb.chart_frame("rId1")), xb.anchor(0, xb.pic("rId2")),
                  rels=[("rId1", "chart", "../charts/chart1.xml"), ("rId2", "image", "../media/image1.png")])
    parts.update({"xl/charts/chart1.xml": xb.BAR, "xl/media/image1.png": xb.PNG})
    rows = xlsx_row(1, xlsx_cell("A1", "top", t="str"), xlsx_cell("B1", "next", t="str"))
    out, ctx = md([sheet(rows, rels=[DRAWING])], extra_parts=parts)
    assert "\ntop|next\n" in out and "Chart: column" in out and "\nQ1|7\n" in out, out
    assert out.index("\ntop|next\n") < out.index("assets/img-001.png") < out.index("Chart: column"), out  # anchor order
    assert not ctx.skipped, ctx.skipped


def test_chartsheet():
    parts = drawn(1, xb.anchor(0, xb.chart_frame("rId1")), rels=[("rId1", "chart", "../charts/chart1.xml")])
    parts["xl/charts/chart1.xml"] = xb.BAR
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "cells", t="str")), name="W"),
              {"name": "Graph", "chartsheet": True, "rels": [DRAWING]}]
    out, ctx = md(sheets, extra_parts=parts)
    assert "## Sheet: Graph" in out and "Chart: column" in out and out.index("## Sheet: Graph") < out.index("Chart: column")
    assert not ctx.skipped, ctx.skipped
    out, ctx = md([sheets[0], {"name": "Empty", "chartsheet": True}])
    assert "cells" in out and "Empty" not in out and not ctx.skipped, (out, ctx.skipped)


def test_chart_total_shared():
    parts = {}
    for n in (1, 2):
        parts.update(drawn(n, xb.anchor(0, xb.chart_frame("rId1")), rels=[("rId1", "chart", f"../charts/chart{n}.xml")]))
        parts[f"xl/charts/chart{n}.xml"] = xb.BAR
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", "a", t="str")), name="A", rels=[DRAWING]),
              sheet(xlsx_row(1, xlsx_cell("A1", "b", t="str")), name="B",
                    rels=[("rD", "drawing", "../drawings/drawing2.xml", False)])]
    with patched("CHART_TOTAL", 6):  # one BAR table is 4 cells
        out, ctx = md(sheets, extra_parts=parts)
    assert out.count("Chart: column") == 1 and ctx.skipped["chart"] == 1, (out, ctx.skipped)


def test_cut_sheet_not_opened():
    # N2: a sheet that would stop at its first element is not opened, so its bytes are not charged to ZIP_TOTAL and
    # the drawing after it still reads; the counts and warnings are today's
    import random
    pad = "<!--" + random.Random(1).randbytes(100_000).hex() + "-->"
    big = f'<worksheet xmlns="{XLSX_SML}">{pad}<sheetData>{xlsx_row(1, xlsx_cell("A1", "late", t="str"))}</sheetData></worksheet>'
    parts = drawn(1, xb.anchor(0, xb.pic("rId1")), rels=[("rId1", "image", "../media/image1.png")])
    image = xb.PNG + random.Random(2).randbytes(60_000)
    parts["xl/media/image1.png"] = image
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", 7)), name="A"), sheet("", name="B", part=big, rels=[DRAWING])]
    data = make_xlsx(None, sheets, extra_parts=parts)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = sum(i.file_size for i in z.infolist())
    # B (200 KB) charged leaves no room for the 60 KB image; B left unread leaves plenty
    with patched("SHEET_SCAN", 5), patched("ZIP_TOTAL", total - len(image) // 2):
        out, ctx = md(sheets, extra_parts=parts)
    assert "7" in out and "late" not in out and "## Sheet: B" in out and "assets/img-001.png" in out, out
    assert dict(+ctx.skipped) == {"sheet_cut": 1} and warned(ctx, "sheet B cut at 5 scanned XML elements"), (
        ctx.skipped, ctx.warnings)
    with patched("ZIP_TOTAL", len(big) - 1):  # a sheet that is read is still charged in full
        out, ctx = md(sheets, extra_parts=parts)
    assert "late" not in out and ctx.skipped["sheet"] == 1, (out, ctx.skipped)


def test_cut_sheet_comments_counted():
    # the unopened sheet's comments are not read: the cut they would have met is counted, once
    sheets = [sheet(xlsx_row(1, xlsx_cell("A1", 7)), name="A"),
              sheet(B2, name="B", rels=[("rC", LEGACY, "../comments1.xml", False)])]
    with patched("SHEET_SCAN", 5):
        out, ctx = md(sheets, extra_parts={LEG_PART: legacy_xml(("B2", 0, "unread"))})
    assert "unread" not in out and "[^" not in out, out
    assert dict(+ctx.skipped) == {"sheet_cut": 1, "comments_cut": 1}, ctx.skipped


def test_drawing_error_keeps_cells():
    # a drawing part flagged encrypted: the ReadError costs the drawings (counted `object`), not the sheet's cells
    parts = drawn(1, xb.anchor(0, xb.pic("rId1")), rels=[("rId1", "image", "../media/image1.png")])
    parts["xl/media/image1.png"] = xb.PNG
    members = zip_members(make_xlsx(None, [sheet(xlsx_row(1, xlsx_cell("A1", "kept", t="str")), rels=[DRAWING])],
                                    extra_parts=parts))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
        z.getinfo("xl/drawings/drawing1.xml").flag_bits |= 1  # writestr resets it, so mark the central entry
    ctx = Context()
    doc = ooxml.parse_package(buf.getvalue(), ctx)
    out = markdown.render(doc, Path(tempfile.gettempdir()) / "unused-assets", False, ctx.skipped)
    assert "kept" in out and "assets/" not in out and dict(+ctx.skipped) == {"object": 1}, (out, ctx.skipped)


def zip_members(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def worst():
    """The slowest workbook the caps allow: a styles flood and a shared-strings run flood past their caps, and a
    sheet past SHEET_SCAN. Random padding keeps each flood part under ZIP_RATIO. (parse seconds, ctx)"""
    import random
    pad = "<!--" + random.Random(0).randbytes(900_000).hex() + "-->"
    sst = (f'<sst xmlns="{XLSX_SML}">{pad}<si>' + "<r/>" * (2 * limits.SHEET_SCAN + 1000)
           + "</si><si><t>z</t></si></sst>")
    styles = (f'<styleSheet xmlns="{XLSX_SML}">{pad}<cellXfs>' + "<xf/>" * (ooxml_sheets.STYLE_ELEMENTS + 1000)
              + "</cellXfs></styleSheet>")
    rows = "".join(xlsx_row(r, *(f'<c r="{col_name(c)}{r}" s="1"/>' for c in range(1000)))
                   for r in range(1, limits.SHEET_SCAN // 1000 + 2))
    _, ctx, secs = parsed([sheet(rows)], shared=[], xfs=[0, 0],
                          extra_parts={"xl/sharedStrings.xml": sst, "xl/styles.xml": styles})
    return secs, ctx


def measure(n=2_000_000):
    """Not a test: one sheet of n styled empty cells (1,000 a row) through the whole reader."""
    rows = "".join(xlsx_row(r, *(f'<c r="{col_name(c)}{r}" s="1"/>' for c in range(1000)))
                   for r in range(1, n // 1000 + 1))
    _, ctx, secs = parsed([sheet(rows)], xfs=[0, 0])
    print(f"SCAN {n} {secs:.2f}")
    secs, ctx = worst()
    print(f"WORST {secs:.2f}")


def test_valueless_formulas_do_not_spend_the_cells_of_later_sheets():
    old = limits.SHEET_CELLS, limits.BLANK_FORMULAS
    limits.SHEET_CELLS, limits.BLANK_FORMULAS = 50, 20
    try:
        blank = "".join(f'<row r="{r}">' + "".join(f'<c r="{col}{r}" t="str"><f>IF($A{r}="","",1)</f><v></v></c>'
                                                    for col in "BCDEFGHIJK") + "</row>" for r in range(1, 11))
        data = '<row r="1"><c r="A1" t="inlineStr"><is><t>kim</t></is></c><c r="B1"><v>42</v></c></row>'
        out, ctx = md([sheet(blank, name="Template"), sheet(data, name="Data")])
    finally:
        limits.SHEET_CELLS, limits.BLANK_FORMULAS = old
    assert "\nkim|42\n" in out and ctx.skipped["formula"] >= 80 and not ctx.skipped["sheet_cut"], (out, ctx.skipped)


def ranged(sheet_name="S"):
    """xb.BAR drawn from B1 (name), A2 (category) and B2 (value) of `sheet_name`, hidden cells plotted."""
    return xb.BAR.replace("<c:strRef>", f"<c:strRef><c:f>{sheet_name}!$B$1</c:f>", 1).replace(
        "<c:cat><c:strRef>", f"<c:cat><c:strRef><c:f>{sheet_name}!$A$2</c:f>").replace(
        "<c:numRef>", f"<c:numRef><c:f>{sheet_name}!$B$2</c:f>").replace(
        "</c:plotArea>", '</c:plotArea><c:plotVisOnly val="0"/>')


NAME, CAT, VAL = xlsx_cell("B1", "S", t="str"), xlsx_cell("A2", "Q1", t="str"), xlsx_cell("B2", "7")
PLOTTED = xlsx_row(1, NAME) + xlsx_row(2, CAT, VAL)


def charted(sheets, chart=None, **kw):
    """(the chart's Table, markdown, ctx) of a workbook whose first sheet draws `chart` (default `ranged()`); `kw`
    goes to make_xlsx."""
    parts = drawn(1, xb.anchor(5, xb.chart_frame("rId1")), rels=[("rId1", "chart", "../charts/chart1.xml")])
    parts["xl/charts/chart1.xml"] = ranged() if chart is None else chart
    sheets = [dict(sheets[0], rels=[DRAWING])] + list(sheets[1:])
    doc, ctx, _ = parsed(sheets, extra_parts=parts, **kw)
    (t,) = [b for s in doc.blocks for b in getattr(s, "blocks", ()) if isinstance(b, Table)
            and b.caption.startswith("Chart")]
    with tempfile.TemporaryDirectory() as d:
        return t, markdown.render(doc, Path(d) / "assets", False, ctx.skipped), ctx


def test_chart_of_cells_the_sheet_shows_keeps_its_caption_alone():
    t, out, ctx = charted([sheet(PLOTTED)])
    assert t.caption == "Chart: column. Data: S!A1:B2" and t.rows == [], (t.caption, t.rows)
    assert out.count("Q1") == 1 and "\nChart: column. Data: S!A1:B2\n" in out and not ctx.skipped, (out, ctx.skipped)


def test_chart_of_another_shown_sheets_cells_keeps_its_caption_alone():
    top = sheet(xlsx_row(1, xlsx_cell("A1", "top", t="str")), name="First")
    t, out, _ = charted([top, sheet(PLOTTED, name="Data")], ranged("Data"))
    assert t.caption == "Chart: column. Data: Data!A1:B2" and t.rows == [] and out.count("Q1") == 1, (t.rows, out)
    t, out, _ = charted([top, sheet(PLOTTED, name="data")], ranged("Data"))
    assert t.rows == [], t.rows
    t, out, _ = charted([top, sheet(PLOTTED, name="Data", state="hidden")], ranged("Data"))
    assert len(t.rows) == 2 and out.count("Q1") == 1, (t.rows, out)
    t, out, _ = charted([top], ranged("Data"))
    assert len(t.rows) == 2, t.rows


def test_chart_keeps_its_table_when_its_range_holds_a_hidden_row_or_column():
    t, out, _ = charted([sheet(xlsx_row(1, NAME) + xlsx_row(2, CAT, VAL, attrs='hidden="1"'))])
    assert len(t.rows) == 2 and "\nQ1|7\n" in out, (t.rows, out)
    t, out, _ = charted([sheet(PLOTTED, before='<cols><col min="2" max="2" hidden="1"/></cols>')])
    assert len(t.rows) == 2 and "\nQ1|7\n" in out, (t.rows, out)
    t, out, _ = charted([sheet(PLOTTED + xlsx_row(3, xlsx_cell("A3", "below", t="str"), attrs='hidden="1"'))])
    assert t.rows == [], t.rows


def test_chart_keeps_its_table_when_its_sheet_is_cut():
    with patched("SHEET_CELLS", 2):
        t, out, ctx = charted([sheet(PLOTTED)])
    assert len(t.rows) == 2 and ctx.skipped["sheet_cut"] == 1 and "\nQ1|7\n" in out, (t.rows, ctx.skipped, out)


def test_chart_keeps_its_table_when_a_cell_of_its_range_is_not_in_the_sheet():
    for rows in (xlsx_row(1, NAME) + xlsx_row(2, CAT), xlsx_row(2, CAT, VAL), xlsx_row(1, NAME) + xlsx_row(2, VAL)):
        t, out, _ = charted([sheet(rows)])
        assert len(t.rows) == 2 and "\nQ1|7\n" in out, (rows, t.rows, out)


def test_chart_keeps_its_table_when_a_merge_meets_its_range():
    merged = '<mergeCells count="1"><mergeCell ref="A2:A3"/></mergeCells>'
    t, out, _ = charted([sheet(PLOTTED, after=merged)])
    assert len(t.rows) == 2, t.rows
    t, out, _ = charted([sheet(PLOTTED, after=merged.replace("A2:A3", "D1:E1"))])
    assert t.rows == [], t.rows


def test_chart_keeps_its_table_when_the_sheet_shows_a_cell_of_its_range_as_something_else():
    t, out, _ = charted([sheet(xlsx_row(1, NAME) + xlsx_row(2, CAT, xlsx_cell("B2", f="A1+1")))])
    assert len(t.rows) == 2 and "\nQ1|=A1+1\n" in out and "\nQ1|7\n" in out, (t.rows, out)
    t, out, _ = charted([sheet(xlsx_row(1, NAME) + xlsx_row(2, CAT, xlsx_cell("B2", "7", s=1)))],
                        numfmts={164: ";;;"}, xfs=[0, 164])
    assert len(t.rows) == 2 and "hidden places left out" in out and "\nQ1|7\n" in out, (t.rows, out)
    t, out, _ = charted([sheet(xlsx_row(1, NAME) + xlsx_row(2, CAT, xlsx_cell("B2", "7", f="A1+1")))])
    assert t.rows == [] and out.count("Q1") == 1, (t.rows, out)


def test_chart_keeps_its_table_when_two_sheets_answer_to_its_sheet_name():
    top = sheet(xlsx_row(1, xlsx_cell("A1", "top", t="str")), name="First")
    for other in ({"state": "hidden"}, {}):
        for names in (("Data", "data"), ("data", "Data")):
            sheets = [top, sheet(PLOTTED, name=names[0], **other), sheet(PLOTTED, name=names[1])]
            t, _, _ = charted(sheets, ranged("Data"))
            assert len(t.rows) == 2, (other, names, t.rows)


def test_chart_cut_short_keeps_its_table():
    tall = "".join(xlsx_row(r, xlsx_cell(f"A{r}", "c", t="str"), xlsx_cell(f"B{r}", str(r))) for r in range(2, 8))
    chart = ranged().replace("$A$2", "$A$2:$A$7").replace("$B$2", "$B$2:$B$7")
    with patched("CHART_CELLS", 2):
        t, out, ctx = charted([sheet(xlsx_row(1, NAME) + tall)], chart)
    assert t.caption.endswith("(first 0 points)") and len(t.rows) == 1 and ctx.skipped["chart_cut"] == 1, \
        (t.caption, t.rows, ctx.skipped)


def test_chart_keeps_its_table_when_the_sheets_merges_or_layout_were_cut():
    from reader.formats import sheet_grid
    far = '<mergeCells count="2"><mergeCell ref="D1:E1"/><mergeCell ref="D2:E2"/></mergeCells>'
    with patched("_MERGES", 1, sheet_grid):
        t, _, _ = charted([sheet(PLOTTED, after=far)])
    assert len(t.rows) == 2, t.rows
    with patched("GRID_SLOTS", 3):
        t, _, ctx = charted([sheet(PLOTTED)])
    assert len(t.rows) == 2 and ctx.skipped["sheet_cut"] == 1, (t.rows, ctx.skipped)


def test_ranges_meets_matches_a_walk_and_holds_walks_no_hidden_range():
    import random
    from reader.formats import sheet_grid
    rnd = random.Random(5)
    for _ in range(300):
        rg, spans = sheet_grid._Ranges(), []
        for _ in range(rnd.randrange(0, 6)):
            a = rnd.randrange(0, 40)
            spans.append((a, a + rnd.randrange(0, 5)))
            rg.add(*spans[-1])
        a = rnd.randrange(0, 45)
        b = a + rnd.randrange(0, 8)
        assert rg.meets(a, b) == any(x <= b and y >= a for x, y in spans), (spans, a, b)
    w = sheet_grid.Written.__new__(sheet_grid.Written)
    w.rows, w.merges, w.rows_h, w.cols_h = {0: [(0, 1)], 1: [(0, 1)]}, [], sheet_grid._Ranges(), sheet_grid._Ranges()
    for k in range(5, 2005, 2):
        w.rows_h.add(k, k)
        w.cols_h.add(k, k)

    def walked(self, a, b):
        raise AssertionError("walked the hidden ranges")

    with patched("within", walked, sheet_grid._Ranges):
        assert w.holds((0, 0, 1, 1), [(0, 0, 1, 1)], [100]) and not w.holds((0, 0, 5, 1), [(0, 0, 1, 1)], [100])
        assert not w.holds((0, 0, 1, 7), [(0, 0, 1, 1)], [100])


def test_charts_past_the_scan_count_keep_their_tables():
    tall = "".join(xlsx_row(r, xlsx_cell(f"A{r}", "c", t="str"), xlsx_cell(f"B{r}", str(r))) for r in range(2, 8))
    chart = ranged().replace("$A$2", "$A$2:$A$7").replace("$B$2", "$B$2:$B$7")
    t, _, _ = charted([sheet(xlsx_row(1, NAME) + tall)], chart)
    assert t.caption == "Chart: column. Data: S!A1:B7" and t.rows == [], (t.caption, t.rows)
    with patched("SHEET_SCAN", 10):
        t, _, _ = charted([sheet(xlsx_row(1, NAME) + tall)], chart)
    assert len(t.rows) == 2, t.rows


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    if sys.argv[1:]:  # the named tests only
        for name in sys.argv[1:]:
            globals()[name]()
        print("pass")
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
