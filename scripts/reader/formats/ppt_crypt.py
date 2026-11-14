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

    Raises ReadError("encrypted") for a missing or unknown (non-RC4) CryptSession and ReadError("too_large") over
    `limits.DECRYPT_BYTES`. A persist entry that leaves the stream or overlaps an object already taken is skipped and
    its bytes stay as they were, so its record fails to parse later and is counted there. A Pictures stream comes back
    decrypted up to the first record that is not a clean BLIP; the images behind it are counted rather than lost whole.
    """
    for name, stream in (("PowerPoint Document", doc), ("Pictures", pictures)):
        if stream and len(stream) > limits.DECRYPT_BYTES:
            raise ReadError("too_large", "part: " + name)
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
    taken, total = 0, 0  # end of the last object taken: entries go by offset, so an overlap starts before it
    for at, pid in sorted((at, pid) for pid, at in persist.objects.items()):
        if pid == persist.crypt_ref or at < taken or at + 8 > len(doc):
            continue
        key = keys(pid)
        size = struct.unpack_from("<I", crypto.rc4(key, doc[at:at + 8]), 4)[0] + 8
        if size > len(doc) - at:
            continue
        total += size
        if total > limits.DECRYPT_BYTES:
            raise ReadError("too_large", "part: PowerPoint Document")
        out[at:at + size] = crypto.rc4(key, doc[at:at + size])
        taken = at + size
    return bytes(out)


def _blips(pictures: bytes, key: bytes) -> bytes:
    """The stream decrypted up to the first record that does not frame cleanly (cut off there)."""
    out = bytearray(pictures)
    pos, count = 0, 0
    while pos < len(pictures):
        start = pos
        count += 1
        if count > MAX_OBJECTS or len(pictures) - pos < 8:
            return bytes(out[:start])
        options, kind, size = struct.unpack("<HHI", crypto.rc4(key, pictures[pos:pos + 8]))
        if kind not in _BLIPS or size > len(pictures) - pos - 8:
            return bytes(out[:start])
        out[pos:pos + 8] = struct.pack("<HHI", options, kind, size)
        end, pos = pos + 8 + size, pos + 8
        instance = options >> 4
        uids = next((1 + instance - base for base in _BLIPS[kind] if instance in (base, base + 1)), 0)
        if not uids:
            return bytes(out[:start])
        tag = 34 if kind in _METAFILE else 1
        for n in [16] * uids + [tag, end - pos - 16 * uids - tag]:
            if n < 0 or pos + n > end:
                return bytes(out[:start])
            out[pos:pos + n] = crypto.rc4(key, pictures[pos:pos + n])
            pos += n
    return bytes(out)
