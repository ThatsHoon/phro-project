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


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
TIFF = b"II*\x00" + b"\x00" * 20


def test_hidden_row_images_are_not_written():
    def img(n):
        return Cell([Image(PNG + bytes([n]), "")])

    one_col = Table([[c("a")], [img(1)], [c("b")]], hidden_rows=frozenset({1}))
    inner = Table([[c("x")], [img(2)], [c("y")]], hidden_rows=frozenset({1}))
    nested = Table([[Cell([P("k"), inner]), c("z")]])
    for doc in (Doc(blocks=[one_col]), Doc(blocks=[nested])):
        d = Path(tempfile.mkdtemp())
        info = {}
        out = markdown.render(doc, d, info=info)
        assert "![" not in out and not any(d.iterdir()), (out, list(d.iterdir()))
        assert info["images"] == 0 and out.startswith("(1 hidden places left out)"), out


def test_image_count_is_linked_and_viewable():
    info = {}
    doc = Doc(blocks=[Image(PNG + b"1"), P("x"), Image(PNG + b"1"), Image(PNG + b"2"), Image(TIFF, "t")])
    out = markdown.render(doc, Path(tempfile.mkdtemp()), info=info)
    assert out.count("![") == 3 and info["images"] == 2, (out, info)


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
    assert R(Doc(blocks=[Para([Bold([Text("a ")]), Bold([Text("b")]), Text(" c")])])) == "**a b** c\n"
    assert R(Doc(blocks=[Para([Bold([Text("bold")]), Text("s")])])) == "**bold**s\n"
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


def test_span_kept_in_one_row_table():
    out = R(Doc(blocks=[Table([[Cell([P("a")], colspan=2), Cell([P("b")])]])]))
    assert '<td colspan="2">a</td>' in out and "<td>b</td>" in out, out


def test_huge_spans_render_fast():
    import time
    row = [Cell([P("x")], rowspan=9999999, colspan=9999999) for _ in range(5)]
    t0 = time.time()
    out = R(Doc(blocks=[Table([list(row) for _ in range(50)])]))
    assert time.time() - t0 < 5 and "x" in out


def test_wide_row_keeps_all_cells():
    out = R(Doc(blocks=[Table([[c(str(i)) for i in range(5000)]])]))
    assert "4999" in out and out.count("|") > 5000


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


def test_section_comments_render():
    doc = Doc(blocks=[Section("slide", number=1, notes=[P("n")], comments=[P("Comment by A: c")])])
    assert "> Notes: n\n> Comment by A: c" in R(doc)
    doc = Doc(blocks=[Section("slide", number=1, comments=[P("Comment by A: c"), P("Comment by B: d")])])
    assert R(doc).endswith("> Comment by A: c\n> Comment by B: d\n"), R(doc)


def test_sheet_page_chapter():
    assert R(Doc(blocks=[Section("sheet", "Q1", blocks=[P("x")])])) == "## Sheet: Q1\n\nx\n"
    assert R(Doc(blocks=[Section("page", "Page-1", blocks=[P("x")])])) == "## Page: Page-1\n\nx\n"
    assert R(Doc(blocks=[Section("page", "", blocks=[P("x")])])) == "x\n"
    assert R(Doc(blocks=[Section("chapter", "Ch", blocks=[P("x")])])) == "x\n"


def test_footnotes_numbered_by_use():
    doc = Doc(blocks=[Para([Text("t"), NoteRef("a"), NoteRef("b")])],
              notes={"b": [P("B")], "a": [P("A")], "u": [P("U")]})
    assert R(doc) == "t[^1][^2]\n\n[^1]: A\n[^2]: B\n[^3]: U\n"


def test_hidden_only_note_drops_its_visible_mark():
    # a note whose whole text is hidden has no definition to show, so its visible mark goes with it and the note is
    # one hidden place; the next note takes number 1, and --hidden shows both mark and definition
    doc = Doc(blocks=[Para([Text("a"), NoteRef("h"), Text(" b"), NoteRef("v")])],
              notes={"h": [Para([Hidden([Text("secret")])])], "v": [P("seen")]})
    assert R(doc) == "(1 hidden places left out)\n\na b[^1]\n\n[^1]: seen\n", R(doc)
    assert R(doc, hidden=True) == "a[^1] b[^2]\n\n[^1]: (hidden: secret)\n[^2]: seen\n", R(doc, hidden=True)
    # a mark inside another note's definition goes too
    doc = Doc(blocks=[Para([Text("a"), NoteRef("v")])],
              notes={"v": [Para([Text("seen"), NoteRef("h")])], "h": [Para([Hidden([Text("secret")])])]})
    assert R(doc) == "(1 hidden places left out)\n\na[^1]\n\n[^1]: seen\n", R(doc)


