"""Tests for Hancom document scripts: hwp's Scripts/DefaultJScript and hwpx's Scripts parts, read and kept out of the
main text, written as files beside it.

Run: python -I -X utf8 tests/test_reader_hwp_scripts.py
"""
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import gate, limits, markdown  # noqa: E402
from reader.model import Context, Doc, Para, Text  # noqa: E402
from reader_fix import hwp_para, make_dist_section, make_hwp  # noqa: E402
import test_reader_hwpx as in_hwpx  # noqa: E402

HEADER = "var Documents = XHwpDocuments;\r\nvar Document = Documents.Active_XHwpDocument;\r\n"
NEW = "function OnDocument_New()\r\n{\r\n\t//todo : \r\n}\r\n"
OPEN = 'function OnDocument_Open()\r\n{\r\n\tDocument.Run("x");\r\n}\r\n'
OPENED = ("source", "JScript source", 'function OnDocument_Open()\n{\n\tDocument.Run("x");\n}', "js")
DIST = 1 << 2


def raw(*parts, end=True):
    """The parts as DefaultJScript holds them: each a u32 length in UTF-16 units and the text, then -1."""
    out = b"".join(struct.pack("<I", len(p.encode("utf-16-le")) // 2) + p.encode("utf-16-le") for p in parts)
    return out + (struct.pack("<i", -1) if end else b"")


def packed(data):
    """Raw deflate, then the CRC-32 and the length of `data`, as Hancom writes a script stream."""
    c = zlib.compressobj(wbits=-15)
    return c.compress(data) + c.flush() + struct.pack("<II", zlib.crc32(data), len(data))


def hwp(stream, flags=1):
    extra = {} if stream is None else {"Scripts/DefaultJScript": stream,
                                       "Scripts/JScriptVersion": packed(struct.pack("<II", 1, 0))}
    path = make_hwp(Path(tempfile.mkdtemp()) / "a.hwp", [hwp_para("body")], flags=flags, extra=extra)
    ctx = Context()
    return reader.parse(path, ctx), ctx


def test_hwp_scripts_are_read():
    doc, ctx = hwp(packed(raw(HEADER, OPEN, "", "")))
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)
    doc, ctx = hwp(raw(HEADER, OPEN, "", ""), flags=0)
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)
    # a stream that ends after its last part, without -1, is read
    doc, ctx = hwp(packed(raw(HEADER, OPEN, end=False)))
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)


def test_a_new_documents_scripts_are_left_out():
    for stream in (packed(raw(HEADER, NEW, "", "")), packed(raw(HEADER, NEW + "\r\n", "", ""))):
        doc, ctx = hwp(stream)
        assert "scripts" not in doc.meta and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)
    doc, ctx = hwp(None)
    assert "scripts" not in doc.meta and not ctx.skipped, ctx.skipped


def test_every_part_a_new_document_lacks_is_listed():
    doc, ctx = hwp(packed(raw("var n = 1;\r\n", NEW, "\r\n  before();\r\n\r\n", "after();\rdone();", "fifth();")))
    assert doc.meta.get("scripts") == [("header", "JScript header", "var n = 1;", "js"),
                                       ("pre-source", "JScript pre-source", "  before();", "js"),
                                       ("post-source", "JScript post-source", "after();\ndone();", "js")
                                       ], doc.meta.get("scripts")
    assert not ctx.skipped, ctx.skipped
    # what follows the fourth part is not read, so stray bytes there lose nothing
    doc, ctx = hwp(packed(raw(HEADER, OPEN, "", "", end=False) + b"\x01"))
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, ctx.skipped


