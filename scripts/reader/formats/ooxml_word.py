"""WordprocessingML (docm) to the document model. Elements match by local name.

Each drawing group (`wpg:wgp`) and canvas (`wpc:wpc`) is one diagram scope: its `wps:wsp` members, nested groups
included, go to `diagram.Diagram` under their `wps:cNvPr@id` with their boxes in EMU through every enclosing group's
transform, and a member with `wps:cNvCnPr` is a connector with its glue and arrowheads. The diagram takes the group's
place among the blocks that follow the paragraph. A group in a header or footer, a lone `wps:wsp` and VML read as text.
"""
import re

from .. import diagram, html as xhtml, limits
from ..containers.zipx import Package, attr, first, kids, local
from ..limits import IMAGE_BYTES, ReadError
from ..model import (Bold, Break, Cell, Doc, Equation, Heading, Hidden, Image, Inserted, Link, ListItem, Math, NoteRef,
                     Para, Table, Text)
from . import omml, ooxml_base, rtf, smartart
from .drawingml import Xf, arrows, glue, kid, kind, rule, xfrm
from .ooxml_base import int_of as _int, is_on as _on, val as _val
from .ooxml_styles import Numbering, Styles, numpr
from .word_fields import MAX_INSTR, MAX_NAME, MAX_URL, comment_note, hyperlink_url, properties, symbol, when

MAX_COLS = 1_000
MAX_ALT = 200

_NOTE_SKIP = frozenset({"separator", "continuationSeparator", "continuationNotice"})
_NOTE_REFS = {"footnoteReference": "fn-", "endnoteReference": "en-", "commentReference": "cm-"}
_SKIP = frozenset({"pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr", "proofErr", "bookmarkStart", "bookmarkEnd",
                   "commentRangeStart", "commentRangeEnd", "docPr", "extent", "effectExtent", "lastRenderedPageBreak"})
_PAGE_FIELD = re.compile(r"^\s*(?:PAGE|NUMPAGES|SECTIONPAGES)\b", re.I)
_CORE = {"title": "title", "subject": "subject", "creator": "author", "lastModifiedBy": "last author",
         "created": "created", "modified": "modified", "keywords": "keywords"}
_SHARED = ("image_bytes", "chart_cells", "smartart_nodes", "opened", "budget")
_PROPERTY_PARTS = ("/metadata/core-properties", "/extended-properties", "/custom-properties")


# ---- inline helpers
def _itext(inlines) -> str:
    out = ""
    for it in inlines:
        if isinstance(it, Text):
            out += it.text
        elif isinstance(it, (Bold, Link, Inserted)):
            out += _itext(it.inlines)
        elif isinstance(it, Break):
            out += " "
    return out


def _author(el) -> str:
    return re.sub(r"\s+", " ", attr(el, "author") or "").strip()[:MAX_NAME]


def _squash(s, n=MAX_ALT) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def _renamed(blocks, notes, tag) -> None:
    """Give every NoteRef in `blocks` and `notes` (a part read on its own: an altChunk) the id `<id>@<tag>`, and the
    notes the same keys, so its ids cannot meet the document's. One walk."""
    stack = [*blocks, *(b for v in notes.values() for b in v)]
    while stack:
        x = stack.pop()
        if isinstance(x, NoteRef):
            x.id = f"{x.id}@{tag}"
            continue
        for name in ("inlines", "blocks", "notes", "comments"):
            stack.extend(getattr(x, name, ()))
        for row in getattr(x, "rows", ()):
            stack.extend(row)
    for key in list(notes):
        notes[f"{key}@{tag}"] = notes.pop(key)


