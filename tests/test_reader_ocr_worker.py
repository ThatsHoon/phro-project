"""The OCR worker process and the conversion-side Session: frames and their caps, answers matched by id, a dead,
slow or malformed worker turning into "failed" or "late" tickets, the worker's whole process tree killed at the
deadline, the per-picture cache and its key, the worker's own job handling (stand-in engines, in process), and two
real reads of the Korean fixture through `read.py ocr-worker`.

Run: python -I -X utf8 tests/test_reader_ocr_worker.py
The real-worker tests skip unless Windows OCR with the `ko` recognizer is present.
"""
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import cache, images, limits, ocr  # noqa: E402
from reader.ocr_pixels import Gray, rotate  # noqa: E402
from ocr_fix import cer, load, truth  # noqa: E402

STUB = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve().parent / "ocr_stub_worker.py")]
BACKEND = ("stub", "1")
KO = "ko-malgun-11.150dpi"


@contextmanager
def stub(**conf):
    old = os.environ.get("PHRO_OCR_STUB")
    os.environ["PHRO_OCR_STUB"] = json.dumps(conf)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PHRO_OCR_STUB", None)
        else:
            os.environ["PHRO_OCR_STUB"] = old


@contextmanager
def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="ocr-worker-test-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def gray(w: int, h: int = 4, v: int = 255) -> Gray:
    return Gray(w, h, bytes([v]) * (w * h))


def words(r) -> list[str]:
    return [w.text for w in r.page.words]


def gone(proc) -> bool:
    return proc is None or proc.poll() is not None


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def end(pid: int) -> None:
    """Kills a leftover test process (never reached when the code under test works)."""
    if not alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


# ---- frames ----------------------------------------------------------------------------------------------------------

def test_frames_round_trip():
    f = io.BytesIO()
    req = {"id": 7, "w": 3, "h": 2, "kind": "page", "dpi": 200.0, "left": 12.5}
    ocr.write_frame(f, req, bytes(range(6)))
    res = {"id": 7, "ok": True, "page": None, "rules": [[1, 2, 3, 4]], "unread": None, "hint": False, "t": "가"}
    ocr.write_frame(f, res)
    raw = f.getvalue()
    n = struct.unpack(">I", raw[:4])[0]
    assert json.loads(raw[4:4 + n].decode("utf-8")) == req and raw[4 + n:4 + n + 6] == bytes(range(6))
    f.seek(0)
    assert ocr.read_frame(f) == (req, bytes(range(6)))
    assert ocr.read_frame(f) == (res, b"")
    assert ocr.read_frame(f) is None


def test_frame_over_cap_refused():
    old_json, old_px = limits.OCR_FRAME_JSON, images._MAX_PIXELS
    try:
        limits.OCR_FRAME_JSON = 64
        try:
            ocr.write_frame(io.BytesIO(), {"id": 1, "pad": "x" * 100})
            raise AssertionError("head over cap written")
        except ValueError:
            pass
        try:
            ocr.read_frame(io.BytesIO(struct.pack(">I", 65) + b"{" * 65))
            raise AssertionError("head over cap read")
        except ValueError:
            pass
        limits.OCR_FRAME_JSON = old_json
        images._MAX_PIXELS = 50
        try:
            ocr.write_frame(io.BytesIO(), {"id": 1, "w": 10, "h": 10}, bytes(100))
            raise AssertionError("pixels over cap written")
        except ValueError:
            pass
        head = json.dumps({"id": 1, "w": 10, "h": 10}).encode()
        try:
            ocr.read_frame(io.BytesIO(struct.pack(">I", len(head)) + head + bytes(100)))
            raise AssertionError("pixels over cap read")
        except ValueError:
            pass
        images._MAX_PIXELS = old_px
        try:  # the body must be w*h bytes
            ocr.write_frame(io.BytesIO(), {"id": 1, "w": 3, "h": 3}, bytes(8))
            raise AssertionError("short body written")
        except ValueError:
            pass
        for raw in (struct.pack(">I", len(head)) + head + bytes(99), struct.pack(">I", 10) + b"{}", b"\0\0"):
            try:  # a frame cut short
                ocr.read_frame(io.BytesIO(raw))
                raise AssertionError("short frame read")
            except ValueError:
                pass
        for bad in (b"[1]", b"\xff\xfe", b"{\"id\": 1, \"w\": 2.5, \"h\": 2}", b"{\"id\": 1, \"w\": -1, \"h\": 2}"):
            try:
                ocr.read_frame(io.BytesIO(struct.pack(">I", len(bad)) + bad + bytes(10)))
                raise AssertionError(f"bad head read: {bad!r}")
            except ValueError:
                pass
    finally:
        limits.OCR_FRAME_JSON, images._MAX_PIXELS = old_json, old_px


