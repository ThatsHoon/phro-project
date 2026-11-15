"""MOBI 6 Kindle books to the document model: one chapter section per `<mbp:pagebreak`, in reading order.

The container is `containers/palmdb.py`. The record 0 head, the EXTH records and the refusals (encryption, DRM fields,
compression, a text encoding other than 1252 and 65001) follow Apache Tika (Apache-2.0) `mobi/MobiHeader.java` and
`mobi/MobiParser.java` (main commit a40d54f6): the head fields are read at the same offsets, a field the MOBI head is too
short to hold reads as absent, and the EXTH walk keeps Tika's record and value caps.

The text is HTML. `filepos` links and anchors are resolved by inserting `<a id="..."></a>` at the target byte before the
text is parsed; `<img recindex>` becomes `src="recindex:N"`, so `html.py` reads both unchanged.
"""
import html as stdhtml
import re
from dataclasses import dataclass, field

from .. import html as xhtml
from .. import images, limits
from ..containers import palmdb
from ..containers.palmdb import HuffCdic, Palm, palmdoc, record_cap, text_cap, trailing
from ..limits import ReadError
from ..model import Doc, Para, Section, Text

MOBI_ANCHORS = 100_000  # anchors one book may insert
MAX_SHEETS = 32         # stylesheets one chapter may use
MAX_TITLE = 300
MAX_EXTH = 1024         # EXTH records read
MAX_EXTH_VALUES = 64    # values kept per EXTH type
NULL = 0xFFFFFFFF
EXTH_KF8_BOUNDARY = 121
EXTH_TITLE = 503
_LOOK = 8192            # bytes searched back for the `<` of the tag an anchor position falls in
_CODECS = {1252: "cp1252", 65001: "utf-8"}
_FILEPOS = re.compile(rb"(<a\b[^>]{0,500}?)\bfilepos\s*=\s*[\"']?(\d{1,12})[\"']?", re.I)
_RECINDEX = re.compile(rb"(<img\b[^>]{0,500}?)\brecindex\s*=\s*[\"']?(\d{1,12})[\"']?", re.I)
_PAGEBREAK = re.compile(rb"<mbp:pagebreak\b[^>]*>(?:\s*</mbp:pagebreak>)?", re.I)
_BLOCK_TAG = re.compile(r"<\s*/?\s*(?:p|div|br|hr|h[1-6]|li|ul|ol|dl|dt|dd|tr|table|blockquote|pre|section|article|"
                        r"mbp:pagebreak)\b[^>]*>", re.I)
_DEAD = re.compile(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]*>")


@dataclass
class Head:
    compression: int = 1
    text_length: int = 0
    text_records: int = 0
    record_size: int = 4096
    encryption: int = 0
    drm_offset: int = NULL
    drm_count: int = 0
    mobi_length: int = 0
    encoding: int = 1252
    version: int = 0
    first_image: int = NULL
    huff_record: int = 0
    huff_count: int = 0
    exth: dict = field(default_factory=dict)
    fdst: int = NULL
    skel: int = NULL
    frag: int = NULL
    ncx: int = NULL
    extra_flags: int = 0
    full_title: bytes = b""


def _u16(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 2], "big")


def _u32(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 4], "big")


def _head(r: bytes) -> Head:
    """The PalmDOC and MOBI head of record 0, with its EXTH records and full title."""
    if len(r) < 16 + 24:
        raise ReadError("damaged", "record 0 is too short for a MOBI head")
    if r[16:20] != b"MOBI":
        raise ReadError("damaged", "record 0 has no MOBI header")
    length = _u32(r, 20)
    if length < 24 or 16 + length > len(r):
        raise ReadError("damaged", f"MOBI header length {length} does not fit record 0")
    h = Head(compression=_u16(r, 0), text_length=_u32(r, 4), text_records=_u16(r, 8), record_size=_u16(r, 10),
             encryption=_u16(r, 12), mobi_length=length)

    def f(at, default=NULL):  # a 4-byte head field; absent when the head ends before it
        return _u32(r, at) if at + 4 <= 16 + length else default

    h.encoding = f(28, 0)
    h.version = f(36, 0)
    h.first_image = f(108)
    h.huff_record, h.huff_count = f(112, 0), f(116, 0)
    h.drm_offset, h.drm_count = f(168), f(172, 0)
    h.fdst, h.ncx, h.frag, h.skel = f(192), f(244), f(248), f(252)
    if length >= 0xE4 and h.version >= 5:
        h.extra_flags = _u16(r, 242)
    name_at, name_len = f(84, 0), f(88, 0)
    if 0 < name_len and name_at + name_len <= len(r):
        h.full_title = r[name_at:name_at + min(name_len, MAX_TITLE * 4)]
    if f(128, 0) & 0x40:
        h.exth = _exth(r, 16 + length)
    return h


