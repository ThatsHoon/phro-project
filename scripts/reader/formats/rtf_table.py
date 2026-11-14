"""RTF tables: rows, cells, merges and nesting, built into a model `Table`.

Ported from MIT sources: the row and cell state (`\\trowd`, `\\cellx`, `\\cell`, `\\row`, the nested `\\nestcell` and
`\\nestrow`) follows anydoc (`rtf/table.rs`); the grid (the union of every row's right edges, one column per edge, a
cell's span from the columns its edges cover) follows anydoc's `shared/formats/grid.rs`; the merge chains are the ones
`doc.py`'s `_table` reads from Word binary files, written again here for RTF's own flags. Stdlib only.

`Tables` is one table, built while its story is read. It keeps one row-and-cell state per nesting level (`\\itap`
1..8). The reader calls `nest(itap)` before anything it does at a level; going deeper opens a level, going back
closes the deeper ones and puts each finished table into the open cell of the level above it, after the blocks that
were already there.

Edges. A row's right edges are forced increasing (a non-increasing one becomes the one before plus 1). The edges of
every row's cells are collected into one set, sorted once, and clustered: an edge within 10 twips of the first of its
cluster is that cluster's column. A cell spans the columns from the end of the one before it to its own edge's column,
at least one, so two cells of one row never share a column. A cell past the row's defined edges is one column wide.
A row's left edge (`\trleftN`; a row without one keeps the one before it, the first row has none) is in the same union.
When it is right of the table's leftmost column the row starts with an empty cell over the gap, as `doc.py` does; the
leftmost cluster, when a row's left edge is in it (Word's `\trleft-108`), is the table's edge and not a column. A left
edge at or past the row's first edge is put just below it.

Merges. `\\clmgf` opens a horizontal set that the `\\clmrg` cells right after it join; `\\clvmgf` starts a vertical set
that the cell over the same columns (start and span) in each next row with `\\clvmrg` joins. A joined cell is left out
and its blocks go to the set's first cell. A `\\clmrg` with no `\\clmgf` before it and a `\\clvmrg` with no `\\clvmgf` over
it stay visible, as cells of their own. `\\trhdr` makes the row's cells header cells.

Budgets, each counted under `cells` once. A row holds at most `GRID_COLS` cells and edges: the excess is dropped, once
per row. A table stops taking cells past the document's `TABLE_NODES` (the budget list is shared by every table of one
reader); its text is dropped, not run into the body. `\\itap` past 8 is clamped and counted under `depth`, once per
table; that text joins the cell at level 8. All of it is O(cells + edges log edges).
"""
from .. import limits
from ..model import Cell, Table

CLUSTER = 10  # twips: right edges this close to the first of a cluster are one column
MAX_LEVEL = 8

FIRST, CONT, VFIRST, VCONT = 1, 2, 4, 8
_FLAGS = {"first": FIRST, "cont": CONT, "vfirst": VFIRST, "vcont": VCONT}


class _Level:
    """The row and cell state of one nesting level."""
    __slots__ = ("rows", "edges", "eflags", "pend", "hdr", "cells", "blocks", "cut", "edgeset", "left", "lefts")

    def __init__(self):
        self.rows = []        # finished rows: (cells, header) with cells a list of (blocks, right edge or None, flags)
        self.edges = []       # the right edges of the row being defined, forced increasing
        self.eflags = []      # the merge flags of those cells
        self.pend = 0         # flags read since the last \cellx: they belong to the next cell
        self.hdr = False      # \trhdr
        self.cells = []       # the blocks of each finished cell of the open row
        self.blocks = []      # the blocks of the cell under way
        self.cut = False      # this row was already counted as cut
        self.edgeset = set()  # every right edge a stored cell uses, and every row's left edge
        self.left = None      # the left edge the open row has (kept by the rows after it until another is read)
        self.lefts = set()    # the left edges of the stored rows


