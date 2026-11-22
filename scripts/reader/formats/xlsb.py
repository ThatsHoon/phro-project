"""Excel binary workbooks (.xlsb): one sheet section per worksheet, with displayed values, merges and hidden parts.

The record layouts were checked against anydoc `sheet/xlsb.rs` (BrtWbProp, BrtBundleSh, the cell XF block of
styles.bin, BrtSSTItem, the row, cell, column and merge records) and the
framing in `containers/biff12.py`. Parts are found through the package relationships, with the conventional names
only when a relationship is absent; a part no relationship reaches is never read. Every handler runs per record:
the record size gives an exact step, so a bad record is counted under `record` and the walk goes on. Cells go
through `numfmt.display`; a formula shows its cached value, and its tokens (a shared or array one's from its
BrtShrFmla or BrtArrFmla) are read by `ptg` into the sheet's formulas paragraph, 3-D references through BrtExternSheet
and names through BrtName ([MS-XLSB] 2.4.647, 2.4.689, 2.5.173). A sheet that cannot be read, or whose workbook entry
names no readable part, is counted under `sheet` and the others still come out.
A sheet's charts and pictures come from `sheet_drawing` and follow the sheet's table in anchor order. A compound file named .xlsb goes to
`cfb_route`: an encrypted package is `encrypted`, and another format's file is read by its own parser.

The workbook's own facts come before its sheets ([MS-XLSB] 2.4): its names (BrtName, a hidden built-in one left out),
its external links by number (BrtSupBookSrc, each external link part's BrtBeginSupBook) and its protection
(BrtBookProtection). A sheet's facts follow its table, as `Grid.fact`: its own names, its tables (BrtListPart, each
table part's BrtBeginList), its autofilter (BrtBeginAFilter and its columns' filters), its validations (BrtDVal),
one line per conditional format rule (BrtBeginConditionalFormatting, BrtBeginCFRule) and its protection
(BrtSheetProtection). A BrtHLink links its cells; a comments part's BrtBeginComment and BrtCommentText become notes
on their cells, with their BrtCommentAuthor. A VBA project is named, never read.
"""
import re
import struct
from dataclasses import replace
from pathlib import Path

from .. import limits
from ..containers import biff12, cfb
from ..containers.zipx import Package
from ..limits import ReadError
from ..model import Doc, Para, Section, Text
from ..numfmt import code_for
from . import biff, cfb_route, ooxml_base, ptg, sheet_drawing, sheet_grid
from .word_fields import MAX_URL, comment_note

BRT_ROW_HDR = 0
BRT_CELL_RK, BRT_CELL_ERROR, BRT_CELL_BOOL, BRT_CELL_REAL, BRT_CELL_ST, BRT_CELL_ISST = 2, 3, 4, 5, 6, 7
BRT_FMLA_STRING, BRT_FMLA_NUM, BRT_FMLA_BOOL, BRT_FMLA_ERROR = 8, 9, 10, 11
BRT_COL_INFO = 60
BRT_CELL_RSTRING = 62
BRT_MERGE_CELL = 176
BRT_WS_DIM = 148
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!",
           0x2A: "#N/A", 0x2B: "#GETTING_DATA"}

