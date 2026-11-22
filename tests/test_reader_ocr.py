"""Engine choice per page, rotation and the page verdict (`reader.ocr`), with stand-in engines that return canned
pages and record their calls. Neither Windows OCR nor Tesseract is needed.

Run: python -I -X utf8 tests/test_reader_ocr.py
"""
import json
import math
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import limits, ocr, ocr_layout  # noqa: E402
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


def rp(g, eng, deadline, kind, dpi=limits.OCR_DPI):
    """read_page's (page, hint); (None, False) when no engine gave a page."""
    got = ocr.read_page(g, eng, deadline, kind, dpi)
    return (None, False) if got is None else (got.page, got.hint)


def test_latin_page_goes_to_tess_eng():
    win = Win(lambda g: page(ENGLISH))
    tess = Tess({"eng", "kor"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))
    got, hint = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "tess-eng", got.engine
    assert tess.calls == [("eng", limits.TESS_PSM_LATIN)], tess.calls
    assert len(win.calls) == 1 and hint is False


def test_latin_page_without_tess_keeps_windows():
    win = Win(lambda g: page(ENGLISH))
    got, hint = rp(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == " ".join(ENGLISH)
    tess = Tess({"kor"})  # Tesseract without eng keeps Windows too
    got, hint = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and tess.calls == [] and hint is False


def test_mixed_korean_english_stays_windows():
    p = page(["가나다라마바사", "abc"])  # 7 Hangul letters, 3 Latin: 30 % Latin
    assert abs(ocr.latin_share(p) - 0.3) < 1e-9 and ocr.latin_share(p) < limits.OCR_LATIN
    win = Win(lambda g: p)
    tess = Tess({"eng", "kor"})
    got, hint = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == "가나다라마바사 abc"
    assert tess.calls == [] and hint is False


def test_junk_on_non_korean_engine_goes_to_tess_kor():
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    tess = Tess({"eng", "kor"})
    got, hint = rp(G, ocr.Engines(win, tess, False), FAR, "page")
    assert got.engine == "tess-kor+eng" and text(got) == " ".join(CLEAN_KO)
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR)], tess.calls
    assert len(win.calls) == 1 and hint is False  # the re-read is clean: no rotation


def test_junk_without_tess_kor_sets_hint():
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    for tess in (None, Tess({"eng"})):
        got, hint = rp(G, ocr.Engines(win, tess, False), FAR, "picture")
        assert got.engine == "win-en-us" and hint is True
        assert tess is None or tess.calls == []
    # a Korean Windows engine that gives junk is not a missing-language case
    win = Win(lambda g: page(JUNK))
    got, hint = rp(G, ocr.Engines(win, None, True), FAR, "picture")
    assert got.engine == "win-ko" and hint is False


def test_no_windows_uses_tess_present_langs():
    cases = [({"eng"}, ("eng", limits.TESS_PSM_LATIN)),
             ({"kor", "eng", "osd"}, ("kor+eng", limits.TESS_PSM_KOR)),
             ({"kor"}, ("kor", limits.TESS_PSM_KOR))]
    for langs, call in cases:
        tess = Tess(langs)
        got, hint = rp(G, ocr.Engines(None, tess, False), FAR, "page")
        assert tess.calls == [call], (langs, tess.calls)
        assert got.engine == "tess-" + call[0] and hint is False
    tess = Tess({"osd"})  # neither kor nor eng
    assert rp(G, ocr.Engines(None, tess, False), FAR, "page") == (None, False)
    assert tess.calls == []
    assert rp(G, ocr.Engines(None, None, False), FAR, "page") == (None, False)


def test_no_page_from_windows_falls_back_to_tess():
    for langs, call in (({"kor", "eng"}, ("kor+eng", limits.TESS_PSM_KOR)), ({"eng"}, ("eng", limits.TESS_PSM_LATIN))):
        win = Win(lambda g: None, engine="win-en-us")
        tess = Tess(langs)
        got, hint = rp(G, ocr.Engines(win, tess, False), FAR, "page")
        assert got is not None and got.engine == "tess-" + call[0] and text(got) == " ".join(CLEAN_KO)
        assert tess.calls == [call], tess.calls
        assert len(win.calls) == 1 and hint is False
    win = Win(lambda g: None)
    assert rp(G, ocr.Engines(win, None, True), FAR, "page") == (None, False)  # no Tesseract
    assert rp(G, ocr.Engines(win, Tess({"osd"}), True), FAR, "page") == (None, False)


