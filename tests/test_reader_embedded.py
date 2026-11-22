"""Tests for reader.embedded: one dispatcher for the objects a document embeds."""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import embedded, limits  # noqa: E402  (ModuleNotFoundError before embedded.py exists)
from reader.formats import hwp_chart, sheet_grid  # noqa: E402
from reader.model import Context, Equation, Table, Text  # noqa: E402
from reader_fix import (XLSX_SML, cfb_bytes, comp_obj, encrypt_xls, eq_native, excel_object, make_xlsx,  # noqa: E402
                        make_zip, msgraph_object, xlsx_cell, xlsx_row)
import test_reader_doc_objects as in_doc  # noqa: E402
import test_reader_docm as in_docm  # noqa: E402
import test_reader_odt as odt  # noqa: E402
import test_reader_ppt_objects as in_ppt  # noqa: E402
import test_reader_rtf as in_rtf  # noqa: E402
import test_reader_slides as in_slides  # noqa: E402
import test_reader_vsd as in_vsd  # noqa: E402

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
    embedded.of(ctx).depth = depth
    return embedded.read(data, ctx, sheet_grid.Budget()), ctx


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


SHEET_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"


def nest(levels, fan=1, book=""):
    """An xlsx whose one sheet holds `fan` copies of the xlsx of the level below, `levels` deep; the innermost
    holds the cell `deep`."""
    data = make_xlsx(None, [{"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", inline="<t>deep</t>"))}], book=book)
    for level in range(levels):
        rows = xlsx_row(1, xlsx_cell("A1", inline=f"<t>level{level}</t>"))
        data = make_xlsx(None, [{"name": "S", "rows": rows,
                                 "rels": [(f"rO{k}", SHEET_REL, f"../embeddings/b{k}.xlsx", False)
                                          for k in range(fan)]}],
                         extra_parts={f"xl/embeddings/b{k}.xlsx": data for k in range(fan)})
    return data


def host(data):
    from reader.formats import ooxml
    ctx = Context()
    doc = ooxml.parse_package(data, ctx)
    return doc, ctx


def flat(doc):
    out = []

    def walk(blocks):
        for b in blocks:
            if isinstance(b, Table):
                out.extend(c for row in cells([b]) for c in row)
            walk(getattr(b, "blocks", ()))
    walk(doc.blocks)
    return out


def test_workbooks_inside_workbooks_stop_at_depth():
    doc, ctx = host(nest(embedded.DEPTH + 1))
    assert "A1: deep" in flat(doc) and not ctx.skipped, (flat(doc), ctx.skipped)
    doc, ctx = host(nest(embedded.DEPTH + 2))
    got = flat(doc)
    assert "A1: deep" not in got and "A1: level0" in got and ctx.skipped == {"object": 1}, (got, ctx.skipped)


def test_workbooks_inside_workbooks_share_the_object_count():
    doc, ctx = host(nest(3, fan=8))
    objs = embedded.of(ctx)
    assert objs.count == embedded.MAX_OBJECTS and objs.depth == 0, (objs.count, objs.depth)
    assert ctx.skipped["object"] > 0, ctx.skipped


def test_embedded_workbook_reads_one_sheet_under_the_documents_budget():
    ctx = Context()
    budget = embedded.of(ctx).budget()
    before = budget.cells
    got = embedded.read(workbook(), ctx, budget)
    assert cells(got[1]) == [["Item", "Qty"], ["Pen", "3"]] and before - budget.cells == 4, before - budget.cells
    old = limits.SHEET_CELLS
    limits.SHEET_CELLS = 2
    try:
        ctx = Context()
        got = embedded.read(workbook(), ctx, embedded.of(ctx).budget())
    finally:
        limits.SHEET_CELLS = old
    assert got[0] == "table" and sum(ctx.skipped.values()) > 0, (got, ctx.skipped)


def test_embedded_workbooks_scan_under_the_documents_count():
    from reader.formats import ooxml_sheets
    rows = "".join(xlsx_row(r, xlsx_cell(f"A{r}", inline="<t>v</t>")) for r in range(1, 21))
    inner = make_xlsx(None, [{"name": "S", "rows": rows}], shared=["<t>s</t>"] * 30)
    top = make_xlsx(None, [{"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", inline="<t>top</t>")),
                            "rels": [(f"rO{k}", SHEET_REL, f"../embeddings/b{k}.xlsx", False) for k in range(6)]}],
                    extra_parts={f"xl/embeddings/b{k}.xlsx": inner for k in range(6)})
    old = limits.SHEET_SCAN
    limits.SHEET_SCAN = 100
    try:
        doc, ctx = host(top)
    finally:
        limits.SHEET_SCAN = old
    budget = embedded.of(ctx).budget()
    assert 0 < budget.scan <= 101 and budget.strings == ooxml_sheets.SST_ELEMENTS * 100, \
        (budget.scan, budget.strings)
    assert ctx.skipped == {"sheet_cut": 1, "object": 4} and flat(doc).count("v") == 22, (ctx.skipped, flat(doc))


