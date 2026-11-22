"""Small text helpers shared by the format parsers."""
import re


GARBLED = 0.05   # a garble_ratio past it makes the text "garbled" (reader.convert, pub_2k's 97 text)
_BAD = re.compile("[\ufffd\x00-\x08\x0b-\x1f\x7f-\x9f]")


def garble_ratio(s: str) -> float:
    """Share of U+FFFD and control characters other than tab and newline."""
    if not s:
        return 0.0
    return len(_BAD.findall(s)) / len(s)


def squash(s: str) -> str:
    """LF line endings, runs of spaces to one, no trailing spaces per line."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r" {2,}", " ", s)
    return re.sub(r" +$", "", s, flags=re.M)


# ---- legacy 8-bit text: the Windows code page of a language id (LID), after anydoc formats/doc/mod.rs
_LID_EXACT = {0x0C1A: "cp1251", 0x1C1A: "cp1251"}  # Serbian and Bosnian Cyrillic, whose primary 0x1A is Croatian
_PRIMARY = {lang: cp for cp, langs in (
    ("cp949", (0x12,)), ("cp932", (0x11,)), ("cp1251", (0x19, 0x22, 0x23, 0x02)), ("cp1253", (0x08,)),
    ("cp1254", (0x1F,)), ("cp1255", (0x0D,)), ("cp1256", (0x01, 0x29, 0x20)), ("cp1257", (0x25, 0x26, 0x27)),
    ("cp1258", (0x2A,)), ("cp874", (0x1E,)), ("cp1250", (0x05, 0x0E, 0x15, 0x18, 0x1A, 0x1B, 0x24, 0x1C)),
) for lang in langs}
_TRADITIONAL = (0x04, 0x0C, 0x14)  # Chinese with this sub-language byte (Taiwan, Hong Kong, Macao) is Big5


def codepage(lid: int) -> str:
    """The Python codec of a LID's ANSI code page: the full LID's exception first, then its primary language
    (lid & 0x3FF); Chinese picks Big5 or GBK by the sub-language byte; anything else is cp1252."""
    if lid in _LID_EXACT:
        return _LID_EXACT[lid]
    primary = lid & 0x3FF
    if primary == 0x04:
        return "cp950" if (lid >> 8) & 0xFF in _TRADITIONAL else "cp936"
    return _PRIMARY.get(primary, "cp1252")


def decode_8bit(data: bytes, lid: int) -> str:
    """8-bit text in the code page of `lid`; a byte the code page does not define becomes U+FFFD."""
    return bytes(data).decode(codepage(lid), "replace")


# Adobe Symbol encoding (Unicode's SYMBOL.TXT, codes 0x20 to 0xFE): a Symbol-font run's bytes to Unicode.
# Where SYMBOL.TXT maps to a private-use piece (radical extender, bracket and brace parts), the nearest plain or
# Mathematical Symbols character is used. 0x7F to 0x9F and 0xF0 are undefined.
SYMBOL: dict[int, str] = {
    0x20: ' ',
    0x21: '!',
    0x22: '∀',
    0x23: '#',
    0x24: '∃',
    0x25: '%',
    0x26: '&',
    0x27: '∋',
    0x28: '(',
    0x29: ')',
    0x2A: '∗',
    0x2B: '+',
    0x2C: ',',
    0x2D: '−',
    0x2E: '.',
    0x2F: '/',
    0x30: '0',
    0x31: '1',
    0x32: '2',
    0x33: '3',
    0x34: '4',
    0x35: '5',
    0x36: '6',
    0x37: '7',
    0x38: '8',
    0x39: '9',
    0x3A: ':',
    0x3B: ';',
    0x3C: '<',
    0x3D: '=',
    0x3E: '>',
    0x3F: '?',
    0x40: '≅',
    0x41: 'Α',
    0x42: 'Β',
    0x43: 'Χ',
    0x44: 'Δ',
    0x45: 'Ε',
    0x46: 'Φ',
    0x47: 'Γ',
    0x48: 'Η',
    0x49: 'Ι',
    0x4A: 'ϑ',
    0x4B: 'Κ',
    0x4C: 'Λ',
    0x4D: 'Μ',
    0x4E: 'Ν',
    0x4F: 'Ο',
    0x50: 'Π',
    0x51: 'Θ',
    0x52: 'Ρ',
    0x53: 'Σ',
    0x54: 'Τ',
    0x55: 'Υ',
    0x56: 'ς',
    0x57: 'Ω',
    0x58: 'Ξ',
    0x59: 'Ψ',
    0x5A: 'Ζ',
    0x5B: '[',
    0x5C: '∴',
    0x5D: ']',
    0x5E: '⊥',
    0x5F: '_',
    0x60: '‾',
    0x61: 'α',
    0x62: 'β',
    0x63: 'χ',
    0x64: 'δ',
    0x65: 'ε',
    0x66: 'φ',
    0x67: 'γ',
    0x68: 'η',
    0x69: 'ι',
    0x6A: 'ϕ',
    0x6B: 'κ',
    0x6C: 'λ',
    0x6D: 'μ',
    0x6E: 'ν',
    0x6F: 'ο',
    0x70: 'π',
    0x71: 'θ',
    0x72: 'ρ',
    0x73: 'σ',
    0x74: 'τ',
    0x75: 'υ',
    0x76: 'ϖ',
    0x77: 'ω',
    0x78: 'ξ',
    0x79: 'ψ',
    0x7A: 'ζ',
    0x7B: '{',
    0x7C: '|',
    0x7D: '}',
    0x7E: '∼',
    0xA0: '€',
    0xA1: 'ϒ',
    0xA2: '′',
    0xA3: '≤',
    0xA4: '⁄',
    0xA5: '∞',
    0xA6: 'ƒ',
    0xA7: '♣',
    0xA8: '♦',
    0xA9: '♥',
    0xAA: '♠',
    0xAB: '↔',
    0xAC: '←',
    0xAD: '↑',
    0xAE: '→',
    0xAF: '↓',
    0xB0: '°',
    0xB1: '±',
    0xB2: '″',
    0xB3: '≥',
    0xB4: '×',
    0xB5: '∝',
    0xB6: '∂',
    0xB7: '•',
    0xB8: '÷',
    0xB9: '≠',
    0xBA: '≡',
    0xBB: '≈',
    0xBC: '…',
    0xBD: '⏐',
    0xBE: '⎯',
    0xBF: '↵',
    0xC0: 'ℵ',
    0xC1: 'ℑ',
    0xC2: 'ℜ',
    0xC3: '℘',
    0xC4: '⊗',
    0xC5: '⊕',
    0xC6: '∅',
    0xC7: '∩',
    0xC8: '∪',
    0xC9: '⊃',
    0xCA: '⊇',
    0xCB: '⊄',
    0xCC: '⊂',
    0xCD: '⊆',
    0xCE: '∈',
    0xCF: '∉',
    0xD0: '∠',
    0xD1: '∇',
    0xD2: '®',
    0xD3: '©',
    0xD4: '™',
    0xD5: '∏',
    0xD6: '√',
    0xD7: '⋅',
    0xD8: '¬',
    0xD9: '∧',
    0xDA: '∨',
    0xDB: '⇔',
    0xDC: '⇐',
    0xDD: '⇑',
    0xDE: '⇒',
    0xDF: '⇓',
    0xE0: '◊',
    0xE1: '〈',
    0xE2: '®',
    0xE3: '©',
    0xE4: '™',
    0xE5: '∑',
    0xE6: '⎛',
    0xE7: '⎜',
    0xE8: '⎝',
    0xE9: '⎡',
    0xEA: '⎢',
    0xEB: '⎣',
    0xEC: '⎧',
    0xED: '⎨',
    0xEE: '⎩',
    0xEF: '⎪',
    0xF1: '〉',
    0xF2: '∫',
    0xF3: '⌠',
    0xF4: '⎮',
    0xF5: '⌡',
    0xF6: '⎞',
    0xF7: '⎟',
    0xF8: '⎠',
    0xF9: '⎤',
    0xFA: '⎥',
    0xFB: '⎦',
    0xFC: '⎫',
    0xFD: '⎬',
    0xFE: '⎭',
}
