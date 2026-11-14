"""RTF lexer and code pages.

Ported from MIT sources: the lexer (control words, control symbols, hex escapes, `\\binN`, the
backslash + line-break paragraph mark) and the font charset / code page tables come from anydoc
(`rtf/lexer.rs`, `rtf/tables.rs`); the font-charset to code-page choice, including the Mac,
Johab and DOS charsets, follows xberg (`extractors/rtf/encoding.rs`). Stdlib only.

Token interface. `tokens(data)` yields plain tuples whose first item is the kind::

    (OPEN,)                      "{"
    (CLOSE,)                     "}"
    (WORD, name, param)          control word; name is a str of at most 32 letters,
                                 param is an int clamped to -2**31 .. 2**31-1, or None
    (SYM, char)                  control symbol; char is a one-character str
    (HEX, byte)                  hex escape with two valid hex digits; byte is an int 0..255
    (TEXT, bytes)                a run of plain text bytes (never empty)
    (BIN, offset, n)             the n raw bytes data[offset:offset+n] of a bin control, no copy

Decisions that the parser relies on:

* An escaped brace or backslash is never OPEN or CLOSE. As in anydoc it comes out as
  (SYM, "{"), (SYM, "}") or (SYM, "\\\\"), so the parser must treat those three symbols as
  literal text. Every other control symbol (tilde, hyphen, underscore, star, colon, bar ...)
  is (SYM, char) as well.
* CR, LF and NUL are dropped from the input. A backslash directly before CR or LF is
  (WORD, "par", None); the LF of a CRLF pair is dropped as plain whitespace.
* A quote-backslash escape without two hex digits does not raise. As in anydoc it yields
  (TEXT, b"'") and whatever follows lexes as usual, nothing is swallowed.
* A lone backslash at the very end of the input is dropped.
* The letters of a control word past the 32nd are discarded: the name is cut at 32 letters, so
  a 100-letter word is one 32-letter WORD and its digits, if any, are still its parameter.
* A parameter keeps its sign. A minus sign not followed by a digit is not part of the control
  word and lexes as text. More than 10 significant digits gives the i32 limit of the right
  sign without building a large int.
* One space after a control word, or after its parameter, is eaten; a second space, a tab and a hex escape's following
  space are text.
* What the control symbols mean is the parser's business: `\\*` marks a destination the reader may leave out, `\\~` is
  a no-break space, `\\-` an optional hyphen (dropped), `\\_` a no-break hyphen. The lexer gives each as (SYM, char).
* `\\bin` is always (BIN, offset, n), never a (WORD, "bin", ...): the lexer takes the bytes itself, so the parser never
  sees the word.
* TEXT tokens may be adjacent: text split by a hex escape or a symbol is several TEXT tokens (and HEX) with nothing
  between them, and the parser joins them (Pending) before decoding, so a DBCS pair split across a HEX and a TEXT token
  is one character. A TEXT token never holds CR, LF, NUL, a brace or a backslash.
* A SYM may be any single byte after a backslash except a letter, a quote and CR or LF: NUL, a tab, a space, a high
  byte (as its latin-1 character), digits and punctuation. Symbols the parser does not know are dropped.
* For the bin control: the space is eaten, then (BIN, offset, n) with n = max(0, N) clamped to
  the rest of the input. A missing N, zero and a negative N all give n == 0. Lexing resumes
  right after those n bytes; braces and backslashes inside them are not tokens.
"""
import codecs
import re

OPEN, CLOSE, WORD, SYM, HEX, TEXT, BIN = "open", "close", "word", "sym", "hex", "text", "bin"

I32_MIN, I32_MAX = -2**31, 2**31 - 1
NAME_MAX = 32
# A parameter's digits are all consumed; past this many significant digits the value is the i32 limit of its sign.
# Kept as anydoc reads "at most 10 digits": the lexer never stops at the 10th digit, so `\fs12345678901` is one
# word whose parameter clamps, not `\fs1234567890` followed by text `1`.
PARAM_DIGITS = 10

# One alternative per token kind; the groups are named so m.lastgroup (the outermost group
# that closed last) tells which one matched. Every input byte is covered, so there are no gaps.
_TOKEN = re.compile(
    rb"(?P<t>[^\\{}\r\n\x00]+)"
    rb"|(?P<w>\\(?P<n>[A-Za-z]+)(?P<p>-?[0-9]+)?[ ]?)"
    rb"|(?P<o>\{)"
    rb"|(?P<c>\})"
    rb"|(?P<h>\\'(?P<x>[0-9A-Fa-f]{2}))"
    rb"|(?P<q>\\')"
    rb"|(?P<r>\\[\r\n])"
    rb"|(?P<s>\\[^A-Za-z'\r\n])"
    rb"|(?P<z>[\r\n\x00]+|\\\Z)",
    re.DOTALL,
)

