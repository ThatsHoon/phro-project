"""Excel number formats: numbers, text, sections, conditions, fractions, General.

Run: python -I -X utf8 tests/test_reader_numfmt.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader.numfmt import BUILTIN, code_for, display  # noqa: E402


def test_won_currency():
    assert display(1385.5, '"₩"#,##0.00') == "₩1,385.50"
    assert display(1385.5, "[$₩-412]#,##0.00") == "₩1,385.50"
    assert display(5, "[Red]0") == "5"


def test_thousands_scaling():
    assert display(1234567, "#,##0") == "1,234,567"
    assert display(1234567, '#,##0,"K"') == "1,235K"
    assert display(5, "0,000") == "0,005"
    assert display(1234567, "0.0,,") == "1.2"


def test_percent():
    assert display(0.256, "0.0%") == "25.6%"
    assert display(1, "0%") == "100%"


def test_sections():
    assert display(-5, "#,##0;(#,##0)") == "(5)"
    assert display(0, '#,##0;-#,##0;"-"') == "-"
    assert display(-5, "0") == "-5"
    assert display(7, ";;;") == ""
    assert display(-1234.5, '"$"#,##0.00') == "-$1,234.50"


def test_condition():
    assert display(150, '[>100]"big";"small"') == "big"
    assert display(50, '[>100]"big";"small"') == "small"
    assert display(5, '[>100]"big";[<10]"tiny"') == "tiny"


def test_padding():
    assert display(-2.5, "0.00_);(0.00)") == "(2.50)"
    assert display(2.5, "0.00_);(0.00)") == "2.50 "
    assert display(3, "*-0") == "3"
    assert display(3, r"\#0") == "#3"


def test_half_up():
    assert display(0.125, "0.00") == "0.13"
    assert display(2.675, "0.00") == "2.68"
    assert display(0.5, "0") == "1"
    assert display(2.5, "#") == "3"
    assert display(1.5, "0.0#") == "1.5"
    assert display(1.05, "0.0#") == "1.05"
    assert display(0.5, "#.00") == ".50"
    assert display(12.5, ".00") == "12.50"


def test_scientific():
    assert display(12345.678, "0.00E+00") == "1.23E+04"
    assert display(0.00012, "0.0E+00") == "1.2E-04"
    assert display(0.00012, "0.0E-00") == "1.2E-04"
    assert display(12345, "##0.0E+0") == "12.3E+3"
    assert display(0, "0.00E+00") == "0.00E+00"
    assert display(9.999, "0.0E+0") == "1.0E+1"


def test_fraction():
    assert display(0.5, "# ?/?") == "1/2"
    assert display(2.25, "# ?/4") == "2 1/4"
    assert display(3.14159, "# ??/??") == "3 14/99"
    assert display(0.75, "?/8") == "6/8"
    assert display(2, "# ?/?") == "2    "
    assert display(1.5, "?/?") == "3/2"
    assert display(-1.5, "# ?/?") == "-1 1/2"


def test_general():
    assert display(0.1 + 0.2, "General") == "0.3"
    assert display(123456789012, "General") == "123456789012"
    assert display(1e20, "General") == "1E+20"
    assert display(1.5e-10, "General") == "1.5E-10"
    assert display(-0.0, "General") == "0"
    assert display(3.0, "General") == "3"
    assert display(1e15, "General") == "1E+15"
    assert display(1.5, "") == "1.5"
    assert display(-2.5, "General") == "-2.5"
    assert display(2, '"n="General') == "n=2"
    assert display(float("nan"), "General") == "#NUM!"


def test_text_and_bool():
    assert display("abc", '"Name: "@') == "Name: abc"
    assert display(True, "General") == "TRUE"
    assert display(False, "0.00") == "FALSE"
    assert display("#N/A", "0.00") == "#N/A"
    assert display("#DIV/0!", '"x"@') == "#DIV/0!"
    assert display("abc", "0.00") == "abc"
    assert display("abc", '0;0;0;"t:"@') == "t:abc"
    assert display(None, "0.00") == ""
    assert display(5, "@") == "5"


def test_bad_code():
    assert display(1.5, '0.0"') == "1.5"
    assert display(1.5, "0.0[") == "1.5"
    assert display(1.5, "0;0;0;0;0") == "1.5"
    assert display(1.5, "0" * 300) == "1.5"


def test_date_code_falls_back_for_now():
    assert display(1.5, "yyyy-mm-dd") == "1.5"


def test_code_for():
    assert code_for(164, {164: "0.0"}) == "0.0"
    assert code_for(3, {}) == "#,##0"
    assert code_for(999, {}) == "General"
    assert BUILTIN[14] == "yyyy-mm-dd" and BUILTIN[22] == "yyyy-mm-dd h:mm"
    assert BUILTIN[32] == "h:mm" and BUILTIN[33] == "h:mm:ss"
    assert "$" not in BUILTIN[5] and "$" not in BUILTIN[44]
    assert all(BUILTIN[i] == "yyyy-mm-dd" for i in (27, 28, 29, 30, 31, 34, 35, 36, *range(50, 59)))


def test_hostile_is_bounded():
    t = time.time()
    for code in ("[" * 250, '"' * 250, "0," * 120, "0/" * 120, "[$-" * 80, ";" * 250, "0.0E+" * 40,
                 "# ?/?" * 40, "_" * 250, "\\" * 249, "*" * 249):
        display(1234.5678, code)
        display("abc", code)
    assert display(1e308, "0." + "0" * 30) != ""
    assert display(1.7976931348623157e308, "#,##0") != ""
    assert time.time() - t < 3


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
