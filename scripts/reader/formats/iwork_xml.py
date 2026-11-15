"""iWork '09 XML (Pages `index.xml`, Keynote `index.apxl`) to the document model.

Ported from docling (MIT) `docling/backend/iwork/legacy.py` (the `sf` vocabulary: `legacy_runs`, `legacy_styles`,
`legacy_list_styles`, `legacy_inherited_lists`, `legacy_list_label`, `legacy_formatting`, `legacy_picture`,
`sf_attr`), `docling/backend/iwork/pages_xml.py` (`read_content`, `iter_body_elements`, `legacy_furniture`,
`legacy_comments`) and `docling/backend/iwork/keynote_xml.py` (`iter_slides`, `read_slide`, `slide_drawables`,
`slide_placeholders`, `iter_drawables`, `drawable_blocks`, `sticky_note_text`, `slide_notes`), v2.150.0
(1ae470a1f4580881ed73853f64e0bf178f623c5e).

`iwork._xml` hands over the parsed root (`zipx.parse_xml_ns`: a DOCTYPE or entity is refused there). The root
picks the app (`sl:document` Pages, `key:presentation` Keynote, `ls:document` Numbers, Task 16); an unknown root is
read by the extension's app, and a root no walk reads gives a Doc with no blocks (the preview fallback).

The shared `sf` vocabulary: `sf:p` paragraphs of text, `sf:span` (character style), `sf:link` (`href`, http, https
and mailto kept), `sf:br` and the other breaks (a line break inside a paragraph; none at its edges), `sf:tab`;
`sf:paragraphstyle`, `sf:characterstyle` and `sf:liststyle` found by `sf:ident` or `sfa:ID` (the first wins) and
their parents by `sf:parent-ident`, at most `iwork_text.STYLE_CHAIN` parents with a visited set, each answer cached
by style; headings by the paragraph style's name (Pages only, `iwork_text.level`; Keynote names theme styles in the
theme's language, so its placeholders say what a paragraph is, as in docling); bold from a character style's
`sf:bold` property; lists from `sf:list-style` on the paragraph, else the paragraph style chain's `sf:listStyle`
(`sf:liststyle-ref` or an inline style), the rung `sf:list-level` (0 is body text) picking the ladder's
`sf:list-label-typeinfo` (`none` plain, a numbering `sf:text-label` type "1.", any other "-"); `sf:media` and
`sf:image` give `Image` from the first `sf:data` (`sf:path` or `path`) whose member is in the package, an `-ref`
inside searched REF_DEPTH levels into its target, once by id; `sf:ghost-text` and `sf:ghost-text-ref` are the
template's placeholder text
and are dropped uncounted. A table (`sf:tabular-info`, `sf:tabular-model`) is not read here: it counts `table`
(Task 16 reads '09 tables), its cells never reach the body, and a footnote its cells mark keeps its place in the
pairing below, kept as an unmarked note.

Pages (`sl:document`): the body is the document outside `sf:header`, `sf:footer`, `sf:footnotes`, `sf:annotations`
and `sl:section-prototypes` (the templates of new sections, like ghost text), in document order, then the
floating drawables of `sl:drawables` in their container's order (Defaults "Pages styles"; one that fails counts
`shape`), then the attachments no `sf:attachment-ref` placed (one that fails counts `object`). An
`sf:attachment-ref` places its attachment after its paragraph, once; any other
`-ref` (a text box chain's shape ref, say) is never followed, so nothing is read twice. Footnotes: the paragraphs
of `sf:footnotes` split at each `sf:footnote-mark` and after each `sf:footnotebr`, paired in order with the body's
`sf:footnote` marks (`[^n]`); a mark past the last footnote counts `note`, and a footnote no mark names is kept as
an unmarked note. Comments: an `sf:annotation` names the `sf:annotation-field` it comments by `sf:target`; its
paragraphs become a "Comment: …" note (`word_fields.comment_note`; '09 records no author), marked where the field
ends and read once a document, so a field repeated gives the same mark; one no field names is kept unmarked.
Headers and footers (every page variant; the renderer drops repeats) are one line a paragraph; a note mark there,
in a footnote or in a comment counts `note` and adds no note. A body that cannot be read counts `document` and the
rest of the page is still read.

Keynote (`key:presentation`): each `key:slide` of `key:slide-list`, under `ctx.guard("slide")` (a failed slide
counts `slide` and keeps its number), is `Section("slide", title, number=n)`: the title placeholder gives the title
(MAX_TITLE characters; a picture in it goes to the body, and the notes its marks name to the comments), then the
body placeholder, then the drawables of `key:page` (the master's layer proxy is not followed, nor the placeholder
refs), then `key:notes` as `Section.notes`; sticky notes, on the page or in `key:sticky-notes`, are "Comment: …"
lines in `Section.comments`. The theme's master slides are never read.

Every walk is iterative (an explicit stack, so a deep tree never reaches Python's recursion limit) and enters each
element at most once, so the work is bounded by the element count, which `iwork.IWORK_XML` bounds. Pictures are
charged against `limits.IMAGE_BYTES` per document, each placement; one missing or past it counts `image`.

Changes from docling: footnotes are paired with their marks rather than listed as furniture; comments are marked
where they apply; floating drawables follow the body; templates are left out; a character style that does not set
bold keeps the bold in force; list ladders follow their `-ref`s.
"""
from itertools import groupby