def test_hidden_note_is_counted_once():
    # a hidden mark of a hidden-only note is the one hidden place; its skipped definition is not counted again
    doc = Doc(blocks=[Para([Text("a"), NoteRef("h", hidden="hidden"), Text(" c")])],
              notes={"h": [Para([Hidden([Text("note")])])]})
    assert R(doc) == "(1 hidden places left out)\n\na c\n", R(doc)
    # the same when the mark sits inside a hidden run
    doc = Doc(blocks=[Para([Text("a"), Hidden([Text("b"), NoteRef("h")]), Text(" c")])],
              notes={"h": [Para([Hidden([Text("note")])])]})
    assert R(doc) == "(1 hidden places left out)\n\na c\n", R(doc)
    # the same when the mark sits in a hidden block, a hidden sheet's section, or a hidden table row or column
    note = {"h": [Para([Hidden([Text("note")])])]}
    marked = Para([Text("b"), NoteRef("h")])
    for hidden_block in (Para([Text("b"), NoteRef("h")], hidden="hidden"),
                         Section("sheet", "H", blocks=[marked], hidden="hidden"),
                         Table([[Cell([P("x")])], [Cell([marked])]], hidden_rows=frozenset({1})),
                         Table([[Cell([P("x")]), Cell([P("y")])], [Cell([P("z")]), Cell([marked])]],
                               hidden_rows=frozenset({1})),
                         Table([[Cell([P("x")]), Cell([marked])]], hidden_cols=frozenset({1}))):
        doc = Doc(blocks=[P("a"), hidden_block], notes=dict(note))
        out = R(doc)
        assert out.startswith("(1 hidden places left out)\n\na\n"), (hidden_block, out)
        assert "[^" not in out, out
    # an unreferenced hidden-only note is still counted, once
    doc = Doc(blocks=[P("a")], notes={"h": [Para([Hidden([Text("note")])])]})
    assert R(doc) == "(1 hidden places left out)\n\na\n", R(doc)


def test_many_notes_render_fast():
    # numbering is linear in the notes: every other one referenced, the rest numbered after them in insertion order
    import time
    n = 60_000
    doc = Doc(blocks=[Para([NoteRef(f"n{i}") for i in range(0, n, 2)])],
              notes={f"n{i}": [P(f"x{i}")] for i in range(n)})
    t0 = time.monotonic()
    lines = R(doc).splitlines()
    assert time.monotonic() - t0 < 10, time.monotonic() - t0
    assert lines[2:4] == ["[^1]: x0", "[^2]: x2"] and lines[-1] == f"[^{n}]: x{n - 1}" and len(lines) == n + 2


def test_chart_caption():
    out = R(t([["a", "b"], ["1", "2"]], caption="Chart: bar — 매출"))
    assert out == "Chart: bar — 매출\n|a|b|\n|---|---|\n|1|2|\n"


def test_equations():
    assert R(Doc(blocks=[Equation("\\frac{a}{b}")])) == "$$\\frac{a}{b}$$\n"
    assert R(Doc(blocks=[Equation("x", display=False)])) == "$x$\n"
    assert R(Doc(blocks=[Para([Math("y")])])) == "$y$\n"


def test_quote_and_image():
    assert R(Doc(blocks=[Quote([P("a"), P("b")])])) == "> a\n>\n> b\n"
    assert R(Doc(blocks=[Image(b"x", alt="pic"), Image(b"x")])) == "[image: pic]\n"


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
    out = R(Doc(blocks=[P("lead"), b]))
    assert "lead" in out and "nested too deeply left out" in out


def test_deep_nesting_is_fast():
    import time
    t0 = time.time()
    tb = Table([[c("leaf")]])
    for _ in range(30):
        tb = Table([[Cell([P("k"), tb])]])
    out = R(Doc(blocks=[Table([[Cell([P("k"), tb]), c("z")]])]))
    assert time.time() - t0 < 1 and "leaf" in out


