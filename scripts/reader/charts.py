"""DrawingML chart caches (c:chartSpace) to a Table, shared by hwpx and OOXML, and the caption, kind and data range
words every chart reader (DrawingML, BIFF, HWP, ODF, iWork) shares."""
import math
import re

from . import limits
from .containers.zipx import attr, first, kids, local, walk
from .model import Cell, Para, Table, Text
from .numfmt import display

_CACHES = ("strCache", "numCache", "strLit", "numLit", "multiLvlStrCache")
_NUMERIC = ("numCache", "numLit")
_AXES = ("catAx", "valAx", "dateAx", "serAx")
_GROUPING = {"stacked": "stacked", "percentStacked": "percent"}
_REF = re.compile(r"(?:'((?:[^']|'')+)'|([^'!\[\]()|,:]+))!\$?([A-Za-z]{1,3})\$?(\d{1,7})"
                  r"(?::\$?([A-Za-z]{1,3})\$?(\d{1,7}))?")
_BARE = re.compile(r"[^\W\d][\w.]*")
_CELL = re.compile(r"[A-Za-z]{1,3}\d+|[Rr]\d*[Cc]\d*")
CHART_ROWS = 10_000


def kind(family: str, horizontal: bool = False, grouping: str = "", three_d: bool = False) -> str:
    """A chart kind in the words every reader uses: `column` and `bar` for vertical and horizontal bars, after
    `stacked ` or `100% stacked ` (grouping "stacked" or "percent") and `3-D `."""
    name = ("bar" if horizontal else "column") if family == "bar" else family
    head = {"stacked": "stacked ", "percent": "100% stacked "}.get(grouping, "")
    return head + ("3-D " if three_d else "") + name


def caption(kinds: str, title: str = "", axes=(), data: str = "") -> str:
    """`Chart: <kind> — <title> (x: <title>; y: <title>)`, then `. Data: <range>` when the chart names the cells it
    is drawn from. `axes` are (letter, title) pairs, x the horizontal axis, y the vertical one, z the depth; a
    second axis of a letter is `y2`."""
    out = f"Chart: {kinds}" if kinds else "Chart"
    if title:
        out += f" — {title}"
    named, seen = [], {}
    for letter, text in sorted(axes, key=lambda a: "xyz".index(a[0])):
        if text:
            seen[letter] = seen.get(letter, 0) + 1
            named.append(f"{letter}{seen[letter] if seen[letter] > 1 else ''}: {text}")
    if named:
        out += " (" + "; ".join(named) + ")"
    if data:
        out += f". Data: {data}"
    return out


def _a1(r: int, c: int) -> str:
    letters = ""
    c += 1
    while c:
        c, m = divmod(c - 1, 26)
        letters = chr(65 + m) + letters
    return f"{letters}{r + 1}"


