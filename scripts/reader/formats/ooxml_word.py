"""WordprocessingML (docm) to the document model. Elements match by local name."""
import re

from .. import limits
from ..containers.zipx import attr, first, kids, local
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Hidden, Image, Link, ListItem, NoteRef, Para, Table, Text
from . import ooxml

IMAGE_BYTES = 256 * 2**20  # image bytes one document may emit, copies counted
CHART_CELLS = 200_000      # chart table cells one document may emit, copies counted
MAX_COLS = 1_000
MAX_URL, MAX_ALT, MAX_NAME, MAX_MARKER, MAX_INSTR = 1000, 200, 64, 64, 300

_HEAD = re.compile(r"^heading\s*(\d+)$", re.I)
_PLACEHOLDER = re.compile(r"%([1-9])")
_HYPERLINK = re.compile(r'^\s*HYPERLINK\s+(?:"([^"]*)"|([^\s"\\]+))', re.I)
_NOTE_SKIP = frozenset({"separator", "continuationSeparator", "continuationNotice"})
_NOTE_REFS = {"footnoteReference": "fn-", "endnoteReference": "en-", "commentReference": "cm-"}
_SKIP = frozenset({"pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr", "proofErr", "bookmarkStart", "bookmarkEnd",
                   "commentRangeStart", "commentRangeEnd", "docPr", "extent", "effectExtent", "lastRenderedPageBreak"})
_SYLLABLE = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _on(el) -> bool:
    return attr(el, "val") not in ("0", "false", "off")


def _val(el):
    return attr(el, "val") if el is not None else None


# ---- numbering formats
def _roman(n):
    if not 0 < n < 4000:
        return str(n)
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def _bijective(n, alphabet):
    out = ""
    while n > 0:
        n, r = divmod(n - 1, len(alphabet))
        out = alphabet[r] + out
    return out or str(n)


def _fmt(n, kind):
    if n < 1:
        return str(n)
    if kind == "lowerLetter":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "upperLetter":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "lowerRoman":
        return _roman(n).lower()
    if kind == "upperRoman":
        return _roman(n)
    if kind == "ganada":
        return _bijective(n, _SYLLABLE)
    if kind == "chosung":
        return _bijective(n, _JAMO)
    if kind == "decimalEnclosedCircle":
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    return str(n)


