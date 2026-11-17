"""PowerPoint 97-2003 presentations (.ppt): one slide section per slide, with outline and text-box text and notes.

The slide list (SlidePersistAtom, then the outline text atoms up to the next one), the notes mapping through NotesAtom
slideIdRef, the SlideShowSlideInfoAtom hidden flag and the text atoms (TextHeaderAtom, TextCharsAtom, TextBytesAtom,
StyleTextPropAtom) were checked against dochan `office_binary/ppt.py`, `ppt_structure.py` and `ppt_text.py` and anydoc
`formats/ppt/mod.rs`. Slides are found through the persist chain of `ppt_records`; only a clear stream whose chain is
unusable (no persist directory) is read by walking its top-level records, each SlideContainer in stream order, with
its notes counted, not placed. Masters, notes masters and handouts are not read. A slide's text comes in shape
order: an OutlineTextRefAtom puts the outline text block it names there, a ClientTextbox brings its own, and outline
blocks nothing refers to follow. The shapes come in reading order from `ppt_shapes` (groups, tables and placeholders;
date, number, footer and header placeholders are left out) and a title placeholder gives the slide its title. A slide
that cannot be read is counted under `slide` and the others still come out. An object shape places the blocks
`ppt_objects` reads from its OLE storage (an Excel chart or sheet, an MS Graph chart, an equation); one it cannot read
shows its preview picture and is counted under `object`, a workbook with no table under `chart`. A picture shape
becomes the Image of its pib (`ppt_pictures`: the BStore of the DocumentContainer's PPDrawingGroup, the BLIPs in the
`Pictures` stream) at its place; one that cannot be placed is counted under `image`. Each slide is one diagram scope:
every shape (the title and left-out placeholders too) goes to `diagram.Diagram` under its spid, in master units after
the group maps, and each connector (types 32 to 40) or line (20) with the ends its drawing's connector rule names, its
arrowheads and its text as the label (`officeart`); the deck shares one ConnectionBudget. Every walk spends one budget
of `BIN_RECORDS` records, and the text emitted is capped at `MAX_OUTPUT` characters. A file saved with the default
password is decrypted first (`ppt_crypt`); the Persist keeps its offsets and reads the decrypted copy. A compound
file with no PowerPoint Document stream goes to `cfb_route` (another format's file named .ppt is read by its own
parser), and a PowerPoint 95 file (a PP40 stream, or PowerPoint 95's short Current User) is `unsupported`.
"""
import struct

from .. import diagram, officeart
from ..containers import flate
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Section
from . import ppt_records as rec
from . import cfb_route, ppt_crypt, ppt_objects, ppt_pictures, ppt_shapes, sheet_grid, zip_route

MAX_TITLE = 300
_TITLES = (0, 6)
_TITLE_SHAPE = -1  # the kind of a run in a title placeholder whose text atom says otherwise
_PICTURE = -2      # the kind of a picture's run


