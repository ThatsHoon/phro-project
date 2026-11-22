"""pdf annotations, AcroForm field values, the outline and the XFA and attachment flags.

Port of dochan 1.21.0 pdf/annotations.py (MIT) and its outline and link walk in pdf/reader.py.
Differences from dochan: links keep only the four schemes of SCHEMES and are boxes with their
URL, not paragraphs; comments are `Comment` records with replies placed after their parent
(`order_comments`); field values come from the AcroForm /Fields tree, not from the widgets
alone, and go through `field_entry`; the outline is a flat list of (depth, title, page) and a
named destination is resolved through the catalog's /Dests and the /Names /Dests tree. Every
cap that drops content is counted: `annotation` and `field` on the Counter a function returns or
is given, `outline` on `pdf.cuts`. A comment whose /F has bit 2 (Hidden) or bit 6 (NoView) is
hidden, and so is a field all of whose widgets have one of them.

`field_entry` and `order_comments` take no PDFFile: the pdfium front end calls them too.
"""
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..formats.pdf_layout import Comment
from .objects import PDFName, PDFRef, PDFStream
from .structure import MAX_PAGES, PDFFile

SCHEMES = ("http", "https", "mailto", "ftp")
PDF_ANNOTS = 256            # annotations read from a page
ANNOT_CHARS = 65_536        # characters of one comment
FIELD_CHARS = 2_000         # characters of one field value
FIELD_DEPTH = 32            # levels of the field tree
FIELD_NODES = 20_000        # field nodes read in all
FIELD_KIDS = 10_000         # /Kids entries read from one field (and /Fields entries read)
FIELD_KID_TOTAL = 200_000   # /Kids entries read in all
FIELD_SCAN =100_000        # page /Annots entries scanned to place fields without /P
OUTLINE_ITEMS = 1_000
OUTLINE_DEPTH = 32
NAME_NODES = 10_000         # name tree nodes one destination lookup (or one tree walk) may visit
ATTACHMENTS = 256           # embedded files listed
_COORD = 1e9
_MARKUP = {"Text", "FreeText", "Highlight", "Underline", "Squiggly", "StrikeOut", "Caret",
           "Stamp", "Ink", "Square", "Circle", "Line", "Polygon", "PolyLine", "FileAttachment",
           "Sound", "Redact"}
_HIDDEN = 2                 # /F bit 2
_NOVIEW = 32                # /F bit 6
UNSEEN = _HIDDEN | _NOVIEW  # an annotation with either bit is not shown by a viewer
_FF_RADIO = 1 << 15         # /Ff bit 16
_FF_PUSH = 1 << 16          # /Ff bit 17
FIELD_OPTIONS = 64          # options of a choice field listed
EMPTY = "(empty)"           # the value of a field with none
_KINDS = {"choice": "choice", "check": "checkbox", "radio": "radio button", "sig": "signature"}

_SPECIAL = "•†‡…—–ƒ⁄‹›−‰„“”‘’‚™ﬁﬂŁŒŠŸŽıłœšž"
_ACCENTS = "˘ˇˆ˙˝˛˚˜"


def text_string(value, limit: Optional[int] = None) -> str:
    """A pdf text string (PDFDocEncoding, or UTF-16BE or UTF-8 behind a byte order mark)."""
    if not isinstance(value, bytes):
        return ""
    if limit is not None:
        value = value[:2 * limit + 2]
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", errors="replace")
    if value.startswith(b"\xef\xbb\xbf"):
        return value[3:].decode("utf-8", errors="replace")
    chars = []
    for byte in value:
        if 0x18 <= byte <= 0x1f:
            chars.append(_ACCENTS[byte - 0x18])
        elif 0x80 <= byte <= 0x9e:
            chars.append(_SPECIAL[byte - 0x80])
        elif byte == 0xa0:
            chars.append("€")
        elif byte in (0x7f, 0x9f, 0xad):
            chars.append("�")
        else:
            chars.append(chr(byte))
    return "".join(chars)


def _key(ref):
    return ref.num if isinstance(ref, PDFRef) else id(ref)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and abs(value) <= _COORD and value == value