def _flat(blocks) -> str:
    parts = []
    for b in blocks:
        if isinstance(b, (Para, Heading, ListItem)):
            parts.append(_itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(_flat(c.blocks) for row in b.rows for c in row)
    return " ".join(re.sub(r"\s+", " ", p).strip() for p in parts if p.strip())


class _Frame:
    """One open inline scope inside a paragraph: root, link, inserted, deleted, hidden, drop (a page number's result
    in a header, left out) or reading (a ruby's, put after its base); a change's author."""

    def __init__(self, kind, url="", author=""):
        self.kind, self.url, self.author, self.inl = kind, url, author, []


class _State:
    def __init__(self):
        self.frames = [_Frame("root")]
        self.bold = False
        self.extras = []  # blocks that follow the paragraph: images, charts, text-box content
        self.fields = []  # open fldChar fields: [instr parts, phase, frame index or None, form text, result start]
        self.alt = ""


class _Scope:
    """The part being walked: its name, relationships and namespace prefixes."""

    def __init__(self, part, rels, nsmap):
        self.part, self.rels, self.nsmap = part, rels, nsmap


class _Reader:
    def __init__(self, pkg, part, ctx, chunk=0):
        self.pkg, self.part, self.ctx, self.chunk = pkg, part, ctx, chunk
        self.chunks = set()
        self.doc = Doc()
        self.styles, self.numbering = Styles(), Numbering()
        self.scope = None
        self.image_bytes = 0
        self.chart_cells = 0
        self.smartart_nodes = 0
        self.opened = 0  # tables opened so far (TABLE_NODES)
        self.budget = diagram.ConnectionBudget(ctx)  # the document's edges, shared by every group's diagram
        self.diagrams = True  # False while a header or footer is read: its text is flattened to one line

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
                    self.numbering.link(self.styles)
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
                    self.diagrams = False
                    try:
                        with self.ctx.guard(kind):
                            text = _flat(self.blocks(root, 0))
                    finally:
                        self.diagrams = True
                    if text:
                        store.append(text)
        root = self.open(self.part, "document")
        body = next(iter(kids(root, "body")), None)
        if body is None:
            raise ReadError("damaged", "no readable body")
        self.doc.blocks.extend(self.blocks(body, 0))
        if not self.chunk:
            with self.ctx.guard("properties"):
                self.properties()
        return self.doc

    # ---- document properties
    def properties(self):
        """docProps' core, extended and custom parts, by the package's own relationships: Doc.title, and the rest as
        Doc.meta["properties"] (word_fields.properties). Only what says something about the document is kept: no
        application, template or counts."""
        got = {}
        for rel in ooxml_base.rels(self.pkg, "", self.ctx).values():
            kind = next((k for k in _PROPERTY_PARTS if rel.type.endswith(k)), None)
            if kind is None or rel.external or not self.pkg.has(rel.target) or kind in got:
                continue
            try:
                got[kind] = ooxml_base.load(self.pkg, rel.target)[0]
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["properties"] += 1
        pairs = []
        core = got.get(_PROPERTY_PARTS[0])
        for el in (core if core is not None else ()):
            label = _CORE.get(local(el.tag))
            if label is None:
                continue
            text = (el.text or "").strip()
            if label == "title":
                self.doc.title = _squash(text, MAX_ALT)
            else:
                pairs.append((label, when(text) if label in ("created", "modified") else text))
        app = got.get(_PROPERTY_PARTS[1])
        company = first(app, "Company") if app is not None else None
        if company is not None:
            pairs.append(("company", company.text))
        for prop in kids(got[_PROPERTY_PARTS[2]], "property") if _PROPERTY_PARTS[2] in got else ():
            value = next(iter(prop), None)
            if value is not None:
                pairs.append((attr(prop, "name"), when(value.text or "") if local(value.tag) == "filetime" else value.text))
        if kept := properties(pairs):
            self.doc.meta["properties"] = kept

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
        if omml.is_math(c, "oMathPara"):
            return [Equation(t, display=True) for t in omml.lines(c)]
        if omml.is_math(c, "oMath"):
            t = omml.tex(c)
            return [Para([Math(t)])] if t else []
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
        if n == "altChunk":
            return self.alt_chunk(attr(c, "id"))
        return []

    def alt_chunk(self, rid) -> list:
        """An imported part (w:altChunk, its aFChunk relationship) read by the reader's own parser for its kind, by
        its bytes, then its name: a Word package as a docx body (its counters shared with this one, so the document's
        budgets hold), RTF (its pictures charged to IMAGE_BYTES with this document's), HTML, plain text. Its note ids
        are renamed (_renamed). A part is read once, so a package cannot read one part many times over; one read
        again, one past ALT_CHUNK_DEPTH levels, missing, unreadable or of another kind is counted under chunk."""
        rel = self.scope.rels.get(rid or "")
        data = None
        if rel is not None and not rel.external and rel.type.endswith("/aFChunk") and self.pkg.has(rel.target) \
                and rel.target not in self.chunks and self.chunk < limits.ALT_CHUNK_DEPTH:
            self.chunks.add(rel.target)
            try:
                data = self.pkg.read(rel.target)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
        if not data:
            self.ctx.skipped["chunk"] += 1
            return []
        name = rel.target.lower()
        head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:64].lower()
        tag = f"chunk{self.chunk + 1}-{rid}"
        if data.startswith(b"PK\x03\x04"):
            from .ooxml import _main_part
            with Package(data) as pkg:
                sub = _Reader(pkg, _main_part(pkg), self.ctx, self.chunk + 1)
                for k in _SHARED:
                    setattr(sub, k, getattr(self, k))
                doc = sub.run()
                for k in _SHARED:
                    setattr(self, k, getattr(sub, k))
        elif head.startswith(b"{\\rtf"):
            spent = {"images": self.image_bytes}
            doc = rtf.parse_bytes(data, self.ctx, spent)
            self.image_bytes = spent["images"]
        elif head.startswith((b"<!doctype", b"<html", b"<?xml")) or name.endswith((".htm", ".html", ".xhtml")):
            notes = {}
            blocks = xhtml.blocks(xhtml.tree(data), css=xhtml.Css([]), load=lambda _: None, notes=notes,
                                  ctx=self.ctx, part=tag)
            kept = {k: notes[k] for k in xhtml.referenced(blocks, notes)}
            for b in (blocks, *kept.values()):
                xhtml.settle(b, kept)
            self.doc.notes.update(kept)
            return blocks
        elif name.endswith(".txt"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = xhtml.decode(data) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else \
                    data.decode("cp1252", "replace")
            return [Para([Text(line)]) for line in (x.strip() for x in text.splitlines()) if line]
        else:
            self.ctx.skipped["chunk"] += 1
            return []
        _renamed(doc.blocks, doc.notes, tag)
        self.doc.notes.update(doc.notes)
        return doc.blocks

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
            elif f.kind == "inserted":
                parent.append(Inserted(f.inl, f.author))
            elif f.kind == "deleted":
                parent.append(Hidden(f.inl, "deleted", f.author))
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
        """A field's begin, separate or end. A HYPERLINK's result is a Link; a page number's (PAGE, NUMPAGES,
        SECTIONPAGES) is dropped in a header or footer. A legacy form field (w:ffData) shows its state at its end
        when its result shows nothing: a check box ☒ or ☐, a drop-down its chosen entry."""
        kind = attr(el, "fldCharType")
        if kind == "begin":
            if len(st.fields) < limits.MAX_DEPTH:
                st.fields.append([[], "code", None, self.form(first(el, "ffData")), None])
            else:
                self.deep()
        elif kind == "separate" and st.fields:
            f = st.fields[-1]
            f[1] = "result"
            f[4] = (st.frames[-1], len(st.frames[-1].inl))
            instr = "".join(f[0])
            url = hyperlink_url(instr)
            frame = _Frame("link", url) if url else _Frame("drop") if not self.diagrams and _PAGE_FIELD.match(instr) \
                else None
            if frame is not None:
                idx = len(st.frames)
                if self.push(st, frame):
                    f[2] = idx
        elif kind == "end" and st.fields:
            f = st.fields.pop()
            if f[2] is not None:
                self.close_to(st, f[2])
            if f[3] and (f[4] is None or not _itext(f[4][0].inl[f[4][1]:]).strip()):
                self.text(f[3], st)

    @staticmethod
    def form(ff) -> str:
        """What a legacy form field's w:ffData shows: ☒ or ☐ for a check box (w:checked, else w:default), a
        drop-down's entry at w:result (else w:default, else the first), "" for anything else."""
        if ff is None:
            return ""
        box = first(ff, "checkBox")
        if box is not None:
            mark = first(box, "checked")
            mark = mark if mark is not None else first(box, "default")
            return "☒" if mark is not None and _on(mark) else "☐"
        dd = first(ff, "ddList")
        if dd is None:
            return ""
        entries = [_val(e) or "" for e in kids(dd, "listEntry")]
        pick = first(dd, "result")
        pick = pick if pick is not None else first(dd, "default")
        i = _int(_val(pick), 0) if pick is not None else 0
        return _squash(entries[i]) if 0 <= i < len(entries) else ""

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
    @staticmethod
    def scope_hidden(st) -> str:
        """Why the run scope open now is hidden ("deleted" before "hidden"), or "": the blocks that follow the
        paragraph (display math, SmartArt, charts, pictures, text boxes) are hidden as the scope they sit in."""
        kinds = {f.kind for f in st.frames}
        return "deleted" if "deleted" in kinds else "hidden" if "hidden" in kinds else ""

    def extra(self, st, blocks) -> None:
        """Add blocks after the paragraph, hidden as the scope they sit in; a block deleted on its own stays so."""
        hidden = self.scope_hidden(st)
        for b in blocks:
            if hidden and b.hidden != "deleted":
                b.hidden = hidden
            st.extras.append(b)

    def node(self, el, st, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        if omml.is_math(el, "oMath"):
            t = omml.tex(el)
            if t:
                self.add(st, Math(t))
            return
        if omml.is_math(el, "oMathPara"):  # display lines follow the paragraph, hidden as the scope they sit in
            self.extra(st, [Equation(t, display=True) for t in omml.lines(el)])
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
            elif not self.diagrams and _PAGE_FIELD.match(attr(el, "instr") or ""):
                return
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
            self.scoped(st, el, d, _Frame("deleted", author=_author(el)))
        elif n in ("ins", "moveTo"):
            self.scoped(st, el, d, _Frame("inserted", author=_author(el)))
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
            st.alt = _squash((attr(pr[0], "descr") or attr(pr[0], "title") or "") if pr else old)
            for c in el:
                self.node(c, st, d + 1)
            st.alt = old
        elif n == "blip":
            self.picture(st, attr(el, "embed"))
        elif n == "imagedata":
            self.picture(st, attr(el, "id"), re.sub(r"\s+", " ", attr(el, "title") or "").strip()[:MAX_ALT])
        elif n == "chart":
            self.chart(st, attr(el, "id"))
        elif n == "relIds":
            self.extra(st, self.smartart(attr(el, "dm")))
        elif n == "txbxContent":
            self.extra(st, self.blocks(el, d + 1))
        elif n == "textpath":
            if text := _squash(attr(el, "string"), limits.MAX_OUTPUT):
                self.extra(st, [Para([Text(text)])])
        elif n == "sym":
            try:
                ch = symbol(attr(el, "font"), int(attr(el, "char") or "", 16))
            except ValueError:
                ch = None
            if ch:
                self.text(ch, st)
            else:
                self.ctx.skipped["symbol"] += 1
        elif n == "ruby":
            self.ruby(el, st, d)
        elif n in ("wgp", "wpc") and self.diagrams:
            self.group(el, st, d)
        elif n not in _SKIP:
            for c in el:
                self.node(c, st, d + 1)

    def ruby(self, el, st, d):
        """Ruby as its base text, then its reading in parentheses: 漢字(かんじ)."""
        for c in kids(el, "rubyBase"):
            for x in c:
                self.node(x, st, d + 1)
        k = len(st.frames)
        if not self.push(st, _Frame("reading")):
            return
        for c in kids(el, "rt"):
            for x in c:
                self.node(x, st, d + 1)
        self.close_to(st, k + 1)
        reading = _squash(_itext(st.frames.pop().inl), limits.MAX_OUTPUT)
        if reading:
            self.text(f"({reading})", st)

    # ---- drawing groups and canvases
    def group(self, el, st, d):
        """A group or canvas as one diagram scope: the blocks its members add after the paragraph become one slot each,
        and the diagram's flow replaces them there."""
        start, dia, slots = len(st.extras), diagram.Diagram(self.ctx, self.budget), []
        self.members(el, st, d, Xf().group(xfrm(el)), False, dia, slots, set())
        del st.extras[start:]
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = dia.place(slots)
        st.extras.extend(flow)

    def members(self, el, st, d, xf, hidden, dia, slots, keys):
        """Each child of a group at depth `d`: a nested group is walked into with its transform composed (the same
        scope), the chosen branch of an AlternateContent in place of it, anything else read as the paragraph reads it.
        A member of a hidden group, or a hidden `wps:wsp`, has its blocks hidden."""
        for c in el:
            n = local(c.tag)
            if n in ("grpSp", "wgp", "AlternateContent"):
                if d + 1 > limits.MAX_DEPTH:
                    self.deep()
                elif n == "AlternateContent":
                    chosen = self.choose(c)
                    if chosen is not None:
                        self.members(chosen, st, d + 1, xf, hidden, dia, slots, keys)
                else:
                    cnv = kid(c, "cNvPr")
                    hid = hidden or (cnv is not None and attr(cnv, "hidden") in ("1", "true"))
                    self.members(c, st, d + 1, xf.group(xfrm(c)), hid, dia, slots, keys)
                continue
            start = len(st.extras)
            self.node(c, st, d + 1)
            blocks = st.extras[start:]
            del st.extras[start:]
            cnv = kid(c, "cNvPr") if n == "wsp" else None
            hid = hidden or (cnv is not None and attr(cnv, "hidden") in ("1", "true"))
            if hid:
                for b in blocks:
                    if not b.hidden:
                        b.hidden = "hidden"
            key = attr(cnv, "id") if cnv is not None else None
            if key is None or key in keys:
                key = ("unkeyed", len(keys))
            keys.add(key)
            if n == "wsp":
                with self.ctx.guard("shape"):
                    self.member(c, key, blocks, xf, hid or bool(self.scope_hidden(st)), dia)
            if blocks:
                slots.append((key, blocks))

    @staticmethod
    def member(el, key, blocks, xf, hidden, dia):
        """One `wps:wsp` of the scope: a connector when it has `wps:cNvCnPr`, else a shape."""
        x, sppr = xfrm(el), kid(el, "spPr")
        if kid(el, "cNvCnPr") is not None:
            p0, p1 = xf.ends(x) or (None, None)
            st, en = glue(el)
            dia.connector(key, st, en, p0, p1, arrows(sppr), line=rule(sppr, (st, en)), hidden=hidden, blocks=blocks)
        else:
            dia.shape(key, blocks, kind(sppr), xf.box(x), hidden)

    def picture(self, st, rid, alt=None):
        img = ooxml_base.image(self.pkg, self.scope.part, rid)
        if img is not None:
            self.image_bytes += len(img.data)
        if img is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        self.extra(st, [Image(img.data, st.alt if alt is None else alt)])

    def chart(self, st, rid):
        tbl = ooxml_base.chart(self.pkg, self.scope.part, rid, self.ctx)
        if tbl is not None:
            self.chart_cells += sum(len(r) for r in tbl.rows)
        if tbl is None or self.chart_cells > limits.CHART_TOTAL:
            self.ctx.skipped["chart"] += 1
            return
        self.extra(st, [tbl])

    def smartart(self, rid):
        got = smartart.items(self.pkg, self.scope.part, rid, self.ctx, limits.SMARTART_NODES - self.smartart_nodes)
        self.smartart_nodes += len(got)
        return got

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
