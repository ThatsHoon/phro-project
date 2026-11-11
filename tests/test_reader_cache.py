"""Conversion pipeline, content-hash cache and the read.py CLI.

Run: python -I -X utf8 tests/test_reader_cache.py
"""
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
import read  # noqa: E402
from reader import cache  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Doc, Para, Text  # noqa: E402

READ = HERE / "scripts" / "read.py"
STUB = "reader.formats._teststub"


def tmp():
    return Path(tempfile.mkdtemp())


@contextmanager
def stub(calls=None, fn=None):
    """Register the txtx stub format and a throwaway cache root; restore everything."""
    old_formats, old_built = dict(reader.FORMATS), set(reader.BUILT)
    old_mod, old_env = sys.modules.get(STUB), os.environ.get("PHRO_READER_CACHE")
    mod = types.ModuleType(STUB)

    def parse(path, ctx):
        if fn is not None:
            fn()
        if calls is not None:
            calls.append(path)
        return Doc(blocks=[Para([Text(Path(path).read_text())])])

    mod.parse = parse
    try:
        reader.FORMATS["txtx"] = "_teststub"
        reader.BUILT.add("txtx")
        sys.modules[STUB] = mod
        os.environ["PHRO_READER_CACHE"] = str(tmp())
        yield
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


def sample(text="hello"):
    p = tmp() / "a.txtx"
    p.write_text(text)
    return p


def test_convert_writes_out_meta():
    with stub():
        out = tmp() / "o"
        meta = reader.convert(sample(), out)
        assert meta["lines"] == 1 and meta["images"] == 0 and meta["warnings"] == []
        assert (out / "out.md").read_text() == "hello\n"
        assert (out / "meta.json").exists()
        assert meta["version"] == reader.VERSION and meta["hidden"] == 0


def test_unsupported_ext():
    try:
        reader.parse(Path("x.hwp"), Context())
    except ReadError as e:
        assert e.kind == "unsupported"
        return
    raise AssertionError("ReadError not raised")


def test_cache_hit_skips_convert():
    calls = []
    with stub(calls):
        p = sample()
        k = cache.key(p)
        assert cache.lookup(k) is None
        d = cache.store(k, p)
        assert cache.lookup(k) == d and len(calls) == 1
        assert cache.store(k, p) == d
        assert len(calls) == 1


def test_cache_key_follows_content():
    with stub():
        p = sample("one")
        k1 = cache.key(p)
        d1 = cache.store(k1, p)
        p.write_text("two")
        k2 = cache.key(p)
        assert k1 != k2
        d2 = cache.store(k2, p)
        assert d1 != d2 and (d2 / "out.md").read_text() == "two\n"


def test_cache_key_follows_code():
    import shutil
    p = sample()
    k = cache.key(p)
    old = cache.code_digest
    cache.code_digest = lambda: "0" * 40
    try:
        assert cache.key(p) != k
    finally:
        cache.code_digest = old
    assert cache.key(p) == k
    copy = tmp() / "reader"
    shutil.copytree(HERE / "scripts" / "reader", copy, ignore=shutil.ignore_patterns("__pycache__"))
    d1 = cache.code_digest(copy)
    assert d1 == cache.code_digest()
    f = copy / "markdown.py"
    f.write_bytes(f.read_bytes() + b"#")
    assert cache.code_digest(copy) != d1


def test_cache_concurrent_store():
    with stub():
        p = sample()
        k = cache.key(p)
        res, errs = [], []

        def work():
            try:
                res.append(cache.store(k, p))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs, errs
        assert len(set(res)) == 1
        assert (res[0] / "out.md").read_text() == "hello\n"
        bucket = cache.root() / k[:2]
        assert [x.name for x in bucket.iterdir()] == [k]


def test_cache_root_fallback():
    old = os.environ.get("PHRO_READER_CACHE")
    f = tmp() / "afile"
    f.write_text("x")
    try:
        os.environ["PHRO_READER_CACHE"] = str(f)
        r = cache.root()
        assert Path(tempfile.gettempdir()) in r.parents or r.parent == Path(tempfile.gettempdir())
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old


def test_cache_root_fallback_is_private():
    import shutil
    import stat
    old_env, old_tmp = os.environ.get("PHRO_READER_CACHE"), tempfile.gettempdir
    had_uid, old_uid = hasattr(os, "getuid"), getattr(os, "getuid", None)
    t = tmp()
    f = t / "afile"
    f.write_text("x")
    me = os.lstat(t).st_uid  # 0 on Windows
    posix = sys.platform != "win32"
    try:
        os.environ["PHRO_READER_CACHE"] = str(f)  # a file: the cache root cannot be made there
        tempfile.gettempdir = lambda: str(t)
        cache._private = None
        os.getuid = lambda: me
        mine = t / f"phro-project-reader-{me}"
        assert cache.root() == mine and mine.is_dir()
        if posix:
            assert stat.S_IMODE(os.stat(mine).st_mode) == 0o700
            os.chmod(mine, 0o777)
            assert cache.root() == mine and stat.S_IMODE(os.stat(mine).st_mode) == 0o700
        # a directory of that name owned by someone else: a fresh private directory, the same one all process long
        os.getuid = lambda: me + 1
        (t / f"phro-project-reader-{me + 1}").mkdir()
        r = cache.root()
        assert r.parent == t and r.name.startswith("phro-project-reader-") and r.name != f"phro-project-reader-{me + 1}"
        assert cache.root() == r
        if posix:
            assert stat.S_IMODE(os.stat(r).st_mode) == 0o700
        # a symlink in its place, where one can be made
        cache._private = None
        os.getuid = lambda: me
        shutil.rmtree(mine)
        (t / "elsewhere").mkdir()
        try:
            os.symlink(t / "elsewhere", mine, target_is_directory=True)
        except OSError:
            print("note: symlink case skipped (no symlink privilege)")
        else:
            r = cache.root()
            assert r != mine and r.parent == t, r
    finally:
        tempfile.gettempdir = old_tmp
        if had_uid:
            os.getuid = old_uid
        else:
            del os.getuid
        cache._private = None
        if old_env is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old_env


