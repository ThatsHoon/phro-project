"""HWPX (Hancom Office, zip + XML) to the document model. Elements match by local name."""
import fnmatch
import re
from bisect import bisect_left

from .. import limits
from ..charts import chart_table
from ..containers.zipx import Package, attr, first, kids, local, parse_xml, resolve
from ..limits import ReadError
from ..model import (
    Bold, Break, Cell, Doc, Equation, Heading, Hidden, Image, Link, ListItem,
    Math, NoteRef, Para, Table, Text,
)

HPF = "Contents/content.hpf"
_MIME = "application/hwp+zip"
_CASE_NS = {"http://www.hancom.co.kr/hwpml/2016/paragraph", "http://www.hancom.co.kr/hwpml/2016/ooxmlchart"}
_AUTO_ALT = re.compile(r"^(그림|사각형|선|타원|글상자|표|수식)입니다\.?$")
_STYLE_HEAD = re.compile(r"^(?:개요|outline|heading)\s*(\d+)$", re.I)
_BORDERS = ("leftBorder", "rightBorder", "topBorder", "bottomBorder")
_SYLLABLE = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi pi rho sigma "
          "tau upsilon phi chi psi omega").split()
_GREEK_CAP = ("Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega")
_WORDS = {"times": "\\times", "le": "\\le", "ge": "\\ge",
          **{g: "\\" + g for g in _GREEK}, **{g: "\\" + g for g in _GREEK_CAP}}
_TEX_DEPTH = 32


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# ---- equation script to TeX
def _operand(toks, i, depth):
    """One operand starting at toks[i]: ((text, inner-or-None), next index)."""
    if i >= len(toks):
        return ("", None), i
    if toks[i] == "{" and depth < _TEX_DEPTH:
        items, j = _group(toks, i + 1, depth + 1)
        inner = _join(items)
        return ("{" + inner + "}", inner), j
    t = toks[i]
    return (_WORDS.get(t, t), None), i + 1


def _group(toks, i, depth):
    out = []
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "}":
            if depth:
                return out, i
            continue
        if t == "{" and depth < _TEX_DEPTH:
            items, i = _group(toks, i, depth + 1)
            inner = _join(items)
            out.append(("{" + inner + "}", inner))
        elif t == "over" and out and i < len(toks):
            (den, dinner), i = _operand(toks, i, depth)
            s, inner = out.pop()
            out.append((f"\\frac{{{inner if inner is not None else s}}}{{{dinner if dinner is not None else den}}}", None))
        elif t == "sqrt" and i < len(toks):
            (s, inner), i = _operand(toks, i, depth)
            out.append((f"\\sqrt{{{inner if inner is not None else s}}}", None))
        else:
            out.append((_WORDS.get(t, t), None))
    return out, i


def _join(items):
    out = ""
    for s, _ in items:
        out += s if not out or s in "^_" or out[-1] in "^_" else " " + s
    return out


def _tex(script: str) -> str:
    toks = re.findall(r"[{}^_]|[^\s{}^_]+", script or "")
    items, _ = _group(toks, 0, 0)
    return _join(items)


