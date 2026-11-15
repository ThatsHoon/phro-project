"""WordPerfect reader: the Tika character sets, the head, the WP6+ prefix index and body walk, and the WP6+
sub-documents (notes, comments, headers, footers, watermarks, boxes), pictures and tables; WordPerfect 5.x and 4.2,
the compound-file wrapper and the LibreOffice switch (Popen faked: no soffice is run).

Run: python -I -X utf8 tests/test_reader_wpd.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import images, limits, markdown  # noqa: E402
from reader.formats import wpd, wpd_charsets  # noqa: E402
from reader.formats.wpd_charsets import WP5, WP6, char  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Cell, Context, Hidden, Image, NoteRef, Para, Table, Text  # noqa: E402

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
    packets, then the body the document pointer points at. Packet k of the list (from 0) has prefix id k + 1."""
    ih = 16
    index = 14 * (len(packets) + 1)
    entries, blobs = [], []
    at = ih + index
    for ptype, blob, *flags in packets:  # (type, bytes) or (type, bytes, entry flags)
        entries.append(struct.pack("<BBHHII", flags[0] if flags else 0, ptype, 1, 0, len(blob), at))
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
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)


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
    return [flat(b.inlines) if hasattr(b, "inlines") else type(b).__name__ for b in doc.blocks]


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
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.wpd"
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


# ---- WP6+ sub-document, box and table fixtures

def _grp(group, sub, pids=(), data=b"", flags=0):
    """A variable group with its head: flags (0x80 added when there are prefix ids), [n][u16 id] * n, the u16
    non-deletable size (all of `data` here), then `data`."""
    head = bytes((flags | (0x80 if pids else 0),))
    if pids:
        head += bytes((len(pids),)) + b"".join(struct.pack("<H", p) for p in pids)
    return _var(group, sub, head + struct.pack("<H", len(data)) + data)


def _text(*parts):
    """A 0x08 sub-document packet: u16 block count, 4 bytes, a u32 size a block, then the blocks."""
    return (struct.pack("<H", len(parts)) + b"\x00" * 4 + b"".join(struct.pack("<I", len(p)) for p in parts)
            + b"".join(parts))


def _note(pid, endnote=False, number=b"1"):
    """A footnote (or endnote) on, the number text the body shows, and the note off."""
    return _grp(0xD7, 2 if endnote else 0, [pid]) + number + _grp(0xD7, 3 if endnote else 1)


def _style(ctype, name=0):
    """A 0x41 graphics box style packet: no children, the box data (name and library, counter, positioning and
    content sections, each after its u16 size); `name` is the s16 name length, -4 an equation box."""
    box = (struct.pack("<H", 4) + b"\x00\x00" + struct.pack("<h", name) + struct.pack("<H", 0) + struct.pack("<H", 0)
           + struct.pack("<H", 3) + bytes((0, ctype, 0)))
    return struct.pack("<H", 0) + struct.pack("<H", len(box)) + box


def _override(ctype):
    """A box group's own data overriding the content type: 14 reserved bytes, two sizes, the override flags (content
    data only), then the content data: u16 size, u16 flags (0x4000: the type follows), the type."""
    content = struct.pack("<H", 0x4000) + bytes((ctype,))
    return b"\x00" * 18 + struct.pack("<H", 0x2000) + struct.pack("<H", len(content)) + content


def _eol(*subs):
    """EOL group data: the u16 deletable size (0), then the non-deletable sub-functions."""
    return struct.pack("<H", 0) + b"".join(subs)


def _span(cols, rows):
    return bytes((0x85, cols, rows, 0x85))


def _rowinfo(flags):
    return bytes((0x80, flags, 0, 0, 0x80))


_WPG = b"\xffWPC" + struct.pack("<I", 16) + bytes((1, 0x16, 1, 0)) + b"\x00" * 6 + b"picture"


def _utf16(s):
    return s.encode("utf-16-le") + b"\x00\x00"


# ---- the brief's tests (Task 8)

def test_footnote_packet():
    body = b"Before" + _note(1) + b"\x80after"
    doc, ctx = _read(_wp6(body, [(0x08, _text(b"Note\x80", b"text"))]))
    assert doc.blocks[0].inlines == [Text("Before"), NoteRef("fn-1"), Text(" after")], doc.blocks[0].inlines
    assert doc.notes == {"fn-1": [Para([Text("Note text")])]}, doc.notes
    out = _md(doc, ctx)
    assert "Before[^1] after" in out and "[^1]: Note text" in out, out
    assert out.count("Note text") == 1 and "Before1" not in out
    assert not ctx.skipped
    # a note whose packet is missing, or not a text packet, keeps the body and counts the note
    doc, ctx = _read(_wp6(b"A" + _note(5) + b"B" + _note(1) + b"C", [(0x12, b"")]))
    assert _plain(doc) == ["ABC"] and not doc.notes and ctx.skipped["note"] == 2, ctx.skipped
    # a note with no text gives no mark; one whose block table runs past its packet counts the note
    doc, ctx = _read(_wp6(b"A" + _note(1) + b"B", [(0x08, _text())]))
    assert _plain(doc) == ["AB"] and not doc.notes and not ctx.skipped
    bad = struct.pack("<H", 2) + b"\x00" * 4 + struct.pack("<II", 1, 2**31) + b"x"
    doc, ctx = _read(_wp6(b"A" + _note(1) + b"B", [(0x08, bad)]))
    assert _plain(doc) == ["AB"] and ctx.skipped["note"] == 1
    # the same note referred to twice is one note with two marks
    doc, ctx = _read(_wp6(b"A" + _note(1) + b"B" + _note(1) + b"C", [(0x08, _text(b"N"))]))
    assert doc.blocks[0].inlines == [Text("A"), NoteRef("fn-1"), Text("B"), NoteRef("fn-1"), Text("C")]
    assert list(doc.notes) == ["fn-1"] and not ctx.skipped
    # inside deleted text a note is left out with it, as libwpd does
    doc, ctx = _read(_wp6(b"A" + _undo(0) + b"x" + _note(1) + _undo(1) + b"B", [(0x08, _text(b"N"))]))
    assert not doc.notes and not ctx.skipped


def test_endnote_is_footnote():
    body = b"One" + _note(1, endnote=True, number=b"i") + b"\x80two" + _note(2)
    doc, ctx = _read(_wp6(body, [(0x08, _text(b"End")), (0x08, _text(b"Foot"))]))
    assert doc.blocks[0].inlines == [Text("One"), NoteRef("fn-1"), Text(" two"), NoteRef("fn-2")]
    assert doc.notes == {"fn-1": [Para([Text("End")])], "fn-2": [Para([Text("Foot")])]}
    out = _md(doc, ctx)
    assert "One[^1] two[^2]" in out and "[^1]: End" in out and "[^2]: Foot" in out, out


