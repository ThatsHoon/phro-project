"""Conversion pipeline, content-hash cache and the read.py CLI.

Run: python -I -X utf8 tests/test_reader_cache.py
"""
import io
import json
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

os.environ["PHRO_READER_OCR"] = "none"  # decision 8: OCR off, keys as before, except in the OCR id tests
os.environ.setdefault("PHRO_READER_CACHE", tempfile.mkdtemp())

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


@contextmanager
def unbuilt(ext="mpp"):
    """ext out of reader.BUILT for a block, restored after: every in-scope extension is built, so the path of one
    that is not stays covered this way."""
    had = ext in reader.BUILT
    reader.BUILT.discard(ext)
    try:
        yield
    finally:
        if had:
            reader.BUILT.add(ext)


def test_unsupported_ext():
    assert set(reader.FORMATS) <= reader.BUILT, sorted(set(reader.FORMATS) - reader.BUILT)  # phase 4: all built
    with unbuilt():
        try:
            reader.parse(Path("x.mpp"), Context())
        except ReadError as e:
            assert e.kind == "unsupported"
            return
    raise AssertionError("ReadError not raised")


def test_line_tokens_cover_reads_count():
    """Counts Claude Code's Read gave, less 8 of its own: 30 single digits between bars, 60 tokens a line with the
    newline; a one-letter line, 2; 3,000 lines of base64 from random.Random(11), 231,244, which as random text must be
    estimated at READ_TOKENS / 25,000 of it at least. A line of spaces is charged its bytes / 12, so READ_TOKENS of
    lines stay under READ_BYTES too."""
    import base64
    import random
    from reader import limits
    grid = "|".join("526018159083016613186091390996") + "\n"
    assert cache.line_tokens(grid * 3) == cache.line_tokens(grid) * 3 and cache.line_tokens(grid)[0] >= 60
    assert cache.line_tokens("x") == cache.line_tokens("x\n") and cache.line_tokens("x")[0] >= 2
    r = random.Random(11)
    b64 = "".join(base64.b64encode(r.randbytes(60)).decode() + "\n" for _ in range(3000))
    assert sum(cache.line_tokens(b64)) >= 231_244 * limits.READ_TOKENS / 25_000, sum(cache.line_tokens(b64))
    assert cache.line_tokens(" " * 1199) == [100], cache.line_tokens(" " * 1199)


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


def run_cli(*args, env_extra=None, prefix=None):
    env = dict(os.environ)
    env["PHRO_READER_CACHE"] = str(tmp())
    env["PYTHONUTF8"] = "1"
    env["PHRO_READER_PYENV"] = "off"  # a pdf convert must not re-run in the user's own pdf venv
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", *(prefix or [str(READ)]), *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return r


# read.py's CLI with mpp out of reader.BUILT (see unbuilt)
UNBUILT_CLI = ["-c", "import sys; sys.path.insert(0, sys.argv[1]); import reader; reader.BUILT.discard('mpp'); "
               "import read; sys.exit(read.main(sys.argv[2:]))", str(HERE / "scripts")]


def test_cli_convert_and_fail():
    f = tmp() / "x.mpp"
    f.write_bytes(b"x")
    r = run_cli("convert", str(f), prefix=UNBUILT_CLI)
    assert r.returncode == 1
    assert r.stdout == "FAIL\tunsupported\tmpp\n", r.stdout


def test_cli_formats():
    r = run_cli("formats")
    lines = r.stdout.splitlines()
    assert r.returncode == 0 and len(lines) == 63
    for ext in ("wmf", "emf", "wmz", "emz", "tif", "tiff", "pct", "pict", "wpg"):
        assert f"{ext}	picture	built" in lines, ext
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
        from reader import raster
        assert code == 0 and Path(o.strip()).parent.name == cache.key(p, image=raster.backend()), o


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


def test_key_engine():
    p = sample()
    k0 = cache.key(p)
    kn = cache.key(p, "native")
    kp = cache.key(p, "pdfium-5.15.2-7000")
    assert kn != k0 and kp not in (k0, kn)
    assert cache.key(p, "") == k0


