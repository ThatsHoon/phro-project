"""iWork: raw Snappy, the IWA chunk stream, the protobuf wire reader, the container and the IWA object store.

Run: python -I -X utf8 tests/test_reader_iwork.py
"""
import gzip
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from iwork_fix import (SECTION, archive, attachment, author, cell_v4, cell_v5, change, chart, chunk,  # noqa: E402
                       comment, data_list, decimal128_bytes, fmt, footnote, group, highlight, image, iwa_member,
                       iwork_zip, keynote09, keynote_doc, link, list_style, metadata_iwa, numbers09, numbers_doc,
                       pages09,
                       pages_doc, pb, pb_ref, segment, shape, slide09, slide_ids, snappy_copy, snappy_literal, sticky,
                       storage, string_entry, style, table, tile)
from reader import markdown  # noqa: E402
from reader import limits  # noqa: E402
from reader.containers import flate, pbwire, snappy  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import iwork, iwork_iwa, iwork_tables, iwork_text, sheet_grid  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import (Bold, Break, Context, Doc, Heading, Hidden, Image, Link, ListItem,  # noqa: E402
                          NoteRef, Para, Section, Table, Text)


def _raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def _body(data: bytes) -> bytes:
    """The tags of a literal block, without its declared length."""
    return snappy_literal(data)[1:]


def test_varint():
    assert pbwire.varint(b"\x01", 0) == (1, 1)
    assert pbwire.varint(b"\xac\x02", 0) == (300, 2)
    enc = pb((1, 2**63))[1:]
    assert pbwire.varint(enc, 0) == (2**63, 10)
    assert pbwire.varint(b"\xff" * 9 + b"\x01", 0) == (2**64 - 1, 10)
    _raises(ValueError, pbwire.varint, b"\x80" * 10 + b"\x01", 0)
    _raises(ValueError, pbwire.varint, b"\x80\x80", 0)
    _raises(ValueError, pbwire.varint, b"", 0)


def test_fields_wire_types():
    raw5 = bytes([5 << 3 | 5]) + b"\x00\x00\x80\x3f"
    f = pbwire.fields(pb((1, 7), (2, b"abc"), (3, 1.5), (1, 9), (4, "hi")) + raw5)
    assert f[1] == [7, 9]
    assert isinstance(f[2][0], memoryview) and bytes(f[2][0]) == b"abc"
    assert f[3] == [struct.pack("<d", 1.5)] and isinstance(f[3][0], bytes)
    assert bytes(f[4][0]) == b"hi"
    assert f[5] == [b"\x00\x00\x80\x3f"]
    assert pbwire.fields(b"") == {}


def test_fields_rejects():
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 3]))
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 4]))
    _raises(ValueError, pbwire.fields, bytes([0, 1]))
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 2, 5]) + b"ab")
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 1]) + b"abc")
    _raises(ValueError, pbwire.fields, bytes([1 << 3 | 5]) + b"ab")


def test_packed_f32_f64_ref_one():
    assert pbwire.packed(b"\x01\xac\x02\x05") == [1, 300, 5]
    assert pbwire.packed(memoryview(b"\x01\x02")) == [1, 2]
    _raises(ValueError, pbwire.packed, b"\x80")
    assert pbwire.f32(struct.pack("<f", 2.5)) == 2.5
    assert pbwire.f64(struct.pack("<d", -3.25)) == -3.25
    _raises(ValueError, pbwire.f32, b"ab")
    _raises(ValueError, pbwire.f64, b"abc")
    assert pbwire.ref(pb_ref(42)) == 42
    assert pbwire.ref(pb((2, 1))) is None
    assert pbwire.ref(pb((1, b"x"))) is None
    assert pbwire.ref(pb((1, 3), (1, 4))) == 4
    f = pbwire.fields(pb((1, 5), (1, 6)))
    assert pbwire.one(f, 1) == 6                           # a repeated singular field: the last value
    assert pbwire.one(f, 2) is None
    assert pbwire.one(f, 2, 9) == 9


def test_snappy_copies():
    out = bytearray(b"abcdefgh")
    blk = _body(bytes(out))
    blk += snappy_copy(8, 4, 1)                 # copy-1
    out += out[-8:-4]
    blk += snappy_copy(2, 3, 2)                 # copy-2, overlapping
    for _ in range(3):
        out.append(out[-2])
    blk += snappy_copy(11, 2, 4)                # copy-4
    s = len(out) - 11
    out += out[s:s + 2]
    assert snappy.raw(bytes([len(out)]) + blk, 1000) == bytes(out)
    assert snappy.raw(bytes([8]) + _body(b"ab") + snappy_copy(2, 6), 100) == b"abababab"
    assert snappy.raw(bytes([10]) + _body(b"x") + snappy_copy(1, 9), 100) == b"x" * 10
    data = bytes(range(256)) * 2
    assert snappy.raw(snappy_literal(data), 1000) == data
    assert snappy.raw(b"\x00", 10) == b""


def test_snappy_bad():
    lit = _body(b"abcd")
    _raises(ValueError, snappy.raw, bytes([5]) + lit, 100)                      # declared 5, got 4
    _raises(ValueError, snappy.raw, bytes([3]) + lit, 100)                      # declared 3, got 4
    _raises(ValueError, snappy.raw, bytes([8]) + lit + snappy_copy(9, 4), 100)  # copy before the start
    _raises(ValueError, snappy.raw, bytes([8]) + lit + snappy_copy(0, 4), 100)  # offset 0
    _raises(ValueError, snappy.raw, bytes([8]) + lit + b"\x02\x01", 100)        # truncated copy offset
    _raises(ValueError, snappy.raw, bytes([8, 0x0C]) + b"ab", 100)              # truncated literal
    _raises(ValueError, snappy.raw, b"", 100)
    blk = bytes([67]) + b"\x00a"                                                # 67 > 22 x 3
    _raises(ValueError, snappy.raw, blk, 10**6)
    _raises(ValueError, snappy.raw, snappy_literal(b"x" * 50), 49)
    assert snappy.raw(snappy_literal(b"x" * 50), 50) == b"x" * 50


def test_snappy_declared_before_output():
    huge = b"\xff\xff\xff\x7f" + b"\x00" * 3
    _raises(ValueError, snappy.raw, huge, 10**9)
    _raises(ValueError, snappy.raw, huge, 100)


def test_iwa_chunks():
    out = snappy.iwa(chunk(snappy_literal(b"hello "), 0) + chunk(b"world", 1), flate.Budget(1000))
    assert out == b"hello world"
    assert snappy.iwa(b"", flate.Budget(10)) == b""
    _raises(ValueError, snappy.iwa, chunk(b"zz", 2), flate.Budget(1000))
    _raises(ValueError, snappy.iwa, b"\x00\x05\x00", flate.Budget(1000))
    _raises(ValueError, snappy.iwa, b"\x01\x09\x00\x00ab", flate.Budget(1000))


def test_iwa_lzvn():
    e = _raises(snappy.Lzvn, snappy.iwa, b"bvxn" + b"\x00" * 8, flate.Budget(1000))
    assert isinstance(e, ValueError)


def test_iwa_budget():
    data = chunk(snappy_literal(b"y" * 200))
    e = _raises(ReadError, snappy.iwa, data, flate.Budget(100))
    assert e.kind == "too_large" and "total" in str(e)
    b = flate.Budget(300)
    assert len(snappy.iwa(data, b)) == 200 and b.used == 200
    _raises(ReadError, snappy.iwa, data, b)


def test_snappy_block_cap():
    old = snappy.SNAPPY_BLOCK
    assert old == 16 * 2**20
    try:
        snappy.SNAPPY_BLOCK = 10
        _raises(ValueError, snappy.iwa, chunk(snappy_literal(b"z" * 20)), flate.Budget(10**6))
        assert snappy.iwa(chunk(snappy_literal(b"z" * 10)), flate.Budget(10**6)) == b"z" * 10
    finally:
        snappy.SNAPPY_BLOCK = old


# -- the container and the object store

JPG = b"\xff\xd8\xff\xe0" + b"preview" + b"\xff\xd9"
THUMB = b"\xff\xd8\xff\xe0" + b"thumb" + b"\xff\xd9"


def _doc_member(*archives) -> bytes:
    return iwa_member(*(archives or (archive(1, 10000, pb((1, "body"))),)))


@contextmanager
def _walk(app="pages"):
    """Registers a walk under `app` that keeps the store it is given and returns one paragraph."""
    seen = []

    def fn(store, ctx):
        seen.append(store)
        return Doc(blocks=[Para([Text("read")])])

    old = dict(iwork_iwa.WALKS)
    iwork_iwa.WALKS[app] = fn
    try:
        yield seen
    finally:
        iwork_iwa.WALKS.clear()
        iwork_iwa.WALKS.update(old)


def _read(members, methods={}, app="pages"):
    ctx = Context()
    return iwork.parse_bytes(iwork_zip(members, methods), ctx, app), ctx


def _kind(members, methods={}, app="pages"):
    return _raises(ReadError, iwork.parse_bytes, iwork_zip(members, methods), Context(), app).kind


def _load(members, pkg_members=None):
    ctx = Context()
    pkg = Package(iwork_zip(pkg_members or {"x": b""}))
    return iwork_iwa.load(members, ctx, flate.Budget(10**6), pkg), ctx


def test_iwpv2_encrypted_any_depth():
    assert _kind({"Index/x.iwpv2": b"\x02" * 104, "Index/Document.iwa": _doc_member()}) == "encrypted"
    assert _kind({"a/b/.iwph": b"hint", "preview.jpg": JPG}) == "encrypted"


def test_encrypted_iwa_zero_byte():
    members = {".iwpv2": b"\x02" * 104, "Index/Document.iwa": b"\x00" + b"\x9a" * 40}
    with _walk():
        assert _kind(members) == "encrypted"


def test_09_method_25452_encrypted():
    assert _kind({"index.xml": b"<sl:document/>", "preview.jpg": JPG}, {"index.xml": 25452}) == "encrypted"
    assert _kind({"index.apxl.gz": gzip.compress(b"<k/>")}, {"index.apxl.gz": 25451}, "key") == "encrypted"


def test_09_gz_cap():
    old = iwork.IWORK_XML
    assert old == 100 * 2**20
    try:
        iwork.IWORK_XML = 100
        data = gzip.compress(b"<sl:document>" + b"x" * 1000 + b"</sl:document>")
        assert _kind({"index.xml.gz": data}, {"index.xml.gz": 8}) == "too_large"
    finally:
        iwork.IWORK_XML = old


def test_index_zip_one_folder_down():
    inner = iwork_zip({"Index/Document.iwa": _doc_member(archive(7, 10000, pb((1, "body"))))})
    with _walk() as seen:
        doc, ctx = _read({"x.pages/Index.zip": inner, "x.pages/preview.jpg": JPG})
    assert [type(b) for b in doc.blocks] == [Para]
    assert 7 in seen[0].objects and seen[0].objects[7][0] == 10000
    assert not ctx.skipped
    with _walk() as seen:
        doc, ctx = _read({"Index.zip": inner})
    assert 7 in seen[0].objects
    # deeper than one folder is not an index: nothing readable, the preview stands in
    doc, ctx = _read({"a/b/Index.zip": inner, "preview.jpg": JPG})
    assert [type(b) for b in doc.blocks] == [Image] and ctx.skipped["document"] == 1


def test_nested_index_zip_refused():
    deep = iwork_zip({"Index/Document.iwa": _doc_member(archive(9, 1, b""))})
    inner = iwork_zip({"Index/Document.iwa": _doc_member(archive(7, 10000, b"")), "Index.zip": deep})
    with _walk() as seen:
        doc, ctx = _read({"Index.zip": inner})
    assert ctx.skipped["document"] == 1
    assert 7 in seen[0].objects and 9 not in seen[0].objects


def test_lzvn_member_counted():
    members = {"Index/Tables/Tile.iwa": b"bvxn" + b"\x00" * 12,
               "Index/Document.iwa": _doc_member(archive(3, 10000, pb((1, "ok"))))}
    with _walk() as seen:
        doc, ctx = _read(members)
    assert ctx.skipped["document"] == 1
    assert seen[0].objects == {3: (10000, pb((1, "ok")))}
    # a member that is not a chunk stream fails the same way
    with _walk() as seen:
        doc, ctx = _read({"Index/Bad.iwa": b"\x07\x01\x00\x00z", "Index/Document.iwa": _doc_member()})
    assert ctx.skipped["document"] == 1 and 1 in seen[0].objects


def test_preview_fallback():
    doc, ctx = _read({"preview.jpg": JPG, "QuickLook/Thumbnail.jpg": THUMB})
    assert len(doc.blocks) == 1 and isinstance(doc.blocks[0], Image) and doc.blocks[0].data == JPG
    assert ctx.skipped["document"] == 1
    # an IWA body no walk reads (WALKS is empty here) falls back too
    doc, ctx = _read({"Index/Document.iwa": _doc_member(), "preview.jpg": JPG})
    assert [b.data for b in doc.blocks] == [JPG] and ctx.skipped["document"] == 1


def test_thumbnail_fallback():
    doc, ctx = _read({"QuickLook/Thumbnail.jpg": THUMB, "buildVersionHistory.plist": b"<plist/>"})
    assert [b.data for b in doc.blocks] == [THUMB] and ctx.skipped["document"] == 1


def test_nothing_damaged():
    assert _kind({"readme.txt": b"hello"}) == "damaged"
    assert _kind({"Index/Document.iwa": b"bvxn" + b"\x00" * 4}) == "damaged"
    # a directory entry needing zip version 19.1: zipfile raises NotImplementedError, read as damaged
    data = bytearray(iwork_zip({"preview.jpg": JPG}))
    cd = data.rindex(b"PK\x01\x02")
    data[cd + 6:cd + 8] = (191).to_bytes(2, "little")
    e = _raises(ReadError, iwork.parse_bytes, bytes(data), Context(), "pages")
    assert e.kind == "damaged"
    # an Index.zip that cannot be opened leaves the preview
    doc, ctx = _read({"Index.zip": bytes(data), "preview.jpg": JPG})
    assert [b.data for b in doc.blocks] == [JPG] and ctx.skipped["document"] == 1


def test_should_merge():
    base = pb((1, 5), (2, "a"))
    patch = pb((1, 7), (2, "b"))
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(4, 12026, base, patch, merge=True)))])
    f = store.get(4)
    assert pbwire.one(f, 1) == 7                           # a single field: the later value wins
    assert [bytes(v) for v in f[2]] == [b"a", b"b"]        # a repeated field grows
    assert store.objects[4] == (12026, base + patch)
    # a singular sub-message merges field by field through msg
    base = pb((1, 5), (3, pb((1, 1), (2, "x"))))
    patch = pb((3, pb((1, 2), (3, "y"))))
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(4, 12026, base, patch, merge=True)))])
    f = store.get(4)
    sub = pbwire.msg(f, 3)
    assert pbwire.one(sub, 1) == 2 and sub[1] == [1, 2]
    assert bytes(pbwire.one(sub, 2)) == b"x" and bytes(pbwire.one(sub, 3)) == b"y"
    assert pbwire.one(f, 1) == 5 and pbwire.msg(f, 9) == {}
    _raises(ValueError, pbwire.msg, f, 1)
    # without should_merge the later messages are ignored
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(4, 12026, base, patch)))])
    assert store.objects[4] == (12026, base) and not ctx.skipped


def test_fields_to_remove_counted():
    base, patch = pb((1, 5)), pb((2, "b"))
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(4, 12026, base, patch, merge=True, removes=True)))])
    assert ctx.skipped["merge"] == 1
    assert store.objects[4] == (12026, base + patch)


def test_duplicate_id_first_kept():
    store, ctx = _load([("Index/A.iwa", iwa_member(archive(4, 1, b"\x08\x01"))),
                        ("Index/B.iwa", iwa_member(archive(4, 2, b"\x08\x02")))])
    assert store.objects == {4: (1, b"\x08\x01")} and ctx.skipped["duplicate"] == 1
    assert store.get(4, 1) == {1: [1]} and store.get(4, 2) is None and store.get(4, (2, 1)) == {1: [1]}
    assert store.get(5) is None


def test_archive_across_chunk_boundary():
    arc = archive(6, 2001, pb((3, "split across chunks")))
    member = chunk(snappy_literal(arc[:5])) + chunk(arc[5:], 1)
    store, ctx = _load([("Index/Document.iwa", member)])
    assert store.objects == {6: (2001, pb((3, "split across chunks")))} and not ctx.skipped


def test_data_reference():
    pkg = {"Data/a-12.png": b"\x89PNGdata", "Index/Metadata.iwa": b""}
    members = [("Index/Metadata.iwa", metadata_iwa([(7, "a-12.png"), (8, "b.png")]))]
    store, ctx = _load(members, pkg)
    assert store.data(7) == b"\x89PNGdata"
    assert store.data(8) is None and ctx.skipped["image"] == 1
    # an Index.zip package keeps its data beside the index, under the same folder
    store, ctx = _load(members, {"x.key/Data/a-12.png": b"img"})
    assert store.data(7) is None
    ctx = Context()
    store = iwork_iwa.load(members, ctx, flate.Budget(10**6), Package(iwork_zip({"x.key/Data/a-12.png": b"img"})),
                           prefix="x.key/")
    assert store.data(7) == b"img" and not ctx.skipped


