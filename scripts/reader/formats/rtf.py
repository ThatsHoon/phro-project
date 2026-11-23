"""RTF reader: text, destinations, Unicode, code pages, hidden runs, headings, notes, fields, headers, footers,
tables, lists and pictures.

Ported from MIT sources. The state machine (group state, destinations, paragraphs, notes, fields, the `\\*` rule and
the special characters) follows anydoc (`rtf/mod.rs`, including its list text and pictures; the HYPERLINK instruction through
`word_fields`, after `shared/fields.rs`; tables are `rtf_table`); the `\\v` hidden runs, the `\\uc` stack and the end of the `\\u` fallback skip follow xberg
(`extractors/rtf/parser`). The destination list is anydoc's list of destinations whose content is left out. Stdlib only.

The tokens come from `rtf_lex`; this module walks them once, O(input), with an explicit stack of group states: no
recursion, so group depth never costs the interpreter stack. Work is per token: the bytes of text and hex escapes
are gathered in one `Pending` and decoded together when anything else arrives.

Group state (`_Group`). Every `{` clones the state of its parent and every `}` restores it. The fields are the
font, `\\uc`, bold, hidden, the destination name, whether text is left out (`suppress`), where text is captured to
(`cap`: a field instruction, an info property, a style name, list text, a picture's data), the paragraph's outline
level and style number, and its table and list state (`in_table`, `itap`, `ls`, `ilvl`).

Output. Text goes to the current story (`_Story`): the body, a note, or a header or footer. A story keeps its own
paragraph being built and its own open fields, so a note or a header never ends the body's paragraph.

* Tables: `\\trowd`, `\\cellx`, `\\clmgf` `\\clmrg` `\\clvmgf` `\\clvmrg`, `\\trhdr`, `\\cell` and `\\row` (and `\\nestcell` and
  `\\nestrow`, with `{\\*\\nesttableprops ...}`, at `\\itap` 2 and up) drive one `rtf_table.Tables` per table, kept on the
  story. A paragraph is in the table when it has `\\intbl` or an `\\itap`; the first paragraph that is not ends the table
  at its place in the story, as does the end of the story or of the input. A table in a note or header stays there; a
  header's line holds the text of its cells.
* Lists: the text of a `\\listtext` or `\\pntext` group, without tabs, is the marker of the paragraph's ListItem, its
  level `\\ilvlN` clamped to 0..8 (0 first, as every format here). A paragraph with `\\lsN` and no list text has the
  marker `-`. `\\pard` resets `\\ls`, `\\ilvl`, `\\intbl` and `\\itap`. A heading stays a Heading.
* Pictures: `{\\pict ...}` with `\\pngblip`, `\\jpegblip`, `\\emfblip`, `\\wmetafile`, `\\dibitmap` or `\\macpict` is one Image after the
  paragraph it is in (a table cell's blocks, in a table). Its hex digits (cleaned in one `re.sub`, an odd last digit
  dropped) or `\\bin` bytes are charged against `IMAGE_BYTES`, the document's earlier pictures included, as they are
  collected: past it collection stops, `image` is counted once and nothing is emitted. A DIB becomes a BMP through
  `officeart.dib_to_bmp`, then a PNG. A `\\macpict` gets the 512-byte zero head of a PICT file before its bytes, as a BLIP PICT does. Any other format is counted under `image`. A group inside `\\pict` is
  skipped; `{\\*\\shppict{\\pict ...}}` is read and `\\nonshppict` skipped, so the pair is one image; an `\\object` whose
  `\\objdata` (hex or `\\bin`, EMBED_BYTES at most) holds an embedded OLE1 object is read by embedded.Objects: an
  equation stands in the line, tables follow the paragraph, and its `\\result` is left out; any other object keeps
  its `\\result`, and a chart left unread is counted.
* Paragraphs: `\\par` and `\\sect` end one; `\\line`, `\\lbr`, `\\page` and `\\column` are a Break; `\\tab` is a space;
  `\\pard` resets the paragraph's outline and style. A paragraph with no text is dropped.
* Headings: `\\outlinelevelN` (0..8) gives level N+1; so does a paragraph style `\\sN` named `heading N` or
  `제목 N`, or based on one, the `\\sbasedon` chain walked iteratively, memoized and cut at 32 steps. A heading's
  bold is dropped.
* The `\\*` rule: `\\*` counts only as the first token after `{`. That group leaves its text out unless its destination
  is one this reader handles (fldinst, listtext, pntext, shppict, shptxt, nesttableprops, footnote, annotation).
  Leaving text out is a flag on the group, not a skip to its close: a handled destination inside (a shape's text inside its instructions, an
  object's result) switches text on again.
* Unicode: `\\uN` adds 65536 to a negative N; a high and a low surrogate combine, a lone one is U+FFFD. `\\ucN` is
  clamped to 0..10 and kept per group. After `\\u` the next N text bytes or `\\'hh` units are skipped; the skip ends
  at a brace or any other control word.
* Hidden: `\\v` and `\\v0` runs are Hidden inlines. Their bytes still feed the skip and the pending decoder.
* Tracked changes: `\\deleted` runs are a Hidden of kind "deleted", `\\revised` runs an Inserted, each with the author
  the `\\revtbl` (entries ended by `;`) has at `\\revauthdelN` (a deletion's, else `\\revauthN`) or `\\revauthN`.
* Notes: `{\\footnote ...}` is a NoteRef at its place and a block list in `Doc.notes`, the id `fn-N`; `\\ftnalt` makes it
  an endnote, `en-N`. The note's own `\\chftn` is dropped, so the number shows once at the reference and once at the
  definition. A note inside a note, or in a header, is read as text of the outer story. Past 10,000 notes the rest
  are counted under `note`. A comment (`\\annotation`) is a note `cm-N` as `word_fields.comment_note` writes it, its
  author the `\\atnauthor` before it, else its `\\atnid` initials.
* Fields: a HYPERLINK instruction wraps the field's result in a Link; any other field gives its result. Fields nest
  32 deep; a field past that keeps its result text and is counted under `depth`.
* Depth: groups past 1,000 deep are skipped to their matching close, counted once under `depth` for each run.
* Text past the root group's close is ignored, but a stray close is not that close: a first pass counts the braces,
  and a `}` that would close the root while more closes than opens remain after it is ignored, the root staying open.
  So an extra `}` mid-body is skipped and the rest read; `{\\rtf1 one}\\r\\n trailing {\\b junk}` still ends at the
  first `}`. A `}` with no open group is ignored. The last paragraph, written without `\\par`, ends under the state
  it was written in. The input must start, after an optional byte-order mark and
  whitespace, with `{\\rtf`; a compound file goes to `cfb_route` (Word's to `doc.parse`, another format's to its own
  parser), a zip to `zip_route` (an OpenDocument package to `odf.parse`, any other to `ooxml.parse`). `parse_bytes`
  reads RTF held in memory and is what `parse` ends in.
* Code pages: `\\ansicpg`, `\\deff`, the font table's `\\fN \\fcharsetN \\cpgN` and `\\fN` per group, through
  `rtf_lex.codec`.
* Properties: the `\\info` group's `\\title` is Doc.title; its `\\subject`, `\\author`, `\\operator` (the last author),
  `\\keywords`, `\\company`, `\\creatim` and `\\revtim` (`\\yr` `\\mo` `\\dy` `\\hr` `\\min`) and the `\\userprops`
  pairs (`\\propname`, `\\staticval`) are Doc.meta["properties"] (word_fields.properties).
"""
import re

