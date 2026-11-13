"""WordprocessingML (docm) to the document model. Elements match by local name."""
import re

from .. import limits
from ..containers.zipx import attr, kids, local
from ..limits import IMAGE_BYTES, ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Hidden, Image, Link, ListItem, NoteRef, Para, Table, Text
from . import ooxml_base
from .ooxml_base import int_of as _int, is_on as _on, val as _val
from .ooxml_styles import Numbering, Styles, numpr
from .word_fields import MAX_INSTR, MAX_URL, comment_note, hyperlink_url

MAX_COLS = 1_000
MAX_ALT = 200

_NOTE_SKIP = frozenset({"separator", "continuationSeparator", "continuationNotice"})
_NOTE_REFS = {"footnoteReference": "fn-", "endnoteReference": "en-", "commentReference": "cm-"}
_SKIP = frozenset({"pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr", "proofErr", "bookmarkStart", "bookmarkEnd",
                   "commentRangeStart", "commentRangeEnd", "docPr", "extent", "effectExtent", "lastRenderedPageBreak"})


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
        self.styles, self.numbering = Styles(), Numbering()
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0
        self.opened = 0  # tables opened so far (TABLE_NODES)

    # ---- packages and parts
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def related(self, suffix, kind):
        """Unique internal targets of the main part's relationships of one type, in order; missing ones are counted."""
        out = []
        for rel in ooxml_base.rels(self.pkg, self.part, self.ctx).values():
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
            root, nsmap = ooxml_base.load(self.pkg, name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self.ctx.skipped[kind] += 1
            return None
        self.scope = _Scope(name, ooxml_base.rels(self.pkg, name, self.ctx), nsmap)
        return root

    def run(self):
        for name in self.related("/styles", "styles")[:1]:
            root = self.open(name, "styles")
            if root is not None:
                with self.ctx.guard("styles"):
                    self.styles.load(root)
        for name in self.related("/numbering", "numbering")[:1]:
            root = self.open(name, "numbering")
            if root is not None:
                with self.ctx.guard("numbering"):
                    self.numbering.load(root)
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
                self.doc.notes["cm-" + nid] = comment_note(self.blocks(cm, 0), attr(cm, "author") or "")

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
            chosen = self.choose(c)
            return self.blocks(chosen, d + 1) if chosen is not None else []
        return []

    def choose(self, el):
        chosen = ooxml_base.alternate(el, self.scope.nsmap)
        if chosen is None:
            self.ctx.skipped["alternate"] += 1
        return chosen

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
                    own_num, own_lvl = numpr(e)
        self.close_to(st, 1)
        inl = st.frames[0].inl
        chain = self.styles.chain(pstyle)
        num_id, lvl = own_num, own_lvl
        if num_id is None or lvl is None:
            s_num = next((s["num"] for s in chain if s["num"] != (None, None)), (None, None))
            num_id = num_id if num_id is not None else s_num[0]
            lvl = lvl if lvl is not None else s_num[1]
        mark = self.numbering.marker(num_id, _int(lvl)) if num_id not in (None, "0") else None
        level = self.styles.heading_level(own_outline, chain)
        hidden = "hidden" if self.styles.prop(chain, "vanish") else ""
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
        if not self.push(st, frame) and frame.kind == "deleted":
            return  # counted by push; deleted text is never shown
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
            url = hyperlink_url("".join(f[0]))
            if url:
                idx = len(st.frames)
                if self.push(st, _Frame("link", url)):
                    f[2] = idx
        elif kind == "end" and st.fields:
            f = st.fields.pop()
            if f[2] is not None:
                self.close_to(st, f[2])

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
        chain = self.styles.chain(rstyle)
        bold = bold if bold is not None else self.styles.prop(chain, "bold")
        vanish = vanish if vanish is not None else self.styles.prop(chain, "vanish")
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
            if not vanish or pushed:  # a vanished run that cannot be framed is left out, never shown
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
            url = hyperlink_url(attr(el, "instr"))
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
            chosen = self.choose(el)
            for c in (chosen if chosen is not None else ()):
                self.node(c, st, d + 1)
        elif n in _NOTE_REFS:
            key = _NOTE_REFS[n] + (attr(el, "id") or "")
            if key in self.doc.notes:
                self.add(st, NoteRef(key))
            else:
                self.ctx.skipped["note"] += 1
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
        img = ooxml_base.image(self.pkg, self.scope.part, rid)
        if img is not None:
            self.image_bytes += len(img.data)
        if img is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        st.extras.append(Image(img.data, st.alt if alt is None else alt))

    def chart(self, st, rid):
        tbl = ooxml_base.chart(self.pkg, self.scope.part, rid, self.ctx)
        if tbl is not None:
            self.chart_cells += sum(len(r) for r in tbl.rows)
        if tbl is None or self.chart_cells > limits.CHART_TOTAL:
            self.ctx.skipped["chart"] += 1
            return
        st.extras.append(tbl)

    # ---- tables
    def table(self, tbl, d):
        if d > limits.MAX_DEPTH or self.opened >= limits.TABLE_NODES:
            self.deep()
            return []
        self.opened += 1
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
