"""BIFF8 records, CONTINUE-aware strings, the SST, RK numbers and the workbook globals.

Run: python -I -X utf8 tests/test_reader_biff.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.formats import biff  # noqa: E402
from reader_fix import biff as rec, biff_workbook, make_xls  # noqa: E402

CONT = 0x003C


def u16(n):
    return struct.pack("<H", n)


def u32(n):
    return struct.pack("<I", n)


def test_records_and_continue():
    data = rec(0x00FC, b"AAAA") + rec(CONT, b"BB") + rec(CONT, b"CCC") + rec(0x000A, b"")
    st = biff.Stream(data, Context())
    recs = list(st.records(0))
    assert [r[0] for r in recs] == [0x00FC, 0x000A]
    rid, off, size, conts = recs[0]
    assert (off, size) == (4, 4) and data[off:off + size] == b"AAAA"
    assert [data[o:o + n] for o, n in conts] == [b"BB", b"CCC"]
    assert recs[1][3] == ()
    assert list(st.records(len(data) - 4, None))[0][0] == 0x000A


def test_truncated_record():
    ctx = Context()
    data = rec(0x0001, b"xy") + struct.pack("<HH", 0x0002, 100) + b"x" * 10
    assert [r[0] for r in biff.Stream(data, ctx).records(0)] == [0x0001]
    assert ctx.skipped["record"] == 1
    ctx = Context()  # a header cut short
    assert len(list(biff.Stream(rec(1, b"") + b"\x02\x00", ctx).records(0))) == 1
    assert ctx.skipped["record"] == 1
    ctx = Context()  # end bound inside a body
    data = rec(1, b"abcd") + rec(2, b"efgh")
    assert len(list(biff.Stream(data, ctx).records(0, 10))) == 1
    assert ctx.skipped["record"] == 1


def test_record_cap():
    old = limits.BIN_RECORDS
    limits.BIN_RECORDS = 10
    try:
        ctx = Context()
        st = biff.Stream(b"".join(rec(1, b"") for _ in range(20)), ctx)
        assert len(list(st.records(0))) == 10
        assert ctx.skipped["record"] == 1
        assert list(st.records(0)) == []  # the budget is shared by every walk of the stream
    finally:
        limits.BIN_RECORDS = old


def test_sst_width_switch():
    first = u32(2) + u32(1) + u16(5) + b"\x00" + b"ab"  # 5 chars, 8-bit, 2 present
    cont = b"\x01" + "c한글".encode("utf-16-le")  # the rest is 16-bit
    data = rec(0x00FC, first) + rec(CONT, cont)
    (r,) = list(biff.Stream(data, Context()).records(0))
    cur = biff.Cursor(data, [(r[1], r[2])] + list(r[3]))
    assert biff.sst(cur) == ["abc한글"]
    first = u32(1) + u32(1) + u16(4) + b"\x01" + "한".encode("utf-16-le")  # 16-bit to 8-bit
    cont = b"\x00" + b"xyz"
    data = rec(0x00FC, first) + rec(CONT, cont)
    (r,) = list(biff.Stream(data, Context()).records(0))
    assert biff.sst(biff.Cursor(data, [(r[1], r[2])] + list(r[3]))) == ["한xyz"]


def test_sst_rich_phonetic():
    one = u16(2) + b"\x0c" + u16(2) + u32(5) + b"hi" + b"\1\2\3\4" * 2 + b"PPPPP"
    two = u16(3) + b"\x00" + b"end"
    body = u32(2) + u32(2) + one + two
    assert biff.sst(biff.Cursor(body, [(0, len(body))])) == ["hi", "end"]
    # runs sitting in the CONTINUE after the characters
    data = rec(0x00FC, u32(1) + u32(1) + u16(2) + b"\x08" + u16(1) + b"hi") + rec(CONT, b"\1\2\3\4")
    (r,) = list(biff.Stream(data, Context()).records(0))
    cur = biff.Cursor(data, [(r[1], r[2])] + list(r[3]))
    assert biff.sst(cur) == ["hi"]
    assert cur.at_end()


def test_sst_count_lies():
    body = u32(9) + u32(0xFFFFFFFF) + u16(1) + b"\x00a" + u16(1) + b"\x00b"
    t = time.perf_counter()
    assert biff.sst(biff.Cursor(body, [(0, len(body))])) == ["a", "b"]
    assert time.perf_counter() - t < 0.1
    body = u32(1) + u32(5) + u16(40) + b"\x00ab"  # a string longer than the data
    assert biff.sst(biff.Cursor(body, [(0, len(body))])) == []


def test_cursor_errors_and_short_string():
    cur = biff.Cursor(b"\x01\x02\x03", [(0, 3)])
    assert cur.u16() == 0x0201
    try:
        cur.u16()
        assert False
    except ValueError:
        pass
    cur = biff.Cursor(b"ab" + b"\x01\x00\x00\x00" + struct.pack("<d", 1.5), [(0, 2), (2, 4), (6, 8)])
    cur.skip(2)
    assert cur.u32() == 1 and cur.f64() == 1.5
    assert biff.short_string(b"\x00\x03\x00abcZ", 1) == ("abc", 6)
    assert biff.short_string(b"\x02\x01\x41\x00\x42\x00", 0) == ("AB", 6)
    try:
        biff.short_string(b"\x09\x00ab", 0)
        assert False
    except ValueError:
        pass


def test_rk():
    assert biff.rk(0x3FF00000) == 1.0
    assert biff.rk((100 << 2) | 2) == 100
    assert biff.rk((123 << 2) | 3) == 1.23
    assert biff.rk(((-5 << 2) & 0xFFFFFFFF) | 2) == -5
    assert biff.rk(0x3FF00001) == 0.01


def sheets():
    return [{"name": "Data", "cells": {(0, 0): "x", (0, 1): ("fmt", 1234.5, '"₩"#,##0.00')}},
            {"name": "Chart", "kind": 2, "state": 1}]


def test_globals():
    data = biff_workbook(sheets(), date1904=True, active=1, formats={164: '"₩"#,##0.00'})
    ctx = Context()
    g = biff.read_globals(biff.Stream(data, ctx))
    assert [(s.name, s.state, s.kind) for s in g.sheets] == [("Data", 0, 0), ("Chart", 1, 2)]
    assert data[g.sheets[0].offset:g.sheets[0].offset + 2] == b"\x09\x08"
    assert g.date1904 and g.active == 1 and g.filepass is None
    assert g.sst == ["x"] and g.formats[164] == '"₩"#,##0.00'
    assert len(g.xf_fmt) == 17 and biff.fmt_code(g, 16) == '"₩"#,##0.00'
    assert biff.fmt_code(g, 15) == "General" and biff.fmt_code(g, 99) == "General"
    assert not ctx.skipped
    # the formatted cell really points at that XF
    st = biff.Stream(data, ctx)
    nums = [r for r in st.records(g.sheets[0].offset) if r[0] == 0x0203]
    assert struct.unpack_from("<H", data, nums[0][1] + 4)[0] == 16


def test_globals_extras():
    words = {(0, 0): "abcdefghij", (0, 1): "한글한글한", (0, 2): "kl"}
    data = biff_workbook([{"name": "S", "cells": words}], sst_split=12)
    assert data.count(b"\x3c\x00") >= 3  # really split into CONTINUEs
    assert biff.read_globals(biff.Stream(data, Context())).sst == ["abcdefghij", "한글한글한", "kl"]
    blob = bytes(range(40))
    data = biff_workbook(sheets(), globals_extra=rec(0x00EB, blob) + rec(CONT, blob))
    assert biff.read_globals(biff.Stream(data, Context())).drawing == blob + blob
    data = biff_workbook(sheets(), filepass=b"\x01\x00" + bytes(50))
    g = biff.read_globals(biff.Stream(data, Context()))
    assert g.filepass is not None and g.filepass[0] == 0x002F and g.sheets == []


def test_workbook_in_cfb():
    p = Path(tempfile.mkdtemp()) / "a.xls"
    data = make_xls(p, sheets())
    assert Compound(p.read_bytes()).read("Workbook") == data


def test_biff5_unsupported():
    bof = struct.pack("<HHHH", 0x0500, 0x0005, 0, 0)
    try:
        biff.read_globals(biff.Stream(rec(0x0809, bof) + rec(0x000A, b""), Context()))
        assert False
    except ReadError as e:
        assert e.kind == "unsupported" and "5.0/95" in e.detail


def test_not_biff():
    for data in (b"\x00" * 64, b"", rec(0x0809, struct.pack("<HH", 0x0600, 0x0010) + bytes(12)), rec(0x000A, b"")):
        try:
            biff.read_globals(biff.Stream(data, Context()))
            assert False
        except ReadError as e:
            assert e.kind == "damaged", e


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
