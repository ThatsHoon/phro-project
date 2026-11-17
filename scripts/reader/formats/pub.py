# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub 0477b85184e02dc5276582d9cea969208d56c655, src/lib/MSPUBParser.cpp, MSPUBCollector.cpp, EscherContainerType.h, EscherFieldIds.h, TableInfo.h.
"""Publisher 2002 and later (pub) to the document model: one section per page in the document's page order, each
page's text boxes, tables and pictures in reading order with their bold, then the master pages, the scratch area
(hidden) and a links line.

Ported from libmspub (MPL-2.0) at 0477b85184e02dc5276582d9cea969208d56c655, `src/lib/`: `MSPUBParser.cpp` (`parse`,
`parseContents`, `parseDocumentChunk`, `parsePageChunk`, `parsePageShapeList`, `parseShape` with its table branch,
`getPageTypeBySeqNum`, `parseEscher`, `parseShapeGroup`, `parseEscherShape`, `extractEscherValues`,
`getEscherElementTailLength`, `getEscherElementAdditionalHeaderLength`), `MSPUBCollector.cpp` (`go`, `writePage`,
`assignShapesToPages`, `designateMasterPage`, `createTableLayout`, `mapTableTextToCells`), `EscherContainerType.h`,
`EscherFieldIds.h` and `TableInfo.h`. The Contents blocks and chunk directory are `pub_contents`'s, the Quill stories,
bold, table cells and links `pub_quill`'s, the BStore and BLIPs `officeart`'s.

Version. The Contents stream starts `E8 AC xx 00`: 0x2C is 2002 and later, read here; 0x22 is Publisher 2000 (with a
Quill stream) or 97/98 (without), read by `pub_2k` on this module's `_Pages`; any other byte is `unsupported`, and a
stream not starting E8 AC is damaged. A compound file with no Contents stream goes to
`cfb_route.route(cf, "pub", "no Contents stream")`. A 2002+ file needs the Quill stream (`Quill/QuillSub/CONTENTS`)
and a DOCUMENT chunk, as libmspub does; without either it is damaged. A stream the compound file cannot give (a broken
sector chain) is damaged, the detail naming it ("Contents stream", "Quill/QuillSub/CONTENTS stream",
"Escher/EscherStm stream", "Escher/EscherDelayStm stream"); the readers run once the streams are read, so their own
errors are never taken for a damaged stream.

Pages. The DOCUMENT chunk's page list (block 0x02, its entries of id 0) gives the order, each seqnum once; with no
list, the PAGE chunks in seqnum order (libmspub's `go`). Of these, a PAGE chunk whose block 0x0E (THIS_MASTER_NAME)
holds a byte other than 0 is a master, and the DUMMY seqnums 0x10D, 0x110, 0x113 and 0x117 (`getPageTypeBySeqNum`)
are the scratch area; every other listed page is numbered in turn, `Section("page", number=N)`, and given only when
it has text (libmspub writes a page only with shapes). A page's block 0x02 (PAGE_SHAPES) lists its shapes (entries of
type 0x70). Block 0x0D, the master a page applies, is not followed: the master's shapes are given once, in its own
section, never again on the pages naming it, so no master chain is walked. Masters follow the normal pages, in
directory order, as `Section("master", title=name)` (the name UTF-16LE, charged to the room; past it the title is
left out, counted `text`). Then the scratch area: the shapes of the DUMMY pages, in directory order, in one
`Section("page", title="Scratch area", hidden="hidden")`. A chunk ends at the next chunk's offset (or the trailer's
or the stream's end): a declared length past that is cut there, counted `record` once a document, and the walk of a
chunk so cut is not counted again when a block runs past the cut.

The offset cache. Each page, shape and CELLS chunk offset is walked once a document, however many directory entries
name it: what a walk keeps (a page's master name and 0x70 shape entries, a shape's text id, a table's rows, columns
and CELLS seqnum, a CELLS chunk's cells) is cached by offset. The DOCUMENT chunk's page list is walked once, apart.
The cache holds at most one entry per directory offset (`pub_contents.PUB_CHUNKS`), and its lists one item per block
walked, so at most `limits.BIN_RECORDS` items in all (each block ticks the meter); a CELLS chunk keeps at most
`limits.TABLE_NODES` cells, and one listing more is kept as too many (its tables counted `table`).

Shapes. A listed shape chunk (SHAPE, ALTSHAPE, TABLE; libmspub's `parseShape`) gives the Quill story its block 0x27
(SHAPE_TEXT_ID) names, the last one winning; a GROUP or LOGO chunk has no text and stands for its members, the
chunks whose directory parent it is, in directory order, groups inside included. This differs from libmspub, which
takes a group's members from the Escher SpgrContainer nesting (`beginGroup`, `setCurrentGroupSeqNum`); the two agree
where Publisher writes a member's directory parent as its group, as on every bench file. Each shape is placed once
in the document (where first met) and each story given once (a story two shapes name follows the first); a shape
naming no story gives no text, uncounted. The story's `Para`s carry its bold (`pub_quill`). A shape's picture, when
it has one, comes before its text.

Tables. A TABLE chunk's blocks 0x66 and 0x67 give its rows and columns, 0x6B the seqnum of its CELLS chunk (0x63),
whose block 0x02 lists one item (id 0) a cell: sub-blocks 0x01 and 0x02 its start and end row, 0x03 and 0x04 its
start and end column (0 when absent). The story is cut into cells by its `TCD ` bit (`pub_quill.cell_spans`), the
k-th cell text going to the k-th cell of the CELLS list, as libmspub's `mapTableTextToCells`; each cell's paragraphs
keep the story's bold (`pub_quill.cell_paras`). The grid kept is the rows and columns where a cell starts (a span
counts the kept ones it covers, as hancom's `place`), so the model holds no more places than cells. A cell's end
past the grid is cut to it, an end before its start is a span of one; cells are laid out in grid order, and a cell
whose start another covers adds its paragraphs to that cell, a span reaching into another cell is cut short there,
and a kept place no cell covers is an empty cell (libmspub leaves such a place covered). Cell texts past the last
cell, or text past the `TCD ` bit's last cell, follow the table as paragraphs. Any of these counts `record` once for
the table; fewer texts than cells leave the last cells empty, uncounted. A table without rows, columns, a CELLS
chunk, a cell or a `TCD ` bit, one whose rows x columns pass `limits.GRID_SLOTS`, or one whose cells pass what is
left of `limits.TABLE_NODES` (the document's: each table charges its cells) gives its story as paragraphs, counted
`table`; the grid is never allocated from the declared counts: the layout's one array is the kept places (kept rows
x kept columns, no more than the declared rows x columns, so within `limits.GRID_SLOTS`, checked before), 4 bytes each.

Pictures. The Escher walk (below) records each ClientData shape seqnum's SpContainer, with an anchor or without;
`officeart.shape_pib` reads its pib (0x4104), a 1-based index into the BStore of the stream's DggContainer
(`officeart.bstore`). The BLIP is the FBSE's own, or the delay stream's (`Escher/EscherDelayStm`) at its foDelay
(`officeart.fbse_delay`), read by `officeart.blip_image` under one `flate.Budget` of `limits.IMAGE_BYTES` a document.
As doc's `delayed`, each delay record read is charged its 8 + length bytes against the delay stream's length before it
is read: foDelay offsets naming records nested in one another share their bytes, so only the first that fits is read
and the others are None, unread (each walk stays linear in the stream). PNG and JPEG are `Image`s, a DIB (made a BMP
by `officeart.dib_to_bmp`) becomes a PNG through `images.bmp_to_png`, or stays a BMP when that refuses it (an RLE or
16-bit DIB), and WMF and EMF are kept as they are (BMP, WMF and EMF are assets, not viewable). Each BLIP is decoded
once a document (by pib, and by its place, so FBSEs naming one delay BLIP share it and charge it once). Each placement
is charged to `limits.IMAGE_BYTES` (copies counted, the PNG's length after conversion); past it, or for a pib outside
the BStore, an unreadable BLIP, a delay record past the delay read or a kind Assets does not keep, the placement is
counted `image`. Once the budget is spent no BLIP is decoded or converted, and a BLIP whose bytes alone pass what is
left is not converted.

Order. Top, then left, by the shape's box as libmspub's `parseEscherShape` reads it (the Contents shape chunk holds
only a width and height): the `Escher/EscherStm` DgContainers' SpgrContainers, walked as `parseShapeGroup`, each
SpContainer's ClientData (0xF011) value 0x6801 naming the Contents shape and its ClientAnchor (0xF010: values 0x2001
to 0x2004, signed) or ChildAnchor (0xF00F: four s32, mapped from the group's FSPGR coordinate system to the group's
box, as libmspub does). The DggContainer and DgContainers are followed by 4 more bytes, and a ClientAnchor's and
ClientData's values start 4 bytes in (`getEscherElementTailLength`, `getEscherElementAdditionalHeaderLength`). The
box is arranged (its top the smaller y); rotation is not read. A shape with no box follows the placed ones in list
order. This walk is the one walk over Publisher's SpContainers.

Connectors. The same walk reads each DgContainer's SolverContainer (0xF005, `officeart.rules`: spidA the start,
spidB the end, spidC the connector) and, for each SpContainer whose ClientData names a shape, its first FSP's spid,
flags and shape type and its first FOPT's arrowheads (`officeart.arrows`, absent is none). A spid maps to the shape
seqnum of the first SpContainer naming it. A page is one diagram scope (`diagram.Diagram`), its slots each placed
shape's (seqnum, blocks); a master page and each scratch page are one too. A connector is a shape that a rule names as
spidC (the first such rule its glue) or whose type is a connector (`officeart.CONNECTOR_TYPES`) or a line
(`officeart.LINE_TYPE`, a candidate only with an arrowhead); it runs from its box's start corner after the FSP flips to
the opposite one. An end keeps its glue only when that end's seqnum is among the connector's page's shapes, so a rule
across pages, or naming spid 0, leaves that end loose for `diagram`'s inference (`connector_loose` when no box catches
it). Its key is its seqnum and its slot's blocks go with it (`diagram` makes them its label, and the slot goes with
the edge). A rule whose spidC no shape has is drawn only when both ends are on the page, as `("rule", spidC)` with
glued ends and arrowheads not known. Nodes take their type's kind
(`officeart.KINDS`) and their arranged box. A page with neither is given exactly as before. Edges go through one
`diagram.ConnectionBudget` a document. Publisher 2000 and 97/98 (`pub_2k`) have no solver, and their line chunk no
arrowhead field, so their pages have no diagram.

Links. `pub_quill.links` give one `Para([Text("Links: "), Link(u, [Text(u)]), Text(", "), ...])` at the end. VBA: a
storage named VBA anywhere (Publisher's `VBA/`, Word's `Macros/VBA/`) counts `vba` once; macros are never read.

Bounds (each read at call time). One `pub_contents.Meter` of `limits.BIN_RECORDS` a document: the directory, every
Contents block walked (page list, page chunks, shape, table and CELLS chunks), each page visited, each shape placed,
each Escher record, value and connector rule, the Quill stories' styles and each step of a table's layout (a place
checked or marked, a cell or gap of a row) tick it; the first refusal counts `record` once and every later walk stops (a
table it cuts gives its story as paragraphs, uncounted again). The Escher walk, which only orders text and finds
pictures, may spend no more than half of `limits.BIN_RECORDS` (`_Share`): past that the boxes found so far are kept and
the pages are still read, `record` counted once a document. `officeart.shape_pib` re-reads only the children of an
SpContainer the walk ticked, once (a seqnum names one), and `officeart.bstore` is walked once a document within
`officeart.MAX_RECORDS`. The page list and the shapes are walked with seen-sets, so a page or shape listed twice, naming
itself or a cycle of groups is read once; group members past `limits.MAX_DEPTH` levels, and Escher groups nested past
it, are left out, counted `depth` once. `ctx.guard("page")` wraps each page and scratch page, `ctx.guard("master_page")`
each master, `ctx.guard("shape")` each shape, each diagram shape and connector registered and each page's place(), and
`ctx.guard("image")` each picture; a page's blocks are kept only when its guard succeeds. Each story given is charged to
one room of `limits.MAX_OUTPUT` a document, as the stories are charged (`pub_quill`): its characters and `vsdx.LINE` for
itself, each paragraph end or line break and each bold run; past the room it is left out, counted `text`. Each link is
charged its characters and LINE; past the room the rest are left out, counted `link` once. Nothing is allocated from a
declared count; loops over the directory's references are bounded by `pub_contents.PUB_CHUNKS`.
"""
import struct
from array import array
from bisect import bisect_left, bisect_right
from itertools import islice