def _exth(r: bytes, at: int) -> dict:
    out: dict = {}
    if at + 12 > len(r) or r[at:at + 4] != b"EXTH":
        return out
    count = _u32(r, at + 8)
    pos = at + 12
    for _ in range(min(count, MAX_EXTH)):
        if pos + 8 > len(r):
            break
        kind, size = _u32(r, pos), _u32(r, pos + 4)
        if size < 8 or pos + size > len(r):
            break
        values = out.setdefault(kind, [])
        if len(values) < MAX_EXTH_VALUES:
            values.append(r[pos + 8:pos + size])
        pos += size
    return out


def _refuse(head: Head):
    if head.encryption in (1, 2) or (head.drm_offset != NULL and head.drm_count > 0):
        raise ReadError("drm", "the text is encrypted")
    if head.encryption:
        raise ReadError("unsupported", f"encryption {head.encryption}")
    if head.compression not in (1, 2, 17480):
        raise ReadError("unsupported", f"compression {head.compression}")
    if head.encoding not in _CODECS:
        raise ReadError("unsupported", f"text encoding {head.encoding}")


# ---- text
def _text(palm: Palm, head: Head, file_size: int, ctx) -> bytes:
    """Text records 1..n, each trimmed of its trailing entries and unpacked to its cap, joined. A record that fails or
    is cut counts `record`; text past text_cap is cut and counts `truncated` once."""
    n = min(head.text_records, palm.count - 1)
    bad = 0
    if n < head.text_records:
        bad += 1
    huff = None
    if head.compression == 17480:
        first, count = head.huff_record, head.huff_count
        if count < 2 or first < 1 or first + count > palm.count:
            raise ReadError("damaged", "HUFF/CDIC records are outside the file")
        try:
            huff = HuffCdic(palm.record(first), [palm.record(first + i) for i in range(1, count)])
        except ValueError as e:
            raise ReadError("damaged", f"HUFF/CDIC dictionary: {e}") from None
    rcap, tcap = record_cap(head.record_size), text_cap(file_size)
    out, total = [], 0
    for i in range(1, n + 1):
        rec = trailing(palm.record(i), head.extra_flags)
        try:
            if head.compression == 1:
                data, cut = rec[:rcap], len(rec) > rcap
            elif head.compression == 2:
                data, cut = palmdoc(rec, rcap)
            else:
                data, cut = huff.unpack(rec, rcap)
        except ValueError:
            bad += 1
            continue
        bad += cut
        if total + len(data) > tcap:
            out.append(data[:tcap - total])
            ctx.skipped["truncated"] += 1
            ctx.warnings.append("truncated: the text was cut at the size limit")
            break
        out.append(data)
        total += len(data)
    if bad:
        ctx.skipped["record"] += bad
        ctx.warnings.append(f"record: {bad} text record(s) were damaged or cut")
    return b"".join(out)


def _utf8(text: bytes, encoding: int) -> bytes:
    """The text as UTF-8 behind a BOM, so a `<meta charset>` in it cannot override the book's own encoding."""
    return b"\xef\xbb\xbf" + text.decode(_CODECS.get(encoding, "utf-8"), errors="replace").encode("utf-8")


def _anchor(text: bytes, targets: dict, utf8: bool = True) -> bytes:
    """`<a id="..."></a>` inserted at the byte positions of `targets` (position -> id). A position inside a tag moves
    before that tag's `<`, one inside a UTF-8 sequence back to its lead byte, one past the end is dropped. At most
    MOBI_ANCHORS are inserted."""
    n = len(text)
    spots: dict = {}
    for pos in sorted(p for p in targets if 0 <= p <= n)[:MOBI_ANCHORS]:
        p = pos
        if utf8:
            while 0 < p < n and text[p] & 0xC0 == 0x80 and pos - p < 3:
                p -= 1
        lo = max(0, p - _LOOK)
        lt = text.rfind(b"<", lo, p)
        if lt >= 0 and lt > text.rfind(b">", lo, p):
            p = lt
        spots.setdefault(p, []).append(targets[pos])
    if not spots:
        return text
    out, last = [], 0
    for p in sorted(spots):
        out.append(text[last:p])
        out.extend(b'<a id="' + str(i).encode("ascii", "replace") + b'"></a>' for i in spots[p])
        last = p
    out.append(text[last:])
    return b"".join(out)


# ---- images
def _loader(palm: Palm, head: Head, ctx):
    """src -> image bytes. `recindex:N` and `kindle:embed:XXXX` (base 32) name record first_image + n - 1; a record that
    is not a picture, or bytes past IMAGE_BYTES, give None (html counts it as `image`)."""
    used = [0]

    def load(src: str):
        src = src.strip()
        try:
            if src.startswith("recindex:"):
                n = int(src[9:])
            elif src.startswith("kindle:embed:"):
                n = int(src[13:].split("?", 1)[0], 32)
            else:
                return None
        except ValueError:
            return None
        if head.first_image == NULL or n < 1:
            return None
        i = head.first_image + n - 1
        if i >= palm.count:
            return None
        data = palm.record(i)
        if images.sniff(data) is None:
            return None
        used[0] += len(data)
        return data if used[0] <= limits.IMAGE_BYTES else None
    return load


