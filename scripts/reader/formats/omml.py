"""Office Math (OMML) to TeX source without delimiters, ported from anydoc (`shared/math/omml.rs` and `tex.rs`).

Elements are dispatched by local name, math-namespace elements only; a host element is walked through, except
revision and property markup. Past limits.MAX_DEPTH levels an element is written as its plain text, so nothing is
lost. A property value is its m:val, or the element's own text (the rtf-shaped tree).
"""
from .. import limits

MATH_NS = frozenset({"http://schemas.openxmlformats.org/officeDocument/2006/math",
                     "http://purl.oclc.org/ooxml/officeDocument/math"})
_VALS = tuple(f"{{{ns}}}val" for ns in MATH_NS)
_HOST_SKIP = frozenset({"del", "moveFrom", "rPr", "pPr"})
_MATH, _FUNC, _ARRAY = "math", "func", "array"  # walk modes: plain math, inside m:fName, inside an eqArr row
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_SPECIAL = frozenset("{}$%&#_")


def _split(tag):
    """(namespace, local name) of an element tag; (None, "") for comments and processing instructions."""
    if not isinstance(tag, str):
        return None, ""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def is_math(el, name: str) -> bool:
    ns, n = _split(el.tag)
    return n == name and ns in MATH_NS


def _alpha(c):
    return c in _LETTERS


def _space(c):
    return c.isspace() and c not in "\x1c\x1d\x1e\x1f"


def _control(c):
    o = ord(c)
    return o < 0x20 or 0x7F <= o <= 0x9F


def _ends_with_word(s):
    """Whether s ends in a letter-named control word, which would swallow a letter written right after it."""
    n = len(s)
    i = n
    while i and _alpha(s[i - 1]):
        i -= 1
    return i < n and s[:i].endswith("\\")


# ---- the TeX buffer
class Tex:
    """TeX source as parts joined once; a letter after a control word gets a separating space."""

    __slots__ = ("parts", "after")

    def __init__(self):
        self.parts, self.after = [], False

    def empty(self):
        return not self.parts

    def text(self):
        return "".join(self.parts)

    def push_str(self, s):
        if not s:
            return
        if self.after and _alpha(s[0]):
            self.parts.append(" ")
        self.parts.append(s)
        self.after = _ends_with_word(s)

    def push_char(self, c):
        if self.after and _alpha(c):
            self.parts.append(" ")
        self.parts.append(c)
        self.after = False

    def push_macro(self, name):
        self.parts.append(name)
        self.after = _ends_with_word(name)

    def push_tex(self, other):
        if not other.parts:
            return
        if self.after and _alpha(other.parts[0][0]):
            self.parts.append(" ")
        self.parts.extend(other.parts)
        self.after = other.after

    def push_group(self, inner):
        self.push_char("{")
        self.push_tex(inner)
        self.push_char("}")

    def push_base(self, inner):
        """A script base: as it stands when it is one atom (one character or control word), braced otherwise."""
        s = inner.text()
        name = s[1:] if s.startswith("\\") else ""
        if len(s) == 1 or (name and all(map(_alpha, name))):
            self.push_tex(inner)
        else:
            self.push_group(inner)

    def push_command(self, name, inner):
        self.push_macro(name)
        self.push_group(inner)

    def push_math_text(self, text):
        """Symbols become control words, styled alphanumerics fold to their base inside the font command, and TeX
        specials are escaped."""
        pending, run = None, []
        for c in text:
            folded = fold_alnum(c)
            if folded is not None and folded[1] != "plain":
                if pending != folded[1]:
                    self._flush(pending, run)
                    pending, run = folded[1], []
                run.append(folded[0])
                continue
            self._flush(pending, run)
            pending, run = None, []
            self.push_math_char(folded[0] if folded is not None else c)
        self._flush(pending, run)

    def _flush(self, variant, run):
        if variant is None:
            return
        inner = Tex()
        inner.push_math_text("".join(run))
        self.push_command(_VARIANT[variant], inner)

    def push_math_char(self, c):
        mapped = symbol(c)
        if mapped is not None:
            if mapped.startswith("\\"):
                self.push_macro(mapped)
            else:
                self.push_str(mapped)
        elif c in _SPECIAL:
            self.push_char("\\")
            self.push_char(c)
        elif c == "\\":
            self.push_macro("\\backslash")
        elif c == "^":
            self.push_str("\\char`^")
        elif c == "~":
            self.push_macro("\\sim")
        elif c == "\xa0":
            self.push_str("\\ ")
        elif _space(c):
            self.push_char(" ")
        elif not _control(c):
            self.push_char(c)

    def push_text_mode(self, text):
        """\\text{...}: literal text inside a formula."""
        inner = Tex()
        for c in text:
            if c in _SPECIAL:
                inner.push_char("\\")
                inner.push_char(c)
            elif c == "\\":
                inner.push_str("\\textbackslash{}")
            elif c == "^":
                inner.push_str("\\textasciicircum{}")
            elif c == "~":
                inner.push_str("\\textasciitilde{}")
            elif c == "\xa0":
                inner.push_char("~")
            elif _space(c):
                inner.push_char(" ")
            elif not _control(c):
                inner.push_char(c)
        self.push_command("\\text", inner)

    def finish(self):
        """The source, trimmed, with runs of spaces made one."""
        out, prev = [], True
        for c in self.text().strip():
            space = c == " "
            if not (space and prev):
                out.append(c)
            prev = space
        return "".join(out)


