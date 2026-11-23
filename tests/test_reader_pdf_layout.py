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


def gapped(text, gaps, x=72, y=700, size=12, **kw):
    """A fragment whose char_gaps are {index: gap}."""
    g = tuple(gaps.get(k, 0.0) for k in range(len(text)))
    return frag(text, x, y, size=size, char_gaps=g, **kw)


def test_tj_gap_end_of_word_punctuation():
    # pdfTeX [(end.)-333(Next)]: "." always ends a word, so only the gap can say so
    assert texts(L.lines(numbered([gapped("end.Next", {4: 4.0})]))) == ["end. Next"]


def test_tj_gap_glyph_seen_once():
    assert texts(L.lines(numbered([gapped("extab", {2: 3.0})]))) == ["ex tab"]


def test_tj_gap_same_font_two_sizes():
    big = gapped("abab", {}, size=24, font="F", char_offsets=(0.0, 12.0, 24.0, 36.0, 48.0))
    small = gapped("abab", {}, y=600, size=12, font="F", char_offsets=(0.0, 6.0, 12.0, 18.0, 24.0))
    assert texts(L.lines(numbered([big, small]))) == ["abab", "abab"]


def test_tj_kerning_gives_no_space():
    assert texts(L.lines(numbered([gapped("AVoid", {1: -1.5, 2: 1.0})]))) == ["AVoid"]


def test_tj_gap_not_doubled_beside_space():
    assert texts(L.lines(numbered([gapped("a b", {1: 4.0, 2: 4.0})]))) == ["a b"]


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


LONG = "so the first line of the paragraph runs on to "  # a first line longer than the next, so the next one wrapped


def _paras(*lines):
    """The paragraph texts of lines set 14 apart, one paragraph's spacing."""
    fr = [frag(t, 72, 700 - 14 * k) for k, t in enumerate(lines)]
    return [para_text(p) for p in L.paragraphs(L.lines(numbered(fr)), L.SpacingModel())]


def test_ended_line_ends_paragraph():
    # at one pitch, a line the next first word would have fitted after was ended, not wrapped: a
    # heading over its text, the items of a list; a line that wrapped joins the next
    assert _paras("Accuracy of information",
                  "All information will be used to assess if the applicant is",
                  "entitled to a certificate. An application may:",
                  "o Result in rejection of the application",
                  "o Raise doubts about the ability of the applicant to",
                  "control the operation",
                  "o Result in delays") == [
        "Accuracy of information",
        "All information will be used to assess if the applicant is entitled to a certificate. An"
        " application may:",
        "o Result in rejection of the application",
        "o Raise doubts about the ability of the applicant to control the operation",
        "o Result in delays"]
    # items of one length, each set off by its marker
    assert _paras("Here is a numbered list:", "1) Number bullet 1", "2) Number bullet 2", "3) Number bullet 3") == [
        "Here is a numbered list:", "1) Number bullet 1", "2) Number bullet 2", "3) Number bullet 3"]
    assert _paras("• Bullet one", "• Bullet two") == ["• Bullet one", "• Bullet two"]


def test_piece_that_goes_on_along_the_line_stays_in_its_paragraph():
    from dataclasses import replace
    fr = [frag(t, 72, 700 - 14 * k) for k, t in zip(
        (0, 1, 1, 2), ["A long first line of the paragraph that fills the column up", "risk of pregnancies.2",
                       "x", "and the last line of it."])]
    lns = [L.lines(numbered([f]))[0] for f in fr]
    for text, dy in ((",3 It is, therefore, important to know more than that", 0.007),
                     ("1) to our stockholders over the same period of time", -0.007)):
        on = replace(lns[2], text=text, runs=[(text, "")],
                     box=(lns[1].box[2] - 0.1, lns[1].box[1] - dy, 426.0, lns[1].box[3] - dy))
        got = [para_text(p) for p in L.paragraphs(L._whole([lns[0], lns[1], on, lns[3]]), L.SpacingModel())]
        assert len(got) == 1 and "pregnancies.2" + text[:2] in got[0], got
    far = replace(lns[2], box=(lns[1].box[2] + 60, lns[1].box[1], 400.0, lns[1].box[3]))
    assert len(L.paragraphs(L._whole([lns[0], lns[1], far, lns[3]]), L.SpacingModel())) > 1


