"""Tests for OpenDocument macros: Basic libraries (Basic/), scripts (Scripts/) and dialogs (Dialogs/), read into the
document's scripts and kept out of the main text.

Run: python -I -X utf8 tests/test_reader_odf_macros.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import gate, limits, markdown  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
import test_reader_embedded as emb  # noqa: E402
import test_reader_docm as in_docm  # noqa: E402
import test_reader_ods_odp as in_ods  # noqa: E402
import test_reader_odt as odt  # noqa: E402

DTD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
       '<!DOCTYPE {0} PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "{1}">\n')
LIB_NS = 'xmlns:library="http://openoffice.org/2000/library"'
SCRIPT_NS = 'xmlns:script="http://openoffice.org/2000/script"'
MAN_NS = 'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
SHELL = 'Sub Main\r\n  Shell(&quot;calc.exe&quot;)\r\nEnd Sub\r\n\r\n      '
SHOWN = 'Sub Main\n  Shell("calc.exe")\nEnd Sub'
NEW = "REM  *****  BASIC  *****\n\nSub Main\n\nEnd Sub\n"


def lc(*libs):
    """Basic/script-lc.xml listing `libs`, each a name or (name, link)."""
    rows = "".join(f'<library:library library:name="{n}" library:link="{link}"/>'
                   for n, link in ((x, "false") if isinstance(x, str) else x for x in libs))
    return DTD.format("library:libraries", "libraries.dtd") + f'<library:libraries {LIB_NS}>{rows}</library:libraries>'


def lb(name, *modules, protected=False):
    rows = "".join(f'<library:element library:name="{m}"/>' for m in modules)
    return (DTD.format("library:library", "library.dtd") + f'<library:library {LIB_NS} library:name="{name}" '
            f'library:readonly="false" library:passwordprotected="{str(protected).lower()}">{rows}</library:library>')


def module(name, text, kind=None):
    t = f' script:moduleType="{kind}"' if kind else ""
    return (DTD.format("script:module", "module.dtd") + f'<script:module {SCRIPT_NS} script:name="{name}" '
            f'script:language="StarBasic"{t}>{text}</script:module>')


def basic(**libs):
    """Members of a document's Basic container: each library `name=[(module, text, kind?)]`."""
    out = {"Basic/script-lc.xml": lc(*libs)}
    for lib, mods in libs.items():
        out[f"Basic/{lib}/script-lb.xml"] = lb(lib, *(m[0] for m in mods))
        out.update({f"Basic/{lib}/{m[0]}.xml": module(*m) for m in mods})
    return out


def manifest(*encrypted):
    rows = "".join(f'<manifest:file-entry manifest:full-path="{p}"><manifest:encryption-data/></manifest:file-entry>'
                   for p in encrypted)
    return f'<manifest:manifest {MAN_NS}>{rows}</manifest:manifest>'


def scripts_of(path):
    ctx = Context()
    doc = reader.parse(Path(path), ctx)
    return doc.meta.get("scripts", []), ctx


def test_basic_modules_of_a_text_document_are_read():
    p = odt.odt_doc(odt.P("body"), extra=basic(Standard=[("test", SHELL), ("blank", " \r\n ")],
                                               Tools=[("Calc", "\r\n \r\nFunction F()\nF = 1\nEnd Function", "class"),
                                                      ("Module1", NEW.replace("\n", "\r\n"))]))
    got, ctx = scripts_of(p)
    assert got == [("test", "Basic module, library Standard", SHOWN, "bas"),
                   ("Calc", "Basic class module, library Tools", "Function F()\nF = 1\nEnd Function", "bas")], got
    assert not ctx.skipped, ctx.skipped
    assert "Sub Main" not in odt.md(p), odt.md(p)


def test_spreadsheets_and_presentations_read_theirs():
    extra = basic(Standard=[("test1", SHELL)])
    for p in (in_ods.ods(in_ods.sheet("S", [in_ods.row(in_ods.c("x"))]), extra=extra),
              in_ods.odp(in_ods.page("p1", in_ods.frame("x")), extra=extra)):
        got, ctx = scripts_of(p)
        assert got == [("test1", "Basic module, library Standard", SHOWN, "bas")] and not ctx.skipped, (got, ctx)


def test_a_module_or_library_that_cannot_be_read_is_named():
    entity = module("test", "x", "normal").replace("<script:module", "<!ENTITY a 'b'><script:module")
    cases = (("Basic/Standard/test.xml", None, ["more"]),
             ("Basic/Standard/test.xml", "<script:module", ["more"]),
             ("Basic/Standard/test.xml", entity, ["more"]),
             ("Basic/Standard/script-lb.xml", None, []),
             ("Basic/Standard/script-lb.xml", "<library:library", []),
             ("Basic/script-lc.xml", None, []),
             ("Basic/script-lc.xml", "<library:libraries", []),
             ("Basic/script-lc.xml", lc("Standard", "Gone"), ["test", "more"]))
    for member, value, kept in cases:
        members = basic(Standard=[("test", SHELL), ("more", "Sub B\nEnd Sub")])
        if value is None:
            del members[member]
        else:
            members[member] = value
        got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
        assert [g[0] for g in got] == kept and dict(ctx.skipped) == {"script": 1}, (member, got, ctx.skipped)


