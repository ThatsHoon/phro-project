"""MOBI 6 and KF8 (azw3) Kindle books to the document model: one chapter section per part, in reading order.

The container is `containers/palmdb.py`. The record 0 head, the EXTH records and the refusals (encryption, DRM fields,
compression, a text encoding other than 1252 and 65001) follow Apache Tika (Apache-2.0) `mobi/MobiHeader.java` and
`mobi/MobiParser.java` (main commit a40d54f6): the head fields are read at the same offsets, a field the MOBI head is too
short to hold reads as absent, and the EXTH walk keeps Tika's record and value caps.

MOBI 6 text is HTML, split into chapters at `<mbp:pagebreak`. `filepos` links and anchors are resolved by inserting
`<a id="..."></a>` at the target byte before the text is parsed; `<img recindex>` becomes `src="recindex:N"`, so
`html.py` reads both unchanged.

KF8 follows foliate-js (MIT) `mobi.js` (commit 78914ae): the FDST flows, the INDX/TAGX/CNCX index reader, the SKEL and
FRAG tables and the assembly of each skeleton with its fragments, and the `kindle:flow`, `kindle:embed` and `kindle:pos`
links. Unlike foliate-js, a book whose SKEL index is 0xFFFFFFFF is read from flow 0 split at each `<html`, and a
`kindle:pos` target is an anchor inserted at its byte, as for `filepos`, not an `id`/`aid` lookup. A combo book
(EXTH 121 and a BOUN record) is read from its KF8 half; a KF8 structure that cannot be read falls back to the MOBI 6
copy of a combo, or to the stripped text of flow 0 in a KF8-only book.
"""
import bisect
import html as stdhtml
import itertools
import re
import struct
from dataclasses import dataclass, field, replace

from .. import html as xhtml
from .. import images, limits
from ..containers import palmdb
from ..containers.palmdb import HuffCdic, Palm, palmdoc, record_cap, text_cap, trailing
from ..limits import ReadError
from ..model import Context, Doc, Para, Section, Text

MOBI_ANCHORS = 100_000  # anchors one book may insert
MAX_SHEETS = 32         # stylesheets one chapter may use
MAX_TITLE = 300
MAX_EXTH = 1024         # EXTH records read
MAX_EXTH_VALUES = 64    # values kept per EXTH type
INDX_ENTRIES = 100_000  # entries (and CNCX strings) one index may hold
FDST_FLOWS = 1_000      # flows one FDST may hold
MAX_TAGS = 64           # TAGX tag definitions
MAX_CONTROL = 8         # TAGX control bytes
MAX_VARINT = 5          # bytes of one index varint
FRAG_WORK = 1 << 28     # bytes moved, and fragment positions shifted (64 each), by out-of-place fragment inserts
NULL = 0xFFFFFFFF
EXTH_KF8_BOUNDARY = 121
EXTH_TITLE = 503
_LOOK = 8192            # bytes searched back for the `<` of the tag an anchor position falls in
_CODECS = {1252: "cp1252", 65001: "utf-8"}
_FILEPOS = re.compile(rb"(<a\b[^<>]{0,500}?)\bfilepos\s*=\s*[\"']?(\d{1,12})[\"']?", re.I)
_KINDLE_POS = re.compile(rb"(<a\b[^<>]{0,500}?)\bhref\s*=\s*[\"']?kindle:pos:fid:(\w{1,8}):off:(\w{1,12})"
                         rb"[^\"'<>\s]{0,200}[\"']?", re.I)
_KINDLE_FLOW = re.compile(r"kindle:flow:([0-9A-Va-v]{1,8})", re.I)
_HTML = re.compile(rb"<html\b", re.I)
_RECINDEX = re.compile(rb"(<img\b[^<>]{0,500}?)\brecindex\s*=\s*[\"']?(\d{1,12})[\"']?", re.I)
_PAGEBREAK = re.compile(rb"<mbp:pagebreak\b[^<>]*>(?:\s*</mbp:pagebreak>)?", re.I)
_BLOCK_TAG = re.compile(r"<\s*/?\s*(?:p|div|br|hr|h[1-6]|li|ul|ol|dl|dt|dd|tr|table|blockquote|pre|section|article|"
                        r"mbp:pagebreak)\b[^<>]*>", re.I)
