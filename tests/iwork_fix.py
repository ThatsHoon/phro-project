"""In-memory builders for iWork fixtures: protobuf messages, Snappy blocks, IWA chunks, archives and zip containers."""
import struct
import zlib


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb(*pairs) -> bytes:
    """A protobuf message from (field, value) pairs: int is wire 0, bytes or str wire 2, float wire 1."""
    out = bytearray()
    for field, value in pairs:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            out += _varint(field << 3) + _varint(value)
        elif isinstance(value, float):
            out += _varint(field << 3 | 1) + struct.pack("<d", value)
        else:
            if isinstance(value, str):
                value = value.encode("utf-8")
            out += _varint(field << 3 | 2) + _varint(len(value)) + bytes(value)
    return bytes(out)


def pb_ref(id: int) -> bytes:
    """A TSP.Reference: field 1 is the target id."""
    return pb((1, id))


def snappy_literal(data: bytes) -> bytes:
    """A raw Snappy block holding data as one literal (declared length first)."""
    n = len(data)
    head = _varint(n)
    if n == 0:
        return head
    ln = n - 1
    if ln < 60:
        tag = bytes([ln << 2])
    else:
        raw = ln.to_bytes((ln.bit_length() + 7) // 8, "little")
        tag = bytes([(59 + len(raw)) << 2]) + raw
    return head + tag + data


def snappy_copy(offset: int, length: int, width: int = 2) -> bytes:
    """A hand-made copy tag: width 1 (copy-1, length 4-11, offset under 2048), 2 or 4."""
    if width == 1:
        return bytes([1 | ((length - 4) << 2) | ((offset >> 8) << 5), offset & 0xFF])
    if width == 2:
        return bytes([2 | ((length - 1) << 2)]) + offset.to_bytes(2, "little")
    return bytes([3 | ((length - 1) << 2)]) + offset.to_bytes(4, "little")


def chunk(block: bytes, type: int = 0) -> bytes:
    """One IWA chunk: type byte, three length bytes, then the block."""
    return bytes([type]) + len(block).to_bytes(3, "little") + block


def archive(id: int, type: int, payload: bytes, *more: bytes, merge: bool = False, removes: bool = False) -> bytes:
    """A TSP archive: varint length, ArchiveInfo (id, one MessageInfo per payload), then the payloads. `merge` sets
    ArchiveInfo 3 should_merge (and MessageInfo 8 on every message); `removes` gives every later message a
    MessageInfo 10 fields_to_remove."""
    payloads = (payload,) + more
    infos = []
    for i, p in enumerate(payloads):
        pairs = [(1, type), (3, len(p))]
        if merge:
            pairs.append((8, 1))
        if removes and i:
            pairs.append((10, pb((1, 1))))
        infos.append((2, pb(*pairs)))
    info = pb((1, id), *infos, *([(3, 1)] if merge else []))
    return _varint(len(info)) + info + b"".join(payloads)


def iwa_member(*archives) -> bytes:
    """An .iwa member: the archives joined and stored as one Snappy chunk."""
    return chunk(snappy_literal(b"".join(archives)))


def metadata_iwa(datas: list[tuple[int, str]]) -> bytes:
    """An Index/Metadata.iwa member: one TSP.PackageMetadata (11006) whose field 4 lists a TSP.DataInfo
    {1 id, 3 preferred file name, 4 file name} per (id, name)."""
    infos = [(4, pb((1, i), (3, name), (4, name))) for i, name in datas]
    return iwa_member(archive(2, 11006, pb(*infos)))


def iwork_zip(members: dict[str, bytes], methods: dict[str, int] = {}) -> bytes:
    """A zip written by hand: each member stored, or raw-deflated for method 8. Any other method number is written
    as given over the stored bytes, so a method Python cannot inflate (iWork '09's 25452) is still in the directory."""
    out = bytearray()
    central = bytearray()
    for name, data in members.items():
        method = methods.get(name, 0)
        if method == 8:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = c.compress(data) + c.flush()
        else:
            body = data
        crc = zlib.crc32(data)
        raw = name.encode("utf-8")
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method, 0, 0x21, crc, len(body), len(data), len(raw), 0)
        out += raw + body
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, method, 0, 0x21, crc, len(body),
                               len(data), len(raw), 0, 0, 0, 0, 0, offset) + raw
    n = len(members)
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, len(central), len(out), 0)
    return bytes(out + central + eocd)


# -- Pages text storages and documents (Task 12). Run tables take (character index, object id or None) pairs; the
# indexes are written as given (IWA counts them in UTF-16 code units).

def _table(entries) -> bytes:
    """A TSWP.ObjectAttributeTable: {1 entries {1 character_index, 2 TSP.Reference}}; a None ref ends the run."""
    return pb(*((1, pb((1, i), *([(2, pb_ref(r))] if r is not None else []))) for i, r in entries))