def test_windows_fallback_stops_at_deadline():
    clock = [100.0]

    def late(g):
        clock[0] = 111.0
        return None

    tess = Tess({"kor", "eng"})
    saved = ocr._now
    ocr._now = lambda: clock[0]
    try:
        got = rp(G, ocr.Engines(Win(late), tess, True), 110.0, "page")
    finally:
        ocr._now = saved
    assert got == (None, False) and tess.calls == []


def test_engine_error_propagates():
    def boom(g):
        raise OSError("engine fault")
    try:
        rp(G, ocr.Engines(Win(boom), None, True), FAR, "page")
    except OSError as e:
        assert "engine fault" in str(e)
    else:
        raise AssertionError("OSError swallowed")


def test_upside_down_page_takes_180():
    flipped = rotate(G, 180).data
    win = Win(lambda g: page(CLEAN_KO) if g.data == flipped else page(JUNK))
    got, hint = rp(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 180 and text(got) == " ".join(CLEAN_KO), (got.turn, text(got))
    assert [c.data for c in win.calls] == [rotate(G, t).data for t in (0, 180)]  # a clean turn ends the search
    assert hint is False


def by_turn(out: dict) -> Win:
    """A stand-in Windows engine giving `out[turn]` (a list of word texts) for the bitmap turned by `turn`, and JUNK
    for any other bitmap (a turn read again at another scale)."""
    turned = {rotate(G, t).data: t for t in (0, 90, 180, 270)}
    return Win(lambda g: page(out.get(turned.get(g.data), JUNK)))


def test_rotation_empty_turn_does_not_stop():
    win = by_turn({0: JUNK, 180: [], 90: JUNK, 270: CLEAN_KO})  # a sideways page: nothing at 180
    got, _ = rp(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 270 and text(got) == " ".join(CLEAN_KO), got.turn
    assert [c.data for c in win.calls] == [rotate(G, t).data for t in (0, 180, 90, 270)]


def test_rotation_stops_at_clean_90():
    win = by_turn({0: JUNK, 180: JUNK, 90: CLEAN_KO, 270: CLEAN_KO * 3})
    got, _ = rp(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 90 and text(got) == " ".join(CLEAN_KO), got.turn
    assert [c.data for c in win.calls] == [rotate(G, t).data for t in (0, 180, 90)]


def test_rotation_keeps_most_good_text():
    out = {0: JUNK, 90: ["ㄱ가나다라마바사아자", "ㅋ"], 180: JUNK, 270: ["ㄱ가나", "ㅋ"]}  # all doubtful
    got, _ = rp(G, ocr.Engines(by_turn(out), None, True), FAR, "page")
    assert got.turn == 90 and text(got) == "ㄱ가나다라마바사아자 ㅋ", got.turn


def test_rotation_ranks_by_doubt_before_score():
    out = {0: JUNK, 180: ["가나다라마바사아자차카타파하" * 3, "ㅋㅋ"], 90: CLEAN_KO, 270: CLEAN_KO}
    assert ocr._score(page(out[180])) > ocr._score(page(CLEAN_KO))
    win = by_turn(out)
    got, _ = rp(G, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 90 and text(got) == " ".join(CLEAN_KO), (got.turn, text(got))
    assert [c.data for c in win.calls] == [rotate(G, t).data for t in (0, 180, 90)]


def test_rotation_hint_follows_chosen_pass():
    flipped = rotate(G, 180).data
    win = Win(lambda g: page(CLEAN_KO, "win-en-us") if g.data == flipped else page(JUNK, "win-en-us"),
              engine="win-en-us")
    got, hint = rp(G, ocr.Engines(win, None, False), FAR, "page")
    assert got.turn == 180 and hint is False  # the junk was the turn, not a missing language
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    got, hint = rp(G, ocr.Engines(win, None, False), FAR, "page")
    assert got.turn == 0 and hint is True and len(win.calls) == 5  # four turns, then turn 0 at twice the scale


def test_picture_read_once():
    win = Win(lambda g: page(JUNK))
    got, _ = rp(G, ocr.Engines(win, None, True), FAR, "picture")
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
        got, hint = rp(G, ocr.Engines(win, tess, False), deadline, "page")
    finally:
        ocr._now = saved
    assert got.turn == 0 and text(got) == " ".join(JUNK)
    assert len(win.calls) == 1 and tess.calls == [], (len(win.calls), tess.calls)
    assert hint is False  # kor exists; only the time ran out


def under_over(threshold: float, n: int) -> tuple[int, int]:
    """Counts out of n whose share is just under and just over `threshold` (never at it)."""
    return math.ceil(threshold * n) - 1, math.floor(threshold * n) + 1


def test_judge_thresholds():
    clean = Unread(0.0, None, 0.0)
    n = 10_000
    lo, hi = under_over(limits.OCR_JUNK, n)
    for k, want in ((lo, "trusted"), (hi, "doubtful")):
        p = page(["가" * (n - k) + "ㄱ" * k])
        assert ocr.judge(p, clean).kind == want, ("junk", k, ocr.junk_share(p))

    p = page(CLEAN_KO, "tess-kor+eng", conf=95.0)
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


def test_judge_unread_bar_without_confidence():
    """Words without a confidence (Windows OCR) leave nothing but the ink they did not read to doubt them by, and an
    engine that reads clean text covers nearly all of it: past OCR_UNREAD_NOCONF the page is doubtful. Words with a
    confidence keep the looser OCR_UNREAD, the bar Tesseract's scattered misses need, and so does ink a picture
    holds (a dense box): a page with a large photo is not doubted for the photo's ink."""
    lo, hi = limits.OCR_UNREAD_NOCONF - 1e-3, limits.OCR_UNREAD_NOCONF + 1e-3
    assert hi < limits.OCR_UNREAD
    bare, sure = page(ENGLISH), page(ENGLISH, "tess-eng", conf=95.0)
    assert ocr.judge(bare, Unread(lo, None, 0.0)).kind == "trusted"
    assert ocr.judge(bare, Unread(limits.OCR_UNREAD_NOCONF, None, 0.0)).kind == "trusted"
    assert ocr.judge(bare, Unread(hi, None, 0.0)).kind == "doubtful"
    assert ocr.judge(bare, Unread(hi, (0.0, 0.0, 1.0, 1.0), 1.0, 0.006)).kind == "doubtful"  # noise over the page
    assert ocr.judge(sure, Unread(hi, None, 0.0)).kind == "trusted"
    photo = (0.0, 0.1, 1.0, 0.9)
    assert ocr.judge(bare, Unread(hi, photo, 0.8, 0.5)) == ocr.Verdict("area", photo)


def test_judge_area_then_trusted():
    p = page(CLEAN_KO, "tess-kor+eng", conf=95.0)
    box = (0.1, 0.5, 0.6, 0.9)
    dense = limits.OCR_AREA_DENSITY
    v = ocr.judge(p, Unread(0.05, box, limits.OCR_AREA, dense))
    assert v == ocr.Verdict("area", box), v
    v = ocr.judge(p, Unread(0.05, box, limits.OCR_AREA - 1e-6, dense))
    assert v == ocr.Verdict("trusted", None), v
    assert ocr.judge(p, Unread(0.0, None, 0.0)) == ocr.Verdict("trusted", None)


def test_judge_scattered_misses_trusted():
    """I3: glyphs an engine missed here and there span a large box with sparse ink in it: no picture there."""
    p = page(CLEAN_KO, "tess-kor+eng", conf=95.0)
    box = (0.1, 0.1, 0.9, 0.7)
    v = ocr.judge(p, Unread(0.03, box, 0.48, limits.OCR_AREA_DENSITY - 1e-3))
    assert v == ocr.Verdict("trusted", None), v
    v = ocr.judge(p, Unread(0.03, box, 0.48, limits.OCR_AREA_DENSITY))
    assert v == ocr.Verdict("area", box), v


def test_judge_compact_dense_ink_is_area_past_unread():
    """I8: a logo or a dark photo is much of a clean page's ink, but compact and dense: a crop of it, not the whole
    page. Ink spread over the page (a noisy scan), or sparse in its box, stays doubtful."""
    p = page(CLEAN_KO)
    logo = (0.75, 0.05, 0.85, 0.11)
    v = ocr.judge(p, Unread(0.12, logo, 0.006, 0.9))
    assert v == ocr.Verdict("area", logo), v
    photo = (0.3, 0.35, 0.7, 0.65)
    assert ocr.judge(p, Unread(0.7, photo, 0.12, 0.95)) == ocr.Verdict("area", photo)
    whole = (0.0, 0.0, 1.0, 1.0)
    assert ocr.judge(p, Unread(0.12, whole, 1.0, 0.9)).kind == "doubtful"
    big = (0.0, 0.0, 1.0, limits.FIGURE_WHOLE)
    assert ocr.judge(p, Unread(0.12, big, limits.FIGURE_WHOLE, 0.9)).kind == "doubtful"
    assert ocr.judge(p, Unread(0.12, logo, 0.006, limits.OCR_AREA_DENSITY - 1e-3)).kind == "doubtful"
    assert ocr.judge(page(JUNK), Unread(0.12, logo, 0.006, 0.9)).kind == "doubtful"  # the other signals still count


def test_scribbles_without_words_doubtful():
    empty = page([])
    assert ocr.judge(empty, Unread(0.6, (0.1, 0.1, 0.9, 0.9), 0.64)).kind == "doubtful"
    assert ocr.judge(empty, Unread(limits.OCR_UNREAD / 2, None, 0.0)).kind == "doubtful"
    assert ocr.judge(empty, Unread(0.0, None, 0.0)).kind == "trusted"  # a blank page


def test_faint_page_without_words_doubtful():
    """M3: ink too faint for the Otsu split leaves no unread share; a gray range past OCR_FLAT still keeps the
    page's image. A truly flat page is blank: its image is dropped."""
    empty = page([])
    assert ocr.judge(empty, Unread(0.0, None, 0.0, 0.0, limits.OCR_FLAT + 1)).kind == "doubtful"
    assert ocr.judge(empty, Unread(0.0, None, 0.0, 0.0, limits.OCR_FLAT)).kind == "trusted"
    assert ocr.judge(page(CLEAN_KO), Unread(0.0, None, 0.0, 0.0, 200)).kind == "trusted"  # words were read


def sloped(texts_per_line: int, lines: int, degrees: float, engine="tess-kor+eng", psm=limits.TESS_PSM_KOR) -> OcrPage:
    """Lines of word boxes whose baselines slope by `degrees` (y down), each line its own engine line, as the engine
    `engine` read them at page segmentation mode `psm` (None: not Tesseract)."""
    k = math.tan(math.radians(degrees))
    words = []
    for ln in range(lines):
        for i in range(texts_per_line):
            x = 100 + 120 * i
            words.append(Word("가나", x, round(200 + 60 * ln + k * x), 100, 30, ln, 95.0))
    return OcrPage(2000, 3000, words, 0.0, 0, engine, "v1", psm=psm)


def test_judge_sloped_lines_doubtful():
    """I7: a skewed page read without deskewing (Tesseract psm 4 shreds its lines) is doubtful past OCR_SKEW."""
    clean = Unread(0.0, None, 0.0)
    assert ocr.judge(sloped(12, 20, 2.0), clean).kind == "doubtful"
    assert ocr.judge(sloped(12, 20, -2.0), clean).kind == "doubtful"
    assert ocr.judge(sloped(12, 20, limits.OCR_SKEW * 1.2), clean).kind == "doubtful"
    assert ocr.judge(sloped(12, 20, limits.OCR_SKEW * 0.6), clean).kind == "trusted"
    assert ocr.judge(sloped(12, 20, 0.0), clean).kind == "trusted"
    assert ocr.judge(sloped(1, 20, 3.0), clean).kind == "trusted"  # one word a line gives no slope


def test_skew_doubt_only_for_psm_kor_reads():
    """The skew doubt is for Tesseract's psm 4 (TESS_PSM_KOR) read, which shreds skewed lines: the eng read at
    TESS_PSM_LATIN (psm 3) and Windows (which deskews) read 2-degree pages at CER 0.001-0.015 in the spike."""
    clean = Unread(0.0, None, 0.0)
    assert ocr.judge(sloped(12, 20, 2.0, "tess-kor+eng", limits.TESS_PSM_KOR), clean).kind == "doubtful"
    assert ocr.judge(sloped(12, 20, 2.0, "tess-eng", limits.TESS_PSM_LATIN), clean).kind == "trusted"
    assert ocr.judge(sloped(12, 20, 2.0, "win-ko", None), clean).kind == "trusted"
    page = sloped(12, 20, 2.0, "tess-kor+eng", limits.TESS_PSM_KOR)
    back = ocr._page_of(json.loads(json.dumps(asdict(page))))
    assert back.psm == limits.TESS_PSM_KOR and ocr.judge(back, clean).kind == "doubtful"


def test_no_windows_latin_page_read_again_by_eng():
    """I7: without Windows OCR, a kor+eng page of Latin letters is read again by eng at TESS_PSM_LATIN."""
    tess = Tess({"kor", "eng"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))
    got, hint = rp(G, ocr.Engines(None, tess, False), FAR, "page")
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR), ("eng", limits.TESS_PSM_LATIN)], tess.calls
    assert got.engine == "tess-eng" and hint is False
    tess = Tess({"kor", "eng"}, lambda g, langs: page(ENGLISH if langs == "kor+eng" else [], "tess-" + langs))
    got, _ = rp(G, ocr.Engines(None, tess, False), FAR, "page")
    assert got.engine == "tess-kor+eng", got.engine  # a re-read with no words keeps the first
    tess = Tess({"kor", "eng"})  # Korean: no re-read
    rp(G, ocr.Engines(None, tess, False), FAR, "page")
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR)], tess.calls
    tess = Tess({"eng"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))  # eng alone read it already
    rp(G, ocr.Engines(None, tess, False), FAR, "page")
    assert tess.calls == [("eng", limits.TESS_PSM_LATIN)], tess.calls
    tess = Tess({"kor", "eng"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))
    got, _ = rp(G, ocr.Engines(Win(lambda g: None), tess, True), FAR, "page")  # Windows gave no page
    assert got.engine == "tess-eng" and tess.calls[-1] == ("eng", limits.TESS_PSM_LATIN), tess.calls


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
    assert ocr.picture_lines(two) == ocr_layout.reading_lines(two) == ["hello world", "next line"], ocr.picture_lines(two)
    saved = limits.OCR_MIN_WORDS
    limits.OCR_MIN_WORDS = 5
    try:
        assert ocr.picture_lines(two) == []
    finally:
        limits.OCR_MIN_WORDS = saved