_DEAD = re.compile(r"<\s*(script|style)\b", re.I)
_CLOSE = {"script": re.compile(r"</\s*script\b", re.I), "style": re.compile(r"</\s*style\b", re.I)}
_TAG = re.compile(r"<[^<>]*>")


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
def _drop_dead(s: str) -> str:
    """The text without script and style elements; one with no close tag runs to the end. Linear: each close tag is found
    with a pattern of its own, never by a pattern that could scan to the end from every start."""
    out, pos = [], 0
    while True:
        m = _DEAD.search(s, pos)
        if not m:
            break
        out.append(s[pos:m.start()])
        c = _CLOSE[m.group(1).lower()].search(s, m.end())
        if not c:
            return "".join(out)
        gt = s.find(">", c.end())
        if gt < 0:
            return "".join(out)
        pos = gt + 1
    out.append(s[pos:])
    return "".join(out)


def _stripped(text: bytes, ctx) -> list:
    """Paragraphs of a text with its tags removed, split at block tags. Counts `plain_text`."""
    s = text.decode("utf-8", errors="replace").lstrip("﻿")
    s = _TAG.sub("", _BLOCK_TAG.sub("\0", _drop_dead(s)))
    paras = []
    for piece in stdhtml.unescape(s).split("\0"):
        line = " ".join(piece.split())
        if line:
            paras.append(Para([Text(line)]))
    ctx.skipped["plain_text"] = 1
    ctx.warnings.append("plain_text: read as plain text; the formatting of this book is lost")
    return paras


# ---- KF8 indexes and flows
class _Fallback(Exception):
    """A KF8 structure that cannot be read: a combo reads its MOBI 6 copy, a KF8-only book its stripped text."""


_STRUCTURAL = (_Fallback, ValueError, IndexError, struct.error)


class _View:
    """The records of a combo book from its KF8 record 0 on, numbered from 0, as the KF8 head counts them."""

    def __init__(self, palm: Palm, base: int):
        self.palm, self.base, self.count = palm, base, palm.count - base

    def record(self, i: int) -> bytes:
        if not 0 <= i < self.count:
            raise IndexError(i)
        return self.palm.record(self.base + i)


class _Index(list):
    """The entries (label, {tag: values}) of an index; `cncx` holds its CNCX strings by offset."""
    cncx: dict


def _varint(b: bytes, pos: int) -> tuple:
    """A forward varint (7-bit groups, the last byte's high bit set) at pos, and the position after it."""
    value = 0
    for i in range(MAX_VARINT):
        if pos + i >= len(b):
            raise _Fallback("an index varint runs past its record")
        c = b[pos + i]
        value = value << 7 | c & 0x7F
        if c & 0x80:
            return value, pos + i + 1
    raise _Fallback(f"an index varint is longer than {MAX_VARINT} bytes")


