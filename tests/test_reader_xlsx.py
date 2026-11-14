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
from reader.formats import ooxml, ooxml_sheets  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Link, NoteRef  # noqa: E402
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


# ---- fix round 1


def test_shared_strings_element_cap():
    # 2 x SHEET_SCAN closed elements: a run flood inside one si stops the table there
    shared = ["<t>a</t>", "<r/>" * 250, "<t>c</t>"]
    rows = xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", 2, t="s"), xlsx_cell("C1", 3))
    with patched("SHEET_SCAN", 100):
        out, ctx = md([sheet(rows)], shared=shared)
    assert "|a||3|" in out, out
    assert warned(ctx, "shared strings cut at 200 XML elements") and ctx.skipped["sheet_cut"] == 1, (ctx.warnings,
                                                                                                   ctx.skipped)


def test_styles_element_cap():
    flood = "<xf/>" * 100_000  # with the two xf above, past the styles cap of 100,000 closed elements
    styles = (f'<styleSheet xmlns="{XLSX_SML}"><cellXfs><xf numFmtId="0"/><xf numFmtId="10"/>{flood}</cellXfs>'
              "</styleSheet>")
    rows = xlsx_row(1, xlsx_cell("A1", 0.5, s=1), xlsx_cell("B1", 2))
    doc, ctx, secs = parsed([sheet(rows)], xfs=[0], extra_parts={"xl/styles.xml": styles})
    out = markdown.render(doc, Path(tempfile.gettempdir()) / "unused-assets", False, ctx.skipped)
    assert "|50.00%|2|" in out and ctx.skipped["document"] == 1 and secs < 2.0, (out, ctx.skipped, secs)
    assert "(could not read: 1 document part)" in out, out


def test_styles_damaged_counts_document():
    rows = xlsx_row(1, xlsx_cell("A1", 45356, s=1), xlsx_cell("B1", 2))
    bad = DOCTYPE + f'<styleSheet xmlns="{XLSX_SML}"/>'
    out, ctx = md([sheet(rows)], xfs=[0, 14], extra_parts={"xl/styles.xml": bad})
    assert "|45356|2|" in out and ctx.skipped["document"] == 1 and not ctx.skipped["styles"], (out, ctx.skipped)


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
    assert "|hi||3|" in out and ctx.skipped["document"] == 1, (out, ctx.skipped)


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
    assert "\U0001F600" * 3 in out and "|q|" in out and not ctx.skipped, (out, ctx.skipped)


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
    assert "|0.5||3|" in out and ctx.skipped["document"] == 2, (out, ctx.skipped)


def test_bool_values():
    vals = ["2", "-0.5", "0", "0.0", "True", "FALSE", "maybe"]
    rows = xlsx_row(1, *(xlsx_cell(f"{col_name(i)}1", v, t="b") for i, v in enumerate(vals)))
    out, _ = md([sheet(rows)])
    assert "|TRUE|TRUE|FALSE|FALSE|TRUE|FALSE|maybe|" in out, out


def test_unescape_beyond_shared():
    rows = xlsx_row(1, xlsx_cell("A1", "a_x000D_b", t="str"), xlsx_cell("B1", "_x0023_N/A", t="e"),
                    xlsx_cell("C1", f='"_x0041_"'))
    out, _ = md([sheet(rows, name="S_x0031_")])
    assert "## Sheet: S1" in out and '|a<br>b|#N/A|="A"|' in out and "_x" not in out, out


def test_ref_fallbacks():
    rows = (xlsx_row(2, xlsx_cell("A2", "a", t="str"), xlsx_cell(None, "b", t="str"), xlsx_cell("1A", "c", t="str"))
            + xlsx_row(None, xlsx_cell(None, "d", t="str"))
            + xlsx_row("abc", xlsx_cell(None, "e", t="str"), xlsx_cell(None, "f", t="str"))
            + xlsx_row(0, xlsx_cell("B9", "g", t="str")))
    out, ctx = md([sheet(rows)])
    assert "|a|b|c|\n|---|---|---|\n|d|||\n|e|f||\n||g||" in out and not ctx.skipped, out


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


def test_hidden_rows_cols():
    rows = (xlsx_row(1, xlsx_cell("A1", "shown", t="str"), xlsx_cell("B1", "hc", t="str"))
            + xlsx_row(2, xlsx_cell("A2", "hr", t="str"), attrs='hidden="1"'))
    sh = sheet(rows, before='<cols><col min="2" max="2" width="9" hidden="1"/></cols>')
    out, _ = md([sh])
    assert "shown" in out and "hr" not in out and "hc" not in out and "hidden places left out" in out, out
    out, _ = md([sh], hidden=True)
    assert "hr" in out and "hc" in out, out


def test_merge():
    rows = xlsx_row(1, xlsx_cell("A1", "m", t="str"))
    out, _ = md([sheet(rows, after='<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>')])
    assert 'colspan="2"' in out, out
    out, ctx = md([sheet(rows, after='<mergeCells><mergeCell ref="bad"/><mergeCell ref="A1:B1"/></mergeCells>')])
    assert 'colspan="2"' in out and not ctx.skipped, (out, ctx.skipped)


def test_hyperlink_external():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="A1" r:id="rL"/>'])
    assert f"[go]({LINK})" in out and not ctx.skipped, (out, ctx.skipped)


def test_hyperlink_location():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
    out, ctx = linked(rows, ['<hyperlink ref="A1" location="Sheet2!A1"/>'])
    assert "go" in out and "](" not in out and ctx.skipped["link"] == 0, (out, ctx.skipped)


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
    me.ctx, me.scan = ctx, 0
    me._link(grid, [(0, 0, 0, 0, LINK)])
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
    with patched("MAX_COMMENTS", 3, ooxml_sheets):
        out, ctx = commented(rows, leg=leg)
    assert "[^3]" in out and "[^4]" not in out and ctx.skipped["comments_cut"] == 1, (out, ctx.skipped)


def test_comment_with_link():
    rows = xlsx_row(1, xlsx_cell("A1", "go", t="str"))
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
    me.ctx, me.scan = ctx, 0
    me._link(grid, [(0, 0, 0, 0, LINK)])
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
    with patched("MAX_COMMENTS", 3, ooxml_sheets):
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
    assert isinstance(cell[0], Link) and isinstance(cell[-1], NoteRef), cell


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
