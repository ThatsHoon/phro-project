"""The OCR worker process and the conversion-side Session: frames and their caps, answers matched by id, a dead,
slow or malformed worker turning into "failed" or "late" tickets, the worker's whole process tree killed at the
deadline, the per-picture cache and its key, the worker's own job handling (stand-in engines, in process), and two
real reads of the Korean fixture through `read.py ocr-worker`.

Run: python -I -X utf8 tests/test_reader_ocr_worker.py
The real-worker tests skip unless Windows OCR with the `ko` recognizer is present.
"""
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
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


def inked(w: int, h: int = 4) -> Gray:
    """A white bitmap with one black pixel: a picture the session sends (one of a single value is `ocr.blank`)."""
    return Gray(w, h, bytes(1) + bytes([255]) * (w * h - 1))


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
        padded = json.dumps({"id": 1, "pad": "x" * 45}).encode()  # valid JSON, one byte over the cap
        assert len(padded) == 65
        for raw in (struct.pack(">I", 65) + padded, struct.pack(">I", 2**31)):
            try:
                ocr.read_frame(io.BytesIO(raw))
                raise AssertionError("head over cap read")
            except ValueError as e:
                assert "over the cap" in str(e), e
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
            ts = [s.submit(inked(w), "picture", 150.0) for w in (11, 12, 13)]
            assert len({t.id for t in ts}) == 3 and all(t.kind == "picture" and t.dpi == 150.0 for t in ts)
            t0 = time.monotonic()
            r = s.wait(ts[1])
            assert r.status == "done" and words(r) == ["w12"]
            assert time.monotonic() - t0 < 1.9, "the quick answer waited for the slow one"
            assert words(s.wait(ts[2])) == ["w13"] and words(s.wait(ts[0])) == ["w11"]
            assert all(s.wait(t).verdict.kind == "trusted" for t in ts)  # a picture is judged by its text alone
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
    with stub(delay=5):  # a worker that runs past its `left`
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + 1, BACKEND, cmd=STUB)
        try:
            t0 = time.monotonic()
            ts = [s.submit(gray(w), "page", 200.0) for w in (11, 12)]
            r = s.wait(ts[0])
            assert r.status == "late" and r.page is None, r.status
            assert time.monotonic() - t0 < limits.OCR_ANSWER_MARGIN + 2.0
            assert s.wait(ts[1]).status == "late" and s.late
            assert gone(s._proc), "the worker outlived the deadline"
            assert s.wait(s.submit(gray(13), "page", 200.0)).status == "late"
        finally:
            s.close()


def test_deadline_kills_grandchild():
    """The worker's tesseract children die with it: a grandchild the stand-in spawns is gone within 2 s of the kill."""
    pid = None
    with stub(child=True, delays=[0.0, 30.0]):
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + 2, BACKEND, cmd=STUB)
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
                t = s.submit(inked(11), "page", 200.0)
                assert s.wait(t).status == "done" and s.hint
            finally:
                s.close()
        assert store.get(t.key) is not None
        nowhere = [str(d / "no-such-worker.exe")]
        s2 = ocr.Session(time.monotonic() + 60, BACKEND, cmd=nowhere, store=store)
        try:
            t2 = s2.submit(inked(11), "page", 200.0)
            assert t2.key == t.key
            r = s2.wait(t2)
            assert r.status == "done" and words(r) == ["w11"] and r.verdict.kind == "trusted"
            assert r.unread is not None and r.hint and s2.hint
            assert s2._proc is None, "a cache hit started the worker"
            old = limits.OCR_UNREAD_NOCONF  # the verdict is judged again on load, with the threshold of the day
            limits.OCR_UNREAD_NOCONF = -1.0
            try:
                assert s2.wait(s2.submit(inked(11), "page", 200.0)).verdict.kind == "doubtful"
            finally:
                limits.OCR_UNREAD_NOCONF = old
            miss = s2.submit(inked(11), "picture", 200.0)  # the same pixels read as a picture are another entry
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


