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
    from reader import ocr_probe
    assert "ko-kr" in ocr_probe.recognizers()


@needs_ko
def test_fit_averages_keeps_text():
    """A page fitted to a quarter of its pixels by 2x2 averaging still reads: keeping every 2nd row and column instead
    measured CER 0.235 on this page."""
    from reader import ocr_pixels
    g = load(KO)
    small = ocr_pixels.fit(g, g.w * g.h // 4)
    assert (small.w, small.h) == (g.w // 2, g.h // 2)
    page = engine().recognize(small, monotonic() + 30)
    got = cer(by_line(page), truth(KO))
    assert got <= 0.12, got


@needs_ko
def test_start_fits_by_averaging():
    """start() fits past MaxImageDimension with ocr_pixels.fit, not by dropping rows and columns."""
    from reader import ocr_pixels
    g = load(KO)
    seen = []
    real = ocr_pixels.fit

    def spy(img, max_pixels, **kw):
        out = real(img, max_pixels, **kw)
        seen.append((img.w, img.h, max_pixels, kw.get("max_side"), out.w, out.h))
        return out
    eng = engine()
    old = eng.max_dim
    eng.max_dim = 1000
    ocr_pixels.fit = spy
    try:
        eng.start(g).cancel()
    finally:
        ocr_pixels.fit = real
        eng.max_dim = old
    assert seen and seen[-1][3] == 1000 and seen[-1][4:] == (g.w // 2, g.h // 2), seen


@needs_ko
def test_result_before_done_cancels():
    """result() on an operation still running cancels it before releasing it, so the recognition stops, and raises."""
    g = load(KO)
    calls = []
    real = ocr_win._call

    def spy(lib, obj, index, what, argtypes, *args):
        calls.append(what)
        return real(lib, obj, index, what, argtypes, *args)
    op = engine().start(g)
    ocr_win._call = spy
    try:
        op._status = lambda lib: ocr_win._STARTED  # as if read the instant after start
        try:
            op.result()
        except OSError as e:
            assert "not completed" in str(e), e
        else:
            raise AssertionError("result() of a running operation did not raise")
    finally:
        ocr_win._call = real
    assert "IAsyncInfo.Cancel" in calls, calls
    assert op.done() and engine().recognize(g, monotonic() + 30).words


@needs_ko
def test_version_without_orp():
    """A recognizer folder with no .orp file (or no folder at all) leaves "0:0" for that part, not a failed engine."""
    from reader import ocr_probe
    real = ocr_probe._recognizer_folder
    try:
        ocr_probe._recognizer_folder = lambda tag, deps=None: Path(__file__).parent / "fixtures"
        eng = ocr_win.WinOcr("ko")
        try:
            dll, orp = eng.version.split("/")
            assert orp == "0:0" and dll != "0:0", eng.version
        finally:
            eng.close()
        ocr_probe._recognizer_folder = lambda tag, deps=None: None
        eng = ocr_win.WinOcr("ko")
        eng.close()
        assert eng.version.endswith("/0:0"), eng.version
    finally:
        ocr_probe._recognizer_folder = real
    folder = real("ko")
    assert folder is not None and any(p.suffix.lower() == ".orp" for p in folder.iterdir())


def turned(g, degrees):
    """`g` turned by `degrees` about its centre (y down, nearest pixel, white outside): a page scanned askew."""
    import math
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    cx, cy = g.w / 2, g.h / 2
    out = bytearray(b"\xff" * (g.w * g.h))
    for y in range(g.h):
        dy = y - cy
        for x in range(g.w):
            dx = x - cx
            sx, sy = int(cx + dx * c + dy * s), int(cy - dx * s + dy * c)
            if 0 <= sx < g.w and 0 <= sy < g.h:
                out[y * g.w + x] = g.data[sy * g.w + sx]
    return Gray(g.w, g.h, bytes(out))


def test_word_boxes_turned_back_cover_skewed_ink():
    """I1: boxes given in the deskewed frame, turned back by the text angle, cover the ink they read; as given they
    leave much of it unread."""
    import math
    from reader import ocr_pixels
    w, h, angle = 2000, 1500, -2.0
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    buf = bytearray(b"\xff" * (w * h))
    boxes = []
    for y in range(200, 1300, 50):
        for x in range(150, 1800, 80):
            boxes.append((x, y, 60, 20))
            dx, dy = x + 30 - w / 2, y + 10 - h / 2
            px, py = round(w / 2 + dx * c - dy * s), round(h / 2 + dx * s + dy * c)
            for r in range(py - 8, py + 8):
                buf[r * w + px - 25:r * w + px + 25] = b"\x1e" * 50
    g = Gray(w, h, bytes(buf))
    thr = ocr_pixels.threshold(g)
    assert ocr_pixels.unread(g, boxes, [], thr).share > 0.3
    back = [ocr_win._unskew(x, y, x + bw, y + bh, angle, w, h) for x, y, bw, bh in boxes]
    assert ocr_pixels.unread(g, back, [], thr).share == 0.0
    assert ocr_win._unskew(10.2, 10.0, 60.0, 30.4, 0.0, w, h) == (10, 10, 50, 21)
    assert ocr_win._unskew(-5, -5, 60, 30, 0.0, w, h) == (0, 0, 60, 30)  # clipped to the frame


@needs_ko
def test_skewed_fixture_ink_boxes():
    """I1 on the engine: a page scanned 2 degrees askew comes back with its words in the deskewed frame (the layout
    keeps them) and `ink`, the same boxes in the page's pixels, which leave next to no ink unread."""
    from reader import ocr_pixels
    g = turned(load(KO), 2.0)
    page = engine().recognize(g, monotonic() + 30)
    assert page is not None and abs(abs(page.angle) - 2.0) < 0.5, page.angle
    assert page.ink is not None and len(page.ink) == len(page.words)
    thr = ocr_pixels.threshold(g)
    deskewed = ocr_pixels.unread(g, [(w.x, w.y, w.w, w.h) for w in page.words], [], thr).share
    back = ocr_pixels.unread(g, page.ink, [], thr).share
    assert deskewed > 0.1 and back < 0.02, (deskewed, back)
    straight = read(KO)
    assert straight.ink is None or straight.ink == [(w.x, w.y, w.w, w.h) for w in straight.words]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
