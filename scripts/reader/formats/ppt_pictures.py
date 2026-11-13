"""PowerPoint 97-2003 (.ppt) pictures, for ppt.py, through officeart.py (the same placement rules as xls_drawing.py).

A picture shape carries a pib, a 1-based index into the BStore of the DocumentContainer's PPDrawingGroup (0x040B). Each
BStore entry is an FBSE whose BLIP sits in the `Pictures` stream at its foDelay (or is embedded in the FBSE). A pib is
decoded once; every placement of it is charged to IMAGE_BYTES (copies counted). A placement is counted under `image`
when its pib is outside the BStore, its BLIP cannot be read or is not a kind Assets keeps, there is no `Pictures`
stream, or it passes IMAGE_BYTES.
"""
from .. import officeart
from ..containers import flate
from ..images import sniff
from ..limits import IMAGE_BYTES, ReadError
from ..model import Image


class Pictures:
    """The pictures of one presentation. `doc` is the PowerPoint Document stream, `dgg` the (start, end) of the
    DggContainer in it (None without a PPDrawingGroup), `pictures` the Pictures stream (None when absent)."""

    def __init__(self, doc: bytes, dgg: tuple[int, int] | None, pictures: bytes | None, budget: flate.Budget, ctx):
        self.pictures, self.budget, self.ctx = pictures, budget, ctx
        self.dgg = bytes(doc[dgg[0]:dgg[1]]) if dgg is not None else b""
        self.store = officeart.bstore(self.dgg)
        self.blips: dict[int, bytes | None] = {}
        self.spent = 0

    def _decode(self, pib: int) -> bytes | None:
        if not 0 < pib <= len(self.store):
            return None
        try:
            data = officeart.fbse_blip(self.dgg, self.store[pib - 1], self.pictures, self.budget)
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            return None
        return data if data is not None and len(data) >= 16 and sniff(data) is not None else None

    def image(self, pib: int | None) -> Image | None:
        """One placement of picture `pib`: its Image, or None after counting `image`."""
        if pib not in self.blips:
            self.blips[pib] = self._decode(pib) if pib else None
        data = self.blips[pib]
        if data is not None:
            self.spent += len(data)
            if self.spent <= IMAGE_BYTES:
                return Image(data)
        self.ctx.skipped["image"] += 1
        return None
