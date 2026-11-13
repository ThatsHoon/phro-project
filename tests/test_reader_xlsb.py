"""xlsb records, the workbook sheet list, styles and shared strings, and the container checks.

Run: python -I -X utf8 tests/test_reader_xlsb.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.containers import biff12 as b12  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import xlsb  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import biff12, make_cfb, make_xlsb, make_zip, xlsb_bundle, xlsb_wide  # noqa: E402


def read(sheets, **kw):
    """Build an .xlsb, read its workbook, return (_Wb, ctx)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, sheets, **kw)
        ctx = Context()
        with Package(path) as pkg:
            return xlsb.read_workbook(pkg, ctx), ctx


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def test_varint_framing():
    assert biff12(156, bytes(300))[:4] == bytes.fromhex("9c01ac02")
    data = biff12(5, b"ab") + biff12(156, bytes(300)) + biff12(7)
    ctx = Context()
    got = [(rid, size) for rid, off, size in b12.records(data, ctx)]
    assert got == [(5, 2), (156, 300), (7, 0)] and not ctx.skipped, (got, ctx.skipped)
    rid, off, size = list(b12.records(data, Context()))[0]
    assert data[off:off + size] == b"ab"


def test_fifth_size_byte():
    ctx = Context()
    data = biff12(5, b"x") + bytes([0x05, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]) + biff12(6)
    got = list(b12.records(data, ctx))
    assert [g[0] for g in got] == [5] and ctx.skipped["record"] == 1, (got, ctx.skipped)
    ctx = Context()
    assert len(list(b12.records(biff12(5, b"abc")[:-1], ctx))) == 0 and ctx.skipped["record"] == 1
    ctx = Context()
    assert len(list(b12.records(bytes([0x05, 0x80]), ctx))) == 0 and ctx.skipped["record"] == 1


def test_fields_wide():
    body = xlsb_wide("hi") + xlsb_wide(None) + struct.pack("<I", 2) + b"\x00\xd8x\x00"  # lone high surrogate, then x
    f = b12.Fields(body, 0, len(body))
    assert f.wide() == "hi" and f.wide() is None and f.wide() == "�x"
    bad = struct.pack("<I", 5) + b"ab"
    try:
        b12.Fields(bad, 0, len(bad)).wide()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_bad_sst_item_skipped():
    def good(s):
        return biff12(19, b"\0" + xlsb_wide(s))
    bad = biff12(19, b"\0" + struct.pack("<I", 500) + "no".encode("utf-16-le"))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_xlsb(path, [{"name": "S"}], extra_parts={"xl/sharedStrings.bin": good("first") + bad + good("third")})
        ctx = Context()
        with Package(path) as pkg:
            wb = xlsb.read_workbook(pkg, ctx)
    assert wb.sst == ["first", "third"] and ctx.skipped["record"] == 1, (wb.sst, ctx.skipped)


def test_bundlesh_extra_dword():
    rec = xlsb_bundle("Data", "rId1", extra=bytes(4))
    wb, ctx = read([{"name": "x"}], workbook_records=[rec])
    assert wb.sheets == [("Data", "xl/worksheets/sheet1.bin", 0)] and not ctx.skipped, (wb.sheets, ctx.skipped)
    plain, ctx = read([{"name": "Plain"}])
    assert plain.sheets == [("Plain", "xl/worksheets/sheet1.bin", 0)]


def test_bundlesh_fallback():
    rec = biff12(156, struct.pack("<II", 0, 1) + struct.pack("<I", 900))
    wb, ctx = read([{"name": "x"}], workbook_records=[rec])
    assert wb.sheets == [("Sheet 1", "xl/worksheets/sheet1.bin", 0)], wb.sheets
    assert ctx.skipped["record"] == 1, ctx.skipped


def test_cell_xfs_only():
    def xf(fid):
        return biff12(47, struct.pack("<HH", 0xFFFF, fid) + bytes(8))
    styles = b"".join([
        biff12(615, struct.pack("<I", 1)), biff12(44, struct.pack("<H", 164) + xlsb_wide("0.0%")), biff12(616),
        biff12(626, struct.pack("<I", 2)), xf(9), xf(10), biff12(627),
        biff12(617, struct.pack("<I", 2)), xf(0), xf(164), biff12(618),
        xf(14),  # outside the block: not counted
    ])
    wb, ctx = read([{"name": "S"}], extra_parts={"xl/styles.bin": styles})
    assert wb.xf_fmt == [0, 164] and wb.formats == {164: "0.0%"}, (wb.xf_fmt, wb.formats)
    wb, ctx = read([{"name": "S"}], date1904=True)
    assert wb.date1904 is True and wb.xf_fmt == [0]
    wb, ctx = read([{"name": "S"}])
    assert wb.date1904 is False


def test_encrypted_xlsb():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_cfb(path, {"EncryptionInfo": b"\x04\x00\x04\x00" + bytes(40), "EncryptedPackage": bytes(64)})
        kind, _ = denied(lambda: xlsb.parse(path, Context()))
        assert kind == "encrypted", kind
        plain = Path(d) / "b.xlsb"
        make_cfb(plain, {"Other": bytes(16)})
        assert denied(lambda: xlsb.parse(plain, Context()))[0] == "damaged"
        junk = Path(d) / "c.xlsb"
        junk.write_bytes(b"not a zip at all")
        assert denied(lambda: xlsb.parse(junk, Context()))[0] == "damaged"


def test_xlsx_saved_as_xlsb():
    rel = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
           'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
           'Target="xl/workbook.xml"/></Relationships>')
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.xlsb"
        make_zip(path, {"_rels/.rels": rel, "xl/workbook.xml": "<workbook/>"}, stored=())
        got = denied(lambda: xlsb.parse(path, Context()))
    assert got == ("unsupported", "xlsx workbook saved as .xlsb"), got


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