from .. import limits
from ..limits import ReadError
from ..model import Bold, Break, Doc, Heading, Image, Link, ListItem, NoteRef, Para, Section, Text
from . import iwork_iwa
from .iwork_text import LINK_SCHEMES, OBJECT_REPLACEMENT, STYLE_CHAIN, level, plain
from .word_fields import MAX_URL, comment_note

SF = "{http://developer.apple.com/namespaces/sf}"
SFA = "{http://developer.apple.com/namespaces/sfa}"
SL = "{http://developer.apple.com/namespaces/sl}"
KEY = "{http://developer.apple.com/namespaces/keynote2}"
LS = "{http://developer.apple.com/namespaces/ls}"
ROOTS = {SL + "document": "pages", KEY + "presentation": "key", LS + "document": "numbers"}

P, SPAN, LINK, TAB = SF + "p", SF + "span", SF + "link", SF + "tab"
BREAKS = frozenset(SF + t for t in ("br", "lnbr", "pgbr", "sectbr", "layoutbr", "contbr", "colbr"))
GHOST = frozenset({SF + "ghost-text", SF + "ghost-text-ref"})
FOOTNOTE, FOOTNOTE_MARK = SF + "footnote", SF + "footnote-mark"
FOOTNOTEBR, FOOTNOTES = SF + "footnotebr", SF + "footnotes"
ANNOTATION, ANNOTATIONS, FIELD = SF + "annotation", SF + "annotations", SF + "annotation-field"
HEADER, FOOTER = SF + "header", SF + "footer"
ATTACHMENT, ATTACHMENTS, ATTACHMENT_REF = SF + "attachment", SF + "attachments", SF + "attachment-ref"
MEDIA = frozenset({SF + "media", SF + "image"})
DATA = SF + "data"
TABLES = frozenset({SF + "tabular-info", SF + "tabular-model"})
STICKY = frozenset({SF + "sticky-note", KEY + "sticky-note"})
# drawables written inside a paragraph are placed after it, as an anchored attachment is
INLINE_BLOCKS = MEDIA | TABLES | STICKY | {SF + "drawable-shape", SF + "shape", SF + "group"}
STYLESHEETS = frozenset({SL + "stylesheet", KEY + "stylesheet", SF + "stylesheet", SF + "styles", SF + "anon-styles"})
PROTOTYPES, FLOATING = SL + "section-prototypes", SL + "drawables"
PAGES_PRUNE = STYLESHEETS | {HEADER, FOOTER, FOOTNOTES, ANNOTATIONS, ANNOTATION, PROTOTYPES}
SLIDE_LIST, SLIDE, PAGE, NOTES = KEY + "slide-list", KEY + "slide", KEY + "page", KEY + "notes"
TITLE_PH, BODY_PH, STICKIES = KEY + "title-placeholder", KEY + "body-placeholder", KEY + "sticky-notes"
KEY_PRUNE = STYLESHEETS | {SF + "proxy-master-layer", ANNOTATIONS, ANNOTATION}

