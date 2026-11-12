"""Bounded inflate shared by hwp, doc and OfficeArt images."""
import zlib

from .. import limits
from ..limits import ReadError

_CHUNK = 64 * 1024


class Budget:
    """Inflated bytes one document may expand to, across every stream that shares this budget."""

    def __init__(self, total: int = limits.ZIP_TOTAL):
        self.total = total
        self.used = 0


def _run(data: bytes, wbits: int, budget: Budget) -> bytes:
    cap = min(limits.CFB_STREAM, max(limits.ZIP_RATIO * len(data), limits.ZIP_RATIO_FLOOR))
    d = zlib.decompressobj(wbits)
    out: list[bytes] = []
    size = 0
    view = memoryview(data)
    pos = 0
    while pos < len(view) and not d.eof:
        # feed the input in slices: unconsumed_tail is a copy, so a whole-input feed would cost O(n^2)
        buf = bytes(view[pos:pos + _CHUNK])
        pos += _CHUNK
        while True:
            chunk = d.decompress(buf, _CHUNK)
            size += len(chunk)
            if size > cap:
                raise ReadError("too_large", f"{limits.BIG_PART}: inflated stream over {cap} bytes")
            if budget.used + size > budget.total:
                raise ReadError("too_large", f"{limits.BIG_TOTAL}: inflated bytes of the document")
            if chunk:
                out.append(chunk)
            buf = d.unconsumed_tail
            if d.eof or (not buf and len(chunk) < _CHUNK):
                break
    if not out and not d.eof:
        raise ValueError("empty or truncated deflate stream")
    budget.used += size
    return b"".join(out)


def inflate(data: bytes, budget: Budget) -> bytes:
    """Raw deflate first, then the zlib wrapper. A too_large ReadError is final; a corrupt stream raises
    ValueError so ctx.guard counts it."""
    try:
        return _run(data, -15, budget)
    except zlib.error:
        pass
    try:
        return _run(data, 15, budget)
    except zlib.error as e:
        raise ValueError(f"bad deflate stream: {e}")
