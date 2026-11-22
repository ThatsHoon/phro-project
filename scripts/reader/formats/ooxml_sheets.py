"""SpreadsheetML workbooks (xlsx and its kin) to the document model: one sheet section per worksheet, displayed values.

The shared strings and rich text without phonetics, the styles (`numFmts` and the `cellXfs` format ids) and the cell
types follow anydoc `sheet/xlsx.rs`; the streaming walk, which keeps one cell in memory, follows dochan
`ooxml/xlsx.py`. Elements match by local name, so Transitional and Strict files take the same path.

The workbook part gives the sheets in file order. A relationship of type worksheet or chartsheet is read; any other
type (a macro or dialog sheet) and a missing relationship are counted under `sheet`. Styles and shared strings are
streamed, the shared strings under their own cap (`SHEET_CHARS` characters, `SHEET_SCAN` entries), and each part
under a closed-element cap (SST_ELEMENTS, STYLE_ELEMENTS); one that is named but missing, damaged or capped counts
`document`. Text escapes (`_xHHHH_`) are undone in strings, str and error values, formulas and sheet names. Every
sheet part is streamed, and every element it closes is charged to the workbook's scan count, capped at `SHEET_SCAN`.
A sheet reached once the count is spent is cut without being opened, its comments unread (counted as `comments_cut`
when it has some), so neither is charged to ZIP_TOTAL ahead of its drawings.
A cell shows its cached value; a formula without one shows `=` and its text. A sheet that cannot be read is counted
under `sheet` and the others still come out; one whose XML breaks after its first row keeps the rows read.
Charts and pictures come from `sheet_drawing`, one `Drawings` a workbook (one CHART_TOTAL and IMAGE_BYTES count),
through the relationships the sheet read once: a worksheet's follow its grid in anchor order, and a chartsheet's are
all it holds (one with nothing drawn has no section). A drawing ReadError other than a timeout costs only the drawings,
counted under `object`.
Hidden rows and columns and merged ranges go to the grid as they are read. Hyperlinks follow the cells, so they are
collected (at most _LINKS a sheet) and applied after the stream: an external one wraps the cells in its range that
hold a value in a link, at a cost of min(area, cells held) charged to the scan count; past SHEET_SCAN each link left
is counted under `link`. A `location` alone changes nothing; an `r:id` without an external hyperlink relationship
counts `link`.
Cell comments become notes (legacy `comments` parts ported from dochan, threaded ones new). A thread is its root and
then its replies, each a "Comment by <name>:" paragraph of one note, the names from the workbook's persons part; a
legacy comment on a cell a thread covers is its placeholder and is dropped. The note's mark follows the cell's text
(and its link); a commented cell with no value holds the mark alone. At most MAX_COMMENTS comments are read a
workbook (the rest counted once as `comments_cut`). The parts are streamed under _COMMENT_ELEMENTS closed elements each, empty
and out-of-grid comments included. Notes are placed after the cells: first the cells that hold only a mark, then the
marks appended to valued cells, each only if its put succeeds and its text fits the character budget that the cells
share (else the note is dropped, counted once as `comments_cut`). A comment on a merged-away cell moves its mark to
the merge's origin; one whose cell lies in a hidden row or column is hidden with it. A missing or damaged comments, threaded or persons part counts `comments`. A hidden sheet's notes are hidden
with it.
"""
import math
import posixpath
import re
from datetime import datetime

from .. import limits
from ..containers.zipx import attr, iterparse, local
from ..limits import ReadError
from ..model import Doc, Link, NoteRef, Para, Section, Text
from ..numfmt import code_for, display
from . import ooxml_base, sheet_drawing, sheet_grid
from .word_fields import MAX_URL, comment_note
from .xls import _hide

