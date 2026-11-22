"""EPUB to the document model: one chapter section per spine document, in reading order.

The package is read through Package; every href is resolved inside it and anything that points outside
(`../`, absolute paths, URL schemes) counts as missing. The table of contents (the EPUB 3 nav, else the NCX) makes
headings: each target's block, when its text is the entry's label, is marked in the tree and becomes a heading of the
entry's depth (`html.promote`); a chapter that already holds the marker byte is read without them.
"""
import re
from urllib.parse import unquote

from .. import html as xhtml
from .. import limits
from ..containers.zipx import Package, attr, first, kids, parse_xml, resolve, walk
from ..limits import ReadError
from ..model import Doc, Section

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


def _text_is(node, want: str, work: list) -> bool:
    """Whether the text under node, white space collapsed and case folded, is `want`; it stops reading once the text
    or the walk outgrows want, or the book's `work` (nodes left, in a one-item list) is spent."""
    chars, steps = len(want.replace(" ", "")), 4 * len(want) + 256
    out, seen, stack = [], 0, [node]
    while stack:
        steps -= 1
        work[0] -= 1
        if steps < 0 or work[0] < 0:
            return False
        n = stack.pop()
        if isinstance(n, str):
            seen += len("".join(n.split()))
            if seen > chars:
                return False
            out.append(n)
        elif n.tag not in ("script", "style"):
            stack.extend(reversed(n.children))
    return " ".join("".join(out).split()).casefold() == want


def _labelled(node, want: str, work: list):
    """The deepest of node and its leading blocks (each the first content of the one before) whose text is `want`,
    a label with its white space collapsed and its case folded."""
    found = None
    for _ in range(limits.MAX_DEPTH):
        if not want or work[0] < 0:
            break
        if _text_is(node, want, work):
            found = node
        lead = next((c for c in node.children if not isinstance(c, str) or c.strip()), None)
        if not isinstance(lead, xhtml.Node) or lead.tag not in xhtml.BLOCKS:
            break
        node = lead
    return found


def _mark(root, targets, work: list) -> bool:
    """A heading marker (html.MARK and the depth digit) put first in the block each table-of-contents target names:
    the element its fragment names, climbed to the nearest block, or for a target with no fragment the body; then down
    its leading blocks to the deepest whose text is the target's label. A target whose label matches no such block
    marks nothing, and a block is searched for a label once. True when any marker was put. `work` is the book's
    nodes left for the search (`_text_is`); it is under 0 when the search stopped for it."""
    want = {frag for frag, _, _ in targets if frag}
    ids, parent, stack = {}, {}, [root]
    while want and stack:
        n = stack.pop()
        i = n.attrs.get("id")
        if i in want and i not in ids:
            ids[i] = n
        kids_ = [c for c in n.children if isinstance(c, xhtml.Node)]
        for c in kids_:
            parent[id(c)] = n
        stack.extend(reversed(kids_))
    body = xhtml.find(root, "body") or root
    marked, tried = set(), set()
    for frag, depth, label in targets:
        node = ids.get(frag) if frag else body
        while node is not None and node is not body and node.tag not in xhtml.BLOCKS:
            node = parent.get(id(node))
        want = " ".join(label.split()).casefold()
        if node is None or (id(node), want) in tried:
            continue
        if work[0] < 0:
            break
        tried.add((id(node), want))
        node = _labelled(node, want, work)
        if node is not None and id(node) not in marked:
            marked.add(id(node))
            node.children.insert(0, xhtml.MARK + str(min(max(depth, 0), 5)))
    return bool(marked)


