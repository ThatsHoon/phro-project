"""Tests for reader.containers.vba: VBA project source, read and kept out of the main text."""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import gate, limits, markdown  # noqa: E402
from reader.containers import vba  # noqa: E402  (ModuleNotFoundError before vba.py exists)
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import ooxml  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.model import Context, Doc, Para, Text  # noqa: E402
from reader_fix import cfb_bytes  # noqa: E402
import test_reader_doc as in_doc  # noqa: E402
import test_reader_docm as in_docm  # noqa: E402
import test_reader_pub as in_pub  # noqa: E402
import test_reader_vsd as in_vsd  # noqa: E402
import test_reader_xls as in_xls  # noqa: E402

# [MS-OVBA] 3.2: the specification's three examples
PLAIN = bytes.fromhex("01 19 B0 00 61 62 63 64 65 66 67 68 00 69 6A 6B 6C 6D 6E 6F 70 00 71 72 73 74 75 76 2E")
NORMAL = bytes.fromhex("01 2F B0 00 23 61 61 61 62 63 64 65 82 66 00 70 61 67 68 69 6A 01 38 08 61 6B 6C 00 30 6D"
                       " 6E 6F 70 06 71 02 70 04 10 72 73 74 75 76 10 77 78 79 7A 00 3C")
MAXIMUM = bytes.fromhex("01 03 B0 02 61 45 00")


def container(data: bytes) -> bytes:
    """A compressed container of literal tokens only, 3000 bytes a chunk."""
    out = bytearray(b"\x01")
    for at in range(0, max(len(data), 1), 3000):
        piece = data[at:at + 3000]
        body = b"".join(b"\x00" + piece[k:k + 8] for k in range(0, len(piece), 8))
        out += struct.pack("<H", 0xB000 | len(body) + 2 - 3) + body
    return bytes(out)


def rec(rid, data=b""):
    return struct.pack("<HI", rid, len(data)) + data


def dir_stream(mods, codepage=949):
    """A dir stream: the project's code page and version, then one module record run for each (name, stream name,
    offset, type id)."""
    out = rec(1, struct.pack("<I", 1)) + rec(3, struct.pack("<H", codepage))
    out += struct.pack("<HIIH", 9, 4, 1, 2) + rec(0x0F, struct.pack("<H", len(mods))) + rec(0x13, b"\xff\xff")
    for name, stream, offset, kind in mods:
        codec = f"cp{codepage}"
        out += rec(0x19, name.encode(codec, "replace")) + rec(0x47, name.encode("utf-16-le"))
        out += rec(0x1A, stream.encode(codec, "replace")) + rec(0x32, stream.encode("utf-16-le"))
        out += rec(0x31, struct.pack("<I", offset)) + rec(kind) + rec(0x2B)
    return container(out + rec(0x10))


def project(sources, prefix="", codepage=949):
    """The streams of a project: {name: (source, type id)}; each module's source starts 7 bytes into its stream."""
    streams = {prefix + "VBA/dir": dir_stream([(n, n, 7, k) for n, (_, k) in sources.items()], codepage)}
    for n, (src, _) in sources.items():
        streams[prefix + "VBA/" + n] = b"compild" + container(src.encode(f"cp{codepage}"))
    return streams


def read(streams, ctx=None, prefix=""):
    ctx = ctx or Context()
    with Compound(cfb_bytes(streams)[0]) as cf:
        return vba.modules(cf, prefix, ctx), ctx


SUB = 'Attribute VB_Name = "Module1"\r\nSub Hello()\r\n    MsgBox "안녕"\r\nEnd Sub\r\n'
EMPTY = 'Attribute VB_Name = "ThisDocument"\r\nAttribute VB_Base = "x"\r\n'


def test_decompress_the_specifications_examples():
    assert vba.decompress(PLAIN, 100) == b"abcdefghijklmnopqrstuv."
    assert vba.decompress(NORMAL, 100) == b"#aaabcdefaaaaghijaaaaaklaaamnopqaaaaaaaaaaaarstuvwxyzaaa"
    assert vba.decompress(MAXIMUM, 100) == b"a" * 73
    big = bytes(range(256)) * 40
    assert vba.decompress(container(big), len(big)) == big


