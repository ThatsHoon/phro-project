"""A sparse sheet-cell builder for the xls, xlsb and embedded-workbook readers.

Cells arrive in any order as (row, col, text). `Grid.blocks()` lays them out as one Table whose cost follows the
number of cells put, never the sheet's extent: rows before the first value are trimmed, a long run of empty rows
shrinks, and hidden rows and columns and merged ranges are kept as sorted intervals. The hidden-merge rule (a merge
whose origin is hidden keeps its text at its first visible slot; its extent counts toward the table) follows anydoc.
One `Budget` is shared by every sheet of a document.
"""
from bisect import bisect_right

from .. import limits
from ..model import Break, Cell, Para, Table, Text

_GAP = 64            # empty rows kept between two rows that hold values
_MERGES = 100_000    # merged ranges one sheet may keep
_BUCKET = 256        # rows per merge-index bucket
_TALL = 64           # a merge touching more buckets than this is checked against every probe


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


def _uncovered(cov, ci, a, b):
    """Columns in a..b-1 outside the sorted covered intervals cov (inclusive), scanning from index ci."""
    n = b - a
    while ci < len(cov) and cov[ci][1] < a:
        ci += 1
    j = ci
    while j < len(cov) and cov[j][0] < b:
        lo, hi = max(cov[j][0], a), min(cov[j][1], b - 1)
        if hi >= lo:
            n -= hi - lo + 1
        j += 1
    return n, ci


class Grid:
    def __init__(self, name: str, budget: Budget, ctx):
        self.name, self.budget, self.ctx = name, budget, ctx
        self.cells = {}
        self.merges = []
        self._buckets, self._tall = {}, []
        self.rows_h, self.cols_h = _Ranges(), _Ranges()
        self.cut = False
        self._merges_cut = False

    # ---- input
    def _stop(self, what, n):
        self.cut = True
        self.ctx.warnings.append(f"sheet {self.name} cut at {n} {what}")
        self.ctx.skipped["sheet_cut"] += 1
        return False

    def put(self, row: int, col: int, text: str) -> bool:
        if self.cut:
            return False
        if not text or row < 0 or col < 0:
            return True
        b = self.budget
        if row >= limits.SHEET_ROWS:
            return self._stop("rows", limits.SHEET_ROWS)
        if col >= limits.SHEET_COLS:
            return self._stop("columns", limits.SHEET_COLS)
        if b.cells <= 0:
            return self._stop("cells", b.cap_cells)
        if b.chars < len(text):
            return self._stop("characters", b.cap_chars)
        b.cells -= 1
        b.chars -= len(text)
        self.cells[(row, col)] = text
        return True

    def _near(self, r1, r2):
        """Merges that may touch rows r1..r2: those in the row buckets, plus the tall ones."""
        seen = set()
        for k in range(r1 // _BUCKET, r2 // _BUCKET + 1):
            for i in self._buckets.get(k, ()):
                if i not in seen:
                    seen.add(i)
                    yield self.merges[i]
        for i in self._tall:
            yield self.merges[i]

    def merge(self, r1: int, r2: int, c1: int, c2: int):
        if r1 > r2 or c1 > c2 or r1 < 0 or c1 < 0:
            return
        r2, c2 = min(r2, limits.SHEET_ROWS - 1), min(c2, limits.SHEET_COLS - 1)
        if r1 > r2 or c1 > c2 or (r1 == r2 and c1 == c2):
            return
        if len(self.merges) >= _MERGES:
            if not self._merges_cut:
                self._merges_cut = True
                self._stop_merges()
            return
        lo, hi = r1 // _BUCKET, r2 // _BUCKET
        if hi - lo < _TALL:
            near = self._near(r1, r2)
        else:
            near = iter(self.merges)  # rare; bounded by the merge cap
        for a1, a2, b1, b2 in near:
            if a1 <= r2 and r1 <= a2 and b1 <= c2 and c1 <= b2:
                return
        i = len(self.merges)
        self.merges.append((r1, r2, c1, c2))
        if hi - lo >= _TALL:
            self._tall.append(i)
        else:
            for k in range(lo, hi + 1):
                self._buckets.setdefault(k, []).append(i)

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

    def _covering(self, r, c):
        for m in self._near(r, r):
            if m[0] <= r <= m[1] and m[2] <= c <= m[3]:
                return m
        return None

    # ---- output
    def blocks(self) -> list:
        origins = {}  # (row, col) -> (text, last row, last col)
        for (r, c), text in self.cells.items():
            m = self._covering(r, c) if self.merges else None
            if m is None:
                origins[(r, c)] = (text, r, c)
            elif (r, c) == (m[0], m[2]):  # a covered cell's own text is dropped
                vr = self.rows_h.first_free(m[0], m[1])
                vc = self.cols_h.first_free(m[2], m[3])
                if vr is None or vc is None:
                    vr, vc = m[0], m[2]
                origins[(vr, vc)] = (text, m[1], m[3])
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
        out, active, slots = [], [], 0
        for r in rows:
            nr = new[r]
            while len(out) < nr:
                out.append([])
            active = [a for a in active if a[0] >= nr]
            cov = sorted((a[1], a[2]) for a in active)
            row, at, ci = [], 0, 0
            for c, (text, r2, c2) in sorted(by_row.get(r, ())):
                nc = c - c0
                n, ci = _uncovered(cov, ci, at, nc)
                row.extend([empty] * n)
                rs, cs = new[r2] - nr + 1, c2 - c + 1
                row.append(Cell([self._para(text)], rs, cs))
                if rs > 1:
                    active.append((new[r2], nc, nc + cs - 1))
                at = nc + cs
            slots += len(row)
            if slots > limits.GRID_SLOTS:
                self._stop("grid slots", limits.GRID_SLOTS)
                break
            out.append(row)
        return [Table(out, hidden_rows=hidden_rows, hidden_cols=hidden_cols)]

    @staticmethod
    def _para(text):
        inl = []
        for i, part in enumerate(text.split("\n")):
            if i:
                inl.append(Break())
            if part:
                inl.append(Text(part))
        return Para(inl)
