"""ppt decryption: RC4 CryptoAPI persist objects and Pictures records, with the default password only.

The CurrentUser stream, the UserEditAtom and the PersistDirectoryAtom are never encrypted, so `ppt_records.Persist` reads
the persist chain first. The CryptSession10Container at `crypt_ref` holds the EncryptionVersionInfo, also clear. Every
other persist object, header and body, is one RC4 stream keyed by block = its persist id. The Pictures stream is not
laid out by persist ids: each BLIP's header, UIDs, tag or metafile header and image data are separate fields and each
restarts the keystream at block 0. The object rules and the BLIP field table follow dochan `crypto/ppt.py`
(`decrypt_presentation`, `_decrypt_pictures`); the check against overlapping objects and the byte budget are done here.
"""
import struct

from .. import limits
from ..containers import crypto, officecrypt
from ..limits import ReadError
from . import ppt_records as rec

MAX_OBJECTS = 100_000
# BLIP record type -> the instance values that carry one UID (and +1 two), as in [MS-ODRAW]
_BLIPS = {0xF01A: (0x3D4,), 0xF01B: (0x216,), 0xF01C: (0x542,), 0xF01D: (0x46A, 0x6E2), 0xF01E: (0x6E0,),
          0xF01F: (0x7A8,), 0xF029: (0x6E4,), 0xF02A: (0x6E2,)}
_METAFILE = (0xF01A, 0xF01B, 0xF01C)  # their 34-byte metafile header follows the UIDs; the others have a 1-byte tag


def unlock(doc: bytes, persist: rec.Persist, pictures: bytes | None) -> tuple[bytes, bytes | None] | None:
    """(Document stream, Pictures stream) decrypted with the default password; None when it does not verify.

    Raises ReadError("encrypted") for a missing or unknown (non-RC4) CryptSession, ReadError("too_large") over
    `limits.DECRYPT_BYTES` and ReadError("damaged") for persist objects that leave the stream or overlap. A Pictures
    stream that is not a run of BLIP records comes back as None, so its images are counted rather than the file lost.
    """
    if max(len(doc), len(pictures or b"")) > limits.DECRYPT_BYTES:
        raise ReadError("too_large", "part: PowerPoint Document")
    got = persist.obj(persist.crypt_ref) if persist.crypt_ref else None
    if got is None or got[0] != rec.CRYPT_SESSION10_CONTAINER:
        raise ReadError("encrypted")
    try:
        header = officecrypt.parse_rc4(doc[got[2]:got[2] + got[3]])
    except ValueError:
        raise ReadError("encrypted") from None
    keys = officecrypt.block_keys(header, officecrypt.PPT_DEFAULT)
    if keys is None:
        return None
    return _objects(doc, persist, keys), _blips(pictures, keys(0)) if pictures else pictures


def _objects(doc: bytes, persist: rec.Persist, keys) -> bytes:
    if len(persist.objects) > MAX_OBJECTS:
        raise ReadError("too_large", "persist objects")
    out = bytearray(doc)
    spans, total = [], 0
    for pid, at in persist.objects.items():
        if pid == persist.crypt_ref:
            continue
        if at < 0 or at + 8 > len(doc):
            raise ReadError("damaged", "persist object outside the stream")
        key = keys(pid)
        size = struct.unpack_from("<I", crypto.rc4(key, doc[at:at + 8]), 4)[0] + 8
        total += size
        if size > len(doc) - at or total > limits.DECRYPT_BYTES:
            raise ReadError("too_large" if total > limits.DECRYPT_BYTES else "damaged", "persist object size")
        spans.append((at, at + size))
        out[at:at + size] = crypto.rc4(key, doc[at:at + size])
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ReadError("damaged", "overlapping persist objects")
    return bytes(out)


def _blips(pictures: bytes, key: bytes) -> bytes | None:
    out = bytearray(pictures)
    pos, count = 0, 0
    while pos < len(pictures):
        count += 1
        if count > MAX_OBJECTS or len(pictures) - pos < 8:
            return None
        options, kind, size = struct.unpack("<HHI", crypto.rc4(key, pictures[pos:pos + 8]))
        if kind not in _BLIPS or size > len(pictures) - pos - 8:
            return None
        out[pos:pos + 8] = struct.pack("<HHI", options, kind, size)
        end, pos = pos + 8 + size, pos + 8
        instance = options >> 4
        uids = next((1 + instance - base for base in _BLIPS[kind] if instance in (base, base + 1)), 0)
        if not uids:
            return None
        for n in [16] * uids + [34 if kind in _METAFILE else 1, end - pos - 16 * uids - (34 if kind in _METAFILE else 1)]:
            if n < 0 or pos + n > end:
                return None
            out[pos:pos + n] = crypto.rc4(key, pictures[pos:pos + n])
            pos += n
    return bytes(out)
