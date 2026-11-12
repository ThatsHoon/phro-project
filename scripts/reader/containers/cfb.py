"""Compound File Binary (OLE2) reader, written from MS-CFB.

Every count, sector id and chain is bounds-checked before use. A chain may not loop, run past the file,
end short of the declared size or share a sector with another stream; those raise CfbError on read.
A file that cannot be opened at all raises ReadError("damaged", ...).
"""
import os
import struct
import sys
from array import array
from pathlib import Path

from ..limits import BIG_PART, CFB_CHAIN, CFB_DEPTH, CFB_ENTRIES, CFB_SECTORS, CFB_STREAM, ReadError

MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
FREESECT = NOSTREAM = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
MAXREG = 0xFFFFFFFA   # sector ids from here up are markers, never sectors
_STORAGE, _STREAM, _ROOT = 1, 2, 5
_MINI_SECTOR = 64
_MINI_CUTOFF = 4096


class CfbError(ValueError):
    """A chain or stream of an opened compound file is unreadable."""


def is_cfb(head: bytes) -> bool:
    return head[:8] == MAGIC


def _words(data) -> array:
    n = len(data) // 4 * 4
    a = array("I")
    a.frombytes(bytes(data[:n]))
    if sys.byteorder != "little":
        a.byteswap()
    return a


