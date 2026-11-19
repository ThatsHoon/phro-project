"""OCR results as the reader keeps them, whichever engine read the page; the engine chosen per page, the rotation
tried for an upside-down or sideways page, and the page's verdict. Then the worker's frames and the conversion-side
`Session` that sends pages to the worker process (`ocr_worker`), with the per-picture cache and the deadline.

The engines are reached only through `Engines`, so `ocr_win` and `ocr_tess` are imported inside `engines()` alone.
"""
import contextlib
import functools
import hashlib
import itertools
import json
import math
import os
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import images, limits
from .ocr_pixels import Gray, Unread, rotate


@dataclass
class Word:
    """One recognized word. The box is in pixels of the page's frame, y growing downward. `line` is the engine's
    line number; `conf` is the engine's confidence (0-100), None when the engine gives none."""
    text: str
    x: int
    y: int
    w: int
    h: int
    line: int
    conf: float | None


@dataclass
class OcrPage:
    """An engine's result for one bitmap. `turn` is the rotation the reader applied before the engine read it: 0,
    90, 180 or 270 degrees clockwise (`ocr_pixels.rotate`). `w`, `h` and every word box are in the frame of that
    turned bitmap, `rotate(g, turn)`, so `w` and `h` are swapped for 90 and 270. `angle` is the text angle the engine
    reported, `engine` and `version` the ids that key caches. An engine that deskews the bitmap before reading it
    gives its word boxes in the deskewed frame, which the layout keeps, and `ink`: the same boxes (x, y, w, h) in the
    bitmap's own pixels, one per word, which the unread ink is measured with. None when the word boxes are those.
    `psm` is the Tesseract page segmentation mode the page was read at, None for another engine."""
    w: int
    h: int
    words: list[Word]
    angle: float
    turn: int
    engine: str
    version: str
    ink: list[tuple[int, int, int, int]] | None = None
    psm: int | None = None


# ---- engines ---------------------------------------------------------------------------------------------------------

@dataclass
class Engines:
    """The engines on this machine. `win`: an object with `.engine`, `.version` and `.recognize(g, deadline)`, or
    None. `tess`: an object with `.langs` (frozenset) and `.recognize(g, langs, psm, deadline)`, or None.
    `win_korean`: the Windows engine reads Korean."""
    win: object | None
    tess: object | None
    win_korean: bool


class _Tess:
    """Tesseract as `Engines.tess`: the traineddata stems found next to the executable, and `ocr_tess.recognize`."""

    def __init__(self, langs: frozenset):
        self.langs = langs

    def recognize(self, g: Gray, langs: str, psm: int, deadline: float) -> OcrPage | None:
        from . import ocr_tess
        return ocr_tess.recognize(g, langs, psm, deadline)


def _win_load():
    """Windows OCR asking for Korean, when a recognizer folder exists under %WINDIR%\\OCR; else None."""
    from . import ocr_probe, ocr_win
    return ocr_win.WinOcr("ko") if ocr_probe.recognizers() else None


def _tess_load() -> _Tess | None:
    """Tesseract when it is found and has `kor` or `eng`; else None."""
    from . import ocr_probe
    exe = ocr_probe.find()
    if exe is None:
        return None
    langs = ocr_probe.langs(exe)
    return _Tess(langs) if langs & {"kor", "eng"} else None


def engines() -> Engines:
    """The real engines. The Windows engine is Korean when the engine it gives is (`win-ko`), which needs a `ko`
    recognizer. An engine that fails to load, for any reason, is None: OCR never makes a document fail. The caller
    owns `win` and closes it (`WinOcr.close()`) after the last page."""
    try:
        win = _win_load()
        korean = win is not None and (win.engine == "win-ko" or win.engine.startswith("win-ko-"))
    except Exception:  # noqa: BLE001 - a ctypes fault of any type means no Windows engine
        win, korean = None, False
    try:
        tess = _tess_load()
    except Exception:  # noqa: BLE001 - likewise for the Tesseract probe
        tess = None
    return Engines(win, tess, korean)


# ---- characters ------------------------------------------------------------------------------------------------------

# The punctuation `junk_share` accepts: the brief's list, plus `$ | ^ ° ₩ €` (prices, footnotes and degrees are
# common on clean pages), plus the full-width forms of the ASCII ones.
_ASCII_PUNCT = ".,:;!?'\"()[]{}<>-_/\\%&*+=#@~$|^"
PUNCT = _ASCII_PUNCT + "–—·…‘’“”「」『』《》〈〉○●□■△▲※°₩€" +"".join(chr(ord(c) + 0xFEE0) for c in _ASCII_PUNCT)
_PUNCT = frozenset(PUNCT)


def _hangul(c: str) -> bool:
    return "가" <= c <= "힣"


def _latin(c: str) -> bool:
    """A letter of the Latin script: ASCII, Latin-1, Latin Extended-A and -B, and Latin Extended Additional."""
    o = ord(c)
    return (o < 0x250 or 0x1E00 <= o <= 0x1EFF) and c.isalpha()


