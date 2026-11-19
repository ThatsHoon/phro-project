"""OCR test fixtures: the synthetic spike pages in `tests/fixtures/ocr/` (see `make_fixtures.py` there).

A helper module, not a test file. It puts `scripts/` on sys.path itself, so a caller that loads it by file path can
import `reader`.

    load("ko-malgun-11.150dpi")   -> ocr_pixels.Gray
    truth(name), spike(name)      -> the printed text, and the spike's Windows OCR text (lines joined with "\\n")
    cer(got, want)                -> character error rate, whitespace removed, over the length of `want`
"""
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

DIR = Path(__file__).resolve().parent / "fixtures" / "ocr"
NAMES = ("ko-malgun-11.150dpi", "en-times-11.150dpi", "mix-table-malgun.150dpi")


def load(name: str):
    from reader.ocr_pixels import Gray  # lazy: the fixtures predate ocr_pixels
    raw = zlib.decompress((DIR / f"{name}.gray.z").read_bytes())
    w, h = struct.unpack(">II", raw[:8])
    return Gray(w, h, raw[8:])


def truth(name: str) -> str:
    return (DIR / f"{name}.txt").read_text(encoding="utf-8")


def spike(name: str) -> str:
    r = json.loads((DIR / f"{name}.win.json").read_text(encoding="utf-8"))
    return "\n".join(line["text"] for line in r["lines"])


def cer(got: str, want: str) -> float:
    """Edit distance between `want` and `got` with all whitespace removed, divided by the length of `want`."""
    ref, hyp = "".join(want.split()), "".join(got.split())
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)
