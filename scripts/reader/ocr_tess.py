"""Tesseract as an OCR engine: found by `ocr_probe.find`, fed a PNG the reader encoded on stdin, read back as inline TSV."""
import math
import os
import subprocess
import sys
import time

from . import images
from .ocr import OcrPage, Word
from .ocr_probe import find, version


def parse_tsv(text: str) -> list[Word]:
    """Level-5 rows with non-blank text become words; `line` numbers the distinct (page, block, par, line) keys. Rows
    end at a line feed alone (a carriage return before it is dropped): `splitlines` would also cut at a form feed or
    NEL inside the recognized text."""
    rows = [r.rstrip("\r") for r in text.split("\n")]
    if not rows or not rows[0]:
        return []
    head = rows[0].split("\t")
    words: list[Word] = []
    key, line = None, -1
    for r in rows[1:]:
        f = dict(zip(head, r.split("\t")))
        if f.get("level") != "5" or not f.get("text", "").strip():
            continue
        k = (f.get("page_num"), f.get("block_num"), f.get("par_num"), f.get("line_num"))
        if k != key:
            key, line = k, line + 1
        try:
            words.append(Word(f["text"].strip(), int(f["left"]), int(f["top"]), int(f["width"]), int(f["height"]),
                              line, float(f["conf"])))
        except (KeyError, ValueError):
            continue
    return words


def recognize(g, langs: str, psm: int, deadline: float, *, run=subprocess.run) -> OcrPage | None:
    """Tesseract's reading of `g`, or None past the deadline, on a non-zero exit, on output that is not TSV, or
    when no executable is found."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    exe = find()
    if exe is None:
        return None
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"
    # the reader runs inside a hook: no console window may flash on Windows
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # no deadline (math.inf) is no timeout: subprocess cannot wait an infinite time
    timeout = max(0.0, deadline - time.monotonic()) if math.isfinite(deadline) else None
    try:
        p = run([str(exe), "-", "stdout", "-l", langs, "--psm", str(psm), "-c", "tessedit_create_tsv=1"],
                input=images.gray_png(g), capture_output=True, timeout=timeout,
                env=env, creationflags=flags)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:
        return None
    text = p.stdout.decode("utf-8", "replace")
    if not text.startswith("level\t"):
        return None
    return OcrPage(g.w, g.h, parse_tsv(text), 0.0, 0, "tess-" + langs, version(exe, langs.split("+")), psm=psm)
