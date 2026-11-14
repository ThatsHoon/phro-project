"""The pdf standard security handler: RC4 and AES decryption with the empty user password.

Port of dochan 1.21.0 pdf/crypto.py (MIT) to ISO 32000-1 section 7.6 (V1, V2, V4 with RC4 or
AESV2, V5 with R5 and R6 AESV3). The AES and RC4 primitives come from `containers.crypto`.
A user password the empty one does not match is `encrypted`; a public-key or other handler
is `encrypted` or `drm`. Each handler decrypts at most DECRYPT_TOTAL bytes.
"""
import hashlib
import stringprep
import struct
import unicodedata
from typing import Optional, Union

from ..containers.crypto import aes_cbc_decrypt, aes_cbc_encrypt, rc4
from ..limits import ReadError

# decision 4: bytes one handler decrypts in all (Task 19 measures it)
DECRYPT_TOTAL = 16 * 2**20

# The standard padding string (algorithm 2)
_PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])


class StandardSecurityHandler:
    """Key derivation and per-object decryption."""

    def __init__(self, encrypt: dict, doc_id: bytes, password: Union[str, bytes] = ""):
        self.v = encrypt.get("V", 0)
        self.r = encrypt.get("R", 0)
        if not isinstance(self.v, int) or not isinstance(self.r, int):
            raise ReadError("damaged", "encryption dictionary")
        if self.r not in (2, 3, 4, 5, 6) or self.v not in (1, 2, 4, 5):
            raise ReadError("encrypted", f"security handler V{self.v} R{self.r}")
        length = encrypt.get("Length", 40)
        self.length_bits = 256 if self.v == 5 else length
        if self.v == 4 and "Length" not in encrypt:
            self.length_bits = self._v4_filter_key_bits(encrypt)
        self.o = _as_bytes(encrypt.get("O"))
        self.u = _as_bytes(encrypt.get("U"))
        self.oe = _as_bytes(encrypt.get("OE"))
        self.ue = _as_bytes(encrypt.get("UE"))
        p = encrypt.get("P", 0)
        self.p = (p if isinstance(p, int) else 0) & 0xFFFFFFFF
        self.encrypt_metadata = encrypt.get("EncryptMetadata", True) is not False
        self.doc_id = doc_id
        self.used = 0  # bytes decrypted so far
        self._validate()
        self._cipher = self._detect_cipher(encrypt, "StmF")
        self._string_cipher = self._detect_cipher(encrypt, "StrF")
        self.key = self._compute_key(_password_bytes(password, self.r))
        if self.key is None:
            raise ReadError("encrypted", "user password")

    @staticmethod
    def _v4_filter_key_bits(encrypt: dict) -> int:
        """The key length of the active filters, for a V4 dictionary without /Length."""
        lengths = set()
        filters = encrypt.get("CF")
        for field in ("StmF", "StrF"):
            name = str(encrypt.get(field, "Identity"))
            if name == "Identity":
                continue
            entry = filters.get(name) if isinstance(filters, dict) else None
            if not isinstance(entry, dict):
                raise ReadError("encrypted", "unsupported crypt filter")
            cipher = str(entry.get("CFM", "None"))
            if cipher in ("None", "Identity"):
                continue
            if cipher not in ("V2", "AESV2"):
                raise ReadError("encrypted", "unsupported V4 crypt filter")
            length = entry.get("Length", 16 if cipher == "AESV2" else 5)
            if (type(length) is not int or not 5 <= length <= 16
                    or cipher == "AESV2" and length != 16):
                raise ReadError("damaged", "crypt filter key length")
            lengths.add(length * 8)
        if len(lengths) > 1:
            raise ReadError("encrypted", "crypt filters of different key lengths")
        return next(iter(lengths)) if lengths else 40

    def _validate(self) -> None:
        if self.r >= 5:
            if (self.v != 5 or len(self.o) < 48 or len(self.u) < 48
                    or len(self.oe) != 32 or len(self.ue) != 32):
                raise ReadError("damaged", "AES-256 encryption dictionary")
        elif (not isinstance(self.length_bits, int) or not 40 <= self.length_bits <= 128
              or self.length_bits % 8 or len(self.o) < 32 or len(self.u) < 32):
            raise ReadError("damaged", "RC4 or AES-128 encryption dictionary")

    def _detect_cipher(self, encrypt: dict, field: str) -> str:
        """rc4, aesv2, aesv3 or identity, for streams (StmF) or strings (StrF)."""
        if self.v < 4:
            return "rc4"
        name = str(encrypt.get(field, "Identity"))
        if name == "Identity":
            return "identity"
        cf = encrypt.get("CF")
        entry = cf.get(name) if isinstance(cf, dict) else None
        if isinstance(entry, dict):
            cfm = str(entry.get("CFM", ""))
            if cfm == "AESV3":
                return "aesv3"
            if cfm == "AESV2":
                return "aesv2"
            if cfm == "V2":
                return "rc4"
            if cfm in ("None", "Identity"):
                return "identity"
        raise ReadError("encrypted", "unsupported crypt filter")

    def _compute_key(self, password: bytes) -> Optional[bytes]:
        if self.r >= 5:
            return self._compute_key_r5(password)
        return self._compute_key_rc4(password)

    def _compute_key_rc4(self, password: bytes, owner_retry: bool = True) -> Optional[bytes]:
        """Algorithm 2 (R2-R4): the file key, checked against /U."""
        n = self.length_bits // 8 if self.v >= 2 else 5
        md = hashlib.md5()
        md.update((password + _PAD)[:32])
        md.update(self.o[:32])
        md.update(struct.pack("<I", self.p))
        md.update(self.doc_id)
        if self.r >= 4 and not self.encrypt_metadata:
            md.update(b"\xff\xff\xff\xff")
        key = md.digest()
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:n]).digest()
        key = key[:n]
        if self._check_user(key):
            return key
        if owner_retry:  # the password may be the owner's: recover the user password
            user = self._owner_user_password(password)
            if user is not None:
                return self._compute_key_rc4(user, owner_retry=False)
        return None

    def _check_user(self, key: bytes) -> bool:
        if self.r == 2:
            return rc4(key, _PAD) == self.u[:32]
        digest = hashlib.md5(_PAD + self.doc_id).digest()
        result = rc4(key, digest)
        for i in range(1, 20):
            result = rc4(bytes(b ^ i for b in key), result)
        return result[:16] == self.u[:16]

    def _owner_user_password(self, owner_password: bytes) -> Optional[bytes]:
        """Algorithm 7: the user password hidden in /O."""
        n = self.length_bits // 8 if self.v >= 2 else 5
        key = hashlib.md5((owner_password + _PAD)[:32]).digest()
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key).digest()
        key = key[:n]
        user = self.o[:32]
        if self.r == 2:
            return rc4(key, user)
        for i in range(19, -1, -1):
            user = rc4(bytes(b ^ i for b in key), user)
        return user

    def _compute_key_r5(self, password: bytes) -> Optional[bytes]:
        """Algorithm 2.A (R5, R6): the file key from /UE or /OE."""
        pw = password[:127]
        if self._hash(pw, self.u[32:40], b"") == self.u[:32]:
            return aes_cbc_decrypt(self._hash(pw, self.u[40:48], b""), b"\0" * 16, self.ue)
        if self._hash(pw, self.o[32:40], self.u[:48]) == self.o[:32]:
            return aes_cbc_decrypt(self._hash(pw, self.o[40:48], self.u[:48]),
                                   b"\0" * 16, self.oe)
        return None

    def _hash(self, password: bytes, salt: bytes, extra: bytes) -> bytes:
        """Algorithm 2.B (R6); R5 is a single SHA-256."""
        k = hashlib.sha256(password + salt + extra).digest()
        if self.r == 5:
            return k
        round_num = 0
        while True:
            k1 = (password + k + extra) * 64
            e = aes_cbc_encrypt(k[:16], k[16:32], k1)
            # 256 = 1 (mod 3), so the first 16 bytes' sum has the big-endian number's residue
            mod = sum(e[:16]) % 3
            k = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[mod](e).digest()
            round_num += 1
            if round_num >= 64 and e[-1] <= round_num - 32:
                return k[:32]

    def decrypt(self, num: int, gen: int, data: bytes, string: bool = False) -> bytes:
        """The plain bytes of a stream (or string) of object `num gen`."""
        cipher = self._string_cipher if string else self._cipher
        if cipher == "identity" or not data:
            return data
        # A refused piece consumes nothing, so a smaller one after it still decrypts.
        if self.used + len(data) > DECRYPT_TOTAL:
            raise ReadError("too_large", "part: decrypt budget")
        if cipher == "aesv3":
            out = _aes_decrypt(self.key, data)
        else:
            obj_key = self._object_key(num, gen, cipher == "aesv2")
            out = _aes_decrypt(obj_key, data) if cipher == "aesv2" else rc4(obj_key, data)
        self.used += len(data)
        return out

    def _object_key(self, num: int, gen: int, aes: bool) -> bytes:
        """Algorithm 1."""
        md = hashlib.md5()
        md.update(self.key)
        md.update(struct.pack("<I", num & 0xFFFFFFFF)[:3])
        md.update(struct.pack("<I", gen & 0xFFFFFFFF)[:2])
        if aes:
            md.update(b"sAlT")
        return md.digest()[:min(len(self.key) + 5, 16)]


