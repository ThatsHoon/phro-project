"""DjVu to the document model: the text layer of single-page and bundled documents, one page section per page.

Format knowledge, no code ported: the DjVu 3 specification (LizardTech, 2005) for the IFF container, the DIRM
directory, INCL and the TXTa/TXTz layout. BZZ chunks (TXTz and the DIRM's packed part) are decoded by
`containers/bzz.py`; nothing in this module is ported from another project.

Container. `AT&T`, then one FORM: `DJVU` is one page, `DJVM` a bundled document whose first chunk is DIRM. A chunk
must fit inside its parent; one that does not is dropped and counted `record`, and the walk of that parent stops
there. Only one level is followed: a DJVM's components are reached through the DIRM offsets, never by nesting.

DIRM. Byte 0 bit 7 clear is an indirect document (its pages are other files): `unsupported`, as DjVu 2's NDIR is. The
packed part gives each component's size, flags (type 1 a page, 2 thumbnails, 0 and 3 a shared DJVI) and its id, name
and title. A component whose offset does not hold a FORM of its type that fits the DJVM, or whose FORM overlaps or
repeats an earlier one, is dropped and counted `record`; so every byte is walked at most once. Pages are numbered
by their place among DIRM's page components, so a dropped page does not renumber the pages after it.

Page text. TXTa, or TXTz through BZZ: a BE24 length, then UTF-8 (decoded with errors="replace"); the zone tree after
it is never read. GS, VT and US end a paragraph, LF a line (`_text`). INCL brings in a shared DJVI's chunks, read once
per document and one level deep (an INCL inside it is not followed). A page with no TXTa or TXTz is counted
`textless` and has no section unless it has bookmark headings or annotation lines (see Bookmarks); a text layer that
is empty gives an empty section. A page cut short (a chunk that
runs past its FORM, or a one-page file that ends before its FORM) is counted `record` once and never `textless`: it
keeps the text read before the cut and has no section without any. When every page read is textless, the file is
`unsupported` ("no text layer: ..."), so a scan is not called damaged; a file whose pages are all cut is `damaged`.

BZZ budget. Each BZZ chunk decodes with `min(BZZ_CHUNK, left)`, `left` being what remains of the document's
`BZZ_TOTAL`. A Limit at BZZ_CHUNK, a Truncated stream or a damaged one counts the chunk `record`. A Limit at `left`
spends the budget: that page and every later one count `textless` (but never make the file "no text layer"). Shared
text repeated on a later page charges its size to `left` again, as if decoded again, so output stays bounded when one
DJVI is included by every page. A shared DJVI's annotations are parsed once, and each repeat charges the size of
the lines it gives to `left` the same way; a repeat that does not fit drops that page's shared annotation lines only,
counted `link` once for the page (the budget is not spent, so the page's text is kept).

Annotations. ANTa, or ANTz through BZZ. A page's own annotation chunks are read as one S-expression text, and so
are a shared DJVI's, once per document (`_sexp`, iterative: at most DJVU_ANT characters, ANT_DEPTH open lists and
ANT_STRING bytes per string; past a bound, or at an expression that never closes, the rest is ignored and counted
`link` once). Up to ANT_AREAS mapareas per text (the rest counted `link` once): `(maparea URL COMMENT AREA ...)`, URL
a string or `(url "href" "target")`. An http, https, mailto or ftp URL gives `Link: ` and a Link labelled with the
comment (or the URL); any other URL, an internal one (`#12`, `#+1`) or none gives `Comment: <comment>` when there is a
comment, else nothing. A URL holding a space or a control character is no link (it could end the markdown line): its
comment is kept and it counts `link`. Comments and titles are put on one line. The lines are the page section's
comments, rendered after its text: the included DJVIs' first (INCL chunks come before a page's own annotations), then
the page's own. The first `(metadata (title "..."))` of the pages read is the document title.

Bookmarks. A DJVM's NAVM (the chunk after DIRM), through BZZ: a BE16 count, then each bookmark in pre-order (a
child-count byte, a BE24 length and the description, a BE24 length and the URL). Up to DJVU_BOOKMARKS are read and
those deeper than BOOKMARK_DEPTH levels are walked past; the rest are unused. A bookmark whose URL names a page
(`#<component id>` or `#<number>`) becomes `Heading(min(depth + 2, 6), text[:120])` at the start of that page. A
NAVM that does not fit, cannot be decoded or ends early counts `record` once (the bookmarks read before are kept). A
page counted textless keeps a section when it has a heading or annotation lines (it is still counted textless).

Deadline. Before each page, `clock() - started` past DJVU_DEADLINE seconds: that page and the rest count `page_cut`,
and the output so far is kept, as DJVU_PAGES does.
"""
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..containers import bzz
from ..limits import ReadError
from ..model import Doc, Heading, Link, Para, Section, Text