def test_submit_waits_while_queued_pixels_pass_the_budget():
    """The worker sleeps on its first page and the writer blocks on the second (past any pipe buffer), so pages pile up
    in the queue: past OCR_QUEUE_BYTES `submit` blocks, at most until the stop, and a page still blocked then is late
    without being queued. Under the budget the same submits return at once."""
    assert limits.OCR_QUEUE_BYTES == 64 * 2**20
    saved = limits.OCR_QUEUE_BYTES
    with stub(delay=30):
        try:
            for budget, blocks in ((1, True), (2**40, False)):
                limits.OCR_QUEUE_BYTES = budget
                s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + 3, BACKEND, cmd=STUB)
                try:
                    t0 = time.monotonic()
                    ts = [s.submit(gray(1000 + k, 300), "page", 200.0) for k in range(6)]
                    took = time.monotonic() - t0
                    if blocks:
                        assert took > 2.5, took
                        assert s._state == "over" and s.wait(ts[-1]).status == "late"
                    else:
                        assert took < 2.0, took
                        assert len(s._queue) >= 3, len(s._queue)
                finally:
                    s.close()
        finally:
            limits.OCR_QUEUE_BYTES = saved


def test_submit_over_budget_past_until_is_late_at_once():
    """Over OCR_QUEUE_BYTES, a submit waits only until its `until`: then its ticket is late at once, its pixels not
    queued, and the session reads on what it has."""
    saved = limits.OCR_QUEUE_BYTES
    limits.OCR_QUEUE_BYTES = 1
    try:
        with stub(delay=1):
            s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + 20, BACKEND, cmd=STUB)
            try:
                earlier, late = [], None
                for k in range(6):
                    queued, t0 = len(s._queue), time.monotonic()
                    t = s.submit(gray(1000 + k, 300), "page", 200.0, until=t0 + 0.5)
                    if s.peek(t) is not None:
                        late = (s.peek(t).status, time.monotonic() - t0, len(s._queue) - queued)
                        break
                    earlier.append(t)
                assert late is not None and late[0] == "late" and late[1] < 3.0 and late[2] == 0, late
                assert s._state == "running" and earlier, (s._state, earlier)
                assert [s.wait(t).status for t in earlier] == ["done"] * len(earlier)
            finally:
                s.close()
    finally:
        limits.OCR_QUEUE_BYTES = saved


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


def serve(inp, fake=None) -> tuple[dict, FakeWin]:
    from reader import ocr_worker
    fake = FakeWin() if fake is None else fake
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


# ---- fix round 1 -----------------------------------------------------------------------------------------------------

def test_cut_answer_is_done_but_not_cached():
    """I1 (a): an answer the worker flags as cut by the time is used, never cached."""
    with tmpdir() as d, stub(answer={"cut": True}):
        store = cache.OcrResults(d)
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB, store=store)
        try:
            t = s.submit(gray(12), "page", 200.0)
            assert s.wait(t).status == "done"
            assert store.get(t.key) is None, "a cut answer was cached"
        finally:
            s.close()


def test_answer_after_stop_is_not_cached():
    """I1 (b): an answer that arrives at or after the stop, while no wait is pending, is not filed in the store."""
    with tmpdir() as d, stub(delay=limits.OCR_ANSWER_MARGIN + 1.5):  # a worker that runs past its `left`
        store = cache.OcrResults(d)
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + 0.5, BACKEND, cmd=STUB,
                        store=store)
        try:
            t = s.submit(gray(11), "page", 200.0)
            t0 = time.monotonic()  # nobody waits: the reader thread files the answer, after the stop
            while t.id not in s._done and time.monotonic() - t0 < 15:
                time.sleep(0.05)
            assert t.id in s._done and time.monotonic() >= s._stop()
            assert store.get(t.key) is None, "an answer past the stop was cached"
            assert s.wait(t).status in ("done", "late")
        finally:
            s.close()


def test_worker_flags_cut_answers():
    """I1 (a), the worker side: a job whose deadline passed while it was read is answered with "cut": true."""
    g = block(40, 30, 0, 0, 10, 8)
    head = {"w": g.w, "h": g.h, "kind": "picture", "dpi": 200.0}
    got, _ = serve(frames(({"id": 1, **head, "left": 0.0}, g.data), ({"id": 2, **head, "left": 30.0}, g.data),
                          ({"id": 3, **head}, g.data)))  # I4: no `left` is no limit
    assert got[1]["ok"] and got[1].get("cut") is True, got[1]
    assert got[2]["ok"] and not got[2].get("cut"), got[2]
    assert got[3]["ok"] and not got[3].get("cut"), got[3]


