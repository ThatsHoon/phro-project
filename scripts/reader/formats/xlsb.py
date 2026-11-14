"""Excel binary workbooks (.xlsb): one sheet section per worksheet, with displayed values, merges and hidden parts.

The record layouts were checked against anydoc `sheet/xlsb.rs` (BrtWbProp, BrtBundleSh, the cell XF block of
styles.bin, BrtSSTItem, the row, cell, column and merge records) and `sheet/xlsx.rs` (drawing anchors), and the
framing in `containers/biff12.py`. Parts are found through the package relationships, with the conventional names
only when a relationship is absent; a part no relationship reaches is never read. Every handler runs per record:
the record size gives an exact step, so a bad record is counted under `record` and the walk goes on. Cells go
through `numfmt.display`; a formula shows its cached value and its expression is never read. A sheet that cannot
be read, or whose workbook entry names no readable part, is counted under `sheet` and the others still come out.
A sheet's drawing part is XML: its charts (cached values as a table, charged against `CHART_TOTAL`) and pictures
(charged against `IMAGE_BYTES`) follow the sheet's table in anchor order. A compound file named .xlsb goes to
`cfb_route`: an encrypted package is `encrypted`, and another format's file is read by its own parser.
"""
import re
import struct
from pathlib import Path

from .. import limits
from ..containers import biff12, cfb
from ..containers.zipx import Package, attr, local, first
from ..limits import ReadError
from ..model import Doc, Section
from ..numfmt import code_for, display
from . import biff, cfb_route, ooxml_base, sheet_grid
from .xls import _hide

BRT_ROW_HDR = 0
BRT_CELL_RK, BRT_CELL_ERROR, BRT_CELL_BOOL, BRT_CELL_REAL, BRT_CELL_ST, BRT_CELL_ISST = 2, 3, 4, 5, 6, 7
BRT_FMLA_STRING, BRT_FMLA_NUM, BRT_FMLA_BOOL, BRT_FMLA_ERROR = 8, 9, 10, 11
BRT_COL_INFO = 60
BRT_CELL_RSTRING = 62
BRT_MERGE_CELL = 176
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!",
           0x2A: "#N/A", 0x2B: "#GETTING_DATA"}
MAX_ANCHORS = 10_000  # drawing anchors one drawing part reads

BRT_SST_ITEM = 19
BRT_FMT = 44
BRT_XF = 47
BRT_WB_PROP = 153
BRT_BUNDLE_SH = 156
BRT_BEGIN_CELL_XFS = 617
BRT_END_CELL_XFS = 618

_PARTS = {"workbook": "xl/workbook.bin", "styles": "xl/styles.bin", "sharedStrings": "xl/sharedStrings.bin"}


class _Wb:
    """The workbook-level facts: sheets (name, part, hsState), the 1904 flag, shared strings and cell XF formats."""

    def __init__(self):
        self.sheets: list[tuple[str, str, int]] = []
        self.date1904 = False
        self.sst: list[str] = []
        self.xf_fmt: list[int] = []
        self.formats: dict[int, str] = {}
        self.chartsheets: set[str] = set()  # sheet parts whose relationship is a chartsheet