def test_engines_never_raise():
    class BadWinOcr:  # a WinOcr whose ctypes call fails with something other than OSError
        def __init__(self, lang):
            raise ValueError("bad vtable call")

    class GoodWinOcr:
        engine, version = "win-ko", "v1"

        def __init__(self, lang):
            self.lang = lang

    def bad_tess():
        raise RuntimeError("tesseract probe failed")

    saved = ocr._win_load, ocr._tess_load
    try:
        ocr._win_load, ocr._tess_load = (lambda: BadWinOcr("ko")), bad_tess
        assert ocr.engines() == ocr.Engines(None, None, False)
        ocr._win_load = lambda: GoodWinOcr("ko")
        e = ocr.engines()
        assert isinstance(e.win, GoodWinOcr) and e.win_korean is True and e.tess is None
        ocr._win_load, ocr._tess_load = (lambda: BadWinOcr("ko")), (lambda: ocr._Tess(frozenset({"eng"})))
        e = ocr.engines()
        assert e.win is None and e.win_korean is False and e.tess.langs == frozenset({"eng"})
    finally:
        ocr._win_load, ocr._tess_load = saved


def test_empty_reread_keeps_windows_page():
    empty = lambda g, langs: OcrPage(200, 100, [], 0.0, 0, "tess-" + langs, "v1")  # noqa: E731
    win = Win(lambda g: page(ENGLISH))
    tess = Tess({"eng", "kor"}, empty)
    got, hint = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == " ".join(ENGLISH) and hint is False
    assert tess.calls == [("eng", limits.TESS_PSM_LATIN)]
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    tess = Tess({"eng", "kor"}, empty)
    got, hint = rp(G, ocr.Engines(win, tess, False), FAR, "picture")
    assert got.engine == "win-en-us" and text(got) == " ".join(JUNK) and hint is False
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR)]


