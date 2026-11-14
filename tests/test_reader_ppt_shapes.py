"""ppt shapes: reading order, anchors, group transforms, tables, placeholders, nesting limits.

Run: python -I -X utf8 tests/test_reader_ppt_shapes.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.formats import ppt_shapes  # noqa: E402
from reader.model import Context, Table  # noqa: E402
from reader_fix import make_ppt, oa_group, ppt_drawing, ppt_shape  # noqa: E402


def md(slides, **kw):
    """Build a .ppt, parse it, render it: (markdown, ctx)."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_ppt(path, slides, **kw)
    ctx = Context()
    doc = reader.parse(path, ctx)
    return markdown.render(doc, d / "assets", False, ctx.skipped), ctx


def text(t, box, **kw):
    return {"text": t, "box": box, **kw}


def walked(shapes):
    """ppt_shapes.walk over a PPDrawing of the make_ppt shape dicts: the top-level Shapes."""
    ids = [1025]
    data = ppt_drawing(*[ppt_shape(s, ids) for s in shapes])
    return ppt_shapes.walk(data, 0, len(data), Context())


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
    for t in ("date text", "number text", "footer text", "header text", "kept body"):
        assert t not in out, (t, out)
    assert "shown" in out, out


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


def test_hyperlink_keeps_text_only():
    out, _ = md([{"shapes": [text("click here", (0, 100, 100, 150), link=True)]}])
    assert "click here" in out and "](" not in out and "http" not in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
