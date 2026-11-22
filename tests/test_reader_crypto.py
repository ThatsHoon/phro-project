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

from reader.containers.crypto import (  # noqa: E402
    MsvcRand, aes128_ecb_decrypt, aes128_ecb_encrypt, aes_cbc_decrypt, aes_cbc_encrypt,
    aes_ecb_decrypt, aes_ecb_encrypt, rc4)
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


def test_aes256_fips197():
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = aes_ecb_encrypt(bytes(range(32)), pt)
    assert ct.hex() == "8ea2b7ca516745bfeafc49904b496089", ct.hex()
    assert aes_ecb_decrypt(bytes(range(32)), ct) == pt
    ct = aes_ecb_encrypt(bytes(range(24)), pt)
    assert ct.hex() == "dda97ca4864cdfe06eaf70a0ec0d7191", ct.hex()
    assert aes_ecb_decrypt(bytes(range(24)), ct) == pt


def test_aes128_fips197_kept():
    assert aes128_ecb_encrypt(bytes(range(16)), PLAIN).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"


def test_cbc_sp800_38a():
    iv = bytes(range(16))
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    cases = (("2b7e151628aed2a6abf7158809cf4f3c", "7649abac8119b246cee98e9b12e9197d"),
             ("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4",
              "f58c4c04d6e5f1ba779eabfb5f7bfbd6"))
    for k, c in cases:
        key = bytes.fromhex(k)
        assert aes_cbc_encrypt(key, iv, pt).hex() == c
        assert aes_cbc_decrypt(key, iv, bytes.fromhex(c)) == pt
        four = bytes(range(64))
        assert aes_cbc_decrypt(key, iv, aes_cbc_encrypt(key, iv, four)) == four


def test_cbc_rejects_bad_sizes():
    key = bytes(16)
    for fn in (aes_cbc_encrypt, aes_cbc_decrypt):
        for args in ((key, bytes(8), bytes(16)), (key, bytes(16), bytes(17)), (bytes(20), bytes(16), bytes(16))):
            try:
                fn(*args)
            except ValueError:
                continue
            raise AssertionError("bad sizes must raise ValueError")


def test_cbc_cap():
    from unittest import mock
    from reader import limits
    from reader.limits import ReadError
    with mock.patch.object(limits, "DECRYPT_BYTES", 32):
        for fn in (aes_cbc_encrypt, aes_cbc_decrypt):
            try:
                fn(bytes(16), bytes(16), bytes(48))
            except ReadError as e:
                assert e.kind == "too_large", e.kind
            else:
                raise AssertionError("over DECRYPT_BYTES must raise ReadError too_large")


def test_cbc_decrypt_speed():
    key, iv = bytes(range(32)), bytes(16)
    data = bytes(range(256)) * 4096
    start = time.perf_counter()
    out = aes_cbc_decrypt(key, iv, data)
    took = time.perf_counter() - start
    assert len(out) == len(data)
    print(f"aes256 cbc decrypt {took:.2f} s per MiB")
    assert took < 2.0, took


def test_tables_built_on_first_use():
    import subprocess
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from reader.containers import crypto; "
            "n = crypto._tables.cache_info().currsize; crypto.aes_ecb_encrypt(bytes(16), bytes(16)); "
            "print(n, crypto._tables.cache_info().currsize)")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(HERE / "scripts")], capture_output=True, text=True)
    assert out.stdout.split() == ["0", "1"], (out.stdout, out.stderr)


def test_cbc_key_schedule_once_a_call():
    from reader.containers import crypto
    calls, real = [], crypto._round_keys
    crypto._round_keys = lambda key: calls.append(key) or real(key)
    try:
        key, iv = bytes(range(32)), bytes(range(16))
        data = bytes(range(64)) * 4
        enc = aes_cbc_encrypt(key, iv, data)
        assert len(calls) == 1, len(calls)
        assert aes_cbc_decrypt(key, iv, enc) == data and len(calls) == 2, len(calls)
    finally:
        crypto._round_keys = real


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


def test_rc4_of_the_system_matches_the_byte_loop():
    import random
    from reader.containers import crypto
    rnd = random.Random(7)
    for _ in range(200):
        key = bytes(rnd.randrange(256) for _ in range(rnd.choice((1, 5, 16, 40, 128))))
        data = bytes(rnd.randrange(256) for _ in range(rnd.choice((0, 1, 7, 300, 5000))))
        skip = rnd.choice((0, 0, 3, 1024))
        want = crypto._rc4_loop(key, data, skip)
        assert crypto.rc4(key, data, skip) == want, (key, len(data), skip)
        got = crypto._rc4_system(key, data, skip)
        assert got is None or got == want, (key, len(data), skip)
    if sys.platform == "win32":
        assert crypto._rc4_system(b"Key", b"Plaintext", 0).hex() == "bbf316e8d940af0ad3"


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


def test_rc4_size_cap():
    from unittest import mock
    from reader import limits
    from reader.limits import ReadError
    with mock.patch.object(limits, "DECRYPT_BYTES", 100):
        rc4(b"k", bytes(100))  # exactly the cap is read
        try:
            rc4(b"k", bytes(101))
        except ReadError as e:
            assert e.kind == "too_large", e.kind
        else:
            raise AssertionError("over DECRYPT_BYTES must raise ReadError too_large")


def test_cryptoapi_header_flags_and_bad_branches():
    raw = rc4_header("pw")
    assert struct.unpack_from("<I", raw, 4)[0] == 0x04          # outer flags: fCryptoAPI only, no fAES
    for off, val in ((8, 31), (8, 4097), (8, 10**6)):            # header size below 32, over 4096, past the data
        bad = bytearray(raw)
        struct.pack_into("<I", bad, off, val)
        try:
            parse_rc4(bytes(bad))
        except ValueError:
            continue
        raise AssertionError(f"header size {val} must raise ValueError")
    size = struct.unpack_from("<I", raw, 8)[0]
    for name, off, val in (("key size 32", 12 + 16, 32), ("key size 129", 12 + 16, 129), ("key size 44", 12 + 16, 44),
                           ("salt size", 12 + size, 8), ("hash size", 12 + size + 36, 16)):
        bad = bytearray(raw)
        struct.pack_into("<I", bad, off, val)
        try:
            parse_rc4(bytes(bad))
        except ValueError:
            continue
        raise AssertionError(name + " must raise ValueError")


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
