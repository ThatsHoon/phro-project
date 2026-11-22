"""Parsed-formula tokens (Ptg) of BIFF8 (.xls, [MS-XLS] 2.5.198) and BIFF12 (.xlsb, [MS-XLSB] 2.5.97) as A1 text.

`text(rgce, extra, env)` turns one parsed formula (its token bytes and the extra data after them) into the text Excel
shows after `=`. The formats share the token set; they differ in the width of a row, a string length and an array
header, which `Env.wide` picks. A reference gives an address: a relative token of a shared formula (RefN, AreaN) an
offset from the cell at `env.row`, `env.col`. A 3-D reference names its sheet through `env.sheets` (one entry per
XTI); a defined name through `env.names`. A token outside the set read here, a built-in function outside the table
or with an arity the table does not give, a sheet or name the tables do not hold, or bytes that end inside a token
make the result None: the caller shows `=?` and counts it, so a wrong formula is never given. `put` does that for
the xls and xlsb readers: it gives each held cell's formula to the sheet's `Grid`, a shared or array one read once at
the cell its PtgExp names.
"""
import math
import re
import struct
from dataclasses import dataclass, field, replace

from . import sheet_grid

_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?", 0x24: "#NUM!",
           0x2A: "#N/A", 0x2B: "#GETTING_DATA"}
_BINARY = {0x03: "+", 0x04: "-", 0x05: "*", 0x06: "/", 0x07: "^", 0x08: "&", 0x09: "<", 0x0A: "<=", 0x0B: "=",
           0x0C: ">=", 0x0D: ">", 0x0E: "<>", 0x0F: " ", 0x10: ",", 0x11: ":"}
_UNREAD = ("?",)
_PLAIN_SHEET = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_CELL_LIKE = re.compile(r"[A-Za-z]{1,3}[0-9]+|[Rr][0-9]*[Cc][0-9]*")

# [MS-XLS] 2.5.198.17 Ftab: built-in function names by index
_NAMES = (
    "COUNT IF ISNA ISERROR SUM AVERAGE MIN MAX ROW COLUMN NA NPV STDEV DOLLAR FIXED SIN COS TAN ATAN PI SQRT EXP LN "
    "LOG10 ABS INT SIGN ROUND LOOKUP INDEX REPT MID LEN VALUE TRUE FALSE AND OR NOT MOD DCOUNT DSUM DAVERAGE DMIN "
    "DMAX DSTDEV VAR DVAR TEXT LINEST TREND LOGEST GROWTH GOTO HALT RETURN PV FV NPER PMT RATE MIRR IRR RAND MATCH "
    "DATE TIME DAY MONTH YEAR WEEKDAY HOUR MINUTE SECOND NOW AREAS ROWS COLUMNS OFFSET ABSREF RELREF ARGUMENT "
    "SEARCH TRANSPOSE ERROR STEP TYPE ECHO SET.NAME CALLER DEREF WINDOWS SERIES DOCUMENTS ACTIVE.CELL SELECTION "
    "RESULT ATAN2 ASIN ACOS CHOOSE HLOOKUP VLOOKUP LINKS INPUT ISREF GET.FORMULA GET.NAME SET.VALUE LOG EXEC CHAR "
    "LOWER UPPER PROPER LEFT RIGHT EXACT TRIM REPLACE SUBSTITUTE CODE NAMES DIRECTORY FIND CELL ISERR ISTEXT "
    "ISNUMBER ISBLANK T N FOPEN FCLOSE FSIZE FREADLN FREAD FWRITELN FWRITE FPOS DATEVALUE TIMEVALUE SLN SYD DDB "
    "GET.DEF REFTEXT TEXTREF INDIRECT REGISTER CALL ADD.BAR ADD.MENU ADD.COMMAND ENABLE.COMMAND CHECK.COMMAND "
    "RENAME.COMMAND SHOW.BAR DELETE.MENU DELETE.COMMAND GET.CHART.ITEM DIALOG.BOX CLEAN MDETERM MINVERSE MMULT "
    "FILES IPMT PPMT COUNTA CANCEL.KEY FOR WHILE BREAK NEXT INITIATE REQUEST POKE EXECUTE TERMINATE RESTART HELP "
    "GET.BAR PRODUCT FACT GET.CELL GET.WORKSPACE GET.WINDOW GET.DOCUMENT DPRODUCT ISNONTEXT GET.NOTE NOTE STDEVP "
    "VARP DSTDEVP DVARP TRUNC ISLOGICAL DCOUNTA DELETE.BAR UNREGISTER").split()
