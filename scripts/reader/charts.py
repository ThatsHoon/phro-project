"""DrawingML chart caches (c:chartSpace) to a Table, shared by hwpx and OOXML."""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .containers.zipx import attr, first, kids, local, walk
from .model import Cell, Para, Table, Text

_CACHES = ("strCache", "numCache", "strLit", "numLit")
_PCT = re.compile(r"^0(?:\.(0+))?%$")


def _kid(el, name):
    k = kids(el, name)
    return k[0] if k else None


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


def _fmt(text: str, code: str) -> str:
    plain = text[:-2] if text.endswith(".0") else text
    try:
        d = Decimal(text.strip())
        if not d.is_finite():
            return plain
    except InvalidOperation:
        return plain
    m = _PCT.match(code or "")
    if m:
        places = len(m.group(1) or "")
        q = (d * 100).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return f"{q:.{places}f}%"
    if code == "#,##0":
        return f"{int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"
    return plain


def _column(holder):
    """({idx: text}, row count) of the cache under a c:cat / c:val / c:xVal / c:yVal."""
    cache = _cache(holder)
    if cache is None:
        return {}, 0
    fc = _kid(cache, "formatCode")
    code = (fc.text or "").strip() if fc is not None else ""
    pc = _kid(cache, "ptCount")
    top = _int(attr(pc, "val")) if pc is not None else 0
    out = {}
    for pt in kids(cache, "pt"):
        idx = _int(attr(pt, "idx", ""), -1)
        v = first(pt, "v")
        if idx < 0 or v is None:
            continue
        out[idx] = _fmt(v.text or "", (attr(pt, "formatCode") or code).strip())
        top = max(top, idx + 1)
    return out, top


def _name(ser, n: int) -> str:
    tx = _kid(ser, "tx")
    v = first(tx, "v") if tx is not None else None
    return ((v.text or "").strip() if v is not None else "") or f"Series {n}"


def _cell(text: str, header=False) -> Cell:
    return Cell([Para([Text(text)])] if text else [], header=header)


def chart_table(space):
    plot = first(space, "plotArea")
    if plot is None:
        return None
    types, sers = [], []
    for ch in plot:
        name = local(ch.tag)
        if name.endswith("Chart") and len(name) > 5:
            if name[:-5] not in types:
                types.append(name[:-5])
            sers.extend(kids(ch, "ser"))
    if not sers:
        return None

    title = ""
    chart = _kid(space, "chart")
    if chart is not None and _kid(chart, "title") is not None:
        title = "".join(t.text or "" for t in walk(_kid(chart, "title"), "t")).strip()
    kind = "+".join(types)
    caption = f"Chart: {kind} — {title}" if title else f"Chart: {kind}"

    scatter = "scatter" in types
    label, top = _column(_kid(sers[0], "xVal" if scatter else "cat"))
    names, cols = ["x" if scatter else ""], []
    for n, s in enumerate(sers, 1):
        col, t = _column(_kid(s, "yVal" if scatter else "val"))
        names.append(_name(s, n))
        cols.append(col)
        top = max(top, t)

    rows = [[_cell(n, True) for n in names]]
    for i in range(top):
        rows.append([_cell(label.get(i, ""))] + [_cell(c.get(i, "")) for c in cols])
    return Table(rows, caption)