# ---- tables
_VARIANT = {"plain": "\\mathit", "bold": "\\mathbf", "bolditalic": "\\boldsymbol", "script": "\\mathcal",
            "fraktur": "\\mathfrak", "double": "\\mathbb", "sans": "\\mathsf", "mono": "\\mathtt"}
_LATIN = ("bold", "plain", "bolditalic", "script", "script", "fraktur", "double", "fraktur",
          "sans", "sans", "sans", "sans", "mono")
_GREEK = ("bold", "plain", "bolditalic", "sans", "sans")
_DIGITS = ("bold", "double", "sans", "sans", "mono")
_GREEK_UPPER = "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡϴΣΤΥΦΧΨΩ"
_GREEK_LOWER = "αβγδεζηθικλμνξοπρςστυφχψω"
_GREEK_EXTRA = "∂ϵϑϰϕϱϖ"
_LETTERLIKE = {
    "ℎ": ("h", "plain"), "ℬ": ("B", "script"), "ℰ": ("E", "script"), "ℱ": ("F", "script"), "ℋ": ("H", "script"),
    "ℐ": ("I", "script"), "ℒ": ("L", "script"), "ℳ": ("M", "script"), "ℛ": ("R", "script"), "ℯ": ("e", "script"),
    "ℊ": ("g", "script"), "ℴ": ("o", "script"), "ℭ": ("C", "fraktur"), "ℌ": ("H", "fraktur"),
    "ℑ": ("I", "fraktur"), "ℜ": ("R", "fraktur"), "ℨ": ("Z", "fraktur"), "ℂ": ("C", "double"),
    "ℍ": ("H", "double"), "ℕ": ("N", "double"), "ℙ": ("P", "double"), "ℚ": ("Q", "double"),
    "ℝ": ("R", "double"), "ℤ": ("Z", "double"),
}


