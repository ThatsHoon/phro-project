"""pdf stream filter decoders, standard library only.

Port of dochan 1.21.0 pdf/filters.py (MIT). Differences from dochan: RunLength is new; the
predictor sample budget and the document byte budget live in one DecodeBudget; output stops
at a cap and the stream is counted in `budget.cuts` instead of only warned about.
"""
import base64
import binascii
import zlib
from typing import List, Optional

MAX_DECODED_SIZE = 50 * 1024 * 1024
# The bytes one document may decode in total. A stream cap alone lets a small file name the
# same bomb stream many times.
MAX_TOTAL_DECODED = 200 * 1024 * 1024
# Packed samples take up to eight Python decode steps per byte. Bound work separately from
# byte size; return only completed rows on exhaustion.
MAX_PACKED_PREDICTOR_SAMPLES = 1024 * 1024

# Image codecs: not text, so the chain ends there and returns the bytes so far.
_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}

_WHITESPACE = b"\x00\t\n\x0c\r "


class DecodeBudget:
    """What a document may still decode: `total` bytes left, `cuts` streams cut so far."""

    def __init__(self, total: Optional[int] = None):
        self.total = MAX_TOTAL_DECODED if total is None else total
        self.cuts = 0
        self.samples = MAX_PACKED_PREDICTOR_SAMPLES


def decode_stream(stream_dict: dict, raw: bytes, warnings: List[str],
                  budget: Optional[DecodeBudget] = None) -> bytes:
    """Apply a stream's /Filter chain. Output stops at MAX_DECODED_SIZE or the budget.

    A stream that hits a cap adds 1 to `budget.cuts`; the bytes it produced count against
    `budget.total`. With the budget spent, the stream comes back empty and is counted.
    """
    if budget is None:
        budget = DecodeBudget()
    if budget.total <= 0:
        budget.cuts += 1
        _warn_once(warnings, "WARN: the document's decoded-bytes budget is spent; later streams are skipped")
        return b""
    cap = min(MAX_DECODED_SIZE, budget.total)
    cut = [False]
    data = _run_chain(stream_dict, raw, warnings, budget, cap, cut)
    if len(data) > cap:
        data = data[:cap]
        cut[0] = True
    if cut[0]:
        budget.cuts += 1
        _warn_once(warnings, "WARN: a pdf stream hit the decode cap and was cut")
    budget.total -= len(data)
    return data


