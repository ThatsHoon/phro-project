"""WordPerfect to the document model: the `FF 'WPC'` head and its refusals, and the WordPerfect 6+ (6 to X9) prefix
index and body walk.

Format knowledge, not code: libwpd 0.10.3 (MPL-2.0 / LGPL-2.1+; never copied) gives the offsets and function codes:
the head (`WPXHeader.cpp`, `WP6Header.cpp`), the prefix index and its 14-byte entries (`WP6PrefixIndice.cpp`), the
summary packet 0x12 (`WP6ExtendedDocumentSummaryPacket.cpp`), the byte classes of the body (`WP6Parser.cpp`), the
single-byte functions (`WP6FileStructure.h`), the group checks and their one-byte resync (`WP6Part.cpp`,
`WP6VariableLengthGroup.cpp`, `WP6FixedLengthGroup.cpp`) and the undo, attribute and extended-character groups. The
Corel WordPerfect SDK sample was read to check offsets only. The walk's outline (text bytes, the 0x01-0x20 extended
international characters, single-byte functions, variable groups 0xD0-0xEF, fixed groups 0xF0-0xFE, the undo state
between F1 type 0 and type 1) follows Apache Tika (Apache-2.0) `wordperfect/WP6DocumentAreaExtractor.java` at main
commit a40d54f6; `_INTL` is Tika's `DEFAULT_EXTENDED_INTL_CHARS` (`WP6Charsets.java`, tag 4.2.0), which matches libwpd's
table. The extended character sets are `wpd_charsets` (Tika's tables). Stdlib only.

Head. `FF 'WPC'`, u32 document pointer, product type, file type, major and minor version, u16 encryption word; at 14
(WP6+) the u16 index header offset, 16 when less. The refusals run in this order: an encryption word not 0 is
`encrypted`; file type 0x16 (a WPG picture) and 0x2C (Mac WordPerfect, `_mac`) are `unsupported`; a product type
other than 1, or a major version other than 0 (WP5, `_wp5`) or 2 (WP6+), is `unsupported`. A document pointer inside
the head or past the end of the file is `damaged`, as is a body that gives no block. Offset 20 (the 6.1 document size)
is never read. A file without the head goes to `_headless`.

Prefix index (`_prefix6`). The entry count is at `ih + 2`, the entries (flags, type, use count, hide count, u32 size,
u32 offset) from `ih + 14`, 14 bytes each; entry 0 is the index header itself. Entries are read while they lie inside
the file (an index cut by the end is counted `record` once); an entry whose range is not inside the file is skipped
and counted `record`. The title is field 17 of the first summary packet (0x12): groups of [u16 length][u16 tag][2
bytes][name][value], name and value u16 characters ended by 0, each a code (low byte) in a set (high byte). It is
cut at `MAX_TITLE` characters after its white space is collapsed.

Body (`_walk6`), from the document pointer to the end of the file:
* 0x00-0x7F are taken as whole runs (found with one `bytes.translate` of the body, no regex): 0x00 is nothing,
  0x01-0x20 are the extended international characters, 0x21-0x7F are ASCII.
* Single bytes: 0x80, 0xBA-0xBC and 0xCD-0xCF (soft spaces and soft EOLs) are a space, 0x81 a no-break space, 0x84 a
  hyphen. 0x87, 0xB4-0xB9 and 0xC7-0xCC (hard EOLs, column and page breaks) end a paragraph; so do the table codes
  0xBD-0xC6 until tables are read. The others are nothing.
* Variable groups 0xD0-0xEF: [id][sub][u16 size] ... [u16 size][id], the size counting the whole group. A group passes
  when its size is at least 7, it lies inside the data, and it ends with the same size and id. D0 (end of line):
  subfunctions 1-3 and 0x14-0x16 are a space; 4-9, 0x0A-0x13 and 0x17-0x1C end a paragraph. E0 (tab) is a space.
* Fixed groups 0xF0-0xFE have the sizes of `_FIXED` and end with their id. F0 [code][set] is an extended character
  (`wpd_charsets`; set 0 is ASCII from 0x20, as libwpd reads it); an unmapped one, set 12 included, is counted
  `glyphs` and gives nothing. F1 [type][u16 level] is undo: type 0 starts deleted text, type 1 ends it, and deleted
  text is `Hidden(kind="deleted")`, running to the end of the stream when it has no end. Inside it a paragraph end
  is a space of the deleted text and an attribute change does nothing (libwpd drops both there). F2/F3 [attr] turn an
  attribute on and off: 12 is bold; 0-16 are the attributes WordPerfect 6+ is known to have (redline 10 and
  strikeout 13 are plain text); a number above 16 is counted under the internal kind `attribute`, the first one
  adding the warning.
* 0xFF is nothing.
* Resync: a group that fails its check consumes one byte and the walk goes on from the next. The failures of one
  run count `record` once: a failure right after another, or inside the extent a failed group declared (its own
  bytes, read again as text and codes), is the same run. A group cut by the end of the data, outside such an
  extent, ends the walk: the text before it is kept and `record` is counted once.

Stories. `_Story` keeps one story's paragraphs (the body here), the way `rtf.py` keeps one per story: text arrives
in runs under the bold and hidden state, and a paragraph end turns them into a Para with its edges trimmed. A
paragraph with no text is dropped.
"""
import struct
from dataclasses import dataclass