def test_slow_page_answers_cut_before_stop():
    """M6: a job's `left` ends OCR_ANSWER_MARGIN before the session's stop, so a page the worker cannot finish comes
    back cut before the stop: done, used, not late and not cached."""
    with tmpdir() as d, stub(delay=30, honor_left=True):
        store = cache.OcrResults(d)
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN + 1.5, BACKEND, cmd=STUB,
                        store=store)
        try:
            t = s.submit(gray(12), "page", 200.0)
            r = s.wait(t)
            assert r.status == "done" and words(r) == ["w12"] and not s.late, (r.status, s.late)
            assert time.monotonic() < s._stop()
            assert store.get(t.key) is None, "a cut answer was cached"
        finally:
            s.close()


def test_page_queued_past_the_answer_margin_is_late():
    """M6: a job whose answer could no longer land before the stop is not sent: it is late at once."""
    with stub(delay=30, honor_left=True):
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + limits.OCR_ANSWER_MARGIN / 2, BACKEND, cmd=STUB)
        try:
            t0 = time.monotonic()
            r = s.wait(s.submit(gray(12), "page", 200.0))
            assert r.status == "late" and s.late and time.monotonic() - t0 < 0.5, (r.status, time.monotonic() - t0)
            assert time.monotonic() < s._stop()
        finally:
            s.close()


class SlowWin(FakeWin):
    """Reads `secs` a page, or until the deadline, and gives the page read so far either way."""

    def __init__(self, secs):
        super().__init__()
        self.secs = secs

    def recognize(self, g: Gray, deadline: float):
        time.sleep(max(0.0, min(self.secs, deadline - time.monotonic())))
        return ocr.OcrPage(g.w, g.h, [ocr.Word("가나다", 0, 0, 5, 5, 0, None)], 0.0, 0, self.engine, self.version)


def test_worker_deadline_runs_from_receipt():
    """N1: a job's `left` runs from when the worker receives its frame head, not from when a thread takes the job:
    with one thread, the job queued behind a slow one and the one whose head waits for room keep the same end, and
    all three come back cut by then."""
    g = block(40, 30, 0, 0, 10, 8)
    old = limits.OCR_WORKERS
    limits.OCR_WORKERS = 1
    try:
        reqs = [({"id": i, "w": g.w, "h": g.h, "kind": "picture", "dpi": 200.0, "left": 1.0}, g.data)
                for i in range(1, 4)]
        t0 = time.monotonic()
        got, _ = serve(frames(*reqs), SlowWin(30))
        took = time.monotonic() - t0
    finally:
        limits.OCR_WORKERS = old
    assert sorted(got) == [1, 2, 3] and all(a["ok"] and a.get("cut") is True for a in got.values()), got
    assert took < 1.8, f"queued jobs ran past their end: {took:.2f}s"


class NoneWin(FakeWin):
    def recognize(self, g: Gray, deadline: float):
        return None


def test_cut_job_without_page_is_late():
    """N5: a job cut by the time before any page is answered cut, not failed; the Session files it late, so the
    document is not filed. A job with time left whose engine gives no page still fails."""
    g = block(40, 30, 0, 0, 10, 8)
    head = {"w": g.w, "h": g.h, "kind": "page", "dpi": 200.0}
    got, _ = serve(frames(({"id": 1, **head, "left": 0.0}, g.data), ({"id": 2, **head, "left": 30.0}, g.data)),
                   NoneWin())
    assert got[1] == {"id": 1, "ok": False, "cut": True}, got[1]
    assert got[2]["ok"] and got[2]["page"] is None and not got[2].get("cut"), got[2]
    with stub(answer={"ok": False, "cut": True}):
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            r = s.wait(s.submit(gray(11), "page", 200.0))
            assert r.status == "late" and s.late, (r.status, s.late)
        finally:
            s.close()