from .. import diagram, images, limits, officeart
from ..containers import flate
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Cell, Doc, Image, Link, Para, Section, Table, Text
from . import cfb_route, pub_contents, pub_quill, vsdx, zip_route
from .pub_contents import ALTSHAPE, CELLS, DOCUMENT, GROUP, LOGO, PAGE, SHAPE, STRING_CONTAINER, TABLE

CONTENTS = "Contents"
QUILL = "Quill/QuillSub/CONTENTS"
ESCHER = "Escher/EscherStm"
DELAY = "Escher/EscherDelayStm"
HEAD = b"\xE8\xAC"
V2002 = 0x2C
V2000 = 0x22

DUMMY_PAGES = frozenset((0x10D, 0x110, 0x113, 0x117))   # getPageTypeBySeqNum: the scratch area
SHAPES = frozenset((SHAPE, ALTSHAPE, GROUP, TABLE, LOGO))
GROUPS = frozenset((GROUP, LOGO))
# MSPUBBlockID.h
PAGE_LIST = 0x02        # DOCUMENT_PAGE_LIST, in the DOCUMENT chunk
PAGE_SHAPES = 0x02      # in a PAGE chunk
THIS_MASTER_NAME = 0x0E
SHAPE_TEXT_ID = 0x27
TABLE_NUM_ROWS = 0x66
TABLE_NUM_COLS = 0x67
TABLE_CELLS_SEQNUM = 0x6B
CELL_LIST = 0x02        # in a CELLS chunk: the cells, one item (id 0) each
CELL_FIELDS = (0x01, 0x02, 0x03, 0x04)   # start row, end row, start column, end column
SHAPE_SEQNUM = pub_contents.SHAPE_SEQNUM  # the block type of a page's shape list entries
# EscherContainerType.h, EscherFieldIds.h
DGG = 0xF000
DG = 0xF002
SPGR = 0xF003
SP = 0xF004
FSPGR = 0xF009
CHILD_ANCHOR = 0xF00F
CLIENT_ANCHOR = 0xF010
CLIENT_DATA = 0xF011
FIELD_SHAPE_ID = 0x6801
FIELD_BOX = (0x2001, 0x2002, 0x2003, 0x2004)   # XS, YS, XE, YE
_TAIL = {DGG: 4, DG: 4}
_VALUES_AT = 4          # a ClientAnchor's or ClientData's values start past its length, written again
_HEAD = struct.Struct("<HHI")
_PAIR = struct.Struct("<HI")
_BOX = struct.Struct("<4i")
_U32 = struct.Struct("<I")
_TOO_MANY = object()    # a CELLS chunk holding more than limits.TABLE_NODES cells (cached as such)


