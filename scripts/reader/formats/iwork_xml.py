"""iWork '09 XML (Pages and Numbers `index.xml`, Keynote `index.apxl`) to the document model.

Ported from docling (MIT) `docling/backend/iwork/legacy.py` (the `sf` vocabulary: `legacy_runs`, `legacy_styles`,
`legacy_list_styles`, `legacy_inherited_lists`, `legacy_list_label`, `legacy_formatting`, `legacy_picture`,
`sf_attr`), `docling/backend/iwork/pages_xml.py` (`read_content`, `iter_body_elements`, `legacy_furniture`,
`legacy_comments`), `docling/backend/iwork/keynote_xml.py` (`iter_slides`, `read_slide`, `slide_drawables`,
`slide_placeholders`, `iter_drawables`, `drawable_blocks`, `sticky_note_text`, `slide_notes`) and
`docling/backend/iwork/numbers_xml.py` (`read_content`, `read_table`, `read_cells`, `place_row`, `column_quotas`,
`cell_text`, `text_value`, `popup_text`, `formula_text`, `read_comment`), v2.150.0
(1ae470a1f4580881ed73853f64e0bf178f623c5e).

`iwork._xml` hands over the parsed root (`zipx.parse_xml_ns`: a DOCTYPE or entity is refused there). The root
picks the app (`sl:document` Pages, `key:presentation` Keynote, `ls:document` Numbers); an unknown root is read by
the extension's app, and a root no walk reads gives a Doc with no blocks (the preview fallback).

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
inside searched REF_DEPTH levels into its target, once by id (REF_DEPTH and MAX_RUNG are local lookup bounds, not
Limits caps); `sf:ghost-text` and `sf:ghost-text-ref` are the template's placeholder text and are dropped uncounted.

Tables (`sf:tabular-info`, `sf:tabular-model`, in all three apps): the model (a child, or the one its `-ref` names;
read once) gives one Table captioned with its `sf:name`, through `sheet_grid.Grid` with one `Budget` a document, so
a table renders as xlsx does and the Grid caps stop it (`sheet_cut`). Its `sf:grid` `sf:datasource` holds the cells
a row stores, in order and with no coordinates; each `sf:grid-row` `sf:nc` says how many are that row's. The
`sf:grid-column` counts, when they add up, place a sparse row: the rows whose widths fill the declared columns are
taken off the counts first, and a sparse row then passes over a column whose remaining count is spent (docling
`place_row`, which spends the counts in row order and so pushes a row above the full ones, an empty top-left corner,
to the left). '09 Numbers allows at most 256 columns, so only a crafted file passes SHEET_COLS and falls back to
left-packed rows. Cells: `sf:t` text (`sf:ct`: its `sfa:s`, else its text, a line a paragraph; a cell, table or
chart nested in it is not entered); `sf:n` the number `sf:v` as General; `sf:d` the seconds `sf:cell-date` from
2001-01-01 as an ISO date, plus the time when not midnight; `sf:b` and `sf:cb` TRUE or FALSE; `sf:pm` the choice its
`sf:proxied-cell-ref` names, looked up in its own `sf:menu-choices` first and then in the document's id index, once
an id; `sf:f` its cached result `sf:r` (`sf:rn`, `sf:rt`, `sf:rd`, `sf:rb`), never its formula; `sf:g` empty; `sf:s`
holds its column with no value; `sf:col-span` is one merged cell (a row does not say how far down a merge goes, so
none spans rows, as in docling). Hidden rows are not read (Defaults "Numbers"), nor are header rows marked. Nothing
is allocated from a declared count: the declared rows and columns only bound the walk, a span stops at them and at
SHEET_COLS, rows are walked to SHEET_ROWS, and each entry, each choice looked at and each node of a cell's text is
charged to the document's scan count, to SHEET_SCAN; entries past the declared grid, or that no row claims, count
`record` once a table, also when a cut follows. A table fails (`table`) without a model, a grid, or the rows its
values need; each is read under a guard of its own. A footnote its cells mark keeps its place in the pairing below
as an unmarked note (a cell shows its text only). In a header or footer a table counts `table` and is not read.

Pages (`sl:document`): the body is the document outside `sf:header`, `sf:footer`, `sf:footnotes`, `sf:annotations`
and `sl:section-prototypes` (the templates of new sections, like ghost text), in document order, then the floating
drawables of `sl:drawables` in their container's order (Defaults "Pages styles"), each under a guard of its own
(`image`, `table`, `chart` or `shape`, as `iwork_iwa` guards an IWA drawable), then the attachments no
`sf:attachment-ref` placed (one that fails counts `object`). An `sf:attachment-ref` places its attachment after its
paragraph, once; any other `-ref` (a text box chain's shape ref, say) is never followed, so nothing is read twice.
Footnotes: the text of
`sf:footnotes` is split at each `sf:footnote-mark` and after each `sf:footnotebr`, inside a paragraph too (a storage
with no marks opens a footnote at its next content), and paired in order with the body's `sf:footnote` marks
(`[^n]`); a mark in an unread table takes its footnote too, kept unmarked; a mark past the last footnote counts
`note`, and a footnote no mark names is kept as an unmarked note. Comments: an `sf:annotation` names the
`sf:annotation-field` it comments by `sf:target`; its paragraphs become a "Comment: …" note
(`word_fields.comment_note`; '09 records no author), marked where the field ends and read once a document, so a
field repeated gives the same mark; one no field names is kept unmarked. Headers and footers (every page variant;
the renderer drops repeats) are one line a paragraph, their pictures counted `image` and never read; a note mark
there, in a footnote or in a comment counts `note` and adds no note. A body that cannot be read counts `document`
and the rest of the page is still read. Tracked changes are not read: no public source names the '09 change elements
(docling and libetonyek's token tables name none; Apple's iWork Programming Guide dates from 2005, before Pages
tracked changes), so a tracked deletion shows as body text.

Keynote (`key:presentation`): each `key:slide` of `key:slide-list`, under `ctx.guard("slide")` (a failed slide
counts `slide` and keeps its number), is `Section("slide", title, number=n)`: the title placeholder gives the title
(MAX_TITLE characters; a picture in it goes to the body, and the notes its marks name to the comments), then the
body placeholder, then the drawables of `key:page`, each under its own guard as in Pages (the master's layer proxy
is not followed, nor the placeholder refs), then `key:notes` as `Section.notes`; sticky notes, on the page or in
`key:sticky-notes`, are "Comment: …" lines in `Section.comments`. A comment no field names is kept as an unmarked
note, and an attachment no ref places counts `object`. The theme's master slides are never read.

Numbers (`ls:document`): each `ls:workspace` (a workspace is not searched for more), under `ctx.guard("sheet")`, is
`Section("sheet", ls:workspace-name)`: its tables, pictures and text boxes in order, each under its own guard as in
Pages, its sticky notes as "Comment: …" lines in `Section.comments`. The template's parts outside the workspaces are
never read, and nothing in them (an attachment, a comment) is indexed or counted.

Charts: an `sf:chart-info`, in any app, is a leaf: it counts `chart` and is never entered ('09 charts keep their
values in `.chrtshr` property lists, which are not read), so nothing under it reaches the output.

Every walk is iterative (an explicit stack, so a deep tree never reaches Python's recursion limit) and enters each
element at most once (a cell's text walk stops at a nested cell, and a pop-up's choice is looked up once an id), so
the work is bounded by the element count, which `iwork.IWORK_XML` bounds. Pictures are
charged against `limits.IMAGE_BYTES` per document, each placement (a member is read once); one missing or past it
counts `image`.

Changes from docling: footnotes are paired with their marks rather than listed as furniture; comments are marked
where they apply; floating drawables follow the body; templates are left out; a character style that does not set
bold keeps the bold in force; list ladders follow their `-ref`s; a sheet keeps its drawables in their order (docling
sorts them by position); a table is read into a Grid rather than allocated from its declared size, and one past the
caps is cut rather than dropped; full rows are taken off the column counts before sparse rows are placed; a span
spends only the columns whose counts are left; Pages and Keynote tables are read too; charts are counted.
"""
import math
from bisect import bisect_left
from itertools import groupby

