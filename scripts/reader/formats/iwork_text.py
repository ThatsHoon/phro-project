"""iWork text storages (`TSWP.StorageArchive`, 2001) to blocks: one reader for Pages, Numbers and Keynote text.

Ported from docling (MIT) `docling/backend/iwork/content.py` (`split_paragraphs`, `runs_for`, `value_at`,
`label_for_style`, `ListStyle.label`) and `docling/backend/iwork/archives.py` (`IWAReader.storage_blocks`,
`comments`, `_thread`, `_author`, `iwa_object_runs`, `iwa_depth_runs`, `iwa_style_name`, `iwa_list_style`,
`iwa_formatting`, `iwa_link`), v2.150.0 (1ae470a1f4580881ed73853f64e0bf178f623c5e).

The text is field 3's strings joined, split into paragraphs at `\\n`. Every run table has the shape
`{1 entries {1 character_index, 2 TSP.Reference}}`; an entry without a reference ends the run before it. Read:
5 paragraph styles (2022; headings by name), 6 paragraph data (list depth), 7 list styles (2023; `-` or `1.`),
8 character styles (2021; bold, property 1), 9 attachments (2003 -> a drawable, placed after its paragraph by the
caller's `attach`), 11 smart fields (2032 hyperlinks), 16 footnotes (2008 -> a storage), 22 deletions (2060 kind 2,
left out as `Hidden(kind="deleted")`) and 23 highlights (2013 -> 3056 comments with their replies). Insertions
(21) are plain text. Style names and properties are found through the style's super (1) and its parent (3), at
most `STYLE_CHAIN` parents, with a visited set.

Changes from docling: character indexes are UTF-16 code units (Apple's NSString; python-pages converts them so),
mapped here to code points, an offset inside a surrogate pair to its character and one past the end to the end, so
every character is kept once; text outside the BMP adds the `utf16` warning, since that mapping is not confirmed on
a real file. A paragraph style's name is taken from the nearest named style of its chain, as a list style's
labels are in docling. Tracked deletions are read (docling does not). A comment and its replies become one note,
each through `word_fields.comment_note`; docling's `[author: …]` form is not used.
"""
import re
from bisect import bisect_left, bisect_right
from collections import deque
from itertools import accumulate, groupby
from typing import Callable, NamedTuple

from .. import limits
from ..containers import pbwire
from ..model import Bold, Heading, Hidden, Link, ListItem, NoteRef, Para, Text
from .word_fields import MAX_URL, comment_note

STYLE_CHAIN = 8  # style parents followed to find a name or a property (docling); past it the chain stops

STORAGE, ATTACHMENT, FOOTNOTE, HIGHLIGHT = 2001, 2003, 2008, 2013
CHAR_STYLE, PARA_STYLE, LIST_STYLE, LINK, CHANGE = 2021, 2022, 2023, 2032, 2060
COMMENT, AUTHOR = 3056, 212
DELETION = 2  # TSWP.ChangeArchive kind
LINK_SCHEMES = ("http://", "https://", "mailto:")
OBJECT_REPLACEMENT = "\ufffc"
UTF16_WARNING = "utf16: text positions may be off after characters outside the BMP"
_HEADING = re.compile(r"heading\s*(\d{1,3})?")


def ref(f: dict, n: int) -> int | None:
    """The target id of singular TSP.Reference field n (merged as protobuf merges it), or None."""
    v = pbwire.one(pbwire.msg(f, n), 1)
    return v if isinstance(v, int) else None


def refs(f: dict, n: int) -> list[int]:
    """The target ids of repeated TSP.Reference field n, in order."""
    out = []
    for v in f.get(n, []):
        if isinstance(v, memoryview):
            r = pbwire.ref(v)
            if r is not None:
                out.append(r)
    return out


def _str(v) -> str | None:
    return bytes(v).decode("utf-8", errors="replace") if isinstance(v, memoryview) else None


def run_table(f: dict, n: int) -> list[tuple[int, int | None]]:
    """(character index, object id or None) of ObjectAttributeTable field n, by index (a stable sort keeps the
    later of two entries at one index last). An entry without an integer index is skipped. ValueError on bad bytes."""
    out = []
    for entry in pbwire.msg(f, n).get(1, []):
        if not isinstance(entry, memoryview):
            continue
        e = pbwire.fields(entry)
        index = pbwire.one(e, 1)
        if isinstance(index, int):
            out.append((index, ref(e, 2)))
    out.sort(key=lambda p: p[0])
    return out