def test_prune():
    with stub():
        p = sample()
        k = cache.key(p)
        d = cache.store(k, p)
        old = time.time() - 40 * 86400
        os.utime(d / "meta.json", (old, old))
        assert cache.prune(30) == 1
        assert not d.exists()
        assert cache.prune(30) == 0


def run_cli(*args, env_extra=None):
    env = dict(os.environ)
    env["PHRO_READER_CACHE"] = str(tmp())
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(READ), *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return r


def test_cli_convert_and_fail():
    f = tmp() / "x.hwp"
    f.write_bytes(b"x")
    r = run_cli("convert", str(f))
    assert r.returncode == 1
    assert r.stdout.startswith("FAIL\tunsupported\t"), r.stdout
    g = tmp() / "x.pdf"
    g.write_bytes(b"x")
    r = run_cli("convert", str(g))
    assert r.returncode == 1 and r.stdout.startswith("FAIL\tunsupported\t")


def test_cli_formats():
    r = run_cli("formats")
    lines = r.stdout.splitlines()
    assert r.returncode == 0 and len(lines) == 29
    assert any(ln.startswith("hwpx\thwpx\t") for ln in lines)
    assert lines == sorted(lines)
    assert all(ln.split("\t")[2] in ("built", "unsupported") for ln in lines)


def test_cli_prune():
    r = run_cli("prune", "--days", "5")
    assert r.returncode == 0 and r.stdout.strip() == "PRUNED\t0"


def test_hidden_entry_is_separate():
    with stub():
        p = sample()
        k = cache.key(p)
        a = cache.store(k, p)
        b = cache.store(k + "-hidden", p, hidden=True)
        assert a != b and b.name == k + "-hidden" and b.parent == a.parent




def cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = read.main(list(args))
    return code, buf.getvalue()


def test_cli_nonascii_path_one_line():
    with stub():
        d = tmp() / "한글 폴더"
        d.mkdir()
        f = d / "보고서.txtx"
        f.write_text("hello")
        code, out = cli("convert", str(f))
        lines = out.splitlines()
        assert code == 0 and len(lines) == 1
        assert Path(lines[0]).name == "out.md" and Path(lines[0]).read_text() == "hello\n"


def test_cli_single_fail_line():
    def noisy():
        print("junk")
        raise ReadError("damaged", "line1\nline2\tx")

    with stub(fn=noisy):
        code, out = cli("convert", str(sample()))
        assert code == 1 and out == "FAIL\tdamaged\tline1 line2 x\n", repr(out)


def test_cli_other_exception():
    def boom():
        raise ValueError("bad\nthing")

    with stub(fn=boom):
        code, out = cli("convert", str(sample()))
        assert code == 1 and out == "FAIL\tdamaged\tinternal error ValueError\n", repr(out)


def test_cli_cache_hit_hidden_and_out():
    calls = []
    with stub(calls):
        p = sample()
        c1, o1 = cli("convert", str(p))
        c2, o2 = cli("convert", str(p))
        assert c1 == c2 == 0 and o1 == o2 and len(calls) == 1
        c3, o3 = cli("convert", str(p), "--hidden")
        assert c3 == 0 and Path(o3.strip()).parent.name.endswith("-hidden") and len(calls) == 2
        od = tmp() / "o"
        c4, o4 = cli("convert", str(p), "--out", str(od))
        assert c4 == 0 and Path(o4.strip()) == od / "out.md"
        assert (od / "out.md").read_text() == "hello\n"


def test_cli_key_from_the_gate():
    with stub():
        p = sample()
        code, o = cli("convert", str(p), "--key", "ab" * 20)
        assert code == 0 and Path(o.strip()).parent.name == "ab" * 20, o
        code, o = cli("convert", str(p), "--key", "../x")
        assert code == 0 and Path(o.strip()).parent.name == cache.key(p), o


def test_prune_stale_tmp():
    with stub():
        p = sample()
        k = cache.key(p)
        cache.store(k, p)
        bucket = cache.root() / k[:2]
        old, new = bucket / ".tmp-old", bucket / ".tmp-new"
        old.mkdir()
        new.mkdir()
        t = time.time() - 2 * 86400
        os.utime(old, (t, t))
        assert cache.prune(30) == 1
        assert not old.exists() and new.exists()


def test_precheck():
    try:
        reader.precheck(Path("x.hwp"))
    except ReadError as e:
        assert e.kind == "unsupported"
        return
    raise AssertionError("ReadError not raised")


def test_store_replaces_incomplete_target():
    with stub():
        p = sample()
        k = cache.key(p)
        t = cache.root() / k[:2] / k
        t.mkdir(parents=True)
        (t / "junk").write_text("x")
        d = cache.store(k, p)
        assert (d / "out.md").read_text() == "hello\n"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
