"""iWork: raw Snappy, the IWA chunk stream, the protobuf wire reader, the container and the IWA object store.

Run: python -I -X utf8 tests/test_reader_iwork.py
"""
import gzip
import struct
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from iwork_fix import (archive, chunk, iwa_member, iwork_zip, metadata_iwa, pb, pb_ref,  # noqa: E402
                       snappy_copy, snappy_literal)
from reader import limits  # noqa: E402
from reader.containers import flate, pbwire, snappy  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.formats import iwork, iwork_iwa  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Doc, Image, Para, Text  # noqa: E402


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
    assert iwork_iwa.WALKS == {}


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
