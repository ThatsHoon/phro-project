"""The reader package skeleton, its limits and the safe zip container.

Run: python -I -X utf8 tests/test_reader_zipx.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits  # noqa: E402
from reader.containers import zipx  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader_fix import make_zip  # noqa: E402


def tmp():
    return Path(tempfile.mkdtemp())


def raises_kind(fn, kind):
    try:
        fn()
    except ReadError as e:
        assert e.kind == kind, (e.kind, kind)
        return
    raise AssertionError("ReadError not raised")


def test_formats_cover_scope():
    assert len(reader.FORMATS) == 28 and not (set(reader.FORMATS) & reader.SKILL_EXTS)
    assert reader.ext_of("C:/x/A.HWPX") == "hwpx"
    assert reader.ext_of("C:/x/noext") == ""
    assert reader.BUILT == set()


def test_read_and_case_insensitive_lookup():
    p = make_zip(tmp() / "a.zip", {"mimetype": "x", "Contents/Section0.xml": "<a/>"})
    pkg = zipx.Package(p)
    assert pkg.has("contents/section0.xml")
    assert pkg.read("contents/section0.xml") == b"<a/>"
    assert pkg.xml("missing.xml") is None


def test_xml_refuses_doctype():
    bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aa">]><x>&a;</x>'
    raises_kind(lambda: zipx.parse_xml(bad), "damaged")
    raises_kind(lambda: zipx.parse_xml(b"<x"), "damaged")
    assert zipx.local(zipx.parse_xml(b"<x/>").tag) == "x"


def test_not_a_zip_is_damaged():
    p = tmp() / "n.zip"
    p.write_bytes(b"hello")
    raises_kind(lambda: zipx.Package(p), "damaged")


def test_ratio_bomb_is_too_large():
    p = make_zip(tmp() / "b.zip", {"big.bin": b"\0" * (3 * 2**20)}, stored=())
    old = limits.ZIP_RATIO
    limits.ZIP_RATIO = 10
    try:
        raises_kind(lambda: zipx.Package(p).read("big.bin"), "too_large")
    finally:
        limits.ZIP_RATIO = old


def test_encrypted_member():
    p = tmp() / "e.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("s.xml", b"<a/>")
        z.getinfo("s.xml").flag_bits |= 1  # writestr resets it, so mark the central entry
    raises_kind(lambda: zipx.Package(p).read("s.xml"), "encrypted")


def test_rels_resolve():
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="t/doc" Target="word/document.xml"/></Relationships>')
    drels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId2" Type="t/img" Target="media/a%20b.png"/>'
             '<Relationship Id="rId3" Type="t/link" Target="http://x.test/" TargetMode="External"/>'
             '</Relationships>')
    p = make_zip(tmp() / "r.zip", {"_rels/.rels": rels, "word/_rels/document.xml.rels": drels,
                                   "word/document.xml": "<d/>"}, stored=())
    pkg = zipx.Package(p)
    assert pkg.rels("")["rId1"].target == "word/document.xml"
    assert pkg.rels("word/document.xml")["rId2"].target == "word/media/a b.png"
    assert pkg.rels("word/document.xml")["rId3"].external
    assert pkg.rels("nothing/here.xml") == {}
    assert zipx.resolve("a/b.xml", "/c/d.xml") == "c/d.xml"
    assert zipx.resolve("a/b.xml", "../c.xml") == "c.xml"


def test_local_name_helpers():
    el = zipx.parse_xml(b'<p:a xmlns:p="u1" xmlns:r="u2"><p:b r:id="7"/><c><x:b xmlns:x="u3"/></c></p:a>')
    b = zipx.first(el, "b")
    assert zipx.attr(b, "id") == "7" and zipx.attr(b, "zz", "d") == "d"
    assert len(zipx.kids(el, "b")) == 1 and len(zipx.walk(el, "b")) == 2
    assert zipx.first(el, "nope") is None


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
