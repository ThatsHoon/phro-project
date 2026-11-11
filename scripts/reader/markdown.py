"""The one Markdown renderer: every output format and token rule lives here."""
import re
from pathlib import Path

from . import limits
from .limits import ReadError
from .model import (
    Bold, Break, Doc, Equation, Heading, Hidden, Image, Link, ListItem, Math,
    NoteRef, Para, Quote, Section, Table, Text,
)

_PAGE_ONLY = re.compile(r"^[\s\-–—]*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*(쪽)?[\s\-–—]*$", re.I)
_LINE_START = re.compile(r"^(#|>|[-*+] |\d+\. )")
_HIDDEN_LINE = re.compile(r"^\((\d+) hidden places left out\)$")
_SPAN_CAP = 1000
_TRUNC = "(output truncated at 20 MB)"


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


class _Render:
    def __init__(self, doc: Doc, assets_dir: Path, show_hidden: bool):
        self.doc, self.assets_dir, self.show = doc, assets_dir, show_hidden
        self.hidden_count = 0
        self.depth = 0
        self.note_no: dict[str, int] = {}

    # ---- hidden handling
    def _skip(self, node) -> bool:
        """True when a hidden node is to be left out (and counted)."""
        if node.hidden and not self.show:
            self.hidden_count += 1
            return True
        return False

    def _enter(self):
        self.depth += 1
        if self.depth > limits.MAX_DEPTH:
            raise ReadError("damaged", "nesting too deep")

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
        self._enter()
        try:
            out = []
            for b in blocks:
                if self._skip(b):
                    continue
                out.extend(self._block(b))
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

    def _image(self, b: Image) -> list:  # Task 4 replaces this with Assets
        return [("p", self._tag(b, b.alt))] if b.alt.strip() else []

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
        self._enter()
        try:
            if isinstance(b, (Para, ListItem)):
                s = self.inlines(b.inlines, m)
                if isinstance(b, ListItem):
                    s = b.marker + " " + s
            elif isinstance(b, Heading):
                s = self.inlines(b.inlines, HEAD)
            elif isinstance(b, Image):
                s = b.alt
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

    def _empty(self, cell) -> bool:
        n = self.hidden_count
        saved = dict(self.note_no)
        empty = not self._cell_text(cell, PIPE)
        self.hidden_count = n
        self.note_no = saved
        return empty

    def _grid(self, t: Table):
        """Origin cells by (row, col) on the reconstructed grid, trimmed: (origins, nrows, ncols)."""
        covered = set()
        origins = {}
        for r, row in enumerate(t.rows):
            c = 0
            for cell in row:
                while (r, c) in covered:
                    c += 1
                rs = min(max(1, cell.rowspan), _SPAN_CAP)
                cs = min(max(1, cell.colspan), _SPAN_CAP)
                origins[(r, c)] = (cell, rs, cs)
                for dr in range(rs):
                    for dc in range(cs):
                        covered.add((r + dr, c + dc))
                c += cs
        nrows = ncols = 0
        for (r, c), (cell, rs, cs) in origins.items():
            if not self._empty(cell):
                nrows = max(nrows, min(r + rs, len(t.rows)))
                ncols = max(ncols, c + cs)
        kept = {k: v for k, v in origins.items() if k[0] < nrows and k[1] < ncols}
        return kept, nrows, ncols

    def _nested(self, t: Table, m) -> str:
        origins, nrows, _ = self._grid(t)
        rows = []
        for r in range(nrows):
            cells = [self._cell_text(origins[k][0], m) for k in sorted(origins) if k[0] == r]
            cells = [x for x in cells if x]
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
            lines = []
            for r in range(nrows):
                if (r, 0) not in origins:
                    continue
                s = self._cell_text(origins[(r, 0)][0], LINES)
                h = hid(r)
                if s and h is not None:
                    lines.append(("(hidden) " if h else "") + s)
            return finish("\n".join(lines))
        spans = any(rs > 1 or cs > 1 for _, rs, cs in origins.values())
        m = HTML if spans else PIPE
        lines = []
        shown = 0
        for r in range(nrows):
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
        if self.doc.title.strip():
            first = pieces[0][1] if pieces else ""
            first = _plain(re.sub(r"^#+\s*", "", first).replace("**", ""))
            if first != _plain(self.doc.title):
                top.append("# " + self.doc.title.strip())
        if self.hidden_count:
            top.insert(0, f"({self.hidden_count} hidden places left out)")
        if hf:
            top.insert(1 if self.hidden_count else 0, "\n".join(hf))
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


def render(doc: Doc, assets_dir: Path, hidden: bool = False) -> str:
    """Render a Doc to Markdown. The Doc is not modified."""
    return _cap(_normalize(_Render(doc, Path(assets_dir), hidden).body()))


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
