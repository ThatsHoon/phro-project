"""Engine choice per page, rotation and the page verdict (`reader.ocr`), with stand-in engines that return canned
pages and record their calls. Neither Windows OCR nor Tesseract is needed.

Run: python -I -X utf8 tests/test_reader_ocr.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import limits, ocr  # noqa: E402
from reader.ocr import OcrPage, Word  # noqa: E402
from reader.ocr_pixels import Gray, Unread, rotate  # noqa: E402

G = Gray(4, 2, bytes(range(8)))  # asymmetric, so each turn gives different bytes
FAR = 1e18  # a deadline that never comes


def page(texts, engine="win-ko", conf=None, lines=None) -> OcrPage:
    """One word per text, left to right on line 0 unless `lines` gives each word's line."""
    lines = lines or [0] * len(texts)
    words = [Word(t, 10 + 60 * i, 10 + 30 * ln, 50, 20, ln, conf) for i, (t, ln) in enumerate(zip(texts, lines))]
    return OcrPage(200, 100, words, 0.0, 0, engine, "v1")


CLEAN_KO = ["안녕하세요", "반갑습니다"]
JUNK = ["ㅇㅏㄴ", "ㄴㅕㅇ", "♬♪"]
ENGLISH = ["The", "quick", "brown", "fox", "jumps"]


class Win:
    """A stand-in Windows engine: `fn(g)` gives the page; every call's bitmap is recorded."""

    def __init__(self, fn, engine="win-ko"):
        self.fn, self.engine, self.version = fn, engine, "v1"
        self.calls = []

    def recognize(self, g, deadline):
        self.calls.append(g)
        return self.fn(g)


class Tess:
    """A stand-in Tesseract adapter: `fn(g, langs)` gives the page; every call's (langs, psm) is recorded."""

    def __init__(self, langs, fn=None):
        self.langs = frozenset(langs)
        self.fn = fn or (lambda g, langs: page(CLEAN_KO, "tess-" + langs, conf=95))
        self.calls = []

    def recognize(self, g, langs, psm, deadline):
        self.calls.append((langs, psm))
        return self.fn(g, langs)


def text(p: OcrPage) -> str:
    return " ".join(w.text for w in p.words)


