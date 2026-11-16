# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not
# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Ported from libmspub 0477b85184e02dc5276582d9cea969208d56c655, src/lib/MSPUBParser.cpp, MSPUBCollector.cpp, EscherContainerType.h, EscherFieldIds.h.
"""Publisher 2002 and later (pub) to the document model: one section per page in the document's page order, each
page's text boxes in reading order with their bold, then the master pages, the scratch area (hidden) and a links line.

Ported from libmspub (MPL-2.0) at 0477b85184e02dc5276582d9cea969208d56c655, `src/lib/`: `MSPUBParser.cpp` (`parse`,
`parseContents`, `parseDocumentChunk`, `parsePageChunk`, `parsePageShapeList`, `parseShape`, `getPageTypeBySeqNum`,
`parseEscher`, `parseShapeGroup`, `parseEscherShape`, `extractEscherValues`, `getEscherElementTailLength`,
`getEscherElementAdditionalHeaderLength`), `MSPUBCollector.cpp` (`go`, `writePage`, `assignShapesToPages`,
`designateMasterPage`), `EscherContainerType.h` and `EscherFieldIds.h`. The Contents blocks and chunk directory are
`pub_contents`'s, the Quill stories, bold and links `pub_quill`'s.

Version. The Contents stream starts `E8 AC xx 00`: 0x2C is 2002 and later, read here; 0x22 (Publisher 2000, 97 and
98) is not read yet ("Publisher 2000 or 98"), any other byte is `unsupported`, and a stream not starting E8 AC is
damaged. A compound file with no Contents stream goes to `cfb_route.route(cf, "pub", "no Contents stream")`. A
2002+ file needs the Quill stream (`Quill/QuillSub/CONTENTS`) and a DOCUMENT chunk, as libmspub does; without
either it is damaged. A stream the compound file cannot give (a broken sector chain) is damaged, the detail naming
it ("Contents stream", "Quill/QuillSub/CONTENTS stream", "Escher/EscherStm stream").

Pages. The DOCUMENT chunk's page list (block 0x02, its entries of id 0) gives the order, each seqnum once; with no
list, the PAGE chunks in seqnum order (libmspub's `go`). Of these, a PAGE chunk whose block 0x0E (THIS_MASTER_NAME)
holds a byte other than 0 is a master, and the DUMMY seqnums 0x10D, 0x110, 0x113 and 0x117 (`getPageTypeBySeqNum`)
are the scratch area; every other listed page is numbered in turn, `Section("page", number=N)`, and given only when
it has text (libmspub writes a page only with shapes). A page's block 0x02 (PAGE_SHAPES) lists its shapes (entries of
type 0x70). Block 0x0D, the master a page applies, is not followed: the master's shapes are given once, in its own
section, never again on the pages naming it, so no master chain is walked. Masters follow the normal pages, in
directory order, as `Section("master", title=name)` (the name UTF-16LE, charged to the room; past it the title is
left out, counted `text`). Then the scratch area: the shapes of the DUMMY pages, in directory order, in one
`Section("page", title="Scratch area", hidden="hidden")`. Each chunk offset is walked once a document, however many
directory entries name it (their reads cached by offset), and a chunk ends at the next chunk's offset (or the trailer's
or the stream's end): a declared length past that is cut there, counted `record` once.

Shapes. A listed shape chunk (SHAPE, ALTSHAPE, TABLE; libmspub's `parseShape`) gives the Quill story its block 0x27
(SHAPE_TEXT_ID) names, the last one winning; a GROUP or LOGO chunk has no text and stands for its members, the
chunks whose directory parent it is, in directory order, groups inside included. This differs from libmspub, which
takes a group's members from the Escher SpgrContainer nesting (`beginGroup`, `setCurrentGroupSeqNum`); the two agree
where Publisher writes a member's directory parent as its group, as on every bench file. Each shape is placed once
in the document (where first met) and each story given once (a story two shapes name follows the first); a shape
naming no story gives nothing, uncounted. The story's `Para`s carry its bold (`pub_quill`). Tables are given as their
story's paragraphs.

Order. Top, then left, by the shape's box as libmspub's `parseEscherShape` reads it (the Contents shape chunk holds
only a width and height): the `Escher/EscherStm` DgContainers' SpgrContainers, walked as `parseShapeGroup`, each
SpContainer's ClientData (0xF011) value 0x6801 naming the Contents shape and its ClientAnchor (0xF010: values 0x2001
to 0x2004, signed) or ChildAnchor (0xF00F: four s32, mapped from the group's FSPGR coordinate system to the group's
box, as libmspub does). The DggContainer and DgContainers are followed by 4 more bytes, and a ClientAnchor's and
ClientData's values start 4 bytes in (`getEscherElementTailLength`, `getEscherElementAdditionalHeaderLength`). The
box is arranged (its top the smaller y); rotation is not read. A shape with no box follows the placed ones in list
order.

Links. `pub_quill.links` give one `Para([Text("Links: "), Link(u, [Text(u)]), Text(", "), ...])` at the end. VBA: a
storage named VBA anywhere (Publisher's `VBA/`, Word's `Macros/VBA/`) counts `vba` once; macros are never read.

Bounds (each read at call time). One `pub_contents.Meter` of `limits.BIN_RECORDS` a document: the directory, every
Contents block walked (page list, page chunks, shape chunks), each page visited, each shape placed, each Escher
record and value, and the Quill stories' styles tick it; the first refusal counts `record` once and every later walk
stops. The Escher walk, which only orders text, may spend no more than half of `limits.BIN_RECORDS` (`_Share`): past
that the boxes found so far are kept and the pages are still read, `record` counted once a document. The page list
and the shapes are walked with seen-sets, so a page or shape listed twice, naming itself or a cycle of groups is read
once; group members past `limits.MAX_DEPTH` levels, and Escher groups nested past it, are left out, counted `depth`
once. `ctx.guard("page")` wraps each page and scratch page, `ctx.guard("master_page")` each master and
`ctx.guard("shape")` each shape; a page's blocks are kept only when its guard succeeds. Each story
given is charged to one room of `limits.MAX_OUTPUT` a document, as the stories are charged (`pub_quill`): its
characters and `vsdx.LINE` for itself, each paragraph end or line break and each bold run; past the room it is left
out, counted `text`. Each link is charged its characters and LINE; past the room the rest are left out, counted
`link` once. Nothing is allocated from a declared count; loops over the directory's references are bounded by
`pub_contents.PUB_CHUNKS`.
"""
import struct
from bisect import bisect_right