def _indx(palm, start: int, ctx) -> list:
    """The entries of the index whose INDX record is `start`: its TAGX, the index records after it and the CNCX
    records after those, as foliate-js `getIndexData` reads them. Raises _Fallback past a cap or on a broken record."""
    if not 0 <= start < palm.count:
        raise _Fallback("the index record is outside the file")
    r = palm.record(start)
    if len(r) < 56 or r[:4] != b"INDX":
        raise _Fallback("no INDX record")
    hlen, nrec, enc, ncncx = _u32(r, 4), _u32(r, 24), _u32(r, 28), _u32(r, 52)
    left = palm.count - start - 1
    if nrec > left or ncncx > left - nrec:
        raise _Fallback("more index records than records left")
    if hlen + 12 > len(r) or r[hlen:hlen + 4] != b"TAGX":
        raise _Fallback("no TAGX section")
    tlen, nctrl = _u32(r, hlen + 4), _u32(r, hlen + 8)
    if tlen < 12 or (tlen - 12) // 4 > MAX_TAGS:
        raise _Fallback(f"TAGX holds more than {MAX_TAGS} tags")
    if nctrl > MAX_CONTROL:
        raise _Fallback(f"TAGX has more than {MAX_CONTROL} control bytes")
    ntags = (tlen - 12) // 4
    if hlen + 12 + 4 * ntags > len(r):
        raise _Fallback("TAGX runs past its record")
    table = [tuple(r[hlen + 12 + 4 * i:hlen + 16 + 4 * i]) for i in range(ntags)]
    codec = "cp1252" if enc == 1252 else "utf-8"
    out = _Index()
    out.cncx = cncx = {}
    for i in range(ncncx):
        rec = palm.record(start + nrec + 1 + i)
        pos = 0
        while pos < len(rec) and rec[pos]:   # a zero byte is the padding after the last string
            if len(cncx) >= INDX_ENTRIES:
                raise _Fallback(f"more than {INDX_ENTRIES} CNCX strings")
            at = pos
            n, pos = _varint(rec, pos)
            cncx[i * 0x10000 + at] = rec[pos:pos + n].decode(codec, errors="replace")
            pos += n
    for i in range(nrec):
        rec = palm.record(start + 1 + i)
        if len(rec) < 28 or rec[:4] != b"INDX":
            raise _Fallback("no INDX record")
        idxt, n = _u32(rec, 20), _u32(rec, 24)
        if n > INDX_ENTRIES - len(out):
            raise _Fallback(f"more than {INDX_ENTRIES} index entries")
        for j in range(n):
            at = idxt + 4 + 2 * j
            if at + 2 > len(rec):
                raise _Fallback("an IDXT offset runs past its record")
            off = _u16(rec, at)
            if off >= len(rec):
                raise _Fallback("an index entry starts past its record")
            ctrl = off + 1 + rec[off]
            label = rec[off + 1:ctrl].decode(codec, errors="replace")
            pos = ctrl + nctrl
            if pos > len(rec):
                raise _Fallback("an index entry runs past its record")
            tags, ci = [], 0
            for tag, nv, mask, end in table:
                if end & 1:
                    ci += 1
                    continue
                if ci >= nctrl:
                    raise _Fallback("a TAGX tag past the control bytes")
                v = rec[ctrl + ci] & mask
                if v == mask:
                    if mask.bit_count() > 1:
                        size, pos = _varint(rec, pos)
                        tags.append((tag, None, size, nv))
                    else:
                        tags.append((tag, 1, None, nv))
                else:
                    tags.append((tag, v >> ((mask & -mask).bit_length() - 1), None, nv))
            tagmap = {}
            for tag, count, size, nv in tags:
                values = []
                if count is not None:
                    for _ in range(count * nv):
                        x, pos = _varint(rec, pos)
                        values.append(x)
                else:
                    used = 0
                    while used < size:
                        x, nxt = _varint(rec, pos)
                        values.append(x)
                        used += nxt - pos
                        pos = nxt
                tagmap[tag] = values
            out.append((label, tagmap))
    return out


def _fdst(palm, head: Head) -> list:
    """The (start, end) text ranges of the FDST flows; flow 0 is the XHTML. Raises _Fallback past FDST_FLOWS, or on a
    range outside the text or one that starts after its end."""
    if not 0 <= head.fdst < palm.count:
        raise _Fallback("the FDST record is outside the file")
    r = palm.record(head.fdst)
    if len(r) < 12 or r[:4] != b"FDST":
        raise _Fallback("no FDST record")
    n = _u32(r, 8)
    if not 0 < n <= FDST_FLOWS:
        raise _Fallback(f"{n} FDST flows")
    if 12 + 8 * n > len(r):
        raise _Fallback("the FDST table runs past its record")
    flows = []
    for i in range(n):
        s, e = _u32(r, 12 + 8 * i), _u32(r, 16 + 8 * i)
        if s > e or e > head.text_length:
            raise _Fallback("an FDST flow outside the text")
        flows.append((s, e))
    return flows