from ..limits import ReadError
from ..model import Bold, Doc, Hidden, Para, Text
from .wpd_charsets import char

MAX_TITLE = 500          # characters of the wpd title

_MAGIC = b"\xffWPC"
_HEAD = 16
_WPG, _MAC = 0x16, 0x2C
_SUMMARY = 0x12
_TITLE_TAG = 17
_BOLD = 12
_MAX_ATTR = 16           # blink, the last attribute libwpd and Tika know for 6+
# the sizes of the fixed groups 0xF0-0xFE, closing byte included
_FIXED = (4, 5, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 8, 8)
_MIN_VAR = 7             # [id][sub][u16 size] ... [u16 size][id]
# text bytes (0x00-0x7F) are 1, function bytes 0: `find(b"\0")` ends a run
_CLASS = bytes(1 if i < 0x80 else 0 for i in range(256))
_INTL = {0: None, **{i + 1: c for i, c in enumerate(
    "åÅæÆäÄáàâãÃçÇëéÉ"
    "èêíñÑøØõÕöÖüÜúùß")}}
_SPACE_BYTES = frozenset((0x80, 0xBA, 0xBB, 0xBC, 0xCD, 0xCE, 0xCF))
_PARA_BYTES = frozenset((0x87, *range(0xB4, 0xBA), *range(0xBD, 0xCD)))
_SINGLE = {**{b: " " for b in _SPACE_BYTES}, 0x81: " ", 0x84: "-"}
_EOL_SPACE = frozenset((1, 2, 3, 0x14, 0x15, 0x16))
_EOL_PARA = frozenset((*range(4, 0x14), *range(0x17, 0x1D)))
_ATTR_WARNING = "attribute: unknown WordPerfect attribute number"


@dataclass(frozen=True)
class Head:
    pointer: int
    product: int
    ftype: int
    major: int
    minor: int
    encryption: int


def head(data: bytes) -> Head | None:
    """The `FF 'WPC'` head, or None when the file has none (or is shorter than one)."""
    if len(data) < _HEAD or data[:4] != _MAGIC:
        return None
    pointer, product, ftype, major, minor, encryption = struct.unpack_from("<IBBBBH", data, 4)
    return Head(pointer, product, ftype, major, minor, encryption)


# ---- stories

class _Story:
    """One story's paragraphs: (hidden kind, bold, text) runs until a paragraph end makes them a Para."""
    __slots__ = ("kind", "_bold", "_hid", "_runs", "_blocks")

    def __init__(self, kind="body"):
        self.kind = kind
        self._bold = False
        self._hid = ""
        self._runs = []
        self._blocks = []

    def text(self, s):
        if s:
            self._runs.append((self._hid, self._bold, s))

    def bold(self, on):
        self._bold = bool(on)

    def hidden(self, kind):
        """Start hidden text of `kind` ("deleted"), or end it with ""."""
        self._hid = kind

    def end_para(self):
        if self._hid:  # a paragraph end inside deleted text is deleted with it
            self.text(" ")
            return
        self._flush()

    def blocks(self) -> list:
        self._flush()
        return self._blocks

    def _flush(self):
        runs, self._runs = self._runs, []
        _trim(runs)
        if runs:
            self._blocks.append(Para(_inlines(runs)))


def _trim(runs):
    """Strip the white space at the paragraph's two edges, in place."""
    k = 0
    while k < len(runs):
        h, b, t = runs[k]
        t = t.lstrip()
        if t:
            runs[k] = (h, b, t)
            break
        k += 1
    del runs[:k]
    k = len(runs)
    while k:
        h, b, t = runs[k - 1]
        t = t.rstrip()
        if t:
            runs[k - 1] = (h, b, t)
            break
        k -= 1
    del runs[k:]