def _box(pdf: PDFFile, annot: dict):
    rect = pdf.resolve(annot.get("Rect"))
    if not isinstance(rect, list) or len(rect) != 4:
        return None
    x0, y0, x1, y1 = (pdf.resolve(v) for v in rect)
    if not all(_number(v) for v in (x0, y0, x1, y1)):
        return None
    box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return box if box[2] > box[0] and box[3] > box[1] else None


def _link(pdf: PDFFile, annot: dict):
    action = pdf.resolve(annot.get("A"))
    if not isinstance(action, dict) or str(pdf.resolve(action.get("S"))) != "URI":
        return None
    uri = pdf.resolve(action.get("URI"))
    if not isinstance(uri, bytes):
        return None
    url = uri.decode("latin-1").strip()
    scheme, colon, _rest = url.partition(":")
    if not colon or scheme.lower() not in SCHEMES:
        return None
    box = _box(pdf, annot)
    return (box, url) if box is not None else None


def page_annots(pdf: PDFFile, page: dict) -> Tuple[List[tuple], List[Comment], Counter]:
    """(links [(box, url)], comments, cut) of one page. Links are /Link annotations with a /URI
    action of an allowed scheme; every other action gives nothing."""
    cut: Counter = Counter()
    annots = pdf.resolve(page.get("Annots"))
    if not isinstance(annots, list):
        return [], [], cut
    if len(annots) > PDF_ANNOTS:
        cut["annotation"] += 1
        annots = annots[:PDF_ANNOTS]
    links: List[tuple] = []
    items: List[tuple] = []
    seen = set()
    for ref in annots:
        key = _key(ref)
        if key in seen:
            continue
        seen.add(key)
        annot = pdf.resolve(ref)
        if not isinstance(annot, dict):
            continue
        subtype = str(pdf.resolve(annot.get("Subtype")))
        if subtype == "Link":
            link = _link(pdf, annot)
            if link is not None:
                links.append(link)
        elif subtype in _MARKUP:  # a Popup is the window of its parent, not a comment
            raw = pdf.resolve(annot.get("Contents"))
            text = text_string(raw, ANNOT_CHARS)
            if len(text) > ANNOT_CHARS or (isinstance(raw, bytes) and len(raw) > 2 * ANNOT_CHARS + 2):
                cut["annotation"] += 1
                text = text[:ANNOT_CHARS]
            text = text.strip()
            if not text:
                continue
            author = text_string(pdf.resolve(annot.get("T")), 1_000)[:1_000].strip()
            flags = pdf.resolve(annot.get("F"))
            hidden = _unseen(flags)
            irt = annot.get("IRT")
            items.append((key, irt.num if isinstance(irt, PDFRef) else None,
                          Comment(author, text, hidden)))
    return links, order_comments(items), cut


def _unseen(flags) -> bool:
    return isinstance(flags, int) and not isinstance(flags, bool) and bool(flags & UNSEEN)


def order_comments(items: List[tuple]) -> List[Comment]:
    """`items` is [(key, irt key or None, Comment)] in file order. Each reply comes right after
    its parent; a reply whose parent is not in `items`, or that sits in a reply cycle, keeps its
    file position among the top-level comments."""
    keys = {key for key, _irt, _c in items}
    children: Dict[Any, List[int]] = {}
    for i, (key, irt, _c) in enumerate(items):
        if irt is not None and irt in keys and irt != key:
            children.setdefault(irt, []).append(i)
    out: List[Comment] = []
    done = set()

    def walk(start: int) -> None:
        stack = [start]
        while stack:
            i = stack.pop()
            if i in done:
                continue
            done.add(i)
            out.append(items[i][2])
            stack.extend(reversed(children.get(items[i][0], [])))

    for i, (key, irt, _c) in enumerate(items):
        if irt is None or irt not in keys or irt == key:
            walk(i)
    for i in range(len(items)):  # what is left sits in a reply cycle
        walk(i)
    return out


# -- form fields --