def _digit(c: str) -> bool:
    return "0" <= c <= "9"


def _good(c: str) -> bool:
    return _hangul(c) or _latin(c) or _digit(c)


def _chars(page: OcrPage) -> str:
    return "".join(w.text for w in page.words)


def latin_share(page: OcrPage) -> float:
    """Latin letters over all letters (0.0 with no letters)."""
    letters = [c for c in _chars(page) if c.isalpha()]
    return sum(map(_latin, letters)) / len(letters) if letters else 0.0


def junk_share(page: OcrPage) -> float:
    """Characters that are not Hangul syllables, Latin letters, digits, whitespace or `PUNCT`, over all non-space
    characters (0.0 with no text). Lone jamo, symbols and full-width letters are junk."""
    chars = [c for c in _chars(page) if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if not (_good(c) or c in _PUNCT)) / len(chars)


def _score(page: OcrPage) -> tuple[float, float, int]:
    """Decision 11: good characters (Hangul syllables, Latin letters, digits) times (1 - junk share). Ties go to the
    lower junk share, then to more words."""
    junk = junk_share(page)
    good = sum(1 for c in _chars(page) if _good(c))
    return good * (1 - junk), -junk, len(page.words)


# ---- reading a page --------------------------------------------------------------------------------------------------

_now = time.monotonic  # the clock the deadline is checked against; tests patch it here
_TURNS = (180, 90, 270)  # tried after 0 degrees, in this order (decision 11)


def _tess_read(tess, g: Gray, want: tuple[str, ...], psm: int | None, deadline: float) -> OcrPage | None:
    """Tesseract with the languages of `want` it has (in that order); `psm` None picks TESS_PSM_KOR with kor, else
    TESS_PSM_LATIN. None when it has none of them, when time is up, or when it gives no page."""
    langs = [lang for lang in want if lang in tess.langs]
    if not langs or _now() >= deadline:
        return None
    if psm is None:
        psm = limits.TESS_PSM_KOR if "kor" in langs else limits.TESS_PSM_LATIN
    return tess.recognize(g, "+".join(langs), psm, deadline)


def _latin_again(tess, g: Gray, page: OcrPage, deadline: float) -> OcrPage | None:
    """Tesseract eng's re-read at TESS_PSM_LATIN of a page whose letters are at least OCR_LATIN Latin, when it gives
    words; else None (a re-read with no words never replaces one with words)."""
    if tess is None or "eng" not in tess.langs or latin_share(page) < limits.OCR_LATIN:
        return None
    again = _tess_read(tess, g, ("eng",), limits.TESS_PSM_LATIN, deadline)
    return again if again is not None and again.words else None


def _tess_page(tess, g: Gray, deadline: float) -> OcrPage | None:
    """Tesseract as on a machine without Windows OCR: kor+eng (or the one of them it has), and a Latin page that kor
    read, read again by eng alone."""
    if tess is None:
        return None
    page = _tess_read(tess, g, ("kor", "eng"), None, deadline)
    if page is None or "kor" not in tess.langs:
        return page
    return _latin_again(tess, g, page, deadline) or page


def _read_once(g: Gray, eng: Engines, deadline: float) -> tuple[OcrPage | None, bool]:
    """One pass of the engine choice on `g` as it stands: (page, hint)."""
    tess = eng.tess
    if eng.win is None:
        return _tess_page(tess, g, deadline), False
    page = eng.win.recognize(g, deadline)
    if page is None:  # Windows gave no page: Tesseract as on a machine without Windows OCR, while time is left
        return _tess_page(tess, g, deadline), False
    again = _latin_again(tess, g, page, deadline)
    if again is not None:
        return again, False
    if not eng.win_korean and junk_share(page) > limits.OCR_JUNK:
        if tess is None or "kor" not in tess.langs:
            return page, True  # decision 13: a non-Korean Windows engine, junk, and no Tesseract kor
        again = _tess_read(tess, g, ("kor", "eng"), limits.TESS_PSM_KOR, deadline)
        if again is not None and again.words:
            return again, False
    return page, False


