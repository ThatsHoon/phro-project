"""pdf image XObjects: a JPEG kept as it is, raw samples written as a stdlib PNG.

Port of dochan 1.21.0 pdf/images.py (MIT). Differences from dochan: Pillow is gone, so
`formats.pdf_layout.picture` writes the PNG (colour type 2, RGB rows, zlib level 1; none for a
flat picture) when the reader calls the image's `load`, and the sample unpacking is done here; Gray and Indexed take 1, 2, 4 or 8 bits per component and RGB takes 8; an Indexed palette
over Gray or RGB is applied; JPX, CCITT and JBIG2 give no bytes (`data` None, with their kind);
CMYK, any other colour space and an image over `reader.images._MAX_PIXELS` give `data` None
with kind "raw". The caller sets the box.
"""
from typing import List

from ..formats.pdf_layout import PageImage, picture
from ..images import _MAX_PIXELS, sniff
from .objects import PDFStream
from .structure import PDFFile

_BOX = (0.0, 0.0, 0.0, 0.0)
_GRAY = {"DeviceGray", "G", "CalGray"}
_RGB = {"DeviceRGB", "RGB", "CalRGB"}
_CODECS = {"DCTDecode": "jpg", "DCT": "jpg", "JPXDecode": "jpx", "CCITTFaxDecode": "ccitt",
           "CCF": "ccitt", "JBIG2Decode": "jbig2"}


def image(pdf: PDFFile, stream: PDFStream) -> PageImage:
    """The image of an image XObject stream: a JPEG's bytes as `data`, raw samples as a `load`
    that decodes them when called; neither when it cannot be shown."""
    stream = _direct(pdf, stream)
    d = stream.dictionary
    width, height = _int(pdf.resolve(d.get("Width"))), _int(pdf.resolve(d.get("Height")))
    px = (width, height)
    filters = d.get("Filter")
    for name in filters if isinstance(filters, list) else [filters]:
        kind = _CODECS.get(str(name))
        if kind is None:
            continue
        data = None
        if kind == "jpg":
            data = pdf.decode_image_bytes(stream)  # the bytes before the JPEG codec
            if sniff(data) != "jpg":
                data = None
        return PageImage(_BOX, data, px, kind if data is not None or kind != "jpg" else "raw")
    if _rows_of(pdf, stream, width, height, probe=True) is None:
        return PageImage(_BOX, None, px, "raw")
    return PageImage(_BOX, None, px, "png", load=lambda: _picture(pdf, stream, width, height))


def _picture(pdf: PDFFile, stream: PDFStream, width: int, height: int):
    rows = _rows_of(pdf, stream, width, height)
    return (None, False) if rows is None else picture(width, height, rows, False)


def _direct(pdf: PDFFile, stream: PDFStream) -> PDFStream:
    """The stream with an indirect /Filter and /DecodeParms (and their entries) resolved: the
    filter chain reads them as they stand."""
    d = stream.dictionary
    changed = {}
    for key in ("Filter", "DecodeParms", "DP"):
        value = d.get(key)
        if value is None:
            continue
        direct = pdf.resolve(value)
        if isinstance(direct, list):
            items = [pdf.resolve(v) for v in direct]
            direct = direct if all(a is b for a, b in zip(items, direct)) else items
        if direct is not value:
            changed[key] = direct
    return PDFStream({**d, **changed}, stream.raw) if changed else stream


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _space(pdf: PDFFile, cs, base: bool = False):
    """(kind, components, palette) of a colour space: kind "gray", "rgb" or "indexed"; None when
    the space is not one of those. palette is [(r, g, b)] for indexed."""
    cs = pdf.resolve(cs)
    name, args = (str(cs), []) if isinstance(cs, str) else (None, [])
    if isinstance(cs, list) and cs:
        name, args = str(pdf.resolve(cs[0])), cs[1:]
    if name in _GRAY:
        return "gray", 1, None
    if name in _RGB:
        return "rgb", 3, None
    if name == "ICCBased" and args:
        profile = pdf.resolve(args[0])
        n = pdf.resolve(profile.dictionary.get("N")) if isinstance(profile, PDFStream) else None
        return {1: ("gray", 1, None), 3: ("rgb", 3, None)}.get(n, (None, 0, None))
    if name in ("Indexed", "I") and len(args) >= 3 and not base:  # a base is gray, RGB or ICC only
        under = _space(pdf, args[0], True)
        hival = pdf.resolve(args[1])
        table = pdf.resolve(args[2])
        if isinstance(table, PDFStream):
            table = pdf.decode_stream_bytes(table)
        if under[0] not in ("gray", "rgb") or not isinstance(table, bytes) \
                or not isinstance(hival, int) or hival < 0:
            return None, 0, None
        step = under[1]
        count = min(hival, 255) + 1
        palette = []
        for i in range(count):
            entry = table[i * step:(i + 1) * step]
            if len(entry) < step:
                entry = entry + bytes(step - len(entry))
            palette.append((entry[0],) * 3 if step == 1 else tuple(entry))
        return "indexed", 1, palette
    return None, 0, None


def _rows_of(pdf: PDFFile, stream: PDFStream, width: int, height: int, probe: bool = False):
    """The RGB rows of raw samples, or None when they cannot be shown; with `probe`, whether
    their dictionary can (True or None) without decoding them."""
    d = stream.dictionary
    if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
        return None
    if pdf.resolve(d.get("ImageMask")) is True:
        return None
    kind, comps, palette = _space(pdf, d.get("ColorSpace"))
    if kind is None:
        return None
    bpc = pdf.resolve(d.get("BitsPerComponent"))
    if bpc not in ((8,) if kind == "rgb" else (1, 2, 4, 8)):
        return None
    if probe:
        return True
    stride = (width * comps * bpc + 7) // 8
    samples = pdf.decode_image_bytes(stream)
    if len(samples) < stride * height:
        return None
    invert = _inverted(pdf, d.get("Decode"), comps) and kind != "indexed"
    rows: List[bytes] = []
    if kind == "rgb":
        flip = bytes(255 - i for i in range(256)) if invert else None
        for y in range(height):
            row = samples[y * stride:y * stride + stride]
            rows.append(row.translate(flip) if flip else row)
        return rows
    levels = (1 << bpc) - 1
    if kind == "gray":
        values = bytes((levels - v if invert else v) * 255 // levels for v in range(levels + 1))
    else:
        values = None
    per = 8 // bpc
    mask = levels
    shifts = [8 - bpc - i * bpc for i in range(per)]
    unpack = [bytes((b >> s) & mask for s in shifts) for b in range(256)]
    if kind == "gray":
        tabs = [values.ljust(256, b"\x00")] * 3
    else:
        tabs = [bytes((palette[i][c] if i < len(palette) else 0) for i in range(256))
                for c in range(3)]
    for y in range(height):
        line = samples[y * stride:y * stride + stride]
        idx = b"".join(map(unpack.__getitem__, line))[:width] if bpc < 8 else line[:width]
        out = bytearray(width * 3)
        for c in range(3):
            out[c::3] = idx.translate(tabs[c])
        rows.append(bytes(out))
    return rows


def _inverted(pdf: PDFFile, decode, comps: int) -> bool:
    """True when /Decode runs every component from 1 to 0."""
    decode = pdf.resolve(decode)
    if not isinstance(decode, list) or len(decode) != 2 * comps:
        return False
    pairs = [pdf.resolve(v) for v in decode]
    return all(isinstance(v, (int, float)) for v in pairs) and all(
        pairs[2 * i] == 1 and pairs[2 * i + 1] == 0 for i in range(comps))
