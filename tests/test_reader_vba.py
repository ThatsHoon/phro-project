"""Tests for reader.containers.vba: VBA project source, read and kept out of the main text."""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers import vba  # noqa: E402  (ModuleNotFoundError before vba.py exists)
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import ooxml  # noqa: E402
from reader.containers.zipx import Package  # noqa: E402
from reader.model import Context, Doc, Para, Text  # noqa: E402
from reader_fix import cfb_bytes  # noqa: E402
import test_reader_docm as in_docm  # noqa: E402
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


def read(streams, ctx=None):
    ctx = ctx or Context()
    with Compound(cfb_bytes(streams)[0]) as cf:
        return vba.project(cf, ctx), ctx


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
        got, _ = read(project({"M": (SUB, 0x21)}, prefix))
        assert [m[0] for m in got] == ["M"], (prefix, got)
    got, ctx = read({"Other/stream": b"x"})
    assert got is None and not ctx.skipped, (got, ctx.skipped)


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
        limits.VBA_MODULES, limits.VBA_BYTES = old[0], len(SUB.encode("cp949")) + 10
        got, ctx = read(streams)
        assert [m[0] for m in got] == ["A"] and ctx.skipped == {"vba_module": 2}, (got, ctx.skipped)
    finally:
        limits.VBA_MODULES, limits.VBA_BYTES = old


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
    assert all(f"{f} (module, 1 line)" in md for f in files), md


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
