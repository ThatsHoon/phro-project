"""Small text helpers shared by the format parsers."""
import re


def garble_ratio(s: str) -> float:
    """Share of U+FFFD and control characters other than tab and newline."""
    if not s:
        return 0.0
    bad = sum(1 for ch in s if ch == "�" or (ord(ch) < 32 and ch not in "\t\n") or 127 <= ord(ch) < 160)
    return bad / len(s)


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