def field_entry(label_tu: Optional[str], name: str, value, kind: str, options=()):
    """(label, value, cut) of one field, or None for a push button. The label is /TU, else the
    last dot-part of the full name, and then the field's kind in brackets, but for a text field;
    a choice's kind lists its options' display texts (at most FIELD_OPTIONS, then "…"). The value
    of a check box is "checked" or "not checked", of a signature "signed" or EMPTY; a choice shows
    the display texts of the export values picked; any other value is its text, EMPTY when blank
    (or Off). Newlines in a value become " / " and a value is cut at FIELD_CHARS (cut True).
    `kind` is text, choice, check, radio, sig or push; a choice value may be a list; `options` are
    (export, display) pairs."""
    if kind == "push":
        return None
    label = (label_tu or "").strip() or (name or "").rsplit(".", 1)[-1].strip() or "field"
    shown = dict(options)
    if kind == "check":
        value = "checked" if isinstance(value, str) and value.strip() not in ("", "Off") else "not checked"
    elif kind == "sig":
        value = "signed" if isinstance(value, str) and value.strip() else EMPTY
    else:
        if isinstance(value, (list, tuple)):
            value = ", ".join(shown.get(v, v) for v in value if isinstance(v, str))
        elif isinstance(value, str):
            value = shown.get(value, value)
        value = value.strip() if isinstance(value, str) else ""
        if not value or (kind == "radio" and value == "Off"):
            value = EMPTY
    if kind != "text":
        desc = _KINDS[kind]
        if kind == "choice" and options:
            names = [" ".join(d.split()) for _e, d in options[:FIELD_OPTIONS]]
            desc += ": " + ", ".join(names + ["…"] * (len(options) > FIELD_OPTIONS))
        label = f"{label} ({desc})"
    value = value.replace("\r\n", " / ").replace("\r", " / ").replace("\n", " / ")
    cut = len(value) > FIELD_CHARS
    return label, value[:FIELD_CHARS], cut


def page_index(pdf: PDFFile) -> Dict[PDFRef, int]:
    """{page reference: 0-based page index}, in the order of pdf.pages()."""
    ids = {id(page): i for i, (page, _inherited) in enumerate(pdf.pages())}
    out: Dict[PDFRef, int] = {}
    stack = [pdf.catalog.get("Pages")]
    seen = {_key(stack[0])}
    steps = 0  # kids examined: shared /Kids arrays cost each time they are read
    while stack:
        ref = stack.pop()
        node = pdf.resolve(ref)
        if not isinstance(node, dict):
            continue
        if id(node) in ids:
            if isinstance(ref, PDFRef):
                out[ref] = ids[id(node)]
            continue
        kids = pdf.resolve(node.get("Kids"))
        if isinstance(kids, list):
            fresh = []
            for kid in kids[:MAX_PAGES]:
                steps += 1
                if steps > 4 * MAX_PAGES:
                    pdf._cut("pages", "WARN: the page tree is larger than the cap; pages beyond it have no reference")
                    return out
                if _key(kid) not in seen:
                    seen.add(_key(kid))
                    fresh.append(kid)
            stack.extend(reversed(fresh))
    return out


def _field_value(pdf: PDFFile, value):
    value = pdf.resolve(value)
    if isinstance(value, PDFName):
        return str(value)
    if isinstance(value, bytes):
        return text_string(value, FIELD_CHARS + 1)
    if isinstance(value, PDFStream):
        return text_string(pdf.decode_stream_bytes(value)[:4 * FIELD_CHARS], FIELD_CHARS + 1)
    if isinstance(value, list):
        return [v for v in (_field_value(pdf, x) for x in value[:FIELD_NODES]) if isinstance(v, str)]
    return None


def _field_kind(ft: str, flags: int) -> Optional[str]:
    if ft == "Tx":
        return "text"
    if ft == "Ch":
        return "choice"
    if ft == "Sig":
        return "sig"
    if ft == "Btn":
        return "push" if flags & _FF_PUSH else "radio" if flags & _FF_RADIO else "check"
    return None


def _options(pdf: PDFFile, opt) -> List[Tuple[str, str]]:
    """(export, display) of a choice field's /Opt: a text string is both, a pair is [export,
    display]; at most FIELD_OPTIONS + 1 are read."""
    out = []
    opt = pdf.resolve(opt)
    for item in opt[:FIELD_OPTIONS + 1] if isinstance(opt, list) else ():
        item = pdf.resolve(item)
        if isinstance(item, list) and len(item) == 2:
            export, display = (text_string(pdf.resolve(v), 1_000) for v in item)
        else:
            export = display = text_string(item, 1_000) if isinstance(item, bytes) else ""
        if display.strip():
            out.append((export, display))
    return out


