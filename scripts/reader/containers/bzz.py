"""DjVu's BZZ codec (decoding): the ZP adaptive binary arithmetic decoder, the MTF stage and the inverse BWT.

Ported from djvu-rs (MIT), the crates `djvu-bzz` 0.45.0 (`src/decode.rs`, `src/lib.rs`) and `djvu-zp` 0.45.0
(`src/lib.rs`, `src/tables.rs`), commit a329e2ad3bd9f710fcfeb758939f58626a6eb69a. The ZP table is checked against
the DjVu 3 specification's appendix table (Appendix 3, Table 9), as printed in DjVuLibre's `default_ztable` (format
knowledge only; the test holds spot values).

A stream is a run of blocks, each a 24-bit size (0 ends the stream), two FSHIFT bits, then `size` MTF positions
read through 262 ZP contexts that are never reset between blocks; position 256 is the BWT end-of-block marker. Added
to djvu-bzz: the 16-byte read past the end is checked at every refill (djvu-bzz looks every 4,096 symbols), a block
is checked against the caller's `limit` and the ZIP_RATIO rule before it is decoded, and MARKERPOS is range checked.
"""
from .. import limits
from ..limits import ReadError

BZZ_BLOCK = 4 * 2**20   # symbols of one block, the spec's maximum (DjVuLibre's MAXBLOCK)
BZZ_CHUNK = 4 * 2**20   # output of one chunk, which the caller passes as `limit`
BZZ_TOTAL = 64 * 2**20  # output of one document, whose remainder the caller passes as `limit` when it is smaller
TAIL = 16               # bytes the coder may read past the input's end: its look-ahead on a valid stream
CONTEXTS = 262          # adaptive contexts of the MTF position tree (indexes 0-259 are used)

