"""EPUB to the document model: one chapter section per spine document, in reading order.

The package is read through Package; every href is resolved inside it and anything that points outside
(`../`, absolute paths, URL schemes) counts as missing.
"""
import re

from .. import html as xhtml
from ..containers.zipx import Package, attr, first, kids, parse_xml, resolve, walk
from ..limits import ReadError
from ..model import Doc, NoteRef, Section, Table

IMAGE_BYTES = 256 * 2**20  # image bytes one book may emit, copies counted
MAX_SHEETS = 32            # stylesheets one chapter may use
MAX_TITLE = 300
DOCUMENT_TYPES = frozenset({"application/xhtml+xml", "text/html", "image/svg+xml", "application/x-dtbook+xml"})
FREE_ALGORITHMS = frozenset({"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"})
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def _inside(base: str, href: str):
    """Package member name for href as seen from member `base`, or None when it leaves the package."""
    href = href.strip()
    if not href or "\\" in href or href.startswith(("/", "//")) or _SCHEME.match(href):
        return None
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None
    name = resolve(base, href)
    return None if name == ".." or name.startswith("../") or not name else name


def _walk(blocks, visit):
    """Call visit(inline_list, owner) for the inline list of every block under `blocks` and every inline
    container inside those lists, without recursion."""
    stack = [blocks]
    while stack:
        for b in stack.pop():
            todo = [b]
            while todo:
                o = todo.pop()
                if hasattr(o, "inlines"):
                    visit(o)
                    todo.extend(i for i in o.inlines if hasattr(i, "inlines"))
            if hasattr(b, "blocks"):
                stack.append(b.blocks)
            if isinstance(b, Table):
                stack.extend(c.blocks for row in b.rows for c in row)


def _referenced(blocks, notes) -> list:
    """Keys of the notes reachable from `blocks` through Ref links, in first-use order."""
    found, work = [], [blocks]

    def visit(o):
        for it in o.inlines:
            if isinstance(it, xhtml.Ref) and it.id in notes and it.id not in found:
                found.append(it.id)
                work.append(notes[it.id])
    while work:
        _walk(work.pop(), visit)
    return found


def _settle(blocks, notes):
    """Ref links that point at a kept note become note references; the rest go back to their link text."""
    def visit(o):
        out = []
        for it in o.inlines:
            if not isinstance(it, xhtml.Ref):
                out.append(it)
            elif it.id in notes:
                out.append(NoteRef(it.id, hidden=it.hidden))
            else:
                out.extend(it.inlines)
        o.inlines[:] = out
    _walk(blocks, visit)