def test_a_module_s_xml_declaration_gives_its_encoding():
    members = basic(Standard=[("test", SHELL)])
    members["Basic/Standard/test.xml"] = module("test", "Sub Caf\u00e9\nEnd Sub").replace(
        'encoding="UTF-8"', 'encoding="windows-1252"').encode("cp1252")
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert got == [("test", "Basic module, library Standard", "Sub Caf\u00e9\nEnd Sub", "bas")], (got, ctx.skipped)


def test_password_protected_and_linked_libraries_are_named_and_the_document_still_reads():
    members = basic(Standard=[("test", SHELL)], Secret=[("hidden", "x")])
    members["Basic/Secret/script-lb.xml"] = lb("Secret", "hidden", protected=True)
    members["META-INF/manifest.xml"] = manifest("Basic/Secret/hidden.xml")
    members["Basic/script-lc.xml"] = lc("Standard", "Secret", ("Shared", "true"))
    p = odt.odt_doc(odt.P("body"), extra=members)
    got, ctx = scripts_of(p)
    assert [g[0] for g in got] == ["test"], got
    assert dict(ctx.skipped) == {"script_locked": 1, "linked_basic": 1}, ctx.skipped
    assert "body" in odt.md(p)
    members["Basic/Secret/script-lb.xml"] = lb("Secret", "hidden")
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert [g[0] for g in got] == ["test"] and ctx.skipped["script_locked"] == 1, (got, ctx.skipped)
    members["Basic/Secret/script-lb.xml"] = lb("Secret", "hidden", protected=True)
    del members["META-INF/manifest.xml"]
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert [g[0] for g in got] == ["test"] and ctx.skipped["script_locked"] == 1, (got, ctx.skipped)
    for man in (f'<manifest:manifest {MAN_NS}><manifest:encryption-data/>',
                f'<manifest:manifest {MAN_NS}><manifest:encryption-data/></manifest:manifest>'):
        members["META-INF/manifest.xml"] = man
        try:
            scripts_of(odt.odt_doc(odt.P("body"), extra=members))
            raise AssertionError(man)
        except ReadError as e:
            assert e.kind == "encrypted", (man, e)
    for locked in ("content.xml", "Basic/../content.xml", "Pictures/a.png", ""):
        members["META-INF/manifest.xml"] = manifest("Basic/Secret/hidden.xml", locked)
        try:
            scripts_of(odt.odt_doc(odt.P("body"), extra=members))
            raise AssertionError(locked)
        except ReadError as e:
            assert e.kind == "encrypted", (locked, e)


def test_scripts_of_other_languages_are_read_and_other_files_named():
    members = {"Scripts/python/tools.py": "def go(*a):\r\n    pass\r\n",
               "Scripts/python/pkg/deep.py": "﻿X = 1\n",
               "Scripts/javascript/Lib1/hello.js": "print('hi');\n",
               "Scripts/javascript/Lib1/parcel-descriptor.xml": "<parcel/>",
               "Scripts/beanshell/Lib2/run.bsh": "return 0;\n"}
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert sorted(got) == [("deep", "Python script, in pkg", "X = 1", "py"),
                           ("hello", "JavaScript script, in Lib1", "print('hi');", "js"),
                           ("run", "BeanShell script, in Lib2", "return 0;", "bsh"),
                           ("tools", "Python script", "def go(*a):\n    pass", "py")], got
    assert not ctx.skipped, ctx.skipped
    for other in ("Scripts/java/Lib3/Run.class", "Scripts/python/notes.txt"):
        _, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra={**members, other: b"\xca\xfe\xba\xbe"}))
        assert dict(ctx.skipped) == {"script": 1}, (other, ctx.skipped)


def test_dialogs_are_counted():
    members = {"Dialogs/dialog-lc.xml": "<x/>", "Dialogs/Standard/dialog-lb.xml": "<x/>",
               "Dialogs/Standard/": "", "Dialogs/Standard/Dialog1.xml": "<x/>",
               "Dialogs/Standard/Dialog2.xml": "<x/>"}
    _, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert dict(ctx.skipped) == {"dialog": 2}, ctx.skipped


def test_limits_apply_to_the_document_s_scripts_together():
    big = os.urandom(limits.SCRIPT_BYTES // 4).hex()
    members = basic(Standard=[("a", big), ("b", big), ("c", "Sub C\nEnd Sub")])
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=members))
    assert [g[0] for g in got] == ["a", "c"] and ctx.skipped["script"] == 1, ([g[0] for g in got], ctx.skipped)
    many = [(f"m{i}", f"Sub M{i}\nEnd Sub") for i in range(limits.SCRIPT_FILES + 2)]
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=basic(Standard=many)))
    assert len(got) == limits.SCRIPT_FILES and ctx.skipped["script"] == 2, (len(got), ctx.skipped)
    files = {f"Scripts/python/m{i}.py": f"X = {i}" for i in range(limits.SCRIPT_FILES + 1)}
    got, ctx = scripts_of(odt.odt_doc(odt.P("body"), extra=files))
    assert len(got) == limits.SCRIPT_FILES and ctx.skipped["script"] == 1, (len(got), ctx.skipped)


