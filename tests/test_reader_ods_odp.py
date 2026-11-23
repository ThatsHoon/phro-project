"""ODF spreadsheets and presentations: sheets, values, merges, hidden parts, slides, shapes, notes, hostile input.

Run: python -I -X utf8 tests/test_reader_ods_odp.py
"""
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.formats import odf  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Code, Context, Para, Text  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from reader_fix import make_zip  # noqa: E402

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" '
      'xmlns:script="urn:oasis:names:tc:opendocument:xmlns:script:1.0" '
      'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"')
MIME = "application/vnd.oasis.opendocument."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def tmp():
    return Path(tempfile.mkdtemp())


def package(kind, body_xml, auto_styles="", masters="", ext="ods", extra=None):
    members = {
        "mimetype": MIME + kind,
        "content.xml": (f'<office:document-content {NS}><office:automatic-styles>{auto_styles}'
                        f'</office:automatic-styles><office:body><office:{kind}>{body_xml}'
                        f'</office:{kind}></office:body></office:document-content>'),
        "styles.xml": (f'<office:document-styles {NS}><office:master-styles>{masters}'
                       '</office:master-styles></office:document-styles>'),
        "META-INF/manifest.xml": f'<manifest:manifest {NS}/>',
    }
    members.update(extra or {})
    return make_zip(tmp() / f"a.{ext}", members)


def ods(body_xml, auto_styles="", extra=None):
    return package("spreadsheet", body_xml, auto_styles, extra=extra)


def odp(body_xml, auto_styles="", masters="", extra=None):
    return package("presentation", body_xml, auto_styles, masters, "odp", extra)


def parse(path):
    ctx = Context()
    return odf.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, _ = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden)


def P(text):
    return f"<text:p>{text}</text:p>"


def sheet(name, rows, style=""):
    st = f' table:style-name="{style}"' if style else ""
    return f'<table:table table:name="{name}"{st}>' + "".join(rows) + "</table:table>"


def row(*cells, extra=""):
    return f"<table:table-row {extra}>" + "".join(cells) + "</table:table-row>"


def c(text="", extra=""):
    return f"<table:table-cell {extra}>{P(text) if text else ''}</table:table-cell>"


# ---- spreadsheets
def test_registered():
    assert {"ods", "odp"} <= reader.BUILT


def test_repeat_trap_fast():
    xml = sheet("S", [row(c("x"), c(extra='table:number-columns-repeated="16382"')),
                      row(c(extra='table:number-columns-repeated="16384"'), extra='table:number-rows-repeated="1048000"')])
    path = ods(xml)
    t0 = time.monotonic()
    out = md(path)
    assert time.monotonic() - t0 < 2
    assert out == "## Sheet: S\n\nA1: x\n", repr(out)


def test_display_text_first():
    cur = c("₩1,385.50", 'office:value-type="currency" office:value="1385.5"')
    pct = c("", 'office:value-type="percentage" office:value="0.25"')
    flt = c("", 'office:value-type="float" office:value="3.0"')
    doc, _ = parse(ods(sheet("S", [row(cur, pct, flt)])))
    t = doc.blocks[0].blocks[0]
    assert (t.caption, t.origin) == ("A1:C1", "A1"), (t.caption, t.origin)
    assert [c.blocks[0].inlines[0].text for c in t.rows[0]] == ["₩1,385.50", "25%", "3"], t.rows


def test_interior_repeat_kept():
    out = md(ods(sheet("S", [row(c("a", 'table:number-columns-repeated="3"')), row(c("b"))])))
    assert "\na|a|a\n" in out.replace(" ", ""), out


def test_formulas_read():
    f = 'office:value-type="float" office:value="{}" table:formula="{}"'
    xml = sheet("S", [row(c("1"), c("2", f.format(2, "of:=[.A1]+1")),
                          c("", 'table:formula="of:=SUM([$Other.A1:.A3];&quot;a;b&quot;)"')),
                      row(c("3"), c("4", f.format(4, "of:=[.A2]+1")), c("x"))])
    doc, _ = parse(ods(xml))
    blocks = doc.blocks[0].blocks
    assert blocks[0].rows[0][2].blocks[0].inlines[0].text == '=SUM(Other!A1:A3,"a;b")', blocks[0].rows[0]
    p = blocks[-1]
    assert "".join(i.text for i in p.inlines) == 'Formulas: B1:B2 =A1+1; C1 =SUM(Other!A1:A3,"a;b")', p


def test_formula_of_a_cell_without_a_value_is_listed():
    f = 'office:value-type="string" office:string-value="" table:formula="{}"'
    xml = sheet("S", [row(c("1"), c("", f.format("of:=IF([.A1]>4;&quot;&quot;;[.A1]*2)"))),
                      row(c("2"), c("4", f.format("of:=IF([.A2]>4;&quot;&quot;;[.A2]*2)")))])
    doc, ctx = parse(ods(xml))
    p = doc.blocks[0].blocks[-1]
    assert "".join(i.text for i in p.inlines) == 'Formulas: B1:B2 =IF(A1>4,"",A1*2)' and not ctx.skipped, p


