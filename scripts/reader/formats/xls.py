"""Excel 97-2003 workbooks (.xls): one sheet section per worksheet, with displayed values, merges and hidden parts.

The container check, the BOUNDSHEET-ordered sheet walk, the ROW and COLINFO hidden flags and the cached formula values
(a string result comes from the STRING record after FORMULA, past any SHRFMLA, ARRAY or TABLE) were checked against
dochan `office_binary/xls.py` and anydoc `sheet/xls.rs`. Cells go through `numfmt.display`; formulas are never shown.
Every count from the file is bounded by the record it sits in: MULRK uses its own length, nested BOF and EOF pairs are
skipped by depth, and BOUNDSHEET offsets are checked against the stream. A FILEPASS workbook is decrypted by `xls_crypt`
when its default password verifies, else it is `encrypted`. A sheet that cannot be read is counted under
`sheet` and the others still come out.
"""
import copy
import struct

from .. import limits
from ..containers.cfb import CfbError, Compound, is_cfb
from ..containers import flate
from ..limits import ReadError
from ..model import Doc, Section
from ..numfmt import display
from . import biff, sheet_grid, xls_crypt, xls_drawing

_ROW, _COLINFO, _MERGED = 0x0208, 0x007D, 0x00E5
_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD, 0x0205
_FORMULA, _STRING = 0x0006, 0x0207
_MSODRAWING = 0x00EC
_AFTER_FORMULA = frozenset({0x04BC, 0x0221, 0x0236})  # SHRFMLA, ARRAY, TABLE: may sit between FORMULA and STRING
_CELLS = frozenset({_LABELSST, _LABEL, _RSTRING, _NUMBER, _RK, _MULRK, _BOOLERR, _FORMULA})
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A"}
_WORKSHEET, _MACRO, _CHART, _VBA = 0, 1, 2, 6


class Workbook:
    """One BIFF8 workbook stream. `g` holds its globals; `cells[i]` the displayed values of sheet i (BOUNDSHEET order)."""

    def __init__(self, data: bytes, ctx, budget: sheet_grid.Budget):
        self.data, self.ctx, self.budget = data, ctx, budget
        self.st = biff.Stream(data, ctx)
        self.g = biff.read_globals(self.st)
        if self.g.filepass is not None:
            # only the default password is tried; the clear stream keeps its FILEPASS record, so its id is blanked
            clear = xls_crypt.decrypt(data, self.g.filepass)
            if clear is None:
                raise ReadError("encrypted")
            patched = bytearray(clear)
            patched[self.g.filepass[1] - 4:self.g.filepass[1] - 2] = b"\0\0"
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

    # ---- sheets
    def sections(self) -> list:
        out, seen = [], set()
        for i, ref in enumerate(self.g.sheets):
            if ref.kind in (_MACRO, _VBA):
                continue
            if ref.kind == _CHART:  # Task 10 turns chart sheets into tables
                self.ctx.skipped["chart"] += 1
                continue
            blocks = None
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
            if ref.state:
                out.append(Section("sheet", ref.name, blocks=_hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", ref.name, blocks=blocks))
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
        self._walk(ref.offset, grid, draw)
        blocks = grid.blocks()
        if draw:
            blocks = blocks + xls_drawing.pictures(self.g, b"".join(draw), self.flate, self.spent, self.ctx)
        self._blocks[i] = blocks
        return blocks

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

    def _walk(self, offset: int, grid: sheet_grid.Grid, draw: list) -> None:
        """Walk sheet substream `offset` into `grid`; the sheet's MSODRAWING bodies, CONTINUEs joined, go into `draw`
        (a TXO or OBJ is another record, and a TXO's CONTINUEs belong to it)."""
        data, g = self.data, self.g
        if not 0 <= offset < len(data):
            raise ReadError("damaged", "sheet offset outside the stream")
        depth, pending = 0, None
        for rec in self.st.records(offset):
            rid, off, size = rec[0], rec[1], rec[2]
            if rid == biff.BOF:
                depth += 1
                if depth > limits.MAX_DEPTH:
                    raise ReadError("damaged", "sheet nested too deep")
                if depth == 1 and (size < 4 or struct.unpack_from("<H", data, off + 2)[0] != 0x0010):
                    raise ReadError("damaged", "not a worksheet")
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
                        text = biff.Cursor(data, [(off, size)] + list(rec[3])).string()
                    except ValueError:
                        self.ctx.skipped["record"] += 1
                        continue
                    grid.put(r, c, self._show(text, ixfe))
            elif rid == _MSODRAWING:
                draw.append(data[off:off + size])
                draw.extend(data[o:o + n] for o, n in rec[3])
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

    def _cell(self, rec, grid: sheet_grid.Grid):
        """Put one cell record's displayed value. A FORMULA with a string result returns (row, col, ixfe) to wait for
        its STRING record."""
        data, g = self.data, self.g
        rid, off, size = rec[0], rec[1], rec[2]
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
            grid.put(row, col, self._show(biff.Cursor(data, [(off + 6, size - 6)] + list(rec[3])).string(), ixfe))
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
        head = f.read(16)
    if not is_cfb(head):  # other heads are read by a later task
        raise ReadError("damaged", "not an Excel workbook")
    try:
        with Compound(path) as cf:
            if cf.exists("Workbook"):
                data = cf.read("Workbook")
            elif cf.exists("Book"):
                raise ReadError("unsupported", "Excel 5.0/95 workbook")
            else:
                raise ReadError("damaged", "no Workbook stream")
    except (KeyError, CfbError):
        raise ReadError("damaged", "Workbook stream") from None
    secs = Workbook(data, ctx, sheet_grid.Budget()).sections()
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=secs)
