"""pdf fonts: per-font decoding of text codes and glyph widths.

Port of dochan 1.21.0 pdf/reader.py (font section, MIT) and of FontInfo, VerticalMetrics and
default_byte_decoder from pdf/content.py. `font_infos` reads a page's /Font resources into one
FontInfo per name, cached by object reference. A font decodes through, in order: its ToUnicode
CMap, the Adobe CID to Unicode table of its character collection, the reverse of its embedded
TrueType cmap, the Core 14 glyph names, and last the bare bytes. A Type0 font that none of
these reaches is `unmapped`.

A cap that drops content is counted on the PDFFile `cuts`, once, under one kind.
"""
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .cid_unicode import (MAX_FONT_BYTES, CIDDecoder, adobe_cid, adobe_space_cid,
                          reverse_truetype_cmap)
from .cmap import encoding_wmode, parse_encoding_cmap, parse_tounicode, predefined_cmap
from .core14 import Core14Decoder, canonical_font, glyph_names
from .core14_metrics import GLYPH_WIDTHS
from .objects import PDFName, PDFRef, PDFStream
from .structure import PDFFile
from .widths import WidthMap

MAX_DIFFERENCES = 4096
MAX_CID_WIDTH_ITEMS = 65536
MAX_CID_TO_GID_BYTES = 131072
MAX_TRUETYPE_CMAP_FONTS = 16


def default_byte_decoder(raw: bytes) -> str:
    """The default reading of the bytes of a simple font that has no ToUnicode."""
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _drop_decoder(raw: bytes) -> str:
    return ""


class VerticalMetrics:
    """CIDFont /W2 and /DW2 (w1y, v1x, v1y), ISO 32000-1 9.7.4.3.

    The default vertical origin x is half the horizontal width. DW2 is [v1y, w1y]. The CID
    ranges and the whole mapping are capped at 65,536.
    """

    def __init__(self, w_array, default_array, widths: WidthMap):
        self._widths = widths
        self._table: Dict[int, Tuple[float, float, float]] = {}
        self.warnings: List[str] = []
        self._v1y, self._w1y = 880.0, -1000.0
        if isinstance(default_array, list) and len(default_array) >= 2:
            if self._numbers(default_array[:2]):
                self._v1y, self._w1y = map(float, default_array[:2])
            else:
                self._warn()
        if not isinstance(w_array, list):
            return
        i = 0
        remaining = 65536
        while i < min(len(w_array), 65536 * 5):
            if remaining <= 0:
                self._warn()
                break
            start = w_array[i]
            if not isinstance(start, int) or not 0 <= start <= 65535:
                self._warn()
                i += 1
                continue
            if i + 1 < len(w_array) and isinstance(w_array[i + 1], list):
                entries = w_array[i + 1]
                limit = min(len(entries) // 3, 65536 - start, remaining)
                for offset in range(limit):
                    triple = entries[offset * 3:offset * 3 + 3]
                    if self._numbers(triple):
                        self._table[start + offset] = tuple(map(float, triple))
                    else:
                        self._warn()
                remaining -= limit
                if len(entries) % 3 or len(entries) // 3 > limit:
                    self._warn()
                i += 2
            elif i + 4 < len(w_array):
                end = w_array[i + 1]
                triple = w_array[i + 2:i + 5]
                if isinstance(end, int) and start <= end <= 65535 and self._numbers(triple):
                    metrics = tuple(map(float, triple))
                    count = min(end - start + 1, remaining)
                    for cid in range(start, start + count):
                        self._table[cid] = metrics
                    remaining -= count
                    if count < end - start + 1:
                        self._warn()
                else:
                    self._warn()
                i += 5
            else:
                self._warn()
                break
        if i < len(w_array):
            self._warn()

    @staticmethod
    def _numbers(values):
        try:
            return all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in values)
        except OverflowError:
            return False

    def _warn(self):
        if not self.warnings:
            self.warnings.append("WARN: vertical CID glyph metrics are damaged or passed the cap; using defaults")

    def metrics(self, cid: int) -> Tuple[float, float, float]:
        return self._table.get(cid, (self._w1y, self._widths.advance(cid) / 2.0, self._v1y))


