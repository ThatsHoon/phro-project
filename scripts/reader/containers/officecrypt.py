"""Office RC4 encryption headers: RC4 with MD5 (1.1) and RC4 CryptoAPI (2|3|4.2).

xls FILEPASS and ppt CurrentUser tokens carry one of these. Ported from dochan's
`crypto/legacy.py`. AES and Agile headers are not handled and raise ValueError.
"""
import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Callable

from .crypto import rc4

XLS_DEFAULT = "VelvetSweatshop"
PPT_DEFAULT = "/01Hannes Ruescher/01"


@dataclass
class Rc4Header:
    cryptoapi: bool
    salt: bytes
    verifier: bytes
    verifier_hash: bytes
    key_bits: int


def parse_rc4(data: bytes, off: int = 0) -> Rc4Header:
    if off < 0 or len(data) - off < 4:
        raise ValueError("short encryption header")
    major, minor = struct.unpack_from("<HH", data, off)
    if (major, minor) == (1, 1):
        if len(data) - off < 52:
            raise ValueError("short RC4 header")
        b = off + 4
        return Rc4Header(False, bytes(data[b:b + 16]), bytes(data[b + 16:b + 32]),
                         bytes(data[b + 32:b + 48]), 40)
    if major in (2, 3, 4) and minor == 2:
        if len(data) - off < 12 + 32:
            raise ValueError("short CryptoAPI header")
        _flags, size = struct.unpack_from("<II", data, off + 4)
        if not 32 <= size <= 4096 or 12 + size + 60 > len(data) - off:
            raise ValueError("bad CryptoAPI header size")
        _f, _extra, alg, halg, bits = struct.unpack_from("<5I", data, off + 12)
        if alg not in (0, 0x6801) or halg not in (0, 0x8004):
            raise ValueError("not RC4 CryptoAPI")
        bits = bits or 40
        if bits < 40 or bits > 128 or bits % 8:
            raise ValueError("bad RC4 key size")
        v = off + 12 + size
        salt_size = struct.unpack_from("<I", data, v)[0]
        hash_size = struct.unpack_from("<I", data, v + 36)[0]
        if salt_size != 16 or hash_size != 20:
            raise ValueError("bad verifier size")
        return Rc4Header(True, bytes(data[v + 4:v + 20]), bytes(data[v + 20:v + 36]),
                         bytes(data[v + 40:v + 60]), bits)
    raise ValueError("not an RC4 Office encryption header")


def block_keys(h: Rc4Header, password: str) -> Callable[[int], bytes] | None:
    """Return key(block) when the verifier checks against `password`, else None."""
    pw = password.encode("utf-16le")
    if h.cryptoapi:
        base = hashlib.sha1(h.salt + pw).digest()
        n = h.key_bits // 8

        def key(b: int) -> bytes:
            k = hashlib.sha1(base + struct.pack("<I", b)).digest()[:n]
            return k + bytes(11) if h.key_bits == 40 else k

        digest, size = hashlib.sha1, 20
    else:
        h0 = hashlib.md5(pw).digest()[:5]
        base = hashlib.md5((h0 + h.salt) * 16).digest()[:5]

        def key(b: int) -> bytes:
            return hashlib.md5(base + struct.pack("<I", b)).digest()

        digest, size = hashlib.md5, 16
    clear = rc4(key(0), h.verifier + h.verifier_hash)
    if hmac.compare_digest(digest(clear[:16]).digest(), clear[16:16 + size]):
        return key
    return None