# The ZP adaptation table: per state the probability p, the MPS adaptation threshold m, and the next state after an
# MPS (up) or an LPS (dn). States 0-250; every up and dn stays within them.
PROB = (
    0x8000, 0x8000, 0x8000, 0x6BBD, 0x6BBD, 0x5D45, 0x5D45, 0x51B9, 0x51B9, 0x4813, 0x4813, 0x3FD5,
    0x3FD5, 0x38B1, 0x38B1, 0x3275, 0x3275, 0x2CFD, 0x2CFD, 0x2825, 0x2825, 0x23AB, 0x23AB, 0x1F87,
    0x1F87, 0x1BBB, 0x1BBB, 0x1845, 0x1845, 0x1523, 0x1523, 0x1253, 0x1253, 0x0FCF, 0x0FCF, 0x0D95,
    0x0D95, 0x0B9D, 0x0B9D, 0x09E3, 0x09E3, 0x0861, 0x0861, 0x0711, 0x0711, 0x05F1, 0x05F1, 0x04F9,
    0x04F9, 0x0425, 0x0425, 0x0371, 0x0371, 0x02D9, 0x02D9, 0x0259, 0x0259, 0x01ED, 0x01ED, 0x0193,
    0x0193, 0x0149, 0x0149, 0x010B, 0x010B, 0x00D5, 0x00D5, 0x00A5, 0x00A5, 0x007B, 0x007B, 0x0057,
    0x0057, 0x003B, 0x003B, 0x0023, 0x0023, 0x0013, 0x0013, 0x0007, 0x0007, 0x0001, 0x0001, 0x5695,
    0x24EE, 0x8000, 0x0D30, 0x481A, 0x0481, 0x3579, 0x017A, 0x24EF, 0x007B, 0x1978, 0x0028, 0x10CA,
    0x000D, 0x0B5D, 0x0034, 0x078A, 0x00A0, 0x050F, 0x0117, 0x0358, 0x01EA, 0x0234, 0x0144, 0x0173,
    0x0234, 0x00F5, 0x0353, 0x00A1, 0x05C5, 0x011A, 0x03CF, 0x01AA, 0x0285, 0x0286, 0x01AB, 0x03D3,
    0x011A, 0x05C5, 0x00BA, 0x08AD, 0x007A, 0x0CCC, 0x01EB, 0x1302, 0x02E6, 0x1B81, 0x045E, 0x24EF,
    0x0690, 0x2865, 0x09DE, 0x3987, 0x0DC8, 0x2C99, 0x10CA, 0x3B5F, 0x0B5D, 0x5695, 0x078A, 0x8000,
    0x050F, 0x24EE, 0x0358, 0x0D30, 0x0234, 0x0481, 0x0173, 0x017A, 0x00F5, 0x007B, 0x00A1, 0x0028,
    0x011A, 0x000D, 0x01AA, 0x0034, 0x0286, 0x00A0, 0x03D3, 0x0117, 0x05C5, 0x01EA, 0x08AD, 0x0144,
    0x0CCC, 0x0234, 0x1302, 0x0353, 0x1B81, 0x05C5, 0x24EF, 0x03CF, 0x2B74, 0x0285, 0x201D, 0x01AB,
    0x1715, 0x011A, 0x0FB7, 0x00BA, 0x0A67, 0x01EB, 0x06E7, 0x02E6, 0x0496, 0x045E, 0x030D, 0x0690,
    0x0206, 0x09DE, 0x0155, 0x0DC8, 0x00E1, 0x2B74, 0x0094, 0x201D, 0x0188, 0x1715, 0x0252, 0x0FB7,
    0x0383, 0x0A67, 0x0547, 0x06E7, 0x07E2, 0x0496, 0x0BC0, 0x030D, 0x1178, 0x0206, 0x19DA, 0x0155,
    0x24EF, 0x00E1, 0x320E, 0x0094, 0x432A, 0x0188, 0x447D, 0x0252, 0x5ECE, 0x0383, 0x8000, 0x0547,
    0x481A, 0x07E2, 0x3579, 0x0BC0, 0x24EF, 0x1178, 0x1978, 0x19DA, 0x2865, 0x24EF, 0x3987, 0x320E,
    0x2C99, 0x432A, 0x3B5F, 0x447D, 0x5695, 0x5ECE, 0x8000, 0x8000, 0x5695, 0x481A, 0x481A,
)
THRESHOLD = (
    0x0000, 0x0000, 0x0000, 0x10A5, 0x10A5, 0x1F28, 0x1F28, 0x2BD3, 0x2BD3, 0x36E3, 0x36E3, 0x408C,
    0x408C, 0x48FD, 0x48FD, 0x505D, 0x505D, 0x56D0, 0x56D0, 0x5C71, 0x5C71, 0x615B, 0x615B, 0x65A5,
    0x65A5, 0x6962, 0x6962, 0x6CA2, 0x6CA2, 0x6F74, 0x6F74, 0x71E6, 0x71E6, 0x7404, 0x7404, 0x75D6,
    0x75D6, 0x7768, 0x7768, 0x78C2, 0x78C2, 0x79EA, 0x79EA, 0x7AE7, 0x7AE7, 0x7BBE, 0x7BBE, 0x7C75,
    0x7C75, 0x7D0F, 0x7D0F, 0x7D91, 0x7D91, 0x7DFE, 0x7DFE, 0x7E5A, 0x7E5A, 0x7EA6, 0x7EA6, 0x7EE6,
    0x7EE6, 0x7F1A, 0x7F1A, 0x7F45, 0x7F45, 0x7F6B, 0x7F6B, 0x7F8D, 0x7F8D, 0x7FAA, 0x7FAA, 0x7FC3,
    0x7FC3, 0x7FD7, 0x7FD7, 0x7FE7, 0x7FE7, 0x7FF2, 0x7FF2, 0x7FFA, 0x7FFA, 0x7FFF, 0x7FFF, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
)
MPS_NEXT = (
    84, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41,
    42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61,
    62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81,
    82, 81, 82, 9, 86, 5, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 82, 99, 76, 101,
    70, 103, 66, 105, 106, 107, 66, 109, 60, 111, 56, 69, 114, 65, 116, 61, 118, 57, 120, 53,
    122, 49, 124, 43, 72, 39, 60, 33, 56, 29, 52, 23, 48, 23, 42, 137, 38, 21, 140, 15,
    142, 9, 144, 141, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 70, 157, 66, 81, 62, 75,
    58, 69, 54, 65, 50, 167, 44, 65, 40, 59, 34, 55, 30, 175, 24, 177, 178, 179, 180, 181,
    182, 183, 184, 69, 186, 59, 188, 55, 190, 51, 192, 47, 194, 41, 196, 37, 198, 199, 72, 201,
    62, 203, 58, 205, 54, 207, 50, 209, 46, 211, 40, 213, 36, 215, 30, 217, 26, 219, 20, 71,
    14, 61, 14, 57, 8, 53, 228, 49, 230, 45, 232, 39, 234, 35, 138, 29, 24, 25, 240, 19,
    22, 13, 16, 13, 10, 7, 244, 249, 10, 89, 230,
)
LPS_NEXT = (
    145, 4, 3, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
    18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
    38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57,
    58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77,
    78, 79, 80, 85, 226, 6, 176, 143, 138, 141, 112, 135, 104, 133, 100, 129, 98, 127, 72, 125,
    102, 123, 60, 121, 110, 119, 108, 117, 54, 115, 48, 113, 134, 59, 132, 55, 130, 51, 128, 47,
    126, 41, 62, 37, 66, 31, 54, 25, 50, 131, 46, 17, 40, 15, 136, 7, 32, 139, 172, 9,
    170, 85, 168, 248, 166, 247, 164, 197, 162, 95, 160, 173, 158, 165, 156, 161, 60, 159, 56, 71,
    52, 163, 48, 59, 42, 171, 38, 169, 32, 53, 26, 47, 174, 193, 18, 191, 222, 189, 218, 187,
    216, 185, 214, 61, 212, 53, 210, 49, 208, 45, 206, 39, 204, 195, 202, 31, 200, 243, 64, 239,
    56, 237, 52, 235, 48, 233, 44, 231, 38, 229, 34, 227, 28, 225, 22, 223, 16, 221, 220, 63,
    8, 55, 224, 51, 2, 47, 87, 43, 246, 37, 244, 33, 238, 27, 236, 21, 16, 15, 8, 241,
    242, 7, 10, 245, 2, 1, 83, 250, 2, 143, 246,
)

