"""Legacy 8-bit text: the Windows code page a language id (LID) picks, and decoding with it.

Run: python -I -X utf8 tests/test_reader_codepage.py
"""
import codecs
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader.text import codepage, decode_8bit  # noqa: E402


def test_codepages():
    cases = {0x0412: "cp949", 0x0411: "cp932", 0x0404: "cp950", 0x0804: "cp936", 0x0419: "cp1251",
             0x0C1A: "cp1251", 0x041A: "cp1250", 0x0409: "cp1252", 0x0000: "cp1252"}
    for lid, cp in cases.items():
        assert codepage(lid) == cp, (hex(lid), codepage(lid))


def test_every_rule():
    cases = {
        0x1C1A: "cp1251", 0x081A: "cp1250",                        # Serbian and Bosnian Cyrillic, Serbian Latin
        0x0C04: "cp950", 0x1404: "cp950", 0x1004: "cp936",           # Hong Kong and Macao, Singapore
        0x0402: "cp1251", 0x0422: "cp1251", 0x0423: "cp1251",
        0x0408: "cp1253", 0x041F: "cp1254", 0x040D: "cp1255",
        0x0401: "cp1256", 0x0C01: "cp1256", 0x0429: "cp1256", 0x0420: "cp1256",
        0x0425: "cp1257", 0x0426: "cp1257", 0x0427: "cp1257", 0x042A: "cp1258", 0x041E: "cp874",
        0x0405: "cp1250", 0x040E: "cp1250", 0x0415: "cp1250", 0x0418: "cp1250", 0x041B: "cp1250",
        0x0424: "cp1250", 0x041C: "cp1250",
        0x0407: "cp1252", 0x040C: "cp1252", 0x0816: "cp1252", 0xFFFF: "cp1252",
    }
    for lid, cp in cases.items():
        assert codepage(lid) == cp, (hex(lid), codepage(lid))
    for lid in range(0x10000):
        codecs.lookup(codepage(lid))  # every answer is a codec Python has


def test_decode_8bit():
    assert decode_8bit("Привет".encode("cp1251"), 0x0419) == "Привет"
    assert decode_8bit("한글".encode("cp949"), 0x0412) == "한글"
    assert decode_8bit("café".encode("cp1252"), 0x0409) == "café"
    assert decode_8bit(b"a\x81b", 0x0409) == "a�b"  # undefined in cp1252: replaced, no error
    assert decode_8bit(b"\xb0", 0x0412) == "�"     # a lead byte with nothing after it


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
