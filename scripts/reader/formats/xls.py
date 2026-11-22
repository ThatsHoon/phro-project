"""Excel 97-2003 workbooks (.xls): one sheet section per worksheet, with displayed values, merges and hidden parts.

The container check, the BOUNDSHEET-ordered sheet walk, the ROW and COLINFO hidden flags and the cached formula values
(a string result comes from the STRING record after FORMULA, past any SHRFMLA, ARRAY or TABLE) were checked against
dochan `office_binary/xls.py` and anydoc `sheet/xls.rs`. Cells go through `numfmt.display`. Each FORMULA's tokens
(a shared or array one's from its SHRFMLA or ARRAY) are read by `ptg` into the sheet's formulas paragraph.
Every count from the file is bounded by the record it sits in: MULRK uses its own length, nested BOF and EOF pairs are
skipped by depth, and BOUNDSHEET offsets are checked against the stream. A FILEPASS workbook is decrypted by `xls_crypt`
when its default password verifies, else it is `encrypted`. A compound file with no Workbook stream goes to
`cfb_route`: another format's file named .xls is read by its own parser. A sheet that cannot be read is counted under
`sheet` and the others still come out. Charts (a chart sheet, or a substream nested in a worksheet) are read by
`biff_chart` after every worksheet, so a reference to a later sheet resolves; each table goes back into its sheet's
section after its pictures, charged against `CHART_TOTAL` like the other formats' charts. A sheet's pictures
(`xls_drawing`) follow its table, charged against `IMAGE_BYTES` and the workbook's placement cap. Between them come its
text boxes: a TXO's text (cchText u16 at 10) is in the CONTINUEs after it, each starting with a flag byte (0 8-bit,
1 UTF-16), but the last when there are two or more, which holds the formatting runs. It is charged against the sheet
characters like a cell, and `xls_drawing.shapes` makes it a diagram node, a connector label or a paragraph; the sheet
is one diagram scope, the workbook one connection budget. An embedded workbook reads no drawing and no TXO, and its
sheets are record tables (`Grid.blocks`): the object shows a range, not a sheet.
A TXO after an OBJ whose FtCmo says comment (ot 0x19) is a note's text, not a text box: the NOTE that names that
object id puts it on its cell with its author, as the xlsx notes are put (`Grid.place_notes`). Each HLINK
([MS-XLS] 2.4.140, the link object [MS-OSHARED] 2.3.7.1: a URL or file moniker, or one saved as a string, and a
location) lands on its cells through `Grid.link`, charged to one SHEET_SCAN count a workbook. The workbook's names,
its external SUPBOOK paths by number and its protection come before its sheets; a sheet's own names, its filter (an
AUTOFILTERINFO: its `_FilterDatabase` range and the AUTOFILTER criteria), its validations (DV), its conditional rules
(CONDFMT and CF, the formulas at the record's end) and its PROTECT follow its formulas (`Grid.fact`). A
`_VBA_PROJECT_CUR` storage is a VBA project: its modules are read by containers.vba.
"""
import re
from dataclasses import replace
import struct

from .. import diagram, limits
from ..containers.cfb import CfbError, Compound
from ..containers import flate, vba
from ..containers.zipx import Package
from ..limits import ReadError
from ..model import Doc, Para, Section, Text
from . import biff, biff_chart, cfb_route, ptg, sheet_grid, xls_alike, xls_crypt, xls_drawing, zip_route
from .word_fields import MAX_URL, comment_note

_ROW, _COLINFO, _MERGED, _DIMENSIONS = 0x0208, 0x007D, 0x00E5, 0x0200
_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD, 0x0205
_FORMULA, _STRING = 0x0006, 0x0207
_MSODRAWING = 0x00EC  # a sheet's drawing records, joined and read by xls_drawing
_TXO = 0x01B6        # a text box's text, in the CONTINUEs after it
_OBJ, _NOTE, _HLINK, _DV, _CONDFMT, _CF = 0x005D, 0x001C, 0x01B8, 0x01BE, 0x01B0, 0x01B1
_AUTOFILTERINFO, _AUTOFILTER = 0x009D, 0x009E
_SXVIEW, _SXDI = 0x00B0, 0x00C5
_UNREAD = frozenset({0x0872, 0x0878, 0x087A})
_COMMENT = 0x19
_OBJ5 = {5: None, 8: "image"}
_URL_MONIKER = bytes.fromhex("e0c9ea79f9bace118c8200aa004ba90b")   # {79EAC9E0-BAF9-11CE-8C82-00AA004BA90B}
_FILE_MONIKER = bytes.fromhex("0303000000000000c000000000000046")  # {00000303-0000-0000-C000-000000000046}
_AFTER_FORMULA = frozenset({0x04BC, 0x0221, 0x0236})  # SHRFMLA, ARRAY, TABLE: may sit between FORMULA and STRING
_ANCHORED = {0x04BC: 8, 0x0221: 12}  # SHRFMLA and ARRAY: the offset of cce in the body
_CELLS = frozenset({_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR, _FORMULA})
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A"}
_WORKSHEET, _MACRO, _CHART, _VBA = 0, 1, 2, 6
MAX_CHARTS = 256  # chart substreams one workbook reads