def test_cache_key_holds_image_backend():
    """The image backend id is in the key after the engine id; the CLI puts it in every key but pdf's."""
    from reader import raster
    from pdf_fix import text_ops
    p = sample()
    k0 = cache.key(p)
    kn = cache.key(p, image="none")
    kr = cache.key(p, image="resvg-0.5.0-pillow-12.3.0")
    kg = cache.key(p, image="gdiplus")
    assert len({k0, kn, kr, kg}) == 4, (k0, kn, kr, kg)
    assert cache.key(p, image="") == k0 and cache.key(p, "native", "") == cache.key(p, "native")
    assert cache.key(p, "none") != kn  # an engine id never meets an image id
    assert cache.key(p, "native", "none") not in (cache.key(p, "native"), kn)
    with stub():
        q = sample()
        code, o = cli("convert", str(q))
        assert code == 0 and Path(o.strip()).parent.name == cache.key(q, image=raster.backend()), o
    f = _pdf_file([text_ops("Hi")])
    r = run_cli("convert", str(f))  # a pdf: its key keeps the engine id alone
    assert r.returncode == 0 and Path(r.stdout.strip()).parent.name == cache.key(f, "native"), r.stdout


def test_time_cut_is_filed_partial():
    """Decision 4: a conversion whose figures or OCR the deadline cut is filed under its key with partial in its
    meta. A convert by hand of a partial entry converts again and puts the new result in its place, partial while
    the cut comes again and whole once it does not; a whole entry is returned as is. A whole result carries no
    partial mark."""
    from reader import limits, raster
    calls = []
    late = []

    def fn():
        calls.append(1)
    with stub(fn=fn):
        mod = sys.modules[STUB]
        plain = mod.parse
        mod.parse = lambda path, ctx: Doc(blocks=plain(path, ctx).blocks, warnings=list(late))
        for cut in (limits.FIGURES_LATE, limits.OCR_LATE):
            calls.clear()
            late[:] = [cut]
            p = sample(cut)
            c1, o1 = cli("convert", str(p))
            c2, o2 = cli("convert", str(p))
            k = cache.key(p, image=raster.backend())
            assert c1 == c2 == 0 and len(calls) == 2, (calls, o1, o2)
            assert Path(o1.strip()).parent.name == k == Path(o2.strip()).parent.name, (o1, k)
            meta = json.loads((Path(o1.strip()).parent / "meta.json").read_text(encoding="utf-8"))
            assert meta["partial"] is True and cut in meta["warnings"], meta
            late.clear()
            c3, o3 = cli("convert", str(p))
            c4, o4 = cli("convert", str(p))
            assert c3 == c4 == 0 and len(calls) == 3 and o3 == o4 == o1, (calls, o3, o4)
            meta = json.loads((Path(o3.strip()).parent / "meta.json").read_text(encoding="utf-8"))
            assert "partial" not in meta and cut not in meta["warnings"], meta
            assert [x.name for x in Path(o1.strip()).parent.parent.iterdir()] == [k], k
            late[:] = [cut]
        late.clear()
        q = sample("on time")
        c3, o3 = cli("convert", str(q))
        meta = json.loads((Path(o3.strip()).parent / "meta.json").read_text(encoding="utf-8"))
        assert c3 == 0 and "partial" not in meta, meta


def test_figure_cache_carries_a_cut_read_further():
    """A read the deadline cut keeps the figures it finished (PNG, lines, warnings) in the figure cache under the
    cache root. The cut document is filed partial; the next convert of it reads again, takes the finished figures
    from the figure cache and gets further, until the entry is whole; another read of the same pictures (the hidden
    variant here) takes them all from the figure cache."""
    from metafile_fix import wmf, wmf_text
    from reader import limits, raster
    from reader.model import Image
    png = b"\x89PNG\r\n\x1a\n" + bytes(16)
    pics = [wmf([wmf_text(0, 100, f"pic{i}".encode())]) for i in range(6)]
    calls, left = [], [0]

    def fake(kind, data, **kw):  # time for `left` figures a read; the others are cut, their text still read
        calls.append(data)
        text = [f"pic{pics.index(data)}"]
        if left[0] <= 0:
            return raster.Figure(None, text, {limits.FIGURES_LATE})
        left[0] -= 1
        return raster.Figure(png, text, {"figure text cut"})
    real = raster.convert
    raster.convert = fake
    try:
        with stub():
            sys.modules[STUB].parse = lambda path, ctx: Doc(blocks=[Image(b) for b in pics])
            p = sample()
            k = cache.key(p, image=read._image_id("_teststub"))
            done = []
            for n, args, name in ((6, (), k), (4, (), k), (2, (), k), (0, ("--hidden",), k + "-hidden")):
                calls.clear()
                left[0] = 2
                code, o = cli("convert", str(p), *args)
                entry = Path(o.strip()).parent
                meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
                md = (entry / "out.md").read_text(encoding="utf-8")
                assert code == 0 and len(calls) == n and entry.name == name, (len(calls), n, o)
                done.append(meta["figures"])
                assert all(f"Figure text: pic{i}" in md for i in range(6)), md
                assert "figure text cut" in meta["warnings"] and (meta.get("partial") is True) == (len(done) < 3), meta
            assert done == [2, 4, 6, 6], done
            assert (cache.root() / "figures").is_dir()
    finally:
        raster.convert = real