def open_handler(encrypt: dict, id0: bytes, password: Union[str, bytes] = "") -> StandardSecurityHandler:
    """The handler of an /Encrypt dictionary, opened with `password` (empty by default)."""
    name = str(encrypt.get("Filter", ""))
    if name == "Adobe.PubSec":
        raise ReadError("encrypted", "public-key handler")
    if name != "Standard":
        raise ReadError("drm", f"security handler {name}")
    return StandardSecurityHandler(encrypt, id0 if isinstance(id0, bytes) else b"", password)


def _aes_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-CBC: the first 16 bytes are the IV; valid PKCS#7 padding is stripped."""
    if len(data) < 32:
        return b""
    iv, body = bytes(data[:16]), bytes(data[16:])
    body = body[:len(body) - len(body) % 16]
    plain = aes_cbc_decrypt(key, iv, body)
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def _as_bytes(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("latin-1", errors="replace")
    return b""


def _password_bytes(password: Union[str, bytes], revision: int) -> bytes:
    """R2-R4: PDFDocEncoding; R5: UTF-8; R6: SASLprep, then UTF-8."""
    if isinstance(password, bytes):
        return password[:127] if revision >= 5 else password[:32]
    if not isinstance(password, str) or len(password) > 4096:
        raise ReadError("encrypted", "user password")
    try:
        if revision <= 4:
            return _encode_pdfdoc_password(password[:32])
        if revision == 6:
            password = _saslprep(password)
        return password.encode("utf-8")[:127]
    except (UnicodeError, ValueError):
        raise ReadError("encrypted", "user password") from None


def _encode_pdfdoc_password(value: str) -> bytes:
    """The single-byte PDFDocEncoding of ISO 32000-1 annex D, reversed."""
    encoding = {chr(i): i for i in range(256)
                if not 0x18 <= i <= 0x1f and not 0x7f <= i <= 0xa0 and i != 0xad}
    encoding.update({char: 0x18 + i for i, char in enumerate("˘ˇˆ˙˝˛˚˜")})
    encoding.update({char: 0x80 + i for i, char in enumerate(
        "•†‡…—–ƒ⁄‹›−‰„“"
        "”‘’‚™ﬁﬂŁŒŠŸŽıł"
        "œšž")})
    encoding["€"] = 0xa0
    try:
        return bytes(encoding[char] for char in value)
    except KeyError as exc:
        raise ValueError("a character outside PDFDocEncoding") from exc


def _saslprep(value: str) -> str:
    """RFC 4013: the Unicode 3.2 mapping, NFKC, prohibited characters and the bidi check."""
    mapped = "".join(" " if stringprep.in_table_c12(c) else c for c in value
                     if not stringprep.in_table_b1(c))
    normalized = unicodedata.ucd_3_2_0.normalize("NFKC", mapped)
    prohibited = (stringprep.in_table_a1, stringprep.in_table_c12,
                  stringprep.in_table_c21_c22, stringprep.in_table_c3,
                  stringprep.in_table_c4, stringprep.in_table_c5,
                  stringprep.in_table_c6, stringprep.in_table_c7,
                  stringprep.in_table_c8, stringprep.in_table_c9)
    if any(check(c) for c in normalized for check in prohibited):
        raise ValueError("a prohibited character")
    if any(stringprep.in_table_d1(c) for c in normalized):
        if (any(stringprep.in_table_d2(c) for c in normalized)
                or not stringprep.in_table_d1(normalized[0])
                or not stringprep.in_table_d1(normalized[-1])):
            raise ValueError("bidirectional characters out of order")
    return normalized