from .. import limits
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Image, Link, ListItem, NoteRef, Para, Section, Table, Text
from ..numfmt import display
from . import iwork_iwa, sheet_grid
from .iwork_tables import EPOCH_SERIAL, ISO_DATE, ISO_TIME
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
SHAPES = frozenset({SF + "drawable-shape", SF + "shape", SF + "group"})
CHART = SF + "chart-info"  # an '09 chart: a leaf, counted `chart`, never read
# drawables written inside a paragraph are placed after it, as an anchored attachment is
INLINE_BLOCKS = MEDIA | TABLES | STICKY | SHAPES | {CHART}
STYLESHEETS = frozenset({SL + "stylesheet", KEY + "stylesheet", SF + "stylesheet", SF + "styles", SF + "anon-styles"})
PROTOTYPES, FLOATING = SL + "section-prototypes", SL + "drawables"
PAGES_PRUNE = STYLESHEETS | {HEADER, FOOTER, FOOTNOTES, ANNOTATIONS, ANNOTATION, PROTOTYPES}
SLIDE_LIST, SLIDE, PAGE, NOTES = KEY + "slide-list", KEY + "slide", KEY + "page", KEY + "notes"
TITLE_PH, BODY_PH, STICKIES = KEY + "title-placeholder", KEY + "body-placeholder", KEY + "sticky-notes"
KEY_PRUNE = STYLESHEETS | {SF + "proxy-master-layer", ANNOTATIONS, ANNOTATION}
THEMES = KEY + "theme-list"  # the theme's master slides
WORKSPACE, WORKSPACE_NAME = LS + "workspace", LS + "workspace-name"

