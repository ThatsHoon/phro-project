"""The IWA object store of Pages, Numbers and Keynote: every archived object of a document, by identifier.

Ported from docling (MIT) `docling/backend/iwork/iwa.py` (`iter_objects`) and `docling/backend/iwork/archives.py`
(`read_objects`, `iwa_data_files`, the `IWAReader` data lookup and emitted set). An .iwa member unpacks through
`snappy.iwa` to `[varint n][TSP.ArchiveInfo][payloads...]` repeated; the archives are read from the joined chunks,
since one may cross a chunk boundary. ArchiveInfo gives 1 identifier, 2 message infos (1 type, 3 length, 10
fields_to_remove) and 3 should_merge. docling keeps the last message of each archive under its id; here an object is
its archive's first message, and with should_merge the later messages' bytes are appended to it, which is
protobuf's merge (a single field's later value comes last, a repeated field grows). fields_to_remove is not applied
and is counted `merge`; a repeated id keeps the first object, counted `duplicate`. All members fill one map before
any walk, because references cross members.

The drawables (`drawable_blocks`) follow `IWAReader._drawable_blocks` and `_picture` of `archives.py`; the Pages
walk follows `docling/backend/iwork/pages_iwa.py` (`read_content`, `floating_blocks`, `page_furniture`), with the
floating drawables kept in their container's order rather than sorted by id, and every drawable and storage
entered through `Store.follow`, so one reached twice is read once. The Numbers walk follows
`docling/backend/iwork/numbers_iwa.py` (`read_content`, `read_sheet`, `read_comment`), with the drawables kept in
the sheet's order rather than sorted by position; tables are read by `iwork_tables`.
"""
import posixpath
from collections.abc import Callable, Iterable

from .. import limits
from ..containers import flate, pbwire, snappy
from ..limits import ReadError
from ..model import Context, Doc, Image, Section
from . import iwork_tables, iwork_text, sheet_grid

IWA_BYTES = 256 * 2**20     # unpacked bytes of all .iwa members of one document (the flate.Budget iwork builds)
IWA_OBJECTS = 1_000_000     # objects one document's store may hold; the rest are unread, counted `record`
PACKAGE_METADATA = 11006    # TSP.PackageMetadata in Metadata.iwa: field 4 lists the TSP.DataInfo entries
APPS = ("pages", "numbers", "key")
SHAPE, EQUATION, IMAGE, GROUP = 2011, 2015, 3005, 3008   # TSWP.ShapeInfo, TSWP.EquationInfo, TSD.Image, TSD.Group
TABLE, PAGES_TABLE = iwork_tables.TABLE_INFO, iwork_tables.PAGES_TABLE_INFO  # TST.TableInfo, TST.WPTableInfo
DRAWABLES = {SHAPE, EQUATION, IMAGE, GROUP, TABLE}  # kinds drawable_blocks enters (6007 in Pages); Task 14 adds charts
NUMBERS_DOCUMENT, NUMBERS_SHEET, COMMENT_INFO = 1, 2, 2014  # TN.DocumentArchive, TN.SheetArchive, TSWP.CommentInfo
IMAGE_DATA = (15, 13, 11, 12)  # TSD.ImageArchive renditions: adjusted, original, placed, thumbnail (docling)
FLOATING_DEPTH = 4          # embedded messages searched for references in the floating-drawable container (docling)
PAGES_DOCUMENT, PAGES_SECTION, PAGES_TEMPLATE = 10000, 10011, 10143

# app -> walk(store, ctx), returning None when the app's root object is not in the store (Tasks 12-14 fill it)
WALKS: dict[str, Callable[["Store", Context], Doc | None]] = {}


def read_member(pkg, name: str) -> bytes:
    """`pkg.read(name)`, with what zipfile raises for a broken local header (a negative seek is a ValueError) turned
    into ReadError("damaged"), so the caller that owns the member counts it."""
    try:
        return pkg.read(name)
    except (ValueError, OSError) as e:
        raise ReadError("damaged", f"{name}: {type(e).__name__}")