class _Book:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.notes = {}
        self.sheets = {}   # member -> stylesheet text
        self.css = {}      # tuple of sheet texts -> Css
        self.image_bytes = 0
        self.targets = {}  # member -> [(fragment or None, depth, label)]
        self.toc_work = [limits.TOC_WORK]

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

    # ---- table of contents
    def toc(self, opf, root) -> dict:
        """{member: [(fragment or None, depth, label)]} of the book's table of contents, in order: the EPUB 3 nav
        document's toc nav, else the NCX. At most limits.TOC_ITEMS entries; one that cannot be read gives none,
        counted."""
        nav = ncx = None
        man = first(root, "manifest")
        for it in kids(man, "item") if man is not None else []:
            href = _inside(opf, it.get("href", ""))
            if nav is None and "nav" in it.get("properties", "").split():
                nav = href
            elif ncx is None and it.get("media-type", "").split(";")[0].strip().lower() == "application/x-dtbncx+xml":
                ncx = href
        entries = []
        for name, read in ((nav, self.nav_entries), (ncx, self.ncx_entries)):
            if entries or name is None or not self.pkg.has(name):
                continue
            try:
                entries = read(name)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.warn("toc", "a table of contents could not be read; its headings are not marked")
        out: dict = {}
        for base, href, depth, label in entries:
            part = _inside(base, href)
            if part is not None and label:
                frag = unquote(href.partition("#")[2]) or None
                out.setdefault(part, []).append((frag, depth, label[:MAX_TITLE]))
        return out

    def nav_entries(self, name) -> list:
        root, toc = xhtml.tree(self.pkg.read(name)), None
        stack = [root]
        while stack and toc is None:
            n = stack.pop()
            if n.tag == "nav" and {"toc", "doc-toc"} & set(
                    (n.attrs.get("epub:type", "") + " " + n.attrs.get("role", "")).lower().split()):
                toc = n
            stack.extend(c for c in reversed(n.children) if isinstance(c, xhtml.Node))
        stack, out = ([(toc, -1)] if toc is not None else []), []
        while stack and len(out) < limits.TOC_ITEMS:
            n, depth = stack.pop()
            if n.tag == "a":
                href = n.attrs.get("href", "").strip()
                if href:
                    out.append((name, href, max(depth, 0), " ".join(xhtml.text_of(n).split())))
                continue
            depth += n.tag in ("ol", "ul")
            stack.extend((c, depth) for c in reversed(n.children) if isinstance(c, xhtml.Node))
        return out

    def ncx_entries(self, name) -> list:
        nav_map = first(parse_xml(self.pkg.read(name)), "navMap")
        stack, out = ([(p, 0) for p in reversed(kids(nav_map, "navPoint"))] if nav_map is not None else []), []
        while stack and len(out) < limits.TOC_ITEMS:
            point, depth = stack.pop()
            label = next(iter(kids(point, "navLabel")), None)
            content = next(iter(kids(point, "content")), None)
            src = (attr(content, "src") or "").strip() if content is not None else ""
            if src:
                out.append((name, src, depth, " ".join("".join(label.itertext()).split()) if label is not None else ""))
            stack.extend((p, depth + 1) for p in reversed(kids(point, "navPoint")))
        return out

    # ---- chapters
    def run(self) -> Doc:
        opf, root = self.package()
        doc = Doc()
        title = first(root, "title")
        doc.title = xhtml.book_title(title.text or "", MAX_TITLE) if title is not None else ""
        parts = self.spine(opf, root)
        self.check_drm(parts)
        self.targets = self.toc(opf, root)
        for number, part in enumerate(parts, 1):
            if self.ctx.late():
                break
            try:
                with self.ctx.guard("chapter"):
                    doc.blocks.append(self.chapter(part, number))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.warn("chapter", "skipped damaged part")
        if not doc.blocks:
            raise ReadError("damaged", "no readable chapter")
        keep = xhtml.referenced(doc.blocks, self.notes)
        doc.notes = {k: self.notes[k] for k in keep}
        self.ctx.skipped["note"] += len(self.notes) - len(doc.notes)
        xhtml.settle(doc.blocks, doc.notes)
        for nb in doc.notes.values():
            xhtml.settle(nb, doc.notes)
        return doc

    def chapter(self, part, number) -> Section:
        raw = self.pkg.read(part)
        if len(raw) > xhtml.MAX_DOC:
            self.warn("truncated", "a chapter was cut at the size limit")
        node = xhtml.tree(raw)
        spent = self.toc_work[0] < 0
        marked = xhtml.MARK.encode() not in raw and _mark(node, self.targets.get(part, ()), self.toc_work)
        if self.toc_work[0] < 0 and not spent:
            self.ctx.skipped["toc"] += 1
        known = len(self.notes)
        blocks = xhtml.blocks(node, css=self.stylesheet(part, node), load=lambda src: self.load(part, src),
                              notes=self.notes, ctx=self.ctx, part=part)
        if marked:
            blocks = xhtml.promote(blocks)
            for key in list(self.notes)[known:]:
                xhtml.strip_marks(self.notes[key])
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
                if e.kind not in ("damaged", "too_large"):  # a stylesheet over a limit is skipped
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
            if e.kind not in ("damaged", "too_large"):  # an image or object over a limit is skipped
                raise
            return None
        self.image_bytes += len(data)
        return data if self.image_bytes <= IMAGE_BYTES else None


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Book(pkg, ctx).run()
