"""The optional pypdfium2 venv: pyenv state checks, pysetup with stub steps, the pinned requirements and the
convert re-exec. No test runs the real pip or venv steps.

Run: python -I -X utf8 tests/test_reader_pyenv.py
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
from reader import pyenv, pysetup  # noqa: E402
from reader.model import Context  # noqa: E402

KEYS = ("PHRO_READER_CACHE", "PHRO_READER_PYENV", "PHRO_READER_MPPENV", "CLAUDE_PLUGIN_DATA")


@contextmanager
def env(**over):
    """A fresh temp dir; the three env vars set from `over` (None unsets) and restored after. Yields the dir."""
    old = {k: os.environ.get(k) for k in KEYS}
    d = Path(tempfile.mkdtemp())
    try:
        os.environ["PHRO_READER_CACHE"] = str(d / "cache" / "reader")
        for k in ("PHRO_READER_PYENV", "PHRO_READER_MPPENV", "CLAUDE_PLUGIN_DATA"):
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
        return "5.15.2", self.build, pyenv.record_digests(Path(python).parent.parent)

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
        assert pyenv.venv_dir() == d / "X"
        del os.environ["PHRO_READER_PYENV"]
        assert pyenv.venv_dir() == d / "D" / "pdf-venv"
        del os.environ["CLAUDE_PLUGIN_DATA"]
        from reader import cache
        (cache.root() / "pdf-venv.json").write_text(json.dumps({"venv": str(d / "P")}), encoding="utf-8")
        assert pyenv.venv_dir() == d / "P"
        (cache.root() / "pdf-venv.json").write_text("not json", encoding="utf-8")
        assert pyenv.venv_dir() == cache.root().parent / "pdf-venv"
        (cache.root() / "pdf-venv.json").unlink()
        assert pyenv.venv_dir() == cache.root().parent / "pdf-venv"


def test_off_forces_native():
    with env() as d:
        default = pyenv.venv_dir()
        pdf_fix.fake_venv(default)
        assert pyenv.engine() == "pdfium-5.15.2-7000"
        os.environ["PHRO_READER_PYENV"] = "off"
        assert pyenv.forced_native()
        assert pyenv.engine() == "native"
        assert pyenv.engine(venv=default) == "native"


def test_ready_engine_id():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        assert pyenv.state(v) == "READY"
        assert pyenv.verify(v) is True
        assert pyenv.engine(venv=v) == "pdfium-5.15.2-7000"
        assert pyenv.engine_id("1", 2) == "pdfium-1-2"


def test_stale_after_pin_change():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
        ready["requirements"] = "0" * 64
        (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
        assert pyenv.state(v) == "STALE"
        assert pyenv.engine(venv=v) == "native"


def test_broken_home():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        (v / "pyvenv.cfg").write_text(f"home = {d / 'nowhere'}\n", encoding="utf-8")
        assert pyenv.state(v) == "BROKEN"
        assert pyenv.engine(venv=v) == "native"
        v2 = pdf_fix.fake_venv(d / "v2")
        (v2 / "ready.json").write_text("{", encoding="utf-8")
        assert pyenv.state(v2) == "BROKEN"
        v3 = pdf_fix.fake_venv(d / "v3")
        pyenv.interpreter(v3).unlink()
        assert pyenv.state(v3) == "BROKEN"


def test_missing():
    with env() as d:
        assert pyenv.state(d / "nope") == "MISSING"
        assert pyenv.engine(venv=d / "nope") == "native"


def test_verify_record_mismatch():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        a = v / "Lib" / "site-packages" / "pypdfium2" / "__init__.py"
        old = a.read_bytes()
        a.write_bytes(old + b"# changed\n")
        assert pyenv.verify(v) is False
        a.write_bytes(old)
        assert pyenv.verify(v) is True
        a.unlink()
        assert pyenv.verify(v) is False


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
        assert pyenv.state(dest) == "MISSING"
        out = pysetup.setup(dest, Stub())
        assert out.startswith("READY\t"), out
        assert not part.exists()
        assert not lock.exists()


def test_pointer_without_plugin_data():
    with env() as d:
        os.environ["CLAUDE_PLUGIN_DATA"] = str(d / "D")
        dest = pyenv.venv_dir()
        assert pysetup.setup(dest, Stub()).startswith("READY\t")
        del os.environ["CLAUDE_PLUGIN_DATA"]
        assert pyenv.venv_dir() == d / "D" / "pdf-venv"


def test_setup_success():
    with env() as d:
        dest = d / "pdf-venv"
        from reader import cache
        decl = cache.root() / "pdf-declined"
        decl.write_text("", encoding="utf-8")
        out = pysetup.setup(dest, Stub())
        assert out == f"READY\t{dest}\tpdfium-5.15.2-7000", out
        ready = json.loads((dest / "ready.json").read_text(encoding="utf-8"))
        assert ready["requirements"] == pyenv.requirements_hash()
        assert ready["pypdfium2"] == "5.15.2" and ready["pdfium"] == 7000
        assert set(ready["records"]) == {"pypdfium2"}
        assert "version" in ready["python"] and "home" in ready["python"]
        assert pyenv.state(dest) == "READY" and pyenv.verify(dest)
        assert leftovers(dest) == []
        assert not decl.exists() and not pyenv.declined()
        assert json.loads((cache.root() / "pdf-venv.json").read_text(encoding="utf-8")) == {"venv": str(dest)}


def test_setup_replaces_old():
    with env() as d:
        dest = d / "pdf-venv"
        dest.mkdir()
        (dest / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        (dest / "marker").write_text("old", encoding="utf-8")
        assert pysetup.setup(dest, Stub()).startswith("READY\t")
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
        out = pysetup.setup(dest, Stub(pip=(False, "ERROR: boom\nsecond line")))
        assert out.startswith("FAIL\tpip\t") and "boom" in out and out.count("\n") == 0, out
        _nothing_left(dest)


def test_setup_no_wheel():
    with env() as d:
        dest = d / "pdf-venv"
        out = pysetup.setup(dest, Stub(pip=(False, "ERROR: No matching distribution found for pypdfium2")))
        assert out == "FAIL\tpip\tno wheel for this platform", out
        _nothing_left(dest)


def test_setup_smoke_fail():
    with env() as d:
        dest = d / "pdf-venv"
        out = pysetup.setup(dest, Stub(smoke=False))
        assert out.startswith("FAIL\tsmoke\t"), out
        _nothing_left(dest)


def test_setup_lock_busy():
    with env() as d:
        dest = d / "pdf-venv"
        lock = Path(str(dest) + ".lock")
        lock.write_text("1", encoding="utf-8")
        out = pysetup.setup(dest, Stub())
        assert out == "FAIL\tlock\tanother setup is running", out
        assert lock.exists()
        assert not dest.exists()


def test_lock_released_after_fail():
    with env() as d:
        dest = d / "pdf-venv"
        pysetup.setup(dest, Stub(smoke=False))
        assert not Path(str(dest) + ".lock").exists()


def test_check_decline_remove():
    with env() as d:
        dest = d / "pdf-venv"
        assert pysetup.check(dest) == f"MISSING\t{dest}"
        assert not pyenv.declined()
        pysetup.decline()
        assert pyenv.declined()
        assert pysetup.setup(dest, Stub()).startswith("READY\t")
        assert pysetup.check(dest) == f"READY\t{dest}\tpdfium-5.15.2-7000"
        pysetup.remove(dest)
        _nothing_left(dest)


def test_requirements_pinned():
    text = pyenv.REQUIREMENTS["pdf"].read_text(encoding="utf-8")
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
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(pyenv.REQUIREMENTS["pdf"])],
                       capture_output=True, text=True, cwd=tempfile.gettempdir())
    if r.returncode == 3:
        print("note: pip not importable, pip parse check skipped")
        return
    assert r.returncode == 0, r.stderr
    want = re.findall(r"--hash=sha256:([0-9a-f]{64})", pyenv.REQUIREMENTS["pdf"].read_text(encoding="utf-8"))
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(got) == 1, got
    assert got[0][0].replace(" ", "") == "pypdfium2==5.15.2" and got[0][1] == want, got


def test_smoke_pdf_reads():
    from reader import markdown
    from reader.formats import pdf
    ctx = Context()
    doc = pdf.parse_bytes(pysetup._smoke_pdf(), ctx)
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
            assert Path(cmd[0]) == pyenv.interpreter(v)
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


def test_pyenv_import_light():
    code = ("import sys; sys.path.insert(0, %r); import reader.pyenv as p; "
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
        out = pysetup.setup(dest, Stub())
        assert out == f"FAIL\tinstall\t{dest.resolve()} is not a pdf venv", out
        assert (dest / "thesis.docx").read_text(encoding="utf-8") == "precious"
        assert not Path(str(dest) + ".lock").exists()
        assert leftovers(dest) == []
        empty = d / "empty"  # an empty folder is fine to replace
        empty.mkdir()
        assert pysetup.setup(empty, Stub()).startswith("READY\t")


def test_remove_refuses_foreign_dir():
    with env() as d:
        dest = d / "docs"
        dest.mkdir()
        (dest / "thesis.docx").write_text("precious", encoding="utf-8")
        out = pysetup.remove(dest)
        assert out == f"FAIL\tinstall\t{dest.resolve()} is not a pdf venv", out
        assert (dest / "thesis.docx").is_file()


def test_remove_keeps_other_pointer():
    with env() as d:
        from reader import cache
        mine, other = d / "mine", d / "other"
        pdf_fix.fake_venv(mine)
        (cache.root() / "pdf-venv.json").write_text(json.dumps({"venv": str(other)}), encoding="utf-8")
        assert pysetup.remove(mine).startswith("REMOVED")
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
        assert pyenv.verify(v) is False
        v2 = pdf_fix.fake_venv(d / "v2")  # two dist-infos
        s2 = v2 / "Lib" / "site-packages"
        shutil.copytree(next(s2.glob("pypdfium2-*.dist-info")), s2 / "pypdfium2-5.0.0.dist-info")
        assert pyenv.verify(v2) is False


def test_step_isolation():
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with env() as d:
        venv = d / "pdf-venv"
        real = pysetup.subprocess.run
        pysetup.subprocess.run = fake_run
        try:
            pysetup.Steps().make_venv(venv)
            assert pysetup.Steps().pip(venv / "bin" / "python3", pyenv.REQUIREMENTS["pdf"]) == (True, "")
        finally:
            pysetup.subprocess.run = real
        mk, pip = seen
        assert "-I" in mk[0] and Path(mk[1]["cwd"]) == d
        assert "-I" in pip[0] and "--isolated" in pip[0] and pip[1]["timeout"] == 900 and Path(pip[1]["cwd"]) == d

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 900)

        pysetup.subprocess.run = slow
        try:
            assert pysetup.Steps().pip(venv / "bin" / "python3", pyenv.REQUIREMENTS["pdf"]) == (False, "timeout")
        finally:
            pysetup.subprocess.run = real


def test_step_exceptions_are_fail_lines():
    for step in ("pip", "versions", "smoke"):
        with env() as d:
            dest = d / "pdf-venv"
            out = pysetup.setup(dest, Boom(step))
            assert out.startswith(f"FAIL\t{step}\t"), out
            _nothing_left(dest)
    with env() as d:
        from reader import cache
        (cache.root() / "pdf-venv.json").mkdir()
        out = pysetup.setup(d / "pdf-venv", Stub())
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
        assert pysetup.setup(dest, Stub()).startswith("READY\t")
        assert keep.exists() and not mine_tmp.exists() and not mine_old.exists() and fresh_old.exists()


def test_paths_are_resolved():
    with env() as d:
        from reader import cache
        cwd = os.getcwd()
        os.chdir(d)
        try:
            out = pysetup.setup(Path("rel-venv"), Stub())
            assert pysetup.check(Path("rel-venv")).startswith("READY\t")
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
            assert pyenv.venv_dir() == root / "pdf-venv"
        finally:
            tempfile.tempdir = old_tmp


def test_lock_token_ownership():
    with env() as d:
        dest = d / "pdf-venv"
        lock = Path(str(dest) + ".lock")
        out = pysetup.setup(dest, Shadow(lambda: lock.write_text("someone else", encoding="utf-8")))
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
            out = pysetup.setup(dest, Stub())
        finally:
            os.replace = real
        assert out.startswith("FAIL\tinstall\t") and str(dest) in out and ".old-" in out, out


def test_engine_native_without_version():
    with env() as d:
        v = pdf_fix.fake_venv(d / "v")
        ready = json.loads((v / "ready.json").read_text(encoding="utf-8"))
        del ready["pdfium"]
        (v / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
        assert pyenv.engine(venv=v) == "native"


def test_read_py_does_not_shadow_stdlib():
    code = ("import sys; sys.path.insert(0, %r); import read; import queue; "
            "assert hasattr(queue, 'Queue'), queue.__file__; print('ok')" % str(HERE / "scripts"))
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True,
                       cwd=tempfile.gettempdir())
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_stale_outlasts_pip_timeout():
    from reader import pysetup
    assert pysetup.STALE_SECONDS > pysetup.PIP_TIMEOUT


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


def test_pdf_kind_default():
    with env() as d:
        assert pyenv.KINDS == ("pdf", "mpp")
        assert pyenv.venv_dir() == pyenv.venv_dir("pdf")
        assert pyenv.requirements_hash() == pyenv.requirements_hash("pdf")
        assert pyenv.declined() == pyenv.declined("pdf") is False
        assert pyenv.engine() == pyenv.engine("pdf") == "native"
        v = d / "v"
        pdf_fix.fake_venv(v)
        assert pyenv.state(v) == pyenv.state(v, "pdf") == "READY"
        assert pyenv.verify(v) == pyenv.verify(v, "pdf")
        assert pyenv.engine(venv=v) == pyenv.engine("pdf", v) == "pdfium-5.15.2-7000"
        assert pysetup.check(v) == pysetup.check(v, "pdf")


def test_pointer_and_marker_names():
    with env() as d:
        from reader import cache
        pysetup.decline()
        assert (cache.root() / "pdf-declined").is_file()
        assert pyenv.declined("pdf")
        dest = d / "dest"
        assert pysetup.setup(dest, Stub()).startswith("READY\t")
        assert json.loads((cache.root() / "pdf-venv.json").read_text(encoding="utf-8")) == {"venv": str(dest.resolve())}
        assert not (cache.root() / "pdf-declined").exists()


def test_not_venv_names_kind():
    with env() as d:
        plain = d / "plain"
        plain.mkdir()
        (plain / "x.txt").write_text("x", encoding="utf-8")
        assert pysetup.remove(plain, kind="pdf") == f"FAIL\tinstall\t{plain.resolve()} is not a pdf venv"


def test_unknown_kind_refused():
    for fn in (lambda: pyenv.venv_dir("zip"), lambda: pyenv.declined("zip"), lambda: pyenv.engine("zip")):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("an unknown kind was accepted")


# ---- kind "mpp": MPXJ and JPype in a venv, Java found on PATH (nothing is installed; java, pip and the smoke run are stubs)

JAVA_EXE = str(Path(tempfile.gettempdir()) / "fake-jdk" / "bin" / "java.exe")


@contextmanager
def which_java(path):
    """shutil.which("java") answers `path` (None: no java); every other name goes to the real which."""
    real = shutil.which
    shutil.which = lambda name, *a, **k: path if name == "java" else real(name, *a, **k)
    try:
        yield
    finally:
        shutil.which = real


def jdk(d, rel=("bin", "server", "jvm.dll")):
    home = Path(d) / "jdk"
    jvm = home.joinpath(*rel)
    jvm.parent.mkdir(parents=True, exist_ok=True)
    jvm.write_bytes(b"")
    return home


def java_run(home, version="21.0.5", bits=None):
    """A stub for subprocess.run: `java -XshowSettings:properties -version` prints its settings on stderr."""
    bits = pyenv.python_bits() if bits is None else bits
    err = (f"Property settings:\n    java.home = {home}\n    java.version = {version}\n"
           f"    sun.arch.data.model = {bits}\n    os.name = Windows 11\n\nopenjdk version \"{version}\"\n")

    def run(cmd, **kw):
        run.cmd, run.kw = cmd, kw
        return types.SimpleNamespace(returncode=0, stdout="", stderr=err)
    return run


def fake_mpp_venv(tmp):
    """A fake mpp venv: an interpreter file, pyvenv.cfg with an existing home, and one dist-info with a RECORD
    for each of mpxj and jpype1 (no ready.json: setup writes it)."""
    import base64
    import hashlib
    d = Path(tmp)
    py = pyenv.interpreter(d)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")
    (d / "pyvenv.cfg").write_text(f"home = {d.parent}\nversion = 3.12.0\n", encoding="utf-8")
    site = d / "Lib" / "site-packages"
    for name, ver in (("mpxj", "16.10.0"), ("JPype1", "1.7.1")):
        mod = site / name.lower() / "__init__.py"
        mod.parent.mkdir(parents=True, exist_ok=True)
        data = f"# {name}\n".encode()
        mod.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        info = site / f"{name}-{ver}.dist-info"
        info.mkdir(exist_ok=True)
        (info / "RECORD").write_bytes(
            f"{name.lower()}/__init__.py,sha256={digest},{len(data)}\n{name}-{ver}.dist-info/RECORD,,\n".encode())


class MppStub(Stub):
    def __init__(self, java, **kw):
        super().__init__(**kw)
        self.java_result = java

    def java(self):
        self.calls.append("java")
        return self.java_result

    def make_venv(self, tmp):
        self.calls.append("make_venv")
        fake_mpp_venv(tmp)

    def versions(self, python):
        self.calls.append("versions")
        return "16.10.0", "1.7.1", pyenv.record_digests(Path(python).parent.parent, "mpp")

    def smoke(self, python, java):
        self.calls.append("smoke")
        self.smoked_with = java
        return self.smoke_result


def mpp_info(d, **kw):
    """The dict java_info returns for a stub JDK under d."""
    home = jdk(d)
    with which_java(JAVA_EXE):
        return pyenv.java_info(run=java_run(home, **kw))


def mpp_ready(d):
    """A READY mpp venv at d/mpp-venv made by setup with stubs (call inside which_java(JAVA_EXE))."""
    dest = d / "mpp-venv"
    out = pysetup.setup(dest, MppStub(mpp_info(d)), kind="mpp")
    assert out.startswith("READY\t"), out
    return dest


def with_kind_pdf(venv):
    path = Path(venv) / "ready.json"
    ready = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**ready, "kind": "pdf"}), encoding="utf-8")


def test_mpp_requirements_pinned():
    text = pyenv.REQUIREMENTS["mpp"].read_text(encoding="utf-8")
    assert "mpxj==16.10.0" in text and "jpype1==1.7.1" in text
    assert "sha256:5cc44621b58c9d218afd26f4a4a6baa614b41d207a69f8f4ff16a8f6d6f2e5cd" in text
    blocks, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        if not ln.startswith(" "):
            cur = ln.split("==")[0]
            blocks[cur] = []
        blocks[cur].append(ln)
    assert set(blocks) == {"mpxj", "jpype1", "packaging"}, set(blocks)
    assert len(blocks["mpxj"]) == 2 and len(blocks["jpype1"]) == 45 and len(blocks["packaging"]) == 2
    assert re.fullmatch(r"packaging==\d+(\.\d+)+ \\", blocks["packaging"][0]), blocks["packaging"][0]
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", "\n".join(blocks["jpype1"]))) == 44
    for name, lines in blocks.items():
        assert all(ln.endswith("\\") for ln in lines[:-1]) and not lines[-1].endswith("\\"), name
        assert all(re.fullmatch(r"    --hash=sha256:[0-9a-f]{64}( \\)?", ln) for ln in lines[1:]), name


def test_java_major():
    assert pyenv.java_major("1.8.0_392") == 8
    assert pyenv.java_major("21.0.5") == 21
    assert pyenv.java_major("17") == 17
    assert pyenv.java_major("abc") is None
    assert pyenv.java_major("21-ea") == 21
    assert pyenv.JAVA_FLOOR == 8 and pyenv.MPXJ_VERSION == "16.10.0"


def test_java_info_parses():
    with env() as d:
        home = jdk(d)
        run = java_run(home)
        with which_java(JAVA_EXE):
            info = pyenv.java_info(run=run)
        assert info == {"path": JAVA_EXE, "major": 21, "home": str(home),
                        "jvm": str(home / "bin" / "server" / "jvm.dll"), "bits": pyenv.python_bits()}, info
        assert run.cmd == [JAVA_EXE, "-XshowSettings:properties", "-version"] and run.kw["timeout"] == 30
        home8 = jdk(d / "j8", ("jre", "lib", "server", "libjvm.so"))
        with which_java(JAVA_EXE):
            info = pyenv.java_info(run=java_run(home8, version="1.8.0_392"))
        assert info["major"] == 8 and info["jvm"] == str(home8 / "jre" / "lib" / "server" / "libjvm.so"), info


def test_java_too_old():
    with env() as d:
        with which_java(JAVA_EXE):
            got = pyenv.java_info(run=java_run(jdk(d), version="1.7.0_80"))
        assert got == "Java 7 found; MPXJ needs Java 8 or later", got


def test_java_no_jvm_library():
    with env() as d:
        home = Path(d) / "empty-jdk"
        home.mkdir()
        with which_java(JAVA_EXE):
            got = pyenv.java_info(run=java_run(home))
        assert got == f"no JVM library under {home}", got


def test_java_run_failures():
    def boom(cmd, **kw):
        raise OSError("nope")

    def late(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)
    with which_java(JAVA_EXE):
        for run in (boom, late):
            got = pyenv.java_info(run=run)
            assert isinstance(got, str) and got.startswith("java did not run"), got
        got = pyenv.java_info(run=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr="x"))
        assert isinstance(got, str) and "version" in got, got


class JavaStub(MppStub):
    """java() runs the real java_info against a stub JDK."""

    def __init__(self, home, **kw):
        super().__init__(None)
        self.home, self.kw = home, kw

    def java(self):
        self.calls.append("java")
        return pyenv.java_info(run=java_run(self.home, **self.kw))


def test_java_bits_mismatch_fails():
    with env() as d:
        py = pyenv.python_bits()
        other = 32 if py == 64 else 64
        with which_java(JAVA_EXE):
            got = pyenv.java_info(run=java_run(jdk(d), bits=other))
            assert got == f"{other}-bit Java with {py}-bit Python", got
            dest = d / "sub" / "mpp-venv"
            steps = JavaStub(jdk(d), bits=other)
            out = pysetup.setup(dest, steps, kind="mpp")
        assert out == f"FAIL\tjava\t{other}-bit Java with {py}-bit Python", out
        assert steps.calls == ["java"]
        assert not (d / "sub").exists()


def test_no_java_fails():
    with env() as d:
        with which_java(None):
            assert pyenv.java_info() == "java not found on PATH"
            dest = d / "sub" / "mpp-venv"
            steps = MppStub(pyenv.java_info())
            out = pysetup.setup(dest, steps, kind="mpp")
        assert out == "FAIL\tjava\tjava not found on PATH", out
        assert steps.calls == ["java"] and not (d / "sub").exists()


def test_mpp_setup_ready():
    with env() as d:
        from reader import cache
        decl = cache.root() / "mpp-declined"
        decl.write_text("", encoding="utf-8")
        home = jdk(d)
        steps = MppStub(mpp_info(d))
        dest = d / "mpp-venv"
        with which_java(JAVA_EXE):
            out = pysetup.setup(dest, steps, kind="mpp")
            assert out == f"READY\t{dest}\tmpxj-16.10.0-java21", out
            assert steps.calls == ["java", "make_venv", "pip", "versions", "smoke"], steps.calls
            assert steps.smoked_with["jvm"] == str(home / "bin" / "server" / "jvm.dll")
            ready = json.loads((dest / "ready.json").read_text(encoding="utf-8"))
            assert ready["kind"] == "mpp" and ready["mpxj"] == "16.10.0" and ready["jpype1"] == "1.7.1"
            assert ready["requirements"] == pyenv.requirements_hash("mpp")
            assert "pypdfium2" not in ready and set(ready["records"]) == {"mpxj", "jpype1"}
            assert ready["java"] == {"path": JAVA_EXE, "major": 21, "home": str(home),
                                     "jvm": str(home / "bin" / "server" / "jvm.dll"), "bits": pyenv.python_bits()}
            assert pyenv.state(dest, "mpp") == "READY" and pyenv.verify(dest, "mpp") is True
            assert pyenv.engine("mpp", dest) == "mpxj-16.10.0-java21"
            assert pysetup.check(dest, "mpp") == f"READY\t{dest}\tmpxj-16.10.0-java21"
            assert pyenv.mpp_engine_id("16.10.0", 21) == "mpxj-16.10.0-java21"
        assert json.loads((cache.root() / "mpp-venv.json").read_text(encoding="utf-8")) == {"venv": str(dest)}
        assert not decl.exists() and not pyenv.declined("mpp")
        assert leftovers(dest) == []


def test_mpp_without_java_not_ready():
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
        with which_java(None):
            assert pyenv.state(dest, "mpp") == "STALE" and pyenv.engine("mpp", dest) == ""
            assert pyenv.verify(dest, "mpp") is False
            assert pysetup.check(dest, "mpp") == f"STALE\t{dest}"


def test_java_gone_not_ready():
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
            assert pyenv.state(dest, "mpp") == "READY"
        with which_java(str(Path(tempfile.gettempdir()) / "other-jdk" / "bin" / "java.exe")):
            assert pyenv.state(dest, "mpp") == "STALE" and pyenv.engine("mpp", dest) == ""
            assert pyenv.verify(dest, "mpp") is False
        with which_java(JAVA_EXE):
            (Path(d) / "jdk" / "bin" / "server" / "jvm.dll").unlink()  # the JVM library moved away
            assert pyenv.state(dest, "mpp") == "STALE" and pyenv.engine("mpp", dest) == ""


def test_pins_changed_stale():
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
            old = pyenv.REQUIREMENTS["mpp"]
            changed = Path(d) / "requirements-mpp.txt"
            changed.write_text(old.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            pyenv.REQUIREMENTS["mpp"] = changed
            try:
                assert pyenv.state(dest, "mpp") == "STALE" and pyenv.engine("mpp", dest) == ""
            finally:
                pyenv.REQUIREMENTS["mpp"] = old
            assert pyenv.state(dest, "mpp") == "READY"


def test_mpp_verify_record_mismatch():
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
            rec = next(dest.rglob("JPype1-1.7.1.dist-info")) / "RECORD"
            rec.write_bytes(rec.read_bytes() + b"x/y.py,sha256=AAAA,1\n")
            assert pyenv.verify(dest, "mpp") is False
            assert pyenv.state(dest, "mpp") == "READY"  # the cheap check does not hash


def test_kind_mismatch_is_broken():
    with env() as d:
        pv = pdf_fix.fake_venv(d / "pv")
        assert pyenv.state(pv, "pdf") == "READY"
        assert pyenv.state(pv, "mpp") == "BROKEN"  # a ready.json with no kind is a pdf venv
        with which_java(JAVA_EXE):
            mv = mpp_ready(d)
        assert pyenv.state(mv, "pdf") == "BROKEN"


def test_mpp_decline_and_remove():
    with env() as d:
        from reader import cache
        assert pysetup.decline("mpp") == "DECLINED"
        assert (cache.root() / "mpp-declined").is_file()
        assert pyenv.declined("mpp") is True and pyenv.declined("pdf") is False
        pv = pdf_fix.fake_venv(d / "pv")
        with_kind_pdf(pv)
        assert pysetup.remove(pv, "mpp") == f"FAIL\tinstall\t{pv.resolve()} is not a mpp venv"
        assert pv.is_dir()
        with which_java(JAVA_EXE):
            mv = mpp_ready(d)
        assert pysetup.remove(mv, "pdf") == f"FAIL\tinstall\t{mv.resolve()} is not a pdf venv"
        assert pysetup.remove(mv, "mpp") == f"REMOVED\t{mv.resolve()}"
        assert not mv.exists() and not (cache.root() / "mpp-venv.json").exists()


def test_mpp_setup_refuses_pdf_venv():
    with env() as d:
        pv = pdf_fix.fake_venv(d / "pv")
        with_kind_pdf(pv)
        steps = MppStub(mpp_info(d))
        with which_java(JAVA_EXE):
            out = pysetup.setup(pv, steps, kind="mpp")
        assert out == f"FAIL\tinstall\t{pv.resolve()} is not a mpp venv", out
        assert (pv / "ready.json").is_file()


def test_mpp_venv_dir_order():
    with env() as d:
        os.environ["PHRO_READER_MPPENV"] = str(d / "X")
        os.environ["CLAUDE_PLUGIN_DATA"] = str(d / "D")
        assert pyenv.venv_dir("mpp") == d / "X"
        assert pyenv.venv_dir("pdf") == d / "D" / "pdf-venv"
        del os.environ["PHRO_READER_MPPENV"]
        assert pyenv.venv_dir("mpp") == d / "D" / "mpp-venv"
        del os.environ["CLAUDE_PLUGIN_DATA"]
        from reader import cache
        (cache.root() / "mpp-venv.json").write_text(json.dumps({"venv": str(d / "P")}), encoding="utf-8")
        assert pyenv.venv_dir("mpp") == d / "P"
        assert pyenv.venv_dir("pdf") != d / "P"


def test_mpp_off():
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
            assert pyenv.engine("mpp", dest) == "mpxj-16.10.0-java21"
            os.environ["PHRO_READER_MPPENV"] = "off"
            assert pyenv.forced_native("mpp") and not pyenv.forced_native("pdf") and not pyenv.forced_native()
            assert pyenv.engine("mpp", dest) == ""
            assert str(pyenv.venv_dir("mpp")) != "off"  # "off" is not a folder name


def test_mpp_engine_id_from_module():
    # the venv child reads mpxj's version from its metadata and the java major from <sys.prefix>/ready.json
    import importlib.metadata as md
    with env() as d:
        with which_java(JAVA_EXE):
            dest = mpp_ready(d)
        real, prefix = md.version, sys.prefix
        md.version = lambda name: "16.10.0" if name == "mpxj" else real(name)
        sys.prefix = str(dest)
        try:
            assert pyenv.engine_id_from_module("mpp") == "mpxj-16.10.0-java21"
        finally:
            sys.prefix, md.version = prefix, real


def test_prune_skips_mpp_declined():
    import inspect
    from reader import cache
    assert '"mpp-declined"' in inspect.getsource(cache.prune)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
