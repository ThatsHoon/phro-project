"""PowerPoint 97-2003 (.ppt) embedded OLE objects, for ppt.py (the ExOleObjStg handling after dochan
`office_binary/ppt*.py` and `ole_objects.py`: an object's compound file, Equation Native first, then a Workbook or Book).

The DocumentContainer's ExObjList (0x0409) holds one ExEmbed (0x0FCC) per object; its ExOleObjAtom (0x0FC3, five u32:
drawAspect, type, exObjId, subType, persistIdRef) maps the exObjId an object shape's ExObjRefAtom names to the persist
object holding the storage, an ExOleObjStg (0x1011). At instance 0 its body is the compound file itself; at instance 1
it is a u32 size and zlib data, inflated under the document's budget and capped at EMBED_BYTES, as the raw body is.
The storage is read by embedded.read, once an exObjId:
  * an equation is placed as an Equation;
  * a workbook (Excel.Chart, Excel.Sheet, MSGraph.Chart), a chart package or a spreadsheet gives tables, over one
    sheet_grid.Budget for the presentation; each placement of its tables is charged against CHART_TOTAL (copies
    counted), and one past it shows the shape's preview and is counted under chart, as an unread workbook is;
  * anything else, and anything that cannot be read (a persist id with no ExOleObjStg, bad zlib, a body that is not a
    compound file, past EMBED_BYTES, an equation that does not convert), gives None: the caller shows the shape's
    preview picture and counts it under object. At most MAX_OBJECTS objects are read; the rest give None too.
An equation converted in part (stopped early, symbols left out) keeps its TeX, is counted under object once, and
`partial` says the caller may also show the preview; lists dropped past mtef.DEPTH are counted under depth at each
placement. A workbook that fails inside xls_embedded (damaged, encrypted, a chart with no table) is read once and
remembered as an unread chart, as in doc: `blocks` gives ([], True), its chart count is taken back and the caller
shows the preview and counts one chart at each placement (never an object).
"""
import struct

from .. import embedded, limits, officeart
from ..containers import flate
from ..limits import ReadError
from ..model import Equation, Table
from . import sheet_grid
from . import ppt_records as rec

MAX_OBJECTS = 256     # objects one presentation reads (the rest are counted under object where they are placed)
_SKIP = ("damaged", "too_large", "unsupported", "encrypted")  # a ReadError that leaves the one object unread


class Objects:
    """The embedded objects of one presentation. `doc` is the PowerPoint Document stream (decrypted when it was),
    `persist` its rec.Persist, `budget` the inflate budget, `sheets` the one sheet_grid.Budget of all embedded
    workbooks and `ctx` the Context."""

    def __init__(self, doc: bytes, persist, budget: flate.Budget, sheets: sheet_grid.Budget, ctx):
        self.doc, self.persist, self.budget, self.sheets, self.ctx = doc, persist, budget, sheets, ctx
        self._index = None   # exObjId -> persistIdRef
        self._read = {}      # exObjId -> an object read: (kind, blocks, lost) or None
        self.partial = set()  # exObjIds of equations converted in part
        self.chart_cells = 0  # table cells placed from embedded workbooks, copies counted (CHART_TOTAL)

    def _ids(self) -> dict:
        """{exObjId: persistIdRef} of the DocumentContainer's ExObjList, the first of an id winning."""
        if self._index is not None:
            return self._index
        self._index = out = {}
        got = self.persist.obj(self.persist.doc_ref)
        if got is None or got[0] != rec.DOCUMENT_CONTAINER:
            return out
        budget = limits.BIN_RECORDS
        for _, _, typ, body, size in officeart.records(self.doc, got[2], got[2] + got[3]):
            if typ != rec.EX_OBJ_LIST:
                continue
            for _, _, t, b, s in officeart.records(self.doc, body, body + size):
                if t != rec.EX_EMBED:
                    continue
                for _, _, t2, b2, s2 in officeart.records(self.doc, b, b + s):
                    budget -= 1
                    if budget < 0:
                        self.ctx.skipped["record"] += 1
                        return out
                    if t2 == rec.EX_OLE_OBJ_ATOM and s2 >= 20:
                        ex_id, pid = struct.unpack_from("<I", self.doc, b2 + 8)[0], \
                            struct.unpack_from("<I", self.doc, b2 + 16)[0]
                        out.setdefault(ex_id, pid)
        return out

    def _storage(self, pid) -> bytes | None:
        """The compound file bytes of ExOleObjStg persist object `pid`; None when it is not one, is over EMBED_BYTES
        or its zlib data does not inflate."""
        got = self.persist.obj(pid)
        if got is None or got[0] != rec.EX_OLE_OBJ_STG:
            return None
        _, inst, body, size = got
        if inst == 0:
            return self.doc[body:body + size] if size <= limits.EMBED_BYTES else None
        if inst != 1 or size < 5:
            return None
        claimed = struct.unpack_from("<I", self.doc, body)[0]
        if claimed > limits.EMBED_BYTES:
            return None
        try:
            data = flate.inflate(self.doc[body + 4:body + size], self.budget, limits.EMBED_BYTES)
        except ReadError as e:
            if e.kind not in _SKIP:
                raise
            return None
        except ValueError:  # corrupt or cut zlib data
            return None
        return data

    def _load(self, pid):
        """(kind, blocks, lost) of the object stored under persist id `pid`, or None when it is not read."""
        try:
            raw = self._storage(pid)
            if raw is None:
                return None
            got = embedded.read(raw, self.ctx, self.sheets)
            if got is None or got[0] == "bad":
                return None
            if got[0] == "equation":
                return "equation", [Equation(got[1], False)], got[2]
            return got
        except ReadError as e:
            if e.kind not in _SKIP:
                raise
            return None

    def blocks(self, ex_obj_id: int | None) -> tuple[list | None, bool]:
        """(blocks, unread) of object `ex_obj_id` for one placement. blocks is None when the object is not read (the
        caller shows the preview and counts it under object). unread is true, with blocks [], for an unread chart:
        a workbook that gave no table, or whose tables would take the presentation past CHART_TOTAL (the caller
        shows the preview and counts it under chart). An object is read once; its blocks may be placed again."""
        pid = self._ids().get(ex_obj_id)
        if pid is None:
            return None, False
        if ex_obj_id not in self._read:
            if len(self._read) >= MAX_OBJECTS:
                return None, False  # past the cap: not read, counted where it is placed, never remembered
            self._read[ex_obj_id] = None
            got = self._read[ex_obj_id] = self._load(pid)
            if got is not None and got[0] == "equation" and got[2] - {"depth"}:
                self.partial.add(ex_obj_id)
                self.ctx.skipped["object"] += 1
        got = self._read[ex_obj_id]
        if got is None:
            return None, False
        kind, blocks, lost = got
        if kind == "chart":
            return [], True
        if kind == "equation":
            if "depth" in lost:
                self.ctx.skipped["depth"] += 1
            return list(blocks), False
        for t in blocks:
            if isinstance(t, Table):
                self.chart_cells += sum(len(row) for row in t.rows)
        if self.chart_cells > limits.CHART_TOTAL and blocks:
            return [], True  # the caller shows the preview and counts the chart, as for an unread workbook
        return list(blocks), False
