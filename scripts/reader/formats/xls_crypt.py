"""xls FILEPASS decryption: XOR obfuscation (type 0) and RC4 / RC4 CryptoAPI (type 1), with the default password only.

The XOR password check and array, the clear-record list and the per-record array index follow dochan
`crypto/legacy.py` (`_xor_array_bytes`, `decrypt_xls_workbook`). The XOR pass works on whole record bodies: the body is
rotated through a `bytes.translate` table and XORed with the array in one `int.from_bytes` operation, so no byte loop
runs. RC4 keys one keystream per 1024-byte block of the stream (the block number is the stream offset // 1024), never
per record. Offsets and record sizes stay the same, so BOUNDSHEET offsets still point at the right place.
"""
import struct

from .. import limits
from ..containers import crypto, officecrypt
from ..limits import ReadError
from ..containers.officecrypt import XLS_DEFAULT
from . import biff

# Records whose bodies stay clear ([MS-XLS] 2.2.10), as in dochan's `clear_records`.
CLEAR = frozenset({0x0809, 0x0009, 0x0209, 0x0409, 0x002F, 0x0194, 0x0195, 0x00E1, 0x0196, 0x0138})
_BOUNDSHEET = 0x0085
_ROT_LEFT = bytes(((v << 3) | (v >> 5)) & 0xFF for v in range(256))     # decrypting: rotate left by 3
_ROT_RIGHT = bytes(((v >> 3) | (v << 5)) & 0xFF for v in range(256))    # encrypting (tests): rotate right by 3
_INITIAL = (0xE1F0, 0x1D0F, 0xCC9C, 0x84C0, 0x110C, 0x0E10, 0xF1CE, 0x313E, 0x1872, 0xE139, 0xD40F, 0x84F9,
            0x280C, 0xA96A, 0x4EC3)
_SEEDS = (0xAEFC, 0x7B61, 0x4563, 0x0375, 0xD849, 0x6F45, 0xEB23, 0x47D3, 0xB861, 0x45A0, 0xAA51, 0x76B4,
          0x3730, 0x3331, 0x1021)
_PAD = bytes.fromhex("bbffffbaffffb98000be0f00bf0f00")


def _matrix() -> list:
    out = []
    for value in _SEEDS:
        for _ in range(7):
            out.append(value)
            value = ((value << 1) ^ (0x1021 if value & 0x8000 else 0)) & 0xFFFF
    return out


_MATRIX = _matrix()


def _raw(password: str) -> bytes:
    """The password as the 1 to 15 single bytes the XOR scheme uses (the low byte of each UTF-16 unit, else the high)."""
    if not 1 <= len(password) <= 15:
        raise ValueError("XOR passwords hold 1 to 15 characters")
    enc = password.encode("utf-16le", errors="surrogatepass")
    return bytes(lo or hi for lo, hi in zip(enc[0::2], enc[1::2]))


def xor_verifier(password: str) -> int:
    raw = _raw(password)
    v = 0
    for byte in raw[::-1]:
        v = ((v << 1) & 0x7FFF) | (v >> 14)
        v ^= byte
    v = ((v << 1) & 0x7FFF) | (v >> 14)
    return v ^ len(raw) ^ 0xCE4B


def xor_key(password: str) -> int:
    raw = _raw(password)
    key = _INITIAL[len(raw) - 1]
    index = 104
    for byte in raw[::-1]:
        for _ in range(7):
            if byte & 0x40:
                key ^= _MATRIX[index]
            byte <<= 1
            index -= 1
    return key


def xor_array(password: str, key: int) -> bytes:
    """The 16-byte XOR array: the padded password XORed with the key bytes, each rotated right by 1."""
    values = (_raw(password) + _PAD)[:16]
    out = []
    for i, v in enumerate(values):
        v ^= (key >> (8 * (i % 2))) & 0xFF
        out.append(((v >> 1) | (v << 7)) & 0xFF)
    return bytes(out)


def _records(data: bytes, start: int):
    """(id, header offset, body end) of each raw record from `start`; CONTINUE counts as a record of its own."""
    out = []
    pos, n = start, len(data)
    while pos + 4 <= n:
        rid, size = struct.unpack_from("<HH", data, pos)
        end = pos + 4 + size
        if end > n:
            raise ReadError("damaged", "truncated record in an encrypted workbook")
        out.append((rid, pos, end))
        if len(out) > limits.BIN_RECORDS:
            raise ReadError("damaged", "too many records")
        pos = end
    return out


def decrypt(data: bytes, filepass: biff.Rec, password: str = XLS_DEFAULT) -> bytes | None:
    """The stream decrypted with `password`, or None when it does not verify.

    Raises ReadError("encrypted") for an encryption this reader does not know and ReadError("too_large") over
    `limits.DECRYPT_BYTES`. The FILEPASS record stays in the result.
    """
    if len(data) > limits.DECRYPT_BYTES:
        raise ReadError("too_large", "part: Workbook")
    off, size = filepass[1], filepass[2]
    if size < 2 or off + size > len(data):
        raise ReadError("encrypted")
    kind = struct.unpack_from("<H", data, off)[0]
    start = off + size  # everything before this is clear
    if kind == 0:
        if size != 6:
            raise ReadError("encrypted")
        key, verifier = struct.unpack_from("<HH", data, off + 2)
        try:
            if xor_verifier(password) != verifier or xor_key(password) != key:
                return None
            array = xor_array(password, key)
        except ValueError:
            return None
        return _xor(data, start, array)
    if kind == 1:
        try:
            header = officecrypt.parse_rc4(data[off + 2:off + size])
        except ValueError:
            raise ReadError("encrypted") from None
        keys = officecrypt.block_keys(header, password)
        if keys is None:
            return None
        return _rc4(data, start, keys)
    raise ReadError("encrypted")


def _xor(data: bytes, start: int, array: bytes) -> bytes:
    out = bytearray(data)
    rot = array.translate(_ROT_LEFT)
    for rid, pos, end in _records(data, start):
        body = pos + 4
        n = end - body
        if rid in CLEAR or not n:
            continue
        shift = end % 16  # array index of the body's first byte (dochan: end + offset in body)
        ks = (rot * ((shift + n) // 16 + 2))[shift:shift + n]
        x = int.from_bytes(data[body:end].translate(_ROT_LEFT), "little") ^ int.from_bytes(ks, "little")
        out[body:end] = x.to_bytes(n, "little")
        if rid == _BOUNDSHEET:
            out[body:min(body + 4, end)] = data[body:min(body + 4, end)]
    return bytes(out)


def _rc4(data: bytes, start: int, keys) -> bytes:
    n = len(data)
    parts = [data[:start]]
    pos = start
    while pos < n:
        block = pos // 1024
        stop = min((block + 1) * 1024, n)
        parts.append(crypto.rc4(keys(block), data[pos:stop], skip=pos - block * 1024))
        pos = stop
    out = bytearray(b"".join(parts))
    for rid, p, end in _records(data, start):
        out[p:p + 4] = data[p:p + 4]
        if rid in CLEAR:
            out[p + 4:end] = data[p + 4:end]
        elif rid == _BOUNDSHEET:
            out[p + 4:min(p + 8, end)] = data[p + 4:min(p + 8, end)]
    return bytes(out)
