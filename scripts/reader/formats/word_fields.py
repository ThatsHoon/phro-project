"""Rules the docm (ooxml_word) and doc readers share so both read a document alike: a field instruction's link
target, and the note a comment becomes."""
import re

from ..model import Para, Text

MAX_URL, MAX_INSTR = 1000, 300  # characters kept of a link target, and read of a field instruction
MAX_NAME = 64  # characters kept of a comment's author
_HYPERLINK = re.compile(r'^\s*HYPERLINK\s+(?:"([^"]*)"|([^\s"\\]+))', re.I)


def hyperlink_url(instr: str) -> str:
    """The target of a HYPERLINK field instruction, cut to MAX_URL; "" for any other field and for a bookmark
    (\\l), whose text stays plain."""
    m = _HYPERLINK.match(instr or "")
    return ((m.group(1) if m.group(1) is not None else m.group(2)) or "")[:MAX_URL] if m else ""


def comment_note(blocks: list, author: str) -> list:
    """A comment's blocks as its note: "Comment by <author>: " ("Comment: " without an author), the author cut to MAX_NAME, before the first
    paragraph's text, or as a paragraph of its own when the comment does not start with one. Changes `blocks`."""
    lead = Text(f"Comment by {author[:MAX_NAME]}: " if author else "Comment: ")
    if blocks and isinstance(blocks[0], Para):
        blocks[0].inlines.insert(0, lead)
    else:
        blocks.insert(0, Para([lead]))
    return blocks
