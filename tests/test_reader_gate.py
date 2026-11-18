"""PreToolUse Read gate: decide(), read.py hook and the hooks.json entry.

Run: python -I -X utf8 tests/test_reader_gate.py
"""
import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import gate  # noqa: E402
from reader.model import Doc, Heading, Para, Text  # noqa: E402

READ = HERE / "scripts" / "read.py"
STUB = "reader.formats._teststub"

# the child process registers the same stub, then runs the real CLI
STUB_READ = '''import sys, types
sys.path.insert(0, {scripts!r})
import reader
from reader.model import Doc, Heading, Para, Text
from pathlib import Path
mod = types.ModuleType({stub!r})
mod.parse = lambda path, ctx: Doc(blocks=[Heading(1, [Text(t[2:])]) if t.startswith("# ") else Para([Text(t)]) for t in Path(path).read_text(encoding="utf-8").lstrip(chr(0)).split("|")])
reader.FORMATS["txtx"] = "_teststub"
reader.BUILT.add("txtx")
sys.modules[{stub!r}] = mod
import read
sys.exit(read.main(sys.argv[1:]))
'''


def tmp():
    return Path(tempfile.mkdtemp())


@contextmanager
def stub():
    """Register txtx in-process, a throwaway cache root and the child script; restore all."""
    old_formats, old_built = dict(reader.FORMATS), set(reader.BUILT)
    old_mod, old_env = sys.modules.get(STUB), os.environ.get("PHRO_READER_CACHE")
    mod = types.ModuleType(STUB)
    mod.parse = lambda path, ctx: Doc(blocks=[
        Heading(1, [Text(t[2:])]) if t.startswith("# ") else Para([Text(t)])
        for t in Path(path).read_text(encoding="utf-8").lstrip(chr(0)).split("|")])
    try:
        reader.FORMATS["txtx"] = "_teststub"
        reader.BUILT.add("txtx")
        sys.modules[STUB] = mod
        os.environ["PHRO_READER_CACHE"] = str(tmp())
        stub_py = tmp() / "stub_read.py"
        stub_py.write_text(STUB_READ.format(scripts=str(HERE / "scripts"), stub=STUB), encoding="utf-8")
        yield [sys.executable, "-I", "-X", "utf8", str(stub_py), "convert"]
    finally:
        reader.FORMATS.clear()
        reader.FORMATS.update(old_formats)
        reader.BUILT.clear()
        reader.BUILT.update(old_built)
        if old_mod is None:
            sys.modules.pop(STUB, None)
        else:
            sys.modules[STUB] = old_mod
        if old_env is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old_env


def ev(path, tool="Read", **extra):
    return {"tool_name": tool, "tool_input": {"file_path": str(path), **extra}}


def sample(text="hello", name="a.txtx"):
    p = tmp() / name
    p.write_bytes(bytes(1) + text.encode("utf-8"))
    return p


def run(p, base, **extra):
    return gate.decide(ev(p, **extra), command=base + [str(p)])


def out(d):
    return d["hookSpecificOutput"]


def test_pass_through():
    d = tmp()
    for n in ("a.py", "a.md", "a.png"):
        (d / n).write_text("x")
        assert gate.decide(ev(d / n)) is None
    (d / "w.hwp").write_text("x")
    assert gate.decide(ev(d / "w.hwp", tool="Write")) is None
    assert gate.decide(ev(d / "missing.hwp")) is None
    (d / "dir.hwp").mkdir()
    assert gate.decide(ev(d / "dir.hwp")) is None
    assert gate.decide({"tool_name": "Read", "tool_input": {}}) is None


def put(name, data):
    p = tmp() / name
    p.write_bytes(data)
    return p


def test_plain_text_passes_through():
    pem = b"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
    for name, data in (("a.pub", b"ssh-ed25519 AAAA user@host\n"), ("tls.key", pem),
                       ("a.doc", "안녕하세요 메모".encode("utf-8")), ("e.doc", b""),
                       ("cut.doc", ("가" * 2000).encode("utf-8")[:4097])):
        assert gate.decide(ev(put(name, data))) is None, name


def test_empty_key_passes_through():
    # a 0-byte .key is plain text to the gate (nothing in it), so it passes through before the Keynote check
    assert gate.decide(ev(put("empty.key", b""))) is None


def test_deny_detail_capped():
    # a detail the file controls (a zip member name of 64 KB) is cut to DETAIL_MAX characters with an ellipsis
    long = "Index/" + "x" * 65_000 + ".iwa: ValueError"
    for kind in ("damaged", "unsupported"):
        why = gate._why(kind, long)
        assert len(why) < gate.DETAIL_MAX + 40 and why.count("x") <= gate.DETAIL_MAX and "…" in why, (kind, len(why))
    assert gate._why("damaged", "internal error " + "y" * 5000).count("y") <= gate.DETAIL_MAX
    assert gate._why("damaged", "short") == "the file looks damaged (short)"
    assert gate.DETAIL_MAX == 200


def test_binary_still_denied():
    # a .vsd of random bytes is not plain text and not built-in-parseable: denied as damaged
    import random
    data = bytes(random.Random(7).randrange(256) for _ in range(400))
    o = out(gate.decide(ev(put("a.vsd", data))))
    assert o["permissionDecision"] == "deny", o
    assert "damaged" in o["permissionDecisionReason"], o


def test_vst_mpt_not_cfb_pass_through():
    # .vst is also a Targa image extension; a .vst or .mpt without the compound-file magic is not ours
    targa = bytes([0, 0, 2]) + bytes(9) + bytes([4, 0, 4, 0, 24, 0]) + bytes(range(256)) * 2
    assert gate.decide(ev(put("a.vst", targa))) is None
    assert gate.decide(ev(put("a.mpt", bytes(range(1, 40)) * 20 + bytes(3)))) is None
    # with the magic the file is ours: a .vst of that head is denied, not passed through
    o = gate.decide(ev(put("b.vst", bytes.fromhex("D0CF11E0A1B11AE1") + bytes(600))))
    assert o is not None and out(o)["permissionDecision"] == "deny", o


def test_djvu_unsupported_detail():
    from djvu_fix import iff, page
    o = out(gate.decide(ev(put("a.djvu", iff(page(None))))))
    assert o["permissionDecision"] == "deny", o
    assert "this file is not supported (no text layer:" in o["permissionDecisionReason"], o


def test_phase4_formats_convert():
    import shutil
    import test_reader_jet as tj
    from djvu_fix import iff, page
    from jet_fix import jet_db
    from reader_fix import cfb_bytes, vsd_document
    import test_reader_pub as tp
    import test_reader_vsdx as tx
    jet = jet_db(1, [tj.orders()])
    ace = jet_db(5, [tj.orders()])
    vsd = cfb_bytes({"VisioDocument": vsd_document(11, [[{"text": "Hello"}]])})[0]
    vsdx = tx.vsdx({"P": tx.shape(1, "Hello")}, mem=True)
    files = {"a.djvu": iff(page("Hello")), "a.djv": iff(page("Hello")), "a.mdb": jet, "a.accdb": ace,
             "a.mde": jet, "a.accde": ace, "a.accdr": ace, "a.pub": tp.one_page(["Hello"]),
             "a.vsd": vsd, "a.vst": vsd, "a.vsdm": vsdx, "a.vstx": vsdx}
    d = tmp()
    try:
        with stub() as base:
            for name, data in files.items():
                (d / name).write_bytes(data)  # clean fixtures
                r = run(d / name, base)
                assert r is not None and out(r)["permissionDecision"] == "allow", (name, r)
                got = Path(out(r)["updatedInput"]["file_path"])
                assert got != d / name, name
                assert ("Orders" if name.rsplit(".", 1)[1] in ("mdb", "accdb", "mde", "accde", "accdr") else "Hello") in got.read_text(encoding="utf-8"), name
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rtf_converts():
    """RTF is not plain text, so the gate converts it, with or without a byte-order mark."""
    with stub():  # only for its throwaway cache root: the real read.py converts
        for name, data in (("a.rtf", b"{\\rtf1\\ansi hi}"), ("b.rtf", b"\xef\xbb\xbf{\\rtf1\\ansi hi}")):
            p = put(name, data)
            o = out(gate.decide(ev(p)))
            assert o["permissionDecision"] == "allow", (name, o)
            text = Path(o["updatedInput"]["file_path"]).read_text(encoding="utf-8")
            assert text.strip() == "hi", (name, text)


def test_relative_path_uses_cwd():
    with stub() as base:
        p = sample()
        e = {"tool_name": "Read", "tool_input": {"file_path": p.name}, "cwd": str(p.parent)}
        o = out(gate.decide(e, command=base + [str(p)]))
        assert o["permissionDecision"] == "allow"
        assert gate.decide({"tool_name": "Read", "tool_input": {"file_path": p.name}}) is None


