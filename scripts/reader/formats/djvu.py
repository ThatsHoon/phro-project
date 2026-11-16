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
`textless` and gets no section; a text layer that is empty gives an empty section. When every page read is textless,
the file is `unsupported` ("no text layer: ..."), so a scan is not called damaged.

BZZ budget. Each BZZ chunk decodes with `min(BZZ_CHUNK, left)`, `left` being what remains of the document's
`BZZ_TOTAL`. A Limit at BZZ_CHUNK, a Truncated stream or a damaged one counts the chunk `record`. A Limit at `left`
spends the budget: that page and every later one count `textless`. Shared text repeated on a later page charges its
size to `left` again, as if it were decoded again, so output stays bounded when one DJVI is included by every page.
"""
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..containers import bzz
from ..limits import ReadError
from ..model import Doc, Para, Section, Text

DJVU_PAGES = 5_000  # pages read per conversion; the rest count page_cut

IMAGE_CHUNKS = frozenset((b"BG44", b"FG44", b"Sjbz", b"BGjp", b"FGjp", b"Smmr"))
TEXT_CHUNKS = frozenset((b"TXTa", b"TXTz"))
_KINDS = {1: "page", 2: "thum"}  # DIRM type bits; 0 (include) and 3 (shared annotations) are DJVI components
_FORM_OF = {"page": b"DJVU", "djvi": b"DJVI", "thum": b"THUM"}
_PARAGRAPH = re.compile("[\x1d\x0b\x1f]")  # GS (region), VT (column), US (paragraph)


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


class _Reader:
    def __init__(self, data: bytes, ctx, started, clock):
        self.data, self.ctx, self.started, self.clock = data, ctx, started, clock
        self.left = bzz.BZZ_TOTAL  # BZZ output the document may still decode
        self.spent = False         # a chunk hit `left`: the remaining pages are textless
        self.djvi = {}             # component id -> (body start, end) of its FORM:DJVI
        self.shared = {}           # component id -> _Part, or None when the id names no DJVI
        self.used = set()          # shared ids already given to a page

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
        placed = self._place(comps, stop, end)
        pages = []
        number = 0  # page components in DIRM order, dropped ones included, so later pages keep their numbers
        for i, comp in enumerate(comps):
            number += comp.kind == "page"
            if i not in placed:
                continue
            body, form_end = placed[i]
            if comp.kind == "page":
                title = comp.title if comp.title and comp.title != str(number) else ""
                pages.append((number, title, body, form_end))
            elif comp.kind == "djvi":
                self.djvi.setdefault(comp.id, (body, form_end))
        return pages

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
            elif cid == b"INCL" and not nested:
                name = data[body:stop].decode("utf-8", "replace").strip("\x00 \t\r\n")
                if name not in seen:
                    seen.add(name)
                    self._include(name, part)
            if self.spent:
                return

    @staticmethod
    def _dropped(cid: bytes, part: _Part):
        """A chunk that did not fit (already counted `record`) still says what the page held: a dropped text layer
        is not counted `textless` as well, and a dropped image chunk still names the page images."""
        if cid in TEXT_CHUNKS:
            part.text = True
        elif cid in IMAGE_CHUNKS:
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

    def _include(self, name: str, part: _Part):
        if name not in self.shared:
            where = self.djvi.get(name)
            if where is None:
                self._record()
                self.shared[name] = None
            else:
                got = _Part()
                self._read(where[0], where[1], got, nested=True)
                self.shared[name] = got
        got = self.shared[name]
        if got is None:
            return
        part.text = part.text or got.text
        part.images = part.images or got.images
        if name in self.used and got.size:  # a repeat: charged as if decoded again
            if got.size > self.left:
                self.spent, self.left = True, 0
                return
            self.left -= got.size
        self.used.add(name)
        part.paras.extend(got.paras)

    def _page(self, start: int, end: int) -> _Part:
        part = _Part()
        self._read(start, end, part, nested=False)
        return part

    def _fail(self):
        self.ctx.skipped["page"] += 1
        if self.ctx.skipped["page"] == 1:
            self.ctx.warnings.append("page: skipped damaged part")

    def run(self, pages: list[tuple[int, str, int, int]]) -> Doc:
        ctx = self.ctx
        if not pages:
            raise ReadError("damaged", "no pages")
        sections = []
        textless = images = read = 0
        cut = False
        for n, (number, title, start, end) in enumerate(pages):
            if n >= DJVU_PAGES:
                ctx.skipped["page_cut"] += len(pages) - n
                cut = True
                break
            read += 1
            if self.spent:
                textless += 1
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
            if self.spent or not part.text:
                textless += 1
                continue
            sections.append(Section("page", title=title, number=number, blocks=part.paras))
        if images:
            ctx.skipped["page_images"] = 1
        if textless and textless == read:
            noun = "page" if textless == 1 else "pages"
            raise ReadError("unsupported", f"no text layer: {textless} image-only {noun} (OCR is out of scope)")
        if textless:
            ctx.skipped["textless"] += textless
        if not sections and not cut and not textless:
            raise ReadError("damaged", "no readable pages")
        return Doc(blocks=sections)


def parse_bytes(data, ctx, *, started=None, clock=time.monotonic) -> Doc:
    """A DjVu file's text layer as one page section per page. `started` and `clock` are for the page loop's
    deadline (`started` is clock() at entry unless given)."""
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
    return reader.run(pages)


def parse(path, ctx, *, started=None) -> Doc:
    return parse_bytes(Path(path).read_bytes(), ctx, started=started)
