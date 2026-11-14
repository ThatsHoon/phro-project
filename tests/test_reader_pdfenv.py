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
        assert set(ready["records"]) == {"pypdfium2"}
        assert "version" in ready["python"] and "home" in ready["python"]
        assert pdfenv.state(dest) == "READY" and pdfenv.verify(dest)
        assert leftovers(dest) == []
        assert not decl.exists() and not pdfenv.declined()
        assert json.loads((cache.root() / "pdf-venv.json").read_text(encoding="utf-8")) == {"venv": str(dest)}


def test_setup_replaces_old():
    with env() as d:
        dest = d / "pdf-venv"
        dest.mkdir()
        (dest / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
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
    lines = text.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith("pypdfium2==5.15.2"))
    last = max(i for i, ln in enumerate(lines) if "--hash=sha256:" in ln)
    cont = lines[first:last + 1]
    assert len(cont) == 8, cont
    assert not any(ln.lstrip().startswith("#") for ln in cont), "a comment inside the continuation"
    assert all(ln.endswith("\\") for ln in cont[:-1]) and not cont[-1].endswith("\\")


def test_requirements_pip_parse():
    # a child, because the tests folder holds modules that shadow stdlib names pip imports
    code = ("import json, sys\n"
            "try:\n"
            "    from pip._internal.network.session import PipSession\n"
            "    from pip._internal.req.req_file import parse_requirements\n"
            "except Exception:\n"
            "    sys.exit(3)\n"
            "reqs = list(parse_requirements(sys.argv[1], session=PipSession()))\n"
            "print(json.dumps([[r.requirement, r.options.get('hashes', {}).get('sha256')] for r in reqs]))\n")
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(pdfenv.REQUIREMENTS)],
                       capture_output=True, text=True, cwd=tempfile.gettempdir())
    if r.returncode == 3:
        print("note: pip not importable, pip parse check skipped")
        return
    assert r.returncode == 0, r.stderr
    want = re.findall(r"--hash=sha256:([0-9a-f]{64})", pdfenv.REQUIREMENTS.read_text(encoding="utf-8"))
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(got) == 1, got
    assert got[0][0].replace(" ", "") == "pypdfium2==5.15.2" and got[0][1] == want, got


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


class Shadow(Stub):
    def __init__(self, hook, **kw):
        super().__init__(**kw)
        self.hook = hook

    def make_venv(self, tmp):
        super().make_venv(tmp)
        self.hook()


class Boom(Stub):
    def __init__(self, step):
        super().__init__()
        self.step = step

    def pip(self, python, requirements):
        if self.step == "pip":
            raise OSError("pip exploded")
        return super().pip(python, requirements)

    def versions(self, python):
        if self.step == "versions":
            raise OSError("no versions")
        return super().versions(python)

    def smoke(self, python):
        if self.step == "smoke":
            raise OSError("no smoke")
        return super().smoke(python)


def test_setup_refuses_foreign_dir():
    with env() as d:
        dest = d / "docs"
        dest.mkdir()
        (dest / "thesis.docx").write_text("precious", encoding="utf-8")
        out = pdfsetup.setup(dest, Stub())
        assert out == f"FAIL\tinstall\t{dest.resolve()} is not a pdf venv", out
        assert (dest / "thesis.docx").read_text(encoding="utf-8") == "precious"
        assert not Path(str(dest) + ".lock").exists()
        assert leftovers(dest) == []
        empty = d / "empty"  # an empty folder is fine to replace
        empty.mkdir()
        assert pdfsetup.setup(empty, Stub()).startswith("READY\t")


def test_remove_refuses_foreign_dir():
    with env() as d:
        dest = d / "docs"
        dest.mkdir()
        (dest / "thesis.docx").write_text("precious", encoding="utf-8")
        out = pdfsetup.remove(dest)
        assert out == f"FAIL\tinstall\t{dest.resolve()} is not a pdf venv", out
        assert (dest / "thesis.docx").is_file()


