"""RTF lexer tokens, \\bin handling, parameters and code page choice.

Run: python -I -X utf8 tests/test_reader_rtf_lex.py
"""
import codecs
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader.formats import rtf_lex as lex  # noqa: E402
from reader.formats.rtf_lex import BIN, CLOSE, HEX, OPEN, SYM, TEXT, WORD, Pending, codec, tokens  # noqa: E402


def toks(data):
    return list(tokens(data))


def test_tokens_basic():
    got = toks(rb"{\rtf1\ansi\b bold\b0 text}")
    assert got == [(OPEN,), (WORD, "rtf", 1), (WORD, "ansi", None), (WORD, "b", None), (TEXT, b"bold"),
                   (WORD, "b", 0), (TEXT, b"text"), (CLOSE,)], got


def test_word_caps():
    got = toks(b"\\" + b"a" * 100 + b"7 x")
    assert got == [(WORD, "a" * 32, 7), (TEXT, b"x")], got
    got = toks(rb"\fs99999999999999 y")
    assert got == [(WORD, "fs", 2**31 - 1), (TEXT, b"y")], got
    assert toks(rb"\fs-99999999999999")[0] == (WORD, "fs", -2**31)
    assert toks(rb"\fs0000000000000012")[0] == (WORD, "fs", 12)


def test_bin_raw():
    data = rb"{\pict\bin5 " + b"\x00}{\x01\x02" + rb"}"
    got = toks(data)
    assert [t[0] for t in got] == [OPEN, WORD, BIN, CLOSE], got
    assert got[2][2] == 5 and data[got[2][1]:got[2][1] + 5] == b"\x00}{\x01\x02"


def test_bin_huge_clamped():
    data = rb"\bin2147483647 abc"
    got = toks(data)
    assert len(got) == 1 and got[0][0] == BIN and data[got[0][1]:got[0][1] + got[0][2]] == b"abc", got


def test_hex_pair_cp949():
    p = Pending()
    for t in toks(rb"\'c7\'d1\'b1\'db"):
        assert t[0] == HEX
        p.push(t[1])
    assert p.flush("cp949") == "한글"
    assert p.flush("cp949") == ""


def test_backslash_newline():
    assert toks(b"a\\\r\nb") == [(TEXT, b"a"), (WORD, "par", None), (TEXT, b"b")]
    assert toks(b"a\\\nb") == [(TEXT, b"a"), (WORD, "par", None), (TEXT, b"b")]
    assert toks(b"a\r\n\x00b") == [(TEXT, b"a"), (TEXT, b"b")]


