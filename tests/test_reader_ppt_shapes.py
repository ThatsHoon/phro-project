"""ppt shapes: reading order, anchors, group transforms, tables, placeholders, nesting limits.

Run: python -I -X utf8 tests/test_reader_ppt_shapes.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown, officeart  # noqa: E402
from reader.formats import ppt_shapes  # noqa: E402
from reader.model import Code, Context, Table  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from reader_fix import (make_ppt, oa_container, oa_group, oa_record, oa_shape, oa_solver,  # noqa: E402
                        ppt_drawing, ppt_shape)


def parsed(slides, **kw):
    """Build a .ppt and parse it: (doc, ctx, assets directory)."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_ppt(path, slides, **kw)
    ctx = Context()
    return reader.parse(path, ctx), ctx, d / "assets"


def md(slides, **kw):
    """Build a .ppt, parse it, render it: (markdown, ctx)."""
    doc, ctx, assets = parsed(slides, **kw)
    return markdown.render(doc, assets, False, ctx.skipped), ctx


def text(t, box, **kw):
    return {"text": t, "box": box, **kw}


def walked(shapes):
    """ppt_shapes.walk over a PPDrawing of the make_ppt shape dicts: the top-level Shapes."""
    ids = [1025]
    data = ppt_drawing(*[ppt_shape(s, ids) for s in shapes])
    return ppt_shapes.walk(data, 0, len(data), Context())[0]


def test_textbox_order():
    out, _ = md([{"shapes": [text("lower", (100, 500, 900, 600)), text("upper", (100, 100, 900, 200))]}])
    assert 0 <= out.index("upper") < out.index("lower"), out
    out, _ = md([{"shapes": [text("right", (600, 100, 900, 200)), text("left", (100, 100, 400, 200))]}])
    assert out.index("left") < out.index("right"), out


def test_outline_ref():
    slide = {"title": "Head", "body": ["Body text"], "extra": [(1, "Tail text")], "outline": True,
             "shapes": [text("Top box", (0, 100, 500, 200)),
                        {"placeholder": 14, "ref": 1, "box": (0, 500, 500, 600)},
                        {"placeholder": 13, "ref": 0, "box": (0, 10, 500, 50)}]}
    out, _ = md([slide])
    assert "## Slide 1: Head" in out, out
    assert out.index("Top box") < out.index("Body text") < out.index("Tail text"), out
    for t in ("Head", "Top box", "Body text", "Tail text"):
        assert out.count(t) == 1, (t, out)


def test_title_placeholder():
    # a title placeholder whose text is not a kind 0 or 6 run still becomes the slide title
    out, _ = md([{"shapes": [text("Big idea", (0, 10, 500, 50), placeholder=15, text_kind=4),
                             text("detail", (0, 100, 500, 150))]}])
    assert "## Slide 1: Big idea" in out and out.count("Big idea") == 1 and "detail" in out, out


def test_footer_placeholders():
    slide = {"title": "T", "body": ["kept body"], "outline": True,
             "shapes": [text("date text", (0, 900, 100, 950), placeholder=7),
                        text("number text", (0, 900, 100, 950), placeholder=8),
                        text("footer text", (0, 900, 100, 950), placeholder=9),
                        text("header text", (0, 900, 100, 950), placeholder=10),
                        {"placeholder": 9, "ref": 1, "box": (0, 900, 100, 950)},
                        text("shown", (0, 100, 100, 150))]}
    out, _ = md([slide])
    assert out == "## Slide 1: T\n\nshown\n\nfooter text\n\nheader text\n\nkept body\n", repr(out)


def test_group_transform():
    group = {"kind": "group", "box": (0, 100, 1000, 1100), "rect": (0, 0, 100, 100), "client": True,
             "children": [text("A", (0, 50, 10, 60)), text("B", (0, 10, 10, 20))]}
    a, b = walked([group])[0].children
    assert (b.top, b.bottom, b.left, b.right) == (200, 300, 0, 100), (b.top, b.bottom, b.left, b.right)
    assert a.top == 600, a.top
    out, _ = md([{"shapes": [text("Cee", (0, 400, 100, 450)), group]}])
    assert out.index("B") < out.index("A") < out.index("Cee"), out
    # flipped vertically, the children swap places
    a, b = walked([dict(group, flags=0x80)])[0].children
    assert a.top == 500 and b.top == 900, (a.top, b.top)


