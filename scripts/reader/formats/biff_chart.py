"""BIFF8 chart substreams (chart sheets and charts embedded in worksheets) as Tables.

The substream walk, the Begin/End context rule that binds a record to the Series or Text it follows, the BRAI
roles, the SIIndex caches, the title from a Text with ObjectLink 1 and the chart kinds were checked against dochan
`office_binary/xls_chart.py`. The table is built by `charts.series_table`. A BRAI reference reads the already
displayed cells of its sheet; a series with cache points reads those instead, formatted with the BRAI's ifmt.
Every count from the file is capped: 256 series, 100,000 points, nesting 64 deep, point rows `charts.CHART_ROWS`.
A reference visits at most min(area, cells of its sheet) cells, so a whole-sheet area costs nothing extra.
A substream that has no EOF, or holds a worksheet-only record, is one chart counted under `chart`.
`graph` reads an MS Graph 97 Workbook stream (BOF vers 0x0680, dt 0x8000) with the same record handling: its chart
records give the series, the kind and the title, but their numbers are not cached: the datasheet cells of the same
substream (NUMBER 3 and LABEL 4, Graph's own ids) hold them, a BRAI's last word being the datasheet row or column.
The orientation after dochan `office_binary/ole_objects.py` `_graph_chart`: 0x1055 says whether the series are in rows,
a series' name is the datasheet cell at point 0 of its axis, its values the points after that, and the categories
the points of the axis a role 2 BRAI names; selections 0x1053 / 0x1054 stop the rows and columns that count, and a
Series whose point counts differ from them is a ReadError, as in dochan. The labels' codepage is the first CODEPAGE
anywhere in the stream.
"""
import struct

from .. import limits
from ..charts import CHART_ROWS, series_table
from ..numfmt import code_for, display
from . import biff

BEGIN, END = 0x1033, 0x1034
MAX_SERIES = 256
MAX_POINTS = 100_000
MAX_TEXTS = 256
_SERIES, _TEXT, _SERIES_TEXT, _OBJECT_LINK, _BRAI, _SIINDEX = 0x1003, 0x1025, 0x100D, 0x1027, 0x1051, 0x1065
_NUMBER, _LABEL, _BOOLERR = 0x0203, 0x0204, 0x0205
_BAR, _PIE, _SCATTER, _CHART3D = 0x1017, 0x1019, 0x101B, 0x103A
_SER_PARENT = 0x104A
_DS_NUMBER, _DS_LABEL, _DS_ORIENT, _DS_ROWS, _DS_COLS = 0x0003, 0x0004, 0x1055, 0x1053, 0x1054
_GRAPH_BOF = 0x8000
_CODEPAGE = 0x0042
_KINDS = {0x1018: "line", 0x101A: "area", 0x103E: "radar", 0x103F: "surface", 0x1040: "radar"}
# these cannot belong to a chart: BLANK is not here, real charts hold it after SIIndex
_SHEET_ONLY = frozenset({0x00FD, 0x027E, 0x00BD, 0x0006, 0x0208, 0x00BE})


class _Series:
    def __init__(self):
        self.name = None
        self.refs = {}  # BRAI role -> (source, tokens, ifmt)
        self.aux = False  # carries SerParent: a trendline or error bar, not a data series
        self.idx = {}  # graph only: BRAI role -> datasheet row or column
        self.counts = None  # graph only: the Series record's point counts (cValx, cValy)


class _Text:
    def __init__(self):
        self.link = 0
        self.text = ""
        self.tokens = None  # a BRAI reference to a cell


class _Datasheet:
    """What an MS Graph substream holds beside its chart records."""

    def __init__(self):
        self.cells = {}      # (row, col) -> float | str
        self.by_rows = None  # 0x1055: the series are in rows
        self.codepage = None  # the first CODEPAGE 0x0042 of the stream: the encoding of the narrow labels
        self.stop = {}       # 0x1053 / 0x1054 -> the rows / columns that count


class _Found:
    """What one walk over a chart substream collected."""
    series: list
    texts: list
    kinds: list
    cache: dict
    points: int = 0
    cut_series = cut_points = three_d = False
    end = None
    stop = 0


