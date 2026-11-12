"""Word 97-2003 binary (.doc) to the document model.

The text is one CP stream cut into stories (doc_fib.STORIES). A story is walked once: plain runs are text, and every
character below 0x20 other than a tab goes to its handler in _Reader.SPECIAL, paragraph marks included. Logic follows
dochan office_binary/doc_binary.py (stream checks, the table stream, story ranges) and doc_stories.py (field begin,
separator and end), and anydoc formats/doc/mod.rs (the language is lidFE in a Far East document, else lid). The FIB,
the piece table and the story text are doc_fib.py's; the HYPERLINK rule and the comment note are word_fields.py's,
shared with the docx reader.

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

The other stories are cut into entries by their PLCs (MS-DOC 2.2.2: n + 1 CPs, then n data elements; logic after
dochan doc_stories.py: the PLC pairs, PlcfHdd's layout), each entry walked as the main story is, so bold, hidden text,
fields, lists and tables work in them too. Footnotes, endnotes and comments are Ref and Txt PLC pairs: Ref entry i is
the CP of its reference character (\\x02 for a note, \\x05 for a comment) and Txt range i its text, relative to its
story; each becomes doc.notes[id] and the reference a NoteRef where the walk meets that very CP. A comment is
word_fields.comment_note's, as in the docx reader, the author from GrpXstAtnOwners. Headers and footers are the
PlcfHdd stories after the six separators, six a section, each one line of doc.headers or doc.footers. Text boxes
(PlcftxbxTxt) follow the main text in story order; header text boxes (PlcfHdrtxbxTxt) are header lines. A story
with text (ccp > 0) whose PLCs hold no entry, left out or empty, is counted once under its kind.

Pictures go through officeart.py (logic after dochan office_binary/doc_images.py). An inline picture is a \\x01 with
fSpec whose PicLocation is a PICF in the Data stream; a floating one is a \\x08 with fSpec whose CP has an FSPA in
PlcSpaMom (main story) or PlcSpaHdr (header story), naming a shape of the DggInfo's drawings, its pib a BStore entry.
A picture is an Image block after its paragraph, in that paragraph's story, cell or note, as the docx reader puts
pictures in st.extras; in vanished or deleted text it is hidden as the text is. Each is read under guard("image") and
one that cannot be read is counted there; every deflated BLIP shares the document's flate.Budget.
"""
import re
import struct
from bisect import bisect_right
from itertools import islice
from operator import gt

from .. import images, limits, officeart
from ..containers import flate
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Break, Cell, Doc, Heading, Hidden, Image, Link, ListItem, NoteRef, Para, Table, Text
from . import doc_fib, ooxml
from .hancom import flat
from .word_fields import MAX_INSTR, comment_note, hyperlink_url

_ZIP = b"PK\x03\x04"
_RTF = b"{\\rtf"
_BOM = b"\xef\xbb\xbf"
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f]")  # the characters the walk hands to a handler; a tab is text
_MARK = re.compile(r"[\r\x07\x0c]")  # the characters that end a paragraph and own its PAPX
_SURROGATE = re.compile("[\ud800-\udfff]")
_C0 = re.compile(r"[\x00-\x1f]")
_HUGE_PAPX = 0x6646
_PLAIN = doc_fib.RunProps()

# FibRgFcLcb97 indices of the stories' PLCs (MS-DOC 2.5.6) and GrpXstAtnOwners
_HDD, _ATN_OWNERS, _TXBX, _HDR_TXBX = 11, 36, 56, 58
# the note stories: story, id prefix, Ref and Txt PLC indices (PlcffndRef/Txt, PlcfendRef/Txt, PlcfandRef/Txt), the
# size of a Ref data element (an FRD, or an ATRDPre10: xstUsrInitl, then ibst u16 at 20) and the kind a loss counts as
_NOTES = (("footnote", "fn", 2, 3, 2, "footnotes"), ("endnote", "en", 46, 47, 2, "footnotes"),
          ("annotation", "cm", 4, 5, 30, "comments"))