def test_has_text_scanned_once_a_render():
    # a chain of one-cell tables, each cell holding only the next table: every table's grid asks whether its cell has
    # text, a scan of the chain below it. One memo for the whole render scans each cell once, so the scans grow with
    # the chain's depth, not with its square
    calls = []
    old = markdown._Render._block_has_text

    def spy(self, b):
        calls.append(b)
        return old(self, b)

    def chain(d):
        tb = Table([[c("leaf")]])
        for _ in range(d):
            tb = Table([[Cell([tb])]])
        return Doc(blocks=[tb])
    counts = {}
    markdown._Render._block_has_text = spy
    try:
        for d in (20, 40):
            calls.clear()
            assert R(chain(d)) == "leaf\n"
            counts[d] = len(calls)
    finally:
        markdown._Render._block_has_text = old
    assert counts[40] <= 2 * counts[20] + 4, counts
    # a list met again where its scan would pass MAX_DEPTH is scanned again: the answer and the count are a fresh
    # scan's
    r = markdown._Render(Doc(), Path(tempfile.mkdtemp()), False)
    blocks = [Table([[Cell([P("s")])]])]
    assert r._has_text(blocks) and not r.too_deep
    r.depth = limits.MAX_DEPTH - 1
    assert not r._has_text(blocks) and r.too_deep == 1
    r.depth = 0
    assert r._has_text(blocks) and r.too_deep == 1


def test_nested_hidden_rows():
    inner = Table([[c("a")], [c("SECRET")], [c("b")]], hidden_rows=frozenset({1}))
    doc = Doc(blocks=[Table([[Cell([inner]), c("z")]])])
    out = R(doc)
    assert "SECRET" not in out and out.splitlines()[0] == "(1 hidden places left out)" and "|a ; b|z|" in out
    assert "|a ; (hidden) SECRET ; b|z|" in R(doc, hidden=True)


def test_span_is_clamped():
    import time
    t0 = time.time()
    doc = Doc(blocks=[Table([[c("a", rowspan=10**6, colspan=10**6), c("b")], [c("d")]])])
    out = R(doc)
    assert time.time() - t0 < 1 and 'rowspan="2"' in out and f'colspan="{limits.GRID_COLS}"' in out


def test_cell_heading_and_alt_escaped():
    h = Cell([Heading(2, [Text("a|b")])])
    assert "|a\\|b|z|" in R(Doc(blocks=[Table([[h, c("z")]])]))
    img = Cell([Image(b"x", alt="p|q")])
    assert "|[image: p\\|q]|z|" in R(Doc(blocks=[Table([[img, c("z")]])]))
    h2 = Cell([Heading(1, [Text("<&>")]), Image(b"x", alt="<i>")])
    out = R(Doc(blocks=[Table([[h2, c("z", colspan=1, rowspan=2)], [c("y")]])]))
    assert "&lt;&amp;>" in out and "[image: &lt;i>]" in out


def test_stats():
    assert markdown.stats("# A\n\nx\n\n# B\n") == {"lines": 5, "hidden": 0, "headings": [(1, "A"), (5, "B")], "pages": []}
    s = markdown.stats("(3 hidden places left out)\n\n## A\n\n### b\n")
    assert s["hidden"] == 3 and s["headings"] == [(3, "A"), (5, "b")]  # no level has 3: every level


def test_stats_levels_down_to_the_first_with_three():
    md = "# T\n\n## a\n\n### x\n\n## b\n\n## c\n\n#### y\n"
    assert markdown.stats(md)["headings"] == [(1, "T"), (3, "a"), (7, "b"), (9, "c")]
    md = "# A\n\n# B\n\n# C\n\n## d\n"
    assert markdown.stats(md)["headings"] == [(1, "A"), (3, "B"), (5, "C")]