def chart(data: bytes, start: int, g: biff.Globals, cells: dict, ctx, st: biff.Stream | None = None):
    """The chart substream whose BOF is at `start`: (its Table or None, the offset after its EOF). `cells` maps a
    sheet index to {(row, col): displayed text}; a sheet it lacks has no cells. A substream that cannot be read is
    counted under `chart` and the offset is where the walk stopped. `st` is the workbook's Stream over `data`, so
    worksheets and charts share one `limits.BIN_RECORDS`; without it the chart gets a Stream of its own."""
    st = biff.Stream(data, ctx) if st is None else st
    f = _scan(data, start, st, ctx, biff.CHART_BOF)
    if f.end is None:
        ctx.skipped["chart"] += 1
        return None, f.stop
    series, texts, kinds, cache, end = f.series, f.texts, f.kinds, f.cache, f.end
    left = [MAX_POINTS - f.points]
    cuts = [f.cut_points]
    out, cats = [], {}
    for k, s in enumerate(series):
        got = {r: _values(s, r, (k, r) in cache and cache[k, r], g, cells, left, cuts) for r in (0, 1, 2)}
        if not cats and got[2] and not s.aux:  # a trendline's categories are not the chart's
            cats = got[2]
        name = s.name or (got[0][min(got[0])] if got[0] else "") or f"Series {k + 1}"
        if not s.aux:  # k stays the cache column of the series after it
            out.append((name, got[1]))
    n = max([max(d) + 1 for d in [cats] + [v for _, v in out] if d] + [0])
    table = series_table(_caption(kinds, f.three_d, _title(texts, g, cells, left, cuts)),
                         [cats.get(i, "") for i in range(n)],
                         [(name, [v.get(i, "") for i in range(n)]) for name, v in out], f.cut_series, cuts[0], ctx)
    return table, end


