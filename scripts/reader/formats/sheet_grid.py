"""A sparse sheet-cell builder for the xls, xlsb and embedded-workbook readers.

Cells arrive in any order as (row, col, text). `Grid.blocks()` lays them out as one Table whose cost follows the
number of cells put, never the sheet's extent: rows before the first value are trimmed, a long run of empty rows
shrinks, and hidden rows and columns and merged ranges are kept as sorted intervals. The hidden-merge rule (a merge
whose origin is hidden keeps its text at its first visible slot; its extent counts toward the table) follows anydoc.
One `Budget` is shared by every sheet of a document.
"""
import heapq
from bisect import bisect_left, bisect_right, insort

from .. import limits
from ..model import Bold, Break, Cell, Link, Para, Table, Text

_GAP = 64            # empty rows kept between two rows that hold values
_MERGES = 100_000    # merged ranges one sheet may keep


def _chars(value) -> int:
    """The characters a cell value is charged: a str's length; for inlines, every Text inside (Link and Bold
    contents included) plus each Link URL's length."""
    if isinstance(value, str):
        return len(value)
    n, stack = 0, [value]
    while stack:
        for x in stack.pop():
            if isinstance(x, Text):
                n += len(x.text)
            elif isinstance(x, Link):
                n += len(x.url)
                stack.append(x.inlines)
            elif isinstance(x, Bold):
                stack.append(x.inlines)
    return n


def text_inlines(text: str) -> list:
    """The Text and Break pieces of a text, split on newlines."""
    inl = []
    for i, part in enumerate(text.split("\n")):
        if i:
            inl.append(Break())
        if part:
            inl.append(Text(part))
    return inl


class Budget:
    """Cells and characters left for a document's sheets; shared, so embedded workbooks count too."""

    def __init__(self, cells=None, chars=None):
        self.cells = self.cap_cells = limits.SHEET_CELLS if cells is None else cells
        self.chars = self.cap_chars = limits.SHEET_CHARS if chars is None else chars


class _Ranges:
    """Inclusive integer intervals; sorted and joined on first lookup, then searched with bisect."""

    def __init__(self):
        self.raw, self.starts, self.ends, self.dirty = [], [], [], False

    def add(self, a, b):
        self.raw.append((a, b))
        self.dirty = True

    def _norm(self):
        if not self.dirty:
            return
        self.dirty = False
        self.raw.sort()
        out = []
        for a, b in self.raw:
            if out and a <= out[-1][1] + 1:
                if b > out[-1][1]:
                    out[-1][1] = b
            else:
                out.append([a, b])
        self.raw = [(a, b) for a, b in out]
        self.starts = [a for a, _ in self.raw]
        self.ends = [b for _, b in self.raw]

    def __contains__(self, x):
        self._norm()
        i = bisect_right(self.starts, x) - 1
        return i >= 0 and self.ends[i] >= x

    def __bool__(self):
        return bool(self.raw)

    def first_free(self, a, b):
        """The first x in a..b outside every interval, or None."""
        self._norm()
        x = a
        while x <= b:
            i = bisect_right(self.starts, x) - 1
            if i < 0 or self.ends[i] < x:
                return x
            x = self.ends[i] + 1
        return None


class _Spans:
    """The row spans still open in blocks()'s row walk: disjoint column intervals, each open until its last row.
    A span's start and expiry cost a heap step, a list insert and a Fenwick update over the table's columns; counting
    the covered columns of a range costs two bisects and two Fenwick sums, whatever the number of open spans."""

    def __init__(self, ncols: int):
        self.heap = []              # (last row, first column)
        self.starts = []            # first columns, sorted
        self.last = {}              # first column -> last column
        self.tree = [0] * (ncols + 1)  # Fenwick tree: the widths of the spans, by first column

    def _add(self, i: int, w: int):
        i += 1
        while i < len(self.tree):
            self.tree[i] += w
            i += i & -i

    def _below(self, i: int) -> int:
        """The widths of the spans whose first column is below i."""
        s = 0
        while i > 0:
            s += self.tree[i]
            i -= i & -i
        return s

    def open(self, last_row: int, c1: int, c2: int):
        heapq.heappush(self.heap, (last_row, c1))
        insort(self.starts, c1)
        self.last[c1] = c2
        self._add(c1, c2 - c1 + 1)

    def expire(self, row: int):
        """Close the spans whose last row is before `row`."""
        while self.heap and self.heap[0][0] < row:
            _, c1 = heapq.heappop(self.heap)
            del self.starts[bisect_left(self.starts, c1)]
            self._add(c1, c1 - self.last.pop(c1) - 1)

    def uncovered(self, a: int, b: int) -> int:
        """Columns in a..b-1 outside every open span (b - a when that is not positive, as nothing is covered)."""
        if b <= a or not self.starts:
            return b - a
        n = self._below(b) - self._below(a)  # the spans starting in a..b-1, whole
        i = bisect_left(self.starts, a) - 1   # one starting before a may reach into the range
        if i >= 0 and self.last[self.starts[i]] >= a:
            n += min(self.last[self.starts[i]], b - 1) - a + 1
        j = bisect_left(self.starts, b) - 1   # the last one starting in the range may run past it
        if j >= 0 and self.starts[j] >= a and self.last[self.starts[j]] >= b:
            n -= self.last[self.starts[j]] - b + 1
        return b - a - n


