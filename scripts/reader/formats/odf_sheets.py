"""OpenDocument spreadsheets (.ods): one sheet section per table, bounded by row, column and cell caps.

A sheet's `table:shapes` that holds a connector is one diagram scope: every shape and connector in it, groups walked
into, goes to `diagram.Diagram` through `odf_base.register`; only its frames have a slot, after the table, as before."""
import math
import re

from .. import diagram, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..markdown import blocks_len
from ..model import Cell, Doc, Para, Section, Table, Text
from .odf_base import GROUPS, SHAPES, Body, Styles, _GAP, _SPAN, _empty, _int, _squash, edge_like, register, shape_key

_WS = re.compile(r"[ \t\r\n]+")
_COVERED = "covered-table-cell"
_NAMES = {}  # element tag -> local name; a sheet repeats a few tags millions of times
_GROUPS = ("table-header-rows", "table-rows", "table-row-group")
_HIDE_ROW = frozenset({"collapse", "filter"})


def _ln(tag):
    n = _NAMES.get(tag)
    if n is None:
        n = _NAMES[tag] = local(tag)
    return n


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

    def __init__(self, body, name, budget, chars):
        self.body, self.name, self.budget, self.chars = body, name, budget, chars  # cells and characters left for the document
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
            n = _ln(c.tag)
            if n == "table-row":
                self.row(c, d, header)
            elif n in _GROUPS:
                self.walk(c, d + 1, header or n == "table-header-rows")

    def row(self, tr, d, header):
        rep = max(1, _int(attr(tr, "number-rows-repeated"), 1))
        cells, chars = self.cells(tr, d, header)
        if not cells and not self.cols_cut:
            self.gap += rep
            return
        if self.gap:
            gap, self.gap = self.gap, 0
            if gap > _GAP:
                self.body.ctx.skipped["repeat"] += 1
            self.add([], 0, min(gap, _GAP), False)
        if not self.stop:
            self.add(cells, chars, 1 if self.cols_cut else rep, attr(tr, "visibility") in _HIDE_ROW)
        if self.cols_cut:
            self.cut("columns", limits.SHEET_COLS)

    def add(self, cells, chars, rep, hidden):
        """Append rep copies of a row, as many as the row, cell and character caps allow."""
        w, n, why = len(cells), rep, None
        room = limits.SHEET_ROWS - len(self.rows)
        if n > room:
            n, why = room, ("rows", limits.SHEET_ROWS)
        if w and n > self.budget[0] // w:
            n, why = self.budget[0] // w, ("cells", limits.SHEET_CELLS)
        if chars and n > self.chars[0] // chars:
            n, why = self.chars[0] // chars, ("characters", limits.SHEET_CHARS)
        n = max(n, 0)
        self.budget[0] -= w * n
        self.chars[0] -= chars * n
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
        """(the row's origin cells up to its last significant one, their text length);
        repeated empties past the last one are never built."""
        out, gap, col, chars = [], 0, 0, 0  # col counts the pending empties too
        empty = Cell([], header=header)
        self.cols_cut = False
        for c in tr:
            n = _ln(c.tag)
            if n != "table-cell" and n != _COVERED:
                continue
            rep = cs = rs = 1
            if c.attrib:
                rep = max(1, _int(attr(c, "number-columns-repeated"), 1))
                if n == "table-cell":
                    cs = min(max(_int(attr(c, "number-columns-spanned"), 1), 1), _SPAN)
                    rs = min(max(_int(attr(c, "number-rows-spanned"), 1), 1), _SPAN)
            if n == _COVERED:  # its origin's span covers the slot, so it has no cell of its own
                col += rep
                if col > limits.SHEET_COLS:
                    self.cols_cut = True
                    break
                continue
            blocks, text = self.blocks(c, d)
            if not text and cs == 1 and rs == 1:
                gap += rep
                col += rep
                continue
            take = min(rep, limits.SHEET_COLS - col)  # a span's other slots arrive as covered cells
            if take > 0:
                out.extend([empty] * gap)
                gap = 0
                out.extend([Cell(blocks, rs, cs, header)] * take)
                col += take
                chars += blocks_len(blocks) * take
            if take < rep:
                self.cols_cut = True
                break
        return out, chars

    def blocks(self, c, d):
        """(blocks, whether they carry anything but blank paragraphs)."""
        if len(c) == 1:  # the common cell: one plain paragraph
            p = c[0]
            if len(p) == 0 and p.text and p.text.strip() and _ln(p.tag) == "p":
                return [Para([Text(_WS.sub(" ", p.text))])], True
        blocks = self.body.blocks(c, d + 1)
        if not blocks or _empty(blocks):
            v = _value(c)
            return ([Para([Text(v)])], True) if v else ([], False)
        for a in kids(c, "annotation"):
            out = []
            self.body.annotation(a, out, d + 1)
            if isinstance(blocks[0], Para):
                blocks[0].inlines.extend(out)
        return blocks, True

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
    budget, chars, found, ok = [limits.SHEET_CELLS], [limits.SHEET_CHARS], 0, 0
    links = diagram.ConnectionBudget(ctx)  # the workbook's edges, shared by every sheet's diagram
    for t in kids(office, "table"):  # sheets of the same name are kept: each is a separate element
        found += 1
        name = _squash(attr(t, "name", ""), 200)
        try:
            with ctx.guard("sheet"):
                body.doc.blocks.append(_section(body, t, name, budget, chars, links))
                ok += 1
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["sheet"] += 1
    if found and not ok:
        raise ReadError("damaged", "no readable sheets")
    return body.doc


def _section(body, t, name, budget, chars, links) -> Section:
    sh = _Sheet(body, name, budget, chars)
    sh.walk(t, 0, False)
    blocks = sh.result()
    for shapes in kids(t, "shapes"):  # charts and pictures anchored to the sheet
        if not any(edge_like(el, body.styles) for el in shapes.iter()):
            for fr in kids(shapes, "frame"):
                blocks.extend(body.frame_blocks(fr, 1))
            continue
        dia, slots = diagram.Diagram(body.ctx, links), [(None, blocks)]
        _drawn(body, shapes, 0, dia, set(), slots)
        blocks = [b for _, got in slots for b in got]
        with body.ctx.guard("shape"):
            blocks = dia.place(slots)
    style = attr(t, "style-name")
    if style and body.styles.prop(style, "table", "display") == "false":
        return Section("sheet", name, blocks=body.hide(blocks), hidden="hidden")
    return Section("sheet", name, blocks=blocks)


def _drawn(body, cont, d, dia, keys, slots):
    """The shapes and connectors under cont, groups walked into up to limits.MAX_DEPTH, to the diagram; a frame of
    table:shapes itself also gets a slot."""
    if d > limits.MAX_DEPTH:
        body.deep()
        return
    for el in cont:
        n = local(el.tag)
        if n in GROUPS:
            _drawn(body, el, d + 1, dia, keys, slots)
        elif n in SHAPES:
            got = body.frame_blocks(el, 1) if n == "frame" else body.blocks(el, 1)
            key = shape_key(el, keys)
            with body.ctx.guard("shape"):
                register(dia, body.styles, el, key, got)
            if n == "frame" and d == 0:
                slots.append((key, got))
