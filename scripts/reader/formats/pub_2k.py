# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub 0477b85184e02dc5276582d9cea969208d56c655, src/lib/MSPUBParser2k.cpp, MSPUBParser97.cpp.
"""Publisher 2000 and Publisher 97/98 (pub, Contents version byte 0x22) to the document model, the same model and
order as Publisher 2002 and later (`pub`): one section per page, each page's text boxes and pictures in reading
order, then the master page, the scratch area (hidden) and a links line.

Ported from libmspub (MPL-2.0) at 0477b85184e02dc5276582d9cea969208d56c655, `src/lib/`: `MSPUBParser2k.cpp`
(`parseContents`, `parse2kShapeChunk`, `parseGroup`, `assignShapeImgIndex`, `parseShapeCoordinates`, `parseShapeType`,
`getTextIdOffset`, `getTextMarker`, `getPageTypeBySeqNum`) and `MSPUBParser97.cpp` (`getTextMarker`,
`getTextIdOffset`, `parseContentsTextIfNecessary`, `getTextInfo`). `pub.parse_compound` picks the reader as
libmspub's `MSPUBDocument::parse` does: a Quill stream (`Quill/QuillSub/CONTENTS`) makes the file Publisher 2000
(`parse_2000`), none Publisher 97/98 (`parse_97`). The page, shape, story, master, scratch area and links machinery,
the meter and the room are `pub._Pages`'s, shared with 2002+; the directory is `pub_contents.directory`'s table
layout (`trailer=pub_contents.TRAILER_2K`); the Quill stories, bold and links are `pub_quill`'s.

Chunks. The directory at the u32 at 0x16 gives each chunk's sequence number, parent and offset; its type is the
chunk's first u16: 0x14 a page, 0x15 the document, 0x0F a group, 0x02 a picture, 0x21 a picture's data, and 0x00,
0x04, 0x05, 0x06, 0x07 and 0x08 other shapes (and, in 2000, 0x0A a table: see Text). A page's shapes are the chunks
naming it as their parent, in directory order (2000 has no shape list); a group stands for its members, the chunks
naming it, groups inside included. A file with no document chunk is damaged, as libmspub refuses it. A picture
inside a group is placed (libmspub's `parseGroup` walks only the shapes and groups in it, so draws none).

Pages. In seqnum order (there is no page list); 0x109 is the master page, given after the others as
`Section("master", title="Background")` (Publisher 2000 and 98 call it the background; it has no name), and the
DUMMY seqnums 0x108, 0x10B, 0x10D, 0x116 and 0x119 hold the scratch area. libmspub draws neither; here they are
given as 2002's are.

Order. Top, then left, by the shape's own box: four s32 at +6 (xs, ys, xe, ye; arranged, its top the smaller y), a
shape whose box passes the stream last in list order. Group members carry their own boxes (libmspub: shape transforms
are not compounded with the group's).

Text, Publisher 2000. A text box (type 0x08) names a Quill story by the u16 at +0x58; the stories, their bold and
the links are read as in 2002 and later (`pub_quill`). A table (type 0x0A; libmspub reads none, an UNKNOWN_CHUNK)
names its story by the u16 at +0x66, an offset seen in files, not ported: the story is given as paragraphs, its rows
and cells not read, counted `table` once a table (as 2002's fallback for a table it cannot lay out).

Text, Publisher 97/98 (`text_boxes`). The u32 at 0x12 is the offset of a text header whose u32s at +14 and +18 are the
text's start and end in Contents; a text box (type 0x00) names a box by the u16 at +0x46, box k being the k-th run of
the text ended by 0x0C (the end of the text ends the last; libmspub drops text after the last 0x0C). Its bytes are
8-bit, decoded with `text.decode_8bit(data, 0x0409)`; CR LF ends a paragraph, a lone CR is dropped (libmspub), 0x0B
and LF break the line and other control characters go (`pub_quill.paragraphs`). Character styles (bold) are not
read. When more than `text.GARBLED` (5%) of the decoded text is U+FFFD or control characters (`text.garble_ratio`,
the text's own breaks aside) the Doc carries the `garbled` warning, the one `reader.convert` gives from the Markdown;
no skip kind.

Pictures. A picture shape's data is the first 0x21 chunk naming it as parent (`assignShapeImgIndex`). In Publisher
2000 its bytes, a u32 length at +4 then the bytes from +8, are the image (a WMF: an asset, not viewable), read once a
document by offset and charged per placement to `limits.IMAGE_BYTES`; bytes past the chunk's span (the next chunk
offset, the trailer or the stream's end), past what is left of IMAGE_BYTES or no image `images.sniff` knows are
counted `image`. In Publisher 97/98 pictures are not read: each picture shape with data that is placed is counted
`image`. A picture shape with no data chunk holds no picture and gives nothing.

Bounds (each read at call time). One `pub_contents.Meter` of `limits.BIN_RECORDS` a document: each directory entry,
each page visited, each shape placed and the Quill styles (2000) or each 97 text box tick it; the first refusal
counts `record` once and every later walk stops. The 97 text range is clamped to the stream: an end past it reads to
the stream's end and a header or start past it, or an end before the start, reads no text, each counted `record`
once. At most `BOXES` 97 boxes are read (the u16 text ids name no more), past it `record` once; each box is charged,
before it is decoded, to a room of `limits.MAX_OUTPUT` (its bytes and `vsdx.LINE` for itself and each line or
paragraph end), past it the rest left out, counted `text` once, as `pub_quill.stories` charges its stories. Every
loop is linear in the bytes it reads; nothing is allocated from a declared length; a picture's bytes are sliced only
within its chunk's span, so distinct data chunks never copy the same bytes. Groups, pages and text ids are walked
with `pub._Pages`'s seen-sets and `limits.MAX_DEPTH`, and its guards wrap each page, master, shape and picture.
"""
import struct