class Store:
    """`objects` maps id -> (type, payload). `pkg` is the outer package and `prefix` the folder its `Data/` sits in
    (that of the Index.zip, else empty). `budget`, `scan` and `cache` are the document's table state: the cells and
    characters all its tables share, the cells they have unpacked (against `limits.SHEET_SCAN`), and what
    `iwork_tables` reads once by id."""

    def __init__(self, ctx, pkg, prefix: str = ""):
        self.ctx, self.pkg, self.prefix = ctx, pkg, prefix
        self.objects: dict[int, tuple[int, bytes]] = {}
        self.datas: dict[int, str] = {}   # data id -> file name under Data/
        self.visited: set[int] = set()    # objects a walk has entered, each entered once a walk
        self._path: set[int] = set()      # objects on the walk's current path, to tell a cycle from a repeat
        self.image_bytes = 0
        self.failed = 0                   # walks that raised (counted by walk()) or bodies that failed (by the walk)
        self.budget = sheet_grid.Budget()
        self.scan = 0
        self.cache: dict = {}

    def _count(self, kind: str):
        self.ctx.skipped[kind] += 1

    @property
    def depth(self) -> int:
        """Objects on the walk's current path: the depth a nested `follow` passes on."""
        return len(self._path)

    def has_data(self, data_id: int) -> bool:
        """Whether a data id names a member that is in the package (nothing is read or counted)."""
        name = self.datas.get(data_id)
        return bool(name) and self.pkg.has(self.prefix + "Data/" + name)

    def get(self, id: int, type: int | tuple | None = None) -> dict | None:
        """The object's fields (`pbwire.fields`), or None when the id is missing or its type is not `type` (one
        number or a tuple of them). A payload that is not a message raises ValueError, for the caller's guard."""
        item = self.objects.get(id)
        if item is None:
            return None
        if type is not None and item[0] not in (type if isinstance(type, tuple) else (type,)):
            return None
        return pbwire.fields(item[1])

    def data(self, data_id: int) -> bytes | None:
        """The bytes of `Data/<file name>` for a data id. An id with no DataInfo, a name that leaves `Data/`, a
        missing or unreadable member, or bytes past `limits.IMAGE_BYTES` (each call is charged, so copies count)
        give None and count `image`."""
        name = self.datas.get(data_id)
        member = self.prefix + "Data/" + name if name else None
        if member is None or not self.pkg.has(member):
            self._count("image")
            return None
        if self.image_bytes + self.pkg.size(member) > limits.IMAGE_BYTES:
            self._count("image")
            return None
        try:
            raw = read_member(self.pkg, member)
        except ReadError as e:
            if e.kind == "timeout":
                raise
            self._count("image")
            return None
        self.image_bytes += len(raw)
        return raw

    def follow(self, ids, visit: Callable[[int, int], object], depth: int = 0) -> list:
        """The shared walk guard: visit(id, depth) for each id not yet entered, results that are not None in order.
        An id on the current path (a cycle) is not entered and counts `depth`; one already entered elsewhere is
        skipped uncounted, since its content was already emitted. Past `limits.MAX_DEPTH` the ids are not entered,
        counted `depth` once."""
        if isinstance(ids, int):
            ids = (ids,)
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return []
        if depth >= limits.MAX_DEPTH:
            self._count("depth")
            return []
        out = []
        for i in ids:
            if i in self._path:
                self._count("depth")
                continue
            if i in self.visited:
                continue
            self.visited.add(i)
            self._path.add(i)
            try:
                r = visit(i, depth)
            finally:
                self._path.discard(i)
            if r is not None:
                out.append(r)
        return out


