"""DjVu's BZZ codec (ZP coder, MTF, inverse BWT): round trips, damaged and cut streams, the output caps, the ZP table.

Run: python -I -X utf8 tests/test_reader_bzz.py
     python -I -X utf8 tests/test_reader_bzz.py measure synthetic     (BZZ on 4 MiB of words: MiB, s, MiB/s)
     python -I -X utf8 tests/test_reader_bzz.py measure sample <path>  (djvu.parse_bytes on one file: pages, s, pages/s)
"""
import random
import sys
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits  # noqa: E402
from reader.containers import bzz  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from djvu_fix import bzz_encode  # noqa: E402

MIB = 2**20


def damaged(fn):
    try:
        fn()
    except ReadError as e:
        assert e.kind == "damaged" and e.detail.startswith("bzz: "), (e.kind, e.detail)
        return
    raise AssertionError("ReadError not raised")


def raises(fn, cls):
    try:
        fn()
    except cls:
        return
    raise AssertionError(f"{cls.__name__} not raised")


def words(seed, size):
    rng = random.Random(seed)
    vocab = [bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(1, 9))) for _ in range(300)]
    out = bytearray()
    while len(out) < size:
        out += rng.choice(vocab) + rng.choice((b" ", b" ", b" ", b"\n", b", "))
    return bytes(out[:size])


def test_round_trips():
    for data in (b"", b"a", random.Random(4).randbytes(100 * 1024)):
        assert bzz.decode(bzz_encode(data), bzz.BZZ_CHUNK) == data
    # the FSHIFT bits (the encoder writes 0 unless asked): each speed of the MTF frequency adaptation
    text = words(1, 20_000)
    for fshift in (0, 1, 2):
        assert bzz.decode(bzz_encode(text, fshift=fshift), bzz.BZZ_CHUNK) == text
    # 4 MiB of zeros codes to far under 1/200 of its size, so the ZIP_RATIO rule (chunks over 1 MiB of output) stops
    # it at the default ratio; the round trip raises the ratio for this one call (pre-flight ruling F1). The first
    # block is the largest allowed, BZZ_BLOCK symbols (its bytes and the marker); a 1-byte block follows
    zeros = bytes(4 * MIB)
    packed = bzz_encode(zeros, blocksize=bzz.BZZ_BLOCK - 1)
    raises(lambda: bzz.decode(packed, bzz.BZZ_CHUNK), bzz.Limit)
    saved = limits.ZIP_RATIO
    limits.ZIP_RATIO = 10**9
    try:
        assert bzz.decode(packed, bzz.BZZ_CHUNK) == zeros
    finally:
        limits.ZIP_RATIO = saved


def test_two_blocks_contexts_kept():
    # ten 1 KiB blocks: the 262 ZP contexts carry over from block to block, as the encoder's do
    data = words(2, 10 * 1024)
    assert bzz.decode(bzz_encode(data, blocksize=1024), bzz.BZZ_CHUNK) == data


def test_truncated():
    data = random.Random(4).randbytes(3000)
    packed = bzz_encode(data)
    raises(lambda: bzz.decode(packed[:-32], bzz.BZZ_CHUNK), bzz.Truncated)
    # within TAIL (16 bytes past the end) a cut is not called a truncation: this one is damaged. (A cut can still
    # end Truncated when the padding reads as a new block and the coder runs on past TAIL.)
    damaged(lambda: bzz.decode(packed[:-8], bzz.BZZ_CHUNK))
    # a valid stream's look-ahead reads a few padding bytes: Truncated starts exactly one byte past TAIL
    saved = bzz.TAIL
    try:
        need = 0
        while True:
            bzz.TAIL = need
            try:
                assert bzz.decode(packed, bzz.BZZ_CHUNK) == data
                break
            except bzz.Truncated:
                need += 1
        assert 0 < need <= saved, need
    finally:
        bzz.TAIL = saved
    # the coder starts from two bytes
    damaged(lambda: bzz.decode(b"\x00", bzz.BZZ_CHUNK))


def test_missing_marker_damaged():
    damaged(lambda: bzz.decode(bzz_encode(b"hello, world", eob=False), bzz.BZZ_CHUNK))


def test_bad_markerpos_damaged():
    data = b"hello, world"
    assert bzz.decode(bzz_encode(data, markerpos=None), bzz.BZZ_CHUNK) == data
    # the block is len(data) + 1 symbols, so MARKERPOS == blocksize leaves the block without its marker
    damaged(lambda: bzz.decode(bzz_encode(data, markerpos=len(data) + 1), bzz.BZZ_CHUNK))


def test_oversize_block_damaged():
    damaged(lambda: bzz.decode(bzz_encode(b"abc", size=bzz.BZZ_BLOCK + 1), bzz.BZZ_CHUNK))
    # the cap is read at call time
    saved = bzz.BZZ_BLOCK
    bzz.BZZ_BLOCK = 4
    try:
        assert bzz.decode(bzz_encode(b"abc"), bzz.BZZ_CHUNK) == b"abc"
        damaged(lambda: bzz.decode(bzz_encode(b"abcd"), bzz.BZZ_CHUNK))
    finally:
        bzz.BZZ_BLOCK = saved


def work_of(fn):
    """The `work` a failing decode reports (the symbols it decoded)."""
    try:
        fn()
    except (bzz.Limit, bzz.Truncated, ReadError) as e:
        return e.work
    raise AssertionError("no failure")


