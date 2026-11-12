"""HWP 5.0 protection and change tracking: distribution documents (ViewText), DRM sniffing, certificate encryption,
tracked deletions, and the zero tail AES padding leaves after a record stream.

Run: python -I -X utf8 tests/test_reader_hwp_protect.py
"""
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.containers import flate  # noqa: E402
from reader.formats import hwp  # noqa: E402
from reader.formats import hwp_records as hr  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import hwp_para, hwp_record, make_dist_section, make_hwp  # noqa: E402

KEY = bytes.fromhex("8a2f04c1d37e5b9960aa13f2c84e0d71")
DIST, TRACK = 1 << 2, 1 << 14
INSERT, DELETE = 0x10 << 24, 0x11 << 24
HEAD = 4 + 256  # the DISTRIBUTE_DOC_DATA record in front of the ciphertext


def tmp():
    return Path(tempfile.mkdtemp())


def props(sections):
    return hwp_record(hr.DOCUMENT_PROPERTIES, 0, struct.pack("<H", sections) + bytes(24))


def deflate(b, level=6):
    c = zlib.compressobj(level, wbits=-15)
    return c.compress(b) + c.flush()


def dist_file(*views, flags=1 | DIST, body=()):
    """A distribution document: ViewText/Section<N> are `views` as given, BodyText/Section<N> are `body`."""
    extra = {f"ViewText/Section{i}": v for i, v in enumerate(views)}
    return make_hwp(tmp() / "d.hwp", list(body), doc_info=props(len(views)), flags=flags, extra=extra)


def tracked_file(body, view, flags=1 | TRACK):
    """A track-changes document: BodyText/Section0 is `body`, ViewText/Section0 `view` as given (None: no stream)."""
    extra = {"ViewText/Section0": view} if view is not None else {}
    return make_hwp(tmp() / "t.hwp", [body], doc_info=props(1), flags=flags, extra=extra)


def corrupt(view):
    """The same stream with its ciphertext scrambled: it decrypts to garbage."""
    return view[:HEAD] + bytes(b ^ 0x5A for b in view[HEAD:])


def parse(path):
    ctx = Context()
    return hwp.parse(Path(path), ctx), ctx


def md(path, hidden=False):
    doc, ctx = parse(path)
    return markdown.render(doc, tmp() / "assets", hidden, ctx.skipped)


def denied(path, kind):
    try:
        parse(path)
    except ReadError as e:
        assert e.kind == kind, e
        return e
    raise AssertionError(f"expected ReadError {kind}")


def value_error(fn):
    """fn() raises ValueError that is not flate.Truncated (which would be parsed as a cut stream)."""
    try:
        fn()
    except flate.Truncated as e:
        raise AssertionError(f"Truncated, not a plain ValueError: {e}")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---- distribution documents
def test_distribution_doc():
    p = dist_file(make_dist_section(hwp_para("배포용 본문"), KEY, 0x2468ACE1))
    assert md(p) == "배포용 본문\n", md(p)
    # the BodyText of a distribution document is not its text
    p = dist_file(make_dist_section(hwp_para("배포용 본문"), KEY, 7), body=[hwp_para("가짜 본문")])
    assert md(p) == "배포용 본문\n", md(p)


def test_distribution_key_offsets_and_padding():
    # seed & 0xF moves the key through offsets 4-19; uncompressed sections end in 0-14 bytes of zero padding
    for seed in (0, 0xF, 0x7FFFFFF3, 0xFFFFFFFF, 0x2468ACE6):
        for n in range(1, 9):
            text = "열쇠" * 4 + "가나다라마바사아"[:n]
            for compressed in (True, False):
                view = make_dist_section(hwp_para(text), KEY, seed, compressed)
                doc, ctx = parse(dist_file(view, flags=DIST | compressed))
                out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
                assert out == text + "\n" and not ctx.skipped, (seed, n, compressed, out, ctx.skipped)


