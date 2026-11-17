"""The one Markdown renderer: every output format and token rule lives here."""
import collections
import re
import secrets
from bisect import bisect_left, bisect_right
from pathlib import Path

from . import limits
from .images import Assets, sniff
from .limits import ReadError
from .model import (
    Bold, Break, Code, Doc, Equation, Heading, Hidden, Image, Link, ListItem, Math,
    NoteRef, Para, Quote, Section, Table, Text,
)

_PAGE_ONLY = re.compile(r"^[\s\-–—]*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*(쪽)?[\s\-–—]*$", re.I)
_LINE_START = re.compile(r"^( {0,3})(#|>|[-*+] |\d+\. )")  # up to three spaces may come before a block's mark
_NEWLINE = re.compile(r"\r\n?|\n")  # what ends a markdown line (_normalize turns a CR into LF)
_TITLE_BREAK = re.compile(r"[\r\n\t]")  # a heading title's line breaks (and tabs) become spaces...
_TITLE_DROP = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")  # ...and its other controls go
_HIDDEN_LINE = re.compile(r"^\((\d+) hidden places left out\)$")
_TRUNC = "(output truncated at 20 MB)"
# with a random tail drawn per render (_Render.head_mark), starts each page head while the document renders,
# so render() knows the lines it wrote (stats counts those alone, so a "## Page N" line inside a comment is
# not a page); removed before the text is returned. The tail keeps the document's own text from matching it:
# a U+10FFFD there stays as it is and never passes for a head
_HEAD_MARK = "\U0010fffd"
_PAGE_HEAD = re.compile(r"^## Page (\d+)(?::|$)")

# Skip kinds the parsers count in ctx.skipped that mean content is missing from the output, in the order the
# "(could not read: ...)" line lists them: (kinds, singular, plural). Kinds that share words are added up.
LOST = (
    (("section",), "section", "sections"),
    (("chapter", "spine"), "chapter", "chapters"),
    (("slide",), "slide", "slides"),
    (("sheet",), "sheet", "sheets"),
    (("page",), "page", "pages"),
    (("page_cut",), "page past the time or page limit", "pages past the time or page limit"),
    (("page_part",), "page read in part", "pages read in part"),
    (("scan",), "scanned page not shown", "scanned pages not shown"),
    (("textless",), "page with no text layer", "pages with no text layer"),
    (("figure",), "vector figure not shown", "vector figures not shown"),
    (("field",), "form field", "form fields"),
    (("annotation",), "annotation", "annotations"),
    (("document",), "document part", "document parts"),
    (("master",), "master slide", "master slides"),
    (("master_page",), "master page", "master pages"),
    (("notes",), "notes page", "notes pages"),
    (("header",), "header", "headers"),
    (("footer",), "footer", "footers"),
    (("footnotes",), "footnotes part", "footnotes parts"),
    (("comments",), "comments part", "comments parts"),
    (("comments_cut",), "comment list cut short", "comment lists cut short"),
    (("note",), "note", "notes"),
    (("block",), "text block", "text blocks"),
    (("shape", "alternate"), "shape", "shapes"),
    (("stencil",), "master shape", "master shapes"),
    (("image",), "image", "images"),
    (("chart",), "chart", "charts"),
    (("table",), "table", "tables"),
    (("db_table",), "database table", "database tables"),
    (("query",), "query not rebuilt as SQL", "queries not rebuilt as SQL"),
    (("withheld",), "query with connection details withheld", "queries with connection details withheld"),
    (("smartart",), "SmartArt diagram", "SmartArt diagrams"),
    (("smartart_cut",), "SmartArt diagram cut short", "SmartArt diagrams cut short"),
    (("object",), "embedded object", "embedded objects"),
    (("link",), "link", "links"),
    (("links_cut",), "hyperlink list cut short", "hyperlink lists cut short"),
    (("connections_cut",), "connection list cut short", "connection lists cut short"),
    (("glyphs",), "text run without Unicode", "text runs without Unicode"),
    (("text",), "text run cut short", "text runs cut short"),
    (("truncated", "node", "parse"), "chapter cut short", "chapters cut short"),
    (("sheet_cut",), "sheet cut short", "sheets cut short"),
    (("chart_cut",), "chart cut short", "charts cut short"),
    (("cells",), "table cut short", "tables cut short"),
    (("repeat",), "repeated row or cell cut short", "repeated rows or cells cut short"),
    (("record",), "damaged record", "damaged records"),
    (("depth",), "place nested too deeply", "places nested too deeply"),
)
# Named items: reported after the counted ones, by name only (a flag, not a count).
NAMED = (("vba", "VBA macros"), ("xfa", "XFA form"), ("attachment", "attached files"),
         ("kf8_fallback", "KF8 layout (the MOBI 6 copy was read)"), ("plain_text", "book markup (read as plain text)"),
         ("page_images", "page images (text layer only)"), ("linked_data", "linked tables' data (in other files)"))