class _BrokenHeader:
    """A package whose member reads fail as zipfile fails on a broken local header (a negative seek)."""

    def __init__(self, pkg):
        self.pkg = pkg
        self.names = pkg.names

    def __getattr__(self, name):
        return getattr(self.pkg, name)

    def read(self, name):
        raise ValueError("negative seek value -1")


def test_broken_member_header_counted():
    pkg = _BrokenHeader(Package(iwork_zip({"Index/Document.iwa": _doc_member(), "Data/a.png": b"img"})))
    ctx = Context()
    assert list(iwork._members(pkg, ["Index/Document.iwa"], ctx)) == [] and ctx.skipped["document"] == 1
    store = iwork_iwa.load([("Index/Metadata.iwa", metadata_iwa([(7, "a.png")]))], ctx, flate.Budget(10**6), pkg)
    assert store.data(7) is None and ctx.skipped["image"] == 1


def test_iwa_objects_cap():
    old = iwork_iwa.IWA_OBJECTS
    assert old == 1_000_000
    try:
        iwork_iwa.IWA_OBJECTS = 2
        store, ctx = _load([("Index/Document.iwa", iwa_member(*(archive(i, 1, b"") for i in (1, 2, 3))))])
        assert sorted(store.objects) == [1, 2] and ctx.skipped["record"] == 1
        # several archives past the cap, in this member and the next: the parse stops, counted once
        store, ctx = _load([("Index/A.iwa", iwa_member(*(archive(i, 1, b"") for i in range(1, 8)))),
                            ("Index/B.iwa", iwa_member(*(archive(i, 1, b"") for i in range(10, 15))))])
        assert sorted(store.objects) == [1, 2] and ctx.skipped["record"] == 1
        # exactly at the cap nothing is lost
        store, ctx = _load([("Index/A.iwa", iwa_member(archive(1, 1, b""), archive(2, 1, b"")))])
        assert sorted(store.objects) == [1, 2] and not ctx.skipped
    finally:
        iwork_iwa.IWA_OBJECTS = old


def test_follow_self_cycle():
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(5, 1, pb((1, pb_ref(5))))))])
    calls = []

    def visit(i, depth):
        calls.append(i)
        f = store.get(i) or {}
        store.follow([pbwire.ref(v) for v in f.get(1, [])], visit, depth + 1)
        return i

    assert store.follow([5], visit) == [5]
    assert calls == [5] and ctx.skipped["depth"] == 1
    # a long chain stops at MAX_DEPTH, counted once
    n = limits.MAX_DEPTH + 10
    chain = iwa_member(*(archive(i, 1, pb((1, pb_ref(i + 1)))) for i in range(1, n)))
    store, ctx = _load([("Index/Document.iwa", chain)])
    calls = []
    store.follow([1], visit)
    assert len(calls) == limits.MAX_DEPTH and ctx.skipped["depth"] == 1


def test_follow_dag_repeat():
    # 1 -> 2, 3; 2 -> 4; 3 -> 4: object 4 is reached twice, entered once, and no depth is counted
    refs = {1: (2, 3), 2: (4,), 3: (4,), 4: ()}
    member = iwa_member(*(archive(i, 1, pb(*((1, pb_ref(r)) for r in rs))) for i, rs in refs.items()))
    store, ctx = _load([("Index/Document.iwa", member)])
    calls = []

    def visit(i, depth):
        calls.append(i)
        store.follow([pbwire.ref(v) for v in store.get(i).get(1, [])], visit, depth + 1)
        return i

    assert store.follow([1], visit) == [1]
    assert calls == [1, 2, 4, 3] and not ctx.skipped


def test_walk_that_raises():
    def bad(store, ctx):
        raise IndexError("broken table")

    def good(store, ctx):
        return Doc(blocks=[Para([Text("numbers")])])

    old = dict(iwork_iwa.WALKS)
    try:
        iwork_iwa.WALKS.update({"pages": bad, "numbers": good})
        doc, ctx = _read({"Index/Document.iwa": _doc_member()})
        assert doc.blocks[0].inlines[0].text == "numbers" and ctx.skipped["document"] == 1
        # with no other walk, the preview stands in, and the body is counted once
        iwork_iwa.WALKS.pop("numbers")
        doc, ctx = _read({"Index/Document.iwa": _doc_member(), "preview.jpg": JPG})
        assert [b.data for b in doc.blocks] == [JPG] and ctx.skipped["document"] == 1
        assert _kind({"Index/Document.iwa": _doc_member()}) == "damaged"
        # a ReadError is never swallowed
        iwork_iwa.WALKS["pages"] = lambda store, ctx: (_ for _ in ()).throw(ReadError("timeout", "walk"))
        assert _kind({"Index/Document.iwa": _doc_member(), "preview.jpg": JPG}) == "timeout"
    finally:
        iwork_iwa.WALKS.clear()
        iwork_iwa.WALKS.update(old)


def test_iwa_budget_shared():
    old = iwork_iwa.IWA_BYTES
    assert old == 256 * 2**20
    big = iwa_member(archive(1, 1, b"z" * 300))
    try:
        iwork_iwa.IWA_BYTES = 500
        assert _kind({"Index/A.iwa": big, "Index/B.iwa": big, "preview.jpg": JPG}) == "too_large"
        with _walk():
            doc, ctx = _read({"Index/A.iwa": big})
        assert not ctx.skipped
    finally:
        iwork_iwa.IWA_BYTES = old


def test_app_by_extension():
    order = []

    def make(app):
        def fn(store, ctx):
            order.append(app)
            return Doc(blocks=[Para([Text(app)])]) if app == "key" else None
        return fn

    old = dict(iwork_iwa.WALKS)
    try:
        iwork_iwa.WALKS.update({a: make(a) for a in ("pages", "numbers", "key")})
        data = iwork_zip({"Index/Document.iwa": _doc_member()})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "renamed.numbers"
            path.write_bytes(data)
            doc = iwork.parse(path, Context())
        assert order == ["numbers", "pages", "key"] and doc.blocks[0].inlines[0].text == "key"
    finally:
        iwork_iwa.WALKS.clear()
        iwork_iwa.WALKS.update(old)
    assert set(iwork_iwa.WALKS) == {"pages", "numbers", "key"}


# -- Pages text storages and the Pages walk

BODY = 10
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
UTF16_WARNING = "utf16: text positions may be off after characters outside the BMP"


def _pages(text, *more, datas=(), files=None, floating=(), headers=(), footers=(), **kw):
    """A Pages file whose body storage is `text` with the storage keywords `kw`, plus the archives `more`."""
    archives = pages_doc(BODY, floating=floating, headers=headers, footers=footers)
    archives += [archive(BODY, 2001, storage(text, **kw)), *more]
    members = {"Index/Document.iwa": iwa_member(*archives)}
    if datas:
        members["Index/Metadata.iwa"] = metadata_iwa(list(datas))
    members.update(files or {})
    ctx = Context()
    return iwork.parse_bytes(iwork_zip(members), ctx, "pages"), ctx


def _md(doc, ctx, hidden=False):
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)


def test_headings_by_style_name():
    text = "T\nS\nH\nC\nSub\nBare\nBig\nBody"
    starts = [0, 2, 4, 6, 8, 12, 17, 21]
    ids = [20, 21, 22, 24, 25, 26, 27, None]
    doc, ctx = _pages(text, style(20, "Title"), style(21, "Subheading"), style(22, "Heading 4"),
                      style(23, "Heading 2"), style(24, None, parent=23), style(25, "Subtitle"), style(26, "heading"),
                      style(27, "Heading 9"), para_styles=list(zip(starts, ids)))
    assert doc.blocks == [Heading(1, [Text("T")]), Heading(2, [Text("S")]), Heading(4, [Text("H")]),
                          Heading(2, [Text("C")]), Heading(2, [Text("Sub")]), Heading(1, [Text("Bare")]),
                          Heading(6, [Text("Big")]), Para([Text("Body")])], doc.blocks
    assert not ctx.skipped and not ctx.warnings


def test_style_chain_and_cycle():
    # 100 has 8 parents (101..108), the named one last: reached. 200 has 9 (201..209): the chain stops first.
    near = [style(100 + i, None, parent=101 + i) for i in range(8)] + [style(108, "Heading 3")]
    far = [style(200 + i, None, parent=201 + i) for i in range(9)] + [style(209, "Heading 3")]
    loops = [style(300, None, parent=300), style(301, None, parent=302), style(302, None, parent=301)]
    doc, ctx = _pages("a\nb\nc\nd", *near, *far, *loops, para_styles=[(0, 100), (2, 200), (4, 300), (6, 301)])
    assert doc.blocks == [Heading(3, [Text("a")]), Para([Text("b")]), Para([Text("c")]), Para([Text("d")])], doc.blocks
    assert not ctx.skipped


def test_list_items():
    doc, ctx = _pages("one\ntwo\nthree\nplain", list_style(60, [2, 3]), list_style(61, [], parent=60),
                      lists=[(0, 0, 60), (4, 1, 60), (8, 0, 61), (14, 0, None)])
    assert doc.blocks == [ListItem(0, "-", [Text("one")]), ListItem(1, "1.", [Text("two")]),
                          ListItem(0, "-", [Text("three")]), Para([Text("plain")])], doc.blocks
    # label type 0 at a depth is plain text
    doc, ctx = _pages("x", list_style(62, [0]), lists=[(0, 0, 62)])
    assert doc.blocks == [Para([Text("x")])]
    assert "- one\n  1. two" in _md(*_pages("one\ntwo", list_style(60, [2, 3]), lists=[(0, 0, 60), (4, 1, 60)]))


def test_bold_from_char_style():
    doc, ctx = _pages("plain bold end\ninherit not", style(70, None, bold=True, type=2021),
                      style(71, None, parent=72, type=2021), style(72, "Strong", bold=True, type=2021),
                      style(73, None, parent=72, bold=False, type=2021),
                      char_styles=[(6, 70), (10, None), (15, 71), (22, 73)])
    assert doc.blocks == [Para([Text("plain "), Bold([Text("bold")]), Text(" end")]),
                          Para([Bold([Text("inherit")]), Text(" not")])], doc.blocks
    assert "plain **bold** end" in _md(doc, ctx)


def test_links():
    doc, ctx = _pages("A B C D E", link(80, "https://x.test/"), link(81, "mailto:a@b.test"),
                      link(82, "x-apple-data-detectors://3"), link(83, "HTTP://y.test"),
                      links=[(2, 80), (3, None), (4, 81), (5, None), (6, 82), (7, None), (8, 83)])
    assert doc.blocks[0].inlines == [Text("A "), Link("https://x.test/", [Text("B")]), Text(" "),
                                     Link("mailto:a@b.test", [Text("C")]), Text(" D "),
                                     Link("HTTP://y.test", [Text("E")])], doc.blocks[0].inlines
    assert "[B](https://x.test/)" in _md(doc, ctx)


def test_footnote_noteref():
    doc, ctx = _pages("Body\ufffc end", footnote(90, 91), archive(91, 2001, storage("The note.")),
                      footnotes=[(4, 90)])
    assert doc.blocks == [Para([Text("Body"), NoteRef("fn-1"), Text(" end")])], doc.blocks
    assert doc.notes == {"fn-1": [Para([Text("The note.")])]}
    out = _md(doc, ctx)
    assert "Body[^1] end" in out and "[^1]: The note." in out, out
    # a footnote object that is missing loses its note: counted
    doc, ctx = _pages("Body\ufffc", footnotes=[(4, 92)])
    assert doc.blocks == [Para([Text("Body")])] and ctx.skipped["note"] == 1


def test_comment_note():
    doc, ctx = _pages("Look here please", highlight(95, 96), comment(96, "Fix this", author=97, replies=[98]),
                      author(97, "Ann"), comment(98, "Done", author=99), author(99, "Bo"),
                      comments=[(5, 95), (9, None)])
    assert doc.blocks[0].inlines == [Text("Look here"), NoteRef("cm-1"), Text(" please")], doc.blocks[0].inlines
    assert doc.notes == {"cm-1": [Para([Text("Comment by Ann: "), Text("Fix this")]),
                                  Para([Text("Comment by Bo: "), Text("Done")])]}, doc.notes
    out = _md(doc, ctx)
    assert "[^1]: Comment by Ann: Fix this Comment by Bo: Done" in out, out
    # a reply that points back at its thread is read once
    doc, ctx = _pages("x y", highlight(95, 96), comment(96, "Loop", replies=[96]), comments=[(0, 95)])
    assert doc.notes == {"cm-1": [Para([Text("Comment: "), Text("Loop")])]}, doc.notes


def test_tracked_deletion_hidden():
    doc, ctx = _pages("Keep gone stay", change(100, 2), change(101, 1),
                      deletions=[(5, 100), (10, None)], insertions=[(0, 101), (5, None)])
    assert doc.blocks[0].inlines == [Text("Keep "), Hidden([Text("gone ")], "deleted"), Text("stay")], \
        doc.blocks[0].inlines
    out = _md(doc, ctx)
    assert "gone" not in out and "Keep stay" in out and "(1 hidden places left out)" in out, out
    shown = _md(doc, ctx, hidden=True)
    assert "(hidden: gone )" in shown, shown
    # a deletion table that cannot be read fails the storage rather than showing deleted text as body text
    body = archive(BODY, 2001, pb((3, "secret"), (22, b"\x0f")))
    assert _kind({"Index/Document.iwa": iwa_member(*pages_doc(BODY), body)}) == "damaged"


def test_attachment_after_paragraph():
    doc, ctx = _pages("Before\ufffc words\nAfter", attachment(110, 111), image(111, 7),
                      datas=[(7, "pic.png")], files={"Data/pic.png": PNG}, attachments=[(6, 110)])
    assert doc.blocks == [Para([Text("Before words")]), Image(PNG), Para([Text("After")])], doc.blocks
    assert not ctx.skipped
    # an image whose data is not in the package is counted once, even with several renditions named
    doc, ctx = _pages("x\ufffc", attachment(110, 111), archive(111, 3005, pb((15, pb_ref(8)), (11, pb_ref(9)))),
                      attachments=[(1, 110)])
    assert doc.blocks == [Para([Text("x")])] and ctx.skipped == {"image": 1}, ctx.skipped


def test_outside_bmp_warning_text_intact():
    # UTF-16: a 0, the emoji 1-2, b 3, newline 4; c 5, the emoji 6-7, d 8. 7 falls inside the emoji's pair.
    doc, ctx = _pages("a\U0001F600b\nc\U0001F600d\ufffc", style(70, None, bold=True, type=2021),
                      footnote(90, 91), archive(91, 2001, storage("note \U0001F600")),
                      char_styles=[(3, 70), (4, None), (7, 70), (9, None), (999, 70)], footnotes=[(9, 90)])
    assert doc.blocks == [Para([Text("a\U0001F600"), Bold([Text("b")])]),
                          Para([Text("c"), Bold([Text("\U0001F600d")]), NoteRef("fn-1")])], doc.blocks
    assert ctx.warnings.count(UTF16_WARNING) == 1 and ctx.warnings == [UTF16_WARNING]
    assert not ctx.skipped
    # BMP text adds no warning
    doc, ctx = _pages("plain \u00e9", char_styles=[(1, None)])
    assert ctx.warnings == []


def test_equation_object():
    doc, ctx = _pages("x\ufffc", attachment(120, 121), archive(121, 2015, b""), attachments=[(1, 120)])
    assert doc.blocks == [Para([Text("x")])] and ctx.skipped == {"object": 1}, ctx.skipped


def test_pages_headers_footers():
    doc, ctx = _pages("Body", archive(130, 2001, storage("Head")), archive(131, 2001, storage(["Fo", "ot"])),
                      headers=[130], footers=[131], sections=[(0, SECTION)])
    assert doc.blocks == [Para([Text("Body")])]
    assert doc.headers == ["Head"] and doc.footers == ["Foot"], (doc.headers, doc.footers)
    out = _md(doc, ctx)
    assert "Header: Head" in out and "Footer: Foot" in out, out


def test_floating_drawables_after_body():
    doc, ctx = _pages("Body\ufffc", attachment(110, 140), image(140, 7), group(142, [141, 9999]),
                      shape(141, 150), archive(150, 2001, storage("Box text")), archive(143, 3005, b"\x0f"),
                      shape(144, 151, field=2), archive(151, 2001, storage("Old box")),
                      archive(9999, 4242, b""),
                      datas=[(7, "pic.png")], files={"Data/pic.png": PNG}, attachments=[(4, 110)],
                      floating=[142, 143, 140, 144])
    assert doc.blocks == [Para([Text("Body")]), Image(PNG), Para([Text("Box text")]), Para([Text("Old box")])], \
        doc.blocks
    assert ctx.skipped == {"image": 1}, ctx.skipped


def test_shape_cycle():
    # a text box whose storage anchors the same text box: entered once, the cycle counted
    doc, ctx = _pages("Body\ufffc", attachment(110, 141), shape(141, 150), attachment(111, 141),
                      archive(150, 2001, storage("Inner\ufffc", attachments=[(5, 111)])), attachments=[(4, 110)])
    assert doc.blocks == [Para([Text("Body")]), Para([Text("Inner")])] and ctx.skipped == {"depth": 1}, \
        (doc.blocks, ctx.skipped)


