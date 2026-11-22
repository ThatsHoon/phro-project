"""WordprocessingML styles (basedOn chains) and numbering (list markers), split out of ooxml_word."""
import re

from .. import limits
from ..containers.zipx import attr, first, kids, local
from .ooxml_base import int_of as _int, is_on as _on, val as _val

MAX_NAME, MAX_MARKER = 64, 64
STYLE_LINKS = 8
_HEAD = re.compile(r"^heading\s*(\d+)$", re.I)
_PLACEHOLDER = re.compile(r"%([1-9])")
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


def _letters(n, alphabet):
    """The letter n falls on, repeated once more each time the set is passed (a..z, aa, bb: ECMA-376 17.18.59);
    past MAX_MARKER letters, the number."""
    turn, at = divmod(n - 1, len(alphabet))
    return alphabet[at] * (turn + 1) if turn < MAX_MARKER else str(n)


def _counting(n, digits, units, one=False, zero="", teen=False):
    """n below 10,000 in a counting system: the digit before each unit. Without `one` a one is left out before a
    unit (十, 百十); with it the one is said (壹拾), except, with `teen`, before ten in a number below twenty
    (Chinese 十一, 一百一十). `zero` stands once for the empty places between two digits (一百零一)."""
    if not 0 < n < 10_000:
        return str(n)
    out, gap = "", False
    for i, unit in ((3, units[2]), (2, units[1]), (1, units[0]), (0, "")):
        d = n // 10 ** i % 10
        if not d:
            gap = bool(out)
            continue
        if gap and zero:
            out += zero
        gap = False
        said = one and not (teen and i == 1 and n < 20)
        out += (digits[d] if d > 1 or said or not unit else "") + unit
    return out