def test_none_reread_keeps_windows_page():
    win = Win(lambda g: page(ENGLISH))
    tess = Tess({"eng", "kor"}, lambda g, langs: None)
    got, hint = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "win-ko" and text(got) == " ".join(ENGLISH) and hint is False
    assert tess.calls == [("eng", limits.TESS_PSM_LATIN)]
    win = Win(lambda g: page(JUNK, "win-en-us"), engine="win-en-us")
    tess = Tess({"eng", "kor"}, lambda g, langs: None)
    got, hint = rp(G, ocr.Engines(win, tess, False), FAR, "picture")
    assert got.engine == "win-en-us" and text(got) == " ".join(JUNK) and hint is False
    assert tess.calls == [("kor+eng", limits.TESS_PSM_KOR)]


def test_money_and_degrees_are_punctuation():
    assert ocr.junk_share(page("₩1,000 $5 30°".split())) == 0.0
    for c in "$|^°₩€":
        assert c in ocr.PUNCT, c


def test_judge_at_threshold():
    clean = Unread(0.0, None, 0.0)
    n = 10_000
    k = round(limits.OCR_JUNK * n)
    p = page(["가" * (n - k) + "ㄱ" * k])
    assert ocr.junk_share(p) == limits.OCR_JUNK  # exactly at it: not over
    assert ocr.judge(p, clean).kind == "trusted"
    assert ocr.judge(page(CLEAN_KO, "tess-kor+eng", conf=95.0), Unread(limits.OCR_UNREAD, None, 0.0)).kind == "trusted"
    p = page(CLEAN_KO * 5, "tess-kor+eng", conf=float(limits.OCR_CONF))
    assert sum(w.conf for w in p.words) / len(p.words) == limits.OCR_CONF  # exactly at it: not under
    assert ocr.judge(p, clean).kind == "trusted"