ZP_TABLE = tuple(zip(PROB, THRESHOLD, MPS_NEXT, LPS_NEXT))


class Limit(ValueError):
    """The output would pass the caller's `limit`, or a chunk over 1 MiB of output passes ZIP_RATIO times its input."""


class Truncated(ValueError):
    """The coder read more than TAIL bytes past the input's end: the stream was cut, or it is spinning on padding."""


def _tree():
    # The MTF position tree, one node per context: ZERO[ci] / ONE[ci] is the next context after a 0 / 1 bit, or a
    # leaf, -1 - position. Nodes 0-2 (by the last position, at most 2) code position 0, nodes 3-5 position 1; then
    # the level heads 6, 8, 12, 20, 36, 68 and 132 open 2^k-wide ranges (k = 1..7) whose k bits are a binary subtree
    # below the head (decode.rs `decode_ctx_bits`); a 0 at the last head is the marker, 256.
    zero = [0] * CONTEXTS
    one = [0] * CONTEXTS
    for i in range(3):
        one[i] = -1
        zero[i] = 3 + i
        one[3 + i] = -2
        zero[3 + i] = 6
    off = 6
    for k in range(1, 8):
        width = 1 << k
        one[off] = off + 1
        zero[off] = off + width if k < 7 else -257
        for nn in range(1, width):
            for bit, side in ((0, zero), (1, one)):
                child = 2 * nn + bit
                side[off + nn] = -1 - child if child >= width else off + child
        off += width
    return tuple(zero), tuple(one)


_ZERO, _ONE = _tree()


