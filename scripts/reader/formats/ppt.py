"""PowerPoint 97-2003 presentations (.ppt): one slide section per slide, with outline and text-box text and notes.

The slide list (SlidePersistAtom, then the outline text atoms up to the next one), the notes mapping through NotesAtom
slideIdRef, the SlideShowSlideInfoAtom hidden flag and the text atoms (TextHeaderAtom, TextCharsAtom, TextBytesAtom,
StyleTextPropAtom) were checked against dochan `office_binary/ppt.py`, `ppt_structure.py` and `ppt_text.py` and anydoc
`formats/ppt/mod.rs`. Slides are found through the persist chain of `ppt_records`, never by scanning the stream; masters,
notes masters and handouts are not read. A slide's text comes in shape order: an OutlineTextRefAtom puts the outline
text block it names there, a ClientTextbox brings its own, and outline blocks nothing refers to follow. A slide that
cannot be read is counted under `slide` and the others still come out. Every walk spends one budget of
`BIN_RECORDS` records, and the text emitted is capped at `MAX_OUTPUT` characters.
"""
import struct

from .. import limits, officeart
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Doc, Section
from . import ppt_records as rec
from . import ppt_text

MAX_TITLE = 300
_TITLES = (0, 6)


class _Walk:
    """Shared budgets of one presentation: records walked, and text characters emitted."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.records = limits.BIN_RECORDS
        self.chars = limits.MAX_OUTPUT

    def spend(self) -> bool:
        self.records -= 1
        if self.records < 0:
            if self.records == -1:
                self.ctx.skipped["record"] += 1
            return False
        return True


def _decode(data, start, size, typ) -> str:
    raw = bytes(data[start:start + size])
    if typ == rec.TEXT_CHARS_ATOM:
        return raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace")
    return raw.decode("latin-1")


def _texts(data, start, end, walk):
    """The text in data[start:end] in document order, containers entered, as ["text", kind, text, style] and
    ["ref", index] items."""
    items = []
    stack = [(officeart.records(data, start, end), 4, None)]
    while stack:
        it, kind, last = stack.pop()
        for ver, inst, typ, body, size in it:
            if not walk.spend():
                return items
            if typ == rec.TEXT_HEADER_ATOM and size >= 4:
                kind = struct.unpack_from("<I", data, body)[0]
            elif typ in (rec.TEXT_CHARS_ATOM, rec.TEXT_BYTES_ATOM):
                last = ["text", kind, _decode(data, body, size, typ), None]
                items.append(last)
                kind = 4
            elif typ == rec.STYLE_TEXT_PROP_ATOM and last is not None and last[3] is None:
                last[3] = bytes(data[body:body + size])
            elif typ == rec.OUTLINE_TEXT_REF_ATOM and size >= 4:
                items.append(["ref", struct.unpack_from("<I", data, body)[0]])
            elif ver == 0xF:
                if len(stack) >= limits.MAX_DEPTH:
                    walk.ctx.skipped["record"] += 1
                    continue
                stack.append((it, kind, last))
                stack.append((officeart.records(data, body, body + size), 4, None))
                break
    return items


def _text_blocks(items, walk):
    """[(kind, blocks, plain text)] of the "text" items."""
    out = []
    for item in items:
        if item[0] != "text":
            continue
        walk.chars -= len(item[2])
        if walk.chars < 0:
            raise ValueError("text budget")
        blocks = ppt_text.blocks(item[2], item[3], item[1], walk.ctx)
        out.append((item[1], blocks, " ".join(item[2].replace("\x0b", " ").replace("\r", " ").split())))
    return out


class Presentation:
    def __init__(self, doc: bytes, current_user, ctx):
        self.doc, self.ctx = doc, ctx
        self.persist = rec.Persist(doc, current_user, ctx)
        if self.persist.encrypted:
            raise ReadError("encrypted")
        self.walk = _Walk(ctx)

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

    def _slide(self, pid, number, outline_span):
        got = self.persist.obj(pid)
        if got is None or got[0] != rec.SLIDE_CONTAINER:
            raise ValueError("slide record")
        start, size = got[2], got[3]
        outline = _text_blocks(_texts(self.doc, *outline_span, self.walk), self.walk)
        hidden, drawing = False, []
        for _, _, typ, body, sz in officeart.records(self.doc, start, start + size):
            if typ == rec.SLIDE_SHOW_SLIDE_INFO_ATOM and sz >= 12:
                hidden = bool(struct.unpack_from("<H", self.doc, body + 10)[0] & 0x0004)
            elif typ == rec.PP_DRAWING and not drawing:
                drawing = _texts(self.doc, body, body + sz, self.walk)
        ordered, used = [], set()
        for item in drawing:
            if item[0] == "ref":
                if item[1] < len(outline) and item[1] not in used:
                    used.add(item[1])
                    ordered.append(outline[item[1]])
            else:
                ordered.extend(_text_blocks([item], self.walk))
        ordered.extend(o for i, o in enumerate(outline) if i not in used)
        sec = Section("slide", number=number, hidden="hidden" if hidden else "")
        titled = False
        for kind, blocks, plain in ordered:
            if not titled and kind in _TITLES and plain:
                sec.title, titled = plain[:MAX_TITLE], True
            else:
                sec.blocks.extend(blocks)
        return sec

    def _notes(self, pid):
        """(slideIdRef, blocks) of the NotesContainer persist id `pid` names; None when it is not one."""
        span = self._container(pid, rec.NOTES_CONTAINER)
        if span is None:
            return None
        ref, items = None, []
        for _, _, typ, body, sz in officeart.records(self.doc, *span):
            if typ == rec.NOTES_ATOM and sz >= 4:
                ref = struct.unpack_from("<I", self.doc, body)[0]
            elif typ == rec.PP_DRAWING and not items:
                items = _texts(self.doc, body, body + sz, self.walk)
        blocks = []
        for kind, bl, _ in _text_blocks(items, self.walk):
            if kind == 2:
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
    except (KeyError, CfbError):
        raise ReadError("damaged", "PowerPoint Document stream") from None
    secs = Presentation(data, current, ctx).sections()
    if not secs:
        raise ReadError("damaged", "no readable slides")
    return Doc(blocks=secs)