DJVU_PAGES = 5_000       # pages read per conversion; the rest count page_cut
DJVU_DEADLINE = 36.0     # seconds for the page loop from `started`, as PDF_DEADLINE; the rest count page_cut
DJVU_ANT = 2**20         # characters of annotation S-expression read per page; the rest ignored, counted link
ANT_DEPTH = 64           # open lists in it
ANT_STRING = 64 * 2**10  # bytes per string in it
ANT_AREAS = 256          # mapareas per page; the rest counted link
DJVU_BOOKMARKS = 1_000   # NAVM bookmarks read; the rest unused
BOOKMARK_DEPTH = 32      # bookmark levels used; deeper ones are walked past

IMAGE_CHUNKS = frozenset((b"BG44", b"FG44", b"Sjbz", b"BGjp", b"FGjp", b"Smmr"))
TEXT_CHUNKS = frozenset((b"TXTa", b"TXTz"))
_KINDS = {1: "page", 2: "thum"}  # DIRM type bits; 0 (include) and 3 (shared annotations) are DJVI components
_FORM_OF = {"page": b"DJVU", "djvi": b"DJVI", "thum": b"THUM"}
_PARAGRAPH = re.compile("[\x1d\x0b\x1f]")  # GS (region), VT (column), US (paragraph)
ANT_CHUNKS = frozenset((b"ANTa", b"ANTz"))
LINK_SCHEMES = frozenset(("http", "https", "mailto", "ftp"))
HEADING_CHARS = 120
# one token after optional space: "(", ")", the opening quote of a string, or a bare word; each branch starts with its
# own characters, so a match never backtracks
_TOKEN = re.compile(r'\s*(?:(\()|(\))|(")|([^\s()"]+))')
_PLAIN = re.compile(r'[^"\\]*')  # a string's run up to its closing quote or a backslash
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "a": "\a"}


@dataclass(frozen=True)
class Component:
    offset: int
    size: int
    kind: str  # page | djvi | thum
    id: str
    title: str


@dataclass
class _Part:
    """What a page's chunks (or a shared component's) gave."""
    paras: list = field(default_factory=list)
    text: bool = False    # a TXTa or TXTz chunk was there
    images: bool = False  # an image chunk was there
    size: int = 0         # bytes of text layer read, charged again when shared text is repeated
    cut: bool = False     # the walk stopped at a chunk that ran past its FORM (or the file ended): counted `record`
    ant: list = field(default_factory=list)    # annotation texts, in chunk order (shared ones where INCL stood)
    lines: list = field(default_factory=list)  # Para per link or comment line, from the annotations
    title: str = ""       # the first (metadata (title ...)) of the annotations
    weight: int = 0       # characters of `lines`, charged to `left` when shared lines are repeated
    over: bool = False    # a repeat of shared lines did not fit `left`: counted `link` once for the page


