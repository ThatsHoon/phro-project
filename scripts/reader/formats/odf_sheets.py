"""OpenDocument spreadsheets (.ods): one sheet section per table, its cells through `sheet_grid.Grid`.

Each table's cells go to a Grid at their sheet addresses, so an ods sheet is split into islands and captioned, hidden,
merged and cut as the xlsx, xls and xlsb sheets are: one `Budget` for the document, a cell repeated by
`number-columns-repeated` or a row by `number-rows-repeated` put once a copy (and charged once a copy), empty ones
only counted. A row or column whose `table:visibility` is collapse or filter is hidden; a `table-header-rows` row is a
header row; a span is a merge, its covered cells passed over. A `table:formula` (OpenFormula, read as A1 text by `_of`)
goes to the sheet's formulas paragraph once a copy (a repeated one read again for each, charged to
FORMULA_COPY_CHARS, unless it holds no relative reference: then it is read once), and shows in its cell when the
cell has no value type.

A sheet's `table:shapes` that holds a connector is one diagram scope: every shape and connector in it, groups walked
into, goes to `diagram.Diagram` through `odf_base.register`; each with text, and each top-level frame, has a slot after
the table, so the text of a shape or connector that joins nothing stays in the flow. A `table:shapes` with no connector
reads as before: only its top-level frames."""
import math
import re

from .. import diagram, embedded, limits
from ..containers.zipx import attr, kids, local
from ..limits import ReadError
from ..markdown import blocks_len
from ..model import Doc, Para, Section, Text
from . import sheet_grid
from .odf_base import GROUPS, SHAPES, Body, Styles, _SPAN, _empty, _int, _squash, edge_like, register, shape_key

_WS = re.compile(r"[ \t\r\n]+")
_COVERED = "covered-table-cell"
_NAMES = {}  # element tag -> local name; a sheet repeats a few tags millions of times
_GROUPS = ("table-header-rows", "table-rows", "table-row-group")
_COLUMN_GROUPS = ("table-header-columns", "table-columns", "table-column-group")
_HIDE = frozenset({"collapse", "filter"})
_OF_HEAD = re.compile(r"(?:[a-z]+:)?=")
_OF_TOKEN = re.compile(r'"(?:[^"]|"")*"|\[[^\]]*\]|;')


def _ln(tag):
    n = _NAMES.get(tag)
    if n is None:
        n = _NAMES[tag] = local(tag)
    return n


def _plain(v):
    return re.sub(r"\.0+$", "", v.strip())[:64]


def _of(form) -> str:
    """An OpenFormula's text in A1 form without its `=`: `[.A1]` is A1, `[$Other.A1:.B2]` is Other!A1:B2, and the
    `;` between arguments is `,`. "" for an attribute that is not a formula."""
    m = _OF_HEAD.match(form or "")
    return _OF_TOKEN.sub(_of_token, form[m.end():]) if m else ""


def _of_token(m) -> str:
    s = m.group()
    if s == ";":
        return ","
    if s[0] == '"':
        return s
    ends = []
    for p in s[1:-1].split(":"):
        i = p.rfind(".")
        ends.append((p[:i].lstrip("$"), p[i + 1:]) if i >= 0 else ("", p))
    sheet = ends[0][0]
    return (sheet + "!" if sheet else "") + ":".join(cell if sh in ("", sheet) else f"{sh}!{cell}" for sh, cell in ends)


