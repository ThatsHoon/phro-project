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
from reader.model import Context  # noqa: E402


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


def test_utf16_text_named_xls():
    """Excel's "Unicode Text" export: a UTF-16LE BOM, then tab-separated rows; a UTF-16BE copy reads the same."""
    rows = "﻿이름\t금액\r\n가\t1\r\n"
    for enc in ("utf-16-le", "utf-16-be"):
        md = run(rows.encode(enc))
        assert "## Sheet: Sheet1" in md, (enc, md)
        assert "|이름|금액|" in md and "|가|1|" in md, (enc, md)
    assert xls_alike.kind(rows.encode("utf-16-le")) == "text"


def test_utf16_html_named_xls():
    page = "<html><head><title>표</title></head><body><table><tr><td>이름</td><td>1</td></tr></table></body></html>"
    for data in (("﻿" + page).encode("utf-16-le"), page.encode("utf-16-le")):  # with a BOM, and every other byte NUL
        assert xls_alike.kind(data) == "html", data[:16]
        md = run(data)
        assert "## Sheet: 표" in md and "|이름|1|" in md, md


def test_one_column_csv_named_xls():
    """A head with no NUL that decodes as text and holds a line break is text, though line 1 has no delimiter."""
    md = run("이름\n가\n나\n".encode("cp949"))
    assert "## Sheet: Sheet1" in md, md
    assert "이름\n가\n나" in md, md  # a one-column table is written as its lines
    assert xls_alike.kind(b"\x01\x02" * 100 + b"\n") == "other"  # control bytes are not text


def test_rtf_named_xls():
    """An RTF head goes to the RTF reader, as doc.py does, never to the text sheet as raw markup."""
    for data in (b"{\\rtf1\\ansi hello\\par\n}\n", b"\xef\xbb\xbf{\\rtf1\\ansi hello\\par\n}\n",
                 b" \r\n{\\rtf1\\ansi hello\\par\n}\n", b"{\\rtf1\\ansi a,hello\\par\n}\n"):
        md = run(data)
        assert "hello" in md and "\\rtf" not in md and "## Sheet" not in md, (data[:12], md)


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


def parse_text(data):
    """(the Doc, the Context) of parse_text over a file holding `data`."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        src.write_bytes(data)
        ctx = Context()
        return xls_alike.parse_text(src, ctx), ctx


def test_cr_only_lines():
    # a CR-only file has lines: line 1 picks the delimiter, and kind() looks at line 1 only
    md = run("a,b\rc\td\r".encode("utf-8"))
    assert "|a|b|" in md and "|c\td|" in md, md
    assert xls_alike.kind(b"head\rbin\x01a,b\r") == "other"  # the comma is on line 2, and the head is not text
    assert xls_alike.kind(b"a\tb\rc\r") == "text"
    md = run("a\tb\rc,d\r".encode("utf-8"))
    assert "|a|b|" in md and "|c,d|" in md, md


def test_utf8_and_bom_text_named_xls():
    rows = "이름,값\n가,1\n"
    for data in (rows.encode("utf-8"), b"\xef\xbb\xbf" + rows.encode("utf-8")):
        md = run(data)
        assert "|이름|값|" in md and "|가|1|" in md and "\ufeff" not in md, (data[:4], md)


def test_field_over_the_budget_counts_characters():
    old, limits.SHEET_CHARS = limits.SHEET_CHARS, 200_000
    try:
        doc, ctx = parse_text(b"a,b\n" + b"x" * 250_000 + b",c\nlast,row\n")
    finally:
        limits.SHEET_CHARS = old
    assert ctx.skipped["sheet_cut"] == 1 and not ctx.skipped["record"], dict(ctx.skipped)
    assert any("cut at 200000 characters" in w for w in ctx.warnings), ctx.warnings


def test_file_of_exactly_the_cap_is_not_cut():
    # 404 bytes is the read cap for a 100 character budget; this file ends in two bytes UTF-8 would take for the
    # start of a longer sequence, so only a file that really was cut may drop them
    old, limits.SHEET_CHARS = limits.SHEET_CHARS, 100
    try:
        data = b"a,b\n" + b" " * 398 + b"\xea\xb0"
        assert len(data) == 100 * 4 + 4
        doc, ctx = parse_text(data)
    finally:
        limits.SHEET_CHARS = old
    text = repr(doc)
    assert b"\xea\xb0".decode("cp949") in text, text[-200:]


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
