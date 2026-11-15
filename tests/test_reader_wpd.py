"""WordPerfect reader: the Tika character sets, and (later tasks) the readers on top of them.

Run: python -I -X utf8 tests/test_reader_wpd.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.formats import wpd_charsets  # noqa: E402
from reader.formats.wpd_charsets import WP5, WP6, char  # noqa: E402

# Sizes of the Java arrays: Tika's own "(N chars)" comments, checked against the literals in the files.
WP6_SIZES = (96, 242, 145, 88, 102, 255, 238, 229, 219, 123, 250, 63, 256, 196, 220)
WP5_SIZES = (96, 242, 28, 88, 102, 35, 238, 229, 210, 119, 150, 185, 255)

# Samples were read by hand from the Java files, not from the module. Each is
# (set, code, text, Java line) in WP6Charsets.java / WP5Charsets.java at tag 4.2.0, where the line is the one that
# holds that entry's literal. Per set: its first entry, a middle one and its last. Set 12 is unmapped (tested apart).
WP6_SAMPLES = (
    (0, 0, " ", 44), (0, 49, "Q", 47), (0, 95, " ", 49),
    (1, 0, "̀", 51), (1, 120, "Ģ", 66), (1, 241, "ŏ", 81),
    (2, 0, "ʹ", 83), (2, 72, "ɞ", 92), (2, 144, "ʨ", 101),
    (3, 0, "░", 103), (3, 40, "╡", 108), (3, 87, "╇", 113),
    (4, 0, "●", 115), (4, 48, "□", 121), (4, 101, "ᴒ", 127),
    (5, 0, "♡", 131), (5, 128, "″", 147), (5, 254, "➾", 162),
    (6, 0, "−", 164), (6, 120, "≽", 179), (6, 237, " ", 193),
    (7, 0, "⌠", 195), (7, 112, "⎨", 209), (7, 228, " ", 223),
    (8, 0, "Α", 225), (8, 112, "ᾴ", 239), (8, 218, "Ϡ", 252),
    (9, 0, "א", 254), (9, 64, "֒", 262), (9, 122, "₪", 269),
    (10, 0, "А", 271), (10, 128, "Х", 287), (10, 249, "უ", 302),
    (11, 0, "｡", 304), (11, 32, "ﾁ", 308), (11, 62, "ﾟ", 311),
    (13, 0, " ", 331), (13, 96, "ش", 343), (13, 195, "ـ", 355),
    (14, 0, " ", 357), (14, 120, "ڙ", 372), (14, 219, "ﮥ", 384),
)
WP5_SAMPLES = (
    (2, 0, "̣", 42), (2, 16, "̡", 44), (2, 27, "‴", 45),
    (5, 0, "♥", 51), (5, 16, "⌐", 53), (5, 34, "⎵", 55),
    (8, 0, "Α", 61), (8, 104, "ἂ", 74), (8, 209, "ῥ", 87),
    (9, 0, "א", 89), (9, 56, "֚", 96), (9, 118, "יִ", 103),
    (10, 0, "А", 105), (10, 72, "Є", 114), (10, 149, "̀", 123),
    (11, 0, "ぁ", 125), (11, 96, "〃", 137), (11, 184, "ヾ", 148),
)


def test_wp6_sets_sampled():
    for s, code, want, line in WP6_SAMPLES:
        assert WP6[s][code] == want, ("WP6", s, code, line)
        assert char(s, code) == want, ("WP6", s, code, line)


def test_wp5_sets_sampled():
    for s, code, want, line in WP5_SAMPLES:
        assert WP5[s][code] == want, ("WP5", s, code, line)
        assert char(s, code, wp5=True) == want, ("WP5", s, code, line)
    # the sets Tika shares with WP6 are the WP6 arrays themselves (WP5Charsets.java lines 38, 40, 47, 49, 57, 59)
    for s in (0, 1, 3, 4, 6, 7):
        assert WP5[s] is WP6[s], s


def test_table_sizes():
    assert tuple(len(t) for t in WP6) == WP6_SIZES
    assert tuple(len(t) for t in WP5) == WP5_SIZES
    # Tika defines WP6 sets 0-14 and WP5 sets 0-12; every entry is a str
    assert len(WP6) == 15 and len(WP5) == 13
    assert all(isinstance(c, str) for t in (*WP6, *WP5) for c in t)


def test_unmapped_is_none():
    assert char(12, 5) is None
    assert char(12, 5, wp5=True) is None
    assert char(12, 0) is None and char(12, 255) is None
    assert all(c == "" for c in WP6[12]) and all(c == "" for c in WP5[12])
    assert char(1, 10_000) is None
    assert char(99, 0) is None
    assert char(-1, 0) is None and char(1, -1) is None
    assert char(14, 220) is None  # one past the WP6 set 14 table
    assert char(13, 0, wp5=True) is None  # WP5 has no set 13
    # WP5 set 10 codes 144-147 are '\u0000' placeholders in Tika (WP5Charsets.java line 123): kept in the table, unmapped
    assert WP5[10][144:148] == ("\x00",) * 4
    assert all(char(10, c, wp5=True) is None for c in (144, 145, 146, 147))
    assert char(10, 148, wp5=True) == "́" and char(10, 149, wp5=True) == "̀"
    # a space Tika maps is mapped, not unmapped (WP6Charsets.java line 331)
    assert char(13, 0) == " "


def test_composite_entry():
    # Tika's tables hold one character per entry (its own TODO says multi-characters are not mapped), so a
    # composite entry cannot come from them; `char` passes a longer entry through whole.
    assert all(len(c) <= 1 for t in (*WP6, *WP5) for c in t)
    real = wpd_charsets.WP6
    try:
        wpd_charsets.WP6 = real[:1] + (("a", "é"),) + real[2:]
        assert wpd_charsets.char(1, 1) == "é"
        assert len(wpd_charsets.char(1, 1)) == 2
    finally:
        wpd_charsets.WP6 = real


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
