"""XPS / OpenXPS to the document model, written from the specification.

Fixed-layout pages carry positioned glyph runs; they are turned back into ordered paragraphs, either in the order
a StoryFragments part gives or by geometry. Elements match by local name, so both namespaces work.
"""
import math
import unicodedata

from .. import limits
from ..containers.zipx import Package, attr, kids, local, parse_xml, resolve
from ..limits import ReadError
from ..model import Doc, Image, Para, Section, Text
from ..images import sniff
from . import ooxml_base

IMAGE_BYTES = 256 * 2**20  # image bytes one package may emit, copies counted
MAX_RUN = 100_000          # characters one Glyphs run may emit
COORD = 1e9                # coordinates and matrix terms are clamped / refused beyond this
MIN_SIZE = 1e-3
MAX_REFS = 1_000_000      # NameReferences one StoryFragments part may index
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _num(v, default=0.0, limit=COORD):
    """A finite float within bounds, else `default` (missing, junk, NaN and inf alike)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return max(-limit, min(limit, f))


def _matrix(s):
    """Six finite, bounded numbers as a tuple, or None."""
    if not isinstance(s, str):
        return None
    parts = s.replace(",", " ").split()
    if len(parts) != 6:
        return None
    try:
        m = tuple(float(p) for p in parts)
    except ValueError:
        return None
    if not all(math.isfinite(v) and abs(v) <= COORD for v in m):
        return None
    return m


def _compose(own, parent):
    """`own` applied first, then `parent`; None when the result leaves the finite range."""
    a, b, c, d, e, f = own
    pa, pb, pc, pd, pe, pf = parent
    m = (a * pa + b * pc, a * pb + b * pd, c * pa + d * pc, c * pb + d * pd,
         e * pa + f * pc + pe, e * pb + f * pd + pf)
    return m if all(math.isfinite(v) and abs(v) <= 1e30 for v in m) else None


def _lines(runs):
    """[(y, size, text)] from runs (y, x, size, text, name): sorted by (y, x), grouped by |dy| <= 0.5 size."""
    out, cur, anchor, lsize = [], [], 0.0, 0.0
    for r in sorted(runs, key=lambda r: (r[0], r[1])):
        if cur and r[0] - anchor <= 0.5 * max(lsize, r[2]):
            cur.append(r)
            lsize = max(lsize, r[2])
            continue
        if cur:
            out.append((anchor, lsize, _join(cur)))
        cur, anchor, lsize = [r], r[0], r[2]
    if cur:
        out.append((anchor, lsize, _join(cur)))
    return out


def _extent(text, size):
    """Estimated width of a run: wide East Asian characters are one em, others half."""
    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in "WF")
    return (wide + (len(text) - wide) * 0.5) * size


def _join(line):
    return " ".join(r[3] for r in sorted(line, key=lambda r: r[1]))


def _paras(lines, out):
    """Consecutive lines join with a space; a whitespace gap over 1 x the line's size (dy over 2 x size) starts a new Para."""
    prev, buf = None, []
    for y, size, text in lines:
        if prev is not None and (y - prev) - size > size and buf:
            out.append(Para([Text(" ".join(buf))]))
            buf = []
        buf.append(text)
        prev = y
    if buf:
        out.append(Para([Text(" ".join(buf))]))