_FTXBXS = 22      # bytes of an FTXBXS: reusable01, reusable02, fReusable i16 at 8, itxbxsDest, lid, txidUndo
_SEPARATORS = 6   # PlcfHdd stories before the first section's: the footnote and endnote separators and notices
_SECTION = 6      # PlcfHdd stories a section: even header, odd header, even footer, odd footer, first header, footer
_FOOTERS = frozenset((2, 3, 5))  # their places among a section's six
_OWNERS = 0x10000  # GrpXstAtnOwners names an ibst (u16) can index
PLC_MAX = 1_000_000  # entries one story PLC may hold; past it the PLC is not read and its story is counted
IMAGE_BYTES = 256 * 2**20  # image bytes one document may emit, copies counted
# the stories MS-DOC gives a PlcfSpa (PlcSpaMom, PlcSpaHdr), each one's CPs counted from that story's start
_SPA = {"text": 40, "header": 41}
_FSPA = 26          # bytes of an FSPA: spid u32 first
_DGG_INFO = 50      # the FibRgFcLcb97 index of the OfficeArtContent
_DG_CONTAINER = 0xF002
_MM_WMF, _MM_SHAPE, _MM_SHAPEFILE = 8, 0x64, 0x66  # PICF mfpf.mm: a raw WMF, an inline shape, one with a name
_MISS = object()


def _plc(data: bytes, cb: int) -> tuple[list[int], list[bytes]]:
    """A PLC: n + 1 u32 CPs, then n cb-byte data elements, n = (lcb - 4) / (4 + cb). ValueError when n is not whole
    or past PLC_MAX, or the CPs decrease; no CPs when there is no PLC."""
    if not data:
        return [], []
    if len(data) < 4:
        raise ValueError("PLC size")
    n, rest = divmod(len(data) - 4, 4 + cb)
    if rest or n > PLC_MAX:
        raise ValueError("PLC size")
    cps = list(struct.unpack_from("<%dI" % (n + 1), data))
    if any(map(gt, cps, islice(cps, 1, None))):
        raise ValueError("PLC CPs decrease")
    at = 4 * (n + 1)
    return cps, [data[at + cb * i:at + cb * (i + 1)] for i in range(n)] if cb else []


