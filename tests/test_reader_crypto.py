"""AES-128 (ECB) and the MSVC rand stream.

Run: python -I -X utf8 tests/test_reader_crypto.py
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader.containers.crypto import MsvcRand, aes128_ecb_decrypt, aes128_ecb_encrypt  # noqa: E402

KEY = bytes(range(16))
PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_fips197_vector():
    assert aes128_ecb_encrypt(KEY, PLAIN) == CIPHER
    assert aes128_ecb_decrypt(KEY, CIPHER) == PLAIN


def test_two_blocks_and_tail():
    data = bytes(range(37))
    enc = aes128_ecb_encrypt(KEY, data)
    assert len(enc) == 37 and enc[32:] == data[32:] and enc[:32] != data[:32]
    assert aes128_ecb_decrypt(KEY, enc) == data
    assert aes128_ecb_decrypt(KEY, b"") == b""
    assert aes128_ecb_decrypt(KEY, b"abc") == b"abc"


def test_bad_key():
    for fn in (aes128_ecb_decrypt, aes128_ecb_encrypt):
        try:
            fn(KEY[:15], PLAIN)
        except ValueError:
            continue
        raise AssertionError("a 15-byte key must raise ValueError")


def test_msvc_rand():
    r = MsvcRand(1)
    assert [r() for _ in range(5)] == [41, 18467, 6334, 26500, 19169]


def test_throughput():
    data = bytes(range(256)) * 4096          # 1 MiB
    start = time.perf_counter()
    out = aes128_ecb_decrypt(KEY, data)
    took = time.perf_counter() - start
    assert len(out) == len(data)
    assert aes128_ecb_encrypt(KEY, out) == data
    print(f"aes128 decrypt {1 / took:.2f} MiB/s")
    assert took < 10, took


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