# ---- the session with the stand-in worker ----------------------------------------------------------------------------

def test_results_out_of_order_matched_by_id():
    with stub(delays=[4.0, 0.0, 2.0]):
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            ts = [s.submit(gray(w), "picture", 150.0) for w in (11, 12, 13)]
            assert len({t.id for t in ts}) == 3 and all(t.kind == "picture" and t.dpi == 150.0 for t in ts)
            t0 = time.monotonic()
            r = s.wait(ts[1])
            assert r.status == "done" and words(r) == ["w12"]
            assert time.monotonic() - t0 < 1.9, "the quick answer waited for the slow one"
            assert words(s.wait(ts[2])) == ["w13"] and words(s.wait(ts[0])) == ["w11"]
            assert all(s.wait(t).verdict is None for t in ts)  # pictures are not judged
            assert not s.late and not s.hint
            try:  # a ticket of another session is refused, and costs this one nothing
                s.wait(ocr.Ticket(999, "page", "0" * 40, 200.0))
                raise AssertionError("a foreign ticket was waited on")
            except KeyError:
                pass
            assert not s.late and not gone(s._proc)
        finally:
            s.close()


def test_worker_killed_mid_page_gives_failed():
    with stub(die_after=1):
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            ts = [s.submit(gray(w), "page", 200.0) for w in (11, 12, 13)]
            rs = [s.wait(t) for t in ts]
            assert [r.status for r in rs] == ["done", "failed", "failed"], [r.status for r in rs]
            assert words(rs[0]) == ["w11"] and rs[0].verdict.kind == "trusted"
            assert rs[1].page is None and rs[1].verdict is None and not s.late
            later = s.submit(gray(14), "page", 200.0)  # the worker is started once per conversion
            assert s.wait(later).status == "failed"
        finally:
            s.close()


def test_deadline_makes_late():
    with stub(delay=5):
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + 1, BACKEND, cmd=STUB)
        try:
            t0 = time.monotonic()
            ts = [s.submit(gray(w), "page", 200.0) for w in (11, 12)]
            r = s.wait(ts[0])
            assert r.status == "late" and r.page is None, r.status
            assert time.monotonic() - t0 < 2.0
            assert s.wait(ts[1]).status == "late" and s.late
            assert gone(s._proc), "the worker outlived the deadline"
            assert s.wait(s.submit(gray(13), "page", 200.0)).status == "late"
        finally:
            s.close()


def test_deadline_kills_grandchild():
    """The worker's tesseract children die with it: a grandchild the stand-in spawns is gone within 2 s of the kill."""
    pid = None
    with stub(child=True, delays=[0.0, 30.0]):
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + 2, BACKEND, cmd=STUB)
        try:
            first = s.submit(gray(11), "page", 200.0)
            slow = s.submit(gray(12), "page", 200.0)
            r = s.wait(first)
            assert r.status == "done", r.status
            pid = int(r.page.version.split(":")[1])
            assert alive(pid)
            assert s.wait(slow).status == "late"
            t0 = time.monotonic()
            while alive(pid) and time.monotonic() - t0 < 2.0:
                time.sleep(0.05)
            assert not alive(pid), "the grandchild outlived the worker"
            print(f"grandchild gone {time.monotonic() - t0:.2f}s after the deadline kill")
        finally:
            s.close()
            if pid is not None:
                end(pid)


def test_bad_response_frame_is_worker_death():
    with stub(bad_frame=True):
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            ts = [s.submit(gray(w), "page", 200.0) for w in (11, 12)]
            assert [s.wait(t).status for t in ts] == ["failed", "failed"]
            t0 = time.monotonic()
            while not gone(s._proc) and time.monotonic() - t0 < 2:
                time.sleep(0.05)
            assert gone(s._proc), "a worker that sent a bad frame is killed"
        finally:
            s.close()
    for answer in ({"id": 999}, {"page": {"w": "x"}}, {"unread": None}):  # unknown id, bad page, a page with no unread
        with stub(answer=answer):
            s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
            try:
                assert s.wait(s.submit(gray(11), "page", 200.0)).status == "failed", answer
            finally:
                s.close()