def read_page(g: Gray, eng: Engines, deadline: float, kind: str) -> tuple[OcrPage | None, bool]:
    """The page read by the engine chosen for it, and whether it calls for the Korean hint (decision 13).

    `kind` is "page" or "picture". A page whose 0-degree pass has a junk share over OCR_JUNK is read again turned
    clockwise by 180, 90 and 270 degrees, in that order, while time is left. The search stops at the first turn whose
    pass has words and a junk share of at most OCR_JUNK. The pass with the best `_score` is kept, with its `turn` set
    and its hint. A picture is read once (decision 11). `deadline` is a time.monotonic() value. (None, False) when no
    engine gives a page. An engine's OSError is not caught here: the worker catches it per job.

    The returned page's `w`, `h` and boxes are in the frame of `rotate(g, page.turn)`. The caller computes the page's
    `threshold`, `rules` and `Unread` on that turned bitmap, so `judge`'s `Verdict.box` is in the same frame. The
    picture shown is cut from the image as it is stored, not from the turned bitmap: the caller turns the box back
    first (`pdf._unturned`).
    """
    if kind not in ("page", "picture"):
        raise ValueError(f"kind {kind!r}")
    page, hint = _read_once(g, eng, deadline)
    if page is None:
        return None, False
    if kind == "picture" or junk_share(page) <= limits.OCR_JUNK:
        return page, hint
    best, best_hint, best_score = page, hint, _score(page)
    for turn in _TURNS:
        if _now() >= deadline:
            break
        p, h = _read_once(rotate(g, turn), eng, deadline)
        if p is None:
            continue
        p = replace(p, turn=turn)
        s = _score(p)
        if s > best_score:
            best, best_hint, best_score = p, h, s
        if p.words and junk_share(p) <= limits.OCR_JUNK:  # a clean turn ends the search; an empty one does not
            break
    return best, best_hint


# ---- the verdict -----------------------------------------------------------------------------------------------------

@dataclass
class Verdict:
    """`kind`: "trusted" (the text alone), "area" (the text, then a crop of `box`, an `Unread.box`) or "doubtful"
    (the text, then the page image)."""
    kind: str
    box: tuple[float, float, float, float] | None


def skew(page: OcrPage) -> float:
    """The slope of the page's lines in degrees, unsigned: the least-squares slope of the word box centres within
    each engine line, pooled over the lines, so a long line weighs more. 0.0 with no line of two words."""
    lines: dict[int, list[Word]] = {}
    for w in page.words:
        lines.setdefault(w.line, []).append(w)
    sxy = sxx = 0.0
    for ws in lines.values():
        if len(ws) < 2:
            continue
        xs = [w.x + w.w / 2 for w in ws]
        ys = [w.y + w.h / 2 for w in ws]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxy += sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sxx += sum((x - mx) ** 2 for x in xs)
    return abs(math.degrees(math.atan(sxy / sxx))) if sxx else 0.0


def judge(page: OcrPage, un: Unread) -> Verdict:
    """Doubtful when the junk share passes OCR_JUNK, the words' mean confidence (when they carry one) is under
    OCR_CONF, the share of those words under OCR_LOW_WORD passes OCR_LOW, the lines of a page read at TESS_PSM_KOR
    slope past OCR_SKEW degrees (that mode cuts skewed lines and mixes their order; Windows deskews and the eng read
    at TESS_PSM_LATIN keeps them), or there are no words while some ink is unread or the gray range passes OCR_FLAT
    (faint writing).

    Otherwise the unread ink decides. A picture is a box whose pixels are at least OCR_AREA_DENSITY unread ink:
    scattered misses span a large box with sparse ink. Past OCR_UNREAD unread, a compact picture (its box under
    FIGURE_WHOLE: a logo, a dark photo) is an area, and anything else (a noisy scan's ink over the whole page) is
    doubtful. Under it, a picture whose box covers at least OCR_AREA is an area, and the page is trusted otherwise.
    Words without a confidence (Windows OCR) have nothing else to doubt them by, and an engine that reads clean text
    leaves next to no ink: with no picture to hold it, the page is doubtful past the tighter OCR_UNREAD_NOCONF.

    `un` must be computed on `rotate(g, page.turn)`, the frame the page's boxes are in, so `Verdict.box` is a
    fractional box of that turned bitmap."""
    confs = [w.conf for w in page.words if w.conf is not None]
    doubtful = (
        junk_share(page) > limits.OCR_JUNK
        or (confs and sum(confs) / len(confs) < limits.OCR_CONF)
        or (confs and sum(1 for c in confs if c < limits.OCR_LOW_WORD) / len(confs) > limits.OCR_LOW)
        or (page.psm == limits.TESS_PSM_KOR and skew(page) > limits.OCR_SKEW)
        or (not page.words and (un.share > 0 or un.spread > limits.OCR_FLAT))
    )
    if doubtful:
        return Verdict("doubtful", None)
    picture = un.box is not None and un.density >= limits.OCR_AREA_DENSITY
    if un.share > (limits.OCR_UNREAD if confs or picture else limits.OCR_UNREAD_NOCONF):
        return Verdict("area", un.box) if picture and un.area < limits.FIGURE_WHOLE else Verdict("doubtful", None)
    if picture and un.area >= limits.OCR_AREA:
        return Verdict("area", un.box)
    return Verdict("trusted", None)


def picture_lines(page: OcrPage) -> list[str]:
    """The text lines of a picture for `Picture text:`, in reading order; empty under OCR_MIN_WORDS words or past
    OCR_JUNK, in the order of `ocr_layout.reading_lines`."""
    if len(page.words) < limits.OCR_MIN_WORDS or junk_share(page) > limits.OCR_JUNK:
        return []
    from .ocr_layout import reading_lines
    return reading_lines(page)