def _chunks(data: bytes, start: int, end: int, ctx=None, dropped=None):
    """(id, body start, body end) of each chunk in data[start:end]. A chunk longer than what is left of its parent is
    dropped, counted `record` (when ctx is given), handed to `dropped(id)` (when given), and ends the walk. An odd
    length is followed by one pad byte, as in IFF85. Checked on the djvu-rs corpus sample (public-djvu-427847fcd2,
    2026-11-16, ids and offsets only): at the top level its DIRM (143 bytes) and four of its thirteen component FORMs
    have odd lengths, and each is followed by one 00 byte and then the next `FORM` id (read without the pad, the id
    would be `\\x00FOR`); inside the page FORMs, eight odd `Sjbz` chunks are each followed by one 00 byte and then
    `TXTz`. A FORM's length counts the pads of its inner chunks but not the pad after its own last odd chunk (that
    byte follows the FORM). The last FORM is odd and ends the file with no pad byte, which this walk accepts, since
    fewer than 8 bytes are left."""
    pos = start
    while end - pos >= 8:
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        body = pos + 8
        if size > end - body:
            if ctx is not None:
                ctx.skipped["record"] += 1
            if dropped is not None:
                dropped(cid)
            return
        yield cid, body, body + size
        pos = body + size + (size & 1)


def _zstr(data: bytes, pos: int) -> tuple[str, int]:
    """The NUL-terminated UTF-8 string at pos (to the end when there is no NUL), and the position after it."""
    stop = data.find(b"\x00", pos)
    if stop < 0:
        stop = len(data)
    return data[pos:stop].decode("utf-8", "replace"), stop + 1


def _dirm(body: bytes, decode=None) -> list[Component]:
    """The components a bundled DIRM lists, in its order. `decode(packed) -> bytes` unpacks the BZZ part (by default
    bzz.decode at BZZ_CHUNK); it raises ReadError for a part it cannot read. Indirect: `unsupported`."""
    if not body:
        raise ReadError("damaged", "empty DIRM")
    if not body[0] & 0x80:
        raise ReadError("unsupported", "indirect DjVu (its pages are separate files; bundle it first)")
    if len(body) < 3:
        raise ReadError("damaged", "DIRM too short for its component count")
    count = struct.unpack_from(">H", body, 1)[0]
    head = 3 + 4 * count
    if len(body) < head:
        raise ReadError("damaged", f"DIRM too short for {count} offsets")
    offsets = struct.unpack_from(f">{count}I", body, 3)
    meta = (decode or _decode_dirm)(body[head:])
    if len(meta) < 4 * count:
        raise ReadError("damaged", f"DIRM directory too short for {count} components")
    comps = []
    pos = 4 * count
    for i in range(count):
        flags = meta[3 * count + i]
        cid, pos = _zstr(meta, pos)
        if flags & 0x80:  # a name, not used
            _, pos = _zstr(meta, pos)
        title = ""
        if flags & 0x40:
            title, pos = _zstr(meta, pos)
        size = int.from_bytes(meta[3 * i:3 * i + 3], "big")
        comps.append(Component(offsets[i], size, _KINDS.get(flags & 0x3F, "djvi"), cid, title))
    return comps


def _decode_dirm(packed: bytes) -> bytes:
    try:
        return bzz.decode(packed, min(bzz.BZZ_CHUNK, bzz.BZZ_TOTAL))
    except (bzz.Limit, bzz.Truncated) as e:
        raise ReadError("damaged", f"DIRM: {e}") from None


def _han_kana(ch: str) -> bool:
    """Han or Kana, whose lines join with no space (the ranges of pdf_layout._is_han_kana)."""
    o = ord(ch)
    return (0x3040 <= o <= 0x30ff or 0x3400 <= o <= 0x4dbf or 0x4e00 <= o <= 0x9fff
            or 0xf900 <= o <= 0xfaff or 0x20000 <= o <= 0x2fa1f)


def _text(raw: str) -> list[Para]:
    """Paragraphs of a text layer. GS, VT and US end a paragraph, LF a line; lines are trimmed and blank ones left
    out. A line break is a space, except between Han or Kana (dropped) and after a hyphen that follows a letter when
    the next line starts with a lowercase letter (the hyphen is dropped and the word joined)."""
    out = []
    for block in _PARAGRAPH.split(raw):
        parts = []
        prev = ""
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if parts:
                a, b = prev[-1], line[0]
                if a == "-" and len(prev) > 1 and prev[-2].isalpha() and b.islower():
                    parts[-1] = parts[-1][:-1]
                elif not (_han_kana(a) and _han_kana(b)):
                    parts.append(" ")
            parts.append(line)
            prev = line
        if parts:
            out.append(Para([Text("".join(parts))]))
    return out


