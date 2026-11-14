"""Word 97-2003 (.doc) embedded OLE objects, for doc.py: the ObjectPool storage an object's id names (logic after
dochan office_binary/ole_objects.py: the storage _<id>, Equation Native first, then a Workbook or Book).

Word writes an object as an EMBED field: the separator has fSpec and fOle2 and names the storage ObjectPool/_<id>
(sprmCObjLocation, else sprmCPicLocation, a u32), and the result's \\x01 has fSpec and the preview's PICF at its
PicLocation in the Data stream. An object is read once an id: an Equation Native is converted by mtef.to_tex after
its EQNOLEFILEHDR (cbHdr bytes, a u16 first), and the equation replaces the preview, which is not read; a Workbook or
Book stream is read by xls_embedded (its tables are placed after the paragraph, as a picture is; one it cannot
read is a chart, counted), and a CompObj naming MS Graph is a chart; anything else is another object. A document reads
256 objects at most, the rest counted under object. A storage that is not there,
an equation that does not convert and a stream that cannot be read (missing, a broken chain, damaged or too large:
ReadError's other kinds end the document) keep the preview; so does an equation converted in part (stopped early,
symbols left out), after its TeX. What is not read is counted where the object is shown:
a chart under chart, the rest under object (another object only when its preview is missing or not viewable: Assets'
VIEWABLE kinds, a BMP when bmp_to_png reads it). An object in vanished or deleted text is hidden as its text is and
never counted.
"""
from .. import images, limits
from ..containers.cfb import CfbError
from ..limits import ReadError
from ..model import Math
from . import mtef, sheet_grid, xls_embedded

_POOL = "OBJECTPOOL"
_NATIVE = "EQUATION NATIVE"
_BOOKS = ("WORKBOOK", "BOOK")
_COMP_OBJ = "\x01COMPOBJ"
_COMP_OBJ_MAX = 4096  # bytes of a CompObj stream read to find MS Graph's ProgID
_HEADER_MAX = 0xFFFF  # an EQNOLEFILEHDR's cbHdr is a u16
MAX_OBJECTS = 256     # objects one document reads (the rest are counted under object)

# an object read: (kind, TeX, what the conversion lost)
_MISSING = ("missing", "", frozenset())
_BAD = ("bad", "", frozenset())
_CHART = ("chart", "", frozenset())
_OTHER = ("other", "", frozenset())