def test_inline_runs_linear():
    # 50,000 run entries that change nothing in one paragraph: one Text, joined once
    n, step = 50_000, 40
    t0 = time.perf_counter()
    doc, ctx = _pages("a" * (n * step), char_styles=[(i * step, None) for i in range(n)])
    took = time.perf_counter() - t0
    assert doc.blocks == [Para([Text("a" * (n * step))])] and not ctx.skipped
    assert took < 2, took


def test_comment_thread_once():
    # 1,000 highlights on one thread of 1,000 replies: one note, every highlight marks it
    h, r = 1000, 1000
    replies = [comment(1000 + i, "r%d" % i) for i in range(r)]
    marks = [highlight(100_000 + i, 999) for i in range(h)]
    t0 = time.perf_counter()
    doc, ctx = _pages("x" * h, comment(999, "root", replies=[1000 + i for i in range(r)]), *replies, *marks,
                      comments=[(i, 100_000 + i) for i in range(h)])
    took = time.perf_counter() - t0
    assert list(doc.notes) == ["cm-1"] and len(doc.notes["cm-1"]) == r + 1
    refs = [it for it in doc.blocks[0].inlines if isinstance(it, NoteRef)]
    assert len(refs) == h and {x.id for x in refs} == {"cm-1"} and not ctx.skipped
    assert took < 2, took
    # a highlight on a reply already in a thread marks that thread's note
    doc, ctx = _pages("a b", comment(999, "root", replies=[1000]), comment(1000, "reply"), highlight(95, 999),
                      highlight(96, 1000), comments=[(0, 95), (1, None), (2, 96)])
    assert list(doc.notes) == ["cm-1"] and doc.blocks[0].inlines == [Text("a"), NoteRef("cm-1"), Text(" b"),
                                                                     NoteRef("cm-1")], doc.blocks[0].inlines


def test_list_depths_parse_once():
    depths = list(range(9)) * 20
    text = "\n".join("i%d" % k for k in range(len(depths)))
    starts, at = [], 0
    for k in range(len(depths)):
        starts.append(at)
        at += len("i%d" % k) + 1
    calls = []
    real = iwork_text.TextReader._label_types

    def counting(self, sid):
        calls.append(sid)
        return real(self, sid)

    real_chain = iwork_text.TextReader._chain
    chains = []

    def chain(self, sid, type):
        chains.append(type)
        return real_chain(self, sid, type)

    try:
        iwork_text.TextReader._label_types = counting
        iwork_text.TextReader._chain = chain
        doc, ctx = _pages(text, list_style(60, [2, 3] * 4 + [0]), lists=[(s, d, 60) for s, d in zip(starts, depths)])
    finally:
        iwork_text.TextReader._label_types = real
        iwork_text.TextReader._chain = real_chain
    want = [ListItem(d, "-" if d % 2 == 0 else "1.", [Text("i%d" % k)]) if d < 8 else Para([Text("i%d" % k)])
            for k, d in enumerate(depths)]
    assert doc.blocks == want, doc.blocks[:10]
    assert len(calls) == len(depths) and chains.count(2023) == 1, (len(calls), chains.count(2023))


def test_body_failure_keeps_page():
    # a body whose deletion table cannot be read counts `document` once; the floating image and the header stay
    body = archive(BODY, 2001, pb((3, "secret"), (17, pb((1, pb((1, 0), (2, pb_ref(SECTION)))))), (22, b"\x0f")))
    archives = pages_doc(BODY, floating=[140], headers=[130])
    archives += [body, image(140, 7), archive(130, 2001, storage("Head"))]
    members = {"Index/Document.iwa": iwa_member(*archives), "Index/Metadata.iwa": metadata_iwa([(7, "pic.png")]),
               "Data/pic.png": PNG}
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip(members), ctx, "pages")
    assert doc.blocks == [Image(PNG)] and ctx.skipped == {"document": 1}, (doc.blocks, ctx.skipped)
    assert doc.headers == ["Head"] and "secret" not in _md(doc, ctx)
    # with only a preview left, the body is still counted once
    members = {"Index/Document.iwa": iwa_member(*pages_doc(BODY), archive(BODY, 2001, pb((3, "x"), (22, b"\x0f")))),
               "preview.jpg": JPG}
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip(members), ctx, "pages")
    assert [b.data for b in doc.blocks] == [JPG] and ctx.skipped == {"document": 1}, ctx.skipped


def test_header_notes_counted():
    # a footnote and a comment in a header count `note` and add no note: their marks are never shown
    doc, ctx = _pages("Body", archive(130, 2001, storage("Head￼", footnotes=[(4, 90)])), footnote(90, 91),
                      archive(91, 2001, storage("hidden note")),
                      archive(131, 2001, storage("Foot", comments=[(0, 95)])), highlight(95, 96), comment(96, "c"),
                      headers=[130], footers=[131], sections=[(0, SECTION)])
    assert doc.headers == ["Head"] and doc.footers == ["Foot"] and doc.notes == {}, (doc.headers, doc.notes)
    assert ctx.skipped == {"note": 2}, ctx.skipped


def test_header_siblings_and_floating_ids():
    # one broken template counts `header` once and its sibling is read; a floating id that names the header storage
    # is not a drawable, so it never hides the header
    archives = [archive(1, 10000, pb((4, pb_ref(BODY)), (20, pb_ref(5)))), archive(5, 10015, pb((1, pb_ref(130)))),
                archive(BODY, 2001, storage("Body", sections=[(0, 3)])),
                archive(3, 10011, pb((23, pb_ref(4)), (24, pb_ref(6)))), archive(4, 10143, b"\x0f"),
                archive(6, 10143, pb((1, pb_ref(130)))), archive(130, 2001, storage("Head"))]
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip({"Index/Document.iwa": iwa_member(*archives)}), ctx, "pages")
    assert doc.blocks == [Para([Text("Body")])] and doc.headers == ["Head"], (doc.blocks, doc.headers)
    assert ctx.skipped == {"header": 1}, ctx.skipped


def test_unordered_run_table():
    # entries out of order are sorted stably: at an equal index the later entry wins
    doc, ctx = _pages("ab cd\nef", style(70, None, bold=True, type=2021), style(71, None, bold=False, type=2021),
                      style(20, "Heading 2"), char_styles=[(3, 71), (0, None), (3, 70), (5, None)],
                      para_styles=[(6, 20), (0, None)])
    assert doc.blocks == [Para([Text("ab "), Bold([Text("cd")])]), Heading(2, [Text("ef")])], doc.blocks
    doc, ctx = _pages("ab cd", style(70, None, bold=True, type=2021), style(71, None, bold=False, type=2021),
                      char_styles=[(3, 70), (0, None), (3, 71)])
    assert doc.blocks == [Para([Text("ab cd")])], doc.blocks


def test_repeat_blocks():
    # one storage asked for again with repeat gives its blocks from the first read, never walking it again
    store, ctx = _load([("Index/Document.iwa", iwa_member(archive(BODY, 2001, storage("shared"))))])
    reader = iwork_text.TextReader(store, ctx, {}, lambda d: [])
    walks = []
    real = reader._storage
    reader._storage = lambda i, d: walks.append(i) or real(i, d)
    first = reader.blocks(BODY)
    assert first == [Para([Text("shared")])]
    assert reader.blocks(BODY) == []                      # once only, as before
    again = [reader.blocks(BODY, repeat=True) for _ in range(1000)]
    assert all(a == first for a in again) and walks == [BODY], walks
    assert reader.blocks(999, repeat=True) == [] and not ctx.skipped
    # comment_thread: one note a thread, the same id from any of its comments, None for an empty one
    store, ctx = _load([("Index/Document.iwa", iwa_member(comment(1, "a", replies=[2]), comment(2, "b"),
                                                         comment(3, "  ")))])
    notes = {}
    reader = iwork_text.TextReader(store, ctx, notes, lambda d: [])
    assert [reader.comment_thread(i) for i in (1, 2, 1)] == ["cm-1"] * 3
    assert reader.comment_thread(3) is None and reader.comment_thread(None) is None and list(notes) == ["cm-1"]


# -- Numbers tables and the Numbers walk

def _numbers(*archives, sheets=None):
    """A Numbers file: one sheet "S" holding table 600 unless `sheets` says otherwise, plus the archives."""
    members = {"Index/Document.iwa": iwa_member(*numbers_doc(sheets or [("S", [600])]), *archives)}
    ctx = Context()
    return iwork.parse_bytes(iwork_zip(members), ctx, "numbers"), ctx


def _flat(v) -> str:
    """A cell value (str or inlines) as plain text, note marks as [^id]."""
    if isinstance(v, str):
        return v
    out = []
    for x in v:
        if isinstance(x, Text):
            out.append(x.text)
        elif isinstance(x, NoteRef):
            out.append(f"[^{x.id}]")
        elif isinstance(x, (Bold, Link, Hidden)):
            out.append(_flat(x.inlines))
    return "".join(out)


@contextmanager
def _puts():
    """Every value Grid.put is given, as {(row, col): text}: the table's own row numbers, before any layout."""
    seen = {}
    real = sheet_grid.Grid.put

    def put(self, row, col, value):
        seen[(row, col)] = value
        return real(self, row, col, value)

    sheet_grid.Grid.put = put
    try:
        yield seen
    finally:
        sheet_grid.Grid.put = real


@contextmanager
def _limit(mod, name, value):
    old = getattr(mod, name)
    setattr(mod, name, value)
    try:
        yield
    finally:
        setattr(mod, name, old)


def _tables(doc) -> list:
    return [b for s in doc.blocks for b in s.blocks if isinstance(b, Table)]


def _text_cell(key):
    return cell_v5(3, string_id=key)


def test_two_tiles_row_past_256():
    t = table("T", 300, 2, tiles={0: tile({3: [_text_cell(1), None]}), 1: tile({2: [None, _text_cell(2)]})},
              strings=[(1, "a"), (2, "b")])
    with _puts() as seen:
        doc, ctx = _numbers(*t)
    assert {k: _flat(v) for k, v in seen.items()} == {(3, 0): "a", (258, 1): "b"}, seen
    [tab] = _tables(doc)
    assert tab.caption == "T" and ctx.skipped == {"repeat": 1}, ctx.skipped  # Grid shrinks the 254 empty rows


def test_tile_size_bounds():
    for size in (0, 70_000):
        doc, ctx = _numbers(*table("T", 10, 1, tiles={0: tile({0: [_text_cell(1)]})}, strings=[(1, "a")],
                                   tile_size=size))
        assert _tables(doc) == [] and ctx.skipped == {"table": 1}, (size, ctx.skipped)
        assert [s.title for s in doc.blocks] == ["S"]
    for size, row in ((None, 256), (65_536, 65_536), (1, 1)):
        with _puts() as seen:
            doc, ctx = _numbers(*table("T", 100_000, 1, tiles={1: tile({0: [_text_cell(1)]})}, strings=[(1, "a")],
                                       tile_size=size))
        assert list(seen) == [(row, 0)] and not ctx.skipped, (size, seen, ctx.skipped)


def test_v5_each_type():
    cells = [cell_v5(2, value="1.5"), cell_v5(10, value=3, format_id=1), _text_cell(1), cell_v5(9, rich_id=1),
             cell_v5(5, value=86400), cell_v5(6, value=1.0), cell_v5(6, value=0.0), cell_v5(7, value=3725),
             cell_v5(8), cell_v5(0)]
    t = table("T", 1, len(cells), tiles={0: tile({0: cells})}, strings=[(1, "word")], rich=[(1, 700)],
              formats=[(1, fmt(257, decimals=2, currency="USD"))])
    with _puts() as seen:
        doc, ctx = _numbers(*t, archive(700, 2001, storage("see site", links=[(0, 710), (8, None)])),
                            link(710, "https://example.com"))
    got = [_flat(seen.get((0, c), "")) for c in range(len(cells))]
    assert got == ["1.5", "$3.00", "word", "see site", "2001-01-02", "TRUE", "FALSE", "1:02:05", "#ERROR", ""], got
    assert seen[(0, 3)] == [Link("https://example.com", [Text("see site")])], seen[(0, 3)]
    assert not ctx.skipped, ctx.skipped
    assert iwork_tables.decimal128(decimal128_bytes("-0.125")) == iwork_tables.Decimal("-0.125")
    assert iwork_tables.decimal128(decimal128_bytes("0")) == 0 and iwork_tables.decimal128(b"\0" * 16) == 0
    assert iwork_tables.decimal128(decimal128_bytes("12345678901234567890123456789012")) == \
        iwork_tables.Decimal("12345678901234567890123456789012")
    _raises(ValueError, iwork_tables.decimal128, b"\0" * 15 + b"\x78")  # infinity / NaN
    _raises(ValueError, iwork_tables.decimal128, b"\0" * 15)


def test_v4_row():
    # only fields 3 and 4: the version 4 cells, with docling's checks (a missing string key and a type the layout
    # does not carry give nothing; a cell whose identifiers run past the buffer gives nothing)
    cells = [cell_v4(3, string_id=1), cell_v4(2, value=2.5), cell_v4(3, string_id=99), cell_v4(9),
             cell_v4(2, value=7.0, ids=3), cell_v4(3, string_id=1, ids=2)]
    t = table("T", 2, 7, tiles={0: tile({0: cells, 1: [cell_v4(3, string_id=1)[:-2]]}, pre_bnc=True)},
              strings=[(1, "a")])
    with _puts() as seen:
        doc, ctx = _numbers(*t)
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "a", (0, 1): "2.5", (0, 4): "7", (0, 5): "a"}, seen
    assert not ctx.skipped, ctx.skipped


def test_v4_v5_side_by_side():
    v4 = table("Old", 1, 2, tiles={0: tile({0: [cell_v4(3, string_id=1), cell_v4(2, value=4.0)]}, pre_bnc=True)},
               strings=[(1, "old")], id=600)
    v5 = table("New", 1, 2, tiles={0: tile({0: [cell_v5(3, string_id=1), cell_v5(2, value=5)]})},
               strings=[(1, "new")], id=800)
    doc, ctx = _numbers(*v4, *v5, sheets=[("S", [600, 800])])
    md = _md(doc, ctx)
    assert [t.caption for t in _tables(doc)] == ["Old", "New"] and not ctx.skipped, ctx.skipped
    assert "Old\n|old|4|" in md and "New\n|new|5|" in md, md


def test_wide_offsets():
    # offsets x4 when field 8 is set: cells of 13 bytes are padded to 16, so only the x4 reading finds them
    cells = [cell_v5(3, string_id=1), cell_v5(3, string_id=2), cell_v5(2, value=9)]
    with _puts() as seen:
        doc, ctx = _numbers(*table("T", 1, 3, tiles={0: tile({0: cells}, wide=True)}, strings=[(1, "x"), (2, "y")]))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "x", (0, 1): "y", (0, 2): "9"} and not ctx.skipped


def test_merges():
    rows = {0: [_text_cell(1), _text_cell(2), _text_cell(2)], 1: [None, None, _text_cell(3)],
            2: [_text_cell(3), _text_cell(3), _text_cell(3)]}
    doc, ctx = _numbers(*table("T", 3, 3, tiles={0: tile(rows)}, strings=[(1, "big"), (2, "r"), (3, "z")],
                               merges=[(0, 0, 2, 2)]))
    [tab] = _tables(doc)
    first = tab.rows[0][0]
    assert (first.rowspan, first.colspan) == (2, 2) and first.blocks == [Para([Text("big")])], tab.rows
    assert len(tab.rows[0]) == 2 and len(tab.rows[1]) == 1 and len(tab.rows[2]) == 3, tab.rows
    assert not ctx.skipped, ctx.skipped


def test_hidden_row_col():
    rows = {r: [cell_v5(3, string_id=1 + 3 * r + c) for c in range(3)] for r in range(3)}
    strings = [(1 + i, "v%d" % i) for i in range(9)]
    doc, ctx = _numbers(*table("T", 3, 3, tiles={0: tile(rows)}, strings=strings, hidden_rows=[1],
                               hidden_cols=[2]))
    [tab] = _tables(doc)
    assert tab.hidden_rows == frozenset({1}) and tab.hidden_cols == frozenset({2}), tab
    md = _md(doc, ctx)
    assert "v0" in md and "v3" not in md and "v2" not in md and "v6" in md, md
    shown = _md(doc, ctx, hidden=True)
    assert "v3" in shown and "v2" in shown and "(hidden)" in shown, shown
    # hidingState 0 hides nothing
    doc, ctx = _numbers(*table("T", 3, 3, tiles={0: tile(rows)}, strings=strings, hidden_rows=[1], hiding=0))
    assert _tables(doc)[0].hidden_rows == frozenset() and "v3" in _md(doc, ctx)


def _code(type, **kw):
    return iwork_tables.format_code(pbwire.fields(fmt(type, **kw)))