def test_vanished_entry_denied():
    from reader import cache
    with stub() as base:
        p = sample()
        run(p, base)
        d = cache.lookup(cache.key(p))
        (d / "out.md").unlink()
        old = cache.lookup
        cache.lookup = lambda k: d
        try:
            o = out(gate.decide(ev(p), command=base + [str(p)]))
        finally:
            cache.lookup = old
        assert "cache entry vanished" in o["permissionDecisionReason"]


def test_name_is_flattened():
    assert gate._name(Path("x/a\tb\nc.hwp")) == "a b c.hwp"
    o = gate.deny_text(Path("x/a\tb.hwp"), "why")
    assert o["hookSpecificOutput"]["permissionDecisionReason"].startswith("a b.hwp: why.")


def test_unsupported_phase():
    # an extension in FORMATS but not in BUILT is denied as not supported yet
    old = dict(reader.FORMATS)
    try:
        reader.FORMATS["zzq"] = "zzq"
        p = tmp() / "x.zzq"
        p.write_bytes(b"\xd0\xcf\x11\xe0\x00")
        o = out(gate.decide(ev(p)))
    finally:
        reader.FORMATS.clear()
        reader.FORMATS.update(old)
    assert o["permissionDecision"] == "deny"
    assert o["permissionDecisionReason"] == "x.zzq: this format is not supported yet.", o


def test_retry_by_hand_only_where_it_can_help():
    p = Path("d/a.docx")
    for kind in ("unsupported", "encrypted", "drm", "too_large"):
        why = out(gate._deny(p, kind, "x"))["permissionDecisionReason"]
        assert "Retry by hand" not in why and why.endswith("."), why
    for kind in ("damaged", "timeout"):
        why = out(gate._deny(p, kind, "x"))["permissionDecisionReason"]
        assert "Retry by hand: python -I -X utf8" in why, why
    why = out(gate._deny(p, "damaged", "internal error KeyError"))["permissionDecisionReason"]
    assert why.startswith("a.docx: reading failed (internal error KeyError). Retry by hand:"), why


def test_allow_swaps_path_keeps_keys():
    with stub() as base:
        o = out(run(sample(), base, offset=10, limit=50))
        assert o["permissionDecision"] == "allow" and o["hookEventName"] == "PreToolUse"
        u = o["updatedInput"]
        assert u["offset"] == 10 and u["limit"] == 50 and u["file_path"].endswith("out.md")


def test_context_text_and_length():
    with stub() as base:
        c = out(run(sample(), base))["additionalContext"]
        assert c == "a.txtx was converted to Markdown by phro-project (1 line).", c
        assert len(c) < 600


def test_context_unread_clause_fits():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = {"lines": 1, "unread": ["1 slide", "2 images"]}
    assert gate._context("a.hwpx", meta, md).endswith("(1 line). Some parts could not be read: 1 slide, 2 images.")
    c = gate._context("a.hwpx", {"lines": 2, "images": 1}, md)
    assert c.endswith("(2 lines, 1 image in assets/; Read it to view)."), c
    c = gate._context("a.hwpx", {"lines": 2, "images": 3}, md)
    assert c.endswith("(2 lines, 3 images in assets/; Read them to view)."), c
    meta = {"lines": 3000, "hidden": 4, "warnings": ["garbled"], "unread": [f"{i} kinds of thing" for i in range(60)],
            "headings": [(i, "h") for i in range(100)]}
    c = gate._context("n" * 250 + ".hwpx", meta, md)
    assert len(c) < 600 and "0 kinds of thing" in c and "…." in c and c.endswith("may be garbled."), c


def test_edit_hint_by_family():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    hint = " Read-only view (no XML, cell addresses or formulas); to edit the file, use the {} skill."
    assert gate._context("a.docx", {"lines": 1}, md) == (
        "a.docx was converted to Markdown by phro-project (1 line)." + hint.format("docx"))
    assert gate._context("a.pptm", {"lines": 1}, md).endswith("use the pptx skill.")
    assert gate._context("a.potx", {"lines": 1}, md).endswith("use the pptx skill.")
    assert gate._context("a.xltm", {"lines": 1}, md).endswith("use the xlsx skill.")
    for n in ("a.hwpx", "a.xlsb"):
        assert "Read-only view" not in gate._context(n, {"lines": 1}, md)


def test_edit_hint_fits():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = {"lines": 3000, "hidden": 4, "warnings": ["garbled"], "unread": [f"{i} kinds of thing" for i in range(60)],
            "headings": [(i, "h") for i in range(100)]}
    c = gate._context("n" * 250 + ".xlsm", meta, md)
    assert len(c) < 600 and gate.EDIT_HINT.format(skill="xlsx") in c and "…." in c and c.endswith("may be garbled."), c


def test_edit_hint_before_lists():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = {"lines": 3000, "unread": ["1 image"], "headings": [(1, "h"), (2, "g")]}
    c = gate._context("a.docx", meta, md)
    assert c.index("Read-only view") < c.index("Some parts") < c.index("Sections at"), c


def test_deny_names_the_skill():
    def r(d):
        return d["hookSpecificOutput"]["permissionDecisionReason"]
    t = r(gate._deny(Path("d/a.xlsx"), "damaged", "x"))
    assert "Retry by hand: python -I -X utf8" in t and t.endswith(" or read it with the xlsx skill."), t
    assert r(gate._deny(Path("d/a.docx"), "encrypted")) == (
        "a.docx: the file is encrypted and needs a password. Read it with the docx skill instead.")
    assert r(gate._deny(Path("d/a.hwp"), "encrypted")) == "a.hwp: the file is encrypted and needs a password."
    assert r(gate.deny_text(Path("d/a.pptx"), "reading failed (internal error KeyError)")).endswith(
        " or read it with the pptx skill.")


@contextmanager
def off_env(value):
    old = os.environ.get("PHRO_READER_OFF")
    try:
        if value is None:
            os.environ.pop("PHRO_READER_OFF", None)
        else:
            os.environ["PHRO_READER_OFF"] = value
        yield
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_OFF", None)
        else:
            os.environ["PHRO_READER_OFF"] = old


def test_reader_off_switch():
    d = tmp()
    paths = {}
    for n in ("a.docm", "a.hwp", "a.pptm"):
        paths[n] = d / n
        paths[n].write_bytes(b"PK\x03\x04")

    def ev(n):
        return {"tool_name": "Read", "tool_input": {"file_path": str(paths[n])}}
    with off_env(" .DOCM, hwp ,,"):
        assert gate.scope(ev("a.docm")) is None and gate.scope(ev("a.hwp")) is None
        assert gate.scope(ev("a.pptm")) == paths["a.pptm"]
    with off_env(None):
        assert all(gate.scope(ev(n)) == paths[n] for n in paths)


def test_reader_off_convert_ignores():
    import test_reader_docm as dm
    f = dm.docm(dm.P("Hi"))
    env = dict(os.environ, PHRO_READER_OFF="docm", PHRO_READER_CACHE=str(tmp()))
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(READ), "convert", str(f)],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0 and r.stdout.strip().endswith("out.md"), (r.stdout, r.stderr)


def test_heading_index_long_output():
    with stub() as base:
        parts = ["# H"] + [f"p{i}" for i in range(599)] + ["# H"] + [f"q{i}" for i in range(600)]
        c = out(run(sample("|".join(parts)), base))["additionalContext"]
        assert 'Sections at lines: 1 "H", 1201 "H"' in c, c
        assert len(c) < 600


def test_heading_index_reaches_the_second_level():
    from reader import markdown
    lines = ["# T"] + [f"## H{i}" if i % 62 == 0 and i < 2480 else f"p{i}" for i in range(2500)]
    md = tmp() / "out.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta = markdown.stats(md.read_text(encoding="utf-8"))
    assert len(meta["headings"]) == 41
    c = gate._context("a.docx", meta, md)
    assert ' Sections at lines: 1 "T", 2 "H0", 64 "H62", 126 "H124"' in c, c
    assert len(c) < 600 and c.endswith(", …"), c


def test_heading_index_text_is_quoted_and_clean():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = {"lines": 3000, "headings": [(5, 'say "hi"\nnow\x1b[2J\x85 ‮end “q” '), (9, '""'), (12, "z" * 80)]}
    c = gate._context("a.docx", meta, md)
    assert c.endswith(f' Sections at lines: 5 "say hi now [2J end q", 12 "{"z" * 60}"'), c


def test_heading_index_drops_invisible_marks():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = {"lines": 3000, "headings": [(5, "a‎b‏c؜d​e﻿f‍g")]}
    c = gate._context("a.docx", meta, md)
    assert c.endswith(' Sections at lines: 5 "abcdefg"'), c


def test_child_hashes_the_file_itself():
    # a key handed to the child would file a document saved in between under the old content's key
    with stub():
        p = sample()
        seen = []
        real = gate.subprocess.run

        def fake(cmd, **kw):
            seen.append(cmd)
            raise gate.subprocess.TimeoutExpired(cmd, 1)
        gate.subprocess.run = fake
        try:
            gate.decide(ev(p))
        finally:
            gate.subprocess.run = real
        assert seen and "--key" not in seen[0], seen


