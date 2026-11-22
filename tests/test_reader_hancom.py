"""Shared Hancom helpers: equation script to TeX, numbering markers, the table cell grid and the paragraph rules.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reader.formats import hancom  # noqa: E402
from reader.model import Bold, Break, Cell, Equation, Hidden, Link, Math, Text  # noqa: E402


def test_number_kinds():
    n = hancom.number
    assert n(3, "ROMAN_SMALL") == "iii" and n(4, "ROMAN_CAPITAL") == "IV"
    assert n(27, "LATIN_CAPITAL") == "AA" and n(2, "LATIN_SMALL") == "b"
    assert n(2, "HANGUL_SYLLABLE") == "나" and n(2, "HANGUL_JAMO") == "ㄴ"
    assert n(2, "CIRCLED_DIGIT") == "②" and n(21, "CIRCLED_DIGIT") == "21"
    assert n(2, "CIRCLED_LATIN_CAPITAL") == "Ⓑ" and n(26, "CIRCLED_LATIN_CAPITAL") == "Ⓩ"
    assert n(27, "CIRCLED_LATIN_CAPITAL") == "27"
    assert n(2, "CIRCLED_LATIN_SMALL") == "ⓑ" and n(27, "CIRCLED_LATIN_SMALL") == "27"
    assert n(2, "CIRCLED_HANGUL_SYLLABLE") == "㉯" and n(14, "CIRCLED_HANGUL_SYLLABLE") == "㉻"
    assert n(15, "CIRCLED_HANGUL_SYLLABLE") == "15"
    assert n(2, "CIRCLED_HANGUL_JAMO") == "㉡" and n(15, "CIRCLED_HANGUL_JAMO") == "15"
    assert n(3, "HANGUL_PHONETIC") == "삼" and n(10, "HANGUL_PHONETIC") == "십" and n(11, "HANGUL_PHONETIC") == "11"
    assert n(3, "IDEOGRAPH") == "三" and n(10, "IDEOGRAPH") == "十" and n(11, "IDEOGRAPH") == "11"
    assert n(3, "CIRCLED_IDEOGRAPH") == "㊂" and n(11, "CIRCLED_IDEOGRAPH") == "11"
    assert n(5, "DIGIT") == "5" and n(5, "WEIRD") == "5" and n(0, "ROMAN_SMALL") == "0"


def test_num_kinds_order():
    k = hancom.NUM_KINDS
    assert len(k) == 15 and len(set(k)) == 15
    assert k[0] == "DIGIT" and k[1] == "CIRCLED_DIGIT" and k[8] == "HANGUL_SYLLABLE"
    assert k[10] == "HANGUL_JAMO" and k[14] == "CIRCLED_IDEOGRAPH"
    for kind in k[1:]:  # every kind is known to number(): none falls back to digits for 2
        assert hancom.number(2, kind) != "2", kind


def test_tex_moved():
    tex = hancom.tex
    assert tex("a over b") == r"\frac{a}{b}"
    assert tex("{a+b} over {c}") == r"\frac{a+b}{c}"
    assert tex("sqrt x") == r"\sqrt{x}"
    assert tex("sqrt {x+1}") == r"\sqrt{x+1}"
    assert tex("x^2 + y_1") == "x^2 + y_1"
    assert tex("a times b le c ge d") == r"a \times b \le c \ge d"
    assert tex("alpha + Omega + omega") == r"\alpha + \Omega + \omega"
    assert tex("foo baz") == "foo baz"
    assert tex("{" * 500 + "a" + "}" * 500) is None  # past limits.TEX_DEPTH groups the script is not translated


def test_tex_constructs():
    tex = hancom.tex
    # a script binds tighter than over, which takes the one unit before it and the one after it
    assert tex("x_i over n") == r"\frac{x_i}{n}"
    assert tex("a^2 over b_1") == r"\frac{a^2}{b_1}"
    assert tex("1 + a over b") == r"1 + \frac{a}{b}"
    assert tex("LEFT ( a over b RIGHT )") == r"\left( \frac{a}{b} \right)"
    assert tex("left { x right }") == r"\left\{ x \right\}"
    assert tex("matrix{a & b # c & d}") == r"\begin{matrix}a & b \\ c & d\end{matrix}"
    assert tex("pmatrix{1 & 0 # 0 & 1}") == r"\begin{pmatrix}1 & 0 \\ 0 & 1\end{pmatrix}"
    assert tex("cases{1 & x>0 # 0 & x<=0}") == r"\begin{cases}1 & x>0 \\ 0 & x\le0\end{cases}"
    assert tex("root 3 of x") == r"\sqrt[3]{x}"
    assert tex("sum from{i=1} to {n} x_i") == r"\sum_{i=1}^n x_i"
    assert tex("int _0 ^1 f(x) dx") == r"\int_0^1 f(x) dx"
    assert tex("lim from {x -> 0} {sin x} over x") == r"\lim_{x \rightarrow 0} \frac{\sin x}{x}"
    assert tex("bar x + vec {AB}") == r"\bar{x} + \overrightarrow{AB}"
    assert tex('"kg" cdot m') == r"\text{kg} \cdot m"
    assert tex("한글 x") == r"\text{한글} x"
    assert tex("") == "" and tex("  ") == ""


def test_tex_refuses_what_it_cannot_translate():
    # None, never a guess: chained over, a font switch that reaches past its group, a line break, an operand without
    # braces before a script or over, from/to after anything but a big operator, an unknown or mixed-case keyword, a
    # cases with three columns, an unclosed group, a double script
    for s in ("a over b over c", "rm{kg} cdot m", "x # y", "a & b", "sqrt x over 2", "bar x _1", "a from b",
              "Alpha", "Over", "alphabet", "cases{a & b & c}", "x_{i", "x^2^3", "a<<b", "left < x right >"):
        assert hancom.tex(s) is None, s


def test_equation_inline():
    assert hancom.equation("a over b") == Math(r"\frac{a}{b}") and hancom.equation(" ") is None
    assert hancom.equation("a over b over c") == Text("`a over b over c`")
    assert hancom.equation("a`b  #\n c") == Text("``a`b # c``")
    assert hancom.code_span("`x") == "`` `x ``" and hancom.code_span("x``") == "``` x`` ```"


def test_regexes_moved():
    assert hancom.AUTO_ALT.match("그림입니다.") and not hancom.AUTO_ALT.match("그림 설명")
    assert hancom.STYLE_HEAD.match("개요 2").group(1) == "2" and not hancom.STYLE_HEAD.match("본문")


def test_alt_text_drops_the_automatic_description():
    auto = "그림입니다.\r\n원본 그림의 이름: a.bmp\r\n원본 그림의 크기: 가로 10pixel, 세로 7pixel\r\n사진 찍은 날짜: 2019년"
    assert hancom.alt_text(auto) == "" and hancom.alt_text("그림입니다.") == ""
    assert hancom.alt_text(auto + "\n회사  로고") == "회사 로고"
    assert hancom.alt_text("설명: 회사 로고\n둘째 줄") == "설명: 회사 로고 둘째 줄" and hancom.alt_text("") == ""
    # only the labels Hancom writes are dropped: a line the author wrote in the same form stays
    assert hancom.alt_text("그림입니다.\n출처: 통계청 2024") == "출처: 통계청 2024"
    assert hancom.alt_text("그림입니다. 원본 그림의 이름: a.bmp 원본 그림의 크기: 가로 1pixel, 세로 1pixel") == ""
    assert hancom.alt_text("그림입니다. 회사 로고") == "회사 로고"
    assert hancom.alt_text("선입니다만 굵다") == "선입니다만 굵다" and hancom.alt_text("표입니다") == ""


def test_props_reads_hancoms_property_string():
    p = hancom.props("CommonSet:set:60:Name:wstring:2:c1 Caption:wstring:5:a: b  Value:int:1 Enabled:bool:1")
    assert p == {"Name": "c1", "Caption": "a: b ", "Value": "1", "Enabled": "1"}, p
    assert hancom.props("") == {} and hancom.props("Text") == {} and hancom.props("Text:wstring") == {}
    assert hancom.props("Text:wstring:٣:abc") == {}
    assert len(hancom.props("K:int:1 " * 200_000)) == 1
    assert hancom.props("A:int:1 Text:wstring:" + "9" * 5000 + ":abc") == {"A": "1"}


def test_button_states():
    assert hancom.button(1, "동의") == "☑ 동의" and hancom.button(0, " 거부 ") == "☐ 거부"
    assert hancom.button(2, "보류") == "☐ 보류 (indeterminate)" and hancom.button(1, "") == "☑"
    assert hancom.button(0, "IP R&&D연계") == "☐ IP R&D연계" and hancom.button(1, "확인(&O)") == "☑ 확인(O)"


def test_paragraph_writes_every_number_it_takes():
    # an outline number leads a heading and a demoted paragraph; a numbered paragraph that is a heading by its style
    # writes its marker; a bullet never leads a heading; an empty paragraph gives no block
    h = hancom.paragraph([Text("제목")], "제목", 2, "가.")
    assert h.level == 2 and h.inlines == [Text("가. "), Text("제목")]
    assert hancom.paragraph([Text("가. 본문")], "가. 본문", 0, "가.").inlines == [Text("가. "), Text("본문")]
    assert hancom.paragraph([Text("x")], "x", 1, None, (0, "3)", "3)")).inlines == [Text("3) "), Text("x")]
    assert hancom.paragraph([Text("x")], "x", 1, None, (0, "-", "-")).inlines == [Text("x")]
    item = hancom.paragraph([Text("● y")], "● y", 0, None, (1, "-", "●"))
    assert (item.level, item.marker, item.inlines) == (1, "-", [Text("y")])
    assert hancom.paragraph([], "", 0, "1.") is None and hancom.paragraph([Text(" ")], "", 0, "1.").inlines == [Text(" ")]
    assert hancom.outline_marker({1: ("", "DIGIT", 1)}, {}, 0) is None
    cnt = {}
    assert hancom.outline_marker({1: ("^1.", "DIGIT", 1)}, cnt, 0) == "1." and cnt == {0: 1}


def test_place_moved():
    a, b, c = Cell([]), Cell([]), Cell([])
    rows = hancom.place([(0, 0, 1, 2, a), (1, 0, 1, 1, b), (1, 1, 1, 1, c)])
    assert [[x is y for x, y in zip(r, w)] for r, w in zip(rows, [[a], [b, c]])] == [[True], [True, True]]
    assert [len(r) for r in rows] == [1, 2] and a.colspan == 2 and a.rowspan == 1 and b.colspan == 1
    # column 1 never starts a cell, so it is dropped; a span counts only the kept columns it covers
    a, b, c, d, e = (Cell([]) for _ in range(5))
    rows = hancom.place([(0, 0, 1, 3, a), (0, 3, 1, 1, b), (1, 0, 1, 2, c), (1, 2, 1, 1, d), (1, 3, 1, 1, e)])
    assert [len(r) for r in rows] == [2, 3] and rows[1][2] is e and rows[0][1] is b
    assert [x.colspan for x in rows[0]] == [2, 1] and [x.colspan for x in rows[1]] == [1, 1, 1]
    assert hancom.place([]) == []


def test_lone_equations_leave_at_any_depth():
    a, b, c, d = Math("a"), Math("b"), Math("c"), Math("d")
    inl = [Text(" "), a, Bold([b, Text(" ")]), Link("u", [c]), Hidden([d]), Break()]
    assert hancom.lone_equations(inl, [a, b, c]) == [Equation("a"), Equation("b"), Equation("c")]
    assert inl == [Text(" "), Bold([Text(" ")]), Link("u", []), Hidden([d]), Break()], inl
    # any visible text keeps them inline
    inl = [a, Bold([Text("x")])]
    assert hancom.lone_equations(inl, [a]) == [] and inl == [a, Bold([Text("x")])]
    assert hancom.lone_equations([Text("y")], []) == []


def test_lone_equations_skip_hidden():
    # an equation inside Hidden (a tracked deletion) is not visible: it is neither shown as a display equation nor
    # moved out of its Hidden, even when it is the only equation of the paragraph
    a, b = Math("a"), Math("b")
    inl = [Hidden([a], "deleted")]
    assert hancom.lone_equations(inl, [a]) == [] and inl == [Hidden([Math("a")], "deleted")], inl
    assert inl[0].inlines[0] is a
    # a visible one beside it still leaves, the hidden one stays where it is
    inl = [Hidden([Bold([Text(" ")]), a], "deleted"), Link("u", [b])]
    assert hancom.lone_equations(inl, [a, b]) == [Equation("b")]
    assert inl == [Hidden([Bold([Text(" ")]), a], "deleted"), Link("u", [])], inl


def test_lone_equations_one_pass():
    eqs = [Math("x") for _ in range(300_000)]
    inl = list(eqs)
    t0 = time.monotonic()
    out = hancom.lone_equations(inl, eqs)
    took = time.monotonic() - t0
    assert inl == [] and len(out) == 300_000 and took < 2, took


def test_paragraph_rules():
    lv = hancom.style_name_level
    assert (lv("개요 3"), lv("Heading 9"), lv("제목"), lv("Subtitle"), lv("본문")) == (3, 6, 1, 2, 0)
    hl = hancom.heading_level
    assert hl(2, "짧은 제목") == 2 and hl(2, "x" * 121) == 0 and hl(2, "x" * 41 + ".") == 0 and hl(0, "") == 0
    assert hl(1, "x" * 39 + ".") == 1
    assert hancom.cut_command(r"https://e.com/a\;b;1;0") == "https://e.com/a;b"
    assert hancom.plain([Text(" a \n"), Bold([Text("b")]), Break(), Text("c ")]) == "a b c"


def test_next_marker():
    levels = {1: ("^1.", "DIGIT", 1), 2: ("^1.^2)", "HANGUL_SYLLABLE", 3)}
    cnt = {}
    got = [hancom.next_marker(levels, cnt, lvl) for lvl in (0, 1, 1, 0, 1, 2)]
    # a level without a format falls back to ^N. with digits and start 1
    assert got == ["1.", "1.다)", "1.라)", "2.", "2.다)", "1."], got
    assert hancom.next_marker({1: ("", "DIGIT", 5)}, {}, 0) == "5." and hancom.next_marker({}, {}, 0) == "1."
    assert hancom.next_marker({1: ("  ", "DIGIT", 1)}, {}, 0) == "-"  # a format of blanks gives no number


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
