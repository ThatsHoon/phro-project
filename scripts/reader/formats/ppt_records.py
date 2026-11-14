"""ppt record types and the persist chain: the persist id -> offset map of a PowerPoint Document stream.

[MS-PPT] knowledge only; the chain walk follows dochan office_binary/ppt_structure.py (the edit chain, its budgets and
its cycle guard) and anydoc ppt/mod.rs locate_document (the newest edit's entry for an id is never replaced).

The CurrentUserAtom is read at stream offsets (headerToken +12, offsetToCurrentEdit +16); a UserEditAtom's fields are
body offsets (lastEdit 8, persistDirectoryOffset 12, docPersistIdRef 16, encryptSessionPersistIdRef 28). Every offset
read from a file is checked against the stream, the chain is walked once per edit and at most MAX_EDITS edits, and a
directory entry that passes its atom is dropped and counted as a `record`.
"""
import struct

from .. import limits, officeart

DOCUMENT_CONTAINER, SLIDE_LIST_WITH_TEXT, SLIDE_PERSIST_ATOM = 0x03E8, 0x0FF0, 0x03F3
SLIDE_CONTAINER, NOTES_CONTAINER, SLIDE_ATOM, NOTES_ATOM = 0x03EE, 0x03F0, 0x03EF, 0x03F1
SLIDE_SHOW_SLIDE_INFO_ATOM = 0x03F9
TEXT_HEADER_ATOM, TEXT_CHARS_ATOM, TEXT_BYTES_ATOM, STYLE_TEXT_PROP_ATOM = 0x0F9F, 0x0FA0, 0x0FA8, 0x0FA1
OUTLINE_TEXT_REF_ATOM, PP_DRAWING, PP_DRAWING_GROUP, OE_PLACEHOLDER_ATOM = 0x0F9E, 0x040C, 0x040B, 0x0BC3
EX_OBJ_LIST, EX_EMBED, EX_OLE_OBJ_ATOM, EX_OLE_OBJ_STG, EX_OBJ_REF_ATOM = 0x0409, 0x0FCC, 0x0FC3, 0x1011, 0x0BC1
CRYPT_SESSION10_CONTAINER = 0x2F14
USER_EDIT_ATOM, PERSIST_DIRECTORY_ATOM, CURRENT_USER_ATOM = 0x0FF5, 0x1772, 0x0FF6
MAIN_MASTER = 0x03F8

MAX_EDITS = 10_000
ENCRYPTED_TOKEN = 0xF3D1C4DF


def encrypted(current_user: bytes | None) -> bool:
    """Whether the CurrentUserAtom headerToken says the streams are encrypted."""
    return bool(current_user and len(current_user) >= 16
                and struct.unpack_from("<I", current_user, 12)[0] == ENCRYPTED_TOKEN)