def test_latin_page_goes_to_tess_eng():
    win = Win(lambda g: page(ENGLISH))
    tess = Tess({"eng", "kor"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))
    got, hint = ocr.read_page(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "tess-eng", got.engine
    assert tess.calls == [("eng", limits.TESS_PSM_LATIN)], tess.calls
    assert len(win.calls) == 1 and hint is False


def test_latin_page_without_tess_keeps_windows():
    win = Win(lambda g: page(ENGLISH))
    got, hint = ocr.read_page(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == " ".join(ENGLISH)
    tess = Tess({"kor"})  # Tesseract without eng keeps Windows too
    got, hint = ocr.read_page(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and tess.calls == [] and hint is False


def test_mixed_korean_english_stays_windows():
    p = page(["가나다라마바사", "abc"])  # 7 Hangul letters, 3 Latin: 30 % Latin
    assert abs(ocr.latin_share(p) - 0.3) < 1e-9 and ocr.latin_share(p) < limits.OCR_LATIN
    win = Win(lambda g: p)
    tess = Tess({"eng", "kor"})
    got, hint = ocr.read_page(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == "가나다라마바사 abc"
    assert tess.calls == [] and hint is False


def test_junk_on_non_korean_engine_goes_to_tess_kor():
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    tess = Tess({"eng", "kor"})
    got, hint = ocr.read_page(G, ocr.Engines(win, tess, False), FAR, "page")
    assert got.engine == "tess-kor+eng" and text(got) == " ".join(CLEAN_KO)
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR)], tess.calls
    assert len(win.calls) == 1 and hint is False  # the re-read is clean: no rotation


def test_junk_without_tess_kor_sets_hint():
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    for tess in (None, Tess({"eng"})):
        got, hint = ocr.read_page(G, ocr.Engines(win, tess, False), FAR, "picture")
        assert got.engine == "win-en-us" and hint is True
        assert tess is None or tess.calls == []
    # a Korean Windows engine that gives junk is not a missing-language case
    win = Win(lambda g: page(JUNK))
    got, hint = ocr.read_page(G, ocr.Engines(win, None, True), FAR, "picture")
    assert got.engine == "win-ko" and hint is False


def test_no_windows_uses_tess_present_langs():
    cases = [({"eng"}, ("eng", limits.TESS_PSM_LATIN)),
             ({"kor", "eng", "osd"}, ("kor+eng", limits.TESS_PSM_KOR)),
             ({"kor"}, ("kor", limits.TESS_PSM_KOR))]
    for langs, call in cases:
        tess = Tess(langs)
        got, hint = ocr.read_page(G, ocr.Engines(None, tess, False), FAR, "page")
        assert tess.calls == [call], (langs, tess.calls)
        assert got.engine == "tess-" + call[0] and hint is False
    tess = Tess({"osd"})  # neither kor nor eng
    assert ocr.read_page(G, ocr.Engines(None, tess, False), FAR, "page") == (None, False)
    assert tess.calls == []
    assert ocr.read_page(G, ocr.Engines(None, None, False), FAR, "page") == (None, False)


def test_no_page_from_windows_gives_none():
    win = Win(lambda g: None)
    tess = Tess({"kor", "eng"})
    assert ocr.read_page(G, ocr.Engines(win, tess, False), FAR, "page") == (None, False)
    assert tess.calls == []


def test_engine_error_propagates():
    def boom(g):
        raise OSError("engine fault")
    try:
        ocr.read_page(G, ocr.Engines(Win(boom), None, True), FAR, "page")
    except OSError as e:
        assert "engine fault" in str(e)
    else:
        raise AssertionError("OSError swallowed")


def test_upside_down_page_takes_180():
    flipped = rotate(G, 180).data
    win = Win(lambda g: page(CLEAN_KO) if g.data == flipped else page(JUNK))
    got, hint = ocr.read_page(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 180 and text(got) == " ".join(CLEAN_KO), (got.turn, text(got))
    assert [c.data for c in win.calls] == [rotate(G, t).data for t in (0, 180, 90, 270)]
    assert hint is False


def test_rotation_keeps_most_good_text():
    turned = {rotate(G, t).data: t for t in (0, 90, 180, 270)}
    out = {0: JUNK, 90: ["ㄱ가나다라마바사아자", "ㅋ"], 180: JUNK, 270: ["가나"]}  # 90: 10 good, some junk
    win = Win(lambda g: page(out[turned[g.data]]))
    got, _ = ocr.read_page(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 90 and text(got) == "ㄱ가나다라마바사아자 ㅋ", got.turn


def test_rotation_hint_follows_chosen_pass():
    flipped = rotate(G, 180).data
    win = Win(lambda g: page(CLEAN_KO, "win-en-us") if g.data == flipped else page(JUNK, "win-en-us"),
              engine="win-en-us")
    got, hint = ocr.read_page(G, ocr.Engines(win, None, False), FAR, "page")
    assert got.turn == 180 and hint is False  # the junk was the turn, not a missing language
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    got, hint = ocr.read_page(G, ocr.Engines(win, None, False), FAR, "page")
    assert got.turn == 0 and hint is True and len(win.calls) == 4


def test_picture_read_once():
    win = Win(lambda g: page(JUNK))
    got, _ = ocr.read_page(G, ocr.Engines(win, None, True), FAR, "picture")
    assert got.turn == 0 and len(win.calls) == 1 and win.calls[0].data == G.data


def test_rotation_stops_at_deadline():
    clock = [100.0]
    deadline = 110.0

    def slow(g):
        clock[0] = deadline + 1
        return page(JUNK, "win-en-us")

    win = Win(slow, engine="win-en-us")
    tess = Tess({"kor", "eng"})
    saved = ocr._now
    ocr._now = lambda: clock[0]
    try:
        got, hint = ocr.read_page(G, ocr.Engines(win, tess, False), deadline, "page")
    finally:
        ocr._now = saved
    assert got.turn == 0 and text(got) == " ".join(JUNK)
    assert len(win.calls) == 1 and tess.calls == [], (len(win.calls), tess.calls)
    assert hint is False  # kor exists; only the time ran out


def under_over(threshold: float, n: int) -> tuple[int, int]:
    """Counts out of n whose share is just under (or at) and just over `threshold`."""
    return math.ceil(threshold * n) - 1, math.floor(threshold * n) + 1


def test_judge_thresholds():
    clean = Unread(0.0, None, 0.0)
    n = 10_000
    lo, hi = under_over(limits.OCR_JUNK, n)
    for k, want in ((lo, "trusted"), (hi, "doubtful")):
        p = page(["가" * (n - k) + "ㄱ" * k])
        assert ocr.judge(p, clean).kind == want, ("junk", k, ocr.junk_share(p))

    p = page(CLEAN_KO)
    for share, want in ((limits.OCR_UNREAD - 1e-3, "trusted"), (limits.OCR_UNREAD + 1e-3, "doubtful")):
        assert ocr.judge(p, Unread(share, None, 0.0)).kind == want, ("unread", share)

    for conf, want in ((limits.OCR_CONF + 0.5, "trusted"), (limits.OCR_CONF - 0.5, "doubtful")):
        assert conf >= limits.OCR_LOW_WORD  # only the mean moves
        assert ocr.judge(page(CLEAN_KO * 5, "tess-kor+eng", conf=conf), clean).kind == want, ("conf", conf)

    n = 1_000
    lo, hi = under_over(limits.OCR_LOW, n)
    for k, want in ((lo, "trusted"), (hi, "doubtful")):
        p = page(["가"] * n, "tess-kor+eng", conf=100.0)
        for w in p.words[:k]:
            w.conf = limits.OCR_LOW_WORD - 1
        assert sum(w.conf for w in p.words) / n >= limits.OCR_CONF  # only the low share moves
        assert ocr.judge(p, clean).kind == want, ("low", k)
    p = page(["가"] * n, "tess-kor+eng", conf=100.0)
    for w in p.words[:hi]:
        w.conf = limits.OCR_LOW_WORD  # at the line is not low
    assert ocr.judge(p, clean).kind == "trusted"


def test_judge_area_then_trusted():
    p = page(CLEAN_KO)
    box = (0.1, 0.5, 0.6, 0.9)
    v = ocr.judge(p, Unread(0.05, box, limits.OCR_AREA))
    assert v == ocr.Verdict("area", box), v
    v = ocr.judge(p, Unread(0.05, box, limits.OCR_AREA - 1e-6))
    assert v == ocr.Verdict("trusted", None), v
    assert ocr.judge(p, Unread(0.0, None, 0.0)) == ocr.Verdict("trusted", None)


def test_scribbles_without_words_doubtful():
    empty = page([])
    assert ocr.judge(empty, Unread(0.6, (0.1, 0.1, 0.9, 0.9), 0.64)).kind == "doubtful"
    assert ocr.judge(empty, Unread(limits.OCR_UNREAD / 2, None, 0.0)).kind == "doubtful"
    assert ocr.judge(empty, Unread(0.0, None, 0.0)).kind == "trusted"  # a blank page


def test_shares():
    assert ocr.junk_share(page([])) == 0.0 and ocr.latin_share(page([])) == 0.0
    assert ocr.junk_share(page(["가,", "나.", "(abc)", "「다」", "x+y=3%", "1·2…"])) == 0.0
    p = page(["（가）", "［나］", "다！", "라？", "Ａ：Ｂ"])  # full-width punctuation is punctuation
    assert ocr.junk_share(p) == 2 / 13, ocr.junk_share(p)  # full-width Latin letters are not Latin letters
    assert ocr.junk_share(page(["가.", "Café", "№"])) == 1 / 7
    assert ocr.latin_share(page(["Café", "가나"])) == 4 / 6
    for c in ocr.PUNCT:
        assert ocr.junk_share(page(["가" + c])) == 0.0, c


def test_picture_lines_need_two_words():
    one = page(["hello"])
    assert ocr.picture_lines(one) == []
    two = page(["hello", "world", "next", "line"], lines=[0, 0, 1, 1])
    # interim expectation, from the engine's line order; Task 7 replaces it with ocr_layout.reading_lines
    assert ocr.picture_lines(two) == ["hello world", "next line"], ocr.picture_lines(two)
    assert ocr.picture_lines(page(["hello"] + JUNK)) == []
    saved = limits.OCR_MIN_WORDS
    limits.OCR_MIN_WORDS = 5
    try:
        assert ocr.picture_lines(two) == []
    finally:
        limits.OCR_MIN_WORDS = saved


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
