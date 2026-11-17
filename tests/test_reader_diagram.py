"""The shared diagram module: glued edges as Mermaid, labels and escapes, node IDs and placement, follow-ups, trees as
lists, containers as subgraphs, the connection budget, and the two new skip kinds' wording.

Run: python -I -X utf8 tests/test_reader_diagram.py
"""
import collections
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from diagram_fix import mermaid_ok  # noqa: E402
from reader import diagram, limits, markdown  # noqa: E402
from reader.diagram import ConnectionBudget, Diagram  # noqa: E402
from reader.formats import smartart  # noqa: E402
from reader.model import (  # noqa: E402
    Break, Cell, Code, Context, Doc, Image, Link, ListItem, Para, Table, Text,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def new():
    ctx = Context()
    return Diagram(ctx, ConnectionBudget(ctx)), ctx


def P(s, **kw):
    return [Para([Text(s)], **kw)]


def text(blocks):
    return "\n".join(b.text for b in blocks if isinstance(b, Code))


def lines(blocks):
    t = text(blocks)
    assert t and mermaid_ok(t), t
    assert all(b.lang == "mermaid" for b in blocks if isinstance(b, Code)), blocks
    return t.split("\n")


def flat(slots):
    return [b for _, bl in slots for b in bl]


def chart(edges, names=None, **kw):
    """A diagram of shapes named by their keys and connectors k0, k1, ... for (begin, end) edges; the slots are the
    shapes in first-seen order, then the connectors. Returns place()'s output."""
    d, _ = new()
    names = names or []
    for a, b in edges:
        for k in (a, b):
            if k not in names:
                names.append(k)
    for k in names:
        d.shape(k, P(k))
    for i, (a, b) in enumerate(edges):
        d.connector(f"k{i}", begin=a, end=b, **kw)
    return d.place([(k, P(k)) for k in names] + [(f"k{i}", []) for i in range(len(edges))])


def test_no_edges_returns_slots():
    d, _ = new()
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    slots = [("A", P("A")), (None, P("t")), ("B", P("B"))]
    assert d.place(slots) == flat(slots)


def test_glued_edge_mermaid():
    d, _ = new()
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    d.connector("c", begin="A", end="B", arrows=(False, True))
    out = d.place([("A", P("A")), ("B", P("B")), ("c", [])])
    assert len(out) == 1 and isinstance(out[0], Code) and out[0].lang == "mermaid", out
    assert lines(out) == ["flowchart TD", '  n1["A"]', '  n2["B"]', "  n1 --> n2"], out


def test_arrows_four_cases_and_unknown():
    cases = {(False, True): "n1 --> n2", (True, False): "n2 --> n1", (True, True): "n1 <--> n2",
             (False, False): "n1 --- n2", (None, None): "n1 --> n2", (None, True): "n1 --> n2",
             (True, None): "n2 --> n1", (None, False): "n1 --- n2", (False, None): "n1 --- n2"}
    for arrows, want in cases.items():
        got = lines(chart([("A", "B")], arrows=arrows))[-1]
        assert got == "  " + want, (arrows, got)


def test_edge_label():
    d, _ = new()
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    d.connector("c", begin="A", end="B", label="yes")
    out = d.place([("A", P("A")), ("c", P("yes")), ("B", P("B"))])
    assert len(out) == 1, out  # the connector's slot is gone with its shapes'
    assert lines(out)[-1] == '  n1 -->|"yes"| n2', out


def test_label_escapes():
    d, _ = new()
    d.shape("A", [Para([Text('say "hi"'), Break(), Text("now")])])
    d.shape("B", P("x\r\ny\rz"))
    d.connector("c", begin="A", end="B", label='a "b"\nc')
    ls = lines(d.place([("A", []), ("B", []), ("c", [])]))
    assert ls[1] == '  n1["say #quot;hi#quot;<br>now"]', ls
    assert ls[2] == '  n2["x<br>y<br>z"]', ls
    assert ls[3] == '  n1 -->|"a #quot;b#quot;<br>c"| n2', ls
    odd = ["a|b", "%% c", "end", "x --> y"]
    out = chart([(odd[0], odd[1]), (odd[2], odd[3])], label="end|%% -->")
    t = "\n".join(lines(out))
    for s in odd:
        assert f'["{s}"]' in t, (s, t)
    assert t.count('-->|"end|%% -->"|') == 2, t


def test_backtick_label_fence():
    out = chart([("```x", "B")])
    md = markdown.render(Doc(blocks=out), Path(tempfile.mkdtemp()))
    assert '  n1["```x"]' in md, md
    body = md.strip().split("\n")
    assert body[0] == "````mermaid" and body[-1] == "````", md


def test_kinds():
    d, _ = new()
    for k, kind in (("D", "decision"), ("T", "terminator"), ("C", "circle"), ("S", "store"), ("W", "weird"),
                    ("R", "")):
        d.shape(k, P(k), kind=kind)
    for i, (a, b) in enumerate((("D", "T"), ("C", "S"), ("W", "R"))):
        d.connector(f"k{i}", begin=a, end=b)
    ls = lines(d.place([(k, []) for k in "DTCSWR"]))
    assert ls[1:7] == ['  n1{"D"}', '  n2(["T"])', '  n3(("C"))', '  n4[("S")]', '  n5["W"]', '  n6["R"]'], ls


def test_direction_lr_and_td():
    def head(box_a, box_b):
        d, _ = new()
        d.shape("A", P("A"), box=box_a)
        d.shape("B", P("B"), box=box_b)
        d.connector("c", begin="A", end="B")
        return lines(d.place([("A", []), ("B", [])]))[0]
    assert head((0, 0, 10, 10), (100, 0, 110, 10)) == "flowchart LR"
    assert head((0, 0, 10, 10), (110, 10, 100, 0)) == "flowchart LR"  # a box given max-first is normalised
    assert head((0, 0, 10, 10), (0, 100, 10, 110)) == "flowchart TD"
    assert head((0, 0, 10, 10), (50, 50, 60, 60)) == "flowchart TD"  # equal sums
    assert head(None, None) == "flowchart TD"
    assert head((0, 0, 10, 10), None) == "flowchart TD"  # only edges with two boxes count
    assert head((0, 0, 10, 10), (float("nan"), 0, 1e300, 10)) == "flowchart TD"  # bad coordinates: no box


def test_node_ids_follow_slots():
    d, _ = new()
    d.shape("Z", P("Z"))  # registered first, but has no slot: numbered after every slotted node
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    d.connector("c1", begin="A", end="B")
    d.connector("c2", begin="B", end="Z")
    ls = lines(d.place([("B", P("B")), ("A", P("A"))]))
    assert ls == ["flowchart TD", '  n1["B"]', '  n2["A"]', '  n3["Z"]', "  n2 --> n1", "  n1 --> n3"], ls


def test_block_at_first_connected_slot():
    d, _ = new()
    for k in "XAB":
        d.shape(k, P(k))
    d.connector("c", begin="A", end="B")
    out = d.place([(None, P("title")), ("X", P("X")), ("A", P("A")), ("B", P("B")), ("c", [])])
    assert out[:2] == P("title") + P("X") and len(out) == 3 and isinstance(out[2], Code), out
    d, _ = new()  # no slot is part of the diagram: it goes at the end
    for k in "AB":
        d.shape(k, P(k))
    d.connector("c", begin="A", end="B")
    out = d.place([(None, P("title"))])
    assert out[0] == P("title")[0] and isinstance(out[1], Code), out


def test_long_text_follow_up():
    d, _ = new()
    long = "a" * 100
    d.shape("A", P(long))
    d.shape("B", [Para([Text("head"), Break(), Text("b" * 90)])])
    d.shape("C", P("c" * 80))  # exactly NAME: still short
    d.connector("k1", begin="A", end="B")
    d.connector("k2", begin="B", end="C")
    out = d.place([("A", P(long)), ("B", []), ("C", [])])
    ls = lines(out)
    assert ls[1] == '  n1["' + "a" * 80 + '…"]', ls
    assert ls[2] == '  n2["head…"]', ls
    assert ls[3] == '  n3["' + "c" * 80 + '"]', ls
    assert out[1:] == [Para([Text("n1:")])] + P(long) + [Para([Text("n2:")]), Para([Text("head"), Break(),
                                                                                     Text("b" * 90)])], out


def test_rich_node_follow_up():
    pic = [Image(PNG)]
    tab = [Table([[Cell(P("t"))]])]
    cht = [Table([[Cell(P("1"))]], caption="Chart: sales")]
    titled = [Para([Text("Title")]), Image(PNG)]
    linked = [Para([Link("http://x", [Text("L")])])]
    d, _ = new()
    for k, blocks in (("A", pic), ("B", tab), ("C", cht), ("D", titled), ("E", linked)):
        d.shape(k, blocks)
    for i, (a, b) in enumerate((("A", "B"), ("C", "D"), ("D", "E"))):
        d.connector(f"k{i}", begin=a, end=b)
    out = d.place([(k, []) for k in "ABCDE"])
    ls = lines(out)
    assert ls[1:6] == ['  n1["(picture)"]', '  n2["(table)"]', '  n3["(chart)"]', '  n4["Title…"]',
                       '  n5["L…"]'], ls
    assert out[1:] == ([Para([Text("n1:")])] + pic + [Para([Text("n2:")])] + tab + [Para([Text("n3:")])] + cht +
                       [Para([Text("n4:")])] + titled + [Para([Text("n5:")])] + linked), out


def test_connector_blocks_label_and_follow_up():
    two = [Para([Text("yes")]), Para([Text("  now "), Break(), Text("then")])]  # plain and short: one line
    pic = [Image(PNG)]
    rich = [Para([Text("see "), Link("http://x", [Text("here")])])]
    long = P("y" * 90)
    d, _ = new()
    for k in "ABCDE":
        d.shape(k, P(k))
    d.connector("k1", begin="A", end="B", blocks=two)
    d.connector("k2", begin="B", end="C", blocks=pic)
    d.connector("k3", begin="D", end="C", arrows=(True, False), blocks=rich)  # drawn C to D
    d.connector("k4", begin="D", end="E", blocks=long)
    out = d.place([(k, []) for k in "ABCDE"] + [("k1", two), ("k2", pic), ("k3", rich), ("k4", long)])
    ls = lines(out)
    assert ls[6:] == ['  n1 -->|"yes now then"| n2', '  n2 -->|"(picture)"| n3', '  n3 -->|"see here…"| n4',
                      '  n4 -->|"' + "y" * 80 + '…"| n5'], ls
    assert out[1:] == ([Para([Text("n2 --> n3:")])] + pic + [Para([Text("n3 --> n4:")])] + rich +
                       [Para([Text("n4 --> n5:")])] + long), out  # each slot taken out, its blocks once
    # an inferred edge carries its blocks the same way; a loose connector keeps its slot and its blocks stay there
    d, ctx = new()
    d.shape("A", P("A"), box=(0, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    d.connector("k1", p0=(50, 50), p1=(250, 350), blocks=pic)
    d.connector("k2", begin="A", p1=(999, 999), blocks=long)
    out = d.place([("A", []), ("B", []), ("k1", pic), ("k2", long)])
    assert lines(out)[-1] == '  n1 -->|"(picture)"| n2', out
    assert out[1:] == [Para([Text("n1 --> n2:")])] + pic + long and ctx.skipped == {"connector_loose": 1}, out


def test_hidden_end_blank_node():
    d, _ = new()
    secret = P("secret", hidden="hidden")
    d.shape("A", P("A"))
    d.shape("B", secret, hidden=True)
    d.shape("E", [])  # no text: a blank node too, and nothing in its slot to keep
    d.connector("k1", begin="A", end="B")
    d.connector("k2", begin="A", end="E", arrows=(False, False))  # undirected: Mermaid, not a tree list
    out = d.place([("A", P("A")), ("B", secret), ("E", []), ("k1", []), ("k2", [])])
    ls = lines(out)
    assert ls[1:] == ['  n1["A"]', "  n1 --> n2", "  n1 --- n3"], ls  # blank nodes: in their edges only
    assert "secret" not in "\n".join(ls)
    assert out[1:] == secret, out  # the hidden shape's slot stays where it was, after the diagram


def test_blank_node_only_in_its_edges():
    # a blank node outside every subgraph is not declared: Mermaid makes it from its first edge
    d, _ = new()
    d.shape("A", P("A"))
    d.shape("B", [])
    d.connector("k", begin="A", end="B")
    assert lines(d.place([("A", []), ("B", [])])) == ["flowchart TD", '  n1["A"]', "  n1 --> n2"]
    # inside a subgraph it is declared, so it is placed there
    d, _ = new()
    d.shape("C", P("Lane"), box=(0, 0, 100, 100), container=True, title="Lane")
    d.shape("A", P("A"), box=(10, 10, 20, 20))
    d.shape("B", [], box=(50, 50, 60, 60))
    d.connector("k", begin="A", end="B")
    assert lines(d.place([("C", []), ("A", []), ("B", [])])) == [
        "flowchart TD", '  subgraph c1 ["Lane"]', '    n1["A"]', '    n2[" "]', "  end", "  n1 --> n2"]


def test_hidden_connector_no_edge():
    d, _ = new()
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    d.connector("c", begin="A", end="B", hidden=True)
    slots = [("A", P("A")), ("c", P("x", hidden="hidden")), ("B", P("B"))]
    assert d.place(slots) == flat(slots)


def test_self_loop_kept():
    d, _ = new()
    d.shape("A", P("A"))
    d.connector("c", begin="A", end="A")
    assert lines(d.place([("A", P("A")), ("c", [])])) == ["flowchart TD", '  n1["A"]', "  n1 --> n1"]


def test_bad_glue_is_unglued():
    d, ctx = new()
    d.shape("A", P("A"))
    d.connector("c1", begin="A", end="missing")
    d.connector("c2", begin="A", end="c2")  # itself
    d.connector("c3", begin="c1", end="A")  # another connector
    d.connector("c4", begin="A", end=None)
    slots = [("A", P("A")), ("c1", P("1")), ("c2", P("2")), ("c3", P("3")), ("c4", P("4"))]
    assert d.place(slots) == flat(slots)  # loose ends with no point to place: no edge, every slot kept
    assert ctx.skipped == {"connector_loose": 4}, ctx.skipped
    d, ctx = new()  # bad glue on an end with a point: that end is placed by position
    d.shape("A", P("A"), box=(0, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    d.connector("c", begin="missing", end="A", p0=(250, 350), p1=(999, 999))
    ls = lines(d.place([("A", []), ("B", []), ("c", [])]))
    assert ls[-2:] == ["  %% inferred from position", "  n2 --> n1"], ls
    assert not ctx.skipped, ctx.skipped


def scope(*conns, extra=()):
    """A (0,0)-(100,100) and B (200,300)-(300,400): a 300 x 400 scope, diagonal 500, tol 10. Each conn is a
    connector() keyword dict, keyed k0, k1, ...; `extra` are more (key, box) shapes. Returns (place() output, ctx)."""
    d, ctx = new()
    d.shape("A", P("A"), box=(0, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    for k, box in extra:
        d.shape(k, P(k), box=box)
    for i, kw in enumerate(conns):
        d.connector(f"k{i}", **kw)
    slots = ([("A", P("A")), ("B", P("B"))] + [(k, P(k)) for k, _ in extra] +
             [(f"k{i}", P(f"t{i}")) for i in range(len(conns))])
    return d.place(slots), ctx


def test_infer_inside_box():
    d, ctx = new()
    d.shape("A", P("A"), box=(0, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    d.shape("C", P("C"), box=(0, 300, 100, 400))
    d.connector("k1", p0=(50, 50), p1=(250, 350), arrows=(False, True))  # registered first, written after
    d.connector("k2", begin="C", end="A")
    out = d.place([("A", P("A")), ("B", P("B")), ("C", P("C")), ("k1", P("t1")), ("k2", [])])
    assert lines(out) == ["flowchart TD", '  n1["A"]', '  n2["B"]', '  n3["C"]', "  n3 --> n1",
                          "  %% inferred from position", "  n1 --> n2"], out
    assert len(out) == 1 and not ctx.skipped, (out, ctx.skipped)  # the inferred connector's slot is taken out


def test_infer_nested_smallest():
    out, ctx = scope({"p0": (20, 20), "p1": (250, 350)}, extra=[("I", (10, 10, 50, 50))])
    assert lines(out)[1:] == ['  n1["B"]', '  n2["I"]', "  %% inferred from position", "  n2 --> n1"], out
    assert out[:1] == P("A") and len(out) == 2 and not ctx.skipped, (out, ctx.skipped)  # I wins, A stays a shape


def test_infer_within_tol():
    out, ctx = scope({"p0": (100 + 0.019 * 500, 50), "p1": (250, 350)})
    assert lines(out)[-1] == "  n1 --> n2" and not ctx.skipped, (out, ctx.skipped)
    out, ctx = scope({"p0": (100 + 0.021 * 500, 50), "p1": (250, 350)})
    assert out == P("A") + P("B") + P("t0"), out
    assert ctx.skipped == {"connector_loose": 1}, ctx.skipped


def test_infer_exactly_at_tol():
    out, ctx = scope({"p0": (110, 50), "p1": (250, 350)})  # 10.0 from A's right edge, and tol is 10.0
    assert lines(out)[-1] == "  n1 --> n2" and not ctx.skipped, (out, ctx.skipped)


def test_infer_tie_unresolved():
    # D and E are 2 either side of (150, 50): a tie in distance, within tol
    out, ctx = scope({"p0": (150, 50), "p1": (250, 350)}, extra=[("D", (120, 0, 148, 100)),
                                                                 ("E", (152, 0, 180, 100))])
    assert out == P("A") + P("B") + P("D") + P("E") + P("t0"), out
    assert ctx.skipped == {"connector_loose": 1}, ctx.skipped
    # F and G both hold the point and have equal areas: a tie inside
    out, ctx = scope({"p0": (150, 150), "p1": (250, 350)}, extra=[("F", (140, 140, 160, 170)),
                                                                   ("G", (130, 145, 170, 160))])
    assert out == P("A") + P("B") + P("F") + P("G") + P("t0"), out
    assert ctx.skipped == {"connector_loose": 1}, ctx.skipped


def test_infer_same_shape_both_ends():
    out, ctx = scope({"p0": (10, 10), "p1": (90, 90)}, {"begin": "A", "p1": (50, 50)})
    assert out == P("A") + P("B") + P("t0") + P("t1"), out  # never inferred into a self-loop
    assert ctx.skipped == {"connector_loose": 2}, ctx.skipped


def test_one_end_glued_other_inferred():
    out, ctx = scope({"begin": "A", "p0": (250, 350), "p1": (250, 350)})  # the glued end keeps its glue
    assert lines(out)[-2:] == ["  %% inferred from position", "  n1 --> n2"], out
    assert not ctx.skipped, ctx.skipped
    out, ctx = scope({"p0": (250, 350), "end": "A", "p1": (250, 350)})
    assert lines(out)[-1] == "  n2 --> n1", out


def test_plain_line_needs_arrow():
    for arrows in ((False, False), (None, None)):
        out, ctx = scope({"p0": (50, 50), "p1": (250, 350), "line": True, "arrows": arrows})
        assert out == P("A") + P("B") + P("t0") and not ctx.skipped, (arrows, out, ctx.skipped)
    out, ctx = scope({"p0": (50, 50), "p1": (250, 350), "line": True, "arrows": (False, True)})
    assert lines(out)[-1] == "  n1 --> n2" and not ctx.skipped, (out, ctx.skipped)


def test_decorative_connector_not_loose():
    # glued at neither end, snapped at neither end, no arrowhead set True: decoration, not counted, its text kept
    for kw in ({}, {"arrows": (False, False)}, {"p0": None, "p1": None}):
        out, ctx = scope({"p0": (150, 200), "p1": (160, 200), **kw})
        assert out == P("A") + P("B") + P("t0") and not ctx.skipped, (kw, out, ctx.skipped)
    # an arrowhead, an end glued or naming a shape, or one end snapped: a connector meant to join, still loose
    for kw in ({"arrows": (False, True)}, {"arrows": (True, None)}, {"begin": "A"}, {"end": "missing"},
               {"p0": (50, 50)}):
        out, ctx = scope({"p0": (150, 200), "p1": (160, 200), **kw})
        assert out == P("A") + P("B") + P("t0") and ctx.skipped == {"connector_loose": 1}, (kw, out, ctx.skipped)


def test_infer_shapes_cap():
    old = diagram.INFER_SHAPES
    try:
        diagram.INFER_SHAPES = 2
        out, ctx = scope({"p0": (50, 50), "p1": (250, 350)}, {"p0": (60, 60), "p1": (260, 360)},
                         extra=[("C", (0, 300, 100, 400))])
        assert out == P("A") + P("B") + P("C") + P("t0") + P("t1"), out
        assert ctx.skipped == {"infer_skipped": 1}, ctx.skipped  # once for the scope, no connector_loose
        diagram.INFER_SHAPES = 3
        out, ctx = scope({"p0": (50, 50), "p1": (250, 350)}, extra=[("C", (0, 300, 100, 400))])
        assert lines(out)[-1] == "  n1 --> n2" and not ctx.skipped, (out, ctx.skipped)
    finally:
        diagram.INFER_SHAPES = old


def test_infer_tol_read_at_call_time():
    old = diagram.INFER_TOL
    try:
        diagram.INFER_TOL = 0.01  # tol 5: 9.5 from A is past it
        out, ctx = scope({"p0": (109.5, 50), "p1": (250, 350)})
    finally:
        diagram.INFER_TOL = old
    assert ctx.skipped == {"connector_loose": 1}, (out, ctx.skipped)


def test_infer_connector_cap():
    old = diagram.CONNECTIONS
    try:
        diagram.CONNECTIONS = 1
        out, ctx = scope({"p0": (50, 50), "p1": (250, 350)}, {"p0": (60, 60), "p1": (260, 360)})
    finally:
        diagram.CONNECTIONS = old
    assert lines(out)[-1] == "  n1 --> n2" and out[1:] == P("t1"), out  # the second is past the cap, its slot kept
    assert ctx.skipped == {"infer_skipped": 1}, ctx.skipped


def test_infer_document_cap():
    def build(d):
        d.shape("A", P("A"), box=(0, 0, 100, 100))
        d.shape("B", P("B"), box=(200, 300, 300, 400))
        d.connector("k0", p0=(10, 10), p1=(20, 20))  # snapped and loose: it still spends the document's room
        d.connector("k1", p0=(50, 50), p1=(250, 350))
        return d.place([("A", P("A")), ("B", P("B")), ("k0", P("t0")), ("k1", P("t1"))])
    old = diagram.CONNECTIONS
    try:
        diagram.CONNECTIONS = 2
        ctx = Context()
        budget = ConnectionBudget(ctx)
        first = build(Diagram(ctx, budget))
        assert lines(first)[-1] == "  n1 --> n2" and ctx.skipped == {"connector_loose": 1}, (first, ctx.skipped)
        second = build(Diagram(ctx, budget))  # the two candidates of the first scope used the document's room
    finally:
        diagram.CONNECTIONS = old
    assert second == P("A") + P("B") + P("t0") + P("t1"), second
    assert ctx.skipped == {"connector_loose": 1, "infer_skipped": 1}, ctx.skipped


def test_nan_and_huge_geometry():
    nan, inf = float("nan"), float("inf")
    d, ctx = new()
    d.shape("A", P("A"), box=(nan, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    d.shape("H", P("H"), box=(0, 0, 1e10, 10))
    d.connector("k1", p0=(50, 50), p1=(250, 350))  # A has no box
    d.connector("k2", p0=(1e10, 350), p1=(250, 350))
    d.connector("k3", p0=(inf, 0), p1=("x", 1))
    d.connector("k4", p0=(250, 350, 1), p1=None)
    d.connector("k5", p0=5, p1=(nan, nan))
    slots = [("A", P("A")), ("B", P("B")), ("H", P("H"))] + [(f"k{i}", P(f"t{i}")) for i in range(1, 6)]
    assert d.place(slots) == flat(slots)
    assert ctx.skipped == {"connector_loose": 2}, ctx.skipped  # k3-k5 land nowhere at either end: decorations


def test_zero_size_scope():
    d, ctx = new()
    d.shape("A", P("A"), box=(5, 5, 5, 5))
    d.shape("B", P("B"), box=(5, 5, 5, 5))
    d.connector("k", p0=(5, 5), p1=(5, 5))
    slots = [("A", P("A")), ("B", P("B")), ("k", P("t"))]
    assert d.place(slots) == flat(slots)
    assert not ctx.skipped, ctx.skipped  # unglued and arrowless, it lands nowhere: a decoration
    d, ctx = new()
    d.shape("A", P("A"), box=(5, 5, 5, 5))
    d.shape("B", P("B"), box=(5, 5, 5, 5))
    d.connector("k", p0=(5, 5), p1=(5, 5), arrows=(False, True))
    assert d.place(slots) == flat(slots)
    assert ctx.skipped == {"connector_loose": 1}, ctx.skipped


def test_infer_targets_exclude_containers_and_connectors():
    d, ctx = new()
    d.shape("A", P("A"), box=(0, 0, 100, 100))
    d.shape("B", P("B"), box=(200, 300, 300, 400))
    d.shape("L", P("Lane"), box=(190, 290, 310, 410), container=True, title="Lane")
    d.shape("k0", P("k0"), box=(40, 40, 60, 60))  # a connector's key with a box: never a target
    d.connector("k0", p0=(50, 50), p1=(191, 291))  # p0 is in A and k0's box, p1 in the lane only
    d.connector("k1", p0=(50, 50), p1=(250, 350))  # lands in B, which the lane holds
    out = d.place([("A", []), ("B", []), ("L", P("Lane")), ("k0", P("t0")), ("k1", [])])
    assert lines(out) == ["flowchart TD", '  n1["A"]', '  subgraph c1 ["Lane"]', '    n2["B"]', "  end",
                          "  %% inferred from position", "  n1 --> n2"], out
    assert out[1:] == P("t0") and ctx.skipped == {"connector_loose": 1}, (out, ctx.skipped)


def test_tree_as_list():
    d, _ = new()
    d.shape("R", [Para([Text("R"), Break(), Text("x")])])
    for k in "ABC":
        d.shape(k, P(k))
    d.connector("k1", begin="R", end="A")
    d.connector("k2", begin="R", end="B", arrows=(False, True))
    d.connector("k3", begin="C", end="A", arrows=(True, False))  # A -> C
    out = d.place([(k, P(k)) for k in "RABC"] + [("k1", []), ("k2", []), ("k3", [])])
    assert out == [ListItem(0, "-", [Text("R"), Break(), Text("x")]), ListItem(1, "-", [Text("A")]),
                   ListItem(2, "-", [Text("C")]), ListItem(1, "-", [Text("B")])], out
    d, _ = new()  # a long node: its id leads its item, and its blocks follow the list
    long = "z" * 90
    d.shape("R", P("R"))
    d.shape("A", P(long))
    d.shape("B", P("B"))
    d.connector("k1", begin="R", end="A")
    d.connector("k2", begin="R", end="B")
    out = d.place([("R", []), ("A", []), ("B", [])])
    assert out == [ListItem(0, "-", [Text("R")]), ListItem(1, "-", [Text("n2 " + "z" * 80 + "…")]),
                   ListItem(1, "-", [Text("B")]), Para([Text("n2:")])] + P(long), out


def test_not_trees():
    cases = [
        [("R", "A"), ("A", "B")],                          # a chain: no node with two children
        [("R", "A"), ("R", "B"), ("A", "C"), ("B", "C")],  # two parents
        [("R", "A"), ("R", "B"), ("S", "C")],              # two roots
        [("R", "A"), ("R", "B"), ("B", "R")],              # a cycle: no root
    ]
    for edges in cases:
        out = chart(edges)
        lines(out)
        assert not any(isinstance(b, ListItem) for b in out), (edges, out)
    for kw in ({"label": "x"}, {"arrows": (False, False)}, {"arrows": (True, True)}):
        out = chart([("R", "A"), ("R", "B")], **kw)
        lines(out)
        assert not any(isinstance(b, ListItem) for b in out), (kw, out)


def test_tree_depth_cap():
    edges = [("R", "A"), ("R", "B"), ("A", "C")]  # three levels
    old = smartart.MAX_LEVEL
    try:
        smartart.MAX_LEVEL = 3
        assert all(isinstance(b, ListItem) for b in chart(edges))
        smartart.MAX_LEVEL = 2
        out = chart(edges)
    finally:
        smartart.MAX_LEVEL = old
    lines(out)
    assert not any(isinstance(b, ListItem) for b in out), out


def test_container_subgraph():
    d, _ = new()
    d.shape("C", P("Lane"), box=(0, 0, 100, 100), container=True, title="Lane")
    d.shape("E", P("Empty"), box=(500, 500, 600, 600), container=True, title="Empty")  # holds no node
    d.shape("R", P("R"), box=(10, 10, 20, 20))
    d.shape("A", P("A"), box=(50, 10, 60, 20))
    d.shape("B", P("B"), box=(50, 50, 60, 60))
    d.connector("k1", begin="R", end="A")
    d.connector("k2", begin="R", end="B")  # a tree's shape, but a container makes it Mermaid
    out = d.place([("C", P("Lane")), ("E", P("Empty")), ("R", []), ("A", []), ("B", []), ("k1", []), ("k2", [])])
    assert out[1:] == P("Empty"), out  # C's slot is gone, E's stays
    assert lines(out) == ["flowchart LR", '  subgraph c1 ["Lane"]', '    n1["R"]', '    n2["A"]', '    n3["B"]',
                          "  end", "  n1 --> n2", "  n1 --> n3"], out


def test_nested_containers():
    def build():
        d, _ = new()
        d.shape("O", P("Outer"), box=(0, 0, 1000, 1000), container=True, title="Outer")
        d.shape("I", P("Inner"), box=(0, 0, 100, 100), container=True, title="Inner")
        d.shape("A", P("A"), box=(10, 10, 20, 20))
        d.shape("B", P("B"), box=(500, 500, 510, 510))
        d.connector("c", begin="A", end="B")
        return d.place([("O", P("Outer")), ("I", P("Inner")), ("A", []), ("B", []), ("c", [])])
    out = build()
    assert len(out) == 1, out
    assert lines(out) == ["flowchart TD", '  subgraph c1 ["Outer"]', '    subgraph c2 ["Inner"]', '      n1["A"]',
                          "    end", '    n2["B"]', "  end", "  n1 --> n2"], out
    old = limits.MAX_DEPTH
    try:
        limits.MAX_DEPTH = 1
        out = build()
    finally:
        limits.MAX_DEPTH = old
    assert out[1:] == P("Inner"), out  # the inner container is no subgraph: an ordinary shape, its slot kept
    assert lines(out) == ["flowchart TD", '  subgraph c1 ["Outer"]', '    n1["A"]', '    n2["B"]', "  end",
                          "  n1 --> n2"], out


def test_container_cap():
    def build():
        d, _ = new()
        d.shape("O", P("Outer"), box=(0, 0, 1000, 1000), container=True, title="Outer")
        d.shape("I", P("Inner"), box=(0, 0, 100, 100), container=True, title="Inner")
        d.shape("A", P("A"), box=(10, 10, 20, 20))
        d.shape("B", P("B"), box=(500, 500, 510, 510))
        d.connector("c", begin="A", end="B")
        return d.place([("O", P("Outer")), ("I", P("Inner")), ("A", []), ("B", []), ("c", [])])
    old = diagram.INFER_SHAPES
    try:
        diagram.INFER_SHAPES = 1  # two candidates: past the cap, no subgraph at all
        out = build()
    finally:
        diagram.INFER_SHAPES = old
    ls = lines(out)
    assert not any("subgraph" in ln for ln in ls), ls
    assert out[:2] == P("Outer") + P("Inner") and len(out) == 3, out  # ordinary shapes, their text kept
    assert lines(build())[1] == '  subgraph c1 ["Outer"]'  # at the default cap they are subgraphs


def test_containers_without_node_boxes():
    d, ctx = new()
    d.shape("O", P("Outer"), box=(0, 0, 1000, 1000), container=True, title="Outer")
    d.shape("A", P("A"))
    d.shape("B", P("B"))
    d.connector("c", begin="A", end="B")
    out = d.place([("O", P("Outer")), ("A", []), ("B", []), ("c", [])])
    assert lines(out) == ["flowchart TD", '  n1["A"]', '  n2["B"]', "  n1 --> n2"], out
    assert out[:1] == P("Outer") and len(out) == 2 and not ctx.skipped, (out, ctx.skipped)


def test_budget_cut():
    old = diagram.CONNECTIONS
    try:
        diagram.CONNECTIONS = 1
        ctx = Context()
        d = Diagram(ctx, ConnectionBudget(ctx))
    finally:
        diagram.CONNECTIONS = old
    for k in "ABC":
        d.shape(k, P(k))
    d.connector("k1", begin="A", end="B")
    d.connector("k2", begin="B", end="C", label="second")
    out = d.place([("A", []), ("B", []), ("C", P("C")), ("k1", []), ("k2", P("second"))])
    assert lines(out) == ["flowchart TD", '  n1["A"]', '  n2["B"]', "  n1 --> n2"], out
    assert out[1:] == P("C") + P("second"), out  # the cut connector and its unconnected end stay in the flow
    assert ctx.skipped["connections_cut"] == 1, ctx.skipped


def test_skip_kind_wording():
    sk = collections.Counter(connector_loose=2, infer_skipped=1)
    md = markdown.render(Doc(blocks=P("body")), Path(tempfile.mkdtemp()), skipped=sk)
    assert "2 connectors not attached" in md and "1 diagram too large to place loose connectors" in md, md
    md = markdown.render(Doc(blocks=P("body")), Path(tempfile.mkdtemp()),
                         skipped=collections.Counter(connector_loose=1, infer_skipped=2))
    assert "1 connector not attached" in md and "2 diagrams too large to place loose connectors" in md, md


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