def test_joined_line_has_the_size_of_most_of_its_text():
    from dataclasses import replace
    big = L.lines(numbered([frag("23. ", 72, 700)]))[0]
    body = replace(big, text="words of the body in the common size", runs=[("words of the body in the common size", "")],
                   box=(big.box[2] + 0.5, big.box[1], 400.0, big.box[3]), size=big.size / 2)
    got = L._whole([big, body])
    assert len(got) == 1 and got[0].size == body.size and got[0].text.startswith("23."), got
    pieces, x = [], 72.0
    for text, size in (("a body piece", 10.0), ("A Larger Title", 20.0), ("another piece", 10.0)):
        pieces.append(replace(big, text=text, runs=[(text, "")], box=(x, big.box[1], x + 5 * len(text), big.box[3]),
                              size=size))
        x += 5 * len(text) + 0.5
    got = L._whole(pieces)
    assert len(got) == 1 and got[0].size == 10.0, got


def test_toc_entry_ends_paragraph():
    # evenly spaced table-of-contents entries: each one ends at its leader and page label
    assert _paras("Introduction ........ 1", "Scope · · · ··········· 12",
                  "Appendix . . . . iv") == ["Introduction ........ 1",
                                             "Scope · · · ··········· 12", "Appendix . . . . iv"]
    assert _paras("Terms ____ 3", "Index ‥‥…5", "Notes .... XII") == [
        "Terms ____ 3", "Index ‥‥…5", "Notes .... XII"]
    # the line after an entry starts its own paragraph, and it joins the lines after it
    assert _paras("Contents ..... 9", "Body text goes", "on here.") == [
        "Contents ..... 9", "Body text goes on here."]


def test_toc_leader_forms():
    # a leader counts dots, 4 or more (… is 3, ‥ is 2), its marks mixed or a single space
    # apart; the page label may be a chapter page or set between hyphens
    for entry in ("第一章 総則‥‥3", "序論・・・・3", "제1장 서론 …… 1", "．．．．5",
                  "Scope · · · · 7", "Part … … 2", "Index ....\u30008", "Index ....\t\xa08",
                  "Setup ...... 3-1", "Annex ...... A-1", "Preface ...... - 3 -",
                  "Annex ...... -A-1-"):
        assert _paras(entry, "the text goes on") == [entry, "the text goes on"], entry


def test_toc_ending_needs_leader_and_label():
    # a prose ellipsis is 3 dots, short of a leader: the lines join
    for line in ("Well... I", "values 1, 2, ... 10", "and so on ... iv", "pages ‥. 4"):
        assert _paras(LONG + line, "the text goes on") == [LONG + line + " the text goes on"], line
    # dots with no page label, a number with no leader, or too short a leader: the lines join
    for line in ("and so on...", "chapter 12", "as in Fig.. 3", "so . . 3", "and then... did"):
        assert _paras(LONG + line, "the text goes on") == [LONG + line + " the text goes on"], line


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
    down = dict(dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=12.0, vertical=True)
    fr = [frag("縦", 480, 700, **down), frag("書", 480, 688, **down),
          frag("日", 500, 700, **down), frag("本", 500, 688, **down)]
    out = L.vertical_lines(numbered(fr))
    assert texts(out) == ["日本", "縦書"] and all(ln.vertical for ln in out)
    assert texts(L.lines(numbered(fr))) == ["日本", "縦書"]
    # Latin running down the page in a horizontal font is a line of its own, spaced by its gaps
    down["vertical"] = False
    latin = [frag("Down", 300, 700, **dict(down, width=24.0)),
             frag("page", 300, 673, **dict(down, width=24.0))]
    out = L.lines(numbered(latin))
    assert texts(out) == ["Down page"] and not out[0].vertical, texts(out)


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
    assert L.rotate_into_view([f], 90.0, box)[0].x == 20
    assert L.rotate_into_view([f], True, box)[0].x == 10
    assert L.rotate_into_view([f], 450, box)[0].x == 20