_LATER = {
    204: "USDOLLAR", 205: "FINDB", 206: "SEARCHB", 207: "REPLACEB", 208: "LEFTB", 209: "RIGHTB", 210: "MIDB",
    211: "LENB", 212: "ROUNDUP", 213: "ROUNDDOWN", 214: "ASC", 215: "DBCS", 216: "RANK", 219: "ADDRESS",
    220: "DAYS360", 221: "TODAY", 222: "VDB", 227: "MEDIAN", 228: "SUMPRODUCT", 229: "SINH", 230: "COSH",
    231: "TANH", 232: "ASINH", 233: "ACOSH", 234: "ATANH", 235: "DGET", 244: "INFO", 247: "DB", 252: "FREQUENCY",
    261: "ERROR.TYPE", 269: "AVEDEV", 270: "BETADIST", 271: "GAMMALN", 272: "BETAINV", 273: "BINOMDIST",
    274: "CHIDIST", 275: "CHIINV", 276: "COMBIN", 277: "CONFIDENCE", 278: "CRITBINOM", 279: "EVEN",
    280: "EXPONDIST", 281: "FDIST", 282: "FINV", 283: "FISHER", 284: "FISHERINV", 285: "FLOOR", 286: "GAMMADIST",
    287: "GAMMAINV", 288: "CEILING", 289: "HYPGEOMDIST", 290: "LOGNORMDIST", 291: "LOGINV", 292: "NEGBINOMDIST",
    293: "NORMDIST", 294: "NORMSDIST", 295: "NORMINV", 296: "NORMSINV", 297: "STANDARDIZE", 298: "ODD",
    299: "PERMUT", 300: "POISSON", 301: "TDIST", 302: "WEIBULL", 303: "SUMXMY2", 304: "SUMX2MY2", 305: "SUMX2PY2",
    306: "CHITEST", 307: "CORREL", 308: "COVAR", 309: "FORECAST", 310: "FTEST", 311: "INTERCEPT", 312: "PEARSON",
    313: "RSQ", 314: "STEYX", 315: "SLOPE", 316: "TTEST", 317: "PROB", 318: "DEVSQ", 319: "GEOMEAN",
    320: "HARMEAN", 321: "SUMSQ", 322: "KURT", 323: "SKEW", 324: "ZTEST", 325: "LARGE", 326: "SMALL",
    327: "QUARTILE", 328: "PERCENTILE", 329: "PERCENTRANK", 330: "MODE", 331: "TRIMMEAN", 332: "TINV",
    336: "CONCATENATE", 337: "POWER", 342: "RADIANS", 343: "DEGREES", 344: "SUBTOTAL", 345: "SUMIF",
    346: "COUNTIF", 347: "COUNTBLANK", 350: "ISPMT", 351: "DATEDIF", 352: "DATESTRING", 353: "NUMBERSTRING",
    354: "ROMAN", 358: "GETPIVOTDATA", 359: "HYPERLINK", 360: "PHONETIC", 361: "AVERAGEA", 362: "MAXA", 363: "MINA",
    364: "STDEVPA", 365: "VARPA", 366: "STDEVA", 367: "VARA", 368: "BAHTTEXT",
    480: "IFERROR", 481: "COUNTIFS", 482: "SUMIFS", 483: "AVERAGEIF", 484: "AVERAGEIFS"}
FUNCTIONS = {**dict(enumerate(_NAMES)), **_LATER}