class _Book:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.notes = {}
        self.sheets = {}   # member -> stylesheet text
        self.css = {}      # tuple of sheet texts -> Css
        self.image_bytes = 0

    def warn(self, kind, msg):
        self.ctx.skipped[kind] += 1
        if self.ctx.skipped[kind] == 1:
            self.ctx.warnings.append(f"{kind}: {msg}")

    # ---- package
    def package(self):
        container = self.pkg.xml("META-INF/container.xml")
        if container is None:
            raise ReadError("damaged", "not an EPUB package")
        if self.pkg.has("META-INF/rights.xml"):
            raise ReadError("drm", "rights.xml")
        full = next((attr(r, "full-path") for r in walk(container, "rootfile") if attr(r, "full-path")), None)
        opf = resolve("", full) if full else ""
        if not opf or not self.pkg.has(opf):
            raise ReadError("damaged", "no package document")
        return opf, parse_xml(self.pkg.read(opf))

    def spine(self, opf, root) -> list:
        """Spine documents in reading order, linear first, one entry per resolved member."""
        items = {}
        man = first(root, "manifest")
        for it in kids(man, "item") if man is not None else []:
            iid = it.get("id")
            if iid and iid not in items:
                href = it.get("href", "")
                items[iid] = (_inside(opf, href), it.get("media-type", "").split(";")[0].strip().lower(), it.get("fallback"))
        sp = first(root, "spine")
        refs = [(r.get("idref"), r.get("linear", "yes").lower() != "no") for r in kids(sp, "itemref")] if sp is not None else []
        parts, seen = [], set()
        for idref, linear in sorted(refs, key=lambda r: not r[1]):  # stable: nonlinear items move to the end
            part, visited, cur = None, set(), idref
            while cur in items and cur not in visited:
                visited.add(cur)
                href, media, fallback = items[cur]
                if href and (media in DOCUMENT_TYPES or (not media and href.lower().endswith((".xhtml", ".html", ".htm")))):
                    part = href
                    break
                cur = fallback
            if part is None:
                self.warn("spine", "skipped a spine item with no readable document type")
            elif part.lower() not in seen:
                seen.add(part.lower())
                parts.append(part)
        return parts

    def check_drm(self, parts):
        if not self.pkg.has("META-INF/encryption.xml"):
            return
        try:
            enc = parse_xml(self.pkg.read("META-INF/encryption.xml"))
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self.warn("encryption", "encryption.xml unreadable")
            return
        spine = {p.lower() for p in parts}
        for data in walk(enc, "EncryptedData"):
            method, ref = first(data, "EncryptionMethod"), first(data, "CipherReference")
            uri = attr(ref, "URI") if ref is not None else None
            if uri and (attr(method, "Algorithm") if method is not None else "") not in FREE_ALGORITHMS \
                    and resolve("", uri).lower() in spine:
                raise ReadError("drm", "encrypted chapter")

    # ---- chapters
    def run(self) -> Doc:
        opf, root = self.package()
        doc = Doc()
        title = first(root, "title")
        doc.title = " ".join((title.text or "").split())[:MAX_TITLE] if title is not None else ""
        parts = self.spine(opf, root)
        self.check_drm(parts)
        for number, part in enumerate(parts, 1):
            try:
                with self.ctx.guard("chapter"):
                    doc.blocks.append(self.chapter(part, number))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.warn("chapter", "skipped damaged part")
        if not doc.blocks:
            raise ReadError("damaged", "no readable chapter")
        keep = _referenced(doc.blocks, self.notes)
        doc.notes = {k: self.notes[k] for k in keep}
        self.ctx.skipped["note"] += len(self.notes) - len(doc.notes)
        _settle(doc.blocks, doc.notes)
        for nb in doc.notes.values():
            _settle(nb, doc.notes)
        return doc

    def chapter(self, part, number) -> Section:
        raw = self.pkg.read(part)
        if len(raw) > xhtml.MAX_DOC:
            self.warn("truncated", "a chapter was cut at the size limit")
        node = xhtml.tree(raw)
        blocks = xhtml.blocks(node, css=self.stylesheet(part, node), load=lambda src: self.load(part, src),
                              notes=self.notes, ctx=self.ctx, part=part)
        return Section("chapter", number=number, blocks=blocks)

    def stylesheet(self, part, node) -> xhtml.Css:
        texts, stack = [], [node]
        while stack and len(texts) < MAX_SHEETS:
            n = stack.pop()
            if not isinstance(n, xhtml.Node):
                continue
            if n.tag == "style":
                texts.append("".join(c for c in n.children if isinstance(c, str)))
            elif n.tag == "link" and "stylesheet" in n.attrs.get("rel", "").lower().split():
                text = self.sheet(part, n.attrs.get("href", ""))
                if text is not None:
                    texts.append(text)
            stack.extend(reversed(n.children))
        key = tuple(texts)
        if key not in self.css:
            self.css[key] = xhtml.Css(list(key))
            if self.css[key].capped:
                self.warn("css", "stylesheet limits reached; part of the rules were not read")
        return self.css[key]

    def sheet(self, base, href):
        name = _inside(base, href)
        if name is None or not self.pkg.has(name):
            self.ctx.skipped["css"] += 1
            return None
        if name not in self.sheets:
            try:
                self.sheets[name] = xhtml.decode(self.pkg.read(name)[:xhtml.MAX_SHEET * 4])
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.sheets[name] = None
        return self.sheets[name]

    def load(self, base, src):
        name = _inside(base, src)
        if name is None or not self.pkg.has(name):
            return None
        try:
            data = self.pkg.read(name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            return None
        self.image_bytes += len(data)
        return data if self.image_bytes <= IMAGE_BYTES else None


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Book(pkg, ctx).run()
