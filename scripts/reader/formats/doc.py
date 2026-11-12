"""Word 97-2003 binary (.doc) to the document model.

The text is one CP stream cut into stories (doc_fib.STORIES). A story is walked once: plain runs are text, and every
character below 0x20 other than a tab goes to its handler in _Reader.SPECIAL, paragraph marks included. Logic follows
dochan office_binary/doc_binary.py (stream checks, the table stream, story ranges) and doc_stories.py (field begin,
separator and end), and anydoc formats/doc/mod.rs (the language is lidFE in a Far East document, else lid). The FIB,
the piece table and the story text are doc_fib.py's; the HYPERLINK rule is word_fields.py's, shared with the docx
reader.

The walk keeps one str unit a CP: a surrogate pair is two units, so a handler's CP is the Story's cp_of its index,
and the pair is joined only when a Text is built.
"""
import re

from .. import limits
from ..containers.cfb import CfbError, Compound, is_cfb
from ..limits import ReadError
from ..model import Break, Doc, Link, Para, Text
from . import doc_fib, ooxml
from .word_fields import MAX_INSTR, hyperlink_url

_ZIP = b"PK\x03\x04"
_RTF = b"{\\rtf"
_BOM = b"\xef\xbb\xbf"
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f]")  # the characters the walk hands to a handler; a tab is text
_SURROGATE = re.compile("[\ud800-\udfff]")


def _joined(s: str) -> str:
    """UTF-16 units as characters: a pair becomes one, an unpaired surrogate U+FFFD."""
    if _SURROGATE.search(s):
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return s


class _Frame:
    """One open inline scope inside a paragraph: root or link."""

    def __init__(self, kind, url=""):
        self.kind, self.url, self.inl = kind, url, []


class _Field:
    """An open field: its instruction so far, whether its result has begun, the index of the link frame it opened."""

    def __init__(self):
        self.instr, self.size, self.result, self.frame = [], 0, False, None


class _State:
    """A story being walked: its name, its blocks so far, and the open paragraph's inline scopes and fields. Fields
    live with the paragraph: one still open at a paragraph mark ends there, so a broken field costs one paragraph."""

    def __init__(self, name, story=None):
        self.name = name
        self.story = story  # the doc_fib.Story walked: CP <-> text index
        self.out = []
        self.reset()

    def reset(self):
        self.frames = [_Frame("root")]
        self.fields = []
        self.codes = 0  # open fields still in their instruction: text goes to the innermost one, never out
        self.over = 0   # fields opened past MAX_DEPTH: everything up to their ends is skipped