def test_repeated_empty_merges_and_formulas_are_budgeted():
    merged = c(extra='table:number-columns-repeated="1000" table:number-columns-spanned="2"')
    xml = sheet("S", [row(c("x")), row(merged, extra='table:number-rows-repeated="100000"')])
    t0 = time.monotonic()
    doc, ctx = parse(ods(xml))
    assert time.monotonic() - t0 < 20 and ctx.skipped["sheet_cut"] == 1, (time.monotonic() - t0, ctx.skipped)
    form = c(extra='table:number-columns-repeated="1000" table:formula="of:=[.A1]+1" office:value-type="string" '
                   'office:string-value=""')
    xml = sheet("S", [row(c("x")), row(form, extra='table:number-rows-repeated="100000"')]) + sheet("T", [row(c("kept"))])
    t0 = time.monotonic()
    old = limits.BLANK_FORMULAS
    limits.BLANK_FORMULAS = 5000
    try:
        doc, ctx = parse(ods(xml))
    finally:
        limits.BLANK_FORMULAS = old
    assert time.monotonic() - t0 < 20 and not ctx.skipped["sheet_cut"], (time.monotonic() - t0, ctx.skipped)
    assert ctx.skipped["formula"] >= 100_000 * 1000 - 5000 - limits.FORMULA_GROUPS, ctx.skipped
    assert "kept" in markdown.render(doc, tmp() / "a"), ctx.skipped
    spanned = "".join(c(extra='table:number-columns-spanned="2"') + "<table:covered-table-cell/>" for _ in range(1000))
    xml = "".join(sheet(f"S{k}", [row(c("x")), row(spanned, extra='table:number-rows-repeated="100000"')])
                  for k in range(3))
    t0 = time.monotonic()
    doc, ctx = parse(ods(xml))
    assert time.monotonic() - t0 < 12, (time.monotonic() - t0, ctx.skipped)


def test_repeated_formula_text_is_budgeted():
    long = "of:=" + "+".join(["[.A1]"] * 200)
    for value in ('office:value-type="string" office:string-value=""', 'office:value-type="float" office:value="7"'):
        form = c("7" if "float" in value else "",
                 f'table:number-columns-repeated="1000" table:formula="{long}" {value}')
        xml = sheet("S", [row(c("x")), row(form, extra='table:number-rows-repeated="100000"')]) + \
            sheet("T", [row(c("", 'office:value-type="float" office:value="2" table:formula="of:=[.B1]+1"'))])
        t0 = time.monotonic()
        doc, ctx = parse(ods(xml))
        out = markdown.render(doc, tmp() / "a")
        assert time.monotonic() - t0 < 25 and ctx.skipped["formula"] > 100_000, (time.monotonic() - t0, ctx.skipped)
        assert ("B1+1" in out) == ("float" not in value), out[-300:]
    merged = c(extra='table:number-columns-spanned="2" table:formula="of:=[.A1]+1" office:value-type="string" '
                     'office:string-value=""')
    old = limits.BLANK_FORMULAS
    limits.BLANK_FORMULAS = 20
    try:
        doc, ctx = parse(ods(sheet("S", [row(c("x")), row(merged, extra='table:number-rows-repeated="500"')])))
    finally:
        limits.BLANK_FORMULAS = old
    assert ctx.skipped["formula"] == 480, ctx.skipped


def test_repeated_absolute_formula_is_read_once():
    form = c("3", 'table:number-columns-repeated="100" office:value-type="float" office:value="3" '
                  'table:formula="of:=[.$A$1]*[$Cfg.$B$2]+100"')
    rel = c("4", 'table:number-columns-repeated="100" office:value-type="float" office:value="4" '
                 'table:formula="of:=[.A1]+1"')
    xml = sheet("S", [row(form, rel, extra='table:number-rows-repeated="50"')]) + sheet("Cfg", [row(c("x"))])
    old = limits.FORMULA_COPY_CHARS
    limits.FORMULA_COPY_CHARS = 1000
    try:
        doc, ctx = parse(ods(xml))
    finally:
        limits.FORMULA_COPY_CHARS = old
    out = markdown.render(doc, tmp() / "a")
    assert "A1:CV50 =$A$1*Cfg!$B$2+100" in out and 0 < ctx.skipped["formula"] < 5000, (ctx.skipped, out[-300:])


def test_value_types():
    cells = [c("", 'office:value-type="date" office:date-value="2026-01-02"'),
             c("", 'office:value-type="time" office:time-value="PT01H02M03S"'),
             c("", 'office:value-type="boolean" office:boolean-value="true"'),
             c("", 'office:value-type="boolean" office:boolean-value="false"'),
             c("", 'office:value-type="float" office:value="2.5" table:formula="of:=A1+1"')]
    out = md(ods(sheet("S", [row(*cells)])))
    for want in ("2026-01-02", "PT01H02M03S", "TRUE", "FALSE", "2.5"):
        assert want in out, out
    assert "A1+1" in out


def test_interior_repeat_kept():
    out = md(ods(sheet("S", [row(c("a", 'table:number-columns-repeated="3"')), row(c("b"))])))
    assert "\na|a|a\n" in out.replace(" ", ""), out