class _Zp:
    """The ZP decoder state (djvu-zp `ZpDecoder`). Bytes past the input read as 0xFF; reading more than TAIL of them
    raises Truncated. The block loop copies this state into locals and writes it back (decode.rs)."""

    __slots__ = ("data", "end", "pos", "a", "c", "fence", "buf", "count")

    def __init__(self, data: bytes):
        if len(data) < 2:
            raise ReadError("damaged", "bzz: stream under 2 bytes")
        self.data = data
        self.end = len(data) + TAIL
        self.pos = 2
        self.a = 0
        self.c = (data[0] << 8) | data[1]
        self.buf = 0
        self.count = 0
        self._refill()
        self.fence = min(self.c, 0x7FFF)

    def _refill(self):
        data = self.data
        n = len(data)
        while self.count <= 24:
            pos = self.pos
            if pos < n:
                byte = data[pos]
            elif pos < self.end:
                byte = 0xFF
            else:
                raise Truncated(f"bzz: read {TAIL} bytes past the end of a {n}-byte stream")
            self.pos = pos + 1
            self.buf = ((self.buf << 8) | byte) & 0xFFFFFFFF
            self.count += 8

    def passthrough(self) -> int:
        """One context-free bit (threshold 0x8000 + a/2), for the block size and FSHIFT."""
        z = 0x8000 + (self.a >> 1)
        if z > self.c:
            z = 0x10000 - z
            a = (self.a + z) & 0xFFFF
            c = (self.c + z) & 0xFFFF
            shift = 16 - (a ^ 0xFFFF).bit_length()  # the leading ones of a
            self.count -= shift
            self.a = (a << shift) & 0xFFFF
            self.c = ((c << shift) | ((self.buf >> self.count) & ((1 << shift) - 1))) & 0xFFFF
            bit = 1
        else:
            self.count -= 1
            self.a = (z << 1) & 0xFFFF
            self.c = ((self.c << 1) | ((self.buf >> self.count) & 1)) & 0xFFFF
            bit = 0
        if self.count < 16:
            self._refill()
        self.fence = min(self.c, 0x7FFF)
        return bit

    def bits(self, width: int) -> int:
        """`width` passthrough bits, most significant first (decode.rs `decode_raw_bits`)."""
        top = 1 << width
        n = 1
        while n < top:
            n = (n << 1) | self.passthrough()
        return n - top


def _block(zp: _Zp, ctx: bytearray, size: int, fshift: int) -> tuple[bytearray, int]:
    """The `size` MTF positions of one block as BWT bytes, and the marker's index (-1 when there is none)
    (decode.rs `decode_mtf_phase`). The ZP bit decoder is inlined: one body walks the position tree."""
    prob, thresh, up, dn = PROB, THRESHOLD, MPS_NEXT, LPS_NEXT
    zero, one = _ZERO, _ONE
    data = zp.data
    n = len(data)
    end = zp.end
    a, c, fence, buf, count, pos = zp.a, zp.c, zp.fence, zp.buf, zp.count, zp.pos
    out = bytearray(size)
    mtf = bytearray(range(256))
    freq = [0, 0, 0, 0]
    fadd = 4
    last = 3
    marker = -1
    for i in range(size):
        ci = last if last < 2 else 2
        while True:
            st = ctx[ci]
            z = a + prob[st]
            if z <= fence:
                a = z
                bit = st & 1
            else:
                d = 0x6000 + ((a + z) >> 2)
                if z > d:
                    z = d
                if z > c:
                    # LPS, then renormalize by the leading ones of a
                    bit = (st & 1) ^ 1
                    z = 0x10000 - z
                    a = (a + z) & 0xFFFF
                    c = (c + z) & 0xFFFF
                    ctx[ci] = dn[st]
                    shift = 16 - (a ^ 0xFFFF).bit_length()
                    count -= shift
                    a = (a << shift) & 0xFFFF
                    c = ((c << shift) | ((buf >> count) & ((1 << shift) - 1))) & 0xFFFF
                else:
                    bit = st & 1
                    if a >= thresh[st]:
                        ctx[ci] = up[st]
                    count -= 1
                    a = (z << 1) & 0xFFFF
                    c = ((c << 1) | ((buf >> count) & 1)) & 0xFFFF
                if count < 16:
                    while count <= 24:
                        if pos < n:
                            byte = data[pos]
                        elif pos < end:
                            byte = 0xFF
                        else:
                            raise Truncated(f"bzz: read {TAIL} bytes past the end of a {n}-byte stream")
                        pos += 1
                        buf = ((buf << 8) | byte) & 0xFFFFFFFF
                        count += 8
                fence = c if c < 0x7FFF else 0x7FFF
            ci = one[ci] if bit else zero[ci]
            if ci < 0:
                break
        p = -1 - ci
        last = p
        if p == 256:
            marker = i  # out[i] stays 0
            continue
        sym = mtf[p]
        out[i] = sym
        # the frequency-ordered front of the MTF list; the sums stay under 2^32 (FSHIFT is at most 2, so freq[k]
        # is at most 5 * fadd <= 5 * 2^28), so djvu-bzz's u32 saturation never applies
        fadd += fadd >> fshift
        if fadd > 0x10000000:
            fadd >>= 24
            freq = [f >> 24 for f in freq]
        comb = fadd + freq[p] if p < 4 else fadd
        k = p
        if k >= 4:
            mtf[4:k + 1] = mtf[3:k]
            k = 3
        while k and comb >= freq[k - 1]:
            mtf[k] = mtf[k - 1]
            freq[k] = freq[k - 1]
            k -= 1
        mtf[k] = sym
        freq[k] = comb
    zp.a, zp.c, zp.fence, zp.buf, zp.count, zp.pos = a, c, fence, buf, count, pos
    return out, marker