class SkewWin(FakeWin):
    """An engine that deskews: its word box is in the deskewed frame, away from the ink, and `ink` holds where the
    word is in the page's pixels."""

    def recognize(self, g: Gray, deadline: float):
        dark = [i for i, v in enumerate(g.data) if v < 128]
        xs, ys = [i % g.w for i in dark], [i // g.w for i in dark]
        box = (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
        moved = (box[0], box[1] + 12, box[2], box[3])
        return ocr.OcrPage(g.w, g.h, [ocr.Word("가나다", *moved, 0, None)], -2.0, 0, self.engine, self.version,
                           ink=[box])


def test_worker_measures_unread_on_ink_boxes():
    """I1: the unread ink is measured with the page's `ink` boxes when the engine gives them; the layout boxes go out
    as the engine gave them."""
    g = block(60, 40, 10, 2, 40, 8)
    got, _ = serve(frames(({"id": 1, "kind": "page", "w": g.w, "h": g.h, "dpi": 200.0, "left": 30.0}, g.data)),
                   SkewWin())
    a = got[1]
    assert a["ok"] and a["unread"]["share"] == 0.0, a["unread"]
    assert a["page"]["words"][0]["y"] == 14 and a["page"]["ink"] == [[10, 2, 30, 6]], a["page"]
    r = ocr._result_of(a, "page")
    assert r.page.ink == [(10, 2, 30, 6)] and r.verdict.kind == "trusted", r


def test_oversized_number_answer_fails_at_once():
    """I2: a number too big for a float in an answer is the worker's death: "failed" at once, not "late"."""
    with stub(answer={"unread": {"share": 10 ** 400, "box": None, "area": 0.0}}):
        s = ocr.Session(time.monotonic() + limits.FIGURE_RESERVE + 4, BACKEND, cmd=STUB)
        try:
            t0 = time.monotonic()
            r = s.wait(s.submit(gray(11), "page", 200.0))
            assert r.status == "failed" and not s.late, (r.status, s.late)
            assert time.monotonic() - t0 < 2.5
        finally:
            s.close()


class OddStore:
    """A store whose entries do not read: `get` gives `value`, or raises it when it is an exception."""

    def __init__(self, value):
        self.value, self.puts = value, 0

    def get(self, key):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def put(self, key, result):
        self.puts += 1


def test_bad_cache_entry_is_a_miss():
    """I2: a cache entry that does not read, or a store that raises, is a miss; submit never raises."""
    huge = {"page": {"w": 11, "h": 4, "words": [], "angle": 0.0, "turn": 0, "engine": "e", "version": "v"},
            "rules": [], "unread": {"share": 10 ** 400, "box": None, "area": 0.0}, "hint": False}
    for value in (huge, RuntimeError("store fault")):
        with stub():
            store = OddStore(value)
            s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB, store=store)
            try:
                r = s.wait(s.submit(gray(11), "page", 200.0))
                assert r.status == "done" and words(r) == ["w11"], (value, r.status)
            finally:
                s.close()


def test_nan_dpi_costs_nothing():
    """I3: a page with a NaN dpi is read at OCR_DPI, and the pages around it are read too."""
    with stub():
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            ts = [s.submit(gray(11), "page", 200.0), s.submit(gray(12), "page", math.nan),
                  s.submit(gray(13), "page", 200.0), s.submit(gray(14), "page", -1.0),
                  s.submit(gray(15), "page", math.inf)]
            assert [s.wait(t).status for t in ts] == ["done"] * 5, [s.wait(t).status for t in ts]
            assert [t.dpi for t in ts[1:4:2]] == [float(limits.OCR_DPI)] * 2 and ts[4].dpi == float(limits.OCR_DPI)
        finally:
            s.close()


def test_no_deadline():
    """I4: deadline None or math.inf means no limit: jobs are read, nothing goes late."""
    for deadline in (None, math.inf):
        with stub(delay=0.3):
            s = ocr.Session(deadline, BACKEND, cmd=STUB)
            try:
                ts = [s.submit(gray(w), "page", 200.0) for w in (11, 12)]
                assert [s.wait(t).status for t in ts] == ["done", "done"], deadline
                assert not s.late
            finally:
                s.close()
    with ocr.opened(None, BACKEND, cmd=STUB) as s:
        assert s.wait(s.submit(inked(11), "picture", 200.0)).status == "done"


def test_code_hashed_on_first_key_only():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from reader import ocr; "
            "print('CODE' in vars(ocr), len(ocr.CODE), ocr.CODE == ocr._code())")
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(Path(__file__).resolve().parents[1] / "scripts")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.split() == ["False", "40", "True"], (r.stdout, r.stderr)