from .. import embedded, images, limits
from ..containers.cfb import Compound, is_cfb
from ..limits import ReadError
from ..model import (Bold, Break, Doc, Heading, Hidden, Image, Inserted, Link, ListItem, Math, NoteRef, Para, Table,
                     Text)
from ..officeart import dib_to_bmp
from .rtf_lex import BIN, CLOSE, HEX, OPEN, SYM, TEXT, WORD, Pending, codec, tokens
from . import zip_route
from .rtf_table import Tables
from .word_fields import (FORM_DEFAULT, MAX_ENTRIES, MAX_INSTR, MAX_NAME, MAX_PROPERTIES, MAX_PROPERTY, comment_note,
                          form_state, hyperlink_url, properties)

MAX_GROUPS = 1_000      # groups nested deeper than this are skipped to their close
MAX_FIELDS = 32         # fields nested deeper than this give their result and no link
MAX_NOTES = 10_000      # notes kept
MAX_LINES = 10_000      # header and footer lines kept, each
MAX_STYLES = 10_000     # style definitions and fonts kept (a table past this is read no further)
STYLE_CHAIN = 32        # \sbasedon steps followed to find a heading
MAX_TITLE = 500         # characters kept of the info title
MAX_MARK = 64           # characters kept of a list marker

_ZIP = b"PK\x03\x04"
_BOM = b"\xef\xbb\xbf"
_RTF = b"{\\rtf"
_SPACE = b" \t\r\n\x0b\x0c\x00"
_INFO = {"title": "title", "subject": "subject", "author": "author", "operator": "last author", "keywords": "keywords",
         "company": "company"}
