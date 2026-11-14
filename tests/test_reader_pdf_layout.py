"""pdf layout: glyph gaps, lines by script, column order, headings, running headers.

Run: python -I -X utf8 tests/test_reader_pdf_layout.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.formats import pdf_layout as L  # noqa: E402
from reader.formats import xps  # noqa: E402
from reader.formats.pdf_layout import Fragment  # noqa: E402
from reader.model import Link, Para, Text  # noqa: E402

W = 6.0  # device width of one glyph at size 12 in these fixtures


def frag(text, x, y, size=12, **kw):
    kw.setdefault("width", len(text) * size * 0.5)
    kw.setdefault("space_width", size * 0.25)
    return Fragment(x=x, y=y, size=size, text=text, **kw)


def numbered(frags):
    for i, f in enumerate(frags):
        f.order = i
    return frags


def texts(lns):
    return [ln.text for ln in lns]


def para_text(p):
    out = []
    for i in p.inlines:
        out.append(i.text if isinstance(i, Text) else "".join(t.text for t in i.inlines))
    return "".join(out)


def test_space_by_gap():
    # "Hel" is 18 wide: ends at 90 when it starts at 72
    a = frag("Hel", 72, 700)
    assert texts(L.lines(numbered([a, frag("lo", 91, 700)]))) == ["Hello"]
    assert texts(L.lines(numbered([a, frag("lo", 93, 700)]))) == ["Hel lo"]


def test_no_double_space():
    out = L.lines(numbered([frag("a ", 72, 700), frag("b", 83, 700)]))
    assert texts(out) == ["a b"]
    out = L.lines(numbered([frag("a", 72, 700), frag(" b", 90, 700)]))
    assert texts(out) == ["a b"]


def test_letter_spaced():
    frags, x = [], 72.0
    for ch in "WORD":
        frags.append(frag(ch, x, 700, width=6.0))
        x += 6.5
    x += 4.0 - 0.5
    for ch in "ONE":
        frags.append(frag(ch, x, 700, width=6.0))
        x += 6.5
    assert texts(L.lines(numbered(frags))) == ["WORD ONE"]


def test_tj_gap_inside_a_fragment():
    # the same glyph pair steps 6 apart twice and 10 apart once: the wide step is a gap
    f = frag("abab", 72, 700, font="F", char_offsets=(0.0, 6.0, 12.0, 22.0, 28.0), width=28.0)
    g = frag("ab", 200, 600, font="F", char_offsets=(0.0, 6.0, 12.0), width=12.0)
    out = L.lines(numbered([f, g]))
    assert texts(out) == ["aba b", "ab"]


def test_join_latin():
    m = L.SpacingModel()
    assert L.join_break("end of", "line", m) == "end of line"


def test_join_han_kana():
    m = L.SpacingModel()
    assert L.join_break("中文", "字", m) == "中文字"
    assert L.join_break("かな", "カナ", m) == "かなカナ"


def test_join_hangul_model():
    m = L.SpacingModel(prior=0.25, pairs={"의운": 0.9, "안보": 0.5})
    assert L.join_break("의견의", "운수", m) == "의견의 운수"
    assert L.join_break("국가안", "보장", m) == "국가안보장"


def test_spacing_table_loads():
    m = L.load_model()
    assert 0 < m.prior < 1 and len(m.pairs) > 1000
    assert L.load_model() is m


def test_hyphen_join():
    m = L.SpacingModel()
    assert L.join_break("inter-", "national", m) == "international"
    assert L.join_break("well-", "Known", m) == "well- Known"
    assert L.join_break("1-", "2", m) == "1- 2"


def test_paragraph_break():
    fr = [frag("line one", 72, 700), frag("line two", 72, 686), frag("line three", 72, 672),
          frag("next para", 72, 632)]
    out = L.paragraphs(L.lines(numbered(fr)), L.SpacingModel())
    assert len(out) == 2 and all(isinstance(p, Para) for p in out)
    assert para_text(out[0]) == "line one line two line three"
    assert para_text(out[1]) == "next para"


def test_content_order_kept():
    fr = [frag("left a", 72, 700), frag("left b", 72, 686), frag("left c", 72, 672),
          frag("right a", 360, 700), frag("right b", 360, 686), frag("right c", 360, 672)]
    out = L.reading_order(L.lines(numbered(fr)), 612)
    assert texts(out) == ["left a", "left b", "left c", "right a", "right b", "right c"]


def test_column_fallback():
    fr = [frag("left 1", 72, 500), frag("right 1", 360, 520), frag("left 2", 72, 540),
          frag("right 2", 360, 560), frag("left 3", 72, 580), frag("right 3", 360, 600)]
    lns = L.lines(numbered(fr))
    assert len(lns) == 6
    out = L.reading_order(lns, 612)
    assert texts(out) == ["left 3", "left 2", "left 1", "right 3", "right 2", "right 1"]


def test_column_fallback_keeps_links():
    fr = [frag("left 1", 72, 500), frag("right 1", 360, 520), frag("left 2", 72, 540),
          frag("right 2", 360, 560), frag("left 3", 72, 580), frag("right 3", 360, 600)]
    link = ((72, 498, 108, 512), "https://x")
    out = L.reading_order(L.lines(numbered(fr), [link]), 612)
    assert out[2].runs == [("left 1", "https://x")]
    assert out[0].runs == [("left 3", "")]


def test_vertical_cjk():
    fr = [frag("縦", 480, 700, dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=12.0),
          frag("書", 480, 688, dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=12.0),
          frag("日", 500, 700, dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=12.0),
          frag("本", 500, 688, dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=12.0)]
    out = L.vertical_lines(numbered(fr))
    assert texts(out) == ["日本", "縦書"] and all(ln.vertical for ln in out)
    assert texts(L.lines(numbered(fr))) == ["日本", "縦書"]


def test_rotated_page_order():
    box = (0, 0, 612, 792)
    fr = []
    for i, word in enumerate(("first line", "second line", "third line")):
        x = 100 + 14 * i
        fr.append(frag(word[:5], x, 100, dir_x=0.0, dir_y=1.0, up_x=-1.0, up_y=0.0, width=30.0))
        fr.append(frag(word[5:], x, 130, dir_x=0.0, dir_y=1.0, up_x=-1.0, up_y=0.0, width=30.0))
    out = L.lines(L.rotate_into_view(numbered(fr), 90, box))
    assert texts(out) == ["first line", "second line", "third line"]
    ordered = L.reading_order(out, 792)
    assert texts(ordered) == texts(out)
    assert not any(ln.vertical for ln in out)


def test_rotate_into_view_directions():
    f = frag("x", 10, 20, dir_x=1.0, dir_y=0.0, up_x=0.0, up_y=1.0)
    box = (0, 0, 100, 200)
    cases = {90: (20, 90, 0.0, -1.0), 180: (90, 180, -1.0, 0.0), 270: (180, 10, 0.0, 1.0),
             0: (10, 20, 1.0, 0.0)}
    for rot, (x, y, dx, dy) in cases.items():
        g = L.rotate_into_view([f], rot, box)[0]
        assert (g.x, g.y, g.dir_x, g.dir_y) == (x, y, dx, dy), rot
    assert f.x == 10


def test_heading_by_size():
    assert L.heading_level("Chapter one", 15, 10) == 3
    assert L.heading_level("Chapter one", 12.5, 10) == 4
    assert L.heading_level("Chapter one", 12, 10) is None
    assert L.heading_level("x" * 130, 15, 10) is None
    assert L.heading_level("1.", 15, 10) is None


def test_body_size():
    lns = L.lines(numbered([frag("a b", 72, 700, size=10), frag("c d", 72, 686, size=10),
                            frag("T T", 72, 740, size=20)]))
    assert L.body_size(lns) == 10 and L.body_size([]) == 0.0


def test_outline_level():
    assert (L.outline_level(1), L.outline_level(2), L.outline_level(9)) == (3, 4, 6)


def _page(n, body_lines=3):
    fr = [frag("ACME report", 72, 760), frag(f"Page {n}", 280, 40)]
    for i in range(body_lines):
        fr.append(frag(f"body {n} line {i}", 72, 700 - 14 * i))
    return L.lines(numbered(fr))


def test_running_header_footer():
    pages = [_page(1), _page(2), _page(3)]
    boxes = [(0, 0, 612, 792)] * 3
    headers, footers, drops = L.running(pages, boxes)
    assert headers == ["ACME report"] and footers == []
    for lns, drop in zip(pages, drops):
        assert {lns[i].text.split()[0] for i in drop} == {"ACME", "Page"}
        assert len(drop) == 2


def test_running_keeps_body_and_single_page():
    pages = [_page(1)]
    headers, footers, drops = L.running(pages, [(0, 0, 612, 792)])
    assert (headers, footers, drops) == ([], [], [set()])


def test_link_runs():
    link = ((96, 690, 120, 712), "https://x")
    out = L.lines(numbered([frag("see here now", 72, 700, width=72.0)]), [link])
    assert out[0].runs == [("see ", ""), ("here", "https://x"), (" now", "")]
    assert out[0].text == "see here now"
    p = L.paragraphs(out, L.SpacingModel())
    assert [type(i) for i in p[0].inlines] == [Text, Link, Text]
    assert p[0].inlines[1].url == "https://x"


def test_link_survives_hyphen_join():
    link = ((72, 698, 90, 712), "https://x")
    lns = L.lines(numbered([frag("inter-", 72, 700), frag("national", 72, 686)]), [link])
    p = L.paragraphs(lns, L.SpacingModel())
    assert para_text(p[0]) == "international"
    assert isinstance(p[0].inlines[0], Link)


def test_xps_columns_lifted():
    runs = [(1.0, 10.0, 12.0, "a", 0)]
    assert xps._columns(runs, 600) == [runs]


def test_scale():
    fr = [frag("w%d" % i, 72 + (i % 40) * 14, 700 - (i // 40) * 14) for i in range(60000)]
    t = time.perf_counter()
    lns = L.lines(numbered(fr))
    L.reading_order(lns, 612)
    L.paragraphs(lns, L.SpacingModel())
    assert time.perf_counter() - t < 20


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
