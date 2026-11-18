"""In-memory WMF builders for the metafile tests (EMF, EMF+, PICT and WPG builders join them in later tasks)."""
import struct


def _pad(s: bytes) -> bytes:
    return s + b"\0" * (len(s) & 1)


def _colorref(rgb: int) -> bytes:
    return bytes((rgb >> 16 & 0xFF, rgb >> 8 & 0xFF, rgb & 0xFF, 0))


def wmf_rec(fn: int, *words) -> bytes:
    """One record: its size in words, the function, then the parameters. An int is a 16-bit word, bytes go in as they are."""
    body = b"".join(w if isinstance(w, bytes) else struct.pack("<H", w & 0xFFFF) for w in words)
    body = _pad(body)
    return struct.pack("<IH", 3 + len(body) // 2, fn) + body


def wmf(records: list[bytes], placeable=(0, 0, 1440, 1440, 1440)) -> bytes:
    """A WMF file. `placeable` is left, top, right, bottom and units per inch; None leaves the placeable header out."""
    recs = b"".join(records) + wmf_rec(0)
    out = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(recs)) // 2, 16, 0, 0) + recs
    if placeable is not None:
        left, top, right, bottom, inch = placeable
        pre = struct.pack("<IHhhhhH", 0x9AC6CDD7, 0, left, top, right, bottom, inch) + b"\0\0\0\0"
        check = 0
        for (w,) in struct.iter_unpack("<H", pre):
            check ^= w
        out = pre + struct.pack("<H", check) + out
    return out


def wmf_font(height, charset=0, face="Arial", escapement=0, weight=400) -> bytes:
    """META_CREATEFONTINDIRECT; the face is written as cp1252 and padded to 32 bytes."""
    name = face.encode("cp1252")[:31].ljust(32, b"\0")
    return wmf_rec(0x02FB, struct.pack("<hhhhhBBBBBBBB", height, 0, escapement, 0, weight, 0, 0, 0, charset, 0, 0, 0, 0),
                   name)


def wmf_pen(rgb=0, width=1, style=0) -> bytes:
    return wmf_rec(0x02FA, style, width, 0, _colorref(rgb))


def wmf_brush(rgb, style=0) -> bytes:
    return wmf_rec(0x02FC, style, _colorref(rgb), 0)


def wmf_select(i: int) -> bytes:
    return wmf_rec(0x012D, i)


def wmf_text(x, y, s: bytes) -> bytes:
    """META_TEXTOUT: the length, the string padded to a word, then y and x."""
    return wmf_rec(0x0521, len(s), _pad(s), y, x)


def wmf_exttext(x, y, s: bytes, options=0, dx=None) -> bytes:
    """META_EXTTEXTOUT; with ETO_GLYPH_INDEX (0x10) `s` holds 2 bytes a glyph and the length is the glyph count."""
    n = len(s) // 2 if options & 0x10 else len(s)
    words = [y, x, n, options]
    if options & 6:
        words.append(struct.pack("<4h", 0, 0, 0, 0))
    words.append(_pad(s))
    if dx is not None:
        words.append(struct.pack("<%dh" % len(dx), *dx))
    return wmf_rec(0x0A32, *words)


def wmf_window(org, ext) -> list[bytes]:
    """MM_ANISOTROPIC with the window origin and extent."""
    return [wmf_rec(0x0103, 8), wmf_rec(0x020B, org[1], org[0]), wmf_rec(0x020C, ext[1], ext[0])]


def wmf_rect(l, t, r, b) -> bytes:
    return wmf_rec(0x041B, b, r, t, l)


def wmf_polygon(points) -> bytes:
    return wmf_rec(0x0324, len(points), *[v for p in points for v in p])