def test_a_script_stream_that_cannot_be_read_is_named():
    cut = raw(HEADER, OPEN)[:-6]  # the source's text ends short of its length
    negative = raw(HEADER, end=False) + struct.pack("<i", -2)
    for stream in (b"\xff\xfe not deflate", packed(cut), packed(raw(HEADER))[:5], packed(negative)):
        doc, ctx = hwp(stream)
        assert "scripts" not in doc.meta and ctx.skipped == {"script": 1}, (stream, ctx.skipped)
    old = limits.SCRIPT_BYTES
    limits.SCRIPT_BYTES = 300
    try:
        bomb = packed(raw(HEADER, "a" * 1000))  # small, inflating past the limit
        for flags, stream in ((1, bomb), (0, raw(HEADER, OPEN, "a" * 100))):
            doc, ctx = hwp(stream, flags)
            assert "scripts" not in doc.meta and ctx.skipped == {"script": 1}, (flags, ctx.skipped)
        doc, ctx = hwp(raw(HEADER, OPEN), 0)
        assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, ctx.skipped
    finally:
        limits.SCRIPT_BYTES = old


def test_a_distribution_documents_scripts_are_decrypted():
    key = bytes(range(3, 19))
    for flags in (1 | DIST, DIST):
        compressed = bool(flags & 1)
        path = make_hwp(Path(tempfile.mkdtemp()) / "d.hwp", [], flags=flags, extra={
            "ViewText/Section0": make_dist_section(hwp_para("body"), key, 7, compressed),
            "Scripts/DefaultJScript": make_dist_section(raw(HEADER, OPEN), key, 9, compressed)})
        ctx = Context()
        doc = reader.parse(path, ctx)
        assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (flags, doc.meta.get("scripts"), ctx.skipped)
    path = make_hwp(Path(tempfile.mkdtemp()) / "d.hwp", [], flags=1 | DIST, extra={
        "ViewText/Section0": make_dist_section(hwp_para("body"), key, 7),
        "Scripts/DefaultJScript": packed(raw(HEADER, OPEN))})  # not encrypted
    ctx = Context()
    doc = reader.parse(path, ctx)
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, ctx.skipped
    # a distribution document saved again as a plain one keeps its encrypted script stream
    doc, ctx = hwp(make_dist_section(raw(HEADER, OPEN), key, 9))
    assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)
    # a header part 28 units long starts as the record's tag does, without its size
    head = "var Documents = XHwpDoc; //x"
    doc, ctx = hwp(raw(head, OPEN), 0)
    assert len(head) == 28 and doc.meta.get("scripts") == [("header", "JScript header", head, "js"), OPENED], ctx.skipped