@dataclass
class FontInfo:
    """What the text operators need to know about one font."""
    decode: Callable[[bytes], str]
    widths: WidthMap
    code_bytes: int = 1  # 1 = simple font, 2 = Type0/Identity-H CID
    bold: bool = False
    italic: bool = False
    wmode: int = 0
    vertical_metrics: Optional[VerticalMetrics] = None
    link_metrics_reliable: bool = True
    space_code: int = 32  # ToUnicode may assign a CID other than 32 to U+0020.
    encoding_cmap: object = None
    # True for a font whose codes have no ToUnicode entry, no known CID ordering and no usable
    # embedded cmap: its text cannot be recovered.
    unmapped: bool = False


def font_infos(pdf: PDFFile, resources, cache: dict) -> Dict[str, FontInfo]:
    """The FontInfo of each /Font resource name. `cache` maps an object ref to its FontInfo,
    so a font is not read again for every page."""
    infos: Dict[str, FontInfo] = {}
    if not isinstance(resources, dict):
        return infos
    fonts = pdf.resolve(resources.get("Font"))
    if not isinstance(fonts, dict):
        return infos
    for name, font_ref in fonts.items():
        cache_key = font_ref if isinstance(font_ref, PDFRef) else None
        if cache_key is not None and cache_key in cache:
            infos[str(name)] = cache[cache_key]
            continue
        font = pdf.resolve(font_ref)
        if not isinstance(font, dict):
            continue
        info = _build_font_info(pdf, str(name), font)
        infos[str(name)] = info
        if cache_key is not None:
            cache[cache_key] = info
    return infos


def _build_font_info(pdf: PDFFile, name: str, font: dict) -> FontInfo:
    subtype = str(font.get("Subtype", ""))
    encoding = pdf.resolve(font.get("Encoding"))
    encoding_cmap = None
    if subtype == "Type0" and isinstance(encoding, PDFStream):
        encoding_cmap = _encoding_stream_cmap(pdf, encoding)
    elif (subtype == "Type0" and isinstance(encoding, PDFName)
          and str(encoding) not in ("Identity-H", "Identity-V")):
        encoding_cmap = predefined_cmap(str(encoding))
    decoder = build_decoder(pdf, name, font, encoding_cmap)
    if subtype == "Type0":
        code_bytes = 2  # Identity-H/V: a 2 byte CID, the usual Korean font
        widths = cid_widths(pdf, font)
        descriptor_font = _cid_descendant(pdf, font)
    else:
        code_bytes = 1
        widths = simple_widths(pdf, font)
        descriptor_font = font
    bold, italic = _font_style_flags(pdf, font, descriptor_font)
    wmode, vertical_metrics = 0, None
    if subtype == "Type0":
        if isinstance(encoding, PDFStream):
            wmode = encoding_wmode(data=pdf.decode_stream_bytes(encoding),
                                   dictionary_mode=pdf.resolve(encoding.dictionary.get("WMode")))
        elif encoding_cmap is not None:
            wmode = encoding_cmap.wmode
        else:
            wmode = encoding_wmode(str(encoding or ""))
        if wmode == 1:
            vertical_metrics = VerticalMetrics(pdf.resolve(descriptor_font.get("W2")),
                                               pdf.resolve(descriptor_font.get("DW2")), widths)
            for message in vertical_metrics.warnings:
                pdf._warn_once(message)
    reliable = (subtype in ("Type1", "TrueType", "MMType1") or
                subtype == "Type0" and str(pdf.resolve(font.get("Encoding"))) == "Identity-H")
    has_unicode_map = (getattr(decoder, "mapping", None) or
                       getattr(getattr(decoder, "__self__", None), "mapping", None))
    reliable = reliable and getattr(getattr(decoder, "__self__", None), "reliable", True)
    if not has_unicode_map:
        base_font = str(pdf.resolve(font.get("BaseFont")) or "")
        standard_font = base_font in {
            "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
            "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
            "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"}
        reliable = reliable and (str(encoding) in ("WinAnsiEncoding", "MacRomanEncoding")
                                 or encoding is None and standard_font and subtype != "TrueType")
    space_code = 32
    if encoding_cmap is not None:
        selected_space = None
        if isinstance(has_unicode_map, dict):
            spaces = set()
            for key, value in has_unicode_map.items():
                if value != " " or not isinstance(key, tuple) or len(key) != 2:
                    continue
                length, code = key
                if length not in (1, 2, 3, 4):
                    continue
                if not encoding_cmap._space(length, code):
                    continue
                cid = encoding_cmap._lookup(length, code)
                if cid is not None and widths.explicit(cid):
                    spaces.add(cid)
            if len(spaces) == 1:
                selected_space = spaces.pop()
        if selected_space is None:
            recovered_space = getattr(getattr(decoder, "__self__", None), "space_code", None)
            if recovered_space is not None:
                selected_space = recovered_space
        if selected_space is None:
            for candidate in (b" ", b"\x00\x20"):
                length = len(candidate)
                code = int.from_bytes(candidate, "big")
                if encoding_cmap._space(length, code):
                    mapped_space = encoding_cmap._lookup(length, code)
                    if mapped_space is not None:
                        selected_space = mapped_space
                        break
        if selected_space is not None:
            space_code = selected_space
    elif ((subtype == "Type0" and str(encoding) == "Identity-H")
          or subtype in ("Type1", "TrueType", "MMType1")) and isinstance(has_unicode_map, dict):
        # CID 32 is not necessarily a space. Use a uniquely identified, explicitly measured
        # U+0020, not the width of an unrelated glyph.
        spaces = [key[1] for key, value in has_unicode_map.items()
                  if value == " " and isinstance(key, tuple) and len(key) == 2
                  and key[0] == code_bytes and 0 <= key[1] < 256 ** code_bytes
                  and widths.explicit(key[1])]
        if len(spaces) == 1:
            space_code = spaces[0]
    elif subtype == "Type0" and str(encoding) == "Identity-H":
        recovered_space = getattr(getattr(decoder, "__self__", None), "space_code", None)
        if recovered_space is not None:
            space_code = recovered_space
    return FontInfo(decode=decoder, widths=widths, code_bytes=code_bytes,
                    bold=bold, italic=italic, wmode=wmode, vertical_metrics=vertical_metrics,
                    link_metrics_reliable=reliable, space_code=space_code,
                    encoding_cmap=encoding_cmap, unmapped=decoder is _drop_decoder)