def _val(tags: dict, tag: int, i: int = 0) -> int:
    values = tags.get(tag) or ()
    if len(values) <= i:
        raise _Fallback(f"index tag {tag} is missing")
    return values[i]


def _assemble(flow0: bytes, skels: list, frags: list) -> tuple:
    """(parts, where, starts): each skeleton cut from flow 0 with its fragments inserted in turn at `insert position −
    skeleton start`, as foliate-js `loadText`; where[fid] = (part, byte position of that fragment in the part); starts
    are the skeleton starts."""
    parts, where, starts = [], {}, []
    nxt = work = 0
    for count, start, length in skels:
        if count > len(frags) - nxt:
            raise _Fallback("a skeleton claims more fragments than are left")
        mine = frags[nxt:nxt + count]
        nxt += count
        size = length + sum(f[3] for f in mine)
        if start + size > len(flow0):
            raise _Fallback("a skeleton runs past flow 0")
        raw = flow0[start:start + size]
        buf = bytearray(raw[:length])
        at, fids = [], []   # fragment positions in buf, sorted, and their fids
        for insert, fid, off, n in mine:
            p = insert - start
            if not 0 <= p <= len(buf):
                raise _Fallback("a fragment is inserted outside its skeleton")
            if length + off + n > size:
                raise _Fallback("a fragment runs past its skeleton")
            k = bisect.bisect_left(at, p)
            work += len(buf) - p + 64 * (len(at) - k)
            if work > FRAG_WORK:
                raise _Fallback("fragment inserts over the work limit")
            for j in range(k, len(at)):
                at[j] += n
            at.insert(k, p)
            fids.insert(k, fid)
            buf[p:p] = raw[length + off:length + off + n]
        for p, fid in zip(at, fids):
            where.setdefault(fid, (len(parts), p))
        parts.append(bytes(buf))
        starts.append(start)
    return parts, where, starts


def _split_html(flow0: bytes) -> list:
    """Flow 0 of a book without skeletons, split at each `<html` (at most INDX_ENTRIES parts; the rest stays in the
    last)."""
    cuts = [m.start() for m in itertools.islice(_HTML.finditer(flow0), INDX_ENTRIES)]
    if not cuts:
        return [flow0]
    cuts[0] = 0
    return [flow0[a:b] for a, b in zip(cuts, cuts[1:] + [len(flow0)])]


def _kindle_pos(parts: list, where: dict, starts: list, utf8: bool, ctx) -> list:
    """The parts with each `kindle:pos:fid:F:off:O` target anchored (`kp` + its book position) and its link pointed
    there. A target outside its part, an unknown fid, or one past MOBI_ANCHORS (counted `link` once) leaves the link its
    text only."""
    targets = [{} for _ in parts]
    names: dict = {}   # (fid, off) as written -> anchor name; never more than MOBI_ANCHORS, so neither are targets
    over = False

    def resolve(m):
        try:
            fid, off = int(m.group(2), 32), int(m.group(3), 32)
        except ValueError:
            return None
        hit = where.get(fid)
        if hit is None or hit[1] + off > len(parts[hit[0]]):
            return None
        return hit[0], hit[1] + off

    for part in parts:
        for m in _KINDLE_POS.finditer(part):
            key = (m.group(2), m.group(3))
            if key in names:
                continue
            hit = resolve(m)
            if hit is None:
                continue
            if len(names) >= MOBI_ANCHORS:
                over = True
                break
            t, pos = hit
            names[key] = targets[t].setdefault(pos, f"kp{starts[t] + pos}")
        if over:
            break
    if over:
        ctx.skipped["link"] += 1
        ctx.warnings.append("link: links past the anchor limit keep their text only")

    def href(m):
        name = names.get((m.group(2), m.group(3)))
        return m.group(1) + (b'href="#' + name.encode("ascii") + b'"' if name else b"")
    return [_KINDLE_POS.sub(href, _anchor(part, targets[t], utf8)) for t, part in enumerate(parts)]