class Grid:
    def __init__(self, name: str, budget: Budget, ctx):
        self.name, self.budget, self.ctx = name, budget, ctx
        self.cells = {}
        self.merges = []
        self._seen = set()  # the merges kept, so an exact duplicate is dropped before the cap counts it
        self.rows_h, self.cols_h = _Ranges(), _Ranges()
        self.cut = False
        self._merges_cut = False

    # ---- input
    def stop(self, what, n):
        """End the sheet at the limit of `n` `what`: one `put` or `blocks` reached, or one the caller found that `put`
        cannot see (a field past the whole `what` budget). False, so `put` can return it."""
        self.cut = True
        self.ctx.warnings.append(f"sheet {self.name} cut at {n} {what}")
        self.ctx.skipped["sheet_cut"] += 1
        return False

    def put(self, row: int, col: int, value) -> bool:
        """Put a str, or a list of inlines (Text, Break, Bold, Link, NoteRef), at a cell. A value put over another
        replaces it, and the replaced value's cell and characters are refunded."""
        if self.cut:
            return False
        if not value or row < 0 or col < 0:
            return True
        b = self.budget
        if row >= limits.SHEET_ROWS:
            return self.stop("rows", limits.SHEET_ROWS)
        if col >= limits.SHEET_COLS:
            return self.stop("columns", limits.SHEET_COLS)
        old = self.cells.get((row, col))
        cells = b.cells + (old is not None)
        chars = b.chars + (_chars(old) if old is not None else 0)
        n = _chars(value)
        if cells <= 0:
            return self.stop("cells", b.cap_cells)
        if chars < n:
            return self.stop("characters", b.cap_chars)
        b.cells, b.chars = cells - 1, chars - n
        self.cells[(row, col)] = value
        return True

    def append(self, row: int, col: int, inlines: list) -> bool:
        """Add inlines (a note mark) to the end of a cell that already holds a value. Unlike `put` it works on a cut
        grid, as it adds no cell; it charges only the added characters. False when the cell holds nothing or the
        characters are gone."""
        old = self.cells.get((row, col))
        if old is None or not inlines:
            return False
        n = _chars(inlines)
        if self.budget.chars < n:
            return False
        self.cells[(row, col)] = (text_inlines(old) if isinstance(old, str) else list(old)) + list(inlines)
        self.budget.chars -= n
        return True

    def covers(self, positions) -> dict:
        """position -> (row, col) of the origin of the merge that renders over it, for the positions that lie in
        such a merge but are not its origin. The merges are resolved as `blocks()` resolves them."""
        return {p: (m[0], m[1]) for p, m in self._cover(sorted(set(positions))) if m is not None and p != (m[0], m[1])}

    def get(self, row: int, col: int):
        """The value put at a cell (str or list), or None."""
        return self.cells.get((row, col))

    def held(self, r1: int, r2: int, c1: int, c2: int):
        """The positions holding a value inside the inclusive range, in reading order, and the cost
        min(area, cells held): the range is walked, or the held cells are, whichever is fewer."""
        area = max(r2 - r1 + 1, 0) * max(c2 - c1 + 1, 0)
        if area <= len(self.cells):
            found = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1) if (r, c) in self.cells]
            return found, area
        found = sorted(p for p in self.cells if r1 <= p[0] <= r2 and c1 <= p[1] <= c2)
        return found, len(self.cells)

    def merge(self, r1: int, r2: int, c1: int, c2: int):
        """Merges are only checked here; overlaps are resolved in blocks()'s row sweep."""
        if r1 > r2 or c1 > c2 or r1 < 0 or c1 < 0:
            return
        r2, c2 = min(r2, limits.SHEET_ROWS - 1), min(c2, limits.SHEET_COLS - 1)
        if r1 > r2 or c1 > c2 or (r1 == r2 and c1 == c2):
            return
        m = (r1, c1, r2, c2)  # reading order of the first cell, then the extent
        if m in self._seen:
            return
        if len(self.merges) >= _MERGES:
            if not self._merges_cut:
                self._merges_cut = True
                self._stop_merges()
            return
        self._seen.add(m)
        self.merges.append(m)

    def _stop_merges(self):
        self.ctx.warnings.append(f"sheet {self.name} cut at {_MERGES} merged ranges")
        self.ctx.skipped["sheet_cut"] += 1

    def hide_row(self, r: int):
        if 0 <= r < limits.SHEET_ROWS:
            self.rows_h.add(r, r)

    def hide_cols(self, c1: int, c2: int):
        c1, c2 = max(c1, 0), min(c2, limits.SHEET_COLS - 1)
        if c1 <= c2:
            self.cols_h.add(c1, c2)

    # ---- output
    def _sweep(self):
        """Yield ((row, col), text, merge covering it or None) in reading order, in one row sweep."""
        for p, m in self._cover(sorted(self.cells)):
            yield p, self.cells[p], m

    def _cover(self, positions):
        """Yield ((row, col), merge covering it or None) for the sorted positions, in one row sweep.
        Merges are taken in reading order of their first cell (stored as (r1, c1, r2, c2)); one that overlaps an
        earlier one is dropped.
        Active merges never overlap, so they are disjoint in columns and one bisect finds the one over a column."""
        merges = sorted(self.merges)
        mi, starts, act, heap = 0, [], [], []  # act: merges by first column; heap: (last row, first column)

        def expire(row):
            while heap and heap[0][0] < row:
                _, c1 = heapq.heappop(heap)
                i = bisect_left(starts, c1)
                del starts[i], act[i]

        for r, c in positions:
            while mi < len(merges) and merges[mi][0] <= r:
                m = merges[mi]
                mi += 1
                expire(m[0])
                i = bisect_right(starts, m[3]) - 1  # the last active merge starting at or before this one's end
                if i >= 0 and act[i][3] >= m[1]:
                    continue
                j = bisect_left(starts, m[1])
                starts.insert(j, m[1])
                act.insert(j, m)
                heapq.heappush(heap, (m[2], m[1]))
            expire(r)
            i = bisect_right(starts, c) - 1
            hit = act[i] if i >= 0 and act[i][3] >= c else None
            yield (r, c), hit

    def blocks(self) -> list:
        origins = {}  # (row, col) -> (text, last row, last col)
        for (r, c), text, m in self._sweep():
            if m is None:
                origins[(r, c)] = (text, r, c)
            elif (r, c) == (m[0], m[1]):  # a covered cell's own text is dropped
                vr = self.rows_h.first_free(m[0], m[2])
                vc = self.cols_h.first_free(m[1], m[3])
                if vr is None or vc is None:
                    vr, vc = m[0], m[1]
                origins[(vr, vc)] = (text, m[2], m[3])
        if not origins:
            return []
        rows = sorted({r for r, _ in origins} | {v[1] for v in origins.values()})
        c0 = min(c for _, c in origins)
        c1 = max(max(c for _, c in origins), max(v[2] for v in origins.values()))
        new, pos, prev = {}, 0, None
        for r in rows:
            if prev is not None:
                gap = r - prev - 1
                if gap > _GAP:
                    self.ctx.skipped["repeat"] += 1
                    gap = _GAP
                pos += gap + 1
            new[r] = pos
            prev = r
        hidden_rows = frozenset(new[r] for r in rows if r in self.rows_h) if self.rows_h else frozenset()
        hidden_cols = (frozenset(c - c0 for c in range(c0, c1 + 1) if c in self.cols_h)
                       if self.cols_h else frozenset())
        if len(origins) == 1:
            (r, c), (text, r2, c2) = next(iter(origins.items()))
            if (r2, c2) == (r, c) and not hidden_rows and not hidden_cols:
                return [self._para(text)]
        by_row = {}
        for (r, c), v in origins.items():
            by_row.setdefault(r, []).append((c, v))
        empty = Cell([])
        out, slots = [], 0
        spans = _Spans(c1 - c0 + 1)  # the row spans of earlier rows that are still open
        for r in rows:
            nr = new[r]
            while len(out) < nr:
                out.append([])
            spans.expire(nr)
            row, at, opened = [], 0, []
            for c, (text, r2, c2) in sorted(by_row.get(r, ())):
                nc = c - c0
                row.extend([empty] * spans.uncovered(at, nc))
                rs, cs = new[r2] - nr + 1, c2 - c + 1
                row.append(Cell([self._para(text)], rs, cs))
                if rs > 1:
                    opened.append((new[r2], nc, nc + cs - 1))
                at = nc + cs
            for span in opened:  # a span covers the rows below its own, never the rest of its row
                spans.open(*span)
            slots += len(row)
            if slots > limits.GRID_SLOTS:
                self.stop("grid slots", limits.GRID_SLOTS)
                break
            out.append(row)
        return [Table(out, hidden_rows=hidden_rows, hidden_cols=hidden_cols)]

    @staticmethod
    def _para(value):
        return Para(list(value)) if isinstance(value, list) else Para(text_inlines(value))