_DATES = {"creatim": "created", "revtim": "modified"}
_DATE_WORDS = ("yr", "mo", "dy", "hr", "min")
_HEADING = re.compile(r"(?:heading|제목)\s*([1-9])$", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_NONHEX = re.compile(rb"[^0-9a-fA-F]")

# Destinations whose content is left out (anydoc's list), except the headers and footers, which fill Doc.headers and
# Doc.footers, and the info properties, which are captured. A bookmark start is added: anydoc captures its name, here
# the name is simply not text; so are company and a user property's name and value, which are info properties only.
_SKIP = frozenset((
    "fonttbl", "colortbl", "stylesheet", "info", "ftnsep", "ftnsepc", "aftnsep", "aftnsepc",
    "xmlnstbl", "themedata", "colorschememapping", "datastore", "latentstyles", "listtable", "listoverridetable",
    "rsidtbl", "generator", "filetbl", "revtbl", "datafield", "bkmkend",
    "template", "defchp", "defpap", "panose", "falt", "blipuid", "nonshppict", "wgrffmtfilter",
    "pgdsctbl", "docvar", "sp", "sn", "sv", "shpinst", "background", "userprops", "operator", "author", "title",
    "subject", "keywords", "doccomm", "creatim", "revtim", "printim", "bkmkstart", "company", "propname", "staticval",
))
_HEADERS = {"header": "header", "headerl": "header", "headerr": "header", "headerf": "header",
            "footer": "footer", "footerl": "footer", "footerr": "footer", "footerf": "footer"}
# control words that are one character of text (anydoc's, plus the zero-width joiners)
_CHARS = {
    "emdash": "—", "endash": "–", "lquote": "‘", "rquote": "’", "ldblquote": "“",
    "rdblquote": "”", "bullet": "•", "enspace": " ", "emspace": " ", "qmspace": " ", "tab": " ",
    "zwj": "‍", "zwnj": "‌",
}
# control symbols that are text: braces and backslash as themselves, and as doc.py reads Word's no-break space,
# no-break hyphen (0x1E) and optional hyphen (0x1F, dropped)
_SYMS = {"{": "{", "}": "}", "\\": "\\", "~": " ", "_": "-", "-": ""}
_CODEPAGES = {"mac": 10000, "pc": 437, "pca": 850}  # the charset words that stand in for \ansicpg
_LINE_WORDS = frozenset(("line", "lbr", "page", "column"))
_PAR_WORDS = frozenset(("par", "sect"))
# picture format words: the ones read (what _pict_data decodes), and the ones that are counted instead
_BLIPS = {"pngblip": "png", "jpegblip": "jpeg", "emfblip": "emf", "wmetafile": "wmf", "dibitmap": "dib",
          "macpict": "pict"}
_UNREAD = ("wbitmap", "pmmetafile")


class _Group:
    """The state a `{` copies and a `}` restores."""
    __slots__ = ("font", "uc", "bold", "hidden", "ins", "dele", "auth", "auth_del", "dest", "suppress", "cap", "sty",
                 "outline", "in_table", "itap", "ilvl", "ls")

    def __init__(self):
        self.font = None      # \fN, None until set (the \deff font then)
        self.uc = 1           # \ucN: the bytes a \u fallback takes
        self.bold = False
        self.hidden = False   # \v
        self.ins = self.dele = False
        self.auth = self.auth_del = None
        self.dest = ""        # the last destination word seen here or in a parent
        self.suppress = False  # text is left out
        self.cap = ""         # "fldinst", "title", "style", "list", "pict", "revtbl", "atnid" or "atnauthor": text is
                              # captured there, even when suppressed
        self.sty = None       # \sN of the paragraph
        self.outline = None   # \outlinelevelN of the paragraph
        self.in_table = False  # \intbl
        self.itap = 0         # \itapN, 0..9 (9 stands for anything deeper than 8)
        self.ilvl = 0         # \ilvlN
        self.ls = None        # \lsN: the paragraph is in a list

    def clone(self):
        n = _Group.__new__(_Group)
        n.font, n.uc, n.bold, n.hidden = self.font, self.uc, self.bold, self.hidden
        n.ins, n.dele, n.auth, n.auth_del = self.ins, self.dele, self.auth, self.auth_del
        n.dest, n.suppress, n.cap, n.sty, n.outline = self.dest, self.suppress, self.cap, self.sty, self.outline
        n.in_table, n.itap, n.ilvl, n.ls = self.in_table, self.itap, self.ilvl, self.ls
        return n


class _Pic:
    """A picture being collected: its format, the hex chunks or raw bytes so far, and what they are charged."""
    __slots__ = ("depth", "fmt", "hex", "raw", "chars", "nraw", "dead")

    def __init__(self, depth):
        self.depth = depth    # the group depth of the \pict group
        self.fmt = None
        self.hex, self.raw = [], []
        self.chars = self.nraw = 0  # hex characters and raw bytes collected
        self.dead = False     # over the budget or not readable: counted, nothing more is collected


class _Obj:
    """An \\object being read: its objdata's hex chunks or raw bytes so far, and whether it was shown."""
    __slots__ = ("depth", "hex", "raw", "chars", "nraw", "dead", "seen", "shown", "counted")

    def __init__(self, depth):
        self.depth = depth    # the group depth of the \object group
        self.hex, self.raw = [], []
        self.chars = self.nraw = 0
        self.dead = False     # over EMBED_BYTES: nothing more is collected and the object is not read
        self.seen = False     # an objdata group was met (a second one is skipped)
        self.shown = False    # the object was placed, so its result is left out
        self.counted = False  # the object was counted (a chart left unread, an equation converted in part)


class _Field:
    __slots__ = ("depth", "instr", "size", "start", "par", "over", "form")

    def __init__(self, depth, start, par, over):
        self.depth, self.start, self.par, self.over = depth, start, par, over
        self.instr, self.size = [], 0
        self.form = None


class _Story:
    """Where text goes: the body, a note, a header or a footer, with its paragraph under way and its open fields."""
    __slots__ = ("kind", "depth", "cur", "has", "pars", "fields", "blocks", "ref", "alt", "tbl", "mark", "mark_len",
                 "extras", "kept", "pard", "author")

    def __init__(self, kind, depth):
        self.kind, self.depth = kind, depth
        self.cur = []        # (hidden, bold, inserted, text) runs (see look), and finished inlines
        self.has = False     # the paragraph has something to show
        self.pars = 0        # paragraphs ended so far: a field that began in an earlier one starts at 0 in cur
        self.fields = []
        self.blocks = []
        self.ref = None      # a note's NoteRef in the story it came from
        self.alt = False     # a note that is an endnote
        self.tbl = None      # the rtf_table.Tables open in this story
        self.mark = []       # list text of the paragraph under way
        self.mark_len = 0
        self.extras = []     # Images of the paragraph under way
        self.kept = None     # (sty, outline, ls, ilvl) a group closed inside the paragraph under way left to it
        self.pard = False    # a \pard came after the story's last \row: an empty paragraph then ends the table
        self.author = None   # a comment's author: a note story with one is an annotation


def _plain(inlines) -> str:
    out = []
    for it in inlines:
        if isinstance(it, Text):
            out.append(it.text)
        elif isinstance(it, (Bold, Link, Inserted)):
            out.append(_plain(it.inlines))
        elif isinstance(it, Break):
            out.append(" ")
    return "".join(out)


def _all_hidden(blocks) -> bool:
    """Whether every block is text made only of Hidden runs (a note whose body was written under `\v`)."""
    return all(getattr(b, "inlines", None) and all(isinstance(i, Hidden) for i in b.inlines) for b in blocks)


def _text(block) -> str:
    """The text of a header's or footer's block: a table's cells one after another."""
    if isinstance(block, Table):
        return " ".join(t for row in block.rows for c in row for b in c.blocks for t in (_text(b),) if t)
    inl = getattr(block, "inlines", None)
    return _plain(inl).strip() if inl is not None else ""


def _edge(inlines, first):
    """Trim the whitespace and line breaks at the start (first) or the end of a paragraph's inlines: one slice
    deletion, so a long run of breaks costs O(n)."""
    n = len(inlines)
    k = 0  # items dropped from the edge
    while k < n:
        it = inlines[k if first else n - 1 - k]
        if isinstance(it, Break):
            k += 1
            continue
        t = it
        if isinstance(it, (Bold, Inserted)):
            if not it.inlines:
                k += 1
                continue
            t = it.inlines[0 if first else -1]
        if isinstance(t, Text):
            t.text = t.text.lstrip() if first else t.text.rstrip()
            if not t.text:
                if t is not it:
                    it.inlines.pop(0 if first else -1)  # a Bold holds one run; it is now empty
                k += 1
                continue
        break
    if k:
        if first:
            del inlines[:k]
        else:
            del inlines[n - k:]


def _build(items) -> list:
    """Inlines from a story's pending items: adjacent runs of one look merge; consecutive hidden runs of one kind and
    author are one Hidden, consecutive inserted runs of one author one Inserted."""
    out, box, run = [], None, None  # run: [hidden, bold, inserted, parts]; box: the open Hidden or Inserted
    for it in items:
        if type(it) is tuple:
            if run is not None and run[0] == it[0] and run[1] == it[1] and run[2] == it[2]:
                run[3].append(it[3])
                continue
            if run is not None:
                box = _put_run(out, run, box)
            run = [it[0], it[1], it[2], [it[3]]]
        else:
            if run is not None:
                _put_run(out, run, box)
                run = None
            box = None
            out.append(it)
    if run is not None:
        _put_run(out, run, box)
    return out


def _put_run(out, run, box):
    h, b, ins, parts = run
    inl = Text("".join(parts))
    if b:
        inl = Bold([inl])
    if h:
        if not (isinstance(box, Hidden) and (box.kind, box.author) == h):
            box = Hidden([], *h)
            out.append(box)
    elif ins is not None:
        if not (isinstance(box, Inserted) and box.author == ins):
            box = Inserted([], ins)
            out.append(box)
    else:
        out.append(inl)
        return None
    box.inlines.append(inl)
    return box


class _Reader:
    def __init__(self, ctx):
        self.ctx = ctx
        self.doc = Doc()
        self.pend = Pending()
        self.g = _Group()
        self.stack = []        # the parents of the current group
        self.over = 0          # groups open inside a group skipped for depth
        self.skip = 0          # the fallback bytes a \u still takes
        self.hi = None         # a high surrogate waiting for its low one
        self.fresh = 0         # 1 right after `{`, 2 after `\*` as the first token of a group
        self.ansicpg = None
        self.deff = None
        self.fonts = {}        # font number -> [charset, cpg]
        self.names = {}        # style number -> name
        self.based = {}        # style number -> \sbasedon
        self.levels = {}       # style number -> heading level, 0 for none (memo)
        self.ss_depth = -10    # depth of the stylesheet group, while inside it
        self.sty = None        # [style number, based on, name parts] of the stylesheet entry being read
        self.info = {}
        self.dates = {}
        self.user = []
        self.data = b""        # the input the BIN tokens point into
        self.pic = None        # the picture being collected
        self.obj = None        # the object being read
        self.objects = embedded.of(ctx)
        self.img_total = 0     # bytes of the pictures emitted so far (IMAGE_BYTES)
        # Cells the tables of the document may still take: one budget for the whole document, shared by every table
        # and charged per cell, as doc.py's is. It is the bound on hostile input (many tiny tables or one huge one
        # cost the same), so one legitimate table of more than TABLE_NODES cells is cut and counted under "cells" once.
        # Kept per document on purpose (ruled): a limit per table would let a file of many tables grow without bound.
        self.budget = [limits.TABLE_NODES]
        self.n_notes = {"fn": 0, "en": 0, "cm": 0}
        self.revs = []         # the revision table's text: its authors, each ended by ";"
        self.revs_len = 0
        self.authors = None    # the revision table's authors, once read
        self.atn = {"atnid": [], "atnauthor": []}  # the next comment's initials and author
        self.body = _Story("body", 0)
        self.stories = [self.body]
        self.story = self.body
        self.H = self.handlers()

    # ---- the walk
    def run(self, data):
        skipped = self.ctx.skipped
        self.data = data
        # the totals of braces, to tell a stray close from the root's real one
        total_open = total_close = 0
        for ticks, t in enumerate(tokens(data)):
            if not ticks & 1023 and self.ctx.late():
                break
            k = t[0]
            if k == OPEN:
                total_open += 1
            elif k == CLOSE:
                total_close += 1
        seen_open = seen_close = 0
        leaf = None  # the group state in force when the root closed
        for ticks, t in enumerate(tokens(data)):
            if not ticks & 1023 and self.ctx.late():
                break
            k = t[0]
            if k == OPEN:
                seen_open += 1
            elif k == CLOSE:
                seen_close += 1
            if self.over:
                if k == OPEN:
                    self.over += 1
                elif k == CLOSE:
                    self.over -= 1
                continue
            if self.skip:
                if k == TEXT:
                    n = len(t[1])
                    if n <= self.skip:
                        self.skip -= n
                        continue
                    t = (TEXT, t[1][self.skip:])
                    self.skip = 0
                elif k == HEX:
                    self.skip -= 1
                    continue
                else:
                    self.skip = 0
            if self.hi is not None and not (k == WORD and t[1] == "u"):
                self.drop_high()
            fresh = self.fresh
            if fresh:
                self.fresh = 0
            g = self.g
            if k == TEXT or k == HEX:
                cap = g.cap
                if cap == "pict":
                    if k == TEXT:
                        self.pict_text(t[1])
                elif cap == "objdata":
                    if k == TEXT:
                        self.obj_data(t[1], len(t[1]), 0)
                elif not g.suppress or cap:
                    self.pend.push(t[1])
            elif k == WORD:
                if self.pend:
                    self.flush()
                self.word(t[1], t[2], fresh)
            elif k == OPEN:
                if self.pend:
                    self.flush()
                if len(self.stack) >= MAX_GROUPS:
                    self.over = 1
                    skipped["depth"] += 1
                else:
                    self.open_group()
            elif k == CLOSE:
                if self.pend:
                    self.flush()
                if len(self.stack) == 1 and total_close - seen_close > total_open - seen_open:
                    continue  # a stray: more closes than opens remain, so this one does not end the root
                leaf = g
                if self.close_group():
                    break
            elif k == SYM:
                if self.pend:
                    self.flush()
                self.symbol(t[1], fresh)
            elif k == BIN:
                if g.cap == "pict":
                    self.pict_bin(t[1], t[2])  # anywhere else the raw bytes are skipped
                elif g.cap == "objdata":
                    self.obj_data(bytes(self.data[t[1]:t[1] + t[2]]), 0, t[2])
        if self.pend:
            self.flush()
        if self.hi is not None:
            self.drop_high()
        if leaf is None:
            leaf = self.g
        while self.stack:
            self.close_group()
        self.g = leaf  # the last paragraph ends under the state it was written in: its heading, bold, hidden
        self.end_paragraph(self.body)
        self.end_table(self.body)

    def open_group(self):
        self.stack.append(self.g)
        g = self.g = self.g.clone()
        if g.cap in ("pict", "objdata"):
            g.cap, g.suppress = "", True  # a group inside a picture (its uid, its properties) is not data
        elif g.cap == "style" and len(self.stack) != self.ss_depth + 1:
            g.cap = ""
        elif g.dest == "stylesheet" and len(self.stack) == self.ss_depth + 1:
            g.cap = "style"
            self.sty = [None, None, []]
        self.fresh = 1

    def close_group(self) -> bool:
        """End the group; True when it was the root group. A `}` with no open group is ignored."""
        depth = len(self.stack)
        if not depth:
            return False
        if depth == self.ss_depth + 1:
            self.finish_style()
        elif depth == self.ss_depth:
            self.ss_depth = -10
        closing = self.g
        if self.pic is not None and depth == self.pic.depth:
            self.finish_pict(closing)
        obj = self.obj
        if obj is not None:
            if depth == obj.depth:
                if obj.seen and not obj.shown and not obj.counted:
                    self.ctx.skipped["object"] += 1
                self.obj = None
            elif closing.cap == "objdata":
                self.finish_object(closing)
        self.g = self.stack.pop()
        story = self.story
        if story.has and story.depth < depth and not closing.suppress and not closing.cap:
            self.keep_paragraph(story, closing, self.g)
        depth -= 1
        while True:
            story = self.story
            while story.fields and story.fields[-1].depth > depth:
                self.close_field(story)
            if story is not self.body and story.depth > depth:
                self.close_story(story, closing)
                continue
            break
        return depth == 0

    @staticmethod
    def keep_paragraph(story, closing, parent):
        """A paragraph whose properties were set inside a group that closes before its `\\par` (`{\\pard\\s1 x}`) keeps
        them. When the story holds text of the paragraph under way and the closing group has a heading or list state
        its parent lacks, that state is kept on the story for that paragraph only, a later closing group's replacing
        it; end_paragraph takes it where the paragraph's own group has none and clears it, and a `\\pard` clears it.
        The parent group's state is never written, so the next paragraph starts from its own. A group that already
        ended its paragraph leaves nothing under way, so nothing is kept."""
        props = (closing.sty, closing.outline, closing.ls, closing.ilvl)
        if props[:3] != (None, None, None) and props != (parent.sty, parent.outline, parent.ls, parent.ilvl):
            story.kept = props

    # ---- text
    def flush(self):
        g = self.g
        font = g.font if g.font is not None else self.deff
        ent = self.fonts.get(font)
        text = self.pend.flush(codec(ent[0] if ent else None, ent[1] if ent else None, self.ansicpg))
        if text:
            self.put(text)

    def put(self, s):
        """Text to where this group sends it: a capture, the current story, or nowhere."""
        g = self.g
        if g.cap:
            cap = g.cap
            if cap == "fldinst":
                fields = self.story.fields
                if fields and not fields[-1].over:
                    f = fields[-1]
                    if f.size < MAX_INSTR:
                        f.instr.append(s[:MAX_INSTR - f.size])
                        f.size += len(s)
            elif cap in _INFO:
                parts = self.info.setdefault(cap, [])
                if sum(map(len, parts)) < (MAX_TITLE if cap == "title" else MAX_PROPERTY):
                    parts.append(s)
            elif cap in ("propname", "staticval"):
                pair = self.user[-1] if self.user else None
                k = cap == "staticval"
                if pair is not None and len(pair[k]) < (MAX_PROPERTY if k else MAX_NAME):
                    pair[k] += s
            elif cap == "ffl":
                fields = self.story.fields
                entries = fields[-1].form["entries"] if fields and fields[-1].form else None
                if entries and len(entries[-1]) < MAX_NAME:
                    entries[-1] += s[:MAX_NAME]
            elif cap == "formfield":
                pass
            elif cap == "revtbl":
                if self.revs_len < MAX_STYLES * MAX_NAME:
                    self.revs.append(s)
                    self.revs_len += len(s)
            elif cap in self.atn:
                parts = self.atn[cap]
                if sum(map(len, parts)) < MAX_NAME:
                    parts.append(s)
            elif cap == "list":
                story = self.story
                if story.mark_len < MAX_MARK:
                    story.mark.append(s[:MAX_MARK])
                    story.mark_len += len(s)
            elif self.sty is not None:
                self.sty[2].append(s)
        elif not g.suppress:
            if _CTRL.search(s):
                s = _CTRL.sub(lambda m: " " if m.group() == "\t" else "", s)
                if not s:
                    return
            story = self.story
            story.cur.append((*self.look(), s))
            if not story.has and not s.isspace():
                story.has = True

    def look(self):
        """The current group's (hidden, bold, inserted) for a run: hidden is None or a Hidden's (kind, author), a
        deletion first; inserted is None or the insertion's author."""
        g = self.g
        if g.dele:
            return ("deleted", self.author(g.auth_del if g.auth_del is not None else g.auth)), g.bold, None
        if g.hidden:
            return ("hidden", ""), g.bold, None
        return None, g.bold, self.author(g.auth) if g.ins else None

    def author(self, n) -> str:
        """Entry n of the revision table, "" when it has none."""
        if self.authors is None:
            self.authors = [" ".join(a.split())[:MAX_NAME] for a in "".join(self.revs).split(";")[:-1]][:MAX_STYLES]
        return self.authors[n] if n is not None and 0 <= n < len(self.authors) else ""

    def why_hidden(self) -> str:
        """The `hidden` of a break, a note mark or a picture written now: "deleted", "hidden" or ""."""
        g = self.g
        return "deleted" if g.dele else "hidden" if g.hidden else ""

    def add(self, inline):
        """A finished inline (a break, a note reference) into the current story."""
        story = self.story
        story.cur.append(inline)
        if isinstance(inline, NoteRef):
            story.has = True

    def drop_high(self):
        self.hi = None
        self.put("�")

    # ---- control symbols and words
    def symbol(self, ch, fresh):
        if ch == "*":
            if fresh == 1:
                self.g.suppress = True
                self.fresh = 2
            return
        s = _SYMS.get(ch)
        if s:
            self.put(s)

    def word(self, name, p, fresh):
        h = self.H.get(name)
        if h is not None:
            h(p, fresh)
        elif name in _CHARS:
            self.put(_CHARS[name])
        elif name in _PAR_WORDS:
            self.par()
        elif name in _CODEPAGES:
            if self.ansicpg is None:
                self.ansicpg = _CODEPAGES[name]
        elif name in _LINE_WORDS:
            g = self.g
            if not g.suppress and not g.cap:
                self.add(Break(hidden=self.why_hidden()))
        elif name in _HEADERS:
            self.header(_HEADERS[name])
        elif name in _SKIP:
            g = self.g
            g.suppress, g.dest = True, name
            parent = self.stack[-1].dest if self.stack else ""
            g.cap = name if name == "revtbl" or parent == "info" and (name in _INFO or name in _DATES) \
                or parent == "userprops" and name in ("propname", "staticval") else ""
            if g.cap == "propname" and len(self.user) < MAX_PROPERTIES:
                self.user.append(["", ""])
            elif g.cap in _DATES:
                self.dates[name] = {}
            if name == "stylesheet":
                self.ss_depth = len(self.stack)

    def handlers(self):
        return {
            "u": self.w_u, "uc": self.w_uc, "f": self.w_f, "b": self.w_b, "v": self.w_v, "plain": self.w_plain,
            "revised": self.w_revised, "deleted": self.w_deleted, "revauth": self.w_revauth,
            "revauthdel": self.w_revauthdel, "atnid": self.w_capture("atnid"), "atnauthor": self.w_capture("atnauthor"),
            "annotation": self.w_annotation, **{name: self.w_date(name) for name in _DATE_WORDS},
            "pard": self.w_pard, "s": self.w_s, "sbasedon": self.w_sbasedon, "outlinelevel": self.w_outline,
            "ansicpg": self.w_ansicpg, "deff": self.w_deff,
            "fcharset": self.w_fcharset, "cpg": self.w_cpg,
            "field": self.w_field, "fldinst": self.w_fldinst, "fldrslt": self.w_fldrslt,
            "formfield": self.w_formfield, "ffl": self.w_ffl, "fftype": self.w_ff("kind"), "ffres": self.w_ff("result"),
            "ffdefres": self.w_ff("default"),
            "footnote": self.w_footnote, "ftnalt": self.w_ftnalt,
            "pict": self.w_pict, "shppict": self.w_shppict, "shptxt": self.w_on, "nesttableprops": self.w_on,
            "result": self.w_result, "object": self.w_object, "objdata": self.w_objdata, "listtext": self.w_listtext, "pntext": self.w_listtext,
            "intbl": self.w_intbl, "itap": self.w_itap, "ls": self.w_ls, "ilvl": self.w_ilvl,
            "trowd": self.w_trowd, "cellx": self.w_cellx, "trhdr": self.w_trhdr,
            "clmgf": self.w_merge("first"), "clmrg": self.w_merge("cont"),
            "clvmgf": self.w_merge("vfirst"), "clvmrg": self.w_merge("vcont"),
            "cell": self.w_end(False, "cell"), "row": self.w_end(False, "row"),
            "nestcell": self.w_end(True, "cell"), "nestrow": self.w_end(True, "row"), "trleft": self.w_trleft,
            **{name: self.w_blip(kind) for name, kind in _BLIPS.items()},
            **{name: self.w_blip(None) for name in _UNREAD},
        }

    def w_u(self, p, fresh):
        g = self.g
        if p is None:
            self.skip = 0
            return
        self.skip = g.uc
        code = p + 65536 if p < 0 else p
        if 0xD800 <= code <= 0xDBFF:
            if self.hi is not None:
                self.drop_high()
            self.hi = code
            return
        if 0xDC00 <= code <= 0xDFFF:
            if self.hi is not None:
                code = 0x10000 + ((self.hi - 0xD800) << 10) + (code - 0xDC00)
                self.hi = None
            else:
                code = 0xFFFD
        elif self.hi is not None:
            self.drop_high()
        if code > 0x10FFFF or code < 0:
            code = 0xFFFD
        self.put(chr(code))

    def w_uc(self, p, fresh):
        self.g.uc = min(max(1 if p is None else p, 0), 10)

    def w_f(self, p, fresh):
        if p is not None:
            self.g.font = p

    def w_b(self, p, fresh):
        self.g.bold = p != 0

    def w_v(self, p, fresh):
        self.g.hidden = p != 0

    def w_revised(self, p, fresh):
        self.g.ins = p != 0

    def w_deleted(self, p, fresh):
        self.g.dele = p != 0

    def w_revauth(self, p, fresh):
        self.g.auth = p

    def w_revauthdel(self, p, fresh):
        self.g.auth_del = p

    def w_plain(self, p, fresh):
        g = self.g
        g.bold = g.hidden = g.ins = g.dele = False
        g.auth = g.auth_del = None
        g.font = None  # the \deff font again

    def w_pard(self, p, fresh):
        g = self.g
        g.sty = g.outline = g.ls = None
        g.ilvl = g.itap = 0
        g.in_table = False
        if self.live():
            story = self.story
            story.kept = None  # what a closed group left to the paragraph is reset with it
            story.pard = True

    def w_s(self, p, fresh):
        g = self.g
        if g.cap == "style":
            if self.sty is not None:
                self.sty[0] = p
        else:
            g.sty = p

    def w_sbasedon(self, p, fresh):
        if self.g.cap == "style" and self.sty is not None:
            self.sty[1] = p

    def w_outline(self, p, fresh):
        if p is not None and 0 <= p <= 8:
            self.g.outline = p
        elif p == 9:
            self.g.outline = None  # level 9 is body text: it clears an earlier level

    def w_ansicpg(self, p, fresh):
        self.ansicpg = p

    def w_deff(self, p, fresh):
        self.deff = p

    def w_fcharset(self, p, fresh):
        g = self.g
        if g.dest == "fonttbl" and g.font is not None and (g.font in self.fonts or len(self.fonts) < MAX_STYLES):
            self.fonts.setdefault(g.font, [None, None])[0] = p

    def w_cpg(self, p, fresh):
        g = self.g
        if g.dest == "fonttbl" and g.font is not None and (g.font in self.fonts or len(self.fonts) < MAX_STYLES):
            self.fonts.setdefault(g.font, [None, None])[1] = p

    def w_on(self, p, fresh):
        """A destination that is read even inside text that is left out (a shape's text, an object's result)."""
        self.g.suppress = False

    def w_result(self, p, fresh):
        obj = self.obj
        if obj is None or not obj.shown or len(self.stack) <= obj.depth:
            self.g.suppress = False

    def w_object(self, p, fresh):
        g = self.g
        if not g.suppress and not g.cap and self.obj is None:
            self.obj = _Obj(len(self.stack))
        g.suppress, g.dest, g.cap = True, "object", ""

    def w_objdata(self, p, fresh):
        g, obj = self.g, self.obj
        g.cap = ""
        if obj is not None and not obj.seen and len(self.stack) == obj.depth + 1:
            obj.seen, g.cap = True, "objdata"
        g.suppress, g.dest = True, "objdata"

    def w_shppict(self, p, fresh):
        if fresh == 2:
            self.g.suppress = self.stack[-1].suppress if self.stack else False
        else:
            self.g.suppress = False

    def w_listtext(self, p, fresh):
        g = self.g
        if fresh == 2:
            g.suppress = self.stack[-1].suppress if self.stack else False
        if not g.suppress and not g.cap:
            g.cap, g.suppress = "list", True  # its text is the marker of the paragraph, not part of it

    def w_ls(self, p, fresh):
        if p and self.g.cap != "style":
            self.g.ls = p

    def w_ilvl(self, p, fresh):
        self.g.ilvl = min(max(p or 0, 0), 8)

    # ---- tables
    def table_of(self, story):
        if story.tbl is None:
            story.tbl = Tables(self.ctx, self.budget)
        return story.tbl

    def tbl_at(self, level):
        tb = self.table_of(self.story)
        tb.nest(level)
        return tb

    def end_table(self, story):
        tb, story.tbl = story.tbl, None
        if tb is not None:
            table = tb.finish()
            if table is not None:
                story.blocks.append(table)

    def live(self):
        g = self.g
        return not g.suppress and not g.cap

    def w_intbl(self, p, fresh):
        if self.live():
            self.g.in_table = True

    def w_itap(self, p, fresh):
        if self.live():
            g = self.g
            g.itap = min(max(p or 0, 0), 9)
            g.in_table = g.in_table or g.itap > 0

    def w_trowd(self, p, fresh):
        if self.live():
            self.g.in_table = True
            self.tbl_at(max(self.g.itap, 1)).trowd()

    def w_cellx(self, p, fresh):
        if self.live():
            self.tbl_at(max(self.g.itap, 1)).cellx(p or 0)

    def w_trhdr(self, p, fresh):
        if self.live():
            self.tbl_at(max(self.g.itap, 1)).header_row()

    def w_merge(self, kind):
        def merge(p, fresh):
            if self.live():
                self.tbl_at(max(self.g.itap, 1)).merge(kind)
        return merge

    def w_end(self, nested, op):
        """The handler of a cell or row end: the paragraph under way ends in the table, then `op` ("cell" or "row")
        runs at level 1, or at the paragraph's level (2 at least) for \nestcell and \nestrow."""
        def end(p, fresh):
            if not self.live():
                return
            g = self.g
            g.in_table = True
            self.end_paragraph(self.story)
            getattr(self.tbl_at(max(g.itap, 2) if nested else 1), op)()
            if op == "row" and not nested:
                g.in_table, g.itap = False, 0  # what follows is a table paragraph only if it says so
                self.story.pard = False
        return end

    def w_trleft(self, p, fresh):
        if self.live():
            self.tbl_at(max(self.g.itap, 1)).trleft(p or 0)

    # ---- pictures
    def w_pict(self, p, fresh):
        g = self.g
        if g.suppress or g.cap or self.pic is not None:
            return
        self.pic = _Pic(len(self.stack))
        g.cap, g.suppress = "pict", True

    def w_blip(self, kind):
        def blip(p, fresh):
            pic = self.pic
            if pic is None or self.g.cap != "pict" or pic.fmt is not None or pic.dead:
                return
            pic.fmt = kind or "unread"
            if kind is None:
                self.pict_fail(pic)
        return blip

    def pict_fail(self, pic):
        """The picture is counted under image, once, and no more of it is collected."""
        pic.dead = True
        pic.hex, pic.raw = [], []
        self.ctx.skipped["image"] += 1

    def pict_text(self, raw):
        pic = self.pic
        if pic is None or pic.dead:
            return
        pic.chars += len(raw)
        if self.img_total + pic.chars // 2 + pic.nraw > limits.IMAGE_BYTES:
            self.pict_fail(pic)
        else:
            pic.hex.append(raw)

    def pict_bin(self, off, n):
        pic = self.pic
        if pic is None or pic.dead:
            return
        pic.nraw += n
        if self.img_total + pic.chars // 2 + pic.nraw > limits.IMAGE_BYTES:
            self.pict_fail(pic)
        else:
            pic.raw.append(bytes(self.data[off:off + n]))

    # ---- objects
    def obj_data(self, chunk, chars, nraw):
        obj = self.obj
        if obj.dead:
            return
        obj.chars += chars
        obj.nraw += nraw
        if obj.chars // 2 + obj.nraw > limits.EMBED_BYTES:
            obj.dead = True
            obj.hex, obj.raw = [], []
        else:
            (obj.raw if nraw else obj.hex).append(chunk)

    def finish_object(self, g):
        """The objdata group closes: the object's equation goes in the line and its tables after the paragraph
        under way. An equation converted in part is counted and keeps the result too."""
        obj = self.obj
        if obj.dead:
            return
        if obj.raw:
            data = b"".join(obj.raw)
        else:
            digits = _NONHEX.sub(b"", b"".join(obj.hex))
            data = bytes.fromhex(digits[:len(digits) & ~1].decode("ascii"))
        obj.hex, obj.raw = [], []
        native = embedded.ole1_native(data)
        got = self.objects.load(native) if native else None
        kind = got[0] if got else ""
        hidden = "deleted" if g.dele else "hidden" if g.hidden else ""
        story, skipped = self.story, self.ctx.skipped
        if kind == "equation":
            story.cur.append(Math(got[1], hidden=hidden))
            story.has = True
            if "depth" in got[2]:
                skipped["depth"] += 1
            if got[2] <= {"depth"}:
                obj.shown = True
            else:
                skipped["object"] += 1
                obj.counted = True
        elif kind == "table":
            tables = self.objects.place(got)
            if tables is None:
                skipped["chart"] += 1
                obj.counted = True
            else:
                for t in tables:
                    t.hidden = hidden
                story.extras.extend(tables)
                obj.shown = True
        elif kind == "chart":
            skipped["chart"] += 1
            obj.counted = True

    def finish_pict(self, g):
        """The pict group closes: its Image goes after the paragraph under way (before the next cell, in a table)."""
        pic, self.pic = self.pic, None
        if pic.dead:
            return
        data = None
        if pic.fmt in ("png", "jpeg", "emf", "wmf", "dib", "pict"):
            try:
                if pic.raw:
                    data = b"".join(pic.raw)
                else:
                    digits = _NONHEX.sub(b"", b"".join(pic.hex))
                    data = bytes.fromhex(digits[:len(digits) & ~1].decode("ascii"))
                if pic.fmt == "pict":  # an rtf PICT has no file head: Image data starts with the 512 zero bytes
                    data = bytes(512) + data
                if pic.fmt == "dib":  # a DIB is a BMP without its file header
                    bmp = dib_to_bmp(data)
                    data = images.bmp_to_png(bmp) if bmp is not None else None
            except Exception:
                data = None
        pic.hex, pic.raw = [], []
        if data is None or len(data) < 16 or images.sniff(data) is None or \
                self.img_total + len(data) > limits.IMAGE_BYTES:
            self.ctx.skipped["image"] += 1
            return
        self.img_total += len(data)
        self.story.extras.append(Image(data, hidden="deleted" if g.dele else "hidden" if g.hidden else ""))

    # ---- paragraphs
    def par(self):
        g = self.g
        if not g.suppress and not g.cap:
            self.end_paragraph(self.story)

    def heading_level(self, sty, outline) -> int:
        if outline is not None:
            return outline + 1
        return self.style_level(sty) if sty is not None else 0

    def style_level(self, sid) -> int:
        """The heading level of style sid, 0 for none. The chain of \\sbasedon is walked iteratively; a heading
        counts when the style's own walk reaches it within STYLE_CHAIN steps. The memo holds (level, steps to the
        heading) so a style never gets what only another style's walk proved."""
        memo = self.levels
        if sid in memo:
            return memo[sid][0]
        path, seen, cur = [], set(), sid
        level, found, definite = 0, None, True  # found: the path index of the heading style
        while cur is not None and cur not in seen:
            hit = memo.get(cur)
            if hit is not None:
                if hit[0] and len(path) + hit[1] < STYLE_CHAIN:
                    level, found = hit[0], len(path) + hit[1]
                elif hit[0]:
                    definite = False
                break
            if len(path) >= STYLE_CHAIN:
                definite = False
                break
            seen.add(cur)
            path.append(cur)
            m = _HEADING.match(self.names.get(cur, ""))
            if m:
                level, found = int(m.group(1)), len(path) - 1
                break
            cur = self.based.get(cur)
        if found is not None:
            for i, st in enumerate(path):
                memo[st] = (level, found - i)
        elif definite:
            for st in path:
                memo[st] = (0, 0)
        else:
            memo[sid] = (0, 0)  # only its own walk is proven
        return level

    def end_paragraph(self, story, g=None):
        """End the paragraph under way in `story`, its properties those of `g` (the current group by default): a
        Heading, a ListItem or a Para, then its pictures, into the cell of the table it is in or the story's blocks.
        A paragraph that is not in a table ends the story's open table when it has something to show, or when a
        `\\pard` came after the last `\\row`: an empty `\\pard ...\\par` is how Word separates two tables. Only a bare
        `\\par` between `\\row` and the next `\\trowd`, with no `\\pard`, is a stray break that keeps the table."""
        g = g or self.g
        cur, has, extras, mark = story.cur, story.has, story.extras, story.mark
        story.cur, story.has = [], False
        story.pars += 1
        sty, outline, ls, ilvl = g.sty, g.outline, g.ls, g.ilvl
        kept, story.kept = story.kept, None
        if kept is not None:  # what a group closed inside this paragraph left it, where g has none of its own
            if sty is None and outline is None:
                sty, outline = kept[0], kept[1]
            if ls is None and kept[2] is not None:
                ls, ilvl = kept[2], kept[3]
        blocks = []
        if has:
            inlines = _build(cur)
            _edge(inlines, True)
            _edge(inlines, False)
            if inlines:
                level = self.heading_level(sty, outline) if story is self.body else 0
                label = _CTRL.sub("", "".join(mark)).strip() if mark else ""
                if level:
                    inlines = [x for it in inlines for x in (it.inlines if isinstance(it, Bold) else (it,))]
                    blocks.append(Heading(level, inlines))
                elif label or ls:
                    blocks.append(ListItem(ilvl, label or "-", inlines))
                else:
                    blocks.append(Para(inlines))
        if mark:
            story.mark, story.mark_len = [], 0
        if extras:
            story.extras = []
            blocks.extend(extras)
        level = g.itap or (1 if g.in_table else 0)
        if level:
            tb = self.table_of(story)
            tb.nest(level)
            for b in blocks:
                tb.put(b)
        else:
            if story.tbl is not None and (blocks or story.pard):  # a bare \par after \row keeps the table
                self.end_table(story)
            story.blocks.extend(blocks)

    # ---- stylesheet
    def finish_style(self):
        sty, self.sty = self.sty, None
        if sty is None or sty[0] is None or (sty[0] not in self.names and len(self.names) >= MAX_STYLES):
            return
        self.names[sty[0]] = "".join(sty[2]).split(";")[0].strip()
        if sty[1] is not None:
            self.based[sty[0]] = sty[1]

    # ---- fields
    def w_field(self, p, fresh):
        g = self.g
        if g.suppress or g.cap:
            return
        story = self.story
        over = len(story.fields) >= MAX_FIELDS
        if over:
            self.ctx.skipped["depth"] += 1
        story.fields.append(_Field(len(self.stack), len(story.cur), story.pars, over))

    def w_fldinst(self, p, fresh):
        g = self.g
        g.suppress = True
        fields = self.story.fields
        g.cap = "fldinst" if fields and not fields[-1].over else ""

    def w_fldrslt(self, p, fresh):
        self.g.cap = ""

    def w_formfield(self, p, fresh):
        """A legacy form field's data inside its instruction: its kind, result, default and drop-down entries are
        kept on the field, and none of its text (its name, its help) is the instruction's."""
        g = self.g
        fields = self.story.fields
        if g.cap == "fldinst" and fields and not fields[-1].over:
            fields[-1].form = {"kind": 0, "result": FORM_DEFAULT, "default": 0, "entries": []}
            g.cap = "formfield"
        g.suppress = True

    def w_ff(self, key):
        def ff(p, fresh):
            fields = self.story.fields
            if self.g.cap == "formfield" and fields and fields[-1].form and p is not None:
                fields[-1].form[key] = p
        return ff

    def w_ffl(self, p, fresh):
        g = self.g
        fields = self.story.fields
        if g.cap == "formfield" and fields and fields[-1].form and len(fields[-1].form["entries"]) < MAX_ENTRIES:
            fields[-1].form["entries"].append("")
            g.cap = "ffl"

    def close_field(self, story):
        f = story.fields.pop()
        if f.over:
            return
        if f.form:
            shown = form_state(f.form["kind"], f.form["result"], f.form["default"], f.form["entries"])
            if shown:
                story.cur.append((*self.look(), shown))
                story.has = True
            return
        url = hyperlink_url("".join(f.instr))
        start = f.start if f.par == story.pars else 0
        if not url or start >= len(story.cur):
            return
        content = _build(story.cur[start:])
        if _plain(content).strip() or any(not isinstance(x, Text) for x in content):
            story.cur[start:] = [Link(url, content)]

    # ---- notes, headers and footers
    def w_footnote(self, p, fresh):
        self.note(fresh, None)

    def w_annotation(self, p, fresh):
        atn = self.atn
        author = "".join(atn["atnauthor"]).strip() or "".join(atn["atnid"]).strip()
        atn["atnid"], atn["atnauthor"] = [], []
        self.note(fresh, " ".join(author.split())[:MAX_NAME])

    def w_date(self, name):
        def date(p, fresh):
            g = self.g
            if g.cap in _DATES and p is not None:
                self.dates[g.cap][name] = p
        return date

    def w_capture(self, name):
        def capture(p, fresh):
            g = self.g
            g.suppress, g.dest, g.cap = True, name, name
            self.atn[name] = []
        return capture

    def note(self, fresh, author):
        """A footnote, or a comment by `author` (a str): `{\\*\\footnote` and `{\\*\\annotation` are read as the plain
        destinations are, unless their group is inside text that is left out."""
        g = self.g
        if fresh == 2:
            g.suppress = self.stack[-1].suppress if self.stack else False
        if g.suppress or g.cap or self.story.kind != "body":
            if author is not None or fresh == 2:
                g.suppress = True  # a comment, or a starred note, inside a note or a header is left out
            return  # inside a note or a header the text stays part of the outer story
        if len(self.doc.notes) >= MAX_NOTES:
            self.ctx.skipped["note"] += 1
            g.suppress = True
            return
        story = _Story("note", len(self.stack))
        story.ref = NoteRef("", hidden=self.why_hidden())
        story.author = author
        self.add(story.ref)
        self.stories.append(story)
        self.story = story
        g.ls, g.ilvl = None, 0  # a note starts with no list state of the paragraph it follows

    def w_ftnalt(self, p, fresh):
        if self.story.kind == "note" and self.story.depth == len(self.stack):
            self.story.alt = True

    def header(self, kind):
        g = self.g
        if g.suppress or g.cap or self.story.kind != "body":
            g.suppress = True
            return
        story = _Story(kind, len(self.stack))
        self.stories.append(story)
        self.story = story

    def close_story(self, story, g):
        self.end_paragraph(story, g)
        self.end_table(story)
        self.stories.pop()
        self.story = self.stories[-1]
        if story.kind == "note":
            ref = story.ref
            if not story.blocks:
                if self.story.cur and self.story.cur[-1] is ref:
                    self.story.cur.pop()
                return
            if not ref.hidden and _all_hidden(story.blocks):
                ref.hidden = "hidden"  # a note whose whole text is hidden follows the hidden rule: so does its mark
            prefix = "cm" if story.author is not None else "en" if story.alt else "fn"
            self.n_notes[prefix] += 1
            ref.id = f"{prefix}-{self.n_notes[prefix]}"
            self.doc.notes[ref.id] = story.blocks if story.author is None else comment_note(story.blocks, story.author)
            return
        text = " ".join(t for t in map(_text, story.blocks) if t)
        lines = self.doc.headers if story.kind == "header" else self.doc.footers
        if text:
            if len(lines) < MAX_LINES:
                lines.append(text)
            else:
                self.ctx.skipped[story.kind] += 1


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        data = f.read()  # read once; the BOM and blanks are skipped by a view, not a copy
    head = data[:16]
    if head.startswith(_ZIP):  # a package saved as .rtf: an OpenDocument one or a Word one
        return zip_route.parse(path, ctx)
    if is_cfb(head):  # a compound file saved as .rtf: Word's, or another format's by its streams
        from . import cfb_route
        with Compound(path) as cf:
            other = cfb_route.route(cf, "rtf", "not an RTF file")
        return cfb_route.parse(other, path, ctx)
    return parse_bytes(data, ctx)


def parse_bytes(data, ctx, spent=None) -> Doc:
    """Read RTF held in memory: `parse` after it has sent the zip and compound-file heads on. Nothing touches the
    disk, so a caller can read bytes it never writes out. `spent`, when given, is {"images": the picture bytes the
    document reading this RTF inside it (a docx altChunk) has placed}: they count against IMAGE_BYTES with this RTF's,
    and the total is written back."""
    i = len(_BOM) if data.startswith(_BOM) else 0
    while i < len(data) and data[i] in _SPACE:
        i += 1
    if not data.startswith(_RTF, i):
        raise ReadError("damaged", "not an RTF file")
    r = _Reader(ctx)
    if spent is not None:
        r.img_total = spent["images"]
    r.run(memoryview(data)[i:] if i else data)
    if spent is not None:
        spent["images"] = r.img_total
    result = r.doc
    result.blocks = r.body.blocks
    result.title = " ".join("".join(r.info.pop("title", ())).split())[:MAX_TITLE]
    pairs = [(_INFO[k], "".join(v)) for k, v in r.info.items()]
    pairs += [(_DATES[k], _date(d)) for k, d in r.dates.items()]
    if kept := properties(pairs + [tuple(x) for x in r.user]):
        result.meta["properties"] = kept
    return result


def _date(d) -> str:
    """`YYYY-MM-DD HH:MM` of an RTF time group's words; "" without a year in 1602..9999 (1601 is the unset time
    Word writes for a document never saved or printed)."""
    yr = d.get("yr", 0)
    if not 1601 < yr < 10_000:
        return ""
    mo, dy, hr, mi = (min(max(d.get(k, low), 0), 99) for k, low in (("mo", 1), ("dy", 1), ("hr", 0), ("min", 0)))
    return f"{yr:04d}-{mo:02d}-{dy:02d} {hr:02d}:{mi:02d}"