_REF = re.compile(r"([A-Za-z]{1,3})([0-9]{1,7})")  # used with fullmatch
_ESC = re.compile(r"_x([0-9A-Fa-f]{4})_")
_SURROGATE = re.compile("[\ud800-\udfff]")
_HIDDEN = frozenset({"hidden", "veryHidden"})
SST_ELEMENTS = 2  # times SHEET_SCAN: the elements a shared-strings part may close (SHEET_SCAN plain si/t entries fit)
# elements a styles part may close before `cellXfs` ends (the read stops there): a style-bloated workbook near Excel's
# format limit (about 64,000 xfs of up to three closes in `cellStyleXfs` and again in `cellXfs`) fits with room
STYLE_ELEMENTS = 1_000_000
# the most raw characters one unescaped character can take: a surrogate pair (_xD83D__xDE00_) or an escaped CRLF
# (_x000D__x000A_), each two 7-character escapes for one character
_RAW_PER_CHAR = 14
_LINKS = 100_000  # hyperlink elements one sheet may collect; the rest are cut, counted once
_SHEET_TAGS = frozenset({"col", "mergeCell", "hyperlink"})
_PARENT = {"col": "cols", "mergeCell": "mergeCells", "hyperlink": "hyperlinks"}
MAX_COMMENTS = 10_000  # comments one workbook may turn into notes; the rest are cut, counted once as `comments_cut`
_COMMENT_ELEMENTS = 200_000  # elements a comments, threaded comments or persons part may close; past it the part stops
_BATCH = 256  # ended siblings a streamed element keeps before they are dropped together


def _unescape(text: str) -> str:
    """The text an OOXML string stands for: `_xHHHH_` is the UTF-16 code unit HHHH, in one pass (so `_x005F_x000D_`
    is the literal `_x000D_`); surrogate pairs join and a lone surrogate becomes U+FFFD; `\\r\\n` and `\\r` are
    newlines."""
    if "_x" in text:
        text = _ESC.sub(lambda m: chr(int(m.group(1), 16)), text)
        if _SURROGATE.search(text):
            text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _rich(el) -> str:
    """The text of an `si` or `is`: its direct `t`, then each run's `t`. Phonetic runs (`rPh`) are left out."""
    parts = []
    for ch in el:
        n = local(ch.tag)
        if n == "t":
            parts.append(ch.text or "")
        elif n == "r":
            parts.extend(t.text or "" for t in ch if local(t.tag) == "t")
    return _unescape("".join(parts))


def _col(ref) -> int | None:
    """The zero-based column of an A1 reference, or None when it is not one."""
    m = _REF.fullmatch(ref or "")
    if m is None:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _range(ref):
    """(r1, r2, c1, c2), zero-based and inclusive, of an `A1` or `A1:B2` reference; None when it is not one."""
    a, _, b = (ref or "").strip().partition(":")
    ma, mb = _REF.fullmatch(a), _REF.fullmatch(b or a)
    if ma is None or mb is None:
        return None
    r1, r2, c1, c2 = int(ma.group(2)) - 1, int(mb.group(2)) - 1, _col(a), _col(b or a)
    if min(r1, r2) < 0:
        return None
    return min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)


def _int(s, default=None):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


class _Wb:
    """The workbook-level facts: sheets (name, part, state), the 1904 flag, shared strings and cell XF formats."""

    def __init__(self):
        self.sheets: list[tuple[str, str, str]] = []
        self.date1904 = False
        self.sst: list[str] = []
        self.xf_fmt: list[int] = []
        self.formats: dict[int, str] = {}
        self.chartsheets: set[str] = set()  # sheet parts whose relationship is a chartsheet
        self.part = ""  # the workbook part


def _skip(ctx, kind, e):
    """A member that cannot be read: any kind but a timeout is counted under `kind` and the read goes on."""
    if e.kind == "timeout":
        raise e
    ctx.skipped[kind] += 1


def _find(pkg, part, rels, kind, default):
    """(part, named): the part a relationship of that type names, and whether one names it at all; the conventional
    name beside the workbook only when no relationship of that type exists. The part is None when there is none."""
    named = False
    for rel in rels.values():
        if rel.type.endswith("/" + kind) and not rel.external:
            named = True
            if pkg.has(rel.target):
                return rel.target, True
    conventional = posixpath.join(posixpath.dirname(part), default)
    if not named and pkg.has(conventional):
        return conventional, False
    return None, named


def _drop(el, els, done):
    """Let go of an ended element in a streamed part. `els` is the open path (`el` already taken off) and `done` the
    count of ended children each open element still holds, `el`'s own on top. Its children go now; it goes from its
    parent with the next _BATCH ended siblings, in one slice. The parser has already added the children of a whole
    fed chunk, so `remove`, which searches from the first child, would cost the chunk's width per element."""
    if done.pop():
        del el[:]
    if els:
        done[-1] += 1
        if done[-1] >= _BATCH:
            del els[-1][:done[-1]]
            done[-1] = 0