def fields(pdf: PDFFile, cut: Counter) -> Dict[int, List[tuple]]:
    """{0-based page: [(label, value, hidden, box)]} of the fields in /AcroForm /Fields, push
    buttons aside, through `field_entry`. `box` is the Rect of the field's widget on that page,
    or None. A field is hidden when each of its widgets (its kids without /T, else the field
    itself) has /F bit 2 or 6."""
    form = pdf.resolve(pdf.catalog.get("AcroForm"))
    roots = pdf.resolve(form.get("Fields")) if isinstance(form, dict) else None
    if not isinstance(roots, list):
        return {}
    refs = None  # page index by reference, and the page of each widget listed in /Annots: lazy
    scan: Dict[int, int] = {}

    def page_of(widgets):
        nonlocal refs
        if refs is None:
            refs = page_index(pdf)
            budget = FIELD_SCAN
            for number, (page, _inherited) in enumerate(pdf.pages()):
                annots = pdf.resolve(page.get("Annots"))
                if not isinstance(annots, list):
                    continue
                if len(annots) > budget:
                    cut["field"] += 1
                    annots = annots[:budget]
                budget -= len(annots)
                for ref in annots:
                    if isinstance(ref, PDFRef):
                        scan.setdefault(ref.num, number)
                if budget <= 0:
                    break
        for ref, widget in widgets:
            page = pdf.resolve(widget.get("P"))
            index = refs.get(widget.get("P")) if isinstance(widget.get("P"), PDFRef) else None
            if index is None and isinstance(page, dict):
                index = next((i for r, i in refs.items() if pdf.resolve(r) is page), None)
            if index is None and isinstance(ref, PDFRef):
                index = scan.get(ref.num)
            if index is not None:
                return index, _box(pdf, widget)
        return None, None

    out: Dict[int, List[tuple]] = {}
    if len(roots) > FIELD_KIDS:
        cut["field"] += 1
        roots = roots[:FIELD_KIDS]
    seen = set()  # a node is queued once, so the stack never holds a node twice
    queued = []
    for ref in roots:
        if _key(ref) not in seen:
            seen.add(_key(ref))
            queued.append(ref)
    stack = [(ref, "", {}, 1) for ref in reversed(queued)]
    kid_budget = FIELD_KID_TOTAL
    nodes = 0
    while stack:
        ref, parent_name, inherited, depth = stack.pop()
        node = pdf.resolve(ref)
        if not isinstance(node, dict):
            continue
        nodes += 1
        if nodes > FIELD_NODES:
            cut["field"] += 1
            break
        if depth > FIELD_DEPTH:
            cut["field"] += 1
            continue
        partial = text_string(pdf.resolve(node.get("T")), 1_000)
        name = f"{parent_name}.{partial}" if parent_name and partial else partial or parent_name
        state = dict(inherited)
        for attr in ("FT", "V", "Ff", "Opt"):
            if attr in node:
                state[attr] = node[attr]
        kids = pdf.resolve(node.get("Kids"))
        kid_fields, widgets = [], []
        if isinstance(kids, list):
            if len(kids) > FIELD_KIDS or len(kids) > kid_budget:
                cut["field"] += 1
                kids = kids[:min(FIELD_KIDS, kid_budget)]
            kid_budget -= len(kids)
            for kid in kids:
                kid_node = pdf.resolve(kid)
                if not isinstance(kid_node, dict):
                    continue
                if "T" in kid_node:
                    if _key(kid) not in seen:  # a node shared by many parents is queued once
                        seen.add(_key(kid))
                        kid_fields.append(kid)
                else:
                    widgets.append((kid, kid_node))
        if kid_fields:
            stack.extend((kid, name, state, depth + 1) for kid in reversed(kid_fields))
            continue
        flags = pdf.resolve(state.get("Ff"))
        kind = _field_kind(str(pdf.resolve(state.get("FT"))),
                           flags if isinstance(flags, int) and not isinstance(flags, bool) else 0)
        if kind is None:
            continue
        tu = text_string(pdf.resolve(node.get("TU")), 1_000).strip() or None
        if kind == "sig":
            value = "signed" if pdf.resolve(state.get("V")) is not None else None
        else:
            value = _field_value(pdf, state.get("V"))
        entry = field_entry(tu, name, value, kind, _options(pdf, state.get("Opt")) if kind == "choice" else ())
        if entry is None:
            continue
        label, value, was_cut = entry
        if was_cut:
            cut["field"] += 1
        page, box = page_of([(ref, node)] + widgets)
        hidden = all(_unseen(pdf.resolve(widget.get("F"))) for _ref, widget in widgets or [(ref, node)])
        # A field on no page is still document data: it goes to the first page.
        out.setdefault(page or 0, []).append((label, value, hidden, box if page is not None else None))
    return out


