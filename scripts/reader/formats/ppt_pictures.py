"""PowerPoint 97-2003 (.ppt) pictures, for ppt.py, through officeart.py (the same placement rules as xls_drawing.py).

A picture shape carries a pib, a 1-based index into the BStore of the DocumentContainer's PPDrawingGroup (0x040B). Each
BStore entry is an FBSE whose BLIP sits in the `Pictures` stream at its foDelay (or is embedded in the FBSE). A pib is
decoded once; every placement of it is charged to IMAGE_BYTES (copies counted). As doc's `delayed`, a Pictures-stream
record is read once an offset, and charged 8 + its length against the stream's length before it is read: foDelay
offsets naming records nested in one another share their bytes, so only the first that fits is read and the others
are None, unread (the reads stay linear in the stream). A placement is counted under `image` when its pib is outside
the BStore, its BLIP cannot be read, passes the stream or what is left of its read, or is not a kind Assets keeps,
there is no `Pictures` stream, or it passes IMAGE_BYTES.
"""
from .. import officeart
from ..containers import flate
from ..limits import IMAGE_BYTES
from ..model import Image


class Pictures:
    """The pictures of one presentation. `doc` is the PowerPoint Document stream, `dgg` the (start, end) of the
    DggContainer in it (None without a PPDrawingGroup), `pictures` the Pictures stream (None when absent)."""

    def __init__(self, doc: bytes, dgg: tuple[int, int] | None, pictures: bytes | None, budget: flate.Budget, ctx):
        self.pictures, self.budget, self.ctx = pictures, budget, ctx
        self.dgg = bytes(doc[dgg[0]:dgg[1]]) if dgg is not None else b""
        self.store = officeart.bstore(self.dgg)
        self.blips: dict[int, bytes | None] = {}
        self.delayed: dict[int, bytes | None] = {}   # Pictures offset -> its image bytes or None
        self.delay_read = 0                           # Pictures bytes read so far, at most the stream's length
        self.spent = 0

    def _decode(self, pib: int) -> bytes | None:
        if not 0 < pib <= len(self.store):
            return None
        off = self.store[pib - 1]
        at = officeart.fbse_delay(self.dgg, off)
        if at is None:  # an embedded BLIP, or an FBSE fbse_blip refuses without a delay stream
            return officeart.blip_image(self.dgg, off, None, self.budget)
        if at not in self.delayed:
            self.delayed[at] = self._delay(at)
        return self.delayed[at]

    def _delay(self, at: int) -> bytes | None:
        """The image of the Pictures record at `at`, charged to the delay read before it is read."""
        pics = self.pictures
        if pics is None:
            return None
        h = officeart.header(pics, at)
        size = 8 + h[3] if h is not None else 0
        if not size or at + size > len(pics) or self.delay_read + size > len(pics):
            return None
        self.delay_read += size
        return officeart.blip_image(pics, at, None, self.budget)

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