def test_child_key_is_the_gates_without_gdiplus():
    """On Windows the convert child keys a read with the gate's own backend id: GDI+ is not started for the key, so
    a child whose GdiplusStartup fails still files the read where the gate looks for it."""
    if sys.platform != "win32":
        print("skip: test_child_key_is_the_gates_without_gdiplus (needs Windows GDI+)")
        return
    from reader import gate, raster
    started = []

    def failing():  # GdiplusStartup failing in this child
        started.append(1)
        return None
    old = raster._gdip, raster._backend
    raster._gdip, raster._backend = failing, None
    try:
        with stub():
            p = sample()
            code, o = cli("convert", str(p))
            assert code == 0 and Path(o.strip()).parent.name == cache.key(p, image=gate.image_backend()), o
            assert started == [], "GDI+ started for the key"
    finally:
        raster._gdip, raster._backend = old


def test_figure_cache_misses_and_prunes():
    """The figure cache key follows the bytes, the code page, the page and the backend id; a stored figure that is
    damaged on disk is a miss; time-cut figures are never stored; prune clears figures not used for `days`."""
    from reader import limits, raster
    with stub():
        put = cache.Figures(cache.root() / "figures")
        k = put.key("ab" * 20, "cp949", 0, "gdiplus")
        assert len({k, put.key("ab" * 20, "", 0, "gdiplus"), put.key("ab" * 20, "cp949", 1, "gdiplus"),
                    put.key("ab" * 20, "cp949", 0, "none"), put.key("cd" * 20, "cp949", 0, "gdiplus")}) == 5
        png = b"\x89PNG\r\n\x1a\n" + bytes(8)
        put.put(k, raster.Figure(png, ["a", "b"], {"figure damaged"}))
        got = put.get(k)
        assert got.png == png and got.lines == ["a", "b"] and got.warnings == {"figure damaged"}, got
        late = put.key("ef" * 20, "", 0, "gdiplus")
        put.put(late, raster.Figure(None, ["x"], {limits.FIGURES_LATE}))
        assert put.get(late) is None
        f = put.path(k)
        f.write_bytes(f.read_bytes()[:-3])  # a truncated file (not picture bytes): a miss, not an error
        assert put.get(k) is None
        put.put(k, raster.Figure(None, ["only text"], set()))
        assert put.get(k).png is None and put.get(k).lines == ["only text"]
        old = time.time() - 40 * 86400
        os.utime(put.path(k), (old, old))
        assert cache.prune(30) >= 1 and not put.path(k).exists()


def test_key_unchanged_without_ocr():
    """An empty OCR id leaves the key as it was before OCR (these values are from 631d028, with the code digest and
    the version pinned), so no cached document is read again on a machine without OCR."""
    p = tmp() / "a.bin"
    p.write_bytes(b"phro ocr key")
    old = cache.code_digest, cache.VERSION
    cache.code_digest, cache.VERSION = (lambda base=None: "0" * 40), "0.0.0"
    try:
        assert cache.key(p) == cache.key(p, ocr="") == "510f96b67b6fa7fba40476f1e3906ea4a6320270"
        assert cache.key(p, "native", ocr="") == "0475b4c4c7d711147d956fa879a9a4f0c9c082ae"
        assert cache.key(p, image="gdiplus", ocr="") == "0c9520a463e8be09759952d774f4677af870afbe"
        assert cache.key(p, "native", "gdiplus", ocr="") == "2bcdcfd3c12362db84db77575e97a1429173ceaf"
    finally:
        cache.code_digest, cache.VERSION = old


