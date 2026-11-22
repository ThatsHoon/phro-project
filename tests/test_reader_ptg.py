"""Formula tokens: each token class from hand-built bytes, in BIFF8 and BIFF12 widths, and the unknown token."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reader.formats import ptg  # noqa: E402


def env8(**kw):
    return ptg.Env(False, **kw)


def env12(**kw):
    return ptg.Env(True, **kw)


def ref8(row, col, row_rel=True, col_rel=True, ptg_id=0x24):
    return struct.pack("<BHH", ptg_id, row, col | (0x8000 if row_rel else 0) | (0x4000 if col_rel else 0))


def ref12(row, col, row_rel=True, col_rel=True, ptg_id=0x24):
    return struct.pack("<BIH", ptg_id, row, col | (0x8000 if row_rel else 0) | (0x4000 if col_rel else 0))


def area8(r1, r2, c1, c2, ptg_id=0x25):
    return struct.pack("<BHHHH", ptg_id, r1, r2, c1 | 0xC000, c2 | 0xC000)


def num(v):
    return struct.pack("<Bd", 0x1F, v)


def text(rgce, env, extra=b""):
    return ptg.text(rgce, extra, env)


def test_operands():
    assert text(ref8(0, 0), env8()) == "A1"
    assert text(ref8(4, 2, False, False), env8()) == "$C$5"
    assert text(ref8(4, 2, True, False), env8()) == "$C5"
    assert text(ref12(1_048_575, 16_383), env12()) == "XFD1048576"
    assert text(area8(0, 9, 1, 1), env8()) == "B1:B10"
    assert text(struct.pack("<BH", 0x1E, 42), env8()) == "42"
    assert text(num(2.5), env8()) == "2.5" and text(num(3.0), env8()) == "3"
    assert text(struct.pack("<BB", 0x1D, 1), env8()) == "TRUE"
    assert text(struct.pack("<BB", 0x1C, 0x07), env8()) == "#DIV/0!"
    assert text(struct.pack("<BBB", 0x17, 3, 0) + b'a"b', env8()) == '"a""b"'
    assert text(struct.pack("<BH", 0x17, 2) + "가b".encode("utf-16-le"), env12()) == '"가b"'
    assert text(struct.pack("<BHH", 0x2A, 0, 0), env8()) == "#REF!"


def test_operators_and_parentheses():
    rgce = ref8(0, 0) + ref8(0, 1) + b"\x03" + b"\x15" + num(2) + b"\x05"  # (A1+B1)*2
    assert text(rgce, env8()) == "(A1+B1)*2"
    assert text(num(5) + b"\x13", env8()) == "-5"
    assert text(num(5) + b"\x14", env8()) == "5%"
    assert text(ref8(0, 0) + ref8(1, 0) + b"\x0E", env8()) == "A1<>A2"
    assert text(ref8(0, 0) + ref8(1, 0) + b"\x08", env8()) == "A1&A2"


def test_functions():
    fixed = ref8(0, 0) + struct.pack("<BH", 0x41, 24)  # ABS(A1), value class
    assert text(fixed, env8()) == "ABS(A1)"
    var = area8(0, 3, 0, 0) + num(1) + struct.pack("<BBH", 0x42, 2, 4)  # SUM(A1:A4,1)
    assert text(var, env8()) == "SUM(A1:A4,1)"
    assert text(area8(0, 3, 0, 0) + struct.pack("<BBH", 0x19, 0x10, 0), env8()) == "SUM(A1:A4)"  # attrSum
    cond = (ref8(0, 0) + num(0) + b"\x0D" + struct.pack("<BBH", 0x19, 0x02, 9) + num(1)
            + struct.pack("<BBH", 0x19, 0x08, 3) + num(2) + struct.pack("<BBH", 0x19, 0x08, 3)
            + struct.pack("<BBH", 0x42, 3, 1))
    assert text(cond, env8()) == "IF(A1>0,1,2)"
    assert text(num(1) + struct.pack("<BBH", 0x22, 1, 4), env12()) == "SUM(1)"


def test_shared_formula_offsets():
    # RefN and AreaN hold offsets from the cell: one shared formula gives each cell its own references
    rgce = struct.pack("<BHH", 0x2C, 0xFFFF, 0xC0FE) + struct.pack("<BHH", 0x2C, 0, 0xC0FF) + b"\x05"
    assert text(rgce, env8(row=4, col=3)) == "B4*C5"
    assert text(rgce, env8(row=9, col=3)) == "B9*C10"
    assert text(struct.pack("<BHH", 0x2C, 0xFFFF, 0xC000), env8(row=0, col=0)) == "#REF!"
    wide = struct.pack("<BIH", 0x2C, 0xFFFFFFFF, 0xC000 | 0x3FFE)
    assert text(wide, env12(row=4, col=3)) == "B4"
    assert ptg.exp(struct.pack("<BHH", 0x01, 1, 3), b"", False) == (1, 3)
    assert ptg.exp(struct.pack("<BI", 0x01, 7), struct.pack("<I", 3), True) == (7, 3)
    assert ptg.exp(struct.pack("<BI", 0x01, 7), b"", True) is None and ptg.exp(num(1), b"", False) is None


def test_three_d_references_and_names():
    sheets = ["Data", "My Sheet", None]
    assert text(struct.pack("<BH", 0x3A, 0) + ref8(0, 0)[1:], env8(sheets=sheets)) == "Data!A1"
    assert text(struct.pack("<BH", 0x3B, 1) + area8(0, 1, 0, 0)[1:], env8(sheets=sheets)) == "'My Sheet'!A1:A2"
    assert text(struct.pack("<BH", 0x3A, 2) + ref8(0, 0)[1:], env8(sheets=sheets)) is None
    assert text(struct.pack("<BHH", 0x23, 1, 0), env8(names=["TaxRate"])) == "TaxRate"
    assert text(struct.pack("<BI", 0x23, 1), env12(names=["TaxRate"])) == "TaxRate"


def test_array_constant():
    extra = struct.pack("<BH", 1, 0) + struct.pack("<Bd", 1, 1.0) + struct.pack("<BHB", 2, 1, 0) + b"x"
    assert text(b"\x60" + bytes(7), env8(), extra) == '{1,"x"}'


def test_unknown_token_is_none():
    assert text(b"\x18\x00\x00", env8()) is None
    assert text(num(1) + struct.pack("<BH", 0x21, 9999), env8()) is None
    assert text(ref8(0, 0)[:3], env8()) is None
    assert text(num(1) + num(2), env8()) is None


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            fn()
    print("ok")
