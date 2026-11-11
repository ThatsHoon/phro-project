"""OpenDocument to the document model: odt here, ods/odp share this layer. Elements match by local name."""
import collections
import re

from .. import limits
from ..containers.zipx import Package, attr, first, kids, local, parse_xml, resolve
from ..limits import ReadError
from ..model import (
    Bold, Break, Cell, Doc, Equation, Heading, Hidden, Image, Link, ListItem,
    NoteRef, Para, Table, Text,
)

_MIME = "application/vnd.oasis.opendocument."
_BODY_KINDS = {"text": "text", "spreadsheet": "spreadsheet", "presentation": "presentation"}
_DROP = frozenset({"page-number", "page-count", "annotation-end", "soft-page-break", "number", "bookmark",
                   "bookmark-start", "bookmark-end", "change", "change-start", "change-end",
                   "hidden-paragraph", "tracked-changes"})
_CONTAINERS = frozenset({"table-of-content", "user-index", "alphabetical-index", "bibliography",
                         "illustration-index", "table-index", "object-index", "index-body",
                         "index-title", "numbered-paragraph"})
_REPEAT = 100      # a repeated non-empty row or cell is expanded at most this often
_GAP = 64          # a repeated empty row or cell between content is expanded at most this often
_SPAN = 1000       # largest span taken from a file
_CELLS = 100_000   # cells expanded for one table
_SPACES = 200
_SYLLABLE = "가나다라마바사아자차카타파하"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _squash(s, n=200):
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _hide(blocks):
    for b in blocks:
        b.hidden = "hidden"
    return blocks


def _itext(inlines) -> str:
    out = ""
    for it in inlines:
        if isinstance(it, Text):
            out += it.text
        elif isinstance(it, (Bold, Link)):
            out += _itext(it.inlines)
        elif isinstance(it, Break):
            out += " "
    return out


