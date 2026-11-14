"""PowerPoint 97-2003 presentations (.ppt): one slide section per slide, with outline and text-box text and notes.

The slide list (SlidePersistAtom, then the outline text atoms up to the next one), the notes mapping through NotesAtom
slideIdRef, the SlideShowSlideInfoAtom hidden flag and the text atoms (TextHeaderAtom, TextCharsAtom, TextBytesAtom,
StyleTextPropAtom) were checked against dochan `office_binary/ppt.py`, `ppt_structure.py` and `ppt_text.py` and anydoc
`formats/ppt/mod.rs`. Slides are found through the persist chain of `ppt_records`, never by scanning the stream; masters,
notes masters and handouts are not read. A slide's text comes in shape order: an OutlineTextRefAtom puts the outline
text block it names there, a ClientTextbox brings its own, and outline blocks nothing refers to follow. The shapes
come in reading order from `ppt_shapes` (groups, tables and placeholders; date, number, footer and header placeholders
are left out) and a title placeholder gives the slide its title. A slide that
cannot be read is counted under `slide` and the others still come out. An object shape places the blocks `ppt_objects`
reads from its OLE storage (an Excel chart or sheet, an MS Graph chart, an equation); one it cannot read shows its
preview picture and is counted under `object`, a workbook with no table under `chart`. A picture shape becomes the Image of its pib
(`ppt_pictures`: the BStore of the DocumentContainer's PPDrawingGroup, the BLIPs in the `Pictures` stream) at its
place; one that cannot be placed is counted under `image`. Every walk spends one budget of
`BIN_RECORDS` records, and the text emitted is capped at `MAX_OUTPUT` characters. A file saved with the default
password is decrypted first (`ppt_crypt`); the Persist keeps its offsets and reads the decrypted copy.
"""
import struct

from .. import officeart
from ..containers import flate
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Section
from . import ppt_records as rec
from . import ppt_crypt, ppt_objects, ppt_pictures, ppt_shapes, sheet_grid
from .ppt_shapes import Walk as _Walk
from .ppt_shapes import text_blocks as _text_blocks
from .ppt_shapes import texts as _texts

MAX_TITLE = 300
_TITLES = (0, 6)
_TITLE_SHAPE = -1  # the kind of a run in a title placeholder whose text atom says otherwise
_PICTURE = -2      # the kind of a picture's run