def _archives(stream: bytes, objects: dict) -> tuple[dict, bool, int, int]:
    """(new objects, whether IWA_OBJECTS stopped the parse, merges with fields_to_remove, duplicates) of one
    unpacked member. ValueError for broken structure; the caller then drops the whole member."""
    view = memoryview(stream)
    pending: dict[int, tuple[int, bytes]] = {}
    capped = False
    merges = duplicates = 0
    pos, end = 0, len(view)
    while pos < end:
        n, pos = pbwire.varint(view, pos)
        if n > end - pos:
            raise ValueError("ArchiveInfo past the stream")
        info = pbwire.fields(view[pos:pos + n])
        pos += n
        ident = pbwire.one(info, 1)
        if not isinstance(ident, int):
            raise ValueError("archive without an identifier")
        parts = []
        for mi in info.get(2, []):
            if not isinstance(mi, memoryview):
                raise ValueError("MessageInfo is not a message")
            m = pbwire.fields(mi)
            mtype, length = pbwire.one(m, 1, 0), pbwire.one(m, 3, 0)
            if not isinstance(mtype, int) or not isinstance(length, int):
                raise ValueError("bad MessageInfo")
            if length > end - pos:
                raise ValueError("payload past the stream")
            parts.append((mtype, view[pos:pos + length], 10 in m))
            pos += length
        if not parts:
            continue
        # the cap is checked before the object is stored; past it nothing more is parsed
        if len(objects) + len(pending) >= IWA_OBJECTS:
            capped = True
            break
        if ident in objects or ident in pending:
            duplicates += 1
            continue
        merge = pbwire.one(info, 3, 0)
        if isinstance(merge, int) and merge and len(parts) > 1:
            payload = b"".join(bytes(p[1]) for p in parts)
            merges += any(p[2] for p in parts[1:])
        else:
            payload = bytes(parts[0][1])
        pending[ident] = (parts[0][0], payload)
    return pending, capped, merges, duplicates


def _datas(store: Store, payload: bytes):
    """Data id -> file name from TSP.PackageMetadata field 4: TSP.DataInfo {1 id, 3 preferred, 4 file name}. An
    entry that cannot be read is left out; a picture that needs it counts `image` when asked for."""
    try:
        entries = pbwire.fields(payload).get(4, [])
    except ValueError:
        return
    for entry in entries:
        if not isinstance(entry, memoryview):
            continue
        try:
            f = pbwire.fields(entry)
        except ValueError:
            continue
        ident = pbwire.one(f, 1)
        name = pbwire.one(f, 4)
        if not isinstance(name, memoryview):
            name = pbwire.one(f, 3)
        if not isinstance(ident, int) or not isinstance(name, memoryview) or ident in store.datas:
            continue
        text = bytes(name).decode("utf-8", errors="replace")
        if text and "/" not in text and "\\" not in text and text not in (".", ".."):
            store.datas[ident] = text


def load(members: Iterable[tuple[str, bytes]], ctx, budget: flate.Budget, pkg, prefix: str = "") -> Store:
    """Every .iwa member of `members` into one store. Each is unpacked by `snappy.iwa` against the shared `budget`
    (past it: ReadError too_large, total); an LZVN, unreadable or broken member counts `document` and adds nothing.
    The first object past IWA_OBJECTS stops the load: the rest, later members included, are unread, counted
    `record` once. `Metadata.iwa`'s PackageMetadata gives the data file names, read later from `pkg` under `prefix`."""
    store = Store(ctx, pkg, prefix)
    for name, data in members:
        if not name.lower().endswith(".iwa"):
            continue
        try:
            stream = snappy.iwa(data, budget)
            found, capped, merges, duplicates = _archives(stream, store.objects)
        except ValueError:  # snappy.Lzvn is one
            ctx.skipped["document"] += 1
            continue
        del stream
        store.objects.update(found)
        for kind, n in (("merge", merges), ("duplicate", duplicates)):
            if n:
                ctx.skipped[kind] += n
        if posixpath.basename(name).lower() == "metadata.iwa" and not store.datas:
            meta = next((p for t, p in found.values() if t == PACKAGE_METADATA), None)
            if meta is not None:
                _datas(store, meta)
        if capped:
            ctx.skipped["record"] += 1
            break
    return store


def _image(store: Store, id: int) -> list | None:
    """A TSD.ImageArchive's picture: the first of its renditions 15, 13, 11, 12 (adjusted, original, placed,
    thumbnail; docling's order) whose data is in the package. With none there, the picture is counted `image` once."""
    f = store.get(id, IMAGE)
    ids = [d for d in (iwork_text.ref(f, n) for n in IMAGE_DATA) if d is not None]
    found = next((d for d in ids if store.has_data(d)), ids[0] if ids else None)
    if found is None:
        store._count("image")
        return None
    data = store.data(found)  # None is counted there
    if data is None:
        return None
    if not data:
        store._count("image")
        return None
    return [Image(data)]


