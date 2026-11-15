"""WordPerfect to the document model: the compound-file wrapper, the `FF 'WPC'` head and its refusals, the WordPerfect
6+ (6 to X9) prefix index and body walk, WordPerfect 5.x, the headerless 4.2, and the opt-in LibreOffice switch for
the files the native reader refuses (Mac WordPerfect).

Format knowledge, not code: libwpd 0.10.3 (MPL-2.0 / LGPL-2.1+; never copied) gives the offsets and function codes:
the head (`WPXHeader.cpp`, `WP6Header.cpp`), the prefix index and its 14-byte entries (`WP6PrefixIndice.cpp`), the
summary packet 0x12 (`WP6ExtendedDocumentSummaryPacket.cpp`), the byte classes of the body (`WP6Parser.cpp`), the
single-byte functions (`WP6FileStructure.h`), the group checks and their one-byte resync (`WP6Part.cpp`,
`WP6VariableLengthGroup.cpp`, `WP6FixedLengthGroup.cpp`) and the undo, attribute and extended-character groups; for
WP5 the parser, the group checks and sizes (`WP5Parser.cpp`, `WP5Part.cpp`, `WP5FileStructure.*`,
`WP5VariableLengthGroup.cpp`, `WP5FixedLengthGroup.cpp`), the index blocks (`WP5PrefixData.cpp`,
`WP5SpecialHeaderIndex.cpp`, `WP5GeneralPacketIndex.cpp`), the picture packet and its missing WPG head
(`WP5GraphicsInformationPacket.cpp`) and the note, header, box and table groups; for 4.2 the size table and the check
(`WP42FileStructure.cpp`, `WP42Heuristics.cpp`, `WP42Parser.cpp`); the compound wrapper (`WPDocument.cpp`). Corel's
WordPerfect 4.2 format document (`wp42ff.txt`, read on the web) gives the 4.2 footnote layout. The Corel WordPerfect
SDK sample was read to check offsets only, and the spec takes the WP5 comment from it. The WP6+ walk's outline (text
bytes, the 0x01-0x20 extended international characters, single-byte functions, variable groups 0xD0-0xEF, fixed
groups 0xF0-0xFE, the undo state between F1 type 0 and type 1) follows Apache Tika (Apache-2.0) `wordperfect/WP6DocumentAreaExtractor.java` at main
commit a40d54f6; `_INTL` is Tika's `DEFAULT_EXTENDED_INTL_CHARS` (`WP6Charsets.java`, tag 4.2.0), which matches libwpd's
table. The extended character sets are `wpd_charsets` (Tika's tables). Stdlib only.

Wrapper (`parse`). A compound file reads its `PerfectOffice_MAIN` stream as the file; one without it goes to
`cfb_route.route(cf, "wpd", "no PerfectOffice_MAIN stream")`, which hands a Word, Excel, PowerPoint or hwp compound
file to its own parser.

Head. `FF 'WPC'`, u32 document pointer, product type, file type, major and minor version, u16 encryption word (WP5
keeps it in the other byte order, as libwpd reads it); at 14 (WP6+) the u16 index header offset, 16 when less. The
refusals run in this order: an encryption word not 0 is `encrypted`; file type 0x16 (a WPG picture) is
`unsupported`; 0x2C (Mac WordPerfect, `_mac`) goes to the LibreOffice switch; a product type other than 1, or a major
version other than 0 (WP5, `_wp5`) or 2 (WP6+), is `unsupported`. A document pointer inside the head or past the end
of the file is `damaged`, as is a body that gives no block. Offset 20 (the 6.1 document size) is never read. A file
without the head goes to `_headless`: a 4.2 password head `FE FF 61 61` is `encrypted`, a file `is_wp42` takes is read
as 4.2, any other goes to the LibreOffice switch.

Prefix index (`_prefix6`). The entry count is at `ih + 2`, the entries (flags, type, use count, hide count, u32 size,
u32 offset) from `ih + 14`, 14 bytes each; entry 0 is the index header itself. Entries are read while they lie inside
the file (an index cut by the end is counted `record` once); an entry whose range is not inside the file is skipped
and counted `record`. The title is field 17 of the first summary packet (0x12): groups of [u16 length][u16 tag][2
bytes][name][value], name and value u16 characters ended by 0, each a code (low byte) in a set (high byte). It is
cut at `MAX_TITLE` characters after its white space is collapsed.

Body (`_walk6`), from the document pointer to the end of the file (and each sub-document's stream):
* 0x00-0x7F are taken as whole runs (found with one `bytes.translate` of the body, no regex): 0x00 and 0x7F (DEL) are
  nothing, 0x01-0x20 are the extended international characters, 0x21-0x7E are ASCII.
* Single bytes: 0x80, 0xBA-0xBC and 0xCD-0xCF (soft spaces and soft EOLs) are a space, 0x81 a no-break space, 0x84 a
  hyphen. 0x87, 0xB4-0xB9 and 0xC7-0xCC (hard EOLs, column and page breaks) end a paragraph. The table codes are
  0xBD-0xBF (table off), 0xC0-0xC5 (row) and 0xC6 (cell); outside a table they end a paragraph. The others are
  nothing.
* Variable groups 0xD0-0xEF: [id][sub][u16 size] ... [u16 size][id], the size counting the whole group. A group passes
  when its size is at least 7, it lies inside the data, and it ends with the same size and id. Its head, read only
  for the groups below, is [flags] ([n][u16 prefix id] * n when flags has 0x80) [u16 non-deletable size], then the
  contents; a head that does not fit the group, or a non-deletable size past it (or with bit 0x8000), is no head.
  D0 (end of line): subfunctions 1-3 and 0x14-0x16 are a space; 4-9 and 0x17-0x1C end a paragraph; 0x0A is a table
  cell, 0x0B-0x10 a row and its first cell, 0x11-0x13 the table off (outside a table they end a paragraph). E0 (tab)
  is a space.
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

Stories. `_Story` keeps one story's paragraphs (the body, a note, a header or a text box), the way `rtf.py` keeps one
per story: text arrives in runs under the bold and hidden state, and a paragraph end turns them into a Para with its
edges trimmed. A paragraph with no text is dropped. Blocks put in place (a text box, a picture) end the paragraph
they come in.

Sub-documents (`_subdoc`). Notes, comments, headers, footers, watermarks and text boxes keep their text in a 0x08
packet: [u16 block count][4 bytes][u32 size] * count, then the blocks, which follow one another, so the packet's
stream is one range of the file, walked by `_walk6` as a story of its own (a count of 0 is a story with no text; a
size table or a total past the packet is unreadable, counted under the reference's kind). Every reference walk of one
document shares one map of packet ids: a packet being walked is a cycle, and a reference deeper than
`limits.MAX_DEPTH`, are counted `depth` and give nothing, the outer text kept. A packet's blocks are emitted at most
once a document, so nested references cannot multiply the output: a later reference to a note's or comment's packet
is a NoteRef to the same note (not counted), to a text box's packet, or to a picture's 0x6F packet, gives nothing and
counts `depth`, and to a header's, footer's or watermark's packet gives nothing, uncounted (the line would be
deduplicated anyway). Past `WPD_SUBDOCS` references, repeated ones included, the rest are counted `note`, one each.
Inside deleted text (undo) notes, comments, headers and boxes are left out with it, as libwpd does. A story walked
from a header, footer or watermark, at any depth (a text box in one), is inside the header: the line rules below hold
in it.
* Footnotes and endnotes: D7 sub 0 or 2 (on), its first prefix id the text packet; endnotes are footnotes. The text
  up to D7 sub 1 or 3 (off), or to the paragraph's end, is the number the body shows and is dropped. The note goes
  into `Doc.notes` as `fn-N` with a `NoteRef` in place; a note with no text gives no mark.
* Comments: D4 sub 0x1D, the first of its prefix ids that is a 0x1B packet, [u16 count, 1][u16 text packet id][flags],
  read as a note through `word_fields.comment_note(blocks, "")` ("Comment: "), `cm-N`. Any other 0x1B, or none, counts
  `note`.
* Headers, footers and watermarks: D6 sub 0-5 (header A, B, footer A, B, watermark A, B), the first prefix id. Each
  is one line of its blocks' text (a table's cells one after another), a watermark a header line, deduplicated.
  A note or comment in one cannot stand on the line: it is counted `note`, and a picture `image`.
* Boxes: DF sub 0-2 (anchored to a character, paragraph or page; a graphics rule, 3, is nothing; a box with flags bit
  0x40 is ignored, as libwpd does). The content kind comes from the first 0x41 packet among its prefix ids ([u16 n]
  [u16 id] * n [u16 box data size], then the name and library section [u16 size][byte][flags][s16 name length], the
  counter and positioning sections skipped by their u16 sizes, and the content section [u16 size][byte][kind]); the
  box's own content data overrides it (14 reserved bytes, two u16 sizes, u16 override flags, then the counter 0x8000
  and positioning 0x4000 sections skipped by their sizes, and the content 0x2000 section [u16 size][u16 flags], its
  kind after 2 bytes more when those flags have 0x8000, when they have 0x4000). A style whose name length is -4 is an
  equation box: counted `object`. A box with a 0x07 (hyperlink) packet among its ids counts `link`. Kind 1 is a text
  box: its first 0x08 packet's paragraphs in place (none: `shape`). A box of any other kind that names a 0x08 packet
  counts `shape`, its text unread. Kind 3 is a picture: its first 0x40 packet, with
  child ids ([u16 n][u16 id] * n) only when its index entry has flags bit 0x01, names 0x6F packets whose bytes are each
  an `Image` in place, WPG or not, charged against `limits.IMAGE_BYTES` (past it, or a picture missing, `image`).

Tables. D4 sub 0x2A (table definition on) opens a table in the story; 0x2B and 0x2C (definition off, columns) are
nothing. Then each row code starts a row and each row or cell code a cell; the cell's paragraphs go into it; the
table off code, the next table on, or the story's end, closes the table in place. A row or cell group's non-deletable
data, after [u16 size] deletable bytes, is a list of sub-functions, each [id] and its bytes (0x80 5, 0x82 and 0x83 4,
0x84 9, 0x85 4, 0x86 10, 0x87 and 0x88 6, 0x89 11, 0x8B and 0x8C 3, 0x8D 1; 0x81, 0x8E and 0x8F [u16 size]); an unknown
one, or one past the group, ends the list. 0x80 [flags]: bit 0x04 makes the row a header row. 0x85 [columns][rows]:
the cell's spans; 128 columns or more is a cell bound to the one above: it is no cell, its text joining the origin
cell that covers its column (its width the low seven bits, at least 1); with no cell above it is a plain cell one
column wide at the column it is at. Rows
are made on their first cell, and a row that gets none is left out. Cells are charged against the document's
`limits.TABLE_NODES`, shared by every table, and a row stops at `limits.GRID_COLS` columns; the cells past either are
dropped with their text and counted `cells` once a table. A table whose codes fail is dropped, counted `table`. In
deleted text a table code ends a paragraph of the deleted text.

WordPerfect 5.x (`_wp5`). The prefix (`_prefix5`) is a chain of 0xFFFB index blocks from offset 16 (read when the
document pointer leaves room for one): [u16 0xFFFB][u16 5][u16 50][u32 next], then four indexes [u16 type][u32 size]
[u32 offset]; type 0 or 0xFFFF is an empty slot, and the first packet of a type whose range is inside the file is
kept. A block past the end of the file, one whose head is not (0xFFFB, 5, 50), a type above 0x2FF (below 0xFFFB), a
next pointer to a block read already (a visited-offset set), or more than `WP5_BLOCKS` blocks, ends the chain,
counted `record` once; a packet whose range is outside the file is skipped, counted `record`. Packet 8 (`_gfx5`)
holds the pictures: [u16 count][u32 size] * count, then the bytes one after another. The body (`_walk5`) runs from
the document pointer to the end of the file:
* 0x20-0x7E are text, taken as whole runs. 0x0A, 0x0C (a page break), 0x8C, 0x90 and 0x99 end a paragraph; 0x0B,
  0x0D and 0x93-0x95 are a space, 0xA0 a no-break space, 0xA9-0xAB a hyphen; other bytes below 0xC0, and 0xFF, are
  nothing.
* Fixed groups 0xC0-0xCF have the sizes of `_FIXED5` and end with their id; variable groups 0xD0-0xFE are
  [id][sub][u16 len] ... [u16 len][sub][id], len the whole size minus 4, and pass when both copies agree. Failures,
  cuts and their `record` counts follow the WP6+ rules above.
* C0 [char][set] is an extended character (`wpd_charsets`, WP5's sets; unmapped: `glyphs`); C1 (tab) and C2 (indent)
  are a space; C3/C4 [attr] turn an attribute on and off, 12 bold, above 16 counted `attribute`.
* A sub-document's text is inside its group, walked as a story of its own (each counts toward `WPD_SUBDOCS`; one
  deeper than `limits.MAX_DEPTH` is `depth`). D6 sub 0 (footnote) and 1 (endnote): flags, u16 number, then for a
  footnote the page count n and 2 * (n + 1) + 9 bytes, for an endnote 4 bytes; the rest is the note, `fn-N`. D9 sub 2
  (comment): a u16 screen column and a u8 line count, then the text, a "Comment: " note `cm-N`; D9 sub 7 is skipped.
  D5 sub 0-3 (header A, B, footer A, B): 7 bytes, the occurrence byte (0: none), 10 bytes, then the text, one line
  each as for WP6+. A note or comment in a header is counted `note`.
* DA sub 0 (figure): the box type at group offset 52; type 0x80 gives the u16 picture number at 113, a picture of
  packet 8, an `Image` in place once a document (a later reference counts `depth`), libwpd's 16-byte WPG head put
  before bytes that have none, charged against `limits.IMAGE_BYTES` (past it, a picture missing or another box type:
  `image`). DA sub 1-3 (table, text and user boxes) count `shape`, sub 4 (equation) `object`.
* Tables: D2 sub 0x0B (table definition) opens one; DC sub 0 [flags][column][columns, 0x80 bound to the cell above]
  [rows] is a cell; DC sub 1 and DD sub 1 and 3 start a row; DC and DD sub 2 close the table.

WordPerfect 4.2 (`is_wp42`, `_walk42`). No head. The check scans the first `WP42_PROBE` bytes: every byte 0xC0-0xFE
is a group, closed by the byte its size (`_SIZE42`) puts at its end or, for a size of -1, by the next copy of itself;
at least one fixed-size group (variable groups alone pair up in cp1252 text: two é), and no 0xFF outside one. The walk takes 0x20-0x7E as text, 0x09, 0x0B and 0x0D as a space, 0x0A
and 0x0C as a paragraph end, 0x9D/0x9C as bold on and off; E1 [char] is a code page 437 character; E2 is a footnote
or endnote: [def][a][b][c][d] and line and page counts up to 0xFF, the two margins, then the note's text up to the
closing E2 (no 0xFF: counted `note`); D1 (a header or footer) is counted `header`; any other group is skipped by its
size or closing byte, a variable group holding two words counted `record`. A fixed group with a wrong closing byte resyncs as for WP6+; a group cut by the end ends the
walk, counted `record`.

LibreOffice switch (`_switch`). Mac WordPerfect and a headerless file the 4.2 check refuses are read through
LibreOffice only when `PHRO_READER_SOFFICE` is set (`soffice`; "1" walks the absolute PATH entries, never the
current folder, `_on_path`); else they are `unsupported`, the detail naming the switch. `_via_soffice` runs `soffice
--headless --norestore -env:UserInstallation=<tmp>/profile --convert-to docx --outdir <tmp> <absolute file>` in a
process group of its own, with CONVERT_SECONDS - elapsed - 3 seconds from the reader's start and no later than 2
seconds before the gate's `PHRO_READER_DEADLINE`; past them the tree is killed (`_kill_tree`) and the read is a
`timeout` (taskkill by its absolute path under SystemRoot). The docx it writes is read by `ooxml.parse`; none is `damaged`. The temporary folder is removed whatever
happens, a child still running killed first.
"""
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .. import limits
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Cell, Doc, Hidden, Image, NoteRef, Para, Table, Text
from .wpd_charsets import char
from . import cfb_route
from .word_fields import comment_note