class Compound:
    """A compound file opened from a path or from bytes. Use as a context manager or call close()."""

    def __init__(self, src):
        self._f = None
        self._mv = None
        try:
            if isinstance(src, (bytes, bytearray, memoryview)):
                self._mv = memoryview(bytes(src))
                self._size = len(self._mv)
            else:
                self._f = open(Path(src), "rb")
                self._size = os.fstat(self._f.fileno()).st_size
            self._open()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None
        self._mv = None

    # ---- raw access
    def _read_at(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0:
            return b""
        if self._mv is not None:
            return bytes(self._mv[off:off + n])
        if self._f is None:
            raise CfbError("closed")
        self._f.seek(off)
        return self._f.read(n)

    def _sector(self, sid: int) -> bytes:
        return self._read_at((sid + 1) * self._ss, self._ss)

    # ---- open
    def _open(self):
        head = self._read_at(0, 512)
        if len(head) < 512 or head[:8] != MAGIC:
            raise ReadError("damaged", "not a compound file")
        major, = struct.unpack_from("<H", head, 0x1A)
        order, shift, mshift = struct.unpack_from("<HHH", head, 0x1C)
        n_fat, first_dir = struct.unpack_from("<II", head, 0x2C)
        first_mini, n_mini, first_difat, n_difat = struct.unpack_from("<IIII", head, 0x3C)
        if order != 0xFFFE or mshift != 6 or (major, shift) not in ((3, 9), (4, 12)):
            raise ReadError("damaged", "compound file header")
        self._ss = 1 << shift
        self._v3 = major == 3
        self._nsec = max(0, -(-(self._size - self._ss) // self._ss))
        if self._nsec > CFB_SECTORS:
            raise ReadError("damaged", "compound file header")
        self._owner = array("i", bytes(4 * self._nsec))  # sector id -> id of the chain that claimed it
        self._next_key = 1
        self._mini_owner = {}
        try:
            self._load_fat(head, n_fat, first_difat, n_difat)
        except CfbError:
            raise ReadError("damaged", "compound file header") from None
        self._first_mini, self._n_mini = first_mini, n_mini
        self._minifat = None
        self._root_chain = None
        try:
            self._load_dir(first_dir)
        except CfbError:
            raise ReadError("damaged", "compound file directory") from None

    def _load_fat(self, head, n_fat, first_difat, n_difat):
        ids = [s for s in struct.unpack_from("<109I", head, 0x4C) if s != FREESECT]
        per = self._ss // 4 - 1
        sid, seen = first_difat, set()
        for _ in range(min(n_difat, self._nsec)):
            if sid >= MAXREG:
                break
            if sid >= self._nsec or sid in seen:
                raise CfbError("difat")
            seen.add(sid)
            words = _words(self._sector(sid))
            ids.extend(w for w in words[:per] if w != FREESECT)
            sid = words[per] if len(words) > per else ENDOFCHAIN
        ids = ids[:n_fat]
        if len(ids) > self._nsec:
            raise CfbError("fat")
        fat = array("I")
        for s in ids:
            if s >= self._nsec:
                raise CfbError("fat sector out of range")
            fat.extend(_words(self._sector(s)))
        self._fat = fat

    def _chain(self, start: int, fat, nmax: int, owners, key: int) -> list:
        """The sector ids from start to ENDOFCHAIN; raises on a loop, a bad id or a cross-link."""
        out, seen = [], set()
        sid = start
        while sid != ENDOFCHAIN:
            if sid >= nmax or sid >= len(fat) or sid in seen:
                raise CfbError("chain")
            if len(out) >= CFB_CHAIN:
                raise CfbError("chain too long")
            if owners[sid] not in (0, key):
                raise CfbError("cross-linked chain")
            seen.add(sid)
            out.append(sid)
            sid = fat[sid]
        for s in out:
            owners[s] = key
        return out

    def _claim(self, start: int) -> list:
        key = self._next_key
        self._next_key += 1
        return self._chain(start, self._fat, self._nsec, self._owner, key)

    def _gather(self, chain: list, size: int) -> bytes:
        ss, parts, i = self._ss, [], 0
        while i < len(chain):
            j = i
            while j + 1 < len(chain) and chain[j + 1] == chain[j] + 1:
                j += 1
            parts.append(self._read_at((chain[i] + 1) * ss, (j - i + 1) * ss))
            i = j + 1
        data = b"".join(parts)
        if len(data) < size:
            raise CfbError("short stream")
        return data[:size]

    def _load_dir(self, first_dir: int):
        chain = self._claim(first_dir)
        if len(chain) * (self._ss // 128) > CFB_ENTRIES + self._ss // 128:
            raise CfbError("too many directory entries")
        raw = self._gather(chain, len(chain) * self._ss)
        n = min(len(raw) // 128, CFB_ENTRIES)
        if n < 1:
            raise CfbError("no directory")
        ents = []
        for i in range(n):
            e = raw[i * 128:(i + 1) * 128]
            nlen = min(struct.unpack_from("<H", e, 64)[0], 64)
            name = e[:max(nlen - 2, 0) // 2 * 2].decode("utf-16-le", "replace")
            left, right, child = struct.unpack_from("<III", e, 68)
            start = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<Q", e, 120)[0]
            if self._v3:
                size &= 0xFFFFFFFF
            ents.append((name, e[66], left, right, child, start, size))
        if ents[0][1] != _ROOT:
            raise CfbError("no root entry")
        self._ents = ents
        self._root = ents[0]
        self._index = {}   # upper-cased path -> entry id
        self._paths = []   # (path, entry id), in-order
        seen = {0}
        stack = [("visit", ents[0][4], "", 1)]
        while stack:
            op, sid, prefix, depth = stack.pop()
            if op == "visit":
                if sid >= n or sid in seen:
                    continue
                seen.add(sid)
                stack.append(("visit", ents[sid][3], prefix, depth))
                stack.append(("enter", sid, prefix, depth))
                stack.append(("visit", ents[sid][2], prefix, depth))
            else:
                name, kind, _, _, child, _, _ = ents[sid]
                path = prefix + name
                if kind == _STREAM:
                    self._paths.append((path, sid))
                    self._index.setdefault(path.upper(), sid)
                elif kind == _STORAGE and depth < CFB_DEPTH:
                    stack.append(("visit", child, path + "/", depth + 1))

    # ---- public
    def names(self) -> list:
        return [p for p, _ in self._paths]

    def _find(self, name: str) -> int:
        sid = self._index.get(name.upper())
        if sid is None:
            raise KeyError(name)
        return sid

    def exists(self, name: str) -> bool:
        return name.upper() in self._index

    def size(self, name: str) -> int:
        return self._ents[self._find(name)][6]

    def read(self, name: str) -> bytes:
        eid = self._find(name)
        start, size = self._ents[eid][5], self._ents[eid][6]
        if size > CFB_STREAM:
            raise ReadError("too_large", f"{BIG_PART}: {name}")
        if size == 0:
            return b""
        if size < _MINI_CUTOFF:
            return self._read_mini(start, size, eid)
        chain = self._claim(start)
        if len(chain) * self._ss < size:
            raise CfbError("short chain")
        return self._gather(chain, size)

    # ---- mini stream
    def _load_mini(self):
        if self._minifat is not None:
            return
        mf = array("I")
        if self._n_mini and self._first_mini < MAXREG:
            for s in self._claim(self._first_mini):
                mf.extend(_words(self._sector(s)))
        root_start = self._root[5]
        self._root_chain = self._claim(root_start) if root_start < MAXREG else []
        self._minifat = mf

    def _read_mini(self, start: int, size: int, eid: int) -> bytes:
        try:
            self._load_mini()
        except CfbError:
            self._minifat = None
            raise
        mf = self._minifat
        out, seen, sid = [], set(), start
        while sid != ENDOFCHAIN:
            if sid >= len(mf) or sid in seen or len(out) > _MINI_CUTOFF // _MINI_SECTOR + 1:
                raise CfbError("mini chain")
            if self._mini_owner.setdefault(sid, eid) != eid:
                raise CfbError("cross-linked mini chain")
            seen.add(sid)
            out.append(sid)
            sid = mf[sid]
        need = -(-size // _MINI_SECTOR)
        if len(out) < need:
            raise CfbError("short mini chain")
        per = self._ss // _MINI_SECTOR
        parts = []
        for m in out[:need]:
            idx, within = divmod(m, per)
            if idx >= len(self._root_chain):
                raise CfbError("mini sector outside the mini stream")
            parts.append(self._read_at((self._root_chain[idx] + 1) * self._ss + within * _MINI_SECTOR, _MINI_SECTOR))
        data = b"".join(parts)
        if len(data) < size:
            raise CfbError("short mini stream")
        return data[:size]