def test_comment_chain():
    comment = struct.pack("<HHB", 1, 1, 0)  # one prefix id: the text packet 1, then the flags
    body = b"Text" + _grp(0xD4, 0x1D, [2]) + b"\x80more"
    doc, ctx = _read(_wp6(body, [(0x08, _text(b"A\x80remark")), (0x1B, comment)]))
    assert doc.blocks[0].inlines == [Text("Text"), NoteRef("cm-1"), Text(" more")], doc.blocks[0].inlines
    assert doc.notes["cm-1"] == [Para([Text("Comment: "), Text("A remark")])], doc.notes
    out = _md(doc, ctx)
    assert "Text[^1] more" in out and "[^1]: Comment: A remark" in out, out
    assert not ctx.skipped
    # a comment whose 0x1B packet is missing, or names no single text packet, counts a note
    for packets in ([(0x08, _text(b"x"))], [(0x08, _text(b"x")), (0x1B, struct.pack("<HHB", 2, 1, 0))]):
        doc, ctx = _read(_wp6(body, packets))
        assert _plain(doc) == ["Text more"] and not doc.notes and ctx.skipped["note"] == 1, (packets, ctx.skipped)


def test_note_in_note_cycle():
    inner = b"Inner" + _note(1, number=b"x") + b"\x80end"  # packet 1 refers to itself
    doc, ctx = _read(_wp6(b"Body" + _note(1) + b".", [(0x08, _text(inner))]))
    assert doc.blocks[0].inlines == [Text("Body"), NoteRef("fn-1"), Text(".")], doc.blocks[0].inlines
    assert doc.notes == {"fn-1": [Para([Text("Inner end")])]}, doc.notes
    assert ctx.skipped["depth"] == 1 and set(ctx.skipped) == {"depth"}
    # a note in a note is a note of its own; past MAX_DEPTH the deeper one is cut, counted depth, the outer kept
    packets = [(0x08, _text(b"One" + _note(2))), (0x08, _text(b"Two" + _note(3))), (0x08, _text(b"Three"))]
    doc, ctx = _read(_wp6(b"Body" + _note(1), packets))
    assert len(doc.notes) == 3 and not ctx.skipped
    out = _md(doc, ctx)
    assert "Body[^1]" in out and "One[^2]" in out and "Two[^3]" in out and "[^3]: Three" in out, out
    real = limits.MAX_DEPTH
    try:
        limits.MAX_DEPTH = 2
        doc, ctx = _read(_wp6(b"Body" + _note(1), packets))
    finally:
        limits.MAX_DEPTH = real
    assert ctx.skipped["depth"] == 1 and set(ctx.skipped) == {"depth"}, ctx.skipped
    assert sorted(_flatnotes(doc).values()) == ["One", "Two"], doc.notes


def _flatnotes(doc):
    return {k: "".join(i.text for b in v for i in b.inlines if isinstance(i, Text)) for k, v in doc.notes.items()}


def test_header_footer_watermark():
    packets = [(0x08, _text(b"Head")), (0x08, _text(b"Foot")), (0x08, _text(b"Mark")), (0x08, _text(b"Head")),
               (0x08, _text(b"Other\x80foot"))]
    body = (_grp(0xD6, 0, [1], b"\x03") + _grp(0xD6, 2, [2], b"\x03") + _grp(0xD6, 4, [3], b"\x03")
            + _grp(0xD6, 1, [4], b"\x03") + _grp(0xD6, 3, [5], b"\x03") + b"Body")
    doc, ctx = _read(_wp6(body, packets))
    assert doc.headers == ["Head", "Mark"] and doc.footers == ["Foot", "Other foot"], (doc.headers, doc.footers)
    assert _plain(doc) == ["Body"] and not ctx.skipped
    out = _md(doc, ctx)
    assert "Header: Head | Mark" in out and "Footer: Foot | Other foot" in out, out
    # a header without a prefix id, or whose packet is missing, is counted under its kind
    doc, ctx = _read(_wp6(_grp(0xD6, 0, (), b"\x03") + _grp(0xD6, 2, [9], b"\x03") + b"Body", packets))
    assert not doc.headers and not doc.footers
    assert ctx.skipped["header"] == 1 and ctx.skipped["footer"] == 1, ctx.skipped
    # a note in a header cannot be shown on the header line: it is counted, the header kept
    packets = [(0x08, _text(b"Head" + _note(2))), (0x08, _text(b"Lost"))]
    doc, ctx = _read(_wp6(_grp(0xD6, 0, [1], b"\x03") + b"Body", packets))
    assert doc.headers == ["Head"] and not doc.notes and ctx.skipped["note"] == 1, (doc.headers, ctx.skipped)


def test_text_box_in_place():
    packets = [(0x08, _text(b"Boxed")), (0x41, _style(1))]
    body = b"One" + _grp(0xDF, 0, [2, 1]) + b"Two"
    doc, ctx = _read(_wp6(body, packets))
    assert _plain(doc) == ["One", "Boxed", "Two"], _plain(doc)
    assert not ctx.skipped
    # the box's own content data overrides the style; a box flagged to be ignored, or a graphics rule, is nothing
    doc, ctx = _read(_wp6(b"One" + _grp(0xDF, 1, [1], _override(1)) + b"Two", [(0x08, _text(b"Boxed"))]))
    assert _plain(doc) == ["One", "Boxed", "Two"], _plain(doc)
    for group in (_grp(0xDF, 0, [2, 1], flags=0x40), _grp(0xDF, 3, [2, 1])):
        doc, ctx = _read(_wp6(b"One" + group + b"Two", packets))
        assert _plain(doc) == ["OneTwo"] and not ctx.skipped
    # a text box with no text packet counts a shape
    doc, ctx = _read(_wp6(b"One" + _grp(0xDF, 0, [1]) + b"Two", [(0x41, _style(1))]))
    assert _plain(doc) == ["OneTwo"] and ctx.skipped["shape"] == 1, (_plain(doc), ctx.skipped)


def test_equation_box_object():
    packets = [(0x08, _text(b"x\x80over\x80y")), (0x41, _style(1, name=-4))]
    doc, ctx = _read(_wp6(b"Before" + _grp(0xDF, 0, [2, 1]) + b"After", packets))
    assert _plain(doc) == ["BeforeAfter"], _plain(doc)
    assert ctx.skipped["object"] == 1 and set(ctx.skipped) == {"object"}
    assert "1 embedded object" in _md(doc, ctx)


def test_box_hyperlink_link():
    packets = [(0x6F, _WPG), (0x40, struct.pack("<HH", 1, 1), 1), (0x41, _style(3)), (0x07, _utf16("http://x"))]
    doc, ctx = _read(_wp6(b"See" + _grp(0xDF, 0, [3, 2, 4]) + b"this", packets))
    assert ctx.skipped["link"] == 1 and set(ctx.skipped) == {"link"}, ctx.skipped
    assert [type(b) for b in doc.blocks] == [Para, Image, Para]