def test_formats():
    from reader.numfmt import display
    assert _code(256, decimals=2) == "0.00" and display(1.5, _code(256, decimals=2)) == "1.50"
    assert display(1234567, _code(256, decimals=0, thousands=True)) == "1,234,567"
    assert _code(256, decimals=0, thousands=True) == "#,##0"
    assert _code(256, decimals=253) == "General"
    assert display(3, _code(257, decimals=2, currency="USD")) == "$3.00"
    assert display(1000, _code(257, decimals=0, currency="KRW", thousands=True)) == "₩1,000"
    assert display(3, _code(257, decimals=0, currency="CHF")) == "CHF 3"
    assert display(0.255, _code(258, decimals=1)) == "25.5%"
    assert display(36893.75, _code(261, pattern="yyyy-MM-dd")) == "2001-01-02"
    assert display(36893.75, _code(261, pattern="EEEE, MMMM d, yyyy h:mm a")) == "Tuesday, January 2, 2001 6:00 PM"
    assert display(36893.5, _code(261, pattern="dd/MM/yy HH:mm:ss 'at' EEE")) == "02/01/01 12:00:00 at Tue"
    assert _code(261, pattern="QQQ yyyy") == "yyyy-mm-dd" and _code(261) == "yyyy-mm-dd"
    assert _code(270) is None and _code(1) and _code(263)
    # end to end: each format on a cell
    formats = [(1, fmt(256, decimals=2)), (2, fmt(256, decimals=0, thousands=True)), (3, fmt(256, decimals=253)),
               (4, fmt(257, decimals=0, currency="KRW", thousands=True)), (5, fmt(258, decimals=253)),
               (6, fmt(261, pattern="yyyy-MM-dd")), (7, fmt(261, pattern="QQQ yyyy")), (8, fmt(1)), (9, fmt(263)),
               (10, fmt(270)), (11, fmt(258, decimals=0))]
    cells = [cell_v5(2, value="1.5", format_id=1), cell_v5(2, value=1234567, format_id=2),
             cell_v5(2, value="0.1", format_id=3), cell_v5(10, value=1000, format_id=4),
             cell_v5(2, value="0.255", format_id=5), cell_v5(5, value=86400 + 64800, format_id=6),
             cell_v5(5, value=86400 + 64800, format_id=7), cell_v5(5, value=86400, format_id=7),
             cell_v5(6, value=1.0, format_id=8), cell_v5(6, value=0.0, format_id=9),
             cell_v5(2, value="1.25", format_id=10), cell_v5(2, value=1, format_id=8, format_flag="number_format"),
             cell_v5(2, value="0.25", format_id=11)]
    with _puts() as seen:
        doc, ctx = _numbers(*table("T", 1, len(cells), tiles={0: tile({0: cells})}, formats=formats))
    got = [_flat(seen.get((0, c), "")) for c in range(len(cells))]
    assert got == ["1.50", "1,234,567", "0.1", "₩1,000", "25.5%", "2001-01-02", "2001-01-02 18:00:00",
                   "2001-01-02", "TRUE", "FALSE", "1.25", "TRUE", "25%"], got
    assert not ctx.skipped, ctx.skipped


def test_cell_comment_footnote():
    cells = [cell_v5(3, string_id=1, comment_id=1), cell_v5(0, comment_id=2), _text_cell(1)]
    doc, ctx = _numbers(*table("T", 1, 3, tiles={0: tile({0: cells})}, strings=[(1, "a")],
                               comments=[(1, 900), (2, 902)]),
                        comment(900, "check this", author=901, replies=[903]), author(901, "Ann"),
                        comment(903, "done"), comment(902, "empty cell note"))
    [tab] = _tables(doc)
    row = tab.rows[0]
    assert row[0].blocks == [Para([Text("a"), NoteRef("cm-1")])], row
    assert row[1].blocks == [Para([NoteRef("cm-2")])], row
    assert doc.notes["cm-1"] == [Para([Text("Comment by Ann: "), Text("check this")]),
                                 Para([Text("Comment: "), Text("done")])], doc.notes
    md = _md(doc, ctx)
    assert "a[^1]" in md and "[^1]: Comment by Ann: check this" in md and "empty cell note" in md, md
    assert not ctx.skipped, ctx.skipped


def test_false_tileid_rows():
    t0 = time.perf_counter()
    with _puts() as seen:
        doc, ctx = _numbers(*table("T", 10, 1, tiles={0: tile({0: [_text_cell(1)]}), 10**6: tile({0: [_text_cell(2)]}),
                                                       10**12: tile({5: [_text_cell(2)]})},
                                   strings=[(1, "a"), (2, "b")]))
    assert time.perf_counter() - t0 < 1
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "a"} and ctx.skipped == {"sheet_cut": 1}, ctx.skipped
    assert _tables(doc)[0].rows[0][0].blocks == [Para([Text("a")])]


def test_styled_empty_cells_scan():
    rows = {0: [_text_cell(1), _text_cell(1)], 1: [cell_v5(0)] * 20, 2: [_text_cell(1)]}
    with _limit(limits, "SHEET_SCAN", 10):
        with _puts() as seen:
            doc, ctx = _numbers(*table("T", 3, 20, tiles={0: tile(rows)}, strings=[(1, "a")]))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "a", (0, 1): "a"}, seen
    assert ctx.skipped == {"sheet_cut": 1} and any("unpacked cells" in w for w in ctx.warnings), ctx.warnings
    # exactly SHEET_SCAN cells unpacked is not a cut
    with _limit(limits, "SHEET_SCAN", 3):
        doc, ctx = _numbers(*table("T", 3, 3, tiles={0: tile({0: [_text_cell(1)] * 3})}, strings=[(1, "a")]))
    assert not ctx.skipped, ctx.skipped


def test_table_segments_cap():
    cells = [_text_cell(1), _text_cell(2), _text_cell(3)]
    segs = [segment(650, [string_entry(2, "two")]), segment(651, [string_entry(3, "three")])]
    t = table("T", 1, 3, tiles={0: tile({0: cells})}, strings=[(1, "one")], string_segments=[650, 650, 651])
    with _puts() as seen:
        doc, ctx = _numbers(*t, *segs)
    assert [_flat(seen[(0, c)]) for c in range(3)] == ["one", "two", "three"] and not ctx.skipped, ctx.skipped
    with _limit(iwork_tables, "TABLE_SEGMENTS", 1):
        with _puts() as seen:
            doc, ctx = _numbers(*t, *segs)
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "one", (0, 1): "two"}, seen
    assert ctx.skipped == {"cells": 1}, ctx.skipped


def test_numbers_sheets():
    a = table("Ta", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, strings=[(1, "alpha")], id=600)
    b = table("Tb", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, strings=[(1, "beta")], id=800)
    doc, ctx = _numbers(*a, *b, sticky(950, 951), comment(951, "remember", replies=[952]), comment(952, "ok"),
                        archive(960, 5021, pb((1, b""))), shape(961, 962), archive(962, 2001, storage("box text")),
                        sheets=[("A", [600]), ("B", [800, 950, 960, 961])])
    assert [(s.kind, s.title) for s in doc.blocks] == [("sheet", "A"), ("sheet", "B")]
    assert not doc.notes and len(doc.blocks[1].comments) == 2, (doc.notes, doc.blocks[1].comments)
    md = _md(doc, ctx)
    assert "## Sheet: A" in md and "## Sheet: B" in md and "alpha" in md and "beta" in md and "box text" in md, md
    assert "> Comment: remember" in md and "> Comment: ok" in md, md
    assert ctx.skipped == {"chart": 1}, ctx.skipped  # the chart holds no ChartArchive (extension 10000)


def test_numbers_sheet_and_table_failures():
    # a table whose model is missing counts `table`; a sheet that is not a message counts `sheet`; the rest is read
    good = table("T", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, strings=[(1, "kept")], id=800)
    members = {"Index/Document.iwa": iwa_member(*numbers_doc([("A", [600, 800]), ("B", [])]),
                                                archive(600, 6000, pb((2, pb_ref(12345)))), *good)}
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip(members), ctx, "numbers")
    assert [t.caption for t in _tables(doc)] == ["T"] and ctx.skipped == {"table": 1}, ctx.skipped
    members = {"Index/Document.iwa": iwa_member(archive(1, 1, pb((1, pb_ref(3)), (1, pb_ref(4)))),
                                                archive(3, 2, b"\x0a\x05ab"), archive(4, 2, pb((1, "B"))))}
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip(members), ctx, "numbers")
    assert [s.title for s in doc.blocks] == ["B"] and ctx.skipped == {"sheet": 1}, ctx.skipped


def test_broken_shared_parts():
    # one broken string list named by two tables: each table fails (`table`), the list is parsed once
    shared = archive(5001, 6005, b"\x1a\tab")  # field 3 claims 9 bytes and 2 follow
    a = table("A", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, string_list=5001, id=600)
    b = table("B", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, string_list=5001, id=800)
    parses = []
    real = iwork_iwa.Store.get

    def get(self, id, type=None):
        if id == 5001:
            parses.append(id)
        return real(self, id, type)

    iwork_iwa.Store.get = get
    try:
        doc, ctx = _numbers(*a, *b, shared, sheets=[("S", [600, 800])])
    finally:
        iwork_iwa.Store.get = real
    assert _tables(doc) == [] and ctx.skipped == {"table": 2} and parses == [5001], (ctx.skipped, parses)
    # a good list shared by two tables serves both
    good = archive(5002, 6005, pb((1, 1), (3, string_entry(1, "x"))))
    a = table("A", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, string_list=5002, id=600)
    b = table("B", 1, 1, tiles={0: tile({0: [_text_cell(1)]})}, string_list=5002, id=800)
    doc, ctx = _numbers(*a, *b, good, sheets=[("S", [600, 800])])
    assert [t.rows[0][0].blocks for t in _tables(doc)] == [[Para([Text("x")])]] * 2 and not ctx.skipped
    # a broken rich text storage leaves its cell empty (`record`); the table is kept
    cells = [cell_v5(9, rich_id=1), _text_cell(1)]
    doc, ctx = _numbers(*table("T", 1, 2, tiles={0: tile({0: cells})}, strings=[(1, "kept")], rich=[(1, 700)]),
                        archive(700, 2001, b"\x1a\tab"))
    [tab] = _tables(doc)
    assert [c.blocks for c in tab.rows[0]] == [[Para([Text("kept")])]], tab.rows
    assert ctx.skipped == {"record": 1}, ctx.skipped


def test_pages_table():
    # a Pages table (6007, its 6000 message nested in field 1) anchored in the body
    t = table("PT", 1, 2, tiles={0: tile({0: [_text_cell(1), _text_cell(2)]})}, strings=[(1, "p"), (2, "q")],
              info_type=6007)
    doc, ctx = _pages("Body￼", attachment(110, 600), *t, attachments=[(4, 110)])
    assert doc.blocks[0] == Para([Text("Body")]) and isinstance(doc.blocks[1], Table), doc.blocks
    assert doc.blocks[1].caption == "PT" and not ctx.skipped, ctx.skipped


def test_shared_rich_storage():
    # two cells on one rich-text storage (the TableDataList refcount) both show it
    cells = [cell_v5(9, rich_id=1), cell_v5(9, rich_id=2)]
    with _puts() as seen:
        doc, ctx = _numbers(*table("T", 1, 2, tiles={0: tile({0: cells})}, rich=[(1, 700), (2, 700)]),
                            archive(700, 2001, storage("shared")))
    assert [_flat(seen[(0, c)]) for c in range(2)] == ["shared", "shared"] and not ctx.skipped, ctx.skipped


def test_table_linear():
    # 100,000 text cells in four tiles of 250 rows: read in linear time
    n_rows, n_cols = 1000, 100
    row = [cell_v5(3, string_id=1 + c) for c in range(n_cols)]
    tiles = {t: tile({r: row for r in range(250)}) for t in range(n_rows // 250)}
    strings = [(1 + c, "s%d" % c) for c in range(n_cols)]
    t = table("Big", n_rows, n_cols, tiles=tiles, strings=strings, tile_size=250)
    members = {"Index/Document.iwa": iwa_member(*numbers_doc([("S", [600])]), *t)}
    data = iwork_zip(members)
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "numbers")
    took = time.perf_counter() - t0
    [tab] = _tables(doc)
    assert len(tab.rows) == n_rows and all(len(r) == n_cols for r in tab.rows) and not ctx.skipped, ctx.skipped
    assert took < 3, took


def _tiled(entries, tiles, strings=(), *, size=1, row_buckets=None, rich_list=None, extra=()):
    """Table 600 whose TileStorage lists `entries` ((tileid, tile object id)) over `tiles` ({object id: tile
    payload}), tile size `size`, strings ((key, text)) in list 602, the row headers naming `row_buckets`, and the
    rich text list `rich_list` (an id the caller adds in `extra`)."""
    ent = [(1, pb((1, tid), (2, pb_ref(t)))) for tid, t in entries]
    heads = [(1, pb((1, 1), *((2, pb_ref(b)) for b in row_buckets)))] if row_buckets else []
    rich = [(17, pb_ref(rich_list))] if rich_list is not None else []
    ds = pb(*heads, (3, pb(*ent, (2, size))), (4, pb_ref(602)), *rich)
    return [archive(600, 6000, pb((1, b""), (2, pb_ref(601)))), archive(601, 6001, pb((4, ds), (8, "T"))),
            data_list(602, [string_entry(k, t) for k, t in strings]),
            *(archive(i, 6002, t) for i, t in tiles.items()), *extra]


def _empty_rows(n_rows, n_cols):
    """A tile payload of `n_rows` rows, each `n_cols` empty offsets."""
    offs = struct.pack(f"<{n_cols}h", *([-1] * n_cols))
    return pb(*((5, pb((1, i), (6, b"x"), (7, offs))) for i in range(n_rows)))


def test_duplicate_tile_entries():
    a, b = tile({0: [_text_cell(1)]}), tile({0: [_text_cell(2)]})
    strings = [(1, "a"), (2, "b")]
    # a tileid given twice keeps its last tile, as a repeated protobuf key does
    with _puts() as seen:
        doc, ctx = _numbers(*_tiled([(0, 620), (0, 621)], {620: a, 621: b}, strings))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "b"} and not ctx.skipped, (seen, ctx.skipped)
    # one tile under three tileids is read at the first only, counted `record` once
    with _puts() as seen:
        doc, ctx = _numbers(*_tiled([(0, 620), (1, 620), (2, 620)], {620: a}, strings))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "a"}, seen
    assert ctx.skipped == {"record": 1}, ctx.skipped
    # thousands of entries on one tile of 100 rows of 1,000 empty offsets: the tile is walked once
    tiles = {620: _empty_rows(100, 1000)}
    for entries, skipped in (([(0, 620)] * 4000, {}), ([(i, 620) for i in range(4000)], {"record": 1})):
        t0 = time.perf_counter()
        doc, ctx = _numbers(*_tiled(entries, tiles, size=256))
        took = time.perf_counter() - t0
        assert took < 2 and _tables(doc) == [] and ctx.skipped == skipped, (took, ctx.skipped)


def test_offsets_past_column_cap():
    # a cell present past SHEET_COLS stops the table at its columns, though the offsets past the cap are not walked
    cell = _text_cell(1)
    offs = struct.pack("<1500h", *([0] + [-1] * 1199 + [0] + [-1] * 299))
    row = pb((5, pb((1, 0), (6, cell), (7, offs))))
    with _puts() as seen:
        doc, ctx = _numbers(*_tiled([(0, 620)], {620: row}, [(1, "a")]))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 0): "a"}, seen
    assert ctx.skipped == {"sheet_cut": 1} and "sheet T cut at 1000 columns" in ctx.warnings, ctx.warnings
    # empty offsets past the cap are not a cut
    offs = struct.pack("<1500h", *([0] + [-1] * 1499))
    doc, ctx = _numbers(*_tiled([(0, 620)], {620: pb((5, pb((1, 0), (6, cell), (7, offs))))}, [(1, "a")]))
    assert not ctx.skipped and len(_tables(doc)) == 1, ctx.skipped


def test_duplicate_header_buckets():
    # a bucket of 50,000 hidden rows named 4,000 times by the row headers is joined once
    bucket = archive(630, 6006, pb((1, 1), *((2, pb((1, i), (3, 1))) for i in range(1, 50_001))))
    rows = tile({0: [_text_cell(1)], 1: [_text_cell(1)], 2: [_text_cell(1)]})
    joins = []
    real = iwork_tables._Reader._cached

    def count(self, key, fn):
        if key[0] == "hidden":
            joins.append(key[1])
        return real(self, key, fn)

    iwork_tables._Reader._cached = count
    t0 = time.perf_counter()
    try:
        doc, ctx = _numbers(*_tiled([(0, 620)], {620: rows}, [(1, "a")], size=256, row_buckets=[630] * 4000,
                                    extra=[bucket]))
    finally:
        iwork_tables._Reader._cached = real
    took = time.perf_counter() - t0
    [tab] = _tables(doc)
    assert took < 2 and joins == [630] and tab.hidden_rows == frozenset({1, 2}), (took, joins, tab.hidden_rows)
    assert not ctx.skipped, ctx.skipped


