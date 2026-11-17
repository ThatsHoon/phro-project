"""Bounded inflate shared by hwp, doc and OfficeArt images."""
import zlib

from .. import limits
from ..limits import ReadError

_CHUNK = 64 * 1024


def _framing(data: bytes, wbits: int) -> bool:
    """The stream's first bytes are another framing than `wbits` reads, so a zlib.error there with nothing returned
    dropped no inflated output: a zlib (15) or gzip (31) reading stops at a header it does not accept, and the raw
    (-15) reading of a zlib header meets a stored block (CMF's low bits are 000) whose length check (LEN at bytes 1-2,
    NLEN at 3-4) fails, so it stops there. When that check passes, the raw reading can go on past the stored block
    into blocks of its own, so it is charged like any other failure."""
    zlib_head = len(data) >= 2 and data[0] & 0x0F == 8 and (data[0] << 8 | data[1]) % 31 == 0
    if wbits == 15:
        return not zlib_head
    if wbits == 31:
        return data[:2] != b"\x1f\x8b"
    return zlib_head and (len(data) < 5 or (data[1] | data[2] << 8) ^ (data[3] | data[4] << 8) != 0xFFFF)


class Budget:
    """Inflated bytes one document may expand to, across every stream that shares this budget."""

    def __init__(self, total: int = limits.ZIP_TOTAL):
        self.total = total
        self.used = 0


def _run(data: bytes, wbits: int, budget: Budget, limit: int | None = None) -> bytes:
    """Inflate within the cap and what is left of `budget`; the caller charges a whole result. A failure charges
    here what it inflated, so streams that fail still spend the document's budget: a too_large the bytes returned
    before it; a zlib.error those and a whole _CHUNK more, the most its last call can have inflated before zlib
    dropped that call's output (none when `_framing` shows the stream was never read as deflate data). So the inflate
    work of a document is at most its total plus one _CHUNK a call, and none once a failure passed it."""
    if budget.used > budget.total:  # only a charged failure gets here
        raise ReadError("too_large", f"{limits.BIG_TOTAL}: inflated bytes of the document")
    cap = min(limits.CFB_STREAM, max(limits.ZIP_RATIO * len(data), limits.ZIP_RATIO_FLOOR))
    if limit is not None:
        cap = min(cap, limit)
    d = zlib.decompressobj(wbits)
    out: list[bytes] = []
    size = 0
    view = memoryview(data)
    pos = 0
    try:
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
    except ReadError:
        budget.used += size
        raise
    except zlib.error:
        budget.used += size if size == 0 and _framing(data, wbits) else size + _CHUNK
        raise
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


def gunzip(data: bytes, budget: Budget, limit: int) -> bytes:
    """A gzip stream inflated within `limit` bytes. Past it is a too_large ReadError ("part: ..."); a cut stream
    raises Truncated and a corrupt one ValueError. The output is charged to `budget` as inflate does."""
    try:
        out, complete = _run(data, 31, budget, limit)
    except zlib.error as e:
        raise ValueError(f"bad gzip stream: {e}") from None
    budget.used += len(out)
    if not complete:
        raise Truncated(out)
    return out