def test_distribution_bad_section():
    good = make_dist_section(hwp_para("하나"), KEY, 5)
    bad = corrupt(make_dist_section(hwp_para("둘"), KEY, 6))
    out = md(dist_file(good, bad))
    assert out.splitlines()[0] == "(could not read: 1 section)", out
    assert "하나" in out and "둘" not in out, out


def test_distribution_all_bad():
    bad = [corrupt(make_dist_section(hwp_para(t), KEY, k)) for k, t in enumerate(("하나", "둘"))]
    e = denied(dist_file(*bad), "damaged")
    assert e.detail == "no readable section", e
    # a ViewText section without DISTRIBUTE_DOC_DATA, and a distribution document with only a BodyText
    denied(dist_file(deflate(hwp_para("평문"))), "damaged")
    denied(make_hwp(tmp() / "b.hwp", [hwp_para("본문")], doc_info=props(1), flags=1 | DIST), "damaged")


def test_distribution_truncated_section_counted():
    body = hwp_para("앞부분") + hwp_para("x" * 3000)
    packed = deflate(body, level=0)  # stored blocks: a cut keeps an exact prefix
    view = make_dist_section(packed[:len(packed) * 3 // 5], KEY, 3, compressed=False)  # encrypted as it is
    doc, ctx = parse(dist_file(view))
    assert ctx.skipped["record"] == 1 and not ctx.skipped["section"], ctx.skipped
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert out.startswith("(could not read: 1 damaged record)\n\n앞부분"), out


def test_distribution_one_budget():
    seen = []
    old = flate.inflate

    def spy(data, budget):
        seen.append(budget)
        return old(data, budget)
    flate.inflate = spy
    try:
        parse(dist_file(make_dist_section(hwp_para("a"), KEY, 1), make_dist_section(hwp_para("b"), KEY, 2)))
    finally:
        flate.inflate = old
    assert len(seen) == 3 and seen[0] is seen[1] is seen[2], seen  # DocInfo and two sections


def test_distribution_large_section():
    para = hwp_para("가나다라마바사아자차" * 50)
    view = make_dist_section(para * 1300, KEY, 77, compressed=False)
    assert len(view) - HEAD > 1.25 * 2**20, len(view)
    p = dist_file(view, flags=DIST)
    sizes = []
    old = hr.crypto.aes128_ecb_decrypt

    def spy(key, data):
        sizes.append(len(data))
        return old(key, data)
    hr.crypto.aes128_ecb_decrypt = spy
    try:
        out = tmp() / "out"
        t0 = time.monotonic()
        meta = reader.convert(p, out)
        took = time.monotonic() - t0
    finally:
        hr.crypto.aes128_ecb_decrypt = old
    assert took < 15, took
    assert meta["unread"] == [], meta
    assert (out / "out.md").read_text(encoding="utf-8").count("가나다라") == 1300 * 50
    # decrypted in bounded pieces, so memory stays a small multiple of the stream
    assert len(sizes) >= 2 and max(sizes) <= 2**20 and sum(sizes) == len(view) - HEAD, sizes


def test_view_text_contract():
    sec = hwp_para("값")
    budget = flate.Budget()
    assert hr.view_text(make_dist_section(sec, KEY, 9), True, budget) == sec
    assert hr.view_text(make_dist_section(sec, KEY, 9, False), False, budget) == sec + bytes(-len(sec) % 16)
    # DISTRIBUTE_DOC_DATA written with the extended size word is the same record
    view = make_dist_section(sec, KEY, 10)
    assert hr.view_text(struct.pack("<II", 28 | (0xFFF << 20), 256) + view[4:], True, budget) == sec
    good = make_dist_section(sec, KEY, 11)
    wrong_key = bytearray(good)
    wrong_key[4 + 4 + 11] ^= 1  # the first key byte
    cut = deflate(hwp_record(hr.CTRL_HEADER, 0, b"dces" * 400), level=0)
    for data, compressed in (
        (b"", True), (b"\x1c\x00", True), (struct.pack("<I", 28 | (0xFFF << 20)), True),
        (hwp_record(hr.PARA_HEADER, 0, bytes(256)) + good[HEAD:], True),   # not DISTRIBUTE_DOC_DATA
        (hwp_record(28, 0, bytes(255)) + good[HEAD - 1:], True),           # a payload that is not 256 bytes
        (good[:HEAD], True),                                               # no ciphertext
        (good[:-1], True),                                                 # not whole AES blocks
        (bytes(wrong_key), True),                                          # decrypts to garbage
        (make_dist_section(hwp_record(hr.CTRL_HEADER, 0, b"dces"), KEY, 12), True),  # not a paragraph first
        (make_dist_section(hwp_record(hr.CTRL_HEADER, 0, b"dces"), KEY, 12, False), False),
        (make_dist_section(cut[:len(cut) // 2], KEY, 13, False), True),    # cut, and not a paragraph first
    ):
        value_error(lambda: hr.view_text(data, compressed, budget))
    # a cut stream that does start with a paragraph is Truncated, carrying what inflated
    packed = deflate(hwp_para("x" * 2000), level=0)
    try:
        hr.view_text(make_dist_section(packed[:len(packed) // 2], KEY, 14, False), True, budget)
    except flate.Truncated as e:
        assert e.data[:4] == hwp_para("x")[:4] and len(e.data) > 1000, len(e.data)
    else:
        raise AssertionError("expected Truncated")


# ---- protection flags and DRM containers
def test_drm_sniff():
    filler = bytes(range(256)) * 4
    for head in (b"SCDSA", b"SCDSA004", b"\x9b DRMONE  This Document is encrypted", bytes(8) + b"SCDSA002"):
        p = tmp() / "x.hwp"
        p.write_bytes(head + filler)
        denied(p, "drm")
        try:
            reader.convert(p, tmp() / "out")
        except ReadError as e:
            assert e.kind == "drm", e
        else:
            raise AssertionError("expected drm")


def test_cert_encrypted():
    denied(make_hwp(tmp() / "c.hwp", [hwp_para("x")], flags=1 | (1 << 8)), "encrypted")
    denied(make_hwp(tmp() / "c.hwp", [hwp_para("x")], flags=1 | DIST | (1 << 8)), "encrypted")


# ---- tracked changes
def test_tracked_deletion():
    view = deflate(hwp_para("남은지운", ranges=((2, 4, DELETE | 1),)))
    p = tracked_file(hwp_para("남은"), view)  # BodyText holds the final text, ViewText the marked one
    out = md(p)
    assert out == "(1 hidden places left out)\n\n남은\n", out
    o = tmp() / "out"
    reader.convert(p, o, hidden=True)
    shown = (o / "out.md").read_text(encoding="utf-8")
    assert "남은(hidden: 지운)" in shown, shown


def test_tracked_insertion_stays():
    view = deflate(hwp_para("옛새글", ranges=((1, 2, INSERT | 1),)))
    assert md(tracked_file(hwp_para("옛새글"), view)) == "옛새글\n"


def test_tracked_deletion_inside_runs_and_fields():
    # bold on 나다; the deletion covers 다라: one hidden place, the bold run split at its edge
    view = deflate(hwp_para("가나다라마", chars=((0, 0), (1, 1), (3, 0)), ranges=((2, 4, DELETE | 1),)))
    di = props(1) + b"".join(hwp_record(hr.CHAR_SHAPE, 1, bytes(46) + struct.pack("<I", b) + bytes(22))
                             for b in (0, 2))
    p = make_hwp(tmp() / "t.hwp", [hwp_para("가나마")], doc_info=di, flags=1 | TRACK,
                 extra={"ViewText/Section0": view})
    assert md(p) == "(1 hidden places left out)\n\n가**나**마\n", md(p)
    assert md(p, hidden=True) == "가**나**(hidden: **다**라)마\n", md(p, hidden=True)
    # a hyperlink field that a deletion starts inside: the link keeps its visible part, the rest is hidden
    fffc = "￼"
    hlk = b"%hlk" + struct.pack("<I", 0) + b"\0" + struct.pack("<H", 13) + "https://e.com".encode("utf-16-le")
    units = "앞" + fffc + "링크" + fffc + "뒤"  # 앞 0, field begin 1-8, 링 9, 크 10, field end 11-18, 뒤 19
    view = deflate(hwp_para(units, controls=((3, hlk, b""), (4, b"%hlk", b"")), ranges=((10, 19, DELETE | 1),)))
    out = md(tracked_file(hwp_para("앞"), view))
    assert out == "(1 hidden places left out)\n\n앞[링](https://e.com)뒤\n", out


def test_tracked_hostile_ranges():
    # backwards, empty, past the end, overlapping, and a cut 12-byte entry: deletions are their union, clamped
    ranges = ((5, 2, DELETE), (0, 0, DELETE), (3, 0xFFFFFFFF, DELETE), (1, 3, DELETE | 7), (2, 4, DELETE),
              (0, 1, (0x22 << 24) | 1))  # an unknown kind is not a deletion
    tags = b"".join(struct.pack("<III", *r) for r in ranges) + b"\1\2"
    view = deflate(hwp_para("가나다라마") + hwp_record(hr.PARA_RANGE_TAG, 1, tags))  # a record of the paragraph
    doc, ctx = parse(tracked_file(hwp_para("가"), view))
    assert markdown.render(doc, tmp() / "a", False, ctx.skipped) == "(1 hidden places left out)\n\n가\n"


def test_many_ranges_are_linear():
    # every other unit of a 200,000-unit paragraph deleted: 100,000 ranges, more than PARA_HEADER's u16 count holds
    n = 200_000
    tags = b"".join(struct.pack("<III", 2 * k, 2 * k + 1, DELETE | 1) for k in range(n // 2))
    view = deflate(hwp_para("가" * n) + hwp_record(hr.PARA_RANGE_TAG, 1, tags))
    p = tracked_file(hwp_para("가"), view)
    t0 = time.monotonic()
    doc, ctx = parse(p)
    took = time.monotonic() - t0
    out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
    assert out == f"({n // 2} hidden places left out)\n\n" + "가" * (n // 2) + "\n"
    assert took < 5, took


def test_tracked_viewtext_fallback():
    # ViewText is read only when it inflates whole to a paragraph stream; otherwise BodyText is
    stored = deflate(hwp_para("보기") + hwp_para("x" * 3000), level=0)
    for view in (deflate(hwp_record(hr.CTRL_HEADER, 0, b"dces")),  # not a paragraph first
                 b"\xff" * 64,                                      # not deflate
                 stored[:len(stored) // 2],                         # cut
                 None):                                             # missing
        doc, ctx = parse(tracked_file(hwp_para("최종"), view))
        out = markdown.render(doc, tmp() / "a", False, ctx.skipped)
        assert out == "최종\n" and not ctx.skipped, (view, out, ctx.skipped)
    # without the track-changes flag a ViewText is not read at all
    assert md(tracked_file(hwp_para("최종"), deflate(hwp_para("보기")), flags=1)) == "최종\n"


# ---- record streams
def test_records_ignore_zero_tail():
    body = hwp_para("가") + hwp_para("나")
    want = [(r.tag, r.level, r.data) for r in hr.records(body, Context())]
    for k in range(1, 40):  # AES padding after the last record, of any length
        ctx = Context()
        got = hr.records(body + bytes(k), ctx)
        assert [(r.tag, r.level, r.data) for r in got] == want and not ctx.skipped, (k, ctx.skipped)
    assert hr.records(bytes(64), Context()) == []
    # a tail that is not all zero is still a damaged record
    for tail in (b"\0\0\1", b"\1", bytes(5) + b"\1"):
        ctx = Context()
        got = hr.records(body + tail, ctx)
        assert len(got) >= len(want) and ctx.skipped["record"] == 1, (tail, ctx.skipped)
    # zero header words with something after them are still records
    ctx = Context()
    got = hr.records(bytes(4) + body, ctx)
    assert [(r.tag, r.level, r.data) for r in got] == [(0, 0, b"")] + want and not ctx.skipped


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