def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def _records(data, start: int, end: int, meter) -> iter:
    """(type, body, size) of the Escher records in data[start:end], each container's tail skipped; each record ticks
    the meter, and a body past `end` (never past the data: callers pass its length or a checked body) ends the walk."""
    pos = start
    while pos + 8 <= end:
        if not meter.tick():
            return
        _, typ, size = _HEAD.unpack_from(data, pos)
        body = pos + 8
        if size > end - body:
            return
        yield typ, body, size
        pos = body + size + _TAIL.get(typ, 0)


def _values(data, body: int, size: int, meter) -> dict:
    """{id: value} of a ClientAnchor's or ClientData's (u16 id, u32 value) pairs, the last of an id winning."""
    out = {}
    pos, end = body + _VALUES_AT, body + size
    while pos + _PAIR.size <= end:
        if not meter.tick():
            break
        key, value = _PAIR.unpack_from(data, pos)
        out[key] = value
        pos += _PAIR.size
    return out


class _Boxes:
    """The Escher walk of `boxes`: {Contents shape seqnum: (top, left)} in `out`, and in `sp` {Contents shape seqnum:
    the offset of the SpContainer whose ClientData names it} for every shape, with an anchor or without. For the
    diagram: `full` {seqnum: (x0, y0, x1, y1)}, the box arranged; `kinds` {seqnum: (its FSP's shape type and flags,
    the (start, end) arrowheads of its first FOPT)}; `spids` {FSP spid other than 0: the seqnum of the first
    SpContainer naming it}; `rules`, the (spidA, spidB, spidC) of the DgContainers' SolverContainers, each rule
    ticking the meter."""

    def __init__(self, data, meter, ctx, limit=None):
        self.data, self.ctx = data, ctx
        self.meter = meter if limit is None else _Share(meter, limit)
        self.out = {}
        self.sp = {}
        self.full = {}
        self.kinds = {}
        self.spids = {}
        self.rules = []
        self.deep = False

    def walk(self):
        for typ, body, size in _records(self.data, 0, len(self.data), self.meter):
            if typ != DG:
                continue
            for t, b, s in _records(self.data, body, body + size, self.meter):
                if t == SPGR:
                    self.group(b, b + s, [0, 0, 0, 0], [0, 0, 0, 0], 1)
                elif t == officeart.SOLVER:
                    for rule in officeart.rules(self.data, b - 8, b + s):
                        if not self.meter.tick():
                            break
                        self.rules.append(rule)
        return self.out

    def group(self, start: int, end: int, cs: list, box: list, depth: int):
        """An SpgrContainer's body: its shapes, and its groups one level deeper; `cs` and `box` are the coordinate
        system and box its group shape sets (libmspub passes them on by value, and each shape may set them)."""
        if depth > limits.MAX_DEPTH:
            if not self.deep:
                self.deep = True
                self.ctx.skipped["depth"] += 1
            return
        cs, box = list(cs), list(box)
        for typ, body, size in _records(self.data, start, end, self.meter):
            if typ == SPGR:
                self.group(body, body + size, cs, box, depth + 1)
            elif typ == SP:
                self.shape(body, size, cs, box)

    def shape(self, body: int, size: int, cs: list, box: list):
        """One SpContainer: an FSPGR sets the group's coordinate system; the ClientData's shape seqnum gets this
        SpContainer's offset, its first FSP's spid, flags and shape type, its first FOPT's arrowheads, and the box of
        the first anchor, which becomes the group's box when this shape set the coordinate system."""
        data = self.data
        parent = tuple(cs)
        fspgr = anchor = client = fsp = fopt = None
        for typ, b, s in _records(data, body, body + size, self.meter):
            if typ == officeart.FSP and fsp is None:
                fsp = (*officeart.fsp(data[b:b + min(s, 8)]), _HEAD.unpack_from(data, b - 8)[0] >> 4)
            elif typ == officeart.FOPT and fopt is None:
                fopt = officeart.arrows(data, _HEAD.unpack_from(data, b - 8)[0] >> 4, b, s)
            elif typ == FSPGR and fspgr is None:
                fspgr = (b, s)
            elif typ == CLIENT_DATA and client is None:
                client = (b, s)
            elif typ in (CLIENT_ANCHOR, CHILD_ANCHOR) and anchor is None:
                anchor = (typ, b, s)
        defines = fspgr is not None and fspgr[1] >= _BOX.size
        if defines:
            xs, ys, xe, ye = _BOX.unpack_from(data, fspgr[0])
            cs[:] = [min(xs, xe), min(ys, ye), max(xs, xe), max(ys, ye)]
        if client is None:
            return
        seq = _values(data, *client, self.meter).get(FIELD_SHAPE_ID)
        if seq is None:
            return
        self.sp[seq] = body - 8
        spid, flags, stype = fsp or (0, 0, 0)
        self.kinds[seq] = (stype, flags, fopt or (False, False))
        if spid:
            self.spids.setdefault(spid, seq)
        if anchor is None:
            return
        typ, b, s = anchor
        if typ == CLIENT_ANCHOR:
            v = _values(data, b, s, self.meter)
            xs, ys, xe, ye = (_s32(v.get(k, 0)) for k in FIELD_BOX)
        elif s >= _BOX.size:
            cx0, cy0, cx1, cy1 = _BOX.unpack_from(data, b)
            sx = (box[2] - box[0]) / ((parent[2] - parent[0]) or 1)
            sy = (box[3] - box[1]) / ((parent[3] - parent[1]) or 1)
            xs, xe = (int((c - parent[0]) * sx) + box[0] for c in (cx0, cx1))
            ys, ye = (int((c - parent[1]) * sy) + box[1] for c in (cy0, cy1))
        else:
            return
        got = [min(xs, xe), min(ys, ye), max(xs, xe), max(ys, ye)]
        self.out[seq] = (got[1], got[0])
        self.full[seq] = tuple(got)
        if defines:
            box[:] = got


