"""The Jet and ACE page layer of Access databases (mdb, accdb): page 0, pages, usage maps, page chains and Jet text.

Ported from Jackcess 5.0.4 (Apache-2.0; the Maven Central sources jar), `com/healthmarketscience/jackcess/impl/`:
`JetFormat.java` (the version codes, page sizes, the header mask's offset and length, the code page and encoding
key offsets), `PageChannel.java` (the header mask applied to page 0), `UsageMap.java` (inline and reference maps),
`DefaultCodecProvider.java` and `CodecHandler.java` (the encoding key at 0x3E selects a page codec) and
`ColumnImpl.java` (`decodeTextValue`, the compressed unicode form). mdbtools' `HACKING.md` (LGPL) was read as format
knowledge only and no code is taken from it.

Page 0. `00 01 00 00`, the 15-byte engine name at 4 and the version code at 0x14: 0 Jet3 (2,048-byte pages), 1 Jet4,
2, 3, 5 and 6 ACE 12, 14, 16 and 17 (4,096). The 126 (Jet3) or 128 bytes from 0x18 are RC4 with the fixed key
`C7 DA 39 6B`; Jackcess holds the same bytes as its precomputed `BASE_HEADER_MASK`. `header` is those bytes decoded,
`header[i]` being the page-0 byte at 0x18 + i, and `page(0)` holds them in place, so the code page (u16 at 0x3C),
the db_key (u32 at 0x3E) and the password (0x42) are read at their absolute offsets there.

Code page. The u16 at 0x3C is a Windows code page number, as Jackcess's `DatabaseImpl.getCodePageCharset` reads it:
`codepage` is the codec `cp<n>` when Python knows that name, else `cp1252` (also for 0), and nothing is counted.
The phase 4 spec named `text.codepage` here; that helper maps a language id, not a code page, so it is not used.

db_key. A non-zero u32 at 0x3E in a Jet3 or Jet4 file means every page but 0 is RC4 keyed by `db_key XOR n` (four
bytes, little-endian), Jackcess's Jet codec (the encoding holds no user secret). In ACE the same field means Office
encryption, which is not decoded here: `page` returns those pages as stored and `jet.py` refuses the file. A decoded
page is kept (at most `limits.JET_PAGE_CACHE`, the least recently read dropped first), so the many reads of one page
in a pass (an overflow target, an LVAL row, a usage map) decode it once. The pages decoded in all are at most
`max(limits.DECRYPT_BYTES, 2 x the file)` bytes, so a pattern the cache cannot hold (a cycle over more pages than
it keeps) stops at that: past it `page` raises `too_large` ("part: RC4 pages"), as `crypto.rc4` does past
DECRYPT_BYTES for one stream.

Bounds. `page(n)` raises ValueError outside `0 <= n < pages`. A usage map drops every page id outside
`1 <= id < pages`, reads each type 5 page once and stops at the pointer whose range starts past the file, so its
work is bounded by the page count. `chain` keeps a seen-set and yields at most `pages` items; a repeat, an id out
of range or a step giving None ends it. Malformed map bytes raise ValueError; callers count the member.
"""
import codecs
import struct
from collections import OrderedDict

from .. import limits
from ..containers import crypto
from ..limits import ReadError

HEADER_AT = 0x18
HEADER_KEY = b"\xc7\xda\x39\x6b"
VERSIONS = {0: "Jet3", 1: "Jet4", 2: "ACE12", 3: "ACE14", 5: "ACE16", 6: "ACE17"}
ENGINES = (b"Standard Jet DB", b"Standard ACE DB")
MSISAM = b"MSISAM Database"
USAGE_PAGE = 0x05
_MAP_INLINE, _MAP_REFERENCE = 0, 1
_MAP_DATA = 4  # where the bitmap starts on a type 5 page (Jackcess OFFSET_USAGE_MAP_PAGE_DATA)


def open(data: bytes) -> "Jet":
    """Read page 0 of a Jet or ACE file held in memory."""
    data = bytes(data)
    if data[:4] == b"\x01\x00\x00\x00":
        raise ReadError("unsupported", "Access 1.x or 2.0 database")
    if len(data) < HEADER_AT + 128 or data[:4] != b"\x00\x01\x00\x00":
        raise ReadError("damaged", "jet: not a Jet database")
    if data[4:19] == MSISAM:
        raise ReadError("unsupported", "Microsoft Money database (MSISAM)")
    version = data[0x14]
    if version not in VERSIONS:
        raise ReadError("unsupported", f"unknown Jet version {version}")
    if data[4:19] not in ENGINES:
        raise ReadError("damaged", "jet: not a Jet database")
    return Jet(data, version)


def _codec(n: int) -> str:
    if n > 0:
        try:
            codecs.lookup(f"cp{n}")
            return f"cp{n}"
        except LookupError:
            pass
    return "cp1252"