# ---- stripped text
def _stripped(text: bytes, ctx) -> list:
    """Paragraphs of a text with its tags removed, split at block tags. Counts `plain_text`."""
    s = text.decode("utf-8", errors="replace").lstrip("﻿")
    s = _TAG.sub("", _BLOCK_TAG.sub("\0", _DEAD.sub("", s)))
    paras = []
    for piece in stdhtml.unescape(s).split("\0"):
        line = " ".join(piece.split())
        if line:
            paras.append(Para([Text(line)]))
    ctx.skipped["plain_text"] = 1
    ctx.warnings.append("plain_text: read as plain text; the formatting of this book is lost")
    return paras


# ---- book
def _kf8_only(head: Head) -> bool:
    """A KF8 book without MOBI 6 text. Task 4 replaces this branch with the KF8 reader."""
    return head.version >= 8


def _title(head: Head) -> str:
    codec = _CODECS.get(head.encoding, "utf-8")
    raw = (head.exth.get(EXTH_TITLE) or [head.full_title])[0]
    return " ".join(raw.decode(codec, errors="replace").split())[:MAX_TITLE]


def _css(node, ctx) -> xhtml.Css:
    texts, stack = [], [node]
    while stack and len(texts) < MAX_SHEETS:
        n = stack.pop()
        if not isinstance(n, xhtml.Node):
            continue
        if n.tag == "style":
            texts.append("".join(c for c in n.children if isinstance(c, str)))
        stack.extend(reversed(n.children))
    css = xhtml.Css(texts)
    if css.capped:
        ctx.skipped["css"] += 1
        ctx.warnings.append("css: stylesheet limits reached; part of the rules were not read")
    return css


def _mobi6(text: bytes, palm: Palm, head: Head, ctx) -> tuple:
    """(chapter sections, kept notes) of the MOBI 6 text."""
    utf8 = head.encoding == 65001
    targets = {}
    for m in _FILEPOS.finditer(text):
        targets[int(m.group(2))] = f"fp{int(m.group(2))}"
    if len(targets) > MOBI_ANCHORS:
        ctx.skipped["link"] += 1
        ctx.warnings.append("link: links past the anchor limit keep their text only")
    text = _anchor(text, targets, utf8)
    text = _FILEPOS.sub(lambda m: m.group(1) + b'href="#fp' + str(int(m.group(2))).encode() + b'"', text)
    text = _RECINDEX.sub(lambda m: m.group(1) + b'src="recindex:' + m.group(2) + b'"', text)
    load = _loader(palm, head, ctx)
    notes: dict = {}
    sections = []
    for piece in _PAGEBREAK.split(text):
        if not piece.strip():
            continue
        try:
            with ctx.guard("chapter"):
                node = xhtml.tree(_utf8(piece, head.encoding))
                blocks = xhtml.blocks(node, css=_css(node, ctx), load=load, notes=notes, ctx=ctx)
                if blocks:
                    sections.append(Section("chapter", number=len(sections) + 1, blocks=blocks))
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["chapter"] += 1
    doc_notes = {}
    keep = xhtml.referenced(sections, notes)
    for k in keep:
        doc_notes[k] = notes[k]
    if len(notes) > len(doc_notes):
        ctx.skipped["note"] += len(notes) - len(doc_notes)
    for s in sections:
        xhtml.settle(s.blocks, doc_notes)
    for nb in doc_notes.values():
        xhtml.settle(nb, doc_notes)
    return sections, doc_notes


def parse_bytes(data: bytes, ctx) -> Doc:
    if data[:2] == b"PK":
        raise ReadError("unsupported", "KFX or another zip container")
    if data[:3] == b"TPZ" or data[:4] == b"CONT":
        raise ReadError("unsupported", "Topaz or KFX book")
    palm = Palm(data)
    if palm.type_creator == b"TEXtREAd":
        raise ReadError("unsupported", "PalmDOC text, not a Kindle book")
    if palm.type_creator != b"BOOKMOBI":
        raise ReadError("unsupported", f"Palm database type {palm.type_creator.decode('latin-1')[:8]!r}")
    head = _head(palm.record(0))
    _refuse(head)
    text = _text(palm, head, len(data), ctx)
    doc = Doc(title=_title(head))
    if _kf8_only(head):
        paras = _stripped(_utf8(text, head.encoding), ctx)
        sections, notes = ([Section("chapter", number=1, blocks=paras)] if paras else []), {}
    else:
        sections, notes = _mobi6(text, palm, head, ctx)
    if not sections:
        raise ReadError("damaged", "no readable chapter")
    doc.blocks, doc.notes = sections, notes
    return doc


def parse(path, ctx) -> Doc:
    with open(path, "rb") as fh:
        return parse_bytes(fh.read(), ctx)