def test_elements_an_embedded_workbook_only_opens_are_counted():
    from reader.formats import ooxml_sheets
    opened = "<a>" * 500
    inner = make_xlsx(None, [{"name": "S", "part": f'<worksheet xmlns="{XLSX_SML}"><sheetData>{opened}'}],
                      shared=["<t>s</t>"], numfmts={164: "0"},
                      extra_parts={"xl/sharedStrings.xml": f'<sst xmlns="{XLSX_SML}">{opened}',
                                   "xl/styles.xml": f'<styleSheet xmlns="{XLSX_SML}">{opened}'})
    top = make_xlsx(None, [{"name": "S", "rows": xlsx_row(1, xlsx_cell("A1", inline="<t>top</t>")),
                            "rels": [(f"rO{k}", SHEET_REL, f"../embeddings/b{k}.xlsx", False) for k in range(6)]}],
                    extra_parts={f"xl/embeddings/b{k}.xlsx": inner for k in range(6)})
    old, real, starts = (limits.SHEET_SCAN, ooxml_sheets.STYLE_ELEMENTS), ooxml_sheets.iterparse, [0]

    def counted(f, events):
        for ev, el in real(f, events):
            starts[0] += ev == "start"
            yield ev, el
    limits.SHEET_SCAN, ooxml_sheets.STYLE_ELEMENTS, ooxml_sheets.iterparse = 100, 50, counted
    try:
        doc, ctx = host(top)
    finally:
        (limits.SHEET_SCAN, ooxml_sheets.STYLE_ELEMENTS), ooxml_sheets.iterparse = old, real
    budget = embedded.of(ctx).budget()
    assert (budget.scan, budget.strings, budget.styles) == (100, 200, 50), (budget.scan, budget.strings, budget.styles)
    assert starts[0] <= 100 + 200 + 50 + 3 * 7, starts[0]
    assert ctx.skipped == {"document": 1, "sheet_cut": 2, "object": 5}, ctx.skipped


def test_embedded_xls_workbooks_share_the_record_count():
    grid = {"name": "S", "cells": {(r, 0): r for r in range(200)}}
    blob = excel_object([grid], b"Excel.Sheet.8")
    ctx = Context()
    alone = embedded.read(blob, ctx, embedded.of(ctx).budget())
    assert len(cells(alone[1])) == 200 and not ctx.skipped, (alone, ctx.skipped)
    used = embedded.of(ctx).records
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = used * 2 + used // 2
    try:
        ctx = Context()
        got = [embedded.read(blob, ctx, embedded.of(ctx).budget()) for _ in range(4)]
    finally:
        limits.BIN_RECORDS = old
    assert [len(cells(g[1])) for g in got[:2]] == [200, 200], got[:2]
    assert embedded.of(ctx).records == used * 2 + used // 2 and ctx.skipped["record"] >= 1, \
        (embedded.of(ctx).records, ctx.skipped)
    assert not got[3][1], got[3]


def test_embedded_packages_inflate_under_one_total():
    import io
    import zipfile
    data = workbook()
    size = sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    old = limits.EMBED_TOTAL
    limits.EMBED_TOTAL = 2 * size
    try:
        ctx = Context()
        got = [embedded.of(ctx).load(data) for _ in range(4)]
    finally:
        limits.EMBED_TOTAL = old
    assert got[0][0] == "table" and got[3] is None, got
    assert 0 < embedded.of(ctx).inflated <= 2 * size, (embedded.of(ctx).inflated, size)


def test_a_package_past_the_total_inflates_nothing():
    import zipfile
    real, reads = zipfile.ZipFile.read, []
    old = limits.EMBED_TOTAL
    limits.EMBED_TOTAL = 0
    zipfile.ZipFile.read = lambda self, *a, **k: (reads.append(a), real(self, *a, **k))[1]
    try:
        ctx = Context()
        got = [embedded.of(ctx).load(data) for data in (workbook(), chart_package())]
    finally:
        limits.EMBED_TOTAL, zipfile.ZipFile.read = old, real
    assert got == [None, None] and not reads and embedded.of(ctx).inflated == 0, (got, reads)