def test_interior_gap_and_trailing_dropped():
    xml = sheet("S", [row(c("a"), c("b")), row(extra='table:number-rows-repeated="500"'), row(c("z"), c("y")),
                      row(extra='table:number-rows-repeated="900000"')])
    doc, ctx = parse(ods(xml))
    blocks = doc.blocks[0].blocks  # the 500 empty rows part two islands; the trailing rows are gone
    assert [(t.caption, len(t.rows)) for t in blocks] == [("A1:B1", 1), ("A502:B502", 1)], blocks
    assert blocks[1].rows[0][0].blocks[0].inlines[0].text == "z" and not ctx.skipped, ctx.skipped


def test_hidden_sheet_by_style():
    styles = '<style:style style:name="ta2" style:family="table"><style:table-properties table:display="false"/></style:style>'
    out = md(ods(sheet("Shown", [row(c("a"), c("b"))]) + sheet("Hidden", [row(c("q"), c("w"))], "ta2"), styles))
    assert "(1 hidden places left out)" in out and "## Sheet: Shown" in out, out
    assert "## Sheet: Hidden (hidden, left out)" in out and "q" not in out.split("Hidden (hidden, left out)")[1], out
    shown = md(ods(sheet("Hidden", [row(c("q"), c("w"))], "ta2") + sheet("Shown", [row(c("a"))]), styles), True)
    assert "## Sheet: Hidden (hidden)" in shown and "left out)" not in shown.replace("hidden places left out)", "")


def test_hidden_row():
    out = md(ods(sheet("S", [row(c("a"), c("b")), row(c("secret"), c("x"), extra='table:visibility="collapse"'),
                             row(c("filtered"), c("y"), extra='table:visibility="filter"'), row(c("c"), c("d"))])))
    assert "secret" not in out and "filtered" not in out and "(2 hidden places left out)" in out, out
    assert "a" in out and "d" in out


def test_hidden_column_left_out_and_counted():
    cols = ('<table:table-column/><table:table-column table:visibility="collapse"/>'
            '<table:table-column table:number-columns-repeated="2" table:visibility="filter"/><table:table-column/>')
    xml = sheet("S", [cols, row(c("a"), c("secret"), c("f1"), c("f2"), c("e")), row(c("b"), c(), c(), c(), c("g"))])
    doc, _ = parse(ods(xml))
    t = doc.blocks[0].blocks[0]
    assert (t.caption, t.hidden_cols) == ("A1:E2 (hidden columns B-D)", frozenset({1, 2, 3})), (t.caption, t.hidden_cols)
    out = md(ods(xml))
    assert "secret" not in out and "f1" not in out and "(3 hidden places left out)" in out and "e" in out, out
    assert "secret" in md(ods(xml), True)


def test_sheet_merge():
    out = md(ods(sheet("S", [row(c("head", 'table:number-columns-spanned="2"'), "<table:covered-table-cell/>"),
                             row(c("l"), c("r"))])))
    assert "\n|head||\n-|-\nl|r\n\nMerged: A1:B1\n" in out, out


def test_header_rows_flag():
    xml = ('<table:table table:name="S"><table:table-header-rows>' + row(c("H1"), c("H2")) +
           '</table:table-header-rows>' + row(c("v1"), c("v2")) + '</table:table>')
    doc, _ = parse(ods(xml))
    tbl = doc.blocks[0].blocks[0]
    assert tbl.rows[0][0].header and not tbl.rows[1][0].header


def test_sheets_in_order():
    out = md(ods(sheet("A", [row(c("a"), c("b"))]) + sheet("B", [row(c("c"), c("d"))])))
    assert out.index("## Sheet: A") < out.index("## Sheet: B")


def test_empty_sheet_kept():
    out = md(ods(sheet("Empty", []) + sheet("Full", [row(c("a"), c("b"))])))
    assert "## Sheet: Empty" in out and "## Sheet: Full" in out


def test_columns_cut():
    path = ods(sheet("S", [row(c("x", 'table:number-columns-repeated="16384"'), extra='table:number-rows-repeated="1048000"')]))
    t0 = time.monotonic()
    doc, ctx = parse(path)
    assert time.monotonic() - t0 < 2
    assert f"sheet S cut at {limits.SHEET_COLS} columns" in ctx.warnings, ctx.warnings
    assert len(doc.blocks[0].blocks[0].rows) == 1


def test_characters_cut():
    path = ods(sheet("S", [row(c("x" * 1000, 'table:number-columns-repeated="1000"'), extra='table:number-rows-repeated="1000"')]))
    t0 = time.monotonic()
    out = md(path)
    assert time.monotonic() - t0 < 3
    doc, ctx = parse(path)
    assert f"sheet S cut at {limits.SHEET_CHARS} characters" in ctx.warnings, ctx.warnings
    assert len(doc.blocks[0].blocks[0].rows) == limits.SHEET_CHARS // 1_000_000 and "xxxx" in out


def test_cells_cut():
    path = ods(sheet("S", [row(c("x", 'table:number-columns-repeated="1000"'), extra='table:number-rows-repeated="1048000"')]))
    t0 = time.monotonic()
    doc, ctx = parse(path)
    assert time.monotonic() - t0 < 2
    assert f"sheet S cut at {limits.SHEET_CELLS} cells" in ctx.warnings, ctx.warnings
    assert len(doc.blocks[0].blocks[0].rows) == limits.SHEET_CELLS // 1000 == 500


