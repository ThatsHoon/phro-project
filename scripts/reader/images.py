"""Image sniffing, pure-Python BMP to PNG, and the asset files a render writes."""
import hashlib
import struct
import zlib
from pathlib import Path

VIEWABLE = frozenset({"png", "jpg", "gif", "webp"})
_MAX_PIXELS = 16_777_216


def sniff(data: bytes) -> str | None:
    h = data[:16]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if h.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if h.startswith(b"BM"):
        return "bmp"
    if h.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return "webp"
    if h.startswith((b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00")):
        return "wmf"
    if h.startswith(b"\x01\x00\x00\x00") and data[40:44] == b" EMF":
        return "emf"
    # a PICT file: a 512-byte zero head, picSize and picFrame, then the version opcode (PICT 1: 11 01; 2: 00 11 02 FF)
    if (data[522:524] == b"\x11\x01" or data[522:526] == b"\x00\x11\x02\xff") and data[:512].count(0) == 512:
        return "pict"
    if h.startswith(b"\xffWPC") and len(data) > 9 and data[9] == 0x16:  # a WordPerfect Graphic: file type 0x16
        return "wpg"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "heic"
    if data.startswith(b"%PDF-"):
        return "pdf"
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml"):
        head = data[:512]
        if b"<svg" in head:
            return "svg"
    elif head.startswith(b"<svg"):
        return "svg"
    return None


def _chunk(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def _png(width: int, height: int, rows: list, alpha: bool, level: int = -1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b""))


def _shift(mask: int):
    """(shift, bits) of a contiguous channel mask."""
    shift = (mask & -mask).bit_length() - 1
    return shift, (mask >> shift).bit_length()


def bmp_to_png(data: bytes) -> bytes | None:
    try:
        return _bmp_to_png(data)
    except (struct.error, IndexError, ValueError):
        return None


def _bmp_to_png(data: bytes) -> bytes | None:
    if len(data) < 54 or data[:2] != b"BM":
        return None
    off = struct.unpack_from("<I", data, 10)[0]
    hsize = struct.unpack_from("<I", data, 14)[0]
    if hsize < 40 or 14 + hsize > len(data):
        return None
    w, h, planes, bpp, comp, _, _, _, clr_used = struct.unpack_from("<iiHHIIiiI", data, 18)
    if planes != 1 or w <= 0 or h == 0 or bpp not in (1, 4, 8, 16, 24, 32):
        return None
    top_down = h < 0
    h = abs(h)
    if w * h > _MAX_PIXELS:
        return None
    masks = None
    if comp == 0:
        if bpp == 16:
            return None
    elif comp == 3 and bpp in (16, 32):
        if hsize >= 52:
            masks = list(struct.unpack_from("<III", data, 54))
        else:
            masks = list(struct.unpack_from("<III", data, 14 + hsize))
        a = struct.unpack_from("<I", data, 66)[0] if hsize >= 56 else 0
        masks.append(a)
        if not all(masks[:3]):
            return None
    else:
        return None
    palette = []
    if bpp <= 8:
        n = clr_used or (1 << bpp)
        n = min(n, 1 << bpp)
        pal = data[14 + hsize:14 + hsize + 4 * n]
        if len(pal) < 4 * n:
            return None
        palette = [(pal[i + 2], pal[i + 1], pal[i]) for i in range(0, 4 * n, 4)]
    stride = ((w * bpp + 31) // 32) * 4
    if off + stride * h > len(data):
        return None
    alpha = False
    if bpp == 32 and masks is None:
        alpha = any(data[off + y * stride + 3:off + y * stride + w * 4:4].count(0) != w
                    for y in range(h))
    elif masks and masks[3]:
        alpha = True
    rows = []
    if bpp <= 8:
        per = 8 // bpp
        mask = (1 << bpp) - 1
        expand = [bytes((v >> (8 - bpp - i * bpp)) & mask for i in range(per)) for v in range(256)]
        tabs = [bytes(palette[i][c] if i < len(palette) else 0 for i in range(256)) for c in range(3)]
    for y in range(h):
        base = off + (y if top_down else h - 1 - y) * stride
        line = data[base:base + stride]
        if bpp == 24:
            out = bytearray(line[:w * 3])
            out[0::3], out[2::3] = out[2::3], out[0::3]
        elif bpp == 32 and masks is None:
            px = bytearray(line[:w * 4])
            px[0::4], px[2::4] = px[2::4], px[0::4]
            if alpha:
                out = px
            else:
                out = bytearray(w * 3)
                out[0::3], out[1::3], out[2::3] = px[0::4], px[1::4], px[2::4]
        elif bpp in (16, 32):
            out = bytearray()
            fields = [_shift(m) if m else None for m in masks]
            for x in range(w):
                v = struct.unpack_from("<H" if bpp == 16 else "<I", line, x * (bpp // 8))[0]
                ch = [255 if f is None else ((v & m) >> f[0]) * 255 // ((1 << f[1]) - 1)
                      for m, f in zip(masks, fields)]
                out += bytes(ch if alpha else ch[:3])
        else:
            idx = b"".join(map(expand.__getitem__, line))[:w]
            out = bytearray(w * 3)
            for c in range(3):
                out[c::3] = idx.translate(tabs[c])
        rows.append(bytes(out))
    return _png(w, h, rows, alpha)


class Assets:
    """Writes images under `dir`, named img-001.ext in order; identical bytes share a file."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self._seen: dict[str, tuple[str, bool]] = {}
        self._n = 0

    def add(self, data: bytes, alt: str = "") -> tuple[str, bool]:
        if len(data) < 16:
            return "", False
        kind = sniff(data)
        if kind is None:
            return "", False
        key = hashlib.sha1(data).hexdigest()
        if key in self._seen:
            return self._seen[key]
        if kind == "bmp":
            png = bmp_to_png(data)
            if png is not None:
                data, kind = png, "png"
        self._n += 1
        name = f"img-{self._n:03d}.{kind}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)
        res = (f"assets/{name}", kind in VIEWABLE)
        self._seen[key] = res
        return res
