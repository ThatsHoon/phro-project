"""In-memory builders for iWork fixtures: protobuf messages, Snappy blocks, IWA chunks, archives and zip containers."""
import struct
import zlib


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


def archive(id: int, type: int, payload: bytes, *more: bytes, merge: bool = False, removes: bool = False) -> bytes:
    """A TSP archive: varint length, ArchiveInfo (id, one MessageInfo per payload), then the payloads. `merge` sets
    ArchiveInfo 3 should_merge (and MessageInfo 8 on every message); `removes` gives every later message a
    MessageInfo 10 fields_to_remove."""
    payloads = (payload,) + more
    infos = []
    for i, p in enumerate(payloads):
        pairs = [(1, type), (3, len(p))]
        if merge:
            pairs.append((8, 1))
        if removes and i:
            pairs.append((10, pb((1, 1))))
        infos.append((2, pb(*pairs)))
    info = pb((1, id), *infos, *([(3, 1)] if merge else []))
    return _varint(len(info)) + info + b"".join(payloads)


def iwa_member(*archives) -> bytes:
    """An .iwa member: the archives joined and stored as one Snappy chunk."""
    return chunk(snappy_literal(b"".join(archives)))


def metadata_iwa(datas: list[tuple[int, str]]) -> bytes:
    """An Index/Metadata.iwa member: one TSP.PackageMetadata (11006) whose field 4 lists a TSP.DataInfo
    {1 id, 3 preferred file name, 4 file name} per (id, name)."""
    infos = [(4, pb((1, i), (3, name), (4, name))) for i, name in datas]
    return iwa_member(archive(2, 11006, pb(*infos)))


def iwork_zip(members: dict[str, bytes], methods: dict[str, int] = {}) -> bytes:
    """A zip written by hand: each member stored, or raw-deflated for method 8. Any other method number is written
    as given over the stored bytes, so a method Python cannot inflate (iWork '09's 25452) is still in the directory."""
    out = bytearray()
    central = bytearray()
    for name, data in members.items():
        method = methods.get(name, 0)
        if method == 8:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = c.compress(data) + c.flush()
        else:
            body = data
        crc = zlib.crc32(data)
        raw = name.encode("utf-8")
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method, 0, 0x21, crc, len(body), len(data), len(raw), 0)
        out += raw + body
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, method, 0, 0x21, crc, len(body),
                               len(data), len(raw), 0, 0, 0, 0, 0, offset) + raw
    n = len(members)
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, len(central), len(out), 0)
    return bytes(out + central + eocd)
