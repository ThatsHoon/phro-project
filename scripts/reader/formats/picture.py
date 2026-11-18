"""Lone picture files: WMF, EMF, PICT, WPG and TIFF, and `wmz` and `emz` (gzip-compressed WMF and EMF).

Each picture is one Image block, so the output is its PNG link and its figure text (markdown and raster do the rest);
a TIFF gives one block a page, up to TIFF_PAGES. A file over IMAGE_BYTES is too_large, as embedded pictures are capped.
A `wmz` or `emz` is inflated within IMAGE_BYTES and the zip ratio rule (ZIP_RATIO times the input once past
ZIP_RATIO_FLOOR). Bytes that are not the format the extension names are
`damaged`.
"""
from pathlib import Path

from .. import images, limits
from ..containers import flate
from ..limits import ReadError
from ..model import Doc, Image

KIND = {"wmf": "wmf", "wmz": "wmf", "emf": "emf", "emz": "emf", "tif": "tiff", "tiff": "tiff",
        "pct": "pict", "pict": "pict", "wpg": "wpg"}  # extension -> the kind images.sniff names
GZIPPED = frozenset({"wmz", "emz"})
_PICT_HEAD = 512
_PICT_VERSIONS = (b"\x11\x01", b"\x00\x11\x02\xff")  # PICT 1 and PICT 2, right after picSize and picFrame


def _pict(data: bytes) -> bytes:
    """A PICT with the zero 512-byte head images.sniff looks for: a file whose head holds an application's bytes gets
    zeros there, and one saved without the head gets it."""
    if images.sniff(data) == "pict":
        return data
    if data[_PICT_HEAD + 10:].startswith(_PICT_VERSIONS):
        return bytes(_PICT_HEAD) + data[_PICT_HEAD:]
    if data[10:].startswith(_PICT_VERSIONS):
        return bytes(_PICT_HEAD) + data
    return data


def unpack(data: bytes, ext: str) -> bytes:
    """The picture bytes of a file with extension `ext` (lower case, no dot): inflated for wmz and emz, a PICT given
    its head. ReadError("too_large", "part: …") past the inflate caps; ReadError("damaged", "not a <kind> file") when
    the bytes are not that kind."""
    kind = KIND[ext]
    if ext in GZIPPED:
        try:
            data = flate.gunzip(data, flate.Budget(), limits.IMAGE_BYTES)  # IMAGE_BYTES read at call time
        except ValueError:  # a corrupt stream, or flate.Truncated (a cut one)
            raise ReadError("damaged", f"not a {kind} file") from None
    if kind == "pict":
        data = _pict(data)
    if images.sniff(data) != kind:
        raise ReadError("damaged", f"not a {kind} file")
    return data


def parse(path, ctx) -> Doc:
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    if path.stat().st_size > limits.IMAGE_BYTES:  # the cap embedded pictures share, checked before reading
        raise ReadError("too_large", "picture past IMAGE_BYTES")
    data = unpack(path.read_bytes(), ext)
    if KIND[ext] != "tiff":
        return Doc(blocks=[Image(data)])
    from .. import raster
    cap = limits.TIFF_PAGES
    n = raster.tiff_pages(data, cap + 1)  # one past the cap tells a cut file from one of exactly TIFF_PAGES pages
    if n == 0:
        raise ReadError("damaged", "not a tiff file")
    warnings = [f"TIFF pages cut at {cap}"] if n > cap else []
    n = min(n, cap)
    blocks = [Image(data, f"page {i + 1}" if n > 1 else "", page=i) for i in range(n)]
    return Doc(blocks=blocks, warnings=warnings)