# The body walk has no cap of its own (CONVERT_SECONDS bounds it). Measured 2026-11-15 on the development machine
# (`test_reader_wpd.py measure body`): `_walk6` over a 50.0 MB body of text runs, bold groups and hard returns takes 6.31 s.
MAX_TITLE = 500          # characters of the wpd title
WPD_SUBDOCS = 10_000     # sub-documents (notes, comments, headers, footers, watermarks, text boxes) read a document
WP42_PROBE = 65_536      # bytes the 4.2 check scans
WP5_BLOCKS = 1_024       # WP5 0xFFFB index blocks read
TASKKILL_SECONDS = 1     # the tree kill's wait; with `_reap`'s 1-second wait it stays in the 2-second deadline margin

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
_INTL = {0: None, 0x7F: None, **{i + 1: c for i, c in enumerate(
    "åÅæÆäÄáàâãÃçÇëéÉ"
    "èêíñÑøØõÕöÖüÜúùß")}}
_SPACE_BYTES = frozenset((0x80, 0xBA, 0xBB, 0xBC, 0xCD, 0xCE, 0xCF))
_PARA_BYTES = frozenset((0x87, *range(0xB4, 0xBA), *range(0xBD, 0xCD)))
_SINGLE = {**{b: " " for b in _SPACE_BYTES}, 0x81: " ", 0x84: "-"}
_EOL_SPACE = frozenset((1, 2, 3, 0x14, 0x15, 0x16))
_EOL_PARA = frozenset((*range(4, 0x14), *range(0x17, 0x1D)))
_ATTR_WARNING = "attribute: unknown WordPerfect attribute number"
# packet types
_LINK, _TEXT, _COMMENT, _GFX_NAME, _BOX_STYLE, _GFX_DATA = 0x07, 0x08, 0x1B, 0x40, 0x41, 0x6F
_PREFIX_BIT = 0x80       # a variable group's flags: prefix ids follow
_IGNORE_BIT = 0x40       # a box's flags: the box is ignored
_CHILD_BIT = 0x01        # an index entry's flags: the packet has child ids
_EQUATION = -4           # a box style's name length for an equation box
_TEXT_BOX, _PICTURE = 1, 3
_BOUND = 128             # a column span this large binds the cell to the one above
_NOSPAN = (1, 1, False)  # (columns, rows, header row)
# the EOL group's non-deletable sub-functions: their sizes, id included; 0x81, 0x8E and 0x8F give theirs in a u16
_EOL_SUBS = {0x80: 5, 0x82: 4, 0x83: 4, 0x84: 9, 0x85: 4, 0x86: 10, 0x87: 6, 0x88: 6, 0x89: 11, 0x8B: 3, 0x8C: 3,
             0x8D: 1}