def fold_alnum(c):
    """(base character, font variant) of a mathematical alphanumeric or letterlike symbol, or None. Italic is the
    default math style, so it folds to "plain"."""
    if c in _LETTERLIKE:
        return _LETTERLIKE[c]
    cp = ord(c)
    if not 0x1D400 <= cp < 0x1D800:
        return None
    off = cp - 0x1D400
    if off < 13 * 52:
        i = off % 52
        return (chr(ord("A") + i) if i < 26 else chr(ord("a") + i - 26)), _LATIN[off // 52]
    g = cp - 0x1D6A8
    if 0 <= g < 5 * 58:
        i = g % 58
        if i < 25:
            base = _GREEK_UPPER[i]
        elif i == 25:
            base = "∇"
        elif i < 51:
            base = _GREEK_LOWER[i - 26]
        else:
            base = _GREEK_EXTRA[i - 51]
        return base, _GREEK[g // 58]
    dg = cp - 0x1D7CE
    if 0 <= dg < 5 * 10:
        return chr(ord("0") + dg % 10), _DIGITS[dg // 10]
    return None


def _table(*pairs):
    out = {}
    for keys, value in pairs:
        for k in keys:
            out[k] = value
    return out


_SYMBOLS = _table(
    # Greek
    ("α", "\\alpha"), ("β", "\\beta"), ("γ", "\\gamma"), ("δ", "\\delta"), ("ε", "\\varepsilon"),
    ("ϵ", "\\epsilon"), ("ζ", "\\zeta"), ("η", "\\eta"), ("θ", "\\theta"), ("ϑ", "\\vartheta"), ("ι", "\\iota"),
    ("κ", "\\kappa"), ("ϰ", "\\varkappa"), ("λ", "\\lambda"), ("μ", "\\mu"), ("ν", "\\nu"), ("ξ", "\\xi"),
    ("ο", "o"), ("π", "\\pi"), ("ϖ", "\\varpi"), ("ρ", "\\rho"), ("ϱ", "\\varrho"), ("σ", "\\sigma"),
    ("ς", "\\varsigma"), ("τ", "\\tau"), ("υ", "\\upsilon"), ("φ", "\\varphi"), ("ϕ", "\\phi"), ("χ", "\\chi"),
    ("ψ", "\\psi"), ("ω", "\\omega"), ("Α", "A"), ("Β", "B"), ("Γ", "\\Gamma"), ("Δ", "\\Delta"), ("Ε", "E"),
    ("Ζ", "Z"), ("Η", "H"), ("Θϴ", "\\Theta"), ("Ι", "I"), ("Κ", "K"), ("Λ", "\\Lambda"), ("Μ", "M"), ("Ν", "N"),
    ("Ξ", "\\Xi"), ("Ο", "O"), ("Π", "\\Pi"), ("Ρ", "P"), ("Σ", "\\Sigma"), ("Τ", "T"), ("Υ", "\\Upsilon"),
    ("Φ", "\\Phi"), ("Χ", "X"), ("Ψ", "\\Psi"), ("Ω", "\\Omega"),
    # big operators
    ("∑", "\\sum"), ("∏", "\\prod"), ("∐", "\\coprod"), ("∫", "\\int"), ("∬", "\\iint"), ("∭", "\\iiint"),
    ("⨌", "\\iiiint"), ("∮", "\\oint"), ("∯", "\\oiint"), ("∰", "\\oiiint"), ("⋃", "\\bigcup"), ("⋂", "\\bigcap"),
    ("⋁", "\\bigvee"), ("⋀", "\\bigwedge"), ("⨁", "\\bigoplus"), ("⨂", "\\bigotimes"), ("⨀", "\\bigodot"),
    ("⨄", "\\biguplus"), ("⨆", "\\bigsqcup"),
    # binary operators
    ("×", "\\times"), ("÷", "\\div"), ("±", "\\pm"), ("∓", "\\mp"), ("⋅·∙", "\\cdot"), ("∗", "\\ast"),
    ("∘", "\\circ"), ("∖", "\\setminus"), ("⊕", "\\oplus"), ("⊖", "\\ominus"), ("⊗", "\\otimes"),
    ("⊘", "\\oslash"), ("⊙", "\\odot"), ("∪", "\\cup"), ("∩", "\\cap"), ("⊎", "\\uplus"), ("⊓", "\\sqcap"),
    ("⊔", "\\sqcup"), ("∧", "\\wedge"), ("∨", "\\vee"), ("†", "\\dagger"), ("‡", "\\ddagger"), ("⋆", "\\star"),
    # relations
    ("≤⩽", "\\le"), ("≥⩾", "\\ge"), ("≠", "\\ne"), ("≈", "\\approx"), ("≡", "\\equiv"), ("≢", "\\not\\equiv"),
    ("≅", "\\cong"), ("≃", "\\simeq"), ("∼", "\\sim"), ("≁", "\\nsim"), ("∝", "\\propto"), ("≪", "\\ll"),
    ("≫", "\\gg"), ("≺", "\\prec"), ("≻", "\\succ"), ("⪯≼", "\\preceq"), ("⪰≽", "\\succeq"), ("⊂", "\\subset"),
    ("⊃", "\\supset"), ("⊆", "\\subseteq"), ("⊇", "\\supseteq"), ("⊄", "\\not\\subset"), ("⊈", "\\nsubseteq"),
    ("⊊", "\\subsetneq"), ("⊋", "\\supsetneq"), ("⊏", "\\sqsubset"), ("⊐", "\\sqsupset"),
    ("⊑", "\\sqsubseteq"), ("⊒", "\\sqsupseteq"), ("∈", "\\in"), ("∉", "\\notin"), ("∋", "\\ni"),
    ("∌", "\\not\\ni"), ("≐", "\\doteq"), ("≜", "\\triangleq"), ("≝", "\\overset{\\mathrm{def}}{=}"),
    ("≔", "\\coloneqq"), ("⊥", "\\perp"), ("∥", "\\parallel"), ("∦", "\\nparallel"), ("⊢", "\\vdash"),
    ("⊣", "\\dashv"), ("⊨", "\\models"), ("⊤", "\\top"), ("≍", "\\asymp"), ("≀", "\\wr"), ("⋈", "\\bowtie"),
    ("∣", "\\mid"), ("∤", "\\nmid"),
    # arrows
    ("→", "\\to"), ("←", "\\leftarrow"), ("↔", "\\leftrightarrow"), ("⇒", "\\Rightarrow"), ("⇐", "\\Leftarrow"),
    ("⇔", "\\Leftrightarrow"), ("↑", "\\uparrow"), ("↓", "\\downarrow"), ("↕", "\\updownarrow"),
    ("⇑", "\\Uparrow"), ("⇓", "\\Downarrow"), ("⇕", "\\Updownarrow"), ("↦", "\\mapsto"),
    ("⟶", "\\longrightarrow"), ("⟵", "\\longleftarrow"), ("⟷", "\\longleftrightarrow"),
    ("⟹", "\\Longrightarrow"), ("⟸", "\\Longleftarrow"), ("⟺", "\\Longleftrightarrow"), ("⟼", "\\longmapsto"),
    ("↗", "\\nearrow"), ("↘", "\\searrow"), ("↙", "\\swarrow"), ("↖", "\\nwarrow"), ("↩", "\\hookleftarrow"),
    ("↪", "\\hookrightarrow"), ("⇀", "\\rightharpoonup"), ("↼", "\\leftharpoonup"),
    ("⇌", "\\rightleftharpoons"), ("↶", "\\curvearrowleft"), ("↷", "\\curvearrowright"),
    # logic, sets and letterlike
    ("∀", "\\forall"), ("∃", "\\exists"), ("∄", "\\nexists"), ("¬", "\\neg"), ("∅⌀", "\\emptyset"),
    ("∞", "\\infty"), ("∂", "\\partial"), ("∇", "\\nabla"), ("∴", "\\therefore"), ("∵", "\\because"),
    ("ℵ", "\\aleph"), ("ℶ", "\\beth"), ("ℏ", "\\hbar"), ("ℓ", "\\ell"), ("℘", "\\wp"), ("℧", "\\mho"),
    ("√", "\\surd"), ("∠", "\\angle"), ("∡", "\\measuredangle"), ("△", "\\triangle"), ("□◻", "\\square"),
    ("◊", "\\lozenge"), ("°", "^{\\circ}"), ("′", "'"), ("″", "''"), ("‴", "'''"), ("⁗", "''''"),
    ("…", "\\ldots"), ("⋯", "\\cdots"), ("⋮", "\\vdots"), ("⋱", "\\ddots"), ("⋰", "\\iddots"),
    # delimiters
    ("⟨〈", "\\langle"), ("⟩〉", "\\rangle"), ("⌈", "\\lceil"), ("⌉", "\\rceil"), ("⌊", "\\lfloor"),
    ("⌋", "\\rfloor"), ("‖", "\\Vert"),
    # dashes, spaces and invisible characters
    ("−‐‑‒–", "-"), ("   ", "\\,"), ("  ", "\\:"), ("  ", "\\;"),
    ("⁡⁢⁣⁤​﻿", ""),
)

_DELIMS = _table(
    ("(⟮", "("), (")⟯", ")"), ("[", "["), ("]", "]"), ("{", "\\{"), ("}", "\\}"), ("|∣", "|"), ("‖∥", "\\Vert"),
    ("⟨〈", "\\langle"), ("⟩〉", "\\rangle"), ("⌈", "\\lceil"), ("⌉", "\\rceil"), ("⌊", "\\lfloor"),
    ("⌋", "\\rfloor"), ("/", "/"), ("\\", "\\backslash"), ("↑", "\\uparrow"), ("↓", "\\downarrow"),
    ("↕", "\\updownarrow"), ("⇑", "\\Uparrow"), ("⇓", "\\Downarrow"), ("⇕", "\\Updownarrow"),
)

_ACCENTS = _table(
    ("̀`", "\\grave"), ("́´", "\\acute"), ("̂^ˆ", "\\hat"), ("̃~˜", "\\tilde"),
    ("̄¯ˉ", "\\bar"), ("̅‾⎴", "\\overline"), ("̆˘", "\\breve"), ("̇˙", "\\dot"),
    ("̈¨", "\\ddot"), ("̊˚", "\\mathring"), ("̌ˇ", "\\check"), ("̲_⎵", "\\underline"),
    ("⃖←", "\\overleftarrow"), ("⃗→", "\\vec"), ("⃡↔", "\\overleftrightarrow"),
    ("⃛", "\\dddot"), ("⃜", "\\ddddot"), ("⃮", "\\underleftarrow"), ("⃯", "\\underrightarrow"),
    ("⏞︷", "\\overbrace"), ("⏟︸", "\\underbrace"),
)

_FUNCTIONS = frozenset({
    "arccos", "arcsin", "arctan", "arg", "cos", "cosh", "cot", "coth", "csc", "deg", "det", "dim", "exp", "gcd",
    "hom", "inf", "ker", "lg", "lim", "liminf", "limsup", "ln", "log", "max", "min", "Pr", "sec", "sin", "sinh",
    "sup", "tan", "tanh"})
_INTEGRALS = frozenset("∫∬∭⨌∮∯∰")


def symbol(c):
    """TeX for a Unicode math character (a control word, a literal, or "" for an invisible one), or None."""
    return _SYMBOLS.get(c)


def delimiter(c):
    """TeX for a character in a \\left or \\right position; one that cannot stretch gives "."."""
    return _DELIMS.get(c, ".")


def accent(c):
    return _ACCENTS.get(c)


def function_name(name):
    """The control word of a function name TeX predefines, \\operatorname{name} for another alphabetic name, or
    None."""
    name = name.strip()
    if not name or not all(map(_alpha, name)):
        return None
    return f"\\{name}" if name in _FUNCTIONS else f"\\operatorname{{{name}}}"


# ---- tree access
def _find(parent, name):
    for c in parent:
        if is_math(c, name):
            return c
    return None


def _find_all(parent, name):
    return [c for c in parent if is_math(c, name)]


def _direct_text(el):
    return (el.text or "") + "".join(c.tail or "" for c in el)


def _value(el):
    for k in _VALS:
        if k in el.attrib:
            return el.attrib[k]
    return _direct_text(el).strip()


def _prop(parent, pr, name):
    """parent/pr/name's m:val, or its own text; None when absent."""
    p = _find(parent, pr)
    el = _find(p, name) if p is not None else None
    return _value(el) if el is not None else None


def _on(v):
    return v not in ("0", "off", "false")


def _flag(parent, pr, name):
    """An on/off property: None when absent; present with no value is on."""
    v = _prop(parent, pr, name)
    return None if v is None else _on(v)


def _first(v, default):
    return v[0] if v else default


# ---- the walk
def tex(el) -> str:
    """One m:oMath (or any math element) as TeX without delimiters."""
    out = Tex()
    _children(el, out, _MATH, 0)
    return out.finish()


def lines(para) -> list:
    """The TeX of each m:oMath line of an m:oMathPara, empty ones dropped; the whole paragraph when it has none."""
    found = [s for s in map(tex, _find_all(para, "oMath")) if s]
    if found:
        return found
    whole = tex(para)
    return [whole] if whole else []


def _children(el, out, mode, depth):
    for c in el:
        _elem(c, out, mode, depth + 1)


def _arg(parent, name, mode, depth):
    """Child argument `name` of parent (at `depth`), converted on its own."""
    out = Tex()
    c = _find(parent, name)
    if c is not None:
        _children(c, out, mode, depth + 1)
    return out


def _elem(e, out, mode, depth):
    ns, n = _split(e.tag)
    if not n:
        return
    if depth > limits.MAX_DEPTH:
        out.push_math_text("".join(e.itertext()))
        return
    if ns not in MATH_NS:
        if n not in _HOST_SKIP:  # revision and property markup of the host document
            _children(e, out, mode, depth)
        return
    if n == "r":
        _run(e, out, mode)
    elif n == "t":
        out.push_math_text("".join(e.itertext()))
    elif n == "f":
        _fraction(e, out, depth)
    elif n in ("sSup", "sSub", "sSubSup"):
        out.push_base(_arg(e, "e", _MATH, depth))
        if n != "sSup":
            out.push_char("_")
            out.push_group(_arg(e, "sub", _MATH, depth))
        if n != "sSub":
            out.push_char("^")
            out.push_group(_arg(e, "sup", _MATH, depth))
    elif n == "sPre":
        out.push_str("{}_")
        out.push_group(_arg(e, "sub", _MATH, depth))
        out.push_char("^")
        out.push_group(_arg(e, "sup", _MATH, depth))
        out.push_group(_arg(e, "e", _MATH, depth))
    elif n == "rad":
        deg = _arg(e, "deg", _MATH, depth)
        out.push_macro("\\sqrt")
        if _flag(e, "radPr", "degHide") is not True and not deg.empty():
            out.push_char("[")
            out.push_tex(deg)
            out.push_char("]")
        out.push_group(_arg(e, "e", _MATH, depth))
    elif n == "d":
        _delimited(e, out, depth)
    elif n == "nary":
        _nary(e, out, depth)
    elif n == "func":
        name = _find(e, "fName")
        if name is not None:
            _children(name, out, _FUNC, depth + 1)
        out.push_group(_arg(e, "e", _MATH, depth))
    elif n == "acc":
        c = _first(_prop(e, "accPr", "chr"), "̂")
        out.push_command(accent(c) or "\\hat", _arg(e, "e", _MATH, depth))
    elif n == "bar":
        top = _prop(e, "barPr", "pos") == "top"
        out.push_command("\\overline" if top else "\\underline", _arg(e, "e", _MATH, depth))
    elif n == "borderBox":
        out.push_command("\\boxed", _arg(e, "e", _MATH, depth))
    elif n == "phant" and _flag(e, "phantPr", "show") is False:
        out.push_command("\\phantom", _arg(e, "e", _MATH, depth))
    elif n in ("box", "phant"):
        out.push_group(_arg(e, "e", mode, depth))
    elif n == "groupChr":
        _group_char(e, out, depth)
    elif n in ("limLow", "limUpp"):
        _limit(e, n == "limUpp", out, mode, depth)
    elif n == "m":
        _matrix(e, out, depth)
    elif n == "eqArr":
        _equation_array(e, out, depth)
    elif not n.endswith("Pr"):
        _children(e, out, mode, depth)


def _run_text(r):
    """A run's m:t text, or its own text where the tree came from rtf."""
    ts = _find_all(r, "t")
    if ts:
        return "".join("".join(t.itertext()) for t in ts)
    direct = _direct_text(r)
    return direct if direct.strip() else ""


def _run_value(r, name):
    """A run property, under m:rPr or (rtf) on the run itself, where sty and scr are enumeration numbers."""
    p = _find(r, "rPr")
    el = _find(p, name) if p is not None else None
    if el is None:
        el = _find(r, name)
    return _value(el) if el is not None else None


def _run(r, out, mode):
    text = _run_text(r)
    if not text:
        return
    nor = _run_value(r, "nor")
    if nor is not None and _on(nor):
        out.push_text_mode(text)
        return
    if mode == _FUNC:
        name = function_name(text)
        if name is not None:
            out.push_macro(name)
            return
    scr = _run_value(r, "scr")
    font = {"script": "\\mathcal", "1": "\\mathcal", "fraktur": "\\mathfrak", "2": "\\mathfrak",
            "double-struck": "\\mathbb", "3": "\\mathbb", "sans-serif": "\\mathsf", "4": "\\mathsf",
            "monospace": "\\mathtt", "5": "\\mathtt"}.get(scr)
    if font is None:
        font = {"p": "\\mathrm", "0": "\\mathrm", "b": "\\mathbf", "1": "\\mathbf",
                "bi": "\\boldsymbol", "3": "\\boldsymbol"}.get(_run_value(r, "sty"))
        if font == "\\mathrm" and not any(c.isalpha() for c in text):
            font = None
    inner = Tex()
    if mode == _ARRAY:
        for i, piece in enumerate(text.split("&")):
            if i:
                inner.push_str(" & ")
            inner.push_math_text(piece)
    else:
        inner.push_math_text(text)
    if font:
        out.push_command(font, inner)
    else:
        out.push_tex(inner)


def _fraction(e, out, depth):
    num, den = _arg(e, "num", _MATH, depth), _arg(e, "den", _MATH, depth)
    kind = _prop(e, "fPr", "type")
    if kind == "lin":
        out.push_group(num)
        out.push_char("/")
        out.push_group(den)
    elif kind == "skw":
        out.push_str("{}^")
        out.push_group(num)
        out.push_str("/_")
        out.push_group(den)
    elif kind == "noBar":
        out.push_char("{")
        out.push_tex(num)
        out.push_macro("\\atop")
        out.push_tex(den)
        out.push_char("}")
    else:
        out.push_macro("\\frac")
        out.push_group(num)
        out.push_group(den)


def _delimited(e, out, depth):
    def chr_(name, default):  # an explicitly empty character means no delimiter on that side
        v = _prop(e, "dPr", name)
        return default if v is None else _first(v, None)

    beg, end, sep = chr_("begChr", "("), chr_("endChr", ")"), chr_("sepChr", "|")
    out.push_macro("\\left")
    out.push_str(delimiter(beg) if beg else ".")
    for i, part in enumerate(_find_all(e, "e")):
        if i:
            if sep:
                out.push_macro("\\middle")
                out.push_str(delimiter(sep))
            else:
                out.push_char(",")
        inner = Tex()
        _children(part, inner, _MATH, depth + 1)
        out.push_tex(inner)
    out.push_macro("\\right")
    out.push_str(delimiter(end) if end else ".")


def _nary(e, out, depth):
    c = _first(_prop(e, "naryPr", "chr"), "∫")
    op = symbol(c)
    if op is not None and op.startswith("\\"):
        out.push_macro(op)
    else:
        out.push_macro("\\operatorname*")
        inner = Tex()
        inner.push_math_char(c)
        out.push_group(inner)
    integral = c in _INTEGRALS
    loc = _prop(e, "naryPr", "limLoc")
    if loc == "undOvr" and integral:
        out.push_macro("\\limits")
    elif loc == "subSup" and not integral:
        out.push_macro("\\nolimits")
    for name, mark, hide in (("sub", "_", "subHide"), ("sup", "^", "supHide")):
        if _flag(e, "naryPr", hide) is not True:
            part = _arg(e, name, _MATH, depth)
            if not part.empty():
                out.push_char(mark)
                out.push_group(part)
    out.push_group(_arg(e, "e", _MATH, depth))


def _group_char(e, out, depth):
    c = _first(_prop(e, "groupChrPr", "chr"), "⏟")
    top = _prop(e, "groupChrPr", "pos") == "top"
    body = _arg(e, "e", _MATH, depth)
    cmd = accent(c)
    if cmd:
        out.push_command(cmd, body)
        return
    mark = Tex()
    mark.push_math_char(c)
    out.push_macro("\\overset" if top else "\\underset")
    out.push_group(mark)
    out.push_group(body)


def _limit(e, upper, out, mode, depth):
    base, lim = _arg(e, "e", mode, depth), _arg(e, "lim", _MATH, depth)
    if mode == _FUNC:  # \lim_{x \to 0}: the operator takes its own limits
        out.push_tex(base)
        out.push_char("^" if upper else "_")
        out.push_group(lim)
        return
    out.push_macro("\\overset" if upper else "\\underset")
    out.push_group(lim)
    out.push_group(base)


def _matrix(e, out, depth):
    out.push_str("\\begin{matrix}")
    for i, row in enumerate(_find_all(e, "mr")):
        if i:
            out.push_str(" \\\\")
        for j, cell in enumerate(_find_all(row, "e")):
            out.push_str(" & " if j else " ")
            inner = Tex()
            _children(cell, inner, _MATH, depth + 2)
            out.push_tex(inner)
    out.push_str(" \\end{matrix}")


def _equation_array(e, out, depth):
    rows = []
    for row in _find_all(e, "e"):
        inner = Tex()
        _children(row, inner, _ARRAY, depth + 1)
        rows.append(inner)
    env = "aligned" if any("&" in p for row in rows for p in row.parts) else "gathered"
    out.push_str(f"\\begin{{{env}}}")
    for i, row in enumerate(rows):
        if i:
            out.push_str(" \\\\")
        out.push_char(" ")
        out.push_tex(row)
    out.push_str(f" \\end{{{env}}}")