def _workbook(root, rels, wb, ctx):
    for el in root:
        n = local(el.tag)
        if n == "workbookPr":
            wb.date1904 = (attr(el, "date1904") or "").strip().lower() in ("1", "true")
        elif n == "sheets":
            for sh in el:
                if local(sh.tag) != "sheet":
                    continue
                rel = rels.get(attr(sh, "id") or "")
                if rel is None or rel.external or not rel.type.endswith(("/worksheet", "/chartsheet")):
                    ctx.skipped["sheet"] += 1  # a macro or dialog sheet, or no relationship at all
                    continue
                if rel.type.endswith("/chartsheet"):
                    wb.chartsheets.add(rel.target)
                wb.sheets.append((_unescape(attr(sh, "name") or ""), rel.target, attr(sh, "state") or ""))


def _styles(pkg, part, wb, ctx):
    """`numFmts/numFmt` codes and the `cellXfs/xf` format ids, streamed; `cellStyleXfs` is not read. The read ends
    with `cellXfs` (by schema order `numFmts` comes before it, and nothing after it is used). Past STYLE_ELEMENTS
    closed elements the part stops, counted under `document`, and what was read is kept."""
    seen, els, tags, done, names = 0, [], [], [], {}
    with pkg.open(part) as f:
        for ev, el in iterparse(f, ("start", "end")):
            name = names.get(el.tag)
            if name is None:
                name = names[el.tag] = local(el.tag)
            if ev == "start":
                els.append(el)
                tags.append(name)
                done.append(0)
                continue
            els.pop()
            tags.pop()
            seen += 1
            if seen > STYLE_ELEMENTS:
                ctx.skipped["document"] += 1
                return
            if tags:
                if name == "numFmt" and tags[-1] == "numFmts":
                    fid = _int(attr(el, "numFmtId"))
                    if fid is not None:
                        wb.formats[fid] = attr(el, "formatCode") or ""
                elif name == "xf" and tags[-1] == "cellXfs":
                    wb.xf_fmt.append(_int(attr(el, "numFmtId"), 0))
                elif name == "cellXfs" and tags[-1] == "styleSheet":
                    return
            _drop(el, els, done)


def _strings(pkg, part, wb, ctx):
    """The shared-string table, streamed; past its character or entry cap, or SST_ELEMENTS times SHEET_SCAN closed
    elements, it stops, counted once (decision 2)."""
    chars_cap, entries_cap = limits.SHEET_CHARS, limits.SHEET_SCAN
    cap = SST_ELEMENTS * limits.SHEET_SCAN
    seen, total, size, parts, els, tags, done, names = 0, 0, 0, [], [], [], [], {}
    with pkg.open(part) as f:
        for ev, el in iterparse(f, ("start", "end")):
            name = names.get(el.tag)
            if name is None:
                name = names[el.tag] = local(el.tag)
            if ev == "start":
                els.append(el)
                tags.append(name)
                done.append(0)
                if name == "si":
                    parts, size = [], 0
                continue
            els.pop()
            tags.pop()
            seen += 1
            if seen > cap:
                return _cut(ctx, f"{cap} XML elements")
            if name == "t" and tags:
                up = tags[-1]
                if up == "si" or (up == "r" and len(tags) >= 2 and tags[-2] == "si"):
                    text = el.text or ""
                    size += len(text)
                    # past the cap even after unescaping, which shrinks a text by at most _RAW_PER_CHAR: stop now,
                    # so one entry's raw text held here stays bounded
                    if size > _RAW_PER_CHAR * (chars_cap - total):
                        return _cut(ctx, f"{chars_cap} characters")
                    parts.append(text)
            elif name == "si":
                if len(wb.sst) >= entries_cap:
                    return _cut(ctx, f"{entries_cap} entries")
                text = _unescape("".join(parts))
                if total + len(text) > chars_cap:
                    return _cut(ctx, f"{chars_cap} characters")
                total += len(text)
                wb.sst.append(text)
                parts, size = [], 0
            _drop(el, els, done)


def _cut(ctx, what):
    ctx.warnings.append(f"shared strings cut at {what}")
    ctx.skipped["sheet_cut"] += 1


def _no_styles(wb):
    wb.formats, wb.xf_fmt = {}, []


def _no_strings(wb):
    wb.sst = []


