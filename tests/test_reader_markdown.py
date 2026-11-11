"""The document model, text helpers and the one Markdown renderer.

Run: python -I -X utf8 tests/test_reader_markdown.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown, text  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import (  # noqa: E402
    Bold, Break, Cell, Context, Doc, Equation, Heading, Hidden, Image, Link,
    ListItem, Math, NoteRef, Para, Quote, Section, Table, Text,
)


def R(doc, **kw):
    return markdown.render(doc, Path(tempfile.mkdtemp()), **kw)


def P(s, **kw):
    return Para([Text(s)], **kw)


def c(s, **kw):
    return Cell([P(s)] if s else [], **kw)


def t(rows, **kw):
    return Doc(blocks=[Table([[c(x) for x in r] for r in rows], **kw)])


def test_blank_lines_and_lf():
    assert R(Doc(blocks=[Para([Text("a ")]), Para([Text("b")])])) == "a\n\nb\n"
    assert R(Doc()) == ""


def test_hidden_count_line():
    doc = Doc(blocks=[P("x", hidden="hidden"), Para([Text("a"), Hidden([Text("b")], "deleted"), Text("c")])])
    assert R(doc).splitlines()[0] == "(2 hidden places left out)"
    assert R(doc).endswith("ac\n")


def test_hidden_rows_and_sections_count():
    doc = Doc(blocks=[Section("slide", "S", number=1, hidden="hidden"),
                      Table([[c("h")], [c("v")], [c("w")]], hidden_rows=frozenset({1}))])
    out = R(doc)
    assert out.splitlines()[0] == "(2 hidden places left out)" and "v" not in out


def test_hidden_included():
    doc = Doc(blocks=[P("secret", hidden="hidden"), Para([Hidden([Text("old")], "deleted")]),
                      Section("slide", "T", number=2, hidden="hidden"),
                      Table([[c("h")], [c("v")], [c("w")]], hidden_rows=frozenset({1}))])
    out = R(doc, hidden=True)
    assert "(hidden) secret" in out and "(hidden: old)" in out
    assert "## Slide 2: T (hidden)" in out and "(hidden) v" in out and "hidden places" not in out


def test_header_dedupe():
    doc = Doc(blocks=[P("x")], headers=["보고서", " 보고서 ", "- 3 -"], footers=["Page 2 of 9"])
    assert R(doc).splitlines()[0] == "Header: 보고서" and "Footer" not in R(doc)
    doc = Doc(blocks=[P("x")], headers=["a", "b"], footers=["f"])
    assert R(doc).startswith("Header: a | b\nFooter: f\n\nx")


def test_title_not_repeated():
    doc = Doc(title="Plan", blocks=[Heading(1, [Text("plan")])])
    assert R(doc) == "# plan\n"
    assert R(Doc(title="Plan", blocks=[P("x")])) == "# Plan\n\nx\n"


def test_bold_merge_and_heading():
    assert R(Doc(blocks=[Para([Bold([Text("a ")]), Bold([Text("b")]), Text("c")])])) == "**a b** c\n"
    assert R(Doc(blocks=[Para([Text("x"), Bold([Text(" y ")]), Text("z")])])) == "x **y** z\n"
    assert R(Doc(blocks=[Heading(2, [Bold([Text("T")])])])) == "## T\n"
    assert R(Doc(blocks=[Heading(9, [Text("T")])])) == "###### T\n"


def test_link_and_break():
    link = Para([Link("http://a", [Text("x")]), Text(" "), Link("http://b", [Text("http://b")])])
    assert R(Doc(blocks=[link])) == "[x](http://a) http://b\n"
    assert R(Doc(blocks=[Para([Text("a"), Break(), Text("b")])])) == "a\nb\n"


def test_escape_line_start():
    assert R(Doc(blocks=[Para([Text("# not")])])) == "\\# not\n"
    assert R(Doc(blocks=[Para([Text("- x")])])) == "\\- x\n"
    assert R(Doc(blocks=[Para([Text("> q")])])) == "\\> q\n"
    assert R(Doc(blocks=[Para([Text("1. x")])])) == "1\\. x\n"
    assert R(Doc(blocks=[Para([Text("a - b")])])) == "a - b\n"


def test_list_items_consecutive():
    doc = Doc(blocks=[ListItem(0, "-", [Text("a")]), ListItem(1, "1.", [Text("b")]), P("c")])
    assert R(doc) == "- a\n  1. b\n\nc\n"


def test_pipe_table():
    assert R(t([["a", "b"], ["c|d", ""], ["", ""]])) == "|a|b|\n|---|---|\n|c\\|d||\n"


def test_merged_table_html():
    doc = Doc(blocks=[Table([[c("A", colspan=2, header=True)], [c("x"), c("y")]])])
    assert R(doc) == '<table>\n<tr><th colspan="2">A</th></tr>\n<tr><td>x</td><td>y</td></tr>\n</table>\n'
    doc = Doc(blocks=[Table([[c("a<&", rowspan=2), c("b")], [c("d")]])])
    assert R(doc) == '<table>\n<tr><td rowspan="2">a&lt;&amp;</td><td>b</td></tr>\n<tr><td>d</td></tr>\n</table>\n'


def test_one_by_one_and_one_column():
    assert R(t([["only"]])) == "only\n"
    assert R(t([["only", ""], ["", ""]])) == "only\n"
    assert R(t([["a"], [""], ["b"]])) == "a\nb\n"


def test_nested_table_in_cell():
    inner = Table([[c("1"), c("2")], [c("3"), c("4")]])
    doc = Doc(blocks=[Table([[Cell([P("k"), inner]), c("z")]])])
    assert R(doc) == "|k<br>1 / 2 ; 3 / 4|z|\n|---|---|\n"
    empty = Table([[c(""), c("")]])
    assert R(Doc(blocks=[Table([[Cell([P("k"), empty]), c("z")]])])) == "|k|z|\n|---|---|\n"


def test_slide_and_notes():
    doc = Doc(blocks=[Section("slide", "Intro", number=3, blocks=[P("x")], notes=[P("n1"), P("n2")])])
    assert R(doc) == "## Slide 3: Intro\n\nx\n\n> Notes: n1\n> n2\n"
    assert R(Doc(blocks=[Section("slide", "", number=4)])) == "## Slide 4\n"


def test_sheet_page_chapter():
    assert R(Doc(blocks=[Section("sheet", "Q1", blocks=[P("x")])])) == "## Sheet: Q1\n\nx\n"
    assert R(Doc(blocks=[Section("page", "Page-1", blocks=[P("x")])])) == "## Page: Page-1\n\nx\n"
    assert R(Doc(blocks=[Section("page", "", blocks=[P("x")])])) == "x\n"
    assert R(Doc(blocks=[Section("chapter", "Ch", blocks=[P("x")])])) == "x\n"


def test_footnotes_numbered_by_use():
    doc = Doc(blocks=[Para([Text("t"), NoteRef("a"), NoteRef("b")])],
              notes={"b": [P("B")], "a": [P("A")], "u": [P("U")]})
    assert R(doc) == "t[^1][^2]\n\n[^1]: A\n[^2]: B\n[^3]: U\n"


def test_chart_caption():
    out = R(t([["a", "b"], ["1", "2"]], caption="Chart: bar — 매출"))
    assert out == "Chart: bar — 매출\n|a|b|\n|---|---|\n|1|2|\n"


def test_equations():
    assert R(Doc(blocks=[Equation("\\frac{a}{b}")])) == "$$\\frac{a}{b}$$\n"
    assert R(Doc(blocks=[Equation("x", display=False)])) == "$x$\n"
    assert R(Doc(blocks=[Para([Math("y")])])) == "$y$\n"


def test_quote_and_image():
    assert R(Doc(blocks=[Quote([P("a"), P("b")])])) == "> a\n>\n> b\n"
    assert R(Doc(blocks=[Image(b"x", alt="pic"), Image(b"x")])) == "pic\n"


def test_output_cap():
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100
    try:
        out = R(Doc(blocks=[P("para %d" % i) for i in range(50)]))
    finally:
        limits.MAX_OUTPUT = old
    assert out.splitlines()[-1] == "(output truncated at 20 MB)" and len(out) < 200


def test_no_mutation():
    doc = Doc(blocks=[P("x", hidden="hidden"), P("y")], headers=["h"])
    R(doc)
    assert len(doc.blocks) == 2 and doc.headers == ["h"]


def test_depth_guard():
    b = P("x")
    for _ in range(limits.MAX_DEPTH + 5):
        b = Quote([b])
    try:
        R(Doc(blocks=[b]))
    except ReadError as e:
        assert e.kind == "damaged"
        return
    raise AssertionError("ReadError not raised")


def test_stats():
    assert markdown.stats("# A\n\nx\n\n# B\n") == {"lines": 5, "hidden": 0, "headings": [(1, "A"), (5, "B")]}
    s = markdown.stats("(3 hidden places left out)\n\n## A\n\n### b\n")
    assert s["hidden"] == 3 and s["headings"] == [(3, "A")]


def test_guard_isolates():
    ctx = Context()
    for _ in range(2):
        with ctx.guard("slide"):
            raise ValueError("x")
    assert ctx.skipped["slide"] == 2 and len(ctx.warnings) == 1
    try:
        with ctx.guard("slide"):
            raise ReadError("damaged")
    except ReadError:
        assert ctx.skipped["slide"] == 2
        return
    raise AssertionError("ReadError swallowed")


def test_garble_ratio_and_squash():
    assert text.garble_ratio("ab�\x01") == 0.5
    assert text.garble_ratio("a\tb\n") == 0.0 and text.garble_ratio("") == 0.0
    assert text.squash("a  b \r\nc   \rd") == "a b\nc\nd"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
