"""Tests for embedded Word and PowerPoint documents: an object that holds one is read whole and quoted where it stands.

Run: python -I -X utf8 tests/test_reader_embedded_docs.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import embedded, limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import hwp_chart, ooxml, sheet_grid  # noqa: E402
from reader.model import Context, Para, Quote, Text  # noqa: E402
from reader_fix import cfb_bytes, make_doc, make_ppt, make_xlsx, xlsx_cell, xlsx_row  # noqa: E402
import test_reader_doc_objects as in_doc  # noqa: E402
import test_reader_docm as in_docm  # noqa: E402
import test_reader_embedded as emb  # noqa: E402
import test_reader_odt as odt  # noqa: E402
import test_reader_ppt_objects as in_ppt  # noqa: E402
import test_reader_rtf as in_rtf  # noqa: E402
import test_reader_slides as in_slides  # noqa: E402
import test_reader_vsd as in_vsd  # noqa: E402

CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"


def tmp():
    return Path(tempfile.mkdtemp())


def word(body=None, parts=None, rels=()):
    return Path(in_docm.docm(body or in_docm.P("INNER WORD"), parts=parts, rels=rels)).read_bytes()


def deck():
    return Path(in_slides.deck([in_slides.sp("Plan", ph="title") + in_slides.sp("INNER SLIDE", id=3)])).read_bytes()


def old_word(text="INNER DOC"):
    p = tmp() / "i.doc"
    make_doc(p, [(text + "\r", False)])
    return p.read_bytes()


def old_deck():
    p = tmp() / "i.ppt"
    make_ppt(p, [{"title": "Old plan", "body": ["INNER PPT"]}])
    return p.read_bytes()


def streams(blob):
    with Compound(blob) as cf:
        return {n: cf.read(n) for n in cf.names()}


WORD_MD = "> Embedded Word document\n>\n> INNER WORD"
DECK_MD = "> Embedded PowerPoint presentation\n>\n> ## Slide 1: Plan\n>\n> INNER SLIDE"
DOC_MD = "> Embedded Word document\n>\n> INNER DOC"


def lost(doc, ctx):
    return markdown.render(doc, tmp() / "assets", False, ctx.skipped)


def test_read_gives_the_document_quoted():
    got, ctx = emb.read(word())
    assert got[0] == "document" and len(got[1]) == 1 and isinstance(got[1][0], Quote), got
    head = got[1][0].blocks[0]
    assert isinstance(head, Para) and head.inlines == [Text("Embedded Word document")], head
    assert not ctx.skipped and not ctx.warnings, (ctx.skipped, ctx.warnings)
    for blob in (deck(), old_word(), old_deck()):
        got, ctx = emb.read(blob)
        assert got[0] == "document" and not ctx.skipped, (got, ctx.skipped)


def test_word_and_powerpoint_inside_a_docx():
    for target, blob, rel, want in (("w.docx", word(), emb.PKG_REL, WORD_MD), ("p.pptx", deck(), emb.PKG_REL, DECK_MD),
                                    ("d.bin", old_word(), emb.OLE_REL, DOC_MD),
                                    ("o.bin", old_deck(), emb.OLE_REL, "> Embedded PowerPoint presentation\n>\n> "
                                                                      "## Slide 1: Old plan\n>\n> INNER PPT")):
        p = emb.docx([("rO", rel, "embeddings/" + target, False)], {"word/embeddings/" + target: blob})
        doc, ctx = in_docm.parse(p)
        out = lost(doc, ctx)
        assert out == "beforeafter\n\n" + want + "\n" and not ctx.skipped, (target, out, ctx.skipped)


def test_title_properties_and_headers_of_an_embedded_document():
    core = ('<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Budget memo</dc:title>'
            '<dc:creator>Kim</dc:creator></cp:coreProperties>')
    head = f'<w:hdr xmlns:w="{in_docm.W}"><w:p><w:r><w:t>Internal only</w:t></w:r></w:p></w:hdr>'
    sect = '<w:sectPr><w:headerReference w:type="default" r:id="rH"/></w:sectPr>'
    blob = word(in_docm.P("INNER WORD") + sect, parts={"word/header1.xml": head},
                rels=[("rH", "header", "header1.xml", False)])
    import zipfile
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as z, zipfile.ZipFile(buf, "w") as out:
        for item in z.infolist():
            data = z.read(item)
            if item.filename == "_rels/.rels":
                data = data.replace(b"</Relationships>", f'<Relationship Id="rC" Type="{CORE}" '
                                    f'Target="docProps/core.xml"/></Relationships>'.encode())
            out.writestr(item, data)
        out.writestr("docProps/core.xml", core)
    p = emb.docx([("rO", emb.PKG_REL, "embeddings/w.docx", False)], {"word/embeddings/w.docx": buf.getvalue()})
    out = in_docm.md(p)
    want = ("> Embedded Word document: Budget memo\n>\n> Properties: author: Kim\n>\n> Header: Internal only\n>\n"
            "> INNER WORD")
    assert out == "beforeafter\n\n" + want + "\n", out


def test_documents_on_a_slide_in_a_sheet_and_in_rtf():
    p = in_slides.deck([in_slides.sp("Title", ph="title") + emb.ole_frame("rO", "rP")],
                       srels={1: [("rO", emb.PKG_REL, "../embeddings/w.docx"), ("rP", "image", "../media/p.png")]},
                       extra={"ppt/embeddings/w.docx": word(), "ppt/media/p.png": in_docm.PNG})
    doc, ctx = in_slides.parse(p)
    assert lost(doc, ctx) == "## Slide 1: Title\n\n" + WORD_MD + "\n" and not ctx.skipped, (lost(doc, ctx), ctx.skipped)
    rows = xlsx_row(1, xlsx_cell("A1", inline="<t>host</t>"), xlsx_cell("B1", "1"))
    after = '<oleObjects><oleObject progId="Word.Document.12" shapeId="1025" r:id="rO"/></oleObjects>'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
    data = make_xlsx(None, [{"name": "S", "rows": rows, "after": after, "rels": [("rO", rel, "../embeddings/w.docx", False)]}],
                     extra_parts={"xl/embeddings/w.docx": word()})
    ctx = Context()
    out = lost(ooxml.parse_package(data, ctx), ctx)
    assert out == "## Sheet: S\n\nA1:B1\nhost|1\n-|-\n\n" + WORD_MD + "\n" and not ctx.skipped, (out, ctx.skipped)
    got = in_rtf.md(b"{\\rtf1 a " + emb.rtf_object(emb.ole1(old_word(), b"Word.Document.8"),
                                                   lead=b"{\\*\\objclass Word.Document.8}") + b" b\\par}")
    assert got == "a  b\n\n" + DOC_MD + "\n", repr(got)


def test_documents_in_binary_hosts():
    p = in_doc.embed_doc({1: streams(old_word())}, preview=in_doc.PNG_PREVIEW)
    assert in_doc.md(p) == DOC_MD + "\n", in_doc.md(p)
    p = in_doc.embed_doc({1: {"Package": word()}}, preview=in_doc.PNG_PREVIEW)
    assert in_doc.md(p) == WORD_MD + "\n", in_doc.md(p)
    stg, _ = cfb_bytes({"Package": deck()})
    slide = {"shapes": [{"text": "above", "box": (0, 10, 400, 50)}, in_ppt.obj(pib=1)]}
    out, ctx, assets = in_ppt.build([slide], objects=[stg], pictures=[in_ppt.blip_png(in_ppt.PNG)])
    assert DECK_MD in out and not assets and not ctx.skipped, (out, assets, ctx.skipped)
    out, ctx, assets = in_ppt.build([slide], objects=[old_word()], pictures=[in_ppt.blip_png(in_ppt.PNG)])
    assert DOC_MD in out and not assets and not ctx.skipped, (out, assets, ctx.skipped)
    doc, ctx = in_vsd.read(in_vsd.drawing([{"shapes": [emb.vsd_object([(0x1F, old_word(), {"level": 4})])]}]))
    quote = doc.blocks[0].blocks
    assert len(quote) == 1 and isinstance(quote[0], Quote) and not ctx.skipped, (quote, ctx.skipped)
    ctx = Context()
    blob = old_word()
    got = hwp_chart.ole_blocks(struct.pack("<I", len(blob)) + blob, ctx)
    assert len(got) == 1 and isinstance(got[0], Quote) and not ctx.skipped, (got, ctx.skipped)


def test_opendocument_text_and_presentation_packages():
    p = emb.docx([("rO", emb.PKG_REL, "embeddings/t.odt", False)],
                 {"word/embeddings/t.odt": Path(odt.odt_doc(odt.P("INNER ODT"))).read_bytes()})
    assert in_docm.md(p) == "beforeafter\n\n> Embedded OpenDocument text\n>\n> INNER ODT\n", in_docm.md(p)
    import test_reader_ods_odp as odp
    blob = Path(odp.odp(odp.page("p1", odp.frame("Agenda", cls="title"), odp.frame("INNER ODP")))).read_bytes()
    got, ctx = emb.read(blob)
    assert got[0] == "document" and got[1][0].blocks[0].inlines == [Text("Embedded OpenDocument presentation")], got
    assert not ctx.skipped, ctx.skipped


def test_documents_in_an_opendocument_host():
    sub = (f'<office:document-content {odt.NS}><office:body><office:text>{odt.P("INNER SUB")}'
           '</office:text></office:body></office:document-content>')
    stg, _ = cfb_bytes({"Package": word()})
    body = (odt.P("host") + odt.frame('<draw:object xlink:href="./Object 1"/>')
            + odt.frame('<draw:object-ole xlink:href="./Object 2"/>')
            + odt.frame('<draw:object-ole xlink:href="./Object 3"/>'))
    p = odt.odt_doc(body, extra={"Object 1/content.xml": sub, "Object 2": stg, "Object 3": old_word()})
    doc, ctx = odt.parse(p)
    out = lost(doc, ctx)
    want = "host\n\n> Embedded OpenDocument text\n>\n> INNER SUB\n\n" + WORD_MD + "\n\n" + DOC_MD + "\n"
    assert out == want and not ctx.skipped, (out, ctx.skipped)
    blob, _ = cfb_bytes({"CONTENTS": b"%PDF-1.4"})
    p = odt.odt_doc(odt.frame('<draw:object-ole xlink:href="./Object 1"/>'), extra={"Object 1": blob})
    doc, ctx = odt.parse(p)
    assert ctx.skipped == {"object": 1}, ctx.skipped
    p = odt.odt_doc(odt.frame('<draw:object-ole xlink:href="./Object 1"/>'), extra={"Object 1": b""})
    doc, ctx = odt.parse(p)
    assert ctx.skipped == {"object": 1}, ctx.skipped
    eq, _ = cfb_bytes({"Equation Native": emb.eq_native(in_ppt.X)})
    p = odt.odt_doc(odt.P("a") + odt.frame('<draw:object-ole xlink:href="./Object 1"/>'), extra={"Object 1": eq})
    doc, ctx = odt.parse(p)
    assert lost(doc, ctx) == "a\n\n$$x$$\n" and not ctx.skipped, (lost(doc, ctx), ctx.skipped)
    book, _ = cfb_bytes({"Package": emb.workbook()})
    p = odt.odt_doc(odt.frame('<draw:object-ole xlink:href="./Object 1"/>'), extra={"Object 1": book})
    doc, ctx = odt.parse(p)
    out = lost(doc, ctx)
    assert out == "Embedded sheet Data: A1:B2\nItem|Qty\n-|-\nPen|3\n" and not ctx.skipped, (out, ctx.skipped)


def test_notes_of_an_embedded_document_reach_the_host():
    notes = (f'<w:footnotes xmlns:w="{in_docm.W}"><w:footnote w:id="2"><w:p><w:r><w:t>%s</w:t></w:r></w:p>'
             '</w:footnote></w:footnotes>')
    ref = '<w:p><w:r><w:t>%s</w:t></w:r><w:r><w:footnoteReference w:id="2"/></w:r></w:p>'
    inner = word(ref % "inner text", parts={"word/footnotes.xml": notes % "INNER NOTE"},
                 rels=[("rF", "footnotes", "footnotes.xml", False)])
    src = in_docm.docm(ref % "host text" + emb.word_object("rO") + emb.word_object("rQ"),
                       parts={"word/media/p.png": in_docm.PNG, "word/embeddings/a.docx": inner,
                              "word/embeddings/b.docx": inner, "word/footnotes.xml": notes % "HOST NOTE"},
                       rels=[("rP", "image", "media/p.png", False), ("rO", emb.PKG_REL, "embeddings/a.docx", False),
                             ("rQ", emb.PKG_REL, "embeddings/b.docx", False), ("rF", "footnotes", "footnotes.xml", False)])
    out = in_docm.md(src)
    assert out.count("INNER NOTE") == 2 and out.count("HOST NOTE") == 1, out
    marks = [m for m in ("[^1]", "[^2]", "[^3]") if out.count(m) == 2]
    assert len(marks) == 3, out


def test_notes_of_a_workbook_inside_an_embedded_document_reach_the_host():
    import test_reader_xlsx as in_xlsx
    rows = xlsx_row(1, xlsx_cell("A1", inline="<t>Item</t>"), xlsx_cell("B1", "3"))
    book = make_xlsx(None, [{"name": "Data", "rows": rows, "rels": [("rC", in_xlsx.LEGACY, "../comments1.xml", False)]}],
                     extra_parts={in_xlsx.LEG_PART: in_xlsx.legacy_xml(("B1", 0, "CELL COMMENT"))})
    inner = word(emb.word_object("rO"), parts={"word/embeddings/b.xlsx": book, "word/media/p.png": in_docm.PNG},
                 rels=[("rO", emb.PKG_REL, "embeddings/b.xlsx", False), ("rP", "image", "media/p.png", False)])
    p = emb.docx([("rO", emb.PKG_REL, "embeddings/w.docx", False)], {"word/embeddings/w.docx": inner})
    out = in_docm.md(p)
    assert out.count("CELL COMMENT") == 1 and out.count("[^1]") == 2, out


def test_an_object_of_a_doc_inside_a_doc_is_counted_not_failed():
    inner = in_doc.embed_doc({1: {"Equation Native": b"bad"}}, text="x" + in_doc.EMBED + "\r", preview=in_doc.PNG_PREVIEW)
    d, ctx = in_doc.parse(in_doc.embed_doc({1: streams(Path(inner).read_bytes())}, preview=in_doc.PNG_PREVIEW))
    assert ctx.skipped == {"object": 1} and not ctx.warnings, (ctx.skipped, ctx.warnings)
    assert isinstance(d.blocks[0], Quote), d.blocks


def test_what_an_embedded_document_lost_is_the_hosts():
    body = ('<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rX"/>'
            '</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    inner = word(in_docm.P("INNER WORD") + body, rels=[("rX", "image", "../../evil.png", False)])
    p = emb.docx([("rO", emb.PKG_REL, "embeddings/w.docx", False)], {"word/embeddings/w.docx": inner})
    doc, ctx = in_docm.parse(p)
    assert ctx.skipped == {"image": 1}, ctx.skipped
    assert lost(doc, ctx).startswith("(could not read: 1 image)"), lost(doc, ctx)


def test_an_embedded_document_stops_at_the_objects_time():
    inner = word("".join(in_docm.P(f"LINE {i}") for i in range(400)))
    saved, clock = limits.EMBED_SECONDS, embedded.clock
    ticks = iter(range(10 ** 6))
    embedded.clock = lambda: next(ticks) * 1e-3
    limits.EMBED_SECONDS = 0.0025
    try:
        p = emb.docx([("rO", emb.PKG_REL, "embeddings/w.docx", False)], {"word/embeddings/w.docx": inner})
        doc, ctx = in_docm.parse(p)
    finally:
        limits.EMBED_SECONDS, embedded.clock = saved, clock
    out = lost(doc, ctx)
    assert ctx.skipped == {"object_cut": 1}, ctx.skipped
    assert "Embedded Word document" in out and out.count("LINE ") < 400, out[:300]
    assert out.startswith("(could not read: 1 embedded object cut short)"), out[:200]


def test_a_document_is_never_charged_as_a_chart():
    saved = limits.CHART_TOTAL
    limits.CHART_TOTAL = -1
    try:
        p = emb.docx([("rO", emb.PKG_REL, "embeddings/w.docx", False)], {"word/embeddings/w.docx": word()})
        doc, ctx = in_docm.parse(p)
        assert lost(doc, ctx) == "beforeafter\n\n" + WORD_MD + "\n" and not ctx.skipped, (lost(doc, ctx), ctx.skipped)
        slide = {"shapes": [{"text": "above", "box": (0, 10, 400, 50)}, in_ppt.obj(pib=1)]}
        out, ctx, _ = in_ppt.build([slide], objects=[old_word()], pictures=[in_ppt.blip_png(in_ppt.PNG)])
        assert DOC_MD in out and not ctx.skipped, (out, ctx.skipped)
    finally:
        limits.CHART_TOTAL = saved


def test_an_embedded_document_read_past_the_hosts_stop_says_time_once():
    ctx = Context()
    ctx.stop = ctx.clock() - 1
    got = embedded.read(word(), ctx, sheet_grid.Budget())
    assert got[0] == "document" and ctx.skipped == {"time": 1}, (got, ctx.skipped)


def test_warnings_of_an_embedded_document_are_the_hosts():
    cell = "<w:p/>"
    for _ in range(4):
        cell = f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl><w:p/>"
    path = in_docm.docm(in_docm.P("INNER WORD") + cell)
    saved = limits.MAX_DEPTH
    limits.MAX_DEPTH = 2
    try:
        _, plain = in_docm.parse(path)
        got, ctx = emb.read(Path(path).read_bytes())
        embedded.read(Path(path).read_bytes(), ctx, sheet_grid.Budget())
        plain.skipped.update(plain.skipped)
    finally:
        limits.MAX_DEPTH = saved
    assert plain.warnings and ctx.warnings == plain.warnings, (plain.warnings, ctx.warnings)
    assert ctx.skipped == plain.skipped, (ctx.skipped, plain.skipped)


def test_page_numbers_and_repeats_leave_the_header_lines():
    hd = '<w:hdr {NS}>' + in_docm.P("Internal only") + '</w:hdr>'
    ft = '<w:ftr {NS}>' + in_docm.P("- 3 -") + '</w:ftr>'
    got, _ = emb.read(word(parts={"word/header1.xml": hd, "word/header2.xml": hd, "word/footer1.xml": ft},
                           rels=[("rH", "header", "header1.xml", False), ("rJ", "header", "header2.xml", False),
                                 ("rF", "footer", "footer1.xml", False)]))
    lines = [b.inlines[0].text for b in got[1][0].blocks[:3]]
    assert lines == ["Embedded Word document", "Header: Internal only", "INNER WORD"], lines


def test_a_document_in_hidden_doc_text_is_hidden():
    p = in_doc.embed_doc({1: streams(old_word())}, text="a" + in_doc.EMBED + "\r", preview=in_doc.PNG_PREVIEW,
                         props=in_doc.sp(in_doc.VANISH))
    assert in_doc.md(p) == "(1 hidden places left out)\n\na\n", in_doc.md(p)
    assert "(hidden) > Embedded Word document" in in_doc.md(p, hidden=True), in_doc.md(p, hidden=True)


def test_documents_inside_documents_stop_at_depth():
    blob = word()
    for _ in range(embedded.DEPTH + 2):
        blob = word(emb.word_object("rO"), parts={"word/embeddings/w.docx": blob, "word/media/p.png": in_docm.PNG},
                    rels=[("rO", emb.PKG_REL, "embeddings/w.docx", False), ("rP", "image", "media/p.png", False)])
    got, ctx = emb.read(blob)
    flat = []
    stack = [got[1]]
    while stack:
        for b in stack.pop():
            if isinstance(b, Quote):
                flat.append(b)
                stack.append(b.blocks)
    assert len(flat) == embedded.DEPTH + 1, len(flat)
    assert ctx.skipped == {"object": 1}, ctx.skipped


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
