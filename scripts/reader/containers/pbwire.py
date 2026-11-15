"""Protobuf wire reader for the iWork object store.

Ported from docling (MIT) `docling/backend/iwork/iwa.py` (`read_varint`, `read_fields`, `read_reference`;
v2.150.0, 1ae470a1f4580881ed73853f64e0bf178f623c5e). Apple's schemas are not used: callers read the fields they
need by number. Values are lazy: a length-delimited field is a memoryview of the buffer, and a caller parses a nested
message only when it walks into it, so the caller owns the depth. Every length is checked against the buffer.
"""
import struct

_MAX_VARINT = 10


def varint(buf, pos: int) -> tuple[int, int]:
    """A base-128 varint of at most 10 bytes, as (value, next position)."""
    result = 0
    shift = 0
    end = len(buf)
    for _ in range(_MAX_VARINT):
        if pos >= end:
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result & 0xFFFFFFFFFFFFFFFF, pos
        shift += 7
    raise ValueError("overlong varint")


def fields(buf) -> dict[int, list]:
    """A message as {field: [values]}: wire 0 an int, wire 1 and 5 raw bytes, wire 2 a memoryview. Wire 3 and 4,
    field 0 and a length past the buffer raise ValueError."""
    view = memoryview(buf)
    out: dict[int, list] = {}
    pos = 0
    end = len(view)
    while pos < end:
        key, pos = varint(view, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("protobuf field 0")
        if wire == 0:
            value, pos = varint(view, pos)
        elif wire == 2:
            n, pos = varint(view, pos)
            if n > end - pos:
                raise ValueError("protobuf length past the buffer")
            value = view[pos:pos + n]
            pos += n
        elif wire == 1 or wire == 5:
            n = 8 if wire == 1 else 4
            if n > end - pos:
                raise ValueError("protobuf fixed value past the buffer")
            value = bytes(view[pos:pos + n])
            pos += n
        else:
            raise ValueError(f"protobuf wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def packed(b) -> list[int]:
    """A packed repeated varint field."""
    view = memoryview(b)
    out = []
    pos = 0
    while pos < len(view):
        v, pos = varint(view, pos)
        out.append(v)
    return out


def f32(b) -> float:
    if len(b) != 4:
        raise ValueError("protobuf float needs 4 bytes")
    return struct.unpack("<f", bytes(b))[0]


def f64(b) -> float:
    if len(b) != 8:
        raise ValueError("protobuf double needs 8 bytes")
    return struct.unpack("<d", bytes(b))[0]


def ref(b) -> int | None:
    """The target id of a TSP.Reference (its field 1), or None."""
    v = fields(b).get(1)
    return v[0] if v and isinstance(v[0], int) else None


def one(f: dict, n: int, default=None):
    """The first value of field n, or default."""
    v = f.get(n)
    return v[0] if v else default