class Jet:
    """One Jet or ACE file: `version`, `page_size`, `pages`, `codepage`, `db_key` and the decoded `header`."""

    def __init__(self, data: bytes, version: int):
        self._data = data
        self.version = version
        self.page_size = 2048 if version == 0 else 4096
        if len(data) < self.page_size:
            raise ReadError("damaged", "jet: shorter than one page")
        self.pages = len(data) // self.page_size
        size = 126 if version == 0 else 128
        self.header = crypto.rc4(HEADER_KEY, data[HEADER_AT:HEADER_AT + size])
        self._page0 = data[:HEADER_AT] + self.header + data[HEADER_AT + size:self.page_size]
        self.codepage = _codec(struct.unpack_from("<H", self._page0, 0x3C)[0])
        self.db_key = struct.unpack_from("<I", self._page0, 0x3E)[0]
        self._cache = OrderedDict()  # page number -> decoded page (db_key only)
        self._rc4_left = max(limits.DECRYPT_BYTES, 2 * len(data))  # page bytes RC4 may still decode

    def page(self, n: int) -> bytes:
        """Page `n`, decoded (page 0 with its header unmasked; with a Jet3/Jet4 db_key, the RC4 of the others)."""
        if not 0 <= n < self.pages:
            raise ValueError(f"page {n} outside 0..{self.pages - 1}")
        if n == 0:
            return self._page0
        if not (self.db_key and self.version <= 1):
            return self._data[n * self.page_size:(n + 1) * self.page_size]
        out = self._cache.get(n)
        if out is not None:
            self._cache.move_to_end(n)
            return out
        raw = self._data[n * self.page_size:(n + 1) * self.page_size]
        if len(raw) > self._rc4_left:
            raise ReadError("too_large", "part: RC4 pages")
        self._rc4_left -= len(raw)
        out = self._cache[n] = crypto.rc4(((self.db_key ^ n) & 0xFFFFFFFF).to_bytes(4, "little"), raw)
        while len(self._cache) > max(limits.JET_PAGE_CACHE, 1):
            self._cache.popitem(last=False)
        return out

    def next_pg(self, n: int):
        """The `next_pg` step of TDEF and other page chains: the u32 at offset 4 of page `n`, None for 0."""
        nxt = struct.unpack_from("<I", self.page(n), 4)[0]
        return nxt or None

    def chain(self, start, step):
        """Follow `step(item) -> next item | None` from `start`. An item is a page number or a tuple whose first
        field is one (an LVAL (page, row)). The walk ends at a repeat, at a page outside 1..pages-1, or after
        `pages` items."""
        seen = set()
        item = start
        while item is not None and len(seen) < self.pages:
            pg = item if isinstance(item, int) else item[0]
            if not (isinstance(pg, int) and 0 < pg < self.pages) or item in seen:
                return
            seen.add(item)
            yield item
            item = step(item)

    def usage(self, map_bytes: bytes) -> list:
        """The page ids a usage map row names, ascending: type 0 (start page, then a bitmap) or type 1 (u32 pointers
        to type 5 pages, each a bitmap of (page_size - 4) * 8 pages). Ids outside 1..pages-1 are dropped."""
        m = bytes(map_bytes)
        if not m:
            raise ValueError("empty usage map")
        if m[0] == _MAP_INLINE:
            if len(m) < 5:
                raise ValueError("usage map shorter than its start page")
            return self._bits(m, 5, struct.unpack_from("<I", m, 1)[0])
        if m[0] != _MAP_REFERENCE:
            raise ValueError(f"unknown usage map type {m[0]}")
        per = (self.page_size - _MAP_DATA) * 8
        out = []
        seen = set()
        for i in range((len(m) - 1) // 4):
            if i * per >= self.pages:
                break
            ptr = struct.unpack_from("<I", m, 1 + 4 * i)[0]
            if not 0 < ptr < self.pages or ptr in seen:
                continue
            seen.add(ptr)
            page = self.page(ptr)
            if page[0] != USAGE_PAGE:
                continue
            out += self._bits(page, _MAP_DATA, i * per)
        return out

    def _bits(self, buf: bytes, at: int, base: int) -> list:
        out = []
        top = self.pages
        for k in range(at, len(buf)):
            b = buf[k]
            if not b:
                continue
            first = base + (k - at) * 8
            if first >= top:
                break
            for bit in range(8):
                if b >> bit & 1:
                    pg = first + bit
                    if 0 < pg < top:
                        out.append(pg)
        return out

    def text(self, raw: bytes, compressed: bool) -> str:
        """A text value: Jet3 bytes in `codepage`; Jet4 and ACE UCS-2, or, when `compressed` (the column's
        compressed unicode flag) and the value starts `FF FE`, the compressed form: one-byte mode first, every
        0x00 at a character boundary switching between one-byte and two-byte mode (Jackcess `decodeTextValue`)."""
        raw = bytes(raw)
        if self.version == 0:
            return raw.decode(self.codepage, "replace")
        if not (compressed and raw[:2] == b"\xff\xfe"):
            return raw.decode("utf-16-le", "replace")
        out = []
        start = 2
        one = True
        n = len(raw)
        while start <= n:
            end = raw.find(b"\x00", start)
            if not one:
                while end >= 0 and (end - start) % 2:  # the 00 high byte of a character such as "d" (64 00)
                    end = raw.find(b"\x00", end + 1)
            stop = n if end < 0 else end
            seg = raw[start:stop]
            out.append(seg.decode("latin-1") if one else seg.decode("utf-16-le", "replace"))
            if end < 0:
                break
            one = not one
            start = end + 1
        return "".join(out)
