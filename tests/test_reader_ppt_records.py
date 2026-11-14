"""ppt persist chain: the edit chain, the directory, the encrypted token and the fallbacks.

Run: python -I -X utf8 tests/test_reader_ppt_records.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import ppt_records as pr  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_ppt  # noqa: E402

TWO = [{"title": "One", "body": ["a", ("b", 1)]}, {"title": "Two", "body": ["c"]}]


def build(slides=TWO, **kw):
    """Make a .ppt, return (info, PowerPoint Document stream, Current User stream)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.ppt"
        info = make_ppt(path, slides, **kw)
        with Compound(path) as cf:
            return info, cf.read("PowerPoint Document"), (cf.read("Current User") if cf.exists("Current User") else None)


def test_persist_newest_wins():
    info, doc, cu = build(edits=2)
    p = pr.Persist(doc, cu, Context())
    slide1 = info["slides"][0]
    old, new = info["slide1_offsets"]
    assert old != new and p.objects[slide1] == new == info["offsets"][slide1], (p.objects, info)
    assert p.obj(slide1)[0] == pr.SLIDE_CONTAINER
    assert p.obj(info["slides"][1])[0] == pr.SLIDE_CONTAINER


def test_edit_loop():
    info, doc, cu = build()
    doc = bytearray(doc)
    at = info["user_edits"][0]
    struct.pack_into("<I", doc, at + 8 + 8, at)  # lastEdit points at the atom itself
    t = time.perf_counter()
    p = pr.Persist(bytes(doc), cu, Context())
    assert time.perf_counter() - t < 0.1
    assert p.obj(p.doc_ref)[0] == pr.DOCUMENT_CONTAINER


def test_directory_overrun():
    info, doc, cu = build()
    doc = bytearray(doc)
    at = info["directories"][0]
    struct.pack_into("<I", doc, at + 4, 8)  # an atom of 8 bytes: one entry head and one offset
    struct.pack_into("<I", doc, at + 8, 1 | 0xFFF << 20)
    ctx = Context()
    try:
        pr.Persist(bytes(doc), cu, ctx)
    except ReadError:
        pass
    assert ctx.skipped["record"] == 1, ctx.skipped


def test_directory_overrun_keeps_older_directory():
    info, doc, cu = build(edits=2)
    old, new = info["slide1_offsets"]
    doc = bytearray(doc)
    at = info["directories"][1]  # the newest edit's directory: slide 1's new offset
    struct.pack_into("<I", doc, at + 8, info["slides"][0] | 0xFFF << 20)  # its only entry runs past the atom
    ctx = Context()
    p = pr.Persist(bytes(doc), cu, ctx)
    assert ctx.skipped["record"] == 1, ctx.skipped
    assert p.objects[info["slides"][0]] == old and p.obj(info["slides"][1])[0] == pr.SLIDE_CONTAINER, p.objects
    assert p.obj(p.doc_ref)[0] == pr.DOCUMENT_CONTAINER


def test_directory_overrun_keeps_entries_before_it():
    # the entries before the overrunning one are kept and the overrunning one is dropped whole; it runs past the
    # atom, so it is the last entry the atom holds and none follows it. The slides are missing because the rewritten
    # directory lists only ids 1 and 2 before it, where the one run it replaces listed all four ids
    info, doc, cu = build()
    doc = bytearray(doc)
    at = info["directories"][0]
    assert struct.unpack_from("<I", doc, at + 4)[0] == 4 + 4 * 4  # one run of the four ids
    keep = [struct.unpack_from("<I", doc, at + 12 + 4 * k)[0] for k in range(2)]
    struct.pack_into("<IIIII", doc, at + 8, 1 | 2 << 20, keep[0], keep[1], 3 | 0xFFF << 20, 0)
    ctx = Context()
    p = pr.Persist(bytes(doc), cu, ctx)
    assert ctx.skipped["record"] == 1, ctx.skipped
    assert p.obj(info["doc"])[0] == pr.DOCUMENT_CONTAINER and p.obj(info["master"])[0] == pr.MAIN_MASTER
    assert p.obj(info["slides"][0]) is None and p.obj(info["slides"][1]) is None, p.objects


