"""In-memory builders for iWork fixtures: protobuf messages, Snappy blocks, IWA chunks and archives."""
import struct


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb(*pairs) -> bytes:
    """A protobuf message from (field, value) pairs: int is wire 0, bytes or str wire 2, float wire 1."""
    out = bytearray()
    for field, value in pairs:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            out += _varint(field << 3) + _varint(value)
        elif isinstance(value, float):
            out += _varint(field << 3 | 1) + struct.pack("<d", value)
        else:
            if isinstance(value, str):
                value = value.encode("utf-8")
            out += _varint(field << 3 | 2) + _varint(len(value)) + bytes(value)
    return bytes(out)


def pb_ref(id: int) -> bytes:
    """A TSP.Reference: field 1 is the target id."""
    return pb((1, id))


def snappy_literal(data: bytes) -> bytes:
    """A raw Snappy block holding data as one literal (declared length first)."""
    n = len(data)
    head = _varint(n)
    if n == 0:
        return head
    ln = n - 1
    if ln < 60:
        tag = bytes([ln << 2])
    else:
        raw = ln.to_bytes((ln.bit_length() + 7) // 8, "little")
        tag = bytes([(59 + len(raw)) << 2]) + raw
    return head + tag + data


def snappy_copy(offset: int, length: int, width: int = 2) -> bytes:
    """A hand-made copy tag: width 1 (copy-1, length 4-11, offset under 2048), 2 or 4."""
    if width == 1:
        return bytes([1 | ((length - 4) << 2) | ((offset >> 8) << 5), offset & 0xFF])
    if width == 2:
        return bytes([2 | ((length - 1) << 2)]) + offset.to_bytes(2, "little")
    return bytes([3 | ((length - 1) << 2)]) + offset.to_bytes(4, "little")


def chunk(block: bytes, type: int = 0) -> bytes:
    """One IWA chunk: type byte, three length bytes, then the block."""
    return bytes([type]) + len(block).to_bytes(3, "little") + block


def archive(id: int, type: int, payload: bytes, *more: bytes, merge: bool = False) -> bytes:
    """A TSP archive: varint length, ArchiveInfo (id, one MessageInfo per payload), then the payloads."""
    payloads = (payload,) + more
    infos = []
    for p in payloads:
        pairs = [(1, type), (3, len(p))]
        if merge:
            pairs.append((8, 1))
        infos.append((2, pb(*pairs)))
    info = pb((1, id), *infos)
    return _varint(len(info)) + info + b"".join(payloads)


def iwa_member(*archives) -> bytes:
    """An .iwa member: the archives joined and stored as one Snappy chunk."""
    return chunk(snappy_literal(b"".join(archives)))