from .. import images, limits
from ..model import Doc
from .. import text as text_mod
from ..text import decode_8bit, garble_ratio
from . import pub, pub_contents, pub_quill, vsdx
from .pub_quill import Story

# MSPUBParser2k::parseContents: a chunk's type, its first u16
PAGE = 0x14
DOCUMENT = 0x15
GROUP = 0x0F
PICTURE = 0x02
PICTURE_DATA = 0x21
OTHER_SHAPES = (0x00, 0x04, 0x05, 0x06, 0x07, 0x08)
TABLE = 0x0A                # a Publisher 2000 table: libmspub's UNKNOWN_CHUNK, read here for its story
# getPageTypeBySeqNum
MASTER_PAGE = 0x109
DUMMY_PAGES = frozenset((0x108, 0x10B, 0x10D, 0x116, 0x119))
BACKGROUND = "Background"   # the master page's title: Publisher 2000 and 98 name it the background
# getTextMarker, getTextIdOffset: {a text-holding chunk's type: where its text id is}; a 2000 table's +0x66 is seen
# in files, not ported
TEXT_2000 = {0x08: 0x58, TABLE: 0x66}
TEXT_97 = {0x00: 0x46}
BOX_AT = 6                  # parseShapeCoordinates: xs, ys, xe, ye (s32)
# MSPUBParser97::parseContentsTextIfNecessary
TEXT_HEAD = 0x12            # the u32 offset of the text header
TEXT_RANGE = 14             # the text's start and end u32 in that header
BOX_END = 0x0C              # getTextInfo: ends a text box
LID = 0x0409                # 97 text's code page (text.decode_8bit)
BOXES = 0x10000             # 97 text boxes read: a u16 text id names no more

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_RANGE = struct.Struct("<II")
_BREAKS = str.maketrans("\r\x0b", "\n\n")   # the text's own breaks, not damage, for the garble ratio


class _Reader2k(pub._Pages):
    """Publisher 2000 and 97/98 chunks on `pub._Pages` (see the module docstring); a subclass sets `stories`."""
    PAGE, DOCUMENT, TABLE = PAGE, DOCUMENT, None
    SHAPES = frozenset((*OTHER_SHAPES, PICTURE, GROUP))
    GROUPS = frozenset((GROUP,))
    DUMMY = DUMMY_PAGES
    TEXT = TEXT_2000

    def __init__(self, contents, ctx):
        super().__init__(contents, ctx, pub_contents.TRAILER_2K)
        self.pictures = {}   # picture shape seqnum -> its first data chunk (assignShapeImgIndex)
        for ref in self.listed:  # at most PUB_CHUNKS, each ticked by the directory
            if ref.type == PICTURE_DATA:
                self.pictures.setdefault(ref.parent, ref)

    def _page_list(self, doc) -> list:
        return []

    def _page(self, ref) -> tuple:
        """(BACKGROUND for the master page, None for another; the chunks naming the page as their parent)."""
        return (BACKGROUND if ref.seqnum == MASTER_PAGE else None), self.kids.get(ref.seqnum, ())

    def _name(self, raw: str) -> str:
        return raw

    def _box(self, ref):
        """(top, left) of the shape's own box at +6, or None when it passes the stream."""
        at = ref.offset + BOX_AT
        if at + pub._BOX.size > len(self.data):
            return None
        xs, ys, xe, ye = pub._BOX.unpack_from(self.data, at)
        return min(ys, ye), min(xs, xe)

    def _tid(self, ref):
        """The text id of a text box or table (TEXT: the id's place by type), or None."""
        off = self.TEXT.get(ref.type)
        if off is None or ref.offset + off + _U16.size > len(self.data):
            return None
        return _U16.unpack_from(self.data, ref.offset + off)[0]


