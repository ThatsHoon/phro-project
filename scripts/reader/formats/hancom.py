"""Helpers shared by the Hancom parsers (hwpx and hwp): equation scripts, numbering markers, the cell grid, the
inline model and paragraph rules both readers build paragraphs with, and the drawing objects (`Obj`) a section's
diagrams are written from (`diagrams`)."""
import re
from bisect import bisect_left

from .. import diagram, limits
from ..model import Bold, Break, Cell, Equation, Heading, Hidden, Link, ListItem, Math, NoteRef, Para, Table, Text

AUTO_ALT = re.compile(r"^(그림|사각형|선|타원|글상자|표|수식)입니다\.?$")
_AUTO_FIELD = re.compile(r"^[^:.?!]{1,20}:")
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


def outline_marker(levels: dict, counters: dict, lvl: int):
    """An outline paragraph's number at `lvl` from its numbering's levels (next_marker's shapes); None, the counters
    left as they are, when that level has no format or its format gives no number."""
    head = levels.get(lvl + 1)
    if not head or not head[0]:
        return None
    mk = next_marker(levels, counters, lvl)
    return mk if mk != "-" else None


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


def alt_text(comment: str) -> str:
    """A shape comment as alt text. The description Hancom writes on its own, a first line such as "그림입니다." and
    the "<label>: <value>" lines right after it (the original file's name and size, the date a photo was taken), is
    left out; what follows it is kept, its whitespace squashed."""
    lines = [ln.strip() for ln in re.split(r"\r\n|[\r\n]", comment.replace("\0", ""))]
    k = 0
    if AUTO_ALT.match(lines[0]):
        k = 1
        while k < len(lines) and _AUTO_FIELD.match(lines[k]):
            k += 1
    return re.sub(r"\s+", " ", " ".join(lines[k:])).strip()


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

    def open_field(self, st, fid=None, url="", unfilled=False):
        """A field begin: a hyperlink with a target scopes a Link, an unfilled click-here field's guide text is
        dropped, and any other field's text is kept."""
        self.push(st, Frame("drop" if unfilled else "link" if url else "pass", fid, url))

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


def lead(inlines, number) -> None:
    """Write `number` before the paragraph's text, dropping a copy of it typed there."""
    strip_marker(inlines, number)
    inlines.insert(0, Text(number + " "))


def paragraph(inlines, text, level, number=None, item=None):
    """The block a paragraph reads as, by the rules both readers share; None when nothing is left.

    A paragraph with text at a heading `level` is a Heading. Otherwise `item` (level, marker, a typed copy of the
    marker to drop or None), given for a numbered or bulleted paragraph, makes it a ListItem, and anything else is a
    Para. `number`, an outline paragraph's number, leads its heading or its paragraph; a numbered paragraph's marker
    leads its heading, so every number the counters take is written."""
    if level and text:
        mark = number or (item[1] if item and item[1] != "-" else None)
        if mark:
            lead(inlines, mark)
        return Heading(level, inlines)
    if item:
        lvl, marker, typed = item
        if typed:
            strip_marker(inlines, typed)
        return ListItem(lvl, marker, inlines) if inlines else None
    if not inlines:
        return None
    if number and text:
        lead(inlines, number)
    return Para(inlines)


def _shown(inlines, ids, out) -> None:
    """Add to `out` the id() of every inline in `ids` that is visible: inside Bold and Link, not inside Hidden."""
    for it in inlines:
        if id(it) in ids:
            out.add(id(it))
        elif isinstance(it, (Bold, Link)):
            _shown(it.inlines, ids, out)


def lone_equations(inlines, eqs) -> list:
    """A paragraph holding nothing visible but its inline equations shows them as display equations: the visible
    ones leave the inlines (one pass) and come back as Equation blocks. An equation inside Hidden (a tracked
    deletion) is not visible and stays where it is. Otherwise nothing changes and the list is empty."""
    if not eqs or itext(inlines).strip():
        return []
    ids = set()
    _shown(inlines, {id(o) for o in eqs}, ids)
    if not ids:
        return []
    remove(inlines, ids)
    return [Equation(o.tex, True) for o in eqs if id(o) in ids]


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


