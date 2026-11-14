"""SpreadsheetML workbooks (xlsx and its kin) to the document model: one sheet section per worksheet, displayed values.

The shared strings and rich text without phonetics, the styles (`numFmts` and the `cellXfs` format ids) and the cell
types follow anydoc `sheet/xlsx.rs`; the streaming walk, which keeps one cell in memory, follows dochan
`ooxml/xlsx.py`. Elements match by local name, so Transitional and Strict files take the same path.

The workbook part gives the sheets in file order. A relationship of type worksheet or chartsheet is read; any other
type (a macro or dialog sheet) and a missing relationship are counted under `sheet`. Styles and shared strings are
streamed, the shared strings under their own cap (`SHEET_CHARS` characters, `SHEET_SCAN` entries), and each part
under a closed-element cap (SST_ELEMENTS, STYLE_ELEMENTS); one that is named but missing, damaged or capped counts
`document`. Text escapes (`_xHHHH_`) are undone in strings, str and error values, formulas and sheet names. Every
sheet part is streamed, and every element it closes is charged to the workbook's scan count, capped at `SHEET_SCAN`.
A cell shows its cached value; a formula without one shows `=` and its text. A sheet that cannot be read is counted
under `sheet` and the others still come out; one whose XML breaks after its first row keeps the rows read.
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
SST_ELEMENTS = 2  # times SHEET_SCAN: the elements a shared-strings part may close (SHEET_SCAN plain si/t entries fit)
STYLE_ELEMENTS = 100_000  # elements a styles part may close; a real one closes tens of thousands at most
# the most raw characters one unescaped character can take: a surrogate pair (_xD83D__xDE00_) or an escaped CRLF
# (_x000D__x000A_), each two 7-character escapes for one character
_RAW_PER_CHAR = 14
_BATCH = 256  # ended siblings a streamed element keeps before they are dropped together


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
    """(part, named): the part a relationship of that type names, and whether one names it at all; the conventional
    name beside the workbook only when no relationship of that type exists. The part is None when there is none."""
    named = False
    for rel in rels.values():
        if rel.type.endswith("/" + kind) and not rel.external:
            named = True
            if pkg.has(rel.target):
                return rel.target, True
    conventional = posixpath.join(posixpath.dirname(part), default)
    if not named and pkg.has(conventional):
        return conventional, False
    return None, named


def _drop(el, els, done):
    """Let go of an ended element in a streamed part. `els` is the open path (`el` already taken off) and `done` the
    count of ended children each open element still holds, `el`'s own on top. Its children go now; it goes from its
    parent with the next _BATCH ended siblings, in one slice. The parser has already added the children of a whole
    fed chunk, so `remove`, which searches from the first child, would cost the chunk's width per element."""
    if done.pop():
        del el[:]
    if els:
        done[-1] += 1
        if done[-1] >= _BATCH:
            del els[-1][:done[-1]]
            done[-1] = 0


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
                wb.sheets.append((_unescape(attr(sh, "name") or ""), rel.target, attr(sh, "state") or ""))


def _styles(pkg, part, wb, ctx):
    """`numFmts/numFmt` codes and the `cellXfs/xf` format ids, streamed; `cellStyleXfs` is not read. Past
    STYLE_ELEMENTS closed elements the part stops, counted under `document`, and what was read is kept."""
    seen, els, tags, done, names = 0, [], [], [], {}
    with pkg.open(part) as f:
        for ev, el in iterparse(f, ("start", "end")):
            name = names.get(el.tag)
            if name is None:
                name = names[el.tag] = local(el.tag)
            if ev == "start":
                els.append(el)
                tags.append(name)
                done.append(0)
                continue
            els.pop()
            tags.pop()
            seen += 1
            if seen > STYLE_ELEMENTS:
                ctx.skipped["document"] += 1
                return
            if tags:
                if name == "numFmt" and tags[-1] == "numFmts":
                    fid = _int(attr(el, "numFmtId"))
                    if fid is not None:
                        wb.formats[fid] = attr(el, "formatCode") or ""
                elif name == "xf" and tags[-1] == "cellXfs":
                    wb.xf_fmt.append(_int(attr(el, "numFmtId"), 0))
            _drop(el, els, done)