def storage(text, *, para_styles=(), char_styles=(), lists=(), links=(), footnotes=(), comments=(), deletions=(),
            attachments=(), sections=(), insertions=()) -> bytes:
    """A TSWP.StorageArchive (2001) payload. `text` is a string or a list of them (field 3 repeated). `lists` takes
    (index, depth, list style id) triples, for field 6 (ParaData {1 index, 2 first, 3 second}) and field 7."""
    parts = [(3, t) for t in ([text] if isinstance(text, str) else text)]
    for field, entries in ((5, para_styles), (8, char_styles), (9, attachments), (11, links), (16, footnotes),
                           (17, sections), (21, insertions), (22, deletions), (23, comments)):
        if entries:
            parts.append((field, _table(entries)))
    if lists:
        parts.append((6, pb(*((1, pb((1, i), (2, d), (3, 0))) for i, d, _ in lists))))
        parts.append((7, _table((i, s) for i, _, s in lists)))
    return pb(*parts)


def style(id, name, *, parent=None, bold=None, type=2022) -> bytes:
    """A style archive: TSWP.ParagraphStyleArchive (2022) by default, or `type` 2021 (character) or 2023 (list).
    Field 1 is its TSS.StyleArchive super {1 name, 3 parent}; `bold` (True or False) sets character property 1."""
    sup = pb(*([(1, name)] if name else []), *([(3, pb_ref(parent))] if parent is not None else []))
    return archive(id, type, pb((1, sup), *([(11, pb((1, int(bold))))] if bold is not None else [])))


def list_style(id, label_types, *, parent=None) -> bytes:
    """A TSWP.ListStyleArchive (2023): field 11 one label type per depth (0 none, 2 string, 3 number)."""
    sup = pb(*([(3, pb_ref(parent))] if parent is not None else []))
    return archive(id, 2023, pb((1, sup), *((11, t) for t in label_types)))


def link(id, url) -> bytes:
    """A TSWP.HyperlinkFieldArchive (2032): field 2 the url."""
    return archive(id, 2032, pb((2, url)))


def change(id, kind) -> bytes:
    """A TSWP.ChangeArchive (2060): field 1 kind, 1 insertion, 2 deletion."""
    return archive(id, 2060, pb((1, kind)))


def footnote(id, storage_id) -> bytes:
    """A TSWP.FootnoteReferenceAttachmentArchive (2008): field 2 the note's storage."""
    return archive(id, 2008, pb((2, pb_ref(storage_id))))


def comment(id, text, *, author=None, replies=()) -> bytes:
    """A TSD.CommentStorageArchive (3056) {1 text, 3 author, 4 replies}; `author` is a TSK.AnnotationAuthorArchive
    (212) id."""
    return archive(id, 3056, pb((1, text), *([(3, pb_ref(author))] if author is not None else []),
                                *((4, pb_ref(r)) for r in replies)))


def highlight(id, comment_id) -> bytes:
    """A TSWP.HighlightArchive (2013): field 1 the comment storage."""
    return archive(id, 2013, pb((1, pb_ref(comment_id))))


def author(id, name) -> bytes:
    return archive(id, 212, pb((1, name)))


def attachment(id, drawable) -> bytes:
    """A TSWP.DrawableAttachmentArchive (2003): field 1 the drawable."""
    return archive(id, 2003, pb((1, pb_ref(drawable))))


def image(id, data_id, field=11) -> bytes:
    """A TSD.ImageArchive (3005) whose `field` (15, 13, 11 or 12) is a TSP.DataReference to `data_id`."""
    return archive(id, 3005, pb((field, pb_ref(data_id))))


def shape(id, storage_id, field=4) -> bytes:
    """A TSWP.ShapeInfoArchive (2011): field 4 owned_storage (2, the older storage field, with `field=2`)."""
    return archive(id, 2011, pb((1, b""), (field, pb_ref(storage_id))))


def group(id, children) -> bytes:
    """A TSD.GroupArchive (3008): field 2 the children."""
    return archive(id, 3008, pb(*((2, pb_ref(c)) for c in children)))


PAGES_ROOT, FLOATING, SECTION, TEMPLATE = 1, 5, 3, 4  # metadata_iwa takes id 2


def pages_doc(body_sid, *, floating=(), headers=(), footers=()) -> list[bytes]:
    """The archives of a Pages document: TP.DocumentArchive (10000, id 1) with 4 the body storage and 20 a container
    (id 5) listing `floating`; with headers or footers, a section (10011, id 3) whose 23, 24 and 25 name one
    HeadersAndFooters (10143, id 4) {1 headers, 2 footers}. The body storage needs `sections=[(0, SECTION)]`."""
    root = [(4, pb_ref(body_sid))]
    out = []
    if floating:
        root.append((20, pb_ref(FLOATING)))
        out.append(archive(FLOATING, 10015, pb(*((1, pb_ref(d)) for d in floating))))
    if headers or footers:
        out.append(archive(SECTION, 10011, pb((23, pb_ref(TEMPLATE)), (24, pb_ref(TEMPLATE)), (25, pb_ref(TEMPLATE)))))
        out.append(archive(TEMPLATE, 10143, pb(*((1, pb_ref(h)) for h in headers), *((2, pb_ref(f)) for f in footers))))
    return [archive(PAGES_ROOT, 10000, pb(*root))] + out