WHITE = Gray(200, 100, bytes([255]) * 20_000)  # the frame of `page`, no ink: a page read with no doubt
NOISY = Gray(200, 100, bytes(0 if i % 7 == 1 else 255 for i in range(20_000)))  # ink everywhere, turns differ


def answer(p: OcrPage) -> dict:
    return {"page": asdict(p), "rules": [], "unread": None, "hint": False}


def test_picture_verdict_by_the_words_it_keeps():
    def kind(p):
        return ocr._result_of(answer(p), "picture").verdict.kind
    assert kind(page(CLEAN_KO, "tess-kor+eng", conf=95.0)) == "trusted"
    assert kind(page(ENGLISH)) == "trusted"  # Windows: no confidence, no junk
    assert kind(page(CLEAN_KO, "tess-kor+eng", conf=50.0)) == "doubtful"
    low = page(["w%d" % i for i in range(20)], "tess-kor+eng", conf=86.0)
    low.words[3].conf = low.words[7].conf = 10.0
    assert kind(low) == "trusted"  # a readable picture under OCR_CONF and over OCR_LOW: its low words alone go
    assert ocr.picture_lines(low) == [" ".join("w%d" % i for i in range(20) if i not in (3, 7))], ocr.picture_lines(low)
    for i in range(11):
        low.words[i].conf = 10.0
    assert kind(low) == "doubtful"  # under half its words are left
    assert kind(page(["가나"] + JUNK)) == "doubtful"
    left = page(["good", "words"] + JUNK, "tess-kor+eng", conf=95.0)
    left.words[2].conf = 10.0
    assert kind(left) == "doubtful"  # the words left are junk past OCR_JUNK
    sloped = page(["가나", "다라", "마바"], "tess-kor+eng", conf=95.0)
    for i, w in enumerate(sloped.words):
        w.y += 20 * i
    assert kind(replace(sloped, psm=limits.TESS_PSM_KOR)) == "trusted"  # a label on a slope is a label