# ---- numbering markers
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
    if kind == "CIRCLED_DIGIT":
        return chr(0x2460 + n - 1) if n <= 20 else str(n)
    if kind == "ROMAN_CAPITAL":
        return _roman(n)
    if kind == "ROMAN_SMALL":
        return _roman(n).lower()
    if kind == "LATIN_CAPITAL":
        return _bijective(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if kind == "LATIN_SMALL":
        return _bijective(n, "abcdefghijklmnopqrstuvwxyz")
    if kind == "HANGUL_SYLLABLE":
        return _bijective(n, _SYLLABLE)
    if kind == "HANGUL_JAMO":
        return _bijective(n, _JAMO)
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


def _remove(inlines, obj) -> bool:
    for i, it in enumerate(inlines):
        if it is obj:
            del inlines[i]
            return True
        if isinstance(it, (Bold, Link, Hidden)) and _remove(it.inlines, obj):
            return True
    return False


def _strip_marker(inlines, marker) -> bool:
    """Drop a leading `marker` from the first text; True when the first text was found."""
    for it in inlines:
        if isinstance(it, Text):
            t = it.text.lstrip()
            if t.startswith(marker):
                it.text = t[len(marker):].lstrip()
            return True
        if isinstance(it, Bold) and _strip_marker(it.inlines, marker):
            return True
        if isinstance(it, (Link, Hidden, Math, NoteRef, Break)):
            return True
    return False


def _flat(blocks) -> str:
    parts = []
    for b in blocks:
        if isinstance(b, (Para, Heading, ListItem)):
            parts.append(_itext(b.inlines))
        elif isinstance(b, Table):
            parts.extend(_flat(c.blocks) for row in b.rows for c in row)
    return " ".join(re.sub(r"\s+", " ", p).strip() for p in parts if p.strip())


def _cut_command(s: str) -> str:
    m = re.search(r"(?<!\\);", s)
    return re.sub(r"\\([;:])", r"\1", s[:m.start()] if m else s).strip()


class _Header:
    """Lookup tables from Contents/header.xml."""

    def __init__(self, root):
        self.para, self.style, self.numbering, self.plain, self.bold = {}, {}, {}, {}, set()
        if root is None:
            return
        for el in root.iter():
            name, eid = local(el.tag), attr(el, "id")
            if eid is None:
                continue
            if name == "paraPr":
                h = kids(el, "heading")
                self.para[eid] = ((attr(h[0], "type", "NONE").upper(), attr(h[0], "idRef", "0"),
                                   _int(attr(h[0], "level"))) if h else ("NONE", "0", 0))
            elif name == "style":
                self.style[eid] = (attr(el, "name", "").strip(), attr(el, "paraPrIDRef"))
            elif name == "numbering":
                self.numbering[eid] = {
                    _int(attr(ph, "level")): ((ph.text or "").strip(), (attr(ph, "numFormat") or "DIGIT").upper(),
                                              _int(attr(ph, "start"), 1))
                    for ph in kids(el, "paraHead")}
            elif name == "borderFill":
                types = [attr(b[0], "type", "") for n in _BORDERS if (b := kids(el, n))]
                self.plain[eid] = len(types) == 4 and all(t.upper() == "NONE" for t in types)
            elif name == "charPr" and kids(el, "bold"):
                self.bold.add(eid)


class _Frame:
    """One open inline scope inside a paragraph: root, link, pass, drop or deleted."""

    def __init__(self, kind, fid=None, url=""):
        self.kind, self.id, self.url, self.inl = kind, fid, url, []


class _State:
    def __init__(self, nohead):
        self.nohead = nohead
        self.frames = [_Frame("root")]
        self.bold = False
        self.extras = []
        self.eqs = []


class _Reader:
    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.doc = Doc()
        self.hdr = _Header(None)
        self.images = {}
        self.counts = {"fn": 0, "en": 0, "cm": 0}
        self.numbers = {}
        self.cache = {}
        self.charts = {}

    # ---- package
    def find(self, href):
        for cand in (resolve("x", href or ""), resolve(HPF, href or "")):
            if href and self.pkg.has(cand):
                return cand
        return None

    def part_bytes(self, href, kind):
        name = self.find(href)
        if name is None:
            self.ctx.skipped[kind] += 1
            return None
        if name not in self.cache:
            try:
                self.cache[name] = self.pkg.read(name)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):  # an image or object over a limit is skipped
                    raise
                self.ctx.skipped[kind] += 1
                self.cache[name] = None
        return self.cache[name]

    def run(self):
        pkg = self.pkg
        mime = pkg.read("mimetype").decode("utf-8", "replace").strip() if pkg.has("mimetype") else ""
        if mime != _MIME and not pkg.has(HPF):
            raise ReadError("damaged", "not a hwpx package")
        if pkg.has("META-INF/manifest.xml") and b"encryption-data" in pkg.read("META-INF/manifest.xml").lower():
            raise ReadError("encrypted")
        items = []
        if pkg.has(HPF):
            root = pkg.xml(HPF)
            items = [(attr(i, "id"), attr(i, "href", "")) for i in root.iter() if local(i.tag) == "item"]
        self.images = {i: h for i, h in items if i}
        hrefs = [h for _, h in items if "section" in h.lower() and h.lower().endswith(".xml")]
        names = []
        for h in hrefs:
            n = self.find(h)
            if n is None:
                self.ctx.skipped["section"] += 1
            elif n not in names:
                names.append(n)
        if not hrefs:
            names = sorted((n for n in pkg.names if fnmatch.fnmatch(n.lower(), "contents/section*.xml")),
                           key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n.lower())])
        if pkg.has("Contents/header.xml"):
            try:
                self.hdr = _Header(pkg.xml("Contents/header.xml"))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["styles"] += 1  # header.xml holds the style and numbering tables
        good = 0
        for name in names:
            with self.ctx.guard("section"):
                try:
                    root = pkg.xml(name)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["section"] += 1
                    continue
                self.doc.blocks.extend(self.blocks(root, 0, False))
                good += 1
        if not good:
            raise ReadError("damaged", "no readable section")
        return self.doc

    # ---- blocks
    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def pick(self, sw):
        for c in kids(sw, "case"):
            if attr(c, "required-namespace") in _CASE_NS:
                return c
        d = kids(sw, "default")
        return d[0] if d else None

    def blocks(self, container, d, nohead):
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        for c in container:
            n = local(c.tag)
            if n == "p":
                out.extend(self.para(c, d, nohead))
            elif n == "switch":
                chosen = self.pick(c)
                if chosen is not None:
                    out.extend(self.blocks(chosen, d + 1, nohead))
        return out

    def sub(self, el, d, nohead=True):
        out = []
        for sl in kids(el, "subList"):
            out.extend(self.blocks(sl, d + 1, nohead))
        return out

    def para(self, p, d, nohead):
        st = _State(nohead)
        for c in p:
            self.node(c, st, d + 1)
        self.close_to(st, 1)
        inl = st.frames[0].inl
        out = []
        if st.eqs and not _itext(inl).strip():
            for obj in st.eqs:
                _remove(inl, obj)
            out.extend(Equation(o.tex, True) for o in st.eqs)
        text = re.sub(r"\s+", " ", _itext(inl)).strip()
        htype, hid, hlvl = self.hdr.para.get(attr(p, "paraPrIDRef"), ("NONE", "0", 0))
        level = 0
        if not nohead:
            if htype == "OUTLINE":
                level = min(hlvl + 1, 6)
            else:
                level = self.style_level(attr(p, "styleIDRef"))
            if level and (len(text) > 120 or (len(text) > 40 and text[-1] in ".?!")):
                level = 0
        main = None
        if level and text:
            main = Heading(level, inl)
        elif htype in ("NUMBER", "BULLET"):
            marker = self.marker(hid, hlvl) if htype == "NUMBER" else "-"
            _strip_marker(inl, marker)
            if inl:
                main = ListItem(min(hlvl, 6), marker, inl)
        elif inl:
            main = Para(inl)
        return ([main] if main else []) + out + st.extras

    def style_level(self, sid):
        name = self.hdr.style.get(sid, ("", None))[0]
        if m := _STYLE_HEAD.match(name):
            return min(max(int(m.group(1)), 1), 6)
        low = name.lower()
        if low in ("제목", "title"):
            return 1
        if low in ("부제목", "subtitle"):
            return 2
        return 0

    def marker(self, nid, lvl):
        levels = self.hdr.numbering.get(nid, {})
        cnt = self.numbers.setdefault(nid, {})
        head = levels.get(lvl + 1)
        cnt[lvl] = cnt.get(lvl, (head[2] if head else 1) - 1) + 1
        for k in [k for k in cnt if k > lvl]:
            del cnt[k]

        def sub(m):
            n = int(m.group(1))
            h = levels.get(n)
            return _fmt(cnt.get(n - 1, h[2] if h else 1), h[1] if h else "DIGIT")

        fmt = head[0] if head and head[0] else f"^{lvl + 1}."
        return re.sub(r"\^(\d)", sub, fmt).strip()[:64] or "-"

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
            parent = st.frames[-1].inl
            if f.kind == "link" and f.inl:
                parent.append(Link(f.url, f.inl))
            elif f.kind == "deleted" and f.inl:
                parent.append(Hidden(f.inl, "deleted"))
            elif f.kind == "pass":
                parent.extend(f.inl)

    def push(self, st, frame):
        if len(st.frames) < limits.MAX_DEPTH:
            st.frames.append(frame)

    def field_begin(self, el, st):
        kind = (attr(el, "type") or "").upper()
        fid = attr(el, "id")
        if kind == "HYPERLINK":
            params = {attr(p, "name"): (p.text or "") for p in el.iter() if local(p.tag) == "stringParam"}
            url = _cut_command(params["Command"]) if params.get("Command") else (params.get("Path") or "").strip()
            self.push(st, _Frame("link" if url else "pass", fid, url))
        elif kind == "CLICK_HERE":
            self.push(st, _Frame("drop", fid))

    def field_end(self, el, st):
        ref = attr(el, "beginIDRef")
        for i in range(len(st.frames) - 1, 0, -1):
            f = st.frames[i]
            if f.kind in ("link", "pass", "drop") and (ref is None or f.id == ref):
                self.close_to(st, i)
                return

    def delete_end(self, st):
        for i in range(len(st.frames) - 1, 0, -1):
            if st.frames[i].kind == "deleted":
                self.close_to(st, i)
                return

    # ---- paragraph content
    def node(self, el, st, d):
        if d > limits.MAX_DEPTH:
            self.deep()
            return
        n = local(el.tag)
        if n == "run":
            old, st.bold = st.bold, attr(el, "charPrIDRef") in self.hdr.bold
            for c in el:
                self.node(c, st, d + 1)
            st.bold = old
        elif n == "t":
            self.text(el.text, st)
            for c in el:
                self.node(c, st, d + 1)
                self.text(c.tail, st)
        elif n == "tab":
            self.text("\t", st)
        elif n == "lineBreak":
            self.add(st, Break())
        elif n in ("fwSpace", "nbSpace"):
            self.text(" ", st)
        elif n == "fieldBegin":
            self.field_begin(el, st)
        elif n == "fieldEnd":
            self.field_end(el, st)
        elif n == "deleteBegin":
            self.push(st, _Frame("deleted"))
        elif n == "deleteEnd":
            self.delete_end(st)
        elif n == "switch":
            chosen = self.pick(el)
            for c in (chosen if chosen is not None else ()):
                self.node(c, st, d + 1)
        elif n == "tbl":
            st.extras.extend(self.table(el, d))
        elif n == "pic":
            self.picture(el, st)
        elif n == "chart":
            self.chart(el, st)
        elif n == "equation":
            s = first(el, "script")
            tex = _tex(s.text or "") if s is not None else ""
            if tex:
                m = Math(tex)
                st.eqs.append(m)
                self.add(st, m)
        elif n in ("footNote", "endNote"):
            key = "fn" if n == "footNote" else "en"
            self.counts[key] += 1
            nid = f"{key}-{self.counts[key]}"
            self.doc.notes[nid] = self.sub(el, d)
            self.add(st, NoteRef(nid))
        elif n == "hiddenComment":
            self.counts["cm"] += 1
            nid = f"cm-{self.counts['cm']}"
            blocks = self.sub(el, d)
            if blocks and isinstance(blocks[0], Para):
                blocks[0].inlines.insert(0, Text("Comment: "))
            else:
                blocks.insert(0, Para([Text("Comment:")]))
            self.doc.notes[nid] = blocks
            self.add(st, NoteRef(nid))
        elif n in ("header", "footer"):
            text = _flat(self.sub(el, d))
            if text:
                (self.doc.headers if n == "header" else self.doc.footers).append(text)
        elif n == "drawText":
            st.extras.extend(self.sub(el, d, st.nohead))
        elif n != "secPr":
            for c in el:
                self.node(c, st, d + 1)

    def picture(self, pic, st):
        img = first(pic, "img")
        data = self.part_bytes(self.images.get(attr(img, "binaryItemIDRef")), "image") if img is not None else None
        if not data:
            return
        sc = first(pic, "shapeComment")
        alt = re.sub(r"\s+", " ", sc.text or "").strip() if sc is not None else ""
        st.extras.append(Image(data, "" if _AUTO_ALT.match(alt) else alt))

    def chart(self, el, st):
        href = attr(el, "chartIDRef")
        name = self.find(href)
        if name not in self.charts:
            data = self.part_bytes(href, "chart")
            tbl = None
            if data:
                try:
                    tbl = chart_table(parse_xml(data), self.ctx)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["chart"] += 1
            self.charts[name] = tbl
        if self.charts[name] is not None:
            st.extras.append(self.charts[name])

    # ---- tables
    def table(self, tbl, d):
        raw = []  # (row, col, rowspan, colspan, Cell)
        plain = True
        seen = {}
        for r, tr in enumerate(kids(tbl, "tr")):
            for tc in kids(tr, "tc"):
                addr = kids(tc, "cellAddr")
                span = kids(tc, "cellSpan")
                row = _int(attr(addr[0], "rowAddr"), r) if addr else r
                col = _int(attr(addr[0], "colAddr"), seen.get(row, 0)) if addr else seen.get(row, 0)
                seen[row] = seen.get(row, 0) + 1
                cs = max(1, _int(attr(span[0], "colSpan"), 1)) if span else 1
                rs = max(1, _int(attr(span[0], "rowSpan"), 1)) if span else 1
                blocks = []
                for sl in kids(tc, "subList"):
                    blocks.extend(self.blocks(sl, d + 1, True))
                raw.append((row, col, rs, cs, Cell(blocks, header=attr(tc, "header") == "1")))
                plain = plain and self.hdr.plain.get(attr(tc, "borderFillIDRef"), False)
        if not raw:
            return []
        # the grid keeps only rows and columns where a cell starts; a span counts the kept ones it covers
        rows_at, cols_at = sorted({x[0] for x in raw}), sorted({x[1] for x in raw})
        ri = {v: i for i, v in enumerate(rows_at)}
        ci = {v: i for i, v in enumerate(cols_at)}
        grid = [[] for _ in range(len(rows_at))]
        for row, col, rs, cs, cell in raw:
            r, c = ri[row], ci[col]
            cell.rowspan = bisect_left(rows_at, row + rs) - r
            cell.colspan = bisect_left(cols_at, col + cs) - c
            grid[r].append((c, cell))
        nrows, ncols = len(rows_at), len(cols_at)
        rows = [[cell for _, cell in sorted(g, key=lambda x: x[0])] for g in grid]
        if plain and (nrows == 1 or ncols == 1):
            return [b for row in rows for c in row for b in c.blocks]
        return [Table(rows)]


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return _Reader(pkg, ctx).run()
