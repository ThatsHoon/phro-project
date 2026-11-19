"""The Tesseract adapter: the gray PNG the reader encodes, TSV parsing, the call it makes (stand-in `run`), the
deadline, executable lookup, and one real read of the English fixture.

Run: python -I -X utf8 tests/test_reader_ocr_tess.py
The engine test skips unless tesseract with `eng` is present.
"""
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import images, ocr_probe, ocr_tess  # noqa: E402
from reader.ocr_pixels import Gray  # noqa: E402
from ocr_fix import cer, load, truth  # noqa: E402

SIG = b"\x89PNG\r\n\x1a\n"


def chunks(png: bytes) -> dict:
    assert png[:8] == SIG
    out, i = {}, 8
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        out[png[i + 4:i + 8]] = png[i + 8:i + 8 + n]
        i += 12 + n
    return out


def test_gray_png_round_trip():
    g = Gray(3, 2, bytes([0, 10, 20, 30, 40, 255]))
    c = chunks(images.gray_png(g))
    w, h, depth, colour = struct.unpack(">IIBB", c[b"IHDR"][:10])
    assert (w, h, depth, colour) == (3, 2, 8, 0)
    assert zlib.decompress(c[b"IDAT"]) == b"\0" + g.data[:3] + b"\0" + g.data[3:]


TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
       "4\t1\t1\t1\t1\t0\t5\t5\t90\t10\t-1\t\n"
       "5\t1\t1\t1\t1\t1\t5\t5\t40\t10\t96.5\tHello\n"
       "5\t1\t1\t1\t1\t2\t50\t5\t45\t10\t91\tworld\n"
       "5\t1\t1\t1\t1\t3\t100\t5\t5\t10\t90\t \n"
       "5\t1\t1\t1\t2\t1\t5\t20\t30\t10\t80\tNext\n")


def test_parse_tsv_lines_and_conf():
    ws = ocr_tess.parse_tsv(TSV)
    assert [w.text for w in ws] == ["Hello", "world", "Next"]
    assert [w.line for w in ws] == [0, 0, 1]
    assert [w.conf for w in ws] == [96.5, 91.0, 80.0] and all(isinstance(w.conf, float) for w in ws)
    assert (ws[1].x, ws[1].y, ws[1].w, ws[1].h) == (50, 5, 45, 10)


def test_parse_tsv_keeps_rows_whole():
    # a form feed or NEL inside recognized text must not tear its row; CRLF row ends are taken as LF
    tsv = TSV.replace("\tworld\n", "\two\x0crld\n").replace("\tNext\n", "\tNe\x85xt\n").replace("\n", "\r\n")
    ws = ocr_tess.parse_tsv(tsv)
    assert [w.text for w in ws] == ["Hello", "wo\x0crld", "Ne\x85xt"], [w.text for w in ws]
    assert [w.line for w in ws] == [0, 0, 1] and ws[2].conf == 80.0


def test_tesseract_starts_without_a_window():
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=TSV.encode(), stderr=b"")

    orig = ocr_tess.find
    ocr_tess.find = lambda: Path(sys.executable)
    try:
        ocr_tess.recognize(Gray(2, 2, bytes(4)), "eng", 3, time.monotonic() + 30, run=run)
    finally:
        ocr_tess.find = orig
    if sys.platform == "win32":
        assert seen.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW, seen.get("creationflags")
    else:
        assert not seen.get("creationflags")


def test_no_deadline_runs_without_timeout():
    # a conversion with no time limit passes deadline=math.inf; subprocess cannot wait for an infinite timeout
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=TSV.encode(), stderr=b"")

    orig = ocr_tess.find
    ocr_tess.find = lambda: Path(sys.executable)
    try:
        page = ocr_tess.recognize(Gray(2, 2, bytes(4)), "eng", 3, float("inf"), run=run)
    finally:
        ocr_tess.find = orig
    assert page is not None and seen["timeout"] is None, seen.get("timeout")


def test_stdin_is_reader_png():
    g = Gray(4, 4, bytes(range(16)))
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout=TSV.encode(), stderr=b"")

    exe = Path(sys.executable)
    orig = ocr_tess.find
    ocr_tess.find = lambda: exe
    try:
        page = ocr_tess.recognize(g, "eng", 4, time.monotonic() + 30, run=run)
    finally:
        ocr_tess.find = orig
    data = seen["kw"]["input"]
    assert data[:8] == SIG
    assert chunks(data)[b"IHDR"] == chunks(images.gray_png(g))[b"IHDR"]
    argv = seen["argv"]
    assert "-c" in argv and argv[argv.index("-c") + 1] == "tessedit_create_tsv=1"
    assert argv[argv.index("--psm") + 1] == "4"
    assert seen["kw"]["env"]["OMP_THREAD_LIMIT"] == "1"
    assert page is not None and page.engine == "tess-eng" and (page.w, page.h) == (4, 4) and page.psm == 4
    assert page.angle == 0.0 and page.turn == 0 and len(page.words) == 3