def test_remove_keeps_other_pointer():
    with env() as d:
        from reader import cache
        mine, other = d / "mine", d / "other"
        pdf_fix.fake_venv(mine)
        (cache.root() / "pdf-venv.json").write_text(json.dumps({"venv": str(other)}), encoding="utf-8")
        assert pdfsetup.remove(mine).startswith("REMOVED")
        assert not mine.exists()
        assert (cache.root() / "pdf-venv.json").exists()


def test_verify_layout():
    import hashlib
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        site = v / "Lib" / "site-packages"
        assert set(json.loads((v / "ready.json").read_text(encoding="utf-8"))["records"]) == {"pypdfium2"}
        assert not list(site.glob("pypdfium2_raw-*"))
        rec = next(site.glob("pypdfium2-*.dist-info")) / "RECORD"
        text = rec.read_text(encoding="utf-8")
        assert "pypdfium2_raw/" in text and "../../Scripts/" in text
        # no pypdfium2_raw file listed: not a real install
        rec.write_text("".join(ln for ln in text.splitlines(True) if "pypdfium2_raw/" not in ln), encoding="utf-8")
        ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
        ready["records"]["pypdfium2"] = hashlib.sha256(rec.read_bytes()).hexdigest()
        (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
        assert pdfenv.verify(v) is False
        v2 = pdf_fix.fake_venv(d / "v2")  # two dist-infos
        s2 = v2 / "Lib" / "site-packages"
        shutil.copytree(next(s2.glob("pypdfium2-*.dist-info")), s2 / "pypdfium2-5.0.0.dist-info")
        assert pdfenv.verify(v2) is False


def test_step_isolation():
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with env() as d:
        venv = d / "pdf-venv"
        real = pdfsetup.subprocess.run
        pdfsetup.subprocess.run = fake_run
        try:
            pdfsetup.Steps().make_venv(venv)
            assert pdfsetup.Steps().pip(venv / "bin" / "python3", pdfenv.REQUIREMENTS) == (True, "")
        finally:
            pdfsetup.subprocess.run = real
        mk, pip = seen
        assert "-I" in mk[0] and Path(mk[1]["cwd"]) == d
        assert "-I" in pip[0] and "--isolated" in pip[0] and pip[1]["timeout"] == 900 and Path(pip[1]["cwd"]) == d

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 900)

        pdfsetup.subprocess.run = slow
        try:
            assert pdfsetup.Steps().pip(venv / "bin" / "python3", pdfenv.REQUIREMENTS) == (False, "timeout")
        finally:
            pdfsetup.subprocess.run = real


def test_step_exceptions_are_fail_lines():
    for step in ("pip", "versions", "smoke"):
        with env() as d:
            dest = d / "pdf-venv"
            out = pdfsetup.setup(dest, Boom(step))
            assert out.startswith(f"FAIL\t{step}\t"), out
            _nothing_left(dest)
    with env() as d:
        from reader import cache
        (cache.root() / "pdf-venv.json").mkdir()
        out = pdfsetup.setup(d / "pdf-venv", Stub())
        assert out.startswith("FAIL\tpointer\t"), out
        assert not Path(str(d / "pdf-venv") + ".lock").exists()


def test_convert_reexec_oserror_falls_back():
    def broken(cmd, *a, **kw):
        raise OSError("cannot launch")

    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        src = d / "a.pdf"
        src.write_bytes(pdf_fix.simple_pdf([pdf_fix.text_ops("hello")]))
        os.environ["PHRO_READER_PYENV"] = str(v)
        real = subprocess.run
        subprocess.run = broken
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = read.main(["convert", str(src)])
        finally:
            subprocess.run = real
        assert code == 0 and buf.getvalue().strip().endswith("out.md"), buf.getvalue()