_EOL_SIZED = frozenset((0x81, 0x8E, 0x8F))
_NEW = object()          # a packet id not walked yet
_MAIN = "PerfectOffice_MAIN"
_SWITCH = "PHRO_READER_SOFFICE"
_DEADLINE = "PHRO_READER_DEADLINE"  # set by the gate for its child (gate._child)
_SEP = chr(92)
_LONG = _SEP * 2 + "?" + _SEP  # the Windows extended-length path prefix
# WP5
_BLOCK5 = 50             # an index block: [0xFFFB][5][50][u32 next] and four 10-byte packet indexes
_GFX5 = 8                # the graphics information packet
_FIG5_TYPE, _FIG5_NUM = 52, 113  # a figure box's type byte and, for type 0x80, its u16 picture number
# the 16-byte head libwpd puts before an embedded picture that has none: a WPG file, type 0x16
_WPG_HEAD = b"\xffWPC\x10\x00\x00\x00\x01\x16\x01\x00\x00\x00\x00\x00"
# the sizes of the fixed groups 0xC0-0xCF, both ids included
_FIXED5 = (4, 9, 11, 3, 3, 5, 6, 7, 4, 5, 6, 6, 8, 10, 10, 12)
_CLASS5 = bytes(1 if 0x20 <= i <= 0x7E else 0 for i in range(256))  # WP5 and 4.2 text bytes
_PARA5 = frozenset((0x0A, 0x0C, 0x8C, 0x90, 0x99))
# WP5 single bytes: soft new page and soft return, the invisible returns, a hard space, the hard hyphens
_SINGLE5 = {0x0B: " ", 0x0D: " ", 0x93: " ", 0x94: " ", 0x95: " ", 0xA0: _SINGLE[0x81], 0xA9: "-", 0xAA: "-",
            0xAB: "-"}
# WordPerfect 4.2: the sizes of the groups 0xC0-0xFE, -1 for a group that runs to its closing byte
_SIZE42 = (6, 4, 3, 5, 5, 6, 4, 6, 8, 42, 3, 6, 4, 3, 4, 3,
           6, -1, -1, 4, 4, 4, 6, -1, 4, 4, 4, 4, -1, 24, 4, -1,
           4, 3, -1, 150, 6, 23, 11, 3, 3, -1, -1, -1, 4, -1, 44, 18,
           6, 106, -1, 100, 4, -1, 5, -1, -1, -1, -1, -1, -1, -1, -1)
_PASSWORD42 = b"\xfe\xff\x61\x61"
_WORDS42 = re.compile(rb"[\x21-\x7e]{2} [\x21-\x7e]{2}")  # two words in a skipped 4.2 group: text, not formatting
_DONE = object()         # a packet whose blocks were emitted already


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
    if major == 0:  # WP5 keeps the encryption word in the other byte order
        encryption = ((encryption & 0xFF) << 8) | (encryption >> 8)
    return Head(pointer, product, ftype, major, minor, encryption)


# ---- stories

class _Tab:
    """The open table of a story: its rows of origin cells, the row being filled (None until its first cell), whether
    it is a header row, the column the next cell takes, the origin cell covering each column, the cell its text goes
    to, and whether its cells were cut."""
    __slots__ = ("rows", "row", "hdr", "col", "owner", "cell", "cut")

    def __init__(self):
        self.rows = []
        self.row = None
        self.hdr = False
        self.col = 0
        self.owner = {}
        self.cell = None
        self.cut = False