def _author(atrd: bytes, owners: list[str]) -> str:
    """A comment's author: GrpXstAtnOwners[ibst], else (ibst past the list, or the name empty) the initials of the
    ATRDPre10's xstUsrInitl (u16 count, at most 9 UTF-16 units). Control characters become spaces."""
    ibst = struct.unpack_from("<H", atrd, 20)[0]
    name = owners[ibst] if ibst < len(owners) else ""
    if not name:
        name = atrd[2:2 + 2 * min(struct.unpack_from("<H", atrd)[0], 9)].decode("utf-16-le", "replace")
    return _C0.sub(" ", name)


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
    TCGRF merges, read as Word writes them and LibreOffice reads them (ww8par2.cxx; the brief and dochan agree):
    fFirstMerged 0x0001 starts a horizontal set (with or without 0x0002) that the next cells with fMerged 0x0002 alone
    join, so adjacent merged pairs stay two sets. MS-DOC 2.9.328's horzMerge table reads the two bits the other way
    round (1 merged, 2 or 3 first), which real files contradict. fVertRestart 0x0040 starts a vertical set that the
    cell over the same grid columns in each next row with fVertMerge 0x0020 alone joins. A joined cell is left out and
    its blocks go to the set's first cell, which takes its colspan or one more rowspan. O(cells + boundaries log
    boundaries); the spans go through the renderer's GRID_COLS and GRID_SLOTS budgets."""
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
        sets = []  # [col, span, TCGRF, blocks, open to fMerged] once the horizontal merges are made
        for k, blocks in enumerate(cells):
            span = col_of[bounds[k + 1]] - col_of[bounds[k]] if k + 1 < len(bounds) else 1
            g = grf[k] if k < len(grf) else 0
            if (g & 3) == 2 and sets and sets[-1][4]:
                sets[-1][1] += span
                sets[-1][3].extend(blocks)
            else:
                sets.append([col, span, g, blocks, bool(g & 1)])
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
    past MAX_DEPTH, so one overflowing subtree is counted once. extras are the blocks (pictures) that follow the open
    paragraph where it goes."""

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
        self.extras = []


class _Drawing:
    """The document's drawings (_Reader.drawing): the anchors of each story of _SPA ({CP from the story's start:
    spid}), the SpContainers by spid, the pib of each spid looked up so far, the BStore's file blocks and the DggInfo
    bytes they are offsets in."""

    def __init__(self):
        self.anchors = {name: {} for name in _SPA}
        self.shapes = officeart.Shapes()
        self.pibs = {}
        self.store = []
        self.dgg = b""


class _Reader:
    # a control character -> its handler (st, cp), cp being the character's CP; one not listed is dropped. A cell or
    # row mark (\x07) ends its paragraph too; place() gives it its table meaning.
    SPECIAL: dict[str, str] = {
        "\r": "para_end", "\x07": "para_end", "\x0c": "para_end",
        "\x0a": "line_break", "\x0b": "line_break",
        "\x02": "note_ref", "\x05": "comment_ref",
        "\x13": "field_begin", "\x14": "field_separator", "\x15": "field_end",
        "\x1e": "hyphen",
        "\x01": "picture", "\x08": "drawn",
    }

    def __init__(self, cf, ctx, budget=None):
        self.cf, self.ctx = cf, ctx
        self.budget = budget or flate.Budget()  # the one flate.Budget every deflated BLIP of the document shares
        self.image_bytes = 0  # image bytes placed so far, copies counted (IMAGE_BYTES)
        self._inline, self._stored = {}, {}  # memos: Data offset -> picture; pib -> picture (bytes or None)
        self._drawing = None  # the _Drawing once read
        self.doc = Doc()
        self.word = b""
        self.table = b""
        self.fib = None
        self.pieces = []
        self.lid = 0x0409  # the language of compressed text (doc_fib.compressed_codepage)
        self.ranges = {}   # story name -> (cp0, cp1), clamped to the CPs the pieces hold
        self.base = {}     # story name -> its first CP, unclamped: a story PLC's CPs count from it
        self.note_at = {}     # CP -> the id of the footnote or endnote whose reference (\x02) is there
        self.comment_at = {}  # CP -> the id of the comment whose reference (\x05) is there
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
        at = 0
        for name in doc_fib.STORIES:
            self.base[name] = at
            at += f.ccp[name]
        self.props()
        self.notes()
        self.headers()
        with self.ctx.guard("document"):
            self.doc.blocks.extend(self.story("text"))
        for blocks in self.boxes("textbox", _TXBX, "block"):
            self.doc.blocks.extend(blocks)
        if self.styles.bad:
            self.ctx.skipped["styles"] += self.styles.bad
        return self.doc

    # ---- the other stories
    def story_plc(self, index, cb) -> tuple[list[int], list[bytes]]:
        """The PLC at FibRgFcLcb97 entry `index` (see _plc); ValueError when it is outside the table stream."""
        fc, lcb = self.fib.pair(index)
        if not lcb:
            return [], []
        if fc + lcb > len(self.table):
            raise ValueError("PLC outside the table stream")
        return _plc(self.table[fc:fc + lcb], cb)

    def owners(self) -> list[str]:
        """GrpXstAtnOwners: the comment authors, an Xst (u16 count, UTF-16 units) each. The list ends where an Xst does
        not fit, or at as many names as an ibst can index; it is empty when it is outside the table stream."""
        fc, lcb = self.fib.pair(_ATN_OWNERS)
        if not lcb or fc + lcb > len(self.table):
            return []
        raw, out, pos = self.table[fc:fc + lcb], [], 0
        while pos + 2 <= lcb and len(out) < _OWNERS:
            end = pos + 2 + 2 * struct.unpack_from("<H", raw, pos)[0]
            if end > lcb:
                break
            out.append(raw[pos + 2:end].decode("utf-16-le", "replace"))
            pos = end
        return out

    def storyless(self, name, kind, entries):
        """Story `name` has text (ccp > 0) but its PLCs hold no entry (`entries` false: left out, lcb 0, or empty), so
        nothing addresses its text: it is counted once under kind. A story with entries is not: its last range, the
        guard paragraph mark, is no loss."""
        if not entries and self.fib.ccp[name] > 0:
            self.ctx.skipped[kind] += 1

    def notes(self):
        """Footnotes, endnotes and comments (_NOTES). A story's Ref and Txt PLCs are read under guard(kind): one that
        is not a whole PLC, whose CPs decrease or whose texts run past the story's ccp loses that story, counted once.
        Ref entry i owns Txt range i: a reference without a range, and a range past the Ref entries other than the
        last (the story's guard paragraph mark, MS-DOC: Txt holds one CP more than Ref), are counted under kind. An
        empty range is no note. Every note's slot in doc.notes is reserved before any text is read, its id unique to
        its PLC entry, so a reference in a note's own text (to itself, to a note read later, across stories) is a
        NoteRef to that slot and nothing is read twice or recursively; then each text is read in PLC order under
        guard(kind), and one that fails gives up its slot. A story with text and neither Ref nor Txt entries is
        counted once (storyless)."""
        pending = []
        for name, prefix, ref_index, txt_index, cb, kind in _NOTES:
            with self.ctx.guard(kind):
                refs, data = self.story_plc(ref_index, cb)
                txt, _ = self.story_plc(txt_index, 0)
                n, have = len(data), max(len(txt) - 1, 0)
                self.storyless(name, kind, n or have)
                k = min(n, have)
                if k and txt[k] > self.fib.ccp[name]:
                    raise ValueError("note text past its story")
                comment = name == "annotation"
                owners = self.owners() if comment and k else []
                found = [(refs[i], (f"{prefix}-{i + 1}", name, txt[i], txt[i + 1], kind,
                                    _author(data[i], owners) if comment else None))
                         for i in range(k) if txt[i] < txt[i + 1]]
                at = self.comment_at if comment else self.note_at
                for cp, note in found:
                    at.setdefault(cp, note[0])
                    self.doc.notes[note[0]] = []
                    pending.append(note)
                lost = n - k + max(have - n - 1, 0)
                if lost:
                    self.ctx.skipped[kind] += lost
        for nid, name, cp0, cp1, kind, author in pending:
            done = False
            with self.ctx.guard(kind):
                blocks = self.read(name, cp0, cp1)
                self.doc.notes[nid] = blocks if author is None else comment_note(blocks, author)
                done = True
            if not done:
                del self.doc.notes[nid]

    def headers(self):
        """PlcfHdd: a header story a range, relative to the header story, read under guard("header"): the separators
        are skipped, then each section's six stories (_SECTION) give one line each to doc.headers or doc.footers, read
        under guard("header") or guard("footer"). An empty range is no story; a last range left over past whole
        sections is the story's guard paragraph mark. A PLC that is not whole, whose CPs decrease or whose stories run
        past the header story's ccp loses the headers, counted once, and so does header text without a PlcfHdd entry
        (storyless). Then the header text boxes, one line each."""
        spans = []
        with self.ctx.guard("header"):
            cps, _ = self.story_plc(_HDD, 0)
            end = len(cps) - 1
            self.storyless("header", "header", end > 0)
            if end > _SEPARATORS and (end - _SEPARATORS) % _SECTION == 1:
                end -= 1
            if end > _SEPARATORS and cps[end] > self.fib.ccp["header"]:
                raise ValueError("header past its story")
            spans = [(k, cps[k], cps[k + 1]) for k in range(_SEPARATORS, end) if cps[k] < cps[k + 1]]
        for k, a, b in spans:
            footer = (k - _SEPARATORS) % _SECTION in _FOOTERS
            with self.ctx.guard("footer" if footer else "header"):
                text = flat(self.read("header", a, b))
                if text:
                    (self.doc.footers if footer else self.doc.headers).append(text)
        for blocks in self.boxes("header_textbox", _HDR_TXBX, "header"):
            text = flat(blocks)
            if text:
                self.doc.headers.append(text)

    def boxes(self, name, index, kind) -> list[list]:
        """The blocks of each text box of PlcftxbxTxt or PlcfHdrtxbxTxt, in story order: a range a text box, relative
        to story `name`, with a 22-byte FTXBXS; the last FTXBXS is a dummy for the story's guard paragraph mark, and a
        reusable one (fReusable) is a text box no longer in use, so neither is read. The PLC is read under
        guard(kind), each text box too; a PLC that is not whole, whose CPs decrease or whose boxes run past the
        story's ccp loses the story, counted once, and so does story text without a PLC entry (storyless)."""
        spans, out = [], []
        with self.ctx.guard(kind):
            cps, data = self.story_plc(index, _FTXBXS)
            self.storyless(name, kind, data)
            m = len(data) - 1
            if m > 0 and cps[m] > self.fib.ccp[name]:
                raise ValueError("text box past its story")
            spans = [(cps[i], cps[i + 1]) for i in range(max(m, 0))
                     if cps[i] < cps[i + 1] and not struct.unpack_from("<h", data[i], 8)[0]]
        for a, b in spans:
            with self.ctx.guard(kind):
                out.append(self.read(name, a, b))
        return out

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

    def read(self, name, cp0, cp1) -> list:
        """The blocks of CPs [cp0, cp1) of story `name`, counted from its start and clamped to the story's CPs the
        pieces hold."""
        a, b = self.ranges[name]
        base = self.base[name]
        s = doc_fib.story(self.word, self.pieces, min(max(base + cp0, a), b), min(max(base + cp1, a), b), self.lid)
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
        block()) that place() puts in the story or in a table cell, its extras (pictures) after it. A deleted
        paragraph mark (\\r) joins its paragraph, extras too, to the next one. `last` ends a story's last paragraph,
        whose properties are those of its last character's paragraph mark, and closes the tables still open."""
        story = st.story
        mark = "" if last else story.text[story.index_of(cp)]
        if mark == "\r" and st.run.deleted:
            return
        self.close_to(st, 1)
        inl, extras = st.frames[0].inl, st.extras
        if inl or extras or not last:
            p = self.para_props(cp - 1 if last else cp)
            block = self.block(p, inl)
            st.reset()
            self.place(st, p, mark, extras if block is None else [block, *extras])
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

    def place(self, st, p, mark, blocks):
        """A paragraph's blocks into the story, or into the open cell of the table its depth puts it in: max(itap, 1)
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
            st.out.extend(blocks)
            return
        while len(tables) < depth:
            tables.append(_Table())
        t = tables[-1]
        t.cell.extend(blocks)
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

    def note_ref(self, st, cp):
        """A footnote or endnote reference: the note whose PlcffndRef or PlcfendRef entry is this very CP. Any other
        \\x02 (a note's own number, at the start of its text) is dropped."""
        self.ref(st, self.note_at.get(cp))

    def comment_ref(self, st, cp):
        """A comment reference: the comment whose PlcfandRef entry is this very CP; any other \\x05 is dropped."""
        self.ref(st, self.comment_at.get(cp))

    def ref(self, st, nid):
        """A NoteRef to note `nid` while it has a slot, through add(): in vanished or deleted text it is in a Hidden
        and its note still follows, as the docx reader gives a w:footnoteReference in a w:vanish run or in w:del. In
        a field instruction it is dropped, as text there is."""
        if nid in self.doc.notes and self.live(st):
            self.add(st, NoteRef(nid))

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

    # ---- pictures
    def picture(self, st, cp):
        """An inline picture: a \\x01 with fSpec, its PICF at its PicLocation in the Data stream (0, the CHP's
        default, without sprmCPicLocation), read once a location. An OLE object's \\x01 (fOle2) is not a picture
        here; one in a field instruction (a form field's data) is not shown, as text there is not."""
        run = st.run
        if not run.special or run.ole or not self.live(st):
            return
        with self.ctx.guard("image"):
            at = run.pic or 0
            hit = self._inline.get(at, _MISS)
            if hit is _MISS:
                hit = self._inline[at] = self.picf(at)
            self.put_image(st, hit)

    def drawn(self, st, cp):
        """A floating object: a \\x08 with fSpec, the FSPA at its CP in its story's PlcfSpa (_SPA: PlcSpaMom for the
        main story, PlcSpaHdr for the header story, whose CPs count from the header story's start as MS-DOC
        FibRgFcLcb97 says), the SpContainer of the FSPA's spid, its pib (looked up once a spid), the BStore entry and
        its BLIP. MS-DOC gives the other stories no PlcfSpa, so a \\x08 there is not resolved. No FSPA at the CP, a
        spid no drawing holds, or a shape without a pib (a text box, whose text boxes() reads, a line, a drawing) is
        no picture and no loss; a pib whose BStore entry or BLIP cannot be read is counted under image."""
        if not st.run.special or not self.live(st) or st.name not in _SPA:
            return
        with self.ctx.guard("image"):
            d = self.drawing()
            spid = d.anchors[st.name].get(cp - self.base[st.name])
            if spid is None:
                return
            pib = d.pibs.get(spid, _MISS)
            if pib is _MISS:
                at = d.shapes.at.get(spid)
                pib = d.pibs[spid] = officeart.shape_pib(d.dgg, at) if at is not None else None
            if pib:
                self.put_image(st, self.stored(pib))

    def put_image(self, st, data):
        """A picture into st.extras, hidden as its run is: deleted, else vanished. It is counted under image instead
        when it could not be read, when Assets would not keep its bytes (under 16, or no kind images.sniff knows), or
        past IMAGE_BYTES placed, every copy counted."""
        if data is None or len(data) < 16 or images.sniff(data) is None:
            self.ctx.skipped["image"] += 1
            return
        self.image_bytes += len(data)
        if self.image_bytes > IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        run = st.run
        st.extras.append(Image(data, hidden="deleted" if run.deleted else "hidden" if run.vanish else ""))

    def decode(self, read, *args) -> bytes | None:
        """read(*args, self.budget), officeart's fbse_blip or blip. A BLIP that inflates past the ratio cap or the
        document's total, or is damaged (ReadError too_large or damaged), is None, so it costs that picture alone, as
        hwp's load_bin; another ReadError ends the document."""
        try:
            return read(*args, self.budget)
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            return None

    def picf(self, at) -> bytes | None:
        """The picture of the PICFAndOfficeArtData at Data offset `at`: the PICF (lcb u32, cbHeader u16, mfpf.mm u16
        at 6), for mm 0x66 (MM_SHAPEFILE) cchPicName u8 and the name, then the inline SpContainer and its FBSEs, the
        first FBSE giving the picture; with no FBSE, the shape's pib in the document's BStore (dochan). mm 8 is a raw
        WMF after the PICF. None without a Data stream, for a length that passes the stream or the PICF (cbHeader past
        lcb), or another mm."""
        data = self.data_stream()
        if data is None or at + 8 > len(data):
            return None
        lcb, cb, mm = struct.unpack_from("<IHH", data, at)
        end = at + lcb
        if end > len(data) or not 8 <= cb <= lcb:
            return None
        pos = at + cb
        if mm == _MM_WMF:
            return data[pos:end]
        if mm not in (_MM_SHAPE, _MM_SHAPEFILE):
            return None
        if mm == _MM_SHAPEFILE:
            if pos >= end:
                return None
            pos += 1 + data[pos]
        sp = None
        for _, _, typ, body, _ in officeart.records(data, pos, end):
            if typ == officeart.FBSE:
                return self.decode(officeart.fbse_blip, data, body - 8, self.word)
            if typ == officeart.SP_CONTAINER and sp is None:
                sp = body - 8
        pib = officeart.shape_pib(data, sp) if sp is not None else None
        return self.stored(pib) if pib else None

    def stored(self, pib) -> bytes | None:
        """BStore entry `pib` (1-based) as image bytes, read once a pib; None past the BStore or when its FBSE or BLIP
        cannot be read. A delay-loaded BLIP is in WordDocument (MS-DOC OfficeArtContent)."""
        hit = self._stored.get(pib, _MISS)
        if hit is _MISS:
            d = self.drawing()
            ok = 0 < pib <= len(d.store)
            hit = self.decode(officeart.fbse_blip, d.dgg, d.store[pib - 1], self.word) if ok else None
            self._stored[pib] = hit
        return hit

    def drawing(self) -> _Drawing:
        """The drawings, read once, at the first picture that needs them: the anchors of PlcSpaMom and PlcSpaHdr
        (26-byte FSPAs, spid first; the first FSPA at a CP wins), then the DggInfo (MS-DOC OfficeArtContent): a
        DggContainer holding the BStore, then an OfficeArtWordDrawing each (dgglbl u8, a DgContainer), the main
        document's and the header document's, whose shapes go into one spid index; the content ends at anything else.
        Damage that loses shapes is counted once under shape: a PlcfSpa or DggInfo outside the table stream or not
        whole, a DggInfo that does not start with a whole DggContainer, a DgContainer cut short (its shapes before
        the cut are kept), records past officeart.MAX_RECORDS; containers nested past MAX_DEPTH once under depth."""
        d = self._drawing
        if d is not None:
            return d
        d = self._drawing = _Drawing()
        for name, index in _SPA.items():
            with self.ctx.guard("shape"):
                cps, fspas = self.story_plc(index, _FSPA)
                at = d.anchors[name]
                for cp, fspa in zip(cps, fspas):
                    at.setdefault(cp, struct.unpack_from("<I", fspa)[0])
        with self.ctx.guard("shape"):
            fc, lcb = self.fib.pair(_DGG_INFO)
            if not lcb:
                return d
            if fc + lcb > len(self.table):
                raise ValueError("DggInfo outside the table stream")
            d.dgg = dgg = self.table[fc:fc + lcb]
            h = officeart.header(dgg, 0)
            if h is None or h[2] != officeart.DGG_CONTAINER or h[3] > lcb - 8:
                raise ValueError("no DggContainer")
            d.store = officeart.bstore(dgg)
            pos = 8 + h[3]
            while pos + 9 <= lcb:
                h = officeart.header(dgg, pos + 1)
                if h[2] != _DG_CONTAINER:
                    break
                body = pos + 9
                end = body + h[3]
                if end > lcb:
                    self.ctx.skipped["shape"] += 1
                    end = lcb
                d.shapes.add(dgg, body, end)
                pos = end
            if d.shapes.deep:
                self.deep()
            if d.shapes.cut:
                self.ctx.skipped["shape"] += 1
        return d


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
        return _Reader(cf, ctx, flate.Budget()).run()  # one inflate budget a document
