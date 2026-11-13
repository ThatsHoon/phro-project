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