PARA_STYLE, CHAR_STYLE, LIST_STYLE = SF + "paragraphstyle", SF + "characterstyle", SF + "liststyle"
ID, IDREF, IDENT, PARENT = SFA + "ID", SFA + "IDREF", SF + "ident", SF + "parent-ident"
NAME, STYLE, TARGET, TYPE, NUMBER = SF + "name", SF + "style", SF + "target", SF + "type", SFA + "number"
LIST_ATTR, LEVEL_ATTR = SF + "list-style", SF + "list-level"
PROPERTY_MAP, BOLD, LIST_PROPERTY, LIST_REF = SF + "property-map", SF + "bold", SF + "listStyle", SF + "liststyle-ref"
LABEL_TYPES, TYPEINFO, TEXT_LABEL = SF + "listLabelTypes", SF + "list-label-typeinfo", SF + "text-label"
BULLET_LABELS = frozenset({"bullet", "image", "string", "text"})  # text-label types that draw a mark (docling)
MAX_RUNG = 99  # list rungs read; a real ladder has nine
REF_DEPTH = 3  # levels searched for picture data inside a ref's target

_TEXT, _NOTE, _BREAK = 0, 1, 2  # paragraph segment kinds
_OPEN, _TAIL, _CLOSE = 0, 1, 2  # paragraph walk steps


def _ref(el) -> bool:
    return isinstance(el.tag, str) and el.tag.endswith("-ref")


def _attr(el, name: str) -> str | None:
    """An attribute iWork writes qualified or not (docling's `sf_attr`)."""
    return el.get(SF + name) or el.get(name)


def _member(path: str | None) -> str | None:
    """A data path as a package member name, None when it would leave the package."""
    if not path:
        return None
    p = path.replace("\\", "/")
    if p.startswith("/") or ".." in p.split("/"):
        return None
    return p