def _encoding_stream_cmap(pdf: PDFFile, stream: PDFStream, seen=None):
    """Resolve a stream and its dictionary parents once per PDF, with a cycle bound."""
    cache = getattr(pdf, "_encoding_cmap_cache", None)
    if cache is None:
        cache = pdf._encoding_cmap_cache = {}
    key = id(stream)
    cached = cache.get(key)
    if cached is not None and cached[0] is stream:
        return cached[1]
    seen = set(seen or ())
    if key in seen or len(seen) >= 8:
        pdf._warn_once("WARN: Encoding CMap /UseCMap loops or passed the depth cap")
        return None
    seen.add(key)
    parent = None
    parent_obj = pdf.resolve(stream.dictionary.get("UseCMap"))
    if isinstance(parent_obj, PDFName):
        parent = predefined_cmap(str(parent_obj))
    elif isinstance(parent_obj, PDFStream):
        parent = _encoding_stream_cmap(pdf, parent_obj, seen)
    decoded = pdf.decode_stream_bytes(stream)
    cmap = parse_encoding_cmap(decoded, pdf.warnings, parent_cmap=parent)
    for kind, message in cmap.cut_notes:
        pdf._cut(kind, message)
    system = pdf.resolve(stream.dictionary.get("CIDSystemInfo"))
    if isinstance(system, dict):
        for key_name, attr in (("Registry", "registry"), ("Ordering", "ordering")):
            value = pdf.resolve(system.get(key_name))
            if isinstance(value, bytes) and len(value) <= 32:
                setattr(cmap, attr, value.decode("ascii", "ignore").strip())
    if not cmap.has_codespace():
        pdf._warn_once("WARN: Encoding CMap has no code space; using the 2 byte path")
        cmap = None
    cache[key] = (stream, cmap)
    return cmap


def _cid_descendant(pdf: PDFFile, font: dict):
    descendants = pdf.resolve(font.get("DescendantFonts"))
    if isinstance(descendants, list) and descendants:
        cid = pdf.resolve(descendants[0])
        if isinstance(cid, dict):
            return cid
    return font