class _Story:
    """One story's paragraphs: (hidden kind, bold, text or NoteRef) runs until a paragraph end makes them a Para, in
    the open table's cell when there is one."""
    __slots__ = ("kind", "inhead", "mute", "_bold", "_hid", "_runs", "_blocks", "_tab")

    def __init__(self, kind="body", inhead=False):
        self.kind = kind
        self.inhead = inhead or kind == "header"  # a header's story, or one walked from it: its text is a line
        self.mute = False  # a note's number, between its on and off codes, is not text of the story
        self._bold = False
        self._hid = ""
        self._runs = []
        self._blocks = []
        self._tab = None

    def text(self, s):
        if s and not self.mute:
            self._runs.append((self._hid, self._bold, s))

    def ref(self, mark):
        """A NoteRef in the paragraph."""
        self._runs.append(("", False, mark))

    def bold(self, on):
        self._bold = bool(on)

    def hidden(self, kind):
        """Start hidden text of `kind` ("deleted"), or end it with ""."""
        self._hid = kind

    def end_para(self):
        self.mute = False
        if self._hid:  # a paragraph end inside deleted text is deleted with it
            self.text(" ")
            return
        self._flush()

    def put(self, blocks):
        """Blocks in place (a text box, pictures): the paragraph they come in ends before them."""
        self._flush()
        self._target().extend(blocks)

    def blocks(self) -> list:
        self._flush()
        if self._tab is not None:
            self._close_table()
        return self._blocks

    def _target(self) -> list:
        t = self._tab
        return t.cell.blocks if t is not None and t.cell is not None else self._blocks

    def _flush(self):
        self.mute = False
        runs, self._runs = self._runs, []
        _trim(runs)
        if runs:
            self._target().append(Para(_inlines(runs)))

    # ---- tables

    def table(self, event, span, st):
        """A table code: "on", "row", "cell", "off", or "row0" (a row whose cells come with codes of their own, WP5);
        `span` is (columns, rows, header row) from its group."""
        if self._hid:
            self.end_para()
            return
        if event == "on":
            self._flush()
            if self._tab is not None:
                self._close_table()
            self._tab = _Tab()
            return
        t = self._tab
        if t is None:
            self.end_para()
            return
        self._flush()
        if event == "off":
            self._close_table()
            return
        if event in ("row", "row0"):
            t.row, t.hdr, t.col = None, span[2], 0
            if event == "row0":
                return  # the text up to the row's first cell code stays in the cell it was in
        self._cell(t, span, st)

    def drop_table(self):
        self._runs = []
        self._tab = None

    def _cell(self, t, span, st):
        cols, rows = span[0], span[1]
        if cols >= _BOUND:
            owner = t.owner.get(t.col)
            if owner is not None:
                t.col += max(1, cols & 0x7F)
                t.cell = owner  # bound to the cell above: its text joins that cell
                return
            cols = rows = 1  # nothing above: a plain cell one column wide, at this column
        cols, rows = max(1, cols), max(1, rows)
        if t.col >= limits.GRID_COLS or st.budget[0] <= 0:
            if not t.cut:
                t.cut = True
                st.ctx.skipped["cells"] += 1
            t.cell = Cell([])  # in no row: its text is dropped with it
            t.col += cols
            return
        st.budget[0] -= 1
        cell = Cell([], rowspan=rows, colspan=cols, header=t.hdr)
        if t.row is None:
            t.row = []
            t.rows.append(t.row)
        t.row.append(cell)
        for c in range(t.col, min(t.col + cols, limits.GRID_COLS)):
            t.owner[c] = cell
        t.col += cols
        t.cell = cell

    def _close_table(self):
        t, self._tab = self._tab, None
        if t.rows:
            self._blocks.append(Table(t.rows))


def _trim(runs):
    """Strip the white space at the paragraph's two edges, in place; a NoteRef is an edge that stays."""
    k = 0
    while k < len(runs):
        h, b, t = runs[k]
        if not isinstance(t, str):
            break
        t = t.lstrip()
        if t:
            runs[k] = (h, b, t)
            break
        k += 1
    del runs[:k]
    k = len(runs)
    while k:
        h, b, t = runs[k - 1]
        if not isinstance(t, str):
            break
        t = t.rstrip()
        if t:
            runs[k - 1] = (h, b, t)
            break
        k -= 1
    del runs[k:]


def _inlines(runs) -> list:
    """Adjacent runs of one look merge; consecutive hidden runs of one kind are one Hidden; a NoteRef stands alone."""
    out, cur = [], None  # cur: [hidden, bold, parts]
    for h, b, t in runs:
        if not isinstance(t, str):
            if cur is not None:
                _put(out, cur)
                cur = None
            out.append(t)
            continue
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

def _wpchar(code, cset, wp5=False) -> str | None:
    """An extended character: set 0 is ASCII from 0x20 (libwpd), the other sets are `wpd_charsets` (WP5's when
    `wp5`)."""
    if cset == 0:
        return char(0, code - 0x20) if code >= 0x20 else None
    return char(cset, code, wp5=wp5)


def _attribute(ctx):
    ctx.skipped["attribute"] += 1
    if ctx.skipped["attribute"] == 1:
        ctx.warnings.append(_ATTR_WARNING)


# ---- the WP6+ prefix index

def _prefix6(data, ctx, index=None) -> dict:
    """Packet type -> [(id, offset, size)], ids counted from 1 as the index numbers them; `index`, when given, is
    filled with id -> (type, flags, offset, size)."""
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
        flags, ptype, _use, _hide, size, offset = struct.unpack_from("<BBHHII", data, at)
        if offset + size > n:
            ctx.skipped["record"] += 1
            continue
        out.setdefault(ptype, []).append((i, offset, size))
        if index is not None:
            index[i] = (ptype, flags, offset, size)
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

class _State:
    """One WP6+ document's walk: the data and its packets (id -> (type, flags, offset, size)), the Doc its stories
    fill, and what every story of it shares."""
    __slots__ = ("data", "index", "doc", "ctx", "visited", "subdocs", "ids", "count", "seen", "img_total", "budget",
                 "pics", "gfx", "big")

    def __init__(self, data, index, doc, ctx):
        self.data = data
        self.index = index
        self.doc = doc
        self.ctx = ctx
        self.visited = {}   # text packet id -> None while it is walked, _DONE once its blocks were emitted
        self.pics = set()   # 0x6F packet ids (WP5: picture numbers) emitted as pictures
        self.subdocs = 0    # sub-document references read (WPD_SUBDOCS)
        self.ids = {}       # text packet id -> its note id
        self.count = {"fn": 0, "cm": 0}
        self.seen = set()   # (kind, line) of the header and footer lines kept
        self.img_total = 0  # picture bytes emitted (IMAGE_BYTES)
        self.budget = [limits.TABLE_NODES]  # cells the document's tables may still take
        self.gfx = []       # WP5: the pictures of packet 8, (start, end) or None each
        self.big = set()    # WP5: picture numbers refused as past IMAGE_BYTES (the total only grows)


def _walk6(data, start, end, story, st, depth=0):
    """Walk the function codes of data[start:end] into `story` (see the module docstring); `depth` is the story's
    sub-document nesting, 0 for the body."""
    ctx = st.ctx
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
            if 0xBD <= b <= 0xC6:
                _table(story, "off" if b < 0xC0 else "cell" if b == 0xC6 else "row", _NOSPAN, st)
            elif b in _PARA_BYTES:
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
            _variable(b, data, pos, size, story, undo, st, depth)
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


def _variable(b, data, pos, size, story, undo, st, depth):
    """One variable group that passed its check."""
    sub = data[pos + 1]
    if b == 0xD0:
        if 0x0A <= sub <= 0x13:
            event = "cell" if sub == 0x0A else "row" if sub <= 0x10 else "off"
            _table(story, event, _NOSPAN if event == "off" else _cellinfo(data, pos, size), st)
        elif sub in _EOL_SPACE:
            story.text(" ")
        elif sub in _EOL_PARA:
            story.end_para()
    elif b == 0xE0:
        story.text(" ")
    elif b == 0xD4 and sub == 0x2A:
        _table(story, "on", _NOSPAN, st)
    elif undo:
        return  # notes, comments, headers and boxes in deleted text are left out with it
    elif b == 0xD7:
        if sub in (0, 2):
            _footnote(data, pos, size, story, st, depth)
        elif sub in (1, 3):
            story.mute = False
    elif b == 0xD4 and sub == 0x1D:
        _comment(data, pos, size, story, st, depth)
    elif b == 0xD6 and sub <= 5:
        _header(data, pos, size, sub, st, depth)
    elif b == 0xDF and sub <= 2:
        _box(data, pos, size, story, st, depth)


def _ghead(data, pos, size):
    """The head of the variable group at pos: (flags, prefix ids, contents start, non-deletable end, contents end),
    or None when it does not fit the group."""
    gend = pos + size - 3
    q = pos + 4
    if q >= gend:
        return None
    flags = data[q]
    q += 1
    pids = ()
    if flags & _PREFIX_BIT:
        if q >= gend:
            return None
        n = data[q]
        q += 1
        if q + 2 * n > gend:
            return None
        pids = struct.unpack_from(f"<{n}H", data, q)
        q += 2 * n
    if q + 2 > gend:
        return None
    nd = struct.unpack_from("<H", data, q)[0]
    q += 2
    if nd & 0x8000 or nd > size:
        return None
    return flags, pids, q, min(q + nd, gend), gend


