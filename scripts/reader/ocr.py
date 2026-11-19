"""OCR results as the reader keeps them, whichever engine read the page; the engine chosen per page, the rotation
tried for an upside-down or sideways page, and the page's verdict.

The engines are reached only through `Engines`, so `ocr_win` and `ocr_tess` are imported inside `engines()` alone.
"""
import time
from dataclasses import dataclass, replace

from . import limits
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
    reported, `engine` and `version` the ids that key caches."""
    w: int
    h: int
    words: list[Word]
    angle: float
    turn: int
    engine: str
    version: str


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
    from . import ocr_win
    return ocr_win.WinOcr("ko") if ocr_win.recognizers() else None


def _tess_load() -> _Tess | None:
    """Tesseract when it is found and has `kor` or `eng`; else None."""
    from . import ocr_tess
    exe = ocr_tess.find()
    if exe is None:
        return None
    langs = ocr_tess.langs(exe)
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


def _read_once(g: Gray, eng: Engines, deadline: float) -> tuple[OcrPage | None, bool]:
    """One pass of the engine choice on `g` as it stands: (page, hint)."""
    tess = eng.tess
    if eng.win is None:
        return (None, False) if tess is None else (_tess_read(tess, g, ("kor", "eng"), None, deadline), False)
    page = eng.win.recognize(g, deadline)
    if page is None:  # Windows gave no page: Tesseract as on a machine without Windows OCR, while time is left
        return (None, False) if tess is None else (_tess_read(tess, g, ("kor", "eng"), None, deadline), False)
    if tess is not None and "eng" in tess.langs and latin_share(page) >= limits.OCR_LATIN:
        again = _tess_read(tess, g, ("eng",), limits.TESS_PSM_LATIN, deadline)
        if again is not None and again.words:  # a re-read with no words never replaces one with words
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
    `threshold`, `rules` and `Unread` on that turned bitmap, so `judge`'s `Verdict.box` is in the same frame; the page
    image or crop shown for a turned page is cut from that turned bitmap too.
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


def judge(page: OcrPage, un: Unread) -> Verdict:
    """Doubtful when the junk share passes OCR_JUNK, the unread share passes OCR_UNREAD, the words' mean confidence
    (when they carry one) is under OCR_CONF, the share of those words under OCR_LOW_WORD passes OCR_LOW, or there are
    no words while some ink is unread. Otherwise a picture area when the unread box covers at least OCR_AREA.

    `un` must be computed on `rotate(g, page.turn)`, the frame the page's boxes are in, so `Verdict.box` is a
    fractional box of that turned bitmap."""
    confs = [w.conf for w in page.words if w.conf is not None]
    doubtful = (
        junk_share(page) > limits.OCR_JUNK
        or un.share > limits.OCR_UNREAD
        or (confs and sum(confs) / len(confs) < limits.OCR_CONF)
        or (confs and sum(1 for c in confs if c < limits.OCR_LOW_WORD) / len(confs) > limits.OCR_LOW)
        or (not page.words and un.share > 0)
    )
    if doubtful:
        return Verdict("doubtful", None)
    if un.box is not None and un.area >= limits.OCR_AREA:
        return Verdict("area", un.box)
    return Verdict("trusted", None)


def picture_lines(page: OcrPage) -> list[str]:
    """The text lines of a picture for `Picture text:`, in reading order; empty under OCR_MIN_WORDS words or past
    OCR_JUNK. The order comes from `ocr_layout.reading_lines`; until that module exists, from the engine's lines."""
    if len(page.words) < limits.OCR_MIN_WORDS or junk_share(page) > limits.OCR_JUNK:
        return []
    try:
        from .ocr_layout import reading_lines
    except ModuleNotFoundError as e:  # only the missing module itself, never an import error inside it
        if e.name != f"{__package__}.ocr_layout":
            raise
        lines: dict[int, list[str]] = {}
        for w in page.words:
            lines.setdefault(w.line, []).append(w.text)
        return [" ".join(ws) for _, ws in sorted(lines.items())]
    return reading_lines(page)