def _font_style_flags(pdf: PDFFile, font: dict, descriptor_font: dict):
    """Bold and italic from the BaseFont name and the FontDescriptor /Flags."""
    base = str(pdf.resolve(font.get("BaseFont")) or "").lower()
    bold = "bold" in base
    italic = "italic" in base or "oblique" in base
    descriptor = pdf.resolve(descriptor_font.get("FontDescriptor"))
    if isinstance(descriptor, dict):
        flags = pdf.resolve(descriptor.get("Flags"))
        if isinstance(flags, int):
            italic = italic or bool(flags & (1 << 6))       # the Italic bit
            bold = bold or bool(flags & (1 << 18))          # the ForceBold bit
        weight = pdf.resolve(descriptor.get("FontWeight"))
        if isinstance(weight, (int, float)) and weight >= 600:
            bold = True
    return bold, italic


def simple_widths(pdf: PDFFile, font: dict) -> WidthMap:
    first = pdf.resolve(font.get("FirstChar"))
    arr = pdf.resolve(font.get("Widths"))
    if isinstance(first, int) and isinstance(arr, list):
        return WidthMap.simple(first, [pdf.resolve(w) for w in arr], default=500.0)
    base, names = _core14_encoding(pdf, font)
    if names is not None and "Widths" not in font:
        metrics = GLYPH_WIDTHS[base]
        return WidthMap({code: metrics[glyph] for code, glyph in names.items()
                         if glyph in metrics}, 500.0)
    return WidthMap({}, 500.0)


def _core14_encoding(pdf: PDFFile, font: dict):
    if str(font.get("Subtype", "")) not in ("Type1", "TrueType", "MMType1"):
        return None, None
    base = str(pdf.resolve(font.get("BaseFont")) or "")
    descriptor = pdf.resolve(font.get("FontDescriptor"))
    if isinstance(descriptor, dict) and any(key in descriptor for key in
                                           ("FontFile", "FontFile2", "FontFile3")):
        return None, None
    flags = pdf.resolve(descriptor.get("Flags")) if isinstance(descriptor, dict) else 0
    if (str(font.get("Subtype")) == "TrueType" and isinstance(flags, int) and flags & 4
            and canonical_font(base) not in ("Symbol", "ZapfDingbats")):
        # A symbolic TrueType cmap cannot be inferred from an Arial alias.
        return None, None
    encoding = pdf.resolve(font.get("Encoding"))
    if isinstance(encoding, dict):
        encoding = {key: pdf.resolve(value) for key, value in encoding.items()}
        if isinstance(encoding.get("Differences"), list):
            if len(encoding["Differences"]) > MAX_DIFFERENCES:
                pdf._cut("font_differences",
                         f"WARN: pdf font Differences passed the cap ({MAX_DIFFERENCES}); the encoding is withheld")
                return None, None
            encoding["Differences"] = [pdf.resolve(value) for value in encoding["Differences"]]
    return canonical_font(base), glyph_names(base, encoding,
                                              truetype=str(font.get("Subtype")) == "TrueType")


def cid_widths(pdf: PDFFile, font: dict) -> WidthMap:
    descendants = pdf.resolve(font.get("DescendantFonts"))
    cid_font = None
    if isinstance(descendants, list) and descendants:
        cid_font = pdf.resolve(descendants[0])
    if not isinstance(cid_font, dict):
        return WidthMap({}, 1000.0)
    dw = pdf.resolve(cid_font.get("DW"))
    default = float(dw) if isinstance(dw, (int, float)) else 1000.0
    w = pdf.resolve(cid_font.get("W"))
    if not isinstance(w, list):
        widths = WidthMap.cid([], default_width=default)
        for message in widths.warnings:
            pdf._warn_once(message)
        return widths
    resolved = []
    budget = MAX_CID_WIDTH_ITEMS
    truncated = len(w) > MAX_CID_WIDTH_ITEMS
    for item in w[:MAX_CID_WIDTH_ITEMS]:
        value = pdf.resolve(item)
        if isinstance(value, list):
            truncated = truncated or len(value) > budget
            resolved.append([pdf.resolve(entry) for entry in value[:budget]])
            budget -= min(len(value), budget)
        else:
            resolved.append(value)
    widths = WidthMap.cid(resolved, default_width=default)
    capped = truncated
    for message in widths.warnings:
        if "cap" in message:
            capped = True
        else:
            pdf._warn_once(message)
    if capped:
        pdf._cut("cid_widths", f"WARN: pdf CID width array passed the cap ({MAX_CID_WIDTH_ITEMS})")
        widths.reliable = False
    return widths


