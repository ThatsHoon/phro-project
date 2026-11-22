"""A sparse sheet-cell builder for the spreadsheet readers, the embedded workbooks and the record tables.

Cells arrive in any order as (row, col, value). Hidden rows and columns and merged ranges are kept as sorted intervals.
The hidden-merge rule (a merge whose origin is hidden keeps its text at its first visible slot; its extent counts toward
the table) follows anydoc. One `Budget` is shared by every sheet of a document. The cost of laying out follows the
number of cells put, never the sheet's extent.

`Grid.sheet_blocks()` gives a sheet: its cells are split into rectangular islands on fully empty rows and columns (two
parts stay one island when the empty run between them is at most _GAP long and they span the same columns, or rows),
each a Table whose `origin` is its top-left cell and whose caption gives its range and its hidden row and column runs,
so a row's place in the table gives its address. An island of one cell is a paragraph led by its address (a merged
cell's range). The cells' formulas follow as one paragraph (`_formulas`, Decision 5): cells whose formulas share
one R1C1 pattern (`pattern`) are one entry, so a fill-down is one entry and a cell that breaks it its own. The sheet's
facts (`fact`: names, validation, tables, filters, conditional formats, pivots, protection) follow, a paragraph each
(`listed`). Where the sheet was cut a paragraph says so: the cell, the limit, and what the file declares lies past
it.
`Grid.blocks()` gives a record table (an Access table, a project's tasks, an iWork table): one Table, rows before the
first value trimmed and a long run of empty rows shrunk to _GAP.
The workbook readers share how a hyperlink lands on its cells (`Grid.link`), how note marks land on theirs
(`Grid.place_notes`) and how notes whose mark did not reach the output are dropped (`sweep`).
A workbook chart drawn from cells a shown sheet wrote keeps its caption alone (`settle`): the caption names the range,
and the sheet's own table holds the cells. `Grid.written()` is what a sheet wrote, kept after its cells are let go.
"""
import copy
import heapq
import re
from bisect import bisect_left, bisect_right, insort
from itertools import groupby
from operator import itemgetter
from urllib.parse import quote, unquote

from .. import limits
from ..markdown import blocks_len
from ..numfmt import display
from ..model import Bold, Break, Cell, Hidden, Link, NoteRef, Para, Table, Text

_GAP = 64
_MERGES = 100_000
_FIRST_ROW, _FIRST_COL, _LAST_ROW, _LAST_COL = (itemgetter(i) for i in range(4))
_FIRST_TWO = itemgetter(0, 1)


def address(row: int, col: int) -> str:
    """The A1 address of a zero-based cell."""
    s, n = "", col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return f"{s}{row + 1}"


def letters(col: int) -> str:
    return address(0, col)[:-1]


def _chars(value) -> int:
    """The characters a cell value is charged: a str's length; for inlines, every Text inside (Link, Bold and
    Hidden contents included) plus each Link URL's length; for a tuple of blocks, what they render to."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, tuple):
        return blocks_len(value)
    n, stack = 0, [value]
    while stack:
        for x in stack.pop():
            if isinstance(x, Text):
                n += len(x.text)
            elif isinstance(x, Link):
                n += len(x.url)
                stack.append(x.inlines)
            elif isinstance(x, (Bold, Hidden)):
                stack.append(x.inlines)
    return n


def shown(value, code: str, date1904: bool):
    """The value as its number format shows it. One the format shows as nothing (an empty section, as `;;;` has) is
    hidden content, its General text."""
    out = display(value, code, date1904)
    if out or value == "" or code == "General":
        return out
    return [Hidden([Text(display(value, "General", date1904))])]


def text_inlines(text: str) -> list:
    """The Text and Break pieces of a text, split on newlines."""
    if "\n" not in text:
        return [Text(text)] if text else []
    inl = []
    for i, part in enumerate(text.split("\n")):
        if i:
            inl.append(Break())
        if part:
            inl.append(Text(part))
    return inl


_REF = re.compile(
    r"""("(?:[^"]|"")*"|'(?:[^']|'')*'|\[(?:[^\[\]]|\[[^\[\]]*\])*\])"""
    r"|(?<![\w.$\]])(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]{0,6})(?![\w(!\[])"
    r"|(?<![\w.$:\]])(\$?)([A-Z]{1,3}):(\$?)([A-Z]{1,3})(?![\w(!\[$:])"
    r"|(?<![\w.$:\]])(\$?)([1-9][0-9]{0,6}):(\$?)([1-9][0-9]{0,6})(?![\w(!\[$:.])")
_MAX_ROW, _MAX_COL = 1_048_576, 16_384


