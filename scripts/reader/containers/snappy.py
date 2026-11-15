"""Raw Snappy blocks and the IWA chunk stream of Pages, Numbers and Keynote.

Ported from docling (MIT) `docling/backend/iwork/iwa.py` (`decompress_snappy_block`, `decompress`; v2.150.0,
1ae470a1f4580881ed73853f64e0bf178f623c5e). An .iwa member is a run of chunks: a type byte (0 Snappy, 1 stored),
three length bytes (little-endian) and the block. The Snappy is raw: no stream identifier and no checksum.
A declared length is checked against `SNAPPY_BLOCK`, against 22 times the block and against the shared budget before
any output is built, and output past the declaration fails.
"""
from .. import limits
from ..limits import ReadError
from .flate import Budget

SNAPPY_BLOCK = 16 * 2**20

# the spec's Limits "Snappy declares at most 21 times its input": one 3-byte copy tag emits at most 64 bytes, so a
# valid block reaches 64/3 (about 21.3) times its size and never 22; 22 is the first whole number none reaches
_RATIO = 22
_HEADER = 4


class Lzvn(ValueError):
    """The member starts with `bvx`: Apple's LZVN framing, which is not read."""


def _declared(block) -> tuple[int, int]:
    n = 0
    shift = 0
    pos = 0
    while True:
        if pos >= len(block) or pos >= 10:
            raise ValueError("bad snappy length")
        b = block[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7


def raw(block: bytes, limit: int) -> bytes:
    """One raw Snappy block, at most `limit` bytes. ValueError for a declared length over `limit` or over 22 times
    the block (raised before any output), output that differs from the declaration, or a copy before the start."""
    expected, pos = _declared(block)
    size = len(block)
    if expected > limit or expected > _RATIO * size:
        raise ValueError(f"snappy block declares {expected} bytes")
    out = bytearray()
    while pos < size:
        tag = block[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                if pos + extra > size:
                    raise ValueError("truncated snappy literal length")
                n = int.from_bytes(block[pos:pos + extra], "little")
                pos += extra
            n += 1
            if pos + n > size:
                raise ValueError("truncated snappy literal")
            if len(out) + n > expected:
                raise ValueError("snappy output past its declared length")
            out += block[pos:pos + n]
            pos += n
            continue
        if kind == 1:
            n = 4 + ((tag >> 2) & 7)
            if pos >= size:
                raise ValueError("truncated snappy copy")
            offset = ((tag >> 5) << 8) | block[pos]
            pos += 1
        else:
            width = 2 if kind == 2 else 4
            n = (tag >> 2) + 1
            if pos + width > size:
                raise ValueError("truncated snappy copy")
            offset = int.from_bytes(block[pos:pos + width], "little")
            pos += width
        if offset == 0 or offset > len(out):
            raise ValueError("snappy copy before the start")
        if len(out) + n > expected:
            raise ValueError("snappy output past its declared length")
        start = len(out) - offset
        if offset >= n:
            out += out[start:start + n]
        else:
            # overlapping: the pattern out[start:] repeats forward
            tail = bytes(out[start:])
            out += (tail * (n // offset + 1))[:n]
    if len(out) != expected:
        raise ValueError(f"snappy block decoded to {len(out)} bytes, declared {expected}")
    return bytes(out)


def iwa(data: bytes, budget: Budget) -> bytes:
    """The chunk stream of one .iwa member, charged to `budget`. Type 0 is Snappy, type 1 stored; another type is
    ValueError and a `bvx` head is Lzvn. Output past the budget is ReadError("too_large", "total: ...")."""
    if data[:3] == b"bvx":
        raise Lzvn("LZVN-framed member")
    out = bytearray()
    pos = 0
    end = len(data)
    while pos < end:
        if pos + _HEADER > end:
            raise ValueError("truncated IWA chunk header")
        kind = data[pos]
        n = int.from_bytes(data[pos + 1:pos + _HEADER], "little")
        pos += _HEADER
        if n > end - pos:
            raise ValueError("truncated IWA chunk")
        block = data[pos:pos + n]
        pos += n
        if kind == 0:
            declared, _ = _declared(block)
            if budget.used + declared > budget.total:
                raise ReadError("too_large", f"{limits.BIG_TOTAL}: unpacked bytes of the document")
            part = raw(block, SNAPPY_BLOCK)
        elif kind == 1:
            if budget.used + n > budget.total:
                raise ReadError("too_large", f"{limits.BIG_TOTAL}: unpacked bytes of the document")
            part = block
        else:
            raise ValueError(f"IWA chunk type {kind}")
        budget.used += len(part)
        out += part
    return bytes(out)