def _flat(blocks) -> str:
    parts = []
    for b in blocks:
        if hasattr(b, "inlines"):
            parts.append(_itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(_flat(c.blocks) for row in b.rows for c in row)
    return " ".join(_squash(p, 10_000) for p in parts if p.strip())


def _empty(blocks) -> bool:
    return all(isinstance(b, Para) and not _itext(b.inlines).strip() and not b.hidden for b in blocks)


# ---- numbering markers
def _bijective(n, alphabet):
    out = ""
    while n > 0:
        n, r = divmod(n - 1, len(alphabet))
        out = alphabet[r] + out
    return out or str(n)


def _roman(n):
    if not 0 < n < 4000:
        return str(n)
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def _fmt(n, kind):
    if n < 1:
        return str(n)
    if kind == "a":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "A":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "i":
        return _roman(n).lower()
    if kind == "I":
        return _roman(n)
    if kind == "가":
        return _bijective(n, _SYLLABLE)
    if kind == "①":
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    return str(n)


class Styles:
    """Named styles from content.xml and styles.xml, with parent chains and family defaults."""

    def __init__(self, content_root, styles_root):
        self.styles, self.defaults, self.lists = {}, {}, {}
        for root in (content_root, styles_root):  # content first: its automatic styles win
            if root is None:
                continue
            for el in root.iter():
                n = local(el.tag)
                if n == "style":
                    self.styles.setdefault((attr(el, "family", ""), attr(el, "name", "")), el)
                elif n == "default-style":
                    self.defaults.setdefault(attr(el, "family", ""), el)
                elif n == "list-style":
                    self.lists.setdefault(attr(el, "name", ""), {
                        _int(attr(c, "level")): c for c in el if attr(c, "level") is not None})

    @staticmethod
    def _own(el, key):
        for c in el:
            if local(c.tag).endswith("properties"):
                v = attr(c, key)
                if v is not None:
                    return v
        return None

    def prop(self, name, family, key):
        seen, cur = set(), name
        while cur and cur not in seen:
            seen.add(cur)
            el = self.styles.get((family, cur))
            if el is None:
                break
            v = self._own(el, key)
            if v is not None:
                return v
            cur = attr(el, "parent-style-name")
        d = self.defaults.get(family)
        return self._own(d, key) if d is not None else None

    def list_level(self, name, level):
        return self.lists.get(name, {}).get(level)


class Body:
    """Walks ODF text content into model blocks and inlines. Task 10 reuses it for cells and shapes."""

    def __init__(self, pkg, ctx, styles, doc=None):
        self.pkg, self.ctx, self.styles = pkg, ctx, styles
        self.doc = doc if doc is not None else Doc()
        self.extras = []  # blocks produced by frames met inside the current paragraph
        self.cache, self.objects, self.bold_of = {}, {}, {}
        self.counts = collections.Counter()
        self.last, self.ids = None, {}  # list counters: the previous list's, and by xml:id

    # ---- package
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    @staticmethod
    def safe(href):
        """A package-internal member path for href, or None for external, absolute and escaping targets."""
        h = (href or "").strip()
        if not h or h[0] in "/#" or "\\" in h or re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", h):
            return None
        name = resolve("content.xml", h)
        return None if name.startswith("..") or name == "." else name

    def data(self, name, kind):
        if name not in self.cache:
            try:
                self.cache[name] = self.pkg.read(name)
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.cache[name] = None
        if not self.cache[name]:
            self.ctx.skipped[kind] += 1
        return self.cache[name]

    # ---- inlines
    def is_bold(self, name):
        if name not in self.bold_of:
            v = self.styles.prop(name, "text", "font-weight") if name else None
            self.bold_of[name] = v == "bold" or _int(v, 0) >= 600
        return self.bold_of[name]

    def inlines(self, el, d=0):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        self._text(el.text, out)
        for c in el:
            self._child(c, out, d)
            self._text(c.tail, out)
        return out

    @staticmethod
    def _text(s, out):
        if s:
            out.append(Text(re.sub(r"[ \t\r\n]+", " ", s)))

    def _child(self, c, out, d):
        n = local(c.tag)
        if n == "span":
            sub = self.inlines(c, d + 1)
            if sub and self.is_bold(attr(c, "style-name")):
                out.append(Bold(sub))
            else:
                out.extend(sub)
        elif n == "a":
            sub = self.inlines(c, d + 1)
            href = (attr(c, "href") or "").strip()[:2000]
            if href and not href.startswith("#"):
                out.append(Link(href, sub))
            else:
                out.extend(sub)
        elif n == "s":
            out.append(Text(" " * min(max(_int(attr(c, "c"), 1), 1), _SPACES)))
        elif n == "tab":
            out.append(Text("\t"))
        elif n == "line-break":
            out.append(Break())
        elif n == "note":
            self.note(c, out, d)
        elif n == "annotation":
            self.annotation(c, out, d)
        elif n == "hidden-text":
            out.append(Hidden([Text(_squash(attr(c, "string-value") or "".join(c.itertext())))]))
        elif n == "frame":
            self.frame(c, d)
        elif n not in _DROP:
            out.extend(self.inlines(c, d + 1))

    def note(self, el, out, d):
        key = "en" if attr(el, "note-class") == "endnote" else "fn"
        self.counts[key] += 1
        nid = f"{key}-{self.counts[key]}"
        body = kids(el, "note-body")
        self.doc.notes[nid] = self.blocks(body[0], d + 1) if body else []
        out.append(NoteRef(nid))

    def annotation(self, el, out, d):
        self.counts["cm"] += 1
        nid = f"cm-{self.counts['cm']}"
        who = next((_squash("".join(k.itertext()), 64) for k in kids(el, "creator")), "")
        prefix = f"Comment by {who}: " if who else "Comment: "
        blocks = self.blocks(el, d + 1)
        if blocks and isinstance(blocks[0], Para):
            blocks[0].inlines.insert(0, Text(prefix))
        else:
            blocks.insert(0, Para([Text(prefix.rstrip())]))
        self.doc.notes[nid] = blocks
        out.append(NoteRef(nid))

    # ---- frames and objects
    def frame_blocks(self, fr, d):
        saved, self.extras = self.extras, []
        try:
            self.frame(fr, d)
            return self.extras
        finally:
            self.extras = saved

    def frame(self, fr, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        alt = ""
        for tag in ("title", "desc"):
            alt = next((t for k in kids(fr, tag) if (t := _squash("".join(k.itertext())))), "")
            if alt:
                break
        found = False
        for k in fr:
            if local(k.tag) in ("object", "object-ole"):
                blocks = self.obj(k, d + 1)
                self.extras.extend(blocks)
                found = found or bool(blocks)
        if found:
            return
        for k in fr:
            n = local(k.tag)
            if n == "image":
                name = self.safe(attr(k, "href"))
                if name is None or not self.pkg.has(name):
                    self.ctx.skipped["image"] += 1
                    continue
                data = self.data(name, "image")
                if data:
                    self.extras.append(Image(data, alt))
            elif n == "text-box":
                self.extras.extend(self.blocks(k, d + 1))
            elif n == "frame":
                self.frame(k, d + 1)

    def obj(self, el, d):
        href = attr(el, "href")
        if not href:
            for c in el:
                if local(c.tag) in ("document", "math"):
                    return self.object_blocks(c, d)
            return []
        base = self.safe(href)
        name = base + "/content.xml" if base else None
        if name is None or not self.pkg.has(name):
            self.ctx.skipped["object"] += 1
            return []
        if name not in self.objects:
            self.objects[name] = []  # also breaks an object that refers to itself
            data = self.data(name, "object")
            if data:
                try:
                    self.objects[name] = self.object_blocks(parse_xml(data), d)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["object"] += 1
        return self.objects[name]

    def object_blocks(self, root, d):
        if local(root.tag) == "math":
            tex = ""
            for a in root.iter():
                if local(a.tag) == "annotation" and (attr(a, "encoding") or "").startswith("StarMath"):
                    tex = _squash("".join(a.itertext()), 2000)
                    break
            tex = tex or _squash(" ".join(t.strip() for t in root.itertext() if t.strip()), 2000)
            return [Equation(tex)] if tex else []
        for body in kids(root, "body"):
            for c in body:
                if local(c.tag) == "chart":
                    return self.chart(c, d)
                break
        self.ctx.skipped["object"] += 1
        return []

    def chart(self, ch, d):
        ch = next(iter(kids(ch, "chart")), ch)  # office:chart wraps chart:chart
        cls = (attr(ch, "class") or "").replace("chart:", "", 1)[:64]
        title = next((_squash("".join(t.itertext()), 100) for t in kids(ch, "title")), "")
        cap = ("Chart: " + cls if cls else "Chart") + (" — " + title if title else "")
        src = first(ch, "table")
        tbl = self.table(src, d + 1) if src is not None else None
        if tbl is None:
            return [Para([Text(cap)])]
        tbl.caption = cap
        return [tbl]

    # ---- blocks
    def blocks(self, el, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        for c in el:
            out.extend(self.block(c, d))
        return out

    def block(self, c, d):
        n = local(c.tag)
        if n in ("p", "h"):
            return self.para(c, d)
        if n == "list":
            return self.list(c, d)
        if n == "table":
            t = self.table(c, d)
            return [t] if t else []
        if n == "section":
            inner = self.blocks(c, d + 1)
            return _hide(inner) if attr(c, "display") == "none" else inner
        if n == "tracked-changes":
            return self.deletions(c)
        if n in _CONTAINERS:
            return self.blocks(c, d + 1)
        if n == "frame":
            return self.frame_blocks(c, d)
        return []

    def para(self, p, d):
        saved, self.extras = self.extras, []
        try:
            inl = self.inlines(p, d + 1)
            extras = self.extras
        finally:
            self.extras = saved
        if local(p.tag) == "h":
            level = min(max(_int(attr(p, "outline-level"), 1), 1), 6)
            main = Heading(level, inl)
        else:
            main = Para(inl)
        out = ([main] if inl else []) + extras
        if any(local(c.tag) == "hidden-paragraph" for c in p):
            _hide(out)
        return out

    def deletions(self, tc):
        out = []
        for reg in kids(tc, "changed-region"):
            for dl in kids(reg, "deletion"):
                text = " ".join("".join(k.itertext()) for k in dl if local(k.tag) != "change-info")
                out.append(Para([Hidden([Text(_squash(text))], "deleted")]))
        return out

    # ---- lists
    def list(self, lst, d):
        cnt = {}
        ref = attr(lst, "continue-list")
        if ref in self.ids:
            cnt = self.ids[ref]
        elif attr(lst, "continue-numbering") == "true" and self.last is not None:
            cnt = self.last
        out = self.items(lst, d, 0, attr(lst, "style-name"), cnt)
        self.last = cnt
        if attr(lst, "id"):
            self.ids[attr(lst, "id")] = cnt
        return out

    def items(self, lst, d, level, style, cnt):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        style = attr(lst, "style-name") or style
        out = []
        for it in lst:
            n = local(it.tag)
            if n == "list-header":
                out.extend(self.blocks(it, d + 1))
            elif n == "list-item":
                for k in [k for k in cnt if k > level]:
                    del cnt[k]
                marker = self.marker(style, level, cnt, attr(it, "start-value"))
                placed = False
                for c in it:
                    cn = local(c.tag)
                    if cn == "list":
                        out.extend(self.items(c, d + 1, level + 1, style, cnt))
                    elif cn == "p" and not placed:
                        placed = True
                        got = self.para(c, d + 1)
                        if got and isinstance(got[0], Para):
                            got[0] = ListItem(min(level, 6), marker, got[0].inlines, hidden=got[0].hidden)
                        out.extend(got)
                    else:
                        out.extend(self.block(c, d + 1))
        return out

    def marker(self, style, level, cnt, start_value):
        lv = self.styles.list_level(style, level + 1)
        fmt = attr(lv, "num-format") if lv is not None else None
        if not fmt:
            return "-"
        n = cnt[level] + 1 if level in cnt else _int(attr(lv, "start-value"), 1)
        if start_value is not None:
            n = _int(start_value, n)
        cnt[level] = n
        pre, suf = (attr(lv, "num-prefix") or "")[:16], (attr(lv, "num-suffix") or "")[:16]
        return (pre + _fmt(n, fmt) + (suf if pre or suf else "."))[:64]

    # ---- tables
    def table(self, tbl, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return None
        rows, budget = [], [_CELLS]
        self.rows_of(tbl, d, False, rows, budget)
        while rows and all(_empty(c.blocks) for c in rows[-1]):
            rows.pop()
        if not rows:
            return None
        for r, row in enumerate(rows):
            for cell in row:
                cell.rowspan = min(cell.rowspan, len(rows) - r)
        return Table(rows)

    def rows_of(self, cont, d, header, rows, budget):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        for c in cont:
            if budget[0] <= 0:
                return
            n = local(c.tag)
            if n == "table-row":
                cells = self.row(c, d, header, budget)
                rep = self.repeat(attr(c, "number-rows-repeated"), all(_empty(x.blocks) for x in cells))
                for _ in range(rep):
                    rows.append([Cell(x.blocks, x.rowspan, x.colspan, x.header) for x in cells])
                    budget[0] -= len(cells) or 1
            elif n in ("table-header-rows", "table-rows", "table-row-group"):
                self.rows_of(c, d + 1, header or n == "table-header-rows", rows, budget)

    def repeat(self, raw, empty):
        rep, cap = max(1, _int(raw, 1)), _GAP if empty else _REPEAT
        if rep > cap:
            self.ctx.skipped["repeat"] += 1
            return cap
        return rep

    def row(self, tr, d, header, budget):
        cells = []
        for c in tr:
            if local(c.tag) != "table-cell":
                continue  # covered cells carry no content; the spans of their origin cover them
            blocks = self.blocks(c, d + 1)
            cs = min(max(_int(attr(c, "number-columns-spanned"), 1), 1), _SPAN)
            rs = min(max(_int(attr(c, "number-rows-spanned"), 1), 1), _SPAN)
            for _ in range(self.repeat(attr(c, "number-columns-repeated"), _empty(blocks))):
                if budget[0] <= 0:
                    self.ctx.skipped["cells"] += 1
                    return cells
                budget[0] -= 1
                cells.append(Cell(blocks, rs, cs, header))
        while cells and _empty(cells[-1].blocks):
            cells.pop()
        return cells

    # ---- headers and footers
    def master(self, styles_root):
        for mp in (e for e in styles_root.iter() if local(e.tag) == "master-page"):
            for el in mp:
                n = local(el.tag)
                kind = "headers" if n.startswith("header") else "footers" if n.startswith("footer") else None
                if kind and (text := _flat(self.blocks(el, 0))):
                    getattr(self.doc, kind).append(text)


def para_inlines(el, styles, pkg, ctx, body=None) -> list:
    """Inlines of one paragraph-like element; a caller that needs its notes and frames passes its Body."""
    return (body or Body(pkg, ctx, styles)).inlines(el)


# ---- package
def _open(pkg, ctx):
    """(kind, content root, styles root) after the encryption and package checks."""
    if pkg.has("META-INF/manifest.xml"):
        try:
            man = pkg.read("META-INF/manifest.xml")
        except ReadError as e:
            if e.kind != "damaged":
                raise
            man = b""
            ctx.skipped["manifest"] += 1
        if b"encryption-data" in man.lower():
            raise ReadError("encrypted")
    if not pkg.has("content.xml"):
        raise ReadError("damaged", "not an OpenDocument package")
    try:
        content = pkg.xml("content.xml")
    except ReadError as e:
        if e.kind != "damaged":
            raise
        raise ReadError("damaged", "no readable content.xml")
    styles = None
    if pkg.has("styles.xml"):
        try:
            styles = pkg.xml("styles.xml")
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["styles"] += 1
    mime = ""
    if pkg.has("mimetype"):
        try:
            mime = pkg.read("mimetype").decode("utf-8", "replace").strip()
        except ReadError as e:
            if e.kind != "damaged":
                raise
    kind = _BODY_KINDS.get(mime[len(_MIME):].split("-")[0]) if mime.startswith(_MIME) else None
    if kind is None:
        kind = next((_BODY_KINDS[local(c.tag)] for b in kids(content, "body") for c in b
                     if local(c.tag) in _BODY_KINDS), None)
    if kind is None:
        raise ReadError("damaged", "not an OpenDocument package")
    return kind, content, styles


def _text(pkg, ctx, content, styles_root) -> Doc:
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "text"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    for c in office:
        with ctx.guard("block"):
            body.doc.blocks.extend(body.block(c, 0))
    if styles_root is not None:
        with ctx.guard("header"):
            body.master(styles_root)
    return body.doc


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        kind, content, styles_root = _open(pkg, ctx)
        if kind == "text":
            return _text(pkg, ctx, content, styles_root)
        raise ReadError("unsupported", "ods/odp arrive in Task 10")