class Presentation:
    def __init__(self, doc: bytes, current_user, ctx, pictures: bytes | None = None):
        self.doc, self.ctx = doc, ctx
        self.broken = None  # the chain's error when a clear stream has no usable persist chain
        try:
            self.persist = rec.Persist(doc, current_user, ctx)
        except ReadError as e:
            if e.kind != "damaged" or rec.encrypted(current_user):  # an encrypted stream needs its chain
                raise
            self.persist, self.broken = rec.Unchained(doc), e
        if self.persist.encrypted:
            # the chain was read from the clear edit records; the same offsets then point into the decrypted copy
            unlocked = ppt_crypt.unlock(doc, self.persist, pictures)
            if unlocked is None:
                raise ReadError("encrypted")
            self.doc, pictures = unlocked
            self.persist.doc = self.doc
        self.walk = ppt_shapes.Walk(ctx)
        self.budget = diagram.ConnectionBudget(ctx)  # the deck's edges, shared by every slide's diagram
        self.pictures = ppt_pictures.Pictures(self.doc, self._dgg(), pictures, flate.Budget(), ctx)
        self.objects = ppt_objects.Objects(self.doc, self.persist, flate.Budget(), sheet_grid.Budget(), ctx)

    def _dgg(self):
        """(start, end) of the DggContainer inside the DocumentContainer's PPDrawingGroup; None without one. With no
        chain the DocumentContainer is the first one among the top-level records."""
        if self.broken is None:
            span = self._container(self.persist.doc_ref, rec.DOCUMENT_CONTAINER)
        else:
            span = next(((body, body + size) for _, _, typ, body, size in officeart.records(self.doc)
                         if typ == rec.DOCUMENT_CONTAINER), None)
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

    def _drawn(self, span, outline, skip=ppt_shapes.SKIPPED, dia=None):
        """([(kind, blocks, plain text, key)] of a PPDrawing's shapes in reading order, the outline indexes they took).
        A shape's OutlineTextRefAtom takes its outline block once; a skipped placeholder takes its block and drops it.
        `key` is the shape's spid, or a key no rule names when it has none (0) or an earlier shape has it. With `dia`
        every shape goes to that diagram under its key, a skipped placeholder with its text and no item. A drawing the
        shape walk finds nothing in is read as plain text atoms in record order, keyed None."""
        if span is None:
            return [], set()
        shapes, rules = ppt_shapes.walk(self.doc, *span, self.ctx, self.walk)
        out, used = [], set()
        if not shapes:
            for item in ppt_shapes.texts(self.doc, *span, self.walk):
                if item[0] == "ref":
                    if item[1] < len(outline) and item[1] not in used:
                        used.add(item[1])
                        out.append((*outline[item[1]], None))
                else:
                    out.extend((*run, None) for run in ppt_shapes.text_blocks([item], self.walk))
            return out, used
        glue, keys = {}, set()
        for a, b, c in rules:  # spidC -> (spidA, spidB); the first rule naming a connector wins
            glue.setdefault(c, (a, b))
        for shape in ppt_shapes.flatten(ppt_shapes.ordered(shapes)):
            key = shape.spid if shape.spid and shape.spid not in keys else ("unkeyed", len(keys))
            keys.add(key)
            ref = shape.text_ref
            runs = list(shape.runs)
            # an index outside the list, or one an earlier shape took, is ignored and not counted: no text is lost,
            # the block it did not take follows the slide's shapes as unreferenced (see _slide_at)
            if ref is not None and ref < len(outline) and ref not in used:
                used.add(ref)
                runs.insert(0, outline[ref])
            if shape.placeholder in skip:  # left out of the flow; a connector may still end on it
                if dia is not None:
                    with self.ctx.guard("shape"):
                        self._register(dia, shape, key, runs, glue)
                continue
            mine = []
            if shape.kind == "picture":
                image = self.pictures.image(shape.pib)
                if image is not None:
                    mine.append((_PICTURE, [image], ""))
            elif shape.kind == "object":
                placed, unread = self.objects.blocks(shape.ole_ref)
                if placed:
                    mine.append((_PICTURE, placed, ""))
                if placed is None:
                    self.ctx.skipped["object"] += 1
                elif unread:
                    self.ctx.skipped["chart"] += 1
                if (placed is None or unread or shape.ole_ref in self.objects.partial) and shape.pib:
                    image = self.pictures.image(shape.pib)
                    if image is not None:
                        mine.append((_PICTURE, [image], ""))
            title = shape.placeholder in ppt_shapes.TITLES
            mine.extend((_TITLE_SHAPE if title and kind not in _TITLES else kind, blocks, plain)
                        for kind, blocks, plain in runs)
            if dia is not None:
                with self.ctx.guard("shape"):
                    self._register(dia, shape, key, mine, glue)
            out.extend((kind, blocks, plain, key) for kind, blocks, plain in mine)
        return out, used

    @staticmethod
    def _register(dia, shape, key, runs, glue):
        """One shape on the slide's diagram, with the blocks of its `runs`: a connector (types 32 to 40) or line (20)
        from its start corner to the opposite one, glued to the ends of the rule that names its spid, labelled with its
        text; any other shape with its type's kind and its box."""
        if shape.stype == officeart.LINE_TYPE or shape.stype in officeart.CONNECTOR_TYPES:
            begin, end = glue.get(key, (None, None))
            p0, p1 = shape.ends or (None, None)
            label = " ".join(plain for _, _, plain in runs if plain)
            dia.connector(key, begin, end, p0, p1, shape.arrows, label, line=shape.stype == officeart.LINE_TYPE)
        else:
            box = (shape.left, shape.top, shape.right, shape.bottom) if shape.anchored else None
            dia.shape(key, [b for _, blocks, _ in runs for b in blocks], officeart.KINDS.get(shape.stype, ""), box)

    def _slide(self, pid, number, outline_span):
        got = self.persist.obj(pid)
        if got is None or got[0] != rec.SLIDE_CONTAINER:
            raise ValueError("slide record")
        return self._slide_at(got[2], got[3], number, outline_span)

    def _slide_at(self, start, size, number, outline_span):
        """The slide section of the SlideContainer whose body is `size` bytes at `start`."""
        outline = ppt_shapes.text_blocks(ppt_shapes.texts(self.doc, *outline_span, self.walk), self.walk)
        hidden, drawing = False, None
        for _, _, typ, body, sz in officeart.records(self.doc, start, start + size):
            if typ == rec.SLIDE_SHOW_SLIDE_INFO_ATOM and sz >= 12:
                hidden = bool(struct.unpack_from("<H", self.doc, body + 10)[0] & 0x0004)
            elif typ == rec.PP_DRAWING and drawing is None:
                drawing = (body, body + sz)
        bare = drawing is None
        if bare:  # no PPDrawing: the text atoms the slide holds itself, in record order
            drawing = (start, start + size)
        dia = diagram.Diagram(self.ctx, self.budget)
        ordered, used = self._drawn(drawing, outline, dia=dia)
        # a slide list run the slide's own atoms already gave is the same text: read it once
        seen = {plain for _, _, plain, _ in ordered} if bare else set()
        ordered.extend((*o, None) for i, o in enumerate(outline) if i not in used and o[2] not in seen)
        sec = Section("slide", number=number, hidden="hidden" if hidden else "")
        titled, slots = False, []
        for kind, blocks, plain, key in ordered:
            if not titled and (kind in _TITLES or kind == _TITLE_SHAPE) and plain:
                sec.title, titled = plain[:MAX_TITLE], True  # the slide's heading: no slot, a node keeps its text
            else:
                slots.append((key, blocks))
        flow = [b for _, blocks in slots for b in blocks]
        with self.ctx.guard("shape"):
            flow = dia.place(slots)
        sec.blocks.extend(flow)
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
        if drawing is None:  # no PPDrawing: the text atoms the notes page holds itself
            drawing = span
        for kind, bl, _, _ in self._drawn(drawing, [], ppt_shapes.SKIPPED | {5})[0]:
            if kind in (2, _PICTURE):
                blocks.extend(bl)
        return ref, blocks

    def _unchained(self) -> list:
        """The slides of a clear stream with no usable persist chain: each SlideContainer among the top-level records,
        in stream order, read by the per-slide code with no outline text (the slide list ties it to a slide through
        the chain). Each NotesContainer is counted under note and the broken chain once under record; the chain's own
        error is raised when no slide reads."""
        secs, number, notes = [], 0, 0
        for _, _, typ, body, size in officeart.records(self.doc):
            if not self.walk.spend():
                break
            if typ == rec.SLIDE_CONTAINER:
                number += 1
                with self.ctx.guard("slide"):
                    secs.append(self._slide_at(body, size, number, (body, body)))
            elif typ == rec.NOTES_CONTAINER:
                notes += 1
        if not secs:
            raise self.broken
        self.ctx.skipped["record"] += 1
        if notes:
            self.ctx.skipped["note"] += notes
        return secs

    def sections(self) -> list:
        if self.broken is not None:
            return self._unchained()
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