def test_trees_of_embedded_packages_are_built_under_one_count():
    ctx = Context()
    for data in (workbook(), chart_package()):
        assert embedded.of(ctx).load(data)[0] == "table", data[:30]
    used = embedded.of(ctx).nodes
    assert used > 20, used
    old = limits.EMBED_NODES
    limits.EMBED_NODES = used + used // 2
    try:
        ctx = Context()
        got = [embedded.of(ctx).load(data) for data in (workbook(), chart_package()) * 3]
    finally:
        limits.EMBED_NODES = old
    assert [g[0] for g in got[:2]] == ["table", "table"] and got[4:] == [None, None], got
    assert embedded.of(ctx).nodes <= used + used // 2, (embedded.of(ctx).nodes, used)


def test_embedded_xls_workbooks_decrypt_under_one_total():
    grid = {"name": "S", "cells": {(0, 0): 1, (0, 1): 2}}
    stream = encrypt_xls([grid], "VelvetSweatshop", "rc4")
    blob, _ = cfb_bytes({"Workbook": stream, "\x01CompObj": comp_obj(b"Excel.Sheet.8")})
    old = limits.EMBED_DECRYPT
    limits.EMBED_DECRYPT = 2 * len(stream) + len(stream) // 2
    try:
        ctx = Context()
        got = [embedded.read(blob, ctx, embedded.of(ctx).budget()) for _ in range(4)]
    finally:
        limits.EMBED_DECRYPT = old
    assert [cells(g[1]) for g in got[:2]] == [[["1", "2"]]] * 2, got[:2]
    assert not got[3][1] and embedded.of(ctx).decrypted == 2 * len(stream), (got[3], embedded.of(ctx).decrypted)


def test_a_graph_objects_tail_walk_counts_its_records():
    walked = []
    for at in ("in", "after"):
        ctx = Context()
        blob = msgraph_object(["Q1"], [("East", [1])], codepage=949, codepage_at=at)
        got = embedded.read(blob, ctx, embedded.of(ctx).budget())
        assert got[0] == "table" and not ctx.skipped, (at, got, ctx.skipped)
        walked.append(embedded.of(ctx).records)
    assert walked[0] == walked[1], walked


def test_a_parser_error_costs_only_the_object():
    bad = nest(0, book='<bookViews><workbookView activeTab="²"/></bookViews>')
    got, ctx = read(bad)
    assert got is not None and cells(got[1]) == [["A1: deep"]], got
    got, ctx = read(nest(0, book='<bookViews><workbookView activeTab="' + "9" * 5000 + '"/></bookViews>'))
    assert cells(got[1]) == [["A1: deep"]], got
    saved = embedded._package
    embedded._package = lambda *a: 1 // 0
    try:
        got, ctx = read(workbook())
        assert got is None, got
        p = docx([("rO", PKG_REL, "embeddings/book.xlsx", False)], {"word/embeddings/book.xlsx": workbook()})
        doc, ctx = in_docm.parse(p)
        out = in_docm.md(p)
        assert out.startswith("beforeafter\n\n![preview") and ctx.skipped == {"object": 1}, (out, ctx.skipped)
        d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a " + rtf_object(ole1(workbook(), b"Excel.Sheet.12"))
                                              + b"\\par}"))
        assert ctx.skipped == {"object": 1}, ctx.skipped
    finally:
        embedded._package = saved


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


OLE_REL = "oleObject"
PKG_REL = "package"


def word_object(rid, pic="rP"):
    return ('<w:p><w:r><w:t>before</w:t></w:r><w:r><w:object><v:shape id="s1">'
            f'<v:imagedata r:id="{pic}" o:title="preview"/></v:shape>'
            f'<o:OLEObject xmlns:o="urn:schemas-microsoft-com:office:office" Type="Embed" ProgID="Excel.Sheet.12" '
            f'ShapeID="s1" DrawAspect="Content" ObjectID="_1" r:id="{rid}"/></w:object></w:r>'
            '<w:r><w:t>after</w:t></w:r></w:p>').replace("o:title", "title")


def docx(rels, parts):
    return in_docm.docm(word_object("rO"), parts={"word/media/p.png": in_docm.PNG, **parts},
                        rels=[("rP", "image", "media/p.png", False)] + rels)


