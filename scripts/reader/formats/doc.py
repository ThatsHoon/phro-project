"""Word 97-2003 binary (.doc) to the document model.

The text is one CP stream cut into stories (doc_fib.STORIES). A story is walked once: plain runs are text, and every
character below 0x20 other than a tab goes to its handler in _Reader.SPECIAL, paragraph marks included. Logic follows
dochan office_binary/doc_binary.py (stream checks, the table stream, story ranges) and doc_stories.py (field begin,
separator and end), and anydoc formats/doc/mod.rs (the language is lidFE in a Far East document, else lid). The FIB,
the piece table and the story text are doc_fib.py's; the HYPERLINK rule is word_fields.py's, shared with the docx
reader.

The walk keeps one str unit a CP: a surrogate pair is two units, so a handler's CP is the Story's cp_of its index,
and the pair is joined only when a Text is built.

Formatting (doc_fib's FKPs, Prms and Styles) is mapped to CPs through the pieces once, then the walk takes a story as
segments of constant character properties: st.run is the segment's RunProps, so text, breaks and every handler see
the properties of their own CP. A paragraph's properties are its mark's: the PAPX whose FC range holds the mark's FC.
Bold text is Bold; vanished and deleted text are Hidden(..., "hidden") and Hidden(..., "deleted"), the shapes the docx
reader gives w:vanish runs and w:del. Damaged formatting is counted under styles and never costs text.

A paragraph in a list (sprmPIlfo) is a ListItem with doc_fib.Lists' marker, as the docx reader makes w:numPr ones.
Paragraphs with a table depth (sprmPFInTable, sprmPItap) are grouped into tables as their marks come (MS-DOC
"Overview of Tables", logic after dochan doc_tables.py): at depth 1 a cell ends at \\x07 and a row at its TTP mark; a
nested table's cells and rows end at marks with sprmPFInnerTableCell and sprmPFInnerTtp. The row mark's TDefTable
puts the cells on the grid of all the table's boundaries and gives the merges, the colspan and rowspan the docx
reader gives w:gridSpan and w:vMerge.
"""
import re
import struct
from bisect import bisect_right

from .. import limits
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Hidden, Link, ListItem, Para, Table, Text
from . import doc_fib, ooxml
from .word_fields import MAX_INSTR, hyperlink_url

_ZIP = b"PK\x03\x04"
_RTF = b"{\\rtf"
_BOM = b"\xef\xbb\xbf"
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f]")  # the characters the walk hands to a handler; a tab is text
_MARK = re.compile(r"[\r\x07\x0c]")  # the characters that end a paragraph and own its PAPX
_SURROGATE = re.compile("[\ud800-\udfff]")
_HUGE_PAPX = 0x6646
_PLAIN = doc_fib.RunProps()


def _joined(s: str) -> str:
    """UTF-16 units as characters: a pair becomes one, an unpaired surrogate U+FFFD."""
    if _SURROGATE.search(s):
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return s


def _shown(inlines) -> bool:
    """Whether inlines hold text outside every Hidden."""
    for it in inlines:
        if isinstance(it, Text):
            if it.text.strip():
                return True
        elif isinstance(it, (Bold, Link)) and _shown(it.inlines):
            return True
    return False


class _Frame:
    """One open inline scope inside a paragraph: root or link."""

    def __init__(self, kind, url=""):
        self.kind, self.url, self.inl = kind, url, []


class _Field:
    """An open field: its instruction so far, whether its result has begun, the index of the link frame it opened."""

    def __init__(self):
        self.instr, self.size, self.result, self.frame = [], 0, False, None


class _Table:
    """A table being grouped: its rows so far as (cells, the row mark's TDefTable operand or None), a cell being its
    blocks, then the cells of the open row and the blocks of the open cell."""

    def __init__(self):
        self.rows, self.cells, self.cell = [], [], []

    def end_cell(self):
        self.cells.append(self.cell)
        self.cell = []

    def end_row(self, tdef):
        """A row mark: blocks after the last cell mark make one more cell (the row mark's own text, in a damaged
        file); a row without cells is left out."""
        if self.cell:
            self.end_cell()
        if self.cells:
            self.rows.append((self.cells, tdef))
        self.cells = []

    def finish(self) -> list:
        """The rows, cells after the last row mark making one more row without a TDefTable."""
        self.end_row(None)
        return self.rows