def test_unread_line_words_and_order():
    import collections
    sk = collections.Counter({"image": 2, "slide": 1, "styles": 3, "rels": 1, "css": 4, "transform": 2,
                              "spine": 1, "chapter": 2, "depth": 1, "sheet_cut": 0})
    assert markdown.unread(sk) == ["3 chapters", "1 slide", "2 images", "1 place nested too deeply"]
    out = R(Doc(blocks=[P("x", hidden="hidden"), P("body")]), skipped=sk)
    assert out.startswith("(1 hidden places left out)\n\n(could not read: 3 chapters, 1 slide, 2 images, "
                          "1 place nested too deeply)\n\nbody"), out
    assert markdown.unread({}) == [] and R(Doc(blocks=[P("a")]), skipped={"styles": 1}) == "a\n"
    import re
    kinds = set()
    for f in (HERE / "scripts" / "reader").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        kinds |= set(re.findall(r'skipped\["(\w+)"\]', src)) | set(re.findall(r'guard\("(\w+)"\)', src))
        kinds |= set(re.findall(r'self\.(?:warn|count|part_bytes|data|related|open)\([^)]*?"([a-z_]+)"', src))
    known = {k for ks, _, _ in markdown.LOST for k in ks} | markdown.INTERNAL | {k for k, _ in markdown.NAMED}
    assert kinds - known == set(), kinds - known


def test_unread_named_after_counted():
    sk = {"vba": 1, "slide": 2}
    assert markdown.unread(sk) == ["2 slides", "VBA macros"] and markdown.lost(sk) == ["2 slides"]
    assert markdown.unread({"vba": 0}) == [] and markdown.lost({"vba": 1}) == []


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


def test_output_cap_applies_while_building():
    import time
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100_000
    try:
        one = P("x" * 1000)
        for blocks in ([one] * 1_000_000, [Section("chapter", blocks=[one] * 1_000_000)],
                       [Quote([one] * 1_000_000)]):
            t0 = time.monotonic()
            out = R(Doc(blocks=blocks))
            assert time.monotonic() - t0 < 3
            assert out.endswith("(output truncated at 20 MB)" + chr(10)) and len(out) < 150_000
    finally:
        limits.MAX_OUTPUT = old


def test_table_stops_at_output_cap_while_rendering():
    import time
    from reader.model import Cell, Table
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100_000
    try:
        shared = Cell([P("x" * 1000)])
        for ncols in (1, 2):
            t0 = time.monotonic()
            out = R(Doc(blocks=[Table([[shared] * ncols] * 1_000_000)]))
            assert time.monotonic() - t0 < 3
            assert out.endswith("(output truncated at 20 MB)" + chr(10)) and len(out) < 150_000
    finally:
        limits.MAX_OUTPUT = old


def test_overlong_line_is_cut_not_dropped():
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100_000
    try:
        out = R(Doc(blocks=[P("lead"), P("가" * 300_000), P("after")]))
    finally:
        limits.MAX_OUTPUT = old
    assert out.startswith("lead\n\n가가가") and out.endswith("\n(output truncated at 20 MB)\n"), out[:50]
    assert 90_000 < len(out.encode("utf-8")) <= 100_000 + 40 and "after" not in out


def test_wide_row_stops_at_output_cap():
    import time
    import tracemalloc
    old = limits.MAX_OUTPUT
    limits.MAX_OUTPUT = 100_000
    try:
        for kw in ({}, {"rowspan": 2}):
            shared = Cell([P("x" * 10_000)])
            rows = [[Cell([P("y" * 10_000)], **kw)] + [shared] * 999, [shared] * 999]
            tracemalloc.start()
            t0 = time.monotonic()
            out = R(Doc(blocks=[P("lead"), Table(rows)]))
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            assert time.monotonic() - t0 < 2 and peak < 3_000_000, peak
            assert out.startswith("lead\n\n") and "y" * 10_000 in out and out.endswith("(output truncated at 20 MB)\n")
    finally:
        limits.MAX_OUTPUT = old


def test_over_deep_nesting_is_left_out():
    inner = c("deep")
    for _ in range(100):
        inner = Cell([Table([[inner, c("x")]])])
    doc = Doc(blocks=[P("lead"), Table([[inner, c("y")]])])
    out = R(doc)
    assert "lead" in out and "places nested too deeply left out)" in out.split(chr(10) * 2)[0], out[:200]


def test_hidden_cols_left_out():
    out = R(t([["a", "SECRET", "b"], ["c", "SECRET2", "d"]], hidden_cols=frozenset({1})))
    assert "SECRET" not in out and out.splitlines()[0] == "(1 hidden places left out)", out
    assert "|a|b|" in out and "|c|d|" in out, out


def test_hidden_cols_shown():
    out = R(t([["a", "SECRET", "b"], ["c", "SECRET2", "d"]], hidden_cols=frozenset({1})), hidden=True)
    assert "|a|(hidden) SECRET|b|" in out and "|c|SECRET2|d|" in out and "hidden places" not in out, out