def test_xlsx_inside_a_docx():
    p = docx([("rO", PKG_REL, "embeddings/book.xlsx", False)], {"word/embeddings/book.xlsx": workbook()})
    doc, ctx = in_docm.parse(p)
    out = in_docm.md(p)
    assert out == "beforeafter\n\nA1:B2\nItem|Qty\n-|-\nPen|3\n" and not ctx.skipped, (out, ctx.skipped)
    blob, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
    p = docx([("rO", OLE_REL, "embeddings/eq.bin", False)], {"word/embeddings/eq.bin": blob})
    assert in_docm.md(p) == "before$x$after\n", in_docm.md(p)
    blob, _ = cfb_bytes({"CONTENTS": b"%PDF-1.4"})
    p = docx([("rO", OLE_REL, "embeddings/o.bin", False)], {"word/embeddings/o.bin": blob})
    out = in_docm.md(p)
    assert out.startswith("beforeafter\n\n![preview") and "img-001.png" in out, out
    assert in_docm.parse(p)[1].skipped == {"object": 1}
    p = docx([("rO", OLE_REL, "https://example.com/x.xlsx", True)], {})
    assert in_docm.md(p).startswith("beforeafter\n\n![preview"), in_docm.md(p)
    assert not in_docm.parse(p)[1].skipped
    p = docx([], {})
    assert in_docm.md(p).startswith("beforeafter\n\n![preview"), in_docm.md(p)
    blob, _ = cfb_bytes({"Equation Native": b"\x00" * 40})
    p = docx([("rO", OLE_REL, "embeddings/eq.bin", False)], {"word/embeddings/eq.bin": blob})
    assert in_docm.parse(p)[1].skipped == {"object": 1}
    blob, _ = cfb_bytes({"CONTENTS": b"%PDF-1.4"})
    p = in_docm.docm(word_object("rO").replace('<v:imagedata r:id="rP" title="preview"/>', ""),
                     parts={"word/embeddings/o.bin": blob}, rels=[("rO", OLE_REL, "embeddings/o.bin", False)])
    assert in_docm.md(p) == "beforeafter\n" and in_docm.parse(p)[1].skipped == {"object": 1}, in_docm.md(p)


def ole_frame(rid, pic=None):
    inner = (f'<p:pic><p:nvPicPr><p:cNvPr id="9" name="preview"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
             f'<p:blipFill><a:blip r:embed="{pic}"/></p:blipFill><p:spPr/></p:pic>') if pic else ""
    return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Object 1"/><p:cNvGraphicFramePr/><p:nvPr/>'
            '</p:nvGraphicFramePr><p:xfrm><a:off x="0" y="900"/><a:ext cx="100" cy="100"/></p:xfrm><a:graphic>'
            '<a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
            f'<p:oleObj name="Worksheet" r:id="{rid}" progId="Excel.Sheet.12"><p:embed/>{inner}</p:oleObj>'
            '</a:graphicData></a:graphic></p:graphicFrame>')


def test_ole_object_workbook_on_a_slide():
    p = in_slides.deck([in_slides.sp("Title", ph="title") + ole_frame("rO", "rP")],
                       srels={1: [("rO", PKG_REL, "../embeddings/book.xlsx"), ("rP", "image", "../media/p.png")]},
                       extra={"ppt/embeddings/book.xlsx": workbook(), "ppt/media/p.png": in_docm.PNG})
    doc, ctx = in_slides.parse(p)
    out = in_slides.md(p)
    assert out == "## Slide 1: Title\n\nA1:B2\nItem|Qty\n-|-\nPen|3\n" and not ctx.skipped, (out, ctx.skipped)
    blob, _ = cfb_bytes({"CONTENTS": b"%PDF-1.4"})
    p = in_slides.deck([ole_frame("rO", "rP")],
                       srels={1: [("rO", OLE_REL, "../embeddings/o.bin"), ("rP", "image", "../media/p.png")]},
                       extra={"ppt/embeddings/o.bin": blob, "ppt/media/p.png": in_docm.PNG})
    doc, ctx = in_slides.parse(p)
    out = in_slides.md(p)
    assert "img-001.png" in out and ctx.skipped == {"object": 1}, (out, ctx.skipped)
    p = in_slides.deck([ole_frame("rO")], srels={1: [("rO", OLE_REL, "../embeddings/o.bin")]},
                       extra={"ppt/embeddings/o.bin": blob})
    doc, ctx = in_slides.parse(p)
    assert ctx.skipped == {"object": 1}, ctx.skipped