class _Reader:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.image_bytes = 0
        self.chars = 0
        self.stories = {}   # part -> (paragraphs, name index) or None
        self.images = {}    # part -> bytes or None
        self.no_unicode = 0

    def count(self, kind, warning=None):
        self.ctx.skipped[kind] += 1
        if warning and self.ctx.skipped[kind] == 1:
            self.ctx.warnings.append(warning)

    def xml(self, name):
        if not self.pkg.has(name):
            raise ReadError("damaged", f"missing part {name[:80]}")
        return parse_xml(self.pkg.read(name))

    # ---- package chain
    def run(self) -> Doc:
        seq = None
        for rel in ooxml_base.rels(self.pkg, "", self.ctx).values():
            if rel.type.lower().endswith("/fixedrepresentation") and not rel.external and self.pkg.has(rel.target):
                seq = rel.target
                break
        if seq is None:
            raise ReadError("damaged", "not an XPS package")
        try:
            seq_root = self.xml(seq)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            raise ReadError("damaged", "not an XPS package")
        doc, listed, ok_docs, seen_docs, seen_pages = Doc(), 0, 0, set(), set()
        for ref in kids(seq_root, "DocumentReference"):
            fdoc = resolve(seq, attr(ref, "Source", ""))
            if fdoc in seen_docs:
                continue
            seen_docs.add(fdoc)
            try:
                with self.ctx.guard("document"):
                    fdoc_root = self.xml(fdoc)
                    ok_docs += 1
                    for page in self.pages(fdoc, fdoc_root, seen_pages):
                        listed += 1
                        self.add_page(doc, page, fdoc)
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["document"] += 1
        if self.no_unicode:
            doc.warnings.append(f"{self.no_unicode} text runs without Unicode")
        if seen_docs and not ok_docs:
            raise ReadError("damaged", "no readable document")
        if listed and not doc.blocks:
            raise ReadError("damaged", "no readable page")
        return doc

    def pages(self, fdoc, root, seen):
        out = []
        for pc in kids(root, "PageContent"):
            name = resolve(fdoc, attr(pc, "Source", ""))
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def add_page(self, doc, part, fdoc):
        try:
            with self.ctx.guard("page"):
                doc.blocks.append(self.page(part, fdoc))
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self.ctx.skipped["page"] += 1

    # ---- one page
    def page(self, part, fdoc) -> Section:
        root = self.xml(part)
        if local(root.tag) != "FixedPage":
            raise ReadError("damaged", "not a page")
        width = _num(attr(root, "Width"), 0.0)
        res = {}
        for rd in root.iter():
            if local(rd.tag) == "ResourceDictionary":
                for ch in rd:
                    k = attr(ch, "Key")
                    if k is not None and k not in res:
                        res[k] = ch
        runs, images = [], []
        self.walk(root, part, res, runs, images)
        sec = Section("page")
        self.text_blocks(sec.blocks, runs, width, [part, fdoc])
        seen = set()
        for src in images:
            if src in seen:
                continue
            seen.add(src)
            img = self.image(part, src)
            if img is not None:
                sec.blocks.append(img)
        return sec

    def transform(self, el, res):
        """The element's own RenderTransform matrix, IDENTITY when absent or unusable (counted)."""
        s = attr(el, "RenderTransform")
        if s is None:
            for c in el:
                if local(c.tag).endswith(".RenderTransform"):
                    mt = next((g for g in c if local(g.tag) == "MatrixTransform"), None)
                    s = attr(mt, "Matrix") if mt is not None else ""
                    break
        if s is None:
            return IDENTITY
        s = s.strip()
        if s.startswith("{"):
            parts = s.strip("{}").split(None, 1)
            target = res.get(parts[1].strip()) if len(parts) == 2 and parts[0] == "StaticResource" else None
            s = attr(target, "Matrix") if target is not None and local(target.tag) == "MatrixTransform" else None
        m = _matrix(s)
        if m is None:
            self.count("transform", "transform: unusable RenderTransform treated as identity")
            return IDENTITY
        return m

    def walk(self, root, part, res, runs, images):
        stack = [(root, IDENTITY, 0)]
        while stack:
            el, m, d = stack.pop()
            name = local(el.tag)
            if name == "Glyphs":
                self.glyphs(el, m, res, runs)
                continue
            if name == "ImageBrush":
                src = self.image_src(attr(el, "ImageSource"))
                if src:
                    images.append(src)
            if name == "Canvas":
                own = self.transform(el, res)
                composed = _compose(own, m)
                if composed is None:
                    self.count("transform", "transform: unusable RenderTransform treated as identity")
                else:
                    m = composed
            if d >= limits.MAX_DEPTH:
                if len(el):
                    self.count("depth", "depth: skipped content nested too deeply")
                continue
            for c in reversed(el):
                stack.append((c, m, d + 1))

    @staticmethod
    def image_src(v):
        if not v:
            return None
        v = v.strip()
        if v.startswith("{"):  # {ColorConvertedBitmap /image /profile}
            parts = v.strip("{}").split()
            v = parts[1] if len(parts) > 1 else ""
        return v or None

    def glyphs(self, el, m, res, runs):
        text = attr(el, "UnicodeString")
        if text is None:
            self.no_unicode += 1
            self.ctx.skipped["glyphs"] += 1
            return
        if text.startswith("{}"):
            text = text[2:]
        if len(text) > MAX_RUN:
            self.count("text", "text: long run cut")
        text = " ".join(text[:MAX_RUN].split())
        if not text:
            return
        if self.chars + len(text) > limits.MAX_OUTPUT:
            self.count("text", "text: output limit reached, later text dropped")
            return
        self.chars += len(text)
        own = self.transform(el, res)
        full = _compose(own, m)
        if full is None:
            self.count("transform", "transform: unusable RenderTransform treated as identity")
            full = m
        a, b, c, d, e, f = full
        x0, y0 = _num(attr(el, "OriginX")), _num(attr(el, "OriginY"))
        em = _num(attr(el, "FontRenderingEmSize"), 1.0)
        if em <= 0:
            em = 1.0
        size = max(em * math.hypot(c, d), MIN_SIZE)
        x = _num(a * x0 + c * y0 + e)
        y = _num(b * x0 + d * y0 + f)
        runs.append((y, x, size, text, attr(el, "Name")))

    # ---- ordering
    def text_blocks(self, blocks, runs, width, owners):
        if not runs:
            return
        emitted = set()
        by_name = {}
        for i, r in enumerate(runs):
            if r[4] and r[4] not in by_name:
                by_name[r[4]] = i
        stories = self.story_parts(owners)
        if by_name and stories:
            for paras, index in stories:
                hits = sorted({(p, pos, n) for n in by_name for p, pos in index.get(n, ())})
                texts, cur = [], None
                for p, pos, n in hits + [(None, 0, None)]:
                    if p != cur:
                        if texts:
                            blocks.append(Para([Text(" ".join(texts))]))
                        texts, cur = [], p
                    i = by_name.get(n)
                    if i is not None and i not in emitted:
                        emitted.add(i)
                        texts.append(runs[i][3])
            runs = [r for i, r in enumerate(runs) if i not in emitted]
            if not runs:
                return
        for stream in self.columns(runs, width):
            _paras(_lines(stream), blocks)

    def columns(self, runs, width):
        """[runs] in reading order: [left, right] for a clean two-column page, else [all]."""
        if width > 0 and len(runs) >= 6:
            mid, band = width / 2, width * 0.05
            lo, hi = mid - band, mid + band
            if not any(r[1] < hi and r[1] + _extent(r[3], r[2]) > lo for r in runs):
                left = [r for r in runs if r[1] < mid]
                right = [r for r in runs if r[1] >= mid]
                if len(_lines(left)) >= 3 and len(_lines(right)) >= 3:
                    return [left, right]
        return [runs]

    def story_parts(self, owners):
        out, seen = [], set()
        for owner in owners:
            for rel in ooxml_base.rels(self.pkg, owner, self.ctx).values():
                if (rel.type.lower().endswith("/storyfragments") and not rel.external
                        and rel.target not in seen and self.pkg.has(rel.target)):
                    seen.add(rel.target)
                    story = self.story(rel.target)
                    if story:
                        out.append(story)
        return out

    def story(self, part):
        """(paragraphs, {name: [(paragraph, position)]}) of a StoryFragments part, cached; None when unreadable.

        Each NamedElement belongs to its nearest enclosing ParagraphStructure; the walk is one pass, no recursion.
        """
        if part not in self.stories:
            story = None
            try:
                root = self.xml(part)
                paras, index, total = [], {}, 0
                stack = [(root, None)]
                while stack:
                    el, cur = stack.pop()
                    name = local(el.tag)
                    if name == "ParagraphStructure":
                        cur = len(paras)
                        paras.append(0)
                    elif name == "NamedElement" and cur is not None:
                        ref = attr(el, "NameReference")
                        if ref:
                            if total >= MAX_REFS:
                                self.count("story", "story: references beyond the limit ignored")
                                break
                            index.setdefault(ref, []).append((cur, paras[cur]))
                            paras[cur] += 1
                            total += 1
                    for c in reversed(el):
                        stack.append((c, cur))
                story = (paras, index)
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.count("story", "story: damaged StoryFragments ignored")
            self.stories[part] = story
        return self.stories[part]

    # ---- images
    def image(self, page, src):
        if "\\" in src:
            self.ctx.skipped["image"] += 1
            return None
        name = resolve(page, src)
        if name not in self.images:
            data = None
            if self.pkg.has(name):
                try:
                    data = self.pkg.read(name)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
            self.images[name] = data if data and sniff(data) else None
        data = self.images[name]
        if data is not None:
            self.image_bytes += len(data)
        if data is None or self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return None
        return Image(data)


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