# -- outline --

class _Destinations:
    """Named destinations: the catalog's /Dests dictionary and its /Names /Dests name tree."""

    def __init__(self, pdf: PDFFile):
        self.pdf = pdf
        catalog = pdf.catalog
        old = pdf.resolve(catalog.get("Dests"))
        self.old = old if isinstance(old, dict) else {}
        names = pdf.resolve(catalog.get("Names"))
        self.tree = names.get("Dests") if isinstance(names, dict) else None
        self.leaves: Dict[int, Dict[bytes, Any]] = {}

    def lookup(self, name) -> Any:
        pdf = self.pdf
        if isinstance(name, bytes):
            found = self.old.get(text_string(name))
            return found if found is not None else self._tree(name)
        found = self.old.get(str(name))
        return found if found is not None else self._tree(str(name).encode("utf-8"))

    def _tree(self, key: bytes) -> Any:
        pdf = self.pdf
        stack = [self.tree]
        seen = set()
        steps = 0
        while stack:
            ref = stack.pop()
            if _key(ref) in seen:
                continue
            seen.add(_key(ref))
            steps += 1
            if steps > NAME_NODES:
                break
            node = pdf.resolve(ref)
            if not isinstance(node, dict):
                continue
            limits = pdf.resolve(node.get("Limits"))
            if isinstance(limits, list) and len(limits) == 2:
                low, high = pdf.resolve(limits[0]), pdf.resolve(limits[1])
                if isinstance(low, bytes) and isinstance(high, bytes) and not low <= key <= high:
                    continue
            kids = pdf.resolve(node.get("Kids"))
            if isinstance(kids, list):
                stack.extend(reversed(kids))
            pairs = pdf.resolve(node.get("Names"))
            if isinstance(pairs, list):
                leaf = self.leaves.get(id(node))
                if leaf is None:
                    leaf = self.leaves[id(node)] = {}
                    for i in range(0, len(pairs) - 1, 2):
                        k = pdf.resolve(pairs[i])
                        if isinstance(k, bytes):
                            leaf.setdefault(k, pairs[i + 1])
                if key in leaf:
                    return leaf[key]
        return None


def _dest(pdf: PDFFile, item: dict, pages: Dict[PDFRef, int], names: _Destinations):
    """(0-based page or None, top or None) of an outline item's destination: its /Dest, else its
    GoTo action's /D, a name looked up in `names`. The top is the y of an /XYZ, /FitH or /FitBH
    view (ISO 32000-1 12.3.2.2), in page space."""
    dest = pdf.resolve(item.get("Dest"))
    if dest is None:
        action = pdf.resolve(item.get("A"))
        if isinstance(action, dict) and str(pdf.resolve(action.get("S"))) == "GoTo":
            dest = pdf.resolve(action.get("D"))
    if isinstance(dest, (bytes, PDFName)):
        dest = pdf.resolve(names.lookup(dest))
    if isinstance(dest, dict):
        dest = pdf.resolve(dest.get("D"))
    if not (isinstance(dest, list) and dest and isinstance(dest[0], PDFRef)):
        return None, None
    view = str(pdf.resolve(dest[1])) if len(dest) > 1 else ""
    at = {"XYZ": 3, "FitH": 2, "FitBH": 2}.get(view)
    top = pdf.resolve(dest[at]) if at is not None and len(dest) > at else None
    return pages.get(dest[0]), float(top) if _number(top) else None