def build_decoder(pdf: PDFFile, name: str, font: dict, encoding_cmap=None) -> Callable[[bytes], str]:
    to_unicode = pdf.resolve(font.get("ToUnicode"))
    if isinstance(to_unicode, PDFStream):
        cmap_data = pdf.decode_stream_bytes(to_unicode)
        if cmap_data:
            local: List[str] = []
            cmap = parse_tounicode(cmap_data, local)
            if cmap.cut:
                pdf._cut(cmap.cut, local[-1])
            if cmap.mapping:
                if encoding_cmap is not None:
                    def decode_encoded(raw):
                        return "".join(cmap.decode(chunk) for chunk, _cid in
                                       encoding_cmap.iter_codes(raw, pdf.warnings)
                                       if encoding_cmap._space(len(chunk),
                                                               int.from_bytes(chunk, "big")))
                    decode_encoded.mapping = cmap.mapping
                    return decode_encoded
                return cmap.decode
    if str(font.get("Subtype", "")) == "Type0":
        recovered = cid_fallback_decoder(pdf, name, font, encoding_cmap)
        if recovered is not None:
            if encoding_cmap is not None:
                return lambda raw: "".join(recovered.decode(cid.to_bytes(2, "big"))
                                           for _chunk, cid in encoding_cmap.iter_codes(raw, pdf.warnings))
            return recovered.decode
    # Reading a CID font that has no ToUnicode as cp1252 would leak NULs and other control
    # characters into the text: warn and drop that text.
    encoding = pdf.resolve(font.get("Encoding"))
    if isinstance(encoding, dict):
        encoding = pdf.resolve(encoding.get("BaseEncoding"))
    encoding_name = str(encoding) if isinstance(encoding, PDFName) else ""
    _base, names = _core14_encoding(pdf, font)
    if names is not None:
        macroman = encoding_name == "MacRomanEncoding"
        fallback = (lambda raw: raw.decode("mac_roman", errors="replace")) if macroman else default_byte_decoder
        return Core14Decoder(names, fallback, preserve_undefined=macroman).decode
    if str(font.get("Subtype", "")) == "Type0" or encoding_name.startswith("Identity-"):
        pdf._warn_once(f"WARN: font {name}: CID font without ToUnicode; its text cannot be extracted")
        return _drop_decoder
    if encoding_name == "MacRomanEncoding":
        return lambda raw: raw.decode("mac_roman", errors="replace")
    return default_byte_decoder


