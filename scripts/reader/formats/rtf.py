"""RTF reader: text, destinations, Unicode, code pages, hidden runs, headings, notes, fields, headers and footers.

Ported from MIT sources. The state machine (group state, destinations, paragraphs, notes, fields, the `\\*` rule and
the special characters) follows anydoc (`rtf/mod.rs`; the HYPERLINK instruction through `word_fields`, after
`shared/fields.rs`); the `\\v` hidden runs, the `\\uc` stack and the end of the `\\u` fallback skip follow xberg
(`extractors/rtf/parser`). The destination list is anydoc's list of destinations whose content is left out. Stdlib only.

The tokens come from `rtf_lex`; this module walks them once, O(input), with an explicit stack of group states: no
recursion, so group depth never costs the interpreter stack. Work is per token: the bytes of text and hex escapes
are gathered in one `Pending` and decoded together when anything else arrives.

Group state (`_Group`). Every `{` clones the state of its parent and every `}` restores it. The fields are the
font, `\\uc`, bold, hidden, the destination name, whether text is left out (`suppress`), where text is captured to
(`cap`: a field instruction, the info title, a style name), and the paragraph's outline level and style number.
Task 23 adds the table, list and picture fields here: add each one to `__slots__`, `__init__` and `clone`, and read
it where the three INTERIM comments below stand (`\\cell` and `\\row`, `\\listtext`, `\\pict`).

Output. Text goes to the current story (`_Story`): the body, a note, or a header or footer. A story keeps its own
paragraph being built and its own open fields, so a note or a header never ends the body's paragraph.

* Paragraphs: `\\par` and `\\sect` end one; `\\line`, `\\lbr`, `\\page` and `\\column` are a Break; `\\tab` is a space;
  `\\pard` resets the paragraph's outline and style. A paragraph with no text is dropped.
* Headings: `\\outlinelevelN` (0..8) gives level N+1; so does a paragraph style `\\sN` named `heading N` or
  `제목 N`, or based on one, the `\\sbasedon` chain walked iteratively, memoized and cut at 32 steps. A heading's
  bold is dropped.
* The `\\*` rule: `\\*` counts only as the first token after `{`. That group leaves its text out unless its destination
  is one this reader handles (fldinst, listtext, pntext, shppict, shptxt, nesttableprops). Leaving text out is a flag
  on the group, not a skip to its close: a handled destination inside (a shape's text inside its instructions, an
  object's result) switches text on again.
* Unicode: `\\uN` adds 65536 to a negative N; a high and a low surrogate combine, a lone one is U+FFFD. `\\ucN` is
  clamped to 0..10 and kept per group. After `\\u` the next N text bytes or `\\'hh` units are skipped; the skip ends
  at a brace or any other control word.
* Hidden: `\\v` and `\\v0` runs are Hidden inlines. Their bytes still feed the skip and the pending decoder.
* Notes: `{\\footnote ...}` is a NoteRef at its place and a block list in `Doc.notes`, the id `fn-N`; `\\ftnalt` makes it
  an endnote, `en-N`. The note's own `\\chftn` is dropped, so the number shows once at the reference and once at the
  definition. A note inside a note, or in a header, is read as text of the outer story. Past 10,000 notes the rest
  are counted under `note`.
* Fields: a HYPERLINK instruction wraps the field's result in a Link; any other field gives its result. Fields nest
  32 deep; a field past that keeps its result text and is counted under `depth`.
* Depth: groups past 1,000 deep are skipped to their matching close, counted once under `depth` for each run.
* Text past the root group's close is ignored. The input must start, after an optional byte-order mark and
  whitespace, with `{\\rtf`; a compound file goes to `doc.parse`, a zip to `ooxml.parse`.
* Code pages: `\\ansicpg`, `\\deff`, the font table's `\\fN \\fcharsetN \\cpgN` and `\\fN` per group, through
  `rtf_lex.codec`.
"""
import re

from ..containers.cfb import is_cfb
from ..limits import ReadError
from ..model import Bold, Break, Doc, Heading, Hidden, Link, NoteRef, Para, Text
from .rtf_lex import CLOSE, HEX, OPEN, SYM, TEXT, WORD, Pending, codec, tokens
from .word_fields import MAX_INSTR, hyperlink_url