def test_failure_reports_its_work():
    # (final review B) every failure says how many symbols it decoded, so a caller can charge it: the review's probe,
    # a 13-byte stream that decodes 2^20 - 2 symbols from the padding and then has no EOB marker
    probe = bzz_encode(b"a" * (MIB - 3), eob=False)
    assert len(probe) < 16, len(probe)
    assert work_of(lambda: bzz.decode(probe, bzz.BZZ_CHUNK)) == MIB - 2
    # a missing marker (in the first block): that block's symbols, its 1,000 bytes and the marker's slot
    data = words(5, 3000)
    assert work_of(lambda: bzz.decode(bzz_encode(data, blocksize=1000, eob=False), bzz.BZZ_CHUNK)) == 1001
    # a Limit before the third block: the two blocks before it
    assert work_of(lambda: bzz.decode(bzz_encode(data, blocksize=1000), 2500)) == 2002
    # a cut stream: Truncated inside a block, with the symbols of that block decoded before the cut
    packed = bzz_encode(random.Random(4).randbytes(3000))
    got = work_of(lambda: bzz.decode(packed[:-32], bzz.BZZ_CHUNK))
    assert 0 < got < 3001, got
    # an oversize block, and a stream under two bytes: nothing decoded
    assert work_of(lambda: bzz.decode(bzz_encode(b"abc", size=bzz.BZZ_BLOCK + 1), bzz.BZZ_CHUNK)) == 0
    assert work_of(lambda: bzz.decode(b"\x00", bzz.BZZ_CHUNK)) == 0


def test_limit_and_ratio():
    raises(lambda: bzz.decode(bzz_encode(random.Random(3).randbytes(1000)), 100), bzz.Limit)
    zeros = bytes(2 * MIB)
    packed = bzz_encode(zeros, blocksize=len(zeros))
    assert len(packed) * limits.ZIP_RATIO < len(zeros)
    raises(lambda: bzz.decode(packed, bzz.BZZ_CHUNK), bzz.Limit)
    half = bytes(512 * 1024)
    assert bzz.decode(bzz_encode(half), bzz.BZZ_CHUNK) == half


def test_unbwt_memory():
    # the inverse BWT keeps its row links in a typed array: a block peaks under 6 bytes a row (4 for the links, 1 for
    # the output), so a BZZ_BLOCK block stays near 24 MiB; the list-based walk this replaced peaked near 46 bytes a
    # row. The block is 256 KiB because tracemalloc slows the walk several times over
    size = 256 * 1024
    bwt = bytearray(size)  # a block of zeros: its BWT is the bytes, then the marker
    tracemalloc.start()
    try:
        out = bzz._unbwt(bwt, size - 1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out == bytes(size - 1)
    assert peak < 6 * size, peak


def test_zp_table_spot_values():
    # (p, m, up, dn) rows of the ZP adaptation table (bzz.py holds djvu-zp's tables.rs); the values here were read from
    # DjVuLibre's libdjvu/ZPCodec.cpp `default_ztable` on 2026-11-16, as format knowledge
    t = bzz.ZP_TABLE
    assert t[0] == (0x8000, 0x0000, 84, 145)
    assert t[3] == (0x6BBD, 0x10A5, 5, 1)
    assert t[82] == (0x0001, 0x7FFF, 82, 80)
    assert t[83] == (0x5695, 0x0000, 9, 85)
    assert t[84] == (0x24EE, 0x0000, 86, 226)
    assert t[128] == (0x02E6, 0x0000, 56, 50)
    assert t[249] == (0x481A, 0x0000, 89, 143)
    assert t[250] == (0x481A, 0x0000, 230, 246)
    # the table ends at state 250: no up or dn leads past it, so the C table's five zero rows 251-255 are left out
    assert len(t) == 251 and all(up < 251 and dn < 251 for _, _, up, dn in t)


# -- measure: BZZ's speed on synthetic text, and djvu's on one file named at run time (never committed)

def measure_synthetic():
    """4 MiB of text-like words (random.Random(7)) BZZ-coded in memory, then bzz.decode timed."""
    data = words(7, 4 * MIB)
    packed = bzz_encode(data)
    t0 = time.perf_counter()
    out = bzz.decode(packed, bzz.BZZ_CHUNK)
    dt = time.perf_counter() - t0
    assert out == data
    mib = len(out) / MIB
    print(f"BZZ {mib:.2f} {dt:.2f} {mib / dt:.3f}", flush=True)


def measure_sample(path):
    """djvu.parse_bytes timed on one file (run with python -I; the file is only read)."""
    from reader.formats import djvu
    from reader.model import Context
    data = Path(path).read_bytes()
    counted = []
    run = djvu._Reader.run

    def counting(self, pages, short=False):  # the page list the reader walks, its length the file's pages
        counted.append(len(pages))
        return run(self, pages, short)

    djvu._Reader.run = counting
    try:
        ctx = Context()
        t0 = time.perf_counter()
        djvu.parse_bytes(data, ctx)
        dt = time.perf_counter() - t0
    finally:
        djvu._Reader.run = run
    pages = counted[0]
    print(f"DJVU {pages} {dt:.2f} {pages / dt:.2f}  {dict(ctx.skipped)}", flush=True)


def main():
    if sys.argv[1:] == ["measure", "synthetic"]:
        measure_synthetic()
        return
    if sys.argv[1:3] == ["measure", "sample"] and len(sys.argv) == 4:
        measure_sample(sys.argv[3])
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