class _Reader2000(_Reader2k):
    """Publisher 2000: Quill stories, tables as their stories' paragraphs and WMF pictures."""
    TABLE = TABLE
    SHAPES = _Reader2k.SHAPES | {TABLE}

    def __init__(self, contents, quill, ctx):
        super().__init__(contents, ctx)
        self._quill(quill)
        self.wmfs = {}   # data chunk offset -> its image bytes or None

    def _table(self, ref, story) -> list:
        """A table's story as paragraphs, its structure not read: counted `table`."""
        self.ctx.skipped["table"] += 1
        return list(story.paras)

    def _picture(self, ref) -> list:
        data = self.pictures.get(ref.seqnum) if ref.type == PICTURE else None
        if data is None:
            return []
        with self.ctx.guard("image"):
            return self._place(self._wmf(data.offset))
        return []

    def _wmf(self, at: int):
        """The image bytes of the data chunk at `at`, read once a document (see the module docstring), or None."""
        if at not in self.wmfs:
            raw = None
            if at + 8 <= len(self.data):
                n = _U32.unpack_from(self.data, at + 4)[0]
                if at + 8 + n <= self._span(at) and self.img + n <= limits.IMAGE_BYTES:
                    raw = self.data[at + 8:at + 8 + n]
                    if images.sniff(raw) is None:
                        raw = None
            self.wmfs[at] = raw
        return self.wmfs[at]


class _Reader97(_Reader2k):
    """Publisher 97/98: 8-bit text boxes in Contents; pictures counted."""
    TEXT = TEXT_97

    def __init__(self, contents, ctx):
        super().__init__(contents, ctx)
        self.stories, self.garbled = text_boxes(contents, ctx, self.meter)

    def _picture(self, ref) -> list:
        if ref.type == PICTURE and ref.seqnum in self.pictures:
            self.ctx.skipped["image"] += 1
        return []


def _text_range(contents):
    """(start, end, cut) of Publisher 97's text in Contents, the end clamped to the stream (`cut` when it was: a start
    past the stream then reads nothing), or None when the header is past the stream or the end before the start."""
    n = len(contents)
    if TEXT_HEAD + _U32.size > n:
        return None
    at = _U32.unpack_from(contents, TEXT_HEAD)[0] + TEXT_RANGE
    if at + _RANGE.size > n:
        return None
    start, end = _RANGE.unpack_from(contents, at)
    if start > end:
        return None
    return start, min(end, n), end > n


def text_boxes(contents, ctx, meter) -> tuple:
    """({text id: Story}, garbled) of Publisher 97's text boxes (see the module docstring)."""
    out = {}
    span = _text_range(contents)
    if span is None or span[2]:
        ctx.skipped["record"] += 1
    if span is None:
        return out, False
    pos, end, _ = span
    room, line = limits.MAX_OUTPUT, vsdx.LINE
    bad = total = 0.0
    index = 0
    while pos < end:
        if index >= BOXES:
            ctx.skipped["record"] += 1
            break
        if not meter.tick():
            break
        stop = contents.find(BOX_END, pos, end)
        stop = end if stop < 0 else stop
        breaks = sum(contents.count(c, pos, stop) for c in (b"\r", b"\n", b"\x0b"))
        cost = stop - pos + line * (1 + breaks)
        if cost > room:
            ctx.skipped["text"] += 1
            break
        room -= cost
        raw = decode_8bit(memoryview(contents)[pos:stop], LID)
        text = "\r".join(part.replace("\r", "") for part in raw.split("\r\n"))
        plain = text.translate(_BREAKS)
        bad += garble_ratio(plain) * len(plain)
        total += len(plain)
        out[index] = Story(index, index, text, pub_quill.paragraphs(text))
        index += 1
        pos = stop + 1
    return out, total > 0 and bad / total > text_mod.GARBLED


def parse_2000(quill, contents, ctx) -> Doc:
    """Publisher 2000: `contents` (the Contents stream) and `quill` (the Quill stream's bytes)."""
    return _Reader2000(contents, quill, ctx).run()


def parse_97(contents, ctx) -> Doc:
    """Publisher 97/98: the Contents stream alone; a garbled text adds the `garbled` warning."""
    reader = _Reader97(contents, ctx)
    doc = reader.run()
    if reader.garbled:
        doc.warnings.append("garbled")
    return doc
