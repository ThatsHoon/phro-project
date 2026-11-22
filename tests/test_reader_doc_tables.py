"""Word 97-2003 (.doc) lists and tables: PlfLst and PlfLfo markers with numbers shared per lsid, cell and row marks,
TDefTable spans on the boundary grid, nested tables, hostile input.

Run: python -I -X utf8 tests/test_reader_doc_tables.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import doc, doc_fib  # noqa: E402
from reader.model import Cell, Context, ListItem, Para, Table, Text  # noqa: E402
from reader_fix import doc_lists, doc_stsh, make_doc  # noqa: E402

IN_TABLE, TTP, INNER_CELL, INNER_ROW, ITAP = 0x2416, 0x2417, 0x244B, 0x244C, 0x6649
ILVL, ILFO, TDEF, OUTLINE, ISTD = 0x260A, 0x460B, 0xD608, 0x2640, 0x4600
# TCGRF as Word writes it and LibreOffice reads it (ww8par2.cxx): 0x0001 fFirstMerged opens a horizontal set, 0x0002
# fMerged without it joins the set (MS-DOC 2.9.328's horzMerge table reads the two bits the other way); fVertMerge
# 0x0020 with fVertRestart 0x0040 starts a vertical set, fVertMerge alone continues it
FIRST_MERGED, MERGED, VERT_MERGE, VERT_RESTART = 0x0001, 0x0002, 0x0020, 0x0040
NORMAL = (0, 1, 0xFFF, "Normal", b"", b"")


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def sp(op, value=1):
    """One sprm: a number is sized by op >> 13; bytes are a variable operand (u8 length; u16 length + 1 for
    sprmTDefTable)."""
    if isinstance(value, bytes):
        if op in (0xD606, 0xD608):
            return u16(op) + u16(len(value) + 1) + value
        return u16(op) + bytes([len(value)]) + value
    return u16(op) + value.to_bytes((1, 1, 2, 4, 2, 2, 0, 3)[op >> 13], "little")


def doc_file(pieces, **kw):
    return make_doc(tmp() / "a.doc", pieces, **kw)


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    d, ctx = parse(path)
    return markdown.render(d, tmp() / "assets", hidden, ctx.skipped)


def list_doc(items, lists, lfos, **kw):
    """Paragraphs (text, ilfo, ilvl), each ended by \\r, its PAPX giving sprmPIlfo and sprmPIlvl unless ilfo is None;
    the PlfLst and PlfLfo from doc_lists(lists, lfos)."""
    text, spans = "", []
    for t, ilfo, ilvl in items:
        g = u16(0) + (sp(ILFO, ilfo) + sp(ILVL, ilvl) if ilfo is not None else b"")
        spans.append((len(text), len(text) + len(t) + 1, g))
        text += t + "\r"
    fc = doc_lists(lists, lfos)
    fc.update(kw.pop("fc", {}))
    return doc_file([(text, False)], fc=fc, papx=spans, **kw)


def tdef(bounds, grf=()):
    """A TDefTable operand: itcMac, the itcMac + 1 boundaries, then a TC80 a cell (its TCGRF, then 18 zero bytes)."""
    k = len(bounds) - 1
    return (bytes([k]) + b"".join(struct.pack("<h", b) for b in bounds)
            + b"".join(u16(grf[i] if i < len(grf) else 0) + bytes(18) for i in range(k)))


def table_text(rows, tdefs=(), at=0):
    """Text and PAPX spans of a depth-1 table: rows of cell texts, each cell ended by \\x07 (sprmPFInTable), each row
    by a TTP mark \\x07 (sprmPFTtp) whose PAPX holds tdefs[r] when given. CPs start at `at`."""
    text, spans = "", []
    cell = u16(0) + sp(IN_TABLE, 1)
    for r, row in enumerate(rows):
        start = len(text)
        text += "".join(c + "\x07" for c in row)
        if len(text) > start:
            spans.append((at + start, at + len(text), cell))
        ttp = cell + sp(TTP, 1) + (sp(TDEF, tdefs[r]) if r < len(tdefs) and tdefs[r] is not None else b"")
        text += "\x07"
        spans.append((at + len(text) - 1, at + len(text), ttp))
    return text, spans


def table_doc(rows, tdefs=(), **kw):
    text, spans = table_text(rows, tdefs)
    return doc_file([(text, False)], papx=spans, **kw)


# ---- the brief's behaviour: lists
def test_numbered_list():
    p = list_doc([("a", 1, 0), ("b", 1, 0)], [(7, [(0, "%1.")])], [(7, ())])
    assert md(p) == "1. a\n2. b\n"
    d, _ = parse(p)
    assert d.blocks == [ListItem(0, "1.", [Text("a")]), ListItem(0, "2.", [Text("b")])], d.blocks


def test_list_two_levels():
    levels = [(0, "%1."), (4, "%2)")]
    items = [("a", 1, 0), ("b", 1, 1), ("c", 1, 1), ("d", 1, 0), ("e", 1, 1)]
    assert md(list_doc(items, [(7, levels)], [(7, ())])) == "1. a\n  a) b\n  b) c\n2. d\n  a) e\n"
    # a composite number text, iStartAt, and the number formats nfc 0-4 (anything else decimal)
    deep = [(1, "%1)", 3), (0, "%1.%2", 1)]
    assert md(list_doc([("x", 1, 0), ("y", 1, 1)], [(7, deep)], [(7, ())])) == "III) x\n  III.1 y\n"
    for nfc, want in ((0, "3"), (1, "III"), (2, "iii"), (3, "C"), (4, "c"), (47, "3")):
        assert md(list_doc([("x", 1, 0)], [(7, [(nfc, "%1", 3)])], [(7, ())])) == f"{want} x\n", nfc


def test_bullet_list():
    p = list_doc([("a", 1, 0), ("b", 1, 0)], [(7, [(23, "•")])], [(7, ())])
    assert md(p) == "- a\n- b\n"
    d, _ = parse(p)
    assert all(isinstance(b, ListItem) and b.marker == "-" for b in d.blocks), d.blocks
    # nfc 255: a level without numbers is a plain paragraph
    assert md(list_doc([("a", 1, 0)], [(7, [(255, "")])], [(7, ())])) == "a\n"


def test_shared_lsid_counter():
    items = [("a", 1, 0), ("b", 2, 0), ("c", 1, 0)]
    assert md(list_doc(items, [(7, [(0, "%1.")])], [(7, ()), (7, ())])) == "1. a\n2. b\n3. c\n"
    # an LFO start-at override restarts the shared numbers the first time that LFO numbers the level
    items = [("a", 1, 0), ("b", 1, 0), ("c", 2, 0), ("d", 2, 0), ("e", 1, 0)]
    assert md(list_doc(items, [(7, [(0, "%1.")])], [(7, ()), (7, [(0, 1)])])) == "1. a\n2. b\n1. c\n2. d\n3. e\n"
    # two lists keep their own numbers
    items = [("a", 1, 0), ("b", 2, 0), ("c", 1, 0)]
    two = [(7, [(0, "%1.")]), (8, [(0, "%1)")])]
    assert md(list_doc(items, two, [(7, ()), (8, ())])) == "1. a\n1) b\n2. c\n"


def test_suppressed_ilfo():
    items = [("a", 1, 0), ("b", 0xF801, 0), ("c", 1, 0), ("d", 0, 0), ("e", 9, 0), ("f", None, 0)]
    assert md(list_doc(items, [(7, [(0, "%1.")])], [(7, ())])) == "1. a\n\nb\n\n2. c\n\nd\n\ne\n\nf\n"


# ---- the brief's behaviour: tables
def test_table_2x2():
    assert md(table_doc([["a", "b"], ["c", "d"]])) == "a|b\n-|-\nc|d\n"
    with_defs = table_doc([["a", "b"], ["c", "d"]], [tdef([0, 1000, 2000])] * 2)
    assert md(with_defs) == "a|b\n-|-\nc|d\n"
    d, _ = parse(with_defs)
    assert d.blocks == [Table([[Cell([Para([Text("a")])]), Cell([Para([Text("b")])])],
                               [Cell([Para([Text("c")])]), Cell([Para([Text("d")])])]])], d.blocks


def test_table_horizontal_merge():
    defs = [tdef([0, 1, 2, 3], [FIRST_MERGED, MERGED, 0]), tdef([0, 1, 2, 3])]
    want = "ab||c\n-|-|-\nd|e|f\n\nMerged: A1:B1\n"
    assert md(table_doc([["ab", "", "c"], ["d", "e", "f"]], defs)) == want
    # both bits (3) make a first cell too; a merged cell that holds text gives it to the first cell
    defs = [tdef([0, 1, 2, 3], [FIRST_MERGED | MERGED, MERGED, 0]), tdef([0, 1, 2, 3])]
    out = md(table_doc([["a", "b", "c"], ["d", "e", "f"]], defs))
    assert out == "a<br>b||c\n-|-|-\nd|e|f\n\nMerged: A1:B1\n", out
    # fMerged with no set open is a plain cell
    out = md(table_doc([["a", "b"], ["c", "d"]], [tdef([0, 1, 2], [MERGED, 0])]))
    assert out == "a|b\n-|-\nc|d\n", out
    # adjacent merged pairs: a first cell closes the set before it and opens its own
    pair = "|a||c||\n-|-|-|-\nd|e|f|g\n\nMerged: A1:B1, C1:D1\n"
    for grf in ([FIRST_MERGED, MERGED, FIRST_MERGED, MERGED], [FIRST_MERGED, MERGED, FIRST_MERGED | MERGED, MERGED]):
        defs = [tdef([0, 1, 2, 3, 4], grf), tdef([0, 1, 2, 3, 4])]
        out = md(table_doc([["a", "", "c", ""], ["d", "e", "f", "g"]], defs))
        assert out == pair, (grf, out)
    # a set goes on through every fMerged cell
    defs = [tdef([0, 1, 2, 3], [FIRST_MERGED, MERGED, MERGED]), tdef([0, 1, 2, 3])]
    assert md(table_doc([["a", "", ""], ["d", "e", "f"]], defs)) == "|a|||\n-|-|-\nd|e|f\n\nMerged: A1:C1\n"


def test_table_vertical_merge():
    defs = [tdef([0, 1, 2], [VERT_MERGE | VERT_RESTART, 0]), tdef([0, 1, 2], [VERT_MERGE, 0])]
    want = "a|b\n-|-\n||d|\n\nMerged: A1:A2\n"
    assert md(table_doc([["a", "b"], ["", "d"]], defs)) == want
    three = defs + [tdef([0, 1, 2], [VERT_MERGE, 0])]
    assert md(table_doc([["a", "b"], ["", "d"], ["", "f"]], three)) == "a|b\n-|-\n||d|\n||f|\n\nMerged: A1:A3\n"
    # a row without the merge ends the set; a continuation under nothing open is a plain cell
    ended = defs + [tdef([0, 1, 2]), tdef([0, 1, 2], [VERT_MERGE, 0])]
    out = md(table_doc([["a", "b"], ["", "d"], ["e", "f"], ["g", "h"]], ended))
    assert out == "a|b\n-|-\n||d|\ne|f\ng|h\n\nMerged: A1:A2\n", out


def test_nested_table():
    outer = u16(0) + sp(IN_TABLE, 1) + sp(ITAP, 1)
    inner = u16(0) + sp(IN_TABLE, 1) + sp(ITAP, 2)
    text = "x\x07" + "p\r" + "q\r" + "\r" + "\x07" + "\x07"
    spans = [(0, 2, outer), (2, 4, inner + sp(INNER_CELL, 1)), (4, 6, inner + sp(INNER_CELL, 1)),
             (6, 7, inner + sp(INNER_ROW, 1)), (7, 8, outer), (8, 9, outer + sp(TTP, 1))]
    p = doc_file([(text, False)], papx=spans)
    assert md(p) == "x|p / q\n-|-\n"
    d, _ = parse(p)
    (row,) = d.blocks[0].rows
    assert row[1].blocks == [Table([[Cell([Para([Text("p")])]), Cell([Para([Text("q")])])]])], row[1].blocks


def test_table_without_ttp():
    cell = u16(0) + sp(IN_TABLE, 1)
    p = doc_file([("a\x07b\x07after\r", False)], papx=[(0, 4, cell), (4, 10, u16(0))])
    assert md(p) == "a|b\n-|-\n\nafter\n"
    d, ctx = parse(doc_file([("a\x07b\x07", False)], papx=[(0, 4, cell)]))
    assert len(d.blocks) == 1 and len(d.blocks[0].rows) == 1 and not ctx.skipped, (d.blocks, ctx.skipped)


# ---- ruling P9 and the Task 10 _READ trap
def test_fc_trailing():
    p = doc_file([("x\r", False)], fc={73: (b"abc", b"xyz"), 74: b"\x00" * 4})
    with Compound(p) as cf:
        word, table = cf.read("WordDocument"), cf.read("1Table")
    f = doc_fib.fib(word)
    fc, lcb = f.pair(73)
    assert lcb == 3 and table[fc:fc + 6] == b"abcxyz" and f.pair(74) == (fc + 6, 4), (f.pair(73), f.pair(74))
    assert md(p) == "x\n"


def test_read_sprms_survive_style_upx():
    # every sprm the reader handles is kept by last_sprms, and so reaches the properties through a style's UPX
    def grpprl(op):
        return sp(op, b"\x01" + bytes(5)) if op >> 13 == 6 else sp(op, 1)
    for op, (name, _) in doc_fib.PARA_SPRMS.items():
        g = grpprl(op)
        assert doc_fib.last_sprms(doc_fib.sprms(g)) == doc_fib.sprms(g), hex(op)
        s = doc_fib.Styles(doc_stsh([NORMAL, (0xFFE, 1, 0, "s", g, b"")]))
        assert getattr(s.para(1, []), name) != getattr(doc_fib.ParaProps(), name), (hex(op), name)
    for op, name in {**doc_fib.RUN_TOGGLES, **doc_fib.RUN_VALUES}.items():
        g = grpprl(op)
        assert doc_fib.last_sprms(doc_fib.sprms(g)) == doc_fib.sprms(g), hex(op)
        s = doc_fib.Styles(doc_stsh([NORMAL, (0xFFE, 1, 0, "s", b"", g)]))
        assert getattr(s.run(1, []), name) != getattr(doc_fib.RunProps(), name), (hex(op), name)
    assert set(doc_fib.PARA_SPRMS) | set(doc_fib.RUN_TOGGLES) | set(doc_fib.RUN_VALUES) <= doc_fib._READ


# ---- more lists
def test_list_lvl_walk():
    # grpprlPapx and grpprlChpx inside the LVLs are stepped over; the second list's levels are found past them
    first = [(0, "%1.", 1, sp(OUTLINE, 1) * 3, sp(0x0835, 1))] + [(0, f"%{k}.", 1, b"\x00" * 7, b"\x00") for k in
                                                                 range(2, 10)]
    lists = [(7, first), (8, [(4, "(%1)", 2, b"\x00" * 9, b"")])]
    items = [("a", 1, 0), ("b", 2, 0)]
    assert md(list_doc(items, lists, [(7, ()), (8, ())])) == "1. a\n(b) b\n"
    # an LFOLVL fFormatting override replaces the level; its LVL is stepped over to the next LFO's data
    lfos = [(7, [(0, None, (3, "<%1>", 4))]), (8, [(0, 5)])]
    assert md(list_doc(items, lists, lfos)) == "<D> a\n(e) b\n"
    # fStartAt with fFormatting: MS-DOC makes the LFOLVL's iStartAt (2 here) undefined, so the start is the override
    # LVL's (4), restarting the list's numbers
    lfos = [(7, [(0, 2, (3, "<%1>", 4))]), (8, ())]
    assert md(list_doc([("a", 1, 0), ("c", 1, 0)], lists, lfos)) == "<D> a\n<E> c\n"
    lfos = [(7, ()), (7, [(0, 2, (3, "<%1>", 4))])]
    assert md(list_doc([("a", 1, 0), ("b", 1, 0), ("c", 2, 0)], lists, lfos)) == "1. a\n2. b\n<D> c\n"


def test_lfo_data_inside_lcb():
    # MS-DOC counts the LFOData in lcbPlfLfo; read either way
    fc = doc_lists([(7, [(0, "%1.")])], [(7, ()), (7, [(0, 4)])])
    fc[74] = fc[74][0] + fc[74][1]
    items = [("a", 1, 0), ("b", 2, 0)]
    assert md(list_doc(items, [], [], fc=fc)) == "1. a\n4. b\n"


def test_list_heading_and_cells():
    styles = doc_stsh([NORMAL, (1, 1, 0, "heading 1", b"", b"")])
    fc = doc_lists([(7, [(0, "%1.")])], [(7, ())])
    fc[1] = styles
    g = sp(ILFO, 1) + sp(ILVL, 0)
    p = doc_file([("Title\rbody\r", False)], fc=fc, papx=[(0, 6, u16(1) + g), (6, 11, u16(0) + g)])
    assert md(p) == "# 1. Title\n\n2. body\n"
    cell = u16(0) + sp(IN_TABLE, 1)
    q = doc_file([("a\x07b\x07\x07", False)], fc=fc, papx=[(0, 2, cell + g), (2, 4, cell), (4, 5, cell + sp(TTP, 1))])
    assert md(q) == "|1. a|b|\n-|-\n"
    # an empty list paragraph still takes its number
    assert md(list_doc([("a", 1, 0), ("", 1, 0), ("c", 1, 0)], [(7, [(0, "%1.")])], [(7, ())])) == "1. a\n3. c\n"


def test_ilvl_clamped():
    lv = [(0, f"%{k}.") for k in range(1, 10)]
    d, _ = parse(list_doc([("a", 1, 0), ("b", 1, 200)], [(7, lv)], [(7, ())]))
    assert d.blocks[1] == ListItem(8, "1.", [Text("b")]), d.blocks
    # a simple list has one level: any ilvl numbers it
    assert md(list_doc([("a", 1, 0), ("b", 1, 3)], [(7, [(0, "%1.")])], [(7, ())])).startswith("1. a\n")


def test_lists_bounded():
    good = doc_lists([(7, [(0, "%1.")])], [(7, ())])
    lst, lvls = good[73]
    lfo, data = good[74]
    table = lst + lvls + lfo + data
    at_lfo = len(lst) + len(lvls)
    s = doc_fib.Lists(table, (0, len(lst)), (at_lfo, len(lfo)))
    assert s.marker(1, 0) == "1." and s.bad == 0
    # cLst far past what lcb holds: the LSTFs lcb holds are read
    big = struct.pack("<h", 30_000) + lst[2:]
    s = doc_fib.Lists(big + lvls + lfo + data, (0, len(lst)), (at_lfo, len(lfo)))
    assert s.marker(1, 0) == "1." and s.bad == 1
    # an LVL whose xst runs past the stream; lcb past the stream; LSTFs and LFOs with no room at all
    cut = lst + lvls[:-1]
    s = doc_fib.Lists(cut, (0, len(lst)), (0, 0))
    assert s.marker(1, 0) is None and s.bad == 1
    for pair in ((0, len(table) + 1), (len(table) - 1, 2)):
        s = doc_fib.Lists(table, pair, pair)
        assert s.bad == 2 and s.marker(1, 0) is None, pair
    # rgbxchNums offsets past xst are counted and read as no placeholder
    bad_nums = bytearray(lvls)
    bad_nums[6:8] = bytes([1, 40])
    s = doc_fib.Lists(lst + bytes(bad_nums) + lfo + data, (0, len(lst)), (at_lfo, len(lfo)))
    assert s.marker(1, 0) == "1." and s.bad == 1
    # a grpprl size past the stream
    big_grpprl = bytearray(lvls)
    big_grpprl[25] = 255
    s = doc_fib.Lists(lst + bytes(big_grpprl), (0, len(lst)), (0, 0))
    assert s.marker(1, 0) is None and s.bad == 1
    # lfoMac 2^32 - 1: the LFOs the stream holds (two); the first has clfolvl 255 and its LFOData is cut after the
    # cp: no hang, counted, and both LFOs still name their list
    rec = lfo[4:20]
    rec255 = rec[:12] + bytes([255]) + rec[13:]
    huge = struct.pack("<I", 0xFFFFFFFF) + rec255 + rec + b"\xff" * 4 + bytes(4)
    t0 = time.monotonic()
    s = doc_fib.Lists(lst + lvls + huge, (0, len(lst)), (at_lfo, 4))
    assert time.monotonic() - t0 < 1 and s.bad == 2 and len(s.lfos) == 2, (s.bad, len(s.lfos))
    assert s.marker(1, 0) == "1." and s.marker(2, 0) == "2."
    # a marker is at most 64 characters, however long the number text
    long = doc_lists([(7, [(0, "%1" + "x" * 5000)])], [(7, ())])
    t = long[73][0] + long[73][1] + long[74][0] + long[74][1]
    s = doc_fib.Lists(t, (0, len(long[73][0])), (len(long[73][0]) + len(long[73][1]), len(long[74][0])))
    assert s.marker(1, 0) == "1" + "x" * 63


def test_list_counters_bounded():
    # 20,000 paragraphs at changing levels of one list: the numbers stay per level, nine at most a list
    lv = [(0, f"%{k}.") for k in range(1, 10)]
    items = [(f"p{k}", 1, k % 9) for k in range(20_000)]
    p = list_doc(items, [(7, lv)], [(7, ())])
    t0 = time.monotonic()
    d, _ = parse(p)
    assert len(d.blocks) == 20_000 and time.monotonic() - t0 < 10
    s = doc_fib.Lists()
    assert s.marker(1, 0) is None and s.marker(0, 0) is None and s.marker(0xF801, 0) is None


# ---- more tables
def test_table_boundary_grid():
    # a row of one wide cell over a row of two: the boundaries put both on one grid, as Word's tblGrid does
    defs = [tdef([0, 2000]), tdef([0, 1000, 2000])]
    want = "|head||\n-|-\na|b\n\nMerged: A1:B1\n"
    assert md(table_doc([["head"], ["a", "b"]], defs)) == want
    # a row that starts right of the grid's left edge starts with an empty cell over the gap
    defs = [tdef([0, 1000, 2000]), tdef([1000, 2000])]
    assert md(table_doc([["a", "b"], ["c"]], defs)) == "a|b\n-|-\n||c|\n"
    # vertical merges match cells over the same grid columns
    defs = [tdef([0, 1000, 2000], [VERT_MERGE | VERT_RESTART, 0]), tdef([0, 1000, 2000], [VERT_MERGE, 0])]
    assert md(table_doc([["a", "b"], ["", "d"]], defs)) == "a|b\n-|-\n||d|\n\nMerged: A1:A2\n"


def test_tdef_damaged():
    rows = [["a", "b"], ["c", "d"]]
    for bad in (bytes([64]) + bytes(400), bytes([3]) + bytes(4), tdef([1000, 0, 2000]), bytes([0]) + bytes(2), b""):
        d, ctx = parse(table_doc(rows, [bad, tdef([0, 1000, 2000])]))
        out = markdown.render(d, tmp() / "a", False, ctx.skipped)
        assert out == "a|b\n-|-\nc|d\n" and ctx.skipped["styles"] == 1, (bad[:4], out, ctx.skipped)
    # fewer TC80s than cells: the rest have no merges, uncounted
    short = tdef([0, 1, 2], [FIRST_MERGED, MERGED])[:1 + 6 + 20]
    d, ctx = parse(table_doc(rows, [short]))
    assert not ctx.skipped and len(d.blocks[0].rows[0]) == 2, ctx.skipped
    # crafted grids stay linear and inside the renderer's budgets: every row shifts its boundaries, so the grid has
    # a column for each and every cell spans about a hundred; a vertical set over the whole width matches nothing
    rows = [["x"]] + [[f"y{r}"] * 20 for r in range(100)]
    wide = [tdef([0, 30000], [VERT_MERGE | VERT_RESTART])]
    wide += [tdef([r + 400 * k for k in range(21)])[:1 + 2 * 21] for r in range(100)]  # boundaries, no TC80s
    p = table_doc(rows, wide)
    t0 = time.monotonic()
    d, ctx = parse(p)
    out = markdown.render(d, tmp() / "a", False, ctx.skipped)
    assert time.monotonic() - t0 < 10 and "x" in out and "y99" in out and not ctx.skipped, ctx.skipped
    assert max(c.colspan for row in d.blocks[0].rows for c in row) > 90


def test_cp_exact_cells():
    # a compressed piece then a Unicode one with a surrogate pair: the TTP is found at its CP, not its text index
    cell, ttp = u16(0) + sp(IN_TABLE, 1), u16(0) + sp(IN_TABLE, 1) + sp(TTP, 1)
    p = doc_file([("a\x07", True), ("😀\x07\x07", False)], papx=[(0, 5, cell), (5, 6, ttp)])
    assert md(p) == "a|😀\n-|-\n"
    text = "😀\x07b\x07\x07c\x07d\x07\x07"  # CPs: 😀 0-1, marks at 2, 4, 5, 7, 9, 10
    spans = [(0, 5, cell), (5, 6, ttp), (6, 10, cell), (10, 11, ttp)]
    assert md(doc_file([(text, False)], papx=spans)) == "😀|b\n-|-\nc|d\n"


def test_table_depth_capped():
    # paragraphs past MAX_DEPTH stay as text in the innermost open cell; one overflowing subtree counts once
    def par(itap, extra=b""):
        return u16(0) + sp(IN_TABLE, 1) + sp(ITAP, itap) + extra
    over = limits.MAX_DEPTH + 6
    text = "a\r" + "deep\r" + "deeper\r" + "\x07" + "b\r" + "again\r" + "\x07" + "\x07"
    spans = [(0, 2, par(1)), (2, 7, par(over)), (7, 14, par(over + 30, sp(INNER_ROW, 1))), (14, 15, par(1)),
             (15, 17, par(1)), (17, 23, par(over)), (23, 24, par(1)), (24, 25, par(1, sp(TTP, 1)))]
    d, ctx = parse(doc_file([(text, False)], papx=spans))
    assert ctx.skipped["depth"] == 2, ctx.skipped
    (row,) = d.blocks[0].rows
    assert [b.inlines[0].text for b in row[0].blocks] == ["a", "deep", "deeper"]
    assert [b.inlines[0].text for b in row[1].blocks] == ["b", "again"]


def test_tables_linear():
    rows = [[f"r{r}c{c}" for c in range(3)] for r in range(20_000)]
    p = table_doc(rows)
    t0 = time.monotonic()
    d, _ = parse(p)
    assert len(d.blocks[0].rows) == 20_000 and time.monotonic() - t0 < 10
    # one row of 100,000 cells and no TTP
    cell = u16(0) + sp(IN_TABLE, 1)
    q = doc_file([("x\x07" * 100_000, False)], papx=[(0, 200_000, cell)])
    t0 = time.monotonic()
    d, _ = parse(q)
    assert len(d.blocks[0].rows[0]) == 100_000 and time.monotonic() - t0 < 10
    # depth alternating between 1 and 40 at each paragraph: tables open and close linearly
    text = "p\r" * 4_000 + "\x07\x07"
    spans = [(2 * k, 2 * k + 2, u16(0) + sp(IN_TABLE, 1) + sp(ITAP, 40 if k % 2 else 1)) for k in range(4_000)]
    spans += [(8_000, 8_001, u16(0) + sp(IN_TABLE, 1)), (8_001, 8_002, u16(0) + sp(IN_TABLE, 1) + sp(TTP, 1))]
    t0 = time.monotonic()
    d, ctx = parse(doc_file([(text, False)], papx=spans))
    out = markdown.render(d, tmp() / "a", False, ctx.skipped)
    assert len(d.blocks) == 1 and time.monotonic() - t0 < 10 and not ctx.skipped, ctx.skipped
    assert out == "p\n\n" * 3_999 + "p\n", out[:200]


def test_depth_flip_table_budget():
    # depth flipping between 1 and MAX_DEPTH at each paragraph opens 63 tables a paragraph for one 3-byte sprm: past
    # TABLE_NODES tables none open, the paragraph stays in the innermost open cell and each overflowing one is
    # counted under depth; every paragraph is kept, and parse and render stay linear
    n = 20_000
    text = "p\r" * n + "\x07\x07"
    deep = [u16(0) + sp(IN_TABLE, 1) + sp(ITAP, itap) for itap in (1, limits.MAX_DEPTH)]
    spans = [(2 * k, 2 * k + 2, deep[k % 2]) for k in range(n)]
    spans += [(2 * n, 2 * n + 1, deep[0]), (2 * n + 1, 2 * n + 2, deep[0] + sp(TTP, 1))]
    p = doc_file([(text, False)], papx=spans)
    t0 = time.monotonic()
    d, ctx = parse(p)
    out = markdown.render(d, tmp() / "a", False, ctx.skipped)
    took = time.monotonic() - t0
    tables = paras = 0
    todo = list(d.blocks)
    while todo:
        b = todo.pop()
        if isinstance(b, Table):
            tables += 1
            todo.extend(x for row in b.rows for cell in row for x in cell.blocks)
        elif isinstance(b, Para):
            paras += 1
    assert took < 10, took
    assert ctx.skipped["depth"], ctx.skipped
    assert ctx.skipped["depth"] == n // 2 - (limits.TABLE_NODES - 1) // (limits.MAX_DEPTH - 1), ctx.skipped
    assert tables <= limits.TABLE_NODES and paras == n and "p" in out, (tables, paras)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