def _one_line(s: str) -> str:
    """A comment or title on one line, its line breaks as " / " (as pdf's comments): a line of it must not start a
    markdown line."""
    return " / ".join(part for part in (p.strip() for p in s.splitlines()) if part)


class _Sym(str):
    """A bare word of an annotation (a keyword, a number, a colour), told from a quoted string."""


def _string(text: str, pos: int):
    """The string whose body starts at text[pos] (after its quote), and the position after its closing quote; (None,
    pos) when it passes ANT_STRING bytes or never closes. Escapes: backslash n t r b f v a, up to three octal digits
    (one byte each: several make UTF-8), and a backslash before any other character keeps that character."""
    cap = ANT_STRING
    buf = bytearray()
    n = len(text)
    while True:
        m = _PLAIN.match(text, pos)
        buf += m.group().encode("utf-8")
        pos = m.end()
        if len(buf) > cap or pos >= n:
            return None, pos
        if text[pos] == '"':
            return buf.decode("utf-8", "replace"), pos + 1
        pos += 1  # a backslash
        if pos >= n:
            return None, pos
        stop = pos
        while stop < n and stop - pos < 3 and text[stop] in "01234567":
            stop += 1
        if stop > pos:
            buf.append(int(text[pos:stop], 8) & 0xFF)
            pos = stop
        else:
            buf += _ESCAPES.get(text[pos], text[pos]).encode("utf-8")
            pos += 1


def _sexp(text: str, ctx=None) -> list:
    """The complete top-level lists of an annotation text, as nested lists of strings and `_Sym` words (bare words
    outside a list and stray `)` are ignored). Bounded and iterative: at most DJVU_ANT characters, ANT_DEPTH open
    lists and ANT_STRING bytes per string. Past a bound, or at a list or string that never closes, the rest
    (including the unclosed top-level list) is ignored and counted `link` once (when ctx is given)."""
    cut = len(text) > DJVU_ANT
    text = text[:DJVU_ANT]
    depth = ANT_DEPTH
    out, stack = [], []
    pos, n = 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if m is None:  # only space is left
            break
        pos = m.end()
        if m.group(1):
            if len(stack) >= depth:
                cut = True
                break
            stack.append([])
        elif m.group(2):
            if stack:
                done = stack.pop()
                (stack[-1] if stack else out).append(done)
        elif m.group(3):
            value, pos = _string(text, pos)
            if value is None:
                cut = True
                break
            if stack:
                stack[-1].append(value)
        elif stack:
            stack[-1].append(_Sym(m.group(4)))
    if stack:
        cut = True
    if cut and ctx is not None:
        ctx.skipped["link"] += 1
    return out


def _maparea(item: list, bad: list):
    """The comment line of one `(maparea URL COMMENT ...)`, or None. A link URL holding a space or a control character
    is appended to `bad` and gives no Link."""
    url = item[1] if len(item) > 1 else ""
    if isinstance(url, list):  # (url "href" "target")
        url = url[1] if len(url) > 1 and url[0] == "url" and isinstance(url[1], str) else ""
    url = url.strip() if isinstance(url, str) else ""
    comment = item[2] if len(item) > 2 and isinstance(item[2], str) else ""
    comment = _one_line(comment)
    scheme, colon, rest = url.partition(":")
    if colon and rest and scheme.lower() in LINK_SCHEMES:
        if not any(ch.isspace() or not ch.isprintable() for ch in url):
            return Para([Text("Link: "), Link(url, [Text(comment or url)])])
        bad.append(url)  # a line break in it would end the "> " line and start a markdown one
    if comment:
        return Para([Text("Comment: " + comment)])
    return None