# counted too, but they only change formatting or reading order, so they are not reported
INTERNAL = frozenset({"styles", "numbering", "rels", "manifest", "css", "encryption", "transform", "story",
                      "char_positions", "attribute", "merge", "duplicate", "jet_page8"})  # char_positions: pdf glyph positions dropped, the text kept


def lost(skipped) -> list:
    """The counted items of unread(): content that was in the file and is missing from the output."""
    out = []
    for kinds, one, many in LOST:
        n = sum(max(0, skipped.get(k, 0)) for k in kinds)
        if n:
            out.append(f"{n} {one if n == 1 else many}")
    return out


def unread(skipped) -> list:
    """Plain-word counts of what the parsers could not read, e.g. ["1 slide", "2 images"], then the named items."""
    return lost(skipped) + [name for k, name in NAMED if skipped.get(k, 0) > 0]


class _Mode:
    """Where inline text lands: block, table cell (pipe or HTML), or heading."""

    def __init__(self, br="\n", kind="", plain=False):
        self.br, self.kind, self.plain = br, kind, plain

    def esc(self, s):
        if self.kind == "pipe":
            s = s.replace("|", "\\|")
        elif self.kind == "html":
            s = s.replace("&", "&amp;").replace("<", "&lt;")
        if self.kind:
            s = _NEWLINE.sub(self.br, s)  # a CR as well: it would end the row on its way out (_normalize)
        return s


BLOCK = _Mode()
HEAD = _Mode(br=" ", plain=True)
PIPE = _Mode("<br>", "pipe")
HTML = _Mode("<br>", "html")
LINES = _Mode()


def one_line(s: str) -> str:
    """A heading title on one line: its line breaks become spaces, other C0 and C1 controls and U+2028/U+2029 go, and
    runs of whitespace are squashed, so no title can end its heading's line and start a markdown line of its own."""
    return " ".join(_TITLE_DROP.sub("", _TITLE_BREAK.sub(" ", s)).split())


def _esc_start(mo):
    lead, mark = mo.group(1), mo.group(2)
    if mark[0].isdigit():
        return lead + mark[:-2] + "\\. "
    return lead + "\\" + mark


def _esc_lines(s: str, skip_first: bool = False) -> str:
    """s with a backslash before the mark of each line that would start a markdown block (_LINE_START), a CR ending a
    line as LF does; `skip_first` leaves the first line as it is, when it follows the block's own mark (a list
    item's marker, "> Notes: ", a footnote's "[^n]: ")."""
    lines = _NEWLINE.split(s)
    for i in range(1 if skip_first else 0, len(lines)):
        lines[i] = _LINE_START.sub(_esc_start, lines[i])
    return "\n".join(lines)


def _plain(s):
    return re.sub(r"\s+", " ", s).strip().casefold()


def _inl_len(inl) -> int:
    n = 0
    for it in inl:
        if hasattr(it, "text"):
            n += len(it.text)
        elif hasattr(it, "inlines"):
            n += _inl_len(it.inlines)
    return n


def blocks_len(blocks, d=0) -> int:
    """Rough characters a block list renders to; only used to stop building a table past the cap."""
    n = 0
    for b in blocks:
        if hasattr(b, "inlines"):
            n += _inl_len(b.inlines)
        elif d < 8 and hasattr(b, "blocks"):
            n += blocks_len(b.blocks, d + 1)
        elif d < 8 and hasattr(b, "rows"):
            n += sum(blocks_len(c.blocks, d + 1) for row in b.rows for c in row)
    return n


