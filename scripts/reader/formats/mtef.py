"""MathType equations (MTEF v3, as Equation Editor 3.0 writes it, and v5, as MathType does) to TeX: the data an
Equation Native stream holds after its EQNOLEFILEHDR. Logic after dochan office_binary/mtef.py (the record walk, the
size, font and v5 definition records skipped by their layouts, the symbol table, sibling scripts joined, text runs as
\\text); record layouts per Design Science's MTEF v3 and v5 descriptions.

The data is a header (v3: version, platform, product, version, subversion; v5: those, the application key, a
null-terminated string, and an options byte), then an object list: records up to an END (0). A record is a tag (v3:
the type in the low nibble, its options in the high one; v5: the type, then an options byte for LINE to EMBELL), a
nudge when the options say so, and its fields; LINE, TMPL, PILE, MATRIX and a CHAR with embellishments then hold an
object list of their own. A LINE is a row of objects (a template's slot; a null one is empty), a CHAR a character (its
MTCode is Unicode; typeface 0x81 is text), a TMPL a template over its slots and characters: fences (selectors 0-7),
roots, fractions, under and over bars and scripts are TeX here, sibling scripts one script. A PILE is lines one under
another, a MATRIX cells row by row, an EMBELL a CHAR's dot, prime, tilde, hat, arrow or bar. RULER, FONT, SIZE, the
size records (FULL to SUBSYM) and v5's COLOR, COLOR_DEF, FONT_DEF, EQN_PREFS and ENCODING_DEF only change how it
looks and are skipped.

Bounds: every field is read through take(), which stops at the data's end, and a string takes at most _STRING bytes.
Object lists nested more than DEPTH below the top one are walked without being built, their content reported lost as
"depth". A record of an unknown type or with options whose fields are not known here (a LINE's xfLSPACE, a v5 CHAR
without MTCode), a template with no TeX here (a v3 fence of another variation than 0, 1 or 2 too), or data cut short
stops the conversion: the TeX built so far is kept and reported lost as "stop". A private-use character and an
embellishment with no TeX here are left out, the TeX balanced, and reported lost as "symbol". Each record costs O(its
bytes), and the TeX it adds is bounded by them, so the walk and the TeX are linear in the input (a nested list's TeX
is copied once a level, at most DEPTH times).

Where dochan differs: it reads only v5 fractions and fences (here v5's roots, bars and scripts too) and v3 fences of
variation 0 (here 1 and 2, one-sided, too), rejects embellishments, rulers and characters outside its table, raises
past 64 levels, and rejects bytes after the last END; here embellishments and rulers are read, other printable
characters are kept, deeper lists are dropped and counted, and bytes after the top END are ignored.
"""
import struct
import unicodedata

DEPTH = 32           # object lists below the top one that are built; deeper ones are walked and reported ("depth")
MAX_BYTES = 1 << 20  # MTEF bytes to_tex reads; more is a ValueError
_STRING = 256        # bytes a null-terminated string may take, its zero included

_END, _LINE, _CHAR, _TMPL, _PILE, _MATRIX, _EMBELL, _RULER, _FONT, _SIZE = range(10)
_SIZES = range(10, 15)  # FULL, SUB, SUB2, SYM, SUBSYM
_COLOR, _COLOR_DEF, _FONT_DEF, _EQN_PREFS, _ENCODING_DEF = range(15, 20)  # v5 only (v5's 8 is FONT_STYLE_DEF)
_SCRIPT = -1            # a script template's node: its (sub, sup) wait for the LINE to join them
_SLOTS = (_LINE, _PILE)
_NUDGE = 0x08
_TEXT_FACE = 0x81       # fnTEXT + 128: a CHAR in text style

# TMPL selector -> template (fences, 0-7, are both versions')
_TEMPLATES = {3: {13: "root", 14: "fraction", 15: "script", 16: "under", 17: "over"},
              5: {10: "root", 11: "fraction", 12: "under", 13: "over", 27: "sub", 28: "sup", 29: "script"}}
