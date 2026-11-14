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


def _run(data: bytes, wbits: int, budget: Budget, limit: int | None = None) -> bytes:
    cap = min(limits.CFB_STREAM, max(limits.ZIP_RATIO * len(data), limits.ZIP_RATIO_FLOOR))
    if limit is not None:
        cap = min(cap, limit)
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
    return b"".join(out), d.eof


class Truncated(ValueError):
    """The stream ended before the deflate end-of-stream marker; `data` is what could be inflated."""

    def __init__(self, data: bytes):
        super().__init__(f"truncated deflate stream ({len(data)} bytes inflated)")
        self.data = data


def inflate(data: bytes, budget: Budget, limit: int | None = None) -> bytes:
    """Raw deflate first, then the zlib wrapper. A too_large ReadError is final, and so is output past `limit` bytes
    when it is given (inflation stops there); a corrupt stream raises ValueError and a cut one Truncated (a
    ValueError carrying the partial output), so ctx.guard counts either."""
    best = None
    error = None
    for wbits in (-15, 15):
        try:
            out, complete = _run(data, wbits, budget, limit)
        except zlib.error as e:
            error = e
            continue
        if complete:
            budget.used += len(out)
            return out
        if best is None or len(out) > len(best):
            best = out
    if best is not None:
        budget.used += len(best)
        raise Truncated(best)
    raise ValueError(f"bad deflate stream: {error}")