def _u16(data, q, end):
    """The u16 at q, or None when it is not inside [.., end)."""
    return struct.unpack_from("<H", data, q)[0] if q + 2 <= end else None


def _kind(st, pid):
    ent = st.index.get(pid)
    return ent[0] if ent is not None else None


# ---- tables

def _table(story, event, span, st):
    """A table code into `story`, under guard("table"): a table whose codes fail is dropped."""
    done = False
    with st.ctx.guard("table"):
        story.table(event, span, st)
        done = True
    if not done:
        story.drop_table()


def _cellinfo(data, pos, size):
    """(columns, rows, header row) from the non-deletable sub-functions of the EOL group at pos."""
    h = _ghead(data, pos, size)
    if h is None:
        return _NOSPAN
    _, _, c, nd_end, gend = h
    skip = _u16(data, c, nd_end)
    if skip is None:
        return _NOSPAN
    cols = rows = 1
    hdr = False
    q = c + 2 + skip
    while q < nd_end:  # each sub-function moves q on by at least one byte
        f = data[q]
        if f in _EOL_SIZED:
            n = _u16(data, q + 1, gend)
            if n is None or n < 3:
                break
        else:
            n = _EOL_SUBS.get(f)
            if n is None:
                break
        if q + n > gend:
            break
        if f == 0x80:
            hdr = bool(data[q + 1] & 0x04)
        elif f == 0x85:
            cols, rows = data[q + 1], data[q + 2]
        q += n
    return cols, rows, hdr


# ---- sub-documents: notes, comments, headers, footers, watermarks, boxes

def _text_span(data, off, size):
    """The stream of the 0x08 packet at off: (start, end), empty for no text; None when its block table does not
    fit the packet."""
    end = off + size
    n = _u16(data, off, end)
    if n is None or off + 6 > end:
        return None
    q = off + 6
    if not n:
        return q, q
    if q + 4 * n > end:
        return None
    a = q + 4 * n
    total = sum(struct.unpack_from(f"<{n}I", data, q))
    if a + total > end:
        return None
    return a, a + total


def _subdoc(pid, kind, story_kind, visited, depth, st, inhead=False):
    """The blocks of text packet `pid` walked as a story of `story_kind` (inside a header when `inhead`), at nesting
    `depth`; None when it is not read: past WPD_SUBDOCS (`note`), a cycle or past MAX_DEPTH (`depth`), or a packet
    missing or unreadable (`kind`). A packet walked before gives `_DONE`, counted toward WPD_SUBDOCS: its blocks were
    emitted once and are not given again, so the caller decides what a repeat is."""
    skipped = st.ctx.skipped
    if st.subdocs >= WPD_SUBDOCS:
        skipped["note"] += 1
        return None
    got = visited.get(pid, _NEW)
    if got is None or depth > limits.MAX_DEPTH:
        skipped["depth"] += 1
        return None
    if got is _DONE:
        st.subdocs += 1
        return _DONE
    ent = st.index.get(pid)
    span = _text_span(st.data, ent[2], ent[3]) if ent is not None and ent[0] == _TEXT else None
    if span is None:
        skipped[kind] += 1
        return None
    st.subdocs += 1
    visited[pid] = None
    story = _Story(story_kind, inhead)
    _walk6(st.data, span[0], span[1], story, st, depth)
    visited[pid] = _DONE
    return story.blocks()


def _footnote(data, pos, size, story, st, depth):
    story.mute = True  # the number the body shows, up to the note off
    h = _ghead(data, pos, size)
    if story.inhead or h is None or not h[1]:
        st.ctx.skipped["note"] += 1
        return
    _mark(h[1][0], "fn", story, st, depth)


def _comment(data, pos, size, story, st, depth):
    h = _ghead(data, pos, size)
    if not story.inhead and h is not None:
        for pid in h[1]:
            ent = st.index.get(pid)
            if ent is not None and ent[0] == _COMMENT:
                off, end = ent[2], ent[2] + ent[3]
                text = _u16(data, off + 2, end)
                if _u16(data, off, end) == 1 and text is not None:
                    _mark(text, "cm", story, st, depth)
                    return
                break
    st.ctx.skipped["note"] += 1


def _mark(pid, prefix, story, st, depth):
    """A note ("fn") or comment ("cm") of text packet `pid`: into Doc.notes once, its NoteRef into `story`; a later
    reference is a NoteRef to the same note (none when the packet gave no note)."""
    blocks = _subdoc(pid, "note", "note", st.visited, depth + 1, st)
    if blocks is None:
        return
    nid = st.ids.get(pid)
    if nid is None:
        if blocks is _DONE or not blocks:
            return
        st.count[prefix] += 1
        nid = st.ids[pid] = f"{prefix}-{st.count[prefix]}"
        st.doc.notes[nid] = comment_note(blocks, "") if prefix == "cm" else blocks
    story.ref(NoteRef(nid))


def _header(data, pos, size, sub, st, depth):
    kind = "footer" if sub in (2, 3) else "header"  # 0-1 headers, 2-3 footers, 4-5 watermarks
    h = _ghead(data, pos, size)
    if h is None or not h[1]:
        st.ctx.skipped[kind] += 1
        return
    blocks = _subdoc(h[1][0], kind, "header", st.visited, depth + 1, st)
    line = _flat(blocks) if blocks and blocks is not _DONE else ""  # a repeat's line is the one kept already
    if line and (kind, line) not in st.seen:
        st.seen.add((kind, line))
        (st.doc.footers if kind == "footer" else st.doc.headers).append(line)


def _flat(blocks) -> str:
    """A header's line: its paragraphs' text and its tables' cells, one after another; hidden text left out."""
    out = []
    for b in blocks:
        if isinstance(b, Para):
            t = " ".join(_plain(b.inlines).split())
        elif isinstance(b, Table):
            t = _flat([x for row in b.rows for cell in row for x in cell.blocks])
        else:
            continue
        if t:
            out.append(t)
    return " ".join(out)


def _plain(inlines) -> str:
    return "".join(i.text if isinstance(i, Text) else _plain(i.inlines) if isinstance(i, Bold) else ""
                   for i in inlines)


def _box(data, pos, size, story, st, depth):
    skipped = st.ctx.skipped
    h = _ghead(data, pos, size)
    if h is None:
        skipped["shape"] += 1
        return
    flags, pids, c, _, gend = h
    if flags & _IGNORE_BIT:
        return
    ctype, name = 0, None
    for pid in pids:
        ent = st.index.get(pid)
        if ent is not None and ent[0] == _BOX_STYLE:
            ctype, name = _style(data, ent[2], ent[2] + ent[3])
            break
    own = _override(data, c, gend)
    if own is not None:
        ctype = own
    if any(_kind(st, p) == _LINK for p in pids):
        skipped["link"] += 1
    text = next((p for p in pids if _kind(st, p) == _TEXT), None)
    if name == _EQUATION:
        skipped["object"] += 1
    elif ctype == _TEXT_BOX:
        if text is None:
            skipped["shape"] += 1
            return
        blocks = _subdoc(text, "shape", "box", st.visited, depth + 1, st, story.inhead)
        if blocks is _DONE:
            skipped["depth"] += 1  # its paragraphs stand where the packet was first referenced
        elif blocks:
            story.put(blocks)
    elif ctype == _PICTURE:
        _pictures(data, pids, story, st)
    elif text is not None:
        skipped["shape"] += 1  # a box of another kind with text: not read


def _style(data, q, end):
    """(content kind, name length) of the 0x41 box style packet in data[q:end]; the kind 0, the name None, when the
    packet is too short to give them."""
    n = _u16(data, q, end)
    if n is None:
        return 0, None
    q += 2 + 2 * n + 2  # the child ids, the box data size
    size = _u16(data, q, end)  # the name and library section
    if size is None:
        return 0, None
    name = struct.unpack_from("<h", data, q + 4)[0] if q + 6 <= end else None
    q += 2 + size
    for _ in range(2):  # the counter and positioning sections
        size = _u16(data, q, end)
        if size is None:
            return 0, name
        q += 2 + size
    q += 2  # the content section's size, then a byte, then the kind
    return (data[q + 1] if q + 2 <= end else 0), name