class _Share:
    """The Escher walk's share of the document meter: it stops once the meter's use reaches `limit` (uncounted here:
    `_Reader.run` counts it), and ticks the meter itself below that."""

    def __init__(self, meter, limit: int):
        self.meter, self.limit, self.cut = meter, limit, False

    def tick(self) -> bool:
        if self.meter.used >= self.limit:
            self.cut = True
            return False
        return self.meter.tick()


class _Quiet:
    """The document meter for the walk of a chunk whose span was cut (counted `record` already): it ticks the meter,
    and a block running past the cut ends the walk without counting it again."""

    def __init__(self, meter):
        self.meter = meter

    @property
    def spent(self) -> bool:
        return self.meter.spent

    def tick(self) -> bool:
        return self.meter.tick()

    def cut(self):
        pass


def boxes(data, meter, ctx) -> dict:
    """{Contents shape seqnum: (top, left)} from an Escher/EscherStm stream (see the module docstring)."""
    return _Boxes(data, meter, ctx).walk()


def _cost(story) -> int:
    """A story's charge, as `pub_quill.stories` charges it: its characters, and LINE for itself, each paragraph end or
    line break and each bold run."""
    text = story.text
    runs = sum(isinstance(i, Bold) for p in story.paras for i in p.inlines)
    breaks = text.count("\r") + text.count("\n") + text.count("\x0b")
    return len(text) + vsdx.LINE * (1 + breaks + runs)