# tables (docling `numbers_xml.py`, `legacy.py`)
TABULAR_MODEL, TABULAR_MODEL_REF = SF + "tabular-model", SF + "tabular-model-ref"
GRID, DATASOURCE, ROWS, GRID_ROW, COLUMNS, GRID_COLUMN = (
    SF + "grid", SF + "datasource", SF + "rows", SF + "grid-row", SF + "columns", SF + "grid-column")
NUMROWS, NUMCOLS, NC, COL_SPAN = SF + "numrows", SF + "numcols", SF + "nc", SF + "col-span"
CELL_TEXT, STRING, VALUE, CELL_DATE = SF + "ct", SFA + "s", SF + "v", SF + "cell-date"
FORMULA_CELL, POPUP_CELL, SPANNED, RESULT, PROXIED = (
    SF + "f", SF + "pm", SF + "s", SF + "r", SF + "proxied-cell-ref")
TEXT_VALUES = frozenset({SF + "t", SF + "rt"})
NUMBER_VALUES = frozenset({SF + "n", SF + "rn"})
BOOL_VALUES = frozenset({SF + "b", SF + "cb", SF + "rb"})
DATE_VALUES = frozenset({SF + "d", SF + "rd"})
RESULTS = frozenset({SF + "rn", SF + "rt", SF + "rd", SF + "rb"})  # a formula's cached result, in sf:r
MENU_CHOICES = SF + "menu-choices"
# a cell's text walk enters none of these: cells, tables and charts nested in it, ghost text and note marks
NOT_IN_TEXT = (TEXT_VALUES | NUMBER_VALUES | BOOL_VALUES | DATE_VALUES | TABLES | GHOST
               | {FORMULA_CELL, POPUP_CELL, SPANNED, SF + "g", CHART, FOOTNOTE})

PARA_STYLE, CHAR_STYLE, LIST_STYLE = SF + "paragraphstyle", SF + "characterstyle", SF + "liststyle"
ID, IDREF, IDENT, PARENT = SFA + "ID", SFA + "IDREF", SF + "ident", SF + "parent-ident"
NAME, STYLE, TARGET, TYPE, NUMBER = SF + "name", SF + "style", SF + "target", SF + "type", SFA + "number"
LIST_ATTR, LEVEL_ATTR = SF + "list-style", SF + "list-level"
PROPERTY_MAP, BOLD, LIST_PROPERTY, LIST_REF = SF + "property-map", SF + "bold", SF + "listStyle", SF + "liststyle-ref"
LABEL_TYPES, TYPEINFO, TEXT_LABEL = SF + "listLabelTypes", SF + "list-label-typeinfo", SF + "text-label"
BULLET_LABELS = frozenset({"bullet", "image", "string", "text"})  # text-label types that draw a mark (docling)
# local lookup bounds, not caps of the Limits table: no content is cut by them (a rung past MAX_RUNG is body text;
# a picture whose data sits deeper than REF_DEPTH in a ref's target counts `image`, as a missing one does)
MAX_RUNG = 99  # list rungs read; a real ladder has nine
REF_DEPTH = 3  # levels searched for picture data inside a ref's target

_TEXT, _NOTE, _BREAK, _MARK, _FNBR = 0, 1, 2, 3, 4  # paragraph segment kinds (a footnote's mark and its end)
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


class _ScanCut(Exception):
    """The document's datasource walk reached limits.SHEET_SCAN."""