def test_ole_object_in_a_sheet():
    rows = xlsx_row(1, xlsx_cell("A1", inline="<t>host</t>"), xlsx_cell("B1", "1"))
    after = '<oleObjects><oleObject progId="Excel.Chart.8" shapeId="1025" r:id="rO"/></oleObjects>'
    blob, _ = cfb_bytes({"package_stream": chart_package()})
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"
    data = make_xlsx(None, [{"name": "S", "rows": rows, "after": after,
                             "rels": [("rO", rel, "../embeddings/o.bin", False)]}],
                     extra_parts={"xl/embeddings/o.bin": blob})
    from reader import markdown
    from reader.formats import ooxml
    import tempfile
    ctx = Context()
    out = markdown.render(ooxml.parse_package(data, ctx), Path(tempfile.mkdtemp()), False, ctx.skipped)
    assert out == "## Sheet: S\n\nA1:B1\nhost|1\n-|-\n\nChart: column — 매출\n연도|값\n-|-\n2025|3\n", out


def ole1(native, cls=b"Equation.3"):
    """An OLE1 embedded object as \\objdata holds it: version, format 2, class, empty topic and item, native data, no
    presentation."""
    return (struct.pack("<II", 0x501, 2) + struct.pack("<I", len(cls) + 1) + cls + b"\0" + struct.pack("<II", 0, 0)
            + struct.pack("<I", len(native)) + native + struct.pack("<II", 0x501, 0))


def rtf_object(data, result=b"{\\result preview}", lead=b"{\\*\\objclass Equation.3}"):
    return b"{\\object\\objemb" + lead + b"{\\*\\objdata " + data.hex().encode() + b"}" + result + b"}"


def test_rtf_object_equation_stands_in_the_line():
    blob, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
    got = in_rtf.md(b"{\\rtf1 a " + rtf_object(ole1(blob)) + b" b\\par}")
    assert got == "a $x$ b\n", repr(got)
    got = in_rtf.md(b"{\\rtf1 " + rtf_object(ole1(blob), result=b"") + b"\\par next\\par}")
    assert got == "$x$\n\nnext\n", repr(got)


def test_rtf_object_workbook_follows_its_paragraph():
    blob, _ = cfb_bytes({"Package": workbook()})
    got = in_rtf.md(b"{\\rtf1 a " + rtf_object(ole1(blob, b"Excel.Sheet.12")) + b" b\\par c\\par}")
    assert got == "a  b\n\nA1:B2\nItem|Qty\n-|-\nPen|3\n\nc\n", repr(got)


def test_rtf_object_that_is_not_read_keeps_its_result():
    d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a " + rtf_object(ole1(b"not a file", b"Package")) + b"\\par}"))
    got = in_rtf.markdown.render(d, in_rtf.tmp() / "assets", False, ctx.skipped)
    assert got == "(could not read: 1 embedded object)\n\na preview\n" and ctx.skipped == {"object": 1}, (got, ctx.skipped)
    d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a " + rtf_object(ole1(b"%PDF-1.4", b"Package"), result=b"")
                                          + b"\\par}"))
    assert ctx.skipped == {"object": 1}, ctx.skipped
    d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a {\\object\\objlink{\\result preview}}\\par}"))
    assert not ctx.skipped, ctx.skipped
    for data in (b"\x01\x05", ole1(b"")[:20], struct.pack("<II", 0x501, 1) + b"\0" * 40):
        got = in_rtf.md(b"{\\rtf1 a " + rtf_object(data) + b"\\par}")
        assert got == "(could not read: 1 embedded object)\n\na preview\n", (data, got)
    blob, _ = cfb_bytes({"\x01CompObj": comp_obj(b"MSGraph.Chart.8")})
    d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a " + rtf_object(ole1(blob)) + b"\\par}"))
    assert ctx.skipped == {"chart": 1}, ctx.skipped


def test_rtf_object_in_a_captured_destination_adds_nothing_to_it():
    obj = rtf_object(ole1(b"not a file", b"Package"))
    d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1{\\info{\\title T " + obj + b"}}body\\par}"))
    assert d.title == "T", d.title
    got = in_rtf.md(b"{\\rtf1 {\\listtext 1. " + obj + b"}item\\par}")
    assert "Equation" not in got and "0105" not in got, got