def _scan(data: bytes, start: int, st: biff.Stream, ctx, dt: int, ds: _Datasheet | None = None) -> _Found:
    """One walk over the substream whose BOF (of type `dt`) is at `start`. With `ds` (MS Graph) the datasheet cells,
    orientation and selections go to it and each BRAI keeps its index. A damaged datasheet record is a ReadError."""
    series, texts, kinds = [], [], []
    stack = [None]       # the context (a _Series or a _Text) in force at each Begin depth
    pending = None
    cache = {}           # (series, role) -> {row: float | str}
    role, points, depth, end, stop = 0, 0, 0, None, len(data)
    cut_series = cut_points = False
    three_d = False
    for rec in st.records(start):
        rid, off, size = rec[0], rec[1], rec[2]
        if rid == biff.BOF:
            if depth == 0 and (size < 4 or struct.unpack_from("<H", data, off + 2)[0] != dt):
                stop = off - 4
                break
            depth += 1
            if depth > limits.MAX_DEPTH:
                break
            continue
        if depth == 0:
            stop = off - 4
            break
        if rid == biff.EOF:
            depth -= 1
            if depth == 0:
                end = off + size
                break
            continue
        if rid in _SHEET_ONLY:
            stop = off - 4
            break
        if ds is not None and rid == _CODEPAGE and size >= 2 and ds.codepage is None:
            ds.codepage = struct.unpack_from("<H", data, off)[0]  # the first in the stream, at any nesting depth
            continue
        if depth != 1:
            continue
        if rid == BEGIN:
            if len(stack) > limits.MAX_DEPTH:
                break
            stack.append(pending if pending is not None else stack[-1])
            pending = None
            continue
        if rid == END:
            if len(stack) > 1:
                stack.pop()
            pending = None
            continue
        here, follows, pending = stack[-1], pending, None
        try:
            if rid == _SERIES:
                item = _Series()
                if size >= 8:
                    item.counts = struct.unpack_from("<HH", data, off + 4)
                if len(series) < MAX_SERIES:
                    series.append(item)
                else:
                    cut_series = True
                pending = item
            elif rid == _TEXT:
                item = _Text()
                if len(texts) < MAX_TEXTS:
                    texts.append(item)
                pending = item
            elif rid == _SERIES_TEXT and size >= 4:
                cur = biff.Cursor(data, [(off + 2, size - 2)] + list(rec[3]))  # a CONTINUE carries the rest
                n, flags = cur.u8(), cur.u8()
                value = cur.chars(n, bool(flags & 1))
                if isinstance(here, _Series):
                    here.name = value
                elif isinstance(here, _Text):
                    here.text = value
            elif rid == _OBJECT_LINK and size >= 2 and isinstance(here, _Text):
                here.link = struct.unpack_from("<H", data, off)[0]
            elif rid == _BRAI and size >= 8:
                brole, source, _flags, ifmt, cce = struct.unpack_from("<BBHHH", data, off)
                if ds is not None and isinstance(here, _Series):
                    here.idx[brole] = cce  # not a formula length: the datasheet row or column
                if cce <= size - 8:
                    tokens = data[off + 8:off + 8 + cce]
                    if isinstance(here, _Series):
                        here.refs[brole] = (source, tokens, ifmt)
                    elif isinstance(here, _Text) and source == 2:
                        here.tokens = tokens
            elif rid == _SIINDEX and size >= 2:
                role = struct.unpack_from("<H", data, off)[0]
            elif rid in (_NUMBER, _LABEL, _BOOLERR) and role in (1, 2) and size >= 6:
                row, col = struct.unpack_from("<HH", data, off)
                if col >= len(series):
                    continue
                if row >= CHART_ROWS or points >= MAX_POINTS:
                    cut_points = True
                    continue
                if rid == _NUMBER:
                    value = struct.unpack_from("<d", data, off + 6)[0] if size >= 14 else None
                elif rid == _LABEL:
                    value = biff.Cursor(data, [(off + 6, size - 6)] + list(rec[3])).string()
                else:
                    value = None if size < 8 else "#N/A" if data[off + 7] else ("TRUE" if data[off + 6] else "FALSE")
                if value is not None:
                    cache.setdefault((col, role), {})[row] = value
                    points += 1
            elif ds is not None and rid in (_DS_NUMBER, _DS_LABEL):
                if len(ds.cells) >= MAX_POINTS:
                    cut_points = True
                else:
                    _datasheet_cell(ds, rid, data, off, size)
            elif ds is not None and rid == _DS_ORIENT and size >= 6:
                ds.by_rows = bool(data[off])
            elif ds is not None and rid in (_DS_ROWS, _DS_COLS) and size == 4:
                # dochan refuses a selection whose first is not 0, whose stop is under 1 or that is given twice (the
                # object is then unread). Looser here on purpose: the last one given counts as it stands, and a stop
                # of 0 keeps no row or column; what a damaged selection does to the table is bounded by the point
                # count check in graph(), which refuses a Series that disagrees with it as dochan does
                ds.stop[rid] = struct.unpack_from("<HH", data, off)[1]
            elif rid == _SER_PARENT:
                # it marks the Series it follows, one written without a Begin included, else the enclosing one
                owner = follows if isinstance(follows, _Series) else here
                if isinstance(owner, _Series):
                    owner.aux = True
            elif rid == _CHART3D:
                three_d = True
            else:
                kind = _kind(rid, data, off, size)
                if kind and kind not in kinds:
                    kinds.append(kind)
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1
    found = _Found()
    found.series, found.texts, found.kinds, found.cache, found.points = series, texts, kinds, cache, points
    found.cut_series, found.cut_points, found.three_d = cut_series, cut_points, three_d
    found.end, found.stop = end, stop
    return found


def _datasheet_cell(ds: _Datasheet, rid: int, data: bytes, off: int, size: int) -> None:
    """A NUMBER (row, col, 3 attribute bytes, f64) or LABEL (the same, a u16 size, a narrow string) cell of the
    datasheet. The narrow strings use the datasheet's codepage."""
    if size < (15 if rid == _DS_NUMBER else 9):
        raise limits.ReadError("damaged", "truncated MS Graph datasheet cell")
    row, col = struct.unpack_from("<HH", data, off)
    if rid == _DS_NUMBER:
        ds.cells[row, col] = struct.unpack_from("<d", data, off + 7)[0]
        return
    n = struct.unpack_from("<H", data, off + 7)[0]
    if size != 9 + n:
        raise limits.ReadError("damaged", "MS Graph datasheet label size")
    ds.cells[row, col] = bytes(data[off + 9:off + 9 + n])  # decoded once the walk has found the codepage