_FENCES = {0: (r"\langle ", r"\rangle "), 1: ("(", ")"), 2: (r"\{", r"\}"), 3: ("[", "]"), 4: ("|", "|"),
           5: (r"\| ", r"\| "), 6: (r"\lfloor ", r"\rfloor "), 7: (r"\lceil ", r"\rceil ")}
_DELIMITERS = frozenset(d for pair in _FENCES.values() for d in pair) | {"/", "."}
_CLOSERS = frozenset((")", "]", r"\}", r"\rangle ", r"\rfloor ", r"\rceil "))  # a fence CHAR on the right side
_MT_SPACES = ("", "", "")  # MathType's space glyphs (dochan's)
# EMBELL type -> TeX before and after its character (MTEF v3 and v5 share 2-18); another is left out
_EMBELLISH = {2: (r"\dot{", "}"), 3: (r"\ddot{", "}"), 4: (r"\dddot{", "}"), 5: ("", "'"), 6: ("", "''"),
              8: (r"\tilde{", "}"), 9: (r"\hat{", "}"), 10: (r"\not{", "}"), 11: (r"\vec{", "}"),
              12: (r"\overleftarrow{", "}"), 13: (r"\overleftrightarrow{", "}"), 17: (r"\overline{", "}"),
              18: ("", "'''")}

# MTCode characters -> TeX (dochan's table, its MathType spaces among them, and the fence delimiters)
_SYMBOLS = {
    "α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "δ": r"\delta ", "ε": r"\epsilon ", "ζ": r"\zeta ",
    "η": r"\eta ", "θ": r"\theta ", "ι": r"\iota ", "κ": r"\kappa ", "λ": r"\lambda ", "μ": r"\mu ", "ν": r"\nu ",
    "ξ": r"\xi ", "π": r"\pi ", "ρ": r"\rho ", "σ": r"\sigma ", "τ": r"\tau ", "υ": r"\upsilon ", "φ": r"\phi ",
    "χ": r"\chi ", "ψ": r"\psi ", "ω": r"\omega ", "Γ": r"\Gamma ", "Δ": r"\Delta ", "Θ": r"\Theta ",
    "Λ": r"\Lambda ", "Ξ": r"\Xi ", "Π": r"\Pi ", "Σ": r"\Sigma ", "Υ": r"\Upsilon ", "Φ": r"\Phi ",
    "Ψ": r"\Psi ", "Ω": r"\Omega ", "ϕ": r"\varphi ", "ϑ": r"\vartheta ",
    "±": r"\pm ", "∓": r"\mp ", "×": r"\times ", "÷": r"\div ", "⋅": r"\cdot ", "·": r"\cdot ", "•": r"\bullet ",
    "−": "-", "≤": r"\leq ", "≥": r"\geq ", "≠": r"\neq ", "≈": r"\approx ", "≡": r"\equiv ", "∞": r"\infty ",
    "∂": r"\partial ", "∇": r"\nabla ", "∈": r"\in ", "∉": r"\notin ", "⊂": r"\subset ", "⊆": r"\subseteq ",
    "∪": r"\cup ", "∩": r"\cap ", "∅": r"\emptyset ", "∑": r"\sum ", "∏": r"\prod ", "∫": r"\int ", "→": r"\to ",
    "←": r"\leftarrow ", "↔": r"\leftrightarrow ", "⇒": r"\Rightarrow ", "∀": r"\forall ", "∃": r"\exists ",
    "…": r"\ldots ", "⋯": r"\cdots ", "°": r"^{\circ}", "⊗": r"\otimes ", "~": r"\sim ",
    "{": r"\{", "}": r"\}", "%": r"\%", "#": r"\#", "&": r"\&", "$": r"\$", "_": r"\_", "^": r"\hat{}",
    "\\": r"\backslash ", "": r"{\ }", "": r"{\ }", "": r"{\ }",
    "⟨": r"\langle ", "〈": r"\langle ", "〈": r"\langle ",
    "⟩": r"\rangle ", "〉": r"\rangle ", "〉": r"\rangle ",
    "⌊": r"\lfloor ", "⌋": r"\rfloor ", "⌈": r"\lceil ", "⌉": r"\rceil ", "‖": r"\| ",
}
_TEXT_ESCAPES = {"\\": r"\textbackslash{}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
                 **{c: "\\" + c for c in "{}%#&$_"}}


