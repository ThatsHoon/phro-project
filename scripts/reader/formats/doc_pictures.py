"""Word 97-2003 binary (.doc) pictures, for doc.py's \\x01 and \\x08 handlers, through officeart.py (logic after dochan
office_binary/doc_images.py).

An inline picture is the PICFAndOfficeArtData at its PicLocation in the Data stream: the PICF, then the inline
SpContainer and the FBSE that holds the picture, or the shape's pib in the BStore. A floating one is the FSPA at its CP
in PlcSpaMom, naming a shape of the DggInfo's drawings: its pib's BStore entry, or for a group shape its members'
pictures in record order. A BStore entry's BLIP is in the entry or delay-loaded from WordDocument.

What is read is bounded as doc.py's huge PAPXs are: the PICFs of a well-formed file never overlap and neither do its
delay-loaded BLIPs, so the PICFs read in all are capped at len(Data) bytes, and the delay BLIPs at len(WordDocument),
each charged before anything is copied; one past its cap is a lost picture. A delay BLIP is read once an offset, so
FBSEs that share it share one copy, and a group's members are placed once a member in all.
"""
import struct

from .. import officeart
from ..limits import ReadError

NOT_PICTURE = object()  # an inline drawing object without a picture: a horizontal rule, inline WordArt, an AutoShape
SPA_MOM = 40        # the FibRgFcLcb97 index of PlcSpaMom, the main story's PlcfSpa, its CPs the main story's
_FSPA = 26          # bytes of an FSPA: spid u32 first
_DGG_INFO = 50      # the FibRgFcLcb97 index of the OfficeArtContent
_DG_CONTAINER = 0xF002
_MM_WMF, _MM_SHAPE, _MM_SHAPEFILE = 8, 0x64, 0x66  # PICF mfpf.mm: a raw WMF, an inline shape, one with a name
_MISS = object()


class Drawing:
    """The document's drawings (Pictures.drawing): the anchors of PlcSpaMom ({CP: spid}), the shapes, the BStore's
    file blocks and the DggInfo bytes they are offsets in. `damaged` is set when reading them counted a loss, so an
    anchor whose shape is not found is not counted again."""

    def __init__(self):
        self.anchors = {}
        self.shapes = officeart.Shapes()
        self.store = []
        self.dgg = b""
        self.damaged = False