def test_far_apart_runs_on_one_baseline():
    # dochan's line builder is not in 1.21.0's layout.py; see the report for what it does
    out = L.lines(numbered([frag("left", 72, 700), frag("right", 360, 700)]))
    assert texts(out) == ["left right"]


def test_heading_by_size():
    assert L.heading_level("Chapter one", 15, 10) == 3
    assert L.heading_level("Chapter one", 12.5, 10) == 4
    assert L.heading_level("Chapter one", 12, 10) is None
    assert L.heading_level("x" * 130, 15, 10) is None
    assert L.heading_level("1.", 15, 10) is None
    lns = L.lines(numbered([frag("Body text of the page that goes on and on " * 3, 72, 700 - 14 * k) for k in range(60)]
                           + [frag("Section two of the book", 72, 760, size=14)]))
    floor = L.heading_floor(lns, L.body_size(lns))
    assert floor == 14 and L.heading_level("Section two of the book", 14, 12, floor) == 4, floor
    assert L.heading_level("x" * 61, 14, 12, floor) is None and L.heading_level("Section", 13, 12, floor) is None
    many = L.lines(numbered([frag("Body text of the page that goes on and on " * 3, 72, 700 - 14 * k) for k in range(60)]
                            + [frag("A quoted part set a little larger " * 3, 300, 700 - 16 * k, size=14) for k in range(9)]))
    assert L.heading_floor(many, 12) == 15, L.heading_floor(many, 12)


def test_body_size():
    lns = L.lines(numbered([frag("a b", 72, 700, size=10), frag("c d", 72, 686, size=10),
                            frag("T T", 72, 740, size=20)]))
    assert L.body_size(lns) == 10 and L.body_size([]) == 0.0
    # the size most chars are set in: cutting small text into many lines (a tagged table's cells) moves no body
    cells = [frag("%d" % k, 72 + 30 * k, 600, size=8) for k in range(6)]
    lns = L.lines(numbered([frag("A body line that holds most of the text", 72, 700, size=10)])) + [
        ln for c in cells for ln in L.lines(numbered([c]))]
    assert L.body_size(lns) == 10, [ln.size for ln in lns]


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


def _tight_line(words, y, size=10.0, word_gap=0.14, kern=-0.02):
    """One fragment of words set with no space glyphs: each glyph 0.5 em wide, a kern between
    letters and `word_gap` em between words, as a tightly set newspaper column draws them."""
    text, offsets, gaps, pos = "", [], [], 0.0
    for w, word in enumerate(words):
        for k, ch in enumerate(word):
            step = (word_gap if w and not k else kern if text else 0.0) * size
            pos += step
            text += ch
            offsets.append(pos)
            gaps.append(step)
            pos += 0.5 * size
    offsets.append(pos)
    return frag(text, 72, y, size=size, width=pos, char_offsets=tuple(offsets), char_gaps=tuple(gaps))


def test_tight_line_keeps_spaces():
    rows = [["Heute", "lest", "ihr", "die", "letzte", "Ausgabe"],
            ["der", "Pendler", "zeitung", "zwanzig", "Minuten"],
            ["Damit", "geht", "ein", "Stück", "Geschichte", "zu", "Ende"]]
    fr = [_tight_line(r, 700 - 12 * k) for k, r in enumerate(rows)]
    assert texts(L.lines(numbered(fr))) == [" ".join(r) for r in rows]
    assert L.GAP_FLOOR <= L.word_gap(fr) < 0.14
    loose = [_tight_line(r, 700 - 12 * k, word_gap=0.3, kern=0.0) for k, r in enumerate(rows)]
    assert texts(L.lines(numbered(loose))) == [" ".join(r) for r in rows]
    assert L.word_gap(loose[:1]) == L.GAP