def _hebrew(n):
    if not 0 < n < 1000:
        return str(n)
    out = "ת" * (n // 400) if n >= 400 else ""
    n %= 400
    out += " קרש"[n // 100].strip()
    n %= 100
    if n in (15, 16):
        return out + "ט" + "וז"[n - 15]
    return out + " יכלמנסעפצ"[n // 10].strip() + " אבגדהוזחט"[n % 10].strip()


def _native_korean(n):
    if not 0 < n < 100:
        return _counting(n, _HANGUL_DIGITS, "십백천")
    tens, ones = divmod(n, 10)
    return ("" if not tens else _KO_TENS[tens - 1]) + ("" if not ones else _KO_ONES[ones - 1])


def _english(n, ordinal=False):
    if not 0 < n < 1000:
        return str(n)
    words = []
    if n >= 100:
        words += [_EN_ONES[n // 100], "Hundred"]
        n %= 100
    if n >= 20:
        words.append(_EN_TENS[n // 10] + ("-" + _EN_ONES[n % 10] if n % 10 else ""))
    elif n:
        words.append(_EN_ONES[n])
    text = " ".join(words)
    if ordinal:
        head, _, last = text.rpartition(" ")
        tail, dash, word = last.rpartition("-")
        word = _EN_ORDINAL.get(word) or (word[:-1] + "ieth" if word.endswith("y") else word + "th")
        text = (head + " " if head else "") + tail + dash + word
    return text.capitalize()


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_HANGUL_DIGITS = "영일이삼사오육칠팔구"
_KO_ONES = ("하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉")
_KO_TENS = ("열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
_EN_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
            "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_EN_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
_EN_ORDINAL = {"One": "First", "Two": "Second", "Three": "Third", "Five": "Fifth", "Eight": "Eighth", "Nine": "Ninth",
               "Twelve": "Twelfth"}
_CJK = "〇一二三四五六七八九"
_COUNTING = {
    "japaneseCounting": (_CJK, "十百千"), "koreanCounting": (_HANGUL_DIGITS, "십백천"),
    **dict.fromkeys(("chineseCounting", "chineseCountingThousand", "taiwaneseCounting", "taiwaneseCountingThousand"),
                    (_CJK, "十百千", True, "零", True)),
    "japaneseLegal": ("〇壱弐参四伍六七八九", "拾百阡", True),
    "chineseLegalSimplified": ("零壹贰叁肆伍陆柒捌玖", "拾佰仟", True, "零"),
    "ideographLegalTraditional": ("零壹貳參肆伍陸柒捌玖", "拾佰仟", True, "零"),
}
_STEMS, _BRANCHES = "甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉戌亥"
_DIGITS = {
    "koreanDigital": _HANGUL_DIGITS, "koreanDigital2": "零一二三四五六七八九",
    "decimalFullWidth": "０１２３４５６７８９", "decimalFullWidth2": "０１２３４５６７８９",
    "decimalHalfWidth": "0123456789", "ideographDigital": _CJK, "japaneseDigitalTenThousand": _CJK,
    "taiwaneseDigital": _CJK, "hindiNumbers": "०१२३४५६७८९", "thaiNumbers": "๐๑๒๓๔๕๖๗๘๙",
}
_ALPHABETS = {
    "lowerLetter": "abcdefghijklmnopqrstuvwxyz", "upperLetter": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ganada": "가나다라마바사아자차카타파하", "chosung": "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ",
    "aiueo": "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝ",
    "aiueoFullWidth": "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン",
    "iroha": "ｲﾛﾊﾆﾎﾍﾄﾁﾘﾇﾙｦﾜｶﾖﾀﾚｿﾂﾈﾅﾗﾑｳヰﾉｵｸﾔﾏｹﾌｺｴﾃｱｻｷﾕﾒﾐｼヱﾋﾓｾｽﾝ",
    "irohaFullWidth": "イロハニホヘトチリヌルヲワカヨタレソツネナラムウヰノオクヤマケフコエテアサキユメミシヱヒモセスン",
    "ideographTraditional": "甲乙丙丁戊己庚辛壬癸", "ideographZodiac": "子丑寅卯辰巳午未申酉戌亥",
    "russianLower": "абвгдежзиклмнопрстуфхцчшщэюя", "russianUpper": "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ",
    "hebrew2": "אבגדהוזחטיכלמנסעפצקרשת", "arabicAlpha": "أبتثجحخدذرزسشصضطظعغفقكلمنهوي",
    "arabicAbjad": "أبجدهوزحطيكلمنسعفصقرشتثخذضظغ",
    "thaiLetters": "กขคงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ",
    "hindiVowels": "अआइईउऊऋऌएऐओऔ", "hindiConsonants": "कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह",
}


def _fmt(n, kind):
    """n in the numFmt `kind` (ECMA-376 17.18.59); a format not written here, and n below 1, as decimal."""
    if n < 1:
        return str(n)
    if kind in _ALPHABETS:
        return _letters(n, _ALPHABETS[kind])
    if kind in _COUNTING:
        return _counting(n, *_COUNTING[kind])
    if kind in _DIGITS:
        return "".join(_DIGITS[kind][int(c)] for c in str(n))
    if kind == "lowerRoman":
        return _roman(n).lower()
    if kind == "upperRoman":
        return _roman(n)
    if kind in ("decimalEnclosedCircle", "decimalEnclosedCircleChinese"):
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    if kind == "ideographEnclosedCircle":
        return chr(0x3280 + n - 1) if n <= 10 else str(n)
    if kind == "decimalEnclosedFullstop":
        return chr(0x2488 + n - 1) if n <= 20 else f"{n}."
    if kind == "decimalEnclosedParen":
        return f"({n})"
    if kind == "decimalZero":
        return f"{n:02d}"
    if kind == "hebrew1":
        return _hebrew(n)
    if kind == "ideographZodiacTraditional":
        return _STEMS[(n - 1) % 10] + _BRANCHES[(n - 1) % 12]
    if kind == "koreanLegal":
        return _native_korean(n)
    if kind == "ordinal":
        return _ordinal(n)
    if kind == "cardinalText":
        return _english(n)
    if kind == "ordinalText":
        return _english(n, True)
    if kind == "hex":
        return f"{n:X}"
    if kind == "chicago":
        return "*†‡§"[(n - 1) % 4] * ((n - 1) // 4 + 1)
    if kind == "numberInDash":
        return f"- {n} -"
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
    """word/numbering.xml: abstract levels, numbers with their level and start overrides, and the running counters.

    An abstractNum with numStyleLink takes the levels of the abstractNum its numbering style's num names (`link`), and
    shares that list's counters. A list's counters belong to its abstractNum, so a second num of the same list goes on
    counting; a num with a startOverride starts its levels again at its first paragraph."""

    def __init__(self):
        self.abstracts, self.nums, self.counters, self.links, self.alias, self.used = {}, {}, {}, {}, {}, set()

    @staticmethod
    def level(lv):
        st, nf, tx = (first(lv, k) for k in ("start", "numFmt", "lvlText"))
        return _int(_val(st), 1), _val(nf) or "decimal", (_val(tx) or "")[:MAX_MARKER]

    def load(self, root):
        for a in kids(root, "abstractNum"):
            aid = attr(a, "abstractNumId")
            if aid is None:
                continue
            self.abstracts[aid] = {_int(attr(lv, "ilvl")): self.level(lv) for lv in kids(a, "lvl")}
            link = first(a, "numStyleLink")
            if link is not None and _val(link):
                self.links[aid] = _val(link)
        for n in kids(root, "num"):
            nid = attr(n, "numId")
            ab = kids(n, "abstractNumId")
            over, levels = {}, {}
            for o in kids(n, "lvlOverride"):
                k = _int(attr(o, "ilvl"))
                if first(o, "startOverride") is not None:
                    over[k] = _int(_val(first(o, "startOverride")), 1)
                if first(o, "lvl") is not None:
                    levels[k] = self.level(first(o, "lvl"))
            if nid is not None and ab:
                self.nums[nid] = (_val(ab[0]), over, levels)

    def link(self, styles):
        """Each numStyleLink list takes the levels of the list its numbering style's numPr names, following at most
        STYLE_LINKS links, and counts with it (`alias`)."""
        for aid in self.links:
            cur = aid
            for _ in range(STYLE_LINKS):
                if cur not in self.links:
                    break
                style = styles.styles.get(self.links[cur])
                num = self.nums.get(style["num"][0]) if style else None
                cur = num[0] if num is not None and num[0] != cur else None
                if cur is None:
                    break
            if cur is not None and cur != aid and cur not in self.links and cur in self.abstracts:
                self.abstracts[aid], self.alias[aid] = self.abstracts[cur], cur

    def marker(self, nid, lvl):
        """List marker for the next paragraph at (numId, level); None when the list is not defined."""
        num = self.nums.get(nid)
        if num is None:
            return None
        aid, over, own = num
        levels = self.abstracts.get(aid, {})
        if own:
            levels = {**levels, **own}
        info = levels.get(lvl)
        if info is None:
            return None

        def start(k):
            return over.get(k, levels[k][0] if k in levels else 1)

        cnt = self.counters.setdefault(self.alias.get(aid, aid), {})
        if nid not in self.used:
            self.used.add(nid)
            if over:
                low = min(over)
                for k in [k for k in cnt if k >= low]:
                    del cnt[k]
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