def test_key_changes_with_ocr_version():
    p = sample()
    keys = {cache.key(p, image="gdiplus"), cache.key(p, image="gdiplus", ocr="win-ko:1:2/3:4"),
            cache.key(p, image="gdiplus", ocr="win-ko:1:2/3:5"), cache.key(p, image="gdiplus", ocr="win-en:1:2/3:4"),
            cache.key(p, image="gdiplus", ocr="win-ko+tess:1:2/3:4+5:6/7:8"),
            cache.key(p, image="gdiplus", ocr="win-ko+tess:1:2/3:4+5:6/7:9")}
    assert len(keys) == 6, keys
    assert cache.key(p, ocr="tess:1:2") not in (cache.key(p, "tess:1:2"), cache.key(p, image="tess:1:2"))
    assert cache.key(p, "native", ocr="tess:1:2") != cache.key(p, "native")


@contextmanager
def fake_tess():
    """OCR on, with a Tesseract found through PHRO_READER_TESSERACT (a file that is never run) and eng."""
    d = tmp()
    exe = d / "tesseract.exe"
    exe.write_bytes(b"not run")
    (d / "tessdata").mkdir()
    (d / "tessdata" / "eng.traineddata").write_bytes(b"eng")
    values = {"PHRO_READER_OCR": None, "PHRO_READER_TESSERACT": str(exe), "TESSDATA_PREFIX": str(d / "tessdata")}
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_pdf_key_ignores_image():
    """read.py gives the image backend to every key but pdf's (its venv child has no figure backend), and the gate's
    OCR backend id to every key, a Project file's too."""
    from pdf_fix import text_ops
    from reader import gate
    seen = []
    real = cache.key

    def spy(path, engine="", image="", ocr=""):
        seen.append((Path(path).suffix, engine, image, ocr))
        return real(path, engine, image, ocr)
    old = {k: os.environ.get(k) for k in ("PHRO_READER_PYENV",)}
    os.environ["PHRO_READER_PYENV"] = "off"
    cache.key = spy
    try:
        with stub(), fake_tess():
            oid = gate.ocr_backend()
            m = tmp() / "x.mpp"
            m.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600))
            cli("convert", str(m))
            cli("convert", str(_pdf_file([text_ops("Hi")])), "--engine", "native")
            cli("convert", str(sample()))
    finally:
        cache.key = real
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    from reader import raster
    assert "tess:" in oid and seen == [(".mpp", "", raster.backend(), oid), (".pdf", "native", "", oid),
                                       (".txtx", "", raster.backend(), oid)], (oid, seen)


def test_code_digest_json():
    base = tmp()
    (base / "a.py").write_text("x = 1\n")
    d0 = cache.code_digest(base)
    (base / "data.json").write_text("{}")
    d1 = cache.code_digest(base)
    assert d1 != d0
    (base / "data.json").write_text('{"a": 1}')
    assert cache.code_digest(base) != d1
    (base / "note.txt").write_text("x")
    d2 = cache.code_digest(base)
    (base / "note.txt").write_text("changed")
    assert cache.code_digest(base) == d2


def test_prune_markers():
    with stub():
        r = cache.root()
        for sub in ("offers", "failed", "pdf-declined"):
            (r / sub).mkdir()
        old = time.time() - 2 * 86400
        recent = time.time() - 3600
        files = {r / "offers" / "x": old, r / "failed" / "y": old, r / "offers" / "new": recent,
                 r / "failed" / "new": recent, r / "pdf-declined" / "z": old}
        for f, t in files.items():
            f.write_text("1")
            os.utime(f, (t, t))
        (r / "pdf-venv.json").write_text("{}")
        os.utime(r / "pdf-venv.json", (old, old))
        cache.prune(30)
        assert not (r / "offers" / "x").exists() and not (r / "failed" / "y").exists()
        assert (r / "offers" / "new").exists() and (r / "failed" / "new").exists()
        assert (r / "pdf-declined" / "z").exists() and (r / "pdf-venv.json").exists()


def _pdf_file(pages):
    from pdf_fix import simple_pdf
    f = tmp() / "x.pdf"
    f.write_bytes(simple_pdf(pages))
    return f


