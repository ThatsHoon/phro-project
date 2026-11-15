"""PalmDB container of Kindle books: the record table, trailing entries, PalmDOC LZ77 and HUFF/CDIC.

Ported from Apache Tika (Apache-2.0) `mobi/PalmDatabase.java`, `mobi/PalmDoc.java` and `mobi/HuffCdic.java`
(main commit a40d54f6). Tika's checks and caps are kept: a record table that does not fit, offsets that go
backwards, a PalmDOC distance of 0 or past the output, HUFF/CDIC tables out of bounds, phrase nesting, a phrase that
refers to itself, a dictionary over `HUFF_DICT` and a phrase over `HUFF_PHRASE`.
"""
from ..limits import ReadError

MOBI_TEXT = 64 * 2**20
HUFF_DEPTH = 32
HUFF_DICT = 16 * 2**20
HUFF_PHRASE = 64 * 2**10

_HEADER = 78
_ENTRY = 8
_MAX_CODE = 32
_PHRASE_OVERHEAD = 64


def record_cap(size_field: int) -> int:
    return (size_field or 4096) + 8


def text_cap(file_size: int) -> int:
    return min(MOBI_TEXT, 32 * file_size)


def _u16(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 2], "big")


def _u32(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 4], "big")


class Palm:
    def __init__(self, data: bytes):
        if len(data) < _HEADER:
            raise ReadError("damaged", "shorter than a Palm database header")
        count = _u16(data, 76)
        if count == 0 or _HEADER + count * _ENTRY > len(data):
            raise ReadError("damaged", f"{count} records do not fit the file")
        offsets = []
        prev = _HEADER + count * _ENTRY
        for i in range(count):
            off = _u32(data, _HEADER + i * _ENTRY)
            if off < prev:
                raise ReadError("damaged", f"record {i} starts before the record before it")
            if off > len(data):
                raise ReadError("damaged", f"record {i} starts past the end")
            offsets.append(off)
            prev = off
        self._data = data
        self._offsets = offsets
        self.name = bytes(data[:32]).split(b"\0", 1)[0]
        self.type_creator = bytes(data[60:68])
        self.count = count

    def record(self, i: int) -> bytes:
        if not 0 <= i < self.count:
            raise IndexError(i)
        end = self._offsets[i + 1] if i + 1 < self.count else len(self._data)
        return bytes(self._data[self._offsets[i]:end])


def trailing(rec: bytes, flags: int) -> bytes:
    """The record without its trailing entries (flag bits 1 and up) and multibyte tail (bit 0)."""
    length = len(rec)
    cut = 0
    bits = flags >> 1
    while bits:
        if bits & 1:
            cut = min(length, cut + _entry_size(rec, length - cut))
        bits >>= 1
    if flags & 1 and length - cut > 0:
        cut += (rec[length - cut - 1] & 3) + 1
    return rec[:length - min(cut, length)]


def _entry_size(rec: bytes, length: int) -> int:
    # a backward variable-length integer: 7 bits a byte, the byte with the high bit set is the first of the number
    size = 0
    shift = 0
    while length > 0:
        b = rec[length - 1]
        size |= (b & 0x7F) << shift
        shift += 7
        length -= 1
        if b & 0x80 or shift >= 28:
            break
    return size