def test_nested_group_transform():
    inner = {"kind": "group", "box": (0, 0, 50, 50), "rect": (0, 0, 10, 10),
             "children": [text("deep", (0, 5, 10, 6))]}
    outer = {"kind": "group", "box": (100, 100, 300, 300), "rect": (0, 0, 100, 100), "client": True,
             "children": [inner]}
    leaf = walked([outer])[0].children[0].children[0]
    # inner maps y 5 -> 25 in outer's space, outer maps 25 -> 100 + 25 * 2
    assert leaf.top == 150 and leaf.left == 100, (leaf.top, leaf.left)


def table(children, **kw):
    return {"kind": "group", "table": True, "box": (0, 0, 200, 100), "rect": (0, 0, 200, 100), "client": True,
            "children": children, **kw}


def test_table_group():
    cells = [text("a1", (0, 0, 100, 50)), text("b1", (100, 0, 200, 50)),
             text("c1", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    out, _ = md([{"shapes": [table(cells)]}])
    assert "|a1|b1|" in out and "|c1|d1|" in out, out
    shape = walked([table(cells)])[0]
    assert shape.kind == "table" and isinstance(shape.blocks[0], Table), shape.kind


def test_table_flag_in_tertiary_fopt():
    cells = [text("a1", (0, 0, 100, 50)), text("b1", (100, 0, 200, 50)),
             text("c1", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    shape = walked([table(cells, table="tertiary")])[0]
    assert shape.kind == "table" and len(shape.blocks[0].rows) == 2, shape.kind
    out, _ = md([{"shapes": [table(cells, table="tertiary")]}])
    assert "|a1|b1|" in out and "|c1|d1|" in out, out


def test_table_spans():
    cells = [text("wide", (0, 0, 200, 50)), text("c1", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    t = walked([table(cells)])[0].blocks[0]
    assert len(t.rows) == 2 and len(t.rows[0]) == 1 and t.rows[0][0].colspan == 2, t.rows
    assert len(t.rows[1]) == 2


def test_table_overlap_flat():
    cells = [text("left-x", (0, 0, 120, 50)), text("right-y", (100, 0, 200, 50))]
    out, ctx = md([{"shapes": [table(cells)]}])
    assert "left-x" in out and "right-y" in out and "|" not in out, out
    assert out.index("left-x") < out.index("right-y")
    assert walked([table(cells)])[0].kind == "group"


def test_table_budget_flat():
    cells = [text("a1", (0, 0, 100, 50)), text("b1", (100, 0, 200, 50)),
             text("c1", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    old = limits.TABLE_NODES
    limits.TABLE_NODES = 3
    try:
        out, ctx = md([{"shapes": [table(cells)]}])
    finally:
        limits.TABLE_NODES = old
    for t in ("a1", "b1", "c1", "d1"):
        assert t in out, (t, out)
    assert "|" not in out and ctx.skipped["cells"] == 1, (out, ctx.skipped)


def test_group_depth():
    inner = oa_group(3000, ppt_shape(text("deepest", (0, 0, 10, 10)), [3001]))
    for i in range(40):
        inner = oa_group(3002 + i, inner)
    out, ctx = md([{"shapes": [text("ok text", (0, 0, 10, 10))]}], raw_shapes={1: inner})
    assert "ok text" in out, out
    assert ctx.skipped["depth"] >= 1 and "place nested too deeply" in out, (out, ctx.skipped)
    assert "deepest" not in out


def test_anchor_16_bytes():
    narrow = walked([text("n", (10, 20, 300, 80))])[0]
    wide = walked([text("w", (10, 20, 300, 80), wide=True)])[0]
    assert (narrow.top, narrow.left, narrow.bottom, narrow.right) == (wide.top, wide.left, wide.bottom, wide.right)
    assert (wide.top, wide.left, wide.bottom, wide.right) == (20, 10, 80, 300)
    out, _ = md([{"shapes": [text("second", (0, 500, 100, 550), wide=True), text("first", (0, 100, 100, 150))]}])
    assert out.index("first") < out.index("second"), out


def test_hyperlink_wraps_its_text():
    url = "http://example.com/target"
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    info = make_ppt(path, [{"shapes": [text("click here", (0, 100, 100, 150), link=url)]}])
    assert url.encode("utf-16le") in info["doc_stream"], "the fixture holds no URL"
    ctx = Context()
    out = markdown.render(reader.parse(path, ctx), d / "assets", False, ctx.skipped)
    assert out == f"## Slide 1\n\n[click here]({url})\n", out
    assert not ctx.skipped, ctx.skipped


def slide_with_refs(*refs):
    """A slide whose outline holds two runs (Alpha body, Beta tail) and whose shapes refer to the given indexes."""
    return {"title": "Head", "body": ["Alpha body"], "extra": [(1, "Beta tail")], "outline": True,
            "shapes": [{"placeholder": 14, "ref": r, "box": (0, 100 + 100 * k, 500, 150 + 100 * k)}
                       for k, r in enumerate(refs)]}


def test_bad_or_repeated_outline_ref_follows_unreferenced():
    # the title run is index 0, Alpha body 1, Beta tail 2. A reference outside the list and a repeated one are ignored,
    # uncounted; the blocks they did not take follow the shapes, so no text is lost
    out, ctx = md([slide_with_refs(9, 1, 1)])
    for t in ("Head", "Alpha body", "Beta tail"):
        assert out.count(t) == 1, (t, out)
    assert out.index("Alpha body") < out.index("Beta tail"), out
    assert not ctx.skipped, ctx.skipped
    out, _ = md([slide_with_refs(2, 2)])  # the second run comes first, the unreferenced ones after
    assert out.index("Beta tail") < out.index("Alpha body"), out
    assert out.count("Beta tail") == 1 and out.count("Alpha body") == 1, out


def test_unanchored_shapes_keep_their_record_place():
    shapes = [{"text": "free one"}, text("low", (0, 500, 100, 550)), {"text": "free two"},
              text("high", (0, 100, 100, 150))]
    out, _ = md([{"shapes": shapes}])
    assert out.index("free one") < out.index("free two") < out.index("high") < out.index("low"), out


def test_unanchored_member_does_not_pull_group_union():
    group = {"kind": "group", "children": [{"text": "free member"}, text("g low", (0, 500, 100, 550)),
                                           text("g high", (0, 400, 100, 450))]}
    sibling = text("sibling", (0, 300, 100, 350))
    shapes = ppt_shapes.ordered(walked([group, sibling]))
    union = next(s for s in shapes if s.kind == "group")
    assert (union.top, union.left, union.bottom, union.right) == (400, 0, 550, 100), vars(union)
    out, _ = md([{"shapes": [group, sibling]}])
    assert out.index("sibling") < out.index("free member") < out.index("g high") < out.index("g low"), out


def test_table_edges_within_tolerance_are_one_line():
    # the edges at 100 / 101 / 103 and 50 / 52 are one grid line each: a 2 x 2 table, no empty spanned column or row
    cells = [text("a1", (0, 0, 100, 50)), text("b1", (101, 0, 200, 50)),
             text("c1", (0, 52, 103, 100)), text("d1", (103, 50, 200, 100))]
    t = walked([table(cells)])[0].blocks[0]
    assert [len(r) for r in t.rows] == [2, 2] and all(c.colspan == c.rowspan == 1 for r in t.rows for c in r), t.rows
    far = [text("a1", (0, 0, 100, 50)), text("b1", (110, 0, 200, 50)),
           text("c1", (0, 50, 100, 100)), text("d1", (110, 50, 200, 100))]
    assert len(walked([table(far)])[0].blocks[0].rows[0]) == 3  # 10 units apart: two lines, an empty column between


def test_zero_size_fspgr_keeps_parent_coordinates():
    group = {"kind": "group", "box": (100, 100, 300, 300), "rect": (5, 5, 5, 5), "client": True,
             "children": [text("kid", (10, 20, 30, 40))]}
    kid = walked([group])[0].children[0]
    assert (kid.left, kid.top, kid.right, kid.bottom) == (10, 20, 30, 40), vars(kid)
    out, ctx = md([{"shapes": [group]}])
    assert "kid" in out and not ctx.skipped, (out, ctx.skipped)


def test_table_with_empty_cells():
    cells = [text("a1", (0, 0, 100, 50)), {"box": (100, 0, 200, 50)},
             text("", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    t = walked([table(cells)])[0].blocks[0]
    assert [[bool(c.blocks) for c in r] for r in t.rows] == [[True, False], [False, True]], t.rows
    out, _ = md([{"shapes": [table(cells)]}])
    assert "|a1|" in out and "d1|" in out, out


def test_placeholder_5_text_left_out_of_notes():
    notes = {1: {"shapes": [text("image words", (0, 10, 100, 50), placeholder=5, text_kind=2),
                            text("spoken words", (0, 100, 100, 150), placeholder=6, text_kind=2)]}}
    out, ctx = md([{"title": "T"}], notes=notes)
    assert "spoken words" in out and "image words" not in out and not ctx.skipped, (out, ctx.skipped)


# ---- connectors
END_ARROW = struct.pack("<HI", 0x01D1, 1)  # FOPT lineEndArrowhead: an arrowhead at the end


def mermaid(doc):
    return "\n".join(b.text for sec in doc.blocks for b in sec.blocks if isinstance(b, Code) and b.lang == "mermaid")


def test_rules_walk():
    dg = oa_container(0xF002, oa_group(1024, oa_shape(2), oa_shape(3), oa_shape(4, inst=32)), oa_solver((2, 3, 4)))
    assert officeart.rules(dg, 8, len(dg)) == [(2, 3, 4)], officeart.rules(dg, 8, len(dg))
    # a rule shorter than its 24 bytes is left out; a solver cut by the end of the range is not read
    short = oa_record(0xF012, struct.pack("<5I", 9, 5, 6, 7, 0), ver=1)
    solver = oa_container(0xF005, short, oa_solver((2, 3, 4))[8:])
    assert officeart.rules(solver, 0, len(solver)) == [(2, 3, 4)]
    assert officeart.rules(solver, 0, len(solver) - 1) == []
    two = oa_solver((2, 3, 4), (5, 6, 7))
    assert officeart.rules(two[:-4], 0, len(two)) == []
    # a rule whose own length passes its solver ends that solver's walk
    long = oa_container(0xF005, oa_solver((2, 3, 4))[8:], struct.pack("<HHI", 0x10, 0xF012, 0x100) + bytes(24))
    assert officeart.rules(long, 0, len(long)) == [(2, 3, 4)]
    # rules outside a SolverContainer are not read; the walk visits at most MAX_RECORDS records
    assert officeart.rules(oa_solver((2, 3, 4))[8:], 0, 32) == []
    many = oa_solver(*[(k, k + 1, k + 2) for k in range(10)])
    old = officeart.MAX_RECORDS
    officeart.MAX_RECORDS = 4
    try:
        assert officeart.rules(many, 0, len(many)) == [(0, 1, 2), (1, 2, 3), (2, 3, 4)]
    finally:
        officeart.MAX_RECORDS = old


def test_fsp_flags_and_arrows():
    sp = oa_shape(4, inst=32, flags=0xA40, props=END_ARROW)
    (_, inst, typ, b, s), (_, finst, ftyp, fb, fs) = officeart.records(sp, 8, len(sp))
    assert typ == officeart.FSP and inst in officeart.CONNECTOR_TYPES and ftyp == officeart.FOPT
    spid, flags = officeart.fsp(sp[b:b + s])
    assert spid == 4 and flags & officeart.FLIP_H and not flags & officeart.FLIP_V, (spid, flags)
    assert officeart.arrows(sp, finst, fb, fs) == (False, True)
    both = oa_shape(5, pib=3, props=struct.pack("<HIHI", 0x01D0, 2, 0x01D1, 0))
    _, (_, finst, _, fb, fs) = officeart.records(both, 8, len(both))
    assert officeart.arrows(both, finst, fb, fs) == (True, False)
    assert officeart.arrows(both, 0, fb, fs) == (False, False)  # no property: no arrowhead
    assert officeart.fsp(b"\x05\x00") == (0, 0) and officeart.fsp(struct.pack("<I", 7)) == (7, 0)
    assert officeart.KINDS[110] == "decision" and officeart.LINE_TYPE == 20 and 40 in officeart.CONNECTOR_TYPES


def flow(far=False, rule=(2, 3, 4), **connector):
    """A slide: a title, Start (spid 2) above End (spid 3) and connector spid 4 between them (far off when `far`),
    with make_ppt's keywords for a solver of `rule`."""
    box = (600, 600, 700, 700) if far else (200, 200, 200, 400)
    shapes = [text("Deck title", (0, 10, 500, 50), placeholder=13, text_kind=0),
              text("Start", (100, 100, 300, 200), spid=2), text("End", (100, 400, 300, 500), spid=3),
              {"spid": 4, "inst": 32, "props": END_ARROW, "box": box, **connector}]
    return [{"shapes": shapes}], {"raw_shapes": {1: oa_solver(rule)}} if rule else {}


def test_ppt_connector_mermaid():
    slides, kw = flow(far=True)
    doc, ctx, assets = parsed(slides, **kw)
    t = mermaid(doc)
    assert mermaid_ok(t) and t == 'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 --> n2', t
    out = markdown.render(doc, assets, False, ctx.skipped)
    assert out.startswith("## Slide 1: Deck title\n\n```mermaid\n"), out
    for word in ("Start", "End", "Deck title"):
        assert out.count(word) == 1, (word, out)
    assert doc.blocks[0].title == "Deck title" and not ctx.skipped, ctx.skipped
    # a rule naming a missing shape is no glue: far from every shape the connector stays loose
    slides, kw = flow(far=True, rule=(2, 99, 4))
    out, ctx = md(slides, **kw)
    assert "mermaid" not in out and ctx.skipped["connector_loose"] == 1, (out, ctx.skipped)
    # a line (type 20) glued by a rule is an edge too, labelled by its text
    slides, kw = flow(far=True, inst=20, text="go")
    doc, ctx, _ = parsed(slides, **kw)
    t = mermaid(doc)
    assert mermaid_ok(t) and t.endswith('  n1 -->|"go"| n2'), t


def test_ppt_kinds_from_shape_type():
    slides, kw = flow()
    slides[0]["shapes"][1]["inst"] = 110
    slides[0]["shapes"][2]["inst"] = 116
    doc, _, _ = parsed(slides, **kw)
    t = mermaid(doc)
    assert mermaid_ok(t) and '  n1{"Start"}' in t and '  n2(["End"])' in t, t


def test_ppt_place_failure_keeps_the_flow():
    from reader import diagram

    def boom(self, slots):
        raise ValueError("bad")
    slides, kw = flow()
    real = diagram.Diagram.place
    diagram.Diagram.place = boom
    try:
        out, ctx = md(slides, **kw)
    finally:
        diagram.Diagram.place = real
    assert out.endswith("## Slide 1: Deck title\n\nStart\n\nEnd\n") and ctx.skipped["shape"] == 1, (out, ctx.skipped)


def snap_group(**connector):
    """A Decoy at the top level where unmapped ends would fall, then a group mapping (0, 0, 100, 20) onto
    (0, 100)-(1000, 300): A on the left, B on the right and a straight connector from (10, 10) to (90, 10) in its
    ChildAnchor."""
    return [text("Decoy", (0, 0, 30, 30)),
            {"kind": "group", "box": (0, 100, 1000, 300), "rect": (0, 0, 100, 20), "client": True,
             "children": [text("A", (0, 0, 20, 20)), text("B", (80, 0, 100, 20)),
                          {"inst": 32, "props": END_ARROW, "box": (10, 10, 90, 10), **connector}]}]


def test_ppt_child_anchor_snaps():
    con = walked(snap_group())[1].children[2]
    assert con.ends == ((100, 200), (900, 200)), con.ends
    assert walked(snap_group(flags=0x40))[1].children[2].ends == ((900, 200), (100, 200))
    for flags, edge in ((0, "n1 --> n2"), (0x40, "n2 --> n1")):  # flipped, the connector starts at its right corner
        doc, ctx, assets = parsed([{"shapes": snap_group(flags=flags)}])
        t = mermaid(doc)
        assert mermaid_ok(t) and t == f'flowchart LR\n  n1["A"]\n  n2["B"]\n  %% inferred from position\n  {edge}', t
        out = markdown.render(doc, assets, False, ctx.skipped)
        assert out.startswith("## Slide 1\n\nDecoy\n\n```mermaid\n") and not ctx.skipped, (out, ctx.skipped)
    # a flipped group mirrors its members, the connector's ends with them
    con = walked([dict(snap_group()[1], flags=0x40)])[0].children[2]
    assert con.ends == ((900, 200), (100, 200)), con.ends


def _plain_slides():
    """Decks with no solver and no connector: fixtures above, plus decision and terminator shapes and an arrowless
    line."""
    cells = [text("a1", (0, 0, 100, 50)), text("b1", (100, 0, 200, 50)),
             text("c1", (0, 50, 100, 100)), text("d1", (100, 50, 200, 100))]
    group = {"kind": "group", "box": (0, 100, 1000, 1100), "rect": (0, 0, 100, 100), "client": True,
             "children": [text("A", (0, 50, 10, 60)), text("B", (0, 10, 10, 20))]}
    return [
        [{"shapes": [text("lower", (100, 500, 900, 600)), text("upper", (100, 100, 900, 200))]}],
        [{"title": "Head", "body": ["Body text"], "extra": [(1, "Tail text")], "outline": True,
          "shapes": [text("Top box", (0, 100, 500, 200)), {"placeholder": 14, "ref": 1, "box": (0, 500, 500, 600)},
                     {"placeholder": 13, "ref": 0, "box": (0, 10, 500, 50)}]}],
        [{"shapes": [text("Big idea", (0, 10, 500, 50), placeholder=15, text_kind=4),
                     text("detail", (0, 100, 500, 150))]}],
        [{"shapes": [text("Cee", (0, 400, 100, 450)), group]}],
        [{"shapes": [table(cells)]}],
        [{"shapes": [{"text": "free one"}, text("low", (0, 500, 100, 550)), {"text": "free two"}]}],
        [{"title": "T", "body": ["kept body"]}, {"title": "U", "body": ["more"], "hidden": True}],
        [{"shapes": [text("Choice", (0, 0, 100, 100), inst=110), text("rule", (0, 200, 500, 200), inst=20),
                     text("Done", (0, 300, 100, 400), inst=116)]}],
    ]


_PLAIN = [  # what the reader wrote for these decks before it read connectors
    '## Slide 1\n\nupper\n\nlower\n',
    '## Slide 1: Head\n\nTop box\n\nBody text\n\nTail text\n',
    '## Slide 1: Big idea\n\ndetail\n',
    '## Slide 1\n\nB\n\nA\n\nCee\n',
    '## Slide 1\n\n|a1|b1|\n|---|---|\n|c1|d1|\n',
    '## Slide 1\n\nfree one\n\nlow\n\nfree two\n',
    '(1 hidden places left out)\n\n## Slide 1: T\n\nkept body\n',
    '## Slide 1\n\nChoice\n\nrule\n\nDone\n',
]


def test_ppt_without_solver_unchanged():
    got = [md(s)[0] for s in _plain_slides()]
    assert got == _PLAIN, got


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
