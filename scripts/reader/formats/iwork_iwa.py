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
"""
import posixpath
from collections.abc import Callable, Iterable

from .. import limits
from ..containers import flate, pbwire, snappy
from ..limits import ReadError
from ..model import Context, Doc

IWA_BYTES = 256 * 2**20     # unpacked bytes of all .iwa members of one document (the flate.Budget iwork builds)
IWA_OBJECTS = 1_000_000     # objects one document's store may hold; the rest are unread, counted `record`
PACKAGE_METADATA = 11006    # TSP.PackageMetadata in Metadata.iwa: field 4 lists the TSP.DataInfo entries
APPS = ("pages", "numbers", "key")

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
    (that of the Index.zip, else empty)."""

    def __init__(self, ctx, pkg, prefix: str = ""):
        self.ctx, self.pkg, self.prefix = ctx, pkg, prefix
        self.objects: dict[int, tuple[int, bytes]] = {}
        self.datas: dict[int, str] = {}   # data id -> file name under Data/
        self.visited: set[int] = set()    # objects a walk has entered, each entered once a walk
        self._path: set[int] = set()      # objects on the walk's current path, to tell a cycle from a repeat
        self.image_bytes = 0

    def _count(self, kind: str):
        self.ctx.skipped[kind] += 1

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


def _archives(stream: bytes, objects: dict) -> tuple[dict, int, int, int]:
    """(new objects, unread past IWA_OBJECTS, merges with fields_to_remove, duplicates) of one unpacked member.
    ValueError for broken structure; the caller then drops the whole member."""
    view = memoryview(stream)
    pending: dict[int, tuple[int, bytes]] = {}
    dropped = merges = duplicates = 0
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
        # the cap is checked before the object is stored
        if len(objects) + len(pending) >= IWA_OBJECTS:
            dropped += 1
            continue
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
    return pending, dropped, merges, duplicates


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
    `Metadata.iwa`'s PackageMetadata gives the data file names, read later from `pkg` under `prefix`."""
    store = Store(ctx, pkg, prefix)
    for name, data in members:
        if not name.lower().endswith(".iwa"):
            continue
        try:
            stream = snappy.iwa(data, budget)
            found, dropped, merges, duplicates = _archives(stream, store.objects)
        except ValueError:  # snappy.Lzvn is one
            ctx.skipped["document"] += 1
            continue
        del stream
        store.objects.update(found)
        for kind, n in (("record", dropped), ("merge", merges), ("duplicate", duplicates)):
            if n:
                ctx.skipped[kind] += n
        if posixpath.basename(name).lower() == "metadata.iwa" and not store.datas:
            meta = next((p for t, p in found.values() if t == PACKAGE_METADATA), None)
            if meta is not None:
                _datas(store, meta)
    return store


def walk(store: Store, app: str, ctx) -> Doc | None:
    """The document from `WALKS[app]`, else from the other two apps' walks in turn (a renamed file still reads);
    None when no walk finds its root."""
    for name in (app,) + tuple(a for a in APPS if a != app):
        fn = WALKS.get(name)
        if fn is None:
            continue
        store.visited.clear()
        doc = fn(store, ctx)
        if doc is not None:
            return doc
    return None