class Workbook:
    """One BIFF8 workbook stream. `g` holds its globals; `cells[i]` the displayed values of sheet i (BOUNDSHEET order).

    A BIFF5 one (`g.ansi` set) reads its cells, its cached formula results and its NOTEs (rw, col, the text's length,
    then its bytes; a NOTE with row 0xFFFF continues the one before it); its formula tokens are BIFF5's and are
    counted under formula, its OBJs under shape (image for a picture), its charts under chart, and a FILEPASS is
    encrypted."""

    def __init__(self, data: bytes, ctx, budget: sheet_grid.Budget, pictures: bool = True):
        self.data, self.ctx, self.budget, self.pictures = data, ctx, budget, pictures
        self.st = biff.Stream(data, ctx)
        self.g = biff.read_globals(self.st)
        if self.g.filepass is not None:
            # only the default password is tried; the clear stream keeps its FILEPASS record, so its id is blanked
            clear = None if self.g.ansi else xls_crypt.decrypt(data, self.g.filepass)
            if clear is None:
                raise ReadError("encrypted")
            patched = bytearray(clear)
            patched[self.g.filepass.off - 4:self.g.filepass.off - 2] = b"\0\0"
            self.data = data = bytes(patched)
            self.st = biff.Stream(data, ctx)
            self.g = biff.read_globals(self.st)
            if self.g.filepass is not None:
                raise ReadError("encrypted")
        self.cells: dict[int, dict] = {}
        self._blocks: dict[int, list] = {}
        self._fmt: dict[int, str] = {}
        self.flate = flate.Budget()  # one inflate budget and one image byte count for the whole document's pictures
        self.spent = [0, 0]  # image bytes placed, picture placements read
        self.blips: dict[int, bytes | None] = {}  # BStore entries read (pib -> image), shared by every sheet
        self._starts: dict[int, list] = {}  # sheet index -> offsets of the chart substreams nested in it
        self.connections = diagram.ConnectionBudget(ctx)  # the workbook's edges, every sheet's diagram together
        self.notes: dict = {}
        self.comment_count = 0
        self.comments_cut = False
        self.link_scan = 0

    # ---- sheets
    def sections(self) -> list:
        seen, shown = set(), []
        for i, ref in enumerate(self.g.sheets):
            if ref.kind in (_MACRO, _VBA):
                continue
            blocks = []
            if ref.kind != _CHART:
                blocks = None  # stays None when the guard swallows an error, so the check below is real
                try:
                    with self.ctx.guard("sheet"):
                        if ref.offset in seen:
                            raise ReadError("damaged", "sheet offset used twice")
                        seen.add(ref.offset)
                        blocks = self.sheet_blocks(i)
                except ReadError as e:
                    if e.kind != "damaged":
                        raise
                    self.ctx.skipped["sheet"] += 1
                    continue
                if blocks is None:  # guard counted a failure
                    continue
            shown.append((i, ref, blocks))
        tables = self._charts(shown)
        out = []
        for i, ref, blocks in shown:
            blocks = blocks + tables.get(i, [])
            if ref.kind == _CHART and not blocks:
                continue
            if ref.state:
                out.append(Section("sheet", ref.name, blocks=sheet_grid.hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", ref.name, blocks=blocks))
        return out

    def book_facts(self) -> list:
        """The workbook's own facts, before its sheets: its names, its external links by number, its protection."""
        env = self._env()
        names = [e for e in (ptg.named(n, env, self.ctx) for n in self.g.names if n.itab == 0) if e]
        links = [(f"[{k}] {p}", False) for k, p in enumerate(self.g.books, 1) if p]
        b, ctx = self.budget, self.ctx
        return (sheet_grid.listed("Names", names, b, ctx, cap=limits.FACT_ITEMS) + sheet_grid.listed("External links", links, b, ctx, cap=limits.FACT_ITEMS)
                + sheet_grid.listed("Protected", [(p, False) for p in self.g.protect], b, ctx))

    def _charts(self, shown: list) -> dict:
        """Every chart of the shown sheets, in sheet then stream order: {sheet index: [Table]}. Each placement is
        charged against CHART_TOTAL, and one past it, or past MAX_CHARTS, is counted under chart, as is every chart
        of a BIFF5 workbook."""
        out, seen = {}, set()
        charts = chart_cells = 0  # per call, so a second sections() counts what the first did
        for i, ref, _ in shown:
            for start in [ref.offset] if ref.kind == _CHART else self._starts.get(i, ()):
                if start in seen or charts >= MAX_CHARTS or self.g.ansi:
                    self.ctx.skipped["chart"] += 1
                    continue
                seen.add(start)
                charts += 1
                with self.ctx.guard("chart"):
                    tbl, _ = biff_chart.chart(self.data, start, self.g, self.cells, self.ctx, self.st, workbook=True)
                    if tbl is None:
                        continue
                    chart_cells += sum(len(r) for r in tbl.rows)
                    if chart_cells > limits.CHART_TOTAL:
                        self.ctx.skipped["chart"] += 1
                    else:
                        out.setdefault(i, []).append(tbl)
        return out

    def sheet_blocks(self, i: int) -> list:
        if i in self._blocks:
            return self._blocks[i]
        ref = self.g.sheets[i]
        if ref.kind != _WORKSHEET:
            return []
        grid = sheet_grid.Grid(ref.name, self.budget, self.ctx)
        self.cells[i] = grid.cells
        sh = _Sheet(self._env())
        try:
            self._walk(ref.offset, grid, sh)
        except Exception:
            del self.cells[i]  # a sheet that fails leaves no half-read cells for a chart to use
            raise
        self._starts[i] = sh.starts
        ptg.put(grid, *sh.forms, sh.env, self.ctx)
        keys = []
        try:
            blocks = self._sheet_tail(i, ref, grid, sh, keys)
        except BaseException:
            for k in keys:
                self.notes.pop(k, None)
            raise
        self._blocks[i] = blocks
        return blocks

    def _sheet_tail(self, i, ref, grid, sh, keys) -> list:
        """The walked sheet's facts, links and notes on `grid`, then its blocks, drawing and pictures."""
        self._facts(i, grid, sh)
        for r1, r2, c1, c2, url, place in sh.links:
            if grid.cut:
                break
            if self.link_scan + min((r2 - r1 + 1) * (c2 - c1 + 1), len(grid.cells)) > limits.SHEET_SCAN:
                self.ctx.skipped["link"] += 1
                continue
            self.link_scan += grid.link(r1, r2, c1, c2, url, place)
        if self.pictures and grid.place_notes(self._marks(i, grid, sh, keys), self.notes, bool(ref.state)):
            self._cut_notes()
        blocks = grid.sheet_blocks() if self.pictures else grid.blocks()
        if (sh.draw or sh.texts) and self.pictures:
            blocks = self._drawn(blocks, b"".join(sh.draw), sh.texts)
        if sh.draw and self.pictures:
            blocks = blocks + xls_drawing.pictures(self.g, b"".join(sh.draw), self.flate, self.spent, self.ctx,
                                                   self.blips)
        if sheet_grid.sweep(blocks, keys, self.notes):
            self._cut_notes()
        return blocks

    def _facts(self, i, grid, sh):
        """The sheet's facts in their order: its names, its filter, its validations, its rules, its pivot tables
        (SXVIEW: the range, 16 fields, the lengths of its name and its data name, the name; an SXDI's name is a
        value it shows), its protection. A table (FEAT11, FEAT12) and a CF12 rule are counted under fact."""
        grid.fact("Names", [e for e in (ptg.named(n, sh.env, self.ctx) for n in self.g.names if n.itab == i + 1)
                            if e])
        rng = self._filter_range(i) if sh.filtered else None
        if rng is not None:
            crit = [(c, False) for c in (_criterion(b, rng[2]) for b in sh.crit) if c]
            where = sheet_grid.span(*rng)
            if crit:
                grid.fact(f"Filter {where}", crit)
            else:
                grid.fact("Filter", [(where, False)])
        grid.fact("Validation", sh.valid)
        grid.fact("Conditional", sheet_grid.ruled(sh.rules))
        for name, where, values in sh.pivots:
            if values:
                grid.fact(" ".join(filter(None, ("Pivot", name, where))), [("values " + ", ".join(values), False)])
            else:
                grid.fact("Pivot", [(f"{name} {where}".strip(), False)])
        if sh.protect:
            grid.fact("Protected", [("sheet", False)])

    def _filter_range(self, i):
        """(r1, r2, c1, c2) of sheet i's `_FilterDatabase` name (an Area or Area3d token), or None."""
        for n in self.g.names:
            body = n.rgce
            if n.itab == i + 1 and n.name == "_FilterDatabase" and len(body) >= 9:
                at = 3 if body[0] in (0x3B, 0x5B, 0x7B) else 1 if body[0] in (0x25, 0x45, 0x65) else None
                if at is not None and len(body) >= at + 8:
                    r1, r2, c1, c2 = struct.unpack_from("<4H", body, at)
                    return r1, r2, c1 & 0x3FFF, c2 & 0x3FFF
        return None

    def _cut_notes(self):
        if not self.comments_cut:
            self.comments_cut = True
            self.ctx.skipped["comments_cut"] += 1

    def _marks(self, i, grid, sh, keys) -> dict:
        """The sheet's NOTEs as notes `xc<sheet>-<row>-<col>` ("Comment by <author>:", the author's own lead cut
        from the text) and their marks (row, col) -> (key, characters). The comment texts were charged as TXOs; that
        charge is given back, as `place_notes` charges the notes it keeps (a BIFF5 NOTE's text was not charged). At
        most SHEET_COMMENTS a workbook."""
        if not self.g.ansi:
            grid.budget.chars += sum(len(t) for t in sh.comments.values())
        marks = {}
        for r, c, oid, author in sh.notes:
            text = re.sub(r"\s+", " ", sh.comments.get(oid, "")).strip()
            if author and text.startswith(author + ":"):
                text = text[len(author) + 1:].lstrip()
            if not text or r >= limits.SHEET_ROWS or c >= limits.SHEET_COLS:
                continue
            if self.comment_count >= limits.SHEET_COMMENTS:
                self._cut_notes()
                break
            self.comment_count += 1
            key = f"xc{i + 1}-{r}-{c}"
            blocks = comment_note([Para([Text(text)])], author)
            if key in self.notes:
                self.notes[key].extend(blocks)
                marks[(r, c)] = (key, marks[(r, c)][1] + len(text))
            else:
                self.notes[key] = blocks
                keys.append(key)
                marks[(r, c)] = (key, len(text))
        return marks

    def _drawn(self, table: list, drawing: bytes, texts: list) -> list:
        """The sheet's `table` blocks, then its shapes and TXO `texts` as one diagram scope (`xls_drawing.shapes`).
        The shapes are read and place() runs under guard("shape"); a failure there falls back to the texts as
        paragraphs in TXO order, or to the slots' blocks."""
        dia = diagram.Diagram(self.ctx, self.connections)
        slots = None
        with self.ctx.guard("shape"):
            slots = xls_drawing.shapes(drawing, texts, dia, self.ctx)
        if slots is None:
            return table + [b for t in texts for b in xls_drawing.paragraph(t)]
        slots = [(None, table)] + slots
        out = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            out = dia.place(slots)
        return out

    def _code(self, ixfe: int) -> str:
        code = self._fmt.get(ixfe)
        if code is None:
            code = self._fmt[ixfe] = biff.fmt_code(self.g, ixfe)
        return code

    def _show(self, value, ixfe: int) -> str:
        code = self._code(ixfe)
        if isinstance(value, str) and code == "General":
            return value
        return sheet_grid.shown(value, code, self.g.date1904)

    def _walk(self, offset: int, grid: sheet_grid.Grid, sh) -> None:
        """Walk sheet substream `offset` into `grid` and `sh`; the sheet's MSODRAWING bodies, CONTINUEs joined, go
        into `sh.draw` (a TXO or OBJ is another record, and a TXO's CONTINUEs belong to it); each TXO's text goes
        into `sh.texts` (`_txo`; none for an embedded workbook), a comment box's as "" with its text kept in
        `sh.comments` under its object id; the offset of each chart substream nested in it goes into `sh.starts`.
        Each FORMULA's (row, col, tokens, extra) goes into forms[0], and the tokens of a SHRFMLA or ARRAY into
        forms[1] under the cell of the FORMULA before it, which is the cell a PtgExp names."""
        data, ansi = self.data, self.g.ansi
        if not 0 <= offset < len(data):
            raise ReadError("damaged", "sheet offset outside the stream")
        draw, starts, texts, forms = sh.draw, sh.starts, sh.texts, sh.forms
        depth, pending, last, obj, cond = 0, None, None, None, None
        for rec in self.st.records(offset):
            rid, off, size = rec.id, rec.off, rec.size
            if rid == biff.BOF:
                depth += 1
                if depth > limits.MAX_DEPTH:
                    raise ReadError("damaged", "sheet nested too deep")
                if depth == 1 and (size < 4 or struct.unpack_from("<H", data, off + 2)[0] != 0x0010):
                    raise ReadError("damaged", "not a worksheet")
                if depth == 2 and size >= 4 and struct.unpack_from("<H", data, off + 2)[0] == biff.CHART_BOF:
                    # one start per BOF record, and every walk of the stream draws on its one BIN_RECORDS, so
                    # `starts` never outgrows that; _charts reads MAX_CHARTS of them and counts the rest, every call
                    starts.append(off - 4)
                continue
            if depth == 0:
                raise ReadError("damaged", "sheet does not start with a BOF")
            if rid == biff.EOF:
                depth -= 1
                if depth == 0:
                    return
                continue
            if depth > 1:
                continue
            if pending is not None and rid != _STRING and rid not in _AFTER_FORMULA:
                pending = None
            if rid in _CELLS:
                if grid.cut or size < 6:
                    continue
                try:
                    pending = self._cell(rec, grid)
                except (ValueError, struct.error):
                    self.ctx.skipped["record"] += 1
                if rid == _FORMULA and ansi:
                    self.ctx.skipped["formula"] += 1
                elif rid == _FORMULA:
                    last = self._formula(rec, forms[0])
            elif rid in _ANCHORED:
                body = _body(data, rec)
                at = _ANCHORED[rid]
                if last is not None and len(body) >= at + 2:
                    cce = struct.unpack_from("<H", body, at)[0]
                    forms[1].setdefault(last, (body[at + 2:at + 2 + cce], body[at + 2 + cce:]))
                last = None
            elif rid == _STRING:
                if pending is not None and not grid.cut:
                    r, c, ixfe = pending
                    pending = None
                    try:
                        text = biff.Cursor(data, [(off, size)] + list(rec.conts)).string(ansi)
                    except ValueError:
                        self.ctx.skipped["record"] += 1
                        continue
                    grid.put(r, c, self._show(text, ixfe))
            elif rid == _MSODRAWING:
                draw.append(data[off:off + size])
                draw.extend(data[o:o + n] for o, n in rec.conts)
            elif rid == _OBJ and ansi:
                kind = _OBJ5.get(struct.unpack_from("<H", data, off + 4)[0], "shape") if size >= 6 else "shape"
                if kind:
                    self.ctx.skipped[kind] += 1
            elif rid == _OBJ:
                obj = struct.unpack_from("<HHHH", data, off)[2:] if size >= 8 and data[off] == 0x15 else None
            elif rid == _TXO:
                if self.pictures:
                    text = self._txo(rec, grid)
                    if obj is not None and obj[0] == _COMMENT:
                        sh.comments[obj[1]] = text
                        text = ""
                    texts.append(text)
                obj = None
            elif rid in _FACTS:
                try:
                    cond = self._fact(rid, rec, sh, cond)
                except (ValueError, struct.error):
                    self.ctx.skipped["record"] += 1
            elif rid == _ROW:
                if size >= 14 and struct.unpack_from("<H", data, off + 12)[0] & 0x20:
                    grid.hide_row(struct.unpack_from("<H", data, off)[0])
            elif rid == _COLINFO:
                if size >= 10 and struct.unpack_from("<H", data, off + 8)[0] & 1:
                    grid.hide_cols(*struct.unpack_from("<HH", data, off))
            elif rid == _DIMENSIONS:
                if size >= 12:
                    rw1, rw2, col1, col2 = struct.unpack_from("<IIHH", data, off)
                    grid.declare(rw1, rw2 - 1, col1, col2 - 1)
            elif rid == _MERGED:
                n = min(struct.unpack_from("<H", data, off)[0] if size >= 2 else 0, max(size - 2, 0) // 8)
                for r1, r2, c1, c2 in struct.iter_unpack("<HHHH", data[off + 2:off + 2 + n * 8]):
                    grid.merge(r1, r2, c1, c2)
        if depth == 0:  # no record at all, or a stream cut before any BOF
            raise ReadError("damaged", "sheet does not start with a BOF")

    def _fact(self, rid, rec, sh, cond):
        """One NOTE, HLINK, DV, CONDFMT, CF, AUTOFILTERINFO, AUTOFILTER or PROTECT record into `sh`; the CONDFMT the
        CFs after it belong to, ([ranges], first row, first col) or None, is returned."""
        body = _body(self.data, rec)
        if rid == _NOTE and self.g.ansi:
            r, c, n = struct.unpack_from("<3H", body)
            text = body[6:6 + n].decode(self.g.ansi, "replace")
            if r != 0xFFFF:
                sh.comments[len(sh.notes)] = text
                sh.notes.append((r, c, len(sh.notes), ""))
            elif sh.notes:
                sh.comments[sh.notes[-1][2]] += text
        elif rid == _NOTE:
            r, c, _, oid = struct.unpack_from("<4H", body)
            try:
                author = biff.Cursor(body, [(8, len(body) - 8)]).string()
            except ValueError:
                author = ""
            sh.notes.append((r, c, oid, author))
        elif rid == _HLINK:
            got = _hlink(body)
            if got is None:
                self.ctx.skipped["link"] += 1
            elif got[4] or got[5]:
                sh.links.append(got)
        elif rid == _DV:
            text = self._dv(body, sh.env)
            if text:
                sh.valid.append((text, False))
        elif rid == _CONDFMT:
            bound = struct.unpack_from("<4H", body, 4)
            return [sheet_grid.span(*r) for r in _refs(body, 12)], bound[0], bound[2]
        elif rid == _CF:
            if cond is not None:
                sh.rules.append((cond[0], self._cf(body, replace(sh.env, row=cond[1], col=cond[2]))))
        elif rid == _SXVIEW:
            r1, r2, c1, c2 = struct.unpack_from("<4H", body)
            n = struct.unpack_from("<H", body, 40)[0]
            if r1 > r2 or c1 > c2 or len(body) < 45:
                raise ValueError("pivot view")
            sh.pivots.append((_hname(body, 44, n), sheet_grid.span(r1, r2, c1, c2), []))
        elif rid == _SXDI:
            n = struct.unpack_from("<H", body, 12)[0]
            if sh.pivots and n != 0xFFFF and len(sh.pivots[-1][2]) < limits.FACT_VALUES:
                sh.pivots[-1][2].append(_hname(body, 14, n))
        elif rid in _UNREAD:
            self.ctx.skipped["fact"] += 1
        elif rid == _AUTOFILTER:
            sh.crit.append(body)
        elif rid == _AUTOFILTERINFO:
            sh.filtered = True
        else:
            sh.protect = bool(struct.unpack_from("<H", body)[0])
        return cond

    def _text(self, rgce: bytes, env) -> str:
        return ptg.shown(rgce, b"", env, self.ctx)

    def _dv(self, body: bytes, env) -> str | None:
        """A DV ([MS-XLS] 2.4.93): dwDvFlags (the type in bits 0-3, fStrLookup 0x80, the operator in bits 20-23),
        four XLUnicodeStrings (prompt and error titles and texts), two formulas (cce, two unused bytes, the tokens),
        then the ranges, as `<ranges> <type> <rule>`; None for one that checks nothing."""
        flags = struct.unpack_from("<I", body)[0]
        pos = 4
        for _ in range(4):
            n, high = struct.unpack_from("<HB", body, pos)
            pos += 3 + n * (2 if high & 1 else 1)
        f = []
        for _ in range(2):
            n = struct.unpack_from("<H", body, pos)[0]
            f.append(body[pos + 4:pos + 4 + n])
            pos += 4 + n
        ranges = _refs(body, pos)
        kind = sheet_grid.VALIDATIONS[flags & 0xF] if flags & 0xF < len(sheet_grid.VALIDATIONS) else "any"
        if kind == "any" or not ranges:
            return None
        env = replace(env, row=ranges[0][0], col=ranges[0][2])
        op = (flags >> 20) & 0xF
        return sheet_grid.validation(", ".join(sheet_grid.span(*r) for r in ranges), kind,
                                     sheet_grid.OPERATORS[op] if op < len(sheet_grid.OPERATORS) else None,
                                     self._text(f[0], env), self._text(f[1], env) if f[1] else "",
                                     bool(flags & 0x80))

    def _cf(self, body: bytes, env) -> str:
        """A CF ([MS-XLS] 2.4.42): ct (1 a cell's value, 2 a formula), cp (the operator), cce1, cce2, the format,
        then the two formulas at the record's end."""
        ct, cp, n1, n2 = struct.unpack_from("<BBHH", body)
        if 6 + n1 + n2 > len(body):
            raise ValueError("CF formulas past the record")
        end = len(body)
        f1 = self._text(body[end - n2 - n1:end - n2], env)
        if ct == 2:
            return sheet_grid.rule("expression", (f1, ""))
        f2 = self._text(body[end - n2:], env) if n2 else ""
        return sheet_grid.rule("cellIs", (f1, f2), op=sheet_grid.OPERATORS[cp - 1] if 0 < cp <= 8 else None)

    def _formula(self, rec, out: list):
        """Keep a FORMULA's (row, col, tokens, extra) in `out` (rw, col, ixfe, an 8-byte result, grbit, chn, then cce
        and the tokens); its (row, col), or None for one too short to hold them."""
        body = _body(self.data, rec)
        if len(body) < 22:
            return None
        row, col = struct.unpack_from("<HH", body)
        cce = struct.unpack_from("<H", body, 20)[0]
        out.append((row, col, body[22:22 + cce], body[22 + cce:]))
        return row, col

    def _env(self) -> ptg.Env:
        """The workbook's formula tables: a sheet name (a range of them `First:Last`) per XTI, None for one outside
        the workbook, and the defined names in NAME order."""
        sheets = []
        for first, last in zip(self.g.externs, self.g.externs_last):
            if 0 <= first <= last < len(self.g.sheets):
                a, b = self.g.sheets[first].name, self.g.sheets[last].name
                sheets.append(a if first == last else f"{a}:{b}")
            else:
                sheets.append(None)
        return ptg.Env(False, sheets=sheets, names=[n.name for n in self.g.names])

    def _txo(self, rec, grid: sheet_grid.Grid) -> str:
        """The text of a TXO record, charged against the sheet characters (past them the sheet is cut, as by a cell).
        "" for a TXO under 12 bytes, one with no text, a cut sheet, and text its CONTINUEs do not hold (counted under
        record): cchText past them, an odd UTF-16 length, no CONTINUE."""
        if grid.cut or rec.size < 12:
            return ""
        n = struct.unpack_from("<H", self.data, rec.off + 10)[0]
        if not n:
            return ""
        segs = rec.conts[:-1] if len(rec.conts) > 1 else rec.conts  # the last CONTINUE holds the formatting runs
        try:
            cur = biff.Cursor(self.data, segs)
            text = cur.chars(n, bool(cur.u8() & 1))
        except ValueError:
            self.ctx.skipped["record"] += 1
            return ""
        budget = grid.budget
        if len(text) > budget.chars:
            grid.stop("characters", budget.cap_chars)
            return ""
        budget.chars -= len(text)
        return text

    def _cell(self, rec, grid: sheet_grid.Grid):
        """Put one cell record's displayed value. A FORMULA with a string result returns (row, col, ixfe) to wait for
        its STRING record."""
        data, g = self.data, self.g
        rid, off, size = rec.id, rec.off, rec.size
        if rid == _MULRK:
            row, first = struct.unpack_from("<HH", data, off)
            n = max(size - 6, 0) // 6
            for k, (ixfe, v) in enumerate(struct.iter_unpack("<HI", data[off + 4:off + 4 + n * 6])):
                grid.put(row, first + k, self._show(biff.rk(v), ixfe))
            return None
        row, col, ixfe = struct.unpack_from("<HHH", data, off)
        if rid == _LABELSST:
            if size >= 10:
                idx = struct.unpack_from("<I", data, off + 6)[0]
                if idx < len(g.sst):
                    grid.put(row, col, self._show(g.sst[idx], ixfe))
        elif rid in (_LABEL, _RSTRING):
            text = biff.Cursor(data, [(off + 6, size - 6)] + list(rec.conts)).string(g.ansi)
            grid.put(row, col, self._show(text, ixfe))
        elif rid == _NUMBER:
            if size >= 14:
                grid.put(row, col, self._show(struct.unpack_from("<d", data, off + 6)[0], ixfe))
        elif rid == _RK:
            if size >= 10:
                grid.put(row, col, self._show(biff.rk(struct.unpack_from("<I", data, off + 6)[0]), ixfe))
        elif rid == _BOOLERR:
            if size >= 8:
                val, is_err = data[off + 6], data[off + 7]
                grid.put(row, col, _ERRORS.get(val, "#N/A") if is_err else self._show(bool(val), ixfe))
        elif size >= 14:  # FORMULA: the cached result
            res = data[off + 6:off + 14]
            if res[6:8] != b"\xff\xff":
                grid.put(row, col, self._show(struct.unpack("<d", res)[0], ixfe))
            elif res[0] == 0:
                return row, col, ixfe
            elif res[0] == 1:
                grid.put(row, col, self._show(bool(res[2]), ixfe))
            elif res[0] == 2:
                grid.put(row, col, _ERRORS.get(res[2], "#N/A"))
        return None


class _Sheet:
    """What one worksheet's walk gathers beside its cells."""

    def __init__(self, env):
        self.env = env
        self.draw, self.starts, self.texts = [], [], []
        self.forms = ([], {})
        self.comments: dict[int, str] = {}
        self.notes: list = []
        self.links: list = []
        self.valid, self.rules, self.crit, self.pivots = [], [], [], []
        self.protect = self.filtered = False


_FACTS = frozenset({_NOTE, _HLINK, _DV, _CONDFMT, _CF, _AUTOFILTERINFO, _AUTOFILTER, biff.PROTECT, _SXVIEW, _SXDI,
                    *_UNREAD})


def _hname(body: bytes, pos: int, n: int) -> str:
    """An XLUnicodeStringNoCch of `n` characters at `pos`: a flags byte (bit 0: UTF-16), then the characters."""
    wide = body[pos] & 1
    raw = body[pos + 1:pos + 1 + n * (2 if wide else 1)]
    if len(raw) < n * (2 if wide else 1):
        raise ValueError("string cut")
    return raw.decode("utf-16-le" if wide else "latin-1", "replace").strip()


def _refs(body: bytes, pos: int) -> list:
    """A SqRefU at `pos`: its Ref8U (first row, last row, first col, last col), as many as the record holds."""
    n = min(struct.unpack_from("<H", body, pos)[0], (len(body) - pos - 2) // 8)
    return [(r1, r2, c1, c2) for r1, r2, c1, c2 in struct.iter_unpack("<4H", body[pos + 2:pos + 2 + n * 8])]


def _hstr(body: bytes, pos: int) -> tuple[str, int]:
    """A HyperlinkString: a character count (the NUL in it), UTF-16; the text and the offset after it."""
    n = struct.unpack_from("<I", body, pos)[0]
    end = pos + 4 + 2 * n
    if end > len(body):
        raise ValueError("hyperlink string past the record")
    return body[pos + 4:end].decode("utf-16-le", "replace").split("\0", 1)[0], end


def _hlink(body: bytes):
    """An HLINK as (r1, r2, c1, c2, url, place): the range, then the link object ([MS-OSHARED] 2.3.7.1) after the
    StdLink CLSID: streamVersion, flags, then as the flags say a display name (0x10), a frame name (0x80), a
    moniker (0x01; a string when 0x100, else a URL or file moniker) and a location (0x08). None for a moniker of
    another kind or bytes that end too soon."""
    try:
        r1, r2, c1, c2 = struct.unpack_from("<4H", body)
        flags = struct.unpack_from("<I", body, 28)[0]
        pos, url = 32, None
        if flags & 0x10:
            _, pos = _hstr(body, pos)
        if flags & 0x80:
            _, pos = _hstr(body, pos)
        if flags & 0x01 and flags & 0x100:
            url, pos = _hstr(body, pos)
        elif flags & 0x01:
            kind, pos = body[pos:pos + 16], pos + 16
            if kind == _URL_MONIKER:
                n = struct.unpack_from("<I", body, pos)[0]
                url = body[pos + 4:pos + 4 + n].decode("utf-16-le", "replace").split("\0", 1)[0]
                pos += 4 + n
            elif kind == _FILE_MONIKER:
                up, n = struct.unpack_from("<HI", body, pos)
                ansi = body[pos + 6:pos + 6 + n].split(b"\0", 1)[0].decode("cp1252", "replace")
                pos += 6 + n + 24
                wide = struct.unpack_from("<I", body, pos)[0]
                pos += 4
                if wide:
                    n = struct.unpack_from("<I", body, pos)[0]
                    ansi = body[pos + 6:pos + 6 + n].decode("utf-16-le", "replace")
                    pos += wide
                url = "..\\" * up + ansi
            else:
                return None
        place = _hstr(body, pos)[0] if flags & 0x08 else None
    except (struct.error, ValueError):
        return None
    return r1, r2, c1, c2, url[:MAX_URL] if url else None, place[:MAX_URL] if place else None


def _criterion(body: bytes, first: int) -> str:
    """An AUTOFILTER ([MS-XLS] 2.4.8) on column `first` + iEntry: a top or bottom N, or one or two DOPER tests (a
    number, string, Boolean or error, blanks), joined by and (wJoin 0) or or."""
    if len(body) < 24:
        return ""
    entry, grbit = struct.unpack_from("<HH", body)
    col = sheet_grid.letters(first + entry)
    if grbit & 0x10:
        return f"{col} {'top' if grbit & 0x20 else 'bottom'} {grbit >> 7}" + ("%" if grbit & 0x40 else "")
    pos, parts = 24, []
    for d in (body[4:14], body[14:24]):
        vt, sign = d[0], d[1]
        if vt == 2:
            value = sheet_grid.shown(biff.rk(struct.unpack_from("<I", d, 2)[0]), "General", False)
        elif vt == 4:
            value = sheet_grid.shown(struct.unpack_from("<d", d, 2)[0], "General", False)
        elif vt == 6:
            n = d[6]
            unit = 2 if pos < len(body) and body[pos] & 1 else 1
            raw = body[pos + 1:pos + 1 + n * unit]
            value = raw.decode("utf-16-le" if unit == 2 else "latin-1", "replace")
            pos += 1 + n * unit
        elif vt == 8:
            value = _ERRORS.get(d[2], "#N/A") if d[3] else ("TRUE" if d[2] else "FALSE")
        elif vt in (0x0C, 0x0E):
            parts.append(f"{col} is {'' if vt == 0x0C else 'not '}(blank)")
            continue
        else:
            continue
        parts.append(f"{col} {sheet_grid.SIGNS.get(sign, '=')} {value}")
    return (" or " if grbit & 3 == 1 else " and ").join(parts)


def _body(data: bytes, rec) -> bytes:
    """A record's body with its CONTINUEs joined."""
    body = data[rec.off:rec.off + rec.size]
    return body + b"".join(data[o:o + n] for o, n in rec.conts) if rec.conts else body


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(xls_alike.HEAD)
    what = xls_alike.kind(head)
    if what == "rtf":  # RTF saved as .xls: rtf.parse sends only compound-file and zip heads on, so no loop
        from . import rtf
        return rtf.parse(path, ctx)
    if what == "html":
        return xls_alike.parse_html(path, ctx)
    if what == "text":
        return xls_alike.parse_text(path, ctx)
    if what == "zip":  # an xlsb package holds xl/workbook.bin; any other zip goes where its parts say
        with Package(path) as pkg:  # a zip that cannot be opened is damaged
            binary = pkg.has("xl/workbook.bin")
        if binary:
            from . import xlsb
            return xlsb.parse_package(path, ctx)
        return zip_route.parse(path, ctx)
    if what != "cfb":
        raise ReadError("damaged", "not an Excel workbook")
    got = parse_compound(path, ctx)
    if isinstance(got, str):  # another format's compound file named .xls
        return cfb_route.parse(got, path, ctx)
    return got


def parse_compound(src, ctx) -> Doc | str:
    """Read the compound file `src` (a path or its bytes, nothing is written): the Doc, or the name of the other
    parser `cfb_route` picks for a compound file that holds no Workbook (BIFF8) or Book (BIFF5) stream. `parse` ends
    in it."""
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "xls", "no Workbook stream")
            if other is None:
                data = cf.read("Workbook" if cf.exists("Workbook") else "Book")
            if other is None:
                vba.compound(cf, ctx, any(n.upper().startswith("_VBA_PROJECT_CUR/") for n in cf.names()))
    except (KeyError, CfbError):
        raise ReadError("damaged", "Workbook stream") from None
    if other is not None:
        return other
    wb = Workbook(data, ctx, sheet_grid.Budget())
    lead = wb.book_facts()
    secs = wb.sections()
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=lead + secs, notes=wb.notes)