# the argument count of a function a PtgFunc calls (one a PtgFuncVar calls carries its own)
_ARITY = {
    2: 1, 3: 1, 10: 0, 15: 1, 16: 1, 17: 1, 18: 1, 19: 0, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1, 25: 1, 26: 1, 27: 2,
    30: 2, 31: 3, 32: 1, 33: 1, 34: 0, 35: 0, 38: 1, 39: 2, 40: 3, 41: 3, 42: 3, 43: 3, 44: 3, 45: 3, 47: 3, 48: 2,
    61: 3, 63: 0, 65: 3, 66: 3, 67: 1, 68: 1, 69: 1, 71: 1, 72: 1, 73: 1, 74: 0, 75: 1, 76: 1, 77: 1, 83: 1, 86: 1,
    97: 2, 98: 1, 99: 1, 105: 1, 111: 1, 112: 1, 113: 1, 114: 1, 117: 2, 118: 1, 119: 4, 121: 1, 126: 1, 127: 1,
    128: 1, 129: 1, 130: 1, 131: 1, 140: 1, 141: 1, 142: 3, 143: 4, 162: 1, 163: 1, 164: 1, 165: 2, 184: 1,
    189: 3, 190: 1, 195: 3, 196: 3, 198: 1, 199: 3, 207: 4, 210: 3, 211: 1, 212: 2, 213: 2, 214: 1, 215: 1,
    221: 0, 229: 1, 230: 1, 231: 1, 232: 1, 233: 1, 234: 1, 235: 3, 244: 1, 252: 2, 261: 1, 271: 1, 273: 4,
    274: 2, 275: 2, 276: 2, 277: 3, 278: 3, 279: 1, 280: 3, 281: 3, 282: 3, 283: 1, 284: 1, 285: 2, 286: 4,
    287: 3, 288: 2, 289: 4, 290: 3, 291: 3, 292: 3, 293: 4, 294: 1, 295: 3, 296: 1, 297: 3, 298: 1, 299: 2,
    300: 3, 301: 3, 302: 4, 303: 2, 304: 2, 305: 2, 306: 2, 307: 2, 308: 2, 309: 3, 310: 2, 311: 2, 312: 2,
    313: 2, 314: 2, 315: 2, 316: 4, 325: 2, 326: 2, 327: 2, 328: 2, 331: 2, 332: 2, 337: 2, 342: 1, 343: 1,
    346: 2, 347: 1, 350: 4, 351: 3, 352: 1, 353: 2, 360: 1, 368: 1, 480: 2}


class _Unknown(Exception):
    pass


@dataclass
class Env:
    """What a formula's tokens refer to: the format, the cell it sits in, the sheets by XTI (None for one outside
    the workbook) and the defined names by index - 1."""
    wide: bool
    row: int = 0
    col: int = 0
    sheets: list = field(default_factory=list)
    names: list = field(default_factory=list)


def exp(rgce: bytes, extra: bytes, wide: bool):
    """The (row, col) of the cell holding the shared or array formula a formula that is only a PtgExp belongs to, or
    None for any other formula. BIFF12 keeps the column in the PtgExtraCol of the extra data."""
    if len(rgce) != 5 or rgce[0] != 0x01:
        return None
    if not wide:
        return struct.unpack_from("<HH", rgce, 1)
    if len(extra) < 4:
        return None
    return struct.unpack_from("<I", rgce, 1)[0], struct.unpack_from("<I", extra)[0]


def text(rgce: bytes, extra: bytes, env: Env):
    """The A1 text of a parsed formula, or None when a token is not one read here (see the module)."""
    try:
        return _Formula(rgce, extra, env).run()
    except (_Unknown, struct.error, IndexError, ValueError, UnicodeDecodeError):
        return None


def shown(rgce: bytes, extra: bytes, env: Env, ctx) -> str:
    """A fact's formula text (a validation's, a rule's, a name's); `?`, counted under formula, for one not read."""
    got = text(rgce, extra, env)
    if got is None:
        ctx.skipped["formula"] += 1
        return "?"
    return got


def named(n, env: Env, ctx):
    """A defined name (biff.DefinedName) as (`name = formula`, hidden); None for one with no formula or a hidden
    built-in one (flags 0x01 and 0x20: Excel's own record, as `_FilterDatabase`)."""
    if not n.name or not n.rgce or n.flags & 0x21 == 0x21:
        return None
    return f"{n.name} = {shown(n.rgce, n.extra, env, ctx)}", bool(n.flags & 1)


def put(grid, forms: list, anchored: dict, env: Env, ctx) -> None:
    """Each cell's formula into the sheet's formulas paragraph (`Grid.formula`). `forms` holds each formula's
    (row, col, tokens, extra); one that is only a PtgExp takes the shared or array formula `anchored` keeps under the
    cell it names, read once at that cell. A formula not read here is `?` and counted under formula."""
    done = {}
    for row, col, rgce, extra in forms:
        if not rgce or grid.cut and (row, col) not in grid.cells:
            continue
        at = exp(rgce, extra, env.wide)
        if at is None:
            pat = _pattern(rgce, extra, env, row, col)
        else:
            if at not in done:
                got = anchored.get(at)
                done[at] = None if got is None else _pattern(*got, env, *at)
            pat = done[at]
        if pat is None:
            ctx.skipped["formula"] += 1
            pat = _UNREAD
        grid.formula(row, col, pat)


def _pattern(rgce, extra, env, row, col):
    got = text(rgce, extra, replace(env, row=row, col=col))
    return None if got is None else sheet_grid.pattern(got, row, col)


