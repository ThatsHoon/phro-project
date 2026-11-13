"""AES-128 (ECB) and the MSVC rand stream.

Run: python -I -X utf8 tests/test_reader_crypto.py
"""
import hashlib
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

sys.path.insert(0, str(HERE / "tests"))

from reader.containers.crypto import MsvcRand, aes128_ecb_decrypt, aes128_ecb_encrypt, rc4  # noqa: E402
from reader.containers.officecrypt import XLS_DEFAULT, block_keys, parse_rc4  # noqa: E402
from reader_fix import rc4_header  # noqa: E402

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


def test_rc4_vectors():
    assert rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"
    assert rc4(b"Wiki", b"pedia").hex() == "1021bf0420"
    assert rc4(b"Secret", b"Attack at dawn").hex() == "45a01f645fc35b383552544b9bf5"
    assert rc4(b"Key", b"") == b""


def test_rc4_skip():
    k, d = b"k3y", bytes(range(50))
    assert rc4(k, d, skip=5) == rc4(k, bytes(5) + d)[5:]


def test_rc4_md5_header():
    salt = bytes(range(16))
    raw = rc4_header(XLS_DEFAULT, cryptoapi=False, salt=salt)
    h = parse_rc4(raw)
    assert not h.cryptoapi and h.salt == salt
    key = block_keys(h, XLS_DEFAULT)
    pw = XLS_DEFAULT.encode("utf-16le")
    h0 = hashlib.md5(pw).digest()[:5]
    base = hashlib.md5((h0 + salt) * 16).digest()[:5]
    assert key(0) == hashlib.md5(base + struct.pack("<I", 0)).digest()
    assert block_keys(h, "nope") is None
    assert parse_rc4(b"junk" + raw, 4) == h


def test_cryptoapi_header():
    salt = bytes(range(16))
    h = parse_rc4(rc4_header("pw", cryptoapi=True, key_bits=128, salt=salt))
    assert h.cryptoapi and h.key_bits == 128
    key = block_keys(h, "pw")
    base = hashlib.sha1(salt + "pw".encode("utf-16le")).digest()
    assert key(3) == hashlib.sha1(base + struct.pack("<I", 3)).digest()[:16]
    assert block_keys(h, "no") is None


def test_cryptoapi_40bit():
    h = parse_rc4(rc4_header("pw", key_bits=40))
    assert h.key_bits == 40
    key = block_keys(h, "pw")
    assert len(key(0)) == 16 and key(0)[5:] == bytes(11)


def test_not_rc4():
    cases = [struct.pack("<HH", 4, 4) + bytes(100), bytes(10)]
    raw = bytearray(rc4_header("pw"))
    struct.pack_into("<I", raw, 20, 0x660E)       # algID of the EncryptionHeader
    cases.append(bytes(raw))
    for c in cases:
        try:
            parse_rc4(c)
        except ValueError:
            continue
        raise AssertionError("must raise ValueError")


def test_rc4_throughput():
    data = bytes(range(256)) * 16384         # 4 MiB
    start = time.perf_counter()
    out = b"".join(rc4(i.to_bytes(4, "little"), data[i * 1024:(i + 1) * 1024]) for i in range(len(data) // 1024))
    took = time.perf_counter() - start
    assert len(out) == len(data)
    print(f"rc4 {4 / took:.2f} MiB/s")
    assert took < 3, took


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