def outline(pdf: PDFFile, page_index: Dict[PDFRef, int]) -> List[tuple]:
    """[(depth from 1, title, 0-based page, top or None)] of the outline in document order, `top`
    the y its destination shows at the top (`_dest`). An item whose destination is no page of
    this file is left out; both that and a cap are counted on pdf.cuts["outline"]."""
    root = pdf.resolve(pdf.catalog.get("Outlines"))
    if not isinstance(root, dict):
        return []
    names = _Destinations(pdf)
    out: List[Tuple[int, str, int]] = []
    stack = [(root.get("First"), 1)]
    seen = set()
    while stack:
        ref, depth = stack.pop()
        if ref is None or _key(ref) in seen:
            continue
        seen.add(_key(ref))
        item = pdf.resolve(ref)
        if not isinstance(item, dict):
            continue
        title = text_string(pdf.resolve(item.get("Title")), 1_000).strip()
        if title:
            page, top = _dest(pdf, item, page_index, names)
            if page is None:
                pdf._cut("outline", "WARN: an outline item names no page of this file; it is left out")
            elif len(out) >= OUTLINE_ITEMS:
                pdf._cut("outline", f"WARN: the outline passed {OUTLINE_ITEMS} items; the rest are dropped")
                break
            else:
                out.append((depth, title, page, top))
        stack.append((item.get("Next"), depth))
        if item.get("First") is not None:
            if depth + 1 > OUTLINE_DEPTH:
                pdf._cut("outline", f"WARN: the outline is deeper than {OUTLINE_DEPTH}; the levels below are dropped")
            else:
                stack.append((item.get("First"), depth + 1))
    return out


# -- flags --

def has_xfa(pdf: PDFFile) -> bool:
    """True when /AcroForm has an /XFA entry."""
    form = pdf.resolve(pdf.catalog.get("AcroForm"))
    return isinstance(form, dict) and "XFA" in form


def _name_values(pdf: PDFFile, root) -> List[Any]:
    """The values of a name tree (ISO 32000-1 7.9.6) in key order, at most ATTACHMENTS of them,
    visiting at most NAME_NODES nodes."""
    out: List[Any] = []
    stack, seen = [root], set()
    while stack and len(seen) < NAME_NODES and len(out) < ATTACHMENTS:
        ref = stack.pop()
        if _key(ref) in seen:
            continue
        seen.add(_key(ref))
        node = pdf.resolve(ref)
        if not isinstance(node, dict):
            continue
        pairs = pdf.resolve(node.get("Names"))
        if isinstance(pairs, list):
            out.extend(pairs[1:2 * ATTACHMENTS:2])
        kids = pdf.resolve(node.get("Kids"))
        if isinstance(kids, list):
            stack.extend(reversed(kids[:NAME_NODES]))
    return out[:ATTACHMENTS]


def _attached(pdf: PDFFile, spec) -> Optional[Tuple[str, Optional[int], str]]:
    """(name, size or None, media type or "") of a file specification with an embedded file."""
    spec = pdf.resolve(spec)
    if not isinstance(spec, dict):
        return None
    name = next((text_string(pdf.resolve(spec.get(k)), 1_000).strip() for k in ("UF", "F")
                 if isinstance(pdf.resolve(spec.get(k)), bytes)), "") or "attached file"
    ef = pdf.resolve(spec.get("EF"))
    stream = pdf.resolve(ef.get("UF") or ef.get("F")) if isinstance(ef, dict) else None
    if not isinstance(stream, PDFStream):
        return None
    d = stream.dictionary
    params = pdf.resolve(d.get("Params"))
    size = pdf.resolve(params.get("Size")) if isinstance(params, dict) else None
    kind = pdf.resolve(d.get("Subtype"))
    return (" ".join(name.split()), size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            str(kind) if isinstance(kind, PDFName) else "")


def attachments(pdf: PDFFile) -> List[tuple]:
    """[(name, size or None, media type, 0-based page or None, box or None)] of the embedded
    files: those of /Names /EmbeddedFiles (no page), then those of /FileAttachment annotations
    (their page and Rect), at most ATTACHMENTS in all."""
    out: List[tuple] = []
    names = pdf.resolve(pdf.catalog.get("Names"))
    if isinstance(names, dict) and names.get("EmbeddedFiles") is not None:
        for spec in _name_values(pdf, names.get("EmbeddedFiles")):
            got = _attached(pdf, spec)
            if got is not None:
                out.append((*got, None, None))
    for index, (page, _inherited) in enumerate(pdf.pages()):
        annots = pdf.resolve(page.get("Annots"))
        if not isinstance(annots, list):
            continue
        for ref in annots[:PDF_ANNOTS]:
            annot = pdf.resolve(ref)
            if len(out) >= ATTACHMENTS:
                return out
            if isinstance(annot, dict) and str(pdf.resolve(annot.get("Subtype"))) == "FileAttachment":
                got = _attached(pdf, annot.get("FS"))
                if got is not None:
                    out.append((*got, index, _box(pdf, annot)))
    return out


