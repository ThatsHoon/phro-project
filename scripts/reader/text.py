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