def _unbwt(bwt: bytearray, marker: int) -> bytes:
    """The inverse BWT of one block (decode.rs `inverse_bwt`), the marker sorting before every byte. djvu-bzz walks
    the LF mapping backwards from row 0; this walks its inverse forwards from the marker's row: `order` lists the
    rows by first symbol (the marker, then each byte's rows in order), so row j continues at row order[j]."""
    total = len(bwt)
    if total <= 1:
        return b""
    rows = [[] for _ in range(256)]
    add = [r.append for r in rows]
    for i, b in enumerate(bwt):
        add[b](i)
    rows[0].remove(marker)
    order = [marker]
    for r in rows:
        order += r
    out = bytearray(total - 1)
    j = marker
    for t in range(total - 1):
        j = order[j]
        out[t] = bwt[j]
    return bytes(out)


def decode(data: bytes, limit: int) -> bytes:
    """The BZZ stream `data` decoded, at most `limit` bytes. ReadError("damaged") for a block over BZZ_BLOCK, a block
    without its EOB marker or a MARKERPOS outside the block; Limit when the output would pass `limit`, or pass 1 MiB
    (limits.ZIP_RATIO_FLOOR) and ZIP_RATIO times len(data), checked before each block is decoded; Truncated after a
    read more than TAIL bytes past the end. Caps are read at call time."""
    data = bytes(data)
    zp = _Zp(data)
    ctx = bytearray(CONTEXTS)
    parts = []
    total = 0
    while True:
        size = zp.bits(24)
        if size == 0:
            break
        if size > BZZ_BLOCK:
            raise ReadError("damaged", f"bzz: block of {size} symbols over {BZZ_BLOCK}")
        grown = total + size - 1
        if grown > limit:
            raise Limit(f"bzz: output over {limit} bytes")
        if grown > limits.ZIP_RATIO_FLOOR and grown > limits.ZIP_RATIO * len(data):
            raise Limit(f"bzz: {grown} bytes from {len(data)}, over {limits.ZIP_RATIO} times")
        fshift = 0
        if zp.passthrough():
            fshift = 1
            if zp.passthrough():
                fshift = 2
        bwt, marker = _block(zp, ctx, size, fshift)
        # the marker is one of the block's symbols, so a MARKERPOS at or past the block's end arrives as no marker;
        # the range check below stays for the spec's rule
        if marker < 0:
            raise ReadError("damaged", "bzz: block without its EOB marker")
        if not 0 <= marker < size:
            raise ReadError("damaged", f"bzz: MARKERPOS {marker} outside a block of {size}")
        part = _unbwt(bwt, marker)
        parts.append(part)
        total += len(part)
    return b"".join(parts)