def encrypted_payload(pdf: PDFFile) -> Optional[str]:
    """The /EP /Subtype of an unencrypted wrapper document (ISO 32000-2 7.6.7): a file of the
    catalog's /AF or of /Names /EmbeddedFiles whose /AFRelationship is /EncryptedPayload or
    that has an /EP dictionary. None for any other file."""
    names = pdf.resolve(pdf.catalog.get("Names"))
    specs = list(pdf.resolve(pdf.catalog.get("AF")) or ()) if isinstance(pdf.resolve(pdf.catalog.get("AF")), list) else []
    if isinstance(names, dict) and names.get("EmbeddedFiles") is not None:
        specs += _name_values(pdf, names.get("EmbeddedFiles"))
    for spec in specs[:2 * ATTACHMENTS]:
        spec = pdf.resolve(spec)
        if not isinstance(spec, dict):
            continue
        ep = pdf.resolve(spec.get("EP"))
        if isinstance(ep, dict) or str(pdf.resolve(spec.get("AFRelationship"))) == "EncryptedPayload":
            kind = pdf.resolve(ep.get("Subtype")) if isinstance(ep, dict) else None
            return str(kind) if isinstance(kind, PDFName) else "unknown"
    return None


def page_labels(pdf: PDFFile, count: int) -> Dict[int, str]:
    """{0-based page: label} from the catalog's /PageLabels number tree (ISO 32000-1 12.4.2): a
    range's /P prefix, then its /St start (1 by default) plus the page's place in the range in
    its /S style: D decimal, R and r roman, A and a letters (A to Z, then AA to ZZ, ...). At most
    NAME_NODES tree nodes and `count` pages."""
    root = pdf.resolve(pdf.catalog.get("PageLabels"))
    if not isinstance(root, dict):
        return {}
    starts: List[Tuple[int, dict]] = []
    stack, seen = [root], set()
    while stack and len(seen) < NAME_NODES:
        ref = stack.pop()
        if _key(ref) in seen:
            continue
        seen.add(_key(ref))
        node = pdf.resolve(ref)
        if not isinstance(node, dict):
            continue
        nums = pdf.resolve(node.get("Nums"))
        if isinstance(nums, list):
            for i in range(0, min(len(nums), 2 * MAX_PAGES) - 1, 2):
                key, spec = pdf.resolve(nums[i]), pdf.resolve(nums[i + 1])
                if isinstance(key, int) and not isinstance(key, bool) and 0 <= key < count and isinstance(spec, dict):
                    starts.append((key, spec))
        kids = pdf.resolve(node.get("Kids"))
        if isinstance(kids, list):
            stack.extend(reversed(kids[:NAME_NODES]))
    starts.sort(key=lambda t: t[0])
    out: Dict[int, str] = {}
    for k, (first, spec) in enumerate(starts):
        last = starts[k + 1][0] if k + 1 < len(starts) else count
        style = str(pdf.resolve(spec.get("S")) or "")
        prefix = pdf.resolve(spec.get("P"))
        prefix = text_string(prefix, 1_000) if isinstance(prefix, bytes) else ""
        start = pdf.resolve(spec.get("St"))
        start = start if isinstance(start, int) and not isinstance(start, bool) and start >= 1 else 1
        for page in range(first, last):
            out[page] = " ".join((prefix + _label_number(start + page - first, style)).split())
    return out


def _label_number(n: int, style: str) -> str:
    if style == "D":
        return str(n)
    if style in ("R", "r"):
        if n >= 4000:
            return str(n)
        digits = []
        for value, mark in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            count, n = divmod(n, value)
            digits.append(mark * count)
        roman = "".join(digits)
        return roman if style == "R" else roman.lower()
    if style in ("A", "a"):
        letter = chr(ord("A") + (n - 1) % 26) * ((n - 1) // 26 + 1) if n <= 26 * 64 else str(n)
        return letter if style == "A" else letter.lower()
    return ""