# ---- drawing objects as diagrams
class Obj:
    """A drawing object a paragraph holds, standing among the blocks where it was read until `diagrams` writes its
    section. `blocks` is what it reads as on its own and `key` its id (None when it has none). A group holds `kids`
    in its own frame (a run of other blocks it holds is a keyless group kid, so the kids keep reading order); `box`
    (x0, y0, x1, y1) is in the parent's frame, or None. A connector or line has `edge` (begin id, end id, start point,
    end point, (start, end) arrowheads, plain line), its points in the same frame."""
    __slots__ = ("key", "blocks", "kids", "group", "kind", "box", "edge")

    def __init__(self, key, blocks, kids=(), group=False, kind="", box=None, edge=None):
        self.key, self.blocks, self.kids, self.group = key, blocks, list(kids), group
        self.kind, self.box, self.edge = kind, box, edge


def _swap(blocks, fn) -> list:
    """The blocks with each Obj replaced by fn(obj), through table cells (nested no deeper than the parser reads)."""
    out = []
    for b in blocks:
        if isinstance(b, Obj):
            out.extend(fn(b))
            continue
        if isinstance(b, Table):
            for row in b.rows:
                for cell in row:
                    cell.blocks = _swap(cell.blocks, fn)
        out.append(b)
    return out


def _shift(p, s):
    return None if p is None or s is None else (p[0] + s[0], p[1] + s[1])


def _below(top) -> list:
    """The object and every object under it, in reading order, each with the shift from its frame to the top's own
    (the top's kids are in its frame): (dx, dy), or None past a group with no box."""
    out, stack = [(top, None)], [(k, (0, 0)) for k in reversed(top.kids)]
    while stack:
        o, s = stack.pop()
        out.append((o, s))
        inner = _shift(s, o.box[:2]) if o.box else None
        stack.extend((k, inner) for k in reversed(o.kids))
    return out


def _scrub(blocks) -> list:
    """The blocks with every Obj in them, through table cells, read as its own blocks: what it read as before."""
    return _swap(blocks, lambda o: _scrub(_own(o)))


def _own(o) -> list:
    """What the object and every object under it read as, in reading order: what they read as with no diagram."""
    return [b for x, _ in _below(o) for b in x.blocks]


def _register(dia, o, key, shift) -> None:
    if o.edge:
        begin, end, p0, p1, arrows, line = o.edge
        dia.connector(key, begin, end, _shift(p0, shift), _shift(p1, shift), arrows, line=line, blocks=o.blocks)
    else:
        lo, hi = (_shift(o.box[:2], shift), _shift(o.box[2:], shift)) if o.box else (None, None)
        dia.shape(key, o.blocks, o.kind, lo + hi if lo and hi else None)


def _write(ctx, budget, regs, slots) -> list:
    """One scope: the objects regs [(obj, key, shift)] registered and the slots [(key, blocks)] placed. Returns what
    goes in each slot's place, plus one entry for the end of the section: the diagram before the slot it anchors at,
    each slot's blocks when kept. A scope with no connector or line keeps every slot as it was."""
    if not any(o.edge for o, _, _ in regs):
        return [list(blocks) for _, blocks in slots] + [[]]
    dia = diagram.Diagram(ctx, budget)
    for o, key, shift in regs:
        with ctx.guard("shape"):
            _register(dia, o, key, shift)
    marks = [object() for _ in slots]
    flow = marks
    with ctx.guard("shape"):
        flow = dia.place([(key, [m]) for (key, _), m in zip(slots, marks)])
    index = {id(m): i for i, m in enumerate(marks)}
    kept, made, at, last = set(), [], len(slots), -1
    for b in flow:  # the kept marks in order, the diagram's blocks just before the anchor's slot
        i = index.get(id(b))
        if i is not None:
            kept.add(i)
            last = i
        else:
            if not made:
                at = last + 1
            made.append(b)
    out = [list(blocks) if i in kept else [] for i, (_, blocks) in enumerate(slots)] + [[]]
    out[at][:0] = made
    return out


