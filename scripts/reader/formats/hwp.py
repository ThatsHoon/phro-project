"""HWP 5.0 (Hancom Office binary) to the document model.

The body is a flat list of records per section; a record's children are the records after it with a deeper level.
Logic follows dochan hwp/section.py and records/ctrl_header.py (paragraph walk, controls, field commands, tables,
drawing objects and their captions), dochan hwp/bin_data.py and rhwp parser/mod.rs (BinData streams and the per-item
compression bit), kordoc hwp5/body.ts, record.ts and numbering.ts (secd outline numbering, note numbers, %clk
placeholders, EQEDIT script) and rhwp parser/body_text.rs (control-character classes). Distribution documents read
ViewText through hwp_records.view_text (dochan hwp/distdoc.py); the DRM sniff and the track-changes ViewText
preference follow rhwp parser/mod.rs, the PARA_RANGE_TAG kinds rhwp parser/tracked_viewtext.rs and dochan
hwp/revisions.py. The inline model, paragraph rules and cell grid are hancom.py's, shared with hwpx.py. The chart
in an OLE object is hwp_chart.py's.

A section holding a connect line ($col) or a line ($lin) reads its drawing objects as `hancom.Obj` for
`hancom.diagrams`, as hwpx.py does. The SHAPE_COMPONENT layout, the instance id after its line, fill and shadow
(`drawingStyle`), and the LINE record of a connect line (points, type, then the start and end subjects and indexes)
follow kordoc's `src/hwp5/to-hwpx/shapes.ts` (MIT); the CTRL_HEADER's offsets and their vertRelTo and horzRelTo bits
its `controls.ts`. Every field comes from a record long enough to hold it, or is left unknown.
"""
import re
import struct
from bisect import bisect_right
from collections import deque
from itertools import groupby

from .. import diagram, limits
from ..containers import flate
from ..containers.cfb import Compound, is_cfb
from ..images import sniff
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Image, NoteRef, Table, Text
from . import hwpx
from .hancom import (
    Frame, Obj, Scopes, State, alt_text, button, cut_command, diagrams, flat, heading_level, lone_equations,
    next_marker, outline_marker, paragraph, place, plain, props, scripts, style_name_level,
)
from .hwp_chart import Items
from .hwp_records import (
    CTRL_HEADER, EQEDIT, FORM_OBJECT, LIST_HEADER, MEMO_LIST, PARA_CHAR_SHAPE, PARA_HEADER, PARA_RANGE_TAG, PARA_TEXT,
    SHAPE_COMPONENT, SHAPE_LINE, SHAPE_OLE, SHAPE_PICTURE, TABLE, TEXTART, DocInfo, ParaShape, distributed, doc_info,
    file_header, first_tag, records, view_text,
)
from .word_fields import comment_note

_ZIP = b"PK\x03\x04"
_V3 = b"HWP Document File V3"
_DRM = (b"SCDSA", b"\x9b DRMONE")  # SoftCamp and Fasoo DRM containers (rhwp parser/mod.rs)
_SECTION = re.compile(r"(bodytext|viewtext)/section(\d+)", re.I)
_SECTIONS = 1000
_DIST_HEAD = struct.pack("<I", 256 << 20 | 28)  # DISTRIBUTE_DOC_DATA's record header: tag 28, 256 bytes
_DELETE = 0x11  # the PARA_RANGE_TAG kind (top byte of its tag) of a tracked deletion; 0x10, an insertion, stays
# control characters: one unit (char class) unless listed here; these take eight units
_EXTENDED = frozenset((1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23))  # the k-th one owns the k-th CTRL_HEADER
_INLINE = frozenset((4, 5, 6, 7, 8, 9, 19, 20))
_FIELD_END, _TAB, _LINE, _PARA_END, _NBSP, _FIXED = 4, 9, 10, 13, 30, 31
# from an even offset: UTF-16LE units that are neither control characters (below 0x20) nor surrogates
_PLAIN = re.compile(rb"(?:[\x20-\xff][^\xd8-\xdf]|[\x00-\x1f][^\x00\xd8-\xdf])+")
_NO_SHAPE = ParaShape(0, 0, 0)
_GROUPS = 32  # shape groups nested deeper than this are counted under depth
# a BinData item read for a picture (image) or an OLE object (object): the extension of a storage item's stream
# (rhwp names an OLE storage BIN%04X.OLE), and the test that keeps the stored bytes of an item marked compressed
# that is not deflate at all (rhwp and dochan keep them)
_MEDIA = {
    "image": ("", lambda raw: len(raw) >= 16 and sniff(raw) is not None),
    "object": ("OLE", lambda raw: is_cfb(raw[4:12])),  # the OLE item: a 4-byte length, then a compound file
}
# SHAPE_COMPONENT control ids as stored (byte-reversed): a group, a connect line, a line, an ellipse; the drawn
# shapes kordoc reads a drawing style for after their matrices ($rec, $ell, $arc, $pol, $cur, $col, $lin)
_CON, _COL, _LIN, _ELL = b"noc$", b"loc$", b"nil$", b"lle$"
_EDGES = (_COL, _LIN)
_STYLED = (b"cer$", _ELL, b"cra$", b"lop$", b"ruc$", _COL, _LIN)
_CONNECT = ((False, False), (False, True), (True, True))  # (start, end) arrowheads of connect type % 3, types 0-8


def _num(data, pos, fmt="<i"):
    """One number at pos, or None when the record is too short for it."""
    return struct.unpack_from(fmt, data, pos)[0] if pos + struct.calcsize(fmt) <= len(data) else None


