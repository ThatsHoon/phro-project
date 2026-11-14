"""Excel number formats: the text Excel shows for a cell value and a format code.

The numeric core follows dochan's `spreadsheet_format.py` (MIT): General at 15 significant digits,
the text section, sections and `[>n]` conditions, half-up rounding of the 15-digit decimal form,
scientific and fraction layouts, literals and padding. Dates and times are a separate branch
(`_datetime`); a code that holds a date or time token renders through it.

A format code comes from the file, so it is bounded: at most 255 characters, scanned once, and
`display` never raises. Built-in ids that depend on the locale are fixed choices: the CJK date
ids show `yyyy-mm-dd`, and the currency ids show no currency sign.
"""
import math
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

MAX_CODE = 255
_PRECISION = 800  # decimal digits: 1.8e308 scaled by 100**255 with 255 places still fits

_ERRORS = frozenset({"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A"})
_COND = re.compile(r"(<=|>=|<>|=|<|>)\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)")
_ELAPSED = frozenset({"h", "hh", "m", "mm", "s", "ss"})
_PAD = {"0": "0", "#": "", "?": " "}

BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    5: "#,##0_);(#,##0)", 6: "#,##0_);[Red](#,##0)", 7: "#,##0.00_);(#,##0.00)",
    8: "#,##0.00_);[Red](#,##0.00)", 9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM",
    20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm",
    32: "h:mm", 33: "h:mm:ss",
    37: "#,##0_);(#,##0)", 38: "#,##0_);[Red](#,##0)", 39: "#,##0.00_);(#,##0.00)",
    40: "#,##0.00_);[Red](#,##0.00)", 41: "#,##0", 42: "#,##0", 43: "#,##0.00", 44: "#,##0.00",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0", 48: "##0.0E+0", 49: "@",
}
BUILTIN.update({i: "yyyy-mm-dd" for i in (27, 28, 29, 30, 31, 34, 35, 36, *range(50, 59))})


def code_for(fid: int, custom: dict[int, str]) -> str:
    """The custom code of a format id first, then the built-in one, else General."""
    if fid in custom:
        return custom[fid]
    return BUILTIN.get(fid, "General")


def display(value, code: str, date1904: bool = False) -> str:
    """The text Excel shows for `value` under `code`. Never raises."""
    try:
        return _display(value, code or "", date1904)
    except Exception:
        return _plain(value)


def _plain(value) -> str:
    try:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return _general(float(value))
        return str(value)
    except Exception:
        return ""


