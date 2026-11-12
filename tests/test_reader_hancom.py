"""Shared Hancom helpers: equation script to TeX, numbering markers and the table cell grid.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reader.formats import hancom  # noqa: E402
from reader.model import Cell  # noqa: E402


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
    assert tex("foo bar") == "foo bar"
    assert tex("{" * 500 + "a" + "}" * 500)


def test_regexes_moved():
    assert hancom.AUTO_ALT.match("그림입니다.") and not hancom.AUTO_ALT.match("그림 설명")
    assert hancom.STYLE_HEAD.match("개요 2").group(1) == "2" and not hancom.STYLE_HEAD.match("본문")


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