def test_rows_cut():
    path = ods(sheet("S", [row(c("x"), extra='table:number-rows-repeated="1048000"')]))
    t0 = time.monotonic()
    doc, ctx = parse(path)
    assert time.monotonic() - t0 < 2
    assert f"sheet S cut at {limits.SHEET_ROWS} rows" in ctx.warnings, ctx.warnings
    assert len(doc.blocks[0].blocks[0].rows) == limits.SHEET_ROWS
    out = markdown.render(doc, tmp() / "assets", skipped=ctx.skipped)
    assert out.startswith("(could not read: 1 sheet cut short)\n\n## Sheet: S"), out[:200]


def test_hostile_span_clamped():
    xml = sheet("S", [row(c("a", 'table:number-columns-spanned="999999999" table:number-rows-spanned="999999999"'), c("b")),
                      row(c("c"), c("d"))])
    doc, _ = parse(ods(xml))
    # the spans are clamped to 1000; the cells under the merge are covered, so it is a lone cell led by its range
    assert doc.blocks[0].blocks == [Para([Text("A1:ALL1000: "), Text("a")])], doc.blocks[0].blocks


def test_nested_groups_depth():
    xml = '<table:table table:name="S">' + "<table:table-row-group>" * 200 + row(c("deep"), c("x")) + "</table:table-row-group>" * 200 + "</table:table>"
    doc, ctx = parse(ods(xml + sheet("T", [row(c("ok"), c("fine"))])))
    assert ctx.skipped["depth"] >= 1
    assert "ok" in markdown.render(doc, tmp() / "a")


def test_duplicate_sheet_names_kept():
    out = md(ods(sheet("S", [row(c("one"), c("1"))]) + sheet("S", [row(c("two"), c("2"))])))
    assert out.count("## Sheet: S") == 2 and "one" in out and "two" in out


def test_damaged_sheet_isolated():
    orig = odf.odf_sheets._Sheet.walk

    def boom(self, cont, d, header):
        if self.name == "Bad":
            raise ValueError("boom")
        return orig(self, cont, d, header)
    odf.odf_sheets._Sheet.walk = boom
    try:
        doc, ctx = parse(ods(sheet("Bad", [row(c("x"), c("y"))]) + sheet("Good", [row(c("g"), c("h"))])))
    finally:
        odf.odf_sheets._Sheet.walk = orig
    out = markdown.render(doc, tmp() / "a")
    assert ctx.skipped["sheet"] == 1 and "## Sheet: Good" in out and "Bad" not in out


def test_all_sheets_damaged_raises():
    orig = odf.odf_sheets._Sheet.walk
    odf.odf_sheets._Sheet.walk = lambda self, *a: (_ for _ in ()).throw(ValueError("x"))
    try:
        parse(ods(sheet("A", [row(c("x"))])))
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "damaged"
    finally:
        odf.odf_sheets._Sheet.walk = orig


def test_ods_without_sheets_is_empty():
    doc, _ = parse(ods(""))
    assert doc.blocks == []


# ---- presentations
def frame(text, x="", y="", cls="", extra=""):
    pos = (f' svg:x="{x}"' if x else "") + (f' svg:y="{y}"' if y else "")
    pc = f' presentation:class="{cls}"' if cls else ""
    return f'<draw:frame{pos}{pc} {extra}><draw:text-box>{P(text)}</draw:text-box></draw:frame>'


def page(name, *shapes, style="", notes="", master=""):
    st = f' draw:style-name="{style}"' if style else ""
    ms = f' draw:master-page-name="{master}"' if master else ""
    nt = f"<presentation:notes>{notes}</presentation:notes>" if notes else ""
    return f'<draw:page draw:name="{name}"{st}{ms}>' + "".join(shapes) + nt + "</draw:page>"


def test_slide_title_and_numbering():
    styles = ('<style:style style:name="dp2" style:family="drawing-page"><style:drawing-page-properties '
              'presentation:visibility="hidden"/></style:style>')
    out = md(odp(page("p1", frame("A", cls="title"), frame("body1")) + page("p2", frame("B", cls="title"), style="dp2")
                 + page("p3", frame("C", cls="title"), frame("body3")), styles))
    assert "## Slide 1: A" in out and "## Slide 3: C" in out and "(1 hidden places left out)" in out, out
    assert "Slide 2" not in out and out.count("A") == 1 and "body1" in out and "body3" in out


def test_custom_slide_name_is_the_title_when_none_else():
    out = md(odp(page("Quarterly plan", frame("body")) + page("page2", frame("b2")) + page("Slide 3", frame("b3"))
                 + page("슬라이드 4", frame("b4")) + page("Folie 5") + page("", frame("b6"))
                 + page("Named", frame("T", cls="title"), frame("b7"))))
    assert "## Slide 1: Quarterly plan" in out, out
    for n in (2, 3, 4, 6):
        assert f"## Slide {n}" + chr(10) in out, out
    assert "## Slide 7: T" in out and "Named" not in out, out
    long = md(odp(page("x" * 500, frame("body"))))
    assert "## Slide 1: " + "x" * 199 in long and "x" * 201 not in long, long


def test_custom_shape_and_group():
    cs = f'<draw:custom-shape>{P("모양글")}<draw:enhanced-geometry/></draw:custom-shape>'
    grp = f'<draw:g>{frame("묶음글")}<draw:g>{frame("깊은글")}</draw:g></draw:g>'
    out = md(odp(page("p", cs, grp)))
    for want in ("모양글", "묶음글", "깊은글"):
        assert want in out, out