def test_cli_convert_pdf():
    import json
    from pdf_fix import text_ops
    f = _pdf_file([text_ops("Hi")])
    r = run_cli("convert", str(f))
    assert r.returncode == 0, r.stdout
    d = Path(r.stdout.strip()).parent
    text = (d / "out.md").read_text(encoding="utf-8")
    assert "## Page 1" in text and "Hi" in text
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["engine"] == "native" and meta["pages_total"] == 1, meta
    assert len(meta["pages"]) == 1 and meta["pages"][0][0] == 1, meta


def test_cli_pages_range():
    from pdf_fix import text_ops
    f = _pdf_file([text_ops(f"Body{i}") for i in range(1, 5)])
    r = run_cli("convert", str(f), "--pages", "2-3")
    assert r.returncode == 0, r.stdout
    d = Path(r.stdout.strip()).parent
    text = (d / "out.md").read_text(encoding="utf-8")
    assert "## Page 2" in text and "## Page 3" in text
    assert "## Page 1" not in text and "## Page 4" not in text
    assert d.name.endswith("-p2-3"), d.name
    for bad in ("0-2", "3-2", "x", "2"):
        assert run_cli("convert", str(f), "--pages", bad).returncode == 2, bad


def test_cli_pdfium_without_module():
    import json
    from pdf_fix import text_ops
    f = _pdf_file([text_ops("Hi")])
    r = run_cli("convert", str(f), "--engine", "pdfium")
    assert r.returncode == 0, r.stdout
    d = Path(r.stdout.strip()).parent
    assert d.name == cache.key(f, "native"), d.name
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["engine"] == "native", meta


def test_precheck():
    with unbuilt():
        try:
            reader.precheck(Path("x.mpp"))
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


def test_cut_ocr_page_files_the_document():
    """M6: a lone TIFF page whose OCR outlasts the session is answered cut before the stop: the page is read, the
    conversion is filed under its document key, and the cut answer stays out of the per-picture cache. A page that
    still goes late (a worker that runs past its `left`) files the conversion partial."""
    if sys.platform != "win32":
        print("skip: test_cut_ocr_page_files_the_document (needs GDI+)")
        return
    import ocr_fix
    from reader import limits, ocr, raster
    from test_read_cli import pages, tiff_file, words
    saved = ocr.WORKER_CMD
    ocr.WORKER_CMD = ocr_fix.STUB
    try:
        for honor in (True, False):
            with stub(), ocr_fix.stub_env(delay=30, honor_left=honor, answer=words("Alpha beta")):
                wall = time.time() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + 2
                os.environ[raster.DEADLINE_ENV] = repr(wall)
                try:
                    src = tiff_file(pages(1))
                    k = cache.key(src)
                    out = cache.store(k, src, backend=("win-ko", "test"))
                finally:
                    os.environ.pop(raster.DEADLINE_ENV, None)
                meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
                assert out.name == k and (meta.get("partial") is True) is not honor, (honor, out.name, meta)
                assert (meta["ocr"]["pages"] == 1) is honor, (honor, meta["ocr"])
                assert (limits.OCR_LATE in meta["warnings"]) is not honor, (honor, meta["warnings"])
                kept = [p for p in (cache.root() / cache.OCR).rglob("*") if p.is_file()]
                assert kept == [], kept
    finally:
        ocr.WORKER_CMD = saved


def _scan_store(n, conf, ahead):
    """`n` TIFF pages stored with the stand-in worker set by `conf`, under a deadline `ahead` seconds past the
    session's answer margin; (folder, key, meta, out.md)."""
    import ocr_fix
    from reader import limits, ocr, raster
    from test_read_cli import pages, tiff_file
    saved = ocr.WORKER_CMD
    ocr.WORKER_CMD = ocr_fix.STUB
    try:
        with stub(), ocr_fix.stub_env(**conf):
            wall = time.time() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + ahead
            os.environ[raster.DEADLINE_ENV] = repr(wall)
            try:
                src = tiff_file(pages(n))
                k = cache.key(src)
                out = cache.store(k, src, backend=("win-ko", "test"))
            finally:
                os.environ.pop(raster.DEADLINE_ENV, None)
            stop = wall - limits.FIGURE_RESERVE
            assert time.time() < stop, "the conversion ran past the session's stop"
            meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
            return out, k, meta, (out / "out.md").read_text(encoding="utf-8")
    finally:
        ocr.WORKER_CMD = saved