from .. import limits
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Doc, Link, Para, Section, Text
from . import cfb_route, pub_contents, pub_quill, vsdx, zip_route
from .pub_contents import ALTSHAPE, DOCUMENT, GROUP, LOGO, PAGE, SHAPE, STRING_CONTAINER, TABLE

CONTENTS = "Contents"
QUILL = "Quill/QuillSub/CONTENTS"
ESCHER = "Escher/EscherStm"
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
    """The Escher walk of `boxes`: {Contents shape seqnum: (top, left)}."""

    def __init__(self, data, meter, ctx, limit=None):
        self.data, self.ctx = data, ctx
        self.meter = meter if limit is None else _Share(meter, limit)
        self.out = {}
        self.deep = False

    def walk(self):
        for typ, body, size in _records(self.data, 0, len(self.data), self.meter):
            if typ != DG:
                continue
            for t, b, s in _records(self.data, body, body + size, self.meter):
                if t == SPGR:
                    self.group(b, b + s, [0, 0, 0, 0], [0, 0, 0, 0], 1)
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
        """One SpContainer: an FSPGR sets the group's coordinate system; the ClientData's shape seqnum gets the box
        of the first anchor, which becomes the group's box when this shape set the coordinate system."""
        data = self.data
        parent = tuple(cs)
        fspgr = anchor = client = None
        for typ, b, s in _records(data, body, body + size, self.meter):
            if typ == FSPGR and fspgr is None:
                fspgr = (b, s)
            elif typ == CLIENT_DATA and client is None:
                client = (b, s)
            elif typ in (CLIENT_ANCHOR, CHILD_ANCHOR) and anchor is None:
                anchor = (typ, b, s)
        defines = fspgr is not None and fspgr[1] >= _BOX.size
        if defines:
            xs, ys, xe, ye = _BOX.unpack_from(data, fspgr[0])
            cs[:] = [min(xs, xe), min(ys, ye), max(xs, xe), max(ys, ye)]
        if client is None or anchor is None:
            return
        seq = _values(data, *client, self.meter).get(FIELD_SHAPE_ID)
        if seq is None:
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