def test_notes_skip_page_number():
    notes = (f'<draw:page-thumbnail presentation:class="page"/>'
             f'<draw:frame presentation:class="page-number"><draw:text-box>{P("&lt;숫자&gt;")}</draw:text-box></draw:frame>'
             f'<draw:frame presentation:class="notes"><draw:text-box>{P("발표 메모")}</draw:text-box></draw:frame>')
    out = md(odp(page("p", frame("본문"), notes=notes)))
    assert "> Notes: 발표 메모" in out and "숫자" not in out, out


def test_slide_skips_footer_classes():
    out = md(odp(page("p", frame("keep"), frame("n1", cls="page-number"), frame("d1", cls="date-time"),
                      frame("h1", cls="header"), frame("f1", cls="footer"))))
    assert "keep" in out and not any(w in out for w in ("n1", "d1", "h1", "f1")), out


def test_master_placeholder_skipped():
    masters = ('<style:master-page style:name="M">'
               + frame("Click to edit", cls="title") + frame("outline text", cls="outline")
               + frame("ph", extra='presentation:placeholder="true"') + frame("ACME", x="1cm", y="1cm")
               + '</style:master-page>')
    out = md(odp(page("p1", frame("one"), master="M") + page("p2", frame("two"), master="M"), masters=masters))
    assert out.count("ACME") == 1 and "Click to edit" not in out and "outline text" not in out and "ph" not in out.replace("phro", ""), out
    assert out.index("ACME") < out.index("one")


def test_master_footer_text_kept_when_slides_show_it():
    shown = ('<style:style style:name="dp1" style:family="drawing-page"><style:drawing-page-properties '
             'presentation:display-footer="true"/></style:style>')
    hidden = shown.replace("dp1", "dp2").replace("true", "false")
    masters = ('<style:master-page style:name="M">' + frame("Footer line", cls="footer")
               + frame("", cls="footer") + '</style:master-page>')
    two = page("p1", style="dp1", master="M") + page("p2", style="dp1", master="M")
    out = md(odp(two, shown, masters=masters))
    assert out.count("Footer line") == 1, out
    off = md(odp(page("p1", style="dp2", master="M"), shown + hidden, masters=masters))
    assert "Footer line" not in off, off
    plain = md(odp(page("p1", master="M"), masters=masters))
    assert "Footer line" not in plain, plain


def test_shape_order_by_position():
    out = md(odp(page("p", frame("LOW", "1cm", "5cm"), frame("HIGH", "1cm", "2cm"))))
    assert out.index("HIGH") < out.index("LOW"), out


def test_shape_order_units_and_unpositioned():
    out = md(odp(page("p", frame("NOPOS"), frame("CCC", "0cm", "30mm"), frame("AAA", "0cm", "1in"),
                      frame("BBB", "0cm", "36pt"), frame("UNPARSE", "x", "y"), frame("DDD", "0cm", "192px"))))
    order = [out.index(k) for k in ("NOPOS", "BBB", "AAA", "CCC", "UNPARSE", "DDD")]
    assert order == sorted(order), out


def _box(text, x, y, w, h, cls=""):
    return frame(text, f"{x}cm", f"{y}cm", cls, f'svg:width="{w}cm" svg:height="{h}cm"')


def test_odp_reading_order_cuts_columns_and_rows_off_by_a_fraction():
    shapes = [_box("Cons", 10, 2.1, 8, 0.9), _box("Vendor lock-in", 10, 4.1, 8, 0.9), _box("Pros", 0, 2, 8, 0.9),
              _box("Fast to build", 0, 4, 8, 0.9), _box("Comparison", 0, 12, 18, 1, "title"),
              _box("Plan", 0, 8, 5.6, 0.8), _box("Build", 6, 8.08, 5.6, 0.8), _box("Launch", 12, 7.92, 5.6, 0.8)]
    out = md(odp(page("p", *shapes)))
    assert out == ("## Slide 1: Comparison\n\nPros\n\nFast to build\n\nCons\n\nVendor lock-in\n\nPlan\n\nBuild\n\n"
                   "Launch\n"), repr(out)


def test_odp_master_and_footer_content_is_written_once():
    style = ('<style:style style:name="{0}" style:family="drawing-page"><style:drawing-page-properties '
             'presentation:display-footer="{1}" presentation:background-objects-visible="{2}"/></style:style>')
    styles = style.format("dp1", "true", "true") + style.format("dp3", "true", "false") + style.format("dp4", "false",
                                                                                                        "true")
    field = ('<draw:frame presentation:class="footer"><draw:text-box><text:p><presentation:footer/></text:p>'
             '</draw:text-box></draw:frame>')
    masters = ('<style:master-page style:name="M">' + frame("Click to edit", cls="title") + field
               + frame("ACME", x="1cm", y="17cm") + '</style:master-page>')
    decl = '<presentation:footer-decl presentation:name="ftr1">Board 2026</presentation:footer-decl>'

    def pg(name, text, st):
        return (f'<draw:page draw:name="{name}" draw:style-name="{st}" draw:master-page-name="M" '
                f'presentation:use-footer-name="ftr1">{frame(text)}</draw:page>')
    out = md(odp(decl + pg("page1", "one", "dp1") + pg("page2", "two", "dp1") + pg("page3", "three", "dp3")
                 + pg("page4", "four", "dp4"), styles, masters=masters))
    assert out == ("## On slides 1-3\n\nBoard 2026\n\n## On slides 1-2, 4\n\nACME\n\n## Slide 1\n\none\n\n"
                   "## Slide 2\n\ntwo\n\n## Slide 3\n\nthree\n\n## Slide 4\n\nfour\n"), repr(out)