def read_workbook(pkg, part, ctx) -> _Wb:
    """The workbook part's sheet list and date system, then its styles and shared strings. A styles or
    shared-strings part that is named but missing, damaged or capped counts `document` and the sheets still read:
    a ReadError keeps what was read before it, any other error leaves General formats or no shared strings."""
    root, _ = ooxml_base.load(pkg, part)
    rels = ooxml_base.rels(pkg, part, ctx)
    wb = _Wb()
    wb.part = part
    _workbook(root, rels, wb, ctx)
    for kind, default, read, reset in (("styles", "styles.xml", _styles, _no_styles),
                                       ("sharedStrings", "sharedStrings.xml", _strings, _no_strings)):
        name, named = _find(pkg, part, rels, kind, default)
        if name is None:
            if named:
                ctx.skipped["document"] += 1
            continue
        try:
            read(pkg, name, wb, ctx)
        except ReadError as e:
            _skip(ctx, "document", e)
        except Exception:
            reset(wb)
            ctx.skipped["document"] += 1
    return wb


class _Sheets:
    """The sheets of one workbook, sharing one cell budget and one scan count."""

    def __init__(self, pkg, wb, ctx):
        self.pkg, self.wb, self.ctx = pkg, wb, ctx
        self.budget = sheet_grid.Budget()
        self.draw = sheet_drawing.Drawings(pkg, ctx, {n for n, _, s in wb.sheets if s not in _HIDDEN})
        self.scan = 0                    # elements the sheet parts have closed, all sheets together
        self.used: set[str] = set()      # sheet parts read
        self.notes: dict = {}            # note key -> blocks, for Doc.notes
        self.comment_count = 0           # comments turned into notes, all sheets together
        self.comments_cut = False
        self.persons = None              # person id -> display name, read when a threaded part first needs it
        self._code: dict[int, str] = {}

    def sections(self) -> list:
        out = []
        for no, (name, part, state) in enumerate(self.wb.sheets, 1):
            blocks = None  # stays None when the guard swallows an error, so the check below is real
            try:
                with self.ctx.guard("sheet"):
                    blocks = self.sheet(name, part, no, state in _HIDDEN)
            except ReadError as e:
                _skip(self.ctx, "sheet", e)
                continue
            if blocks is None:  # the guard counted a failure
                continue
            if not blocks and part in self.wb.chartsheets:  # a chartsheet with nothing drawn has no section
                continue
            if state in _HIDDEN:
                out.append(Section("sheet", name, blocks=_hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", name, blocks=blocks))
        return out

    def code(self, style: int) -> str:
        """The format code of a cell style index; an index past the `cellXfs` list is General."""
        code = self._code.get(style)
        if code is None:
            xf = self.wb.xf_fmt
            code = self._code[style] = code_for(xf[style], self.wb.formats) if 0 <= style < len(xf) else "General"
        return code

    def show(self, value, style: int) -> str:
        """Text under General is shown as is, as `xlsb._Sheets.show` does; anything else goes through `display`."""
        code = self.code(style)
        if isinstance(value, str) and code == "General":
            return value
        return display(value, code, self.wb.date1904)

    def sheet(self, name: str, part: str, no: int = 0, hidden: bool = False) -> list:
        keys = []  # the note keys this sheet made
        try:
            return self._sheet(name, part, no, hidden, keys)
        except BaseException:  # a sheet that is lost takes its notes with it
            for k in keys:
                self.notes.pop(k, None)
            raise

    def _sheet(self, name, part, no, hidden, keys) -> list:
        if not part or not self.pkg.has(part):
            raise ReadError("damaged", "sheet part missing")
        if part in self.used:
            raise ReadError("damaged", "sheet part used twice")
        self.used.add(part)
        relmap = []  # the sheet's relationships, read once: [dict]
        if part in self.wb.chartsheets:  # a chartsheet has no cells: its drawing is all there is
            return self._drawn(part, relmap)
        grid = sheet_grid.Grid(name, self.budget, self.ctx)
        cap = limits.SHEET_SCAN
        if self.scan >= cap:
            # its first element could not close inside the cap: the sheet is cut as the stream would cut it, but its
            # part and comments are not opened, so their bytes are not charged to ZIP_TOTAL ahead of its drawings
            grid.stop("scanned XML elements", cap)
            if any(not r.external and r.type.endswith(("/comments", "/threadedComment"))
                   for r in self._sheet_rels(part, relmap).values()):
                self._cut_comments()  # no mark could be placed on the cut grid
            return self._drawn(part, relmap)
        row, col, cells, rows_seen = -1, -1, 0, False  # cells: the `c` elements open
        links = []  # (r1, r2, c1, c2, url) of the external hyperlinks, applied after the stream
        stack, done, names = [], [], {}  # the open path, and the ended children each open element still holds
        marks = self._comments(part, no, relmap, keys)  # (row, col) -> (note key, characters)
        try:
            with self.pkg.open(part) as f:
                for ev, el in iterparse(f, ("start", "end")):
                    tag = names.get(el.tag)
                    if tag is None:
                        tag = names[el.tag] = local(el.tag)
                    if ev == "start":
                        if self.scan >= cap:  # this element cannot close inside the cap
                            grid.stop("scanned XML elements", cap)
                            break
                        stack.append(el)
                        done.append(0)
                        if tag == "c":
                            cells += 1
                        elif tag == "row":
                            r = _int(el.get("r"))
                            row = r - 1 if r is not None and r >= 1 else row + 1
                            col, rows_seen = -1, True
                            if row >= limits.SHEET_ROWS:
                                # no row from here on can be put: end the sheet now, as `put` would at its first
                                # value, so styled empties down to the last row leave the scan count to later sheets
                                grid.stop("rows", limits.SHEET_ROWS)
                                break
                            if (el.get("hidden") or "").strip().lower() in ("1", "true"):
                                grid.hide_row(row)
                        continue
                    stack.pop()
                    kids = done.pop()
                    self.scan += 1
                    if self.scan > cap:  # an element past the cap: exactly SHEET_SCAN closes are not a cut
                        grid.stop("scanned XML elements", cap)
                        break
                    if tag == "c":
                        cells -= 1
                        c = _col(el.get("r"))
                        col = c if c is not None else col + 1
                        value = self._cell(el)
                        if value and not grid.put(max(row, 0), col, value):
                            break  # the grid is cut: nothing more can be put
                    elif tag in _SHEET_TAGS and not cells and stack and names.get(stack[-1].tag) == _PARENT[tag]:
                        if tag == "hyperlink":
                            self._collect(el, part, links, grid, relmap)
                        elif tag == "mergeCell":
                            rng = _range(el.get("ref"))
                            if rng is not None:
                                grid.merge(*rng)
                        elif (el.get("hidden") or "").strip().lower() in ("1", "true"):
                            lo, hi = _int(el.get("min")), _int(el.get("max"))
                            if lo is not None and hi is not None and lo >= 1:
                                grid.hide_cols(lo - 1, hi - 1)
                    if not cells:  # outside a cell an ended element is let go, as `_drop` does (inlined for speed)
                        if kids or tag == "c":  # a cell's subtree goes once the cell is read
                            del el[:]
                        if stack:
                            done[-1] += 1
                            if done[-1] >= _BATCH:
                                del stack[-1][:done[-1]]
                                done[-1] = 0
        except ReadError as e:
            if e.kind != "damaged" or not rows_seen:
                raise
            grid.stop("XML error", 1)  # decision 12: the rows read are kept
        self._place(grid, marks, hidden)
        self._link(grid, links)
        blocks = grid.blocks()
        self._sweep(blocks, keys)
        return blocks + self._drawn(part, relmap)

    def _drawn(self, part, relmap) -> list:
        """The sheet's charts and pictures in anchor order, from the relationships the sheet read once. A drawing
        ReadError other than a timeout costs only the drawings, counted under `object`."""
        try:
            return self.draw.blocks(part, self._sheet_rels(part, relmap))
        except ReadError as e:
            _skip(self.ctx, "object", e)
            return []

    def _collect(self, el, part, links, grid, relmap):
        """Keep the range and URL of a `hyperlink` that names an external hyperlink relationship; an `r:id` that
        names none counts `link`. Past _LINKS the rest are cut, counted once."""
        rid = attr(el, "id")
        if not rid:  # a `location` alone points inside the workbook
            return
        if len(links) >= _LINKS:
            if len(links) == _LINKS:
                links.append(None)  # the cut is counted once
                self.ctx.warnings.append(f"sheet {grid.name} cut at {_LINKS} hyperlinks")
                self.ctx.skipped["links_cut"] += 1
            return
        rng = _range(el.get("ref"))
        if rng is None:
            return
        rel = self._sheet_rels(part, relmap).get(rid)
        if rel is None or not rel.external or not rel.type.endswith("/hyperlink") or not rel.target:
            self.ctx.skipped["link"] += 1
            return
        links.append((*rng, rel.target[:MAX_URL]))

    def _sheet_rels(self, part, relmap) -> dict:
        """The sheet's relationships, read once whether that succeeds or fails."""
        if not relmap:
            try:
                relmap.append(ooxml_base.rels(self.pkg, part, self.ctx))
            except ReadError as e:
                _skip(self.ctx, "rels", e)
                relmap.append({})
        return relmap[0]

    # ---- comments
    def _cut_comments(self):
        """Comments were left out: counted once a workbook, and no more are read."""
        if not self.comments_cut:
            self.comments_cut = True
            self.ctx.skipped["comments_cut"] += 1

    def _ended(self, target):
        """Stream a part: yield (name, element, names of the open ancestors) for each element as it ends, then let
        it go. Stops past _COMMENT_ELEMENTS closed elements, counted as `comments_cut`."""
        seen, els, tags, done, names = 0, [], [], [], {}
        with self.pkg.open(target) as f:
            for ev, el in iterparse(f, ("start", "end")):
                name = names.get(el.tag)
                if name is None:
                    name = names[el.tag] = local(el.tag)
                if ev == "start":
                    els.append(el)
                    tags.append(name)
                    done.append(0)
                    continue
                els.pop()
                tags.pop()
                seen += 1
                if seen > _COMMENT_ELEMENTS:
                    self._cut_comments()
                    return
                yield name, el, tags
                _drop(el, els, done)

    def _people(self) -> dict:
        """Person id -> display name from the workbook's persons part, streamed once; one missing or damaged counts
        `comments`."""
        if self.persons is None:
            self.persons = {}
            try:
                with self.ctx.guard("comments"):
                    for rel in ooxml_base.rels(self.pkg, self.wb.part, self.ctx).values():
                        if rel.external or not rel.type.endswith("/person"):
                            continue
                        if not self.pkg.has(rel.target):
                            self.ctx.skipped["comments"] += 1
                            continue
                        for name, el, tags in self._ended(rel.target):
                            pid = attr(el, "id")
                            if name == "person" and len(tags) == 1 and pid is not None:
                                self.persons[pid] = attr(el, "displayName") or ""
            except ReadError as e:
                _skip(self.ctx, "comments", e)
        return self.persons

    def _comments(self, part, no, relmap, keys) -> dict:
        """Read the sheet's threaded and legacy comments into `self.notes`; the cells (row, col) they sit on map to
        (note key `xc<sheet>-<row>-<col>`, characters). Threads come first and cover their cells; a legacy comment on
        a covered cell is dropped. The sheet's relationships are read once, into `relmap`."""
        rels = self._sheet_rels(part, relmap)
        per_cell: dict = {}  # (row, col) -> the note's blocks
        sizes: dict = {}     # (row, col) -> the characters of its comments
        covered: set = set()
        for suffix, read in (("/threadedComment", self._threaded), ("/comments", self._legacy)):
            for rel in rels.values():
                if rel.external or not rel.type.endswith(suffix):
                    continue
                try:
                    with self.ctx.guard("comments"):
                        if not self.pkg.has(rel.target):
                            self.ctx.skipped["comments"] += 1
                            continue
                        read(rel.target, per_cell, sizes, covered)
                except ReadError as e:
                    _skip(self.ctx, "comments", e)
            if suffix == "/threadedComment":
                covered = set(per_cell)  # the cells the threads cover
        marks = {}
        for pos, blocks in per_cell.items():
            key = f"xc{no}-{pos[0]}-{pos[1]}"
            self.notes[key] = blocks
            keys.append(key)
            marks[pos] = (key, sizes[pos])
        return marks

    def _take(self, n=0) -> bool:
        """Count a comment against MAX_COMMENTS (the cut counted once); `n` characters are only checked here."""
        if self.comments_cut:
            return False
        if self.comment_count >= MAX_COMMENTS or self.budget.chars < n:
            self._cut_comments()
            return False
        self.comment_count += 1
        return True

    def _cell_of(self, ref):
        """(row, col) of a comment's `ref`; a bad or out-of-grid one counts `comments` and gives None."""
        rng = _range(ref)
        if rng is None or rng[0] >= limits.SHEET_ROWS or rng[2] >= limits.SHEET_COLS:
            self.ctx.skipped["comments"] += 1
            return None
        return rng[0], rng[2]

    def _legacy(self, target, per_cell, sizes, covered):
        authors, parts, size = [], [], 0
        for name, el, tags in self._ended(target):
            if name == "author" and tags[-1:] == ["authors"]:
                authors.append(_unescape(el.text or ""))
            elif name == "t" and (tags[-2:] == ["comment", "text"] or tags[-3:] == ["comment", "text", "r"]):
                parts.append(el.text or "")
                size += len(el.text or "")
                if size > _RAW_PER_CHAR * max(self.budget.chars, 0):
                    self._cut_comments()
                    return
            elif name == "comment" and tags[-1:] == ["commentList"]:
                text = re.sub(r"\s+", " ", _unescape("".join(parts))).strip()
                parts, size = [], 0
                if not text:
                    continue
                pos = self._cell_of(el.get("ref"))
                if pos is None or pos in covered:
                    continue
                i = _int(el.get("authorId"))
                author = authors[i] if i is not None and 0 <= i < len(authors) else ""
                if not self._take(len(text)):
                    return
                per_cell.setdefault(pos, []).extend(comment_note([Para([Text(text)])], author))
                sizes[pos] = sizes.get(pos, 0) + len(text)

    def _threaded(self, target, per_cell, sizes, covered):
        """Each thread is its root then its replies in file order (a reply of a reply follows its parent); a reply
        whose parent is unknown starts a thread. A visited set lists each comment once, so a parent cycle ends; the
        comments no walk reaches follow in file order. At most MAX_COMMENTS are kept while streaming."""
        items, text = [], ""  # (id, parentId, cell, person, text)
        for name, el, tags in self._ended(target):
            if name == "text" and tags[-1:] == ["threadedComment"]:
                text = re.sub(r"\s+", " ", el.text or "").strip()
            elif name == "threadedComment" and len(tags) == 1:
                t, text = text, ""
                if not t:
                    continue
                pos = self._cell_of(el.get("ref"))
                if pos is None:
                    continue
                if not self._take(len(t)):
                    break
                items.append((el.get("id"), el.get("parentId"), pos, el.get("personId"), t))
        index = {}
        for n, it in enumerate(items):
            if it[0] and it[0] not in index:
                index[it[0]] = n
        kids = [[] for _ in items]
        roots = []
        for n, it in enumerate(items):
            up = index.get(it[1]) if it[1] else None
            (kids[up] if up is not None and up != n else roots).append(n)
        seen = set()
        for start in roots + list(range(len(items))):  # then the ones a cycle hid
            stack = [start]
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                pos, who, t = items[n][2], items[n][3], items[n][4]
                per_cell.setdefault(pos, []).extend(comment_note([Para([Text(t)])], self._people().get(who or "", "")))
                sizes[pos] = sizes.get(pos, 0) + len(t)
                stack.extend(reversed(kids[n]))

    def _place(self, grid, marks, hidden):
        """Put the marks after the cells. A mark on a cell that holds a value is appended to it (`Grid.append`, which
        still works once the sheet is cut); a cell that holds nothing gets the mark alone, which needs an uncut grid.
        A note is kept only if its mark is placed and its text fits the character budget; otherwise it is dropped
        and `comments_cut` counted. A comment on a merged-away cell goes to the origin of the merge that renders
        (the origin's own comment first), as `Grid.covers` resolves it. A note on a hidden row or column (its own
        or its mark's), or on a hidden sheet, is hidden with its place."""
        if not marks:
            return
        moved = grid.covers(marks)
        targets: dict = {}
        for pos, (key, size) in marks.items():
            t = moved.get(pos, pos)
            targets.setdefault(t, [])
            targets[t].insert(0 if t == pos else len(targets[t]), (key, size, pos))
        order = [t for t in targets if grid.get(*t) is None] + [t for t in targets if grid.get(*t) is not None]
        b = grid.budget

        def hides(pos):
            return pos[0] in grid.rows_h or pos[1] in grid.cols_h

        for t in order:
            fit, total = [], 0
            for key, size, pos in targets[t]:
                if b.chars - total >= size:
                    fit.append((key, pos))
                    total += size
                else:
                    self.notes.pop(key, None)
                    self._cut_comments()
            if not fit:
                continue
            refs = [NoteRef(k) for k, _ in fit]
            if grid.get(*t) is not None:
                ok = grid.append(t[0], t[1], refs)
            else:
                ok = grid.put(t[0], t[1], refs)
            if ok:
                b.chars -= total
                for k, pos in fit:
                    if hidden or hides(t) or hides(pos):
                        self.notes[k] = _hide(self.notes[k])
            else:
                for k, _ in fit:
                    self.notes.pop(k, None)
                self._cut_comments()

    def _sweep(self, blocks, keys):
        """Drop this sheet's notes whose mark did not reach the output; that is counted as `comments_cut`."""
        if not keys:
            return
        live, stack = set(), list(blocks)
        while stack:
            x = stack.pop()
            if isinstance(x, NoteRef):
                live.add(x.id)
                continue
            for name in ("inlines", "blocks"):
                stack.extend(getattr(x, name, ()))
            for row in getattr(x, "rows", ()):
                stack.extend(row)
        for k in keys:
            if k not in live and self.notes.pop(k, None) is not None:
                self._cut_comments()

    def _link(self, grid, links):
        """Decision 9: wrap the valued cells of each range in a Link. The cost min(area, cells held) is charged to
        the scan count before the range is walked; a link that no longer fits is counted under `link`."""
        cap = limits.SHEET_SCAN
        for item in links:
            if item is None or grid.cut:
                continue
            r1, r2, c1, c2, url = item
            if self.scan + min((r2 - r1 + 1) * (c2 - c1 + 1), len(grid.cells)) > cap:
                self.ctx.skipped["link"] += 1
                continue
            found, cost = grid.held(r1, r2, c1, c2)
            self.scan += cost
            for r, c in found:
                value = grid.get(r, c)
                inl = sheet_grid.text_inlines(value) if isinstance(value, str) else list(value)
                body = [x for x in inl if not isinstance(x, NoteRef)]
                if not body or any(isinstance(x, Link) for x in body):  # nothing to link, or already linked
                    continue
                if not grid.put(r, c, [Link(url, body)] + [x for x in inl if isinstance(x, NoteRef)]):
                    break  # the budget is gone; the cell keeps its old value

    def _cell(self, el) -> str:
        """The displayed value of a `c` element by its type: the cached value first, then `=` and the formula."""
        t = el.get("t") or "n"
        v = f = inline = None
        for ch in el:
            n = local(ch.tag)
            if n == "v":
                v = ch
            elif n == "f":
                f = ch
            elif n == "is":
                inline = ch
        style = _int(el.get("s"), 0)
        if t == "inlineStr" and inline is not None:
            return self.show(_rich(inline), style)
        if v is None:
            return "=" + _unescape(f.text) if f is not None and f.text else ""
        text = v.text or ""
        if t == "s":
            i = _int(text.strip())
            return self.show(self.wb.sst[i], style) if i is not None and 0 <= i < len(self.wb.sst) else ""
        if t in ("str", "inlineStr"):
            return self.show(_unescape(text), style)
        if t == "e":
            return _unescape(text)
        if t == "b":
            return self._bool(text, style)
        if t == "d":
            return self._date(text, style)
        try:
            number = float(text)
        except ValueError:
            return text
        return self.show(number, style) if math.isfinite(number) else text

    def _bool(self, text: str, style: int) -> str:
        """TRUE for "true" in any case or any non-zero number, FALSE for "false" or zero; anything else as stored."""
        word = text.strip().lower()
        if word in ("true", "false"):
            return self.show(word == "true", style)
        try:
            number = float(word)
        except ValueError:
            return text
        return self.show(number != 0, style) if math.isfinite(number) else text

    def _date(self, text: str, style: int) -> str:
        """Decision 8: under General the stored ISO text; under another code its serial in the workbook's date
        system through that code. A text `fromisoformat` rejects is shown as stored."""
        if self.code(style) == "General":
            return text
        try:
            when = datetime.fromisoformat(text.strip()).replace(tzinfo=None)
        except ValueError:
            return text
        delta = when - (datetime(1904, 1, 1) if self.wb.date1904 else datetime(1899, 12, 30))
        return self.show(delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400, style)


def parse_sheets(pkg, part, ctx) -> Doc:
    wb = read_workbook(pkg, part, ctx)
    sheets = _Sheets(pkg, wb, ctx)
    secs = sheets.sections() if wb.sheets else []
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=secs, notes=sheets.notes)
