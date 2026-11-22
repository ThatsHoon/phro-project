"""Tolerant XHTML/HTML walker with a small CSS cascade; epub and mobi use it.

`tree` builds a depth-limited Node tree with the stdlib HTMLParser, `Css` answers the two questions the
reader cares about (is this hidden, is this bold), and `blocks` turns a tree into document-model blocks.
MathML becomes TeX (`mathml`). A table of contents marks its targets with MARK and a depth digit, which
`promote` turns into headings (epub marks the tree, mobi the text).
This module is named html on purpose: reader/ itself must never be on sys.path, so the stdlib `html.parser`
below still resolves to the standard library.
"""
import base64
import binascii
import codecs
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import unquote

from . import limits
from .containers.zipx import resolve
from .css import MAX_SHEET, Css  # noqa: F401  (re-exported)
from .formats import omml
from .model import Bold, Break, Cell, Context, Heading, Hidden, Image, Link, ListItem, Math, NoteRef, Para, Quote, Table, Text

MAX_DOC = 16 * 2**20       # bytes of one document that are parsed
MAX_DATA = 5 * 2**20       # decoded bytes of one data: image
DATA_BUDGET = 64 * 2**20   # decoded data: bytes one blocks() call may emit
MAX_URL = 2000
MAX_ALT = 200
MAX_NODES = 500_000        # elements one document materializes

VOID = frozenset("area base basefont br col command embed frame hr img input isindex keygen link meta param "
                 "source track wbr".split())
CLOSES = {"p": {"p"}, "li": {"li", "p"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "td": {"td", "th"},
          "th": {"td", "th"}, "tr": {"td", "th", "tr"}, "option": {"option"}}
DROP = frozenset({"head", "script", "style", "template", "epub:case", "title", "meta", "link", "rp"})
BLOCKS = frozenset("p div section article aside header footer main figure figcaption body html dl dt dd form "
                   "fieldset address details summary hgroup center nav h1 h2 h3 h4 h5 h6 ul ol li table "
                   "blockquote pre hr img image svg epub:switch caption thead tbody tfoot tr td th".split())
DROPPED_NAV = frozenset({"toc", "landmarks", "page-list"})
NOTE_TYPES = frozenset({"footnote", "endnote", "rearnote", "note", "doc-footnote", "doc-endnote"})
EXTERNAL = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)
    deep: int = 0  # root only: tags dropped for nesting past MAX_DEPTH
    cut: bool = False    # root only: parsing stopped at MAX_NODES
    fault: bool = False  # root only: the parser raised and the tree is partial


@dataclass
class Ref(NoteRef):
    """A link that may point at a note. The caller keeps it as a note reference when the target is a
    note it collected and puts `inlines` (the link text) back otherwise."""
    inlines: list = field(default_factory=list)


# ---- decoding and tree building
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
_XMLDECL = re.compile(rb"^\s*<\?xml[^>]{0,200}?encoding\s*=\s*[\"']([\w.:\-]+)")
_META = re.compile(rb"<meta[^>]{0,300}?charset\s*=\s*[\"']?\s*([\w.:\-]+)", re.I)