def _col_no(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _part(dollar: str, value: int, base: int):
    """One coordinate of a reference: ("$", value) when absolute, else its offset from `base`."""
    return ("$", value) if dollar else value - base


def pattern(text: str, row: int, col: int) -> tuple:
    """The R1C1 pattern of a formula's A1 text at (row, col): the text between its references, and each reference as
    a tuple of its coordinates (an absolute one ("$", n), a relative one its offset from the cell). The cells of one
    fill-down or fill-right have one pattern. A reference past the sheet's last row or column stays text, and so
    does all of a string, a quoted sheet name and a bracketed part (a structured reference, a workbook index)."""
    out, at = [], 0
    for m in _REF.finditer(text):
        g = m.groups()
        if g[0] is not None:
            continue
        if g[2] is not None:
            r, c = int(g[4]) - 1, _col_no(g[2])
            if r >= _MAX_ROW or c >= _MAX_COL:
                continue
            ref = ("c", _part(g[3], r, row), _part(g[1], c, col))
        elif g[6] is not None:
            a, b = _col_no(g[6]), _col_no(g[8])
            if max(a, b) >= _MAX_COL:
                continue
            ref = ("k", _part(g[5], a, col), _part(g[7], b, col))
        else:
            a, b = int(g[10]) - 1, int(g[12]) - 1
            if max(a, b) >= _MAX_ROW:
                continue
            ref = ("r", _part(g[9], a, row), _part(g[11], b, row))
        out.append(text[at:m.start()])
        out.append(ref)
        at = m.end()
    out.append(text[at:])
    return tuple(out)


def _place(part, base: int, limit: int):
    """A coordinate's (dollar, zero-based value) at `base`; None off the sheet."""
    if isinstance(part, tuple):
        return part
    v = part + base
    return ("", v) if 0 <= v < limit else None


def formula_text(pat: tuple, row: int, col: int) -> str:
    """The A1 text of a pattern at (row, col); a reference shifted off the sheet is #REF!."""
    out = []
    for piece in pat:
        if isinstance(piece, str):
            out.append(piece)
        elif piece[0] == "c":
            r, c = _place(piece[1], row, _MAX_ROW), _place(piece[2], col, _MAX_COL)
            out.append("#REF!" if r is None or c is None else f"{c[0]}{letters(c[1])}{r[0]}{r[1] + 1}")
        else:
            limit, base, name = (_MAX_COL, col, letters) if piece[0] == "k" else (_MAX_ROW, row, _row_name)
            a, b = _place(piece[1], base, limit), _place(piece[2], base, limit)
            out.append("#REF!" if a is None or b is None else f"{a[0]}{name(a[1])}:{b[0]}{name(b[1])}")
    return "".join(out)


OPS = {"equal": "=", "notEqual": "<>", "greaterThan": ">", "lessThan": "<", "greaterThanOrEqual": ">=",
       "lessThanOrEqual": "<="}


def condition(op, f1: str, f2: str) -> str:
    """A validation or cell rule's test by its OOXML operator name (None is `between`): `between a and b`, `not
    between a and b`, or the operator and a value."""
    op = op or "between"
    if op in ("between", "notBetween"):
        return f"{'between' if op == 'between' else 'not between'} {f1} and {f2}"
    return f"{OPS.get(op, op)} {f1}"


OPERATORS = ("between", "notBetween", "equal", "notEqual", "greaterThan", "lessThan", "greaterThanOrEqual",
             "lessThanOrEqual")
VALIDATIONS = ("any", "whole", "decimal", "list", "date", "time", "textLength", "custom")
SIGNS = {1: "<", 2: "=", 3: "<=", 4: ">", 5: "<>", 6: ">="}
_RULES = {"colorScale": "color scale", "dataBar": "data bar", "iconSet": "icon set",
          "duplicateValues": "duplicate values", "uniqueValues": "unique values", "containsBlanks": "blank",
          "notContainsBlanks": "not blank", "containsErrors": "error", "notContainsErrors": "not error"}
_TEXT_RULES = {"containsText": "contains", "notContainsText": "does not contain", "beginsWith": "begins with",
               "endsWith": "ends with"}


def validation(where: str, kind: str, op, f1: str, f2: str, literal: bool) -> str:
    """A validation as `<ranges> <type> <rule>` by its OOXML type and operator names; a `literal` list is its quoted
    values (NULs between them in the binary formats become commas), another list or a custom rule a formula."""
    if kind == "list":
        rule = f1.replace("\0", ",") if literal else "=" + f1
    elif kind == "custom":
        rule = "=" + f1
    else:
        rule = condition(op, f1, f2)
    return f"{where} {kind} {rule}"


def picked(col: str, vals: list, more: int, blank: bool) -> str:
    """A filter column's chosen values (the first FACT_VALUES of them, `more` past those), blanks last."""
    vals = vals + ["(blank)"] if blank else vals
    return f"{col} is {', '.join(vals)}" + (f" and {more:,} more" if more else "")


def rule(kind: str, forms=("", ""), op=None, text: str = "", rank=10, bottom=False, percent=False, below=False,
         dev=None, period: str = "") -> str:
    """A conditional format rule in a few words by its OOXML type name: its test, never its format."""
    if kind == "cellIs":
        return "cell " + condition(op, forms[0], forms[1])
    if kind == "expression":
        return "formula =" + forms[0]
    if kind == "top10":
        return f"{'bottom' if bottom else 'top'} {rank}" + ("%" if percent else "")
    if kind == "aboveAverage":
        side = "below" if below else "above"
        return f"{dev} std dev {side} average" if dev else f"{side} average"
    if kind in _TEXT_RULES:
        return f'{_TEXT_RULES[kind]} "{text}"'
    if kind == "timePeriod":
        return f"date {period}".strip()
    return _RULES.get(kind, kind)


def ruled(rules) -> list:
    """Conditional format rules ((ranges, rule) in the file's order) as a fact's entries, `<ranges> <rule>`: the
    ranges of one rule joined, a range said once, the first FACT_VALUES of them and how many more."""
    groups = {}
    for ranges, rule in rules:
        groups.setdefault(rule, {}).update(dict.fromkeys(ranges))
    out = []
    for rule, ranges in groups.items():
        shown, more = list(ranges)[:limits.FACT_VALUES], len(ranges) - limits.FACT_VALUES
        if shown:
            out.append((", ".join(shown) + (f" and {more:,} more" if more > 0 else "") + " " + rule, False))
    return out


_PLACE_UNSAFE = re.compile(r"[\s()<>\[\]%#\\]")


def fragment(text: str) -> str:
    """A place in a file as a URL fragment: white space and the characters that end or break a Markdown link target
    percent-encoded, every other one (letters of any script) as it is."""
    return _PLACE_UNSAFE.sub(lambda m: quote(m.group()), text)


def _leaf(url: str) -> str:
    return unquote(url.replace("\\", "/").rsplit("/", 1)[-1]).casefold()


def span(r1: int, r2: int, c1: int, c2: int) -> str:
    """An inclusive zero-based range in A1 form, one cell as its address."""
    return address(r1, c1) if (r1, c1) == (r2, c2) else f"{address(r1, c1)}:{address(r2, c2)}"


def hide(blocks: list) -> list:
    """Hidden copies of a hidden sheet's blocks, so showing hidden content marks them."""
    out = []
    for b in blocks:
        c = copy.copy(b)
        c.hidden = "hidden"
        out.append(c)
    return out


def sweep(blocks: list, keys: list, notes: dict) -> int:
    """Drop from `notes` the sheet's notes (`keys`) whose mark did not reach `blocks`; how many were dropped."""
    if not keys:
        return 0
    live, stack = set(), list(blocks)
    while stack:
        x = stack.pop()
        if isinstance(x, NoteRef):
            live.add(x.id)
            continue
        for name in ("inlines", "blocks"):
            stack.extend(getattr(x, name, ()))
        for row in getattr(x, "rows", ()):
            stack.extend(row)
    gone = 0
    for k in keys:
        if k not in live and notes.pop(k, None) is not None:
            gone += 1
    return gone


def listed(label: str, entries, budget, ctx, kind: str = "fact", cap: int = 0, total=None) -> list:
    """One paragraph, `label: ` and its entries joined by `; `, or none for no entries. An entry is (text, hidden)
    or (text, hidden, units); the shown ones come first, in order, then the hidden ones, each a Hidden run (all
    hidden, the whole paragraph is). The label and each entry are charged their characters to `budget`; past them,
    or past `cap` units when one is given, the rest are counted under `kind` and the paragraph says how many (a
    label that does not fit leaves no paragraph). With `total`, `entries` is an iterator of that many entries
    already in that order, read only as far as it is listed."""
    if total is None:
        entries = sorted(entries, key=itemgetter(1))
        total = len(entries)
    if not total:
        return []
    if budget.chars < len(label) + 2:
        ctx.skipped[kind] += total
        return []
    budget.chars -= len(label) + 2
    body, units, shown, visible = [], 0, 0, 0
    entries = iter(entries)
    while not cap or units < cap:  # an entry past the cap is not asked for: asking may read it
        text, hid, *n = next(entries, (None, False))
        if text is None:
            break
        units += n[0] if n else 1
        if cap and units > cap or len(text) + 2 > budget.chars:
            break
        budget.chars -= len(text) + 2
        text = ("; " if shown else "") + text
        body.append(Hidden([Text(text)]) if hid else Text(text))
        shown += 1
        visible += not hid
    rest = total - shown
    if rest:
        ctx.skipped[kind] += rest
        body.append(Text(f"{'; ' if visible else ''}({rest:,} more not listed)"))
    if not visible and not rest:
        return [Para([Hidden([Text(f"{label}: ")] + [t for h in body for t in h.inlines])])]
    return [Para([Text(f"{label}: ")] + body)]


def _row_name(r: int) -> str:
    return str(r + 1)


def _column_major(item):
    return item[0][1], item[0][0]


def shows(value) -> bool:
    """Whether a cell's value, as the grid holds it, shows the cell's own value in the output: its first text is
    not a formula written for want of a value, and nothing of it is hidden. A link and a note mark around the text
    do not count; a value of blocks is taken as not shown."""
    if isinstance(value, str):
        return bool(value) and value[0] != "="
    if not isinstance(value, list):
        return False
    stack, first = [value], None
    while stack:
        for x in stack.pop():
            if isinstance(x, Hidden):
                return False
            if isinstance(x, (Link, Bold)):
                stack.append(x.inlines)
            elif isinstance(x, Text) and first is None and not stack:
                first = x.text
    return bool(first) and first[0] != "="


class Written:
    """What a sheet read whole wrote to the output: the columns holding a value the output shows in each row, as
    runs, its hidden rows and columns and its merged ranges. A formula with no value, written as its text, and a
    value its format hides are not such values."""

    def __init__(self, grid):
        rows = {}
        for (r, c), v in sorted(grid.cells.items()):
            if not shows(v):
                continue
            runs = rows.get(r)
            if runs is None:
                rows[r] = [(c, c)]
            elif runs[-1][1] == c - 1:
                runs[-1] = (runs[-1][0], c)
            else:
                runs.append((c, c))
        self.rows, self.rows_h, self.cols_h, self.merges = rows, grid.rows_h, grid.cols_h, grid.merges

    def holds(self, box, parts, left: list) -> bool:
        """Whether the output shows every cell of the range `box` as the sheet holds it: no hidden row or column and
        no merged range meets it, and every cell of each of `parts` holds a value. All are (r1, c1, r2, c2). The rows
        and merged ranges looked at are charged to `left[0]`; past it the answer is no."""
        r1, c1, r2, c2 = box
        if self.rows_h.meets(r1, r2) or self.cols_h.meets(c1, c2):
            return False
        left[0] -= len(self.merges) + sum(p[2] - p[0] + 1 for p in parts)
        if left[0] < 0:
            return False
        if any(m[0] <= r2 and m[2] >= r1 and m[1] <= c2 and m[3] >= c1 for m in self.merges):
            return False
        for a, b, c, d in parts:
            for r in range(a, c + 1):
                runs = self.rows.get(r)
                if not runs:
                    return False
                i = bisect_right(runs, (b, limits.SHEET_COLS)) - 1
                if i < 0 or runs[i][1] < d:
                    return False
        return True


def settle(charts, written: dict) -> None:
    """A workbook chart whose data range a shown sheet wrote whole keeps its caption alone: the caption names the
    range and the sheet's table holds the cells. `charts` are the workbook's chart Tables, `written` {the casefolded
    name of a sheet: its Grid.written(), None for one not shown or a name two sheets answer to}. The charts share
    one limits.SHEET_SCAN of rows looked at; past it they keep their tables."""
    left, seen = [limits.SHEET_SCAN], set()
    for t in charts:
        if not t.source or id(t) in seen:
            continue
        seen.add(id(t))
        sheet = written.get(t.source[0].casefold())
        if sheet is not None and sheet.holds(t.source[1], t.source[2:], left):
            t.rows = []


class Budget:
    """Cells, characters, valueless formula cells and characters of repeated formula text left for a document's
    sheets, and the XML elements its sheet, shared-string and styles parts have opened (`scan`, `strings`,
    `styles`); shared, so embedded workbooks count too."""

    def __init__(self, cells=None, chars=None):
        self.cells = self.cap_cells = limits.SHEET_CELLS if cells is None else cells
        self.chars = self.cap_chars = limits.SHEET_CHARS if chars is None else chars
        self.blank = limits.BLANK_FORMULAS
        self.copies = limits.FORMULA_COPY_CHARS
        self.scan = self.strings = self.styles = 0


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

    def meets(self, a, b) -> bool:
        """Whether an interval meets a..b."""
        self._norm()
        i = bisect_right(self.starts, b) - 1
        return i >= 0 and self.ends[i] >= a

    def within(self, a, b) -> list:
        """The intervals that meet a..b, clipped to it."""
        self._norm()
        i = max(bisect_right(self.starts, a) - 1, 0)
        out = []
        while i < len(self.raw) and self.starts[i] <= b:
            if self.ends[i] >= a:
                out.append((max(self.starts[i], a), min(self.ends[i], b)))
            i += 1
        return out

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


_REASONS = {"rows": "the {n:,}-row limit", "columns": "the {n:,}-column limit",
            "cells": "the document's {n:,}-cell limit", "characters": "the document's {n:,}-character limit",
            "scanned XML elements": "the workbook's {n:,}-element limit", "grid slots": "the {n:,}-slot table limit",
            "XML error": "damaged XML", "seconds": "the time limit"}
_SPENT = frozenset({"cells", "characters", "scanned XML elements", "seconds"})


def _drain(items):
    """The items in order, each taken out of the list as it is given."""
    items.reverse()
    pop = items.pop
    while items:
        yield pop()


def _rows_word(n: int) -> str:
    return f"{n:,} row" + ("" if n == 1 else "s")


def _runs(spans, name) -> str:
    """Intervals as `7, 12-14`, at most limits.SHEET_RUNS of them, then how many more."""
    shown = [name(a) if a == b else f"{name(a)}-{name(b)}" for a, b in spans[:limits.SHEET_RUNS]]
    more = len(spans) - len(shown)
    return ", ".join(shown) + (f" and {more:,} more" if more > 0 else "")


def _hidden_note(hr, hc) -> str:
    """`(hidden rows 2-3; hidden columns B)` for the hidden runs inside a table, or ""."""
    notes = ([f"hidden rows {_runs(hr, _row_name)}"] if hr else []) + \
            ([f"hidden columns {_runs(hc, letters)}"] if hc else [])
    return f"({'; '.join(notes)})" if notes else ""


def _split_rows(items, same_span, steps) -> list:
    """`items` (r1, c1, r2, c2), sorted by r1, cut on its empty row runs; two neighbours stay together when the run
    between them is at most _GAP rows and (with `same_span`) they cover the same columns."""
    steps[0] += len(items)
    cuts, hi = [0], items[0][2]
    for i, it in enumerate(items):
        if it[0] > hi + 1:
            cuts.append(i)
        if it[2] > hi:
            hi = it[2]
    if len(cuts) == 1:
        return [items]
    steps[0] += len(items)
    cuts.append(len(items))
    bands = []
    for a, b in zip(cuts, cuts[1:]):
        part = items[a:b]
        bands.append([a, b, part[0][0], max(map(_LAST_ROW, part)), min(map(_FIRST_COL, part)), max(map(_LAST_COL, part))])
    out, g = [], bands[0]
    for b in bands[1:]:
        if b[2] - g[3] - 1 <= _GAP and (not same_span or (b[4], b[5]) == (g[4], g[5])):
            g = [g[0], b[1], g[2], b[3], g[4], g[5]]
        else:
            out.append(items[g[0]:g[1]])
            g = b
    out.append(items[g[0]:g[1]])
    return out


def _split_cols(items, steps) -> list:
    """`items` cut on its empty column runs, as _split_rows cuts rows; each part keeps the items' order."""
    lo, hi = min(map(_FIRST_COL, items)), max(map(_LAST_COL, items))
    width = hi - lo + 1
    steps[0] += 2 * len(items) + width
    cov = [0] * (width + 1)
    for it in items:
        cov[it[1] - lo] += 1
        cov[it[3] - lo + 1] -= 1
    runs, n = [], 0
    for x in range(width):
        n += cov[x]
        if n <= 0:
            continue
        if runs and runs[-1][1] == x - 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    if len(runs) == 1:
        return [items]
    owner = [0] * width
    for k, (a, b) in enumerate(runs):
        owner[a:b + 1] = [k] * (b - a + 1)
    top, bottom = [None] * len(runs), [-1] * len(runs)
    for it in items:
        k = owner[it[1] - lo]
        if top[k] is None:
            top[k] = it[0]
        if it[2] > bottom[k]:
            bottom[k] = it[2]
    group, g = [0] * len(runs), 0
    for k in range(1, len(runs)):
        if runs[k][0] - runs[k - 1][1] - 1 > _GAP or (top[k], bottom[k]) != (top[k - 1], bottom[k - 1]):
            g += 1
        group[k] = g
    if not g:
        return [items]
    parts = [[] for _ in range(g + 1)]
    for it in items:
        parts[group[owner[it[1] - lo]]].append(it)
    return parts


class Grid:
    def __init__(self, name: str, budget: Budget, ctx):
        self.name, self.budget, self.ctx = name, budget, ctx
        self.cells = {}
        self.merges = []
        self.lead = {}  # a merge moved past hidden rows or columns: its slot -> (rows, columns) moved
        self._seen = set()
        self.rows_h, self.cols_h, self.heads = _Ranges(), _Ranges(), _Ranges()
        self.cut = False
        self._merges_cut = False
        self.why = None
        self._row = None
        self.dim = None
        self.formulas = {}
        self._patterns = {}
        self.facts = []

    # ---- input
    def stop(self, what, n, at=None):
        """End the sheet at the limit of `n` `what`: one `put` or `blocks` reached, or one the caller found that `put`
        cannot see (a field past the whole `what` budget). `at` is the (row, col) where it ends, col None when only
        the row is known. False, so `put` can return it."""
        if not self.cut:
            self.why = (what, n, at)
        self.cut = True
        self.ctx.warnings.append(f"sheet {self.name} cut at {n} {what}")
        self.ctx.skipped["sheet_cut"] += 1
        return False

    def declare(self, r1: int, r2: int, c1: int, c2: int):
        """The range the file says the sheet spans (a dimension record), zero-based and inclusive."""
        if 0 <= r1 <= r2 and 0 <= c1 <= c2:
            self.dim = (r1, r2, c1, c2)

    def put(self, row: int, col: int, value, size=None) -> bool:
        """Put a str, a list of inlines (Text, Break, Bold, Link, NoteRef, Hidden) or a tuple of blocks at a cell,
        charged `size` characters when the caller has counted them (as _chars counts). A value put over another
        replaces it, and the replaced value's cell and characters are refunded."""
        if self.cut:
            return False
        if not value or row < 0 or col < 0:
            return True
        if row != self._row:
            self._row = row
            if self.ctx.late():
                return self.stop("seconds", limits.READ_SECONDS, (row, None))
        b = self.budget
        if row >= limits.SHEET_ROWS:
            return self.stop("rows", limits.SHEET_ROWS, (row, col))
        if col >= limits.SHEET_COLS:
            return self.stop("columns", limits.SHEET_COLS, (row, col))
        old = self.cells.get((row, col))
        cells = b.cells + (old is not None)
        chars = b.chars + (_chars(old) if old is not None else 0)
        n = _chars(value) if size is None else size
        if cells <= 0:
            return self.stop("cells", b.cap_cells, (row, col))
        if chars < n:
            return self.stop("characters", b.cap_chars, (row, col))
        b.cells, b.chars = cells - 1, chars - n
        self.cells[(row, col)] = value
        return True

    def put_cols(self, row: int, c1: int, c2: int, value, size: int) -> bool:
        """Put one value of `size` characters at each of the row's columns c1..c2, as `put` puts it at each in turn;
        a run inside the sheet and the budget is charged at once."""
        b, n = self.budget, c2 - c1 + 1
        if self.cut or not value or row < 0 or c1 < 0 or row >= limits.SHEET_ROWS or c2 >= limits.SHEET_COLS:
            return all(self.put(row, c, value, size) for c in range(c1, c2 + 1))
        keys = [(row, c) for c in range(c1, c2 + 1)]
        old = [v for v in map(self.cells.get, keys) if v is not None]
        cells, chars = b.cells + len(old) - n, b.chars + sum(map(_chars, old)) - n * size
        if cells < 0 or chars < 0:
            return all(self.put(row, c, value, size) for c in range(c1, c2 + 1))
        b.cells, b.chars = cells, chars
        self.cells.update(dict.fromkeys(keys, value))
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

    def link(self, r1: int, r2: int, c1: int, c2: int, url, place) -> int:
        """Decision 9: wrap the valued cells of the range in a Link to `url`, `#place` after it (as one fragment, its spaces escaped; a URL that has a
        fragment keeps its own) when the link names a place in that file, or follow them with `→ place` for a place in this workbook (no URL, or one whose
        last part is this file's own name); a cell already linked, or holding only note marks, is left. The cost of
        the walk (`held`) is returned; past the character budget the rest keep their old values."""
        if url and place and self.ctx.name and _leaf(url) == self.ctx.name.casefold():
            url = None
        target = f"{url}#{fragment(place)}" if url and place and "#" not in url else url
        found, cost = self.held(r1, r2, c1, c2)
        for r, c in found:
            value = self.get(r, c)
            inl = text_inlines(value) if isinstance(value, str) else list(value)
            body = [x for x in inl if not isinstance(x, NoteRef)]
            if not body or any(isinstance(x, Link) for x in body):
                continue
            marks = [x for x in inl if isinstance(x, NoteRef)]
            linked = [Link(target, body)] if target else body + [Text(f" → {place}")]
            if not self.put(r, c, linked + marks):
                break
        return cost

    def place_notes(self, marks: dict, notes: dict, hidden: bool) -> bool:
        """Put the note marks ((row, col) -> (note key, characters)) after the cells. A mark on a cell that holds a
        value is appended to it (`append`, which still works once the sheet is cut); a cell that holds nothing gets
        the mark alone, which needs an uncut grid. A note is kept in `notes` only if its mark is placed and its text
        fits the character budget; otherwise it is dropped. A comment on a merged-away cell goes to the origin of
        the merge that renders (the origin's own comment first), as `covers` resolves it. A note on a hidden row or
        column (its own or its mark's), or on a hidden sheet, is hidden with its place. True when one was dropped."""
        if not marks:
            return False
        moved = self.covers(marks)
        targets: dict = {}
        for pos, (key, size) in marks.items():
            t = moved.get(pos, pos)
            targets.setdefault(t, [])
            targets[t].insert(0 if t == pos else len(targets[t]), (key, size, pos))
        order = [t for t in targets if self.get(*t) is None] + [t for t in targets if self.get(*t) is not None]
        b, cut = self.budget, False

        def hides(pos):
            return pos[0] in self.rows_h or pos[1] in self.cols_h

        for t in order:
            fit, total = [], 0
            for key, size, pos in targets[t]:
                if b.chars - total >= size:
                    fit.append((key, pos))
                    total += size
                else:
                    notes.pop(key, None)
                    cut = True
            if not fit:
                continue
            refs = [NoteRef(k) for k, _ in fit]
            ok = self.append(t[0], t[1], refs) if self.get(*t) is not None else self.put(t[0], t[1], refs)
            if ok:
                b.chars -= total
                for k, pos in fit:
                    if hidden or hides(t) or hides(pos):
                        notes[k] = hide(notes[k])
            else:
                for k, _ in fit:
                    notes.pop(k, None)
                cut = True
        return cut

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
        m = (r1, c1, r2, c2)
        if m in self._seen:
            return
        if len(self.merges) >= _MERGES:
            if not self._merges_cut:
                self._merges_cut = True
                self._stop_merges()
            return
        self._seen.add(m)
        self.merges.append(m)

    @property
    def merges_cut(self) -> bool:
        """Whether the merged ranges have reached their cap: no further one is kept."""
        return self._merges_cut

    def _stop_merges(self):
        self.ctx.warnings.append(f"sheet {self.name} cut at {_MERGES} merged ranges")
        self.ctx.skipped["sheet_cut"] += 1

    def written(self):
        """What the sheet wrote, once its blocks are made: None for a sheet that was cut, or whose merged ranges
        were."""
        return None if self.cut or self._merges_cut else Written(self)

    def hide_row(self, r: int):
        self.hide_rows(r, r)

    def hide_rows(self, r1: int, r2: int):
        r1, r2 = max(r1, 0), min(r2, limits.SHEET_ROWS - 1)
        if r1 <= r2:
            self.rows_h.add(r1, r2)

    def hide_cols(self, c1: int, c2: int):
        c1, c2 = max(c1, 0), min(c2, limits.SHEET_COLS - 1)
        if c1 <= c2:
            self.cols_h.add(c1, c2)

    def head_rows(self, r1: int, r2: int):
        """Rows whose cells are header cells."""
        r1, r2 = max(r1, 0), min(r2, limits.SHEET_ROWS - 1)
        if r1 <= r2:
            self.heads.add(r1, r2)

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

    def _items(self) -> list:
        """The cells that render as (first row, first col, last row, last col, value), sorted by row and column: a
        merge's at its first visible slot."""
        if not self.merges:
            return sorted((r, c, r, c, v) for (r, c), v in self.cells.items())
        items = []
        for (r, c), text, m in self._sweep():
            if m is None:
                items.append((r, c, r, c, text))
            elif (r, c) == (m[0], m[1]):
                vr = self.rows_h.first_free(m[0], m[2])
                vc = self.cols_h.first_free(m[1], m[3])
                if vr is None or vc is None:
                    vr, vc = m[0], m[1]
                if (vr, vc) != (m[0], m[1]):
                    self.lead[(vr, vc)] = (vr - m[0], vc - m[1])
                items.append((vr, vc, m[2], m[3], text))
        items.sort(key=_FIRST_TWO)
        return items

    def blocks(self) -> list:
        """A record table: one Table, rows before the first value trimmed and empty runs past _GAP shrunk to it;
        its caption names the hidden rows and columns inside it."""
        items = self._items()
        if not items:
            return []
        rows = sorted(set(map(_FIRST_ROW, items)) | set(map(_LAST_ROW, items)))
        c0, c1 = min(map(_FIRST_COL, items)), max(map(_LAST_COL, items))
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
        if len(items) == 1:
            r, c, r2, c2, text = items[0]
            if (r2, c2) == (r, c) and not hidden_rows and not hidden_cols:
                return self._blocks(text)
        out = self._layout(items, new.__getitem__, pos + 1, c0, c1 - c0 + 1, [0, 0])
        note = _hidden_note(self.rows_h.within(rows[0], rows[-1]) if self.rows_h else [],
                            self.cols_h.within(c0, c1) if self.cols_h else [])
        return [Table(out, caption=note, hidden_rows=hidden_rows, hidden_cols=hidden_cols)]

    def sheet_blocks(self) -> list:
        """A sheet: one Table an island in reading order (a lone cell a paragraph), then the cut paragraph, the
        formulas and the facts (FACT_ITEMS entries each)."""
        out, slots = [], [0, 0]
        for part in self._islands(self._items()):
            if slots[0] > limits.GRID_SLOTS:
                break
            out.extend(self._island(part, slots))
        line = self._cut_line()
        if line:
            out.append(Para([Text(line)]))
        out.extend(self._formulas())
        for label, entries, total in self.facts:
            out.extend(listed(label, entries, self.budget, self.ctx, cap=limits.FACT_ITEMS, total=total))
        return out

    @staticmethod
    def _islands(items) -> list:
        """`items` grouped into islands in reading order. Each split is charged its items (and a column split its
        width) against SHEET_ISLAND_STEPS; past it, a part not yet cut on its rows is cut only on its empty runs past
        _GAP, and is an island as it is."""
        if not items:
            return []
        out, steps, stack = [], [0], [(items, "")]
        while stack:
            part, done = stack.pop()
            if steps[0] > limits.SHEET_ISLAND_STEPS:
                out.extend([part] if done == "rows" else _split_rows(part, False, steps))
                continue
            for axis in ("rows", "cols"):
                if axis == done:
                    continue
                groups = _split_rows(part, True, steps) if axis == "rows" else _split_cols(part, steps)
                if len(groups) > 1:
                    stack.extend((g, axis) for g in reversed(groups))
                    break
            else:
                out.append(part)
        return out

    def _island(self, items, slots) -> list:
        r0, r1 = items[0][0], max(map(_LAST_ROW, items))
        c0, c1 = min(map(_FIRST_COL, items)), max(map(_LAST_COL, items))
        hr = self.rows_h.within(r0, r1) if self.rows_h else []
        hc = self.cols_h.within(c0, c1) if self.cols_h else []
        rng = address(r0, c0) if (r0, c0) == (r1, c1) else f"{address(r0, c0)}:{address(r1, c1)}"
        if len(items) == 1 and not hr and not hc:
            return self._addressed(rng, items[0][4])
        out = self._layout(items, lambda r: r - r0, r1 - r0 + 1, c0, c1 - c0 + 1, slots)
        note = _hidden_note(hr, hc)
        return [Table(out, caption=rng + (" " + note if note else ""),
                      hidden_rows=frozenset(x - r0 for a, b in hr for x in range(a, b + 1)),
                      hidden_cols=frozenset(x - c0 for a, b in hc for x in range(a, b + 1)),
                      origin=address(r0, c0))]

    def _addressed(self, rng, value) -> list:
        """A lone cell's blocks, its first paragraph led by its address (a merged one's range)."""
        blocks = self._blocks(value)
        if blocks and isinstance(blocks[0], Para):
            first = blocks[0]
            return [Para([Text(f"{rng}: ")] + list(first.inlines), hidden=first.hidden)] + blocks[1:]
        return [Para([Text(f"{rng}:")])] + blocks

    def fact(self, label: str, entries, total=None):
        """A paragraph of the sheet's facts after its formulas: `label` and its (text, hidden) entries, by `listed`
        (with `total`, an iterator of that many, as `listed` takes it)."""
        self.facts.append((label, entries, total))

    def table(self, name: str, r1: int, r2: int, c1: int, c2: int, heads: bool, totals: bool):
        """A table's fact: `Table <name> <range>` and its header and totals rows."""
        about = ([(f"header row {r1 + 1}", False)] if heads else []) + \
                ([(f"totals row {r2 + 1}", False)] if totals else [])
        self.fact(f"Table {name} {span(r1, r2, c1, c2)}", about or [("no header row", False)])

    def formula(self, row: int, col: int, pat: tuple, count: bool = True) -> bool:
        """Keep a cell's formula pattern (from `pattern`) for the sheet's formulas paragraph; whether it was kept. A
        cell that holds no value (its result is empty) is charged to BLANK_FORMULAS, the document's; past them it is
        counted under `formula` (unless the caller has counted it: `count`) and the sheet goes on."""
        at = (row, col)
        if at not in self.cells and at not in self.formulas:
            if self.cut or row < 0 or col < 0 or row >= limits.SHEET_ROWS or col >= limits.SHEET_COLS:
                return False
            if self.budget.blank <= 0:
                if count:
                    self.ctx.skipped["formula"] += 1
                return False
            self.budget.blank -= 1
        self.formulas[at] = self._patterns.setdefault(pat, pat)
        return True

    @property
    def formulas_cut(self) -> bool:
        """Whether a formula of a cell with no value would now be counted, not kept."""
        return self.budget.blank <= 0

    def _formulas(self) -> list:
        """Decision 5: the sheet's formulas as one paragraph: `Formulas: ` and an entry for each pattern, its ranges
        (a column run of cells, runs of one extent in neighbouring columns one range) and its A1 text at the first,
        in reading order of their first cells. Cells in hidden rows or columns make entries of their own, hidden,
        after the shown ones. Each entry is charged its characters; past FORMULA_GROUPS ranges or the characters the
        rest are counted under `formula` and the paragraph says how many (`listed`), their texts never made."""
        if not self.formulas:
            return []
        runs = {}
        for (r, c), pat in sorted(self.formulas.items(), key=_column_major):
            key = (pat, r in self.rows_h or c in self.cols_h, c)
            got = runs.get(key)
            if got and got[-1][1] == r - 1:
                got[-1][1] = r
            else:
                runs.setdefault(key, []).append([r, r])
        rects = {}
        for (pat, hid, c), spans in runs.items():
            group = rects.setdefault((pat, hid), {})
            for r1, r2 in spans:
                cols = group.setdefault((r1, r2), [])
                if cols and cols[-1][1] == c - 1:
                    cols[-1][1] = c
                else:
                    cols.append([c, c])
        entries = []
        for (pat, hid), group in rects.items():
            ranges = sorted((r1, c1, r2, c2) for (r1, r2), cols in group.items() for c1, c2 in cols)
            entries.append((hid, ranges[0][:2], pat, ranges))
        entries.sort(key=_FIRST_TWO)
        return listed("Formulas", ((", ".join(address(r1, c1) if (r1, c1) == (r2, c2)
                                              else f"{address(r1, c1)}:{address(r2, c2)}" for r1, c1, r2, c2 in ranges)
                                    + " =" + formula_text(pat, r, c), hid, len(ranges))
                                   for hid, (r, c), pat, ranges in entries),
                      self.budget, self.ctx, "formula", limits.FORMULA_GROUPS, len(entries))

    def _cut_line(self) -> str:
        """Where the sheet was cut, by what, and what the file declares past it; "" for a sheet read whole."""
        if self.why is None:
            return ""
        what, n, at = self.why
        reason = _REASONS.get(what, "the {n:,} " + what + " limit").format(n=n)
        dim = self.dim
        if not self.cells and not self.formulas and what in _SPENT:
            line = f"(Sheet not read: {reason} was used up by the sheets before it"
            if dim:
                span = f"{address(dim[0], dim[2])}:{address(dim[1], dim[3])}"
                line += f"; it spans {span}, {_rows_word(dim[1] - dim[0] + 1)}"
            return line + ")"
        if at is None:
            where = ""
        elif at[1] is None:
            where = f" at row {at[0] + 1:,}"
        else:
            where = f" at {address(*at)}"
        if dim and at is not None and dim[1] >= at[0]:
            rest = f"not read from there to {address(dim[1], dim[3])}, {_rows_word(dim[1] - at[0] + 1)}"
        else:
            rest = "not read from there on"
        return f"(Sheet cut{where} by {reason}; {rest})"

    def _layout(self, items, new, nrows, c0, ncols, slots) -> list:
        """The `nrows` table rows of `items` (sorted (first row, first col, last row, last col, value)), a sheet row
        placed at `new(row)`. `slots` is [what the sheet's tables lay out, the area their spans cover]: each row, cell
        and filler cell counts in the first, and past GRID_SLOTS the sheet is cut; a span that would take the second
        past GRID_SLOTS is laid out as one cell, as the renderer lays out a table's. `items` is emptied as it is laid
        out, so the cells are let go as the rows are made. A tuple value's cells of one span and kind are one Cell."""
        empty, head_empty = Cell([]), Cell([], header=True)
        heads = self.heads
        made = {}
        out = []
        spans = _Spans(ncols)
        for r, group in groupby(_drain(items), _FIRST_ROW):
            nr = new(r)
            spans.expire(nr)
            head = bool(heads) and r in heads
            gap = head_empty if head else empty
            row, at, opened = [], 0, []
            for _, c, r2, c2, value in group:
                nc = c - c0
                if nc > at:
                    row.extend([gap] * (spans.uncovered(at, nc) if spans.starts else nc - at))
                rs, cs = new(r2) - nr + 1, c2 - c + 1
                if rs > 1 or cs > 1:
                    if slots[1] + rs * cs > limits.GRID_SLOTS:
                        rs = cs = 1
                    else:
                        slots[1] += rs * cs
                lead = self.lead.get((r, c), (0, 0)) if self.lead else (0, 0)
                if isinstance(value, tuple):
                    key = (id(value), rs, cs, head, lead)
                    cell = made.get(key)
                    if cell is None:
                        cell = made[key] = Cell(list(value), rs, cs, head, lead)
                else:
                    cell = Cell(self._blocks(value), rs, cs, head, lead)
                row.append(cell)
                if rs > 1:
                    opened.append((nr + rs - 1, nc, nc + cs - 1))
                at = nc + cs
            for span in opened:
                spans.open(*span)
            slots[0] += nr - len(out) + len(row)
            if slots[0] > limits.GRID_SLOTS:
                self.stop("grid slots", limits.GRID_SLOTS, (r, None))
                return out
            out.extend([] for _ in range(nr - len(out)))
            out.append(row)
        slots[0] += nrows - len(out)
        if slots[0] > limits.GRID_SLOTS:
            self.stop("grid slots", limits.GRID_SLOTS)
            return out
        out.extend([] for _ in range(nrows - len(out)))
        return out

    @staticmethod
    def _blocks(value) -> list:
        if isinstance(value, tuple):
            return list(value)
        return [Para(list(value)) if isinstance(value, list) else Para(text_inlines(value))]
