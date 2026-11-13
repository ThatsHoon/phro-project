"""BIFF12 (xlsb) records: the framing walk and bounds-checked field reads inside one record.

The framing follows anydoc `sheet/xlsb.rs` (a 1 or 2 byte id and a size of up to four 7-bit groups) and xberg
`table_budget/xlsb_framing.rs` (a size byte count past four is a framing error). A record body stays an offset into
the one part `bytes`. There is no sync marker, so a header or body past the end ends the part.
"""
import struct
from typing import Iterator

from .. import limits


def records(data: bytes, ctx) -> Iterator[tuple[int, int, int]]:
    """Yield (id, body offset, size) for each record of one part; a bad frame or the record cap ends the walk."""
    n = len(data)
    pos = 0
    count = 0
    while pos < n:
        if count >= limits.BIN_RECORDS:
            ctx.skipped["record"] += 1
            return
        b0 = data[pos]
        pos += 1
        rid = b0 & 0x7F
        if b0 & 0x80:
            if pos >= n:
                ctx.skipped["record"] += 1
                return
            rid |= (data[pos] & 0x7F) << 7
            pos += 1
        size = 0
        for i in range(4):
            if pos >= n:
                ctx.skipped["record"] += 1
                return
            b = data[pos]
            pos += 1
            size |= (b & 0x7F) << (7 * i)
            if not b & 0x80:
                break
            if i == 3:  # a fifth size byte: there is no way to find the next record
                ctx.skipped["record"] += 1
                return
        if pos + size > n:
            ctx.skipped["record"] += 1
            return
        count += 1
        yield rid, pos, size
        pos += size


class Fields:
    """Reads within one record body; any read past its end raises ValueError."""

    def __init__(self, data: bytes, body: int, size: int):
        self.data = data
        self.pos = body
        self.end = body + size

    def _take(self, n: int) -> int:
        if n < 0 or self.pos + n > self.end:
            raise ValueError("past the end of the record")
        at = self.pos
        self.pos += n
        return at

    def u8(self) -> int:
        return self.data[self._take(1)]

    def u16(self) -> int:
        return struct.unpack_from("<H", self.data, self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.data, self._take(4))[0]

    def f64(self) -> float:
        return struct.unpack_from("<d", self.data, self._take(8))[0]

    def skip(self, n: int) -> None:
        self._take(n)

    def left(self) -> int:
        return self.end - self.pos

    def wide(self) -> str | None:
        """XLWideString / XLNullableWideString: a u32 cch, then UTF-16LE; 0xFFFFFFFF is None."""
        cch = self.u32()
        if cch == 0xFFFFFFFF:
            return None
        at = self._take(cch * 2)
        return self.data[at:at + cch * 2].decode("utf-16-le", errors="replace")