def _label(raw: bytes, codepage: int) -> str:
    """A narrow label as dochan: ASCII as it is, 65001 as UTF-8, else the Windows page cp<N>; a page Python has no
    codec for, or bytes that do not decode, are unsupported (the caller counts the chart once)."""
    codec = "ascii" if raw.isascii() else "utf-8" if codepage == 65001 else f"cp{codepage}"
    try:
        return raw.decode(codec)
    except (LookupError, UnicodeError):
        raise limits.ReadError("unsupported", "MS Graph label codepage") from None


def graph(data: bytes, ctx):
    """The Table of an MS Graph 97 `Workbook` stream, or None when it has no series. A stream that is not BOF vers
    0x0680, has no dt 0x8000 substream, no EOF or no orientation, or whose datasheet cell is cut short, is a
    ReadError (damaged or unsupported), counted as one chart by the caller."""
    st = biff.Stream(data, ctx)
    start = None
    ds = _Datasheet()
    for rec in st.records(0):
        if rec[0] == _CODEPAGE and rec[2] >= 2 and ds.codepage is None:  # the first one anywhere wins, as dochan
            ds.codepage = struct.unpack_from("<H", data, rec[1])[0]
        elif rec[0] == biff.BOF and rec[2] >= 4:
            vers, dt = struct.unpack_from("<HH", data, rec[1])
            if vers != 0x0680:
                raise limits.ReadError("unsupported", "not an MS Graph workbook")
            if dt == _GRAPH_BOF:
                start = rec[1] - 4
                break
    if start is None:
        raise limits.ReadError("damaged", "no MS Graph datasheet substream")
    f = _scan(data, start, st, ctx, _GRAPH_BOF, ds)
    if f.end is None:
        raise limits.ReadError("damaged", "unterminated MS Graph substream")
    if ds.codepage is None:  # the first CODEPAGE anywhere in the stream: one after the substream's EOF counts too
        for rec in st.records(f.end):
            if rec[0] == _CODEPAGE and rec[2] >= 2:
                ds.codepage = struct.unpack_from("<H", data, rec[1])[0]
                break
    if ds.by_rows is None:
        raise limits.ReadError("damaged", "MS Graph datasheet orientation missing")
    rows_stop, cols_stop = ds.stop.get(_DS_ROWS), ds.stop.get(_DS_COLS)
    series_stop, point_stop = (rows_stop, cols_stop) if ds.by_rows else (cols_stop, rows_stop)
    indexed, heads = {}, {}  # axis -> {point: value}; axis -> its point 0, the label
    cuts = [f.cut_points]
    for (row, col), value in ds.cells.items():
        if isinstance(value, bytes):
            value = _label(value, 1252 if ds.codepage is None else ds.codepage)
        if (rows_stop is not None and row >= rows_stop) or (cols_stop is not None and col >= cols_stop):
            continue
        axis, point = (row, col) if ds.by_rows else (col, row)
        if point == 0:
            heads[axis] = value
        elif point > CHART_ROWS:
            cuts[0] = True
        else:
            indexed.setdefault(axis, {})[point - 1] = value
    code = code_for(0, {})

    def text(v):
        return display(v, code) if isinstance(v, float) else v

    out, cats = [], {}
    for k, s in enumerate(f.series):
        axis = s.idx.get(1)
        if s.aux or axis is None or (series_stop is not None and axis >= series_stop):
            continue  # a Series left over after its axis was deselected, or a trendline
        if point_stop is not None and s.counts is not None and any(n != point_stop - 1 for n in s.counts):
            raise limits.ReadError("damaged", "MS Graph Series point count does not match its selection")
        if not cats and s.idx.get(2) is not None:
            cats = {i: text(v) for i, v in indexed.get(s.idx[2], {}).items()}
        label = heads.get(s.idx.get(0, axis), heads.get(axis, ""))
        out.append((s.name or text(label) or f"Series {k + 1}", {i: text(v) for i, v in indexed.get(axis, {}).items()}))
    n = max([max(d) + 1 for d in [cats] + [v for _, v in out] if d] + [0])
    left = [MAX_POINTS - f.points]
    return series_table(_caption(f.kinds, f.three_d, _title(f.texts, biff.Globals(), {}, left, cuts)),
                        [cats.get(i, "") for i in range(n)],
                        [(name, [v.get(i, "") for i in range(n)]) for name, v in out], f.cut_series, cuts[0], ctx)