class Tables:
    def __init__(self, ctx, budget=None):
        """`budget` is a one-item list holding the cells left in the document (TABLE_NODES), shared by its tables."""
        self.ctx = ctx
        self.budget = budget if budget is not None else [limits.TABLE_NODES]
        self.levels = [_Level()]
        self.full = False   # the budget ran out here: no more cells, counted once
        self.deep = False   # depth already counted

    @property
    def lv(self) -> _Level:
        return self.levels[-1]

    # ---- levels
    def nest(self, itap: int) -> None:
        """Make `itap` the level the next call works at: open the levels down to it, or close those above it."""
        if itap > MAX_LEVEL:
            if not self.deep:
                self.deep = True
                self.ctx.skipped["depth"] += 1
            itap = MAX_LEVEL
        itap = max(itap, 1)
        levels = self.levels
        while len(levels) > itap:
            table = self._close(levels.pop())
            if table is not None:
                levels[-1].blocks.append(table)
        while len(levels) < itap:
            levels.append(_Level())

    # ---- row definition
    def trowd(self) -> None:
        lv = self.lv
        lv.edges, lv.eflags, lv.pend, lv.hdr = [], [], 0, False

    def cellx(self, edge: int) -> None:
        lv = self.lv
        if len(lv.edges) >= limits.GRID_COLS:
            self._cut(lv)
            lv.pend = 0
            return
        if lv.edges and edge <= lv.edges[-1]:
            edge = lv.edges[-1] + 1
        lv.edges.append(edge)
        lv.eflags.append(lv.pend)
        lv.pend = 0

    def merge(self, kind: str) -> None:
        """`first` (\\clmgf), `cont` (\\clmrg), `vfirst` (\\clvmgf) or `vcont` (\\clvmrg): flags for the next \\cellx."""
        self.lv.pend |= _FLAGS.get(kind, 0)

    def trleft(self, edge: int) -> None:
        self.lv.left = edge

    def header_row(self) -> None:
        self.lv.hdr = True

    # ---- cells and rows
    def put(self, block) -> None:
        """A block of the cell under way (dropped once the table is full)."""
        if not self.full:
            self.lv.blocks.append(block)

    def cell(self, blocks=()) -> None:
        """End the cell under way, with `blocks` after what was put in it."""
        self._cell(self.lv, blocks)

    def _cell(self, lv, blocks=()) -> None:
        if blocks and not self.full:
            lv.blocks.extend(blocks)
        pending, lv.blocks = lv.blocks, []
        if self.full:
            return
        if len(lv.cells) >= limits.GRID_COLS:
            self._cut(lv)
            return
        if self.budget[0] <= 0:
            self.full = True
            self.ctx.skipped["cells"] += 1
            return
        self.budget[0] -= 1
        lv.cells.append(pending)

    def row(self) -> None:
        """End the row. Blocks after the last cell mark make one more cell; a row without cells is left out."""
        self._row(self.lv)

    def _row(self, lv) -> None:
        if lv.blocks:
            self._cell(lv)
        cells = lv.cells
        if cells:
            edges, eflags, used = lv.edges, lv.eflags, lv.edgeset
            n_edges = len(edges)
            row = []
            for k, blocks in enumerate(cells):
                e = edges[k] if k < n_edges else None
                if e is not None:
                    used.add(e)
                row.append((blocks, e, eflags[k] if k < n_edges else 0))
            left = lv.left
            if left is not None:
                if n_edges and left >= edges[0]:
                    left = edges[0] - 1  # a left edge at or past the first edge is put just below it
                lv.lefts.add(left)
            lv.rows.append((row, lv.hdr, left))
        lv.cells = []
        lv.cut = False

    def _cut(self, lv) -> None:
        if not lv.cut:
            lv.cut = True
            self.ctx.skipped["cells"] += 1

    # ---- the grid
    def finish(self) -> Table | None:
        """Close every open level; the outermost table, or None when it holds no row."""
        self.nest(1)
        table = self._close(self.levels[0])
        self.levels = [_Level()]
        return table

    def _close(self, lv) -> Table | None:
        if lv.blocks or lv.cells:
            self._row(lv)
        return self._grid(lv)

    @staticmethod
    def _grid(lv) -> Table | None:
        if not lv.rows:
            return None
        col_of, idx, base = {}, -1, None
        for e in sorted(lv.edgeset | lv.lefts):
            if base is None or e - base > CLUSTER:
                idx, base = idx + 1, e
            col_of[e] = idx
        # the leftmost cluster is the table's left edge, not a column, when only left edges are in it: the columns
        # then count from the next one, so rows at \trleft-108 add no column. Else column 0 ends at that cluster.
        origin = 0
        if lv.lefts and min(col_of[e] for e in lv.lefts) == 0 and min(col_of[e] for e in lv.edgeset) > 0:
            origin = 1
        if origin:
            for e in col_of:
                col_of[e] -= 1
        out, above = [], {}
        for cells, hdr, left in lv.rows:
            pos = 0 if left is None else col_of[left] + 1
            if cells[0][1] is not None:
                pos = min(pos, col_of[cells[0][1]])  # a left edge clustered with the first edge leaves no gap
            lead = [Cell([], colspan=pos)] if pos > 0 else []  # an empty cell over the gap before a row's left edge
            sets = []  # [column, span, flags, blocks, open to \clmrg]
            for blocks, e, f in cells:
                span = 1 if e is None else max(col_of[e] + 1 - pos, 1)
                if (f & CONT) and sets and sets[-1][4]:
                    last = sets[-1]
                    last[1] += span
                    last[3].extend(blocks)
                else:
                    sets.append([pos, span, f, blocks, bool(f & FIRST)])
                pos += span
            below, row = {}, lead
            for col, span, f, blocks, _ in sets:
                key = (col, span)
                if (f & (VFIRST | VCONT)) == VCONT and key in above:
                    origin = below[key] = above[key]
                    origin.rowspan += 1
                    origin.blocks.extend(blocks)
                    continue
                cell = Cell(blocks, colspan=span, header=hdr)
                row.append(cell)
                if f & VFIRST:
                    below[key] = cell
            above = below
            out.append(row)
        return Table(out)