def test_span_over_hidden_col():
    tb = Table([[Cell([P("wide")], colspan=3)], [c("a"), c("b"), c("d")]], hidden_cols=frozenset({1}))
    out = R(Doc(blocks=[tb]))
    assert 'colspan="2"' in out and "wide" in out and "<td>b</td>" not in out, out


def test_hidden_empty_col_quiet():
    out = R(t([["a", "", "b"], ["c", "", "d"]], hidden_cols=frozenset({1})))
    assert "hidden" not in out and "|a|b|" in out, out


def test_nested_hidden_cols():
    inner = Table([[c("a"), c("SECRET"), c("b")]], hidden_cols=frozenset({1}))
    doc = Doc(blocks=[Table([[Cell([inner]), c("z")]])])
    out = R(doc)
    assert "SECRET" not in out and out.splitlines()[0] == "(1 hidden places left out)" and "|a / b|z|" in out, out
    assert "(hidden) SECRET" in R(doc, hidden=True)


def test_all_hidden_cols_cell_dropped():
    out = R(t([["a", "S", "b"]], hidden_cols=frozenset({0, 1, 2})))
    assert "S" not in out and out.splitlines()[0] == "(3 hidden places left out)", out


def test_page_heading_numbered():
    out = R(Doc(blocks=[Section("page", number=3, blocks=[Para([Text("x")])]), Section("page", number=4)]))
    assert "## Page 3" + chr(10)*2 + "x" + chr(10)*2 + "## Page 4" in out, out
    assert "## Page 2: T" in R(Doc(blocks=[Section("page", number=2, title="T")]))


def test_page_heading_xps_unchanged():
    assert "## Page: T" in R(Doc(blocks=[Section("page", title="T")]))
    assert "## Page" not in R(Doc(blocks=[Section("page", blocks=[P("a")])]))


def test_stats_pages_renderer_heads():
    # a "## Page N" line inside a comment is not a page head: stats counts the heads the renderer wrote
    nl = chr(10)
    doc = Doc(blocks=[Section("page", number=1, blocks=[P("a")], comments=[P("x" + nl + "## Page 9")]),
                      Section("page", number=2, blocks=[P("b")])])
    info = {}
    md = R(doc, info=info)
    assert "## Page 9" in md.splitlines(), md
    lines = md.splitlines()
    assert markdown.stats(md, info["pages"])["pages"] == [[1, 1], [2, lines.index("## Page 2") + 1]], (md, info)
    assert markdown.stats(md)["pages"] == [[1, 1], [9, 6], [2, 8]], markdown.stats(md)  # the text alone


def test_stats_pages_mark_in_document_text():
    # a U+10FFFD in the document's own text neither spoofs a page head nor is dropped
    nl, mark = chr(10), chr(0x10FFFD)
    for spoof in (mark + "## Page 9", "x" + mark + nl + "## Page 9", "hello " + mark):
        doc = Doc(blocks=[Section("page", number=1, blocks=[P(spoof)], comments=[P("y" + nl + "## Page 9")]),
                          Section("page", number=2, blocks=[P("b")])])
        info = {}
        md = R(doc, info=info)
        lines = md.splitlines()
        assert markdown.stats(md, info["pages"])["pages"] == [[1, 1], [2, lines.index("## Page 2") + 1]], (spoof, md, info)
        assert md.count(mark) == spoof.count(mark), (spoof, md)


def test_stats_pages():
    md = "x" + chr(10) + "## Page 1" + chr(10) + "a" + chr(10) + "## Page 2: T" + chr(10) + chr(92) + "## Page 9" + chr(10)
    assert markdown.stats(md)["pages"] == [[1, 2], [2, 4]]


def test_lost_pdf_kinds():
    from collections import Counter
    sk = Counter(page_cut=2, page_part=1, scan=1, figure=3, field=1, annotation=2)
    assert markdown.unread(sk) == ["2 pages past the time or page limit", "1 page read in part",
                                   "1 scanned page not shown", "3 vector figures not shown", "1 form field",
                                   "2 annotations"]


def test_named_pdf_items():
    from collections import Counter
    assert markdown.unread(Counter(xfa=1, attachment=1)) == ["XFA form", "attached files"]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