class _Reader:
    def __init__(self, contents, quill, escher, ctx):
        self.data, self.ctx = contents, ctx
        self.meter = pub_contents.Meter(ctx)
        self.refs = {}   # seqnum -> its reference (a 2002 seqnum is its directory index, so each is one chunk)
        self.kids = {}   # seqnum -> the seqnums naming it as their parent, in directory order (a group's members)
        bounds = {len(contents)}
        for ref in pub_contents.directory(contents, ctx, meter=self.meter):  # at most PUB_CHUNKS
            self.refs[ref.seqnum] = ref
            bounds.update((ref.offset, ref.end))
            if ref.parent is not None:
                self.kids.setdefault(ref.parent, []).append(ref.seqnum)
        self.bounds = sorted(bounds)  # every chunk offset, the trailer's and the stream's end: a chunk ends at the next
        bits = pub_quill.bits(quill, ctx)
        self.stories = pub_quill.stories(bits, ctx, meter=self.meter)
        self.urls = pub_quill.links(bits)
        walk = _Boxes(escher, self.meter, ctx, self.meter.used + limits.BIN_RECORDS // 2)
        self.boxes = walk.walk() if escher else {}
        self.escher_cut = walk.meter.cut
        self.room = limits.MAX_OUTPUT
        self.chunks = {}     # chunk offset -> what `_read` found there
        self.long = False    # a chunk length past its span was cut (counted once)
        self.placed = set()  # shapes placed
        self.told = set()    # text ids given
        self.deep = False

    def _chunk(self, ref):
        """The blocks of a chunk: a u32 length counting itself, then blocks, to offset + length as libmspub reads, but
        never past the next chunk's offset (a longer length is cut there, counted record once)."""
        at = ref.offset
        if at + 4 > len(self.data):
            return iter(())
        end = at + _U32.unpack_from(self.data, at)[0]
        k = bisect_right(self.bounds, at)
        span = self.bounds[k] if k < len(self.bounds) else len(self.data)
        if end > span:
            end = span
            if not self.long:
                self.long = True
                self.meter.cut()
        return pub_contents.blocks(self.data, at + 4, end, meter=self.meter)

    def _read(self, ref) -> tuple:
        """(the 0x0E master name's bytes or None, the 0x02 list's id-0 entries, its 0x70 entries, the last 0x27 text
        id) of the chunk at `ref.offset`: walked once a document, however many directory entries name that offset."""
        got = self.chunks.get(ref.offset)
        if got is None:
            master, listed, shapes, tid = None, [], [], None
            for b in self._chunk(ref):
                if b.id == THIS_MASTER_NAME and b.type == STRING_CONTAINER:
                    master = b.data if any(b.data) else None
                elif b.id == PAGE_LIST:  # PAGE_SHAPES too; a fixed block has no children
                    for s in pub_contents.children(self.data, b, meter=self.meter):
                        if s.id == 0:
                            listed.append(s.data)  # a non-int entry names no chunk
                        if s.type == SHAPE_SEQNUM:
                            shapes.append(s.data)
                elif b.id == SHAPE_TEXT_ID:  # a non-int value names no story
                    tid = b.data
            got = self.chunks[ref.offset] = (master, listed or (), shapes or (), tid)  # () shared: most chunks list none
        return got

    def _page_list(self, doc) -> list:
        return self._read(doc)[1]

    def _page(self, ref) -> tuple:
        """(the master name's bytes, None for a normal page; its shape seqnums) of a PAGE chunk."""
        master, _, shapes, _ = self._read(ref)
        return master, shapes

    def _expand(self, seqs) -> list:
        """The shape references of a page's list, each group replaced by its members, each shape once a document."""
        flat = []
        for seq in seqs:
            stack = [(seq, 0)]
            while stack:
                s, depth = stack.pop()
                ref = self.refs.get(s)
                if s in self.placed or ref is None or ref.type not in SHAPES:
                    continue
                if depth > limits.MAX_DEPTH:
                    if not self.deep:
                        self.deep = True
                        self.ctx.skipped["depth"] += 1
                    continue
                if not self.meter.tick():
                    return flat
                self.placed.add(s)
                if ref.type in GROUPS:
                    stack.extend((k, depth + 1) for k in reversed(self.kids.get(s, ())))
                else:
                    flat.append(ref)
        return flat

    def _shapes(self, seqs) -> list:
        """The blocks of a page's shapes, in reading order."""
        flat = self._expand(seqs)
        placed = sorted((self.boxes[r.seqnum], i) for i, r in enumerate(flat) if r.seqnum in self.boxes)
        order = [flat[i] for _, i in placed] + [r for r in flat if r.seqnum not in self.boxes]
        out = []
        for ref in order:
            with self.ctx.guard("shape"):
                out.extend(self._text(ref))
        return out

    def _text(self, ref) -> list:
        """The paragraphs of the story a shape names, once a document, charged to the room."""
        tid = self._read(ref)[3]
        story = self.stories.get(tid)
        if story is None or tid in self.told:
            return []
        self.told.add(tid)
        cost = _cost(story)
        if cost > self.room:
            self.ctx.skipped["text"] += 1
            return []
        self.room -= cost
        return list(story.paras)

    def _title(self, raw: bytes) -> str:
        name = str(memoryview(raw)[:len(raw) // 2 * 2], "utf-16-le", "replace").replace("\0", "").strip()
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
        doc = next((r for r in self.refs.values() if r.type == DOCUMENT), None)
        if doc is None:
            raise ReadError("damaged", "no document chunk")
        pages = [r for r in self.refs.values() if r.type == PAGE]   # directory order
        order = self._page_list(doc) or sorted(r.seqnum for r in pages)
        out, seen, number = [], set(), 0
        for seq in order:
            ref = self.refs.get(seq)
            if seq in seen or ref is None or ref.type != PAGE or seq in DUMMY_PAGES:
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
            if ref.seqnum in DUMMY_PAGES or not self.meter.tick():
                continue
            with self.ctx.guard("master_page"):
                master, shapes = self._page(ref)
                if master is not None:
                    blocks = self._shapes(shapes)
                    if blocks:
                        out.append(Section("master", title=self._title(master), blocks=blocks))
        scratch = []
        for ref in pages:
            if ref.seqnum not in DUMMY_PAGES or not self.meter.tick():
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
    parser `cfb_route` picks for a compound file that holds no Contents stream. `parse` ends in it."""
    part = "compound file"
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "pub", "no Contents stream")
            if other is None:
                part = f"{CONTENTS} stream"
                contents = cf.read(CONTENTS)
                if len(contents) < 4 or contents[:2] != HEAD:
                    raise ReadError("damaged", "not a Publisher Contents stream")
                if contents[2] == V2000:
                    raise ReadError("unsupported", "Publisher 2000 or 98")
                if contents[2] != V2002:
                    raise ReadError("unsupported", f"unknown Publisher version (Contents byte 0x{contents[2]:02X})")
                if not cf.exists(QUILL):
                    raise ReadError("damaged", "no Quill stream")
                part = f"{QUILL} stream"
                quill = cf.read(QUILL)
                part = f"{ESCHER} stream"
                escher = cf.read(ESCHER) if cf.exists(ESCHER) else b""
                vba = any(_vba(n) for n in cf.names())
    except (KeyError, CfbError):
        raise ReadError("damaged", part) from None
    if other is not None:
        return other
    if vba:
        ctx.skipped["vba"] = 1  # macros are never read; named once
    return _Reader(contents, quill, escher, ctx).run()