class _Pages:
    """What the Publisher readers share (`_Reader` here, `pub_2k`'s for Publisher 2000 and 97/98): the document's
    meter and directory, the pages in order with their masters and scratch area, each page's shapes (groups expanded,
    each once) in reading order under their guards, each story given once and charged to the room, the master titles,
    the pictures charged to IMAGE_BYTES and the links line. A subclass names its chunk types and DUMMY seqnums, sets
    `stories` and gives the hooks `_page_list`, `_page`, `_box`, `_tid`, `_name`, `_picture` and, when TABLE is a
    type, `_table`."""
    PAGE, DOCUMENT, TABLE = PAGE, DOCUMENT, TABLE
    SHAPES, GROUPS, DUMMY = SHAPES, GROUPS, DUMMY_PAGES
    escher_cut = False   # an Escher walk cut short by its share of the meter (2002+ only)
    urls = ()            # the links line's URLs

    def __init__(self, contents, ctx, trailer=pub_contents.TRAILER):
        self.data, self.ctx = contents, ctx
        self.meter = pub_contents.Meter(ctx)
        self.refs = {}   # seqnum -> its reference (a 2002 seqnum is its directory index, so each is one chunk)
        self.kids = {}   # seqnum -> the seqnums naming it as their parent, in directory order (a group's members)
        self.listed = pub_contents.directory(contents, ctx, trailer=trailer, meter=self.meter)  # at most PUB_CHUNKS
        bounds = {len(contents)}
        for ref in self.listed:
            self.refs[ref.seqnum] = ref
            bounds.update((ref.offset, ref.end))
            if ref.parent is not None:
                self.kids.setdefault(ref.parent, []).append(ref.seqnum)
        self.bounds = sorted(bounds)  # every chunk offset, the trailer's and the stream's end: a chunk ends at the next
        self.room = limits.MAX_OUTPUT
        self.placed = set()  # shapes placed
        self.told = set()    # text ids given
        self.deep = False
        self.img = 0         # image bytes placed so far (IMAGE_BYTES)

    def _quill(self, quill):
        """Read the Quill stream: its bits (kept), its stories (on the meter) and the links."""
        self.bits = pub_quill.bits(quill, self.ctx)
        self.stories = pub_quill.stories(self.bits, self.ctx, meter=self.meter)
        self.urls = pub_quill.links(self.bits)

    def _span(self, at: int) -> int:
        """Where the chunk at `at` ends: the first chunk offset, trailer or stream end past it."""
        k = bisect_right(self.bounds, at)
        return self.bounds[k] if k < len(self.bounds) else len(self.data)

    def _place(self, data) -> list:
        """[Image(data)] charged to limits.IMAGE_BYTES; [] after counting `image` when `data` is None or past it."""
        if data is not None and self.img + len(data) <= limits.IMAGE_BYTES:
            self.img += len(data)
            return [Image(data)]
        self.ctx.skipped["image"] += 1
        return []

    def _expand(self, seqs) -> list:
        """The shape references of a page's list, each group replaced by its members, each shape once a document."""
        flat = []
        for seq in seqs:
            stack = [(seq, 0)]
            while stack:
                s, depth = stack.pop()
                ref = self.refs.get(s)
                if s in self.placed or ref is None or ref.type not in self.SHAPES:
                    continue
                if depth > limits.MAX_DEPTH:
                    if not self.deep:
                        self.deep = True
                        self.ctx.skipped["depth"] += 1
                    continue
                if not self.meter.tick():
                    return flat
                self.placed.add(s)
                if ref.type in self.GROUPS:
                    stack.extend((k, depth + 1) for k in reversed(self.kids.get(s, ())))
                else:
                    flat.append(ref)
        return flat

    def _shapes(self, seqs) -> list:
        """The blocks of a page's shapes, in reading order (`_box`: top, then left; no box last, in list order): each
        shape's picture, then its text, a (seqnum, blocks) slot that `_drawn` lays out."""
        flat = self._expand(seqs)
        at = [self._box(r) for r in flat]
        placed = sorted((b, i) for i, b in enumerate(at) if b is not None)
        order = [flat[i] for _, i in placed] + [r for r, b in zip(flat, at) if b is None]
        slots = []
        for ref in order:
            with self.ctx.guard("shape"):
                got = self._picture(ref)
                got.extend(self._text(ref))
                slots.append((ref.seqnum, got))
        return self._drawn(slots)

    def _drawn(self, slots) -> list:
        """A page's blocks: each slot's in turn (Publisher 2000 and 97/98 have no connectors)."""
        return [b for _, blocks in slots for b in blocks]

    def _text(self, ref) -> list:
        """The paragraphs of the story a shape names, or its table, once a document, charged to the room."""
        tid = self._tid(ref)
        story = self.stories.get(tid)
        if story is None or tid in self.told:
            return []
        self.told.add(tid)
        cost = _cost(story)
        if cost > self.room:
            self.ctx.skipped["text"] += 1
            return []
        self.room -= cost
        if ref.type == self.TABLE:
            return self._table(ref, story)
        return list(story.paras)

    def _title(self, raw) -> str:
        """A master's title (`_name` of what `_page` gave for it), charged to the room; past it "" (counted text)."""
        name = self._name(raw)
        cost = len(name) + vsdx.LINE
        if cost > self.room:
            self.ctx.skipped["text"] += 1
            return ""
        self.room -= cost
        return name

    def _links(self) -> list:
        inlines = [Text("Links: ")]
        for url in self.urls:
            cost = len(url) + vsdx.LINE
            if cost > self.room:
                self.ctx.skipped["link"] += 1
                break
            self.room -= cost
            if len(inlines) > 1:
                inlines.append(Text(", "))
            inlines.append(Link(url, [Text(url)]))
        return [Para(inlines)] if len(inlines) > 1 else []

    def run(self) -> Doc:
        doc = next((r for r in self.refs.values() if r.type == self.DOCUMENT), None)
        if doc is None:
            raise ReadError("damaged", "no document chunk")
        pages = [r for r in self.refs.values() if r.type == self.PAGE]   # directory order
        order = self._page_list(doc) or sorted(r.seqnum for r in pages)
        out, seen, number = [], set(), 0
        for seq in order:
            ref = self.refs.get(seq)
            if seq in seen or ref is None or ref.type != self.PAGE or seq in self.DUMMY:
                continue
            seen.add(seq)
            if not self.meter.tick():
                break
            with self.ctx.guard("page"):
                master, shapes = self._page(ref)
                if master is None:
                    number += 1
                    blocks = self._shapes(shapes)
                    if blocks:
                        out.append(Section("page", number=number, blocks=blocks))
        for ref in pages:
            if ref.seqnum in self.DUMMY or not self.meter.tick():
                continue
            with self.ctx.guard("master_page"):
                master, shapes = self._page(ref)
                if master is not None:
                    blocks = self._shapes(shapes)
                    if blocks:
                        out.append(Section("master", title=self._title(master), blocks=blocks))
        scratch = []
        for ref in pages:
            if ref.seqnum not in self.DUMMY or not self.meter.tick():
                continue
            with self.ctx.guard("page"):
                blocks = self._shapes(self._page(ref)[1])
                scratch.extend(blocks)
        if scratch:
            out.append(Section("page", title="Scratch area", blocks=scratch, hidden="hidden"))
        out.extend(self._links())
        if self.escher_cut and not self.meter.spent:  # the Escher walk's share ran out (record counted once)
            self.ctx.skipped["record"] += 1
        return Doc(blocks=out)