def _strings(pkg, part, wb, ctx):
    """The shared-string table, streamed; past its character or entry cap, or SST_ELEMENTS times SHEET_SCAN closed
    elements, it stops, counted once (decision 2)."""
    chars_cap, entries_cap = limits.SHEET_CHARS, limits.SHEET_SCAN
    cap = SST_ELEMENTS * limits.SHEET_SCAN
    seen, total, size, parts, els, tags, done, names = 0, 0, 0, [], [], [], [], {}
    with pkg.open(part) as f:
        for ev, el in iterparse(f, ("start", "end")):
            name = names.get(el.tag)
            if name is None:
                name = names[el.tag] = local(el.tag)
            if ev == "start":
                els.append(el)
                tags.append(name)
                done.append(0)
                if name == "si":
                    parts, size = [], 0
                continue
            els.pop()
            tags.pop()
            seen += 1
            if seen > cap:
                return _cut(ctx, f"{cap} XML elements")
            if name == "t" and tags:
                up = tags[-1]
                if up == "si" or (up == "r" and len(tags) >= 2 and tags[-2] == "si"):
                    text = el.text or ""
                    size += len(text)
                    # past the cap even after unescaping, which shrinks a text by at most _RAW_PER_CHAR: stop now,
                    # so one entry's raw text held here stays bounded
                    if size > _RAW_PER_CHAR * (chars_cap - total):
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
            _drop(el, els, done)


def _cut(ctx, what):
    ctx.warnings.append(f"shared strings cut at {what}")
    ctx.skipped["sheet_cut"] += 1


def _no_styles(wb):
    wb.formats, wb.xf_fmt = {}, []


def _no_strings(wb):
    wb.sst = []


def read_workbook(pkg, part, ctx) -> _Wb:
    """The workbook part's sheet list and date system, then its styles and shared strings. A styles or
    shared-strings part that is named but missing, damaged or capped counts `document` and the sheets still read:
    a ReadError keeps what was read before it, any other error leaves General formats or no shared strings."""
    root, _ = ooxml_base.load(pkg, part)
    rels = ooxml_base.rels(pkg, part, ctx)
    wb = _Wb()
    _workbook(root, rels, wb, ctx)
    for kind, default, read, reset in (("styles", "styles.xml", _styles, _no_styles),
                                       ("sharedStrings", "sharedStrings.xml", _strings, _no_strings)):
        name, named = _find(pkg, part, rels, kind, default)
        if name is None:
            if named:
                ctx.skipped["document"] += 1
            continue
        try:
            read(pkg, name, wb, ctx)
        except ReadError as e:
            _skip(ctx, "document", e)
        except Exception:
            reset(wb)
            ctx.skipped["document"] += 1
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
        row, col, cells, rows_seen = -1, -1, 0, False  # cells: the `c` elements open
        stack, done, names = [], [], {}  # the open path, and the ended children each open element still holds
        try:
            with self.pkg.open(part) as f:
                for ev, el in iterparse(f, ("start", "end")):
                    tag = names.get(el.tag)
                    if tag is None:
                        tag = names[el.tag] = local(el.tag)
                    if ev == "start":
                        if self.scan >= cap:  # this element cannot close inside the cap (a later sheet: its first)
                            grid.stop("scanned XML elements", cap)
                            break
                        stack.append(el)
                        done.append(0)
                        if tag == "c":
                            cells += 1
                        elif tag == "row":
                            r = _int(el.get("r"))
                            row = r - 1 if r is not None and r >= 1 else row + 1
                            col, rows_seen = -1, True
                        continue
                    stack.pop()
                    kids = done.pop()
                    self.scan += 1
                    if self.scan > cap:  # an element past the cap: exactly SHEET_SCAN closes are not a cut
                        grid.stop("scanned XML elements", cap)
                        break
                    if tag == "c":
                        cells -= 1
                        c = _col(el.get("r"))
                        col = c if c is not None else col + 1
                        value = self._cell(el)
                        if value and not grid.put(max(row, 0), col, value):
                            break  # the grid is cut: nothing more can be put
                    if not cells:  # outside a cell an ended element is let go, as `_drop` does (inlined for speed)
                        if kids or tag == "c":  # a cell's subtree goes once the cell is read
                            del el[:]
                        if stack:
                            done[-1] += 1
                            if done[-1] >= _BATCH:
                                del stack[-1][:done[-1]]
                                done[-1] = 0
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
            return "=" + _unescape(f.text) if f is not None and f.text else ""
        text = v.text or ""
        if t == "s":
            i = _int(text.strip())
            return self.show(self.wb.sst[i], style) if i is not None and 0 <= i < len(self.wb.sst) else ""
        if t in ("str", "inlineStr"):
            return self.show(_unescape(text), style)
        if t == "e":
            return _unescape(text)
        if t == "b":
            return self._bool(text, style)
        if t == "d":
            return self._date(text, style)
        try:
            number = float(text)
        except ValueError:
            return text
        return self.show(number, style) if math.isfinite(number) else text

    def _bool(self, text: str, style: int) -> str:
        """TRUE for "true" in any case or any non-zero number, FALSE for "false" or zero; anything else as stored."""
        word = text.strip().lower()
        if word in ("true", "false"):
            return self.show(word == "true", style)
        try:
            number = float(word)
        except ValueError:
            return text
        return self.show(number != 0, style) if math.isfinite(number) else text

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
