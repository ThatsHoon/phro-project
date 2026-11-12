"""Helpers shared by the Hancom parsers (hwpx and hwp): equation scripts, numbering markers, the cell grid, and the
inline model and paragraph rules both readers build paragraphs with."""
import re
from bisect import bisect_left

from .. import limits
from ..model import Bold, Break, Cell, Equation, Heading, Hidden, Link, ListItem, Math, NoteRef, Para, Table, Text

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


def next_marker(levels: dict, counters: dict, lvl: int) -> str:
    """Advance one numbering's counters to level `lvl` (0-based) and expand that level's format.

    levels: {level (1-based): (format, number kind, start)}; counters: {level (0-based): count}, one dict per
    numbering id, kept by the reader. ^N in the format is level N's count in level N's kind; no format means "^N.".
    """
    head = levels.get(lvl + 1)
    counters[lvl] = counters.get(lvl, (head[2] if head else 1) - 1) + 1
    for k in [k for k in counters if k > lvl]:
        del counters[k]

    def sub(m):
        n = int(m.group(1))
        h = levels.get(n)
        return number(counters.get(n - 1, h[2] if h else 1), h[1] if h else "DIGIT")

    fmt = head[0] if head and head[0] else f"^{lvl + 1}."
    return re.sub(r"\^(\d)", sub, fmt).strip()[:64] or "-"


# ---- the inline model
def itext(inlines) -> str:
    """The visible text: Bold and Link opened, a Break as a space."""
    out = ""
    for it in inlines:
        if isinstance(it, Text):
            out += it.text
        elif isinstance(it, (Bold, Link)):
            out += itext(it.inlines)
        elif isinstance(it, Break):
            out += " "
    return out


def plain(inlines) -> str:
    """The visible text with its whitespace squashed."""
    return re.sub(r"\s+", " ", itext(inlines)).strip()


def remove(inlines, ids) -> None:
    """Drop every inline whose id() is in `ids`, inside Bold, Link and Hidden too, in one pass."""
    keep = []
    for it in inlines:
        if id(it) in ids:
            continue
        if isinstance(it, (Bold, Link, Hidden)):
            remove(it.inlines, ids)
        keep.append(it)
    inlines[:] = keep


def strip_marker(inlines, marker) -> bool:
    """Drop a leading `marker` from the first text; True when the first text was found."""
    for it in inlines:
        if isinstance(it, Text):
            t = it.text.lstrip()
            if t.startswith(marker):
                it.text = t[len(marker):].lstrip()
            return True
        if isinstance(it, Bold) and strip_marker(it.inlines, marker):
            return True
        if isinstance(it, (Link, Hidden, Math, NoteRef, Break)):
            return True
    return False


def flat(blocks) -> str:
    """Blocks as one line of text (headers and footers)."""
    parts = []
    for b in blocks:
        if isinstance(b, (Para, Heading, ListItem)):
            parts.append(itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(flat(c.blocks) for row in b.rows for c in row)
    return " ".join(re.sub(r"\s+", " ", p).strip() for p in parts if p.strip())


def cut_command(s: str) -> str:
    """A hyperlink field command up to its first unescaped `;`, unescaped."""
    m = re.search(r"(?<!\\);", s)
    return re.sub(r"\\([;:])", r"\1", s[:m.start()] if m else s).strip()


class Frame:
    """One open inline scope inside a paragraph: root, link, pass, drop or deleted."""

    def __init__(self, kind, fid=None, url=""):
        self.kind, self.id, self.url, self.inl = kind, fid, url, []


class State:
    """A paragraph being read: its open scopes, the run's boldness, the blocks that follow it (extras) and its
    inline equations (eqs)."""

    def __init__(self, nohead):
        self.nohead = nohead
        self.frames = [Frame("root")]
        self.bold = False
        self.extras = []
        self.eqs = []


class Scopes:
    """The inline scope operations both readers mix in."""

    def add(self, st, inline):
        st.frames[-1].inl.append(inline)

    def push(self, st, frame):
        if len(st.frames) < limits.MAX_DEPTH:
            st.frames.append(frame)

    def close_to(self, st, n):
        """Close scopes until `n` are open: a link becomes a Link, a deletion Hidden, a pass gives its inlines to its
        parent and a drop loses them."""
        while len(st.frames) > n:
            f = st.frames.pop()
            parent = st.frames[-1].inl
            if f.kind == "link" and f.inl:
                parent.append(Link(f.url, f.inl))
            elif f.kind == "deleted" and f.inl:
                parent.append(Hidden(f.inl, "deleted"))
            elif f.kind == "pass":
                parent.extend(f.inl)

    def close_field(self, st, ref=None):
        """A field end closes the innermost open field (the one whose id is `ref`, when given)."""
        for i in range(len(st.frames) - 1, 0, -1):
            f = st.frames[i]
            if f.kind in ("link", "pass", "drop") and (ref is None or f.id == ref):
                self.close_to(st, i)
                return


# ---- paragraph rules
def style_name_level(name: str) -> int:
    """The heading level a style name gives: 개요/outline/heading N, 제목/title 1, 부제목/subtitle 2; else 0."""
    if m := STYLE_HEAD.match(name):
        return min(max(int(m.group(1)), 1), 6)
    low = name.lower()
    if low in ("제목", "title"):
        return 1
    if low in ("부제목", "subtitle"):
        return 2
    return 0


def heading_level(level: int, text: str) -> int:
    """A heading candidate over 120 characters, or over 40 ending like a sentence, stays a paragraph (0)."""
    if level and (len(text) > 120 or (len(text) > 40 and text[-1] in ".?!")):
        return 0
    return level


def lone_equations(inlines, eqs) -> list:
    """A paragraph holding nothing visible but its inline equations shows them as display equations: they leave
    the inlines (one pass) and come back as Equation blocks. Otherwise nothing changes and the list is empty."""
    if not eqs or itext(inlines).strip():
        return []
    remove(inlines, {id(o) for o in eqs})
    return [Equation(o.tex, True) for o in eqs]


def comment_note(blocks) -> list:
    """A hidden comment's paragraphs as a note that says it is one."""
    if blocks and isinstance(blocks[0], Para):
        blocks[0].inlines.insert(0, Text("Comment: "))
    else:
        blocks.insert(0, Para([Text("Comment:")]))
    return blocks


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
