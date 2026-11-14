"""RTF tables, lists and pictures.

Run: python -I -X utf8 tests/test_reader_rtf_tables.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.model import Image, ListItem, Table  # noqa: E402
from test_reader_rtf import md, parse, rtf_file, tmp  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + bytes(28)
# a 1x1 24-bit DIB: BITMAPINFOHEADER, then one row of one red pixel padded to 4 bytes
import struct  # noqa: E402
DIB = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"


def rows(*cells_edges, flags=None, hdr=False):
    """One row: cells_edges are (text, edge) pairs; flags maps a cell index to its merge words."""
    flags = flags or {}
    head = b"\\trowd" + (b"\\trhdr" if hdr else b"")
    for i, (_, edge) in enumerate(cells_edges):
        head += flags.get(i, b"") + b"\\cellx%d" % edge
    body = b"\\pard\\intbl " + b"".join(t + b"\\cell " for t, _ in cells_edges) + b"\\row\n"
    return head + body


def doc_md(data, **kw):
    d, ctx = parse(rtf_file(data))
    return markdown.render(d, tmp() / "assets", False, ctx.skipped), d, ctx


def only_table(d):
    tables = [b for b in d.blocks if isinstance(b, Table)]
    assert len(tables) == 1, d.blocks
    return tables[0]


def hexed(data, line=64):
    h = data.hex().encode()
    return b"\n".join(h[i:i + line] for i in range(0, len(h), line))


# ---- the brief's tests
def test_simple_table():
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + rows((b"C", 1000), (b"D", 2000)) + b"\\pard after\\par}"
    out = md(data)
    assert out == "|A|B|\n|---|---|\n|C|D|\n\nafter\n", out


def test_horizontal_merge():
    data = (b"{\\rtf1 " + rows((b"AB", 1000), (b"", 2000), (b"C", 3000), flags={0: b"\\clmgf", 1: b"\\clmrg"})
            + rows((b"x", 1000), (b"y", 2000), (b"z", 3000)) + b"}")
    out = md(data)
    assert '<td colspan="2">AB</td><td>C</td>' in out, out
    assert "<td>x</td><td>y</td><td>z</td>" in out, out


def test_vertical_merge():
    data = (b"{\\rtf1 " + rows((b"top", 1000), (b"R1", 2000), flags={0: b"\\clvmgf"})
            + rows((b"", 1000), (b"R2", 2000), flags={0: b"\\clvmrg"}) + b"}")
    out = md(data)
    assert '<td rowspan="2">top</td>' in out, out
    assert "<td>R2</td>" in out, out


def test_uneven_edges():
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + rows((b"C", 1500), (b"D", 2000)) + b"}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [[c.colspan for c in r] for r in t.rows] == [[1, 2], [2, 1]], t.rows


def test_nested_table():
    inner = (b"\\pard\\intbl\\itap2 in1\\nestcell\\pard\\intbl\\itap2 in2\\nestcell"
             b"{\\*\\nesttableprops\\trowd\\cellx500\\cellx1000\\nestrow}")
    data = (b"{\\rtf1 \\trowd\\cellx1000\\cellx2000\\pard\\intbl\\itap1 out\\par" + inner
            + b"\\pard\\intbl\\itap1 tail\\cell right\\cell\\row}")
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert len(t.rows) == 1 and len(t.rows[0]) == 2, t.rows
    cell = t.rows[0][0]
    kinds = [type(b).__name__ for b in cell.blocks]
    assert kinds == ["Para", "Table", "Para"], kinds
    inner_t = cell.blocks[1]
    assert [[c.blocks[0].inlines[0].text for c in r] for r in inner_t.rows] == [["in1", "in2"]], inner_t.rows
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert "in1" in out and "in2" in out and "right" in out, out


def test_cellx_flood():
    data = b"{\\rtf1 \\trowd" + b"\\cellx1000" * 100_000 + b"\\pard\\intbl a\\cell b\\cell\\row}"
    t0 = time.perf_counter()
    out, d, ctx = doc_md(data)
    dt = time.perf_counter() - t0
    assert dt < 3, dt
    assert ctx.skipped["cells"] >= 1, ctx.skipped
    assert "table cut short" in out, out[-200:]


def test_listtext_marker():
    d, ctx = parse(rtf_file(rb"{\rtf1 \pard\ls1\ilvl0 {\listtext 1.\tab}Item\par}"))
    assert len(d.blocks) == 1 and isinstance(d.blocks[0], ListItem), d.blocks
    li = d.blocks[0]
    assert li.marker == "1." and li.level == 0 and li.inlines[0].text == "Item", li


def test_png_hex_pict():
    out = tmp() / "out"
    d, ctx = parse(rtf_file(b"{\\rtf1 a{\\pict\\pngblip\\picw10\\pich10 " + hexed(PNG) + b"}b\\par}"))
    assert [type(b).__name__ for b in d.blocks] == ["Para", "Image"], d.blocks
    assert d.blocks[1].data == PNG
    text = markdown.render(d, out / "assets", False, ctx.skipped)
    assert "assets/img-001.png" in text, text
    assert (out / "assets" / "img-001.png").read_bytes() == PNG


def test_bin_pict():
    data = b"{\\rtf1 a{\\pict\\pngblip\\bin%d " % len(PNG) + PNG + b"}b\\par}"
    d, ctx = parse(rtf_file(data))
    imgs = [b for b in d.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data == PNG and not ctx.skipped, (d.blocks, ctx.skipped)


def test_nonshppict_skipped():
    data = (b"{\\rtf1 a{\\*\\shppict{\\pict\\pngblip " + hexed(PNG) + b"}}{\\nonshppict{\\pict\\wmetafile8 "
            + hexed(b"\x01\x00\x09\x00" + bytes(28)) + b"}}b\\par}")
    d, ctx = parse(rtf_file(data))
    imgs = [b for b in d.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data == PNG and not ctx.skipped, (d.blocks, ctx.skipped)


def test_pict_budget():
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = len(PNG) - 1
    try:
        out, d, ctx = doc_md(b"{\\rtf1 a{\\pict\\pngblip " + hexed(PNG) + b"}b\\par}")
    finally:
        limits.IMAGE_BYTES = old
    assert "(could not read: 1 image)" in out, out
    assert ctx.skipped["image"] == 1 and not any(isinstance(b, Image) for b in d.blocks)


# ---- extras
def test_header_row_is_th():
    data = (b"{\\rtf1 " + rows((b"H1", 1000), (b"H2", 2000), hdr=True) + rows((b"a", 1000), (b"b", 2000)) + b"}")
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [c.header for c in t.rows[0]] == [True, True] and [c.header for c in t.rows[1]] == [False, False]
    # a span makes the renderer write HTML, where header cells are th
    data = (b"{\\rtf1 " + rows((b"H", 2000), hdr=True) + rows((b"a", 1000), (b"b", 2000)) + b"}")
    out = md(data)
    assert "<th" in out and "<td>a</td>" in out, out


def test_clustering_within_ten_twips():
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + rows((b"C", 1008), (b"D", 2003)) + b"}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [[c.colspan for c in r] for r in t.rows] == [[1, 1], [1, 1]], t.rows
    # ten apart is one column, eleven apart two
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + rows((b"C", 1011), (b"D", 2000)) + b"}"
    d, ctx = parse(rtf_file(data))
    assert [[c.colspan for c in r] for r in only_table(d).rows] == [[1, 2], [2, 1]]


def test_stray_vertical_merge_stays_visible():
    data = (b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000))
            + rows((b"", 1000), (b"D", 2000), flags={0: b"\\clvmrg"}) + b"}")
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [len(r) for r in t.rows] == [2, 2] and all(c.rowspan == 1 for r in t.rows for c in r), t.rows


def test_stray_horizontal_merge_stays_visible():
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000), flags={1: b"\\clmrg"}) + b"}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [len(r) for r in t.rows] == [2] and [c.colspan for c in t.rows[0]] == [1, 1], t.rows


def test_table_in_footnote_stays_in_the_note():
    data = (b"{\\rtf1 body{\\footnote\\pard\\plain " + rows((b"n1", 1000), (b"n2", 2000))
            + b"}\\par next\\par}")
    d, ctx = parse(rtf_file(data))
    assert not any(isinstance(b, Table) for b in d.blocks), d.blocks
    note = d.notes["fn-1"]
    assert any(isinstance(b, Table) for b in note), note
    out = markdown.render(d, tmp() / "assets", False, ctx.skipped)
    assert "n1" in out and "n2" in out and "next" in out, out


def test_picture_in_table_cell():
    pic = b"{\\pict\\pngblip " + hexed(PNG) + b"}"
    data = b"{\\rtf1 " + rows((b"x" + pic, 1000), (b"y", 2000)) + b"}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert any(isinstance(b, Image) for b in t.rows[0][0].blocks), t.rows
    assert not any(isinstance(b, Image) for b in d.blocks)


def test_macpict_counted():
    d, ctx = parse(rtf_file(b"{\\rtf1 a{\\pict\\macpict " + hexed(bytes(64)) + b"}b\\par}"))
    assert ctx.skipped["image"] == 1 and not any(isinstance(b, Image) for b in d.blocks), ctx.skipped


def test_dib_becomes_png():
    d, ctx = parse(rtf_file(b"{\\rtf1 {\\pict\\dibitmap0 " + hexed(DIB) + b"}\\par}"))
    imgs = [b for b in d.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data.startswith(b"\x89PNG"), (d.blocks, ctx.skipped)


def test_jpeg_and_object_result():
    data = (b"{\\rtf1 {\\object{\\*\\objdata " + hexed(bytes(range(64))) + b"}{\\result{\\pict\\jpegblip "
            + hexed(JPEG) + b"}}}\\par}")
    d, ctx = parse(rtf_file(data))
    imgs = [b for b in d.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data == JPEG and not ctx.skipped, (d.blocks, ctx.skipped)


def test_nested_group_in_pict_is_not_hex():
    data = (b"{\\rtf1 {\\pict{\\*\\blipuid 0123456789abcdef0123456789abcdef}\\pngblip\\picw5 " + hexed(PNG)
            + b"}\\par}")
    d, ctx = parse(rtf_file(data))
    imgs = [b for b in d.blocks if isinstance(b, Image)]
    assert len(imgs) == 1 and imgs[0].data == PNG, (d.blocks, ctx.skipped)


def test_huge_hex_pict_is_cut_early():
    old = limits.IMAGE_BYTES
    limits.IMAGE_BYTES = 1000
    try:
        line = b"89504e47" * 16 + b"\n"
        data = b"{\\rtf1 a{\\pict\\pngblip " + line * (10_000_000 // len(line)) + b"}b\\par}"
        t0 = time.perf_counter()
        out, d, ctx = doc_md(data)
        dt = time.perf_counter() - t0
    finally:
        limits.IMAGE_BYTES = old
    assert dt < 4, dt
    assert ctx.skipped["image"] == 1, ctx.skipped
    assert "(could not read: 1 image)" in out and "89504e47" not in out, out[:200]


def test_ls_without_listtext_is_a_dash():
    d, ctx = parse(rtf_file(rb"{\rtf1 \pard\ls3\ilvl2 item\par\pard plain\par}"))
    li = d.blocks[0]
    assert isinstance(li, ListItem) and li.marker == "-" and li.level == 2, d.blocks
    assert not isinstance(d.blocks[1], ListItem), d.blocks  # \pard reset \ls and \ilvl


def test_list_item_in_a_cell_stays_a_list_item():
    data = b"{\\rtf1 \\trowd\\cellx1000\\pard\\intbl\\ls1 {\\listtext 1.\\tab}one\\cell\\row}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert isinstance(t.rows[0][0].blocks[0], ListItem), t.rows
    assert t.rows[0][0].blocks[0].marker == "1."


def test_ilvl_clamped():
    d, ctx = parse(rtf_file(rb"{\rtf1 \pard\ls1\ilvl99 {\listtext x}a\par}"))
    assert d.blocks[0].level == 8, d.blocks


def test_itap_clamped_and_counted():
    data = b"{\\rtf1 \\trowd\\cellx1000\\pard\\intbl\\itap12 deep\\cell\\row}"
    out, d, ctx = doc_md(data)
    assert "deep" in out and ctx.skipped["depth"] == 1, (out, ctx.skipped)


def test_rows_flood_counts_table_nodes():
    old = limits.TABLE_NODES
    limits.TABLE_NODES = 5000
    try:
        data = b"{\\rtf1 " + b"\\trowd\\cellx1000\\pard\\intbl a\\cell\\row\n" * 200_000 + b"\\pard end\\par}"
        t0 = time.perf_counter()
        out, d, ctx = doc_md(data)
        dt = time.perf_counter() - t0
    finally:
        limits.TABLE_NODES = old
    assert dt < 12, dt
    assert ctx.skipped["cells"] == 1, ctx.skipped
    assert out.rstrip().endswith("end"), out[-50:]
    assert len(only_table(d).rows) == 5000


def test_rows_flood_default_cap_speed():
    data = b"{\\rtf1 " + b"\\trowd\\cellx1000\\pard\\intbl a\\cell\\row\n" * 200_000 + b"}"
    t0 = time.perf_counter()
    d, ctx = parse(rtf_file(data))
    dt = time.perf_counter() - t0
    assert dt < 8, dt
    assert ctx.skipped["cells"] == 1, ctx.skipped


def test_text_after_table_is_not_in_it():
    data = b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + b"\\pard after\\par\\pard more\\par}"
    d, ctx = parse(rtf_file(data))
    assert [type(b).__name__ for b in d.blocks] == ["Table", "Para", "Para"], d.blocks


def test_two_tables_apart():
    # text between two tables splits them
    data = (b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + b"\\pard between\\par\n" + rows((b"C", 1000), (b"D", 2000))
            + b"}")
    d, ctx = parse(rtf_file(data))
    assert [type(b).__name__ for b in d.blocks] == ["Table", "Para", "Table"], d.blocks


def test_empty_paragraph_between_rows_does_not_split_the_table():
    data = (b"{\\rtf1 " + rows((b"A", 1000), (b"B", 2000)) + b"\\pard\\par\n" + rows((b"C", 1000), (b"D", 2000))
            + b"}")
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert spans(t) == [[("A", 1), ("B", 1)], [("C", 1), ("D", 1)]], spans(t)
    # several empty paragraphs, and one after the last row, change nothing
    data = (b"{\\rtf1 " + rows((b"A", 1000)) + b"\\pard\\par\\pard\\par\n" + rows((b"C", 1000)) + b"\\pard\\par after\\par}")
    d, ctx = parse(rtf_file(data))
    assert [type(b).__name__ for b in d.blocks] == ["Table", "Para"], d.blocks
    assert len(d.blocks[0].rows) == 2, d.blocks[0].rows


def test_footnote_starts_with_no_list_state():
    data = rb"{\rtf1 \pard\ls1\ilvl2 item{\footnote note text}\par}"
    d, ctx = parse(rtf_file(data))
    assert type(d.blocks[0]).__name__ == "ListItem", d.blocks
    note = d.notes["fn-1"]
    assert [type(b).__name__ for b in note] == ["Para"], note
    # the paragraph after the note is still the list item
    d, ctx = parse(rtf_file(rb"{\rtf1 \pard\ls1 a{\footnote n} b\par}"))
    assert [type(b).__name__ for b in d.blocks] == ["ListItem"], d.blocks
    # a note with its own list state keeps it
    d, ctx = parse(rtf_file(rb"{\rtf1 a{\footnote\pard\ls1 n}\par}"))
    assert [type(b).__name__ for b in d.notes["fn-1"]] == ["ListItem"], d.notes


def test_cells_past_defined_edges_are_kept():
    data = (b"{\\rtf1 \\trowd\\cellx1000\\pard\\intbl a\\cell b\\cell c\\cell\\row}")
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    assert [len(r) for r in t.rows] == [3], t.rows


def test_table_header_footer_text():
    data = (b"{\\rtf1{\\header " + rows((b"h1", 1000), (b"h2", 2000)) + b"}body\\par}")
    d, ctx = parse(rtf_file(data))
    assert d.headers == ["h1 h2"], d.headers

def lrow(left, cells, extra=b""):
    """A row with an optional trleft (None for none): cells are (text, edge) pairs."""
    head = b"\\trowd" + (b"\\trleft%d" % left if left is not None else b"") + extra
    head += b"".join(b"\\cellx%d" % e for _, e in cells)
    return head + b"\\pard\\intbl " + b"".join(t + b"\\cell " for t, _ in cells) + b"\\row\n"


def spans(t):
    return [[(c.blocks[0].inlines[0].text if c.blocks else "", c.colspan) for c in r] for r in t.rows]


def test_trleft_gap_cell():
    data = b"{\\rtf1 " + lrow(None, [(b"a", 1000), (b"b", 2000)]) + lrow(1000, [(b"c", 2000)]) + b"}"
    d, ctx = parse(rtf_file(data))
    assert spans(only_table(d)) == [[("a", 1), ("b", 1)], [("", 1), ("c", 1)]], spans(only_table(d))
    assert md(data) == "|a|b|\n|---|---|\n||c|\n"


def test_trleft_minus_108_adds_no_column():
    data = (b"{\\rtf1 " + lrow(-108, [(b"a", 1000), (b"b", 2000)]) + lrow(-108, [(b"c", 1000), (b"d", 2000)])
            + b"}")
    d, ctx = parse(rtf_file(data))
    assert spans(only_table(d)) == [[("a", 1), ("b", 1)], [("c", 1), ("d", 1)]]
    # rows with and without it in one table: no gap either
    data = b"{\\rtf1 " + lrow(None, [(b"a", 1000), (b"b", 2000)]) + lrow(-108, [(b"c", 1000), (b"d", 2000)]) + b"}"
    d, ctx = parse(rtf_file(data))
    assert spans(only_table(d)) == [[("a", 1), ("b", 1)], [("c", 1), ("d", 1)]]


def test_trleft_kept_by_rows_without_it():
    data = (b"{\\rtf1 " + lrow(None, [(b"a", 1000), (b"b", 2000)]) + lrow(1000, [(b"c", 2000)])
            + lrow(None, [(b"d", 2000)]) + b"}")
    d, ctx = parse(rtf_file(data))
    assert spans(only_table(d))[2] == [("", 1), ("d", 1)], spans(only_table(d))


def test_trleft_past_first_edge_is_clamped():
    data = b"{\\rtf1 " + lrow(None, [(b"a", 1000), (b"b", 2000)]) + lrow(5000, [(b"c", 1500), (b"d", 2000)]) + b"}"
    d, ctx = parse(rtf_file(data))
    t = only_table(d)
    # the left edge lands just below the first edge: the row keeps its cells and the grid's width
    assert [sum(c.colspan for c in r) for r in t.rows] == [3, 3], spans(t)
    assert [c.blocks[0].inlines[0].text for c in t.rows[1] if c.blocks] == ["c", "d"], spans(t)


def test_nested_row_with_its_own_trleft():
    inner = (b"\\pard\\intbl\\itap2 i1\\nestcell"
             b"{\\*\\nesttableprops\\trowd\\trleft500\\cellx1000\\nestrow}")
    inner2 = (b"\\pard\\intbl\\itap2 i2\\nestcell\\pard\\intbl\\itap2 i3\\nestcell"
              b"{\\*\\nesttableprops\\trowd\\trleft0\\cellx500\\cellx1000\\nestrow}")
    data = (b"{\\rtf1 \\trowd\\cellx2000\\pard\\intbl\\itap1 " + inner + inner2
            + b"\\pard\\intbl\\itap1 \\cell\\row}")
    d, ctx = parse(rtf_file(data))
    inner_t = only_table(d).rows[0][0].blocks[0]
    assert spans(inner_t) == [[("", 1), ("i1", 1)], [("i2", 1), ("i3", 1)]], spans(inner_t)

def grid_of(*row_specs):
    data = b"{\\rtf1 " + b"".join(lrow(left, cells) for left, cells in row_specs) + b"}"
    d, ctx = parse(rtf_file(data))
    return spans(only_table(d))


def test_trleft_minus_108_mixed_with_other_left_edges():
    ab = [(b"a", 1000), (b"b", 2000)]
    # -108 beside a row that starts right of the left edge: the gap is one column, the -108 rows add none
    got = grid_of((-108, ab), (1000, [(b"c", 2000)]), (-108, [(b"d", 1000), (b"e", 2000)]))
    assert got == [[("a", 1), ("b", 1)], [("", 1), ("c", 1)], [("d", 1), ("e", 1)]], got
    # no \trleft on the first row, -108 after a row with another left edge
    got = grid_of((None, ab), (1000, [(b"c", 2000)]), (-108, [(b"d", 1000), (b"e", 2000)]))
    assert got == [[("a", 1), ("b", 1)], [("", 1), ("c", 1)], [("d", 1), ("e", 1)]], got
    # a row without \trleft keeps the one before it (here -108): no gap invented by a default of 0
    got = grid_of((-108, ab), (None, [(b"c", 1000), (b"d", 2000)]))
    assert got == [[("a", 1), ("b", 1)], [("c", 1), ("d", 1)]], got
    # 0 and -108 are 108 twips apart, far past a cluster: the row at 0 starts one column in, as doc.py's rows do,
    # and the row at -108 (the table's edge) has its first cell over that column too
    got = grid_of((0, ab), (-108, ab))
    assert got == [[("", 1), ("a", 1), ("b", 1)], [("a", 2), ("b", 1)]], got


def test_left_edge_clustered_with_or_clamped_to_the_first_edge_leaves_the_right_gap():
    # row 1 has its left edge 5 twips before its first edge, which row 0 shares: clustered, so no gap before it
    got = grid_of((None, [(b"a", 1000), (b"b", 2000), (b"c", 3000)]), (995, [(b"d", 1000), (b"e", 2000)]))
    assert got[1] == [("d", 1), ("e", 1)], got
    # a left edge past the first edge is clamped just below it: the gap is every column before that edge, however
    # many, and the first cell takes one column
    got = grid_of((None, [(b"a", 1000), (b"b", 2000), (b"c", 3000)]), (9000, [(b"d", 2000), (b"e", 3000)]))
    assert got[1] == [("", 1), ("d", 1), ("e", 1)], got
    got = grid_of((None, [(b"a", 1000), (b"b", 2000), (b"c", 3000), (b"z", 4000)]),
                  (9000, [(b"d", 3000), (b"e", 4000)]))
    assert got[1] == [("", 2), ("d", 1), ("e", 1)], got
    # left exactly on the first edge
    got = grid_of((None, [(b"a", 1000), (b"b", 2000), (b"c", 3000)]), (2000, [(b"d", 2000), (b"e", 3000)]))
    assert got[1] == [("", 1), ("d", 1), ("e", 1)], got


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