def _number(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


def _inlines(segs: list) -> list:
    """Segments to inlines: outer whitespace and breaks trimmed, one Link per url, one Bold per bold stretch, adjacent
    text joined once. [] when nothing is left."""
    i, j = 0, len(segs)
    while i < j:
        kind, v, bold, url = segs[i]
        if kind == _BREAK or (kind == _TEXT and not v.strip()):
            i += 1
            continue
        if kind == _TEXT:
            segs[i] = (kind, v.lstrip(), bold, url)
        break
    while j > i:
        kind, v, bold, url = segs[j - 1]
        if kind == _BREAK or (kind == _TEXT and not v.strip()):
            j -= 1
            continue
        if kind == _TEXT:
            segs[j - 1] = (kind, v.rstrip(), bold, url)
        break
    out = []
    for url, g in groupby(segs[i:j], key=lambda s: s[3]):
        styled = []
        for bold, g2 in groupby(g, key=lambda s: s[2]):
            items, texts = [], []
            for kind, v, _, _ in g2:
                if kind == _TEXT:
                    texts.append(v)
                    continue
                if texts:
                    items.append(Text("".join(texts)))
                    texts = []
                items.append(NoteRef(v) if kind == _NOTE else Break())
            if texts:
                items.append(Text("".join(texts)))
            styled.extend([Bold(items)] if bold else items)
        out.extend([Link(url, styled)] if url else styled)
    return out


class Reader:
    """One '09 document: the indexes built in one pass over the tree, then the app's walk (`pages` or `keynote`)."""

    def __init__(self, root, pkg, ctx, app: str):
        self.root, self.pkg, self.ctx, self.app = root, pkg, ctx, app
        self.doc = Doc()
        self.notes = self.doc.notes
        self.headings = app == "pages"
        self.prune = PAGES_PRUNE if app == "pages" else KEY_PRUNE
        self.ids: dict[str, object] = {}
        self.styles = {PARA_STYLE: {}, CHAR_STYLE: {}, LIST_STYLE: {}}
        self.footnotes, self.annotations, self.headers, self.footers, self.attachments = [], {}, [], [], []
        self.loose: list = []                   # annotations in document order
        self._cache: dict = {}                  # (lookup, key) -> answer
        self._ref_paths: dict[str, str | None] = {}
        self._fn_blocks: list[list] = []        # the footnotes, in order
        self._fn_used = 0                       # body marks paired so far
        self._cm: dict[str, str | None] = {}    # annotation field id -> note id
        self._used: set[int] = set()            # annotations read
        self._moved: set[str] = set()           # note ids a slide title moved into its comments
        self.placed: set[int] = set()           # attachments an attachment-ref placed
        self.entered: set[int] = set()          # attachments walked
        self._noted = 0                         # > 0 while a header, footer, footnote, comment or slide note is read
        self._next = {"fn": 0, "cm": 0}
        self.defer: list | None = None          # Pages: the floating drawables, read after the body
        self.sink: list | None = None           # Keynote: the current slide's comments
        self.image_bytes = 0
        self._index()

    # ---- the one pass
    def _index(self):
        """Element ids, styles (anywhere), and in the body scope (outside templates, tables and ghost text) the
        footnotes, comments, headers, footers and attachments."""
        stack = [(self.root, True)]
        while stack:
            el, body = stack.pop()
            tag = el.tag
            if not isinstance(tag, str):
                continue
            i = el.get(ID)
            if i is not None:
                self.ids.setdefault(i, el)
            styles = self.styles.get(tag)
            if styles is not None:
                for key in (el.get(IDENT), i):
                    if key:
                        styles.setdefault(key, el)
            if body:
                if tag == FOOTNOTES:
                    self.footnotes.append(el)
                    body = False
                elif tag == ANNOTATION:
                    self.loose.append(el)
                    target = el.get(TARGET)
                    if target:
                        self.annotations.setdefault(target, el)
                    body = False
                elif tag in (HEADER, FOOTER):
                    (self.headers if tag == HEADER else self.footers).append(el)
                    body = False
                elif tag == ATTACHMENT:
                    self.attachments.append(el)
                elif tag == PROTOTYPES or tag in TABLES or tag in GHOST:
                    body = False
            stack.extend((c, body) for c in reversed(el))

    # ---- styles
    def _cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _chain(self, kind: str, key):
        """The style named `key` and at most STYLE_CHAIN parents, each once."""
        table = self.styles[kind]
        el, seen = table.get(key) if key else None, set()
        for _ in range(STYLE_CHAIN + 1):
            if el is None or id(el) in seen:
                return
            seen.add(id(el))
            yield el
            parent = el.get(PARENT)
            el = table.get(parent) if parent else None

    def _own_bold(self, el) -> bool | None:
        def find():
            pm = el.find(PROPERTY_MAP)
            b = pm.find(BOLD) if pm is not None else None
            if b is None:
                return None
            for c in b:
                n = _number(c.get(NUMBER))
                if n is not None:
                    return n != 0
            return None
        return self._cached(("ob", id(el)), find)

    def _bold(self, key) -> bool | None:
        """Bold as the character style `key` sets it, None when no style of its chain does."""
        if not key:
            return None
        return self._cached(("b", key), lambda: next(
            (v for v in (self._own_bold(s) for s in self._chain(CHAR_STYLE, key)) if v is not None), None))

    def _heading(self, key) -> int | None:
        def find():
            for s in self._chain(PARA_STYLE, key):
                name = (s.get(NAME) or "").strip()
                if name:
                    return level(name)
            return None
        return self._cached(("h", key), find) if key and self.headings else None

    def _own_list(self, el):
        """The list style a paragraph style names itself (sf:listStyle -> sf:liststyle-ref or an inline style)."""
        def find():
            pm = el.find(PROPERTY_MAP)
            prop = pm.find(LIST_PROPERTY) if pm is not None else None
            for c in prop if prop is not None else ():
                if c.tag == LIST_STYLE:
                    return c
                if c.tag == LIST_REF:
                    target = self.ids.get(c.get(IDREF) or "")
                    if target is not None and target.tag == LIST_STYLE:
                        return target
            return None
        return self._cached(("ol", id(el)), find)

    def _inherited_list(self, key):
        if not key:
            return None
        return self._cached(("l", key), lambda: next(
            (v for v in (self._own_list(s) for s in self._chain(PARA_STYLE, key)) if v is not None), None))

    def _resolve(self, el):
        """An `-ref` element's target by id, else the element itself."""
        if _ref(el):
            return self.ids.get(el.get(IDREF) or "")
        return el

    def _label(self, info) -> str | None:
        """The marker of one sf:list-label-typeinfo: None for `none`, "1." for a numbering text label, else "-"."""
        def find():
            if info.get(TYPE) == "none":
                return None
            for c in info:
                label = self._resolve(c) if c.tag in (TEXT_LABEL, TEXT_LABEL + "-ref") else None
                if label is not None and label.tag == TEXT_LABEL:
                    kind = label.get(TYPE)
                    return "1." if kind is not None and kind not in BULLET_LABELS else "-"
            return "-"
        return self._cached(("lb", id(info)), find)

    def _array(self, arr) -> tuple:
        """The markers of one label array (sf:array or sf:mutable-array of typeinfos or their refs), at most
        MAX_RUNG + 1; read once, since list styles may share an array by ref."""
        def find():
            out = []
            for info in arr:
                info = self._resolve(info)
                if info is not None and info.tag == TYPEINFO:
                    out.append(self._label(info))
                    if len(out) > MAX_RUNG:
                        break
            return tuple(out)
        return self._cached(("ar", id(arr)), find)

    def _own_ladder(self, el) -> tuple:
        """A list style's own markers by rung (sf:property-map/sf:listLabelTypes/array/typeinfo, refs followed once)."""
        def find():
            pm = el.find(PROPERTY_MAP)
            types = pm.find(LABEL_TYPES) if pm is not None else el.find(LABEL_TYPES)
            out = []
            for arr in types if types is not None else ():
                arr = self._resolve(arr)
                if arr is not None:
                    out.extend(self._array(arr))
                    if len(out) > MAX_RUNG:
                        break
            return tuple(out)
        return self._cached(("ld", id(el)), find)

    def _ladder(self, el) -> tuple:
        """The markers of the first list style of `el`'s chain that has any."""
        def find():
            seen, cur = set(), el
            for _ in range(STYLE_CHAIN + 1):
                if cur is None or id(cur) in seen:
                    break
                seen.add(id(cur))
                got = self._own_ladder(cur)
                if got:
                    return got
                parent = cur.get(PARENT)
                cur = self.styles[LIST_STYLE].get(parent) if parent else None
            return ()
        return self._cached(("lad", id(el)), find)

    def _list(self, p) -> tuple[int, str] | None:
        rung = _number(p.get(LEVEL_ATTR))
        if not rung or rung < 1 or rung > MAX_RUNG:
            return None
        named = p.get(LIST_ATTR)
        style = self.styles[LIST_STYLE].get(named) if named else None
        if style is None:
            style = self._inherited_list(p.get(STYLE))
        if style is None:
            return None
        ladder = self._ladder(style)
        marker = ladder[rung] if rung < len(ladder) else None
        return (min(rung - 1, 8), marker) if marker else None

    # ---- notes
    def _new(self, prefix: str) -> str:
        while True:
            self._next[prefix] += 1
            nid = f"{prefix}-{self._next[prefix]}"
            if nid not in self.notes:
                return nid

    def _footnote_ref(self) -> str | None:
        """The note id of the next body footnote mark: the next footnote in order. In a header, footer or note, or
        past the last footnote, the mark counts `note`."""
        if self._noted:
            self.ctx.skipped["note"] += 1
            return None
        k = self._fn_used
        self._fn_used += 1
        if k >= len(self._fn_blocks):
            self.ctx.skipped["note"] += 1
            return None
        blocks = self._fn_blocks[k]
        if not blocks:
            return None
        nid = self._new("fn")
        self.notes[nid] = blocks
        return nid

    def _comment_blocks(self, ann) -> list:
        self._used.add(id(ann))
        blocks = self._noted_walk(ann)
        return comment_note(blocks, "") if blocks else []

    def _field(self, el) -> str | None:
        """The note id of the comment on annotation field `el`, its note read once a document."""
        if self._noted:
            self.ctx.skipped["note"] += 1
            return None
        fid = el.get(ID)
        if not fid:
            return None
        if fid in self._cm:
            nid = self._cm[fid]
            return None if nid in self._moved else nid  # a note a slide title took has no mark elsewhere
        self._cm[fid] = nid = None
        ann = self.annotations.get(fid)
        if ann is None or id(ann) in self._used:
            return None
        with self.ctx.guard("note"):
            blocks = self._comment_blocks(ann)
            if blocks:
                nid = self._new("cm")
                self.notes[nid] = blocks
        self._cm[fid] = nid
        return nid

    def _sticky(self, el):
        """A sticky note's paragraphs as "Comment: …" lines: the slide's comments, else an unmarked note."""
        with self.ctx.guard("note"):
            blocks = self._noted_walk(el)
            if blocks:
                blocks = comment_note(blocks, "")
                if self.sink is not None:
                    self.sink.extend(blocks)
                else:
                    self.notes[self._new("cm")] = blocks

    def _noted_walk(self, el) -> list:
        self._noted += 1
        try:
            return self.walk(el)
        finally:
            self._noted -= 1

    # ---- pictures and tables
    def _data_path(self, el) -> tuple[str | None, bool]:
        """(the first data path under `el` whose member is in the package, whether any path or ref was named). Its
        own sf:data come first; then each `-ref` other than a style's is searched (`_near_data`), once by id."""
        named = False
        for d in el.iter(DATA):
            raw = _attr(d, "path")
            named = named or bool(raw)
            path = _member(raw)
            if path and self.pkg.has(path):
                return path, True
        for d in el.iter():
            if not _ref(d) or d.tag.endswith("style-ref"):
                continue
            rid = d.get(IDREF) or ""
            named = True
            if rid not in self._ref_paths:
                self._ref_paths[rid] = self._near_data(self.ids.get(rid))
            path = self._ref_paths[rid]
            if path:
                return path, True
        return None, named

    def _near_data(self, target) -> str | None:
        """The first data path in the package at most REF_DEPTH levels inside a ref's target (an sf:unfiltered holds
        its sf:data, an sf:filtered-image one level further), so targets nested in one another are each searched
        near themselves only and the searches stay linear."""
        level_ = [target] if target is not None else []
        for _ in range(REF_DEPTH + 1):
            below = []
            for el in level_:
                if el.tag == DATA:
                    path = _member(_attr(el, "path"))
                    if path and self.pkg.has(path):
                        return path
                below.extend(el)
            level_ = below
        return None

    def _image(self, el, out: list):
        """`Image` from a media element's data, within `limits.IMAGE_BYTES` a document (each placement charged); a
        missing, unreadable or empty member, or one past the cap, counts `image`. A media element naming no data at
        all is passed over."""
        path, named = self._data_path(el)
        if path is None:
            if named:
                self.ctx.skipped["image"] += 1
            return
        if self.image_bytes + self.pkg.size(path) > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        try:
            raw = iwork_iwa.read_member(self.pkg, path)
        except ReadError as e:
            if e.kind == "timeout":
                raise
            self.ctx.skipped["image"] += 1
            return
        if not raw:
            self.ctx.skipped["image"] += 1
            return
        self.image_bytes += len(raw)
        out.append(Image(raw))

    def table(self, el) -> list:
        """The blocks of an '09 table. Task 16 reads the cells; here a table counts `table`, and the footnotes its
        cells mark keep their places in the pairing (`_unread_marks`)."""
        self.ctx.skipped["table"] += 1
        self._unread_marks(el)
        return []

    def _unread_marks(self, el):
        """The footnote marks under `el`, which is not read: each takes its footnote in order, kept as an unmarked
        note, so the marks after it still pair with their own footnotes."""
        if self._noted:
            return
        for _ in el.iter(FOOTNOTE):
            k = self._fn_used
            if k >= len(self._fn_blocks):
                return
            self._fn_used += 1
            if self._fn_blocks[k]:
                self.notes[self._new("fn")] = self._fn_blocks[k]

    # ---- text
    def _place(self, el, after: list):
        a = self.ids.get(el.get(IDREF) or "")
        if a is not None and a.tag == ATTACHMENT and id(a) not in self.placed:
            self.placed.add(id(a))
            after.append(a)

    def _href(self, el) -> str | None:
        url = (_attr(el, "href") or "").strip()
        return url[:MAX_URL] if url.lower().startswith(LINK_SCHEMES) else None

    def paragraph(self, p) -> tuple[object | None, list, bool, bool]:
        """(the block of sf:p `p` or None, the elements to place after it, whether it holds a footnote mark, whether
        it holds a footnote break). Walked with an explicit stack: a span's style is in force inside it, a child's
        tail outside it."""
        segs, after = [], []
        mark = brk = False
        stack = [(_OPEN, p, False, None)]
        while stack:
            op, el, bold, url = stack.pop()
            if op == _TAIL:
                if el.tail:
                    segs.append((_TEXT, el.tail.replace(OBJECT_REPLACEMENT, ""), bold, url))
                continue
            if op == _CLOSE:
                nid = self._field(el)
                if nid is not None:
                    segs.append((_NOTE, nid, False, None))
                continue
            tag = el.tag
            if el is not p:
                if not isinstance(tag, str) or tag in GHOST:
                    continue
                if tag == FOOTNOTE:
                    nid = self._footnote_ref()
                    if nid is not None:
                        segs.append((_NOTE, nid, False, None))
                    continue
                if tag in BREAKS or tag == FOOTNOTEBR:
                    brk = brk or tag == FOOTNOTEBR
                    segs.append((_BREAK, None, bold, url))
                    continue
                if tag == FOOTNOTE_MARK:
                    mark = True
                    continue
                if tag == TAB:
                    segs.append((_TEXT, "\t", bold, url))
                    continue
                if tag == ATTACHMENT_REF:
                    self._place(el, after)
                    continue
                if tag in INLINE_BLOCKS:
                    after.append(el)
                    continue
                if _ref(el):
                    continue
                if tag == SPAN:
                    b = self._bold(el.get(STYLE))
                    if b is not None:
                        bold = b
                elif tag == LINK:
                    url = self._href(el) or url
                elif tag == FIELD:
                    stack.append((_CLOSE, el, bold, url))
            if el.text:
                segs.append((_TEXT, el.text.replace(OBJECT_REPLACEMENT, ""), bold, url))
            for c in reversed(el):
                stack.append((_TAIL, c, bold, url))
                stack.append((_OPEN, c, bold, url))
        inl = _inlines(segs)
        if not inl:
            return None, after, mark, brk
        lvl = self._heading(p.get(STYLE))
        if lvl:
            return Heading(lvl, inl), after, mark, brk
        item = self._list(p)
        if item:
            return ListItem(item[0], item[1], inl), after, mark, brk
        return Para(inl), after, mark, brk

    def walk(self, top, out: list | None = None, inside: bool = True) -> list:
        """The blocks of `top`'s children (with `inside`, else of `top` itself) in document order: paragraphs,
        pictures, tables (counted), sticky notes (into the comments), each element once; the app's pruned parts,
        ghost text and `-ref`s are not entered."""
        out = [] if out is None else out
        stack = list(reversed(top)) if inside else [top]
        while stack:
            el = stack.pop()
            tag = el.tag
            if not isinstance(tag, str):
                continue
            if tag == P:
                block, after, _, _ = self.paragraph(el)
                if block is not None:
                    out.append(block)
                stack.extend(reversed(after))
                continue
            if tag in MEDIA:
                self._image(el, out)
                continue
            if tag in TABLES:
                out.extend(self.table(el))
                continue
            if tag in STICKY:
                self._sticky(el)
                continue
            if tag in self.prune or tag in GHOST or tag == ATTACHMENTS or _ref(el):
                continue
            if tag == ATTACHMENT:
                if id(el) in self.entered:
                    continue
                self.entered.add(id(el))
            if tag == FLOATING and self.defer is not None:
                self.defer.append(el)
                continue
            stack.extend(reversed(el))
        return out

    # ---- Pages
    def _read_footnotes(self):
        """The footnotes, in order: a paragraph with a footnote mark starts one, a footnote break ends one."""
        self._noted += 1
        try:
            for el in self.footnotes:
                with self.ctx.guard("note"):
                    cur = None
                    stack = list(reversed(el))
                    while stack:
                        node = stack.pop()
                        tag = node.tag
                        if not isinstance(tag, str) or tag in GHOST or tag == ATTACHMENTS or _ref(node):
                            continue
                        if tag in MEDIA or tag in TABLES or tag in STICKY:  # a drawable between paragraphs
                            if cur is None:
                                cur = []
                                self._fn_blocks.append(cur)
                            self.walk(node, cur, inside=False)
                            continue
                        if tag != P:
                            stack.extend(reversed(node))
                            continue
                        block, after, mark, brk = self.paragraph(node)
                        if cur is None or mark:
                            cur = []
                            self._fn_blocks.append(cur)
                        if block is not None:
                            cur.append(block)
                        for a in after:
                            self.walk(a, cur, inside=False)
                        if brk:
                            cur = None
        finally:
            self._noted -= 1

    def pages(self) -> Doc:
        doc = self.doc
        self._read_footnotes()
        failure = None
        self.defer = []
        try:
            self.walk(self.root, doc.blocks)
        except ReadError:
            raise
        except Exception as e:
            failure = e
        floating, self.defer = self.defer, None
        for el in floating:
            with self.ctx.guard("shape"):
                self.walk(el, doc.blocks)
        for a in self.attachments:  # the ones no attachment-ref placed, last
            if id(a) not in self.placed and id(a) not in self.entered:
                with self.ctx.guard("object"):
                    self.walk(a, doc.blocks, inside=False)
        for elements, lines in ((self.headers, doc.headers), (self.footers, doc.footers)):
            for el in elements:
                with self.ctx.guard("header" if lines is doc.headers else "footer"):
                    lines.extend(plain(self._noted_walk(el)))
        for blocks in self._fn_blocks[self._fn_used:]:  # footnotes no mark names: kept unmarked
            if blocks:
                self.notes[self._new("fn")] = blocks
        for ann in self.loose:  # comments no field names: kept unmarked
            if id(ann) not in self._used:
                with self.ctx.guard("note"):
                    blocks = self._comment_blocks(ann)
                    if blocks:
                        self.notes[self._new("cm")] = blocks
        if failure is not None:
            if not doc.blocks:
                raise ReadError("damaged", f"iWork '09 body: {type(failure).__name__}")
            self.ctx.skipped["document"] += 1
            if self.ctx.skipped["document"] == 1:
                self.ctx.warnings.append("document: skipped damaged part")
        return doc

    # ---- Keynote
    def _title(self, blocks: list, sec: Section) -> str:
        """The title text of a title placeholder's blocks; a block without inlines goes to the body, and the notes
        the title's marks name to the slide's comments."""
        lines = []
        for b in blocks:
            if not hasattr(b, "inlines"):
                sec.blocks.append(b)
                continue
            lines.extend(plain([b]))
            stack = [iter(b.inlines)]
            while stack:
                it = next(stack[-1], None)
                if it is None:
                    stack.pop()
                elif isinstance(it, NoteRef):
                    self._moved.add(it.id)
                    sec.comments.extend(self.notes.pop(it.id, []))
                elif isinstance(it, (Bold, Link)):
                    stack.append(iter(it.inlines))
        return " ".join(" ".join(lines).split())[:iwork_iwa.MAX_TITLE]

    def _slide(self, slide, number: int) -> Section:
        sec = Section("slide", number=number)
        self.sink = sec.comments
        try:
            kids = list(slide)
            title = []
            for c in kids:
                if c.tag == TITLE_PH:
                    self.walk(c, title)
            body = []
            for c in kids:
                if c.tag == BODY_PH:
                    self.walk(c, body)
            sec.title = self._title(title, sec)
            sec.blocks.extend(body)
            for c in kids:
                if c.tag == PAGE:
                    self.walk(c, sec.blocks)
            for c in kids:
                if c.tag == STICKIES:
                    for note in c:
                        if isinstance(note.tag, str):
                            self._sticky(note)
            for c in kids:
                if c.tag == NOTES:
                    sec.notes.extend(self._noted_walk(c))
        finally:
            self.sink = None
        return sec

    def keynote(self) -> Doc:
        number = 0
        for lst in self.root:
            if lst.tag != SLIDE_LIST:
                continue
            for slide in lst:
                if slide.tag != SLIDE:
                    continue
                number += 1
                with self.ctx.guard("slide"):
                    self.doc.blocks.append(self._slide(slide, number))
        return self.doc


# app -> the Reader method that walks it (Task 16 adds "numbers")
WALKS = {"pages": Reader.pages, "key": Reader.keynote}


def parse(root, app: str, pkg, ctx) -> Doc:
    """The document of an '09 index root. The root element names the app; an unknown root is read as the
    extension's app. A root no walk reads gives a Doc with no blocks. A walk that fails outside its guarded parts is
    `damaged` (the caller's preview fallback)."""
    kind = ROOTS.get(root.tag, app)
    fn = WALKS.get(kind)
    if fn is None:
        return Doc()
    try:
        return fn(Reader(root, pkg, ctx, kind))
    except ReadError:
        raise
    except Exception as e:
        raise ReadError("damaged", f"iWork '09 {kind}: {type(e).__name__}")