def _kf8_parts(palm, head: Head, base: int, ctx, text: bytes | None = None) -> tuple:
    """(XHTML parts, flows by FDST number) of a KF8 book whose record 0 is `base` (0, or the record after BOUN in a
    combo; the KF8 indexes count from it). A book without FDST is one flow; one without SKEL is flow 0 split at each
    `<html`. Raises _Fallback (or ValueError, IndexError) on a structure that cannot be read."""
    view = _View(palm, base)
    if text is None:
        text = _text(view, head, len(palm._data), ctx)
    ranges = _fdst(view, head) if head.fdst != NULL else [(0, len(text))]
    flows = [text[s:e] for s, e in ranges]
    if not flows[0].strip():
        raise _Fallback("flow 0 is empty")
    if head.skel == NULL:
        return _kindle_pos(_split_html(flows[0]), {}, [], head.encoding == 65001, ctx), flows
    skels = [(_val(t, 1), _val(t, 6), _val(t, 6, 1)) for _, t in _indx(view, head.skel, ctx)]
    frags = []
    if head.frag != NULL:
        for i, (label, t) in enumerate(_indx(view, head.frag, ctx)):
            if not (label.isascii() and label.isdigit()):
                raise _Fallback("a FRAG label is not an insert position")
            frags.append((int(label), _val(t, 4) if t.get(4) else i, _val(t, 6), _val(t, 6, 1)))   # tag 3: file
    parts, where, starts = _assemble(flows[0], skels, frags)
    return _kindle_pos(parts, where, starts, head.encoding == 65001, ctx), flows


def _kf8_head(palm: Palm, head: Head) -> tuple:
    """(base, KF8 head): (0, head) for a KF8 book; for a combo, the record after the BOUN record EXTH 121 names. (0,
    None) when there is no KF8 head to read."""
    if head.version >= 8:
        return 0, head
    value = (head.exth.get(EXTH_KF8_BOUNDARY) or [b""])[0]
    if len(value) < 4:
        return 0, None
    b = _u32(value, 0)
    if not 2 <= b < palm.count or palm.record(b - 1)[:4] != b"BOUN":
        return 0, None
    try:
        kf8 = _head(palm.record(b))
    except ReadError:
        return 0, None
    return (b, kf8) if kf8.version >= 8 else (0, None)


def _first_image(palm: Palm, head: Head, base: int) -> Head:
    """In a combo, the KF8 first image read as absolute, else as relative to the boundary, whichever sniffs as an
    image."""
    if not base or head.first_image == NULL:
        return head
    for i in (head.first_image, base + head.first_image):
        if 0 <= i < palm.count and images.sniff(palm.record(i)) is not None:
            return replace(head, first_image=i)
    return head


def _kf8(data: bytes, palm: Palm, head: Head, kf8: Head, base: int, ctx) -> tuple:
    """(chapter sections, kept notes) of the KF8 half, or of its fallback."""
    view = _View(palm, base)
    scratch = Context() if base else ctx   # a combo's KF8 text counts only if the KF8 half is read
    text = _text(view, kf8, len(data), scratch)
    try:
        parts, flows = _kf8_parts(palm, kf8, base, ctx, text=text)
    except _STRUCTURAL:
        if base:
            ctx.skipped["kf8_fallback"] = 1
            ctx.warnings.append("kf8_fallback: the KF8 layout could not be read; the MOBI 6 copy was read")
            return _mobi6(_text(palm, head, len(data), ctx), palm, head, ctx)
        try:
            s, e = _fdst(view, kf8)[0] if kf8.fdst != NULL else (0, len(text))
            flow0 = text[s:e]
        except _STRUCTURAL:
            flow0 = text
        paras = _stripped(_utf8(flow0, kf8.encoding), ctx)
        return ([Section("chapter", number=1, blocks=paras)] if paras else []), {}
    if base:
        ctx.skipped.update(scratch.skipped)
        ctx.warnings.extend(scratch.warnings)
    codec = _CODECS.get(kf8.encoding, "utf-8")
    sheets: dict = {}

    def sheet(i):
        if i not in sheets:
            sheets[i] = flows[i][:xhtml.MAX_SHEET * 4].decode(codec, errors="replace")
        return sheets[i]
    load = _loader(palm, _first_image(palm, kf8, base), ctx)
    return _chapters(parts, load, lambda node: _css(node, ctx, sheet, len(flows)), kf8.encoding, ctx)


