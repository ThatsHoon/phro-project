"""The OCR worker process: `read.py ocr-worker`, started by `ocr.Session` once per conversion.

It reads request frames on stdin (`ocr.read_frame`: a JSON head with the job's id, size, kind, dpi and the seconds
`left` to the conversion's deadline, then the Gray8 pixels) and answers each job on stdout when it finishes, so
answers come in any order. It holds no state between jobs (decision 12): the per-picture cache is the Session's.

An engine that faults natively kills this process alone; the Session turns its open tickets into "failed" ones and
the conversion carries on. Any exception in a job answers `{"id", "ok": false}` for that job only.
"""
import math
import sys
import threading
import time
from collections import deque
from dataclasses import asdict

from . import limits, ocr
from .ocr_pixels import Gray, rotate, rules, threshold, unread


def _job(head: dict, body: bytes, eng: ocr.Engines) -> dict:
    """One job's answer. Pages get their rules and unread ink in the page's turned frame; pictures get neither. An
    answer read while the job's deadline passed carries `"cut": true`: the rotation search or a re-read may have
    been cut short, so the Session never caches it. A head without `left` has no deadline."""
    jid, kind = head["id"], head["kind"]
    dpi = float(head["dpi"])
    left = head.get("left")
    if left is None:
        deadline = math.inf
    else:
        left = float(left)
        if not math.isfinite(left):
            raise ValueError(f"left {left!r}")
        # the conversion's own deadline: a cancelled Windows page keeps running natively, so the deadline is the
        # only bound, and the Session kills this process before it comes
        deadline = time.monotonic() + left
    g = Gray(head["w"], head["h"], body)
    page, hint = ocr.read_page(g, eng, deadline, kind)
    cut = time.monotonic() >= deadline
    if page is None:
        if cut:  # cut by the time, not a page without text: nothing to keep
            return {"id": jid, "ok": False}
        return {"id": jid, "ok": True, "page": None, "rules": [], "unread": None, "hint": hint}
    if kind != "page":
        res = {"id": jid, "ok": True, "page": asdict(page), "rules": [], "unread": None, "hint": hint}
    else:
        turned = rotate(g, page.turn)  # the frame the page's boxes are in
        thr = threshold(turned)
        found = rules(turned, dpi, thr)
        un = unread(turned, [(w.x, w.y, w.w, w.h) for w in page.words], found, thr)
        res = {"id": jid, "ok": True, "page": asdict(page), "rules": [list(b) for b in found],
               "unread": {"share": un.share, "box": None if un.box is None else list(un.box), "area": un.area},
               "hint": hint}
    if cut:
        res["cut"] = True
    return res


def _skip(inp, n: int) -> bool:
    """Reads past `n` bytes of `inp`, a MiB at a time; False at EOF first."""
    while n > 0:
        b = inp.read(min(n, 1 << 20))
        if not b:
            return False
        n -= len(b)
    return True


def serve(inp=None, out=None) -> None:
    """Answers jobs from `inp` (default sys.stdin.buffer) on `out` (default sys.stdout.buffer) until EOF on `inp`.

    `ocr.engines()` is built once; up to OCR_WORKERS jobs run at once in threads, the Windows ones sharing its
    `WinOcr`, the Tesseract ones each a process of its own. At most twice that many jobs are held: past them the
    next frame is not read, so the pipe and the Session's writer wait. A request whose head is over OCR_FRAME_JSON,
    or whose size is not two positive ints or is over images._MAX_PIXELS, is answered `ok: false` when its id can
    be read, and the worker goes on when the stream can be followed past it; otherwise it ends. At EOF it waits for
    the running jobs, then closes the Windows engine: never before every operation has ended."""
    inp = sys.stdin.buffer if inp is None else inp
    out = sys.stdout.buffer if out is None else out
    eng = ocr.engines()
    n = max(1, int(limits.OCR_WORKERS))
    out_lock = threading.Lock()
    cond = threading.Condition()
    jobs: deque = deque()  # (head, body); None ends a thread
    held = threading.BoundedSemaphore(2 * n)

    def send(head: dict) -> None:
        with out_lock:
            try:
                ocr.write_frame(out, head)
            except (ValueError, TypeError):  # over the frame cap, or not JSON: refused, answered as a failed job
                try:
                    ocr.write_frame(out, {"id": head.get("id"), "ok": False})
                except (OSError, ValueError, TypeError):
                    pass
            except OSError:  # the Session is gone; EOF on stdin ends this process
                pass

    def run() -> None:
        while True:
            with cond:
                while not jobs:
                    cond.wait()
                item = jobs.popleft()
            if item is None:
                return
            head, body = item
            try:
                res = _job(head, body, eng)
            except Exception:  # noqa: BLE001 - an engine's OSError or anything else: this job fails, not the worker
                res = {"id": head.get("id"), "ok": False}
            del body, item
            try:
                send(res)
            except Exception:  # noqa: BLE001 - nothing more can be said for this job; the thread and its slot live on
                pass
            finally:
                held.release()

    def give(item) -> None:
        with cond:
            jobs.append(item)
            cond.notify()

    threads = [threading.Thread(target=run, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    try:
        while True:
            held.acquire()
            try:
                fr = ocr.read_frame(inp)
            except ocr.FrameError as e:
                held.release()
                jid = e.head.get("id") if isinstance(e.head, dict) else None
                if type(jid) is not int:
                    break
                send({"id": jid, "ok": False})
                if e.skip is not None and _skip(inp, e.skip):
                    continue
                break
            except (OSError, ValueError):
                held.release()
                break
            if fr is None:
                held.release()
                break
            give(fr)
    finally:
        for _ in threads:
            give(None)
        for t in threads:
            t.join()
        if eng.win is not None:
            try:
                eng.win.close()
            except Exception:  # noqa: BLE001 - nothing is left running on it
                pass