def test_graphics_box_wpg():
    packets = [(0x6F, _WPG), (0x40, struct.pack("<HH", 1, 1), 1), (0x41, _style(3))]
    doc, ctx = _read(_wp6(b"See" + _grp(0xDF, 0, [3, 2]) + b"this", packets))
    assert [type(b) for b in doc.blocks] == [Para, Image, Para], doc.blocks
    pic = doc.blocks[1]
    assert pic.data == _WPG and images.sniff(pic.data) == "wpg"
    assert not ctx.skipped
    out = _md(doc, ctx)
    assert "Not viewable here: assets/" in out and ".wpg" in out, out
    # bytes that are not WPG are still kept as the box's picture
    doc, ctx = _read(_wp6(b"See" + _grp(0xDF, 0, [3, 2]) + b"this", [(0x6F, b"junk"), *packets[1:]]))
    assert doc.blocks[1] == Image(b"junk") and not ctx.skipped
    # a 0x40 packet without its child flag names no picture (libwpd): counted image
    doc, ctx = _read(_wp6(b"See" + _grp(0xDF, 0, [3, 2]), [packets[0], (0x40, struct.pack("<HH", 1, 1)), packets[2]]))
    assert [type(b) for b in doc.blocks] == [Para] and ctx.skipped["image"] == 1
    # past IMAGE_BYTES a picture is counted image (two boxes, two pictures)
    two = [*packets, (0x6F, _WPG), (0x40, struct.pack("<HH", 1, 4), 1)]
    real = limits.IMAGE_BYTES
    try:
        limits.IMAGE_BYTES = len(_WPG) + 1
        doc, ctx = _read(_wp6(b"A" + _grp(0xDF, 0, [3, 2]) + b"B" + _grp(0xDF, 0, [3, 5]), two))
    finally:
        limits.IMAGE_BYTES = real
    assert [type(b) for b in doc.blocks] == [Para, Image, Para] and ctx.skipped["image"] == 1, doc.blocks
    assert set(ctx.skipped) == {"image"}


def test_picture_once():
    # one picture packet referred to by two boxes is one Image; the second reference counts depth
    packets = [(0x6F, _WPG), (0x40, struct.pack("<HH", 1, 1), 1), (0x41, _style(3))]
    doc, ctx = _read(_wp6(b"A" + _grp(0xDF, 0, [3, 2]) + b"B" + _grp(0xDF, 0, [3, 2]) + b"C", packets))
    assert [type(b) for b in doc.blocks] == [Para, Image, Para], doc.blocks
    assert ctx.skipped["depth"] == 1 and set(ctx.skipped) == {"depth"}, ctx.skipped


def test_doubling_chain_linear():
    # each text packet holds two boxes to the next: emitted once a packet, so the output stays linear
    levels = 20
    packets = [(0x41, _style(1))]  # pid 1; text packets are pids 2 .. levels + 2
    for k in range(levels):
        nxt = k + 3
        packets.append((0x08, _text(b"L%d" % k + _grp(0xDF, 0, [1, nxt]) + _grp(0xDF, 0, [1, nxt]))))
    packets.append((0x08, _text(b"leaf")))
    t0 = time.perf_counter()
    doc, ctx = _read(_wp6(b"Body" + _grp(0xDF, 0, [1, 2]) + _grp(0xDF, 0, [1, 2]), packets))
    took = time.perf_counter() - t0
    assert _plain(doc) == ["Body", *("L%d" % k for k in range(levels)), "leaf"], _plain(doc)
    assert ctx.skipped["depth"] == levels + 1 and set(ctx.skipped) == {"depth"}, ctx.skipped
    assert took < 2, took


def test_header_box_counts():
    # a picture in a text box in a header counts image; a footnote there counts note and leaves no orphan note
    pic = [(0x6F, _WPG), (0x40, struct.pack("<HH", 1, 1), 1), (0x41, _style(3)), (0x41, _style(1))]
    packets = [*pic, (0x08, _text(b"Top\x80Head" + _grp(0xDF, 0, [4, 6]))),
               (0x08, _text(b"In\x80box" + _grp(0xDF, 0, [3, 2]) + _note(7))), (0x08, _text(b"Lost"))]
    doc, ctx = _read(_wp6(_grp(0xD6, 0, [5], b"\x03") + b"Body", packets))
    assert doc.headers == ["Top Head In box"], doc.headers
    assert not doc.notes and ctx.skipped["image"] == 1 and ctx.skipped["note"] == 1, ctx.skipped
    assert set(ctx.skipped) == {"image", "note"}, ctx.skipped
    # a header packet referred to again gives nothing, uncounted
    doc, ctx = _read(_wp6(_grp(0xD6, 0, [1], b"\x03") * 2 + b"Body", [(0x08, _text(b"Head"))]))
    assert doc.headers == ["Head"] and not ctx.skipped


def test_bound_cell_without_owner():
    # bound cells with nothing above are plain one-column cells, each at the column it is at
    def row(*subs):
        return _grp(0xD0, 0x0B, (), _eol(*subs))

    def cell(*subs):
        return _grp(0xD0, 0x0A, (), _eol(*subs))

    body = _grp(0xD4, 0x2A) + row(_span(255, 1)) + b"a" + cell(_span(255, 1)) + b"b" + cell(_span(255, 1)) + b"c\xbf"
    doc, ctx = _read(_wp6(body))
    assert doc.blocks[0].rows == [[Cell([Para([Text(x)])]) for x in "abc"]], doc.blocks
    assert not ctx.skipped
    # a long row of them keeps a cell a column up to GRID_COLS, the rest cut once
    body = _grp(0xD4, 0x2A) + row(_span(255, 1)) + (b"x" + cell(_span(255, 1))) * 1_200 + b"\xbf"
    doc, ctx = _read(_wp6(body))
    assert len(doc.blocks[0].rows[0]) == limits.GRID_COLS and ctx.skipped["cells"] == 1, ctx.skipped


def test_other_box_with_text_counts_shape():
    packets = [(0x08, _text(b"Unread")), (0x41, _style(2))]
    doc, ctx = _read(_wp6(b"One" + _grp(0xDF, 0, [2, 1]) + b"Two", packets))
    assert _plain(doc) == ["OneTwo"] and ctx.skipped["shape"] == 1 and set(ctx.skipped) == {"shape"}
    # a box of kind 0 with no text packet is nothing, as in libwpd
    doc, ctx = _read(_wp6(b"One" + _grp(0xDF, 0, [1]) + b"Two", [(0x41, _style(0))]))
    assert _plain(doc) == ["OneTwo"] and not ctx.skipped


def test_reference_loops_and_caps():
    # two notes referring to each other: the inner reference back is a cycle, counted depth
    packets = [(0x08, _text(b"One" + _note(2))), (0x08, _text(b"Two" + _note(1)))]
    doc, ctx = _read(_wp6(b"Body" + _note(1), packets))
    assert sorted(_flatnotes(doc).values()) == ["One", "Two"] and ctx.skipped["depth"] == 1, ctx.skipped
    assert set(ctx.skipped) == {"depth"}
    # a comment whose text holds a comment back to itself
    comment = struct.pack("<HHB", 1, 1, 0)
    packets = [(0x08, _text(b"Remark" + _grp(0xD4, 0x1D, [2]))), (0x1B, comment)]
    doc, ctx = _read(_wp6(b"Body" + _grp(0xD4, 0x1D, [2]), packets))
    assert list(doc.notes) == ["cm-1"] and ctx.skipped["depth"] == 1 and set(ctx.skipped) == {"depth"}
    assert doc.blocks[0].inlines == [Text("Body"), NoteRef("cm-1")]
    # repeated references count toward WPD_SUBDOCS
    real = wpd.WPD_SUBDOCS
    try:
        wpd.WPD_SUBDOCS = 2
        doc, ctx = _read(_wp6(b"a" + _note(1) + b"b" + _note(1) + b"c" + _note(1) + b"d", [(0x08, _text(b"N"))]))
    finally:
        wpd.WPD_SUBDOCS = real
    assert doc.blocks[0].inlines == [Text("a"), NoteRef("fn-1"), Text("b"), NoteRef("fn-1"), Text("cd")]
    assert ctx.skipped["note"] == 1 and set(ctx.skipped) == {"note"}, ctx.skipped
    # a link on a text box (not a picture) counts link, the text in place
    packets = [(0x08, _text(b"Boxed")), (0x41, _style(1)), (0x07, _utf16("http://x"))]
    doc, ctx = _read(_wp6(b"One" + _grp(0xDF, 0, [2, 1, 3]) + b"Two", packets))
    assert _plain(doc) == ["One", "Boxed", "Two"] and ctx.skipped["link"] == 1 and set(ctx.skipped) == {"link"}