def test_ok_false_gives_failed_and_is_not_cached():
    with tmpdir() as d, stub(answer={"ok": False, "page": None}):
        store = cache.OcrResults(d)
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB, store=store)
        try:
            t = s.submit(gray(11), "page", 200.0)
            assert s.wait(t).status == "failed"
            assert store.get(t.key) is None
        finally:
            s.close()


def test_cache_hit_skips_worker():
    with tmpdir() as d:
        store = cache.OcrResults(d)
        with stub(answer={"hint": True}):
            s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB, store=store)
            try:
                t = s.submit(gray(11), "page", 200.0)
                assert s.wait(t).status == "done" and s.hint
            finally:
                s.close()
        assert store.get(t.key) is not None
        nowhere = [str(d / "no-such-worker.exe")]
        s2 = ocr.Session(time.monotonic() + 60, BACKEND, cmd=nowhere, store=store)
        try:
            t2 = s2.submit(gray(11), "page", 200.0)
            assert t2.key == t.key
            r = s2.wait(t2)
            assert r.status == "done" and words(r) == ["w11"] and r.verdict.kind == "trusted"
            assert r.unread is not None and r.hint and s2.hint
            assert s2._proc is None, "a cache hit started the worker"
            old = limits.OCR_UNREAD  # the verdict is judged again on load, with the threshold of the day
            limits.OCR_UNREAD = -1.0
            try:
                assert s2.wait(s2.submit(gray(11), "page", 200.0)).verdict.kind == "doubtful"
            finally:
                limits.OCR_UNREAD = old
            miss = s2.submit(gray(11), "picture", 200.0)  # the same pixels read as a picture are another entry
            assert miss.key != t.key and s2.wait(miss).status == "failed"  # the worker cannot start
        finally:
            s2.close()


def test_key_changes_with_engine_version():
    g = gray(11)
    k = ocr.picture_key(g, "win-ko", "10.0.1")
    assert len(k) == 40 and k == ocr.picture_key(gray(11), "win-ko", "10.0.1")
    assert k != ocr.picture_key(g, "win-ko", "10.0.2")
    assert k != ocr.picture_key(g, "win-en-us", "10.0.1")
    assert k != ocr.picture_key(g, "tess", "10.0.1")
    assert k != ocr.picture_key(gray(11, v=0), "win-ko", "10.0.1")
    assert k != ocr.picture_key(Gray(4, 11, g.data), "win-ko", "10.0.1")
    assert len(ocr.CODE) == 40


def test_spawn_flags_and_spawn_failure():
    seen = {}
    orig = subprocess.Popen

    def popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        raise OSError("no worker")

    subprocess.Popen = popen
    try:
        s = ocr.Session(time.monotonic() + 60, BACKEND)
        try:
            assert s.wait(s.submit(gray(11), "page", 200.0)).status == "failed"
        finally:
            s.close()
    finally:
        subprocess.Popen = orig
    assert seen["argv"] == ocr.WORKER_CMD
    assert ocr.WORKER_CMD[:4] == [sys.executable, "-I", "-X", "utf8"] and ocr.WORKER_CMD[5] == "ocr-worker"
    assert Path(ocr.WORKER_CMD[4]).name == "read.py" and Path(ocr.WORKER_CMD[4]).is_file()
    kw = seen["kw"]
    assert kw["stderr"] == subprocess.DEVNULL and kw["stdin"] == subprocess.PIPE and kw["stdout"] == subprocess.PIPE
    if sys.platform == "win32":
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert kw["start_new_session"] is True


def test_over_cap_picture_fails_without_worker():
    old = images._MAX_PIXELS
    images._MAX_PIXELS = 40
    try:
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            assert s.wait(s.submit(gray(11), "page", 200.0)).status == "failed"
            assert s._proc is None
        finally:
            s.close()
    finally:
        images._MAX_PIXELS = old


def test_close_ends_worker():
    with stub(delay=30):
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        t = s.submit(gray(11), "page", 200.0)
        t0 = time.monotonic()
        s.close()
        assert time.monotonic() - t0 < 3 and gone(s._proc)
        assert s.wait(t).status == "failed"