def decode(data: bytes) -> str:
    """BOM, then the XML declaration's encoding, then <meta charset> in the first 2 KiB, else UTF-8."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    m = _XMLDECL.match(data[:300]) or _META.search(data[:2048])
    enc = "utf-8"
    if m:
        try:
            info = codecs.lookup(m.group(1).decode("ascii"))
            if info._is_text_encoding and not info.name.startswith(("utf-16", "utf-32")):
                enc = info.name
        except (LookupError, UnicodeError):
            pass
    try:
        return data.decode(enc, errors="replace")
    except (LookupError, UnicodeError):
        return data.decode("utf-8", errors="replace")


class _Stop(Exception):
    pass


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]
        self.over = 0  # open elements past MAX_DEPTH: not materialized, only counted
        self.buf = []  # text pieces since the last tag
        self.nodes = 0

    def stop(self):
        self.root.cut = True
        raise _Stop

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs)
        if tag in VOID:
            self._close(tag)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)
        self._close(tag)

    def handle_endtag(self, tag):
        self._close(tag)

    def handle_data(self, data):
        self.buf.append(data)

    def flush(self):
        if self.buf:
            self.stack[-1].children.append("".join(self.buf))
            self.buf.clear()

    def _open(self, tag, attrs):
        self.flush()
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.stop()
        if self.over or len(self.stack) > limits.MAX_DEPTH:
            self.over += 1
            self.root.deep += 1
            return
        closes = CLOSES.get(tag)
        while closes and len(self.stack) > 1 and self.stack[-1].tag in closes:
            self.stack.pop()
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def _close(self, tag):
        self.flush()
        if self.over:
            self.over -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


_SPECIAL = re.compile(r"[\"'<>]")


def _tag_end(s: str, i: int, dead: set) -> int:
    """Index after the '>' that closes the tag opened by the '<' at i, or -1 when it never closes: a '<' met
    outside quotes ends the attempt. `dead` remembers (position, quote) states known to fail, so a run of
    failures costs one pass in all."""
    p, q, trail = i + 1, "", []
    while (p, q) not in dead:
        trail.append((p, q))
        if q:
            j = s.find(q, p)
            if j < 0:
                break
            p, q = j + 1, ""
        else:
            m = _SPECIAL.search(s, p)
            if m is None or m.group() == "<":
                break
            if m.group() == ">":
                return m.end()
            p, q = m.end(), m.group()
    dead.update(trail)
    return -1


def _escape_lonely(s: str) -> str:
    """Text with every '<' that opens no complete tag or comment written as &lt;. The stdlib parser rescans
    such runs and goes quadratic on "<a <a <a ..."; quotes inside a tag are honoured so alt="a<b" survives."""
    parts, last, i, dead, no_comment_end = [], 0, 0, set(), False
    while True:
        i = s.find("<", i)
        if i < 0:
            break
        if s.startswith("<!--", i):
            j = -1 if no_comment_end else s.find("-->", i + 4)
            if j >= 0:
                i = j + 3
                continue
            no_comment_end = True
        else:
            end = _tag_end(s, i, dead)
            if end >= 0:
                i = end
                continue
        parts.append(s[last:i])
        parts.append("&lt;")
        last = i = i + 1
    parts.append(s[last:])
    return "".join(parts)


def tree(data: bytes) -> Node:
    """Node tree of a document, nesting past MAX_DEPTH flattened and parsing stopped at MAX_NODES elements
    (root.cut). A parser fault keeps what was built (root.fault)."""
    p = _Builder()
    try:
        p.feed(_escape_lonely(decode(data[:MAX_DOC])))
        p.close()
    except _Stop:
        pass
    except Exception:
        p.root.fault = True
    p.flush()
    return p.root


def text_of(node) -> str:
    """All text under node (script and style left out)."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            out.append(n)
        elif n.tag not in ("script", "style"):
            stack.extend(reversed(n.children))
    return "".join(out)


def find(root: Node, tag: str):
    """First element named tag in document order, or None."""
    stack = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, Node):
            if n.tag == tag:
                return n
            stack.extend(reversed(n.children))
    return None


# ---- helpers
def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def _int(v, default):
    m = re.match(r"\s*(\d{1,9})", v or "")
    return int(m.group(1)) if m else default


def _types(node) -> frozenset:
    return frozenset((node.attrs.get("epub:type") or node.attrs.get("role") or "").lower().split())


def _data_uri(src: str):
    """Decoded bytes of a base64 data: URI no bigger than MAX_DATA, else None (length checked before decoding)."""
    head, sep, payload = src.partition(",")
    if not sep or not head.lower().endswith(";base64") or len(payload) > MAX_DATA * 4 // 3 + 64:
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", unquote(payload) if "%" in payload else payload), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if len(data) <= MAX_DATA else None