def test_rtf_objects_past_the_count_are_counted():
    old = embedded.MAX_OBJECTS
    embedded.MAX_OBJECTS = 2
    try:
        blob, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
        body = b"".join(rtf_object(ole1(blob)) for _ in range(5))
        d, ctx = in_rtf.parse(in_rtf.rtf_file(b"{\\rtf1 a " + body + b"\\par}"))
    finally:
        embedded.MAX_OBJECTS = old
    assert ctx.skipped == {"object": 3}, ctx.skipped


def test_rtf_object_data_is_bounded():
    old = limits.EMBED_BYTES
    limits.EMBED_BYTES = 64
    try:
        blob, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
        got = in_rtf.md(b"{\\rtf1 a " + rtf_object(ole1(blob)) + b"\\par}")
    finally:
        limits.EMBED_BYTES = old
    assert got == "(could not read: 1 embedded object)\n\na preview\n", repr(got)


def test_sheet_objects_follow_their_relationship_numbers():
    rows = xlsx_row(1, xlsx_cell("A1", inline="<t>host</t>"))
    data = make_xlsx(None, [{"name": "S", "rows": rows,
                             "rels": [(f"rId{n}", SHEET_REL, f"../embeddings/b{n}.xlsx", False) for n in (10, 2)]}],
                     extra_parts={"xl/embeddings/b10.xlsx": chart_package("ten"),
                                  "xl/embeddings/b2.xlsx": chart_package("two")})
    doc, ctx = host(data)
    caps = [b.caption for s in doc.blocks for b in getattr(s, "blocks", ()) if isinstance(b, Table)]
    assert [c for c in caps if c and "Chart" in c] == ["Chart: column — two", "Chart: column — ten"], caps


def vsd_object(chunks, y=1.0):
    return {"type": 0x4E, "pin": (0.0, y), "chunks": [(0x98, in_vsd.foreign_type(2), {"level": 3}),
                                                      (0x0C, in_vsd.EMF, {"level": 3}), *chunks]}


def test_vsd_ole_data_object():
    book, _ = cfb_bytes({"Package": workbook()})
    eq, _ = cfb_bytes({"Equation Native": eq_native(in_ppt.X)})
    half = len(book) // 2
    shapes = [vsd_object([(0x1F, book[:half], {"level": 4}), (0x1F, book[half:], {"level": 4})], 2.0),
              vsd_object([(0x1F, eq, {"level": 4})], 1.0)]
    doc, ctx = in_vsd.read(in_vsd.drawing([{"shapes": shapes}]))
    blocks = doc.blocks[0].blocks
    assert cells(blocks[:1]) == [["Item", "Qty"], ["Pen", "3"]] and not ctx.skipped, (blocks, ctx.skipped)
    assert isinstance(blocks[1], Equation) and blocks[1].tex == "x" and len(blocks) == 2, blocks
    doc, ctx = in_vsd.read(in_vsd.drawing([{"shapes": [vsd_object([(0x1F, b"not a file", {"level": 4})]),
                                                       vsd_object([])]}]))
    assert doc.blocks[0].blocks == [] and ctx.skipped == {"object": 2}, (doc.blocks[0].blocks, ctx.skipped)
    old = limits.EMBED_BYTES
    limits.EMBED_BYTES = 64
    try:
        doc, ctx = in_vsd.read(in_vsd.drawing([{"shapes": [vsd_object([(0x1F, eq, {"level": 4})])]}]))
    finally:
        limits.EMBED_BYTES = old
    assert doc.blocks[0].blocks == [] and ctx.skipped == {"object": 1}, (doc.blocks[0].blocks, ctx.skipped)


def test_hwp_ole_item_that_is_no_chart():
    def item(streams):
        blob, _ = cfb_bytes(streams)
        return struct.pack("<I", len(blob)) + blob

    ctx = Context()
    got = hwp_chart.ole_blocks(item({"Equation Native": eq_native(in_ppt.X)}), ctx)
    assert len(got) == 1 and isinstance(got[0], Equation) and got[0].tex == "x" and not ctx.skipped, (got, ctx.skipped)
    got = hwp_chart.ole_blocks(item({"Package": workbook()}), ctx)
    assert cells(got) == [["Item", "Qty"], ["Pen", "3"]] and not ctx.skipped, (got, ctx.skipped)
    assert hwp_chart.ole_blocks(item({"\x01CompObj": comp_obj(b"MSGraph.Chart.8")}), ctx) == []
    assert hwp_chart.ole_blocks(item({"\x01Ole10Native": b"x" * 40}), ctx) == []
    assert ctx.skipped == {"chart": 1, "object": 1}, ctx.skipped


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