def _geometry(data) -> tuple:
    """A SHAPE_COMPONENT's (offset, size, where its drawing style starts) (kordoc readComponent): after the control id,
    twice in a drawing object's own and once in a group member's, the offset (x, y) in the group's frame, group level,
    version, the original and current sizes, flip, angle and centre, then the count of scale and rotation matrix pairs
    that follow the translation matrix. The size is the current one, an axis written 0 the original's. Each is None
    when the record is too short for it."""
    p = 8 if len(data) >= 8 and data[:4] == data[4:8] else 4
    x, y = _num(data, p), _num(data, p + 4)
    w, h = (_num(data, p + 20 + a) or _num(data, p + 12 + a) for a in (0, 4))
    n = _num(data, p + 42, "<H")
    return (None if x is None or y is None else (x, y), None if w is None or h is None else (w, h),
            None if n is None else p + 92 + 96 * n)


def _style(data, p) -> tuple:
    """(line attribute, instance id) of the drawing style at p (kordoc drawingStyle and fillBrushXml): line colour,
    width, attribute and outline, the fill brush (its type, a solid colour, a gradation with its colours, an image,
    the extra bytes, an alpha per kind), the shadow (type, colour, offsets), then the instance id; None for what the
    record does not hold, and for an id of 0."""
    if p is None:
        return None, None
    attr, kind = _num(data, p + 8, "<I"), _num(data, p + 13, "<I")
    if kind is None:
        return attr, None
    f = p + 17
    if kind & 1:
        f += 12
    if kind & 4:
        n = _num(data, f + 17)  # after its type byte, angle, centre and step: the colour count
        if n is None:
            return attr, None
        n = max(n, 0)
        f += 21 + 4 * n + (4 * n if n > 2 else 0)  # past two colours, their positions come first
    if kind & 2:
        f += 6
    extra = _num(data, f, "<I")
    if extra is None:
        return attr, None
    f += 4 + extra + (kind & 1) + (kind >> 2 & 1) + (kind >> 1 & 1)
    return attr, _num(data, f + 16, "<I") or None


def _heads(attr) -> tuple:
    """(start, end) arrowheads of a line attribute: the head and tail styles at bits 10 and 16 (kordoc lineShapeXml),
    0 being none; (None, None) when the attribute is unknown."""
    return (None, None) if attr is None else (bool(attr >> 10 & 0x3F), bool(attr >> 16 & 0x3F))


def _line_ends(data, connect) -> tuple:
    """(start, end, connect type, start subject, end subject) of a LINE record (kordoc $col and $lin): a line's two
    points; a connect line's points, type, then the subjects and their indexes, all or nothing, so a record cut short
    of its glue gives neither glue nor points. Subject 0 is no glue."""
    if connect and len(data) >= 36:
        x0, y0, x1, y1, kind, a, _, b, _ = struct.unpack_from("<4i5I", data)
        return (x0, y0), (x1, y1), kind, a or None, b or None
    if not connect and len(data) >= 16:
        x0, y0, x1, y1 = struct.unpack_from("<4i", data)
        return (x0, y0), (x1, y1), None, None, None
    return None, None, None, None, None


def _placed(head):
    """A drawing object's top left from its CTRL_HEADER (vertical offset @8, horizontal @12) when both are relative to
    the paper or the page: vertRelTo is bits 3-4 of the attribute @4, horzRelTo bits 8-9, 0 the paper and 1 the page
    (kordoc controls.ts objPlacementXml); else None."""
    if len(head) < 16:
        return None
    attr, y, x = struct.unpack_from("<Iii", head, 4)
    return (x, y) if attr >> 3 & 3 < 2 and attr >> 8 & 3 < 2 else None


def _at(origin, p):
    return None if origin is None or p is None else (origin[0] + p[0], origin[1] + p[1])


def _objs(data, head, inner, line, got) -> list:
    """A SHAPE_COMPONENT read as hancom.Obj, with what it added to the paragraph (`got`): a group holds the objects
    and runs of other blocks in it as its kids; anything else holds the other blocks, and objects in it follow. `head`
    is a drawing object's CTRL_HEADER, None for a group member, whose offset is in its group's frame when `inner`.
    `line` is the LINE record under it, or None. The key is the instance id, else the CTRL_HEADER's id @36."""
    kind = data[:4]
    offset, size, style = _geometry(data)
    origin = _placed(head) if head is not None else offset if inner else None
    box = origin + (origin[0] + size[0], origin[1] + size[1]) if origin and size else None
    attr, key = _style(data, style) if kind in _STYLED else (None, None)
    if key is None and head is not None:
        key = _num(head, 36, "<I") or None
    if kind == _CON:
        kids = []
        for is_obj, run in groupby(got, lambda b: isinstance(b, Obj)):
            kids += run if is_obj else [Obj(None, list(run), group=True)]
        return [Obj(key, [], kids, True, box=box)]
    edge = None
    if kind in _EDGES:
        p0, p1, ctype, a, b = _line_ends(line or b"", kind == _COL)
        arrows = _CONNECT[ctype % 3] if ctype is not None and ctype < 9 else _heads(attr)
        edge = a, b, _at(origin, p0), _at(origin, p1), arrows, kind == _LIN
    own = Obj(key, [b for b in got if not isinstance(b, Obj)], (), False, "circle" if kind == _ELL else "", box, edge)
    return [own] + [b for b in got if isinstance(b, Obj)]


def _wstr(data: bytes, pos: int) -> str:
    """u16 count, then UTF-16LE; cut to what the record holds."""
    if pos + 2 > len(data):
        return ""
    n = struct.unpack_from("<H", data, pos)[0]
    end = min(pos + 2 + 2 * n, len(data))
    return data[pos + 2:end - (end - pos) % 2].decode("utf-16-le", "replace")


def _tail(recs) -> int:
    """Where a section's tail starts: the first level-1 LIST_HEADER or MEMO_LIST after its last level-0 paragraph
    header (a paragraph's own lists sit under a control, two levels down). Hancom keeps the memo lists and the extra
    master pages there. len(recs) without one."""
    last = next((i for i in range(len(recs) - 1, -1, -1) if recs[i].tag == PARA_HEADER and not recs[i].level), None)
    if last is None:
        return len(recs)
    return next((k for k in range(last + 1, len(recs))
                 if recs[k].level == 1 and recs[k].tag in (LIST_HEADER, MEMO_LIST)), len(recs))