def test_shared_rich_payload():
    # 4,000 rich-list entries naming one payload padded with 50,000 fields: the payload is parsed once
    n = 4000
    payload = archive(640, 6218, pb((1, pb_ref(700)), *([(7, 1)] * 50_000)))
    lst = archive(641, 6005, pb((1, 8), *((3, pb((1, k), (2, 1), (9, pb_ref(640)))) for k in range(1, n + 1))))
    cells = [cell_v5(9, rich_id=1), cell_v5(9, rich_id=n)]
    gets = []
    real = iwork_iwa.Store.get

    def get(self, id, type=None):
        if id == 640:
            gets.append(id)
        return real(self, id, type)

    iwork_iwa.Store.get = get
    t0 = time.perf_counter()
    try:
        with _puts() as seen:
            doc, ctx = _numbers(*_tiled([(0, 620)], {620: tile({0: cells})}, size=256, rich_list=641,
                                        extra=[payload, lst, archive(700, 2001, storage("shared"))]))
    finally:
        iwork_iwa.Store.get = real
    took = time.perf_counter() - t0
    assert took < 2 and gets == [640], (took, len(gets))
    assert [_flat(seen[(0, c)]) for c in range(2)] == ["shared", "shared"] and not ctx.skipped, ctx.skipped


def test_deleted_text_in_cell_counts_chars():
    # a rich cell keeps its deleted text as Hidden; that text counts toward SHEET_CHARS ("Keep stay" is 9 shown,
    # "gone " 5 more)
    marks = [change(100, 2), change(101, 1)]
    body = archive(700, 2001, storage("Keep gone stay", deletions=[(5, 100), (10, None)],
                                      insertions=[(0, 101), (5, None)]))
    t = table("T", 1, 1, tiles={0: tile({0: [cell_v5(9, rich_id=1)]})}, rich=[(1, 700)])
    with _limit(limits, "SHEET_CHARS", 14):
        with _puts() as seen:
            doc, ctx = _numbers(*t, body, *marks)
    assert seen[(0, 0)] == [Text("Keep "), Hidden([Text("gone ")], "deleted"), Text("stay")], seen
    assert not ctx.skipped, ctx.skipped
    with _limit(limits, "SHEET_CHARS", 13):
        doc, ctx = _numbers(*t, body, *marks)
    assert ctx.skipped == {"sheet_cut": 1} and "sheet T cut at 13 characters" in ctx.warnings, ctx.warnings


def test_v5_key_missing_from_list():
    # a version 5 string, rich or comment key its list lacks counts `record` once a cell; version 4 stays silent
    cells = [_text_cell(9), cell_v5(9, rich_id=9), cell_v5(3, string_id=1, comment_id=9),
             cell_v5(3, string_id=9, comment_id=9), _text_cell(1)]
    t = table("T", 1, len(cells), tiles={0: tile({0: cells})}, strings=[(1, "a")], rich=[(1, 700)],
              comments=[(1, 900)])
    with _puts() as seen:
        doc, ctx = _numbers(*t, archive(700, 2001, storage("r")))
    assert {k: _flat(v) for k, v in seen.items()} == {(0, 2): "a", (0, 4): "a"}, seen
    assert ctx.skipped == {"record": 4}, ctx.skipped
    doc, ctx = _numbers(*table("T", 1, 1, tiles={0: tile({0: [cell_v4(3, string_id=9)]})}, strings=[(1, "a")]))
    assert not ctx.skipped, ctx.skipped


# -- charts and the Keynote walk (Task 14)

def _cells(t: Table) -> list[list[str]]:
    return [[_flat(c.blocks[0].inlines) if c.blocks else "" for c in row] for row in t.rows]


def test_chart_table_caption():
    # by column (the default): a series a column, the row names are the categories; a missing number stays empty
    doc, ctx = _numbers(*chart(1, ["r1", "r2"], ["A", "B"], [[1, 2], [3.5, None]], "Sales"), sheets=[("S", [500])])
    [t] = _tables(doc)
    assert t.caption.startswith("Chart: ") and t.caption == "Chart: column — Sales", t.caption
    assert _cells(t) == [["", "A", "B"], ["r1", "1", "2"], ["r2", "3.5", ""]], _cells(t)
    assert not ctx.skipped, ctx.skipped
    # by row: a series a row; a title the chart does not show is left out; the kind follows docling's table
    doc, ctx = _numbers(*chart(7, ["r1"], ["A", "B"], [[1, 2]], "Hidden", by_row=True, show_title=False),
                        sheets=[("S", [500])])
    [t] = _tables(doc)
    assert t.caption == "Chart: bar" and _cells(t) == [["", "r1"], ["A", "1"], ["B", "2"]], (t.caption, _cells(t))
    # an unknown kind is "other"; a series with no name is named by its place
    doc, ctx = _numbers(*chart(99, [], [], [[4]], None), sheets=[("S", [500])])
    [t] = _tables(doc)
    assert t.caption == "Chart: other" and _cells(t) == [["", "Series 1"], ["", "4"]], (t.caption, _cells(t))
    # a chart that cannot be read counts `chart`, once
    doc, ctx = _numbers(archive(500, 5021, b"\x0a\x05ab"), sheets=[("S", [500])])
    assert not _tables(doc) and ctx.skipped == {"chart": 1}, ctx.skipped


def test_chart_cut():
    rows = [[i, i + 0.5] for i in range(10)]
    with _limit(limits, "CHART_CELLS", 6):
        doc, ctx = _numbers(*chart(1, [f"r{i}" for i in range(10)], ["A", "B"], rows, "T"), sheets=[("S", [500])])
    [t] = _tables(doc)
    # four rows read (more than 6 values stops the grid); series_table keeps what 6 cells hold
    assert t.caption == "Chart: column — T (first 2 points, first 1 series)", t.caption
    assert _cells(t) == [["", "A"], ["r0", "0"], ["r1", "1"]], _cells(t)
    assert ctx.skipped == {"chart_cut": 1}, ctx.skipped
    # by row, a row cut to CHART_CELLS values cuts points, not series
    with _limit(limits, "CHART_CELLS", 4):
        doc, ctx = _numbers(*chart(1, ["r"], [f"c{i}" for i in range(10)], [list(range(10))], None, by_row=True),
                            sheets=[("S", [500])])
    [t] = _tables(doc)
    assert t.caption == "Chart: column (first 1 points)" and ctx.skipped == {"chart_cut": 1}, (t.caption, ctx.skipped)
    # a grid row that is not a message is passed over, counted `chart` once; the rest is shown
    grid = pb((1, "r1"), (1, "r2"), (2, "A"), (3, 5), (3, 6), (3, pb((1, pb((1, 2.0))))))
    doc, ctx = _numbers(archive(500, 5021, pb((10000, pb((1, 1), (7, grid))))), sheets=[("S", [500])])
    [t] = _tables(doc)
    assert _cells(t) == [["", "A"], ["r1", "2"]] and ctx.skipped == {"chart": 1}, (_cells(t), ctx.skipped)
    # past CHART_TOTAL a whole chart is left out, counted `chart`
    with _limit(limits, "CHART_TOTAL", 8):
        doc, ctx = _numbers(*chart(1, ["r"], ["A"], [[1]], None, id=500), *chart(1, ["r"], ["A"], [[1]], None, id=510),
                            *chart(1, ["r"], ["A"], [[1]], None, id=520), sheets=[("S", [500, 510, 520])])
    assert len(_tables(doc)) == 2 and ctx.skipped == {"chart": 1}, ctx.skipped


def _keynote(slides, *archives, tree=None):
    members = {"Index/Document.iwa": iwa_member(*keynote_doc(slides, tree), *archives)}
    ctx = Context()
    return iwork.parse_bytes(iwork_zip(members), ctx, "key"), ctx


def _swap(archives, id, new) -> list:
    """`archives` with the one whose ArchiveInfo identifier is `id` replaced by `new`."""
    out = []
    for a in archives:
        n, pos = pbwire.varint(a, 0)
        out.append(new if pbwire.one(pbwire.fields(a[pos:pos + n]), 1) == id else a)
    assert out != list(archives)
    return out


def test_slides_title_body_order():
    doc, ctx = _keynote([{"title": "T", "body": "Body text", "drawables": [900, 902, 500], "z_order": [902, 500, 900]},
                         {"title": "U", "body": "Second"}],
                        shape(900, 901), archive(901, 2001, storage("Drawn first")),
                        shape(902, 903), archive(903, 2001, storage("Drawn second")),
                        *chart(1, ["r"], ["A"], [[2]], "C"))
    assert [(s.kind, s.title, s.number) for s in doc.blocks] == [("slide", "T", 1), ("slide", "U", 2)], doc.blocks
    md = _md(doc, ctx)
    order = ["## Slide 1: T", "Body text", "Drawn second", "Chart: column — C", "Drawn first", "## Slide 2: U",
             "Second"]
    at = [md.find(x) for x in order]
    assert -1 not in at and at == sorted(at), (at, md)
    assert md.count("T\n") == 1, md  # the title is the heading only, not body text too
    assert not ctx.skipped, ctx.skipped
    # without a z-order the owned drawables keep their stored order
    doc, ctx = _keynote([{"title": "T", "drawables": [902, 900]}], shape(900, 901),
                        archive(901, 2001, storage("Drawn first")), shape(902, 903),
                        archive(903, 2001, storage("Second")))
    md = _md(doc, ctx)
    assert md.find("Second") < md.find("Drawn first"), md


def test_skipped_slide():
    doc, ctx = _keynote([{"title": "A", "body": "one"}, {"title": "S", "body": "skipped body", "skipped": True},
                         {"title": "C", "body": "three"}])
    assert [s.hidden for s in doc.blocks] == ["", "hidden", ""], doc.blocks
    md = _md(doc, ctx)
    assert "skipped body" not in md and "## Slide 2" not in md and "## Slide 3: C" in md, md
    assert "(1 hidden places left out)" in md, md  # decision 6: left out and counted
    md = _md(doc, ctx, hidden=True)
    assert "## Slide 2: S (hidden)" in md and "skipped body" in md, md


def test_slide_notes():
    doc, ctx = _keynote([{"title": "T", "notes": "Say this\nThen that"}])
    assert [n.inlines[0].text for n in doc.blocks[0].notes] == ["Say this", "Then that"], doc.blocks[0].notes
    md = _md(doc, ctx)
    assert "> Notes: Say this\n> Then that" in md and not ctx.skipped, (md, ctx.skipped)


def test_slide_comment_author():
    # a comment placed on two slides goes out once, on the first
    doc, ctx = _keynote([{"title": "T", "drawables": [950]}, {"title": "U", "drawables": [950]}],
                        sticky(950, 951), comment(951, "Fix this", author=952, replies=[953]), author(952, "Ann"),
                        comment(953, "Done", author=952))
    assert not doc.notes and len(doc.blocks[0].comments) == 2 and not doc.blocks[1].comments, doc.blocks
    md = _md(doc, ctx)
    assert "> Comment by Ann: Fix this\n> Comment by Ann: Done" in md and md.count("Fix this") == 1, md
    assert not ctx.skipped, ctx.skipped


def test_templates_not_walked():
    doc, ctx = _keynote([{"title": "T", "body": "real", "template": "TEMPLATE ONLY"}])
    md = _md(doc, ctx, hidden=True)
    assert "real" in md and "TEMPLATE ONLY" not in md and len(doc.blocks) == 1, md
    assert not ctx.skipped, ctx.skipped


def test_slide_tree_cycle():
    # slide 2 is a child of slide 1 and names slide 1 as its child: the loop is cut, counted `depth` once
    doc, ctx = _keynote([{"title": "A", "children": [1]}, {"title": "B", "children": [0]}], tree=[0])
    assert [(s.title, s.number) for s in doc.blocks] == [("A", 1), ("B", 2)] and ctx.skipped == {"depth": 1}, (
        doc.blocks, ctx.skipped)
    # a node listed twice is read once, uncounted
    doc, ctx = _keynote([{"title": "A"}, {"title": "B"}], tree=[0, 1, 0, 1])
    assert [(s.title, s.number) for s in doc.blocks] == [("A", 1), ("B", 2)] and not ctx.skipped, ctx.skipped


def test_slide_failures():
    # a slide that is not a message counts `slide` and keeps its number; the other slides are read
    slides = [{"title": "A"}, {"title": "B"}, {"title": "C", "body": "kept"}]
    broken = _swap(keynote_doc(slides), slide_ids(1)["slide"], archive(slide_ids(1)["slide"], 5, b"\x0a\x05ab"))
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip({"Index/Document.iwa": iwa_member(*broken)}), ctx, "key")
    assert [(s.title, s.number) for s in doc.blocks] == [("A", 1), ("C", 3)] and ctx.skipped == {"slide": 1}, (
        doc.blocks, ctx.skipped)
    # a title placeholder that is not a message counts `shape`; the body is still read
    ids = slide_ids(0)
    broken = _swap(keynote_doc([{"title": "A", "body": "kept"}]), ids["title"], archive(ids["title"], 7, b"\x0a\x05ab"))
    ctx = Context()
    doc = iwork.parse_bytes(iwork_zip({"Index/Document.iwa": iwa_member(*broken)}), ctx, "key")
    assert doc.blocks[0].title == "" and "kept" in _md(doc, ctx) and ctx.skipped == {"shape": 1}, ctx.skipped
    # a show with no slide node is not a Keynote document: the other walks are tried, then the preview fallback
    members = {"Index/Document.iwa": iwa_member(archive(1, 1, pb((2, pb_ref(3)))), archive(3, 2, pb((1, "x"))))}
    _raises(ReadError, iwork.parse_bytes, iwork_zip(members), Context(), "key")


def test_slide_title_notes_and_hidden():
    # a comment on the title text: the title shows no mark, so its note moves to the slide's comments
    title = storage("Title", comments=[(0, 960), (2, None)])
    doc, ctx = _keynote([{"title": title, "body": "b"}], highlight(960, 961), comment(961, "on title"))
    sec = doc.blocks[0]
    assert sec.title == "Title" and not doc.notes and len(sec.comments) == 1, (sec.title, doc.notes, sec.comments)
    md = _md(doc, ctx)
    assert "## Slide 1: Title" in md and "> Comment: on title" in md and "[^" not in md and not ctx.skipped, md
    # a deleted run in the title: left out of the title, counted by default, shown by --hidden in the body
    title = storage("Kept gone", deletions=[(5, 970), (9, None)])
    doc, ctx = _keynote([{"title": title}], archive(970, 2060, pb((1, 2))))
    assert doc.blocks[0].title == "Kept", doc.blocks[0].title
    md = _md(doc, ctx)
    assert "gone" not in md and "(1 hidden places left out)" in md, md
    md = _md(doc, ctx, hidden=True)
    assert "(hidden: gone)" in md and "## Slide 1: Kept\n" in md, md


def test_walk_resets_between_apps():
    # the next app's walk starts with no cache and no chart cells spent
    seen = []

    def first(store, ctx):
        store.cache["x"] = 1
        store.chart_cells = limits.CHART_TOTAL
        return None

    def second(store, ctx):
        seen.append((dict(store.cache), store.chart_cells))
        return Doc(blocks=[Para([Text("ok")])])

    old = dict(iwork_iwa.WALKS)
    try:
        iwork_iwa.WALKS.update({"key": first, "pages": second})
        iwork.parse_bytes(iwork_zip({"Index/Document.iwa": _doc_member()}), Context(), "key")
    finally:
        iwork_iwa.WALKS.clear()
        iwork_iwa.WALKS.update(old)
    assert seen == [({}, 0)], seen


def test_keynote_linear():
    # 2,000 slides sharing one big drawable and one chart title part of 50,000 fields, one z-order list of 200,000
    # copies of one id: every shared part is read once
    n, BIG = 2000, 9_000_000  # ids clear of the slides' own
    big = archive(BIG + 1, 2001, storage("\n".join(f"line {i}" for i in range(20000))))
    ns = archive(BIG + 2, 5023, pb((10000, pb((21, 1), *((23, "t") for _ in range(50000))))))
    grid = pb((1, "r"), (2, "A"), (3, pb((1, pb((1, 1.0))))))
    charts = [archive(10**7 + i, 5021, pb((10000, pb((1, 1), (7, grid), (10, pb_ref(BIG + 2)))))) for i in range(n)]
    slides = [{"title": f"S{i}", "drawables": [BIG, 10**7 + i], "z_order": [BIG] * (200000 if i == 0 else 1)}
              for i in range(n)]
    data = iwork_zip({"Index/Document.iwa": iwa_member(*keynote_doc(slides), shape(BIG, BIG + 1), big, ns, *charts)})
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "key")
    took = time.perf_counter() - t0
    assert len(doc.blocks) == n and not ctx.skipped, ctx.skipped
    assert sum(isinstance(b, Table) for s in doc.blocks for b in s.blocks) == n
    assert doc.blocks[1].blocks[0].caption == "Chart: column — t", doc.blocks[1].blocks
    assert took < 2, took
    # 1,000 slides sharing one comment (2014) and one note (15), each padded with 50,000 fields: each parsed once
    junk = [(9, 1)] * 50000
    slides = [{"title": f"S{i}", "drawables": [BIG + 3], "note": BIG + 5} for i in range(1000)]
    data = iwork_zip({"Index/Document.iwa": iwa_member(
        *keynote_doc(slides), archive(BIG + 3, 2014, pb((1, b""), (2, pb_ref(BIG + 4)), *junk)),
        comment(BIG + 4, "c"), archive(BIG + 5, 15, pb((1, pb_ref(BIG + 6)), *junk)),
        archive(BIG + 6, 2001, storage("the note")))})
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "key")
    took = time.perf_counter() - t0
    assert [bool(s.comments) for s in doc.blocks[:2]] == [True, False] and doc.blocks[0].notes, doc.blocks[:2]
    assert sum(bool(s.notes) for s in doc.blocks) == 1 and not ctx.skipped, ctx.skipped
    assert took < 2, took
    # Numbers: 1,000 sheets sharing one padded sticky note, one sheet listing it 200,000 times
    sheets = [("S%d" % i, [BIG + 3] * (200000 if i == 0 else 1)) for i in range(1000)]
    data = iwork_zip({"Index/Document.iwa": iwa_member(
        *numbers_doc(sheets), archive(BIG + 3, 2014, pb((1, b""), (2, pb_ref(BIG + 4)), *junk)),
        comment(BIG + 4, "c"))})
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "numbers")
    took = time.perf_counter() - t0
    assert len(doc.blocks) == 1000 and sum(len(s.comments) for s in doc.blocks) == 1 and not ctx.skipped, ctx.skipped
    assert took < 2, took