class Objects:
    """The embedded objects of one document, read for `reader` (doc._Reader: its compound file, ctx, pictures, add,
    put_image and depth counter), each read once an id."""

    def __init__(self, reader):
        self.r = reader
        self._pool = None    # storage name (upper case) -> {stream name (upper case): path}
        self._objects = {}   # id -> an object read
        self._viewable = {}  # Data offset -> whether its preview shows as an image
        self.sheets = sheet_grid.Budget()  # one for the document: the sheets of every embedded workbook share it
        self.chart_cells = 0  # table cells placed from embedded workbooks, copies counted (CHART_TOTAL)

    def pool(self) -> dict:
        """The ObjectPool's storages and their streams, from the compound file's paths, once."""
        if self._pool is None:
            self._pool = {}
            for path in self.r.cf.names():
                parts = path.split("/")
                if len(parts) == 3 and parts[0].upper() == _POOL:
                    self._pool.setdefault(parts[1].upper(), {}).setdefault(parts[2].upper(), path)
        return self._pool

    def get(self, oid):
        """Object `oid`, read once; one whose reading raised stays bad."""
        hit = self._objects.get(oid)
        if hit is None:
            if len(self._objects) >= MAX_OBJECTS:
                return _BAD  # past the cap: not read, counted under object where it is placed, never remembered
            self._objects[oid] = _BAD
            hit = self._objects[oid] = self.load(oid)
        return hit

    def load(self, oid):
        streams = self.pool().get(f"_{oid}")
        if streams is None:
            return _MISSING
        native = streams.get(_NATIVE)
        if native is not None:
            data = self.stream(native, _HEADER_MAX + mtef.MAX_BYTES)
            if data is None:
                return _BAD
            lost = set()
            try:
                tex = mtef.native_tex(data, lost)
            except ValueError:
                return _BAD
            return ("equation", tex, frozenset(lost))
        if any(name in streams for name in _BOOKS):
            return self.workbook(streams)
        comp = streams.get(_COMP_OBJ)
        if comp is not None and b"MSGraph" in (self.stream(comp, _COMP_OBJ_MAX) or b""):
            return _CHART
        return _OTHER

    def workbook(self, streams):
        """A Workbook or Book object: ("table", its tables, none lost) through xls_embedded, else a chart left
        unread (the preview and the count stay). The reads of a failed workbook are not counted here, place() counts
        the chart where it is shown."""
        ctx = self.r.ctx
        before, warned = ctx.skipped["chart"], len(ctx.warnings)
        out = xls_embedded.blocks(_Storage(self, streams), ctx, self.sheets)
        if out:
            return ("table", out, frozenset())
        del ctx.warnings[warned:]  # the guard's warning goes with its count: place() counts the chart where it is shown
        if before:
            ctx.skipped["chart"] = before
        else:
            ctx.skipped.pop("chart", None)
        return _CHART

    def stream(self, path, cap) -> bytes | None:
        """Stream `path`, or None when it is over `cap` bytes or cannot be read. A ReadError other than damaged or
        too_large (a timeout) ends the document."""
        cf = self.r.cf
        try:
            if cf.size(path) > cap:
                return None
            return cf.read(path)
        except (KeyError, CfbError):
            return None
        except ReadError as e:
            if e.kind in ("damaged", "too_large"):
                return None
            raise

    def place(self, st, oid, at):
        """Object `oid` where its \\x01 or its field's end is, `at` the Data offset of its preview's PICF (None when
        it has none): read under guard("object"), placed under guard("chart") for a chart, else guard("object"). An
        equation is Math, through reader.add (a Hidden in vanished or deleted text) and into st.eqs for
        hancom.lone_equations; a conversion that dropped lists past mtef.DEPTH is counted under depth. Any other
        object, and an equation whose conversion lost more (it stopped early or left symbols out: its text is kept,
        as real content), puts out its preview as a picture of its run (reader.put_image), then is counted once."""
        r = self.r
        obj = None
        with r.ctx.guard("object"):
            obj = self.get(oid)
        if obj is None:
            return
        kind, tex, lost = obj
        shown = not (st.run.vanish or st.run.deleted)
        if kind == "table":
            with r.ctx.guard("chart"):
                if shown:
                    self.tables(st, tex)
            return
        with r.ctx.guard("chart" if kind == "chart" else "object"):
            if kind == "equation":
                m = Math(tex)
                r.add(st, m)
                st.eqs.append(m)
                if shown and "depth" in lost:
                    r.deep()
                if lost <= {"depth"}:
                    return
            preview = self.preview(at)
            if preview is not None:
                r.put_image(st, preview)
            if not shown:
                return
            if kind == "chart":
                r.ctx.skipped["chart"] += 1
            elif kind != "other" or preview is None or not self.viewable(at, preview):
                r.ctx.skipped["object"] += 1

    def tables(self, st, tables):
        """An embedded workbook's tables after the open paragraph, as a picture goes (st.extras). Each placement is
        charged against CHART_TOTAL, and one past it is counted under chart instead."""
        for t in tables:
            self.chart_cells += sum(len(row) for row in t.rows)
            if self.chart_cells > limits.CHART_TOTAL:
                self.r.ctx.skipped["chart"] += 1
            else:
                st.extras.append(t)

    def preview(self, at) -> bytes | None:
        """The picture of the PICF at Data offset `at` (doc_pictures, its read budgets), or None when there is none,
        it cannot be read, or Assets would not keep it."""
        if at is None:
            return None
        data = self.r.pictures.inline(at)
        if not isinstance(data, bytes) or len(data) < 16 or images.sniff(data) is None:
            return None
        return data

    def viewable(self, at, data) -> bool:
        """Whether the preview at `at` shows as an image (Assets: a VIEWABLE kind, or a BMP bmp_to_png reads), once
        an offset."""
        hit = self._viewable.get(at)
        if hit is None:
            kind = images.sniff(data)
            hit = kind in images.VIEWABLE or (kind == "bmp" and images.bmp_to_png(data) is not None)
            self._viewable[at] = hit
        return hit


class _Storage:
    """One ObjectPool storage as xls_embedded.blocks reads it: exists, size and read by stream name, through
    Objects.stream so a stream is read under the same cap and ReadError rules (one that cannot be read is a KeyError,
    which blocks counts as a failed chart)."""

    def __init__(self, objects, streams):
        self.objects, self.streams = objects, streams

    def exists(self, name) -> bool:
        return name.upper() in self.streams

    def size(self, name) -> int:
        return self.objects.r.cf.size(self.streams[name.upper()])

    def read(self, name) -> bytes:
        data = self.objects.stream(self.streams[name.upper()], limits.EMBED_BYTES)
        if data is None:
            raise KeyError(name)
        return data