def test_repeated_last_edit_counts_one_record():
    info, doc, cu = build(edits=3)
    doc = bytearray(doc)
    oldest, middle, newest = info["user_edits"]
    struct.pack_into("<I", doc, oldest + 8 + 8, newest)  # the oldest edit points back at the newest: a cycle
    ctx = Context()
    p = pr.Persist(bytes(doc), cu, ctx)
    assert ctx.skipped["record"] == 1, ctx.skipped
    assert p.obj(p.doc_ref)[0] == pr.DOCUMENT_CONTAINER
    info, doc, cu = build()
    doc = bytearray(doc)
    struct.pack_into("<I", doc, info["user_edits"][0] + 8 + 8, info["user_edits"][0])  # points at itself
    ctx = Context()
    pr.Persist(bytes(doc), cu, ctx)
    assert ctx.skipped["record"] == 1, ctx.skipped


def test_encrypted_token():
    info, doc, cu = build(encrypt="x")
    p = pr.Persist(doc, cu, Context())
    assert p.encrypted and p.crypt_ref == info["crypt"], (p.encrypted, p.crypt_ref)
    assert p.obj(p.crypt_ref)[0] == pr.CRYPT_SESSION10_CONTAINER
    info, doc, cu = build()
    p = pr.Persist(doc, cu, Context())
    assert not p.encrypted and p.crypt_ref is None


def test_no_current_user():
    info, doc, cu = build(edits=2)
    want = pr.Persist(doc, cu, Context()).objects
    for missing in (None, b"", b"\0" * 7):
        p = pr.Persist(doc, missing, Context())
        assert p.objects == want
        assert p.obj(p.doc_ref)[0] == pr.DOCUMENT_CONTAINER


def test_no_directory():
    info, doc, cu = build()
    doc = bytearray(doc)
    for at in info["user_edits"]:
        struct.pack_into("<H", doc, at + 2, 0x0FF9)  # no longer a UserEditAtom
    for given in (cu, None):
        try:
            pr.Persist(bytes(doc), given, Context())
        except ReadError as e:
            assert e.kind == "damaged", e
        else:
            raise AssertionError("expected ReadError")


def test_directory_work_budget():
    info, doc, cu = build(edits=60)  # 59 later directories repeat slide 1's id: no new id, still work
    assert pr.Persist(doc, cu, Context()).obj(info["doc"]) is not None
    saved = pr.limits.BIN_RECORDS
    pr.limits.BIN_RECORDS = 40
    try:
        ctx = Context()
        pr.Persist(doc, cu, ctx)
    finally:
        pr.limits.BIN_RECORDS = saved
    assert ctx.skipped["record"] == 1, ctx.skipped


def test_doc_ref_missing_from_directory():
    info, doc, cu = build()
    doc = bytearray(doc)
    at = info["directories"][0]
    entry = struct.unpack_from("<I", doc, at + 8)[0]
    struct.pack_into("<I", doc, at + 8, entry + 1)  # ids 2.. instead of 1..: the document id is gone
    p = pr.Persist(bytes(doc), cu, Context())
    assert p.doc_ref == info["doc"] and p.obj(p.doc_ref) is None


def test_slots_patch_one_offset():
    info, doc, cu = build([{"title": "A"}, {"title": "B"}, {"title": "C"}])
    pids = info["slides"]
    assert struct.unpack_from("<I", doc, info["slots"][pids[1]])[0] == info["offsets"][pids[1]]
    doc = bytearray(doc)
    struct.pack_into("<I", doc, info["slots"][pids[1]], len(doc) + 100)
    p = pr.Persist(bytes(doc), cu, Context())
    assert p.obj(pids[1]) is None
    assert p.obj(pids[0])[0] == pr.SLIDE_CONTAINER and p.obj(pids[2])[0] == pr.SLIDE_CONTAINER


def test_every_persist_id_resolves():
    info, doc, cu = build(notes={1: "n1", 2: "n2"})
    p = pr.Persist(doc, cu, Context())
    kinds = {info["doc"]: pr.DOCUMENT_CONTAINER, info["master"]: pr.MAIN_MASTER}
    kinds.update({pid: pr.SLIDE_CONTAINER for pid in info["slides"]})
    kinds.update({pid: pr.NOTES_CONTAINER for pid in info["notes"].values()})
    assert len(kinds) == 6
    for pid, typ in kinds.items():
        assert p.obj(pid)[0] == typ, (pid, typ, p.obj(pid))
    assert p.doc_ref == info["doc"]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
