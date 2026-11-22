"""Embedded objects: one dispatcher that sniffs an object's bytes and hands them to the parser the reader already has.

A caller holds an object either as bytes (`read`) or as a storage of streams (`storage`: any object with
`exists(name)`, `size(name)` and `read(name)` by stream name, as containers.cfb.Compound has; a KeyError or CfbError
from them is a stream that cannot be read). What comes back is what the object shows:

  * ("equation", TeX, lost): an `Equation Native` stream through mtef.native_tex (`lost` as mtef names it), or an
    OpenDocument formula package (its StarMath annotation, else its text);
  * ("table", [Table, ...], frozenset()): a `Workbook` or `Book` stream through xls_embedded.blocks (the active
    sheet, a chart, an MS Graph datasheet); an OOXML workbook's active sheet; an OpenDocument chart package (one
    Table under its chart caption) or spreadsheet package (its first sheet). Only tables are kept, a lone cell or
    line as a 1x1 Table, so a caller counts and places them one way;
  * ("chart", [], frozenset()): a workbook or MS Graph object that gave no table. Its reads leave no count and no
    warning behind: the caller counts the chart where it shows the preview;
  * ("bad", "", frozenset()): an equation that cannot be read or does not convert;
  * None: anything else (a PDF's CONTENTS, a packager's Ole10Native, a Word or PowerPoint document, bytes that are
    no container). The caller keeps the object's preview picture and counts it under object.

A compound file's `Package` (Office), `package_stream` or `EmbeddedOdf` (LibreOffice) stream holds the object's own
file and is read as bytes again, DEPTH levels at most; a stream or package over EMBED_BYTES is not read. A ReadError
that only says this object cannot be read (damaged, too large, unsupported, encrypted) gives None; any other ends
the document, as it does everywhere.
"""
from . import limits
from .containers.cfb import CfbError, Compound, is_cfb
from .containers.zipx import Package
from .limits import ReadError
from .model import Cell, Context, Equation, Para, Section, Table

DEPTH = 4  # objects inside objects that are opened
NATIVE = "Equation Native"
_PACKAGES = ("Package", "package_stream", "EmbeddedOdf")
_COMP_OBJ = "\x01CompObj"
_COMP_OBJ_MAX = 4096  # bytes of a CompObj stream read to find MS Graph's ProgID
_SKIP = ("damaged", "too_large", "unsupported", "encrypted")
_NONE = frozenset()
CHART = ("chart", [], _NONE)
BAD = ("bad", "", _NONE)


def read(data: bytes, ctx, sheets, depth: int = 0):
    """The object held in `data` (a zip package or a compound file), as the module names it. `sheets` is the
    document's one sheet_grid.Budget."""
    if depth > DEPTH or len(data) > limits.EMBED_BYTES:
        return None
    try:
        if data[:4] == b"PK\x03\x04":
            return _package(data, ctx, sheets)
        if is_cfb(data[:8]):
            with Compound(data) as cf:
                return storage(cf, ctx, sheets, depth)
    except ReadError as e:
        if e.kind not in _SKIP:
            raise
    except (CfbError, KeyError):
        pass
    return None


def storage(cf, ctx, sheets, depth: int = 0):
    """The object whose streams `cf` holds, as the module names it."""
    from .formats import mtef, xls_embedded
    try:
        if cf.exists(NATIVE):
            lost = set()
            try:
                if cf.size(NATIVE) > 0xFFFF + mtef.MAX_BYTES:
                    return BAD
                tex = mtef.native_tex(cf.read(NATIVE), lost)
            except (ValueError, KeyError, CfbError):
                return BAD
            return "equation", tex, frozenset(lost)
        for name in _PACKAGES:
            if cf.exists(name):
                if cf.size(name) > limits.EMBED_BYTES:
                    return None
                return read(cf.read(name), ctx, sheets, depth + 1)
        before, warned = ctx.skipped["chart"], len(ctx.warnings)
        out = xls_embedded.blocks(cf, ctx, sheets)
        if out:
            return "table", out, _NONE
        if out is not None:
            del ctx.warnings[warned:]  # the guard's warning goes with its count
            if before:
                ctx.skipped["chart"] = before
            else:
                ctx.skipped.pop("chart", None)
            return CHART
        if cf.exists(_COMP_OBJ) and cf.size(_COMP_OBJ) <= _COMP_OBJ_MAX and b"MSGraph" in cf.read(_COMP_OBJ):
            return CHART
    except ReadError as e:
        if e.kind not in _SKIP:
            raise
    except (CfbError, KeyError):
        pass
    return None


def _tables(blocks) -> list:
    return [b if isinstance(b, Table) else Table([[Cell([b])]]) for b in blocks if isinstance(b, (Table, Para))]


def _package(data, ctx, sheets):
    from .formats import zip_route
    kind = zip_route.route(data)
    if kind == "odf":
        return _odf(data, ctx)
    if kind == "ooxml":
        return _workbook(data)
    return None


def _odf(data, ctx):
    from .formats import odf_sheets
    from .formats.odf_base import Body, Styles, kids, local
    with Package(data) as pkg:
        if not pkg.has("content.xml"):
            return None
        root = pkg.xml("content.xml")
        own = Context()
        body = Body(pkg, own, Styles(root, None))
        inner = [c for b in kids(root, "body") for c in b]
        if local(root.tag) == "math" or any(local(c.tag) == "chart" for c in inner):
            got = body.object_blocks(root, 0)
            if got and isinstance(got[0], Equation):
                return "equation", got[0].tex, _NONE
            if got and not own.skipped:
                ctx.skipped.update(own.skipped)
                return "table", _tables(got), _NONE
            return CHART if local(root.tag) != "math" else BAD
        if any(local(c.tag) == "spreadsheet" for c in inner):
            doc = odf_sheets.sheets(pkg, own, root, None)
            return _first_sheet(doc.blocks, None)
    return None


def _workbook(data):
    from .formats import ooxml, ooxml_base
    from .formats.ooxml_base import attr, local
    with Package(data) as pkg:
        part = ooxml._main_part(pkg)
        root, _ = ooxml_base.load(pkg, part)
        if local(root.tag) != "workbook":
            return None
        names = [attr(s, "name") or "" for e in root if local(e.tag) == "sheets" for s in e]
        views = [v for e in root if local(e.tag) == "bookViews" for v in e]
        tab = attr(views[0], "activeTab") if views else None
        active = int(tab) if tab and tab.isdigit() else 0
        doc = ooxml.read_package(pkg, Context())
    return _first_sheet(doc.blocks, names[active] if active < len(names) else None)


def _first_sheet(blocks, name):
    """The tables of the sheet section titled `name`, else of the first sheet section; a chart when it has none."""
    secs = [b for b in blocks if isinstance(b, Section) and b.kind == "sheet" and not b.hidden]
    sec = next((s for s in secs if s.title == name), secs[0] if secs else None)
    out = _tables(sec.blocks) if sec is not None else []
    return ("table", out, _NONE) if out else CHART
