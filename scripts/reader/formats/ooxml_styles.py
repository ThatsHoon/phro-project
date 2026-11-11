"""WordprocessingML styles (basedOn chains) and numbering (list markers), split out of ooxml_word."""
import re

from .. import limits
from ..containers.zipx import attr, first, kids, local
from .ooxml_base import int_of as _int, is_on as _on, val as _val

MAX_NAME, MAX_MARKER = 64, 64
_HEAD = re.compile(r"^heading\s*(\d+)$", re.I)
_PLACEHOLDER = re.compile(r"%([1-9])")
_SYLLABLE = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def numpr(el):
    """(numId, ilvl) strings of a w:numPr; None for an absent child."""
    ids = kids(el, "numId")
    lv = kids(el, "ilvl")
    return (_val(ids[0]) if ids else None, _val(lv[0]) if lv else None)


# ---- numbering formats
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


def _fmt(n, kind):
    if n < 1:
        return str(n)
    if kind == "lowerLetter":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "upperLetter":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "lowerRoman":
        return _roman(n).lower()
    if kind == "upperRoman":
        return _roman(n)
    if kind == "ganada":
        return _bijective(n, _SYLLABLE)
    if kind == "chosung":
        return _bijective(n, _JAMO)
    if kind == "decimalEnclosedCircle":
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    return str(n)


class Styles:
    """Paragraph and character styles from word/styles.xml, with basedOn chains."""

    def __init__(self):
        self.styles, self.chains = {}, {}

    def load(self, root):
        for s in kids(root, "style"):
            sid = attr(s, "styleId")
            if not sid:
                continue
            d = {"name": "", "based": None, "outline": None, "num": (None, None), "bold": None, "vanish": None}
            for c in s:
                n = local(c.tag)
                if n == "name":
                    d["name"] = (_val(c) or "")[:MAX_NAME]
                elif n == "basedOn":
                    d["based"] = _val(c)
                elif n == "pPr":
                    for e in c:
                        en = local(e.tag)
                        if en == "outlineLvl":
                            d["outline"] = _int(_val(e), None)
                        elif en == "numPr":
                            d["num"] = numpr(e)
                elif n == "rPr":
                    for e in c:
                        en = local(e.tag)
                        if en in ("b", "vanish"):
                            d["bold" if en == "b" else "vanish"] = _on(e)
            self.styles[sid] = d

    def chain(self, sid):
        """The style and its basedOn ancestors, nearest first; cycles and runaway chains stop."""
        if sid not in self.chains:
            out, seen, cur = [], set(), sid
            while cur and cur not in seen and cur in self.styles and len(out) < limits.MAX_DEPTH:
                seen.add(cur)
                out.append(self.styles[cur])
                cur = self.styles[cur]["based"]
            self.chains[sid] = out
        return self.chains[sid]

    @staticmethod
    def prop(chain, key):
        return next((s[key] for s in chain if s[key] is not None), None)

    def heading_level(self, own, chain) -> int:
        if own is not None:
            return min(own + 1, 6) if 0 <= own <= 8 else 0
        for s in chain:
            if m := _HEAD.match(s["name"].strip()):
                return min(max(int(m.group(1)), 1), 6)
            if s["name"].strip().lower() == "title":
                return 1
        lvl = self.prop(chain, "outline")
        return min(lvl + 1, 6) if lvl is not None and 0 <= lvl <= 8 else 0


class Numbering:
    """word/numbering.xml: abstract levels, numbers with start overrides, and the running counters."""

    def __init__(self):
        self.abstracts, self.nums, self.counters = {}, {}, {}

    def load(self, root):
        for a in kids(root, "abstractNum"):
            aid = attr(a, "abstractNumId")
            levels = {}
            for lv in kids(a, "lvl"):
                st, nf, tx = (first(lv, k) for k in ("start", "numFmt", "lvlText"))
                levels[_int(attr(lv, "ilvl"))] = (_int(_val(st), 1), _val(nf) or "decimal", (_val(tx) or "")[:MAX_MARKER])
            if aid is not None:
                self.abstracts[aid] = levels
        for n in kids(root, "num"):
            nid = attr(n, "numId")
            ab = kids(n, "abstractNumId")
            over = {_int(attr(o, "ilvl")): _int(_val(first(o, "startOverride")), 1)
                    for o in kids(n, "lvlOverride") if first(o, "startOverride") is not None}
            if nid is not None and ab:
                self.nums[nid] = (_val(ab[0]), over)

    def marker(self, nid, lvl):
        """List marker for the next paragraph at (numId, level); None when the list is not defined."""
        num = self.nums.get(nid)
        if num is None:
            return None
        levels, over = self.abstracts.get(num[0], {}), num[1]
        info = levels.get(lvl)
        if info is None:
            return None

        def start(k):
            return over.get(k, levels[k][0] if k in levels else 1)

        cnt = self.counters.setdefault(nid, {})
        cnt[lvl] = cnt.get(lvl, start(lvl) - 1) + 1
        for k in [k for k in cnt if k > lvl]:
            del cnt[k]
        if info[1] == "bullet":
            return "-"
        if info[1] == "none":
            return ""

        def sub(m):
            k = int(m.group(1)) - 1
            return _fmt(cnt.get(k, start(k)), levels[k][1] if k in levels else "decimal")

        return _PLACEHOLDER.sub(sub, info[2])[:MAX_MARKER].strip()