def test_queued_ocr_pages_answer_before_the_stop():
    """N1: on one worker thread, the pages queued behind a slow one keep the session's stop less the margin, from
    when the worker received them: each comes back done or cut before the stop, none late, and the conversion is
    filed under its document key."""
    if sys.platform != "win32":
        print("skip: test_queued_ocr_pages_answer_before_the_stop (needs GDI+)")
        return
    from reader import limits
    out, k, meta, text = _scan_store(3, {"serve": 30, "workers": 1}, 2)
    assert out.name == k, (out.name, meta["warnings"])
    assert meta["ocr"]["pages"] == 3 and meta["ocr"]["stopped_at"] is None, meta["ocr"]
    assert limits.OCR_LATE not in meta["warnings"], meta["warnings"]
    assert text.count("w600") == 3, text


def test_cut_ocr_page_without_text_is_late():
    """N5: a page cut by the time before any page was read is late, not failed: the stop note shows and the
    conversion is filed partial under its document key."""
    if sys.platform != "win32":
        print("skip: test_cut_ocr_page_without_text_is_late (needs GDI+)")
        return
    from reader import limits
    out, k, meta, text = _scan_store(1, {"answer": {"ok": False, "cut": True}}, 2)
    assert out.name == k and meta["partial"] is True and limits.OCR_LATE in meta["warnings"], (out.name, meta)
    assert meta["ocr"]["stopped_at"] == 1 and meta["ocr"]["failed"] == 0, meta["ocr"]
    assert "(OCR stopped at page 1 for the time; later scanned pages are shown as images)" in text, text


def _memos(base, name):
    d = base / name
    return sorted(f for f in d.rglob("*") if f.is_file()) if d.is_dir() else []


def test_code_digest_memo():
    """The code digest is kept in the cache root under the sources' (path, size, mtime) list: the same list takes
    the kept digest without reading a source; a source rewritten at the same size with a new mtime is read again."""
    old = os.environ.get("PHRO_READER_CACHE")
    root = tmp()
    os.environ["PHRO_READER_CACHE"] = str(root)
    try:
        src = tmp()
        (src / "a.py").write_text("x = 1\n")
        (src / "sub").mkdir()
        (src / "sub" / "b.json").write_text("{}")
        d0 = cache.code_digest(src)
        memos = _memos(root, "code")
        assert len(memos) == 1 and memos[0].read_text(encoding="ascii") == d0, memos
        memos[0].write_text("f" * 40, encoding="ascii")
        assert cache.code_digest(src) == "f" * 40
        f = src / "a.py"
        st = f.stat()
        f.write_text("x = 2\n")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        d1 = cache.code_digest(src)
        assert d1 not in (d0, "f" * 40), d1
        f.write_text("x = 1\n")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 2 * 10 ** 9))
        assert cache.code_digest(src) == d0
        (src / "c.py").write_text("")
        assert cache.code_digest(src) not in (d0, d1)
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_CACHE", None)
        else:
            os.environ["PHRO_READER_CACHE"] = old


def test_key_memo_by_stat():
    """A document whose (path, size, mtime) has a remembered key takes it without hashing the bytes; the key itself
    stays the content hash, so new bytes at the same size with a new mtime give a new key. prune clears old memos."""
    with stub():
        p = sample("one")
        k1 = cache.key(p)
        root = cache.root()
        assert len(_memos(root, "keys")) == 1
        st = os.stat(p)
        p.write_text("two")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert cache.key(p) == k1
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        k2 = cache.key(p)
        q = tmp() / "b.txtx"
        q.write_text("two")
        assert k2 != k1 and k2 == cache.key(q), (k1, k2)
        assert cache.key(p, "native") != k2
        p.write_text("one")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2 * 10 ** 9))
        assert cache.key(p) == k1
        memos = _memos(root, "keys") + _memos(root, "code")
        assert len(memos) >= 5, memos
        for f in memos[:2]:
            os.utime(f, (time.time() - 40 * 86400,) * 2)
        assert cache.prune(30) >= 2
        assert all(f.exists() == (i >= 2) for i, f in enumerate(memos)), memos


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