def _check_container(path) -> str | None:
    """None for a file that is not a compound file. A compound file is never an xlsb package: an encrypted one is
    `encrypted`, and another format's (an .xls saved as .xlsb) returns the parser `cfb_route` names."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head != cfb.MAGIC:
        return None
    damaged = "not an xlsb package (OLE file)"
    try:
        c = cfb.Compound(path)
    except ReadError:
        raise ReadError("damaged", damaged) from None
    with c:
        return cfb_route.route(c, "xlsb", damaged)


def _walk(pkg, name, ctx):
    """(id, data, offset, size) of each record of a part, or nothing when it cannot be read."""
    try:
        data = pkg.read(name)
    except KeyError:
        return
    for rid, off, size in biff12.records(data, ctx):
        yield rid, data, off, size


def _find(pkg, rels, kind, default):
    """The part a relationship of that type names; the conventional name only when there is no such relationship."""
    named = False
    for rel in rels.values():
        if rel.type.endswith("/" + kind) and not rel.external:
            named = True
            if pkg.has(rel.target):
                return rel.target
    if not named and pkg.has(default):
        return default
    return None


def _rid_key(rid: str):
    m = re.search(r"\d+", rid)
    return (int(m.group()) if m else 1 << 30, rid)


def _sheet_pair(data, off, size, at):
    """(strRelID, strName) read from `at` into a BrtBundleSh body; ValueError when it is not exactly the record."""
    f = biff12.Fields(data, off + at, size - at)
    rid = f.wide()
    name = f.wide()
    if rid is None or name is None or f.left():
        raise ValueError("BrtBundleSh strings")
    return rid, name


def _workbook(pkg, part, rels, wb, ctx):
    fallback = sorted((r for r in rels.values() if r.type.endswith("/worksheet") and not r.external),
                      key=lambda r: _rid_key(r.id))
    wb.chartsheets = {r.target for r in rels.values() if r.type.endswith("/chartsheet") and not r.external}
    index = 0
    for rid, data, off, size in _walk(pkg, part, ctx):
        try:
            if rid == BRT_WB_PROP:
                wb.date1904 = biff12.Fields(data, off, size).u32() & 1 != 0
            elif rid == BRT_BUNDLE_SH:
                n = index  # a short body must still use up its place, or every later fallback name slips
                index += 1
                f = biff12.Fields(data, off, size)
                state = f.u32()
                f.u32()  # iTabID
                pair = None
                for at in (8, 12):  # some writers put an extra zero dword before strRelID
                    try:
                        pair = _sheet_pair(data, off, size, at)
                        break
                    except (ValueError, struct.error):
                        continue
                if pair is not None:
                    rel = rels.get(pair[0])
                    wb.sheets.append((pair[1], rel.target if rel and not rel.external else "", state))
                else:
                    ctx.skipped["record"] += 1
                    if n < len(fallback):
                        wb.sheets.append((f"Sheet {n + 1}", fallback[n].target, state))
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1


def _styles(pkg, part, wb, ctx):
    in_cell_xfs = False
    for rid, data, off, size in _walk(pkg, part, ctx):
        try:
            if rid == BRT_BEGIN_CELL_XFS:
                in_cell_xfs = True
            elif rid == BRT_END_CELL_XFS:
                in_cell_xfs = False
            elif rid == BRT_FMT:
                f = biff12.Fields(data, off, size)
                ifmt = f.u16()
                code = f.wide()
                if code is None:
                    raise ValueError("BrtFmt code")
                wb.formats[ifmt] = code
            elif rid == BRT_XF and in_cell_xfs:
                f = biff12.Fields(data, off, size)
                f.u16()  # ixfeParent
                wb.xf_fmt.append(f.u16())
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1


def _strings(pkg, part, wb, ctx):
    for rid, data, off, size in _walk(pkg, part, ctx):
        if rid != BRT_SST_ITEM:
            continue
        try:
            f = biff12.Fields(data, off, size)
            f.u8()  # fRichStr / fExtStr
            text = f.wide()
            if text is None:
                raise ValueError("BrtSSTItem string")
            wb.sst.append(text)
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1
            wb.sst.append("")  # keep every later index pointing at its own string


def read_workbook(pkg, ctx) -> _Wb:
    """Find the workbook part and read its sheet list, styles and shared strings."""
    root = pkg.rels("")
    named = [r for r in root.values() if r.type.endswith("/officeDocument") and not r.external]
    part = None
    if named:
        part = named[0].target if pkg.has(named[0].target) else None
    elif pkg.has(_PARTS["workbook"]):
        part = _PARTS["workbook"]
    if part is None:
        raise ReadError("damaged", "not an xlsb package")
    if part.lower().endswith(".xml"):
        raise ReadError("unsupported", "xlsx workbook saved as .xlsb")
    rels = pkg.rels(part)
    wb = _Wb()
    _workbook(pkg, part, rels, wb, ctx)
    styles = _find(pkg, rels, "styles", _PARTS["styles"])
    if styles:
        _styles(pkg, styles, wb, ctx)
    strings = _find(pkg, rels, "sharedStrings", _PARTS["sharedStrings"])
    if strings:
        _strings(pkg, strings, wb, ctx)
    return wb


class _Sheets:
    """The sheets of one workbook, sharing one cell budget, one image byte count and one chart cell count."""

    def __init__(self, pkg, wb, ctx):
        self.pkg, self.wb, self.ctx = pkg, wb, ctx
        self.budget = sheet_grid.Budget()
        self.used: set[str] = set()      # sheet parts read
        self.drawings: set[str] = set()  # drawing parts read
        self.image_bytes = 0             # image bytes placed (IMAGE_BYTES)
        self.chart_cells = 0             # chart table cells placed (CHART_TOTAL)
        self._code: dict[int, str] = {}

    def sections(self) -> list:
        out = []
        for name, part, state in self.wb.sheets:
            try:
                with self.ctx.guard("sheet"):
                    blocks = self.sheet(name, part)
            except ReadError as e:
                if e.kind != "damaged":
                    raise
                self.ctx.skipped["sheet"] += 1
                continue
            if blocks is None:  # the guard counted a failure
                continue
            if not blocks and part in self.wb.chartsheets:  # a chartsheet with nothing drawn has no section
                continue
            if state in (1, 2):
                out.append(Section("sheet", name, blocks=_hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", name, blocks=blocks))
        return out

    def show(self, value, style: int) -> str:
        code = self._code.get(style)
        if code is None:
            xf = self.wb.xf_fmt
            code = self._code[style] = code_for(xf[style], self.wb.formats) if style < len(xf) else "General"
        if isinstance(value, str) and code == "General":
            return value
        return display(value, code, self.wb.date1904)

    def sheet(self, name: str, part: str) -> list:
        if not part or not self.pkg.has(part):
            raise ReadError("damaged", "sheet part missing")
        if part in self.used:
            raise ReadError("damaged", "sheet part used twice")
        self.used.add(part)
        if part in self.wb.chartsheets:  # a chartsheet has no cell records: its drawing is all there is
            return self.drawing(part)
        grid = sheet_grid.Grid(name, self.budget, self.ctx)
        row = None
        for rid, data, off, size in _walk(self.pkg, part, self.ctx):
            try:
                if rid == BRT_ROW_HDR:
                    row = None
                    f = biff12.Fields(data, off, size)
                    row = f.u32()
                    f.skip(7)
                    if f.u8() & 0x10:
                        grid.hide_row(row)
                elif rid in _CELLS:
                    if grid.cut:
                        continue
                    if row is None:  # a cell before any row header has no row
                        self.ctx.skipped["record"] += 1
                        continue
                    f = biff12.Fields(data, off, size)
                    col = f.u32()
                    style = f.u32() & 0xFFFFFF
                    grid.put(row, col, self._cell(rid, f, style))
                elif rid == BRT_COL_INFO:
                    f = biff12.Fields(data, off, size)
                    first_col, last_col = f.u32(), f.u32()
                    f.skip(8)
                    if f.u16() & 1:
                        grid.hide_cols(first_col, last_col)
                elif rid == BRT_MERGE_CELL:
                    f = biff12.Fields(data, off, size)
                    r1, r2, c1, c2 = f.u32(), f.u32(), f.u32(), f.u32()
                    grid.merge(r1, r2, c1, c2)
            except (ValueError, struct.error):
                self.ctx.skipped["record"] += 1
        return grid.blocks() + self.drawing(part)

    def _cell(self, rid: int, f, style: int) -> str:
        """The displayed value of one cell record whose header has been read. A formula shows its cached value."""
        if rid == BRT_CELL_RK:
            return self.show(biff.rk(f.u32()), style)
        if rid in (BRT_CELL_ERROR, BRT_FMLA_ERROR):
            return _ERRORS.get(f.u8(), "#N/A")
        if rid in (BRT_CELL_BOOL, BRT_FMLA_BOOL):
            return self.show(bool(f.u8()), style)
        if rid in (BRT_CELL_REAL, BRT_FMLA_NUM):
            return self.show(f.f64(), style)
        if rid == BRT_CELL_ISST:
            idx = f.u32()
            if idx >= len(self.wb.sst):
                raise ValueError("shared string index")
            return self.show(self.wb.sst[idx], style)
        if rid == BRT_CELL_RSTRING:  # a RichStr: fRichStr / fExtStr first; the runs after the string are not read
            f.u8()
        return self.show(f.wide() or "", style)  # BrtCellSt, BrtCellRString, BrtFmlaString

    # ---- drawings
    def drawing(self, part: str) -> list:
        """The charts and pictures of the drawing parts that the sheet relationships name, in anchor order."""
        out = []
        for rel in ooxml_base.rels(self.pkg, part, self.ctx).values():
            if rel.external or not rel.type.endswith("/drawing"):
                continue
            if rel.target in self.drawings:
                self.ctx.skipped["object"] += 1
                continue
            self.drawings.add(rel.target)
            try:
                root, nsmap = ooxml_base.load(self.pkg, rel.target)
            except ReadError as e:
                if e.kind not in ("damaged", "too_large"):
                    raise
                self.ctx.skipped["object"] += 1
                continue
            out += self.anchors(rel.target, root, nsmap)
        return out

    def anchors(self, part: str, root, nsmap) -> list:
        """Each anchor places every chart and picture it holds, group members included. Positioned anchors come in (row, column) order, then
        absolute ones in file order. An AlternateContent is replaced by the branch the reader understands."""
        found = []
        for a in self._descend(root, nsmap, _ANCHORS):
            if len(found) >= MAX_ANCHORS:
                self.ctx.skipped["object"] += 1
                break
            at = first(a, "from")
            if at is None:
                found.append((1, 0, 0, len(found), a))
            else:
                r = ooxml_base.int_of(_text(first(at, "row")))
                c = ooxml_base.int_of(_text(first(at, "col")))
                found.append((0, r, c, len(found), a))
        found.sort(key=lambda t: t[:4])
        out = []
        for *_, a in found:
            for el in self._descend(a, nsmap, ("chart", "pic")):
                if local(el.tag) == "chart":
                    self.chart(part, attr(el, "id"), out)
                else:
                    blip = first(el, "blip")
                    self.picture(part, attr(blip, "embed") if blip is not None else None, out)
        return out

    def _descend(self, root, nsmap, names):
        """The elements under `root` named in `names`, in document order, not entered once found; an
        AlternateContent is replaced by its chosen branch (counted under alternate when there is none)."""
        stack = [iter(root)]
        while stack:
            for el in stack[-1]:
                name = local(el.tag)
                if name in names:
                    yield el
                elif name == "AlternateContent":
                    chosen = ooxml_base.alternate(el, nsmap)
                    if chosen is None:
                        self.ctx.skipped["alternate"] += 1
                    else:
                        stack.append(iter(chosen))
                        break
                else:
                    stack.append(iter(el))
                    break
            else:
                stack.pop()

    def chart(self, part: str, rid, out: list) -> None:
        tbl = ooxml_base.chart(self.pkg, part, rid, self.ctx)
        if tbl is None:
            self.ctx.skipped["chart"] += 1
            return
        self.chart_cells += sum(len(r) for r in tbl.rows)
        if self.chart_cells > limits.CHART_TOTAL:
            self.ctx.skipped["chart"] += 1
        else:
            out.append(tbl)

    def picture(self, part: str, rid, out: list) -> None:
        img = ooxml_base.image(self.pkg, part, rid)
        if img is None:
            self.ctx.skipped["image"] += 1
            return
        self.image_bytes += len(img.data)
        if self.image_bytes > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
        else:
            out.append(img)


_ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")
_CELLS = frozenset({BRT_CELL_RK, BRT_CELL_ERROR, BRT_CELL_BOOL, BRT_CELL_REAL, BRT_CELL_ST, BRT_CELL_ISST,
                    BRT_CELL_RSTRING, BRT_FMLA_STRING, BRT_FMLA_NUM, BRT_FMLA_BOOL, BRT_FMLA_ERROR})


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse(path, ctx) -> Doc:
    other = _check_container(Path(path))
    if other is not None:
        return cfb_route.parse(other, path, ctx)
    with Package(path) as pkg:
        wb = read_workbook(pkg, ctx)
        if not wb.sheets:
            raise ReadError("damaged", "no readable sheets")
        secs = _Sheets(pkg, wb, ctx).sections()
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=secs)