def test_codec_choice():
    assert codec(129, None, 1252) == "cp949"
    assert codec(0, None, 949) == "cp949"
    assert codec(None, None, None) == "cp1252"
    assert codec(None, 932, 1252) == "cp932"
    assert codec(None, None, 1251) == "cp1251"
    assert codec(999, 1253, None) == "cp1253"      # unknown charset falls through to cpg
    assert codec(1, 999999, 777) == "cp1252"
    assert codec(0, 1251, 99999) == "cp1251"       # unknown ansicpg falls through
    for cs, name in {2: "cp1252", 77: "mac_roman", 128: "cp932", 130: "johab", 134: "gbk", 136: "cp950",
                     161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255", 178: "cp1256",
                     186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250", 254: "cp437", 255: "cp437"}.items():
        assert codec(cs, None, 1252) == name, cs
    for cs in list(lex._CHARSET) + [0]:
        codecs.lookup(codec(cs, None, 949))
    for cp in lex._CODEPAGE:
        codecs.lookup(codec(None, cp, None))
    assert codec(None, None, 936) == "gbk" and codec(None, None, 874) == "cp874"


def test_escaped_braces_are_symbols():
    got = toks(rb"a\{b\}c\\d")
    assert got == [(TEXT, b"a"), (SYM, "{"), (TEXT, b"b"), (SYM, "}"), (TEXT, b"c"), (SYM, "\\"), (TEXT, b"d")], got
    assert toks(rb"\~\-\_\*\:\|") == [(SYM, c) for c in "~-_*:|"]


def test_bad_hex_does_not_raise():
    assert toks(rb"\'zz") == [(TEXT, b"'"), (TEXT, b"zz")]
    assert toks(rb"\'4") == [(TEXT, b"'"), (TEXT, b"4")]
    assert toks(b"\\'") == [(TEXT, b"'")]
    assert toks(rb"\'41") == [(HEX, 0x41)]


def test_negative_param_and_dash():
    assert toks(rb"\u-10179?") == [(WORD, "u", -10179), (TEXT, b"?")]
    assert toks(rb"\b-x") == [(WORD, "b", None), (TEXT, b"-x")]
    assert toks(rb"\b- x") == [(WORD, "b", None), (TEXT, b"- x")]


def test_bin_zero_and_negative():
    for src in (rb"\bin0 ab", rb"\bin-5 ab", rb"\bin ab"):
        got = toks(src)
        assert got[0][0] == BIN and got[0][2] == 0 and got[1] == (TEXT, b"ab"), (src, got)
    data = rb"{\bin3 a}b}c}"
    got = toks(data)
    assert got == [(OPEN,), (BIN, 7, 3), (CLOSE,), (TEXT, b"c"), (CLOSE,)], got
    assert data[got[1][1]:got[1][1] + got[1][2]] == b"a}b"


def test_lone_backslash_end():
    assert toks(b"ab\\") == [(TEXT, b"ab")]
    assert toks(b"\\") == []
    assert toks(b"") == []


def test_pending_unknown_codec():
    p = Pending()
    p.push(b"ab")
    p.push(0xe9)
    assert p.flush("no-such-codec") == "abé"
    p.push(b"\x81")
    assert p.flush("cp949") == "�"
    p.push(b"x")
    assert p.flush(None) == "x"


def test_lexer_speed():
    unit = (rb"{\rtf1\ansi\deff0{\fonttbl{\f0\fcharset129 Font;}}\pard\plain\f0\fs24\b Some words here\b0 "
            rb"and more text \'c7\'d1\'b1\'db \u-10179? 12345? {\i italic}\par\line\tab }" + b"\r\n")
    data = unit * (2 * 2**20 // len(unit))
    assert len(data) >= 2 * 2**20 - len(unit)
    start = time.perf_counter()
    n = sum(1 for _ in tokens(data))
    took = time.perf_counter() - start
    print(f"lexer_speed: {len(data)} bytes, {n} tokens, {took:.2f}s")
    assert took < 3.0, took


def test_pending_empty_push_is_a_no_op():
    p = Pending()
    p.push(b"")
    assert not p
    p.push(b"")
    p.push(b"a")
    assert p and p.flush("cp1252") == "a" and not p


def test_space_after_hex_escape_stays_text():
    # only control words eat a following space; a hex escape does not
    assert toks(rb"\'e9 x") == [(HEX, 0xE9), (TEXT, b" x")]
    assert toks(rb"\'e9  x") == [(HEX, 0xE9), (TEXT, b"  x")]


def test_one_space_only_is_eaten_and_delimiters_are_kept():
    assert toks(rb"\b  x") == [(WORD, "b", None), (TEXT, b" x")]
    assert toks(rb"\b12  x") == [(WORD, "b", 12), (TEXT, b" x")]
    # a delimiter that is not a space stays: the next backslash, a brace, a letter after a digit run
    assert toks(rb"\b\i{x}") == [(WORD, "b", None), (WORD, "i", None), (OPEN,), (TEXT, b"x"), (CLOSE,)]
    assert toks(rb"\b12x") == [(WORD, "b", 12), (TEXT, b"x")]
    # a tab is not the eaten space
    assert toks(b"\\b\tx") == [(WORD, "b", None), (TEXT, b"\tx")]


def test_dbcs_pair_split_across_hex_and_text():
    # cp932 lead byte 0x82 as a hex escape, trail byte 0xA0 as plain text: one character
    got = toks(b"\\'82\xa0")
    assert got == [(HEX, 0x82), (TEXT, b"\xa0")], got
    p = Pending()
    for t in got:
        p.push(t[1])
    assert p.flush("cp932") == "\u3042"
    # and the other way round: text lead, hex trail
    p = Pending()
    p.push(b"\x82")
    p.push(0xA0)
    assert p.flush("cp932") == "\u3042"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