def _count(v) -> int | None:
    """A declared positive count, None when absent, malformed or not positive."""
    n = _number(v)
    return n if n is not None and n > 0 else None


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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
        self._raw: dict[str, bytes | None] = {}  # picture member -> its bytes, read once
        self._fn_blocks: list[list] = []        # the footnotes, in order
        self._fn_used = 0                       # body marks paired so far
        self._cm: dict[str, str | None] = {}    # annotation field id -> note id
        self._used: set[int] = set()            # annotations read
        self._moved: set[str] = set()           # note ids a slide title moved into its comments
        self.placed: set[int] = set()           # attachments an attachment-ref placed
        self.entered: set[int] = set()          # attachments walked
        self._noted = 0                         # > 0 while a header, footer, footnote, comment or slide note is read
        self._furniture = False                 # a header or footer is read (lines only: pictures are counted)
        self._next = {"fn": 0, "cm": 0}
        self.defer: list | None = None          # Pages: the floating drawables, read after the body
        self.sink: list | None = None           # Keynote: the current slide's comments
        self.image_bytes = 0
        self.budget = sheet_grid.Budget()       # the cells and characters all the document's tables share
        self.scan = 0                           # datasource entries walked, against limits.SHEET_SCAN
        self._tables: set[int] = set()          # table models read
        self._choices: dict[str, str | None] = {}  # pop-up choice id -> the value it shows
        self._index()

    # ---- the one pass
    def _index(self):
        """Element ids, styles (anywhere), and in the body scope (outside templates, tables, charts and ghost text;
        in Numbers, only inside the first level of ls:workspace) the footnotes, comments, headers, footers and
        attachments."""
        numbers = self.app == "numbers"
        stack = [(self.root, not numbers, False)]
        while stack:
            el, body, ws = stack.pop()
            tag = el.tag
            if not isinstance(tag, str):
                continue
            if numbers and tag == WORKSPACE and not ws:  # the walk reads these, and nothing outside them
                body = ws = True
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
                elif tag in (PROTOTYPES, THEMES, CHART) or tag in TABLES or tag in GHOST:
                    body = False
            stack.extend((c, body, ws) for c in reversed(el))

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
        """`Image` from a media element's data, within `limits.IMAGE_BYTES` a document (each placement charged, each
        member read once); a missing, unreadable or empty member, or one past the cap, counts `image`. A media element
        naming no data at all is passed over."""
        path, named = self._data_path(el)
        if path is None:
            if named:
                self.ctx.skipped["image"] += 1
            return
        if self.image_bytes + self.pkg.size(path) > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        if path not in self._raw:
            try:
                self._raw[path] = iwork_iwa.read_member(self.pkg, path)
            except ReadError as e:
                if e.kind == "timeout":
                    raise
                self._raw[path] = None
        raw = self._raw[path]
        if not raw:
            self.ctx.skipped["image"] += 1
            return
        self.image_bytes += len(raw)
        out.append(Image(raw))

    def table(self, el) -> list:
        """The blocks of an '09 table (`sf:tabular-info` or `sf:tabular-model`): one Table through `sheet_grid.Grid`
        (`_table`), under a guard of its own, so a table that fails counts `table` and adds nothing. In a header or
        footer, which shows lines only, a table counts `table` and is not read. Either way the footnotes its cells
        mark keep their places in the pairing (`_unread_marks`)."""
        self._unread_marks(el)
        if self._furniture:
            self.ctx.skipped["table"] += 1
            return []
        out = []
        with self.ctx.guard("table"):
            out = self._table(el)
        return out

    def _unread_marks(self, el):
        """The footnote marks under `el`, whose marks are not shown (a cell shows its text only): each takes its
        footnote in order as a body mark does (`_footnote_ref`, counted the same way), kept as an unmarked note, so
        the marks after it still pair with their own footnotes."""
        for _ in el.iter(FOOTNOTE):
            self._footnote_ref()

    def _model(self, el):
        """The sf:tabular-model of a table element: itself, its child, or the one its `-ref` child names."""
        if el.tag == TABULAR_MODEL:
            return el
        for c in el:
            if c.tag == TABULAR_MODEL:
                return c
            if c.tag == TABULAR_MODEL_REF:
                target = self.ids.get(c.get(IDREF) or "")
                if target is not None and target.tag == TABULAR_MODEL:
                    return target
        return None

    def _table(self, el) -> list:
        """One Table captioned with the model's `sf:name`, [] for a table with no values or a model already read
        (one placed twice is read once). ValueError without a model or a grid."""
        model = self._model(el)
        if model is None:
            raise ValueError("no table model")
        if id(model) in self._tables:
            return []
        self._tables.add(id(model))
        grid_el = model.find(GRID)
        if grid_el is None:
            raise ValueError("no table grid")
        name = (model.get(NAME) or "").strip()
        grid = sheet_grid.Grid(name or "table", self.budget, self.ctx)
        self._cells(grid_el, grid)
        out = grid.blocks()
        if not out:
            return []
        if isinstance(out[0], Table):
            out[0].caption = name
            return out[:1]
        return [Table([[Cell(out[:1])]], caption=name)]  # one cell: Grid gives a paragraph

    def _quotas(self, el, ncols: int | None, n_entries: int):
        """(the columns whose count is not spent, sorted; each column's count) from the sf:grid-column `sf:nc`
        counts, or (None, None) when they cannot place cells (docling `column_quotas`): no sf:columns, no declared
        column count, more grid columns than SHEET_COLS, a number other than the declared one, or counts adding up
        to more than the datasource holds. '09 Numbers allows at most 256 columns, so only a crafted file passes
        SHEET_COLS and falls back to left-packed rows."""
        cols = el.find(COLUMNS)
        if cols is None or ncols is None:
            return None, None
        q = []
        for c in cols.iter(GRID_COLUMN):
            if len(q) >= min(ncols, limits.SHEET_COLS):
                return None, None
            q.append(max(_number(c.get(NC)) or 0, 0))
        if len(q) != ncols or sum(q) > n_entries:
            return None, None
        return [c for c, n in enumerate(q) if n > 0], q

    @staticmethod
    def _spend(avail: list, quotas: list, col: int, span: int):
        """Each column in col..col+span-1 that still stores a cell stores one less; a spent column leaves `avail`.
        The work is bounded by the counts, whatever the span."""
        lo, hi = bisect_left(avail, col), bisect_left(avail, col + span)
        keep = []
        for c in avail[lo:hi]:
            quotas[c] -= 1
            if quotas[c] > 0:
                keep.append(c)
        avail[lo:hi] = keep

    def _cells(self, el, grid):
        """The datasource of sf:grid `el` put on `grid` (docling `read_cells`, `place_row`). The datasource holds
        only the cells a row stores, in row order, with no coordinates: each sf:grid-row's `sf:nc` says how many
        are its own. Rows are claimed first (`plan`, one entry per row that has cells, walked as they come to
        SHEET_ROWS); the rows that fill the declared columns are taken off the column counts (`_full_rows`), then
        each row is laid out (`_lay_out`). Nothing is allocated from a declared count. Past a cap the sheet is cut
        (`Grid.stop`). Entries past the declared rows or columns, or that no row claims, are dropped, counted
        `record` once a table, also when the sheet is cut later."""
        ds, rows = el.find(DATASOURCE), el.find(ROWS)
        entries = [e for e in ds if isinstance(e.tag, str)] if ds is not None else []
        if not entries:
            return
        if rows is None:
            raise ValueError("no grid rows")
        ncols, nrows = _count(el.get(NUMCOLS)), _count(el.get(NUMROWS))
        avail, quotas = self._quotas(el, ncols, len(entries))
        plan, k, total, rows_cut = [], 0, len(entries), False  # (row, first entry, end)
        for r, row in enumerate(rows.iter(GRID_ROW)):
            if k >= total:
                break
            if r >= limits.SHEET_ROWS:
                rows_cut = True
                break
            if nrows is not None and r >= nrows:
                break
            n = _number(row.get(NC)) or 0
            if n > 0:
                plan.append((r, k, min(k + n, total)))
                k += n
        full = self._full_rows(entries, plan, ncols, avail, quotas) if avail is not None else set()
        dropped = self._lay_out(grid, entries, plan, full, ncols, avail, quotas)
        if rows_cut and not grid.cut:
            grid.stop("rows", limits.SHEET_ROWS)
        if dropped or (k < total and not rows_cut and not grid.cut):
            self.ctx.skipped["record"] += 1

    def _full_rows(self, entries, plan, ncols: int, avail: list, quotas: list) -> set:
        """The indexes in `plan` of the rows whose widths (each entry's `sf:col-span`, an sf:s one column) fill the
        declared columns, their cells taken off the column counts first (an sf:s was never counted). A sparse row
        is then placed against what the full rows leave, so one above them (an empty top-left corner) is not pushed
        left. A row is looked at only until its width passes the columns, and only as far as SHEET_SCAN leaves
        entries to place, so the pass is linear in the entries."""
        full, left = set(), limits.SHEET_SCAN - self.scan
        for idx, (r, a, b) in enumerate(plan):
            if left <= 0:
                break
            width = 0
            for i in range(a, b):
                left -= 1
                e = entries[i]
                width += 1 if e.tag == SPANNED else max(1, min(_number(e.get(COL_SPAN)) or 1, ncols))
                if width > ncols:
                    break
            if width != ncols:
                continue
            full.add(idx)
            col = 0
            for i in range(a, b):
                e = entries[i]
                if e.tag == SPANNED:
                    col += 1
                    continue
                span = max(1, min(_number(e.get(COL_SPAN)) or 1, ncols - col))
                self._spend(avail, quotas, col, span)
                col += span
        return full

    def _lay_out(self, grid, entries, plan, full, ncols, avail, quotas) -> bool:
        """Each planned row's entries put on `grid`. A full row, or any row without usable column counts, is laid
        out from its first column; a sparse row passes over a column whose count is spent and spends the columns it
        covers. An sf:s holds its column with no value; `sf:col-span` (stopped at the declared columns and at
        SHEET_COLS) is one merged cell. Each entry, and each node its value is read from, is charged to the
        document's scan count; at SHEET_SCAN the sheet is cut. True when an entry was dropped before any cut: past
        the declared columns, or with no column left."""
        dropped = False
        try:
            for idx, (r, a, b) in enumerate(plan):
                packed = avail is None or idx in full
                col = 0
                for i in range(a, b):
                    self._charge()
                    e = entries[i]
                    if e.tag == SPANNED:
                        col += 1
                        continue
                    if not packed:  # the next column that still stores a cell
                        j = bisect_left(avail, col)
                        if j == len(avail):
                            dropped = True
                            break
                        col = avail[j]
                    if ncols is not None and col >= ncols:
                        dropped = True
                        break
                    span = _number(e.get(COL_SPAN)) or 1
                    span = max(1, min(span, limits.SHEET_COLS - col, (ncols - col) if ncols is not None else span))
                    if not packed:
                        self._spend(avail, quotas, col, span)
                    value = self._shown(e)
                    if value:
                        if not grid.put(r, col, value):
                            return dropped  # the grid is cut
                        if span > 1:
                            grid.merge(r, r, col, col + span - 1)
                    col += span
        except _ScanCut:
            grid.stop("scanned cells", limits.SHEET_SCAN)
        return dropped

    def _charge(self):
        """One step of a datasource walk against limits.SHEET_SCAN; _ScanCut once it is spent."""
        if self.scan >= limits.SHEET_SCAN:
            raise _ScanCut
        self.scan += 1

    def _shown(self, el) -> str | None:
        """The value a datasource entry shows: a formula's result (`sf:r`: the first `sf:rn`, `sf:rt`, `sf:rd` or
        `sf:rb`), never its formula; a pop-up's chosen value (`_popup`); else the cell's own (`_value`)."""
        tag = el.tag
        if tag == FORMULA_CELL:
            result = el.find(RESULT)
            for v in result if result is not None else ():
                if v.tag in RESULTS:
                    return self._value(v)
            return None
        if tag == POPUP_CELL:
            return self._popup(el)
        return self._value(el)

    def _popup(self, el) -> str | None:
        """A pop-up cell's shown value: the choice its `sf:proxied-cell-ref` names (`_choice`), followed through at
        most MAX_DEPTH pop-ups with a visited path; a cycle or a chain past it counts `depth`. The answer is cached
        by id, so a choice shared by many cells is looked up once."""
        path, value = [], None
        for _ in range(limits.MAX_DEPTH):
            if el.tag != POPUP_CELL:
                value = self._shown(el)
                break
            ref_el = el.find(PROXIED)
            key = ref_el.get(IDREF) if ref_el is not None else None
            if not key:
                break
            if key in self._choices:
                value = self._choices[key]
                break
            if key in path:
                self.ctx.skipped["depth"] += 1
                break
            path.append(key)
            el = self._choice(el, key)
            if el is None:
                break
        else:
            self.ctx.skipped["depth"] += 1
        for key in path:
            self._choices[key] = value
        return value

    def _choice(self, pm, key: str):
        """The element `key` names for pop-up `pm`: a child of its own sf:menu-choices with that id (docling
        `popup_text` looks only inside the cell, and Numbers keeps the choices there), else the document's id index
        (a choice kept elsewhere, named by the `-ref`). Each child looked at is charged to SHEET_SCAN."""
        menu = pm.find(MENU_CHOICES)
        for c in menu if menu is not None else ():
            self._charge()
            if c.get(ID) == key:
                return c
        return self.ids.get(key)

    def _value(self, el) -> str | None:
        """A cell or result element's value as shown: `sf:t`/`sf:rt` text (`sf:ct`), `sf:n`/`sf:rn` the number
        `sf:v` as General, `sf:b`/`sf:cb`/`sf:rb` TRUE or FALSE (a value above 0 is TRUE), `sf:d`/`sf:rd` the
        seconds `sf:cell-date` from 2001-01-01 as an ISO date, plus the time when it is not midnight. None for an
        empty cell (`sf:g`), a missing or non-finite value, or any other element."""
        tag = el.tag
        if tag in TEXT_VALUES:
            ct = el.find(CELL_TEXT)
            return self._cell_text(ct) if ct is not None else None
        if tag in NUMBER_VALUES:
            v = _finite(el.get(VALUE))
            return None if v is None else display(v, "General")
        if tag in BOOL_VALUES:
            v = _finite(el.get(VALUE))
            return None if v is None else "TRUE" if v > 0 else "FALSE"
        if tag in DATE_VALUES:
            s = _finite(el.get(CELL_DATE))
            if s is None:
                return None
            return display(EPOCH_SERIAL + s / 86400, ISO_TIME if s % 86400 else ISO_DATE)
        return None

    def _cell_text(self, ct) -> str | None:
        """An sf:ct's text: its `sfa:s`, else its content's text, a line a paragraph or break, outer whitespace and
        empty lines dropped. A cell's text holds paragraphs, not cells: a cell, table or chart nested in it is not
        entered (`NOT_IN_TEXT`, with ghost text and note marks), so the text walks of different cells never overlap.
        Walked with an explicit stack, each node charged to SHEET_SCAN."""
        s = ct.get(STRING)
        if s is not None:
            return s.strip() or None
        out, stack = [], [(ct, False)]
        while stack:
            el, tail = stack.pop()
            if tail:
                if el.tail:
                    out.append(el.tail)
                continue
            if el is not ct:
                tag = el.tag
                if not isinstance(tag, str) or tag in NOT_IN_TEXT:
                    continue
                self._charge()
                if tag == P and out:
                    out.append("\n")
                if tag in BREAKS:
                    out.append("\n")
                    continue
                if tag == TAB:
                    out.append("\t")
                    continue
            if el.text:
                out.append(el.text)
            for c in reversed(el):
                stack.append((c, True))
                stack.append((c, False))
        lines = (ln.strip() for ln in "".join(out).replace(OBJECT_REPLACEMENT, "").split("\n"))
        return "\n".join(ln for ln in lines if ln) or None

    # ---- text
    def _place(self, el):
        """The attachment an sf:attachment-ref names, the first time it is named, else None."""
        a = self.ids.get(el.get(IDREF) or "")
        if a is not None and a.tag == ATTACHMENT and id(a) not in self.placed:
            self.placed.add(id(a))
            return a
        return None

    def _href(self, el) -> str | None:
        url = (_attr(el, "href") or "").strip()
        return url[:MAX_URL] if url.lower().startswith(LINK_SCHEMES) else None

    def paragraph(self, p) -> tuple[object | None, list]:
        """(the block of sf:p `p` or None, the elements to place after it)."""
        segs, after = self._segments(p)
        return self._block(p, segs), [el for _, el in after]

    def _segments(self, p) -> tuple[list, list]:
        """(the segments of sf:p `p`, with a footnote's mark and its end as segments; the elements to place after
        it, each with the number of segments before it). Walked with an explicit stack: a span's style is in force
        inside it, a child's tail outside it."""
        segs, after = [], []
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
                if tag in BREAKS:
                    segs.append((_BREAK, None, bold, url))
                    continue
                if tag == FOOTNOTEBR:
                    segs.append((_FNBR, None, bold, url))
                    continue
                if tag == FOOTNOTE_MARK:
                    segs.append((_MARK, None, False, None))
                    continue
                if tag == TAB:
                    segs.append((_TEXT, "\t", bold, url))
                    continue
                if tag == ATTACHMENT_REF:
                    a = self._place(el)
                    if a is not None:
                        after.append((len(segs), a))
                    continue
                if tag in INLINE_BLOCKS:
                    after.append((len(segs), el))
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
        return segs, after

    def _block(self, p, segs: list):
        """The block of segments of sf:p `p` (all of them, or one footnote's part), None when no text is left."""
        segs = [(_BREAK, None, s[2], s[3]) if s[0] == _FNBR else s for s in segs if s[0] != _MARK]
        inl = _inlines(segs)
        if not inl:
            return None
        lvl = self._heading(p.get(STYLE))
        if lvl:
            return Heading(lvl, inl)
        item = self._list(p)
        if item:
            return ListItem(item[0], item[1], inl)
        return Para(inl)

    def walk(self, top, out: list | None = None, inside: bool = True) -> list:
        """The blocks of `top`'s children (with `inside`, else of `top` itself) in document order: paragraphs,
        pictures, tables, sticky notes (into the comments), each element once; a chart counts `chart` and is not
        entered, nor are the app's pruned parts, ghost text and `-ref`s."""
        out = [] if out is None else out
        stack = list(reversed(top)) if inside else [top]
        while stack:
            el = stack.pop()
            tag = el.tag
            if not isinstance(tag, str):
                continue
            if tag == P:
                block, after = self.paragraph(el)
                if block is not None:
                    out.append(block)
                stack.extend(reversed(after))
                continue
            if tag in MEDIA:
                if self._furniture:  # a header's picture is not shown: counted, never read
                    path, named = self._data_path(el)
                    if path or named:
                        self.ctx.skipped["image"] += 1
                else:
                    self._image(el, out)
                continue
            if tag in TABLES:
                out.extend(self.table(el))
                continue
            if tag == CHART:  # '09 charts keep their values in .chrtshr property lists, which are not read
                self.ctx.skipped["chart"] += 1
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

    def _drawables(self, top, out: list):
        """Each drawable under `top` (descending through its layers, page groups and other containers) read under
        a guard of its own: `image` for a picture, `table` for a table, `chart` for a chart, else `shape`; one that
        fails adds nothing."""
        stack = list(reversed(top))
        while stack:
            el = stack.pop()
            tag = el.tag
            if not isinstance(tag, str) or tag in self.prune or tag in GHOST or tag == ATTACHMENTS or _ref(el):
                continue
            if tag in INLINE_BLOCKS or tag == P:
                got = []
                kind = "image" if tag in MEDIA else "table" if tag in TABLES else "chart" if tag == CHART else "shape"
                with self.ctx.guard(kind):
                    self.walk(el, got, inside=False)
                    out.extend(got)
                continue
            stack.extend(reversed(el))

    def _leftovers(self):
        """The footnotes no mark names and the comments no field names, kept as unmarked notes."""
        for blocks in self._fn_blocks[self._fn_used:]:
            if blocks:
                self.notes[self._new("fn")] = blocks
        for ann in self.loose:
            if id(ann) not in self._used:
                with self.ctx.guard("note"):
                    blocks = self._comment_blocks(ann)
                    if blocks:
                        self.notes[self._new("cm")] = blocks

    # ---- Pages
    def _read_footnotes(self):
        """The footnotes, in order, from each sf:footnotes storage (`_footnote_storage`)."""
        self._noted += 1
        try:
            for el in self.footnotes:
                with self.ctx.guard("note"):
                    self._footnote_storage(el)
        finally:
            self._noted -= 1

    def _footnote_storage(self, el):
        """A footnote mark opens a footnote and a footnote break closes it, inside one paragraph too. In a storage
        with no marks, content after a close (or at the start) opens one. Content outside a footnote joins the last
        one, or the first one when it comes before it, so no blank or stray paragraph shifts the pairing."""
        fns, marked = self._fn_blocks, next(el.iter(FOOTNOTE_MARK), None) is not None
        cur, lead = None, []

        def into() -> list:
            nonlocal cur
            if cur is None and not marked:
                cur = []
                fns.append(cur)
            return cur if cur is not None else fns[-1] if fns else lead

        stack = list(reversed(el))
        while stack:
            node = stack.pop()
            tag = node.tag
            if not isinstance(tag, str) or tag in GHOST or tag == ATTACHMENTS or _ref(node):
                continue
            if tag in MEDIA or tag in TABLES or tag in STICKY or tag == CHART:  # a drawable between paragraphs
                self.walk(node, into(), inside=False)
                continue
            if tag != P:
                stack.extend(reversed(node))
                continue
            segs, after = self._segments(node)
            start = k = 0
            for i in [i for i, s in enumerate(segs) if s[0] in (_MARK, _FNBR)] + [len(segs)]:
                end = i + 1 if i < len(segs) and segs[i][0] == _FNBR else i
                block = self._block(node, segs[start:end])
                if block is not None:
                    into().append(block)
                while k < len(after) and (i == len(segs) or after[k][0] <= end):
                    self.walk(after[k][1], into(), inside=False)
                    k += 1
                if i == len(segs):
                    break
                if segs[i][0] == _MARK:
                    cur, lead = (lead or []), []
                    fns.append(cur)
                else:
                    cur = None
                start = end
        if lead:  # a storage whose only marks were never reached
            fns.append(lead)

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
            self._drawables(el, doc.blocks)
        for a in self.attachments:  # the ones no attachment-ref placed, last
            if id(a) not in self.placed and id(a) not in self.entered:
                with self.ctx.guard("object"):
                    self.walk(a, doc.blocks, inside=False)
        self._furniture = True
        try:
            for elements, lines in ((self.headers, doc.headers), (self.footers, doc.footers)):
                for el in elements:
                    with self.ctx.guard("header" if lines is doc.headers else "footer"):
                        lines.extend(plain(self._noted_walk(el)))
        finally:
            self._furniture = False
        self._leftovers()
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
            sec.title = self._title(title, sec)  # before the body, so a field it shares with the title shows no mark
            for c in kids:
                if c.tag == BODY_PH:
                    self.walk(c, sec.blocks)
            for c in kids:
                if c.tag == PAGE:
                    self._drawables(c, sec.blocks)
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
        self._leftovers()
        for a in self.attachments:  # no ref placed it: a slide has no place for it
            if id(a) not in self.placed and id(a) not in self.entered:
                self.ctx.skipped["object"] += 1
        return self.doc

    # ---- Numbers
    def _workspaces(self) -> list:
        """The ls:workspace elements in document order; one is not searched for more, nor are the stylesheets."""
        found, stack = [], [self.root]
        while stack:
            el = stack.pop()
            tag = el.tag
            if not isinstance(tag, str) or tag in STYLESHEETS:
                continue
            if tag == WORKSPACE:
                found.append(el)
                continue
            stack.extend(reversed(el))
        return found

    def numbers(self) -> Doc:
        """Each ls:workspace, under `ctx.guard("sheet")`, as `Section("sheet", ls:workspace-name)`: its tables,
        pictures and text boxes in order, each under its own guard (`_drawables`), and its sticky notes as "Comment:
        …" lines in `Section.comments`. A sheet that fails counts `sheet` and adds nothing. The template's parts
        outside the workspaces are never read."""
        for ws in self._workspaces():
            with self.ctx.guard("sheet"):
                sec = Section("sheet", (ws.get(WORKSPACE_NAME) or "").strip())
                self.sink = sec.comments
                try:
                    self._drawables(ws, sec.blocks)
                finally:
                    self.sink = None
                self.doc.blocks.append(sec)
        self._leftovers()
        for a in self.attachments:  # no ref placed it: a sheet has no place for it
            if id(a) not in self.placed and id(a) not in self.entered:
                self.ctx.skipped["object"] += 1
        return self.doc


# app -> the Reader method that walks it
WALKS = {"pages": Reader.pages, "key": Reader.keynote, "numbers": Reader.numbers}


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