class _Stop(Exception):
    """A record this reader cannot walk, or the end of the data: the conversion ends with what was built."""


class _Node:
    """A record's TeX: kind (its record type, or _SCRIPT), text, plain (a text-style character, joined into \\text),
    scripts ((sub, sup) of a script template) and code (an EMBELL's type)."""
    __slots__ = ("kind", "text", "plain", "scripts", "code")

    def __init__(self, kind, text="", plain=False, scripts=None, code=0):
        self.kind, self.text, self.plain, self.scripts, self.code = kind, text, plain, scripts, code


def _text(s) -> str:
    """Text-style characters as TeX: in \\text{...} unless every one is an ASCII digit or sign, which math sets
    upright already."""
    return s if all(c.isascii() and not c.isalpha() and not c.isspace() for c in s) else r"\text{" + s + "}"


def _join(nodes) -> str:
    """A LINE's objects as TeX (dochan's join): runs of text characters become one _text, and sibling script
    templates one _{...}^{...} after the object before them. Objects without TeX (a character left out, an
    embellishment outside a character) are skipped."""
    out, text, sub, sup = [], [], [], []
    for n in nodes:
        if n.kind != _SCRIPT and not n.text:
            continue
        if n.kind == _SCRIPT:
            if text:
                out.append(_text("".join(text)))
                text = []
            if n.scripts[0]:
                sub.append(n.scripts[0])
            if n.scripts[1]:
                sup.append(n.scripts[1])
            continue
        if sub or sup:
            out.append(("_{" + "".join(sub) + "}" if sub else "") + ("^{" + "".join(sup) + "}" if sup else ""))
            sub, sup = [], []
        if n.plain:
            text.append(n.text)
        else:
            if text:
                out.append(_text("".join(text)))
                text = []
            out.append(n.text)
    if sub or sup:
        out.append(("_{" + "".join(sub) + "}" if sub else "") + ("^{" + "".join(sup) + "}" if sup else ""))
    if text:
        out.append(_text("".join(text)))
    return "".join(out)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0
        self.dropped = self.stopped = self.missing = False  # what to_tex reports lost: depth, stop, symbol
        try:
            self.v = self.byte()
            if self.v not in (3, 5):
                raise ValueError(f"MTEF version {self.v}")
            self.take(4)  # platform, product, product version and subversion
            if self.v == 5:
                self.cstring()  # the application key
                self.take(1)    # the equation options
        except _Stop:
            raise ValueError("MTEF header cut short") from None
        self.templates = _TEMPLATES[self.v]

    # ---- fields
    def take(self, n) -> bytes:
        pos = self.pos
        if pos + n > len(self.data):
            raise _Stop
        self.pos = pos + n
        return self.data[pos:pos + n]

    def byte(self) -> int:
        pos = self.pos
        if pos >= len(self.data):
            raise _Stop
        self.pos = pos + 1
        return self.data[pos]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def uint(self) -> int:
        """MTEF's uint: a byte, or 255 and a u16."""
        b = self.byte()
        return self.u16() if b == 255 else b

    def cstring(self):
        end = self.data.find(b"\0", self.pos, self.pos + _STRING)
        if end < 0:
            raise _Stop
        self.pos = end + 1

    def tag(self) -> tuple[int, int]:
        """A record's type and options."""
        t = self.byte()
        if self.v == 5:
            return t, (self.byte() if _LINE <= t <= _EMBELL else 0)
        return t & 15, t >> 4

    def ruler(self):
        """A RULER record after a LINE or PILE: its tag, the tab stop count, a type byte and a u16 offset each."""
        if self.byte() != _RULER:
            raise _Stop
        self.take(3 * self.byte())

    def prefs(self):
        """v5 EQN_PREFS: options (none known), the sizes and the spaces (a count of values, each nibbles ending with
        0xF, the list ending on a byte), then the styles (a count, each a font definition index and, when not 0, a
        style byte); dochan's layout."""
        if self.byte():
            raise _Stop
        for _ in range(2):
            count, done = self.byte(), 0
            while done < count:
                b = self.byte()
                done += b >> 4 == 15
                if done < count:
                    done += b & 15 == 15
        for _ in range(self.byte()):
            if self.uint():
                self.byte()

    def head(self, kind, opts) -> tuple[bool, object]:
        """The fields of a record whose tag (and v5 options byte) has been read: whether an object list follows, and
        what the record needs (a CHAR's typeface and MTCode, a TMPL's selector and variation, a PILE's alignment, a
        MATRIX's columns, an EMBELL's type)."""
        if opts & _NUDGE:
            if self.take(2) == b"\x80\x80":
                self.take(4)
            opts &= ~_NUDGE
        v5 = self.v == 5
        if kind == _LINE:
            if opts & ~3:  # xfLSPACE: the spacing's layout is not read here
                raise _Stop
            if opts & 2:
                self.ruler()
            return not opts & 1, None
        if kind == _CHAR:
            # v3: xfAUTO 1, xfEMBELL 2; v5: EMBELL 1, FUNC_START 2, ENC_CHAR_8 4, ENC_CHAR_16 0x10 (NO_MTCODE stops)
            if opts & ~(0x17 if v5 else 3):
                raise _Stop
            face, code = self.byte(), self.u16()
            if v5:
                self.take((1 if opts & 4 else 0) + (2 if opts & 0x10 else 0))  # font positions after the MTCode
            return bool(opts & (1 if v5 else 2)), (face, code)
        if kind == _TMPL:
            if opts:
                raise _Stop
            sel, var = self.byte(), self.byte()
            if v5 and var & 0x80:  # a second variation byte (dochan's reading)
                var = var & 0x7F | self.byte() << 7
            self.byte()  # template-specific options
            return True, (sel, var)
        if kind == _PILE:
            if opts & ~2:
                raise _Stop
            halign, _ = self.take(2)
            if opts & 2:
                self.ruler()
            return True, halign
        if kind == _MATRIX:
            if opts:
                raise _Stop
            _, _, _, rows, cols = self.take(5)  # valign, h_just, v_just, rows, cols
            self.take((rows + 4) // 4 + (cols + 4) // 4)  # rows + 1 and cols + 1 partition lines, 2 bits each
            return True, cols
        if opts:
            raise _Stop
        if kind == _EMBELL:
            return False, self.byte()
        if kind == _RULER:
            self.take(3 * self.byte())
        elif kind == _FONT:
            if v5:  # FONT_STYLE_DEF: a font definition index and a style
                self.uint()
                self.byte()
            else:   # typeface, style, name
                self.take(2)
                self.cstring()
        elif kind == _SIZE:
            lsize = self.byte()
            self.take(2 if lsize == 101 else 3 if lsize == 100 else 1)
        elif kind in _SIZES:
            pass
        elif not v5:
            raise _Stop
        elif kind == _COLOR:
            self.uint()
        elif kind == _COLOR_DEF:
            o = self.byte()  # CMYK 1, SPOT 2, NAME 4: four or three u16 values, then the name
            self.take(8 if o & 1 else 6)
            if o & 4:
                self.cstring()
        elif kind == _FONT_DEF:
            self.uint()
            self.cstring()
        elif kind == _EQN_PREFS:
            self.prefs()
        elif kind == _ENCODING_DEF:
            self.cstring()
        else:
            raise _Stop
        return False, None

    # ---- object lists
    def items(self, depth) -> list:
        """The object list at `depth` (the top one 0) up to its END, as nodes; what was built when the data stops."""
        out = []
        try:
            while True:
                kind, opts = self.tag()
                if kind == _END:
                    if opts:
                        raise _Stop
                    return out
                node = self.record(kind, opts, depth)
                if node is not None:
                    out.append(node)
                if self.stopped:
                    return out
        except _Stop:
            self.stopped = True
            return out

    def sub(self, depth) -> list:
        """The object list a record at `depth` holds; past DEPTH it is walked without being built."""
        if depth < DEPTH:
            return self.items(depth + 1)
        self.dropped = True
        self.skip()
        return []

    def skip(self):
        """Walk the rest of an object list, the lists inside it too, building nothing."""
        lists = 1
        while lists:
            kind, opts = self.tag()
            if kind == _END:
                if opts:
                    raise _Stop
                lists -= 1
            else:
                lists += self.head(kind, opts)[0]

    def record(self, kind, opts, depth):
        opens, value = self.head(kind, opts)
        if kind == _LINE:
            return _Node(_LINE, _join(self.sub(depth)) if opens else "")
        if kind == _CHAR:
            node = self.char(*value)
            return self.embellish(node, self.sub(depth)) if opens else node
        if kind == _TMPL:
            sel, var = value
            name = self.templates.get(sel) or ("fence" if sel in _FENCES else None)
            if name is None or (name == "fence" and self.v == 3 and var > 2):  # v3 fences: 0 both, 1 left, 2 right
                raise _Stop
            return self.template(name, sel, var, self.sub(depth))
        if kind == _PILE:
            lines = [n.text for n in self.sub(depth) if n.kind in _SLOTS]
            if len(lines) < 2:
                return _Node(_PILE, "".join(lines))
            col = {1: "l", 3: "r"}.get(value, "c")
            return _Node(_PILE, r"\begin{array}{" + col + "}" + r"\\".join(lines) + r"\end{array}")
        if kind == _MATRIX:
            cells = [n.text for n in self.sub(depth) if n.kind in _SLOTS]
            cols = max(value, 1)
            rows = ["&".join(cells[i:i + cols]) for i in range(0, len(cells), cols)]
            return _Node(_MATRIX, r"\begin{matrix}" + r"\\".join(rows) + r"\end{matrix}" if rows else "")
        if kind == _EMBELL:
            return _Node(_EMBELL, code=value)
        return None

    # ---- TeX
    def char(self, face, code) -> _Node:
        """A character: text style (typeface 0x81) escaped for \\text; else dochan's symbols, other printable
        characters as they are. A space isprintable rejects (Unicode Zs other than U+0020, a line or paragraph
        separator) and MathType's space glyphs are a space in text and {\\ } in math. A private-use (MathType's own
        glyphs), surrogate or unassigned character is a lost symbol; control and format characters (an invisible
        operator such as U+2061) are left out, no loss."""
        ch = chr(code)
        text = face == _TEXT_FACE
        if text and ch.isprintable():
            return _Node(_CHAR, _TEXT_ESCAPES.get(ch, ch), True)
        tex = None if text else _SYMBOLS.get(ch)
        if tex is None:
            cat = unicodedata.category(ch)
            if not text and ch.isprintable():
                tex = ch
            elif cat[0] == "Z" or ch in _MT_SPACES:
                tex = " " if text else r"{\ }"
            else:
                self.missing |= cat in ("Co", "Cs", "Cn")
                tex = ""
        return _Node(_CHAR, tex, text)

    def embellish(self, node, kids) -> _Node:
        """A character under its embellishments, each over what the ones before it made: TeX before the character
        and after it, in lists, so many cost linear time. An embellishment with no TeX here is a lost symbol, its
        character kept."""
        before, after = [], []
        for k in kids:
            if k.kind != _EMBELL:
                continue
            e = _EMBELLISH.get(k.code)
            if e is None:
                self.missing = True
            else:
                before.append(e[0])
                after.append(e[1])
        if not before:
            return node
        base = _text(node.text) if node.plain else node.text
        return _Node(_CHAR, "".join(reversed(before)) + base + "".join(after))

    def template(self, name, sel, var, kids) -> _Node:
        """A template's TeX from its slots (LINEs and PILEs, missing ones empty) and, for a fence, its CHARs. A root's
        slots are the radicand and the index; a script's the subscript and the superscript (tmSUP with one slot, the
        superscript)."""
        slots = [k.text for k in kids if k.kind in _SLOTS]
        a, b = (slots + ["", ""])[:2]
        if name == "fence":
            return _Node(_TMPL, self.fence(sel, var, a, [k.text for k in kids if k.kind == _CHAR]))
        if name == "fraction":
            return _Node(_TMPL, r"\frac{" + a + "}{" + b + "}")
        if name == "root":
            return _Node(_TMPL, (r"\sqrt[" + b + "]{" if b else r"\sqrt{") + a + "}")
        if name in ("under", "over"):
            return _Node(_TMPL, "\\" + name + "line{" + a + "}")
        if name == "sup" and len(slots) == 1:
            return _Node(_SCRIPT, scripts=("", a))
        return _Node(_SCRIPT, scripts=(a, b))

    def fence(self, sel, var, body, chars) -> str:
        """Delimiters around a body. The variation says which sides are there (v5: tvFENCE_L 1, tvFENCE_R 2; v3: 0
        both, 1 the left, 2 the right, as record() checked), an absent side a '.'. The template's CHARs give the
        sides that are there in order; with fewer CHARs than sides, a CHAR goes to the side it opens or closes and
        the other keeps the template's own. A CHAR that is no delimiter is a '.'."""
        sides = list(_FENCES[sel])
        bits = var if self.v == 5 else var or 3
        present = [i for i in (0, 1) if bits & (1 << i)]
        for i in (0, 1):
            if i not in present:
                sides[i] = "."
        pairs = zip(present, chars) if len(chars) >= len(present) else [(int(ch in _CLOSERS), ch) for ch in chars]
        for i, ch in pairs:
            sides[i] = ch if ch in _DELIMITERS else "."
        return r"\left" + sides[0] + body + r"\right" + sides[1]


def native_tex(data: bytes, lost: set | None = None) -> str:
    """The TeX of an Equation Native stream `data`: its EQNOLEFILEHDR (cbHdr, a u16 first, the header's length)
    and then MTEF, converted by to_tex (which has `lost`). ValueError for a stream too short or a cbHdr past it, and
    for what to_tex refuses."""
    if len(data) < 2:
        raise ValueError("Equation Native too short")
    cb = struct.unpack_from("<H", data)[0]
    if cb > len(data):
        raise ValueError("Equation Native header past the stream")
    return to_tex(data[cb:], lost)


def to_tex(data: bytes, lost: set | None = None) -> str:
    """MTEF v3 or v5 `data` as TeX, without math delimiters. What could not be read is added to `lost`, when given:
    "depth" for lists dropped past DEPTH, "stop" when the conversion stopped before the end, "symbol" for characters
    and embellishments left out (char(), embellish()). ValueError for data past MAX_BYTES, another version, a header
    cut short, or no TeX at all."""
    if len(data) > MAX_BYTES:
        raise ValueError("MTEF data too large")
    r = _Reader(bytes(data))
    tex = _join(r.items(0)).strip()
    if not tex:
        raise ValueError("no equation")
    if lost is not None:
        for marker, hit in (("depth", r.dropped), ("stop", r.stopped), ("symbol", r.missing)):
            if hit:
                lost.add(marker)
    return tex
