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


def test_fraction_zero_shows_zero():
    # Excel shows 0 for zero under a whole-plus-fraction code (dochan's _fraction_number gives "0"); the alignment
    # spaces stay, as they do for 2 above
    assert display(0, "# ?/?") == "0    "
    assert display(0, "# ??/??") == "0      "
    assert display(0, "0 ?/?") == "0    "
    assert display(0, "?/?") == "0/1"


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


def test_iso_and_builtin():
    assert display(45000, "yyyy-mm-dd") == "2023-03-15"
    assert display(45000, code_for(14, {})) == "2023-03-15"


def test_month_names():
    assert display(45000, "d-mmm-yy") == "15-Mar-23"
    assert display(45000, "mmmm d, yyyy") == "March 15, 2023"


def test_weekday():
    assert display(45000, "dddd") == "Wednesday"
    assert display(45000, "ddd") == "Wed"


def test_korean_date_tokens():
    assert display(45000.75, 'yyyy"년" m"월" d"일"') == "2023년 3월 15일"
    assert display(45000, "yyyy-mm-dd(aaa)") == "2023-03-15(수)"
    assert display(45000, "yyyy aaaa") == "2023 수요일"
    assert display(45000.75, "[$-412]AM/PM h:mm") == "오후 6:00"


def test_number_section_edges():
    assert display(5, "[<0]0;0") == "5"
    assert display(-5, "[<0]0;[>0]0;\"z\"") == "-5"
    assert display(0, "[<0]0;[>0]0;\"z\"") == "z"
    assert display(1234567, "#,###,") == "1,235"
    assert display(1234567, "#,##0,,") == "1"
    assert display(5, "[$ ]0") == " 5"
    assert display(-0.001, "0.00") == "-0.00"       # Excel keeps the sign of a negative that rounds to zero
    assert display(0.001, "0.00") == "0.00"


def test_date_edges():
    assert display(0.5, "m:ss") == "0:00"            # m before s without h is minutes
    assert display(0.0007, "m:ss") == "1:00"
    assert display(45000, "mmmmm") == "M"
    assert display(45000, "yyyy e") == "2023 2023"
    # g (the era) prints nothing in a date section, as dochan; outside one it is a literal
    assert display(45000, "gyyyy") == "2023" and display(45000, "ggg yyyy") == " 2023"
    assert display(5, "0 g") == "5 g"
    assert display(44926.99999999, "yyyy-mm-dd hh:mm:ss") == "2023-01-01 00:00:00"   # rounds over midnight
    assert display(0.5, "[s]") == "43200"
    assert display(1, "[m]") == "1440"
    assert display(0, "dddd", True) == "Friday" and display(1462, "yyyy-mm-dd dddd", True) == "1908-01-02 Thursday"
    # a section mixing date tokens and number placeholders prints the placeholders as literals
    assert display(1.5, "0.0 h") == "0.0 12"


def test_korean_locale_names():
    # 45000 is 2023-03-15, a Wednesday; under [$-412] month and day names are Korean like AM/PM
    assert display(45000, "[$-412]yyyy년 mmmm d일 dddd") == "2023년 3월 15일 수요일"
    assert display(45000, "[$-412]mmm ddd") == "3월 수"
    assert display(45000, "[$-412]mmmm") == "3월"
    assert display(45000, "mmmm ddd") == "March Wed"          # no locale tag: English names stay
    assert display(45000, "[$-409]mmm dddd") == "Mar Wednesday"
    assert display(45000, "[$-412]mmmmm") == "3"  # the first character of the Korean month name, 3월
    # a tag is Korean when its hex LCID's low 16 bits are 0x0412, for month and day names and AM/PM alike
    for tag in ("412", "0412", "10412"):
        got = display(45000.75, f"[$-{tag}]mmm dddd AM/PM h"), display(45000, f"[$-{tag}]mmmmm")
        assert got == ("3월 수요일 오후 6", "3"), (tag, got)
    got = display(45000.75, "[$-F412]mmm dddd AM/PM h"), display(45000, "[$-F412]mmmmm")
    assert got == ("Mar Wednesday PM 6", "M"), got  # low word 0xF412: not Korean


def test_weekday_only():
    """A section whose only date token is the Korean weekday is still a date: 45000 is a Wednesday."""
    assert display(45000, "aaa") == "수", display(45000, "aaa")
    assert display(45000, "aaaa") == "수요일", display(45000, "aaaa")
    assert display(45000, "(aaa)") == "(수)", display(45000, "(aaa)")
    assert display(45000, "m/d aaa") == "3/15 수"


def test_twelve_hour():
    assert display(45000.75, "h:mm AM/PM") == "6:00 PM"
    assert display(45000.25, "h:mm AM/PM") == "6:00 AM"
    assert display(45000, "h:mm AM/PM") == "12:00 AM"
    assert display(45000.75, "h:mm A/P") == "6:00 P"


def test_minutes_vs_months():
    assert display(0.5, "h:mm") == "12:00"
    assert display(45000.5, "yyyy-mm-dd hh:mm:ss") == "2023-03-15 12:00:00"
    assert display(0.5, "mm:ss") == "00:00"
    assert display(0.50069444, "mm:ss.0") == "01:00.0"


def test_elapsed():
    assert display(1.5, "[h]:mm") == "36:00"
    assert display(1.5, "[h]:mm:ss") == "36:00:00"
    assert display(0.5, "[m]") == "720"


def test_leap_bug_and_1904():
    assert display(60, "yyyy-mm-dd") == "1900-02-29"
    assert display(59, "yyyy-mm-dd") == "1900-02-28"
    assert display(61, "yyyy-mm-dd") == "1900-03-01"
    assert display(0, "yyyy-mm-dd", date1904=True) == "1904-01-01"


def test_second_rounding():
    assert display(0.99999999, "h:mm:ss") == "0:00:00"
    assert display(0.5000058, "h:mm:ss.00") == "12:00:00.50"


def test_out_of_range():
    assert display(-1, "yyyy-mm-dd") == "-1"
    assert display(2958466, "yyyy-mm-dd") == "2958466"
    assert display(2958465, "yyyy-mm-dd") == "9999-12-31"
    assert display(2958465, "yyyy-mm-dd", date1904=True) == "2958465"


def test_numeric_letters_unchanged():
    assert display(5, '0 "e"') == "5 e"


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
