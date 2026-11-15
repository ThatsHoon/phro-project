"""WordPerfect reader: the Tika character sets, the head, the WP6+ prefix index and body walk.

Run: python -I -X utf8 tests/test_reader_wpd.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import markdown  # noqa: E402
from reader.formats import wpd, wpd_charsets  # noqa: E402
from reader.formats.wpd_charsets import WP5, WP6, char  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Context, Hidden, Para, Text  # noqa: E402

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


# ---- WP6+ fixtures, built in memory

def _var(group, sub, payload=b"\x00\x00\x00"):
    """A variable-length group: [group][sub][u16 size] payload [u16 size][group]. The payload is everything between
    the two sizes (flags, prefix ids, the non-deletable size and the data); the default is flags 0, size 0."""
    size = len(payload) + 7
    return bytes((group, sub)) + struct.pack("<H", size) + payload + struct.pack("<H", size) + bytes((group,))


def _fixed(code, payload):
    """A fixed-length group: [code] payload [code]."""
    return bytes((code,)) + payload + bytes((code,))


def _on(attr):
    return _fixed(0xF2, bytes((attr,)))


def _off(attr):
    return _fixed(0xF3, bytes((attr,)))


def _undo(kind):
    return _fixed(0xF1, bytes((kind,)) + b"\x00\x00")


def _ext(code, cset):
    return _fixed(0xF0, bytes((code, cset)))


def _wp6(body: bytes, packets=(), *, encryption=0, ftype=0x0A, major=2, product=1) -> bytes:
    """A WordPerfect 6+ file: the 16-byte head, the index header at 16 and its 14-byte entries from 16 + 14, the
    packets, then the body the document pointer points at."""
    ih = 16
    index = 14 * (len(packets) + 1)
    entries, blobs = [], []
    at = ih + index
    for ptype, blob in packets:
        entries.append(struct.pack("<BBHHII", 0, ptype, 1, 0, len(blob), at))
        blobs.append(blob)
        at += len(blob)
    header = struct.pack("<BBH10s", 2, 0, len(packets) + 1, b"")
    head = b"\xffWPC" + struct.pack("<I", at) + bytes((product, ftype, major, 1)) + struct.pack("<HH", encryption, ih)
    return head + header + b"".join(entries) + b"".join(blobs) + body


def _summary(tag, text):
    """One group of a 0x12 summary packet: [u16 length][u16 tag][2 bytes][name chars, 0][data chars, 0]; each char is
    u16, low byte the code, high byte the set (set 0 is ASCII)."""
    name = struct.pack("<H", 0)
    data = b"".join(struct.pack("<BB", ord(c), 0) for c in text) + struct.pack("<H", 0)
    rest = struct.pack("<H", tag) + b"\x00\x00" + name + data
    return struct.pack("<H", len(rest) + 2) + rest


def _read(data):
    ctx = Context()
    return wpd.parse_bytes(data, ctx), ctx


def _md(doc, ctx, hidden=False):
    return markdown.render(doc, Path(tempfile.mkdtemp()) / "assets", hidden, ctx.skipped)


def _refused(data, kind):
    try:
        _read(data)
    except ReadError as e:
        assert e.kind == kind, e
        return e
    raise AssertionError(f"expected ReadError {kind}")


def _plain(doc):
    def flat(inl):
        out = []
        for it in inl:
            if isinstance(it, Text):
                out.append(it.text)
            elif isinstance(it, (Bold, Hidden)):
                out.append(flat(it.inlines))
        return "".join(out)
    return [flat(b.inlines) for b in doc.blocks]


# ---- the brief's tests

def test_wp6_paragraphs_and_bold():
    body = b"Hello\x80" + _on(12) + b"World" + _off(12) + b"\xccNext"
    data = _wp6(body)
    doc, ctx = _read(data)
    assert len(doc.blocks) == 2 and all(isinstance(b, Para) for b in doc.blocks)
    first = doc.blocks[0].inlines
    assert first[0] == Text("Hello ") and first[1] == Bold([Text("World")]), first
    assert doc.blocks[1].inlines == [Text("Next")]
    out = _md(doc, ctx)
    assert "Hello **World**" in out and "Next" in out
    assert not ctx.skipped
    # parse(path) reads the file and gives the same document
    h = wpd.head(data)
    assert h == wpd.Head(pointer=len(data) - len(body), product=1, ftype=0x0A, major=2, minor=1, encryption=0)
    path = Path(tempfile.mkdtemp()) / "a.wpd"
    path.write_bytes(data)
    assert wpd.parse(path, Context()).blocks == doc.blocks
    # soft EOLs are spaces; a D0 hard EOL and a page break end a paragraph
    body = (b"one\xcftwo" + _var(0xD0, 0x01) + b"three" + _var(0xD0, 0x04) + b"four\xc7five"
            + _var(0xD0, 0x17) + b"six")
    doc, _ = _read(_wp6(body))
    assert _plain(doc) == ["one two three", "four", "five", "six"], _plain(doc)
    # the extended international characters 0x01-0x20, a hard space and a hard hyphen
    doc, _ = _read(_wp6(b"\x01\x20x\x81y\x84z"))
    assert _plain(doc) == ["åßx y-z"], _plain(doc)


def test_extended_chars():
    body = b"a" + _ext(120, 1) + _ext(0x41, 0) + _ext(5, 12) + b"b"
    doc, ctx = _read(_wp6(body))
    assert _plain(doc) == ["aĢAb"], _plain(doc)
    assert ctx.skipped["glyphs"] == 1
    # a composite entry (one code, two code points) is put in whole
    real = wpd_charsets.WP6
    try:
        wpd_charsets.WP6 = real[:1] + (("a", "é"),) + real[2:]
        doc, ctx = _read(_wp6(b"x" + _ext(1, 1) + b"y"))
    finally:
        wpd_charsets.WP6 = real
    assert _plain(doc) == ["xéy"] and not ctx.skipped


def test_undo_hidden_and_shown():
    body = b"Keep\x80" + _undo(0) + b"Gone" + _undo(1) + b"\x80Also"
    doc, ctx = _read(_wp6(body))
    inl = doc.blocks[0].inlines
    assert inl == [Text("Keep "), Hidden([Text("Gone")], "deleted"), Text(" Also")], inl
    out = _md(doc, ctx)
    assert "Gone" not in out and "Keep" in out and "Also" in out
    assert "(1 hidden places left out)" in out, out
    shown = _md(doc, ctx, hidden=True)
    assert "(hidden: Gone)" in shown, shown
    # inside an undo span a paragraph end and a bold change are deleted too: the paragraph runs on
    body = b"A\x80" + _undo(0) + b"x\xcc" + _on(12) + b"y" + _undo(1) + b"\x80B"
    doc, _ = _read(_wp6(body))
    assert len(doc.blocks) == 1
    assert doc.blocks[0].inlines == [Text("A "), Hidden([Text("x y")], "deleted"), Text(" B")], doc.blocks[0].inlines


def test_undo_without_end():
    body = b"Start\xcc" + _undo(0) + b"Tail\xccMore"
    doc, ctx = _read(_wp6(body))
    assert _plain(doc)[0] == "Start"
    assert all(isinstance(i, Hidden) and i.kind == "deleted" for i in doc.blocks[1].inlines)
    out = _md(doc, ctx)
    assert "Start" in out and "Tail" not in out and "More" not in out
    shown = _md(doc, ctx, hidden=True)
    assert "Tail" in shown and "More" in shown


def test_resync_one_record():
    good = _var(0xD4, 0x1C)
    bad = good[:-1] + b"\x99"  # the closing byte is not the group's
    doc, ctx = _read(_wp6(b"Before\x80" + bad + b"After"))
    text = _plain(doc)[0]
    assert text.startswith("Before") and text.endswith("After"), text
    assert ctx.skipped["record"] == 1
    # a wrong closing size fails too; two bad groups apart are two runs, two bad bytes in a row one run
    bad_size = good[:-3] + b"\x00\x00" + good[-1:]
    doc, ctx = _read(_wp6(b"x" + bad_size + b"y" + bad + b"z"))
    assert ctx.skipped["record"] == 2, ctx.skipped
    doc, ctx = _read(_wp6(b"x\xf2\xf2\x0cqz"))  # F2 fails, the next F2 fails at once: one run
    assert ctx.skipped["record"] == 1 and _plain(doc)[0].endswith("qz"), (ctx.skipped, _plain(doc))
    # a size too small to hold the group fails
    doc, ctx = _read(_wp6(b"x\xd4\x00\x03\x00y"))
    assert ctx.skipped["record"] == 1 and _plain(doc)[0].endswith("y")


def test_truncated_body_keeps_text():
    data = _wp6(b"Before\xccKept" + _var(0xD4, 0x1C, b"\x00\x00\x00" + b"\x00" * 20) + b"Lost")
    cut = data[:len(data) - len("Lost") - 12]  # in the middle of the group
    doc, ctx = _read(cut)
    assert _plain(doc) == ["Before", "Kept"], _plain(doc)
    assert ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}
    # a fixed group cut by the end, and a variable group cut inside its size field
    for tail in (b"\xf0\x41", b"\xd4\x1c\x10"):
        doc, ctx = _read(_wp6(b"Text" + tail))
        assert _plain(doc) == ["Text"] and ctx.skipped["record"] == 1, (tail, ctx.skipped)


def test_redline_strikeout_plain():
    body = b"a\x80" + _on(10) + b"red" + _off(10) + b"\x80" + _on(13) + b"strike" + _off(13) + b"\x80b"
    doc, ctx = _read(_wp6(body))
    assert doc.blocks[0].inlines == [Text("a red strike b")], doc.blocks[0].inlines
    assert not ctx.skipped and not ctx.warnings


def test_attribute_above_16():
    doc, ctx = _read(_wp6(b"x" + _on(17) + b"y"))
    assert _plain(doc) == ["xy"]
    assert ctx.skipped["attribute"] == 1
    assert ctx.warnings == ["attribute: unknown WordPerfect attribute number"]
    doc, ctx = _read(_wp6(_on(17) + b"y" + _off(17) + _on(200) + b"z" + _on(16)))
    assert ctx.skipped["attribute"] == 3 and len(ctx.warnings) == 1
    assert "attribute" in markdown.INTERNAL and not markdown.lost(ctx.skipped)


def test_summary_title():
    packet = _summary(5, "Author") + _summary(17, "  A   Title ")
    doc, ctx = _read(_wp6(b"Body", [(0x01, b"\x00" * 4), (0x12, packet)]))
    assert doc.title == "A Title", doc.title
    assert _plain(doc) == ["Body"] and not ctx.skipped
    doc, _ = _read(_wp6(b"Body", [(0x12, _summary(17, "t" * 600))]))
    assert doc.title == "t" * 500
    # the prefix index: type -> (id, offset, size), the ids counted from 1
    data = _wp6(b"Body", [(0x01, b"abcd"), (0x12, packet), (0x01, b"")])
    got = wpd._prefix6(data, Context())
    assert sorted(got) == [0x01, 0x12]
    assert [(i, s) for i, _, s in got[0x01]] == [(1, 4), (3, 0)]
    _, off, size = got[0x12][0]
    assert data[off:off + size] == packet
    # an entry whose range is past the end of the file is skipped and counted
    data = bytearray(_wp6(b"Body", [(0x12, packet)]))
    struct.pack_into("<I", data, 16 + 14 + 10, len(data))  # the entry's offset
    ctx = Context()
    assert wpd._prefix6(bytes(data), ctx) == {} and ctx.skipped["record"] == 1
    doc, ctx = _read(bytes(data))
    assert doc.title == "" and _plain(doc) == ["Body"] and ctx.skipped["record"] == 1
    # an index whose entries run past the end of the file: the ones inside are read, the cut counted once
    data = bytearray(_wp6(b"Body", [(0x12, packet)]))
    struct.pack_into("<H", data, 16 + 2, 60_000)
    struct.pack_into("<II", data, 16 + 14 + 6, 4, 0)  # entry 1: the head's first 4 bytes
    ctx = Context()
    assert wpd._prefix6(bytes(data[:16 + 28 + 7]), ctx) == {0x12: [(1, 0, 4)]}  # cut inside entry 2
    assert ctx.skipped["record"] == 1


def test_refusals():
    assert wpd.head(b"not a wpd file at all") is None
    assert _refused(_wp6(b"x", encryption=1), "encrypted")
    assert "WPG" in _refused(_wp6(b"x", ftype=0x16), "unsupported").detail
    assert _refused(_wp6(b"x", ftype=0x2C), "unsupported")
    assert "WordPerfect version" in _refused(_wp6(b"x", major=1), "unsupported").detail
    assert "WordPerfect version" in _refused(_wp6(b"x", product=2), "unsupported").detail
    # the order: the encryption word first, then WPG, then Mac, then the version
    assert _refused(_wp6(b"x", encryption=1, ftype=0x16, major=1), "encrypted")
    assert "WPG" in _refused(_wp6(b"x", ftype=0x16, major=1, product=2), "unsupported").detail
    data = bytearray(_wp6(b"x"))
    struct.pack_into("<I", data, 4, len(data) + 1)
    assert _refused(bytes(data), "damaged")
    assert _refused(_wp6(b""), "damaged")
    assert _refused(_wp6(b"\x80\x80\xcc"), "damaged")  # a body that gives no block
    assert _refused(b"\xffWPC\x10\x00", "damaged")  # a head cut short


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
