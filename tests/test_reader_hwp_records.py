"""HWP record walk, bounded inflate, FileHeader and DocInfo.

Run: python -I -X utf8 tests/test_reader_hwp_records.py
"""
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits  # noqa: E402
from reader.containers import flate  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.formats import hwp_records as hr  # noqa: E402
from reader_fix import hwp_record, make_cfb, make_hwp  # noqa: E402


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def wstr(s):
    return u16(len(s)) + s.encode("utf-16-le")


def raw_deflate(b):
    c = zlib.compressobj(wbits=-15)
    return c.compress(b) + c.flush()


def test_record_header():
    big = bytes(range(256)) * 20
    data = hwp_record(67, 1, b"0123456789") + hwp_record(66, 2, big)
    assert len(big) == 5120
    ctx = Context()
    recs = hr.records(data, ctx)
    assert [(r.tag, r.level, len(r.data)) for r in recs] == [(67, 1, 10), (66, 2, 5120)]
    assert recs[0].data == b"0123456789" and recs[1].data == big
    assert not ctx.skipped


def test_record_truncated():
    data = hwp_record(67, 0, b"abcd") + hwp_record(67, 0, b"efgh")[:-2]
    ctx = Context()
    recs = hr.records(data, ctx)
    assert len(recs) == 1 and recs[0].data == b"abcd"
    assert ctx.skipped["record"] == 1
    ctx = Context()  # an extended size that claims more than is left
    hr.records(struct.pack("<II", 67 | (0xFFF << 20), 10**9) + b"x", ctx)
    assert ctx.skipped["record"] == 1


def test_record_cap():
    old = limits.HWP_RECORDS
    limits.HWP_RECORDS = 3
    try:
        ctx = Context()
        recs = hr.records(b"".join(hwp_record(67, 0, b"x") for _ in range(5)), ctx)
    finally:
        limits.HWP_RECORDS = old
    assert len(recs) == 3
    assert ctx.skipped["record"] == 1 and ctx.warnings


def test_inflate_raw_and_zlib():
    payload = b"hello hwp " * 100
    assert flate.inflate(raw_deflate(payload), flate.Budget()) == payload
    assert flate.inflate(zlib.compress(payload), flate.Budget()) == payload


def test_inflate_ratio():
    packed = raw_deflate(bytes(8 * 2**20))
    try:
        flate.inflate(packed, flate.Budget())
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith("part:")


def test_inflate_total():
    packed = raw_deflate(bytes(800))
    budget = flate.Budget(total=1000)
    assert flate.inflate(packed, budget) == bytes(800)
    try:
        flate.inflate(packed, budget)
        raise AssertionError("expected ReadError")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith("total:")


def test_inflate_garbage_is_not_a_readerror():
    try:
        flate.inflate(b"\xff\xfe\xfd\xfc" * 8, flate.Budget())
        raise AssertionError("expected ValueError")
    except ReadError:
        raise AssertionError("garbage should be counted by ctx.guard, not fail the file")
    except ValueError:
        pass


