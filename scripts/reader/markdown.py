"""The one Markdown renderer: every output format and token rule lives here."""
import re
from pathlib import Path

from . import limits
from .images import Assets, sniff
from .limits import ReadError
from .model import (
    Bold, Break, Doc, Equation, Heading, Hidden, Image, Link, ListItem, Math,
    NoteRef, Para, Quote, Section, Table, Text,
)

_PAGE_ONLY = re.compile(r"^[\s\-–—]*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*(쪽)?[\s\-–—]*$", re.I)
_LINE_START = re.compile(r"^(#|>|[-*+] |\d+\. )")
_HIDDEN_LINE = re.compile(r"^\((\d+) hidden places left out\)$")
_TRUNC = "(output truncated at 20 MB)"

# Skip kinds the parsers count in ctx.skipped that mean content is missing from the output, in the order the
# "(could not read: ...)" line lists them: (kinds, singular, plural). Kinds that share words are added up.
LOST = (
    (("section",), "section", "sections"),
    (("chapter", "spine"), "chapter", "chapters"),
    (("slide",), "slide", "slides"),
    (("sheet",), "sheet", "sheets"),
    (("page",), "page", "pages"),
    (("document",), "document part", "document parts"),
    (("master",), "master slide", "master slides"),
    (("notes",), "notes page", "notes pages"),
    (("header",), "header", "headers"),
    (("footer",), "footer", "footers"),
    (("footnotes",), "footnotes part", "footnotes parts"),
    (("comments",), "comments part", "comments parts"),
    (("note",), "note", "notes"),
    (("block",), "text block", "text blocks"),
    (("shape", "alternate"), "shape", "shapes"),
    (("image",), "image", "images"),
    (("chart",), "chart", "charts"),
    (("object",), "embedded object", "embedded objects"),
    (("link",), "link", "links"),
    (("glyphs",), "text run without Unicode", "text runs without Unicode"),
    (("text",), "text run cut short", "text runs cut short"),
    (("truncated", "node", "parse"), "chapter cut short", "chapters cut short"),
    (("sheet_cut",), "sheet cut short", "sheets cut short"),
    (("cells",), "table cut short", "tables cut short"),
    (("repeat",), "repeated row or cell cut short", "repeated rows or cells cut short"),
    (("depth",), "place nested too deeply", "places nested too deeply"),
)
# counted too, but they only change formatting or reading order, so they are not reported
INTERNAL = frozenset({"styles", "numbering", "rels", "manifest", "css", "encryption", "transform", "story"})


def unread(skipped) -> list:
    """Plain-word counts of what the parsers could not read, e.g. ["1 slide", "2 images"]."""
    out = []
    for kinds, one, many in LOST:
        n = sum(max(0, skipped.get(k, 0)) for k in kinds)
        if n:
            out.append(f"{n} {one if n == 1 else many}")
    return out


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
            s = s.replace("\n", self.br)
        return s