def test_key_follows_worker_limits_and_code():
    """I5: the key changes with the worker-side limits; ocr_worker.py is part of CODE."""
    assert "ocr_worker.py" in ocr._SOURCES and list(ocr._SOURCES) == sorted(ocr._SOURCES)
    g = gray(11)
    k = ocr.picture_key(g, "win-ko", "1", kind="page", dpi=200.0)
    for name, value in (("TESS_PSM_KOR", 6), ("OCR_JUNK", 0.5), ("OCR_LATIN", 0.5), ("TESS_PSM_LATIN", 6),
                        ("OCR_RULE", 0.5), ("OCR_RULE_THICK", 0.5), ("OCR_RULES_MAX", 7), ("OCR_DPI", 300),
                        ("OCR_PAPER", 5), ("OCR_LIGHT_CELLS", 9)):
        old = getattr(limits, name)
        setattr(limits, name, value)
        try:
            assert ocr.picture_key(g, "win-ko", "1", kind="page", dpi=200.0) != k, name
        finally:
            setattr(limits, name, old)
    assert ocr.picture_key(g, "win-ko", "1", kind="page", dpi=200.0) == k


class TypeErrorWin(FakeWin):
    """Every page it gives has a confidence JSON cannot write (TypeError in the answer)."""

    def recognize(self, g: Gray, deadline: float):
        return ocr.OcrPage(g.w, g.h, [ocr.Word("가나다", 0, 0, 2, 2, 0, 1j)], 0.0, 0, self.engine, self.version)


def test_worker_survives_unwritable_answers():
    """M2: answers that cannot be written are refused one by one; no thread dies and no slot is lost."""
    g = block(5, 5, 0, 0, 2, 2)
    old = limits.OCR_WORKERS
    limits.OCR_WORKERS = 1
    box = {}
    try:
        reqs = [({"id": i, "w": 5, "h": 5, "kind": "picture", "dpi": 200.0, "left": 30.0}, g.data) for i in range(6)]
        th = threading.Thread(target=lambda: box.update(got=serve(frames(*reqs), TypeErrorWin())[0]), daemon=True)
        th.start()
        th.join(15)
        assert not th.is_alive(), "the worker stalled after an answer it could not write"
    finally:
        limits.OCR_WORKERS = old
    assert box["got"] == {i: {"id": i, "ok": False} for i in range(6)}, box["got"]


def test_kill_after_reap_spares_the_group():
    """M3: once the worker is reaped, a later kill never signals its (possibly reused) process group."""
    if sys.platform == "win32":
        print("skip: test_kill_after_reap_spares_the_group (POSIX process groups)")
        return
    import signal
    with stub():
        s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
        try:
            s.wait(s.submit(gray(11), "page", 200.0))
            s._kill()
            assert s._proc.returncode is not None
            seen, orig = [], os.killpg
            os.killpg = lambda pid, sig: seen.append((pid, sig))
            try:
                s._kill()
            finally:
                os.killpg = orig
            assert not seen, seen
        finally:
            s.close()


def test_job_object_takes_the_popen_handle():
    """M3: on Windows the job object is given the worker's own process handle, not one reopened by pid."""
    if sys.platform != "win32":
        print("skip: test_job_object_takes_the_popen_handle (Windows job objects)")
        return
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job = ocr._job_for(p)
        assert job, "no job object for the Popen"
        ocr._job_kill(job)
        p.wait(3)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_close_with_inherited_pipes_is_bounded():
    """M5: with no job object, a grandchild that holds the worker's pipes must not make close() wait for it."""
    pid = None
    orig = ocr._job_for
    ocr._job_for = lambda *a: None
    try:
        with stub(child=True, child_secs=12, child_pipes=True):
            s = ocr.Session(time.monotonic() + 60, BACKEND, cmd=STUB)
            try:
                r = s.wait(s.submit(gray(11), "page", 200.0))
                pid = int(r.page.version.split(":")[1])
            finally:
                t0 = time.monotonic()
                s.close()
                took = time.monotonic() - t0
        assert took < 7, f"close took {took:.1f}s"
    finally:
        ocr._job_for = orig
        if pid is not None:
            end(pid)


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