class Pictures:
    """The pictures of one document, read for `reader` (doc._Reader: its ctx, flate budget, FIB, table and
    WordDocument streams, Data stream and depth counter), each read once."""

    def __init__(self, reader):
        self.r = reader
        self._inline, self._stored, self._delayed = {}, {}, {}  # Data offset, pib, (stream, offset) -> picture
        self._picf_read = 0    # PICF bytes read in all (capped at len(Data))
        self._delay_read = 0   # delay BLIP bytes read in all (capped at len(WordDocument))
        self._members = 0      # group members placed in all (capped at the drawings' pictures)
        self._drawing = None

    def inline(self, at):
        """The picture of the PICF at Data offset `at`, read once an offset: bytes, None when it cannot be read, or
        NOT_PICTURE."""
        hit = self._inline.get(at, _MISS)
        if hit is _MISS:
            hit = self._inline[at] = self.picf(at)
        return hit

    def floating(self, cp) -> list[int]:
        """The pibs of the shape anchored at main-story CP `cp`: none without an FSPA there (ruling: no loss), none for
        a shape without a pib (a text box, whose text doc.boxes reads, a line), its own pib, or a group's members'
        pibs in record order. An FSPA whose shape no drawing holds is counted under shape, unless drawing() counted
        the damage that lost it. A group anchored past its members' share (each member once in all, as Word writes
        them) has its pictures counted under image instead."""
        d = self.drawing()
        spid = d.anchors.get(cp)
        if spid is None:
            return []
        sh = d.shapes
        at = sh.at.get(spid)
        if at is None:
            if not d.damaged:
                self.r.ctx.skipped["shape"] += 1
            return []
        span = sh.groups.get(at)
        if span is None:
            pib = sh.pib.get(at)
            return [pib] if pib else []
        i, j = span
        self._members += j - i
        if self._members > len(sh.pics):
            self.r.ctx.skipped["image"] += j - i
            return []
        return sh.pics[i:j]

    def stored(self, pib) -> bytes | None:
        """BStore entry `pib` (1-based) as image bytes, read once a pib; None past the BStore or when its FBSE or BLIP
        cannot be read."""
        hit = self._stored.get(pib, _MISS)
        if hit is _MISS:
            d = self.drawing()
            hit = self.block(d.dgg, d.store[pib - 1]) if 0 < pib <= len(d.store) else None
            self._stored[pib] = hit
        return hit

    def block(self, data, off) -> bytes | None:
        """The image of the FBSE or BLIP at `off` in `data`: its embedded BLIP, else its delay-loaded one."""
        at = officeart.fbse_delay(data, off)
        if at is None:
            return self.decode(officeart.fbse_blip, data, off, None)
        return self.delayed(at)

    def delayed(self, at) -> bytes | None:
        """The BLIP at WordDocument offset `at` (MS-DOC OfficeArtContent: the delay stream), read once an offset. Its
        record is charged to the delay read budget before it is read; one past the budget is None."""
        key = ("WordDocument", at)
        hit = self._delayed.get(key, _MISS)
        if hit is _MISS:
            word = self.r.word
            h = officeart.header(word, at)
            size = 8 + h[3] if h is not None else 0
            if not size or at + size > len(word) or self._delay_read + size > len(word):
                hit = None
            else:
                self._delay_read += size
                hit = self.decode(officeart.blip, word, at)
            self._delayed[key] = hit
        return hit

    def decode(self, read, *args) -> bytes | None:
        """read(*args, the document's flate budget), officeart's fbse_blip or blip. A BLIP that inflates past the
        ratio cap or the document's total, or is damaged (ReadError too_large or damaged), is None, so it costs that
        picture alone, as hwp's load_bin; another ReadError ends the document."""
        try:
            return read(*args, self.r.budget)
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            return None

    def picf(self, at):
        """The picture of the PICFAndOfficeArtData at Data offset `at`: the PICF (lcb u32, cbHeader u16, mfpf.mm u16
        at 6), for mm 0x66 (MM_SHAPEFILE) cchPicName u8 and the name, then the inline SpContainer and its FBSEs, the
        first FBSE giving the picture; with no FBSE, the shape's pib in the document's BStore (dochan), and with no pib
        (or pib 0) NOT_PICTURE. mm 8 is a raw WMF after the PICF. None without a Data stream, for a length that passes
        the stream or the PICF (cbHeader past lcb), a PICF past the read budget, another mm, or no SpContainer."""
        data = self.r.data_stream()
        if data is None or at + 8 > len(data):
            return None
        lcb, cb, mm = struct.unpack_from("<IHH", data, at)
        end = at + lcb
        if end > len(data) or not 8 <= cb <= lcb or self._picf_read + lcb > len(data):
            return None
        self._picf_read += lcb
        pos = at + cb
        if mm == _MM_WMF:
            return data[pos:end]
        if mm not in (_MM_SHAPE, _MM_SHAPEFILE):
            return None
        if mm == _MM_SHAPEFILE:
            if pos >= end:
                return None
            pos += 1 + data[pos]
        sp = None
        for _, _, typ, body, _ in officeart.records(data, pos, end):
            if typ == officeart.FBSE:
                return self.block(data, body - 8)
            if typ == officeart.SP_CONTAINER and sp is None:
                sp = body - 8
        if sp is None:
            return None
        pib = officeart.shape_pib(data, sp)
        return self.stored(pib) if pib else NOT_PICTURE

    def drawing(self) -> Drawing:
        """The drawings, read once, at the first picture that needs them: the anchors of PlcSpaMom (26-byte FSPAs,
        spid first; the first FSPA at a CP wins), then the DggInfo (MS-DOC OfficeArtContent): a DggContainer holding
        the BStore, then an OfficeArtWordDrawing each (dgglbl u8, a DgContainer), the main document's and the header
        document's, whose shapes go into one index; the content ends at anything else. Damage that loses shapes is
        counted once under shape: a PlcfSpa or DggInfo outside the table stream or not whole, a DggInfo that does not
        start with a whole DggContainer, a DgContainer cut short (its shapes before the cut are kept), records past
        officeart.MAX_RECORDS; containers nested past MAX_DEPTH once under depth."""
        d = self._drawing
        if d is not None:
            return d
        r, ctx = self.r, self.r.ctx
        d = self._drawing = Drawing()
        with ctx.guard("shape"):
            cps, fspas = r.story_plc(SPA_MOM, _FSPA)
            for cp, fspa in zip(cps, fspas):
                d.anchors.setdefault(cp, struct.unpack_from("<I", fspa)[0])
        with ctx.guard("shape"):
            d.damaged = True  # until the DggInfo is read through: the guard counts what an exception loses
            fc, lcb = r.fib.pair(_DGG_INFO)
            if not lcb:
                d.damaged = False
                return d
            if fc + lcb > len(r.table):
                raise ValueError("DggInfo outside the table stream")
            d.dgg = dgg = r.table[fc:fc + lcb]
            h = officeart.header(dgg, 0)
            if h is None or h[2] != officeart.DGG_CONTAINER or h[3] > lcb - 8:
                raise ValueError("no DggContainer")
            d.store = officeart.bstore(dgg)
            pos, cut = 8 + h[3], False
            while pos + 9 <= lcb:
                h = officeart.header(dgg, pos + 1)
                if h[2] != _DG_CONTAINER:
                    break
                body = pos + 9
                end = body + h[3]
                if end > lcb:
                    ctx.skipped["shape"] += 1
                    cut, end = True, lcb
                d.shapes.add(dgg, body, end)
                pos = end
            if d.shapes.deep:
                r.deep()
            if d.shapes.cut:
                ctx.skipped["shape"] += 1
            d.damaged = cut or d.shapes.cut or bool(d.shapes.deep)
        return d
