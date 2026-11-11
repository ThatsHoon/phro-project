"""Tolerant XHTML/HTML walker with a small CSS cascade; epub uses it and mobi will.

`tree` builds a depth-limited Node tree with the stdlib HTMLParser, `Css` answers the two questions the
reader cares about (is this hidden, is this bold), and `blocks` turns a tree into document-model blocks.
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
from .model import Bold, Break, Cell, Context, Heading, Hidden, Image, Link, ListItem, Math, NoteRef, Para, Quote, Table, Text

MAX_DOC = 16 * 2**20       # bytes of one document that are parsed
MAX_SHEET = 2**20          # characters of one stylesheet that are read
MAX_SELECTORS = 5000       # simple selectors one Css keeps
MAX_DATA = 5 * 2**20       # decoded bytes of one data: image
DATA_BUDGET = 64 * 2**20   # decoded data: bytes one blocks() call may emit
MAX_URL = 2000
MAX_ALT = 200

VOID = frozenset("area base basefont br col command embed frame hr img input isindex keygen link meta param "
                 "source track wbr".split())
CLOSES = {"p": {"p"}, "li": {"li", "p"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "td": {"td", "th"},
          "th": {"td", "th"}, "tr": {"td", "th", "tr"}, "option": {"option"}}
DROP = frozenset({"head", "script", "style", "template", "epub:case", "title", "meta", "link", "rp"})
BLOCKS = frozenset("p div section article aside header footer main figure figcaption body html dl dt dd form "
                   "fieldset address details summary hgroup center nav h1 h2 h3 h4 h5 h6 ul ol li table "
                   "blockquote pre hr img image svg epub:switch caption thead tbody tfoot tr td th".split())
DROPPED_NAV = frozenset({"toc", "landmarks", "page-list"})
NOTE_TYPES = frozenset({"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"})
NOTEREF_TYPES = frozenset({"noteref", "doc-noteref"})
EXTERNAL = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)
    deep: int = 0  # root only: tags dropped for nesting past MAX_DEPTH


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


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]
        self.over = 0  # open elements past MAX_DEPTH: not materialized, only counted
        self.buf = []  # text pieces since the last tag

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


_LONELY = re.compile(r"<(?=[^<>]*(?:<|$))")  # a "<" whose tag never closes before the next "<"


def tree(data: bytes) -> Node:
    """Node tree of a document. A "<" that opens no complete tag becomes text: the stdlib parser rescans
    such runs and goes quadratic (or worse) on "<a <a <a ...". Nesting past MAX_DEPTH is flattened."""
    p = _Builder()
    try:
        p.feed(_LONELY.sub("&lt;", decode(data[:MAX_DOC])))
        p.close()
    except Exception:  # a parser fault keeps what was built so far
        pass
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


# ---- CSS
_SEL = re.compile(r"^([A-Za-z][A-Za-z0-9:\-]*)?(?:\.([\w\-]+))?$")


def _strip_comments(s: str) -> str:
    out, i = [], 0
    while True:
        j = s.find("/*", i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:j])
        k = s.find("*/", j + 2)
        if k < 0:
            return "".join(out)
        i = k + 2


def _rules(s: str):
    """(prelude, body) of each top-level style rule; at-rules and anything nested in them are skipped."""
    i, n = 0, len(s)
    while i < n:
        j = s.find("{", i)
        if j < 0:
            return
        prelude = s[i:j].rsplit(";", 1)[-1].strip()
        if prelude.startswith("@"):
            depth, k = 1, j + 1
            while depth and k < n:
                a, b = s.find("{", k), s.find("}", k)
                if b < 0:
                    k = n
                elif 0 <= a < b:
                    depth, k = depth + 1, a + 1
                else:
                    depth, k = depth - 1, b + 1
            i = k
            continue
        k = s.find("}", j + 1)
        if k < 0:
            k = n
        yield prelude, s[j + 1:k]
        i = k + 1


def _decls(body: str) -> dict:
    """{display|font-weight: (value, important)} of one declaration block; the last one wins unless !important."""
    out = {}
    for d in body[:10_000].split(";"):
        name, sep, val = d.partition(":")
        name = name.strip().lower()
        if not sep or name not in ("display", "font-weight"):
            continue
        val, imp = val.strip().lower(), False
        if "!important" in val:
            val, imp = val.replace("!important", "").strip(), True
        if name not in out or imp or not out[name][1]:
            out[name] = (val, imp)
    return out


class Css:
    """Selectors tag, .class, tag.class and comma lists for display and font-weight; the rest is ignored."""

    def __init__(self, sheets: list[str]):
        self._idx: dict = {}  # (tag, class) -> {prop: (important, order, value)}
        n = order = 0
        for sheet in sheets:
            for prelude, body in _rules(_strip_comments(sheet[:MAX_SHEET])):
                props = _decls(body)
                if not props:
                    continue
                order += 1
                for sel in prelude.split(","):
                    m = _SEL.match(sel.strip())
                    if not m or not (m.group(1) or m.group(2)):
                        continue
                    n += 1
                    if n > MAX_SELECTORS:
                        return
                    slot = self._idx.setdefault(((m.group(1) or "").lower(), m.group(2) or ""), {})
                    for name, (val, imp) in props.items():
                        if name not in slot or (imp, order) >= slot[name][:2]:
                            slot[name] = (imp, order, val)

    def _props(self, node) -> dict:
        best = {}  # prop -> (rank, value)

        def put(slot, spec):
            for name, (imp, order, val) in slot.items():
                rank = (imp, 0, spec, order)
                if name not in best or rank > best[name][0]:
                    best[name] = (rank, val)
        if self._idx:
            tag = node.tag
            put(self._idx.get((tag, ""), {}), (0, 1))
            for c in node.attrs.get("class", "").split()[:32]:
                put(self._idx.get(("", c), {}), (1, 0))
                put(self._idx.get((tag, c), {}), (1, 1))
        for name, (val, imp) in _decls(node.attrs.get("style", "")).items():
            rank = (imp, 1, (2, 2), 0)
            if name not in best or rank > best[name][0]:
                best[name] = (rank, val)
        return {k: v[1] for k, v in best.items()}

    def display_none(self, node, ancestors) -> bool:
        """True when node, or any of `ancestors`, has display:none."""
        return any(self._props(n).get("display") == "none" for n in (node, *ancestors))

    def bold(self, node) -> bool:
        v = self._props(node).get("font-weight", "")
        return v in ("bold", "bolder") or (v.isdigit() and int(v) >= 600)


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


class _Walk:
    def __init__(self, css, load, notes, ctx, part):
        self.css, self.load, self.notes, self.ctx, self.part = css, load, notes, ctx, part
        self.data_bytes = 0

    # ---- notes and hidden
    def note_key(self, href: str):
        file, _, frag = href.partition("#")
        if not frag:
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
        return "hidden" in k.attrs or self.css.display_none(k, ())

    # ---- inlines
    def inl(self, k, cur, imgs):
        """Append the inline form of node k (or string) to cur; img elements go to imgs, or become alt text."""
        if isinstance(k, str):
            cur.append(Text(_ws(k)))
            return
        t = k.tag
        if t in DROP or self.take_note(k):
            return
        if "hidden" in k.attrs or self.css.display_none(k, ()):
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
        elif t == "math":
            alt = k.attrs.get("alttext", "").strip()[:2000]
            cur.append(Math(alt) if alt else Text(_ws(text_of(k))))
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
        if _types(k) & NOTEREF_TYPES:
            key = self.note_key(href)
            if key:
                cur.append(NoteRef(key))
                return
        m = _SCHEME.match(href)
        if m and m.group(1).lower() in EXTERNAL:
            url = re.sub(r"[\s()<>]", lambda x: "%%%02X" % ord(x.group()), href[:MAX_URL])
            sub = []
            self.inl_children(k, sub, imgs)
            cur.append(Link(url, sub))
        else:
            self.inl_children(k, cur, imgs)

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
            items = _tidy(cur)
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
            if isinstance(c, str) or self.take_note(c) or c.tag in DROP:
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
        cap = max(max((len(r) for r in cells), default=0), 16)  # a span never needs to reach past the real grid
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
                line.append(Cell(blocks, max(1, rs), min(max(1, _int(c.attrs.get("colspan"), 1)), cap), c.tag == "th"))
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
    if root.deep:
        ctx.skipped["depth"] += root.deep
        if ctx.skipped["depth"] == root.deep:
            ctx.warnings.append("depth: skipped content nested too deeply")
    return out
