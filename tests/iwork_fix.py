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


# -- Numbers tables and documents (Task 13)

V5_FLAG = {"decimal": 0x1, "double": 0x2, "seconds": 0x4, "string": 0x8, "rich": 0x10, "number_format": 0x2000,
           "currency_format": 0x4000, "date_format": 0x8000, "duration_format": 0x10000, "text_format": 0x20000,
           "bool_format": 0x40000, "comment": 0x80000}
# the format flag a cell type's format_id goes under, as Numbers writes it (numbers-parser `_from_storage`)
_FORMAT_FLAG = {2: "number_format", 10: "currency_format", 5: "date_format", 7: "duration_format", 6: "bool_format",
                3: "text_format", 9: "text_format"}
_V5_ORDER = ("decimal", "double", "seconds", "string", "rich", "number_format", "currency_format", "date_format",
             "duration_format", "text_format", "bool_format", "comment")


def fixed32(field: int, value: int) -> bytes:
    """One protobuf fixed32 field (wire 5)."""
    return _varint(field << 3 | 5) + struct.pack("<I", value)


def decimal128_bytes(value) -> bytes:
    """An IEEE 754-2008 decimal128 in the binary integer encoding: the coefficient in the low 113 bits, the exponent
    biased by 6176 above it, the sign in bit 127."""
    from decimal import Decimal
    sign, digits, exp = Decimal(str(value)).as_tuple()
    coeff = int("".join(map(str, digits)) or "0")
    n = (sign << 127) | ((exp + 6176) << 113) | coeff
    return n.to_bytes(16, "little")


def cell_v5(type, *, value=None, string_id=None, rich_id=None, format_id=None, comment_id=None, format_flag=None,
            raw_flags=None) -> bytes:
    """A version 5 packed cell: 0 version, 1 type, 8-11 flags, then the flagged values in numbers-parser's order.
    `value` is a decimal128 for a number (2) or currency (10), seconds for a date (5) and a double for a bool (6) or
    duration (7). `format_id` goes under the type's format flag, or under `format_flag` (a V5_FLAG name)."""
    vals = {}
    if value is not None:
        if type in (2, 10):
            vals["decimal"] = decimal128_bytes(value)
        elif type == 5:
            vals["seconds"] = struct.pack("<d", float(value))
        else:
            vals["double"] = struct.pack("<d", float(value))
    for name, v in (("string", string_id), ("rich", rich_id), ("comment", comment_id)):
        if v is not None:
            vals[name] = struct.pack("<I", v)
    if format_id is not None:
        vals[format_flag or _FORMAT_FLAG.get(type, "number_format")] = struct.pack("<I", format_id)
    flags = sum(V5_FLAG[k] for k in vals) if raw_flags is None else raw_flags
    body = b"".join(vals[k] for k in _V5_ORDER if k in vals)
    return bytes([5, type, 0, 0, 0, 0, 0, 0]) + struct.pack("<I", flags) + body


def cell_v4(type, *, value=None, string_id=None, ids: int = 1) -> bytes:
    """A version 4 packed cell as docling's cells.py reads it: 12 header bytes (0 version, 1 type, 4-7 a bitmask of
    `ids` bits), one 4-byte identifier a bit, an IEEE double before the last identifier for a numeric type, and a
    text cell's string key as the last identifier."""
    head = bytes([4, type, 0, 0]) + struct.pack("<I", (1 << ids) - 1) + b"\0" * 4
    slots = b"\0\0\0\0" * (ids - 1)
    if type == 3:
        return head + slots + struct.pack("<I", string_id or 0)
    return head + slots + struct.pack("<d", float(value or 0)) + b"\0\0\0\0"


