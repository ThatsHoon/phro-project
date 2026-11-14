"""The optional pypdfium2 venv: pdfenv state checks, pdfsetup with stub steps, the pinned requirements and the
convert re-exec. No test runs the real pip or venv steps.

Run: python -I -X utf8 tests/test_reader_pdfenv.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import pdf_fix  # noqa: E402
import read  # noqa: E402
from reader import pdfenv, pdfsetup  # noqa: E402
from reader.model import Context  # noqa: E402

KEYS = ("PHRO_READER_CACHE", "PHRO_READER_PYENV", "CLAUDE_PLUGIN_DATA")


@contextmanager
def env(**over):
    """A fresh temp dir; the three env vars set from `over` (None unsets) and restored after. Yields the dir."""
    old = {k: os.environ.get(k) for k in KEYS}
    d = Path(tempfile.mkdtemp())
    try:
        os.environ["PHRO_READER_CACHE"] = str(d / "cache" / "reader")
        for k in ("PHRO_READER_PYENV", "CLAUDE_PLUGIN_DATA"):
            os.environ.pop(k, None)
        for k, v in over.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield d
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


class Stub:
    """Stub steps: a fake venv, no pip, no real smoke."""

    def __init__(self, *, pip=(True, "ok"), smoke=True, build=7000):
        self.pip_result, self.smoke_result, self.build, self.calls = pip, smoke, build, []

    def make_venv(self, tmp):
        self.calls.append("make_venv")
        pdf_fix.fake_venv(tmp, build=self.build)
        (Path(tmp) / "ready.json").unlink()
        # a real venv's home is the base python, which outlives the rename of the venv folder
        (Path(tmp) / "pyvenv.cfg").write_text(f"home = {Path(tmp).parent}\nversion = 3.12.0\n", encoding="utf-8")

    def pip(self, python, requirements):
        self.calls.append("pip")
        return self.pip_result

    def versions(self, python):
        self.calls.append("versions")
        return "5.15.2", self.build, pdfenv.record_digests(Path(python).parent.parent)

    def smoke(self, python):
        self.calls.append("smoke")
        return self.smoke_result


def leftovers(dirpath):
    return sorted(p.name for p in Path(dirpath).parent.iterdir()
                  if p.name.startswith(Path(dirpath).name + ".") )


def test_venv_dir_order():
    with env() as d:
        os.environ["PHRO_READER_PYENV"] = str(d / "X")
        os.environ["CLAUDE_PLUGIN_DATA"] = str(d / "D")
        assert pdfenv.venv_dir() == d / "X"
        del os.environ["PHRO_READER_PYENV"]
        assert pdfenv.venv_dir() == d / "D" / "pdf-venv"
        del os.environ["CLAUDE_PLUGIN_DATA"]
        from reader import cache
        (cache.root() / "pdf-venv.json").write_text(json.dumps({"venv": str(d / "P")}), encoding="utf-8")
        assert pdfenv.venv_dir() == d / "P"
        (cache.root() / "pdf-venv.json").write_text("not json", encoding="utf-8")
        assert pdfenv.venv_dir() == cache.root().parent / "pdf-venv"
        (cache.root() / "pdf-venv.json").unlink()
        assert pdfenv.venv_dir() == cache.root().parent / "pdf-venv"


def test_off_forces_native():
    with env() as d:
        default = pdfenv.venv_dir()
        pdf_fix.fake_venv(default)
        assert pdfenv.engine() == "pdfium-5.15.2-7000"
        os.environ["PHRO_READER_PYENV"] = "off"
        assert pdfenv.forced_native()
        assert pdfenv.engine() == "native"
        assert pdfenv.engine(default) == "native"


def test_ready_engine_id():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        assert pdfenv.state(v) == "READY"
        assert pdfenv.verify(v) is True
        assert pdfenv.engine(v) == "pdfium-5.15.2-7000"
        assert pdfenv.engine_id("1", 2) == "pdfium-1-2"


def test_stale_after_pin_change():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
        ready["requirements"] = "0" * 64
        (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
        assert pdfenv.state(v) == "STALE"
        assert pdfenv.engine(v) == "native"


def test_broken_home():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        (v / "pyvenv.cfg").write_text(f"home = {d / 'nowhere'}\n", encoding="utf-8")
        assert pdfenv.state(v) == "BROKEN"
        assert pdfenv.engine(v) == "native"
        v2 = pdf_fix.fake_venv(d / "v2")
        (v2 / "ready.json").write_text("{", encoding="utf-8")
        assert pdfenv.state(v2) == "BROKEN"
        v3 = pdf_fix.fake_venv(d / "v3")
        pdfenv.interpreter(v3).unlink()
        assert pdfenv.state(v3) == "BROKEN"


def test_missing():
    with env() as d:
        assert pdfenv.state(d / "nope") == "MISSING"
        assert pdfenv.engine(d / "nope") == "native"


def test_verify_record_mismatch():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        a = v / "Lib" / "site-packages" / "pypdfium2" / "__init__.py"
        old = a.read_bytes()
        a.write_bytes(old + b"# changed\n")
        assert pdfenv.verify(v) is False
        a.write_bytes(old)
        assert pdfenv.verify(v) is True
        a.unlink()
        assert pdfenv.verify(v) is False


def test_partial_install_ignored():
    with env() as d:
        dest = d / "pdf-venv"
        part = Path(str(dest) + ".tmp-99")
        part.mkdir(parents=True)
        (part / "junk").write_text("x", encoding="utf-8")
        lock = Path(str(dest) + ".lock")
        lock.write_text("1", encoding="utf-8")
        old = time.time() - 1200
        os.utime(part, (old, old))
        os.utime(lock, (old, old))
        assert pdfenv.state(dest) == "MISSING"
        out = pdfsetup.setup(dest, Stub())
        assert out.startswith("READY\t"), out
        assert not part.exists()
        assert not lock.exists()


def test_pointer_without_plugin_data():
    with env() as d:
        os.environ["CLAUDE_PLUGIN_DATA"] = str(d / "D")
        dest = pdfenv.venv_dir()
        assert pdfsetup.setup(dest, Stub()).startswith("READY\t")
        del os.environ["CLAUDE_PLUGIN_DATA"]
        assert pdfenv.venv_dir() == d / "D" / "pdf-venv"


def test_setup_success():
    with env() as d:
        dest = d / "pdf-venv"
        from reader import cache
        decl = cache.root() / "pdf-declined"
        decl.write_text("", encoding="utf-8")
        out = pdfsetup.setup(dest, Stub())
        assert out == f"READY\t{dest}\tpdfium-5.15.2-7000", out
        ready = json.loads((dest / "ready.json").read_text(encoding="utf-8"))
        assert ready["requirements"] == pdfenv.requirements_hash()
        assert ready["pypdfium2"] == "5.15.2" and ready["pdfium"] == 7000
        assert set(ready["records"]) == {"pypdfium2", "pypdfium2_raw"}
        assert "version" in ready["python"] and "home" in ready["python"]
        assert pdfenv.state(dest) == "READY" and pdfenv.verify(dest)
        assert leftovers(dest) == []
        assert not decl.exists() and not pdfenv.declined()
        assert json.loads((cache.root() / "pdf-venv.json").read_text(encoding="utf-8")) == {"venv": str(dest)}


def test_setup_replaces_old():
    with env() as d:
        dest = d / "pdf-venv"
        dest.mkdir()
        (dest / "marker").write_text("old", encoding="utf-8")
        assert pdfsetup.setup(dest, Stub()).startswith("READY\t")
        assert not (dest / "marker").exists()
        assert leftovers(dest) == []


def _nothing_left(dest):
    from reader import cache
    assert not dest.exists()
    assert leftovers(dest) == []
    assert not (cache.root() / "pdf-venv.json").exists()


def test_setup_pip_fail():
    with env() as d:
        dest = d / "pdf-venv"
        out = pdfsetup.setup(dest, Stub(pip=(False, "ERROR: boom\nsecond line")))
        assert out.startswith("FAIL\tpip\t") and "boom" in out and out.count("\n") == 0, out
        _nothing_left(dest)


def test_setup_no_wheel():
    with env() as d:
        dest = d / "pdf-venv"
        out = pdfsetup.setup(dest, Stub(pip=(False, "ERROR: No matching distribution found for pypdfium2")))
        assert out == "FAIL\tpip\tno wheel for this platform", out
        _nothing_left(dest)


def test_setup_smoke_fail():
    with env() as d:
        dest = d / "pdf-venv"
        out = pdfsetup.setup(dest, Stub(smoke=False))
        assert out.startswith("FAIL\tsmoke\t"), out
        _nothing_left(dest)


def test_setup_lock_busy():
    with env() as d:
        dest = d / "pdf-venv"
        lock = Path(str(dest) + ".lock")
        lock.write_text("1", encoding="utf-8")
        out = pdfsetup.setup(dest, Stub())
        assert out == "FAIL\tlock\tanother setup is running", out
        assert lock.exists()
        assert not dest.exists()


def test_lock_released_after_fail():
    with env() as d:
        dest = d / "pdf-venv"
        pdfsetup.setup(dest, Stub(smoke=False))
        assert not Path(str(dest) + ".lock").exists()


def test_check_decline_remove():
    with env() as d:
        dest = d / "pdf-venv"
        assert pdfsetup.check(dest) == f"MISSING\t{dest}"
        assert not pdfenv.declined()
        pdfsetup.decline()
        assert pdfenv.declined()
        assert pdfsetup.setup(dest, Stub()).startswith("READY\t")
        assert pdfsetup.check(dest) == f"READY\t{dest}\tpdfium-5.15.2-7000"
        pdfsetup.remove(dest)
        _nothing_left(dest)


def test_requirements_pinned():
    text = pdfenv.REQUIREMENTS.read_text(encoding="utf-8")
    assert "pypdfium2==5.15.2" in text
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", text)) == 7


def test_smoke_pdf_reads():
    from reader import markdown
    from reader.formats import pdf
    ctx = Context()
    doc = pdf.parse_bytes(pdfsetup._smoke_pdf(), ctx)
    with tempfile.TemporaryDirectory() as t:
        text = markdown.render(doc, Path(t) / "assets", False, ctx.skipped)
    assert "phro smoke" in text


def test_convert_reexec():
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout=b"relayed\n", stderr=b"")

    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        src = d / "a.pdf"
        src.write_bytes(pdf_fix.simple_pdf([pdf_fix.text_ops("hello")]))
        os.environ["PHRO_READER_PYENV"] = str(v)
        real = subprocess.run
        subprocess.run = fake_run
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = read.main(["convert", str(src)])
            assert code == 0 and "relayed" in buf.getvalue()
            assert len(calls) == 1
            cmd = calls[0]
            assert Path(cmd[0]) == pdfenv.interpreter(v)
            assert cmd[1:4] == ["-I", "-X", "utf8"] and "convert" in cmd
            assert cmd[cmd.index("--engine") + 1] == "pdfium" and str(src) in cmd
            calls.clear()
            os.environ["PHRO_READER_PYENV"] = "off"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = read.main(["convert", str(src)])
            assert code == 0 and calls == []
            assert buf.getvalue().strip().endswith("out.md")
        finally:
            subprocess.run = real


def test_pdfenv_import_light():
    code = ("import sys; sys.path.insert(0, %r); import reader.pdfenv as p; "
            "bad = [m for m in sys.modules if m.startswith('reader.formats')]; "
            "assert not bad, bad; assert 'subprocess' not in sys.modules; print('ok')" % str(HERE / "scripts"))
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