def _kind(rid: int, data: bytes, off: int, size: int) -> str:
    if rid == _BAR:
        return "bar"
    if rid == _PIE and size >= 4:
        return "doughnut" if struct.unpack_from("<H", data, off + 2)[0] else "pie"
    if rid == _SCATTER and size >= 6:
        return "bubble" if struct.unpack_from("<H", data, off + 4)[0] & 1 else "scatter"
    return _KINDS.get(rid, "")


def _caption(kinds: list, three_d: bool, title: str) -> str:
    kind = "+".join("bar3D" if three_d and k == "bar" else k for k in kinds) or "unknown"
    return f"Chart: {kind} — {title}" if title else f"Chart: {kind}"


def _title(texts: list, g, cells, left, cuts) -> str:
    for t in texts:
        if t.link == 1:
            if t.text:
                return t.text
            if t.tokens:
                got = _reference(t.tokens, g, cells, left, cuts)
                if got:
                    return got[min(got)]
    return ""


def _values(s: _Series, role: int, cached, g, cells, left, cuts) -> dict:
    """The point texts of one series role: its SIIndex cache when it has points, else its BRAI reference."""
    source, tokens, ifmt = s.refs.get(role, (0, b"", 0))
    if cached:
        if role == 0:
            return {}
        code = code_for(ifmt, g.formats)
        return {r: display(v, code, g.date1904) if isinstance(v, float) else v for r, v in cached.items()}
    return _reference(tokens, g, cells, left, cuts) if source == 2 and tokens else {}


def _reference(tokens: bytes, g, cells: dict, left: list, cuts: list) -> dict:
    """{point index: text} of a Ref3d or Area3d token's cells, the index counting along the rows of the area."""
    if len(tokens) < 7 or tokens[0] < 0x20 or ((tokens[0] & 0x1F) | 0x20) not in (0x3A, 0x3B):
        return {}
    if (tokens[0] & 0x1F) == 0x1A and len(tokens) == 7:  # Ref3d: sheet, row, column
        xti, r1, c1 = struct.unpack_from("<HHH", tokens, 1)
        r2, c2 = r1, c1 & 0xFF
    elif (tokens[0] & 0x1F) == 0x1B and len(tokens) == 11:  # Area3d: sheet, rows, columns
        xti, r1, r2, c1, c2 = struct.unpack_from("<HHHHH", tokens, 1)
        c2 &= 0xFF
    else:
        return {}
    c1 &= 0xFF
    if xti >= len(g.externs) or g.externs[xti] < 0 or r2 < r1 or c2 < c1:
        return {}
    sheet = cells.get(g.externs[xti]) or {}
    width = c2 - c1 + 1
    area = (r2 - r1 + 1) * width
    visits = min(area, len(sheet))
    if visits > left[0]:
        cuts[0] = True
        return {}
    left[0] -= visits
    out = {}
    if area <= len(sheet):
        found = ((r, c, sheet.get((r, c))) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1))
    else:
        found = ((r, c, v) for (r, c), v in sheet.items() if r1 <= r <= r2 and c1 <= c <= c2)
    for r, c, text in found:
        if text:
            i = (r - r1) * width + c - c1
            if i >= CHART_ROWS:
                cuts[0] = True
            else:
                out[i] = text
    return out