def test_odp_shape_and_picture_links_jumps_and_media_are_named():
    def clicked(action, href=""):
        h = f' xlink:href="{href}"' if href else ""
        return ('<office:event-listeners><presentation:event-listener script:event-name="dom:click" '
                f'presentation:action="{action}"{h}/></office:event-listeners>')
    pic = '<draw:frame svg:x="0cm" svg:y="{0}cm"><draw:image xlink:href="Pictures/a.png"/>{1}</draw:frame>'
    shapes = ['<draw:a xlink:href="https://example.com/x">' + frame("See it", "0cm", "1cm") + '</draw:a>',
              frame("Compare", "0cm", "2cm", extra="").replace("</draw:text-box>",
                                                                "</draw:text-box>" + clicked("show", "#page2")),
              f'<draw:custom-shape svg:x="0cm" svg:y="3cm">{clicked("next-page")}<draw:enhanced-geometry/>'
              '</draw:custom-shape>',
              '<draw:a xlink:href="https://example.com/dash">' + pic.format(4, "") + '</draw:a>',
              '<draw:frame draw:name="launch-demo" svg:x="0cm" svg:y="5cm"><draw:plugin xlink:href="Media/clip.mp4" '
              'draw:mime-type="video/mp4"/><draw:image xlink:href="Pictures/a.png"/></draw:frame>']
    doc, ctx = parse(odp(page("page1", *shapes) + page("page2", frame("two")), extra={"Pictures/a.png": PNG}))
    out = markdown.render(doc, tmp() / "assets", False, ctx.skipped)
    assert out == ("## Slide 1\n\n[See it](https://example.com/x)\n\nCompare (→ Slide 2)\n\nLink (→ next slide)\n\n"
                   "![](assets/img-001.png)\n\n[Picture](https://example.com/dash)\n\n"
                   "Video: launch-demo (poster below)\n\n![](assets/img-001.png)\n\n## Slide 2\n\ntwo\n"), repr(out)


def test_impress_table_in_frame():
    tbl = ('<draw:frame svg:x="0cm" svg:y="0cm"><table:table><table:table-row><table:table-cell>' + P("t1") +
           '</table:table-cell><table:table-cell>' + P("t2") + '</table:table-cell></table:table-row></table:table></draw:frame>')
    out = md(odp(page("p", tbl)))
    assert "t1" in out and "t2" in out and "|" in out, out


def test_image_in_slide():
    img = '<draw:frame svg:x="0cm" svg:y="0cm"><draw:image xlink:href="Pictures/a.png"/></draw:frame>'
    doc, ctx = parse(odp(page("p", img), extra={"Pictures/a.png": PNG}))
    assert any(type(b).__name__ == "Image" for b in doc.blocks[0].blocks)
    doc, ctx = parse(odp(page("p", img.replace("Pictures/a.png", "../x.png") + frame("ok"))))
    assert ctx.skipped["image"] == 1 and "ok" in markdown.render(doc, tmp() / "a")


def test_damaged_page_isolated():
    orig = odf.odf_slides._Slides.slide

    def boom(self, pg, number):
        if number == 2:
            raise ValueError("boom")
        return orig(self, pg, number)
    odf.odf_slides._Slides.slide = boom
    try:
        doc, ctx = parse(odp(page("a", frame("A", cls="title")) + page("b", frame("B", cls="title")) + page("c", frame("C", cls="title"))))
    finally:
        odf.odf_slides._Slides.slide = orig
    out = markdown.render(doc, tmp() / "a")
    assert ctx.skipped["slide"] == 1 and "## Slide 1: A" in out and "## Slide 3: C" in out and "Slide 2" not in out


def test_odp_without_pages_is_empty():
    doc, _ = parse(odp(""))
    assert doc.blocks == []


def test_group_depth_capped():
    xml = page("p", "<draw:g>" * 200 + frame("deep") + "</draw:g>" * 200) + page("q", frame("fine"))
    doc, ctx = parse(odp(xml))
    assert ctx.skipped["depth"] >= 1 and "fine" in markdown.render(doc, tmp() / "a")


def test_shared_body_notes_document_wide():
    ann = '<office:annotation><text:p>memo</text:p></office:annotation>'
    xml = page("a", frame("x" + ann)) + page("b", frame("y" + ann))
    doc, _ = parse(odp(xml))
    assert len(doc.notes) == 2


# ---- connectors
def shape(key, text, x, y="0cm", kind=""):
    return (f'<draw:custom-shape {key} svg:x="{x}" svg:y="{y}" svg:width="2cm" svg:height="1cm">{P(text)}'
            f'<draw:enhanced-geometry draw:type="{kind}"/></draw:custom-shape>')