# -- iWork '09 XML (Task 15)

def _x(data: bytes, app="pages"):
    ctx = Context()
    return iwork.parse_bytes(data, ctx, app), ctx


def _p(text, style=None, **attrs) -> str:
    """One sf:p, ended by the sf:br Pages writes at a paragraph's end."""
    a = f' sf:style="{style}"' if style else ""
    a += "".join(f' sf:{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f"<sf:p{a}>{text}<sf:br/></sf:p>"


def _box(xml) -> str:
    return f"<sf:text><sf:text-storage><sf:text-body>{xml}</sf:text-body></sf:text-storage></sf:text>"


def _media(path, attr="sf:path") -> str:
    return (f'<sf:media><sf:content><sf:image-media><sf:filtered-image><sf:unfiltered>'
            f'<sf:data {attr}="{path}"/></sf:unfiltered></sf:filtered-image></sf:image-media></sf:content></sf:media>')


BOLD_STYLE = ('<sf:characterstyle sf:ident="b" sfa:ID="SFWPCharacterStyle-1"><sf:property-map><sf:bold>'
              '<sf:number sfa:number="1" sfa:type="c"/></sf:bold></sf:property-map></sf:characterstyle>')


def _labels(*types) -> str:
    """An sf:liststyle ladder: "none", or a text-label type (bullet, decimal...) per rung."""
    out = []
    for t in types:
        if t == "none":
            out.append('<sf:list-label-typeinfo sf:type="none"/>')
        else:
            out.append(f'<sf:list-label-typeinfo sf:type="text"><sf:text-label sf:type="{t}" sf:format="x"/>'
                       '</sf:list-label-typeinfo>')
    return ('<sf:property-map><sf:listLabelTypes><sf:array>' + "".join(out)
            + '</sf:array></sf:listLabelTypes></sf:property-map>')


def test_09_pages_body_header_footnote():
    body = _p('Body text<sf:footnote sf:autonumber="1" sf:index="0"/> more.') + _p("Second para")
    notes = '<sf:p><sf:span><sf:footnote-mark sf:mark="1"/></sf:span> The note.<sf:footnotebr/></sf:p>'
    doc, ctx = _x(pages09(body, header=_p("Running head"), footer=_p("Page foot"), footnotes=notes))
    assert doc.blocks == [Para([Text("Body text"), NoteRef("fn-1"), Text(" more.")]), Para([Text("Second para")])], \
        doc.blocks
    assert doc.headers == ["Running head"] and doc.footers == ["Page foot"], (doc.headers, doc.footers)
    md = _md(doc, ctx)
    assert "Header: Running head" in md and "Footer: Page foot" in md, md
    assert "Body text[^1] more." in md and "[^1]: The note." in md, md
    assert not ctx.skipped and not ctx.warnings, ctx.skipped


def test_09_pages_footnote_pairing():
    # footnotes pair with the body's marks in order; a mark past the last note counts `note` and shows nothing;
    # a footnote in a header counts `note`; a note continues over paragraphs until the next mark
    body = _p('a<sf:footnote sf:autonumber="1"/> b<sf:footnote sf:autonumber="2"/> c<sf:footnote/>')
    notes = ('<sf:p><sf:footnote-mark sf:mark="1"/>One</sf:p><sf:p>One more</sf:p>'
             '<sf:p><sf:footnote-mark sf:mark="2"/>Two<sf:footnotebr/></sf:p>')
    doc, ctx = _x(pages09(body, header=_p('Head<sf:footnote sf:autonumber="9"/>'), footnotes=notes))
    assert doc.blocks == [Para([Text("a"), NoteRef("fn-1"), Text(" b"), NoteRef("fn-2"), Text(" c")])], doc.blocks
    assert doc.notes == {"fn-1": [Para([Text("One")]), Para([Text("One more")])], "fn-2": [Para([Text("Two")])]}, \
        doc.notes
    assert doc.headers == ["Head"] and ctx.skipped == {"note": 2}, (doc.headers, ctx.skipped)
    # a footnote no mark names is still kept, after the marked ones
    doc, ctx = _x(pages09(_p("plain"), footnotes='<sf:p><sf:footnote-mark sf:mark="1"/>Orphan</sf:p>'))
    md = _md(doc, ctx)
    assert "Orphan" in md and not ctx.skipped, (md, ctx.skipped)


def test_09_pages_ghost_text_dropped():
    body = (_p('<sf:ghost-text sfa:ID="g1">Type the title<sf:br/></sf:ghost-text>')
            + _p('Real <sf:ghost-text-ref sfa:IDREF="g1">Type the title</sf:ghost-text-ref>text')
            + _p('<sf:span sf:style="b"><sf:ghost-text sfa:ID="g2">Template body</sf:ghost-text></sf:span>'))
    doc, ctx = _x(pages09(body, styles=BOLD_STYLE))
    md = _md(doc, ctx, hidden=True)
    assert "Type the title" not in md and "Template body" not in md and "Real text" in md, md
    assert doc.blocks == [Para([Text("Real text")])], doc.blocks
    assert not ctx.skipped and not ctx.warnings, ctx.skipped


def test_09_pages_headings_lists_links():
    styles = (BOLD_STYLE + '<sf:paragraphstyle sf:ident="t" sf:name="Title"/>'
              '<sf:paragraphstyle sf:ident="h2" sfa:ID="SFWPParagraphStyle-2" sf:name="Heading 2"/>'
              '<sf:paragraphstyle sf:ident="h2b" sf:parent-ident="h2"/>'
              '<sf:paragraphstyle sf:ident="c1" sf:parent-ident="c2"/><sf:paragraphstyle sf:ident="c2" '
              'sf:parent-ident="c1"/>'
              f'<sf:liststyle sf:ident="bul">{_labels("none", "bullet", "bullet")}</sf:liststyle>'
              f'<sf:liststyle sf:ident="num">{_labels("none", "bullet", "decimal")}</sf:liststyle>'
              '<sf:liststyle sf:ident="kid" sf:parent-ident="num"/>')
    body = (_p("Doc Title", "t") + _p("Section", "h2b") + _p("Looping", "c1")
            + _p("item one", list_style="bul", list_level=1) + _p("sub", list_style="kid", list_level=2)
            + _p("rung zero", list_style="bul")
            + _p('See <sf:link href="https://example.com/x"><sf:span sf:style="b">bold link</sf:span></sf:link> and '
                 '<sf:span sf:style="SFWPCharacterStyle-1">strong</sf:span> <sf:link href="#bookmark">inner</sf:link>'
                 '<sf:lnbr/>end'))
    doc, ctx = _x(pages09(body, styles=styles))
    assert doc.blocks == [
        Heading(1, [Text("Doc Title")]), Heading(2, [Text("Section")]), Para([Text("Looping")]),
        ListItem(0, "-", [Text("item one")]), ListItem(1, "1.", [Text("sub")]), Para([Text("rung zero")]),
        Para([Text("See "), Link("https://example.com/x", [Bold([Text("bold link")])]), Text(" and "),
              Bold([Text("strong")]), Text(" inner"), Break(), Text("end")])], doc.blocks
    assert not ctx.skipped, ctx.skipped
    md = _md(doc, ctx)
    assert "# Doc Title" in md and "- item one\n  1. sub" in md and "[**bold link**](https://example.com/x)" in md, md


def test_09_pages_comment():
    body = (_p('Look <sf:annotation-field sfa:ID="af1">here</sf:annotation-field> now.')
            + _p('Again <sf:annotation-field sfa:ID="af1">there</sf:annotation-field>'))
    extra = ('<sf:annotations><sf:annotation sf:target="af1"><sf:text-storage><sf:text-body>'
             + _p("Check this") + '</sf:text-body></sf:text-storage></sf:annotation>'
             '<sf:annotation sf:target="nowhere"><sf:text-storage><sf:text-body>' + _p("Loose remark")
             + '</sf:text-body></sf:text-storage></sf:annotation></sf:annotations>')
    doc, ctx = _x(pages09(body, extra=extra))
    assert doc.blocks[0] == Para([Text("Look here"), NoteRef("cm-1"), Text(" now.")]), doc.blocks
    assert doc.blocks[1] == Para([Text("Again there"), NoteRef("cm-1")]), doc.blocks
    md = _md(doc, ctx)
    assert "Look here[^1] now." in md and "Again there[^1]" in md, md
    assert "[^1]: Comment: Check this" in md and md.count("Check this") == 1, md
    assert "Comment: Loose remark" in md and "Check this" not in md.split("[^1]:")[0], md  # kept, never in the body
    assert not ctx.skipped, ctx.skipped


def test_09_pages_image():
    body = _p("Before") + _media("Contents/pic.png") + _media("pic2.png", "path") + _media("gone.png") + _p("After")
    doc, ctx = _x(pages09(body, files={"Contents/pic.png": PNG, "pic2.png": PNG + b"2"}))
    assert doc.blocks == [Para([Text("Before")]), Image(PNG), Image(PNG + b"2"), Para([Text("After")])], doc.blocks
    assert ctx.skipped == {"image": 1}, ctx.skipped
    # a path that leaves the package counts `image`; a picture past IMAGE_BYTES for the document counts `image`
    doc, ctx = _x(pages09(_p("x") + _media("../x.png") + _media("/x.png"), files={"x.png": PNG}))
    assert doc.blocks == [Para([Text("x")])] and ctx.skipped == {"image": 2}, (doc.blocks, ctx.skipped)
    old = limits.IMAGE_BYTES
    try:
        limits.IMAGE_BYTES = len(PNG) + 1
        doc, ctx = _x(pages09(_p("x") + _media("a.png") + _media("a.png"), files={"a.png": PNG}))
    finally:
        limits.IMAGE_BYTES = old
    assert [type(b) for b in doc.blocks] == [Para, Image] and ctx.skipped == {"image": 1}, (doc.blocks, ctx.skipped)


def test_09_pages_drawables_attachments_prototypes():
    extra = ('<sl:section-prototypes><sl:prototype><sf:text-storage><sf:text-body>' + _p("PROTOTYPE ONLY")
             + '</sf:text-body></sf:text-storage></sl:prototype></sl:section-prototypes>'
             '<sl:drawables><sl:page-group sl:page="1"><sf:drawable-shape sfa:ID="ds1">' + _box(_p("Floating"))
             + '</sf:drawable-shape></sl:page-group><sl:textbox-chain><sf:drawable-shape-ref sfa:IDREF="ds1"/>'
             '</sl:textbox-chain></sl:drawables>'
             '<sf:attachments><sf:attachment sfa:ID="a1"><sf:drawable-shape>' + _box(_p("Anchored"))
             + '</sf:drawable-shape></sf:attachment><sf:attachment sfa:ID="a2">' + _media("p.png")
             + '</sf:attachment></sf:attachments>')
    body = (_p('First<sf:attachment-ref sfa:IDREF="a1"/>') + _p('Second<sf:attachment-ref sfa:IDREF="a1"/>')
            + _p("Last"))
    doc, ctx = _x(pages09(body, extra=extra, files={"p.png": PNG}))
    # the floating drawables follow the body, and an attachment no ref places comes last
    assert doc.blocks == [Para([Text("First")]), Para([Text("Anchored")]), Para([Text("Second")]),
                          Para([Text("Last")]), Para([Text("Floating")]), Image(PNG)], doc.blocks
    assert not ctx.skipped, ctx.skipped


def test_09_pages_table_counted():
    # a table that fails (values and no sf:rows to place them, a model with no grid) counts `table`, and its cells
    # stay out of the body
    table = ('<sf:tabular-info><sf:tabular-model><sf:grid sf:numcols="1" sf:numrows="1"><sf:datasource><sf:t>'
             '<sf:ct sfa:s="cell"/></sf:t></sf:datasource></sf:grid></sf:tabular-model></sf:tabular-info>')
    cell_text = '<sf:tabular-info><sf:tabular-model>' + _p("CELL TEXT") + '</sf:tabular-model></sf:tabular-info>'
    doc, ctx = _x(pages09(_p("Body") + table + cell_text))
    assert doc.blocks == [Para([Text("Body")])] and ctx.skipped == {"table": 2}, (doc.blocks, ctx.skipped)
    # a footnote marked in an unread cell keeps its place, so the marks after it pair with their own footnotes
    in_cell = '<sf:tabular-info><sf:tabular-model>' + _p('cell<sf:footnote/>') + '</sf:tabular-model></sf:tabular-info>'
    notes = "".join(f'<sf:p><sf:footnote-mark sf:mark="{i}"/>n{i}<sf:footnotebr/></sf:p>' for i in (1, 2, 3))
    doc, ctx = _x(pages09(_p('a<sf:footnote/>') + in_cell + _p('b<sf:footnote/>'), footnotes=notes))
    md = _md(doc, ctx)
    assert "a[^1]" in md and "b[^2]" in md and "[^1]: n1" in md and "[^2]: n3" in md and "n2" in md, md
    assert ctx.skipped == {"table": 1}, ctx.skipped


def _boom(data, app, method="paragraph", word="boom"):
    """`_x` with Reader.`method` failing on an element whose text holds `word`."""
    from reader.formats import iwork_xml
    real = getattr(iwork_xml.Reader, method)

    def broken(self, el, *a):
        if word in "".join(el.itertext()):
            raise ValueError("broken part")
        return real(self, el, *a)

    setattr(iwork_xml.Reader, method, broken)
    try:
        return _x(data, app)
    finally:
        setattr(iwork_xml.Reader, method, real)


def test_09_drawables_guarded_one_by_one():
    # each floating drawable is read under its own guard: the second of three failing keeps the first and third
    boxes = "".join(f'<sf:drawable-shape>{_box(_p(w))}</sf:drawable-shape>' for w in ("one", "boom", "three"))
    extra = f'<sl:drawables><sl:page-group sl:page="1">{boxes}</sl:page-group></sl:drawables>'
    doc, ctx = _boom(pages09(_p("Body"), extra=extra), "pages")
    assert doc.blocks == [Para([Text("Body")]), Para([Text("one")]), Para([Text("three")])], doc.blocks
    assert ctx.skipped == {"shape": 1}, ctx.skipped
    # a picture that fails counts `image`, by its kind
    extra = (f'<sl:drawables><sl:page-group>{_media("boom.png")}<sf:drawable-shape>{_box(_p("after"))}'
             '</sf:drawable-shape></sl:page-group></sl:drawables>')
    doc, ctx = _boom(pages09(_p("Body"), extra=extra, files={"boom.png": PNG}), "pages", "_image", "")
    assert doc.blocks == [Para([Text("Body")]), Para([Text("after")])], doc.blocks
    assert ctx.skipped == {"image": 1}, ctx.skipped
    # Keynote: one bad drawable loses itself, not the slide
    shapes = "".join(f'<sf:shape>{_box(_p(w))}</sf:shape>' for w in ("one", "boom", "three"))
    doc, ctx = _boom(keynote09(slide09(_p("T"), _p("B"), drawables=shapes)), "key")
    assert [(s.title, s.blocks) for s in doc.blocks] == [
        ("T", [Para([Text("B")]), Para([Text("one")]), Para([Text("three")])])], doc.blocks
    assert ctx.skipped == {"shape": 1}, ctx.skipped


def _reads(data):
    """`_x`, and the picture members read through `iwork_iwa.read_member` (the index is read through it too)."""
    from reader.formats import iwork_iwa
    real, calls = iwork_iwa.read_member, []

    def spy(pkg, path):
        if not path.startswith("index"):
            calls.append(path)
        return real(pkg, path)

    iwork_iwa.read_member = spy
    try:
        return _x(data) + (calls,)
    finally:
        iwork_iwa.read_member = real


def test_09_header_picture_counted():
    # a header shows lines only: its picture counts `image`, and is not read
    doc, ctx, calls = _reads(pages09(_p("Body"), header=_media("logo.png") + _p("Head"), footer=_media("logo.png"),
                                     files={"logo.png": PNG}))
    assert doc.headers == ["Head"] and ctx.skipped == {"image": 2} and not calls, (doc.headers, ctx.skipped, calls)


def test_09_picture_read_once_per_member():
    # three placements of one member: read once, each placement still charged
    doc, ctx, calls = _reads(pages09(_p("x") + _media("a.png") * 3, files={"a.png": PNG}))
    assert doc.blocks == [Para([Text("x")])] + [Image(PNG)] * 3 and calls == ["a.png"], (doc.blocks, calls)


def test_09_keynote_loose_parts():
    # Keynote keeps a comment no field names as an unmarked note, and counts an attachment no ref places; the
    # theme's masters hold neither
    loose = ('<sf:annotations><sf:annotation sf:target="nowhere"><sf:text-storage><sf:text-body>' + _p("Loose")
             + '</sf:text-body></sf:text-storage></sf:annotation></sf:annotations>')
    unplaced = '<sf:attachments><sf:attachment sfa:ID="a9">' + _media("p.png") + '</sf:attachment></sf:attachments>'
    master = ('<key:master-slide><key:page><sf:layers><sf:layer><sf:drawables>' + loose.replace("Loose", "MASTER")
              + unplaced.replace("a9", "m9") + '</sf:drawables></sf:layer></sf:layers></key:page></key:master-slide>')
    doc, ctx = _x(keynote09(slide09(_p("T"), _p("B"), drawables=loose + unplaced), masters=master,
                            files={"p.png": PNG}), "key")
    assert list(doc.notes.values()) == [[Para([Text("Comment: "), Text("Loose")])]], doc.notes
    assert ctx.skipped == {"object": 1}, ctx.skipped


def test_09_footnote_split_at_marks():
    # a leading blank paragraph opens no footnote, and two footnotes in one paragraph are split at the mark
    body = _p('a<sf:footnote/> b<sf:footnote/> c<sf:footnote/>')
    notes = ('<sf:p><sf:br/></sf:p><sf:p><sf:footnote-mark sf:mark="1"/>One<sf:footnotebr/>'
             '<sf:span><sf:footnote-mark sf:mark="2"/></sf:span>Two<sf:footnotebr/></sf:p>'
             '<sf:p><sf:footnote-mark sf:mark="3"/>Three</sf:p>')
    doc, ctx = _x(pages09(body, footnotes=notes))
    assert doc.notes == {"fn-1": [Para([Text("One")])], "fn-2": [Para([Text("Two")])],
                         "fn-3": [Para([Text("Three")])]}, doc.notes
    assert not ctx.skipped, ctx.skipped
    # a storage with no marks: a content paragraph opens a footnote, a footnote break ends it
    notes = '<sf:p><sf:br/></sf:p><sf:p>Alpha</sf:p><sf:p>Beta<sf:footnotebr/></sf:p><sf:p>Gamma</sf:p>'
    doc, ctx = _x(pages09(_p('a<sf:footnote/> b<sf:footnote/>'), footnotes=notes))
    assert doc.notes == {"fn-1": [Para([Text("Alpha")]), Para([Text("Beta")])], "fn-2": [Para([Text("Gamma")])]}, \
        doc.notes


def test_09_keynote_title_field_shared_with_body():
    # a body field with the title field's id: the note moved to the comments, and the body shows no mark
    extra = ('<sf:annotations><sf:annotation sf:target="t1"><sf:text-storage><sf:text-body>' + _p("on the title")
             + '</sf:text-body></sf:text-storage></sf:annotation></sf:annotations>')
    title = _p('Head <sf:annotation-field sfa:ID="t1">line</sf:annotation-field>')
    body = _p('<sf:annotation-field sfa:ID="t1">again</sf:annotation-field>')
    doc, ctx = _x(keynote09(slide09(title, body, drawables=extra)), "key")
    s = doc.blocks[0]
    assert s.blocks == [Para([Text("again")])] and not doc.notes, (s.blocks, doc.notes)
    assert s.comments == [Para([Text("Comment: "), Text("on the title")])], s.comments


def test_09_unread_marks_counted():
    # a mark in an unread table past the last footnote counts `note`, as a body mark does; so does one in a header
    in_cell = ('<sf:tabular-info><sf:tabular-model>' + _p('c<sf:footnote/> d<sf:footnote/>')
               + '</sf:tabular-model></sf:tabular-info>')
    doc, ctx = _x(pages09(_p("Body") + in_cell, footnotes='<sf:p><sf:footnote-mark sf:mark="1"/>n1</sf:p>'))
    assert ctx.skipped == {"table": 1, "note": 1}, ctx.skipped
    doc, ctx = _x(pages09(_p("Body"), header=in_cell + _p("Head")))
    assert doc.headers == ["Head"] and ctx.skipped == {"table": 1, "note": 2}, ctx.skipped


def test_09_gz_index():
    doc, ctx = _x(pages09(_p("Zipped body"), gz=True))
    assert doc.blocks == [Para([Text("Zipped body")])] and not ctx.skipped, (doc.blocks, ctx.skipped)
    doc, ctx = _x(keynote09(slide09(_p("T"), _p("B")), gz=True), "key")
    assert [(s.title, s.blocks) for s in doc.blocks] == [("T", [Para([Text("B")])])], doc.blocks


def test_09_keynote_slides_notes():
    styles = ('<sf:paragraphstyle sf:ident="body" sf:parent-ident="base"/>'
              '<sf:paragraphstyle sf:ident="base"><sf:property-map><sf:listStyle>'
              '<sf:liststyle-ref sfa:IDREF="SFWPListStyle-1"/></sf:listStyle></sf:property-map></sf:paragraphstyle>'
              f'<sf:liststyle sfa:ID="SFWPListStyle-1" sf:ident="bullets">{_labels("none", "bullet")}</sf:liststyle>'
              '<sf:paragraphstyle sf:ident="named" sf:name="Title"/>')
    drawables = ('<sf:shape sfa:ID="s1">' + _box(_p("Box text")) + '</sf:shape>' + _media("Shared/a.png")
                 + '<sf:group><sf:shape>' + _box(_p("Grouped")) + '</sf:shape></sf:group>'
                 + '<sf:sticky-note>' + _box(_p("Drawn note")) + '</sf:sticky-note>')
    sticky = '<key:sticky-note>' + _box(_p("Fix it")) + '</key:sticky-note>'
    master = ('<key:master-slide><key:title-placeholder>' + _box(_p("MASTER ONLY"))
              + '</key:title-placeholder><key:page><sf:layers><sf:layer><sf:drawables><sf:shape>'
              + _box(_p("MASTER SHAPE")) + '</sf:shape></sf:drawables></sf:layer></sf:layers></key:page>'
              '</key:master-slide>')
    slides = (slide09(_p("Title A"), _p("Point one", "body", list_level=1) + _p("Plain", "named"),
                      drawables=drawables, notes=_p("Say this") + _p("Then that"), sticky=sticky, n=0)
              + slide09(_p("Title B"), _p("Second body"), n=1))
    doc, ctx = _x(keynote09(slides, styles=styles, masters=master, files={"Shared/a.png": PNG}), "key")
    assert [(s.kind, s.title, s.number) for s in doc.blocks] == [("slide", "Title A", 1), ("slide", "Title B", 2)]
    s = doc.blocks[0]
    assert s.blocks == [ListItem(0, "-", [Text("Point one")]), Para([Text("Plain")]), Para([Text("Box text")]),
                        Image(PNG), Para([Text("Grouped")])], s.blocks
    assert s.notes == [Para([Text("Say this")]), Para([Text("Then that")])], s.notes
    assert s.comments == [Para([Text("Comment: "), Text("Drawn note")]), Para([Text("Comment: "), Text("Fix it")])], \
        s.comments
    md = _md(doc, ctx)
    order = ["## Slide 1: Title A", "- Point one", "Plain", "Box text", "Grouped", "> Notes: Say this", "> Then that",
             "> Comment: Drawn note", "> Comment: Fix it", "## Slide 2: Title B", "Second body"]
    at = [md.find(x) for x in order]
    assert -1 not in at and at == sorted(at), (at, md)
    assert "MASTER" not in md and md.count("Title A") == 1, md
    assert not ctx.skipped and not ctx.warnings, ctx.skipped


def test_09_keynote_slide_failure_and_title_note():
    # a slide that cannot be read counts `slide` and keeps its number; a comment mark in a title moves its note
    # into the slide's comments, since the title shows no mark
    extra = ('<sf:annotations><sf:annotation sf:target="t1"><sf:text-storage><sf:text-body>' + _p("on the title")
             + '</sf:text-body></sf:text-storage></sf:annotation></sf:annotations>')
    title = _p('Head <sf:annotation-field sfa:ID="t1">line</sf:annotation-field>')
    again = _p('<sf:annotation-field sfa:ID="t1">again</sf:annotation-field>')  # its note went to the title's slide
    slides = slide09(title, _p("x"), n=0) + slide09(_p("Two"), _p("y"), n=1) + slide09(_p("Three"), again, n=2)
    data = keynote09(slides + extra)
    from reader.formats import iwork_xml
    real = iwork_xml.Reader._slide

    def broken(self, slide, number):
        if number == 2:
            raise ValueError("broken slide")
        return real(self, slide, number)

    iwork_xml.Reader._slide = broken
    try:
        doc, ctx = _x(data, "key")
    finally:
        iwork_xml.Reader._slide = real
    assert [(s.title, s.number) for s in doc.blocks] == [("Head line", 1), ("Three", 3)], doc.blocks
    assert doc.blocks[0].comments == [Para([Text("Comment: "), Text("on the title")])] and not doc.notes, doc.blocks
    assert doc.blocks[1].blocks == [Para([Text("again")])], doc.blocks[1]
    assert ctx.skipped == {"slide": 1}, ctx.skipped


def test_09_root_picks_app():
    # the root element names the app; the extension's app is used only when the root is unknown
    doc, ctx = _x(pages09(_p("Pages body")), "key")
    assert doc.blocks == [Para([Text("Pages body")])], doc.blocks
    doc, ctx = _x(keynote09(slide09(_p("T"))), "pages")
    assert [s.title for s in doc.blocks] == ["T"], doc.blocks
    doc, ctx = _x(pages09(_p("Older body"), root="sl:other"), "pages")
    assert doc.blocks == [Para([Text("Older body")])], doc.blocks
    # an unknown root the extension's walk finds nothing in: no blocks, so the preview stands in
    data = pages09(_p("text"), root="sl:other", files={"preview.jpg": JPG})
    doc, ctx = _x(data, "key")
    assert doc.blocks == [Image(JPG)] and ctx.skipped == {"document": 1}, (doc.blocks, ctx.skipped)


def test_09_doctype_refused():
    head = (f'<?xml version="1.0"?><!DOCTYPE sl:document [<!ENTITY e "boom">]><sl:document '
            f'xmlns:sf="http://developer.apple.com/namespaces/sf" xmlns:sl="http://developer.apple.com/namespaces/sl">'
            '<sf:p>&e;</sf:p></sl:document>').encode()
    doc, ctx = _read({"index.xml": head, "preview.jpg": JPG})
    assert doc.blocks == [Image(JPG)] and ctx.skipped == {"document": 1}, (doc.blocks, ctx.skipped)
    assert _kind({"index.xml": head}) == "damaged"


def test_09_deep_nesting():
    # 30,000 nested spans and sections: walked without recursion
    n = 30000
    body = "<sf:p>" + '<sf:span sf:style="b">' * n + "deep" + "</sf:span>" * n + "</sf:p>"
    wrap = "<sf:section>" * n + _p("inner") + "</sf:section>" * n
    doc, ctx = _x(pages09(body + wrap, styles=BOLD_STYLE))
    assert doc.blocks == [Para([Bold([Text("deep")])]), Para([Text("inner")])], doc.blocks


def test_09_linear():
    # 50,000 spans whose styles each run an 8-step parent chain over one shared tail, 20,000 marks on one comment,
    # 20,000 paragraphs naming one list style through an 8-step chain, and 20,000 refs to one picture's data
    n = 50000
    chain = "".join(f'<sf:characterstyle sf:ident="c{i}" sf:parent-ident="c{i + 1}"/>' for i in range(8))
    chain += ('<sf:characterstyle sf:ident="c8"><sf:property-map><sf:bold><sf:number sfa:number="1"/></sf:bold>'
              '</sf:property-map></sf:characterstyle>')
    spans = "".join(f'<sf:span sf:style="c{i % 8}">s{i} </sf:span>' for i in range(n))
    marks = "".join('<sf:annotation-field sfa:ID="af">x</sf:annotation-field>' for _ in range(20000))
    lists = ''.join(f'<sf:liststyle sf:ident="l{i}" sf:parent-ident="l{i + 1}"/>' for i in range(8))
    lists += f'<sf:liststyle sf:ident="l8">{_labels("none", "bullet")}</sf:liststyle>'
    items = "".join(_p(f"i{i}", list_style="l0", list_level=1) for i in range(20000))
    pic = ('<sf:unfiltered sfa:ID="shared">' + "".join("<sf:pad/>" for _ in range(20000))
           + '<sf:data sf:path="a.png"/></sf:unfiltered>')
    refs = "".join('<sf:media><sf:unfiltered-ref sfa:IDREF="shared"/></sf:media>' for _ in range(20000))
    extra = ('<sf:annotations><sf:annotation sf:target="af"><sf:text-storage><sf:text-body>' + _p("c")
             + '</sf:text-body></sf:text-storage></sf:annotation></sf:annotations>'
             '<sf:attachments><sf:attachment sfa:ID="z">' + pic + '</sf:attachment></sf:attachments>')
    data = pages09(_p(spans) + _p(marks) + items + refs, styles=chain + lists, extra=extra,
                   files={"a.png": PNG})
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "pages")
    took = time.perf_counter() - t0
    first = doc.blocks[0].inlines
    assert len(first) == 1 and isinstance(first[0], Bold) and first[0].inlines[0].text.startswith("s0 s1 "), first[:2]
    assert len(doc.notes) == 1 and sum(isinstance(b, ListItem) for b in doc.blocks) == 20000
    assert sum(isinstance(b, Image) for b in doc.blocks) == 20000 and not ctx.skipped, ctx.skipped
    assert took < 2, took
    # 10,000 list styles sharing one 20,000-entry label array by ref, and 20,000 picture refs to targets nested in
    # one another: each array and each target is read once, near itself
    arr = '<sf:array sfa:ID="arr">' + '<sf:list-label-typeinfo sf:type="text"/>' * 20000 + "</sf:array>"
    styles = "".join(f'<sf:liststyle sf:ident="s{i}"><sf:property-map><sf:listLabelTypes><sf:array-ref '
                     f'sfa:IDREF="arr"/></sf:listLabelTypes></sf:property-map></sf:liststyle>' for i in range(10000))
    items = "".join(_p(f"i{i}", list_style=f"s{i}", list_level=1) for i in range(10000))
    nest = "".join(f'<sf:x sfa:ID="n{i}">' for i in range(20000)) + '<sf:data sf:path="a.png"/>' + "</sf:x>" * 20000
    refs = "".join(f'<sf:media><sf:unfiltered-ref sfa:IDREF="n{i}"/></sf:media>' for i in range(20000))
    data = pages09(items + refs, styles=styles + f'<sf:liststyle sf:ident="holder"><sf:x>{arr}</sf:x></sf:liststyle>',
                   extra=f'<sf:attachments><sf:attachment>{nest}</sf:attachment></sf:attachments>',
                   files={"a.png": PNG})
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "pages")
    took = time.perf_counter() - t0
    assert sum(isinstance(b, ListItem) for b in doc.blocks) == 10000, doc.blocks[:3]
    # the ref targets more than REF_DEPTH levels above the data hold no picture near them: counted `image`
    assert sum(isinstance(b, Image) for b in doc.blocks) == 3 and ctx.skipped == {"image": 19997}, ctx.skipped
    assert took < 2, took