def _table(rows) -> tuple[Table | None, int]:
    """Rows of (cells, TDefTable operand or None) as a Table, and how many operands were damaged (doc_fib.table_def
    None). The cells sit on the grid of every row's boundaries (their union, as Word's own tblGrid): a cell spans the
    grid columns between its two boundaries, a row starting right of the grid's left edge starts with an empty cell
    over the gap, and a row without a usable TDefTable has a column a cell, as do cells past those it defines. Then the
    TCGRF merges (MS-DOC 2.9.328): horzMerge 2 or 3 starts a set the next cells with horzMerge 1 join; vertMerge 3
    (bit 0x40) starts one that the cell over the same grid columns in each next row with vertMerge 1 joins. A joined
    cell is left out and its blocks go to the set's first cell, which takes its colspan or one more rowspan. O(cells +
    boundaries log boundaries); the spans go through the renderer's GRID_COLS and GRID_SLOTS budgets."""
    defs, bad = [], 0
    for _, arg in rows:
        d = doc_fib.table_def(arg) if arg is not None else None
        bad += arg is not None and d is None
        defs.append(d)
    col_of = {e: i for i, e in enumerate(sorted({e for d in defs if d for e in d[0]}))}
    out, above = [], {}
    for (cells, _), d in zip(rows, defs):
        bounds, grf = d or ((), ())
        col = col_of[bounds[0]] if bounds else 0
        row = [Cell([], colspan=col)] if col else []
        sets = []  # [col, span, TCGRF, blocks, open to horzMerge 1] once the horizontal merges are made
        for k, blocks in enumerate(cells):
            span = col_of[bounds[k + 1]] - col_of[bounds[k]] if k + 1 < len(bounds) else 1
            g = grf[k] if k < len(grf) else 0
            if (g & 3) == 1 and sets and sets[-1][4]:
                sets[-1][1] += span
                sets[-1][3].extend(blocks)
            else:
                sets.append([col, span, g, blocks, bool(g & 2)])
            col += span
        below = {}
        for c, span, g, blocks, _ in sets:
            key = (c, span)
            if (g & 0x60) == 0x20 and key in above:
                origin = below[key] = above[key]
                origin.rowspan += 1
                origin.blocks.extend(blocks)
                continue
            cell = Cell(blocks, colspan=span)
            row.append(cell)
            if g & 0x40:
                below[key] = cell
        above = below
        out.append(row)
    return (Table(out) if out else None), bad


class _State:
    """A story being walked: its name, its blocks so far, and the open paragraph's inline scopes and fields. Fields
    live with the paragraph: one still open at a paragraph mark ends there, so a broken field costs one paragraph.
    run and seg are the properties and the CHPX run of the segment being walked; hid is the Hidden last opened, which
    the rest of its segment (or of a deletion) joins while it is still the last inline of its scope. tables are the
    tables open around the paragraph, outermost first (tables[k] at depth k + 1); overflow is set while paragraphs nest
    past MAX_DEPTH, so one overflowing subtree is counted once."""

    def __init__(self, name, story=None):
        self.name = name
        self.story = story  # the doc_fib.Story walked: CP <-> text index
        self.out = []
        self.run, self.seg = _PLAIN, -1
        self.tables: list[_Table] = []
        self.overflow = False
        self.reset()

    def reset(self):
        self.frames = [_Frame("root")]
        self.fields = []
        self.codes = 0  # open fields still in their instruction: text goes to the innermost one, never out
        self.over = 0   # fields opened past MAX_DEPTH: everything up to their ends is skipped
        self.hid, self.hid_seg = None, -1