def test_opened_sets_session():
    assert ocr.session() is None
    with ocr.opened(time.monotonic() + 60, ("", "")) as s:
        assert s is None and ocr.session() is None
    with ocr.opened(time.monotonic() + 60, BACKEND) as s:
        assert isinstance(s, ocr.Session) and ocr.session() is s
    assert ocr.session() is None


# ---- the worker's own job handling, in process, with stand-in engines ------------------------------------------------

class FakeWin:
    """A Windows engine stand-in. A bitmap whose first pixel is 255 raises OSError. One whose top-left pixel is dark
    reads as junk (lone jamo); otherwise one clean word whose box is the bitmap's dark area."""
    engine, version = "win-ko", "1"

    def __init__(self):
        self.closed = False

    def recognize(self, g: Gray, deadline: float):
        assert not self.closed
        if g.data[0] == 255 and g.data[-1] == 255:
            raise OSError("engine fault")
        if g.data[0] < 128:
            return ocr.OcrPage(g.w, g.h, [ocr.Word("ㄱㄴㄷ", 0, 0, 5, 5, 0, None)], 0.0, 0, self.engine, self.version)
        dark = [i for i, v in enumerate(g.data) if v < 128]
        xs, ys = [i % g.w for i in dark], [i // g.w for i in dark]
        box = (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
        return ocr.OcrPage(g.w, g.h, [ocr.Word("가나다", *box, 0, None)], 0.0, 0, self.engine, self.version)

    def close(self):
        self.closed = True


def frames(*items) -> io.BytesIO:
    f = io.BytesIO()
    for it in items:
        if isinstance(it, bytes):
            f.write(it)
        else:
            ocr.write_frame(f, *it)
    f.seek(0)
    return f


def answers(out: io.BytesIO) -> dict:
    out.seek(0)
    got = {}
    while (fr := ocr.read_frame(out)) is not None:
        assert fr[1] == b""
        got[fr[0]["id"]] = fr[0]
    return got


def serve(inp) -> tuple[dict, FakeWin]:
    from reader import ocr_worker
    fake = FakeWin()
    orig = ocr.engines
    ocr.engines = lambda: ocr.Engines(fake, None, True)
    try:
        out = io.BytesIO()
        ocr_worker.serve(inp, out)
    finally:
        ocr.engines = orig
    return answers(out), fake


def block(w: int, h: int, x0: int, y0: int, x1: int, y1: int) -> Gray:
    px = bytearray(b"\xff" * (w * h))
    for y in range(y0, y1):
        px[y * w + x0:y * w + x1] = b"\0" * (x1 - x0)
    return Gray(w, h, bytes(px))


def test_worker_turned_frame_and_job_errors():
    g = block(40, 30, 0, 0, 10, 8)  # dark at the top left: junk at 0 degrees, clean turned by 180
    head = {"w": g.w, "h": g.h, "dpi": 200.0, "left": 30.0}
    got, fake = serve(frames(({"id": 1, "kind": "page", **head}, g.data),
                             ({"id": 2, "kind": "page", **head, "w": 5, "h": 2}, b"\xff" * 10),  # engine fault
                             ({"id": 3, "kind": "picture", **head}, g.data),
                             ({"id": 4, "kind": "nonsense", **head}, g.data)))
    assert fake.closed, "the worker closes its engine after the last job"
    assert set(got) == {1, 2, 3, 4}
    a = got[1]
    assert a["ok"] and a["page"]["turn"] == 180 and a["page"]["words"][0]["text"] == "가나다"
    assert (a["page"]["w"], a["page"]["h"]) == (40, 30)
    assert a["unread"]["share"] == 0.0, "unread was not computed in the page's turned frame"
    assert a["hint"] is False and isinstance(a["rules"], list)
    assert got[2] == {"id": 2, "ok": False} and got[4] == {"id": 4, "ok": False}
    p = got[3]
    assert p["ok"] and p["page"]["turn"] == 0 and p["rules"] == [] and p["unread"] is None


def test_worker_refuses_bad_frames():
    old = images._MAX_PIXELS
    images._MAX_PIXELS = 50
    try:
        big = json.dumps({"id": 5, "w": 10, "h": 10, "kind": "page", "dpi": 200.0, "left": 30.0}).encode()
        g = block(5, 5, 0, 0, 2, 2)
        got, fake = serve(frames(struct.pack(">I", len(big)) + big + bytes(100),
                                 ({"id": 6, "w": 5, "h": 5, "kind": "picture", "dpi": 200.0, "left": 30.0}, g.data)))
        assert got[5] == {"id": 5, "ok": False} and got[6]["ok"], got  # refused, and the next job still read
        assert fake.closed
        cut = json.dumps({"id": 7, "w": "x"}).encode()  # no body length: answered, then the worker exits
        got, _ = serve(frames(struct.pack(">I", len(cut)) + cut,
                              ({"id": 8, "w": 5, "h": 5, "kind": "picture", "dpi": 200.0, "left": 30.0}, g.data)))
        assert got == {7: {"id": 7, "ok": False}}, got
        got, fake = serve(frames(struct.pack(">I", limits.OCR_FRAME_JSON + 1) + b"{}"))  # no id: the worker exits
        assert got == {} and fake.closed
    finally:
        images._MAX_PIXELS = old


# ---- cache ----------------------------------------------------------------------------------------------------------

def test_ocr_results_store_and_prune():
    with tmpdir() as d:
        old = os.environ.get("PHRO_READER_CACHE")
        os.environ["PHRO_READER_CACHE"] = str(d)
        try:
            store = cache.OcrResults(cache.root() / cache.OCR)
            k = "ab" + "0" * 38
            value = {"page": None, "rules": [[1, 2, 3, 4]], "unread": None, "hint": False, "t": "가"}
            assert store.get(k) is None
            store.put(k, value)
            assert store.get(k) == value
            assert not [p for p in store.path(k).parent.iterdir() if p.name.startswith(".tmp-")]
            store.path(k).write_bytes(b"\xff not json")
            assert store.get(k) is None
            store.path(k).write_text("[1, 2]", encoding="utf-8")
            assert store.get(k) is None
            store.put(k, value)
            other = "cd" + "0" * 38
            store.put(other, value)
            past = time.time() - 40 * 86400
            os.utime(store.path(other), (past, past))
            assert cache.prune(30) >= 1
            assert store.get(k) == value and not store.path(other).exists()
        finally:
            if old is None:
                os.environ.pop("PHRO_READER_CACHE", None)
            else:
                os.environ["PHRO_READER_CACHE"] = old


# ---- the real worker -------------------------------------------------------------------------------------------------

_ko = []


def ko_backend():
    """("win-ko", version) when Windows OCR with the ko recognizer is here, else None."""
    if not _ko:
        e = ocr.engines()
        try:
            _ko.append((e.win.engine, e.win.version) if e.win is not None and e.win_korean else None)
        finally:
            if e.win is not None:
                e.win.close()
    return _ko[0]


def by_line(page) -> str:
    lines: dict[int, list[str]] = {}
    for w in page.words:
        lines.setdefault(w.line, []).append(w.text)
    return "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))


def test_real_worker_reads_fixture():
    backend = ko_backend()
    if backend is None:
        print("skip: test_real_worker_reads_fixture (needs Windows OCR ko)")
        return
    g = load(KO)
    s = ocr.Session(time.monotonic() + 120, backend)
    try:
        t0 = time.monotonic()
        r = s.wait(s.submit(g, "page", 150.0))
        first = time.monotonic() - t0
        assert r.status == "done", r.status
        c = cer(by_line(r.page), truth(KO))
        t1 = time.monotonic()
        ts = [s.submit(g, "page", 150.0) for _ in range(4)]
        rs = [s.wait(t) for t in ts]
        four = time.monotonic() - t1
        assert all(x.status == "done" for x in rs)
        print(f"real worker: CER {c:.4f}, verdict {r.verdict.kind}, first page (with start-up) {first:.2f}s, "
              f"4 pages at once {four:.2f}s")
        assert c <= 0.03, c
        assert r.verdict.kind == "trusted", (r.verdict, r.unread)
    finally:
        s.close()
    assert gone(s._proc)


def test_worker_reads_rotated_fixture():
    backend = ko_backend()
    if backend is None:
        print("skip: test_worker_reads_rotated_fixture (needs Windows OCR ko)")
        return
    s = ocr.Session(time.monotonic() + 120, backend)
    try:
        r = s.wait(s.submit(rotate(load(KO), 180), "page", 150.0))
        assert r.status == "done", r.status
        c = cer(by_line(r.page), truth(KO))
        print(f"rotated 180: turn {r.page.turn}, CER {c:.4f}, verdict {r.verdict.kind}")
        assert r.page.turn == 180 and c <= 0.03, (r.page.turn, c)
    finally:
        s.close()


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
