"""Test helpers for the document reader tests."""
import hashlib
import io
import itertools
import struct
import tempfile
import zipfile
import zlib
from bisect import bisect_right
from pathlib import Path


def make_zip(path, members, stored=("mimetype",)):
    """Write a zip from {name: str | bytes}; names in `stored` go first, uncompressed. With path None nothing is
    written and the zip's bytes are returned."""
    target = io.BytesIO() if path is None else Path(path)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        names = [n for n in stored if n in members] + [n for n in members if n not in stored]
        for name in names:
            data = members[name]
            if isinstance(data, str):
                data = data.encode("utf-8")
            method = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return target.getvalue() if path is None else target


def bad_name_zip(central=0x800, local=0x800) -> bytes:
    """The bytes of a stored zip of one member whose name, 0xFF then ".xml", is not UTF-8: `central` and `local`
    are the general-purpose flags of its central-directory entry and of its local header (0x800 marks a UTF-8
    name)."""
    name, body = b"\xff.xml", b"x"
    crc, n = zlib.crc32(body), len(body)
    head = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, local, 0, 0, 0, crc, n, n, len(name), 0) + name + body
    entry = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, central, 0, 0, 0, crc, n, n, len(name),
                        0, 0, 0, 0, 0, 0) + name
    return head + entry + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(entry), len(head), 0)


def make_cfb(path, streams, sector_size=512):
    """Write a compound file (see cfb_bytes) to `path`; returns the same info dict."""
    blob, info = cfb_bytes(streams, sector_size)
    with open(path, "wb") as f:
        f.write(blob)
    return info