# ---- the per-picture key ---------------------------------------------------------------------------------------------

_SOURCES = ("ocr.py", "ocr_layout.py", "ocr_pixels.py", "ocr_probe.py", "ocr_tess.py", "ocr_win.py",
            "ocr_worker.py")  # sorted

# the limits that shape what the worker answers (engine choice, rotation search, rules); the verdict thresholds
# are not here, since `judge` runs again on every load
_KEY_LIMITS = ("OCR_JUNK", "OCR_LATIN", "TESS_PSM_LATIN", "TESS_PSM_KOR", "OCR_RULE", "OCR_RULE_THICK",
               "OCR_RULES_MAX", "OCR_DPI", "OCR_PAPER", "OCR_LIGHT_CELLS")


@functools.cache
def _code() -> str:
    """SHA-1 of the OCR modules' source bytes (CRLF read as LF), in sorted name order; a file not there is skipped.
    Computed on first use (`CODE`), not at import."""
    here = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for name in _SOURCES:
        try:
            data = (here / name).read_bytes()
        except OSError:
            continue
        h.update(name.encode() + b"\0" + data.replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def __getattr__(name):
    if name == "CODE":
        return _code()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def picture_key(g: Gray, engine: str, version: str, *, kind: str = "picture", dpi: float = 0.0) -> str:
    """The per-picture cache key: SHA-1 over the size and Gray8 pixels, the engine id and version, `CODE` (which
    covers the worker too), the reader's version, and the current values of `_KEY_LIMITS`, read at call time, so a
    calibrated limit never serves a reading made under the old one. A page (`kind` "page") is another entry than the
    same pixels read as a picture, and its dpi is hashed too: a page's result carries rules, found at that dpi, and
    its unread ink."""
    from . import VERSION
    h = hashlib.sha1(f"{g.w}x{g.h}\0".encode())
    h.update(g.data)
    h.update(b"\0")
    for part in (engine, version, _code(), VERSION):
        h.update(str(part).encode("utf-8", "replace") + b"\0")
    for name in _KEY_LIMITS:
        h.update(f"{name}={getattr(limits, name)!r}\0".encode())
    if kind != "picture":
        h.update(f"kind:{kind}\0dpi:{float(dpi)!r}\0".encode())
    return h.hexdigest()


# ---- the worker's frames ---------------------------------------------------------------------------------------------

class FrameError(ValueError):
    """A frame refused. `head`: its head when it was read and parsed, else None. `skip`: the body bytes that follow
    the head in the stream when the head gives a valid size over the pixel cap (the reader can skip them and go on);
    None when the stream cannot be followed further."""

    def __init__(self, msg: str, head: dict | None = None, skip: int | None = None):
        super().__init__(msg)
        self.head = head
        self.skip = skip


def _body_len(head: dict) -> int:
    """The body length a head declares: w*h for a head with `w` and `h` (a request), else 0 (a response)."""
    if "w" not in head and "h" not in head:
        return 0
    w, h = head.get("w"), head.get("h")
    if type(w) is not int or type(h) is not int or w <= 0 or h <= 0:
        raise FrameError(f"frame size {w!r}x{h!r}", head)
    if w * h > images._MAX_PIXELS:
        raise FrameError(f"frame of {w}x{h} pixels over the cap", head, w * h)
    return w * h


def _exact(f, n: int) -> bytes:
    """Up to `n` bytes of `f`, fewer only at EOF; read a MiB at a time, so a declared length is never allocated."""
    parts, left = [], n
    while left > 0:
        b = f.read(min(left, 1 << 20))
        if not b:
            break
        parts.append(b)
        left -= len(b)
    return b"".join(parts)


def _write_all(f, data) -> None:
    """All of `data` to `f`, also when `f` is a raw file whose write may take part of it."""
    mv = memoryview(data)
    while mv:
        n = f.write(mv)
        if n is None:  # a non-blocking file that took nothing
            raise OSError("frame write would block")
        mv = mv[n:]


def _frame_head(head: dict, body: bytes) -> bytes:
    """The length prefix and JSON head of a frame, checked against `body`. FrameError for a head over OCR_FRAME_JSON
    bytes or not JSON (NaN, infinity, a value JSON cannot write), or a body that is not the head's w*h bytes (none
    for a head without `w` and `h`) or is over images._MAX_PIXELS."""
    try:
        data = json.dumps(head, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as e:
        raise FrameError(f"frame head is not JSON: {e}") from None
    if len(data) > limits.OCR_FRAME_JSON:
        raise FrameError(f"frame head of {len(data)} bytes over the cap")
    if len(body) != _body_len(head):
        raise FrameError(f"frame body of {len(body)} bytes for {head.get('w')!r}x{head.get('h')!r}")
    return struct.pack(">I", len(data)) + data


def _send(f, prefix: bytes, body: bytes) -> None:
    _write_all(f, prefix)
    if body:
        _write_all(f, body)
    f.flush()


def write_frame(f, head: dict, body: bytes = b"") -> None:
    """One frame: `struct.pack(">I", len(head)) + head + body`, the head as UTF-8 JSON. FrameError (a ValueError),
    before anything is written, for a head or body `_frame_head` refuses."""
    _send(f, _frame_head(head, body), body)


def read_frame(f) -> tuple[dict, bytes] | None:
    """The next frame of `f` as (head, body), or None at EOF before a frame: `read_head`, then `read_body`."""
    head = read_head(f)
    return None if head is None else (head, read_body(f, head))


def read_head(f) -> dict | None:
    """The head of the next frame of `f`, its body not read, or None at EOF before a frame. FrameError (a
    ValueError) for a head length over OCR_FRAME_JSON, a head that is not a UTF-8 JSON object, a size that is not two
    positive ints or is over images._MAX_PIXELS, or a head cut short."""
    raw = _exact(f, 4)
    if not raw:
        return None
    if len(raw) < 4:
        raise FrameError("frame length cut short")
    (n,) = struct.unpack(">I", raw)
    if n > limits.OCR_FRAME_JSON:
        raise FrameError(f"frame head of {n} bytes over the cap")
    data = _exact(f, n)
    if len(data) < n:
        raise FrameError("frame head cut short")
    try:
        head = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FrameError("frame head is not JSON") from None
    if not isinstance(head, dict):
        raise FrameError("frame head is not an object")
    _body_len(head)
    return head


def read_body(f, head: dict) -> bytes:
    """The body of the frame whose head `read_head` just gave. FrameError for a body cut short."""
    size = _body_len(head)
    body = _exact(f, size)
    if len(body) < size:
        raise FrameError("frame body cut short", head)
    return body


# ---- results ---------------------------------------------------------------------------------------------------------

@dataclass
class Ticket:
    """One picture or page sent to OCR. `key` is its per-picture cache key."""
    id: int
    kind: str
    key: str
    dpi: float


@dataclass
class Result:
    """A ticket's outcome. `status`: "done" (`page` is set; for a page, `unread` and `verdict` too), "failed" (the
    worker died, could not start, refused the job, or no engine gave a page) or "late" (the deadline came first).
    `rules` are pixel boxes (x0, y0, x1, y1) and `unread` the unread ink, both in the frame of
    `rotate(g, page.turn)`; a picture has no rules and no unread. `hint`: it calls for the Korean hint (decision 13)."""
    status: str
    page: OcrPage | None = None
    rules: list = field(default_factory=list)
    unread: Unread | None = None
    verdict: Verdict | None = None
    hint: bool = False


_FIELDS = ("page", "rules", "unread", "hint")  # the part of an answer the per-picture cache keeps


def _int(v) -> int:
    if type(v) is not int:
        raise ValueError(f"not an int: {v!r}")
    return v


def _num(v) -> float:
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError(f"not a number: {v!r}")
    return float(v)


def _str(v) -> str:
    if not isinstance(v, str):
        raise ValueError(f"not a string: {v!r}")
    return v


def _box_of(b, num) -> tuple:
    if not isinstance(b, list) or len(b) != 4:
        raise ValueError("box")
    return tuple(num(v) for v in b)


def _page_of(d) -> OcrPage:
    if not isinstance(d, dict) or not isinstance(d["words"], list):
        raise ValueError("page is not an object with words")
    words = [Word(_str(x["text"]), _int(x["x"]), _int(x["y"]), _int(x["w"]), _int(x["h"]), _int(x["line"]),
                  None if x["conf"] is None else _num(x["conf"])) for x in d["words"]]
    turn = _int(d["turn"])
    if turn not in (0, 90, 180, 270):
        raise ValueError(f"turn {turn}")
    ink = d.get("ink")
    if ink is not None:
        if not isinstance(ink, list) or len(ink) != len(words):
            raise ValueError("ink boxes")
        ink = [_box_of(b, _int) for b in ink]
    psm = d.get("psm")
    return OcrPage(_int(d["w"]), _int(d["h"]), words, _num(d["angle"]), turn, _str(d["engine"]), _str(d["version"]),
                   ink, None if psm is None else _int(psm))


def _unread_of(d) -> Unread | None:
    if d is None:
        return None
    box = None if d["box"] is None else _box_of(d["box"], _num)
    return Unread(_num(d["share"]), box, _num(d["area"]), _num(d.get("density", 0.0)), _int(d.get("spread", 0)))


def _result_of(d: dict, kind: str) -> Result:
    """The Result of an answer's fields (or a cached value of them) for a ticket of `kind`; a page is judged here,
    with today's thresholds. ValueError, TypeError, KeyError or AttributeError for fields that do not read."""
    hint = d["hint"]
    if type(hint) is not bool or not isinstance(d["rules"], list):
        raise ValueError("answer fields")
    if d["page"] is None:
        return Result("failed", hint=hint)
    page = _page_of(d["page"])
    rules = []
    for b in d["rules"]:
        if not isinstance(b, list) or len(b) != 4:
            raise ValueError("rule box")
        rules.append(tuple(_int(v) for v in b))
    un = _unread_of(d["unread"])
    if kind == "picture":
        return Result("done", page, rules, un, None, hint)
    if un is None:
        raise ValueError("a page answer without unread ink")
    return Result("done", page, rules, un, judge(page, un), hint)


_BAD = (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError)


# ---- killing the worker's whole tree ---------------------------------------------------------------------------------

def _k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return ctypes, wintypes, k32


def _job_for(proc) -> int | None:
    """A Windows Job object holding the `subprocess.Popen` `proc`, set to kill every process in it when its handle
    closes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), so the worker's tesseract children die with it. The Popen's own
    process handle is used (no pid to reopen, so no pid-reuse window); a process is opened by pid only when the
    Popen has none. None when any call fails: the worker alone is killed then."""
    try:
        ctypes, wintypes, k32 = _k32()

        class Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        own = None
        try:
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # Extended = 9
                raise OSError("SetInformationJobObject")
            handle = getattr(proc, "_handle", None)
            if handle is None:
                handle = own = k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)  # SET_QUOTA | TERMINATE
            if not handle or not k32.AssignProcessToJobObject(job, int(handle)):
                raise OSError("AssignProcessToJobObject")
        except OSError:
            k32.CloseHandle(job)
            return None
        finally:
            if own:
                k32.CloseHandle(own)
        return job
    except Exception:  # noqa: BLE001 - a ctypes fault of any type: fall back to killing the worker alone
        return None


def _job_kill(job: int) -> None:
    """Ends every process in the job, then closes its handle (which would end them too: KILL_ON_JOB_CLOSE)."""
    try:
        _, _, k32 = _k32()
        k32.TerminateJobObject(job, 1)
        k32.CloseHandle(job)
    except Exception:  # noqa: BLE001 - the worker itself is still killed by the caller
        pass


# ---- the session -----------------------------------------------------------------------------------------------------

WORKER_CMD = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve().parents[1] / "read.py"), "ocr-worker"]