class _Reader:
    # a control character -> its handler (st, cp), cp being the character's CP; one not listed is dropped. Tasks
    # 12-14 give \x01 (picture), \x02 (note reference), \x05 (comment reference) and \x08 (drawn object) handlers. A
    # cell or row mark (\x07) ends its paragraph too; place() gives it its table meaning.
    SPECIAL: dict[str, str] = {
        "\r": "para_end", "\x07": "para_end", "\x0c": "para_end",
        "\x0a": "line_break", "\x0b": "line_break",
        "\x13": "field_begin", "\x14": "field_separator", "\x15": "field_end",
        "\x1e": "hyphen",
    }

    def __init__(self, cf, ctx):
        self.cf, self.ctx = cf, ctx
        self.doc = Doc()
        self.word = b""
        self.table = b""
        self.fib = None
        self.pieces = []
        self.lid = 0x0409  # the language of compressed text (doc_fib.compressed_codepage)
        self.ranges = {}   # story name -> (cp0, cp1), clamped to the CPs the pieces hold
        self.on = {ch: getattr(self, name) for ch, name in self.SPECIAL.items()}
        self.styles = doc_fib.Styles()
        self.lists = doc_fib.Lists()
        self.prc = []                # the Clx Prc grpprls a Prm1 indexes
        self.chp, self.chp_at = [], []  # (cp0, cp1, CHPX grpprl, prm) in CP order, and their cp0s
        self.pap, self.pap_at = [], []  # (cp0, cp1, PAPX, prm) in CP order, and their cp0s
        self.data = None             # the Data stream once read; False when missing or unreadable
        self._prm, self._runs, self._paras = {}, {}, {}  # memos: prm -> sprms; run and paragraph properties
        self._huge, self._huge_read = {}, 0  # Data offset -> the sprms of its huge PAPX; the bytes read so far

    # ---- streams
    def stream(self, name) -> bytes:
        try:
            return self.cf.read(name)
        except (KeyError, CfbError):  # missing, or a broken chain
            raise ReadError("damaged", f"{name} stream") from None

    def table_stream(self, name) -> bytes:
        """The table stream the FIB names; the other one when only that exists (dochan, anydoc)."""
        for n in (name, "0Table" if name == "1Table" else "1Table"):
            if self.cf.exists(n):
                return self.stream(n)
        raise ReadError("damaged", "no table stream")

    def run(self):
        if not self.cf.exists("WordDocument"):
            if self.cf.exists("EncryptedPackage"):  # an encrypted OOXML package saved as .doc
                raise ReadError("encrypted")
            raise ReadError("damaged", "no WordDocument stream")
        self.word = self.stream("WordDocument")
        self.fib = f = doc_fib.fib(self.word)
        if f.encrypted or f.obfuscated:
            raise ReadError("encrypted")
        self.table = self.table_stream(f.table)
        self.pieces, cut = doc_fib.fit(doc_fib.pieces(self.table, *f.pair(doc_fib.CLX)), len(self.word))
        if cut:
            self.ctx.skipped["text"] += cut
        self.lid = f.lid_fe if f.far_east and f.lid_fe else f.lid
        self.ranges = doc_fib.story_ranges(f.ccp, self.pieces[-1].cp_end if self.pieces else 0)
        self.props()
        with self.ctx.guard("document"):
            self.doc.blocks.extend(self.story("text"))
        if self.styles.bad:
            self.ctx.skipped["styles"] += self.styles.bad
        return self.doc

    # ---- formatting
    def plc(self, index) -> bytes:
        """FibRgFcLcb97 entry `index` in the table stream; one outside it is counted under styles."""
        fc, lcb = self.fib.pair(index)
        if not lcb:
            return b""
        if fc + lcb > len(self.table):
            self.ctx.skipped["styles"] += 1
            return b""
        return self.table[fc:fc + lcb]

    def props(self):
        """The style sheet, the lists, the Prc list and the CHPX and PAPX runs by CP. Each is read under
        guard("styles") (the lists under guard("numbering")): a damaged one leaves its default (no styles, no lists, no
        runs) and costs formatting only."""
        with self.ctx.guard("styles"):
            self.styles = doc_fib.Styles(self.plc(doc_fib.STSH))
        with self.ctx.guard("numbering"):
            self.lists = doc_fib.Lists(self.table, self.fib.pair(doc_fib.PLF_LST), self.fib.pair(doc_fib.PLF_LFO))
        if self.lists.bad:
            self.ctx.skipped["numbering"] += self.lists.bad
        with self.ctx.guard("styles"):
            self.prc = doc_fib.prcs(self.table, *self.fib.pair(doc_fib.CLX))
        with self.ctx.guard("styles"):
            fkp = doc_fib.fkp_runs(self.word, self.plc(doc_fib.BTE_CHPX), "chpx", self.ctx)
            self.chp = doc_fib.cp_runs(fkp, self.pieces)
            self.chp_at = [r[0] for r in self.chp]
        with self.ctx.guard("styles"):
            fkp = doc_fib.fkp_runs(self.word, self.plc(doc_fib.BTE_PAPX), "papx", self.ctx)
            self.pap = doc_fib.cp_runs(fkp, self.pieces)
            self.pap_at = [r[0] for r in self.pap]

    def prm_sprms(self, prm) -> list:
        """The sprms of a piece's Prm; a Prm1 past the Prc list is counted under styles once per value."""
        hit = self._prm.get(prm)
        if hit is None:
            hit = doc_fib.prm_sprms(prm, self.prc)
            if hit is None:
                self.ctx.skipped["styles"] += 1
                hit = []
            self._prm[prm] = hit
        return hit

    def data_stream(self):
        """The Data stream, read once; None when it is missing or cannot be read."""
        if self.data is None:
            try:
                self.data = self.cf.read("Data")
            except (KeyError, CfbError, ReadError):
                self.data = False
        return self.data if self.data is not False else None

    def papx_sprms(self, grpprl) -> list:
        """A PAPX's sprms, cut to doc_fib.last_sprms, a sprmPHugePapx replaced where it stands by the grpprl it points
        at in the Data stream."""
        ops = doc_fib.sprms(grpprl)
        if any(op == _HUGE_PAPX for op, _ in ops):
            out = []
            for op, arg in ops:
                if op == _HUGE_PAPX:
                    out.extend(self.huge_papx(int.from_bytes(arg, "little")))
                else:
                    out.append((op, arg))
            ops = out
        return doc_fib.last_sprms(ops)

    def huge_papx(self, at) -> list:
        """The sprms of the huge PAPX at Data offset `at`, read once per offset; a sprmPHugePapx nested in it is left
        out. One that cannot be read (no Data stream, outside it) is counted under styles, and so is every one past
        len(Data) bytes read in all: the PrcData of a well-formed file never overlap, so only a hostile file reads
        more, and it reads no more than the Data stream holds."""
        hit = self._huge.get(at)
        if hit is None:
            data = self.data_stream()
            g = doc_fib.huge_papx(data, at) if data is not None else None
            if g is None or self._huge_read + len(g) > len(data):
                self.ctx.skipped["styles"] += 1
                hit = []
            else:
                self._huge_read += len(g)
                hit = doc_fib.last_sprms([x for x in doc_fib.sprms(g) if x[0] != _HUGE_PAPX])
            self._huge[at] = hit
        return hit

    def para_props(self, cp) -> doc_fib.ParaProps:
        """The properties of the paragraph whose mark is at `cp`: the PAPX run holding it (its istd, then its
        sprms), then the Prm of the piece holding it. Memoized by (PAPX, Prm)."""
        k = bisect_right(self.pap_at, cp) - 1
        data, prm = (self.pap[k][2], self.pap[k][3]) if k >= 0 and cp < self.pap[k][1] else (b"", 0)
        key = (data, prm)
        hit = self._paras.get(key)
        if hit is None:
            istd = struct.unpack_from("<H", data)[0] if len(data) >= 2 else 0
            hit = self._paras[key] = self.styles.para(istd, self.papx_sprms(data[2:]) + self.prm_sprms(prm))
        return hit

    def run_props(self, istd, data, prm) -> doc_fib.RunProps:
        """A CHPX run's properties in a paragraph of style istd, its piece's Prm applied last. Memoized."""
        key = (istd, data, prm)
        hit = self._runs.get(key)
        if hit is None:
            hit = self._runs[key] = self.styles.run(istd, doc_fib.sprms(data) + self.prm_sprms(prm))
        return hit

    def segments(self, story) -> list:
        """(i0, i1, RunProps, run) covering story.text in order: the CHPX runs inside the story (`run` is the index
        of each in self.chp), cut where a paragraph ends, each resolved over its paragraph's style, which its mark
        owns. Pieces that hold no run are runs without properties. O((runs + paragraphs) log)."""
        text = story.text
        n = len(text)
        if not n:
            return []
        cp_of, index_of = story.cp_of, story.index_of
        paras = [(m.end(), self.para_props(cp_of(m.start())).istd) for m in _MARK.finditer(text)]
        if not paras or paras[-1][0] < n:
            paras.append((n, self.para_props(cp_of(n - 1)).istd))
        runs, chp, pos = [], self.chp, 0
        k = max(bisect_right(self.chp_at, story.cp0) - 1, 0)
        while k < len(chp) and chp[k][0] < story.cp1:
            end = index_of(min(chp[k][1], story.cp1))
            if end > pos:
                runs.append((end, chp[k][2], chp[k][3], k))
                pos = end
            k += 1
        if pos < n:
            runs.append((n, b"", 0, -1))
        out, a, b, pos = [], 0, 0, 0
        while pos < n:
            end = min(paras[a][0], runs[b][0])
            _, data, prm, run = runs[b]
            props = self.run_props(paras[a][1], data, prm)
            if out and out[-1][3] == run and out[-1][2] is props:
                out[-1] = (out[-1][0], end, props, run)
            else:
                out.append((pos, end, props, run))
            pos = end
            a += paras[a][0] == end
            b += runs[b][0] == end
        return out

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    # ---- the walk
    def story(self, name) -> list:
        cp0, cp1 = self.ranges[name]
        s = doc_fib.story(self.word, self.pieces, cp0, cp1, self.lid)
        return self.walk(_State(name, s))

    def walk(self, st) -> list:
        """The text of st.story into blocks, one segment of constant character properties at a time (st.run):
        plain runs are text and each control character runs its handler with its CP (story.cp_of its index: exact
        past surrogate pairs and pieces cut short). A story that does not end with a paragraph mark still ends its
        last paragraph. O(len(text) + (segments + controls) * log(pieces))."""
        story = st.story
        text, cp_of = story.text, story.cp_of
        on, drop, controls = self.on, self.drop, _CONTROL.finditer
        for i0, i1, st.run, st.seg in self.segments(story):
            pos = i0
            for m in controls(text, i0, i1):
                i = m.start()
                if i > pos:
                    self.text(st, text[pos:i])
                on.get(text[i], drop)(st, cp_of(i))
                pos = i + 1
            if pos < i1:
                self.text(st, text[pos:i1])
        self.para_end(st, cp_of(len(text)), True)
        return st.out

    # ---- inline scopes
    @staticmethod
    def live(st) -> bool:
        """Whether what comes now is shown: outside every field instruction and under the field depth cap."""
        return not st.codes and not st.over

    def add(self, st, inline):
        """An inline into the open scope under the segment's properties: vanished goes into a Hidden, one per CHPX
        run (as one w:r with w:vanish is in docx); deleted into a Hidden("deleted") that the deletion's next runs join
        (as one w:del is)."""
        inl = st.frames[-1].inl
        run = st.run
        if run.deleted or run.vanish:
            kind = "deleted" if run.deleted else "hidden"
            h = st.hid
            if inl and inl[-1] is h and h.kind == kind and (run.deleted or st.hid_seg == st.seg):
                h.inlines.append(inline)
                return
            inline = st.hid = Hidden([inline], kind)
            st.hid_seg = st.seg
        inl.append(inline)

    def text(self, st, s):
        """Shown text, or the instruction of the field it is in (up to MAX_INSTR units), or nothing past the depth
        cap or inside a field nested in an instruction. Surrogate pairs are joined here, as the Text is built; a
        bold run's text is Bold."""
        if st.over:
            return
        if st.codes:
            f = st.fields[-1]
            if not f.result and f.size < MAX_INSTR:
                s = s[:MAX_INSTR - f.size]
                f.instr.append(s)
                f.size += len(s)
            return
        t = Text(_joined(s))
        self.add(st, Bold([t]) if st.run.bold else t)

    def push(self, st, frame) -> bool:
        if len(st.frames) < limits.MAX_DEPTH:
            st.frames.append(frame)
            return True
        self.deep()
        return False

    def close_to(self, st, n):
        """Close scopes until n are open: a link with content becomes a Link in its parent; one whose content is
        all hidden or deleted gives its parent that content alone, so its URL never shows."""
        while len(st.frames) > n:
            f = st.frames.pop()
            if f.inl:
                parent = st.frames[-1].inl
                if f.kind == "link" and not all(isinstance(it, Hidden) for it in f.inl):
                    parent.append(Link(f.url, f.inl))
                else:
                    parent.extend(f.inl)

    # ---- control character handlers: (st, cp)
    def drop(self, st, cp):
        """A control character with no meaning here."""

    def para_end(self, st, cp, last=False):
        """A paragraph mark, a cell or row mark or a page or section break: the open paragraph becomes a block (see
        block()) that place() puts in the story or in a table cell. A deleted paragraph mark (\\r) joins its
        paragraph to the next one. `last` ends a story's last paragraph, whose properties are those of its last
        character's paragraph mark, and closes the tables still open."""
        story = st.story
        mark = "" if last else story.text[story.index_of(cp)]
        if mark == "\r" and st.run.deleted:
            return
        self.close_to(st, 1)
        inl = st.frames[0].inl
        if not (last and not inl):
            p = self.para_props(cp - 1 if last else cp)
            block = self.block(p, inl)
            st.reset()
            self.place(st, p, mark, block)
        if last:
            self.close_tables(st, 0)

    def block(self, p, inl):
        """A paragraph of properties p as a block: a Heading when its style is a built-in heading or its outline level
        is 0-8 and it shows text (a list number before its text), a ListItem when it is in a list, else a Para; None
        when it has no inlines. The list marker is taken even then, as Word numbers an empty list paragraph."""
        mark = self.lists.marker(p.ilfo, p.ilvl) if p.ilfo else None
        if not inl:
            return None
        level = self.styles.heading(p.istd) or (p.outline + 1 if p.outline < 9 else 0)
        if level and _shown(inl):
            if mark and mark != "-":
                inl.insert(0, Text(mark + " "))
            return Heading(level, inl)
        if mark:
            return ListItem(min(p.ilvl, doc_fib.LEVELS - 1), mark, inl)
        return Para(inl)

    def place(self, st, p, mark, block):
        """A paragraph's block into the story, or into the open cell of the table its depth puts it in: max(itap, 1)
        with sprmPFInTable, else 0. The tables deeper than that close and the missing ones open. At depth 1 a \\x07
        ends the cell and, with sprmPFTtp, the row (its TDefTable defining the row's cells); deeper, a mark with
        sprmPFInnerTableCell ends the cell and one with sprmPFInnerTtp the row. Past MAX_DEPTH the paragraph stays a
        paragraph of the innermost open cell, its marks ending nothing, and each overflowing subtree is counted once
        under depth. A \\x07 outside a table ends a paragraph only. O(1) a paragraph past the tables it opens."""
        depth = max(p.itap, 1) if p.in_table else 0
        tables = st.tables
        if depth > limits.MAX_DEPTH:
            if not st.overflow:
                st.overflow = True
                self.deep()
            depth = max(len(tables), 1)
            row_end = cell_end = False
        else:
            st.overflow = False
            row_end = p.ttp or (depth > 1 and p.inner_row)
            cell_end = mark == "\x07" or (depth > 1 and p.inner_cell)
        self.close_tables(st, depth)
        if not depth:
            if block is not None:
                st.out.append(block)
            return
        while len(tables) < depth:
            tables.append(_Table())
        t = tables[-1]
        if block is not None:
            t.cell.append(block)
        if row_end:
            t.end_row(p.tdef)
        elif cell_end:
            t.end_cell()

    def close_tables(self, st, depth):
        """Close the tables deeper than `depth`, innermost first: each goes into the open cell of the table around it,
        the outermost into the story. Damaged TDefTable operands are counted under styles."""
        while len(st.tables) > depth:
            table, bad = _table(st.tables.pop().finish())
            if bad:
                self.ctx.skipped["styles"] += bad
            if table is not None:
                (st.tables[-1].cell if st.tables else st.out).append(table)

    def line_break(self, st, cp):
        if self.live(st):
            self.add(st, Break())

    def hyphen(self, st, cp):
        """A non-breaking hyphen."""
        self.text(st, "-")

    def field_begin(self, st, cp):
        """A field's instruction follows. Past MAX_DEPTH open fields the field, up to its end, is skipped and counted
        once under depth (the fields nested in it are not counted again)."""
        if st.over or len(st.fields) >= limits.MAX_DEPTH:
            if not st.over:
                self.deep()
            st.over += 1
            return
        st.fields.append(_Field())
        st.codes += 1

    def field_separator(self, st, cp):
        """The instruction ends and the result, which is shown, begins; a HYPERLINK to a URL makes it a link."""
        if st.over or not st.fields or st.fields[-1].result:
            return
        f = st.fields[-1]
        f.result = True
        st.codes -= 1
        url = "" if st.codes else hyperlink_url(_joined("".join(f.instr)))
        if url and self.push(st, _Frame("link", url)):
            f.frame = len(st.frames) - 1

    def field_end(self, st, cp):
        """The innermost field ends: its link, if its result opened one, closes. A stray end is dropped."""
        if st.over:
            st.over -= 1
            return
        if not st.fields:
            return
        f = st.fields.pop()
        if not f.result:
            st.codes -= 1
        if f.frame is not None:
            self.close_to(st, f.frame)


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(_ZIP):  # a Word package saved as .doc
        return ooxml.parse(path, ctx)
    if head.removeprefix(_BOM).startswith(_RTF):  # RTF saved as .doc: read in phase 2b
        raise ReadError("unsupported", "rtf")
    if not is_cfb(head):
        raise ReadError("damaged", "not a Word document")
    with Compound(path) as cf:
        return _Reader(cf, ctx).run()