def palmdoc(rec: bytes, limit: int) -> tuple[bytes, bool]:
    """PalmDOC LZ77: (output, True when it stopped at `limit`). A bad back reference raises ValueError."""
    out = bytearray()
    i = 0
    end = len(rec)
    cut = False
    while i < end:
        if len(out) >= limit:
            cut = True
            break
        c = rec[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7F:
            out.append(c)
        elif c <= 8:
            n = min(c, end - i)
            take = min(n, limit - len(out))
            out += rec[i:i + take]
            i += c
            if take < n:
                cut = True
                break
        elif c >= 0xC0:
            out.append(0x20)
            if len(out) < limit:
                out.append(c ^ 0x80)
            else:
                cut = True
                break
        else:
            if i >= end:
                break
            pair = (c << 8) | rec[i]
            i += 1
            dist = (pair >> 3) & 0x7FF
            run = (pair & 7) + 3
            if dist == 0 or dist > len(out):
                raise ValueError("PalmDOC distance outside the output")
            for _ in range(run):
                if len(out) >= limit:
                    cut = True
                    break
                out.append(out[-dist])
            if cut:
                break
    return bytes(out), cut


class HuffCdic:
    def __init__(self, huff: bytes, cdics: list[bytes]):
        self._len = [0] * 256
        self._settled = [False] * 256
        self._base = [0] * 256
        self._smallest = [0] * (_MAX_CODE + 1)
        self._base_len = [0] * (_MAX_CODE + 1)
        self._phrases: list[list] = []  # [bytes, literal, decoding]
        self._bytes = 0
        self._tables(huff)
        for cdic in cdics:
            self._read_phrases(cdic)

    @staticmethod
    def _index_base(base: int, length: int) -> int:
        return ((base + 1) << (32 - length)) - 1

    def _tables(self, huff: bytes):
        if len(huff) < 24 or huff[:4] != b"HUFF":
            raise ReadError("damaged", "the HUFF record has no HUFF header")
        first_at = _u32(huff, 8)
        len_at = _u32(huff, 12)
        if first_at + 256 * 4 > len(huff) or len_at + _MAX_CODE * 8 > len(huff):
            raise ReadError("damaged", "the HUFF record is shorter than its tables")
        for first in range(256):
            entry = _u32(huff, first_at + first * 4)
            length = entry & 0x1F
            settled = bool(entry & 0x80)
            # `length > _MAX_CODE` is defensive: the 5-bit field cannot exceed 31
            if length == 0 or length > _MAX_CODE or (length <= 8 and not settled):
                raise ReadError("damaged", f"the HUFF table has an invalid entry for first byte {first}")
            self._len[first] = length
            self._settled[first] = settled
            self._base[first] = self._index_base(entry >> 8, length)
        for length in range(1, _MAX_CODE + 1):
            at = len_at + (length - 1) * 8
            self._smallest[length] = _u32(huff, at) << (32 - length)
            self._base_len[length] = self._index_base(_u32(huff, at + 4), length)

    def _charge(self, n: int):
        self._bytes += n
        if self._bytes > HUFF_DICT:
            raise ValueError("the CDIC dictionary is too large")

    def _read_phrases(self, cdic: bytes):
        if len(cdic) < 16 or cdic[:4] != b"CDIC":
            raise ReadError("damaged", "a CDIC record has no CDIC header")
        total = _u32(cdic, 8)
        bits = _u32(cdic, 12)
        if bits > 16:
            raise ReadError("damaged", f"a CDIC record claims {bits} index bits")
        for i in range(min(1 << bits, total - len(self._phrases))):
            at = 16 + i * 2
            if at + 2 > len(cdic):
                raise ReadError("damaged", "a CDIC record ends inside its offset table")
            start = 16 + _u16(cdic, at)
            if start + 2 > len(cdic):
                raise ReadError("damaged", "a CDIC phrase starts past the record")
            lf = _u16(cdic, start)
            n = lf & 0x7FFF
            if start + 2 + n > len(cdic):
                raise ReadError("damaged", "a CDIC phrase ends past the record")
            self._charge(_PHRASE_OVERHEAD + n)
            self._phrases.append([cdic[start + 2:start + 2 + n], bool(lf & 0x8000), False])

    def unpack(self, rec: bytes, limit: int) -> tuple[bytes, bool]:
        return self._decode(rec, limit, 0)

    def _decode(self, stream: bytes, limit: int, depth: int) -> tuple[bytes, bool]:
        out = bytearray()
        total_bits = len(stream) * 8
        pos = 0
        while True:
            byte_at = pos >> 3
            chunk = stream[byte_at:byte_at + 5].ljust(5, b"\0")
            window = (int.from_bytes(chunk, "big") >> (8 - (pos & 7))) & 0xFFFFFFFF
            first = window >> 24
            length = self._len[first]
            base = self._base[first]
            if not self._settled[first]:
                while length <= _MAX_CODE and window < self._smallest[length]:
                    length += 1
                if length > _MAX_CODE:
                    raise ValueError("a HUFF code matches no code length")
                base = self._base_len[length]
            pos += length
            if pos > total_bits:
                break  # the padding of the last byte
            index = (base - window) >> (32 - length)
            if not 0 <= index < len(self._phrases):
                raise ValueError("a HUFF code points outside the CDIC dictionary")
            phrase = self._phrase(index, depth)
            if len(out) + len(phrase) > limit:
                if depth > 0:
                    raise ValueError("a CDIC phrase is too long")
                out += phrase[:limit - len(out)]
                return bytes(out), True
            out += phrase
        return bytes(out), False

    def _phrase(self, index: int, depth: int) -> bytes:
        ph = self._phrases[index]
        if not ph[1]:
            if ph[2]:
                raise ValueError("a CDIC phrase is defined in terms of itself")
            if depth >= HUFF_DEPTH:
                raise ValueError("CDIC phrases are nested too deep")
            ph[2] = True
            try:
                decoded, _ = self._decode(ph[0], HUFF_PHRASE, depth + 1)
                self._charge(len(decoded) - len(ph[0]))
            finally:
                ph[2] = False
            ph[0] = decoded
            ph[1] = True
        return ph[0]
