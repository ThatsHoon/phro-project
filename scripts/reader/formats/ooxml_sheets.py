"""SpreadsheetML workbooks (xlsx and its kin) to the document model: one sheet section per worksheet, displayed values.

The shared strings and rich text without phonetics, the styles (`numFmts` and the `cellXfs` format ids) and the cell
types follow anydoc `sheet/xlsx.rs`; the streaming walk, which keeps one cell in memory, follows dochan
`ooxml/xlsx.py`. Elements match by local name, so Transitional and Strict files take the same path.

The workbook part gives the sheets in file order. A relationship of type worksheet or chartsheet is read; any other
type (a macro or dialog sheet) and a missing relationship are counted under `sheet`. Styles and shared strings are
streamed, the shared strings under their own cap (`SHEET_CHARS` characters, `SHEET_SCAN` entries). Every sheet part
is streamed, and every element it closes is charged to the workbook's scan count, capped at `SHEET_SCAN`. A cell
shows its cached value; a formula without one shows `=` and its text. A sheet that cannot be read is counted under
`sheet` and the others still come out; one whose XML breaks after its first row keeps the rows read.
"""
import math
import posixpath
import re
from datetime import datetime

from .. import limits
from ..containers.zipx import attr, iterparse, local
from ..limits import ReadError
from ..model import Doc, Section
from ..numfmt import code_for, display
from . import ooxml_base, sheet_grid
from .xls import _hide

_REF = re.compile(r"([A-Za-z]{1,3})([0-9]{1,7})")  # used with fullmatch
_ESC = re.compile(r"_x([0-9A-Fa-f]{4})_")
_SURROGATE = re.compile("[\ud800-\udfff]")
_HIDDEN = frozenset({"hidden", "veryHidden"})
_SST_ELEMENTS = 4  # times SHEET_SCAN: the elements a shared-strings part may close (runs and their properties)
_STYLE_ELEMENTS = 1  # times SHEET_SCAN: the elements a styles part may close


def _unescape(text: str) -> str:
    """The text an OOXML string stands for: `_xHHHH_` is the UTF-16 code unit HHHH, in one pass (so `_x005F_x000D_`
    is the literal `_x000D_`); surrogate pairs join and a lone surrogate becomes U+FFFD; `\\r\\n` and `\\r` are
    newlines."""
    if "_x" in text:
        text = _ESC.sub(lambda m: chr(int(m.group(1), 16)), text)
        if _SURROGATE.search(text):
            text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _rich(el) -> str:
    """The text of an `si` or `is`: its direct `t`, then each run's `t`. Phonetic runs (`rPh`) are left out."""
    parts = []
    for ch in el:
        n = local(ch.tag)
        if n == "t":
            parts.append(ch.text or "")
        elif n == "r":
            parts.extend(t.text or "" for t in ch if local(t.tag) == "t")
    return _unescape("".join(parts))


def _col(ref) -> int | None:
    """The zero-based column of an A1 reference, or None when it is not one."""
    m = _REF.fullmatch(ref or "")
    if m is None:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _int(s, default=None):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


class _Wb:
    """The workbook-level facts: sheets (name, part, state), the 1904 flag, shared strings and cell XF formats."""

    def __init__(self):
        self.sheets: list[tuple[str, str, str]] = []
        self.date1904 = False
        self.sst: list[str] = []
        self.xf_fmt: list[int] = []
        self.formats: dict[int, str] = {}
        self.chartsheets: set[str] = set()  # sheet parts whose relationship is a chartsheet


def _skip(ctx, kind, e):
    """A member that cannot be read: any kind but a timeout is counted under `kind` and the read goes on."""
    if e.kind == "timeout":
        raise e
    ctx.skipped[kind] += 1


def _find(pkg, part, rels, kind, default):
    """The part a relationship of that type names; the conventional name beside the workbook only when there is
    no such relationship."""
    named = False
    for rel in rels.values():
        if rel.type.endswith("/" + kind) and not rel.external:
            named = True
            if pkg.has(rel.target):
                return rel.target
    conventional = posixpath.join(posixpath.dirname(part), default)
    if not named and pkg.has(conventional):
        return conventional
    return None


def _stream(pkg, part):
    """(event, element, local name, open elements) over a part, for start and end events. After an end has been
    handled the element is removed from its parent, so the tree holds only the open path; at an end the open list no
    longer holds the element, so its last item is the parent."""
    stack, names = [], {}
    with pkg.open(part) as f:
        for ev, el in iterparse(f, ("start", "end")):
            tag = el.tag
            name = names.get(tag)
            if name is None:
                name = names[tag] = local(tag)
            if ev == "start":
                stack.append(el)
                yield ev, el, name, stack
            else:
                stack.pop()
                yield ev, el, name, stack
                if stack:
                    stack[-1].remove(el)