MAX_GROUPS = 1_000      # groups nested deeper than this are skipped to their close
MAX_FIELDS = 32         # fields nested deeper than this give their result and no link
MAX_NOTES = 10_000      # notes kept
MAX_LINES = 10_000      # header and footer lines kept, each
MAX_STYLES = 10_000     # style definitions and fonts kept (a table past this is read no further)
STYLE_CHAIN = 32        # \sbasedon steps followed to find a heading
MAX_TITLE = 500         # characters kept of the info title

_ZIP = b"PK\x03\x04"
_BOM = b"\xef\xbb\xbf"
_RTF = b"{\\rtf"
_SPACE = b" \t\r\n\x0b\x0c\x00"
_HEADING = re.compile(r"(?:heading|제목)\s*([1-9])$", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# Destinations whose content is left out (anydoc's list), except the headers and footers, which fill Doc.headers and
# Doc.footers, and the info title, which fills Doc.title. A bookmark start is added: anydoc captures its name, here
# the name is simply not text.
_SKIP = frozenset((
    "fonttbl", "colortbl", "stylesheet", "info", "object", "ftnsep", "ftnsepc", "aftnsep", "aftnsepc",
    "xmlnstbl", "themedata", "colorschememapping", "datastore", "latentstyles", "listtable", "listoverridetable",
    "rsidtbl", "generator", "filetbl", "revtbl", "datafield", "bkmkend", "annotation", "atnid", "atnauthor",
    "template", "defchp", "defpap", "panose", "falt", "objdata", "blipuid", "nonshppict", "wgrffmtfilter",
    "pgdsctbl", "docvar", "sp", "sn", "sv", "shpinst", "background", "userprops", "operator", "author", "title",
    "subject", "keywords", "doccomm", "creatim", "revtim", "printim", "bkmkstart",
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
# INTERIM (Task 23): a cell or row end only ends the paragraph, so table text does not run together
_PAR_WORDS = frozenset(("par", "sect", "cell", "row", "nestcell", "nestrow"))


class _Group:
    """The state a `{` copies and a `}` restores. Task 23: add table, list and picture fields here, in __init__ and
    in clone()."""
    __slots__ = ("font", "uc", "bold", "hidden", "dest", "suppress", "cap", "sty", "outline")

    def __init__(self):
        self.font = None      # \fN, None until set (the \deff font then)
        self.uc = 1           # \ucN: the bytes a \u fallback takes
        self.bold = False
        self.hidden = False   # \v
        self.dest = ""        # the last destination word seen here or in a parent
        self.suppress = False  # text is left out
        self.cap = ""         # "fldinst", "title" or "style": text is captured there, even when suppressed
        self.sty = None       # \sN of the paragraph
        self.outline = None   # \outlinelevelN of the paragraph

    def clone(self):
        n = _Group.__new__(_Group)
        n.font, n.uc, n.bold, n.hidden = self.font, self.uc, self.bold, self.hidden
        n.dest, n.suppress, n.cap, n.sty, n.outline = self.dest, self.suppress, self.cap, self.sty, self.outline
        return n


class _Field:
    __slots__ = ("depth", "instr", "size", "start", "par", "over")

    def __init__(self, depth, start, par, over):
        self.depth, self.start, self.par, self.over = depth, start, par, over
        self.instr, self.size = [], 0


class _Story:
    """Where text goes: the body, a note, a header or a footer, with its paragraph under way and its open fields."""
    __slots__ = ("kind", "depth", "cur", "has", "pars", "fields", "blocks", "ref", "alt")

    def __init__(self, kind, depth):
        self.kind, self.depth = kind, depth
        self.cur = []        # (hidden, bold, text) runs, and finished inlines
        self.has = False     # the paragraph has something to show
        self.pars = 0        # paragraphs ended so far: a field that began in an earlier one starts at 0 in cur
        self.fields = []
        self.blocks = []
        self.ref = None      # a note's NoteRef in the story it came from
        self.alt = False     # a note that is an endnote


def _plain(inlines) -> str:
    out = []
    for it in inlines:
        if isinstance(it, Text):
            out.append(it.text)
        elif isinstance(it, (Bold, Link)):
            out.append(_plain(it.inlines))
        elif isinstance(it, Break):
            out.append(" ")
    return "".join(out)


def _edge(inlines, first):
    """Trim the whitespace and line breaks at the start (first) or the end of a paragraph's inlines."""
    i = 0 if first else -1
    while inlines:
        it = inlines[i]
        if isinstance(it, Break):
            inlines.pop(i)
            continue
        holder, t = inlines, it
        if isinstance(it, Bold):
            holder = it.inlines
            t = holder[i] if holder else None
            if t is None:
                inlines.pop(i)
                continue
        if isinstance(t, Text):
            t.text = t.text.lstrip() if first else t.text.rstrip()
            if not t.text:
                holder.pop(i)
                if holder is not inlines and not holder:
                    inlines.pop(i)
                continue
        return


def _build(items) -> list:
    """Inlines from a story's pending items: adjacent runs of one look merge, consecutive hidden runs are one Hidden."""
    out, hid, run = [], None, None  # run: [hidden, bold, parts]
    for it in items:
        if type(it) is tuple:
            if run is not None and run[0] == it[0] and run[1] == it[1]:
                run[2].append(it[2])
                continue
            if run is not None:
                hid = _put_run(out, run, hid)
            run = [it[0], it[1], [it[2]]]
        else:
            if run is not None:
                _put_run(out, run, hid)
                run = None
            hid = None
            out.append(it)
    if run is not None:
        _put_run(out, run, hid)
    return out


def _put_run(out, run, hid):
    h, b, parts = run
    inl = Text("".join(parts))
    if b:
        inl = Bold([inl])
    if not h:
        out.append(inl)
        return None
    if hid is None:
        hid = Hidden([])
        out.append(hid)
    hid.inlines.append(inl)
    return hid


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
        self.title = []
        self.n_notes = {"fn": 0, "en": 0}
        self.body = _Story("body", 0)
        self.stories = [self.body]
        self.story = self.body
        self.H = self.handlers()

    # ---- the walk
    def run(self, data):
        skipped = self.ctx.skipped
        for t in tokens(data):
            k = t[0]
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
                if not g.suppress or g.cap:
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
                if self.close_group():
                    break
            elif k == SYM:
                if self.pend:
                    self.flush()
                self.symbol(t[1], fresh)
            # BIN: the raw bytes are skipped
        if self.pend:
            self.flush()
        if self.hi is not None:
            self.drop_high()
        while self.stack:
            self.close_group()
        self.end_paragraph(self.body)

    def open_group(self):
        self.stack.append(self.g)
        g = self.g = self.g.clone()
        if g.cap == "style" and len(self.stack) != self.ss_depth + 1:
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
        self.g = self.stack.pop()
        depth -= 1
        while True:
            story = self.story
            while story.fields and story.fields[-1].depth > depth:
                self.close_field(story)
            if story is not self.body and story.depth > depth:
                self.close_story(story)
                continue
            break
        return depth == 0

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
            elif cap == "title":
                if sum(map(len, self.title)) < MAX_TITLE:
                    self.title.append(s)
            elif self.sty is not None:
                self.sty[2].append(s)
        elif not g.suppress:
            if _CTRL.search(s):
                s = _CTRL.sub(lambda m: " " if m.group() == "\t" else "", s)
                if not s:
                    return
            story = self.story
            story.cur.append((g.hidden, g.bold, s))
            if not story.has and not s.isspace():
                story.has = True

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
                self.add(Break(hidden="hidden" if g.hidden else ""))
        elif name in _HEADERS:
            self.header(_HEADERS[name])
        elif name in _SKIP:
            g = self.g
            g.suppress, g.dest = True, name
            g.cap = "title" if name == "title" and self.stack and self.stack[-1].dest == "info" else ""
            if name == "stylesheet":
                self.ss_depth = len(self.stack)

    def handlers(self):
        return {
            "u": self.w_u, "uc": self.w_uc, "f": self.w_f, "b": self.w_b, "v": self.w_v, "plain": self.w_plain,
            "pard": self.w_pard, "s": self.w_s, "sbasedon": self.w_sbasedon, "outlinelevel": self.w_outline,
            "ansicpg": self.w_ansicpg, "deff": self.w_deff,
            "fcharset": self.w_fcharset, "cpg": self.w_cpg,
            "field": self.w_field, "fldinst": self.w_fldinst, "fldrslt": self.w_fldrslt,
            "footnote": self.w_footnote, "ftnalt": self.w_ftnalt,
            "pict": self.w_pict, "shppict": self.w_shppict, "shptxt": self.w_on, "nesttableprops": self.w_on,
            "result": self.w_on, "listtext": self.w_listtext, "pntext": self.w_listtext,
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

    def w_plain(self, p, fresh):
        g = self.g
        g.bold = g.hidden = False

    def w_pard(self, p, fresh):
        g = self.g
        g.sty = g.outline = None

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

    def w_shppict(self, p, fresh):
        if fresh == 2:
            self.g.suppress = self.stack[-1].suppress if self.stack else False
        else:
            self.g.suppress = False

    def w_listtext(self, p, fresh):
        # INTERIM (Task 23): list text stays a plain prefix of its paragraph; Task 23 reads it as the list marker
        if fresh == 2:
            self.g.suppress = self.stack[-1].suppress if self.stack else False

    def w_pict(self, p, fresh):
        g = self.g
        if not g.suppress and not g.cap:
            # INTERIM (Task 23): a picture is skipped and counted, once; Task 23 reads its bytes
            self.ctx.skipped["image"] += 1
            g.suppress = True

    # ---- paragraphs
    def par(self):
        g = self.g
        if not g.suppress and not g.cap:
            self.end_paragraph(self.story)

    def heading_level(self) -> int:
        g = self.g
        if g.outline is not None:
            return g.outline + 1
        return self.style_level(g.sty) if g.sty is not None else 0

    def style_level(self, sid) -> int:
        levels = self.levels
        if sid in levels:
            return levels[sid]
        path, seen, cur, level = [], set(), sid, 0
        while cur is not None and cur not in seen and len(path) < STYLE_CHAIN:
            if cur in levels:
                level = levels[cur]
                break
            seen.add(cur)
            path.append(cur)
            m = _HEADING.match(self.names.get(cur, ""))
            if m:
                level = int(m.group(1))
                break
            cur = self.based.get(cur)
        for s in path:
            levels[s] = level
        return level

    def end_paragraph(self, story):
        cur, has = story.cur, story.has
        story.cur, story.has = [], False
        story.pars += 1
        if not has:
            return
        inlines = _build(cur)
        _edge(inlines, True)
        _edge(inlines, False)
        if not inlines:
            return
        level = self.heading_level() if story is self.body else 0
        if level:
            inlines = [x for it in inlines for x in (it.inlines if isinstance(it, Bold) else (it,))]
            story.blocks.append(Heading(level, inlines))
        else:
            story.blocks.append(Para(inlines))

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

    def close_field(self, story):
        f = story.fields.pop()
        if f.over:
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
        g = self.g
        if g.suppress or g.cap or self.story.kind != "body":
            return  # inside a note or a header the text stays part of the outer story
        if len(self.doc.notes) >= MAX_NOTES:
            self.ctx.skipped["note"] += 1
            g.suppress = True
            return
        story = _Story("note", len(self.stack))
        story.ref = NoteRef("", hidden="hidden" if g.hidden else "")
        self.add(story.ref)
        self.stories.append(story)
        self.story = story

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

    def close_story(self, story):
        self.end_paragraph(story)
        self.stories.pop()
        self.story = self.stories[-1]
        if story.kind == "note":
            ref = story.ref
            if not story.blocks:
                if self.story.cur and self.story.cur[-1] is ref:
                    self.story.cur.pop()
                return
            prefix = "en" if story.alt else "fn"
            self.n_notes[prefix] += 1
            ref.id = f"{prefix}-{self.n_notes[prefix]}"
            self.doc.notes[ref.id] = story.blocks
            return
        text = " ".join(t for t in (_plain(b.inlines).strip() for b in story.blocks) if t)
        lines = self.doc.headers if story.kind == "header" else self.doc.footers
        if text:
            if len(lines) < MAX_LINES:
                lines.append(text)
            else:
                self.ctx.skipped[story.kind] += 1


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(16)
        if head.startswith(_ZIP):  # a Word package saved as .rtf
            from . import ooxml
            return ooxml.parse(path, ctx)
        if is_cfb(head):  # a Word binary document saved as .rtf
            from . import doc
            return doc.parse(path, ctx)
        data = head + f.read()
    if data.startswith(_BOM):
        data = data[len(_BOM):]
    data = data.lstrip(_SPACE)
    if not data.startswith(_RTF):
        raise ReadError("damaged", "not an RTF file")
    r = _Reader(ctx)
    r.run(data)
    result = r.doc
    result.blocks = r.body.blocks
    result.title = " ".join("".join(r.title).split())[:MAX_TITLE]
    return result
