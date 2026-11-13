"""Files named .xls that are not BIFF: HTML tables, delimited text, xlsx zips and other bytes.

Run: python -I -X utf8 tests/test_reader_xls_alike.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits  # noqa: E402
from reader.formats import xls_alike  # noqa: E402
from reader.limits import ReadError  # noqa: E402


def run(data):
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        src.write_bytes(data)
        reader.convert(src, Path(d) / "out")
        return (Path(d) / "out" / "out.md").read_text(encoding="utf-8")


def denied(data):
    try:
        run(data)
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def test_euckr_html_named_xls():
    html = '<html><head><meta charset="euc-kr"><title>표</title></head><body><table><tr><td>이름</td><td>1</td></tr></table>'
    md = run(html.encode("cp949"))
    assert "## Sheet: 표" in md, md
    assert "|이름|1|" in md, md


def test_cp949_csv_named_xls():
    md = run("이름,값\n가,1\n".encode("cp949"))
    assert "## Sheet: Sheet1" in md, md
    assert "|이름|값|" in md and "|가|1|" in md, md


def test_tsv_named_xls():
    md = run("a\tb\n1\t2\n".encode("cp1252"))
    assert "|a|b|" in md and "|1|2|" in md, md


def test_zip_named_xls():
    assert denied(b"PK\x03\x04" + b"\0" * 100) == ("unsupported", "xlsx workbook saved as .xls")


def test_cut_inside_multibyte_char():
    rows = "".join("a,가" + chr(10) for _ in range(40))
    for cap in range(40, 46):  # the read is cap*4+4 bytes, so one of these cuts mid-character
        old, limits.SHEET_CHARS = limits.SHEET_CHARS, cap
        try:
            md = run(rows.encode("utf-8"))
        finally:
            limits.SHEET_CHARS = old
        assert "|a|가|" in md, (cap, md)
    for cap in range(40, 46):
        old, limits.SHEET_CHARS = limits.SHEET_CHARS, cap
        try:
            md = run(rows.encode("cp949"))
        finally:
            limits.SHEET_CHARS = old
        assert "|a|가|" in md, (cap, md)


def test_long_field_keeps_other_rows():
    md = run(("a,b" + chr(10) + "x" * 200_000 + ",c" + chr(10) + "last,row" + chr(10)).encode("utf-8"))
    assert "|last|row|" in md and "|a|b|" in md, md[:200]
    assert limits.SHEET_CHARS > 200_000
    import csv
    assert csv.field_size_limit() == 131072


def test_other_damaged():
    assert denied(b"\x01\x02" * 100)[0] == "damaged"
    assert xls_alike.kind(b"\x01\x02" * 100) == "other"
    assert xls_alike.kind(b"\xef\xbb\xbf \n<TABLE>") == "html"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