class _Render:
    def __init__(self, doc: Doc, assets_dir: Path, show_hidden: bool, skipped=None, drop=frozenset()):
        self.doc, self.assets_dir, self.show = doc, assets_dir, show_hidden
        self.drop = drop  # ids of hidden-only notes whose marks are left out with their definitions
        self.marked: set[str] = set()  # ids of notes whose mark was shown
        self.hidden_marks: set[str] = set()  # ids of notes whose mark was left out as hidden (and counted)
        self.dangling: set[str] = set()  # ids of hidden-only notes whose mark was shown: render() renders again
        self.unread = unread(skipped or {})
        self.lost = lost(skipped or {})
        self.empty = True  # nothing but status lines was rendered
        self.hidden_count = 0
        self.page_heads = 0  # page heads written, each starting with head_mark
        self.head_mark = _HEAD_MARK + secrets.token_hex(8) + _HEAD_MARK
        self.depth = 0
        self.too_deep = 0  # subtrees left out for nesting past MAX_DEPTH
        self.note_no: dict[str, int] = {}
        self.note_order: list[str] = []  # the ids of note_no in numbering order
        self.assets = Assets(assets_dir)
        self.unviewable: list[str] = []
        self.linked: set[str] = set()  # viewable images the output links to
        self.size = 0  # characters emitted so far by leaf blocks; building stops past MAX_OUTPUT
        self.memo = {}  # id(blocks) -> (has text, levels its scan went below the call, blocks): see _has_text
        self.reach = 0  # the deepest level the has-text scan under way has called _enter at

    # ---- hidden handling
    def _skip(self, node) -> bool:
        """True when a hidden node is to be left out (and counted)."""
        if node.hidden and not self.show:
            self.hidden_count += 1
            if self.doc.notes:
                self._hidden_marks(node)
            return True
        return False

    def _enter(self) -> bool:
        """Step one level in; False (counted, nothing to undo) when past MAX_DEPTH."""
        if self.depth >= limits.MAX_DEPTH:
            self.too_deep += 1
            return False
        self.depth += 1
        return True

    # ---- inlines
    def inlines(self, items, m=BLOCK) -> str:
        parts = []  # str or list-of-inlines for a bold run
        for it in items:
            if isinstance(it, Hidden) or it.hidden:
                if not self.show:
                    self.hidden_count += 1
                    if self.doc.notes:
                        self._hidden_marks(it)
                    continue
                inner = self._one(it, m, as_hidden=True)
                parts.append(f"(hidden: {inner})")
            elif isinstance(it, Bold) and not m.plain:
                if parts and isinstance(parts[-1], list):
                    parts[-1].extend(it.inlines)
                else:
                    parts.append(list(it.inlines))
            else:
                parts.append(self._one(it, m))
        return "".join(self._bold(p, m) if isinstance(p, list) else p for p in parts)  # joined once: linear

    def _bold(self, items, m):
        inner = self.inlines(items, m)
        core = inner.strip()
        if not core:
            return inner
        lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
        return f"{lead}**{core}**{trail}"

    def _one(self, it, m, as_hidden=False) -> str:
        if isinstance(it, Text):
            return m.esc(it.text)
        if isinstance(it, Hidden):
            return self.inlines(it.inlines, m) if as_hidden else ""
        if isinstance(it, Bold):
            inner = self.inlines(it.inlines, m)
            return inner if m.plain else self._bold(it.inlines, m)
        if isinstance(it, Link):
            label = self.inlines(it.inlines, m)
            url = m.esc(it.url)
            return url if label == url or not label.strip() else f"[{label}]({url})"
        if isinstance(it, Break):
            return m.br
        if isinstance(it, Math):
            return m.esc(f"${it.tex}$")
        if isinstance(it, NoteRef):
            if it.id in self.drop:
                return ""
            self.marked.add(it.id)
            return f"[^{self._note_number(it.id)}]"
        return ""

    def _hidden_marks(self, node):
        """Note the ids of the marks inside what is left out as hidden (an inline, a block, a section, the cells of a
        hidden row or column), so their notes are not counted twice. Iterative: the subtree is walked once."""
        stack = [node]
        while stack:
            x = stack.pop()
            if isinstance(x, NoteRef):
                self.hidden_marks.add(x.id)
                continue
            for name in ("inlines", "blocks", "notes"):
                stack.extend(getattr(x, name, ()))
            for row in getattr(x, "rows", ()):
                stack.extend(row)

    def _hidden_row_marks(self, t: Table, r: int):
        """The marks in the cells a hidden row `r` starts: grid row r is `t.rows[r]`, as `_grid` numbers it."""
        if self.doc.notes and 0 <= r < len(t.rows):
            for cell in t.rows[r]:
                self._hidden_marks(cell)

    def _note_number(self, nid):
        if nid not in self.note_no:
            self.note_no[nid] = len(self.note_no) + 1
            self.note_order.append(nid)
        return self.note_no[nid]

    # ---- blocks
    def blocks(self, blocks) -> list:
        """Rendered pieces as (kind, text); kind 'li' pieces stay tight."""
        if not self._enter():
            return []
        try:
            out = []
            for b in blocks:
                if self.size > limits.MAX_OUTPUT:
                    break
                if self._skip(b):
                    if isinstance(b, Section) and b.kind == "sheet":  # a hidden sheet is named, not read
                        out.append(("h", f"## Sheet: {one_line(b.title)} (hidden, left out)"))
                    continue
                before = self.size
                got = self._block(b)
                if self.size == before:  # nothing nested was counted: count this block itself
                    self.size += sum(len(s) + 2 for _, s in got)
                out.extend(got)
            return out
        finally:
            self.depth -= 1

    @staticmethod
    def join(pieces) -> str:
        """The pieces with one blank line between them, none between list items; collected and joined once, since
        `out += s` copies the text so far on each piece (quadratic past a few MB)."""
        out = []
        prev = None
        for kind, s in pieces:
            if prev is not None:
                out.append("\n" if prev == kind == "li" else "\n\n")
            out.append(s)
            prev = kind
        return "".join(out)

    def _tag(self, b, s):
        return f"(hidden) {s}" if b.hidden and self.show else s

    def _block(self, b) -> list:
        if isinstance(b, Para):
            s = self.inlines(b.inlines)
            if not s.strip():
                return []
            return [("p", self._tag(b, _esc_lines(s)))]
        if isinstance(b, Heading):
            s = one_line(self.inlines(b.inlines, HEAD))
            if not s:
                return []
            return [("h", self._tag(b, "#" * min(6, max(1, b.level)) + " " + s))]
        if isinstance(b, ListItem):
            s = self.inlines(b.inlines)
            if not s.strip():
                return []
            return [("li", "  " * max(0, b.level) + b.marker + " " + self._tag(b, _esc_lines(s, skip_first=True)))]
        if isinstance(b, Table):
            return self._table(b)
        if isinstance(b, Image):
            return self._image(b)
        if isinstance(b, Equation):
            s = f"$${b.tex}$$" if b.display else f"${b.tex}$"
            return [("p", self._tag(b, _esc_lines(s, True)))]
        if isinstance(b, Quote):
            inner = self.join(self.blocks(b.blocks))
            if not inner:
                return []
            return [("q", self._tag(b, "\n".join(("> " + ln).rstrip() for ln in inner.split("\n"))))]
        if isinstance(b, Code):
            if not b.text.strip():
                return []
            runs = re.findall(r"`+", b.text)
            fence = "`" * max(3, max(map(len, runs), default=0) + 1)
            lang = re.sub(r"[`\r\n]", "", b.lang)
            return [("c", ("(hidden)\n" if b.hidden and self.show else "") + f"{fence}{lang}\n{b.text}\n{fence}")]
        if isinstance(b, Section):
            return self._section(b)
        return []

    def _image_text(self, b: Image, m=BLOCK) -> str:
        path, ok = self.assets.add(b.data, b.alt)
        alt = re.sub(r"\s+", " ", b.alt).strip()
        if ok:
            self.linked.add(path)
            alt = alt.replace("\\", "\\\\").replace("]", "\\]")
            return m.esc(f"![{alt}](") + path + ")"
        if path and path not in self.unviewable:
            self.unviewable.append(path)
        return m.esc(f"[image: {alt}]") if alt else ""

    def _image(self, b: Image) -> list:
        s = self._image_text(b)
        return [("p", self._tag(b, s))] if s else []

    def _section(self, s: Section) -> list:
        out = []
        tag = " (hidden)" if s.hidden else ""
        head = None
        title = one_line(s.title)  # every reader's title is cut to its head's one line here, once
        if s.kind == "slide":
            head = "## Slide" + (f" {s.number}" if s.number is not None else "") + (f": {title}" if title else "")
        elif s.kind == "sheet":
            head = f"## Sheet: {title}"
        elif s.kind == "table":
            head = f"## Table: {title}"
        elif s.kind == "query":
            head = f"## Query: {title}"
        elif s.kind == "master":
            head = f"## Master page: {title}"
        elif s.kind == "page" and s.number is not None:
            head = f"## Page {s.number}" + (f": {title}" if title else "")
        elif s.kind == "page" and title:
            head = f"## Page: {title}"
        if head and s.kind == "page":
            self.page_heads += 1
            out.append(("h", self.head_mark + head + tag))
        elif head:
            out.append(("h", head + tag))
        out.extend(self.blocks(s.blocks))
        # each entry follows its own "> " mark: its later lines are escaped
        notes = [_esc_lines(t, True) for t in (self._flat(n, LINES) for n in s.notes if not self._skip(n)) if t]
        comments = [_esc_lines(t, True) for t in (self._flat(c, LINES) for c in s.comments if not self._skip(c)) if t]
        if notes or comments:
            lines = (*(("> Notes: " + notes[0], *("> " + n for n in notes[1:])) if notes else ()),
                     *("> " + c for c in comments))
            out.append(("q", "\n".join(lines)))
        return out

    # ---- tables
    def _flat(self, b, m) -> str:
        """A block as one run of text for a table cell or note."""
        if b.hidden and not self.show:
            self.hidden_count += 1
            if self.doc.notes:
                self._hidden_marks(b)
            return ""
        if not self._enter():
            return ""
        try:
            if isinstance(b, (Para, ListItem)):
                s = self.inlines(b.inlines, m)
                if isinstance(b, ListItem):
                    s = b.marker + " " + s
            elif isinstance(b, Heading):
                s = self.inlines(b.inlines, _Mode(" ", m.kind, True))
            elif isinstance(b, Image):
                s = self._image_text(b, m)
            elif isinstance(b, Equation):
                s = m.esc(f"${b.tex}$")
            elif isinstance(b, Code):
                s = m.esc(b.text)
            elif isinstance(b, Quote):
                s = m.br.join(x for x in (self._flat(x, m) for x in b.blocks) if x)
            elif isinstance(b, Section):
                s = m.br.join(x for x in (self._flat(x, m) for x in b.blocks) if x)
            elif isinstance(b, Table):
                s = self._nested(b, m)
            else:
                s = ""
        finally:
            self.depth -= 1
        s = s.strip()
        return f"(hidden) {s}" if s and b.hidden else s

    def _cell_text(self, cell, m) -> str:
        return m.br.join(x for x in (self._flat(b, m) for b in cell.blocks) if x)

    def _has_text(self, blocks) -> bool:
        """Whether blocks would render to something, decided without rendering. The answer is kept for the whole
        render by identity (the entry holds the list, so its id is never reused) with how many levels below the call
        its scan went: a call deep enough for that scan to pass MAX_DEPTH scans again, and a scan the cap cut is not
        kept, so every answer and too_deep count is a fresh scan's. A chain of one-cell tables is scanned once, not
        once a table."""
        d = self.depth
        hit = self.memo.get(id(blocks))
        if hit is not None and d + hit[1] < limits.MAX_DEPTH:
            self.reach = max(self.reach, d + hit[1])
            return hit[0]
        outer, self.reach = self.reach, d
        found = False
        if self._enter():
            try:
                found = any(self._block_has_text(b) for b in blocks)
            finally:
                self.depth -= 1
            if self.reach < limits.MAX_DEPTH:
                self.memo[id(blocks)] = (found, self.reach - d, blocks)
        self.reach = max(outer, self.reach)
        return found

    def _inl_has_text(self, items) -> bool:
        for it in items:
            if isinstance(it, Hidden) or it.hidden:
                if not self.show:
                    continue
            if isinstance(it, Text):
                if it.text.strip():
                    return True
            elif isinstance(it, (Bold, Hidden)):
                if self._inl_has_text(it.inlines):
                    return True
            elif isinstance(it, Link):
                if it.url.strip() or self._inl_has_text(it.inlines):
                    return True
            elif isinstance(it, (NoteRef, Math)):
                return True
        return False

    def _block_has_text(self, b) -> bool:
        if b.hidden and not self.show:
            return False
        if isinstance(b, (Para, ListItem, Heading)):
            return self._inl_has_text(b.inlines) or isinstance(b, ListItem)
        if isinstance(b, Image):
            return bool(b.alt.strip()) or (len(b.data) >= 16 and sniff(b.data) is not None)
        if isinstance(b, Equation):
            return True
        if isinstance(b, Code):
            return bool(b.text.strip())
        if isinstance(b, (Quote, Section)):
            return self._has_text(b.blocks)
        if isinstance(b, Table):
            if b.hidden_cols and not self.show:  # a cell's column is known only on the laid-out grid
                origins = self._grid(b, False)[0]
                return any(self._has_text(cell.blocks) for (r, _), (cell, _, _) in origins.items()
                           if r not in b.hidden_rows)
            return any(self._has_text(c.blocks) for r, row in enumerate(b.rows)
                       if self.show or r not in b.hidden_rows for c in row)
        return False

    def _grid(self, t: Table, count=True):
        """Origin cells by (row, col) on the reconstructed grid, trimmed: (origins, nrows, ncols).
        Unless hidden content is shown, hidden columns are left out here: a span loses the ones it covers,
        a cell whose columns are all hidden is dropped, and each hidden column with such text counts one place."""
        covered = set()
        origins = {}
        texty = {}  # id(blocks) -> has text; a cell shared by many slots is scanned once
        widest = max((len(row) for row in t.rows), default=0)
        spanned = max((sum(min(max(1, x.colspan), limits.GRID_COLS) for x in row) for row in t.rows), default=0)
        width = max(widest, min(spanned, limits.GRID_COLS), 1)
        slots = 0
        sizes, est = {}, 0  # est: text the origins so far will render; past the cap more rows are pointless
        for r, row in enumerate(t.rows):
            if self.size + est > limits.MAX_OUTPUT:
                break
            c = 0
            for cell in row:
                k = id(cell.blocks)
                if k not in sizes:
                    sizes[k] = blocks_len(cell.blocks)
                est += sizes[k]
                while (r, c) in covered:
                    c += 1
                rs = min(max(1, cell.rowspan), len(t.rows) - r)
                cs = min(max(1, cell.colspan), width)
                if slots + rs * cs > limits.GRID_SLOTS:
                    rs = cs = 1
                slots += rs * cs
                origins[(r, c)] = (cell, rs, cs)
                for dr in range(rs):
                    for dc in range(cs):
                        covered.add((r + dr, c + dc))
                c += cs
        hc = sorted(x for x in t.hidden_cols if 0 <= x < width) if t.hidden_cols and not self.show else ()
        if hc:
            mapped, lost = {}, set()
            for (r, c), (cell, rs, cs) in origins.items():
                lo, hi = bisect_left(hc, c), bisect_right(hc, c + cs - 1)
                n = hi - lo
                if n < cs:
                    mapped[(r, c - lo)] = (cell, rs, cs - n)
                    if n == 0:
                        continue
                    cols = hc[lo:hi]  # the hidden columns a wider span holds: its text is partly left out
                else:
                    cols = range(c, c + cs)
                k = id(cell.blocks)
                if k not in texty:
                    texty[k] = self._has_text(cell.blocks)
                if texty[k] and r not in t.hidden_rows:
                    lost.update(cols)
                if n >= cs and self.doc.notes:  # the cell is dropped whole: its marks are left out as hidden
                    self._hidden_marks(cell)
            origins = mapped
            if count:
                self.hidden_count += len(lost)
        nrows = ncols = 0
        for (r, c), (cell, rs, cs) in origins.items():
            k = id(cell.blocks)
            if k not in texty:
                texty[k] = self._has_text(cell.blocks)
            if texty[k]:
                nrows = max(nrows, r + rs)
                ncols = max(ncols, c + cs)
        kept = {k: v for k, v in origins.items() if k[0] < nrows and k[1] < ncols}
        return kept, nrows, ncols

    def _nested(self, t: Table, m) -> str:
        origins, nrows, _ = self._grid(t)
        hcs, marked = (t.hidden_cols if self.show else ()), set()
        rows, used = [], 0
        for r in range(nrows):
            if self.size + used > limits.MAX_OUTPUT:
                break
            if r in t.hidden_rows and not self.show:  # before the cells are built, so their images are not written
                self.hidden_count += 1
                self._hidden_row_marks(t, r)
                continue
            cells = []
            for k in sorted(origins):
                if k[0] == r:
                    x = self._cell_text(origins[k][0], m)
                    if x and k[1] in hcs and k[1] not in marked:
                        marked.add(k[1])
                        x = "(hidden) " + x
                    cells.append(x)
            cells = [x for x in cells if x]
            used += sum(len(x) + 3 for x in cells)
            if r in t.hidden_rows and cells and not cells[0].startswith("(hidden) "):
                cells[0] = "(hidden) " + cells[0]
            if cells:
                rows.append(" / ".join(cells))
        return " ; ".join(rows)

    def _table(self, t: Table) -> list:
        origins, nrows, ncols = self._grid(t)
        cap = [t.caption] if t.caption else []
        hcs, marked = (t.hidden_cols if self.show else ()), set()

        def mark(c, s):  # a hidden column shown: its first non-empty cell says so
            if s and c in hcs and c not in marked:
                marked.add(c)
                return "(hidden) " + s
            return s

        def hid(r):
            if r in t.hidden_rows:
                if not self.show:
                    self.hidden_count += 1
                    self._hidden_row_marks(t, r)
                    return None
                return True
            return False

        def finish(body):
            if not body:
                return [("p", c) for c in cap]
            text = "\n".join(cap + [body])
            if t.hidden and self.show:
                text = "(hidden)\n" + text
            return [("t", text)]

        if not nrows:
            return finish("")
        if nrows == 1 and ncols == 1:
            cell = origins[(0, 0)][0]
            h = hid(0)
            if h is None:
                return finish("")
            out = self.blocks(cell.blocks)
            if out:
                s = mark(0, out[0][1])
                if h and not s.startswith("(hidden) "):
                    s = "(hidden) " + s
                out[0] = (out[0][0], s)
            return ([("p", c) for c in cap]) + out
        if ncols == 1:
            lines, used = [], 0
            for r in range(nrows):
                if self.size + used > limits.MAX_OUTPUT:
                    break
                if (r, 0) not in origins:
                    continue
                h = hid(r)
                if h is None:
                    continue
                s = mark(0, self._cell_text(origins[(r, 0)][0], LINES))
                if s:
                    lines.append(("(hidden) " if h and not s.startswith("(hidden) ") else "") + s)
                    used += len(lines[-1]) + 1
            return finish("\n".join(lines))
        spans = any(rs > 1 or cs > 1 for _, rs, cs in origins.values())
        m = HTML if spans else PIPE
        lines = []
        shown = 0
        used = 0  # a table stops growing once the document would pass the output cap
        for r in range(nrows):
            if self.size + used > limits.MAX_OUTPUT:
                break
            h = hid(r)
            if h is None:
                continue
            cells, row_used = [], 0
            for c in range(ncols):
                if self.size + used + row_used > limits.MAX_OUTPUT:
                    break  # the row is cut here; the output is truncated anyway
                if (r, c) not in origins:
                    if not spans:
                        cells.append(("", None))
                    continue
                cell, rs, cs = origins[(r, c)]
                s = mark(c, self._cell_text(cell, m))
                row_used += len(s) + 1
                cells.append((s, (cell, rs, cs)))
            if h and cells and not cells[0][0].startswith("(hidden) "):
                cells[0] = ("(hidden) " + cells[0][0], cells[0][1])
            before = len(lines)
            if spans:
                tds = []
                for s, info in cells:
                    cell, rs, cs = info
                    tag = "th" if cell.header else "td"
                    attrs = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
                    tds.append(f"<{tag}{attrs}>{s}</{tag}>")
                lines.append(f"<tr>{''.join(tds)}</tr>")
            else:
                lines.append("|" + "|".join(s for s, _ in cells) + "|")
                if shown == 0:
                    lines.append("|" + "|".join("---" for _ in range(ncols)) + "|")
            shown += 1
            used += sum(len(x) + 1 for x in lines[before:])
        if spans:
            lines = ["<table>", *lines, "</table>"] if lines else []
        return finish("\n".join(lines))

    # ---- document
    def body(self) -> str:
        pieces = self.blocks(self.doc.blocks)
        text = self.join(pieces)
        notes = self._note_defs()
        if notes:
            text += ("\n\n" if text else "") + notes
        top = []
        hf = []
        for label, items in (("Header", self.doc.headers), ("Footer", self.doc.footers)):
            seen, kept = set(), []
            for it in items:
                it = re.sub(r"\s+", " ", it).strip()
                if it and it not in seen and not _PAGE_ONLY.match(it):
                    seen.add(it)
                    kept.append(it)
            if kept:
                hf.append(f"{label}: " + " | ".join(kept))
        self.empty = not text and not hf and not self.unviewable
        title = one_line(self.doc.title)
        if title:
            first = pieces[0][1].replace(self.head_mark, "") if pieces else ""
            first = _plain(re.sub(r"^#+\s*", "", first).replace("**", ""))
            if first != _plain(title):
                top.append("# " + title)
        status = []
        if self.hidden_count:
            status.append(f"({self.hidden_count} hidden places left out)")
        if self.too_deep:
            status.append(f"({self.too_deep} places nested too deeply left out)")
        if self.unread:
            status.append("(could not read: " + ", ".join(self.unread) + ")")
        if hf:
            status.append("\n".join(hf))
        top = status + top
        if self.unviewable:
            tail = "Not viewable here: " + ", ".join(self.unviewable)
            return "\n\n".join(top + ([text] if text else []) + [tail])
        return "\n\n".join(top + ([text] if text else []))

    def _note_defs(self) -> str:
        notes = self.doc.notes
        lines = []
        i = 0
        referenced_done = False
        while True:
            while i < len(self.note_order):  # rendering a note may reference more notes
                nid = self.note_order[i]
                i += 1
                if nid in notes:
                    left_out = self.hidden_count
                    s = " ".join(x for x in (self._flat(b, LINES) for b in notes[nid]) if x)
                    if not s and self.hidden_count > left_out:
                        # a note whose whole text is hidden has no definition to show, and no mark: one shown is
                        # left out by a second render (render); one left out as hidden already counted the note
                        if nid in self.marked:
                            self.dangling.add(nid)
                        if nid in self.hidden_marks:
                            self.hidden_count = left_out
                        continue
                    lines.append(_esc_lines(f"[^{self.note_no[nid]}]: {s}".rstrip(), True))
            if referenced_done:
                break
            for nid in notes:  # unreferenced ones follow, in insertion order
                self._note_number(nid)
            referenced_done = True
        return "\n".join(lines)