def _pp95(cf, current) -> bool:
    """A PowerPoint 95 file: its Current User stream is under 28 bytes, and the file holds a PP40 stream or that stream
    has PowerPoint 95's shape, a u32 size and then that many bytes. A PP40 stream beside a PowerPoint Document and a full
    Current User (28 bytes or more) is the 97/95 dual format, which PowerPoint 97 and later read as a 97 file: it is
    read as ppt here too, and only the two shapes above are unsupported."""
    if current is None or len(current) >= 28:  # a full Current User: PP40 or not, a PowerPoint 97 file
        return False
    if cf.exists("PP40"):
        return True
    return len(current) >= 4 and struct.unpack_from("<I", current)[0] + 4 == len(current)


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:2] == b"PK":  # an OpenDocument package to odf, any other zip to ooxml; neither comes back here
        return zip_route.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a PowerPoint file")
    got = parse_compound(path, ctx)
    if isinstance(got, str):  # another format's compound file named .ppt
        return cfb_route.parse(got, path, ctx)
    return got


def parse_compound(src, ctx) -> Doc | str:
    """Read the compound file `src` (a path or its bytes, nothing is written): the Doc, or the name of the other
    parser `cfb_route` picks for a compound file that holds no PowerPoint Document stream. `parse` ends in it."""
    try:
        with Compound(src) as cf:
            other = cfb_route.route(cf, "ppt", "no PowerPoint Document stream")
            if other is None:
                current = cf.read("Current User") if cf.exists("Current User") else None
                if _pp95(cf, current):
                    raise ReadError("unsupported", cfb_route.PP95)
                data = cf.read("PowerPoint Document")
                pictures = cf.read("Pictures") if cf.exists("Pictures") else None
    except (KeyError, CfbError):
        raise ReadError("damaged", "PowerPoint Document stream") from None
    if other is not None:
        return other
    secs = Presentation(data, current, ctx, pictures).sections()
    if not secs:
        raise ReadError("damaged", "no readable slides")
    return Doc(blocks=secs)