BLOCK = _Mode()
HEAD = _Mode(br=" ", plain=True)
PIPE = _Mode("<br>", "pipe")
HTML = _Mode("<br>", "html")
LINES = _Mode()


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
    def __init__(self, doc: Doc, assets_dir: Path, show_hidden: bool, skipped=None):
        self.doc, self.assets_dir, self.show = doc, assets_dir, show_hidden
        self.unread = unread(skipped or {})
        self.empty = True  # nothing but status lines was rendered
        self.hidden_count = 0
        self.depth = 0
        self.too_deep = 0  # subtrees left out for nesting past MAX_DEPTH
        self.note_no: dict[str, int] = {}
        self.assets = Assets(assets_dir)
        self.unviewable: list[str] = []
        self.size = 0  # characters emitted so far by leaf blocks; building stops past MAX_OUTPUT

    # ---- hidden handling
    def _skip(self, node) -> bool:
        """True when a hidden node is to be left out (and counted)."""
        if node.hidden and not self.show:
            self.hidden_count += 1
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
        out = ""
        for p in parts:
            if isinstance(p, list):
                s = self._bold(p, m)
                out += s
            else:
                out += p
        return out

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
            return f"[^{self._note_number(it.id)}]"
        return ""

    def _note_number(self, nid):
        if nid not in self.note_no:
            self.note_no[nid] = len(self.note_no) + 1
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
                        out.append(("h", f"## Sheet: {b.title} (hidden, left out)"))
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
        out = ""
        prev = None
        for kind, s in pieces:
            if prev is not None:
                out += "\n" if prev == kind == "li" else "\n\n"
            out += s
            prev = kind
        return out

    def _tag(self, b, s):
        return f"(hidden) {s}" if b.hidden and self.show else s

    def _block(self, b) -> list:
        if isinstance(b, Para):
            s = self.inlines(b.inlines)
            if not s.strip():
                return []
            s = "\n".join(_LINE_START.sub(lambda mo: self._esc_start(mo.group(0)), ln) for ln in s.split("\n"))
            return [("p", self._tag(b, s))]
        if isinstance(b, Heading):
            s = self.inlines(b.inlines, HEAD).strip()
            if not s:
                return []
            return [("h", self._tag(b, "#" * min(6, max(1, b.level)) + " " + s))]
        if isinstance(b, ListItem):
            s = self.inlines(b.inlines)
            if not s.strip():
                return []
            return [("li", "  " * max(0, b.level) + b.marker + " " + self._tag(b, s))]
        if isinstance(b, Table):
            return self._table(b)
        if isinstance(b, Image):
            return self._image(b)
        if isinstance(b, Equation):
            s = f"$${b.tex}$$" if b.display else f"${b.tex}$"
            return [("p", self._tag(b, s))]
        if isinstance(b, Quote):
            inner = self.join(self.blocks(b.blocks))
            if not inner:
                return []
            return [("q", self._tag(b, "\n".join(("> " + ln).rstrip() for ln in inner.split("\n"))))]
        if isinstance(b, Section):
            return self._section(b)
        return []

    @staticmethod
    def _esc_start(s):
        if s[0].isdigit():
            return s[:-2] + "\\. "
        return "\\" + s

    def _image_text(self, b: Image, m=BLOCK) -> str:
        path, ok = self.assets.add(b.data, b.alt)
        alt = re.sub(r"\s+", " ", b.alt).strip()
        if ok:
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
        if s.kind == "slide":
            head = "## Slide" + (f" {s.number}" if s.number is not None else "") + (f": {s.title}" if s.title else "")
        elif s.kind == "sheet":
            head = f"## Sheet: {s.title}"
        elif s.kind == "page" and s.title:
            head = f"## Page: {s.title}"
        if head:
            out.append(("h", head + tag))
        out.extend(self.blocks(s.blocks))
        notes = [t for t in (self._flat(n, LINES) for n in s.notes if not self._skip(n)) if t]
        if notes:
            lines = ("> Notes: " + notes[0], *("> " + n for n in notes[1:]))
            out.append(("q", "\n".join(lines)))
        return out

    # ---- tables
    def _flat(self, b, m) -> str:
        """A block as one run of text for a table cell or note."""
        if b.hidden and not self.show:
            self.hidden_count += 1
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
        """Whether blocks would render to something, decided without rendering."""
        if not self._enter():
            return False
        try:
            return any(self._block_has_text(b) for b in blocks)
        finally:
            self.depth -= 1

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
        if isinstance(b, (Quote, Section)):
            return self._has_text(b.blocks)
        if isinstance(b, Table):
            return any(self._has_text(c.blocks) for r, row in enumerate(b.rows)
                       if self.show or r not in b.hidden_rows for c in row)
        return False

    def _grid(self, t: Table):
        """Origin cells by (row, col) on the reconstructed grid, trimmed: (origins, nrows, ncols)."""
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
        rows = []
        for r in range(nrows):
            cells = [self._cell_text(origins[k][0], m) for k in sorted(origins) if k[0] == r]
            cells = [x for x in cells if x]
            if r in t.hidden_rows:
                if not self.show:
                    self.hidden_count += 1
                    continue
                if cells:
                    cells[0] = "(hidden) " + cells[0]
            if cells:
                rows.append(" / ".join(cells))
        return " ; ".join(rows)

    def _table(self, t: Table) -> list:
        origins, nrows, ncols = self._grid(t)
        cap = [t.caption] if t.caption else []

        def hid(r):
            if r in t.hidden_rows:
                if not self.show:
                    self.hidden_count += 1
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
            if h and out:
                out[0] = (out[0][0], "(hidden) " + out[0][1])
            return ([("p", c) for c in cap]) + out
        if ncols == 1:
            lines, used = [], 0
            for r in range(nrows):
                if self.size + used > limits.MAX_OUTPUT:
                    break
                if (r, 0) not in origins:
                    continue
                s = self._cell_text(origins[(r, 0)][0], LINES)
                h = hid(r)
                if s and h is not None:
                    lines.append(("(hidden) " if h else "") + s)
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
            cells = []
            for c in range(ncols):
                if (r, c) not in origins:
                    if not spans:
                        cells.append(("", None))
                    continue
                cell, rs, cs = origins[(r, c)]
                s = self._cell_text(cell, m)
                cells.append((s, (cell, rs, cs)))
            if h and cells:
                cells[0] = ("(hidden) " + cells[0][0], cells[0][1])
            before = len(lines)
            if spans:
                tds = ""
                for s, info in cells:
                    cell, rs, cs = info
                    tag = "th" if cell.header else "td"
                    attrs = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
                    tds += f"<{tag}{attrs}>{s}</{tag}>"
                lines.append(f"<tr>{tds}</tr>")
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
        if self.doc.title.strip():
            first = pieces[0][1] if pieces else ""
            first = _plain(re.sub(r"^#+\s*", "", first).replace("**", ""))
            if first != _plain(self.doc.title):
                top.append("# " + self.doc.title.strip())
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
            while i < len(self.note_no):  # rendering a note may reference more notes
                nid = list(self.note_no)[i]
                i += 1
                if nid in notes:
                    s = " ".join(x for x in (self._flat(b, LINES) for b in notes[nid]) if x)
                    lines.append(f"[^{self.note_no[nid]}]: {s}".rstrip())
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
        n = len(line.encode("utf-8")) + 1
        if size + n > limit:
            break
        out.append(line)
        size += n
    return "\n".join(out) + "\n" + _TRUNC + "\n"


def render(doc: Doc, assets_dir: Path, hidden: bool = False, skipped=None, info=None) -> str:
    """Render a Doc to Markdown. The Doc is not modified.

    `skipped` is the parse context's skip counter; what it says was lost becomes a "(could not read: ...)" line.
    A dict passed as `info` receives "unread" (that line's items) and "empty" (no content rendered at all).
    """
    r = _Render(doc, Path(assets_dir), hidden, skipped)
    md = _cap(_normalize(r.body()))
    if info is not None:
        info.update(unread=r.unread, empty=r.empty and not r.hidden_count and not r.too_deep)
    return md


def stats(md: str) -> dict:
    lines = md.splitlines()
    n_hidden = 0
    if lines and (mo := _HIDDEN_LINE.match(lines[0])):
        n_hidden = int(mo.group(1))
    heads = [(i, len(mo.group(1)), mo.group(2).strip())
             for i, ln in enumerate(lines, 1) if (mo := re.match(r"^(#{1,6}) (.+)$", ln))]
    if heads:
        top = min(h[1] for h in heads)
        heads = [(i, t) for i, lvl, t in heads if lvl == top]
    return {"lines": len(lines), "hidden": n_hidden, "headings": heads}