def connector(a, b, text="", extra=""):
    return (f'<draw:connector draw:start-shape="{a}" draw:end-shape="{b}" svg:x1="1cm" svg:y1="1cm" svg:x2="1cm" '
            f'svg:y2="5cm" {extra}>{P(text) if text else ""}</draw:connector>')


def mermaid(blocks):
    return "\n".join(b.text for b in blocks if isinstance(b, Code) and b.lang == "mermaid")


def test_odp_connector():
    shapes = (frame("Heading", "0cm", "0cm", "title") + shape('draw:id="a" xml:id="ignored"', "Top", "0cm", "1cm", "Flowchart-Terminator")
              + shape('xml:id="b"', "Bottom", "0cm", "5cm") + f'<draw:ellipse draw:id="e" svg:x="9cm" svg:y="9cm">{P("Round")}</draw:ellipse>'
              + connector("a", "b", "yes") + connector("b", "e") + frame("Loose text", "0cm", "7cm"))
    doc, ctx = parse(odp(page("p", shapes)))
    t = mermaid(doc.blocks[0].blocks)
    assert mermaid_ok(t) and t == ('flowchart TD\n  n1(["Top"])\n  n2["Bottom"]\n  n3(("Round"))\n'
                                   '  n1 -->|"yes"| n2\n  n2 --> n3'), t
    out = markdown.render(doc, tmp() / "a")
    for word in ("Top", "Bottom", "Round", "yes", "Heading", "Loose text"):
        assert out.count(word) == 1, (word, out)
    assert out.startswith("## Slide 1: Heading\n\n```mermaid\n") and out.index("```\n") < out.index("Loose text"), out
    assert not ctx.skipped["connector_loose"] and not ctx.skipped["shape"], ctx.skipped


def test_odp_connector_long_text_follows():
    long = "w" * 90
    shapes = (shape('draw:id="a"', "Top", "0cm", "1cm") + shape('draw:id="b"', "Bottom", "0cm", "5cm")
              + connector("a", "b", long))
    doc, ctx = parse(odp(page("p", shapes)))
    t = mermaid(doc.blocks[0].blocks)
    assert mermaid_ok(t) and t.endswith('\n  n1 -->|"' + "w" * 80 + '…"| n2'), t
    out = markdown.render(doc, tmp() / "a")
    assert out.endswith(f"```\n\nn1 --> n2:\n\n{long}\n") and out.count(long) == 1, out


def test_odp_marker_from_style_chain():
    styles = ('<style:style style:name="Base" style:family="graphic"><style:graphic-properties draw:marker-end="Arrow"/>'
              '</style:style><style:style style:name="gr1" style:family="graphic" style:parent-style-name="Base"/>'
              '<style:style style:name="gr2" style:family="graphic" style:parent-style-name="Base">'
              '<style:graphic-properties draw:marker-end="none"/></style:style>'
              '<style:style style:name="gr3" style:family="graphic" style:parent-style-name="Base">'
              '<style:graphic-properties draw:marker-start="Arrow"/></style:style>'
              '<style:style style:name="gr4" style:family="graphic"><style:graphic-properties draw:marker-start=""/>'
              '</style:style>'
              '<style:style style:name="pr1" style:family="presentation"><style:graphic-properties '
              'draw:marker-start="Arrow"/></style:style>')
    pair = shape('draw:id="a"', "A", "0cm", "1cm") + shape('draw:id="b"', "B", "0cm", "5cm")
    want = {"gr1": "n1 --> n2", "gr2": "n1 --- n2", "gr3": "n1 <--> n2", "gr4": "n1 --- n2"}
    pages = "".join(page(f"p{i}", pair, connector("a", "b", extra=f'draw:style-name="{s}"'))
                    for i, s in enumerate(want))
    pages += page("pres", pair, connector("a", "b", extra='presentation:style-name="pr1"'))
    doc, _ = parse(odp(pages, styles))
    for sec, edge in zip(doc.blocks, list(want.values()) + ["n2 --> n1"]):
        t = mermaid(sec.blocks)
        assert mermaid_ok(t) and t.endswith("\n  " + edge), (edge, t)


def test_ods_shapes_connector():
    drawn = ('<table:shapes>' + shape('draw:id="a"', "Left", "0cm") + '<draw:g>' + shape('draw:id="b"', "Right", "6cm")
             + '</draw:g><draw:connector draw:start-shape="a" draw:end-shape="b" svg:x1="2cm" svg:y1="0.5cm" '
             'svg:x2="6cm" svg:y2="0.5cm"/>' + shape('draw:id="c"', "Unlinked", "0cm", "9cm") + frame("Framed")
             + '</table:shapes>')
    xml = '<table:table table:name="S">' + drawn + row(c("x"), c("y")) + '</table:table>'
    doc, ctx = parse(ods(xml))
    t = mermaid(doc.blocks[0].blocks)
    assert mermaid_ok(t) and t == 'flowchart LR\n  n1["Left"]\n  n2["Right"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    # every drawn shape with text keeps it in the flow: the unlinked one after the diagram, before the frame
    assert out == f"## Sheet: S\n\nA1:B1\nx|y\n-|-\n\n```mermaid\n{t}\n```\n\nUnlinked\n\nFramed\n", out
    plain = '<table:table table:name="S"><table:shapes>' + shape('draw:id="a"', "Left", "0cm") + frame("Framed")
    assert md(ods(plain + '</table:shapes>' + row(c("x"), c("y")) + '</table:table>')) == (
        "## Sheet: S\n\nA1:B1\nx|y\n-|-\n\nFramed\n")  # no connector: the sheet reads as before


