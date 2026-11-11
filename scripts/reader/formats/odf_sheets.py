"""OpenDocument spreadsheets (.ods): one sheet section per table, bounded by row, column and cell caps."""
import math
import re

from .. import limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..model import Cell, Doc, Para, Section, Table, Text
from .odf_base import Body, Styles, _GAP, _SPAN, _empty, _int, _squash

_COVERED = "covered-table-cell"
_GROUPS = ("table-header-rows", "table-rows", "table-row-group")
_HIDE_ROW = frozenset({"collapse", "filter"})


def _plain(v):
    return re.sub(r"\.0+$", "", v.strip())[:64]


def _value(c) -> str:
    """The cell's value as text when it has no display text; formulas are never shown."""
    t = attr(c, "value-type")
    if t in ("float", "currency"):
        return _plain(attr(c, "value") or "")
    if t == "percentage":
        v = attr(c, "value") or ""
        try:
            x = float(v) * 100
        except ValueError:
            return _plain(v)
        return f"{x:.10g}%" if math.isfinite(x) else _plain(v)
    if t == "date":
        return (attr(c, "date-value") or "").strip()[:64]
    if t == "time":
        return (attr(c, "time-value") or "").strip()[:64]
    if t == "boolean":
        v = attr(c, "boolean-value")
        return "" if v is None else "TRUE" if v.strip().lower() == "true" else "FALSE"
    if t == "string":
        return _squash(attr(c, "string-value"), 1000)
    return ""


class _Sheet:
    """Rows of one table. Empty rows wait as a count until content follows; trailing ones are never built."""

    def __init__(self, body, name, budget):
        self.body, self.name, self.budget = body, name, budget  # budget: [cells left for the document]
        self.rows, self.hidden, self.spanned = [], set(), []
        self.gap, self.stop, self.cols_cut = 0, False, False

    def cut(self, what, n):
        self.body.ctx.warnings.append(f"sheet {self.name} cut at {n} {what}")
        self.body.ctx.skipped["sheet_cut"] += 1
        self.stop = True

    # ---- rows
    def walk(self, cont, d, header):
        if d > limits.MAX_DEPTH:
            self.body.deep()
            return
        for c in cont:
            if self.stop:
                return
            n = local(c.tag)
            if n == "table-row":
                self.row(c, d, header)
            elif n in _GROUPS:
                self.walk(c, d + 1, header or n == "table-header-rows")

    def row(self, tr, d, header):
        rep = max(1, _int(attr(tr, "number-rows-repeated"), 1))
        cells = self.cells(tr, d, header)
        if not cells and not self.cols_cut:
            self.gap += rep
            return
        if self.gap:
            gap, self.gap = self.gap, 0
            if gap > _GAP:
                self.body.ctx.skipped["repeat"] += 1
            self.add([], min(gap, _GAP), False)
        if not self.stop:
            self.add(cells, 1 if self.cols_cut else rep, attr(tr, "visibility") in _HIDE_ROW)
        if self.cols_cut:
            self.cut("columns", limits.SHEET_COLS)

    def add(self, cells, rep, hidden):
        """Append rep copies of a row, as many as the row and cell caps allow."""
        w, n, why = len(cells), rep, None
        room = limits.SHEET_ROWS - len(self.rows)
        if n > room:
            n, why = room, ("rows", limits.SHEET_ROWS)
        if w and n > self.budget[0] // w:
            n, why = self.budget[0] // w, ("cells", limits.SHEET_CELLS)
        n = max(n, 0)
        self.budget[0] -= w * n
        copy = None
        for i in range(n):
            if i == 0:
                row = cells
                self.spanned.extend((len(self.rows), c) for c in {id(c): c for c in cells}.values() if c.rowspan > 1)
            else:
                if copy is None:  # repeated rows share one set of cells; a span never crosses the copies
                    copy = [Cell(c.blocks, 1, c.colspan, c.header) for c in cells]
                row = copy
            if hidden:
                self.hidden.add(len(self.rows))
            self.rows.append(row)
        if why:
            self.cut(*why)

    # ---- cells
    def cells(self, tr, d, header):
        """The row's origin cells up to its last significant one; repeated empties past it are never built."""
        out, gap, col = [], 0, 0  # col counts the pending empties too
        empty = Cell([], header=header)
        self.cols_cut = False
        for c in tr:
            n = local(c.tag)
            if n != "table-cell" and n != _COVERED:
                continue
            rep = max(1, _int(attr(c, "number-columns-repeated"), 1))
            if n == _COVERED:  # its origin's span covers the slot, so it has no cell of its own
                col += rep
                if col > limits.SHEET_COLS:
                    self.cols_cut = True
                    break
                continue
            cs = min(max(_int(attr(c, "number-columns-spanned"), 1), 1), _SPAN)
            rs = min(max(_int(attr(c, "number-rows-spanned"), 1), 1), _SPAN)
            blocks = self.blocks(c, d)
            if _empty(blocks) and cs == 1 and rs == 1:
                gap += rep
                col += rep
                continue
            fit = limits.SHEET_COLS - col  # a span's other slots arrive as covered cells
            take = min(rep, fit)
            if take > 0:
                out.extend([empty] * gap)
                gap = 0
                out.extend([Cell(blocks, rs, cs, header)] * take)
                col += take
            if take < rep:
                self.cols_cut = True
                break
        return out

    def blocks(self, c, d):
        blocks = self.body.blocks(c, d + 1)
        if _empty(blocks):
            v = _value(c)
            return [Para([Text(v)])] if v else []
        for a in kids(c, "annotation"):
            out = []
            self.body.annotation(a, out, d + 1)
            if isinstance(blocks[0], Para):
                blocks[0].inlines.extend(out)
        return blocks

    # ---- result
    def result(self):
        rows = self.rows
        while rows and all(_empty(c.blocks) for c in rows[-1]):
            rows.pop()
            self.hidden.discard(len(rows))
        for r, c in self.spanned:
            c.rowspan = min(c.rowspan, max(1, len(rows) - r))
        if not rows:
            return []
        if len(rows) == 1 and len(rows[0]) == 1 and not self.hidden:
            c = rows[0][0]
            if c.rowspan == 1 and c.colspan == 1 and not c.header:
                return list(c.blocks)  # a lone cell is just its text
        return [Table(rows, hidden_rows=frozenset(self.hidden))]


def sheets(pkg, ctx, content, styles_root) -> Doc:
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "spreadsheet"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    budget, seen, found, ok = [limits.SHEET_CELLS], set(), 0, 0
    for t in kids(office, "table"):
        name = _squash(attr(t, "name", ""), 200)
        if name and name in seen:
            ctx.skipped["duplicate"] += 1
            continue
        seen.add(name)
        found += 1
        try:
            with ctx.guard("sheet"):
                body.doc.blocks.append(_section(body, t, name, budget))
                ok += 1
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["sheet"] += 1
    if not ok:
        raise ReadError("damaged", "no readable sheets")
    return body.doc


def _section(body, t, name, budget) -> Section:
    sh = _Sheet(body, name, budget)
    sh.walk(t, 0, False)
    blocks = sh.result()
    for shapes in kids(t, "shapes"):  # charts and pictures anchored to the sheet
        for fr in kids(shapes, "frame"):
            blocks.extend(body.frame_blocks(fr, 1))
    style = attr(t, "style-name")
    if style and body.styles.prop(style, "table", "display") == "false":
        return Section("sheet", name, blocks=body.hide(blocks), hidden="hidden")
    return Section("sheet", name, blocks=blocks)