def test_heading_index_cut_whole_items():
    with stub() as base:
        parts = [f"# Heading number {i}" if i % 10 == 0 else f"p{i}" for i in range(2500)]
        c = out(run(sample("|".join(parts)), base))["additionalContext"]
        assert len(c) < 600 and c.endswith(", …"), c


def test_cache_hit_no_child():
    with stub() as base:
        p = sample()
        assert out(run(p, base))["permissionDecision"] == "allow"
        d = gate.decide(ev(p), command=["false-binary-that-does-not-exist"])
        assert out(d)["permissionDecision"] == "allow"


def test_gate_wrong_content():
    """A binary-looking file that is not a zip is denied as damaged by the real child."""
    old = os.environ.get("PHRO_READER_CACHE")
    os.environ["PHRO_READER_CACHE"] = str(tmp())
    try:
        o = out(gate.decide(ev(put("bad.hwpx", b"PK\x03\x04" + b"\x00" * 64 + b"not a zip"))))
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old
    assert o["permissionDecision"] == "deny" and "looks damaged" in o["permissionDecisionReason"]


def test_too_large_reason_names_the_limit():
    from reader import limits
    p = Path("x/a.hwpx")
    why = {d: gate._deny(p, "too_large", d)["hookSpecificOutput"]["permissionDecisionReason"]
           for d in ("input: 600000000 bytes", "part: Contents/section0.xml", "total: zip uncompressed size")}
    assert why["input: 600000000 bytes"].startswith("a.hwpx: the file is over 512 MiB"), why
    assert why["part: Contents/section0.xml"].startswith("a.hwpx: a part expands too much to read safely"), why
    assert why["total: zip uncompressed size"].startswith("a.hwpx: the file expands past the 1 GiB safety limit"), why
    assert limits.MAX_INPUT == 512 * 2**20 and limits.ZIP_TOTAL == 2**30


def test_timeout_denies():
    with stub():
        d = gate.decide(ev(sample()), command=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        assert "conversion took over 45 seconds" in out(d)["permissionDecisionReason"]


@contextmanager
def clock(*ticks):
    """gate._now returns these seconds in turn, then the last one again."""
    ticks, old = list(ticks), gate._now
    gate._now = lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]
    try:
        yield
    finally:
        gate._now = old


def test_slow_hash_denies_without_a_child():
    from reader import limits
    with stub(), clock(0.0, limits.HOOK_SECONDS - limits.HOOK_CHILD_MIN + 1):
        # starting this command would raise FileNotFoundError
        d = gate.decide(ev(sample()), command=["phro-no-such-program"])
    o = out(d)
    assert o["permissionDecision"] == "deny", o
    assert "a.txtx: reading the file used up the hook's time limit" in o["permissionDecisionReason"], o


def test_child_gets_what_is_left_of_the_budget():
    import subprocess as sp
    from reader import limits
    seen, old = [], gate.subprocess.run

    def fake(cmd, **kw):
        seen.append(kw["timeout"])
        raise sp.TimeoutExpired(cmd, kw["timeout"])

    gate.subprocess.run = fake
    try:
        with stub(), clock(0.0, 30.0):
            d = gate.decide(ev(sample()), command=["x"])
        assert seen == [limits.HOOK_SECONDS - 30.0], seen
        assert "used up the hook's time limit" in out(d)["permissionDecisionReason"]
        with stub(), clock(0.0, 1.0):
            d = gate.decide(ev(sample()), command=["x"])
        assert seen[1] == limits.CONVERT_SECONDS, seen
        assert f"conversion took over {limits.CONVERT_SECONDS} seconds" in out(d)["permissionDecisionReason"]
    finally:
        gate.subprocess.run = old


def test_child_env_carries_the_deadline():
    import subprocess as sp
    import time
    from reader import limits
    seen, old = [], gate.subprocess.run

    def fake(cmd, **kw):
        seen.append((kw["timeout"], kw.get("env")))
        raise sp.TimeoutExpired(cmd, kw["timeout"])

    gate.subprocess.run = fake
    try:
        for ticks in ((0.0, 30.0), (0.0, 1.0)):
            before = time.time()
            with stub(), clock(*ticks):
                gate.decide(ev(sample()), command=["x"])
                ours = dict(os.environ)
            after = time.time()
            budget, env = seen[-1]
            # a wall-clock epoch, the budget away, so the child's soffice is killed before the gate gives up
            deadline = float(env["PHRO_READER_DEADLINE"])
            assert before + budget <= deadline <= after + budget, (before, deadline, after, budget)
            assert budget == min(limits.CONVERT_SECONDS, limits.HOOK_SECONDS - ticks[1]), budget
            # the rest of the environment goes through unchanged
            rest = {k: v for k, v in env.items() if k != "PHRO_READER_DEADLINE"}
            assert rest == {k: v for k, v in ours.items() if k != "PHRO_READER_DEADLINE"}
    finally:
        gate.subprocess.run = old


def test_child_fail_kind():
    with stub():
        cmd = [sys.executable, "-c", "print('FAIL\\tencrypted\\t')"]
        d = gate.decide(ev(sample()), command=cmd)
        assert "encrypted and needs a password" in out(d)["permissionDecisionReason"]


def test_gate_unicode_path():
    with stub() as base:
        d = tmp() / "한글 폴더 1"
        d.mkdir()
        f = d / "보고서.txtx"
        f.write_bytes(bytes(1) + "안녕".encode("utf-8"))
        o = out(run(f, base))
        assert o["permissionDecision"] == "allow"
        got = Path(o["updatedInput"]["file_path"])
        assert got.exists() and got.read_text(encoding="utf-8") == "안녕\n"


def _odt_bytes(text):
    from reader_fix import make_zip
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')
    p = make_zip(tmp() / "a.odt", {
        "mimetype": "application/vnd.oasis.opendocument.text",
        "content.xml": (f"<office:document-content {ns}><office:body><office:text><text:p>{text}</text:p>"
                        "</office:text></office:body></office:document-content>")})
    return p.read_bytes()


def test_long_windows_path_is_converted():
    if sys.platform != "win32":
        print("note: test_long_windows_path_is_converted runs on Windows only")
        return
    base = str(tmp())
    long_dir = os.path.join(base, *(["d" * 60] * 4))
    target = os.path.join(long_dir, "보고서.odt")
    assert len(target) >= 260
    try:
        os.makedirs("\\\\?\\" + long_dir)
        with open("\\\\?\\" + target, "wb") as f:
            f.write(_odt_bytes("long path body"))
    except OSError as e:
        print(f"note: test_long_windows_path_is_converted skipped, the OS refused the long path ({e})")
        return
    old = os.environ.get("PHRO_READER_CACHE")
    os.environ["PHRO_READER_CACHE"] = str(tmp())
    try:
        o = out(gate.decide(ev(target)))
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old
    assert o["permissionDecision"] == "allow", o
    assert Path(o["updatedInput"]["file_path"]).read_text(encoding="utf-8") == "long path body\n"
    assert o["additionalContext"].startswith("보고서.odt was converted"), o["additionalContext"]


def test_long_path_forms():
    if sys.platform != "win32":
        assert gate.long_path(Path("/x/" + "a" * 300)) == Path("/x/" + "a" * 300)
        return
    name = "a" * 300 + ".odt"
    assert str(gate.long_path(Path("C:/x/../y/" + name))) == "\\\\?\\C:\\y\\" + name
    assert str(gate.long_path(Path("//srv/share/" + name))) == "\\\\?\\UNC\\srv\\share\\" + name
    assert str(gate.long_path(Path("\\\\?\\C:\\" + name))) == "\\\\?\\C:\\" + name
    assert gate.long_path(Path("C:/x/a.odt")) == Path("C:/x/a.odt")


def test_gate_uppercase_ext():
    with stub() as base:
        assert out(run(sample(name="X.TXTX"), base))["permissionDecision"] == "allow"


def test_import_is_light():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import reader.gate as g; "
            "r = g.decide({'tool_name': 'Read', 'tool_input': {'file_path': 'C:/x.py'}}); "
            "print(r); print(sorted(m for m in sys.modules if m == 'reader' or m.startswith('reader.')))")
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(HERE / "scripts")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "None"
    assert lines[1] == "['reader', 'reader.gate', 'reader.limits']", lines[1]


def hook(payload):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(READ), "hook"],
                          input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True)


def test_hook_cli_silent_for_other():
    r = hook({"tool_name": "Read", "tool_input": {"file_path": "C:/x.py"}})
    assert r.returncode == 0 and r.stdout == b""


def test_hook_cli_deeply_nested_stdin():
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(READ), "hook"], input=b"[" * 100_000,
                       capture_output=True)
    assert r.returncode == 0 and r.stdout == b"" and b"Traceback" not in r.stderr, r.stderr[-300:]


