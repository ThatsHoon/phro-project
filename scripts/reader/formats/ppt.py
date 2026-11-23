"""PowerPoint 97-2003 presentations (.ppt): one slide section per slide, with outline and text-box text and notes.

The slide list (SlidePersistAtom, then the outline text atoms up to the next one), the notes mapping through NotesAtom
slideIdRef, the SlideShowSlideInfoAtom hidden flag and the text atoms (TextHeaderAtom, TextCharsAtom, TextBytesAtom,
StyleTextPropAtom) were checked against dochan `office_binary/ppt.py`, `ppt_structure.py` and `ppt_text.py` and anydoc
`formats/ppt/mod.rs`. Slides are found through the persist chain of `ppt_records`; only a clear stream whose chain is
unusable (no persist directory) is read by walking its top-level records, each SlideContainer in stream order, with
its notes counted, not placed. A slide's text comes in shape order: an OutlineTextRefAtom puts the outline text block
it names there, a ClientTextbox brings its own, and outline blocks nothing refers to follow. The shapes come in reading
order from `ppt_shapes` (groups, tables and placeholders; date and number placeholders are left out) and a title
placeholder gives the slide its title. A slide that cannot be read is counted under `slide` and the others still come
out. A notes page gives every shape's text and pictures but its slide image and its date, number, footer and header.

The master a SlideAtom's masterIdRef names (a title master's own master behind it) gives the slide's text the bullets
of its TextMasterStyleAtoms by text type and indent level, and, when the SlideAtom's fMasterObjects is set, the
shapes on it that are not placeholders. Those, the text of the slide's footer and header placeholders and the footer
of its HeadersFooters (the slide's own, else the document's slide one, when fHasFooter is set; not on a title slide
when the DocumentAtom's fOmitTitlePlace is set) go to `deck.Repeated`:
written once, on the slide when only one shows it. A click (InteractiveInfoAtom) on a shape or a text range is a link
to its ExHyperlink's URL or the words of its jump ("Slide 2", "next slide", "macro"); a shape's click on text wraps the
text, else a line of its label follows: an action button's ("Next button"), a picture's or object's "Picture", or
"Link". A shape whose ExObjRefAtom names a movie or sound in the ExObjList is the line "Video: file name" (its poster
picture after it). An object shape places the blocks
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
import re
import struct

from .. import diagram, embedded, officeart
from ..containers import flate
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Para, Section, Text
from . import ppt_records as rec
from . import cfb_route, deck, ppt_crypt, ppt_objects, ppt_pictures, ppt_shapes, ppt_text, zip_route
from .word_fields import MAX_URL

MAX_TITLE = 300
MAX_NAME = 200
_TITLES = (0, 6)
_TITLE_SHAPE = -1  # the kind of a run in a title placeholder whose text atom says otherwise
_PICTURE = -2      # the kind of a picture's run
_FOOTERS = frozenset((9, 10))
_PLACEHOLDERS = frozenset(range(1, 256))
_NOTES_SKIPPED = ppt_shapes.SKIPPED | {5, 11}
_HAS_FOOTER = 0x20
_JUMPS = {1: "next slide", 2: "previous slide", 3: "first slide", 4: "last slide", 5: "last slide viewed",
          6: "end of show"}
_LINK_JUMPS = {0: "next slide", 1: "previous slide", 2: "first slide", 3: "last slide"}
_BUTTONS = {189: "", 190: "Home", 191: "Help", 192: "Information", 193: "Next", 194: "Previous", 195: "Last",
            196: "First", 197: "Return", 198: "Document", 199: "Sound", 200: "Movie"}


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
        self.walk.link = self._link
        self.budget = diagram.ConnectionBudget(ctx)  # the deck's edges, shared by every slide's diagram
        self.pictures = ppt_pictures.Pictures(self.doc, self._dgg(), pictures, flate.Budget(), ctx)
        self.objects = ppt_objects.Objects(self.doc, self.persist, flate.Budget(), embedded.of(ctx).budget(), ctx)
        self.links, self.media, self.hf = {}, {}, (0, "")
        self.omit_title = False
        self.numbers, self.masters, self.master_of = {}, {}, {}
        self.repeated = deck.Repeated()
        self._index()

    def _document(self):
        """(start, end) of the DocumentContainer's body; None without one. With no chain it is the first one among
        the top-level records."""
        if self.broken is None:
            return self._container(self.persist.doc_ref, rec.DOCUMENT_CONTAINER)
        return next(((body, body + size) for _, _, typ, body, size in officeart.records(self.doc)
                     if typ == rec.DOCUMENT_CONTAINER), None)

    def _children(self, body, size):
        """The records of a container body, each one spending the walk."""
        for item in officeart.records(self.doc, body, body + size):
            if not self.walk.spend():
                return
            yield item

    def _cstring(self, body, size) -> str:
        return " ".join(bytes(self.doc[body:body + size]).decode("utf-16-le", "replace").replace("\x00", " ").split())

    def _hf(self, body, size):
        """(flags, footer text) of a HeadersFooters container."""
        flags, footer = 0, ""
        for _, inst, typ, b, sz in self._children(body, size):
            if typ == rec.HEADERS_FOOTERS_ATOM and sz >= 4:
                flags = struct.unpack_from("<H", self.doc, b + 2)[0]
            elif typ == rec.CSTRING and inst == 2:
                footer = self._cstring(b, sz)[:MAX_NAME]
        return flags, footer

    def _media(self, body, size):
        """(exObjId, file name) of a movie's ExVideoContainer or a sound container; None without an ExMediaAtom."""
        ref, name = None, ""
        for _, _, typ, b, sz in self._children(body, size):
            if typ == rec.EX_MEDIA_ATOM and sz >= 4:
                ref = struct.unpack_from("<I", self.doc, b)[0]
            elif typ == rec.CSTRING and not name:
                name = re.split(r"[\\/]", self._cstring(b, sz))[-1][:MAX_NAME]
        return None if ref is None else (ref, name)

    def _index(self):
        """The DocumentContainer's hyperlinks ({exHyperId: (target, location)}), movies and sounds ({exObjId: (kind,
        file name)}) of its ExObjList, and its slides' HeadersFooters."""
        span = self._document()
        if span is None:
            return
        for _, inst, typ, body, size in self._children(span[0], span[1] - span[0]):
            if typ == rec.HEADERS_FOOTERS and inst == 3:
                self.hf = self._hf(body, size)
            elif typ == rec.DOCUMENT_ATOM and size >= 38:
                self.omit_title = bool(self.doc[body + 37])
            if typ != rec.EX_OBJ_LIST:
                continue
            for _, _, t, b, sz in self._children(body, size):
                if t == rec.EX_HYPERLINK:
                    ref, strings = None, {}
                    for _, i, tt, bb, ss in self._children(b, sz):
                        if tt == rec.EX_HYPERLINK_ATOM and ss >= 4:
                            ref = struct.unpack_from("<I", self.doc, bb)[0]
                        elif tt == rec.CSTRING:
                            strings.setdefault(i, self._cstring(bb, ss))
                    if ref is not None:
                        self.links.setdefault(ref, (strings.get(1, ""), strings.get(3, "")))
                elif t in rec.EX_MOVIES:
                    video = next(((bb, ss) for _, _, tt, bb, ss in self._children(b, sz) if tt == rec.EX_VIDEO), None)
                    got = self._media(*video) if video else None
                    if got is not None:
                        self.media.setdefault(got[0], ("Video", got[1]))
                elif t in rec.EX_AUDIO:
                    got = self._media(b, sz)
                    if got is not None:
                        self.media.setdefault(got[0], ("Audio", got[1]))

    def _link(self, click):
        """(URL or "", jump words or "") of a click (exHyperlinkIdRef, action, jump, hyperlinkType): a jump action its
        move, a macro "macro", a hyperlink its URL, file or slide ("Slide 2"; nothing when the slide is not in the
        deck) or its move. A hyperlink id the ExObjList does not hold is counted under `link`."""
        ref, action, jump, kind = click
        if action == 3:
            return "", _JUMPS.get(jump, "")
        if action == 1:
            return "", "macro"
        if action != 4:
            return "", ""
        if kind in _LINK_JUMPS:
            return "", _LINK_JUMPS[kind]
        got = self.links.get(ref)
        if got is None:
            self.ctx.skipped["link"] += 1
            return "", ""
        target, location = got
        if kind == 7:
            sid = location.partition(",")[0].strip()
            number = self.numbers.get(int(sid)) if sid.isdigit() else None
            return "", f"Slide {number}" if number else ""
        return (target[:MAX_URL], "") if target else ("", "")

    def _master(self, sid):
        """(bullets by text type, [blocks of each shape that is not a placeholder]) of the master slide id `sid`
        names; a title master (a SlideContainer) adds its own master's styles under its own and that master's shapes
        before its own when its SlideAtom's fMasterObjects is set. Read once per master."""
        if sid in self.master_of:
            return self.master_of[sid]
        self.master_of[sid] = ({}, [])
        pid = self.masters.get(sid)
        got = self.persist.obj(pid) if pid is not None else None
        styles, items = {}, []
        if got is not None and got[0] in (rec.MAIN_MASTER, rec.SLIDE_CONTAINER):
            parent, drawing = None, None
            for _, inst, typ, body, size in self._children(got[2], got[3]):
                if typ == rec.TEXT_MASTER_STYLE_ATOM:
                    styles.setdefault(inst, ppt_text.master_levels(bytes(self.doc[body:body + size]), inst))
                elif typ == rec.SLIDE_ATOM and size >= 22 and got[0] == rec.SLIDE_CONTAINER:
                    parent = struct.unpack_from("<I", self.doc, body + 12)[0], self.doc[body + 20] & 1
                elif typ == rec.PP_DRAWING and drawing is None:
                    drawing = (body, body + size)
            if parent is not None:
                base, shapes = self._master(parent[0])
                styles = {**base, **styles}
                if parent[1]:
                    items.extend(shapes)
            if drawing is not None:
                saved, self.walk.styles = self.walk.styles, styles
                try:
                    with self.ctx.guard("master"):
                        by_key = {}
                        for _, blocks, _, key in self._drawn(drawing, [], _PLACEHOLDERS)[0]:
                            by_key.setdefault(key, []).extend(blocks)
                        items.extend(b for b in by_key.values() if b)
                finally:
                    self.walk.styles = saved
        self.master_of[sid] = (styles, items)
        return styles, items

    def _dgg(self):
        """(start, end) of the DggContainer inside the DocumentContainer's PPDrawingGroup; None without one."""
        span = self._document()
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

    def _drawn(self, span, outline, skip=ppt_shapes.SKIPPED, dia=None, furniture=None):
        """([(kind, blocks, plain text, key)] of a PPDrawing's shapes in reading order, the outline indexes they took).
        A shape's OutlineTextRefAtom takes its outline block once; a skipped placeholder takes its block and drops it,
        a footer or header one into `furniture` when that is a list. `key` is the shape's spid, or a key no rule names
        when it has none (0) or an earlier shape has it. With `dia` every shape goes to that diagram under its key, a
        skipped placeholder with its text and no item. A drawing the shape walk finds nothing in is read as plain text
        atoms in record order, keyed None."""
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
        for shape in ppt_shapes.flatten(ppt_shapes.ordered(shapes, self.walk.order)):
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
                if furniture is not None and shape.placeholder in _FOOTERS:
                    furniture.append([b for _, blocks, _ in runs for b in blocks])
                continue
            mine = []
            if shape.kind == "picture":
                image = self.pictures.image(shape.pib)
                if image is not None:
                    mine.append((_PICTURE, [image], ""))
            elif shape.kind == "object" and shape.ole_ref in self.media:
                kind, name = self.media[shape.ole_ref]
                image = self.pictures.image(shape.pib) if shape.pib else None
                line = f"{kind}: {name}" if name else kind
                mine.append((_PICTURE, [Para([Text(line + (" (poster below)" if image is not None else ""))])], ""))
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
            if shape.click is not None:
                url, jump = self._link(shape.click)
                if url or jump:
                    flat = [b for _, blocks, _ in mine for b in blocks]
                    word = _BUTTONS.get(shape.stype)
                    label = (f"{word} button" if word else "Button" if word is not None
                             else "Picture" if shape.kind in ("picture", "object") else "Link")
                    extra = deck.linked(flat, url, jump, label)[len(flat):]
                    if extra:
                        mine.append((4, extra, ""))
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
            dia.connector(key, begin, end, p0, p1, shape.arrows, line=shape.stype == officeart.LINE_TYPE,
                          blocks=[b for _, blocks, _ in runs for b in blocks])
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
        hidden, drawing, atom, hf = False, None, None, None
        for _, _, typ, body, sz in officeart.records(self.doc, start, start + size):
            if typ == rec.SLIDE_SHOW_SLIDE_INFO_ATOM and sz >= 12:
                hidden = bool(struct.unpack_from("<H", self.doc, body + 10)[0] & 0x0004)
            elif typ == rec.PP_DRAWING and drawing is None:
                drawing = (body, body + sz)
            elif typ == rec.SLIDE_ATOM and sz >= 22 and atom is None:
                atom = (struct.unpack_from("<I", self.doc, body + 12)[0], self.doc[body + 20] & 1,
                        struct.unpack_from("<I", self.doc, body)[0] == 0)
            elif typ == rec.HEADERS_FOOTERS and hf is None:
                hf = self._hf(body, sz)
        styles, items = self._master(atom[0]) if atom is not None else ({}, [])
        bare = drawing is None
        if bare:  # no PPDrawing: the text atoms the slide holds itself, in record order
            drawing = (start, start + size)
        dia = diagram.Diagram(self.ctx, self.budget)
        furniture = []
        self.walk.styles = styles
        try:
            outline = ppt_shapes.text_blocks(ppt_shapes.texts(self.doc, *outline_span, self.walk), self.walk)
            ordered, used = self._drawn(drawing, outline, dia=dia, furniture=furniture)
        finally:
            self.walk.styles = {}
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
        if atom is not None and atom[1]:
            for blocks in items:
                self.repeated.add(number, blocks)
        flags, footer = hf or self.hf
        if flags & _HAS_FOOTER and footer and not (self.omit_title and atom is not None and atom[2]):
            self.repeated.add(number, [Para([Text(footer)])])
        for blocks in furniture:
            self.repeated.add(number, blocks)
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
        for _, bl, _, _ in self._drawn(drawing, [], _NOTES_SKIPPED)[0]:
            blocks.extend(bl)
        return ref, blocks

    def _unchained(self) -> list:
        """The slides of a clear stream with no usable persist chain: each SlideContainer among the top-level records,
        in stream order, read by the per-slide code with no outline text (the slide list ties it to a slide through
        the chain). Each NotesContainer is counted under note and the broken chain once under record; the chain's own
        error is raised when no slide reads."""
        secs, number, notes = [], 0, 0
        for _, _, typ, body, size in officeart.records(self.doc):
            if not self.walk.spend() or self.ctx.late():
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
        return self.repeated.place(secs) + secs

    def sections(self) -> list:
        if self.broken is not None:
            return self._unchained()
        lists = self._lists()
        secs, by_id = [], {}
        self.numbers = {sid: number for number, (_, sid, _, _) in enumerate(lists.get(0, []), 1)}
        self.masters = {sid: pid for pid, sid, _, _ in lists.get(1, [])}
        for number, (pid, sid, a, b) in enumerate(lists.get(0, []), 1):
            if self.ctx.late():
                break
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
        return self.repeated.place(secs) + secs


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
            return read_compound(cf, ctx)
    except CfbError:
        raise ReadError("damaged", "PowerPoint Document stream") from None


def read_compound(cf, ctx) -> Doc | str:
    """What `parse_compound` gives, for the open compound file `cf` (any object with exists, size and read by stream
    name)."""
    try:
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