# ---- inline helpers
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
        if isinstance(b, (Para, Heading, ListItem)):
            parts.append(_itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(_flat(c.blocks) for row in b.rows for c in row)
    return " ".join(re.sub(r"\s+", " ", p).strip() for p in parts if p.strip())


class _Frame:
    """One open inline scope inside a paragraph: root, link, deleted or hidden."""

    def __init__(self, kind, url=""):
        self.kind, self.url, self.inl = kind, url, []


class _State:
    def __init__(self):
        self.frames = [_Frame("root")]
        self.bold = False
        self.extras = []  # blocks that follow the paragraph: images, charts, text-box content
        self.fields = []  # open fldChar fields: [instr parts, phase, frame index or None]
        self.alt = ""


class _Scope:
    """The part being walked: its name, relationships and namespace prefixes."""

    def __init__(self, part, rels, nsmap):
        self.part, self.rels, self.nsmap = part, rels, nsmap


class _Reader:
    def __init__(self, pkg, part, ctx):
        self.pkg, self.part, self.ctx = pkg, part, ctx
        self.doc = Doc()
        self.styles, self.chains = {}, {}
        self.abstracts, self.nums, self.counters = {}, {}, {}
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0

    # ---- packages and parts
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def related(self, suffix, kind):
        """Unique internal targets of the main part's relationships of one type, in order; missing ones are counted."""
        out = []
        for rel in ooxml.rels(self.pkg, self.part, self.ctx).values():
            if rel.external or not rel.type.endswith(suffix):
                continue
            if not self.pkg.has(rel.target):
                self.ctx.skipped[kind] += 1
            elif rel.target not in out:
                out.append(rel.target)
        return out

    def open(self, name, kind):
        """Make `name` the current scope; its root, or None (counted) when damaged."""
        try:
            root, nsmap = ooxml.load(self.pkg, name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self.ctx.skipped[kind] += 1
            return None
        self.scope = _Scope(name, ooxml.rels(self.pkg, name, self.ctx), nsmap)
        return root

    def run(self):
        for name in self.related("/styles", "styles")[:1]:
            root = self.open(name, "styles")
            if root is not None:
                with self.ctx.guard("styles"):
                    self.load_styles(root)
        for name in self.related("/numbering", "numbering")[:1]:
            root = self.open(name, "numbering")
            if root is not None:
                with self.ctx.guard("numbering"):
                    self.load_numbering(root)
        for suffix, tag, prefix in (("/footnotes", "footnote", "fn-"), ("/endnotes", "endnote", "en-")):
            for name in self.related(suffix, "footnotes")[:1]:
                root = self.open(name, "footnotes")
                if root is not None:
                    self.load_notes(root, tag, prefix)
        for name in self.related("/comments", "comments")[:1]:
            root = self.open(name, "comments")
            if root is not None:
                self.load_comments(root)
        for suffix, kind, store in (("/header", "header", self.doc.headers), ("/footer", "footer", self.doc.footers)):
            for name in self.related(suffix, kind):
                root = self.open(name, kind)
                if root is not None:
                    with self.ctx.guard(kind):
                        text = _flat(self.blocks(root, 0))
                    if text:
                        store.append(text)
        root = self.open(self.part, "document")
        body = next(iter(kids(root, "body")), None)
        if body is None:
            raise ReadError("damaged", "no readable body")
        self.doc.blocks.extend(self.blocks(body, 0))
        return self.doc

    # ---- styles
    def load_styles(self, root):
        for s in kids(root, "style"):
            sid = attr(s, "styleId")
            if not sid:
                continue
            d = {"name": "", "based": None, "outline": None, "num": (None, None), "bold": None, "vanish": None}
            for c in s:
                n = local(c.tag)
                if n == "name":
                    d["name"] = (_val(c) or "")[:MAX_NAME]
                elif n == "basedOn":
                    d["based"] = _val(c)
                elif n == "pPr":
                    for e in c:
                        en = local(e.tag)
                        if en == "outlineLvl":
                            d["outline"] = _int(_val(e), None)
                        elif en == "numPr":
                            d["num"] = self.numpr(e)
                elif n == "rPr":
                    for e in c:
                        en = local(e.tag)
                        if en in ("b", "vanish"):
                            d["bold" if en == "b" else "vanish"] = _on(e)
            self.styles[sid] = d

    def chain(self, sid):
        """The style and its basedOn ancestors, nearest first; cycles and runaway chains stop."""
        if sid not in self.chains:
            out, seen, cur = [], set(), sid
            while cur and cur not in seen and cur in self.styles and len(out) < limits.MAX_DEPTH:
                seen.add(cur)
                out.append(self.styles[cur])
                cur = self.styles[cur]["based"]
            self.chains[sid] = out
        return self.chains[sid]

    @staticmethod
    def numpr(el):
        ids = kids(el, "numId")
        lv = kids(el, "ilvl")
        return (_val(ids[0]) if ids else None, _val(lv[0]) if lv else None)

    @staticmethod
    def prop(chain, key):
        return next((s[key] for s in chain if s[key] is not None), None)

    def heading_level(self, own, chain) -> int:
        if own is not None:
            return min(own + 1, 6) if 0 <= own <= 8 else 0
        for s in chain:
            if m := _HEAD.match(s["name"].strip()):
                return min(max(int(m.group(1)), 1), 6)
            if s["name"].strip().lower() == "title":
                return 1
        lvl = self.prop(chain, "outline")
        return min(lvl + 1, 6) if lvl is not None and 0 <= lvl <= 8 else 0

    # ---- numbering
    def load_numbering(self, root):
        for a in kids(root, "abstractNum"):
            aid = attr(a, "abstractNumId")
            levels = {}
            for lv in kids(a, "lvl"):
                st, nf, tx = (first(lv, k) for k in ("start", "numFmt", "lvlText"))
                levels[_int(attr(lv, "ilvl"))] = (_int(_val(st), 1), _val(nf) or "decimal", (_val(tx) or "")[:MAX_MARKER])
            if aid is not None:
                self.abstracts[aid] = levels
        for n in kids(root, "num"):
            nid = attr(n, "numId")
            ab = kids(n, "abstractNumId")
            over = {_int(attr(o, "ilvl")): _int(_val(first(o, "startOverride")), 1)
                    for o in kids(n, "lvlOverride") if first(o, "startOverride") is not None}
            if nid is not None and ab:
                self.nums[nid] = (_val(ab[0]), over)

    def marker(self, nid, lvl):
        """List marker for the next paragraph at (numId, level); None when the list is not defined."""
        num = self.nums.get(nid)
        if num is None:
            return None
        levels, over = self.abstracts.get(num[0], {}), num[1]
        info = levels.get(lvl)
        if info is None:
            return None

        def start(k):
            return over.get(k, levels[k][0] if k in levels else 1)

        cnt = self.counters.setdefault(nid, {})
        cnt[lvl] = cnt.get(lvl, start(lvl) - 1) + 1
        for k in [k for k in cnt if k > lvl]:
            del cnt[k]
        if info[1] == "bullet":
            return "-"
        if info[1] == "none":
            return ""

        def sub(m):
            k = int(m.group(1)) - 1
            return _fmt(cnt.get(k, start(k)), levels[k][1] if k in levels else "decimal")

        return _PLACEHOLDER.sub(sub, info[2])[:MAX_MARKER].strip()

    # ---- notes
    def load_notes(self, root, tag, prefix):
        for nt in kids(root, tag):
            if attr(nt, "type") in _NOTE_SKIP:
                continue
            nid = attr(nt, "id")
            with self.ctx.guard("note"):
                if nid is not None:
                    self.doc.notes[prefix + nid] = self.blocks(nt, 0)

    def load_comments(self, root):
        for cm in kids(root, "comment"):
            nid = attr(cm, "id")
            with self.ctx.guard("note"):
                if nid is None:
                    continue
                blocks = self.blocks(cm, 0)
                lead = Text(f"Comment by {(attr(cm, 'author') or '')[:MAX_NAME]}: ")
                if blocks and isinstance(blocks[0], Para):
                    blocks[0].inlines.insert(0, lead)
                else:
                    blocks.insert(0, Para([lead]))
                self.doc.notes["cm-" + nid] = blocks

    # ---- blocks
    def blocks(self, container, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        for c in container:
            with self.ctx.guard("block"):
                out.extend(self.block(c, d))
        return out

    def block(self, c, d):
        n = local(c.tag)
        if n == "p":
            return self.para(c, d)
        if n == "tbl":
            return self.table(c, d)
        if n == "sdt":
            return self.blocks(self.sdt_content(c), d + 1)
        if n in ("ins", "moveTo", "customXml", "smartTag"):
            return self.blocks(c, d + 1)
        if n in ("del", "moveFrom"):
            out = self.blocks(c, d + 1)
            for b in out:
                b.hidden = "deleted"
            return out
        if n == "AlternateContent":
            chosen = ooxml.alternate(c, self.scope.nsmap)
            return self.blocks(chosen, d + 1) if chosen is not None else []
        return []

    @staticmethod
    def sdt_content(sdt):
        """The sdtContent element, or an empty tuple for a placeholder that is still showing."""
        if any(local(e.tag) == "showingPlcHdr" for pr in kids(sdt, "sdtPr") for e in pr):
            return ()
        return next(iter(kids(sdt, "sdtContent")), ())

    def para(self, p, d):
        st = _State()
        own_outline = own_num = own_lvl = pstyle = None
        for c in p:
            if local(c.tag) != "pPr":
                self.node(c, st, d + 1)
                continue
            for e in c:
                en = local(e.tag)
                if en == "pStyle":
                    pstyle = _val(e)
                elif en == "outlineLvl":
                    own_outline = _int(_val(e), None)
                elif en == "numPr":
                    own_num, own_lvl = self.numpr(e)
        self.close_to(st, 1)
        inl = st.frames[0].inl
        chain = self.chain(pstyle)
        num_id, lvl = own_num, own_lvl
        if num_id is None or lvl is None:
            s_num = next((s["num"] for s in chain if s["num"] != (None, None)), (None, None))
            num_id = num_id if num_id is not None else s_num[0]
            lvl = lvl if lvl is not None else s_num[1]
        mark = self.marker(num_id, _int(lvl)) if num_id not in (None, "0") else None
        level = self.heading_level(own_outline, chain)
        hidden = "hidden" if self.prop(chain, "vanish") else ""
        main = None
        if level and _itext(inl).strip():
            if mark and mark != "-":
                inl.insert(0, Text(mark + " "))
            main = Heading(level, inl, hidden=hidden)
        elif mark and inl:
            main = ListItem(min(max(_int(lvl), 0), 8), mark, inl, hidden=hidden)
        elif inl:
            main = Para(inl, hidden=hidden)
        return ([main] if main else []) + st.extras

    # ---- inline scopes
    def add(self, st, inline):
        st.frames[-1].inl.append(inline)

    def text(self, s, st):
        if s:
            t = Text(s)
            self.add(st, Bold([t]) if st.bold else t)

    def close_to(self, st, n):
        while len(st.frames) > n:
            f = st.frames.pop()
            if not f.inl:
                continue
            parent = st.frames[-1].inl
            if f.kind == "link":
                parent.append(Link(f.url, f.inl))
            elif f.kind == "deleted":
                parent.append(Hidden(f.inl, "deleted"))
            elif f.kind == "hidden":
                parent.append(Hidden(f.inl))

    def push(self, st, frame) -> bool:
        if len(st.frames) < limits.MAX_DEPTH:
            st.frames.append(frame)
            return True
        self.deep()
        return False

    def scoped(self, st, el, d, frame):
        n = len(st.frames)
        self.push(st, frame)
        for c in el:
            self.node(c, st, d + 1)
        self.close_to(st, n)

    def field_char(self, el, st):
        kind = attr(el, "fldCharType")
        if kind == "begin":
            if len(st.fields) < limits.MAX_DEPTH:
                st.fields.append([[], "code", None])
            else:
                self.deep()
        elif kind == "separate" and st.fields:
            f = st.fields[-1]
            f[1] = "result"
            url = self.hyperlink_url("".join(f[0]))
            if url:
                idx = len(st.frames)
                if self.push(st, _Frame("link", url)):
                    f[2] = idx
        elif kind == "end" and st.fields:
            f = st.fields.pop()
            if f[2] is not None:
                self.close_to(st, f[2])

    @staticmethod
    def hyperlink_url(instr):
        m = _HYPERLINK.match(instr or "")
        return ((m.group(1) if m.group(1) is not None else m.group(2)) or "")[:MAX_URL] if m else ""

    def run_props(self, r):
        bold = vanish = rstyle = None
        for pr in kids(r, "rPr"):
            for e in pr:
                en = local(e.tag)
                if en == "b":
                    bold = _on(e)
                elif en == "vanish":
                    vanish = _on(e)
                elif en == "rStyle":
                    rstyle = _val(e)
        chain = self.chain(rstyle)
        bold = bold if bold is not None else self.prop(chain, "bold")
        vanish = vanish if vanish is not None else self.prop(chain, "vanish")
        return bool(bold), bool(vanish)

    # ---- paragraph content
    def node(self, el, st, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        n = local(el.tag)
        if n == "r":
            bold, vanish = self.run_props(el)
            old, st.bold = st.bold, bold
            idx = len(st.frames)
            pushed = vanish and self.push(st, _Frame("hidden"))
            for c in el:
                self.node(c, st, d + 1)
            if pushed:
                self.close_to(st, idx)
            st.bold = old
        elif n in ("t", "delText"):
            self.text(el.text, st)
        elif n == "instrText":
            if st.fields and st.fields[-1][1] == "code" and sum(map(len, st.fields[-1][0])) < MAX_INSTR:
                st.fields[-1][0].append(el.text or "")
        elif n == "fldChar":
            self.field_char(el, st)
        elif n in ("tab", "ptab"):
            self.text("\t", st)
        elif n in ("br", "cr"):
            self.add(st, Break())
        elif n == "noBreakHyphen":
            self.text("-", st)
        elif n == "fldSimple":
            url = self.hyperlink_url(attr(el, "instr"))
            if url:
                self.scoped(st, el, d, _Frame("link", url))
            else:
                for c in el:
                    self.node(c, st, d + 1)
        elif n == "hyperlink":
            rid, url = attr(el, "id"), ""
            if rid:
                rel = self.scope.rels.get(rid)
                if rel is not None and rel.external and rel.type.endswith("/hyperlink"):
                    url = rel.target[:MAX_URL]
                else:
                    self.ctx.skipped["link"] += 1
            if url:
                self.scoped(st, el, d, _Frame("link", url))
            else:
                for c in el:
                    self.node(c, st, d + 1)
        elif n in ("del", "moveFrom"):
            self.scoped(st, el, d, _Frame("deleted"))
        elif n == "sdt":
            for c in self.sdt_content(el):
                self.node(c, st, d + 1)
        elif n == "AlternateContent":
            chosen = ooxml.alternate(el, self.scope.nsmap)
            for c in (chosen if chosen is not None else ()):
                self.node(c, st, d + 1)
        elif n in _NOTE_REFS:
            key = _NOTE_REFS[n] + (attr(el, "id") or "")
            if key in self.doc.notes:
                self.add(st, NoteRef(key))
        elif n in ("inline", "anchor"):
            old = st.alt
            pr = kids(el, "docPr")
            st.alt = re.sub(r"\s+", " ", (attr(pr[0], "descr") or "") if pr else old).strip()[:MAX_ALT]
            for c in el:
                self.node(c, st, d + 1)
            st.alt = old
        elif n == "blip":
            self.picture(st, attr(el, "embed"))
        elif n == "imagedata":
            self.picture(st, attr(el, "id"), re.sub(r"\s+", " ", attr(el, "title") or "").strip()[:MAX_ALT])
        elif n == "chart":
            self.chart(st, attr(el, "id"))
        elif n == "txbxContent":
            st.extras.extend(self.blocks(el, d + 1))
        elif n not in _SKIP:
            for c in el:
                self.node(c, st, d + 1)

    def picture(self, st, rid, alt=None):
        img = ooxml.image(self.pkg, self.scope.part, rid)
        if img is not None:
            self.image_bytes += len(img.data)
        if img is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        st.extras.append(Image(img.data, st.alt if alt is None else alt))

    def chart(self, st, rid):
        tbl = ooxml.chart(self.pkg, self.scope.part, rid)
        if tbl is not None:
            self.chart_cells += sum(len(r) for r in tbl.rows)
        if tbl is None or self.chart_cells > CHART_CELLS:
            self.ctx.skipped["chart"] += 1
            return
        st.extras.append(tbl)

    # ---- tables
    def table(self, tbl, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        rows = []  # (tr, [tc, ...])
        for ch in tbl:
            n = local(ch.tag)
            if n == "tr":
                rows.append(ch)
            elif n == "sdt":
                rows.extend(c for c in self.sdt_content(ch) if local(c.tag) == "tr")
        cells_of = []
        for tr in rows:
            tcs = []
            for c in tr:
                n = local(c.tag)
                if n == "tc":
                    tcs.append(c)
                elif n == "sdt":
                    tcs.extend(x for x in self.sdt_content(c) if local(x.tag) == "tc")
            cells_of.append(tcs)
        grid = next(iter(kids(tbl, "tblGrid")), None)
        width = min(max(len(kids(grid, "gridCol")) if grid is not None else 0,
                        max((len(t) for t in cells_of), default=0), 1), MAX_COLS)
        out, hidden, open_ = [], set(), {}
        for ri, (tr, tcs) in enumerate(zip(rows, cells_of)):
            before, header = 0, False
            for pr in kids(tr, "trPr"):
                for e in pr:
                    en = local(e.tag)
                    if en == "gridBefore":
                        before = min(max(_int(_val(e)), 0), width)
                    elif en == "tblHeader":
                        header = _on(e)
                    elif en == "del":
                        hidden.add(ri)
            row, col, new_open = [], before, {}
            if before:
                row.append(Cell([], colspan=before))
            for tc in tcs:
                span, merge = 1, None
                for pr in kids(tc, "tcPr"):
                    for e in pr:
                        en = local(e.tag)
                        if en == "gridSpan":
                            span = max(_int(_val(e), 1), 1)
                        elif en == "vMerge":
                            merge = "restart" if _val(e) == "restart" else "continue"
                span = min(span, max(width - col, 1))
                if merge == "continue" and col in open_:
                    open_[col].rowspan += 1
                    new_open[col] = open_[col]
                else:
                    cell = Cell(self.blocks(tc, d + 1), 1, span, header)
                    row.append(cell)
                    if merge == "restart":
                        new_open[col] = cell
                col += span
            open_ = new_open
            out.append(row)
        return [Table(out, hidden_rows=frozenset(hidden))] if out else []


def parse_word(pkg, part, ctx) -> Doc:
    return _Reader(pkg, part, ctx).run()