class Persist:
    """The persistent objects of a PowerPoint Document stream: `objects` maps a persist id to the offset of its record
    (the newest edit's entry wins), `doc_ref` is the DocumentContainer's id, `crypt_ref` the CryptSession10Container's
    (None when the newest edit names none), `encrypted` whether the CurrentUserAtom headerToken says the streams are
    encrypted. Raises ReadError("damaged") only when no UserEditAtom can be found. A doc_ref the directories do not
    list is kept as read: obj(doc_ref) is then None and the caller decides what a missing document container means.

    The directory work budget (BIN_RECORDS entries, repeated ids counted) is spent across all the edits together, not
    per edit: a file with very many edits that each re-list the same objects stops at the budget, the oldest
    directories are not read and the stop is counted under `record`. A directory entry that runs past its atom ends
    that directory there (the entries before it are kept, none after it can be trusted) and the older edits are still
    read."""

    def __init__(self, doc: bytes, current_user: bytes | None, ctx):
        self.doc, self.ctx = doc, ctx
        self.objects: dict[int, int] = {}
        self.encrypted = encrypted(current_user)
        first = self._current_edit(current_user)
        if first is None:
            first = self._last_edit()
        if first is None:
            raise limits.ReadError("damaged", "no persist directory")
        self.work = limits.BIN_RECORDS  # directory entries all edits may read, repeated ids counted
        self.doc_ref, self.crypt_ref = self._walk(first)

    def _edit(self, off: int) -> tuple[int, int] | None:
        """The UserEditAtom at `off` as (body offset, body size); None when there is none that fits."""
        head = officeart.header(self.doc, off)
        if head is None or head[2] != USER_EDIT_ATOM or head[3] < 28 or head[3] > len(self.doc) - off - 8:
            return None
        return off + 8, head[3]

    def _current_edit(self, cu) -> int | None:
        """The offset the CurrentUserAtom gives for the current edit; None when the atom or the edit is unusable."""
        if not cu or len(cu) < 20:
            return None
        head = officeart.header(cu, 0)
        if head is None or head[2] != CURRENT_USER_ATOM:
            return None
        off = struct.unpack_from("<I", cu, 16)[0]
        return off if self._edit(off) else None

    def _last_edit(self) -> int | None:
        """The offset of the last UserEditAtom among the stream's top-level records."""
        found = None
        for _, _, typ, body, size in officeart.records(self.doc):
            if typ == USER_EDIT_ATOM and size >= 28:
                found = body - 8
        return found

    def _walk(self, off: int) -> tuple[int | None, int | None]:
        doc_ref = crypt_ref = None
        seen, dirs = set(), set()
        for _ in range(MAX_EDITS):
            if off in seen:  # a lastEdit that points back at an edit already walked ends the chain
                self.ctx.skipped["record"] += 1
                break
            seen.add(off)
            edit = self._edit(off)
            if edit is None:
                self.ctx.skipped["record"] += 1
                break
            body, size = edit
            last, directory, ref = struct.unpack_from("<III", self.doc, body + 8)
            if doc_ref is None:
                doc_ref = ref
                if size >= 32:
                    crypt_ref = struct.unpack_from("<I", self.doc, body + 28)[0] or None
            if directory not in dirs:
                dirs.add(directory)
                if not self._directory(directory):
                    break
            if not last:
                break
            off = last
        else:
            self.ctx.skipped["record"] += 1
        return doc_ref, crypt_ref

    def _directory(self, off: int) -> bool:
        """Merge the directory at `off` into `objects`; False when the walk must stop (budget spent)."""
        head = officeart.header(self.doc, off)
        if head is None or head[2] != PERSIST_DIRECTORY_ATOM or head[3] > len(self.doc) - off - 8:
            self.ctx.skipped["record"] += 1
            return True
        pos, end = off + 8, off + 8 + head[3]
        while pos + 4 <= end:
            entry = struct.unpack_from("<I", self.doc, pos)[0]
            start, count = entry & 0xFFFFF, entry >> 20
            pos += 4
            if count * 4 > end - pos:
                self.ctx.skipped["record"] += 1
                return True
            self.work -= count + 1  # work, not new ids: edits repeating the same ids must not escape the budget
            if self.work < 0:
                self.ctx.skipped["record"] += 1
                return False
            for k, value in enumerate(struct.unpack_from(f"<{count}I", self.doc, pos)):
                self.objects.setdefault(start + k, value)
            pos += count * 4
        return True

    def obj(self, pid: int) -> tuple[int, int, int, int] | None:
        """The record persist id `pid` names, as (type, instance, body offset, body size); None when the id is
        unknown or its record does not fit the stream."""
        off = self.objects.get(pid)
        head = officeart.header(self.doc, off) if off is not None else None
        if head is None or head[3] > len(self.doc) - off - 8:
            return None
        return head[2], head[1], off + 8, head[3]


class Unchained:
    """The stand-in for a Persist when a clear stream has no usable chain: no persist id resolves, so the slides come
    from a walk of the top-level records and anything reached only through a persist id (an embedded object's
    storage) is not read."""
    encrypted = False
    doc_ref = crypt_ref = None

    def __init__(self, doc: bytes):
        self.doc = doc
        self.objects: dict[int, int] = {}

    def obj(self, pid: int) -> None:
        return None
