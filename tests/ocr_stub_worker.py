"""A stand-in OCR worker for tests: it speaks the worker's frame protocol and takes its behaviour from the
`PHRO_OCR_STUB` environment variable, a JSON object:

    delay       seconds to sleep before each answer (default 0)
    delays      a list of per-job delays, by arrival order; jobs then run in threads, so answers can come out of order
    die_after   exit (code 3) once this many jobs were answered, before answering any more
    answer      fields merged into every answer head (e.g. {"ok": false})
    child       spawn a sleeping grandchild at start and report its pid in each answer's page `version` as "child:<pid>"
    child_secs  how long that grandchild sleeps (default 30)
    child_pipes the grandchild inherits this worker's stdin and stdout pipes
    bad_frame   answer the first job with a frame whose head length is past OCR_FRAME_JSON
    honor_left  as the real worker: a job whose delay passes its `left` is answered when `left` runs out, flagged cut

A done answer is a page of one word, "w<W>", where W is the job's width, so a test can match answers to jobs.

A helper module, not a test file. Run as: python -I -X utf8 tests/ocr_stub_worker.py
"""
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import limits, ocr  # noqa: E402


def main():
    conf = json.loads(os.environ.get("PHRO_OCR_STUB") or "{}")
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    lock = threading.Lock()
    answered = [0]
    child = None
    if conf.get("child"):
        secs = float(conf.get("child_secs", 30))
        pipes = {"stdin": sys.stdin.buffer, "stdout": sys.stdout.buffer} if conf.get("child_pipes") else {}
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({secs})"], **pipes)

    def answer(head: dict, cut: bool = False) -> None:
        w = head["w"]
        page = {"w": w, "h": head["h"], "words": [{"text": f"w{w}", "x": 0, "y": 0, "w": w, "h": head["h"],
                                                     "line": 0, "conf": None}],
                "angle": 0.0, "turn": 0, "engine": "stub", "version": f"child:{child.pid}" if child else "1"}
        res = {"id": head["id"], "ok": True, "page": page, "rules": [],
               "unread": {"share": 0.0, "box": None, "area": 0.0} if head["kind"] == "page" else None,
               "hint": False}
        if cut:
            res["cut"] = True
        res.update(conf.get("answer") or {})
        with lock:
            n = conf.get("die_after")
            if n is not None and answered[0] >= n:
                os._exit(3)
            if conf.get("bad_frame"):
                out.write(struct.pack(">I", limits.OCR_FRAME_JSON + 1) + b"{}")
                out.flush()
            else:
                ocr.write_frame(out, res)
            answered[0] += 1
            if n is not None and answered[0] >= n:
                out.flush()
                os._exit(3)

    def job(head: dict, delay: float) -> None:
        left = head.get("left")
        cut = bool(conf.get("honor_left")) and left is not None and delay > left
        time.sleep(max(0.0, left) if cut else delay)
        answer(head, cut)

    delays = conf.get("delays")
    threads = []
    while True:
        fr = ocr.read_frame(inp)
        if fr is None:
            break
        head, _ = fr
        if delays is not None:
            k = len(threads)
            t = threading.Thread(target=job, args=(head, delays[k] if k < len(delays) else 0.0), daemon=True)
            t.start()
            threads.append(t)
        else:
            job(head, float(conf.get("delay", 0)))
    for t in threads:
        t.join()
    if child is not None:
        child.kill()


if __name__ == "__main__":
    main()