def _normalize(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+$", "", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s).strip("\n")
    return s + "\n" if s else ""


def _cap(s: str) -> str:
    limit = limits.MAX_OUTPUT
    if len(s.encode("utf-8")) <= limit:
        return s
    out, size = [], 0
    for line in s.split("\n"):
        raw = line.encode("utf-8")
        if size + len(raw) + 1 > limit:  # keep what fits of the line that crosses the cap
            part = raw[:max(0, limit - size - 1)].decode("utf-8", "ignore").rstrip()
            if part:
                out.append(part)
            break
        out.append(line)
        size += len(raw) + 1
    return "\n".join(out) + "\n" + _TRUNC + "\n"


def render(doc: Doc, assets_dir: Path, hidden: bool = False, skipped=None, info=None) -> str:
    """Render a Doc to Markdown. The Doc is not modified.

    `skipped` is the parse context's skip counter; what it says was lost becomes a "(could not read: ...)" line.
    A dict passed as `info` receives "unread" (that line's items), "lost" (the counted ones among them), "empty" (no content rendered at all),
    "images" (how many viewable images the output links to) and "pages" (the line numbers of the page heads it wrote, for stats; None
    when it wrote none, or a head did not survive to the output).
    """
    r = _Render(doc, Path(assets_dir), hidden, skipped)
    text = r.body()
    if r.dangling:  # a hidden-only note's mark was shown: render again with those marks left out (and unnumbered)
        drop = frozenset(r.dangling)
        del text, r  # the first render is not kept while the second runs
        r = _Render(doc, Path(assets_dir), hidden, skipped, drop=drop)
        text = r.body()
    text = _normalize(text)  # rebound, so the raw text is not held while _cap runs
    pages = None
    if r.page_heads:
        if text.count(r.head_mark) == r.page_heads:  # else a head was cut or rewritten on its way out
            pages = [i for i, ln in enumerate(text.splitlines(), 1) if ln.startswith(r.head_mark)]
        text = text.replace(r.head_mark, "")
    md = _cap(text)
    if info is not None:
        info.update(unread=r.unread, lost=r.lost, empty=r.empty and not r.hidden_count and not r.too_deep, images=len(r.linked),
                    pages=pages)
    return md


def stats(md: str, pages=None) -> dict:
    """Line count, hidden count, the heading index and the page heads [[page, line]] of rendered markdown. `pages` is the
    line numbers of the page heads render() wrote (its info["pages"]); without it every "## Page N" line counts."""
    lines = md.splitlines()
    n_hidden = 0
    if lines and (mo := _HIDDEN_LINE.match(lines[0])):
        n_hidden = int(mo.group(1))
    heads = [(i, len(mo.group(1)), mo.group(2).strip())
             for i, ln in enumerate(lines, 1) if (mo := re.match(r"^(#{1,6}) (.+)$", ln))]
    # every level down to the first one with 3 headings or more (all of them when none has), so a lone
    # "#" title does not leave a long document with a one-entry index
    count = collections.Counter(h[1] for h in heads)
    deepest = max(count, default=0)
    for lvl in sorted(count):
        if count[lvl] >= 3:
            deepest = lvl
            break
    heads = [(i, t) for i, lvl, t in heads if lvl <= deepest]
    if pages is None:
        pages = [[int(mo.group(1)), i] for i, ln in enumerate(lines, 1) if (mo := _PAGE_HEAD.match(ln))]
    else:
        pages = [[int(mo.group(1)), i] for i in pages if 0 < i <= len(lines) and (mo := _PAGE_HEAD.match(lines[i - 1]))]
    return {"lines": len(lines), "hidden": n_hidden, "headings": heads, "pages": pages}