def _sheet(name: str) -> str:
    if _BARE.fullmatch(name) and not _CELL.fullmatch(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _block(vals, names, cat):
    """[(top, left, bottom, right)] of the block whose columns are the series: values one column each, side by side,
    each name right above its values, the categories in the columns right before the first. After the block come its
    parts that hold a value in every cell: the values, the names, the categories' last column. None otherwise."""
    _, r1, c1, r2, _ = vals[0]
    for k, v in enumerate(vals):
        if v[1:] != (r1, c1 + k, r2, c1 + k):
            return None
    right = c1 + len(vals) - 1
    top, left, parts = r1, c1, [(r1, c1, r2, right)]
    if names[0] is not None:
        for k, n in enumerate(names):
            if n[1:] != (r1 - 1, c1 + k, r1 - 1, c1 + k):
                return None
        top = r1 - 1
        parts.append((top, c1, top, right))
    if cat is not None:
        if (cat[1], cat[3], cat[4]) != (r1, r2, c1 - 1):
            return None
        left = cat[2]
        parts.append((r1, c1 - 1, r2, c1 - 1))
    return [(top, left, r2, right)] + parts


def data_block(series):
    """The chart data range Excel would show, when every series' values, and all or none of the names and the
    categories, are ranges of one sheet that make one block laid out as a chart data range; else None. It is
    (sheet, whether each series is a row, the range, then its parts that hold a value in every cell: the values, the
    names, the innermost categories), each (r1, c1, r2, c2), zero-based and inclusive.
    `series` holds (name, categories, values) per series: None when the chart has none, False for a literal or a
    reference that is not one range here, else (sheet, r1, c1, r2, c2), zero-based and inclusive."""
    if not series:
        return None
    names, cats, vals = (list(x) for x in zip(*series))
    if None in vals or False in vals or False in names or False in cats or len(set(cats)) > 1:
        return None
    if None in names and any(n is not None for n in names):
        return None
    sheet = vals[0][0]
    if any(r[0] != sheet for r in vals + names + cats if r):
        return None
    for by_rows in (False, True):
        def turn(r):
            return (r[0], r[2], r[1], r[4], r[3]) if r and by_rows else r
        boxes = _block([turn(v) for v in vals], [turn(n) for n in names], turn(cats[0]))
        if boxes is not None:
            return (sheet, by_rows, *((b[1], b[0], b[3], b[2]) if by_rows else b for b in boxes))
    return None


def data_range(block) -> str:
    """A data_block as a caption names it: `Sheet1!A1:C9`, `, series in rows` when each series is a row; "" for
    None."""
    if block is None:
        return ""
    sheet, by_rows, (r1, c1, r2, c2) = block[:3]
    a, b = _a1(r1, c1), _a1(r2, c2)
    return f"{_sheet(sheet)}!{a if a == b else a + ':' + b}" + (", series in rows" if by_rows else "")


def source(block) -> tuple:
    """A data_block as a chart's Table.source holds it."""
    return () if block is None else (block[0], *block[2:])


def _kid(el, name):
    k = kids(el, name)
    return k[0] if k else None


def _val(el, name, default=None):
    k = _kid(el, name) if el is not None else None
    return attr(k, "val", default) if k is not None else None


def _cache(el):
    """The first cache/literal element under `el`, or None."""
    if el is None:
        return None
    for e in el.iter():
        if e is not el and local(e.tag) in _CACHES:
            return e
    return None


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _fmt(text: str, code: str, date1904: bool) -> str:
    text = text.strip()
    try:
        v = float(text)
    except ValueError:
        return text[:-2] if text.endswith(".0") else text
    if not math.isfinite(v):
        return text
    return display(v, code or "General", date1904)


def _points(cache, numeric: bool, date1904: bool):
    """({idx: text}, rows its points reach, clamped) of one cache or one c:lvl.

    The row count comes from the points present, never from ptCount, which a file can set to anything.
    """
    fc = _kid(cache, "formatCode")
    code = (fc.text or "").strip() if fc is not None else ""
    top = 0
    clamped = False
    out = {}
    for pt in kids(cache, "pt"):
        idx = _int(attr(pt, "idx", ""), -1)
        v = first(pt, "v")
        if idx < 0 or v is None:
            continue
        if idx >= CHART_ROWS:
            clamped = True
            continue
        text = v.text or ""
        out[idx] = _fmt(text, (attr(pt, "formatCode") or code).strip(), date1904) if numeric else text.strip()
        top = max(top, idx + 1)
    return out, top, clamped


def _levels(holder, date1904: bool):
    """([{idx: text}] outermost level first, rows they reach, clamped) of the cache under a c:cat / c:val / c:xVal /
    c:yVal: one level, or a multi-level category's levels."""
    cache = _cache(holder)
    if cache is None:
        return [], 0, False
    if local(cache.tag) != "multiLvlStrCache":
        got, top, clamped = _points(cache, local(cache.tag) in _NUMERIC, date1904)
        return [got], top, clamped
    out, top, clamped = [], 0, False
    for lvl in reversed(kids(cache, "lvl")[:limits.GRID_COLS - 1]):
        got, t, c = _points(lvl, False, date1904)
        out.append(got)
        top, clamped = max(top, t), clamped or c
    return out, top, clamped


def _name(ser, n: int) -> str:
    tx = _kid(ser, "tx")
    v = first(tx, "v") if tx is not None else None
    return ((v.text or "").strip() if v is not None else "") or f"Series {n}"


def _title(el) -> str:
    """The text a c:title shows: its rich text, paragraphs joined, or the cached text of its reference."""
    tx = _kid(el, "tx") if el is not None else None
    if tx is None:
        return ""
    rich = _kid(tx, "rich")
    if rich is not None:
        text = " ".join("".join(t.text or "" for t in walk(p, "t")) for p in kids(rich, "p"))
    else:
        v = first(tx, "v")
        text = (v.text or "") if v is not None else ""
    return " ".join(text.split())


def _kind(ch, base: str) -> str:
    three = base.endswith("3D")
    base = base[:-2] if three else base
    if base == "ofPie":
        return ("bar" if _val(ch, "ofPieType") == "bar" else "pie") + " of pie"
    horizontal = base == "bar" and _val(ch, "barDir", "col") == "bar"
    return kind(base, horizontal, _GROUPING.get(_val(ch, "grouping") or "", ""), three)


def _axes(plot) -> list:
    """(letter, title) of each shown axis that has a title, by where it is drawn: bottom or top x, left or right y."""
    out = []
    for ax in plot:
        n = local(ax.tag)
        if n not in _AXES or (_val(ax, "delete", "1") or "0") not in ("0", "false"):
            continue
        text = _title(_kid(ax, "title"))
        if not text:
            continue
        pos = _val(ax, "axPos")
        letter = ("z" if n == "serAx" else "x" if pos in ("b", "t") else "y" if pos in ("l", "r")
                  else "y" if n == "valAx" else "x")
        out.append((letter, text))
    return out


def _ref(holder):
    """None without `holder`, else the range its c:f names as data_block takes it, or False for a literal or a
    formula that is not one range."""
    if holder is None:
        return None
    f = next((e for e in holder.iter() if local(e.tag) == "f"), None)
    m = _REF.fullmatch((f.text or "").strip()) if f is not None else None
    if m is None:
        return False
    sheet = m.group(1).replace("''", "'") if m.group(1) is not None else m.group(2)
    r1, r2 = int(m.group(4)) - 1, int(m.group(6) or m.group(4)) - 1
    c1, c2 = (_column(s) for s in (m.group(3), m.group(5) or m.group(3)))
    if min(r1, r2) < 0:
        return False
    return sheet, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _column(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _ranges(ser):
    if _kid(ser, "bubbleSize") is not None:
        return None, None, False
    xy = _kid(ser, "val") is None
    return _ref(_kid(ser, "tx")), _ref(_kid(ser, "xVal" if xy else "cat")), _ref(_kid(ser, "yVal" if xy else "val"))


def _cell(text: str, header=False) -> Cell:
    return Cell([Para([Text(text)])] if text else [], header=header)


def chart_table(space, ctx=None, workbook=False):
    """The chart's cached values as a Table, or None without series. A table is held to limits.CHART_CELLS cells
    (series first, then rows); a cut is counted in ctx.skipped["chart_cut"] when ctx is given. A chart of a workbook
    (`workbook`) names its data_range in the caption too, and holds it as its `source` unless the table was cut."""
    plot = first(space, "plotArea")
    if plot is None:
        return None
    kinds, sers = [], []
    for ch in plot:
        name = local(ch.tag)
        if name.endswith("Chart") and len(name) > 5:
            k = _kind(ch, name[:-5])
            if k not in kinds:
                kinds.append(k)
            sers.extend(kids(ch, "ser"))
    if not sers:
        return None
    chart = _kid(space, "chart")
    title = _title(_kid(chart, "title")) if chart is not None else ""
    date1904 = (_val(space, "date1904", "1") or "0") not in ("0", "false")
    block = data_block([_ranges(s) for s in sers]) if workbook else None
    head = caption("+".join(kinds), title, _axes(plot), data_range(block))

    xy = not any(_kid(x, "val") is not None for x in sers)
    heads, cols, top, clamped, more = [], [], 0, False, False
    if not xy:
        cat = next((h for s in sers if (h := _kid(s, "cat")) is not None), None)
        cols, top, clamped = _levels(cat, date1904)
        cols = cols or [{}]
        heads = [""] * len(cols)
    points = sum(len(c) for c in cols)
    pairs = []
    for n, s in enumerate(sers, 1):
        if (len(heads) or 1) + len(pairs) + 1 > limits.GRID_COLS or points > limits.CHART_CELLS:
            more = True
            break
        x = None
        if xy:
            got, t, c = _levels(_kid(s, "xVal"), date1904)
            x = got[0] if got else {}
            top, clamped, points = max(top, t), clamped or c, points + len(x)
        got, t, c = _levels(_kid(s, "yVal") if xy or _kid(s, "val") is None else _kid(s, "val"), date1904)
        y = got[0] if got else {}
        pairs.append((x, _name(s, n), y))
        top, clamped, points = max(top, t), clamped or c, points + len(y)
    if xy and pairs and any(p[0] != pairs[0][0] for p in pairs):
        if len(pairs) > limits.GRID_COLS // 2:
            more, pairs = True, pairs[:limits.GRID_COLS // 2]
        for x, name, y in pairs:
            heads += ["x", name]
            cols += [x, y]
    else:
        if xy:
            heads, cols = ["x"], [pairs[0][0]]
        for _, name, y in pairs:
            heads.append(name)
            cols.append(y)
    room = max(limits.CHART_CELLS // len(heads) - 1, 0)  # the header row counts too
    rows_cut = top > room
    top = min(top, room)
    notes = []
    if rows_cut or clamped:
        notes.append(f"first {top if rows_cut else CHART_ROWS} points")
    if more:
        notes.append(f"first {len(pairs)} series")
    if notes:
        head += " (" + ", ".join(notes) + ")"
        if ctx is not None:
            ctx.skipped["chart_cut"] += 1

    rows = [[_cell(h, True) for h in heads]]
    for i in range(top):
        rows.append([_cell(c.get(i, "")) for c in cols])
    return Table(rows, head, source=() if notes else source(block))


def series_table(caption, cats, series, series_cut, points_cut, ctx) -> Table | None:
    """charts.chart_table's cut: at most GRID_COLS - 1 series, parsed points kept near CHART_CELLS, rows to what
    CHART_CELLS leaves with the header row; one chart_cut for any cut, including a grid read only in part."""
    if not series:
        if series_cut or points_cut:
            ctx.skipped["chart_cut"] += 1
        return None
    names, cols, points, more = [""], [], len(cats), series_cut
    for n, (name, values) in enumerate(series, 1):
        if n >= limits.GRID_COLS or points > limits.CHART_CELLS:
            more = True
            break
        names.append(name)
        cols.append(values)
        points += len(values)
    room = max(limits.CHART_CELLS // len(names) - 1, 0)
    top = min(len(cats), room)
    notes = []
    if len(cats) > room or points_cut:
        notes.append(f"first {top} points")
    if more:
        notes.append(f"first {len(cols)} series")
    if notes:
        caption += " (" + ", ".join(notes) + ")"
        ctx.skipped["chart_cut"] += 1
    rows = [[_cell(n, True) for n in names]]
    for i in range(top):
        rows.append([_cell(cats[i])] + [_cell(c[i]) for c in cols])
    return Table(rows, caption)