def _workbook(root, rels, wb, ctx):
    for el in root:
        n = local(el.tag)
        if n == "workbookPr":
            wb.date1904 = (attr(el, "date1904") or "").strip().lower() in ("1", "true")
        elif n == "sheets":
            for sh in el:
                if local(sh.tag) != "sheet":
                    continue
                rel = rels.get(attr(sh, "id") or "")
                if rel is None or rel.external or not rel.type.endswith(("/worksheet", "/chartsheet")):
                    ctx.skipped["sheet"] += 1  # a macro or dialog sheet, or no relationship at all
                    continue
                if rel.type.endswith("/chartsheet"):
                    wb.chartsheets.add(rel.target)
                wb.sheets.append((attr(sh, "name") or "", rel.target, attr(sh, "state") or ""))


def _styles(pkg, part, wb, ctx):
    """`numFmts/numFmt` codes and the `cellXfs/xf` format ids; `cellStyleXfs` is not read."""
    cap, seen = _STYLE_ELEMENTS * limits.SHEET_SCAN, 0
    for ev, el, name, stack in _stream(pkg, part):
        if ev != "end":
            continue
        seen += 1
        if seen > cap:
            ctx.skipped["styles"] += 1
            return
        parent = local(stack[-1].tag) if stack else ""
        if name == "numFmt" and parent == "numFmts":
            fid = _int(attr(el, "numFmtId"))
            if fid is not None:
                wb.formats[fid] = attr(el, "formatCode") or ""
        elif name == "xf" and parent == "cellXfs":
            wb.xf_fmt.append(_int(attr(el, "numFmtId"), 0))


def _strings(pkg, part, wb, ctx):
    """The shared-string table, streamed; past its character or entry cap it stops, counted once (decision 2)."""
    chars_cap, entries_cap = limits.SHEET_CHARS, limits.SHEET_SCAN
    cap, seen, total, parts, size = _SST_ELEMENTS * limits.SHEET_SCAN, 0, 0, [], 0
    for ev, el, name, stack in _stream(pkg, part):
        if ev == "start":
            if name == "si":
                parts, size = [], 0
            continue
        seen += 1
        if seen > cap:
            return _cut(ctx, f"{cap} XML elements")
        depth = len(stack)
        if name == "t" and depth >= 2:
            up = local(stack[-1].tag)
            if up == "si" or (up == "r" and depth >= 3 and local(stack[-2].tag) == "si"):
                text = el.text or ""
                size += len(text)
                if size > 7 * (chars_cap - total):  # past the cap even unescaped (_xHHHH_ is 7 for 1): stop now
                    return _cut(ctx, f"{chars_cap} characters")
                parts.append(text)
        elif name == "si":
            if len(wb.sst) >= entries_cap:
                return _cut(ctx, f"{entries_cap} entries")
            text = _unescape("".join(parts))
            if total + len(text) > chars_cap:
                return _cut(ctx, f"{chars_cap} characters")
            total += len(text)
            wb.sst.append(text)
            parts, size = [], 0


def _cut(ctx, what):
    ctx.warnings.append(f"shared strings cut at {what}")
    ctx.skipped["sheet_cut"] += 1


def read_workbook(pkg, part, ctx) -> _Wb:
    """The workbook part's sheet list and date system, then its styles and shared strings."""
    root, _ = ooxml_base.load(pkg, part)
    rels = ooxml_base.rels(pkg, part, ctx)
    wb = _Wb()
    _workbook(root, rels, wb, ctx)
    for kind, default, read, counted in (("styles", "styles.xml", _styles, "styles"),
                                         ("sharedStrings", "sharedStrings.xml", _strings, "document")):
        name = _find(pkg, part, rels, kind, default)
        if name is None:
            continue
        try:
            read(pkg, name, wb, ctx)
        except ReadError as e:  # what was read before the break is kept
            _skip(ctx, counted, e)
    return wb