def test_decompress_refuses_what_is_no_container():
    for bad in (b"", b"\x00" + PLAIN[1:], b"\x01\x03\x90\x02aE\x00", b"\x01\x02\xb0\x01\x05\x00"):
        try:
            vba.decompress(bad, 100)
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        vba.decompress(MAXIMUM, 72)
    except ValueError:
        return
    raise AssertionError("past the cap")


def test_modules_are_listed_with_their_source():
    got, ctx = read(project({"Module1": (SUB, 0x21), "ThisDocument": (EMPTY, 0x22),
                             "매크로": ("Attribute VB_Name = \"x\"\r\nPublic n As Long\r\n", 0x22)}))
    assert got == [("Module1", "module", 'Sub Hello()\n    MsgBox "안녕"\nEnd Sub'),
                   ("매크로", "class or document", "Public n As Long")], got
    assert not ctx.skipped, ctx.skipped


def test_attribute_lines_keep_only_a_description():
    src = ('Attribute VB_Name = "M"\r\nSub A()\r\nAttribute A.VB_Description = "what it does"\r\n'
           'Attribute A.VB_ProcData.VB_Invoke_Func = "P.M.A"\r\nEnd Sub\r\n')
    got, _ = read(project({"M": (src, 0x21)}))
    assert got[0][2] == 'Sub A()\nAttribute A.VB_Description = "what it does"\nEnd Sub', got


def test_project_is_found_under_any_storage():
    for prefix in ("Macros/", "_VBA_PROJECT_CUR/", ""):
        got, _ = read(project({"M": (SUB, 0x21)}, prefix), prefix=prefix)
        assert [m[0] for m in got] == ["M"], (prefix, got)
    got, ctx = read({"Other/stream": b"x"})
    assert got is None and not ctx.skipped, (got, ctx.skipped)


def test_a_documents_own_project_is_the_one_read():
    decoy = project({"Decoy": (SUB, 0x21)}, "MBD00000001/Macros/")
    _, ctx = in_xls.book([{"name": "S", "cells": {(0, 0): "x"}}],
                         streams={**decoy, **project({"Real": (SUB, 0x21)}, "_VBA_PROJECT_CUR/")})
    assert [m[0] for m in ctx.vba] == ["Real"] and not ctx.skipped, (ctx.vba, ctx.skipped)
    _, ctx = in_xls.book([{"name": "S", "cells": {(0, 0): "x"}}], streams=decoy)
    assert not ctx.vba and not ctx.skipped, (ctx.vba, ctx.skipped)
    decoy = project({"Decoy": (SUB, 0x21)}, "ObjectPool/_1/_VBA_PROJECT_CUR/")
    p = in_doc.doc_file([("x\r", False)], streams={**decoy, **project({"Real": (SUB, 0x21)}, "Macros/")})
    _, ctx = in_doc.parse(p)
    assert [m[0] for m in ctx.vba] == ["Real"] and not ctx.skipped, (ctx.vba, ctx.skipped)


def test_vsd_and_pub_projects_are_read():
    doc, ctx = in_vsd.read(in_vsd.drawing([{"shapes": [{"text": "t"}]}]),
                           project({"ThisDocument": (SUB, 0x22)}, "_VBA_PROJECT_CUR/"))
    assert [m[:2] for m in ctx.vba] == [("ThisDocument", "class or document")] and not ctx.skipped, ctx.vba
    for prefix in ("", "Macros/"):
        doc, ctx = in_pub.read_pub(in_pub.one_page(["t"], streams=project({"M": (SUB, 0x21)}, prefix)))
        assert [m[0] for m in ctx.vba] == ["M"] and not ctx.skipped, (prefix, ctx.vba, ctx.skipped)