def _para_data(f: dict) -> list[tuple[int, int]]:
    """(character index, list depth) of field 6, ParaDataAttributeTable {1 character_index, 2 first, 3 second}."""
    out = []
    for entry in pbwire.msg(f, 6).get(1, []):
        if not isinstance(entry, memoryview):
            continue
        e = pbwire.fields(entry)
        index, first = pbwire.one(e, 1), pbwire.one(e, 2, 0)
        if isinstance(index, int) and isinstance(first, int):
            out.append((index, first))
    out.sort(key=lambda p: p[0])
    return out


def level(name: str) -> int | None:
    """The heading level of a Pages style name (Defaults "Pages styles"): "Title" and "Heading" or "Heading 1" are
    1, "Subtitle" and "Subheading" 2, "Heading N" N up to 6; any other name is body text."""
    n = name.strip().casefold()
    if n == "title":
        return 1
    if n in ("subtitle", "subheading"):
        return 2
    m = _HEADING.fullmatch(n)
    if m:
        return max(1, min(int(m.group(1)) if m.group(1) else 1, 6))
    return None


def plain(blocks: list) -> list[str]:
    """The visible text of each block that has inlines (hidden text and note marks left out), for header and footer
    lines."""
    def flat(items):
        out = []
        for it in items:
            if isinstance(it, Text) and not it.hidden:
                out.append(it.text)
            elif isinstance(it, (Bold, Link)) and not it.hidden:
                out.append(flat(it.inlines))
        return "".join(out)
    return [s for s in (flat(getattr(b, "inlines", [])) for b in blocks if not b.hidden) if s.strip()]


class _Offsets:
    """UTF-16 code-unit offsets of one text to its code-point indexes, clamped to the text."""

    def __init__(self, text: str):
        self.n = len(text)
        self.wide = not text.isascii() and max(text) > "￿"
        if self.wide:
            self.starts = list(accumulate((2 if c > "￿" else 1 for c in text), initial=0))

    def __call__(self, u: int) -> int:
        if not self.wide:
            return min(u, self.n)
        if u >= self.starts[-1]:
            return self.n
        return bisect_right(self.starts, u) - 1  # inside a surrogate pair: that character


class _Runs:
    """A run table as (code-point index, value), each value in force from its index to the next entry's."""

    def __init__(self, pairs):
        self.idx = [i for i, _ in pairs]
        self.val = [v for _, v in pairs]

    def at(self, i: int):
        k = bisect_right(self.idx, i) - 1
        return self.val[k] if k >= 0 else None

    def inside(self, a: int, b: int) -> list[int]:
        return self.idx[bisect_right(self.idx, a):bisect_left(self.idx, b)]


class _Seg(NamedTuple):
    text: str | None   # None for a note mark
    note: str | None
    bold: bool
    url: str | None
    deleted: bool


def _inlines(segs: list) -> list:
    """Segments to inlines: deleted stretches in one Hidden, then one Link per url, then one Bold per bold stretch,
    adjacent text joined."""
    out = []
    for deleted, g in groupby(segs, key=lambda s: s.deleted):
        linked = []
        for url, g2 in groupby(g, key=lambda s: s.url):
            styled = []
            for bold, g3 in groupby(g2, key=lambda s: s.bold):
                items, texts = [], []
                for s in g3:
                    if s.note is None:
                        texts.append(s.text)
                        continue
                    if texts:
                        items.append(Text("".join(texts)))
                        texts = []
                    items.append(NoteRef(s.note))
                if texts:
                    items.append(Text("".join(texts)))  # joined once, however many runs it spans
                styled.extend([Bold(items)] if bold else items)
            linked.extend([Link(url, styled)] if url else styled)
        out.extend([Hidden(linked, "deleted")] if deleted else linked)
    return out


class _Tables(NamedTuple):
    heading: _Runs
    depth: _Runs
    lists: _Runs
    bold: _Runs
    link: _Runs
    deleted: _Runs
    highlight: _Runs
    attachments: list
    footnotes: list


