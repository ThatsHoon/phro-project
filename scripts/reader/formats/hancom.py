"""Helpers shared by the Hancom parsers (hwpx and hwp): equation scripts, numbering markers, the cell grid."""
import re
from bisect import bisect_left

from ..model import Cell

AUTO_ALT = re.compile(r"^(그림|사각형|선|타원|글상자|표|수식)입니다\.?$")
STYLE_HEAD = re.compile(r"^(?:개요|outline|heading)\s*(\d+)$", re.I)

# HWP 5.0 binary numbering-format code (the index) to the hwpx numFormat name; anything else is DIGIT
NUM_KINDS = ("DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL", "LATIN_SMALL",
             "CIRCLED_LATIN_CAPITAL", "CIRCLED_LATIN_SMALL", "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE",
             "HANGUL_JAMO", "CIRCLED_HANGUL_JAMO", "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH")

_SYLLABLE = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_PHONETIC = "일이삼사오육칠팔구십"
_IDEOGRAPH = "一二三四五六七八九十"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi pi rho sigma "
          "tau upsilon phi chi psi omega").split()
_GREEK_CAP = ("Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega")
_WORDS = {"times": "\\times", "le": "\\le", "ge": "\\ge",
          **{g: "\\" + g for g in _GREEK}, **{g: "\\" + g for g in _GREEK_CAP}}
_TEX_DEPTH = 32
# kind -> (first code point, how many)
_CIRCLED = {"CIRCLED_DIGIT": (0x2460, 20), "CIRCLED_LATIN_CAPITAL": (0x24B6, 26),
            "CIRCLED_LATIN_SMALL": (0x24D0, 26), "CIRCLED_HANGUL_SYLLABLE": (0x326E, 14),
            "CIRCLED_HANGUL_JAMO": (0x3260, 14), "CIRCLED_IDEOGRAPH": (0x3280, 10)}


# ---- equation script to TeX
def _operand(toks, i, depth):
    """One operand starting at toks[i]: ((text, inner-or-None), next index)."""
    if i >= len(toks):
        return ("", None), i
    if toks[i] == "{" and depth < _TEX_DEPTH:
        items, j = _group(toks, i + 1, depth + 1)
        inner = _join(items)
        return ("{" + inner + "}", inner), j
    t = toks[i]
    return (_WORDS.get(t, t), None), i + 1


def _group(toks, i, depth):
    out = []
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "}":
            if depth:
                return out, i
            continue
        if t == "{" and depth < _TEX_DEPTH:
            items, i = _group(toks, i, depth + 1)
            inner = _join(items)
            out.append(("{" + inner + "}", inner))
        elif t == "over" and out and i < len(toks):
            (den, dinner), i = _operand(toks, i, depth)
            s, inner = out.pop()
            out.append((f"\\frac{{{inner if inner is not None else s}}}{{{dinner if dinner is not None else den}}}", None))
        elif t == "sqrt" and i < len(toks):
            (s, inner), i = _operand(toks, i, depth)
            out.append((f"\\sqrt{{{inner if inner is not None else s}}}", None))
        else:
            out.append((_WORDS.get(t, t), None))
    return out, i


def _join(items):
    out = ""
    for s, _ in items:
        out += s if not out or s in "^_" or out[-1] in "^_" else " " + s
    return out


def tex(script: str) -> str:
    toks = re.findall(r"[{}^_]|[^\s{}^_]+", script or "")
    items, _ = _group(toks, 0, 0)
    return _join(items)


# ---- numbering markers
def _roman(n):
    if not 0 < n < 4000:
        return str(n)
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def _bijective(n, alphabet):
    out = ""
    while n > 0:
        n, r = divmod(n - 1, len(alphabet))
        out = alphabet[r] + out
    return out or str(n)


def number(n: int, kind: str) -> str:
    if n < 1:
        return str(n)
    if kind in _CIRCLED:
        base, count = _CIRCLED[kind]
        return chr(base + n - 1) if n <= count else str(n)
    if kind == "ROMAN_CAPITAL":
        return _roman(n)
    if kind == "ROMAN_SMALL":
        return _roman(n).lower()
    if kind == "LATIN_CAPITAL":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "LATIN_SMALL":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "HANGUL_SYLLABLE":
        return _bijective(n, _SYLLABLE)
    if kind == "HANGUL_JAMO":
        return _bijective(n, _JAMO)
    if kind == "HANGUL_PHONETIC":
        return _PHONETIC[n - 1] if n <= len(_PHONETIC) else str(n)
    if kind == "IDEOGRAPH":
        return _IDEOGRAPH[n - 1] if n <= len(_IDEOGRAPH) else str(n)
    return str(n)


# ---- table cell grid
def place(cells: list[tuple[int, int, int, int, Cell]]) -> list[list[Cell]]:
    """(row, col, rowspan, colspan, cell) addresses to rows of cells.

    The grid keeps only the rows and columns where a cell starts; a span counts the kept ones it covers.
    Sets each cell's rowspan and colspan.
    """
    rows_at, cols_at = sorted({x[0] for x in cells}), sorted({x[1] for x in cells})
    ri = {v: i for i, v in enumerate(rows_at)}
    ci = {v: i for i, v in enumerate(cols_at)}
    grid = [[] for _ in rows_at]
    for row, col, rs, cs, cell in cells:
        r, c = ri[row], ci[col]
        cell.rowspan = bisect_left(rows_at, row + rs) - r
        cell.colspan = bisect_left(cols_at, col + cs) - c
        grid[r].append((c, cell))
    return [[cell for _, cell in sorted(g, key=lambda x: x[0])] for g in grid]