def test_an_embedded_document_s_macros_are_the_host_s():
    inner = Path(odt.odt_doc(odt.P("INNER ODT"), extra=basic(Standard=[("test", SHELL)]))).read_bytes()
    p = emb.docx([("rO", emb.PKG_REL, "embeddings/t.odt", False)], {"word/embeddings/t.odt": inner})
    got, ctx = scripts_of(p)
    assert got == [("test", "Basic module, library Standard, in an embedded OpenDocument text", SHOWN, "bas")], got
    assert "INNER ODT" in in_docm.md(p)
    book = Path(in_ods.ods(in_ods.sheet("S", [in_ods.row(in_ods.c("cell"))]),
                           extra=basic(Standard=[("test1", SHELL)]))).read_bytes()
    got, ctx = emb.read(book)
    assert got[0] == "table", got
    assert ctx.scripts == [("test1", "Basic module, library Standard, in an embedded OpenDocument spreadsheet",
                            SHOWN, "bas")], ctx.scripts


def test_a_workbook_with_no_table_still_gives_its_lost_scripts():
    from reader import embedded
    members = basic(Standard=[("test1", SHELL)])
    del members["Basic/Standard/test1.xml"]
    got, ctx = emb.read(Path(in_ods.ods("", extra=members)).read_bytes())
    assert got == embedded.CHART and dict(ctx.skipped) == {"script": 1}, (got, ctx.skipped)


def test_embedded_documents_share_the_host_s_room_for_scripts():
    from reader import embedded
    from reader.formats import sheet_grid
    two = basic(Standard=[("a", "Sub A\nEnd Sub"), ("b", "Sub B\nEnd Sub")])
    for blob in (Path(odt.odt_doc(odt.P("INNER"), extra=two)).read_bytes(),
                 Path(in_ods.ods(in_ods.sheet("S", [in_ods.row(in_ods.c("cell"))]), extra=two)).read_bytes()):
        ctx = Context()
        ctx.script_files, ctx.script_bytes = limits.SCRIPT_FILES - 1, 7
        embedded.read(blob, ctx, sheet_grid.Budget())
        assert [s[0] for s in ctx.scripts] == ["a"] and ctx.skipped["script"] == 1, (ctx.scripts, ctx.skipped)
        assert ctx.script_files == limits.SCRIPT_FILES and ctx.script_bytes > 7, ctx.script_files


def render(doc, ctx):
    out = Path(tempfile.mkdtemp())
    return markdown.render(doc, out / "assets", False, ctx.skipped), out


def test_out_md_lists_the_scripts_and_names_what_was_not_read():
    from reader.model import Doc, Para, Text
    doc = Doc(blocks=[Para([Text("body")])])
    doc.meta["scripts"] = [("test", "Basic module, library Standard", SHOWN, "bas"),
                           ("test", "Python script", "pass", "py"), ("Test", "Basic module, library Two", "x", "bas")]
    md, out = render(doc, Context())
    assert md == ("Document scripts (source in assets/scripts/): test.bas (Basic module, library Standard, 3 lines); "
                  'test.py (Python script, 1 line); Test-2.bas ("Test", Basic module, library Two, 1 line)'
                  "\n\nbody\n"), md
    assert (out / "assets" / "scripts" / "test.bas").read_text(encoding="utf-8") == SHOWN + "\n"
    ctx = Context()
    ctx.skipped.update({"script": 1, "script_locked": 1, "linked_basic": 1, "dialog": 2})
    md, _ = render(Doc(blocks=[Para([Text("body")])]), ctx)
    assert md == ("(could not read: 2 Basic dialogs, document scripts, password-protected Basic libraries, "
                  "linked Basic libraries (in other files))\n\nbody\n"), md


def test_the_gate_and_meta_count_the_scripts():
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "out.md"
        md.write_text("x\n", encoding="utf-8")
        folder = str(Path(tmp) / "assets" / "scripts") + gate.os.sep
        c = gate._context("a.odt", {"lines": 5, "scripts": 2}, md)
        assert f"2 document scripts in {folder}; Read one to see its source" in c, c
    p = odt.odt_doc(odt.P("body"), extra=basic(Standard=[("test", SHELL)]))
    with tempfile.TemporaryDirectory() as tmp:
        reader.convert(p, Path(tmp))
        assert json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8"))["scripts"] == 1
        assert (Path(tmp) / "assets" / "scripts" / "test.bas").read_text(encoding="utf-8") == SHOWN + "\n"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")


if __name__ == "__main__":
    main()