def test_inflate_truncated_raises_with_prefix():
    payload = bytes(range(256)) * 800
    c = zlib.compressobj(level=0, wbits=-15)  # stored blocks: a cut leaves an exact prefix
    packed = c.compress(payload) + c.flush()
    for whole in (packed, zlib.compress(payload, 0)):
        cut = whole[:len(whole) // 2]
        try:
            flate.inflate(cut, flate.Budget())
            raise AssertionError("expected Truncated")
        except flate.Truncated as e:
            assert isinstance(e, ValueError) and not isinstance(e, ReadError)
            assert 0 < len(e.data) < len(payload) and payload.startswith(e.data)
    assert flate.inflate(packed, flate.Budget()) == payload


def header_of(path):
    with Compound(path) as cf:
        return hr.file_header(cf)


def test_file_header_flags():
    h = header_of(make_hwp(tmp() / "a.hwp", [b""], flags=0b10111))
    assert h.version == "5.0.3.0"
    assert h.compressed and h.password and h.distribution and h.drm
    assert not (h.cert_encrypted or h.cert_drm or h.track_changes)
    h = header_of(make_hwp(tmp() / "b.hwp", [b""], flags=(1 << 8) | (1 << 10) | (1 << 14)))
    assert h.cert_encrypted and h.cert_drm and h.track_changes and not h.compressed


def test_not_hwp_signature():
    p1 = tmp() / "z.hwp"
    make_cfb(p1, {"FileHeader": bytes(256)})
    p2 = tmp() / "m.hwp"
    make_cfb(p2, {"Other": b"x"})
    for path in (p1, p2):
        try:
            header_of(path)
            raise AssertionError("expected ReadError")
        except ReadError as e:
            assert e.kind == "damaged"


def doc_info_data():
    bin_data = hwp_record(hr.BIN_DATA, 0, u16(1) + u16(1) + wstr("png"))
    cs = bytearray(72)
    struct.pack_into("<I", cs, 42, 1000)
    struct.pack_into("<I", cs, 46, 0b10)
    plain = bytearray(72)
    ps = bytearray(54)
    struct.pack_into("<I", ps, 0, (1 << 23) | (1 << 25))  # outline, level 1 (0-based)
    struct.pack_into("<H", ps, 30, 4)
    style = wstr("개요 2") + wstr("Outline 2") + bytes([0, 0]) + struct.pack("<hHH", 1042, 7, 9)
    return b"".join([
        hwp_record(hr.DOCUMENT_PROPERTIES, 0, u16(2) + bytes(24)),
        bin_data,
        hwp_record(hr.CHAR_SHAPE, 1, bytes(plain)),
        hwp_record(hr.CHAR_SHAPE, 1, bytes(cs)),
        hwp_record(hr.PARA_SHAPE, 1, bytes(ps)),
        hwp_record(hr.STYLE, 1, style),
    ])


def test_doc_info_items():
    ctx = Context()
    info = hr.doc_info(doc_info_data(), ctx)
    assert info.sections == 2
    b = info.bins[1]
    assert (b.kind, b.id, b.ext, b.compress) == ("embed", 1, "png", 0)
    assert info.bold == [False, True]
    assert info.para[0].head == 1 and info.para[0].level == 1 and info.para[0].ref == 4
    st = info.styles[0]
    assert (st.name, st.local, st.para, st.char) == ("개요 2", "Outline 2", 7, 9)
    assert not ctx.skipped


def test_doc_info_bin_kinds_and_compress():
    data = (hwp_record(hr.BIN_DATA, 0, u16(0)) +
            hwp_record(hr.BIN_DATA, 0, u16(1 | (2 << 4)) + u16(5) + wstr("jpg")) +
            hwp_record(hr.BIN_DATA, 0, u16(2) + u16(6)))
    info = hr.doc_info(data, Context())
    assert info.bins[1].kind == "link"
    assert (info.bins[2].kind, info.bins[2].id, info.bins[2].compress) == ("embed", 5, 2)
    assert (info.bins[3].kind, info.bins[3].id) == ("storage", 6)


def test_doc_info_numbering_and_bullet():
    heads = b""
    for i in range(7):
        attr = (1 << 5) if i == 1 else 0   # level 2 uses format code 1 (circled digit)
        heads += struct.pack("<IhhI", attr, 0, 0, 0) + wstr(f"^{i + 1}.")
    heads += u16(1) + b"".join(struct.pack("<I", n + 1) for n in range(7))
    bullet = bytes(12) + u16(0x25CF)
    data = hwp_record(hr.NUMBERING, 0, heads) + hwp_record(hr.BULLET, 0, bullet) + \
        hwp_record(hr.BULLET, 0, bytes(12) + u16(0))
    ctx = Context()
    info = hr.doc_info(data, ctx)
    n = info.numberings[0]
    assert n.formats == [f"^{i + 1}." for i in range(7)]
    assert n.kinds[0] == "DIGIT" and n.kinds[1] == "CIRCLED_DIGIT"
    assert n.starts == [1, 2, 3, 4, 5, 6, 7]
    assert info.bullets == ["●", "•"]
    sur = hr.doc_info(hwp_record(hr.BULLET, 0, bytes(12) + u16(0xD800)), Context()).bullets
    assert sur == ["□"]
    sur[0].encode("utf-8")
    assert not ctx.skipped


def test_doc_info_bad_record_skipped():
    cs = bytearray(72)
    struct.pack_into("<I", cs, 46, 0b10)
    data = (hwp_record(hr.CHAR_SHAPE, 1, b"abc") + hwp_record(hr.CHAR_SHAPE, 1, bytes(cs)) +
            hwp_record(hr.STYLE, 1, wstr("본문") + wstr("Normal") + bytes([0, 0]) + struct.pack("<hHH", 0, 0, 1)))
    ctx = Context()
    info = hr.doc_info(data, ctx)
    assert ctx.skipped["styles"] == 1
    assert info.bold == [False, True]  # the bad record keeps its place, so ids stay aligned
    assert info.styles[0].name == "본문" and info.styles[0].char == 1


def test_doc_info_hostile_string_length():
    # a name length that runs past the record is a damaged record, not an overread
    ctx = Context()
    info = hr.doc_info(hwp_record(hr.STYLE, 1, u16(60000) + b"ab"), ctx)
    assert ctx.skipped["styles"] == 1 and len(info.styles) == 1


def test_make_hwp_roundtrip():
    p = make_hwp(tmp() / "r.hwp", [hwp_record(66, 0, b"a")], doc_info=doc_info_data(),
                 bins={"BIN0001.png": b"PNG"}, extra={"Scripts/x": b"s"})
    with Compound(p) as cf:
        assert cf.exists("BinData/BIN0001.png") and cf.exists("Scripts/x")
        h = hr.file_header(cf)
        assert h.compressed
        info = hr.doc_info(flate.inflate(cf.read("DocInfo"), flate.Budget()), Context())
        assert info.bold == [False, True]
        recs = hr.records(flate.inflate(cf.read("BodyText/Section0"), flate.Budget()), Context())
        assert recs[0].tag == 66


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