class _Reader:
    # a control character -> its handler (st, cp), cp being the character's CP; one not listed is dropped. Tasks
    # 10-13 give \x01 (picture), \x02 (note reference), \x05 (comment reference) and \x08 (drawn object) handlers, and
    # Task 11 gives \x07 (cell mark) its own; until then a cell is a paragraph.
    SPECIAL: dict[str, str] = {
        "\r": "para_end", "\x07": "para_end", "\x0c": "para_end",
        "\x0a": "line_break", "\x0b": "line_break",
        "\x13": "field_begin", "\x14": "field_separator", "\x15": "field_end",
        "\x1e": "hyphen",
    }

    def __init__(self, cf, ctx):
        self.cf, self.ctx = cf, ctx
        self.doc = Doc()
        self.word = b""
        self.table = b""
        self.fib = None
        self.pieces = []
        self.lid = 0x0409  # the language of compressed text (doc_fib.compressed_codepage)
        self.ranges = {}   # story name -> (cp0, cp1), clamped to the CPs the pieces hold
        self.on = {ch: getattr(self, name) for ch, name in self.SPECIAL.items()}

    # ---- streams
    def stream(self, name) -> bytes:
        try:
            return self.cf.read(name)
        except (KeyError, CfbError):  # missing, or a broken chain
            raise ReadError("damaged", f"{name} stream") from None

    def table_stream(self, name) -> bytes:
        """The table stream the FIB names; the other one when only that exists (dochan, anydoc)."""
        for n in (name, "0Table" if name == "1Table" else "1Table"):
            if self.cf.exists(n):
                return self.stream(n)
        raise ReadError("damaged", "no table stream")

    def run(self):
        if not self.cf.exists("WordDocument"):
            if self.cf.exists("EncryptedPackage"):  # an encrypted OOXML package saved as .doc
                raise ReadError("encrypted")
            raise ReadError("damaged", "no WordDocument stream")
        self.word = self.stream("WordDocument")
        self.fib = f = doc_fib.fib(self.word)
        if f.encrypted or f.obfuscated:
            raise ReadError("encrypted")
        self.table = self.table_stream(f.table)
        self.pieces, cut = doc_fib.fit(doc_fib.pieces(self.table, *f.pair(doc_fib.CLX)), len(self.word))
        if cut:
            self.ctx.skipped["text"] += cut
        self.lid = f.lid_fe if f.far_east and f.lid_fe else f.lid
        self.ranges = doc_fib.story_ranges(f.ccp, self.pieces[-1].cp_end if self.pieces else 0)
        with self.ctx.guard("document"):
            self.doc.blocks.extend(self.story("text"))
        return self.doc

    def deep(self):
        self.ctx.skipped["depth"] += 1
        if self.ctx.skipped["depth"] == 1:
            self.ctx.warnings.append("depth: skipped content nested too deeply")

    # ---- the walk
    def story(self, name) -> list:
        cp0, cp1 = self.ranges[name]
        s = doc_fib.story(self.word, self.pieces, cp0, cp1, self.lid)
        return self.walk(_State(name, s))

    def walk(self, st) -> list:
        """The text of st.story into blocks: plain runs are text and each control character runs its handler with
        its CP (story.cp_of its index: exact past surrogate pairs and pieces cut short). A story that does not end with
        a paragraph mark still ends its last paragraph. O(len(text) + controls * log(pieces))."""
        story = st.story
        text, cp_of = story.text, story.cp_of
        on, drop = self.on, self.drop
        pos = 0
        for m in _CONTROL.finditer(text):
            i = m.start()
            if i > pos:
                self.text(st, text[pos:i])
            on.get(text[i], drop)(st, cp_of(i))
            pos = i + 1
        if pos < len(text):
            self.text(st, text[pos:])
        self.para_end(st, cp_of(len(text)))
        return st.out

    # ---- inline scopes
    @staticmethod
    def live(st) -> bool:
        """Whether what comes now is shown: outside every field instruction and under the field depth cap."""
        return not st.codes and not st.over

    def add(self, st, inline):
        st.frames[-1].inl.append(inline)

    def text(self, st, s):
        """Shown text, or the instruction of the field it is in (up to MAX_INSTR units), or nothing past the depth
        cap or inside a field nested in an instruction. Surrogate pairs are joined here, as the Text is built."""
        if st.over:
            return
        if st.codes:
            f = st.fields[-1]
            if not f.result and f.size < MAX_INSTR:
                s = s[:MAX_INSTR - f.size]
                f.instr.append(s)
                f.size += len(s)
            return
        self.add(st, Text(_joined(s)))

    def push(self, st, frame) -> bool:
        if len(st.frames) < limits.MAX_DEPTH:
            st.frames.append(frame)
            return True
        self.deep()
        return False

    def close_to(self, st, n):
        """Close scopes until n are open: a link with content becomes a Link in its parent."""
        while len(st.frames) > n:
            f = st.frames.pop()
            if f.inl:
                parent = st.frames[-1].inl
                if f.kind == "link":
                    parent.append(Link(f.url, f.inl))
                else:
                    parent.extend(f.inl)

    # ---- control character handlers: (st, cp)
    def drop(self, st, cp):
        """A control character with no meaning here."""

    def para_end(self, st, cp):
        """A paragraph mark, a cell mark or a page or section break: the open paragraph becomes a block."""
        self.close_to(st, 1)
        inl = st.frames[0].inl
        if inl:
            st.out.append(Para(inl))
        st.reset()

    def line_break(self, st, cp):
        if self.live(st):
            self.add(st, Break())

    def hyphen(self, st, cp):
        """A non-breaking hyphen."""
        self.text(st, "-")

    def field_begin(self, st, cp):
        """A field's instruction follows. Past MAX_DEPTH open fields the field, up to its end, is skipped and counted
        once under depth (the fields nested in it are not counted again)."""
        if st.over or len(st.fields) >= limits.MAX_DEPTH:
            if not st.over:
                self.deep()
            st.over += 1
            return
        st.fields.append(_Field())
        st.codes += 1

    def field_separator(self, st, cp):
        """The instruction ends and the result, which is shown, begins; a HYPERLINK to a URL makes it a link."""
        if st.over or not st.fields or st.fields[-1].result:
            return
        f = st.fields[-1]
        f.result = True
        st.codes -= 1
        url = "" if st.codes else hyperlink_url(_joined("".join(f.instr)))
        if url and self.push(st, _Frame("link", url)):
            f.frame = len(st.frames) - 1

    def field_end(self, st, cp):
        """The innermost field ends: its link, if its result opened one, closes. A stray end is dropped."""
        if st.over:
            st.over -= 1
            return
        if not st.fields:
            return
        f = st.fields.pop()
        if not f.result:
            st.codes -= 1
        if f.frame is not None:
            self.close_to(st, f.frame)


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(_ZIP):  # a Word package saved as .doc
        return ooxml.parse(path, ctx)
    if head.removeprefix(_BOM).startswith(_RTF):  # RTF saved as .doc: read in phase 2b
        raise ReadError("unsupported", "rtf")
    if not is_cfb(head):
        raise ReadError("damaged", "not a Word document")
    with Compound(path) as cf:
        return _Reader(cf, ctx).run()