def test_table_span_and_header():
    def cell(*subs):
        return _grp(0xD0, 0x0A, (), _eol(*subs))

    def row(*subs):
        return _grp(0xD0, 0x0B, (), _eol(*subs))

    body = (b"Pre" + _grp(0xD4, 0x2A, (), b"\x00\x00\x00\x00") + _grp(0xD4, 0x2C) + _grp(0xD4, 0x2B)
            + row(_rowinfo(0x04), _span(1, 2)) + b"A" + cell() + b"B"
            + row(_span(128, 1)) + cell() + b"D" + _grp(0xD0, 0x11) + b"Post")
    doc, ctx = _read(_wp6(body))
    assert [type(b) for b in doc.blocks] == [Para, Table, Para], doc.blocks
    t = doc.blocks[1]
    assert t.rows == [[Cell([Para([Text("A")])], rowspan=2, header=True), Cell([Para([Text("B")])], header=True)],
                      [Cell([Para([Text("D")])])]], t.rows
    assert not ctx.skipped
    out = _md(doc, ctx)
    assert all(x in out for x in ("Pre", "A", "B", "D", "Post")), out
    # the single-byte codes: a row, a cell, the table off
    doc, ctx = _read(_wp6(_grp(0xD4, 0x2A) + b"\xc5x\xc6y\xc5z\xbfafter"))
    assert doc.blocks[0].rows == [[Cell([Para([Text("x")])]), Cell([Para([Text("y")])])],
                                  [Cell([Para([Text("z")])])]], doc.blocks
    assert _plain(doc)[1:] == ["after"]
    # text in a bound cell joins the cell above
    body = _grp(0xD4, 0x2A) + row(_span(1, 2)) + b"top" + row(_span(129, 1)) + b"joined" + _grp(0xD0, 0x11)
    doc, _ = _read(_wp6(body))
    assert doc.blocks[0].rows == [[Cell([Para([Text("top")]), Para([Text("joined")])], rowspan=2)]], doc.blocks
    # outside a table the cell and row codes end a paragraph, as before
    doc, _ = _read(_wp6(b"one" + _grp(0xD0, 0x0A) + b"two\xc6three"))
    assert _plain(doc) == ["one", "two", "three"]
    # a table never closed ends with its story; one with no cell gives nothing
    doc, _ = _read(_wp6(b"x" + _grp(0xD4, 0x2A) + b"\xc5a\xc6b"))
    assert doc.blocks[1].rows == [[Cell([Para([Text("a")])]), Cell([Para([Text("b")])])]], doc.blocks
    doc, _ = _read(_wp6(b"x" + _grp(0xD4, 0x2A) + _grp(0xD0, 0x11) + b"y"))
    assert _plain(doc) == ["x", "y"]
    # the document's cells are capped by TABLE_NODES: past it the rest are cut, counted once a table
    real = limits.TABLE_NODES
    try:
        limits.TABLE_NODES = 2
        doc, ctx = _read(_wp6(_grp(0xD4, 0x2A) + b"\xc5a\xc6b\xc5c\xc6d\xbfe"))
    finally:
        limits.TABLE_NODES = real
    assert doc.blocks[0].rows == [[Cell([Para([Text("a")])]), Cell([Para([Text("b")])])]], doc.blocks
    assert ctx.skipped["cells"] == 1 and _plain(doc)[1:] == ["e"]
    # a table cell holds a text box in place
    packets = [(0x08, _text(b"Boxed")), (0x41, _style(1))]
    doc, _ = _read(_wp6(_grp(0xD4, 0x2A) + b"\xc5a" + _grp(0xDF, 0, [2, 1]) + b"b\xbf", packets))
    assert doc.blocks[0].rows == [[Cell([Para([Text("a")]), Para([Text("Boxed")]), Para([Text("b")])])]], doc.blocks


def test_subdocs_cap():
    packets = [(0x08, _text(b"N1")), (0x08, _text(b"N2")), (0x08, _text(b"N3"))]
    real = wpd.WPD_SUBDOCS
    try:
        wpd.WPD_SUBDOCS = 2
        doc, ctx = _read(_wp6(b"a" + _note(1) + b"b" + _note(2) + b"c" + _note(3) + b"d", packets))
    finally:
        wpd.WPD_SUBDOCS = real
    assert list(doc.notes) == ["fn-1", "fn-2"] and ctx.skipped["note"] == 1, (doc.notes, ctx.skipped)
    assert _plain(doc) == ["abcd"]
    assert wpd.WPD_SUBDOCS == 10_000


# ---- carried over from the Task 7 review

def test_del_byte_dropped():
    doc, ctx = _read(_wp6(b"a\x7fb"))
    assert _plain(doc) == ["ab"] and not ctx.skipped


def test_cut_group_inside_failed_extent():
    # a D4 group failing its closing byte declares 12 bytes; inside them a D5 claims 65,535 bytes, past the end:
    # that cut group joins the failure run, so the walk goes on and one record is counted
    bad = b"\xd4\x1c\x0c\x00" + b"\xd5\x00\xff\xff" + b"\x00" + b"\x0c\x00" + b"\x99"
    doc, ctx = _read(_wp6(b"x" + bad + b"After"))
    text = _plain(doc)[0]
    assert text.startswith("x") and text.endswith("After"), text
    assert ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}, ctx.skipped


def test_stray_group_ends_walk():
    doc, ctx = _read(_wp6(b"Before" + b"\xd5\x00\xff\xff" + b"After"))
    assert _plain(doc) == ["Before"], _plain(doc)
    assert ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}


def test_hostile_index_count():
    data = bytearray(_wp6(b"Body"))
    struct.pack_into("<H", data, 16 + 2, 65535)
    doc, ctx = _read(bytes(data))
    assert _plain(doc) == ["Body"] and ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}, ctx.skipped


# ---- WP5 fixtures, built in memory

def _var5(group, sub, payload=b""):
    """A WP5 variable group: [group][sub][u16 len] payload [u16 len][sub][group], len the whole size minus 4."""
    n = len(payload) + 4
    return bytes((group, sub)) + struct.pack("<H", n) + payload + struct.pack("<H", n) + bytes((sub, group))