def _display(value, code: str, date1904: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return _text(value, code)
    v = float(value)
    if not math.isfinite(v):
        return "#NUM!"
    if len(code) > MAX_CODE or code.strip().lower() in ("", "general"):
        return _general(v)
    toks = _sections(code)
    if toks is None:
        return _general(v)
    picked = _pick(toks, v)
    if picked is None:
        return _general(v)
    section, minus = picked
    if not section:
        return ""
    kinds = {t[0] for t in section}
    if kinds & {"dt", "ampm", "elapsed", "wd"}:  # the weekday alone (aaa, aaaa) is a date too
        shown = _datetime(v, section, date1904)
        return _general(v) if shown is None else shown
    section = [("lit", t[1]) if t[0] in ("yr", "era") else t for t in section if t[0] != "locale"]
    return _render(abs(v), section, minus, v)


# ---- General (dochan :58-72)

def _general(number: float) -> str:
    """At most 15 significant digits, trailing zeros dropped, exponent form at 1E+15 and above and below 1E-9."""
    if number == 0:
        return "0"
    decimal = Decimal(format(number, ".15g"))
    if not -10 < decimal.adjusted() < 15:
        mantissa, exponent = format(decimal.normalize(), "E").split("E")
        return f"{mantissa}E{int(exponent):+03d}"
    fixed = format(decimal, "f")
    return fixed.rstrip("0").rstrip(".") if decimal.as_tuple().exponent < 0 else fixed


# ---- the text section (dochan :74-113)

def _text(value: str, code: str) -> str:
    if value in _ERRORS or len(code) > MAX_CODE or code.strip().lower() in ("", "general"):
        return value
    parts = _split(code)
    if parts is None or len(parts) > 4:
        return value
    if len(parts) == 4:
        toks = _tokenize(parts[3])
    else:
        toks = None
        for part in parts[:3]:
            t = _tokenize(part)
            if t is None:
                return value
            if any(x[0] == "at" for x in t):
                toks = t
                break
    if toks is None:
        return value
    return _emit(toks, value)


def _emit(toks, at_text: str) -> str:
    out = []
    for t in toks:
        if t[0] in ("lit", "digit"):
            out.append(t[1])
        elif t[0] == "at":
            out.append(at_text)
    return "".join(out)


# ---- sections, conditions and tokens (dochan :326-347)

def _split(code: str):
    """Sections split on unquoted, unescaped, unbracketed semicolons; None for an unterminated quote or bracket."""
    parts, start, i, n = [], 0, 0, len(code)
    while i < n:
        c = code[i]
        if c == '"':
            j = code.find('"', i + 1)
            if j < 0:
                return None
            i = j + 1
        elif c == "[":
            j = code.find("]", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif c in "\\_*":
            i += 2
        else:
            if c == ";":
                parts.append(code[start:i])
                start = i + 1
            i += 1
    parts.append(code[start:])
    return parts


def _sections(code: str):
    parts = _split(code)
    if parts is None or len(parts) > 4:
        return None
    toks = [_tokenize(p) for p in parts]
    return None if any(t is None for t in toks) else toks


def _bracket(body: str):
    if body.startswith("$"):
        text, _, locale = body[1:].partition("-")
        if text:
            return ("lit", text)
        return ("locale", locale.strip().lower()) if locale else None
    m = _COND.fullmatch(body.strip())
    if m:
        return ("cond", m.group(1), float(m.group(2)))
    if body.strip().lower() in _ELAPSED:
        return ("elapsed", body.strip().lower())
    return None  # colours, [DBNum1] and the like change nothing here


def _tokenize(sec: str):
    """Tokens of one section, or None when a quote or bracket is not closed."""
    out, i, n = [], 0, len(sec)
    while i < n:
        c = sec[i]
        if c == '"':
            j = sec.find('"', i + 1)
            if j < 0:
                return None
            out.append(("lit", sec[i + 1:j]))
            i = j + 1
        elif c == "\\":
            out.append(("lit", sec[i + 1:i + 2]))
            i += 2
        elif c == "_":
            out.append(("lit", " "))
            i += 2
        elif c == "*":
            i += 2
        elif c == "[":
            j = sec.find("]", i + 1)
            if j < 0:
                return None
            t = _bracket(sec[i + 1:j])
            if t:
                out.append(t)
            i = j + 1
        elif c in "0#?":
            out.append(("ph", c))
            i += 1
        elif c in ".,/@%":
            out.append(({".": "dot", ",": "comma", "/": "slash", "@": "at", "%": "pct"}[c], c))
            i += 1
        elif c in "Gg" and sec[i:i + 7].lower() == "general":
            out.append(("general", ""))
            i += 7
        elif c in "Aa" and sec[i:i + 5].lower() == "am/pm":
            out.append(("ampm", sec[i:i + 5]))
            i += 5
        elif c in "Aa" and sec[i:i + 3].lower() == "a/p":
            out.append(("ampm", sec[i:i + 3]))
            i += 3
        elif c in "Aa" and sec[i:i + 3].lower() == "aaa":
            j = i
            while j < n and sec[j] in "Aa":
                j += 1
            out.append(("wd", j - i))
            i = j
        elif c in "Ee" and sec[i + 1:i + 2] in ("+", "-"):
            out.append(("exp", sec[i:i + 2]))
            i += 2
        elif c in "Ee":
            out.append(("yr", c))
            i += 1
        elif c in "Gg":
            out.append(("era", c))
            i += 1
        elif c in "ydhmsYDHMS":
            out.append(("dt", c))
            i += 1
        elif c in "123456789":
            out.append(("digit", c))
            i += 1
        else:
            out.append(("lit", c))
            i += 1
    return out


def _pick(toks, v: float):
    """The numeric section for `v` and whether to print a minus sign; None when no condition matches."""
    n = min(len(toks), 3)
    conds = [next((t for t in section if t[0] == "cond"), None) for section in toks[:3]]
    if not any(conds[:2]):
        if n == 1:
            i = 0
        elif n == 2:
            i = 1 if v < 0 else 0
        else:
            i = 1 if v < 0 else 2 if v == 0 else 0
        return toks[i], v < 0 and not (i == 1 and n > 1)
    for i in range(n):
        c = conds[i]
        if c is None or _holds(c[1], v, c[2]):
            section = toks[i]
            sign_matters = any(t[0] in ("ph", "general") for t in section)
            return section, v < 0 and sign_matters
    return None


def _holds(op: str, v: float, bound: float) -> bool:
    return {"<": v < bound, "<=": v <= bound, ">": v > bound, ">=": v >= bound,
            "=": v == bound, "<>": v != bound}[op]


# ---- dates and times (dochan :459-640 for the 1900 bug, rounding and elapsed forms)

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_KO_DAYS = "일월화수목금토"
_MAX_SERIAL = 2958466  # 10000-01-01 in the 1900 system


def _items(section):
    """Date tokens as lists: ('unit', letter, count), ('elapsed', letter, width), ('ampm', text),
    ('wd', count), ('yr',), ('sub', digits), ('text', str); plus the locale id of the section."""
    items, locale, i, n = [], "", 0, len(section)
    while i < n:
        t = section[i]
        k = t[0]
        last = items[-1] if items else None
        if k == "dt":
            letter = t[1].lower()
            if last and last[0] == "unit" and last[1] == letter:
                last[2] += 1
            else:
                items.append(["unit", letter, 1])
        elif k == "elapsed":
            items.append(["elapsed", t[1][0], len(t[1])])
        elif k == "ampm":
            items.append(["ampm", t[1]])
        elif k == "wd":
            items.append(["wd", t[1]])
        elif k == "yr":
            if not (last and last[0] == "yr"):
                items.append(["yr"])
        elif k == "locale":
            locale = t[1]
        elif k == "dot" and last and last[0] in ("unit", "elapsed") and last[1] == "s" \
                and i + 1 < n and section[i + 1] == ("ph", "0"):
            j = i + 1
            while j < n and section[j] == ("ph", "0"):
                j += 1
            items.append(["sub", min(j - i - 1, 3)])
            i = j
            continue
        elif k in ("lit", "digit", "dot", "comma", "slash", "pct", "ph", "exp"):
            items.append(["text", t[1]])
        i += 1
    return items, locale


def _ymd(day: int, date1904: bool):
    """(year, month, day, weekday index from Sunday) of a serial; the 1900 system keeps Excel's phantom 02-29."""
    if date1904:
        d = date(1904, 1, 1) + timedelta(days=day)
        return d.year, d.month, d.day, (d.weekday() + 1) % 7
    weekday = (day - 1) % 7
    if day == 0:
        return 1900, 1, 0, weekday
    if day == 60:
        return 1900, 2, 29, weekday
    d = (date(1899, 12, 31) if day < 60 else date(1899, 12, 30)) + timedelta(days=day)
    return d.year, d.month, d.day, weekday


def _datetime(v: float, section, date1904: bool):
    """The date and time branch: the text for a section that holds date or time tokens, or None for General."""
    if v < 0 or v >= _MAX_SERIAL:
        return None
    items, locale = _items(section)
    units = [it for it in items if it[0] in ("unit", "elapsed")]
    for idx, it in enumerate(units):  # m is a minute after an hour or before a second, else a month
        if it[0] == "unit" and it[1] == "m" and it[2] <= 2:
            if (idx > 0 and units[idx - 1][1] == "h") or (idx + 1 < len(units) and units[idx + 1][1] == "s"):
                it[1] = "i"
    precision = next((it[1] for it in items if it[0] == "sub"), 0)
    elapsed = any(it[0] == "elapsed" for it in items)
    has_time = elapsed or any(it[0] == "ampm" or (it[0] == "unit" and it[1] in "his") for it in items)
    scale = 10 ** precision
    if has_time:
        seconds = Decimal(format(v * 86400, ".15g"))
        with localcontext() as ctx:
            ctx.prec = 60
            ticks = int((seconds * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        day, rest = divmod(ticks, 86400 * scale)
        total = ticks // scale
        clock = rest // scale
        sub = ticks % scale
    else:
        day, total, clock, sub = math.floor(v), 0, 0, 0
    if day >= _MAX_SERIAL:
        return None
    secs = total if elapsed else clock
    need_date = any(it[0] in ("wd", "yr") or (it[0] == "unit" and it[1] in "ymd") for it in items)
    try:
        year, month, dom, wday = _ymd(day, date1904) if need_date else (0, 0, 0, 0)
    except OverflowError:
        return None
    hour24 = (secs // 3600) % 24
    has_ampm = any(it[0] == "ampm" for it in items)
    out = []
    for it in items:
        k = it[0]
        if k == "text":
            out.append(it[1])
        elif k == "yr":
            out.append(f"{year:04d}")
        elif k == "wd":
            out.append(_KO_DAYS[wday] if it[1] == 3 else _KO_DAYS[wday] + "요일")
        elif k == "sub":
            out.append("." + str(sub).zfill(precision) if precision else "")
        elif k == "ampm":
            pm = hour24 >= 12
            text = it[1]
            if locale == "412":
                out.append("오후" if pm else "오전")
            elif len(text) == 3:
                out.append(("p" if pm else "a") if text.islower() else ("P" if pm else "A"))
            else:
                out.append("PM" if pm else "AM")
        elif k == "elapsed":
            size = {"h": 3600, "m": 60, "s": 1}[it[1]]
            out.append(str(total // size).zfill(it[2]))
        else:
            letter, count = it[1], it[2]
            if letter == "y":
                out.append(f"{year % 100:02d}" if count <= 2 else f"{year:04d}")
            elif letter == "m":
                name = _MONTHS[month - 1] if month else ""
                if locale == "412" and month and count in (3, 4):
                    out.append(f"{month}월")  # Korean month names are the number and 월, short or long
                else:
                    out.append(str(month).zfill(count) if count <= 2 else name[:3] if count == 3
                               else name if count == 4 else name[:1])
            elif letter == "d":
                if count <= 2:
                    out.append(str(dom).zfill(count))
                elif locale == "412":
                    out.append(_KO_DAYS[wday] if count == 3 else _KO_DAYS[wday] + "요일")
                else:
                    out.append(_DAYS[wday][:3] if count == 3 else _DAYS[wday])
            elif letter == "h":
                out.append(str(hour24 % 12 or 12 if has_ampm else hour24).zfill(count))
            elif letter == "i":
                out.append(str((secs // 60) % 60).zfill(count))
            else:
                out.append(str(secs % 60).zfill(count))
    return "".join(out)


# ---- numbers (dochan :263-300), scientific (:670-782), fractions (:784-1040), literals (:1184-1225)

def _resolve(toks):
    """Commas, percents and the decimal point told apart: (tokens, thousands, scale, percents)."""
    n = len(toks)
    prev_ph, last = [False] * n, False
    for i, t in enumerate(toks):  # is the nearest non-comma token before i a placeholder
        prev_ph[i] = last
        if t[0] != "comma":
            last = t[0] == "ph"
    next_ph, nxt = [False] * n, False
    for i in range(n - 1, -1, -1):
        next_ph[i] = nxt
        if toks[i][0] != "comma":
            nxt = toks[i][0] == "ph"
    out, thousands, scale, pct, dot_seen = [], False, 0, 0, False
    for i, t in enumerate(toks):
        k = t[0]
        if k == "comma":
            if prev_ph[i] and next_ph[i]:
                thousands = True
            elif prev_ph[i]:
                scale += 1
            else:
                out.append(("lit", ","))
        elif k == "pct":
            pct += 1
            out.append(("lit", "%"))
        elif k == "dot":
            near = (i > 0 and toks[i - 1][0] == "ph") or (i + 1 < n and toks[i + 1][0] == "ph")
            if near and not dot_seen:
                dot_seen = True
                out.append(t)
            else:
                out.append(("lit", "."))
        else:
            out.append(t)
    return out, thousands, scale, pct


def _render(x: float, section, minus: bool, v: float) -> str:
    kinds = {t[0] for t in section}
    sign = "-" if minus else ""
    if "general" in kinds:
        body, done = [], False
        for t in section:
            if t[0] == "general" and not done:
                body.append(_general(x))
                done = True
            elif t[0] in ("lit", "digit"):
                body.append(t[1])
        return sign + "".join(body)
    toks, thousands, scale, pct = _resolve(section)
    at_text = _general(x)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        d = Decimal(format(x, ".15g")).scaleb(2 * pct - 3 * scale)
        i_exp = next((i for i, t in enumerate(toks) if t[0] == "exp"), -1)
        if i_exp > 0 and any(t[0] == "ph" for t in toks[:i_exp]):
            return sign + _sci(d, toks, i_exp, thousands)
        i_sl = _fraction_slash(toks)
        if i_sl >= 0:
            return sign + _fraction(d, toks, i_sl, at_text)
        return sign + _decimal(d, toks, thousands, at_text)


def _fraction_slash(toks) -> int:
    for i, t in enumerate(toks):
        if t[0] == "slash" and i > 0 and toks[i - 1][0] == "ph" and i + 1 < len(toks) \
                and toks[i + 1][0] in ("ph", "digit"):
            return i
    return -1


def _fill_int(digits: str, kinds, thousands: bool):
    """Digits laid over placeholders right to left: (text before the first placeholder, one text per placeholder).

    Extra digits go to the first placeholder; with no placeholder they come back as the lead text.
    """
    k = len(kinds)
    total = max(len(digits), k)
    cells, seen = [], False
    for p in range(total):
        from_right = total - 1 - p
        ch = digits[len(digits) - 1 - from_right] if from_right < len(digits) else _PAD[kinds[k - 1 - from_right]]
        seen = seen or ch != ""
        if thousands and seen and p < total - 1 and from_right % 3 == 0:
            ch += ","
        cells.append(ch)
    if k == 0:
        return "".join(cells), []
    head = total - k + 1
    return "", ["".join(cells[:head])] + cells[head:]


def _fill_frac(digits: str, kinds):
    """Fraction digits under placeholders; trailing zeros under `#` vanish and under `?` become spaces."""
    out, trailing = list(digits), True
    for j in range(len(kinds) - 1, -1, -1):
        if trailing and digits[j] == "0" and kinds[j] in "#?":
            out[j] = "" if kinds[j] == "#" else " "
        else:
            trailing = False
    return out


def _decimal(d: Decimal, toks, thousands: bool, at_text: str) -> str:
    dot = next((i for i, t in enumerate(toks) if t[0] == "dot"), -1)
    kinds_f = [t[1] for i, t in enumerate(toks) if t[0] == "ph" and 0 <= dot < i]
    kinds_i = [t[1] for i, t in enumerate(toks) if t[0] == "ph" and (dot < 0 or i < dot)]
    q = d.quantize(Decimal(1).scaleb(-len(kinds_f)), rounding=ROUND_HALF_UP)
    ip, _, fp = format(q, "f").partition(".")
    lead, cells = _fill_int(ip.lstrip("0"), kinds_i, thousands)
    fcells = _fill_frac(fp, kinds_f)
    out, ci, fi = [], 0, 0
    for i, t in enumerate(toks):
        k = t[0]
        if k == "ph":
            if 0 <= dot < i:
                out.append(fcells[fi])
                fi += 1
            else:
                out.append(cells[ci])
                ci += 1
        elif k == "dot":
            out.append(lead + ".")
        elif k in ("lit", "digit", "slash", "exp"):
            out.append(t[1])
        elif k == "at":
            out.append(at_text)
    return "".join(out)


def _sci(d: Decimal, toks, i_exp: int, thousands: bool) -> str:
    left, right = toks[:i_exp], toks[i_exp + 1:]
    dot = next((i for i, t in enumerate(left) if t[0] == "dot"), len(left))
    n_int = max(1, sum(1 for t in left[:dot] if t[0] == "ph"))
    f = sum(1 for t in left[dot:] if t[0] == "ph")
    e = 0
    mant = d
    if d != 0:
        e = (d.adjusted() // n_int) * n_int
        mant = d.scaleb(-e)
        if mant.quantize(Decimal(1).scaleb(-f), rounding=ROUND_HALF_UP) >= Decimal(10) ** n_int:
            e += n_int
            mant = d.scaleb(-e)
    text = _decimal(mant, left, thousands, "")
    sign = "-" if e < 0 else ("+" if toks[i_exp][1][1] == "+" else "")
    kinds = [t[1] for t in right if t[0] == "ph"]
    lead, cells = _fill_int(str(abs(e)), kinds, False)
    out, ci = [toks[i_exp][1][0], sign, lead], 0
    for t in right:
        if t[0] == "ph":
            out.append(cells[ci])
            ci += 1
        elif t[0] in ("lit", "digit", "slash", "dot"):
            out.append(t[1])
    return text + "".join(out)


def _fraction(d: Decimal, toks, s: int, at_text: str) -> str:
    n_start = s
    while n_start > 0 and toks[n_start - 1][0] == "ph":
        n_start -= 1
    d_end = s + 1
    while d_end < len(toks) and toks[d_end][0] in ("ph", "digit"):
        d_end += 1
    w_end = n_start - 1
    while w_end >= 0 and toks[w_end][0] != "ph":
        w_end -= 1
    w_start = w_end
    while w_start > 0 and toks[w_start - 1][0] == "ph":
        w_start -= 1
    has_whole = w_end >= 0
    num_kinds = [t[1] for t in toks[n_start:s]]
    den_toks = toks[s + 1:d_end]
    x = Fraction(d)
    whole = int(x) if has_whole else 0
    rest = x - whole
    if any(t[0] == "digit" for t in den_toks):
        q = int("".join(t[1] for t in den_toks)) or 1
        p = math.floor(rest * q + Fraction(1, 2))
        den_text = str(q)
    else:
        approx = rest.limit_denominator(10 ** min(len(den_toks), 5) - 1)
        p, q = approx.numerator, approx.denominator
        den_text = "".join(_pad_right(str(q), [t[1] for t in den_toks]))
    if has_whole and p >= q:
        whole += p // q
        p %= q
    num_digits = str(p) if (p or not has_whole) else ""
    lead, cells = _fill_int(num_digits, num_kinds, False)
    num_text = lead + "".join(cells)
    frac_text = num_text + "/" + den_text
    if has_whole and p == 0:
        frac_text = " " * len(frac_text)
    out = [_emit(toks[:w_start if has_whole else n_start], at_text)]
    if has_whole:
        w_kinds = [t[1] for t in toks[w_start:w_end + 1]]
        # a zero value shows 0 in the whole slot, as Excel does; a fraction part or a whole number leaves it as is
        wl, wcells = _fill_int(str(whole) if whole or not p else "", w_kinds, False)
        whole_text = wl + "".join(wcells)
        out.append(whole_text)
        if whole_text.strip() or any(k != "#" for k in w_kinds):
            out.append(_emit(toks[w_end + 1:n_start], at_text))
        else:
            frac_text = frac_text.lstrip(" ") if p else frac_text
    out.append(frac_text)
    out.append(_emit(toks[d_end:], at_text))
    return "".join(out)


def _pad_right(digits: str, kinds):
    """Denominator digits left-aligned under their placeholders, short ones padded by kind."""
    out = list(digits)
    for kind in kinds[len(digits):]:
        out.append(_PAD.get(kind, ""))
    return out
