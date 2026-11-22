"""Rules the docm (ooxml_word) and doc readers share so both read a document alike: a field instruction's link
target, the note a comment becomes, a symbol font's characters and the properties line every Word format fills
(Doc.meta["properties"], which markdown renders as one line at the top)."""
import re
import struct

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
WINGDINGS2 = {0x4F: "✗", 0x50: "✓", 0x51: "☒", 0x52: "☑", 0x53: "☒", 0x54: "☒", 0xA3: "☐"}
_PICTURE_FONTS = frozenset(("webdings", "wingdings 2", "wingdings 3", "mt extra", "marlett", "bookshelf symbol 7",
                            "zapfdingbats", "zapf dingbats", "monotype sorts", "ms outlook", "ms reference specialty"))
_APP_PROPERTY = re.compile(r"^(?:KSOProductBuildVer|ICV|ContentTypeId|MSIP_Label_|_dlc_|_MarkAsFinal$|"
                           r"MediaServiceImageTags$|GrammarlyDocumentId$|_NewReviewCycle$|TaxCatchAll)", re.I)
_PAGE_SHELL = re.compile(r"(?:pages?|of|쪽|페이지|면|[\W_])*", re.I)


def page_shell(text: str) -> bool:
    """Whether a header or footer line whose page numbers were dropped holds only their wording ("Page of",
    "- -", "/"), which says nothing without them."""
    return _PAGE_SHELL.fullmatch(text) is not None


def symbol(font: str, code: int) -> str | None:
    """The character a symbol font's code shows: Symbol through the Adobe Symbol table, Wingdings through WINGDINGS,
    Wingdings 2's check marks through WINGDINGS2, a code in their private-use form (U+F020-U+F0FF) the same; a font
    whose letters are pictures (_PICTURE_FONTS) has no other character; any other font's code as itself when it is
    not a private-use one. None when nothing is known."""
    face = (font or "").strip().lower()
    low = code - 0xF000 if 0xF020 <= code <= 0xF0FF else code
    if face == "symbol":
        return SYMBOL.get(low)
    if face == "wingdings":
        return WINGDINGS.get(low)
    if face == "wingdings 2":
        return WINGDINGS2.get(low)
    if face in _PICTURE_FONTS:
        return None
    if 0 < code < 0x110000 and not 0xE000 <= code <= 0xF8FF and not 0xD800 <= code <= 0xDFFF:
        return chr(code)
    return None


MAX_FORM, MAX_ENTRIES = 2**16, 256  # bytes read of a form field's data, and entries kept of a drop-down
FORM_DEFAULT = 25  # a form field's result when it shows its default


def form_state(kind: int, result: int, default: int, entries) -> str:
    """What a legacy form field shows beside its result: ☒ or ☐ for a check box (kind 1), a drop-down's (kind 2)
    chosen entry, "" for a text field; a result of FORM_DEFAULT stands for the default."""
    pick = default if result == FORM_DEFAULT else result
    if kind == 1:
        return "☒" if pick else "☐"
    return entries[pick] if kind == 2 and 0 <= pick < len(entries) else ""


def form_data(ff: bytes) -> str:
    """form_state of a .doc form field's FFData (MS-DOC 2.9.78): its kind and result in the bits after the version,
    the default after the name, a drop-down's entries in the string table that ends it. "" for data cut short."""
    try:
        version, bits = struct.unpack_from("<IH", ff, 0)
        if version != 0xFFFFFFFF:
            return ""
        kind, at = bits & 3, 10

        def xstz(at):
            return at + 4 + 2 * struct.unpack_from("<H", ff, at)[0]
        at = xstz(at)
        default = 0
        if kind == 0:
            at = xstz(at)
        else:
            default = struct.unpack_from("<H", ff, at)[0]
            at += 2
        for _ in range(5):
            at = xstz(at)
        entries = []
        if kind == 2:
            mark, count = struct.unpack_from("<HH", ff, at)
            at += 6
            for _ in range(min(count, MAX_ENTRIES) if mark == 0xFFFF else 0):
                n = struct.unpack_from("<H", ff, at)[0]
                if at + 2 + 2 * n > len(ff):
                    return ""
                entries.append(ff[at + 2:at + 2 + 2 * n].decode("utf-16-le", "replace")[:MAX_NAME])
                at += 2 + 2 * n
        return form_state(kind, bits >> 2 & 0x1F, default, entries)
    except struct.error:
        return ""


def when(value: str) -> str:
    """An ISO 8601 date and time as `YYYY-MM-DD HH:MM` (the date alone when it has no time), else as it is."""
    m = _DATE.match(value.strip())
    return (m.group(1) + (" " + m.group(2) if m.group(2) else "")) if m else value


def properties(pairs) -> list:
    """The (label, value) pairs of a document's properties line (markdown renders it): values squashed to one line
    and cut to MAX_PROPERTY, empty ones, repeats of a label, a last author who is the author and the properties an
    application stamps on every file (_APP_PROPERTY: build numbers, sensitivity labels, content type ids) left out; the
    labels of _ORDER first in that order, then the others (custom properties) as they came; at most MAX_PROPERTIES."""
    out, seen = [], {}
    for label, value in pairs:
        value = " ".join(str(value or "").split())[:MAX_PROPERTY]
        label = " ".join(str(label or "").split())[:MAX_NAME]
        if value and label and label not in seen and not _APP_PROPERTY.match(label):
            seen[label] = value
            out.append((label, value))
    if seen.get("last author") == seen.get("author"):
        out = [p for p in out if p[0] != "last author"]
    out.sort(key=lambda p: _ORDER.index(p[0]) if p[0] in _ORDER else len(_ORDER))
    return out[:MAX_PROPERTIES]