def _inlines(runs) -> list:
    """Adjacent runs of one look merge; consecutive hidden runs of one kind are one Hidden."""
    out, cur = [], None  # cur: [hidden, bold, parts]
    for h, b, t in runs:
        if cur is not None and cur[0] == h and cur[1] == b:
            cur[2].append(t)
            continue
        if cur is not None:
            _put(out, cur)
        cur = [h, b, [t]]
    if cur is not None:
        _put(out, cur)
    return out


def _put(out, run):
    h, b, parts = run
    inl = Text("".join(parts))
    if b:
        inl = Bold([inl])
    if not h:
        out.append(inl)
    elif out and isinstance(out[-1], Hidden) and out[-1].kind == h:
        out[-1].inlines.append(inl)
    else:
        out.append(Hidden([inl], h))


# ---- characters

def _wpchar(code, cset) -> str | None:
    """An extended character: set 0 is ASCII from 0x20 (libwpd), the other sets are `wpd_charsets`."""
    if cset == 0:
        return char(0, code - 0x20) if code >= 0x20 else None
    return char(cset, code)


def _attribute(ctx):
    ctx.skipped["attribute"] += 1
    if ctx.skipped["attribute"] == 1:
        ctx.warnings.append(_ATTR_WARNING)


# ---- the WP6+ prefix index

def _prefix6(data, ctx) -> dict:
    """Packet type -> [(id, offset, size)], ids counted from 1 as the index numbers them."""
    n = len(data)
    ih = struct.unpack_from("<H", data, 14)[0] if n >= _HEAD else 0
    ih = max(ih, 16)
    if ih + 4 > n:
        ctx.skipped["record"] += 1  # the index header is past the end of the file
        return {}
    count = struct.unpack_from("<H", data, ih + 2)[0]
    out = {}
    for i in range(1, count):  # count is a u16: at most 65,534 entries, each inside the file
        at = ih + 14 * i
        if at + 14 > n:
            ctx.skipped["record"] += 1  # the index is cut by the end of the file
            break
        _flags, ptype, _use, _hide, size, offset = struct.unpack_from("<BBHHII", data, at)
        if offset + size > n:
            ctx.skipped["record"] += 1
            continue
        out.setdefault(ptype, []).append((i, offset, size))
    return out


def _title6(data, packets) -> str:
    """Field 17 of the first summary packet, white space collapsed, cut at MAX_TITLE."""
    if not packets.get(_SUMMARY):
        return ""
    _, off, size = packets[_SUMMARY][0]
    end = off + size
    i = off
    while i + 6 <= end:  # each group is at least 6 bytes long, so the walk is bounded by the packet size
        glen, tag = struct.unpack_from("<HH", data, i)
        if glen < 6 or i + glen > end:
            break
        gend = i + glen
        j = _skip_chars(data, i + 6, gend)  # the field's name
        if tag == _TITLE_TAG:
            text = _chars(data, j, gend)
            return " ".join(text.split())[:MAX_TITLE]
        i = gend
    return ""


def _skip_chars(data, j, end):
    while j + 2 <= end:
        j += 2
        if data[j - 2] == 0 and data[j - 1] == 0:
            break
    return j


def _chars(data, j, end) -> str:
    out = []
    while j + 2 <= end and len(out) < MAX_TITLE * 4:
        code, cset = data[j], data[j + 1]
        j += 2
        if code == 0 and cset == 0:
            break
        c = _wpchar(code, cset)
        if c:
            out.append(c)
    return "".join(out)


# ---- the WP6+ body walk

def _walk6(data, start, end, story, ctx):
    """Walk the function codes of data[start:end] into `story` (see the module docstring)."""
    skipped = ctx.skipped
    cls = data[start:end].translate(_CLASS)
    pos = start
    undo = False
    last_fail = -2   # where the last failed check was
    junk_end = -1    # the end of the extent the failed groups of the run declared
    while pos < end:
        b = data[pos]
        if b < 0x80:
            stop = cls.find(b"\x00", pos - start)
            stop = end if stop < 0 else stop + start
            story.text(data[pos:stop].decode("latin-1").translate(_INTL))
            pos = stop
            continue
        if b < 0xD0:
            if b in _PARA_BYTES:
                story.end_para()
            else:
                story.text(_SINGLE.get(b, ""))
            pos += 1
            continue
        if b == 0xFF:
            pos += 1
            continue
        size, extent = _check(data, pos, end, b)
        if size is None:
            if pos >= junk_end:
                skipped["record"] += 1  # cut by the end of the data: the walk ends here
                break
            size, extent = 0, 1  # only bytes of a group that failed: resync as for a failure
        if not size:  # failed its check: consume one byte
            if pos != last_fail + 1 and pos >= junk_end:
                skipped["record"] += 1  # a new run of failures
            junk_end = max(junk_end, pos + extent)
            last_fail = pos
            pos += 1
            continue
        if b < 0xF0:
            _variable(b, data[pos + 1], story)
        else:
            undo = _fixed(b, data, pos, story, undo, ctx)
        pos += size
    story.hidden("")