def test_letter_spaced_page_splits_its_own_gaps():
    rows = [["WORD", "ONE", "TWO"], ["MORE", "WORDS", "HERE"], ["AND", "THE", "LAST", "ROW"]]
    fr = [_tight_line(r, 700 - 14 * k, size=12.0, word_gap=0.4, kern=0.1) for k, r in enumerate(rows)]
    assert texts(L.lines(numbered(fr))) == [" ".join(r) for r in rows]


def test_word_fragments_keep_the_fixed_gap():
    # one fragment per word, as OCR gives them: every gap is a word gap, none a letter gap, so the
    # most common one parts words too
    fr, x = [], 72.0
    words = "the quick brown fox jumps over a lazy dog and runs".split()
    for k in range(60):
        w = words[k % len(words)]
        fr.append(frag(w, x, 700 - 14 * (k // 6)))
        x = 72.0 if k % 6 == 5 else x + len(w) * 6 + 12 * (0.28 + 0.02 * (k % 5))
    assert L.word_gap(numbered(fr)) == L.GAP
    assert texts(L.lines(fr))[0] == "the quick brown fox jumps over", texts(L.lines(fr))


def test_own_spaces_keep_words_whole():
    # a page whose words are set apart by space glyphs: its other gaps are letter spacing, here
    # glyphs placed one by one with up to 0.15 em of jitter, and split no word
    rows = ["ODBC DRIVER CALL FAILED", "PARTITION VIEW TEST REPORT", "BITMAP INDEX AND SPACE",
            "CORRUPTED BLOCK OBJECT FINDER", "DATABASE INTERNAL RECOVERY"]
    fr = []
    for k, row in enumerate(rows):
        jitter = [0.02, -0.01, 0.0, 0.15, 0.01, -0.09, 0.03, 0.0]
        text, offsets, gaps, pos = "", [], [], 0.0
        for i, ch in enumerate(row):
            step = 0.0 if not text or ch == " " or text[-1] == " " else jitter[i % len(jitter)] * 10
            pos += step
            text += ch
            offsets.append(pos)
            gaps.append(step)
            pos += (0.25 if ch == " " else 0.5) * 10
        offsets.append(pos)
        fr.append(frag(text, 72, 700 - 12 * k, size=10.0, width=pos, char_offsets=tuple(offsets),
                       char_gaps=tuple(gaps)))
    assert texts(L.lines(numbered(fr))) == rows
    assert L.word_gap(fr) == L.GAP


def test_loose_leading_is_one_paragraph():
    text = ["이 편람은 1991년 사무관리규정 제정 시", "처음 발간된 이후 여러 차례", "개정되어 왔다.",
            "둘째 문단은 여기서", "시작한다."]
    ys = [700, 681, 662, 633, 614]
    # justified: a line that wraps runs to the measure
    fr = [frag(t, 72, y, size=12, **({"width": 132.0} if t[-1] != "." else {})) for t, y in zip(text, ys)]
    out = [para_text(p) for p in L.paragraphs(L.lines(numbered(fr)), L.SpacingModel())]
    assert len(out) == 2, out
    assert out[0].endswith("개정되어 왔다.") and out[1].startswith("둘째")


def test_spaced_out_lines_stay_apart():
    # form labels 3 sizes apart are items, not a paragraph in loose leading: no pitch is measured from them
    fr = [frag(t, 72, 700 - 36 * k, size=12) for k, t in enumerate(("Check boxes: A B", "Radio buttons: yes no",
                                                                     "Drop-down:", "Signature:"))]
    lns = L.lines(numbered(fr))
    assert L.pitch(lns) == (L.LEADING, L.LEADING)
    assert len(L.paragraphs(lns, L.SpacingModel(), L.pitch(lns))) == 4


def test_pitch_measured_by_text_not_by_stray_lines():
    body = [frag("body line number %d of the page" % k, 72, 700 - 19 * k) for k in range(4)]
    tab = [frag(ch, 560, 600 - 9 * k, size=9) for k, ch in enumerate("행정업무운영개요")]
    lns = L.lines(numbered(body + tab))
    assert abs(L.pitch(lns)[0] - 19 / 12) < 0.05
    assert len(L.paragraphs(lns[:4], L.SpacingModel(), L.pitch(lns))) == 1


def test_wrapped_heading_is_one_heading():
    fr = [frag("A Study of Stress-", 72, 740, size=20), frag("Induced Failures", 72, 716, size=20)]
    fr += [frag("body text of the paper goes on here", 72, 680 - 14 * k) for k in range(4)]
    lns = L.lines(numbered(fr))
    out = L.place(lns, [], {0: 3, 1: 3}, L.SpacingModel())
    assert [type(b).__name__ for b in out] == ["Heading", "Para"]
    assert "".join(t.text for t in out[0].inlines) == "A Study of Stress- Induced Failures"


def test_math_alphanumerics_fold():
    out = L.lines(numbered([frag("𝑉ℎ𝑖𝑔ℎ = 𝐀𝟏", 72, 700)]))
    assert texts(out) == ["Vhigh = A1"]


def _paged(n, header):
    fr = [frag(header, 72, 760), frag(f"Downloaded from repo.example.org on {n} May", 20, 200,
                                       dir_x=0.0, dir_y=1.0, up_x=-1.0, up_y=0.0, width=200.0)]
    for i in range(3):
        fr.append(frag(f"body {n} line {i} of the text", 72, 700 - 14 * i))
    return L.lines(numbered(fr))


def test_running_header_with_page_number_inside():
    pages = [_paged(n, f"{44 + n} Smith et al., 2020, Vol. {n % 2 + 3}") for n in range(1, 7)]
    headers, footers, drops = L.running(pages, [(0, 0, 612, 792)] * 6)
    for lns, drop in zip(pages, drops):
        kept = [ln.text for i, ln in enumerate(lns) if i not in drop]
        assert all(t.startswith("body") for t in kept), kept
    assert headers[0].startswith("45 Smith")


def test_vertical_column_keeps_punctuation():
    down = dict(dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=8.0, vertical=True)
    col = []
    for k, ch in enumerate("中「含む」を"):
        kw = down if ch in "「」" else {}
        col.append(frag(ch, 421, 614 - 8 * k, size=8, **({"width": 8.0} | kw)))
    col += [frag(ch, 384, 614 - 8 * k, size=8, width=8.0) for k, ch in enumerate("以下")]
    assert texts(L.lines(numbered(col))) == ["中「含む」を", "以下"]


def test_vertical_tiers():
    # two tiers of columns parted by one empty row: each tier's columns are read before the next
    # tier's; a column's own empty cell does not part it
    down = dict(dir_x=0.0, dir_y=-1.0, up_x=1.0, up_y=0.0, width=8.0, vertical=True)
    cells = {421: {614: "上", 606: "段", 598: "一", 582: "続", 566: "下", 558: "段", 550: "一"},
             410: {614: "上", 606: "段", 598: "二", 590: "終", 582: "わ", 566: "下", 558: "段", 550: "二"}}
    fr = [frag(ch, x, y, size=8, **down) for x, col in cells.items() for y, ch in col.items()]
    assert texts(L.lines(numbered(fr))) == ["上段一続", "上段二終わ", "下段一", "下段二"], texts(L.lines(fr))


def test_scale():
    fr = [frag("w%d" % i, 72 + (i % 40) * 14, 700 - (i // 40) * 14) for i in range(60000)]
    t = time.perf_counter()
    lns = L.lines(numbered(fr))
    L.reading_order(lns, 612)
    L.paragraphs(lns, L.SpacingModel())
    assert time.perf_counter() - t < 5


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