def cid_fallback_decoder(pdf: PDFFile, name: str, font: dict, encoding_cmap=None):
    encoding = pdf.resolve(font.get("Encoding"))
    if encoding_cmap is None and (not isinstance(encoding, PDFName)
                                  or encoding not in ("Identity-H", "Identity-V")):
        return None  # another predefined CMap needs a code to CID table
    descendant = _cid_descendant(pdf, font)
    system = pdf.resolve(descendant.get("CIDSystemInfo"))
    descriptor = pdf.resolve(descendant.get("FontDescriptor"))
    font_stream = (pdf.resolve(descriptor.get("FontFile2"))
                   if isinstance(descriptor, dict) else None)
    gid_object = pdf.resolve(descendant.get("CIDToGIDMap"))
    embedded_identity_gid = (str(pdf.resolve(descendant.get("Subtype"))) == "CIDFontType2"
                             and isinstance(font_stream, PDFStream)
                             and not isinstance(gid_object, PDFStream))
    if isinstance(system, dict):
        registry = pdf.resolve(system.get("Registry"))
        ordering = pdf.resolve(system.get("Ordering"))
        registry = (registry.decode("ascii", "ignore") if isinstance(registry, bytes)
                    and len(registry) <= 32 else str(registry) if isinstance(registry, PDFName) else "")
        ordering = (ordering.decode("ascii", "ignore") if isinstance(ordering, bytes)
                    and len(ordering) <= 32 else str(ordering) if isinstance(ordering, PDFName) else "")
        if encoding_cmap is not None and (registry != encoding_cmap.registry
                                          or ordering != encoding_cmap.ordering):
            pdf._warn_once(f"WARN: font {name}: Encoding CMap and CIDFont CIDSystemInfo do not match")
            return None
        if (registry == "Adobe" and ordering in ("Japan1", "GB1", "CNS1", "Korea1", "KR")
                and not embedded_identity_gid):
            return CIDDecoder(lambda cid: adobe_cid(ordering, cid), pdf.warnings, name,
                              adobe_space_cid(ordering))
    if pdf.resolve(descendant.get("Subtype")) != "CIDFontType2":
        return None
    if not isinstance(descriptor, dict):
        return None
    if not isinstance(font_stream, PDFStream):
        return None
    dropped = getattr(pdf, "_cid_font_dropped", None)
    if dropped is None:
        dropped = pdf._cid_font_dropped = set()
    if len(font_stream.raw) > MAX_FONT_BYTES:
        if id(font_stream) not in dropped:
            dropped.add(id(font_stream))
            pdf._cut("truetype_font_bytes",
                     "WARN: embedded TrueType font passed the size cap; CID recovery is withheld")
        return None
    cache = getattr(pdf, "_cid_cmap_cache", None)
    if cache is None:
        cache = {}
        pdf._cid_cmap_cache = cache
    cache_key = id(font_stream)
    entry = cache.get(cache_key)
    if entry is not None and entry[0] is font_stream:
        reverse = entry[1]
    else:
        if len(cache) >= MAX_TRUETYPE_CMAP_FONTS:
            if id(font_stream) not in dropped:  # once per font, however often it is read
                dropped.add(id(font_stream))
                pdf._cut("truetype_fonts",
                         "WARN: the document's TrueType cmap scan cap passed; CID recovery is withheld")
            return None
        warning_count = len(pdf.warnings)
        font_bytes = pdf.decode_stream_bytes(font_stream)
        if not font_bytes:
            # The CID warning covers a corrupt optional font stream; keep the document-wide
            # budget warnings of the decoder.
            pdf.warnings[warning_count:] = [warning for warning in pdf.warnings[warning_count:]
                                            if not warning.startswith("WARN: FlateDecode failed")]
            cache[cache_key] = (font_stream, {})
            return None
        if len(font_bytes) > MAX_FONT_BYTES:
            if id(font_stream) not in dropped:
                dropped.add(id(font_stream))
                pdf._cut("truetype_font_bytes",
                         "WARN: embedded TrueType font passed the size cap; CID recovery is withheld")
            return None
        reverse = reverse_truetype_cmap(font_bytes, pdf._cut)
        cache[cache_key] = (font_stream, reverse)
    if not reverse:
        return None
    gid_bytes = None
    if isinstance(gid_object, PDFStream):
        if len(gid_object.raw) > MAX_CID_TO_GID_BYTES:
            pdf._cut("cid_to_gid", "WARN: CIDToGIDMap passed the size cap; CID recovery is withheld")
            return None
        gid_bytes = pdf.decode_stream_bytes(gid_object)
        if len(gid_bytes) > MAX_CID_TO_GID_BYTES:
            pdf._cut("cid_to_gid", "WARN: CIDToGIDMap passed the size cap; CID recovery is withheld")
            return None
    elif gid_object is not None and gid_object != "Identity":
        return None
    if gid_bytes is None:
        return CIDDecoder(lambda cid: reverse.get(cid, ""), pdf.warnings, name,
                          next((gid for gid, value in reverse.items() if value == " "), None))
    space_gid = next((gid for gid, value in reverse.items() if value == " "), None)
    space_cid = (next((cid for cid in range(len(gid_bytes) // 2)
                       if int.from_bytes(gid_bytes[2 * cid:2 * cid + 2], "big") == space_gid), None)
                 if space_gid is not None else None)
    return CIDDecoder(lambda cid: reverse.get(int.from_bytes(gid_bytes[2 * cid:2 * cid + 2], "big"), "")
                      if 2 * cid + 2 <= len(gid_bytes) else "", pdf.warnings, name,
                      space_cid)
