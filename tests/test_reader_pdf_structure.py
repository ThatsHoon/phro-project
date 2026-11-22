"""Tagged pdf (ISO 32000-1 14.7-14.8): headings, lists, table cells, figure /Alt and /ActualText
come from the structure tree through the marked-content ids, /Artifact content is left out, and a
file that is not tagged reads by its layout. Both engines: native here, pdfium through
tests/pdfium_run.py when the pdf venv exists.

Run: python -I -X utf8 tests/test_reader_pdf_structure.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import TAGGED_MD, Ref, form_xobject, simple_pdf, struct_tree, tagged_pdf, text_ops  # noqa: E402
from test_reader_pdf import md  # noqa: E402
import test_reader_pdfium  # noqa: E402

from reader.pdfnative import tagged  # noqa: E402
from reader.pdfnative.structure import PDFFile  # noqa: E402


def lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not set(ln) <= set("|-: ")]


def test_tree():
    tags = tagged.read(PDFFile(tagged_pdf()))
    page = tags.page(0)
    assert sorted(page) == list(range(13)) and [page[m][0] for m in range(13)] == list(range(13)), page
    kinds = [tags.elements[page[m][1]].kind for m in range(13)]
    assert kinds == ["H1", "P", "Lbl", "LBody", "Lbl", "LBody", "TH", "TH", "TD", "TD", "Figure", "P", "P"], kinds
    assert tags.elements[tags.elements[page[0][1]].parent].kind == "Document"
    assert tags.elements[tags.block(page[3][1])].kind == "LBody"
    table = tags.nearest(page[6][1], ("Table",))
    assert tags.rows(table, [page[9][1], page[6][1]]) == [[page[6][1], page[7][1]], [page[8][1], page[9][1]]]
    assert tags.rows(table, [page[8][1]]) == [[page[8][1], page[9][1]]]
    assert tags.elements[page[10][1]].alt == "A bar chart of scores"
    assert tags.elements[page[12][1]].actual == "x minus y"
    assert tagged.read(PDFFile(tagged_pdf(marked=False))) is None


def test_tree_order_wins_over_drawing_order():
    # marked content drawn in another order than the order of the tree ranks as the tree has it
    tree = struct_tree({601: ("Document", 600, [Ref(602), Ref(603)]), 602: ("P", 601, 1), 603: ("P", 601, 0)},
                       [603, 602])
    tags = tagged.read(PDFFile(simple_pdf([b""], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                          page_extra={"StructParents": 0}, extra_objects=tree)))
    assert {m: rank for m, (rank, _el) in tags.page(0).items()} == {1: 0, 0: 1}


def test_tagged_page():
    text, _ctx, _doc = md(tagged_pdf())
    assert lines(text) == TAGGED_MD, text


def test_ruled_table_on_tagged_page():
    # a grid of three columns drawn round the tagged table of two finds no table in its cells; without
    # tags the grid makes the table
    text, _ctx, _doc = md(tagged_pdf(ruled=True))
    assert lines(text) == TAGGED_MD, text
    plain, _ctx, _doc = md(tagged_pdf(marked=False, ruled=True))
    assert "|Name||Score|" in plain, plain


def test_tagged_page_pdfium():
    if test_reader_pdfium.PY is None:
        return
    got = test_reader_pdfium.run(tagged_pdf())
    assert lines(got["md"]) == TAGGED_MD and got["meta"]["engine"] == "pdfium", got["md"]


def test_untagged_file_reads_by_layout():
    text, _ctx, _doc = md(tagged_pdf(marked=False))
    assert "Running head of the report" in text and "|Name|Score|" not in text, text
    assert "1. First item" not in lines(text) and "A bar chart of scores" not in text, text


def test_page_the_tree_does_not_cover():
    # a page whose text is mostly unmarked reads by its layout: its artifact stays
    ops = (b"/Artifact BMC " + text_ops("Running head", y=760) + b" EMC "
           + b"/P << /MCID 1 >> BDC " + text_ops("Only this", y=694) + b" EMC "
           + b" ".join(text_ops("Plain unmarked line number %d" % k, y=680 - 14 * k) for k in range(6)))
    tree = struct_tree({601: ("P", 600, 1)}, [None, 601])
    text, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                     page_extra={"StructParents": 0}, extra_objects=tree))
    assert "Running head" in text and "Plain unmarked line number 5" in text, text


def _artifact_pages(pages):
    tree = struct_tree({601: ("P", 600, 0)}, [601])
    tree[699] = {"Nums": [k for n in range(len(pages)) for k in (n, [Ref(601)])]}
    return simple_pdf(pages, catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                      page_extra=[{"StructParents": n} for n in range(len(pages))], extra_objects=tree)


def test_artifact_text_is_said_once_and_hidden_text_is_counted():
    def page(n):
        return (b"/Artifact BMC " + text_ops("Confidential - plan", y=770) + b" " + text_ops("Page %d" % n, y=30)
                + b" " + text_ops("Regional office", y=44) + b" " + text_ops("Userid: CPM", x=-500, y=400) + b" EMC "
                + b"/P << /MCID 0 >> BDC " + text_ops("Body of page %d that is the text of it." % n, y=600) + b" EMC")
    text, ctx, _doc = md(_artifact_pages([page(1), page(2)]))
    assert text.count("Header: Confidential - plan") == 1 and text.count("Footer: Regional office") == 1, text
    assert text.count("Confidential") == 1 and "Page 1" not in lines(text) and "Userid" not in text, text
    assert "(2 hidden places left out)" in text, text
    text, ctx, _doc = md(_artifact_pages([page(1), page(2)]), hidden=True)
    assert text.count("Userid: CPM") == 2, text


def test_page_whose_text_is_mostly_artifact_reads_by_layout():
    ops = (b"/H1 << /MCID 0 >> BDC " + text_ops("Title", y=740) + b" EMC /Artifact BMC "
           + b" ".join(text_ops("Body line number %d of the page" % k, y=700 - 14 * k) for k in range(20)) + b" EMC")
    text, _ctx, _doc = md(_artifact_pages([ops]))
    assert "Body line number 19 of the page" in text and "Header:" not in text, text


def test_unmarked_lines_keep_their_place():
    # on a tagged page, lines in no marked content go among the blocks by their top, in their own order
    text = ["First tagged paragraph of the page body", "Second tagged paragraph of the page body",
            "Third tagged paragraph of the page body"]
    ops = b" ".join(b"/P << /MCID %d >> BDC " % k + text_ops(t, y=700 - 60 * k) + b" EMC" for k, t in enumerate(text))
    ops += b" " + text_ops("loose one", y=670) + b" " + text_ops("loose two", y=650)
    tree = struct_tree({601: ("Document", 600, [Ref(602), Ref(603), Ref(604)]),
                        602: ("P", 601, 0), 603: ("P", 601, 1), 604: ("P", 601, 2)}, [602, 603, 604])
    got, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                    page_extra={"StructParents": 0}, extra_objects=tree))
    assert lines(got)[1:] == [text[0], "loose one", "loose two", text[1], text[2]], got


def test_table_over_pages():
    # one Table element over two pages, its rows written with single-kid /K refs: each page shows
    # its own rows
    def row(n, cells):
        return {n: ("TR", 610, [Ref(c) for c in cells] if len(cells) > 1 else Ref(cells[0]))}
    elements = {601: ("Document", 600, [Ref(610)]), 610: ("Table", 601, [Ref(611), Ref(612), Ref(613)])}
    elements.update(row(611, [621, 622]))
    elements.update(row(612, [623]))
    elements.update(row(613, [624, 625]))
    for n, mcid in zip(range(621, 626), range(5)):
        elements[n] = ("TD", 611 if n < 623 else 612 if n == 623 else 613, mcid)
    tree = struct_tree(elements, [621, 622, 623])
    tree[699] = {"Nums": [0, [Ref(621), Ref(622), Ref(623)], 1, [None, None, None, Ref(624), Ref(625)]]}
    cell = lambda mcid, t, x, y: b"/TD << /MCID %d >> BDC " % mcid + text_ops(t, x=x, y=y) + b" EMC"
    first = b" ".join([cell(0, "Name", 72, 700), cell(1, "Score", 200, 700), cell(2, "Kim 90", 72, 686)])
    second = b" ".join([cell(3, "Lee", 72, 700), cell(4, "85", 200, 700)])
    data = simple_pdf([first, second], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                      page_extra=[{"StructParents": 0}, {"StructParents": 1}], extra_objects=tree)
    text, _ctx, _doc = md(data)
    assert lines(text) == ["## Page 1", "|Name|Score|", "|Kim 90||", "## Page 2", "|Lee|85|"], text


def test_paragraph_set_as_heading():
    # a tree that tags its headings as P: a P whose lines the layout reads as a heading is one
    big = b"BT /F1 20 Tf 72 720 Td (Payroll tables) Tj ET"
    body = [text_ops("Body line number %d of the section text" % k, y=690 - 14 * k) for k in range(4)]
    ops = b"/P << /MCID 0 >> BDC " + big + b" EMC " + b"/P << /MCID 1 >> BDC " + b" ".join(body) + b" EMC"
    tree = struct_tree({601: ("Document", 600, [Ref(602), Ref(603)]), 602: ("P", 601, 0), 603: ("P", 601, 1)},
                       [602, 603])
    text, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                     page_extra={"StructParents": 0}, extra_objects=tree))
    got = lines(text)
    assert got[1].startswith("#") and got[1].endswith(" Payroll tables"), got
    assert got[2].startswith("Body line number 0"), got


def test_block_lines_read_top_down():
    # a block whose lines are drawn from the bottom up (chart labels) reads them top down
    labels = [text_ops("Label %d" % k, x=100 + 30 * k, y=300 + 40 * k) for k in range(5)]
    ops = b"/P << /MCID 0 >> BDC " + b" ".join(labels) + b" EMC"
    tree = struct_tree({601: ("Document", 600, [Ref(602)]), 602: ("P", 601, 0)}, [602])
    text, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                     page_extra={"StructParents": 0}, extra_objects=tree))
    at = [text.index("Label %d" % k) for k in range(5)]
    assert at == sorted(at, reverse=True), text


def test_title_is_a_heading():
    # PDF 2.0's Title element heads the document as an H1 does
    ops = (b"/Title << /MCID 0 >> BDC " + text_ops("Form example", y=730) + b" EMC "
           + b"/H1 << /MCID 1 >> BDC " + text_ops("First part", y=700) + b" EMC "
           + b"/P << /MCID 2 >> BDC " + text_ops("Body text of the first part.", y=680) + b" EMC")
    tree = struct_tree({601: ("Document", 600, [Ref(602), Ref(603), Ref(604)]), 602: ("Title", 601, 0),
                        603: ("H1", 601, 1), 604: ("P", 601, 2)}, [602, 603, 604])
    text, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                     page_extra={"StructParents": 0}, extra_objects=tree))
    got = lines(text)
    assert got[1:4] == ["### Form example", "### First part", "Body text of the first part."], got


def test_unmarked_line_beside_a_block():
    # a line in no marked content whose top lies among a block's lines goes after the block, which stays whole
    body = ["First line of the tagged paragraph that runs", "on over a second line of the same", "paragraph and ends on its third."]
    ops = (b"/P << /MCID 0 >> BDC " + b" ".join(text_ops(t, y=700 - 14 * k) for k, t in enumerate(body)) + b" EMC "
           + text_ops("NOTE", x=480, y=690))
    tree = struct_tree({601: ("Document", 600, [Ref(602)]), 602: ("P", 601, 0)}, [602])
    got, _ctx, _doc = md(simple_pdf([ops], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                    page_extra={"StructParents": 0}, extra_objects=tree))
    assert lines(got)[1:] == [" ".join(body), "NOTE"], got


def _form_marked():
    text = ["Tagged paragraph drawn on the page first", "Tagged paragraph drawn inside a form",
            "Tagged paragraph drawn on the page last"]
    ops = (b"/P << /MCID 0 >> BDC " + text_ops(text[0], y=640) + b" EMC "
           + b"/P << /MCID 2 >> BDC " + text_ops(text[2], y=580) + b" EMC "
           + b"/P << /MCID 1 >> BDC /Fm0 Do EMC")
    tree = struct_tree({601: ("Document", 600, [Ref(602), Ref(603), Ref(604)]),
                        602: ("P", 601, 0), 603: ("P", 601, 1), 604: ("P", 601, 2)}, [602, 603, 604])
    tree[20] = form_xobject(text_ops(text[1], y=700))
    data = simple_pdf([ops], resources={"Font": {"F1": Ref(3)}, "XObject": {"Fm0": Ref(20)}},
                      catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                      page_extra={"StructParents": 0}, extra_objects=tree)
    return data, text


def test_form_drawn_in_marked_content():
    # text a form draws inside marked content is that content's: it reads in the tree's order, not by its top
    data, text = _form_marked()
    got, _ctx, _doc = md(data)
    assert lines(got)[1:] == text, got


def test_form_drawn_in_marked_content_pdfium():
    if test_reader_pdfium.PY is None:
        return
    data, text = _form_marked()
    got = test_reader_pdfium.run(data)
    assert lines(got["md"])[1:] == text and got["meta"]["engine"] == "pdfium", got["md"]


def test_tree_walk_bounded():
    # a parent chain deeper than STRUCT_DEPTH, and one that loops, own nothing
    saved = tagged.STRUCT_DEPTH
    tagged.STRUCT_DEPTH = 5
    try:
        chain = {n: ("Div", n - 1, [Ref(n + 1)]) for n in range(601, 621)}
        chain[621] = ("P", 620, 0)
        chain[630] = ("P", 631, 1)
        chain[631] = ("Div", 630, [Ref(630)])
        tree = struct_tree(chain, [621, 630])
        tags = tagged.read(PDFFile(simple_pdf([b""], catalog={"StructTreeRoot": Ref(600),
                                                             "MarkInfo": {"Marked": True}},
                                              page_extra={"StructParents": 0}, extra_objects=tree)))
        assert tags.page(0) == {} and len(tags.elements) == 0, (tags.page(0), len(tags.elements))
    finally:
        tagged.STRUCT_DEPTH = saved


def test_parent_tree_read_once_and_bounded():
    import time
    tree = struct_tree({601: ("P", 600, 0)}, [601])
    tree[699] = {"Kids": [Ref(698)]}
    tree[698] = {"Kids": [Ref(697), Ref(696)]}
    tree[697] = {"Limits": [0, 0], "Nums": [0, [Ref(601)]]}
    tree[696] = {"Limits": [1, 1], "Nums": [1, [Ref(601)]]}
    extra = {"StructParents": 1}
    tags = tagged.read(PDFFile(simple_pdf([b""], catalog={"StructTreeRoot": Ref(600), "MarkInfo": {"Marked": True}},
                                          page_extra=extra, extra_objects=tree)))
    assert list(tags.page(0)) == [0], tags.page(0)
    tree[699] = {"Kids": [Ref(699)] * 3000 + [Ref(697)]}
    tags = tagged.read(PDFFile(simple_pdf([b""] * 3, catalog={"StructTreeRoot": Ref(600),
                                                             "MarkInfo": {"Marked": True}},
                                          page_extra={"StructParents": 0}, extra_objects=tree)))
    t = time.perf_counter()
    got = [list(tags.page(i)) for i in range(3)]
    assert time.perf_counter() - t < 1 and got == [[0]] * 3, (time.perf_counter() - t, got)
    saved = tagged.TREE_ITEMS
    tagged.TREE_ITEMS = 2000
    try:
        tags = tagged.read(PDFFile(simple_pdf([b""], catalog={"StructTreeRoot": Ref(600),
                                                             "MarkInfo": {"Marked": True}},
                                              page_extra={"StructParents": 0}, extra_objects=tree)))
        assert tags.page(0) == {}, tags.page(0)
    finally:
        tagged.TREE_ITEMS = saved


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
