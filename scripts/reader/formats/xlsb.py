"""Excel binary workbooks (.xlsb): the container check, the sheet list, number formats, cell XFs and shared strings.

The record layouts were checked against anydoc `sheet/xlsb.rs` (BrtWbProp, BrtBundleSh, the cell XF block of
styles.bin and BrtSSTItem) and the framing in `containers/biff12.py`. Parts are found through the package
relationships, with the conventional names only when a relationship is absent. Every handler runs per record:
the record size gives an exact step, so a bad record is counted under `record` and the walk goes on. The sheets
themselves are read in a later step.
"""
import re
import struct
from pathlib import Path

from ..containers import biff12, cfb
from ..containers.zipx import Package
from ..limits import ReadError

BRT_SST_ITEM = 19
BRT_FMT = 44
BRT_XF = 47
BRT_WB_PROP = 153
BRT_BUNDLE_SH = 156
BRT_BEGIN_CELL_XFS = 617
BRT_END_CELL_XFS = 618

_PARTS = {"workbook": "xl/workbook.bin", "styles": "xl/styles.bin", "sharedStrings": "xl/sharedStrings.bin"}


class _Wb:
    """The workbook-level facts: sheets (name, part, hsState), the 1904 flag, shared strings and cell XF formats."""

    def __init__(self):
        self.sheets: list[tuple[str, str, int]] = []
        self.date1904 = False
        self.sst: list[str] = []
        self.xf_fmt: list[int] = []
        self.formats: dict[int, str] = {}


def _check_container(path):
    with open(path, "rb") as f:
        head = f.read(8)
    if head != cfb.MAGIC:
        return
    try:
        with cfb.Compound(path) as c:
            found = any(n.upper() == "ENCRYPTEDPACKAGE" for n in c.names())
    except ReadError:
        found = False
    if found:
        raise ReadError("encrypted")
    raise ReadError("damaged", "not an xlsb package (OLE file)")


def _walk(pkg, name, ctx):
    """(id, data, offset, size) of each record of a part, or nothing when it cannot be read."""
    try:
        data = pkg.read(name)
    except KeyError:
        return
    for rid, off, size in biff12.records(data, ctx):
        yield rid, data, off, size


def _find(pkg, rels, kind, default):
    """The part a relationship of that type names; the conventional name only when there is no such relationship."""
    named = False
    for rel in rels.values():
        if rel.type.endswith("/" + kind) and not rel.external:
            named = True
            if pkg.has(rel.target):
                return rel.target
    if not named and pkg.has(default):
        return default
    return None


def _rid_key(rid: str):
    m = re.search(r"\d+", rid)
    return (int(m.group()) if m else 1 << 30, rid)


def _sheet_pair(data, off, size, at):
    """(strRelID, strName) read from `at` into a BrtBundleSh body; ValueError when it is not exactly the record."""
    f = biff12.Fields(data, off + at, size - at)
    rid = f.wide()
    name = f.wide()
    if rid is None or name is None or f.left():
        raise ValueError("BrtBundleSh strings")
    return rid, name


def _workbook(pkg, part, rels, wb, ctx):
    fallback = sorted((r for r in rels.values() if r.type.endswith("/worksheet") and not r.external),
                      key=lambda r: _rid_key(r.id))
    index = 0
    for rid, data, off, size in _walk(pkg, part, ctx):
        try:
            if rid == BRT_WB_PROP:
                wb.date1904 = biff12.Fields(data, off, size).u32() & 1 != 0
            elif rid == BRT_BUNDLE_SH:
                f = biff12.Fields(data, off, size)
                state = f.u32()
                f.u32()  # iTabID
                n = index
                index += 1
                pair = None
                for at in (8, 12):  # some writers put an extra zero dword before strRelID
                    try:
                        pair = _sheet_pair(data, off, size, at)
                        break
                    except (ValueError, struct.error):
                        continue
                if pair is not None:
                    rel = rels.get(pair[0])
                    wb.sheets.append((pair[1], rel.target if rel and not rel.external else "", state))
                else:
                    ctx.skipped["record"] += 1
                    if n < len(fallback):
                        wb.sheets.append((f"Sheet {n + 1}", fallback[n].target, state))
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1


def _styles(pkg, part, wb, ctx):
    in_cell_xfs = False
    for rid, data, off, size in _walk(pkg, part, ctx):
        try:
            if rid == BRT_BEGIN_CELL_XFS:
                in_cell_xfs = True
            elif rid == BRT_END_CELL_XFS:
                in_cell_xfs = False
            elif rid == BRT_FMT:
                f = biff12.Fields(data, off, size)
                ifmt = f.u16()
                code = f.wide()
                if code is None:
                    raise ValueError("BrtFmt code")
                wb.formats[ifmt] = code
            elif rid == BRT_XF and in_cell_xfs:
                f = biff12.Fields(data, off, size)
                f.u16()  # ixfeParent
                wb.xf_fmt.append(f.u16())
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1


def _strings(pkg, part, wb, ctx):
    for rid, data, off, size in _walk(pkg, part, ctx):
        if rid != BRT_SST_ITEM:
            continue
        try:
            f = biff12.Fields(data, off, size)
            f.u8()  # fRichStr / fExtStr
            text = f.wide()
            if text is None:
                raise ValueError("BrtSSTItem string")
            wb.sst.append(text)
        except (ValueError, struct.error):
            ctx.skipped["record"] += 1


def read_workbook(pkg, ctx) -> _Wb:
    """Find the workbook part and read its sheet list, styles and shared strings."""
    root = pkg.rels("")
    named = [r for r in root.values() if r.type.endswith("/officeDocument") and not r.external]
    part = None
    if named:
        part = named[0].target if pkg.has(named[0].target) else None
    elif pkg.has(_PARTS["workbook"]):
        part = _PARTS["workbook"]
    if part is None:
        raise ReadError("damaged", "not an xlsb package")
    if part.lower().endswith(".xml"):
        raise ReadError("unsupported", "xlsx workbook saved as .xlsb")
    rels = pkg.rels(part)
    wb = _Wb()
    _workbook(pkg, part, rels, wb, ctx)
    styles = _find(pkg, rels, "styles", _PARTS["styles"])
    if styles:
        _styles(pkg, styles, wb, ctx)
    strings = _find(pkg, rels, "sharedStrings", _PARTS["sharedStrings"])
    if strings:
        _strings(pkg, strings, wb, ctx)
    return wb


def parse(path, ctx):
    _check_container(Path(path))
    with Package(path) as pkg:
        wb = read_workbook(pkg, ctx)
        if not wb.sheets:
            raise ReadError("damaged", "no readable sheets")
        raise ReadError("unsupported", "xlsb sheets are not read yet")