class TextReader:
    """Reads text storages into blocks. Footnotes and comments go into `notes` (Doc.notes) as `fn-N` and `cm-N`;
    `attach(drawable_id)` gives the blocks of a drawable anchored at U+FFFC, placed after its paragraph. Each
    storage is entered through `Store.follow`, so one read twice or reached through itself is read once. Each
    comment is emitted at most once a document (`comment_thread`)."""

    def __init__(self, store, ctx, notes: dict, attach: Callable[[int], list]):
        self.store, self.ctx, self.notes, self.attach = store, ctx, notes, attach
        self._note_ids: dict[int, str | None] = {}   # footnote or highlight object id -> note id (None: no note)
        self._thread_of: dict[int, str | None] = {}  # comment (3056) id -> the note id of the thread holding it
        self._next = {"fn": 0, "cm": 0}
        self._cache: dict[tuple, object] = {}         # (lookup, style id) -> value
        self._read: dict[int, list] = {}              # storage id -> its blocks, once read
        self._head = 0                                # > 0 while a header or footer is read

    def blocks(self, storage_id, repeat: bool = False) -> list:
        """The blocks of a storage. A storage already read gives [] (it was placed where it was first read), or with
        `repeat` its blocks again, from the first read and never walked again (Numbers cells can share one rich
        text storage)."""
        if repeat and storage_id in self._read:
            return list(self._read[storage_id])
        out = []
        for got in self.store.follow([storage_id], self._storage, self.store.depth):
            out.extend(got)
        return out

    def header_lines(self, storage_id) -> list[str]:
        """A header or footer storage as one line a paragraph. A footnote or comment there counts `note` and adds no
        note, so no note is left without its mark (the WordPerfect header rule)."""
        self._head += 1
        try:
            return plain(self.blocks(storage_id))
        finally:
            self._head -= 1

    # ---- objects
    def _get(self, id, type) -> dict | None:
        """The object's fields, None when missing, of another type, or not a message."""
        if id is None:
            return None
        try:
            return self.store.get(id, type)
        except ValueError:
            return None

    def _chain(self, sid, type):
        """The style and at most STYLE_CHAIN parents (super 1 -> parent 3), each once."""
        seen = set()
        for _ in range(STYLE_CHAIN + 1):
            if sid is None or sid in seen:
                return
            seen.add(sid)
            f = self._get(sid, type)
            if f is None:
                return
            yield f
            try:
                sid = ref(pbwire.msg(f, 1), 3)
            except ValueError:
                return

    def _cached(self, key, fn):
        if key not in self._cache:
            try:
                self._cache[key] = fn()
            except ValueError:
                self._cache[key] = None
        return self._cache[key]

    def _heading(self, sid) -> int | None:
        def find():
            for f in self._chain(sid, PARA_STYLE):
                name = _str(pbwire.one(pbwire.msg(f, 1), 1))
                if name and name.strip():
                    return level(name)
            return None
        return self._cached(("h", sid), find)

    def _bold(self, sid) -> bool:
        def find():
            for f in self._chain(sid, CHAR_STYLE):
                v = pbwire.one(pbwire.msg(f, 11), 1)
                if isinstance(v, int):
                    return bool(v)
            return False
        return bool(self._cached(("b", sid), find))

    def _label_types(self, sid) -> tuple:
        """A list style's label types (field 11, packed or not), one per depth, from the first style of its chain
        that has any; parsed once a style."""
        def find():
            for f in self._chain(sid, LIST_STYLE):
                types = []
                for v in f.get(11, []):
                    types.extend(pbwire.packed(v) if isinstance(v, memoryview) else [v] if isinstance(v, int) else [])
                if types:
                    return tuple(types)
            return ()
        return self._cached(("l", sid), find) or ()

    def _marker(self, sid, depth: int) -> str | None:
        types = self._label_types(sid)
        t = types[depth] if depth < len(types) else 0
        return None if t == 0 else "1." if t == 3 else "-"

    def _url(self, lid) -> str | None:
        if lid is None:
            return None
        def find():
            item = self.store.objects.get(lid)
            if item is None or item[0] != LINK:
                return None
            try:
                url = (_str(pbwire.one(self.store.get(lid), 2)) or "").strip()
            except ValueError:
                self.ctx.skipped["link"] += 1  # the text stays; its target is lost
                return None
            return url[:MAX_URL] if url.lower().startswith(LINK_SCHEMES) else None
        return self._cached(("u", lid), find)

    def _deleted(self, cid) -> bool:
        if cid is None:
            return False
        item = self.store.objects.get(cid)
        if item is None or item[0] != CHANGE:
            return False
        f = self._get(cid, CHANGE)
        # a change that cannot be read is left out as deleted: --hidden still shows it
        return f is None or pbwire.one(f, 1) == DELETION

    # ---- notes
    def _new_id(self, prefix: str) -> str:
        while True:
            self._next[prefix] += 1
            nid = f"{prefix}-{self._next[prefix]}"
            if nid not in self.notes:
                return nid

    def _footnote(self, fid) -> str | None:
        """The note id of footnote object `fid` (2008 -> 2 storage), its blocks into `notes` once; None when it holds
        nothing. A footnote that cannot be read loses its note, counted `note`, as does one in a header or footer."""
        if self._head:
            self.ctx.skipped["note"] += 1
            return None
        if fid in self._note_ids:
            return self._note_ids[fid]
        self._note_ids[fid] = nid = None
        with self.ctx.guard("note"):
            f = self.store.get(fid, FOOTNOTE)
            sid = ref(f, 2) if f is not None else None
            if sid is None or self.store.objects.get(sid, (None,))[0] != STORAGE:
                self.ctx.skipped["note"] += 1
                return None
            blocks = self.blocks(sid)
            if blocks:
                nid = self._new_id("fn")
                self.notes[nid] = blocks
        self._note_ids[fid] = nid
        return nid

    def comment_thread(self, thread_id) -> str | None:
        """The note id of the comment thread that starts at `thread_id` (TSD.CommentStorageArchive, 3056 {1 text,
        3 author -> 212 {1 name}, 4 replies}): the comment, then its replies breadth first, each "Comment by
        <author>: …" (`word_fields.comment_note`), into `notes` once. Each comment is emitted at most once a document:
        one already in a thread gives that thread's note id, so a second highlight on a thread marks the same note and
        the work never grows with the highlights. None when the thread holds no text. Replies are followed
        MAX_DEPTH deep (past it counted `depth`); a thread that cannot be read counts `note`. Keynote's comments
        (2014) call this too."""
        if thread_id is None:
            return None
        if thread_id in self._thread_of:
            return self._thread_of[thread_id]
        walked, paras, failed = [], [], True
        with self.ctx.guard("note"):
            pending = deque([(thread_id, 0)])
            while pending:
                cid, depth = pending.popleft()
                if cid is None or cid in self._thread_of:
                    continue
                if depth > limits.MAX_DEPTH:
                    self.ctx.skipped["depth"] += 1
                    break
                self._thread_of[cid] = None  # entered: this comment is never walked again
                walked.append(cid)
                c = self.store.get(cid, COMMENT)
                if c is None:
                    continue
                body = (_str(pbwire.one(c, 1)) or "").strip()
                if body:
                    paras.extend(comment_note([Para([Text(body)])], self._author(c)))
                pending.extend((r, depth + 1) for r in refs(c, 4))
            failed = False
        if failed:
            self._thread_of[thread_id] = None
        nid = None
        if paras and not failed:
            nid = self._new_id("cm")
            self.notes[nid] = paras
        for cid in walked:
            self._thread_of[cid] = nid
        return nid

    def _comment(self, hid) -> str | None:
        """The note id of highlight `hid` (2013 -> 1 the thread's first comment), through `comment_thread`. A
        highlight that cannot be read, or one in a header or footer, counts `note`."""
        if self._head:
            self.ctx.skipped["note"] += 1
            return None
        if hid in self._note_ids:
            return self._note_ids[hid]
        self._note_ids[hid] = None
        thread = None
        with self.ctx.guard("note"):
            f = self.store.get(hid, HIGHLIGHT)
            if f is None:
                self.ctx.skipped["note"] += 1
                return None
            thread = ref(f, 1)
        self._note_ids[hid] = nid = self.comment_thread(thread)
        return nid

    def _author(self, c: dict) -> str:
        a = self._get(ref(c, 3), AUTHOR)
        return (_str(pbwire.one(a, 1)) or "").strip() if a is not None else ""

    # ---- the storage
    def _table(self, f, n, pos, value, lost=None) -> _Runs:
        try:
            pairs = run_table(f, n)
        except ValueError:
            if lost:
                self.ctx.skipped[lost] += 1
            return _Runs([])
        return _Runs([(pos(i), value(r)) for i, r in pairs])

    def _anchors(self, f, n, pos, lost) -> list:
        try:
            return [(pos(i), r) for i, r in run_table(f, n) if r is not None]
        except ValueError:
            self.ctx.skipped[lost] += 1
            return []

    def _storage(self, sid, depth):
        f = self.store.get(sid, STORAGE)
        if f is None:
            return None
        text = "".join(s for s in (_str(v) for v in f.get(3, [])) if s is not None)
        pos = _Offsets(text)
        if pos.wide and UTF16_WARNING not in self.ctx.warnings:
            self.ctx.warnings.append(UTF16_WARNING)
        try:
            depths = _para_data(f)
        except ValueError:
            depths = []
        t = _Tables(
            heading=self._table(f, 5, pos, self._heading),
            depth=_Runs([(pos(i), d) for i, d in depths]),
            lists=self._table(f, 7, pos, lambda r: r),
            bold=self._table(f, 8, pos, self._bold),
            link=self._table(f, 11, pos, self._url, "link"),
            # an unreadable deletion table fails the storage: its deleted text must not show as body text
            deleted=_Runs([(pos(i), self._deleted(r)) for i, r in run_table(f, 22)]),
            highlight=self._table(f, 23, pos, lambda r: r, "note"),
            attachments=self._anchors(f, 9, pos, "object"),
            footnotes=self._anchors(f, 16, pos, "note"),
        )
        out = []
        closed: set = set()
        start = 0
        for line in text.split("\n"):
            end = start + len(line)
            out.extend(self._paragraph(text, start, end, t, closed))
            start = end + 1
        self._read[sid] = out
        return out

    def _paragraph(self, text, start, end, t: _Tables, closed: set) -> list:
        """The paragraph text[start:end] (end is its newline, or the text's end) and the drawables anchored in it.
        A comment's mark follows the highlighted text where its highlight ends or changes; a highlight that runs
        past the paragraph's end puts its mark at the end of the first paragraph it covers, and no later one."""
        lo = bisect_left(t.footnotes, (start,))
        hi = bisect_right(t.footnotes, (end, float("inf")))
        notes = t.footnotes[lo:hi]
        cuts = {p for p, _ in notes if start < p < end}
        for runs in (t.bold, t.link, t.deleted, t.highlight):
            cuts.update(runs.inside(start, end))
        bounds = [start, *sorted(cuts), end]
        segs: list[_Seg] = []
        current, deleted, k = None, False, 0

        def close():
            if current is not None and current not in closed:
                closed.add(current)
                nid = self._comment(current)
                if nid is not None:
                    segs.append(_Seg(None, nid, False, None, deleted))

        for a, b in zip(bounds, bounds[1:]):
            h = t.highlight.at(a)
            if h != current:
                close()
                current = h
            deleted = bool(t.deleted.at(a))
            while k < len(notes) and notes[k][0] <= a:
                nid = self._footnote(notes[k][1])
                if nid is not None:
                    segs.append(_Seg(None, nid, False, None, deleted))
                k += 1
            s = text[a:b].replace(OBJECT_REPLACEMENT, "")
            if s:
                segs.append(_Seg(s, None, bool(t.bold.at(a)), t.link.at(a), deleted))
        close()
        deleted = bool(t.deleted.at(end))
        for _, fid in notes[k:]:  # at the newline
            nid = self._footnote(fid)
            if nid is not None:
                segs.append(_Seg(None, nid, False, None, deleted))
        out = []
        if any(s.note is not None or s.text.strip() for s in segs):
            inl = _inlines(segs)
            lvl = t.heading.at(start)
            style = t.lists.at(start)
            marker = self._marker(style, t.depth.at(start) or 0) if style is not None and not lvl else None
            if lvl:
                out.append(Heading(lvl, inl))
            elif marker:
                out.append(ListItem(min(t.depth.at(start) or 0, 8), marker, inl))
            else:
                out.append(Para(inl))
        lo = bisect_left(t.attachments, (start,))
        hi = bisect_right(t.attachments, (end, float("inf")))
        for p, aid in t.attachments[lo:hi]:
            got = self._attachment(aid)
            if got and t.deleted.at(p):
                for b in got:
                    b.hidden = b.hidden or "deleted"
            out.extend(got)
        return out

    def _attachment(self, aid) -> list:
        """The blocks of attachment `aid`: a TSWP.DrawableAttachmentArchive (2003) gives its drawable (field 1) to
        `attach`. Other attachments (page numbers, footnote marks) hold no drawable. One that cannot be read loses its
        drawable, counted `object`."""
        if self.store.objects.get(aid, (None,))[0] != ATTACHMENT:
            return []
        drawable = None
        with self.ctx.guard("object"):
            drawable = ref(self.store.get(aid, ATTACHMENT), 1)
        return list(self.attach(drawable)) if drawable is not None else []