def _annotations(text: str, ctx) -> tuple[list, str]:
    """(comment lines, metadata title) of one page's annotation text. Up to ANT_AREAS mapareas are used; the rest
    count `link` once."""
    lines, title = [], ""
    areas = 0
    bad = []
    for item in _sexp(text, ctx):
        if not item or not isinstance(item[0], _Sym):
            continue
        if item[0] == "metadata" and not title:
            for entry in item[1:]:
                if (isinstance(entry, list) and len(entry) > 1 and entry[0] == "title"
                        and isinstance(entry[1], str) and _one_line(entry[1])):
                    title = _one_line(entry[1])
                    break
        elif item[0] == "maparea":
            areas += 1
            if areas > ANT_AREAS:
                continue
            line = _maparea(item, bad)
            if line is not None:
                lines.append(line)
    if areas > ANT_AREAS:
        ctx.skipped["link"] += 1
    ctx.skipped["link"] += len(bad)
    return lines, title


def _weight(lines: list) -> int:
    """Characters a list of comment lines holds: their texts, link URLs and labels."""
    total = 0
    for para in lines:
        for it in para.inlines:
            if isinstance(it, Link):
                total += len(it.url) + sum(len(t.text) for t in it.inlines)
            else:
                total += len(it.text)
    return total


def _target(url: str, ids: dict, count: int):
    """The page number a bookmark URL names (`#<component id>` or `#<number>`), or None."""
    if not url.startswith("#"):
        return None
    name = url[1:]
    if name in ids:
        return ids[name]
    if name.isascii() and name.isdigit() and len(name) <= 9 and 1 <= int(name) <= count:
        return int(name)
    return None


