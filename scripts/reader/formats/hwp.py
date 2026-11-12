"""HWP 5.0 (Hancom Office binary) to the document model.

The body is a flat list of records per section; a record's children are the records after it with a deeper level.
Logic follows dochan hwp/section.py and records/ctrl_header.py (paragraph walk, controls, field commands), kordoc
hwp5/body.ts, record.ts and numbering.ts (secd outline numbering, note numbers, %clk placeholders, EQEDIT script) and
rhwp parser/body_text.rs (control-character classes). The inline model and paragraph rules are hancom.py's, shared
with hwpx.py.
"""
import re
import struct
from bisect import bisect_right

from .. import limits
from ..containers import flate
from ..containers.cfb import Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Break, Doc, Equation, Heading, ListItem, Math, NoteRef, Para, Text
from . import hwpx
from .hancom import (
    Frame, Scopes, State, comment_note, cut_command, flat, heading_level, lone_equations, next_marker, plain,
    strip_marker, style_name_level, tex as _tex,
)
from .hwp_records import (
    CTRL_HEADER, EQEDIT, PARA_CHAR_SHAPE, PARA_HEADER, PARA_TEXT, DocInfo, ParaShape, doc_info, file_header,
    records,
)

_ZIP = b"PK\x03\x04"
_V3 = b"HWP Document File V3"
_SECTION = re.compile(r"bodytext/section(\d+)", re.I)
_SECTIONS = 1000
# control characters: one unit (char class) unless listed here; these take eight units
_EXTENDED = frozenset((1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23))  # the k-th one owns the k-th CTRL_HEADER
_INLINE = frozenset((4, 5, 6, 7, 8, 9, 19, 20))
_FIELD_END, _TAB, _LINE, _PARA_END, _NBSP, _FIXED = 4, 9, 10, 13, 30, 31
# from an even offset: UTF-16LE units that are neither control characters (below 0x20) nor surrogates
_PLAIN = re.compile(rb"(?:[\x20-\xff][^\xd8-\xdf]|[\x00-\x1f][^\x00\xd8-\xdf])+")
_NO_SHAPE = ParaShape(0, 0, 0)


def _wstr(data: bytes, pos: int) -> str:
    """u16 count, then UTF-16LE; cut to what the record holds."""
    if pos + 2 > len(data):
        return ""
    n = struct.unpack_from("<H", data, pos)[0]
    end = min(pos + 2 + 2 * n, len(data))
    return data[pos + 2:end - (end - pos) % 2].decode("utf-16-le", "replace")


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
        self._ends_of, self._ends = None, []

    # ---- streams
    def load(self, name, parse):
        """Read a stream, inflate it when the document is compressed and parse it. A cut deflate stream is parsed
        from what inflated and counts one damaged record, whether or not its last record was split."""
        data = self.cf.read(name)
        cut = False
        if self.head.compressed:
            try:
                data = flate.inflate(data, self.budget)
            except flate.Truncated as e:
                data, cut = e.data, True
        before = self.ctx.skipped["record"]
        out = parse(data, self.ctx)
        if cut and self.ctx.skipped["record"] == before:
            self.ctx.skipped["record"] += 1
        return out

    def section_names(self) -> list:
        """The declared section count together with the BodyText/Section<N> streams found, N under 1000, in order."""
        found, over = {}, 0
        for name in self.cf.names():
            if m := _SECTION.fullmatch(name):
                n = int(m.group(1))
                if n < _SECTIONS:
                    found.setdefault(n, name)
                else:
                    over += 1
        if over:
            self.ctx.skipped["section"] += over
        nums = sorted(set(range(min(self.info.sections, _SECTIONS))) | set(found))
        return [found.get(n, f"BodyText/Section{n}") for n in nums]

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
        for name in self.section_names():
            with self.ctx.guard("section"):
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

    def control(self, recs, i, st, d) -> int:
        data = recs[i].data
        cid = data[3::-1] if len(data) >= 4 else b""
        name = self.CONTROLS.get(cid) or ("field" if cid[:1] == b"%" else "skip")
        return max(getattr(self, name)(recs, i, st, d), i + 1)

    # ---- paragraphs
    def para(self, recs, i, d, nohead) -> list:
        head, end = recs[i], self.end(recs, i)
        lvl = head.level + 1
        text, pairs = [], []
        j = i + 1
        while j < end:
            r = recs[j]
            if r.level == lvl:
                if r.tag == PARA_TEXT:
                    text.append(r.data[:len(r.data) // 2 * 2])
                elif r.tag == PARA_CHAR_SHAPE:
                    pairs.append(r.data[:len(r.data) // 8 * 8])
            j = self.end(recs, j)
        st = State(nohead)
        self.walk(b"".join(text), self.bold_runs(b"".join(pairs)), st, recs, i + 1, end, lvl, d)
        self.close_to(st, 1)
        return self.blocks(head, st)

    def bold_runs(self, pairs: bytes):
        bold = self.info.bold
        runs = sorted((p, 0 <= s < len(bold) and bold[s]) for p, s in struct.iter_unpack("<II", pairs))
        return [p for p, _ in runs], [b for _, b in runs]

    def walk(self, data, runs, st, recs, cur, end, lvl, d):
        """PARA_TEXT units into inlines; the k-th extended control character runs the k-th CTRL_HEADER."""
        cur = self.next_ctrl(recs, cur, end, lvl)
        n, p = len(data), 0
        while p + 1 < n:
            if m := _PLAIN.match(data, p):
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
        text = flat(self.sub(recs, i, d))
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


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(32)
    if head.startswith(_ZIP):
        return hwpx.parse(path, ctx)
    if head.startswith(_V3):
        raise ReadError("unsupported", "hwp 3.x")
    if not is_cfb(head):
        raise ReadError("damaged", "not an HWP file")
    with Compound(path) as cf:
        return _Reader(cf, ctx).run()