def cfb_bytes(streams, sector_size=512):
    """A compound file (v3 for 512-byte sectors, v4 for 4096) from {"Storage/Name": bytes}.

    Storages come from the "/" parts; siblings are chained through right links. Streams under 4096 bytes go in
    the mini stream. Returns (bytes, {"fat": [sid], "dir": [sid], "chains": {name: [sid]}, "mini": {name: [mini sid]}}),
    so a test can patch bytes at (sid + 1) * sector_size.
    """
    ss = sector_size
    assert ss in (512, 4096)
    END, FREE, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    entry = "<64sHBBIII16sIQQIQ"  # name, name bytes, type, color, left, right, child, clsid, state, 2 times, start, size
    # tree: node = [name, type, children, data, id]
    root = ["Root Entry", 5, [], None, 0]
    nodes = [root]
    for full, data in streams.items():
        parent, parts = root, full.split("/")
        for part in parts[:-1]:
            nxt = next((c for c in parent[2] if c[0] == part and c[1] == 1), None)
            if nxt is None:
                nxt = [part, 1, [], None, len(nodes)]
                nodes.append(nxt)
                parent[2].append(nxt)
            parent = nxt
        leaf = [parts[-1], 2, [], bytes(data), len(nodes)]
        nodes.append(leaf)
        parent[2].append(leaf)
    path_of, right = {}, {}

    def walk(node, prefix):
        for i, c in enumerate(node[2]):
            right[c[4]] = node[2][i + 1][4] if i + 1 < len(node[2]) else FREE
            if c[1] == 2:
                path_of[c[4]] = prefix + c[0]
            else:
                walk(c, prefix + c[0] + "/")
    walk(root, "")
    # sector layout: regular streams, mini stream, mini FAT, directory, FAT
    per = ss // 4
    sectors, fat = [], []
    chains, mini_of, start, size_of = {}, {}, {}, {}

    def add(data):
        first = len(sectors)
        n = max(1, -(-len(data) // ss))
        for i in range(n):
            sectors.append(data[i * ss:(i + 1) * ss].ljust(ss, b"\0"))
            fat.append(first + i + 1 if i + 1 < n else END)
        return list(range(first, first + n))
    mini, minifat = bytearray(), []
    for node in nodes[1:]:
        if node[1] != 2:
            continue
        data, nid = node[3], node[4]
        size_of[nid] = len(data)
        if len(data) == 0:
            start[nid] = END
        elif len(data) >= 4096:
            chains[path_of[nid]] = add(data)
            start[nid] = chains[path_of[nid]][0]
        else:
            n = -(-len(data) // 64)
            first = len(mini) // 64
            ids = list(range(first, first + n))
            mini_of[path_of[nid]] = ids
            start[nid] = first
            minifat.extend(i + 1 for i in ids[:-1])
            minifat.append(END)
            mini.extend(data.ljust(n * 64, b"\0"))
    root_start, root_size = END, 0
    if mini:
        mchain = add(bytes(mini))
        root_start, root_size = mchain[0], len(mini)
    mini_first, mini_count = END, 0
    if minifat:
        minifat += [FREE] * (-len(minifat) % per)
        mf = add(struct.pack("<%dI" % len(minifat), *minifat))
        mini_first, mini_count = mf[0], len(mf)
    dirblob = b""
    for node in nodes:
        nid = node[4]
        raw = node[0].encode("utf-16-le") + b"\0\0"
        assert len(raw) <= 64
        st, sz = (root_start, root_size) if nid == 0 else (start.get(nid, 0), size_of.get(nid, 0))
        child = node[2][0][4] if node[2] else FREE
        dirblob += struct.pack(entry, raw, len(raw), node[1], 1, FREE, right.get(nid, FREE), child,
                               b"\0" * 16, 0, 0, 0, st, sz)
    free_entry = struct.pack(entry, b"", 0, 0, 0, FREE, FREE, FREE, b"\0" * 16, 0, 0, 0, 0, 0)
    dirblob += free_entry * (-(-len(dirblob) // ss) * ss // 128 - len(dirblob) // 128)
    dirs = add(dirblob)
    nfat = 1
    while nfat * per < len(fat) + nfat:
        nfat += 1
    assert nfat <= 109, "make_cfb writes no DIFAT"
    fat_ids = list(range(len(sectors), len(sectors) + nfat))
    fat += [FATSECT] * nfat
    fat += [FREE] * (nfat * per - len(fat))
    for i in range(nfat):
        sectors.append(struct.pack("<%dI" % per, *fat[i * per:(i + 1) * per]))
    hdr = bytearray(ss)
    hdr[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 3 if ss == 512 else 4, 0xFFFE, 9 if ss == 512 else 12, 6)
    struct.pack_into("<IIIIIIIII", hdr, 0x28, len(dirs) if ss == 4096 else 0, nfat, dirs[0], 0, 4096,
                     mini_first, mini_count, END, 0)
    struct.pack_into("<109I", hdr, 0x4C, *(fat_ids + [FREE] * (109 - nfat)))
    return bytes(hdr) + b"".join(sectors), {"fat": fat_ids, "dir": dirs, "chains": chains, "mini": mini_of}


def hwp_record(tag, level, data):
    """One HWP record: u32 header (tag 10 bits, level 10 bits, size 12 bits), extended size from 0xFFF bytes."""
    data = bytes(data)
    if len(data) >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), len(data)) + data
    return struct.pack("<I", tag | (level << 10) | (len(data) << 20)) + data


_HWP_EXTENDED = (1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23)  # control characters that own a CTRL_HEADER


def hwp_para(text, *, shape=0, style=0, chars=((0, 0),), level=0, controls=(), ranges=()):
    """One HWP paragraph: PARA_HEADER at `level`, then PARA_TEXT and PARA_CHAR_SHAPE one level deeper.

    `text` is written as UTF-16LE units and ends with unit 13. Each U+FFFC takes the next (code, ctrl_id, children)
    of `controls` and becomes the eight-unit control character: code, the id, four units of padding, code. `ctrl_id`
    is big-endian ASCII such as b"tbl " (stored byte-reversed); bytes past its first four are the CTRL_HEADER's own
    fields after the id. For an extended code the builder then appends that CTRL_HEADER at level + 1 and `children`
    as given (records the caller wrote at level + 2); an inline code such as 4 (field end) or 9 (tab) has none.
    `chars` are the PARA_CHAR_SHAPE (position, char shape id) pairs. `ranges` are PARA_RANGE_TAG (start, end, tag)
    triples in units, written after PARA_CHAR_SHAPE; a tag's top byte is its kind (0x10 insert, 0x11 delete).
    """
    units, ctrls = bytearray(), []
    pending = iter(controls)
    for ch in text:
        if ch != "\ufffc":
            units += ch.encode("utf-16-le")
            continue
        code, cid, children = next(pending)
        raw = bytes(cid[:4]).ljust(4, b"\0")[::-1]
        units += struct.pack("<H", code) + raw + bytes(8) + struct.pack("<H", code)
        if code in _HWP_EXTENDED:
            ctrls += [hwp_record(71, level + 1, raw + bytes(cid[4:])), bytes(children)]
    units += struct.pack("<H", 13)
    header = struct.pack("<IIHBBHHHI", len(units) // 2, 0, shape, style, 0, len(chars), len(ranges), 0, 0)
    pairs = b"".join(struct.pack("<II", pos, sid) for pos, sid in chars)
    tags = hwp_record(70, level + 1, b"".join(struct.pack("<III", *r) for r in ranges)) if ranges else b""
    return (hwp_record(66, level, header) + hwp_record(67, level + 1, bytes(units))
            + hwp_record(68, level + 1, pairs) + tags + b"".join(ctrls))


def _hwp_caption(caption, level):
    """A caption list: its LIST_HEADER and one paragraph, both at `level`."""
    return [hwp_record(72, level, struct.pack("<HIH", 1, 0, 0)), hwp_para(caption, level=level)]


def hwp_table(rows, *, level=2, caption=None):
    """A table for hwp_para's `controls`: (11, b"tbl ", records), the records written at `level` (the paragraph's
    level + 2): an optional caption list, TABLE, then a LIST_HEADER per cell with the cell's paragraphs beside it.

    `rows` lists rows of cells. A cell is text (one paragraph), a list of texts (one paragraph each), bytes (paragraph
    records already written at `level`) or a tuple (cell, colspan, rowspan). A cell's column is the sum of the
    colspans before it in its row.
    """
    out = _hwp_caption(caption, level) if caption is not None else []
    cells, ncols = [], 0
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            body, cs, rs = cell if isinstance(cell, tuple) else (cell, 1, 1)
            if isinstance(body, (bytes, bytearray)):
                n, paras = 1, bytes(body)
            else:
                texts = [body] if isinstance(body, str) else list(body)
                n, paras = len(texts), b"".join(hwp_para(t, level=level) for t in texts)
            # paragraph count, attr, width ref, then col, row, colspan, rowspan, size, margins, border fill
            cells += [hwp_record(72, level, struct.pack("<HIH4H", n, 0, 0, c, r, cs, rs) + bytes(18)), paras]
            c += cs
        ncols = max(ncols, c)
    # attr, rows, cols, spacing, margins, a row size per row, border fill
    out.append(hwp_record(77, level, struct.pack("<I2H", 0, len(rows), ncols) + bytes(10 + 2 * len(rows) + 2)))
    return 11, b"tbl ", b"".join(out + cells)


def hwp_picture(bin_id, alt="", *, level=2, caption=None):
    """A picture for hwp_para's `controls`: (11, b"gso " + its object attributes, records at `level`).

    The CTRL_HEADER holds the shape comment `alt` at offset 44 (u16 length, UTF-16LE); an optional caption list comes
    first, then SHAPE_COMPONENT, with SHAPE_PICTURE one level deeper holding the 1-based `bin_id` at offset 71.
    """
    cid = b"gso " + bytes(40) + struct.pack("<H", len(alt)) + alt.encode("utf-16-le")
    out = _hwp_caption(caption, level) if caption is not None else []
    out += [hwp_record(76, level, b"cip$cip$" + bytes(32)),
            hwp_record(85, level + 1, bytes(71) + struct.pack("<H", bin_id) + bytes(9))]
    return 11, cid, b"".join(out)


_HWP_REL = {"PAPER": 0, "PAGE": 1 << 3 | 1 << 8, "PARA": 2 << 3 | 3 << 8}  # vertRelTo bits 3-4, horzRelTo bits 8-9


def hwp_shape(kind, inst_id, box, text="", line=None, *, level=2, member=False, members=(), rel="PAPER",
              arrows=(0, 0), fill=1, head_id=0, component=None):
    """A drawing object: for hwp_para's `controls` (11, b"gso " + its CTRL_HEADER fields, records at `level`), or with
    `member` a group member's records at `level` alone.

    `kind` is the control id: "$rec", "$ell", "$col", "$lin" or "$con" (stored byte-reversed). `box` (x, y, w, h) in
    HWPUNIT: x, y are a gso's CTRL_HEADER offsets relative to `rel` (PAPER, PAGE or PARA) and a member's component
    offset (in its group's frame); w, h are the component's current size. The CTRL_HEADER holds `head_id` at offset 36.
    The SHAPE_COMPONENT (kordoc readComponent and drawingStyle) holds the id (twice for a gso, once for a member), the
    offset, group level, version, original and current size, flip, angle, centre, no scale and rotation pairs, the
    translation matrix, then for a group its members' count and ids, for anything else the drawing style: line colour,
    width, attribute (`arrows`: head and tail styles at bits 10 and 16), outline, a fill brush (`fill` 1 solid, 0
    none), the shadow, the instance id `inst_id` and two alphas. `component` replaces that record's data. Its records,
    one level deeper: `text` as a text box (LIST_HEADER and one paragraph), then the shape record: RECTANGLE for
    $rec, LINE for $col from `line` ((x0, y0), (x1, y1), connect type, begin id, end id) and for $lin from `line`
    ((x0, y0), (x1, y1)), or `line` as raw bytes; a group's SHAPE_CONTAINER then its `members` (records written with
    member=True at level + 1).
    """
    x, y, w, h = box
    cid = kind.encode()[::-1]
    data = (cid if member else cid + cid) + struct.pack("<ii", *((x, y) if member else (0, 0)))
    data += struct.pack("<HH4IIhiiH", 0, 0, w, h, w, h, 0, 0, 0, 0, 0) + struct.pack("<6d", 1, 0, 0, 0, 1, 0)
    if kind == "$con":
        data += struct.pack("<H", len(members)) + bytes(4 * len(members)) + struct.pack("<I", inst_id)
    else:
        brush = struct.pack("<I3IIB", 1, 0xFFFFFF, 0, 0, 0, 0) if fill else struct.pack("<II", 0, 0)
        data += (struct.pack("<iiIB", 0, 1, arrows[0] << 10 | arrows[1] << 16, 0) + brush
                 + struct.pack("<IIiiIBB", 0, 0, 0, 0, inst_id, 0, 0))
    if component is not None:
        data = component
    out = [hwp_record(76, level, data)]
    if text:
        out += [hwp_record(72, level + 1, struct.pack("<HIH", 1, 0, 0)), hwp_para(text, level=level + 1)]
    if kind == "$rec":
        out.append(hwp_record(79, level + 1, bytes(33)))
    elif kind == "$col" and isinstance(line, tuple):
        (x0, y0), (x1, y1), ctype, begin, end = line
        out.append(hwp_record(78, level + 1, struct.pack("<4i6I", x0, y0, x1, y1, ctype, begin, 0, end, 0, 0)))
    elif kind == "$lin" and isinstance(line, tuple):
        (x0, y0), (x1, y1) = line
        out.append(hwp_record(78, level + 1, struct.pack("<4iH", x0, y0, x1, y1, 0)))
    elif line is not None:
        out.append(hwp_record(78, level + 1, bytes(line)))
    if kind == "$con":
        out += [hwp_record(86, level + 1, struct.pack("<H", len(members)))] + list(members)
    if member:
        return b"".join(out)
    attrs = struct.pack("<Iii2I", _HWP_REL[rel], y, x, w, h) + bytes(12) + struct.pack("<Ii", head_id, 0)
    return 11, b"gso " + attrs + struct.pack("<H", 0), b"".join(out)


def make_hwp(path, sections, doc_info=b"", bins=None, flags=1, extra=None):
    """Write an HWP 5.0.3.0 compound file: FileHeader, DocInfo, BodyText/Section<N> (raw-deflated when flags & 1),
    BinData/<name> and any extra streams as given."""

    def pack(raw):
        if not flags & 1:
            return bytes(raw)
        c = zlib.compressobj(wbits=-15)
        return c.compress(bytes(raw)) + c.flush()

    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, flags)
    streams = {"FileHeader": header.ljust(256, b"\0"), "DocInfo": pack(doc_info)}
    for i, body in enumerate(sections):
        streams[f"BodyText/Section{i}"] = pack(body)
    for name, data in (bins or {}).items():
        streams[f"BinData/{name}"] = bytes(data)
    streams.update(extra or {})
    path = Path(path)
    make_cfb(path, streams)
    return path


def make_dist_section(section: bytes, key: bytes, seed: int, compressed: bool = True) -> bytes:
    """A distribution document's ViewText/Section<N> stream, the inverse of hwp_records.view_text (tests only).

    DISTRIBUTE_DOC_DATA (tag 28) carries 256 bytes: `seed` as u32 LE at 0-3, then filler XORed with the MSVC rand
    stream seeded with it (key = rand() & 0xFF, then n = (rand() & 0xF) + 1 bytes), with the 16-byte AES `key` at
    4 + (seed & 0xF). After it comes `section` (raw-deflated when `compressed`), zero-padded to whole blocks and
    encrypted with AES-128-ECB under `key`. Needs the reader's scripts folder on sys.path, as the tests set it.
    """
    from reader.containers.crypto import MsvcRand, aes128_ecb_encrypt
    rand = MsvcRand(seed)
    mask = bytearray()
    while len(mask) < 256:
        fill = rand() & 0xFF
        mask += bytes([fill]) * ((rand() & 0xF) + 1)
    # filler that is not zero, so a key read from the wrong offset cannot decrypt
    plain = bytearray(struct.pack("<I", seed & 0xFFFFFFFF) + bytes((i * 37 + 11) & 0xFF for i in range(4, 256)))
    at = 4 + (seed & 0xF)
    plain[at:at + 16] = key
    payload = plain[:4] + bytes(b ^ m for b, m in zip(plain[4:], mask[4:256]))
    body = bytes(section)
    if compressed:
        c = zlib.compressobj(wbits=-15)
        body = c.compress(body) + c.flush()
    body += bytes(-len(body) % 16)
    return hwp_record(28, 0, payload) + aes128_ecb_encrypt(bytes(key), body)


DOC_EXTRA = 0x400  # where make_doc puts word_extra in WordDocument; the piece text follows it
DOC_STORIES = ("text", "footnote", "header", "macro", "annotation", "endnote", "textbox", "header_textbox")


def _doc_pcds(clx):
    """(cp0, cp1, fc, bytes a CP) of each piece of a Clx: Prc records skipped, then the Pcdt's PlcPcd."""
    pos = 0
    while clx[pos] == 1:
        pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
    size = struct.unpack_from("<I", clx, pos + 1)[0]
    plc = clx[pos + 5:pos + 5 + size]
    n = (size - 4) // 12
    cps = struct.unpack_from("<%dI" % (n + 1), plc)
    out = []
    for i in range(n):
        raw = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        packed = bool(raw & 0x40000000)
        at = raw & 0x3FFFFFFF
        out.append((cps[i], cps[i + 1], at // 2 if packed else at, 1 if packed else 2))
    return out


def _doc_fkp(spans, pcds, papx, pn):
    """FKP pages and their PlcBte for (cp0, cp1, bytes) spans: each span is mapped to FCs through the pieces (split
    where a piece ends), the runs are sorted by FC and the gaps between them filled with runs without properties, so
    each page's runs are contiguous. A CHPX is cb u8 + grpprl; a PAPX (bytes: istd u16 + grpprl) is cb u8 with size
    2cb - 1 for an odd size, else 0 and cb' u8 with size 2cb'. Pages are numbered from `pn`."""
    starts = [p[0] for p in pcds]
    runs = []
    for c0, c1, data in spans:
        k = max(bisect_right(starts, c0) - 1, 0)
        while k < len(pcds) and pcds[k][0] < c1:
            p0, p1, f, w = pcds[k]
            a, b = max(c0, p0), min(c1, p1)
            if a < b:
                runs.append((f + (a - p0) * w, f + (b - p0) * w, bytes(data)))
            k += 1
    runs.sort(key=lambda r: r[0])
    filled = []
    for r in runs:
        if filled and filled[-1][1] < r[0]:
            filled.append((filled[-1][1], r[0], b""))
        filled.append(r)
    width = 13 if papx else 1

    def blob(data):
        if not data:
            return b""
        if not papx:
            return bytes([len(data)]) + data
        return bytes([(len(data) + 1) // 2]) + data if len(data) % 2 else bytes([0, len(data) // 2]) + data

    pages, fcs, pns, i = [], [], [], 0
    while i < len(filled):
        page, top, chosen = bytearray(512), 511, []
        while i < len(filled) and len(chosen) < 255:
            b = blob(filled[i][2])
            c = len(chosen) + 1
            off = (top - len(b)) & ~1 if b else 0
            if (c + 1) * 4 + c * width > (off if b else top):
                break
            if b:
                page[off:off + len(b)] = b
                top = off
            chosen.append((filled[i], off))
            i += 1
        assert chosen, "a run too large for one FKP page"
        c = len(chosen)
        for j, ((f0, _, _), off) in enumerate(chosen):
            struct.pack_into("<I", page, 4 * j, f0)
            page[(c + 1) * 4 + j * width] = off // 2
        struct.pack_into("<I", page, 4 * c, chosen[-1][0][1])
        page[511] = c
        fcs.append(chosen[0][0][0])
        pns.append(pn + len(pages))
        pages.append(bytes(page))
    if not pages:
        return b"", b""
    fcs.append(filled[-1][1])
    return b"".join(pages), struct.pack("<%dI" % (2 * len(pages) + 1), *fcs, *pns)


def doc_stsh(styles, base=18):
    """An STSH for make_doc's fc={1: ...}. `styles` lists the istd slots: None for an empty one, else (sti, stk,
    istd_base, name, papx, chpx), stk 1 a paragraph style (UpxPapx: its istd + papx, then UpxChpx: chpx) and 2 a
    character style (UpxChpx only); papx and chpx are grpprls. The Stshi is the 18-byte Stshif plus ftcBi;
    cbSTDBaseInFile is `base`: 10 (Word 97) or 18 (with StdfPost2000). Each UPX is padded to an even size."""

    def upx(data):
        return struct.pack("<H", len(data)) + data + bytes(len(data) % 2)

    stshi = struct.pack("<10H", len(styles), base, 1, 0x5B, 15, 0, 0, 0, 0, 0)
    out = bytearray(struct.pack("<H", len(stshi)) + stshi)
    for istd, s in enumerate(styles):
        if s is None:
            out += struct.pack("<H", 0)
            continue
        sti, stk, based, name, papx, chpx = s
        std = struct.pack("<5H", sti & 0xFFF, stk | based << 4, 2 if stk == 1 else 1, 0, 0).ljust(base, b"\0")
        std += struct.pack("<H", len(name)) + name.encode("utf-16-le") + b"\0\0"
        if stk == 1:
            std += upx(struct.pack("<H", istd) + bytes(papx))
        std += upx(bytes(chpx))
        out += struct.pack("<H", len(std)) + std
    return bytes(out)


def _doc_lvl(level):
    """One LVL from (nfc, text[, start[, papx, chpx]]): the 28-byte LVLF (iStartAt, nfc, rgbxchNums,
    cbGrpprlChpx at 24, cbGrpprlPapx at 25), grpprlPapx, grpprlChpx, then the number text xst (u16 count, UTF-16).
    "%N" (N 1-9) in text is a placeholder for level N's number: the unit N - 1, its 1-based offset in rgbxchNums."""
    nfc, text, *rest = level
    start = rest[0] if rest else 1
    papx, chpx = (bytes(rest[1]), bytes(rest[2])) if len(rest) > 2 else (b"", b"")
    units, nums, i = bytearray(), [], 0
    while i < len(text):
        if text[i] == "%" and text[i + 1:i + 2] in tuple("123456789"):
            units += struct.pack("<H", int(text[i + 1]) - 1)
            nums.append(len(units) // 2)
            i += 2
        else:
            units += text[i].encode("utf-16-le")
            i += 1
    lvlf = struct.pack("<iBB9sBiiBBBB", start, nfc, 0, bytes(nums[:9]), 0, 0, 0, len(chpx), len(papx), 0, 0)
    return lvlf + papx + chpx + struct.pack("<H", len(units) // 2) + bytes(units)


def doc_lists(lists, lfos=()):
    """FibRgFcLcb97 entries 73 (PlfLst) and 74 (PlfLfo) for make_doc's `fc`, each as (data, trailing): the LVLs
    follow the PlfLst past its lcb, and the LFOData follow the LFOs.

    `lists` are (lsid, levels): one level makes an fSimpleList; more are padded to nine with decimal "%N." levels.
    A level is _doc_lvl's (nfc, text[, start[, papx, chpx]]). `lfos` are (lsid, overrides), ilfo k + 1 being lfos[k];
    an override is (ilvl, start) for a start-at override (LFOLVL fStartAt), or (ilvl, start, level) for an LVL that
    replaces the level (fFormatting; fStartAt too unless start is None). Each LFOData has cp 0xFFFFFFFF.
    """
    head, lvls = bytearray(struct.pack("<h", len(lists))), bytearray()
    for lsid, levels in lists:
        levels = list(levels)
        simple = len(levels) == 1
        if not simple:
            levels += [(0, f"%{k + 1}.") for k in range(len(levels), 9)]
        head += struct.pack("<iI9HBB", lsid, 0, *([0x0FFF] * 9), 1 if simple else 0, 0)
        lvls += b"".join(_doc_lvl(lv) for lv in levels)
    lfo, data = bytearray(struct.pack("<I", len(lfos))), bytearray()
    for lsid, overrides in lfos:
        lfo += struct.pack("<iIIBBBB", lsid, 0, 0, len(overrides), 0, 0, 0)
        data += struct.pack("<I", 0xFFFFFFFF)
        for ov in overrides:
            ilvl, start, level = (*ov, None) if len(ov) == 2 else ov
            bits = ilvl | (0x10 if start is not None else 0) | (0x20 if level is not None else 0)
            data += struct.pack("<iI", start or 0, bits) + (_doc_lvl(level) if level is not None else b"")
    return {73: (bytes(head), bytes(lvls)), 74: (bytes(lfo), bytes(data))}


def doc_plc(cps, data=b""):
    """A PLC for make_doc's fc: the CPs as u32, then the data elements, already joined."""
    return struct.pack("<%dI" % len(cps), *cps) + bytes(data)


def doc_stories(main, *, footnotes=(), endnotes=(), comments=(), authors=(), headers=(), textboxes=(),
                header_textboxes=()):
    """make_doc's pieces, ccp and fc for a main story and the stories after it, laid out as Word writes them: every
    story but the main one ends with one more "\\r" that no entry owns (its guard), and a story's text PLC holds the
    start of each entry, then the end of the last one and the guard's end (the story's length), relative to the story.

    - footnotes, endnotes: (ref_cp, text). PlcffndRef/PlcfendRef (2/46) hold ref_cp, a CP in the whole text, with a
      2-byte FRD (nAuto 1) each and, last, a CP past every reference; PlcffndTxt/PlcfendTxt (3/47) the texts' CPs.
    - comments: (ref_cp, ibst, text[, initials]): PlcfandRef (4) with a 30-byte ATRDPre10 each (xstUsrInitl: u16
      count and 9 UTF-16 units; ibst u16 at 20; bitsNotUsed, grfNotUsed, ITagBkmk -1), PlcfandTxt (5) as above.
      authors: GrpXstAtnOwners (36), an Xst (u16 count, UTF-16) a name; none when empty.
    - headers: the PlcfHdd (11) stories in order, the 6 separators first, then 6 a section (even header, odd header,
      even footer, odd footer, first header, first footer); "" for one that is not there.
    - textboxes, header_textboxes: PlcftxbxTxt (56) and PlcfHdrtxbxTxt (58), a 22-byte FTXBXS each (cTxbx 1, fReusable
      0, the shape id) and a dummy one, all zero, for the guard.

    The text is one Unicode piece; a CP is a UTF-16 unit. Returns {"pieces", "ccp", "fc"} for make_doc(path, **it).
    """

    def units(s):
        return len(s.encode("utf-16-le")) // 2

    def story(texts):
        """The texts and their guard as one story, and its text PLC."""
        cps, at = [], 0
        for t in texts:
            cps.append(at)
            at += units(t)
        return "".join(texts) + "\r", doc_plc(cps + [at, at + 1])

    def refs(cps, data):
        return doc_plc(list(cps) + [max([*cps, units(main)]) + 1], b"".join(data))

    out, fc = {"text": main}, {}
    for name, ref, txt, notes in (("footnote", 2, 3, footnotes), ("endnote", 46, 47, endnotes)):
        if notes:
            out[name], fc[txt] = story([t for _, t in notes])
            fc[ref] = refs([r for r, _ in notes], [struct.pack("<h", 1)] * len(notes))
    if comments:
        out["annotation"], fc[5] = story([c[2] for c in comments])
        atrd = []
        for c in comments:
            initials = (c[3] if len(c) > 3 else "").encode("utf-16-le")[:18]
            atrd.append(struct.pack("<H", len(initials) // 2) + initials.ljust(18, b"\0")
                        + struct.pack("<HHHi", c[1], 0, 0, -1))
        fc[4] = refs([c[0] for c in comments], atrd)
    if authors:
        fc[36] = b"".join(struct.pack("<H", units(n)) + n.encode("utf-16-le") for n in authors)
    if headers:
        out["header"], fc[11] = story(list(headers))
    for name, index, boxes in (("textbox", 56, textboxes), ("header_textbox", 58, header_textboxes)):
        if boxes:
            out[name], plc = story(list(boxes))
            fc[index] = plc + b"".join(struct.pack("<iihiii", 1, 0, 0, -1, k + 1, 0) for k in range(len(boxes)))
            fc[index] += bytes(22)
    text = "".join(out.get(name, "") for name in DOC_STORIES)
    return {"pieces": [(text, False)], "ccp": {name: units(out.get(name, "")) for name in DOC_STORIES}, "fc": fc}


def oa_record(typ, body=b"", *, ver=0, inst=0):
    """One OfficeArt record (MS-ODRAW 2.2.1): u16 ver (low 4 bits) | inst << 4, u16 type, u32 length, then body."""
    return struct.pack("<HHI", ver | inst << 4, typ, len(body)) + bytes(body)


def oa_container(typ, *children, inst=0):
    """An OfficeArt container (ver 0xF) holding the records `children`, already built."""
    return oa_record(typ, b"".join(children), ver=0xF, inst=inst)


def blip_bitmap(typ, inst, data):
    """A bitmap BLIP (JPEG 0xF01D, PNG 0xF01E, DIB 0xF01F, TIFF 0xF029): one 16-byte UID, two when inst is odd, the
    tag byte 0xFF, then the image bytes."""
    return oa_record(typ, bytes(range(16)) * (1 + (inst & 1)) + b"\xff" + bytes(data), inst=inst)


def blip_png(data):
    """A PNG BLIP (0xF01E, instance 0x6E0) of the PNG bytes `data`."""
    return blip_bitmap(0xF01E, 0x6E0, data)


def blip_metafile(typ, inst, data, deflate=True, cb_save=None):
    """A metafile BLIP (EMF 0xF01A/0x3D4, WMF 0xF01B/0x216, PICT 0xF01C/0x542; inst + 1 has two UIDs): the UIDs, the
    34-byte OfficeArtMetafileHeader (cbSize, rcBounds, ptSize, cbSave, compression 0 deflate or 0xFE none, filter
    0xFE), then `data`, zlib-deflated when `deflate`. cb_save overrides cbSave."""
    packed = zlib.compress(bytes(data)) if deflate else bytes(data)
    save = len(packed) if cb_save is None else cb_save
    head = struct.pack("<I16s8sIBB", len(data), bytes(16), bytes(8), save, 0 if deflate else 0xFE, 0xFE)
    return oa_record(typ, bytes(range(16)) * (1 + (inst & 1)) + head + packed, inst=inst)


def oa_fbse(blip=b"", delay=0xFFFFFFFF, *, name=b"", cref=1):
    """An FBSE (0xF007): btWin32, btMacOS, rgbUid, tag, size, cRef, foDelay, unused1, cbName, unused2, unused3, the
    name, then the BLIP record `blip` embedded; b"" for one delay-loaded from WordDocument at `delay`."""
    body = struct.pack("<BB16sHIIIBBBB", 6, 6, bytes(16), 0xFF, len(blip), cref, delay, 0, len(name), 0, 0)
    return oa_record(0xF007, body + bytes(name) + bytes(blip), ver=2, inst=6)


def oa_shape(spid, pib=None, anchor: bytes | None = None, *, inst=75, flags=0xA00, props=b""):
    """An SpContainer (0xF004): its FSP (0xF00A, instance the shape type `inst`: spid, grfPersistent `flags`) and,
    when `pib` or `props` is given, an FOPT (0xF00B) holding pib (0x4104) when given, then the 6-byte properties
    `props` (opid u16, op u32 each); when `anchor` is given, a ClientAnchor (0xF010) with that body."""
    out = oa_record(0xF00A, struct.pack("<II", spid, flags), ver=2, inst=inst)
    fopt = (struct.pack("<HI", 0x4104, pib) if pib is not None else b"") + bytes(props)
    if fopt:
        out += oa_record(0xF00B, fopt, ver=3, inst=len(fopt) // 6)
    if anchor is not None:
        out += oa_record(0xF010, anchor)
    return oa_container(0xF004, out)


def oa_group(spid, *members, anchor: bytes | None = None):
    """An SpgrContainer (0xF003): its group shape first (an SpContainer whose FSP has fGroup 0x1, and a ClientAnchor
    (0xF010) with the body `anchor` when that is given), then the members, records already built (oa_shape,
    oa_group), in order."""
    shape = oa_record(0xF00A, struct.pack("<II", spid, 0x201), ver=2)
    if anchor is not None:
        shape += oa_record(0xF010, anchor)
    shape = oa_container(0xF004, shape)
    return oa_container(0xF003, shape, *members)


def oa_solver(*rules):
    """A SolverContainer (0xF005) of one ConnectorRule (0xF012) per (spidA, spidB, spidC) in `rules`: ruid k + 1,
    spidA (the start's shape), spidB (the end's), spidC (the connector), cptiA and cptiB 0."""
    return oa_container(0xF005, *[oa_record(0xF012, struct.pack("<6I", k + 1, a, b, c, 0, 0), ver=1)
                                  for k, (a, b, c) in enumerate(rules)], inst=len(rules))


def picf_inline(blip, mm=0x64, *, name=b"", pib=None, fbses=None):
    """A PICFAndOfficeArtData for the Data stream: the 68-byte PICF (lcb, cbHeader 0x44, mfpf.mm at offset 6); for mm
    0x66 cchPicName and the name; then the inline SpContainer (spid 1025, `pib` when given) and one FBSE embedding
    the BLIP record `blip` (none when `blip` is b""), or the FBSE records `fbses` as given. For mm 8 `blip` is the
    raw WMF that follows the PICF."""
    if mm == 8:
        rest = bytes(blip)
    else:
        rest = (bytes([len(name)]) + bytes(name) if mm == 0x66 else b"") + oa_shape(1025, pib)
        rest += (oa_fbse(blip) if blip else b"") if fbses is None else bytes(fbses)
    return struct.pack("<IHH", 68 + len(rest), 0x44, mm).ljust(68, b"\0") + rest


def dgg_with_bstore(blips, shapes=None, header_shapes=None, solver=b""):
    """A DggInfo (FibRgFcLcb97 50, OfficeArtContent) for make_doc's fc: the DggContainer (an FDGG, then a
    BStoreContainer with an FBSE a blip: bytes embed that BLIP record, an int is the foDelay of a BLIP in
    WordDocument), then the main document's drawing (dgglbl 0, a DgContainer whose SpgrContainer holds the shapes,
    then `solver`, records already built: oa_solver) and, when header_shapes is given, the header document's (dgglbl
    1). A shape is (spid, pib), pib None for one without a pib (a text box, a line), or bytes: records already built
    (oa_shape, oa_group); `shapes` defaults to spid 1025 + k showing pib k + 1."""
    store = [oa_fbse(b) if isinstance(b, (bytes, bytearray)) else oa_fbse(delay=b) for b in blips]
    dgg = oa_container(0xF000, oa_record(0xF006, struct.pack("<4I", 1026, 2, 1, 1)),
                       oa_container(0xF001, *store, inst=len(store)))

    def drawing(label, shapes, tail=b""):
        group = oa_container(0xF003, oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", 1024, 5), ver=2)),
                             *(s if isinstance(s, (bytes, bytearray)) else oa_shape(*s) for s in shapes))
        return bytes([label]) + oa_container(0xF002, oa_record(0xF008, struct.pack("<II", len(shapes), 1024)), group,
                                             bytes(tail))

    if shapes is None:
        shapes = [(1025 + k, k + 1) for k in range(len(blips))]
    out = dgg + drawing(0, shapes, solver)
    if header_shapes is not None:
        out += drawing(1, header_shapes)
    return out


def doc_spa(anchors):
    """A PlcfSpa (PlcSpaMom 40, PlcSpaHdr 41) for make_doc's fc: (cp, spid) anchors, then a last CP past them, and a
    26-byte FSPA each, spid first."""
    cps = [cp for cp, _ in anchors]
    return doc_plc(cps + [max(cps, default=0) + 1], b"".join(struct.pack("<I", s).ljust(26, b"\0") for _, s in anchors))


def mtef_v3(body):
    """MTEF v3 data, as Equation Editor 3.0 writes it: the 5-byte header (version 3, Windows, Equation Editor, 3.0),
    then the records `body`, already built, and the END of the top-level list."""
    return b"\x03\x01\x01\x03\x00" + bytes(body) + b"\x00"


def mtef_v5(body):
    """MTEF v5 data, as MathType writes it: the header (version 5, Windows, MathType, 6.0), the application key
    "DSMT6" and the equation options byte, then the records `body` and the END of the top-level list."""
    return b"\x05\x01\x00\x06\x00DSMT6\x00\x00" + bytes(body) + b"\x00"


def eq_native(mtef, cb=28):
    """An Equation Native stream: the 28-byte EQNOLEFILEHDR (cbHdr u16, version u32 0x00020000, cf u16, cbObject
    u32, four reserved u32), its cbHdr `cb` and the header padded or cut to it, then the MTEF bytes."""
    head = struct.pack("<HIHI", cb, 0x00020000, 0, len(mtef)).ljust(28, b"\0")
    return head[:cb].ljust(cb, b"\0") + bytes(mtef)


def comp_obj(prog, user=b"Object"):
    """A \\x01CompObj stream (MS-OLEDS CompObjStream): the 28-byte header, the AnsiUserType, no clipboard format,
    then the ProgID `prog`, each string a u32 length with its terminating zero."""

    def ansi(s):
        return struct.pack("<I", len(s) + 1) + bytes(s) + b"\0"
    return struct.pack("<II", 0xFFFE0001, 0xA03).ljust(28, b"\xff") + ansi(user) + bytes(4) + ansi(prog)


def object_pool(objects):
    """make_doc streams for the ObjectPool storage: {id: {stream name: bytes}} -> {"ObjectPool/_<id>/<name>": bytes},
    one storage an object."""
    return {f"ObjectPool/_{oid}/{name}": bytes(data) for oid, streams in objects.items()
            for name, data in streams.items()}


def make_doc(path, pieces, *, lid=0x0409, nfib=0x00C1, flags=0x0200, ccp=None, fc=None, word_extra=b"",
             streams=None, lid_fe=None, chpx=None, papx=None):
    """Write a Word 97 .doc compound file.

    WordDocument holds the FIB (FibBase with `lid`, `nfib` and `flags`; 14 FibRgW words, lidFE at 13, `lid_fe` or
    `lid`; 22 FibRgLw longs, the story lengths at 3-10; 93 FibRgFcLcb97 pairs; cswNew 0), `word_extra` at DOC_EXTRA,
    then the text of `pieces`, (text, compressed) in CP order: compressed text is one CP a byte, in the code page of
    the document's language (lidFE when flags has fFarEast 0x4000 and lidFE is set, else `lid`) when that code page is
    single-byte, else cp1252; the rest is UTF-16LE from an even offset. The table stream (1Table when flags has
    0x0200, else 0Table) holds each `fc` entry (FibRgFcLcb97 index -> bytes, or (data, trailing): lcb covers data and
    trailing follows it, as the LVLs follow a PlfLst) in index order with its (fc, lcb) set in the FIB, then the Clx
    (one Pcdt, no Prc) at index 33 unless `fc` gives it. `ccp` sets story lengths by DOC_STORIES
    name; "text" takes the CPs the others leave. `streams` adds streams and replaces a built one of the same name.
    `chpx` and `papx` are (cp0, cp1, bytes) spans in CPs, a CHPX grpprl or a PAPX (istd u16 + grpprl): they are mapped
    to FCs through the piece table at index 33 (built or given), written as FKP pages after the text, from the next
    512-byte boundary, and their PlcBteChpx (12) and PlcBtePapx (13) join the table stream unless `fc` gives them.
    Needs the reader's scripts folder on sys.path, as the tests set it.
    """
    from reader.text import codepage
    fe = lid if lid_fe is None else lid_fe
    eight = codepage(fe if flags & 0x4000 and fe else lid)
    if eight in ("cp932", "cp936", "cp949", "cp950"):  # one byte a CP: a double-byte code page is not used
        eight = "cp1252"
    body = bytearray(bytes(word_extra))
    cps, pcds = [0], []
    for text, compressed in pieces:
        if compressed:
            raw = text.encode(eight)
            at, n = DOC_EXTRA + len(body), len(raw)
            fcv = at * 2 | 0x40000000
        else:
            body += bytes(len(body) % 2)
            raw = text.encode("utf-16-le")
            at, n = DOC_EXTRA + len(body), len(raw) // 2
            fcv = at
        body += raw
        cps.append(cps[-1] + n)
        pcds.append(struct.pack("<HIH", 0, fcv, 0))
    lengths = dict(ccp or {})
    if "text" not in lengths:
        lengths["text"] = max(0, cps[-1] - sum(lengths.values()))
    entries = dict(fc or {})
    if 33 not in entries:
        plc = struct.pack("<%dI" % len(cps), *cps) + b"".join(pcds)
        entries[33] = b"\x02" + struct.pack("<I", len(plc)) + plc
    pages = b""
    if chpx or papx:
        first = -(-(DOC_EXTRA + len(body)) // 512)  # the page number of the first FKP
        clx = entries[33]
        pcd = _doc_pcds(bytes(clx[0] if isinstance(clx, tuple) else clx))
        for index, spans in ((12, chpx), (13, papx)):
            if spans and index not in entries:
                blob, bte = _doc_fkp(spans, pcd, index == 13, first + len(pages) // 512)
                pages += blob
                entries[index] = bte
        if pages:
            pages = bytes(first * 512 - DOC_EXTRA - len(body)) + pages
    pairs, table = [(0, 0)] * 93, bytearray()
    for i in sorted(entries):
        data, trailing = entries[i] if isinstance(entries[i], tuple) else (entries[i], b"")
        pairs[i] = (len(table), len(data))
        table += bytes(data) + bytes(trailing)
    rgw = [0] * 14
    rgw[13] = fe
    rglw = [0] * 22
    rglw[0] = DOC_EXTRA + len(body)  # cbMac
    for k, name in enumerate(DOC_STORIES):
        rglw[3 + k] = lengths.get(name, 0)
    fib = (struct.pack("<7HI2B2H2I", 0xA5EC, nfib, 0, lid, 0, flags, 0x00BF, 0, 0, 0, 0, 0, 0, 0)
           + struct.pack("<15H", 14, *rgw) + struct.pack("<H22I", 22, *rglw)
           + struct.pack("<H", 93) + b"".join(struct.pack("<II", *p) for p in pairs) + struct.pack("<H", 0))
    assert len(fib) <= DOC_EXTRA
    out = {"WordDocument": fib.ljust(DOC_EXTRA, b"\0") + bytes(body) + pages,
           "1Table" if flags & 0x0200 else "0Table": bytes(table)}
    out.update(streams or {})
    path = Path(path)
    make_cfb(path, out)
    return path


def rc4_header(password, *, cryptoapi=True, key_bits=128, salt=bytes(range(16))):
    """The bytes officecrypt.parse_rc4 reads at offset 0: an RC4 or RC4 CryptoAPI header for `password`."""
    from reader.containers.crypto import rc4
    pw = password.encode("utf-16le")
    verifier = bytes(range(100, 116))
    if cryptoapi:
        base = hashlib.sha1(salt + pw).digest()
        key = hashlib.sha1(base + bytes(4)).digest()[:key_bits // 8]
        key = key + bytes(11) if key_bits == 40 else key
        enc = rc4(key, verifier + hashlib.sha1(verifier).digest())
        header = struct.pack("<8I", 0x04, 0, 0x6801, 0x8004, 0 if key_bits == 40 else key_bits, 1, 0, 0) + b"\0\0"
        return (struct.pack("<HHII", 3, 2, 0x04, len(header)) + header + struct.pack("<I", 16) + salt
                + enc[:16] + struct.pack("<I", 20) + enc[16:])
    h0 = hashlib.md5(pw).digest()[:5]
    base = hashlib.md5((h0 + salt) * 16).digest()[:5]
    enc = rc4(hashlib.md5(base + bytes(4)).digest(), verifier + hashlib.md5(verifier).digest())
    return struct.pack("<HH", 1, 1) + salt + enc


def biff(rid, body=b""):
    """One BIFF record: id, size, body."""
    return struct.pack("<HH", rid, len(body)) + body


def xls_txo(text, *, wide=None, cch=None):
    """A text box's TXO (0x01B6, 18 bytes: grbit, rot, 6 reserved, cchText at 10, cbRuns, ifntEmpty, fmla) and its
    two CONTINUEs: the text (a flag byte, 0 for 8-bit or 1 for UTF-16, then the characters; UTF-16 when `wide`, or
    when `wide` is None and a character is past U+00FF) and the formatting runs (two 8-byte runs). `cch` overrides
    cchText."""
    if wide is None:
        wide = any(ord(ch) > 255 for ch in text)
    raw = text.encode("utf-16-le" if wide else "latin-1")
    runs = struct.pack("<HHI", 0, 0, 0) + struct.pack("<HHI", len(text), 0, 0)
    body = struct.pack("<HH6xHHH2x", 0x0212, 0, len(text) if cch is None else cch, len(runs), 0)
    return biff(0x01B6, body) + biff(0x003C, bytes([1 if wide else 0]) + raw) + biff(0x003C, runs)


def _xl_string(text, short=False):
    wide = any(ord(ch) > 255 for ch in text)
    raw = text.encode("utf-16-le" if wide else "latin-1")
    return struct.pack("<B" if short else "<H", len(text)) + bytes([1 if wide else 0]) + raw


def _biff_sst(strings, split):
    """The SST record and its CONTINUEs; with `split` (8224 when the SST is longer than that) each body holds at
    most that many bytes and a string cut by a boundary restarts with a flags byte, as BIFF8 writes it."""
    head = struct.pack("<II", len(strings), len(strings))
    if not split:
        whole = head + b"".join(_xl_string(s) for s in strings)
        if len(whole) <= 8224:
            return biff(0x00FC, whole)
        split = 8224  # a record body holds at most 8224 bytes (and a size field 65535): split as Excel does
    assert split >= 12
    bodies, cur = [], bytearray(head)
    for s in strings:
        wide = any(ord(ch) > 255 for ch in s)
        unit = 2 if wide else 1
        raw = s.encode("utf-16-le" if wide else "latin-1")
        if len(cur) + 3 + (unit if raw else 0) > split:
            bodies.append(bytes(cur))
            cur = bytearray()
        cur += struct.pack("<HB", len(s), 1 if wide else 0)
        while raw:
            take = min(len(raw) // unit, (split - len(cur)) // unit) * unit
            cur += raw[:take]
            raw = raw[take:]
            if raw:
                bodies.append(bytes(cur))
                cur = bytearray([1 if wide else 0])
    bodies.append(bytes(cur))
    return biff(0x00FC, bodies[0]) + b"".join(biff(0x003C, b) for b in bodies[1:])


def _biff_cell(r, c, v, sst, fmt_xf):
    pre = struct.pack("<HHH", r, c, 15)
    if isinstance(v, tuple) and v[0] == "fmt":
        return biff(0x0203, struct.pack("<HHH", r, c, fmt_xf[v[2]]) + struct.pack("<d", v[1]))
    if isinstance(v, tuple) and v[0] == "err":
        return biff(0x0205, pre + bytes([v[1], 1]))
    if isinstance(v, tuple) and v[0] == "f":
        cached, extra = v[1], b""
        if isinstance(cached, tuple):
            res = bytes([2, 0, cached[1], 0, 0, 0, 0xFF, 0xFF])
        elif isinstance(cached, bool):
            res = bytes([1, 0, int(cached), 0, 0, 0, 0xFF, 0xFF])
        elif isinstance(cached, str):
            res, extra = bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF]), biff(0x0207, _xl_string(cached))
        else:
            res = struct.pack("<d", float(cached))
        return biff(0x0006, pre + res + struct.pack("<HIH", 0, 0, 0)) + extra
    if isinstance(v, bool):
        return biff(0x0205, pre + bytes([int(v), 0]))
    if isinstance(v, str):
        return biff(0x00FD, pre + struct.pack("<I", sst[v]))
    if isinstance(v, int) and -(1 << 29) <= v < (1 << 29):
        return biff(0x027E, pre + struct.pack("<I", ((v << 2) & 0xFFFFFFFF) | 2))
    return biff(0x0203, pre + struct.pack("<d", float(v)))


def _biff_sheet(sh, sst, fmt_xf):
    """One worksheet (or chart, macro, VBA) substream. Merges are (first row, first col, last row, last col). A
    `drawing` (bytes) is the MSODRAWING 0x00EC body, cut after `drawing_split` bytes into a CONTINUE when that is set."""
    if "raw" in sh:
        return sh["raw"]
    kind = sh.get("kind", 0)
    dt = {0: 0x0010, 1: 0x0040, 2: 0x0020, 6: 0x0006}.get(kind, 0x0010)
    out = [biff(0x0809, struct.pack("<HHHHII", 0x0600, dt, 0x0DBB, 0x07CC, 0, 6))]
    cells = dict(sorted(sh.get("cells", {}).items()))
    hidden_rows, hidden_cols = set(sh.get("hidden_rows", ())), sorted(set(sh.get("hidden_cols", ())))
    rows = sorted({r for r, _ in cells} | hidden_rows)
    if kind == 0:
        top = max((r for r, _ in cells), default=-1) + 1
        wide = max((c for _, c in cells), default=-1) + 1
        out += [biff(0x007D, struct.pack("<HHHHHH", c, c, 2560, 15, 1, 0)) for c in hidden_cols]  # before DIMENSIONS
        out.append(biff(0x0200, struct.pack("<IIHHH", 0, top, 0, wide, 0)))
        for r in rows:
            used = [c for rr, c in cells if rr == r]  # colMic and colMac: the row's first column and one past its last
            lo, hi = (min(used), max(used) + 1) if used else (0, 0)
            out.append(biff(0x0208, struct.pack("<HHHHHHHH", r, lo, hi, 255, 0, 0, 0x20 if r in hidden_rows else 0, 15)))
        out += [_biff_cell(r, c, v, sst, fmt_xf) for (r, c), v in cells.items()]
        merges = list(sh.get("merges", ()))
        for i in range(0, len(merges), 1000):
            part = merges[i:i + 1000]
            out.append(biff(0x00E5, struct.pack("<H", len(part))
                            + b"".join(struct.pack("<HHHH", m[0], m[2], m[1], m[3]) for m in part)))
    for extra in sh.get("records", ()):
        out.append(extra if isinstance(extra, bytes) else biff(*extra))
    if sh.get("drawing") is not None:
        out += _biff_drawing(0x00EC, sh["drawing"], sh.get("drawing_split"))
    out.append(biff(0x000A))
    return b"".join(out)


def _biff_drawing(rid, body, split=None):
    """An MSODRAWINGGROUP or MSODRAWING record of `body`, its tail from byte `split` on in a CONTINUE."""
    if split is None:
        return [biff(rid, body)]
    return [biff(rid, body[:split]), biff(0x003C, body[split:])]


def biff_workbook(sheets, *, formats=None, date1904=False, active=0, filepass=None, sst_split=None,
                  globals_extra=b"", drawing=None, drawing_split=None):
    """A BIFF8 Workbook stream: globals, then one substream per sheet dict.

    A sheet dict: name, state (0 visible, 1 hidden, 2 very hidden), kind (0 sheet, 1 macro, 2 chart, 6 VBA);
    cells {(row, col): value} with str (LABELSST), float (NUMBER), int (RK), bool (BOOLERR), ("err", code),
    ("f", cached) (FORMULA, plus STRING for text) and ("fmt", number, code) (NUMBER in a cell XF of that format);
    merges [(r1, c1, r2, c2)], hidden_rows, hidden_cols, records (raw records before EOF: framed bytes or
    (id, body)) and raw (the whole substream). Cell XFs start at index 16, one per format code the cells use, in
    order of first use; every other cell uses XF 15. `drawing` (bytes, a DggContainer) is the globals' MSODRAWINGGROUP,
    cut after `drawing_split` bytes into a CONTINUE; a sheet's `drawing` and `drawing_split` do the same for its
    MSODRAWING.
    """
    formats = dict(formats or {})
    strings = list(dict.fromkeys(v for sh in sheets for v in sh.get("cells", {}).values() if isinstance(v, str)))
    sst = {s: i for i, s in enumerate(strings)}
    codes = list(dict.fromkeys(v[2] for sh in sheets for v in sh.get("cells", {}).values()
                               if isinstance(v, tuple) and v[0] == "fmt"))
    by_code = {code: fid for fid, code in formats.items()}
    for code in codes:
        if isinstance(code, int):
            by_code[code] = code
        elif code not in by_code:
            by_code[code] = max([163, *formats]) + 1
            formats[by_code[code]] = code
    fmt_xf = {code: 16 + i for i, code in enumerate(codes)}
    head = [biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6))]
    if filepass is not None:
        head.append(biff(0x002F, filepass))
    head.append(biff(0x0042, struct.pack("<H", 1200)))
    head.append(biff(0x0022, struct.pack("<H", int(date1904))))
    head.append(biff(0x003D, struct.pack("<9H", 0, 0, 0x4000, 0x2000, 0x38, active, 0, 1, 600)))
    head += [biff(0x041E, struct.pack("<H", fid) + _xl_string(code)) for fid, code in formats.items()]
    xf = struct.pack("<HHH", 0, 0, 0xFFF5) + bytes(14)
    head += [biff(0x00E0, xf)] * 16
    head += [biff(0x00E0, struct.pack("<HHH", 0, by_code[code], 1) + bytes(14)) for code in codes]
    head.append(_biff_sst(strings, sst_split))
    names = [biff(0x0085, struct.pack("<IBB", 0, sh.get("state", 0), sh.get("kind", 0))
                  + _xl_string(sh["name"], short=True)) for sh in sheets]
    if drawing is not None:
        globals_extra += b"".join(_biff_drawing(0x00EB, drawing, drawing_split))
    tail = biff(0x000A)
    base = sum(map(len, head)) + sum(map(len, names)) + len(globals_extra) + len(tail)
    subs = [_biff_sheet(sh, sst, fmt_xf) for sh in sheets]
    pos, bounds = base, []
    for n, sub in zip(names, subs):
        bounds.append(n[:4] + struct.pack("<I", pos) + n[8:])
        pos += len(sub)
    return b"".join(head) + b"".join(bounds) + globals_extra + tail + b"".join(subs)


def biff_chart(series, *, title=None, kinds=((0x1017, bytes(6)),), cache=True, eof=True, extra=()):
    """A chart substream (BOF dt 0x20 ... EOF), as a chart sheet's `raw` or inside a worksheet's `records`.

    `series` is a list of dicts: name, vals and cats (lists), ifmt (BRAI number format id), aux (carries SerParent
    0x104A) and ref ((role, tokens) pairs that make that role's BRAI a reference: role 0 is the name, 1 the values and 2
    the categories). With `cache` the values and categories follow as SIIndex
    caches (NUMBER for a number, LABEL for a string, col = the series), else each series has only its BRAIs.
    `kinds` are (id, body) records; `extra` raw records go before the EOF."""
    short = lambda t: _xl_string(t, short=True)

    def brai(role, source, tokens=b"", ifmt=0):
        return biff(0x1051, struct.pack("<BBHHH", role, source, 1 if ifmt else 0, ifmt, len(tokens)) + tokens)

    out = [biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0020, 0x0DBB, 0x07CC, 0, 6))]
    out += [biff(0x1002, bytes(16)), biff(0x1033)]
    for s in series:
        refs = dict(s.get("ref", ()))
        out += [biff(0x1003, struct.pack("<6H", 1, 1, len(s.get("vals", ())), len(s.get("cats", ())), 0, 0)),
                biff(0x1033)]
        out.append(brai(0, 2 if 0 in refs else 1, refs.get(0, b"")))
        if s.get("name") is not None:
            out.append(biff(0x100D, struct.pack("<H", 0) + short(s["name"])))
        out.append(brai(1, 2 if 1 in refs else 1, refs.get(1, b""), s.get("ifmt", 0)))
        out.append(brai(2, 2 if 2 in refs else 1, refs.get(2, b"")))
        if s.get("aux"):  # a trendline or error-bar series carries SerParent
            out.append(biff(0x104A, bytes(2)))
        out.append(biff(0x1034))
    for rid, body in kinds:
        out.append(biff(rid, body))
    if title is not None:
        out += [biff(0x1025, bytes(32)), biff(0x1033), biff(0x1027, struct.pack("<HHH", 1, 0, 0)),
                biff(0x100D, struct.pack("<H", 0) + short(title)), biff(0x1034)]
    if cache:
        out.append(biff(0x1006, bytes(16)))
        for role, key in ((1, "vals"), (2, "cats")):
            out.append(biff(0x1065, struct.pack("<H", role) + bytes(2)))
            for col, s in enumerate(series):
                for row, v in enumerate(s.get(key, ())):
                    if isinstance(v, str):
                        out.append(biff(0x0204, struct.pack("<HHH", row, col, 0) + _xl_string(v)))
                    else:
                        out.append(biff(0x0203, struct.pack("<HHH", row, col, 0) + struct.pack("<d", float(v))))
    out += [e if isinstance(e, bytes) else biff(*e) for e in extra]
    out.append(biff(0x1034))
    if eof:
        out.append(biff(0x000A))
    return b"".join(out)


def area3d(sheet_xti, r1, r2, c1, c2):
    """A BRAI Area3d token: ptg 0x3B, the XTI index, then the rows and columns."""
    return struct.pack("<BHHHHH", 0x3B, sheet_xti, r1, r2, c1, c2)


def externsheet(*sheets):
    """SUPBOOK (this workbook, `len(sheets)` sheets) and an EXTERNSHEET with one XTI per sheet index given."""
    xti = b"".join(struct.pack("<Hhh", 0, i, i) for i in sheets)
    return (biff(0x01AE, struct.pack("<HH", max(sheets, default=0) + 1, 0x0401))
            + biff(0x0017, struct.pack("<H", len(sheets)) + xti))


def make_xls(path, sheets, **kw):
    """Write a compound file whose Workbook stream is biff_workbook(sheets, **kw); returns that stream."""
    data = biff_workbook(sheets, **kw)
    make_cfb(path, {"Workbook": data})
    return data


def excel_object(sheets, prog=b"Excel.Sheet.8", **kw):
    """The compound file of an embedded Excel object (an OLE storage's streams): a Workbook stream that is
    biff_workbook(sheets, **kw) and a \\x01CompObj naming `prog`, as bytes."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "obj.cfb"
        make_cfb(path, {"Workbook": biff_workbook(sheets, **kw), "\x01CompObj": comp_obj(prog)})
        return path.read_bytes()


def msgraph_object(cats, series, kind="bar", title="", *, by_rows=False, codepage=1252, codepage_at="in", drop=(),
                   label_codec=None):
    """The compound file of an embedded MS Graph 97 object: a Workbook stream (BOF vers 0x0680 dt 0x8000) holding the
    chart records and the datasheet (CODEPAGE `codepage_at` "in" the substream after its BOF, "before" the BOF,
    "late", after the first cell, "nested", inside a nested BOF/EOF pair, or "after" the substream's EOF), and a \\x01CompObj naming MSGraph.Chart.8, as bytes.

    `series` is a list of (name, values). The datasheet's row 0 (column 0 with by_rows) holds the series names, its
    first column (row) the categories and the cells use Graph's own ids, NUMBER 3 and LABEL 4: row, col, 3 attribute
    bytes, then the f64 or a u16-sized narrow string. Selections 0x1053 / 0x1054 stop after the rows / columns, 0x1055
    says whether the series are in rows; each Series' BRAIs end in the datasheet index of their row or column. `drop` is a set of (row, col) datasheet cells
    left out; `label_codec` is the codec the labels are written in when it differs from the CODEPAGE record."""
    n = len(series)
    head = [biff(0x0809, struct.pack("<HHHHII", 0x0680, 0x8000, 0, 0, 0, 0)),
            biff(0x1055, bytes([1 if by_rows else 0]) + bytes(5)),
            biff(0x1053, struct.pack("<HH", 0, (n if by_rows else len(cats)) + 1)),
            biff(0x1054, struct.pack("<HH", 0, (len(cats) if by_rows else n) + 1)), biff(0x1033)]

    def brai(role, idx):
        return biff(0x1051, struct.pack("<BBHHH", role, 1, 0, 0, idx))
    for k, (_, vals) in enumerate(series):
        head += [biff(0x1003, struct.pack("<6H", 1, 1, len(cats), len(vals), 0, 0)), biff(0x1033),
                 brai(0, k + 1), brai(1, k + 1), brai(2, 0), biff(0x1034)]
    ids = {"bar": (0x1017, bytes(6)), "line": (0x1018, bytes(2)), "pie": (0x1019, bytes(6)),
           "area": (0x101A, bytes(2))}
    head.append(biff(*ids[kind]))
    if title:
        head += [biff(0x1025, bytes(32)), biff(0x1033), biff(0x1027, struct.pack("<HHH", 1, 0, 0)),
                 biff(0x100D, struct.pack("<H", 0) + _xl_string(title, short=True)), biff(0x1034)]
    head.append(biff(0x1034))

    def cell(point, axis, value):
        row, col = (axis, point) if by_rows else (point, axis)
        if isinstance(value, str):
            raw = value.encode(label_codec or f"cp{codepage}")
            return biff(4, struct.pack("<HH", row, col) + bytes(3) + struct.pack("<H", len(raw)) + raw)
        return biff(3, struct.pack("<HH", row, col) + bytes(3) + struct.pack("<d", float(value)))
    cells = [cell(0, k + 1, name) for k, (name, _) in enumerate(series)]
    cells += [cell(i + 1, 0, c) for i, c in enumerate(cats)]
    cells += [cell(i + 1, k + 1, v) for k, (_, vals) in enumerate(series) for i, v in enumerate(vals)]
    where = {(row, col) for row, col in drop}
    cells = [c for c in cells if struct.unpack_from("<HH", c, 4) not in where]
    cp = biff(0x0042, struct.pack("<H", codepage))
    tail = []
    if codepage_at == "before":  # ahead of the dt 0x8000 BOF
        head.insert(0, cp)
    elif codepage_at == "late":  # after the first label cell
        cells.insert(1, cp)
    elif codepage_at == "nested":  # inside a BOF/EOF pair within the substream
        cells.insert(1, biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0, 0, 0, 0)) + cp + biff(0x000A))
    elif codepage_at == "after":  # after the substream's EOF
        tail.append(cp)
    else:
        head.insert(1, cp)
    data = b"".join(head + cells + [biff(0x000A)] + tail)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "obj.cfb"
        make_cfb(path, {"Workbook": data, "CompObj": comp_obj(b"MSGraph.Chart.8", b"Microsoft Graph Chart")})
        return path.read_bytes()


def encrypt_xls(sheets, password, kind, **kw):
    """A Workbook stream encrypted with `password`: kind "xor", "rc4", "cryptoapi" (128-bit) or "cryptoapi40".

    Built from biff_workbook(sheets, filepass=..., **kw). Everything up to and including FILEPASS stays clear, as do
    every record header, the BOF/INTERFACEHDR-style records and the first 4 bytes of each BOUNDSHEET ([MS-XLS] 2.2.10).
    XOR: per record body, rotate right by 3 and XOR with the array from index (record end offset) % 16.
    RC4: the stream is encrypted in 1024-byte blocks, block key = key(offset // 1024).
    """
    from reader.containers import officecrypt
    from reader.containers.crypto import rc4
    from reader.formats import xls_crypt
    # The clear-record set and the rotate-right table below are spelled out here on purpose, not imported from
    # xls_crypt: the fixture must stay an independent encoder, or a slip in the reader's copy would cancel out.
    clear = {0x0809, 0x0009, 0x0209, 0x0409, 0x002F, 0x0194, 0x0195, 0x00E1, 0x0196, 0x0138}
    if kind == "xor":
        key = xls_crypt.xor_key(password)
        filepass = struct.pack("<HHH", 0, key, xls_crypt.xor_verifier(password))
    else:
        assert kind in ("rc4", "cryptoapi", "cryptoapi40"), kind
        filepass = struct.pack("<H", 1) + rc4_header(password, cryptoapi=kind != "rc4",
                                                    key_bits=40 if kind == "cryptoapi40" else 128)
    data = biff_workbook(sheets, filepass=filepass, **kw)
    recs, pos = [], 0
    while pos + 4 <= len(data):
        rid, size = struct.unpack_from("<HH", data, pos)
        recs.append((rid, pos, pos + 4 + size))
        pos += 4 + size
    start = next(e for rid, _, e in recs if rid == 0x002F)
    out = bytearray(data)
    if kind == "xor":
        arr = xls_crypt.xor_array(password, key)
        rot_right = bytes(((v >> 3) | (v << 5)) & 0xFF for v in range(256))
        for rid, p, e in recs:
            if p < start or rid in clear or e == p + 4:
                continue
            n = e - p - 4
            ks = (arr * (n // 16 + 2))[e % 16:e % 16 + n]
            x = int.from_bytes(data[p + 4:e].translate(rot_right), "little") ^ int.from_bytes(ks, "little")
            out[p + 4:e] = x.to_bytes(n, "little")
    else:
        keys = officecrypt.block_keys(officecrypt.parse_rc4(filepass, 2), password)
        assert keys is not None
        for b in range(start // 1024, (len(data) - 1) // 1024 + 1):
            lo, hi = max(b * 1024, start), min((b + 1) * 1024, len(data))
            out[lo:hi] = rc4(keys(b), data[lo:hi], skip=lo - b * 1024)
        for rid, p, e in recs:
            if p >= start:
                out[p:p + 4] = data[p:p + 4]
                if rid in clear:
                    out[p + 4:e] = data[p + 4:e]
    for rid, p, e in recs:
        if p >= start and rid == 0x0085:
            out[p + 4:p + 8] = data[p + 4:p + 8]
    return bytes(out)


def biff12(rid: int, body: bytes = b"") -> bytes:
    """One BIFF12 record: a 1 or 2 byte id and a size of 1 to 4 bytes, 7 bits each."""
    def seven(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)
    return (seven(rid) if rid >= 0x80 else bytes([rid])) + seven(len(body)) + bytes(body)


def xlsb_wide(text):
    """An XLWideString (u32 cch, UTF-16LE), or the null string for None."""
    if text is None:
        return struct.pack("<I", 0xFFFFFFFF)
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def xlsb_bundle(name, rid, state=0, tab=1, extra=b""):
    """A BrtBundleSh record; `extra` goes before strRelID."""
    return biff12(156, struct.pack("<II", state, tab) + extra + xlsb_wide(rid) + xlsb_wide(name))


def xlsb_row(r, hidden=False):
    """A BrtRowHdr: the row, seven filler bytes, then the flag byte (0x10 is hidden)."""
    return biff12(0, struct.pack("<I", r) + bytes(7) + bytes([0x10 if hidden else 0]))


def xlsb_cell(rid, col, style, body=b""):
    """A cell record: column, style (a cellXfs index), then `body`."""
    return biff12(rid, struct.pack("<II", col, style) + bytes(body))


def make_xlsb(path, sheets, *, formats=None, date1904=False, workbook_records=None, extra_parts=None):
    """Write an .xlsb package: content types, rels, workbook.bin, styles.bin, sharedStrings.bin, one sheet part per
    dict.

    A sheet dict has Task 5's keys (name, state, cells, ...), `chartsheet` (its relationship is a chartsheet) and
    `records`, raw BIFF12 records (bytes) for the sheet part. Shared strings come from the str cells; each
    ("fmt", number, code) cell format gets a cell XF after the General one (a str code is a custom format, an int a
    built-in format id). The `cells` of a sheet are also written as records before `records`: a row header per row,
    then BrtCellIsst for a str, BrtCellBool for a bool, BrtCellReal for a number and BrtCellReal in the format's XF
    for a ("fmt", number, code). `workbook_records` replaces the generated BrtBundleSh records. `extra_parts` adds
    parts or replaces generated ones ({name: bytes}); a None value leaves a part out.
    """
    formats = dict(formats or {})
    strings = list(dict.fromkeys(v for sh in sheets for v in sh.get("cells", {}).values() if isinstance(v, str)))
    codes = list(dict.fromkeys(v[2] for sh in sheets for v in sh.get("cells", {}).values()
                               if isinstance(v, tuple) and v[0] == "fmt"))
    by_code = {code: fid for fid, code in formats.items()}
    for code in codes:
        if isinstance(code, int):
            by_code[code] = code
        elif code not in by_code:
            by_code[code] = max([163, *formats]) + 1
            formats[by_code[code]] = code
    wb = [biff12(153, struct.pack("<I", int(date1904)) + bytes(4)), biff12(143)]
    if workbook_records is None:
        wb += [xlsb_bundle(sh["name"], f"rId{i + 1}", sh.get("state", 0), i + 1) for i, sh in enumerate(sheets)]
    else:
        wb += list(workbook_records)
    wb.append(biff12(144))
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    wb_rels = "".join(f'<Relationship Id="rId{i + 1}" Type="{rel}{"chartsheet" if sh.get("chartsheet") else "worksheet"}" '
                      f'Target="worksheets/sheet{i + 1}.bin"/>' for i, sh in enumerate(sheets))
    wb_rels += (f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}styles" Target="styles.bin"/>'
                f'<Relationship Id="rId{len(sheets) + 2}" Type="{rel}sharedStrings" Target="sharedStrings.bin"/>')

    def xf(parent, fid):
        return biff12(47, struct.pack("<HH", parent, fid) + bytes(8))
    styles = [biff12(278)]
    if formats:
        styles += [biff12(615, struct.pack("<I", len(formats)))]
        styles += [biff12(44, struct.pack("<H", fid) + xlsb_wide(code)) for fid, code in formats.items()]
        styles += [biff12(616)]
    styles += [biff12(626, struct.pack("<I", 1)), xf(0xFFFF, 0), biff12(627)]
    styles += [biff12(617, struct.pack("<I", 1 + len(codes))), xf(0, 0)]
    styles += [xf(0, by_code[code]) for code in codes] + [biff12(618), biff12(279)]
    sst = [biff12(159, struct.pack("<II", len(strings), len(strings)))]
    sst += [biff12(19, b"\0" + xlsb_wide(s)) for s in strings] + [biff12(160)]
    members = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="bin" ContentType="application/vnd.ms-excel.sheet.binary.macroEnabled.main"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}officeDocument" Target="xl/workbook.bin"/></Relationships>'),
        "xl/workbook.bin": b"".join(wb),
        "xl/_rels/workbook.bin.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{wb_rels}'
            '</Relationships>'),
        "xl/styles.bin": b"".join(styles),
        "xl/sharedStrings.bin": b"".join(sst),
    }
    for i, sh in enumerate(sheets):
        recs, last = [], None
        for (r, c), v in sorted(sh.get("cells", {}).items()):
            if r != last:
                recs.append(xlsb_row(r))
                last = r
            if isinstance(v, str):
                recs.append(xlsb_cell(7, c, 0, struct.pack("<I", strings.index(v))))
            elif isinstance(v, bool):
                recs.append(xlsb_cell(4, c, 0, bytes([int(v)])))
            elif isinstance(v, tuple):
                recs.append(xlsb_cell(5, c, 1 + codes.index(v[2]), struct.pack("<d", v[1])))
            else:
                recs.append(xlsb_cell(5, c, 0, struct.pack("<d", v)))
        members[f"xl/worksheets/sheet{i + 1}.bin"] = (
            biff12(129) + b"".join(recs) + b"".join(sh.get("records", ())) + biff12(130))
    for name, data in (extra_parts or {}).items():
        if data is None:
            members.pop(name, None)
        else:
            members[name] = data
    make_zip(path, members, stored=())


# ---- xlsx ----

XLSX_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml."


def _xml_text(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(s) -> str:
    return _xml_text(s).replace('"', "&quot;")


def xlsx_cell(ref, v=None, *, t=None, s=None, f=None, inline=None) -> str:
    """One `c` element. `ref` None leaves out `r`; `v` is the value text (escaped); `f` is the formula text (escaped),
    or a raw element when it starts with "<"; `inline` is raw `is` inner XML (and sets t="inlineStr" unless `t` is
    given)."""
    if inline is not None and t is None:
        t = "inlineStr"
    attrs = "".join(f' {k}="{_xml_attr(x)}"' for k, x in (("r", ref), ("s", s), ("t", t)) if x is not None)
    body = ""
    if f is not None:
        body += f if str(f).startswith("<") else f"<f>{_xml_text(f)}</f>"
    if v is not None:
        body += f"<v>{_xml_text(v)}</v>"
    if inline is not None:
        body += f"<is>{inline}</is>"
    return f"<c{attrs}>{body}</c>" if body else f"<c{attrs}/>"


def xlsx_row(r, *cells, attrs="") -> str:
    """One `row` element holding the `cells` XML; `r` None leaves out its `r`, `attrs` is raw attribute text."""
    head = (f' r="{r}"' if r is not None else "") + (f" {attrs}" if attrs else "")
    return f"<row{head}>{''.join(cells)}</row>"


def _xlsx_rels(items) -> str:
    """A relationships part from (id, type, target, external): a type without "://" gets the officeDocument
    relationships prefix."""
    body = ""
    for rid, typ, target, ext in items:
        typ = typ if "://" in typ else f"{XLSX_REL}/{typ}"
        mode = ' TargetMode="External"' if ext else ""
        body += f'<Relationship Id="{_xml_attr(rid)}" Type="{_xml_attr(typ)}" Target="{_xml_attr(target)}"{mode}/>'
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_PKG_REL}">{body}</Relationships>'


def make_xlsx(path, sheets, *, shared=None, numfmts=None, xfs=None, date1904=False, wb_rels=(), extra_parts=None):
    """Write an .xlsx package: content types, rels, workbook.xml, styles.xml, sharedStrings.xml and one part per sheet
    dict; with path None nothing is written and the zip's bytes are returned.

    A sheet dict has `name` (default "Sheet<n>"), `state`, `rows` (XML inside `sheetData`), `before` and `after` (XML
    around it), `rels` ((id, type, target, external) for the sheet's own relationships part), `chartsheet` (True
    writes `xl/chartsheets/sheet<n>.xml` holding `<drawing r:id="rId1"/>`), `rel_type` (the full Type URI of the
    workbook's relationship to it), `part` (raw bytes or str for the part), `missing` (True writes no part) and
    `number` (the n of the part name and of its workbook relationship id `rId<n>`, default its position + 1).
    `shared` is a list of raw `si` inner XML strings. `numfmts` ({id: code}) and `xfs` (the `cellXfs` numFmtId list,
    default [0]) write `xl/styles.xml`. `wb_rels` adds workbook relationships ((id, type, target, external), ids after
    the generated ones). `extra_parts` adds or replaces parts ({name: bytes | str}); a None value leaves one out.
    """
    sheets = list(sheets)
    nums = [sh.get("number", i + 1) for i, sh in enumerate(sheets)]
    ns = f'xmlns="{XLSX_SML}" xmlns:r="{XLSX_REL}"'
    entries, rels, overrides, members = [], [], [], {}
    for i, (sh, n) in enumerate(zip(sheets, nums)):
        name = sh.get("name", f"Sheet{i + 1}")
        state = f' state="{_xml_attr(sh["state"])}"' if sh.get("state") else ""
        entries.append(f'<sheet name="{_xml_attr(name)}" sheetId="{i + 1}"{state} r:id="rId{n}"/>')
        chart = sh.get("chartsheet", False)
        folder, kind = ("chartsheets", "chartsheet") if chart else ("worksheets", "worksheet")
        part = f"xl/{folder}/sheet{n}.xml"
        rels.append((f"rId{n}", sh.get("rel_type", kind), f"{folder}/sheet{n}.xml", False))
        if sh.get("missing"):
            continue
        overrides.append((part, f"{_XLSX_CT}{kind}+xml"))
        if "part" in sh:
            members[part] = sh["part"]
        elif chart:
            members[part] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><chartsheet {ns}>'
                             f'{sh.get("before", "")}<drawing r:id="rId1"/>{sh.get("after", "")}</chartsheet>')
        else:
            members[part] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet {ns}>'
                             f'{sh.get("before", "")}<sheetData>{sh.get("rows", "")}</sheetData>'
                             f'{sh.get("after", "")}</worksheet>')
        if sh.get("rels"):
            members[f"xl/{folder}/_rels/sheet{n}.xml.rels"] = _xlsx_rels(sh["rels"])
    top = max(nums, default=0)
    if numfmts is not None or xfs is not None:
        rels.append((f"rId{top + 1}", "styles", "styles.xml", False))
        overrides.append(("xl/styles.xml", f"{_XLSX_CT}styles+xml"))
        fmts = "".join(f'<numFmt numFmtId="{fid}" formatCode="{_xml_attr(code)}"/>'
                       for fid, code in (numfmts or {}).items())
        fmts = f'<numFmts count="{len(numfmts)}">{fmts}</numFmts>' if numfmts else ""
        cell_xfs = list(xfs) if xfs is not None else [0]
        members["xl/styles.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{XLSX_SML}">{fmts}'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(cell_xfs)}">'
            + "".join(f'<xf numFmtId="{fid}" fontId="0" xfId="0" applyNumberFormat="1"/>' for fid in cell_xfs)
            + '</cellXfs></styleSheet>')
    if shared is not None:
        rels.append((f"rId{top + 2}", "sharedStrings", "sharedStrings.xml", False))
        overrides.append(("xl/sharedStrings.xml", f"{_XLSX_CT}sharedStrings+xml"))
        members["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{XLSX_SML}" count="{len(shared)}" uniqueCount="{len(shared)}">'
            + "".join(f"<si>{x}</si>" for x in shared) + "</sst>")
    rels.extend(wb_rels)
    pr = '<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>"
    members["xl/workbook.xml"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {ns}>{pr}'
                                  f'<sheets>{"".join(entries)}</sheets></workbook>')
    members["xl/_rels/workbook.xml.rels"] = _xlsx_rels(rels)
    members["_rels/.rels"] = _xlsx_rels([("rId1", "officeDocument", "xl/workbook.xml", False)])
    overrides.insert(0, ("xl/workbook.xml", f"{_XLSX_CT}sheet.main+xml"))
    members["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Override PartName="/{p}" ContentType="{ct}"/>' for p, ct in overrides) + "</Types>")
    order = ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"]
    members = {k: members[k] for k in order + sorted(set(members) - set(order))}
    for name, data in (extra_parts or {}).items():
        if data is None:
            members.pop(name, None)
        else:
            members[name] = data
    data = make_zip(path, members, stored=())
    return data if path is None else None


# ---- ppt ----

def _ppt_chars(text, as_bytes):
    """A TextCharsAtom (0x0FA0, UTF-16LE) or, when `as_bytes`, a TextBytesAtom (0x0FA8, Latin-1) of `text`."""
    if as_bytes:
        return oa_record(0x0FA8, text.encode("latin-1"))
    return oa_record(0x0FA0, text.encode("utf-16-le"))


def _ppt_style(text, indents, bold):
    """A StyleTextPropAtom (0x0FA1) for `text`, split into paragraphs at \\r: one paragraph run each (count u32, indent
    u16, mask u32; a bulleted paragraph, indents[i] an int, has mask 0x1 and a bulletFlags u16 of 1; indents[i] a tuple
    (level,) has that indent and mask 0, its bullet left to the master), then character
    runs (count u32, mask u32, style u16 when the mask is set; bold is mask and style 0x1) over every occurrence of the
    `bold` substrings. Each kind of run covers len(text) + 1 characters."""
    out = b""
    for para, indent in zip(text.split("\r"), indents):
        if indent is None:
            out += struct.pack("<IHI", len(para) + 1, 0, 0)
        elif isinstance(indent, tuple):
            out += struct.pack("<IHI", len(para) + 1, indent[0], 0)
        else:
            out += struct.pack("<IHIH", len(para) + 1, indent, 1, 1)
    marks = [0] * (len(text) + 1)
    for word in bold:
        at = text.find(word) if word else -1
        while at >= 0:
            marks[at:at + len(word)] = [1] * len(word)
            at = text.find(word, at + len(word))
    for flag, run in itertools.groupby(marks):
        size = len(list(run))
        out += struct.pack("<IIH", size, 1, 1) if flag else struct.pack("<II", size, 0)
    return oa_record(0x0FA1, out)


def ppt_text(kind, text, *, indents=None, bold=(), as_bytes=False):
    """The records of one text run: TextHeaderAtom (0x0F9F, kind 0 title, 1 body, 2 notes), the text atom and, when a
    paragraph is bulleted (`indents` holds an indent level or None per \\r-separated paragraph) or a `bold` substring
    occurs, a StyleTextPropAtom."""
    out = oa_record(0x0F9F, struct.pack("<I", kind)) + _ppt_chars(text, as_bytes)
    indents = list(indents) if indents is not None else [None] * (text.count("\r") + 1)
    if any(i is not None for i in indents) or any(w and w in text for w in bold):
        out += _ppt_style(text, indents, bold)
    return out


def ppt_textbox(spid, *children):
    """An SpContainer holding a text-box FSP and a ClientTextbox (0xF00D) of the records `children`."""
    return oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", spid, 0xA00), ver=2, inst=202),
                        oa_container(0xF00D, *children))


def ppt_outline_ref(spid, index):
    """An SpContainer whose ClientData (0xF011) holds an OutlineTextRefAtom (0x0F9E) for text run `index` of the slide."""
    return oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", spid, 0xA00), ver=2, inst=202),
                        oa_container(0xF011, oa_record(0x0F9E, struct.pack("<I", index))))


def ppt_drawing(*shapes, raw=b""):
    """A PPDrawing (0x040C) holding a DgContainer whose SpgrContainer holds the shapes, records already built; the
    bytes `raw` follow the SpgrContainer inside the DgContainer."""
    group = oa_group(1024, *shapes)
    return oa_container(0x040C, oa_container(0xF002, oa_record(0xF008, struct.pack("<II", len(shapes), 1024)), group,
                                             raw))


def ppt_anchor(box, *, child=False, wide=False):
    """An anchor record for `box` = (left, top, right, bottom): a ChildAnchor (0xF00F, four i32 left, top, right,
    bottom) when `child`, else a ClientAnchor (0xF010, top, left, right, bottom as four i16, or i32 when `wide`)."""
    left, top, right, bottom = box
    if child:
        return oa_record(0xF00F, struct.pack("<4i", left, top, right, bottom))
    return oa_record(0xF010, struct.pack("<4i" if wide else "<4h", top, left, right, bottom))


def _ppt_target(spec):
    """The ExHyperlink a make_ppt shape dict names: a string `link`, a click {"url": URL} or {"slide": number} (as
    ("slide", number)); None for none."""
    link, click = spec.get("link"), spec.get("click") or {}
    if isinstance(link, str):
        return link
    if "url" in click:
        return click["url"]
    if "slide" in click:
        return ("slide", click["slide"])
    return None


def ppt_link_urls(specs) -> list:
    """The distinct hyperlink targets (`_ppt_target`) of make_ppt shape dicts, groups' children included, in order of
    first use."""
    out = []
    for spec in specs:
        target = _ppt_target(spec)
        if target is not None and target not in out:
            out.append(target)
        out.extend(u for u in ppt_link_urls(spec.get("children", ())) if u not in out)
    return out


def ppt_media(specs) -> list:
    """The make_ppt "media" shape dicts of `specs`, groups' children included."""
    out = []
    for spec in specs:
        if spec.get("kind") == "media":
            out.append(spec)
        out.extend(ppt_media(spec.get("children", ())))
    return out


def _ppt_interactive(spec, urls):
    """The InteractiveInfo container (0x0FF2) of a shape dict's `link` or `click`: an InteractiveInfoAtom (soundIdRef,
    exHyperlinkIdRef, action, oleVerb, jump, flags, hyperlinkType, 3 unused). A string link or a click url is action 4
    (hyperlink) of type 8 (URL), a click slide action 4 of type 7 (slide number), a click jump action 3 with that jump
    code, a click action that action; any other link names no hyperlink."""
    target = _ppt_target(spec)
    ref = list(urls).index(target) + 1 if target is not None else 0
    click = spec.get("click") or {}
    if "jump" in click:
        action, jump, kind = 3, click["jump"], 0xFF
    elif target is not None:
        action, jump, kind = 4, 0, 7 if isinstance(target, tuple) else 8
    else:
        action, jump, kind = click.get("action", 0), 0, 0xFF
    return oa_container(0x0FF2, oa_record(0x0FF3, struct.pack("<IIBBBBB3x", 0, ref, action, 0, jump, 0, kind)))


def ppt_shape(spec, ids, *, inside=False, urls=()):
    """The SpContainer (or, for a group, SpgrContainer) bytes of one make_ppt `shapes` dict; `ids` is a one-item list
    holding the next spid, `urls` the document's hyperlink URLs (a string `link` is the id k + 1 of its URL). See
    make_ppt for the keys."""
    kind = spec.get("kind", "text")
    spid = spec.get("spid", ids[0])
    ids[0] = max(ids[0], spid) + 1
    box = spec.get("box")
    anchor = ppt_anchor(box, child=inside and not spec.get("client"), wide=bool(spec.get("wide"))) if box else b""
    if kind == "group":
        flags = 0x201 | spec.get("flags", 0)
        head = oa_record(0xF00A, struct.pack("<II", spid, flags), ver=2)
        if spec.get("table") == "tertiary":  # as PowerPoint writes it: the flag in an OfficeArtTertiaryFOPT
            head += oa_record(0xF00B, struct.pack("<HI", 0x007F, 0x10001), ver=3, inst=1)
            head += oa_record(0xF122, struct.pack("<HI", 0x039F, 1), ver=3, inst=1)
        elif spec.get("table"):
            head += oa_record(0xF00B, struct.pack("<HI", 0x039F, 1), ver=3, inst=1)
        if spec.get("rect"):
            head += oa_record(0xF009, struct.pack("<4i", *spec["rect"]))
        head += anchor
        members = [ppt_shape(c, ids, inside=True, urls=urls) for c in spec.get("children", ())]
        return oa_container(0xF003, oa_container(0xF004, head), *members)
    pib = spec.get("pib")
    out = oa_record(0xF00A, struct.pack("<II", spid, 0xA00 | spec.get("flags", 0)), ver=2,
                    inst=spec.get("inst", 75 if pib else 202))
    fopt = (struct.pack("<HI", 0x4104, pib) if pib else b"") + bytes(spec.get("props", b""))
    if fopt:
        out += oa_record(0xF00B, fopt, ver=3, inst=len(fopt) // 6)
    out += anchor
    data = b""
    if kind in ("object", "media"):
        data += oa_record(0x0BC1, struct.pack("<I", spec["ex_obj_id"]))
    if spec.get("placeholder") is not None:
        data += oa_record(0x0BC3, struct.pack("<IBBH", 0, spec["placeholder"], 0, 0))
    if spec.get("ref") is not None:
        data += oa_record(0x0F9E, struct.pack("<I", spec["ref"]))
    if spec.get("link") or spec.get("click"):
        data += _ppt_interactive(spec, urls)
    if data:
        out += oa_container(0xF011, data)
    if spec.get("text") is not None:
        text = ppt_text(spec.get("text_kind", 1), spec["text"], indents=spec.get("indents"))
        if spec.get("link"):
            text += _ppt_interactive(spec, urls) + oa_record(0x0FDF, struct.pack("<II", 0, len(spec["text"])))
        text += b"".join(oa_record(0x0FD8, struct.pack("<I", pos)) for pos in spec.get("fields", ()))
        out += oa_container(0xF00D, text)
    return oa_container(0xF004, out)


def ppt_slide_persist(pid, slide_id, texts=0):
    """A SlidePersistAtom (0x03F3): persistIdRef at +0, flags, numberTexts at +8, slideId at +12, reserved."""
    return oa_record(0x03F3, struct.pack("<IIiII", pid, 0, texts, slide_id, 0))


def ppt_slide_atom(master_id, notes_id, flags=0x0007):
    """A SlideAtom (0x03EF): geometry, 8 placeholder bytes, masterIdRef, notesIdRef, flags (fMasterObjects 0x1,
    fMasterScheme 0x2, fMasterBackground 0x4), unused."""
    return oa_record(0x03EF, struct.pack("<I8sIIHH", 0, bytes(8), master_id, notes_id, flags, 0), ver=2)


def ppt_cstring(text, inst):
    """A CString (0x0FBA) of `text`, UTF-16LE, record instance `inst`."""
    return oa_record(0x0FBA, text.encode("utf-16le"), inst=inst)


def ppt_headers_footers(footer, inst, *, shown=True):
    """A HeadersFooters container (0x0FD9) of record instance `inst` (3 the document's slides, 0 one slide's): a
    HeadersFootersAtom (formatId i16, flags u16 with fHasFooter 0x20 when `shown`) and the footer CString (instance
    2)."""
    return oa_container(0x0FD9, oa_record(0x0FDA, struct.pack("<hH", 0, 0x20 if shown else 0)),
                        ppt_cstring(footer, 2), inst=inst)


def ppt_master_style(inst, bullets):
    """A TextMasterStyleAtom (0x0FA3) of text type `inst`: cLevels u16, then per level (a u16 level first from type 5
    on) a TextPFException (mask 0x1 and bulletFlags 1 or 0; mask 0 for a None level) and an empty TextCFException."""
    body = struct.pack("<H", len(bullets))
    for level, bullet in enumerate(bullets):
        if inst >= 5:
            body += struct.pack("<H", level)
        body += struct.pack("<I", 0) if bullet is None else struct.pack("<IH", 1, int(bullet))
        body += struct.pack("<I", 0)
    return oa_record(0x0FA3, body, inst=inst)


def ppt_media_record(spec):
    """The ExObjList record of a make_ppt "media" shape dict: media ("Video", path) is an ExMCIMovie (0x1007) holding
    an ExVideoContainer (0x1005: ExMediaAtom 0x1004 with exObjId, then the path CString); ("Audio", path) an
    ExWAVAudioLink (0x1010: ExMediaAtom, then the path CString)."""
    kind, path = spec["media"]
    media = oa_record(0x1004, struct.pack("<IHH", spec["ex_obj_id"], 0, 0)) + ppt_cstring(path, 0)
    if kind == "Audio":
        return oa_container(0x1010, media)
    return oa_container(0x1007, oa_container(0x1005, media))


def ppt_user_edit(last, directory, doc_ref, seed, crypt=None):
    """A UserEditAtom (0x0FF5): body lastSlideIdRef, version/minor/major, lastEdit at 8, directory offset at 12,
    docPersistIdRef at 16, persistIdSeed, lastView, unused; encryptSessionPersistIdRef at 28 when `crypt` is given."""
    body = struct.pack("<IHBBIIIIHH", 0x100, 0, 0, 3, last, directory, doc_ref, seed, 1, 0)
    return oa_record(0x0FF5, body + (struct.pack("<I", crypt) if crypt is not None else b""))


def ppt_directory(entries, slots=None):
    """A PersistDirectoryAtom (0x1772) of {persist id: offset}: one run (id | count << 20, then count offsets) per
    stretch of consecutive ids. A `slots` dict is filled with {persist id: offset of its u32 inside the atom}."""
    out, ids, i = b"", sorted(entries), 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1 and j + 1 - i < 0xFFF:
            j += 1
        out += struct.pack("<I", ids[i] | (j - i + 1) << 20)
        for p in ids[i:j + 1]:
            if slots is not None:
                slots[p] = 8 + len(out)
            out += struct.pack("<I", entries[p])
        i = j + 1
    return oa_record(0x1772, out)


def ppt_current_user(edit_offset, *, encrypted=False, name=b"user"):
    """A Current User stream: a CurrentUserAtom (0x0FF6) with size 20 at +8, headerToken at +12 (0xE391C05F, or
    0xF3D1C4DF when encrypted), offsetToCurrentEdit at +16, lenUserName, docFileVersion 0x03F4, version 3.0, the
    ANSI name and relVersion 8."""
    body = struct.pack("<IIIHHBBH", 20, 0xF3D1C4DF if encrypted else 0xE391C05F, edit_offset, len(name), 0x03F4, 3, 0, 0)
    return oa_record(0x0FF6, body + bytes(name) + struct.pack("<I", 8))


def _ppt_encrypt(stream, pic_stream, objects, pid_crypt, password):
    """(PowerPoint Document, Pictures) with the persist objects and BLIPs RC4 CryptoAPI encrypted, 128-bit keys;
    `objects` is every (persist id, offset) written, the older edits' copies too (they keep their id as the block):
    key(block) = SHA-1(SHA-1(salt + UTF-16LE password) + block as u32)[:16], salt = bytes(range(16)) as in rc4_header."""
    from reader.containers.crypto import rc4
    base = hashlib.sha1(bytes(range(16)) + password.encode("utf-16le")).digest()

    def key(block):
        return hashlib.sha1(base + struct.pack("<I", block)).digest()[:16]

    out = bytearray(stream)
    for pid, at in objects:
        if pid == pid_crypt:
            continue
        size = 8 + struct.unpack_from("<I", stream, at + 4)[0]
        out[at:at + size] = rc4(key(pid), bytes(stream[at:at + size]))
    pics, pos = bytearray(pic_stream), 0
    while pos < len(pic_stream):
        options, typ, size = struct.unpack_from("<HHI", pic_stream, pos)
        end = pos + 8 + size
        instance = options >> 4
        fields = [8] + [16] * (2 if instance & 1 else 1) + [34 if typ in (0xF01A, 0xF01B, 0xF01C) else 1]
        fields.append(end - pos - sum(fields))
        for n in fields:
            pics[pos:pos + n] = rc4(key(0), bytes(pic_stream[pos:pos + n]))
            pos += n
    return out, pics


def _ppt_object(item, k):
    """One make_ppt `objects` item as {"id", "compressed", "body"}: the ExOleObjStg body."""
    if isinstance(item, (bytes, bytearray)):
        item = {"data": bytes(item)}
    elif isinstance(item, tuple):
        item = {"data": bytes(item[0]), **item[1]}
    compressed = bool(item.get("compressed"))
    body = item.get("body")
    if body is None:
        data = item["data"]
        body = struct.pack("<I", len(data)) + zlib.compress(data) if compressed else data
    return {"id": item.get("ex_obj_id", k + 1), "compressed": compressed, "body": bytes(body)}


def make_ppt(path, slides, *, notes=None, master_text="Click to edit", pictures=(), objects=(), encrypt=None,
             edits=1, raw_shapes=None, no_pictures_stream=False, encrypt_header=None, master=None):
    """Write a .ppt compound file with PowerPoint Document, Current User and Pictures streams.

    slides: dicts with title (str), body (list of str paragraphs, or (text, indent) bulleted ones), bold (substrings
    given a bold character run in the slide's text), hidden (a SlideShowSlideInfoAtom with fHidden 0x0004 at +10),
    outline (the text sits in the SlideListWithText after the slide's SlidePersistAtom and the slide's shapes hold
    OutlineTextRefAtoms with the run's index in that list; otherwise a ClientTextbox in the slide's PPDrawing),
    text_bytes (TextBytesAtom, Latin-1, instead of TextCharsAtom), extra (a list of (TextHeaderAtom type, text) runs
    added to the SLWT after the body, when outline is true) and shapes (below). Paragraphs are joined with \\r;
    \\x0b stays as written. notes: {slide number from 1: text}, NotesContainers listed in SlideListWithText instance 2
    (SlidePersistAtom slideId = the slide's slideId + 0x1000, which the SlideAtom's notesIdRef carries; NotesAtom
    slideIdRef = the slide's slideId; a value may be a dict {"shapes": [...]} of shape dicts instead of text).
    master_text goes in the MainMaster's title placeholder (OEPlaceholderAtom type 1; None: a master with no text).
    master: {"shapes": shape dicts drawn on the MainMaster beside it, "styles": {text type: [bullet True, False or
    None per indent level]} as its TextMasterStyleAtoms, "footer": text of the document's slide HeadersFooters
    (instance 3, fHasFooter)}. A slide's footer key gives it its own HeadersFooters (instance 0) with that footer and
    no_master clears its SlideAtom's fMasterObjects. edits > 1 appends edits - 1 more
    PersistDirectoryAtom + UserEditAtom pairs chained through lastEdit; each remaps slide 1 to a fresh copy.
    encrypt (any string, the password): the CurrentUserAtom headerToken 0xF3D1C4DF, a CryptSession10Container persist
    object holding an RC4 CryptoAPI EncryptionVersionInfo (rc4_header(password), 128-bit; clear) and the UserEditAtom's
    encryptSessionPersistIdRef. Every other persist object, the older edits' copies too, is RC4-encrypted whole (header
    and body) with block = its persist id; the CryptSession, UserEditAtom, PersistDirectoryAtom and Current User stay
    clear. The Pictures stream is encrypted BLIP by BLIP: the 8-byte header, each UID, the tag or metafile header and
    the image data are separate fields, each restarting the keystream at block 0 ([MS-OFFCRYPTO] / dochan
    crypto/ppt.py). The encryption here is written out on its own (its own key derivation), not by running the reader's
    unlock backwards.
    encrypt_header (bytes) replaces the CryptSession10Container body, for example a (4, 4) AES EncryptionVersionInfo;
    the streams are still encrypted with the password's RC4 keys.
    no_drawing (true) writes the title and body runs straight into the SlideContainer, without a PPDrawing, and the
    slide's notes likewise.
    shapes: a list of dicts that replaces the slide's own text shapes in its PPDrawing (the SLWT is unchanged). A dict
    is a text shape, kind "group" or kind "picture" (key pib, the 1-based BStore index; box, text, placeholder and ref
    as for a text shape); kind "object" (below). Keys: kind ("text" by default), spid, inst (the FSP's shape type, 202
    or 75 with a pib by default), flags (FSP flag bits added to 0xA00, 0x40 flipH, 0x80 flipV), props (6-byte FOPT
    properties after the pib, for example 0x01D1 lineEndArrowhead), box ((left, top, right, bottom):
    a ClientAnchor at the top level, a ChildAnchor inside a group, unless client is true; wide makes the ClientAnchor 16
    bytes), text and text_kind (a text run of TextHeaderAtom type text_kind, 1 by default), placeholder (an
    OEPlaceholderAtom of that type in the ClientData), ref (an OutlineTextRefAtom index in the ClientData), link (the
    text carries an InteractiveInfo and a TextInteractiveInfoAtom; a string link is also a URL: an ExHyperlink
    (ExHyperlinkAtom, CString) in the ExObjList of the DocumentContainer, named by the InteractiveInfoAtom), click (the
    shape's own InteractiveInfo in its ClientData: {"url": URL}, {"slide": number} an ExHyperlink whose location
    CString (instance 3) is "slideId,number,Slide number", {"jump": code} or {"action": code}), indents (as ppt_text's,
    for the shape's text), fields (positions in the text of SlideNumberMCAtoms, 0x0FD8). Kind "media" (keys media
    ("Video" or "Audio", file path), ex_obj_id, pib, box) is a movie or sound shape: an ExObjRefAtom in its ClientData
    naming a media record in the ExObjList. A group
    also has children (the shape dicts inside it), rect (its FSPGR rectangle, left, top, right, bottom), table (FOPT
    property 0x039F = 1; "tertiary" puts it in a tertiary FOPT, 0xF122) and flags (extra FSP flag bits, 0x40 flipH, 0x80
    flipV).
    raw_shapes: {slide number from 1: OfficeArt record bytes} appended to that slide's DgContainer after its
    SpgrContainer. pictures: a list of BLIP records (blip_png, blip_bitmap, blip_metafile), written one after
    another into the Pictures stream; the DocumentContainer gains a PPDrawingGroup (0x040B) whose DggContainer holds a
    BStore with one FBSE per picture, its foDelay that BLIP's offset in Pictures. no_pictures_stream (true) leaves the
    Pictures stream out of the compound file, the BStore as it is.
    objects: embedded OLE objects, each compound-file bytes, a tuple (bytes, options) or a dict. Each becomes an
    ExOleObjStg persist object (0x1011) named by an ExEmbed (ExEmbedAtom, then an ExOleObjAtom with exObjId and
    persistIdRef) in an ExObjList of the DocumentContainer. The dict keys are data (the compound file), compressed
    (instance 1: a u32 size and zlib data; else instance 0, the raw bytes), ex_obj_id (k + 1 for the k-th object by
    default) and body (bytes that replace the persist object's whole body as written, for example garbage zlib data;
    with compressed it is written as given, size field included). A tuple's options are such a dict.
    An object shape (kind "object") has ex_obj_id (an ExObjRefAtom in its ClientData), pib (its preview picture,
    optional) and the box and text keys of a text shape.

    Returns a dict: doc, master (persist ids), slides (persist ids in order), slide_ids, notes ({slide number:
    persist id}), crypt (persist id or None), offsets ({persist id: offset} as the newest edit sees them),
    slide1_offsets ((first, last) offset of slide 1), slots ({persist id: stream offset of the u32 holding that
    id's offset in the newest directory naming it; patch it to move or break one object}), user_edits and
    directories (offsets in the PowerPoint Document stream, oldest first), doc_stream and current_user (the stream
    bytes)."""
    raw_shapes = dict(raw_shapes or {})
    notes = dict(notes or {})
    n = len(slides)
    pid_doc, pid_master = 1, 2
    pid_slides = [3 + k for k in range(n)]
    pid_notes = {num: 3 + n + i for i, num in enumerate(sorted(notes))}
    pid_crypt = 3 + n + len(notes) if encrypt is not None else None
    objects = [_ppt_object(o, k) for k, o in enumerate(objects)]
    master = dict(master or {})
    every = [s for slide in slides for s in slide.get("shapes", ())] + list(master.get("shapes", ()))
    urls = ppt_link_urls(every)
    media = ppt_media(every)
    pid_objects = [3 + n + len(notes) + (pid_crypt is not None) + k for k in range(len(objects))]
    slide_ids = [256 + k for k in range(n)]
    notes_slide_id = {num: slide_ids[num - 1] + 0x1000 for num in notes}
    master_slide_id = 0x80000000

    def runs(slide):
        """The slide's text runs as record bytes: the title (kind 0), then the body (kind 1)."""
        bold, as_bytes = slide.get("bold", ()), bool(slide.get("text_bytes"))
        out = []
        if slide.get("title"):
            out.append(ppt_text(0, slide["title"], bold=bold, as_bytes=as_bytes))
        body = slide.get("body") or []
        if body:
            texts = [b if isinstance(b, str) else b[0] for b in body]
            indents = [None if isinstance(b, str) else b[1] for b in body]
            out.append(ppt_text(1, "\r".join(texts), indents=indents, bold=bold, as_bytes=as_bytes))
        if slide.get("outline"):
            out.extend(ppt_text(kind, text) for kind, text in slide.get("extra", ()))
        return out

    def slide_record(k):
        slide = slides[k]
        parts = [ppt_slide_atom(master_slide_id, notes_slide_id.get(k + 1, 0),
                                0x0006 if slide.get("no_master") else 0x0007)]
        if slide.get("footer") is not None:
            parts.append(ppt_headers_footers(slide["footer"], 0))
        if slide.get("hidden"):
            parts.append(oa_record(0x03F9, struct.pack("<iIBBHB3x", 0, 0, 0, 0, 0x0004, 0)))
        if slide.get("no_drawing"):  # the text runs sit in the SlideContainer itself, with no PPDrawing
            return oa_container(0x03EE, *parts, *runs(slide))
        if slide.get("shapes"):
            ids = [1025]
            shapes = [ppt_shape(spec, ids, urls=urls) for spec in slide["shapes"]]
        elif slide.get("outline"):
            shapes = [ppt_outline_ref(1025 + i, i) for i in range(len(runs(slide)))]
        else:
            shapes = [ppt_textbox(1025 + i, rec) for i, rec in enumerate(runs(slide))]
        return oa_container(0x03EE, *parts, ppt_drawing(*shapes, raw=raw_shapes.get(k + 1, b"")))

    def notes_record(num):
        head = oa_record(0x03F1, struct.pack("<IHH", slide_ids[num - 1], 0, 0))
        if isinstance(notes[num], dict):  # {"shapes": [shape dicts]}: the notes page's own shapes
            ids = [1025]
            return oa_container(0x03F0, head, ppt_drawing(*[ppt_shape(s, ids) for s in notes[num]["shapes"]]))
        if slides[num - 1].get("no_drawing"):  # the notes text sits in the NotesContainer itself
            return oa_container(0x03F0, head, ppt_text(2, notes[num]))
        return oa_container(0x03F0, head,
                            ppt_drawing(ppt_textbox(1025, ppt_text(2, notes[num]))))

    def slide_list(inst, atoms):
        return oa_container(0x0FF0, *atoms, inst=inst)

    slwt = []
    for k in range(n):
        outline = runs(slides[k]) if slides[k].get("outline") else []
        slwt.append(ppt_slide_persist(pid_slides[k], slide_ids[k], len(outline)))
        slwt.extend(outline)
    doc_parts = [oa_record(0x03E9, bytes(40), ver=1), slide_list(0, slwt),
                 slide_list(1, [ppt_slide_persist(pid_master, master_slide_id)])]
    pic_stream, store = bytearray(), []
    for blip in pictures:
        store.append(oa_fbse(delay=len(pic_stream)))
        pic_stream.extend(blip)
    if pictures:
        dgg = oa_container(0xF000, oa_record(0xF006, struct.pack("<4I", 1026, 2, 1, 1)),
                           oa_container(0xF001, *store, inst=len(store)))
        doc_parts.append(oa_container(0x040B, dgg))
    if objects or urls or media:
        embeds = [oa_container(0x0FCC, oa_record(0x0FCD, bytes(8)),
                               oa_record(0x0FC3, struct.pack("<5I", 1, 1, o["id"], 0, pid)))
                  for o, pid in zip(objects, pid_objects)]
        links = [oa_container(0x0FD7, oa_record(0x0FD3, struct.pack("<I", k + 1)),
                              ppt_cstring(f"{slide_ids[url[1] - 1]},{url[1]},Slide {url[1]}", 3)
                              if isinstance(url, tuple) else ppt_cstring(url, 1)) for k, url in enumerate(urls)]
        doc_parts.append(oa_container(0x0409, oa_record(0x040A, struct.pack("<I", len(objects) + 1)), *embeds, *links,
                                      *[ppt_media_record(m) for m in media]))
    if master.get("footer") is not None:
        doc_parts.append(ppt_headers_footers(master["footer"], 3))
    if notes:
        doc_parts.append(slide_list(2, [ppt_slide_persist(pid_notes[num], notes_slide_id[num]) for num in sorted(notes)]))
    stream, offsets = bytearray(), {}

    written = []  # (persist id, offset) of every persist object, older edits' copies included

    def put(pid, record):
        at = len(stream)
        if pid is not None:
            offsets[pid] = at
            written.append((pid, at))
        stream.extend(record)
        return at

    put(pid_doc, oa_container(0x03E8, *doc_parts))
    ids = [1025]
    drawn = [ppt_shape({"text": master_text, "text_kind": 0, "placeholder": 1}, ids)] if master_text is not None else []
    drawn += [ppt_shape(spec, ids, urls=urls) for spec in master.get("shapes", ())]
    styles = [ppt_master_style(inst, bullets) for inst, bullets in sorted(master.get("styles", {}).items())]
    put(pid_master, oa_container(0x03F8, ppt_slide_atom(0, 0), *styles, *([ppt_drawing(*drawn)] if drawn else [])))
    for k in range(n):
        put(pid_slides[k], slide_record(k))
    for num in sorted(notes):
        put(pid_notes[num], notes_record(num))
    if pid_crypt is not None:
        put(pid_crypt, oa_record(0x2F14, encrypt_header if encrypt_header is not None else rc4_header(encrypt)))
    for o, pid in zip(objects, pid_objects):
        put(pid, oa_record(0x1011, o["body"], inst=1 if o["compressed"] else 0))
    first_slide = offsets.get(pid_slides[0]) if n else None
    user_edits, directories, last, slots = [], [], 0, {}
    seed = 3 + n + len(notes) + (pid_crypt is not None) + len(objects)
    for e in range(max(edits, 1)):
        changed = dict(offsets) if e == 0 else {}
        if e and n:
            offsets[pid_slides[0]] = changed[pid_slides[0]] = put(None, slide_record(0))
            written.append((pid_slides[0], offsets[pid_slides[0]]))
        rel = {}
        directories.append(put(None, ppt_directory(changed, rel)))
        slots.update({p: directories[-1] + r for p, r in rel.items()})
        last = put(None, ppt_user_edit(last, directories[-1], pid_doc, seed, pid_crypt))
        user_edits.append(last)
    cu = ppt_current_user(last, encrypted=encrypt is not None)
    if encrypt is not None:
        stream, pic_stream = _ppt_encrypt(stream, pic_stream, written, pid_crypt, encrypt)
    streams = {"PowerPoint Document": bytes(stream), "Current User": cu}
    if not no_pictures_stream:
        streams["Pictures"] = bytes(pic_stream)
    make_cfb(path, streams)
    return {"doc": pid_doc, "master": pid_master, "slides": pid_slides, "slide_ids": slide_ids, "notes": pid_notes,
            "crypt": pid_crypt, "offsets": offsets, "slide1_offsets": (first_slide, offsets.get(pid_slides[0])),
            "slots": slots, "user_edits": user_edits, "directories": directories, "doc_stream": bytes(stream), "current_user": cu}


# -- Visio binary (vsd): the VisioDocument stream, laid out as libvisio reads it (VSDParser, VSD6Parser, VSD5Parser)

VSD_MAGIC = b"Visio (TM) Drawing\r\n"
# where VSD5Parser::readPointerInfo finds a v5 list's s16 pointer count, by the list's pointer type
_VSD5_COUNT = {0x14: 0x82, 0x15: 0x42, 0x18: 0x2E, 0x1A: 0x12, 0x1D: 0x1E, 0x4E: 0x1E, 0x1E: 0x36}


def lz77(data: bytes) -> bytes:
    """Visio's LZ77: a flag byte per eight items, low bit first; bit 1 is a literal byte, bit 0 a two-byte back
    reference (12-bit window slot, 4-bit length - 3) into a 4,096-byte window. Greedy, one candidate per three-byte
    key, so it is linear; a match may overlap the bytes it produces."""
    items, last, i, n = [], {}, 0, len(data)
    while i < n:
        length = 0
        j = last.get(data[i:i + 3]) if i + 3 <= n else None
        if j is not None and i - j <= 4096:
            while length < 18 and i + length < n and data[j + length] == data[i + length]:
                length += 1
        if length >= 3:
            slot = (j - 18) & 4095  # the decoder maps slot s to s + 18 (s <= 4078) or s - 4078, which is j mod 4096
            items.append(bytes([slot & 0xFF, (slot >> 8) << 4 | (length - 3)]))
        else:
            length = 1
            items.append(data[i:i + 1])
        for k in range(i, i + length):
            if k + 3 <= n:
                last[data[k:k + 3]] = k
        i += length
    out = bytearray()
    for g in range(0, len(items), 8):
        group = items[g:g + 8]
        out.append(sum(1 << b for b, item in enumerate(group) if len(item) == 1))
        out += b"".join(group)
    return bytes(out)


def vsd_trailer(ctype, lst, level, unknown, version) -> int:
    """The trailer bytes after a chunk's data: VSDParser::getChunkHeader (v11) or VSD6Parser's (v6); v5 has none."""
    if version == 5:
        return 0
    if version == 6:
        eight = {0x76, 0x73, 0x72, 0x71, 0x70, 0x6F, 0x6E, 0x6D, 0x6C, 0x6B, 0x6A, 0x69, 0x68, 0x67, 0x66, 0x65, 0x64,
                 0x2C, 0x0D}
        return 0 if ctype in (0x1F, 0xC9) else 8 if lst or ctype in eight else 0
    n = 8 if lst or ctype in (0x71, 0x70, 0x6B, 0x6A, 0x69, 0x66, 0x65, 0x2C) else 0
    if lst or (level == 2 and unknown == 0x55) or (level == 2 and unknown == 0x54 and ctype == 0xAA) \
            or (level == 3 and unknown not in (0x50, 0x54)):
        n += 4
    four = (0x64, 0x65, 0x66, 0x69, 0x6A, 0x6B, 0x6F, 0x71, 0x92, 0xA9, 0xB4, 0xB6, 0xB9, 0xC7)
    if ctype in four and n not in (12, 4):
        n += 4
    return 0 if ctype in (0x1F, 0xC9, 0x2D, 0xD1) else n


def vsd_chunk(ctype, data, *, version=11, cid=0, list_=0, level=0) -> bytes:
    """One chunk: a 19-byte header (type, id, list, dataLength u32, level u16, unknown u8 = 0) or v5's 12-byte one
    (type s16, id s16, level u8, unknown u8, list s16, dataLength u32), the data and the trailer, filled with 0xFF
    (a reader skips zero bytes before a header, so a zero trailer would hide a trailer rule it gets wrong)."""
    if version == 5:
        head = struct.pack("<HHBBHI", ctype & 0xFFFF, cid & 0xFFFF, level, 0, list_ & 0xFFFF, len(data))
    else:
        head = struct.pack("<IIIIHB", ctype, cid, list_, len(data), level, 0)
    return head + bytes(data) + b"\xff" * vsd_trailer(ctype, list_, level, 0, version)


def vsd_pointer(ptype, offset, length, fmt, *, version=11) -> bytes:
    """An 18-byte pointer (type u32, 4 skipped, offset u32, length u32, format u16) or v5's 16-byte one (type u16,
    format u16, 4 skipped, offset u32, length u32)."""
    if version == 5:
        return struct.pack("<HHIII", ptype, fmt, 0, offset, length)
    return struct.pack("<IIIIH", ptype, 0, offset, length, fmt)


def vsd_list(pointers, *, version=11, ptype=0x14, blob=b"", order=(), count=None) -> bytes:
    """A pointer list stream as readPointerInfo reads it (before any compression prefix). v6/v11: u32 at 0 = the
    list's offset + 4, `blob` from 4, then listSize u32, pointerCount s32, 4 skipped, the pointers and `order` (u32
    indexes; libvisio reads them only when there are two or more). v5: `blob` from 0, the s16 count at the type's
    fixed place, then the pointers. `pointers` are `vsd_pointer` bytes or (type, offset, length, format) tuples;
    `count` overrides the declared pointer count."""
    raw = b"".join(p if isinstance(p, bytes) else vsd_pointer(*p, version=version) for p in pointers)
    n = len(pointers) if count is None else count
    if version == 5:
        at = _VSD5_COUNT.get(ptype, 0x1E if ptype > 0x45 else 0x0A)
        head = bytearray(at)
        head[:len(blob)] = blob[:at]
        return bytes(head) + struct.pack("<h", n) + raw
    return (struct.pack("<I", 8 + len(blob)) + bytes(blob) + struct.pack("<IiI", len(order), n, 0) + raw
            + b"".join(struct.pack("<I", k) for k in order))


def vsd_document(version, pages, *, masters=(), trailer_extra=()) -> bytes:
    """A VisioDocument stream: the header (version at 0x1A, size at 0x1C, the trailer pointer at 0x24) and its
    pointer tree. The trailer list (type 0x14, format 0x50) holds the pages list (0x27), the stencils list (0x1D,
    when `masters` is given) and `trailer_extra`. A page is a list of shape dicts or a dict with "shapes" and the
    optional "background" (format bit 0 clear), "compressed" and "blob" (the bytes before its pointer list); a page
    pointer is type 0x15, format 0x50 or 0x51. Each master is a list of shape dicts under a stencil page pointer
    (0x1E). A shape dict gives its chunk stream (format 0xD0): "type" (0x48; 0x47 group, 0x4E foreign), "data" (the
    shape chunk's data, 0x30 zero bytes), "cid" (its chunk id), "text" (a 0x0E chunk: 8 zero bytes, then UTF-16LE in
    v11 or cp1252 in v5 and v6), "chunks" (more chunks: bytes, or (type, data) or (type, data, vsd_chunk keywords))
    and "compressed" (the stream LZ77-coded, format bit 1 set). A coded list or blob holds four zero bytes before its
    content (libvisio's shift 4); a coded chunk stream starts with its first chunk, as in real files.
    `trailer_extra` items are (type, format, stream bytes), placed as given, or a (type, offset, length, format)
    pointer used as it is."""
    body = bytearray(0x40)

    def put(raw, ptype, fmt, compressed=False):
        if compressed:
            raw, fmt = lz77(raw if fmt >> 4 in (0x8, 0xC, 0xD) else bytes(4) + raw), fmt | 2
        body.extend(raw)
        return ptype, len(body) - len(raw), len(raw), fmt

    def shape(s):
        typ = s.get("type", 0x48)
        parts = [vsd_chunk(typ, s.get("data", bytes(0x30)), version=version, cid=s.get("cid", 0), level=1)]
        if "text" in s:
            text = s["text"].encode("utf-16-le" if version == 11 else "cp1252")
            parts.append(vsd_chunk(0x0E, bytes(8) + text, version=version, level=2))
        for c in s.get("chunks", ()):
            parts.append(c if isinstance(c, bytes) else
                         vsd_chunk(c[0], c[1], version=version, **{"level": 2, **(c[2] if len(c) > 2 else {})}))
        return put(b"".join(parts), typ, 0xD0, s.get("compressed", False))

    def listed(ptype, fmt, children, blob=b"", compressed=False):
        return put(vsd_list(children, version=version, ptype=ptype, blob=blob), ptype, fmt, compressed)

    page_ptrs = []
    for page in pages:
        page = {"shapes": page} if isinstance(page, (list, tuple)) else page
        shapes = [shape(s) for s in page["shapes"]]
        fmt = 0x50 if page.get("background") else 0x51
        page_ptrs.append(listed(0x15, fmt, shapes, page.get("blob", bytes(8)), page.get("compressed", False)))
    top = [listed(0x27, 0x50, page_ptrs)]
    if masters:
        stencils = [listed(0x1E, 0x50, [shape(s) for s in m]) for m in masters]
        top.append(listed(0x1D, 0x50, stencils))
    for item in trailer_extra:
        top.append(tuple(item) if len(item) == 4 else put(item[2], item[0], item[1]))
    trailer = listed(0x14, 0x50, top)
    return vsd_head(version, trailer, body)


def vsd_head(version, trailer, body) -> bytes:
    """`body` (whose first 0x40 bytes are the header's room) with the header written over its start: the magic, the
    version at 0x1A, the size at 0x1C and the `trailer` (type, offset, length, format) pointer at 0x24."""
    head = VSD_MAGIC + bytes(0x1A - len(VSD_MAGIC)) + bytes([version, 0]) + struct.pack("<I", len(body))
    head += bytes(0x24 - len(head)) + vsd_pointer(*trailer, version=version)
    return head + bytes(body[len(head):])


# -- Publisher (pub): Contents blocks and directory, Quill bits (libmspub MSPUBParser layouts; POI QuillContents)

_PUB_FIXED = {0x78: 0, 0x05: 0, 0x08: 0, 0x0A: 0, 0x10: 2, 0x12: 2, 0x18: 2, 0x1A: 2, 0x07: 2, 0x20: 4, 0x22: 4,
              0x58: 4, 0x68: 4, 0x70: 4, 0xB8: 4, 0x28: 8, 0x38: 16, 0x48: 24}
_PUB_VARIABLE = (0xC0, 0x80, 0x82, 0x88, 0x8A, 0x90, 0x98, 0xA0)


def pub_block(bid, btype, data=b"") -> bytes:
    """One Contents block: id, type and its data; a variable type's data gets its u32 length (counting itself), a
    fixed type's int data is packed to the type's length."""
    if btype in _PUB_VARIABLE:
        return bytes([bid, btype]) + struct.pack("<I", len(data) + 4) + bytes(data)
    n = _PUB_FIXED.get(btype, 0)
    raw = data.to_bytes(n, "little") if isinstance(data, int) else bytes(data)
    assert len(raw) == n, (btype, len(raw))
    return bytes([bid, btype]) + raw


def pub_contents(chunks, *, version=0x2C) -> bytes:
    """A Contents stream: `E8 AC <version> 00`, each chunk of `chunks` ((seqnum, ctype, parent, blocks) tuples,
    `blocks` the chunk's block bytes, parent None for none) written as a u32 length and its blocks, then the trailer.
    Version 0x2C: the trailer offset at 0x1A, the trailer a u32 length and three blocks, the third a 0x90 directory
    whose block k is sequence number k (0x88 references with sub-blocks 0x02 type, 0x04 offset, 0x05 parent; a
    sequence number no chunk names is a DUMMY block). Version 0x22 (Publisher 2000): the trailer offset at 0x16, the
    trailer a u16 count and 10-byte entries (2 bytes, seqnum, parent, offset), each chunk starting with its type u16
    (parent None is 0xFFFF)."""
    body = bytearray(b"\xE8\xAC" + bytes([version, 0]) + bytes(0x40 - 4))
    placed = []
    for seq, ctype, parent, blocks in chunks:
        placed.append((seq, ctype, parent, len(body)))
        if version == 0x22:
            body += struct.pack("<H", ctype) + bytes(blocks)
        else:
            body += struct.pack("<I", len(blocks) + 4) + bytes(blocks)
    toff = len(body)
    if version == 0x22:
        body += struct.pack("<H", len(placed))
        for seq, ctype, parent, off in placed:
            body += struct.pack("<HHHI", 0, seq, 0xFFFF if parent is None else parent, off)
        struct.pack_into("<I", body, 0x16, toff)
        return bytes(body)
    by_seq = {p[0]: p for p in placed}
    entries = bytearray()
    for k in range(max(by_seq, default=-1) + 1):
        if k not in by_seq:
            entries += pub_block(0, 0x78)
            continue
        _, ctype, parent, off = by_seq[k]
        subs = pub_block(0x02, 0x18, ctype) + pub_block(0x04, 0xB8, off)
        if parent is not None:
            subs += pub_block(0x05, 0x68, parent)
        entries += pub_block(0, 0x88, subs)
    trailer = pub_block(1, 0x20, 0) + pub_block(2, 0x20, 0) + pub_block(3, 0x90, bytes(entries))
    body += struct.pack("<I", len(trailer) + 4) + trailer
    struct.pack_into("<I", body, 0x1A, toff)
    return bytes(body)


def _quill_plc(count, ptype, pre, values) -> bytes:
    return struct.pack("<II", count, ptype) + bytes(pre) + b"".join(struct.pack("<I", v) for v in values)


def _quill_links(urls) -> bytes:
    """POI's PLC type 12 (QCPLCBit.Type12): count, type, the starts, then the URL list where POI looks for it."""
    n = len(urls)
    head = struct.pack("<II", n, 12) + struct.pack(f"<{n + 2}I", 0x1FFFF, *range(0, 10 * n, 10), 10 * n)
    until = 0x4C if n == 1 else 0x68 + (n - 2) * 22
    head += bytes(until - len(head))
    return head + b"".join(struct.pack("<H", len(u)) + u.encode("utf-16-le") for u in urls)


def _quill_fdpc(text_off, total, bold) -> bytes:
    """An FDPC bit: runs covering [0, total) characters, the `bold` (start, end) ranges bold, as libmspub's
    parseCharacterStyles reads them (u16 count, 6 bytes, u32 run ends as stream offsets, u16 style offsets)."""
    cuts = sorted({0, total, *(c for r in bold for c in r)})
    runs = [(b, any(s <= a and b <= e for s, e in bold)) for a, b in zip(cuts, cuts[1:]) if b > a]
    n = len(runs)
    styles_at = 8 + 6 * n
    plain, strong = styles_at, styles_at + 4
    out = struct.pack("<H", n) + bytes(6)
    out += b"".join(struct.pack("<I", text_off + 2 * end) for end, _ in runs)
    out += b"".join(struct.pack("<H", strong if b else plain) for _, b in runs)
    return out + struct.pack("<I", 4) + struct.pack("<I", 6) + bytes([0x02, 0x0A])


def _quill_fdpp(text_off, runs) -> bytes:
    """An FDPP bit (FDPC's layout): paragraph runs (end, default character style index), each style a u32 length and,
    unless the index is None, block 0x19 (PARAGRAPH_DEFAULT_CHAR_STYLE) holding the index."""
    n = len(runs)
    styles = b""
    at = []
    for _, index in runs:
        at.append(8 + 6 * n + len(styles))
        body = b"" if index is None else pub_block(0x19, 0x20, index)
        styles += struct.pack("<I", 4 + len(body)) + body
    out = struct.pack("<H", n) + bytes(6)
    out += b"".join(struct.pack("<I", text_off + 2 * end) for end, _ in runs)
    return out + b"".join(struct.pack("<H", a) for a in at) + styles


def _quill_stsh(defaults) -> bytes:
    """The second STSH bit as libmspub's parseDefaultStyle reads it: a u32, the count at +4, 12 bytes, the count's u32
    offsets from +20, each style at +20 + offset past a u16; even entries are the default character styles (bold for
    each true flag of `defaults`), odd ones paragraph styles (empty)."""
    styles = []
    for flag in defaults:
        styles.append(bytes(2) + (struct.pack("<I", 6) + bytes([0x02, 0x0A]) if flag else struct.pack("<I", 4)))
        styles.append(bytes(2) + struct.pack("<I", 4))
    count = len(styles)
    offsets, body = [], b""
    for s in styles:
        offsets.append(4 * count + len(body))
        body += s
    return struct.pack("<II", 0, count) + bytes(12) + b"".join(struct.pack("<I", o) for o in offsets) + body


def pub_quill(stories, *, bold=(), tcd=None, links=(), fdpc=True, cycle=False, fdpp=None, stsh=None) -> bytes:
    """A Quill/QuillSub/CONTENTS stream: `CHNKINK `, the bit directory at 0x18 (24-byte entries: 0x18, name, id,
    two u16, kind, offset, length) and the bits from 0x200: TEXT (the `stories` texts, (text id, text) pairs, in
    UTF-16LE), STRS (PLC type 8, lengths from +12), SYID (ids from +8), FDPC when `fdpc` (bold over the `bold`
    (start, end) character ranges of the whole text), a `TCD ` bit (PLC type 0, id = the story index) for each
    {story index: cell boundaries} of `tcd`, written as the cell ends from +0xC (boundaries [0, 2, 5] are ends
    [2, 5]), and a TOKN PLC type 12 bit holding `links`. `cycle`: the directory is two lists, the second naming the
    first as next. `fdpp`: (end, default character style index or None) paragraph runs, as an FDPP bit; `stsh`: the
    bold flags of the default character styles, as the second of three STSH bits, as Publisher writes them (the
    first holds none, the third as many styles as the second, none bold)."""
    text = "".join(t for _, t in stories).encode("utf-16-le")
    total = len(text) // 2
    data_at = 0x200
    bits = [("TEXT", "TEXT", 0, text)]
    bits.append(("STRS", "PLC ", 0, _quill_plc(len(stories), 8, struct.pack("<I", 255),
                                               [len(t) for _, t in stories])))
    bits.append(("SYID", "SYID", 0, struct.pack("<II", 8, len(stories)) +
                 b"".join(struct.pack("<I", i) for i, _ in stories)))
    if fdpc:
        bits.append(("FDPC", "FDPC", 0, _quill_fdpc(data_at, total, bold)))
    if fdpp is not None:
        bits.append(("FDPP", "FDPP", 0, _quill_fdpp(data_at, fdpp)))
    if stsh is not None:
        bits.append(("STSH", "STSH", 0, _quill_stsh(())))
        bits.append(("STSH", "STSH", 1, _quill_stsh(stsh)))
        bits.append(("STSH", "STSH", 2, _quill_stsh([False] * len(stsh))))
    for index, bounds in sorted((tcd or {}).items()):
        ends = list(bounds[1:])
        bits.append(("TCD ", "PLC ", index, _quill_plc(len(ends) - 1, 0, struct.pack("<I", 0xFF00), ends)))
    if links:
        bits.append(("TOKN", "PLC ", 0, _quill_links(links)))
    data = bytearray()
    entries = []
    for name, kind, bid, raw in bits:
        entries.append(struct.pack("<H4sHHH4sII", 0x18, name.encode("latin-1"), bid, 1, 0, kind.encode("latin-1"),
                                   data_at + len(data), len(raw)))
        data += raw
    out = bytearray(b"CHNKINK " + bytes(0x18 - 8))
    if cycle:
        half = max(1, len(entries) // 2)
        second = 0x18 + 8 + 24 * half
        out += struct.pack("<HHI", 0, half, second) + b"".join(entries[:half])
        out += struct.pack("<HHI", 0, len(entries) - half, 0x18) + b"".join(entries[half:])
    else:
        out += struct.pack("<HHI", 0, len(entries), 0xFFFFFFFF) + b"".join(entries)
    assert len(out) <= data_at, len(out)
    return bytes(out + bytes(data_at - len(out)) + data)


def _pub_values(pairs) -> bytes:
    """Publisher's Escher values (libmspub extractEscherValues): 4 bytes (the length once more), then (u16 id, u32
    value) pairs."""
    return bytes(4) + b"".join(struct.pack("<HI", k, v & 0xFFFFFFFF) for k, v in pairs)


def pub_sp(seq=None, box=None, *, child=None, group=None, pib=None, spid=0, props=b"", inst=0, flags=None) -> bytes:
    """A Publisher SpContainer (0xF004): its FSP (instance the shape type `inst`, spid `spid`, grfPersistent `flags`,
    by default 0x201 for a group and 0xA00 otherwise); an FSPGR (0xF009) with the child coordinate system
    `group` (xs, ys, xe, ye) when given; an FOPT (0xF00B) holding `pib` (0x4104) when given, then the 6-byte
    properties `props` (opid u16, op u32 each), when either is given; a ClientAnchor (0xF010) of the `box` (xs, ys, xe,
    ye, as values 0x2001-0x2004), or a ChildAnchor (0xF00F) of the `child` box in its group's coordinates; a ClientData
    (0xF011) naming the Contents shape seqnum `seq` (value 0x6801)."""
    if flags is None:
        flags = 0x201 if group is not None else 0xA00
    rec = oa_record(0xF00A, struct.pack("<II", spid, flags), ver=2, inst=inst)
    if group is not None:
        rec += oa_record(0xF009, struct.pack("<4i", *group), ver=1)
    fopt = (struct.pack("<HI", 0x4104, pib) if pib is not None else b"") + bytes(props)
    if fopt:
        rec += oa_record(0xF00B, fopt, ver=3, inst=len(fopt) // 6)
    if box is not None:
        rec += oa_record(0xF010, _pub_values(zip((0x2001, 0x2002, 0x2003, 0x2004), box)))
    if child is not None:
        rec += oa_record(0xF00F, struct.pack("<4i", *child))
    if seq is not None:
        rec += oa_record(0xF011, _pub_values([(0x6801, seq)]))
    return oa_container(0xF004, rec)


def pub_group(leader, *members) -> bytes:
    """A Publisher SpgrContainer (0xF003): the group's own shape `leader` (pub_sp with `group`), then `members`."""
    return oa_container(0xF003, leader, *members)


def pub_escher(*members, store=(), solver=b"") -> bytes:
    """An Escher/EscherStm stream as Publisher writes it: a DggContainer and a DgContainer, each followed by 4 more
    bytes (libmspub getEscherElementTailLength); the Dg holds one SpgrContainer, its patriarch shape and `members`
    (pub_sp, pub_group), then `solver` (an oa_solver record) when given. `store`: the BStore's file blocks (oa_fbse
    records, pib k + 1 the k-th), a BStoreContainer (0xF001) in the DggContainer after its FDGG when given."""
    bst = (oa_container(0xF001, *store, inst=len(store) & 0xFFF),) if store else ()  # the count in 12 bits
    dgg = oa_container(0xF000, oa_record(0xF006, bytes(16)), *bst) + bytes(4)
    spgr = oa_container(0xF003, pub_sp(group=(0, 0, 0, 0)), *members)
    return dgg + oa_container(0xF002, oa_record(0xF008, bytes(8)), spgr, bytes(solver)) + bytes(4)


def pub_table(rows, cols, cells) -> bytes:
    """A TABLE chunk's blocks (libmspub parseShape): rows (0x66), columns (0x67) and the CELLS chunk's seqnum (0x6B),
    each left out when None."""
    out = b""
    for bid, value in ((0x66, rows), (0x67, cols), (0x6B, cells)):
        if value is not None:
            out += pub_block(bid, 0x20, value)
    return out


def pub_cells(cells) -> bytes:
    """A CELLS chunk's blocks: the count (0x01), then the list (0x02) of one 0x88 item (id 0) a cell, each holding its
    (start row, end row, start column, end column) as sub-blocks 0x01 to 0x04, a 0 left out as Publisher writes it (the
    count a u16, cut there; the reader does not use it)."""
    items = b"".join(pub_block(0, 0x88, b"".join(pub_block(k + 1, 0x20, v) for k, v in enumerate(cell) if v))
                     for cell in cells)
    return pub_block(0x01, 0x18, min(len(cells), 0xFFFF)) + pub_block(0x02, 0xA0, items)


PUB_DOCUMENT = 1   # the DOCUMENT chunk's seqnum in pub_file


def pub_file(pages, shapes=(), stories=(), *, order=None, escher=None, version=0x2C, quill=True, streams=None,
             **quill_kw) -> bytes:
    """A Publisher 2002+ compound file: Contents (pub_contents) with the DOCUMENT chunk (seqnum PUB_DOCUMENT) whose
    page list (block 0x02, entries id 0) is `order` (the pages' seqnums in turn when None), a PAGE chunk for each page
    and a shape chunk for each shape; the Quill stream of `stories` (pub_quill, with `quill_kw`) unless `quill` is
    False; `escher` as Escher/EscherStm when given; `streams` added as they are.
    A page is a dict: seq, shapes (the seqnums of block 0x02, type 0x70), master (its 0x0E name, a str; "" or None for
    none), applied (its 0x0D master seqnum), extra (more blocks, already built, after those). A shape is a dict: seq,
    type (0x01 SHAPE by default), text (its 0x27 text id), parent (its directory parent seqnum), extra (more blocks,
    already built, after those: a TABLE's pub_table blocks, a CELLS chunk's pub_cells)."""
    plist = [p["seq"] for p in pages] if order is None else list(order)
    doc = pub_block(0x02, 0xA0, b"".join(pub_block(0, 0x70, s) for s in plist))
    chunks = [(PUB_DOCUMENT, 0x44, None, doc)]
    for p in pages:
        blocks = pub_block(0x02, 0xA0, b"".join(pub_block(0, 0x70, s) for s in p.get("shapes", ())))
        name = p.get("master") or ""
        blocks += pub_block(0x0E, 0xC0, name.encode("utf-16-le") + (b"\0\0" if name else b""))
        if p.get("applied") is not None:
            blocks += pub_block(0x0D, 0x68, p["applied"])
        blocks += p.get("extra", b"")
        chunks.append((p["seq"], 0x43, p.get("parent"), blocks))
    for s in shapes:
        blocks = pub_block(0xAA, 0x20, 100) + pub_block(0xAB, 0x20, 100)
        if s.get("text") is not None:
            blocks += pub_block(0x27, 0x20, s["text"])
        blocks += s.get("extra", b"")
        chunks.append((s["seq"], s.get("type", 0x01), s.get("parent"), blocks))
    out = {"Contents": pub_contents(chunks, version=version)}
    if quill:
        out["Quill/QuillSub/CONTENTS"] = pub_quill(list(stories), **quill_kw)
    if escher is not None:
        out["Escher/EscherStm"] = escher
    out.update(streams or {})
    return cfb_bytes(out)[0]


# -- Publisher 2000 and 97/98 (libmspub MSPUBParser2k, MSPUBParser97 layouts)

PUB_2K_DOCUMENT = 0x100   # the DOCUMENT chunk's seqnum in pub_2k_contents


def pub_2k_chunk(box=None, text=None, at=0x58, data=None, size=0x60) -> bytes:
    """The bytes of a Publisher 2000 or 97 chunk after its type u16: `size` bytes, the box (xs, ys, xe, ye; s32) at +6
    and the text id u16 at `at` (+0x58 in 2000, +0x46 in 97) when given; `data` makes an image data (0x21) chunk
    instead: 2 bytes, the u32 length at +4 and the bytes from +8."""
    if data is not None:
        return bytes(2) + struct.pack("<I", len(data)) + bytes(data)
    raw = bytearray(size - 2)
    if box is not None:
        struct.pack_into("<4i", raw, 6 - 2, *box)
    if text is not None:
        struct.pack_into("<H", raw, at - 2, text)
    return bytes(raw)


def pub_2k_contents(pages, shapes=(), *, text97=None) -> bytes:
    """A Publisher 2000 or 97 Contents stream (pub_contents version 0x22): the DOCUMENT chunk (0x15, seqnum
    PUB_2K_DOCUMENT), a PAGE chunk (0x14) for each page seqnum, whose parent is the document, and each shape, a
    dict: seq, type (the chunk's first u16), parent (its page or group), and its pub_2k_chunk keywords (box, text,
    at, data, size). `text97`: the 8-bit text of Publisher 97, placed after the trailer with its header (14 bytes, the
    text's start and end u32, four u16), whose offset the u32 at 0x12 holds."""
    chunks = [(PUB_2K_DOCUMENT, 0x15, None, bytes(0x20))]
    chunks += [(p, 0x14, PUB_2K_DOCUMENT, bytes(0x10)) for p in pages]
    for s in shapes:
        kw = {k: s[k] for k in ("box", "text", "at", "data", "size") if k in s}
        chunks.append((s["seq"], s["type"], s.get("parent"), pub_2k_chunk(**kw)))
    body = bytearray(pub_contents(chunks, version=0x22))
    if text97 is not None:
        head = len(body)
        start = head + 14 + 8 + 8
        body += bytes(14) + struct.pack("<II", start, start + len(text97)) + bytes(8) + bytes(text97)
        struct.pack_into("<I", body, 0x12, head)
    return bytes(body)


def pub_2k_file(pages, shapes=(), stories=(), *, quill=True, text97=None, streams=None, **quill_kw) -> bytes:
    """A Publisher 2000 compound file (Contents from pub_2k_contents and the Quill stream of `stories`), or, with
    `quill` False, a Publisher 97/98 one (no Quill; `text97` the Contents text); `streams` added as they are."""
    out = {"Contents": pub_2k_contents(pages, shapes, text97=text97)}
    if quill:
        out["Quill/QuillSub/CONTENTS"] = pub_quill(list(stories), **quill_kw)
    out.update(streams or {})
    return cfb_bytes(out)[0]