def drawable_blocks(store: Store, id: int, ctx, text, app: str) -> list:
    """The blocks of drawable `id`, entered through `Store.follow`: an image (3005) through `Store.data`, a shape or
    text box (2011) its storage (4 owned_storage, else 2) through `text` (an `iwork_text.TextReader`), a group (3008)
    its children (2) in order, a table (6000, and 6007 when `app` is pages) through `iwork_tables.table_blocks`. An
    equation (2015) counts `object`. Other kinds, charts (5021) among them until Task 14, are skipped uncounted
    (decision 10). An id of a kind not entered is never marked visited for a later walk. Each drawable is read under
    `ctx.guard` of what it loses."""
    kinds = DRAWABLES | {PAGES_TABLE} if app == "pages" else DRAWABLES

    def drawables(ids):
        return [i for i in ids if store.objects.get(i, (None,))[0] in kinds]

    def visit(i, depth):
        kind = store.objects.get(i, (None,))[0]
        out = None
        if kind == IMAGE:
            with ctx.guard("image"):
                out = _image(store, i)
        elif kind == SHAPE:
            with ctx.guard("shape"):
                f = store.get(i, SHAPE)
                sid = iwork_text.ref(f, 4)
                if sid is None:
                    sid = iwork_text.ref(f, 2)
                out = text.blocks(sid) if sid is not None else None
        elif kind == GROUP:
            with ctx.guard("shape"):
                children = drawables(iwork_text.refs(store.get(i, GROUP), 2))
                out = [b for got in store.follow(children, visit, store.depth) for b in got]
        elif kind in (TABLE, PAGES_TABLE):  # guarded (`table`) inside
            out = iwork_tables.table_blocks(store, i, ctx, store.budget, text, text.notes)
        elif kind == EQUATION:
            store._count("object")
        return out

    return [b for got in store.follow(drawables([id]), visit, store.depth) for b in got]


def _ordered_refs(f: dict, depth: int = 0, out: list | None = None) -> list[int]:
    """Every TSP.Reference inside a message, in wire order, embedded messages searched FLOATING_DEPTH deep (docling's
    `iwa_referenced_ids`, without its sort). A part that is not a message is passed over."""
    out = [] if out is None else out
    for values in f.values():
        for v in values:
            if not isinstance(v, memoryview):
                continue
            try:
                sub = pbwire.fields(v)
            except ValueError:
                continue
            if set(sub) == {1} and len(sub[1]) == 1 and isinstance(sub[1][0], int):
                out.append(sub[1][0])
            elif depth < FLOATING_DEPTH:
                _ordered_refs(sub, depth + 1, out)
    return out


def _furniture(store: Store, body_fields: dict, text, doc: Doc, ctx):
    """Doc.headers and Doc.footers: the body storage's section table (17) -> TP.SectionArchive (10011; docling's
    PageMasterArchive) 23, 24, 25 (first, even, odd) -> TP.HeadersAndFootersArchive (10143) {1 headers, 2 footers},
    each a list of storages, one line a paragraph (`TextReader.header_lines`). A section table, section or template
    that cannot be read counts `header` once, and its siblings are still read."""
    sections = []
    with ctx.guard("header"):
        sections = [r for _, r in iwork_text.run_table(body_fields, 17) if r is not None]
    seen: set[int] = set()
    for sid in sections:
        if sid in seen:
            continue
        seen.add(sid)
        templates = []
        with ctx.guard("header"):
            section = store.get(sid, PAGES_SECTION)
            if section is not None:
                templates = [iwork_text.ref(section, n) for n in (23, 24, 25)]
        for tid in templates:
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            with ctx.guard("header"):
                template = store.get(tid, PAGES_TEMPLATE)
                if template is None:
                    continue
                for field, lines in ((1, doc.headers), (2, doc.footers)):
                    for storage in iwork_text.refs(template, field):
                        lines.extend(text.header_lines(storage))


