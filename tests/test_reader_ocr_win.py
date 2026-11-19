"""Windows OCR through ctypes WinRT: the Gray type, one fixture page read and compared with its text and with the
PowerShell spike's result, word boxes, the deadline, the engine id and version, and the recognizer listing.

Run: python -I -X utf8 tests/test_reader_ocr_win.py
The engine tests skip unless Windows OCR with the `ko` recognizer is present.
"""
import sys
from pathlib import Path
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.ocr_pixels import Gray  # noqa: E402
from reader import ocr_win  # noqa: E402
from ocr_fix import cer, load, spike, truth  # noqa: E402

KO = "ko-malgun-11.150dpi"
_engine = []


def engine():
    """One shared WinOcr("ko"), or None when Windows OCR with the ko recognizer is not here."""
    if not _engine:
        try:
            eng = ocr_win.WinOcr("ko") if sys.platform == "win32" else None
        except OSError:
            eng = None
        _engine.append(eng if eng is not None and eng.engine == "win-ko" else None)
    return _engine[0]


def needs_ko(fn):
    def wrapped():
        if engine() is None:
            print(f"skip: {fn.__name__} (needs Windows OCR ko)")
            return
        fn()
    wrapped.__name__ = fn.__name__
    return wrapped


def by_line(page) -> str:
    lines: dict[int, list[str]] = {}
    for w in page.words:
        lines.setdefault(w.line, []).append(w.text)
    return "\n".join(" ".join(words) for _, words in sorted(lines.items()))


_pages = {}


def read(name):
    if name not in _pages:
        _pages[name] = engine().recognize(load(name), monotonic() + 30)
    return _pages[name]


def test_gray_size_checked():
    for args in ((2, 2, b"\0" * 3), (0, 0, b""), (-1, -1, b"\0"), (2, 2, b"\0" * 5)):
        try:
            Gray(*args)
        except ValueError:
            continue
        raise AssertionError(f"Gray{args[:2]} with {len(args[2])} bytes was accepted")
    assert Gray(2, 2, b"\0" * 4).data == b"\0" * 4


@needs_ko
def test_reads_korean_fixture():
    page = read(KO)
    assert page is not None
    got = cer(by_line(page), truth(KO))
    assert got <= 0.03, got


@needs_ko
def test_matches_spike_result():
    got = cer(by_line(read(KO)), spike(KO))
    assert got <= 0.005, got


@needs_ko
def test_boxes_inside_frame():
    g = load(KO)
    page = read(KO)
    assert (page.w, page.h) == (g.w, g.h)
    assert page.words and page.turn == 0 and all(w.conf is None for w in page.words)
    for w in page.words:
        assert w.w > 0 and w.h > 0, w
        assert 0 <= w.x and w.x + w.w <= page.w and 0 <= w.y and w.y + w.h <= page.h, w


@needs_ko
def test_fitted_image_boxes_scale_back():
    """Past MaxImageDimension the image is fitted down and the boxes come back in the frame's own pixels."""
    g = load(KO)
    eng = engine()
    old = eng.max_dim
    eng.max_dim = 1000  # the 1754-pixel page goes in at half size
    try:
        page = eng.recognize(g, monotonic() + 30)
    finally:
        eng.max_dim = old
    assert page is not None and (page.w, page.h) == (g.w, g.h) and page.words
    assert max(w.y + w.h for w in page.words) > 1000  # scaled back, not left in the 877-row frame
    for w in page.words:
        assert 0 <= w.x and w.x + w.w <= page.w and 0 <= w.y and w.y + w.h <= page.h, w


@needs_ko
def test_deadline_cancels():
    g = load(KO)
    t = monotonic()
    assert engine().recognize(g, monotonic() - 1) is None
    assert monotonic() - t < 1.0
    again = engine().recognize(g, monotonic() + 30)
    assert again is not None and again.words


@needs_ko
def test_operations_overlap():
    """Several operations started before any finishes all complete: one OcrEngine refuses a second page while busy
    (E_ABORT), so each operation runs on an engine of its own."""
    g = load(KO)
    want = by_line(read(KO))
    ops = [engine().start(g) for _ in range(3)]
    deadline = monotonic() + 30
    while not all(op.done() for op in ops):
        assert monotonic() < deadline
        sleep(0.005)
    for op in ops:
        assert by_line(op.result()) == want


@needs_ko
def test_engine_id_and_version():
    eng = engine()
    assert eng.engine == "win-ko"
    parts = eng.version.split("/")
    assert len(parts) == 2, eng.version
    for part in parts:
        size, mtime = part.split(":")
        assert int(size) > 0 and int(mtime) > 0, eng.version
    assert eng.max_dim > 0


@needs_ko
def test_recognizers_lists_ko():
    assert "ko-kr" in ocr_win.recognizers()


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