BRT_SST_ITEM = 19
BRT_FMT = 44
BRT_XF = 47
BRT_WB_PROP = 153
BRT_BUNDLE_SH = 156
BRT_BEGIN_CELL_XFS = 617
BRT_END_CELL_XFS = 618
BRT_NAME = 39
BRT_SUP_BOOK_SRC, BRT_SUP_SELF, BRT_SUP_SAME, BRT_SUP_ADDIN = 355, 357, 358, 667
BRT_EXTERN_SHEET = 362
BRT_ARR_FMLA, BRT_SHR_FMLA = 426, 427
BRT_BOOK_PROTECTION, BRT_SHEET_PROTECTION = 534, 535
BRT_BEGIN_SUP_BOOK = 360
BRT_HLINK = 494
BRT_DVAL = 64
BRT_BEGIN_CF, BRT_END_CF, BRT_BEGIN_CF_RULE = 461, 462, 463
BRT_BEGIN_AFILTER, BRT_BEGIN_FILTER_COLUMN = 161, 163
BRT_BEGIN_FILTERS, BRT_END_FILTERS, BRT_FILTER, BRT_COLOR_FILTER, BRT_ICON_FILTER = 165, 166, 167, 168, 169
BRT_TOP10_FILTER, BRT_DYNAMIC_FILTER = 170, 171
BRT_BEGIN_CUSTOM_FILTERS, BRT_END_CUSTOM_FILTERS, BRT_CUSTOM_FILTER = 172, 173, 174
BRT_BEGIN_LIST, BRT_LIST_PART = 343, 661
BRT_COMMENT_AUTHOR, BRT_BEGIN_COMMENT, BRT_END_COMMENT, BRT_COMMENT_TEXT = 632, 635, 636, 637
_TEXT_OPERS = ("containsText", "notContainsText", "beginsWith", "endsWith")
_PERIODS = ("today", "tomorrow", "yesterday", "last7Days", "lastMonth", "nextMonth", "thisWeek", "nextWeek",
            "lastWeek", "thisMonth")
_TEMPLATES = {7: "uniqueValues", 9: "containsBlanks", 10: "notContainsBlanks", 11: "containsErrors",
              12: "notContainsErrors", 0x1B: "duplicateValues"}