def _sheet(name: str) -> str:
    if _PLAIN_SHEET.fullmatch(name) and not _CELL_LIKE.fullmatch(name):
        return name + "!"
    return "'" + name.replace("'", "''") + "'!"


def _number(v: float) -> str:
    if not math.isfinite(v):
        return "#NUM!"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return format(v, ".15g").upper()


class _Formula:
    def __init__(self, rgce, extra, env):
        self.b, self.at, self.extra, self.xat, self.env = rgce, 0, extra, 0, env
        self.wide = env.wide

    def take(self, fmt: str):
        out = struct.unpack_from(fmt, self.b, self.at)
        self.at += struct.calcsize(fmt)
        return out

    def run(self) -> str:
        stack = []
        while self.at < len(self.b):
            ptg = self.b[self.at]
            self.at += 1
            if ptg in _BINARY:
                b, a = stack.pop(), stack.pop()
                stack.append(a + _BINARY[ptg] + b)
            elif ptg == 0x12:
                stack.append("+" + stack.pop())
            elif ptg == 0x13:
                stack.append("-" + stack.pop())
            elif ptg == 0x14:
                stack.append(stack.pop() + "%")
            elif ptg == 0x15:
                stack.append("(" + stack.pop() + ")")
            elif ptg == 0x16:
                stack.append("")
            elif ptg == 0x17:
                stack.append('"' + self.string().replace('"', '""') + '"')
            elif ptg == 0x19:
                self.attr(stack)
            elif ptg == 0x1C:
                stack.append(_ERRORS.get(self.take("<B")[0], "#N/A"))
            elif ptg == 0x1D:
                stack.append("TRUE" if self.take("<B")[0] else "FALSE")
            elif ptg == 0x1E:
                stack.append(str(self.take("<H")[0]))
            elif ptg == 0x1F:
                stack.append(_number(self.take("<d")[0]))
            elif ptg >= 0x20:
                self.operand(0x20 + (ptg & 0x1F), stack)
            else:
                raise _Unknown(ptg)
        if len(stack) != 1:
            raise _Unknown("stack")
        return stack[0]

    def string(self) -> str:
        if self.wide:
            n = self.take("<H")[0]
            s = self.b[self.at:self.at + 2 * n]
            self.at += 2 * n
            if len(s) != 2 * n:
                raise _Unknown("string")
            return s.decode("utf-16-le")
        n, flags = self.take("<BB")
        size = n * (2 if flags & 1 else 1)
        s = self.b[self.at:self.at + size]
        self.at += size
        if len(s) != size:
            raise _Unknown("string")
        return s.decode("utf-16-le") if flags & 1 else s.decode("latin-1")

    def attr(self, stack):
        kind = self.take("<B")[0]
        if kind == 0x04:
            n = self.take("<H")[0]
            self.at += 2 * (n + 1)
        elif kind == 0x10:
            stack.append("SUM(" + stack.pop() + ")")
            self.at += 2
        elif kind in (0x01, 0x02, 0x08, 0x20, 0x40, 0x41):
            self.at += 2
        else:
            raise _Unknown("attr")

    def loc(self, relative: bool):
        """A RgceLoc (or RgceLocRel when `relative`): (row part, col part), each ("$", n) or an offset."""
        row, col = self.take("<IH" if self.wide else "<HH")
        return self.coords(row, col, relative)

    def coords(self, row, col, relative):
        row_rel, col_rel, c = col & 0x8000, col & 0x4000, col & 0x3FFF
        if not relative:
            return (row - self.env.row if row_rel else ("$", row)), (c - self.env.col if col_rel else ("$", c))
        if row_rel:
            top = 1 << (32 if self.wide else 16)
            row = row - top if row >= top >> 1 else row
        if col_rel and self.wide:
            c = c - 0x4000 if c & 0x2000 else c
        elif col_rel:
            c = (c & 0xFF) - 0x100 if c & 0x80 else c & 0xFF
        return (row if row_rel else ("$", row)), (c if col_rel else ("$", c))

    def cell(self, row, col) -> str:
        out = []
        for part, base, name, limit in ((col, self.env.col, _col_name, 16_384),
                                        (row, self.env.row, _row_name, 1_048_576)):
            if isinstance(part, tuple):
                out.append("$" + name(part[1]))
            else:
                v = part + base
                if not 0 <= v < limit:
                    return "#REF!"
                out.append(name(v))
        return "".join(out)

    def area(self, relative: bool) -> str:
        if self.wide:
            r1, r2, c1, c2 = self.take("<IIHH")
        else:
            r1, r2, c1, c2 = self.take("<HHHH")
        a = self.cell(*self.coords(r1, c1, relative))
        b = self.cell(*self.coords(r2, c2, relative))
        return "#REF!" if "#REF!" in (a, b) else f"{a}:{b}"

    def sheet(self) -> str:
        xti = self.take("<H")[0]
        sheets = self.env.sheets
        if xti >= len(sheets) or sheets[xti] is None:
            raise _Unknown("sheet")
        return _sheet(sheets[xti])

    def operand(self, base: int, stack):
        wide = self.wide
        if base == 0x20:
            self.at += 14 if wide else 7
            stack.append(self.array())
        elif base == 0x21:
            tab = self.take("<H")[0]
            if tab not in _ARITY:
                raise _Unknown("function")
            self.call(tab, _ARITY[tab], stack)
        elif base == 0x22:
            n, tab = self.take("<BH")
            if tab & 0x8000 or tab == 255:
                raise _Unknown("function")
            self.call(tab, n & 0x7F, stack)
        elif base == 0x23:
            i = self.take("<I" if wide else "<H")[0]
            self.at += 0 if wide else 2
            if not 1 <= i <= len(self.env.names) or not self.env.names[i - 1]:
                raise _Unknown("name")
            stack.append(self.env.names[i - 1])
        elif base == 0x24:
            stack.append(self.cell(*self.loc(False)))
        elif base == 0x25:
            stack.append(self.area(False))
        elif base == 0x26:
            self.at += 6
            if not wide:
                self.xat += 2 + 8 * struct.unpack_from("<H", self.extra, self.xat)[0]
        elif base in (0x27, 0x28):
            self.at += 6
        elif base == 0x29:
            self.at += 2
        elif base == 0x2A:
            self.at += 6 if wide else 4
            stack.append("#REF!")
        elif base == 0x2B:
            self.at += 12 if wide else 8
            stack.append("#REF!")
        elif base == 0x2C:
            stack.append(self.cell(*self.loc(True)))
        elif base == 0x2D:
            stack.append(self.area(True))
        elif base == 0x3A:
            sheet = self.sheet()
            stack.append(sheet + self.cell(*self.loc(False)))
        elif base == 0x3B:
            sheet = self.sheet()
            stack.append(sheet + self.area(False))
        elif base == 0x3C:
            sheet = self.sheet()
            self.at += 6 if wide else 4
            stack.append(sheet + "#REF!")
        elif base == 0x3D:
            sheet = self.sheet()
            self.at += 12 if wide else 8
            stack.append(sheet + "#REF!")
        else:
            raise _Unknown(base)

    def call(self, tab: int, n: int, stack):
        name = FUNCTIONS.get(tab)
        if name is None or n > len(stack):
            raise _Unknown("function")
        args = stack[len(stack) - n:] if n else []
        del stack[len(stack) - n:]
        stack.append(f"{name}({','.join(args)})")

    def array(self) -> str:
        """An array constant from the extra data: BIFF8's PtgExtraArray (BIFF12's is not read)."""
        if self.wide:
            raise _Unknown("array")
        e = self.extra
        cols, rows = struct.unpack_from("<BH", e, self.xat)
        self.xat += 3
        out = []
        for _ in range(rows + 1):
            row = []
            for _ in range(cols + 1):
                kind = e[self.xat]
                self.xat += 1
                if kind == 0x01:
                    row.append(_number(struct.unpack_from("<d", e, self.xat)[0]))
                    self.xat += 8
                elif kind == 0x02:
                    n, flags = struct.unpack_from("<HB", e, self.xat)
                    self.xat += 3
                    size = n * (2 if flags & 1 else 1)
                    raw = e[self.xat:self.xat + size]
                    if len(raw) != size:
                        raise _Unknown("array")
                    self.xat += size
                    s = raw.decode("utf-16-le") if flags & 1 else raw.decode("latin-1")
                    row.append('"' + s.replace('"', '""') + '"')
                elif kind == 0x04:
                    row.append("TRUE" if e[self.xat] else "FALSE")
                    self.xat += 8
                elif kind == 0x10:
                    row.append(_ERRORS.get(e[self.xat], "#N/A"))
                    self.xat += 8
                elif kind == 0x00:
                    row.append("")
                    self.xat += 8
                else:
                    raise _Unknown("array")
            out.append(",".join(row))
        return "{" + ";".join(out) + "}"


def _col_name(c: int) -> str:
    return sheet_grid.address(0, c)[:-1]


def _row_name(r: int) -> str:
    return str(r + 1)
