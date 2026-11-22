"""Rules the docm (ooxml_word) and doc readers share so both read a document alike: a field instruction's link
target, the note a comment becomes, a symbol font's characters and the properties line every Word format fills
(Doc.meta["properties"], which markdown renders as one line at the top)."""
import re

from ..model import Para, Text
from ..text import SYMBOL

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


MAX_PROPERTY, MAX_PROPERTIES = 200, 32  # characters kept of a property's value, and properties kept a document
_ORDER = ("subject", "author", "last author", "created", "modified", "company", "keywords")
_DATE = re.compile(r"^(\d{4}-\d\d-\d\d)(?:[T ](\d\d:\d\d))?")
# Wingdings codes 0x20 to 0xFE as Unicode (Unicode 7.0 Wingdings mapping, L2/11-052); 0x7F and the Windows logo at 0xFF have none.
WINGDINGS = dict(zip(range(0x20, 0x100), (
    " ✏✂✁👓🕭🕮🕯🕿✆🖂🖃📪📫📬📭📁📂📄🗏🗐🗄⌛🖮🖰🖲🖳🖴🖫🖬✇✍"
    "🖎✌👌👍👎☜☞☝☟🖐☺😐☹💣☠🏳🏱✈☼💧❄🕆✞🕈✠✡☪☯ॐ☸♈"
    "♉♊♋♌♍♎♏♐♑♒♓🙰🙵●🔾■□🞐❑❒⬧⧫◆❖⬥⌧⮹⌘🏵🏶🙶🙷\x00"
    "⓪①②③④⑤⑥⑦⑧⑨⑩⓿❶❷❸❹❺❻❼❽❾❿🙢🙠🙡🙣🙞🙜🙝🙟·•"
    "▪⚪🞆🞈◉◎🔿▪◻🟂✦★✶✴✹✵⯐⌖⟡⌑⯑✪✰🕐🕑🕒🕓🕔🕕🕖🕗🕘"
    "🕙🕚🕛⮰⮱⮲⮳⮴⮵⮶⮷🙪🙫🙕🙔🙗🙖🙐🙑🙒🙓⌫⌦⮘⮚⮙⮛⮈⮊⮉⮋🡨"
    "🡪🡩🡫🡬🡭🡯🡮🡸🡺🡹🡻🡼🡽🡿🡾⇦⇨⇧⇩⬄⇳⬀⬁⬃⬂🢬🢭🗶✔🗷🗹"
)))
WINGDINGS.pop(0x7F)


def symbol(font: str, code: int) -> str | None:
    """The character a symbol font's code shows: Symbol through the Adobe Symbol table, Wingdings through WINGDINGS,
    a code in either's private-use form (U+F020-U+F0FF) the same; any other font's code as itself when it is not a
    private-use one. None when nothing is known."""
    face = (font or "").strip().lower()
    low = code - 0xF000 if 0xF020 <= code <= 0xF0FF else code
    if face == "symbol":
        return SYMBOL.get(low)
    if face == "wingdings":
        return WINGDINGS.get(low)
    if 0 < code < 0x110000 and not 0xE000 <= code <= 0xF8FF and not 0xD800 <= code <= 0xDFFF:
        return chr(code)
    return None


def when(value: str) -> str:
    """An ISO 8601 date and time as `YYYY-MM-DD HH:MM` (the date alone when it has no time), else as it is."""
    m = _DATE.match(value.strip())
    return (m.group(1) + (" " + m.group(2) if m.group(2) else "")) if m else value


def properties(pairs) -> list:
    """The (label, value) pairs of a document's properties line (markdown renders it): values squashed to one line
    and cut to MAX_PROPERTY, empty ones, repeats of a label and a last author who is the author left out; the
    labels of _ORDER first in that order, then the others (custom properties) as they came; at most MAX_PROPERTIES."""
    out, seen = [], {}
    for label, value in pairs:
        value = " ".join(str(value or "").split())[:MAX_PROPERTY]
        label = " ".join(str(label or "").split())[:MAX_NAME]
        if value and label and label not in seen:
            seen[label] = value
            out.append((label, value))
    if seen.get("last author") == seen.get("author"):
        out = [p for p in out if p[0] != "last author"]
    out.sort(key=lambda p: _ORDER.index(p[0]) if p[0] in _ORDER else len(_ORDER))
    return out[:MAX_PROPERTIES]