def test_tmp_cleanup_by_prefix():
    with env() as d:
        dest = d / "v[ab]"
        keep = d / "va.tmp-keep"
        mine_tmp = d / "v[ab].tmp-77"
        mine_old = d / "v[ab].old-5"
        fresh_old = d / "v[ab].old-6"
        for p in (keep, mine_tmp, mine_old, fresh_old):
            p.mkdir()
        t = time.time() - 1200
        for p in (keep, mine_tmp, mine_old):
            os.utime(p, (t, t))
        assert pdfsetup.setup(dest, Stub()).startswith("READY\t")
        assert keep.exists() and not mine_tmp.exists() and not mine_old.exists() and fresh_old.exists()


def test_paths_are_resolved():
    with env() as d:
        from reader import cache
        cwd = os.getcwd()
        os.chdir(d)
        try:
            out = pdfsetup.setup(Path("rel-venv"), Stub())
            assert pdfsetup.check(Path("rel-venv")).startswith("READY\t")
        finally:
            os.chdir(cwd)
        want = (d / "rel-venv").resolve()
        assert out.split("\t")[1] == str(want)
        assert json.loads((cache.root() / "pdf-venv.json").read_text(encoding="utf-8")) == {"venv": str(want)}


def test_fallback_cache_default_venv():
    with env() as d:
        blocker = d / "afile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["PHRO_READER_CACHE"] = str(blocker / "sub")
        old_tmp = tempfile.tempdir
        tempfile.tempdir = str(d)
        try:
            from reader import cache
            root = cache.root()
            assert root.parent == d, root
            assert pdfenv.venv_dir() == root / "pdf-venv"
        finally:
            tempfile.tempdir = old_tmp


def test_lock_token_ownership():
    with env() as d:
        dest = d / "pdf-venv"
        lock = Path(str(dest) + ".lock")
        out = pdfsetup.setup(dest, Shadow(lambda: lock.write_text("someone else", encoding="utf-8")))
        assert out.startswith("READY\t")
        assert lock.read_text(encoding="utf-8") == "someone else"


def test_swap_restore_failure_names_both():
    with env() as d:
        dest = (d / "pdf-venv").resolve()
        dest.mkdir()
        (dest / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        real = os.replace
        calls = []

        def fake_replace(src, dst, *a, **kw):
            calls.append((str(src), str(dst)))
            if Path(dst) == dest and len(calls) >= 2:
                raise OSError("denied")
            return real(src, dst, *a, **kw)

        os.replace = fake_replace
        try:
            out = pdfsetup.setup(dest, Stub())
        finally:
            os.replace = real
        assert out.startswith("FAIL\tinstall\t") and str(dest) in out and ".old-" in out, out


def test_engine_native_without_version():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
        del ready["pdfium"]
        (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
        assert pdfenv.engine(v) == "native"


def test_read_py_does_not_shadow_stdlib():
    code = ("import sys; sys.path.insert(0, %r); import read; import queue; "
            "assert hasattr(queue, 'Queue'), queue.__file__; print('ok')" % str(HERE / "scripts"))
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True,
                       cwd=tempfile.gettempdir())
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_stale_outlasts_pip_timeout():
    from reader import pdfsetup
    assert pdfsetup.STALE_SECONDS > pdfsetup.PIP_TIMEOUT


def test_read_py_before_site_packages():
    code = ("import sys; sys.path.insert(0, %r); import read; "
            "here = read._HERE; "
            "p = read._place(['/x/lib/python3.12', here, '/x/lib/site-packages', '/y'], here); "
            "assert p == ['/x/lib/python3.12', here, '/x/lib/site-packages', '/y'], p; "
            "p = read._place([here, '/x/lib', '/x/lib/site-packages'], here); "
            "assert p == ['/x/lib', here, '/x/lib/site-packages'], p; "
            "assert read._place(['/x/lib'], here) == ['/x/lib', here]; print('ok')" % str(HERE / "scripts"))
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True,
                       cwd=tempfile.gettempdir())
    assert r.stdout.strip() == "ok", r.stdout + r.stderr


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