_CF_TYPES = {3: "colorScale", 4: "dataBar", 6: "iconSet"}
_DYNAMIC = {1: "aboveAverage", 2: "belowAverage", 8: "tomorrow", 9: "today", 10: "yesterday", 11: "nextWeek",
            12: "thisWeek", 13: "lastWeek", 14: "nextMonth", 15: "thisMonth", 16: "lastMonth", 17: "nextQuarter",
            18: "thisQuarter", 19: "lastQuarter", 20: "nextYear", 21: "thisYear", 22: "lastYear", 23: "yearToDate",
            **{24 + q: f"Q{q + 1}" for q in range(4)}, **{28 + m: f"M{m + 1}" for m in range(12)}}

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
        self.supbooks: list[bool] = []  # one flag per supporting link: this workbook itself
        self.xti: list[tuple[int, int, int]] = []  # (supporting link, first sheet, last sheet) per XTI
        self.names: list[biff.DefinedName] = []  # one per BrtName, in order
        self.rels: dict = {}
        self.externals: list[str] = []
        self.protect: list[tuple[str, bool]] = []


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
            elif rid in (BRT_SUP_BOOK_SRC, BRT_SUP_SELF, BRT_SUP_SAME, BRT_SUP_ADDIN):
                wb.supbooks.append(rid == BRT_SUP_SELF)
                if rid == BRT_SUP_BOOK_SRC:
                    wb.externals.append(biff12.Fields(data, off, size).wide() or "")
            elif rid == BRT_BOOK_PROTECTION:
                f = biff12.Fields(data, off, size)
                f.skip(4)
                flags = f.u16()
                wb.protect = [(f"workbook {w}", False) for w, bit in (("structure", 1), ("windows", 2)) if flags & bit]
            elif rid == BRT_EXTERN_SHEET and not wb.xti:
                f = biff12.Fields(data, off, size)
                n = min(f.u32(), f.left() // 12)
                wb.xti = list(struct.iter_unpack("<Iii", data[f.pos:f.pos + n * 12]))
            elif rid == BRT_NAME:
                wb.names.append(_name(biff12.Fields(data, off, size)))
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


def _name(f) -> biff.DefinedName:
    """A BrtName: flags (0x01 hidden, 0x20 built-in), chKey, itab (0xFFFFFFFF the workbook), the name and its
    NameParsedFormula; `itab` becomes biff's one-based sheet index, 0 for the workbook."""
    flags = f.u32()
    f.u8()
    itab = f.u32()
    name = f.wide() or ""
    rgce, extra = _formula(f)
    return biff.DefinedName(name[6:] if name.startswith("_xlnm.") else name, flags,
                            0 if itab == 0xFFFFFFFF else itab + 1, rgce, extra)


def _formula(f) -> tuple[bytes, bytes]:
    """A parsed formula (cce, the tokens, cb, the extra data) as (tokens, extra)."""
    rgce = f.raw(f.u32())
    return rgce, f.raw(f.u32())


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


def _workbook_part(pkg) -> str:
    """The workbook part: the officeDocument relationship's target, else xl/workbook.bin."""
    root = pkg.rels("")
    named = [r for r in root.values() if r.type.endswith("/officeDocument") and not r.external]
    part = None
    if named:
        part = named[0].target if pkg.has(named[0].target) else None
    elif pkg.has(_PARTS["workbook"]):
        part = _PARTS["workbook"]
    if part is None:
        raise ReadError("damaged", "not an xlsb package")
    return part


def read_workbook(pkg, ctx) -> _Wb:
    """Find the workbook part and read its sheet list, styles and shared strings."""
    part = _workbook_part(pkg)
    rels = pkg.rels(part)
    wb = _Wb()
    wb.rels = rels
    _workbook(pkg, part, rels, wb, ctx)
    styles = _find(pkg, rels, "styles", _PARTS["styles"])
    if styles:
        _styles(pkg, styles, wb, ctx)
    strings = _find(pkg, rels, "sharedStrings", _PARTS["sharedStrings"])
    if strings:
        _strings(pkg, strings, wb, ctx)
    return wb


class _Facts:
    """What one sheet's records give beside its cells, gathered as they stream."""

    def __init__(self):
        self.links, self.valid, self.rules, self.crit, self.tables = [], [], [], [], []
        self.filter = self.cond = self.col = None
        self.vals, self.custom, self.more, self.blank, self.join = [], [], 0, False, " and "
        self.protect = False


class _Sheets:
    """The sheets of one workbook, sharing one cell budget, one image byte count, one chart cell count, one link
    scan count and one comment count."""

    def __init__(self, pkg, wb, ctx):
        self.pkg, self.wb, self.ctx = pkg, wb, ctx
        self.budget = sheet_grid.Budget()
        self.used: set[str] = set()      # sheet parts read
        self.draw = sheet_drawing.Drawings(pkg, ctx)  # charts and pictures; one image byte count and chart cell count
        self._code: dict[int, str] = {}
        self.notes: dict = {}
        self.comment_count, self.comments_cut, self.scan = 0, False, 0

    def book_facts(self) -> list:
        """The workbook's own facts, before its sheets: its names, its external links by number, its protection."""
        env = self._env()
        names = [e for e in (ptg.named(n, env, self.ctx) for n in self.wb.names if n.itab == 0) if e]
        links = []
        for k, rid in enumerate(self.wb.externals, 1):
            target = self._external(rid)
            if target:
                links.append((f"[{k}] {target}", False))
        b, ctx = self.budget, self.ctx
        return (sheet_grid.listed("Names", names, b, ctx) + sheet_grid.listed("External links", links, b, ctx)
                + sheet_grid.listed("Protected", self.wb.protect, b, ctx))

    def _external(self, rid: str) -> str | None:
        """Where an external link part points: its book's path, or its DDE or OLE source ([MS-XLSB] 2.4.235); None
        (counted under `link`) when the part or its path cannot be read."""
        rel = self.wb.rels.get(rid)
        if rel is not None and not rel.external and rel.type.endswith("/externalLink"):
            for brt, data, off, size in _walk(self.pkg, rel.target, self.ctx):
                if brt != BRT_BEGIN_SUP_BOOK:
                    continue
                try:
                    f = biff12.Fields(data, off, size)
                    sbt, first, second = f.u16(), f.wide() or "", f.wide() or ""
                except (ValueError, struct.error):
                    break
                if sbt == 1:
                    return f"DDE {first}|{second}"
                path = ooxml_base.rels(self.pkg, rel.target, self.ctx).get(first)
                if sbt in (0, 2) and path is not None and path.target:
                    return path.target if sbt == 0 else f"OLE {second} {path.target}"
                break
        self.ctx.skipped["link"] += 1
        return None

    def sections(self) -> list:
        out = []
        for no, (name, part, state) in enumerate(self.wb.sheets, 1):
            blocks = None  # stays None when the guard swallows an error, so the check below is real
            try:
                with self.ctx.guard("sheet"):
                    blocks = self.sheet(name, part, no, state in (1, 2))
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
                out.append(Section("sheet", name, blocks=sheet_grid.hide(blocks), hidden="hidden"))
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
        return sheet_grid.shown(value, code, self.wb.date1904)

    def sheet(self, name: str, part: str, no: int, hidden: bool) -> list:
        if not part or not self.pkg.has(part):
            raise ReadError("damaged", "sheet part missing")
        if part in self.used:
            raise ReadError("damaged", "sheet part used twice")
        self.used.add(part)
        if part in self.wb.chartsheets:  # a chartsheet has no cell records: its drawing is all there is
            return self.draw.blocks(part)
        grid = sheet_grid.Grid(name, self.budget, self.ctx)
        row, lost = None, False  # lost: the row header was bad, so its row is counted (once) and its cells go with it
        forms, anchored, last = [], {}, None  # each formula's (row, col, tokens, extra); shared and array ones by cell
        facts, rels, env = _Facts(), ooxml_base.rels(self.pkg, part, self.ctx), self._env()
        for rid, data, off, size in _walk(self.pkg, part, self.ctx):
            try:
                if rid == BRT_ROW_HDR:
                    row, lost = None, True
                    f = biff12.Fields(data, off, size)
                    row = f.u32()
                    lost = False
                    f.skip(7)
                    if f.u8() & 0x10:
                        grid.hide_row(row)
                elif rid in _CELLS:
                    if grid.cut or lost:
                        continue
                    if row is None:  # a cell before any row header has no row
                        self.ctx.skipped["record"] += 1
                        continue
                    f = biff12.Fields(data, off, size)
                    col = f.u32()
                    style = f.u32() & 0xFFFFFF
                    grid.put(row, col, self._cell(rid, f, style))
                    if rid in _FORMULAS:
                        last = None
                        try:
                            f.skip(2)
                            forms.append((row, col, *_formula(f)))
                            last = row, col
                        except ValueError:
                            self.ctx.skipped["formula"] += 1
                elif rid in (BRT_SHR_FMLA, BRT_ARR_FMLA):
                    f = biff12.Fields(data, off, size)
                    f.skip(16 if rid == BRT_SHR_FMLA else 17)
                    if last is not None:
                        anchored.setdefault(last, _formula(f))
                    last = None
                elif rid == BRT_COL_INFO:
                    f = biff12.Fields(data, off, size)
                    first_col, last_col = f.u32(), f.u32()
                    f.skip(8)
                    if f.u16() & 1:
                        grid.hide_cols(first_col, last_col)
                elif rid == BRT_WS_DIM:
                    f = biff12.Fields(data, off, size)
                    r1, r2, c1, c2 = f.u32(), f.u32(), f.u32(), f.u32()
                    grid.declare(r1, r2, c1, c2)
                elif rid == BRT_MERGE_CELL:
                    f = biff12.Fields(data, off, size)
                    r1, r2, c1, c2 = f.u32(), f.u32(), f.u32(), f.u32()
                    grid.merge(r1, r2, c1, c2)
                elif rid in _FACT_RECORDS:
                    self._fact(rid, biff12.Fields(data, off, size), facts, rels, env)
            except (ValueError, struct.error):
                self.ctx.skipped["record"] += 1
        ptg.put(grid, forms, anchored, env, self.ctx)
        self._facts(grid, no, facts, rels, env)
        for r1, r2, c1, c2, url, place in facts.links:
            if grid.cut:
                break
            if self.scan + min((r2 - r1 + 1) * (c2 - c1 + 1), len(grid.cells)) > limits.SHEET_SCAN:
                self.ctx.skipped["link"] += 1
                continue
            self.scan += grid.link(r1, r2, c1, c2, url, place)
        keys = []
        try:
            if grid.place_notes(self._marks(no, rels, keys), self.notes, hidden):
                self._cut_notes()
            blocks = grid.sheet_blocks() + self.draw.blocks(part)
            if sheet_grid.sweep(blocks, keys, self.notes):
                self._cut_notes()
        except BaseException:
            for k in keys:
                self.notes.pop(k, None)
            raise
        return blocks

    def _fact(self, rid: int, f, facts: _Facts, rels: dict, env: ptg.Env):
        """One record of `_FACT_RECORDS` into `facts`."""
        if rid == BRT_HLINK:
            rng = _rfx(f)
            target, place = f.wide() or "", (f.wide() or "").strip()
            if place:
                facts.links.append((*rng, None, place[:MAX_URL]))
            elif target:
                rel = rels.get(target)
                if rel is None or not rel.external or not rel.type.endswith("/hyperlink") or not rel.target:
                    self.ctx.skipped["link"] += 1
                else:
                    facts.links.append((*rng, rel.target[:MAX_URL], None))
        elif rid == BRT_DVAL:
            text = self._dval(f, env)
            if text:
                facts.valid.append((text, False))
        elif rid == BRT_BEGIN_CF:
            f.skip(8)
            facts.cond = _sqrfx(f)
        elif rid == BRT_END_CF:
            facts.cond = None
        elif rid == BRT_BEGIN_CF_RULE:
            if facts.cond:
                r1, _, c1, _ = facts.cond[0]
                facts.rules.append((", ".join(sheet_grid.span(*r) for r in facts.cond),
                                    self._rule(f, replace(env, row=r1, col=c1))))
        elif rid == BRT_SHEET_PROTECTION:
            f.skip(2)
            facts.protect = f.u32() == 1
        elif rid == BRT_LIST_PART:
            facts.tables.append(f.wide() or "")
        elif rid == BRT_BEGIN_AFILTER:
            facts.filter, facts.crit = _rfx(f), []
        elif facts.filter is not None:
            self._filter(rid, f, facts)

    def _filter(self, rid: int, f, facts: _Facts):
        """A record inside the sheet's autofilter: a column, its values, its custom tests, a top N or a dynamic one."""
        col = facts.col
        if rid == BRT_BEGIN_FILTER_COLUMN:
            facts.col = sheet_grid.letters(facts.filter[2] + f.u32())
        elif col is None:
            return
        elif rid == BRT_BEGIN_FILTERS:
            facts.vals, facts.more, facts.blank = [], 0, f.u32() != 0
        elif rid == BRT_FILTER:
            if len(facts.vals) < limits.FACT_VALUES:
                facts.vals.append(f.wide() or "")
            else:
                facts.more += 1
        elif rid == BRT_END_FILTERS:
            facts.crit.append((sheet_grid.picked(col, facts.vals, facts.more, facts.blank), False))
        elif rid == BRT_BEGIN_CUSTOM_FILTERS:
            facts.custom, facts.join = [], " or " if f.u32() else " and "
        elif rid == BRT_CUSTOM_FILTER:
            vts, sign, value = f.u8(), f.u8(), f.raw(8)
            if vts == 4:
                facts.custom.append(f"{col} {sheet_grid.SIGNS.get(sign, '=')} "
                                    f"{sheet_grid.shown(struct.unpack('<d', value)[0], 'General', False)}")
            elif vts == 8:
                facts.custom.append(f"{col} {sheet_grid.SIGNS.get(sign, '=')} {'TRUE' if value[0] else 'FALSE'}")
            elif vts == 6:
                facts.custom.append(f"{col} {sheet_grid.SIGNS.get(sign, '=')} {f.wide() or ''}")
            elif vts in (0x0C, 0x0E):
                facts.custom.append(f"{col} is {'' if vts == 0x0C else 'not '}(blank)")
        elif rid == BRT_END_CUSTOM_FILTERS:
            if facts.custom:
                facts.crit.append((facts.join.join(facts.custom), False))
        elif rid == BRT_TOP10_FILTER:
            flags, value = f.u8(), sheet_grid.shown(f.f64(), "General", False)
            facts.crit.append((f"{col} {'top' if flags & 1 else 'bottom'} {value}" + ("%" if flags & 2 else ""),
                               False))
        elif rid == BRT_DYNAMIC_FILTER:
            kind = _DYNAMIC.get(f.u32())
            if kind:
                facts.crit.append((f"{col} {kind}", False))
        elif rid in (BRT_COLOR_FILTER, BRT_ICON_FILTER):
            facts.crit.append((f"{col} by {'color' if rid == BRT_COLOR_FILTER else 'icon'}", False))

    def _dval(self, f, env: ptg.Env) -> str | None:
        """A BrtDVal ([MS-XLSB] 2.4.356): the flags (valType in bits 0-3, typOperator in bits 20-23), the ranges,
        four strings, two formulas; None for one that checks nothing. A list whose formula is one string is its
        values."""
        flags = f.u32()
        ranges = _sqrfx(f)
        for _ in range(4):
            f.wide()
        f1, x1 = _formula(f)
        f2, x2 = _formula(f)
        kind = sheet_grid.VALIDATIONS[flags & 0xF] if flags & 0xF < len(sheet_grid.VALIDATIONS) else "any"
        if kind == "any" or not ranges:
            return None
        env = replace(env, row=ranges[0][0], col=ranges[0][2])
        op = (flags >> 20) & 0xF
        literal = len(f1) >= 3 and f1[0] == 0x17 and len(f1) == 3 + 2 * struct.unpack_from("<H", f1, 1)[0]
        return sheet_grid.validation(", ".join(sheet_grid.span(*r) for r in ranges), kind,
                                     sheet_grid.OPERATORS[op] if op < len(sheet_grid.OPERATORS) else None,
                                     ptg.shown(f1, x1, env, self.ctx), ptg.shown(f2, x2, env, self.ctx) if f2 else "",
                                     literal)

    def _rule(self, f, env: ptg.Env) -> str:
        """A BrtBeginCFRule ([MS-XLSB] 2.4.23) in a few words: iType, iTemplate, dxfId, iPri, iParam, two reserved,
        the flags (fAbove 0x04, fBottom 0x08, fPercent 0x10), three formula sizes, strParam, then the formulas."""
        kind, template, _, _, param = (f.u32() for _ in range(5))
        f.skip(8)
        flags = f.u16()
        sizes = [f.u32() for _ in range(3)]
        text = f.wide() or ""
        if kind in _CF_TYPES:
            return sheet_grid.rule(_CF_TYPES[kind])
        if kind == 5:
            return sheet_grid.rule("top10", rank=param, bottom=bool(flags & 0x08), percent=bool(flags & 0x10))
        if kind == 1 or kind == 2 and template == 1:
            forms = []
            for n in sizes[:2]:
                forms.append(ptg.shown(*_formula(f), env, self.ctx) if n else "")
            if kind == 2:
                return sheet_grid.rule("expression", forms)
            return sheet_grid.rule("cellIs", forms, op=sheet_grid.OPERATORS[param - 1] if 0 < param <= 8 else None)
        if template == 8:
            return sheet_grid.rule(_TEXT_OPERS[param] if param < 4 else "containsText", text=text)
        if 0x0F <= template <= 0x18:
            return sheet_grid.rule("timePeriod", period=_PERIODS[template - 0x0F])
        if template in (0x19, 0x1A, 0x1D, 0x1E):
            return sheet_grid.rule("aboveAverage", below=template in (0x1A, 0x1E), dev=param or None)
        return sheet_grid.rule(_TEMPLATES.get(template, "expression"))

    def _facts(self, grid, no: int, facts: _Facts, rels: dict, env: ptg.Env):
        """The sheet's facts in their order on `grid`: names, tables, filter, validations, rules, protection."""
        grid.fact("Names", [e for e in (ptg.named(n, env, self.ctx) for n in self.wb.names if n.itab == no) if e])
        for rid in facts.tables:
            rel = rels.get(rid)
            if rel is not None and not rel.external and rel.type.endswith("/table"):
                self._table(grid, rel.target)
        if facts.filter is not None:
            where = sheet_grid.span(*facts.filter)
            if facts.crit:
                grid.fact(f"Filter {where}", facts.crit)
            else:
                grid.fact("Filter", [(where, False)])
        grid.fact("Validation", facts.valid)
        for where, rule in facts.rules:
            grid.fact(f"Conditional {where}", [(rule, False)])
        if facts.protect:
            grid.fact("Protected", [("sheet", False)])

    def _table(self, grid, part: str):
        """A table part's BrtBeginList: the range, lt, idList, crwHeader, crwTotals, the flags,
        six DXF ids, dwConnID, then stName and stDisplayName. One that cannot be read counts under `table`."""
        for rid, data, off, size in _walk(self.pkg, part, self.ctx):
            if rid != BRT_BEGIN_LIST:
                continue
            try:
                f = biff12.Fields(data, off, size)
                rng = _rfx(f)
                f.skip(8)
                heads, totals = f.u32(), f.u32()
                f.skip(32)
                title, shown = f.wide(), f.wide()
            except (ValueError, struct.error):
                break
            grid.table(shown or title or "", *rng, bool(heads), bool(totals))
            return
        self.ctx.skipped["table"] += 1

    def _marks(self, no: int, rels: dict, keys: list) -> dict:
        """The sheet's comments ([MS-XLSB] 2.1.7.8) as notes `xc<sheet>-<row>-<col>` ("Comment by <author>:", the
        author's own lead cut from the text) and their marks (row, col) -> (key, characters). At most SHEET_COMMENTS
        a workbook; past them, or past the characters left, the rest are cut, counted once as `comments_cut`."""
        part = next((r.target for r in rels.values() if not r.external and r.type.endswith("/comments")), None)
        marks, authors, at = {}, [], None
        for rid, data, off, size in _walk(self.pkg, part, self.ctx) if part else ():
            try:
                f = biff12.Fields(data, off, size)
                if rid == BRT_COMMENT_AUTHOR:
                    authors.append(f.wide() or "")
                elif rid == BRT_BEGIN_COMMENT:
                    i = f.u32()
                    r, _, c, _ = _rfx(f)
                    at = [r, c, authors[i] if i < len(authors) else "", ""]
                elif rid == BRT_COMMENT_TEXT and at is not None:
                    f.u8()
                    at[3] = f.wide() or ""
                elif rid == BRT_END_COMMENT and at is not None:
                    r, c, author, text = at
                    at = None
                    text = re.sub(r"\s+", " ", text).strip()
                    if author and text.startswith(author + ":"):
                        text = text[len(author) + 1:].lstrip()
                    if not text or r >= limits.SHEET_ROWS or c >= limits.SHEET_COLS:
                        continue
                    if self.comment_count >= limits.SHEET_COMMENTS or self.budget.chars < len(text):
                        self._cut_notes()
                        break
                    self.comment_count += 1
                    key = f"xc{no}-{r}-{c}"
                    blocks = comment_note([Para([Text(text)])], author)
                    if key in self.notes:
                        self.notes[key].extend(blocks)
                        marks[(r, c)] = (key, marks[(r, c)][1] + len(text))
                    else:
                        self.notes[key] = blocks
                        keys.append(key)
                        marks[(r, c)] = (key, len(text))
            except (ValueError, struct.error):
                self.ctx.skipped["record"] += 1
        return marks

    def _cut_notes(self):
        if not self.comments_cut:
            self.comments_cut = True
            self.ctx.skipped["comments_cut"] += 1

    def _env(self) -> ptg.Env:
        """A sheet name (a range of them `First:Last`) per XTI, None for one outside the workbook, and the names."""
        wb, sheets = self.wb, []
        for book, first, last in wb.xti:
            if book < len(wb.supbooks) and wb.supbooks[book] and 0 <= first <= last < len(wb.sheets):
                a, b = wb.sheets[first][0], wb.sheets[last][0]
                sheets.append(a if first == last else f"{a}:{b}")
            else:
                sheets.append(None)
        return ptg.Env(True, sheets=sheets, names=[n.name for n in wb.names])

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


_CELLS = frozenset({BRT_CELL_RK, BRT_CELL_ERROR, BRT_CELL_BOOL, BRT_CELL_REAL, BRT_CELL_ST, BRT_CELL_ISST,
                    BRT_CELL_RSTRING, BRT_FMLA_STRING, BRT_FMLA_NUM, BRT_FMLA_BOOL, BRT_FMLA_ERROR})
_FORMULAS = frozenset({BRT_FMLA_STRING, BRT_FMLA_NUM, BRT_FMLA_BOOL, BRT_FMLA_ERROR})
_FACT_RECORDS = frozenset({BRT_HLINK, BRT_DVAL, BRT_BEGIN_CF, BRT_END_CF, BRT_BEGIN_CF_RULE, BRT_SHEET_PROTECTION,
                           BRT_LIST_PART, BRT_BEGIN_AFILTER, BRT_BEGIN_FILTER_COLUMN, BRT_BEGIN_FILTERS,
                           BRT_END_FILTERS, BRT_FILTER, BRT_COLOR_FILTER, BRT_ICON_FILTER, BRT_TOP10_FILTER,
                           BRT_DYNAMIC_FILTER, BRT_BEGIN_CUSTOM_FILTERS, BRT_END_CUSTOM_FILTERS, BRT_CUSTOM_FILTER})


def _rfx(f) -> tuple:
    """An RfX or UncheckedRfX: (first row, last row, first col, last col)."""
    return f.u32(), f.u32(), f.u32(), f.u32()


def _sqrfx(f) -> list:
    """An UncheckedSqRfX: its count (-1 for none), then as many ranges as the record holds."""
    n = f.u32()
    n = 0 if n == 0xFFFFFFFF else min(n, f.left() // 16)
    return [_rfx(f) for _ in range(n)]


def parse(path, ctx) -> Doc:
    other = _check_container(Path(path))
    if other is not None:
        return cfb_route.parse(other, path, ctx)
    return parse_package(path, ctx)


def parse_package(src, ctx) -> Doc:
    """Read the xlsb package `src` (a path or its bytes, nothing is written). `parse` ends in it."""
    with Package(src) as pkg:
        if _workbook_part(pkg).lower().endswith(".xml"):  # an xlsx saved as .xlsb; read_package never calls back here
            from . import ooxml
            return ooxml.read_package(pkg, ctx)
        ooxml_base.macros(pkg, ctx)
        wb = read_workbook(pkg, ctx)
        if not wb.sheets:
            raise ReadError("damaged", "no readable sheets")
        sheets = _Sheets(pkg, wb, ctx)
        lead = sheets.book_facts()
        secs = sheets.sections()
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=lead + secs, notes=sheets.notes)