def test_a_dir_that_names_modules_and_gives_none_is_named():
    cut = rec(1, struct.pack("<I", 1)) + rec(3, struct.pack("<H", 949)) + rec(0x0F, struct.pack("<H", 2))
    got, ctx = read({"VBA/dir": container(cut)})
    assert got == [] and ctx.skipped == {"vba": 1}, (got, ctx.skipped)
    open_ = cut + rec(0x19, b"A") + rec(0x1A, b"A") + rec(0x31, struct.pack("<I", 7)) + rec(0x21)
    got, ctx = read({"VBA/dir": container(open_), "VBA/A": b"compild" + container(SUB.encode("cp949"))})
    assert got == [] and ctx.skipped == {"vba_module": 2}, (got, ctx.skipped)
    twice = open_ + rec(0x19, b"B") + rec(0x1A, b"A") + rec(0x31, struct.pack("<I", 7)) + rec(0x2B)
    got, ctx = read({"VBA/dir": container(twice), "VBA/A": b"compild" + container(SUB.encode("cp949"))})
    assert [m[0] for m in got] == ["B"] and ctx.skipped == {"vba_module": 1}, (got, ctx.skipped)


def test_a_mac_code_page_decodes():
    src = 'Attribute VB_Name = "M"\r\nx = "\u00e9"\r\n'.encode("mac_roman")
    streams = {"VBA/M": container(src)}
    out = rec(1, struct.pack("<I", 1)) + rec(3, struct.pack("<H", 10000))
    out += rec(0x19, b"M") + rec(0x1A, b"M") + rec(0x31, struct.pack("<I", 0)) + rec(0x21) + rec(0x2B)
    streams["VBA/dir"] = container(out)
    got, _ = read(streams)
    assert got == [("M", "module", 'x = "\u00e9"')], got


def test_what_cannot_be_read_is_counted():
    got, ctx = read({"VBA/dir": b"\x01" * 16})
    assert got == [] and ctx.skipped == {"vba": 1}, (got, ctx.skipped)
    streams = project({"A": (SUB, 0x21), "B": (SUB, 0x21)})
    streams["VBA/B"] = b"compild" + b"\x00garbage"
    got, ctx = read(streams)
    assert [m[0] for m in got] == ["A"] and ctx.skipped == {"vba_module": 1}, (got, ctx.skipped)
    del streams["VBA/B"]
    got, ctx = read(streams)
    assert [m[0] for m in got] == ["A"] and ctx.skipped == {"vba_module": 1}, (got, ctx.skipped)


def test_modules_and_bytes_are_bounded():
    streams = project({"A": (SUB, 0x21), "B": (SUB, 0x21), "C": (SUB, 0x21)})
    old = limits.VBA_MODULES, limits.VBA_BYTES
    try:
        limits.VBA_MODULES = 2
        got, ctx = read(streams)
        assert [m[0] for m in got] == ["A", "B"] and ctx.skipped == {"vba_module": 1}, (got, ctx.skipped)
        limits.VBA_MODULES, limits.VBA_BYTES = old[0], len(streams["VBA/dir"]) + len(streams["VBA/A"]) - 7 + 10
        got, ctx = read(streams)
        assert [m[0] for m in got] == ["A"] and ctx.skipped == {"vba_module": 2}, (got, ctx.skipped)
    finally:
        limits.VBA_MODULES, limits.VBA_BYTES = old