# ---- book
def _title(head: Head) -> str:
    codec = _CODECS.get(head.encoding, "utf-8")
    for raw in (head.exth.get(EXTH_TITLE) or [b""])[:1] + [head.full_title]:
        title = " ".join(raw[:MAX_TITLE * 4].decode(codec, errors="replace").split())[:MAX_TITLE]
        if title:
            return title
    return ""


def _css(node, ctx, sheet=None, flows: int = 0) -> xhtml.Css:
    """The stylesheets of a part: its `<style>` elements and, in KF8, the flows its `kindle:flow:N?mime=text/css`
    links name (sheet(N) gives a flow's text). At most MAX_SHEETS, in document order."""
    texts, stack = [], [node]
    while stack and len(texts) < MAX_SHEETS:
        n = stack.pop()
        if not isinstance(n, xhtml.Node):
            continue
        if n.tag == "style":
            texts.append("".join(c for c in n.children if isinstance(c, str)))
        elif n.tag == "link" and sheet is not None:
            href = n.attrs.get("href", "").strip()
            m = _KINDLE_FLOW.match(href)
            if m and ("text/css" in href.lower() or "stylesheet" in n.attrs.get("rel", "").lower().split()):
                i = int(m.group(1), 32)
                if 0 < i < flows:
                    texts.append(sheet(i))
        stack.extend(reversed(n.children))
    css = xhtml.Css(texts)
    if css.capped:
        ctx.skipped["css"] += 1
        ctx.warnings.append("css: stylesheet limits reached; part of the rules were not read")
    return css


def _mobi6(text: bytes, palm: Palm, head: Head, ctx) -> tuple:
    """(chapter sections, kept notes) of the MOBI 6 text."""
    utf8 = head.encoding == 65001
    targets, over = {}, False
    for m in _FILEPOS.finditer(text):  # the first MOBI_ANCHORS targets by occurrence
        pos = int(m.group(2))
        if pos in targets:
            continue
        if len(targets) >= MOBI_ANCHORS:
            over = True
            break
        if pos <= len(text):
            targets[pos] = f"fp{pos}"
    if over:
        ctx.skipped["link"] += 1
        ctx.warnings.append("link: links past the anchor limit keep their text only")
    text = _anchor(text, targets, utf8)

    def href(m):  # a link whose target was not kept loses its filepos and stays plain text
        pos = int(m.group(2))
        return m.group(1) + (b'href="#fp' + str(pos).encode() + b'"' if pos in targets else b"")
    text = _FILEPOS.sub(href, text)
    text = _RECINDEX.sub(lambda m: m.group(1) + b'src="recindex:' + m.group(2) + b'"', text)
    return _chapters(_PAGEBREAK.split(text), _loader(palm, head, ctx), lambda node: _css(node, ctx), head.encoding, ctx)


def _chapters(pieces, load, css, encoding: int, ctx) -> tuple:
    """(chapter sections, kept notes) of the text pieces, each parsed as one part under ctx.guard("chapter")."""
    notes: dict = {}
    sections = []
    for piece in pieces:
        if not piece.strip():
            continue
        try:
            with ctx.guard("chapter"):
                node = xhtml.tree(_utf8(piece, encoding))
                blocks = xhtml.blocks(node, css=css(node), load=load, notes=notes, ctx=ctx)
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
    doc = Doc(title=_title(head))
    base, kf8 = _kf8_head(palm, head)
    if kf8 is None:
        sections, notes = _mobi6(_text(palm, head, len(data), ctx), palm, head, ctx)
    else:
        _refuse(kf8)
        sections, notes = _kf8(data, palm, head, kf8, base, ctx)
    if not sections:
        raise ReadError("damaged", "no readable chapter")
    doc.blocks, doc.notes = sections, notes
    return doc


def parse(path, ctx) -> Doc:
    with open(path, "rb") as fh:
        return parse_bytes(fh.read(), ctx)