def _deleted(tags: bytes) -> list:
    """The units PARA_RANGE_TAG deletions cover (12-byte start, end, tag entries), merged into sorted edges
    [start, end, start, end, ...]: unit u is deleted when an odd number of edges are <= u. Backwards and empty ranges
    are left out."""
    spans = sorted((s, e) for s, e, t in struct.iter_unpack("<III", tags) if t >> 24 == _DELETE and s < e)
    edges = []
    for s, e in spans:
        if edges and s <= edges[-1]:
            edges[-1] = max(edges[-1], e)
        else:
            edges += (s, e)
    return edges


def _ends(recs) -> list:
    """For each record, the index of the next record that is not deeper: the end of its subtree. O(n)."""
    out = [len(recs)] * len(recs)
    stack = []
    for j, r in enumerate(recs):
        while stack and recs[stack[-1]].level >= r.level:
            out[stack.pop()] = j
        stack.append(j)
    return out


class _Reader(Scopes):
    # control id (big-endian ASCII) -> handler name; an unlisted id starting with "%" is a field, any other is skipped
    CONTROLS: dict[bytes, str] = {
        b"secd": "section_def", b"cold": "skip", b"pgnp": "skip", b"atno": "skip", b"nwno": "skip",
        b"bokm": "skip", b"head": "header", b"foot": "footer", b"fn  ": "note", b"en  ": "note",
        b"tcmt": "comment", b"eqed": "equation", b"%hlk": "field", b"%clk": "field",
        b"tbl ": "table", b"gso ": "drawing", b"tdut": "ruby", b"form": "form",
    }
    # a SHAPE_COMPONENT's child record tag -> handler (recs, j, st, d, g, alt) -> the index after what it read; g is
    # the component's group depth (1 for the drawing object itself) and alt the shape comment its picture takes
    SHAPES: dict[int, str] = {
        SHAPE_COMPONENT: "member", SHAPE_PICTURE: "picture", SHAPE_OLE: "ole", LIST_HEADER: "text_box",
        PARA_HEADER: "text_box", TEXTART: "art",
    }

    def __init__(self, cf, ctx):
        self.cf, self.ctx = cf, ctx
        self.doc = Doc()
        self.head = None
        self.info = DocInfo(0, {}, [], [], [], [], [])
        self.budget = flate.Budget()  # one per document, shared by every inflation
        self.counts = {"fn": 0, "en": 0, "cm": 0}
        self.memos = {}
        self.numbers = {}   # numbering id -> {level: counter}
        self.outline = 0    # the outline numbering id of the current section's secd
        self.blobs = {}     # (BinData stream name, inflated?, kind) -> its bytes, or None when it cannot be read
        self.items = Items(ctx)  # the OLE items read so far
        self.opened = 0     # tables opened so far (TABLE_NODES)
        self.image_bytes = 0  # picture bytes placed so far, copies counted (IMAGE_BYTES)
        self.chart_cells = 0  # chart table cells placed so far, copies counted (CHART_TOTAL)
        self.margins = False  # walking a header or footer, whose pictures and objects are skipped
        # the current section came from ViewText, whose PARA_RANGE_TAG deletions are pending changes; BodyText already
        # holds the accepted text (dochan reader.py), so a range left in it hides nothing
        self.view = False
        self._ends_of, self._ends = None, []
        self.edges = diagram.ConnectionBudget(ctx)  # the document's edges, shared by every section's diagrams
        self.collect = False  # reading drawing objects as hancom.Obj: in a section with a line, not in a sub-list
        self.grouped = False  # reading the members of a group

    # ---- streams
    def load(self, name, parse, decode=None):
        """Read a stream, decode it (`decode`, else inflate when the document is compressed) and parse it. A cut
        deflate stream is parsed from what inflated and counts one damaged record, whether or not its last record
        was split."""
        data = self.cf.read(name)
        cut = False
        try:
            if decode is not None:
                data = decode(data)
            elif self.head.compressed:
                data = flate.inflate(data, self.budget)
        except flate.Truncated as e:
            data, cut = e.data, True
        before = self.ctx.skipped["record"]
        out = parse(data, self.ctx)
        if cut and self.ctx.skipped["record"] == before:
            self.ctx.skipped["record"] += 1
        return out

    def bin_data(self, bid, kind="image"):
        """The bytes of BinData item `bid` (1-based, as DocInfo lists them) for a picture (kind image) or an OLE object
        (kind object); None, counted under that kind for every shape that wanted it, for a link item or a missing,
        empty or broken stream. A stream is read and inflated once however many items and shapes of a kind name it."""
        item = self.info.bins.get(bid)
        data = None
        if item is not None and item.kind != "link":
            ext, keep = _MEDIA[kind]
            name = f"BinData/BIN{item.id:04X}.{item.ext or (ext if item.kind == 'storage' else '')}"
            # the item's own bit decides (rhwp): 1 compressed, 2 stored, anything else as the document is
            packed = item.compress == 1 or (item.compress != 2 and self.head.compressed)
            key = (name.upper(), packed, kind)  # stream names match without case
            if key not in self.blobs:
                self.blobs[key] = self.load_bin(name, packed, keep)
            data = self.blobs[key]
        if data is None:
            self.ctx.skipped[kind] += 1
        return data

    def load_bin(self, name, packed, keep):
        """A stream over a limit (ReadError damaged or too_large), missing, broken or cut is None, as hwpx skips one;
        `keep` says whether the stored bytes of an item marked compressed that is not deflate at all are kept."""
        try:
            raw = self.cf.read(name)
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            return None
        except (KeyError, ValueError):  # no such stream, a broken chain
            return None
        if not packed:
            return raw or None
        try:
            return flate.inflate(raw, self.budget) or None
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            return None
        except flate.Truncated:  # a cut stream is a broken one
            return None
        except ValueError:  # not deflate at all: an item marked compressed but stored, kept as rhwp and dochan keep it
            return raw if keep(raw) else None

    def section_names(self, storage) -> list:
        """(N, stream name) for the declared section count together with the <storage>/Section<N> streams found
        (storage BodyText or ViewText), N under 1000, in order."""
        found, over = {}, 0
        for name in self.cf.names():
            if (m := _SECTION.fullmatch(name)) and m.group(1).lower() == storage.lower():
                n = int(m.group(2))
                if n < _SECTIONS:
                    found.setdefault(n, name)
                else:
                    over += 1
        if over:
            self.ctx.skipped["section"] += over
        nums = sorted(set(range(min(self.info.sections, _SECTIONS))) | set(found))
        return [(n, found.get(n, f"{storage}/Section{n}")) for n in nums]

    def tracked(self, n, name) -> tuple:
        """A track-changes section (rhwp parser/mod.rs; dochan hwp/revisions.py): ViewText/Section<n>, not encrypted,
        holds the text with its insertions and deletions marked, BodyText the result. ViewText is read when it
        inflates whole to a stream that starts with a paragraph; otherwise BodyText is, and nothing is counted.
        Returns (records, read from ViewText?)."""
        try:
            data = self.cf.read(f"ViewText/Section{n}")
            if self.head.compressed:
                data = flate.inflate(data, self.budget)
            if first_tag(data) != PARA_HEADER:
                raise ValueError("not a paragraph stream")
        except (KeyError, ValueError):  # missing, a broken chain, bad or cut deflate (Truncated), not paragraphs
            return self.load(name, records), False
        return records(data, self.ctx), True

    def read_scripts(self):
        """The document's JScript (pyhwp; hwp 5.0 spec, Scripts storage): Scripts/DefaultJScript, encrypted as ViewText
        when it opens with DISTRIBUTE_DOC_DATA (a distribution document, or one saved again from it with FileHeader's
        flag cleared) and then raw deflate when the document is compressed (its CRC-32 and length after), holds
        parts of an i32 count of UTF-16 units and the text, ended by -1 or the stream's end; hancom.scripts names
        the first four. A stream over SCRIPT_BYTES, stored or inflated, or broken is named under
        script; so is a cut one, or one with a part cut short, whose parts before the cut are read."""
        cut = False
        try:
            data = self.cf.read("Scripts/DefaultJScript")
            if len(data) > limits.SCRIPT_BYTES:
                raise ReadError("too_large", f"{limits.BIG_PART}: script stream over {limits.SCRIPT_BYTES} bytes")
            if data[:4] == _DIST_HEAD:
                data = distributed(data)
            if self.head.compressed:
                data = flate.inflate(data, self.budget, limits.SCRIPT_BYTES)
        except KeyError:
            return
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            self.ctx.skipped["script"] = 1
            return
        except flate.Truncated as e:
            data, cut = e.data, True
        except ValueError:  # a broken chain, not a distribution stream, not deflate
            self.ctx.skipped["script"] = 1
            return
        texts, short = _script_parts(data)
        if cut or short:
            self.ctx.skipped["script"] = 1
        self.ctx.scripts += scripts(texts)

    def run(self):
        try:
            self.head = h = file_header(self.cf)
        except ValueError:  # a broken FileHeader chain
            raise ReadError("damaged", "not an HWP file") from None
        if h.password or h.cert_encrypted:
            raise ReadError("encrypted")
        if h.drm or h.cert_drm:
            raise ReadError("drm")
        try:
            self.info = self.load("DocInfo", doc_info)
        except (KeyError, ValueError):
            self.ctx.skipped["styles"] += 1  # DocInfo holds the style, shape and numbering tables
        good = 0
        # a distribution document's text is only in ViewText, encrypted with the key it carries (no BodyText fallback)
        for n, name in self.section_names("ViewText" if h.distribution else "BodyText"):
            if self.ctx.late():
                break
            with self.ctx.guard("section"):
                self.view = False
                if h.distribution:
                    recs = self.load(name, records, lambda raw: view_text(raw, h.compressed, self.budget))
                    self.view = True
                elif h.track_changes:
                    recs, self.view = self.tracked(n, name)
                else:
                    recs = self.load(name, records)
                cut = _tail(recs)
                recs, tail = recs[:cut], recs[cut:]
                scoped = self.collect = any(r.tag == SHAPE_COMPONENT and r.data[:4] in _EDGES for r in recs)
                try:
                    blocks = self.paras(recs, 0, len(recs), 0, False)
                finally:
                    self.collect = False
                self.doc.blocks.extend(diagrams(self.ctx, self.edges, blocks) if scoped else blocks)
                self.doc.blocks.extend(self.tail(tail))
                good += 1
        if not good:
            raise ReadError("damaged", "no readable section")
        self.read_scripts()
        for waiting in self.memos.values():
            for nid, author in waiting:
                self.doc.notes[nid] = comment_note([], author)
                self.ctx.skipped["annotation"] += 1
        return self.doc

    def tail(self, recs) -> list:
        """A section's tail (_tail), as groups that each start at a level-1 MEMO_LIST or LIST_HEADER. A MEMO_LIST with
        its u32 memo number and a LIST_HEADER right after it is a memo: its paragraphs become the comment note of the
        first memo field with that number still waiting, in this section or one before; with no such field they are
        returned, so the text stays at the section's end. Any other group (a master page) is not read."""
        out = []
        j, n = 0, len(recs)
        while j < n:
            memo = (recs[j].tag == MEMO_LIST and len(recs[j].data) >= 4 and j + 1 < n
                    and recs[j + 1].level == 1 and recs[j + 1].tag == LIST_HEADER)
            k = j + (2 if memo else 1)
            while k < n and not (recs[k].level == 1 and recs[k].tag in (LIST_HEADER, MEMO_LIST)):
                k += 1
            if memo:
                blocks = self.apart(recs, j + 2, k, 1, True)
                waiting = self.memos.get(struct.unpack_from("<I", recs[j].data)[0])
                if waiting:
                    nid, author = waiting.popleft()
                    self.doc.notes[nid] = comment_note(blocks, author)
                else:
                    out.extend(comment_note(blocks, ""))
            j = k
        return out

    # ---- record tree
    def end(self, recs, i) -> int:
        if self._ends_of is not recs:
            self._ends_of, self._ends = recs, _ends(recs)
        return self._ends[i]

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    def paras(self, recs, start, end, d, nohead) -> list:
        """The paragraphs in recs[start:end]; a paragraph's own records (and its controls) are skipped as a whole."""
        if d > limits.MAX_DEPTH:
            self.deep()
            return []
        out = []
        j = start
        while j < end:
            if self.ctx.late():
                break
            if recs[j].tag == PARA_HEADER:
                out.extend(self.para(recs, j, d, nohead))
                j = self.end(recs, j)
            else:
                j += 1
        return out

    def sub(self, recs, i, d, nohead=True) -> list:
        """The paragraphs under the control at recs[i] (its LIST_HEADER's list): a note, comment, header or footer."""
        return self.apart(recs, i + 1, self.end(recs, i), d + 1, nohead)

    def apart(self, recs, start, end, d, nohead) -> list:
        """paras() for a list read apart from the section's drawing (a text box, caption, note, comment, header or
        footer): drawing objects in it read as before, with no diagram."""
        outer, self.collect = self.collect, False
        try:
            return self.paras(recs, start, end, d, nohead)
        finally:
            self.collect = outer

    def next_ctrl(self, recs, j, end, lvl) -> int:
        while j < end and not (recs[j].tag == CTRL_HEADER and recs[j].level == lvl):
            j = self.end(recs, j)
        return j

    def list_end(self, recs, j, cell=False) -> int:
        """The end of the list that starts at recs[j], its LIST_HEADER (or, in a shape, a bare first paragraph). Its
        paragraphs follow at that record's level (deeper records are theirs) up to a lower level, and up to the next
        same-level LIST_HEADER for a table cell or the first same-level record that is not a PARA_HEADER for any
        other list. O(paragraphs)."""
        lvl, n = recs[j].level, len(recs)
        k = self.end(recs, j)
        while k < n and recs[k].level == lvl and (recs[k].tag != LIST_HEADER if cell else recs[k].tag == PARA_HEADER):
            k = self.end(recs, k)
        return k

    def control(self, recs, i, st, d) -> int:
        data = recs[i].data
        cid = data[3::-1] if len(data) >= 4 else b""
        name = self.CONTROLS.get(cid) or ("field" if cid[:1] == b"%" else "skip")
        return max(getattr(self, name)(recs, i, st, d), i + 1)

    # ---- paragraphs
    def para(self, recs, i, d, nohead) -> list:
        head, end = recs[i], self.end(recs, i)
        lvl = head.level + 1
        text, pairs, tags = [], [], []
        j = i + 1
        while j < end:
            r = recs[j]
            if r.level == lvl:
                if r.tag == PARA_TEXT:
                    text.append(r.data[:len(r.data) // 2 * 2])
                elif r.tag == PARA_CHAR_SHAPE:
                    pairs.append(r.data[:len(r.data) // 8 * 8])
                elif r.tag == PARA_RANGE_TAG and self.view:
                    tags.append(r.data[:len(r.data) // 12 * 12])
            j = self.end(recs, j)
        st = State(nohead)
        runs, edges = self.bold_runs(b"".join(pairs)), _deleted(b"".join(tags))
        self.walk(b"".join(text), runs, edges, st, recs, i + 1, end, lvl, d)
        self.close_to(st, 1)
        return self.blocks(head, st)

    def bold_runs(self, pairs: bytes):
        bold = self.info.bold
        runs = sorted((p, 0 <= s < len(bold) and bold[s]) for p, s in struct.iter_unpack("<II", pairs))
        return [p for p, _ in runs], [b for _, b in runs]

    def walk(self, data, runs, edges, st, recs, cur, end, lvl, d):
        """PARA_TEXT units into inlines; the k-th extended control character runs the k-th CTRL_HEADER. What a
        tracked deletion covers (`edges`, from _deleted) goes into a deleted scope, so it is hidden."""
        cur = self.next_ctrl(recs, cur, end, lvl)
        n, p = len(data), 0
        k, scope, stop = 0, None, n  # edges passed, the open deleted scope, where a plain run must stop
        while p + 1 < n:
            if edges:
                u = p // 2
                while k < len(edges) and edges[k] <= u:
                    k += 1
                scope = self.deleting(st, k % 2, scope)
                stop = min(n, 2 * edges[k]) if k < len(edges) else n  # endpos, so no run is matched twice
            if m := _PLAIN.match(data, p, stop):
                self.chunk(data[p:m.end()].decode("utf-16-le"), p // 2, runs, st)
                p = m.end()
                continue
            c = data[p] | data[p + 1] << 8
            if 0xD800 <= c <= 0xDFFF:
                if c < 0xDC00 and p + 3 < n and 0xDC <= data[p + 3] <= 0xDF:
                    self.chunk(data[p:p + 4].decode("utf-16-le"), p // 2, runs, st)
                    p += 4
                else:  # an unpaired surrogate, shown as Hancom shows it
                    self.chunk("□", p // 2, runs, st)
                    p += 2
                continue
            if c == _PARA_END:
                break
            if c in _EXTENDED:
                if cur < end:
                    cur = self.next_ctrl(recs, self.control(recs, cur, st, d), end, lvl)
                p += 16
                continue
            if c in _INLINE:
                if c == _FIELD_END:
                    self.close_field(st)
                elif c == _TAB:
                    self.chunk(" ", p // 2, runs, st)
                p += 16
                continue
            if c == _LINE:
                self.add(st, Break())
            elif c == _NBSP:
                self.chunk("\u00a0", p // 2, runs, st)
            elif c == _FIXED:
                self.chunk(" ", p // 2, runs, st)
            p += 2  # 0 and 24-29 are dropped
        while cur < end:  # controls the text never reached still give their content
            cur = self.next_ctrl(recs, self.control(recs, cur, st, d), end, lvl)

    def deleting(self, st, inside, scope):
        """Keep a deleted scope open exactly while the walk is inside a tracked deletion. `scope` is (index, Frame) of
        the one this paragraph opened, or None; the new value is returned. A field end that closes a field opened
        before the deletion closes it too, and the next deleted unit opens another. Past MAX_DEPTH open scopes none
        opens and the text stays visible."""
        if scope and (len(st.frames) <= scope[0] or st.frames[scope[0]] is not scope[1]):
            scope = None
        if inside and not scope:
            f = Frame("deleted")
            self.push(st, f)
            if st.frames[-1] is f:
                scope = (len(st.frames) - 1, f)
        elif scope and not inside:
            self.close_to(st, scope[0])
            scope = None
        return scope

    def chunk(self, s, u, runs, st):
        """Text starting at unit u, split where PARA_CHAR_SHAPE changes boldness."""
        pos, bold = runs
        k = bisect_right(pos, u)
        on = bold[k - 1] if k else False
        start = 0
        while k < len(pos) and pos[k] < u + len(s):
            cut = pos[k] - u
            if cut > start:
                self.text(s[start:cut], st, on)
                start = cut
            on = bold[k]
            k += 1
        self.text(s[start:], st, on)

    def text(self, s, st, bold):
        if s:
            t = Text(s)
            self.add(st, Bold([t]) if bold else t)

    def shape(self, head) -> ParaShape:
        sid = struct.unpack_from("<H", head.data, 8)[0] if len(head.data) >= 10 else -1
        return self.info.para[sid] if 0 <= sid < len(self.info.para) else _NO_SHAPE

    def style_level(self, head) -> int:
        sid = head.data[10] if len(head.data) >= 11 else -1
        if not 0 <= sid < len(self.info.styles):
            return 0
        style = self.info.styles[sid]
        return style_name_level(style.name) or style_name_level(style.local)

    def blocks(self, head, st) -> list:
        """The Hancom paragraph rules (shared with hwpx): lone equations shown on their own, headings, list items,
        then the extras."""
        inl = st.frames[0].inl
        out = lone_equations(inl, st.eqs)
        text = plain(inl)
        shape = self.shape(head)
        lvl = min(shape.level, 6)
        level = 0
        if not st.nohead:
            level = heading_level(min(shape.level + 1, 6) if shape.head == 1 else self.style_level(head), text)
        number = self.outline_marker(shape, lvl) if shape.head == 1 else None
        item = None
        if shape.head == 2:
            marker = self.marker(shape.ref, lvl)
            item = (lvl, marker, marker)
        elif shape.head == 3:  # a Markdown bullet; a copy of the BULLET character typed into the text is dropped
            bullets = self.info.bullets
            item = (lvl, "-", bullets[shape.ref - 1] if 1 <= shape.ref <= len(bullets) else None)
        main = paragraph(inl, text, level, number, item)
        return ([main] if main else []) + out + st.extras

    # ---- numbering
    def levels(self, nid) -> dict:
        """A numbering's levels in hwpx's {level: (format, kind, start)} shape; {} for an id past the table."""
        nums = self.info.numberings
        num = nums[nid - 1] if 1 <= nid <= len(nums) else None
        return {k + 1: (num.formats[k], num.kinds[k], num.starts[k]) for k in range(7)} if num else {}

    def marker(self, nid, lvl) -> str:
        return next_marker(self.levels(nid), self.numbers.setdefault(nid, {}), lvl)

    def outline_marker(self, shape, lvl):
        """An outline paragraph's number: its own numbering id, else the section's (kordoc); None without one."""
        nid = shape.ref or self.outline
        return outline_marker(self.levels(nid), self.numbers.setdefault(nid, {}), lvl)

    # ---- control handlers: (recs, i, st, d) -> the index of the first record after the control
    def skip(self, recs, i, st, d):
        return self.end(recs, i)

    def section_def(self, recs, i, st, d):
        data = recs[i].data
        if len(data) >= 20:
            self.outline = struct.unpack_from("<H", data, 18)[0]
        return self.end(recs, i)

    def header(self, recs, i, st, d):
        return self.margin(recs, i, d, self.doc.headers)

    def footer(self, recs, i, st, d):
        return self.margin(recs, i, d, self.doc.footers)

    def margin(self, recs, i, d, into):
        """A header or footer as one line of text, its text boxes' too. A header is a line of text, as in doc: its
        pictures and OLE objects are skipped, neither read nor counted."""
        outer, self.margins = self.margins, True
        try:
            text = flat(self.sub(recs, i, d))
        finally:
            self.margins = outer
        if text:
            into.append(text)
        return self.end(recs, i)

    def note(self, recs, i, st, d):
        data = recs[i].data
        key = "fn" if data[3::-1] == b"fn  " else "en"
        num = struct.unpack_from("<I", data, 4)[0] & 0xFFFF if len(data) >= 8 else 0
        self.counts[key] += 1
        nid = f"{key}-{num}"
        if not num or nid in self.doc.notes:  # numbers restart per section; the id must stay unique
            nid = f"{key}-{num}-{self.counts[key]}"
        self.doc.notes[nid] = self.sub(recs, i, d)
        self.add(st, NoteRef(nid))
        return self.end(recs, i)

    def comment(self, recs, i, st, d):
        self.counts["cm"] += 1
        nid = f"cm-{self.counts['cm']}"
        self.doc.notes[nid] = comment_note(self.sub(recs, i, d), "")
        self.add(st, NoteRef(nid))
        return self.end(recs, i)

    def equation(self, recs, i, st, d):
        """EQEDIT's script (after its attr u32) as a formula; a caption list before it follows as paragraphs."""
        end = self.end(recs, i)
        data = recs[i].data
        inline = len(data) >= 8 and struct.unpack_from("<I", data, 4)[0] & 1  # common attr bit 0: like a character
        caption = []
        j = i + 1
        while j < end:
            if recs[j].tag == LIST_HEADER:
                k = self.list_end(recs, j)
                caption.extend(self.apart(recs, j + 1, k, d + 1, True))
                j = k
            elif recs[j].tag == EQEDIT:
                self.formula(st, _wstr(recs[j].data, 4).replace("\0", ""), inline)
                break
            else:
                j = self.end(recs, j)
        st.extras.extend(caption)
        return end

    def ruby(self, recs, i, st, d):
        """Ruby (덧말, tdut): after the id, the base text and the ruby text as two WCHAR arrays, written base(ruby);
        a record too short to hold both counts under record."""
        data = recs[i].data
        k = 6 + 2 * struct.unpack_from("<H", data, 4)[0] if len(data) >= 6 else len(data)
        if k + 2 > len(data) or k + 2 + 2 * struct.unpack_from("<H", data, k)[0] > len(data):
            self.ctx.skipped["record"] += 1
            return self.end(recs, i)
        base, ruby = _wstr(data, 4).strip(), _wstr(data, k).strip()
        self.text(f"{base}({ruby})" if base and ruby else base or ruby, st, False)
        return self.end(recs, i)

    def form(self, recs, i, st, d):
        """A form object. Its FORM_OBJECT record holds the kind in bytes 0-3 and, at 12, a property string
        (hancom.props): a check box or radio button gives its box and Caption, a combo box or edit box its Text, a
        push button nothing. A record that holds no kind this reader knows is counted under field."""
        end = self.end(recs, i)
        data = next((recs[j].data for j in range(i + 1, end) if recs[j].tag == FORM_OBJECT), b"")
        kind, p = data[:4], props(_wstr(data, 12))
        if kind in (b"tbc+", b"tbr+"):
            v = p.get("Value", "")
            self.text(button(int(v) if v in ("1", "2") else 0, p.get("Caption", "")), st, False)
        elif kind in (b"boc+", b"tde+"):
            self.text(p.get("Text", ""), st, False)
        elif kind != b"tbp+":
            self.ctx.skipped["field"] += 1
        return end

    def field(self, recs, i, st, d):
        """A field begin: id, attr u32, one byte, the command, the field's u32 id and a u32 memo number. The text up
        to its field end is a link, a click-here field's value (its guide text, the command's Direction, is dropped
        when the field is not marked filled), the anchor of a memo, or kept. A memo's command is
        MEMO/<shape>/<number>/<id>/<id>/<author>/, whatever the control id (Hancom writes %unk); its paragraphs are in
        a section's tail, so its note waits for them under its number (the trailing one, else the command's)."""
        data = recs[i].data
        cid = data[3::-1]
        attr = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0
        command = _wstr(data, 9)
        if cid == b"%hlk":
            self.open_field(st, url=cut_command(command))
        elif command.startswith("MEMO/"):
            part = command.split("/", 5)
            author = part[5].rsplit("/", 1) if len(part) > 5 else []
            k = 15 + 2 * struct.unpack_from("<H", data, 9)[0]
            number = struct.unpack_from("<I", data, k)[0] if len(data) >= k + 4 else 0
            if not number and len(part) > 2 and part[2].isascii() and part[2].isdigit() and len(part[2]) < 10:
                number = int(part[2])
            self.counts["cm"] += 1
            nid = f"cm-{self.counts['cm']}"
            self.doc.notes[nid] = []
            self.memos.setdefault(number, deque()).append((nid, author[0] if len(author) == 2 else ""))
            self.memo(st, None, nid)
        else:
            unfilled = cid == b"%clk" and not attr & 0x8000
            self.open_field(st, unfilled=unfilled, guide=props(command).get("Direction", "") if unfilled else "")
        return self.end(recs, i)

    def table(self, recs, i, st, d):
        """An optional caption list, TABLE, then a LIST_HEADER per cell with the cell's paragraphs beside it. The
        table, then its caption as paragraphs, follow the paragraph that holds it. Past MAX_DEPTH or the document's
        TABLE_NODES the table is left out and counted under depth."""
        end = self.end(recs, i)
        if d + 1 > limits.MAX_DEPTH or self.opened >= limits.TABLE_NODES:
            self.deep()
            return end
        self.opened += 1
        raw, caption = [], []  # raw: (row, col, rowspan, colspan, Cell)
        cols, seen = 0, False
        j = i + 1
        while j < end:
            r = recs[j]
            if r.tag == TABLE and not seen:
                seen = True
                cols = struct.unpack_from("<H", r.data, 6)[0] if len(r.data) >= 8 else 0
                j = self.end(recs, j)
            elif r.tag == LIST_HEADER and not seen:
                k = self.list_end(recs, j)
                caption.extend(self.paras(recs, j + 1, k, d + 1, True))
                j = k
            elif r.tag == LIST_HEADER:
                k = self.list_end(recs, j, cell=True)
                row, col, rs, cs = self.cell_at(r.data, len(raw), cols)
                raw.append((row, col, rs, cs, Cell(self.paras(recs, j + 1, k, d + 1, True))))
                j = k
            else:
                j = self.end(recs, j)
        if raw:
            st.extras.append(Table(place(raw)))
        st.extras.extend(caption)
        return end

    @staticmethod
    def cell_at(data, n, cols):
        """(row, col, rowspan, colspan) of the n-th cell; a LIST_HEADER too short for them takes the next slot."""
        if len(data) >= 16:
            col, row, cs, rs = struct.unpack_from("<4H", data, 8)
            return row, col, max(rs, 1), max(cs, 1)
        row, col = divmod(n, cols) if cols else (0, n)
        return row, col, 1, 1

    def drawing(self, recs, i, st, d):
        """A drawing object (gso): an optional caption list, then its SHAPE_COMPONENT, or in an older layout the shape
        record (SHAPE_PICTURE or SHAPE_OLE) right under the control, read through SHAPES. What the shape holds, then
        its caption as paragraphs, follow the paragraph. The shape comment (offset 44) is its picture's alt text."""
        end = self.end(recs, i)
        alt = alt_text(_wstr(recs[i].data, 44))
        caption = []
        j = i + 1
        while j < end:
            r = recs[j]
            if r.tag == LIST_HEADER:
                k = self.list_end(recs, j)
                caption.extend(self.apart(recs, j + 1, k, d + 1, True))
                j = k
            elif r.tag == SHAPE_COMPONENT:
                j = self.component(recs, j, st, d, 1, alt, recs[i].data)
            elif name := self.SHAPES.get(r.tag):
                j = max(getattr(self, name)(recs, j, st, d, 1, alt), j + 1)
            else:
                j = self.end(recs, j)
        st.extras.extend(caption)
        return end

    # ---- the SHAPE_COMPONENT walk: SHAPES handlers (recs, j, st, d, g, alt) -> the index after what they read
    def component(self, recs, k, st, d, g, alt, head=None) -> int:
        """A SHAPE_COMPONENT's child records in order. A group's members sit beside its SHAPE_CONTAINER, each one
        group and one depth level deeper; past _GROUPS groups or MAX_DEPTH the component is counted under depth. While
        collecting, what it added to the paragraph becomes its hancom.Obj (_objs); `head` is the drawing object's
        CTRL_HEADER, None for a group member."""
        end = self.end(recs, k)
        if g > _GROUPS or d > limits.MAX_DEPTH:
            self.deep()
            return end
        data, start, inner, line = recs[k].data, len(st.extras), self.grouped, None
        self.grouped = data[:4] == _CON
        try:
            j = k + 1
            while j < end:
                r = recs[j]
                if r.tag == SHAPE_LINE and line is None:
                    line = r.data
                name = self.SHAPES.get(r.tag)
                j = max(getattr(self, name)(recs, j, st, d, g, alt), j + 1) if name else self.end(recs, j)
        finally:
            self.grouped = inner
        if self.collect:
            got = st.extras[start:]
            del st.extras[start:]
            st.extras.extend(_objs(data, head, inner, line, got))
        return end

    def member(self, recs, j, st, d, g, alt):
        """A shape inside a group. The group's comment describes the group, so it is not the member's alt text."""
        return self.component(recs, j, st, d + 1, g + 1, "")

    def text_box(self, recs, j, st, d, g, alt):
        """A text box: its LIST_HEADER (or a bare paragraph) and the paragraphs beside it."""
        k = self.list_end(recs, j)
        st.extras.extend(self.apart(recs, j, k, d + 1, st.nohead))
        return k

    def art(self, recs, j, st, d, g, alt):
        """TEXTART: its record's layout is not established, so the text art is counted under shape."""
        self.ctx.skipped["shape"] += 1
        return self.end(recs, j)

    def picture(self, recs, j, st, d, g, alt):
        """SHAPE_PICTURE: the BinData item at offset 71. Every placement is charged against IMAGE_BYTES, as in the Word
        readers; one past it is counted under image instead. In a header or footer it is skipped (margin)."""
        if self.margins:
            return self.end(recs, j)
        data = recs[j].data
        bid = struct.unpack_from("<H", data, 71)[0] if len(data) >= 73 else 0
        img = self.bin_data(bid)
        if img is not None:
            self.image_bytes += len(img)
            if self.image_bytes > limits.IMAGE_BYTES:
                self.ctx.skipped["image"] += 1
            else:
                st.extras.append(Image(img, alt, codepage="cp949"))
        return self.end(recs, j)

    def ole(self, recs, j, st, d, g, alt):
        """SHAPE_OLE: the BinData item at offset 12, an OLE object. What it shows (hwp_chart.Items: a chart's table,
        an equation, a package's tables, a document) stands where a picture would; every placement is charged against
        CHART_TOTAL, as in the Word and slide readers, and one past it is counted under chart instead. In a header or
        footer it is skipped (margin)."""
        if self.margins:
            return self.end(recs, j)
        data = recs[j].data
        bid = struct.unpack_from("<H", data, 12)[0] if len(data) >= 14 else 0
        item = self.bin_data(bid, "object")
        if item is not None:
            blocks, cells = self.items.show(item)
            if blocks:
                self.chart_cells += cells
                if self.chart_cells > limits.CHART_TOTAL:
                    self.ctx.skipped["chart"] += 1
                else:
                    st.extras.extend(blocks)
        return self.end(recs, j)


def _script_parts(data) -> tuple:
    """The texts of DefaultJScript's first four parts read whole, and whether a part was cut short."""
    texts, pos = [], 0
    while len(texts) < 4 and pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "little", signed=True)
        end = pos + 4 + 2 * n
        if pos + 4 > len(data) or n < -1 or end > len(data):
            return texts, True
        if n == -1:
            break
        texts.append(data[pos + 4:end].decode("utf-16-le", "replace"))
        pos = end
    return texts, False


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(32)
    if any(mark in head for mark in _DRM):  # a DRM container is not a compound file: sniffed before one opens
        raise ReadError("drm")
    if head.startswith(_ZIP):
        return hwpx.parse(path, ctx)
    if head.startswith(_V3):
        raise ReadError("unsupported", "hwp 3.x")
    if not is_cfb(head):
        raise ReadError("damaged", "not an HWP file")
    with Compound(path) as cf:
        return _Reader(cf, ctx).run()