# -- iWork '09 Numbers and '09 tables (Task 16)

def _grid09(rows, *, quotas=None, numcols=None, numrows=None, nc=None) -> str:
    """An sf:grid: `rows` is a list of rows, each the XML of that row's datasource entries; `quotas` the
    sf:grid-column counts (None leaves sf:columns out); `nc` overrides the sf:grid-row counts."""
    ncols = numcols if numcols is not None else max((len(r) for r in rows), default=0)
    nrows = numrows if numrows is not None else len(rows)
    cols = ("<sf:columns>" + "".join(f'<sf:grid-column sf:nc="{n}"/>' for n in quotas) + "</sf:columns>"
            if quotas is not None else "")
    counts = nc if nc is not None else [len(r) for r in rows]
    return (f'<sf:grid sf:numcols="{ncols}" sf:numrows="{nrows}">{cols}<sf:datasource>'
            + "".join("".join(r) for r in rows) + "</sf:datasource><sf:rows>"
            + "".join(f'<sf:grid-row sf:nc="{n}"/>' for n in counts) + "</sf:rows></sf:grid>")


def _table09(name, rows, **kw) -> str:
    return (f'<sf:tabular-info><sf:tabular-model sf:name="{name}" sf:num-header-rows="1">{_grid09(rows, **kw)}'
            '</sf:tabular-model></sf:tabular-info>')


def _ws(name, drawables) -> str:
    return (f'<ls:workspace ls:workspace-name="{name}"><ls:page-info><sf:layers><sf:layer><sf:drawables>'
            f'{drawables}</sf:drawables></sf:layer></sf:layers></ls:page-info></ls:workspace>')


def _t(s) -> str:
    return f'<sf:t><sf:ct sfa:s="{s}"/></sf:t>'


def _n(v) -> str:
    return f'<sf:n sf:v="{v}"/>'


