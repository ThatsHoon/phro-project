"""DjVu fixtures built in memory: a BZZ encoder and an IFF writer.

The encoder is ported from djvu-rs (MIT), the crates `djvu-bzz` 0.45.0 (`src/encode.rs`) and `djvu-zp` 0.45.0
(`src/encoder.rs`), commit a329e2ad3bd9f710fcfeb758939f58626a6eb69a; `scripts/reader/containers/bzz.py` decodes what
it writes. The forward BWT sorts by prefix doubling as encode.rs does, with Python's sort for the radix passes and a
closed form for a block of one repeated byte; a run of MTF position 0 in context 0 is one ZP run (encoder.rs
`encode_run`), which writes the same bytes. Knobs that write damaged streams: `eob`, `markerpos`, `size`.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader.containers.bzz import LPS_NEXT, MPS_NEXT, PROB, THRESHOLD  # noqa: E402

_CONTEXTS = 262


class _ZpEncoder:
    """djvu-zp `ZpEncoder`: the bits DjVuLibre's encoder emits, built as one value in `acc` (the newest `nacc`
    bits, starting from the 24-bit 0xFFFFFF that is never written); a borrow decrements bytes already written."""

    def __init__(self):
        self.a = 0
        self.subend = 0
        self.acc = 0xFFFFFF
        self.nacc = 24
        self.skip = 3
        self.out = bytearray()

    def bit(self, ctx: bytearray, i: int, bit: int):
        st = ctx[i]
        z = self.a + PROB[st]
        if bit != st & 1:
            self._lps(ctx, i, z)
        elif z >= 0x8000:
            self._mps(ctx, i, z)
        else:
            self.a = z

    def run(self, ctx: bytearray, i: int, bit: int, n: int):
        """`n` copies of `bit` in context i: the same bytes and state as n calls to `bit` (encoder.rs `encode_run`).
        MPS bits on the fast path only add p to a, so a run of them is one multiply-add."""
        while n > 0:
            st = ctx[i]
            p = PROB[st]
            if bit != st & 1:
                self._lps(ctx, i, self.a + p)
                n -= 1
                continue
            fast = max(0x7FFF - self.a, 0) // p
            if fast >= n:
                self.a += n * p
                return
            self.a += fast * p
            self._mps(ctx, i, self.a + p)
            n -= fast + 1

    def _mps(self, ctx, i, z):
        st = ctx[i]
        z = min(z, 0x6000 + ((z + self.a) >> 2))
        if (self.a & 0xFFFF) >= THRESHOLD[st]:
            ctx[i] = MPS_NEXT[st]
        self.a = z
        self._shift(1)

    def _lps(self, ctx, i, z):
        z = min(z, 0x6000 + ((z + self.a) >> 2))
        ctx[i] = LPS_NEXT[ctx[i]]
        z = 0x10000 - z
        self.subend += z
        self.a += z
        self._renormalize()

    def passthrough(self, bit: int):
        z = 0x8000 + (self.a >> 1)
        if not bit:
            self.a = z
            self._shift(1)
        else:
            z = 0x10000 - z
            self.subend += z
            self.a += z
            self._renormalize()

    def finish(self) -> bytes:
        if self.subend > 0x8000:
            self.subend = 0x10000
        elif self.subend > 0:
            self.subend = 0x8000
        if self.subend:
            self._shift(1)
        # drop the trailing ones of the last 24 bits: DjVuLibre flushes ones and never writes that buffer
        t = 0
        while t < 24 and (self.acc >> t) & 1:
            t += 1
        self.acc >>= t
        self.nacc -= t
        while self.nacc >= 8:
            self.nacc -= 8
            self._put((self.acc >> self.nacc) & 0xFF)
        if self.nacc > 0:
            pad = 8 - self.nacc
            self._put(((self.acc << pad) | ((1 << pad) - 1)) & 0xFF)
        while len(self.out) < 2:
            self.out.append(0xFF)
        return bytes(self.out)

    def _renormalize(self):
        k = 16 - (self.a ^ 0xFFFF).bit_length()  # the leading ones of a, as 16 bits
        if k > 0:
            self._shift(k)

    def _shift(self, k: int):
        term = ((1 << k) - 1) - (self.subend >> (16 - k))
        self.subend = (self.subend << k) & 0xFFFF
        self.a = (self.a << k) & 0xFFFF
        nacc = self.nacc + k
        acc = (self.acc << k) + term
        if acc < 0:
            self._borrow()
        self.acc = acc & ((1 << nacc) - 1)
        self.nacc = nacc
        while self.nacc >= 40:
            self.nacc -= 8
            self._put((self.acc >> self.nacc) & 0xFF)
        self.acc &= (1 << self.nacc) - 1

    def _put(self, byte: int):
        if self.skip:
            self.skip -= 1
        else:
            self.out.append(byte)

    def _borrow(self):
        for j in range(len(self.out) - 1, -1, -1):
            if self.out[j]:
                self.out[j] -= 1
                return
            self.out[j] = 0xFF


def _suffix_array(data: bytes) -> list[int]:
    """Suffix array of data + a sentinel below every byte (encode.rs `suffix_array_of_bwt_string`)."""
    m = len(data) + 1
    rank = [b + 1 for b in data] + [0]
    sa = sorted(range(m), key=rank.__getitem__)
    gap = 1
    while True:
        width = max(rank) + 1
        second = rank[gap:] + [0] * min(gap, m)
        key = [r * width + s for r, s in zip(rank, second)]
        sa.sort(key=key.__getitem__)
        new = [0] * m
        r = 0
        prev = key[sa[0]]
        for i in sa:
            if key[i] != prev:
                r += 1
                prev = key[i]
            new[i] = r
        rank = new
        if r == m - 1:
            return sa
        gap *= 2


def _forward_bwt(data: bytes) -> tuple[bytes, int]:
    """(BWT of data + marker, the marker's row); the marker byte is written as 0 (encode.rs `forward_bwt`)."""
    n = len(data)
    if n == 0:
        return b"\x00", 0
    if data.count(data[0]) == n:
        # one repeated byte: shorter suffixes sort first, so the rows are n, n-1, ..., 0 and the marker is last
        return data + b"\x00", n
    sa = _suffix_array(data)
    out = bytearray(n + 1)
    marker = 0
    for row, idx in enumerate(sa):
        if idx == 0:
            marker = row
        else:
            out[row] = data[idx - 1]
    return bytes(out), marker


def _bits(enc: _ZpEncoder, value: int, width: int):
    for i in range(width - 1, -1, -1):
        enc.passthrough((value >> i) & 1)


def _position(enc: _ZpEncoder, ctx: bytearray, ctx_id: int, pos: int):
    """One MTF position through the context tree (encode.rs `encode_mtf_position`)."""
    if pos == 0:
        enc.bit(ctx, ctx_id, 1)
        return
    enc.bit(ctx, ctx_id, 0)
    if pos == 1:
        enc.bit(ctx, 3 + ctx_id, 1)
        return
    enc.bit(ctx, 3 + ctx_id, 0)
    off = 6
    for k in range(1, 8):
        width = 1 << k
        if pos < 2 * width:
            enc.bit(ctx, off, 1)
            n = 1
            for i in range(k - 1, -1, -1):
                b = (pos >> i) & 1
                enc.bit(ctx, off + n, b)
                n = 2 * n + b
            return
        enc.bit(ctx, off, 0)
        off += width
    # pos == 256, the marker: no more bits


def _run(bwt: bytes, i: int, stop: int, byte: int) -> int:
    """How many bytes from i (before `stop`) equal `byte`; windows double, so the cost follows the run's length."""
    one = bytes([byte])
    n = 0
    width = 16
    while i + n < stop:
        seg = bwt[i + n:min(i + n + width, stop)]
        same = len(seg) - len(seg.lstrip(one))
        n += same
        if same < len(seg):
            break
        width *= 2
    return n


def _block(enc, ctx, data, *, eob, markerpos, size, fshift):
    bwt, marker = _forward_bwt(data)
    if markerpos is not None:
        marker = markerpos
    _bits(enc, len(bwt) if size is None else size, 24)
    enc.passthrough(1 if fshift else 0)
    if fshift:
        enc.passthrough(1 if fshift > 1 else 0)
    mtf = bytearray(range(256))
    where = list(range(256))  # where[byte] = its MTF position
    freq = [0, 0, 0, 0]
    fadd = 4
    last = 3
    i = 0
    total = len(bwt)
    while i < total:
        byte = bwt[i]
        pos = 256 if i == marker and eob else where[byte]
        if pos == 0 and last == 0:
            # a run of position 0 in context 0: one ZP run, and the MTF list does not change (only the frequencies)
            stop = marker if i < marker < total and eob else total
            r = _run(bwt, i, stop, byte)
            enc.run(ctx, 0, 1, r)
            for _ in range(r):
                fadd += fadd >> fshift
                if fadd > 0x10000000:
                    fadd >>= 24
                    freq = [f >> 24 for f in freq]
                freq[0] += fadd
            i += r
            continue
        i += 1
        _position(enc, ctx, min(last, 2), pos)
        last = pos
        if pos == 256:
            continue
        sym = mtf[pos]
        fadd += fadd >> fshift
        if fadd > 0x10000000:
            fadd >>= 24
            freq = [f >> 24 for f in freq]
        comb = fadd + freq[pos] if pos < 4 else fadd
        k = pos
        while k >= 4:
            mtf[k] = mtf[k - 1]
            where[mtf[k]] = k
            k -= 1
        while k and comb >= freq[k - 1]:
            mtf[k] = mtf[k - 1]
            where[mtf[k]] = k
            freq[k] = freq[k - 1]
            k -= 1
        mtf[k] = sym
        where[sym] = k
        freq[k] = comb


def bzz_encode(data: bytes, blocksize: int = 1 << 20, *, eob=True, markerpos=None, size=None, fshift=0) -> bytes:
    """`data` as a BZZ stream, `blocksize` input bytes per block (each block is that plus the marker). Knobs, for
    every block: `eob=False` writes the marker's row as a 0 byte (no marker), `markerpos` writes the marker at that
    row instead (at or past the block's end: none), `size` replaces the declared 24-bit block size, `fshift` (0-2)
    is the MTF adaptation speed (encode.rs always writes 0)."""
    enc = _ZpEncoder()
    ctx = bytearray(_CONTEXTS)
    for start in range(0, len(data), blocksize):
        _block(enc, ctx, data[start:start + blocksize], eob=eob, markerpos=markerpos, size=size, fshift=fshift)
    _bits(enc, 0, 24)
    return enc.finish()


# -- IFF: chunks, FORMs, pages and bundled documents

def chunk(cid: bytes, data: bytes) -> bytes:
    """An IFF chunk: id, BE32 length, data, and a pad byte after an odd length."""
    return cid + struct.pack(">I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def form(kind: bytes, chunks: list[bytes]) -> bytes:
    return chunk(b"FORM", kind + b"".join(chunks))


def iff(form_bytes: bytes) -> bytes:
    """A file: the `AT&T` head, then one FORM."""
    return b"AT&T" + form_bytes


def _text(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(3, "big") + raw


def page(text=None, *, z=False, ant=None, antz=False, images=False, incl=None) -> bytes:
    """A `DJVU` FORM: INFO (100 x 100 at 300 dpi), then INCL (an id, or a list of ids), an image chunk (`Sjbz`, not
    decodable) when `images`, the text layer when `text` is given (`text` is the raw string with its GS, VT, US and
    LF separators; TXTa, or TXTz with `z`; no zone tree), and the annotations `ant` (ANTa, or ANTz with `antz`)."""
    chunks = [chunk(b"INFO", struct.pack(">HHBB", 100, 100, 26, 0) + struct.pack("<HBB", 300, 22, 0))]
    for cid in ([incl] if isinstance(incl, str) else incl or ()):
        chunks.append(chunk(b"INCL", cid.encode("utf-8")))
    if images:
        chunks.append(chunk(b"Sjbz", b"\x00\x01\x02\x03"))
    if text is not None:
        body = _text(text)
        chunks.append(chunk(b"TXTz", bzz_encode(body)) if z else chunk(b"TXTa", body))
    if ant is not None:
        body = ant.encode("utf-8")
        chunks.append(chunk(b"ANTz", bzz_encode(body)) if antz else chunk(b"ANTa", body))
    return form(b"DJVU", chunks)


def navm(bookmarks: list[tuple[str, str, list]]) -> bytes:
    """A NAVM chunk: (description, url, children) bookmarks as BZZ of a BE16 count of all of them, then each in
    pre-order: a child-count byte, a BE24 length and the description, a BE24 length and the url."""
    out = bytearray()
    count = 0

    def put(items):
        nonlocal count
        for desc, url, kids in items:
            count += 1
            d = desc.encode("utf-8")
            u = url.encode("utf-8")
            out.extend(bytes([len(kids)]) + len(d).to_bytes(3, "big") + d + len(u).to_bytes(3, "big") + u)
            put(kids)

    put(bookmarks)
    return chunk(b"NAVM", bzz_encode(struct.pack(">H", count) + bytes(out)))


def djvm(pages: list[bytes], *, ids=None, titles=None, shared=(), navm=None, indirect=False, ndir=False) -> bytes:
    """A whole multi-page file (`AT&T` included): FORM:DJVM, its DIRM (bundled: BE32 offsets from the file's start;
    `indirect`: bit 7 of the first byte clear, no offsets and no components), the `navm` chunk, then the components.
    `shared` is (id, FORM bytes) pairs of DJVI components, listed before the pages; `ids` and `titles` name the pages
    (default `p0001.djvu`...; a None title is left out). `ndir` writes DjVu 2's NDIR chunk in place of DIRM."""
    ids = list(ids) if ids is not None else [f"p{i + 1:04d}.djvu" for i in range(len(pages))]
    titles = list(titles) if titles is not None else [None] * len(pages)
    comps = [(cid, body, 0, None) for cid, body in shared]
    comps += [(cid, body, 1, title) for cid, body, title in zip(ids, pages, titles)]
    meta = bytearray()
    for _, body, _, _ in comps:
        meta += len(body).to_bytes(3, "big")
    for _, _, kind, title in comps:
        meta.append(kind | (0x40 if title is not None else 0))
    for cid, _, _, title in comps:
        meta += cid.encode("utf-8") + b"\x00"
        if title is not None:
            meta += title.encode("utf-8") + b"\x00"
    packed = bzz_encode(bytes(meta))
    count = struct.pack(">H", len(comps))
    if ndir:
        head = chunk(b"NDIR", packed)
    elif indirect:
        head = chunk(b"DIRM", b"\x01" + count + packed)
    else:
        size = 1 + 2 + 4 * len(comps) + len(packed)
        offset = 4 + 12 + 8 + size + size % 2 + len(navm or b"")
        offsets = bytearray()
        for _, body, _, _ in comps:
            offsets += struct.pack(">I", offset)
            offset += len(body)
        head = chunk(b"DIRM", b"\x81" + count + bytes(offsets) + packed)
    parts = [head] + ([navm] if navm else [])
    if not indirect:
        parts += [body for _, body, _, _ in comps]
    return iff(form(b"DJVM", parts))