class _Reader:
    def __init__(self, data: bytes, ctx, started, clock):
        self.data, self.ctx, self.started, self.clock = data, ctx, started, clock
        self.left = bzz.BZZ_TOTAL  # BZZ output the document may still decode
        self.spent = False         # a chunk hit `left`: the remaining pages are textless
        self.djvi = {}             # component id -> (body start, end) of its FORM:DJVI
        self.shared = {}           # component id -> _Part, or None when the id names no DJVI
        self.used = set()          # shared ids already given to a page
        self.marks = {}            # page number -> its bookmark Headings
        self.title = ""            # the first metadata title of the pages read

    # -- BZZ under the document's budget

    def _record(self):
        self.ctx.skipped["record"] += 1

    def _decode(self, packed: bytes) -> bytes:
        """packed decoded under the budget, for the DIRM: anything wrong is `damaged`."""
        try:
            out = bzz.decode(packed, min(bzz.BZZ_CHUNK, self.left))
        except (bzz.Limit, bzz.Truncated) as e:
            raise ReadError("damaged", f"DIRM: {e}") from None
        self.left -= len(out)
        return out

    def _unpack(self, packed: bytes):
        """packed decoded under the budget, for a page's chunk; None when the chunk is counted `record` or the
        budget is spent (self.spent)."""
        if self.spent or self.left <= 0:
            self.spent = True
            return None
        limit = min(bzz.BZZ_CHUNK, self.left)
        try:
            out = bzz.decode(packed, limit)
        except bzz.Limit:
            if self.left < bzz.BZZ_CHUNK:
                self.spent, self.left = True, 0
            else:
                self._record()
            return None
        except bzz.Truncated:
            self._record()
            return None
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self._record()
            return None
        self.left -= len(out)
        return out

    def _unpack_navm(self, packed: bytes):
        """The NAVM decoded under the budget; None, counted `record`, when it cannot be."""
        try:
            out = bzz.decode(packed, min(bzz.BZZ_CHUNK, max(self.left, 0)))
        except (bzz.Limit, bzz.Truncated):
            self._record()
            return None
        except ReadError as e:
            if e.kind != "damaged":
                raise
            self._record()
            return None
        self.left -= len(out)
        return out

    # -- the container

    def bundle(self, start: int, end: int) -> list[tuple[int, str, int, int]]:
        """The pages of a DJVM whose chunks are data[start:end]: (number, title, FORM body start, FORM end)."""
        first = next(_chunks(self.data, start, end, self.ctx), None)
        if first is None:
            raise ReadError("damaged", "DJVM without a DIRM")
        cid, body, stop = first
        if cid == b"NDIR":
            raise ReadError("unsupported", "DjVu 2 directory (NDIR)")
        if cid != b"DIRM":
            raise ReadError("damaged", f"DJVM starts with {cid!r}, not DIRM")
        comps = _dirm(self.data[body:stop], self._decode)
        navm = self._navm_chunk(stop + ((stop - body) & 1), end)
        placed = self._place(comps, stop, end)
        pages = []
        ids = {}
        number = 0  # page components in DIRM order, dropped ones included, so later pages keep their numbers
        for i, comp in enumerate(comps):
            number += comp.kind == "page"
            if comp.kind == "page":
                ids.setdefault(comp.id, number)
            if i not in placed:
                continue
            body, form_end = placed[i]
            if comp.kind == "page":
                title = comp.title if comp.title and comp.title != str(number) else ""
                pages.append((number, title, body, form_end))
            elif comp.kind == "djvi":
                self.djvi.setdefault(comp.id, (body, form_end))
        if navm is not None:
            with self.ctx.guard("record"):
                self._bookmarks(navm, ids, number)
        return pages

    def _navm_chunk(self, pos: int, end: int):
        """The body of the NAVM chunk at pos (the one after DIRM), or None; one that does not fit counts `record`."""
        data = self.data
        if end - pos < 8 or data[pos:pos + 4] != b"NAVM":
            return None
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        if size > end - pos - 8:
            self._record()
            return None
        return data[pos + 8:pos + 8 + size]

    def _bookmarks(self, packed: bytes, ids: dict, count: int):
        """self.marks from a NAVM body. The tree is walked in pre-order, its levels kept as a stack of children still
        to come; at most DJVU_BOOKMARKS entries are read (each takes at least 7 bytes), so the walk is bounded by the
        cap and by the bytes. A bookmark that runs past the data ends the walk, counted `record` once."""
        raw = self._unpack_navm(packed)
        if raw is None:
            return
        if len(raw) < 2:
            self._record()
            return
        total = struct.unpack_from(">H", raw, 0)[0]
        cap, deep = DJVU_BOOKMARKS, BOOKMARK_DEPTH
        pos, n = 2, len(raw)
        stack = []
        read = 0
        while read < total and read < cap:
            if n - pos < 7:
                self._record()
                return
            kids = raw[pos]
            size = int.from_bytes(raw[pos + 1:pos + 4], "big")
            pos += 4
            if size > n - pos - 3:
                self._record()
                return
            desc = raw[pos:pos + size]
            pos += size
            size = int.from_bytes(raw[pos:pos + 3], "big")
            pos += 3
            if size > n - pos:
                self._record()
                return
            url = raw[pos:pos + size]
            pos += size
            read += 1
            depth = len(stack)
            if stack:
                stack[-1] -= 1
            if kids:
                stack.append(kids)
            while stack and stack[-1] <= 0:
                stack.pop()
            if depth >= deep:
                continue
            number = _target(url.decode("utf-8", "replace").strip(), ids, count)
            text = " ".join(desc.decode("utf-8", "replace").split())[:HEADING_CHARS]
            if number is not None and text:
                self.marks.setdefault(number, []).append(Heading(min(depth + 2, 6), [Text(text)]))

    def _place(self, comps: list[Component], start: int, end: int) -> dict[int, tuple[int, int]]:
        """DIRM index -> (FORM body start, FORM end) of each component whose offset holds a FORM of its type within
        data[start:end] and does not overlap or repeat an earlier one (by offset; the first in DIRM order wins).
        Each one dropped counts `record`."""
        data = self.data
        found = []
        for i, comp in enumerate(comps):
            o = comp.offset
            if o < start or end - o < 12 or data[o:o + 4] != b"FORM":
                self._record()
                continue
            size = int.from_bytes(data[o + 4:o + 8], "big")
            if size < 4 or size > end - o - 8 or data[o + 8:o + 12] != _FORM_OF[comp.kind]:
                self._record()
                continue
            found.append((o, i, o + 8 + size))
        found.sort()
        placed = {}
        reach = start
        for o, i, form_end in found:
            if o < reach:
                self._record()
                continue
            placed[i] = (o + 12, form_end)
            reach = form_end
        return placed

    # -- pages

    def _read(self, start: int, end: int, part: _Part, nested: bool):
        """Read the chunks of data[start:end] into part; INCL is followed unless nested."""
        data = self.data
        seen = set()
        for cid, body, stop in _chunks(data, start, end, self.ctx, lambda cid: self._dropped(cid, part)):
            if cid in TEXT_CHUNKS:
                self._text_chunk(cid, data[body:stop], part)
            elif cid in IMAGE_CHUNKS:
                part.images = True
            elif cid in ANT_CHUNKS:
                self._ant_chunk(cid, data[body:stop], part)
            elif cid == b"INCL" and not nested:
                name = data[body:stop].decode("utf-8", "replace").strip("\x00 \t\r\n")
                if name not in seen:
                    seen.add(name)
                    self._include(name, part)
            if self.spent:
                return

    @staticmethod
    def _dropped(cid: bytes, part: _Part):
        """A chunk that did not fit (already counted `record`) cuts the page short: whatever followed it is unknown,
        so the page is never also counted `textless`. A dropped image chunk still names the page images."""
        part.cut = True
        if cid in IMAGE_CHUNKS:
            part.images = True

    def _text_chunk(self, cid: bytes, raw: bytes, part: _Part):
        part.text = True
        if cid == b"TXTz":
            raw = self._unpack(raw)
            if raw is None:
                return
        if not raw:
            return
        if len(raw) < 3:
            self._record()
            return
        size = int.from_bytes(raw[:3], "big")
        if size > len(raw) - 3:
            self._record()
        text = raw[3:3 + size]
        part.size += len(text)
        part.paras.extend(_text(text.decode("utf-8", "replace")))

    def _ant_chunk(self, cid: bytes, raw: bytes, part: _Part):
        if cid == b"ANTz":
            raw = self._unpack(raw)
            if raw is None:
                return
        part.ant.append(raw.decode("utf-8", "replace"))

    def _include(self, name: str, part: _Part):
        if name not in self.shared:
            where = self.djvi.get(name)
            if where is None:
                self._record()
                self.shared[name] = None
            else:
                got = _Part()
                self._read(where[0], where[1], got, nested=True)
                if got.ant and not self.spent:  # parsed once per document, whatever includes it
                    got.lines, got.title = _annotations("\n".join(got.ant), self.ctx)
                    got.weight = _weight(got.lines)
                got.ant = []
                self.shared[name] = got
        got = self.shared[name]
        if got is None:
            return
        part.text = part.text or got.text
        part.images = part.images or got.images
        repeat = name in self.used
        if repeat and got.size:  # a repeat: charged as if decoded again
            if got.size > self.left:
                self.spent, self.left = True, 0
                return
            self.left -= got.size
        self.used.add(name)
        part.paras.extend(got.paras)
        part.title = part.title or got.title
        if repeat and got.weight > self.left:  # these lines only are dropped; the budget is not spent
            part.over = True
            return
        if repeat:
            self.left -= got.weight
        part.lines.extend(got.lines)

    def _page(self, start: int, end: int) -> _Part:
        part = _Part()
        self._read(start, end, part, nested=False)
        if self.spent:
            return part
        if part.ant:
            lines, title = _annotations("\n".join(part.ant), self.ctx)
            part.lines.extend(lines)
            part.title = part.title or title
        if part.over:
            self.ctx.skipped["link"] += 1
        return part

    def _late(self) -> bool:
        """The page loop is past DJVU_DEADLINE seconds from `started`."""
        return self.clock() - (self.started or self.clock()) > DJVU_DEADLINE

    def _fail(self):
        self.ctx.skipped["page"] += 1
        if self.ctx.skipped["page"] == 1:
            self.ctx.warnings.append("page: skipped damaged part")

    def run(self, pages: list[tuple[int, str, int, int]], short: bool = False) -> Doc:
        """The page sections. `short`: the file ends before its FORM does, so the last page is cut short even when
        the walk ended on a chunk boundary. A cut page is counted `record` once; it keeps the text read before the
        cut, has no section when there was none, and is never `textless`. Pages counted `textless` because the BZZ
        budget ran out (`starved`) never make the file "no text layer"; a page whose budget ran out is counted only
        that way, even when it is also the cut last page. A page with no text keeps a section for its bookmark
        headings and annotation lines. DJVU_PAGES and DJVU_DEADLINE count the pages left `page_cut`."""
        ctx = self.ctx
        if not pages:
            raise ReadError("damaged", "no pages")
        sections = []
        textless = starved = images = read = 0
        capped = False
        for n, (number, title, start, end) in enumerate(pages):
            if n >= DJVU_PAGES or self._late():
                ctx.skipped["page_cut"] += len(pages) - n
                capped = True
                break
            read += 1
            heads = list(self.marks.get(number, ()))

            def keep(blocks, lines, heads=heads, number=number, title=title):
                if heads or blocks or lines:
                    sections.append(Section("page", title=title, number=number, blocks=heads + blocks,
                                            comments=lines))

            if self.spent:
                textless += 1
                starved += 1
                keep([], [])
                continue
            got = []
            try:
                with ctx.guard("page"):
                    got.append(self._page(start, end))
            except ReadError as e:
                if e.kind == "timeout":
                    raise
                self._fail()
            if not got:
                continue
            part = got[0]
            images += part.images
            if self.spent:  # this page's text passed the budget: it and the rest count textless, and only that
                textless += 1
                starved += 1
                keep([], [])
                continue
            if short and n == len(pages) - 1 and not part.cut:
                part.cut = True
                self._record()
            if part.title and not self.title:
                self.title = part.title
            if part.cut and not part.paras:  # counted record by the walk, never also textless
                keep([], part.lines)
                continue
            if not part.text:
                textless += 1
                keep([], part.lines)
                continue
            sections.append(Section("page", title=title, number=number, blocks=heads + part.paras,
                                    comments=part.lines))
        if images:
            ctx.skipped["page_images"] = 1
        if textless and textless == read and not starved:
            noun = "page" if textless == 1 else "pages"
            raise ReadError("unsupported", f"no text layer: {textless} image-only {noun} (OCR is out of scope)")
        if textless:
            ctx.skipped["textless"] += textless
        if not sections and not capped and not textless:
            raise ReadError("damaged", "no readable pages")
        return Doc(title=self.title, blocks=sections)


