"""Word field instructions, one rule for the docm (ooxml_word) and doc readers so both read a field alike."""
import re

MAX_URL, MAX_INSTR = 1000, 300  # characters kept of a link target, and read of a field instruction
_HYPERLINK = re.compile(r'^\s*HYPERLINK\s+(?:"([^"]*)"|([^\s"\\]+))', re.I)


def hyperlink_url(instr: str) -> str:
    """The target of a HYPERLINK field instruction, cut to MAX_URL; "" for any other field and for a bookmark
    (\\l), whose text stays plain."""
    m = _HYPERLINK.match(instr or "")
    return ((m.group(1) if m.group(1) is not None else m.group(2)) or "")[:MAX_URL] if m else ""