def _override(data, q, end):
    """The content kind a box group's own data in data[q:end] overrides, or None."""
    q += 18  # 14 reserved bytes, two u16 sizes
    flags = _u16(data, q, end)
    if flags is None or not flags & 0x2000:
        return None
    q += 2
    for bit in (0x8000, 0x4000):  # the counter and positioning sections
        if flags & bit:
            size = _u16(data, q, end)
            if size is None:
                return None
            q += 2 + size
    inner = _u16(data, q + 2, end)  # after the content section's size
    if inner is None or not inner & 0x4000:
        return None
    q += 4 + (2 if inner & 0x8000 else 0)
    return data[q] if q < end else None


def _pictures(data, pids, story, st):
    """A picture box: the 0x6F packets its first 0x40 packet names, each an Image in place, once a document (a later
    reference to one counts `depth`)."""
    skipped = st.ctx.skipped
    kids = ()
    gid = next((p for p in pids if _kind(st, p) == _GFX_NAME), None)
    if gid is not None:
        _, flags, off, size = st.index[gid]
        n = _u16(data, off, off + size) if flags & _CHILD_BIT else None
        if n is not None:
            fit = min(n, (size - 2) // 2)
            if fit < n:
                skipped["image"] += 1  # ids cut by the packet's end
            kids = struct.unpack_from(f"<{fit}H", data, off + 2)
    if not kids:
        skipped["image"] += 1
        return
    pics = []
    for k in kids:
        if k in st.pics:
            skipped["depth"] += 1
            continue
        ent = st.index.get(k)
        if (ent is None or ent[0] != _GFX_DATA or not ent[3] or story.inhead
                or st.img_total + ent[3] > limits.IMAGE_BYTES):
            skipped["image"] += 1
            continue
        st.pics.add(k)
        st.img_total += ent[3]
        pics.append(Image(data[ent[2]:ent[2] + ent[3]]))
    if pics:
        story.put(pics)


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


# ---- WordPerfect 5.x

def _prefix5(data, ctx) -> dict:
    """Packet type -> (offset, size) from the 0xFFFB index blocks chained from offset 16 (the first packet of a type
    whose range is inside the file)."""
    n = len(data)
    skipped = ctx.skipped
    out = {}
    seen = set()
    at = _HEAD
    for _ in range(WP5_BLOCKS):
        if at in seen or at + _BLOCK5 > n:
            skipped["record"] += 1  # a cycle, or a block past the end of the file
            return out
        seen.add(at)
        btype, count, bsize, nxt = struct.unpack_from("<HHHI", data, at)
        if (btype, count, bsize) != (0xFFFB, 5, _BLOCK5):
            skipped["record"] += 1
            return out
        for k in range(4):
            ptype, psize, poff = struct.unpack_from("<HII", data, at + 10 + 10 * k)
            if ptype in (0, 0xFFFF):
                continue  # an empty slot
            if 0x02FF < ptype < 0xFFFB:
                skipped["record"] += 1  # not a packet type: the chain is corrupt from here (libwpd)
                return out
            if poff + psize > n:
                skipped["record"] += 1
                continue
            out.setdefault(ptype, (poff, psize))
        if not nxt:
            return out
        at = nxt
    skipped["record"] += 1  # past WP5_BLOCKS
    return out


def _gfx5(data, packet) -> list:
    """The pictures of graphics packet 8 at `packet` (offset, size): a (start, end) for each, None for one whose
    bytes run past the packet; [] when its size table does not fit the packet."""
    if packet is None:
        return []
    off, size = packet
    end = off + size
    count = _u16(data, off, end)
    if count is None or off + 2 + 4 * count > end:
        return []
    q = off + 2 + 4 * count
    out = []
    for size_k in struct.unpack_from(f"<{count}I", data, off + 2):
        out.append((q, q + size_k) if q + size_k <= end else None)
        q += size_k
    return out


def _walk5(data, start, end, story, st, depth=0):
    """Walk the WP5 function codes of data[start:end] into `story` (see the module docstring)."""
    skipped = st.ctx.skipped
    cls = data[start:end].translate(_CLASS5)
    pos = start
    last_fail = -2
    junk_end = -1
    while pos < end:
        b = data[pos]
        if 0x20 <= b <= 0x7E:
            stop = cls.find(b"\x00", pos - start)
            stop = end if stop < 0 else stop + start
            story.text(data[pos:stop].decode("latin-1"))
            pos = stop
            continue
        if b < 0xC0 or b == 0xFF:
            if b in _PARA5:
                story.end_para()
            else:
                story.text(_SINGLE5.get(b, ""))
            pos += 1
            continue
        size, extent = _check5(data, pos, end, b)
        if size is None:
            if pos >= junk_end:
                skipped["record"] += 1  # cut by the end of the data: the walk ends here
                break
            size, extent = 0, 1
        if not size:
            if pos != last_fail + 1 and pos >= junk_end:
                skipped["record"] += 1
            junk_end = max(junk_end, pos + extent)
            last_fail = pos
            pos += 1
            continue
        if b < 0xD0:
            _fixed5(b, data, pos, story, st.ctx)
        else:
            _variable5(b, data, pos, size, story, st, depth)
        pos += size


def _check5(data, pos, end, b):
    """As `_check`, for WP5: fixed groups 0xC0-0xCF by `_FIXED5`, variable groups 0xD0-0xFE as
    [id][sub][u16 len] ... [u16 len][sub][id], len the whole size minus 4."""
    if b < 0xD0:
        size = _FIXED5[b - 0xC0]
        if pos + size > end:
            return None, 0
        return (size, size) if data[pos + size - 1] == b else (0, size)
    if pos + 4 > end:
        return None, 0
    n = struct.unpack_from("<H", data, pos + 2)[0]
    if n < 4:
        return 0, 1
    size = n + 4
    if pos + size > end:
        return None, 0
    if (data[pos + size - 1] != b or data[pos + size - 2] != data[pos + 1]
            or struct.unpack_from("<H", data, pos + size - 4)[0] != n):
        return 0, size
    return size, size


def _fixed5(b, data, pos, story, ctx):
    if b == 0xC0:
        c = _wpchar(data[pos + 1], data[pos + 2], wp5=True)
        if c is None:
            ctx.skipped["glyphs"] += 1
        else:
            story.text(c)
    elif b in (0xC1, 0xC2):  # tab, indent
        story.text(" ")
    elif b in (0xC3, 0xC4):
        attr = data[pos + 1]
        if attr > _MAX_ATTR:
            _attribute(ctx)
        elif attr == _BOLD:
            story.bold(b == 0xC3)


def _variable5(b, data, pos, size, story, st, depth):
    """One WP5 variable group that passed its check; its contents are data[pos + 4:pos + size - 4]."""
    sub = data[pos + 1]
    c, gend = pos + 4, pos + size - 4
    if b == 0xD2 and sub == 0x0B:
        _table(story, "on", _NOSPAN, st)
    elif b == 0xDC and sub == 0:
        if c + 4 > gend:
            _table(story, "cell", _NOSPAN, st)
        else:
            _table(story, "cell", (data[c + 2], data[c + 3], False), st)  # columns (0x80: bound above), rows
    elif (b == 0xDC and sub == 1) or (b == 0xDD and sub in (1, 3)):
        _table(story, "row0", _NOSPAN, st)
    elif b in (0xDC, 0xDD) and sub == 2:
        _table(story, "off", _NOSPAN, st)
    elif b == 0xD5 and sub <= 3:
        _header5(data, c, gend, sub, st, depth)
    elif b == 0xD6 and sub <= 1:
        q = c + 3  # flags, the u16 number
        if sub == 0:
            q = q + 1 + 2 * (data[q] + 1) + 9 if q < gend else gend + 1  # the page count and what it sizes
        else:
            q += 4
        _note5(q, gend, "fn", story, st, depth)
    elif b == 0xD9 and sub == 2:
        _note5(c + 3, gend, "cm", story, st, depth)  # a u16 screen column, a u8 line count, then the text
    elif b == 0xDA:
        _box5(data, pos, c, gend, sub, story, st)


def _sub5(start, end, kind, story_kind, depth, st, inhead=False):
    """The blocks of data[start:end] walked as a WP5 story; None past WPD_SUBDOCS (`note`) or MAX_DEPTH (`depth`)."""
    skipped = st.ctx.skipped
    if st.subdocs >= WPD_SUBDOCS:
        skipped["note"] += 1
        return None
    if depth > limits.MAX_DEPTH:
        skipped["depth"] += 1
        return None
    st.subdocs += 1
    story = _Story(story_kind, inhead)
    _walk5(st.data, start, end, story, st, depth)
    return story.blocks()


def _note5(start, end, prefix, story, st, depth):
    """A footnote, endnote ("fn") or comment ("cm") whose text is data[start:end]."""
    if story.inhead or start > end:
        st.ctx.skipped["note"] += 1
        return
    blocks = _sub5(start, end, "note", "note", depth + 1, st)
    if not blocks:
        return
    st.count[prefix] += 1
    nid = f"{prefix}-{st.count[prefix]}"
    st.doc.notes[nid] = comment_note(blocks, "") if prefix == "cm" else blocks
    story.ref(NoteRef(nid))


def _header5(data, c, gend, sub, st, depth):
    """D5: 7 bytes, the occurrence byte (0: no header), 10 bytes, then the text."""
    kind = "footer" if sub >= 2 else "header"
    if c + 8 > gend:
        st.ctx.skipped[kind] += 1
        return
    if not data[c + 7] or c + 18 > gend:
        return  # a header turned off, or one with no text
    blocks = _sub5(c + 18, gend, kind, "header", depth + 1, st)
    line = _flat(blocks) if blocks else ""
    if line and (kind, line) not in st.seen:
        st.seen.add((kind, line))
        (st.doc.footers if kind == "footer" else st.doc.headers).append(line)


def _box5(data, pos, c, gend, sub, story, st):
    """DA: a figure (sub 0) of box type 0x80 is picture n of packet 8; an equation (4) is `object`; a table, text or
    user box (1-3) is `shape`, its text unread; lines (5, 6) are nothing."""
    skipped = st.ctx.skipped
    if sub in (1, 2, 3):
        skipped["shape"] += 1
        return
    if sub == 4:
        skipped["object"] += 1
        return
    if sub != 0:
        return
    at = pos + _FIG5_TYPE
    num = _u16(data, pos + _FIG5_NUM, gend) if at < gend and data[at] == 0x80 else None
    pics = st.gfx
    span = pics[num] if num is not None and num < len(pics) else None
    if span is None or story.inhead or span[1] <= span[0]:
        skipped["image"] += 1
        return
    if num in st.pics:
        skipped["depth"] += 1
        return
    # the size is checked before any copy, and a picture once past the cap is never copied again; each reference
    # past IMAGE_BYTES counts `image`, as in WP6
    a, b = span
    whole = data[a:min(a + 4, b)] == _MAGIC  # else an embedded picture without its WPG head (libwpd adds the same one)
    need = b - a + (0 if whole else len(_WPG_HEAD))
    if num in st.big or st.img_total + need > limits.IMAGE_BYTES:
        st.big.add(num)
        skipped["image"] += 1
        return
    pic = data[a:b] if whole else _WPG_HEAD + data[a:b]
    st.pics.add(num)
    st.img_total += len(pic)
    story.put([Image(pic)])


def _wp5(data, h, ctx, path):
    n = len(data)
    if h.pointer < _HEAD:
        raise ReadError("damaged", "the WordPerfect document pointer is inside the head")
    if h.pointer > n:
        raise ReadError("damaged", "the WordPerfect document pointer is past the end of the file")
    packets = _prefix5(data, ctx) if h.pointer >= _HEAD + _BLOCK5 else {}
    doc = Doc()
    st = _State(data, {}, doc, ctx)
    st.gfx = _gfx5(data, packets.get(_GFX5))
    story = _Story("body")
    _walk5(data, h.pointer, n, story, st)
    doc.blocks = story.blocks()
    if not doc.blocks:
        raise ReadError("damaged", "no WordPerfect body")
    return doc


# ---- WordPerfect 4.2

def is_wp42(data: bytes) -> bool:
    """The 4.2 check over data[:WP42_PROBE]: a 4.2 password head, or every byte 0xC0-0xFE closing by its size
    (`_SIZE42`) or by its closing byte, no stray 0xFF, and at least one fixed-size group closing by its size. A group
    the probe's end cuts passes when the file goes on past the probe and a fixed-size group came before it. Variable
    groups alone are not enough: plain cp1252 text with two é (0xE9) or two ü (0xFC) pairs up as variable groups."""
    if data[:4] == _PASSWORD42:
        return True
    probe = data[:WP42_PROBE]
    n = len(probe)
    more = len(data) > n
    fixed = 0
    pos = 0
    while pos < n:
        b = probe[pos]
        if b < 0xC0:
            pos += 1
            continue
        if b == 0xFF:
            return False
        size = _SIZE42[b - 0xC0]
        if size < 0:
            close = probe.find(bytes((b,)), pos + 1)
            if close < 0:
                return more and fixed > 0  # open at the probe's end: it passes only when the file goes on
            pos = close + 1
        else:
            if pos + size > n:
                return more and fixed > 0
            if probe[pos + size - 1] != b:
                return False
            pos += size
            fixed += 1
    return fixed > 0


def _walk42(data, start, end, story, st, depth=0):
    """Walk the 4.2 codes of data[start:end] into `story`: text, tab, hard and soft new lines, bold, extended
    characters and footnotes; any other group is skipped by its size or its closing byte, a variable one holding words
    (`_WORDS42`) counted `record`, so text the 4.2 check took for a group is never lost silently."""
    skipped = st.ctx.skipped
    cls = data[start:end].translate(_CLASS5)
    pos = start
    last_fail = -2
    junk_end = -1
    while pos < end:
        b = data[pos]
        if 0x20 <= b <= 0x7E:
            stop = cls.find(b"\x00", pos - start)
            stop = end if stop < 0 else stop + start
            story.text(data[pos:stop].decode("latin-1"))
            pos = stop
            continue
        if b < 0xC0 or b == 0xFF:
            if b in (0x0A, 0x0C):
                story.end_para()
            elif b in (0x09, 0x0B, 0x0D):
                story.text(" ")
            elif b in (0x9C, 0x9D):
                story.bold(b == 0x9D)
            pos += 1
            continue
        size = _SIZE42[b - 0xC0]
        if size < 0:
            close = data.find(bytes((b,)), pos + 1, end)
            size = close + 1 - pos if close >= 0 else None
        elif pos + size > end:
            size = None
        elif data[pos + size - 1] != b:
            if pos != last_fail + 1 and pos >= junk_end:
                skipped["record"] += 1
            junk_end = max(junk_end, pos + size)
            last_fail = pos
            pos += 1
            continue
        if size is None:
            if pos >= junk_end:
                skipped["record"] += 1  # cut by the end of the data: the walk ends here
                break
            last_fail = pos
            pos += 1
            continue
        if b == 0xE1:
            c = bytes((data[pos + 1],)).decode("cp437") if data[pos + 1] >= 0x20 else None
            if c is None:
                skipped["glyphs"] += 1
            else:
                story.text(c)
        elif b == 0xE2:
            _note42(data, pos, pos + size - 1, story, st, depth)
        elif b == 0xD1:
            skipped["header"] += 1  # headers and footers are not read in 4.2
        elif _SIZE42[b - 0xC0] < 0 and _WORDS42.search(data, pos + 1, pos + size - 1):
            skipped["record"] += 1
        pos += size


def _note42(data, pos, close, story, st, depth):
    """0xE2: [def][a][b][c][d] and the line and page counts up to 0xFF, then the two margins and the text, up to the
    closing byte at `close` (wp42ff.txt); an endnote is a footnote."""
    ff = data.find(b"\xff", pos + 6, close)
    if ff < 0 or ff + 3 > close or story.inhead:
        st.ctx.skipped["note"] += 1
        return
    if st.subdocs >= WPD_SUBDOCS:
        st.ctx.skipped["note"] += 1
        return
    if depth + 1 > limits.MAX_DEPTH:
        st.ctx.skipped["depth"] += 1
        return
    st.subdocs += 1
    sub = _Story("note")
    _walk42(data, ff + 3, close, sub, st, depth + 1)
    blocks = sub.blocks()
    if not blocks:
        return
    st.count["fn"] += 1
    nid = f"fn-{st.count['fn']}"
    st.doc.notes[nid] = blocks
    story.ref(NoteRef(nid))


def _wp42(data, ctx) -> Doc:
    doc = Doc()
    st = _State(data, {}, doc, ctx)
    story = _Story("body")
    _walk42(data, 0, len(data), story, st)
    doc.blocks = story.blocks()
    if not doc.blocks:
        raise ReadError("damaged", "no WordPerfect body")
    return doc


# ---- the LibreOffice switch

def _on_path() -> str | None:
    """`soffice` on PATH, walked here and not with `shutil.which`, which on Windows looks in the current folder first
    and takes any PATHEXT name (a soffice.bat beside a document would run). Only absolute entries are searched (on
    Windows with a drive or share), never the current folder; on Windows only soffice.exe and soffice.com are taken,
    elsewhere only a regular file named soffice that is executable."""
    nt = os.name == "nt"
    names = ("soffice.exe", "soffice.com") if nt else ("soffice",)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry) or (nt and not os.path.splitdrive(entry)[0]):
            continue
        for name in names:
            p = os.path.join(entry, name)
            if os.path.isfile(p) and (nt or os.access(p, os.X_OK)):
                return p
    return None