class _Reader(_Pages):
    """Publisher 2002 and later (see the module docstring)."""

    def __init__(self, contents, quill, escher, ctx, delay=b""):
        super().__init__(contents, ctx)
        self._quill(quill)
        self.tcd = None      # TCD bit id -> bit, made at the first table
        walk = _Boxes(escher, self.meter, ctx, self.meter.used + limits.BIN_RECORDS // 2)
        self.boxes = walk.walk() if escher else {}
        self.sps = walk.sp   # shape seqnum -> its SpContainer's offset in the Escher stream
        self.full, self.kinds, self.spids = walk.full, walk.kinds, walk.spids
        self.rule_of = {}    # connector seqnum -> (spidA, spidB) of the first rule whose spidC names it
        self.glue = {}       # start seqnum -> [(rule index, start seqnum, end seqnum, spidC)] of the rules whose spidC
        for k, (a, b, c) in enumerate(walk.rules):  # no shape has, both ends' spids mapped
            if c in self.spids:
                self.rule_of.setdefault(self.spids[c], (a, b))
            elif a in self.spids and b in self.spids:
                self.glue.setdefault(self.spids[a], []).append((k, self.spids[a], self.spids[b], c))
        self.connections = diagram.ConnectionBudget(ctx)  # the document's edges, every page's together
        self.escher_cut = walk.meter.cut
        self.escher, self.delay = escher, delay
        self.nodes = limits.TABLE_NODES   # table cells the document may still lay out
        self.chunks = {}     # chunk offset -> what `_read` found there
        self.cell_defs = {}  # CELLS chunk offset -> its cells, or _TOO_MANY
        self.long = False    # a chunk length past its span was cut (counted once)
        self.store = None    # the BStore's file block offsets, read at the first picture
        self.budget = flate.Budget(limits.IMAGE_BYTES)
        self.blips = {}      # pib -> its image bytes or None
        self.decoded = {}    # a BLIP's place (the delay offset or the FBSE's) -> its image bytes or None
        self.delay_read = 0  # delay-stream bytes read so far, at most the stream's length

    def _chunk(self, ref):
        """The blocks of a chunk: a u32 length counting itself, then blocks, to offset + length as libmspub reads, but
        never past the next chunk's offset (a longer length is cut there, counted record once, and that walk's own
        damage not counted again)."""
        at = ref.offset
        if at + 4 > len(self.data):
            return iter(())
        end = at + _U32.unpack_from(self.data, at)[0]
        span = self._span(at)
        meter = self.meter
        if end > span:
            end = span
            meter = _Quiet(meter)
            if not self.long:
                self.long = True
                self.meter.cut()
        return pub_contents.blocks(self.data, at + 4, end, meter=meter)

    def _read(self, ref) -> tuple:
        """(the 0x0E master name's bytes or None, the 0x02 list's 0x70 entries, the last 0x27 text id, the table's
        (rows, columns, CELLS seqnum) or None) of the chunk at `ref.offset`: walked once a document, however many
        directory entries name that offset."""
        got = self.chunks.get(ref.offset)
        if got is None:
            master, shapes, tid, table = None, [], None, None
            for b in self._chunk(ref):
                if b.id == THIS_MASTER_NAME and b.type == STRING_CONTAINER:
                    master = b.data if any(b.data) else None
                elif b.id == PAGE_SHAPES:  # a fixed block has no children
                    shapes.extend(s.data for s in pub_contents.children(self.data, b, meter=self.meter)
                                  if s.type == SHAPE_SEQNUM)
                elif b.id == SHAPE_TEXT_ID:  # a non-int value names no story
                    tid = b.data
                elif b.id in (TABLE_NUM_ROWS, TABLE_NUM_COLS, TABLE_CELLS_SEQNUM) and isinstance(b.data, int):
                    table = list(table or (None, None, None))
                    table[(TABLE_NUM_ROWS, TABLE_NUM_COLS, TABLE_CELLS_SEQNUM).index(b.id)] = b.data
            got = self.chunks[ref.offset] = (master, shapes or (), tid, tuple(table) if table else None)
        return got

    def _page_list(self, doc) -> list:
        """The DOCUMENT chunk's page list, its 0x02 entries of id 0 (a non-int entry names no chunk); read once."""
        out = []
        for b in self._chunk(doc):
            if b.id == PAGE_LIST:
                out.extend(s.data for s in pub_contents.children(self.data, b, meter=self.meter) if s.id == 0)
        return out

    def _page(self, ref) -> tuple:
        """(the master name's bytes, None for a normal page; its shape seqnums) of a PAGE chunk."""
        master, shapes, _, _ = self._read(ref)
        return master, shapes

    def _box(self, ref):
        """(top, left) of a shape from the Escher walk, or None."""
        return self.boxes.get(ref.seqnum)

    def _drawn(self, slots) -> list:
        """A page's blocks: each slot's in turn, unless the page has a connector (a shape a rule names as spidC, of a
        connector type or a line) or a rule whose spidC no shape has with both ends among its shapes; then the page is
        one diagram scope (see the module docstring), each shape and connector registered under guard("shape") and
        place() run under it, the slots' blocks kept when it fails."""
        own = dict(slots)
        conns = {seq for seq in own if self._connector(seq)}
        kept = sorted(r for k in own for r in self.glue.get(k, ()) if r[2] in own)  # in rule order
        if not conns and not kept:
            return [b for _, blocks in slots for b in blocks]
        dia = diagram.Diagram(self.ctx, self.connections)
        for seq, blocks in slots:
            if seq not in conns:
                with self.ctx.guard("shape"):
                    dia.shape(seq, blocks, officeart.KINDS.get(self.kinds.get(seq, (0,))[0], ""), self.full.get(seq))
        for seq in (s for s, _ in slots if s in conns):  # in reading order
            with self.ctx.guard("shape"):
                stype, flags, arrows = self.kinds.get(seq, (0, 0, (False, False)))
                ends = [self.spids.get(e) for e in self.rule_of.get(seq, ())] or [None, None]
                begin, end = (e if e in own else None for e in ends)  # glue holds on the connector's page only
                p0 = p1 = None
                if seq in self.full:
                    x0, y0, x1, y1 = self.full[seq]
                    if flags & officeart.FLIP_H:
                        x0, x1 = x1, x0
                    if flags & officeart.FLIP_V:
                        y0, y1 = y1, y0
                    p0, p1 = (x0, y0), (x1, y1)
                dia.connector(seq, begin, end, p0, p1, arrows, line=stype == officeart.LINE_TYPE, blocks=own[seq])
        for _, a, b, c in kept:
            with self.ctx.guard("shape"):
                dia.connector(("rule", c), a, b)
        out = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            out = dia.place(slots)
        return out

    def _connector(self, seq) -> bool:
        """Whether the shape is a connector: a rule names it as spidC, or its type is a connector's or a line's."""
        stype = self.kinds.get(seq, (0,))[0]
        return seq in self.rule_of or stype == officeart.LINE_TYPE or stype in officeart.CONNECTOR_TYPES

    def _tid(self, ref):
        """The text id (block 0x27) of a shape chunk, or None."""
        return self._read(ref)[2]

    def _name(self, raw: bytes) -> str:
        """A master's name: its 0x0E block's UTF-16LE."""
        return str(memoryview(raw)[:len(raw) // 2 * 2], "utf-16-le", "replace").replace("\0", "").strip()

    # ---- tables

    def _table(self, ref, story) -> list:
        """A TABLE shape's blocks: its Table (and any text past its cells), or its story's paragraphs (see the module
        docstring)."""
        paras = list(story.paras)
        if self.meter.spent:  # its walks were cut short: record counted once already
            return paras
        rows, cols, csn = self._read(ref)[3] or (None, None, None)
        cref = self.refs.get(csn)
        if self.tcd is None:
            self.tcd = pub_quill.table_bits(self.bits)
        bit = self.tcd.get(story.index)
        if (not rows or not cols or cref is None or cref.type != CELLS or bit is None or len(bit) < 0xC
                or rows * cols > limits.GRID_SLOTS):
            self.ctx.skipped["table"] += 1
            return paras
        defs = self._cells(cref)
        if self.meter.spent:
            return paras
        if defs is _TOO_MANY or not defs or len(defs) > self.nodes:
            self.ctx.skipped["table"] += 1
            return paras
        self.nodes -= len(defs)
        got = self._layout(defs, rows, cols, story, bit)
        return paras if got is None else got

    def _cells(self, ref):
        """The (start row, end row, start column, end column) of each cell of a CELLS chunk, in order; _TOO_MANY past
        limits.TABLE_NODES. Walked once a document per offset."""
        got = self.cell_defs.get(ref.offset)
        if got is None:
            got, cap = [], limits.TABLE_NODES
            for b in self._chunk(ref):
                if b.id != CELL_LIST or got is _TOO_MANY:
                    continue
                for item in pub_contents.children(self.data, b, meter=self.meter):
                    if item.id != 0:
                        continue
                    if len(got) >= cap:
                        got = _TOO_MANY
                        break
                    v = [0, 0, 0, 0]
                    for sub in pub_contents.children(self.data, item, 1, meter=self.meter):
                        if sub.id in CELL_FIELDS and isinstance(sub.data, int):
                            v[sub.id - 1] = sub.data
                    got.append(tuple(v))
            self.cell_defs[ref.offset] = got
        return got

    def _layout(self, defs, rows: int, cols: int, story, bit) -> list | None:
        """[Table, paragraphs past its cells] of a table's cells and its story cut by `bit`; None when the meter runs
        out (see the module docstring)."""
        tick = self.meter.tick
        damaged = False
        spans = list(islice(pub_quill.cell_spans(bit, story), len(defs)))
        texts = pub_quill.cell_paras(story, [(a, b) for a, b, _ in spans])
        rest = spans[-1][2] if spans else 0   # where the text past the cells starts
        boxes = []
        for sr, er, sc, ec in defs:
            box = (min(sr, rows - 1), 0, min(sc, cols - 1), 0)
            box = (box[0], min(max(er, box[0]), rows - 1), box[2], min(max(ec, box[2]), cols - 1))
            damaged |= box != (sr, er, sc, ec)
            boxes.append(box)
        rows_at = sorted({b[0] for b in boxes})
        cols_at = sorted({b[2] for b in boxes})
        ri = {v: i for i, v in enumerate(rows_at)}
        ci = {v: i for i, v in enumerate(cols_at)}
        width = len(cols_at)
        # the cell covering each kept place (row-major), -1 for none: kept rows x columns <= rows x cols <= GRID_SLOTS
        taken = array("i", [-1]) * (len(rows_at) * width)
        made = []     # (Cell, kept row, kept column) of each cell laid out
        for _, _, k in sorted((ri[b[0]], ci[b[2]], k) for k, b in enumerate(boxes)):
            if not tick():
                return None
            sr, er, sc, ec = boxes[k]
            r, c = ri[sr], ci[sc]
            blocks = list(texts[k]) if k < len(texts) else []
            owner = taken[r * width + c]
            if owner >= 0:  # its start is covered: its text joins the covering cell
                made[owner][0].blocks.extend(blocks)
                damaged = True
                continue
            rs = bisect_left(rows_at, er + 1) - r
            cs = bisect_left(cols_at, ec + 1) - c
            w = 1
            while w < cs:
                if not tick():
                    return None
                if taken[r * width + c + w] >= 0:
                    damaged = True
                    break
                w += 1
            for dr in range(rs):
                for dc in range(w):
                    if not tick():
                        return None
                    taken[(r + dr) * width + c + dc] = len(made)
            made.append((Cell(blocks, rs, w), r, c))
        grid = []
        for r in range(len(rows_at)):
            row, c = [], 0
            while c < width:
                if not tick():
                    return None
                owner = taken[r * width + c]
                if owner < 0:  # a place no cell covers: an empty cell over the run of such places
                    g = c
                    while c < width and taken[r * width + c] < 0:
                        if not tick():
                            return None
                        c += 1
                    row.append(Cell([], colspan=c - g))
                    damaged = True
                    continue
                cell, r0, c0 = made[owner]
                if r0 == r:
                    row.append(cell)
                c = c0 + cell.colspan
            grid.append(row)
        out = [Table(grid)]
        if rest < len(story.text):
            tail = list(pub_quill.cell_paras(story, [(rest, len(story.text))])[0])
            if tail:
                out.extend(tail)
                damaged = True
        if damaged:
            self.ctx.skipped["record"] += 1
        return out

    # ---- pictures

    def _picture(self, ref) -> list:
        """The Image of a shape whose SpContainer has a pib, charged to IMAGE_BYTES; [] after counting `image` when it
        cannot be given, and for a shape with no picture."""
        at = self.sps.get(ref.seqnum)
        if at is None:
            return []
        pib = officeart.shape_pib(self.escher, at)
        if not pib:
            return []
        with self.ctx.guard("image"):
            return self._place(self._blip(pib))
        return []

    def _blip(self, pib: int):
        """The image bytes of BStore entry `pib` (decoded once a document), or None."""
        if pib not in self.blips:
            data = None
            if self.img < limits.IMAGE_BYTES:  # no work once the budget is spent
                if self.store is None:
                    self.store = officeart.bstore(self.escher)
                if 0 < pib <= len(self.store):
                    off = self.store[pib - 1]
                    at = officeart.fbse_delay(self.escher, off)
                    key = (True, at) if at is not None else (False, off)
                    if key not in self.decoded:
                        self.decoded[key] = self._delayed(at) if at is not None else self._decode(self.escher, off)
                    data = self.decoded[key]
            self.blips[pib] = data
        return self.blips[pib]

    def _delayed(self, at: int):
        """The image bytes of the delay stream's BLIP record at `at`, as doc's `delayed`: the record (8 + its length)
        is charged to the delay read before it is read, and one that passes the stream, or what is left of the delay
        read (records nested in one another), is None, unread."""
        h = officeart.header(self.delay, at)
        size = 8 + h[3] if h is not None else 0
        if not size or at + size > len(self.delay) or self.delay_read + size > len(self.delay):
            return None
        self.delay_read += size
        return self._decode(self.delay, at)

    def _decode(self, data, off: int):
        """The image bytes of the FBSE or BLIP at `off` in `data`: a DIB made a PNG (kept as its BMP, an asset, when
        bmp_to_png refuses it), any other kind as it is; None when unreadable, or when its bytes alone pass what is
        left of IMAGE_BYTES."""
        raw = officeart.blip_image(data, off, None, self.budget)
        if raw is None or self.img + len(raw) > limits.IMAGE_BYTES:
            return None
        if images.sniff(raw) == "bmp":  # a DIB, given its file header by officeart.dib_to_bmp
            raw = images.bmp_to_png(raw) or raw
        return raw


def _vba(name: str) -> bool:
    return "VBA" in name.upper().split("/")[:-1]


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:2] == b"PK":  # a zip named .pub: zip_route picks its parser, never this one
        return zip_route.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a Publisher file")
    got = parse_compound(path, ctx)
    if isinstance(got, str):  # another format's compound file named .pub
        return cfb_route.parse(got, path, ctx)
    return got


def parse_compound(src, ctx) -> Doc | str:
    """Read the compound file `src` (a path or its bytes, nothing is written): the Doc, or the name of the other
    parser `cfb_route` picks for a compound file that holds no Contents stream. `parse` ends in it. Version 0x22 goes
    to `pub_2k`: Publisher 2000 with a Quill stream, 97/98 without (libmspub's `MSPUBDocument::parse`)."""
    from . import pub_2k  # it builds on this module, so it is imported once this one is
    part = "compound file"
    older, quill = False, None
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "pub", "no Contents stream")
            if other is None:
                part = f"{CONTENTS} stream"
                contents = cf.read(CONTENTS)
                if len(contents) < 4 or contents[:2] != HEAD:
                    raise ReadError("damaged", "not a Publisher Contents stream")
                if contents[2] not in (V2000, V2002):
                    raise ReadError("unsupported", f"unknown Publisher version (Contents byte 0x{contents[2]:02X})")
                if any(_vba(n) for n in cf.names()):
                    ctx.skipped["vba"] = 1  # macros are never read; named once
                if contents[2] == V2000:
                    older = True
                    if cf.exists(QUILL):
                        part = f"{QUILL} stream"
                        quill = cf.read(QUILL)
                else:
                    if not cf.exists(QUILL):
                        raise ReadError("damaged", "no Quill stream")
                    part = f"{QUILL} stream"
                    quill = cf.read(QUILL)
                    part = f"{ESCHER} stream"
                    escher = cf.read(ESCHER) if cf.exists(ESCHER) else b""
                    part = f"{DELAY} stream"
                    delay = cf.read(DELAY) if escher and cf.exists(DELAY) else b""
    except (KeyError, CfbError):
        raise ReadError("damaged", part) from None
    if other is not None:
        return other
    if older:  # read after the try, so a reader's own KeyError is not taken for a damaged stream
        return pub_2k.parse_97(contents, ctx) if quill is None else pub_2k.parse_2000(quill, contents, ctx)
    return _Reader(contents, quill, escher, ctx, delay).run()