class Presentation:
    def __init__(self, doc: bytes, current_user, ctx, pictures: bytes | None = None):
        self.doc, self.ctx = doc, ctx
        self.persist = rec.Persist(doc, current_user, ctx)
        if self.persist.encrypted:
            # the chain was read from the clear edit records; the same offsets then point into the decrypted copy
            unlocked = ppt_crypt.unlock(doc, self.persist, pictures)
            if unlocked is None:
                raise ReadError("encrypted")
            self.doc, pictures = unlocked
            self.persist.doc = self.doc
        self.walk = _Walk(ctx)
        self.pictures = ppt_pictures.Pictures(self.doc, self._dgg(), pictures, flate.Budget(), ctx)
        self.objects = ppt_objects.Objects(self.doc, self.persist, flate.Budget(), sheet_grid.Budget(), ctx)

    def _dgg(self):
        """(start, end) of the DggContainer inside the DocumentContainer's PPDrawingGroup; None without one."""
        span = self._container(self.persist.doc_ref, rec.DOCUMENT_CONTAINER)
        if span is None:
            return None
        for _, _, typ, body, size in officeart.records(self.doc, *span):
            if typ == rec.PP_DRAWING_GROUP:
                return body, body + size
        return None

    def _container(self, pid, typ):
        got = self.persist.obj(pid)
        if got is None or got[0] != typ:
            return None
        return got[2], got[2] + got[3]

    def _lists(self):
        """{instance: [[persist id, slide id, start, end of its outline records]]} of the document container's
        SlideListWithText records; the first of each instance wins."""
        span = self._container(self.persist.doc_ref, rec.DOCUMENT_CONTAINER)
        if span is None:
            raise ReadError("damaged", "no document container")
        out = {}
        for _, inst, typ, body, size in officeart.records(self.doc, *span):
            if typ != rec.SLIDE_LIST_WITH_TEXT or inst in out:
                continue
            entries, cur = [], None
            for _, _, t, b, s in officeart.records(self.doc, body, body + size):
                if not self.walk.spend():
                    break
                if t == rec.SLIDE_PERSIST_ATOM and s >= 16:
                    cur = [struct.unpack_from("<I", self.doc, b)[0], struct.unpack_from("<I", self.doc, b + 12)[0],
                           b + s, b + s]
                    entries.append(cur)
                elif cur is not None:
                    cur[3] = b + s
            out[inst] = entries
        return out

    def _drawn(self, span, outline, skip=ppt_shapes.SKIPPED):
        """([(kind, blocks, plain text)] of a PPDrawing's shapes in reading order, the outline indexes they took).
        A shape's OutlineTextRefAtom takes its outline block once; a skipped placeholder takes its block and drops it.
        A drawing the shape walk finds nothing in is read as plain text atoms in record order."""
        if span is None:
            return [], set()
        shapes = ppt_shapes.walk(self.doc, *span, self.ctx, self.walk)
        out, used = [], set()
        if not shapes:
            for item in _texts(self.doc, *span, self.walk):
                if item[0] == "ref":
                    if item[1] < len(outline) and item[1] not in used:
                        used.add(item[1])
                        out.append(outline[item[1]])
                else:
                    out.extend(_text_blocks([item], self.walk))
            return out, used
        for shape in ppt_shapes.flatten(ppt_shapes.ordered(shapes)):
            ref = shape.text_ref
            runs = list(shape.runs)
            if ref is not None and ref < len(outline) and ref not in used:
                used.add(ref)
                runs.insert(0, outline[ref])
            if shape.placeholder in skip:
                continue
            if shape.kind == "picture":
                image = self.pictures.image(shape.pib)
                if image is not None:
                    out.append((_PICTURE, [image], ""))
            elif shape.kind == "object":
                placed = self.objects.blocks(shape.ole_ref)
                if placed:
                    out.append((_PICTURE, placed, ""))
                unread = self.objects.unread_chart(shape.ole_ref)
                if placed is None:
                    self.ctx.skipped["object"] += 1
                elif unread:
                    self.ctx.skipped["chart"] += 1
                if (placed is None or unread or shape.ole_ref in self.objects.partial) and shape.pib:
                    image = self.pictures.image(shape.pib)
                    if image is not None:
                        out.append((_PICTURE, [image], ""))
            title = shape.placeholder in ppt_shapes.TITLES
            out.extend((_TITLE_SHAPE if title and kind not in _TITLES else kind, blocks, plain)
                       for kind, blocks, plain in runs)
        return out, used

    def _slide(self, pid, number, outline_span):
        got = self.persist.obj(pid)
        if got is None or got[0] != rec.SLIDE_CONTAINER:
            raise ValueError("slide record")
        start, size = got[2], got[3]
        outline = _text_blocks(_texts(self.doc, *outline_span, self.walk), self.walk)
        hidden, drawing = False, None
        for _, _, typ, body, sz in officeart.records(self.doc, start, start + size):
            if typ == rec.SLIDE_SHOW_SLIDE_INFO_ATOM and sz >= 12:
                hidden = bool(struct.unpack_from("<H", self.doc, body + 10)[0] & 0x0004)
            elif typ == rec.PP_DRAWING and drawing is None:
                drawing = (body, body + sz)
        ordered, used = self._drawn(drawing, outline)
        ordered.extend(o for i, o in enumerate(outline) if i not in used)
        sec = Section("slide", number=number, hidden="hidden" if hidden else "")
        titled = False
        for kind, blocks, plain in ordered:
            if not titled and (kind in _TITLES or kind == _TITLE_SHAPE) and plain:
                sec.title, titled = plain[:MAX_TITLE], True
            else:
                sec.blocks.extend(blocks)
        return sec

    def _notes(self, pid):
        """(slideIdRef, blocks) of the NotesContainer persist id `pid` names; None when it is not one."""
        span = self._container(pid, rec.NOTES_CONTAINER)
        if span is None:
            return None
        ref, drawing = None, None
        for _, _, typ, body, sz in officeart.records(self.doc, *span):
            if typ == rec.NOTES_ATOM and sz >= 4:
                ref = struct.unpack_from("<I", self.doc, body)[0]
            elif typ == rec.PP_DRAWING and drawing is None:
                drawing = (body, body + sz)
        blocks = []
        for kind, bl, _ in self._drawn(drawing, [], ppt_shapes.SKIPPED | {5})[0]:
            if kind in (2, _PICTURE):
                blocks.extend(bl)
        return ref, blocks

    def sections(self) -> list:
        lists = self._lists()
        secs, by_id = [], {}
        for number, (pid, sid, a, b) in enumerate(lists.get(0, []), 1):
            with self.ctx.guard("slide"):
                sec = self._slide(pid, number, (a, b))
                secs.append(sec)
                by_id.setdefault(sid, sec)
        for pid, _, _, _ in lists.get(2, []):
            with self.ctx.guard("notes"):
                got = self._notes(pid)
                if got is None:
                    raise ValueError("notes record")
                sec = by_id.get(got[0])
                if sec is not None and not sec.notes:
                    sec.notes = got[1]
        return secs


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:2] == b"PK":
        from . import ooxml
        return ooxml.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a PowerPoint file")
    try:
        with Compound(path) as cf:
            if not cf.exists("PowerPoint Document"):
                raise ReadError("damaged", "no PowerPoint Document stream")
            data = cf.read("PowerPoint Document")
            current = cf.read("Current User") if cf.exists("Current User") else None
            pictures = cf.read("Pictures") if cf.exists("Pictures") else None
    except (KeyError, CfbError):
        raise ReadError("damaged", "PowerPoint Document stream") from None
    secs = Presentation(data, current, ctx, pictures).sections()
    if not secs:
        raise ReadError("damaged", "no readable slides")
    return Doc(blocks=secs)