def _value(c) -> str:
    """The cell's value as text when it has no display text."""
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
    """The cells of one table, put into a Grid at their row and column."""

    def __init__(self, body, name, budget):
        self.body, self.name = body, name
        self.grid = sheet_grid.Grid(name, budget, body.ctx)
        self.at = 0
        self.anchored = []

    # ---- columns
    def columns(self, cont, d, col=0) -> int:
        """Hide the columns whose `table:visibility` hides them; the first column after `cont`'s."""
        if d > limits.MAX_DEPTH:
            self.body.deep()
            return col
        for el in cont:
            if col >= limits.SHEET_COLS:
                break
            n = _ln(el.tag)
            if n == "table-column":
                rep = max(1, _int(attr(el, "number-columns-repeated"), 1))
                if attr(el, "visibility") in _HIDE:
                    self.grid.hide_cols(col, col + rep - 1)
                col += rep
            elif n in _COLUMN_GROUPS:
                col = self.columns(el, d + 1, col)
        return col

    # ---- rows
    def walk(self, cont, d, header):
        if d > limits.MAX_DEPTH:
            self.body.deep()
            return
        for c in cont:
            if self.grid.cut:
                return
            n = _ln(c.tag)
            if n == "table-row":
                self.row(c, d, header)
            elif n in _GROUPS:
                self.walk(c, d + 1, header or n == "table-header-rows")

    def row(self, tr, d, header):
        rep = max(1, _int(attr(tr, "number-rows-repeated"), 1))
        r0, grid = self.at, self.grid
        self.at += rep
        if attr(tr, "visibility") in _HIDE:
            grid.hide_rows(r0, r0 + rep - 1)
        if header:
            grid.head_rows(r0, r0 + rep - 1)
        mark = len(self.anchored)
        cells, end, state, budget = self.cells(tr, d), min(r0 + rep, limits.SHEET_ROWS + 1), None, grid.budget
        if attr(tr, "visibility") in _HIDE and len(self.anchored) > mark:
            self.anchored[mark:] = self.body.hide(self.anchored[mark:])
        same = {}
        for r in range(r0, end) if cells else ():
            if state != (grid.merges_cut, grid.formulas_cut, budget.copies <= 0):
                state = grid.merges_cut, grid.formulas_cut, budget.copies <= 0
                gone = [c[6] and (state[1] and c[4] is None or state[2] and (rep > 1 or c[1] > 1) and c[0] not in same)
                        for c in cells]
                if any(gone):
                    self.body.ctx.skipped["formula"] += (end - r) * sum(c[1] for c, g in zip(cells, gone) if g)
                cells = [c[:6] + ("",) if g else c for c, g in zip(cells, gone)]
                cells = [c for c in cells if c[4] is not None or c[6] or ((c[2] > 1 or c[3] > 1) and not state[0])]
                if not cells:
                    return
            for col, crep, cs, rs, value, size, form in cells:
                if value is not None and not grid.put_cols(r, col, col + crep - 1, value, size):
                    return
                copied = len(form) if rep > 1 or crep > 1 else 0
                for c in range(col, col + crep) if form else ():
                    pat = same.get(col)
                    if pat is None:
                        if budget.copies < copied:
                            budget.copies = 0
                            self.body.ctx.skipped["formula"] += col + crep - c
                            break
                        budget.copies -= copied
                        pat = sheet_grid.pattern(form, r, c)
                        if copied and sheet_grid.fixed(pat):
                            same[col] = pat
                    grid.formula(r, c, pat)
                    if grid.cut:
                        return
                if (cs > 1 or rs > 1) and not grid.merges_cut:
                    for c in range(col, col + crep):
                        grid.merge(r, r + rs - 1, c, c + cs - 1)

    # ---- cells
    def cells(self, tr, d) -> list:
        """(column, repeats, column span, row span, blocks or None, their characters, formula) of the row's cells that
        hold something, have a formula or span. A cell's repeats end one column past SHEET_COLS, where `put` stops
        the sheet, and the row ends with it."""
        out, col = [], 0
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
            if n == "table-cell":
                form = _of(attr(c, "formula")) if c.attrib else ""
                blocks, text = self.blocks(c, d, form)
                if text or form or cs > 1 or rs > 1:
                    out.append((col, min(rep, max(1, limits.SHEET_COLS + 1 - col)), cs, rs,
                                tuple(blocks) if text else None, blocks_len(blocks), form))
                    if col + rep > limits.SHEET_COLS:
                        break
            col += rep
        return out

    def blocks(self, c, d, form=""):
        """(blocks, whether they carry anything but blank paragraphs); a formula cell with no value type shows `=`
        and its formula."""
        if len(c) == 1:  # the common cell: one plain paragraph
            p = c[0]
            if len(p) == 0 and p.text and p.text.strip() and _ln(p.tag) == "p":
                return [Para([Text(_WS.sub(" ", p.text))])], True
        blocks = self.body.blocks(c, d + 1, self.anchored)
        if not blocks or _empty(blocks):
            v = _value(c) or ("=" + form if form and attr(c, "value-type") is None else "")
            return ([Para([Text(v)])], True) if v else ([], False)
        for a in kids(c, "annotation"):
            out = []
            self.body.annotation(a, out, d + 1)
            if isinstance(blocks[0], Para):
                blocks[0].inlines.extend(out)
        return blocks, True

    # ---- result
    def result(self):
        return self.grid.sheet_blocks()


def sheets(pkg, ctx, content, styles_root, first=False) -> Doc:
    """The workbook's sheets, over the document's one Budget; with `first` the read ends with the first sheet that
    is shown (an embedded workbook)."""
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "spreadsheet"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    budget, found, ok = embedded.of(ctx).budget(), 0, 0
    links = diagram.ConnectionBudget(ctx)  # the workbook's edges, shared by every sheet's diagram
    for t in kids(office, "table"):  # sheets of the same name are kept: each is a separate element
        if ctx.late():
            break
        found += 1
        name = _squash(attr(t, "name", ""), 200)
        try:
            with ctx.guard("sheet"):
                body.doc.blocks.append(_section(body, t, name, budget, links))
                ok += 1
                if first and not body.doc.blocks[-1].hidden:
                    break
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["sheet"] += 1
    if found and not ok:
        raise ReadError("damaged", "no readable sheets")
    return body.doc


def _section(body, t, name, budget, links) -> Section:
    sh = _Sheet(body, name, budget)
    sh.columns(t, 0)
    sh.walk(t, 0, False)
    blocks = sh.result() + sh.anchored
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
    """The shapes and connectors under cont, groups walked into up to limits.MAX_DEPTH, to the diagram; each with
    blocks gets a slot, so its text stays in the flow, and so does a frame of table:shapes itself."""
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
            if got or (n == "frame" and d == 0):
                slots.append((key, got))