class Session:
    """The conversion's link to its OCR worker process, started on first need and once per conversion.

    `submit` files a picture or page and returns at once, unless more than OCR_QUEUE_BYTES of pixels already wait
    for the worker: it then waits for room until the caller's `until`, and at most until the stop. A page still
    waiting at `until` is "late" at once, not queued, and the session goes on; one still waiting at the stop expires
    the session. `wait` gives its Result. A result in `store` (a `cache.OcrResults`, or any object with `get(key)`
    and `put(key, dict)`) is taken from there without the worker, and each "done" one is filed there (decision 12).
    Frames go to the worker from a writer thread and come back to one reader thread, which matches answers to tickets
    by id: they come in any order.

    OCR never fails a conversion, nor stalls it past the stop. A worker that exits, cannot start, refuses a job or
    sends a frame that does not read gives "failed" tickets (a bad frame also kills it); a dead worker is not started
    again. A refusal the worker flags `cut` (the job's time ran out before any page) is "late". At
    `deadline - FIGURE_RESERVE` (the stop) a waiting `wait` kills the worker with its whole process tree, and every
    open ticket becomes "late", as does any later one. An answer the worker flags `cut` (its deadline passed while it
    read the job) or that arrives at or after the stop is used but never filed in `store`: it may be a reading the
    time cut short.

    `deadline` is a time.monotonic() value, or None (or math.inf, or NaN) for no limit: nothing then goes late, waits
    are only bounded by the answers, and the jobs go without `left`, which the worker reads as no limit. `backend`
    is (engine id, version), the ids the per-picture key takes. A `dpi` that is not a positive finite number is read
    as OCR_DPI.
    """

    def __init__(self, deadline: float | None, backend: tuple[str, str], *, cmd: list | None = None, store=None):
        try:
            deadline = math.inf if deadline is None else float(deadline)
        except (TypeError, ValueError):
            deadline = math.inf
        self.deadline = math.inf if math.isnan(deadline) else deadline
        self.backend = (str(backend[0]), str(backend[1]))
        self.cmd = list(WORKER_CMD if cmd is None else cmd)
        self.store = store
        self.hint = False
        self.late = False
        self._cond = threading.Condition()
        self._ids = itertools.count(1)
        self._done: dict[int, Result] = {}
        self._open: dict[int, Ticket] = {}
        self._state = "idle"  # idle, running, dead (no worker any more), over (past the deadline), closed
        self._proc = None
        self._job = None
        self._queue: deque = deque()  # (ticket, pixels) for the writer thread; None ends it
        self._queued = 0  # pixel bytes in `_queue`
        self._threads: list[threading.Thread] = []

    def _stop(self) -> float:
        return self.deadline - limits.FIGURE_RESERVE

    def _cached(self, key: str, kind: str) -> Result | None:
        if self.store is None:
            return None
        try:
            d = self.store.get(key)
            r = _result_of(d, kind) if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001 - a store or entry that does not read is a miss, never the document's fault
            return None
        return r if r is not None and r.status == "done" else None

    def _file(self, t: Ticket, r: Result) -> None:
        """Under the lock: `t` settled with `r`."""
        self._done[t.id] = r
        self.hint = self.hint or r.hint
        if r.status == "late":
            self.late = True
        self._cond.notify_all()

    def submit(self, g: Gray, kind: str, dpi: float, until: float | None = None) -> Ticket:
        """A ticket for `g`. Over OCR_QUEUE_BYTES, waits for room until `until` (a time.monotonic() value; None: no
        bound of its own), and at most until the stop: still over budget at `until`, the ticket is "late" at once,
        its pixels not queued and the session going on; at the stop, the session expires."""
        if kind not in ("page", "picture"):
            raise ValueError(f"kind {kind!r}")
        try:
            dpi = float(dpi)
        except (TypeError, ValueError):
            dpi = math.nan
        if not math.isfinite(dpi) or dpi <= 0:  # a resolution tag of 0/0, say: the page is still read
            dpi = float(limits.OCR_DPI)
        key = picture_key(g, *self.backend, kind=kind, dpi=dpi)
        t = Ticket(next(self._ids), kind, key, dpi)
        hit = self._cached(key, kind)
        expire = full = False
        end = self._stop() if until is None else min(float(until), self._stop())
        with self._cond:
            while hit is None and self._state == "running" and self._queued > limits.OCR_QUEUE_BYTES:
                left = end - time.monotonic()
                if left <= 0:
                    full = True
                    break
                self._cond.wait(min(left, 3600.0))
            if hit is not None:
                self._file(t, hit)
            elif full and time.monotonic() < self._stop():
                self._file(t, Result("late"))
            elif self._state == "over" or time.monotonic() >= self._stop():
                self._open[t.id] = t
                expire = True
            elif self._state in ("dead", "closed") or g.w * g.h > images._MAX_PIXELS:
                self._file(t, Result("failed"))
            else:
                if self._state == "idle":
                    self._start()
                if self._state != "running":
                    self._file(t, Result("failed"))
                else:
                    self._open[t.id] = t
                    self._queue.append((t, g))
                    self._queued += len(g.data)
                    self._cond.notify_all()
        if expire:
            self._expire()
        return t

    def _start(self) -> None:
        """Under the lock: the worker process and its two threads; state "dead" when it cannot start."""
        import subprocess
        kw = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "bufsize": 0}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # the reader runs inside a hook: no console flashes
        else:
            kw["start_new_session"] = True  # its own process group, killed as one
        try:
            proc = subprocess.Popen(self.cmd, **kw)
        except (OSError, ValueError):
            self._state = "dead"
            return
        self._proc = proc
        self._job = _job_for(proc) if sys.platform == "win32" else None
        self._state = "running"
        self._threads = [threading.Thread(target=self._write_loop, args=(proc,), daemon=True),
                         threading.Thread(target=self._read_loop, args=(proc,), daemon=True)]
        for th in self._threads:
            th.start()

    def _write_loop(self, proc) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                item = self._queue.popleft()
                if item is None:
                    return
                t, g = item
                self._queued -= len(g.data)
                self._cond.notify_all()
                if t.id not in self._open:  # settled meanwhile (late, or the worker died)
                    continue
            # `left` ends OCR_ANSWER_MARGIN before the session's stop, so a page the worker cannot finish comes back
            # cut while it can still be used; with no deadline it is left out, which the worker reads as no limit
            head = {"id": t.id, "w": g.w, "h": g.h, "kind": t.kind, "dpi": t.dpi}
            left = self._stop() - limits.OCR_ANSWER_MARGIN - time.monotonic()
            if math.isfinite(left):
                if left <= 0:  # its answer could no longer land before the stop
                    self._settle(t.id, Result("late"))
                    continue
                head["left"] = left
            try:
                prefix = _frame_head(head, g.data)
            except FrameError:  # nothing was written: the stream is intact, only this ticket fails
                self._settle(t.id, Result("failed"))
                continue
            try:
                _send(proc.stdin, prefix, g.data)
            except (OSError, ValueError):  # the pipe broke or was closed: the worker is gone
                self._gone(proc)
                return

    def _settle(self, tid: int, r: Result) -> None:
        with self._cond:
            t = self._open.pop(tid, None)
            if t is not None:
                self._file(t, r)

    def _read_loop(self, proc) -> None:
        try:
            while (fr := read_frame(proc.stdout)) is not None:
                try:
                    ok = self._answer(fr[0])
                except Exception:  # noqa: BLE001 - an answer that breaks anything is a worker not to be trusted
                    ok = False
                if not ok:
                    break
        except Exception:  # noqa: BLE001 - a frame that does not read, or the pipe: the worker is gone
            pass
        finally:
            self._gone(proc)

    def _answer(self, head: dict) -> bool:
        """One answer filed; False for one that does not read, which counts as the worker's death."""
        tid, ok, cut = head.get("id"), head.get("ok"), head.get("cut", False)
        if type(tid) is not int or type(ok) is not bool or type(cut) is not bool:
            return False
        with self._cond:
            t = self._open.get(tid)
        if t is None:
            return False
        if not ok:
            self._settle(tid, Result("late" if cut else "failed"))
            return True
        try:
            r = _result_of(head, t.kind)
        except _BAD:
            return False
        if r.status == "done" and self.store is not None and not cut and time.monotonic() < self._stop():
            try:
                self.store.put(t.key, {k: head[k] for k in _FIELDS})
            except Exception:  # noqa: BLE001 - a cache that cannot be written costs only the next read's time
                pass
        self._settle(tid, r)
        return True

    def _gone(self, proc) -> None:
        """The worker is dead or not to be trusted: its open tickets fail and it is killed."""
        with self._cond:
            if self._proc is proc and self._state == "running":
                self._state = "dead"
            for t in list(self._open.values()):
                self._file(t, Result("failed"))
            self._open.clear()
            self._drop()
        self._kill()

    def _drop(self) -> None:
        """Under the lock: the frames not yet written are dropped and the writer thread ends."""
        self._queue.clear()
        self._queued = 0
        self._queue.append(None)
        self._cond.notify_all()

    def _expire(self) -> None:
        """The deadline: the worker is killed and every open ticket is late."""
        with self._cond:
            if self._state in ("idle", "running", "dead"):
                self._state = "over"
            for t in list(self._open.values()):
                self._file(t, Result("late"))
            self._open.clear()
            self._drop()
        self._kill()

    def _kill(self) -> None:
        """Ends the worker and every process it started (a Job object on Windows, the process group elsewhere)."""
        with self._cond:
            proc, job, self._job = self._proc, self._job, None
        if proc is None:
            return
        if job is not None:
            _job_kill(job)
        elif sys.platform != "win32" and proc.returncode is None:  # once reaped, its pgid may be someone else's
            import os
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(2)
        except Exception:  # noqa: BLE001 - TimeoutExpired: nothing more can be done from here
            pass

    def peek(self, t: Ticket) -> Result | None:
        """`t`'s Result when it is settled, else None, without waiting."""
        with self._cond:
            return self._done.get(t.id)

    def wait(self, t: Ticket) -> Result:
        """`t`'s Result, waiting at most until `deadline - FIGURE_RESERVE`; then the worker is killed and every open
        ticket, `t` among them, is "late"."""
        with self._cond:
            while t.id not in self._done:
                if t.id not in self._open:  # a ticket is always one or the other, under the lock
                    raise KeyError(f"ticket {t.id} is not this session's")
                left = self._stop() - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 3600.0))  # no deadline is inf, which Condition.wait cannot take
            r = self._done.get(t.id)
        if r is None:
            self._expire()
            with self._cond:
                r = self._done[t.id]
        return r

    def close(self) -> None:
        """Closes the worker's stdin, waits up to 1 s for it to end, and kills it. Open tickets fail."""
        with self._cond:
            if self._state == "closed":
                return
            self._state = "closed"
            for t in list(self._open.values()):
                self._file(t, Result("failed"))
            self._open.clear()
            proc = self._proc
            self._queue.append(None)  # after the frames already queued: their tickets are failed, so none is sent
            self._cond.notify_all()
        if proc is not None:
            writer = self._threads[0]
            writer.join(1.0)
            if writer.is_alive():  # stuck on a full pipe: the worker is not reading
                self._kill()
                writer.join(1.0)
            if not writer.is_alive():  # closing a pipe a thread is blocked on can wait on the CRT's fd lock
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            try:
                proc.wait(1.0)
            except Exception:  # noqa: BLE001 - TimeoutExpired: killed below
                pass
        self._kill()
        for th in self._threads:
            th.join(2.0)
        # a reader still blocked means another process holds the pipe (a grandchild with no job object): its pipe is
        # left to that process's exit
        if proc is not None and not self._threads[1].is_alive():
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass


_current: Session | None = None


def backend() -> tuple[str, str]:
    """(engine id, version) of the OCR engines on this machine: `ocr_probe.backend()`, the gate's own."""
    from . import ocr_probe
    return ocr_probe.backend()


def session() -> Session | None:
    """The conversion's current Session, or None (no OCR engine, or outside `opened`)."""
    return _current


@contextlib.contextmanager
def opened(deadline: float | None, backend: tuple[str, str], *, cmd: list | None = None, store=None):
    """Sets the current Session for the block and closes it after; yields None, and sets none, when `backend[0]`
    is empty (no OCR engine). With a Session, raster's backend is settled first, before any thread may decode
    pixels through `raster.gray`."""
    global _current
    s = None
    if backend and backend[0]:
        from . import raster
        raster.backend()
        s = Session(deadline, backend, cmd=cmd, store=store)
    prev, _current = _current, s
    try:
        yield s
    finally:
        _current = prev
        if s is not None:
            s.close()
