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
from reader.model import Context  # noqa: E402
from reader_fix import make_zip  # noqa: E402

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" '
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
    assert out == "## Sheet: S\n\nx\n", repr(out)


def test_display_text_first():
    cur = c("₩1,385.50", 'office:value-type="currency" office:value="1385.5"')
    pct = c("", 'office:value-type="percentage" office:value="0.25"')
    flt = c("", 'office:value-type="float" office:value="3.0"')
    out = md(ods(sheet("S", [row(cur, pct, flt)])))
    assert out == "## Sheet: S\n\n|₩1,385.50|25%|3|\n|---|---|---|\n", repr(out)


def test_interior_repeat_kept():
    out = md(ods(sheet("S", [row(c("a", 'table:number-columns-repeated="3"')), row(c("b"))])))
    assert "|a|a|a|" in out.replace(" ", ""), out


def test_value_types():
    cells = [c("", 'office:value-type="date" office:date-value="2026-01-02"'),
             c("", 'office:value-type="time" office:time-value="PT01H02M03S"'),
             c("", 'office:value-type="boolean" office:boolean-value="true"'),
             c("", 'office:value-type="boolean" office:boolean-value="false"'),
             c("", 'office:value-type="float" office:value="2.5" table:formula="of:=A1+1"')]
    out = md(ods(sheet("S", [row(*cells)])))
    for want in ("2026-01-02", "PT01H02M03S", "TRUE", "FALSE", "2.5"):
        assert want in out, out
    assert "A1+1" not in out


def test_interior_repeat_kept():
    out = md(ods(sheet("S", [row(c("a", 'table:number-columns-repeated="3"')), row(c("b"))])))
    assert "|a|a|a|" in out.replace(" ", ""), out


def test_interior_gap_and_trailing_dropped():
    xml = sheet("S", [row(c("a"), c("b")), row(extra='table:number-rows-repeated="500"'), row(c("z"), c("y")),
                      row(extra='table:number-rows-repeated="900000"')])
    doc, ctx = parse(ods(xml))
    rows = doc.blocks[0].blocks[0].rows
    assert len(rows) == 1 + 64 + 1, len(rows)  # the gap is capped, the trailing rows are gone
    assert rows[-1][0].blocks[0].inlines[0].text == "z" and ctx.skipped["repeat"] == 1


def test_hidden_sheet_by_style():
    styles = '<style:style style:name="ta2" style:family="table"><style:table-properties table:display="false"/></style:style>'
    out = md(ods(sheet("Shown", [row(c("a"), c("b"))]) + sheet("Hidden", [row(c("q"), c("w"))], "ta2"), styles))
    assert "(1 hidden places left out)" in out and "## Sheet: Hidden" not in out and "## Sheet: Shown" in out, out
    shown = md(ods(sheet("Hidden", [row(c("q"), c("w"))], "ta2") + sheet("Shown", [row(c("a"))]), styles), True)
    assert "## Sheet: Hidden (hidden)" in shown


def test_hidden_row():
    out = md(ods(sheet("S", [row(c("a"), c("b")), row(c("secret"), c("x"), extra='table:visibility="collapse"'),
                             row(c("filtered"), c("y"), extra='table:visibility="filter"'), row(c("c"), c("d"))])))
    assert "secret" not in out and "filtered" not in out and "(2 hidden places left out)" in out, out
    assert "a" in out and "d" in out


def test_sheet_merge():
    out = md(ods(sheet("S", [row(c("head", 'table:number-columns-spanned="2"'), "<table:covered-table-cell/>"),
                             row(c("l"), c("r"))])))
    assert '<td colspan="2">' in out and "head" in out, out


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


def test_hostile_span_clamped():
    xml = sheet("S", [row(c("a", 'table:number-columns-spanned="999999999" table:number-rows-spanned="999999999"'), c("b")),
                      row(c("c"), c("d"))])
    doc, _ = parse(ods(xml))
    a = doc.blocks[0].blocks[0].rows[0][0]
    assert a.colspan == 1000 and a.rowspan == 2, (a.colspan, a.rowspan)  # the row span stops at the last row


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


def test_shape_order_by_position():
    out = md(odp(page("p", frame("LOW", "1cm", "5cm"), frame("HIGH", "1cm", "2cm"))))
    assert out.index("HIGH") < out.index("LOW"), out


def test_shape_order_units_and_unpositioned():
    out = md(odp(page("p", frame("NOPOS"), frame("CCC", "0cm", "30mm"), frame("AAA", "0cm", "1in"),
                      frame("BBB", "0cm", "36pt"), frame("UNPARSE", "x", "y"), frame("DDD", "0cm", "192px"))))
    order = [out.index(k) for k in ("BBB", "AAA", "CCC", "DDD", "NOPOS", "UNPARSE")]
    assert order == sorted(order), out


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


def test_via_convert():
    out_dir = tmp() / "o"
    path = ods(sheet("S", [row(c("a"), c("b"))]))
    reader.convert(path, out_dir)
    assert "## Sheet: S" in (out_dir / "out.md").read_text(encoding="utf-8")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
