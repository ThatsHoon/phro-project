"""pdf text show: per-font glyph tables for the text operators.

Port of the idea of dochan 1.21.0 pdf/_show_table.py (MIT), which snapshots a font once so the
text loop does not call the decoder and the width map for every glyph. dochan builds the
snapshot for its C loop; this module keeps the same snapshot as a memo for the Python loop.

A `GlyphTable` belongs to one FontInfo for its lifetime and is derived from the font alone.
Mutating a FontInfo (space_code, wmode, the width map, the decoder) after its first text
operator leaves the table stale: build a new FontInfo instead.
"""
from typing import List, Tuple

from .fonts import FontInfo

# (piece of text, advance in 1/1000 em, is the word space, width is from the file)
Glyph = Tuple[str, float, bool, bool]


class GlyphTable:
    __slots__ = ("font", "_glyphs", "_space_ok")

    def __init__(self, font: FontInfo):
        self.font = font
        self._glyphs = {}
        # An Encoding CMap that does not take the one-byte code 32 gives no word space.
        cmap = font.encoding_cmap
        self._space_ok = cmap is None or bool(cmap._space(1, 32))

    def chunks(self, raw: bytes) -> List[Tuple[bytes, int]]:
        """Split a string into (code bytes, code or CID), as the font's encoding reads it."""
        font = self.font
        if font.encoding_cmap is not None:
            return list(font.encoding_cmap.iter_codes(raw, font.encoding_cmap.warnings))
        if font.code_bytes == 2:
            return [(raw[i:i + 2], (raw[i] << 8) | raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
        return [(bytes((byte,)), byte) for byte in raw]

    def glyph(self, chunk: bytes, code: int) -> Glyph:
        """The decoded text and horizontal advance of one code."""
        known = self._glyphs.get(chunk)
        if known is None:
            font = self.font
            known = (font.decode(chunk), font.widths.advance(code) / 1000.0,
                     chunk == b" " and self._space_ok, bool(font.widths.explicit(code)))
            self._glyphs[chunk] = known
        return known


def font_table(font: FontInfo) -> GlyphTable:
    """The glyph table of a font, built on first use and kept on the FontInfo."""
    try:
        return font._glyph_table
    except AttributeError:
        font._glyph_table = GlyphTable(font)
        return font._glyph_table