_PAR = (WORD, "par", None)
_OPEN = (OPEN,)
_CLOSE = (CLOSE,)
_QUOTE = (TEXT, b"'")


def _param(raw: bytes) -> int:
    neg = raw[:1] == b"-"
    digits = (raw[1:] if neg else raw).lstrip(b"0")
    if len(digits) > PARAM_DIGITS:
        return I32_MIN if neg else I32_MAX
    value = int(digits) if digits else 0
    if neg:
        value = -value
    return min(max(value, I32_MIN), I32_MAX)


def tokens(data: bytes):
    """Yield the tokens of `data` (see the module docstring). O(len(data))."""
    size = len(data)
    pos = 0
    while pos < size:
        resume = size
        for m in _TOKEN.finditer(data, pos):
            kind = m.lastgroup
            if kind == "t":
                yield (TEXT, m.group())
            elif kind == "w":
                raw = m.group("p")
                param = None if raw is None else _param(raw)
                name = m.group("n")
                if name == b"bin":
                    start = m.end()
                    n = min(max(param or 0, 0), size - start)
                    yield (BIN, start, n)
                    resume = start + n
                    break
                yield (WORD, name[:NAME_MAX].decode("ascii"), param)
            elif kind == "o":
                yield _OPEN
            elif kind == "c":
                yield _CLOSE
            elif kind == "h":
                yield (HEX, int(m.group("x"), 16))
            elif kind == "s":
                yield (SYM, m.group()[1:].decode("latin-1"))
            elif kind == "r":
                yield _PAR
            elif kind == "q":
                yield _QUOTE
            # "z": CR, LF and NUL runs and a trailing lone backslash are dropped
        pos = resume


# Font fcharset -> Python codec. Charsets 0 and 1 mean "the document's ansicpg" (see codec()).
_CHARSET = {
    2: "cp1252", 77: "mac_roman",
    128: "cp932", 129: "cp949", 130: "johab", 134: "gbk", 136: "cp950",
    161: "cp1253", 162: "cp1254", 163: "cp1258",
    177: "cp1255", 178: "cp1256", 179: "cp1256", 180: "cp1256", 186: "cp1257",
    204: "cp1251", 222: "cp874", 238: "cp1250",
    254: "cp437", 255: "cp437",
}
# ansicpg / cpg value -> Python codec.
_CODEPAGE = {
    932: "cp932", 936: "gbk", 949: "cp949", 950: "cp950", 874: "cp874",
    1250: "cp1250", 1251: "cp1251", 1252: "cp1252", 1253: "cp1253", 1254: "cp1254",
    1255: "cp1255", 1256: "cp1256", 1257: "cp1257", 1258: "cp1258",
    437: "cp437", 850: "cp850", 1361: "johab", 10000: "mac_roman",
}
DEFAULT_CODEC = "cp1252"


def codec(charset, cpg, ansicpg) -> str:
    """Python codec name for text under a font's charset, the font's cpg and the ansicpg.

    Order: the charset's code page (charsets 0 and 1 mean the ansicpg), then `cpg`, then
    `ansicpg`, then cp1252. An unknown value falls through to the next source.
    """
    if charset in (0, 1):
        name = _CODEPAGE.get(ansicpg)
        if name:
            return name
    elif charset is not None:
        name = _CHARSET.get(charset)
        if name:
            return name
    return _CODEPAGE.get(cpg) or _CODEPAGE.get(ansicpg) or DEFAULT_CODEC


class Pending:
    """Bytes of consecutive hex escapes and text, decoded together so DBCS pairs survive."""

    __slots__ = ("_parts",)

    def __init__(self):
        self._parts = []

    def __bool__(self):
        return bool(self._parts)

    def push(self, b) -> None:
        """Add the bytes of a TEXT token, or the int of a HEX token. Empty bytes are not added, so they never make
        the pending truthy."""
        b = bytes((b,)) if isinstance(b, int) else bytes(b)
        if b:
            self._parts.append(b)

    def flush(self, codec: str) -> str:
        """Decode and clear the gathered bytes (errors replaced). An unknown codec is cp1252."""
        raw = b"".join(self._parts)
        self._parts.clear()
        if not raw:
            return ""
        try:
            codecs.lookup(codec)
        except (LookupError, TypeError):
            codec = DEFAULT_CODEC
        return raw.decode(codec, errors="replace")