def _check(data, pos, end, b):
    """The group at pos, id b: (size, size) when it passes; (0, extent) when it fails its check, the extent being the
    bytes it declared (1 when it declared none it could hold); (None, 0) when it is cut by the end of the data."""
    if b < 0xF0:
        if pos + 4 > end:
            return None, 0
        size = struct.unpack_from("<H", data, pos + 2)[0]
        if size < _MIN_VAR:
            return 0, 1
    else:
        size = _FIXED[b - 0xF0]
    if pos + size > end:
        return None, 0
    if data[pos + size - 1] != b or (b < 0xF0 and struct.unpack_from("<H", data, pos + size - 3)[0] != size):
        return 0, size
    return size, size


def _variable(b, sub, story):
    if b == 0xD0:
        if sub in _EOL_SPACE:
            story.text(" ")
        elif sub in _EOL_PARA:
            story.end_para()
    elif b == 0xE0:
        story.text(" ")


def _fixed(b, data, pos, story, undo, ctx) -> bool:
    """One fixed group that passed its check; returns the undo state after it."""
    if b == 0xF0:
        c = _wpchar(data[pos + 1], data[pos + 2])
        if c is None:
            ctx.skipped["glyphs"] += 1
        else:
            story.text(c)
    elif b == 0xF1:
        kind = data[pos + 1]
        if kind == 0:
            undo = True
            story.hidden("deleted")
        elif kind == 1:
            undo = False
            story.hidden("")
    elif b in (0xF2, 0xF3):
        attr = data[pos + 1]
        if attr > _MAX_ATTR:
            _attribute(ctx)
        elif attr == _BOLD and not undo:
            story.bold(b == 0xF2)
    return undo


# ---- the entries

def _headless(data, ctx, path):
    """A file without the `FF 'WPC'` head (Task 9 adds the 4.2 check and the LibreOffice switch here)."""
    raise ReadError("unsupported", "not a WordPerfect 5 or later file")


def _mac(data, h, ctx, path):
    """Mac WordPerfect, file type 0x2C (Task 9 adds the LibreOffice switch here)."""
    raise ReadError("unsupported", "Mac WordPerfect")


def _wp5(data, h, ctx, path):
    """WordPerfect 5.x, major version 0 (Task 9 reads it here)."""
    raise ReadError("unsupported", "WordPerfect 5.x is not read yet")


def _wp6(data, h, ctx) -> Doc:
    n = len(data)
    if h.pointer < _HEAD:
        raise ReadError("damaged", "the WordPerfect document pointer is inside the head")
    if h.pointer > n:
        raise ReadError("damaged", "the WordPerfect document pointer is past the end of the file")
    packets = _prefix6(data, ctx)
    doc = Doc()
    with ctx.guard("record"):
        doc.title = _title6(data, packets)
    story = _Story("body")
    _walk6(data, h.pointer, n, story, ctx)
    doc.blocks = story.blocks()
    if not doc.blocks:
        raise ReadError("damaged", "no WordPerfect body")
    return doc


def parse_bytes(data: bytes, ctx, *, path=None) -> Doc:
    """Read a WordPerfect file held in memory. `path` is only for the LibreOffice switch."""
    data = bytes(data)
    h = head(data)
    if h is None:
        if data[:4] == _MAGIC:
            raise ReadError("damaged", "the WordPerfect head is cut short")
        return _headless(data, ctx, path)
    if h.encryption:
        raise ReadError("encrypted", "WordPerfect password")
    if h.ftype == _WPG:
        raise ReadError("unsupported", "WordPerfect graphic (WPG)")
    if h.ftype == _MAC:
        return _mac(data, h, ctx, path)
    if h.product != 1 or h.major not in (0, 2):
        raise ReadError("unsupported", f"WordPerfect version {h.major}.{h.minor}, product type {h.product}")
    if h.major == 0:
        return _wp5(data, h, ctx, path)
    return _wp6(data, h, ctx)


def parse(path, ctx) -> Doc:
    with open(path, "rb") as fh:
        data = fh.read()
    return parse_bytes(data, ctx, path=path)
