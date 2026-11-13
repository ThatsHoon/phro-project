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
"""
import re
import struct
from bisect import bisect_right

from .. import limits
from ..containers import flate
from ..containers.cfb import Compound, is_cfb
from ..images import sniff
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Equation, Heading, Image, ListItem, Math, NoteRef, Para, Table, Text
from . import hwpx
from .hancom import (
    AUTO_ALT, Frame, Scopes, State, comment_note, cut_command, flat, heading_level, lone_equations, next_marker,
    place, plain, strip_marker, style_name_level, tex as _tex,
)
from .hwp_chart import ole_table
from .hwp_records import (
    CTRL_HEADER, EQEDIT, LIST_HEADER, PARA_CHAR_SHAPE, PARA_HEADER, PARA_RANGE_TAG, PARA_TEXT, SHAPE_COMPONENT,
    SHAPE_OLE, SHAPE_PICTURE, TABLE, DocInfo, ParaShape, doc_info, file_header, first_tag, records, view_text,
)

_ZIP = b"PK\x03\x04"
_V3 = b"HWP Document File V3"
_DRM = (b"SCDSA", b"\x9b DRMONE")  # SoftCamp and Fasoo DRM containers (rhwp parser/mod.rs)
_SECTION = re.compile(r"(bodytext|viewtext)/section(\d+)", re.I)
_SECTIONS = 1000
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


def _wstr(data: bytes, pos: int) -> str:
    """u16 count, then UTF-16LE; cut to what the record holds."""
    if pos + 2 > len(data):
        return ""
    n = struct.unpack_from("<H", data, pos)[0]
    end = min(pos + 2 + 2 * n, len(data))
    return data[pos + 2:end - (end - pos) % 2].decode("utf-16-le", "replace")


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
        b"tbl ": "table", b"gso ": "drawing",
    }
    # a SHAPE_COMPONENT's child record tag -> handler (recs, j, st, d, g, alt) -> the index after what it read; g is
    # the component's group depth (1 for the drawing object itself) and alt the shape comment its picture takes
    SHAPES: dict[int, str] = {
        SHAPE_COMPONENT: "member", SHAPE_PICTURE: "picture", SHAPE_OLE: "ole", LIST_HEADER: "text_box",
        PARA_HEADER: "text_box",
    }

    def __init__(self, cf, ctx):
        self.cf, self.ctx = cf, ctx
        self.doc = Doc()
        self.head = None
        self.info = DocInfo(0, {}, [], [], [], [], [])
        self.budget = flate.Budget()  # one per document, shared by every inflation
        self.counts = {"fn": 0, "en": 0, "cm": 0}
        self.numbers = {}   # numbering id -> {level: counter}
        self.outline = 0    # the outline numbering id of the current section's secd
        self.blobs = {}     # (BinData stream name, inflated?, kind) -> its bytes, or None when it cannot be read
        self.tables = {}    # an OLE item's bytes -> (its chart table or None, the table's cells)
        self.opened = 0     # tables opened so far (TABLE_NODES)
        self.image_bytes = 0  # picture bytes placed so far, copies counted (IMAGE_BYTES)
        self.chart_cells = 0  # chart table cells placed so far, copies counted (CHART_TOTAL)
        self.margins = False  # walking a header or footer, whose pictures and objects are skipped
        # the current section came from ViewText, whose PARA_RANGE_TAG deletions are pending changes; BodyText already
        # holds the accepted text (dochan reader.py), so a range left in it hides nothing
        self.view = False
        self._ends_of, self._ends = None, []

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
            with self.ctx.guard("section"):
                self.view = False
                if h.distribution:
                    recs = self.load(name, records, lambda raw: view_text(raw, h.compressed, self.budget))
                    self.view = True
                elif h.track_changes:
                    recs, self.view = self.tracked(n, name)
                else:
                    recs = self.load(name, records)
                blocks = self.paras(recs, 0, len(recs), 0, False)
                self.doc.blocks.extend(blocks)
                good += 1
        if not good:
            raise ReadError("damaged", "no readable section")
        return self.doc

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
            if recs[j].tag == PARA_HEADER:
                out.extend(self.para(recs, j, d, nohead))
                j = self.end(recs, j)
            else:
                j += 1
        return out

    def sub(self, recs, i, d, nohead=True) -> list:
        """The paragraphs under the control at recs[i] (its LIST_HEADER's list)."""
        return self.paras(recs, i + 1, self.end(recs, i), d + 1, nohead)

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
        outline = self.outline_marker(shape, lvl) if shape.head == 1 else None
        main = None
        if level and text:
            if outline:
                strip_marker(inl, outline)
                inl.insert(0, Text(outline + " "))
            main = Heading(level, inl)
        elif shape.head in (2, 3):
            if shape.head == 2:
                marker = self.marker(shape.ref, lvl)
                strip_marker(inl, marker)
            else:  # a Markdown bullet; a copy of the BULLET character typed into the text is dropped
                marker = "-"
                if 1 <= shape.ref <= len(self.info.bullets):
                    strip_marker(inl, self.info.bullets[shape.ref - 1])
            if inl:
                main = ListItem(lvl, marker, inl)
        elif inl:
            main = Para(inl)
        return ([main] if main else []) + out + st.extras

    # ---- numbering
    def marker(self, nid, lvl) -> str:
        """The shared marker rule over this numbering's levels, in hwpx's {level: (format, kind, start)} shape."""
        nums = self.info.numberings
        num = nums[nid - 1] if 1 <= nid <= len(nums) else None
        levels = {k + 1: (num.formats[k], num.kinds[k], num.starts[k]) for k in range(7)} if num else {}
        return next_marker(levels, self.numbers.setdefault(nid, {}), lvl)

    def outline_marker(self, shape, lvl):
        """An outline paragraph's number: its own numbering id, else the section's (kordoc); None without one."""
        nid = shape.ref or self.outline
        nums = self.info.numberings
        if not 1 <= nid <= len(nums) or not nums[nid - 1].formats[lvl]:
            return None
        mk = self.marker(nid, lvl)
        return mk if mk != "-" else None

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
        self.doc.notes[nid] = comment_note(self.sub(recs, i, d))
        self.add(st, NoteRef(nid))
        return self.end(recs, i)

    def equation(self, recs, i, st, d):
        end = self.end(recs, i)
        data = recs[i].data
        inline = len(data) >= 8 and struct.unpack_from("<I", data, 4)[0] & 1  # common attr bit 0: like a character
        for j in range(i + 1, end):
            if recs[j].tag == EQEDIT:
                t = _tex(_wstr(recs[j].data, 4).replace("\0", ""))  # attr u32, then the script
                if t and inline:
                    m = Math(t)
                    st.eqs.append(m)
                    self.add(st, m)
                elif t:
                    st.extras.append(Equation(t, True))
                break
        return end

    def field(self, recs, i, st, d):
        """A field begin: the text up to its field end is a link, dropped (an unfilled %clk) or kept."""
        data = recs[i].data
        cid = data[3::-1]
        attr = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0
        if cid == b"%hlk":
            url = cut_command(_wstr(data, 9))  # id, attr u32, one byte, then the command
            self.push(st, Frame("link" if url else "pass", None, url))
        elif cid == b"%clk" and not attr & 0x8000:
            self.push(st, Frame("drop"))
        else:
            self.push(st, Frame("pass"))
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
        alt = re.sub(r"\s+", " ", _wstr(recs[i].data, 44).replace("\0", "")).strip()
        caption = []
        j = i + 1
        while j < end:
            r = recs[j]
            if r.tag == LIST_HEADER:
                k = self.list_end(recs, j)
                caption.extend(self.paras(recs, j + 1, k, d + 1, True))
                j = k
            elif r.tag == SHAPE_COMPONENT:
                j = self.component(recs, j, st, d, 1, alt)
            elif name := self.SHAPES.get(r.tag):
                j = max(getattr(self, name)(recs, j, st, d, 1, alt), j + 1)
            else:
                j = self.end(recs, j)
        st.extras.extend(caption)
        return end

    # ---- the SHAPE_COMPONENT walk: SHAPES handlers (recs, j, st, d, g, alt) -> the index after what they read
    def component(self, recs, k, st, d, g, alt) -> int:
        """A SHAPE_COMPONENT's child records in order. A group's members sit beside its SHAPE_CONTAINER, each one
        group and one depth level deeper; past _GROUPS groups or MAX_DEPTH the component is counted under depth."""
        end = self.end(recs, k)
        if g > _GROUPS or d > limits.MAX_DEPTH:
            self.deep()
            return end
        j = k + 1
        while j < end:
            name = self.SHAPES.get(recs[j].tag)
            j = max(getattr(self, name)(recs, j, st, d, g, alt), j + 1) if name else self.end(recs, j)
        return end

    def member(self, recs, j, st, d, g, alt):
        """A shape inside a group. The group's comment describes the group, so it is not the member's alt text."""
        return self.component(recs, j, st, d + 1, g + 1, "")

    def text_box(self, recs, j, st, d, g, alt):
        """A text box: its LIST_HEADER (or a bare paragraph) and the paragraphs beside it."""
        k = self.list_end(recs, j)
        st.extras.extend(self.paras(recs, j, k, d + 1, st.nohead))
        return k

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
                st.extras.append(Image(img, "" if AUTO_ALT.match(alt) else alt))
        return self.end(recs, j)

    def ole(self, recs, j, st, d, g, alt):
        """SHAPE_OLE: the BinData item at offset 12, an OLE object. The chart in it (hwp_chart.ole_table) is a table
        where a picture would be; an item two shapes name is read once, but every placement is charged against
        CHART_TOTAL, as in the Word and slide readers, and one past it is counted under chart instead. In a header or
        footer it is skipped (margin)."""
        if self.margins:
            return self.end(recs, j)
        data = recs[j].data
        bid = struct.unpack_from("<H", data, 12)[0] if len(data) >= 14 else 0
        item = self.bin_data(bid, "object")
        if item is not None:
            if item not in self.tables:
                tbl = ole_table(item, self.ctx)
                self.tables[item] = tbl, sum(len(r) for r in tbl.rows) if tbl is not None else 0
            tbl, cells = self.tables[item]
            if tbl is not None:
                self.chart_cells += cells
                if self.chart_cells > limits.CHART_TOTAL:
                    self.ctx.skipped["chart"] += 1
                else:
                    st.extras.append(tbl)
        return self.end(recs, j)


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