def diagrams(ctx, budget, blocks) -> list:
    """The section's blocks with each Obj in them (through table cells) given way to what it reads as.

    The section is one diagram scope. A top-level group is a scope of its own, in its own frame, when no glue crosses
    its edge and no loose end leaves its box; otherwise its objects join the section's scope, placed by the group's
    box, or with no geometry when it has none. Keys are the objects' ids, unique in the section (a missing or repeated
    one is unkeyed); groups are not shapes, and glue to a connector does not join scopes. An object in a table that
    another object holds reads as before. Each object is registered under guard("shape"), and place() runs under it
    too, falling back to the slots' blocks; should anything else fail, every object reads as its blocks. No Obj is
    left in what comes back."""
    tops = []
    blocks = _swap(blocks, lambda o: tops.append(o) or [o])
    if not tops:
        return blocks
    done, tail = {}, []
    with ctx.guard("shape"):
        for t in tops:
            for o, _ in _below(t):
                o.blocks = _scrub(o.blocks)
        done, tail = _section(ctx, budget, tops)
    return _scrub(_swap(blocks, lambda o: done[id(o)] if id(o) in done else _own(o)) + tail)


def _leaves(t, tree, owner) -> bool:
    """Whether a loose end of a connector or arrowed line under group t lies outside t's box, compared in t's parent's
    frame. A group with no box has no frame to compare in, so its loose ends are taken to stay inside it."""
    if not t.box:
        return False
    (x0, x1), (y0, y1) = sorted((t.box[0], t.box[2])), sorted((t.box[1], t.box[3]))
    for o, s in tree:
        if not o.edge:
            continue
        begin, end, p0, p1, arrows, line = o.edge
        if line and True not in arrows:  # a divider is no connector
            continue
        for ref, p in ((begin, p0), (end, p1)):
            q = _shift(_shift(p, s), t.box[:2])
            if owner.get(ref) is None and q is not None and not (x0 <= q[0] <= x1 and y0 <= q[1] <= y1):
                return True
    return False


def _section(ctx, budget, tops) -> tuple:
    """({id(top): its blocks}, the blocks for the section's end): the closed groups' scopes, then the section's."""
    trees = [_below(t) for t in tops]
    keyed, used, owner = {}, set(), {}  # owner: the top holding each shape (not connector) id glue can name
    for t, tree in zip(tops, trees):
        for o, _ in tree:
            if o.group:
                continue
            if o.key is not None and o.key not in used:
                keyed[id(o)] = o.key
                used.add(o.key)
                if not o.edge:
                    owner[o.key] = t
            else:
                keyed[id(o)] = ("unkeyed", len(keyed))
    opened = {id(t) for t, tree in zip(tops, trees) if t.group and _leaves(t, tree, owner)}
    for t, tree in zip(tops, trees):
        for o, _ in tree:
            for ref in o.edge[:2] if o.edge else ():
                u = owner.get(ref)
                if u is not None and u is not t:
                    opened.update((id(t), id(u)))
    regs, slots, spans = [], [], []
    for t, tree in zip(tops, trees):
        start = len(slots)
        if t.group and id(t) not in opened:
            parts = _write(ctx, budget, [(o, keyed[id(o)], s) for o, s in tree if not o.group],
                           [(keyed.get(id(o)), o.blocks) for o, _ in tree])
            slots.append((None, [b for p in parts for b in p]))
        else:
            at = t.box[:2] if t.box else None  # a group's frame placed in the section's, when it has a box
            regs += [(o, keyed[id(o)], (0, 0) if o is t else _shift(s, at)) for o, s in tree if not o.group]
            slots += [(keyed.get(id(o)), o.blocks) for o, _ in tree]
        spans.append((start, len(slots)))
    parts = _write(ctx, budget, regs, slots)
    return {id(t): [b for p in parts[a:b] for b in p] for t, (a, b) in zip(tops, spans)}, parts[-1]