def test_timeout_gives_none():
    g = Gray(2, 2, bytes(4))

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    orig = ocr_tess.find
    ocr_tess.find = lambda: Path(sys.executable)
    try:
        assert ocr_tess.recognize(g, "eng", 3, time.monotonic() + 5, run=run) is None
        called = []
        assert ocr_tess.recognize(g, "eng", 3, time.monotonic() - 1, run=lambda *a, **k: called.append(1)) is None
        assert not called
    finally:
        ocr_tess.find = orig


def test_find_uses_env():
    old = os.environ.get("PHRO_READER_TESSERACT")
    try:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tesseract.exe"
            f.write_bytes(b"")
            os.environ["PHRO_READER_TESSERACT"] = str(f)
            assert ocr_probe.find() == f
            os.environ["PHRO_READER_TESSERACT"] = str(Path(d) / "missing.exe")
            assert ocr_probe.find() is None
    finally:
        if old is None:
            os.environ.pop("PHRO_READER_TESSERACT", None)
        else:
            os.environ["PHRO_READER_TESSERACT"] = old


def _executable(p: Path) -> Path:
    p.write_bytes(b"")
    p.chmod(0o755)
    return p


def test_find_only_absolute_path_entries():
    """A stray Tesseract in the working folder, or on an empty, "." or relative PATH entry, is never found, even with
    NoDefaultCurrentDirectoryInExePath unset; on Windows only `tesseract.exe` counts, not a .bat or .cmd on PATH. A
    relative PHRO_READER_TESSERACT is no Tesseract either."""
    keys = ("PATH", "PHRO_READER_TESSERACT", "NoDefaultCurrentDirectoryInExePath")
    saved, cwd = {k: os.environ.get(k) for k in keys}, os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        work, on_path = Path(d) / "work", Path(d) / "bin"
        (work / "rel").mkdir(parents=True)
        on_path.mkdir()
        for folder in (work, work / "rel"):
            for name in ("tesseract.bat", "tesseract.cmd", "tesseract.exe", "tesseract"):
                _executable(folder / name)
        _executable(on_path / "tesseract.bat")
        _executable(on_path / "tesseract.cmd")
        try:
            os.chdir(work)
            for k in keys[1:]:
                os.environ.pop(k, None)
            os.environ["PATH"] = os.pathsep.join(["", ".", "rel", str(on_path)])
            assert ocr_probe.find() is None, ocr_probe.find()
            os.environ["PHRO_READER_TESSERACT"] = "tesseract.exe"
            assert ocr_probe.find() is None, ocr_probe.find()
            del os.environ["PHRO_READER_TESSERACT"]
            real = _executable(on_path / ("tesseract.exe" if sys.platform == "win32" else "tesseract"))
            got = ocr_probe.find()
            assert got is not None and got.is_absolute() and os.path.samefile(got, real), got
        finally:
            os.chdir(cwd)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_tessdata_found_off_windows():
    """Without TESSDATA_PREFIX: <bin>/tessdata, else <prefix>/share/tessdata, else the newest
    <prefix>/share/tesseract-ocr/<version>/tessdata that exists; None when none does. Found by listing only."""
    saved = os.environ.pop("TESSDATA_PREFIX", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            top = Path(d)
            exe = top / "bin" / "tesseract"
            exe.parent.mkdir()
            exe.write_bytes(b"")
            assert ocr_probe.tessdata(exe) is None
            versions = top / "share" / "tesseract-ocr"
            for v in ("4.00", "5", "notes"):
                (versions / v / "tessdata").mkdir(parents=True)
            (versions / "10").mkdir()  # newest, but holds no tessdata
            (versions / "5" / "tessdata" / "kor.traineddata").write_bytes(b"k")
            assert ocr_probe.tessdata(exe) == versions / "5" / "tessdata", ocr_probe.tessdata(exe)
            assert ocr_probe.langs(exe) == {"kor"}
            (top / "share" / "tessdata").mkdir()
            assert ocr_probe.tessdata(exe) == top / "share" / "tessdata"
            (top / "bin" / "tessdata").mkdir()
            assert ocr_probe.tessdata(exe) == top / "bin" / "tessdata"
            os.environ["TESSDATA_PREFIX"] = str(versions / "4.00" / "tessdata")
            assert ocr_probe.tessdata(exe) == versions / "4.00" / "tessdata"
    finally:
        if saved is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = saved


def test_reads_english_fixture():
    exe = ocr_probe.find()
    if exe is None or "eng" not in ocr_probe.langs(exe):
        print("skip: test_reads_english_fixture (needs tesseract)")
        return
    name = "en-times-11.150dpi"
    t = time.monotonic()
    page = ocr_tess.recognize(load(name), "eng", 3, t + 120)
    secs = time.monotonic() - t
    assert page is not None
    got = " ".join(w.text for w in page.words)
    c = cer(got, truth(name))
    print(f"en-times CER {c:.4f} in {secs:.2f}s, version {page.version}")
    assert c <= 0.01, c


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