def _wp5(body: bytes, packets=(), *, encryption=0, last_next=0) -> bytes:
    """A WordPerfect 5.x file: the 16-byte head (major 0, the encryption word in WP5's byte order), from 16 the
    0xFFFB index blocks (each [0xFFFB][5][50][u32 next] and four 10-byte indexes [u16 type][u32 size][u32 offset]),
    the packets, then the body. `last_next` is the last block's next pointer (0 ends the chain)."""
    nb = max(1, -(-len(packets) // 4))
    at = 16 + 50 * nb
    blocks, blobs = [], []
    for k in range(nb):
        nxt = 16 + 50 * (k + 1) if k < nb - 1 else last_next
        block = struct.pack("<HHHI", 0xFFFB, 5, 50, nxt)
        for ptype, blob in packets[4 * k:4 * k + 4]:
            block += struct.pack("<HII", ptype, len(blob), at)
            blobs.append(blob)
            at += len(blob)
        blocks.append(block.ljust(50, b"\x00"))
    head = b"\xffWPC" + struct.pack("<I", at) + bytes((1, 0x0A, 0, 1)) + struct.pack(">H", encryption) + b"\x00\x00"
    return head + b"".join(blocks) + b"".join(blobs) + body


def _fn5(text, endnote=False, pages=0):
    """A WP5 footnote (D6 sub 0) or endnote (sub 1): flags, u16 number, then for a footnote the page count and
    2 * (pages + 1) + 9 bytes, for an endnote 4 bytes, then the note's text."""
    head = b"\x00" + struct.pack("<H", 1)
    head += b"\x00" * 4 if endnote else bytes((pages,)) + b"\x00" * (2 * (pages + 1) + 9)
    return _var5(0xD6, 1 if endnote else 0, head + text)


def _hf5(sub, text, occurrence=1):
    """A WP5 header or footer (D5 sub 0-3): 7 bytes, the occurrence byte, 10 bytes, the text."""
    return _var5(0xD5, sub, b"\x00" * 7 + bytes((occurrence,)) + b"\x00" * 10 + text)


def _figure5(n, box_type=0x80):
    """A WP5 figure box (DA sub 0): 48 bytes, the box type, 60 bytes, the u16 picture number."""
    return _var5(0xDA, 0, b"\x00" * 48 + bytes((box_type,)) + b"\x00" * 60 + struct.pack("<H", n))


def _gfx5(*pictures):
    """A WP5 graphics packet (8): u16 count, u32 sizes, the bytes."""
    return (struct.pack("<H", len(pictures)) + b"".join(struct.pack("<I", len(p)) for p in pictures)
            + b"".join(pictures))


def _fixed5(code, payload):
    return bytes((code,)) + payload + bytes((code,))


def _cells(table):
    class _D:
        def __init__(self, blocks):
            self.blocks = blocks
    return [[" ".join(_plain(_D(c.blocks))) for c in row] for row in table.rows]


# ---- the brief's tests (Task 9)

def test_wp5_text_notes_headers():
    body = (_hf5(0, b"Running\x0dhead") + b"Body " + _fixed5(0xC3, b"\x0c") + b"bold" + _fixed5(0xC4, b"\x0c")
            + _fn5(b"Foot\x0dnote") + b" text\x0aNext" + _fixed5(0xC0, bytes((120, 1))) + b"\xa0end\x0c"
            + _hf5(2, b"Page foot") + _fn5(b"End", endnote=True) + b"Last")
    doc, ctx = _read(_wp5(body))
    assert doc.headers == ["Running head"] and doc.footers == ["Page foot"], (doc.headers, doc.footers)
    first = doc.blocks[0].inlines
    assert first == [Text("Body "), Bold([Text("bold")]), NoteRef("fn-1"), Text(" text")], first
    assert _plain(doc) == ["Body bold text", "NextĢ end", "Last"], _plain(doc)
    assert doc.blocks[2].inlines == [NoteRef("fn-2"), Text("Last")], doc.blocks[2].inlines
    assert doc.notes == {"fn-1": [Para([Text("Foot note")])], "fn-2": [Para([Text("End")])]}, doc.notes
    assert not ctx.skipped, ctx.skipped
    out = _md(doc, ctx)
    assert "Body **bold**[^1] text" in out and "[^1]: Foot note" in out and "Running head" in out, out
    # a footnote that skips its page counts, a header turned off (occurrence 0), a note in a header (counted note)
    body = _fn5(b"Skip", pages=2) + b"x" + _hf5(1, b"Gone", occurrence=0) + _hf5(0, b"H" + _fn5(b"in head"))
    doc, ctx = _read(_wp5(body))
    assert _flatnotes(doc) == {"fn-1": "Skip"} and doc.headers == ["H"], (doc.notes, doc.headers)
    assert ctx.skipped["note"] == 1 and set(ctx.skipped) == {"note"}, ctx.skipped
    # notes nested past MAX_DEPTH are cut, counted depth, the outer kept; the version refusal still stands
    nested = b"deep"
    for _ in range(5):
        nested = _fn5(b"n" + nested)
    real = limits.MAX_DEPTH
    try:
        limits.MAX_DEPTH = 2
        doc, ctx = _read(_wp5(b"Body" + nested))
    finally:
        limits.MAX_DEPTH = real
    assert len(doc.notes) == 2 and ctx.skipped["depth"] == 1 and set(ctx.skipped) == {"depth"}, ctx.skipped
    assert wpd.head(_wp5(b"x")).major == 0
    # a broken group resyncs one byte (record once); a group cut by the end ends the walk, the text kept
    bad = _var5(0xD4, 0, b"abc")[:-1] + b"\x9b"  # the closing id is wrong (0x9B is no code of its own)
    doc, ctx = _read(_wp5(b"Before " + bad + b" After"))
    assert _plain(doc)[0].startswith("Before") and _plain(doc)[0].endswith("After") and ctx.skipped["record"] == 1
    doc, ctx = _read(_wp5(b"Kept\x0a\xd4\x00\xff\xffLost"))
    assert _plain(doc) == ["Kept"] and ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}, ctx.skipped
    # a table: definition on, a row, cells (one bound to the cell above), off
    cell = lambda span=1, rows=1: _var5(0xDC, 0, bytes((0, 0, span, rows)) + b"\x00" * 7)  # noqa: E731
    body = (b"Before" + _var5(0xD2, 0x0B, b"\x00" * 8) + _var5(0xDC, 1) + cell(rows=2) + b"A" + cell() + b"B"
            + _var5(0xDC, 1) + cell(0x81) + b"A2" + cell() + b"C" + _var5(0xDC, 2) + b"After")
    doc, ctx = _read(_wp5(body))
    assert [type(b) for b in doc.blocks] == [Para, Table, Para], doc.blocks
    rows = doc.blocks[1].rows
    assert _cells(doc.blocks[1]) == [["A A2", "B"], ["C"]], rows
    assert rows[0][0].rowspan == 2 and not ctx.skipped


def test_wp5_comment():
    body = b"Text" + _var5(0xD9, 2, b"\x00\x00\x01" + b"A\x0dremark") + b" more" + _var5(0xD9, 7, b"conversion")
    doc, ctx = _read(_wp5(body))
    assert doc.blocks[0].inlines == [Text("Text"), NoteRef("cm-1"), Text(" more")], doc.blocks[0].inlines
    assert doc.notes == {"cm-1": [Para([Text("Comment: "), Text("A remark")])]}, doc.notes
    assert not ctx.skipped
    out = _md(doc, ctx)
    assert "Text[^1] more" in out and "[^1]: Comment: A remark" in out and "conversion" not in out, out


def test_wp5_picture_gets_wpg_head():
    raw = b"\x01\x02picture records"
    doc, ctx = _read(_wp5(b"See" + _figure5(0) + b"this", [(8, _gfx5(raw))]))
    assert [type(b) for b in doc.blocks] == [Para, Image, Para], doc.blocks
    pic = doc.blocks[1].data
    assert images.sniff(pic) == "wpg" and pic.endswith(raw) and len(pic) == 16 + len(raw), pic
    assert not ctx.skipped
    # a picture with its own WPG head is kept as it is; the second of two references to it counts depth
    doc, ctx = _read(_wp5(b"A" + _figure5(1) + b"B" + _figure5(1) + b"C", [(8, _gfx5(raw, _WPG))]))
    assert [b.data for b in doc.blocks if isinstance(b, Image)] == [_WPG] and ctx.skipped["depth"] == 1
    # a picture number past the packet, no packet, a figure of another box type: counted image
    for data in (_wp5(b"A" + _figure5(3), [(8, _gfx5(raw))]), _wp5(b"A" + _figure5(0)),
                 _wp5(b"A" + _figure5(0, box_type=1), [(8, _gfx5(raw))])):
        doc, ctx = _read(data)
        assert _plain(doc) == ["A"] and ctx.skipped["image"] == 1 and set(ctx.skipped) == {"image"}, ctx.skipped
    # sizes that run past the packet: the pictures are unreadable, counted image
    doc, ctx = _read(_wp5(b"A" + _figure5(0), [(8, struct.pack("<HI", 1, 2**31) + b"x")]))
    assert ctx.skipped["image"] == 1
    # past IMAGE_BYTES a picture is counted image
    real = limits.IMAGE_BYTES
    try:
        limits.IMAGE_BYTES = 20
        doc, ctx = _read(_wp5(b"A" + _figure5(0), [(8, _gfx5(raw))]))
    finally:
        limits.IMAGE_BYTES = real
    assert ctx.skipped["image"] == 1 and not any(isinstance(b, Image) for b in doc.blocks)


def test_wp5_picture_cap_before_copy():
    raw = bytes(range(1, 19))  # 18 bytes without a WPG head: 34 with the head put before it
    real = limits.IMAGE_BYTES
    try:
        # the head counts toward the cap: 34 fits in 34, not in 33
        limits.IMAGE_BYTES = 34
        doc, ctx = _read(_wp5(b"A" + _figure5(0), [(8, _gfx5(raw))]))
        assert [len(b.data) for b in doc.blocks if isinstance(b, Image)] == [34] and not ctx.skipped, ctx.skipped
        limits.IMAGE_BYTES = 33
        # each reference past the cap counts image, as in WP6; a smaller picture after it still fits
        body = b"A" + _figure5(0) + b"B" + _figure5(0) + b"C" + _figure5(0) + b"D" + _figure5(1)
        doc, ctx = _read(_wp5(body, [(8, _gfx5(raw, _WPG))]))
        assert [b.data for b in doc.blocks if isinstance(b, Image)] == [_WPG], doc.blocks
        assert ctx.skipped["image"] == 3 and set(ctx.skipped) == {"image"}, ctx.skipped
        # a picture past the cap is never copied: not at its first reference, not at a later one
        copies = []

        class _Counted(bytes):
            def __getitem__(self, key):
                if isinstance(key, slice) and len(range(*key.indices(len(self)))) > 1_000:
                    copies.append(key)
                return bytes.__getitem__(self, key)

        fig = _figure5(0)
        data = _Counted(fig + bytes(100_000))
        limits.IMAGE_BYTES = 50_000
        ctx = Context()
        st = wpd._State(data, {}, wpd.Doc(), ctx)
        st.gfx = [(len(fig), len(data))]
        story = wpd._Story("body")
        for _ in range(5):
            wpd._box5(data, 0, 4, len(fig) - 4, 0, story, st)
        assert not copies and ctx.skipped["image"] == 5, (copies, ctx.skipped)
    finally:
        limits.IMAGE_BYTES = real


def test_wp5_block_cycle():
    raw = b"\x01\x02picture records"
    doc, ctx = _read(_wp5(b"Body" + _figure5(0), [(8, _gfx5(raw))], last_next=16))
    assert _plain(doc)[0] == "Body" and images.sniff(doc.blocks[1].data) == "wpg"
    assert ctx.skipped["record"] == 1 and set(ctx.skipped) == {"record"}, ctx.skipped
    # a next pointer past the end, or to a block that is not 0xFFFB, counts record once; the text kept
    for nxt in (10**6, 16 + 50 + 1):
        doc, ctx = _read(_wp5(b"Body", [(8, _gfx5(raw))], last_next=nxt))
        assert _plain(doc) == ["Body"] and ctx.skipped["record"] == 1, (nxt, ctx.skipped)
    # WP5_BLOCKS ends a long chain: counted record
    real = wpd.WP5_BLOCKS
    try:
        wpd.WP5_BLOCKS = 2
        doc, ctx = _read(_wp5(b"Body", [(1, b"")] * 12))
    finally:
        wpd.WP5_BLOCKS = real
    assert _plain(doc) == ["Body"] and ctx.skipped["record"] == 1, ctx.skipped
    assert wpd.WP5_BLOCKS == 1_024 and wpd.WP42_PROBE == 65_536


def test_wp5_encryption():
    data = _wp5(b"x", encryption=0x0102)
    assert wpd.head(data).encryption == 0x0102  # read in WP5's byte order
    assert _refused(data, "encrypted")
    assert wpd.head(_wp5(b"x")).encryption == 0


def _fn42(text, endnote=False):
    """A 4.2 footnote (0xE2): def, a, b (number), c, d (lines), then for a footnote the old line count, one page's
    line count and the page count, for an endnote a null; 0xFF, the margins, the text, the closing 0xE2."""
    head = bytes((2 if endnote else 0, 0, 1, 0, 2)) + (b"\x00" if endnote else b"\x03\x02\x01")
    return b"\xe2" + head + b"\xff\x0a\x46" + text + b"\xe2"


def test_wp42_text_and_footnote():
    data = (b"\xc0\x00\x00\x00\x00\xc0" + b"Hello\x09world\x0dagain" + b"\x9dBold\x9c"
            + _fn42(b"Note\x0dtext") + b"\x0aNext" + b"\xe1\x82\xe1" + b"\xd1\x00\x00\xffhead\xff\x00\x01\xd1"
            + b" end" + _fn42(b"End", endnote=True) + b"\x0cLast")
    assert wpd.is_wp42(data)
    doc, ctx = _read(data)
    assert _plain(doc) == ["Hello world againBold", "Nexté end", "Last"], _plain(doc)
    assert doc.blocks[0].inlines[-2:] == [Bold([Text("Bold")]), NoteRef("fn-1")], doc.blocks[0].inlines
    assert doc.notes == {"fn-1": [Para([Text("Note text")])], "fn-2": [Para([Text("End")])]}, doc.notes
    assert ctx.skipped["header"] == 1 and set(ctx.skipped) == {"header"}, ctx.skipped
    # the 4.2 check: a group must occur, every group must close, no stray 0xFF
    assert not wpd.is_wp42(b"just plain text\r\n")
    assert not wpd.is_wp42(b"text \xc0\x00\x00\x00\x00\x99")  # a fixed group whose closing byte is wrong
    assert not wpd.is_wp42(b"text \xd1 never closed")
    assert not wpd.is_wp42(b"\xc0\x00\x00\x00\x00\xc0 and \xff")
    assert not wpd.is_wp42("café ok".encode("utf-8"))
    assert wpd.is_wp42(b"\xc0\x00\x00\x00\x00\xc0" + b"x" * wpd.WP42_PROBE + b"\xd1 open")  # past the probe
    long = b"x" * (wpd.WP42_PROBE - 3) + b"\xd1\x00\x00\x00"  # a group cut by the probe, the file going on
    assert wpd.is_wp42(long + b"\xd1rest")
    # a footnote without its 0xFF delimiter counts note; a variable group cut by the end ends the walk
    doc, ctx = _read(b"\xc0\x00\x00\x00\x00\xc0A\xe2\x00\x00\x01\x00\x01\x00\xe2B")
    assert _plain(doc) == ["AB"] and ctx.skipped["note"] == 1, ctx.skipped


def test_wp42_password():
    assert _refused(b"\xfe\xff\x61\x61\x12\x34rest", "encrypted")
    assert wpd.is_wp42(b"\xfe\xff\x61\x61\x00\x00")


class _Env:
    """Set environment variables for a test and restore them after; None removes one."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        import os
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        import os
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ole_perfectoffice_main():
    from reader_fix import make_cfb
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.wpd"
        make_cfb(p, {"PerfectOffice_MAIN": _wp6(b"Inside\x80the\xccwrapper")})
        doc = wpd.parse(p, Context())
        assert _plain(doc) == ["Inside the", "wrapper"], _plain(doc)
        # an encrypted stream in the wrapper is refused as the bare file would be
        make_cfb(p, {"PerfectOffice_MAIN": _wp6(b"x", encryption=1)})
        try:
            wpd.parse(p, Context())
            raise AssertionError("expected encrypted")
        except ReadError as e:
            assert e.kind == "encrypted"


def test_ole_without_main():
    from reader_fix import make_cfb, make_doc
    with tempfile.TemporaryDirectory() as d:
        p = make_doc(Path(d) / "w.wpd", [("word text\r", False)])
        doc = wpd.parse(p, Context())
        assert "word text" in str(doc.blocks), doc.blocks
        q = Path(d) / "o.wpd"
        make_cfb(q, {"Other": b"x" * 10})
        try:
            wpd.parse(q, Context())
            raise AssertionError("expected damaged")
        except ReadError as e:
            assert e.kind == "damaged" and e.detail == "no PerfectOffice_MAIN stream", e


def _mac():
    return _wp6(b"Mac text", ftype=0x2C, major=3)


def test_mac_without_switch():
    with _Env(PHRO_READER_SOFFICE=None):
        e = _refused(_mac(), "unsupported")
        assert e.detail == "Mac WordPerfect; set PHRO_READER_SOFFICE=1 to read it through LibreOffice", e.detail
        assert e.detail.endswith("LibreOffice")
        e = _refused(b"\x00\x01plain words, no groups", "unsupported")
        assert e.detail.startswith("not a WordPerfect 4.2 file") and e.detail.endswith("LibreOffice"), e.detail
    with _Env(PHRO_READER_SOFFICE=""):
        assert _refused(_mac(), "unsupported").detail.endswith("LibreOffice")


class _FakeProc:
    """A stand-in for the soffice child: `make` runs at start with the --outdir folder; wait() exits at once, or
    never when `hang`."""
    made = []

    def __init__(self, cmd, make=None, hang=False, **kw):
        self.cmd, self.kw, self.hang, self.pid = cmd, kw, hang, 4242
        self.waits, self.killed = [], 0
        self.outdir = Path(cmd[cmd.index("--outdir") + 1])
        _FakeProc.made.append(self)
        if make:
            make(self.outdir, Path(cmd[-1]))

    def wait(self, timeout=None):
        import subprocess
        self.waits.append(timeout)
        if isinstance(self.hang, BaseException):
            raise self.hang
        if self.hang:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return 0

    def poll(self):
        return None if self.hang else 0

    def kill(self):
        self.killed += 1


def _docx(text):
    from reader_fix import make_zip
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    return make_zip(None, {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>'),
        "word/document.xml": (f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
                              '</w:body></w:document>'),
    }, stored=())


def _with_fake(make=None, hang=False, deadline=None):
    """Run the Mac file through the switch with Popen faked; returns (doc or ReadError, the fake, kills). `hang` is
    True (wait times out) or an exception wait raises; `deadline` is the PHRO_READER_DEADLINE value, None unset."""
    import reader
    kills = []
    real_popen, real_kill, real_started = wpd.subprocess.Popen, wpd._kill_tree, reader.STARTED
    reader.STARTED = time.monotonic()  # the child's budget runs from the reader's start, as in a CLI child
    _FakeProc.made = []
    with tempfile.TemporaryDirectory() as d:
        exe = Path(d) / "soffice-stub"
        exe.write_bytes(b"")
        src = Path(d) / "mac.wpd"
        src.write_bytes(_mac())
        try:
            wpd.subprocess.Popen = lambda cmd, **kw: _FakeProc(cmd, make, hang, **kw)
            wpd._kill_tree = kills.append
            with _Env(PHRO_READER_SOFFICE=str(exe), PHRO_READER_DEADLINE=deadline):
                try:
                    got = wpd.parse(src, Context())
                except (ReadError, KeyboardInterrupt) as e:
                    got = e
        finally:
            wpd.subprocess.Popen, wpd._kill_tree, reader.STARTED = real_popen, real_kill, real_started
        fake = _FakeProc.made[0] if _FakeProc.made else None
        if fake is not None:
            assert fake.cmd[0] == str(exe) and fake.cmd[-1] == str(src.resolve()), fake.cmd
    return got, fake, kills


def test_soffice_deadline():
    # the gate's deadline, 2 seconds early, cuts the child's own budget
    got, fake, kills = _with_fake(hang=True, deadline=str(time.time() + 6))
    assert isinstance(got, ReadError) and got.kind == "timeout", got
    assert kills == [fake] and not fake.outdir.exists()
    assert 3 < fake.waits[0] <= 4, fake.waits
    # a deadline in the past (or under 2 seconds away) is no time: no child at all
    for gone in (time.time() - 100, time.time() + 1):
        got, fake, kills = _with_fake(hang=True, deadline=str(gone))
        assert isinstance(got, ReadError) and got.kind == "timeout" and fake is None and not kills, got
    # a missing or unparsable deadline is ignored: the reader's own budget
    for bad in (None, "", "soon", "nan", "inf"):
        got, fake, kills = _with_fake(hang=True, deadline=bad)
        assert got.kind == "timeout" and fake.waits[0] > limits.CONVERT_SECONDS - 5, (bad, fake.waits)


def test_soffice_interrupt_kills_child():
    # an interrupt while soffice runs: the tree is killed before the folder goes, the interrupt goes on
    got, fake, kills = _with_fake(hang=KeyboardInterrupt())
    assert isinstance(got, KeyboardInterrupt) and kills == [fake] and not fake.outdir.exists(), (got, kills)


def test_soffice_argument_absolute():
    import os
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        try:
            os.chdir(d)
            arg = wpd._argument("-x.wpd")
        finally:
            os.chdir(old)
    assert os.path.isabs(arg) and not arg.startswith("-") and arg.endswith("-x.wpd"), arg
    # the Windows extended-length prefix resolve may add is taken off
    sep = chr(92)
    long = sep * 2 + "?" + sep

    class _P:
        def __init__(self, s):
            self.s = s

        def resolve(self):
            return self.s

    real = wpd.Path
    try:
        wpd.Path = _P
        assert wpd._argument(long + "C:" + sep + "a.wpd") == "C:" + sep + "a.wpd"
        assert wpd._argument(long + "UNC" + sep + "srv" + sep + "a.wpd") == sep * 2 + "srv" + sep + "a.wpd"
        assert wpd._argument("C:" + sep + "b.wpd") == "C:" + sep + "b.wpd"
    finally:
        wpd.Path = real


def test_kill_tree_falls_back():
    import os

    class _R:
        def __init__(self, code):
            self.returncode = code

    class _Proc:
        pid = 4242

        def __init__(self, err=None):
            self.err, self.killed = err, 0

        def kill(self):
            self.killed += 1
            if self.err:
                raise self.err

    real_run = wpd.subprocess.run
    real_killpg = getattr(wpd.os, "killpg", None)

    def failing(*a, **k):
        raise OSError("no tree kill")

    try:
        if os.name == "nt":
            outcomes = (lambda *a, **k: _R(0), lambda *a, **k: _R(1), failing)
        else:
            outcomes = (lambda *a, **k: None, failing)
        for i, kill in enumerate(outcomes):
            if os.name == "nt":
                wpd.subprocess.run = kill
            else:
                wpd.os.killpg = kill
            p = _Proc()
            wpd._kill_tree(p)
            assert p.killed == (0 if i == 0 else 1), (i, p.killed)
        # the fallback's own error is raised, except that the child is gone already
        wpd._kill_tree(_Proc(ProcessLookupError()))
        try:
            wpd._kill_tree(_Proc(PermissionError("denied")))
            raise AssertionError("expected PermissionError")
        except PermissionError:
            pass
    finally:
        wpd.subprocess.run = real_run
        if real_killpg is not None:
            wpd.os.killpg = real_killpg


def test_mac_with_switch_stub():
    got, fake, kills = _with_fake(lambda out, src: (out / (src.stem + ".docx")).write_bytes(_docx("Converted text")))
    assert not isinstance(got, ReadError), got
    assert _plain(got) == ["Converted text"], _plain(got)
    assert not fake.outdir.exists() and not kills
    cmd = fake.cmd
    assert cmd[1:3] == ["--headless", "--norestore"] and cmd[3].startswith("-env:UserInstallation=file:///"), cmd
    assert cmd[3].endswith("/profile") and cmd[4:6] == ["--convert-to", "docx"], cmd
    assert ("creationflags" in fake.kw) if wpd.os.name == "nt" else fake.kw.get("start_new_session"), fake.kw


def test_soffice_timeout():
    got, fake, kills = _with_fake(hang=True)
    assert isinstance(got, ReadError) and got.kind == "timeout", got
    assert kills == [fake] and not fake.outdir.exists()


def test_soffice_no_docx():
    got, fake, kills = _with_fake()
    assert isinstance(got, ReadError) and got.kind == "damaged", got
    assert not fake.outdir.exists() and not kills


def test_soffice_lookup():
    import os
    with tempfile.TemporaryDirectory() as d:
        name = "soffice.exe" if os.name == "nt" else "soffice"
        exe = Path(d) / name
        exe.write_bytes(b"")
        exe.chmod(0o755)
        empty = Path(d) / "none"
        empty.mkdir()
        with _Env(PHRO_READER_SOFFICE="1", PATH=d, ProgramFiles=str(empty), **{"ProgramFiles(x86)": str(empty)}):
            got = wpd.soffice()
            assert got is not None and Path(got).resolve() == exe.resolve(), got
        with _Env(PHRO_READER_SOFFICE="1", PATH=str(empty), ProgramFiles=str(empty),
                  **{"ProgramFiles(x86)": str(empty)}):
            fixed = ("/usr/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")
            if not any(os.path.isfile(f) for f in fixed):
                assert wpd.soffice() is None
            # the switch on but no soffice found: the denial says so
            if wpd.soffice() is None:
                assert "no soffice" in _refused(_mac(), "unsupported").detail
            # a Program Files install is found
            prog = empty / "LibreOffice" / "program"
            prog.mkdir(parents=True)
            (prog / "soffice.exe").write_bytes(b"")
            if os.name == "nt":
                assert wpd.soffice() == str(prog / "soffice.exe"), wpd.soffice()
        with _Env(PHRO_READER_SOFFICE=str(exe)):
            assert wpd.soffice() == str(exe)
        with _Env(PHRO_READER_SOFFICE=None):
            assert wpd.soffice() is None
    # PATH is walked by hand: only absolute entries, never the current folder, only soffice.exe / .com on Windows
    # and an executable regular file named soffice elsewhere
    nt = os.name == "nt"
    with tempfile.TemporaryDirectory() as d:
        on = Path(d) / "on"
        cwd = Path(d) / "cwd"
        on.mkdir()
        cwd.mkdir()
        (on / "soffice.bat").write_bytes(b"")
        (on / "soffice.cmd").write_bytes(b"")
        (on / "soffice").write_bytes(b"")  # not executable: not taken on POSIX, not a name taken on Windows
        (cwd / ("soffice.exe" if nt else "soffice")).write_bytes(b"")
        (cwd / ("soffice.exe" if nt else "soffice")).chmod(0o755)
        old = os.getcwd()
        try:
            os.chdir(cwd)
            rel = os.path.join("..", "cwd")
            for path in (str(on), "", ".", rel, os.pathsep.join((".", str(on))), str(on) + os.pathsep):
                with _Env(PATH=path):
                    assert wpd._on_path() is None, (path, wpd._on_path())
            if nt:
                with _Env(PATH=chr(92) + "cwd"):  # rooted on the current drive only: not taken
                    assert wpd._on_path() is None
        finally:
            os.chdir(old)
        exe = on / ("soffice.exe" if nt else "soffice")
        if nt:
            exe.write_bytes(b"")
        else:
            exe.chmod(0o755)
        with _Env(PATH=os.pathsep.join((".", str(on)))):
            assert wpd._on_path() == str(exe), wpd._on_path()
        if nt:
            exe.unlink()
            (on / "soffice.com").write_bytes(b"")
            with _Env(PATH=str(on)):
                assert wpd._on_path() == str(on / "soffice.com"), wpd._on_path()
        # "1" takes what PATH gives
        with _Env(PHRO_READER_SOFFICE="1", PATH=str(on)):
            assert wpd.soffice() == wpd._on_path() is not None
    with _Env(PHRO_READER_SOFFICE=""):
        assert wpd.soffice() is None


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