def test_hook_cli_deny_unicode():
    d = tmp() / "한글"
    d.mkdir()
    f = d / "보고서.hwp"
    f.write_bytes(bytes.fromhex("D0CF11E0"))
    r = hook(ev(f))
    assert r.returncode == 0
    o = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert o["permissionDecision"] == "deny" and "보고서.hwp" in o["permissionDecisionReason"]


def test_hooks_json_entry():
    h = json.loads((HERE / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    e = h["PreToolUse"][0]
    assert e["matcher"] == "^Read$"
    c = e["hooks"][0]
    assert c["type"] == "command" and c["command"].endswith('read.py" hook') and c["timeout"] == 60
    assert {"SessionStart", "SubagentStart", "UserPromptSubmit"} <= set(h)


def test_ooxml_four_converted():
    import test_reader_docm as dm
    import test_reader_slides as sl
    from reader_fix import make_xlsx
    rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>cellword</t></is></c></row>'
    d = tmp()
    files = {
        "a.docx": (dm.docm(dm.P("wordword")).read_bytes(), "docx"),
        "a.pptx": (sl.deck(["<p:sp/>"], kind="ppsx").read_bytes(), "pptx"),
        "a.xlsx": (make_xlsx(None, [{"name": "S", "rows": rows}]), "xlsx"),
        "a.xlsm": (make_xlsx(None, [{"name": "S", "rows": rows}]), "xlsx"),
    }
    with pdf_env() as base:
        for name, (data, skill) in files.items():
            (d / name).write_bytes(data)
            r = run(d / name, base)
            assert r is not None and out(r)["permissionDecision"] == "allow", (name, r)
            ctx = out(r)["additionalContext"]
            assert ctx.startswith(name + " was converted to Markdown by phro-project"), ctx
            assert ctx.endswith(f"to edit the file, use the {skill} skill."), (name, ctx)
        (d / "a.pdf").write_bytes(b"%PDF-1.7\n" + bytes(64))
        r = gate.decide(ev(d / "a.pdf"))  # a damaged pdf passes through to Claude's own Read
        assert r is not None and "permissionDecision" not in out(r), r
        assert out(r)["additionalContext"].startswith("phro-project could not convert a.pdf ("), r


def test_readme_formats_match():
    text = (HERE / "README.md").read_text(encoding="utf-8")
    sec = text.split("## Document reader", 1)[1].split("\n## ", 1)[0]
    for ext in sorted(reader.BUILT):
        assert "`" + ext + "`" in sec, ext
    assert "read.py prune" in sec


def test_notices_credit_port_sources():
    # a reader module whose docstring names a project it follows is on that project's Ported: line in the notices
    import ast
    import fnmatch
    import re
    text = (HERE / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    docs = {f.relative_to(HERE).as_posix(): ast.get_docstring(ast.parse(f.read_text(encoding="utf-8"))) or ""
            for f in sorted((HERE / "scripts" / "reader").rglob("*.py"))}
    for proj in ("dochan", "kordoc", "rhwp", "anydoc", "docling", "xberg", "tika", "foliate-js", "numbers-parser",
                 "keynote-parser", "djvu-rs", "jackcess", "libmspub", "libvisio", "libwpg", "poi"):
        sec = text.split(f"## {proj}\n", 1)[1].split("\n## ", 1)[0]
        paths = re.findall(r"`(scripts/[^`]+)`", next(ln for ln in sec.splitlines() if ln.startswith("Ported:")))
        for rel, doc in docs.items():
            if re.search(rf"\b{proj}\b", doc, re.I):
                assert any(fnmatch.fnmatch(rel, p) for p in paths), (proj, rel)


MPL_HEAD = ("# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the"
            " MPL was not", "# distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.")


def _mpl_ok(source: str) -> bool:
    """Whether a module's source starts with the three MPL lines: the licence's two and the port line naming the full
    commit and the src/lib/ files."""
    import re
    lines = source.splitlines()[:3]
    return (len(lines) == 3 and tuple(lines[:2]) == MPL_HEAD
            and re.match(r"^# Ported from (libmspub|libvisio|libwpg) [0-9a-f]{40}, src/lib/", lines[2]) is not None)


def test_mpl_headers():
    # every reader module whose docstring names libmspub, libvisio or libwpg (MPL-2.0) starts with the MPL header
    import ast
    import re
    ported = []
    for f in sorted((HERE / "scripts" / "reader").rglob("*.py")):
        source = f.read_text(encoding="utf-8")
        if re.search(r"\b(libmspub|libvisio|libwpg)\b", ast.get_docstring(ast.parse(source)) or "", re.I):
            ported.append(f.relative_to(HERE).as_posix())
            assert _mpl_ok(source), f
    assert {"scripts/reader/formats/vsd_stream.py", "scripts/reader/formats/pub_contents.py",
            "scripts/reader/formats/pub_quill.py", "scripts/reader/wpg.py"} <= set(ported), ported
    head = "\n".join(MPL_HEAD) + "\n# Ported from libvisio " + "a" * 40 + ", src/lib/X.cpp.\n"
    assert _mpl_ok(head + '"""From libvisio."""\n')
    assert not _mpl_ok('"""From libvisio."""\n')
    assert not _mpl_ok(head.replace("a" * 40, "a42aaff") + '"""From libvisio."""\n')
    assert not _mpl_ok(head.replace("MPL/2.0/.", "MPL/2.0/") + '"""From libvisio."""\n')


# -- pdf: every pdf test pins PHRO_READER_PYENV (off, a missing folder or a fake venv) so a real venv never runs

PDF_HINT = ' Pages start at "## Page N" lines. Read-only view; to edit or create a pdf, use the pdf skill.'
OFFER_START = " Built-in parser used; pypdfium2 (PyPI, 4 MB) reads tables, scans and forms better."
EXIT3 = [sys.executable, "-c", "import sys; sys.exit(3)"]

# a child CLI whose pdf reader stops after 2 pages
CUT_READ = '''import sys
sys.path.insert(0, {scripts!r})
from reader.formats import pdf
pdf.PDF_PAGES = 2
import read
sys.exit(read.main(sys.argv[1:]))
'''


@contextmanager
def envs(**values):
    """Set (or, for None, unset) environment variables; restore them after."""
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def pdf_env(pyenv="off"):
    """stub()'s throwaway cache and child, with PHRO_READER_PYENV pinned: "off" (native, no offer), a missing
    folder (native with the offer) or a fake venv."""
    import time
    import reader
    # the pdf page loop's deadline runs from reader.STARTED (the process start): a long test run must not use it up
    reader.STARTED = time.monotonic()
    with stub() as base, envs(PHRO_READER_PYENV=pyenv):
        yield base


def pdf_file(pages, name="a.pdf", **kw):
    import pdf_fix
    p = tmp() / name
    with open(gate.long_path(p), "wb") as f:  # a long name needs the \\?\ form on Windows
        f.write(pdf_fix.simple_pdf(pages, **kw))
    return p


def four_pages():
    from pdf_fix import text_ops
    return pdf_file([text_ops(f"Body{i}") for i in range(1, 5)])


def sev(p, sid=None, **extra):
    e = ev(p, **extra)
    if sid is not None:
        e["session_id"] = sid
    return e


def ctx_of(d):
    return out(d)["additionalContext"]


def md_lines(o):
    return Path(o["updatedInput"]["file_path"]).read_text(encoding="utf-8").splitlines()


def test_pdf_converts_native():
    from pdf_fix import text_ops
    with pdf_env(tmp() / "no-venv"):
        p = pdf_file([text_ops("Hi")])
        o = out(gate.decide(ev(p)))
        assert o["permissionDecision"] == "allow", o
        c = o["additionalContext"]
        assert c.startswith("a.pdf was converted to Markdown by phro-project (3 lines)." + PDF_HINT), c
        assert OFFER_START in c and "Read-only view (no XML" not in c, c
        assert "Hi" in md_lines(o) and "## Page 1" in md_lines(o)


def test_engine_in_key():
    import pdf_fix
    from reader import cache, pyenv
    v = pdf_fix.fake_venv(tmp() / "venv")
    with pdf_env(v):
        assert pyenv.engine() == "pdfium-5.15.2-7000"
        p = pdf_file([pdf_fix.text_ops("Hi")])
        k = cache.key(p, "pdfium-5.15.2-7000")
        d = cache.store(k, p, engine="native")
        o = out(gate.decide(ev(p), command=EXIT3))  # a cache hit: the child would exit 3
        assert o["permissionDecision"] == "allow", o
        assert Path(o["updatedInput"]["file_path"]) == d / "out.md", o
        assert OFFER_START not in o["additionalContext"], o


def test_verify_fail_repairs():
    import pdf_fix
    from reader import cache
    v = pdf_fix.fake_venv(tmp() / "venv")
    rec = next(v.rglob("RECORD"))
    rec.write_bytes(rec.read_bytes() + b"extra,,\n")
    with pdf_env(v):
        p = pdf_file([pdf_fix.text_ops("Hi")])
        o = out(gate.decide(ev(p)))
        assert o["permissionDecision"] == "allow", o
        assert Path(o["updatedInput"]["file_path"]).parent.name == cache.key(p, "native"), o
        c = o["additionalContext"]
        assert " The pdf engine failed its file check, so the built-in parser was used;" in c, c
        assert f'setup --venv "{v}".' in c and OFFER_START not in c, c
        assert len(c) < 600


def test_range_miss_verifies_first():
    """A whole entry cached under the pdfium key and a range past its cut: verify runs (once) before any venv child;
    a tampered venv reads natively with the repair sentence."""
    import pdf_fix
    from reader import cache, pyenv
    v = pdf_fix.fake_venv(tmp() / "venv")
    rec = next(v.rglob("RECORD"))
    rec.write_bytes(rec.read_bytes() + b"tampered,,\n")
    with pdf_env(v):
        p = four_pages()
        k = cache.key(p, pyenv.engine())
        d = cache.store(k, p, engine="native")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta.update(pages=[[1, 1], [2, 5]], pages_total=4)
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        calls, real_verify = [], pyenv.verify
        seen, real_run = [], gate.subprocess.run
        pyenv.verify = lambda venv: calls.append(venv) or real_verify(venv)
        gate.subprocess.run = lambda cmd, **kw: seen.append(cmd) or real_run(cmd, **kw)
        try:
            o = out(gate.decide(ev(p, pages="3-4")))
        finally:
            pyenv.verify, gate.subprocess.run = real_verify, real_run
        assert len(calls) == 1, calls
        venv_py = str(pyenv.interpreter(v))
        assert seen and all(c[0] != venv_py for c in seen), seen
        assert o["permissionDecision"] == "allow", o
        assert "failed its file check" in o["additionalContext"], o
        assert Path(o["updatedInput"]["file_path"]).parent.name.startswith(cache.key(p, "native")), o


def test_late_writes_no_marker():
    """The hook's own budget spent before a child: pass through, but no failure marker, so the next Read retries."""
    from reader import cache
    with pdf_env():
        p = four_pages()
        k = cache.key(p, "native")
        d = cache.store(k, p, engine="native")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta.update(pages=[[1, 1], [2, 5]], pages_total=4)
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with clock(0.0, 52.0):
            o = out(gate.decide(ev(p, pages="3-4")))
        assert "permissionDecision" not in o and "hook's time limit" in o["additionalContext"], o
        assert not (cache.root() / "failed" / f"{k}-p3-4").exists()
        seen, real_run = [], gate.subprocess.run
        gate.subprocess.run = lambda cmd, **kw: seen.append(cmd) or real_run(cmd, **kw)
        try:
            o = out(gate.decide(ev(p, pages="3-4")))
        finally:
            gate.subprocess.run = real_run
        assert len(seen) == 1 and o["permissionDecision"] == "allow", (seen, o)
        assert Path(o["updatedInput"]["file_path"]).parent.name == f"{k}-p3-4", o


def test_pages_values_parsed():
    assert gate.parse_pages(" 3 - 5 ") == (3, 5)
    assert gate.parse_pages("7") == (7, 7)
    for bad in ("5-3", "0", "3,5", "x", 7, "", "-3", "3-", "1-2-3", "+3", "٣", None, "1" * 19, "0-0"):
        assert gate.parse_pages(bad) is None, bad
    big = 10**17
    assert gate.parse_pages("1234567890") == (1234567890, 1234567890)
    assert gate.parse_pages(f"3-{big}") == (3, big) and gate.parse_pages("9" * 18) == (10**18 - 1, 10**18 - 1)
    with pdf_env():
        o = out(gate.decide(ev(four_pages(), pages="1234567890")))
        assert ' The pdf has 4 pages; pages="1234567890-1234567890" starts past its end.' in o["additionalContext"], o


def test_pages_offset_limit():
    with pdf_env():
        p = four_pages()
        o = out(gate.decide(ev(p, pages="2-3", offset=1, limit=9)))
        lines = md_lines(o)
        first, after = lines.index("## Page 2") + 1, lines.index("## Page 4") + 1
        u = o["updatedInput"]
        assert u["offset"] == first and u["limit"] == after - first and "pages" not in u, u
        o = out(gate.decide(ev(p, pages="4", limit=9)))
        u = o["updatedInput"]
        assert u["offset"] == md_lines(o).index("## Page 4") + 1 and "limit" not in u and "pages" not in u, u
        u = out(gate.decide(ev(p, pages="3,4")))["updatedInput"]  # not a pages value: dropped, the whole read
        assert "pages" not in u and "offset" not in u, u


def test_pages_past_cut_range():
    with pdf_env():
        cut = tmp() / "cut_read.py"
        cut.write_text(CUT_READ.format(scripts=str(HERE / "scripts")), encoding="utf-8")
        base = [sys.executable, "-I", "-X", "utf8", str(cut), "convert"]
        p = four_pages()
        seen, real = [], gate.subprocess.run

        def spy(cmd, **kw):
            seen.append(cmd)
            return real(cmd, **kw)
        gate.subprocess.run = spy
        try:
            o = out(gate.decide(ev(p, pages="3-4"), command=base))
        finally:
            gate.subprocess.run = real
        assert o["permissionDecision"] == "allow", o
        assert seen[-1][-2:] == ["--pages", "3-4"] and seen[-1][-4:-2] == ["--engine", "native"], seen
        assert "--pages" not in seen[0], seen
        got = Path(o["updatedInput"]["file_path"])
        assert got.parent.name.endswith("-p3-4"), got
        assert "## Page 3" in md_lines(o) and "## Page 1" not in md_lines(o)
        assert "pages" not in o["updatedInput"] and "offset" not in o["updatedInput"], o


def _edit_meta(p, engine="native", **changes):
    from reader import cache
    d = cache.lookup(cache.key(p, engine))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta.update(changes)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_pages_unrendered_pass_through():
    with pdf_env():
        p = four_pages()
        assert out(gate.decide(ev(p)))["permissionDecision"] == "allow"
        _edit_meta(p, unrendered=[2, 3])
        assert gate.decide(ev(p, pages="2-3")) is None
        assert gate.decide(ev(p, pages="3")) is None
        o = out(gate.decide(ev(p, pages="1-3")))
        assert o["permissionDecision"] == "allow" and o["updatedInput"]["offset"] == 1, o
        assert "Pages not shown as images: 2-3; Read the pdf with pages=\"2-3\" to see them." in o["additionalContext"]


def test_pages_past_end():
    with pdf_env():
        o = out(gate.decide(ev(four_pages(), pages="9", offset=3)))
        assert o["permissionDecision"] == "allow", o
        assert "pages" not in o["updatedInput"], o
        assert ' The pdf has 4 pages; pages="9-9" starts past its end.' in o["additionalContext"], o


def test_pdf_damaged_pass_through():
    from reader import cache
    with pdf_env():
        p = put("a.pdf", b"%PDF-1.7\n" + bytes(64))
        r = gate.decide(ev(p))
        o = out(r)
        assert set(o) == {"hookEventName", "additionalContext"} and o["hookEventName"] == "PreToolUse", o
        c = o["additionalContext"]
        assert c.startswith("phro-project could not convert a.pdf (the file looks damaged ("), c
        assert c.endswith("); Claude's own Read is used."), c
        assert (cache.root() / "failed" / cache.key(p, "native")).is_file()
        seen, real = [], gate.subprocess.run
        gate.subprocess.run = lambda cmd, **kw: seen.append(cmd) or real(cmd, **kw)
        try:
            assert gate.decide(ev(p), command=EXIT3) == r
        finally:
            gate.subprocess.run = real
        assert seen == [], seen


def test_pdf_timeout_pass_through():
    from pdf_fix import text_ops
    from reader import cache
    with pdf_env():
        p = pdf_file([text_ops("Hi")])
        r = gate.decide(ev(p), command=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        o = out(r)
        assert "permissionDecision" not in o, o
        assert o["additionalContext"] == ("phro-project could not convert a.pdf (conversion took over 45 seconds); "
                                          "Claude's own Read is used."), o
        assert (cache.root() / "failed" / cache.key(p, "native")).is_file()


def test_pdf_encrypted_denies():
    import pdf_fix
    with pdf_env():
        p = pdf_file([pdf_fix.text_ops("Hi")], encrypt=pdf_fix.EncryptSpec(4, user=b"pw"))
        o = out(gate.decide(ev(p)))
        assert o["permissionDecision"] == "deny", o
        assert o["permissionDecisionReason"] == (
            "a.pdf: the file is encrypted and needs a password. Read it with the pdf skill instead."), o


def test_offer_once_per_session():
    from pdf_fix import text_ops
    with pdf_env(tmp() / "no-venv"):
        p = pdf_file([text_ops("Hi")])
        assert OFFER_START in ctx_of(gate.decide(sev(p, "s1")))
        assert OFFER_START not in ctx_of(gate.decide(sev(p, "s1")))
        assert OFFER_START in ctx_of(gate.decide(sev(p, "s2")))
        assert OFFER_START in ctx_of(gate.decide(sev(p)))
        assert OFFER_START in ctx_of(gate.decide(sev(p)))


def test_offer_never_after_decline():
    from pdf_fix import text_ops
    from reader import cache
    with pdf_env(tmp() / "no-venv"):
        (cache.root() / "pdf-declined").write_text("", encoding="utf-8")
        c = ctx_of(gate.decide(sev(pdf_file([text_ops("Hi")]), "s1")))
        assert PDF_HINT in c and OFFER_START not in c, c


def test_offer_not_when_off():
    from pdf_fix import text_ops
    with pdf_env("off"):
        c = ctx_of(gate.decide(sev(pdf_file([text_ops("Hi")]), "s1")))
        assert PDF_HINT in c and OFFER_START not in c, c


def test_offer_fits_with_hint():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    read_py = "C:/Users/someone/.claude/plugins/cache/phro-markt/phro-project/0.16.0/scripts/read.py"
    venv = "C:/Users/someone/.claude/plugins/data/phro-project-phro/pdf-venv-xyz"
    assert len(read_py) == 85 and len(venv) == 68, (len(read_py), len(venv))
    old = gate.READ_PY
    gate.READ_PY = read_py
    try:
        offer = gate.offer_text(venv)
    finally:
        gate.READ_PY = old
    assert len(offer) == 194 + 85 + 68 and offer.startswith(OFFER_START), offer
    assert f'python -I -X utf8 "{read_py}" setup --venv "{venv}" (--decline' in offer, offer
    meta = {"lines": 120, "images": 3, "hidden": 3,
            "unread": ["1 slide", "2 charts", "3 form fields", "4 annotations", "5 links"]}
    c = gate._context("n" * 24 + ".pdf", meta, md, offer=offer)
    assert c.index(PDF_HINT) == 149 and len(PDF_HINT) == 94, c
    assert offer in c and len(c) < 600, (len(c), c)
    # the repair sentence fits the same way
    gate.READ_PY = read_py
    try:
        repair = gate.offer_text(venv, repair=True)
    finally:
        gate.READ_PY = old
    assert repair == (' The pdf engine failed its file check, so the built-in parser was used; to repair it, ask the user '
                      f'and only on their yes run: python -I -X utf8 "{read_py}" setup --venv "{venv}".'), repair
    c = gate._context("n" * 24 + ".pdf", meta, md, offer=repair)
    assert repair in c and len(c) < 600, (len(c), c)
    # an unrendered page whose sentence does not fit beside the offer: the sentence stays, the offer goes
    c = gate._context("n" * 24 + ".pdf", dict(meta, unrendered=[2]), md, offer=offer)
    assert offer not in c and ' Pages not shown as images: 2; Read the pdf with pages="2-2" to see them.' in c, c


def test_offer_yields_to_unrendered():
    """Through decide: with no room for both, the unrendered sentence wins and no offer marker is written, so the
    offer comes on a later Read that has room."""
    from pdf_fix import text_ops
    from reader import cache
    read_py = "C:/Users/someone/.claude/plugins/cache/phro-markt/phro-project/0.16.0/scripts/read.py"
    venv = "C:/Users/someone/.claude/plugins/data/phro-project-phro/pdf-venv-xyz"
    old = gate.READ_PY
    gate.READ_PY = read_py
    try:
        with pdf_env(venv) as base:  # READ_PY is fake here: the stub prefix converts
            p = pdf_file([text_ops("Hi")], name="n" * 50 + ".pdf")
            assert OFFER_START in ctx_of(gate.decide(sev(p), command=base))  # room for the offer alone
            _edit_meta(p, unrendered=[1])
            c = ctx_of(gate.decide(sev(p, "s1"), command=base))
            assert OFFER_START not in c and "Pages not shown as images: 1;" in c and len(c) < 600, c
            offers = cache.root() / "offers"
            assert not offers.exists() or not any(offers.iterdir())
            _edit_meta(p, unrendered=[])
            assert OFFER_START in ctx_of(gate.decide(sev(p, "s1"), command=base))
    finally:
        gate.READ_PY = old


def test_offer_dropped_when_long():
    from pdf_fix import text_ops
    from reader import cache
    with pdf_env(tmp() / "no-venv"):
        p = pdf_file([text_ops("Hi")], name="n" * 250 + ".pdf")
        o = out(gate.decide(sev(p, "s1")))
        assert o["permissionDecision"] == "allow", o
        c = o["additionalContext"]
        assert PDF_HINT in c and OFFER_START not in c and len(c) < 600, c
        offers = cache.root() / "offers"
        assert not offers.exists() or not any(offers.iterdir())


def _pdf_meta(pages=300, per=10, **extra):
    meta = {"lines": pages * per, "pages_total": pages, "unrendered": [], "figures": [],
            "pages": [[i, per * (i - 1) + 1] for i in range(1, pages + 1)],
            "headings": [[per * (i - 1) + 1, f"Page {i}"] for i in range(1, pages + 1)]}
    meta.update(extra)
    return meta


def test_many_unrendered_fit():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = _pdf_meta(unrendered=list(range(1, 301, 2)), figures=list(range(1, 51)))
    c = gate._context("a.pdf", meta, md)
    assert len(c) < 600, (len(c), c)
    assert " Pages not shown as images: 1, 3, 5, " in c and ', …; Read the pdf with pages="1-1" to see them.' in c, c
    meta = _pdf_meta(unrendered=list(range(5, 60)) + [70])
    c = gate._context("a.pdf", meta, md)
    assert ' Pages not shown as images: 5-59, 70; Read the pdf with pages="5-24" to see them.' in c, c


def test_pdf_context_order():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    meta = _pdf_meta(hidden=2, unrendered=[3, 5], figures=[4, 7], unread=["1 image"], warnings=["garbled"])
    old = gate.CONTEXT_MAX
    gate.CONTEXT_MAX = 5000  # room for every item at once
    try:
        c = gate._context("a.pdf", meta, md, past_end=' The pdf has 300 pages; pages="400-400" starts past its end.',
                          offer=gate.offer_text("v"))
    finally:
        gate.CONTEXT_MAX = old
    marks = [PDF_HINT, "starts past its end", OFFER_START, "Pages not shown", " Vector figures on pages 4, 7 are linked",
             "Some parts", "Pages at lines: 1:1, ", "garbled"]
    at = [c.index(m) for m in marks]
    assert at == sorted(at), (at, c)
    assert c.startswith("a.pdf was converted to Markdown by phro-project (3000 lines). 2 hidden places left out."), c


def test_page_index_long():
    md = tmp() / "out.md"
    md.write_text("x\n", encoding="utf-8")
    c = gate._context("a.pdf", _pdf_meta(), md)
    assert " Pages at lines: 1:1, " in c and "Sections at lines" not in c and len(c) < 600, c
    shown = [int(x.split(":")[0]) for x in c.split(" Pages at lines: ")[1].split(", ") if x != "…"]
    step = shown[1] - shown[0]
    assert step > 1 and shown == list(range(1, 301, step))[:len(shown)], shown  # every k-th page, smallest k
    longer = ", ".join(f"{p}:{10 * (p - 1) + 1}" for p in range(1, 301, step - 1))
    assert len(c) - len(c.split(" Pages at lines: ")[1]) + len(longer) > 599, step
    c = gate._context("a.pdf", _pdf_meta(pages=5, per=500), md)
    assert c.endswith(" Pages at lines: 1:1, 2:501, 3:1001, 4:1501, 5:2001"), c


def test_pdf_reader_off():
    from pdf_fix import text_ops
    with pdf_env(), off_env("pdf"):
        assert gate.decide(ev(pdf_file([text_ops("Hi")]))) is None


def test_ascii_pdf_not_text():
    import pdf_fix
    data = pdf_fix.simple_pdf([pdf_fix.text_ops("Hi")]).replace(b"\n%\xe2\xe3\xcf\xd3\n", b"\n%abcd\n", 1)
    assert data.isascii()
    with pdf_env():
        p = put("a.pdf", data)
        assert not gate.looks_like_text(p)
        o = out(gate.decide(ev(p)))
        assert o["permissionDecision"] == "allow" and "Hi" in md_lines(o), o


def test_unsupported_detail_reason():
    assert gate._why("unsupported", "Excel 5.0/95 workbook") == "this file is not supported (Excel 5.0/95 workbook)"
    assert gate._why("unsupported") == "this format is not supported yet"


def test_phase3_formats_convert():
    """Each phase 3 extension converts through the real read.py: a tiny clean fixture, written to a temp dir."""
    import test_reader_mobi as tm
    import test_reader_wpd as tw
    from iwork_fix import keynote09, numbers09, pages09, slide09
    import test_reader_iwork as ti
    wp = tw._wp6(b"Hello\x80" + b"World" + b"\xccNext")
    mobi = tm._mobi(tm.BOOK)
    iwork = {"pages": pages09(ti._p("wordword")),
             "numbers": numbers09(ti._ws("Sheet 1", ti._table09("Table 1", [[ti._t("cellword"), ti._n("5")]]))),
             "key": keynote09(slide09(ti._p("Title"), ti._p("slideword")))}
    files = {f"a.{e}": (wp, "Hello") for e in ("wpd", "wp", "wp5", "wp6", "wp7", "wpt")}
    files.update({f"a.{e}": (mobi, "first para") for e in ("mobi", "azw3")})
    files.update({"a.pages": (iwork["pages"], "wordword"), "a.numbers": (iwork["numbers"], "cellword"),
                  "a.key": (iwork["key"], "slideword")})
    with stub():  # only for its throwaway cache root: the real read.py converts
        for name, (data, word) in files.items():
            p = put(name, data)
            o = out(gate.decide(ev(p)))
            assert o["permissionDecision"] == "allow", (name, o)
            new_path = Path(o["updatedInput"]["file_path"])
            assert new_path.name == "out.md" and new_path != p, name
            assert word in new_path.read_text(encoding="utf-8"), (name, new_path.read_text(encoding="utf-8"))


def test_unsupported_mac_hint_fits():
    detail = "Mac WordPerfect; set PHRO_READER_SOFFICE=1 to read it through LibreOffice"
    child = [sys.executable, "-c", f"print('FAIL\tunsupported\t{detail}')"]
    with stub():
        p = put("a.wpd", bytes(8))
        o = out(gate.decide(ev(p), command=child))
    assert o["permissionDecision"] == "deny", o
    assert "PHRO_READER_SOFFICE=1" in o["permissionDecisionReason"], o
    assert "this file is not supported (" + detail + ")" in o["permissionDecisionReason"], o


def test_bundle_folder_passes():
    d = tmp() / "a.pages"
    d.mkdir()
    assert gate.decide(ev(d)) is None


def test_binary_key_not_zip_passes_through():
    der = bytes([0x30, 0x82, 0x04, 0xA2, 0x02, 0x01, 0x00, 0x02, 0x82]) + bytes(64)
    assert gate.decide(ev(put("a.key", der))) is None
    assert gate.decide(ev(put("b.key", b"PK\x03"))) is None


def test_keynote_key_converts():
    from iwork_fix import keynote09, slide09
    import test_reader_iwork as ti
    with stub():
        o = out(gate.decide(ev(put("a.key", keynote09(slide09(ti._p("T"), ti._p("slideword")))))))
    assert o["permissionDecision"] == "allow", o


def test_setup_for_flag():
    import io
    import tempfile
    from contextlib import redirect_stdout
    import read
    nowhere = Path(tempfile.gettempdir()).resolve() / "phro-no-such-mpp-venv"
    old = os.environ.get("PHRO_READER_MPPENV")
    os.environ["PHRO_READER_MPPENV"] = str(nowhere)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = read.main(["setup", "--for", "mpp", "--check"])
        assert code == 0 and buf.getvalue().strip() == f"MISSING	{nowhere}", buf.getvalue()
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_MPPENV", None)
        else:
            os.environ["PHRO_READER_MPPENV"] = old


# -- mpp: every mpp test pins PHRO_READER_MPPENV (a missing folder or a fake venv) and PATH (one folder, holding an
# inert java stub or nothing), so a real venv or a real Java is never used; no child runs unless a test starts one

MPP_HEAD = "{name}: Microsoft Project files are read only through MPXJ, which is not installed."
MPP_ENGINE = "mpxj-16.10.0-java21"

# a stand-in for the venv child: it records its arguments, files a one-line entry under the key the real child
# works out from its own ready.json (cache.key(path, engine id)) and prints the out.md path
FAKE_MPXJ_CHILD = '''import json, sys
sys.path.insert(0, {scripts!r})
from pathlib import Path
from reader import cache
Path({log!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
k = cache.key(Path(sys.argv[1]), {engine!r})
d = cache.root() / k[:2] / k
d.mkdir(parents=True, exist_ok=True)
(d / "out.md").write_text("# Plan" + chr(10), encoding="utf-8")
(d / "meta.json").write_text(json.dumps({{"lines": 1}}), encoding="utf-8")
print(d / "out.md")
'''


def mpp_offer(venv):
    return (' MPXJ 16.10.0 with JPype 1.7.1 (PyPI, about 32 MB, LGPL-2.1 and Apache-2.0) reads it using the Java on'
            ' this machine. Ask the user whether to install it, and only on their yes run: python -I -X utf8'
            f' "{gate.READ_PY}" setup --for mpp --venv "{venv}" (or the same with --decline if they never want it).')


@contextmanager
def mpp_env(venv, java=True):
    """stub()'s throwaway cache, PHRO_READER_MPPENV at `venv` and PATH holding one folder with an inert `java` file
    (java=True: find_java answers it; the file is never run) or nothing. Yields the java path."""
    bin_dir = tmp() / "bin"
    bin_dir.mkdir()
    exe = bin_dir / ("java.exe" if sys.platform == "win32" else "java")
    if java:
        exe.write_bytes(b"")
    with stub(), envs(PHRO_READER_MPPENV=venv, PATH=bin_dir):
        yield exe


@contextmanager
def no_child():
    """Fail the test if the gate starts any process."""
    real = gate.subprocess.run

    def refuse(cmd, **kw):
        raise AssertionError(f"a child was started: {cmd}")
    gate.subprocess.run = refuse
    try:
        yield
    finally:
        gate.subprocess.run = real


def mpp_venv(d, java):
    """A fake READY mpp venv at d (test_reader_pyenv's layout: an interpreter file, pyvenv.cfg, the mpxj and jpype1
    dist-infos) with a ready.json recording `java` as the Java on PATH, a JVM library file and Java 21."""
    import test_reader_pyenv as tp
    from reader import pyenv
    d = Path(d)
    tp.fake_mpp_venv(d)
    jvm = tmp() / "jdk" / "bin" / "server" / "jvm.dll"
    jvm.parent.mkdir(parents=True)
    jvm.write_bytes(b"")
    ready = {"kind": "mpp", "requirements": pyenv.requirements_hash("mpp"),
             "python": {"version": "3.12.0", "home": str(d.parent)}, "mpxj": "16.10.0", "jpype1": "1.7.1",
             "java": {"path": str(java), "major": 21, "home": str(jvm.parents[2]), "jvm": str(jvm),
                      "bits": pyenv.python_bits()},
             "records": pyenv.record_digests(d, "mpp")}
    (d / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
    return d


def project(name="a.mpp", data=b""):
    from reader_fix import cfb_bytes, comp_obj
    return put(name, cfb_bytes({"\x01CompObj": comp_obj(b"MSProject.MPP9", b"Microsoft Project"),
                                "Extra": data or b"x"})[0])


def reason_of(d):
    o = out(d)
    assert o["permissionDecision"] == "deny", o
    return o["permissionDecisionReason"]


def test_mpp_deny_with_offer():
    missing = tmp() / "no-mpp-venv"
    with mpp_env(missing), no_child():
        r = reason_of(gate.decide(ev(project())))
    assert r == MPP_HEAD.format(name="a.mpp") + mpp_offer(missing), r
    assert len(r) < gate.CONTEXT_MAX


def test_mpp_deny_no_java():
    with mpp_env(tmp() / "no-mpp-venv", java=False), no_child():
        r = reason_of(gate.decide(ev(project())))
    assert r == MPP_HEAD.format(name="a.mpp") + " It also needs Java 8 or later on PATH, which phro-project does not install.", r


def test_mpp_deny_declined():
    from reader import cache
    for java in (True, False):  # a decline is said first, Java or not
        with mpp_env(tmp() / "no-mpp-venv", java=java), no_child():
            (cache.root() / "mpp-declined").parent.mkdir(parents=True, exist_ok=True)
            (cache.root() / "mpp-declined").write_text("", encoding="utf-8")
            r = reason_of(gate.decide(ev(project())))
        assert r == (MPP_HEAD.format(name="a.mpp")
                     + f' (Declined; python -I -X utf8 "{gate.READ_PY}" setup --for mpp installs it.)'), r


def test_mpp_reason_fits():
    venv = tmp() / "mpp-venv"
    name = "x" * 496 + ".mpp"  # longer than a file name can be on disk: the reason alone is built
    with mpp_env(venv):
        r = gate.mpp_reason(Path("d") / name)
        assert len(r) <= gate.CONTEXT_MAX - 1, len(r)
        cmd = f'python -I -X utf8 "{gate.READ_PY}" setup --for mpp --venv "{venv}"'
        assert cmd in r and r.endswith(mpp_offer(venv)), r
        assert r.startswith("xxx") and "…: Microsoft Project files" in r, r
        short = gate.mpp_reason(Path("d") / "a b\tc.mpt")
        assert short.startswith("a b c.mpt: Microsoft Project") and "…" not in short, short
    with mpp_env(venv, java=False):
        r = gate.mpp_reason(Path("d") / name)
        assert len(r) == gate.CONTEXT_MAX - 1 and r.endswith(" which phro-project does not install."), r


def test_mpp_ready_child():
    from reader import cache, pyenv
    with mpp_env(tmp() / "unset") as java:
        v = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV=v):
            assert pyenv.engine("mpp", v) == MPP_ENGINE and pyenv.verify(v, "mpp")
            p = project()
            k = cache.key(p, MPP_ENGINE)
            assert k != cache.key(p)
            log = tmp() / "argv.json"
            child = tmp() / "child.py"
            child.write_text(FAKE_MPXJ_CHILD.format(scripts=str(HERE / "scripts"), log=str(log), engine=MPP_ENGINE),
                             encoding="utf-8")
            o = out(gate.decide(ev(p), command=[sys.executable, "-I", "-X", "utf8", str(child)]))
            assert o["permissionDecision"] == "allow", o
            assert Path(o["updatedInput"]["file_path"]) == cache.root() / k[:2] / k / "out.md", o
            args = json.loads(log.read_text(encoding="utf-8"))
            assert args == [str(p), "--engine", "mpxj"] and "--key" not in args, args  # the child hashes the file
            assert o["additionalContext"].startswith("a.mpp was converted to Markdown by phro-project (1 line)."), o
            # the gate looks the entry up under the engine's key: a second Read starts no child
            with no_child():
                o2 = out(gate.decide(ev(p), command=EXIT3))
            assert o2["updatedInput"] == o["updatedInput"], o2
            # with no command the child is the venv's interpreter, exactly as for pdf, and never gets --key
            p2 = project("b.mpt", b"y")
            seen, real = [], gate.subprocess.run

            def fake_run(cmd, **kw):
                seen.append(list(cmd))
                return real([sys.executable, "-I", "-X", "utf8", str(child), *cmd[6:]], **kw)
            gate.subprocess.run = fake_run
            try:
                o3 = out(gate.decide(ev(p2)))
            finally:
                gate.subprocess.run = real
            assert o3["permissionDecision"] == "allow", o3
            assert seen == [[str(pyenv.interpreter(v)), "-I", "-X", "utf8", str(gate.READ_PY), "convert", str(p2),
                             "--engine", "mpxj"]], seen
            k2 = cache.key(p2, MPP_ENGINE)
            assert Path(o3["updatedInput"]["file_path"]) == cache.root() / k2[:2] / k2 / "out.md", o3


def test_mpp_ready_but_broken_denies_with_offer():
    from reader import pyenv
    with mpp_env(tmp() / "unset") as java:
        # the recorded Java no longer resolves (removed or upgraded after setup)
        v = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV=v):
            assert pyenv.engine("mpp", v) == MPP_ENGINE
            java.unlink()
            with no_child():
                r = reason_of(gate.decide(ev(project()), command=EXIT3))
            assert r == MPP_HEAD.format(name="a.mpp") + " It also needs Java 8 or later on PATH, which phro-project does not install.", r
            java.write_bytes(b"")  # a java on PATH again, but another one than setup recorded
            ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
            ready["java"]["path"] = str(tmp() / "old-jdk" / java.name)
            (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
            with no_child():
                r = reason_of(gate.decide(ev(project()), command=EXIT3))
            assert r == MPP_HEAD.format(name="a.mpp") + mpp_offer(v), r
        # the venv's files changed after setup: the file check fails before any child starts
        v2 = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV=v2):
            assert pyenv.engine("mpp", v2) == MPP_ENGINE
            mod = v2 / "Lib" / "site-packages" / "mpxj" / "__init__.py"
            mod.write_bytes(mod.read_bytes() + b"# changed\n")
            with no_child():
                r = reason_of(gate.decide(ev(project()), command=EXIT3))
            assert r == MPP_HEAD.format(name="a.mpp") + mpp_offer(v2), r


def test_mpp_child_fail_kinds():
    from reader import pyenv
    with mpp_env(tmp() / "unset") as java:
        v = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV=v):
            def fail(kind, detail=""):
                return [sys.executable, "-c", f"print('FAIL\\t{kind}\\t{detail}')"]
            r = reason_of(gate.decide(ev(project()), command=fail("encrypted", "password")))
            assert r == "a.mpp: the file is encrypted and needs a password.", r
            r = reason_of(gate.decide(ev(project()), command=fail("damaged", "MPXJ could not read the file")))
            assert r.startswith("a.mpp: the file looks damaged (MPXJ could not read the file). Retry by hand:"), r
            r = reason_of(gate.decide(ev(project()), command=[sys.executable, "-c", "import time; time.sleep(5)"],
                                      timeout=1))
            assert r.startswith(f"a.mpp: conversion took over {gate.limits.CONVERT_SECONDS} seconds."), r
            # the venv child without its engine: the offer again (setup repairs the venv)
            from reader.formats import mpp
            assert gate.MPP_UNSUPPORTED == mpp.UNSUPPORTED
            r = reason_of(gate.decide(ev(project()), command=fail("unsupported", mpp.UNSUPPORTED)))
            assert r == MPP_HEAD.format(name="a.mpp") + mpp_offer(v), r
            # another format's compound file named .mpp, which its own parser refuses: that parser's detail
            r = reason_of(gate.decide(ev(project()), command=fail("unsupported", "Excel 5.0/95 workbook")))
            assert r == "a.mpp: this file is not supported (Excel 5.0/95 workbook).", r
            assert pyenv.engine("mpp", v) == MPP_ENGINE


def test_mpt_same_path():
    missing = tmp() / "no-mpp-venv"
    with mpp_env(missing), no_child():
        r = reason_of(gate.decide(ev(project("a.mpt"))))
    assert r == MPP_HEAD.format(name="a.mpt") + mpp_offer(missing), r
    assert "mpp" in reader.BUILT and "mpt" in reader.BUILT


def test_mpp_off_no_offer():
    # PHRO_READER_MPPENV=off turns the engine off: no install offer (setup could never make it read), Java or not,
    # declined or not
    from reader import cache
    for java in (True, False):
        for declined in (False, True):
            with mpp_env("off", java=java), no_child():
                if declined:
                    (cache.root() / "mpp-declined").parent.mkdir(parents=True, exist_ok=True)
                    (cache.root() / "mpp-declined").write_text("", encoding="utf-8")
                r = reason_of(gate.decide(ev(project())))
            assert r == MPP_HEAD.format(name="a.mpp") + " The mpp engine is turned off (PHRO_READER_MPPENV=off).", r
    # a READY venv is not used either while the engine is off
    with mpp_env(tmp() / "unset") as java:
        v = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV="off"), no_child():
            r = reason_of(gate.decide(ev(project()), command=EXIT3))
        assert r.endswith(" The mpp engine is turned off (PHRO_READER_MPPENV=off)."), r
        assert v.is_dir()


def test_mpp_reason_commands_never_cut():
    # READ_PY and the venv folder so long that the commands alone pass CONTEXT_MAX: the commands stay whole and
    # only the name is cut, down to "…"
    venv = Path("C:/" + "d" * 150) / "mpp-venv"
    real = gate.READ_PY
    gate.READ_PY = Path("C:/" + "p" * 150) / "scripts" / "read.py"
    try:
        with mpp_env(venv):
            r = gate.mpp_reason(Path("d") / "plan.mpp")
            cmd = f'python -I -X utf8 "{gate.READ_PY}" setup --for mpp --venv "{venv}"'
            assert len(r) > gate.CONTEXT_MAX and cmd in r, r
            assert r == MPP_HEAD.format(name="…") + mpp_offer(venv), r
    finally:
        gate.READ_PY = real


def test_mpp_child_default_timeout():
    # with no timeout given, the mpp child runs under CONVERT_SECONDS, as every other child
    with mpp_env(tmp() / "unset") as java:
        v = mpp_venv(tmp() / "mpp-venv", java)
        with envs(PHRO_READER_MPPENV=v):
            seen, real = [], gate._child
            gate._child = lambda cmd, start, timeout: seen.append(timeout) or (None, "damaged", "x")
            try:
                r = reason_of(gate.decide(ev(project())))
            finally:
                gate._child = real
    assert seen == [gate.limits.CONVERT_SECONDS] and "looks damaged (x)" in r, (seen, r)


def test_mpp_too_large_before_engine():
    # the size check comes first: an oversized Project file is denied as too large, offer or not
    with mpp_env(tmp() / "no-mpp-venv"), no_child():
        p = project()
        real = gate.limits.MAX_INPUT
        gate.limits.MAX_INPUT = 10
        try:
            r = reason_of(gate.decide(ev(p)))
        finally:
            gate.limits.MAX_INPUT = real
    assert r == "a.mpp: the file is over 512 MiB.", r


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
