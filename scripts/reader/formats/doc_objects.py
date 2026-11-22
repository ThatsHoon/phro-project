"""Word 97-2003 (.doc) embedded OLE objects, for doc.py: the ObjectPool storage an object's id names (logic after
dochan office_binary/ole_objects.py: the storage _<id>, Equation Native first, then a Workbook or Book).

Word writes an object as an EMBED field: the separator has fSpec and fOle2 and names the storage ObjectPool/_<id>
(sprmCObjLocation, else sprmCPicLocation, a u32), and the result's \\x01 has fSpec and the preview's PICF at its
PicLocation in the Data stream. An object is read once an id, by embedded.storage: an equation replaces the preview,
which is not read; a workbook, a chart package or a spreadsheet gives tables (placed after the paragraph, as a
picture is; one that cannot be read, or whose placement would pass CHART_TOTAL, is a chart: its preview stays,
counted); anything else is another object. A document reads
256 objects at most, the rest counted under object. A storage that is not there,
an equation that does not convert and a stream that cannot be read (missing, a broken chain, damaged or too large:
ReadError's other kinds end the document) keep the preview; so does an equation converted in part (stopped early,
symbols left out), after its TeX. What is not read is counted where the object is shown:
a chart under chart, the rest under object (another object only when its preview is missing or not viewable: Assets'
VIEWABLE kinds, a BMP when bmp_to_png reads it). An object in vanished or deleted text is hidden as its text is and
never counted (a workbook's tables are Hidden copies in vanished text, which --hidden shows; in tracked-deleted text
they are dropped).
"""
import dataclasses

from .. import embedded, images, limits
from ..containers.cfb import CfbError
from ..limits import ReadError
from ..model import Math

_POOL = "OBJECTPOOL"
MAX_OBJECTS = 256     # objects one document reads (the rest are counted under object)

# an object read: (kind, TeX, what the conversion lost)
_MISSING = ("missing", "", frozenset())
_BAD = ("bad", "", frozenset())
_OTHER = ("other", "", frozenset())


class Objects:
    """The embedded objects of one document, read for `reader` (doc._Reader: its compound file, ctx, pictures, add,
    put_image and depth counter), each read once an id."""

    def __init__(self, reader):
        self.r = reader
        self._pool = None    # storage name (upper case) -> {stream name (upper case): path}
        self._objects = {}   # id -> an object read
        self._viewable = {}  # Data offset -> whether its preview shows as an image
        self.sheets = embedded.of(reader.ctx).budget()
        self.chart_cells = 0  # table cells placed from embedded workbooks, copies counted (CHART_TOTAL)
        self.whole = 0

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
        got = embedded.storage(_Storage(self, streams), self.r.ctx, self.sheets)
        if got is None:
            return _OTHER
        return _BAD if got[0] == "bad" else got

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
                if st.run.deleted:  # tracked-deleted text stays dropped
                    return
                if self.tables(st, tex, "" if shown else "hidden"):
                    self.whole += 1
                else:
                    # past CHART_TOTAL: as a workbook that gave no table, the preview shows and a shown one is counted
                    preview = self.preview(at)
                    if preview is not None:
                        r.put_image(st, preview)
                    if shown:
                        r.ctx.skipped["chart"] += 1
            return
        with r.ctx.guard("chart" if kind == "chart" else "object"):
            if kind == "equation":
                m = Math(tex)
                r.add(st, m)
                st.eqs.append(m)
                if shown and "depth" in lost:
                    r.deep()
                if lost <= {"depth"}:
                    self.whole += 1
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

    def tables(self, st, tables, hidden="") -> bool:
        """An embedded workbook's tables after the open paragraph, not at the object's place inside it, as a picture
        goes (st.extras): a Markdown table is a block and cannot sit in the middle of a line of text, and doc pictures
        follow the same rule (test_doc_text_order pins it). Each placement is charged, copies counted, against
        CHART_TOTAL; past it none of the placement's tables is placed (False) and the caller shows the preview and
        counts the chart, as for a workbook that gave no table. `hidden` marks the tables placed in vanished text."""
        cells = sum(len(row) for t in tables for row in t.rows)
        self.chart_cells += cells
        if self.chart_cells > limits.CHART_TOTAL:
            return False
        # in vanished text the tables are Hidden copies, which --hidden shows
        st.extras.extend(dataclasses.replace(t, hidden=hidden) if hidden else t for t in tables)
        return True

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
    """One ObjectPool storage as embedded.storage reads it: exists, size and read by stream name, through
    Objects.stream so a stream is read under the same cap and ReadError rules (one that cannot be read is a
    KeyError)."""

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