def test_a_module_that_does_not_fit_spends_the_budget():
    streams = project({n: ("x = 1\r\n" * 60, 0x21) for n in "ABCDEF"})
    size = len(vba.decompress(streams["VBA/dir"], 10**6))
    old, real, calls = limits.VBA_BYTES, vba.decompress, []
    limits.VBA_BYTES = size + 100
    vba.decompress = lambda data, cap: (calls.append(cap), real(data, cap))[1]
    try:
        got, ctx = read(streams)
        assert got == [] and ctx.skipped == {"vba_module": 6} and len(calls) == 2, (got, ctx.skipped, calls)
        assert ctx.vba_bytes >= limits.VBA_BYTES, ctx.vba_bytes
        calls.clear()
        blob, _ = cfb_bytes(streams)
        doc, ctx2 = in_docm.parse(in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": blob}))
        ctx2.vba_bytes = limits.VBA_BYTES
        from reader.containers.zipx import Package as Pkg
        with Pkg(in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": blob})) as pkg:
            calls.clear()
            assert vba.package(pkg, ctx2) == [] and not calls and ctx2.skipped["vba"] == 1, (calls, ctx2.skipped)
    finally:
        limits.VBA_BYTES, vba.decompress = old, real


def test_a_stream_that_gives_nothing_is_charged_what_was_scanned():
    names = [f"M{k}" for k in range(40)]
    streams = {"VBA/dir": dir_stream([(n, "S", 0, 0x21) for n in names]),
               "VBA/S": b"\x01" + b"\x00\xb0\x00" * 20000}
    old, real, calls = limits.VBA_BYTES, vba.decompress, []
    limits.VBA_BYTES = 150_000
    vba.decompress = lambda data, cap: (calls.append(len(data)), real(data, cap))[1]
    try:
        got, ctx = read(streams)
    finally:
        limits.VBA_BYTES, vba.decompress = old, real
    assert got == [] and len(calls) <= 4 and ctx.skipped == {"vba_module": 40}, (got, calls, ctx.skipped)


def test_modules_a_cut_dir_announced_are_counted():
    cut = rec(1, struct.pack("<I", 1)) + rec(3, struct.pack("<H", 949)) + rec(0x0F, struct.pack("<H", 5))
    for n in (b"A", b"B"):
        cut += rec(0x19, n) + rec(0x1A, n) + rec(0x31, struct.pack("<I", 7)) + rec(0x21) + rec(0x2B)
    source = b"compild" + container(SUB.encode("cp949"))
    got, ctx = read({"VBA/dir": container(cut), "VBA/A": source, "VBA/B": source})
    assert [m[0] for m in got] == ["A", "B"] and ctx.skipped == {"vba_module": 3}, (got, ctx.skipped)


def test_the_module_count_is_the_documents():
    one, _ = cfb_bytes(project({"A": (SUB, 0x21), "B": (SUB, 0x21)}))
    two, _ = cfb_bytes(project({"C": (SUB, 0x21), "D": (SUB, 0x21)}))
    old = limits.VBA_MODULES
    limits.VBA_MODULES = 3
    try:
        doc, ctx = in_docm.parse(in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": one,
                                                                    "x/vbaProject.bin": two}))
    finally:
        limits.VBA_MODULES = old
    assert [m[0] for m in doc.meta["vba"]] == ["A", "B", "C"] and ctx.skipped == {"vba_module": 1}, ctx.skipped


def render(doc, ctx):
    out = Path(tempfile.mkdtemp())
    return markdown.render(doc, out / "assets", False, ctx.skipped), out


def test_out_md_lists_modules_and_the_source_is_a_file():
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["vba"] = [("Module1", "module", "Sub A()\nEnd Sub"), ("Sheet1", "class or document", "x = 1")]
    md, out = render(doc, Context())
    assert md == ("VBA modules (source in assets/vba/): Module1.bas (module, 2 lines); "
                  "Sheet1.bas (class or document, 1 line)\n\nbody\n"), md
    assert (out / "assets" / "vba" / "Module1.bas").read_text(encoding="utf-8") == "Sub A()\nEnd Sub\n"
    assert sorted(p.name for p in (out / "assets" / "vba").iterdir()) == ["Module1.bas", "Sheet1.bas"]


