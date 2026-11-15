"""iWork containers: raw Snappy, the IWA chunk stream and the protobuf wire reader.

Run: python -I -X utf8 tests/test_reader_iwork.py
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from iwork_fix import chunk, pb, pb_ref, snappy_copy, snappy_literal  # noqa: E402
from reader.containers import flate, pbwire, snappy  # noqa: E402
from reader.limits import ReadError  # noqa: E402


def _raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def _body(data: bytes) -> bytes:
    """The tags of a literal block, without its declared length."""
    return snappy_literal(data)[1:]


def test_varint():
    assert pbwire.varint(b"\x01", 0) == (1, 1)
    assert pbwire.varint(b"\xac\x02", 0) == (300, 2)
    enc = pb((1, 2**63))[1:]
    assert pbwire.varint(enc, 0) == (2**63, 10)
    assert pbwire.varint(b"\xff" * 9 + b"\x01", 0) == (2**64 - 1, 10)
    _raises(ValueError, pbwire.varint, b"\x80" * 10 + b"\x01", 0)
    _raises(ValueError, pbwire.varint, b"\x80\x80", 0)
    _raises(ValueError, pbwire.varint, b"", 0)


def test_fields_wire_types():
    raw5 = bytes([5 << 3 | 5]) + b"\x00\x00\x80\x3f"
    f = pbwire.fields(pb((1, 7), (2, b"abc"), (3, 1.5), (1, 9), (4, "hi")) + raw5)
    assert f[1] == [7, 9]
    assert isinstance(f[2][0], memoryview) and bytes(f[2][0]) == b"abc"
    assert f[3] == [struct.pack("<d", 1.5)] and isinstance(f[3][0], bytes)
    assert bytes(f[4][0]) == b"hi"
    assert f[5] == [b"\x00\x00\x80\x3f"]
    assert pbwire.fields(b"") == {}


def test_fields_rejects():
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 3]))
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 4]))
    _raises(ValueError, pbwire.fields, bytes([0, 1]))
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 2, 5]) + b"ab")
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 1]) + b"abc")
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 5]) + b"ab")


def test_packed_f32_f64_ref_one():
    assert pbwire.packed(b"\x01\xac\x02\x05") == [1, 300, 5]
    assert pbwire.packed(memoryview(b"\x01\x02")) == [1, 2]
    _raises(ValueError, pbwire.packed, b"\x80")
    assert pbwire.f32(struct.pack("<f", 2.5)) == 2.5
    assert pbwire.f64(struct.pack("<d", -3.25)) == -3.25
    assert pbwire.ref(pb_ref(42)) == 42
    assert pbwire.ref(pb((2, 1))) is None
    assert pbwire.ref(pb((1, b"x"))) is None
    f = pbwire.fields(pb((1, 5), (1, 6)))
    assert pbwire.one(f, 1) == 5
    assert pbwire.one(f, 2) is None
    assert pbwire.one(f, 2, 9) == 9


def test_snappy_copies():
    out = bytearray(b"abcdefgh")
    blk = _body(bytes(out))
    blk += snappy_copy(8, 4, 1)                 # copy-1
    out += out[-8:-4]
    blk += snappy_copy(2, 3, 2)                 # copy-2, overlapping
    for _ in range(3):
        out.append(out[-2])
    blk += snappy_copy(11, 2, 4)                # copy-4
    s = len(out) - 11
    out += out[s:s + 2]
    assert snappy.raw(bytes([len(out)]) + blk, 1000) == bytes(out)
    assert snappy.raw(bytes([8]) + _body(b"ab") + snappy_copy(2, 6), 100) == b"abababab"
    assert snappy.raw(bytes([10]) + _body(b"x") + snappy_copy(1, 9), 100) == b"x" * 10
    data = bytes(range(256)) * 2
    assert snappy.raw(snappy_literal(data), 1000) == data
    assert snappy.raw(b"\x00", 10) == b""


def test_snappy_bad():
    lit = _body(b"abcd")
    _raises(ValueError, snappy.raw, bytes([5]) + lit, 100)                      # declared 5, got 4
    _raises(ValueError, snappy.raw, bytes([3]) + lit, 100)                      # declared 3, got 4
    _raises(ValueError, snappy.raw, bytes([8]) + lit + snappy_copy(9, 4), 100)  # copy before the start
    _raises(ValueError, snappy.raw, bytes([8]) + lit + snappy_copy(0, 4), 100)  # offset 0
    _raises(ValueError, snappy.raw, bytes([8]) + lit + b"\x02\x01", 100)        # truncated copy offset
    _raises(ValueError, snappy.raw, bytes([8, 0x0C]) + b"ab", 100)              # truncated literal
    _raises(ValueError, snappy.raw, b"", 100)
    blk = bytes([67]) + b"\x00a"                                                # 67 > 22 x 3
    _raises(ValueError, snappy.raw, blk, 10**6)
    _raises(ValueError, snappy.raw, snappy_literal(b"x" * 50), 49)
    assert snappy.raw(snappy_literal(b"x" * 50), 50) == b"x" * 50


def test_snappy_declared_before_output():
    huge = b"\xff\xff\xff\x7f" + b"\x00" * 3
    _raises(ValueError, snappy.raw, huge, 10**9)
    _raises(ValueError, snappy.raw, huge, 100)


def test_iwa_chunks():
    out = snappy.iwa(chunk(snappy_literal(b"hello "), 0) + chunk(b"world", 1), flate.Budget(1000))
    assert out == b"hello world"
    assert snappy.iwa(b"", flate.Budget(10)) == b""
    _raises(ValueError, snappy.iwa, chunk(b"zz", 2), flate.Budget(1000))
    _raises(ValueError, snappy.iwa, b"\x00\x05\x00", flate.Budget(1000))
    _raises(ValueError, snappy.iwa, b"\x01\x09\x00\x00ab", flate.Budget(1000))


def test_iwa_lzvn():
    e = _raises(snappy.Lzvn, snappy.iwa, b"bvxn" + b"\x00" * 8, flate.Budget(1000))
    assert isinstance(e, ValueError)


def test_iwa_budget():
    data = chunk(snappy_literal(b"y" * 200))
    e = _raises(ReadError, snappy.iwa, data, flate.Budget(100))
    assert e.kind == "too_large" and "total" in str(e)
    b = flate.Budget(300)
    assert len(snappy.iwa(data, b)) == 200 and b.used == 200
    _raises(ReadError, snappy.iwa, data, b)


def test_snappy_block_cap():
    old = snappy.SNAPPY_BLOCK
    assert old == 16 * 2**20
    try:
        snappy.SNAPPY_BLOCK = 10
        _raises(ValueError, snappy.iwa, chunk(snappy_literal(b"z" * 20)), flate.Budget(10**6))
        assert snappy.iwa(chunk(snappy_literal(b"z" * 10)), flate.Budget(10**6)) == b"z" * 10
    finally:
        snappy.SNAPPY_BLOCK = old


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