def _warn_once(warnings: List[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _run_chain(stream_dict, raw, warnings, budget, cap, cut) -> bytes:
    filters = stream_dict.get("Filter")
    if filters is None:
        return raw  # decode_stream cuts it at the cap
    if not isinstance(filters, list):
        filters = [filters]
    parms = stream_dict.get("DecodeParms", stream_dict.get("DP"))
    parameters = parms if isinstance(parms, list) else [parms]
    data = raw
    for index, filt in enumerate(filters):
        name = str(filt)
        param = parameters[index] if index < len(parameters) else None
        param = param if isinstance(param, dict) else {}
        if name in ("FlateDecode", "Fl"):
            data = _flate(data, warnings, cap, cut)
        elif name in ("LZWDecode", "LZW"):
            early = param.get("EarlyChange", 1)
            if early not in (0, 1):
                warnings.append("WARN: LZWDecode EarlyChange is invalid")
                return b""
            data = _lzw(data, early, warnings, cap, cut)
        elif name in ("RunLengthDecode", "RL"):
            data = _run_length(data, cap, cut)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = _ascii_hex(data, warnings)
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85(data, warnings)
        elif name == "Crypt":
            continue  # the document's handler already decrypted the stream
        elif name in _IMAGE_FILTERS:
            return data
        else:
            warnings.append(f"WARN: unsupported pdf filter: {name}")
            return b""
        if len(data) > cap:
            cut[0] = True
            return data[:cap]
        if name in ("FlateDecode", "Fl", "LZWDecode", "LZW"):
            data = _predictor(data, param, warnings, budget)
    return data


def _lzw(data: bytes, early: int, warnings: List[str], cap: int, cut) -> bytes:
    """ISO 32000-1 7.4.4: MSB-first codes of 9 to 12 bits, Clear and EOD."""
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    width, bitpos, previous = 9, 0, b""
    out = bytearray()
    while bitpos + width <= len(data) * 8:
        bytepos, shift = divmod(bitpos, 8)
        count = (shift + width + 7) // 8
        word = int.from_bytes(data[bytepos:bytepos + count], "big")
        code = (word >> (count * 8 - shift - width)) & ((1 << width) - 1)
        if bitpos == 0 and code != 256:
            warnings.append("WARN: LZWDecode does not start with a Clear code")
            return b""
        bitpos += width
        if code == 256:
            table = table[:258]
            width, previous = 9, b""
            continue
        if code == 257:
            return bytes(out)
        if code < len(table):
            entry = table[code]
        elif code == len(table) and previous:
            entry = previous + previous[:1]
        else:
            warnings.append("WARN: LZWDecode has a bad dictionary code")
            return bytes(out)
        if len(out) + len(entry) > cap:
            out.extend(entry[:cap - len(out)])
            cut[0] = True
            return bytes(out)
        out.extend(entry)
        if previous and len(table) < 4096:
            table.append(previous + entry[:1])
            if width < 12 and len(table) + early == 1 << width:
                width += 1
        previous = entry
    warnings.append("WARN: LZWDecode data ends without EOD")
    return bytes(out)


def _run_length(data: bytes, cap: int, cut) -> bytes:
    """ISO 32000-1 7.4.5: a length byte 0-127 copies n+1 bytes, 129-255 repeats the next byte
    257-n times, 128 ends the data."""
    out = bytearray()
    pos, n = 0, len(data)
    while pos < n:
        length = data[pos]
        pos += 1
        if length == 128:
            break
        if length < 128:
            chunk = data[pos:pos + length + 1]
            pos += length + 1
        else:
            if pos >= n:
                break
            chunk = data[pos:pos + 1] * (257 - length)
            pos += 1
        if len(out) + len(chunk) > cap:
            out.extend(chunk[:cap - len(out)])
            cut[0] = True
            break
        out.extend(chunk)
    return bytes(out)


def _predictor(data: bytes, parms: dict, warnings: List[str], budget: DecodeBudget) -> bytes:
    predictor = parms.get("Predictor", 1)
    if predictor == 1:
        return data
    columns, colors, bits = (parms.get("Columns", 1), parms.get("Colors", 1),
                              parms.get("BitsPerComponent", 8))
    if (not isinstance(predictor, int) or predictor not in (2, 10, 11, 12, 13, 14, 15)
            or not isinstance(columns, int) or not isinstance(colors, int)
            or columns <= 0 or colors <= 0 or not isinstance(bits, int)
            or bits not in (1, 2, 4, 8, 16)
            or columns * colors * bits > MAX_DECODED_SIZE * 8):
        warnings.append("WARN: pdf Predictor parameters are invalid or past the cap")
        return b""
    if predictor == 2:
        return _apply_tiff_predictor(data, columns, colors, bits, warnings, budget)
    return _apply_png_predictor(data, columns, colors, bits, warnings)


def _apply_tiff_predictor(data: bytes, columns: int, colors: int, bits: int,
                          warnings: List[str], budget: DecodeBudget) -> bytes:
    """Per row, add the previous pixel of the same color. Padding is not a sample."""
    samples = columns * colors
    row_width = (samples * bits + 7) // 8
    out = bytearray()
    mask = (1 << bits) - 1
    for pos in range(0, len(data), row_width):
        if len(data) - pos < row_width:
            warnings.append("WARN: TIFF Predictor row is cut; the rest is ignored")
            break
        if bits < 8:
            if samples > budget.samples:
                _warn_once(warnings, "WARN: TIFF Predictor work cap passed; completed rows only")
                break
            budget.samples -= samples
        row = bytearray(data[pos:pos + row_width])
        if bits == 8:
            for i in range(colors, row_width):
                row[i] = (row[i] + row[i - colors]) & 255
        else:
            # One row at a time: never spread a whole row into a big Python int.
            for i in range(colors, samples):
                offset, left_offset = i * bits, (i - colors) * bits
                if bits == 16:
                    at, left = offset // 8, left_offset // 8
                    value = (int.from_bytes(row[at:at + 2], "big") +
                             int.from_bytes(row[left:left + 2], "big")) & mask
                    row[at:at + 2] = value.to_bytes(2, "big")
                else:
                    at, left = offset // 8, left_offset // 8
                    shift, left_shift = 8 - bits - offset % 8, 8 - bits - left_offset % 8
                    value = ((row[at] >> shift) + (row[left] >> left_shift)) & mask
                    row[at] = (row[at] & ~(mask << shift)) | (value << shift)
        out.extend(row)
    return bytes(out)


def _apply_png_predictor(data: bytes, columns: int, colors: int, bits: int,
                         warnings: List[str]) -> bytes:
    bpp = max(1, (colors * bits + 7) // 8)
    row_width = (columns * colors * bits + 7) // 8
    stride = row_width + 1  # each row starts with its filter type byte
    out = bytearray()
    prev = bytearray(row_width)
    pos = 0
    n = len(data)
    while pos < n:
        if n - pos < stride:
            warnings.append("WARN: PNG Predictor row is cut; the rest is ignored")
            break
        filter_type = data[pos]
        row = bytearray(data[pos + 1:pos + stride])
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for i in range(bpp, row_width):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(row_width):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(row_width):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(row_width):
                left = row[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                row[i] = (row[i] + pred) & 0xFF
        else:
            warnings.append(f"WARN: unknown PNG Predictor filter type: {filter_type}")
            return b""
        out += row
        prev = row
    return bytes(out)


def _flate(data: bytes, warnings: List[str], cap: int, cut) -> bytes:
    try:
        decomp = zlib.decompressobj()
        out = decomp.decompress(data, cap)
        if decomp.unconsumed_tail:
            cut[0] = True
        return out
    except zlib.error as e:
        warnings.append(f"WARN: FlateDecode failed: {e}")
        return b""


def _ascii_hex(data: bytes, warnings: List[str]) -> bytes:
    text = data.split(b">")[0]
    text = bytes(c for c in text if c not in _WHITESPACE)
    if len(text) % 2:
        text += b"0"
    try:
        return binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        warnings.append("WARN: ASCIIHexDecode failed")
        return b""


def _ascii85(data: bytes, warnings: List[str]) -> bytes:
    text = bytes(c for c in data if c not in _WHITESPACE)
    if text.startswith(b"<~"):
        text = text[2:]
    if text.endswith(b"~>"):
        text = text[:-2]
    try:
        return base64.a85decode(text)
    except ValueError:
        warnings.append("WARN: ASCII85Decode failed")
        return b""