def soffice() -> str | None:
    """The `soffice` PHRO_READER_SOFFICE names: "1" searches PATH (`_on_path`) and the standard install folders (None
    when none is there), any other non-empty value is the path itself; unset or empty is None."""
    value = os.environ.get(_SWITCH, "")
    if not value:
        return None
    if value != "1":
        return value
    found = _on_path()
    if found:
        return found
    places = [os.path.join(base, "LibreOffice", "program", "soffice.exe")
              for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if base]
    places += ["/usr/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    return next((p for p in places if os.path.isfile(p)), None)


def _taskkill() -> str:
    """taskkill.exe by its absolute path under %SystemRoot%\\System32 (C:\\Windows when SystemRoot is unset or
    empty): CreateProcess looks for a bare name in the current folder too, where a planted taskkill.exe could sit."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")


def _kill_tree(proc):
    """Kill the child and every process it started. When taskkill or killpg fails, the child itself is killed
    (`proc.kill`); an error from that is raised."""
    try:
        if os.name == "nt":
            r = subprocess.run([_taskkill(), "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TASKKILL_SECONDS)
            done = r.returncode == 0
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            done = True
    except (OSError, subprocess.SubprocessError):
        done = False
    if not done:
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # gone already


def _switch(what, path, ctx):
    """A file the native reader refuses: through LibreOffice when the switch names a soffice, else `unsupported`."""
    if not os.environ.get(_SWITCH):
        raise ReadError("unsupported", f"{what}; set {_SWITCH}=1 to read it through LibreOffice")
    if soffice() is None:
        raise ReadError("unsupported", f"{what}; {_SWITCH} is set but found no soffice")
    if path is None:
        raise ReadError("unsupported", f"{what}; LibreOffice reads only a file on disk")
    from .. import STARTED
    return _via_soffice(path, ctx, STARTED)


def _deadline() -> float | None:
    """The wall-clock deadline (epoch seconds) the gate gives its child in PHRO_READER_DEADLINE; None when it is
    missing or unparsable."""
    try:
        value = float(os.environ.get(_DEADLINE, ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _argument(path) -> str:
    """`path` absolute, so neither a relative name nor one starting with `-` reads as a soffice option; without the
    Windows extended-length prefix (`_LONG`) that resolve may put before it."""
    s = str(Path(path).resolve())
    if s.startswith(_LONG + "UNC" + _SEP):
        return _SEP * 2 + s[len(_LONG) + 4:]
    return s[len(_LONG):] if s.startswith(_LONG) else s


def _via_soffice(path, ctx, started) -> Doc:
    """Convert `path` to docx with soffice in a temporary folder and read it with `ooxml.parse`. The child has
    CONVERT_SECONDS - elapsed - 3 seconds from `started` (a `time.monotonic` reading), and never past 2 seconds before
    the gate's deadline (`_deadline`; one in the past leaves no time), so the tree is killed and the folder removed
    before the gate gives up on this process; past it the process tree is killed and the read is a `timeout`. No
    docx in the folder is `damaged`. The folder is removed in every case, the child killed first when it still runs
    (an interrupt, another error)."""
    exe = soffice()
    left = limits.CONVERT_SECONDS - (time.monotonic() - started) - 3
    deadline = _deadline()
    if deadline is not None:
        left = min(left, deadline - 2 - time.time())
    if left <= 0:
        raise ReadError("timeout")
    tmp = tempfile.mkdtemp(prefix="phro-soffice-")
    proc, killed = None, False
    try:
        cmd = [exe, "--headless", "--norestore", "-env:UserInstallation=" + Path(tmp, "profile").as_uri(),
               "--convert-to", "docx", "--outdir", tmp, _argument(path)]
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, **group)
        except OSError:
            raise ReadError("unsupported", f"soffice could not be started ({_SWITCH})") from None
        try:
            proc.wait(timeout=left)
        except subprocess.TimeoutExpired:
            killed = True
            _reap(proc)
            raise ReadError("timeout") from None
        out = sorted(p for p in Path(tmp).glob("*.docx") if p.is_file())
        if not out:
            raise ReadError("damaged", "LibreOffice gave no docx")
        from . import ooxml
        return ooxml.parse(out[0], ctx)
    finally:
        try:
            if proc is not None and not killed and proc.poll() is None:
                _reap(proc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _reap(proc):
    """Kill the tree and wait 1 second for the child (within the gate's 2-second margin), so Windows lets go of its
    files before the folder is removed. A kill that fails is let pass: it never takes the place of the timeout or of
    the exception on its way out."""
    try:
        _kill_tree(proc)
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


# ---- the entries

def _headless(data, ctx, path):
    """A file without the `FF 'WPC'` head: a 4.2 password head is `encrypted`; a file the 4.2 check takes is read as
    4.2; any other goes to the LibreOffice switch."""
    if data[:4] == _PASSWORD42:
        raise ReadError("encrypted", "WordPerfect 4.2 password")
    if is_wp42(data):
        return _wp42(data, ctx)
    return _switch("not a WordPerfect 4.2 file", path, ctx)


def _mac(data, h, ctx, path):
    """Mac WordPerfect, file type 0x2C: only through the LibreOffice switch."""
    return _switch("Mac WordPerfect", path, ctx)


def _wp6(data, h, ctx) -> Doc:
    n = len(data)
    if h.pointer < _HEAD:
        raise ReadError("damaged", "the WordPerfect document pointer is inside the head")
    if h.pointer > n:
        raise ReadError("damaged", "the WordPerfect document pointer is past the end of the file")
    index = {}
    packets = _prefix6(data, ctx, index)
    doc = Doc()
    with ctx.guard("record"):
        doc.title = _title6(data, packets)
    story = _Story("body")
    _walk6(data, h.pointer, n, story, _State(data, index, doc, ctx))
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
    if is_cfb(data):
        with Compound(path) as cf:
            other = cfb_route.route(cf, "wpd", f"no {_MAIN} stream")
            if other is None:
                try:
                    data = cf.read(_MAIN)
                except (KeyError, CfbError):
                    raise ReadError("damaged", f"the {_MAIN} stream is unreadable") from None
        if other is not None:
            return cfb_route.parse(other, path, ctx)  # another format's compound file named .wpd
    return parse_bytes(data, ctx, path=path)