def test_hostile_module_names_stay_inside_the_folder():
    names = ["../../evil", "..\\up", "CON", "a/b:c*?\x00", "", "con", "x" * 300, "매크로 1"]
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["vba"] = [(n, "module", "x = 1") for n in names]
    md, out = render(doc, Context())
    folder = out / "assets" / "vba"
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == len(names) and all(p.parent == folder and p.is_file() for p in folder.iterdir()), files
    assert not [p for p in out.rglob("*") if p.is_file() and p.parent != folder], list(out.rglob("*"))
    assert "매크로_1.bas" in files and "_CON.bas" in files and "_con-2.bas" in files, files
    assert all(len(f) <= 72 and "/" not in f and "\\" not in f and ".." not in f for f in files), files
    assert '매크로_1.bas ("매크로 1", module, 1 line)' in md and '_CON.bas ("CON", module, 1 line)' in md, md
    assert "module.bas (module, 1 line)" in md, md


def test_a_module_name_in_the_line_is_quoted():
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["vba"] = [('a b); x.bas ("y", module', "module", "x = 1")]
    md, out = render(doc, Context())
    assert '.bas ("a b); x.bas (\\"y\\", module", module, 1 line)\n' in md, md


def test_superscript_device_names_are_reserved():
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["vba"] = [("COM\u00b9", "module", "x = 1"), ("lpt\u00b3", "module", "x = 1")]
    md, out = render(doc, Context())
    assert sorted(p.name for p in (out / "assets" / "vba").iterdir()) == ["_COM\u00b9.bas", "_lpt\u00b3.bas"], md


def test_a_long_module_list_is_cut_in_the_line():
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["vba"] = [(f"M{k}", "module", "x = 1") for k in range(30)]
    md, out = render(doc, Context())
    assert len(list((out / "assets" / "vba").iterdir())) == 30
    assert "M19.bas (module, 1 line); and 10 more\n" in md and "M20.bas" not in md, md


def test_the_gate_names_the_vba_folder():
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "out.md"
        md.write_text("x\n", encoding="utf-8")
        folder = str(Path(tmp) / "assets" / "vba") + gate.os.sep
        c = gate._context("a.docm", {"lines": 5, "vba": 2}, md)
        assert f"2 VBA modules in {folder}; Read one to see its source" in c, c
        c = gate._context("a.docm", {"lines": 5, "vba": 1, "images": 1}, md)
        assert f"1 VBA module in {folder}" in c and "1 image in" in c, c
        assert "VBA" not in gate._context("a.docm", {"lines": 5, "vba": "2"}, md)


def test_convert_writes_the_module_count():
    import json
    import reader
    blob, _ = cfb_bytes(project({"NewMacros": (SUB, 0x21)}))
    src = in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": blob})
    with tempfile.TemporaryDirectory() as tmp:
        reader.convert(src, Path(tmp))
        assert json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8"))["vba"] == 1
        reader.convert(in_docm.docm(in_docm.P("Hi")), Path(tmp))
        assert "vba" not in json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8"))


def test_docm_project_is_read():
    blob, _ = cfb_bytes(project({"NewMacros": (SUB, 0x21), "ThisDocument": (EMPTY, 0x22)}))
    doc, ctx = in_docm.parse(in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": blob}))
    assert doc.meta["vba"] == [("NewMacros", "module", 'Sub Hello()\n    MsgBox "안녕"\nEnd Sub')], doc.meta
    assert not ctx.skipped, ctx.skipped
    md, _ = render(doc, ctx)
    assert md.startswith("VBA modules (source in assets/vba/): NewMacros.bas (module, 3 lines)\n\n"), md
    doc, ctx = in_docm.parse(in_docm.docm(in_docm.P("Hi"), parts={"word/vbaProject.bin": b"x"}))
    assert "vba" not in doc.meta and ctx.skipped == {"vba": 1}, (doc.meta, ctx.skipped)


def test_xls_project_is_read():
    streams = project({"Module1": (SUB, 0x21)}, "_VBA_PROJECT_CUR/")
    _, ctx = in_xls.book([{"name": "S", "cells": {(0, 0): "x"}}], streams=streams)
    assert [m[:2] for m in ctx.vba] == [("Module1", "module")] and not ctx.skipped, (ctx.vba, ctx.skipped)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")


if __name__ == "__main__":
    main()
