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
    for n in ("a.py", "a.md", "a.png", "a.pdf", "a.docx"):
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


def test_binary_and_rtf_still_denied():
    for name, data in (("a.rtf", b"{\\rtf1\\ansi hi}"), ("b.rtf", b"\xef\xbb\xbf{\\rtf1 hi}"),
                       ("a.hwp", bytes.fromhex("D0CF11E0A1B11AE1") + bytes(100))):
        o = out(gate.decide(ev(put(name, data))))
        assert "not supported yet" in o["permissionDecisionReason"], name


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
    p = tmp() / "x.hwp"
    p.write_bytes(b"\xd0\xcf\x11\xe0\x00")
    o = out(gate.decide(ev(p)))
    assert o["permissionDecision"] == "deny"
    assert o["permissionDecisionReason"].startswith("x.hwp: this format is not supported yet. Retry by hand:")


def test_allow_swaps_path_keeps_keys():
    with stub() as base:
        o = out(run(sample(), base, offset=10, limit=50))
        assert o["permissionDecision"] == "allow" and o["hookEventName"] == "PreToolUse"
        u = o["updatedInput"]
        assert u["offset"] == 10 and u["limit"] == 50 and u["file_path"].endswith("out.md")


def test_context_text_and_length():
    with stub() as base:
        c = out(run(sample(), base))["additionalContext"]
        assert c == "a.txtx was converted to Markdown by phro-project (1 lines).", c
        assert len(c) < 600


def test_heading_index_long_output():
    with stub() as base:
        parts = ["# H"] + [f"p{i}" for i in range(599)] + ["# H"] + [f"q{i}" for i in range(600)]
        c = out(run(sample("|".join(parts)), base))["additionalContext"]
        assert "Sections at lines: 1 H, 1201 H" in c, c
        assert len(c) < 600


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


def test_timeout_denies():
    with stub():
        d = gate.decide(ev(sample()), command=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        assert "conversion took over 45 seconds" in out(d)["permissionDecisionReason"]


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