def parse_bytes(data, ctx, *, started=None, clock=time.monotonic) -> Doc:
    """A DjVu file's text layer as one page section per page, with its annotations and bookmarks. `started` and
    `clock` are for the page loop's deadline, DJVU_DEADLINE (`started` is clock() at entry unless given)."""
    data = bytes(data)
    if started is None:
        started = clock()
    if len(data) < 16 or data[:4] != b"AT&T" or data[4:8] != b"FORM":
        raise ReadError("damaged", "not a DjVu file (no AT&T FORM)")
    size = int.from_bytes(data[8:12], "big")
    if size < 4:
        raise ReadError("damaged", "DjVu FORM too short")
    end = min(12 + size, len(data))  # a cut file is read as far as it goes; what does not fit counts record
    kind = data[12:16]
    reader = _Reader(data, ctx, started, clock)
    if kind == b"DJVU":
        pages = [(1, "", 16, end)]
    elif kind == b"DJVM":
        pages = reader.bundle(16, end)
    else:
        raise ReadError("damaged", f"FORM:{kind.decode('latin-1')} is not a DjVu page or document")
    # a bundle's components that do not fit the cut file are dropped by _place; a single page is read as far as it goes
    return reader.run(pages, short=kind == b"DJVU" and 12 + size > len(data))


def parse(path, ctx, *, started=None) -> Doc:
    return parse_bytes(Path(path).read_bytes(), ctx, started=started)
