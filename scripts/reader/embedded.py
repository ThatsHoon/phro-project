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
    no container, an object whose parser failed). The caller keeps the object's preview picture and counts it
    under object.

A document has one `Objects` (`of(ctx)`), whatever reads through it: MAX_OBJECTS at most, the sheets of every
workbook over its one sheet_grid.Budget, the tables of all of them charged, copies counted, against CHART_TOTAL. An
OOXML package keeps its objects as parts a relationship names (`Parts`), each read once; a part it holds and does
not read is UNREAD, which a caller counts under object as it does a "bad" one, while a relationship that names no
part inside the package is None, a link. An RTF `\\objdata` holds an OLE1 object whose native data is the file
(`ole1_native`).

An object inside an object is read under the same `Objects`, DEPTH levels at most: a compound file's `Package`
(Office), `package_stream` or `EmbeddedOdf` (LibreOffice) stream, which holds the object's own file, and the
objects of an embedded workbook's sheet. An embedded workbook shows one sheet (an OOXML one its active sheet, an
OpenDocument one its first) and only that sheet is read; what the read counted goes to the document's counts with
the table. A stream or package over EMBED_BYTES is not read. The work objects cause is the document's too: what
their packages inflate to (EMBED_TOTAL), the XML elements built into trees while one is read (EMBED_NODES), the
BIFF records walked (BIN_RECORDS, the document's own streams with them) and the xls bytes decrypted
(EMBED_DECRYPT); an object that would pass one of them is not read. Whatever an object makes the reader do, the
objects of one document are read for EMBED_SECONDS at most: one reached after that is not read (None), as past
MAX_OBJECTS. A ReadError that only says this object cannot be read
(damaged, too large, unsupported, encrypted) gives None, and so does any error that is no ReadError; any other
ReadError ends the document, as it does everywhere.
"""
import contextlib
import copy
import struct
import time

from . import limits
from .containers.cfb import CfbError, Compound, is_cfb
from .containers.zipx import Package, charged
from .limits import ReadError
from .model import Cell, Context, Equation, Para, Section, Table

DEPTH = 4  # objects inside objects that are opened
MAX_OBJECTS = 256  # embedded objects one document reads (the rest are not read)
NATIVE = "Equation Native"
_PACKAGES = ("Package", "package_stream", "EmbeddedOdf")
_COMP_OBJ = "\x01CompObj"
_COMP_OBJ_MAX = 4096  # bytes of a CompObj stream read to find MS Graph's ProgID
_SKIP = ("damaged", "too_large", "unsupported", "encrypted")
_NONE = frozenset()
CHART = ("chart", [], _NONE)
BAD = ("bad", "", _NONE)
UNREAD = ("unread", "", _NONE)


clock = time.perf_counter


def of(ctx):
    """The one `Objects` of the document `ctx` reads."""
    if ctx.embedded is None:
        ctx.embedded = Objects(ctx)
    return ctx.embedded


class Objects:
    """The embedded objects of one document."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sheets = None  # the document's one sheet_grid.Budget, made when first asked for
        self.cells = 0      # table cells placed, copies counted
        self.count = 0      # objects read
        self.depth = 0      # objects open around the one being read
        self.inflated = 0   # bytes the members read from embedded packages inflate to
        self.records = 0    # records walked in BIFF streams, the document's own and its objects'
        self.nodes = 0      # XML elements built into trees while objects were read
        self.decrypted = 0  # bytes of xls streams decrypted
        self.spent = 0.0    # seconds the objects read so far took
        self.since = None   # when the outermost object being read began

    def budget(self):
        """The document's one sheet_grid.Budget."""
        if self.sheets is None:
            from .formats import sheet_grid
            self.sheets = sheet_grid.Budget()
        return self.sheets

    def late(self) -> bool:
        """Whether reading the document's objects has taken EMBED_SECONDS: the ones left are not read."""
        spent = self.spent if self.since is None else self.spent + clock() - self.since
        return spent >= limits.EMBED_SECONDS

    @contextlib.contextmanager
    def timed(self):
        """Counts the time one object takes; an object read inside another is in the outer one's time."""
        if self.since is not None:
            yield
            return
        self.since = clock()
        try:
            yield
        finally:
            self.spent += clock() - self.since
            self.since = None

    def inner(self):
        """A Context for the file an object holds: its own counts and warnings, this document's objects."""
        own = Context()
        own.embedded = self
        return own

    def load(self, data: bytes, ctx=None):
        """What `read` gives for `data`, counted and warned in `ctx` (the document's when None); None past
        MAX_OBJECTS or EMBED_BYTES, or when it is not read."""
        if self.count >= MAX_OBJECTS or len(data) > limits.EMBED_BYTES:
            return None
        self.count += 1
        return read(data, ctx or self.ctx, self.budget())

    def place(self, got) -> list | None:
        """Copies of a "table" object's tables for one placement, or None when they would pass CHART_TOTAL (the
        caller counts a chart, as for a workbook that gave no table)."""
        self.cells += sum(len(row) for t in got[1] for row in t.rows)
        if self.cells > limits.CHART_TOTAL:
            return None
        return [copy.copy(t) for t in got[1]]


class Parts:
    """The embedded objects of one OOXML package `pkg`, read through the document's `Objects`."""

    def __init__(self, pkg, ctx):
        self.pkg, self.ctx = pkg, ctx
        self.objects = of(ctx)
        self._read = {}     # part name -> what `rel` gave

    def place(self, got) -> list | None:
        return self.objects.place(got)

    def get(self, part: str, rid):
        """The object relationship `rid` of `part` names; None when it names none inside the package."""
        from .formats import ooxml_base
        return self.rel(ooxml_base.rels(self.pkg, part, self.ctx).get(rid) if rid else None)

    def rel(self, rel):
        """The object at relationship `rel`'s target, read once; None when it is external or missing, UNREAD when
        the part is over EMBED_BYTES, past MAX_OBJECTS or not read."""
        if rel is None or rel.external or not self.pkg.has(rel.target):
            return None
        name = rel.target
        if name not in self._read:
            if self.objects.count >= MAX_OBJECTS:
                return UNREAD
            self._read[name] = UNREAD
            if self.pkg.size(name) <= limits.EMBED_BYTES:
                try:
                    self._read[name] = self.objects.load(self.pkg.read(name), self.ctx) or UNREAD
                except ReadError as e:
                    if e.kind not in _SKIP:
                        raise
        return self._read[name]


def ole1_native(data: bytes) -> bytes | None:
    """The native data of an OLE1 embedded object ([MS-OLEDS] 2.2.5 EmbeddedObject: OLEVersion, FormatID 2, three
    length-prefixed strings for class, topic and item, then NativeDataSize and the data), None for a linked object
    or a header that does not fit. For an OLE2 object the native data is its compound file."""
    try:
        if struct.unpack_from("<I", data, 4)[0] != 2:
            return None
        at = 8
        for _ in range(3):
            at += 4 + struct.unpack_from("<I", data, at)[0]
        size = struct.unpack_from("<I", data, at)[0]
    except struct.error:
        return None
    return bytes(data[at + 4:at + 4 + size])


def read(data: bytes, ctx, sheets):
    """The object held in `data` (a zip package or a compound file), as the module names it. `sheets` is the
    document's one sheet_grid.Budget."""
    objs = of(ctx)
    if objs.depth > DEPTH or len(data) > limits.EMBED_BYTES or objs.late():
        return None
    try:
        if data[:4] == b"PK\x03\x04":
            objs.depth += 1
            try:
                with objs.timed(), charged(objs):
                    return _package(data, ctx)
            finally:
                objs.depth -= 1
        if is_cfb(data[:8]):
            with objs.timed(), Compound(data) as cf:
                return storage(cf, ctx, sheets)
    except ReadError as e:
        if e.kind not in _SKIP:
            raise
    except Exception:
        pass
    return None


def storage(cf, ctx, sheets):
    """The object whose streams `cf` holds, as the module names it."""
    objs = of(ctx)
    if objs.late():
        return None
    objs.depth += 1
    try:
        with objs.timed(), charged(objs):
            return _storage(cf, ctx, sheets)
    except ReadError as e:
        if e.kind not in _SKIP:
            raise
    except Exception:
        pass
    finally:
        objs.depth -= 1
    return None


def _storage(cf, ctx, sheets):
    from .formats import mtef, xls_embedded
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
            return read(cf.read(name), ctx, sheets)
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
    return None


def _tables(blocks) -> list:
    return [b if isinstance(b, Table) else Table([[Cell([b])]]) for b in blocks if isinstance(b, (Table, Para))]


def _package(data, ctx):
    from .formats import zip_route
    kind = zip_route.route(data)
    if kind == "odf":
        return _odf(data, ctx)
    if kind == "ooxml":
        return _workbook(data, ctx)
    return None


def _shown(blocks, own, ctx):
    """The tables of `blocks`, with what reading them counted in `own`. With no table, a chart, or UNREAD when
    the read counted something it left out."""
    out = _tables(blocks)
    if not out:
        return UNREAD if own.skipped else CHART
    ctx.skipped.update(own.skipped)
    return "table", out, _NONE


def _odf(data, ctx):
    from .formats import odf_sheets
    from .formats.odf_base import Body, Styles, kids, local
    with Package(data) as pkg:
        if not pkg.has("content.xml"):
            return None
        root = pkg.xml("content.xml")
        own = of(ctx).inner()
        body = Body(pkg, own, Styles(root, None))
        inner = [c for b in kids(root, "body") for c in b]
        if local(root.tag) == "math" or any(local(c.tag) == "chart" for c in inner):
            got = body.object_blocks(root, 0)
            if got and isinstance(got[0], Equation):
                return "equation", got[0].tex, _NONE
            if local(root.tag) == "math":
                return BAD
            return _shown(got, own, ctx)
        if any(local(c.tag) == "spreadsheet" for c in inner):
            doc = odf_sheets.sheets(pkg, own, root, None, first=True)
            sec = next((b for b in doc.blocks if isinstance(b, Section) and not b.hidden), None)
            return _shown(sec.blocks if sec is not None else [], own, ctx)
    return None


def _workbook(data, ctx):
    from .formats import ooxml, ooxml_base, ooxml_sheets
    with Package(data) as pkg:
        part = ooxml._main_part(pkg)
        root, _ = ooxml_base.load(pkg, part)
        if ooxml_base.local(root.tag) != "workbook":
            return None
        own = of(ctx).inner()
        return _shown(ooxml_sheets.active_sheet(pkg, part, own), own, ctx)
