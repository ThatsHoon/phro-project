"""ppt decryption: RC4 CryptoAPI persist objects and Pictures records, with the default password only.

The CurrentUser stream, the UserEditAtom and the PersistDirectoryAtom are never encrypted, so `ppt_records.Persist` reads
the persist chain first. The CryptSession10Container at `crypt_ref` holds the EncryptionVersionInfo, also clear. Every
other persist object, header and body, is one RC4 stream keyed by block = its persist id. The Pictures stream is not
laid out by persist ids: each BLIP's header, UIDs, tag or metafile header and image data are separate fields and each
restarts the keystream at block 0. The object rules and the BLIP field table follow dochan `crypto/ppt.py`
(`decrypt_presentation`, `_decrypt_pictures`); the check against overlapping objects and the byte budget are done here.
"""
import bisect
import struct

from .. import limits, officeart
from ..containers import crypto, officecrypt
from ..limits import ReadError
from . import ppt_records as rec

MAX_OBJECTS = 100_000  # over it the file is `too_large`, named: a ReadError beats dochan's silent failure
# BLIP record type -> the instance values that carry one UID (and +1 two), as in [MS-ODRAW]
_BLIPS = {0xF01A: (0x3D4,), 0xF01B: (0x216,), 0xF01C: (0x542,), 0xF01D: (0x46A, 0x6E2), 0xF01E: (0x6E0,),
          0xF01F: (0x7A8,), 0xF029: (0x6E4,), 0xF02A: (0x6E2,)}
_METAFILE = (0xF01A, 0xF01B, 0xF01C)  # their 34-byte metafile header follows the UIDs; the others have a 1-byte tag


def unlock(doc: bytes, persist: rec.Persist, pictures: bytes | None) -> tuple[bytes, bytes | None] | None:
    """(Document stream, Pictures stream) decrypted with the default password; None when it does not verify.

    Raises ReadError("encrypted") for a missing or unknown (non-RC4) CryptSession and ReadError("too_large") over
    `limits.DECRYPT_BYTES`, or over MAX_OBJECTS persist objects (dochan fails on such a file; this reader names the
    cause). A persist entry that leaves the stream or overlaps an object already taken is skipped and removed from
    `persist.objects`, so that persist id is missing as it would be for an id the directory never listed: the reader
    never reads a record out of another object's bytes through it, and the object it stood for is counted where it is
    wanted (a slide under `slide`). A Pictures stream is decrypted record by record; a record that is not a clean BLIP
    stays as it was (its picture is counted under `image`) and the walk goes on at the next BLIP the BStore names
    (an FBSE's foDelay), so the pictures behind it are read.
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
    clear = _objects(doc, persist, keys)
    return clear, _blips(pictures, keys(0), _delays(clear, persist)) if pictures else pictures


def _delays(doc: bytes, persist: rec.Persist) -> list[int]:
    """The foDelay offsets, sorted, of the delay-loaded BLIPs the BStore of the (decrypted) `doc` names."""
    off = persist.objects.get(persist.doc_ref)
    head = officeart.header(doc, off) if off is not None else None
    if head is None or head[2] != rec.DOCUMENT_CONTAINER or head[3] > len(doc) - off - 8:
        return []
    for _, _, typ, body, size in officeart.records(doc, off + 8, off + 8 + head[3]):
        if typ == rec.PP_DRAWING_GROUP:
            dgg = bytes(doc[body:body + size])
            found = (officeart.fbse_delay(dgg, at) for at in officeart.bstore(dgg))
            return sorted({at for at in found if at is not None})
    return []


def _objects(doc: bytes, persist: rec.Persist, keys) -> bytes:
    """`doc` with every persist object decrypted; the entries of `persist.objects` that cannot be are removed."""
    if len(persist.objects) > MAX_OBJECTS:
        raise ReadError("too_large", "persist objects")
    out = bytearray(doc)
    taken, total = 0, 0  # end of the last object taken: entries go by offset, so an overlap starts before it
    for at, pid in sorted((at, pid) for pid, at in persist.objects.items()):
        if pid == persist.crypt_ref:
            continue
        if at < taken or at + 8 > len(doc):
            del persist.objects[pid]
            continue
        key = keys(pid)
        size = struct.unpack_from("<I", crypto.rc4(key, doc[at:at + 8]), 4)[0] + 8
        if size > len(doc) - at:
            del persist.objects[pid]
            continue
        total += size
        if total > limits.DECRYPT_BYTES:
            raise ReadError("too_large", "part: PowerPoint Document")
        out[at:at + size] = crypto.rc4(key, doc[at:at + size])
        taken = at + size
    return bytes(out)


def _blips(pictures: bytes, key: bytes, resume: list[int]) -> bytes:
    """The stream decrypted record by record. A record that does not frame cleanly stays as it was (it fails to read
    later and is counted there) and the walk goes on at the first offset of `resume` (the BStore's foDelay values, the
    only starts a bad record's size cannot be trusted to give) after the bad record's start; with none the rest of the
    stream stays as it was."""
    out = bytearray(pictures)
    pos, count = 0, 0
    while pos < len(pictures):
        start = pos
        count += 1
        if count > MAX_OBJECTS:
            break
        pos = _blip(pictures, out, start, key)
        if pos is None:
            i = bisect.bisect_right(resume, start)
            if i == len(resume):
                break
            pos = resume[i]
    return bytes(out)


def _blip(pictures: bytes, out: bytearray, start: int, key: bytes) -> int | None:
    """Decrypt the BLIP at `start` into `out`; the offset after it, or None (nothing written) when it does not frame."""
    if len(pictures) - start < 8:
        return None
    options, kind, size = struct.unpack("<HHI", crypto.rc4(key, pictures[start:start + 8]))
    if kind not in _BLIPS or size > len(pictures) - start - 8:
        return None
    instance = options >> 4
    uids = next((1 + instance - base for base in _BLIPS[kind] if instance in (base, base + 1)), 0)
    end, pos = start + 8 + size, start + 8
    tag = 34 if kind in _METAFILE else 1
    fields = [16] * uids + [tag, end - pos - 16 * uids - tag]
    if not uids or min(fields) < 0:
        return None
    out[start:start + 8] = struct.pack("<HHI", options, kind, size)
    for n in fields:
        out[pos:pos + n] = crypto.rc4(key, pictures[pos:pos + n])
        pos += n
    return end
