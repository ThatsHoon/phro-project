"""Image sniffing, pure-Python BMP to PNG, and the asset files a render writes."""
import hashlib
import struct
import zlib
from pathlib import Path

VIEWABLE = frozenset({"png", "jpg", "gif", "webp"})
_MAX_PIXELS = 16_777_216
FIGURES = frozenset({"wmf", "emf", "pict", "wpg", "tiff"})  # converted to PNG with their text (raster)
# the per-figure cache (cache.Figures) Assets reads before converting a figure and keeps finished ones in; set by
# cache.store for the read it files, None otherwise
FIGURE_CACHE = None


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
    if h.startswith((b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00", b"\x02\x00\x09\x00")):  # placeable, memory, disk
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


def gray_png(g, level: int = 1) -> bytes:
    """An 8-bit grayscale PNG (colour type 0, filter 0 on every row) of an `ocr_pixels.Gray`."""
    ihdr = struct.pack(">IIBBBBB", g.w, g.h, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + g.data[y * g.w:(y + 1) * g.w] for y in range(g.h))
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


def _figure(kind: str, data: bytes, sha: str, codepage: str, page: int):
    """raster.convert's Figure for one picture, from FIGURE_CACHE when it holds it; a finished one is kept there."""
    from . import raster
    store, k = FIGURE_CACHE, None
    if store is not None:
        try:
            k = store.key(sha, codepage, page, raster.backend())
            fig = store.get(k)
            if fig is not None:
                return fig
        except Exception:  # noqa: BLE001 - the figure cache is only a shortcut
            k = None
    fig = raster.convert(kind, data, codepage=codepage, **({"page": page} if page else {}))
    if k is not None:
        try:
            store.put(k, fig)  # not when the time cut it
        except Exception:  # noqa: BLE001
            pass
    return fig


class Assets:
    """Writes images under `dir`, named img-001.ext in order; identical bytes share a file."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self._seen: dict[str, tuple[str, bool]] = {}
        self._n = 0
        self.lines: dict[str, list] = {}  # figure text lines by the path add returned
        self.figures = 0  # pictures converted to PNG
        self.figures_left = 0  # figure kinds kept as the original file
        self.warnings: set[str] = set()  # the figure warnings raster raised
        self._kept: dict[str, tuple[str, bool]] = {}  # unconverted figures by bytes and code page: one copy each

    def add(self, data: bytes, alt: str = "", codepage: str = "", page: int = 0) -> tuple[str, bool]:
        """`page` is the TIFF page to draw, from 0 (a lone TIFF file's blocks name theirs)."""
        if len(data) < 16:
            return "", False
        kind = sniff(data)
        if kind is None:
            return "", False
        sha = key = hashlib.sha1(data).hexdigest()
        if kind in FIGURES:  # the code page can change the text, so it is part of the key
            key += "/" + codepage
        base = key
        if kind in FIGURES and page:
            key += f"/p{page}"
        if key in self._seen:
            return self._seen[key]
        lines: list = []
        if kind == "bmp":
            png = bmp_to_png(data)
            if png is not None:
                data, kind = png, "png"
        elif kind in FIGURES:
            try:
                fig = _figure(kind, data, sha, codepage, page)
                lines = list(fig.lines)
                self.warnings |= fig.warnings
                png = fig.png
            except Exception:  # noqa: BLE001 - a picture never makes a document fail
                lines, png = [], None
            if png is not None:
                data, kind = png, "png"
                self.figures += 1
            elif base in self._kept:  # another page of a file already kept as it is
                self._seen[key] = self._kept[base]
                return self._kept[base]
            else:
                self.figures_left += 1
        self._n += 1
        name = f"img-{self._n:03d}.{kind}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)
        res = (f"assets/{name}", kind in VIEWABLE)
        if lines:
            self.lines[res[0]] = lines
        self._seen[key] = res
        if kind in FIGURES:  # kept as the original file
            self._kept[base] = res
        return res