def test_09_numbers_cells():
    rich = ('<sf:t><sf:ct><sf:so><sf:text-storage><sf:text-body><sf:p>Two<sf:br/>lines<sf:br/></sf:p>'
            '</sf:text-body></sf:text-storage></sf:so></sf:ct></sf:t>')
    popup = ('<sf:pm><sf:menu-choices><sf:t sfa:ID="c1"><sf:ct sfa:s="Low"/></sf:t><sf:t sfa:ID="c2">'
             '<sf:ct sfa:s="High"/></sf:t></sf:menu-choices><sf:proxied-cell-ref sfa:IDREF="c2"/></sf:pm>')
    rows = [[_t("Name"), _n("4650"), _n("3.25")],
            ['<sf:d sf:cell-date="0"/>', '<sf:d sf:cell-date="43200"/>', '<sf:b sf:v="1"/>'],
            ['<sf:cb sf:v="0"/>', popup, rich, "<sf:g/>"]]
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("Sheet 1", _table09("Table 1", rows, numcols=4))), "numbers")
    assert seen == {(0, 0): "Name", (0, 1): "4650", (0, 2): "3.25", (1, 0): "2001-01-01",
                    (1, 1): "2001-01-01 12:00:00", (1, 2): "TRUE", (2, 0): "FALSE", (2, 1): "High",
                    (2, 2): "Two\nlines"}, seen
    [sec] = doc.blocks
    assert (sec.kind, sec.title) == ("sheet", "Sheet 1") and [t.caption for t in _tables(doc)] == ["Table 1"]
    assert not ctx.skipped and not ctx.warnings, (ctx.skipped, ctx.warnings)
    # a pop-up whose choice is a pop-up naming the first: the cycle counts `depth` and the cell shows nothing; a
    # choice shared by 3,000 cells is followed once
    loop = ['<sf:pm sfa:ID="p1"><sf:proxied-cell-ref sfa:IDREF="p2"/></sf:pm>',
            '<sf:pm sfa:ID="p2"><sf:proxied-cell-ref sfa:IDREF="p1"/></sf:pm>']
    shared = '<sf:pm><sf:proxied-cell-ref sfa:IDREF="c2"/></sf:pm>'
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("P", [[popup, *loop]] + [[shared]] * 3000))), "numbers")
    assert seen == {(r, 0): "High" for r in range(3001)}, len(seen)
    assert ctx.skipped == {"depth": 1}, ctx.skipped


def test_09_numbers_formula_result():
    cells = ['<sf:f><sf:fo sf:fs="=SUM(A1:A2)"/><sf:r><sf:rn sf:v="7"/></sf:r></sf:f>',
             '<sf:f><sf:fo sf:fs="=CONCATENATE(B1)"/><sf:r><sf:rt><sf:ct sfa:s="joined"/></sf:rt></sf:r></sf:f>',
             '<sf:f><sf:r><sf:rd sf:cell-date="86400"/></sf:r></sf:f>',
             '<sf:f><sf:r><sf:rb sf:v="1"/></sf:r></sf:f>',
             '<sf:f><sf:fo sf:fs="=NOW()"/></sf:f>']
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("F", [cells]))), "numbers")
    assert seen == {(0, 0): "7", (0, 1): "joined", (0, 2): "2001-01-02", (0, 3): "TRUE"}, seen
    md = _md(doc, ctx)
    assert "SUM" not in md and "CONCATENATE" not in md and "NOW" not in md and "joined" in md, md
    assert not ctx.skipped, ctx.skipped


def test_09_numbers_spans():
    # sf:col-span 2 makes one merged cell; an sf:s holds its column without a value
    wide = '<sf:t sf:col-span="2"><sf:ct sfa:s="Wide"/></sf:t>'
    rows = [[wide, _t("C")], [_t("a"), _t("b"), _t("c")], ["<sf:s/>", _t("q")]]
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("M", rows, numcols=3))), "numbers")
    assert seen == {(0, 0): "Wide", (0, 2): "C", (1, 0): "a", (1, 1): "b", (1, 2): "c", (2, 1): "q"}, seen
    [tab] = _tables(doc)
    assert [c.colspan for c in tab.rows[0]] == [2, 1], tab.rows[0]
    md = _md(doc, ctx)
    assert md.count('colspan="2"') == 1, md
    assert not ctx.skipped, ctx.skipped
    # the column counts place a sparse row: a column whose count is spent is passed over (docling `place_row`)
    rows = [[_t("a"), _t("b")], [_t("y")], [_t("z")]]
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("Q", rows, numcols=3, quotas=[1, 2, 1]))), "numbers")
    assert seen == {(0, 0): "a", (0, 1): "b", (1, 1): "y", (2, 2): "z"} and not ctx.skipped, (seen, ctx.skipped)
    # counts that do not add up are not used: each row starts at its first column
    with _puts() as seen:
        _x(numbers09(_ws("S", _table09("Q", rows, numcols=3, quotas=[1, 2, 9]))), "numbers")
    assert seen == {(0, 0): "a", (0, 1): "b", (1, 0): "y", (2, 0): "z"}, seen


def test_09_numbers_two_sheets():
    template = _table09("TEMPLATE", [[_t("TEMPLATE CELL")]])  # outside the workspaces: never read
    sheets = (_ws("A", _table09("Ta", [[_t("alpha")]]))
              + _ws("B", _table09("Tb", [[_t("beta")]]) + '<sf:drawable-shape>' + _box(_p("box text"))
                    + '</sf:drawable-shape><sf:sticky-note>' + _box(_p("remember")) + '</sf:sticky-note>'))
    # a template attachment outside the workspaces is neither shown nor counted
    loose = '<sf:attachments><sf:attachment sfa:ID="z">' + _p("TEMPLATE BOX") + '</sf:attachment></sf:attachments>'
    doc, ctx = _x(numbers09(sheets, extra=f"<ls:template>{template}{loose}</ls:template>"), "numbers")
    assert [(s.kind, s.title) for s in doc.blocks] == [("sheet", "A"), ("sheet", "B")], doc.blocks
    md = _md(doc, ctx)
    assert "## Sheet: A" in md and "## Sheet: B" in md and md.find("## Sheet: A") < md.find("## Sheet: B"), md
    assert "alpha" in md and "beta" in md and "box text" in md and "> Comment: remember" in md, md
    assert "TEMPLATE" not in md and not ctx.skipped, (md, ctx.skipped)


def test_09_numbers_sheet_cap():
    rows = [[_t("a"), _t("b"), _t("c")], [_t("d"), _t("e"), _t("f")]]
    with _limit(limits, "SHEET_CELLS", 4):
        doc, ctx = _x(numbers09(_ws("S", _table09("T", rows)) + _ws("S2", _table09("T2", [[_t("g")]]))),
                      "numbers")
    assert ctx.skipped == {"sheet_cut": 2}, ctx.skipped
    assert ctx.warnings == ["sheet T cut at 4 cells", "sheet T2 cut at 4 cells"], ctx.warnings
    assert [s.title for s in doc.blocks] == ["S", "S2"], doc.blocks
    # the datasource entries walked are charged to one scan count a document, empty ones too
    with _limit(limits, "SHEET_SCAN", 5):
        doc, ctx = _x(numbers09(_ws("S", _table09("T", [["<sf:g/>"] * 3 + [_t("x")] * 3]))), "numbers")
    assert ctx.skipped == {"sheet_cut": 1} and ctx.warnings == ["sheet T cut at 5 scanned cells"], ctx.warnings
    assert [_cells(t) for t in _tables(doc)] == [[["x", "x"]]], _tables(doc)


def test_09_numbers_full_rows_first():
    # an empty top-left corner above full rows (the usual chart-data layout): row counts 4, 5, 5 and column counts
    # 2, 3, 3, 3, 3 on a 3 x 5 grid; the full rows spend column 0's count, so row 0 lies in columns 1-4
    rows = [[_t(h) for h in "ABCD"], [_t("r1")] + [_n(i) for i in range(4)], [_t("r2")] + [_n(i) for i in range(4)]]
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("C", rows, numcols=5, quotas=[2, 3, 3, 3, 3]))), "numbers")
    assert [k for k in seen if k[0] == 0] == [(0, 1), (0, 2), (0, 3), (0, 4)], seen
    assert len(seen) == 14 and not ctx.skipped, (seen, ctx.skipped)
    # a row is full when its spans and sf:s fill the declared columns
    rows = [[_t("x")], ['<sf:t sf:col-span="2"><sf:ct sfa:s="w"/></sf:t>', "<sf:s/>"]]
    with _puts() as seen:
        doc, ctx = _x(numbers09(_ws("S", _table09("D", rows, numcols=3, quotas=[1, 1, 1]))), "numbers")
    assert seen == {(0, 2): "x", (1, 0): "w"} and not ctx.skipped, (seen, ctx.skipped)


def test_09_numbers_drop_then_cut():
    # an entry dropped before a later cut still counts `record`, once
    rows = [[_t("a"), _t("b"), _t("c")], [_t("d"), _t("e")]]
    with _limit(limits, "SHEET_SCAN", 4):
        doc, ctx = _x(numbers09(_ws("S", _table09("T", rows, numcols=2, nc=[3, 2]))), "numbers")
    assert ctx.skipped == {"sheet_cut": 1, "record": 1}, ctx.skipped


def test_09_charts_counted():
    # an sf:chart-info is a leaf: counted `chart` once, never read, nothing under it reaches the output
    chart = '<sf:chart-info sfa:ID="ch">' + _box(_p("CHART TEXT")) + _media("c.png") + '</sf:chart-info>'
    files = {"c.png": PNG}
    doc, ctx = _x(pages09(_p("Body") + chart + _p("x" + chart.replace('"ch"', '"ch2"')), files=files))
    assert doc.blocks == [Para([Text("Body")]), Para([Text("x")])] and ctx.skipped == {"chart": 2}, ctx.skipped
    doc, ctx = _x(keynote09(slide09(_p("T"), _p("B"), drawables=chart), files=files), "key")
    assert doc.blocks[0].blocks == [Para([Text("B")])] and ctx.skipped == {"chart": 1}, ctx.skipped
    doc, ctx = _x(numbers09(_ws("S", _table09("T", [[_t("v")]]) + chart), files=files), "numbers")
    md = _md(doc, ctx)
    assert "CHART" not in md and "v" in md and ctx.skipped == {"chart": 1}, (md, ctx.skipped)


def test_09_popup_chain_linear():
    # choices nested in a chain (sf:t > sf:ct > sf:t > ...), each named by a pop-up of its own: a cell's text walk
    # stops at a nested cell, so the walks are disjoint and the time linear
    n = 8000
    chain = "".join(f'<sf:t sfa:ID="c{i}"><sf:ct>v{i}' for i in range(n)) + "</sf:ct></sf:t>" * n
    first = f'<sf:pm><sf:menu-choices>{chain}</sf:menu-choices><sf:proxied-cell-ref sfa:IDREF="c0"/></sf:pm>'
    rows = [[first]] + [[f'<sf:pm><sf:proxied-cell-ref sfa:IDREF="c{i}"/></sf:pm>'] for i in range(1, n)]
    data = numbers09(_ws("S", _table09("P", rows)))
    # 1,000 one-cell tables naming distinct choices in a 20,000-deep chain
    deep = 20000
    chain = "".join(f'<sf:t sfa:ID="d{i}"><sf:ct>w{i}' for i in range(deep)) + "</sf:ct></sf:t>" * deep
    tables = _table09("Holder", [[f'<sf:pm><sf:menu-choices>{chain}</sf:menu-choices>'
                                  '<sf:proxied-cell-ref sfa:IDREF="d0"/></sf:pm>']])
    tables += "".join(_table09(f"T{i}", [[f'<sf:pm><sf:proxied-cell-ref sfa:IDREF="d{i * 20}"/></sf:pm>']])
                      for i in range(1, 1000))
    data2 = numbers09(_ws("S", tables))
    got = []
    for d in (data, data2):
        ctx = Context()
        t0 = time.perf_counter()
        doc = iwork.parse_bytes(d, ctx, "numbers")
        took = time.perf_counter() - t0
        assert took < 1, took
        assert not ctx.skipped, ctx.skipped
        got.append([_cells(t) for t in _tables(doc)])
    [one] = got[0]
    assert len(one) == n and one[0] == ["v0"] and one[5] == ["v5"] and one[-1] == [f"v{n - 1}"], one[:3]
    assert len(got[1]) == 1000 and got[1][0] == [["w0"]] and got[1][1] == [["w20"]], got[1][:3]
    # past SHEET_SCAN the walk is cut, counted once a table
    with _limit(limits, "SHEET_SCAN", 50):
        doc, ctx = _x(data, "numbers")
    assert ctx.skipped == {"sheet_cut": 1}, ctx.skipped


def test_09_pages_table():
    t = _table09("PT", [[_t("p"), _n("2")]])
    doc, ctx = _x(pages09(_p("Before") + t + _p("After")))
    assert [type(b).__name__ for b in doc.blocks] == ["Para", "Table", "Para"], doc.blocks
    tab = doc.blocks[1]
    assert tab.caption == "PT" and [[c.blocks for c in r] for r in tab.rows] == [
        [[Para([Text("p")])], [Para([Text("2")])]]], tab.rows
    assert not ctx.skipped, ctx.skipped


def test_09_keynote_table():
    t = _table09("KT", [[_t("k"), _n("1")], [_t("l"), _n("2")]])
    doc, ctx = _x(keynote09(slide09(_p("T"), _p("B"), drawables=t)), "key")
    [s] = doc.blocks
    assert s.title == "T" and isinstance(s.blocks[0], Para) and isinstance(s.blocks[1], Table), s.blocks
    assert s.blocks[1].caption == "KT" and len(s.blocks[1].rows) == 2 and not ctx.skipped, (s.blocks, ctx.skipped)


def test_09_numbers_hostile_linear():
    # declared counts and spans far past the caps, and many cells: read in linear time, stopped at the caps
    big = "1000000000000000"
    spans = _table09("Spans", [[f'<sf:t sf:col-span="{big}"><sf:ct sfa:s="w"/></sf:t>', _n(1)]] * 2000,
                     numcols=big, numrows=big)
    # every row's first value spans every column, spending their counts; the rest of each row has no column left
    quotas = _table09("Quotas", [[f'<sf:n sf:v="1" sf:col-span="{big}"/>'] + [_n(2)] * 999] * 100,
                      numcols=1000, quotas=[100] * 1000)
    rows = _table09("Rows", [[]] * 150_000 + [[_t("late")]])
    many = _table09("Many", [[_n(c) for c in range(100)]] * 1000, numcols=big, nc=[big] + [100] * 999)
    data = numbers09(_ws("A", spans) + _ws("B", quotas) + _ws("C", rows) + _ws("D", many))
    assert len(data) > 5_000_000
    ctx = Context()
    t0 = time.perf_counter()
    doc = iwork.parse_bytes(data, ctx, "numbers")
    took = time.perf_counter() - t0
    tabs = {s.title: [b for b in s.blocks if isinstance(b, Table)] for s in doc.blocks}
    assert [len(t.rows) for t in tabs["A"]] == [1] and [len(t.rows) for t in tabs["B"]] == [100], tabs["A"]
    assert tabs["C"] == [] and [len(t.rows) for t in tabs["D"]] == [1], tabs["D"]  # one row takes every value
    assert ctx.skipped == {"sheet_cut": 3, "record": 1}, ctx.skipped
    assert ctx.warnings == ["sheet Spans cut at 1000 columns", "sheet Rows cut at 100000 rows",
                            "sheet Many cut at 1000 columns"], ctx.warnings
    assert took < 3, took


def measure(what):
    """Not a test: `snappy` unpacks 256 MiB of 64 KiB blocks (a 4 KiB literal, then copy-2 tags) and prints
    `SNAPPY <MiB> <s>`; `archives` parses IWA_OBJECTS small objects in one unpacked stream and prints
    `ARCHIVES <objects> <MiB> <s>`. Built in memory."""
    if what == "snappy":
        lit = bytes(range(256)) * 16
        body = snappy_literal(lit)[2:]  # tags without the declared length
        copies = b"".join(snappy_copy(4096, 64) for _ in range((65536 - 4096) // 64))
        block = bytes([0x80, 0x80, 0x04]) + body + copies   # declared 65536
        assert snappy.raw(block, 65536) is not None
        data = chunk(block) * (limits_iwa_bytes() // 65536)
        t0 = time.perf_counter()
        out = snappy.iwa(data, flate.Budget(limits_iwa_bytes()))
        took = time.perf_counter() - t0
        print(f"SNAPPY {len(out) / 2**20:.0f} {took:.2f}")
    elif what == "archives":
        n = iwork_iwa.IWA_OBJECTS
        stream = b"".join(archive(i, 1, b"payload-") for i in range(1, n + 1))
        assert len(stream) <= iwork_iwa.IWA_BYTES, len(stream)
        t0 = time.perf_counter()
        found, capped, _, _ = iwork_iwa._archives(stream, {})
        took = time.perf_counter() - t0
        assert len(found) == n and not capped
        print(f"ARCHIVES {n} {len(stream) / 2**20:.1f} {took:.2f}")
    else:
        raise SystemExit("measure snappy | archives")


def limits_iwa_bytes():
    return iwork_iwa.IWA_BYTES


def main():
    if sys.argv[1:2] == ["measure"]:
        measure(*sys.argv[2:3])
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
