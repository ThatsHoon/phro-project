"""Excel 97-2003 workbooks (.xls): one sheet section per worksheet, with displayed values, merges and hidden parts.

The container check, the BOUNDSHEET-ordered sheet walk, the ROW and COLINFO hidden flags and the cached formula values
(a string result comes from the STRING record after FORMULA, past any SHRFMLA, ARRAY or TABLE) were checked against
dochan `office_binary/xls.py` and anydoc `sheet/xls.rs`. Cells go through `numfmt.display`; formulas are never shown.
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
is one diagram scope, the workbook one connection budget. An embedded workbook reads no drawing and no TXO.
"""
import copy
import struct

from .. import diagram, limits
from ..containers.cfb import CfbError, Compound
from ..containers import flate
from ..containers.zipx import Package
from ..limits import ReadError
from ..model import Doc, Section
from ..numfmt import display
from . import biff, biff_chart, cfb_route, sheet_grid, xls_alike, xls_crypt, xls_drawing, zip_route

_ROW, _COLINFO, _MERGED = 0x0208, 0x007D, 0x00E5
_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD, 0x0205
_FORMULA, _STRING = 0x0006, 0x0207
_MSODRAWING = 0x00EC  # a sheet's drawing records, joined and read by xls_drawing
_TXO = 0x01B6        # a text box's text, in the CONTINUEs after it
_AFTER_FORMULA = frozenset({0x04BC, 0x0221, 0x0236})  # SHRFMLA, ARRAY, TABLE: may sit between FORMULA and STRING
_CELLS = frozenset({_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR, _FORMULA})
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A"}
_WORKSHEET, _MACRO, _CHART, _VBA = 0, 1, 2, 6
MAX_CHARTS = 256  # chart substreams one workbook reads


class Workbook:
    """One BIFF8 workbook stream. `g` holds its globals; `cells[i]` the displayed values of sheet i (BOUNDSHEET order)."""

    def __init__(self, data: bytes, ctx, budget: sheet_grid.Budget, pictures: bool = True):
        self.data, self.ctx, self.budget, self.pictures = data, ctx, budget, pictures
        self.st = biff.Stream(data, ctx)
        self.g = biff.read_globals(self.st)
        if self.g.filepass is not None:
            # only the default password is tried; the clear stream keeps its FILEPASS record, so its id is blanked
            clear = xls_crypt.decrypt(data, self.g.filepass)
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
                out.append(Section("sheet", ref.name, blocks=_hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", ref.name, blocks=blocks))
        return out

    def _charts(self, shown: list) -> dict:
        """Every chart of the shown sheets, in sheet then stream order: {sheet index: [Table]}. Each placement is
        charged against CHART_TOTAL, and one past it, or past MAX_CHARTS, is counted under chart."""
        out, seen = {}, set()
        charts = chart_cells = 0  # per call, so a second sections() counts what the first did
        for i, ref, _ in shown:
            for start in [ref.offset] if ref.kind == _CHART else self._starts.get(i, ()):
                if start in seen or charts >= MAX_CHARTS:
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
        draw: list = []
        starts: list = []
        texts: list = []
        try:
            self._walk(ref.offset, grid, draw, starts, texts)
        except Exception:
            del self.cells[i]  # a sheet that fails leaves no half-read cells for a chart to use
            raise
        self._starts[i] = starts
        blocks = grid.blocks()
        if (draw or texts) and self.pictures:  # an embedded workbook drops its drawing, so nothing is decoded
            blocks = self._drawn(blocks, b"".join(draw), texts)
        if draw and self.pictures:
            blocks = blocks + xls_drawing.pictures(self.g, b"".join(draw), self.flate, self.spent, self.ctx,
                                                       self.blips)
        self._blocks[i] = blocks
        return blocks

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
        return display(value, code, self.g.date1904)

    def _walk(self, offset: int, grid: sheet_grid.Grid, draw: list, starts: list, texts: list) -> None:
        """Walk sheet substream `offset` into `grid`; the sheet's MSODRAWING bodies, CONTINUEs joined, go into `draw`
        (a TXO or OBJ is another record, and a TXO's CONTINUEs belong to it); each TXO's text goes into `texts`
        (`_txo`; none for an embedded workbook); the offset of each chart substream nested in it goes into
        `starts`."""
        data = self.data
        if not 0 <= offset < len(data):
            raise ReadError("damaged", "sheet offset outside the stream")
        depth, pending = 0, None
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
            elif rid == _STRING:
                if pending is not None and not grid.cut:
                    r, c, ixfe = pending
                    pending = None
                    try:
                        text = biff.Cursor(data, [(off, size)] + list(rec.conts)).string()
                    except ValueError:
                        self.ctx.skipped["record"] += 1
                        continue
                    grid.put(r, c, self._show(text, ixfe))
            elif rid == _MSODRAWING:
                draw.append(data[off:off + size])
                draw.extend(data[o:o + n] for o, n in rec.conts)
            elif rid == _TXO:
                if self.pictures:
                    texts.append(self._txo(rec, grid))
            elif rid == _ROW:
                if size >= 14 and struct.unpack_from("<H", data, off + 12)[0] & 0x20:
                    grid.hide_row(struct.unpack_from("<H", data, off)[0])
            elif rid == _COLINFO:
                if size >= 10 and struct.unpack_from("<H", data, off + 8)[0] & 1:
                    first, last = struct.unpack_from("<HH", data, off)
                    grid.hide_cols(first, last)
            elif rid == _MERGED:
                n = min(struct.unpack_from("<H", data, off)[0] if size >= 2 else 0, max(size - 2, 0) // 8)
                for r1, r2, c1, c2 in struct.iter_unpack("<HHHH", data[off + 2:off + 2 + n * 8]):
                    grid.merge(r1, r2, c1, c2)
        if depth == 0:  # no record at all, or a stream cut before any BOF
            raise ReadError("damaged", "sheet does not start with a BOF")

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
            grid.put(row, col, self._show(biff.Cursor(data, [(off + 6, size - 6)] + list(rec.conts)).string(), ixfe))
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


def _hide(blocks: list) -> list:
    """Hidden copies of a hidden sheet's blocks, so showing hidden content marks them."""
    out = []
    for b in blocks:
        c = copy.copy(b)
        c.hidden = "hidden"
        out.append(c)
    return out


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
    parser `cfb_route` picks for a compound file that holds no Workbook stream. `parse` ends in it."""
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "xls", "no Workbook stream")
            if other is None:
                data = cf.read("Workbook")
    except (KeyError, CfbError):
        raise ReadError("damaged", "Workbook stream") from None
    if other is not None:
        return other
    secs = Workbook(data, ctx, sheet_grid.Budget()).sections()
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=secs)