def _tidy(items: list) -> list:
    """Merged, space-collapsed, edge-trimmed inlines."""
    res, buf = [], []

    def put():
        if buf:
            res.append(Text(re.sub(r" {2,}", " ", "".join(buf))))
            buf.clear()
    for it in items:
        if isinstance(it, Text):
            buf.append(it.text)
        else:
            put()
            res.append(it)
    put()

    def blank(x):
        return isinstance(x, Break) or (isinstance(x, Text) and not x.text.strip())
    i, j = 0, len(res)
    while i < j and blank(res[i]):
        i += 1
    while j > i and blank(res[j - 1]):
        j -= 1
    res = res[i:j]
    if res and isinstance(res[0], Text):
        res[0].text = res[0].text.lstrip()
    if res and isinstance(res[-1], Text):
        res[-1].text = res[-1].text.rstrip()
    return res


@dataclass
class _Cap:
    """A picture whose alt is one letter (an opening quote mark may come before it), held in its paragraph until the paragraph shows whether it opens a
    word (a drop cap: the letter stays and the picture goes) or not (the picture stays, after the paragraph)."""
    node: Node


def _cap_letter(node) -> str:
    alt = node.attrs.get("alt", "").strip()
    return alt if alt[-1:].isalpha() and (len(alt) == 1 or (len(alt) == 2 and alt[0] in "\"'“‘«「『")) else ""


def _uncap(items: list) -> list:
    out = []
    for it in items:
        if isinstance(it, _Cap):
            continue
        if hasattr(it, "inlines") and it.inlines:
            it.inlines[:] = _uncap(it.inlines)
            if not it.inlines:
                continue
        out.append(it)
    return out


def _caps(items: list, imgs: list) -> list:
    """items with every _Cap resolved: the one that leads the paragraph, with a letter right after it, becomes its
    letter and its picture leaves imgs; any other goes, its picture staying in imgs."""
    chain = [items]
    while chain[-1] and hasattr(chain[-1][0], "inlines"):
        chain.append(chain[-1][0].inlines)
    lead = chain[-1]
    if lead and isinstance(lead[0], _Cap):
        after = next((part[1] for part in reversed(chain) if len(part) > 1), None)
        if isinstance(after, Text) and after.text[:1].isalpha():
            node = lead[0].node
            lead[0] = Text(_cap_letter(node))
            imgs[:] = [im for im in imgs if im is not node]
    return _uncap(items)


