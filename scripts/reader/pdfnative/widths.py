"""Glyph widths of a pdf font, for the advance of text.

Port of dochan 1.21.0 pdf/widths.py (MIT). Widths are in text space units (1/1000 em): a
simple font from /Widths and /FirstChar, a composite (Type0, CID) font from the /W array and
the /DW default. With them the x of each glyph accumulates exactly, which rebuilds column
edges, reading order and word gaps.
"""
import math
from typing import Dict, List


class WidthMap:
    """A character code (or CID) to its advance width (1/1000 em)."""

    def __init__(self, widths: Dict[int, float], default: float, cid_default: bool = False):
        self._widths = widths
        self._default = default
        self._cid_default = cid_default
        self.reliable = True
        self.warnings = []

    def advance(self, code: int) -> float:
        return self._widths.get(code, self._default)

    def explicit(self, code: int) -> bool:
        """Tells a width from the document, the AFM or a standard CID from an unfounded default."""
        return self.reliable and (code in self._widths or (self._cid_default and 0 <= code <= 65535))

    @classmethod
    def simple(cls, first_char: int, widths: List, default: float = 500.0) -> "WidthMap":
        table: Dict[int, float] = {}
        for i, w in enumerate(widths):
            if isinstance(w, (int, float)) and math.isfinite(w) and 0 <= first_char + i < 256:
                table[first_char + i] = float(w)
        return cls(table, default)

    @classmethod
    def cid(cls, w_array: List, default_width: float = 1000.0) -> "WidthMap":
        """Parse a /W array. Two forms: `c [w1 w2 ...]` and `c_first c_last w`."""
        table: Dict[int, float] = {}
        warnings = []
        if not math.isfinite(default_width):
            default_width = 1000.0
            warnings.append("WARN: pdf CID default width is not finite; using the standard default")
        i = 0
        n = min(len(w_array), 65536)
        budget = 65536
        while i < n:
            if not isinstance(w_array[i], int):
                i += 1
                continue
            start = int(w_array[i])
            if i + 1 < n and isinstance(w_array[i + 1], list):
                count = min(len(w_array[i + 1]), budget)
                for j, w in enumerate(w_array[i + 1][:count]):
                    if 0 <= start + j <= 65535 and isinstance(w, (int, float)) and math.isfinite(w):
                        table[start + j] = float(w)
                budget -= len(w_array[i + 1])
                i += 2
            elif i + 2 < n and isinstance(w_array[i + 1], int) \
                    and isinstance(w_array[i + 2], (int, float)) and math.isfinite(w_array[i + 2]):
                end = int(w_array[i + 1])
                w = float(w_array[i + 2])
                if 0 <= start <= end <= 65535:
                    for code in range(start, min(end + 1, start + budget)):
                        table[code] = w
                    budget -= end - start + 1
                i += 3
            else:
                i += 1
            if budget <= 0 and i < n:
                break
        if i < len(w_array) or budget < 0:
            warnings.append("WARN: pdf CID width array passed the cap (65536); only part is used")
        result = cls(table, default_width, cid_default=not warnings)
        result.reliable = not warnings
        result.warnings = warnings
        return result