def test_parts_before_a_cut_are_kept():
    head = ("header", "JScript header", "var n = 1;", "js")
    doc, ctx = hwp(packed(raw("var n = 1;\r\n", OPEN)[:-6]))
    assert doc.meta.get("scripts") == [head] and ctx.skipped == {"script": 1}, (doc.meta.get("scripts"), ctx.skipped)
    long = packed(raw("var n = 1;\r\n", "".join(f"x{i:x};" for i in range(20000))))
    doc, ctx = hwp(long[:len(long) // 2])  # the deflate stream cut in the source
    assert doc.meta.get("scripts") == [head] and ctx.skipped == {"script": 1}, (doc.meta.get("scripts"), ctx.skipped)
    doc, ctx = hwp(packed(raw(HEADER, OPEN)[:-2]))  # two bytes where the -1 should be
    assert doc.meta.get("scripts") == [OPENED] and ctx.skipped == {"script": 1}, (doc.meta.get("scripts"), ctx.skipped)
    c = zlib.compressobj(wbits=-15)
    open_end = c.compress(raw(HEADER, OPEN)) + c.flush(zlib.Z_SYNC_FLUSH)  # every part inflates, the end marker not
    doc, ctx = hwp(open_end)
    assert doc.meta.get("scripts") == [OPENED] and ctx.skipped == {"script": 1}, (doc.meta.get("scripts"), ctx.skipped)


def hwpx(members, items=('headersc', 'sourcesc')):
    hrefs = {"headersc": "Scripts/headerScripts.js", "sourcesc": "Scripts/sourceScripts.js"}
    listed = "".join(f'<opf:item id="{i}" href="{hrefs[i]}" media-type="application/x-javascript ;charset=utf-16"/>'
                     for i in items)
    path = in_hwpx.hwpx_doc(in_hwpx.P("body"), hpf_items=listed, extra=members)
    ctx = Context()
    return reader.parse(path, ctx), ctx


def test_hwpx_scripts_are_read():
    for bom in (b"", b"\xff\xfe"):
        doc, ctx = hwpx({"Scripts/headerScripts.js": bom + HEADER.encode("utf-16-le"),
                         "Scripts/sourceScripts.js": bom + OPEN.encode("utf-16-le")})
        assert doc.meta.get("scripts") == [OPENED] and not ctx.skipped, (doc.meta.get("scripts"), ctx.skipped)
    doc, ctx = hwpx({"Scripts/sourceScripts.js": NEW.encode("utf-16-le")}, items=("sourcesc",))
    assert "scripts" not in doc.meta and not ctx.skipped, ctx.skipped
    doc, ctx = hwpx({"Scripts/sourceScripts.js": OPEN.encode("utf-16-le")})  # the header listed, not there
    assert doc.meta.get("scripts") == [OPENED] and ctx.skipped == {"script": 1}, ctx.skipped
    doc, ctx = hwpx({}, items=())
    assert "scripts" not in doc.meta and not ctx.skipped, ctx.skipped
    old = limits.SCRIPT_BYTES
    limits.SCRIPT_BYTES = len(OPEN.encode("utf-16-le"))
    try:
        doc, ctx = hwpx({"Scripts/headerScripts.js": ("var n = 1;" + " " * 200).encode("utf-16-le"),
                         "Scripts/sourceScripts.js": OPEN.encode("utf-16-le")})
    finally:
        limits.SCRIPT_BYTES = old
    assert doc.meta.get("scripts") == [OPENED] and ctx.skipped == {"script": 1}, ctx.skipped


def render(doc, ctx):
    out = Path(tempfile.mkdtemp())
    return markdown.render(doc, out / "assets", False, ctx.skipped), out


def test_out_md_lists_scripts_and_the_source_is_a_file():
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["scripts"] = [OPENED, ("header", "JScript header", "var n = 1;", "js")]
    md, out = render(doc, Context())
    assert md == ("Document scripts (source in assets/scripts/): source.js (JScript source, 4 lines); "
                  "header.js (JScript header, 1 line)\n\nbody\n"), md
    folder = out / "assets" / "scripts"
    assert (folder / "source.js").read_text(encoding="utf-8") == OPENED[2] + "\n"
    assert sorted(p.name for p in folder.iterdir()) == ["header.js", "source.js"]
    ctx = Context()
    ctx.skipped["script"] = 1
    md, _ = render(Doc(blocks=[Para([Text("body")])]), ctx)
    assert md == "(could not read: document scripts)\n\nbody\n", md


def test_the_gate_names_the_scripts_folder():
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "out.md"
        md.write_text("x\n", encoding="utf-8")
        folder = str(Path(tmp) / "assets" / "scripts") + gate.os.sep
        c = gate._context("a.hwp", {"lines": 5, "scripts": 2}, md)
        assert f"2 document scripts in {folder}; Read one to see its source" in c, c
        c = gate._context("a.hwp", {"lines": 5, "scripts": 1}, md)
        assert f"1 document script in {folder}" in c, c
        assert "document script" not in gate._context("a.hwp", {"lines": 5, "scripts": True}, md)


def test_convert_writes_the_script_count():
    path = make_hwp(Path(tempfile.mkdtemp()) / "a.hwp", [hwp_para("body")],
                    extra={"Scripts/DefaultJScript": packed(raw(HEADER, OPEN, "", ""))})
    with tempfile.TemporaryDirectory() as tmp:
        reader.convert(path, Path(tmp))
        assert json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8"))["scripts"] == 1
        assert (Path(tmp) / "assets" / "scripts" / "source.js").is_file(), list(Path(tmp).rglob("*"))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")


if __name__ == "__main__":
    main()