class _Walk:
    def __init__(self, css, load, notes, ctx, part):
        self.css, self.load, self.notes, self.ctx, self.part = css, load, notes, ctx, part
        self.data_bytes = 0

    # ---- notes and hidden
    def note_key(self, href: str):
        file, _, frag = href.partition("#")
        if not frag or file.startswith("/") or "\\" in file:
            return None
        return (resolve(self.part, file) if file and self.part else self.part) + "#" + frag

    def take_note(self, k) -> bool:
        if not _types(k) & NOTE_TYPES:
            return False
        nid = k.attrs.get("id")
        key = (self.part + "#" + nid) if nid else None
        if key is None:
            self.ctx.skipped["note"] += 1
        elif key not in self.notes:
            nb = []
            self.flow(k.children, nb, 0, None)
            self.notes[key] = nb
        return True

    def hid(self, k) -> bool:
        return "hidden" in k.attrs or self.css.hidden(k, ())

    # ---- inlines
    def inl(self, k, cur, imgs):
        """Append the inline form of node k (or string) to cur; img elements go to imgs, or become alt text."""
        if isinstance(k, str):
            cur.append(Text(_ws(k)))
            return
        t = k.tag
        if t in DROP or self.take_note(k):
            return
        if self.hid(k):
            sub = []
            self.inl_inner(k, sub, imgs)
            if sub:
                cur.append(Hidden(sub))
            return
        self.inl_inner(k, cur, imgs)

    def inl_children(self, k, cur, imgs):
        for c in k.children:
            self.inl(c, cur, imgs)

    def inl_inner(self, k, cur, imgs):
        t = k.tag
        if t == "br":
            cur.append(Break())
        elif t in ("img", "image"):
            if imgs is not None:
                imgs.append(k)
                if _cap_letter(k):
                    cur.append(_Cap(k))
            elif k.attrs.get("alt"):
                cur.append(Text(k.attrs["alt"][:MAX_ALT]))
        elif t == "a":
            self.anchor(k, cur, imgs)
        elif t == "ruby":
            for c in k.children:
                if isinstance(c, Node) and c.tag in ("rt", "rtc"):
                    cur.append(Text("(" + _ws(text_of(c)).strip() + ")"))
                else:
                    self.inl(c, cur, imgs)
        elif _local(t) == "math":
            tex = mathml(k) or k.attrs.get("alttext", "").strip()[:2000]
            cur.append(Math(tex) if tex else Text(_ws(text_of(k))))
        elif t == "epub:switch":
            for c in k.children:
                if isinstance(c, Node) and c.tag == "epub:default":
                    self.inl_children(c, cur, imgs)
        elif t in ("b", "strong") or self.css.bold(k):
            sub = []
            self.inl_children(k, sub, imgs)
            if sub:
                cur.append(Bold(sub))
        else:
            if t in BLOCKS:
                cur.append(Text(" "))
            self.inl_children(k, cur, imgs)

    def anchor(self, k, cur, imgs):
        href = k.attrs.get("href", "").strip()
        m = _SCHEME.match(href)
        if m and m.group(1).lower() in EXTERNAL:
            url = re.sub(r"[\s()<>]", lambda x: "%%%02X" % ord(x.group()), href[:MAX_URL])
            sub = []
            self.inl_children(k, sub, imgs)
            cur.append(Link(url, sub))
            return
        key = None if m else self.note_key(href)
        if key is None:
            self.inl_children(k, cur, imgs)
            return
        sub = []
        self.inl_children(k, sub, imgs)
        cur.append(Ref(key, sub))

    def image(self, k):
        src = (k.attrs.get("src") or k.attrs.get("href") or k.attrs.get("xlink:href") or "").strip()
        data = None
        if src[:5].lower() == "data:":
            data = _data_uri(src)
            if data is not None:
                self.data_bytes += len(data)
                if self.data_bytes > DATA_BUDGET:
                    data = None
        elif src:
            data = self.load(src)
        if data is None:
            self.ctx.skipped["image"] += 1
            return None
        return Image(data, k.attrs.get("alt", "")[:MAX_ALT])

    # ---- blocks
    def flow(self, kids, out, lvl, li):
        """Blocks of a run of children. `li` is [marker] while the next paragraph is a list item's own text."""
        cur, imgs = [], []

        def flush():
            items = _tidy(_caps(_tidy(cur), imgs))
            if items:
                if li and li[0] is not None:
                    out.append(ListItem(lvl, li[0], items))
                    li[0] = None
                else:
                    out.append(Para(items))
            for im in imgs:
                b = self.image(im)
                if b is not None:
                    out.append(b)
            cur.clear()
            imgs.clear()
        for k in kids:
            if isinstance(k, str) or k.tag not in BLOCKS or k.tag in ("img", "image"):
                self.inl(k, cur, imgs)
                continue
            if k.tag in DROP or self.take_note(k):
                continue
            flush()
            if self.hid(k):
                tmp = []
                self.block(k, tmp, lvl, li)
                for b in tmp:
                    b.hidden = "hidden"
                out.extend(tmp)
            else:
                self.block(k, out, lvl, li)
        flush()

    def block(self, k, out, lvl, li):
        t = k.tag
        if t[0] == "h" and len(t) == 2 and t[1] in "123456":
            sub = []
            self.inl_children(k, sub, None)
            items = _tidy(sub)
            if items:
                out.append(Heading(int(t[1]), items))
        elif t in ("ul", "ol"):
            self.lst(k, out, lvl + (1 if li is not None else 0))
        elif t == "li":
            self.item(k, out, lvl, "-")
        elif t == "table":
            self.table(k, out)
        elif t == "blockquote":
            inner = []
            self.flow(k.children, inner, 0, None)
            if inner:
                out.append(Quote(inner))
        elif t == "pre":
            lines = text_of(k).replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
            items = []
            for i, ln in enumerate(lines):
                if i:
                    items.append(Break())
                if ln:
                    items.append(Text(ln))
            if any(isinstance(x, Text) and x.text.strip() for x in items):
                out.append(Para(items))
        elif t == "hr":
            pass
        elif t in ("img", "image"):
            b = self.image(k)
            if b is not None:
                out.append(b)
        elif t == "nav":
            if not _types(k) & DROPPED_NAV:
                self.flow(k.children, out, lvl, li)
        elif t == "epub:switch":
            for c in k.children:
                if isinstance(c, Node) and c.tag == "epub:default":
                    self.flow(c.children, out, lvl, li)
        else:
            self.flow(k.children, out, lvl, li)

    def lst(self, k, out, lvl):
        ordered = k.tag == "ol"
        n = _int(k.attrs.get("start"), 1)
        for c in k.children:
            if isinstance(c, str):
                if MARK in c:
                    self.flow([c], out, lvl, None)
                continue
            if self.take_note(c) or c.tag in DROP:
                continue
            if c.tag == "li":
                self.item(c, out, lvl, f"{n}." if ordered else "-")
                n += 1
            else:
                self.flow([c], out, lvl, None)

    def item(self, c, out, lvl, marker):
        tmp = []
        self.flow(c.children, tmp, lvl, [marker])
        if self.hid(c):
            for b in tmp:
                b.hidden = "hidden"
        out.extend(tmp)

    def table(self, k, out):
        rows, caption = [], ""

        def collect(n, d):
            nonlocal caption
            for c in n.children:
                if isinstance(c, str):
                    continue
                if c.tag == "tr":
                    rows.append(c)
                elif c.tag == "caption":
                    caption = _ws(text_of(c)).strip()[:MAX_ALT]
                elif c.tag in ("thead", "tbody", "tfoot") and d < 2:
                    collect(c, d + 1)
        collect(k, 0)
        cells = [[c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")] for tr in rows]
        width = max(max((len(r) for r in cells), default=0), 1)  # a span never reaches past the real grid
        built, hidden_rows = [], set()
        for r, row in enumerate(cells):
            if self.hid(rows[r]):
                hidden_rows.add(r)
            line = []
            for c in row:
                rs = _int(c.attrs.get("rowspan"), 1)
                rs = len(cells) - r if rs == 0 else min(rs, len(cells) - r)
                blocks = []
                self.flow(c.children, blocks, 0, None)
                line.append(Cell(blocks, max(1, rs), min(max(1, _int(c.attrs.get("colspan"), 1)), width), c.tag == "th"))
            built.append(line)
        if any(built):
            out.append(Table(built, caption, frozenset(hidden_rows)))


def blocks(root: Node, *, css: Css, load: Callable[[str], bytes | None], notes: dict, ctx: Context,
           part: str = "") -> list:
    """Document-model blocks of a tree. Footnotes are collected into `notes` under "<part>#<id>"; `part` is
    the document's own name, used to resolve cross-file note references."""
    w = _Walk(css, load, notes, ctx, part)
    out = []
    w.flow((find(root, "body") or root).children, out, 0, None)
    for kind, n, msg in (("depth", root.deep, "skipped content nested too deeply"),
                         ("node", root.cut, "stopped at the element limit of one document"),
                         ("parse", root.fault, "the parser failed; part of a document is missing")):
        if n:
            ctx.skipped[kind] += int(n)
            if ctx.skipped[kind] == int(n):
                ctx.warnings.append(f"{kind}: {msg}")
    return out


# ---- MathML
_BIG_OPS = frozenset("∑∏∐⋀⋁⋂⋃⨀⨁⨂⨄⨆∫∬∭∮∯∰⨌")
_LIMIT_NAMES = frozenset({"lim", "liminf", "limsup", "max", "min", "sup", "inf", "det", "gcd", "Pr"})
_SILENT = frozenset({"annotation", "annotation-xml", "none", "mprescripts", "mspace", "malignmark", "maligngroup"})


_PRESENTED = frozenset({"mi", "mn", "mo", "mtext", "ms", "annotation"})
_VARIANT = {"bold": "\\mathbf", "double-struck": "\\mathbb", "script": "\\mathcal", "fraktur": "\\mathfrak",
            "sans-serif": "\\mathsf", "monospace": "\\mathtt", "bold-italic": "\\boldsymbol",
            "bold-script": "\\mathcal", "bold-fraktur": "\\mathfrak"}


def _local(tag: str) -> str:
    return tag.rpartition(":")[2]


def _args(node) -> list:
    return [c for c in node.children if isinstance(c, Node)]


def _token(node) -> str:
    return _ws(text_of(node)).strip()


def mathml(node) -> str:
    """TeX of a MathML element without delimiters: a TeX annotation of `semantics` as written, else the presentation
    markup through the TeX buffer the Office Math reader writes with. Past limits.MAX_DEPTH an element is its text.
    "" for a tree with no presentation token and no annotation (Content MathML): its text is not the formula."""
    stack = [node]
    while stack:
        n = stack.pop()
        if _local(n.tag) in _PRESENTED:
            break
        stack.extend(_args(n))
    else:
        return ""
    out = omml.Tex()
    _mm(node, out, 0)
    return out.finish()


def _mm_arg(node, depth) -> omml.Tex:
    out = omml.Tex()
    _mm(node, out, depth)
    return out


def _big(node) -> bool:
    """An operator that takes its under and over parts as limits: a large operator or a limit-like function name."""
    return _local(node.tag) in ("mo", "mi") and (_token(node) in _BIG_OPS or _token(node) in _LIMIT_NAMES)


def _mm(n, out, depth):
    if isinstance(n, str):
        if n.strip():
            out.push_math_text(_ws(n).strip())
        return
    if depth > limits.MAX_DEPTH:
        out.push_math_text(_token(n))
        return
    t, a = _local(n.tag), _args(n)
    if t in ("mi", "mn", "mo"):
        s = _token(n)
        variant = n.attrs.get("mathvariant", "").strip().lower()
        font = _VARIANT.get(variant) or ("\\mathrm" if variant == "normal" and t == "mi" and len(s) > 1 else None)
        name = omml.function_name(s) if t != "mn" and len(s) > 1 and not font else None
        if name:
            out.push_macro(name)
        elif font and s:
            inner = omml.Tex()
            inner.push_math_text(s)
            out.push_command(font, inner)
        else:
            out.push_math_text(s)
    elif t in ("mtext", "ms"):
        s = _token(n)
        if s:
            out.push_text_mode(s)
    elif t == "mfrac" and len(a) >= 2:
        num, den = _mm_arg(a[0], depth + 1), _mm_arg(a[1], depth + 1)
        if n.attrs.get("linethickness", "").strip() in ("0", "0px", "0em", "0pt"):
            out.push_char("{")
            out.push_tex(num)
            out.push_macro("\\atop")
            out.push_tex(den)
            out.push_char("}")
        else:
            out.push_macro("\\frac")
            out.push_group(num)
            out.push_group(den)
    elif t == "msqrt":
        inner = omml.Tex()
        for c in n.children:
            _mm(c, inner, depth + 1)
        out.push_command("\\sqrt", inner)
    elif t == "mroot" and len(a) >= 2:
        out.push_macro("\\sqrt")
        out.push_char("[")
        out.push_tex(_mm_arg(a[1], depth + 1))
        out.push_char("]")
        out.push_group(_mm_arg(a[0], depth + 1))
    elif t in ("msub", "msup", "msubsup", "munder", "mover", "munderover") and len(a) >= 2:
        _scripts(t, a, out, depth)
    elif t == "mmultiscripts" and a:
        rest = a[1:]
        cut = next((i for i, c in enumerate(rest) if _local(c.tag) == "mprescripts"), len(rest))
        _pairs(rest[cut + 1:], out, depth, "{}")
        out.push_base(_mm_arg(a[0], depth + 1))
        _pairs(rest[:cut], out, depth, "")
    elif t == "mfenced":
        seps = "".join(n.attrs.get("separators", ",").split())
        out.push_math_text(n.attrs.get("open", "("))
        for i, c in enumerate(a):
            if i and seps:
                out.push_math_text(seps[min(i - 1, len(seps) - 1)])
            _mm(c, out, depth + 1)
        out.push_math_text(n.attrs.get("close", ")"))
    elif t == "mtable":
        out.push_str("\\begin{matrix}")
        rows = [r for r in a if _local(r.tag) in ("mtr", "mlabeledtr")]
        for i, row in enumerate(rows):
            if i:
                out.push_str(" \\\\")
            cells = [c for c in _args(row) if _local(c.tag) == "mtd"][1 if _local(row.tag) == "mlabeledtr" else 0:]
            for j, cell in enumerate(cells):
                out.push_str(" & " if j else " ")
                out.push_tex(_mm_arg(cell, depth + 2))
        out.push_str(" \\end{matrix}")
    elif t == "semantics":
        tex = next((_token(c) for c in a if _local(c.tag) == "annotation"
                    and "tex" in c.attrs.get("encoding", "").lower()), "")
        if tex:
            out.push_str(tex)
        elif a:
            _mm(a[0], out, depth + 1)
    elif t not in _SILENT:
        for c in n.children:
            _mm(c, out, depth + 1)


def _scripts(t, a, out, depth):
    base = _mm_arg(a[0], depth + 1)
    big = _big(a[0])
    if t in ("munder", "mover") and not big and _local(a[1].tag) == "mo":
        cmd = omml.accent(_token(a[1]))
        if cmd:
            out.push_command(cmd, base)
            return
    lower = a[1] if t in ("msub", "msubsup", "munder", "munderover") else None
    upper = a[1] if t in ("msup", "mover") else a[2] if len(a) > 2 and t in ("msubsup", "munderover") else None
    if t[1] == "s" or big:
        out.push_base(base)
        for mark, part in (("_", lower), ("^", upper)):
            if part is not None:
                out.push_char(mark)
                out.push_group(_mm_arg(part, depth + 1))
        return
    for cmd, part in (("\\underset", lower), ("\\overset", upper)):
        if part is not None:
            wrapped = omml.Tex()
            wrapped.push_macro(cmd)
            wrapped.push_group(_mm_arg(part, depth + 1))
            wrapped.push_group(base)
            base = wrapped
    out.push_tex(base)


def _pairs(parts, out, depth, lead):
    """The (subscript, superscript) pairs of mmultiscripts; `none` leaves its side out."""
    for i in range(0, len(parts) - 1, 2):
        out.push_str(lead)
        for mark, part in (("_", parts[i]), ("^", parts[i + 1])):
            if _local(part.tag) != "none":
                out.push_char(mark)
                out.push_group(_mm_arg(part, depth + 1))


# ---- headings from a table of contents
MARK = "\x0e"
_MARK = re.compile("\x0e([0-5])")


def book_title(raw: str, limit: int) -> str:
    """A book title on one line, cut at limit; the placeholder "Unknown" (Calibre's, for a book with no title) is no
    title."""
    title = " ".join(raw.split())[:limit]
    return "" if title.casefold() == "unknown" else title


def strip_marks(blocks):
    """Every heading marker (MARK and its depth digit) removed from every text under blocks: paragraphs, headings, list
    items, quotes, table cells and captions, and the inlines inside inlines."""
    work = [blocks]
    while work:
        for b in work.pop():
            if isinstance(b, Table):
                b.caption = _MARK.sub("", b.caption)
                work.extend(c.blocks for row in b.rows for c in row)
            if hasattr(b, "blocks"):
                work.append(b.blocks)
            todo = [b]
            while todo:
                o = todo.pop()
                if isinstance(o, Image):
                    o.alt = _MARK.sub("", o.alt)
                    continue
                if not hasattr(o, "inlines"):
                    continue
                for it in o.inlines:
                    if isinstance(it, Text):
                        it.text = _MARK.sub("", it.text)
                    elif hasattr(it, "inlines"):
                        todo.append(it)


def _mark_depth(inlines):
    """The depth digit of the marker a run of inlines starts with, else None."""
    while inlines:
        first = inlines[0]
        if isinstance(first, Text):
            m = _MARK.match(first.text)
            return int(m.group(1)) if m else None
        if not hasattr(first, "inlines"):
            return None
        inlines = first.inlines
    return None


def _blank(inlines) -> bool:
    todo = [inlines]
    while todo:
        for it in todo.pop():
            if isinstance(it, Text):
                if it.text.strip():
                    return False
            elif hasattr(it, "inlines"):
                todo.append(it.inlines)
            elif not isinstance(it, Break):
                return False
    return True


def promote(blocks: list) -> list:
    """A paragraph or list item whose text starts with a heading marker becomes Heading(depth + 1), its one bold run
    unwrapped. One that is only a marker (the position fell between two tags) gives its depth to the paragraph or list
    item after it and goes. Every marker is then stripped from every inline, headings and table cells included."""
    out, pending = [], None
    for b in blocks:
        if isinstance(b, (Para, ListItem)):
            d = _mark_depth(b.inlines)
            strip_marks([b])
            if d is not None and _blank(b.inlines):
                pending = d
                continue
            if d is None:
                d = pending
            if d is not None:
                items = b.inlines
                while items and isinstance(items[0], Text) and not items[0].text.strip():
                    del items[0]
                if len(items) == 1 and isinstance(items[0], Bold) and not items[0].hidden:
                    items = items[0].inlines
                if items and isinstance(items[0], Text):
                    items[0].text = items[0].text.lstrip()
                b = Heading(d + 1, items, hidden=b.hidden)
        else:
            strip_marks([b])
        pending = None
        out.append(b)
    return out


def _walk_blocks(blocks, visit):
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


def referenced(blocks, notes) -> list:
    """Keys of the notes reachable from `blocks` through Ref links, in first-use order; a set beside the list keeps
    it linear in the notes (mobi reaches it with up to MOBI_ANCHORS targets)."""
    found, seen, work = [], set(), [blocks]

    def visit(o):
        for it in o.inlines:
            if isinstance(it, Ref) and it.id in notes and it.id not in seen:
                seen.add(it.id)
                found.append(it.id)
                work.append(notes[it.id])
    while work:
        _walk_blocks(work.pop(), visit)
    return found


def settle(blocks, notes):
    """Ref links that point at a kept note become note references; the rest go back to their link text."""
    def visit(o):
        out = []
        for it in o.inlines:
            if not isinstance(it, Ref):
                out.append(it)
            elif it.id in notes:
                out.append(NoteRef(it.id, hidden=it.hidden))
            else:
                out.extend(it.inlines)
        o.inlines[:] = out
    _walk_blocks(blocks, visit)