def tile(rows: dict, *, wide=False, pre_bnc=False) -> bytes:
    """A TST.Tile (6002) payload: field 5 one TileRowInfo a row {1 tile_row_index, 2 cell_count, 6 buffer, 7 int16
    offsets (-1 empty), 8 wide offsets (x4)}. `pre_bnc` writes the buffer and offsets as fields 3 and 4 only. A row's
    list holds one cell (bytes) or None a column."""
    infos = []
    for index, cells in rows.items():
        buf, offs = bytearray(), []
        for c in cells:
            if c is None:
                offs.append(-1)
                continue
            if wide:
                buf += b"\0" * (-len(buf) % 4)
            offs.append(len(buf) // 4 if wide else len(buf))
            buf += c
        packed = struct.pack(f"<{len(offs)}h", *offs)
        pairs = [(1, index), (2, sum(c is not None for c in cells))]
        if pre_bnc:
            pairs += [(3, bytes(buf)), (4, packed)]
        else:
            pairs += [(3, b""), (4, b""), (6, bytes(buf)), (7, packed)]
            if wide:
                pairs.append((8, 1))
        infos.append((5, pb(*pairs)))
    return pb(*infos)


def fmt(type, *, decimals=None, currency=None, thousands=None, pattern=None) -> bytes:
    """A TSK.FormatStructArchive {1 type, 2 decimal places, 3 currency code, 5 thousands, 14 ICU date pattern}."""
    pairs = [(1, type)]
    for field, v in ((2, decimals), (3, currency), (5, thousands), (14, pattern)):
        if v is not None:
            pairs.append((field, v))
    return pb(*pairs)


def data_list(id, entries, *, list_type=1, segments=()) -> bytes:
    """A TST.TableDataList (6005) {1 list type, 2 next id, 3 entries, 4 segments}; `entries` are ListEntry payloads."""
    return archive(id, 6005, pb((1, list_type), (2, 1), *((3, e) for e in entries),
                                *((4, pb_ref(s)) for s in segments)))


def segment(id, entries) -> bytes:
    """A TST.TableDataListSegment (6011) {1 list type, 3 entries}."""
    return archive(id, 6011, pb((1, 1), *((3, e) for e in entries)))


def string_entry(key, text) -> bytes:
    """A TableDataList.ListEntry {1 key, 2 refcount, 3 string}."""
    return pb((1, key), (2, 1), (3, text))


def table(name, rows, cols, *, tiles, strings=(), rich=(), merges=(), hidden_rows=(), hidden_cols=(), comments=(),
          formats=(), id=600, tile_size=None, info_type=6000, hiding=1, string_segments=(),
          string_list=None) -> list[bytes]:
    """The archives of one table: TST.TableInfoArchive (`info_type`, id `id`) -> model (6001, id+1) {4 data store,
    6 rows, 7 columns, 8 name}. The data store embeds 3 TileStorage {1 [{1 tileid, 2 tile}], 2 tile size} and
    names 4 strings ((key, text); `string_segments` are the ids of 6011 segments the caller adds; `string_list` names
    a list the caller adds instead), 17 rich text
    ((key, storage id) through 6218), 19 comments ((key, 3056 id)), 22 formats ((key, `fmt` bytes)), 13 the merges
    (6144; (row, col, rows, cols)) and 1/2 the row and column header buckets (6006), whose indexes get hidingState
    `hiding`. `tiles` maps tileid -> `tile` bytes, object id+20+i in tileid order. With info_type 6007 (a Pages
    table) the info nests the 6000 message in field 1. Value-list objects take ids id+2 to id+8, and the rich text
    payloads 10**6 + id*1000 on."""
    out, ds, entries = [], [], []
    for i, (tid, payload) in enumerate(sorted(tiles.items())):
        out.append(archive(id + 20 + i, 6002, payload))
        entries.append((1, pb((1, tid), (2, pb_ref(id + 20 + i)))))
    ds.append((3, pb(*entries, *([(2, tile_size)] if tile_size is not None else []))))
    if string_list is not None:  # a list object the caller adds, perhaps shared with another table
        ds.append((4, pb_ref(string_list)))
    elif strings or string_segments:
        out.append(data_list(id + 2, [string_entry(k, t) for k, t in strings], segments=string_segments))
        ds.append((4, pb_ref(id + 2)))
    if rich:
        rich_entries = []
        for i, (k, sid) in enumerate(rich):
            pid = 10**6 + id * 1000 + i
            out.append(archive(pid, 6218, pb((1, pb_ref(sid)))))
            rich_entries.append(pb((1, k), (2, 1), (9, pb_ref(pid))))
        out.append(data_list(id + 3, rich_entries, list_type=8))
        ds.append((17, pb_ref(id + 3)))
    if comments:
        out.append(data_list(id + 4, [pb((1, k), (2, 1), (10, pb_ref(c))) for k, c in comments], list_type=10))
        ds.append((19, pb_ref(id + 4)))
    if formats:
        out.append(data_list(id + 5, [pb((1, k), (2, 1), (6, f)) for k, f in formats], list_type=2))
        ds.append((22, pb_ref(id + 5)))
    if merges:
        ranges = [pb((1, fixed32(1, c << 16 | r)), (2, fixed32(1, nc << 16 | nr))) for r, c, nr, nc in merges]
        out.append(archive(id + 6, 6144, pb(*((1, x) for x in ranges))))
        ds.append((13, pb_ref(id + 6)))

    def bucket(idx):
        return pb((1, 1), *((2, pb((1, i), (2, 20.0), (3, hiding), (4, 1))) for i in idx))

    out.append(archive(id + 7, 6006, bucket(hidden_rows)))
    out.append(archive(id + 8, 6006, bucket(hidden_cols)))
    ds[:0] = [(1, pb((1, 1), (2, pb_ref(id + 7)))), (2, pb_ref(id + 8))]
    out.append(archive(id + 1, 6001, pb((1, "t"), (4, pb(*ds)), (6, rows), (7, cols), (8, name))))
    info = pb((1, b""), (2, pb_ref(id + 1)))
    out.append(archive(id, info_type, pb((1, info)) if info_type == 6007 else info))
    return out


NUMBERS_ROOT = 1  # metadata_iwa takes id 2


def sticky(id, comment_id) -> bytes:
    """A TSWP.CommentInfoArchive (2014) placing a sticky note: field 2 the comment storage (3056)."""
    return archive(id, 2014, pb((1, b""), (2, pb_ref(comment_id))))


def numbers_doc(sheets) -> list[bytes]:
    """TN.DocumentArchive (1, id 1) field 1 the sheets; each (name, drawable ids) is a TN.SheetArchive (2) at id 3,
    4, ... {1 name, 2 drawables}."""
    out = [archive(NUMBERS_ROOT, 1, pb(*((1, pb_ref(3 + i)) for i in range(len(sheets)))))]
    for i, (name, drawables) in enumerate(sheets):
        out.append(archive(3 + i, 2, pb((1, name), *((2, pb_ref(d)) for d in drawables))))
    return out


# -- charts and Keynote documents (Task 14)

def chart(type, rows, cols, values, title=None, *, id=500, by_row=False, show_title=True) -> list[bytes]:
    """A TSCH.ChartDrawableArchive (5021, id `id`) whose extension 10000 is a ChartArchive {1 type, 5 series
    direction (1 by row, when `by_row`), 7 grid {1 row names, 2 column names, 3 rows {1 values {1 numeric}}}, 10 ->
    5023 at id+1}. `values` holds one list a row, None for a value with no number; with a `title` the non-style
    archive's 10000 {21 show title, 23 title} holds it."""
    grid_rows = [(3, pb(*((1, pb(*([(1, float(v))] if v is not None else []))) for v in row))) for row in values]
    grid = pb(*((1, n) for n in rows), *((2, n) for n in cols), *grid_rows)
    pairs = [(1, type), *([(5, 1)] if by_row else []), (7, grid)]
    out = []
    if title is not None:
        pairs.append((10, pb_ref(id + 1)))
        out.append(archive(id + 1, 5023, pb((10000, pb((21, int(show_title)), (23, title))))))
    return [archive(id, 5021, pb((1, b""), (10000, pb(*pairs))))] + out


KEYNOTE_ROOT, SHOW, THEME = 1, 3, 4  # metadata_iwa takes id 2


def slide_ids(i: int) -> dict:
    """The object ids `keynote_doc` gives slide i: node, slide, title and body placeholders and their storages, the
    note and its storage, and a template slide with its title placeholder and storage."""
    b = 100 + 20 * i
    return dict(node=b, slide=b + 1, title=b + 2, title_text=b + 3, body=b + 4, body_text=b + 5, note=b + 6,
                note_text=b + 7, template=b + 10, template_title=b + 11, template_text=b + 12)


def placeholder(id, storage_id) -> bytes:
    """A KN.PlaceholderArchive (7): field 1 its TSWP.ShapeInfoArchive super, whose 4 is the owned storage."""
    return archive(id, 7, pb((1, pb((1, b""), (4, pb_ref(storage_id))))))


def keynote_doc(slides: list[dict], tree=None) -> list[bytes]:
    """A Keynote document: KN.DocumentArchive (1, id 1) {2 show} -> KN.ShowArchive (2, id 3) {2 theme, 3 slide tree
    {2 nodes}} and a theme (id 4) {2 templates}. Slide i (ids from `slide_ids`) is a KN.SlideNodeArchive (4) {1
    children, 2 slide, 4 isSkipped} -> KN.SlideArchive (5) {5 title, 6 body, 7 owned drawables, 17 template slide,
    27 note, 42 drawables_z_order}. Keys: title, body, notes (text; each a placeholder or note with its storage),
    drawables and z_order (ids the caller adds), skipped, children (slide indexes), template (text of a template
    slide only the theme lists). `tree` lists the slide indexes in the slide tree, all by default."""
    tree = range(len(slides)) if tree is None else tree
    out = [archive(KEYNOTE_ROOT, 1, pb((2, pb_ref(SHOW)))),
           archive(SHOW, 2, pb((2, pb_ref(THEME)), (3, pb(*((2, pb_ref(slide_ids(i)["node"])) for i in tree)))))]
    templates = []
    for i, s in enumerate(slides):
        ids = slide_ids(i)
        out.append(archive(ids["node"], 4, pb(*((1, pb_ref(slide_ids(c)["node"])) for c in s.get("children", ())),
                                              (2, pb_ref(ids["slide"])), (4, int(bool(s.get("skipped")))))))
        fields = []
        if "title" in s:
            fields.append((5, pb_ref(ids["title"])))
            out += [placeholder(ids["title"], ids["title_text"]), archive(ids["title_text"], 2001, storage(s["title"]))]
        if "body" in s:
            fields.append((6, pb_ref(ids["body"])))
            out += [placeholder(ids["body"], ids["body_text"]), archive(ids["body_text"], 2001, storage(s["body"]))]
        # owned drawables repeat the placeholders, as Keynote writes them
        owned = [ids[k] for k in ("title", "body") if k in s] + list(s.get("drawables", ()))
        fields += [(7, pb_ref(d)) for d in owned]
        if "template" in s:
            fields.append((17, pb_ref(ids["template"])))
            templates.append(ids["template"])
            out += [archive(ids["template"], 5, pb((5, pb_ref(ids["template_title"])))),
                    placeholder(ids["template_title"], ids["template_text"]),
                    archive(ids["template_text"], 2001, storage(s["template"]))]
        if "notes" in s:
            fields.append((27, pb_ref(ids["note"])))
            out += [archive(ids["note"], 15, pb((1, pb_ref(ids["note_text"])))),
                    archive(ids["note_text"], 2001, storage(s["notes"]))]
        fields += [(42, pb_ref(d)) for d in s.get("z_order", ())]
        out.append(archive(ids["slide"], 5, pb(*fields)))
    out.append(archive(THEME, 3, pb(*((2, pb_ref(t)) for t in templates))))
    return out