def _pages(store: Store, ctx) -> Doc | None:
    """TP.DocumentArchive (10000, the lowest id when there are several): field 4 the body storage, the headers and
    footers, then field 20's floating drawables, placed after the body in their container's order. A body that
    fails counts `document` (once: `store.failed` tells the preview fallback) and the rest of the page is still
    read. None without the root."""
    root = min((i for i, (t, _) in store.objects.items() if t == PAGES_DOCUMENT), default=None)
    if root is None:
        return None
    f = store.get(root, PAGES_DOCUMENT)
    doc = Doc()
    text = iwork_text.TextReader(store, ctx, doc.notes, lambda d: drawable_blocks(store, d, ctx, text, "pages"))
    body = iwork_text.ref(f, 4)
    body_fields = None
    if body is not None:
        failed = True
        with ctx.guard("document"):
            doc.blocks = text.blocks(body)
            failed = False
        if failed:
            store.failed += 1
        try:
            body_fields = store.get(body, iwork_text.STORAGE)  # its section table names the headers and footers
        except ValueError:
            body_fields = None  # not a message: the body's failure above already counted it
    if body_fields:
        _furniture(store, body_fields, text, doc, ctx)
    container = iwork_text.ref(f, 20)
    if container is not None:
        drawables = []
        with ctx.guard("shape"):
            drawables = _ordered_refs(store.get(container) or {})
        for d in drawables:
            doc.blocks.extend(drawable_blocks(store, d, ctx, text, "pages"))
    return doc


WALKS["pages"] = _pages


def _sticky(store: Store, id: int, text, notes: dict, ctx) -> list:
    """A sticky note (TSWP.CommentInfoArchive, 2014, 2 -> 3056) and its replies as `Section.comments` lines: the
    thread is read by `TextReader.comment_thread`, so it is emitted once a document, and its note is moved out of
    Doc.notes. One that cannot be read counts `note`."""
    cid = None
    with ctx.guard("note"):
        cid = iwork_text.ref(store.get(id, COMMENT_INFO), 2)
    nid = text.comment_thread(cid)
    return notes.pop(nid, []) if nid is not None else []


def _sheet(store: Store, sid: int, text, doc: Doc, ctx) -> Section | None:
    """TN.SheetArchive (2) {1 name, 2 drawables} as `Section("sheet", name)`: tables, images, text boxes and groups
    in the sheet's order (`drawable_blocks`), sticky notes into `Section.comments`. A sheet that fails counts
    `sheet` and gives None."""
    with ctx.guard("sheet"):
        f = store.get(sid, NUMBERS_SHEET)
        name = pbwire.one(f, 1)
        sec = Section("sheet", (bytes(name).decode("utf-8", "replace") if isinstance(name, memoryview) else "").strip())
        for d in iwork_text.refs(f, 2):
            if store.objects.get(d, (None,))[0] == COMMENT_INFO:
                sec.comments.extend(_sticky(store, d, text, doc.notes, ctx))
            else:
                sec.blocks.extend(drawable_blocks(store, d, ctx, text, "numbers"))
        return sec
    return None


def _numbers(store: Store, ctx) -> Doc | None:
    """TN.DocumentArchive (1, the lowest id when there are several) field 1: each TN.SheetArchive (2) once, under
    `ctx.guard("sheet")`. None without the root, or when it names no sheet (so a renamed file tries the next app)."""
    root = min((i for i, (t, _) in store.objects.items() if t == NUMBERS_DOCUMENT), default=None)
    if root is None:
        return None
    f = store.get(root, NUMBERS_DOCUMENT)
    sheets = [s for s in iwork_text.refs(f, 1) if store.objects.get(s, (None,))[0] == NUMBERS_SHEET]
    if not sheets:
        return None
    doc = Doc()
    text = iwork_text.TextReader(store, ctx, doc.notes, lambda d: drawable_blocks(store, d, ctx, text, "numbers"))
    for sid in dict.fromkeys(sheets):
        sec = _sheet(store, sid, text, doc, ctx)
        if sec is not None:
            doc.blocks.append(sec)
    return doc


WALKS["numbers"] = _numbers


def walk(store: Store, app: str, ctx) -> Doc | None:
    """The document from `WALKS[app]`, else from the other two apps' walks in turn (a renamed file still reads);
    None when no walk finds its root. A walk that raises (a ReadError excepted) counts `document` under
    `ctx.guard`, adds one to `store.failed`, and the next app is tried."""
    for name in (app,) + tuple(a for a in APPS if a != app):
        fn = WALKS.get(name)
        if fn is None:
            continue
        store.visited.clear()
        store._path.clear()
        doc, failed = None, True
        with ctx.guard("document"):
            doc = fn(store, ctx)
            failed = False
        if failed:
            store.failed += 1
        elif doc is not None:
            return doc
    return None