def test_ods_shapes_text_kept_beside_connector():
    # an unconnected shape, a half-glued connector's label and a grouped frame keep their text beside a diagram
    drawn = ('<table:shapes>' + shape('draw:id="a"', "Start", "0cm") + shape('draw:id="b"', "Stop", "0cm", "3cm")
             + shape('draw:id="z"', "LEGEND", "0cm", "20cm") + connector("a", "b")
             + '<draw:connector draw:start-shape="a" svg:x1="1cm" svg:y1="1cm" svg:x2="30cm" svg:y2="30cm">'
             + P("LOOSE LABEL") + '</draw:connector><draw:g>' + frame("Grouped") + '</draw:g></table:shapes>')
    doc, ctx = parse(ods('<table:table table:name="S">' + drawn + row(c("x")) + '</table:table>'))
    t = mermaid(doc.blocks[0].blocks)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Start"]\n  n2["Stop"]\n  n1 --> n2', t
    out = markdown.render(doc, tmp() / "a")
    assert out == f"## Sheet: S\n\nA1: x\n\n```mermaid\n{t}\n```\n\nLEGEND\n\nLOOSE LABEL\n\nGrouped\n", out
    assert ctx.skipped == {"connector_loose": 1}, ctx.skipped


def test_via_convert():
    out_dir = tmp() / "o"
    path = ods(sheet("S", [row(c("a"), c("b"))]))
    reader.convert(path, out_dir)
    assert "## Sheet: S" in (out_dir / "out.md").read_text(encoding="utf-8")


CHART_NS = 'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0"'


def ods_chart(plot_props):
    head = row(c(), c("1월"), c("2월"))
    rows = [head, row(c("서울"), c("1200"), c("1350")), row(c("부산"), c("860"), c("910"))]
    local = '<table:table table:name="local-table">' + "".join(rows) + "</table:table>"
    return (f'<office:document-content {NS} {CHART_NS}><office:automatic-styles>'
            f'<style:style style:name="ch4" style:family="chart"><style:chart-properties {plot_props}/>'
            '</style:style></office:automatic-styles><office:body><office:chart><chart:chart chart:class="chart:bar">'
            f'<chart:title>{P("지점별 매출")}</chart:title><chart:plot-area chart:style-name="ch4">'
            f'<chart:axis chart:dimension="x" chart:name="primary-x"><chart:title>{P("월")}</chart:title></chart:axis>'
            f'<chart:axis chart:dimension="y" chart:name="primary-y"><chart:title>{P("원")}</chart:title></chart:axis>'
            f'</chart:plot-area>{local}</chart:chart></office:chart></office:body></office:document-content>')


def test_chart_in_a_cell_is_one_table_after_the_sheet():
    anchored = ('<table:table-cell><draw:frame draw:name="Chart 1"><draw:object xlink:href="./Object 1"/>'
                '</draw:frame></table:table-cell>')
    body = sheet("매출", [row(c("지점"), c("1월")), row(c("서울"), c("1200"), anchored), row(c("부산"), c("860"))])
    for props, kind, axes in (('chart:vertical="false"', "column", "x: 월; y: 원"),
                              ('chart:vertical="true" chart:stacked="true"', "stacked bar", "x: 원; y: 월"),
                              ('chart:percentage="true"', "100% stacked column", "x: 월; y: 원")):
        doc, _ = parse(ods(body, extra={"Object 1/content.xml": ods_chart(props)}))
        blocks = doc.blocks[0].blocks
        assert [type(b).__name__ for b in blocks] == ["Table", "Table"], blocks
        grid, chart = blocks
        assert all(len(r) <= 2 for r in grid.rows), grid.rows
        assert chart.caption == f"Chart: {kind} — 지점별 매출 ({axes})", chart.caption
        assert [["".join(x.text for b in c.blocks for x in b.inlines) for c in r] for r in chart.rows] == [
            ["", "1월", "2월"], ["서울", "1200", "1350"], ["부산", "860", "910"]], chart.rows


def test_picture_and_text_box_in_a_cell_stay_in_their_row():
    def pic(n):
        return (f'<table:table-cell><draw:frame draw:name="p{n}"><draw:image xlink:href="Pictures/{n}.png"/>'
                f'<svg:title>photo {n}</svg:title></draw:frame></table:table-cell>')
    box = ('<table:table-cell><draw:frame draw:name="tb"><draw:text-box>' + P("note in box")
           + '</draw:text-box></draw:frame></table:table-cell>')
    rows = [row(c("item"), c("photo")), row(c("apple"), pic(1)),
            row(c("pear"), pic(2), extra='table:visibility="collapse"'), row(c("fig"), box)]
    path = ods(sheet("S", rows), extra={f"Pictures/{i}.png": PNG for i in (1, 2)})
    out = md(path)
    assert "\napple|![photo 1](assets/img-001.png)\n" in out and "\nfig|note in box\n" in out, out
    assert out.startswith("(1 hidden places left out)") and "photo 2" not in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