def test_picture_lines_leave_doubt_to_the_verdict():
    assert ocr.picture_lines(page(["hello"] + JUNK)) == ["hello " + " ".join(JUNK)]


def test_blank_picture_not_sent():
    assert ocr.blank(Gray(80, 80, bytes([200]) * 6400))
    assert ocr.blank(Gray(80, 80, bytes(200 + i % (limits.OCR_FLAT + 1) for i in range(6400))))
    one = bytearray([200]) * 6400
    one[1] = 0  # off the sparse sample: the whole bitmap is looked at
    assert not ocr.blank(Gray(80, 80, bytes(one)))
    assert not ocr.blank(Gray(80, 80, bytes(200 + i % (limits.OCR_FLAT + 2) for i in range(6400))))
    s = ocr.Session(time.monotonic() + 60, ("win-ko", "test"), cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    try:
        t = s.submit(Gray(80, 80, bytes([200]) * 6400), "picture", 96.0)
        assert s.wait(t).status == "blank" and s._state == "idle", (s.wait(t), s._state)
        t = s.submit(Gray(80, 80, bytes(one)), "picture", 96.0)
        assert s.wait(t).status != "blank"
        t = s.submit(Gray(80, 80, bytes([200]) * 6400), "page", 96.0)
        assert s.wait(t).status != "blank"  # a blank page is still read: the page verdict calls it blank
    finally:
        s.close()


def test_latin_windows_read_without_doubt_not_read_again():
    win = Win(lambda g: page(ENGLISH, "win-en-us"), engine="win-en-us")
    tess = Tess({"eng", "kor"}, lambda g, langs: page(ENGLISH, "tess-" + langs, conf=96))
    got, _ = rp(WHITE, ocr.Engines(win, tess, False), FAR, "page")
    assert got.engine == "win-en-us" and tess.calls == [], (got.engine, tess.calls)
    got, _ = rp(WHITE, ocr.Engines(win, tess, False), FAR, "picture")
    assert got.engine == "win-en-us" and tess.calls == [], (got.engine, tess.calls)
    got, _ = rp(NOISY, ocr.Engines(win, tess, False), FAR, "page")  # doubtful by its unread ink
    assert got.engine == "tess-eng" and tess.calls == [("eng", limits.TESS_PSM_LATIN)], tess.calls
    tess.calls.clear()
    got, _ = rp(WHITE, ocr.Engines(win, tess, True), FAR, "page")  # a Korean engine misreads Latin letters
    assert got.engine == "tess-eng" and tess.calls == [("eng", limits.TESS_PSM_LATIN)], tess.calls


def test_reading_carries_the_page_measures():
    win = Win(lambda g: page(CLEAN_KO))
    got = ocr.read_page(NOISY, ocr.Engines(win, None, True), FAR, "page", 200.0)
    assert got.unread is not None and got.unread.share > limits.OCR_UNREAD, got.unread
    assert ocr.judge(got.page, got.unread).kind == "doubtful"
    got = ocr.read_page(NOISY, ocr.Engines(win, None, True), FAR, "picture", 200.0)
    assert got.unread is None and got.rules == []


def test_junk_page_read_again_at_another_scale():
    win = Win(lambda g: page(CLEAN_KO if g.w == 2 * G.w else JUNK))
    tess = Tess({"kor", "eng"}, lambda g, langs: page(CLEAN_KO, "tess-" + langs, conf=50.0))
    got, _ = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert text(got) == " ".join(CLEAN_KO) and got.turn == 0 and got.engine == "win-ko", (text(got), got.engine)
    assert [(c.w, c.h) for c in win.calls] == [(4, 2), (4, 2), (2, 4), (2, 4), (8, 4)], win.calls
    assert win.calls[-1].data == bytes(b for row in (G.data[:4], G.data[4:]) for b in
                                       bytes(v for v in row for _ in (0, 1)) * 2)
    assert tess.calls == [], tess.calls  # the read at twice the scale has no doubt: no other engine
    assert (got.w, got.h) == (100, 50) and (got.words[0].x, got.words[0].w) == (5, 25), got


def test_junk_page_read_again_by_the_other_engine():
    win = Win(lambda g: page(JUNK))
    tess = Tess({"kor", "eng"}, lambda g, langs: page(CLEAN_KO, "tess-" + langs, conf=95.0))
    got, _ = rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert got.engine == "tess-kor+eng" and got.turn == 0, (got.engine, got.turn)
    assert len(win.calls) == 5 and tess.calls == [("kor+eng", limits.TESS_PSM_KOR)], (len(win.calls), tess.calls)
    tess = Tess({"kor", "eng"}, lambda g, langs: page(CLEAN_KO, "tess-" + langs, conf=50.0))
    got, _ = rp(G, ocr.Engines(Win(lambda g: page(JUNK)), tess, True), FAR, "page")
    assert got.engine == "win-ko" and got.turn == 0, got.engine  # both doubtful: the first read stays
    tess = Tess({"kor", "eng"}, lambda g, langs: page(JUNK, "tess-" + langs, conf=40.0))
    got, _ = rp(G, ocr.Engines(None, tess, False), FAR, "page")
    assert len(tess.calls) == 5 and got.engine == "tess-kor+eng"  # no Windows: no other engine


def test_doubtful_turns_rank_by_score():
    turned = {rotate(NOISY, t).data: t for t in (0, 90, 180, 270)}
    out = {0: ["가나다라마바사아자차카타파하" * 2, "ㅋ"], 180: JUNK, 90: ["가"], 270: JUNK}
    win = Win(lambda g: page(out.get(turned.get(g.data), JUNK)))
    got, _ = rp(NOISY, ocr.Engines(win, None, True), FAR, "page")
    assert got.turn == 0 and text(got) == " ".join(out[0]), (got.turn, text(got))  # every turn doubtful by its ink


def test_big_junk_page_not_read_at_another_scale():
    win = Win(lambda g: page(JUNK))
    saved = ocr.images._MAX_PIXELS
    ocr.images._MAX_PIXELS = 4 * G.w * G.h - 1
    try:
        rp(G, ocr.Engines(win, None, True), FAR, "page")
    finally:
        ocr.images._MAX_PIXELS = saved
    assert [(c.w, c.h) for c in win.calls] == [(4, 2), (4, 2), (2, 4), (2, 4)], win.calls


def test_other_engine_not_asked_for_a_read_it_already_gave():
    win = Win(lambda g: page(ENGLISH + JUNK))
    tess = Tess({"kor", "eng"}, lambda g, langs: page(ENGLISH + JUNK + ["more"], "tess-" + langs, conf=95.0))
    rp(G, ocr.Engines(win, tess, True), FAR, "page")
    assert len(win.calls) <= 5, len(win.calls)


def test_reread_stops_at_deadline():
    clock = [100.0]

    def junk(g):
        if len(win.calls) == 4:
            clock[0] = 111.0
        return page(JUNK)

    win = Win(junk)
    tess = Tess({"kor", "eng"})
    saved = ocr._now
    ocr._now = lambda: clock[0]
    try:
        rp(G, ocr.Engines(win, tess, True), 110.0, "page")
    finally:
        ocr._now = saved
    assert len(win.calls) == 4 and tess.calls == [], (len(win.calls), tess.calls)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