class _Sheets:
    """The sheets of one workbook, sharing one cell budget and one scan count."""

    def __init__(self, pkg, wb, ctx):
        self.pkg, self.wb, self.ctx = pkg, wb, ctx
        self.budget = sheet_grid.Budget()
        self.scan = 0                    # elements the sheet parts have closed, all sheets together
        self.used: set[str] = set()      # sheet parts read
        self._code: dict[int, str] = {}

    def sections(self) -> list:
        out = []
        for name, part, state in self.wb.sheets:
            blocks = None  # stays None when the guard swallows an error, so the check below is real
            try:
                with self.ctx.guard("sheet"):
                    blocks = self.sheet(name, part)
            except ReadError as e:
                _skip(self.ctx, "sheet", e)
                continue
            if blocks is None:  # the guard counted a failure
                continue
            if not blocks and part in self.wb.chartsheets:  # a chartsheet with nothing drawn has no section
                continue
            if state in _HIDDEN:
                out.append(Section("sheet", name, blocks=_hide(blocks), hidden="hidden"))
            else:
                out.append(Section("sheet", name, blocks=blocks))
        return out

    def code(self, style: int) -> str:
        """The format code of a cell style index; an index past the `cellXfs` list is General."""
        code = self._code.get(style)
        if code is None:
            xf = self.wb.xf_fmt
            code = self._code[style] = code_for(xf[style], self.wb.formats) if 0 <= style < len(xf) else "General"
        return code

    def show(self, value, style: int) -> str:
        """Text under General is shown as is, as `xlsb._Sheets.show` does; anything else goes through `display`."""
        code = self.code(style)
        if isinstance(value, str) and code == "General":
            return value
        return display(value, code, self.wb.date1904)

    def sheet(self, name: str, part: str) -> list:
        if not part or not self.pkg.has(part):
            raise ReadError("damaged", "sheet part missing")
        if part in self.used:
            raise ReadError("damaged", "sheet part used twice")
        self.used.add(part)
        if part in self.wb.chartsheets:  # a chartsheet has no cells
            return []
        grid = sheet_grid.Grid(name, self.budget, self.ctx)
        cap = limits.SHEET_SCAN
        if self.scan >= cap:  # the workbook's scan count is spent: this sheet stops at its first element
            grid.stop("scanned XML elements", cap)
            return grid.blocks()
        row, col, cells, rows_seen = -1, -1, 0, False  # cells: the `c` elements open
        stack, names = [], {}
        try:
            with self.pkg.open(part) as f:
                for ev, el in iterparse(f, ("start", "end")):
                    tag = names.get(el.tag)
                    if tag is None:
                        tag = names[el.tag] = local(el.tag)
                    if ev == "start":
                        stack.append(el)
                        if tag == "c":
                            cells += 1
                        elif tag == "row":
                            r = _int(el.get("r"))
                            row = r - 1 if r is not None and r >= 1 else row + 1
                            col, rows_seen = -1, True
                        continue
                    stack.pop()
                    self.scan += 1
                    if self.scan >= cap:
                        grid.stop("scanned XML elements", cap)
                        break
                    if tag == "c":
                        cells -= 1
                        c = _col(el.get("r"))
                        col = c if c is not None else col + 1
                        value = self._cell(el)
                        if value and not grid.put(max(row, 0), col, value):
                            break  # the grid is cut: nothing more can be put
                    if not cells and stack:  # memory holds the open path and one cell
                        stack[-1].remove(el)
        except ReadError as e:
            if e.kind != "damaged" or not rows_seen:
                raise
            grid.stop("XML error", 1)  # decision 12: the rows read are kept
        return grid.blocks()

    def _cell(self, el) -> str:
        """The displayed value of a `c` element by its type: the cached value first, then `=` and the formula."""
        t = el.get("t") or "n"
        v = f = inline = None
        for ch in el:
            n = local(ch.tag)
            if n == "v":
                v = ch
            elif n == "f":
                f = ch
            elif n == "is":
                inline = ch
        style = _int(el.get("s"), 0)
        if t == "inlineStr" and inline is not None:
            return self.show(_rich(inline), style)
        if v is None:
            return "=" + f.text if f is not None and f.text else ""
        text = v.text or ""
        if t == "s":
            i = _int(text.strip())
            return self.show(self.wb.sst[i], style) if i is not None and 0 <= i < len(self.wb.sst) else ""
        if t in ("str", "inlineStr"):
            return self.show(text, style)
        if t == "e":
            return text
        if t == "b":
            flag = text.strip().lower()
            return self.show(flag in ("1", "true"), style) if flag in ("1", "true", "0", "false") else ""
        if t == "d":
            return self._date(text, style)
        try:
            number = float(text)
        except ValueError:
            return text
        return self.show(number, style) if math.isfinite(number) else text

    def _date(self, text: str, style: int) -> str:
        """Decision 8: under General the stored ISO text; under another code its serial in the workbook's date
        system through that code. A text `fromisoformat` rejects is shown as stored."""
        if self.code(style) == "General":
            return text
        try:
            when = datetime.fromisoformat(text.strip()).replace(tzinfo=None)
        except ValueError:
            return text
        delta = when - (datetime(1904, 1, 1) if self.wb.date1904 else datetime(1899, 12, 30))
        return self.show(delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400, style)


def parse_sheets(pkg, part, ctx) -> Doc:
    wb = read_workbook(pkg, part, ctx)
    secs = _Sheets(pkg, wb, ctx).sections() if wb.sheets else []
    if not secs:
        raise ReadError("damaged", "no readable sheets")
    return Doc(blocks=secs)
